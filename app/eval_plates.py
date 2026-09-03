"""Plate accuracy before and after correction. The P3 exit criterion.

    python -m app.eval_plates                       # score data/ocr/test
    python -m app.eval_plates --dir X --labels Y     # any labelled crop folder
    python -m app.eval_plates -v                     # print every miss

Four rows, each a real number from the same labelled crops:

    1. raw OCR                 what the model returned, untouched
    2. + grammar correction    P3's position-aware fix applied to row 1
    3. + multi-frame voting    the reads of one vehicle fused, then corrected
    4. + fuzzy matching        does the correct plate come back as the top hit

Rows 1 and 2 are the before and after the phase is judged on. Row 3 is there
because voting and correction are not independent -- voting repairs dropped
characters that correction alone is required to refuse -- and reporting
correction without it understates what the pipeline does end to end. Row 4
measures matching.py, which is the part of P3 a user actually touches: an
investigator does not need the read to be right, only the search to find the
vehicle.

This lives in app/ rather than scripts/ deliberately. TRAINING.md forbids
scripts/ importing from app/, and every row below is app code being measured --
scripts/eval_ocr.py stays the training track's scorer and keeps its independence.

Accuracy means nothing without held-out labels. Point --dir at hand-labelled
crops of your own footage or this prints an honest refusal, not a number.
"""

import argparse
import sys
from collections import OrderedDict
from pathlib import Path

from app import grammar, matching
from app.config import ROOT
from app.ocr import _load_recognizer, vote

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


# ---------------------------------------------------------------------- data


