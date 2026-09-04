"""FastAPI application.

P1 added read-only views of what the workers produce. P4a added the three
things the Live screen needs: a websocket carrying committed rows, the evidence
crops as static files, and the built frontend with an SPA fallback. P4b makes
sources runtime state -- created, tested, placed, started, stopped and deleted
from the UI, with no file to edit and no restart -- and serves the camera wall.

Route order in here is load-bearing. The catch-all that serves index.html is
registered last, and it refuses anything under /api/ itself, because a catch-all
declared before the API routes silently swallows them.

Every write goes through `write()`, which hands the work to the pipeline's
writer thread. One writer, always: a request thread issuing its own INSERT is
the second writer the whole design exists to avoid.
"""

import asyncio
import json
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    Body,
    FastAPI,
    File,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from app import (
    analytics,
    analyze as analysis,
    config,
    db,
    matching,
    probe,
    sources as source_rules,
    trajectory as trace,
)
from app.stream import Hub

# Long enough to stay quiet on an idle feed, short enough that a browser that
# vanished without closing the socket is noticed rather than held open forever.
PING_EVERY_SEC = 20.0

# Camera wall. A tile polls this often for a newly decoded frame; if none has
# arrived it re-sends the current one, which keeps the connection observably
# alive and lets a disconnect be noticed promptly.
STREAM_POLL_SEC = 0.03
STREAM_REPEAT_AFTER_SEC = 1.5
# A worker loads two models before it decodes anything, which on a cold start
# is tens of seconds. The tile waits rather than reporting a camera as dead.
STREAM_FIRST_FRAME_SEC = 90.0
# Once frames have started, this long without a new one means the worker has
# stopped producing and the stream should end rather than repeat forever.
STREAM_STALL_SEC = 25.0
MJPEG_BOUNDARY = "anprframe"

UPLOAD_CHUNK = 1 << 20  # 1 MiB
# A cap that a real 4K dashcam clip stays under but a runaway upload does not.
MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024

SOURCE_COLUMNS = (
    "source_id", "name", "kind", "uri", "lat", "lon", "heading_deg",
    "fps", "frame_skip", "start_time", "status", "error", "progress",
)
EDITABLE = ("name", "lat", "lon", "heading_deg", "frame_skip", "start_time", "uri", "kind")

DIST = config.ROOT / "web" / "dist"

BUILD_ME = """<!doctype html>
<meta charset="utf-8">
<title>ANPR City -- frontend not built</title>
<style>
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
         background:#10141a; color:#e8edf2;
         font:15px/1.6 "Segoe UI", system-ui, sans-serif; }}
  main {{ max-width:34rem; padding:2rem; }}
  h1 {{ font-size:1.25rem; margin:0 0 .75rem; }}
  p {{ color:#9aa7b4; margin:0 0 1rem; }}
  pre {{ background:#171d25; border-radius:12px; padding:1rem 1.25rem;
        overflow-x:auto; color:#f5c518; }}
  code {{ font-family:Consolas, "Cascadia Mono", monospace; }}
</style>
<main>
  <h1>The frontend has not been built yet.</h1>
  <p>The API is running -- <code>/api/health</code> answers and the websocket at
     <code>/api/ws</code> is live. What is missing is <code>{dist}</code>.</p>
  <p>Build it once, then reload this page:</p>
  <pre><code>cd web
npm install
npm run build</code></pre>
  <p>That needs Node on PATH. <code>node --version</code> should print a version.</p>
</main>
"""


