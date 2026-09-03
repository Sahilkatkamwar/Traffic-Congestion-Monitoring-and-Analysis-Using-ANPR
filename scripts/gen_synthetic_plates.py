"""Synthetic Indian plate crops, rendered and then degraded on purpose.

    python scripts/gen_synthetic_plates.py --count 40000

Writes data/ocr/synth/000000.jpg ... and data/ocr/synth_labels.txt in the same
'file<TAB>PLATE' format as the real splits.

Why this exists: the labelled real set is 1182 training crops carrying 617
distinct plate strings. A ten-slot, 36-way classifier trained on that memorises
it in a couple of epochs and learns nothing about glyphs. Synthetic data is not
a substitute for real plates -- it is what makes the real ones teachable, by
supplying the character variety they cannot.

Two rules keep it honest:

  * Strings are generated from the real format grammar and the real state-code
    whitelist in config/state_codes.yaml. The model never sees a string that
    could not be issued.
  * Any plate string that appears in data/ocr/val or data/ocr/test is refused.
    A synthetic MH12AB1234 whose twin sits in the test set is leakage, and it
    is the kind that would not show up in any split check.

Difficulty is varied deliberately. A clean render teaches the model that plates
are clean, and every real crop then looks like noise. Roughly a third of the
output is heavily degraded -- small, blurred, dark, skewed -- because that is
what a plate looks like at forty metres.
"""

import argparse
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

import cv2
import numpy as np
import yaml
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "ocr" / "synth"
LABELS = ROOT / "data" / "ocr" / "synth_labels.txt"

# Plate-like faces on a stock Windows install: condensed, bold, unambiguous.
# Several rather than one, so the model learns the character and not the face.
FONT_DIR = Path("C:/Windows/Fonts")
FONT_NAMES = [
    "ARIALNB.TTF",   # Arial Narrow Bold -- closest to the real plate face
    "FRAMDCN.TTF",   # Franklin Gothic Demi Condensed
    "arialbd.ttf",
    "ariblk.ttf",
    "consolab.ttf",
    "calibrib.ttf",
    "verdanab.ttf",
    "trebucbd.ttf",
    "tahomabd.ttf",
    "seguisb.ttf",
]

# Ground and ink, straight off the road. The palette is not decoration here --
# a commercial plate really is black on yellow, and a model that has only seen
# black on white treats the yellow ones as damaged.
#            (ground BGR),        (ink BGR),      weight
SCHEMES = [
    ((243, 243, 240), (20, 20, 20), 58),    # private: black on white
    ((40, 200, 235), (20, 20, 20), 22),     # commercial: black on yellow
    ((70, 130, 40), (245, 245, 245), 8),    # electric: white on green
    ((25, 25, 25), (240, 240, 240), 6),     # dealer/rental: white on black
    ((40, 40, 190), (245, 245, 245), 3),    # military-ish: white on red
    ((240, 240, 240), (30, 30, 140), 3),    # diplomatic: blue on white
]


def load_state_codes():
    path = ROOT / "config" / "state_codes.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    codes = sorted((data.get("codes") or {}).keys())
    if not codes:
        raise SystemExit(f"no state codes in {path}")
    return codes


# ------------------------------------------------------------------- strings


def make_plate(rng, codes):
    """One plate string from the real grammar, BH series included.

    Mirrors the layouts app/grammar.py validates: [2L][1-2D][1-3L][4D], plus
    the Bharat series. The distribution is skewed towards the two shapes that
    dominate the road, so the model's prior matches reality.
    """
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    digits = "0123456789"

    if rng.random() < 0.04:
        # Bharat: 22BH1234AA
        year = f"{rng.randint(21, 30):02d}"
        tail = "".join(rng.choice(letters) for _ in range(rng.choice([2, 2, 1])))
        return f"{year}BH{rng.randint(1000, 9999)}{tail}"

    state = rng.choice(codes)
    # District numbers run 1..99; single-digit districts really are written
    # with a leading zero on the plate.
    district = f"{rng.randint(1, 99):02d}" if rng.random() < 0.88 else str(rng.randint(1, 9))
    series_len = rng.choices([2, 1, 3], weights=[70, 20, 10])[0]
    series = "".join(rng.choice(letters) for _ in range(series_len))
    number = "".join(rng.choice(digits) for _ in range(4))
    return f"{state}{district}{series}{number}"


