import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import AlertCard from '../components/AlertCard'
import EvidencePanel from '../components/EvidencePanel'
import Empty from '../components/Empty'
import { Button, Field, Input, Select } from '../components/Field'
import {
  addBlacklistPlate,
  clearControlRoomNumber,
  getAlerts,
  getBlacklist,
  getNotifications,
  removeBlacklistPlate,
  setControlRoomNumber,
} from '../lib/api'
import { openLiveFeed } from '../lib/socket'
import { useRoute } from '../lib/router'

// Alerts, newest first, arriving as they are raised.
//
// The socket carries alerts the same way it carries sightings -- the writer
// publishes after the commit -- so an alert appears here within a second of the
// sighting that caused it rather than on the next poll. A reconnect reloads
// from /api/alerts instead of replaying: the socket is a notification, the
// database is the record.
//
// The screen is a column, not a table. Every alert has to show its evidence,
// and a row of 12px text with a thumbnail in it is neither readable nor
// checkable -- the paired crops of an impossible transition ARE the alert.

const LIMIT = 100

const KINDS = [
  { value: null, label: 'All' },
  { value: 'blacklist', label: 'Blacklist' },
  { value: 'impossible_transition', label: 'Impossible transitions' },
]

function Filter({ options, value, onChange, label }) {
  return (
    <div role="group" aria-label={label} className="flex items-center gap-1">
      {options.map((option) => {
        const active = option.value === value
        return (
          <button
            key={option.label}
            type="button"
            onClick={() => onChange(option.value)}
            aria-pressed={active}
            className={`rounded-control px-3 py-1.5 text-[13px] transition-colors duration-150 ${
              active
                ? 'bg-surface-2 font-semibold text-ink-hi'
                : 'text-ink-mid hover:bg-surface-1 hover:text-ink-hi'
            }`}
          >
            {option.label}
          </button>
        )
      })}
    </div>
  )
}

const SEVERITY_OPTIONS = [
  { value: 'critical', label: 'Critical' },
  { value: 'warning', label: 'Warning' },
  { value: 'info', label: 'Info' },
]

