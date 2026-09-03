import { useEffect, useRef } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'

// Click the map to place a source. Leaflet owns the node; React only reads the
// coordinate back out.
//
// The same tiles as the Live map, because a camera placed here has to look like
// it is in the same place when it appears there.

const TILES =
  'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}'
const ATTRIBUTION =
  'Tiles &copy; <a href="https://www.esri.com/">Esri</a>, &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'

const FALLBACK_CENTRE = [22.35, 78.9]
const FALLBACK_ZOOM = 5
// Close enough to see which side of a junction a camera is on.
const PLACED_ZOOM = 16

function pin() {
  return L.divIcon({
    className: '',
    iconSize: [18, 18],
    iconAnchor: [9, 9],
    html:
      '<span style="position:absolute;inset:3px;border-radius:999px;' +
      'background:var(--plate-yellow);box-shadow:0 0 0 3px rgba(15,19,25,.85),' +
      '0 2px 8px rgba(0,0,0,.6)"></span>',
  })
}

export default function MapPicker({ value, onPick, others = [], height = 300 }) {
  const containerRef = useRef(null)
  const mapRef = useRef(null)
  const markerRef = useRef(null)
  const pickRef = useRef(onPick)
  pickRef.current = onPick

  useEffect(() => {
    const start = value?.lat != null ? [value.lat, value.lon] : FALLBACK_CENTRE
    const map = L.map(containerRef.current, {
      zoomControl: true,
      attributionControl: true,
      preferCanvas: true,
    }).setView(start, value?.lat != null ? PLACED_ZOOM : FALLBACK_ZOOM)

    L.tileLayer(TILES, { attribution: ATTRIBUTION, maxZoom: 16 }).addTo(map)

    // Already-placed sources, so a new camera can be put in relation to them
    // rather than onto an empty map.
    for (const other of others) {
      if (other.lat == null || other.lon == null) continue
      L.circleMarker([other.lat, other.lon], {
        radius: 5,
        color: 'rgba(255,255,255,0.35)',
        weight: 1,
        fillColor: '#66727f',
        fillOpacity: 0.9,
      })
        .addTo(map)
        .bindTooltip(other.name)
    }

    map.on('click', (event) => {
      const { lat, lng } = event.latlng
      pickRef.current?.({ lat: Number(lat.toFixed(6)), lon: Number(lng.toFixed(6)) })
    })

    mapRef.current = map
    // Leaflet measures its container on creation, and inside a dialog that is
    // still animating open the measurement is wrong -- the tiles come back as
    // grey until something forces a resize.
    const settle = setTimeout(() => map.invalidateSize(), 260)

    return () => {
      clearTimeout(settle)
      map.remove()
      mapRef.current = null
      markerRef.current = null
    }
    // Mount once. `value` moves the marker below, not the whole map.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    if (value?.lat == null || value?.lon == null) {
      markerRef.current?.remove()
      markerRef.current = null
      return
    }
    const at = [value.lat, value.lon]
    if (markerRef.current) markerRef.current.setLatLng(at)
    else markerRef.current = L.marker(at, { icon: pin(), keyboard: false }).addTo(map)
  }, [value])

  return (
    <div>
      <div
        ref={containerRef}
        style={{ height }}
        className="overflow-hidden rounded-card"
        aria-label="Map. Click to place this source."
      />
      <div className="mt-2 flex items-center justify-between gap-3 text-[12.5px]">
        {value?.lat != null ? (
          <span className="tabular-nums text-ink-mid">
            {value.lat.toFixed(5)}, {value.lon.toFixed(5)}
          </span>
        ) : (
          <span className="text-ink-low">
            Click the map to place this source. Unplaced sources still record
            sightings — they just cannot appear on a trajectory.
          </span>
        )}
        {value?.lat != null && (
          <button
            type="button"
            onClick={() => pickRef.current?.({ lat: null, lon: null })}
            className="rounded-control px-2 py-1 text-ink-low transition-colors duration-150 hover:bg-surface-2 hover:text-ink-hi"
          >
            Clear
          </button>
        )}
      </div>
    </div>
  )
}
