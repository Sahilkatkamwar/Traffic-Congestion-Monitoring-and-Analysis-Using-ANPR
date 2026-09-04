import { useEffect, useRef } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { ATTRIBUTION, TILES } from './MapCanvas'
import { bendPath } from '../lib/insights'

// Where the traffic was, and where it went next.
//
// A third Leaflet map rather than a mode of the other two, for the same reason
// TrajectoryPath is not a mode of MapCanvas: MapCanvas draws sources and their
// status, TrajectoryPath draws one vehicle's stops in order, and this draws
// volume. Only the basemap is shared, and it is imported so a tile change stays
// one edit.
//
// ------------------------------------------------------------------ the heat
//
// **This is a density map of cameras, not of traffic**, and that is a limit of
// the data rather than of the drawing. A sighting knows which camera saw it and
// the camera knows where it stands; nothing anywhere records where the vehicle
// was between two cameras. So each placed source gets a blob weighted by what
// it saw, the radius is a real distance on the ground rather than a fixed
// number of screen pixels, and the panel above says in words how many sightings
// could not be placed at all. Smearing vehicles along a guessed route would
// draw traffic onto roads nobody filmed.
//
// The ramp is one hue, dark to bright, because the quantity is a magnitude. A
// rainbow would imply categories.
//
// ----------------------------------------------------------------- the flows
//
// A flow line is a schematic of volume, never a route -- it is drawn as a curve
// precisely so it cannot be mistaken for the road. The curve also solves a real
// problem: A-to-B and B-to-A are two different facts, and drawn straight they
// land exactly on top of each other so the busier direction is invisible. The
// bend is always to the left of travel, so the two directions separate.

const RAMP = [
  [0.0, [122, 90, 0, 0]],
  [0.25, [122, 90, 0, 190]],
  [0.6, [245, 197, 24, 225]],
  [1.0, [255, 246, 207, 240]],
]

const HEAT_METRES = 320 // radius on the ground, not on the screen
const HEAT_MIN_PX = 22
const HEAT_MAX_PX = 190

function rampLookup() {
  // 256-entry lookup, built once per layer: interpolating per pixel would run
  // the ramp a quarter of a million times a frame.
  const table = new Uint8ClampedArray(256 * 4)
  for (let i = 0; i < 256; i += 1) {
    const t = i / 255
    let lower = RAMP[0]
    let upper = RAMP[RAMP.length - 1]
    for (let s = 0; s < RAMP.length - 1; s += 1) {
      if (t >= RAMP[s][0] && t <= RAMP[s + 1][0]) {
        lower = RAMP[s]
        upper = RAMP[s + 1]
        break
      }
    }
    const span = upper[0] - lower[0] || 1
    const f = (t - lower[0]) / span
    for (let c = 0; c < 4; c += 1) {
      table[i * 4 + c] = lower[1][c] + (upper[1][c] - lower[1][c]) * f
    }
  }
  return table
}

const RAMP_TABLE = rampLookup()

const HeatLayer = L.Layer.extend({
  initialize(points, max) {
    this._points = points
    this._max = max || 1
  },

  setData(points, max) {
    this._points = points
    this._max = max || 1
    this._redraw()
  },

  onAdd(map) {
    this._map = map
    this._canvas = L.DomUtil.create('canvas', 'leaflet-zoom-animated')
    this._canvas.style.pointerEvents = 'none'
    map.getPanes().overlayPane.appendChild(this._canvas)
    map.on('moveend zoomend resize', this._redraw, this)
    this._redraw()
  },

  onRemove(map) {
    map.off('moveend zoomend resize', this._redraw, this)
    if (this._canvas?.parentNode) this._canvas.parentNode.removeChild(this._canvas)
    this._canvas = null
    this._map = null
  },

  _radiusPx() {
    const centre = this._map.getCenter()
    const east = L.latLng(centre.lat, centre.lng + 0.01)
    const metresPerDegree = centre.distanceTo(east) / 0.01
    const a = this._map.latLngToLayerPoint(centre)
    const b = this._map.latLngToLayerPoint(east)
    const pxPerMetre = Math.abs(b.x - a.x) / metresPerDegree || 0
    return Math.min(HEAT_MAX_PX, Math.max(HEAT_MIN_PX, HEAT_METRES * pxPerMetre))
  },

  _redraw() {
    const map = this._map
    const canvas = this._canvas
    if (!map || !canvas) return

    const size = map.getSize()
    const topLeft = map.containerPointToLayerPoint([0, 0])
    L.DomUtil.setPosition(canvas, topLeft)
    canvas.width = size.x
    canvas.height = size.y

    const ctx = canvas.getContext('2d')
    ctx.clearRect(0, 0, size.x, size.y)
    if (!this._points.length) return

    const radius = this._radiusPx()

    // Pass one: accumulate weight as alpha. Overlapping cameras add up, which
    // is the whole point of a density surface rather than a set of circles.
    for (const point of this._points) {
      const at = map.latLngToContainerPoint([point.lat, point.lon])
      if (at.x < -radius || at.y < -radius || at.x > size.x + radius || at.y > size.y + radius) {
        continue
      }
      const weight = Math.max(0.08, Math.min(1, point.count / this._max))
      const gradient = ctx.createRadialGradient(at.x, at.y, 0, at.x, at.y, radius)
      gradient.addColorStop(0, `rgba(0,0,0,${weight})`)
      gradient.addColorStop(1, 'rgba(0,0,0,0)')
      ctx.fillStyle = gradient
      ctx.beginPath()
      ctx.arc(at.x, at.y, radius, 0, Math.PI * 2)
      ctx.fill()
    }

    // Pass two: colourise the accumulated alpha through the ramp.
    const image = ctx.getImageData(0, 0, size.x, size.y)
    const data = image.data
    for (let i = 0; i < data.length; i += 4) {
      const alpha = data[i + 3]
      if (alpha === 0) continue
      const offset = alpha * 4
      data[i] = RAMP_TABLE[offset]
      data[i + 1] = RAMP_TABLE[offset + 1]
      data[i + 2] = RAMP_TABLE[offset + 2]
      data[i + 3] = RAMP_TABLE[offset + 3]
    }
    ctx.putImageData(image, 0, 0)
  },
})

