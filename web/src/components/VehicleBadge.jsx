// Colour carries data. Yellow is the commercial ground on an Indian plate, so a
// yellow badge means a commercial class of vehicle -- auto, bus, truck. Private
// classes take the white ground.
//
// This is inferred from vehicle_type, which is what the classifier gives us. It
// is not a plate colour that was observed: the schema has no such column and
// inventing one would make the badge a decoration that looks like a fact.

const COMMERCIAL = new Set(['auto', 'bus', 'truck'])

const LABELS = {
  auto: 'Auto',
  car: 'Car',
  motorcycle: 'Motorcycle',
  bus: 'Bus',
  truck: 'Truck',
  unknown: 'Unknown',
}

export default function VehicleBadge({ type }) {
  const key = (type || 'unknown').toLowerCase()
  const label = LABELS[key] || type

  if (key === 'unknown' || !type) {
    return (
      <span
        className="rounded-full px-2 py-[2px] text-[11px] font-semibold text-ink-low bg-surface-3"
        title="The detector found a vehicle but the type is not established"
      >
        Unknown
      </span>
    )
  }

  const commercial = COMMERCIAL.has(key)
  return (
    <span
      className={`rounded-full px-2 py-[2px] text-[11px] font-semibold ${
        commercial ? 'bg-plate-yellow text-[#1a1400]' : 'bg-plate-white/90 text-[#12161c]'
      }`}
      title={commercial ? 'Commercial class -- commercial plates are yellow' : 'Private class'}
    >
      {label}
    </span>
  )
}
