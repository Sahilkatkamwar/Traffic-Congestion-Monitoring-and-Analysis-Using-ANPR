# CLAUDE.md

Build spec for an ANPR platform: multi-camera plate recognition, vehicle
trajectory reconstruction on a map, and traffic analytics.

This file specifies what to build. Environment setup, data collection, and model
training are handled outside this file — do not do them.

## Environment — already configured, do not modify

Windows. Virtual env at `env\`, activated with `env\Scripts\activate.bat` in cmd.
All Python packages are installed. Model weights are in `models\`.

- **Never run pip install.** If something is missing, stop and say so.
- **Never modify or reinstall torch.** The CUDA build is working.
- Node is installed. You may run `npm install` inside `web/` only.
- Assume CUDA is available. Do not add CPU fallback logic.

## Windows rules — these break silently if ignored

- Python multiprocessing on Windows uses **spawn**, not fork. Every entrypoint
  needs an `if __name__ == "__main__":` guard or workers respawn infinitely.
  Worker targets must be module-level functions and all arguments picklable.
  Do not pass open DB connections, cv2 captures, or loaded models between
  processes — each worker constructs its own.
- Use `pathlib.Path` everywhere. Never hardcode `/` or build paths with string
  concatenation.
- Open webcams with `cv2.VideoCapture(index, cv2.CAP_DSHOW)`. Without the
  DirectShow flag, initialisation takes several seconds or hangs.
- Always `release()` captures. Windows locks video files held by a dead handle.
- Write files with explicit `encoding="utf-8"`.

## Hardware constraints

6GB RTX 3050. The pipeline must handle 3+ concurrent streams.

- Nano model variants only. 640px detection input.
- Every source has a configurable `frame_skip`. Build this into the worker from
  P1 — do not retrofit it.
- Run OCR only on plate crops above a minimum pixel width.
- Load each model once per worker process, never per frame.

---

# Architecture

## The source abstraction

A **camera worker** takes one video source plus one `source_id` and emits
sightings. It must not branch on what kind of source it is. These are all the
same code path:

| kind | source value |
|---|---|
| recorded file | `footage/cam01.mp4` |
| webcam | `0` |
| phone / IP camera | `http://192.168.1.7:8080/video` |
| RTSP | `rtsp://...` |

If any downstream code needs to know which kind it is, the abstraction is wrong.

## Timestamp rule — critical

For a **recorded** source: `timestamp = source.start_time + (frame_index / fps)`.
For a **live** source: `timestamp = wall clock at capture`.

This is the ONLY place the two differ, and it is resolved inside the worker.
Everything downstream receives absolute timestamps and cannot tell them apart.
Getting this wrong destroys every trajectory and travel-time calculation.

## Sources are runtime state, not config

Sources are stored in the database and managed from the UI. `config/sources.yaml`
is a seed loaded once at first run. The user must be able to add a live camera,
upload a video, or upload an image from the interface without editing files or
restarting the app.

Adding a source starts a worker. Removing one stops it. Workers are supervised:
if one dies, mark the source `error` with the reason and surface it in the UI.

---

# Data contract — FROZEN

Do not change field names or types without asking first.

### `sources`
| field | type | notes |
|---|---|---|
| source_id | text PK | |
| name | text | shown in UI |
| kind | text | `file` / `webcam` / `network` / `image` |
| uri | text | path, index, or URL |
| lat, lon | real | placed by the user; nullable for uploads |
| heading_deg | int | nullable |
| fps | real | probed for files, measured for live |
| frame_skip | int | default 3 |
| start_time | timestamp | recorded sources only |
| status | text | `idle` / `running` / `done` / `error` |
| error | text | nullable |
| progress | real | 0–1 for files, null for live |

### `sightings`
One row per **vehicle track**, never per frame.

| field | type | notes |
|---|---|---|
| sighting_id | int PK | |
| source_id | text FK | |
| track_id | int | unique within one worker run |
| plate_raw | text | OCR output before correction, nullable |
| plate_text | text | after grammar correction, nullable |
| plate_conf | real | 0–1, aggregated across voted frames |
| plate_candidates | json | top-k alternatives, required for fuzzy matching |
| vehicle_type | text | auto / car / motorcycle / bus / truck / unknown |
| vehicle_color | text | nullable |
| first_seen_ts | timestamp | absolute |
| last_seen_ts | timestamp | absolute |
| crop_path | text | vehicle crop |
| plate_crop_path | text | plate crop, nullable |
| frames_voted | int | |

A sighting with a null plate is valid and still written.

### `alerts`
| field | type | notes |
|---|---|---|
| alert_id | int PK | |
| kind | text | `blacklist` / `impossible_transition` |
| severity | text | `info` / `warning` / `critical` |
| plate_text | text | |
| sighting_ids | json | |
| detail | text | one-line human-readable summary |
| created_ts | timestamp | |

SQLite in WAL mode. **One writer.** Workers push to a queue; a single process
drains it. Concurrent writers will lock.

---

# Stack

Backend: Python, Ultralytics, OpenCV, SQLite, FastAPI, uvicorn.
Frontend: Vite + React + Tailwind, Leaflet, Recharts, Framer Motion.

**Banned unless asked:** Docker, Redis, Celery, Kafka, PostgreSQL, any ORM,
Next.js or SSR, Redux or any state library, any component library, authentication,
abstract base classes with one implementation.

One process at runtime. FastAPI serves the API, the websocket, MJPEG streams, and
the built frontend from `web/dist` with an SPA fallback route.

---

# Interface

## Visual direction

Derive the palette from Indian number plates, which already encode meaning:
white ground for private, yellow for commercial, green for electric, red for
government. Plate yellow is the accent. Colour must carry data — a yellow badge
means commercial because commercial plates are yellow — never decoration.

Base surface is a deep desaturated slate, not pure black, so map tiles and video
sit on it without vibrating.

**Contemporary, not enterprise-legacy.** Specifically:

- Layered elevation and generous spacing instead of borders on everything. One
  hairline divider is worth five boxes.
- Radii 12–16px on cards, 8px on controls. Never 0, never fully rounded except
  on status pills.
- Panels floating over the map use backdrop blur. Nothing else does.
- Motion is spring-eased, 150–250ms. Sightings slide in, the trajectory draws,
  markers pulse on activity. Nothing else animates.
- Type scale with real contrast: large numerals for live counts, small
  uppercase letter-spaced labels, comfortable 14–15px body. No walls of 12px text.
- Plate strings always render in a condensed grotesque, letter-spaced, so they
  read as evidence rather than as table text.

**Do not produce:** bevels, inner shadows, gradient buttons, glowing neon
borders, dense bordered tables, a Bootstrap-shaped layout, or a sidebar of grey
icons with 11px labels.

## Screens

Top-level nav: Live · Sources · Analyze · Trace · Insights · Alerts

**Live** — full-bleed map, source markers with status, pulsing on new sightings.
Floating left panel: live sighting feed with plate crop, plate string,
confidence, vehicle badge, source, timestamp. Alert strip above it. Every
sighting is clickable, opens evidence, offers "Trace this vehicle".

**Sources** — add and manage inputs. Three add flows:
1. *Live camera* — pick a detected webcam or paste an IP/RTSP URL, test the
   connection with a preview frame before saving, then place it on a map picker.
2. *Recorded video* — upload or select a file, set start time and location,
   process with a visible progress bar.
3. *Image* — upload one or more stills for single-shot detection.

Also the camera wall: grid of live MJPEG feeds with detection boxes and plate
reads drawn on. Each tile shows fps and status.

**Analyze** — drop an image or video, get an annotated result view: boxes, plate
reads with confidence, vehicle types, cropped evidence. For video, a scrubbable
result timeline. Export the detections as JSON or CSV. This screen must work
standalone without any camera configured.

**Trace** — plate search. Because matching is fuzzy, results are a ranked
candidate list with match score and sighting count, never one silent answer.
Selecting a candidate opens the trajectory: ordered path on the map with
numbered stops, a time scrubber that drives both the path and an evidence strip
below it, and a table of source, timestamp, gap, and implied average speed.

**Insights** — heatmap toggle, vehicle count over time, type distribution,
per-source density ranking, origin-destination flow lines weighted by volume.
One shared time-window filter across all panels.

**Alerts** — newest first. An impossible transition renders as paired evidence:
both crops side by side, both timestamps, distance between sources, and the
speed that would have been required.

## Interface rules

- Confidence is always visible. Every plate string shows its score; every fuzzy
  match shows how it matched.
- Every sighting is clickable and shows its crop.
- Empty states say what to do next, not "no data".
- Never render placeholder or mock data. If a panel has no data, it says so.
- Errors state what failed and how to fix it: "Camera at 192.168.1.7 did not
  respond. Check the phone is on the same network."
- Keyboard reachable, focus visible, `prefers-reduced-motion` respected.
- Copy is plain and active: "Trace vehicle", not "Initiate trajectory analysis".

---

# Build phases

One phase per session. Announce the phase before writing code. Do not start the
next until exit criteria pass.

### P0 — Skeleton
Repo layout, `db.py` with the frozen schema and WAL, config loader, `run.py`
with the `__main__` guard, FastAPI serving a health route.
**Exit:** `python -m app.run` starts, creates the db, serves `/api/health`.

### P1 — Worker: detection and tracking
Source-agnostic worker with frame-skip. YOLO detection with ByteTrack
(`persist=True`, sequential frames). One sighting per track with vehicle type,
correct timestamps per the timestamp rule, and a saved crop. Writer queue.
Supervision and status updates.
**Exit:** a video file and a webcam both produce sightings through identical
code, with timestamps correct for each.

### P2 — Plate reading
Plate detection on tracked crops. OCR across multiple frames of one track.
Character-level majority vote, ties broken by confidence. Minimum plate width
filter. Store raw, confidence, candidates, frames voted, plate crop.
**Exit:** an eval script scores predictions against a labelled test set and
prints plate-level accuracy.

### P3 — Correction, matching, trajectory
`grammar.py`: validate `[2 letters][1-2 digits][1-3 letters][4 digits]` and BH
format, check state codes, apply position-aware confusion fixes (letter slot
reading `0` becomes `O`, digit slot reading `S` becomes `5`; also O/D/Q, 8/B,
1/I/7, 2/Z, 6/G, M/H).
`matching.py`: fuzzy candidate retrieval, then confusion-weighted edit distance.
**Never exact string equality.**
`trajectory.py`: plate query to time-ordered sightings with coordinates.
**Exit:** eval prints accuracy before and after correction. A vehicle seen at
multiple sources returns a complete trajectory despite OCR variance.

### P4a — UI shell and Live
Vite/React/Tailwind scaffold, design tokens, nav, map, markers, websocket feed,
evidence crops. FastAPI serves `web/dist` with SPA fallback.
**Exit:** browser shows sightings appearing live as workers run.

### P4b — Sources
All three add flows, connection testing, map placement, upload handling,
progress, start/stop, camera wall with MJPEG overlays.
**Exit:** a live camera and a recorded video are both added from the UI, with no
file editing and no restart.

### P4c — Analyze
Image and video upload, annotated results, scrubbable video timeline, JSON/CSV
export. Works with zero cameras configured.
**Exit:** dropping an unseen image returns annotated detections with plate reads.

### P4d — Trace
Fuzzy search with ranked candidates, path rendering, time scrubber driving map
and evidence strip together, sighting table.
**Exit:** searching a plate and pressing play animates its path with crops in sync.

### P5 — Insights and Alerts
Heatmap, charts, OD flows, shared time filter. Blacklist matching on write.
Impossible-transition detection using source distance and elapsed time.
**Exit:** a blacklisted plate raises a visible alert within seconds of its
sighting.

---

# Working rules

- Announce the phase before writing code.
- Do not refactor completed phases unless blocking. Say what and why first.
- Do not add fields to `sightings` without asking.
- Do not add dependencies. Do not run pip.
- Model paths come from `config/settings.yaml`, never hardcoded.
- Experiments go in `scratch/`. Nothing in `app/` imports from it.
- Log with plain `print` to stdout. No logging framework.
- When ambiguous, choose the simplest option that satisfies the exit criteria
  and note the assumption. Do not build for unstated requirements.
- Every phase ends with the app runnable.

# Known traps

- Missing `if __name__ == "__main__":` causes infinite worker respawn on Windows.
- Ultralytics tracking requires sequential frames and `persist=True`. Batching or
  shuffling silently breaks track continuity.
- One sighting per track, not per frame — this inflates every analytic by ~30x
  and is easy to miss.
- Frame-skip too aggressive breaks track association. Default 3, make it tunable.
- Live sources have no reliable fps from OpenCV. Measure it over a rolling window.
- Network cameras drop and stall. Set read timeouts and reconnect with backoff
  rather than blocking the worker forever.
- MJPEG endpoints must not re-decode the source. Reuse the frame the worker
  already decoded.
- SPA fallback missing means deep links 404 when the frontend is served by FastAPI.
- Concurrent SQLite writers lock even in WAL mode. One writer, always.

---

# Current state — measured 2026-09-03

Everything below is a number produced by a script in this repo, not an estimate.
Re-run any of it before trusting it.

## Datasets on disk

Five vehicle datasets now, and they are **not the same kind of data**. Confusing
them is the single easiest way to waste a training run here.