def create_app(pipeline=None):
    hub = Hub()
    # Independent of the pipeline on purpose: the Analyze screen has to work
    # with zero cameras configured, so its job runner is not something a source
    # switches on.
    jobs = analysis.AnalysisJobs()

    @asynccontextmanager
    async def lifespan(app):
        # Bind before the workers start, or the first sightings are published
        # into a hub that has no loop to schedule them on.
        hub.bind_loop(asyncio.get_running_loop())
        jobs.start()
        if pipeline is not None:
            pipeline.on_event = hub.publish
            pipeline.start()
        yield
        if pipeline is not None:
            pipeline.shutdown()
        jobs.shutdown()

    app = FastAPI(
        title="ANPR City",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.pipeline = pipeline
    app.state.hub = hub
    app.state.jobs = jobs

    # ------------------------------------------------------------------- api

    @app.get("/api/health")
    def health():
        try:
            conn = db.connect()
            try:
                sources = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
                sightings = conn.execute("SELECT COUNT(*) FROM sightings").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "error",
                    "db": False,
                    "detail": f"Database at {db.db_path()} is unreadable: {exc}",
                },
            )
        return {
            "status": "ok",
            "db": True,
            "sources": sources,
            "sightings": sightings,
            "ui_built": (DIST / "index.html").exists(),
            "listeners": hub.client_count(),
            "workers": pipeline.status() if pipeline is not None else {},
        }

    @app.get("/api/sources")
    def sources():
        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM sources ORDER BY source_id"
            ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    @app.get("/api/sightings")
    def sightings(
        limit: int = Query(100, ge=1, le=1000),
        source_id: str | None = None,
    ):
        sql = "SELECT * FROM sightings"
        params = []
        if source_id:
            sql += " WHERE source_id = ?"
            params.append(source_id)
        # Newest first: the live feed wants the most recent sighting at the top.
        sql += " ORDER BY first_seen_ts DESC, sighting_id DESC LIMIT ?"
        params.append(limit)

        conn = db.connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    @app.get("/api/sightings/{sighting_id}")
    def sighting(sighting_id: int):
        """One sighting, whole.

        The Trace screen shows the same evidence panel the Live feed does, and
        that panel wants the fields a trajectory stop does not carry -- the
        track id and the stored candidates. Rather than widen the trajectory
        contract to feed a panel, the panel reads the row it is about.
        """
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM sightings WHERE sighting_id = ?", (sighting_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return fail(404, f"There is no sighting {sighting_id}.")
        return dict(row)

    @app.get("/api/alerts")
    def alerts(limit: int = Query(50, ge=1, le=500)):
        """Read-only. P5 is what writes rows here; until then this is empty,
        and the Live screen's alert strip renders nothing rather than something
        invented."""
        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM alerts ORDER BY created_ts DESC, alert_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    # -------------------------------------------------------- sources (P4b)

    def write(fn):
        """Run a write on the one writer thread, or inline when there is none.

        `create_app()` with no pipeline is how the route tests run: there are no
        workers, so there is nothing to be a second writer to.
        """
        if pipeline is not None:
            return pipeline.submit(fn)
        conn = db.connect()
        try:
            return fn(conn)
        finally:
            conn.close()

    def read_source(conn, source_id):
        row = conn.execute(
            "SELECT * FROM sources WHERE source_id = ?", (source_id,)
        ).fetchone()
        return None if row is None else dict(row)

    def publish_source(row):
        hub.publish({"type": "source", "source": row})
        return row

    def fail(status, detail):
        return JSONResponse(status_code=status, content={"detail": detail})

    @app.get("/api/devices")
    def devices():
        """Webcams that answer, plus the ones a worker is already holding.

        Each index is opened in a throwaway process, because opening a camera is
        the one call in this app that can block forever.
        """
        busy = []
        if pipeline is not None:
            busy = [u for u in pipeline.running_uris() if str(u).isdigit()]
        found = probe.detect_webcams(skip=busy)
        return {
            "webcams": found,
            "busy": sorted({str(u) for u in busy}),
            "searched": probe.MAX_DEVICE_INDEX + 1,
        }

    @app.post("/api/sources/test")
    def test_connection(payload: dict = Body(...)):
        """Open a source once and hand back a preview frame before it is saved.

        Nothing is written. This exists so that a camera that is not going to
        work is found out here, with a picture, rather than as an `error` row
        ten seconds after it was added.
        """
        uri = str(payload.get("uri") or "").strip()
        if not uri:
            return fail(400, "Give a webcam index, a stream URL, or a file path to test.")
        if pipeline is not None and uri in {str(u) for u in pipeline.running_uris()}:
            return fail(
                409,
                f"{uri} is already open by a running source. Stop that source "
                f"first, or test a different one.",
            )
        try:
            source_rules.check_reachable(source_rules.kind_for_uri(uri), uri)
        except ValueError as exc:
            return fail(400, str(exc))
        return probe.test_source(uri)

    @app.get("/api/files")
    def files():
        """Video and image files already on disk, for the "select a file" flow.

        The real footage directory, not a picker dialog: the app is served to a
        browser and the browser cannot see the machine's filesystem.
        """
        roots = [config.ROOT / "footage", config.uploads_dir()]
        conn = db.connect()
        try:
            in_use = {row["uri"] for row in conn.execute("SELECT uri FROM sources")}
        finally:
            conn.close()

        seen = {}
        for root in roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                suffix = path.suffix.lower()
                if suffix in source_rules.VIDEO_SUFFIXES:
                    kind = "file"
                elif suffix in source_rules.IMAGE_SUFFIXES:
                    kind = "image"
                else:
                    continue
                try:
                    uri = path.relative_to(config.ROOT).as_posix()
                except ValueError:
                    uri = path.as_posix()
                seen[uri] = {
                    "uri": uri,
                    "name": path.name,
                    "kind": kind,
                    "bytes": path.stat().st_size,
                    "in_use": uri in in_use,
                }
                if len(seen) >= 500:
                    break
        return sorted(seen.values(), key=lambda f: f["uri"])

    @app.post("/api/uploads")
    async def upload(file: UploadFile = File(...)):
        """Take a video or an image and put it where a source can point at it.

        Streamed to disk in chunks. Reading a 2 GB clip into memory to write it
        back out is how a single upload takes the server down.
        """
        name = source_rules.safe_filename(file.filename)
        suffix = Path(name).suffix.lower()
        if suffix in source_rules.VIDEO_SUFFIXES:
            kind = "file"
        elif suffix in source_rules.IMAGE_SUFFIXES:
            kind = "image"
        else:
            allowed = ", ".join(
                sorted(source_rules.VIDEO_SUFFIXES | source_rules.IMAGE_SUFFIXES)
            )
            return fail(
                415,
                f"{name or 'That file'} is not a video or an image this app reads. "
                f"Accepted: {allowed}.",
            )

        target = config.uploads_dir()
        target.mkdir(parents=True, exist_ok=True)
        stem, ext = Path(name).stem, Path(name).suffix
        destination = target / name
        n = 2
        while destination.exists():
            destination = target / f"{stem}_{n}{ext}"
            n += 1

        written = 0
        try:
            with destination.open("wb") as out:
                while True:
                    chunk = await file.read(UPLOAD_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_UPLOAD_BYTES:
                        raise ValueError(
                            f"{name} is larger than the "
                            f"{MAX_UPLOAD_BYTES // (1024 ** 3)} GB upload limit. "
                            f"Trim the clip, or point a source at it on disk instead."
                        )
                    out.write(chunk)
        except ValueError as exc:
            destination.unlink(missing_ok=True)
            return fail(413, str(exc))
        except OSError as exc:
            destination.unlink(missing_ok=True)
            return fail(500, f"Could not save {name}: {exc}")
        finally:
            await file.close()

        # Relative to the project root when it can be -- that is the form the
        # worker resolves and the form every seeded source already uses. An
        # uploads directory configured outside the root still works; it just
        # stores an absolute path.
        try:
            uri = destination.relative_to(config.ROOT).as_posix()
        except ValueError:
            uri = destination.as_posix()
        print(f"[upload] {uri} ({written} bytes)")
        return {"uri": uri, "kind": kind, "name": destination.name, "bytes": written}

    @app.post("/api/sources", status_code=201)
    def create_source(payload: dict = Body(...)):
        """Add a source. Adding one starts its worker."""
        def insert(conn):
            taken = {r["source_id"] for r in conn.execute("SELECT source_id FROM sources")}
            row = source_rules.clean(payload, taken_ids=taken)
            conn.execute(
                f"INSERT INTO sources ({', '.join(SOURCE_COLUMNS)}) "
                f"VALUES ({', '.join(':' + c for c in SOURCE_COLUMNS)})",
                {c: row.get(c) for c in SOURCE_COLUMNS},
            )
            conn.commit()
            return read_source(conn, row["source_id"])

        try:
            row = write(insert)
        except ValueError as exc:
            return fail(400, str(exc))
        except sqlite3.Error as exc:
            return fail(500, f"Could not save the source: {exc}")

        publish_source(row)
        started = False
        message = "Saved. Nothing is processing it yet."
        if payload.get("start", True) and pipeline is not None:
            started, message = pipeline.start_source(row["source_id"])
        return {"source": row, "started": started, "message": message}

    @app.patch("/api/sources/{source_id}")
    def update_source(source_id: str, payload: dict = Body(...)):
        """Edit a source: rename it, place it on the map, retune its frame_skip.

        A change to what the worker reads -- uri, frame_skip, start_time -- only
        reaches a running worker when it is restarted, and the response says so
        rather than pretending it took effect.
        """
        def update(conn):
            existing = read_source(conn, source_id)
            if existing is None:
                raise LookupError(source_id)
            fields = {k: v for k, v in payload.items() if k in EDITABLE}
            if not fields:
                raise ValueError(
                    f"Nothing to change. Editable fields are: {', '.join(EDITABLE)}."
                )
            row = source_rules.clean(fields, existing=existing)
            conn.execute(
                "UPDATE sources SET name = :name, kind = :kind, uri = :uri, "
                "lat = :lat, lon = :lon, heading_deg = :heading_deg, "
                "frame_skip = :frame_skip, start_time = :start_time "
                "WHERE source_id = :source_id",
                {k: row.get(k) for k in
                 ("name", "kind", "uri", "lat", "lon", "heading_deg",
                  "frame_skip", "start_time", "source_id")},
            )
            conn.commit()
            return read_source(conn, source_id)

        try:
            row = write(update)
        except LookupError:
            return fail(404, f"There is no source called {source_id}.")
        except ValueError as exc:
            return fail(400, str(exc))

        publish_source(row)
        restart_needed = (
            pipeline is not None
            and pipeline.is_running(source_id)
            and any(k in payload for k in ("uri", "frame_skip", "start_time"))
        )
        return {
            "source": row,
            "restart_needed": restart_needed,
            "message": (
                "Saved. Restart this source for the change to reach its worker."
                if restart_needed else "Saved."
            ),
        }

    @app.delete("/api/sources/{source_id}")
    def delete_source(source_id: str, delete_sightings: bool = False):
        """Remove a source. Its worker stops first.

        Evidence is not thrown away silently: a source with sightings is refused
        until the request says explicitly that they go too.
        """
        conn = db.connect()
        try:
            existing = read_source(conn, source_id)
            count = conn.execute(
                "SELECT COUNT(*) FROM sightings WHERE source_id = ?", (source_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        if existing is None:
            return fail(404, f"There is no source called {source_id}.")
        if count and not delete_sightings:
            return fail(
                409,
                f"{existing['name']} has {count} sighting"
                f"{'' if count == 1 else 's'} recorded against it. Deleting the "
                f"source deletes them too -- confirm to go ahead.",
            )

        if pipeline is not None:
            pipeline.stop_source(source_id)

        def remove(conn):
            if delete_sightings:
                conn.execute("DELETE FROM sightings WHERE source_id = ?", (source_id,))
            conn.execute("DELETE FROM sources WHERE source_id = ?", (source_id,))
            conn.commit()

        try:
            write(remove)
        except sqlite3.Error as exc:
            return fail(500, f"Could not delete {source_id}: {exc}")

        hub.publish({"type": "source_removed", "source_id": source_id})
        return {
            "deleted": source_id,
            "sightings_deleted": count if delete_sightings else 0,
        }

    @app.post("/api/sources/{source_id}/start")
    def start_source(source_id: str):
        if pipeline is None:
            return fail(503, "No pipeline is running, so nothing can be started.")

        def reset(conn):
            row = read_source(conn, source_id)
            if row is None:
                raise LookupError(source_id)
            # A `done` file is not restarted by autostart, because that would
            # duplicate its sightings. Asking for it explicitly is a different
            # thing, and the row has to leave `done` for the status to be true.
            conn.execute(
                "UPDATE sources SET status = 'idle', error = NULL, progress = NULL "
                "WHERE source_id = ?",
                (source_id,),
            )
            conn.commit()
            return read_source(conn, source_id)

        try:
            row = write(reset)
        except LookupError:
            return fail(404, f"There is no source called {source_id}.")

        publish_source(row)
        started, message = pipeline.start_source(source_id)
        if not started:
            return fail(409, message)
        return {"source_id": source_id, "started": True, "message": message}

    @app.post("/api/sources/{source_id}/stop")
    def stop_source(source_id: str):
        if pipeline is None:
            return fail(503, "No pipeline is running, so nothing can be stopped.")
        stopped, message = pipeline.stop_source(source_id)
        if not stopped:
            return fail(409, message)
        # The worker reports `idle` on a requested stop, but the row is only
        # written when its final message drains. Read it back rather than
        # guessing what it says.
        row = write(lambda conn: read_source(conn, source_id))
        if row is not None:
            publish_source(row)
        return {"source_id": source_id, "stopped": True, "message": message}

    @app.get("/api/sources/{source_id}/stream.mjpg")
    async def stream(source_id: str):
        """The camera wall: annotated frames from a running worker.

        These are the frames the worker already decoded, drawn on and published.
        This endpoint never opens the source -- doing so would double the GPU
        cost of every stream and, for a webcam, simply fail, because the worker
        already holds the device.
        """
        if pipeline is None:
            return fail(503, "No pipeline is running, so there are no live frames.")
        # A plain read on its own connection, not `write()`. This handler is a
        # coroutine, and `write()` blocks the calling thread until the writer
        # answers -- on the event loop that would stall every other request,
        # including the websocket, for as long as the writer is busy.
        conn = db.connect()
        try:
            row = read_source(conn, source_id)
        finally:
            conn.close()
        if row is None:
            return fail(404, f"There is no source called {source_id}.")
        if not pipeline.is_running(source_id):
            return fail(
                409,
                f"{row['name']} is not running, so it has no live frames. "
                f"Start it to see its feed.",
            )

        pipeline.add_viewer(source_id)

        async def frames():
            last_seq = 0
            last_sent = 0.0
            started = time.monotonic()
            last_new = None
            try:
                while True:
                    shot = pipeline.latest_preview(source_id)
                    now = time.monotonic()

                    if shot is not None and shot["seq"] != last_seq:
                        last_seq = shot["seq"]
                        last_new = now
                    elif shot is not None and now - last_sent < STREAM_REPEAT_AFTER_SEC:
                        shot = None  # nothing new yet, and not time to repeat

                    if shot is not None:
                        last_sent = now
                        jpeg = shot["jpeg"]
                        yield (
                            f"--{MJPEG_BOUNDARY}\r\n"
                            f"Content-Type: image/jpeg\r\n"
                            f"Content-Length: {len(jpeg)}\r\n\r\n"
                        ).encode("ascii") + jpeg + b"\r\n"

                    if last_new is None:
                        if now - started > STREAM_FIRST_FRAME_SEC:
                            break
                    elif now - last_new > STREAM_STALL_SEC:
                        break
                    if not pipeline.is_running(source_id) and last_new is not None:
                        break
                    await asyncio.sleep(STREAM_POLL_SEC)
            finally:
                # Runs on client disconnect too: an async generator is closed
                # when the response is cancelled, which is exactly why this is
                # async rather than a thread the server cannot interrupt.
                pipeline.drop_viewer(source_id)

        return StreamingResponse(
            frames(),
            media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
            headers={"Cache-Control": "no-store, no-cache", "Pragma": "no-cache"},
        )

    @app.websocket("/api/ws")
    async def live(websocket: WebSocket):
        """Committed rows, pushed as they land.

        The client still loads /api/sightings first. This carries what happens
        after that, and a reconnect reloads rather than replaying: the database
        is the record, this is the notification.
        """
        await websocket.accept()
        queue = hub.register()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=PING_EVERY_SEC)
                except asyncio.TimeoutError:
                    # Also how a socket whose browser is gone gets noticed: the
                    # send raises and we fall out of the loop.
                    event = {"type": "ping"}
                await websocket.send_json(event)
        except (WebSocketDisconnect, RuntimeError, ConnectionError):
            pass
        finally:
            hub.unregister(queue)

    # ---------------------------------------------------------------- analyze

    # Standalone by design. Nothing in this section touches the sources table or
    # the writer, and no analysis ever becomes a sighting -- see the module
    # docstring in app/analyze.py for why that separation is load-bearing rather
    # than tidy. The screen works with zero cameras configured because there is
    # nothing here for a camera to configure.

    @app.post("/api/analyze", status_code=202)
    def start_analysis(payload: dict = Body(...)):
        """Queue one file for analysis.

        202 rather than 201: a job is accepted here and finished later, and a
        video takes as long as it takes. The client polls the job.
        """
        frame_skip = payload.get("frame_skip")
        try:
            frame_skip = None if frame_skip is None else int(frame_skip)
        except (TypeError, ValueError):
            return fail(400, "frame_skip must be a whole number of frames.")
        if frame_skip is not None and not 1 <= frame_skip <= 60:
            return fail(400, "frame_skip must be between 1 and 60.")

        job, error = jobs.submit(
            payload.get("uri"), frame_skip=frame_skip, name=payload.get("name")
        )
        if error is not None:
            return fail(400, error)
        return job

    @app.get("/api/analyze")
    def list_analyses(limit: int = Query(25, ge=1, le=100)):
        return jobs.list(limit)

    @app.get("/api/analyze/{job_id}")
    def get_analysis(job_id: str):
        """One job, carrying its full result once it has one.

        The result is only attached when the job is done, so a client polling a
        running job gets the small document every time and the large one once.
        """
        job = jobs.view(job_id)
        if job is None:
            return fail(404, f"There is no analysis job {job_id}.")
        document = jobs.result(job_id)
        return {**job, "result": document}

    @app.post("/api/analyze/{job_id}/cancel")
    def cancel_analysis(job_id: str):
        ok, error = jobs.cancel(job_id)
        if not ok:
            return fail(409, error)
        return jobs.view(job_id)

    @app.delete("/api/analyze/{job_id}", status_code=204)
    def delete_analysis(job_id: str):
        if not jobs.delete(job_id):
            return fail(404, f"There is no analysis job {job_id}.")
        return Response(status_code=204)

    @app.get("/api/analyze/{job_id}/export.{fmt}")
    def export_analysis(job_id: str, fmt: str):
        """The detections as JSON or CSV, as a download.

        The whole result document for JSON -- media, parameters, per-frame boxes
        and vehicles -- because an export that drops the settings it was produced
        under cannot be checked later. CSV is the vehicles only: a per-frame box
        list is not a table and pretending it is helps nobody.
        """
        if fmt not in ("json", "csv"):
            return fail(404, "Export is available as json or csv.")
        job = jobs.view(job_id)
        if job is None:
            return fail(404, f"There is no analysis job {job_id}.")
        document = jobs.result(job_id)
        if document is None:
            return fail(
                409,
                f"That analysis has not finished (it is {job['status']}). There "
                f"is nothing to export yet.",
            )
        stem = Path(document.get("name") or job_id).stem or job_id
        filename = source_rules.safe_filename(f"{stem}_detections.{fmt}")
        headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
        if fmt == "csv":
            return Response(
                content=analysis.to_csv(document),
                media_type="text/csv; charset=utf-8",
                headers=headers,
            )
        return Response(
            content=json.dumps(document, indent=2),
            media_type="application/json",
            headers=headers,
        )

    # ------------------------------------------------------------- trace (P4d)

    # Both routes are read-only views over the P3 modules -- nothing here
    # decides anything the matcher does not already decide, and neither route
    # writes. The screen's whole promise is that a search is fuzzy and shows
    # its working, so both the score and how it matched travel with every row.

    @app.get("/api/search")
    def search(
        q: str = Query("", description="plate to look for, however badly read"),
        limit: int = Query(10, ge=1, le=50),
        min_score: float = Query(0.72, ge=0.0, le=1.0),
    ):
        """Ranked candidate plates for one query.

        A list, always, even when it holds one entry. Returning the single best
        row as though it were the answer is the mistake this whole screen exists
        to avoid: the match is fuzzy and the person searching has to be the one
        who decides which candidate is their vehicle.
        """
        query = (q or "").strip()
        if not query:
            return fail(400, "Type a plate to search for. Partial reads are fine.")

        conn = db.connect()
        try:
            results = matching.search(conn, query, limit=limit, min_score=min_score)
            # A miss is not always a miss. Similarity is normalised by the
            # longer string, so half a registration scores about 50% however
            # right it is -- typing MH15HY of MH15HY2237 lands at 0.67, under
            # the floor, and the screen would otherwise say "nothing matches"
            # about a vehicle sitting in the database. The best refused
            # candidate travels with the empty answer so the screen can name it
            # and offer to reach it, rather than the floor being a silent wall.
            closest = None
            if not results:
                near = matching.search(conn, query, limit=1, min_score=0.0)
                if near:
                    closest = {
                        "plate_text": near[0]["plate_text"],
                        "score": near[0]["score"],
                        "sighting_count": near[0]["sighting_count"],
                    }
            plated = conn.execute(
                "SELECT COUNT(*) FROM sightings WHERE plate_text IS NOT NULL"
            ).fetchone()[0]
        finally:
            conn.close()
        return {
            "query": query,
            "normalized": matching.normalize(query),
            "min_score": min_score,
            # What the search had to work with. An empty result over 0 plated
            # sightings is a different situation from an empty result over 400,
            # and the screen has to say which one it is.
            "searched": plated,
            "closest": closest,
            "results": results,
        }

    @app.get("/api/trajectory")
    def path_for_plate(
        plate: str = Query(""),
        min_score: float = Query(0.72, ge=0.0, le=1.0),
        limit: int = Query(500, ge=1, le=2000),
    ):
        """One vehicle's stops, in time order, with the leg to each.

        Gathered by the same fuzzy match, not by equality: the sightings that
        make up one journey are exactly the ones whose plate strings disagree.
        """
        wanted = (plate or "").strip()
        if not wanted:
            return fail(400, "Choose a plate to trace.")

        conn = db.connect()
        try:
            return trace.trajectory(conn, wanted, min_score=min_score, limit=limit)
        finally:
            conn.close()

    # ---------------------------------------------------------- insights (P4e)

    # One route, not five. Every panel on the Insights screen is a slice of the
    # same window, and five routes would let them answer for five slightly
    # different ones -- a source list fetched a second after the chart, with a
    # worker still writing, disagreeing with it by two rows. A shared time
    # filter has to be shared in the data, not only in the control.

    @app.get("/api/insights")
    def insights(
        start: str | None = Query(None, alias="from"),
        end: str | None = Query(None, alias="to"),
        bucket: int | None = Query(None, ge=1, le=2592000),
        min_score: float = Query(analytics.LINK_SCORE, ge=0.5, le=1.0),
    ):
        """Every panel, over one window of time.

        `from` and `to` are ISO-8601 and both optional: no window at all means
        everything there has ever been, which is what the screen opens on.
        """
        opened, error = analytics.parse_ts(start)
        if error:
            return fail(400, error)
        closed, error = analytics.parse_ts(end)
        if error:
            return fail(400, error)
        if opened and closed and opened > closed:
            return fail(
                400,
                "The window ends before it starts. Swap the two times, or clear "
                "one of them.",
            )

        conn = db.connect()
        try:
            return analytics.insights(
                conn, start=opened, end=closed, bucket=bucket, min_score=min_score
            )
        finally:
            conn.close()

    # ---------------------------------------------------------------- statics

    # Evidence crops. sightings.crop_path is stored relative to the project
    # root ('crops/evidence/...'), so mounting the crops directory here means
    # the UI can use the stored path as a URL with a leading slash and nothing
    # else. StaticFiles handles the traversal check.
    crops_root = config.ROOT / "crops"
    crops_root.mkdir(parents=True, exist_ok=True)
    app.mount("/crops", StaticFiles(directory=crops_root), name="crops")

    # Analyze job frames and crops. Under /media rather than /analyze because
    # /analyze is the frontend's own route and a mount there would answer 404
    # for /analyze/<job_id> instead of letting the SPA fallback serve it.
    analyze_root = config.analyze_dir()
    analyze_root.mkdir(parents=True, exist_ok=True)
    app.mount(
        analysis.MEDIA_PREFIX, StaticFiles(directory=analyze_root), name="analyze"
    )

    # ------------------------------------------------------------ spa (last)

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        """Serve the built frontend, falling back to index.html for deep links.

        Without the fallback, /trace/MH12AB1234 404s on reload even though the
        route exists inside the app.
        """
        if full_path.startswith("api/"):
            return JSONResponse(
                status_code=404, content={"detail": f"No API route /{full_path}"}
            )

        index = DIST / "index.html"
        if not index.exists():
            return HTMLResponse(BUILD_ME.format(dist=DIST), status_code=503)

        if full_path:
            candidate = (DIST / full_path).resolve()
            root = DIST.resolve()
            if candidate.is_file() and root in candidate.parents:
                return FileResponse(candidate)
        return FileResponse(index)

    return app
