"""Plate-level OCR accuracy on a held-out, hand-labelled test set.

Usage, from an activated env\\Scripts\\activate.bat shell:

    python scripts/eval_ocr.py                 # score data/ocr/test
    python scripts/eval_ocr.py --split val     # sanity check on val
    python scripts/eval_ocr.py --limit 200 -v  # first 200, print every miss

This is the training track's scorer. Per TRAINING.md it imports nothing from
app/: the two tracks share files on disk -- config/settings.yaml, the weights,
the dataset -- and never code. That keeps a number produced here from silently
depending on a change made to the running application.

It prints the four rows TRAINING.md asks for. Rows whose ingredients do not
exist yet say so rather than being quietly skipped, because a table with a
missing row is how a fine-tune gets adopted without ever being compared.

Accuracy is only ever reported from data/ocr/test/ -- your own footage, hand
labelled, appearing nowhere in training. The moment a synthetic or public image
lands in there the number stops meaning anything.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("YOLO_AUTOINSTALL", "false")

import yaml  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


# --------------------------------------------------------------------- data


def load_labels(labels_path):
    """filename<TAB>PLATESTRING per line -> {filename: plate}.

    Tolerates spaces instead of a tab, because a hand-maintained file always
    ends up with a few, and silently dropping those lines would quietly shrink
    the test set.
    """
    if not labels_path.exists():
        return {}, [f"missing label file {labels_path}"]

    labels, problems = {}, []
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
        name, plate = parts[0].strip(), parts[1].strip().upper()
        if not plate.isalnum():
            problems.append(f"{labels_path.name}:{number}: {plate!r} is not A-Z0-9")
            continue
        labels[name] = plate
    return labels, problems


def load_layouts(ocr_dir):
    """data/ocr/two_row_layouts.csv -> {(split, file): layout}.

    Which real crops are two-row, and in which of the five layouts, is a
    by-eye judgement that used to be made once and thrown away -- every later
    two-row number had to be re-derived by hand. It is a file now, so a
    two-row score is reproducible rather than remembered.
    """
    import csv

    path = ocr_dir / "two_row_layouts.csv"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as handle:
        return {
            (row["split"], row["file"]): row["layout"]
            for row in csv.DictReader(handle)
        }


def load_pairs(image_dir, labels_path, limit=None):
    """[(path, plate)] for every labelled image that is actually present."""
    labels, problems = load_labels(labels_path)
    pairs, missing = [], []
    for name, plate in sorted(labels.items()):
        path = image_dir / name
        if path.exists():
            pairs.append((path, plate))
        else:
            missing.append(name)
    if missing:
        problems.append(
            f"{len(missing)} labelled image(s) not found in {image_dir}, "
            f"e.g. {', '.join(missing[:3])}"
        )
    unlabelled = [
        p.name
        for p in sorted(image_dir.glob("*"))
        if p.suffix.lower() in IMAGE_SUFFIXES and p.name not in labels
    ]
    if unlabelled:
        problems.append(
            f"{len(unlabelled)} image(s) in {image_dir.name}/ have no label and were "
            f"skipped, e.g. {', '.join(unlabelled[:3])}"
        )
    return (pairs[:limit] if limit else pairs), problems


# -------------------------------------------------------------------- model


def read_settings():
    with (ROOT / "config" / "settings.yaml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def resolve(value):
    if value is None:
        return None
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


class TorchRecognizer:
    """A .pt fine-tune answering the same calls the ONNX recogniser answers.

    train_ocr.py produces PyTorch weights rather than ONNX, because
    fast-plate-ocr's trainer needs keras and torch.onnx.export needs onnx, and
    neither is installed. Wrapping it here keeps `predict` below identical for
    both models -- the two rows of the table have to be produced by the same
    code or they are not comparable.

    Decoding borrows fast_plate_ocr's own resize and postprocess, so a slot and
    a confidence mean the same thing in row 1 and row 2.
    """

    def __init__(self, weights, device):
        import torch
        from fast_plate_ocr.inference.config import PlateConfig

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from train_ocr import PlateNet

        weights = Path(weights)
        config_path = weights.with_name(weights.stem + "_plate_config.yaml")
        if not config_path.exists():
            config_path = weights.with_suffix(".yaml")
        if not config_path.exists():
            raise FileNotFoundError(f"no plate config beside {weights}")

        self.torch = torch
        self.config = PlateConfig.from_yaml(config_path)
        checkpoint = torch.load(weights, map_location="cpu", weights_only=False)
        self.device = torch.device(
            "cuda:0" if device != "cpu" and torch.cuda.is_available() else "cpu"
        )
        self.model = PlateNet(width=float(checkpoint.get("width", 1.0)))
        self.model.load_state_dict(checkpoint["state_dict"])
        self.model.eval().to(self.device)

    def run(self, source, return_confidence=False, remove_pad_char=True, **_):
        import numpy as np
        from fast_plate_ocr.core.process import postprocess_output, resize_image

        frames = [
            resize_image(
                image,
                self.config.img_height,
                self.config.img_width,
                image_color_mode=self.config.image_color_mode,
                keep_aspect_ratio=self.config.keep_aspect_ratio,
                interpolation_method=self.config.interpolation,
                padding_color=self.config.padding_color,
            )
            for image in source
        ]
        batch = np.stack(frames, axis=0).astype(np.uint8)
        with self.torch.no_grad():
            logits = self.model(self.torch.from_numpy(batch).to(self.device))
            probabilities = self.torch.softmax(logits, dim=-1).cpu().numpy()
        return postprocess_output(
            probabilities,
            self.config.max_plate_slots,
            self.config.alphabet,
            pad_char=self.config.pad_char,
            remove_pad_char=remove_pad_char,
            return_confidence=return_confidence,
        )


def build_recognizer(weights, settings):
    """(recognizer, label) for the fine-tune at `weights`, or the pretrained one."""
    from fast_plate_ocr import LicensePlateRecognizer

    device = str((settings.get("defaults") or {}).get("ocr_device", "cpu"))
    if weights is None:
        name = str((settings.get("defaults") or {}).get("ocr_model", "cct-s-v2-global-model"))
        return LicensePlateRecognizer(hub_ocr_model=name, device=device), name

    weights = Path(weights)
    if weights.suffix.lower() == ".pt":
        return TorchRecognizer(weights, device), weights.name

    config = weights.with_name(weights.stem + "_plate_config.yaml")
    if not config.exists():
        config = weights.with_suffix(".yaml")
    if not config.exists():
        raise FileNotFoundError(f"no plate config beside {weights}")
    return (
        LicensePlateRecognizer(
            onnx_model_path=str(weights), plate_config_path=str(config), device=device
        ),
        weights.name,
    )


# -------------------------------------------------------------------- score


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


def score(predictions, truths):
    """Plate-level exact match plus a character-level companion number.

    Exact match is the number that matters -- a plate read with one wrong
    character is the wrong vehicle. Character accuracy is reported beside it
    because it moves before exact match does, and shows a fine-tune making
    progress that exact match has not registered yet.
    """
    exact = sum(1 for p, t in zip(predictions, truths) if p == t)
    chars = sum(max(len(t) - levenshtein(p, t), 0) for p, t in zip(predictions, truths))
    total_chars = sum(len(t) for t in truths)
    return {
        "n": len(truths),
        "exact": exact,
        "plate_accuracy": exact / len(truths) if truths else 0.0,
        "char_accuracy": chars / total_chars if total_chars else 0.0,
    }


def predict(recognizer, paths, batch=32):
    """OCR every image, in batches. Returns plate strings in input order."""
    import cv2

    out = []
    for start in range(0, len(paths), batch):
        chunk = paths[start : start + batch]
        images = []
        for path in chunk:
            image = cv2.imread(str(path))
            if image is None:
                images.append(None)
                continue
            images.append(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
        usable = [i for i, image in enumerate(images) if image is not None]
        results = [""] * len(chunk)
        if usable:
            predictions = recognizer.run([images[i] for i in usable])
            for i, prediction in zip(usable, predictions):
                results[i] = prediction.plate.strip().upper()
        out.extend(results)
    return out


# --------------------------------------------------------------------- main


def row(label, result, note=""):
    if result is None:
        return f"  {label:<38s} {'--':>8s} {'--':>8s}   {note}"
    return (
        f"  {label:<38s} {result['plate_accuracy']:>7.1%} "
        f"{result['char_accuracy']:>8.1%}   {note}"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--split", default="test", help="test (default), val, or train")
    parser.add_argument("--dir", type=Path, help="override the image directory")
    parser.add_argument("--labels", type=Path, help="override the label file")
    parser.add_argument("--limit", type=int, help="score only the first N images")
    parser.add_argument("--batch", type=int, default=32)
    parser.add_argument("-v", "--verbose", action="store_true", help="print every miss")
    parser.add_argument("--save", action="store_true", help="write the result to runs/")
    parser.add_argument("--weights", type=Path,
                        help="score this .pt as row 2 instead of settings.models.ocr, "
                             "so two fine-tunes can be compared on one axis")
    parser.add_argument("--no-baseline", action="store_true",
                        help="skip row 1; the pretrained model runs on CPU and its "
                             "number does not change between fine-tunes")
    parser.add_argument("--by-layout", action="store_true",
                        help="break the result down by single-row vs each two-row "
                             "layout, using data/ocr/two_row_layouts.csv")
    args = parser.parse_args(argv)

    image_dir = args.dir or (ROOT / "data" / "ocr" / args.split)
    labels_path = args.labels or (ROOT / "data" / "ocr" / f"{args.split}_labels.txt")

    settings = read_settings()
    pairs, problems = load_pairs(image_dir, labels_path, args.limit)

    print(f"\nOCR evaluation -- {image_dir.relative_to(ROOT) if ROOT in image_dir.parents else image_dir}")
    for problem in problems:
        print(f"  note: {problem}")

    if not pairs:
        print(
            f"\n  No labelled images to score.\n\n"
            f"  This needs two things that are not there yet:\n"
            f"    1. plate crops in {image_dir}\n"
            f"    2. one line per crop in {labels_path}, tab separated:\n"
            f"         000001.jpg\\tMH12AB1234\n\n"
            f"  TRAINING.md asks for 200-300 plates cut from your OWN footage and\n"
            f"  hand-labelled, appearing nowhere in training. The app writes every\n"
            f"  plate crop it finds to crops/plates/<source_id>/ -- those are the\n"
            f"  images to label. Uppercase A-Z and 0-9 only, no spaces or dashes.\n"
        )
        return 1

    if args.split != "test":
        print(
            f"  warning: scoring '{args.split}', not the held-out test set. "
            f"TRAINING.md reports accuracy only from data/ocr/test/."
        )

    paths = [path for path, _ in pairs]
    truths = [plate for _, plate in pairs]
    print(f"  {len(pairs)} labelled plate(s)\n")
    print(f"  {'':<38s} {'plate':>7s} {'chars':>8s}")

    results = {}

    # Row 1 -- the pretrained fallback, the number a fine-tune has to beat.
    baseline_predictions = None
    if args.no_baseline:
        print(row("1. baseline pretrained", None, "skipped (--no-baseline)"))
    else:
        started = time.monotonic()
        recognizer, name = build_recognizer(None, settings)
        baseline_predictions = predict(recognizer, paths, args.batch)
        results["baseline"] = score(baseline_predictions, truths)
        elapsed = time.monotonic() - started
        print(row(f"1. baseline pretrained ({name})", results["baseline"],
                  f"{elapsed:.1f}s"))

    # Row 2 -- the fine-tune, once TRAINING.md has produced one.
    finetuned = args.weights or resolve((settings.get("models") or {}).get("ocr"))
    tuned_predictions = None
    if finetuned is None:
        print(row("2. fine-tuned OCR", None, "models.ocr is null in settings.yaml"))
    elif not finetuned.exists():
        print(row("2. fine-tuned OCR", None, f"not found at {finetuned}"))
    else:
        recognizer, name = build_recognizer(finetuned, settings)
        tuned_predictions = predict(recognizer, paths, args.batch)
        results["finetuned"] = score(tuned_predictions, truths)
        print(row(f"2. fine-tuned ({name})", results["finetuned"]))

    # Rows 3 and 4 measure app code, and this script may not import from app/.
    # They are printed by the app track's own scorer, over the same labelled
    # crops, so both numbers still come from data/ocr/test.
    both = "run: python -m app.eval_plates"
    print(row("3. + grammar correction", None, both))
    print(row("4. + multi-frame voting (video)", None, both))

    best = tuned_predictions if tuned_predictions is not None else baseline_predictions
    if best is None:
        raise SystemExit("nothing was scored: --no-baseline with no fine-tune to score")

    # The breakdown that decides whether a two-row fix worked. Single-row is the
    # class that must not regress; each two-row layout is scored on its own
    # because a synthetic generator can buy one layout and leave the rest at zero.
    if args.by_layout:
        layouts = load_layouts(ROOT / "data" / "ocr")
        if not layouts:
            print("\n  note: no data/ocr/two_row_layouts.csv, skipping --by-layout")
        else:
            groups = {}
            for (path, _), truth, got in zip(pairs, truths, best):
                layout = layouts.get((args.split, path.name), "single")
                groups.setdefault(layout, []).append((got, truth))
            two_row = [pair for key, rows_ in groups.items() if key != "single"
                       for pair in rows_]
            print(f"\n  {'':<38s} {'plate':>7s} {'chars':>8s}")
            ordered = ["single"] + sorted(k for k in groups if k != "single")
            for key in ordered:
                rows_ = groups.get(key)
                if not rows_:
                    continue
                result = score([g for g, _ in rows_], [t for _, t in rows_])
                results[f"layout_{key}"] = result
                print(row(f"   {key}", result, f"n={result['n']}"))
                if key == "single" and two_row:
                    result = score([g for g, _ in two_row], [t for _, t in two_row])
                    results["layout_two_row_all"] = result
                    print(row("   two-row, all layouts", result, f"n={result['n']}"))

    misses = [(p.name, t, g) for (p, _), t, g in zip(pairs, truths, best) if t != g]
    print(f"\n  {len(misses)} miss(es) of {len(pairs)}")
    if args.verbose:
        for filename, truth, got in misses:
            print(f"    {filename:<28s} want {truth:<12s} got {got or '(empty)'}")
    elif misses:
        for filename, truth, got in misses[:10]:
            print(f"    {filename:<28s} want {truth:<12s} got {got or '(empty)'}")
        if len(misses) > 10:
            print(f"    ... {len(misses) - 10} more, use -v")

    if args.save:
        runs = ROOT / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        out = runs / f"eval_ocr_{time.strftime('%Y%m%d_%H%M%S')}.json"
        out.write_text(
            json.dumps(
                {
                    "split": args.split,
                    "images": str(image_dir),
                    "labels": str(labels_path),
                    "n": len(pairs),
                    "results": results,
                    "misses": misses,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n  wrote {out.relative_to(ROOT)}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
