"""Fine-tune 1 -- the vehicle type classifier, on folder-labelled crops.

    python scripts/train_vehicle_cls.py --audit          # count the data, train nothing
    python scripts/train_vehicle_cls.py --epochs 30      # audit, train, score

Input is data/vehicle_cls/, whose folder names are the labels. There are no
annotation files and none are needed: this is a classifier, not a detector.

## Why a classifier and not a detector fine-tune

COCO has no autorickshaw. On Indian traffic that is not a small gap -- the
detector calls an auto a car, or a truck, depending on the angle. The fix is
NOT to retrain the COCO detector: that needs vehicle boxes, which do not exist
anywhere in this project, and single-class fine-tuning trades cars for autos.

So the detector keeps finding and tracking vehicles, and the crop it produces is
classified separately by a small model trained on exactly the five types the
data contract names. app/classify.py is the consumer.

## What is scored, and why twice

Ultralytics prints top-1 accuracy. Top-1 accuracy over an unbalanced val set
hides the only confusion that matters here, auto against car, so this script
computes the confusion matrix itself and prints per-class precision, recall and
F1 from it.

It scores twice:

  argmax          every crop gets the model's best guess. This is the model.
  conf >= floor   below defaults.vehicle_cls_conf the classifier abstains and
                  the detector's COCO label stands. This is what ships, and its
                  abstention rate is a number worth knowing before deployment.

Nothing here imports from app/. The confidence floor is read out of
config/settings.yaml as data, which is a shared file, not shared code.
"""

import argparse
import json
import os
import random
import shutil
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "vehicle_cls"
RUNS = ROOT / "runs"
BASE_WEIGHTS = ROOT / "models" / "yolo11n-cls.pt"
OUT = ROOT / "models" / "finetuned" / "vehicle_cls_best.pt"

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

# The vehicle_type values the frozen data contract allows. A folder named
# anything else is a mistake worth stopping for: app/classify.py refuses to
# load a model whose classes are not contract types, so training one would
# waste the GPU time and fail at the end.
CONTRACT_TYPES = {"auto", "car", "motorcycle", "bus", "truck", "unknown"}


def images_in(directory):
    return sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )


def audit(data):
    """Class counts per split, and every reason training would be wrong."""
    splits = {}
    for split in ("train", "val"):
        directory = data / split
        if not directory.is_dir():
            raise SystemExit(
                f"No {split} split at {directory}. TRAINING.md's layout is "
                f"data/vehicle_cls/<split>/<class>/."
            )
        splits[split] = {
            folder.name: images_in(folder)
            for folder in sorted(directory.iterdir())
            if folder.is_dir()
        }

    problems = []
    classes = sorted(splits["train"])
    off_contract = sorted(set(classes) - CONTRACT_TYPES)
    if off_contract:
        problems.append(
            f"folders {off_contract} are not vehicle_type values in the data "
            f"contract; app/classify.py will refuse the trained weights"
        )
    missing = sorted(set(classes) - set(splits["val"]))
    if missing:
        problems.append(f"classes with no val images: {missing}")
    for split, folders in splits.items():
        for name, files in folders.items():
            if not files:
                problems.append(f"{split}/{name} is empty")

    print("=== data " + "=" * 71)
    print(f"  {'class':<14s} {'train':>7s} {'val':>7s}")
    for name in classes:
        print(f"  {name:<14s} {len(splits['train'][name]):>7d} "
              f"{len(splits['val'].get(name, [])):>7d}")
    print(f"  {'TOTAL':<14s} {sum(len(v) for v in splits['train'].values()):>7d} "
          f"{sum(len(v) for v in splits['val'].values()):>7d}")
    counts = [len(splits["train"][c]) for c in classes]
    print(f"\n  class balance  {min(counts)}-{max(counts)} per class in train, "
          f"ratio {max(counts) / min(counts):.2f}:1")
    for problem in problems:
        print(f"  PROBLEM  {problem}")
    return splits, classes, problems


# --------------------------------------------------------------------- scoring


def confusion(model, splits, classes, imgsz, device, floor, batch=64):
    """Predict every val image. Returns (matrix, floored, abstained, total).

    matrix[true][pred] counted at argmax, ignoring the floor, plus a second
    matrix holding only the predictions the floor would have kept. Two numbers
    from one pass over the data: the model's accuracy and the shipped
    behaviour's, which are not the same thing and should not be conflated.
    """
    index = {name: i for i, name in enumerate(classes)}
    size = len(classes)
    matrix = [[0] * size for _ in range(size)]
    floored = [[0] * size for _ in range(size)]
    abstained = 0
    total = 0

    for true_name in classes:
        files = splits["val"].get(true_name, [])
        for start in range(0, len(files), batch):
            chunk = [str(p) for p in files[start:start + batch]]
            results = model(chunk, imgsz=imgsz, device=device, verbose=False)
            for result in results:
                probs = getattr(result, "probs", None)
                if probs is None:
                    continue
                predicted = model.names[int(probs.top1)]
                confidence = float(probs.top1conf)
                total += 1
                matrix[index[true_name]][index[predicted]] += 1
                if confidence < floor:
                    abstained += 1
                else:
                    floored[index[true_name]][index[predicted]] += 1
    return matrix, floored, abstained, total


