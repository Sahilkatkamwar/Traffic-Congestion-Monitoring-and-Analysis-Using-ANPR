"""Fine-tune 2 -- plate OCR. Fixed-slot recogniser, PyTorch, CUDA.

    python scripts/train_ocr.py                       # synth + real, 40 epochs
    python scripts/train_ocr.py --epochs 5 --limit-synth 4000   # quick loop

Writes models/finetuned/ocr_best.pt, its plate config beside it, and a run log
under runs/ocr_<timestamp>/.

## Why this is PyTorch and not fast-plate-ocr's own trainer

fast-plate-ocr's training track is Keras and its export path is ONNX. Neither
`keras` nor `onnx` is installed in env\\, and CLAUDE.md forbids pip. So the
fine-tune is built here in PyTorch, which is installed and has working CUDA.

What it deliberately does NOT change is the *contract*. The model speaks the
same shape as the pretrained cct-s-v2-global-model:

    input   (N, 64, 128, 3) uint8 RGB, unnormalised -- the model scales
    output  (N, slots, 37) probabilities over '0123456789A-Z_'

so it slots into app/ocr.py's voting, candidate ranking and confidence code
without any of that code knowing which model produced the numbers. The weights
file is still one entry in config/settings.yaml, exactly as TRAINING.md
requires.

## Why synthetic data is in here

The real training split is 1182 crops over 617 distinct plates. A ten-head
classifier memorises that before it learns what an 8 looks like. Synthetic
crops carry the character variety; the real crops carry the camera. Validation
and test are real crops only, so nothing synthetic can flatter the result.

Nothing here imports from app/.
"""

import argparse
import json
import random
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset

ROOT = Path(__file__).resolve().parent.parent
OCR_DIR = ROOT / "data" / "ocr"
OUT_DIR = ROOT / "models" / "finetuned"
RUNS = ROOT / "runs"

# The pretrained model's contract, copied so a fine-tune is a drop-in. The one
# deliberate difference is the slot count: the pretrained model has ten, and
# [2 letters][2 digits][3 letters][4 digits] -- a shape app/grammar.py accepts
# and this dataset contains -- is eleven characters, which ten slots can never
# produce. Both numbers travel in the plate config beside the weights, so
# app/ocr.py reads whichever the loaded model has.
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_"
PAD_CHAR = "_"
MAX_SLOTS = 11
IMG_H, IMG_W = 64, 128

CHAR_TO_INDEX = {char: index for index, char in enumerate(ALPHABET)}
PAD_INDEX = CHAR_TO_INDEX[PAD_CHAR]


# ---------------------------------------------------------------------- data


