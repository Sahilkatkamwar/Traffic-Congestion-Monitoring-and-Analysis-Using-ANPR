import { useMemo, useState } from 'react'
import { axisTicks, bucketLabel, niceMax } from '../lib/insights'
import { clockTime, calendarDay } from '../lib/format'

// Vehicles per interval, stacked by whether a plate was read.
//
// Bars, not an area: a bucket is a count of vehicles that arrived inside one
// interval, and an area line drawn through those counts asserts values between
// the buckets that nobody measured.
//
// Two colours, and both of them are the colours PlateString already uses. A
// read plate renders on the plate-white ground; NO READ renders on slate. So
// the chart is not choosing a palette, it is showing the same two states the
// evidence cards show, in the same colours -- which is what stops the colour
// being decoration.
//
// Empty buckets are drawn as empty. A gap in traffic is a fact about the
// footage, and closing the gap up would draw a busier road than the one filmed.

const READ = 'var(--viz-read)'
const UNREAD = 'var(--viz-unread)'

const PAD = { top: 12, right: 8, bottom: 26, left: 34 }
const HEIGHT = 190

export default function CountsChart({ buckets, bucketSeconds }) {
  const [hover, setHover] = useState(null)
  const [table, setTable] = useState(false)

  const top = useMemo(
    () => niceMax(Math.max(0, ...buckets.map((b) => b.total))),
    [buckets],
  )

  if (buckets.length === 0) {
    return (
      <p className="px-1 py-8 text-center text-[13px] text-ink-mid">
        No vehicles were seen in this window. Widen it, or process a source on
        the Sources screen.
      </p>
    )
  }

  const width = 640
  const plotW = width - PAD.left - PAD.right
  const plotH = HEIGHT - PAD.top - PAD.bottom
  // A 2px surface gap between neighbouring bars, and never a bar under 1px --
  // ninety buckets on a narrow card would otherwise vanish entirely.
  const slot = plotW / buckets.length
  const barW = Math.max(1, slot - 2)
  const y = (value) => PAD.top + plotH - (value / top) * plotH

  const gridlines = [0, 0.5, 1].map((fraction) => Math.round(top * fraction))
  const ticks = axisTicks(buckets.length)
  const active = hover === null ? null : buckets[hover]

  return (
    <div>
      <div className="relative">
        <svg
          viewBox={`0 0 ${width} ${HEIGHT}`}
          className="w-full"
          style={{ height: HEIGHT }}
          role="img"
          aria-label={`Vehicles per ${bucketLabel(bucketSeconds)}, ${buckets.length} intervals, peak ${Math.max(0, ...buckets.map((b) => b.total))}`}
          onMouseLeave={() => setHover(null)}
        >
          {/* Recessive grid: the data is the ink, the scale is not. */}
          {gridlines.map((value) => (
            <g key={value}>
              <line
                x1={PAD.left} x2={width - PAD.right}
                y1={y(value)} y2={y(value)}
                stroke="var(--hairline)" strokeWidth="1"
              />
              <text
                x={PAD.left - 6} y={y(value) + 3.5}
                textAnchor="end" fontSize="10" fill="var(--ink-low)"
                className="tabular-nums"
              >
                {value}
              </text>
            </g>
          ))}

          {buckets.map((bucket, index) => {
            const x = PAD.left + index * slot + (slot - barW) / 2
            const readH = (bucket.plated / top) * plotH
            const unreadH = (bucket.unread / top) * plotH
            const isHover = index === hover
            return (
              <g key={bucket.start} opacity={hover === null || isHover ? 1 : 0.45}>
                {bucket.unread > 0 && (
                  <rect
                    x={x} width={barW}
                    y={y(bucket.total)} height={Math.max(unreadH, 0.8)}
                    fill={UNREAD} rx={barW > 5 ? 2 : 0}
                  />
                )}
                {bucket.plated > 0 && (
                  <rect
                    x={x} width={barW}
                    y={y(bucket.plated)} height={Math.max(readH, 0.8)}
                    fill={READ}
                    rx={barW > 5 ? 2 : 0}
                  />
                )}
              </g>
            )
          })}

          {/* Hit targets, wider than the marks, so a 1px bar is still reachable. */}
          {buckets.map((bucket, index) => (
            <rect
              key={`hit-${bucket.start}`}
              x={PAD.left + index * slot} y={PAD.top}
              width={slot} height={plotH}
              fill="transparent"
              onMouseEnter={() => setHover(index)}
            />
          ))}

          <line
            x1={PAD.left} x2={width - PAD.right}
            y1={PAD.top + plotH} y2={PAD.top + plotH}
            stroke="var(--hairline)"
          />

          {ticks.map((index) => (
            <text
              key={`tick-${index}`}
              x={PAD.left + index * slot + slot / 2}
              y={HEIGHT - 8}
              textAnchor="middle" fontSize="10" fill="var(--ink-low)"
            >
              {clockTime(buckets[index].start)}
            </text>
          ))}
        </svg>

        {active && (
          <div
            className="pointer-events-none absolute top-1 rounded-control bg-surface-3 px-2.5 py-1.5 text-[12px] shadow-lift"
            style={{
              left: `${((PAD.left + hover * slot + slot / 2) / width) * 100}%`,
              transform: 'translateX(-50%)',
            }}
          >
            <div className="text-ink-low">
              {calendarDay(active.start)} {clockTime(active.start)} · {bucketLabel(bucketSeconds)}
            </div>
            <div className="mt-0.5 tabular-nums text-ink-hi">
              {active.total} vehicle{active.total === 1 ? '' : 's'}
              {active.plated > 0 && <span className="text-ink-mid"> · {active.plated} read</span>}
            </div>
          </div>
        )}
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1">
        <Key colour={READ} label="Plate read" />
        <Key colour={UNREAD} label="No read" />
        <span className="text-[11px] text-ink-low">
          one bar = {bucketLabel(bucketSeconds)}
        </span>
        <button
          type="button"
          onClick={() => setTable((on) => !on)}
          className="ml-auto rounded-control px-2 py-1 text-[11px] text-ink-mid hover:bg-surface-2 hover:text-ink-hi"
          aria-expanded={table}
        >
          {table ? 'Hide numbers' : 'Show numbers'}
        </button>
      </div>

      {/* The same data as a table. A chart that can only be read by eye cannot
          be read by everyone, and this is also how the numbers get copied. */}
      {table && (
        <div className="mt-2 max-h-56 overflow-y-auto">
          <table className="w-full text-[12px] tabular-nums">
            <thead className="sticky top-0 bg-surface-1 text-left">
              <tr className="text-ink-low">
                <th className="py-1 font-medium">Interval</th>
                <th className="py-1 text-right font-medium">Vehicles</th>
                <th className="py-1 text-right font-medium">Read</th>
              </tr>
            </thead>
            <tbody>
              {buckets.filter((b) => b.total > 0).map((bucket) => (
                <tr key={bucket.start} className="text-ink-mid">
                  <td className="py-0.5">
                    {calendarDay(bucket.start)} {clockTime(bucket.start)}
                  </td>
                  <td className="py-0.5 text-right text-ink-hi">{bucket.total}</td>
                  <td className="py-0.5 text-right">{bucket.plated}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function Key({ colour, label }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-[11px] text-ink-mid">
      <span
        className="inline-block h-2.5 w-2.5 rounded-[3px]"
        style={{ background: colour }}
      />
      {label}
    </span>
  )
}
