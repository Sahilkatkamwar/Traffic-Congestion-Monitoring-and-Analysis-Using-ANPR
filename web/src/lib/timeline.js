// The scrubber's arithmetic, kept out of the component that draws it.
//
// The scrubber runs over TIME, not over the list of stops, and that distinction
// is the whole point of it. A journey of four stops is not four equal steps: a
// vehicle can pass two cameras eight seconds apart and then take four minutes
// to reach the third, and a scrubber indexed by stop draws those as the same
// interval -- a picture of a journey that never happened. Positions here are
// fractions of the elapsed span, so the ticks bunch where the vehicle moved
// quickly between cameras and spread where it did not.
//
// These two functions are what keeps the map and the evidence strip in sync:
// both are driven by indexAt() over the same positions, so there is one answer
// to "which stop is the head on" rather than two that can drift apart.

/** Each stop's first_seen_ts as epoch ms, or null when it is unparseable. */
export function stopTimes(stops) {
  return stops.map((stop) => {
    const at = new Date(stop.first_seen_ts).getTime()
    return Number.isNaN(at) ? null : at
  })
}

/**
 * Where each stop sits on the 0-1 axis, and the span it was measured over.
 *
 * The one case with no span is a vehicle seen once, or twice at the same
 * instant. Then time cannot order anything and the stops fall back to equal
 * spacing -- the list order, which is the order the trajectory returned them
 * in. A slider over a zero span would otherwise put every stop at 0.
 */
export function timeline(stops) {
  const times = stopTimes(stops)
  const known = times.filter((at) => at != null)
  const start = known.length ? Math.min(...known) : 0
  const end = known.length ? Math.max(...known) : 0
  const span = end - start
  const positions = times.map((at, index) =>
    at == null || span <= 0 ? (stops.length > 1 ? index / (stops.length - 1) : 0) : (at - start) / span,
  )
  return { start, end, span, positions }
}

/** The stop the head is on: the last one it has reached. */
export function indexAt(positions, value) {
  let found = 0
  for (let i = 0; i < positions.length; i += 1) {
    if (positions[i] <= value + 1e-9) found = i
  }
  return found
}
