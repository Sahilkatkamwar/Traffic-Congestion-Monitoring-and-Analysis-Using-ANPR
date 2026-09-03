"""Standalone analysis of one image or video: the Analyze screen's engine.

This is the same pipeline a source worker runs -- the same detector, the same
ByteTrack settings, the same stitcher built from the same config keys, the same
plate detection, sampling, voting and grammar correction -- pointed at a file
that nobody has to add as a source. It has to work with zero cameras
configured, and it does: nothing in here reads or writes the `sources` table.

**It never writes a sighting.** An Analyze result is not a sighting and must not
be mistaken for one. A sighting is a vehicle seen by a placed camera at an
absolute time; an analysis is what the models say about a file. The file has no
location, and it has no start time unless somebody claims one, so the results
carry an OFFSET into the media in seconds and no absolute timestamp at all.
Writing these into `sightings` would put rows with no source and no real clock
into every trajectory and every travel-time figure in the app.

The consequence, stated so it is not discovered later: a clip analysed here and
the same clip ingested as a source produce the same detections and the same
vehicles, but only the source's rows are in the database. Analysing is not a
way to import.

Two processes, for the reason probe.py has two: the analysis loads three models
and holds a capture open, and neither belongs in the API process. The child is
spawned -- run_analysis is module-level, every argument is picklable, and the
child builds its own detector, its own reader and its own capture. Nothing open
crosses the boundary.

One job runs at a time. The card is 6 GB and has to hold three streams; a
fourth model set loaded because two people pressed Analyze is how that budget
is spent twice. Later submissions queue and say so.
"""

import csv
import io
import json
import multiprocessing as mp
import queue as queue_mod
import shutil
import threading
import time
import traceback
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import config, grammar, sources as source_rules

# Offsets into the media are what an analysis actually knows. The tracks and the
# stitcher are written against datetimes, so they get one -- counted from the
# epoch, which makes `seconds since the start of the file` the same number in
# both directions and keeps the shared code identical to the worker's.
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# How long the parent waits for a child that has been asked to stop.
CANCEL_GRACE_SEC = 10.0
# Progress is pushed no faster than this. A 4K clip processes a few frames a
# second and a message per frame is noise.
PROGRESS_EVERY_SEC = 0.4

# Jobs kept in memory. Old ones are evicted oldest-first with their directories,
# because the frames of a 200s clip are 35 MB apiece and nothing here is
# persistent state -- a restart loses every job, which is why the directory is
# also swept at startup.
MAX_JOBS = 12


def _seconds(ts):
    """A track timestamp back to seconds from the start of the media."""
    return round((ts - EPOCH).total_seconds(), 3)


# --------------------------------------------------------------------- child


