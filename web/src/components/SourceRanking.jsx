import { useState } from 'react'
import StatusPill from './StatusPill'
import { durationText } from '../lib/format'

// Which sources see the most, ranked.
//
// Two rankings, because a raw count is not a density. A 20-second clip that saw
// 15 vehicles and a 200-second one that saw 66 are 2610 and 1188 vehicles an
// hour -- the shorter clip is the busier road, and the count ranking says the
// opposite. Both numbers are shown on every row and the sort is a control, so
// the screen never quietly picks one and calls it "density".
//
// A source that saw nothing in the window stays in the list at zero. A camera
// with no traffic is a result, and dropping it would make the list look like a
// list of all the cameras.

const SORTS = [
  { id: 'count', label: 'Total', hint: 'Vehicles seen in this window' },
  { id: 'per_hour', label: 'Per hour', hint: 'Vehicles per hour of the time that source was producing' },
]

export default function SourceRanking({ sources }) {
  const [sort, setSort] = useState('count')

  if (!sources.length) {
    return (
      <p className="py-8 text-center text-[13px] text-ink-mid">
        No sources yet. Add one on the Sources screen and it will appear here as
        soon as it produces a sighting.
      </p>
    )
  }

  const ranked = [...sources].sort((a, b) => {
    if (sort === 'per_hour') {
      // A source whose sightings share one instant -- a still image -- has no
      // rate at all. It sorts last rather than being given a zero it did not
      // earn.
      const av = a.per_hour ?? -1
      const bv = b.per_hour ?? -1
      if (bv !== av) return bv - av
    }
    return b.count - a.count || a.name.localeCompare(b.name)
  })

  const top = Math.max(
    1,
    ...ranked.map((entry) => (sort === 'per_hour' ? entry.per_hour || 0 : entry.count)),
  )
  const quiet = ranked.filter((entry) => entry.count === 0).length

  return (
    <div>
      <div className="mb-3 flex items-center gap-1">
        {SORTS.map((option) => (
          <button
            key={option.id}
            type="button"
            title={option.hint}
            aria-pressed={sort === option.id}
            onClick={() => setSort(option.id)}
            className={`rounded-control px-2.5 py-1 text-[12px] transition-colors duration-150 ${
              sort === option.id
                ? 'bg-surface-3 font-semibold text-ink-hi'
                : 'text-ink-mid hover:bg-surface-2 hover:text-ink-hi'
            }`}
          >
            {option.label}
          </button>
        ))}
      </div>

      <ul className="flex flex-col gap-3">
        {ranked.map((entry) => {
          const value = sort === 'per_hour' ? entry.per_hour : entry.count
          return (
            <li key={entry.source_id}>
              <div className="flex items-baseline justify-between gap-3">
                <span
                  className="truncate text-[13.5px] text-ink-hi"
                  title={entry.source_id}
                >
                  {entry.name}
                  {entry.lat == null && entry.count > 0 && (
                    <span
                      className="ml-2 text-[11px] text-plate-yellow"
                      title="This source has no coordinates, so it cannot appear on the map or in a flow line"
                    >
                      not placed
                    </span>
                  )}
                </span>
                <span className="shrink-0 tabular-nums text-[13px] text-ink-hi">
                  {value == null ? (
                    <span className="text-[11px] text-ink-low">no rate</span>
                  ) : (
                    <>
                      {sort === 'per_hour' ? Math.round(value) : value}
                      <span className="ml-1 text-[11px] text-ink-low">
                        {sort === 'per_hour' ? '/h' : ''}
                      </span>
                    </>
                  )}
                </span>
              </div>

              <div className="mt-1.5 h-2 w-full overflow-hidden rounded-full bg-surface-2">
                <div
                  className="h-full rounded-full bg-plate-yellow transition-[width] duration-200"
                  style={{ width: `${Math.max(0, ((value || 0) / top) * 100)}%` }}
                />
              </div>

              <div className="mt-1 flex items-center gap-2.5 text-[11px] text-ink-low">
                <StatusPill status={entry.status} />
                <span className="tabular-nums">
                  {entry.plated} plate{entry.plated === 1 ? '' : 's'} read
                </span>
                {entry.span_seconds ? (
                  <span className="tabular-nums">
                    over {durationText(entry.span_seconds)}
                  </span>
                ) : null}
                {sort === 'count' && entry.per_hour != null && (
                  <span className="tabular-nums">
                    {Math.round(entry.per_hour)}/h
                  </span>
                )}
              </div>
            </li>
          )
        })}
      </ul>

      {quiet > 0 && (
        <p className="mt-3 text-[11px] text-ink-low">
          {quiet} source{quiet === 1 ? '' : 's'} saw nothing in this window.
        </p>
      )}
    </div>
  )
}
