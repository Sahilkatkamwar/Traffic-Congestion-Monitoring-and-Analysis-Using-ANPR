// The live feed. Committed rows only -- the server publishes after the write,
// so anything that arrives here is already in the database.
//
// Reconnects with backoff because a dropped socket is normal (the app restarts,
// the laptop sleeps) and a feed that gives up after one failure is worse than
// no feed at all. On reconnect the caller reloads from /api/sightings rather
// than replaying: the socket is a notification, the database is the record.
//
// P9 gave it a direction back: `send` carries follow commands up. It is the
// same socket, deliberately -- a follow only exists for as long as this
// connection does, and a second socket would be a second lifetime to keep in
// step with the first. A send made while the socket is down is dropped rather
// than queued, because the server clears a connection's follow set when that
// connection ends: replaying a start onto a new connection would silently
// resurrect a session the user watched end.

const FIRST_RETRY_MS = 800
const MAX_RETRY_MS = 15000

export function openLiveFeed({ onEvent, onStatus }) {
  const url = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/ws`
  let socket = null
  let retry = FIRST_RETRY_MS
  let timer = null
  let closedByUs = false

  function open() {
    onStatus?.('connecting')
    socket = new WebSocket(url)

    socket.onopen = () => {
      retry = FIRST_RETRY_MS
      onStatus?.('live')
    }

    socket.onmessage = (message) => {
      let event
      try {
        event = JSON.parse(message.data)
      } catch {
        return
      }
      if (event.type === 'ping') return
      onEvent?.(event)
    }

    socket.onclose = () => {
      if (closedByUs) return
      onStatus?.('offline')
      timer = setTimeout(open, retry)
      retry = Math.min(retry * 2, MAX_RETRY_MS)
    }

    // An error is always followed by a close, so reconnect is handled there.
    socket.onerror = () => socket?.close()
  }

  open()

  const close = () => {
    closedByUs = true
    clearTimeout(timer)
    socket?.close()
  }

  // Returns whether it went. The caller uses that to say so rather than to
  // leave a Follow button looking pressed on a socket that is reconnecting.
  close.send = (message) => {
    if (!socket || socket.readyState !== WebSocket.OPEN) return false
    socket.send(JSON.stringify(message))
    return true
  }

  return close
}
