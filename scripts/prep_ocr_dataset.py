"""Verify the labelled plate crops, then split them into train/val/test.

    python scripts/prep_ocr_dataset.py                 # verify + build the split
    python scripts/prep_ocr_dataset.py --verify-only   # report, write nothing

Input is one folder of plate crops plus one CSV describing them:

    data/raw/plates/
        plate_labels.csv
        <crop>.jpg  x1690

    cropped_image,source_image,plate_text,xmin,ymin,xmax,ymax,image_width,image_height

Output is the layout TRAINING.md and app/eval_plates.py both expect:

    data/ocr/train/000001.jpg ...   data/ocr/train_labels.txt   (file<TAB>PLATE)
    data/ocr/val/  ...              data/ocr/val_labels.txt
    data/ocr/test/ ...              data/ocr/test_labels.txt
    data/ocr/manifest.csv           split, new name, original name, plate

Two properties of this dataset drive the split, and getting either wrong
produces an accuracy number that is a lie:

  * 1690 crops carry only 981 distinct plate strings. 654 of them are frames
    sampled from eleven videos, so one vehicle can appear 41 times. Splitting
    per image puts frame 12 of a car in train and frame 13 in test, and the
    model is then scored on plates it memorised. The split is therefore made
    over *plate strings*, and every crop of a plate lands in the same split.
  * The three origins -- studio-ish OLX listings, web photos, and video frames
    -- are not equally hard. Each is stratified across the splits so test is
    not accidentally the easy third.

Nothing here imports from app/. TRAINING.md keeps the two tracks apart.
"""

import argparse
import csv
import json
import random
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "plates"
OUT = ROOT / "data" / "ocr"
RUNS = ROOT / "runs"

PLATE_CHARS = re.compile(r"^[A-Z0-9]+$")
MIN_CHARS = 4       # shorter than this is not a plate, whatever the CSV says
MIN_WIDTH = 16      # below this there is no glyph left to read
MIN_HEIGHT = 8

# Placeholder strings the labeller used for crops they could not read. They are
# honest annotations of an unreadable image, not plates, and training or scoring
# on them measures nothing.
NON_PLATE_LABELS = {"DEVANAGRI", "BLUR", "UNREADABLE", "NA", "NONE", "UNKNOWN"}


def load_corrections(raw_dir):
    """label_corrections.csv -> {cropped_image: (action, corrected_plate, reason)}.

    Hand-audited fixes to plate_labels.csv, kept in their own file rather than
    edited into the CSV so that every change to a label is visible, dated and
    reversible. Keyed on the crop filename, which survives a re-split; the
    sequential names in data/ocr/ do not.
    """
    path = raw_dir / "label_corrections.csv"
    if not path.exists():
        return {}
    body = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    ]
    out = {}
    for row in csv.DictReader(body):
        out[row["cropped_image"]] = (
            row["action"].strip(),
            row["corrected_plate"].strip().upper(),
            row["reason"].strip(),
        )
    return out


def normalize(text):
    """Uppercase, strip everything that is not A-Z or 0-9."""
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def origin_of(name):
    """Which of the three source collections a crop came from."""
    return name.split("__", 1)[0] if "__" in name else "other"


# ------------------------------------------------------------------- verify


