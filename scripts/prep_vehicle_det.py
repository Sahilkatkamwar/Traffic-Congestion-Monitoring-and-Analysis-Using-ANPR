"""Build a trainable split from data/vehicle_det's pseudo-labels.

`data/vehicle_det` is read-only input. Everything this writes goes into
`data/vehicle_det_split`, in the same shape as `data/plate_det_split`, so the
original pseudo-labels are never edited in place and the split can be rebuilt
from scratch at any time.

Two things happen on the way through, and both are consequences of how the
labels were made -- YOLO11-L and RT-DETR-L run over the same frames and their
outputs merged, with no NMS across the two models:

  dedup   31 pairs of boxes overlap at IoU > 0.75. Those are one vehicle
          labelled twice, sometimes under two different class names. Trained
          on as-is they teach the detector to emit two boxes per vehicle, which
          is precisely the duplicate-sighting failure this project already
          fights. The larger box of a pair is kept.

  split   the 216 frames are a single ~9-minute run of one fixed camera,
          sampled about every 2.4 seconds. A random split would put the same
          vehicle in train and val -- 1139 and 1140 are two seconds apart and
          share three of their four vehicles. The split is therefore
          **temporal**: the last 20% of frame numbers is val, and no vehicle
          crosses the boundary.

Class ids are the dataset's own, read off the burned-in names in
`data/vehicle_det/Boxed`:

    0 car   1 motorcycle   2 bus   3 truck

which is COCO's [2, 3, 5, 7] in the same order. The app maps a fine-tuned
detector's ids back to COCO in app/detect.py; nothing else in the pipeline
needs to know.

    env\\Scripts\\python.exe scripts\\prep_vehicle_det.py
    env\\Scripts\\python.exe scripts\\prep_vehicle_det.py --val-frac 0.2 --dedup-iou 0.75
"""

import argparse
import json
import shutil
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "vehicle_det"
OUT = ROOT / "data" / "vehicle_det_split"
NAMES = {0: "car", 1: "motorcycle", 2: "bus", 3: "truck"}


def parse(path):
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 5:
            continue
        out.append((int(parts[0]), *(float(v) for v in parts[1:])))
    return out


def to_xyxy(box):
    _c, cx, cy, w, h = box
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def iou(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def dedup(boxes, threshold):
    """Drop the smaller of any pair of boxes that overlap above `threshold`.

    Class-agnostic on purpose. The duplicates in this set are frequently a
    `truck` box and a `bus` box around one lorry -- two models disagreeing
    about the label of the same object -- and keeping both would train the
    detector to fire twice *and* to be uncertain about which class.
    """
    order = sorted(range(len(boxes)), key=lambda i: -(boxes[i][3] * boxes[i][4]))
    kept = []
    dropped = 0
    for i in order:
        xy = to_xyxy(boxes[i])
        if any(iou(xy, to_xyxy(boxes[j])) > threshold for j in kept):
            dropped += 1
            continue
        kept.append(i)
    return [boxes[i] for i in sorted(kept)], dropped


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--val-frac", type=float, default=0.2)
    ap.add_argument("--dedup-iou", type=float, default=0.75)
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    out = Path(args.out)
    images = sorted(
        (SRC / "images").glob("*.jpg"), key=lambda p: int(p.stem.split("_")[1])
    )
    if not images:
        raise SystemExit(f"No images under {SRC / 'images'}.")

    cut = int(len(images) * (1 - args.val_frac))
    assign = {p.stem: ("train" if i < cut else "val") for i, p in enumerate(images)}

    for split in ("train", "val"):
        for kind in ("images", "labels"):
            d = out / split / kind
            if d.exists():
                shutil.rmtree(d)
            d.mkdir(parents=True, exist_ok=True)

    stats = {
        s: {"images": 0, "boxes": 0, "classes": Counter(), "empty": 0}
        for s in ("train", "val")
    }
    total_dropped = 0
    for p in images:
        split = assign[p.stem]
        label = SRC / "labels" / f"{p.stem}.txt"
        boxes = parse(label) if label.exists() else []
        boxes, dropped = dedup(boxes, args.dedup_iou)
        total_dropped += dropped
        shutil.copy2(p, out / split / "images" / p.name)
        (out / split / "labels" / f"{p.stem}.txt").write_text(
            "".join(
                f"{c} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}\n" for c, cx, cy, w, h in boxes
            ),
            encoding="utf-8",
        )
        stats[split]["images"] += 1
        stats[split]["boxes"] += len(boxes)
        stats[split]["empty"] += not boxes
        for c, *_ in boxes:
            stats[split]["classes"][NAMES[c]] += 1

    yaml_path = out / "vehicle_det.yaml"
    yaml_path.write_text(
        f"path: {out}\n"
        "train: train/images\n"
        "val: val/images\n"
        "names:\n"
        + "".join(f"  {k}: {v}\n" for k, v in sorted(NAMES.items())),
        encoding="utf-8",
    )

    report = {
        "source": SRC.relative_to(ROOT).as_posix(),
        "out": out.relative_to(ROOT).as_posix(),
        "val_frac": args.val_frac,
        "dedup_iou": args.dedup_iou,
        "duplicate_boxes_dropped": total_dropped,
        "split_boundary_frame": images[cut].stem,
        "splits": {
            s: {
                "images": v["images"],
                "boxes": v["boxes"],
                "empty_label_files": v["empty"],
                "classes": dict(v["classes"]),
            }
            for s, v in stats.items()
        },
    }
    (ROOT / "runs" / "inf").mkdir(parents=True, exist_ok=True)
    (ROOT / "runs" / "inf" / "vehicle_det_split.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, indent=2))
    print(f"\nwrote {yaml_path}")


if __name__ == "__main__":
    main()
