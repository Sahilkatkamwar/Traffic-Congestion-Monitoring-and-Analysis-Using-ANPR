import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import AlertCard from '../components/AlertCard'
import EvidencePanel from '../components/EvidencePanel'
import Empty from '../components/Empty'
import { getAlerts, getBlacklist } from '../lib/api'
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

// What the writer is matching against right now, read from the file it re-reads.
// There is no add button because there is no table: the file IS the control, so
// the panel names it and says what it currently holds.
function Watching({ blacklist }) {
  if (!blacklist) return null

  const { path, exists, count, plates, skipped, error } = blacklist

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
            ? 'No plate is on the blacklist. Add one and it takes effect on the next sighting — there is nothing to restart.'
            : 'There is no blacklist file yet. Create it and add plates to it; it is read as soon as it exists.'
          : `${count} registration${count === 1 ? '' : 's'} matched against every sighting as it is written.`}
      </p>

      <p className="mt-2 text-[12px] text-ink-low">
        Edit <code className="font-plate tracking-plate text-ink-mid">{path}</code>.
        It is re-read whenever it changes.
      </p>

      {error && (
        <p className="mt-3 rounded-control bg-plate-red/15 px-3 py-2 text-[12.5px] text-ink-hi">
          {error}
        </p>
      )}

      {plates?.length > 0 && (
        <ul className="mt-3 flex flex-col gap-1.5">
          {plates.map((entry) => (
            <li key={entry.plate} className="flex items-baseline gap-2">
              <span className="font-plate tracking-plate text-[14px] font-semibold text-ink-hi">
                {entry.plate}
              </span>
              {entry.reason && (
                <span className="min-w-0 truncate text-[12px] text-ink-mid">
                  {entry.reason}
                </span>
              )}
              {entry.severity !== 'critical' && (
                <span className="ml-auto shrink-0 text-[11px] uppercase tracking-wide text-ink-low">
                  {entry.severity}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}

      {/* A line that could not be used is named with its reason, because a
          blacklist that silently ignored an entry is a blacklist nobody can
          trust. */}
      {skipped?.length > 0 && (
        <div className="hairline-t mt-3 pt-3">
          <div className="label text-plate-yellow">
            {skipped.length} line{skipped.length === 1 ? '' : 's'} skipped
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

export default function AlertsScreen() {
  const { navigate } = useRoute()
  const reduced = useReducedMotion()

  const [alerts, setAlerts] = useState([])
  const [blacklist, setBlacklist] = useState(null)
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
      const [rows, watched] = await Promise.all([
        getAlerts(LIMIT, { kind: forKind }),
        getBlacklist(),
      ])
      if (wanted.current !== forKind) return
      setAlerts(rows)
      setBlacklist(watched)
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
                      ? `Nothing is on the blacklist yet. Add a registration to ${blacklist.path} and the next sighting that matches it raises an alert here — within seconds, with no restart.`
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
            <Watching blacklist={blacklist} />
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
