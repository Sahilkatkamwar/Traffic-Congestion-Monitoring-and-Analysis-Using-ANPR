import { useEffect, useRef } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { hereIcon, markerIcon, markerPopup } from './CameraMarker'
import { attachBaseLayer } from '../lib/basemap'
import { loadBoundaries } from '../lib/boundaries'
import { getBoundaryAt } from '../lib/api'

// Leaflet directly, driven from an effect. Leaflet owns the DOM node and React
// never touches it -- the two only meet through the marker table below.
//
// P10 took the base layer out of this file. Which tiles get drawn depends on
// whether MAPTILER_API_KEY is set on the machine serving the bundle, which is
// something only the server knows, so `attachBaseLayer` asks it -- once per
// page, for all four maps in the app. The key itself never reaches here: when
// there is one, the tile URL points back at the app and it attaches the key on
// the way out.

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

// P10's administrative outlines. Deliberately NOT the accent: a boundary is
// context, not an answer, and plate yellow on this map already means "the
// vehicle you asked about". The outer of the two levels on screen is drawn
// heavier than the inner one, so a district reads as containing its talukas
// without needing a legend.
const BOUNDARY_STYLE = {
  color: '#8fa3b8',
  weight: 1.1,
  opacity: 0.5,
  fill: false,
  interactive: false,
}
const OUTER_BOUNDARY_STYLE = { ...BOUNDARY_STYLE, weight: 2, opacity: 0.65 }
// The one the clicked point sits in. This IS an answer, so it takes the accent.
const BOUNDARY_SELECTED_STYLE = {
  color: 'var(--plate-yellow)',
  weight: 2.5,
  opacity: 0.95,
  fill: true,
  fillColor: 'var(--plate-yellow)',
  fillOpacity: 0.06,
  interactive: false,
}

// A pan fires `moveend` more than once as it settles, and a zoom fires it
// again. Long enough to ask one question per gesture rather than five.
const MOVE_SETTLE_MS = 400

export default function MapCanvas({
  sources,
  activeSourceIds,
  onSelectSource,
  here = null,
  trails = [],
  boundaries = false,
  onBoundaries = null,
}) {
  const containerRef = useRef(null)
  const mapRef = useRef(null)
  const markersRef = useRef(new Map())
  const hereRef = useRef(null)
  const trailsRef = useRef(new Map())
  const fittedRef = useRef(false)
  const boundaryLayerRef = useRef(null)
  const boundaryPickRef = useRef(null)
  const boundaryOnRef = useRef(boundaries)
  const reportRef = useRef(onBoundaries)
  boundaryOnRef.current = boundaries
  reportRef.current = onBoundaries

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

    let alive = true
    attachBaseLayer(map, { alive: () => alive })
    mapRef.current = map

    return () => {
      alive = false
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

  // P10. Administrative outlines for whatever is on screen, and the one the
  // clicked point sits inside.
  //
  // Drawn only while the toggle is on, refetched only when the map settles,
  // and answered from a cache when the box already in hand covers where the
  // map now is -- the Overpass instance behind this is a shared free service
  // and a request per pan tick is what exhausts it.
  useEffect(() => {
    const map = mapRef.current
    if (!map) return undefined

    const clear = () => {
      boundaryLayerRef.current?.remove()
      boundaryLayerRef.current = null
      boundaryPickRef.current?.remove()
      boundaryPickRef.current = null
    }

    if (!boundaries) {
      clear()
      return undefined
    }

    let alive = true
    let timer = null
    // What is currently drawn, so a click can find the outline for the area it
    // landed in without asking for the geometry a second time.
    let drawn = null

    const draw = (data) => {
      if (!alive) return
      drawn = data
      boundaryLayerRef.current?.remove()
      const group = L.layerGroup()
      // The outer of the two levels on screen -- the smaller admin_level -- is
      // the containing one, and is drawn heavier, so a district reads as
      // holding its talukas without needing a legend.
      const outermost = data.features.reduce(
        (lowest, feature) =>
          lowest === null || Number(feature.admin_level) < lowest
            ? Number(feature.admin_level)
            : lowest,
        null,
      )
      for (const feature of data.features) {
        const style =
          Number(feature.admin_level) === outermost
            ? OUTER_BOUNDARY_STYLE
            : BOUNDARY_STYLE
        let labelled = false
        for (const ring of feature.rings) {
          const line = L.polyline(ring.points, style)
          group.addLayer(line)
          if (!labelled && ring.points.length > 2) {
            labelled = true
            line.bindTooltip(feature.name, {
              permanent: true,
              direction: 'center',
              className: 'boundary-label',
              // A label under the cursor must not eat a click meant for the
              // map: the click is how you ask which area you are in.
              interactive: false,
            })
          }
        }
      }
      group.addTo(map)
      boundaryLayerRef.current = group
      reportRef.current?.({
        state: data.features.length ? 'ready' : 'empty',
        levels: data.level_labels,
        count: data.features.length,
        stale: data.stale,
      })
    }

    const refresh = () => {
      const bounds = map.getBounds()
      reportRef.current?.({ state: 'loading' })
      loadBoundaries(
        {
          south: bounds.getSouth(),
          west: bounds.getWest(),
          north: bounds.getNorth(),
          east: bounds.getEast(),
        },
        map.getZoom(),
      )
        .then(draw)
        .catch((error) => {
          if (!alive) return
          // The boundaries failed. The map did not, and it is left exactly as
          // it is -- the sentence the server wrote says which of the two
          // happened and what to do about it.
          reportRef.current?.({ state: 'error', detail: error.message })
        })
    }

    const settle = () => {
      clearTimeout(timer)
      timer = setTimeout(refresh, MOVE_SETTLE_MS)
    }

    const identify = (event) => {
      const { lat, lng } = event.latlng
      reportRef.current?.({ state: 'identifying' })
      getBoundaryAt(lat, lng)
        .then((answer) => {
          if (!alive || !boundaryOnRef.current) return
          reportRef.current?.({ state: 'at', point: [lat, lng], areas: answer.areas })
          boundaryPickRef.current?.remove()
          boundaryPickRef.current = null
          // Highlight the innermost area the click landed in, when its outline
          // is one of the ones on screen. When it is not -- the click was in a
          // village this zoom does not draw -- the panel still names it and
          // nothing is highlighted, rather than something else being
          // highlighted in its place.
          const innermost = answer.areas[answer.areas.length - 1]
          const match = innermost
            ? (drawn?.features || []).find(
                (feature) =>
                  feature.name === innermost.name &&
                  feature.admin_level === innermost.admin_level,
              )
            : null
          if (!match) return
          const group = L.layerGroup()
          for (const ring of match.rings) {
            group.addLayer(
              ring.closed
                ? L.polygon(ring.points, BOUNDARY_SELECTED_STYLE)
                : L.polyline(ring.points, BOUNDARY_SELECTED_STYLE),
            )
          }
          group.addTo(map)
          boundaryPickRef.current = group
        })
        .catch((error) => {
          if (!alive) return
          reportRef.current?.({ state: 'error', detail: error.message })
        })
    }

    map.on('moveend', settle)
    map.on('click', identify)
    refresh()

    return () => {
      alive = false
      clearTimeout(timer)
      map.off('moveend', settle)
      map.off('click', identify)
      clear()
    }
  }, [boundaries])

  return <div ref={containerRef} className="absolute inset-0" aria-label="Source map" />
}
