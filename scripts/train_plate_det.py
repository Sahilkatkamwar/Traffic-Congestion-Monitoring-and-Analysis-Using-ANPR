"""Fine-tune 3 -- the plate detector, on the bounding boxes that actually exist.

    python scripts/train_plate_det.py --prep          # split only, print the audit
    python scripts/train_plate_det.py --epochs 60     # split, train, compare

Input is data/plate_det/, which holds the only real bounding-box annotation in
this project: 1086 images with YOLO labels, single class, plate.

## Read the audit before you read the mAP

The 1086 images are two collections and they are not the same task:

  * 905 `License (N)` images are already-cropped plates. The box covers a median
    86% of the image. Training on these teaches "the plate is the whole frame",
    which is the exact failure app/ocr.py already has to defend against with
    max_plate_area_frac -- given a vehicle crop, the stock detector returns one
    confident box around the entire crop.
  * 181 numbered images are studio photographs of plates on a table or wall.
    The boxes are accurate and the plates are small in frame, but there is no
    vehicle, no distance and no motion in any of them.

Neither is traffic footage. So the honest experiment is run both ways and both
numbers are printed: mAP on a held-out slice of this data, which says whether
the model learned this dataset, and the plate-hit rate on real vehicle crops
from footage/, which says whether it got better at the job. Only the second one
decides whether the weights are adopted. `--subset scenes` trains on the 181
alone, which is the split whose geometry at least resembles detection.

There is deliberately no vehicle-detector fine-tune here. There are no vehicle
boxes anywhere in this project -- data/vehicle_cls/ is folder-labelled crops for
a classifier, not detection -- and TRAINING.md's reason stands on its own:
single-class fine-tuning of the COCO detector trades cars for autos.

Nothing here imports from app/.
"""

import argparse
import json
import random
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import yaml

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "plate_det"
SPLIT = ROOT / "data" / "plate_det_split"
RUNS = ROOT / "runs"
BASE_WEIGHTS = ROOT / "models" / "plate_detector.pt"
OUT = ROOT / "models" / "finetuned" / "plate_det_best.pt"


def subset_of(stem):
    """Which of the two collections an image belongs to."""
    return "crops" if stem.lower().startswith("license") else "scenes"


# --------------------------------------------------------------------- audit


def read_boxes(path):
    boxes = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        boxes.append((int(parts[0]), *(float(v) for v in parts[1:5])))
    return boxes


def audit(src):
    """Every image+label pair, with the numbers that decide how to use them."""
    images = {p.stem: p for p in (src / "images").iterdir() if p.is_file()}
    labels = {p.stem: p for p in (src / "labels").glob("*.txt")}
    paired = sorted(set(images) & set(labels))

    rows, problems = [], []
    for stem in paired:
        image = cv2.imread(str(images[stem]))
        if image is None:
            problems.append(f"{stem}: will not decode")
            continue
        height, width = image.shape[:2]
        boxes = read_boxes(labels[stem])
        if not boxes:
            problems.append(f"{stem}: label file has no box")
            continue
        bad = [
            b
            for b in boxes
            if not (0 <= b[1] <= 1 and 0 <= b[2] <= 1 and 0 < b[3] <= 1 and 0 < b[4] <= 1)
        ]
        if bad:
            problems.append(f"{stem}: {len(bad)} box(es) out of range")
            continue
        rows.append(
            {
                "stem": stem,
                "image": images[stem],
                "label": labels[stem],
                "subset": subset_of(stem),
                "boxes": len(boxes),
                "width": width,
                "height": height,
                "max_area": max(b[3] * b[4] for b in boxes),
                "classes": {b[0] for b in boxes},
            }
        )

    orphan_images = sorted(set(images) - set(labels))
    orphan_labels = sorted(set(labels) - set(images))
    return rows, {
        "problems": problems,
        "images_without_label": orphan_images,
        "labels_without_image": orphan_labels,
    }