| directory | kind | contents | labels |
|---|---|---|---|
| `data/vehicle_cls/` | **classification** | folder-per-class crops of single vehicles | folder name is the label; no boxes anywhere |
| `data/vehicle_det/` | **detection** | 216 road-scene frames, 640×482 | YOLO `.txt` boxes, 4 classes |
| `data/vehicle_det_split/` | detection, derived | train/val built from the above | built by `scripts/prep_vehicle_det.py` |
| `data/vehicle_mot_output/` | **tracker output** | 33 sequences, 107140 frames, 486597 boxes with track ids | YOLO11n + ByteTrack, **not human identity truth** |
| `data/vehicle_plate_frames/` | **plate detection** | 14 sources, 1007 frames, 1705 plate boxes | pseudo-labels, single class, recall-incomplete |
| `data/plate_frames_split/` | plate detection, derived | source-disjoint train/val/test built from the above | built by `scripts/prep_plate_frames.py` |
| `data/plate_frames_split_c/` | as above, plus ensemble-completed train boxes | same split, 163 extra train boxes | same script, `--complete` |

`data/vehicle_cls` holds no bounding boxes and **cannot** be used for detection.
`data/vehicle_det` holds boxes but no `auto` class — autorickshaws in it are
labelled `car` — so it **cannot** supply the classifier the auto/car distinction
that `vehicle_cls` exists for. Neither substitutes for the other.

### `data/vehicle_det` — audited 2026-09-02, `scratch/inf/audit_vehicle_det.py`

    images 216   labels 216   Boxed 216   all three sets of stems match exactly
    image size   640x482, all 216
    boxes        1250, every line 5 fields, every coordinate in range,
                 0 boxes crossing an image edge
    empty        1 label file (frame_1282) -- a near-empty frame with one
                 vehicle cut by the top edge that the pseudo-labeller missed
    classes      0 car 561   1 motorcycle 584   2 bus 21   3 truck 84

**Class ids are not written down anywhere in the dataset** — there is no
`data.yaml`. They were read off the burned-in class names in `Boxed/` and
verified on four frames spanning the range: `frame_1139` carries one bus and two
cars and its label file is `2, 0, 0`; `frame_1175`'s only class-3 line is the
lorry. The order is COCO's own: **0 car, 1 motorcycle, 2 bus, 3 truck**.

`Boxed/` is 216 visual-review copies with boxes burned into the pixels. It is
**not** training data and nothing trains on it.

**Provenance and what it costs.** The labels are pseudo-labels from YOLO11-L +
RT-DETR-L, not human truth, and the audit found the two consequences:

- **31 pairs of boxes overlap at IoU > 0.75** — one vehicle labelled twice,
  often as `truck` by one model and `bus` by the other. That is two models
  merged with no NMS across them. `scripts/prep_vehicle_det.py` drops the
  smaller of each pair (30 boxes removed) before anything trains.
- **The pseudo-labeller has no `auto` class**, so it labels autorickshaws
  `car` — visible in `Boxed/frame_1417.jpg`, where the yellow auto is `car 0.90`.

Scored against `models/yolo11n.pt`, which did not make the labels: the COCO
model recovers **80.4%** of the label boxes at IoU 0.5, **89.2%** of its own
boxes are in the labels, and the two agree on class for **87.8%** of matches.
The disagreements are almost entirely bus/truck/car, which is the same confusion
both models have.

**One camera, one scene.** All 216 frames are a single fixed elevated CCTV feed
("LIVE FROM FIRST FOOT OVER BRIDGE NEAR VHS HOSPITAL"), frames 1139–1417, banner
timestamps 11:51–12:00 on 2015-12-18 — about 2.4 seconds apart across nine
minutes. One viewpoint, one time of day, one weather. So:

- the split is **temporal, not random** — the last 20% of frame numbers is val,
  because two frames seconds apart share their vehicles and a random split
  would leak them;
- **`bus` has 1 instance in val and 14 in train.** Any per-class bus number from
  this split is noise and is reported as such;
- the benchmark footage is handheld street-level phone video and this is not,
  which is the whole risk in fine-tuning on it.

**Suitability: usable for a detector fine-tune, with a frozen backbone, as a
candidate only.** Structurally the labels are clean. What they cannot do is
generalise off this camera — measured below.

### `data/vehicle_mot_output` -- audited 2026-09-03, `scratch/final/mot_audit.py`

    33 sequence directories, 33 distinct source videos
    15 carry annotations.csv + annotations_mot.txt; 18 have frames and no labels
    frames extracted  107140 of the 817341 the videos contain (13%)
    fully extracted   2 sequences only: `10` and `20260903_164745`
    annotations       486597 rows, 19829 physical ids
    integrity         0 malformed boxes, 0 out-of-range boxes,
                      0 annotated frames whose image is missing
    classes           car 421876  truck 58146  bus 3457  motorcycle 3118

**`physical_id` is a renumbering of `raw_track_id`, not a correction of it.**
Measured in every annotated sequence (`scratch/final/mot_idmap.py`): the two
have the same cardinality, **no physical id is fed by more than one raw id, and
no raw id is split across two physical ids**. Sequence `10` has 3490 of each,
`16` has 5113 of each. The ids are simply compacted to 1..N.

`meta.json` says as much — model `yolo11n.pt`, tracker `bytetrack.yaml`,
`"human_review_required": true` — so this is **the raw output of the same
detector and tracker the pipeline runs, and it carries no human identity label
at all.** Scoring identity switches against it would be scoring the tracker
against itself.

**What it is good for is the opposite direction, and that is what it was used
for.** It is 486597 real detections carrying real tracker ids over 107140 real
frames, which is exactly the INPUT `app/stitch.py:adopt()` consumes. Replaying
it through `adopt()` tests the decision rule rather than the labels, and the
audit below rests on physics rather than on any label — see "adopt(): identity
pollution".

**Content.** 13 of the 15 annotated sequences are one family of US highway and
road CCTV at 800x410, where no plate is legible at any threshold.
`20260903_164745` (3757 frames, 92 ids) and `20260903_164407` (unannotated) are
Indian campus phone video at 1080x1920. Frames are original and unannotated —
nothing is burned into the pixels.

