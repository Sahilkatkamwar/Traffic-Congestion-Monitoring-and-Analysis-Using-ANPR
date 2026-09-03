"""Vehicle type from a crop, for the types COCO does not have.

The detector is a COCO model and COCO has no autorickshaw. On Indian traffic
that is not a small gap: an auto is one of the most common vehicles on the road
and the detector calls it a car, or a truck, depending on the angle. Measured on
footage/session01/20260901_152733.mp4 before this existed, every autorickshaw in
the clip was written as `car` or `truck`.

Retraining the detector is the wrong fix and TRAINING.md says so: it needs box
annotations nobody has, and single-class fine-tuning on a handful of classes
causes catastrophic forgetting -- you gain autos and lose cars. So the detector
keeps doing what it is good at, finding and tracking vehicles, and the crop it
produces is classified separately by a small model trained on exactly the five
types this project cares about.

The classifier runs once per track, on the single best view, not once per frame.
A track is one vehicle and one vehicle has one type; classifying every frame
would cost thirty times as much to answer the same question.

A null models.vehicle_classes is legitimate and means the fine-tune has not been
trained yet. `load()` returns None, the worker keeps the detector's own COCO
label, and nothing else changes -- the same contract every other model entry in
settings.yaml has.
"""

import os

os.environ.setdefault("YOLO_AUTOINSTALL", "false")

from ultralytics import YOLO  # noqa: E402

from app import config  # noqa: E402

# The vehicle_type values in the frozen data contract. The classifier's own
# class names come from the training folder names, and they are checked against
# this on load rather than trusted: a model that answers "bike" writes "bike"
# into a column specified to hold "motorcycle", and nothing downstream would
# notice until a filter silently matched nothing.
CONTRACT_TYPES = {"auto", "car", "motorcycle", "bus", "truck", "unknown"}

# Folder names that mean a contract type under another name. Kept small and
# explicit: this is a spelling table, not a place to map a new class onto an
# old one.
ALIASES = {
    "bike": "motorcycle",
    "motorbike": "motorcycle",
    "motorcycle": "motorcycle",
    "autorickshaw": "auto",
    "rickshaw": "auto",
    "auto": "auto",
    "lorry": "truck",
}


class VehicleClassifier:
    """One loaded classification model. One per worker process, never shared."""

    def __init__(self, weights):
        self.model = YOLO(str(weights))
        self.imgsz = int(config.default("vehicle_cls_imgsz", 128))
        self.device = config.default("device", 0)
        self.min_conf = float(config.default("vehicle_cls_conf", 0.55))
        names = self.model.names
        self.names = {
            index: ALIASES.get(str(name).lower(), str(name).lower())
            for index, name in (
                names.items() if isinstance(names, dict) else enumerate(names)
            )
        }
        unknown = sorted(set(self.names.values()) - CONTRACT_TYPES)
        if unknown:
            raise ValueError(
                f"{weights.name} classifies {unknown}, which are not vehicle_type "
                f"values in the data contract ({sorted(CONTRACT_TYPES)}). Either "
                f"the wrong weights are configured or a training folder is "
                f"misnamed; add a spelling to app/classify.py ALIASES only if the "
                f"class really is one of the contract types under another name."
            )

    @classmethod
    def load(cls):
        """The configured classifier, or None if there is not one yet.

        A missing file is an error and a null entry is not. The difference
        matters: "not trained yet" is a normal state of this project, and
        "settings.yaml points at a file that is not there" is a mistake that
        should be said out loud rather than silently degraded past.
        """
        weights = config.model_path("vehicle_classes")
        if weights is None:
            return None
        if not weights.exists():
            raise FileNotFoundError(
                f"Vehicle classifier weights not found at {weights}. Set "
                f"models.vehicle_classes to null in config/settings.yaml to run "
                f"with the detector's own COCO labels instead."
            )
        return cls(weights)

    def classify(self, crop):
        """(type, confidence) for one vehicle crop, or (None, 0.0).

        None means "no opinion" and the caller keeps whatever the detector
        said. That happens when the crop is unusable or when the model is not
        confident enough -- a coin-flip between auto and car is worse than the
        detector's answer, because at least the detector's is consistent.
        """
        if crop is None or crop.size == 0:
            return None, 0.0
        result = self.model(
            crop, imgsz=self.imgsz, device=self.device, verbose=False
        )[0]
        probs = getattr(result, "probs", None)
        if probs is None:
            return None, 0.0
        index = int(probs.top1)
        confidence = float(probs.top1conf)
        if confidence < self.min_conf:
            return None, confidence
        return self.names.get(index, "unknown"), confidence
