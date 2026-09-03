import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import SourceCard from '../components/SourceCard'
import FeedTile from '../components/FeedTile'
import AddSource from '../components/AddSource'
import MapPicker from '../components/MapPicker'
import Modal from '../components/Modal'
import Empty from '../components/Empty'
import { Button, Field, Input, Select } from '../components/Field'
import {
  deleteSource,
  getSources,
  startSource,
  stopSource,
  updateSource,
} from '../lib/api'
import { openLiveFeed } from '../lib/socket'

// Sources are runtime state. Everything on this screen writes to the database
// and takes effect without a restart: adding one starts a worker, removing one
// stops it, and the status shown is the row, not a guess made in the browser.
//
// Status and progress arrive over the same websocket the Live feed uses, so a
// progress bar here moves because the worker moved it, not because a timer in
// the browser is counting.

const TABS = [
  { id: 'list', label: 'Sources' },
  { id: 'wall', label: 'Camera wall' },
]

export default function SourcesScreen() {
  const reduced = useReducedMotion()
  const [tab, setTab] = useState('list')
  const [sources, setSources] = useState([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(null)
  const [busyId, setBusyId] = useState(null)
  const [notice, setNotice] = useState(null)
  const [adding, setAdding] = useState(false)
  const [editing, setEditing] = useState(null)
  const [confirming, setConfirming] = useState(null)
  const noticeTimer = useRef(null)

  const load = useCallback(async () => {
    try {
      setSources(await getSources())
      setLoadError(null)
    } catch (error) {
      setLoadError(error.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    const close = openLiveFeed({
      onStatus: (status) => {
        if (status === 'live') load()
      },
      onEvent: (event) => {
        if (event.type === 'source') {
          const row = event.source
          setSources((current) => {
            const index = current.findIndex((s) => s.source_id === row.source_id)
            if (index === -1) return [...current, row].sort(bySourceId)
            const next = [...current]
            next[index] = row
            return next
          })
        } else if (event.type === 'source_removed') {
          setSources((current) => current.filter((s) => s.source_id !== event.source_id))
        }
      },
    })
    return close
  }, [load])

  useEffect(() => () => clearTimeout(noticeTimer.current), [])

  const say = useCallback((message, tone = 'info') => {
    setNotice({ message, tone })
    clearTimeout(noticeTimer.current)
    noticeTimer.current = setTimeout(() => setNotice(null), 7000)
  }, [])

  const act = useCallback(
    async (source, fn, done) => {
      setBusyId(source.source_id)
      try {
        const result = await fn(source.source_id)
        await load()
        say(done?.(result) ?? 'Done.')
      } catch (error) {
        say(error.message, 'error')
      } finally {
        setBusyId(null)
      }
    },
    [load, say],
  )

  const running = useMemo(() => sources.filter((s) => s.status === 'running'), [sources])
  const placed = useMemo(
    () => sources.filter((s) => s.lat != null && s.lon != null),
    [sources],
  )

  async function confirmDelete(source, withSightings) {
    setBusyId(source.source_id)
    try {
      const result = await deleteSource(source.source_id, { deleteSightings: withSightings })
      setConfirming(null)
      await load()
      say(
        result.sightings_deleted
          ? `Deleted ${source.name} and its ${result.sightings_deleted} sightings.`
          : `Deleted ${source.name}.`,
      )
    } catch (error) {
      if (error.status === 409) setConfirming({ source, detail: error.message })
      else say(error.message, 'error')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-5 py-6">
        <header className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <div className="label">Sources</div>
            <div className="mt-0.5 flex items-baseline gap-3">
              <span className="text-count font-semibold tabular-nums">{sources.length}</span>
              <span className="text-[13.5px] text-ink-mid">
                {running.length} running · {placed.length} on the map
              </span>
            </div>
          </div>
          <Button variant="primary" onClick={() => setAdding(true)}>
            Add source
          </Button>
        </header>

        <nav className="mt-5 flex items-center gap-1" aria-label="Sources views">
          {TABS.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => setTab(item.id)}
              aria-current={tab === item.id ? 'true' : undefined}
              className={`rounded-control px-3 py-1.5 text-[13.5px] transition-colors duration-150 ${
                tab === item.id
                  ? 'bg-surface-2 font-semibold text-ink-hi'
                  : 'text-ink-mid hover:bg-surface-1 hover:text-ink-hi'
              }`}
            >
              {item.label}
              {item.id === 'wall' && running.length > 0 && (
                <span className="ml-1.5 tabular-nums text-ink-low">{running.length}</span>
              )}
            </button>
          ))}
        </nav>

        <AnimatePresence>
          {notice && (
            <motion.p
              role="status"
              initial={reduced ? false : { opacity: 0, y: -6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={reduced ? { opacity: 1 } : { opacity: 0 }}
              transition={{ duration: 0.15 }}
              className={`mt-4 rounded-control px-3.5 py-2.5 text-[13px] ${
                notice.tone === 'error'
                  ? 'bg-plate-red/15 text-ink-hi'
                  : 'bg-surface-2 text-ink-mid'
              }`}
            >
              {notice.message}
            </motion.p>
          )}
        </AnimatePresence>

        <div className="mt-4 pb-10">
          {loadError ? (
            <Empty
              title="The source list could not load."
              action={`${loadError} Check the app is running, then reload this page.`}
            />
          ) : loading ? (
            <p className="py-10 text-center text-[13px] text-ink-low">Loading…</p>
          ) : sources.length === 0 ? (
            <Empty
              title="No sources yet."
              action="Add a live camera, a recorded video, or a still image. A source starts processing the moment it is added."
            />
          ) : tab === 'list' ? (
            <motion.div layout={!reduced} className="flex flex-col gap-3">
              <AnimatePresence initial={false}>
                {sources.map((source) => (
                  <SourceCard
                    key={source.source_id}
                    source={source}
                    busy={busyId === source.source_id}
                    onStart={(s) =>
                      act(s, startSource, () => `${s.name} is starting. Models load first, so give it a moment.`)
                    }
                    onStop={(s) => act(s, stopSource, () => `${s.name} stopped.`)}
                    onEdit={setEditing}
                    onDelete={(s) => confirmDelete(s, false)}
                  />
                ))}
              </AnimatePresence>
            </motion.div>
          ) : running.length === 0 ? (
            <Empty
              title="No source is running."
              action="The wall shows frames from workers as they decode them. Start a source and its feed appears here."
            />
          ) : (
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {running.map((source) => (
                <FeedTile
                  key={source.source_id}
                  source={source}
                  onOpenSource={() => setTab('list')}
                />
              ))}
            </div>
          )}
        </div>
      </div>

      <AddSource
        open={adding}
        sources={sources}
        onClose={() => setAdding(false)}
        onAdded={(added, message) => {
          load()
          say(message || `Added ${added.length} source(s).`)
        }}
      />

      <EditSource
        source={editing}
        others={sources.filter((s) => s.source_id !== editing?.source_id)}
        onClose={() => setEditing(null)}
        onSaved={(message) => {
          load()
          say(message)
          setEditing(null)
        }}
        onError={(message) => say(message, 'error')}
      />

      <Modal
        open={Boolean(confirming)}
        title={`Delete ${confirming?.source.name}?`}
        sub={confirming?.detail}
        onClose={() => setConfirming(null)}
        footer={
          <>
            <Button variant="quiet" onClick={() => setConfirming(null)}>
              Keep it
            </Button>
            <Button
              variant="danger"
              onClick={() => confirmDelete(confirming.source, true)}
              disabled={busyId === confirming?.source.source_id}
            >
              Delete the source and its sightings
            </Button>
          </>
        }
      >
        <p className="text-[13.5px] text-ink-mid">
          The evidence crops stay on disk, but the sightings that point at them are
          removed from the database and from every trajectory. This cannot be undone.
        </p>
      </Modal>
    </div>
  )
}

function EditSource({ source, others, onClose, onSaved, onError }) {
  const [form, setForm] = useState(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (!source) {
      setForm(null)
      return
    }
    setForm({
      name: source.name,
      frame_skip: source.frame_skip,
      lat: source.lat,
      lon: source.lon,
      start_time: source.start_time ? toLocalInput(source.start_time) : '',
    })
  }, [source])

  if (!source || !form) return null
  const recorded = source.kind === 'file' || source.kind === 'image'

  async function save() {
    setSaving(true)
    try {
      const payload = {
        name: form.name,
        frame_skip: Number(form.frame_skip),
        lat: form.lat,
        lon: form.lon,
      }
      if (recorded) payload.start_time = form.start_time || null
      const result = await updateSource(source.source_id, payload)
      onSaved(result.message)
    } catch (error) {
      onError(error.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      open
      title={`Edit ${source.name}`}
      sub={source.uri}
      onClose={onClose}
      footer={
        <>
          <Button variant="quiet" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" onClick={save} disabled={saving || !form.name.trim()}>
            {saving ? 'Saving…' : 'Save'}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <Field label="Name">
          <Input
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
        </Field>

        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            label="Frame skip"
            hint={
              source.status === 'running'
                ? 'Restart this source for the change to reach its worker.'
                : 'Process every Nth frame.'
            }
          >
            <Select
              value={form.frame_skip}
              onChange={(e) => setForm({ ...form, frame_skip: e.target.value })}
            >
              {[1, 2, 3, 5, 8].map((n) => (
                <option key={n} value={n}>
                  {n}
                  {n === 1 ? ' — every frame' : n === 3 ? ' — default' : ''}
                </option>
              ))}
            </Select>
          </Field>

          {recorded && (
            <Field
              label="Recorded at"
              hint="Every timestamp this source produces is measured from here."
            >
              <Input
                type="datetime-local"
                value={form.start_time}
                onChange={(e) => setForm({ ...form, start_time: e.target.value })}
              />
            </Field>
          )}
        </div>

        <div>
          <span className="label block">Location</span>
          <div className="mt-1.5">
            <MapPicker
              value={form.lat != null ? { lat: form.lat, lon: form.lon } : {}}
              onPick={({ lat, lon }) => setForm({ ...form, lat, lon })}
              others={others}
              height={240}
            />
          </div>
        </div>
      </div>
    </Modal>
  )
}

const bySourceId = (a, b) => a.source_id.localeCompare(b.source_id)

// Stored timestamps are ISO-8601 UTC. A datetime-local input wants local time
// with no zone, so the offset is applied here and undone by the server.
function toLocalInput(iso) {
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return ''
  at.setMinutes(at.getMinutes() - at.getTimezoneOffset())
  return at.toISOString().slice(0, 16)
}