def load_pairs(image_dir, labels_path, limit=None):
    """[(path, PLATE)] for every labelled crop present on disk, plus problems.

    Same 'filename<TAB>PLATE' format the training track uses, so one labelled
    folder serves both scorers.
    """
    problems = []
    if not labels_path.exists():
        return [], [f"no label file at {labels_path}"]

    labels = OrderedDict()
    for number, line in enumerate(
        labels_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t") if "\t" in line else line.split(None, 1)
        if len(parts) != 2:
            problems.append(f"{labels_path.name}:{number}: expected 'file<TAB>PLATE'")
            continue
        name, plate = parts[0].strip(), grammar.normalize(parts[1])
        if not plate:
            problems.append(f"{labels_path.name}:{number}: no plate on that line")
            continue
        labels[name] = plate

    pairs, missing = [], []
    for name, plate in labels.items():
        path = image_dir / name
        if path.exists():
            pairs.append((path, plate))
        else:
            missing.append(name)
    if missing:
        problems.append(
            f"{len(missing)} labelled crop(s) missing from {image_dir}, "
            f"e.g. {', '.join(missing[:3])}"
        )
    unlabelled = [
        p.name
        for p in sorted(image_dir.glob("*"))
        if p.suffix.lower() in IMAGE_SUFFIXES and p.name not in labels
    ]
    if unlabelled:
        problems.append(
            f"{len(unlabelled)} crop(s) in {image_dir.name}/ have no label and were "
            f"skipped, e.g. {', '.join(unlabelled[:3])}"
        )
    return (pairs[:limit] if limit else pairs), problems


# --------------------------------------------------------------------- score


def levenshtein(a, b):
    """Plain edit distance, for the character-accuracy column only.

    Deliberately not matching.confusion_distance: this column reports how wrong
    a string is, and weighting the errors we are good at forgiving would flatter
    the result.
    """
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


def score(predictions, truths):
    """Plate-level exact match, with character accuracy beside it.

    Exact match is the number that matters -- a plate one character wrong is the
    wrong vehicle. Character accuracy moves first and shows a change working
    before exact match registers it.
    """
    exact = sum(1 for p, t in zip(predictions, truths) if p == t)
    chars = sum(max(len(t) - levenshtein(p, t), 0) for p, t in zip(predictions, truths))
    total = sum(len(t) for t in truths)
    return {
        "n": len(truths),
        "exact": exact,
        "plate": exact / len(truths) if truths else 0.0,
        "chars": chars / total if total else 0.0,
    }


def row(label, result, note=""):
    """One table line. A missing number prints as -- and never as 0.0%."""
    if result is None:
        return f"  {label:<34s} {'--':>7s} {'--':>8s}   {note}"
    chars = result.get("chars")
    chars_cell = "--" if chars is None else f"{chars:.1%}"
    return (
        f"  {label:<34s} {result['plate']:>7.1%} {chars_cell:>8s}   "
        f"{result['exact']}/{result['n']}  {note}"
    )


# ----------------------------------------------------------------------- run


def read_all(recognizer, pad_char, paths, batch=32):
    """OCR every crop. Returns [(text, char_probs)] in input order.

    Padding is kept, because vote() needs the fixed-width form to line reads up
    by slot. It is stripped for the per-crop rows.
    """
    import cv2
    import numpy as np

    out = []
    for start in range(0, len(paths), batch):
        chunk = paths[start : start + batch]
        images, usable = [], []
        for index, path in enumerate(chunk):
            image = cv2.imread(str(path))
            if image is None:
                images.append(None)
                continue
            images.append(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
            usable.append(index)
        results = [("", np.zeros(0))] * len(chunk)
        if usable:
            predictions = recognizer.run(
                [images[i] for i in usable],
                return_confidence=True,
                remove_pad_char=False,
            )
            for index, prediction in zip(usable, predictions):
                results[index] = (
                    prediction.plate,
                    np.asarray(prediction.char_probs).ravel(),
                )
        out.extend(results)
    return out


def strip(text, pad_char):
    return grammar.normalize(text.replace(pad_char, ""))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dir", type=Path, help="folder of labelled plate crops")
    parser.add_argument("--labels", type=Path, help="the matching label file")
    parser.add_argument("--split", default="test", help="test (default), val, train")
    parser.add_argument("--limit", type=int, help="score only the first N crops")
    parser.add_argument("--batch", type=int, default=32)
    parser.add_argument("-v", "--verbose", action="store_true", help="print every miss")
    args = parser.parse_args(argv)

    image_dir = args.dir or (ROOT / "data" / "ocr" / args.split)
    labels_path = args.labels or (
        args.dir.parent / "labels.txt"
        if args.dir and (args.dir.parent / "labels.txt").exists()
        else ROOT / "data" / "ocr" / f"{args.split}_labels.txt"
    )

    pairs, problems = load_pairs(image_dir, labels_path, args.limit)
    print(f"\nPlate accuracy -- {image_dir}")
    for problem in problems:
        print(f"  note: {problem}")

    if not pairs:
        print(
            f"\n  Nothing to score, and no number is better than a made-up one.\n\n"
            f"  This needs plate crops in {image_dir} and one line per crop in\n"
            f"  {labels_path}, tab separated:\n\n"
            f"      000001.jpg\\tMH12AB1234\n\n"
            f"  The app writes every plate crop it finds to crops/plates/<source_id>/.\n"
            f"  Those are the images to label -- uppercase A-Z and 0-9, no spaces.\n"
        )
        return 1

    paths = [path for path, _ in pairs]
    truths = [plate for _, plate in pairs]
    vocabulary = sorted(set(truths))
    print(f"  {len(pairs)} labelled crop(s), {len(vocabulary)} distinct plate(s)\n")
    if len(vocabulary) < 20:
        print(
            f"  warning: {len(vocabulary)} distinct plate(s) is a smoke test, not an\n"
            f"  accuracy measurement. TRAINING.md asks for 200-300 from your own\n"
            f"  footage before these numbers mean anything.\n"
        )

    recognizer, pad_char, _slots = _load_recognizer()
    reads = read_all(recognizer, pad_char, paths, args.batch)

    print(f"  {'':<34s} {'plate':>7s} {'chars':>8s}")

    # Row 1 -- what the model said.
    raw = [strip(text, pad_char) for text, _ in reads]
    raw_score = score(raw, truths)
    print(row("1. raw OCR", raw_score))

    # Row 2 -- the same strings through the grammar.
    corrected = [grammar.apply(text) or "" for text in raw]
    corrected_score = score(corrected, truths)
    delta = corrected_score["plate"] - raw_score["plate"]
    print(row("2. + grammar correction", corrected_score, f"{delta:+.1%}"))

    # Row 3 -- the reads of one vehicle fused before correcting. In the running
    # app a track supplies that grouping; here the label does, which is the same
    # grouping arrived at from the other side.
    groups = OrderedDict()
    for (text, probs), truth in zip(reads, truths):
        groups.setdefault(truth, []).append((text, probs))
    voted, voted_truths = [], []
    for truth, group in groups.items():
        result = vote(group, pad_char=pad_char)
        voted.append(grammar.apply(result["plate_raw"]) if result else "")
        voted_truths.append(truth)
    voted_score = score(voted, voted_truths)
    print(
        row(
            "3. + multi-frame voting",
            voted_score,
            f"per vehicle, {len(groups)} group(s)",
        )
    )

    # Row 4 -- matching. The read does not have to be right for the search to
    # find the vehicle, and this is the number the Trace screen lives on.
    #
    # Ranking against a one-plate vocabulary is not a measurement: there is
    # nothing to rank, and every read scores 100% by having nowhere else to go.
    # A row that cannot be wrong is not printed as if it could have been.
    if len(vocabulary) < 2:
        print(row("4. + fuzzy matching", None, "needs 2+ distinct plates to rank"))
    else:
        found = 0
        for prediction, truth in zip(corrected, truths):
            best = max(
                vocabulary, key=lambda plate: matching.similarity(prediction, plate)
            )
            if best == truth:
                found += 1
        print(
            row(
                "4. + fuzzy matching",
                {
                    "plate": found / len(truths),
                    "chars": None,
                    "exact": found,
                    "n": len(truths),
                },
                f"top-1 over {len(vocabulary)} known plate(s)",
            )
        )

    misses = [
        (path.name, truth, before, after)
        for (path, _), truth, before, after in zip(pairs, truths, raw, corrected)
        if after != truth
    ]
    fixed = [
        (path.name, truth, before)
        for (path, _), truth, before, after in zip(pairs, truths, raw, corrected)
        if before != truth and after == truth
    ]
    print(f"\n  correction fixed {len(fixed)}, {len(misses)} still wrong")
    for name, truth, before in fixed[: None if args.verbose else 5]:
        print(f"    fixed  {name:<22s} {before:<12s} -> {truth}")
    shown = misses if args.verbose else misses[:5]
    for name, truth, before, after in shown:
        note = "" if after == before else f" (was {before})"
        print(f"    miss   {name:<22s} want {truth:<12s} got {after or '(empty)'}{note}")
    if not args.verbose and len(misses) > 5:
        print(f"    ... {len(misses) - 5} more, use -v")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
