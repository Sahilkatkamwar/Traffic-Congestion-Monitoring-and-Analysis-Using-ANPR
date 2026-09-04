import { useEffect, useRef } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { ATTRIBUTION, TILES } from './MapCanvas'

// One vehicle's path, drawn on its own Leaflet map.
//
// A separate map from MapCanvas rather than a mode of it: that one draws
// sources and their status, this one draws stops and their order, and the only
// thing the two share is the basemap -- which is imported rather than copied so
// a tile change is one edit.
//
// The path is two lines, not one. The whole route is a faint dashed line so the
// shape of the journey is visible at all times; the part up to the scrubber's
// position is drawn solid on top of it. That is what makes the scrubber read as
// playing the journey rather than as selecting from a list.
//
// A stop at an unplaced source has no coordinates and gets no marker. It is not
// dropped from the trajectory -- the vehicle was still seen there -- and the
// screen says so in words rather than inventing a position for it.

const TRAVELLED = 'var(--plate-yellow)'
const WHOLE = 'rgba(233, 238, 244, 0.35)'

function stopIcon(number, { active, seen }) {
  const background = active
    ? 'var(--plate-yellow)'
    : seen
      ? 'var(--surface-3)'
      : 'var(--surface-2)'
  const color = active ? '#1a1400' : seen ? 'var(--ink-hi)' : 'var(--ink-mid)'
  const size = active ? 30 : 24
  return L.divIcon({
    className: '',
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
    popupAnchor: [0, -size / 2],
    html: `
      <span style="display:grid;place-items:center;width:${size}px;height:${size}px;
                   border-radius:999px;background:${background};color:${color};
                   font:600 ${active ? 13 : 12}px var(--font-sans);
                   box-shadow:0 0 0 3px rgba(15,19,25,.85), 0 3px 10px rgba(0,0,0,.55)">
        ${number}
      </span>`,
  })
}

export default function TrajectoryPath({ stops, activeIndex, onSelectStop }) {
  const containerRef = useRef(null)
  const mapRef = useRef(null)
  const layerRef = useRef(null)
  const fitKeyRef = useRef(null)

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
    }
  }, [])

  useEffect(() => {
    const map = mapRef.current
    const layer = layerRef.current
    if (!map || !layer) return
    layer.clearLayers()

    const placed = stops
      .map((stop, index) => ({ stop, index }))
      .filter(({ stop }) => stop.lat != null && stop.lon != null)
    if (placed.length === 0) return

    const line = placed.map(({ stop }) => [stop.lat, stop.lon])
    if (line.length > 1) {
      L.polyline(line, {
        color: WHOLE,
        weight: 2,
        dashArray: '4 7',
        interactive: false,
      }).addTo(layer)

      // Everything up to and including the active stop. Slicing on the stop's
      // own index, not on its position in the placed list, so an unplaced stop
      // in the middle does not make the solid line run ahead of the scrubber.
      const travelled = placed
        .filter(({ index }) => index <= activeIndex)
        .map(({ stop }) => [stop.lat, stop.lon])
      if (travelled.length > 1) {
        L.polyline(travelled, {
          color: TRAVELLED,
          weight: 3.5,
          opacity: 0.95,
          interactive: false,
        }).addTo(layer)
      }
    }

    for (const { stop, index } of placed) {
      const active = index === activeIndex
      const marker = L.marker([stop.lat, stop.lon], {
        icon: stopIcon(index + 1, { active, seen: index <= activeIndex }),
        keyboard: true,
        title: `Stop ${index + 1} -- ${stop.source_name}`,
        zIndexOffset: active ? 1000 : 0,
      }).addTo(layer)
      marker.on('click', () => onSelectStop?.(index))
    }

    // Fit once per path, not per scrub. Refitting on every frame of playback
    // would make the map jump under someone watching it.
    const key = stops.map((stop) => stop.sighting_id).join(',')
    if (fitKeyRef.current !== key) {
      fitKeyRef.current = key
      const bounds = L.latLngBounds(line)
      map.fitBounds(bounds, { padding: [70, 70], maxZoom: 16 })
    }
  }, [stops, activeIndex, onSelectStop])

  return <div ref={containerRef} className="absolute inset-0" aria-label="Vehicle path" />
}
