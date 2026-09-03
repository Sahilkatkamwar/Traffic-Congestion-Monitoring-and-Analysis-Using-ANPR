"""Track stitching: several tracker ids, one physical vehicle.

ByteTrack hands out a fresh id whenever it loses confidence in an association,
and on real traffic footage it does that constantly -- a vehicle passes behind
a pole, a second detection lands on the same car at a different scale, two
tracks swap during an overtake. Every one of those costs a duplicate row,
because the pipeline's rule is one sighting per track.

Measured on footage/session01/20260901_152733.mp4 before this existed: the same
white Ertiga was written three times, as ids 103, 132 and 137, all reading
MH15JS4241. Ids 132 and 137 lived for two frames each *inside* the lifetime of
103 -- so they were never "lost and reacquired" at all. That rules out a design
that only reconnects tracks across a gap, and it is why there are three
mechanisms here rather than one:

  overlap   a new id whose box sits on top of a live track's box, in the same
            frame, is that track. Nothing physical is in two places at once.
  gap       a new id that appears where a recently lost track was heading,
            at the same size and in the same colours, is that track.
  plate     two finished tracks that read the same plate are one vehicle,
            however far apart they are, because two vehicles in one clip do
            not carry the same registration.
  reid      two finished tracks that look like the same vehicle and were never
            in frame at the same time are one vehicle, even with no plate at
            all. This is the one that survives a vehicle leaving the frame and
            coming back, which the geometric gates cannot reach.
  footprint two finished tracks that were in frame at the same time and spent
            that time occupying the same pixels are one vehicle. CANDIDATE,
            off by default. This is the only mechanism here that deliberately
            declines `reid`'s physical veto, and it may do so only because it
            replaces the veto with a stronger physical statement -- see
            `footprint_twin`.

The first two run when a tracker id is first seen; the last three run when a
track is finished -- the earliest moment its voted plate and its best view both
exist.

Why `reid` needs a learned descriptor and not another histogram: measured on
the benchmark's own duplicates (scratch/inf/reid_probe.py), the hue/saturation
histogram above cannot separate them at any threshold. The white Swift seen
from the front and from the rear scores 0.140, while two *different* parked
motorcycles score 0.946 -- the descriptor is measuring colour, and colour is
shared by vehicles that are not the same and not shared by one vehicle seen
from two sides. A 256-d feature vector from `models/yolo11n-cls.pt` -- already
on disk as the classifier's starting weights, so no new dependency -- puts the
same Swift pair at 0.939 and, crucially, ranks every genuine duplicate in the
worst clip above every distinct pair that survives the time gate.

Nothing here writes anything. A stitch makes the worker reuse an existing
track_id, and the existing "this track was already written, so update its row"
path in pipeline.py does the rest. The sightings schema is untouched: the point
is to emit one row where there were three, not to add a column that explains
why there are three.

The bias throughout is against merging. A missed stitch is a duplicate row,
which the benchmark counts and a person can see. A wrong stitch silently
destroys a vehicle -- it merges two cars into one trajectory, and nothing
downstream can tell. So every mechanism requires agreement on position, size
and appearance together, and any single disagreement is a veto rather than a
score to be outweighed.
"""

import os

import cv2
import numpy as np

from app import config

# Loaded lazily, once per process, and never shared between them: it holds CUDA
# state like every other model here. None means "not loaded yet"; False means
# "tried and could not", so a missing file costs the re-identification pass and
# nothing else -- sightings are still written, exactly as with the plate reader
# and the classifier.
_embedder = None


def _load_embedder():
    global _embedder
    if _embedder is not None:
        return _embedder or None
    weights = config.model_path("reid_embedder")
    if weights is None:
        weights = config.ROOT / "models" / "yolo11n-cls.pt"
    if not weights.exists():
        print(f"[stitch] re-identification is off: no embedder at {weights}")
        _embedder = False
        return None
    try:
        os.environ.setdefault("YOLO_AUTOINSTALL", "false")
        from ultralytics import YOLO

        _embedder = YOLO(str(weights))
        print(f"[stitch] re-identification on, embedder {weights.name}")
    except Exception as exc:  # noqa: BLE001 - degrade, do not die
        print(f"[stitch] re-identification is off: {type(exc).__name__}: {exc}")
        _embedder = False
        return None
    return _embedder


