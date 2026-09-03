"""Build a trainable split from data/vehicle_plate_frames.

    python scripts/prep_plate_frames.py --audit
    python scripts/prep_plate_frames.py
    python scripts/prep_plate_frames.py --complete --out data/plate_frames_split_c

The supplied set is 1007 frames from 14 videos with YOLO plate boxes, produced
by an unnamed detector at conf 0.15 and shipped with the warning "Automatic
predictions are pseudo-labels. Review images/labels before training." They were
reviewed, and three findings decide what can be trained on.

1. ONE SOURCE IS POISON. In "Licence Plate Camera Illustration Video" 163 of its
   176 boxes (93%) sit on the burned-in "IP Camera" watermark in the top-left
   corner, not on a plate -- the labeller boxed it at 0.84 confidence in nearly
   every frame. The whole source is dropped, and a corner-box rule drops the
   three stragglers of the same kind elsewhere.

2. TWO SOURCES CARRY BURNED-IN DETECTION GRAPHICS. "ANPR India Detection Demo -
   SmartCow" renders a cyan box and the read plate string ON the plate it is
   demonstrating. A detector trained on that learns the graphic, not the plate,
   so the source is dropped from training.

3. ONE SOURCE LEAKS INTO THE BENCHMARK. 20260901_152704.mp4 is the same phone,
   the same road and the same minute as footage/session01/20260901_152733.mp4,
   which is a benchmark clip. Dropped.

What survives is 792 frames over 11 sources. The split is SOURCE-DISJOINT: 2 fps
sampling of 30 fps video makes consecutive frames near-copies, so a random split
would put the same vehicle on both sides. Held out whole are one Indian source
and one European CCTV source for test, and two stock-footage sources for val.

## The defect that cannot be cleaned, and what --complete does about it

The labels are RECALL-INCOMPLETE. Asked what they see, the two detectors already
in this repo -- neither of which made these labels -- agree on 268 boxes the
labels do not have, and a 48-crop review of those (scratch/final/unmatched_sheet.jpg)
is overwhelmingly real plates. So at least 268 visible plates, ~14% of the true
total, are labelled as background, and every one teaches a detector to suppress
exactly the plate it should find.

`--complete` adds those agreed boxes, after a plate-shape filter, and says so in
the manifest. It is pseudo-labelling on top of pseudo-labelling and is built as
a separate split so the two can be trained and compared rather than argued about.
Neither is ground truth and neither is called that.

Nothing here imports from app/.
"""

import argparse
import hashlib
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "vehicle_plate_frames"
OUT = ROOT / "data" / "plate_frames_split"

# See the module docstring. Each exclusion is a measurement, not a preference.
DROP_SOURCES = {
    "Licence Plate Camera Illustration Video":
        "163 of 176 boxes are the burned-in 'IP Camera' watermark",
    "ANPR India Detection Demo - SmartCow (1)":
        "burned-in cyan detection graphics drawn on the plates themselves",
    "20260901_152704":
        "same phone session as benchmark clip 20260901_152733.mp4",
}
VAL_SOURCES = {
    "pexels-casey-whalen-6571483 (2160p)",
    "pexels-george-morina-5222550 (2160p)",
}
TEST_SOURCES = {
    "Automatic Number Plate Recognition (ANPR) _ Vehicle Number Plate Recognition (1)",
    "Traffic Control CCTV",
}
# A box this high and this far to the side is a channel logo or a timestamp.
CORNER_Y = 0.12
CORNER_X = 0.20
# Shape sanity for boxes --complete invents. Matches app/ocr.py's own gate.
MIN_ASPECT, MAX_ASPECT = 1.1, 6.5


def sources():
    return [d for d in sorted(SRC.iterdir()) if d.is_dir()]


def is_corner(x, y):
    return y < CORNER_Y and (x < CORNER_X or x > 1.0 - CORNER_X)


def read_label(path):
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) != 5:
            continue
        out.append((int(parts[0]), *(float(v) for v in parts[1:])))
    return out


def split_of(name):
    if name in VAL_SOURCES:
        return "val"
    if name in TEST_SOURCES:
        return "test"
    return "train"


