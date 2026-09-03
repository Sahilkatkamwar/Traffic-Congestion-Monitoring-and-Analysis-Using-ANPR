"""Fine-tune 4 -- the vehicle detector, on data/vehicle_det's road-scene frames.

    python scripts/prep_vehicle_det.py            # build the split first
    python scripts/train_vehicle_det.py --epochs 80

Starts from `models/yolo11n.pt` -- the COCO weights the app runs today -- and
writes a **candidate** to `models/finetuned/vehicle_det_road.pt`. It does not
touch `config/settings.yaml`. Adoption is a separate decision made on the real
five-clip benchmark, never on the mAP printed here.

## What this data is, and what that means for the result

`data/vehicle_det` is 216 frames of **one fixed elevated camera** ("LIVE FROM
FIRST FOOT OVER BRIDGE NEAR VHS HOSPITAL", 2015-12-18), sampled about every 2.4
seconds across roughly nine minutes. One scene, one viewpoint, one time of day,
one weather. The labels are pseudo-labels from YOLO11-L + RT-DETR-L.

Three consequences, all of which shape the run below:

1. **The split is temporal, not random.** Built by `scripts/prep_vehicle_det.py`.
2. **The ceiling is the pseudo-labeller, not the truth.** Trained against
   YOLO11-L's opinions, the best this can do is imitate YOLO11-L on this camera.
3. **Catastrophic forgetting is the risk that matters.** The app's footage is
   handheld street-level phone video; this data is none of that. A fine-tune
   that fits 172 overhead frames can forget how a car looks from the pavement,
   and the only instrument that can see that happen is the five-clip benchmark.

`--freeze` exists for exactly that risk: freezing the backbone keeps COCO's
features and retrains only the head, which is the conservative option when the
fine-tuning domain is this narrow.

The class ids are the dataset's, in COCO's order -- 0 car, 1 motorcycle, 2 bus,
3 truck -- so `app/detect.py` maps a fine-tuned model's ids back to COCO by
position and the rest of the pipeline is unchanged.

Nothing here imports from app/.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPLIT = ROOT / "data" / "vehicle_det_split"
RUNS = ROOT / "runs"
BASE_WEIGHTS = ROOT / "models" / "yolo11n.pt"
OUT = ROOT / "models" / "finetuned" / "vehicle_det_road.pt"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data", type=Path, default=SPLIT / "vehicle_det.yaml")
    parser.add_argument("--base", type=Path, default=BASE_WEIGHTS)
    parser.add_argument("--epochs", type=int, default=80)
    parser.add_argument("--imgsz", type=int, default=640,
                        help="must match defaults.imgsz in config/settings.yaml")
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument("--lr", type=float, default=0.002,
                        help="low: this is a fine-tune off COCO, not a fresh run")
    parser.add_argument("--freeze", type=int, default=0,
                        help="freeze the first N layers; 10 keeps the backbone")
    parser.add_argument("--patience", type=int, default=25)
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--name", default=None)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)

    # A --out given relative to the cwd is still a project path: resolve it
    # against ROOT so the summary can name it and a run from anywhere lands in
    # the same place.
    args.out = args.out if args.out.is_absolute() else (ROOT / args.out)
    args.data = args.data if args.data.is_absolute() else (ROOT / args.data)

    if not args.data.exists():
        raise SystemExit(
            f"No dataset yaml at {args.data}. Run scripts/prep_vehicle_det.py first."
        )

    import os

    os.environ.setdefault("YOLO_AUTOINSTALL", "false")
    from ultralytics import YOLO

    name = args.name or f"vehicle_det_f{args.freeze}_e{args.epochs}"
    print("=== fine-tune " + "=" * 58)
    print(f"  base     {args.base}")
    print(f"  data     {args.data}")
    print(f"  epochs   {args.epochs}  imgsz {args.imgsz}  batch {args.batch}")
    print(f"  lr0      {args.lr}  freeze {args.freeze}  seed {args.seed}")

    model = YOLO(str(args.base))
    model.train(
        data=str(args.data),
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device=0,
        lr0=args.lr,
        freeze=args.freeze or None,
        patience=args.patience,
        seed=args.seed,
        project=str(RUNS),
        name=name,
        exist_ok=True,
        pretrained=True,
        verbose=False,
        plots=False,
    )
    best = Path(model.trainer.best)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(best, args.out)
    print(f"\n  best weights -> {args.out.relative_to(ROOT).as_posix()}")

    tuned = YOLO(str(args.out))
    val = tuned.val(data=str(args.data), imgsz=args.imgsz, device=0, verbose=False)
    summary = {
        "base": str(args.base.relative_to(ROOT).as_posix()),
        "data": str(args.data.relative_to(ROOT).as_posix()),
        "epochs": args.epochs,
        "imgsz": args.imgsz,
        "batch": args.batch,
        "lr0": args.lr,
        "freeze": args.freeze,
        "seed": args.seed,
        "run_dir": str(Path(model.trainer.save_dir).relative_to(ROOT).as_posix()),
        "out": str(args.out.relative_to(ROOT).as_posix()),
        "ultralytics_val": {
            "mAP50": float(val.box.map50),
            "mAP50_95": float(val.box.map),
            "per_class_mAP50": {
                tuned.names[int(c)]: float(v)
                for c, v in zip(val.box.ap_class_index, val.box.ap50)
            },
        },
    }
    (RUNS / "inf").mkdir(parents=True, exist_ok=True)
    path = RUNS / "inf" / f"{name}_train.json"
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary["ultralytics_val"], indent=2))
    print(f"  saved {path.relative_to(ROOT).as_posix()}")
    print(
        "\n  This mAP says the model learned this camera. It does NOT say the app\n"
        "  got better. Run scratch/bench_realvideo.py before adopting anything.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