def print_audit(rows, notes):
    print("\n=== plate-detection annotation audit " + "=" * 35)
    print(f"  usable image+label pairs   {len(rows)}")
    print(f"  images with no label       {len(notes['images_without_label'])}")
    print(f"  labels with no image       {len(notes['labels_without_image'])}")
    print(f"  rejected                   {len(notes['problems'])}")
    for problem in notes["problems"][:5]:
        print(f"      {problem}")
    classes = set()
    for row in rows:
        classes |= row["classes"]
    print(f"  class ids present          {sorted(classes)}  (single class: plate)")
    print(f"  total boxes                {sum(r['boxes'] for r in rows)}")

    print(f"\n  {'subset':<9s} {'images':>7s} {'boxes':>6s} {'med img':>10s} "
          f"{'med box area':>13s} {'>50% of frame':>14s}")
    for subset in ("scenes", "crops"):
        group = [r for r in rows if r["subset"] == subset]
        if not group:
            continue
        areas = sorted(r["max_area"] for r in group)
        widths = sorted(r["width"] for r in group)
        heights = sorted(r["height"] for r in group)
        big = sum(1 for a in areas if a > 0.5) / len(areas)
        print(
            f"  {subset:<9s} {len(group):>7d} {sum(r['boxes'] for r in group):>6d} "
            f"{widths[len(widths) // 2]:>4d}x{heights[len(heights) // 2]:<5d} "
            f"{areas[len(areas) // 2]:>13.3f} {big:>13.0%}"
        )
    print(
        "\n  A median box area of 0.86 means the image IS the plate. Those are\n"
        "  valid annotations of a crop, not of a scene, and a detector trained on\n"
        "  them learns to return the whole frame."
    )


# --------------------------------------------------------------------- split