def load_extra(path):
    """Ensemble-agreed boxes the labels lack, as {(source, image): [xyxy, ...]}."""
    if path is None or not Path(path).exists():
        return {}
    out = {}
    for item in json.loads(Path(path).read_text(encoding="utf-8")):
        out.setdefault((item["src"], item["img"]), []).append(item["box"])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--audit", action="store_true", help="report only, write nothing")
    ap.add_argument("--complete", action="store_true",
                    help="add the ensemble-agreed boxes the labels lack")
    ap.add_argument("--extra", type=Path,
                    default=ROOT / "scratch" / "final" / "unmatched.json")
    args = ap.parse_args(argv)

    extra = load_extra(args.extra) if args.complete else {}
    rows = []
    dropped_corner = 0
    for d in sources():
        if d.name in DROP_SOURCES:
            print("  DROP %-52s %s" % (d.name[:51], DROP_SOURCES[d.name]))
            continue
        meta = json.loads((d / "summary.json").read_text(encoding="utf-8"))
        W, H = meta["width"], meta["height"]
        for ip in sorted((d / "images").glob("*.jpg")):
            lp = d / "labels" / (ip.stem + ".txt")
            boxes = []
            for cls, x, y, w, h in read_label(lp):
                if is_corner(x, y):
                    dropped_corner += 1
                    continue
                boxes.append((cls, x, y, w, h))
            added = 0
            # Only ever into train. Adding invented boxes to val or test would
            # make the eval measure agreement with the ensemble that invented
            # them, and the two candidates must be scored on one identical set.
            for x1, y1, x2, y2 in (extra.get((d.name, ip.name), [])
                                   if split_of(d.name) == "train" else []):
                bw, bh = x2 - x1, y2 - y1
                if bh <= 0 or not MIN_ASPECT <= bw / bh <= MAX_ASPECT:
                    continue
                boxes.append((0, (x1 + x2) / 2 / W, (y1 + y2) / 2 / H, bw / W, bh / H))
                added += 1
            rows.append(dict(source=d.name, image=ip, split=split_of(d.name),
                             boxes=boxes, added=added))

    print("\n=== split " + "=" * 62)
    print("%-8s%7s%7s%9s%8s   sources" % ("split", "images", "boxes", "added", "empty"))
    for split in ("train", "val", "test"):
        part = [r for r in rows if r["split"] == split]
        srcs = sorted({r["source"][:26] for r in part})
        print("%-8s%7d%7d%9d%8d   %s" % (
            split, len(part), sum(len(r["boxes"]) for r in part),
            sum(r["added"] for r in part),
            sum(1 for r in part if not r["boxes"]), ", ".join(srcs)))
    print("  corner boxes dropped outside the poisoned source: %d" % dropped_corner)
    print("  source-disjoint: no video appears in more than one split")

    if args.audit:
        return 0

    out = args.out
    if out.exists():
        shutil.rmtree(out)
    for split in ("train", "val", "test"):
        (out / split / "images").mkdir(parents=True, exist_ok=True)
        (out / split / "labels").mkdir(parents=True, exist_ok=True)
    manifest = []
    for r in rows:
        # The source name is folded into the filename: two videos both start at
        # 00000001.jpg and one would silently overwrite the other otherwise.
        slug = hashlib.md5(r["source"].encode("utf-8")).hexdigest()[:8]
        stem = "%s_%s" % (slug, r["image"].stem)
        shutil.copy2(r["image"], out / r["split"] / "images" / (stem + ".jpg"))
        (out / r["split"] / "labels" / (stem + ".txt")).write_text(
            "".join("%d %.6f %.6f %.6f %.6f\n" % b for b in r["boxes"]),
            encoding="utf-8")
        manifest.append(dict(source=r["source"], split=r["split"], stem=stem,
                             boxes=len(r["boxes"]), added=r["added"]))
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    yaml_path = out / "data.yaml"
    yaml_path.write_text(yaml.safe_dump(
        {"path": str(out), "train": "train/images", "val": "val/images",
         "test": "test/images", "names": {0: "plate"}}, sort_keys=False),
        encoding="utf-8")
    print("\n  wrote %s" % yaml_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