def verify(raw_dir):
    """Read the CSV, check it against the files on disk, return (rows, report).

    Every row that survives has: a file that exists and decodes, a plate string
    of only A-Z0-9, and enough pixels to be worth reading. Everything dropped is
    counted and named in the report rather than silently disappearing.
    """
    csv_path = raw_dir / "plate_labels.csv"
    if not csv_path.exists():
        raise SystemExit(f"No CSV at {csv_path}")

    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    on_disk = {p.name for p in raw_dir.glob("*.jpg")}
    corrections = load_corrections(raw_dir)
    report = {
        "corrections": corrections,
        "csv_rows": len(rows),
        "images_on_disk": len(on_disk),
        "dropped": defaultdict(list),
        "kept": 0,
    }

    seen = Counter(r["cropped_image"] for r in rows)
    report["duplicate_csv_rows"] = [n for n, c in seen.items() if c > 1]
    report["images_without_csv_row"] = sorted(on_disk - set(seen))

    kept, geometry_mismatch, invalid_grammar_shape = [], [], 0
    for row in rows:
        name = row["cropped_image"]
        action, corrected, _reason = corrections.get(name, ("", "", ""))
        if action == "drop":
            report["dropped"]["hand-audited, see label_corrections.csv"].append(name)
            continue
        if action == "relabel":
            row = dict(row, plate_text=corrected)
        if name not in on_disk:
            report["dropped"]["no image file"].append(name)
            continue
        plate = normalize(row["plate_text"])
        if not row["plate_text"] or not PLATE_CHARS.match(row["plate_text"].strip()):
            if normalize(row["plate_text"]) in NON_PLATE_LABELS or not plate:
                report["dropped"]["not a plate string"].append(
                    f"{name} = {row['plate_text']!r}"
                )
                continue
        if plate.upper() in NON_PLATE_LABELS:
            report["dropped"]["not a plate string"].append(
                f"{name} = {row['plate_text']!r}"
            )
            continue
        if len(plate) < MIN_CHARS:
            report["dropped"]["label too short"].append(f"{name} = {plate}")
            continue

        image = cv2.imread(str(raw_dir / name))
        if image is None:
            report["dropped"]["image will not decode"].append(name)
            continue
        height, width = image.shape[:2]
        if width < MIN_WIDTH or height < MIN_HEIGHT:
            report["dropped"]["too few pixels"].append(f"{name} = {width}x{height}")
            continue

        # The CSV's box is what the crop was cut with, so the crop's own size is
        # an independent check on the row: if they disagree the CSV describes a
        # different image than the one on disk and no field on that row is
        # trustworthy.
        box_w = int(row["xmax"]) - int(row["xmin"])
        box_h = int(row["ymax"]) - int(row["ymin"])
        if abs(width - box_w) > 2 or abs(height - box_h) > 2:
            geometry_mismatch.append(f"{name}: crop {width}x{height} box {box_w}x{box_h}")

        kept.append(
            {
                "name": name,
                "plate": plate,
                "origin": origin_of(name),
                "width": width,
                "height": height,
                "source_image": row["source_image"],
            }
        )

    report["geometry_mismatch"] = geometry_mismatch
    report["kept"] = len(kept)
    report["dropped"] = dict(report["dropped"])
    return kept, report


def print_report(rows, report):
    print("\n=== plate label verification " + "=" * 44)
    print(f"  CSV rows                {report['csv_rows']}")
    print(f"  images on disk          {report['images_on_disk']}")
    print(f"  duplicate CSV rows      {len(report['duplicate_csv_rows'])}")
    print(f"  images with no CSV row  {len(report['images_without_csv_row'])}")
    print(
        f"  hand-audited corrections {len(report.get('corrections', {}))} "
        f"(data/raw/plates/label_corrections.csv)"
    )
    print(
        f"  crop size vs CSV box    {len(report['geometry_mismatch'])} mismatched "
        f"(0 means the CSV describes exactly these files)"
    )
    total_dropped = sum(len(v) for v in report["dropped"].values())
    print(f"\n  dropped {total_dropped}:")
    for reason, items in sorted(report["dropped"].items()):
        print(f"    {len(items):>4}  {reason}")
        for item in items[:3]:
            print(f"            e.g. {item[:96]}")
    print(f"\n  kept {report['kept']}")

    plates = Counter(r["plate"] for r in rows)
    widths = sorted(r["width"] for r in rows)
    print(f"    distinct plate strings   {len(plates)}")
    print(f"    plates with >1 crop      {sum(1 for c in plates.values() if c > 1)}")
    print(f"    most-repeated plate      {plates.most_common(1)[0]}")
    print(f"    crop width min/med/max   {widths[0]}/{widths[len(widths) // 2]}/{widths[-1]}")
    print(
        f"    crops under 48px wide    {sum(1 for w in widths if w < 48)}"
        f"  (the app's min_plate_width; it would not OCR these)"
    )
    print("    by origin:")
    for origin, count in Counter(r["origin"] for r in rows).most_common():
        print(f"      {origin:<18s} {count}")
    print("    label length histogram:")
    for length, count in sorted(Counter(len(r["plate"]) for r in rows).items()):
        print(f"      {length:>2d} chars  {count}")


# -------------------------------------------------------------------- split


