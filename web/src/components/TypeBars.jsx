import VehicleBadge from './VehicleBadge'

// Vehicle type distribution.
//
// A ranked bar, not a donut, and the reason is the palette. Five types would
// need five identities carried by colour, and this product has no five-hue
// categorical palette to give them -- its colours come from Indian plate
// grounds and each one already means something. Inventing five hues here would
// be exactly the decoration CLAUDE.md forbids.
//
// So colour carries the one thing it already means: yellow is the commercial
// ground, white the private one, and the split is the same one VehicleBadge
// makes beside every sighting. Which of the five types a bar is, is carried by
// the label on it -- identity is never colour alone.
//
// Validated with the dataviz palette checker against the card surface
// (#161d26): the three fills separate at CVD delta-E 15.6 or better on every
// simulated deficiency and all three clear 3:1 contrast.

const COMMERCIAL = new Set(['auto', 'bus', 'truck'])

function fillFor(type) {
  if (type === 'unknown') return 'var(--viz-unread)'
  return COMMERCIAL.has(type) ? 'var(--plate-yellow)' : 'var(--viz-read)'
}

export default function TypeBars({ types, total }) {
  if (!types.length) {
    return (
      <p className="py-8 text-center text-[13px] text-ink-mid">
        No vehicles in this window, so there is nothing to break down.
      </p>
    )
  }

  const top = Math.max(...types.map((entry) => entry.count))
  const commercial = types
    .filter((entry) => COMMERCIAL.has(entry.vehicle_type))
    .reduce((sum, entry) => sum + entry.count, 0)

  return (
    <div>
      <ul className="flex flex-col gap-2.5">
        {types.map((entry) => (
          <li key={entry.vehicle_type}>
            <div className="flex items-baseline justify-between gap-3">
              <VehicleBadge type={entry.vehicle_type} />
              <span className="tabular-nums text-[13px] text-ink-hi">
                {entry.count}
                <span className="ml-1.5 text-[11px] text-ink-low">
                  {Math.round(entry.share * 100)}%
                </span>
              </span>
            </div>
            <div className="mt-1.5 h-2 w-full overflow-hidden rounded-full bg-surface-2">
              <div
                className="h-full rounded-full transition-[width] duration-200"
                style={{
                  width: `${(entry.count / top) * 100}%`,
                  background: fillFor(entry.vehicle_type),
                }}
              />
            </div>
          </li>
        ))}
      </ul>

      <p className="mt-3.5 text-[12px] text-ink-mid">
        <span className="text-ink-hi tabular-nums">{commercial}</span> of{' '}
        <span className="tabular-nums">{total}</span> are commercial classes --
        auto, bus and truck, which is what the yellow says.
      </p>
      {/* Said once, here, rather than left for the reader to infer: the type
          comes from the detector's own COCO label, and it has no `auto`. */}
      <p className="mt-1 text-[11px] text-ink-low">
        Types come from the detector. It has no autorickshaw class, so autos are
        counted as cars.
      </p>
    </div>
  )
}
