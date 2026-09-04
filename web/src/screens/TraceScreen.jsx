import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { motion, useReducedMotion } from 'framer-motion'
import Empty from '../components/Empty'
import EvidencePanel from '../components/EvidencePanel'
import EvidenceStrip from '../components/EvidenceStrip'
import PlateString from '../components/PlateString'
import TimeScrubber from '../components/TimeScrubber'
import TrajectoryPath from '../components/TrajectoryPath'
import VehicleBadge from '../components/VehicleBadge'
import { getSighting, getTrajectory, searchPlates } from '../lib/api'
import { asPercent, calendarDay, clockTime, durationText } from '../lib/format'
import { useRoute } from '../lib/router'
import { indexAt, timeline } from '../lib/timeline'

// Trace: find a vehicle whose plate nobody knows exactly, then follow it.
//
// Two halves, and the split is the argument. On the left a search that always
// answers with a ranked list -- score, how it matched, how many sightings --
// because matching is fuzzy and one silent answer would assert a certainty the
// evidence does not support. On the right the trajectory of whichever candidate
// was chosen: the path, a scrubber over time, the crops, and the arithmetic.
//
// The URL carries the query, so /trace/MH15HY2237 survives a reload and "Trace
// this vehicle" from anywhere else in the app is just a link.

// Above this, an implied average speed is worth looking at twice. It is not an
// alert -- P5 raises those, from these same numbers -- and nothing here decides
// anything: it colours a figure that deserves a second look.
const IMPLAUSIBLE_KMH = 150

const MATCHED_VIA = {
  plate: 'matched the voted plate',
  raw: 'matched the raw OCR read',
  candidate: 'matched an alternative reading from the vote',
}

function plateFromPath(path) {
  const rest = path.replace(/^\/trace\/?/, '')
  if (!rest) return ''
  try {
    return decodeURIComponent(rest)
  } catch {
    return rest
  }
}