def embedding(crop, imgsz=128):
    """A unit-length 256-d descriptor of one vehicle crop, or None.

    These are ImageNet features, not features trained for vehicle
    re-identification -- there is no ReID model in this project and pip is
    forbidden, so this is the strongest descriptor available without adding
    one. It is enough for the case that matters here, which is the *same*
    vehicle in the *same* scene minutes apart, and it is measurably not enough
    for the harder case: the grey Kwid of `20 sec.mp4`, seen once from the side
    and once head-on, scores 0.773 while unrelated pairs in that clip reach
    0.916. That vehicle is out of reach until a real ReID embedding exists, and
    the threshold is set to refuse it rather than to reach for it.
    """
    if crop is None or crop.size == 0:
        return None
    model = _load_embedder()
    if model is None:
        return None
    try:
        vec = model.embed(crop, imgsz=imgsz, verbose=False)[0]
    except Exception:  # noqa: BLE001 - a bad crop must not kill the worker
        return None
    vec = vec.detach().cpu().numpy().astype("float64").ravel()
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0 else None


def embedding_similarity(a, b):
    """Cosine similarity of two unit descriptors, or 0.0 if either is absent.

    Absent means "do not merge". That is the opposite of the histogram helper
    below, and deliberately so: this mechanism has no geometry backing it up,
    so a missing descriptor leaves nothing to be conservative *with*.
    """
    if a is None or b is None:
        return 0.0
    return float(max(0.0, min(1.0, float(np.dot(a, b)))))


# Appearance is compared as a hue/saturation histogram of the vehicle crop.
# Coarse on purpose: this has to survive the same car seen from two angles in
# different light, and a fine histogram does not.
_H_BINS = 24
_S_BINS = 24


def appearance(crop):
    """A small HS histogram of one vehicle crop, or None.

    Value is deliberately excluded. A vehicle that drives from sunlight into
    shade changes its brightness completely and its hue barely at all, and a
    descriptor that tracks brightness would refuse to stitch exactly the case
    that most needs it.
    """
    if crop is None or crop.size == 0:
        return None
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [_H_BINS, _S_BINS], [0, 180, 0, 256])
    cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
    return hist


def appearance_similarity(a, b):
    """Correlation of two histograms, clamped to 0-1. 1.0 when either is absent.

    An absent descriptor must not veto: a track whose crop was too small to
    describe should still be stitchable on position and size, which are the
    stronger signals anyway.
    """
    if a is None or b is None:
        return 1.0
    return max(0.0, min(1.0, float(cv2.compareHist(a, b, cv2.HISTCMP_CORREL))))


def iou(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def containment(a, b):
    """How much of the smaller box lies inside the larger one.

    IoU alone misses the duplicate that matters most here: a second detection
    covering the rear half of a car it already tracks scores a low IoU against
    the full-vehicle box while being entirely inside it.
    """
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter <= 0:
        return 0.0
    smaller = min(
        max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1),
        max(0.0, bx2 - bx1) * max(0.0, by2 - by1),
    )
    return inter / smaller if smaller > 0 else 0.0


def _centre(box):
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def _size(box):
    return max(1.0, box[2] - box[0]), max(1.0, box[3] - box[1])


