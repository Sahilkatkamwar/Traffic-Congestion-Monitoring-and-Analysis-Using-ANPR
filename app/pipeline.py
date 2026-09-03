"""Worker supervision and the single database writer.

Four threads live in the API process:

  feeder      drains the multiprocessing queue the workers push to and hands
              each message to the writer's inbox
  writer      the ONLY thing that writes to SQLite. Concurrent writers lock
              even in WAL mode, so workers never touch the database -- they
              push dicts -- and neither does the API: it submits a callable
              here and waits for the answer.
  supervisor  notices a worker that died without saying why and marks its
              source `error` so the reason reaches the UI instead of the void.
  previews    drains annotated frames from running workers and keeps the most
              recent one per source for the camera wall to serve.

Workers themselves are spawned processes: on Windows spawn re-imports the
target module in the child, so run_worker is module-level and every argument
crosses as a plain picklable value.

P4a added `on_event`: an optional callback the writer calls after a row is
committed, so the live feed shows what was stored rather than what was claimed.
It is set by the API to the websocket hub's publish. Left None the pipeline
behaves exactly as it did in P1.

P4b splits the old single drain loop into feeder + writer. The reason is the
one-writer rule: the UI now creates, edits, places and deletes sources, and
those writes have to happen on the same connection as the workers' or there are
two writers again. They cannot wait behind a blocking multiprocessing get, so
the multiprocessing get moved into its own thread and both kinds of work now
arrive on one in-process inbox. What the writer does with a worker message is
unchanged.
"""

import multiprocessing as mp
import queue as queue_mod
import threading
import time

from app import db
from app.worker import run_worker

# Sentinel put on the inbox when the feeder has nothing left to hand over and
# everything has stopped.
_DONE = object()

# How long after a worker process dies we wait for its final status message to
# come through the queue before calling the death unexplained.
DEATH_GRACE_SEC = 3.0
SUPERVISE_EVERY_SEC = 1.0
# How long a start waits for a worker that has already reported it is finished
# to actually exit, before calling it still running.
EXIT_GRACE_SEC = 8.0

SIGHTING_COLUMNS = (
    "source_id",
    "track_id",
    "plate_raw",
    "plate_text",
    "plate_conf",
    "plate_candidates",
    "vehicle_type",
    "vehicle_color",
    "first_seen_ts",
    "last_seen_ts",
    "crop_path",
    "plate_crop_path",
    "frames_voted",
)

# The fields a worker is given. `kind` is deliberately absent: the worker must
# not be able to branch on what sort of source it has.
WORKER_FIELDS = ("source_id", "uri", "fps", "frame_skip", "start_time")


class _Task:
    """One unit of API work, run on the writer thread and waited on by a request.

    The exception is carried back rather than raised where it happened: a
    request that asked for an invalid write must see the reason, and the writer
    must not die of it.
    """

    def __init__(self, fn):
        self.fn = fn
        self.done = threading.Event()
        self.result = None
        self.error = None

    def run(self, conn):
        try:
            self.result = self.fn(conn)
        except BaseException as exc:  # noqa: BLE001 - handed to the caller
            self.error = exc
        finally:
            self.done.set()