### `data/vehicle_plate_frames` -- audited 2026-09-03, `scratch/final/plate_audit.py`

    14 source videos   1007 images   1007 label files   1007 boxed/ review copies
    1705 boxes, single class `0`, 926 positive frames and 81 negatives
    malformed 0   out-of-range 0   byte-identical duplicate images 8 (one source)
    box width  min 23.2px  median 89.4px  max 533.8px
               under 48px (the pipeline's min_plate_width): 302 = 17.7%

Structurally clean. `boxed/` is burned-in review copies and nothing trains on
it. Every `summary.json` names the labeller — `plate_dataset_builder/models/best.pt`
at conf 0.15 — and carries its own warning that these are pseudo-labels. Four
findings decide how they can be used, and three disqualify one source each:

- **One source is poison.** In `Licence Plate Camera Illustration Video`,
  **163 of its 176 boxes (93%) sit on the burned-in "IP Camera" watermark** in
  the top-left corner, boxed at up to 0.84 confidence in nearly every frame.
  That is why the two detectors already in this repo recover only 8 and 11 of
  its 176 "plates". The whole source is dropped, and a corner-box rule removes
  the 2 stragglers of the same kind elsewhere.
- **Two sources carry burned-in detection graphics.**
  `ANPR India Detection Demo - SmartCow` renders a cyan box and the read plate
  string **on the plate it is demonstrating**. A detector trained on that learns
  the graphic.
- **One source leaks into the benchmark.** `20260901_152704` is the same phone,
  the same road and the same minute as `20260901_152733.mp4`, a benchmark clip.
- **The labels are recall-incomplete, and this one cannot be cleaned.** Asked
  what they see, `plate_det_scenes.pt` and `plate_detector.pt` — neither of
  which made these labels — recover only **69.2%** and **75.7%** of the labelled
  boxes and **agree on 268 boxes the labels do not have**. A 48-box sample of
  those 268 (`scratch/final/unmatched_sheet.jpg`) is **43 real plates and 5
  tail-light clusters**, so at least ~240 visible plates, **~14% of the true
  total, are labelled as background** — and each one teaches a detector to
  suppress exactly the plate it should find.

**Suitability: usable for a plate-detector fine-tune after the three exclusions,
with the recall-incompleteness a ceiling on what it can teach rather than a
detail.** `scripts/prep_plate_frames.py` writes the split and codes every
exclusion with its reason. 792 frames over 11 sources survive, split
**source-disjoint** because 2 fps sampling of 30 fps video makes neighbouring
frames near-copies.

### `data/vehicle_cls` -- re-audited 2026-09-03, and the truck failure is now explained

Counts are unchanged (train 3967, val 627, five classes, **no test split**).
`scratch/final/cls_audit.py` finds no leakage — 115 exact-duplicate groups, **0
spanning train/val and 0 spanning two classes** — and then finds why the truck
class is dead.

**Train and val are three different collections, and the file format alone says
so:**

| | format | width | aspect |
|---|---|---|---|
| train, all classes | 97% `.jpg` | class-specific: bus 363-640, truck 56-**317**, car 92-1920 | varied |
| val auto | 100% `.jpg` | 1181-4160 | 0.75 |
| val bus / car / motorcycle / truck | 100% `.png` | **exactly 192, every file** | 1.00 |

**And `truck` does not mean the same thing on the two sides.** Asked of
`models/yolo11n-cls.pt`, an ImageNet classifier that never saw these labels
(`scratch/final/cls_semantics.py`):

| folder | top ImageNet reads |
|---|---|
| **train/truck** | trailer_truck 41%, moving_van 35% — articulated lorries and box vans |
| **val/truck** | trailer_truck 16%, **pickup 12%, fire_engine 10%, garbage_truck 9%** |
| train/bus | trolleybus 34%, minibus 13%, and **14% of the folder reads as nothing vehicle-shaped** — matching the face, traffic light and city skyline visible in `scratch/final/peek/cls_trucks.jpg` |

The model is trained on "articulated lorry" and tested on pickups, fire engines
and bin lorries. **truck F1 = 0.000 is the expected result of that mismatch, and
no amount of retraining on this data repairs it** — the fix is a `truck` folder
that means one thing, which is still the hand-sort of `crops/unsorted/` that
TRAINING.md already names.

## Benchmark footage -- session01 changed under the benchmark, 2026-09-03

`footage/session01` no longer holds the nine clips `b2_base9` was measured on.
Measured, not assumed:

- it now holds **21 videos** — the 12 plate-dataset source videos were copied in
  beside the originals (they also live in `footage/visible_plates/`);
- **`20 sec.mp4` has moved** to `footage/clips/20 sec.mp4`;
- **`10 sec.mp4` is gone from the disk entirely** — no copy exists anywhere
  under `footage/`.

So **the nine-clip baseline can be reproduced on eight of its nine clips and
never again on the ninth**, and every run in this pass names its clips
explicitly rather than globbing the directory. The eight are the b2_base9 set
minus `10 sec.mp4`, with `20 sec.mp4` read from its new path.

Reproduced exactly: run `f_prodA` — production configuration, those eight clips,
through the changed code with both new candidates off — matches `b2_base9` clip
for clip on processed frames, raw detections, tracker ids, stitches, rows, plate
crops and reads. 380 rows and 389 stitches, which is `b2_base9`'s 392 and 404
less `10 sec.mp4`'s 12 and 15.

**Never run the benchmark over everything in `footage/session01`.** Twelve of
those videos are the plate dataset's own sources and three are in
`data/plate_frames_split`'s training set.

## Real-video benchmark

`scratch/bench_realvideo.py` over the five clips `footage/session01` held at
the time, scored against the hand labels in `data/video_truth.yaml`. Four more
clips were added on 2026-09-03; the nine-clip baseline is the section below and
this one is its five-clip subset, which still reproduces exactly.

`before` is the state at the start of the 2026-09-02 inference pass — saved as
`runs/bench/inf_base.json` and confirmed to reproduce the previous session's
`after` run exactly (148 / 10 / 10 / 10 / 0 / 169 and 1-of-4). `after` is
`runs/bench/inf_reid.json`, the current shipped configuration. **The detector,
the plate detector and the OCR weights are identical in both columns** — every
difference below is pipeline and configuration.

| | before | after |
|---|---|---|
| raw vehicle detections | 4748 | 4748 |
| tracker ids | 328 | 328 |
| sightings written | 148 | **143** |
| track stitches applied | 169 | **174** |
| plate crop hit rate | 10/148 = 6.8% | **12/143 = 8.4%** |
| plate read hit rate | 10/148 = 6.8% | **12/143 = 8.4%** |
| reads on the 4 Indian clips | 10 | **12** |
| **false positives on `23sec.mp4`** | **0** | **0** |
| distinct plates | 10 | 12 |
| duplicate plated rows | 0 | 0 |
| exact OCR vs hand labels | 1/4 = 25.0% | 1/4 = 25.0% |
| **mean similarity to hand labels** | 0.657 | **0.750** |
| workers not ending `done` | 0 | 0 |

Per clip, rows / plate crops / reads / stitches:

| clip | rows | plate crops | reads | stitches |
|---|---|---|---|---|
| 20 sec.mp4 | 10 → 10 | 2 → 2 | 2 → 2 | 5 → 5 |
| 10 sec.mp4 | 12 → 12 | 0 → 0 | 0 → 0 | 15 → 15 |
| 23sec.mp4 | 76 → **74** | 0 → 0 | 0 → 0 | 103 → **105** |
| 20260901_151758.mp4 | 21 → **18** | 2 → **3** | 2 → **3** | 20 → **23** |
| 20260901_152733.mp4 | 29 → 29 | 6 → **7** | 6 → **7** | 26 → 26 |

Per hand-labelled plate, the closest read and its confusion-weighted similarity:

| label | before | after |
|---|---|---|
| MH15JS4241 | `MH15JS4241` 1.00 | `MH15JS4241` 1.00 |
| MH15HY2237 | `MH15HY2277` 0.90 | `MH15HY2277` 0.90 |
| MH17CY4718 | `MH77IVX183` 0.47 | `MH17CI1478` **0.70** |
| MP09ZS0907 | `MH15JS4241` 0.27 | `ML06B0877` **0.40** |

**Exact OCR did not move and is not expected to** — see "Why exact OCR is still
1 of 4" below. What moved is that two plates the pipeline was reading as
nonsense are now read as near-misses, five duplicate rows are gone, and the
zero-false-positive property on the plate-less clip survived every change.

## session01 expanded to nine clips — clean baseline measured 2026-09-03

Four clips were added to `footage/session01`, more than tripling the footage and
changing what the set is for. Nothing was tuned, trained or swapped for this
pass: `config/settings.yaml`, every model and the whole pipeline are
byte-identical to the `inf_reid` configuration above, and the five original
clips reproduce their previous numbers exactly. The run is
`runs/bench/b2_base9.json`, tag `b2_base9`; `inf_reid` is untouched and remains
the five-clip baseline.

### Inventory — probed, not read off the filenames

`scratch/baseline2/probe_session01.py`. Frame counts are what OpenCV actually
yields to a worker, counted by grabbing to the end, and they agree with every
container header here.

| file | MB | codec | WxH | fps | frames | duration |
|---|---|---|---|---|---|---|
| `10 sec.mp4` | 2.4 | h264 | 478x850 | 30.04 | 349 | 11.6s |
| `20 sec.mp4` | 5.4 | h264 | 478x850 | 30.01 | 624 | 20.8s |
| `23sec.mp4` | 20.6 | h264 | 1920x1080 | 30.00 | 720 | 24.0s |
| `20260901_151758.mp4` | 1013.3 | h264 | 2160x3840 | 59.66 | 2005 | 33.6s |
| `20260901_152733.mp4` | 519.2 | h264 | 3840x2160 | 59.35 | 2048 | 34.5s |
| **`vdo.avi`** | 179.8 | FMP4 | 1280x960 | 10.00 | 2001 | **200.1s** |
| **`vdopov2.avi`** | 108.8 | FMP4 | 1280x960 | 10.00 | 2001 | **200.1s** |
| **`vdopov3.avi`** | 109.0 | FMP4 | 1280x720 | 10.00 | 2001 | **200.1s** |
| **`vdopov4.avi`** | 65.3 | FMP4 | 1280x720 | 10.00 | 2001 | **200.1s** |

**9 videos, 924.9s = 15.42 min.** The four new clips are 800.4s of it, against
124.5s in the original five — the set is now **7.4x longer**, and 87% of its
duration is footage that did not exist before.

The four are exactly 2001 frames at 10 fps each, which is a processed-frame
budget of 667 apiece at `frame_skip` 3 — more processed frames per clip than any
original clip except the two 4K phone videos.

### What the new footage is, and what it is not

Sampled frames in `scratch/baseline2/peek_*.jpg`. All four are **elevated
wide-angle fisheye traffic-camera views of suburban US intersections**, winter
daylight, bare trees, dry road, US vehicles — pickups, SUVs and sedans. Four
viewpoints, one apparent locale and one time of day.

They are **not** Indian traffic and carry **no Indian plates**, so they cannot
exercise `app/grammar.py`, the state-code check, the confusion table, the
two-row work, or any OCR metric in this file. Calling them ground truth would be
wrong twice over: they have no hand labels, and the thing this project reads
does not appear in them.

**They are additional evaluation footage, and what they add is scale on the
detection and tracking half of the pipeline** — 2668 processed frames and 12853
raw detections, against 1917 and 4748 for the original five.

### The baseline — nine clips, production configuration

`scratch/bench_realvideo.py --tag b2_base9 --clips ...`, log in
`scratch/baseline2/base9.log`. Every worker ended `done` with no error; 273s
wall for 925s of footage.

| clip | proc frames | raw dets | tracker ids | stitches | rows | plate crops | reads | distinct |
|---|---|---|---|---|---|---|---|---|
| 20 sec.mp4 | 208 | 207 | 16 | 5 | 10 | 2 | 2 | 2 |
| 10 sec.mp4 | 117 | 256 | 28 | 15 | 12 | 0 | 0 | 0 |
| 23sec.mp4 | 240 | 1563 | 181 | 105 | 74 | 0 | 0 | 0 |
| 20260901_151758.mp4 | 669 | 942 | 45 | 23 | 18 | 3 | 3 | 3 |
| 20260901_152733.mp4 | 683 | 1780 | 58 | 26 | 29 | 7 | 7 | 7 |
| **original five** | **1917** | **4748** | **328** | **174** | **143** | **12** | **12** | **12** |
| vdo.avi | 667 | 3195 | 127 | 60 | 66 | 1 | 1 | 1 |
| vdopov2.avi | 667 | 3198 | 92 | 40 | 50 | 0 | 0 | 0 |
| vdopov3.avi | 667 | 5040 | 193 | 97 | 94 | 0 | 0 | 0 |
| vdopov4.avi | 667 | 1420 | 74 | 33 | 39 | 0 | 0 | 0 |
| **new four** | **2668** | **12853** | **486** | **230** | **249** | **1** | **1** | **1** |
| **all nine** | **4585** | **17601** | **814** | **404** | **392** | **13** | **13** | **13** |

The original-five row is **bit-for-bit `inf_reid`** — 4748 / 328 / 174 / 143 /
12 / 12, and the same per-clip split. The pipeline is unchanged and the old
baseline stands.

Vehicle types on the new footage, COCO fallback (`models.vehicle_classes` is
still null): `vdo` car 62 / truck 3 / bus 1, `vdopov2` car 50, `vdopov3` car 92
/ truck 2, `vdopov4` car 39. **243 of 249 rows are `car`** and no row is
`motorcycle`, which is what a US suburban intersection in winter contains.

### OCR, scored only where there is truth

`scratch/baseline2/score_run.py`, which extends `scratch/tworow/score_labels.py`
with the character accuracy `scripts/eval_ocr.py` defines:

| label | b2_base9 | inf_reid |
|---|---|---|
| MH15HY2237 | `MH15HY2277` 0.90 | `MH15HY2277` 0.90 |
| MH17CY4718 | `MH17CI1478` 0.70 | `MH17CI1478` 0.70 |
| MH15JS4241 | `MH15JS4241` 1.00 | `MH15JS4241` 1.00 |
| MP09ZS0907 | `ML06B0877` 0.40 | `ML06B0877` 0.40 |
| **mean similarity** | **0.750** | **0.750** |
| **exact** | **1/4 = 25.0%** | 1/4 = 25.0% |
| **characters** | **30/40 = 75.0%** | 30/40 = 75.0% |

Unchanged, as it must be. **`data/video_truth.yaml` covers the five original
clips and none of the four new ones**, so the denominator is still 4 plates and
the whole OCR row of this baseline rests on the same four.

### The one plate read on the new footage, and why it is not a read

`vdo.avi` produced one: `AP4H4111`, confidence 0.45, one voted frame, track 8.
The crop is in `scratch/baseline2/read_vdo.png` and it is **not a false positive
of the `23sec.mp4` kind** — the plate detector correctly localised a real US
front plate on a Subaru. It is a **false read**: the plate crop is **50x24px**,
which is 6.2px per character against the ~8px/char floor at which this file
already measured a glyph stops existing in the pixels. The string is invented,
and `min_plate_width` 48 admitted it by 2px.

The plate-less control property survives: **`23sec.mp4` is still 0 reads.**

Why there is nothing else to read is measured rather than assumed
(`scratch/baseline2/crop_scale.py`, median width of the vehicle crops the
pipeline actually wrote):

| | 20 sec | 151758 | 152733 | vdo | vdopov2 | vdopov3 | vdopov4 |
|---|---|---|---|---|---|---|---|
| median vehicle crop width | 224 | 249 | 370 | 155 | 95 | 134 | 103 |

A vehicle 95–155px wide cannot carry a plate crop that clears `min_plate_width`
48 with characters still in it. This is a **resolution ceiling in the footage,
not a plate-detector failure**, and no threshold change reaches it.

### Duplicate physical vehicles — measured, because the plate metric cannot

The benchmark's `duplicate_rows` counts rows whose **plate strings** fuzzy-match.
On footage that reads no plates it is 0 by construction and means nothing, so it
reports 0 for all four new clips and that number must not be quoted as a result.

`scratch/baseline2/dup_probe.py` measures the same question with the only other
descriptor the repo trusts: `app/stitch.py`'s own re-identification embedding,
over the crops the pipeline emitted, at production's `stitch_reid_similarity`
0.97. A pair above it is a pair production's re-id would have merged had it been
offered them.

    all nine clips   392 rows   11853 pairs   13 pairs >= 0.97
                     11 vetoed by the lifetime-overlap rule, 2 outside the window

**All 13 pairs were then reviewed against the crops**
(`scratch/baseline2/dup_pairs_old.jpg`, `dup_pairs_new.jpg`), and **12 of 13 are
the same physical vehicle written twice.** The one that is not is
`vdopov4 8~20` at 0.971 — a dark Subaru and a dark pickup, which is the
ImageNet-embedding ceiling this file already documents.

| | rows | confirmed duplicate rows |
|---|---|---|
| original five | 143 | 6 |
| new four | 249 | 5 |
| **all nine** | **392** | **11 = 2.8%** |

Every confirmed pair overlaps in time except the `vdopov3` 57s one, so this is
the **concurrent fragmentation of a parked vehicle** already listed as open
below — reproduced on new footage and now with a number. `vdopov3` writes one
parked silver pickup **three times** (tracks 1120, 1220, 1554), and the
`20260901_151758` autorickshaw is still the same three rows the re-id section
records.

**11 is a floor, not a total.** The descriptor cannot see one vehicle from two
viewpoints — the grey Kwid scores 0.773 against unrelated pairs at 0.916 — so
duplication of that kind is in these 392 rows and is not in this count.

### What is now measurable, and what is not

Measurable objectively on all nine clips: raw detections, tracker ids, stitches
applied, sightings written, worker health and progress, wall time, vehicle crop
scale, and — via the probe above, reviewed by eye — same-appearance duplicate
rows.

Not measurable on the four new clips, and no amount of running them changes it:
exact OCR, character accuracy, similarity to labels, plate recall, and
false-positive rate in the `23sec.mp4` sense. They have no hand labels, and no
plate in them is legible to a person, so hand labels cannot be created either.
`23sec.mp4` remains the only plate-less control in the set.

## Vehicle detector A/B — fine-tune trained, and NOT adopted

Two candidates were trained from `models/yolo11n.pt` on
`data/vehicle_det_split` (`scripts/train_vehicle_det.py`), kept separately, and
neither is in `config/settings.yaml`:

    models/finetuned/vehicle_det_road_frozen.pt   --freeze 10, backbone kept
    models/finetuned/vehicle_det_road_full.pt     --freeze 0, everything trained

On the dataset's own val split (`scratch/inf/eval_vehicle_det.py`, one scorer
for all three so a COCO model and a 4-class model land on one axis):

| model | mAP50 | mAP50-95 | class-agnostic AP50 | agnostic recall | mean IoU on matches |
|---|---|---|---|---|---|
| `yolo11n.pt` (COCO, in use) | 0.513 | 0.418 | 0.879 | 0.833 | 0.902 |
| `vehicle_det_road_frozen.pt` | **0.838** | **0.744** | **0.992** | **0.974** | — |
| `vehicle_det_road_full.pt` | 0.673 | 0.603 | 0.993 | 0.978 | — |

**Read the class-agnostic column, not the mAP.** The pipeline tracks and crops
anything the detector calls a car, motorcycle, bus or truck, so calling a lorry
a bus costs a `vehicle_type` string and never a sighting. Class-agnostically the
COCO model already finds **95.6%** of the labelled vehicles at IoU 0.5 (217 of
227) with a mean IoU of **0.902**, including 98.9% of boxes under 40px wide and
96.7% in frames holding 8+ vehicles. **Localization was not the problem.** The
COCO model's low class-aware mAP is one bus label in val plus car↔bus↔truck
disagreement with the pseudo-labeller.

Then the same three through the five real clips, everything else identical:

| | `yolo11n` (in use) | road_frozen | road_full |
|---|---|---|---|
| raw detections | 4748 | 5513 | **1870** |
| tracker ids | 328 | 282 | 141 |
| sightings | 148 | 136 | 78 |
| plate reads | 10 | 13 | 10 |
| reads on the 4 Indian clips | 10 | **12** | 8 |
| **false positives on 23sec.mp4** | **0** | **1** | **2** |
| exact OCR | 1/4 | 1/4 | 1/4 |
| mean similarity to hand labels | 0.657 | **0.691** | 0.674 |

`road_full` is **catastrophic forgetting, measured.** Raw detections fall 61%,
and on `20 sec.mp4` they collapse from 207 to 22 — it has forgotten what a
vehicle looks like from the pavement, which is the only viewpoint the app has.
Rejected outright.

`road_frozen` is a real trade and it loses. It gains two Indian reads and 0.034
mean similarity, and in exchange it **breaks the zero-false-positive property**
on the plate-less clip (0 → 1), inflates `20 sec.mp4` from 10 rows to 16 — more
physical-vehicle duplication, the open problem below — and degrades the one
near-correct read in the set, `MH15HY2277` (similarity 0.90) becoming
`KH15HY2207` (0.80). Exact OCR does not move. Its extra reads are strings like
`AA15I0222` and `BR16ZI5175`: yield, not accuracy.

