import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from '../lib/router'
import { getInsights } from '../lib/api'
import { bucketLabel, windowFor } from '../lib/insights'
import { durationText } from '../lib/format'
import TimeWindow from '../components/TimeWindow'
import CountsChart from '../components/CountsChart'
import TypeBars from '../components/TypeBars'
import SourceRanking from '../components/SourceRanking'
import DensityMap from '../components/DensityMap'

// Insights. Five panels under one time filter.
//
// The filter is shared in the data, not only in the control: one request
// returns every panel, computed from one fetch of the sightings in the window,
// so two panels cannot answer for two slightly different slices while a worker
// is writing. That is why this screen makes one call and not five.
//
// Nothing here is invented. A panel with no data says what would put data in
// it, and the two panels that need coordinates say so plainly rather than
// rendering an empty map that looks like a map of nothing happening.

export default function InsightsScreen() {
  const [preset, setPreset] = useState('all')
  const [range, setRange] = useState({ from: null, to: null })
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [showHeat, setShowHeat] = useState(true)
  const [showFlows, setShowFlows] = useState(true)

  const load = useCallback((current) => {
    setLoading(true)
    return getInsights(current)
      .then((body) => {
        setData(body)
        setError(null)
      })
      .catch((cause) => setError(cause.message))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    load(range)
  }, [load, range])

  const choosePreset = useCallback(
    (id) => {
      setPreset(id)
      setRange(windowFor(id, data?.extent))
    },
    [data],
  )

  const chooseCustom = useCallback((from, to) => {
    setPreset('custom')
    setRange({ from, to })
  }, [])

  const totals = data?.totals
  const readRate = useMemo(() => {
    if (!totals || !totals.sightings) return null
    return Math.round((totals.plated / totals.sightings) * 100)
  }, [totals])

  if (error) {
    return (
      <div className="grid h-full place-items-center px-6">
        <div className="max-w-md text-center">
          <h1 className="text-[18px] font-semibold text-plate-red">
            Insights could not be loaded.
          </h1>
          <p className="mt-2 text-body text-ink-mid">{error}</p>
          <button
            type="button"
            onClick={() => load(range)}
            className="mt-4 rounded-control bg-surface-3 px-3.5 py-2 text-[13px] font-semibold text-ink-hi"
          >
            Try again
          </button>
        </div>
      </div>
    )
  }

  if (!data) {
    return (
      <div className="grid h-full place-items-center">
        <p className="text-[13px] text-ink-low">Reading the database...</p>
      </div>
    )
  }

  // Nothing has ever been recorded. Not an empty window -- an empty database,
  // which is a different situation and needs a different sentence.
  if (data.extent.sightings === 0) {
    return (
      <div className="grid h-full place-items-center px-6">
        <div className="max-w-md text-center">
          <h1 className="text-[20px] font-semibold">Nothing has been seen yet.</h1>
          <p className="mt-2 text-body text-ink-mid">
            Insights counts what the workers write. Add a camera or a recorded
            video and process it, and the counts, the type split and the density
            map all fill in from the same sightings.
          </p>
          <Link
            to="/sources"
            className="mt-4 inline-block rounded-control bg-plate-yellow px-3.5 py-2 text-[13px] font-semibold text-[#1a1400]"
          >
            Add a source
          </Link>
        </div>
      </div>
    )
  }

  const empty = totals.sightings === 0

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-[1400px] flex-col gap-5 px-5 py-5">
        {/* One filter, one row, above everything it drives. */}
        <section className="rounded-card bg-surface-1 px-4 py-3.5 shadow-lift">
          <TimeWindow
            preset={preset}
            window={range}
            extent={data.extent}
            onPreset={choosePreset}
            onCustom={chooseCustom}
          />
        </section>

        <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Stat
            value={totals.sightings}
            label="Vehicles"
            note={
              data.window.covered_seconds
                ? `over ${durationText(data.window.covered_seconds)}`
                : 'in this window'
            }
          />
          <Stat
            value={totals.plated}
            label="Plates read"
            note={readRate === null ? '' : `${readRate}% of vehicles`}
          />
          <Stat
            value={totals.sources_seen}
            label="Sources active"
            note={`of ${totals.sources_total} configured`}
          />
          <Stat
            value={data.flows.journeys}
            label="Vehicles that moved"
            note={`of ${data.flows.vehicles} identified`}
          />
        </section>

        {empty && (
          <p className="rounded-card bg-surface-1 px-4 py-3 text-[13px] text-ink-mid shadow-lift">
            This window holds no sightings. The database has{' '}
            <span className="text-ink-hi tabular-nums">{data.extent.sightings}</span>{' '}
            in total -- widen the window or pick <span className="text-ink-hi">All</span>.
          </p>
        )}

        <div className="grid gap-5 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
          {/* --------------------------------------------------------- map */}
          <Panel
            title="Where"
            subtitle="Density at each camera, and where vehicles went next"
            className="min-h-[520px]"
            action={
              <div className="flex items-center gap-1">
                <Toggle on={showHeat} onClick={() => setShowHeat((v) => !v)} label="Heatmap" />
                <Toggle on={showFlows} onClick={() => setShowFlows((v) => !v)} label="Flows" />
              </div>
            }
          >
            {data.heat.points.length === 0 ? (
              <div className="grid flex-1 place-items-center px-6 py-10 text-center">
                <div className="max-w-[36ch]">
                  <p className="text-[15px] font-semibold text-ink-hi">
                    No source in this window has been placed on the map.
                  </p>
                  <p className="mt-2 text-[13px] text-ink-mid">
                    {data.heat.unplaced_sightings > 0 ? (
                      <>
                        {data.heat.unplaced_sightings} sighting
                        {data.heat.unplaced_sightings === 1 ? '' : 's'} came from{' '}
                        {data.heat.unplaced_sources} source
                        {data.heat.unplaced_sources === 1 ? '' : 's'} with no coordinates.
                        Give each one a position and this fills in.
                      </>
                    ) : (
                      <>Place a source on the map and its traffic appears here.</>
                    )}
                  </p>
                  <Link
                    to="/sources"
                    className="mt-3 inline-block rounded-control bg-surface-3 px-3 py-1.5 text-[12.5px] font-semibold text-ink-hi"
                  >
                    Place sources
                  </Link>
                </div>
              </div>
            ) : (
              <>
                <div className="relative min-h-[420px] flex-1 overflow-hidden rounded-[10px]">
                  <DensityMap
                    heat={data.heat}
                    flows={data.flows}
                    showHeat={showHeat}
                    showFlows={showFlows}
                  />
                  {showHeat && data.heat.max > 0 && (
                    <div className="glass pointer-events-none absolute bottom-3 left-3 rounded-control px-3 py-2">
                      <div className="label mb-1">Vehicles per camera</div>
                      <div
                        className="h-2 w-32 rounded-full"
                        style={{
                          background:
                            'linear-gradient(90deg, rgba(122,90,0,.75), #f5c518 60%, #fff6cf)',
                        }}
                      />
                      <div className="mt-1 flex justify-between text-[10.5px] tabular-nums text-ink-low">
                        <span>0</span>
                        <span>{data.heat.max}</span>
                      </div>
                    </div>
                  )}
                </div>
                <FlowFootnote heat={data.heat} flows={data.flows} />
              </>
            )}
          </Panel>

          <div className="flex flex-col gap-5">
            {/* ----------------------------------------------------- time */}
            <Panel
              title="Traffic over time"
              subtitle={`One bar per ${bucketLabel(data.window.bucket_seconds)}, split by whether a plate was read`}
            >
              <CountsChart
                buckets={data.buckets}
                bucketSeconds={data.window.bucket_seconds}
              />
            </Panel>

            {/* ---------------------------------------------------- types */}
            <Panel title="Vehicle types" subtitle="What the detector called them">
              <TypeBars types={data.types} total={totals.sightings} />
            </Panel>
          </div>
        </div>

        {/* --------------------------------------------------------- sources */}
        <Panel
          title="Busiest sources"
          subtitle="Ranked by volume, or by rate -- a short clip and a long one are only comparable per hour"
        >
          <SourceRanking sources={data.sources} />
        </Panel>

        {/* --------------------------------------------------------- flows */}
        <Panel
          title="Origin and destination"
          subtitle={`One vehicle's consecutive stops at two different cameras, matched fuzzily at ${Math.round(data.flows.min_score * 100)}%`}
        >
          <FlowTable flows={data.flows} />
        </Panel>

        {loading && (
          <p className="text-center text-[11px] text-ink-low">Updating...</p>
        )}
      </div>
    </div>
  )
}

