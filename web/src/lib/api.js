// Every call goes to the same origin: FastAPI serves this bundle in production
// and vite proxies /api to it in dev, so there is no base URL to configure.

// The server answers a refused request with {"detail": "..."} written for a
// person to read. That sentence is the whole point of the error, so it is what
// gets thrown -- never "Request failed with status 400".
async function send(path, options) {
  let response
  try {
    response = await fetch(path, options)
  } catch (cause) {
    throw new Error(`Could not reach the server at ${path}. Is the app running?`)
  }

  let body = null
  if (response.status !== 204) {
    try {
      body = await response.json()
    } catch {
      body = null
    }
  }

  if (!response.ok) {
    const error = new Error(
      body?.detail || `${path} returned ${response.status} ${response.statusText}`,
    )
    error.status = response.status
    error.body = body
    throw error
  }
  return body
}

const json = (method) => (path, payload) =>
  send(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload ?? {}),
  })

const get = (path) => send(path)
const post = json('POST')
const patch = json('PATCH')

export const getHealth = () => get('/api/health')
export const getSources = () => get('/api/sources')
export const getSightings = (limit = 80) => get(`/api/sightings?limit=${limit}`)
export const getAlerts = (limit = 20) => get(`/api/alerts?limit=${limit}`)

// --- sources (P4b) ---------------------------------------------------------

export const getDevices = () => get('/api/devices')
export const getFiles = () => get('/api/files')
export const testSource = (uri) => post('/api/sources/test', { uri })
export const createSource = (payload) => post('/api/sources', payload)
export const updateSource = (id, payload) => patch(`/api/sources/${encodeURIComponent(id)}`, payload)
export const startSource = (id) => post(`/api/sources/${encodeURIComponent(id)}/start`)
export const stopSource = (id) => post(`/api/sources/${encodeURIComponent(id)}/stop`)

export const deleteSource = (id, { deleteSightings = false } = {}) =>
  send(
    `/api/sources/${encodeURIComponent(id)}?delete_sightings=${deleteSightings}`,
    { method: 'DELETE' },
  )

// Uploads go up as multipart with no Content-Type set by hand: the browser has
// to add the multipart boundary itself, and setting the header removes it.
export function uploadFile(file, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData()
    form.append('file', file)
    const request = new XMLHttpRequest()
    request.open('POST', '/api/uploads')
    // XHR rather than fetch for exactly one reason: a 2 GB video needs a
    // progress bar, and fetch cannot report upload progress.
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress?.(event.loaded / event.total)
    }
    request.onload = () => {
      let body = null
      try {
        body = JSON.parse(request.responseText)
      } catch {
        body = null
      }
      if (request.status >= 200 && request.status < 300) resolve(body)
      else reject(new Error(body?.detail || `Upload failed (${request.status}).`))
    }
    request.onerror = () => reject(new Error('The upload could not reach the server.'))
    request.onabort = () => reject(new Error('The upload was cancelled.'))
    request.send(form)
  })
}

// --- analyze (P4c) ---------------------------------------------------------
//
// Analyze is standalone: none of these touch a source, and an analysis never
// becomes a sighting. A job is accepted with 202 and polled until it finishes,
// because a video takes as long as the video is.

export const startAnalysis = (uri, frameSkip) =>
  post('/api/analyze', { uri, frame_skip: frameSkip })
export const getAnalysis = (id) => get(`/api/analyze/${encodeURIComponent(id)}`)
export const listAnalyses = () => get('/api/analyze')
export const cancelAnalysis = (id) =>
  post(`/api/analyze/${encodeURIComponent(id)}/cancel`)
export const deleteAnalysis = (id) =>
  send(`/api/analyze/${encodeURIComponent(id)}`, { method: 'DELETE' })

// A download, not a fetch: the server sets Content-Disposition and the browser
// saves it. Building the file in JS would mean holding a whole result document
// in a blob for no gain.
export const exportUrl = (id, format) =>
  `/api/analyze/${encodeURIComponent(id)}/export.${format}`

// --- trace (P4d) -----------------------------------------------------------
//
// Both of these are fuzzy. searchPlates never returns one silent answer -- it
// returns a ranked list with every score attached -- and getTrajectory gathers
// the stops the same way, so a vehicle read differently at two cameras is still
// one journey.

export const searchPlates = (query, limit = 10, minScore = null) =>
  get(
    `/api/search?q=${encodeURIComponent(query)}&limit=${limit}` +
      (minScore == null ? '' : `&min_score=${minScore}`),
  )

export const getTrajectory = (plate) =>
  get(`/api/trajectory?plate=${encodeURIComponent(plate)}`)

export const getSighting = (id) => get(`/api/sightings/${encodeURIComponent(id)}`)

// --- insights (P4e) --------------------------------------------------------
//
// One call, every panel. The time window is shared in the data rather than only
// in the control: five calls would let five panels answer for five slightly
// different slices while a worker is writing, and the whole promise of the
// screen is that they cannot.
//
// Both ends are optional. No window at all means everything there has ever
// been, which is what the screen opens on.

export function getInsights({ from = null, to = null, minScore = null } = {}) {
  const query = new URLSearchParams()
  if (from) query.set('from', from)
  if (to) query.set('to', to)
  if (minScore != null) query.set('min_score', minScore)
  const suffix = query.toString()
  return get(`/api/insights${suffix ? `?${suffix}` : ''}`)
}

// crop_path is stored relative to the project root ('crops/evidence/...') and
// FastAPI mounts that directory at /crops, so the stored value is the URL.
export const cropUrl = (path) => (path ? `/${path}` : null)

// The camera wall. The query string is a cache-buster: an <img> pointed at a
// url it has already loaded will not reconnect to it, so a restarted stream
// needs a url the browser has not seen.
export const streamUrl = (id, nonce) =>
  `/api/sources/${encodeURIComponent(id)}/stream.mjpg?v=${nonce}`