**Decision: `models.vehicle_detector` stays `models/yolo11n.pt`.** Both
candidates are kept for the record. The config swap is one line whenever
`data/vehicle_det` grows a second camera and a street-level viewpoint.

`app/detect.py` was changed to make that swap possible at all: it now builds its
class map from the **loaded model's own `names`** instead of hardcoded COCO ids,
because a 4-class fine-tune emits 0–3 and the old id-keyed map would have turned
every car into `unknown` while asking the model for classes 5 and 7 that do not
exist. Verified neutral on the COCO model — the baseline benchmark reproduces
148/10/10/10/0/169 and 1-of-4 exactly.

## Plate detector A/B, under the current filters

Both runs are the full benchmark, identical except for `models.plate_detector`.

| detector | rows | reads | reads on Indian clips | false positives on 23sec | exact OCR | mean similarity to labels |
|---|---|---|---|---|---|---|
| `plate_det_scenes.pt` (fine-tuned) | 148 | 10 | **10** | **0** | 1/4 | **0.658** |
| `plate_detector.pt` (pretrained) | 148 | 19 | 9 | 10 | 1/4 | 0.649 |

The pretrained model's higher raw yield is entirely false positives: 10 of its
19 reads come from the clip where no plate exists. **Winner: the fine-tuned
scene model**, which is what `config/settings.yaml` already points at. It is
kept. The pretrained model's only advantage is on the two-row motorcycle plate
(0.63 vs 0.465 similarity), and both read it wrong.

**Re-run 2026-09-02 at the lowered `plate_conf`**, on the 859 frozen vehicle
crops rather than through the full pipeline, so the two detectors see identical
pixels (`scratch/inf/plate_sweep.py`). Crops where a box survives every filter:

| detector | conf | kept, Indian clips | kept, `23sec.mp4` (all false) |
|---|---|---|---|
| `plate_det_scenes.pt` | 0.25 | 20 | **0** |
| `plate_det_scenes.pt` | **0.12** | **36** | **0** |
| `plate_detector.pt` | 0.25 | 17 | 18 |
| `plate_detector.pt` | 0.12 | 21 | 27 |

The gap **widens** at the lower floor: the fine-tune nearly doubles its yield
while staying at zero false positives, and the pretrained model is dominated at
every operating point. Raising `plate_imgsz` to 960 or to the crop's native
size was also tried and is worse for the fine-tune (36 → 8 and → 16 kept),
because at higher input resolution it returns smaller boxes that then fail
`min_plate_width`. The A/B is not close, and the decision is unchanged.

## Plate recall and OCR — measured 2026-09-02

Three changes were tried against the five clips. Two were adopted, one was
measured and rejected. Nothing about the detectors changed for any of them.

### Adopted: `plate_conf` 0.25 -> 0.12

The confidence floor was doing far less work than assumed. Swept over 859
frozen vehicle crops harvested from the five clips
(`scratch/inf/harvest_crops.py`, `scratch/inf/plate_sweep.py`), counting crops
that survive **every** filter, Indian clips against the plate-less one:

| plate_conf | 0.25 | 0.15 | 0.12 | 0.10 | 0.08 | 0.06 |
|---|---|---|---|---|---|---|
| kept, Indian clips | 20 | 32 | **36** | 37 | 39 | 43 |
| kept, `23sec.mp4` (all false) | **0** | **0** | **0** | **0** | 2 | 3 |

**The shape and edge-density gates are what suppress false positives, not the
confidence floor** — which is why it could be halved with the plate-less clip
staying at exactly 0. False positives appear at 0.08, so 0.12 sits two steps
clear and takes 36 of the 37 available. On real video: **plate reads 10 -> 12,
false positives still 0.**

### Adopted: two-row plates, by rearrangement rather than by splitting

`two_row_split` was off because deciding *which* plates to split from the shape
of the box is impossible — the genuine two-row plate in this footage has a box
aspect of 1.51 and two single-row plates measure 1.31 and 1.89. Two findings
changed it:

1. **Reading the halves separately cannot work, and the OCR model is why.**
   The recogniser emits a fixed slot count, so handed a five-character row it
   invents eight. Over **30** combinations of cut point, trim and upscale on the
   known `MH17CY4718` crop (`scratch/inf/tworow_probe.py`) the halves read as
   `MH4K7017` / `TR4T1888` and **none of the 30 produced the plate.** What works
   is laying the two rows **side by side** into one line.

   The reason given here used to be "the model has only ever seen single-row
   plates", and that is **not true of the model actually in use**. `models.ocr`
   is `models/finetuned/ocr_best.pt`, and `scripts/gen_synthetic_plates.py`
   renders 18% of its 40000 synthetic crops as stacked two rows (~7080 images,
   measured 17.7%), plus the 23 real two-row crops in the training split. The
   fine-tune has seen the stacked form ~7100 times. It reads it badly anyway —
   **5.3% exact on the 19 two-row test crops against 67.1% on the 234
   single-row ones** — and the audit in TRAINING.md shows why: `render()` splits
   every long plate at exactly 4 characters, so the model has practice at one of
   the five layouts real plates use and scores 0.000 on the other four. The
   rearrangement earns its place on the measurement, not on the explanation.
2. **The grammar decides, asymmetrically.** Both readings run; the rearranged
   one is taken only when it parses as a real registration *and the one-line
   read does not*. `MH77AVX183` is not a plate in any layout; `MH17CI4718` is.
   A plate already read as a valid registration is untouchable, which is what
   makes this safe to leave on.

### Rejected: `plate_preprocess: greynorm_x2`

Per-crop it looks clearly best — mean similarity 0.888 against native's 0.755
over the three labelled plates (`scratch/inf/preproc_probe.py`). On the full
benchmark it is a regression: **0.750 -> 0.708.** The probe reads one view once;
the pipeline reads five and votes, and normalising each view independently makes
the five more alike — it removes the exposure differences that made them
independent opinions. Kept in the code, switched off, with the number.

Also rejected on measurement: `plate_samples` 5 -> 8 -> 12 (mean similarity
0.750 -> 0.725 -> 0.716; a wider pool dilutes the vote with poorer views), and
`min_plate_width` 48 -> 40 -> 32, which raises reads 12 -> 14 -> 15 with no new
false positives but moves no accuracy metric and **damages a read that already
worked** — track 44 of `20260901_151758` degrades from `TN1KZI6575` at 0.42
confidence to `TS22Z2215` at 0.18, because narrower crops displace better views
inside the vote.

### Why exact OCR is still 1 of 4

`scratch/inf/ocr_ceiling.py` separates the three possible causes. **It is not
resolution and it is not sampling.** Every labelled plate is above the ~8px per
character floor at which a glyph stops existing in the pixels:

| label | best read | similarity | crop | px/char | verdict |
|---|---|---|---|---|---|
| MH15JS4241 | `MH15JS4241` | 1.00 | 126x42 | 12.6 | exact |
| MH15HY2237 | `MH15HY2277` | 0.90 | 66x35 | 13.2 | resolved, misread |
| MH17CY4718 | `MH17CI1478` | 0.70 | 139x92 | 27.8 | resolved, misread |
| MP09ZS0907 | — | 0.40 | — | — | never read |

`MH15HY2237` is sharp (Laplacian variance 4951) and well resolved at 13.2px per
character, and one digit is still wrong. **The remaining error is the OCR
model's character discrimination, so the next real gain is Fine-tune 2 and not
another threshold.** That fine-tune was then retrained — see "OCR fine-tune 2,
six two-row layouts" below. It did not move this number either, and the reason
it did not is now measured rather than assumed.

The fourth is a different failure and worth separating: `MP09ZS0907` is never
read at all. The row scoring closest to it is a **different vehicle** whose
yellow commercial plate was cropped mid-string — the contact sheet in
`scratch/inf/clip152733_plates.png` shows it reading `MH17 T` with the rest of
the plate outside the crop. Two of the seven plate crops in that clip are not
plates at all but tail-light clusters. Those are plate-detector failures wearing
an OCR costume, and no OCR change addresses them.

## OCR fine-tune 2, six two-row layouts — trained 2026-09-03, NOT adopted

The one open lead from the previous pass was that `gen_synthetic_plates.py`
split every two-row plate at exactly 4 characters, so of the five layouts real
plates use the model had practice at one and scored 0.000 on the other four.
The synthetic set was regenerated with **six** layouts — split4/5/6/7 plus two
stacked-column forms — and the model retrained from scratch on it. Nothing else
changed: same architecture, same 40000/1179×8 mix, same 18% two-row fraction,
same hyperparameters (40 epochs, batch 128, lr 3e-3, seed 1337), same scorer.

    data/ocr/synth   40000 crops   7108 two-row = 17.8%
    split4 2127 (29.9% of two-row)   split5 1579 (22.2%)   split6 1448 (20.4%)
    split7  700 ( 9.8%)   stacked_district 688 (9.7%)   stacked_state 566 (8.0%)

Trained as `models/finetuned/ocr_layouts.pt`, run log `runs/ocr_20260903_030830`,
best epoch 37 of 40. Production `models/finetuned/ocr_best.pt` was not touched
and `config/settings.yaml` still points at it.

### It improved everything except the thing it was for

`scripts/eval_ocr.py --by-layout`, both models over the identical splits
(`scratch/tworow/eval_baseline.log`, `scratch/tworow/eval_layouts.log`):

| | `ocr_best.pt` | `ocr_layouts.pt` |
|---|---|---|
| test exact, all 252 | 62.7% | **64.3%** |
| test chars | 88.7% | **91.2%** |
| test single-row exact, n=233 | 67.4% | **69.1%** |
| **test two-row exact, n=19** | **5.3%** | **5.3%** |
| test two-row chars | 53.5% | **62.7%** |
| val exact, all 252 | 66.7% | **69.8%** |
| val single-row exact, n=231 | 71.0% | **74.9%** |
| **val two-row exact, n=21** | **19.0%** | **14.3%** |
| val two-row chars | 75.2% | 73.8% |

Per layout, exact, and this is where the tiny sample has to be read honestly —
one plate is 5.3 points on test and 4.8 on val:

| layout | test n | before | after | val n | before | after |
|---|---|---|---|---|---|---|
| split4 | 10 | 10.0% | **0.0%** | 18 | 22.2% | **16.7%** |
| split5 | 3 | 0.0% | **33.3%** | 0 | — | — |
| split6 | 2 | 0.0% | 0.0% | 2 | 0.0% | 0.0% |
| split7 | 1 | 0.0% | 0.0% | 0 | — | — |
| stacked | 3 | 0.0% | 0.0% | 1 | 0.0% | 0.0% |

**Two-row exact did not move.** Test is 1 of 19 both times — the plate simply
changed layout, split4 losing one and split5 gaining one — and val went
backwards by one plate. The layouts the retrain was built to buy, split6,
split7 and stacked, are still at 0.000 on every one of the 9 crops that carry
them. What did move is two-row *character* accuracy on test, 53.5% → 62.7%: the
reads are closer without any of them becoming right.

### Real video is bit-for-bit the same decision

`scratch/bench_realvideo.py --tag inf_ocr2`, only `models.ocr` swapped:

| | `ocr_best.pt` (inf_reid) | `ocr_layouts.pt` (inf_ocr2) |
|---|---|---|
| sightings written | 143 | 143 |
| plate crops / reads | 12 / 12 | 12 / 12 |
| false positives on `23sec.mp4` | 0 | 0 |
| exact OCR vs hand labels | 1/4 = 25.0% | 1/4 = 25.0% |
| **mean similarity to hand labels** | **0.750** | **0.750** |

Per hand-labelled plate (`scratch/tworow/score_labels.py`):

| label | before | after |
|---|---|---|
| MH15JS4241 | `MH15JS4241` 1.00 | `MH15JS4241` 1.00 |
| MH15HY2237 | `MH15HY2277` 0.90 | `MH15BY2237` 0.90 |
| MH17CY4718 | `MH17CI1478` 0.70 | `MH17CY6713` **0.80** |
| MP09ZS0907 | `ML06B0877` 0.40 | `DS03R0247` **0.30** |

The two moves cancel to three decimal places. `MH17CY4718` is the two-row plate
and it does get closer, 0.70 → 0.80 — the only real-video sign the layout work
did anything. `MP09ZS0907` moving 0.40 → 0.30 is noise on a plate that is
**never detected**: both numbers are the score of a different vehicle's row that
happens to sit nearest it, which is a plate-detector failure and not an OCR one.

### Decision

**Not adopted. `models.ocr` stays `models/finetuned/ocr_best.pt`.** The gate was
a material improvement in two-row OCR, and there is none: flat on test, one
plate worse on val, and zero on all three layouts the regeneration existed to
teach. `ocr_layouts.pt` is kept beside the production weights for the record.

Separately and honestly: this model **is** better at the job overall — +1.6
points test exact, +3.1 val, +2.5 char, +1.7 on single-row — at exactly zero
real-video cost. Adopting it for that is a different decision on a different
criterion, and it is not the one that was asked for here.

### What the measurement says to do next

The failure is not layout coverage any more, because layout coverage was
supplied and bought nothing on the four hard layouts. What remains is sample
size: **12 distinct two-row plates in train against 605 single-row**, and 9
crops across split6, split7 and stacked in the entire val+test set. A metric
built on 19 and 21 crops cannot resolve an improvement smaller than one plate,
and a training signal of 12 plates cannot reliably produce one. Real two-row
crops are the next move, and the count needed is now a measured requirement
rather than a guess.

## `data/ocr_two_row_real` — 77 supplied real two-row crops, audited 2026-09-03

