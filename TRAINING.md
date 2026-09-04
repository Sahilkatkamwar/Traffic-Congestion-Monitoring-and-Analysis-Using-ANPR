# TRAINING.md

Training track. Runs **parallel to** the app build in `CLAUDE.md`, never inside it.

## The one rule connecting the two tracks

Training produces a weights file. The app loads weights from
`config/settings.yaml`. Swapping a fine-tuned model in is a config edit and
nothing else.

As shipped today, verified against `config/settings.yaml` on 2026-09-02:

```yaml
models:
  vehicle_detector: models/yolo11n.pt        # fine-tunes trained + rejected, Fine-tune 4
  plate_detector:   models/finetuned/plate_det_scenes.pt   # won the A/B, see below
  vehicle_classes:  null                                   # trained, not adopted
  ocr:              models/finetuned/ocr_best.pt           # null falls back to fast-plate-ocr
  reid_embedder:    null                     # null uses models/yolo11n-cls.pt
```

`reid_embedder` is not a fine-tune and there is no training track for it. It is
an appearance descriptor for telling one physical vehicle from another across
tracker fragmentation, and it reuses `yolo11n-cls.pt` — already present as
Fine-tune 1's starting weights — purely as a feature extractor. Its ceiling is
that these are ImageNet features rather than vehicle re-identification features;
CLAUDE.md records what that costs. Training a real ReID embedding would be a new
fine-tune and would need identity-labelled crops, which this project does not
have.

If any of these is null the app must still run using the pretrained default.
The app never blocks on a model that hasn't been trained yet. `vehicle_classes`
being null is a decision, not an omission — the reason is under Fine-tune 1.

**Nothing in `scripts/` may import from `app/`, and nothing in `app/` may import
from `scripts/`.** The two tracks share files on disk, never code.

## Model inventory

| file | source | trained by you? | status |
|---|---|---|---|
| `models/yolo11n.pt` | Ultralytics, COCO | no | in use |
| `models/plate_detector.pt` | morsetechlab v1n | no | superseded, kept for A/B |
| `models/yolo11n-cls.pt` | Ultralytics, ImageNet | starting weights only | **in use** as the re-id embedder |
| `models/finetuned/plate_det_scenes.pt` | you | **yes** | **in use** |
| `models/finetuned/plate_det_best.pt` | you | yes | rejected, kept for the record |
| `models/finetuned/vehicle_cls_best.pt` | you | **yes** | trained, **not adopted** |
| `models/finetuned/ocr_tworow500.pt` | you | **yes** | **in use** since 2026-09-04 |
| `models/finetuned/ocr_best.pt` | you | **yes** | superseded 2026-09-04; **the rollback**, also copied to `ocr_best_rollback.pt` |
| `models/finetuned/ocr_layouts.pt` | you | **yes** | trained, **not adopted** |
| `models/finetuned/vehicle_det_road_frozen.pt` | you | **yes** | trained, **not adopted** |
| `models/finetuned/vehicle_det_road_full.pt` | you | **yes** | rejected, catastrophic forgetting |
| `models/finetuned/vehicle_cls_aug.pt` | you | **yes** | trained, **not adopted** |
| `models/finetuned/vehicle_cls_repro.pt` | you | **yes** | reproduces `vehicle_cls_best` exactly; kept as the reproducibility check |
| `models/finetuned/vehicle_cls_scale.pt` | you | **yes** | rejected, worse on every class |
| `models/finetuned/plate_det_frames.pt` | you | **yes** | trained, **not adopted** -- 3 false reads on the plate-less control |
| `models/finetuned/plate_det_frames_c.pt` | you | **yes** | trained, **not adopted** -- 4 false reads, fewer true ones |
| `models/finetuned/ocr_tworow_*.pt` (6) | you | **yes** | trained, **not adopted**; untouched by the 2026-09-03 inference pass |

---

# Fine-tune 1 — Vehicle type classifier

Fixes the autorickshaw gap. Do NOT retrain the COCO detector *for this*: it
needs box annotations and single-class fine-tuning causes catastrophic
forgetting, so you'd gain autos and lose cars.

