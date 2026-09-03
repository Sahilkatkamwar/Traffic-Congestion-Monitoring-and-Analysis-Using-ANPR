"""The camera worker.

One worker takes one video source and one source_id and emits sightings. It
does not know, and must not care, whether the source is a file, a webcam, a
phone, or an RTSP stream -- so it is never handed the source's `kind`. What it
gets is a uri and, for recorded sources, a start_time.

Recorded and live differ in exactly one thing: how a frame becomes an absolute
timestamp.

    recorded:  timestamp = start_time + frame_index / fps
    live:      timestamp = wall clock at capture

Both are resolved here, in _Clock. Everything downstream of the queue receives
absolute timestamps and cannot tell the two apart. Getting this wrong destroys
every trajectory and travel-time number in the app.

Runs as a spawned process on Windows: run_worker is a module-level function and
every argument is picklable. Nothing open -- no capture, no connection, no
loaded model -- ever crosses the process boundary. The worker builds its own.
"""

import json
import time
import traceback
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2

from app import config, db, grammar, stitch
from app.classify import VehicleClassifier
from app.detect import VehicleDetector
from app.ocr import PlateReader, vote
from app.ocr import sharpness as ocr_sharpness
from app.sources import resolve_uri

# Reconnect backoff for a live source that drops. A network camera stalls; it
# must not block the worker forever.
RECONNECT_DELAYS = (1, 2, 4, 8, 15, 30)
STATUS_EVERY_SEC = 1.0
FPS_WINDOW = 60
# How long a written track is remembered so a re-activated id updates its row.
RETIRE_TTL_FRAMES = 600

# Processed frames of box history one track keeps for the candidate footprint
# stitcher. Bounded because a parked vehicle on a live stream is tracked
# forever, and a list of every box it ever had is unbounded memory held by a
# mechanism that only ever looks at the window two tracks share. 512 covers
# every clip in footage/session01 whole -- the longest is 683 processed frames
# and no track in it lives that long.
FOOTPRINT_FRAMES = 512
# A stalled network camera must fail fast enough to reconnect. FFmpeg's own
# default is 30s of blocking, which stalls the worker instead.
OPEN_TIMEOUT_MS = 8000
READ_TIMEOUT_MS = 8000


# P4b: how often an annotated frame is published for the camera wall, and how
# wide it is encoded. Only ever done while somebody is actually watching -- see
# `preview_on` in run_worker.
PREVIEW_FPS = 6.0
PREVIEW_MAX_WIDTH = 720

# The uri rule moved to app.sources in P4b so that the UI and the connection
# test resolve a source exactly the way the worker does. The name stays bound
# here because this is still the only place in the worker that inspects a uri.
_resolve_uri = resolve_uri


def _open_capture(target):
    """Open a capture. Webcams need DirectShow or init hangs for seconds."""
    if isinstance(target, int):
        cap = cv2.VideoCapture(target, cv2.CAP_DSHOW)
    else:
        # The timeouts have to go in as constructor params. Setting them
        # afterwards is too late: the open already happened, and an unreachable
        # camera has already blocked for FFmpeg's own 30s default.
        cap = cv2.VideoCapture(
            target,
            cv2.CAP_ANY,
            [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_TIMEOUT_MS,
                cv2.CAP_PROP_READ_TIMEOUT_MSEC, READ_TIMEOUT_MS,
            ],
        )
    if not cap.isOpened():
        cap.release()
        return None
    return cap


def _probe(cap):
    """(total_frames, fps) from the capture, sanitised.

    Live sources report -1 or 0 for both. A recorded source reports a real
    frame count, and that -- not any config field -- is what tells the worker
    which clock to run.
    """
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    except (cv2.error, ValueError):
        total = 0
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS))
    except (cv2.error, ValueError):
        fps = 0.0
    if not 0 < fps < 240:
        fps = 0.0
    return max(total, 0), fps


class _Clock:
    """Frame index -> absolute timestamp. The timestamp rule lives here."""

    def __init__(self, is_recorded, start_time, fps, uri_target):
        self.is_recorded = is_recorded
        self.fps = fps
        if not is_recorded:
            self.base = None
            return

        base = db.from_iso(start_time) if isinstance(start_time, str) else start_time
        if base is None:
            # A recorded source with no start_time still has to land somewhere
            # real: file mtime beats "now", which would place last year's
            # footage in the present and break every travel-time calculation.
            base = self._mtime(uri_target)
            print(
                f"[worker] no start_time for a recorded source; "
                f"using file mtime {db.to_iso(base)}"
            )
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        self.base = base.astimezone(timezone.utc)

    @staticmethod
    def _mtime(uri_target):
        try:
            path = Path(str(uri_target))
            if path.exists():
                return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except OSError:
            pass
        return datetime.now(timezone.utc)

    def at(self, frame_index):
        if not self.is_recorded:
            return datetime.now(timezone.utc)
        return self.base + timedelta(seconds=frame_index / self.fps)


