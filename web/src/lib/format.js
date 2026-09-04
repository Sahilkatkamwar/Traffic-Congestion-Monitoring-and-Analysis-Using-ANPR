// Absolute timestamps arrive as ISO-8601 UTC text from the worker. They are
// absolute precisely so a recorded source and a live one are indistinguishable
// here, and nothing in the UI may treat them differently.

export function clockTime(iso) {
  if (!iso) return '--'
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return '--'
  return at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export function calendarDay(iso) {
  if (!iso) return ''
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return ''
  return at.toLocaleDateString([], { day: '2-digit', month: 'short' })
}

export function sinceNow(iso) {
  if (!iso) return ''
  const seconds = Math.round((Date.now() - new Date(iso).getTime()) / 1000)
  if (!Number.isFinite(seconds)) return ''
  // A recorded source carries the timestamp of when it was filmed, which can be
  // any distance from now. Past and future are both said plainly.
  const ago = Math.abs(seconds)
  const stamp =
    ago < 60 ? `${ago}s` :
    ago < 3600 ? `${Math.round(ago / 60)}m` :
    ago < 86400 ? `${Math.round(ago / 3600)}h` :
    `${Math.round(ago / 86400)}d`
  return seconds >= 0 ? `${stamp} ago` : `in ${stamp}`
}

export const asPercent = (value) =>
  value === null || value === undefined ? null : `${Math.round(value * 100)}%`

export function parseCandidates(raw) {
  // plate_candidates is a json text column and may be null.
  if (!raw) return []
  try {
    const parsed = typeof raw === 'string' ? JSON.parse(raw) : raw
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

// A leg's elapsed time. Seconds under a minute, because a vehicle passing two
// cameras eight seconds apart is a different fact from one passing them in
// "0m", and negative gaps are said as negative -- two sightings that overlap in
// time mean a track split or two overlapping views, and rounding that away
// hides it. See app/trajectory.py, which leaves the sign alone for the same
// reason.
export function durationText(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return null
  const sign = seconds < 0 ? '-' : ''
  const total = Math.abs(seconds)
  if (total < 60) return `${sign}${total < 10 ? total.toFixed(1) : Math.round(total)}s`
  const minutes = Math.floor(total / 60)
  if (minutes < 60) return `${sign}${minutes}m ${String(Math.round(total % 60)).padStart(2, '0')}s`
  const hours = Math.floor(minutes / 60)
  return `${sign}${hours}h ${String(minutes % 60).padStart(2, '0')}m`
}
