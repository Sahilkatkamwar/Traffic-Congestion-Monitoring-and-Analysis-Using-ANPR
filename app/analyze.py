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
is spent twice. Later submissions queue and say so -- as runs of their own,
each listed with its own progress, not as something the screen forgets about
while it waits.

P6 makes a run outlive the request that started it. `analyze_runs` records what
was analysed, how it went, and where the saved result document and thumbnail
are; the job directory holds the frames, the crops and that document. That is
the only thing this module puts in the database, it goes through the one writer
like every other write in the app, and it is still not a sighting -- see the
paragraph above, which P6 does not soften.
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
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import config, grammar, runs as run_store, sources as source_rules

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

# How many runs are kept. Past this the oldest FINISHED run is dropped with its
# directory, because the frames of a 200s clip are about 35 MB and a screen that
# never forgets fills the disk. A run still queued or running is never evicted;
# a run the user has not deleted is only evicted by this cap.
DEFAULT_KEEP = 50

# The run list shows a still of each run. Small on purpose: fifty of these load
# at once when the screen opens.
THUMBNAIL_NAME = "thumb.jpg"
THUMBNAIL_WIDTH = 320

# A running job's progress is persisted no faster than this. The screen polls
# the live value, which moves at PROGRESS_EVERY_SEC; the row only has to be
# close enough that a run interrupted by a crash does not look like it never
# started.
PERSIST_EVERY_SEC = 2.0

# Where a job's frames and crops are served from. NOT `/analyze`: that is the
# frontend's own route, and a StaticFiles mount there would swallow
# /analyze/<job_id> -- a deep link into the screen -- and answer 404 instead of
# letting it fall through to the SPA.
MEDIA_PREFIX = "/media/analyze"


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