class _Track:
    """What one tracked vehicle accumulates before it becomes a sighting.

    One sighting per track, never per frame. Writing a row per frame inflates
    every count in the app by roughly the frame rate and still reads as
    plausible.
    """

    def __init__(self, track_id, ts, frame_index, keep_footprint=False):
        self.track_id = track_id
        self.first_ts = ts
        self.last_ts = ts
        self.hits = 0
        self.types = Counter()
        self.best_score = -1.0
        self.best_crop = None
        # --- what the stitcher reads --------------------------------------
        # Where this track was, how fast it was going, and what colour it is.
        # Held here rather than in the stitcher so there is one copy of a
        # track's state and it cannot drift from the track it describes.
        self.last_box = None
        self.velocity = (0.0, 0.0)
        self.appearance = None
        # Set once, from the best view, when the track is finalised. None means
        # the classifier is not configured or would not commit.
        self.classified_type = None
        # Set once at finalisation, from the same best view. This is what the
        # re-identification pass compares, and it is computed there rather than
        # per frame because it costs a forward pass and only the finished
        # track's best view is ever compared.
        self.embedding = None
        # Where this track's box was on every processed frame it appeared in,
        # as (processed_index, box). Kept only when the candidate footprint
        # stitcher is on, because it is the one piece of per-frame state in
        # this class and on a long live stream it is the one that grows: a
        # deque bounds it, and production, which does not ask for it, pays
        # nothing at all. See app/stitch.py:footprint_twin.
        self.footprint = deque(maxlen=FOOTPRINT_FRAMES) if keep_footprint else None
        # Counted in processed frames, not grabs: the two differ by frame_skip
        # on a file and by more than that on a live camera, and every timeout
        # in settings.yaml is written in processed frames.
        self.last_index = frame_index
        self.crop_path = None
        # Set once this track has been written. A tracker can re-activate an id
        # after we emitted it; the second emission must update that row, not
        # add a second one for the same vehicle.
        self.written = False

        # --- plate reading -------------------------------------------------
        # Several reads of the same plate, one per sampled frame, voted into
        # one string when the track ends. Only the plate crop is kept, never
        # the vehicle frames it came from: holding five full crops for every
        # live track is how this runs out of memory on a long stream.
        self.plate_reads = []
        self.plate_attempts = 0
        self.last_plate_index = None
        self.best_plate_score = -1.0
        self.best_plate_crop = None
        self.plate_crop_path = None
        self.plate_result = None

    def update(self, detection, frame, ts, frame_index, pad):
        """Fold one detection in.

        Returns (crop, origin, near_best): the vehicle crop for the plate pass,
        where that crop sits in the frame, and whether this is one of the
        closest views of the vehicle so far. A plate is legible when the
        vehicle is near, so that flag is what decides where the plate reads are
        spent.
        """
        box = detection["box"]
        if self.last_box is not None and frame_index > self.last_index:
            gap = frame_index - self.last_index
            self.velocity = (
                (box[0] - self.last_box[0]) / gap,
                (box[1] - self.last_box[1]) / gap,
            )
        self.last_box = box
        if self.footprint is not None:
            self.footprint.append((frame_index, box))
        self.last_ts = ts
        self.last_index = frame_index
        self.hits += 1
        self.types[detection["vehicle_type"]] += detection["conf"]

        crop, area, origin = _crop(frame, box, pad)
        if crop is None:
            return None, None, False
        # Best evidence, not latest: the largest confident view of the vehicle
        # is the one worth keeping, and it is what the plate is read from.
        score = area * detection["conf"]
        near_best = self.best_score <= 0 or score >= self.best_score * 0.9
        if score > self.best_score:
            self.best_score = score
            self.best_crop = crop
            # Described only when the best view changes -- a handful of times
            # per track, not once per frame.
            self.appearance = stitch.appearance(crop)
        return crop, origin, near_best

    def wants_plate(self, processed, every, near_best, max_attempts):
        """Is this track due another plate read?

        Only on the closest views. Spending the budget evenly across a track
        spends most of it while the vehicle is still far away and unreadable,
        and then stops exactly as it arrives -- which is how a plate that is
        legible in ten frames ends up voted from one.

        Spacing matters too: reads from consecutive processed frames are copies
        of the same motion blur, and voting over them learns nothing.
        """
        if self.plate_attempts >= max_attempts or not near_best:
            return False
        if self.last_plate_index is None:
            return True
        return processed - self.last_plate_index >= every

    def add_plate(self, hit, read, samples):
        """Keep the best `samples` reads of this track, best view first.

        Ranked by the quality of the view the read came from, not by arrival
        order and no longer by width alone. Width alone is why a track could
        vote over five copies of one motion-blurred frame: a plate that is wide
        because the vehicle is close is not legible if it is smeared across
        twelve pixels of travel, and the vote inherits the smear from every
        sample. Sharpness is what separates the two, and it is the cheapest
        measurement in this file.

        The same score picks the crop stored as evidence, so what a person is
        shown is the view the answer was actually read from.
        """
        score = hit["quality"]
        self.plate_reads.append((score, read))
        if len(self.plate_reads) > samples:
            self.plate_reads.sort(key=lambda item: item[0], reverse=True)
            del self.plate_reads[samples:]
        if score > self.best_plate_score:
            self.best_plate_score = score
            self.best_plate_crop = hit["crop"]

    def vehicle_type(self):
        """The type written to the row.

        The classifier wins when it has an opinion, because it is the only one
        of the two that knows what an autorickshaw is. It abstains below its
        confidence floor, and then the detector's COCO vote stands -- weighted
        by confidence across every frame of the track, which is more than any
        single frame's label is worth.
        """
        if self.classified_type is not None:
            return self.classified_type
        if not self.types:
            return "unknown"
        return self.types.most_common(1)[0][0]

    def live_read(self, pad_char="_"):
        """The best single read taken so far, for the camera wall overlay.

        Deliberately not the voted answer: the vote runs once, when the track
        ends, and the wall is showing a vehicle that is still in frame. What is
        drawn is one real read, never a guess assembled early.
        """
        if not self.plate_reads:
            return None
        _, (text, _probs) = max(self.plate_reads, key=lambda item: item[0])
        cleaned = (text or "").replace(pad_char, "").strip()
        return cleaned or None


