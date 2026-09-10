import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import MapCanvas from '../components/MapCanvas'
import SightingCard from '../components/SightingCard'
import EvidencePanel from '../components/EvidencePanel'
import FollowStrip from '../components/FollowStrip'
import BoundaryPanel from '../components/BoundaryPanel'
import LiveCameras from '../components/LiveCameras'
import Empty from '../components/Empty'
import { getAlerts, getSightingByTrack, getSightings, getSources } from '../lib/api'
import { openLiveFeed } from '../lib/socket'
import { Link, useRoute } from '../lib/router'

// Full-bleed map with the feed floating over it. The panel is the only blurred
// surface on the screen, which is what makes it read as floating rather than
// as a column the map happens to sit beside.

const FEED_LIMIT = 80
// The strip is a glance, not the Alerts screen. Two are shown and the rest are
// counted, so a burst of alerts cannot push the live feed off the panel.
const ALERT_LIMIT = 20
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
  // The row itself, not its id: a marker click opens a sighting that may be
  // older than the eighty this feed holds, and an id would have nothing to
  // look up.
  const [selected, setSelected] = useState(null)
  const [activeSourceIds, setActiveSourceIds] = useState(() => new Set())
  const [newIds, setNewIds] = useState(() => new Set())
  const [notice, setNotice] = useState(null)
  // P9. What this browser is following, keyed by the server's follow id. The
  // server holds the same set on the connection; this is the drawing of it.
  const [follows, setFollows] = useState([])
  const [here, setHere] = useState(null)
  // P10. The administrative outlines, and what the map last said about them.
  // Off when the screen opens: it costs a call to a public service and it is
  // context rather than the job, so it is asked for rather than assumed.
  const [boundaries, setBoundaries] = useState(false)
  const [boundaryStatus, setBoundaryStatus] = useState(null)

  const pulseTimers = useRef(new Map())
  const feedRef = useRef(null)
  const noticeTimer = useRef(null)

  const say = useCallback((message) => {
    setNotice(message)
    clearTimeout(noticeTimer.current)
    noticeTimer.current = setTimeout(() => setNotice(null), 6000)
  }, [])

  const load = useCallback(async () => {
    try {
      const [sourceRows, sightingRows, alertRows] = await Promise.all([
        getSources(),
        getSightings(FEED_LIMIT),
        getAlerts(ALERT_LIMIT),
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

  // P9. Browser geolocation, asked for once when the screen opens.
  //
  // A map convenience and nothing else. It never reaches a timestamp, a
  // sighting, or a camera's placement -- CLAUDE.md's timestamp rule is settled
  // inside the worker and this is a browser telling the map where its user is
  // standing. Refused, unavailable or insecure: nothing is shown, nothing is
  // blocked, and the screen is exactly what it was.
  useEffect(() => {
    if (!('geolocation' in navigator)) return undefined
    let cancelled = false
    navigator.geolocation.getCurrentPosition(
      (position) => {
        if (cancelled) return
        setHere({
          lat: position.coords.latitude,
          lon: position.coords.longitude,
          accuracy: position.coords.accuracy,
        })
      },
      () => {
        // Denied, timed out, or served over plain http from another machine.
        // All three mean the same thing here: no marker.
      },
      { enableHighAccuracy: false, timeout: 10000, maximumAge: 300000 },
    )
    return () => {
      cancelled = true
    }
  }, [])

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
        if (status === 'offline') {
          // P9. The follow set lives on the connection and the server clears
          // it when the connection ends, so a dropped socket ends every follow
          // -- and the screen says so rather than showing a watch that is not
          // being kept.
          setFollows((current) =>
            current.map((entry) =>
              entry.status === 'live'
                ? { ...entry, status: 'ended', reason: 'disconnected' }
                : entry,
            ),
          )
        }
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
        } else if (event.type === 'alert') {
          // P5. The writer publishes an alert straight after the sighting that
          // raised it, so the strip fills within a second rather than on the
          // next load. Newest first, and the same row is never added twice --
          // a reconnect reloads and could otherwise duplicate what is here.
          const row = event.alert
          setAlerts((current) => {
            const without = current.filter((a) => a.alert_id !== row.alert_id)
            return [row, ...without].slice(0, ALERT_LIMIT)
          })
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
        } else if (event.type === 'follow_started') {
          // The session as the server made it. The first stop of the trail is
          // the sighting it was started from.
          setFollows((current) => [
            {
              ...event.follow,
              status: 'live',
              reason: null,
              stops: [
                {
                  sighting_id: event.follow.sighting_id,
                  source_id: event.follow.source_id,
                  ts: event.follow.started_ts,
                  plate_text: event.follow.plate_text,
                  score: 1,
                  matched_via: 'plate',
                },
              ],
            },
            ...current.filter((entry) => entry.follow_id !== event.follow.follow_id),
          ])
        } else if (event.type === 'follow_update') {
          const row = event.sighting
          markActive(row.source_id)
          setFollows((current) =>
            current.map((entry) =>
              entry.follow_id !== event.follow_id
                ? entry
                : {
                    ...entry,
                    matches: event.follow.matches,
                    status: 'live',
                    stops: entry.stops.some((s) => s.sighting_id === row.sighting_id)
                      ? entry.stops
                      : [
                          ...entry.stops,
                          {
                            sighting_id: row.sighting_id,
                            source_id: row.source_id,
                            ts: row.first_seen_ts,
                            plate_text: row.plate_text,
                            score: event.score,
                            matched_via: event.matched_via,
                          },
                        ],
                  },
            ),
          )
        } else if (event.type === 'follow_ended') {
          setFollows((current) =>
            current.map((entry) =>
              entry.follow_id === event.follow_id
                ? { ...entry, status: 'ended', reason: event.reason }
                : entry,
            ),
          )
        } else if (event.type === 'follow_error') {
          say(event.detail)
        }
      },
    })
    feedRef.current = close
    return () => {
      close()
      feedRef.current = null
      for (const timer of pulseTimers.current.values()) clearTimeout(timer)
      pulseTimers.current.clear()
      clearTimeout(noticeTimer.current)
    }
  }, [load, markActive, say])

  const sourceNames = useMemo(
    () => new Map(sources.map((s) => [s.source_id, s.name])),
    [sources],
  )
  const sourcePlaces = useMemo(
    () =>
      new Map(
        sources
          .filter((s) => s.lat != null && s.lon != null)
          .map((s) => [s.source_id, [s.lat, s.lon]]),
      ),
    [sources],
  )
  const placedCount = sourcePlaces.size
  const runningCount = useMemo(
    () => sources.filter((s) => s.status === 'running').length,
    [sources],
  )
  const erroredSources = useMemo(
    () => sources.filter((s) => s.status === 'error'),
    [sources],
  )

  // P9. Follow, started and stopped over the same socket the feed arrives on.
  const startFollow = useCallback(
    (sighting) => {
      const sent = feedRef.current?.send?.({
        type: 'follow_start',
        sighting_id: sighting.sighting_id,
      })
      if (!sent) {
        say('The live feed is reconnecting, so nothing can be followed yet. Try again in a moment.')
      }
    },
    [say],
  )

  const stopFollow = useCallback(
    (followId) => {
      const sent = feedRef.current?.send?.({ type: 'follow_stop', follow_id: followId })
      if (!sent) {
        // The connection is gone, which already ended the session server-side.
        setFollows((current) =>
          current.map((entry) =>
            entry.follow_id === followId
              ? { ...entry, status: 'ended', reason: 'disconnected' }
              : entry,
          ),
        )
      }
    },
    [],
  )

  const dismissFollow = useCallback((followId) => {
    setFollows((current) => current.filter((entry) => entry.follow_id !== followId))
  }, [])

  const followingIdsBySighting = useMemo(
    () =>
      new Map(
        follows
          .filter((entry) => entry.status === 'live')
          .map((entry) => [entry.sighting_id, entry.follow_id]),
      ),
    [follows],
  )

  const trails = useMemo(
    () =>
      follows
        .filter((entry) => entry.status === 'live')
        .map((entry) => ({
          follow_id: entry.follow_id,
          points: entry.stops
            .map((stop) => sourcePlaces.get(stop.source_id))
            .filter(Boolean),
        })),
    [follows, sourcePlaces],
  )

  // A marker is a camera, and what a camera has is the vehicles it has seen.
  // Clicking it opens the newest one in the same evidence panel the feed
  // opens -- read from the server rather than from the feed, which only holds
  // the last eighty rows across every source.
  const openLatestFor = useCallback(
    async (sourceId) => {
      const name = sourceNames.get(sourceId) || sourceId
      try {
        const rows = await getSightings(1, { sourceId })
        if (rows.length === 0) {
          say(`${name} has not seen a vehicle yet. Its sightings open here as they happen.`)
          return
        }
        setSelected(rows[0])
      } catch (error) {
        say(error.message)
      }
    },
    [say, sourceNames],
  )

  // A box on the live feed is a vehicle the worker is tracking right now. Its
  // row is written when the track ends, so a vehicle still in frame honestly
  // has nothing to open yet and the panel says that rather than showing an
  // empty evidence sheet.
  const openBox = useCallback(
    async (source, box) => {
      try {
        const row = await getSightingByTrack(source.source_id, box.track_id)
        if (row) setSelected(row)
        else
          say(
            `${box.vehicle_type} #${box.track_id} is still in frame at ${source.name}. ` +
              'Its evidence is written when it leaves.',
          )
      } catch (error) {
        say(error.message)
      }
    },
    [say],
  )

  const status = CONNECTION[connection] || CONNECTION.connecting

  return (
    <div className="relative h-full w-full">
      <MapCanvas
        sources={sources}
        activeSourceIds={activeSourceIds}
        onSelectSource={openLatestFor}
        here={here}
        trails={trails}
        boundaries={boundaries}
        onBoundaries={setBoundaryStatus}
      />

      {/* Under the zoom control, opposite the feed: this is a setting for the
          map behind the screen, not part of the screen's own work. */}
      <div className="absolute right-4 top-[6.5rem] z-[600]">
        <BoundaryPanel
          on={boundaries}
          onToggle={(next) => {
            setBoundaries(next)
            if (!next) setBoundaryStatus(null)
          }}
          status={boundaryStatus}
        />
      </div>

      {/* The running camera itself, bottom right, clear of the attribution.
          The map says where a camera is; this says what it is looking at. */}
      <LiveCameras sources={sources} onOpenBox={openBox} />

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

        {notice && (
          <div className="mt-3 px-4">
            <p className="rounded-control bg-surface-2 px-3 py-2 text-[12.5px] text-ink-mid" role="status">
              {notice}
            </p>
          </div>
        )}

        {/* P9. What is being followed right now, above the feed, because a
            follow is a live thing and the feed is a list of things that have
            already happened. */}
        <FollowStrip
          follows={follows}
          sourceNames={sourceNames}
          onStop={stopFollow}
          onDismiss={dismissFollow}
          onTrace={(plate) => navigate(`/trace/${encodeURIComponent(plate)}`)}
        />

        {/* Alert strip. The newest two, with the rest counted -- the whole list
            is the Alerts screen's job and a strip that grew without bound would
            push the live feed out of the panel. Colour is severity: the
            government-plate red for critical, the commercial yellow for
            everything else, exactly as the Alerts screen spends them. */}
        {alerts.length > 0 && (
          <div className="mt-3 px-4">
            {alerts.slice(0, 2).map((alert) => (
              <Link
                key={alert.alert_id}
                to="/alerts"
                className={`mb-1.5 block rounded-control px-3 py-2 text-[13px] text-ink-hi ${
                  alert.severity === 'critical' ? 'bg-plate-red/20' : 'bg-plate-yellow/15'
                }`}
                title="Open the Alerts screen"
              >
                <span className="font-plate tracking-plate font-semibold">
                  {alert.plate_text}
                </span>
                <span className="ml-2 text-ink-mid">{alert.detail}</span>
              </Link>
            ))}
            {alerts.length > 2 && (
              <Link
                to="/alerts"
                className="mb-1.5 block px-1 text-[12px] text-ink-mid hover:text-ink-hi"
              >
                {alerts.length - 2} more alert{alerts.length - 2 === 1 ? '' : 's'} →
              </Link>
            )}
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
                  : runningCount > 0
                  ? `${runningCount} source${runningCount === 1 ? ' is' : 's are'} running. The first vehicle they see appears here — watch the camera panel to see what they are looking at.`
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
                    onOpen={(row) => setSelected(row)}
                  />
                ))}
              </motion.div>
            </AnimatePresence>
          )}
        </div>
      </aside>

      <EvidencePanel
        sighting={selected}
        sourceName={
          selected ? sourceNames.get(selected.source_id) || selected.source_id : ''
        }
        onClose={() => setSelected(null)}
        onTrace={(plate) => navigate(`/trace/${encodeURIComponent(plate)}`)}
        following={selected ? followingIdsBySighting.has(selected.sighting_id) : false}
        onFollow={startFollow}
        onUnfollow={(row) => stopFollow(followingIdsBySighting.get(row.sighting_id))}
      />
    </div>
  )
}
