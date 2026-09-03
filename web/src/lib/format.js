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