def _imread(path):
    """Read an image without cv2.imread's ASCII-only path handling on Windows."""
    import cv2
    import numpy as np

    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def _write_frame(path, image, max_width):
    """One decoded frame, scaled down, as a jpeg. Returns its (w, h) scale."""
    import cv2

    height, width = image.shape[:2]
    if width > max_width:
        scale = max_width / width
        image = cv2.resize(
            image, (max_width, max(1, int(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    if not ok:
        return None
    path.write_bytes(buf.tobytes())
    return image.shape[1], image.shape[0]


def _normalised(box, width, height):
    """A frame-pixel box as fractions of the frame.

    The UI draws the boxes itself, over a scaled-down frame, so what it needs is
    a fraction and not a pixel. Burning the boxes into the jpeg was the other
    option and is worse twice over: it fixes the annotation at whatever size the
    frame was written at, and it makes every box unclickable -- and this screen's
    whole promise is that every detection can be opened.
    """
    x1, y1, x2, y2 = box
    return [
        round(x1 / width, 5), round(y1 / height, 5),
        round(x2 / width, 5), round(y2 / height, 5),
    ]


def _plate_fields(track):
    """The plate half of one analysed vehicle, in the sightings vocabulary.

    Same field names and same treatment as app/worker.py:_sighting, so that a
    person comparing an analysis with a row is comparing like with like -- the
    raw read, the grammar-corrected read, and the candidates left exactly as the
    vote produced them.
    """
    plate = track.plate_result or {}
    raw = plate.get("plate_raw")
    text = grammar.apply(raw)
    return {
        "plate_raw": raw,
        "plate_text": text,
        "plate_conf": plate.get("plate_conf"),
        "plate_candidates": plate.get("candidates"),
        "frames_voted": plate.get("frames_voted", 0),
        "plate_valid": bool(text) and grammar.is_valid(text),
        "plate_state": grammar.state_name(text) if text else None,
    }


def _finalise_analysis(track, dirs, plate_opts, classifier):
    """What worker._finalise does, writing into the job's directory instead.

    Deliberately not a call into the worker's version. That one writes the
    evidence crop into crops/evidence and a training copy into crops/unsorted,
    and an analysis must produce neither: nothing here is a row's evidence, and
    a file dropped on this screen is not a harvest from a camera this project
    owns. The vote, the embedding and the classifier call are the same.
    """
    from app.ocr import vote
    from app.worker import _write_image
    from app import stitch

    if track.best_crop is not None:
        if track.embedding is None:
            track.embedding = stitch.embedding(track.best_crop)
        if classifier is not None:
            label, _conf = classifier.classify(track.best_crop)
            if label is not None:
                track.classified_type = label
        track.crop_path = _write_image(
            dirs["crops"] / f"{track.track_id:06d}.jpg", track.best_crop
        )
    if track.best_plate_crop is not None and track.plate_crop_path is None:
        track.plate_crop_path = _write_image(
            dirs["plates"] / f"{track.track_id:06d}.jpg", track.best_plate_crop
        )
    if track.plate_reads:
        track.plate_result = vote(
            [read for _, read in track.plate_reads], **plate_opts
        )


def _vehicle(track, url_for):
    """One finished track as the screen's vehicle record."""
    return {
        "track_id": track.track_id,
        "vehicle_type": track.vehicle_type(),
        "hits": track.hits,
        "first_seconds": _seconds(track.first_ts),
        "last_seconds": _seconds(track.last_ts),
        "crop": url_for(track.crop_path),
        "plate_crop": url_for(track.plate_crop_path),
        **_plate_fields(track),
    }


def run_analysis(job, out, stop_event):
    """Spawn target: analyse one file and write its result document.

    Everything about the reading of the media is the worker's, because it has to
    be: a screen that reported different numbers from the pipeline would be
    worse than no screen. What differs is the destination -- a job directory and
    a json file rather than the writer queue -- and the clock, which is an offset
    into the file rather than an absolute time, because a file has no location
    and no start time to be absolute about.
    """
    job_id = job["job_id"]
    out_dir = Path(job["out_dir"])
    dirs = {
        "frames": out_dir / "frames",
        "crops": out_dir / "crops",
        "plates": out_dir / "plates",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)

    def url_for(rel):
        """A path under the job directory as the URL the frontend fetches."""
        if not rel:
            return None
        try:
            tail = Path(rel).resolve().relative_to(out_dir.resolve()).as_posix()
        except ValueError:
            return None
        return f"/analyze/{job_id}/{tail}"

    def say(**message):
        try:
            out.put({"job_id": job_id, **message})
        except Exception:  # noqa: BLE001 - a full queue must not kill the run
            pass

    started = time.monotonic()
    try:
        import cv2

        from app import stitch
        from app.classify import VehicleClassifier
        from app.detect import VehicleDetector
        from app.ocr import PlateReader
        from app.worker import (
            _crop,
            _open_capture,
            _probe,
            _read_plates,
            _Track,
        )

        frame_skip = max(1, int(job.get("frame_skip") or 1))
        max_frames = int(config.default("analyze_max_frames", 600))
        frame_width = int(config.default("analyze_frame_width", 720))
        pad = float(config.default("crop_pad", 0.04))
        timeout_frames = int(config.default("track_timeout_frames", 45))
        min_hits = int(config.default("min_track_hits", 2))
        plate_samples = int(config.default("plate_samples", 5))
        plate_every = int(config.default("plate_every", 2))
        plate_max_attempts = int(config.default("plate_max_attempts", 30))
        plate_opts = {
            "top_k": int(config.default("plate_candidates_k", 5)),
            "min_chars": int(config.default("min_plate_chars", 4)),
        }
        plate_pad = (
            float(config.default("plate_crop_pad_x", 0.10)),
            float(config.default("plate_crop_pad_y", 0.30)),
        )
        # The worker's stitcher, from the worker's settings keys, built in the
        # one place both of them read. See app/stitch.py:from_config.
        stitcher = stitch.TrackStitcher.from_config()
        keep_footprint = bool(stitcher.footprint)

        say(type="stage", stage="loading", detail="Loading the models")
        detector = VehicleDetector()

        # A plate model that will not load costs the analysis its reads, not its
        # detections -- the same degradation rule the worker follows, and the
        # reason reaches the screen instead of only stdout.
        reader = None
        warning = None
        try:
            reader = PlateReader()
            plate_opts["pad_char"] = reader.pad_char
        except Exception as exc:  # noqa: BLE001 - degrade, do not die
            warning = (
                f"Plate reading is off: {type(exc).__name__}: {exc}. Vehicles are "
                f"still detected and typed."
            )
            print(f"[analyze {job_id}] {warning}")

        classifier = None
        try:
            classifier = VehicleClassifier.load()
        except Exception as exc:  # noqa: BLE001 - degrade, do not die
            print(f"[analyze {job_id}] classifier off: {type(exc).__name__}: {exc}")

        target = source_rules.resolve_uri(job["uri"])
        is_image = job["kind"] == "image"

        frames_out = []
        tracks = {}
        retired = {}
        revoked = {}
        aliases = {}
        finished = []
        raw_detections = 0
        ids_seen = set()
        stitched = 0
        revocations = 0
        processed = 0
        media = {}
        stride = 1

        def emit(track):
            """A track the tracker has stopped reporting, resolved and kept."""
            nonlocal stitched
            _finalise_analysis(track, dirs, plate_opts, classifier)
            twin = stitcher.plate_twin(track, retired)
            if twin is None:
                twin = stitcher.reid_twin(track, retired)
            if twin is None:
                twin = stitcher.footprint_twin(track, retired)
            if twin is not None:
                aliases[track.track_id] = twin
                stitched += 1
                track.track_id = twin
            retired[track.track_id] = track
            finished.append(track)

        if is_image:
            say(type="stage", stage="running", detail="Reading the image")
            frame = _imread(target)
            if frame is None:
                raise RuntimeError(
                    f"{Path(job['uri']).name} could not be decoded as an image. "
                    f"Check the file is not truncated and is a jpg, png, bmp or "
                    f"webp."
                )
            height, width = frame.shape[:2]
            media = {
                "width": int(width), "height": int(height),
                "fps": None, "frames": 1, "duration_sec": None,
            }
            ts = EPOCH
            processed = 1
            detections = detector.track(frame)
            raw_detections = len(detections)
            pending = []
            for detection in detections:
                ids_seen.add(detection["track_id"])
                track = _Track(detection["track_id"], ts, 1, keep_footprint)
                tracks[detection["track_id"]] = track
                crop, origin, _near = track.update(detection, frame, ts, 1, pad)
                if reader is not None and crop is not None:
                    pending.append((track, crop, origin))
            if pending:
                _read_plates(reader, pending, frame, 1, plate_samples, plate_pad)
            # min_track_hits does not apply to a still. A vehicle in a single
            # frame is seen exactly once by definition, and a rule written to
            # throw away one-frame noise in a video would throw away every
            # detection in an image.
            for track in list(tracks.values()):
                emit(track)
            size = _write_frame(dirs["frames"] / "000000.jpg", frame, frame_width)
            frames_out.append(
                {
                    "i": 0,
                    "frame": 0,
                    "seconds": 0.0,
                    "image": f"/analyze/{job_id}/frames/000000.jpg",
                    "width": size[0] if size else width,
                    "height": size[1] if size else height,
                    "boxes": [
                        {
                            "track_id": d["track_id"],
                            "box": _normalised(d["box"], width, height),
                            "vehicle_type": d["vehicle_type"],
                            "conf": round(d["conf"], 4),
                        }
                        for d in detections
                    ],
                }
            )
            say(type="progress", progress=1.0)
        else:
            cap = None
            try:
                cap = _open_capture(target)
                if cap is None:
                    raise RuntimeError(
                        f"Could not open {job['uri']!r}. Check the file exists and "
                        f"is a video this machine can decode."
                    )
                total_frames, probed_fps = _probe(cap)
                if total_frames < 1:
                    raise RuntimeError(
                        f"{Path(job['uri']).name} reports no frames. Analyze reads "
                        f"files; add a camera on the Sources screen for a live "
                        f"stream."
                    )
                fps = probed_fps or 25.0
                # Annotated frames are bounded; DETECTIONS never are. Past the
                # cap the pictures are strided and the job says by how much, so
                # the scrubber is honest about what it can show.
                expected = max(1, -(-total_frames // frame_skip))
                stride = max(1, -(-expected // max_frames))
                media = {
                    "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
                    "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0),
                    "fps": round(fps, 3),
                    "frames": int(total_frames),
                    "duration_sec": round(total_frames / fps, 3) if fps else None,
                }
                say(type="stage", stage="running", detail="Reading the video",
                    media=media)

                index = 0
                last_said = 0.0
                cancelled = False
                while True:
                    if stop_event.is_set():
                        cancelled = True
                        break
                    if not cap.grab():
                        break
                    here = index
                    index += 1
                    if here % frame_skip:
                        continue
                    ok, frame = cap.retrieve()
                    if not ok or frame is None:
                        continue

                    # The recorded timestamp rule, on an epoch base: an analysis
                    # knows an offset into the file and claims nothing more.
                    ts = EPOCH + timedelta(seconds=here / fps)
                    processed += 1
                    height, width = frame.shape[:2]

                    detections = detector.track(frame)
                    raw_detections += len(detections)
                    for raw_tid, owner_tid, _together, _a, _b in (
                        stitcher.revoke_aliases(detections, aliases, tracks, retired)
                    ):
                        del aliases[raw_tid]
                        revoked[raw_tid] = processed
                        revocations += 1
                    seen_this_frame = {
                        aliases.get(d["track_id"], d["track_id"]) for d in detections
                    }
                    pending = []
                    drawn = []
                    for detection in detections:
                        raw_tid = detection["track_id"]
                        ids_seen.add(raw_tid)
                        tid = aliases.get(raw_tid, raw_tid)
                        track = tracks.get(tid) or retired.pop(tid, None)
                        if track is None:
                            candidate, _area, _origin = _crop(
                                frame, detection["box"], pad
                            )
                            owner = None
                            if raw_tid not in revoked:
                                owner = stitcher.adopt(
                                    detection["box"], candidate, processed,
                                    {**tracks, **retired}, seen_this_frame,
                                )
                            if owner is not None:
                                aliases[raw_tid] = owner
                                stitched += 1
                                tid = owner
                                track = tracks.get(tid) or retired.pop(tid, None)
                                seen_this_frame.add(owner)
                            if track is None:
                                track = _Track(tid, ts, processed, keep_footprint)
                        tracks[tid] = track
                        crop, origin, near_best = track.update(
                            detection, frame, ts, processed, pad
                        )
                        if (
                            reader is not None
                            and crop is not None
                            and track.wants_plate(
                                processed, plate_every, near_best, plate_max_attempts
                            )
                        ):
                            pending.append((track, crop, origin))
                        drawn.append((tid, detection))

                    if pending:
                        _read_plates(
                            reader, pending, frame, processed, plate_samples,
                            plate_pad,
                        )

                    if (processed - 1) % stride == 0:
                        n = len(frames_out)
                        name = f"{n:06d}.jpg"
                        size = _write_frame(
                            dirs["frames"] / name, frame, frame_width
                        )
                        frames_out.append(
                            {
                                "i": n,
                                "frame": here,
                                "seconds": round(here / fps, 3),
                                "image": f"/analyze/{job_id}/frames/{name}",
                                "width": size[0] if size else width,
                                "height": size[1] if size else height,
                                "boxes": [
                                    {
                                        "track_id": tid,
                                        "box": _normalised(d["box"], width, height),
                                        "vehicle_type": d["vehicle_type"],
                                        "conf": round(d["conf"], 4),
                                        # What had actually been read by this
                                        # point, never the final vote: the vote
                                        # runs when the track ends, and showing
                                        # it here would claim the pipeline knew
                                        # something it did not yet know.
                                        "read": (
                                            tracks[tid].live_read(
                                                plate_opts.get("pad_char", "_")
                                            )
                                            if tid in tracks else None
                                        ),
                                    }
                                    for tid, d in drawn
                                ],
                            }
                        )

                    gone = [
                        tid for tid, t in tracks.items()
                        if processed - t.last_index > timeout_frames
                    ]
                    for tid in gone:
                        track = tracks.pop(tid)
                        if track.hits < min_hits:
                            continue
                        emit(track)

                    now = time.monotonic()
                    if now - last_said >= PROGRESS_EVERY_SEC:
                        last_said = now
                        say(
                            type="progress",
                            progress=min(1.0, index / total_frames),
                            vehicles=len(finished),
                        )

                # Everything still in frame at the end of the file has still
                # been seen. Dropping it would lose every vehicle that was
                # present when the clip stopped.
                for tid in list(tracks):
                    track = tracks.pop(tid)
                    if track.hits >= min_hits:
                        emit(track)
                if cancelled:
                    say(type="cancelled")
                    return
            finally:
                if cap is not None:
                    # Windows locks a video file held by a dead handle.
                    cap.release()

        # A track that was merged into another is not a vehicle of its own. The
        # merge already moved its evidence onto the surviving row, exactly as
        # the worker's update path does.
        vehicles = {}
        for track in finished:
            vehicles[track.track_id] = track
        rows = [_vehicle(t, url_for) for t in vehicles.values()]
        rows.sort(key=lambda v: (v["first_seconds"], v["track_id"]))

        result = {
            "job_id": job_id,
            "uri": job["uri"],
            "name": job.get("name") or Path(job["uri"]).name,
            "kind": job["kind"],
            "media": media,
            "params": {
                "frame_skip": frame_skip,
                "conf": config.default("conf"),
                "imgsz": config.default("imgsz"),
                "min_plate_width": config.default("min_plate_width"),
                "plate_conf": config.default("plate_conf"),
                "frame_stride": stride,
                "frame_width": frame_width,
            },
            "warning": warning,
            "counts": {
                "processed_frames": processed,
                "raw_detections": raw_detections,
                "tracker_ids": len(ids_seen),
                "stitches": stitched,
                "revocations": revocations,
                "vehicles": len(rows),
                "plates": sum(1 for v in rows if v["plate_text"]),
                "plate_crops": sum(1 for v in rows if v["plate_crop"]),
            },
            "frames": frames_out,
            "vehicles": rows,
            "elapsed_sec": round(time.monotonic() - started, 2),
        }
        (out_dir / "result.json").write_text(
            json.dumps(result), encoding="utf-8"
        )
        print(
            f"[analyze {job_id}] done: {processed} frames, {raw_detections} "
            f"detections, {len(rows)} vehicles, "
            f"{result['counts']['plates']} plate reads in "
            f"{result['elapsed_sec']}s"
        )
        say(type="done", counts=result["counts"], media=media,
            elapsed_sec=result["elapsed_sec"])
    except Exception as exc:  # noqa: BLE001 - the reason has to reach the UI
        traceback.print_exc()
        say(type="error", error=f"{type(exc).__name__}: {exc}")


# -------------------------------------------------------------------- parent


class AnalysisJobs:
    """Every Analyze job, and the one child process that runs them.

    In memory on purpose. A job is a question somebody asked about a file, not a
    record of anything the app observed, and it has no place in the frozen
    schema. A restart loses them, which is why the directory is swept on start.
    """

    def __init__(self):
        self.ctx = mp.get_context("spawn")
        self.jobs = {}
        self.order = deque()
        self.pending = deque()
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.thread = None
        self.stopping = False
        self.current = None        # job_id of the running child
        self.stop_event = None     # its stop event

    # ------------------------------------------------------------- lifecycle

    def start(self, sweep=True):
        if sweep:
            self._sweep()
        self.thread = threading.Thread(
            target=self._run_loop, name="analyze", daemon=True
        )
        self.thread.start()

    def _sweep(self):
        """Remove job directories left by a previous run.

        Jobs live in memory, so after a restart every directory under here is
        orphaned: there is no job to open it from and nothing will ever delete
        it. Sweeping is the only thing that keeps this from growing forever.
        """
        root = config.analyze_dir()
        if not root.exists():
            return
        removed = 0
        for path in root.iterdir():
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        if removed:
            print(f"[analyze] swept {removed} job director{'y' if removed == 1 else 'ies'} "
                  f"left by a previous run")

    def shutdown(self):
        self.stopping = True
        if self.stop_event is not None:
            self.stop_event.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join(CANCEL_GRACE_SEC + 5)

    # ---------------------------------------------------------------- public

    def submit(self, uri, frame_skip=None, name=None):
        """Queue one file for analysis and return its job."""
        text = str(uri or "").strip()
        if not text:
            return None, "Choose a file to analyze, or upload one."
        kind = source_rules.kind_for_uri(text)
        if kind not in ("file", "image"):
            return None, (
                f"Analyze reads files. {text} looks like a live source -- add it "
                f"on the Sources screen instead."
            )
        path = Path(source_rules.resolve_uri(text))
        if not path.exists():
            return None, f"There is no file at {text}."

        job_id = uuid.uuid4().hex[:12]
        job = {
            "job_id": job_id,
            "uri": text,
            "name": name or path.name,
            "kind": kind,
            "frame_skip": int(frame_skip or config.default("frame_skip", 3)),
            "status": "queued",
            "stage": None,
            "detail": None,
            "progress": 0.0,
            "error": None,
            "warning": None,
            "counts": None,
            "media": None,
            "created_ts": time.time(),
            "started_ts": None,
            "finished_ts": None,
            "queue_position": None,
        }
        with self.lock:
            self.jobs[job_id] = job
            self.order.append(job_id)
            self.pending.append(job_id)
            self._evict()
        self.wake.set()
        return self.view(job_id), None

    def view(self, job_id):
        """One job as the API returns it, with its queue position filled in."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            snapshot = dict(job)
            if job["status"] == "queued":
                try:
                    snapshot["queue_position"] = list(self.pending).index(job_id) + 1
                except ValueError:
                    snapshot["queue_position"] = None
        return snapshot

    def list(self, limit=25):
        with self.lock:
            ids = list(self.order)[-limit:][::-1]
        return [v for v in (self.view(i) for i in ids) if v is not None]

    def result(self, job_id):
        """The result document, or None if the job has not produced one."""
        job = self.view(job_id)
        if job is None or job["status"] != "done":
            return None
        path = config.analyze_dir() / job_id / "result.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def cancel(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return False, "That job no longer exists."
            if job["status"] in ("done", "error", "cancelled"):
                return False, f"That job has already finished ({job['status']})."
            if job_id in self.pending:
                self.pending.remove(job_id)
                job.update(status="cancelled", finished_ts=time.time(),
                           stage=None, detail=None)
                return True, None
            running = self.current == job_id
        if running and self.stop_event is not None:
            self.stop_event.set()
            return True, None
        return False, "That job could not be stopped."

    def delete(self, job_id):
        self.cancel(job_id)
        with self.lock:
            job = self.jobs.pop(job_id, None)
            if job_id in self.order:
                self.order.remove(job_id)
            if job_id in self.pending:
                self.pending.remove(job_id)
        if job is None:
            return False
        shutil.rmtree(config.analyze_dir() / job_id, ignore_errors=True)
        return True

    # --------------------------------------------------------------- internal

    def _evict(self):
        """Drop the oldest finished jobs once there are too many. Caller holds."""
        while len(self.order) > MAX_JOBS:
            for job_id in list(self.order):
                job = self.jobs.get(job_id)
                if job and job["status"] in ("done", "error", "cancelled"):
                    self.order.remove(job_id)
                    self.jobs.pop(job_id, None)
                    shutil.rmtree(
                        config.analyze_dir() / job_id, ignore_errors=True
                    )
                    break
            else:
                return

    def _run_loop(self):
        while not self.stopping:
            with self.lock:
                job_id = self.pending.popleft() if self.pending else None
            if job_id is None:
                self.wake.wait(0.5)
                self.wake.clear()
                continue
            with self.lock:
                job = self.jobs.get(job_id)
            if job is None or job["status"] == "cancelled":
                continue
            try:
                self._run_one(job)
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                traceback.print_exc()
                with self.lock:
                    job.update(
                        status="error", finished_ts=time.time(),
                        error=f"{type(exc).__name__}: {exc}",
                    )

    def _run_one(self, job):
        job_id = job["job_id"]
        out_dir = config.analyze_dir() / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        queue = self.ctx.Queue(maxsize=256)
        stop_event = self.ctx.Event()
        payload = {
            "job_id": job_id,
            "uri": job["uri"],
            "name": job["name"],
            "kind": job["kind"],
            "frame_skip": job["frame_skip"],
            "out_dir": str(out_dir),
        }
        process = self.ctx.Process(
            target=run_analysis, args=(payload, queue, stop_event), daemon=True
        )
        with self.lock:
            self.current = job_id
            self.stop_event = stop_event
            job.update(status="running", started_ts=time.time(), stage="starting",
                       detail="Starting", queue_position=None)
        process.start()
        print(f"[analyze {job_id}] {job['kind']} {job['uri']} (pid {process.pid})")

        terminal = None
        try:
            while True:
                try:
                    message = queue.get(timeout=0.5)
                except queue_mod.Empty:
                    if not process.is_alive():
                        break
                    continue
                kind = message.get("type")
                with self.lock:
                    if kind == "stage":
                        job["stage"] = message.get("stage")
                        job["detail"] = message.get("detail")
                        if message.get("media"):
                            job["media"] = message["media"]
                    elif kind == "progress":
                        job["progress"] = message.get("progress", job["progress"])
                    elif kind == "done":
                        terminal = "done"
                        job["counts"] = message.get("counts")
                        job["media"] = message.get("media") or job["media"]
                        job["progress"] = 1.0
                    elif kind == "error":
                        terminal = "error"
                        job["error"] = message.get("error")
                    elif kind == "cancelled":
                        terminal = "cancelled"
                if terminal is not None:
                    break
        finally:
            if terminal is None and process.is_alive():
                # Nothing terminal arrived, so either it is being cancelled or
                # it has stopped talking. Either way it does not get to keep the
                # GPU or the file handle.
                process.join(CANCEL_GRACE_SEC if stop_event.is_set() else 0.1)
                if process.is_alive():
                    process.terminate()
            process.join(5)
            queue.close()
            queue.join_thread()
            with self.lock:
                self.current = None
                self.stop_event = None

        if terminal is None:
            terminal = "cancelled" if stop_event.is_set() else "error"
        with self.lock:
            if terminal == "error" and not job["error"]:
                job["error"] = (
                    f"The analysis stopped without saying why (exit code "
                    f"{process.exitcode}). Check the server log."
                )
            job["status"] = terminal
            job["finished_ts"] = time.time()
            job["stage"] = None
            job["detail"] = None
        if terminal == "done":
            document = self.result(job_id)
            if document is not None:
                with self.lock:
                    job["warning"] = document.get("warning")


# --------------------------------------------------------------------- export

EXPORT_COLUMNS = (
    "track_id",
    "vehicle_type",
    "plate_text",
    "plate_raw",
    "plate_conf",
    "plate_valid",
    "plate_state",
    "frames_voted",
    "plate_candidates",
    "first_seconds",
    "last_seconds",
    "hits",
    "crop",
    "plate_crop",
)


def to_csv(document):
    """The vehicles of one result as CSV text.

    The candidates column is the same json the sightings table stores, so a
    spreadsheet and a row carry the same alternatives.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(EXPORT_COLUMNS)
    for vehicle in document.get("vehicles", []):
        row = []
        for column in EXPORT_COLUMNS:
            value = vehicle.get(column)
            if column == "plate_candidates" and value:
                value = json.dumps(value)
            row.append("" if value is None else value)
        writer.writerow(row)
    return buffer.getvalue()
