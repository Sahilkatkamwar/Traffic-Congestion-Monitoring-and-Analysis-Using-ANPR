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

// crop_path is stored relative to the project root ('crops/evidence/...') and
// FastAPI mounts that directory at /crops, so the stored value is the URL.
export const cropUrl = (path) => (path ? `/${path}` : null)

// The camera wall. The query string is a cache-buster: an <img> pointed at a
// url it has already loaded will not reconnect to it, so a restarted stream
// needs a url the browser has not seen.
export const streamUrl = (id, nonce) =>
  `/api/sources/${encodeURIComponent(id)}/stream.mjpg?v=${nonce}`