def per_class(matrix, classes):
    """Precision, recall, F1 and support per class, from the matrix alone."""
    rows = []
    for i, name in enumerate(classes):
        tp = matrix[i][i]
        support = sum(matrix[i])
        predicted = sum(matrix[r][i] for r in range(len(classes)))
        precision = tp / predicted if predicted else 0.0
        recall = tp / support if support else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        rows.append({
            "class": name, "precision": precision, "recall": recall,
            "f1": f1, "support": support, "tp": tp,
        })
    return rows


def print_metrics(title, matrix, classes):
    rows = per_class(matrix, classes)
    correct = sum(matrix[i][i] for i in range(len(classes)))
    total = sum(sum(r) for r in matrix)

    print(f"\n--- {title}")
    print(f"  {'class':<14s} {'prec':>7s} {'recall':>7s} {'f1':>7s} {'support':>8s}")
    for r in rows:
        print(f"  {r['class']:<14s} {r['precision']:>7.3f} {r['recall']:>7.3f} "
              f"{r['f1']:>7.3f} {r['support']:>8d}")
    macro_f1 = sum(r["f1"] for r in rows) / len(rows) if rows else 0.0
    weighted_f1 = (
        sum(r["f1"] * r["support"] for r in rows) / total if total else 0.0
    )
    print(f"  {'macro avg':<14s} "
          f"{sum(r['precision'] for r in rows) / len(rows):>7.3f} "
          f"{sum(r['recall'] for r in rows) / len(rows):>7.3f} "
          f"{macro_f1:>7.3f} {total:>8d}")
    print(f"  {'weighted avg':<14s} {'':>7s} {'':>7s} {weighted_f1:>7.3f} {total:>8d}")
    print(f"\n  accuracy  {correct}/{total} = "
          f"{correct / total if total else 0:.1%}")

    print("\n  confusion matrix   rows = truth, columns = prediction")
    header = "".join(f"{c[:6]:>8s}" for c in classes)
    print(f"  {'':<14s}{header}")
    for i, name in enumerate(classes):
        cells = "".join(f"{matrix[i][j]:>8d}" for j in range(len(classes)))
        print(f"  {name:<14s}{cells}")
    return rows, (correct / total if total else 0.0), macro_f1


# ------------------------------------------------------------------------ main