def _pad_box(frame, box, pad_x, pad_y):
    """A box grown by a fraction of its own size and clipped to the frame.

    Separate fractions because plates are not square and the failures are not
    symmetric: what gets cut off a plate crop is the last character, not the
    top of the letters. A single fraction applied to a 120x35 plate adds three
    pixels above and below, which is nothing.
    """
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = box
    dx = (x2 - x1) * pad_x
    dy = (y2 - y1) * pad_y
    x1 = max(0, int(x1 - dx))
    y1 = max(0, int(y1 - dy))
    x2 = min(width, int(x2 + dx))
    y2 = min(height, int(y2 + dy))
    return x1, y1, x2, y2


def _crop(frame, box, pad, pad_y=None):
    """Padded crop, its pixel area, and its origin in the frame.

    The origin is what lets a plate found inside this crop be mapped back to
    frame coordinates and cut from the frame instead of from the crop. Cutting
    from the crop is not a resolution loss -- the pixels are the same ones --
    but it is a *clipping* loss: the crop's own edge is a hard wall, and a
    plate that reaches it comes out with its last character sliced off. That
    happened to the two plates in footage/session01 that a person can actually
    read, so it is not a hypothetical.

    Returns (None, 0, (0, 0)) if the box degenerates.
    """
    x1, y1, x2, y2 = _pad_box(frame, box, pad, pad if pad_y is None else pad_y)
    if x2 - x1 < 8 or y2 - y1 < 8:
        return None, 0, (0, 0)
    return frame[y1:y2, x1:x2].copy(), (x2 - x1) * (y2 - y1), (x1, y1)


# Overlay colours, BGR. Plate yellow for the box because a box is the detector
# speaking, and plate white behind a read because that is a plate's own ground.
_BOX_BGR = (24, 197, 245)
_LABEL_BG_BGR = (26, 20, 12)
_LABEL_FG_BGR = (242, 243, 238)
_PLATE_BG_BGR = (238, 243, 242)
_PLATE_FG_BGR = (28, 22, 18)
_FONT = cv2.FONT_HERSHEY_SIMPLEX


def _text_box(image, text, origin, bg, fg, scale=0.42, thickness=1):
    """A filled label with text in it, clamped inside the image."""
    (width, height), baseline = cv2.getTextSize(text, _FONT, scale, thickness)
    x, y = origin
    x = max(0, min(x, image.shape[1] - width - 8))
    y = max(height + 6, y)
    cv2.rectangle(
        image,
        (x, y - height - baseline - 3),
        (x + width + 8, y + 2),
        bg,
        -1,
    )
    cv2.putText(
        image, text, (x + 4, y - baseline + 1), _FONT, scale, fg, thickness,
        cv2.LINE_AA,
    )
    return height + baseline + 6


def _draw_overlay(frame, detections, tracks, pad_char, aliases=None):
    """Detection boxes and plate reads drawn onto a copy of the frame.

    A copy, always: the frame this is handed is the one the crops are cut from
    and the one OCR reads, and drawing on it would put a yellow rectangle in
    the stored evidence.
    """
    canvas = frame.copy()
    for detection in detections:
        x1, y1, x2, y2 = (int(v) for v in detection["box"])
        cv2.rectangle(canvas, (x1, y1), (x2, y2), _BOX_BGR, 2)

        tid = (aliases or {}).get(detection["track_id"], detection["track_id"])
        track = tracks.get(tid)
        # The stitched id, not the tracker's: the wall must show the same
        # identity the sighting will be written under, or a vehicle appears to
        # change id on screen and then does not in the database.
        label = f"{track.vehicle_type() if track else detection['vehicle_type']} #{tid}"
        used = _text_box(canvas, label, (x1, y1 - 4), _LABEL_BG_BGR, _LABEL_FG_BGR)

        read = track.live_read(pad_char) if track is not None else None
        if read:
            _text_box(
                canvas, read, (x1, y1 - 4 - used), _PLATE_BG_BGR, _PLATE_FG_BGR,
                scale=0.52, thickness=2,
            )
    return canvas