# ------------------------------------------------------------------- render


def _fit_font(text, font_path, target_h):
    """Largest size of this face whose cap height fills the row."""
    size = max(8, int(target_h * 1.15))
    for _ in range(24):
        font = ImageFont.truetype(str(font_path), size)
        box = font.getbbox(text)
        if box[3] - box[1] <= target_h:
            return font
        size -= 1
    return ImageFont.truetype(str(font_path), max(8, size))


# ------------------------------------------------------------------- layout
#
# Every layout a real two-row Indian plate was observed to use, audited over the
# 36 two-row plate strings in data/ocr (data/ocr/two_row_layouts.csv):
#
#     split4    KA 05    / HS 4495     17 plates    the only one this file used
#     split5    KL 38 F  / 5008         7 plates    to render, for any string of
#     split6    MH 12 FT / 9458         5 plates    9 characters or more
#     split7    WB07D51  / 06           1 plate
#     stacked   MH14 / GN   then 9239   5 plates    a short left column, then a
#               PY / 01     then BL1155             tall block to its right
#
# A fixed split at 4 bought the adopted model 17.9% exact on the layout it
# renders and **0.000 on all four others** -- 7080 synthetic two-row images
# teaching one of five shapes. The mix below is therefore deliberate rather
# than incidental, and it is not the observed frequency: split4 is the layout
# the model can already do, so it is cut from ~98% of two-row output to 30% and
# the room goes to the four it cannot. All six are reproducible from --seed.
TWO_ROW_FRACTION = 0.18        # unchanged; only the mix within it changes
TWO_ROW_LAYOUTS = [
    ("split4", 30),
    ("split5", 22),
    ("split6", 20),
    ("split7", 10),
    ("stacked_district", 10),   # [state+district] / [series]  then  the number
    ("stacked_state", 8),       # [state] / [district]         then  series+number
]


def layout_blocks(plate, layout):
    """Split a plate string into what each part of the layout renders.

    Returns (column, block): `column` is the list of stacked rows and `block` is
    the text set beside them, or None when the layout is two full-width rows.
    Reading order is always column top-to-bottom then block, which concatenates
    back to the plate string -- the same order the labels are stored in, so a
    layout change never changes the target.
    """
    if layout.startswith("split"):
        cut = int(layout[-1])
        return [plate[:cut], plate[cut:]], None
    if layout == "stacked_district":
        return [plate[:4], plate[4:-4]], plate[-4:]
    if layout == "stacked_state":
        return [plate[:2], plate[2:4]], plate[4:]
    raise ValueError(layout)


def layout_fits(plate, layout):
    """Whether this string can actually be painted in this layout.

    A row of one character is not a layout, it is a typo, and a plate cannot be
    split past its own length. Refusing here rather than clamping keeps the mix
    honest: a short string simply draws from the layouts that fit it.
    """
    column, block = layout_blocks(plate, layout)
    parts = column + ([block] if block is not None else [])
    if layout.startswith("split"):
        return len(column[0]) >= 3 and len(column[1]) >= 2
    return all(len(part) >= 1 for part in parts) and len(block) >= 3


def choose_layout(plate, rng):
    """One of the six two-row forms, or None for the usual single line."""
    if len(plate) < 8 or rng.random() >= TWO_ROW_FRACTION:
        return None
    options = [(n, w) for n, w in TWO_ROW_LAYOUTS if layout_fits(plate, n)]
    if not options:
        return None
    return _weighted_choice(rng, options)[0]


def _space_within(text, rng):
    """'MH12FT' -> 'MH 12 FT', at the letter/digit boundaries only.

    The label carries no spaces, so this is cosmetic; varying it stops the model
    keying on gap positions the way it would if every row were painted alike.
    """
    if len(text) < 3 or rng.random() < 0.45:
        return text
    out = [text[0]]
    for previous, char in zip(text, text[1:]):
        if previous.isdigit() != char.isdigit():
            out.append(" ")
        out.append(char)
    return "".join(out)


