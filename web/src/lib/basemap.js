import L from 'leaflet'
import { getMapConfig } from './api'

// P10. Every map in this app draws the same base layer, and which one that is
// comes from the server rather than from a constant in here.
//
// Four maps exist -- Live, the source placer, the density map and the
// trajectory -- and they have to agree: a camera placed on one of them has to
// look like it is in the same place when it appears on the others. Before this
// they agreed by each hardcoding the same URL, which is agreement by copy.
//
// The server decides because only the server knows whether MAPTILER_API_KEY is
// set, and the key never reaches this file. When it is set, `tile_url` points
// back at the app, which attaches the key itself on the way to MapTiler. When
// it is not, it points straight at OpenStreetMap -- there is no credential to
// hide, and the map is a map either way.

// Asked for once per page load, not once per map. Four maps mount at different
// times and a request each is three more than the answer changes.
let pending = null

// What to draw while the answer is in flight, and what to fall back to if the
// server cannot be reached at all. Keyless, so it always works.
const FALLBACK = {
  provider: 'osm',
  tile_url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
  attribution:
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  max_zoom: 19,
  configured: false,
  reason: 'Standard OpenStreetMap tiles.',
}

export function mapConfig() {
  if (!pending) {
    pending = getMapConfig().catch((error) => {
      // The map is not the thing that failed, so it is not the thing that
      // reports the failure. A base layer still draws.
      console.warn('Base map config unavailable, drawing OpenStreetMap:', error.message)
      return FALLBACK
    })
  }
  return pending
}

// Only for a verification run, which mounts nothing and needs the memo not to
// leak between checks.
export function _resetMapConfig() {
  pending = null
}

/**
 * Put the base layer on a Leaflet map, and answer with the config it used.
 *
 * The layer is added when the answer arrives, which is one paint after the map
 * exists. That is deliberate: the alternative is blocking every map's first
 * render on a round trip. `alive()` lets the caller say the map was torn down
 * in between -- a screen navigated away from before the answer landed must not
 * have a layer added to a removed map.
 */
export async function attachBaseLayer(map, { alive = () => true, maxZoom = null } = {}) {
  const config = await mapConfig()
  if (!alive() || !map) return config
  const layer = L.tileLayer(config.tile_url, {
    attribution: config.attribution,
    maxZoom: maxZoom ?? config.max_zoom ?? 19,
    // Tiles beyond what the provider has are upscaled rather than left blank,
    // so zooming past a style's last level shows a blurry street instead of a
    // grey hole.
    maxNativeZoom: config.max_zoom ?? 19,
    crossOrigin: true,
  })
  layer.addTo(map)
  // Under everything: markers, trails and boundary outlines all sit on top.
  layer.bringToBack()
  return config
}
