import { useEffect, useRef } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { markerIcon, markerPopup } from './CameraMarker'

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

export default function MapCanvas({ sources, activeSourceIds, onSelectSource }) {
  const containerRef = useRef(null)
  const mapRef = useRef(null)
  const markersRef = useRef(new Map())
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

  return <div ref={containerRef} className="absolute inset-0" aria-label="Source map" />
}
