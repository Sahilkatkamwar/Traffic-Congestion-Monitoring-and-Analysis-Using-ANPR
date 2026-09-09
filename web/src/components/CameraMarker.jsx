import L from 'leaflet'

// Source markers, built as divIcons so status is a class rather than an image.
//
// Status is the only thing the colour says: green running, yellow idle, red
// error, slate done. The ring underneath animates only while the source has
// just emitted, which is what makes activity legible at a glance on a map with
// a dozen cameras on it.

const STATUS_COLOR = {
  running: 'var(--plate-green)',
  idle: 'var(--plate-yellow)',
  error: 'var(--plate-red)',
  done: 'var(--ink-low)',
}

const escape = (value) =>
  String(value ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]),
  )

export function markerIcon(source, { pulsing = false } = {}) {
  const color = STATUS_COLOR[source.status] || 'var(--ink-low)'
  const ring = pulsing
    ? `<span class="marker-pulse" style="position:absolute;inset:0;border-radius:999px;background:${color}"></span>`
    : ''

  return L.divIcon({
    className: '',
    iconSize: [18, 18],
    iconAnchor: [9, 9],
    popupAnchor: [0, -12],
    html: `
      <span style="position:relative;display:block;width:18px;height:18px" title="${escape(source.name)}">
        ${ring}
        <span style="position:absolute;inset:3px;border-radius:999px;background:${color};
                     box-shadow:0 0 0 3px rgba(15,19,25,.85), 0 2px 8px rgba(0,0,0,.6)"></span>
      </span>`,
  })
}

export function markerPopup(source) {
  const status = escape(source.status)
  const fps = source.fps ? `${source.fps.toFixed(1)} fps` : 'fps not measured yet'
  const error = source.error
    ? `<div style="margin-top:6px;color:var(--plate-red);max-width:24ch">${escape(source.error)}</div>`
    : ''
  return `
    <div style="font-family:var(--font-sans);color:var(--ink-hi);min-width:150px">
      <div style="font-weight:600;margin-bottom:2px">${escape(source.name)}</div>
      <div style="color:var(--ink-mid);font-size:12px">${status} · ${escape(fps)}</div>
      ${error}
    </div>`
}

// P9. Where the browser says the person looking at the map is. Deliberately
// unlike a source marker: a hollow ring in the ink colour rather than a filled
// status dot in a plate colour, because colour on this screen means status of a
// camera and this is not a camera. It carries no status, never pulses, and is
// not clickable in the way a source is.
export function hereIcon() {
  return L.divIcon({
    className: '',
    iconSize: [16, 16],
    iconAnchor: [8, 8],
    popupAnchor: [0, -10],
    html: `
      <span style="position:relative;display:block;width:16px;height:16px" title="Your location">
        <span style="position:absolute;inset:0;border-radius:999px;
                     border:2px solid var(--ink-hi);background:rgba(15,19,25,.35);
                     box-shadow:0 0 0 2px rgba(15,19,25,.75)"></span>
        <span style="position:absolute;inset:5px;border-radius:999px;background:var(--ink-hi)"></span>
      </span>`,
  })
}