The move the section above named as next. Supplied as `data/ocr_two_row_real/`:
image files beside a `labels.txt` of `filename<TAB>PLATESTRING`, one line per
crop, and every image is a confirmed two-row plate — that is given and was not
re-litigated. What was audited is whether they can be *trained on honestly*
(`scratch/tworow2/audit_two_row_real.py`).

    image files 78    label lines 77    byte-identical duplicates 0
    near-duplicate images (phash <= 6) 0
    69 distinct plate strings over 77 labelled crops; 8 plates carry 2 crops
    width  min 72  median 205  max 1366        below min_plate_width 48: 0
    px/char min 7.2  median 24.3               under 8 px/char: 1
    image-level overlap with data/ocr: 0 pairs over 1683 existing crops

Five findings decide how they are used, and four of them are problems:

- **14 of the 69 plate strings are already in `data/ocr/val` or
  `data/ocr/test`.** `MH04DW9020` is in val seven times, `MH01BU1852` in test
  six. Adding those crops to train would put a registration on both sides of
  the split, and every two-row number after that would be a memorisation score.
- **27 of the 77 are Azerbaijani plates, not Indian** — `AZ` roundel, `10-EX-500`
  shape. They are real two-row plates and stay in the experiment, but whether
  a `\d\d[A-Z]{2}\d{3}` grammar helps or hurts a model whose slot heads are
  learning `[2 letters][1-2 digits][1-3 letters][4 digits]` is a question to
  measure, not assume, so origin is a column and one run drops them.
- **Five labels disagree with their own image.** `MP07GA7533` is `MP.07 GA.1533`;
  `KL10AG749` is `KL.10 AG.7249` and drops a digit; `MH20ELJ9991` is
  `MH.20.EU 9991` and reads the U as LJ; `M13S0155` is `ML13S 0155` and drops
  the L; `TOZE674` is `10 ZE674` with the 1 read as T and the 0 as O. The last
  three are registrations `data/ocr/test` already carries under a better
  transcription, which is how they were caught.
- **One image has no label** — `Screenshot 2026-09-03 045004.png`. It is a
  second photograph of `10EX500`, already supplied, so it is excluded rather
  than given an invented label.
- **Five crops carry annotation-tool chrome burned into the pixels** — the
  `0504xx` block has selection handles, a cyan box overlay and in one case a
  toolbar strip. All five are re-captures of plates supplied earlier in the
  set.

`labels.txt` as supplied is never modified. The corrections live in
`scratch/tworow2/corrections.csv` with the reason for each and are applied by
`scratch/tworow2/prep_two_row_real.py`, which writes the split.

### The split — plate-disjoint, and it has to be

`data/ocr_two_row_real/split.csv`, plus `train_labels.txt` and
`heldout_labels.txt` in the same shape `scripts/eval_ocr.py` reads:

| | crops | plates | Indian | Azerbaijani | nonstandard |
|---|---|---|---|---|---|
| train | 46 | 41 | 25 | 19 | 2 |
| heldout | 31 | 28 | 20 | 8 | 3 |

The rule: a plate already in `data/ocr/val` or `data/ocr/test` can never reach
train and goes to heldout; a plate already in `data/ocr/train` goes to train; a
brand-new plate is assigned by a hash of its own string, so all its crops land
together and the assignment is the same on every run. **`data/ocr/train`,
`val` and `test` are left byte-identical**, so every number in the tables above
this line stays comparable.

The heldout 31 are the point. No registration in them appears anywhere in
training, they are all real two-row plates, and they are a bigger two-row set
than the 19 in `data/ocr/test` that CLAUDE.md already called too small to
resolve one plate.

### How they were incorporated

`scripts/train_ocr.py` grew seven flags, all defaulting to off, so
`python scripts/train_ocr.py` with no arguments still builds exactly the run
that produced `ocr_best.pt`:

    --two-row-repeat N          times the 46 supplied train crops repeat per epoch
    --two-row-origins ...       indian / azerbaijan / nonstandard, comma separated
    --existing-two-row-repeat N extra repeats of the 22 two-row crops already in
                                data/ocr/train, on top of --real-repeat
    --two-row-stitch P          probability a two-row crop is presented with its
                                rows laid side by side, the way app/ocr.py feeds
                                one at inference when two_row_split is on
    --two-row-jitter P          probability a two-row crop has its rows re-laid:
                                new split point, sideways offset, height ratio
    --init W                    warm start from an existing checkpoint
    --select val|blend          blend = half val exact, half val two-row exact

Oversampling by repetition rather than a weighted sampler, because with 46
crops against 49432 a sampler needs a weight anyway and a repeat count is the
same statement in a number that lands in the run log. At `--two-row-repeat 24`
plus `--existing-two-row-repeat 16` the epoch is 50888 samples of which 1632,
**3.2%**, are real two-row — 17% of the real, non-synthetic portion.

Two two-row scores print every epoch: `val2r`, the 21 two-row crops inside
`data/ocr/val`, comparable to every earlier run; and `held2r`, the 31 supplied
crops, which no run ever selects on.

### Was the architecture the problem? Measured: no

Before spending runs on data, the obvious architectural suspect was tested.
`PlateNet.forward` collapses height with `x.mean(dim=2)` before the GRU reads
the width axis, which on a stacked plate superimposes the top row onto the
bottom one. If that destroyed vertical order the model could not represent a
two-row layout at all, and no amount of data would help.

`scratch/tworow2/height_probe.py` reads every two-row crop twice: once as it
is, once with its two horizontal halves swapped. A model blind to vertical
order must read them the same.

| | single-row control | two-row, data/ocr/test | two-row, heldout |
|---|---|---|---|
| `ocr_best.pt` similarity | 0.772 | **0.229** | **0.176** |
| `ocr_layouts.pt` similarity | 0.814 | **0.268** | **0.220** |
| identical reads | 16/60, 13/60 | 0/19 | 0/31 |

Swapping the rows changes the read completely and never once produces the same
string. **The architecture resolves vertical order and is not the obstruction**,
so it was left alone.

### Six controlled experiments, and every one of them

All in `models/finetuned/`, all kept, none in `config/settings.yaml`. The
from-scratch control is `ocr_layouts.pt` rather than `ocr_best.pt`:
`data/ocr/synth` on disk is the six-layout regeneration, so a from-scratch run
today reproduces `ocr_layouts`'s ingredients, not `ocr_best`'s.

| | test exact | test single | test two-row | val two-row | heldout Indian chars | heldout exact |
|---|---|---|---|---|---|---|
| `ocr_best.pt` — production | 62.7% | 67.4% | 5.3% | **19.0%** | 30.3% | 0/31 |
| `ocr_layouts.pt` — from-scratch control | 64.3% | 69.1% | 5.3% | 14.3% | 40.5% | 0/31 |
| E1 `ocr_tworow_mix.pt` scratch, x24 | 59.1% | 63.5% | 5.3% | 4.8% | 41.5% | 1/31 |
| E2 `ocr_tworow_warm.pt` warm, x24 | 64.7% | **70.0%** | 0.0% | 14.3% | 41.0% | 0/31 |
| E3 `ocr_tworow_warm_in.pt` warm, Indian only | **65.1%** | **70.0%** | 5.3% | 9.5% | 42.6% | 0/31 |
| E4 `ocr_tworow_stitch.pt` warm + stitch | 64.7% | 69.5% | 5.3% | 9.5% | 40.5% | 0/31 |
| E5 `ocr_tworow_jit.pt` warm + jitter + stitch | 64.3% | 69.1% | 5.3% | 9.5% | 41.0% | 0/31 |
| E5b `ocr_tworow_jit_last.pt` same run, final epoch | 63.5% | 68.2% | 5.3% | **0.0%** | **43.6%** | **2/31** |
| E6 `ocr_tworow_jit_in.pt` E5 Indian only | 63.5% | 68.2% | 5.3% | 4.8% | 37.9% | 0/31 |

E5b is the same run as E5 under a different fixed rule — train the schedule,
take the final epoch — because `--select blend` kept landing on epoch 1 and
never saved the epochs where the heldout moved. It consults the heldout no more
than the blend selector does.

**Character accuracy moves and exact accuracy does not.** Heldout Indian
character accuracy climbs 30.3% to 43.6%, and 10 of those 13 points are bought
by `ocr_layouts.pt`, which never saw one of these 77 crops. The remaining ~3
points are what the supplied data adds. Exact match over the same crops moves
from 0 to 2 in the single best case.

### The pooled two-row count is the number that decides it

Three held-out two-row sets exist now — 19 in `data/ocr/test`, 21 in
`data/ocr/val`, 31 supplied. Pooled, every model reads between two and five of
71 correctly:

| model | test | val | heldout | total |
|---|---|---|---|---|
| **`ocr_best.pt`** | 1/19 | **4/21** | 0/31 | **5/71 = 7.0%** |
| `ocr_layouts.pt` | 1/19 | 3/21 | 0/31 | 4/71 |
| `ocr_tworow_warm_in.pt` | 1/19 | 2/21 | 0/31 | 3/71 |
| `ocr_tworow_stitch.pt` | 1/19 | 2/21 | 0/31 | 3/71 |
| `ocr_tworow_jit.pt` | 1/19 | 2/21 | 0/31 | 3/71 |
| `ocr_tworow_jit_last.pt` | 1/19 | 0/21 | **2/31** | 3/71 |
| `ocr_tworow_jit_in.pt` | 1/19 | 1/21 | 0/31 | 2/71 |

**The production model is the best two-row reader in the table**, and every
model trained on the supplied crops is worse. A gap of two plates in 71 is not
resolvable, so the honest reading is not "ocr_best wins" but **nothing here is
distinguishable from anything else, and none of it is an improvement**.

And E5b's two heldout hits are **one registration, not two**: `BR33T4980`
photographed twice, both copies in heldout. Every candidate reads at most one
distinct new two-row plate correctly.

### Real video is unchanged, and the best candidate is worse

`scratch/tworow2/bench_ocr.py` swaps `models.ocr`, runs
`scratch/bench_realvideo.py` over the five clips, and restores `settings.yaml`
including on a crash. Everything but the OCR weights is identical.

| | `ocr_best` (inf_reid) | E5b (tw_jitlast) | E3 (tw_warmin) |
|---|---|---|---|
| sightings / plate crops / reads | 143 / 12 / 12 | 143 / 12 / 12 | 143 / 12 / 12 |
| stitches / tracker ids / raw detections | 174 / 328 / 4748 | identical | identical |
| false positives on `23sec.mp4` | 0 | 0 | 0 |
| exact OCR vs hand labels | 1/4 | 1/4 | 1/4 |
| **mean similarity to hand labels** | **0.750** | **0.725** | **0.750** |

Per hand-labelled plate:

| label | `ocr_best` | E5b | E3 |
|---|---|---|---|
| MH15JS4241 | `MH15JS4241` 1.00 | `MH15JS4241` 1.00 | `MH15JS4241` 1.00 |
| MH15HY2237 | `MH15HY2277` 0.90 | `MH15BY2237` 0.90 | `MH15BY2237` 0.90 |
| MH17CY4718 — the two-row one | `MH17CI1478` 0.70 | `MH17CV6713` 0.70 | `MH17CY6713` 0.80 |
| MP09ZS0907 — never detected | `ML06B0877` 0.40 | `KL09B1377` 0.30 | `UP29AD0880` 0.30 |

The candidate that is best on the heldout two-row crops is the one that
**regresses** real video, 0.750 to 0.725. E3 holds real video exactly level and
its 0.80 on the two-row plate is the same 0.80 `ocr_layouts.pt` already produced
without any of this data.

### Decision

**Not adopted. `models.ocr` stays `models/finetuned/ocr_best.pt`.** The gate was
a material improvement in real two-row recognition. There is none: two-row exact
is 5.3% on test for every candidate as it was before, the pooled count over all
71 held-out two-row crops goes down rather than up, no candidate reads more than
one distinct new two-row registration, and the one that moves the heldout
furthest loses every val two-row plate and 0.025 of real-video similarity doing
it. All six candidates are kept beside the production weights.

Stated separately and honestly, as with `ocr_layouts.pt`: **`ocr_tworow_warm_in.pt`
is a better model overall** — test exact 65.1% against 62.7%, single-row 70.0%
against 67.4%, characters 91.5% against 88.7%, real video exactly level. Adopting
it would be a decision on a different criterion than the one asked for here, and
it is not made in this pass.

### Known limitations of this result

- **The training signal is 41 plates, of which 25 are Indian.** Up from 12, and
  still small. Indian-only training (E3, E6) is not better than mixed on the
  Indian heldout crops, so the 19 Azerbaijani crops are neither the problem nor
  the fix.
- **The evaluation cannot resolve what is being asked of it.** 71 held-out
  two-row crops, and the spread across every model in the table is three plates.
  The supplied 77 raised the pool from 40 to 71, which is real progress on the
  measurement even though it did not move the model.
- **Five of the 77 labels were wrong and one image was unlabelled**, corrected in
  `scratch/tworow2/corrections.csv` rather than in `labels.txt`. A 6.5% label
  error rate in a 77-sample targeted signal is itself a limit on what that signal
  could teach.
- **27 of the 77 are Azerbaijani.** The effective Indian two-row addition is 50
  crops over 43 registrations, not 77.
- **`heldout stitched` is 0/31 for every model**, including on the rearranged
  input production actually feeds a two-row plate. Training on the rearranged
  form (E4) moved its character accuracy 25.5% to 35.3% and its exact count not
  at all.

## Row rearrangement, inference only — measured 2026-09-03, NOT adopted

