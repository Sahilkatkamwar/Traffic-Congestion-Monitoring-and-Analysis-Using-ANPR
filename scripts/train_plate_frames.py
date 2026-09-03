"""Fine-tune the plate detector on data/plate_frames_split.

    python scripts/train_plate_frames.py --data data/plate_frames_split \
        --name plate_frames --out models/finetuned/plate_det_frames.pt

Starts from the CURRENT PRODUCTION weights, models/finetuned/plate_det_scenes.pt,
not from the pretrained model. That model's one property worth protecting is
that it returns nothing on the plate-less control clip, and the honest way to
ask whether new data improves it is to continue it rather than to rebuild it.

The split is built by scripts/prep_plate_frames.py, which documents every source
it drops and why. mAP on the held-out sources is printed because it is cheap,
but it never decides anything here: the labels it scores against are the same
pseudo-labels the training set uses, and the decision is made on real video by
scratch/bench_realvideo.py. See TRAINING.md.

Nothing here imports from app/.
"""

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("YOLO_AUTOINSTALL", "false")

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "models" / "finetuned" / "plate_det_scenes.pt"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", type=Path, default=ROOT / "data" / "plate_frames_split")
    ap.add_argument("--base", type=Path, default=BASE)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--lr", type=float, default=0.001)
    ap.add_argument("--freeze", type=int, default=0)
    ap.add_argument("--seed", type=int, default=1337)
    args = ap.parse_args(argv)

    from ultralytics import YOLO

    yaml_path = args.data / "data.yaml"
    if not yaml_path.exists():
        print("no split at %s -- run scripts/prep_plate_frames.py first" % yaml_path)
        return 1
    print("base    %s" % args.base)
    print("data    %s" % yaml_path)
    print("epochs %d  imgsz %d  batch %d  lr %g  freeze %d"
          % (args.epochs, args.imgsz, args.batch, args.lr, args.freeze))

    model = YOLO(str(args.base))
    model.train(
        data=str(yaml_path), epochs=args.epochs, imgsz=args.imgsz,
        batch=args.batch, lr0=args.lr, freeze=args.freeze or None,
        device=0, seed=args.seed, project=str(ROOT / "runs" / "plate_det"),
        name=args.name, exist_ok=True, verbose=True, plots=False,
        # The split is source-disjoint and small; heavy geometric augmentation
        # is what stands in for the viewpoints it does not contain.
        degrees=5.0, translate=0.1, scale=0.4, fliplr=0.5, mosaic=1.0,
    )
    best = ROOT / "runs" / "plate_det" / args.name / "weights" / "best.pt"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(best.read_bytes())
    print("\nsaved %s" % args.out)

    metrics = YOLO(str(args.out)).val(data=str(yaml_path), split="test",
                                      imgsz=args.imgsz, device=0, verbose=False)
    print("held-out test  mAP50 %.3f  mAP50-95 %.3f  P %.3f  R %.3f" % (
        metrics.box.map50, metrics.box.map, metrics.box.mp, metrics.box.mr))
    return 0


if __name__ == "__main__":
    sys.exit(main())