// What the writer is matching against right now, and the two edits that change
// it.
//
// Add and Remove answer with the list as it now stands, so what this panel
// shows is what the next sighting will be matched against -- there is no window
// in which the screen and the writer disagree, and nothing to restart. Where
// the list is stored is the server's business and is not shown here: the person
// reading this screen adds a registration, they do not edit a file.
function Watching({ blacklist, onChanged }) {
  const [plate, setPlate] = useState('')
  const [reason, setReason] = useState('')
  const [severity, setSeverity] = useState('critical')
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)

  const add = async (event) => {
    event.preventDefault()
    setError(null)
    setBusy('add')
    try {
      onChanged(
        await addBlacklistPlate({
          plate,
          reason: reason.trim() || null,
          severity,
        }),
      )
      setPlate('')
      setReason('')
      setSeverity('critical')
    } catch (failure) {
      setError(failure.message)
    } finally {
      setBusy(null)
    }
  }

  const remove = async (entry) => {
    setError(null)
    setBusy(entry.plate)
    try {
      onChanged(await removeBlacklistPlate(entry.plate))
    } catch (failure) {
      setError(failure.message)
    } finally {
      setBusy(null)
    }
  }

  if (!blacklist) return null
  const { exists, count, plates, skipped, error: fileError } = blacklist

  return (
    <section
      className="rounded-card bg-surface-1 p-4"
      style={{ boxShadow: 'var(--shadow-lift)' }}
      aria-label="Blacklist"
    >
      <div className="flex items-baseline justify-between gap-3">
        <div className="label">Watching</div>
        <span className="text-count font-semibold tabular-nums leading-none">{count}</span>
      </div>

      <p className="mt-2 text-[13px] text-ink-mid">
        {count === 0
          ? exists
            ? 'No plate is on the blacklist. Add one below and it takes effect on the next sighting — there is nothing to restart.'
            : 'Nothing is being watched yet. Add a registration below and it takes effect on the next sighting.'
          : `${count} registration${count === 1 ? '' : 's'} matched against every sighting as it is written.`}
      </p>

      {/* The file failing to parse is the one state where adding is refused
          rather than merged into, so it is said here and not only on failure. */}
      {fileError && (
        <p className="mt-3 rounded-control bg-plate-red/15 px-3 py-2 text-[12.5px] text-ink-hi">
          {fileError}
        </p>
      )}

      <form onSubmit={add} className="hairline-t mt-3 flex flex-col gap-2.5 pt-3">
        <Field label="Registration">
          <Input
            value={plate}
            onChange={(event) => setPlate(event.target.value.toUpperCase())}
            placeholder="MH15JS4241"
            autoComplete="off"
            spellCheck={false}
            className="font-plate tracking-plate"
            aria-label="Registration to watch"
          />
        </Field>
        <Field label="Reason" hint="Shown in the alert and in the SMS. Optional.">
          <Input
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="reported stolen 2026-08-14"
            aria-label="Why this plate is watched"
          />
        </Field>
        <Field label="Severity">
          <Select
            value={severity}
            onChange={(event) => setSeverity(event.target.value)}
            aria-label="Severity"
          >
            {SEVERITY_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
        </Field>
        <Button
          type="submit"
          variant="primary"
          disabled={busy !== null || plate.trim().length === 0}
          className="self-start"
        >
          {busy === 'add' ? 'Adding…' : 'Add plate'}
        </Button>
        {error && (
          <p className="text-[12.5px] text-plate-red" role="alert">
            {error}
          </p>
        )}
      </form>

      {plates?.length > 0 && (
        <ul className="hairline-t mt-3 flex flex-col gap-1 pt-3">
          {plates.map((entry) => (
            <li
              key={entry.plate}
              className="group flex items-baseline gap-2 rounded-control px-1.5 py-1 transition-colors duration-150 hover:bg-surface-2"
            >
              <span className="font-plate tracking-plate text-[14px] font-semibold text-ink-hi">
                {entry.plate}
              </span>
              {entry.reason && (
                <span className="min-w-0 truncate text-[12px] text-ink-mid">
                  {entry.reason}
                </span>
              )}
              {entry.severity !== 'critical' && (
                <span className="shrink-0 text-[11px] uppercase tracking-wide text-ink-low">
                  {entry.severity}
                </span>
              )}
              <button
                type="button"
                onClick={() => remove(entry)}
                disabled={busy !== null}
                aria-label={`Remove ${entry.plate} from the blacklist`}
                className="ml-auto shrink-0 rounded-control px-2 py-0.5 text-[12px] text-ink-low
                  transition-colors duration-150 hover:bg-plate-red/20 hover:text-plate-red
                  focus-visible:text-plate-red disabled:cursor-not-allowed"
              >
                {busy === entry.plate ? '…' : 'Remove'}
              </button>
            </li>
          ))}
        </ul>
      )}

      {/* A line that could not be used is named with its reason, because a
          blacklist that silently ignored an entry is a blacklist nobody can
          trust. Adding or removing a plate never removes one of these. */}
      {skipped?.length > 0 && (
        <div className="hairline-t mt-3 pt-3">
          <div className="label text-plate-yellow">
            {skipped.length} entr{skipped.length === 1 ? 'y' : 'ies'} not being watched
          </div>
          <ul className="mt-1.5 flex flex-col gap-1 text-[12px] text-ink-mid">
            {skipped.map((item, index) => (
              <li key={`${item.entry}-${index}`}>
                <span className="font-plate tracking-plate text-ink-hi">{item.entry}</span>
                {' — '}
                {item.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}

// Where a blacklist alert is sent, and the one place it is set.
//
// One number, and it is the control room's -- never a vehicle owner's. Nothing
// in this app knows who owns a vehicle.
//
// The number is typed here and saved here. Nothing about the SMS account is
// readable from a browser and nothing about it is asked for: that belongs to
// whoever runs the server, and a screen that asked a control-room operator for
// an API key would be asking the wrong person for the wrong thing.
//
// The panel is honest about the two things a browser cannot see: whether the
// server can actually send, and what happened to the last message. A
// notification that silently failed is the one state worth a red line here.
function ControlRoom({ status, onChanged }) {
  const saved = status?.configured_number || ''
  const [number, setNumber] = useState(saved)
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  const [saidOk, setSaidOk] = useState(null)

  // The field follows the server whenever the saved number changes underneath
  // it -- a reload, or another tab -- but never while it is being typed into.
  const shown = useRef(saved)
  useEffect(() => {
    if (saved !== shown.current) {
      shown.current = saved
      setNumber(saved)
    }
  }, [saved])

  if (!status) return null
  const { police_number: number_in_use, ready, reason, sent, failed, last } = status
  const changed = number.trim() !== saved

  const save = async (event) => {
    event.preventDefault()
    setError(null)
    setSaidOk(null)
    setBusy('save')
    try {
      const next = await setControlRoomNumber(number.trim())
      shown.current = next.configured_number || ''
      setNumber(shown.current)
      onChanged(next)
      setSaidOk(
        next.ready
          ? `Saved. New blacklist alerts go to ${next.police_number}.`
          : `Saved ${next.police_number}.`,
      )
    } catch (failure) {
      setError(failure.message)
    } finally {
      setBusy(null)
    }
  }

  const clear = async () => {
    setError(null)
    setSaidOk(null)
    setBusy('clear')
    try {
      const next = await clearControlRoomNumber()
      shown.current = ''
      setNumber('')
      onChanged(next)
      setSaidOk('Number removed. Alerts are still raised and still shown here.')
    } catch (failure) {
      setError(failure.message)
    } finally {
      setBusy(null)
    }
  }

  return (
    <section
      className="mt-5 rounded-card bg-surface-1 p-4"
      style={{ boxShadow: 'var(--shadow-lift)' }}
      aria-label="Control room"
    >
      <div className="flex items-baseline justify-between gap-3">
        <div className="label">Control room</div>
        <span
          className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide ${
            ready ? 'bg-plate-green/20 text-plate-green' : 'bg-surface-2 text-ink-low'
          }`}
        >
          {ready ? 'Sending' : 'Not sending'}
        </span>
      </div>

      {number_in_use && (
        <p className="mt-2 font-plate tracking-plate text-[16px] font-semibold text-ink-hi">
          {number_in_use}
        </p>
      )}
      <p className="mt-1 text-[12.5px] text-ink-mid">{reason}</p>

      <form onSubmit={save} className="hairline-t mt-3 flex flex-col gap-2.5 pt-3">
        <Field
          label="Phone number"
          hint="With the country code, like +919876543210."
        >
          <Input
            type="tel"
            value={number}
            onChange={(event) => setNumber(event.target.value)}
            placeholder="+919876543210"
            autoComplete="off"
            spellCheck={false}
            className="font-plate tracking-plate"
            aria-label="Control-room phone number"
          />
        </Field>
        <div className="flex items-center gap-2">
          <Button
            type="submit"
            variant="primary"
            disabled={busy !== null || number.trim().length === 0 || !changed}
          >
            {busy === 'save' ? 'Saving…' : saved ? 'Update number' : 'Save number'}
          </Button>
          {saved && (
            <Button type="button" variant="quiet" onClick={clear} disabled={busy !== null}>
              {busy === 'clear' ? 'Removing…' : 'Remove'}
            </Button>
          )}
        </div>
        {error && (
          <p className="text-[12.5px] text-plate-red" role="alert">
            {error}
          </p>
        )}
        {saidOk && !error && (
          <p className="text-[12.5px] text-plate-green" role="status">
            {saidOk}
          </p>
        )}
      </form>

      <p className="hairline-t mt-3 pt-3 text-[12px] text-ink-low">
        One SMS per new blacklist alert — never for an alert already raised, and
        never for footage already in the database.
      </p>

      {(sent > 0 || failed > 0) && (
        <p className="mt-2 text-[12.5px] text-ink-mid">
          {sent} sent{failed > 0 ? `, ${failed} failed` : ''} this run.
        </p>
      )}

      {last && !last.ok && (
        <p className="mt-2 rounded-control bg-plate-red/15 px-3 py-2 text-[12.5px] text-ink-hi">
          The last notification did not go out: {last.detail}
        </p>
      )}
    </section>
  )
}

export default function AlertsScreen() {
  const { navigate } = useRoute()
  const reduced = useReducedMotion()

  const [alerts, setAlerts] = useState([])
  const [blacklist, setBlacklist] = useState(null)
  const [notifications, setNotifications] = useState(null)
  const [kind, setKind] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(null)
  const [selected, setSelected] = useState(null)
  const [newIds, setNewIds] = useState(() => new Set())

  // Which filter the in-flight request is for. Without this a slow request for
  // "All" can land after a fast one for "Blacklist" and quietly show the wrong
  // list under the wrong pressed button.
  const wanted = useRef(kind)

  const load = useCallback(async (forKind) => {
    wanted.current = forKind
    try {
      const [rows, watched, sending] = await Promise.all([
        getAlerts(LIMIT, { kind: forKind }),
        getBlacklist(),
        getNotifications(),
      ])
      if (wanted.current !== forKind) return
      setAlerts(rows)
      setBlacklist(watched)
      setNotifications(sending)
      setLoadError(null)
    } catch (error) {
      if (wanted.current !== forKind) return
      setLoadError(error.message)
    } finally {
      if (wanted.current === forKind) setLoading(false)
    }
  }, [])

  useEffect(() => {
    setLoading(true)
    load(kind)
  }, [kind, load])

  useEffect(() => {
    const close = openLiveFeed({
      onStatus: (status) => {
        // Anything raised while the socket was down is in the database, not in
        // the socket, so a reconnect reloads rather than replays.
        if (status === 'live') load(wanted.current)
      },
      onEvent: (event) => {
        if (event.type !== 'alert') return
        const row = event.alert
        // The socket carries the stored row, not the hydrated one -- the
        // evidence is a join. Refetching keeps this screen showing exactly what
        // /api/alerts would return rather than a thinner version of it.
        setNewIds((current) => new Set(current).add(row.alert_id))
        load(wanted.current)
      },
    })
    return close
  }, [load])

  const heading = useMemo(() => {
    if (alerts.length === 0) return null
    const critical = alerts.filter((a) => a.severity === 'critical').length
    return { total: alerts.length, critical }
  }, [alerts])

  const selectedSighting = useMemo(() => {
    if (selected === null) return null
    for (const alert of alerts) {
      const found = (alert.sightings || []).find((s) => s.sighting_id === selected)
      if (found) return found
    }
    return null
  }, [alerts, selected])

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-[76rem] flex-col gap-5 px-5 py-6">
        <header className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <div className="label">Alerts</div>
            <div className="mt-0.5 flex items-baseline gap-3">
              <span className="text-count font-semibold tabular-nums">
                {loading ? '—' : alerts.length}
              </span>
              <span className="text-[13px] text-ink-mid">
                {heading
                  ? heading.critical > 0
                    ? `${heading.critical} critical`
                    : 'none critical'
                  : 'newest first'}
              </span>
            </div>
          </div>
          <Filter options={KINDS} value={kind} onChange={setKind} label="Alert kind" />
        </header>

        <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_20rem]">
          <div className="min-w-0">
            {loadError ? (
              <Empty
                title="The alerts could not load."
                action={`${loadError} Check the app is running, then reload this page.`}
              />
            ) : loading ? (
              <p className="px-4 py-10 text-center text-[13px] text-ink-low">Loading…</p>
            ) : alerts.length === 0 ? (
              <Empty
                title={
                  kind === null
                    ? 'Nothing has raised an alert.'
                    : 'No alert of this kind.'
                }
                action={
                  kind === 'impossible_transition'
                    ? 'An impossible transition needs one vehicle read at two cameras that are placed on the map. Place your sources in Sources and run them, and any journey too fast to have happened appears here.'
                    : blacklist && blacklist.count === 0
                      ? 'Nothing is on the blacklist yet. Add a registration in the panel beside this and the next sighting that matches it raises an alert here — within seconds, with no restart.'
                      : 'Alerts appear here as they are raised, while sources are running. Nothing has matched yet.'
                }
              />
            ) : (
              <AnimatePresence initial={false}>
                <motion.div layout={!reduced} className="flex flex-col gap-3">
                  {alerts.map((alert) => (
                    <AlertCard
                      key={alert.alert_id}
                      alert={alert}
                      isNew={newIds.has(alert.alert_id)}
                      onOpenSighting={(sighting) => setSelected(sighting.sighting_id)}
                    />
                  ))}
                </motion.div>
              </AnimatePresence>
            )}
          </div>

          <aside className="lg:sticky lg:top-6 lg:self-start">
            {/* Add and Remove answer with the list as it now reads from disk,
                so the panel is updated from the response rather than from a
                refetch -- there is no window in which the screen and the file
                disagree. */}
            <Watching blacklist={blacklist} onChanged={setBlacklist} />
            <ControlRoom status={notifications} onChanged={setNotifications} />
          </aside>
        </div>
      </div>

      <EvidencePanel
        sighting={selectedSighting}
        sourceName={
          selectedSighting
            ? selectedSighting.source_name || selectedSighting.source_id
            : ''
        }
        onClose={() => setSelected(null)}
        onTrace={(plate) => navigate(`/trace/${encodeURIComponent(plate)}`)}
      />
    </div>
  )
}