function volumeIcon(count, max, dim) {
  // Proportional symbol, area-scaled: doubling the count doubles the ink, which
  // is what a radius-scaled circle gets wrong by squaring it.
  const size = dim ? 10 : Math.round(16 + 26 * Math.sqrt(count / Math.max(max, 1)))
  const label = dim || size < 24 ? '' : String(count)
  return L.divIcon({
    className: '',
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
    popupAnchor: [0, -size / 2],
    html: `
      <span style="display:grid;place-items:center;width:${size}px;height:${size}px;
                   border-radius:999px;
                   background:${dim ? 'var(--plate-yellow)' : 'rgba(245,197,24,.22)'};
                   box-shadow:inset 0 0 0 2px var(--plate-yellow),
                              0 0 0 2px rgba(15,19,25,.8);
                   color:var(--ink-hi);font:600 11px var(--font-sans);
                   font-variant-numeric:tabular-nums">${label}</span>`,
  })
}

const escape = (value) =>
  String(value ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]))

export default function DensityMap({ heat, flows, showHeat, showFlows }) {
  const containerRef = useRef(null)
  const mapRef = useRef(null)
  const layerRef = useRef(null)
  const heatRef = useRef(null)
  const fittedRef = useRef(null)

  useEffect(() => {
    const map = L.map(containerRef.current, {
      zoomControl: false,
      attributionControl: true,
      preferCanvas: true,
    }).setView([22.35, 78.9], 5)
    L.control.zoom({ position: 'topright' }).addTo(map)
    L.tileLayer(TILES, { attribution: ATTRIBUTION, maxZoom: 18 }).addTo(map)
    layerRef.current = L.layerGroup().addTo(map)
    mapRef.current = map
    return () => {
      map.remove()
      mapRef.current = null
      layerRef.current = null
      heatRef.current = null
    }
  }, [])

  // The heat surface, added and removed rather than redrawn empty, so the
  // toggle is off in the sense of not being there.
  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    if (!showHeat) {
      if (heatRef.current) {
        map.removeLayer(heatRef.current)
        heatRef.current = null
      }
      return
    }
    if (!heatRef.current) {
      heatRef.current = new HeatLayer(heat.points, heat.max)
      map.addLayer(heatRef.current)
    } else {
      heatRef.current.setData(heat.points, heat.max)
    }
  }, [heat, showHeat])

  useEffect(() => {
    const map = mapRef.current
    const layer = layerRef.current
    if (!map || !layer) return
    layer.clearLayers()

    if (showFlows) {
      const drawable = flows.links.filter((flow) => flow.drawable)
      const top = Math.max(1, ...drawable.map((flow) => flow.count))
      for (const flow of drawable) {
        const path = bendPath(
          [flow.from_lat, flow.from_lon],
          [flow.to_lat, flow.to_lon],
        )
        const line = L.polyline(path, {
          color: 'var(--plate-yellow)',
          // Weighted by volume, which is the whole job of the panel. Floored at
          // 1.5px so a single journey is still a visible line.
          weight: 1.5 + 6 * (flow.count / top),
          opacity: 0.35 + 0.5 * (flow.count / top),
          lineCap: 'round',
        }).addTo(layer)
        line.bindPopup(`
          <div style="font-family:var(--font-sans);color:var(--ink-hi);min-width:170px">
            <div style="font-weight:600">${escape(flow.from_name)} &rarr; ${escape(flow.to_name)}</div>
            <div style="color:var(--ink-mid);font-size:12px;margin-top:2px">
              ${flow.count} crossing${flow.count === 1 ? '' : 's'}${
                flow.distance_km == null ? '' : ` &middot; ${flow.distance_km.toFixed(2)} km`
              }
            </div>
            ${
              flow.median_speed_kmh == null
                ? ''
                : `<div style="color:var(--ink-mid);font-size:12px">median ${flow.median_speed_kmh} km/h</div>`
            }
          </div>`)
      }
    }

    for (const point of heat.points) {
      const marker = L.marker([point.lat, point.lon], {
        icon: volumeIcon(point.count, heat.max, showHeat),
        keyboard: true,
        title: `${point.name} -- ${point.count} vehicles`,
        zIndexOffset: 500,
      }).addTo(layer)
      marker.bindPopup(`
        <div style="font-family:var(--font-sans);color:var(--ink-hi);min-width:150px">
          <div style="font-weight:600">${escape(point.name)}</div>
          <div style="color:var(--ink-mid);font-size:12px">${point.count} vehicles in this window</div>
        </div>`)
    }

    // Fit to the placed sources once per set of them. Refitting on a toggle
    // would move the map under someone who had just panned it.
    const key = heat.points.map((point) => point.source_id).sort().join(',')
    if (heat.points.length && fittedRef.current !== key) {
      fittedRef.current = key
      map.fitBounds(
        L.latLngBounds(heat.points.map((point) => [point.lat, point.lon])),
        { padding: [80, 80], maxZoom: 15 },
      )
    }
  }, [heat, flows, showFlows, showHeat])

  return <div ref={containerRef} className="absolute inset-0" aria-label="Traffic density map" />
}
