import { useEffect, useRef } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { hereIcon, markerIcon, markerPopup } from './CameraMarker'

// Leaflet directly, driven from an effect. Leaflet owns the DOM node and React
// never touches it -- the two only meet through the marker table below.
//
// Dark tiles, because the base surface is a deep slate and a bright basemap
// would fight every panel floating over it.
//
// Esri's dark canvas, not CARTO's: cartocdn still serves without a key but now
// stamps every tile with "API KEY REQUIRED", which is someone else's watermark
// across our evidence. This one is keyless and unbranded. Note the {z}/{y}/{x}
// order -- Esri puts row before column, the reverse of the usual slippy URL.
export const TILES =
  'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}'
export const ATTRIBUTION =
  'Tiles &copy; <a href="https://www.esri.com/">Esri</a>, &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'

// Centre of India, wide zoom. Only used until a source has coordinates -- the
// moment one does, the map fits to what actually exists.
const FALLBACK_CENTRE = [22.35, 78.9]
const FALLBACK_ZOOM = 5

// P9's follow trail. Plate yellow, because the accent means "this is the thing
// you asked about" everywhere else on the screen, and dashed so it never reads
// as a road.
const TRAIL_STYLE = {
  color: 'var(--plate-yellow)',
  weight: 3,
  opacity: 0.9,
  dashArray: '2 7',
  lineCap: 'round',
}

export default function MapCanvas({
  sources,
  activeSourceIds,
  onSelectSource,
  here = null,
  trails = [],
}) {
  const containerRef = useRef(null)
  const mapRef = useRef(null)
  const markersRef = useRef(new Map())
  const hereRef = useRef(null)
  const trailsRef = useRef(new Map())
  const fittedRef = useRef(false)

  useEffect(() => {
    const map = L.map(containerRef.current, {
      // The zoom control defaults to the top left, which is exactly where the
      // sighting feed floats -- Leaflet's controls outrank the panel's z-index
      // and the +/- buttons sat on top of its heading.
      zoomControl: false,
      attributionControl: true,
      preferCanvas: true,
    }).setView(FALLBACK_CENTRE, FALLBACK_ZOOM)

    L.control.zoom({ position: 'topright' }).addTo(map)

    L.tileLayer(TILES, { attribution: ATTRIBUTION, maxZoom: 16 }).addTo(map)
    mapRef.current = map

    return () => {
      map.remove()
      mapRef.current = null
      markersRef.current.clear()
    }
  }, [])

  // Markers follow the source list. A source with no coordinates has not been
  // placed yet and simply has no marker -- guessing a position would draw a
  // trajectory through a road that does not exist.
  useEffect(() => {
    const map = mapRef.current
    if (!map) return

    const placed = sources.filter((s) => s.lat != null && s.lon != null)
    const seen = new Set()

    for (const source of placed) {
      seen.add(source.source_id)
      const pulsing = activeSourceIds.has(source.source_id)
      let marker = markersRef.current.get(source.source_id)

      if (!marker) {
        marker = L.marker([source.lat, source.lon], {
          icon: markerIcon(source, { pulsing }),
          keyboard: true,
          title: source.name,
        }).addTo(map)
        marker.on('click', () => onSelectSource?.(source.source_id))
        markersRef.current.set(source.source_id, marker)
      } else {
        marker.setLatLng([source.lat, source.lon])
        marker.setIcon(markerIcon(source, { pulsing }))
      }
      marker.bindPopup(markerPopup(source))
    }

    for (const [id, marker] of markersRef.current) {
      if (!seen.has(id)) {
        marker.remove()
        markersRef.current.delete(id)
      }
    }

    // Fit once, when there is something to fit to. Refitting on every update
    // would yank the map out from under someone who just panned it.
    if (!fittedRef.current && placed.length > 0) {
      fittedRef.current = true
      map.fitBounds(
        L.latLngBounds(placed.map((s) => [s.lat, s.lon])),
        { padding: [90, 90], maxZoom: 16 },
      )
    }
  }, [sources, activeSourceIds, onSelectSource])

  // P9. Where the browser says the person looking at this is. A convenience on
  // the map and nothing else: it is never written down, never attached to a
  // sighting, and never a camera's placement -- a camera is placed by being
  // put somewhere on purpose, not by whoever happened to open the screen.
  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    if (!here) {
      // Permission was refused or withdrawn. The map is exactly what it was.
      hereRef.current?.remove()
      hereRef.current = null
      return
    }
    if (!hereRef.current) {
      hereRef.current = L.marker([here.lat, here.lon], {
        icon: hereIcon(),
        keyboard: false,
        title: 'Your location, from this browser',
        zIndexOffset: -100,
      }).addTo(map)
      hereRef.current.bindPopup(
        '<div style="font-family:var(--font-sans);color:var(--ink-hi)">' +
          '<div style="font-weight:600">You are here</div>' +
          '<div style="color:var(--ink-mid);font-size:12px;max-width:26ch">' +
          'From this browser. It is not a camera and nothing is recorded here.' +
          '</div></div>',
      )
    } else {
      hereRef.current.setLatLng([here.lat, here.lon])
    }
  }, [here])

  // P9. One line per follow session, through the cameras that vehicle has been
  // seen at, in the order it was seen. A session watching one camera has one
  // point and draws nothing -- a line needs somewhere to have gone.
  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    const seen = new Set()

    for (const trail of trails) {
      const points = (trail.points || []).filter(
        (point) => point && point[0] != null && point[1] != null,
      )
      seen.add(trail.follow_id)
      let line = trailsRef.current.get(trail.follow_id)
      if (points.length < 2) {
        line?.remove()
        trailsRef.current.delete(trail.follow_id)
        continue
      }
      if (!line) {
        line = L.polyline(points, TRAIL_STYLE).addTo(map)
        trailsRef.current.set(trail.follow_id, line)
      } else {
        line.setLatLngs(points)
      }
    }

    for (const [id, line] of trailsRef.current) {
      if (!seen.has(id)) {
        line.remove()
        trailsRef.current.delete(id)
      }
    }
  }, [trails])

  return <div ref={containerRef} className="absolute inset-0" aria-label="Source map" />
}