The question the fine-tunes above kept failing to answer from the training
side, asked from the inference side instead: **can the existing OCR read a real
two-row plate if the two rows are rearranged into one line first?**

No training, no weights changed, `config/settings.yaml` untouched.
`models/finetuned/ocr_best.pt` reads the 77 curated crops of
`data/ocr_two_row_real` five ways, and the arms differ only in what picture the
model is handed. `scratch/tworow3/tworow_rearrange.py`, log in
`scratch/tworow3/experiment.log`, per-crop rows in `results.csv`.

The corrections in `scratch/tworow2/corrections.csv` are applied, so this is
the same 77 crops and the same labels the fine-tune pass used, and the numbers
sit on the same axis.

### The answer is no

| arm | exact | chars | similarity | grammar-valid |
|---|---|---|---|---|
| A the crop as it is | **7.8%** (6/77) | 28.5% | 0.352 | 42/77 |
| B rows rearranged | 2.6% (2/77) | 31.4% | 0.401 | 48/77 |
| B0 same crop localised, **not** split | 6.5% (5/77) | **41.0%** | **0.500** | 39/77 |
| C production's fixed cuts, best of three | 2.6% (2/77) | 31.4% | 0.392 | 54/77 |
| D what the app emits today | **7.8%** (6/77) | 31.8% | 0.389 | 74/77 |

**Exact accuracy falls, 6 plates to 2.** Rearranging turns two plates the model
read correctly into near-misses — `MH02FD6534` becomes `MH02MT4534`,
`MH18AA1002` becomes `MH18AA0022` — and buys back one, `MH01BU1852`, which is
the single crop in 77 where rearrangement is the only arm that gets it right.
Per crop against the same crop unsplit, rearranging is better on 22 and worse
on 49.

Character accuracy and similarity do rise, which is the same pattern every
two-row intervention in this repo has produced: the reads get closer without
becoming right. The distribution says it plainly — reads that are one or two
characters out go 2 to 6, and exact goes 6 to 2.

### Two thirds of B's apparent gain is not the rearrangement

B does two things at once, and the control that separates them is the finding.
The curated crops are **not tight on the plate** — on `KA21M5519` the plate
occupies the lower half and car body fills the upper half — so B localises the
plate before splitting it. Localising **alone**, changing nothing else, is
worth more than the split it was there to enable:

    crop as it is        chars 28.5%   similarity 0.352
    localised, not split chars 41.0%   similarity 0.500
    localised and split  chars 31.4%   similarity 0.401

Cutting each crop down to the region its character strokes occupy is the
largest single move any two-row change in this file has produced, and it is
free — no training, no new model, one deterministic pass over the pixels.
Splitting the localised plate then gives back most of it.

### Why, and it is the input layer

`models/finetuned/ocr_best_plate_config.yaml`: input **128x64**,
`keep_aspect_ratio: False`. That is 2:1, and **a stacked two-row plate is about
2:1**. The crop this model is worst at is the one whose shape already matches
its input; a side-by-side strip is nearer 6:1 and gets squashed to reach
128x64. The rearrangement is fighting the resize, which is why it can move
characters closer while moving exact matches away.

This also explains the flat `--two-row-stitch` result (E4) above without
needing the sample size to explain it.

### The boundary finder, and how it was wrong first

The deterministic split is a stroke-energy profile — `|Sobel_x|` summed per
row, smoothed — with two rules over it: the valley between the rows, and the
midpoint between the two heaviest bands of ink. Both agree with each other
(mean |difference| 0.036, within 0.05 on 69/77) and **both were wrong on half
the crops**, because a smooth car body is quieter than the gap between two rows
of characters and the boundary walked off into the bodywork. Only 27 of 77 cuts
landed inside the 0.44–0.56 band that production's fixed cuts span, and the
arm scored *below* a blind halving.

`scratch/tworow3/cut_check.py` draws the boundaries without running OCR, which
is what caught it. Localising the plate first fixes it, and the arm then beats
every fixed cut — 0.401 against 0.391 — but still loses to not splitting at
all.

`scratch/tworow3/cut_ablation.py` separates cut placement, trim, and the fact
that production reads three images where B reads one, over thirteen
single-read variants. The oracle row — the best of every rearrangement, scored
against the answer, which nothing at inference could ever pick — reaches 7.8%
exact, which only **ties** reading the crop as it is. There is no selection
rule over these rearrangements that would have won.

### What still fails

- **Azerbaijani crops are 0/27 exact in every arm**, and their mean similarity
  under localisation is 0.535 — as high as the Indian crops. The model produces
  Indian-shaped strings for a `10-EX-500` layout, so the grammar is not the
  limit; there is no configuration of the reading that fixes a plate the
  alphabet and slot layout were not built for.
- **The five near-misses under the best arm are single-character errors**, and
  they are the same discrimination failures CLAUDE.md already names:
  `MH04JL5547` → `MH04JV5547`, `MH12SF3212` → `MH12SF3223`, `MH04DW9020` →
  `MH04DFV020`, `KL10AG7249` → `KL40AG7249`. No rearrangement addresses those.
- **36 of 77 are still unrecognisable** (similarity below 0.5) even localised,
  down from 59 unlocalised.

### Decision

**Not adopted. `models.ocr` stays `models/finetuned/ocr_best.pt` and
`config/settings.yaml` is unchanged** — `two_row_split` is left exactly as it
was, `true`, with the same fixed cuts and the same asymmetric grammar gate,
because arm D shows that gate costing nothing: production emits the same 6
exact reads as reading the crop plainly, and only replaces a read the grammar
has already rejected.

The one result worth acting on is not the rearrangement. **Localising the plate
inside its crop before OCR moves character accuracy 28.5% → 41.0% and
similarity 0.352 → 0.500 on this set with no model change**, and that is a
change to `app/ocr.py` that this pass deliberately did not make: it must be
measured on `scratch/bench_realvideo.py` first, where pipeline plate crops are
already tighter than these curated ones and the gain may not survive.

## Vehicle type classifier — retested 2026-09-02, still not adopted

**`data/vehicle_cls` did not change.** Counts are identical to the table in
TRAINING.md and every file is dated 2026-08-31, before this dataset drop. The
only new data is `data/vehicle_det`, which is detection data.

The current classifier's numbers were re-derived independently
(`scratch/inf/cls_experiment.py --eval`) and reproduce TRAINING.md exactly:
accuracy 450/627 = 71.8%, macro F1 0.621, truck F1 0.000, auto called car 0/100.

### Can `data/vehicle_det` fix the truck collapse? Measured: no.

Only part of it is even usable as classification truth, and the exclusions are
the finding (`scratch/inf/det_as_cls.py`):

- **`car` is contaminated with autorickshaws.** The pseudo-labeller is two COCO
  models and COCO has no auto, so autos are labelled `car`. 85 of 445 `car`
  boxes (19.1%) are classified `auto` by a model that never calls an auto a car
  on its own val set; frames 1231, 1232 and 1235 are unmistakable yellow autos
  by eye. Training on them attacks auto-vs-car, the one thing this classifier
  gets right.
- **`motorcycle` is unusable.** 560 of 584 boxes are under 48px.
- That leaves **75 truck and 21 bus** crops.

Those 96 were added to train — val left byte-identical so the comparison is on
one axis — and the model retrained as `models/finetuned/vehicle_cls_aug.pt`:

| class | current F1 | +vehicle_det F1 |
|---|---|---|
| auto | 0.829 | **0.883** |
| bus | 0.733 | 0.764 |
| car | 0.630 | 0.650 |
| motorcycle | **0.914** | 0.889 |
| **truck** | **0.000** | **0.073** |
| accuracy | 0.718 | 0.737 |
| macro F1 | 0.621 | 0.652 |

Truck moves from recall 0.000 to **0.040** — 4 of 100, with 64 still called bus.
That is not a fix and could not have been one: 75 crops from one Indian camera
cannot repair a train/val **collection** mismatch, which is what the collapse
actually is.

### The real-video test settles it

Run through the five clips with the classifier enabled, `23sec.mp4` — a US
parking lot containing nothing but ordinary cars — goes from

    fallback (COCO)   car 72, truck 4
    classifier        truck 42, car 32, bus 2

The truck/bus collapse is not a val-set artifact; it destroys type labels on
real footage. Against that, the classifier does earn its one real win: on
`20260901_151758.mp4` it labels the black-and-yellow autorickshaw **`auto` (5
rows)**, which COCO cannot do at any threshold.

