// The Insights window arithmetic, in one module so node can check it.
//
// Extracted rather than left inside the screen for the same reason
// timeline.js was: a Python reimplementation in the verification script would
// be checking a second copy of the rule, not the one the bundle ships.
//
// The one decision worth stating: **a preset window is relative to the newest
// sighting in the database, never to `Date.now()`.**
//
// Timestamps here are absolute by design -- a recorded source stamps its rows
// with when the footage was filmed, which can be any distance from now, and
// nothing downstream is allowed to tell a recorded source from a live one. So
// "last hour" measured against the wall clock returns an empty screen for
// every clip processed yesterday, while the same control on a live camera
// works. Measuring back from the newest row makes the two identical again: on
// a live feed the newest row IS about now, so the presets mean what they say,
// and on recorded footage they mean the last hour of the footage. The labels
// say "of data" so nobody has to infer which.

export const PRESETS = [
  { id: 'all', label: 'All', hint: 'Every sighting in the database' },
  { id: '1h', label: '1 hour', seconds: 3600 },
  { id: '6h', label: '6 hours', seconds: 6 * 3600 },
  { id: '24h', label: '24 hours', seconds: 24 * 3600 },
  { id: '7d', label: '7 days', seconds: 7 * 86400 },
]

// Milliseconds -> the stored format, so a window sent to the API is written the
// same way the rows are. app/analytics.py normalises anything ISO, but sending
// the shape it already stores keeps the comparison obvious at both ends.
export function toStamp(ms) {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return null
  return new Date(ms).toISOString().replace(/(\.\d{3})?Z$/, (m) => (m === 'Z' ? '.000Z' : m))
}

export function fromStamp(iso) {
  if (!iso) return null
  const ms = new Date(iso).getTime()
  return Number.isNaN(ms) ? null : ms
}

// The window a preset selects, given what the database actually holds.
// `all` is both ends null, which the API reads as no filter at all -- not as a
// range computed from the extent, so a worker writing while the screen is open
// widens the answer instead of being cut off at whatever the extent was when
// the page loaded.
export function windowFor(preset, extent) {
  if (preset === 'all' || !preset) return { from: null, to: null }
  const entry = PRESETS.find((item) => item.id === preset)
  if (!entry || !entry.seconds) return { from: null, to: null }

  const last = fromStamp(extent?.last)
  if (last === null) return { from: null, to: null }
  return { from: toStamp(last - entry.seconds * 1000), to: null }
}

// What the window covers, as a sentence. Never "no data" -- an empty window
// over a full database and an empty database are different situations and the
// screen has to say which one it is looking at.
export function describeWindow({ from, to }, extent) {
  if (!from && !to) {
    if (!extent?.first) return 'No sightings have been recorded yet'
    return `All ${extent.sightings} sighting${extent.sightings === 1 ? '' : 's'}`
  }
  const parts = []
  if (from) parts.push(`from ${shortStamp(from)}`)
  if (to) parts.push(`to ${shortStamp(to)}`)
  return parts.join(' ')
}

export function shortStamp(iso) {
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return String(iso)
  return at.toLocaleString([], {
    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
  })
}

// The bucket width the server chose, said in words. The chart's x-axis is
// meaningless without it: eleven bars can be eleven seconds or eleven days.
export function bucketLabel(seconds) {
  if (!seconds) return ''
  const table = [
    [86400, 'day'],
    [3600, 'hour'],
    [60, 'minute'],
    [1, 'second'],
  ]
  for (const [size, name] of table) {
    if (seconds >= size && seconds % size === 0) {
      const count = seconds / size
      return count === 1 ? `1 ${name}` : `${count} ${name}s`
    }
  }
  return `${seconds}s`
}

// Ticks for the bucket axis: at most `wanted`, always including the first and
// last bucket, and always landing on real buckets rather than on interpolated
// positions between them.
export function axisTicks(count, wanted = 6) {
  if (count <= 0) return []
  if (count <= wanted) return Array.from({ length: count }, (_, i) => i)
  const step = (count - 1) / (wanted - 1)
  const ticks = []
  for (let i = 0; i < wanted; i += 1) ticks.push(Math.round(i * step))
  return [...new Set(ticks)]
}

// A y-axis that ends on a round number at or above the tallest bar, so the top
// gridline is a number a person can read rather than the maximum itself.
export function niceMax(value) {
  if (!Number.isFinite(value) || value <= 0) return 1
  const magnitude = 10 ** Math.floor(Math.log10(value))
  for (const step of [1, 2, 2.5, 5, 10]) {
    const candidate = step * magnitude
    if (candidate >= value) return candidate
  }
  return 10 * magnitude
}

// A great-circle bearing, used to bend the two directions of one
// origin-destination pair onto opposite sides of the straight line. Without it
// A->B and B->A draw exactly on top of each other and the busier direction is
// invisible.
export function bendPath(from, to, bend = 0.16, steps = 24) {
  const [lat1, lon1] = from
  const [lat2, lon2] = to
  const mx = (lat1 + lat2) / 2
  const my = (lon1 + lon2) / 2
  // Perpendicular to the chord, in plain lat/lon. Over the distances between
  // two cameras in one city this is indistinguishable from doing it properly,
  // and the line is a schematic of volume, never a route.
  const dx = lat2 - lat1
  const dy = lon2 - lon1
  const cx = mx - dy * bend
  const cy = my + dx * bend

  const points = []
  for (let i = 0; i <= steps; i += 1) {
    const t = i / steps
    const u = 1 - t
    points.push([
      u * u * lat1 + 2 * u * t * cx + t * t * lat2,
      u * u * lon1 + 2 * u * t * cy + t * t * lon2,
    ])
  }
  return points
}