def build_split(rows, out, ratios, seed, subset):
    """Materialise an Ultralytics-shaped dataset. Returns the yaml path.

    Stems are renamed: several carry spaces and parentheses, and a dataset path
    that needs quoting is a trap waiting for the next person.
    """
    if subset != "all":
        rows = [r for r in rows if r["subset"] == subset]
    if not rows:
        raise SystemExit(f"no images in subset {subset!r}")

    by_subset = defaultdict(list)
    for row in rows:
        by_subset[row["subset"]].append(row)

    assigned = {"train": [], "val": [], "test": []}
    rng = random.Random(seed)
    for group in by_subset.values():
        rng.shuffle(group)
        n = len(group)
        n_train = int(n * ratios[0])
        n_val = int(n * ratios[1])
        assigned["train"] += group[:n_train]
        assigned["val"] += group[n_train : n_train + n_val]
        assigned["test"] += group[n_train + n_val :]

    if out.exists():
        shutil.rmtree(out)
    for split, group in assigned.items():
        (out / split / "images").mkdir(parents=True)
        (out / split / "labels").mkdir(parents=True)
        for index, row in enumerate(sorted(group, key=lambda r: r["stem"])):
            name = f"{row['subset']}_{index:05d}"
            shutil.copy2(row["image"], out / split / "images" / f"{name}{row['image'].suffix}")
            shutil.copy2(row["label"], out / split / "labels" / f"{name}.txt")

    yaml_path = out / "plate_det.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "path": str(out),
                "train": "train/images",
                "val": "val/images",
                "test": "test/images",
                "names": {0: "plate"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    print("\n=== split " + "=" * 62)
    for split in ("train", "val", "test"):
        mix = Counter(r["subset"] for r in assigned[split])
        print(f"  {split:<6s} {len(assigned[split]):>5d}   " + "  ".join(
            f"{k}:{v}" for k, v in sorted(mix.items())
        ))
    print(f"  wrote {yaml_path}")
    return yaml_path


# ---------------------------------------------------------- real-footage check


def plate_hit_rate(weights, crop_dir, settings):
    """How often this detector finds a plate-shaped box in a real vehicle crop.

    This is the number that matters. The crops come from crops/evidence/, which
    the running app wrote from real footage, so they are exactly what the
    detector is handed in production. There are no ground-truth boxes for them,
    so what is measured is not accuracy -- it is how often the detector returns
    something that survives app/ocr.py's own shape filter. A model that returns
    the whole crop every time scores zero here, which is the point.
    """
    from ultralytics import YOLO

    crops = [
        p
        for p in sorted(Path(crop_dir).rglob("*"))
        if p.suffix.lower() in {".jpg", ".jpeg", ".png"}
    ]
    if not crops:
        return None

    defaults = settings.get("defaults") or {}
    min_width = int(defaults.get("min_plate_width", 48))
    max_area = float(defaults.get("max_plate_area_frac", 0.35))
    lo = float(defaults.get("min_plate_aspect", 1.1))
    hi = float(defaults.get("max_plate_aspect", 10.0))
    conf = float(defaults.get("plate_conf", 0.25))
    imgsz = int(defaults.get("plate_imgsz", 640))

    model = YOLO(str(weights))
    hits = raw = 0
    for path in crops:
        image = cv2.imread(str(path))
        if image is None:
            continue
        height, width = image.shape[:2]
        result = model(image, imgsz=imgsz, conf=conf, device=0, verbose=False)[0]
        if result.boxes is None or len(result.boxes) == 0:
            continue
        raw += 1
        for box in result.boxes.xyxy.cpu().numpy():
            x1, y1, x2, y2 = (int(v) for v in box)
            box_w, box_h = max(0, x2 - x1), max(0, y2 - y1)
            if box_w < min_width or box_h < 8:
                continue
            if not lo <= box_w / box_h <= hi:
                continue
            if (box_w * box_h) / float(width * height) > max_area:
                continue
            hits += 1
            break
    return {
        "crops": len(crops),
        "any_box": raw,
        "plate_shaped": hits,
        "rate": hits / len(crops) if crops else 0.0,
    }


# ----------------------------------------------------------------------- main


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--src", type=Path, default=SRC)
    parser.add_argument("--split-dir", type=Path, default=SPLIT)
    parser.add_argument("--subset", default="all", choices=["all", "scenes", "crops"])
    parser.add_argument("--ratios", default="0.70,0.15,0.15")
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--epochs", type=int, default=60)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument("--lr", type=float, default=0.001,
                        help="low by default: this is a fine-tune, not a fresh run")
    parser.add_argument("--prep", action="store_true", help="audit and split, no training")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)

    rows, notes = audit(args.src)
    print_audit(rows, notes)
    ratios = tuple(float(x) for x in args.ratios.split(","))
    yaml_path = build_split(rows, args.split_dir, ratios, args.seed, args.subset)
    if args.prep:
        return 0

    import os

    os.environ.setdefault("YOLO_AUTOINSTALL", "false")
    from ultralytics import YOLO

    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))

    print("\n=== baseline " + "=" * 59)
    base = YOLO(str(BASE_WEIGHTS))
    base_val = base.val(data=str(yaml_path), split="test", imgsz=args.imgsz, device=0, verbose=False)
    print(f"  {BASE_WEIGHTS.name}  mAP50 {base_val.box.map50:.3f}  mAP50-95 {base_val.box.map:.3f}")

    print("\n=== fine-tune " + "=" * 58)
    model = YOLO(str(BASE_WEIGHTS))
    model.train(
        data=str(yaml_path),
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device=0,
        lr0=args.lr,
        seed=args.seed,
        project=str(RUNS),
        name=f"plate_det_{args.subset}",
        exist_ok=True,
        pretrained=True,
        verbose=False,
        plots=False,
    )
    best = Path(model.trainer.best)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(best, args.out)

    tuned = YOLO(str(args.out))
    tuned_val = tuned.val(data=str(yaml_path), split="test", imgsz=args.imgsz, device=0, verbose=False)

    print("\n=== held-out mAP on this dataset " + "=" * 39)
    print(f"  {'model':<28s} {'mAP50':>7s} {'mAP50-95':>9s}")
    print(f"  {'pretrained plate_detector':<28s} {base_val.box.map50:>7.3f} {base_val.box.map:>9.3f}")
    print(f"  {'fine-tuned':<28s} {tuned_val.box.map50:>7.3f} {tuned_val.box.map:>9.3f}")

    print("\n=== plate hit rate on real vehicle crops " + "=" * 31)
    crop_dir = ROOT / (settings.get("paths", {}).get("crops") or "crops/evidence")
    base_hit = plate_hit_rate(BASE_WEIGHTS, crop_dir, settings)
    tuned_hit = plate_hit_rate(args.out, crop_dir, settings)
    if base_hit is None:
        print(f"  no crops in {crop_dir}; run the app over footage first")
    else:
        print(f"  {'model':<28s} {'any box':>8s} {'plate-shaped':>13s} {'rate':>7s}")
        for label, result in (("pretrained", base_hit), ("fine-tuned", tuned_hit)):
            print(
                f"  {label:<28s} {result['any_box']:>8d} {result['plate_shaped']:>13d} "
                f"{result['rate']:>7.1%}"
            )
        print(f"  over {base_hit['crops']} real vehicle crops from {crop_dir}")

    RUNS.mkdir(exist_ok=True)
    (RUNS / f"plate_det_{args.subset}_compare.json").write_text(
        json.dumps(
            {
                "subset": args.subset,
                "epochs": args.epochs,
                "baseline": {"map50": float(base_val.box.map50), "map": float(base_val.box.map)},
                "finetuned": {"map50": float(tuned_val.box.map50), "map": float(tuned_val.box.map)},
                "hit_rate": {"baseline": base_hit, "finetuned": tuned_hit},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        "\n  Adopt the fine-tune only if it wins on BOTH tables. Winning the first\n"
        "  alone means it learned this dataset's framing, which is not the job.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
