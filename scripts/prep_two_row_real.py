"""Build the plate-disjoint split over data/ocr/two_row_plate_images.

    python scripts/prep_two_row_real.py

Writes data/ocr/two_row_split.csv -- one row per usable crop, carrying the
corrected plate string, where the plate comes from, whether the image is a
photograph or a render, and which side of the split it is on.

## Why the supplied labels are not used

data/ocr/two_row_plate_labels holds one .txt per image. Scored against a hand
transcription of all 500 crops (scratch/tworow4/transcriptions.tsv, read off
the pixels without the supplied labels in view), they are **26.2% correct
overall and 11.3% correct on the 363 photographed crops**. The error pattern --
MH -> MHT, 1 -> T, 0 -> O, truncated tails, whole strings replaced by
fragments like `FHEH` -- is OCR output, not a transcription. Training a
recogniser on them teaches it another recogniser's mistakes.

So the plate string in this file is the hand transcription, and
`supplied_label` is carried beside it so the disagreement stays visible and
auditable. Nothing in data/ocr/two_row_plate_labels is modified.

## The split rule, and why it has to be plate-disjoint

A registration photographed twice is one label seen twice. Splitting by image
would put `KL01BM5673` on both sides of the split five times over and every
two-row number afterwards would be a memorisation score. So:

  * a plate already in data/ocr/val or data/ocr/test can NEVER reach train and
    goes to heldout -- otherwise it would leak into an existing benchmark;
  * a plate already in data/ocr/train goes to train, for the same reason
    read the other way;
  * every other plate is assigned by a hash of its own string, so all its
    crops land together and the assignment is identical on every run.

data/ocr/train, val and test are left byte-identical, so every number measured
before this dataset existed stays comparable.

Nothing here imports from app/.
"""

import argparse
import csv
import hashlib
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OCR = ROOT / "data" / "ocr"
IMAGES = OCR / "two_row_plate_images"
SUPPLIED = OCR / "two_row_plate_labels"
TRANSCRIPTS = ROOT / "scratch" / "tworow4" / "transcriptions.tsv"
INDEX = ROOT / "scratch" / "tworow4" / "index.csv"
OUT = OCR / "two_row_split.csv"

# app/grammar.py's shape, restated rather than imported: the training track
# imports nothing from app/, so a change to the running application can never
# silently change what a training split means.
INDIAN = re.compile(r"^[A-Z]{2}\d{1,2}[A-Z]{0,3}\d{4}$")
BH = re.compile(r"^\d{2}BH\d{4}[A-Z]{1,2}$")
AZERBAIJAN = re.compile(r"^\d{2}[A-Z]{1,2}\d{3}$")
STATES = set(
    "AN AP AR AS BR CG CH DD DL DN GA GJ HP HR JH JK KA KL LA LD MH ML MN MP "
    "MZ NL OD OR PB PY RJ SK TN TR TS UK UP WB CT UA TG".split()
)


def origin_of(plate, note):
    """indian / azerbaijan / nonstandard -- what grammar the string follows."""
    if "azerbaijan" in note:
        return "azerbaijan"
    if BH.match(plate):
        return "indian"
    if INDIAN.match(plate) and plate[:2] in STATES:
        return "indian"
    if AZERBAIJAN.match(plate):
        return "azerbaijan"
    return "nonstandard"


def load_transcripts():
    rows = {}
    for line in TRANSCRIPTS.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith(">"):
            continue
        parts = line.split("\t")
        rows[int(parts[0])] = {
            "plate": parts[1].strip().upper(),
            "keep": parts[2].strip() == "keep",
            "note": parts[3].strip() if len(parts) > 3 else "",
        }
    return rows


def load_split_labels(split):
    """{PLATE} already present in one of the existing data/ocr splits."""
    path = OCR / f"{split}_labels.txt"
    out = Counter()
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split("\t")
        if len(parts) == 2:
            out[parts[1].strip().upper()] += 1
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--heldout-frac", type=float, default=0.30,
                        help="fraction of the NEW plates held out, by hash")
    args = parser.parse_args(argv)

    if not TRANSCRIPTS.exists():
        raise SystemExit(f"no transcription at {TRANSCRIPTS}")

    index = {
        int(row["idx"]): row
        for row in csv.DictReader(INDEX.open(encoding="utf-8"))
    }
    transcripts = load_transcripts()
    if set(index) != set(transcripts):
        raise SystemExit(
            f"index.csv has {len(index)} crops, transcriptions.tsv has "
            f"{len(transcripts)} -- they must describe the same crops"
        )

    existing = {s: load_split_labels(s) for s in ("train", "val", "test")}
    heldout_plates = set(existing["val"]) | set(existing["test"])

    # Decide per PLATE, then apply to every crop of it, so no registration can
    # straddle the split however many times it was photographed.
    kept = {i: t for i, t in transcripts.items() if t["keep"]}
    plates = sorted({t["plate"] for t in kept.values()})
    assignment, reasons = {}, Counter()
    for plate in plates:
        if plate in heldout_plates:
            assignment[plate] = "heldout"
            reasons["already in data/ocr val or test"] += 1
        elif plate in existing["train"]:
            assignment[plate] = "train"
            reasons["already in data/ocr train"] += 1
        else:
            digest = hashlib.sha1(plate.encode("utf-8")).hexdigest()
            frac = int(digest[:8], 16) / 0xFFFFFFFF
            assignment[plate] = "heldout" if frac < args.heldout_frac else "train"
            reasons["assigned by hash of the plate string"] += 1

    rows = []
    for i in sorted(kept):
        plate = kept[i]["plate"]
        note = kept[i]["note"]
        rows.append({
            "file": index[i]["file"],
            "plate": plate,
            "supplied_label": index[i]["supplied"],
            "origin": origin_of(plate, note),
            "source": "rendered" if "rendered" in note else "photo",
            "split": assignment[plate],
            "label_agrees": int(index[i]["supplied"] == plate),
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    excluded = [(i, t) for i, t in transcripts.items() if not t["keep"]]
    print(f"crops on disk          {len(index)}")
    print(f"excluded as unusable   {len(excluded)}")
    for i, t in excluded:
        print(f"    #{i:<4d} {index[i]['file'][:44]:<46s} {t['note']}")
    print(f"usable                 {len(rows)} crops over {len(plates)} plates")
    for reason, count in reasons.most_common():
        print(f"    {count:>4d} plates {reason}")

    print()
    header = f"  {'':<10s} {'crops':>6s} {'plates':>7s} {'indian':>7s} {'azeri':>6s} {'other':>6s} {'photo':>6s} {'render':>7s}"
    print(header)
    for split in ("train", "heldout"):
        group = [r for r in rows if r["split"] == split]
        by = Counter(r["origin"] for r in group)
        src = Counter(r["source"] for r in group)
        print(
            f"  {split:<10s} {len(group):>6d} "
            f"{len({r['plate'] for r in group}):>7d} "
            f"{by['indian']:>7d} {by['azerbaijan']:>6d} {by['nonstandard']:>6d} "
            f"{src['photo']:>6d} {src['rendered']:>7d}"
        )

    agree = sum(r["label_agrees"] for r in rows)
    print(f"\n  supplied labels agreeing with the transcription: "
          f"{agree}/{len(rows)} = {agree / len(rows):.1%}")
    photo = [r for r in rows if r["source"] == "photo"]
    print(f"  on the {len(photo)} photographed crops: "
          f"{sum(r['label_agrees'] for r in photo)}/{len(photo)} = "
          f"{sum(r['label_agrees'] for r in photo) / len(photo):.1%}")
    print(f"\n  wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