(Since 2026-09-02 there *are* vehicle boxes, in `data/vehicle_det` — see
Fine-tune 4. They do not change this: that set has no `auto` class at all, it
labels autorickshaws `car`, so it cannot teach the auto/car distinction this
fine-tune exists for. And the forgetting predicted here was then measured, in
Fine-tune 4's `road_full` run.)

Instead: COCO YOLO detects the vehicle as it already does, then a small
classifier assigns the real type from the crop.

## Data

Folder name is the label. No annotation files.

```
data/vehicle_cls/
  train/  auto/ car/ motorcycle/ bus/ truck/      ~600 each, ~400 for bus/truck
  val/    auto/ car/ motorcycle/ bus/ truck/      ~150 each
```

**Source: your own P1 output.** The camera worker saves every vehicle crop to
`crops/unsorted/`. Run it over your footage, then sort the crops into folders by
hand. The data then matches your deployment conditions exactly, which no
downloaded dataset will.

## Training

`scripts/train_vehicle_cls.py` — Ultralytics classification, `yolo11n-cls.pt`
starting weights, 128px, batch 32, ~30 epochs.

Roughly 20–40 minutes on a GTX 1650. This is the cheap one; do it first.

## Exit criterion

Validation accuracy per class printed, with a confusion matrix. Auto vs car
confusion is the number that matters — everything else COCO already handles.

## Result — trained 2026-09-02, and NOT adopted

`python scripts/train_vehicle_cls.py --epochs 30`. 30 epochs at 128px, batch 32,
lr0 0.001, seed 1337, `yolo11n-cls.pt` starting weights, 236s on the 3050.
Hyperparameters and both matrices in `runs/vehicle_cls_metrics.json`, weights at
`models/finetuned/vehicle_cls_best.pt`, per-epoch checkpoints under
`runs/vehicle_cls/weights/`.

Data as it actually exists — not the sizes sketched above:

    class         train    val
    auto            800    100
    bus            1000    176
    car             790    106
    motorcycle      800    145
    truck           577    100
    TOTAL          3967    627

Validation, argmax over all 627 crops. **Accuracy 450/627 = 71.8%, macro F1
0.621:**

| class | precision | recall | F1 | support |
|---|---|---|---|---|
| auto | 0.769 | **0.900** | 0.829 | 100 |
| bus | 0.593 | 0.960 | 0.733 | 176 |
| car | 0.744 | 0.547 | 0.630 | 106 |
| motorcycle | 0.911 | 0.917 | 0.914 | 145 |
| truck | **0.000** | **0.000** | **0.000** | 100 |
| macro avg | 0.603 | 0.665 | 0.621 | 627 |

Confusion matrix, rows = truth, columns = prediction:

                    auto     bus     car  motorc   truck
    auto              90       9       0       1       0
    bus                3     169       3       0       1
    car               11      26      58      11       0
    motorcycle         5       6       1     133       0
    truck              8      75      16       1       0

Under the shipped `vehicle_cls_conf` floor of 0.55 it abstains on 19/627 = 3.0%
of crops and scores 444/608 = 73.0%, macro F1 0.634. Abstention barely helps,
because the model is confidently wrong about trucks rather than uncertain.

**The number TRAINING.md says matters is good.** Auto called car: **0 of 100.**
Car called auto: 11 of 106. The autorickshaw gap this fine-tune exists to close
is closed.

**Truck collapsed completely** — 75 of 100 trucks called bus, F1 0.000 — and the
cause is the data, not the training. There is no train/val leakage (checked by
content hash: 115 exact duplicate images, 0 of them spanning the two splits).
The splits are simply **different collections**:

- `train/truck` is one narrow set — European motorway surveillance crops of
  articulated semi-trailers, near-identical framing, median 131×122 px JPEG.
- `val/truck` is a broad global mix — pickups, tippers, tankers, fire trucks —
  uniform 192×192 PNG.
- `train/auto` is web images, `val/auto` is 2448×3264 Datacluster Labs phone
  photos. Auto generalises across that gap anyway; truck does not.

Training loss falls to 0.028 while val loss climbs from 1.25 to 2.53 and val
top-1 never leaves ~70% after epoch 1. The model memorised each class's
collection artifacts. It never saw a pickup, so it calls one a bus.

**Decision: `models.vehicle_classes` stays `null`.** Adopting this would trade a
class COCO already handles well (truck) for one it handles badly (auto), and
CLAUDE.md's own rule — keep baseline unless the fine-tune beats it on your own
footage — points the same way. The weights are saved and the config swap is one
line whenever the data is fixed.

**To fix it, follow the instruction already in this file:** the source is
supposed to be your own P1 output. `crops/unsorted/` currently holds 1163 real
crops from this project's footage, unsorted. Sorting those by hand with
`scripts/sort_vehicle_crops.py` and splitting them into train/val — one
collection, split randomly, so train and val are the same distribution and that
distribution is the deployment one — is what makes this number mean something.

### Retested 2026-09-02 against `data/vehicle_det` — still not adopted

`data/vehicle_cls` **has not changed**: the counts above are current and every
file predates the new dataset. So the question was whether `data/vehicle_det`,
which is detection data, can supply what the classifier is missing.

Only part of it is usable as classification truth, and working out which part
is the result (`scratch/inf/det_as_cls.py`, `scratch/inf/cls_experiment.py`):

| class in `vehicle_det` | boxes | usable as classification label? |
|---|---|---|
| car | 561 | **no** — 19.1% are autorickshaws labelled `car` |
| motorcycle | 584 | **no** — 560 are under 48px |
| truck | 84 | yes, 75 above 48px |
| bus | 21 | yes |

The `car` exclusion is the important one. The pseudo-labeller was YOLO11-L +
RT-DETR-L, both COCO models, and **COCO has no autorickshaw** — so every auto in
those frames carries some other label, mostly `car`. Using them as truth teaches
the classifier that an auto is a car, destroying the one distinction this
fine-tune exists for.

The 96 valid crops were added to **train only**, val left byte-identical, and
-- `data/vehicle_cls_aug` is not kept on disk (it is a 712MB copy of
`vehicle_cls`); rebuild it in a minute with
`python scratch/inf/cls_experiment.py --build`, which is deterministic --
the model retrained as `models/finetuned/vehicle_cls_aug.pt` (same
hyperparameters: 30 epochs, 128px, batch 32, lr0 0.001, seed 1337). Macro F1
rises 0.621 → 0.652 and auto F1 0.829 → 0.883, but **truck recall reaches only
0.040** — 4 of 100, with 64 still called bus — and motorcycle F1 slips 0.914 →
0.889.

That was the predictable outcome. The truck collapse is a **collection**
mismatch — train/truck is European motorway semi-trailers, val/truck is a global
mix of pickups and tippers — and 75 crops from a single Indian CCTV camera is a
*third* collection, not a bridge between the first two.

On real footage the case closes: with the classifier enabled, `23sec.mp4` (a US
parking lot of ordinary cars) is labelled truck 42 / car 32 / bus 2 against the
COCO fallback's car 72 / truck 4. **`models.vehicle_classes` stays `null`.**

The fix has not changed and is the one this file already specifies: sort
`crops/unsorted/` — now 1354 real crops from this project's footage — by hand,
and split it randomly so train and val are one collection and that collection is
the deployment one.


## Re-run 2026-09-03 -- reproducible, and the cause of the truck collapse is now measured

Two more candidates, scored by `scratch/final/cls_score.py` on the identical
val set:

| model | accuracy | macro F1 | auto | bus | car | moto | **truck** |
|---|---|---|---|---|---|---|---|
| `vehicle_cls_best` | 0.718 | 0.621 | 0.829 | 0.733 | 0.630 | 0.914 | **0.000** |
| `vehicle_cls_aug` | **0.737** | **0.652** | **0.883** | **0.764** | **0.650** | 0.889 | 0.073 |
| `vehicle_cls_repro` | 0.718 | 0.621 | 0.829 | 0.733 | 0.630 | 0.914 | **0.000** |
| `vehicle_cls_scale` | 0.697 | 0.611 | 0.859 | 0.697 | 0.590 | 0.891 | 0.019 |

**`vehicle_cls_repro` establishes that this training is reproducible.** It re-ran
`scripts/train_vehicle_cls.py --epochs 30` with nothing changed and reproduced
`vehicle_cls_best` exactly — same accuracy, same macro F1, and the same confusion
matrix cell for cell. Worth having before trusting any comparison above it.

**`vehicle_cls_scale` tested the obvious hypothesis and refuted it.**
`scripts/train_vehicle_cls.py` grew `--scale`, `--erasing` and `--degrees`, all
defaulting to Ultralytics' own values so the original run still reproduces. The
candidate is `--epochs 40 --scale 0.9 --erasing 0.2 --degrees 8`, aimed squarely
at a measured shortcut: in TRAIN every class carries its own image-size
signature — bus is capped at 640 and never narrower than 363, truck never
exceeds 317 — while in VAL every class except `auto` is **exactly 192x192 png**.
A model can separate the train classes on scale alone and arrive at val with
nothing.

Destroying that shortcut made the model **worse**: accuracy 71.8% to 69.7%, macro
F1 0.621 to 0.611, car 0.630 to 0.590, truck still 1 of 100. So scale is not the
mechanism.

**What the mechanism actually is.** Every candidate sends **75 to 81 of the 100
val trucks to `bus`**. Asked of `models/yolo11n-cls.pt`, an ImageNet classifier
that never saw these labels (`scratch/final/cls_semantics.py`):

| folder | top ImageNet reads |
|---|---|
| **train/truck** | trailer_truck 41%, moving_van 35% |
| **val/truck** | trailer_truck 16%, **pickup 12%, fire_engine 10%, garbage_truck 9%** |

`truck` means "articulated lorry" in train and "any large utility vehicle" in
val. **F1 = 0.000 is the arithmetic consequence of that, not a training failure,
and no hyperparameter reaches it.** The same audit finds 14% of `train/bus` reads
as nothing vehicle-shaped, matching the face, traffic light and city skyline
visible in a random sample of that folder.

**Decision unchanged: `models.vehicle_classes` stays `null`.** The fix is still
the one this file has always named — hand-sort `crops/unsorted/` so that `truck`
denotes one thing and train and val are one collection. Until then this is a
documented non-blocking limitation and vehicle type comes from the detector's
COCO label.

---

# Fine-tune 2 — Plate OCR

The expensive one, and the only place your accuracy ceiling actually moves.

## Data

Tight crops of the plate rectangle only — no vehicle body.

```
data/ocr/
  train/            000001.jpg, 000002.jpg, ...
  train_labels.txt  filename \t PLATESTRING
  val/
  val_labels.txt
  test/             YOUR footage only
  test_labels.txt
```

Label file format, tab separated, one line per image:

```
000001.jpg	MH12AB1234
000002.jpg	KA05MN7788
```

Charset is uppercase A–Z and 0–9 only. 36 classes. No spaces, no dashes, no
lowercase. Save crops at native size; the loader normalises to 128×64
(`IMG_H, IMG_W = 64, 128` in `scripts/train_ocr.py`, `keep_aspect_ratio: false`).

## Composition — as built, counted 2026-09-03

    data/ocr/synth   40000 images   synth_labels.txt   40000 lines
    data/ocr/train    1179 images   train_labels.txt    1179 lines
    data/ocr/val       252 images   val_labels.txt       252 lines
    data/ocr/test      252 images   test_labels.txt      252 lines
    data/ocr/manifest.csv           1683 rows = the three real splits

**Real two-row**, added 2026-09-03, kept in its own directory rather than
merged into the three splits above:

    data/ocr_two_row_real/   78 images   labels.txt   77 lines
                             46 trainable, 31 held out, split by plate string

They are deliberately outside `data/ocr/` because 14 of their 69 registrations
already sit in `val` or `test`, and because keeping `train/`, `val/` and `test/`
byte-identical is what lets a run from before this drop and a run after it be
compared. See "Fine-tune 2c" below.

41683 images, of which **96.0% synthetic and 4.0% real** — not the 80/20 this
section used to claim. The 1683 real crops carry **974 distinct plate strings**.

Counted 2026-09-03 *after* `scratch/tworow/apply_corrections.py` applied
`data/raw/plates/label_corrections.csv` in place: five audited crops were
dropped from the three real splits (1688 → 1683), which is why every number
here is one below the version this section used to carry. The split itself was
not rebuilt — same crops in the same splits — so a model trained before the
corrections and one trained after are still scored on one axis.

**Synthetic**, generated on CPU by `scripts/gen_synthetic_plates.py`.
Render plate strings in a plate-style font on the correct ground colour, then
apply: perspective warp, motion blur, gaussian noise, brightness and contrast
variation, JPEG compression artifacts, and dirt/occlusion overlays. Vary
difficulty deliberately — a dataset of clean renders teaches nothing useful.

**Generating 40k takes 1 minute 44 seconds, not "about an hour."** Measured
2026-09-03 over the regeneration that produced the current set: 02:46:55 to
02:48:39 wall clock, single-process on the CPU, ~385 crops/second. The old
estimate was never timed and was wrong by a factor of 35. Regenerating the
synthetic set is therefore a cheap experiment, not a commitment — which is what
made the six-layout run below worth doing at all.

Generate the strings from the real format grammar: valid state codes, correct
district and series structure, plus a BH-series minority. The model should never
see a string that couldn't exist.

**Real**, from public Indian plate datasets, processed by
`scripts/prep_ocr_dataset.py` into the same layout. Three origins, stratified
across all three splits:

| origin | train | val | test |
|---|---|---|---|
| `State-wise_OLX` | 421 | 90 | 90 |
| `google_images` | 303 | 65 | 65 |
| `video_images` (frames from 10 public clips) | 458 | 98 | 98 |

**The test split is NOT this project's own footage.** It is 253 crops from the
same three public origins, and nothing in `footage/` reaches `data/ocr/` at all.
The 62.5% in `config/settings.yaml` is therefore accuracy on held-out *public*
plates. Read it as such — the own-footage number is the four hand-labelled
plates in `data/video_truth.yaml`, scored by `scratch/bench_realvideo.py`.

The split itself is sound: it is made over **plate strings**, so all crops of one
plate land in one split. Verified 2026-09-03 — **0 plate strings appear in more
than one split** and no crop filename is reused. The one residual overlap is
scene-level, not label-level: 9 of the 10 source videos contribute *different*
vehicles to more than one split, so a camera and its lighting are seen in both
train and test. That inflates nothing about a specific plate, but it does make
test slightly easier than genuinely unseen footage.

Three labels in the real splits are not plates at all — `CRETA` (train),
`DUSTER` (val) and `TERRANO` (test) are model badges the labeller transcribed.
One label is too long for the recogniser and is dropped at load:
`KA42TC131011`, 12 characters against `MAX_SLOTS = 11`.

## Training

`scripts/train_ocr.py` — 40 epochs, batch 128, lr 3e-3, OneCycle, AdamW, AMP,
seed 1337, 10 DataLoader workers, selection on real validation plate accuracy.

**Measured on this machine's RTX 3050, 2026-09-03: 23 minutes for the full 40
epochs** — 34s per epoch steady state, 51s for the first while the ten spawn
workers start. Both OCR runs on record land within a few seconds of that. The
"several hours on a GTX 1650, prefer Kaggle" estimate above was never timed and
is off by an order of magnitude; the laptop trains this model over a coffee and
Kaggle is not needed. The `.contiguous()` note in `PlateNet.forward` is part of
why — feeding cuDNN the permuted NHWC view instead costs 14x on the stem.

## What the real-video evidence says this fine-tune must fix — 2026-09-02

Two findings from the inference pass point straight at this training job, and
both should shape the next run rather than being rediscovered later.

**1. The model cannot read a partial plate, and that is now measured.**
Handed one row of a two-row plate the recogniser invents characters to fill its
fixed slot count: across 30 combinations of cut point, trim and upscale on a
known two-row crop (`scratch/inf/tworow_probe.py`), **none** produced the plate.
The pipeline works around this by rearranging the two rows side by side into one
line, which is a workaround and not a fix.

**"Put real two-row plates in the training set" was the old instruction here and
it was wrong about the starting point — the set already has some.** Audited
2026-09-03 by rendering every crop with a median box aspect under 3.45 and
classifying it by eye (324 of the 979 plate strings; a genuine two-row plate
cannot be wider than about 2.5:1, so nothing above that band was reachable):

| | plate strings | crops |
|---|---|---|
| train | 12 | 23 |
| val | 11 | 21 |
| test | 13 | 19 |
| **total** | **36** | **63** |

All 63 are real photographs — 39 from `video_images`, 24 from `google_images`,
none synthetic — and 61 of the 63 are at or above the app's 48px
`min_plate_width`. Ground truth is stored the same way as everything else: the
two rows **concatenated top-to-bottom into one string**, no separator, which is
exactly the target a rearranged read produces. Of the 36, one label is wrong —
`KA5122727` (train `000317.jpg`) is a plate reading `KA 51 Z` over `2727`, so
the `Z` was transcribed as a `2`; `grammar.is_valid` rejects it and the correct
string is `KA51Z2727`. Two more, `DL758Y1790` (34×23) and `KL57A111` (35×18),
are too small to verify by eye either way.

`scripts/gen_synthetic_plates.py` **already renders two-row plates** — 18% of
output, measured at 17.7% over a 1500-image sample, so roughly **7080** of the
40000 synthetic crops. `scripts/train_ocr.py` feeds all of it, real and
synthetic, straight in: it resizes every crop to 128×64 with no aspect
preservation and no layout branch, so a stacked two-row crop is a training
sample like any other. **The two-row data is already in the pipeline.**

**And it is not working.** Scoring the adopted `models/finetuned/ocr_best.pt`
on the two-row crops separately:

| | n | exact | char |
|---|---|---|---|
| test, single-row | 234 | **0.671** | 0.913 |
| test, two-row | 19 | **0.053** | 0.535 |
| val, single-row | 232 | **0.707** | 0.929 |
| val, two-row | 21 | **0.190** | 0.752 |

The cause is layout, not volume. `render()` splits a two-row plate at a **fixed
4 characters** for any string of 9 or more, which is 98% of the synthetic set —
so of the seven layouts the real plates actually use, the model has practice at
exactly one. Split by where the real plate breaks, over val and test together:

| top row | n | exact |
|---|---|---|
| 4 chars — the only split synth renders | 28 | **0.179** |
| 5 chars (`MH47A`/`V6753`) | 3 | 0.000 |
| 6 chars (`MH12FT`/`9458`) | 4 | 0.000 |
| 7 chars (`WB07D51`/`06`) | 1 | 0.000 |
| left-stacked column (`PY`/`01` then `BL1155`) | 4 | 0.000 |

**7080 synthetic two-row images bought accuracy on one layout and zero on the
other four.** The next move was therefore free before it was expensive: vary the
split point in `render()` across 4–7 and add the left-stacked column form, then
re-measure. **That was done on 2026-09-03 and it did not work** — the result is
the section below. Only if that stalled was more real two-row data the answer,
and it stalled, so that is now the answer: 12 distinct two-row plates in train
against 605 single-row is the actual sample size, and the test split's 13 make
any two-row number move 7.7 points per plate.

## Result — retrained 2026-09-03 with six two-row layouts, and NOT adopted

`gen_synthetic_plates.py` regenerated at seed 1337 with `TWO_ROW_LAYOUTS`
carrying all six observed forms instead of the single fixed split at 4. The
two-row *fraction* is unchanged at 18%; only the mix inside it changed, and
deliberately not to the observed frequency — split4 is the layout the adopted
model can already do, so it was cut to 30% and the room given to the five it
cannot:

    40000 crops in 1m44s   7108 two-row = 17.8%   (data/ocr/synth_layouts.json)
    split4 2127 29.9%   split5 1579 22.2%   split6 1448 20.4%
    split7  700  9.8%   stacked_district 688 9.7%   stacked_state 566 8.0%

Then `python -u scripts/train_ocr.py --out models/finetuned/ocr_layouts.pt`,
every default unchanged: 40 epochs, batch 128, lr 3e-3, seed 1337, workers 10,
real ×8, 49432 samples/epoch, 3.43M parameters, CUDA 0. **23 minutes**, best
epoch 37 of 40 selected on real validation plate accuracy. Run log
`runs/ocr_20260903_030830`, training output `scratch/tworow/train.log`.

### Held-out splits, both models, one scorer

`scripts/eval_ocr.py --by-layout`, `--weights` pointing at each model in turn so
neither reads a different config (`scratch/tworow/eval_baseline.log`,
`scratch/tworow/eval_layouts.log`):

| | `ocr_best.pt` | `ocr_layouts.pt` |
|---|---|---|
| test exact, n=252 | 62.7% | **64.3%** |
| test chars | 88.7% | **91.2%** |
| test single-row exact, n=233 | 67.4% | **69.1%** |
| **test two-row exact, n=19** | **5.3%** | **5.3%** |
| test two-row chars | 53.5% | **62.7%** |
| val exact, n=252 | 66.7% | **69.8%** |
| val chars | 91.6% | **92.3%** |
| val single-row exact, n=231 | 71.0% | **74.9%** |
| **val two-row exact, n=21** | **19.0%** | **14.3%** |
| val two-row chars | 75.2% | 73.8% |

Per layout — one plate is 5.3 points on test and 4.8 on val, so read these as
counts and not as percentages:

| layout | test n | before | after | val n | before | after |
|---|---|---|---|---|---|---|
| split4 | 10 | 1 (10.0%) | **0 (0.0%)** | 18 | 4 (22.2%) | **3 (16.7%)** |
| split5 | 3 | 0 | **1 (33.3%)** | 0 | — | — |
| split6 | 2 | 0 | 0 | 2 | 0 | 0 |
| split7 | 1 | 0 | 0 | 0 | — | — |
| stacked | 3 | 0 | 0 | 1 | 0 | 0 |

**The three layouts the regeneration existed to teach — split6, split7,
stacked — are still 0 of 9 across val and test.** Test two-row is 1 of 19 before
and after, with the one correct plate moving from split4 to split5. Val lost a
plate. The only two-row number that improved is character accuracy on test,
53.5% → 62.7%: the model gets closer to two-row plates without getting one more
of them right.

### Real video

`scratch/bench_realvideo.py --tag inf_ocr2`, only `models.ocr` swapped and
restored afterwards, scored by `scratch/tworow/score_labels.py`:

| | `ocr_best.pt` | `ocr_layouts.pt` |
|---|---|---|
| sightings / plate crops / reads | 143 / 12 / 12 | 143 / 12 / 12 |
| false positives on `23sec.mp4` | 0 | 0 |
| exact OCR vs hand labels | 1/4 | 1/4 |
| mean similarity to hand labels | 0.750 | 0.750 |

Per label: `MH15JS4241` 1.00 → 1.00, `MH15HY2237` 0.90 → 0.90 (`MH15HY2277`
becomes `MH15BY2237` — a different wrong character, same distance),
`MH17CY4718` **0.70 → 0.80**, `MP09ZS0907` 0.40 → 0.30. `MH17CY4718` is the
two-row plate and is the one real-video sign the layouts did anything;
`MP09ZS0907` is never detected at all, so both of its numbers score a different
vehicle's row and neither means anything about OCR. They cancel exactly.

### Decision

**Not adopted. `models.ocr` stays `models/finetuned/ocr_best.pt`,
`config/settings.yaml` restored byte-identical, production weights untouched.**
The adoption gate was a material improvement in two-row OCR and there is none.

`ocr_layouts.pt` is kept beside it, wired to nothing, because the model is
genuinely better at everything else — +1.6 test exact, +3.1 val exact, +2.5 test
chars, +1.7 single-row — for zero cost on real video. Adopting it on *that*
basis is a separate decision against a separate criterion and was not taken here.

### What this rules out, and what is left

Layout coverage is no longer a candidate explanation: it was supplied, in all
six forms, at 4.7x the previous per-layout volume for the hard ones, and bought
nothing on them. What is left is sample size at both ends — **12 distinct
two-row plates in train against 605 single-row**, and 9 crops total across
split6/split7/stacked in val and test combined. A test statistic built on 19
crops cannot resolve a gain smaller than one plate, and 12 training plates
cannot reliably produce one. More real two-row crops is the next move, and it is
now a measured requirement rather than a preference.

**2. Exact accuracy on real video is now limited by this model and nothing
else.** `scratch/inf/ocr_ceiling.py` separates resolution, cropping and model
error over the four hand-labelled plates. Every one is above the ~8px per
character floor at which a glyph stops existing in the pixels; `MH15HY2237` is
sharp (Laplacian variance 4951) and well resolved at 13.2px per character and
is *still* read `MH15HY2277`. Plate detection, crop padding, sampling, best-view
selection and multi-frame voting were each swept during the inference pass and
none of them moves exact OCR off 1/4. The next real gain is here.

## Fine-tune 2c — 77 real two-row crops, trained 2026-09-03, NOT adopted

The move the section above called the next one: real two-row crops instead of
more rendered layouts. Supplied as `data/ocr_two_row_real/`.

### Data, as supplied and as used

```
data/ocr_two_row_real/
  <78 image files>          .jpg and .png, native size
  labels.txt                filename \t PLATESTRING, 77 lines, never modified
  split.csv                 written by scratch/tworow2/prep_two_row_real.py
  train_labels.txt          46 crops, the trainable side
  heldout_labels.txt        31 crops, plate-disjoint from everything trained on
```

Audited by `scratch/tworow2/audit_two_row_real.py` before anything trained on
them. 78 images against 77 labels, 0 byte-identical duplicates, 0 near-duplicates
at phash <= 6, 69 distinct plate strings, 0 crops under `min_plate_width`, and 0
image-level overlap with the 1683 crops already in `data/ocr`. Widths run 72 to
1366 px, median 205; characters get a median 24.3 px each and only one crop falls
under the ~8 px/char floor.

Four findings changed how they are used:

1. **14 of the 69 plate strings are already in `data/ocr/val` or
   `data/ocr/test`.** Those crops can never be trained on.
2. **27 of the 77 are Azerbaijani**, not Indian — `10-EX-500` against
   `[2 letters][1-2 digits][1-3 letters][4 digits]`. Kept, with origin as a
   column, and one experiment pair drops them to find out whether they help.
3. **Five labels disagree with their own image**, and one image has no label.
   Both are handled in `scratch/tworow2/corrections.csv`, which records the
   correction and the reason; `labels.txt` is left exactly as supplied.
4. **Five crops carry annotation-tool chrome burned into the pixels.**

### Splitting — by plate string, against the existing splits

`scratch/tworow2/prep_two_row_real.py`. A supplied plate already in `data/ocr`'s
val or test goes to heldout; one already in train goes to train; a brand-new one
is assigned by a hash of its own string so all its crops land together and the
assignment is stable across runs. `data/ocr/train`, `val` and `test` are left
byte-identical, so every number from earlier runs stays comparable.

| | crops | plates | Indian | Azerbaijani | nonstandard |
|---|---|---|---|---|---|
| train | 46 | 41 | 25 | 19 | 2 |
| heldout | 31 | 28 | 20 | 8 | 3 |

Verified: 0 plate strings shared between the two sides, and 0 train plates
present in `data/ocr/val` or `data/ocr/test`.

### Training method

`scripts/train_ocr.py` gained seven flags, all defaulting to off so the
no-argument invocation still reproduces `ocr_best.pt`:

    --two-row-repeat N           oversample the 46 supplied train crops
    --two-row-origins ...        indian / azerbaijan / nonstandard
    --existing-two-row-repeat N  extra repeats of the 22 two-row crops already
                                 in data/ocr/train, on top of --real-repeat
    --two-row-stitch P           present the crop with its rows laid side by
                                 side, matching app/ocr.py:stitch_rows
    --two-row-jitter P           re-lay the two rows: new split point, sideways
                                 offset, top/bottom height ratio
    --init W                     warm start from an existing checkpoint
    --select val|blend           blend = half val exact, half val two-row exact

Emphasis is by repetition, not a weighted sampler: with 46 crops against 49432
the sampler needs a weight anyway, and a repeat count is the same statement in a
number that appears in the run log. At `--two-row-repeat 24` with
`--existing-two-row-repeat 16` the epoch is 50888 samples, 1632 of them (3.2%,
and 17% of the real portion) real two-row.

`--two-row-stitch` is not generic augmentation. With `two_row_split: true` the
app reads every two-row plate twice, once stacked and once rearranged, and takes
the rearranged read when it is the one that parses — so the rearranged image is
a real production input, and a training set that never contains it is missing
half of deployment. `stitch_rows` is copied into `train_ocr.py` rather than
imported from `app/ocr.py`, per the one rule at the top of this file, with its
three constants restated in full.

`--two-row-jitter` is the answer to 46 crops not covering enough layout variety,
and it differs from the six-layout synthetic regeneration in the way that
matters: the glyphs stay real and only the arrangement is resampled.

Warm-start runs are `--lr 3e-4` for 12–16 epochs from `ocr_layouts.pt`;
from-scratch runs are the standard 40 epochs at 3e-3. Two two-row scores print
every epoch — `val2r` over the 21 two-row crops in `data/ocr/val`, and `held2r`
over the supplied 31, which nothing ever selects on.

### The architecture was cleared first

`PlateNet.forward` collapses height with `x.mean(dim=2)`, which on a stacked
plate superimposes the rows. `scratch/tworow2/height_probe.py` tests whether
that destroys vertical order by reading every two-row crop twice, once with its
horizontal halves swapped: mean similarity between the two reads is **0.229** on
`data/ocr/test`'s two-row crops and **0.176** on the heldout, against **0.772**
on a single-row control, with **0 identical reads out of 50**. The model resolves
vertical order. The architecture is not the obstruction and was left alone.

### Result

Six candidates, all in `models/finetuned/`, none wired into
`config/settings.yaml`. `scratch/tworow2/compare.py` scores them all on one axis;
the from-scratch control is `ocr_layouts.pt`, because `data/ocr/synth` on disk is
the six-layout regeneration.

| | test exact | test single | test two-row | val two-row | heldout Indian chars | heldout exact |
|---|---|---|---|---|---|---|
| `ocr_best.pt` production | 62.7% | 67.4% | 5.3% | **19.0%** | 30.3% | 0/31 |
| `ocr_layouts.pt` control | 64.3% | 69.1% | 5.3% | 14.3% | 40.5% | 0/31 |
| E1 `ocr_tworow_mix.pt` | 59.1% | 63.5% | 5.3% | 4.8% | 41.5% | 1/31 |
| E2 `ocr_tworow_warm.pt` | 64.7% | **70.0%** | 0.0% | 14.3% | 41.0% | 0/31 |
| E3 `ocr_tworow_warm_in.pt` | **65.1%** | **70.0%** | 5.3% | 9.5% | 42.6% | 0/31 |
| E4 `ocr_tworow_stitch.pt` | 64.7% | 69.5% | 5.3% | 9.5% | 40.5% | 0/31 |
| E5 `ocr_tworow_jit.pt` | 64.3% | 69.1% | 5.3% | 9.5% | 41.0% | 0/31 |
| E5b `ocr_tworow_jit_last.pt` | 63.5% | 68.2% | 5.3% | **0.0%** | **43.6%** | **2/31** |
| E6 `ocr_tworow_jit_in.pt` | 63.5% | 68.2% | 5.3% | 4.8% | 37.9% | 0/31 |

E1 is from scratch with all origins at x24; E2 warm-starts the same mix; E3 is
E2 with the Azerbaijani crops dropped and the Indian ones repeated x44 to hold
the sample count level; E4 adds `--two-row-stitch 0.5`; E5 adds
`--two-row-jitter 0.7` on top; E5b is E5's final epoch rather than its selected
one; E6 is E5 Indian-only.

**Character accuracy moves, exact accuracy does not.** Pooled over all 71
held-out two-row crops now available (19 test + 21 val + 31 supplied), the
production model reads 5, the control 4, and every candidate 2 to 3. E5b's two
heldout hits are one registration photographed twice. No candidate reads more
than one distinct new two-row plate correctly.

Real video, `scratch/tworow2/bench_ocr.py` over the five clips with only
`models.ocr` swapped: sightings 143, plate crops 12, reads 12, false positives on
`23sec.mp4` 0, exact OCR 1/4 — all identical for every model. Mean similarity to
the hand labels is 0.750 for the production model, 0.750 for E3 and **0.725** for
E5b, the candidate that did best on the heldout crops.

### Decision

**Not adopted. `models.ocr` stays `models/finetuned/ocr_best.pt`,
`config/settings.yaml` restored byte-identical, production weights untouched.**
The gate was a material improvement in real two-row recognition and there is
none. All six candidates are kept.

As with `ocr_layouts.pt`: **`ocr_tworow_warm_in.pt` is a better model overall** —
test exact 65.1% against 62.7%, single-row 70.0% against 67.4%, characters 91.5%
against 88.7%, real video exactly level. That is a decision on a different
criterion and was not taken here.

### What this rules out, and what is left

Two candidate explanations are now closed. Layout coverage was closed by the
six-layout regeneration. **The architecture is closed by the row-swap probe.**
Real two-row training data was the third, and 41 registrations of it moved
character accuracy ~3 points beyond what the control already had and exact
accuracy not at all.

What is left is the same thing, larger. 41 two-row registrations in train and 71
held-out two-row crops in total is still a regime where the spread across nine
models is three plates. The measurement improved — the held-out two-row pool went
from 40 crops to 71 — and the model did not. Any next attempt needs real two-row
crops in the hundreds, and Indian ones: the Azerbaijani third of this drop is
measurably neither the problem nor the fix, since Indian-only training (E3, E6)
scores no better on the Indian heldout crops than the mixed runs do.

## Inference-only: rearranging the two rows — measured 2026-09-03, NOT adopted

No training, no weights touched. `models/finetuned/ocr_best.pt` reads the same
77 curated crops several ways, and the question is whether laying the two rows
side by side before OCR makes them readable. `scratch/tworow3/`, full result in
CLAUDE.md; the parts that belong to the training track are here.

**It does not.** Exact accuracy over the 77 goes 7.8% reading the crop as it is
to **2.6%** rearranged — 6 plates to 2. Rearranging is a net loss.

Two findings from it bear on the next fine-tune:

- **The model's input is 128x64 with `keep_aspect_ratio: False`.** That is 2:1,
  and a stacked two-row plate is about 2:1, so the crop this fine-tune finds
  hardest is the one that already fits its input geometry natively. A
  side-by-side strip is nearer 6:1 and is squashed to reach 128x64. The
  rearrangement is fighting the input layer, which the `--two-row-stitch`
  flag's flat result (E4) had already hinted at without naming the reason.
- **Localising the plate inside the crop is worth far more than any of this.**
  Cutting each crop to the region its character strokes occupy, changing
  nothing else, moves character accuracy 28.5% -> **41.0%** and mean similarity
  0.352 -> **0.500** on the same 77 crops and the same weights. That is the
  largest single move any two-row intervention in this file has produced, and
  it is a preprocessing change rather than a training one.

The second is a lead for `data/ocr` itself: if a tighter crop reads that much
better at inference, the two-row crops in train may be carrying background that
a tighter harvest would remove. Not acted on here.

## Fine-tune 2d — 500 real two-row crops, trained 2026-09-04, **ADOPTED**

The first OCR promotion this project has made, and the first time any of the
two-row work has moved a number that mattered. Full result in CLAUDE.md, "OCR
fine-tune 3"; what belongs to the training track is here.

This is the experiment "Fine-tune 2c" named as the only one left: *"Any next
attempt needs real two-row crops in the hundreds, and Indian ones."* The
supplied drop is 502 crops, of which **484 are usable over 460 registrations,
319 of the 339 training crops Indian** — against the 41 registrations that pass
had.

### The labels could not be used, and that is the first result

`data/ocr/two_row_plate_labels` supplies one plate string per image. All 500
paired crops were transcribed by eye from `scratch/tworow4/transcribe_sheets.py`'s
numbered contact sheets, **without the supplied labels in view**, and the two
compared afterwards (`scratch/tworow4/compare.py`):

| | crops | supplied label exact |
|---|---|---|
| **photographed** | 363 | **41 = 11.3%** |
| rendered | 121 | 89 = 73.6% |
| **all usable** | **484** | **130 = 26.9%** |

`MH17D P1417` is labelled `FMX`; `MH65E R4547` is labelled `FHEH`; `MH03A
Q4270` is labelled `8423`; `PB10G N4497` is labelled `PBTOLR4L92PBTOGNLL9Z`.
With `MH`→`MHT`, `1`→`T` and `0`→`O` throughout and 73.6% on the *rendered*
crops, these are a recogniser's output rather than a transcription. **Training
on them would have taught this model another OCR model's confusions**, which is
the specific failure this project cannot afford, and it is why the transcription
exists.

`data/ocr/two_row_plate_labels` is never modified. The transcription is
`scratch/tworow4/transcriptions.tsv` and the supplied string is carried into
`data/ocr/two_row_split.csv` as `supplied_label`, so the disagreement stays in
the dataset rather than being resolved out of sight.

**16 crops are excluded**, each with its reason in the same file: cut
mid-string, an `IND` sticker over a character, Devanagari numerals, rust,
a novelty panel, two registrations on one plate.

**121 of the 484 are flat vector renders, not photographs**, tagged `source` so
they can be separated. `--two-row-sources photo` restricts training to the 248
photographed train crops; that ablation was **not** run, so how much of the gain
the renders carry is unmeasured.

### Splitting — by plate string, and verified

`scripts/prep_two_row_real.py` → `data/ocr/two_row_split.csv`. Same rule as
Fine-tune 2c: a plate already in `data/ocr/val` or `test` can never reach train,
one already in `data/ocr/train` goes to train, the rest are assigned by a hash
of the plate string so every crop of a registration lands together.

| | crops | plates | indian | azeri | other | photo | rendered |
|---|---|---|---|---|---|---|---|
| train | 339 | 321 | 319 | 18 | 2 | 248 | 91 |
| heldout | 145 | 139 | 134 | 9 | 2 | 115 | 30 |

Checked rather than asserted: **0 plates on both sides**, 0 new-train plates in
`data/ocr/val` or `test`, and `data/ocr/train`, `val`, `test` byte-identical.

### Training

    python scripts/train_ocr.py --init models/finetuned/ocr_best.pt \
        --out models/finetuned/ocr_tworow500.pt \
        --two-row-repeat 12 --existing-two-row-repeat 8 \
        --epochs 30 --lr 1e-3 --select val

A warm-started fine-tune of the shipped weights, not a new model. Architecture,
128x64 input, preprocessing, alphabet and 11 slots all unchanged, so it is a
drop-in and `app/ocr.py` is untouched. Epoch = **53676 samples: 40000 synthetic
+ 1179 real x8 + 339 real two-row x12 + 22 existing two-row x8**, so the new
data is 7.6% of the epoch and ~30% of the real portion — a controlled mixture,
not a two-row-only fine-tune. Selection is real val plate accuracy, as
`ocr_best.pt` was selected; the heldout 145 are never selected on. Best epoch
14, run log `runs/ocr_20260904_030134`.

`scripts/train_ocr.py` gained one flag, `--two-row-sources`, and its two-row
paths were repointed from the deleted `data/ocr_two_row_real/` to
`data/ocr/two_row_split.csv`. Every flag still defaults to off, so
`python scripts/train_ocr.py` with no arguments still builds the `ocr_best.pt`
run.

### Result

`scratch/tworow4/score.py`, both models over identical crops; the `ocr_best`
column reproduces this file's existing numbers exactly.

| | `ocr_best` | `ocr_tworow500` |
|---|---|---|
| test exact, n=252 | 62.7% | 62.3% |
| test single-row exact, n=233 | 67.4% | 66.1% |
| test two-row exact, n=19 | 5.3% | **15.8%** |
| val single-row exact, n=231 | 71.0% | **73.2%** |
| val two-row exact, n=21 | 19.0% | **47.6%** |
| **heldout two-row exact, n=145** | **0.7%** | **30.3%** |
| heldout two-row characters | 36.9% | **75.2%** |
| **pooled held-out single-row, n=464** | 69.2% | **69.6%** |
| **pooled held-out two-row, n=185** | **3.2%** | **30.8%** |

Test single-row falls 3 plates and that is **churn, not forgetting**: it loses
12 and gains 9, val single-row loses 5 and gains 10, and every one of the 21
moves is a single-character confusion changing direction (`DL12C3536` →
`ML12C3536` one way, `AS23K5585` → the correct `AS23X5585` the other). Pooled
over both held-out single-row sets the candidate is **two plates ahead**.

Real video, eight clips, only `models.ocr` swapped
(`scratch/tworow4/bench_ocr.py`): sightings 412 → 412, reads 14 → 14, false
reads on the plate-less `23sec.mp4` 0 → 0, **exact 1/4 → 2/4**, mean similarity
**0.750 → 0.816**, and `MH17CY4718` — the two-row plate every earlier candidate
missed — read **exactly**.

### The cost, stated

Regression 229/231 rather than 230/231. The new failure is `p2_verify`, and it
is **not** an accuracy regression: on that clip the genuine plate is read
identically and with more votes (5 against 3), but a truck with no legible plate
that both models invent a string for goes from confidence 0.31 to **0.72**, so
it becomes the highest-confidence row that `p2_verify` inspects. **The promoted
model is more confident on unreadable crops.** The check was left failing rather
than adjusted.

### Decision

**Adopted. `models.ocr` is `models/finetuned/ocr_tworow500.pt`.**
`ocr_best.pt` is untouched on disk and copied to `ocr_best_rollback.pt`;
rolling back is one line in `config/settings.yaml`.

### What is left

- **101 of the 145 heldout two-row crops are still wrong.** Improved from 144,
  not solved.
- **Azerbaijani plates stay 0/9.** The slot heads learn the Indian shape; a
  `10-EX-500` layout is not it, and three passes have now shown this data
  cannot change that.
- **Confidence calibration on junk crops got worse**, measured at 0.31 → 0.72.
  Nothing in this pass was tuned against it.
- **The photo-only ablation was not run**, so the renders' contribution is
  unknown.
- **The transcription is one reader's, unreplicated.**

## Exit criterion

`scripts/eval_ocr.py` prints plate-level accuracy on `data/ocr/test/` for:

1. baseline pretrained OCR
2. fine-tuned OCR
3. fine-tuned OCR + grammar correction
4. all of the above + multi-frame voting, measured end to end on video

Each row is a real number from held-out data. If the fine-tune doesn't beat
baseline on your own footage, keep baseline — the config swap makes that a
one-line decision, not a rewrite.

---

# Fine-tune 3 — Plate detector

`scripts/train_plate_det.py`, on `data/plate_det`'s 1086 images — the only real
bounding boxes in this project. The 905 `License (N)` images are annotations *of
a crop* (median box 86% of the frame) and teach the detector that a plate fills
the frame; the 181 numbered scene images do not. Training on all 1086 scored
better mAP and got worse at the job, so it is rejected and kept only as
`plate_det_best.pt`. `plate_det_scenes.pt` is the 181-scene run and is adopted.

## Re-checked on real video, 2026-09-02

The earlier decision was made before the current plate shape and edge-density
filters existed, so it was re-run under them — the full
`scratch/bench_realvideo.py` twice, identical except for `models.plate_detector`:

| detector | rows | reads | reads on the 4 Indian clips | false positives on 23sec.mp4 | exact OCR | mean similarity to hand labels |
|---|---|---|---|---|---|---|
| `plate_det_scenes.pt` | 148 | 10 | **10** | **0** | 1/4 | **0.658** |
| `plate_detector.pt` (pretrained) | 148 | 19 | 9 | 10 | 1/4 | 0.649 |

`23sec.mp4` is a US parking-lot dashcam labelled with no plates in
`data/video_truth.yaml`, so every read it produces is a false positive by
construction — which is what makes it the useful clip here. The pretrained
model's apparently better yield is that clip: 10 of its 19 reads are plates that
do not exist. **The fine-tuned scene model holds the decision** and stays in
`config/settings.yaml`.

### Re-confirmed at `plate_conf` 0.12, on identical pixels

The A/B above runs the whole pipeline twice, so the vehicle boxes shift
underneath the comparison. It was redone on 859 vehicle crops frozen to disk
(`scratch/inf/harvest_crops.py`), which holds the crops fixed and varies only
the detector, and repeated at the lowered confidence floor:

| detector | conf | kept, Indian | kept, plate-less |
|---|---|---|---|
| `plate_det_scenes.pt` | 0.25 | 20 | **0** |
| `plate_det_scenes.pt` | **0.12** | **36** | **0** |
| `plate_detector.pt` | 0.25 | 17 | 18 |
| `plate_detector.pt` | 0.12 | 21 | 27 |

The gap widens rather than narrowing. The fine-tune nearly doubles its yield
at the lower floor while still producing **zero** false positives on the clip
with no plates, and the pretrained model is dominated at both operating
points.

---


## Fine-tune 3b -- `data/vehicle_plate_frames`, trained 2026-09-03, NOT adopted

A second plate dataset arrived: 1007 frames from 14 videos with YOLO plate
boxes. Unlike `data/plate_det` these are real traffic scenes with plates at
realistic scale, which is the shape `plate_det_scenes.pt` was chosen for, so it
is the first plate data in this project that could plausibly raise recall.

### The dataset had to be cleaned before it could be split

Full audit in CLAUDE.md. The four findings, and what each one costs:

| finding | evidence | action |
|---|---|---|
| one source is a watermark, not plates | 163 of 176 boxes in `Licence Plate Camera Illustration Video` are the burned-in "IP Camera" text; the repo's two detectors recover only 8 and 11 of them | source dropped |
| burned-in detection graphics | `ANPR India Detection Demo - SmartCow` draws a cyan box and the plate string **on the plate** | source dropped from training |
| benchmark leakage | `20260901_152704` is the same phone and minute as benchmark clip `20260901_152733.mp4` | source dropped |
| labels miss ~14% of visible plates | `plate_det_scenes.pt` and `plate_detector.pt` recover 69.2% / 75.7% of the labels and **agree on 268 boxes the labels lack**; 43 of a 48-box sample are real plates | cannot be cleaned — see below |

`scripts/prep_plate_frames.py` codes every exclusion with its reason, drops
corner boxes of the watermark kind wherever they appear, and never touches
`boxed/`. What survives is **792 frames over 11 sources**.

### Splitting -- source-disjoint, never random

The videos were sampled at 2 fps from 30 fps footage, so two neighbouring frames
hold the same vehicles at the same distance. A random split would put one
vehicle on both sides and the mAP would be a memorisation score. Whole sources
are held out instead:

| split | images | boxes | sources |
|---|---|---|---|
| train | 567 | 930 | Chandigarh dashcam, License Plate Detection Test, WhatsApp highway, 4 pexels clips |
| val | 59 | 82 | pexels-casey-whalen, pexels-george-morina-5222550 |
| test | 166 | 368 | **Automatic Number Plate Recognition (India)**, Traffic Control CCTV |

One Indian source is held out so the test set is not entirely European.

### Two candidates, because the label defect has two honest answers

Both continue from the **current production weights** rather than rebuilding
from the pretrained model, because the property worth protecting is
`plate_det_scenes.pt`'s zero false positives on the plate-less control clip.

    scripts/train_plate_frames.py --data data/plate_frames_split   --name pf_labels
    scripts/train_plate_frames.py --data data/plate_frames_split_c --name pf_complete

    base models/finetuned/plate_det_scenes.pt   60 epochs   imgsz 640
    batch 8   lr0 1e-3   seed 1337   degrees 5  translate 0.1  scale 0.4
    fliplr 0.5  mosaic 1.0

`--complete` adds the 268 ensemble-agreed boxes the labels lack, after a plate
aspect filter (163 of them survive it), **into train only** — putting invented
boxes into val or test would make the eval score agreement with the ensemble
that invented them. This is pseudo-labelling on top of pseudo-labelling, it is
built as a separate split rather than argued about, and neither split is called
ground truth anywhere.

### Result: better mAP, worse job

| candidate | test mAP50 | mAP50-95 | P | R |
|---|---|---|---|---|
| C `plate_det_frames.pt` | 0.751 | 0.517 | 0.889 | 0.712 |
| D `plate_det_frames_c.pt` | **0.763** | **0.534** | 0.862 | 0.696 |

And on the eight-clip real-video benchmark, `models.plate_detector` the only
thing swapped (`scratch/final/bench_plate.py`):

| | plate reads | reads on the Indian clips | **false reads on `23sec.mp4`** |
|---|---|---|---|
| A `plate_det_scenes` (production) | 13 | 12 | **0** |
| B `plate_detector` (pretrained) | 24 | 11 | 12 |
| C `plate_det_frames` | 18 | **14** | **3** |
| D `plate_det_frames_c` | 11 | 7 | **4** |

**Both break the zero-false-positive property.** On a US car park with no Indian
plate in it, C invents `DL12CG4999`, `RJ83RY173` and `MN23C6711`. C is the only
candidate that reads more genuine Indian plates, and it pays for two of them
with three fabricated registrations.

D is worse on real video despite the better mAP, which is the clearest statement
in this file of why mAP does not decide anything here: the completed boxes taught
it to fire more often, and most of the extra firing is wrong.

### Decision, and what data would change it

**Not adopted. `models.plate_detector` stays `models/finetuned/plate_det_scenes.pt`.**
Both candidates are kept in `models/finetuned/`.

The blocker is the data and it is specific: **a plate set whose labels miss ~14%
of the visible plates cannot teach a detector recall**, because every missed
plate is a negative and the strongest gradient the model sees is "suppress the
small plate you just found". What is needed is **hand-corrected boxes on the
Indian sources** — completing `Chandigarh Road video` alone is 260 frames — not
more pseudo-labelled video. More video labelled the same way will reproduce this
result.

---

# Fine-tune 4 — Vehicle detector

Added 2026-09-02, when `data/vehicle_det` arrived. **Trained, measured, not
adopted.** `config/settings.yaml` still points at `models/yolo11n.pt`.

## Data, and why its provenance decides the method

216 frames, 640×482, with 1250 YOLO boxes over four classes — **0 car,
1 motorcycle, 2 bus, 3 truck**, COCO's own order. The class ids are written
down nowhere in the dataset; they were read off the burned-in names in
`data/vehicle_det/Boxed/` and verified on four frames. `Boxed/` is visual
review material only and nothing trains on it.

**The labels are pseudo-labels — YOLO11-L + RT-DETR-L, no human pass.** That is
not a disqualification but it changes what the training can mean, in three ways:

1. **The ceiling is the pseudo-labeller.** Trained against YOLO11-L's opinions,
   the best achievable is imitating YOLO11-L on this camera. mAP against these
   labels measures agreement with a bigger model, not correctness.
2. **Two models were merged without NMS across them.** 31 pairs of boxes overlap
   at IoU > 0.75 — one vehicle labelled twice, frequently `truck` by one model
   and `bus` by the other. Trained on as-is this teaches the detector to emit
   two boxes per vehicle, which is exactly the duplicate-sighting failure the
   app already fights. `prep_vehicle_det.py` drops the smaller of each pair.
3. **There is no `auto` class.** Autorickshaws are labelled `car`
   (`Boxed/frame_1417.jpg`: the yellow auto is `car 0.90`). This dataset can
   never supply what Fine-tune 1 exists for.

**All 216 frames are one fixed elevated CCTV camera**, banner timestamps
11:51–12:00 on 2015-12-18, sampled ~2.4s apart. One viewpoint, one time of day.

## Splitting — temporal, never random

`scripts/prep_vehicle_det.py` writes `data/vehicle_det_split` in the same shape
as `plate_det_split`, leaving `data/vehicle_det` untouched:

    train  172 images  993 boxes   car 444  motorcycle 476  bus 14  truck 59
    val     44 images  227 boxes   car 102  motorcycle 107  bus  1  truck 17
    boundary frame_1374        duplicate boxes dropped: 30

**A random split would leak.** Frames 2.4 seconds apart share most of their
vehicles, so the same car would sit in train and val and the val number would be
a memorisation score. The split is therefore the last 20% of frame numbers, and
no vehicle crosses the boundary.

**`bus` has one instance in val.** Every per-class bus number off this split is
noise, and is reported as noise rather than as a result.

## Training

`scripts/train_vehicle_det.py`, from `models/yolo11n.pt`, imgsz 640 to match
`defaults.imgsz`, batch 16, lr0 0.002, seed 1337, patience 25, 80 epochs.
Two candidates, kept separately and neither adopted:

    --freeze 10  models/finetuned/vehicle_det_road_frozen.pt   backbone kept
    --freeze 0   models/finetuned/vehicle_det_road_full.pt     all layers trained

`--freeze` is the whole experiment. Fitting 172 frames of one overhead camera
can overwrite COCO's features for the street-level viewpoint the app actually
runs on, and freezing the backbone is the conservative hedge against it.

## Result

Both candidates beat the COCO baseline comfortably on this dataset's val split
and the frozen one reaches mAP50 0.838 / class-agnostic AP50 0.992. **Neither
was adopted**, because the five-clip real-video benchmark says otherwise — the
full table is in CLAUDE.md under "Vehicle detector A/B". In one line: `road_full`
loses 61% of its raw detections on real footage (catastrophic forgetting,
predicted in Fine-tune 1 and now measured), and `road_frozen` buys two extra
plate reads by breaking the zero-false-positive property on the plate-less clip
and inflating duplicate rows.

**The baseline was never the bottleneck.** Class-agnostically, COCO `yolo11n`
already finds 95.6% of this dataset's labelled vehicles at IoU 0.5 with a mean
IoU of 0.902 — including 98.9% of boxes under 40px wide. Vehicle localization is
not where this project's accuracy is lost.

**What would earn adoption:** a second camera and a street-level viewpoint in
`data/vehicle_det`. One scene cannot teach generalisation, and no amount of
epochs on 172 correlated frames substitutes for it.


# Scripts

Each is standalone, runnable from an activated `env\Scripts\activate.bat` shell,
argparse-driven, and writes only into `data/`, `models/finetuned/`, or `runs/`.

| script | does |
|---|---|
| `fetch_models.py` | download pretrained weights into `models/` |
| `make_test_clip.py` | cut a 30s clip for fast iteration |
| `gen_synthetic_plates.py` | synthetic plate crops + labels |
| `prep_ocr_dataset.py` | merge synthetic + public into the layout above |
| `sort_vehicle_crops.py` | keyboard-driven sorter for `crops/unsorted/` (still a stub) |
| `train_vehicle_cls.py` | fine-tune 1; `--audit` counts the data and trains nothing |
| `train_ocr.py` | fine-tune 2 |
| `train_plate_det.py` | fine-tune 3, on plate boxes |
| `prep_two_row_real.py` | plate-disjoint split of `data/ocr/two_row_plate_images` into `data/ocr/two_row_split.csv`, using the hand transcription rather than the supplied labels |
| `prep_vehicle_det.py` | dedup + temporal split of `data/vehicle_det`'s pseudo-labels |
| `train_vehicle_det.py` | fine-tune 4, vehicle detector candidates |
| `prep_plate_frames.py` | source-disjoint split of `data/vehicle_plate_frames`; `--audit` reports and writes nothing, `--complete` adds the ensemble-agreed boxes |
| `train_plate_frames.py` | fine-tune 3b, plate detector on that split, continuing from the production weights |
| `eval_ocr.py` | the four-row accuracy table; `--by-layout` splits it single-row vs each two-row layout, `--weights` scores any `.pt` on that axis |

`scratch/final/` holds the 2026-09-03 inference-and-model pass, and nothing in
`app/` or `scripts/` imports from it:

| script | does |
|---|---|
| `mot_audit.py`, `mot_idmap.py`, `mot_consist.py` | integrity audit of `data/vehicle_mot_output`, and the proof that `physical_id` is a renumbering of `raw_track_id` |
| `plate_audit.py`, `plate_label_check.py`, `unmatched_sheet.py` | integrity audit of `data/vehicle_plate_frames`, its label recall against two independent detectors, and the contact sheet of what the labels miss |
| `cls_audit.py`, `cls_semantics.py`, `cls_score.py` | `data/vehicle_cls` integrity, the ImageNet cross-read that explains the truck collapse, and every classifier candidate on one axis |
| `adopt_replay.py` | replays `adopt()` over the MOT footage and audits it on physics |
| `adopt_analyse.py` | shows no feature available at decision time separates the wrong adoptions |
| `adopt_pollution.py` | end-state pollution: polluted rows and foreign frames per row, with and without the fix |
| `revoke_film.py` | draws every alias revocation back onto the frame it was decided on |
| `bench_plate.py` | eight-clip benchmark with one plate detector swapped in, `settings.yaml` restored on exit |
| `driver*.py` | the batch runners for the training, benchmark and regression passes |

`scratch/tworow2/` holds the real-two-row pass, and nothing in `app/` or
`scripts/` imports from it:

| script | does |
|---|---|
| `audit_two_row_real.py` | integrity audit of the supplied 77: counts, duplicates, label validity, overlap with `data/ocr`, dimensions |
| `corrections.csv` | the five relabels and one exclusion, each with its reason |
| `prep_two_row_real.py` | applies the corrections and writes the plate-disjoint `split.csv`, `train_labels.txt`, `heldout_labels.txt` |
| `height_probe.py` | row-swap test of whether PlateNet's height-mean destroys vertical order |
| `compare.py` | every candidate on one axis: test/val/heldout, single vs two-row, by plate origin |
| `bench_ocr.py` | five-clip benchmark with one checkpoint swapped in, `settings.yaml` restored on exit |

`scratch/tworow3/` holds the inference-only rearrangement pass, and nothing in
`app/` or `scripts/` imports from it either:

| script | does |
|---|---|
| `rowsplit.py` | three deterministic boundary finders — edge-profile valley, two-band midpoint, and the same after localising the plate — plus `ink_region` |
| `tworow_rearrange.py` | the five-arm experiment over the 77 crops; writes `results.csv`, `summary.json`, the example sheets |
| `cut_ablation.py` | thirteen single-read variants separating cut placement, trim and number of attempts |
| `cut_check.py` | draws the boundaries without running OCR, which is how the first two finders were caught landing on bodywork |

`scratch/tworow4/` holds the 2026-09-04 pass that produced the adopted OCR
model, and nothing in `app/` or `scripts/` imports from it:

| script | does |
|---|---|
| `audit.py` | integrity audit of the 502 supplied crops: pairing, duplicates, label validity, plate counts, geometry, overlap with `data/ocr` |
| `transcribe_sheets.py` | writes `index.csv` and 21 numbered contact sheets so all 500 crops can be read off the pixels |
| `transcriptions.tsv` | the hand transcription actually trained on, with a `keep`/`exclude` decision and a reason for each of the 16 exclusions |
| `compare.py` | scores the supplied labels against the transcription — 26.9% overall, 11.3% on the photographs |
| `score.py` | every checkpoint on one axis: test/val single-row and two-row, plus the 145-crop heldout, in exact / character / similarity |
| `bench_ocr.py` | eight-clip real-video benchmark with one checkpoint swapped in, `settings.yaml` restored on exit |

## Rules

- Never run pip. The environment is fixed.
- Never train while the app is running. One GPU.
- Set seeds and log every hyperparameter to `runs/`. An unreproducible result
  is not a result.
- Write checkpoints every epoch. A crash at epoch 24 of 30 must not cost the run.
- Report accuracy only from `data/ocr/test/`. Never from validation, never from
  a public dataset split.