def main():
    parser = argparse.ArgumentParser(
        description="Fine-tune the vehicle type classifier on data/vehicle_cls/."
    )
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--imgsz", type=int, default=128)
    parser.add_argument("--batch", type=int, default=32)
    parser.add_argument("--lr", type=float, default=0.001)
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--device", default=0)
    parser.add_argument("--name", default="vehicle_cls")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--audit", action="store_true",
                        help="print the data audit and stop")
    # Augmentation, added 2026-09-03 and defaulting to Ultralytics' own values
    # so `--epochs 30` still reproduces the run that made vehicle_cls_best.pt.
    #
    # They exist because of a measured shortcut. In data/vehicle_cls every TRAIN
    # class carries its own image-size signature -- bus is capped at 640 and
    # never narrower than 363, truck never exceeds 317 -- while every VAL class
    # except auto is exactly 192x192 png. A model can separate the train classes
    # on scale alone and have nothing left at val. --scale 0.9 makes the crop
    # area vary by an order of magnitude and takes that shortcut away.
    parser.add_argument("--scale", type=float, default=None,
                        help="random-resized-crop scale jitter (0-1)")
    parser.add_argument("--erasing", type=float, default=None,
                        help="random erasing probability")
    parser.add_argument("--degrees", type=float, default=None,
                        help="random rotation, degrees")
    args = parser.parse_args()

    random.seed(args.seed)

    splits, classes, problems = audit(args.data)
    if problems:
        raise SystemExit(
            "\n  Fix the problems above before training. Nothing was trained."
        )
    if args.audit:
        return

    os.environ.setdefault("YOLO_AUTOINSTALL", "false")
    import torch
    from ultralytics import YOLO

    if not torch.cuda.is_available():
        raise SystemExit(
            "CUDA is not available. This project assumes it is; training on CPU "
            "would take hours and is not what the numbers would describe."
        )
    torch.manual_seed(args.seed)

    if not BASE_WEIGHTS.exists():
        raise SystemExit(
            f"Starting weights not found at {BASE_WEIGHTS}. TRAINING.md expects "
            f"the ImageNet yolo11n-cls.pt there."
        )

    hyperparameters = {
        "script": "scripts/train_vehicle_cls.py",
        "data": args.data.relative_to(ROOT).as_posix(),
        "base_weights": BASE_WEIGHTS.name,
        "epochs": args.epochs,
        "imgsz": args.imgsz,
        "batch": args.batch,
        "lr0": args.lr,
        "seed": args.seed,
        "device": args.device,
        "scale": args.scale,
        "erasing": args.erasing,
        "degrees": args.degrees,
        "classes": classes,
        "train_counts": {c: len(splits["train"][c]) for c in classes},
        "val_counts": {c: len(splits["val"].get(c, [])) for c in classes},
    }
    print("\n=== hyperparameters " + "=" * 60)
    for key, value in hyperparameters.items():
        print(f"  {key:<16s} {value}")

    print("\n=== training " + "=" * 67)
    model = YOLO(str(BASE_WEIGHTS))
    model.train(
        data=str(args.data),
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device=args.device,
        lr0=args.lr,
        seed=args.seed,
        deterministic=True,
        project=str(RUNS),
        name=args.name,
        exist_ok=True,
        pretrained=True,
        save_period=1,   # a crash at epoch 24 of 30 must not cost the run
        verbose=False,
        plots=False,
        **{k: v for k, v in (("scale", args.scale), ("erasing", args.erasing),
                             ("degrees", args.degrees)) if v is not None},
    )
    best = Path(model.trainer.best)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(best, args.out)
    print(f"\n  best weights  {best}")
    # --out may be given relative to the project root or absolute; relative_to
    # raises on the first and this line is only a log message.
    saved = args.out if args.out.is_absolute() else (ROOT / args.out)
    print(f"  saved to      {saved}")

    settings = yaml.safe_load(
        (ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")
    )
    floor = float((settings.get("defaults") or {}).get("vehicle_cls_conf", 0.55))

    scored = YOLO(str(args.out))
    # Ultralytics sorts class names alphabetically from the folder names; score
    # against the model's own order rather than the audit's, so a mismatch
    # cannot silently transpose the matrix.
    model_classes = (
        [scored.names[i] for i in sorted(scored.names)]
        if isinstance(scored.names, dict) else list(scored.names)
    )
    if sorted(model_classes) != sorted(classes):
        raise SystemExit(
            f"Trained model classes {model_classes} do not match the data "
            f"folders {classes}."
        )

    matrix, floored, abstained, total = confusion(
        scored, splits, model_classes, args.imgsz, args.device, floor
    )

    print("\n=== validation " + "=" * 65)
    rows, accuracy, macro_f1 = print_metrics("argmax", matrix, model_classes)

    kept = total - abstained
    print(f"\n  abstained  {abstained}/{total} = "
          f"{abstained / total if total else 0:.1%} of crops fall below the "
          f"{floor} floor\n  and keep the detector's COCO label instead")
    floor_rows, floor_accuracy, floor_macro_f1 = print_metrics(
        f"conf >= {floor} (what ships)", floored, model_classes
    )
    print(f"  over the {kept} crops it answered")

    if "auto" in model_classes and "car" in model_classes:
        auto = model_classes.index("auto")
        car = model_classes.index("car")
        print("\n=== the number that matters " + "=" * 52)
        print(f"  auto called car   {matrix[auto][car]}/{sum(matrix[auto])}")
        print(f"  car called auto   {matrix[car][auto]}/{sum(matrix[car])}")

    RUNS.mkdir(exist_ok=True)
    report = {
        "hyperparameters": hyperparameters,
        "classes": model_classes,
        "confidence_floor": floor,
        "argmax": {
            "accuracy": accuracy,
            "macro_f1": macro_f1,
            "per_class": rows,
            "confusion_matrix": matrix,
        },
        "shipped": {
            "accuracy": floor_accuracy,
            "macro_f1": floor_macro_f1,
            "abstained": abstained,
            "answered": kept,
            "total": total,
            "per_class": floor_rows,
            "confusion_matrix": floored,
        },
    }
    path = RUNS / f"{args.name}_metrics.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n  saved {path.relative_to(ROOT).as_posix()}")
    print(
        "\n  To use it: set models.vehicle_classes in config/settings.yaml to\n"
        f"  {args.out.relative_to(ROOT).as_posix()}. Leaving it null keeps the\n"
        "  detector's COCO labels, which is a valid state, not a broken one."
    )


if __name__ == "__main__":
    main()