**Decision: `models.vehicle_classes` stays `null`.** Adopting it would trade 72
correct `car` labels for 5 correct `auto` ones — precisely the "do not sacrifice
a class that works" trap. `vehicle_cls_aug.pt` is kept as the better of the two
candidates for whenever the truck data is fixed; the fix is still the one
TRAINING.md already names — hand-sorting `crops/unsorted/` (now 1354 real crops
from this project's own footage) so train and val are one collection.

## adopt(): identity pollution, measured and fixed -- 2026-09-03

The open problem the "Concurrent fragmentation" section ends on: **a tracker id
that genuinely covers two vehicles**, caused upstream in `adopt()` binding a new
raw id to a track that has moved on. It now has a number and a fix.

### The rate, on footage this project has never tuned on

`scratch/final/adopt_replay.py` feeds `data/vehicle_mot_output` through the real
`app/stitch.py:TrackStitcher`, mirroring the worker's alias map, `min_hits`,
`track_timeout_frames` and retirement TTL, at `frame_skip` 3.

The audit needs no labels, and that is why this dataset can be used for it. It
rests on physics: **after `adopt()` binds raw id B to track A, if the tracker
goes on to report A and B in the same frame on boxes that barely touch, they are
two vehicles and the adoption was wrong.** Low overlap is the necessary
qualifier — two ids on one box in one frame is the concurrent-fragmentation case
`footprint_twin` exists for, so co-occurrence alone proves nothing and
co-occurrence *far apart* proves everything. The bar is IoU < 0.30.

    31575 processed frames   8633 adoptions   313 proved wrong = 3.6%

### Nothing at the moment of the decision separates them

`scratch/final/adopt_analyse.py`, over the same 8633:

| | overlap p50 | size ratio p50 | centre distance p50 | gap p50 |
|---|---|---|---|---|
| the 313 proved wrong | 0.000 | 0.865 | 1.185 | 2 |
| the 8320 others | 0.000 | 0.877 | 1.344 | 5 |

The distributions are the same, and the median overlap is 0.000 in **both**
groups because a moving vehicle across a gap does not overlap its own last box —
which is the entire reason `_score` does not gate on overlap when `gap > 0`. An
overlap floor is therefore not available:

| floor on gap>0 adoptions | adoptions kept | wrong ones removed |
|---|---|---|
| 0.05 | 3621 of 8633 | 189 of 313 |
| 0.30 | 2391 of 8633 | 227 of 313 |

Removing 5000 correct adoptions to remove 189 wrong ones is not a trade. **So
the decision rule was left exactly as it is, and no threshold was moved.**

### The fix uses the evidence that arrives later, and only ever splits

`app/stitch.py:revoke_aliases`, gated on `stitch_alias_revoke`. When the tracker
contradicts an adoption — reporting both raw ids in one frame on boxes that are
not the same box — the alias is revoked and that raw id becomes its own track.
The test for "not the same box" is **`adopt`'s own same-frame rule**, `overlap`
(0.55): below it `adopt` would have refused the pair in the first place, above it
they are one object wearing two ids and must not be broken up. **No threshold
here is new and none is loosened.** A freed id is never adopted again — the
tracker has proved it an independent vehicle.

This can only split a track that was merged. It cannot merge two that were not,
so it cannot create a false merge.

Measured over the same 31575 frames (`scratch/final/adopt_pollution.py`), where
a row is POLLUTED if any two raw ids it kept were ever reported in one frame at
IoU < 0.30, and the damage is the frames of the foreign vehicle it swallowed:

| | rows | polluted rows | frames of the wrong vehicle inside a row |
|---|---|---|---|
| `adopt()` as it was | 2489 | 90 | **1250** |
| with revocation | 2620 | 98 | **416** |

The honest reading is that the fix **truncates** pollution rather than preventing
it: the evidence only exists after the fact, so a row keeps the frames it
absorbed before the contradiction arrived. Damage per polluted row falls from
13.9 to 4.2 and rows rise, because a false merge coming apart is two rows where
there was one.

### On the benchmark, every split was drawn back onto the footage

53 revocations across the eight clips. Overlap at the moment of revocation was
**0.000 on 49 of them** — boxes that do not touch at all — and 0.148 to 0.287 on
the other four.

Sixteen were filmed against the source video they came from
(`scratch/final/revoke_film.py`, sheets `revocations.jpg` and
`revocations_indian.jpg`) and **all sixteen are two plainly different vehicles**:
a distant white car and a dark Chevy Colorado, a black pickup and a silver
sedan, a red SUV and a maroon pickup, and on the Indian footage a red MSRTC bus
and a yellow loading tempo, a black autorickshaw and a white Ertiga.
**Zero false splits.**

### Decision

**Adopted. `stitch_alias_revoke: true`.** Rows 380 to 417 on the eight clips, and
the rise is the result rather than the cost — each is a vehicle production did
not previously report at all. Plate reads 13 to 14; the read it loses is the
50x24px `AP4H4111` on `vdo.avi` this file already records as a false read.
False positives on `23sec.mp4` stay 0, raw detections and tracker ids are
identical clip for clip, and every worker ended `done`.

## `footprint_twin` verified again and ADOPTED -- 2026-09-03

`stitch_footprint` is now `true`. The mechanism, its thresholds and its
five-merge review are unchanged from "Concurrent fragmentation" above; this pass
re-ran it on the eight surviving clips and it makes **the same five merges,
byte-identical, with the same shared-frame counts, IoUs, size ratios, drifts and
appearance scores**, both alone and alongside `stitch_alias_revoke`:

    23sec           2 -> 19    2f  IoU 0.976/0.968   white Chevy pickup
    20260901_151758 3 -> 30    2f  IoU 0.956/0.952   parked autorickshaw
    20260901_152733 12 -> 20   7f  IoU 0.935/0.905   wooden-bodied truck
    20260901_152733 61 -> 71   2f  IoU 0.946/0.936   moving autorickshaw
    vdopov3      1119 -> 1220  5f  IoU 0.970/0.965   parked silver pickup

Alone it takes rows 380 to 375 and the residual-duplication bound
(`scratch/baseline2/dup_probe.py`) from 17 rows to 14. It introduces no merge
that the alias revocation's splits were not already reviewed against.

## Plate detector A/B/C/D on data/vehicle_plate_frames -- trained, and NOT adopted

Two candidates were fine-tuned from the current production weights,
`models/finetuned/plate_det_scenes.pt`, on the source-disjoint split
(`scripts/train_plate_frames.py`), kept separately, and neither is in
`config/settings.yaml`:

    models/finetuned/plate_det_frames.pt     labels exactly as supplied
    models/finetuned/plate_det_frames_c.pt   plus 163 ensemble-completed train boxes

On the held-out sources of the split — an Indian source and a European CCTV
source, neither seen in training:

| candidate | mAP50 | mAP50-95 | P | R |
|---|---|---|---|---|
| C `plate_det_frames` | 0.751 | 0.517 | 0.889 | 0.712 |
| D `plate_det_frames_c` | **0.763** | **0.534** | 0.862 | 0.696 |

**Read the real video, not the mAP.** The labels those numbers score against are
the same pseudo-labels the candidates trained on, and they are the ones measured
above to be missing ~14% of the visible plates. The eight-clip benchmark, only
`models.plate_detector` swapped and every downstream filter identical
(`scratch/final/bench_plate.py`):

| | rows | plate reads | reads on the Indian clips | **false reads on `23sec.mp4`** |
|---|---|---|---|---|
| **A `plate_det_scenes` (production)** | 380 | 13 | 12 | **0** |
| B `plate_detector` (pretrained) | 380 | 24 | 11 | **12** |
| C `plate_det_frames` | 380 | 18 | **14** | **3** |
| D `plate_det_frames_c` | 380 | 11 | 7 | **4** |

**Both candidates break the zero-false-positive property**, which is the one
constraint this work was given. On a US car park containing no Indian plate at
all, C invents `DL12CG4999`, `RJ83RY173` and `MN23C6711`, and D invents four
more. C also adds a false read on `vdopov3.avi`. C is the only candidate that
reads more genuine Indian plates — 14 against 12 — and it buys those two reads
with three fabricated registrations, which in an ANPR system is the more
expensive error by a wide margin.

D, the ensemble-completed variant, is worse on every real-video axis despite the
better mAP: the extra boxes taught it to fire more often, and most of where it
fires more is wrong.

**Decision: `models.plate_detector` stays `models/finetuned/plate_det_scenes.pt`.**
Both candidates are kept for the record. The blocker is named and it is the
data: a plate set whose labels miss ~14% of the visible plates cannot teach a
detector recall, because every missed plate is a negative. What is needed is
**hand-corrected plate boxes on the Indian sources** — completing the labels on
`Chandigarh Road video` alone would be 260 frames — not more pseudo-labelled
video.

## Vehicle classifier -- retrained twice more 2026-09-03, still not adopted

`scratch/final/cls_score.py`, all four candidates over the identical val set:

| model | accuracy | macro F1 | auto | bus | car | moto | **truck** |
|---|---|---|---|---|---|---|---|
| `vehicle_cls_best` (documented) | 0.718 | 0.621 | 0.829 | 0.733 | 0.630 | 0.914 | **0.000** |
| `vehicle_cls_aug` (documented) | **0.737** | **0.652** | **0.883** | **0.764** | **0.650** | 0.889 | **0.073** |
| `vehicle_cls_repro` (this pass) | 0.718 | 0.621 | 0.829 | 0.733 | 0.630 | **0.914** | **0.000** |
| `vehicle_cls_scale` (this pass) | 0.697 | 0.611 | 0.859 | 0.697 | 0.590 | 0.891 | 0.019 |

Two things came out of it.

**The training pipeline is reproducible.** `vehicle_cls_repro` re-ran
`scripts/train_vehicle_cls.py --epochs 30` with nothing changed and reproduces
`vehicle_cls_best` exactly — same accuracy, same macro F1, **same confusion
matrix cell for cell**. That was worth establishing before trusting any of the
numbers above it.

**The size shortcut is not the mechanism, and testing it said so.**
`vehicle_cls_scale` was trained specifically to destroy it — `--scale 0.9
--erasing 0.2 --degrees 8`, 40 epochs — because train carries a class-specific
image-size signature that val does not. It is **worse**: accuracy 71.8% to
69.7%, macro F1 0.621 to 0.611, car 0.630 to 0.590, truck still 1 of 100. So the
hypothesis is wrong and the class-semantics finding in the dataset audit stands
as the explanation: every candidate sends **75 to 81 of the 100 val trucks to
`bus`**, which is what happens when the model is trained on articulated lorries
and tested on pickups, fire engines and bin lorries.

**Decision: `models.vehicle_classes` stays `null`.** No candidate beats
`vehicle_cls_aug`, and `vehicle_cls_aug` still carries the real-video collapse
this file already records — enabled, it relabels `23sec.mp4`, a car park holding
nothing but cars, as truck 42 / car 32 / bus 2. Vehicle type continues to come
from the detector's COCO label. **This is a documented non-blocking limitation,
not an open question**: the fix is a `truck` folder that means one thing.

## Regression -- 230 of 231, re-run 2026-09-03 with the shipped configuration

    p1_verify              21/22   the documented environmental failure, below
    p1_verify_shutdown       5/5
    p1_verify_supervision  14/14
    p2_verify              34/34
    p3_verify              57/57
    p4a_verify             25/25
    p4b_verify             74/74

Run twice: once with the shipped `stitch_alias_revoke: true` and
`stitch_footprint: true`, and once with both `false`
(`scratch/final/driver5.py`, logs `scratch/final/regON_*.log` and `regOFF_*.log`).
**The two runs are identical, check for check.** The changes to `app/stitch.py`
and `app/worker.py` cost nothing.

`p1_verify`'s webcam check fails on "webcam produced sightings -- 0 sightings"
and it is environmental, not a code fault: the check needs something the COCO
model calls a car, motorcycle, bus or truck in front of the lens. The reasoning
is unchanged from 2026-09-02, where probing the camera directly for 12 seconds
at a confidence floor of 0.10 returned only `kite` and `person`.

**Four suites had to be repointed at footage that still exists**, because
`footage/session01` was reorganised under them: `p2_verify`, `p3_verify` and
`p4a_verify` now read `footage/clips/20 sec.mp4` and `p4b_verify` reads
`footage/session01/23sec.mp4`. Before that they failed with
`Could not open source 'footage/session01/20 sec.mp4'` and
`missing footage/session01/10 sec.mp4` — a footage problem wearing a regression
costume. Each repoint carries a comment saying so. No video file was moved or
deleted by this pass.

## Re-identification: one physical vehicle, no plate — added 2026-09-02

`app/stitch.py` gains a fourth mechanism, `reid_twin`, for the case the other
three cannot reach: **a vehicle leaves the frame and comes back carrying no
plate to tie the fragments together.** Nothing above it changed —
`stitch_max_gap_frames` is still 25 — because this is a separate gate on a
separate mechanism that only runs on finished tracks.

### A colour histogram cannot do this, and that is measured

`scratch/inf/reid_probe.py` scores every pair of emitted crops in the two worst
clips against duplicates read off the contact sheets by eye:

| descriptor | worst true duplicate | best distinct pair | separation |
|---|---|---|---|
| HS histogram (what `appearance()` uses) | 0.140 | 0.946 | **−0.805** |
| 3×3 blocked HS histogram | 0.103 | 0.923 | −0.820 |
| `yolo11n-cls` 256-d embedding | **0.939** | 0.982 | −0.043 |

The histogram is measuring colour, and colour is shared by vehicles that are
not the same and *not* shared by one vehicle seen from two sides — the white
Swift's own front and rear views score **0.140** while two different parked
motorcycles score **0.946**. The embedding closes almost all of that gap. It is
`models/yolo11n-cls.pt`, already on disk as the classifier's starting weights,
so no dependency was added and none could be.

### The clock does what no threshold can

Even the embedding does not separate cleanly on its own: in
`20260901_151758.mp4` a dark motorcycle and a white scooter — plainly different
vehicles — score **0.937**, *higher* than the white Swift's two views of itself
at **0.939**. No appearance threshold exists that takes one and refuses the
other.

So the first gate is not a threshold at all. **Two tracks whose lifetimes
overlap are two vehicles**, because one vehicle cannot be in two places at
once. The motorcycle and the scooter are both in frame at 09:00:25, and the
veto is free, exact, and unfoolable.

### The threshold was set on the hardest over-merge test available

`23sec.mp4` — a dashcam driving past a car park, 76 rows of similar vehicles,
no plates anywhere:

| `stitch_reid_similarity` | merges on `23sec.mp4` | verdict |
|---|---|---|
| 0.96 | 6 | includes visibly different cars |
| **0.97** | **2** | both the same white Ford pickup seen three times |
| 0.98 | 0 | nothing |

### Result, and every merge it made

Rows **148 → 143**, with reads, false positives, exact OCR and mean similarity
all unchanged. Every merge is printed by the worker so it can be audited
against the crops without re-running the footage:

    track  41 is track  2  (similarity 0.974,  2.2s apart)   23sec white pickup
    track 171 is track 17  (similarity 0.976, 10.2s apart)   23sec white pickup
    track  64 is track 30  (similarity 0.987,  6.7s apart)   151758 autorickshaw
    track  52 is track 15  (similarity 0.972,  8.0s apart)   151758 autorickshaw
    track 122 is track 46  (similarity 0.981, 18.8s apart)   151758 parked motorcycle

All five were checked against the evidence crops. **Zero false merges.** The
autorickshaw goes from 5 rows to 3, the parked motorcycle from 2 to 1, and the
white pickup in the control clip from 3 to 1.

## Concurrent fragmentation — `footprint_twin` built and measured 2026-09-03

> **Superseded on the decision only, 2026-09-03: `stitch_footprint` is now
> `true`.** Everything below — the mechanism, the thresholds, the sweeps, the
> five reviewed merges, the rejected variants — is unchanged and still the
> reference for how it works and why each number is what it is. Only the last
> line of "Decision" changed, and it changed because the eight-clip re-run
> reproduced the same five merges byte-identically. Read this section as
> written, then "`footprint_twin` verified again and ADOPTED".

The mechanism the section below names as the fix for open problem 1: two
tracker ids on **one** vehicle **at the same time**. `app/stitch.py` gains a
fifth mechanism, `footprint_twin`, gated on `stitch_footprint`, which was
**false** when this section was written — production was unchanged at that
point and every number in every section above this one still stands, verified
rather than assumed (see "Production is untouched" below).

Nothing above was loosened. `stitch_reid_similarity` is 0.97, `stitch_max_gap_frames`
is 25, `stitch_overlap` is 0.55, and `footprint_twin` reads none of them. It
runs **last**, only on pairs `plate_twin` and `reid_twin` have already declined.

### Why the two existing gates cannot reach this, at any threshold

`reid_twin` refuses a pair whose lifetimes overlap, and `adopt` refuses a
candidate the tracker is reporting in the same frame. Both rest on "one vehicle
cannot be in two places at once", which is true and is the wrong test here:
these tracks are not in two places, they are in **one place, twice**. So the
veto is not relaxed — it is replaced by its own contrapositive, which is a
stronger physical statement than any similarity threshold:

> for EVERY frame in which both tracks were reported, their boxes were the same
> box, at the same size, holding the same offset.

**One disagreeing frame is a veto, not a lowered average.** That is the whole
safety argument, and it was arrived at by measuring two weaker formulations to
destruction first:

- **Agreement averaged over the shared frames** fails outright. A stitched track
  is a concatenation of the raw ids `adopt` folded into it, so two ids that are
  one vehicle for twenty frames can be two vehicles for the twenty after.
  Averaged over their whole shared range the *known* duplicates score a mean
  IoU of **0.000 to 0.334** — below unrelated pairs. Measured over all 2265
  co-existing pairs in the nine clips (`scratch/footprint/sweep.py`).
- **Agreement over the best sustained contiguous window** fails because it reads
  only the window. `23sec.mp4`'s tracks 17 and 3 have one: **nine** processed
  frames at IoU 0.98, followed by **seventeen consecutive** frames at IoU 0.000
  with both tracks on screen in different places. Seventeen frames of proof that
  they are two vehicles, invisible to a rule that looks at the best nine. Under
  that formulation it merged them.

### The gates, and the plateau they sit on

    stitch_footprint             false      <- production
    stitch_footprint_min_frames  2          processed frames both were reported
    stitch_footprint_min_iou     0.85       EVERY shared frame must reach this
    stitch_footprint_iou         0.90       mean over the shared frames
    stitch_footprint_min_cover   0.80       shared frames / the shorter track's life
    stitch_footprint_size_ratio  0.80       two boxes on one object are one size
    stitch_footprint_max_drift   0.03       wobble of the centre-to-centre vector,
                                            in box widths -- a passing vehicle sweeps
    stitch_footprint_appearance  0.80       a veto, never the decision

Swept one at a time over the nine clips (`scratch/footprint/replay.py --sweep`),
**every geometry gate produces the identical merge set across a wide band**:

| gate | band giving the identical result |
|---|---|
| `min_iou` | 0.50 – 0.90 |
| `iou` (mean) | 0.80 – 0.90 |
| `size_ratio` | 0.60 – 0.90 |
| `max_drift` | 0.01 – 0.10 |
| `min_cover` | 0.00 – 1.00 |
| `appearance` | 0.00 – 0.80 |

Only two settings change anything: `min_frames` 2 → 3 loses two merges, and
`appearance` above 0.80 loses them one at a time. **This is a plateau, not a
knife edge**, which is the opposite of what `stitch_reid_similarity` sits on
(0.97 works, 0.96 over-merges).

`appearance` **never binds**: dropping it to 0.0 gives the same merges, because
the geometry has already refused everything else. It is kept as a veto against a
grossly different-looking pair, and it is honest to say it earns nothing here.

### The A/B — nine clips, the real pipeline, everything else identical

`scratch/footprint/bench_footprint.py`, which appends the overrides to
`config/settings.yaml` and restores it including on a crash, then runs
`scratch/bench_realvideo.py` unmodified over the same nine clips as `b2_base9`.
Runs `runs/bench/fp_prodA2.json` and `runs/bench/fp_onB2.json`; logs in
`scratch/footprint/prodA2.log` and `onB2.log`.

| clip | A rows | B rows | A stitches | B stitches | plate reads |
|---|---|---|---|---|---|
| 20 sec.mp4 | 10 | 10 | 5 | 5 | 2 → 2 |
| 10 sec.mp4 | 12 | 12 | 15 | 15 | 0 → 0 |
| 23sec.mp4 | 74 | **73** | 105 | **106** | 0 → 0 |
| 20260901_151758.mp4 | 18 | **17** | 23 | **24** | 3 → 3 |
| 20260901_152733.mp4 | 29 | **27** | 26 | **28** | 7 → 7 |
| vdo.avi | 66 | 66 | 60 | 60 | 1 → 1 |
| vdopov2.avi | 50 | 50 | 40 | 40 | 0 → 0 |
| vdopov3.avi | 94 | **93** | 97 | **98** | 0 → 0 |
| vdopov4.avi | 39 | 39 | 33 | 33 | 0 → 0 |
| **total** | **392** | **387** | **404** | **409** | **13 → 13** |

Nothing else moves. Processed frames, raw detections and tracker ids are
identical clip for clip — stitching never feeds back into the tracker. Plate
crops 13 → 13, reads 13 → 13, distinct plates 13 → 13, **false positives on the
plate-less `23sec.mp4` 0 → 0**, exact OCR 1/4, character accuracy 30/40, mean
similarity to the hand labels **0.750 → 0.750** with all four per-plate reads
byte-identical (`scratch/baseline2/score_run.py`). Every worker ended `done`.

### Every merge, reviewed against the source frames

Five merges, and all five were reviewed by drawing each track's whole life back
onto the footage it came from (`scratch/footprint/merge_film.py`,
`track_film.py`; sheets in `scratch/footprint/merges_live_film.jpg` and
`track_*.jpg`).

| clip | merge | shared | IoU mean/worst | size | drift | appearance | verdict |
|---|---|---|---|---|---|---|---|
| 23sec.mp4 | 2 → 19 | 2f | 0.976 / 0.968 | 0.99 | 0.004 | 0.920 | **correct** — white Chevy pickup, two ids |
| 20260901_151758.mp4 | 3 → 30 | 2f | 0.956 / 0.952 | 0.98 | 0.002 | 0.965 | **correct** — the parked autorickshaw |
| 20260901_152733.mp4 | 12 → 20 | 7f | 0.935 / 0.905 | 0.94 | 0.007 | 0.825 | **correct** — the wooden-bodied truck |
| 20260901_152733.mp4 | 61 → 71 | 2f | 0.946 / 0.936 | 0.96 | 0.004 | 0.954 | **correct** — the moving autorickshaw |
| vdopov3.avi | 1119 → 1220 | 5f | 0.970 / 0.965 | 0.98 | 0.005 | 0.890 | **correct** — the parked silver pickup |

**Zero false merges.** The one hand-labelled *distinct* pair in the whole
audit — `vdopov4.avi` tracks 8 and 20, a dark Subaru and a dark pickup that the
ImageNet embedding scores **0.968**, higher than three of the five merges above
— is refused **on geometry alone**: IoU 0.000 on all fifteen frames the two
share, drift 2.82. The gate that stops it is not a similarity threshold, which
is the entire point.

Two merges are worth reading closely because they look wrong and are not:

- `151758 3 → 30` compares track 3 against **track 64's** box history, because
  production's re-id had already folded 64 into 30 and the worker's
  `retired[id] = track` replaces the object under a surviving id. Track 64 is
  the same parked autorickshaw, filmed at `scratch/footprint/track_20260901_151758_64.jpg`,
  so the merge is right — but the pairing is decided by which fragment last
  landed on the id, which is worth knowing.
- `152733 12 → 20` has the lowest appearance of the five at 0.825 and is
  unambiguously one truck: both boxes sit on the same wooden-bodied lorry for
  all seven shared frames.

### The two named vehicles

**`20260901_151758` autorickshaw: 3 rows → 2. Improved, not closed.** The third
row is track 15, which is the same autorickshaw (frames 91–125) but is compared
against **track 52's** history, because production's re-id folded 52 into 15
before track 3 finished. Track 15 and track 30 never share a frame at all
(91–125 against 163–181), so no footprint rule can reach them; that pair is a
`reid_twin` case its clock veto refuses because the two rows' *lifetimes*
overlap through their other fragments.

**`vdopov3.avi` parked silver pickup: 4 rows → 3. Improved, not closed** — and
the audit in the section above undercounted it. The pickup is written as tracks
**1119, 1120, 1220 and 1554**, not three, and filming all four
(`scratch/footprint/vdopov3_pickup.jpg`) shows why the remaining two cannot be
merged: **track 1120 is the pickup at f388 and then a dark red sedan from f401
to f418, and track 1554 is the pickup at f608–622 and then a white pickup
driving through from f645 to f658.** Each of those ids genuinely covers two
vehicles — an `adopt` failure, not a footprint one — and merging them would fold
a red sedan and a moving white pickup into the parked pickup's row. The
every-frame veto refuses them, correctly.

### Residual duplication, and why its headline number must be read carefully

`scratch/baseline2/dup_probe.py`, unchanged, on both arms:

| | rows | pairs ≥ 0.97 | vetoed | windowed | collapse bound |
|---|---|---|---|---|---|
| A, production | 392 | 13 | 11 | 2 | 12 |
| B, candidate | 387 | 9 | 8 | 1 | 9 |

**Part of that fall is not a collapse.** The probe compares the *emitted rows'*
embeddings, and a merge replaces the surviving row's crop and embedding with the
incoming fragment's — so `23sec 17~19` and `vdopov3 1120~1220` drop out of the
probe because a row's descriptor changed, not because the duplicate went away.
The honest statement is the reviewed one: **5 rows removed, each confirmed
against the footage as a second row for a vehicle that already had one, and 0
distinct vehicles merged.**

Against the 11 confirmed duplicate rows the audit above records, three of the
five merges are duplicates that audit **never saw** — `23sec 2~19`,
`152733 12~20` and `152733 61~71` all score below the probe's 0.97 floor. So
the candidate both closes some of the known duplication and finds duplication
the embedding-based probe is blind to.

### Rejected: absorbing the footprint on every stitch

Built and measured (`scratch/footprint/onC.log`, tag `fp_onC`). Instead of
letting `retired[id] = track` replace the box history under a surviving id, the
history was unioned, so a row would be compared against everything the vehicle
behind it has done.

**It is strictly worse: 5 merges become 4, 387 rows become 388.** The union
brings with it a frame on which the two tracks disagree — track 30's own f181,
where it is not on the autorickshaw — and the every-frame veto fires, losing the
`151758 3 → 30` merge that review confirmed correct. It is also inconsistent:
after a merge every other field on that row (embedding, plate, timestamps)
describes the fragment that joined last, and unioning only the footprint would
leave one field describing more of the row than the rest of them do. Reverted;
the reasoning is kept as a comment in `app/stitch.py`.

### Production is untouched, verified rather than asserted

- `stitch_footprint` is `false` in `config/settings.yaml`; with it false,
  `_Track.footprint` is `None`, `update()` records nothing and `footprint_twin`
  returns on its first line.
- Run `fp_prodA2` — the nine clips through the changed code with the flag off —
  is **bit-for-bit `b2_base9`**: 392 rows, 404 stitches, 13 plate crops, 13
  reads, 13 distinct, 0 duplicates, and the same per-clip processed frames, raw
  detections and tracker ids.
- Regression **230 of 231** (`scratch/footprint/reg_*.log`): p1 21/22,
  p1_shutdown 5/5, p1_supervision 14/14, p2 34/34, p3 57/57, p4a 25/25, p4b
  74/74. The one failure is the documented environmental one — `p1_verify`'s
  webcam check needs something the COCO model calls a vehicle in front of the
  lens.
- The suites were run a second time with `stitch_footprint: true`
  (`scratch/footprint/regon_*.log`): p2 34/34, p3 57/57, p1_supervision 14/14,
  p1_shutdown 5/5. The candidate does not break the app when it is on.

### A benchmark-harness fault found on the way, worth recording

`scratch/bench_realvideo.py` runs all nine clips under one `source_id` of
`bench`, and evidence crops are named `<track_id>_<first_seen>.jpg`. Every clip
starts at 09:00:00, so **9 of the 364 crop filenames are written by more than
one clip and the later clip overwrites the earlier** — `crops/evidence/bench/000002_20260901T090000000Z.jpg`
is written by seven of the nine. Reviewing `23sec.mp4`'s track 2 from that file
shows a vehicle from `vdo.avi`, which is exactly how a correct merge first
looked like a false one here. Only ids finalised at t=0 are affected, real
sources are unaffected because each has its own `source_id`, and the fix is one
line in the benchmark — but **any crop-based review of the nine-clip run must
draw from the footage, not from `crops/evidence/bench/`**, which is what
`scratch/footprint/merge_film.py` does.

### Decision

**ADOPTED 2026-09-03 — `stitch_footprint` is now `true`.** It was left off when
this section was written, deliberately, so that turning it on would be a
decision rather than a side effect; that decision was taken in the following
pass after the eight-clip re-run reproduced the same five merges
byte-identically. The rest of this paragraph is the case as it stood then and
it is unchanged. The acceptance
criterion — reduce confirmed physical duplicates without introducing false
merges — is met on every measurement available: 392 rows → 387, five merges, all
five confirmed against the source frames, zero false merges, the one
hand-labelled distinct pair refused on geometry, OCR and plate metrics
byte-identical, the zero-false-positive property on `23sec.mp4` intact, and
every threshold on a plateau rather than an edge.

What the evidence does **not** contain is a false merge at any setting, and that
is a five-merge sample. `reid_twin` was adopted on a comparable sample and this
is the same class of change, so the case for turning it on is a good one; making
that call is a one-line edit to `config/settings.yaml` and it was left to be
taken deliberately rather than as a side effect of that pass. It was taken on
2026-09-03.

## Open: repeated physical vehicles, what is left and why

Updated 2026-09-03. Two of the three failures this section used to list are now
closed in production; the remaining one is the same one, and it is a dependency
problem rather than a threshold problem.

**1. Concurrent fragmentation of a parked vehicle -- CLOSED in production.**
`footprint_twin` is on. Five merges on the eight clips, all five confirmed
against the source frames, zero false merges, residual-duplication bound 17 rows
to 14. See "Concurrent fragmentation" and "`footprint_twin` verified again".

**2. A tracker id that genuinely covers two vehicles -- CLOSED for the case the
tracker itself contradicts.** `stitch_alias_revoke` is on. Measured at 3.6% of
all adoptions on 31575 frames of independent footage, and on the benchmark it
splits 53 merges, 16 of which were drawn back onto the footage and every one of
which is two plainly different vehicles. See "adopt(): identity pollution".

What is NOT closed is the part no evidence reaches: an adoption that is wrong
and that the tracker never contradicts, because it never reports the two raw ids
in one frame again. `vdopov3.avi`'s track 1120 — the parked silver pickup at
f388 and a dark red sedan from f401 — is that shape when the pickup is not
re-detected. Nothing at decision time separates it (measured: overlap, size
ratio and centre distance have the same distribution in the wrong adoptions as
in the right ones), and nothing after the fact contradicts it. It is bounded by
the 3.6% figure, not eliminated.

**3. Viewpoint change beyond an ImageNet embedding -- STILL OPEN and still not a
threshold.** Unchanged and re-stated because it is the one that needs a
dependency this environment does not have. The white Swift's front and rear
views score 0.939 while a dark motorcycle and a white scooter score 0.937; the
grey Kwid of `20 sec.mp4` scores **0.773** side-on against head-on while
unrelated pairs in that clip reach 0.916. `yolo11n-cls` gives ImageNet features,
not vehicle re-identification features, and that is the ceiling. **A real ReID
embedding is the fix and it is a dependency this environment does not have.**

The honest summary metric is now **412 rows across the eight clips**, against 380
before this pass — and the direction is deliberate. 5 of the change are
duplicates collapsed and 37 are false merges taken apart, so the number moved up
because it moved toward the truth, not away from it. It is still not a count of
vehicles.