# ------------------------------------------------------------------- render


def render(plate, rng, fonts, layout="auto"):
    """A clean, straight-on plate image. Degradation happens afterwards.

    `layout` is one of TWO_ROW_LAYOUTS' names, None for a single line, or "auto"
    to draw one. The caller passes it in so the realised mix can be counted
    without drawing it twice and desynchronising the seeded RNG.
    """
    ground, ink, _ = _weighted_choice(rng, SCHEMES)
    # A little variation per plate: no two plates in a scene are the same white.
    ground = tuple(int(np.clip(c + rng.gauss(0, 10), 0, 255)) for c in ground)
    ink = tuple(int(np.clip(c + rng.gauss(0, 8), 0, 255)) for c in ink)

    font_path = rng.choice(fonts)
    if layout == "auto":
        layout = choose_layout(plate, rng)

    pad_x = rng.randint(6, 22)
    pad_y = rng.randint(4, 14)

    if layout is None:
        # Real plates are spaced in groups; the OCR label is not, so the spacing
        # is cosmetic and varied to stop the model keying on gap positions.
        text = _group(plate) if rng.random() < 0.75 else plate
        char_h = rng.randint(30, 46)
        font = _fit_font(text, font_path, char_h)
        placed = [(text, font, char_h)]
        text_w = font.getbbox(text)[2]
        width, height = text_w + 2 * pad_x, char_h + 2 * pad_y
    else:
        column, block = layout_blocks(plate, layout)
        column = [_space_within(part, rng) for part in column]
        if block is None:
            # Two full-width rows. The lower row is usually painted larger --
            # that is what a motorcycle plate looks like -- so the sizes differ.
            top_h = rng.randint(24, 40)
            bottom_h = int(top_h * rng.uniform(1.0, 1.6))
            gap = rng.randint(2, 8)
            fonts_ = [
                _fit_font(column[0], font_path, top_h),
                _fit_font(column[1], font_path, bottom_h),
            ]
            placed = list(zip(column, fonts_, [top_h, bottom_h]))
            text_w = max(f.getbbox(t)[2] for t, f, _ in placed)
            width = text_w + 2 * pad_x
            height = top_h + bottom_h + gap + 2 * pad_y
        else:
            # A short left column against a tall block. The size difference is
            # the whole visual signature of this form.
            block = _space_within(block, rng)
            block_h = rng.randint(30, 46)
            small_h = max(10, int(block_h * rng.uniform(0.42, 0.58)))
            gap_y = rng.randint(1, 5)
            gap_x = rng.randint(4, 14)
            small_fonts = [_fit_font(part, font_path, small_h) for part in column]
            block_font = _fit_font(block, font_path, block_h)
            column_w = max(f.getbbox(t)[2] for t, f in zip(column, small_fonts))
            block_w = block_font.getbbox(block)[2]
            width = column_w + gap_x + block_w + 2 * pad_x
            height = max(block_h, 2 * small_h + gap_y) + 2 * pad_y

    image = Image.new("RGB", (width, height), ground[::-1])
    draw = ImageDraw.Draw(image)

    if rng.random() < 0.6:  # the embossed border most plates carry
        inset = rng.randint(2, 5)
        draw.rectangle(
            [inset, inset, width - inset - 1, height - inset - 1],
            outline=ink[::-1],
            width=rng.randint(1, 2),
        )

    if layout is not None and block is not None:
        y = pad_y
        for text, font in zip(column, small_fonts):
            box = font.getbbox(text)
            draw.text((pad_x - box[0], y - box[1]), text, font=font, fill=ink[::-1])
            y += small_h + gap_y
        box = block_font.getbbox(block)
        x = pad_x + column_w + gap_x
        y = pad_y + (height - 2 * pad_y - block_h) // 2
        draw.text((x - box[0], y - box[1]), block, font=block_font, fill=ink[::-1])
    else:
        y = pad_y
        for text, font, row_h in placed:
            box = font.getbbox(text)
            x = (width - (box[2] - box[0])) // 2
            draw.text((x - box[0], y - box[1]), text, font=font, fill=ink[::-1])
            y += row_h + (4 if layout is None else gap)

    return cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)