class TrackStitcher:
    """Decides whether a new tracker id is really a track we already have.

    Holds configuration and nothing else. The worker owns the tracks; this only
    ever reads them, so there is no second copy of the truth to fall out of
    step with the first.
    """

    def __init__(
        self,
        overlap=0.55,
        max_gap_frames=25,
        max_move_boxes=2.0,
        min_size_ratio=0.5,
        min_appearance=0.45,
        plate_similarity=0.86,
        reid_similarity=0.97,
        reid_window_sec=45.0,
        footprint=False,
        footprint_min_frames=2,
        footprint_min_cover=0.8,
        footprint_iou=0.9,
        footprint_min_iou=0.85,
        footprint_size_ratio=0.8,
        footprint_max_drift=0.03,
        footprint_appearance=0.8,
        alias_revoke=False,
    ):
        self.overlap = overlap
        self.max_gap_frames = max_gap_frames
        self.max_move_boxes = max_move_boxes
        self.min_size_ratio = min_size_ratio
        self.min_appearance = min_appearance
        self.plate_similarity = plate_similarity
        self.reid_similarity = reid_similarity
        self.reid_window_sec = reid_window_sec
        # Candidate mechanism, off by default. See footprint_twin.
        self.footprint = footprint
        self.footprint_min_frames = footprint_min_frames
        self.footprint_min_cover = footprint_min_cover
        self.footprint_iou = footprint_iou
        self.footprint_min_iou = footprint_min_iou
        self.footprint_size_ratio = footprint_size_ratio
        self.footprint_max_drift = footprint_max_drift
        self.footprint_appearance = footprint_appearance
        # Candidate mechanism, off by default. See revoke_aliases.
        self.alias_revoke = alias_revoke

    @classmethod
    def from_config(cls):
        """A stitcher built from config/settings.yaml.

        Extracted from run_worker in P4c so that the Analyze screen stitches
        exactly the way a source worker does. Two copies of twenty thresholds
        drift, and a drifted copy would mean the same clip analysed and ingested
        reported different vehicle counts -- which is precisely the comparison
        the screen exists to support. The defaults below are the ones the worker
        passed and the values are read from the same keys.
        """
        return cls(
            overlap=float(config.default("stitch_overlap", 0.55)),
            max_gap_frames=int(config.default("stitch_max_gap_frames", 25)),
            max_move_boxes=float(config.default("stitch_max_move_boxes", 2.0)),
            min_size_ratio=float(config.default("stitch_min_size_ratio", 0.5)),
            min_appearance=float(config.default("stitch_min_appearance", 0.45)),
            plate_similarity=float(config.default("stitch_plate_similarity", 0.86)),
            reid_similarity=float(config.default("stitch_reid_similarity", 0.97)),
            reid_window_sec=float(config.default("stitch_reid_window_sec", 45.0)),
            footprint=bool(config.default("stitch_footprint", False)),
            footprint_min_frames=int(config.default("stitch_footprint_min_frames", 6)),
            footprint_min_cover=float(config.default("stitch_footprint_min_cover", 0.6)),
            footprint_iou=float(config.default("stitch_footprint_iou", 0.7)),
            footprint_min_iou=float(config.default("stitch_footprint_min_iou", 0.5)),
            footprint_size_ratio=float(
                config.default("stitch_footprint_size_ratio", 0.7)
            ),
            footprint_max_drift=float(
                config.default("stitch_footprint_max_drift", 0.06)
            ),
            footprint_appearance=float(
                config.default("stitch_footprint_appearance", 0.9)
            ),
            alias_revoke=bool(config.default("stitch_alias_revoke", False)),
        )

    # ------------------------------------------------------------- at first sight

    def adopt(self, box, crop, processed, candidates, seen_this_frame):
        """The track this new id belongs to, or None to start a fresh one.

        `candidates` is every track the worker still holds -- live and retired
        alike -- and `seen_this_frame` is the ids the tracker reported in this
        frame. A track the tracker is currently reporting separately is not a
        candidate for the overlap rule to swallow: if ByteTrack is confidently
        following two boxes at once they are two vehicles, and second-guessing
        it there is how a stitcher starts deleting cars.
        """
        # Described once, not once per candidate: this is the only cost the
        # stitcher adds per new tracker id, and a busy frame has several.
        look = appearance(crop)
        best = None
        for tid, track in candidates.items():
            if tid in seen_this_frame or track.last_box is None:
                continue
            gap = processed - track.last_index
            if gap < 0:
                continue
            score = self._score(box, look, track, gap)
            if score is None:
                continue
            if best is None or score > best[0]:
                best = (score, tid)
        return best[1] if best is not None else None

    def _score(self, box, look, track, gap):
        """How much this box looks like the next step of `track`, or None.

        None is a veto, not a low score. Position, size and colour each get an
        absolute bar, and only boxes that clear all three are ranked -- so a
        vehicle that is in the right place but the wrong colour is never
        stitched simply because nothing better was on offer.
        """
        # Size first: it is the cheapest check and it rules out the most.
        w, h = _size(box)
        tw, th = _size(track.last_box)
        ratio = min(w / tw, h / th) / max(w / tw, h / th)
        if ratio < self.min_size_ratio:
            return None

        overlap = max(iou(box, track.last_box), containment(box, track.last_box))
        if gap == 0:
            # Same frame: this is the duplicate-id case, and only a genuine
            # pile-up of one box on another counts.
            return overlap + 1.0 if overlap >= self.overlap else None

        if gap > self.max_gap_frames:
            return None

        # Where the track was going, not where it was. A vehicle crossing the
        # frame moves several box widths during a gap of a second, and matching
        # against its last position would only ever stitch the ones that were
        # stopped.
        cx, cy = _centre(box)
        px = track.last_box[0] + track.velocity[0] * gap + tw / 2.0
        py = track.last_box[1] + track.velocity[1] * gap + th / 2.0
        distance = ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5
        # Tolerance grows with the gap: the longer the track was missing, the
        # less its last velocity says about where it is now.
        budget = self.max_move_boxes * ((tw + th) / 2.0) * (1.0 + gap / 10.0)
        if distance > budget:
            return None

        similarity = appearance_similarity(track.appearance, look)
        if similarity < self.min_appearance:
            return None
        return similarity + overlap + max(0.0, 1.0 - distance / budget)

    def revoke_aliases(self, detections, aliases, tracks, retired):
        """Aliases the tracker has just disproved. Returns the raw ids to free.

        CANDIDATE, gated on `alias_revoke`, off by default.

        `adopt` decides from one frame whether a new tracker id continues a
        track it already has, and measured over 31575 processed frames of
        data/vehicle_mot_output it gets that wrong 313 times in 8633 -- 3.6%.
        Nothing available at the moment of the decision separates those 313
        from the rest: overlap, size ratio and centre distance have the same
        distribution in both groups, and an overlap floor that removes 189 of
        the 313 also removes 5000 of the 8320 correct adoptions
        (scratch/final/adopt_analyse.py). So the decision is left exactly as it
        is and the evidence that arrives LATER is used instead.

        That evidence is the tracker contradicting itself. Once raw id B is
        bound to track A, the tracker can go on to report A and B in the same
        frame. Two boxes in one frame that are not the same box are two
        vehicles, so the adoption is disproved -- by the tracker, not by a
        threshold -- and the alias is revoked.

        "Not the same box" is `adopt`'s own same-frame rule, `overlap`: below it
        `adopt` would have refused to merge the pair in the first place, and
        above it they are one object wearing two ids, which is the concurrent
        fragmentation `footprint_twin` exists for and must not be broken up.
        No threshold here is new and none is loosened.

        One raw id in the group keeps the track -- the one that IS the track's
        own id, or failing that the box nearest where the track was -- and the
        rest are freed. Each entry returned is
        (raw id, track id, overlap, the freed box, the kept box).

        Freeing can only ever split a track that was merged; it
        can never merge two that were not, so it cannot create a false merge.
        The worker also declines to adopt a freed id again: it has been proven
        an independent vehicle, and treating it as its own track is the
        conservative answer. Measured with that lock over the same 31575
        frames, the frames of a foreign vehicle sitting inside a row fall from
        1250 to 416 (scratch/final/adopt_pollution.py).
        """
        if not self.alias_revoke or not aliases:
            return ()
        groups = {}
        for detection in detections:
            raw = detection["track_id"]
            groups.setdefault(aliases.get(raw, raw), []).append(detection)
        freed = []
        for tid, group in groups.items():
            if len(group) < 2:
                continue
            track = tracks.get(tid) or retired.get(tid)
            keep = next((d for d in group if d["track_id"] == tid), None)
            if keep is None and track is not None and track.last_box is not None:
                keep = max(group, key=lambda d: iou(d["box"], track.last_box))
            if keep is None:
                keep = group[0]
            for detection in group:
                raw = detection["track_id"]
                if detection is keep or raw not in aliases:
                    continue
                together = max(iou(detection["box"], keep["box"]),
                               containment(detection["box"], keep["box"]))
                if together >= self.overlap:
                    continue
                freed.append((raw, tid, round(together, 3),
                              list(detection["box"]), list(keep["box"])))
        return tuple(freed)

    # ---------------------------------------------------------------- at the end

    def plate_twin(self, track, written):
        """A finished track already written under a different id, or None.

        Only ever called with a track that has a voted plate, and only against
        tracks that have one. Two vehicles in one clip do not share a
        registration, so this is the one mechanism here that needs no geometry
        at all -- which is exactly why it catches the fragments the geometric
        ones cannot, where the vehicle left the frame and came back.
        """
        from app import matching

        plate = _plate_of(track)
        if not plate:
            return None
        best = None
        for tid, other in written.items():
            if tid == track.track_id:
                continue
            other_plate = _plate_of(other)
            if not other_plate:
                continue
            score = matching.similarity(plate, other_plate)
            if score < self.plate_similarity:
                continue
            if best is None or score > best[0]:
                best = (score, tid)
        return best[1] if best is not None else None


    def reid_twin(self, track, written):
        """A finished track that is the same physical vehicle, by appearance.

        The case nothing else here reaches: a vehicle leaves the frame and
        comes back with **no plate to tie the two together**. The geometric
        gates cannot help -- across twenty seconds a predicted position is
        meaningless -- and `plate_twin` needs a plate that does not exist. On
        the benchmark footage that is most of the remaining duplication: the
        black-and-yellow autorickshaw of `20260901_151758.mp4` is parked and
        panned past repeatedly, and the pipeline wrote it five times.

        Three gates, and the first is the one doing the safety work.

        **Never in frame at the same time.** Two tracks whose lifetimes overlap
        are two vehicles, because one vehicle cannot be in two places at once.
        This is a physical fact rather than a tuned threshold, it costs
        nothing, and it is what makes the appearance threshold safe to set
        where it is: in `20260901_151758.mp4` a dark motorcycle and a white
        scooter that are genuinely different vehicles score 0.937, which is
        *higher* than the white Swift's two genuine views of itself at 0.939.
        No appearance threshold separates those two pairs. The clock does,
        instantly and for free -- the motorcycle and the scooter are both in
        frame at 09:00:25.

        **A re-acquisition window.** Long, because that is the entire point --
        far longer than `max_gap_frames`, which stays where it is. But not
        unbounded: over a long stream, every dark hatchback would eventually
        find a partner.

        **Appearance above `reid_similarity`.** Measured, not chosen. At 0.97
        the plate-less control clip `23sec.mp4` -- 76 rows of a dashcam driving
        past a parking lot, the hardest possible over-merge test -- yields
        exactly two merges, and both are the same white Ford pickup seen three
        times. At 0.96 it starts merging cars that are visibly different.

        Two confidently different registrations veto the merge outright. Two
        vehicles can look identical; they cannot be issued the same plate, and
        the converse is the strongest evidence available that they are not one
        vehicle.
        """
        look = getattr(track, "embedding", None)
        if look is None:
            return None
        plate = _plate_of(track)
        best = None
        for tid, other in written.items():
            if tid == track.track_id:
                continue
            other_look = getattr(other, "embedding", None)
            if other_look is None:
                continue
            if _overlaps_in_time(track, other):
                continue
            gap = _time_gap(track, other)
            if gap is None or gap > self.reid_window_sec:
                continue
            other_plate = _plate_of(other)
            if plate and other_plate:
                from app import matching

                if matching.similarity(plate, other_plate) < self.plate_similarity:
                    continue
            score = embedding_similarity(look, other_look)
            if score < self.reid_similarity:
                continue
            if best is None or score > best[0]:
                best = (score, tid, gap)
        if best is None:
            return None
        # Every re-identification merge is printed. A wrong one destroys a
        # vehicle silently, so the run has to leave a record a person can audit
        # against the evidence crops without re-running the footage.
        print(
            f"[stitch] re-id: track {track.track_id} is track {best[1]} "
            f"(similarity {best[0]:.3f}, {best[2]:.1f}s apart)"
        )
        return best[1]


    # ------------------------------------------------- concurrent fragmentation

    def footprint_twin(self, track, written):
        """A finished track that was the same box, every frame both were seen.

        CANDIDATE. Off unless `stitch_footprint` is true.

        This is the case `reid_twin` refuses by design and `adopt` refuses by
        design, and on the evidence each of them has, both are right to.
        ByteTrack can hold two simultaneous ids on one vehicle -- the parked
        black-and-yellow autorickshaw of `20260901_152733.mp4` is written twice
        as ids 61 and 71, and the parked silver pickup of `vdopov3.avi` twice as
        1119 and 1220. Those lifetimes *overlap*, so `reid_twin`'s clock veto
        discards them; and the tracker is reporting the ids apart in the same
        frame, so `adopt`'s `seen_this_frame` rule discards them too.

        The veto both rules rest on is "one vehicle cannot be in two places at
        once". That is true, and it is the wrong question to ask here: these
        tracks are not in two places. They are in **one** place, twice. So
        nothing above is loosened -- `stitch_reid_similarity` is untouched at
        0.97 and this mechanism never reads it. The veto is replaced by its own
        contrapositive, which is a stronger physical statement than any
        similarity threshold:

            for EVERY frame in which both tracks were reported, their boxes
            were the same box, at the same size, holding the same offset.

        **One disagreeing frame is a veto.** Not a lowered average -- a veto.
        That is the whole safety argument and it was arrived at by measurement,
        after two weaker formulations failed on the nine clips:

        - *Agreement on average over the shared frames* fails because a
          stitched track is a concatenation of the raw ids `adopt` folded into
          it, so two ids that were one vehicle for twenty frames can be two
          vehicles for the twenty after. Averaged over their whole shared
          range the genuine duplicates score a mean IoU of 0.000 to 0.334 --
          below unrelated pairs.
        - *Agreement over a sustained contiguous window* fails because it reads
          only the best window. `23sec.mp4`'s tracks 17 and 3 have one: nine
          processed frames at IoU 0.98, followed by seventeen consecutive
          frames at IoU 0.000 with both tracks on screen in different places.
          Seventeen frames of proof that they are two vehicles, invisible to a
          rule that looks at the best nine.

        The remaining gates are cheap confirmations of the same picture, and
        measured on the nine clips not one of them changes the answer anywhere
        in a wide band around its setting -- the mechanism sits on a plateau,
        not on a knife edge:

        **Enough shared frames** (`footprint_min_frames`). Two boxes cross for
        an instant all the time.

        **Coverage** (`footprint_min_cover`): the shared frames must be most of
        the shorter track's life. A duplicate id exists only while shadowing
        the track it duplicates.

        **Mean IoU, size agreement, offset stability.** Two boxes on one object
        are the same size and hold a near-constant vector between their
        centres; two vehicles passing each other sweep. Drift is measured in
        box widths, so it is scale free.

        **Appearance** (`footprint_appearance`) on the same embedding
        `reid_twin` uses -- a veto against a grossly different-looking pair,
        never the decision. On the nine clips it never binds: dropping it to
        0.0 produces exactly the same four merges, because the geometry has
        already refused everything else. It is kept because a mechanism whose
        only evidence is geometry should still be able to refuse a pair that
        looks nothing alike.

        Two confidently different plate reads veto the merge, exactly as in
        `reid_twin`: two vehicles can sit alike; they cannot be issued the same
        registration.

        Measured on the nine clips of footage/session01: four merges, all four
        confirmed against the source frames as one physical vehicle written
        twice, and the one hand-labelled *distinct* pair in the set --
        `vdopov4.avi` 8 and 20, a dark Subaru and a dark pickup that an
        ImageNet embedding scores 0.968 -- refused on geometry alone, at IoU
        0.000 across all fifteen frames the two share.
        """
        if not self.footprint:
            return None
        mine = getattr(track, "footprint", None)
        if not mine:
            return None
        plate = _plate_of(track)
        best = None
        for tid, other in written.items():
            if tid == track.track_id:
                continue
            theirs = getattr(other, "footprint", None)
            if not theirs:
                continue
            stats = self._footprint_stats(mine, theirs)
            if stats is None:
                continue
            other_plate = _plate_of(other)
            if plate and other_plate:
                from app import matching

                if matching.similarity(plate, other_plate) < self.plate_similarity:
                    continue
            look = getattr(track, "embedding", None)
            other_look = getattr(other, "embedding", None)
            score = embedding_similarity(look, other_look)
            if score < self.footprint_appearance:
                continue
            rank = stats["mean_iou"] + score
            if best is None or rank > best[0]:
                best = (rank, tid, stats, score)
        if best is None:
            return None
        _rank, tid, stats, score = best
        # Printed for the same reason every re-id merge is: a wrong merge
        # silently destroys a vehicle, so the run has to leave a record that
        # can be audited against the evidence crops without re-running it.
        print(
            f"[stitch] footprint: track {track.track_id} is track {tid} "
            f"(shared {stats['frames']}f, cover {stats['cover']:.2f}, "
            f"IoU mean {stats['mean_iou']:.3f} worst {stats['min_iou']:.3f}, "
            f"size {stats['size_ratio']:.2f}, drift {stats['drift']:.3f}, "
            f"appearance {score:.3f})"
        )
        return tid

    def _footprint_stats(self, a, b):
        """Geometry of two tracks over the frames they share, or None to veto.

        `a` and `b` are sequences of `(processed_index, box)`. Every gate is
        absolute: a pair failing one is not ranked at all, so no pair can buy
        its way past a disagreement with a strong score somewhere else.
        """
        left, right = dict(a), dict(b)
        shared = sorted(set(left) & set(right))
        if len(shared) < self.footprint_min_frames:
            return None
        cover = len(shared) / float(min(len(left), len(right)))
        if cover < self.footprint_min_cover:
            return None

        ious, ratios, offsets = [], [], []
        for index in shared:
            box_a, box_b = left[index], right[index]
            overlap = iou(box_a, box_b)
            # The veto. One frame in which the two tracks were somewhere else
            # is one frame in which they were two vehicles.
            if overlap < self.footprint_min_iou:
                return None
            ious.append(overlap)
            aw, ah = _size(box_a)
            bw, bh = _size(box_b)
            ratios.append(
                min(min(aw, bw) / max(aw, bw), min(ah, bh) / max(ah, bh))
            )
            acx, acy = _centre(box_a)
            bcx, bcy = _centre(box_b)
            scale = (aw + ah + bw + bh) / 4.0
            offsets.append(((bcx - acx) / scale, (bcy - acy) / scale))

        mean_iou = sum(ious) / len(ious)
        if mean_iou < self.footprint_iou:
            return None
        size_ratio = _median(ratios)
        if size_ratio < self.footprint_size_ratio:
            return None
        mx = _median([o[0] for o in offsets])
        my = _median([o[1] for o in offsets])
        drift = sum(
            ((ox - mx) ** 2 + (oy - my) ** 2) ** 0.5 for ox, oy in offsets
        ) / len(offsets)
        if drift > self.footprint_max_drift:
            return None
        return {
            "frames": len(shared),
            "cover": cover,
            "mean_iou": mean_iou,
            "min_iou": min(ious),
            "size_ratio": size_ratio,
            "drift": drift,
        }