def _encode_preview(canvas, max_width=PREVIEW_MAX_WIDTH):
    """One annotated frame as jpeg bytes, or None if it will not encode."""
    height, width = canvas.shape[:2]
    if width > max_width:
        scale = max_width / width
        canvas = cv2.resize(
            canvas, (max_width, max(1, int(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    ok, buf = cv2.imencode(".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), 72])
    return buf.tobytes() if ok else None


def _stamp(ts):
    """Filename-safe timestamp. Windows paths cannot contain ':'."""
    return db.to_iso(ts).replace("-", "").replace(":", "").replace(".", "")


def _write_image(path, image):
    """Write via imencode: cv2.imwrite fails silently on non-ascii Windows paths."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    if not ok:
        return None
    path.write_bytes(buf.tobytes())
    try:
        return path.relative_to(config.ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _save_crops(source_id, track, save_unsorted):
    """Evidence crop for the sighting, plus an optional copy for training data."""
    if track.best_crop is None:
        return None
    name = f"{track.track_id:06d}_{_stamp(track.first_ts)}.jpg"
    rel = _write_image(config.crops_dir() / source_id / name, track.best_crop)
    if save_unsorted:
        # TRAINING.md sorts these by hand into a vehicle-type dataset. Flat and
        # source-prefixed so crops from every camera land in one pile.
        _write_image(
            config.unsorted_crops_dir() / f"{source_id}_{name}", track.best_crop
        )
    return rel


def _plate_crop(frame, vehicle_crop, box, origin, pad):
    """The plate cut from the frame, extended only where it was clipped.

    Padding every side is the obvious thing and it is wrong. Measured on
    footage/session01/20 sec.mp4: the tight crop read MH15HY2277 against a true
    MH15HY2237, and the same plate padded 10% wide and 18% tall read JK11B2217.
    fast-plate-ocr wants a plate and nothing else, and a rim of bumper costs
    more than the character it recovers.

    But a plate that ran off the edge of the vehicle box loses a character
    outright, and no amount of careful reading gets it back -- that is what put
    the 77 on the end of MH15HY22. Both of those are true at once, so the
    padding goes only where it is needed: a side of the plate box that has come
    to rest against the side of the vehicle box was cut there, and only that
    side is extended, into the frame the vehicle box was never allowed to
    reach past.

    A plate sitting comfortably inside its vehicle box is cropped exactly as
    tightly as it was before.
    """
    x1, y1, x2, y2 = box
    height, width = vehicle_crop.shape[:2]
    pad_x, pad_y = pad
    # Within a pixel of the edge is against the edge: the box coordinates were
    # rounded to integers on the way here.
    dx1 = (x2 - x1) * pad_x if x1 <= 1 else 0
    dx2 = (x2 - x1) * pad_x if x2 >= width - 1 else 0
    dy1 = (y2 - y1) * pad_y if y1 <= 1 else 0
    dy2 = (y2 - y1) * pad_y if y2 >= height - 1 else 0
    if not (dx1 or dx2 or dy1 or dy2):
        return None

    ox, oy = origin
    fh, fw = frame.shape[:2]
    fx1 = max(0, int(x1 + ox - dx1))
    fy1 = max(0, int(y1 + oy - dy1))
    fx2 = min(fw, int(x2 + ox + dx2))
    fy2 = min(fh, int(y2 + oy + dy2))
    if fx2 - fx1 < 8 or fy2 - fy1 < 8:
        return None
    return frame[fy1:fy2, fx1:fx2].copy()


def _read_plates(reader, pending, frame, frame_index, samples, pad):
    """Detect and OCR plates for the tracks due one this frame.

    Detection is per crop because each is a different size, but the OCR reads
    go through as one batch: the model resizes every plate to the same tensor,
    so one call for eight plates costs far less than eight calls.

    The plate is detected inside the vehicle crop and then cut from the frame.
    Those are the same pixels, so this is not about resolution -- it is about
    the edge. The vehicle box is drawn around a vehicle, not around its plate,
    and a plate at the edge of it comes out with a character missing. Both
    plates in footage/session01 that a person can read were clipped that way,
    and one of them lost its last digit and was read as ...2277 instead of
    ...2237. Cutting from the frame lets the padding run past the vehicle box
    to wherever the plate actually ends.
    """
    found = []
    for track, crop, origin in pending:
        # An attempt counts whether or not it found anything, so a vehicle
        # showing no plate stops being asked instead of being retried forever.
        track.plate_attempts += 1
        track.last_plate_index = frame_index
        hit = reader.find(crop)
        if hit is None:
            continue
        padded = _plate_crop(frame, crop, hit["box"], origin, pad)
        if padded is not None:
            hit["crop"] = padded
            hit["width"] = padded.shape[1]
        # Sharpness times size times detector confidence. Each on its own
        # picks a bad view: the sharpest crop is often the smallest, the widest
        # is often the most motion-blurred, and the most confident is neither.
        hit["quality"] = (
            (1.0 + ocr_sharpness(hit["crop"])) ** 0.5 * hit["width"] * hit["det_conf"]
        )
        found.append((track, hit))
    if not found:
        return
    reads = reader.read(
        [hit["crop"] for _, hit in found], [hit["rows"] for _, hit in found]
    )
    for (track, hit), read in zip(found, reads):
        track.add_plate(hit, read, samples)


def _finalise(source_id, track, save_unsorted, plate_opts, classifier=None):
    """Everything a track needs before it becomes a row: crops, type, the vote.

    Re-runs the vote on a re-activated track, because it has collected more
    reads since its row was written and the update should carry the better
    answer.
    """
    if track.best_crop is not None:
        # Before the crop is dropped. Once the row is written the pixels go and
        # the descriptor is all that is left to re-identify the vehicle by.
        if track.embedding is None:
            track.embedding = stitch.embedding(track.best_crop)
        if classifier is not None:
            # One call per track, on the one view the evidence crop is saved
            # from, so what the row claims is what a person can check.
            label, _conf = classifier.classify(track.best_crop)
            if label is not None:
                track.classified_type = label
        track.crop_path = _save_crops(source_id, track, save_unsorted)
    if track.best_plate_crop is not None and track.plate_crop_path is None:
        name = f"{track.track_id:06d}_{_stamp(track.first_ts)}.jpg"
        track.plate_crop_path = _write_image(
            config.plate_crops_dir() / source_id / name, track.best_plate_crop
        )
    if track.plate_reads:
        track.plate_result = vote(
            [read for _, read in track.plate_reads], **plate_opts
        )


def _sighting(source_id, track):
    """A finished track as a row for the writer."""
    plate = track.plate_result or {}
    candidates = plate.get("candidates")
    plate_raw = plate.get("plate_raw")
    return {
        "type": "sighting",
        "update": track.written,
        "source_id": source_id,
        "track_id": track.track_id,
        "plate_raw": plate_raw,
        # Position-aware grammar correction. When nothing could be corrected --
        # a truncated read is the common case -- this equals plate_raw, and the
        # sighting is still written: matching.py is what finds the vehicle
        # behind an uncorrectable read.
        #
        # The candidates are stored exactly as the vote produced them, not
        # corrected. They exist to record what the model actually saw, and
        # matching applies the grammar to the query side instead.
        "plate_text": grammar.apply(plate_raw),
        "plate_conf": plate.get("plate_conf"),
        "plate_candidates": json.dumps(candidates) if candidates else None,
        "vehicle_type": track.vehicle_type(),
        "vehicle_color": None,
        "first_seen_ts": db.to_iso(track.first_ts),
        "last_seen_ts": db.to_iso(track.last_ts),
        "crop_path": track.crop_path,
        "plate_crop_path": track.plate_crop_path,
        "frames_voted": plate.get("frames_voted", 0),
    }


def _snapshot(track):
    """One finished track, as the offline threshold sweep needs to replay it.

    Taken at the moment the track is finalised and BEFORE any twin is resolved,
    because that is the state the stitcher sees. A snapshot taken at the end of
    the run instead would be the state after every merge -- and a merge
    replaces the object held under an id, so the box history dumped for id 15
    would in fact be the history of whichever fragment merged into it last.
    That was measured and it is wrong by exactly the pairs this is trying to
    study.

    The embedding goes with it because the appearance gate needs it and
    re-deriving it later would need the crops, which are dropped once the row
    is written.
    """
    return {
        "track_id": track.track_id,
        "first_ts": db.to_iso(track.first_ts),
        "last_ts": db.to_iso(track.last_ts),
        "hits": track.hits,
        "written": track.written,
        "plate_raw": (track.plate_result or {}).get("plate_raw"),
        "crop_path": track.crop_path,
        "embedding": (
            [round(float(v), 6) for v in track.embedding]
            if track.embedding is not None
            else None
        ),
        "footprint": [
            [int(i), [round(float(v), 2) for v in box]]
            for i, box in (track.footprint or [])
        ],
    }


def _dump_footprints(directory, source_id, uri, log):
    """The finalisation log for one source, in the order it happened.

    Instrumentation, written only when `stitch_footprint_dump` names a
    directory, which nothing but scratch/footprint does. It exists so the
    candidate stitcher's thresholds can be swept over real footage without
    spending five minutes of GPU per setting -- the geometry of the footage
    does not change when a threshold does, so it is measured once and the
    merges are replayed offline.
    """
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    name = "".join(c if c.isalnum() or c in "-_." else "_" for c in Path(uri).name)
    path = out / f"{source_id}__{name}.json"
    path.write_text(json.dumps(log), encoding="utf-8")
    print(f"[worker {source_id}] footprints -> {path} ({len(log)} finalisations)")


def _status(source_id, status, error=None, fps=None, progress=None, terminal=False,
            stats=None):
    """One status message.

    `stats` is instrumentation, not data: raw detection counts, tracker ids
    handed out, stitches applied. It never reaches the database -- the writer
    reads only the columns it knows -- and the pipeline keeps the last one in
    memory so the benchmark in scratch/ can report what happened inside the
    worker without the worker writing files of its own.
    """
    return {
        "type": "status",
        "source_id": source_id,
        "status": status,
        "error": error,
        "fps": fps,
        "progress": progress,
        "terminal": terminal,
        "stats": stats,
    }


def run_worker(source, queue, stop_event, preview_queue=None, preview_on=None):
    """Read one source, emit one sighting per vehicle track.

    `source` is a plain dict of source_id, uri, fps, frame_skip and start_time.
    It deliberately does not carry `kind`: if this function could see what kind
    of source it has, something in it would eventually branch on that.

    Every exit path reports a terminal status, so the supervisor only has to
    handle a genuinely hard crash.

    P4b adds the camera wall's frames. `preview_on` is an event the parent sets
    while somebody is watching this source; nothing is drawn or encoded unless
    it is set, so a wall nobody has opened costs nothing. The frames published
    are the ones already decoded here -- the MJPEG endpoint never opens the
    source a second time, which for a webcam it could not do anyway.
    """
    source_id = source["source_id"]
    status = "done"
    error = None
    cap = None
    # Filled in at the end of the run and carried out on the terminal status.
    # Declared here so a worker that dies early still reports something rather
    # than tripping over an unbound name on the way out.
    stats = None
    try:
        frame_skip = max(1, int(source.get("frame_skip") or 1))
        pad = float(config.default("crop_pad", 0.04))
        timeout_frames = int(config.default("track_timeout_frames", 15))
        min_hits = int(config.default("min_track_hits", 2))
        save_unsorted = bool(config.default("save_unsorted_crops", True))
        plate_samples = int(config.default("plate_samples", 5))
        plate_every = int(config.default("plate_every", 2))
        plate_max_attempts = int(config.default("plate_max_attempts", 30))
        plate_opts = {
            "top_k": int(config.default("plate_candidates_k", 5)),
            "min_chars": int(config.default("min_plate_chars", 4)),
        }
        # Asymmetric on purpose: a plate crop loses characters off its ends,
        # not off the tops of its letters, and the vertical fraction of a
        # 120x35 box is three pixels unless it is scaled up.
        plate_pad = (
            float(config.default("plate_crop_pad_x", 0.10)),
            float(config.default("plate_crop_pad_y", 0.30)),
        )
        # Every threshold this reads lives in config/settings.yaml and is
        # built in one place -- app/stitch.py:from_config -- so that the
        # Analyze screen added in P4c stitches identically to a worker.
        stitcher = stitch.TrackStitcher.from_config()

        # Recording the footprint costs memory, so it is only done when
        # something will read it: the candidate mechanism, or the offline
        # threshold sweep that sets its numbers.
        footprint_dump = config.default("stitch_footprint_dump", None)
        keep_footprint = bool(stitcher.footprint or footprint_dump)
        footprint_log = [] if footprint_dump else None

        target = _resolve_uri(source["uri"])
        cap = _open_capture(target)
        if cap is None:
            raise RuntimeError(
                f"Could not open source {source['uri']!r}. Check the file exists, "
                f"the camera is not in use by another app, or the URL responds."
            )

        total_frames, probed_fps = _probe(cap)
        is_recorded = total_frames >= 1
        fps = probed_fps or float(source.get("fps") or 0) or 25.0
        if is_recorded and not probed_fps:
            print(f"[worker {source_id}] fps unreadable, assuming {fps}")
        clock = _Clock(is_recorded, source.get("start_time"), fps, target)

        detector = VehicleDetector()  # loaded once, never per frame

        # A plate model that will not load must not cost us the vehicle
        # sightings too: a sighting with a null plate is valid and still
        # written. The reason goes to the UI rather than only to stdout.
        reader = None
        plate_warning = None
        try:
            reader = PlateReader()
            plate_opts["pad_char"] = reader.pad_char
        except Exception as exc:  # noqa: BLE001 - degrade, do not die
            plate_warning = (
                f"Plate reading is off: {type(exc).__name__}: {exc}. "
                f"Vehicles are still detected and tracked."
            )
            print(f"[worker {source_id}] {plate_warning}")

        # Same rule as the plate reader: a classifier that will not load costs
        # the run its vehicle types, not its sightings. Unconfigured is silent
        # -- that is the documented state of a model that is not trained yet --
        # and broken is loud.
        classifier = None
        try:
            classifier = VehicleClassifier.load()
        except Exception as exc:  # noqa: BLE001 - degrade, do not die
            print(
                f"[worker {source_id}] vehicle classifier is off: "
                f"{type(exc).__name__}: {exc}. Types fall back to COCO."
            )

        print(
            f"[worker {source_id}] {'recorded' if is_recorded else 'live'} source, "
            f"fps={fps:.2f}, frame_skip={frame_skip}, "
            f"plates={'on' if reader else 'off'}, "
            f"types={'model' if classifier else 'coco'}"
        )
        queue.put(
            _status(source_id, "running", error=plate_warning, fps=fps,
                    progress=0.0 if is_recorded else None)
        )

        tracks = {}
        # Tracks already written, kept so a re-activated id updates its row
        # instead of adding a second one. Crops are dropped from these; only
        # identity, timing and the best score are worth holding on to.
        retired = {}
        # Raw ids whose alias the tracker disproved. Bounded on a live stream by
        # the same TTL that bounds `retired`.
        revoked = {}
        revocations = 0
        emitted = 0
        updated = 0
        plates = 0
        # Instrumentation only. Nothing here changes what is written; it is how
        # the real-video benchmark can say how many boxes the detector produced
        # and how many tracker ids became one vehicle.
        raw_detections = 0
        ids_seen = set()
        stitched = 0
        # Tracker id -> the track id it was folded into. Once a tracker id is
        # bound it stays bound: a vehicle must not oscillate between two rows
        # because the stitch was re-evaluated on a later frame.
        aliases = {}
        frame_index = 0   # grabs; the recorded clock is defined on this
        captured = 0      # live frames actually retrieved
        processed = 0     # frames put through the detector -- track timebase
        grab_times = deque(maxlen=FPS_WINDOW)
        last_status = time.monotonic()
        last_preview = 0.0
        preview_interval = 1.0 / max(PREVIEW_FPS, 0.5)
        pad_char = plate_opts.get("pad_char", "_")
        attempt = 0

        while not stop_event.is_set():
            if not cap.grab():
                if is_recorded:
                    break  # end of file
                # A live source that stops grabbing has dropped, not finished.
                cap.release()
                cap = None
                if attempt >= len(RECONNECT_DELAYS):
                    raise RuntimeError(
                        f"Source {source['uri']!r} stopped responding and did not "
                        f"come back after {len(RECONNECT_DELAYS)} attempts. Check "
                        f"the camera is powered on and on this network."
                    )
                delay = RECONNECT_DELAYS[attempt]
                attempt += 1
                print(f"[worker {source_id}] read failed, retrying in {delay}s")
                queue.put(
                    _status(source_id, "running",
                            error=f"reconnecting to {source['uri']} (attempt {attempt})")
                )
                if stop_event.wait(delay):
                    break
                cap = _open_capture(target)
                if cap is None:
                    cap = cv2.VideoCapture()  # keeps the loop's grab() falsy
                continue

            attempt = 0
            index = frame_index
            frame_index += 1

            # A bare grab really does advance a file, so skipping here skips
            # the frame and its decode with it. It does not advance a live
            # source: only a retrieve consumes a camera frame, and a grab
            # without one hands back the same buffer immediately. Measured on
            # this machine's webcam -- 15 fps at frame_skip 1, 3 and 8 alike,
            # while the grab rate rose to 45 and 119 -- so on a live source
            # this skip would skip nothing and only inflate the frame count.
            if is_recorded and index % frame_skip:
                continue

            ok, frame = cap.retrieve()
            if not ok or frame is None:
                continue

            if not is_recorded:
                # So a live source skips after the retrieve, and counts the
                # frames it actually took. Without this a camera is reported at
                # frame_skip times its real rate and every frame is put through
                # the detector regardless of the setting -- three times the
                # intended load on a card that has to hold three streams.
                grab_times.append(time.monotonic())
                captured += 1
                if (captured - 1) % frame_skip:
                    continue

            # Read the clock at capture: everything after this is inference
            # latency and must not leak into the timestamp. The clock takes the
            # grab index, because that is what the recorded timestamp rule is
            # defined on; everything else below counts processed frames.
            ts = clock.at(index)
            processed += 1

            detections = detector.track(frame)
            raw_detections += len(detections)
            # An adoption the tracker has just contradicted: two raw ids in ONE
            # frame bound to one track, on boxes that are not the same box.
            # Freeing the intruder splits a merge and can never make one, so it
            # cannot cost a false merge. A freed id is never adopted again --
            # the tracker has proved it is its own vehicle.
            for raw_tid, owner_tid, together, box_a, box_b in stitcher.revoke_aliases(
                detections, aliases, tracks, retired
            ):
                del aliases[raw_tid]
                revoked[raw_tid] = processed
                revocations += 1
                print(
                    f"[worker {source_id}] track {raw_tid} is NOT track "
                    f"{owner_tid} (overlap {together} in one frame) -- "
                    f"alias revoked  [f{processed} grab={index} "
                    f"a={[int(v) for v in box_a]} b={[int(v) for v in box_b]}]"
                )
            if len(revoked) > 4096:
                cutoff = processed - RETIRE_TTL_FRAMES
                for raw_tid in [r for r, p in revoked.items() if p < cutoff]:
                    del revoked[raw_tid]
            seen_this_frame = {
                aliases.get(d["track_id"], d["track_id"]) for d in detections
            }
            pending = []
            for detection in detections:
                raw_tid = detection["track_id"]
                ids_seen.add(raw_tid)
                # A tracker id this run already decided belongs to an earlier
                # track stays bound to it for the rest of the run.
                tid = aliases.get(raw_tid, raw_tid)
                track = tracks.get(tid)
                if track is None:
                    # The tracker keeps lost tracks alive for track_buffer
                    # frames and can hand an id back. That is the same vehicle,
                    # so pick its accumulator back up rather than starting over.
                    track = retired.pop(tid, None)
                if track is None:
                    # A genuinely new id. Before believing it is a new vehicle,
                    # ask whether it is one we are already following: ByteTrack
                    # splits a track far more often than a vehicle appears.
                    candidate, _area, _origin = _crop(frame, detection["box"], pad)
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

            if pending:
                _read_plates(
                    reader, pending, frame, processed, plate_samples, plate_pad
                )

            # The camera wall. Drawn from the frame that was just decoded and
            # the tracks that were just updated -- re-opening the source to
            # serve a second decode would double the GPU cost and, on a webcam,
            # simply fail: the device is already held by this process.
            if (
                preview_queue is not None
                and preview_on is not None
                and preview_on.is_set()
            ):
                now = time.monotonic()
                if now - last_preview >= preview_interval:
                    last_preview = now
                    jpeg = _encode_preview(
                        _draw_overlay(frame, detections, tracks, pad_char, aliases)
                    )
                    if jpeg is not None:
                        rate = fps
                        if not is_recorded and len(grab_times) > 1:
                            span = grab_times[-1] - grab_times[0]
                            rate = (len(grab_times) - 1) / span if span > 0 else None
                        try:
                            preview_queue.put_nowait(
                                {
                                    "source_id": source_id,
                                    "jpeg": jpeg,
                                    "fps": rate,
                                    "tracks": len(detections),
                                    "ts": db.to_iso(ts),
                                }
                            )
                        except Exception:  # noqa: BLE001
                            # A full preview queue is a viewer that fell behind.
                            # Dropping the frame is right; blocking the worker
                            # on a browser is not.
                            pass

            # A track the tracker has stopped reporting is a vehicle that has
            # left. Emit it now rather than holding every track until EOF --
            # a live source would otherwise grow without bound and show
            # nothing until it stopped.
            gone = [
                tid
                for tid, t in tracks.items()
                if processed - t.last_index > timeout_frames
            ]
            for tid in gone:
                track = tracks.pop(tid)
                if track.hits < min_hits:
                    continue
                _finalise(source_id, track, save_unsorted, plate_opts, classifier)
                snapshot = _snapshot(track) if footprint_log is not None else None
                # The plate is only known now, so this is the earliest moment
                # two fragments of one vehicle can be recognised by what they
                # read. A twin makes this an update to that row rather than a
                # new one.
                #
                # Plate first, appearance second: a shared registration is the
                # stronger claim, and most of these tracks carry no plate at
                # all, which is exactly the gap re-identification fills.
                twin = stitcher.plate_twin(track, retired)
                if twin is None:
                    twin = stitcher.reid_twin(track, retired)
                if twin is None:
                    # Last, and only ever on pairs the two above declined: it
                    # exists for the concurrent case reid_twin vetoes on the
                    # clock, so it must never pre-empt a decision made on a
                    # plate or on a clean re-acquisition.
                    twin = stitcher.footprint_twin(track, retired)
                if twin is not None:
                    aliases[tid] = twin
                    stitched += 1
                    track.track_id = twin
                    track.written = True
                if snapshot is not None:
                    snapshot["twin"] = twin
                    footprint_log.append(snapshot)
                queue.put(_sighting(source_id, track))
                updated += track.written
                emitted += not track.written
                plates += bool(track.plate_result) and not track.written
                track.written = True
                # The row has the crops; do not keep holding the pixels. The
                # reads stay -- a re-activated track keeps voting on them.
                track.best_crop = None
                track.best_plate_crop = None
                retired[track.track_id] = track

            # Ids are handed out monotonically, so a retired track this old can
            # never come back. Holding it forever would leak on a live source.
            if len(retired) > 512:
                cutoff = processed - RETIRE_TTL_FRAMES
                for tid in [t for t, r in retired.items() if r.last_index < cutoff]:
                    del retired[tid]

            now = time.monotonic()
            if now - last_status >= STATUS_EVERY_SEC:
                last_status = now
                live_fps = fps
                if not is_recorded and len(grab_times) > 1:
                    span = grab_times[-1] - grab_times[0]
                    live_fps = (len(grab_times) - 1) / span if span > 0 else None
                progress = min(frame_index / total_frames, 1.0) if is_recorded else None
                # Re-send the plate warning, or the next status would clear it
                # a second after it was raised and the UI would never show it.
                queue.put(
                    _status(source_id, "running", error=plate_warning,
                            fps=live_fps, progress=progress)
                )

        # A source short enough to yield a single processed frame -- a still
        # image -- would otherwise emit nothing at all.
        threshold = 1 if processed < min_hits else min_hits
        for track in list(tracks.values()):
            if track.hits < threshold:
                continue
            _finalise(source_id, track, save_unsorted, plate_opts, classifier)
            snapshot = _snapshot(track) if footprint_log is not None else None
            twin = stitcher.plate_twin(track, retired)
            if twin is None:
                twin = stitcher.reid_twin(track, retired)
            if twin is None:
                twin = stitcher.footprint_twin(track, retired)
            if twin is not None:
                stitched += 1
                track.track_id = twin
                track.written = True
            if snapshot is not None:
                snapshot["twin"] = twin
                footprint_log.append(snapshot)
            queue.put(_sighting(source_id, track))
            updated += track.written
            emitted += not track.written
            plates += bool(track.plate_result) and not track.written
            track.written = True
            retired[track.track_id] = track

        if footprint_dump:
            _dump_footprints(footprint_dump, source_id, source["uri"], footprint_log)

        if stop_event.is_set():
            status = "idle"
        stats = {
            "processed_frames": processed,
            "raw_detections": raw_detections,
            "tracker_ids": len(ids_seen),
            "emitted": emitted,
            "updated": updated,
            "stitched": stitched,
            "revocations": revocations,
            "plates": plates,
        }
        print(
            f"[worker {source_id}] finished: {emitted} sighting(s) "
            f"({updated} track re-activation update(s), {plates} with a plate) "
            f"from {processed} processed frame(s)"
        )

    except Exception as exc:  # noqa: BLE001 - the reason has to reach the UI
        status = "error"
        error = f"{type(exc).__name__}: {exc}"
        print(f"[worker {source_id}] {error}")
        traceback.print_exc()
    finally:
        if cap is not None:
            # Windows keeps a lock on a video file held by a dead handle.
            cap.release()
        try:
            queue.put(
                _status(source_id, status, error=error,
                        progress=1.0 if status == "done" else None, terminal=True,
                        stats=stats)
            )
        except Exception:  # noqa: BLE001 - the parent may already be gone
            pass