def _group(plate):
    """MH12AB1234 -> 'MH 12 AB 1234', roughly as it is painted."""
    match = re.match(r"^([A-Z]{2})(\d{1,2})([A-Z]{1,3})(\d{4})$", plate)
    if match:
        return " ".join(match.groups())
    match = re.match(r"^(\d{2})(BH)(\d{4})([A-Z]{1,2})$", plate)
    if match:
        return " ".join(match.groups())
    return plate


def _weighted_choice(rng, options):
    total = sum(o[-1] for o in options)
    pick = rng.uniform(0, total)
    running = 0.0
    for option in options:
        running += option[-1]
        if pick <= running:
            return option
    return options[-1]


# ---------------------------------------------------------------- degrade


def degrade(image, rng, hard):
    """Turn a studio render into something a camera might actually have seen.

    `hard` widens every range. A third of the set gets it, because the crops
    that decide real accuracy are the small blurred ones, and a model trained
    only on comfortable images has never met them.
    """
    height, width = image.shape[:2]

    # Perspective: a plate is almost never square to the lens.
    skew = rng.uniform(0.02, 0.16 if hard else 0.07)
    src = np.float32([[0, 0], [width, 0], [width, height], [0, height]])
    jitter = lambda: rng.uniform(-skew, skew)  # noqa: E731
    dst = np.float32(
        [
            [width * jitter(), height * jitter()],
            [width * (1 + jitter()), height * jitter()],
            [width * (1 + jitter()), height * (1 + jitter())],
            [width * jitter(), height * (1 + jitter())],
        ]
    )
    dst -= dst.min(axis=0)
    out_w = max(16, int(dst[:, 0].max()))
    out_h = max(8, int(dst[:, 1].max()))
    image = cv2.warpPerspective(
        image,
        cv2.getPerspectiveTransform(src, dst),
        (out_w, out_h),
        borderMode=cv2.BORDER_REPLICATE,
    )

    if rng.random() < 0.5:  # in-plane rotation
        angle = rng.uniform(-8 if hard else -4, 8 if hard else 4)
        matrix = cv2.getRotationMatrix2D((out_w / 2, out_h / 2), angle, 1.0)
        image = cv2.warpAffine(
            image, matrix, (out_w, out_h), borderMode=cv2.BORDER_REPLICATE
        )

    # Lighting: uneven sun across the plate, then overall exposure.
    if rng.random() < 0.6:
        gradient = np.linspace(
            rng.uniform(0.6, 1.0), rng.uniform(1.0, 1.4), out_w, dtype=np.float32
        )
        if rng.random() < 0.5:
            gradient = gradient[::-1]
        image = np.clip(image * gradient[None, :, None], 0, 255).astype(np.uint8)
    alpha = rng.uniform(0.45 if hard else 0.7, 1.35)
    beta = rng.uniform(-70 if hard else -35, 55)
    image = cv2.convertScaleAbs(image, alpha=alpha, beta=beta)

    # Dirt, screws and shadows: the occlusions that turn O into Q.
    for _ in range(rng.randint(0, 4 if hard else 2)):
        cx, cy = rng.randint(0, out_w), rng.randint(0, out_h)
        radius = rng.randint(2, max(3, out_h // 3))
        shade = rng.randint(0, 90) if rng.random() < 0.7 else rng.randint(180, 255)
        overlay = image.copy()
        cv2.circle(overlay, (cx, cy), radius, (shade, shade, shade), -1)
        image = cv2.addWeighted(overlay, rng.uniform(0.2, 0.75), image, 1 - rng.uniform(0.2, 0.75), 0)

    # Resolution: the single biggest thing separating an easy crop from a hard
    # one. Downscale to a realistic capture width, then let the loader upscale.
    target_w = rng.randint(42, 88) if hard else rng.randint(70, 260)
    scale = target_w / out_w
    small = cv2.resize(
        image,
        (max(16, int(out_w * scale)), max(8, int(out_h * scale))),
        interpolation=cv2.INTER_AREA,
    )

    if rng.random() < 0.55:  # motion blur along the direction of travel
        length = rng.randint(3, 7 if hard else 5)
        kernel = np.zeros((length, length), np.float32)
        if rng.random() < 0.75:
            kernel[length // 2, :] = 1.0
        else:
            np.fill_diagonal(kernel, 1.0)
        small = cv2.filter2D(small, -1, kernel / kernel.sum())
    elif rng.random() < 0.5:
        k = rng.choice([3, 5])
        small = cv2.GaussianBlur(small, (k, k), 0)

    if rng.random() < 0.7:  # sensor noise
        sigma = rng.uniform(2, 22 if hard else 9)
        small = np.clip(
            small.astype(np.float32) + np.random.normal(0, sigma, small.shape), 0, 255
        ).astype(np.uint8)

    # JPEG last, as it would be in a real pipeline.
    quality = rng.randint(18 if hard else 45, 92)
    ok, buffer = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if ok:
        small = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    return small


# ---------------------------------------------------------------------- run


def held_out_plates(ocr_dir):
    """Every plate string in val and test, which synthesis must never emit."""
    banned = set()
    for split in ("val", "test"):
        path = ocr_dir / f"{split}_labels.txt"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) == 2:
                banned.add(parts[1].strip())
    return banned


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--count", type=int, default=40000)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--labels", type=Path, default=LABELS)
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--hard-fraction", type=float, default=0.35)
    args = parser.parse_args(argv)

    fonts = [FONT_DIR / name for name in FONT_NAMES if (FONT_DIR / name).exists()]
    if not fonts:
        raise SystemExit(f"no usable fonts found in {FONT_DIR}")
    print(f"[synth] {len(fonts)} font(s): {', '.join(f.name for f in fonts)}")

    codes = load_state_codes()
    banned = held_out_plates(args.out.parent)
    print(f"[synth] {len(codes)} state codes, {len(banned)} held-out plate(s) refused")

    rng = random.Random(args.seed)
    np.random.seed(args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    for stale in args.out.glob("*.jpg"):
        stale.unlink()

    lines = []
    collisions = 0
    layouts = Counter()
    for index in range(args.count):
        while True:
            plate = make_plate(rng, codes)
            if plate not in banned:
                break
            collisions += 1
        # Drawn here rather than inside render() so the realised mix can be
        # counted and written down. A distribution nobody measured is how the
        # fixed split at 4 survived 40000 images unnoticed.
        layout = choose_layout(plate, rng)
        layouts[layout or "single"] += 1
        image = render(plate, rng, fonts, layout=layout)
        image = degrade(image, rng, hard=rng.random() < args.hard_fraction)
        name = f"{index:06d}.jpg"
        cv2.imwrite(str(args.out / name), image)
        lines.append(f"{name}\t{plate}")
        if (index + 1) % 5000 == 0:
            print(f"[synth] {index + 1}/{args.count}")

    args.labels.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # The realised mix, written beside the labels. TRAINING.md quotes it, and
    # the next person to wonder what shapes the model has seen reads it here
    # instead of counting images.
    two_row = sum(v for k, v in layouts.items() if k != "single")
    (args.labels.parent / "synth_layouts.json").write_text(
        json.dumps(
            {
                "count": len(lines),
                "seed": args.seed,
                "hard_fraction": args.hard_fraction,
                "two_row_fraction_target": TWO_ROW_FRACTION,
                "two_row_images": two_row,
                "layouts": dict(layouts.most_common()),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"[synth] wrote {len(lines)} crops to {args.out} and {args.labels}"
        f" ({collisions} held-out collision(s) resampled)"
    )
    print(f"[synth] two-row {two_row} of {len(lines)} = {two_row / len(lines):.1%}")
    for name, count in layouts.most_common():
        tail = f"  {count / two_row:.1%} of two-row" if name != "single" and two_row else ""
        print(f"[synth]   {name:<18s} {count:>6d}{tail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
