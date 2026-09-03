"""Live event fan-out from the writer thread to connected browsers.

The pipeline's writer is a plain thread and the websocket handlers are
coroutines on uvicorn's event loop. Nothing may cross that boundary by touching
another thread's objects directly, so this is the one place the hand-off
happens: `publish` is called from the writer thread and does nothing except ask
the loop to run `_deliver` on itself.

Every client gets its own bounded queue. A browser tab that has stopped reading
must never be able to stall the writer -- and the writer is the only thing that
writes to SQLite, so stalling it would stop the whole app. A full queue drops
its oldest event instead: for a live feed the newest sighting is the one that
matters, and the UI reloads from /api/sightings on reconnect anyway.
"""

import asyncio
import threading

# Deep enough to ride out a burst when several workers emit at once, shallow
# enough that a dead tab cannot hoard memory.
CLIENT_QUEUE_SIZE = 200


class Hub:
    """Fan-out of pipeline events to every open websocket."""

    def __init__(self, queue_size=CLIENT_QUEUE_SIZE):
        self._clients = set()
        self._lock = threading.Lock()
        self._loop = None
        self._queue_size = queue_size
        self.dropped = 0

    def bind_loop(self, loop):
        """Called from the running event loop at startup, once."""
        self._loop = loop

    # -- called from the event loop -------------------------------------------

    def register(self):
        queue = asyncio.Queue(maxsize=self._queue_size)
        with self._lock:
            self._clients.add(queue)
        return queue

    def unregister(self, queue):
        with self._lock:
            self._clients.discard(queue)

    def client_count(self):
        with self._lock:
            return len(self._clients)

    # -- called from the writer thread ----------------------------------------

    def publish(self, event):
        """Thread-safe. Returns without raising if nobody is listening yet."""
        loop = self._loop
        if loop is None:
            return
        with self._lock:
            targets = list(self._clients)
        if not targets:
            return
        try:
            loop.call_soon_threadsafe(self._deliver, targets, event)
        except RuntimeError:
            # The loop closed while we were shutting down. Losing a live event
            # at that point is correct; the database write already happened.
            pass

    def _deliver(self, targets, event):
        for queue in targets:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                    self.dropped += 1
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
