import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import MapCanvas from '../components/MapCanvas'
import SightingCard from '../components/SightingCard'
import EvidencePanel from '../components/EvidencePanel'
import Empty from '../components/Empty'
import { getAlerts, getSightings, getSources } from '../lib/api'
import { openLiveFeed } from '../lib/socket'
import { useRoute } from '../lib/router'

// Full-bleed map with the feed floating over it. The panel is the only blurred
// surface on the screen, which is what makes it read as floating rather than
// as a column the map happens to sit beside.

const FEED_LIMIT = 80
// How long a source keeps pulsing after it emitted. Long enough to notice,
// short enough that a busy camera does not simply pulse forever.
const PULSE_MS = 2200

const CONNECTION = {
  live: { text: 'Live', color: 'bg-plate-green' },
  connecting: { text: 'Connecting', color: 'bg-plate-yellow' },
  offline: { text: 'Reconnecting', color: 'bg-plate-red' },
}

export default function LiveScreen() {
  const { navigate } = useRoute()
  const reduced = useReducedMotion()

  const [sources, setSources] = useState([])
  const [sightings, setSightings] = useState([])
  const [alerts, setAlerts] = useState([])
  const [connection, setConnection] = useState('connecting')
  const [loadError, setLoadError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState(null)
  const [activeSourceIds, setActiveSourceIds] = useState(() => new Set())
  const [newIds, setNewIds] = useState(() => new Set())

  const pulseTimers = useRef(new Map())

  const load = useCallback(async () => {
    try {
      const [sourceRows, sightingRows, alertRows] = await Promise.all([
        getSources(),
        getSightings(FEED_LIMIT),
        getAlerts(10),
      ])
      setSources(sourceRows)
      setSightings(sightingRows)
      setAlerts(alertRows)
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

  // Mark a source as active, and stop marking it a couple of seconds later.
  const markActive = useCallback((sourceId) => {
    setActiveSourceIds((current) => {
      if (current.has(sourceId)) return current
      const next = new Set(current)
      next.add(sourceId)
      return next
    })
    clearTimeout(pulseTimers.current.get(sourceId))
    pulseTimers.current.set(
      sourceId,
      setTimeout(() => {
        setActiveSourceIds((current) => {
          const next = new Set(current)
          next.delete(sourceId)
          return next
        })
      }, PULSE_MS),
    )
  }, [])

  useEffect(() => {
    const close = openLiveFeed({
      onStatus: (status) => {
        setConnection(status)
        // A reconnect reloads rather than replaying: anything that happened
        // while the socket was down is in the database, not in the socket.
        if (status === 'live') load()
      },
      onEvent: (event) => {
        if (event.type === 'sighting') {
          const row = event.sighting
          markActive(row.source_id)
          setSightings((current) => {
            const without = current.filter((s) => s.sighting_id !== row.sighting_id)
            return [row, ...without].slice(0, FEED_LIMIT)
          })
          if (event.new) {
            setNewIds((current) => new Set(current).add(row.sighting_id))
          }
        } else if (event.type === 'source') {
          const row = event.source
          setSources((current) => {
            const index = current.findIndex((s) => s.source_id === row.source_id)
            if (index === -1) return [...current, row]
            const next = [...current]
            next[index] = row
            return next
          })
        } else if (event.type === 'source_removed') {
          // A source deleted on the Sources screen has to leave this map too,
          // or its marker outlives the record it was drawn from.
          setSources((current) => current.filter((s) => s.source_id !== event.source_id))
        }
      },
    })
    return () => {
      close()
      for (const timer of pulseTimers.current.values()) clearTimeout(timer)
      pulseTimers.current.clear()
    }
  }, [load, markActive])

  const sourceNames = useMemo(
    () => new Map(sources.map((s) => [s.source_id, s.name])),
    [sources],
  )
  const placedCount = useMemo(
    () => sources.filter((s) => s.lat != null && s.lon != null).length,
    [sources],
  )
  const runningCount = useMemo(
    () => sources.filter((s) => s.status === 'running').length,
    [sources],
  )
  const erroredSources = useMemo(
    () => sources.filter((s) => s.status === 'error'),
    [sources],
  )

  const status = CONNECTION[connection] || CONNECTION.connecting
  const selectedSighting = sightings.find((s) => s.sighting_id === selected) || null

  return (
    <div className="relative h-full w-full">
      <MapCanvas
        sources={sources}
        activeSourceIds={activeSourceIds}
        onSelectSource={() => {}}
      />

      {/* Nothing is placed yet, so say what places it rather than showing an
          empty map with no explanation. */}
      {!loading && sources.length > 0 && placedCount === 0 && (
        <div className="pointer-events-none absolute inset-0 z-[500] grid place-items-center px-6">
          <div className="glass pointer-events-auto max-w-sm rounded-card px-6 py-5 text-center">
            <p className="text-[15px] font-semibold">No source is on the map yet.</p>
            <p className="mt-1 text-[13px] text-ink-mid">
              {sources.length} source{sources.length === 1 ? '' : 's'} exist
              {sources.length === 1 ? 's' : ''} but none has coordinates. Place them
              in Sources and they appear here.
            </p>
          </div>
        </div>
      )}

      <aside
        className="absolute left-4 top-4 z-[600] flex max-h-[calc(100%-2rem)] w-[27rem] max-w-[calc(100vw-2rem)] flex-col overflow-hidden rounded-card glass"
        aria-label="Live sightings"
      >
        <header className="flex items-start justify-between gap-4 px-4 pt-4">
          <div>
            <div className="label">Sightings</div>
            <div className="mt-0.5 flex items-baseline gap-2">
              <span className="text-count font-semibold tabular-nums">{sightings.length}</span>
              <span className="text-[13px] text-ink-mid">
                {runningCount > 0
                  ? `${runningCount} source${runningCount === 1 ? '' : 's'} running`
                  : 'no source running'}
              </span>
            </div>
          </div>
          <div className="flex items-center gap-2 pt-1" title={`Feed is ${status.text.toLowerCase()}`}>
            <span className={`h-1.5 w-1.5 rounded-full ${status.color}`} />
            <span className="label text-ink-mid">{status.text}</span>
          </div>
        </header>

        {/* Alert strip. Rows come from the alerts table, which P5 fills -- until
            then there is nothing here and nothing is shown. */}
        {alerts.length > 0 && (
          <div className="mt-3 px-4">
            {alerts.slice(0, 2).map((alert) => (
              <div
                key={alert.alert_id}
                className="mb-1.5 rounded-control bg-plate-red/15 px-3 py-2 text-[13px] text-ink-hi"
              >
                <span className="font-plate tracking-plate">{alert.plate_text}</span>
                <span className="ml-2 text-ink-mid">{alert.detail}</span>
              </div>
            ))}
          </div>
        )}

        {erroredSources.length > 0 && (
          <div className="mt-3 px-4">
            {erroredSources.map((source) => (
              <div
                key={source.source_id}
                className="mb-1.5 rounded-control bg-plate-red/15 px-3 py-2 text-[12.5px]"
              >
                <span className="font-semibold text-plate-red">{source.name} stopped.</span>{' '}
                <span className="text-ink-mid">{source.error}</span>
              </div>
            ))}
          </div>
        )}

        <div className="mt-3 flex-1 overflow-y-auto px-2 pb-2">
          {loadError ? (
            <Empty
              title="The feed could not load."
              action={`${loadError} Check the app is running, then reload this page.`}
            />
          ) : loading ? (
            <p className="px-4 py-8 text-center text-[13px] text-ink-low">Loading…</p>
          ) : sightings.length === 0 ? (
            <Empty
              title="No vehicle has been seen yet."
              action={
                sources.length === 0
                  ? 'Add a camera or a recorded video in Sources to start reading plates.'
                  : 'Sources exist but none is running. Start one in Sources and sightings appear here as they happen.'
              }
            />
          ) : (
            <AnimatePresence initial={false}>
              <motion.div layout={!reduced} className="flex flex-col gap-1.5">
                {sightings.map((sighting) => (
                  <SightingCard
                    key={sighting.sighting_id}
                    sighting={sighting}
                    sourceName={sourceNames.get(sighting.source_id) || sighting.source_id}
                    isNew={newIds.has(sighting.sighting_id)}
                    onOpen={(row) => setSelected(row.sighting_id)}
                  />
                ))}
              </motion.div>
            </AnimatePresence>
          )}
        </div>
      </aside>

      <EvidencePanel
        sighting={selectedSighting}
        sourceName={
          selectedSighting
            ? sourceNames.get(selectedSighting.source_id) || selectedSighting.source_id
            : ''
        }
        onClose={() => setSelected(null)}
        onTrace={(plate) => navigate(`/trace/${encodeURIComponent(plate)}`)}
      />
    </div>
  )
}