def footprint_of(track):
    """The `(processed_index, box)` history a track kept, or an empty list."""
    return getattr(track, "footprint", None) or []


# A footprint is NOT unioned into the row a fragment merges into. That was
# built, measured on the nine clips and rejected (CLAUDE.md, "Absorbing the
# footprint on every stitch"): the worker replaces the object held under a
# surviving id, so after a merge every field on that row -- embedding, plate,
# timestamps -- describes the fragment that joined last, and unioning only the
# footprint would leave one field describing more of the row than the rest of
# them do. Measured, it is also strictly more conservative and costs a merge
# that review confirmed correct: 5 merges become 4, because the union brings
# with it a frame on which the two tracks disagree and the every-frame veto
# fires. Kept as a note rather than as code.


def _median(values):
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return 0.0
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def _seconds(value):
    return value.timestamp() if hasattr(value, "timestamp") else None


def _overlaps_in_time(a, b):
    """Were these two tracks ever in frame together?

    Unknown timestamps count as overlapping, which refuses the merge. A
    mechanism whose safety rests on the clock must not proceed when the clock
    is missing.
    """
    a1, a2 = _seconds(a.first_ts), _seconds(a.last_ts)
    b1, b2 = _seconds(b.first_ts), _seconds(b.last_ts)
    if None in (a1, a2, b1, b2):
        return True
    return not (a2 < b1 or b2 < a1)


def _time_gap(a, b):
    """Seconds between the end of the earlier track and the start of the later."""
    a1, a2 = _seconds(a.first_ts), _seconds(a.last_ts)
    b1, b2 = _seconds(b.first_ts), _seconds(b.last_ts)
    if None in (a1, a2, b1, b2):
        return None
    return b1 - a2 if b1 > a2 else a1 - b2


def _plate_of(track):
    result = getattr(track, "plate_result", None)
    return (result or {}).get("plate_raw")