def load_labels(path):
    """'file<TAB>PLATE' per line -> [(filename, plate)]."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t") if "\t" in line else line.split(None, 1)
        if len(parts) == 2:
            out.append((parts[0].strip(), parts[1].strip().upper()))
    return out


def encode(plate):
    """Plate string -> ten slot indices, right-padded. None if it will not fit.

    A plate longer than the slot count cannot be represented and must not be
    silently truncated into a different, wrong plate.
    """
    if len(plate) > MAX_SLOTS:
        return None
    indices = [CHAR_TO_INDEX.get(char, PAD_INDEX) for char in plate]
    indices += [PAD_INDEX] * (MAX_SLOTS - len(indices))
    return np.asarray(indices, dtype=np.int64)


def decode(indices):
    return "".join(ALPHABET[i] for i in indices).rstrip(PAD_CHAR)


_STITCH_CUTS = (0.44, 0.50, 0.56)
_STITCH_TRIM = 0.02
_STITCH_HEIGHT = 64


def stitch_rows(crop, cut, trim=_STITCH_TRIM, height=_STITCH_HEIGHT):
    """The two halves of a two-row plate laid side by side as one line.

    A deliberate copy of app/ocr.py:stitch_rows rather than an import of it.
    TRAINING.md's one rule is that the training track imports nothing from
    app/, and the alternative -- a shared module both tracks depend on -- would
    make a change to the running application able to silently change what a
    training run means. The cut points, trim and target height are the same
    three constants the app uses, and if they diverge the augmentation stops
    matching what production feeds the model, so they are stated here in full
    rather than referred to.
    """
    h, w = crop.shape[:2]
    ty, tx = int(h * trim), int(w * trim)
    inner = crop[ty:h - ty or h, tx:w - tx or w]
    ih, iw = inner.shape[:2]
    at = int(ih * cut)
    if at < 6 or ih - at < 6 or iw < 8:
        return None
    parts = []
    for part in (inner[:at], inner[at:]):
        ph, pw = part.shape[:2]
        if ph < 1 or pw < 1:
            return None
        parts.append(
            cv2.resize(part, (max(8, int(pw * height / ph)), height),
                       interpolation=cv2.INTER_CUBIC)
        )
    return cv2.hconcat(parts)


def jitter_rows(crop):
    """Re-lay a two-row crop's two rows: new split, shift, and height ratio.

    Forty-six real two-row crops is not enough layout variety, and the previous
    pass established that adding layout variety *synthetically* buys nothing --
    six rendered layouts moved two-row exact by zero. The difference here is
    that the glyphs stay real. Only the arrangement is resampled: where the two
    rows divide, how far each is offset sideways, and how tall the top row is
    against the bottom. That is the axis a real two-row plate actually varies
    on, and it is the one axis 46 crops cannot cover on their own.

    Returns None when the crop is too small to divide, which the caller treats
    as "use the image as it is".
    """
    h, w = crop.shape[:2]
    if h < 16 or w < 24:
        return None
    at = int(h * random.uniform(0.40, 0.60))
    if at < 6 or h - at < 6:
        return None
    parts = []
    for part in (crop[:at], crop[at:]):
        ph, pw = part.shape[:2]
        shift = np.float32([[1, 0, int(pw * random.uniform(-0.08, 0.08))], [0, 1, 0]])
        part = cv2.warpAffine(part, shift, (pw, ph), borderMode=cv2.BORDER_REPLICATE)
        height = max(6, int(ph * random.uniform(0.85, 1.20)))
        parts.append(cv2.resize(part, (pw, height), interpolation=cv2.INTER_LINEAR))
    width = max(p.shape[1] for p in parts)
    parts = [cv2.resize(p, (width, p.shape[0])) for p in parts]
    return np.ascontiguousarray(np.vstack(parts))


class PlateSet(Dataset):
    """Crops plus slot targets. Augmentation is train-only, by construction.

    Augmentation here is milder than gen_synthetic_plates.py's degradation: the
    synthetic images arrive already degraded, and stacking the two produces
    images with no plate left in them. What this adds is the variation a fixed
    render cannot have -- crop jitter, colour, and the resampling that happens
    when a 40px-wide crop is stretched to 128.

    `stitch_paths` and `stitch_prob` add one more, and it is not a generic
    augmentation: with two_row_split on, the app reads every two-row plate
    twice -- once as the stacked crop and once with its rows laid side by side
    -- and takes the rearranged read when it is the one that parses. So the
    rearranged image is a real input the deployed model receives, and a
    two-row crop that is never presented that way in training is a training
    set that does not contain half of production.
    """

    def __init__(self, items, augment=False, seed=0, stitch_paths=None,
                 stitch_prob=0.0, jitter_prob=0.0):
        self.items = items
        self.augment = augment
        self.seed = seed
        self.stitch_paths = stitch_paths or set()
        self.stitch_prob = stitch_prob
        self.jitter_prob = jitter_prob

    def __len__(self):
        return len(self.items)

    def __getitem__(self, index):
        path, plate, target = self.items[index]
        image = cv2.imread(str(path))
        if image is None:
            image = np.zeros((IMG_H, IMG_W, 3), np.uint8)
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        two_row = path in self.stitch_paths
        if two_row and self.jitter_prob and random.random() < self.jitter_prob:
            jittered = jitter_rows(image)
            if jittered is not None:
                image = jittered
        if two_row and self.stitch_prob and random.random() < self.stitch_prob:
            stitched = stitch_rows(image, random.choice(_STITCH_CUTS))
            if stitched is not None:
                image = stitched
        if self.augment:
            image = self._augment(image)
        image = cv2.resize(image, (IMG_W, IMG_H), interpolation=cv2.INTER_LINEAR)
        return torch.from_numpy(np.ascontiguousarray(image)), torch.from_numpy(target)

    def _augment(self, image):
        # The module-level RNG on purpose: DataLoader seeds each worker's `random`
        # and `numpy.random` deterministically from torch's base seed, so this is
        # reproducible across runs and still different in every worker and epoch.
        rng = random
        height, width = image.shape[:2]

        # Crop jitter. The plate detector's box is never exactly the plate, and
        # a model trained on perfect boxes falls apart on the real ones. This is
        # also the only teacher for the truncation the pretrained model shows:
        # both edges move, so no edge character is reliably present or absent.
        if rng.random() < 0.85:
            dx1 = int(width * rng.uniform(-0.06, 0.08))
            dx2 = int(width * rng.uniform(-0.06, 0.08))
            dy1 = int(height * rng.uniform(-0.12, 0.14))
            dy2 = int(height * rng.uniform(-0.12, 0.14))
            x1, y1 = max(0, dx1), max(0, dy1)
            x2, y2 = min(width, width - dx2), min(height, height - dy2)
            if x2 - x1 > 8 and y2 - y1 > 4:
                image = image[y1:y2, x1:x2]
            height, width = image.shape[:2]

        if rng.random() < 0.5:  # small rotation
            angle = rng.uniform(-5, 5)
            matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
            image = cv2.warpAffine(
                image, matrix, (width, height), borderMode=cv2.BORDER_REPLICATE
            )

        if rng.random() < 0.55:  # resolution loss, then back up
            scale = rng.uniform(0.3, 0.85)
            small = cv2.resize(
                image,
                (max(12, int(width * scale)), max(6, int(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
            image = cv2.resize(small, (width, height), interpolation=cv2.INTER_LINEAR)

        if rng.random() < 0.5:  # exposure and contrast
            image = cv2.convertScaleAbs(
                image, alpha=rng.uniform(0.6, 1.4), beta=rng.uniform(-40, 40)
            )

        if rng.random() < 0.3:  # colour cast: sodium light, dusk, a blue LED
            gains = np.array([rng.uniform(0.85, 1.15) for _ in range(3)], np.float32)
            image = np.clip(image * gains, 0, 255).astype(np.uint8)

        if rng.random() < 0.35:
            k = rng.choice([3, 5])
            image = cv2.GaussianBlur(image, (k, k), 0)

        if rng.random() < 0.35:
            sigma = rng.uniform(2, 14)
            image = np.clip(
                image.astype(np.float32) + np.random.normal(0, sigma, image.shape),
                0,
                255,
            ).astype(np.uint8)

        if rng.random() < 0.4:
            quality = rng.randint(25, 90)
            ok, buffer = cv2.imencode(
                ".jpg", image[:, :, ::-1], [int(cv2.IMWRITE_JPEG_QUALITY), quality]
            )
            if ok:
                image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)[:, :, ::-1]

        return np.ascontiguousarray(image)


def build_items(image_dir, labels_path, skipped):
    """[(path, plate, slots)] for every labelled crop that exists and fits."""
    items = []
    for name, plate in load_labels(labels_path):
        path = image_dir / name
        if not path.exists():
            skipped["missing file"] += 1
            continue
        target = encode(plate)
        if target is None:
            skipped[f"longer than {MAX_SLOTS} slots"] += 1
            continue
        items.append((path, plate, target))
    return items


def read_split_csv(path):
    """data/ocr_two_row_real/split.csv -> [dict], the plate-disjoint assignment.

    Written by scratch/tworow2/prep_two_row_real.py. The plate string in here
    is the corrected one, not necessarily the supplied one; which lines were
    corrected and why is in scratch/tworow2/corrections.csv.
    """
    import csv

    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def build_two_row_items(image_dir, split_csv, want_split, origins, skipped):
    """The supplied real two-row crops on one side of the split.

    `origins` filters by where the plate comes from -- `indian`, `azerbaijan`,
    `nonstandard`. Twenty-seven of the seventy-seven supplied crops are
    Azerbaijani, which are two-row plates but not the grammar this model's slot
    heads are learning, so whether they help is a question to measure rather
    than assume.
    """
    items = []
    for row in read_split_csv(split_csv):
        if row["split"] != want_split:
            continue
        if origins and row["origin"] not in origins:
            continue
        path = image_dir / row["file"]
        if not path.exists():
            skipped["two-row: missing file"] += 1
            continue
        target = encode(row["plate"])
        if target is None:
            skipped[f"two-row: longer than {MAX_SLOTS} slots"] += 1
            continue
        items.append((path, row["plate"], target))
    return items


def two_row_layout_names(path, want_split):
    """{filename} of the real crops in one data/ocr split that are two-row.

    From data/ocr/two_row_layouts.csv, the by-eye judgement eval_ocr.py already
    scores with. Used for two things: repeating the twenty-two two-row crops
    that are already in the train split alongside the new ones, and scoring the
    val two-row subset every epoch without importing the scorer.
    """
    import csv

    if not path.exists():
        return set()
    with path.open(encoding="utf-8", newline="") as handle:
        return {
            row["file"] for row in csv.DictReader(handle)
            if row["split"] == want_split and row["layout"] != "single"
        }


# --------------------------------------------------------------------- model


class ConvBlock(nn.Module):
    def __init__(self, channels_in, channels_out, pool):
        super().__init__()
        self.body = nn.Sequential(
            nn.Conv2d(channels_in, channels_out, 3, padding=1, bias=False),
            nn.BatchNorm2d(channels_out),
            nn.ReLU(inplace=True),
            nn.Conv2d(channels_out, channels_out, 3, padding=1, bias=False),
            nn.BatchNorm2d(channels_out),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(pool),
        )

    def forward(self, x):
        return self.body(x)


class PlateNet(nn.Module):
    """CNN over the image, GRU along its width, one classifier per slot.

    Height is collapsed and width is kept: a plate is read left to right, so
    the width axis is the one carrying the sequence. Sixteen columns feed a
    bidirectional GRU, and the ten slot heads read the whole sequence -- which
    is what lets slot 9 be decided by evidence from column 3, the thing a purely
    positional head cannot do when the plate is off-centre in its crop.

    Normalisation lives inside forward() on purpose. The pretrained model takes
    raw uint8 and does its own scaling, and matching that means the fine-tune
    can be swapped in without touching a line of app/ocr.py.
    """

    def __init__(self, width=1.0, dropout=0.3):
        super().__init__()
        c = [int(w * width) for w in (32, 64, 128, 256)]
        self.stem = nn.Sequential(
            ConvBlock(3, c[0], (2, 2)),      # 64x128 -> 32x64
            ConvBlock(c[0], c[1], (2, 2)),   # -> 16x32
            ConvBlock(c[1], c[2], (2, 2)),   # -> 8x16
            ConvBlock(c[2], c[3], (2, 1)),   # -> 4x16, width preserved
        )
        self.rnn = nn.GRU(
            c[3], 128, num_layers=2, bidirectional=True, batch_first=True, dropout=0.1
        )
        self.drop = nn.Dropout(dropout)
        self.head = nn.Linear(16 * 256, MAX_SLOTS * len(ALPHABET))

        self.register_buffer("mean", torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1))
        self.register_buffer("std", torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1))

    def forward(self, x):
        # (N, H, W, C) uint8 -> (N, C, H, W) normalised float, as the ONNX model
        # the app already talks to would have done internally.
        if x.dtype == torch.uint8:
            x = x.float()
        # .contiguous() is not tidiness. Permuting NHWC->NCHW leaves a tensor
        # whose suggested layout is channels_last, and cuDNN has no good NHWC
        # kernel for these shapes on this card: measured on the 3050, the same
        # stem takes 22ms fed NCHW and 307ms fed the permuted view. Forcing the
        # layout back is a 14x speedup and changes no number.
        x = x.permute(0, 3, 1, 2).contiguous() / 255.0
        x = (x - self.mean) / self.std
        x = self.stem(x)
        x = x.mean(dim=2)              # collapse height -> (N, C, W)
        x = x.permute(0, 2, 1)         # -> (N, W, C)
        x, _ = self.rnn(x)             # -> (N, 16, 256)
        x = self.drop(x.reshape(x.size(0), -1))
        return self.head(x).view(-1, MAX_SLOTS, len(ALPHABET))


# ------------------------------------------------------------------- scoring


def levenshtein(a, b):
    if a == b:
        return 0
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            )
        previous = current
    return previous[-1]


@torch.no_grad()
def evaluate(model, loader, device):
    """Exact-plate and character accuracy on a whole loader."""
    model.eval()
    exact = chars = total_chars = seen = 0
    for images, targets in loader:
        images = images.to(device, non_blocking=True)
        logits = model(images)
        predicted = logits.argmax(-1).cpu().numpy()
        for row, target in zip(predicted, targets.numpy()):
            got, want = decode(row), decode(target)
            exact += got == want
            chars += max(len(want) - levenshtein(got, want), 0)
            total_chars += len(want)
            seen += 1
    return {
        "n": seen,
        "plate": exact / seen if seen else 0.0,
        "chars": chars / total_chars if total_chars else 0.0,
    }


# ----------------------------------------------------------------------- run


def write_plate_config(path):
    """The YAML fast-plate-ocr's PlateConfig reads, beside the weights.

    app/ocr.py looks for '<stem>_plate_config.yaml' next to the weights, so the
    alphabet and slot count travel with the model rather than being duplicated
    in settings.yaml.
    """
    path.write_text(
        "# Written by scripts/train_ocr.py. Matches cct-s-v2-global-model's\n"
        "# contract so the fine-tune is a drop-in for the pretrained fallback.\n"
        f"max_plate_slots: {MAX_SLOTS}\n"
        f"alphabet: '{ALPHABET}'\n"
        f"pad_char: '{PAD_CHAR}'\n"
        f"img_height: {IMG_H}\n"
        f"img_width: {IMG_W}\n"
        "keep_aspect_ratio: false\n"
        "interpolation: linear\n"
        "image_color_mode: rgb\n",
        encoding="utf-8",
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--epochs", type=int, default=40)
    parser.add_argument("--batch", type=int, default=128)
    parser.add_argument("--lr", type=float, default=3e-3)
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--width", type=float, default=1.0)
    parser.add_argument("--real-repeat", type=int, default=8,
                        help="times the real train split is repeated per epoch")
    parser.add_argument("--limit-synth", type=int, help="use only the first N synthetic")
    parser.add_argument("--no-synth", action="store_true")
    parser.add_argument("--out", type=Path, default=OUT_DIR / "ocr_best.pt")
    # --- real two-row crops (data/ocr_two_row_real) ---------------------------
    # All default to off, so `python scripts/train_ocr.py` with no flags builds
    # exactly the run that produced ocr_best.pt.
    parser.add_argument("--two-row-repeat", type=int, default=0,
                        help="times the supplied real two-row train crops are "
                             "repeated per epoch. 0 leaves them out entirely")
    parser.add_argument("--two-row-origins", default="indian,azerbaijan,nonstandard",
                        help="which of the supplied crops to use, by plate origin")
    parser.add_argument("--existing-two-row-repeat", type=int, default=0,
                        help="extra repeats of the two-row crops ALREADY in "
                             "data/ocr/train, on top of --real-repeat")
    parser.add_argument("--init", type=Path,
                        help="warm-start from this checkpoint instead of random")
    parser.add_argument("--two-row-stitch", type=float, default=0.0,
                        help="probability a two-row training crop is presented "
                             "with its rows laid side by side, as app/ocr.py "
                             "presents them at inference")
    parser.add_argument("--two-row-jitter", type=float, default=0.0,
                        help="probability a two-row training crop has its two "
                             "rows re-laid: new split point, sideways offset "
                             "and top/bottom height ratio")
    parser.add_argument("--select", default="val", choices=["val", "blend"],
                        help="val: real val plate accuracy, as ocr_best.pt was "
                             "selected. blend: half val, half val two-row")
    args = parser.parse_args(argv)

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    random.seed(args.seed)

    if not torch.cuda.is_available():
        raise SystemExit(
            "CUDA is not available. CLAUDE.md says to assume it is, so this is a "
            "real problem with the environment rather than something to work around."
        )
    device = torch.device("cuda:0")
    print(f"[train] {torch.cuda.get_device_name(0)}, torch {torch.__version__}")

    skipped = {}
    from collections import Counter

    skipped = Counter()
    real_train = build_items(OCR_DIR / "train", OCR_DIR / "train_labels.txt", skipped)
    val = build_items(OCR_DIR / "val", OCR_DIR / "val_labels.txt", skipped)
    test = build_items(OCR_DIR / "test", OCR_DIR / "test_labels.txt", skipped)
    synth = []
    if not args.no_synth:
        synth = build_items(OCR_DIR / "synth", OCR_DIR / "synth_labels.txt", skipped)
        if args.limit_synth:
            synth = synth[: args.limit_synth]

    if not real_train:
        raise SystemExit(
            "No real training crops. Run scripts/prep_ocr_dataset.py first."
        )

    # The real crops are the ones that matter and there are a thousand of them
    # against forty thousand synthetic. Repeating them is the cheapest honest
    # way to stop the synthetic set drowning the signal; they are augmented
    # differently on every pass, so this is not the same image eight times.
    train_items = synth + real_train * args.real_repeat

    # The supplied real two-row crops, plus optional extra weight on the two-row
    # crops already in the train split. Both are oversampling rather than a
    # weighted sampler, for one reason: with 46 crops against 49432 the sampler
    # would have to be told a weight anyway, and a repeat count is the same
    # thing said in a number that appears in the run log.
    two_row_dir = ROOT / "data" / "ocr_two_row_real"
    origins = {o.strip() for o in args.two_row_origins.split(",") if o.strip()}
    two_row_new = build_two_row_items(
        two_row_dir, two_row_dir / "split.csv", "train", origins, skipped
    )
    two_row_held = build_two_row_items(
        two_row_dir, two_row_dir / "split.csv", "heldout", None, skipped
    )
    if args.two_row_repeat and not two_row_new:
        raise SystemExit(
            "--two-row-repeat was given but data/ocr_two_row_real/split.csv "
            "yielded no train crops. Run scratch/tworow2/prep_two_row_real.py."
        )
    if args.two_row_repeat:
        train_items = train_items + two_row_new * args.two_row_repeat

    existing_two_row_names = two_row_layout_names(
        OCR_DIR / "two_row_layouts.csv", "train"
    )
    existing_two_row = [
        item for item in real_train if item[0].name in existing_two_row_names
    ]
    if args.existing_two_row_repeat:
        train_items = train_items + existing_two_row * args.existing_two_row_repeat

    print(
        f"[train] synthetic {len(synth)}, real {len(real_train)} x{args.real_repeat}"
    )
    if args.two_row_repeat:
        print(
            f"[train] real two-row {len(two_row_new)} x{args.two_row_repeat}"
            f"  origins {sorted(origins)}"
        )
    if args.existing_two_row_repeat:
        print(
            f"[train] existing train two-row {len(existing_two_row)}"
            f" +{args.existing_two_row_repeat} extra repeats"
        )
    if args.two_row_jitter:
        print(f"[train] two-row crops re-laid with probability {args.two_row_jitter}")
    if args.two_row_stitch:
        print(f"[train] two-row crops presented side-by-side with probability "
              f"{args.two_row_stitch}")
    print(f"[train] {len(train_items)} samples/epoch")
    print(f"[train] val {len(val)} real, test {len(test)} real (never trained on)")
    print(
        f"[train] heldout two-row {len(two_row_held)} real "
        f"(never trained on, plate-disjoint from train)"
    )
    for reason, count in skipped.items():
        print(f"[train] skipped {count}: {reason}")

    stitch_paths = {item[0] for item in two_row_new} | {
        item[0] for item in existing_two_row
    }
    train_loader = DataLoader(
        PlateSet(train_items, augment=True, seed=args.seed,
                 stitch_paths=stitch_paths, stitch_prob=args.two_row_stitch,
                 jitter_prob=args.two_row_jitter),
        batch_size=args.batch,
        shuffle=True,
        num_workers=args.workers,
        pin_memory=True,
        drop_last=True,
        persistent_workers=args.workers > 0,
    )
    val_loader = DataLoader(
        PlateSet(val), batch_size=args.batch, num_workers=2, pin_memory=True
    )
    test_loader = DataLoader(
        PlateSet(test), batch_size=args.batch, num_workers=2, pin_memory=True
    )
    # Two two-row scores are printed every epoch. val_two_row is the 21 crops
    # inside the existing val split -- the number that is comparable to every
    # earlier run. heldout is the 31 supplied crops on the other side of the
    # plate-disjoint split, none of whose registrations appear in training, so
    # it is the one that can show generalisation rather than recall.
    val_two_row_names = two_row_layout_names(OCR_DIR / "two_row_layouts.csv", "val")
    val_two_row = [item for item in val if item[0].name in val_two_row_names]
    val_two_row_loader = (
        DataLoader(PlateSet(val_two_row), batch_size=args.batch, num_workers=0)
        if val_two_row else None
    )
    held_loader = (
        DataLoader(PlateSet(two_row_held), batch_size=args.batch, num_workers=0)
        if two_row_held else None
    )

    model = PlateNet(width=args.width).to(device)
    if args.init:
        checkpoint = torch.load(args.init, map_location="cpu", weights_only=False)
        model.load_state_dict(checkpoint["state_dict"])
        print(f"[train] warm start from {args.init} (epoch {checkpoint.get('epoch')})")
    params = sum(p.numel() for p in model.parameters())
    print(f"[train] PlateNet {params / 1e6:.2f}M parameters")

    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.OneCycleLR(
        optimizer,
        max_lr=args.lr,
        epochs=args.epochs,
        steps_per_epoch=len(train_loader),
        pct_start=0.25,
    )
    scaler = torch.amp.GradScaler("cuda")
    # Label smoothing, because a plate crop that genuinely has no readable
    # eighth character should not be trained towards certainty about one.
    criterion = nn.CrossEntropyLoss(label_smoothing=0.05)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir = RUNS / f"ocr_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "args.json").write_text(
        json.dumps(
            {
                **{k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()},
                "alphabet": ALPHABET,
                "slots": MAX_SLOTS,
                "img": [IMG_H, IMG_W],
                "params": params,
                "train_samples": len(train_items),
                "synthetic": len(synth),
                "real_train": len(real_train),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    best = -1.0
    history = []
    for epoch in range(1, args.epochs + 1):
        model.train()
        started = time.time()
        running = 0.0
        for images, targets in train_loader:
            images = images.to(device, non_blocking=True)
            targets = targets.to(device, non_blocking=True)
            optimizer.zero_grad(set_to_none=True)
            with torch.amp.autocast("cuda"):
                logits = model(images)
                loss = criterion(
                    logits.reshape(-1, len(ALPHABET)), targets.reshape(-1)
                )
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()
            scheduler.step()
            running += loss.item()

        result = evaluate(model, val_loader, device)
        two_row = (
            evaluate(model, val_two_row_loader, device) if val_two_row_loader else None
        )
        held = evaluate(model, held_loader, device) if held_loader else None
        history.append({
            "epoch": epoch,
            "loss": running / len(train_loader),
            **result,
            "val_two_row": two_row,
            "heldout_two_row": held,
        })
        marker = ""
        # Selection is on real validation plate accuracy. Selecting on synthetic
        # loss would pick the model that is best at reading our own renderer.
        #
        # `blend` weights val two-row equally with overall val. It is still only
        # val -- the heldout set is printed and never selected on, or it would
        # stop being held out.
        selection = result["plate"]
        if args.select == "blend" and two_row is not None:
            selection = 0.5 * result["plate"] + 0.5 * two_row["plate"]
        if selection > best:
            best = selection
            marker = "  *"
            torch.save(
                {
                    "state_dict": model.state_dict(),
                    "alphabet": ALPHABET,
                    "pad_char": PAD_CHAR,
                    "max_plate_slots": MAX_SLOTS,
                    "img_height": IMG_H,
                    "img_width": IMG_W,
                    "width": args.width,
                    "epoch": epoch,
                    "val_plate": result["plate"],
                },
                args.out,
            )
            write_plate_config(args.out.with_name(args.out.stem + "_plate_config.yaml"))
        # Every epoch, so a crash at 39 of 40 does not cost the run.
        torch.save({"state_dict": model.state_dict(), "epoch": epoch}, run_dir / "last.pt")
        extra = ""
        if two_row is not None:
            extra += f"  val2r {two_row['plate']:.1%}"
        if held is not None:
            extra += f"  held2r {held['plate']:.1%}/{held['chars']:.1%}"
        print(
            f"[train] epoch {epoch:>3d}/{args.epochs}  loss {running / len(train_loader):.4f}"
            f"  val plate {result['plate']:.1%}  chars {result['chars']:.1%}{extra}"
            f"  {time.time() - started:.0f}s{marker}"
        )

    (run_dir / "history.json").write_text(json.dumps(history, indent=2), encoding="utf-8")

    # Load the selected weights back before touching test, so the number
    # reported is the model that will actually be shipped.
    checkpoint = torch.load(args.out, map_location=device, weights_only=False)
    model.load_state_dict(checkpoint["state_dict"])
    final = evaluate(model, test_loader, device)
    held_final = evaluate(model, held_loader, device) if held_loader else None
    print(
        f"\n[train] best epoch {checkpoint['epoch']}, selection score {best:.1%}"
        f"\n[train] TEST plate {final['plate']:.1%}  chars {final['chars']:.1%}"
        f"  over {final['n']} held-out crops"
    )
    if held_final:
        print(
            f"[train] HELDOUT two-row plate {held_final['plate']:.1%}"
            f"  chars {held_final['chars']:.1%}  over {held_final['n']} crops"
        )
    (run_dir / "test.json").write_text(
        json.dumps({"test": final, "heldout_two_row": held_final}, indent=2),
        encoding="utf-8",
    )
    print(f"[train] weights {args.out}")
    print(f"[train] run log {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