def _write_thumbnail(out_dir, image):
    """A small still of the first annotated frame, for the run list.

    Written by the child because the child is the only thing holding pixels, and
    written from the FIRST frame rather than a representative one: the list has
    to show something the moment a long video starts, and "representative"
    cannot be known until the run is over.
    """
    path = out_dir / THUMBNAIL_NAME
    if _write_frame(path, image, THUMBNAIL_WIDTH) is None:
        return None
    return path


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
        return f"{MEDIA_PREFIX}/{job_id}/{tail}"

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
            RETIRE_TTL_FRAMES,
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
            """A track the tracker has stopped reporting, resolved and kept.

            The three twin passes run in the worker's order and for the worker's
            reasons: a shared registration is the strongest claim, a clean
            re-acquisition is next, and the footprint rule only ever sees pairs
            the first two declined.

            A track can be emitted twice -- the tracker re-activates an id after
            it timed out -- and that is an update, not a second vehicle. The
            worker expresses that by writing to the same row; here the finished
            tracks are collapsed by track_id at the end, which is the same
            statement.
            """
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
            # The crops are on disk now. Holding the pixels for every finished
            # track is how a 200s clip runs the process out of memory; the reads
            # stay, because a re-activated track keeps voting on them.
            track.best_crop = None
            track.best_plate_crop = None
            if len(retired) > 512:
                cutoff = processed - RETIRE_TTL_FRAMES
                for tid in [t for t, r in retired.items() if r.last_index < cutoff]:
                    del retired[tid]

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
            thumb = _write_thumbnail(out_dir, frame)
            if thumb is not None:
                say(type="thumbnail", path=str(thumb))
            frames_out.append(
                {
                    "i": 0,
                    "frame": 0,
                    "seconds": 0.0,
                    "image": f"{MEDIA_PREFIX}/{job_id}/frames/000000.jpg",
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
                        if n == 0:
                            thumb = _write_thumbnail(out_dir, frame)
                            if thumb is not None:
                                say(type="thumbnail", path=str(thumb))
                        frames_out.append(
                            {
                                "i": n,
                                "frame": here,
                                "seconds": round(here / fps, 3),
                                "image": f"{MEDIA_PREFIX}/{job_id}/frames/{name}",
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


TERMINAL = ("done", "error", "cancelled")

# In memory a stopped run is `cancelled`; the table's frozen vocabulary has four
# values and that is not one of them, so it is stored as `error` carrying the
# reason. The two agree about everything except the word, and the word only
# differs for as long as this process is up.
_DB_STATUS = {"running": "processing", "cancelled": "error"}


def _media_url(stored):
    """A stored file path as the URL the browser fetches it from."""
    path = run_store.full_path(stored)
    if path is None:
        return None
    try:
        tail = path.resolve().relative_to(config.analyze_dir().resolve()).as_posix()
    except (ValueError, OSError):
        return None
    return f"{MEDIA_PREFIX}/{tail}"


class AnalysisJobs:
    """Every Analyze run: its row, its files, and the one child process.

    Since P6 a run outlives the request that started it and the process that ran
    it. The row in `analyze_runs` is the record -- what was analysed, how it
    went, and where the saved result document and thumbnail are -- and the job
    directory holds the annotated frames, the crops and that document. Neither
    is a sighting and nothing here writes one.

    **This class never opens a write connection.** It is handed the same
    `write(fn)` the API routes use, which runs on the pipeline's single writer
    thread. One writer, always.

    Still one child process at a time. The card is 6 GB and has to hold three
    streams; a fourth model set loaded because two files were dropped in a row is
    how that budget is spent twice. What P6 changes is that the second file is
    now a run of its own from the moment it is submitted -- queued, listed, with
    its own progress bar and its own delete -- rather than something the screen
    forgot while it waited.
    """

    def __init__(self, write=None):
        self.ctx = mp.get_context("spawn")
        # Runs this process has touched, by run id as text. The table is the
        # record; this holds what is known beyond it -- the uri, the stage, the
        # live progress -- none of which is one of the frozen columns.
        self.jobs = {}
        self.pending = deque()
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.thread = None
        self.stopping = False
        self.current = None        # run id of the running child
        self.stop_event = None     # its stop event
        self.write = write

    # ------------------------------------------------------------- lifecycle

    def bind_writer(self, write):
        """Take the one writer. Called by create_app before anything runs."""
        self.write = write

    def _write(self, fn):
        if self.write is not None:
            return self.write(fn)
        return run_store.write_directly(fn)

    def keep(self):
        try:
            return max(1, int(config.default("analyze_keep", DEFAULT_KEEP)))
        except (TypeError, ValueError):
            return DEFAULT_KEEP

    def start(self, sweep=True):
        """Reconcile what a previous process left, then take work.

        Two different leftovers, and they are not the same problem:

        - a row still `queued` or `processing` is a run whose process is gone.
          It is failed with the reason and KEPT, because the user deletes runs,
          not the app -- and its half-written frames are swept, because there is
          no result to view them from.
        - a directory with no row is an orphan: either the run was deleted and
          its files would not go, or the database was replaced. Nothing can open
          it again, so it goes.

        Both deletions happen on a thread of their own. `start` runs inside the
        FastAPI lifespan and a delete on Windows can block rather than fail --
        one wedged file is enough -- which would stop the whole app at "Waiting
        for application startup". Listing first also keeps the sweep's meaning:
        only what existed before this run is ever removed, so a run submitted a
        moment later cannot be swept out from under itself.
        """
        interrupted = []
        try:
            interrupted = self._write(run_store.mark_interrupted)
        except Exception as exc:  # noqa: BLE001 - a sweep must not stop the app
            print(f"[analyze] could not reconcile past runs: "
                  f"{type(exc).__name__}: {exc}")
        for row in interrupted:
            print(f"[analyze] run {row['run_id']} ({row['original_filename']}) was "
                  f"{row['status']} when the app stopped -- marked error")

        sweep = sweep and self._owns_directory()
        stale = self._stale_dirs() if sweep else []
        partial = (
            [config.analyze_dir() / str(row["run_id"]) for row in interrupted]
            if sweep else []
        )
        self.thread = threading.Thread(
            target=self._run_loop, name="analyze", daemon=True
        )
        self.thread.start()
        if stale or partial:
            threading.Thread(
                target=self._sweep, args=(stale, partial), name="analyze-sweep",
                daemon=True,
            ).start()

    def _owner_path(self):
        return config.analyze_dir() / ".belongs-to"

    def _owns_directory(self, quiet=False):
        """Whether this database is the one these job directories belong to.

        A run directory belongs to a row, and every row lives in one database.
        Point the app at a DIFFERENT database -- which every verification suite
        in scratch/ does, and which anyone moving `paths.db` does -- and every
        directory here looks orphaned, because the rows naming them are in the
        file left behind. Sweeping on that reading would delete the real runs of
        the real database, and it would do it at startup, before anybody could
        object.

        So the directory records which database it was swept for, once, and a
        mismatch turns the sweep off and says why rather than guessing. Nothing
        else is affected: runs still start, list, open and delete.
        """
        marker = self._owner_path()
        try:
            owner = str(config.db_path().resolve())
        except OSError:
            return False
        try:
            if marker.exists():
                written = marker.read_text(encoding="utf-8").strip()
                if written and written != owner:
                    if not quiet:
                        print(f"[analyze] {config.analyze_dir()} belongs to {written}, "
                              f"not to {owner} -- leaving every directory in it alone.")
                    return False
                if written == owner:
                    return True
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(owner + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"[analyze] could not read or write {marker}: {exc} -- "
                  f"leaving every directory alone.")
            return False
        return True

    def _attempted_path(self):
        return config.analyze_dir() / ".sweep-attempted"

    def _read_attempted(self):
        path = self._attempted_path()
        if not path.exists():
            return []
        try:
            with path.open(encoding="utf-8") as f:
                return [line.strip() for line in f if line.strip()]
        except OSError:
            return []

    def _write_attempted(self, names):
        path = self._attempted_path()
        try:
            if names:
                with path.open("w", encoding="utf-8") as f:
                    f.write("\n".join(names) + "\n")
            elif path.exists():
                path.unlink()
        except OSError:
            pass

    def _stale_dirs(self):
        """The orphaned job directories, minus the ones that will not go.

        A directory belongs to a run for as long as its row exists -- which is
        what P6 changed, and why this no longer removes everything it finds. An
        orphan is a directory no row names.

        A delete that hangs cannot report that it hung -- the call never
        returns -- so the name is written down before the attempt rather than
        after it. A directory still here on the next start, with its name
        already on the list, is one this machine cannot delete; trying again
        would wedge another thread on every start, and a process with a wedged
        thread cannot exit even after the server shuts down cleanly.
        """
        root = config.analyze_dir()
        if not root.exists():
            return []
        # Asked here as well as in `start`, so that nothing can arrive at this
        # list by another route: a directory is only ever orphaned relative to
        # the database that owns it. `start` has already said so out loud.
        if not self._owns_directory(quiet=True):
            return []
        try:
            known = run_store.read_ids()
        except Exception as exc:  # noqa: BLE001 - never sweep on a guess
            print(f"[analyze] could not read the run list, so nothing is swept: "
                  f"{type(exc).__name__}: {exc}")
            return []
        dirs = [
            path for path in root.iterdir()
            if path.is_dir() and path.name not in known
        ]
        attempted = set(self._read_attempted())
        for path in [p for p in dirs if p.name in attempted]:
            print(f"[analyze] leaving {path} alone -- a previous run could not delete "
                  f"it. Nothing here reads it; delete it by hand when the machine "
                  f"lets go of it.")
        return [p for p in dirs if p.name not in attempted]

    def _sweep(self, stale, partial=()):
        """Remove orphaned directories, and the half-written files of failed runs."""
        attempted = self._read_attempted()
        self._write_attempted(attempted + [p.name for p in stale])
        removed = []
        for path in stale:
            shutil.rmtree(path, ignore_errors=True)
            if path.exists():
                print(f"[analyze] could not remove {path} -- something still holds a "
                      f"file in it. Delete it by hand once nothing does.")
                continue
            removed.append(path.name)
        if removed:
            still = [n for n in self._read_attempted() if n not in removed]
            self._write_attempted(still)
            print(f"[analyze] swept {len(removed)} orphaned job "
                  f"director{'y' if len(removed) == 1 else 'ies'}")

        # An interrupted run keeps its row and its thumbnail -- enough to see
        # what it was and to delete it -- and loses the frames and crops of a
        # result that will never exist.
        for path in partial:
            for name in ("frames", "crops", "plates"):
                shutil.rmtree(path / name, ignore_errors=True)

    def shutdown(self):
        self.stopping = True
        if self.stop_event is not None:
            self.stop_event.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join(CANCEL_GRACE_SEC + 5)

    # ---------------------------------------------------------------- public

    def submit(self, uri, frame_skip=None, name=None):
        """Queue one file for analysis and return its run.

        The row is written before anything else happens, because the row is what
        makes the run exist: the id it comes back with names the directory, the
        media URLs and the route, so there is no moment at which a run is
        running and not recorded.
        """
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

        label = name or path.name
        try:
            row = self._write(
                lambda conn: run_store.insert(
                    conn, kind=kind, original_filename=label, progress=0.0
                )
            )
        except Exception as exc:  # noqa: BLE001 - the reason has to reach the UI
            traceback.print_exc()
            return None, (
                f"The run could not be recorded: {type(exc).__name__}: {exc}. "
                f"Check the server log."
            )

        job_id = str(row["run_id"])
        job = {
            "job_id": job_id,
            "run_id": row["run_id"],
            "uri": text,
            "name": label,
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
            "thumbnail": None,
            "created_ts": row["created_ts"],
            "started_ts": None,
            "finished_ts": None,
            "queue_position": None,
        }
        with self.lock:
            self.jobs[job_id] = job
            self.pending.append(job_id)
        self.wake.set()
        self._evict()
        return self.view(job_id), None

    def _live(self, job_id):
        """The in-memory job, with its queue position filled in. Caller holds."""
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

    def _from_row(self, row):
        """A stored run in the shape a live one has.

        A run from a previous session has no uri, no stage and no counts -- none
        of them are columns, and inventing them would be inventing them. What it
        has is what it was, how it ended, and where its result is.
        """
        return {
            "job_id": str(row["run_id"]),
            "run_id": row["run_id"],
            "uri": None,
            "name": row["original_filename"],
            "kind": row["kind"],
            "frame_skip": None,
            "status": row["status"],
            "stage": None,
            "detail": None,
            "progress": row["progress"],
            "error": row["error"],
            "warning": None,
            "counts": None,
            "media": None,
            "thumbnail": _media_url(row["thumbnail_path"]),
            "created_ts": row["created_ts"],
            "started_ts": None,
            "finished_ts": None,
            "queue_position": None,
        }

    def view(self, job_id):
        """One run as the API returns it: live state over stored state."""
        with self.lock:
            live = self._live(str(job_id))
        if live is not None:
            return live
        row = run_store.read_one(job_id)
        return None if row is None else self._from_row(row)

    def list(self, limit=25):
        """The runs, newest first, for the list beside the result."""
        rows = run_store.read_recent(limit)
        out = []
        with self.lock:
            for row in rows:
                live = self._live(str(row["run_id"]))
                if live is None:
                    out.append(self._from_row(row))
                    continue
                # The row carries the thumbnail, the live job carries everything
                # that moves. Neither is complete on its own.
                if not live.get("thumbnail"):
                    live["thumbnail"] = _media_url(row["thumbnail_path"])
                out.append(live)
        return out

    def result(self, job_id):
        """The result document, or None if this run has not produced one."""
        row = run_store.read_one(job_id)
        if row is None:
            return None
        path = run_store.full_path(row["result_path"])
        if path is None:
            path = config.analyze_dir() / str(row["run_id"]) / "result.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def cancel(self, job_id):
        job_id = str(job_id)
        queued = running = False
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                row = run_store.read_one(job_id)
                if row is None:
                    return False, "That run no longer exists."
                return False, f"That run has already finished ({row['status']})."
            if job["status"] in TERMINAL:
                return False, f"That run has already finished ({job['status']})."
            queued = job_id in self.pending
            if queued:
                self.pending.remove(job_id)
                job.update(status="cancelled", finished_ts=time.time(),
                           stage=None, detail=None)
            running = self.current == job_id
        if queued:
            self._persist(job_id, status="cancelled")
            return True, None
        if running and self.stop_event is not None:
            self.stop_event.set()
            return True, None
        return False, "That run could not be stopped."

    def delete(self, job_id):
        """Stop the run if it is still going, then remove its row and its files.

        In that order, and the wait between them is not optional: on Windows a
        video file held by a process that has not exited cannot be deleted, and
        neither can the frames it is still writing. `_run_one` only marks a run
        terminal after the child has been joined, so waiting for that is waiting
        for every handle to be closed.
        """
        job_id = str(job_id)
        with self.lock:
            known = job_id in self.jobs
        if run_store.read_one(job_id) is None and not known:
            return False

        self.cancel(job_id)
        if not self._await_finished(job_id, CANCEL_GRACE_SEC + 10):
            print(f"[analyze] run {job_id} did not stop in time; its files are "
                  f"left for the next startup sweep")

        with self.lock:
            self.jobs.pop(job_id, None)
            if job_id in self.pending:
                self.pending.remove(job_id)
        self._write(lambda conn: run_store.remove(conn, job_id))

        directory = config.analyze_dir() / job_id
        shutil.rmtree(directory, ignore_errors=True)
        if directory.exists():
            print(f"[analyze] removed run {job_id} but could not delete "
                  f"{directory} -- it is swept on the next start.")
        return True

    # --------------------------------------------------------------- internal

    def _await_finished(self, job_id, timeout):
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                job = self.jobs.get(job_id)
                if job is None or job["status"] in TERMINAL:
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def _persist(self, job_id, **fields):
        """Write what changed onto this run's row, and onto nothing else."""
        status = fields.pop("status", None)
        payload = dict(fields)
        if status is not None:
            payload["status"] = _DB_STATUS.get(status, status)
            # Not setdefault: the caller passes `error=None` for a run that
            # stopped without failing, and a row saying `error` with nothing in
            # its error column is a run that cannot explain itself after a
            # restart.
            if status == "cancelled" and not payload.get("error"):
                payload["error"] = run_store.CANCELLED_ERROR
        if not payload:
            return None
        try:
            return self._write(
                lambda conn: run_store.update(conn, job_id, **payload)
            )
        except Exception as exc:  # noqa: BLE001 - a run must not die of its record
            print(f"[analyze] could not update run {job_id}: "
                  f"{type(exc).__name__}: {exc}")
            return None

    def _evict(self):
        """Drop the oldest finished runs past the cap, with their directories."""
        with self.lock:
            protect = [
                int(j["run_id"]) for j in self.jobs.values()
                if j["status"] not in TERMINAL
            ]
        try:
            doomed = self._write(
                lambda conn: run_store.evict(conn, self.keep(), protect=protect)
            )
        except Exception as exc:  # noqa: BLE001 - the cap is housekeeping, not the run
            print(f"[analyze] could not evict old runs: {type(exc).__name__}: {exc}")
            return
        for row in doomed:
            job_id = str(row["run_id"])
            with self.lock:
                self.jobs.pop(job_id, None)
            shutil.rmtree(config.analyze_dir() / job_id, ignore_errors=True)
        if doomed:
            print(f"[analyze] evicted {len(doomed)} run(s) past the analyze_keep "
                  f"cap of {self.keep()}")

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
                self._persist(job_id, status="error", error=job["error"])

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
        self._persist(job_id, status="running", progress=0.0)
        process.start()
        print(f"[analyze {job_id}] {job['kind']} {job['uri']} (pid {process.pid})")

        terminal = None
        last_persist = time.monotonic()
        try:
            while True:
                try:
                    message = queue.get(timeout=0.5)
                except queue_mod.Empty:
                    if not process.is_alive():
                        break
                    continue
                kind = message.get("type")
                thumbnail = None
                with self.lock:
                    if kind == "stage":
                        job["stage"] = message.get("stage")
                        job["detail"] = message.get("detail")
                        if message.get("media"):
                            job["media"] = message["media"]
                    elif kind == "progress":
                        job["progress"] = message.get("progress", job["progress"])
                    elif kind == "thumbnail":
                        thumbnail = run_store.store_path(message.get("path"))
                        job["thumbnail"] = _media_url(thumbnail)
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
                if thumbnail is not None:
                    self._persist(job_id, thumbnail_path=thumbnail)
                if terminal is not None:
                    break
                now = time.monotonic()
                if kind == "progress" and now - last_persist >= PERSIST_EVERY_SEC:
                    last_persist = now
                    with self.lock:
                        seen = job["progress"]
                    self._persist(job_id, progress=seen)
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
            error = job["error"]
            progress = 1.0 if terminal == "done" else job["progress"]

        # The row is finished last, and only after the child has been joined, so
        # a row that reads `done` is one whose result document is on disk and
        # whose file handles are closed.
        result_path = None
        if terminal == "done":
            document = self.result(job_id)
            if document is not None:
                with self.lock:
                    job["warning"] = document.get("warning")
                result_path = run_store.store_path(
                    config.analyze_dir() / job_id / "result.json"
                )
        self._persist(
            job_id, status=terminal, progress=progress, error=error,
            result_path=result_path,
        )


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