function Stat({ value, label, note }) {
  return (
    <div className="rounded-card bg-surface-1 px-4 py-3.5 shadow-lift">
      <div className="label">{label}</div>
      <div className="mt-1 text-count font-semibold tabular-nums text-ink-hi">
        {value}
      </div>
      {note && <div className="mt-0.5 text-[12px] text-ink-low">{note}</div>}
    </div>
  )
}

function Panel({ title, subtitle, action, children, className = '' }) {
  return (
    <section
      className={`flex flex-col rounded-card bg-surface-1 p-4 shadow-lift ${className}`}
    >
      <header className="mb-3 flex items-start justify-between gap-3">
        <div>
          <h2 className="text-[15px] font-semibold text-ink-hi">{title}</h2>
          {subtitle && <p className="mt-0.5 text-[12px] text-ink-low">{subtitle}</p>}
        </div>
        {action}
      </header>
      {children}
    </section>
  )
}

function Toggle({ on, onClick, label }) {
  return (
    <button
      type="button"
      aria-pressed={on}
      onClick={onClick}
      className={`rounded-control px-2.5 py-1 text-[12px] transition-colors duration-150 ${
        on
          ? 'bg-surface-3 font-semibold text-ink-hi'
          : 'text-ink-mid hover:bg-surface-2 hover:text-ink-hi'
      }`}
    >
      {label}
    </button>
  )
}

