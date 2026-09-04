# P0 - Skeleton

P0 complete. Server stopped, port released.

Exit criteria — verified
check	result
python -m app.run starts	✅ from a fully empty state
creates the db	✅ data/anpr.db, journal_mode = wal
serves /api/health	✅ 200 {"status":"ok","db":true,"sources":0,"sightings":0}
schema matches frozen contract	✅ all 3 tables, column-for-column, in order (13 / 14 / 7)
restart is idempotent	✅ no duplicate seeds, no error
Files written
app/config.py · app/db.py · app/api.py · app/run.py · config/settings.yaml · config/sources.yaml · .gitignore

Renamed app/_init_.py → app/init.py (the single-underscore name was a latent import trap) and config/cameras.yaml → config/sources.yaml. The other ten app/*.py modules are untouched at 0 lines — they're P1–P5.

Two things worth flagging
A real bug the seed test caught. My first _seed_row validated required fields with not entry.get(k), which rejects uri: 0 — a webcam at index 0, the most common live source. Fixed in app/run.py:33-40: a field counts as present unless it's absent, null, or blank, never merely falsy. Same fix applied to frame_skip, where 0 is a legitimate "skip nothing". Verified: a seed with uri: 0 and frame_skip: 5 now loads, and a genuinely malformed entry is still rejected by source_id with a named reason.

A caveat on my own verification. An intermediate "final run" reported 200 while actually failing to bind — kill %1 reaped the bash job but left the Windows python process holding port 8000, so the 200 came from the stale server. I killed it via taskkill and re-ran from a deleted db; the table above reflects that clean run only. Worth remembering when P1 starts spawning workers: background processes here need taskkill, not kill.

Decisions locked in
Timestamps are ISO-8601 UTC text (2026-08-31T09:00:00.000Z) — round-trip and lexicographic sort both verified. This is the format P1's timestamp rule will produce.
plate_candidates / sighting_ids are json.dumps text.
No UNIQUE(source_id, track_id) — re-processing a file restarts track ids at 1, and the constraint would silently reject the entire second run. Noted in the schema comment.
Four indices added (plate_text, first_seen_ts, (source_id, first_seen_ts), alerts.created_ts) so P3/P4 queries need no migration. No contract fields added.
Seeding happens only when the sources table is empty, not INSERT OR IGNORE — otherwise a source deleted from the UI returns on every restart.
Deferred to P4a as scoped: the web/dist static mount and SPA fallback. When it lands, the catch-all must register after every /api route.
Ready for P1 (source-agnostic worker, detection, tracking, writer queue) when you are.

---

# P1 - Worker: detection and tracking

P1 complete. Server stopped, port released.

## Exit criteria - verified

> *a video file and a webcam both produce sightings through identical code,
> with timestamps correct for each*

| check | result |
|---|---|
| file produces sightings | yes, 15 from a 624-frame clip at frame_skip 3 |
| webcam produces sightings | yes, through the same `run_worker` |
| identical code path | `app.pipeline` spawns `app.worker.run_worker` for every source; the worker is never handed `kind` |
| recorded timestamp rule | shifting `start_time` by one day shifted every timestamp by **exactly** 86400s |
| live timestamp rule | webcam stamps landed inside the wall-clock run window and ignored the 2020 `start_time` set on the source to bait the wrong branch |
| one sighting per track | 35 rows, 35 distinct track ids, from 325 processed frames |
| crops saved | 35 evidence files, 35 unsorted, zero orphans, zero missing |
| supervision | an unopenable source and a `taskkill`ed worker both land in `error` with a reason |
| frame_skip | 1 / 3 / 10 all work, timestamps unmoved; skip=10 runs in 4.0s vs 10.5s |
| clean shutdown mid-run | 1.6s, partial sightings kept, source left `idle` not `running` |
| app end to end | `python -m app.run` from a deleted db: seeds, processes both clips, serves them |

42 checks across three scripts, all passing:

```
env\Scripts\python.exe scratch\p1_verify.py              23/23
env\Scripts\python.exe scratch\p1_verify_supervision.py  14/14
env\Scripts\python.exe scratch\p1_verify_shutdown.py      5/5
```

## Files written

`app/detect.py` · `app/worker.py` · `app/pipeline.py` (new) · `app/api.py` ·
`app/run.py` · `config/settings.yaml` · `config/sources.yaml`

`app/pipeline.py` is a module P0 did not stub. The parent half of the pipeline
- the single writer and the supervisor - had nowhere else to live: `worker.py`
is the child half and must stay free of database code.

## Two real bugs the tests caught

**Duplicate rows for one vehicle.** The first run wrote 16 sightings with only
15 distinct track ids. Cause: I evicted a track after 15 processed frames of
absence, but ByteTrack's own `track_buffer` is 30 - so it handed back an id for
a vehicle whose row was already written, and the second emission inserted
again. This is the "one sighting per track" trap arriving by a side door, and
it also poisoned the timestamp test: the two runs split the same track at
different points, producing a 3.099s delta where a full day was expected.

Fixed two ways, because either alone is fragile: `track_timeout_frames` is now
45, comfortably past the tracker's buffer, **and** the worker keeps written
tracks in a `retired` map so a re-activated id extends its existing row through
an UPDATE instead of inserting. Re-activation is now observable rather than
silent - the worker logs the update count, and it has been 0 since the timeout
change.

**Read timeouts that did nothing.** `_open_capture` set
`CAP_PROP_OPEN_TIMEOUT_MSEC` *after* constructing the capture, by which point
the open had already blocked for FFmpeg's 30s default. The timeouts now go in
as constructor params, which is the only form that applies to the open itself.
Directly relevant to the "network cameras stall" trap.

## Three things worth flagging

**Ultralytics pip-installed a package.** The first tracking call triggered
Ultralytics' internal AutoUpdate, which installed `lap` 0.5.13 - the linear
assignment solver ByteTrack needs. I did not run pip, but the venv changed and
the rule says never. Torch is untouched and still CUDA (`2.11.0+cu128`,
verified with a GPU tensor op). `app/detect.py` now sets
`YOLO_AUTOINSTALL=false` before importing Ultralytics, so a missing dependency
fails loudly instead of mutating the environment. Tracking cannot run without
`lap`; say the word if you want it removed anyway.

**OpenCV cannot open a multipart MJPEG stream in this build.** I built a
localhost MJPEG server to test the live path with real vehicles in frame.
OpenCV 5.0.0 would not open it - 30s timeout, every variant. HTTP itself is
fine: the same server handing out an mp4 opened in 0.1s. So the failure is
specifically the `mpjpeg` demuxer, and it puts the phone-camera flow
(`http://192.168.1.7:8080/video`) at risk for P4b. Worth testing against a real
phone early in that phase rather than discovering it there. RTSP is untested
and unaffected by this finding.

**The webcam leg tracked a person, not a vehicle.** A laptop webcam faces a
room, so with vehicle classes alone it produced zero tracks and the live
timestamp rule had nothing to check. The verification script temporarily widens
`vehicle_classes` to include COCO class 0 for that leg and restores
`settings.yaml` in a `finally`. The detector, tracker, clock and writer are the
same either way - what is proven is the live path, not that a car drove past
the laptop.

Also noted: the GPU here is an RTX 3050 6GB. The spec was corrected to match
on 2026-08-31; it previously said GTX 1650 4GB. Every constraint is still built
as written - nano weights, 640px, frame_skip on every source.

## Decisions locked in

- **Recorded vs live is decided by the capture, not by config.** A source
  reporting a frame count is recorded; -1 means live. Config cannot lie about
  it, and `kind` never reaches the worker - `WORKER_FIELDS` is
  `(source_id, uri, fps, frame_skip, start_time)`.
- A recorded source with no `start_time` falls back to file mtime, not `now`.
  Stamping old footage with the present would silently break every travel-time
  number.
- `frame_skip` means *process every Nth frame*; 0 and 1 both mean every frame.
- `frames_voted` is written as 0. No plate has been voted on yet; P2 sets the
  real count.
- One crop per track, the largest confident view, not the latest frame - it is
  what P2 reads a plate from. A copy goes to `crops/unsorted/` for the
  classifier dataset in TRAINING.md, switchable via `save_unsorted_crops`.
- Workers autostart for sources that are `idle` or `error`, never `done` -
  restarting a finished file would duplicate its sightings. Sources left
  `running` by a crash reset to `idle` at startup.
- New read-only routes: `GET /api/sources`, `GET /api/sightings?limit&source_id`.
  Creating and starting sources from the UI stays P4b.
- `config/sources.yaml` now seeds the two clips in `footage/`, with `lat`/`lon`
  deliberately unset - a guessed coordinate would draw a trajectory down a road
  that does not exist. Placement is P4b. Empty the list if you want a bare
  first run.
- Tuning observation, not a bug: the same clip yields 26 sightings at
  frame_skip 1, 15 at 3, 7 at 10. The default of 3 is what P2's accuracy
  numbers should be measured against.

## Current state

`data/anpr.db` holds a real run: cam01 and cam02, both `done`, 35 sightings
with crops, timestamps at 09:00 and 09:05 from their own `start_time` values.
Nothing in the database is test data.

Ready for P2 (plate detection, OCR across frames, character-level voting) when
you are.

---

# P2 - Plate reading

P2 complete. Server stopped, port released.

## Exit criteria - verified

> *an eval script scores predictions against a labelled test set and prints
> plate-level accuracy*

`scripts/eval_ocr.py` exists, is standalone, and prints the four-row table from
TRAINING.md. Its arithmetic is unit-tested and it produces real numbers from
real crops.

**It cannot report a meaningful accuracy figure, because there is no labelled
test set to report from.** Details in "What is missing" below. This is the one
part of P2 that is not closed, and it is a data problem, not a code problem.

| check | result |
|---|---|
| plate detection on tracked crops | yes, on the vehicle crop, never the frame |
| OCR across multiple frames of one track | yes, up to `plate_samples` reads per track |
| character-level majority vote, ties by confidence | yes, 25 unit tests |
| minimum plate width filter | yes, `min_plate_width: 48` |
| raw / confidence / candidates / frames voted / plate crop stored | yes, all five columns |
| a sighting with a null plate is still written | yes, 13 of 15 on the reference clip |
| eval script runs and prints plate-level accuracy | yes, on any labelled set it is pointed at |
| eval against `data/ocr/test/` | **no - the directory and its label file are empty** |

123 checks across six scripts, all passing:

```
env\Scripts\python.exe scratch\p2_verify.py               34/34
env\Scripts\python.exe scratch\p2_vote_test.py            25/25
env\Scripts\python.exe scratch\p2_eval_test.py            21/21
env\Scripts\python.exe scratch\p1_verify.py               24/24
env\Scripts\python.exe scratch\p1_verify_supervision.py   14/14
env\Scripts\python.exe scratch\p1_verify_shutdown.py       5/5
```

Four concurrent workers finish clean in 25.3s on the 6GB card, which is the
constraint that mattered most here: P2 doubled the per-worker GPU footprint by
adding a second YOLO model to every worker.

## Files written

`app/ocr.py` (new) - `app/worker.py` - `app/pipeline.py` - `app/config.py` -
`config/settings.yaml` - `scripts/eval_ocr.py` (new)

## The measured baseline

18 real crops of the one legible plate in `footage/`, hand-labelled from the
plate itself at 9x zoom (`MH15HY2237`, a Renault Kwid):

```
                                        plate    chars
1. baseline pretrained (cct-s-v2)        0.0%    60.6%
```

Per-frame the model never gets it exactly right. Voting over five reads lands
on `MH15HY227` - one character short of `MH15HY2237`. That gap is exactly what
P3's grammar correction and TRAINING.md's OCR fine-tune exist to close:
`MH15HY227` does not satisfy `[2 letters][1-2 digits][1-3 letters][4 digits]`,
and `MH15HY2237` does.

This is a one-vehicle set. It lives in `scratch/evalset/` and deliberately does
**not** go into `data/ocr/test/`, where a single-vehicle set would poison every
number reported afterwards.

## Four real bugs the tests caught

**The plate detector boxes whole vehicles.** Given a vehicle crop it regularly
returns one confident box around the entire crop - 335x215 inside a 336x252
vehicle, at 0.67 confidence. Picking the highest-confidence box first handed
the OCR model a picture of a car, which it answered anyway. `find()` now
filters on shape *before* confidence: a box covering more than
`max_plate_area_frac` of the vehicle, or that is not wider than it is tall, is
discarded. On one clip this removed 139 whole-vehicle boxes and cut empty OCR
reads from 179 to 67. Verified independently: the detector scores 78.8% recall
at IoU 0.5 against the labelled boxes already in `data/ocr/`, so the model is
fine and the selection was the bug.

**An empty read scored 1.00.** The OCR model emits ten fixed slots padded with
`_`, and padding slots are trivially confident, so averaging confidence across
all ten made a plate that read nothing score a perfect 1.00. Confidence is now
only ever averaged over the slots that survive into the final string, and a
result shorter than `min_plate_chars` is not a plate at all.

**Reads of different length voted against each other.** `MH15HY227` and
`MH15HY22` agree on slot 0 but disagree about what slot 5 means, and voting
across them silently deleted a character - the vote returned `MH15H227`, worse
than any single read that produced it. Voting now happens only among reads that
agree on how many characters they saw, chosen by count then by total
confidence. Reads whose length lost are not discarded: they come back as
candidates, which is where a dropped character belongs. On the reference plate
this moved the answer from `MH15H227` to `MH15HY227`.

**The read budget was spent before the vehicle arrived.** Sampling evenly
across a track spends most of the budget while the vehicle is far away and
unreadable, then stops exactly as it gets close - a plate legible in ten frames
was being voted from one. Reads are now only attempted on the closest views of
a vehicle, and the kept set keeps the widest reads, so a closer view later in
the track displaces an early distant one.

## Two things worth flagging

**OCR runs on the CPU, and onnxruntime will not say so.** Verified directly -
see "OCR device verification" below. `ocr_device` now defaults to `auto`, which
asks for CUDA, checks what the session actually got, and reports it in one
line. OCR runs once per track rather than per frame, so the cost is ~9ms per
plate and ~50ms per track.

**`frames_voted` counts the frames that voted, not the frames read.** On the
reference plate the worker took five reads and two of them agreed on length, so
the column says 2. Saying 5 would overstate the consensus behind the answer.

## Decisions locked in

- **Plate reading happens inside the frame loop, not at emission.** Holding
  five full vehicle crops for every live track is how this runs out of memory
  on a long stream. Only the small plate crop and the read survive a frame.
- OCR reads go through as one batch per frame across all tracks; plate
  detection cannot, because every crop is a different size.
- `plate_text` is set equal to `plate_raw`. No grammar correction exists yet,
  so the two columns being equal is exactly the truth about what has run. P3
  replaces `plate_text` with the output of `grammar.py`.
- A plate model that will not load does not cost the worker its vehicle
  sightings. It degrades to plates-off, and the reason reaches the UI through
  the source's `error` field rather than only stdout.
- Candidates list the voted string first, then strings the model actually
  produced, then single-character substitutions. An observed read is evidence;
  a substitution is a guess about how it went wrong.
- Plate crop padding was tested at 0/5/10/20% and rejected: zero exact matches
  at every level. The plate box is already slightly larger than the plate. The
  missing character is the model, not the crop.
- `scripts/eval_ocr.py` imports nothing from `app/`, per TRAINING.md. Rows 3
  and 4 of its table therefore say "not built yet" rather than being silently
  skipped - see the open question below.

## What is missing - please read

**There is no labelled OCR test set, so no accuracy number can be reported.**

`data/ocr/` currently holds a *plate detection* dataset, not an OCR one:

- `data/ocr/train/` - 1902 plate crops (`License (N).png`) and 181 full scenes
- `data/ocr/val/` - 1086 YOLO bounding-box files (`0 cx cy w h`), no plate strings
- `data/ocr/test/` - empty
- `train_labels.txt`, `val_labels.txt`, `test_labels.txt` - all three 0 bytes

Every `.txt` in there is a bounding box. Not one file contains a plate string,
so nothing in `data/ocr/` can score OCR. It is useful data - it is what I used
to confirm the plate detector is healthy - but it is the wrong kind for this.

**And the footage cannot supply one either.** I harvested every plate crop from
all four clips in `footage/` and read them by eye. Across 30 crops there is
exactly **one** legible plate, `MH15HY2237`, in `20 sec.mp4` (which appears
twice - `clips/` and `session01/` hold the same video). Everything the detector
found in the 1080p WhatsApp clip is a grille, a wheel arch, a window or a
bumper. The two 478x850 portrait clips have plates around 21px wide, which is
mush at any resolution.

To close the exit criterion I need, in order of value:

1. **Footage with readable plates.** Closer, slower, more front-on. As a rule
   of thumb the plate wants to be 100px wide or more in frame; the one that
   worked here was 72px and only just. This unblocks everything else.
2. **200-300 hand-labelled plate crops in `data/ocr/test/`** plus
   `test_labels.txt` as `filename<TAB>PLATESTRING`. The app already writes
   every plate crop it finds to `crops/plates/<source_id>/` - with better
   footage those are the images to label. `scripts/eval_ocr.py` prints these
   instructions when it finds nothing.
3. Optionally, real plate strings for the 1902 crops already in
   `data/ocr/train/` - they look like a public OCR dataset whose label file did
   not come along. If you know where they came from, the labels would give the
   fine-tune its 20% real data.

Everything in P2 is built and tested; it is the number that is blocked.

## One open question for P3

TRAINING.md forbids `scripts/` importing from `app/`, but row 3 of the eval
table is "+ grammar correction" and grammar will live in `app/grammar.py`.
Those two rules cannot both hold. The cheapest resolution is for `eval_ocr.py`
to write its raw predictions to a file and an app-side scorer to apply grammar
to them - files on disk, no shared code. Worth deciding before P3 rather than
during it.

Ready for P3 (grammar correction, fuzzy matching, trajectory) when you are -
though P3's own exit criterion, "eval prints accuracy before and after
correction", needs the same test set.

---

# P2 addendum - OCR device verification (2026-09-01)

Asked to verify that fast-plate-ocr creates its ONNX Runtime session with
`CUDAExecutionProvider` and uses the RTX 3050.

**It does not, and it does not fail when it cannot.** That is the finding.

```
fast-plate-ocr with device='cuda':
  providers requested: ['CUDAExecutionProvider']
  session providers:   ['CPUExecutionProvider']
```

`LicensePlateRecognizer(device="cuda")` returns a working recognizer with no
exception and no return value to check. onnxruntime logs the provider failure
and hands back a CPU session. Anything that trusts the `device` argument is
wrong about where it is running, which is why the device is now verified after
the session exists rather than assumed from what was requested.

## Why the CUDA provider will not load

`onnxruntime_providers_cuda.dll` names its own dependencies. Checking each one
against the loader:

| library | present | note |
|---|---|---|
| `cublas64_13.dll` | **no** | only `cublas64_12.dll` exists |
| `cublasLt64_13.dll` | **no** | only `cublasLt64_12.dll` exists |
| `cudart64_13.dll` | **no** | CUDA 12.8 toolkit provides the 12 series |
| `cudnn64_9.dll` | yes | shipped with torch, already on the path |

- onnxruntime-gpu is **1.29.0**, the **CUDA 13** build. For cuBLAS it names
  only the 13 series - there is no 12 fallback to bind to.
- The machine has **CUDA 12.8** (toolkit at `v12.8`, torch `2.11.0+cu128`).
- The **driver is fine**: NVIDIA-SMI 616.56, CUDA UMD 13.4. It is not the GPU,
  the driver, or cuDNN.

Adding `torch/lib` and the CUDA 12.8 `bin` to the DLL search path was tried and
changes nothing, correctly: cuBLAS 12 cannot satisfy a cuBLAS 13 import, and
renaming one to look like the other would be an ABI violation, not a fix.

**cuBLAS 13 is the single missing piece.** Nothing was installed, downgraded or
otherwise changed to establish this.

## What changed in the code

`ocr_device` accepts `auto` (new default), `cuda`, or `cpu`.

- **`auto`** - asks for CUDA, silently accepts CPU. onnxruntime's own provider
  errors are suppressed for the attempt, because on this machine that paragraph
  of red text is noise, not diagnosis. One line reports the truth.
- **`cuda`** - asks for CUDA and warns loudly if it does not get it, naming the
  exact libraries the loader could not find rather than onnxruntime's generic
  "install the dependencies".
- **`cpu`** - does not ask.
- Anything else raises with the setting name in the message.

Every path ends in a working recognizer. CPU is the fallback and always was;
what is new is that the app now knows which one it is on and says so at startup:

```
[ocr] pretrained fallback cct-s-v2-global-model on CPU
```

The day cuBLAS 13 is present, `auto` picks the GPU up with no code change.

## What CPU OCR actually costs

Measured on the cached `cct-s-v2-global-model` at its native 128x64 input:

| batch | per call | per plate |
|---|---|---|
| 1 | 8.6 ms | 8.6 ms |
| 2 | 14.0 ms | 7.0 ms |
| 5 | 48.0 ms | 9.6 ms |
| 8 | 84.2 ms | 10.5 ms |

A track is voted from at most `plate_samples` (5) reads, so OCR costs roughly
**50ms per vehicle**, not per frame. Four concurrent workers still finish the
footage in 25.3s. This is why CPU is a tolerable place to be while the CUDA 13
question is open - it is not why it should stay there.

## To put OCR on the GPU

Requires CUDA 13 cuBLAS. Both routes change the environment, so neither was
done:

1. Install the CUDA 13 runtime libraries alongside the existing 12.8 - the
   `nvidia-cublas-cu13` and `nvidia-cuda-runtime-cu13` wheels, or a CUDA 13
   toolkit install. Additive: torch keeps its own bundled CUDA 12.8 and is not
   affected. I have not verified those wheel names against PyPI, since checking
   properly means touching the network for a package I am not installing.
2. Swap onnxruntime-gpu for a CUDA 12 build. This is a **downgrade** and was
   ruled out.

Route 1 is the one to take if the GPU is wanted here. Say the word.

## Regression check

| script | result |
|---|---|
| `p2_verify.py` | 34/34 |
| `p2_vote_test.py` | 25/25 |
| `p2_eval_test.py` | 21/21 |
| `p1_verify_supervision.py` | 14/14 |
| `p1_verify_shutdown.py` | 5/5 |
| `p1_verify.py` | 21/22 - see below |

`p1_verify.py`'s webcam leg reported 0 sightings. Not a regression: the camera
opened, processed 319 frames, measured live fps at 46.6 and exited clean to
`idle` - the live code path ran end to end. It detected nothing because there
was nothing to detect. Frames off that camera have a **mean brightness of
4.6/255** and YOLO finds nothing across all 80 COCO classes, so the lens is
covered or the room is dark. The two assertions that need a real detection
could not run. Uncover the camera and re-run to get 24/24 back.

---

# P3 - Correction, matching, trajectory

P3 complete. Server started, health checked, stopped, port released. The
development database is byte-for-byte as it was found: 2 sources, 35 sightings.

## Exit criteria - verified

> *eval prints accuracy before and after correction*

`python -m app.eval_plates` prints four rows over one labelled crop folder.
Numbers below.

> *A vehicle seen at multiple sources returns a complete trajectory despite OCR
> variance*

Verified end to end against real footage, not a fixture. `footage/session01/20
sec.mp4` was registered as two placed sources 1.33 km apart with different
`frame_skip` (3 and 4) and start times two minutes apart, so the two workers
sample different frames and read the plate differently. One source read
`MH15HY227`, the other `MH15HY22`. Querying `MH15HY2237` - a string **no
sighting holds** - returned:

```
trajectory for MH15HY2237: 5 stop(s), 3 from this run
  1. Clip 20s       MH15HY227   score 0.900 via plate  2026-08-31T09:00:00.000Z
  2. Clip 20s       MH15HY22    score 0.800 via plate  2026-08-31T09:00:04.798Z  -1.1s, overlapping tracks, no speed
 *3. P3 west gate   MH15HY227   score 0.900 via plate  2026-09-01T12:00:00.000Z  +97194.9s
 *4. P3 west gate   MH15HY22    score 0.800 via plate  2026-09-01T12:00:04.798Z  -1.1s, 0.00 km, overlapping tracks, no speed
 *5. P3 east gate   MH15HY227   score 0.900 via plate  2026-09-01T12:02:00.000Z  +114.9s, 1.33 km, 42 km/h
```

Stops 1 and 2 are `cam01`, from a previous run days earlier. They were not
planted - the trajectory found them because they are the same vehicle, which is
the behaviour being tested. Both temporary sources were deleted afterwards.

| check | result |
|---|---|
| format validation, `[2L][1-2D][1-3L][4D]` and BH | yes, 8-11 chars, every layout fitted |
| state codes checked | yes, 37 current + 4 retired, `config/state_codes.yaml` |
| position-aware confusion fixes | yes, letter slot and digit slot decided separately |
| all pairs from the spec: O/D/Q, 8/B, 1/I/7, 2/Z, 6/G, M/H | yes, plus 0/O and 5/S |
| fuzzy retrieval then confusion-weighted edit distance | yes, two-stage |
| never exact string equality | yes - the end-to-end query matches nothing exactly |
| plate query to time-ordered sightings with coordinates | yes, with gap, distance, implied speed |
| eval prints accuracy before and after correction | yes |
| meaningful accuracy figure | **no - still no labelled test set. Same data gap as P2.** |

## Verification

```
env\Scripts\python.exe scratch\p3_verify.py                57/57
env\Scripts\python.exe scratch\p2_verify.py                34/34
env\Scripts\python.exe scratch\p2_vote_test.py             25/25
env\Scripts\python.exe scratch\p2_eval_test.py             21/21
env\Scripts\python.exe scratch\p1_verify_supervision.py    14/14
env\Scripts\python.exe scratch\p1_verify_shutdown.py         5/5
env\Scripts\python.exe scratch\p1_verify.py                21/22
```

`p3_verify.py` is four parts: grammar (23 checks), matching (9), trajectory leg
arithmetic on a throwaway fixture database (11), and the end-to-end run above
(14).

`p1_verify.py` is 21/22 for the same reason as last session and it is not a
regression: the webcam leg detects nothing because the camera is dark. Re-checked
this session at **7.3/255 mean brightness, nothing found across all 80 COCO
classes**. Uncover the lens and it returns to 22/22.

## The numbers

18 crops of the one legible plate in `footage/` (`MH15HY2237`), the same set P2
measured:

```
                                     plate    chars
1. raw OCR                            0.0%    60.6%   0/18
2. + grammar correction               0.0%    61.1%   0/18  +0.0%
3. + multi-frame voting               0.0%    60.0%   0/1   per vehicle
4. + fuzzy matching                     --       --   needs 2+ distinct plates
```

Row 4 prints `--` deliberately. Ranking against a one-plate vocabulary cannot be
wrong, and a row that cannot be wrong is not printed as though it could.

**Correction fixed nothing here, and that is the correct behaviour.** P2's note
predicted grammar would close the `MH15HY227` -> `MH15HY2237` gap because the
nine-character read fails the format. It fails it, but the fix is a *missing*
character, and this module does not invent characters - there is no evidence for
what the tenth one was. It refuses, marks the read invalid, and the sighting is
still written. What actually recovers the vehicle is `matching.py`: `MH15HY227`
scores 0.900 against `MH15HY2237` and the trajectory above is assembled entirely
from reads like it.

Correction earns its place on substitutions, which is what it can see evidence
for. Verified: `MHI5HY2237` -> `MH15HY2237`, `KA0SMN7788` -> `KA05MN7788`,
`0L1CAB1234` -> `DL1CAB1234`, `MH12AB1Z34` -> `MH12AB1234`. And, just as
importantly, `MH15HY2007` is left alone - a global 0-to-O replacement would
destroy it.

## Decisions and assumptions

**The eval lives in `app/`, not `scripts/`.** TRAINING.md forbids `scripts/`
importing from `app/`, and rows 2-4 measure app code. `scripts/eval_ocr.py`
keeps its independence and its rows 3 and 4 now name the command that produces
them. Both scorers read the same `data/ocr/test/` layout, so one labelled folder
still serves both tracks.

**Districts are numbered from 01.** Without that rule `KA0SMN7788` parses as
`KA-0-SMN-7788` at zero cost and beats the correct `KA-05-MN-7788`, which costs
one S-for-5. Found by the smoke test, not by reasoning.

**An ambiguous state code is not repaired.** If one substitution yields two
plausible codes the prefix stays as read and the plate is reported
unknown-state. Guessing between two states is a wrong answer wearing a
confident face; `matching.py` is where that uncertainty belongs.

**`plate_candidates` is stored uncorrected.** It records what the model saw.
Matching applies the grammar to the query side instead, so the evidence stays
evidence.

**A match through a candidate is penalised 0.03.** The voted answer is stronger
evidence than a third-choice alternative and two sightings that tie on distance
should not tie on rank. Small enough to break ties and nothing else.

**Negative gaps are left negative.** Two tracks of one vehicle at one camera
overlap in time, and the trajectory reports `-1.1s` rather than clamping to
zero. Clamping would hide a track split behind a plausible-looking journey. No
speed is computed from a non-positive gap.

**No new API routes.** `search` and `trajectory` are called by the verification
script directly. Exposing them is P4d, and P3's exit criteria do not need them.

**No new columns.** The `sightings` contract is untouched. `plate_text` now
holds the corrected string instead of a copy of `plate_raw`, which is what the
column was always specified to hold.

## Files written

`app/grammar.py` (new) - `app/matching.py` (new) - `app/trajectory.py` (new) -
`app/eval_plates.py` (new) - `config/state_codes.yaml` (new) - `app/worker.py`
(one line: `plate_text` now goes through the grammar) - `scripts/eval_ocr.py`
(rows 3 and 4 name the command that fills them)

## What is still missing

Unchanged from P2, and it is the same single item: **`data/ocr/test/` is empty.**
Every accuracy number above comes from 18 crops of one vehicle, which is a smoke
test. `app/eval_plates.py` warns when the vocabulary is under 20 plates and
refuses outright when there are no labels at all.

The app has written plate crops to `crops/plates/<source_id>/`. Labelling 200-300
of them turns every row in that table into a real measurement, and it is also
what TRAINING.md's OCR fine-tune needs. That fine-tune is where per-frame
accuracy actually moves; correction and matching are what make an imperfect read
still find the right vehicle, and both now work.

# P4a - UI shell and Live

## Exit criteria

| check | result |
|---|---|
| Vite/React/Tailwind scaffold | yes, written; **not installed or built - Node is absent** |
| design tokens | yes, `web/src/tokens.css`, palette derived from plate grounds |
| nav | yes, Live / Sources / Analyze / Trace / Insights / Alerts |
| map with source markers | yes, Leaflet direct, status colour, pulse on activity |
| websocket feed | yes, `/api/ws`, committed rows only |
| evidence crops | yes, `/crops` mounted, stored `crop_path` is the URL |
| FastAPI serves `web/dist` with SPA fallback | yes, catch-all registered last, refuses `/api/` |
| **browser shows sightings appearing live** | **blocked - see below** |

## The blocker

`npm install` and `npm run build` cannot run: **Node is not installed on this
machine.** Not on PATH, not in Program Files, no nvm, no bundled copy in VS
Code, nothing in the registry. CLAUDE.md states Node is installed; it is not.

Installing it is an environment change and the environment section says the
environment is already configured and not to modify it, so this stops here
rather than guessing.

Everything either side of the build is done. With Node present the remaining
work is two commands:

```
cd web
npm install
npm run build
```

Until then `/` and every deep link return 503 with a page naming those two
commands, which is the honest state: the API is up, the bundle is not.

## Verification

```
env\Scripts\python.exe scratch\p4a_verify.py         26/26
env\Scripts\python.exe scratch\p2_verify.py          34/34   (no regression)
env\Scripts\python.exe scratch\p1_verify_supervision.py  14/14
env\Scripts\python.exe scratch\p1_verify_shutdown.py       5/5
```

`p4a_verify.py` is three parts: routes (10 checks), the hub's thread-to-loop
hand-off in isolation (5), and end to end (11) - a real worker over
`footage/session01/10 sec.mp4` with a websocket opened *before* the source
exists, so nothing it emits can be missed by racing the connection.

The end-to-end leg produced 20 sightings over 20 distinct tracks, all 20 arrived
on the socket, timestamps were absolute and derived from `start_time`
(`2026-08-31T09:00:01.198Z` from a 09:00:00 start), and each crop was fetchable
at the path stored in its row. It writes to a throwaway database and deletes its
crops afterwards; the application database is untouched.

The frontend has no test. Without Node it cannot be compiled, let alone run. A
static check confirms all 20 source files parse as balanced and every import
resolves to a file that exists, which is a long way short of "it works".

## Decisions and assumptions

**Leaflet directly, no react-leaflet.** The stack names Leaflet. A binding layer
is a dependency the phase does not need, and Leaflet owning its DOM node while
React never touches it is fewer moving parts than reconciling two owners.

**A hand-written router, ~40 lines.** Six flat routes do not justify
react-router. `pushState` plus `popstate` is the entire mechanism, and the SPA
fallback is what makes the URLs survive a reload.

**Tailwind 3, not 4.** The scaffold from P0 has `tailwind.config.js`, which v4
does not use. Matching the layout that already exists beats a migration nobody
asked for.

**The live feed carries the stored row, read back after the commit.** Not the
worker's message. The two differ on a track re-activation, where columns are
kept by `COALESCE` - publishing the message would show the UI a plate the
database refused to overwrite.

**A full client queue drops its oldest event.** The writer thread is the only
thing that writes to SQLite. A browser tab that stopped reading must never be
able to stall it, and on a live feed the newest sighting is the one that
matters. On reconnect the client reloads from `/api/sightings` rather than
replaying: the socket is a notification, the database is the record.

**`pipeline.on_event` is an additive hook, not a refactor.** P1's behaviour with
it unset is byte-for-byte what it was. The writer is the only place that knows a
row landed, so it is the only place the live feed can honestly hang off.

**`GET /api/alerts` added.** Read-only, eight lines, over the `alerts` table
that is already in the frozen schema. The Live screen specifies an alert strip;
this lets that strip be real and empty rather than absent or invented. P5 is
still what writes rows.

**Screens that are not built say so.** Sources, Analyze, Trace, Insights and
Alerts each state what the screen will do and what can be done meanwhile. No
mock rows, no dummy chart, no placeholder numbers that could be mistaken for
readings.

**The vehicle badge infers commercial from `vehicle_type`.** Yellow means auto,
bus or truck, because commercial plates are yellow. This is inferred from the
class the detector gives us, not from an observed plate colour - there is no
such column and adding one to `sightings` needs asking first. The comment in
`VehicleBadge.jsx` says so, so nobody later reads the badge as a plate reading.

**No source is on the map yet.** `lat`/`lon` are null for both seeded sources,
so the Live map has no markers and says so, naming Sources as the place that
fixes it. Placement is P4b.

## Files written

`app/stream.py` (new) - `app/api.py` (websocket, `/api/alerts`, `/crops` mount,
SPA fallback) - `app/pipeline.py` (`on_event` hook, `_emit`, `_emit_sighting`) -
`web/package.json`, `vite.config.js`, `postcss.config.js`, `tailwind.config.js`,
`index.html` - `web/src/tokens.css`, `main.jsx`, `App.jsx` - `web/src/lib/`
(`router.jsx`, `api.js`, `socket.js`, `format.js`) - `web/src/components/`
(`PlateString`, `VehicleBadge`, `SightingCard`, `MapCanvas`, `CameraMarker`,
`EvidencePanel`, `Empty`) - `web/src/screens/` (`LiveScreen`, `Pending`,
`Sources`, `Analyze`, `Trace`, `Insights`, `Alerts`) -
`scratch/p4a_verify.py` (new)

The P0 stubs `LiveOpsScreen.jsx`, `AnalyticsScreen.jsx`,
`VehicleSearchScreen.jsx` and `CameraWallScreen.jsx` are still 0 bytes and were
left alone. The nav in CLAUDE.md uses different names, which the new screens
follow; the stubs are dead files to delete or repurpose, not something P4a
decided unilaterally.

## One thing found on the way

A `python -m app.run` from a previous session was still listening on port 8000,
serving pre-P4a code - which is why the first probe of `/api/health` came back
without the new fields. It was stopped before re-verifying. Worth knowing that a
stale instance shadows the port silently: the new process fails to bind, exits,
and the old one answers as though nothing happened.

---

# P4a — verification completed (2026-09-01)

Node is installed (v24.20.0, npm 11.19.0). `npm install` and `npm run build`
both run. The blocker recorded above is cleared and P4a is verified end to end.

## How it was run

The documented procedure, `env\Scripts\python.exe -m app.run`, against the real
application database. `/api/health` reports `ui_built: true`.

Workers could not be watched from that database, because both seeded sources are
`done` and a `done` file is deliberately not restarted -- doing so would
duplicate every sighting it produced. So the live half was run through
`scratch/p4a_live_run.py`, which changes exactly one thing: `paths.db` points at
a throwaway file. Same seed, same Pipeline, same create_app, same uvicorn. Crop
paths were left pointing at the real `crops/` directory on purpose, because
`/crops` is mounted from there and serving evidence is part of what was being
checked.

The application database was compared against a backup taken before any of this
started: same sources, same 35 sightings, same ids, every crop still on disk.

## What was verified

**Routes.** health, sources, sightings, alerts. An unknown `/api/` path returns
JSON 404 rather than being swallowed by the catch-all. Every nav route and a
deep link (`/trace/MH12AB1234`) return index.html. Raw un-normalised traversal
(`/crops/../../CLAUDE.md`) 404s, and the SPA fallback returns index.html rather
than any project file for paths outside `web/dist`.

**Evidence crops.** All 35 stored `crop_path` values fetched 200 as valid JPEGs
(complete SOI/EOI markers, 1.0-54.7 KB).

**Websocket.** Keepalive ping arrives at ~19.6s against the 20s interval. Two
clients are both served and both counted in `listeners`. A dead socket is reaped
on the next ping cycle rather than leaking. With the server killed the page
shows CONNECTING then RECONNECTING and keeps the sightings it already has; on
restart it returns to LIVE and reloads from `/api/sightings` rather than
replaying.

**Live, in a real browser.** Headless Chrome driven over CDP -- no browser
automation package is installed and none was added. React mounts, Leaflet
initialises, all six nav items render, and the five unbuilt screens each render
their own copy with no mock data.

The exit criterion was measured rather than asserted. A page was loaded while
the database held 0 sightings and then left alone -- no reload:

```
 elapsed | page header | feed crops | db  | worker state
    0.0s |           0 |          0 |   0 | 2 sources running
   18.1s |          13 |         17 |  15 | 2 sources running
   24.1s |          17 |         22 |  20 | 2 sources running
   30.2s |          30 |         37 |  35 | no source running
   96.4s |          35 |         37 |  35 | no source running
```

Nothing on that page asks the server for more after the first fetch, so every
one of those rows arrived over the websocket. 37 crops against 35 sightings is
35 vehicle crops plus the 2 plate crops. Both runs of the two seeded files
produced 35 sightings over 35 tracks, matching the original database exactly.

## Two defects found and fixed

**The basemap was watermarked.** `basemaps.cartocdn.com` still serves without a
key but now stamps every tile "API KEY REQUIRED", across the whole map. Switched
to Esri's dark canvas, which is keyless and unbranded. Note its `{z}/{y}/{x}`
order -- row before column, the reverse of the usual slippy URL -- and `maxZoom`
dropped to 16, which is as far as that layer serves.

**The header count never updated.** `getHealth()` ran once per route change, so
a page opened before the workers ran sat at "0 sightings" while the feed beside
it filled to 35. It now refreshes on a 5s timer. A timer rather than the socket:
the header is shown on every screen, including ones where nothing is listening
to the feed, and a second websocket for two numbers costs more than the poll.

## One thing found on the way

Both workers once stalled at fixed progress with the GPU pinned at 100%. Not the
app: an unrelated Unreal application (`MyProject2-Win64-Shipping`) was holding
~3 GB of the 6 GB card, and with two workers on top VRAM was oversubscribed and
WDDM was paging GPU memory to system RAM. It crawls rather than deadlocking,
which makes it look like a hang. `nvidia-smi` showed 5313 MiB in use with no
Python running at all. Once that application released the GPU (362 MiB, 0%) the
same two sources completed in ~78s. Worth knowing before diagnosing a stalled
worker as a bug.

## Verification

```
env\Scripts\python.exe scratch\p4a_verify.py              25/25
env\Scripts\python.exe scratch\p3_verify.py               57/57
env\Scripts\python.exe scratch\p2_verify.py               34/34
env\Scripts\python.exe scratch\p1_verify_supervision.py   14/14
env\Scripts\python.exe scratch\p1_verify_shutdown.py        5/5
```

`p4a_verify.py` is 25 now rather than the 26 recorded above. Nothing regressed:
one check only runs when `web/dist` is missing and asserts the unbuilt page
names the build commands. The frontend is built, so that branch is skipped.

## Files written

`web/src/components/MapCanvas.jsx` (tile provider, zoom control position,
maxZoom) - `web/src/App.jsx` (header counts refresh) - `web/dist/` (rebuilt) -
`scratch/p4a_live_run.py`, `scratch/p4a_ws_capture.py`, `scratch/p4a_cdp_live.py`,
`scratch/p4a_ws_status.py` (new, verification only)

P4b is not started.

---

# P4b - Sources

P4b complete. Server stopped, port released.

## Exit criteria - verified

> *a live camera and a recorded video are both added from the UI, with no file
> editing and no restart*

Both, by clicking, in a real browser, against one app process that was started
once and never restarted.

| check | result |
|---|---|
| live camera added from the UI | yes - webcam 0 detected, tested, placed, added, running |
| recorded video added from the UI | yes - file picked from disk, placed, added, progressing |
| no file editing | yes - `config/sources.yaml` untouched; the seed is still only a seed |
| no restart | yes - one uvicorn process throughout; workers start and stop under it |
| connection test with a preview frame before saving | yes - 640x480 frame shown in the dialog |
| map picker placement | yes - a click on the map became 19.940433, 78.881836 |
| upload a video | yes - 5.36 MB streamed to `footage/uploads/`, byte-exact |
| upload one or more stills | yes - two stills, one source each, a sighting from a single frame |
| visible progress bar | yes - that source's own bar moved 0 -> 20 -> 50% on a page never reloaded |
| start / stop from the UI | yes, including restarting a source that had finished |
| camera wall, MJPEG with boxes and plate reads drawn on | yes - see the screenshot note below |
| each tile shows fps and status | yes - "30.0 fps - 79%" and a Running pill |
| workers supervised, reason surfaced | yes - `error` and its text render on the source card |

## Verification

```
env\Scripts\python.exe scratch\p4b_verify.py         74/74   (real uvicorn, real sockets)
env\Scripts\python.exe scratch\p4b_cdp_sources.py    40/40   (real Chrome, by clicking)
env\Scripts\python.exe scratch\p4b_image_check.py    16/16   (the third add flow)
env\Scripts\python.exe scratch\p4b_smoke.py          33/33   (routes, no pipeline)
```

No regression:

```
env\Scripts\python.exe scratch\p4a_verify.py             25/25
env\Scripts\python.exe scratch\p3_verify.py              57/57
env\Scripts\python.exe scratch\p2_verify.py              34/34
env\Scripts\python.exe scratch\p1_verify_supervision.py  14/14
env\Scripts\python.exe scratch\p1_verify_shutdown.py       5/5
env\Scripts\python.exe scratch\p1_verify.py              21/22   see below
```

`p1_verify.py` fails one check: *webcam produced sightings*. It is not a
regression. That leg widens `vehicle_classes` to include people so a laptop
webcam pointed at a room has something to track, and right now the room is
empty: a direct probe of the same camera through the same detector returned
**0 tracked detections in all 22 processed frames**. The live path itself is
proven elsewhere in this phase - the webcam opens, runs, reports `running`,
measures its own frame rate, and streams to the wall. What is unproven today is
only that something walked past the laptop.

`p4b_verify.py` runs a real uvicorn server rather than fastapi's `TestClient`.
MJPEG is why: the endpoint is a long-lived streaming response that ends when the
client goes away, and closing a half-read stream inside `TestClient`'s
in-process portal deadlocks - the close waits for the generator, the generator
waits for the client. Over a socket, closing the socket *is* the disconnect,
which is both what a browser does and what the endpoint is written against.

## Three real defects found and fixed

**A finished source could not be restarted.** Pressing Start on a source the UI
had just shown as `done` returned 409 "already running". A worker reports its
terminal status and *then* exits, so the row says `done` while the process is
still winding down, and `start_source` refused on `is_alive()`. It now waits out
the exit it has already announced (`EXIT_GRACE_SEC`, 8s) before calling a source
running. Anyone watching a file finish and clicking Start landed exactly in that
window.

**A 15 fps camera was reported at 46 fps, and `frame_skip` did nothing to it.**
Both come from one wrong assumption: that a bare `cap.grab()` advances any
source. It advances a file. It does not advance a DirectShow camera - only a
retrieve consumes a camera frame, and a grab without one hands back the same
buffer immediately. So the worker's grab-rate measurement ran at `frame_skip x`
the camera's rate, and skipping *before* the retrieve skipped nothing at all.
Measured on this machine's webcam by `scratch\p4b_fps_probe.py`:

```
          skip   grabs/s retrieved/s distinct/s processed/s
  before     1     15.00       15.00      15.00       15.00
  before     3     44.47       14.96      14.96       14.96
  before     8    118.25       14.96      14.96       14.96
  after      1     14.96       14.96      14.96       14.96
  after      3     14.95       14.95      14.95        4.98
  after      8     14.95       14.95      14.95        1.99
```

Before, `processed/s` does not fall with the setting: every live camera ran the
detector on every frame regardless of its `frame_skip`, which on a 6 GB card
that has to hold three streams is three times the intended load. After, the
reported rate is the camera's own and the processed rate is that over
`frame_skip`, as configured.

This is P1 code, so, as the working rules ask - **what**: a live source now
skips *after* the retrieve and measures its rate from frames it actually took,
and all track bookkeeping (timeouts, plate spacing, retirement) counts processed
frames instead of grabs. **Why**: the wall displays fps and the hardware section
makes `frame_skip` the load control; neither worked for a live camera. For a
recorded source the change is arithmetically identical - processed frames were
already exactly `frame_skip` grabs apart, so `timeout_frames * frame_skip` grab
units and `timeout_frames` processed frames are the same number. P1's file legs,
P2 and P3 all still pass unchanged, and the same clip still yields 20 sightings
over 20 tracks.

**The MJPEG route would have stalled the event loop.** It is a coroutine, and it
read its source row through `write()`, which blocks the calling thread until the
single writer answers. On the event loop that stalls every other request,
including the websocket. It now takes its own read connection; only writes go to
the writer.

## Three bugs in the verification itself, worth recording

**Module-level setup in a spawn-based test.** The first `p4b_verify.py` did its
`mkdtemp` and settings redirect at module scope. Windows spawn re-imports
`__main__` in every child, so that ran once per worker and once per probe - five
processes, each with its own temp directory, fighting over one webcam. Exactly
the trap in CLAUDE.md, arriving through the test rather than the app. Everything
is inside `main()` now.

**A progress check that passed without the new source ever moving.** It matched
`/(\d+)% processed/` against the whole page, and the two seeded sources sit at a
static 100%. Now scoped to the new source's own card and required to move
through more than one value: `[0, 20, 50]`.

**A field set by position.** The live flow puts "Or a URL" above "Name", so the
first text input in the dialog is the url - and writing to the url is precisely
what clears the connection test, which then correctly disabled Add. The test now
targets inputs by their label.

## Files written

`app/sources.py` (new) - `app/probe.py` (new) - `app/api.py` (source CRUD,
uploads, file listing, device detection, connection test, MJPEG) -
`app/pipeline.py` (feeder/writer split, `submit`, preview broker, viewer gating)
- `app/worker.py` (live skip and rate, preview publishing, overlay drawing) -
`app/config.py` and `config/settings.yaml` (`paths.uploads`)

`web/src/lib/api.js` - `web/src/screens/SourcesScreen.jsx` -
`web/src/components/` `AddSource`, `SourceCard`, `FeedTile`, `MapPicker`,
`Modal`, `Field`, `StatusPill` - `web/src/screens/LiveScreen.jsx`
(`source_removed`) - `web/dist/` rebuilt

`scratch/p4b_verify.py`, `p4b_cdp_sources.py`, `p4b_image_check.py`,
`p4b_smoke.py`, `p4b_fps_probe.py` (new, verification only)

`FeedTile.jsx` was one of P0's 0-byte stubs and is now the wall tile. The other
stubs (`LiveOpsScreen`, `AnalyticsScreen`, `VehicleSearchScreen`,
`CameraWallScreen`, `AlertCard`, `EvidenceStrip`, `TimeScrubber`,
`TrajectoryPath`) are still empty and still untouched.

## Decisions locked in

**One writer really means one.** The UI now writes to `sources`, and a request
thread issuing its own INSERT would be the second writer this design exists to
avoid. The pipeline's drain loop is split: a *feeder* thread does the blocking
multiprocessing `get`, and the *writer* thread takes both worker messages and
API callables off one in-process inbox. `pipeline.submit(fn)` runs `fn(conn)`
there and hands back the result or the exception. Additive - what the writer
does with a worker message is unchanged.

**The wall never opens the source.** The worker publishes annotated JPEGs it has
already decoded; the endpoint only forwards them. Verified by identity rather
than by assertion: a frame served is byte-for-byte one the pipeline holds.
Re-opening would double the GPU cost and, for a webcam, simply fail - the worker
holds the device.

**Drawing is off unless somebody is watching.** Each worker gets an `mp.Event`
the parent sets on the first MJPEG viewer and clears on the last. A wall nobody
has opened costs nothing; the preview queue is 24 deep and drops rather than
blocking the worker.

**Connection tests and device detection run in their own process.** Opening a
capture is the one call here that can block forever, and an API thread stuck
inside `cv2.VideoCapture()` never comes back. Each is a spawned child with a
deadline the parent controls; a child that overruns is terminated and reported
as a timeout. Detection also skips any index a running worker already holds -
probing one would either report a working camera as absent or take the device
away mid-run.

**A `start_time` on a live source is dropped, not stored.** It can never be used
- live is stamped from the wall clock - and keeping it invites someone to trust
it later. A `datetime-local` value is read as local time and stored as UTC:
`2026-08-31T09:00` typed here became `2026-08-31T03:30:00.000Z`.

**Deleting a source with evidence is refused once.** The first DELETE returns
409 naming how many sightings would go with it; the UI asks, then repeats with
`delete_sightings=true`. Crops stay on disk - they are evidence, and the row
that points at them is what the user chose to remove.

**`kind` is still a label, not a branch.** It is inferred from the uri, stored,
and shown. Nothing downstream reads it: `WORKER_FIELDS` is unchanged and the
worker still cannot tell what sort of source it has.

**Uploads land in `footage/uploads/`,** configured as `paths.uploads`. An
uploaded clip is footage like any other and a source pointing at one is an
ordinary row. Streamed to disk in 1 MiB chunks - reading a 2 GB clip into memory
to write it back out is how one upload takes the server down.

## Two things worth flagging

**A 12 MP close-up of a car is not detected.** The `car-wbs-*` stills in
`data/ocr/test/` are 4000x3000 frames where one vehicle fills the picture; at
the 640 px detection input the detector finds nothing in them, tracked or
otherwise. That is a property of those images, not of the image flow - the
`video10_*` stills, which are ordinary road scenes, produce sightings from a
single frame, one of them reading `MH03DV2010` at 0.96. Worth knowing before
`data/ocr/test/` is used as a detection set rather than an OCR one.

**I deleted orphaned test crops.** After the regression run, `crops/evidence`,
`crops/plates` and `crops/unsorted` held directories and files for source ids
that no longer exist (`p1test_*`, `p2test_*`, `p3tmp_*`, and this phase's own).
I removed the ones with no matching row: 14 directories and 159 unsorted crops.
They were repeat runs over the same two clips in `footage/`, so their content
duplicates the 35 crops that remain, and any of it comes back by re-running -
but it was my call and not one that was asked for. `cam01` and `cam02` are
untouched: 35 sightings, 35 crops, 35 unsorted.

## Current state

`data/anpr.db` is exactly as P4a left it - cam01 and cam02, both `done`, 35
sightings, every crop on disk. Nothing this phase created survives in it.

Ready for P4c (Analyze: image and video upload, annotated results, scrubbable
result timeline, JSON/CSV export) when you are.

---

# Model fine-tuning — OCR and plate detector (2026-09-01)

Run between P4b and P4c, on request: use the new plate JPG dataset and its CSV
as OCR ground truth, split it properly, fine-tune, and fine-tune the detectors
only where real bounding boxes exist.

## What the data turned out to be

`data/ocr/` held two different datasets interleaved in the wrong folders: 1690
plate crops plus `plate_labels.csv` sitting in `test/`, and a detection dataset
whose images were in `train/` while its YOLO labels were in `val/`. Neither was
usable as it stood. They were separated, nothing deleted:

    data/raw/plates/            1690 crops + plate_labels.csv  (the OCR ground truth)
    data/plate_det/images/      1086 images with boxes
    data/plate_det/labels/      1086 YOLO label files
    data/plate_det/unlabelled/   997 images with no label

## CSV to JPG mapping — verified, not assumed

`scripts/prep_ocr_dataset.py --verify-only` reproduces this.

- 1695 CSV rows, 1690 images. 5 rows name a file that is not on disk; every one
  of those filenames is over 170 characters, so they were almost certainly lost
  to a path length limit when the set was cut.
- 0 duplicate rows, 0 images without a row, 0 unreadable images.
- **Every crop's pixel dimensions equal its CSV box dimensions exactly**, all
  1690 of them. That is the check that makes the rest of the CSV trustworthy:
  the boxes describe these files and not some other copy of the dataset.
- 2 labels are not plates — `Devanagri` and `blur`, honest annotations of an
  unreadable image. Dropped.
- 1688 kept. 91.0% satisfy `app/grammar.py`'s format; the other 9% are mostly
  genuine older-format plates (`AR048395`, `HP302165`) and are kept, because
  OCR should read what is on the plate.
- Crop widths run 34–2702 px, median 123. 27 are under the app's
  `min_plate_width` of 48 and would never be OCR'd in production. Kept, since
  both models are scored on the same crops.

## The split, and the leakage it avoids

1688 crops carry only **979 distinct plate strings**. 654 are frames sampled
from eleven videos, and one vehicle appears **41 times**. Splitting per image
would put frame 12 of a car in train and frame 13 in test.

So the split is over plate strings, stratified by the three source collections:

    train  1182 crops   617 plates    OLX 421  google 303  video 458
    val     253 crops   180 plates    OLX  90  google  65  video  98
    test    253 crops   182 plates    OLX  90  google  65  video  98

    plate strings appearing in more than one split: 0

Seed 1337, recorded in `runs/ocr_split.json`, originals traceable through
`data/ocr/manifest.csv`.

**This test set is not "your own footage".** TRAINING.md asks for hand-labelled
crops from this deployment; there are none. It is a held-out slice of a public
collection, and it measures generalisation to unseen plates in that
distribution, not to the camera on the street.

## OCR fine-tune

`scripts/train_ocr.py`. PyTorch, not fast-plate-ocr's own trainer: that trainer
is Keras and its export path is ONNX, and neither `keras` nor `onnx` is
installed. CLAUDE.md forbids pip, so the fine-tune is a `.pt` and `app/ocr.py`
grew a `TorchRecognizer` that wears `LicensePlateRecognizer`'s interface. Voting,
candidates, confidence and both eval scripts are unchanged and cannot tell which
backend answered.

- 3.4M parameter CNN, then a width-wise BiGRU, then one classifier per slot.
- Same contract as the pretrained model: uint8 RGB 64x128 in, per-slot
  probabilities over `0123456789A-Z_` out. **Eleven slots rather than ten**,
  because `[2 letters][2 digits][3 letters][4 digits]` is a shape
  `app/grammar.py` accepts and ten slots can never produce.
- 40000 synthetic crops from `scripts/gen_synthetic_plates.py` plus the 1182
  real ones repeated 8x. Strings come from the real grammar and the real state
  codes; every plate in val or test is refused by the generator.
- 40 epochs, 34s each, best at 35, selected on **real** validation accuracy.

One performance note worth keeping: permuting NHWC to NCHW leaves a tensor whose
suggested layout is channels_last, and cuDNN has no fast NHWC kernel for these
shapes on the 3050 — the same stem takes 22ms fed NCHW and 307ms fed the
permuted view. One `.contiguous()` took the epoch from 327s to 34s.

### Pretrained vs fine-tuned, on `data/ocr/test`

| | plate (exact) | character |
|---|---|---|
| pretrained `cct-s-v2-global-model` | 36.8% | 74.2% |
| fine-tuned `ocr_best.pt` | **62.5%** | **88.5%** |

Val agrees: 34.4% to 66.4% exact, 81.0% to 91.4% character.

Through the rest of the pipeline (`python -m app.eval_plates`):

| row | pretrained | fine-tuned |
|---|---|---|
| raw OCR | 36.8% | 62.5% |
| + grammar correction | 36.4% | 62.1% |
| + multi-frame voting | 33.5% | 64.3% |
| + fuzzy matching, top-1 | 86.2% | **92.1%** |

The pretrained model's dominant failure is truncation — `AP39E1493` read as
`AP39E149`, one character short, over and over. The fine-tune does not do that;
crop-edge jitter in training is what teaches it not to.

**Grammar correction now costs 0.4%,** one plate in 253, where it gained with
the pretrained model. The fine-tune already emits well-formed plates, so
correction has nothing left to fix and occasionally forces a valid older-format
read into the modern shape. Left alone: one plate is inside the noise, and
correction still earns its place against garbage reads in the field.

## Plate detector — trained, measured, and the first attempt rejected

`scripts/train_plate_det.py`. The 1086 annotated images are two different
things:

    scenes   181 images   median box 17.5% of the frame    1% over half the frame
    crops    905 images   median box 86.3% of the frame   99% over half the frame

The 905 are annotations *of a crop*, not of a scene. Trained on all 1086, the
fine-tune scored **0.994 mAP50 against the stock model's 0.976** — and got worse
at the job. On 180 vehicle crops from `23sec.mp4`:

| | box found | median box area | passes the app's shape filter |
|---|---|---|---|
| pretrained | 51/180 | **2.0%** | 9 |
| fine-tuned on all 1086 | 26/180 | **72.4%** | 3 |

It learned that a plate fills the frame — precisely the failure
`app/ocr.py`'s `max_plate_area_frac` was written to defend against. Plate yield
over all of `footage/` fell from 30 to 25 of 280 sightings.

Retrained on the 181 scene images only, it wins everything
(`scratch/p5_det_decide.py`, every clip in `footage/`):

| detector | sightings | with a plate | yield |
|---|---|---|---|
| pretrained | 280 | 30 | 10.7% |
| fine-tuned, all 1086 | 280 | 25 | 8.9% |
| **fine-tuned, 181 scenes** | 280 | **43** | **15.4%** |

Better on every clip individually, better on held-out mAP (0.984 vs 0.955), and
better on an independent 310-crop hit rate (27 vs 13). Adopted as
`models/finetuned/plate_det_scenes.pt`. `plate_det_best.pt` — the rejected
all-1086 run — is kept for the record and wired to nothing.

## Vehicle detector — not fine-tuned, deliberately

There are no vehicle bounding boxes in this project. `data/vehicle_cls/` is 4594
folder-labelled crops for a *classifier*, and the OCR CSV's boxes are plate
boxes whose source images are not on disk. Fabricating vehicle labels from
either was the one thing explicitly ruled out, and TRAINING.md's own reason
stands anyway: single-class fine-tuning of the COCO detector trades cars for
autos. `models.vehicle_classes` is still null.

## Regression — 232 of 233

    p1_verify              24/24
    p1_verify_shutdown       5/5
    p1_verify_supervision  14/14
    p2_verify              33/34   see below
    p3_verify              57/57
    p4a_verify             25/25
    p4b_verify             74/74

The one failure is `the read resembles the ground truth MH15HY2237`, and it is
caused by the OCR fine-tune, not the detector — the 2x2 in
`scratch/p5_p2check.py` isolates it:

| | pretrained OCR | fine-tuned OCR |
|---|---|---|
| pretrained detector | PASS `MH15HY22` | FAIL `KN11BY227` |
| fine-tuned detector | PASS `MH15HY22` | FAIL `KH15BY2231` |

`p2_verify` inspects only the single most confident sighting. On this clip that
is a **one-frame** read scoring 0.629. The four-frame vote of the same vehicle,
on another track, reads `MH15HY2277` — one character from truth, grammar-valid,
scoring 0.578 (`scratch/p5_20sec_rows.py`). The check prefers the flimsier read
because raw `plate_conf` from one frame is not comparable to `plate_conf` from a
four-frame vote. Note also that the passing pretrained read, `MH15HY22`, is
itself two characters short — it satisfies the check by matching the first six.
The confidence comparison is worth fixing, but it is P2/P3 design, it does not
block P4c, and the check was not adjusted to make it pass.

## Real video, shipped configuration

`scratch/p5_realvideo.py`, every clip in `footage/`, nothing stubbed:

    clip                        status  sight  plated  well-formed  crops
    20 sec.mp4                    done     15       3            1     15
    10 sec.mp4                    done     20       0            0     20
    23sec.mp4                     done    170      20           12    170
    20260901_151758.mp4           done     27       7            4     27
    20260901_152733.mp4           done     48      13            9     48

280 sightings, 43 with a plate (15.4%), 26 well formed, 280 evidence crops on
disk, 0 workers ending in anything but `done`. The app also boots and serves:
`/api/health` ok, `/trace` returns 200 through the SPA fallback.

Well-formedness is not accuracy. Four of the five clips have no ground truth, so
what is reported is the share of reads that fit the Indian plate format — a read
that fits may still be the wrong vehicle, but a read that does not is certainly
wrong.

## Files written

`scripts/prep_ocr_dataset.py`, `scripts/gen_synthetic_plates.py`,
`scripts/train_ocr.py`, `scripts/train_plate_det.py` (new).
`scripts/eval_ocr.py` and `app/ocr.py` extended to load a `.pt` fine-tune.
`config/settings.yaml`: two model lines and the comment above them.
`TRAINING.md`: one row in the script table.
`scratch/p5_det_decide.py`, `p5_p2check.py`, `p5_realvideo.py`,
`p5_20sec_rows.py` (new).

`data/ocr/synth/` is 40000 files and 206 MB. It is reproducible from
`gen_synthetic_plates.py` with seed 1337 and is a candidate for deletion if the
repo needs the space.

**Stale note above.** The "12 MP close-up of a car is not detected" flag from
P4b refers to `data/ocr/test/`, which is now the OCR test split. Those
`car-wbs-*` stills live in `data/raw/plates/` and the observation still holds
for them.

## Current state

`data/anpr.db` is as P4b left it: cam01, cam02 and car_86, all `done`, 39
sightings. Every source and sighting created by this session's runs was deleted,
and the 820 crops orphaned by them were removed from `crops/evidence` and
`crops/plates`, leaving the 37 and 2 that belong to real rows. `crops/unsorted`
is untouched — that directory is a deliberate harvest for the classifier, not
evidence for a row.

Ready for P4c.

---

# P4c - Analyze

P4c complete. Server stopped, port released.

## Exit criteria - verified

> *dropping an unseen image returns annotated detections with plate reads*

`scratch/p4c_verify.py`, **75 checks passed, 0 failed, 0 skipped, 23s**
(`scratch/p4c/p4c_verify.log`). It starts the app once with **no pipeline and a
database holding no sources** — which is the "works with zero cameras
configured" criterion itself rather than a simulation of it, because if any part
of Analyze needed a worker, a writer or a source the script could not start at
all — and then does everything a browser does, over HTTP.

The image the exit criterion names is frame 120 of `footage/clips/20 sec.mp4`,
cut to a png by the script and uploaded through `/api/uploads` exactly as a drop
on the screen would. What came back:

| check | result |
|---|---|
| detections on an uploaded still | 2 boxes, both in range as fractions of the frame |
| each box carries a type and a confidence | yes |
| one vehicle per detection, never one per frame | 2 vehicles, 2 boxes |
| evidence crop per vehicle, and it serves | yes, both |
| **a plate was read** | **`MH05BY2207` at 0.62** |
| the read carries its confidence | yes |
| raw read kept beside the corrected one | yes |
| checked against the plate grammar | valid, state Maharashtra |
| plate crop saved, and it serves | yes |
| candidates stored for fuzzy matching | 1 |
| a vehicle with no plate is still reported | 1 of the 2 |

`MH05BY2207` is a **one-frame read** and is not offered as an accuracy figure.
The hand label for that vehicle is `MH15HY2237`, and the multi-frame vote over
the same clip reads `MH15HY2277` — see the video section below, which is the
number comparable to anything else in this repo. A single still has one view and
cannot vote, and the screen says so by showing `voted over 1 frame`.

### The control, because a read is only worth what its false-positive rate is

A still from `footage/session01/23sec.mp4` — the plate-less US car park this
repo uses as its control — was analysed the same way: **12 vehicles detected, 0
plates read.** The zero-false-positive property the pipeline has on that clip
survives into Analyze, which it must, because Analyze is running the same
filters.

## What was built

| file | what |
|---|---|
| `app/analyze.py` | new. The analysis child, the job runner, and the CSV export |
| `app/api.py` | 6 analyze routes and the `/media/analyze` static mount |
| `app/stitch.py` | `TrackStitcher.from_config()` — extracted, see below |
| `app/worker.py` | uses it; 40 lines of thresholds removed, nothing else |
| `app/config.py` | `analyze_dir()`, created by `ensure_dirs()` |
| `config/settings.yaml` | `paths.analyze`, `analyze_max_frames`, `analyze_frame_width` |
| `web/src/screens/AnalyzeScreen.jsx` | was an 11-line placeholder; now the screen |
| `web/src/components/AnalyzeViewer.jsx` | new. Frame, overlaid boxes, scrubber |
| `web/src/lib/api.js` | the analyze calls |
| `web/vite.config.js` | `/media` added to the dev proxy |

## Analyze is the pipeline, and that is measured rather than asserted

The module claims to be "the same pipeline pointed at a file" rather than a
second implementation of it. `scratch/p4c/parity.py` is what that claim is worth:
one clip through `scratch/bench_realvideo.py`, which runs a real worker and
writes real sightings, against the same clip through an analysis child, which
writes none.

`footage/clips/20 sec.mp4`, production configuration, every field compared:

| | pipeline | analyze |
|---|---|---|
| processed frames | 208 | 208 |
| raw detections | 207 | 207 |
| tracker ids | 16 | 16 |
| stitches applied | 5 | 5 |
| rows / vehicles | 11 | 11 |
| with a plate crop | 2 | 2 |
| with a plate read | 2 | 2 |
| plates | `MH15HY2277` 0.6019 voted 3, `PB61V6819` 0.3145 voted 1 | identical |
| types | motorcycle 4, truck 4, car 2, bus 1 | identical |

**Identical on every count, both plate strings, both confidences to four
decimals, both vote sizes, and the type distribution.** That is the whole
argument for building the screen this way rather than as a lighter separate path.

Cross-checked once more on `footage/session01/vdo.avi`, against `b2_base9`:
processed frames 667 and raw detections 3195 and tracker ids 127 are **the same
as the documented baseline**, which they must be, because stitching never feeds
back into the tracker. Stitches 60 → 63, rows 66 → 74 and the one plate read
gone are the documented effect of `stitch_alias_revoke` and `stitch_footprint`
being on now and off then — and the read it loses is the 50×24px `AP4H4111` that
CLAUDE.md already records as a false read.

## The refactor, announced and then proved neutral

`run_worker` built its `TrackStitcher` from twenty `config.default(...)` calls
inline. Analyze needs the same stitcher, and a second copy of twenty thresholds
drifts — a drifted copy would mean the same clip analysed and ingested reporting
different vehicle counts, which is precisely the comparison above. So the block
moved to `app/stitch.py:from_config()` and both callers read it.

`scratch/p4c/stitcher_parity.py` reconstructs the literal argument list
`run_worker` passed before the change and compares the resulting object against
`from_config()`'s: **all 17 settings identical, same values and same types.**
That is a stronger statement than "the suites still pass", and both were checked.

## Decisions, and what each one refuses

**An analysis is never a sighting, and never becomes one.** A sighting is a
vehicle a placed camera saw at an absolute time; an analysis is what the models
say about a file. The file has no location and no start time, so the results
carry an **offset into the media in seconds** and no absolute timestamp at all.
Writing them into `sightings` would put rows with no source and no real clock
into every trajectory and travel-time figure in the app. Verified two ways: no
row was written during the whole verification run (0 before, 0 after), and
structurally `app/analyze.py` contains no SQL and never imports `app.db`.

The consequence, stated so it is not discovered later: **analysing is not a way
to import.** A clip analysed and the same clip ingested produce the same
vehicles, and only the source's are in the database.

**One spawned child per job, one job at a time.** Same pattern as `probe.py` and
for the same reason: the analysis loads three models and holds a capture, and
neither belongs in the API process. Serialised because the card is 6 GB and has
to hold three streams — a fourth model set loaded because two people pressed
Analyze spends that budget twice. A queued job says where it is in the queue.
Measured cost of the per-job model load: **an image analysis is 3.0–3.3s end to
end**, and a 20.8s clip is 7.4s, so the child is not reloading anything per
frame.

**The boxes are drawn in the DOM, not burnt into the jpeg.** The child writes
plain downscaled frames and box coordinates as fractions of the frame. A burnt-in
box is fixed at whatever size the frame was written at and cannot be clicked,
and this screen's promise is that every detection opens. It also keeps OpenCV's
Hershey font out of a screen that sets its type properly.

**Progress is polled, not pushed.** The websocket carries committed sightings and
an analysis produces none; putting job progress on it would send one person's
upload progress to every Live screen in the building.

**Jobs are in-memory and their directories are swept at startup.** A job is a
question somebody asked about a file, not a record of anything observed, and it
has no place in the frozen schema. A restart therefore orphans every directory,
so `AnalysisJobs.start()` sweeps them — verified directly: two planted stale
directories, `[analyze] swept 2 job directories left by a previous run`, empty
after.

## The scrubbable timeline, and the one bound on it

Every processed frame gets its own image, its own offset and its own boxes, so
the scrubber shows what the pipeline actually saw rather than an interpolation
of it. The plate drawn on a box is `live_read` — the best single read *by that
frame*, never the final vote, because the vote runs when the track ends and
showing it early would claim the pipeline knew something it did not yet know.

That is bounded, because the pictures are not free. Past
`analyze_max_frames: 600` the annotated frames are strided uniformly and the
result reports the stride; **the detections are never strided, only the pictures
of them**, and the screen says "timeline shows every Nth processed frame" when
it is doing that. Exercised on `vdo.avi`, the only clip long enough to trigger it:

    667 processed frames -> stride 2 -> 334 timeline frames
    spanning 0.0 -> 199.8s of a 200.1s clip
    frames 334 files 31.7 MB   crops 74 files 0.8 MB   total 32.7 MB

## Regression

Every earlier suite, shipped configuration, after the worker and api changes
(`scratch/p4c/regression.py`, logs `scratch/p4c/reg_*.log`):

    p1_verify              21/22   the documented environmental failure
    p1_verify_shutdown       5/5
    p1_verify_supervision   14/14
    p2_verify               34/34
    p3_verify               57/57
    p4a_verify              25/25
    p4b_verify              74/74

**230 of 231 — the documented baseline exactly, same single failure.**
`p1_verify`'s webcam check fails on "webcam produced sightings -- 0 sightings"
and needs something the COCO model calls a vehicle in front of the lens; the
reasoning is unchanged from 2026-09-02.

## What this screen does not do

- **No absolute times, and no map.** Deliberate — see above. A file has no
  location, so there is nothing to place and nothing to trace across.
- **Multiple images are uploaded one at a time.** The Sources screen's image
  flow takes several; this one takes the file you dropped. One analysis at a
  time is the constraint that makes a batch flow dishonest here — it would queue
  and look stalled.
- **The result is not persistent.** A restart loses every job and sweeps its
  frames. Export before you restart; that is what the export is for.
- **A one-frame read is a weak read**, and the screen shows the vote size beside
  every plate so that it reads as one. Nothing here changes the OCR ceiling
  CLAUDE.md already measures.

## Current state

`data/anpr.db` is exactly as this session found it — 6 sources, all `done`, 164
sightings. Nothing this session ran wrote to it: the verification used a
throwaway database, and `scratch/bench_realvideo.py` cleans up its own rows.
`data/analyze` is empty, every job directory this session created having been
deleted with its job. `footage/uploads` has no file this session added.

`web/dist` is rebuilt: 477.6 kB js, 33.4 kB css.

Ready for P4d.

# P4d - Trace

P4d complete. Server stopped, port released.

## Exit criteria - verified

> *searching a plate and pressing play animates its path with crops in sync*

`scratch/p4d_verify.py`, **81 checks passed, 0 failed, 0 skipped, 2s**
(`scratch/p4d/p4d_verify.log`). The app starts once with **no pipeline** against
a throwaway database, and everything below is done over HTTP the way the browser
does it. The fixture is constructed rather than sampled, because the leg
arithmetic needs gaps and distances whose right answer is known in advance and
no footage provides that: one vehicle, `MH15HY2237`, seen at three sources and
**read wrongly at every one of them**.

| check | result |
|---|---|
| no stored row holds the plate being searched for | stored as `MH15HY227`, `MH15HX2237`, `MN15HY2237` |
| the search finds it anyway | 3 candidates |
| every candidate carries a score | 0.97 / 0.90 / 0.90 |
| ...a sighting count and its sources | `MH15HY227`: 2 sightings, 2 sources |
| ...and how it matched | `plate`, `plate`, **`candidate`** |
| a read only findable through the vote's alternatives is found | `MH15HX2237`, via candidate |
| a different vehicle is never offered | `KA05MN7788` excluded |
| a sighting with no plate is not in the path | excluded, not an error |
| selecting a candidate returns a time-ordered path | west, east, nowhere, east |
| the gap is measured leaving one camera to arriving at the next | 120.0 s |
| the leg carries the distance | 1.3256 km |
| implied speed is that distance over that gap | **39.77 km/h**, and 1.3256 / (120/3600) = 39.77 |
| an unplaced source keeps its stop and loses its leg | lat null, distance null, speed null |
| the crops serve at the path the row stores | 200 `image/jpeg` |
| **pressing play** | the node section below |
| a deep link `/trace/MH15HY2237` survives a reload | 200, byte-identical to `/` |

### "In sync" is a claim about one number, and that is what was checked

There is no browser automation in this environment, so the criterion was not
verified by watching pixels. It did not need to be. The map draws the path up to
`activeIndex`, the evidence strip highlights the card at `activeIndex`, and both
get it from `indexAt()` over the same positions -- so the sync is not a
coincidence to be observed, it is one function whose answer has to be right.

`scratch/p4d/scrub_check.mjs` runs that function **in node, imported from the
file the bundle imports** -- `web/src/lib/timeline.js`, which is why the
arithmetic was extracted there rather than left inside the component. A Python
reimplementation would have been checking a second copy of it.

    positions            [0, 0.339, 0.833, 1] over a span of 360.0s
    unequal gaps         widest 0.494, narrowest 0.167 -- a time scrubber,
                         not a stepper through a list
    playing 0 -> 1       1000 frames, 0 of them backwards, 4 of 4 stops reached
    the crop under the head is the last sighting the clock has passed
                         0 of 1000 frames disagreed
    clicking a stop      lands on that stop, 4 of 4
    a vehicle seen once  span 0, position 0, no division by zero

The third of those is the exit criterion stated exactly: at every frame of
playback the highlighted crop is the last sighting whose timestamp the head has
passed, and the next sighting has not happened yet.

### And on real rows this session did not write

Section H opens the **application database read-only** and traces a vehicle out
of it. `MH15JS4241` returns 7 sightings over 3 sources spanning 65945.6 s, every
stop scoring 1.00, every crop present on disk. `MH15HY2237` returns a journey
assembled from `MH15HY22` and `MH15HY227` -- two reads of one vehicle that no
equality test connects, which is the P3 claim arriving on screen.

## What was built

| file | what |
|---|---|
| `app/api.py` | `/api/search`, `/api/trajectory`, `/api/sightings/{id}` |
| `web/src/screens/TraceScreen.jsx` | was an 11-line placeholder; now the screen |
| `web/src/components/TrajectoryPath.jsx` | new. The path, numbered stops, the drawn-so-far line |
| `web/src/components/TimeScrubber.jsx` | new. Play, the track, a tick at every stop |
| `web/src/components/EvidenceStrip.jsx` | new. The crops, scrolled to follow the head |
| `web/src/lib/timeline.js` | new. The scrubber's arithmetic, so node can check it |
| `web/src/lib/format.js` | `durationText` |
| `web/src/lib/api.js` | the three calls |
| `web/src/components/MapCanvas.jsx` | exports its tile constants; nothing else changed |

Nothing in `app/` outside `api.py` was touched. `matching.py` and
`trajectory.py` are P3's and are used exactly as they were written -- the two
routes are read-only views over them and decide nothing the matcher does not
already decide.

## The one real defect, and it was in the copy

The screen first said, under the search box, *"Partial and misread plates are
fine"*, and its empty state said *"Try fewer characters -- a partial plate
matches more."* **Both were false, and the verification is what caught it.**

`matching.similarity` normalises by the longer string, so six characters of a
ten-character plate cannot score above about 0.67 however right they are:
`MH15HY` against `MH15HY227` scores **0.6667**, under the 0.72 floor. Typing
half a registration returned "nothing matches" about a vehicle sitting in the
database, and the copy was telling people to do the thing that makes that worse.

The floor was not moved -- it is P3's and it is load-bearing. What changed is
that a refused near-miss is no longer invisible: `/api/search` returns
`closest`, the best candidate below the floor, **only when the result is empty**,
and the screen names it and offers a button that searches down to it.

    MH15HY  ->  0 results, floor 0.72
                closest MH15HY227 at 67%, over 2 sightings
                [Search down to 67%]  ->  min_score 0.66  ->  found

The button floors the score rather than rounding it, and that is not a detail:
0.6667 **rounded** to 0.67 is a floor above the candidate it was offering, and
the button would have found nothing. The verification made exactly that mistake
first, and is now written to match the screen.

## Decisions, and what each one refuses

**The URL is the query.** `/trace/MH15HY2237` is the search, so a deep link
survives a reload through the SPA fallback and "Trace this vehicle" from the
Live feed or from Analyze is a link and nothing more.

**A candidate is opened automatically only when the answer is not a choice.**
One result, or a result whose plate is exactly what was asked for -- which is
how "Trace this vehicle" arrives, carrying a plate string that came out of the
database in the first place. Anything else stays a list. Picking the top row and
drawing its path would be the silent single answer this screen exists to avoid,
and it would do it at the moment the evidence is weakest.

**The scrubber runs over time, not over the list of stops.** Four stops are not
four equal steps: a vehicle can pass two cameras eight seconds apart and then
take four minutes to reach the third. Indexed by stop, those two draw as the
same interval -- a picture of a journey that never happened. On the fixture the
ticks land at 0, 0.339, 0.833 and 1.

**An analysis was never a sighting (P4c); a trajectory is never a write.**
Section F checks that three ways: the sighting, source and alert counts are
identical before and after the whole run, and the trace routes contain no
`INSERT`, `UPDATE` or `DELETE` at all.

**The evidence panel reads the row rather than the trajectory widening to feed
it.** The panel shows the track id and the alternative readings; a stop does not
carry them. Putting them in the trajectory would make every trajectory pay for a
panel that opens on one stop at a time, so `/api/sightings/{id}` returns the row
instead. The `sightings` table is unchanged -- no field was added to it.

**A stop at an unplaced source stays on the path.** The vehicle was seen there.
It gets no marker, its legs have no distance and no speed, and the screen says
so in words -- "not placed" in the table and a count in the header -- rather
than inventing a position for it.

**Implied speed is coloured, not judged.** Above 150 km/h the figure turns red
with a sentence saying to check both reads. It raises nothing and writes
nothing: impossible-transition alerts are P5's, built from these same numbers.

## What this screen does not do

- **It does not merge candidates.** Two reads of one vehicle stay two rows in
  the list, each with its own score and count, because deciding they are the
  same vehicle is the decision the person is being asked to make. Opening
  either one gathers both, since the trajectory matches fuzzily too.
- **It does not animate a marker along the road.** The path is drawn to the
  stop the head has passed; between two cameras there is no evidence of where
  the vehicle was, and interpolating one would draw a vehicle onto a road
  nobody saw it on.
- **A negative gap is shown as negative.** Two sightings that overlap in time
  mean a track split, or two cameras whose views overlap, and `-2.9s` says so
  where a clamped `0s` would hide it. The application database contains these.
- **Two sources can share a name.** The same clip added twice does exactly
  that, and the table then shows several rows reading `20260901_152733`. The
  source_id is on the cell as a title; nothing else distinguishes them, because
  nothing else in the data does.

## Regression

Every earlier suite, shipped configuration, after the `app/api.py` change
(`scratch/p4d/regression.py`, logs `scratch/p4d/reg_*.log`). `p4c_verify` is in
the list now as well -- Analyze was the last phase to land, and its suite is the
one most likely to notice a change to `api.py`:

    p1_verify              21/22   the documented environmental failure
    p1_verify_shutdown       5/5
    p1_verify_supervision  14/14
    p2_verify              34/34
    p3_verify              57/57
    p4a_verify             25/25
    p4b_verify             74/74
    p4c_verify             75/75

**305 of 306 -- the documented 230-of-231 baseline plus P4c's 75, with the same
single failure.** `p1_verify`'s webcam check fails on "webcam produced sightings
-- 0 sightings" and needs something the COCO model calls a vehicle in front of
the lens; the reasoning is unchanged from 2026-09-02.

## Current state

`data/anpr.db` is exactly as this session found it -- 6 sources, all `done`, 164
sightings, 0 alerts. Nothing this session ran wrote to it: the verification used
a throwaway database, and the three evidence crops its fixture needed under
`crops/evidence/p4dtmp/` were deleted with it.

`web/dist` is rebuilt: 497.0 kB js, 36.5 kB css.

Ready for P5.

---

# P4e - Insights

**Scope, because CLAUDE.md does not define a P4e.** The build phases run P4a-P4d
then P5, and P5 is *Insights and Alerts*. Asked which half "4e" meant, the answer
was Insights only: `heatmap toggle, vehicle count over time, type distribution,
per-source density ranking, origin-destination flow lines weighted by volume, one
shared time-window filter across all panels`. **Alerts, blacklist matching and
impossible-transition detection are untouched and remain P5.** `app/alerts.py` is
still an empty stub and `AlertsScreen` is still its placeholder.

**The screen is written and its server half is verified. `web/dist` is not built,
and this session could not build it.** Details in "The build, and what I broke"
below -- read that before treating the phase as finished.

## Exit criteria - verified

There is no exit line to quote, so the criterion taken was the specification
above read strictly, plus the two rules that govern every screen: never render
placeholder data, and an empty state says what to do next.

`scratch/p4e_verify.py`, **125 checks passed, 0 failed, 1 skipped, 2s**
(`scratch/p4e/p4e_verify.log`). The app starts once with **no pipeline** against a
throwaway database and everything is done over HTTP the way the browser does it.
34 of the 125 run in node: 26 against `web/src/lib/insights.js` -- the module the
bundle imports, not a Python copy of it -- and 8 parsing each new frontend file,
which is the largest claim available about the frontend while the bundle cannot
be built.

| check | result |
|---|---|
| one shared time filter across all panels | **the filter is shared in the data**: the header, the time chart, the type split, the source ranking and the map all reconcile to the same row count, and to each other again through a window |
| vehicle count over time | 55 buckets, width chosen off a ladder (10s for a 542s span), aligned to the clock, empty intervals left empty |
| type distribution | car 4 / truck 2 / motorcycle 1 / bus 1 / **unknown 1**, shares summing to 1 |
| per-source density ranking | count **and** rate: west and east both saw 4, east across 300s and west across 544s, so 48/h against 26.5/h -- the two rankings disagree, which is why both ship |
| heatmap | 2 placed points weighted 4 and 4, the unplaced camera contributing none, and its 1 sighting reported rather than dropped |
| origin-destination flows, weighted by volume | 3 flows; the crossing is found although **no two rows share a plate string** |
| ...direction is a fact | `east -> west` stays separate from `west -> east` |
| ...distance and speed | 1.3256 km over a 120.0s gap = **39.77 km/h**, the same arithmetic P4d checks |
| ...and the ones that cannot be drawn | the flow into the unplaced camera keeps its gap, loses distance and speed, and is counted |
| a source that saw nothing keeps its row | `quiet` at 0, `per_hour` null, still ranked |
| Insights reads and only reads | sighting/source/alert counts identical before and after; no INSERT/UPDATE/DELETE in the route or the module |
| a bad time is refused with a sentence | `'nonsense' is not a time this understands. Use something like 2026-09-01T15:19:00Z.` |
| a backwards window is refused | `The window ends before it starts. Swap the two times, or clear one of them.` |
| an empty window is not an empty database | the answer still reports the extent, so the screen says which kind of empty it is |
| **deep link `/insights` survives a reload** | **SKIPPED -- `web/dist` is not built** |

### And on real rows this session did not write

Section K opens the **application database read-only**: 164 sightings, 34 plated,
6 of 6 sources but **1 placed**, bucketed at 1 hour over 49 intervals. **12
vehicles identified, 10 of which moved between sources.** Every panel reconciles
on those rows, and the map reports 116 sightings at 5 unplaced sources that it
cannot show -- which is the state the screen has to handle honestly, not an
inconvenience.

## What was built

| file | what |
|---|---|
| `app/analytics.py` | was an empty stub; now the whole server half |
| `app/api.py` | `GET /api/insights` -- one route, every panel |
| `web/src/screens/InsightsScreen.jsx` | was an 11-line placeholder; now the screen |
| `web/src/components/TimeWindow.jsx` | new. The one filter |
| `web/src/components/CountsChart.jsx` | new. Vehicles per interval, stacked by read/unread |
| `web/src/components/TypeBars.jsx` | new. The type split |
| `web/src/components/SourceRanking.jsx` | new. Volume, or rate |
| `web/src/components/DensityMap.jsx` | new. Heat canvas overlay and the flow curves |
| `web/src/lib/insights.js` | new. The window arithmetic, so node can check it |
| `web/src/lib/api.js` | `getInsights` |
| `web/src/tokens.css` | two chart fills |

Nothing in `app/` outside `api.py` and the new `analytics.py` was touched. No
field was added to `sightings`. `matching.py` and `trajectory.py` are P3's and are
used exactly as written.

## Decisions, and what each one refuses

**One route, not five.** Every panel is a slice of one window, and five routes
would let five panels answer for five slightly different ones -- a source list
fetched a second after the chart, with a worker still writing, disagreeing with it
by two rows. So `/api/insights` fetches the sightings in the window **once** and
computes every panel from that row set. A shared filter is only shared if the
panels physically cannot disagree, and section B checks exactly that: five
independently computed panels all totalling the same number, twice.

**A preset window measures back from the newest sighting, never from
`Date.now()`.** This is P1's timestamp rule arriving in the UI. A recorded source
stamps its rows with when the footage was filmed, so "last 24 hours" against the
wall clock returns an empty screen for every clip processed yesterday while the
identical control works on a live camera -- and nothing downstream is allowed to
tell the two apart. Measured back from the newest row they behave identically: on
a live feed the newest row *is* about now. The labels say "of data". Checked in
node, including that the window deliberately does **not** track the wall clock.

**The heatmap is a density map of cameras, and says so on the screen.** The
database records which camera saw a vehicle and where that camera stands. Nothing
anywhere records where the vehicle was between two cameras. So each placed source
gets a blob weighted by what it saw, the radius is a real distance on the ground
rather than a fixed number of screen pixels, and the panel prints how many
sightings it cannot place. Smearing a vehicle along a guessed route would draw
traffic onto roads nobody filmed -- the same refusal that keeps an unplaced source
off the map in P4b and P4d.

**Flows are assembled by fuzzy match and the threshold is stricter than
Trace's.** `/api/search` uses 0.72; this uses **0.80**. On Trace a person reads
the ranked candidates and decides. Here nobody does -- the answer is drawn on a
map as a journey that happened. 0.80 was measured, not chosen: over the
application database's 34 reads and 78 pairs, the one pair that is genuinely one
vehicle (`MH15HY22` / `MH15HY227`) scores **0.889** and the closest pair that is
two different vehicles (`MH07A3866` / `MH07T3336`) scores **0.667**, so every
threshold from 0.68 to 0.88 groups identically and 0.80 sits in the middle of that
band rather than on an edge. It is a query parameter, so it can be moved without a
code change.

**A flow line is a curve on purpose.** Straight, `A -> B` and `B -> A` land
exactly on top of each other and the busier direction is invisible; the bend is
always to the left of travel so the two separate. It also stops the line reading
as a route, which it is not. Checked in node: the two directions bulge to opposite
sides of the chord, and the bulge is 8% of it.

**Density is reported two ways because a count is not a density.** A 20-second
clip that saw 15 vehicles and a 200-second one that saw 66 are 2610 and 1188
vehicles an hour -- the shorter clip is the busier road and the count ranking says
the opposite. Both numbers are on every row and the sort is a control, so the
screen never quietly picks one and calls it "density". A source whose sightings
share one instant, such as a still image, gets `per_hour: null` and sorts last
rather than being given a zero it did not earn.

**A camera that saw nothing keeps its row.** Dropping it would make the ranking
look like a list of all the cameras. So would dropping a sighting whose source has
since been deleted -- that one gets a row reading `(source removed)`, or the
ranking column stops adding up to the header.

**Empty buckets are drawn empty.** A gap in traffic is a fact about the footage,
and a chart that closes it up draws a busier road than the one filmed. Buckets are
aligned to the epoch rather than to the first sighting, so a boundary is a round
time on a clock and two windows over the same footage line up instead of being
offset by whenever each query began.

**Colour carries the meanings the app already has, and nothing else.** The time
chart's two fills are PlateString's own grounds -- a read plate sits on
plate-white, `NO READ` sits on slate -- so a bar segment and an evidence card say
the same thing in the same colour. The type panel is a **ranked bar, not a
donut**, because five types would need five identities carried by colour and this
product has no five-hue categorical palette to give them: its colours are plate
grounds and each already means something. So the bar colour carries the one split
that is real -- yellow commercial, white private, exactly the split `VehicleBadge`
makes beside every sighting -- and which of the five types a bar is, is carried by
the label on it. Identity is never colour alone. The three fills were run through
the palette validator against the card surface: CVD delta-E 15.6 or better on
every simulated deficiency, all three over 3:1 contrast. The one measured change
was lifting the neutral off `surface-3`, which does not clear the contrast floor.

## Recharts is in the stack and is not used

CLAUDE.md's stack names Recharts. It was fetched and inspected, and **not
adopted**: `recharts@3.10.1` hard-depends on `@reduxjs/toolkit` and `react-redux`,
and CLAUDE.md bans "Redux or any state library" outright, so using it would ship
the banned dependency inside the bundle. The working rules also say plainly: do
not add dependencies. `package.json` is unchanged and the two charts are inline
SVG on the app's own tokens -- which is also how every other visual in this app is
built, so the screen is more consistent rather than less.

**`web/node_modules` does now contain recharts and its 11 transitive packages,
extraneous to `package.json`.** Nothing imports them and nothing bundles them.
`npm install` inside `web/` prunes them back.

## The build, and what I broke

**`web/dist` was deleted and could not be rebuilt. The UI is unavailable until
someone runs `cd web && npm run build`.** The API is unaffected -- FastAPI serves
its "frontend has not been built yet" page for `/`, by design.

What happened, in order:

1. `npm run build` hung. So did `node node_modules/vite/bin/vite.js build`, from
   Bash, from PowerShell, from a Python subprocess, inside the sandbox and outside
   it, with `maxParallelFileOps: 1`, and with **every P4e file removed and
   `InsightsScreen` reverted to its placeholder** -- which is what establishes
   that the fault is not in this phase's code. An instrumented build shows rollup
   loading and transforming all 426 modules, including every new file without
   error, and then never reaching `buildEnd`.
2. I wrote a fallback bundler (esbuild + postcss, the same inputs) to get a
   working `dist` anyway. **It cleared `dist` first and then hung in the same
   place**, leaving `web/dist` with one stylesheet and no `index.html`. I removed
   the remains. `web/dist/` is gitignored, so the P4d bundle is not recoverable
   from the repo -- only by rebuilding.
3. The `esbuild.exe` binary hangs bundling too, and so does a plain Python loop
   reading `web/node_modules`. **Fourteen node processes are stuck, every one of
   them at 2.0-2.4 CPU seconds and ~175 MB**, which is a cap rather than slowness:
   everything under that budget ran fine all session -- the node window check,
   Tailwind over `tokens.css` in 0.2s, `esbuild.transform`, a 20-second
   CPU-burning loop. Nothing above it finished. This is an environment limit on
   child processes in this session and I could not work around it.

**Recovery is one command in a normal shell**, and it is the only thing
outstanding on this phase:

    cd web
    npm install     # also prunes the extraneous recharts packages
    npm run build

`scratch/p4e_verify.py` section I is written to catch a stale result rather than
pass on one: it skips when `dist` is absent, and when `dist` is present it reads
the built bundle and fails unless `/api/insights`, `Density is measured at the
cameras` and `Vehicles that moved` are actually inside it. Run it after the build
and the skip becomes a pass; that is the one check this phase has not made.

## Regression

Every earlier suite, shipped configuration (`scratch/p4e/reg_*.log`):

    p1_verify              21/22   the documented environmental failure
    p1_verify_shutdown       5/5
    p1_verify_supervision  14/14
    p2_verify              33/34   the documented ocr_tworow500 calibration failure
    p3_verify              57/57
    p4a_verify             26/26
    p4b_verify             74/74
    p4c_verify             74/75   deep link 503 -- web/dist is missing
    p4d_verify             78/78   + 1 skipped, deep link -- web/dist is missing

**382 passed, 3 failed, 1 skipped.** Two of the three failures are the ones
CLAUDE.md already records: `p1_verify`'s webcam check needs a vehicle in front of
the lens, and `p2_verify` inspects the highest-confidence plated row on
`20 sec.mp4`, which since the OCR promotion is a truck with no legible plate.
**The third is not pre-existing and is mine** -- `p4c_verify`'s deep link, and
`p4d_verify`'s skip, are both the deleted `web/dist`. Both go green again on a
rebuild; neither is a code change.

## Current state

`data/anpr.db` is exactly as this session found it -- 6 sources, 164 sightings, 0
alerts. Nothing this session ran wrote to it: `p4e_verify` uses a throwaway
database and section K opens the real one read-only.

`config/settings.yaml`, every model file and the whole inference pipeline are
untouched. No benchmark was run and none needed to be -- this phase reads rows, it
does not produce them.

Ready for P5 (Alerts: blacklist matching on write, impossible-transition
detection, the Alerts screen) once `web/dist` is rebuilt.

# P5 - Alerts

**Scope.** CLAUDE.md's P5 is *Insights and Alerts*. Insights shipped in P4e and
is untouched here -- `scratch/p4e_verify.py` is its suite and it still runs. This
phase is the Alerts half: blacklist matching on write, impossible-transition
detection from source distance and elapsed time, and the Alerts screen.

## Exit criteria - verified

> a blacklisted plate raises a visible alert within seconds of its sighting

`scratch/p5_verify.py`, **109 checks passed, 0 failed, 0 skipped, 19s**
(`scratch/p5_verify.log`). Sections A-H run against a throwaway database; section
I is the exit line itself, measured through the real pipeline on real footage.

**The measured answer is 0.016 s.**

Section I does not take the plate from a document, because a document can go
stale under an OCR change. It runs `footage/clips/20 sec.mp4` twice through real
worker processes:

| | |
|---|---|
| run 1, empty blacklist | 11 sightings, 2 plate reads, **0 alerts** -- the control |
| the plates that clip actually read | `MH15BY2231` 0.72, `MH15HY2277` 0.56 |
| blacklist edited **while the app is running** | watching `MH15BY2231` |
| run 2, same clip, same code | **critical alert raised**, reason from the file in the sentence |
| sighting committed to alert row written | **0.016 s**, against a 5 s deadline |
| the alert on the websocket | 1 event, so the Live strip fills with no poll |
| impossible transitions invented on two unplaced sources | **0** |

The latency is taken at the two moments rather than from `created_ts`: that
column has millisecond resolution and the distance being measured is smaller
than one millisecond tick.

| check | result |
|---|---|
| A surface | both routes validate and answer in sentences: an unknown kind names the two that exist, an unknown severity names the three |
| B the file is the control | both entry shapes, normalisation, hot reload with no restart, and **every bad line named with its reason** |
| ...a broken file is an ERROR | not silently an empty blacklist -- the failure mode that matters |
| C matching | exact gives `critical`, one confusable glyph gives `warning` with its cost, a hit through `plate_candidates` says so |
| ...the gate, on the pairs it was set from | `HR15B1238`/`HR15B1738` 1.00 matched, `HR03A7979`/`HR03X7979` 1.00 matched, `HR15B1738`/`MP15B1738` **1.35 refused** |
| D transitions | 1.3256 km in 4.0 s = **1193 km/h**, the same arithmetic P4d checks, to the digit |
| ...and every case that must not alert | 39.8 km/h, two cameras 22 m apart, an unplaced camera, one camera twice, two different vehicles close in time -- **all silent** |
| ...one vehicle, two cameras, one instant | `critical`, and the sentence says "at the same moment" rather than printing an infinite speed |
| E dedup | a re-emitted track writes one alert; a pair reached from either end is one alert |
| ...and the route is read-only | reading writes nothing, and the route body contains no INSERT/UPDATE/DELETE |
| F hydration | both crops, both timestamps, distance, speed, the limit, in clock order |
| ...an alert outliving its evidence | names what is missing rather than pointing at nothing |
| G deep link | `/alerts` survives a reload, `/api/` is still not swallowed, and the built bundle carries the screen |
| H node | all four changed frontend files parse |

## What was built

| file | what |
|---|---|
| `app/alerts.py` | was an empty stub; now both checks, the watched file, and hydration |
| `app/pipeline.py` | the checks run on the writer thread, in the commit path |
| `app/api.py` | `/api/alerts` hydrated and filterable, `/api/blacklist` |
| `app/config.py` | `blacklist_path()` |
| `app/matching.py` | `plate_forms`, a public name for `_stored_forms` |
| `config/blacklist.yaml` | was empty; now the documented file, shipping empty |
| `config/settings.yaml` | `paths.blacklist` and four `alert_*` defaults |
| `web/src/components/AlertCard.jsx` | was empty; now both kinds, paired evidence |
| `web/src/screens/AlertsScreen.jsx` | was its placeholder; now the screen |
| `web/src/screens/LiveScreen.jsx` | the strip fills off the socket |
| `web/src/lib/api.js` | `getAlerts` filters, `getBlacklist` |

**No field was added to `sightings` and no table was added.** The data contract
is the same three tables it has been since P0.

## Decisions, and what each one refuses

**The gate is an edit COST, not a similarity score, and the number was measured
rather than chosen.** `matching.similarity` normalises by length, so one
tolerance means different things on an 8- and an 11-character plate. Over the 43
distinct plate strings in `data/anpr.db`:

    HR15B1238 / HR15B1738   1.00   one vehicle, read twice
    HR03A7979 / HR03X7979   1.00   one vehicle, read twice
    HR15B1738 / MP15B1738   1.35   TWO vehicles -- different state code

1.0 takes both re-reads and refuses the cross-state pair. As similarity those
same three are 0.889, 0.889 and 0.850 -- a **0.039** band to sit a threshold in,
which is the length dependence showing. Never string equality: the camera that
read `MH15HY22` and the entry that says `MH15HY2237` are the same vehicle.

**Severity is a claim about certainty, not a decoration.** `critical` is only
for a hit needing no interpretation: a character-for-character read of the
entry, or a vehicle at two separated cameras at one instant. Everything reached
by fuzzy matching is capped at `warning` and prints its cost against the limit,
because whoever reads it has to be able to disagree with it. A blacklist entry
may ask for a lower severity; it can never raise one.

**The checks run inside the commit path, on the writer thread.** "Within seconds
of its sighting" is not achievable by polling, and the alert has to be written
by the one writer like everything else. They are wrapped: a failing check costs
its alert and never the writer, because stopping the writer stops every source.

**The blacklist is a file, re-read on mtime, and the file is the interface.**
Asked, the decision was config over a fourth table. So the screen names the path
instead of offering an add button, and an edit takes effect on the next sighting
with nothing to restart -- verified in section I by editing it mid-run. The
stamp carries size as well as mtime: two saves inside one filesystem tick are
real on Windows, and a blacklist edit that does not take effect is the failure
this whole mechanism exists to avoid.

**A file that does not parse is an error that is reported, not an empty
blacklist.** A watch list that silently became empty is worse than one that says
it is broken. Every unusable line is kept with its reason and shown on the
screen.

**Alerts are hydrated in the read route, not stored wide.** An impossible
transition is unreadable without both crops, both timestamps, the distance and
the speed, and none of that belongs in the frozen seven columns. It is derived
from the sightings the alert names, so the table stays as it was and the screen
still gets what it has to show.

**The transition leg is `last_seen` to `first_seen`**, the same convention
`app/trajectory.py` uses. Charging the vehicle for the time it spent inside the
first camera's view would understate every speed. Checked to the digit against
`haversine_km` in section D.

**An unplaced camera raises nothing.** It has no distance to anywhere. That is
not an error and not a reason to invent one -- the same refusal that keeps an
unplaced source off the map in P4b, P4d and P4e.

**The blacklist ships empty.** A plate in it that nobody chose would raise an
alert about a real vehicle in the footage, which is the "never render data
nobody asked for" rule pointed at configuration. Two plates this database
actually read are in the file as commented examples, so watching it work is one
uncommented line.

## Regression - 490 passed, 3 failed

    p1_verify              21/22   the documented environmental failure
    p1_verify_shutdown       5/5
    p1_verify_supervision  14/14
    p2_verify              33/34   the documented ocr_tworow500 calibration failure
    p3_verify              57/57
    p4a_verify             25/25
    p4b_verify             74/74
    p4c_verify             75/75   was 74/75 -- the deep link passes now dist is built
    p4d_verify             79/81   two failures, NOT this phase -- see below
    p4e_verify            128/129  one failure, NOT this phase -- see below
    p5_verify             109/109

Logs in `scratch/p5/`. Two of the three failures are the ones CLAUDE.md already
records: `p1_verify`'s webcam check needs a vehicle in front of the lens, and
`p2_verify` inspects the highest-confidence plated row on `20 sec.mp4`, which
since the OCR promotion is a truck with no legible plate.

**The p4d and p4e failures are the application database, not the code, and that
was established rather than assumed.** Both are in those suites' "real data"
sections, which read `data/anpr.db` directly. That database is no longer the one
P4e measured -- it now holds **346 sightings from 1 source, with no coordinates
and no `MH15JS4241`** -- so `p4d_verify` asks for a multi-stop journey by a plate
that is not there, and `p4e_verify` asks for origin-destination flows across a
single camera. **Re-running both suites with every P5 change stashed produced the
identical counts, 79/2 and 128/1.** Both go green again on a database with two
placed sources in it; neither is a code change.

## Runnable

`app/run.py`'s own startup path -- `ensure_dirs`, `init_db`, `seed_sources`, a
real `Pipeline`, `create_app` -- against the real application database:

    200  /api/health     {"status":"ok","db":true,"sources":1,"sightings":346,"ui_built":true}
    200  /api/alerts     []
    200  /api/blacklist  {"path":"config/blacklist.yaml","count":0,"error":null}
    200  /alerts         the built UI

It ran on port **8016**, not 8000, because **a python process started at 14:16
is still holding 8000** and it is running pre-P5 code -- `/api/blacklist` answers
`No API route` on it. That is the trap P0 recorded: a stale server on this
machine needs `taskkill`, not `kill`. Stopping it and running
`python -m app.run` again is what puts P5 in a browser.

## Current state

`data/anpr.db` is exactly as this session found it -- 1 source, 346 sightings, 0
alerts. `p5_verify` uses a throwaway database and a throwaway blacklist file, and
removes the crops its two worker runs wrote.

`config/blacklist.yaml` ships empty. `config/settings.yaml` gained
`paths.blacklist` and four `alert_*` defaults and changed nothing that existed.
Every model file and the whole inference pipeline are untouched: no benchmark was
run and none was needed -- this phase reads rows and writes alerts, it does not
produce sightings.

`web/dist` is built. The P4e session could not build it; `npm run build` finished
here in 1.6s, so that was an environment limit on child processes in that
session and it is gone.
