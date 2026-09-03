"""Vehicle detection and tracking.

The model is loaded once per worker process and reused for every frame. Loading
per frame is the single easiest way to make this pipeline unusable.

Ultralytics will pip-install missing extras behind your back when it hits one.
The environment here is fixed, so turn that off before the import: a missing
dependency must fail loudly, not silently mutate the venv.
"""

import os

os.environ.setdefault("YOLO_AUTOINSTALL", "false")

from ultralytics import YOLO  # noqa: E402

from app import config  # noqa: E402

# COCO ids -> the vehicle_type values in the data contract. COCO has no
# autorickshaw; that gap is closed by the classifier from TRAINING.md, which
# lands as a config path and nothing else.
COCO_TO_TYPE = {
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
}

# The same four as names. A detector fine-tuned on data/vehicle_det emits four
# classes numbered 0-3, not COCO's 2/3/5/7, so an id-keyed map would silently
# turn every car into "unknown" and every filter into the wrong classes the
# moment models.vehicle_detector points at a fine-tune. Names are what the two
# actually share, so the mapping is built from the loaded model's own names and
# the config's COCO ids are used only as the fallback for a model that has none.
VEHICLE_TYPES = frozenset(COCO_TO_TYPE.values())


class VehicleDetector:
    """One loaded YOLO model plus its tracker state.

    Tracking state lives inside the model, so one instance follows exactly one
    stream. Never share an instance between sources or processes.
    """

    def __init__(self):
        weights = config.model_path("vehicle_detector")
        if weights is None or not weights.exists():
            raise FileNotFoundError(
                f"Vehicle detector weights not found at {weights}. "
                f"Check models.vehicle_detector in config/settings.yaml."
            )
        self.model = YOLO(str(weights))
        self.imgsz = config.default("imgsz", 640)
        self.conf = config.default("conf", 0.35)
        self.iou = config.default("iou", 0.5)
        self.device = config.default("device", 0)
        self.tracker = config.default("tracker", "bytetrack.yaml")
        self.id_to_type, self.classes = self._class_map(
            list(config.default("vehicle_classes", sorted(COCO_TO_TYPE)))
        )

    def _class_map(self, configured):
        """(class id -> vehicle_type, ids to ask the model for).

        Built from the loaded model's own `names`, so an 80-class COCO model
        and a four-class fine-tune of the same four categories both come out
        with the same four vehicle_type strings. `configured` is
        defaults.vehicle_classes and stays authoritative for a COCO model --
        it is how the app is told which COCO categories count as a vehicle --
        but it cannot apply to a model whose ids mean something else, and
        passing COCO's [2, 3, 5, 7] to a four-class model asks it for classes
        5 and 7 that do not exist while filtering out car and motorcycle.
        """
        names = getattr(self.model, "names", None) or {}
        mapped = {
            int(i): str(n).lower()
            for i, n in names.items()
            if str(n).lower() in VEHICLE_TYPES
        }
        if not mapped:
            # A model with no usable names: fall back to the COCO assumption
            # the app shipped with rather than detecting nothing at all.
            return dict(COCO_TO_TYPE), list(configured)
        wanted = sorted(mapped)
        if set(wanted) == set(COCO_TO_TYPE):
            # A COCO model. The configured list is a real setting here, so it
            # wins -- someone may have narrowed it deliberately.
            wanted = sorted(set(configured) & set(mapped)) or wanted
        print(
            f"[detect] {len(names)}-class model; tracking "
            + ", ".join(f"{i}:{mapped[i]}" for i in wanted)
        )
        return mapped, wanted

    def track(self, frame):
        """Detections for one frame, in order.

        persist=True keeps tracker state across calls, which is the whole point
        -- but it only means anything if frames arrive sequentially from one
        stream. Batching or shuffling breaks track continuity without erroring.

        Detections with no track id are dropped: a sighting is a track, and a
        box the tracker has not committed to yet is not one.
        """
        result = self.model.track(
            frame,
            persist=True,
            tracker=self.tracker,
            classes=self.classes,
            imgsz=self.imgsz,
            conf=self.conf,
            iou=self.iou,
            device=self.device,
            verbose=False,
        )[0]

        boxes = result.boxes
        if boxes is None or boxes.id is None:
            return []

        out = []
        xyxy = boxes.xyxy.cpu().numpy()
        ids = boxes.id.cpu().numpy().astype(int)
        clss = boxes.cls.cpu().numpy().astype(int)
        confs = boxes.conf.cpu().numpy()
        for box, track_id, cls_id, conf in zip(xyxy, ids, clss, confs):
            out.append(
                {
                    "track_id": int(track_id),
                    "box": tuple(float(v) for v in box),
                    "vehicle_type": self.id_to_type.get(int(cls_id), "unknown"),
                    "conf": float(conf),
                }
            )
        return out
