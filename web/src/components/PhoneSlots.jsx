import { useEffect, useMemo, useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import ConnectionTest, { TestResult } from './ConnectionTest'
import MapPicker from './MapPicker'
import StatusPill from './StatusPill'
import { Button, Input } from './Field'
import { createSource, startSource, stopSource, testSource, updateSource } from '../lib/api'

// Three phone cameras, one field each.
//
// This is UI convenience and nothing else. A phone slot saves through the same
// POST /api/sources the general Add-source dialog uses, after the same
// POST /api/sources/test, and its preview frame and map picker are the same two
// components that dialog renders. Nothing here is a second code path, and no
// worker, capture or timestamp behaves differently because a source arrived
// through a slot -- the row it writes is a `network` source like any other.
//
// A slot is bound to a source by its id: Phone 1 is the source `phone_1`. That
// needs no column and no browser storage, so the three slots read the same on
// every machine, survive a restart, and stay bound if the source is renamed.

export const SLOTS = [
  { id: 'phone_1', label: 'Phone 1' },
  { id: 'phone_2', label: 'Phone 2' },
  { id: 'phone_3', label: 'Phone 3' },
]

// What a phone camera app puts on its screen is an address, not always a URL.
// `192.168.1.7:8080` and `192.168.1.7` are what people read off the phone and
// type, so they are completed here -- and the completed URL is shown before
// anything is tested or saved, because a URL the app invented silently is one
// nobody can debug.
export function phoneUrl(text) {
  const raw = String(text ?? '').trim()
  if (!raw) return ''
  if (/^\d+$/.test(raw)) return raw // a webcam index; refused with a reason
  const withScheme = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(raw) ? raw : `http://${raw}`
  let url
  try {
    url = new URL(withScheme)
  } catch {
    return raw // let the server say what is wrong with it
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return withScheme
  // Only a bare host is completed. An address that already carries a path is
  // somebody's own camera app -- DroidCam's /mjpegfeed, a vendor's /stream --
  // and rewriting it would be the app overruling what it was told.
  const bareHost = url.pathname === '/' && !url.search
  if (bareHost) {
    if (!url.port) url.port = '8080'
    url.pathname = '/video'
  }
  return url.toString()
}

export default function PhoneSlots({ sources, onChanged, onNotice }) {
  const bound = useMemo(() => {
    const byId = new Map(sources.map((s) => [s.source_id, s]))
    return SLOTS.map((slot) => ({ ...slot, source: byId.get(slot.id) || null }))
  }, [sources])

  const live = bound.filter((s) => s.source?.status === 'running').length
  const [openMap, setOpenMap] = useState(null)

  return (
    <section className="rounded-card bg-surface-1 p-4 shadow-lift" aria-label="Phone cameras">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <span className="label">Phone cameras</span>
          <p className="mt-1 text-[12.5px] text-ink-mid">
            Open the IP Webcam app on the phone, read the address off its screen,
            and paste it here. Three slots, for testing quickly.
          </p>
        </div>
        <span className="text-[12.5px] tabular-nums text-ink-low">{live} of 3 running</span>
      </div>

      <div className="mt-3.5 flex flex-col gap-2.5">
        {bound.map((slot) => (
          <PhoneSlot
            key={slot.id}
            slot={slot}
            others={sources.filter((s) => s.source_id !== slot.id)}
            mapOpen={openMap === slot.id}
            onToggleMap={() => setOpenMap((current) => (current === slot.id ? null : slot.id))}
            onChanged={onChanged}
            onNotice={onNotice}
          />
        ))}
      </div>
    </section>
  )
}

function PhoneSlot({ slot, others, mapOpen, onToggleMap, onChanged, onNotice }) {
  const reduced = useReducedMotion()
  const source = slot.source
  const [text, setText] = useState('')
  const [place, setPlace] = useState({ lat: null, lon: null })
  const [tested, setTested] = useState(null)
  const [testing, setTesting] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  // The saved source is the truth for a bound slot: whatever was typed here
  // before it was saved is no longer what this phone is.
  useEffect(() => {
    setText(source ? source.uri : '')
    setPlace(
      source && source.lat != null
        ? { lat: source.lat, lon: source.lon }
        : { lat: null, lon: null },
    )
    setTested(null)
    setError(null)
  }, [source?.source_id, source?.uri, source?.lat, source?.lon])

  const url = phoneUrl(text)
  const changed = Boolean(source) && url !== source.uri
  const isIndex = /^\d+$/.test(url)
  // A slot that has never been saved needs a frame before it is worth saving,
  // exactly as the general live flow does. A bound slot whose address has not
  // changed needs nothing -- it is already a source.
  const needsTest = !source || changed
  const canSave = Boolean(url) && !isIndex && !busy && !testing && (!needsTest || tested?.ok)

  async function runTest() {
    setTesting(true)
    setTested(null)
    setError(null)
    try {
      setTested(await testSource(url))
    } catch (err) {
      setTested({ ok: false, error: err.message })
    } finally {
      setTesting(false)
    }
  }

  async function save() {
    setBusy(true)
    setError(null)
    try {
      if (!source) {
        const result = await createSource({
          source_id: slot.id,
          name: slot.label,
          uri: url,
          kind: 'network',
          lat: place.lat,
          lon: place.lon,
        })
        onChanged?.()
        onNotice?.(result.message || `${slot.label} added.`)
      } else {
        // Only send the address when it actually changed. A PATCH carrying a
        // uri restarts the worker, and moving a pin on the map is no reason to
        // interrupt a camera that is reading fine.
        const payload = { lat: place.lat, lon: place.lon }
        if (changed) {
          payload.uri = url
          payload.kind = 'network'
        }
        const result = await updateSource(source.source_id, payload)
        // A worker already reading the old address keeps reading it until it is
        // restarted, so the restart happens here rather than being described.
        if (result.restart_needed) {
          await stopSource(source.source_id)
          await startSource(source.source_id)
          onNotice?.(`${slot.label} is now reading ${url}.`)
        } else {
          onNotice?.(result.message || `${slot.label} saved.`)
        }
        onChanged?.()
      }
      setTested(null)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function toggleRun() {
    setBusy(true)
    setError(null)
    try {
      const running = source.status === 'running'
      const result = running
        ? await stopSource(source.source_id)
        : await startSource(source.source_id)
      onChanged?.()
      onNotice?.(
        result.message || (running ? `${slot.label} stopped.` : `${slot.label} is starting.`),
      )
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const placed = place.lat != null && place.lon != null

  return (
    <div className="rounded-card bg-surface-2/50 p-3.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="flex w-[8.5rem] shrink-0 items-center gap-2">
          <span className="text-[14px] font-semibold text-ink-hi">{slot.label}</span>
          {source && <StatusPill status={source.status} pulse={source.status === 'running'} />}
        </div>

        <label className="min-w-[15rem] flex-1">
          <span className="sr-only">{slot.label} address</span>
          <Input
            value={text}
            onChange={(e) => {
              setText(e.target.value)
              setTested(null)
            }}
            placeholder="http://192.168.1.7:8080/video"
            spellCheck={false}
            inputMode="url"
          />
        </label>

        <div className="flex shrink-0 items-center gap-1.5">
          <ConnectionTest
            uri={url}
            tested={null}
            testing={testing}
            onTest={runTest}
            compact
            label={slot.label}
          />
          <Button variant="primary" onClick={save} disabled={!canSave}>
            {busy ? 'Saving…' : source && changed ? 'Update' : 'Save'}
          </Button>
          {source && (
            <Button onClick={toggleRun} disabled={busy}>
              {source.status === 'running' ? 'Stop' : 'Start'}
            </Button>
          )}
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-ink-low">
        {url && url !== text.trim() && !isIndex && (
          <span>
            Will connect to <span className="text-ink-mid">{url}</span>
          </span>
        )}
        <button
          type="button"
          onClick={onToggleMap}
          className="rounded-control px-1.5 py-0.5 text-[12px] text-ink-mid transition-colors duration-150 hover:bg-surface-3 hover:text-ink-hi"
        >
          {placed
            ? `Placed at ${place.lat.toFixed(5)}, ${place.lon.toFixed(5)} — move it`
            : 'Not on the map yet — place it'}
        </button>
        {source && source.fps ? (
          <span className="tabular-nums">{source.fps.toFixed(1)} fps</span>
        ) : null}
      </div>

      {isIndex && (
        <p className="mt-2 text-[12.5px] text-plate-yellow">
          {url} is a webcam index, not a phone address. Use Add source → Live camera
          for a camera plugged into this machine.
        </p>
      )}

      {source?.error && <p className="mt-2 text-[12.5px] text-plate-red">{source.error}</p>}

      {tested && <TestResult tested={tested} className="mt-2.5" />}

      {error && (
        <p role="alert" className="mt-2 text-[12.5px] text-plate-red">
          {error}
        </p>
      )}

      <AnimatePresence initial={false}>
        {mapOpen && (
          <motion.div
            initial={reduced ? false : { opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={reduced ? { opacity: 1 } : { opacity: 0, height: 0 }}
            transition={reduced ? { duration: 0 } : { type: 'spring', stiffness: 300, damping: 32 }}
            className="overflow-hidden"
          >
            <div className="mt-3">
              <MapPicker
                value={placed ? place : {}}
                onPick={({ lat, lon }) => setPlace({ lat, lon })}
                others={others}
                height={200}
              />
              <p className="mt-1.5 text-[12px] text-ink-low">
                {source
                  ? 'Press Save to keep this placement.'
                  : 'A phone with no placement still runs and still writes sightings — it just has no marker on the map.'}
              </p>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