class Pipeline:
    """Owns every worker process, the queue they push to, and the one writer."""

    def __init__(self):
        # spawn explicitly: it is the only start method on Windows and picking
        # it here means the code behaves identically if this ever runs on Linux.
        self.ctx = mp.get_context("spawn")
        self.queue = self.ctx.Queue(maxsize=4000)
        # Annotated frames for the camera wall. Shallow on purpose: these are
        # only useful while they are current, and a deep queue of stale frames
        # is latency, not resilience.
        self.preview_queue = self.ctx.Queue(maxsize=24)
        self.procs = {}       # source_id -> (Process, stop_event)
        self.preview_events = {}  # source_id -> mp.Event, set while watched
        self.viewers = {}     # source_id -> open MJPEG connections
        self.previews = {}    # source_id -> latest annotated frame
        self.terminal = set()  # sources whose final status the writer has seen
        self.deaths = {}      # source_id -> monotonic time the process died
        self.counts = {}      # source_id -> sightings written this run
        # source_id -> the worker's own instrumentation from its last run.
        # In memory only: these are diagnostics, not data, and the sightings
        # table is frozen. The real-video benchmark reads them from here.
        self.stats = {}
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.threads = []
        # Everything the writer thread has to do arrives here: worker messages
        # from the feeder, and callables from the API.
        self.inbox = queue_mod.Queue()
        # Set by the API to Hub.publish. Called from the writer thread only,
        # after the commit, and never allowed to raise into the writer.
        self.on_event = None

    # ---------------------------------------------------------------- start up

    def start(self, autostart=True):
        db.init_db()
        self._reset_stale()
        self.threads = [
            threading.Thread(target=self._feed_loop, name="feeder", daemon=True),
            threading.Thread(target=self._writer_loop, name="writer", daemon=True),
            threading.Thread(target=self._supervise_loop, name="supervisor", daemon=True),
            threading.Thread(target=self._preview_loop, name="previews", daemon=True),
        ]
        for thread in self.threads:
            thread.start()
        if autostart:
            self.autostart()

    def _reset_stale(self):
        """A source left `running` by a crash is not running now."""
        conn = db.connect()
        try:
            cur = conn.execute(
                "UPDATE sources SET status = 'idle', progress = NULL "
                "WHERE status = 'running'"
            )
            conn.commit()
            if cur.rowcount:
                print(f"[pipeline] reset {cur.rowcount} stale running source(s) to idle")
        finally:
            conn.close()

    def autostart(self):
        """Start a worker for every source that is not already finished.

        `done` means a recorded file has been processed end to end; restarting
        it would duplicate every sighting it produced.
        """
        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT source_id FROM sources WHERE status IN ('idle', 'error')"
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            self.start_source(row["source_id"])

    # ----------------------------------------------------------------- control

    def start_source(self, source_id):
        """Spawn a worker. Returns (started, message)."""
        with self.lock:
            proc = self.procs.get(source_id)
            if proc and proc[0].is_alive():
                # A worker reports its final status and then exits, so there is
                # a window where the row already says `done` and the process is
                # still winding down. Someone reading `done` in the UI and
                # pressing Start lands exactly in it, and refusing them with
                # "already running" would be a lie about a source that has
                # visibly stopped. Wait for the exit it has already announced.
                if source_id in self.terminal:
                    proc[0].join(EXIT_GRACE_SEC)
                if proc[0].is_alive():
                    return False, f"{source_id} is already running"

            conn = db.connect()
            try:
                row = conn.execute(
                    "SELECT * FROM sources WHERE source_id = ?", (source_id,)
                ).fetchone()
            finally:
                conn.close()
            if row is None:
                return False, f"No source named {source_id}"

            source = {k: row[k] for k in WORKER_FIELDS}
            stop_event = self.ctx.Event()
            preview_on = self.ctx.Event()
            # A wall tile that was open when this source last ran is still open
            # now, so the new worker starts drawing straight away rather than
            # after the viewer reconnects.
            if self.viewers.get(source_id):
                preview_on.set()
            process = self.ctx.Process(
                target=run_worker,
                args=(source, self.queue, stop_event, self.preview_queue, preview_on),
                name=f"worker-{source_id}",
                daemon=True,
            )
            process.start()
            self.procs[source_id] = (process, stop_event)
            self.preview_events[source_id] = preview_on
            self.terminal.discard(source_id)
            self.deaths.pop(source_id, None)
            self.counts[source_id] = 0
            print(f"[pipeline] started worker for {source_id} (pid {process.pid})")
            return True, f"Started {source_id}"

    def stop_source(self, source_id, timeout=10.0):
        """Ask a worker to stop, then make sure it did."""
        with self.lock:
            entry = self.procs.get(source_id)
        if entry is None:
            return False, f"{source_id} is not running"
        process, stop_event = entry
        stop_event.set()
        process.join(timeout)
        if process.is_alive():
            print(f"[pipeline] {source_id} did not stop, terminating")
            process.terminate()
            process.join(5)
        with self.lock:
            self.procs.pop(source_id, None)
            self.preview_events.pop(source_id, None)
            # A stopped worker draws nothing, so the last frame it drew is not
            # a live feed any more. Better an honest gap than a frozen picture
            # that looks like a running camera.
            self.previews.pop(source_id, None)
        return True, f"Stopped {source_id}"

    def shutdown(self):
        """Stop every worker, then drain what they already sent."""
        self.stopping.set()
        with self.lock:
            source_ids = list(self.procs)
        for source_id in source_ids:
            self.stop_source(source_id)
        for thread in self.threads:
            thread.join(timeout=10)
        # Let each queue's feeder thread finish or the process hangs at exit.
        for queue in (self.queue, self.preview_queue):
            queue.close()
            queue.join_thread()
        print("[pipeline] shut down")

    def status(self):
        with self.lock:
            return {
                sid: {"pid": p.pid, "alive": p.is_alive(),
                      "sightings": self.counts.get(sid, 0)}
                for sid, (p, _) in self.procs.items()
            }

    def running_uris(self):
        """The uris of sources with a live worker.

        Webcam detection needs this: probing an index a worker already holds
        either fails, reporting a working camera as absent, or succeeds and
        takes the device away mid-run.
        """
        with self.lock:
            ids = list(self.procs)
        if not ids:
            return []
        conn = db.connect()
        try:
            rows = conn.execute(
                f"SELECT uri FROM sources WHERE source_id IN "
                f"({','.join('?' * len(ids))})",
                ids,
            ).fetchall()
        finally:
            conn.close()
        return [row["uri"] for row in rows]

    # ------------------------------------------------------------------ writer

    def submit(self, fn, timeout=15.0):
        """Run `fn(conn)` on the writer's connection and return what it returns.

        This is how the API writes. One writer means one writer: a second
        connection issuing INSERTs from a request thread is exactly the
        concurrent writer that locks, and it would do it under the load where
        it matters least conveniently -- while three workers are streaming.
        """
        task = _Task(fn)
        self.inbox.put(task)
        if not task.done.wait(timeout):
            raise TimeoutError(
                f"The database writer did not answer within {timeout:.0f}s. "
                f"It may be blocked; check the console output."
            )
        if task.error is not None:
            raise task.error
        return task.result

    def _feed_loop(self):
        """Hand worker messages to the writer, and notice when there are none left."""
        empty_streak = 0
        try:
            while True:
                try:
                    message = self.queue.get(timeout=0.5)
                    empty_streak = 0
                except queue_mod.Empty:
                    # A worker's last messages can still be in flight through
                    # the pipe when its process has already exited, so one
                    # empty read is not proof there is nothing left to write.
                    empty_streak += 1
                    if self.stopping.is_set() and not self._any_alive() and empty_streak >= 3:
                        break
                    continue
                except (EOFError, OSError):
                    break
                self.inbox.put(message)
        finally:
            self.inbox.put(_DONE)
            print("[feeder] stopped")

    def _writer_loop(self):
        """The one writer. Nothing else in the app opens a write connection."""
        conn = db.connect()
        try:
            while True:
                try:
                    item = self.inbox.get(timeout=0.5)
                except queue_mod.Empty:
                    continue
                if item is _DONE:
                    break
                if isinstance(item, _Task):
                    item.run(conn)
                    continue
                try:
                    self._apply(conn, item)
                except Exception as exc:  # noqa: BLE001 - one bad row must not
                    # take the writer down and silently stop every source.
                    print(f"[writer] dropped a message: {type(exc).__name__}: {exc}")
        finally:
            conn.close()
            print("[writer] stopped")

    def _any_alive(self):
        with self.lock:
            return any(p.is_alive() for p, _ in self.procs.values())

    # ---------------------------------------------------------------- previews

    def _preview_loop(self):
        """Keep the newest annotated frame per source. Older ones are dropped."""
        while not self.stopping.is_set():
            try:
                message = self.preview_queue.get(timeout=0.5)
            except queue_mod.Empty:
                continue
            except (EOFError, OSError):
                break
            message["received"] = time.monotonic()
            message["seq"] = self.previews.get(message["source_id"], {}).get("seq", 0) + 1
            self.previews[message["source_id"]] = message
        print("[previews] stopped")

    def add_viewer(self, source_id):
        """One more MJPEG client. Turns drawing on in the worker if it is not."""
        with self.lock:
            self.viewers[source_id] = self.viewers.get(source_id, 0) + 1
            event = self.preview_events.get(source_id)
        if event is not None:
            event.set()

    def drop_viewer(self, source_id):
        """One fewer. The last one out stops the worker drawing at all."""
        with self.lock:
            remaining = max(0, self.viewers.get(source_id, 0) - 1)
            self.viewers[source_id] = remaining
            event = self.preview_events.get(source_id)
        if remaining == 0:
            if event is not None:
                event.clear()
            with self.lock:
                self.previews.pop(source_id, None)

    def latest_preview(self, source_id):
        return self.previews.get(source_id)

    def is_running(self, source_id):
        with self.lock:
            entry = self.procs.get(source_id)
        return entry is not None and entry[0].is_alive()

    def _extend_sighting(self, conn, row):
        """Extend the row of a track the tracker handed back after we wrote it.

        One sighting per vehicle track: a re-activated id must not become a
        second row. Returns False if there is no row to extend, in which case
        the caller inserts one.
        """
        # The plate fields move together or not at all. A re-activated track
        # has voted over strictly more reads than when its row was written, so
        # its answer supersedes the old one -- but an emission that read no
        # plate must not erase a string the first one found.
        plate_sql = ""
        if row.get("plate_raw") is not None:
            plate_sql = (
                "plate_raw = :plate_raw, plate_text = :plate_text, "
                "plate_conf = :plate_conf, plate_candidates = :plate_candidates, "
                "frames_voted = :frames_voted, "
                "plate_crop_path = COALESCE(:plate_crop_path, plate_crop_path), "
            )
        cur = conn.execute(
            # MIN/MAX rather than assignment. A row can now be extended by a
            # track that started *earlier* than the one that created it --
            # stitching finds fragments in the order they finish, not the order
            # they happened -- and overwriting first_seen_ts with a later time
            # would move the vehicle forward in every trajectory it appears in.
            # ISO-8601 UTC sorts lexicographically, so MIN/MAX on the text is
            # MIN/MAX on the instant.
            "UPDATE sightings SET "
            "first_seen_ts = MIN(first_seen_ts, :first_seen_ts), "
            "last_seen_ts = MAX(last_seen_ts, :last_seen_ts), "
            "vehicle_type = :vehicle_type, "
            f"{plate_sql}"
            # A later, worse view leaves the original evidence in place.
            "crop_path = COALESCE(:crop_path, crop_path) "
            "WHERE source_id = :source_id AND track_id = :track_id",
            row,
        )
        conn.commit()
        return cur.rowcount > 0

    def _emit(self, event):
        """Hand a committed change to the live feed. Never fatal.

        A broken listener must not stop the one writer -- the database is the
        record and the websocket is a convenience.
        """
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception as exc:  # noqa: BLE001
            print(f"[writer] live event dropped: {type(exc).__name__}: {exc}")

    def _emit_sighting(self, conn, source_id, track_id, new):
        """Publish the stored row, read back rather than reconstructed.

        The UI must show what is in the database, not what the worker sent: the
        two differ on an update, where columns are kept by COALESCE.
        """
        row = conn.execute(
            "SELECT * FROM sightings WHERE source_id = ? AND track_id = ? "
            "ORDER BY sighting_id DESC LIMIT 1",
            (source_id, track_id),
        ).fetchone()
        if row is not None:
            self._emit({"type": "sighting", "new": new, "sighting": dict(row)})

    def _apply(self, conn, message):
        kind = message.get("type")
        if kind == "sighting":
            row = {k: message.get(k) for k in SIGHTING_COLUMNS}
            if message.get("update") and self._extend_sighting(conn, row):
                self._emit_sighting(conn, row["source_id"], row["track_id"], new=False)
                return
            conn.execute(
                f"INSERT INTO sightings ({', '.join(SIGHTING_COLUMNS)}) "
                f"VALUES ({', '.join(':' + c for c in SIGHTING_COLUMNS)})",
                row,
            )
            conn.commit()
            sid = row["source_id"]
            self.counts[sid] = self.counts.get(sid, 0) + 1
            self._emit_sighting(conn, sid, row["track_id"], new=True)
        elif kind == "status":
            conn.execute(
                "UPDATE sources SET status = ?, error = ?, progress = ?, "
                "fps = COALESCE(?, fps) WHERE source_id = ?",
                (
                    message.get("status"),
                    message.get("error"),
                    message.get("progress"),
                    message.get("fps"),
                    message["source_id"],
                ),
            )
            conn.commit()
            if message.get("stats"):
                self.stats[message["source_id"]] = message["stats"]
            if message.get("terminal"):
                self.terminal.add(message["source_id"])
            source = conn.execute(
                "SELECT * FROM sources WHERE source_id = ?", (message["source_id"],)
            ).fetchone()
            if source is not None:
                self._emit({"type": "source", "source": dict(source)})
        else:
            print(f"[writer] unknown message type {kind!r}")

    # -------------------------------------------------------------- supervisor

    def _supervise_loop(self):
        """Catch workers that died without reporting a reason."""
        while not self.stopping.wait(SUPERVISE_EVERY_SEC):
            now = time.monotonic()
            with self.lock:
                entries = list(self.procs.items())
            for source_id, (process, _) in entries:
                if process.is_alive():
                    continue
                # Its final status may still be in the queue. Give the writer a
                # moment before calling this a crash.
                first_seen = self.deaths.setdefault(source_id, now)
                if now - first_seen < DEATH_GRACE_SEC:
                    continue
                with self.lock:
                    self.procs.pop(source_id, None)
                    self.preview_events.pop(source_id, None)
                    # A dead worker's last frame is not a live feed.
                    self.previews.pop(source_id, None)
                self.deaths.pop(source_id, None)
                if source_id in self.terminal:
                    continue
                reason = (
                    f"Worker process exited unexpectedly (exit code "
                    f"{process.exitcode}). Check the source is reachable and the "
                    f"console output for the traceback."
                )
                print(f"[supervisor] {source_id}: {reason}")
                self._mark_error(source_id, reason)
        print("[supervisor] stopped")

    def _mark_error(self, source_id, reason):
        """Write the failure through the writer, not from this thread."""
        try:
            self.queue.put_nowait(
                {"type": "status", "source_id": source_id, "status": "error",
                 "error": reason, "fps": None, "progress": None, "terminal": True}
            )
        except queue_mod.Full:
            print(f"[supervisor] queue full, could not record error for {source_id}")