// What the map is not showing, said under the map rather than left out of it.
function FlowFootnote({ heat, flows }) {
  const notes = []
  if (heat.unplaced_sightings > 0) {
    notes.push(
      `${heat.unplaced_sightings} sighting${heat.unplaced_sightings === 1 ? '' : 's'} from ${heat.unplaced_sources} unplaced source${heat.unplaced_sources === 1 ? '' : 's'} are not on this map`,
    )
  }
  if (flows.undrawable > 0) {
    notes.push(
      `${flows.undrawable} flow${flows.undrawable === 1 ? '' : 's'} cannot be drawn until both ends are placed`,
    )
  }
  return (
    <div className="mt-2.5 text-[11.5px] leading-relaxed text-ink-low">
      <p>
        Density is measured at the cameras. Nothing records where a vehicle was
        between two of them, so the map shows what each camera saw, never a
        route.
      </p>
      {notes.length > 0 && (
        <p className="mt-1 text-plate-yellow">{notes.join('. ')}.</p>
      )}
    </div>
  )
}

function FlowTable({ flows }) {
  if (flows.links.length === 0) {
    return (
      <div className="py-8 text-center">
        <p className="text-[15px] font-semibold text-ink-hi">
          No vehicle was seen at two different cameras in this window.
        </p>
        <p className="mx-auto mt-2 max-w-[46ch] text-[13px] text-ink-mid">
          {flows.vehicles === 0
            ? 'No plate was read in this window, so no vehicle can be followed from one camera to another.'
            : `${flows.vehicles} vehicle${flows.vehicles === 1 ? ' was' : 's were'} identified, but each was only seen at one source.`}
        </p>
      </div>
    )
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[560px] text-[13px]">
        <thead>
          <tr className="text-left text-ink-low">
            <th className="pb-2 font-medium">From</th>
            <th className="pb-2 font-medium">To</th>
            <th className="pb-2 text-right font-medium">Vehicles</th>
            <th className="pb-2 text-right font-medium">Median gap</th>
            <th className="pb-2 text-right font-medium">Distance</th>
            <th className="pb-2 text-right font-medium">Median speed</th>
          </tr>
        </thead>
        <tbody>
          {flows.links.map((flow) => (
            <tr
              key={`${flow.from_source}->${flow.to_source}`}
              className="hairline-t text-ink-mid"
            >
              <td className="py-2 pr-3 text-ink-hi" title={flow.from_source}>
                {flow.from_name}
              </td>
              <td className="py-2 pr-3 text-ink-hi" title={flow.to_source}>
                {flow.to_name}
              </td>
              <td className="py-2 text-right tabular-nums text-ink-hi">
                {flow.count}
              </td>
              <td className="py-2 text-right tabular-nums">
                {durationText(flow.median_seconds) ?? '--'}
              </td>
              <td className="py-2 text-right tabular-nums">
                {flow.distance_km == null ? (
                  <span
                    className="text-plate-yellow"
                    title="One of these two sources has no coordinates"
                  >
                    not placed
                  </span>
                ) : (
                  `${flow.distance_km.toFixed(2)} km`
                )}
              </td>
              <td className="py-2 text-right tabular-nums">
                {flow.median_speed_kmh == null ? '--' : `${flow.median_speed_kmh} km/h`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