function CandidateRow({ result, active, onSelect }) {
  const reduced = useReducedMotion()
  return (
    <motion.button
      type="button"
      onClick={() => onSelect(result.plate_text)}
      initial={reduced ? false : { opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={reduced ? { duration: 0 } : { type: 'spring', stiffness: 460, damping: 34 }}
      aria-current={active ? 'true' : undefined}
      className={`w-full rounded-card p-3 text-left transition-colors duration-150 ${
        active ? 'bg-surface-2' : 'bg-surface-1 hover:bg-surface-2'
      }`}
    >
      <div className="flex items-start justify-between gap-2">
        <PlateString text={result.plate_text} conf={result.plate_conf} size="sm" />
        <span
          className="shrink-0 text-[13px] font-semibold tabular-nums text-plate-yellow"
          title="How closely this plate matches the search, on a confusion-weighted comparison"
        >
          {asPercent(result.score)}
        </span>
      </div>

      <div className="mt-1.5 text-[12px] text-ink-mid">
        {result.sighting_count} sighting{result.sighting_count === 1 ? '' : 's'}
        <span aria-hidden> · </span>
        {result.sources.length} source{result.sources.length === 1 ? '' : 's'}
        <span aria-hidden> · </span>
        <span className="tabular-nums">{clockTime(result.first_seen_ts)}</span>
      </div>
      <div className="mt-0.5 text-[11.5px] text-ink-low">
        {MATCHED_VIA[result.matched_via] || result.matched_via}
        {result.matched_text !== result.plate_text && (
          <span className="font-plate tracking-plate"> · {result.matched_text}</span>
        )}
      </div>
    </motion.button>
  )
}

function StopsTable({ stops, activeIndex, onSelect, onOpen }) {
  return (
    <table className="w-full text-[13px]">
      <thead>
        <tr className="text-left">
          {['Stop', 'Source', 'Time', 'Gap', 'Distance', 'Implied speed', 'Match'].map((head) => (
            <th key={head} className="label px-2 pb-2 font-semibold">
              {head}
            </th>
          ))}
          <th className="px-2 pb-2" />
        </tr>
      </thead>
      <tbody>
        {stops.map((stop, index) => {
          const active = index === activeIndex
          const fast = stop.speed_kmh != null && stop.speed_kmh > IMPLAUSIBLE_KMH
          return (
            <tr
              key={stop.sighting_id}
              onClick={() => onSelect(index)}
              className={`cursor-pointer align-middle transition-colors duration-150 ${
                active ? 'bg-surface-2' : 'hover:bg-surface-1'
              }`}
            >
              <td className="px-2 py-2 tabular-nums text-ink-low">{index + 1}</td>
              <td className="px-2 py-2">
                {/* The id as well as the name: two sources can carry the same
                    name -- the same clip added twice does exactly that -- and
                    then the name alone cannot tell two rows apart. */}
                <span className="text-ink-hi" title={stop.source_id}>
                  {stop.source_name}
                </span>
                {stop.lat == null && (
                  <span
                    className="ml-1.5 text-[11px] text-ink-low"
                    title="This source has no coordinates, so the stop cannot be drawn and its leg has no distance"
                  >
                    not placed
                  </span>
                )}
              </td>
              <td className="px-2 py-2 tabular-nums text-ink-mid" title={stop.first_seen_ts || ''}>
                {calendarDay(stop.first_seen_ts)} {clockTime(stop.first_seen_ts)}
              </td>
              <td className="px-2 py-2 tabular-nums text-ink-mid">
                {durationText(stop.gap_seconds) || '--'}
              </td>
              <td className="px-2 py-2 tabular-nums text-ink-mid">
                {stop.distance_km == null ? '--' : `${stop.distance_km.toFixed(2)} km`}
              </td>
              <td
                className={`px-2 py-2 tabular-nums ${fast ? 'text-plate-red' : 'text-ink-mid'}`}
                title={
                  stop.speed_kmh == null
                    ? 'No speed: one of the two sources has no coordinates, or the two sightings overlap in time'
                    : fast
                      ? 'Faster than a vehicle plausibly travels between these cameras. Check both plate reads.'
                      : 'Distance divided by the time from leaving the previous camera to arriving here'
                }
              >
                {stop.speed_kmh == null ? '--' : `${stop.speed_kmh.toFixed(0)} km/h`}
              </td>
              <td className="px-2 py-2">
                <span className="font-plate text-[12.5px] tracking-plate text-ink-mid">
                  {stop.plate_text}
                </span>
                <span className="ml-2 text-[11.5px] tabular-nums text-ink-low">
                  {asPercent(stop.score)}
                </span>
              </td>
              <td className="px-2 py-2 text-right">
                <button
                  type="button"
                  onClick={(event) => {
                    event.stopPropagation()
                    onOpen(stop)
                  }}
                  className="rounded-control px-2 py-1 text-[12px] text-ink-mid
                    transition-colors duration-150 hover:bg-surface-3 hover:text-ink-hi"
                >
                  Evidence
                </button>
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

export default function TraceScreen() {
  const { path, navigate } = useRoute()
  const urlPlate = plateFromPath(path)

  const [query, setQuery] = useState(urlPlate)
  const [search, setSearch] = useState(null)
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState(null)

  // A lowered floor belongs to the query it was lowered for. Keeping the plate
  // beside the value means a new search resets it in the same render rather
  // than in a second effect, which would fire the search twice.
  const [floor, setFloor] = useState({ plate: '', value: null })
  const activeFloor = floor.plate === urlPlate ? floor.value : null

  // Pressing Search on the plate already in the URL has to search again rather
  // than do nothing: navigate() refuses a no-op, so the button would be dead
  // exactly when someone is retrying after adding a source.
  const [attempt, setAttempt] = useState(0)

  const [selected, setSelected] = useState('')
  const [journey, setJourney] = useState(null)
  const [journeyError, setJourneyError] = useState(null)
  const [loadingJourney, setLoadingJourney] = useState(false)

  const [position, setPosition] = useState(1)
  const [playing, setPlaying] = useState(false)
  const [evidence, setEvidence] = useState(null)
  const [evidenceError, setEvidenceError] = useState(null)

  const inputRef = useRef(null)

  // The URL is the query. Typing runs a search only on submit -- a fuzzy search
  // scores every plate in the database, and firing one per keystroke spends
  // that on strings nobody has finished typing.
  useEffect(() => {
    let cancelled = false
    if (!urlPlate) {
      setSearch(null)
      setSelected('')
      setJourney(null)
      return undefined
    }
    setQuery(urlPlate)
    setSearching(true)
    setSearchError(null)
    searchPlates(urlPlate, 10, activeFloor)
      .then((answer) => {
        if (cancelled) return
        setSearch(answer)
        // Open one automatically only when the answer is not a choice: a single
        // candidate, or a candidate that is exactly what was asked for -- which
        // is how "Trace this vehicle" arrives here. Anything else stays a list,
        // because picking one silently is the mistake this screen exists to
        // avoid.
        const exact = answer.results.find((row) => row.plate_text === answer.normalized)
        const only = answer.results.length === 1 ? answer.results[0] : null
        setSelected((exact || only)?.plate_text || '')
      })
      .catch((error) => !cancelled && setSearchError(error.message))
      .finally(() => !cancelled && setSearching(false))
    return () => {
      cancelled = true
    }
  }, [urlPlate, activeFloor, attempt])

  useEffect(() => {
    let cancelled = false
    if (!selected) {
      setJourney(null)
      setJourneyError(null)
      return undefined
    }
    setLoadingJourney(true)
    setJourneyError(null)
    getTrajectory(selected)
      .then((answer) => {
        if (cancelled) return
        setJourney(answer)
        // Land on the whole path rather than at its start: the first thing to
        // see is the journey. Pressing play rewinds and draws it.
        setPosition(1)
        setPlaying(false)
      })
      .catch((error) => !cancelled && setJourneyError(error.message))
      .finally(() => !cancelled && setLoadingJourney(false))
    return () => {
      cancelled = true
    }
  }, [selected])

  const stops = useMemo(() => journey?.stops || [], [journey])
  const { positions } = useMemo(() => timeline(stops), [stops])
  const activeIndex = stops.length ? indexAt(positions, position) : 0

  const goToStop = useCallback(
    (index) => {
      setPlaying(false)
      setPosition(positions[index] ?? 0)
    },
    [positions],
  )

  const openEvidence = useCallback((stop) => {
    setEvidenceError(null)
    // The panel shows the track id and the alternative readings, which a
    // trajectory stop does not carry -- so it reads the row rather than the
    // trajectory contract widening to feed a panel.
    getSighting(stop.sighting_id)
      .then(setEvidence)
      .catch((error) => setEvidenceError(error.message))
  }, [])

  const submit = (event) => {
    event.preventDefault()
    const wanted = query.trim()
    if (!wanted) {
      inputRef.current?.focus()
      return
    }
    if (wanted === urlPlate) setAttempt((count) => count + 1)
    else navigate(`/trace/${encodeURIComponent(wanted)}`)
  }

  const results = search?.results || []
  const unplaced = stops.filter((stop) => stop.lat == null).length

  return (
    <div className="flex h-full min-h-0">
      <aside
        className="flex w-[25rem] shrink-0 flex-col"
        style={{ borderRight: '1px solid var(--hairline)' }}
        aria-label="Plate search"
      >
        <form onSubmit={submit} className="px-5 pb-4 pt-5">
          <label htmlFor="trace-query" className="label">
            Plate
          </label>
          <div className="mt-1.5 flex gap-2">
            <input
              id="trace-query"
              ref={inputRef}
              value={query}
              onChange={(event) => setQuery(event.target.value.toUpperCase())}
              placeholder="MH15HY2237"
              autoComplete="off"
              spellCheck="false"
              className="min-w-0 flex-1 rounded-control bg-surface-2 px-3 py-2 font-plate
                text-[16px] tracking-plate text-ink-hi placeholder:font-sans
                placeholder:tracking-normal placeholder:text-ink-low"
            />
            <button
              type="submit"
              className="shrink-0 rounded-control bg-plate-yellow px-3.5 py-2 text-[13px]
                font-semibold text-[#1a1400] transition-transform duration-150
                hover:brightness-105 active:scale-[.98]"
            >
              Search
            </button>
          </div>
          <p className="mt-2 text-[12px] text-ink-low">
            A misread plate is fine -- the search is fuzzy and answers with ranked
            candidates rather than with one plate. A partial one scores lower, because
            the comparison is over the whole registration.
          </p>
        </form>

        <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
          {searchError ? (
            <Empty title="The search failed." action={searchError} />
          ) : searching ? (
            <p className="px-4 py-8 text-center text-[13px] text-ink-low">Searching…</p>
          ) : !search ? (
            <Empty
              title="Search for a vehicle."
              action="Type a plate, or open a sighting anywhere in the app and press Trace this vehicle."
            />
          ) : results.length === 0 ? (
            <div>
              <Empty
                title={`Nothing matches ${search.normalized}.`}
                action={
                  search.searched === 0
                    ? 'No sighting has a plate read yet. Run a source in Sources, or read a file in Analyze.'
                    : `${search.searched} sighting${search.searched === 1 ? '' : 's'} with a plate were ` +
                      `compared and none scored above ${Math.round(search.min_score * 100)}%.`
                }
              />
              {/* The floor is not a wall. A short query scores low because the
                  comparison is over the longer string -- MH15HY against
                  MH15HY2237 is 67% however right it is -- so the nearest
                  refused candidate is named and can be reached. */}
              {search.closest && (
                <div className="mx-2 rounded-card bg-surface-1 p-3 text-center">
                  <p className="text-[13px] text-ink-mid">
                    The closest was{' '}
                    <span className="font-plate tracking-plate text-ink-hi">
                      {search.closest.plate_text}
                    </span>{' '}
                    at {asPercent(search.closest.score)}, over{' '}
                    {search.closest.sighting_count} sighting
                    {search.closest.sighting_count === 1 ? '' : 's'}.
                  </p>
                  <button
                    type="button"
                    onClick={() =>
                      setFloor({
                        plate: urlPlate,
                        value: Math.max(0.05, Math.floor(search.closest.score * 100) / 100),
                      })
                    }
                    className="mt-2 rounded-control bg-surface-3 px-3 py-1.5 text-[12.5px]
                      font-semibold text-ink-hi transition-colors duration-150 hover:bg-surface-2"
                  >
                    Search down to {asPercent(search.closest.score)}
                  </button>
                </div>
              )}
            </div>
          ) : (
            <>
              <div className="px-2 pb-2 text-[12px] text-ink-low">
                {results.length} candidate{results.length === 1 ? '' : 's'} over {search.searched}{' '}
                plated sighting{search.searched === 1 ? '' : 's'}
                {activeFloor != null && (
                  <span className="text-plate-yellow">
                    {' '}
                    · down to {asPercent(activeFloor)}
                  </span>
                )}
              </div>
              <div className="flex flex-col gap-1.5">
                {results.map((result) => (
                  <CandidateRow
                    key={result.plate_text}
                    result={result}
                    active={result.plate_text === selected}
                    onSelect={setSelected}
                  />
                ))}
              </div>
            </>
          )}
        </div>
      </aside>

      <section className="flex min-h-0 min-w-0 flex-1 flex-col">
        {!selected ? (
          <div className="grid flex-1 place-items-center px-6">
            <Empty
              title={results.length ? 'Choose a candidate.' : 'No vehicle selected.'}
              action={
                results.length
                  ? 'Every candidate carries its own score and sighting count. Pick the one you mean and its path opens here.'
                  : 'Search for a plate on the left. The vehicle you pick is drawn here, stop by stop.'
              }
            />
          </div>
        ) : journeyError ? (
          <div className="grid flex-1 place-items-center px-6">
            <Empty title="The trajectory could not load." action={journeyError} />
          </div>
        ) : loadingJourney && !journey ? (
          <div className="grid flex-1 place-items-center px-6">
            <p className="text-[13px] text-ink-low">Gathering sightings…</p>
          </div>
        ) : stops.length === 0 ? (
          <div className="grid flex-1 place-items-center px-6">
            <Empty
              title={`${selected} has no sightings any more.`}
              action="The rows behind this candidate are gone -- the source may have been deleted with its sightings. Search again."
            />
          </div>
        ) : (
          <>
            <div className="relative min-h-[16rem] flex-1">
              {journey.placed > 0 ? (
                <TrajectoryPath stops={stops} activeIndex={activeIndex} onSelectStop={goToStop} />
              ) : (
                <div className="grid h-full place-items-center px-6">
                  <Empty
                    title="This vehicle's sightings have no coordinates."
                    action={
                      `All ${stops.length} stop${stops.length === 1 ? '' : 's'} are at sources that ` +
                      'have not been placed on the map. Place them in Sources and the path draws itself.'
                    }
                  />
                </div>
              )}

              <div className="glass pointer-events-none absolute left-4 top-4 rounded-card px-4 py-3">
                <div className="flex items-baseline gap-2">
                  <span className="font-plate text-[20px] font-semibold tracking-plate text-ink-hi">
                    {journey.plate}
                  </span>
                  <span className="text-[12px] text-ink-mid">
                    {journey.matched} sighting{journey.matched === 1 ? '' : 's'} · {journey.sources}{' '}
                    source{journey.sources === 1 ? '' : 's'}
                  </span>
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-ink-low">
                  <span className="tabular-nums">
                    {durationText(journey.span_seconds) || 'seen once'}
                  </span>
                  {journey.distance_km != null && (
                    <span className="tabular-nums">{journey.distance_km.toFixed(2)} km</span>
                  )}
                  {unplaced > 0 && (
                    <span className="text-plate-yellow">
                      {unplaced} stop{unplaced === 1 ? '' : 's'} not on the map
                    </span>
                  )}
                  <VehicleBadge type={stops[activeIndex]?.vehicle_type} />
                </div>
              </div>
            </div>

            <div className="hairline-t shrink-0 px-5 py-3">
              <TimeScrubber
                stops={stops}
                value={position}
                onChange={setPosition}
                playing={playing}
                onPlayingChange={setPlaying}
                activeIndex={activeIndex}
              />
            </div>

            <div className="min-h-0 shrink-0 overflow-y-auto" style={{ maxHeight: '46%' }}>
              <div className="px-5 pb-2">
                <EvidenceStrip
                  stops={stops}
                  activeIndex={activeIndex}
                  onSelect={goToStop}
                  onOpen={openEvidence}
                />
              </div>

              <div className="px-3 pb-5">
                <StopsTable
                  stops={stops}
                  activeIndex={activeIndex}
                  onSelect={goToStop}
                  onOpen={openEvidence}
                />
                {evidenceError && (
                  <p className="px-2 pt-2 text-[12px] text-plate-red">{evidenceError}</p>
                )}
              </div>
            </div>
          </>
        )}
      </section>

      <EvidencePanel
        sighting={evidence}
        sourceName={
          evidence
            ? stops.find((stop) => stop.sighting_id === evidence.sighting_id)?.source_name ||
              evidence.source_id
            : ''
        }
        onClose={() => setEvidence(null)}
        onTrace={(plate) => {
          setEvidence(null)
          navigate(`/trace/${encodeURIComponent(plate)}`)
        }}
      />
    </div>
  )
}