def split_rows(rows, ratios, seed):
    """Group by plate string, stratify by origin, fill to the target ratios.

    Groups are placed largest-first: a 41-crop group placed late overshoots its
    split by more than a 1-crop group can correct, so the big ones choose first
    and the small ones tidy up after them.
    """
    train_r, val_r, test_r = ratios
    groups = defaultdict(list)
    for row in rows:
        groups[row["plate"]].append(row)

    # A group's origin is where most of its crops came from.
    by_origin = defaultdict(list)
    for plate, members in groups.items():
        origin = Counter(m["origin"] for m in members).most_common(1)[0][0]
        by_origin[origin].append((plate, members))

    assigned = {}
    rng = random.Random(seed)
    for origin in sorted(by_origin):
        bucket = by_origin[origin]
        rng.shuffle(bucket)
        bucket.sort(key=lambda pair: -len(pair[1]))
        total = sum(len(m) for _, m in bucket)
        target = {
            "train": train_r * total,
            "val": val_r * total,
            "test": test_r * total,
        }
        have = {"train": 0, "val": 0, "test": 0}
        for plate, members in bucket:
            # Whichever split is furthest below its share takes the group.
            split = max(target, key=lambda s: target[s] - have[s])
            assigned[plate] = split
            have[split] += len(members)

    out = {"train": [], "val": [], "test": []}
    for plate, members in groups.items():
        out[assigned[plate]].extend(members)
    return out


def materialise(splits, out_dir, raw_dir):
    """Copy crops into data/ocr/<split>/ under sequential names, write labels.

    Renaming is not tidiness: the original names run to 177 characters and
    Windows starts failing somewhere past 260 for the whole path. manifest.csv
    keeps every original name, so nothing about where a crop came from is lost.
    """
    manifest = []
    for split, rows in splits.items():
        folder = out_dir / split
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        rows = sorted(rows, key=lambda r: (r["plate"], r["name"]))
        lines = []
        for index, row in enumerate(rows):
            new_name = f"{index:06d}.jpg"
            shutil.copy2(raw_dir / row["name"], folder / new_name)
            lines.append(f"{new_name}\t{row['plate']}")
            manifest.append(
                {
                    "split": split,
                    "file": new_name,
                    "plate": row["plate"],
                    "origin": row["origin"],
                    "width": row["width"],
                    "height": row["height"],
                    "original": row["name"],
                }
            )
        (out_dir / f"{split}_labels.txt").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    with (out_dir / "manifest.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["split", "file", "plate", "origin", "width", "height", "original"],
        )
        writer.writeheader()
        writer.writerows(manifest)
    return manifest


def print_split(splits):
    print("\n=== split " + "=" * 62)
    all_plates = {}
    for split, rows in splits.items():
        for row in rows:
            all_plates.setdefault(row["plate"], set()).add(split)
    leaked = [p for p, s in all_plates.items() if len(s) > 1]
    print(f"  {'split':<8s} {'crops':>6s} {'plates':>7s}   origins")
    for split in ("train", "val", "test"):
        rows = splits[split]
        origins = Counter(r["origin"] for r in rows)
        mix = "  ".join(f"{k.split('_')[0]}:{v}" for k, v in sorted(origins.items()))
        print(
            f"  {split:<8s} {len(rows):>6d} {len({r['plate'] for r in rows}):>7d}   {mix}"
        )
    print(f"\n  plate strings appearing in more than one split: {len(leaked)}")
    if leaked:
        print(f"    LEAK: {leaked[:5]}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--raw", type=Path, default=RAW)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--ratios", default="0.70,0.15,0.15")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)

    rows, report = verify(args.raw)
    print_report(rows, report)
    if args.verify_only:
        return 0

    ratios = tuple(float(x) for x in args.ratios.split(","))
    if abs(sum(ratios) - 1.0) > 1e-6:
        raise SystemExit(f"--ratios must sum to 1, got {ratios}")

    splits = split_rows(rows, ratios, args.seed)
    print_split(splits)
    materialise(splits, args.out, args.raw)

    RUNS.mkdir(exist_ok=True)
    summary = {
        "seed": args.seed,
        "ratios": list(ratios),
        "verification": {
            k: (len(v) if isinstance(v, (list, dict)) else v)
            for k, v in report.items()
            if k != "dropped"
        },
        "dropped": {k: len(v) for k, v in report["dropped"].items()},
        "splits": {
            s: {"crops": len(r), "plates": len({x["plate"] for x in r})}
            for s, r in splits.items()
        },
    }
    (RUNS / "ocr_split.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(f"\n  wrote {args.out}/ and {RUNS / 'ocr_split.json'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
