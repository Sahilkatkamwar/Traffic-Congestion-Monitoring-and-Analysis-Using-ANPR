import { getBoundaries } from './api'

// P10. Administrative outlines for whatever the map is currently showing.
//
// Caching happens twice, on purpose, and the two layers protect different
// things:
//
//   here     stops a pan from issuing a request at all, by remembering that
//            the answer already in hand covers where the map now is
//   server   stops a request that IS issued from reaching Overpass, by
//            snapping the viewport to a grid cell and keeping the answer
//
// The second is the one PHASE2.md's trap is about -- Overpass is a shared free
// service and its rate limit is what gets hit mid-demo -- and it lives on the
// server because that is where every browser tab passes through one door. This
// layer is the cheap local one: a request that never leaves the page costs
// nothing at all.

// Kept for the life of the page. An administrative boundary does not move
// while somebody is looking at a map of it.
const answers = []
const inFlight = new Map()
const MAX_ANSWERS = 24

function key(zoom) {
  return String(Math.round(zoom))
}

// Does an answer already in hand cover where the map now is? Containment is
// the real question, and asking it directly is why the grid step the server
// snaps to is not repeated in here -- one copy of that number, on the server.
function covers(entry, view) {
  const [south, west, north, east] = entry.bbox
  return (
    south <= view.south &&
    west <= view.west &&
    north >= view.north &&
    east >= view.east
  )
}

export function cachedBoundaries(view, zoom) {
  const wanted = key(zoom)
  for (let i = answers.length - 1; i >= 0; i -= 1) {
    if (answers[i].key === wanted && covers(answers[i], view)) return answers[i].data
  }
  return null
}

/**
 * The outlines over one viewport, from cache when the cache covers it.
 *
 * Two calls for the same viewport while the first is still in flight share
 * that one request rather than racing: a map fires `moveend` more than once
 * during a single inertial pan.
 */
export async function loadBoundaries(view, zoom) {
  const hit = cachedBoundaries(view, zoom)
  if (hit) return hit

  const question =
    `${key(zoom)}|${view.south.toFixed(3)},${view.west.toFixed(3)},` +
    `${view.north.toFixed(3)},${view.east.toFixed(3)}`
  if (inFlight.has(question)) return inFlight.get(question)

  const request = getBoundaries({ ...view, zoom })
    .then((data) => {
      // Keyed by the box the SERVER snapped to, not the one that was asked
      // for: that is the box the answer actually covers, so it is the box a
      // later pan should be tested against.
      answers.push({ key: key(zoom), bbox: data.bbox, data })
      if (answers.length > MAX_ANSWERS) answers.splice(0, answers.length - MAX_ANSWERS)
      return data
    })
    .finally(() => inFlight.delete(question))

  inFlight.set(question, request)
  return request
}

export function _resetBoundaries() {
  answers.length = 0
  inFlight.clear()
}
