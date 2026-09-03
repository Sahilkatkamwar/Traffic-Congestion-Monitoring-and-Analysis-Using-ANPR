"""Plate detection, OCR, and multi-frame character voting.

A plate is read from a *vehicle crop*, not from the whole frame: the vehicle is
already tracked, so a plate found inside its box belongs to that track without
any extra association step.

One track produces several reads, one per sampled frame, and they disagree --
motion blur turns 8 into B, a dirty screw turns O into Q. `vote` resolves the
disagreement per character position rather than picking a whole string, so a
correct plate can be assembled from reads that were each individually wrong.

Both models load once per worker process. Loading per frame is the single
easiest way to make this pipeline unusable.

Two properties of the OCR model drive the code below:

  * It emits a fixed number of slots -- ten for the pretrained model, eleven
    for the fine-tune -- padded with '_', so slot i means the same character
    position in every read and votes line up without alignment.
  * Padding slots are trivially confident. Averaging confidence over every slot
    makes an empty read score 1.00, so confidence is only ever averaged over
    the slots that survive into the final string.
"""

import os
from pathlib import Path

os.environ.setdefault("YOLO_AUTOINSTALL", "false")

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from ultralytics import YOLO  # noqa: E402

from app import config  # noqa: E402

# Where to cut a candidate two-row plate, as a fraction of its height. Three
# rather than one because the right cut moves with how much bumper the crop
# padding caught, and there is no way to know that in advance: on the known
# MH17CY4718 crop, 0.44 reads MH17CI4418 and 0.56 reads MH17CI4718. Tuning a
# single cut on one plate would be fitting to that plate, so all three are read
# and _choose_rows takes the most confident that parses as a registration.
_TWO_ROW_CUTS = (0.44, 0.50, 0.56)
# Trim before cutting. The crop is padded to recover characters clipped by the
# vehicle box, and that padding is bumper, not plate.
_TWO_ROW_TRIM = 0.06
# Both rows are scaled to this height before being laid side by side. The OCR
# model normalises its input to about 128x32, so matching the row height here
# keeps the two halves at the same scale as each other.
_TWO_ROW_HEIGHT = 32


def preprocess(crop, mode):
    """Normalise one plate crop before OCR. `mode` comes from settings.yaml.

    "greynorm_x2" is greyscale, min-max stretched to the full 0-255 range, then
    doubled with bicubic interpolation. Each part does something specific:

      greyscale   an Indian plate carries its information in stroke contrast,
                  never in hue. The ground colour is the one thing that does
                  vary -- white private, yellow commercial, green electric --
                  so colour is the channel that changes between plates that
                  should read alike, and dropping it removes a nuisance
                  variable rather than information.
      min-max     a faded yellow plate and a bright white one arrive with very
                  different histograms. Stretching each crop to the full range
                  puts them on the same footing.
      x2          the model resizes everything to about 128x32. A 66x35 crop
                  reaching it is stretched in x and squashed in y at once;
                  going through 132x70 first makes that one clean downscale
                  instead of two conflicting ones.

    "none" returns the crop untouched, which is what shipped before this and is
    the fallback for an unrecognised mode -- a typo in settings.yaml must not
    silently change how plates are read.
    """
    if crop is None or crop.size == 0 or mode in (None, "none", False):
        return crop
    if mode not in ("greynorm", "greynorm_x2"):
        return crop
    grey = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    grey = cv2.normalize(grey, None, 0, 255, cv2.NORM_MINMAX)
    out = cv2.cvtColor(grey, cv2.COLOR_GRAY2BGR)
    if mode == "greynorm_x2":
        out = cv2.resize(out, (out.shape[1] * 2, out.shape[0] * 2),
                         interpolation=cv2.INTER_CUBIC)
    return out


class PlateReader:
    """One loaded plate detector plus one loaded OCR model.

    Never share an instance between processes: the YOLO model holds CUDA state
    and the ONNX session holds its own. Each worker builds its own.
    """

    def __init__(self):
        weights = config.model_path("plate_detector")
        if weights is None or not weights.exists():
            raise FileNotFoundError(
                f"Plate detector weights not found at {weights}. "
                f"Check models.plate_detector in config/settings.yaml."
            )
        self.model = YOLO(str(weights))
        self.imgsz = int(config.default("plate_imgsz", 640))
        self.conf = float(config.default("plate_conf", 0.25))
        self.device = config.default("device", 0)
        self.min_width = int(config.default("min_plate_width", 48))
        self.min_vehicle_px = int(config.default("plate_min_vehicle_px", 80))
        self.max_area_frac = float(config.default("max_plate_area_frac", 0.06))
        self.min_area_frac = float(config.default("min_plate_area_frac", 0.0005))
        self.aspect = (
            float(config.default("min_plate_aspect", 1.1)),
            float(config.default("max_plate_aspect", 6.5)),
        )
        self.min_edge_density = float(
            config.default("min_plate_edge_density", 0.22)
        )
        self.min_y_frac = float(config.default("plate_min_y_frac", 0.30))
        self.preprocess_mode = config.default("plate_preprocess", "none")
        self.two_row_split = bool(config.default("two_row_split", False))
        self.two_row_aspect = float(config.default("two_row_aspect", 1.6))
        self.recognizer, self.pad_char, self.slots = _load_recognizer()

    # ------------------------------------------------------------------ detect

    def find(self, vehicle_crop):
        """Best plate box inside one vehicle crop, or None.

        Returns None when there is no plate, or when the plate is too small to
        read. A crop narrower than min_plate_width upscales to mush and the OCR
        model answers confidently anyway, which is worse than not asking.

        The returned `box` is in the vehicle crop's own coordinates. The caller
        maps it back into the frame and cuts the pixels from there, because the
        crop's edge is a wall a plate can be clipped against and the frame's is
        not.

        Four filters stand between the detector and OCR, and all four came from
        measuring the detector's own output on footage/session01 rather than
        from taste. Over 23 hand-labelled detections -- 7 real plates, 16 not:

          area      real plates covered 0.36-3.7% of the vehicle box. Every
                    rear windscreen it mistook for a plate covered 10-15%.
          edges     how much of the box is covered by character strokes. Real
                    plates 0.288 and above, seven of the false positives 0.174
                    and below, nothing in between. Greyscale contrast was tried
                    first and rejects a faded yellow plate outright -- see
                    edge_density().
          position  a plate sits low on a vehicle from every angle. A window
                    does not. The top of the box is not plate territory.
          aspect    unchanged in kind, but the ceiling drops from 10 to 6.5:
                    nothing above that is a plate, and bumper trim is.

        The remaining false positives are the honest ones -- tail-light
        clusters and painted signage, which are genuinely plate-shaped,
        plate-positioned and high contrast. Those need a better detector, not a
        better filter, and that is what the A/B in scratch/p5_det_decide.py and
        the benchmark are for.
        """
        if vehicle_crop is None or vehicle_crop.size == 0:
            return None
        height, width = vehicle_crop.shape[:2]
        # A vehicle this small cannot hold a plate wide enough to read, and the
        # detector call would be pure cost.
        if width < self.min_vehicle_px or height < self.min_vehicle_px // 2:
            return None

        result = self.model(
            vehicle_crop,
            imgsz=self.imgsz,
            conf=self.conf,
            device=self.device,
            verbose=False,
        )[0]
        boxes = result.boxes
        if boxes is None or len(boxes) == 0:
            return None

        xyxy = boxes.xyxy.cpu().numpy()
        confs = boxes.conf.cpu().numpy()

        # Shape before confidence. Given a vehicle crop this detector regularly
        # returns one confident box around the whole crop -- 335x215 inside a
        # 336x252 vehicle, at 0.67 -- and picking the highest confidence first
        # hands OCR a picture of a car. A plate is a small wide rectangle low on
        # a vehicle, so anything that is not is discarded before the best is
        # chosen.
        area = float(width * height)
        best = None
        for box, conf in zip(xyxy, confs):
            x1, y1, x2, y2 = (int(v) for v in box)
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(width, x2), min(height, y2)
            box_w, box_h = x2 - x1, y2 - y1
            if box_w < self.min_width or box_h < 8:
                continue
            aspect = box_w / box_h
            if not self.aspect[0] <= aspect <= self.aspect[1]:
                continue
            share = (box_w * box_h) / area
            if not self.min_area_frac <= share <= self.max_area_frac:
                continue
            # Measured from the centre, so a tall box straddling the middle is
            # judged by where it actually sits rather than by its top edge.
            if ((y1 + y2) / 2.0) / height < self.min_y_frac:
                continue
            patch = vehicle_crop[y1:y2, x1:x2]
            if edge_density(patch) < self.min_edge_density:
                continue
            if best is None or conf > best[0]:
                best = (float(conf), x1, y1, x2, y2)

        if best is None:
            return None
        conf, x1, y1, x2, y2 = best
        return {
            "box": (x1, y1, x2, y2),
            "crop": vehicle_crop[y1:y2, x1:x2].copy(),
            "det_conf": conf,
            "width": x2 - x1,
            # Decided here, from the detected box, and carried with the hit --
            # it cannot be re-derived downstream, because by then the crop has
            # been padded and padding changes the aspect. Off by default; see
            # the note on two_row_split in settings.yaml for why.
            "rows": (
                2
                if self.two_row_split
                and (x2 - x1) / max(1, y2 - y1) <= self.two_row_aspect
                else 1
            ),
        }

    # --------------------------------------------------------------------- ocr

    def read(self, plate_crops, rows=None):
        """OCR a batch of plate crops -> [(slots, probs), ...], aligned to input.

        remove_pad_char stays off: the padded fixed-length form is what makes
        slot i comparable across reads, and the padding is stripped once, after
        voting.

        A two-row plate has to be read a row at a time: the OCR model has only
        ever seen one row, and handed both it reads them superimposed --
        MH17C over Y4718 came back as MH77IVX183. A crop marked as two-row is
        cut in half and the halves are read as separate images, then joined,
        in the same batch and at no extra call.

        `rows` comes from find(), one entry per crop, and is 1 for everything
        unless two_row_split is on -- which by default it is not. Deciding
        which plates are two-row turned out to be the hard part: over the seven
        hand-labelled plates in footage/session01, the one genuine two-row
        plate had a tight box aspect of 1.51 and two single-row plates measured
        1.31 and 1.89, so aspect cannot separate them. An ink-profile test on
        the crop did no better -- the dark bumper above and below a padded
        single-row plate produces exactly the two bands the test looks for.
        Splitting on either rule read MH15HY2237 as PN71A22278MH01B0033, which
        is far worse than reading one two-row plate wrong.

        So the mechanism stays, correct and switchable, and the guess that
        would drive it does not ship. What would earn it back is a row count
        from the detector itself -- a second class -- or an OCR fine-tune that
        has seen two-row plates. Both are training work, and TRAINING.md is
        where they belong.
        """
        if not plate_crops:
            return []
        if rows is None:
            rows = [1] * len(plate_crops)

        # Each crop becomes one or three images: the whole crop always, plus
        # its two halves when a second row is even possible. Both hypotheses
        # are read and the grammar picks between them below -- see _choose_rows.
        parts = []
        for crop, count in zip(plate_crops, rows):
            group = [crop]
            if self.two_row_split and count >= 2:
                for cut in _TWO_ROW_CUTS:
                    stitched = stitch_rows(crop, cut)
                    if stitched is not None:
                        group.append(stitched)
            parts.append(group)

        flat = [image for group in parts for image in group]
        # Preprocessing runs after the two-row rearrangement, not before: the
        # rearrangement cuts and rescales the crop, and normalising twice at
        # two different scales is not the same as normalising once.
        flat = [preprocess(image, self.preprocess_mode) for image in flat]
        # The model config declares rgb input and the library trusts the array
        # it is given. Handing it OpenCV's BGR silently swaps the channels.
        rgb = [cv2.cvtColor(image, cv2.COLOR_BGR2RGB) for image in flat]
        predictions = self.recognizer.run(
            rgb, return_confidence=True, remove_pad_char=False
        )
        reads = [(p.plate, np.asarray(p.char_probs).ravel()) for p in predictions]

        out = []
        at = 0
        for group in parts:
            taken = reads[at:at + len(group)]
            at += len(group)
            if len(taken) == 1:
                out.append(taken[0])
            else:
                out.append(_choose_rows(taken[0], taken[1:], self.pad_char))
        return out

    def _rows_of(self, crop, count):
        """One image, or the two halves of a two-row plate.

        Split slightly above the middle: on an Indian two-row plate the bottom
        row carries the series and the four digits, and it is the taller of the
        two. A clean halving shaves the tops off the characters that matter
        most.
        """
        height = crop.shape[0]
        if count < 2 or height <= 0:
            return [crop]
        cut = int(height * 0.48)
        if cut < 4 or height - cut < 4:
            return [crop]
        return [crop[:cut], crop[cut:]]


def edge_density(patch):
    """Fraction of a candidate box covered by strong edges.

    What separates a plate from the flat blurred rectangles the detector likes
    -- rear windows, panel gaps, shadowed bumpers. A plate is covered in
    character strokes and cannot be legible without them.

    This started out as greyscale standard deviation, which is the obvious
    measurement and is wrong. Over the labelled crops from footage/session01 it
    looked excellent -- real plates 32-78, blurred windows 23-27 -- right up
    until the set included a yellow commercial plate. MP09ZS0907 is faded black
    on faded yellow: it scores 21, below every false positive in the set, and a
    std threshold that rejects any false positive at all rejects it too. That
    plate was found by the detector at confidence 0.89 in twenty consecutive
    frames and thrown away by the filter every time.

    Edge density does not care that the two colours are close together, only
    that the boundary between them is everywhere. Over the same crops plus five
    views of the yellow plate: every one of the 12 real plates scores 0.288 or
    above and 7 of the 16 false positives score 0.174 or below, with nothing in
    between. The threshold sits in that gap.

    The false positives it does not catch are tail-light clusters and painted
    signage, which really are covered in edges. Those need a better detector,
    not a better filter.
    """
    if patch is None or patch.size == 0:
        return 0.0
    grey = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY).astype(np.float32)
    magnitude = cv2.magnitude(
        cv2.Sobel(grey, cv2.CV_32F, 1, 0, ksize=3),
        cv2.Sobel(grey, cv2.CV_32F, 0, 1, ksize=3),
    )
    peak = float(magnitude.max())
    if peak <= 0:
        return 0.0
    # Relative to the box's own strongest edge, so the measure does not change
    # when the same plate is seen in brighter light.
    return float((magnitude > 0.25 * peak).mean())


def sharpness(patch):
    """Variance of the Laplacian: how much real edge detail a crop carries.

    Used to rank one track's plate views against each other, never as an
    absolute threshold. Its scale depends on resolution and content, so a
    number that means "blurred" for one camera means "sharp" for another --
    but within one track, over one vehicle, higher is sharper.
    """
    if patch is None or patch.size == 0:
        return 0.0
    return float(cv2.Laplacian(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY),
                               cv2.CV_64F).var())


def stitch_rows(crop, cut, trim=_TWO_ROW_TRIM, height=_TWO_ROW_HEIGHT):
    """The two halves of a two-row plate laid side by side as one line.

    Reading the halves separately does not work, and the reason is the model
    rather than the cut. fast-plate-ocr emits a fixed number of slots and has
    only ever seen full single-row plates, so handed a five-character row it
    invents eight characters: over 30 combinations of cut point, trim and
    upscale, the two halves of the known MH17CY4718 crop came back as things
    like `MH4K7017` and `TR4T1888`, and **none of the 30 produced the plate**.

    Laying the rows end to end instead hands the model exactly what it was
    trained on -- one line of ten characters -- and the same crop reads
    MH17CI4718, one character from the truth. So the split is a rearrangement
    of the picture, not two reads to be joined afterwards.

    Both halves are scaled to a common height first, because they are rarely
    the same height: on an Indian two-row plate the bottom row carries the
    series and the four digits and is the taller of the two.
    """
    h, w = crop.shape[:2]
    ty, tx = int(h * trim), int(w * trim)
    inner = crop[ty:h - ty or h, tx:w - tx or w]
    ih, iw = inner.shape[:2]
    at = int(ih * cut)
    if at < 6 or ih - at < 6 or iw < 8:
        return None
    parts = []
    for part in (inner[:at], inner[at:]):
        ph, pw = part.shape[:2]
        if ph < 1 or pw < 1:
            return None
        parts.append(
            cv2.resize(part, (max(8, int(pw * height / ph)), height),
                       interpolation=cv2.INTER_CUBIC)
        )
    return cv2.hconcat(parts)


def _choose_rows(single, doubles, pad_char):
    """The one-line read, or a two-row rearrangement of the same crop.

    What was missing when two-row splitting was switched off was a way to
    decide *which* plates to split, and the previous attempt tried to decide it
    from the shape of the box. That cannot work, and the measurement is in
    settings.yaml: over the seven hand-labelled plates in footage/session01 the
    genuine two-row plate has a box aspect of 1.51 while two single-row plates
    measure 1.31 and 1.89. No threshold separates them, and an ink-profile test
    failed for the same reason -- the dark bumper around a padded single-row
    plate makes the same two bands a second row would.

    So the picture does not decide it. Both readings are run and the grammar
    arbitrates, **asymmetrically**: a rearranged read is taken only when it
    parses as a real Indian registration *and the one-line read does not*.

    The asymmetry is the whole safety argument, and it is load-bearing because
    validity on its own does not discriminate here -- a stitched crop nearly
    always produces something plate-shaped, so "the two-row read is valid" is
    weak evidence by itself. "The one-line read is not a plate in any layout"
    is the strong half. MH77AVX183 is not a plate; MH17CI4718 is. A plate the
    pipeline already reads as a valid registration is therefore untouchable by
    this, whatever the stitched version says, which is what makes it safe to
    leave on.

    Among rearrangements that qualify, the most confident wins. They differ
    only in where the crop was cut, and there is no principled way to pick one
    cut in advance -- the right one moves with how much bumper the padding
    caught -- so several are tried and the model's own confidence chooses.
    """
    from app import grammar

    single_text = (single[0] or "").replace(pad_char, "")
    if not doubles or grammar.correct(single_text)["valid"]:
        return single

    best = None
    for text, probs in doubles:
        cleaned = (text or "").replace(pad_char, "")
        if not cleaned or not grammar.correct(cleaned)["valid"]:
            continue
        score = float(np.mean(probs)) if len(probs) else 0.0
        if best is None or score > best[0]:
            best = (score, (text, probs))
    return best[1] if best is not None else single


def _join_rows(reads, pad_char):
    """Two half-plate reads into one, top row first.

    Confidences are concatenated in the same order so slot i of the joined
    string still lines up with slot i of its probabilities, which is what
    voting and the candidate scores both assume.
    """
    text = ""
    probs = []
    for slots, char_probs in reads:
        for i, char in enumerate(slots or ""):
            if char == pad_char:
                continue
            text += char
            probs.append(float(char_probs[i]) if i < len(char_probs) else 0.0)
    return text, np.asarray(probs, dtype=float)


class _quiet_ort:
    """Silence onnxruntime's own logger for the length of a block.

    Probing for CUDA prints a paragraph of red provider-load errors per worker
    process. When the probe is expected to fail on this machine that noise is
    misinformation, not diagnosis -- what actually happened is reported in one
    line afterwards, by us.
    """

    def __init__(self, quiet=True):
        self.quiet = quiet

    def __enter__(self):
        if self.quiet:
            import onnxruntime as ort

            # 4 = FATAL. Nothing short of the session dying gets through.
            ort.set_default_logger_severity(4)
        return self

    def __exit__(self, *exc):
        if self.quiet:
            import onnxruntime as ort

            ort.set_default_logger_severity(2)  # back to WARNING
        return False


class TorchRecognizer:
    """A PyTorch fine-tune wearing LicensePlateRecognizer's interface.

    The training track could not produce ONNX: fast-plate-ocr's trainer is Keras
    and torch.onnx.export needs the `onnx` package, and neither is installed --
    CLAUDE.md forbids pip, so the fine-tune is a .pt file. Rather than teach the
    pipeline about two kinds of model, the .pt is wrapped to answer exactly the
    calls the ONNX one answers: `.run(...) -> [PlatePrediction]` and `.config`.

    Everything downstream -- voting, candidates, confidence, the eval table --
    is unchanged and cannot tell which backend produced the numbers. Decoding
    reuses fast_plate_ocr's own preprocessing and postprocessing so a slot,
    a padding character and a confidence mean the same thing either way.
    """

    def __init__(self, weights, cfg_path, device):
        import torch
        from fast_plate_ocr.inference.config import PlateConfig

        self.torch = torch
        self.config = PlateConfig.from_yaml(cfg_path)
        checkpoint = torch.load(weights, map_location="cpu", weights_only=False)
        for field in ("alphabet", "pad_char", "max_plate_slots"):
            if field in checkpoint and getattr(self.config, field) != checkpoint[field]:
                raise ValueError(
                    f"{Path(weights).name} was trained with {field}="
                    f"{checkpoint[field]!r} but {Path(cfg_path).name} says "
                    f"{getattr(self.config, field)!r}. They must agree or every "
                    f"read is decoded against the wrong alphabet."
                )
        self.device = torch.device(
            "cuda:0" if device == "cuda" and torch.cuda.is_available() else "cpu"
        )
        self.model = _build_plate_net(checkpoint, self.config)
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


def _build_plate_net(checkpoint, cfg):
    """Reconstruct the architecture scripts/train_ocr.py saved.

    Defined here rather than imported: TRAINING.md forbids app/ importing from
    scripts/, and the two tracks are only allowed to share files on disk. The
    shape is pinned by the checkpoint, so a mismatch fails loudly at load rather
    than quietly reading garbage.
    """
    import torch.nn as nn

    class ConvBlock(nn.Module):
        def __init__(self, channels_in, channels_out, pool):
            super().__init__()
            self.body = nn.Sequential(
                nn.Conv2d(channels_in, channels_out, 3, padding=1, bias=False),
                nn.BatchNorm2d(channels_out),
                nn.ReLU(inplace=True),
                nn.Conv2d(channels_out, channels_out, 3, padding=1, bias=False),
                nn.BatchNorm2d(channels_out),
                nn.ReLU(inplace=True),
                nn.MaxPool2d(pool),
            )

        def forward(self, x):
            return self.body(x)

    import torch

    class PlateNet(nn.Module):
        def __init__(self, width, slots, vocabulary, columns):
            super().__init__()
            c = [int(w * width) for w in (32, 64, 128, 256)]
            self.stem = nn.Sequential(
                ConvBlock(3, c[0], (2, 2)),
                ConvBlock(c[0], c[1], (2, 2)),
                ConvBlock(c[1], c[2], (2, 2)),
                ConvBlock(c[2], c[3], (2, 1)),
            )
            self.rnn = nn.GRU(
                c[3], 128, num_layers=2, bidirectional=True, batch_first=True,
                dropout=0.1,
            )
            self.drop = nn.Dropout(0.3)
            self.head = nn.Linear(columns * 256, slots * vocabulary)
            self.slots, self.vocabulary = slots, vocabulary
            self.register_buffer(
                "mean", torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1)
            )
            self.register_buffer(
                "std", torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1)
            )

        def forward(self, x):
            if x.dtype == torch.uint8:
                x = x.float()
            # Contiguous, not decorative: the permuted view is channels_last and
            # cuDNN has no fast NHWC kernel for these shapes -- 22ms becomes
            # 307ms without it. See scripts/train_ocr.py.
            x = x.permute(0, 3, 1, 2).contiguous() / 255.0
            x = (x - self.mean) / self.std
            x = self.stem(x)
            x = x.mean(dim=2).permute(0, 2, 1)
            x, _ = self.rnn(x)
            x = self.drop(x.reshape(x.size(0), -1))
            return self.head(x).view(-1, self.slots, self.vocabulary)

    columns = cfg.img_width // 8
    return PlateNet(
        width=float(checkpoint.get("width", 1.0)),
        slots=cfg.max_plate_slots,
        vocabulary=cfg.vocabulary_size,
        columns=columns,
    )


def _load_recognizer():
    """Build the OCR model named in settings, or the pretrained fallback.

    A null models.ocr means TRAINING.md has not produced a fine-tune yet. The
    app must still run: it falls back to the pretrained fast-plate-ocr model,
    exactly as a null entry is specified to behave.

    models.ocr may name a .onnx or a .pt. Both arrive with a plate config beside
    them and both come back wearing the same interface, so this is the only
    function in the app that knows there is more than one kind.

    ocr_device is a request, never a guarantee. onnxruntime does not raise when
    its CUDA provider will not load -- it logs and hands back a CPU session --
    so asking for CUDA and trusting the answer is exactly how OCR ends up on
    the CPU with nobody noticing. The session is inspected afterwards and what
    it really got is printed.
    """
    from fast_plate_ocr import LicensePlateRecognizer

    want = str(config.default("ocr_device", "auto")).lower()
    if want not in ("auto", "cuda", "cpu"):
        raise ValueError(
            f"ocr_device must be auto, cuda or cpu, not {want!r}. "
            f"Check defaults.ocr_device in config/settings.yaml."
        )
    # 'auto' tries the GPU and quietly accepts the CPU; 'cuda' tries the GPU
    # and says loudly when it does not get it.
    device = "cpu" if want == "cpu" else "cuda"
    weights = config.model_path("ocr")

    if weights is not None:
        if not weights.exists():
            raise FileNotFoundError(
                f"OCR weights not found at {weights}. Set models.ocr to null in "
                f"config/settings.yaml to fall back to the pretrained model."
            )
        # A fine-tune ships beside its plate config, which carries the alphabet
        # and slot count the decoder needs.
        cfg = weights.with_name(weights.stem + "_plate_config.yaml")
        if not cfg.exists():
            cfg = weights.with_suffix(".yaml")
        if not cfg.exists():
            raise FileNotFoundError(
                f"No plate config beside {weights}. Export the fine-tune with a "
                f"matching {weights.stem}_plate_config.yaml alongside it."
            )
        if weights.suffix.lower() == ".pt":
            recognizer = TorchRecognizer(weights, cfg, device)
            on_gpu = recognizer.device.type == "cuda"
        else:
            with _quiet_ort(want == "auto"):
                recognizer = LicensePlateRecognizer(
                    onnx_model_path=str(weights),
                    plate_config_path=str(cfg),
                    device=device,
                )
            on_gpu = "CUDAExecutionProvider" in recognizer.model.get_providers()
        label = f"fine-tuned {weights.name}"
    else:
        name = str(config.default("ocr_model", "cct-s-v2-global-model"))
        with _quiet_ort(want == "auto"):
            recognizer = LicensePlateRecognizer(hub_ocr_model=name, device=device)
        on_gpu = "CUDAExecutionProvider" in recognizer.model.get_providers()
        label = f"pretrained fallback {name}"

    print(f"[ocr] {label} on {'CUDA' if on_gpu else 'CPU'}")
    if device == "cuda" and not on_gpu:
        # Two backends, two reasons, and naming the wrong one sends the next
        # person to reinstall a library that was never involved.
        if isinstance(recognizer, TorchRecognizer):
            message = (
                "OCR asked for CUDA and got CPU: torch.cuda.is_available() is "
                "False. That is the same CUDA the detector uses, so if the "
                "detector is on the GPU something is wrong here specifically."
            )
        else:
            message = (
                "OCR asked for CUDA and got CPU: onnxruntime could not load its "
                "CUDA provider. OCR runs once per track rather than per frame, "
                "so this costs little -- but it is not the GPU."
            )
        # An explicit ocr_device: cuda was a stated intent, so say why it did
        # not happen. On 'auto' the CPU is a documented outcome, not a fault.
        print(f"[ocr] {'WARNING: ' if want == 'cuda' else ''}{message}")
        if want == "cuda" and not isinstance(recognizer, TorchRecognizer):
            print(f"[ocr] {_cuda_diagnosis()}")

    return recognizer, recognizer.config.pad_char, recognizer.config.max_plate_slots


def _cuda_diagnosis():
    """Name the missing piece rather than the symptom.

    The provider DLL states its own dependencies; the first one the loader
    cannot find is the answer, and it is far more useful than onnxruntime's
    generic 'install the dependencies' message.
    """
    import ctypes
    import re
    from pathlib import Path

    try:
        import onnxruntime as ort

        provider = (
            Path(ort.__file__).parent / "capi" / "onnxruntime_providers_cuda.dll"
        )
        if not provider.exists():
            return f"onnxruntime has no CUDA provider library at {provider}."
        names = sorted(
            {
                match.decode("ascii")
                for match in re.findall(rb"[A-Za-z0-9_\-\.]+\.dll", provider.read_bytes())
            }
        )
        wanted = [n for n in names if n.lower().startswith(("cublas", "cudnn", "cudart"))]
        missing = []
        for name in wanted:
            try:
                ctypes.WinDLL(name)
            except OSError:
                missing.append(name)
        # Every CUDA major it can bind to is listed, so a name missing here is
        # only fatal if its whole major is missing.
        if missing:
            return (
                f"onnxruntime-gpu {ort.__version__} cannot find: {', '.join(missing)}. "
                f"Installing the matching CUDA runtime wheels would fix it; that is "
                f"a change to the environment, so it is your call."
            )
        return "every CUDA library the provider names is loadable; the cause is elsewhere."
    except Exception as exc:  # noqa: BLE001 - a diagnosis must never be fatal
        return f"could not diagnose: {type(exc).__name__}: {exc}"


# --------------------------------------------------------------------- voting


def vote(reads, pad_char="_", top_k=5, min_chars=4):
    """Fuse several reads of one plate into one string plus alternatives.

    Character-level majority vote: at each slot the character seen most often
    wins, and a tie is broken by summed confidence. Voting per slot rather than
    per string is what lets three reads that are each wrong in a different
    place still produce the right plate.

    Returns None when the result is too short to be a plate -- an empty read is
    a confident row of padding, not a plate.
    """
    # Strip the padding first. The model emits a fixed number of slots, so a
    # read of eight characters and one of nine agree on slot 0 but disagree
    # about what slot 5 even means, and voting across them silently deletes a
    # character.
    stripped = []
    for text, probs in reads:
        if not text:
            continue
        kept = [
            (char, float(probs[i]) if i < len(probs) else 0.0)
            for i, char in enumerate(text)
            if char != pad_char
        ]
        if len(kept) >= min_chars:
            stripped.append(kept)
    if not stripped:
        return None

    # Vote only among reads that agree on how many characters they saw. Reads
    # of a different length are not discarded -- they come back below as
    # candidates, which is where a dropped character belongs.
    groups = {}
    for read in stripped:
        groups.setdefault(len(read), []).append(read)
    best_len = max(
        groups,
        key=lambda size: (
            len(groups[size]),
            sum(prob for read in groups[size] for _, prob in read),
        ),
    )
    group = groups[best_len]

    width = best_len
    # votes[i][char] = (times seen, summed confidence)
    votes = [{} for _ in range(width)]
    for read in group:
        for i, (char, prob) in enumerate(read):
            count, total = votes[i].get(char, (0, 0.0))
            votes[i][char] = (count + 1, total + prob)

    n = len(group)

    def slot_score(i, char):
        """0-1 strength of one character at one slot, over every read taken.

        A character seen confidently in every read scores near 1; one seen once
        out of five scores near 0.2 however confident that once was.
        """
        _, total = votes[i].get(char, (0, 0.0))
        return total / n if n else 0.0

    def rank(i):
        # Majority first, confidence only as the tiebreak, per the spec.
        return sorted(
            votes[i], key=lambda c: (votes[i][c][0], votes[i][c][1]), reverse=True
        )

    chosen = [rank(i)[0] for i in range(width)]

    def assemble(slots):
        """Slot characters -> (plate string, confidence over its real chars)."""
        kept = [(i, c) for i, c in enumerate(slots) if c != pad_char]
        if not kept:
            return "", 0.0
        text = "".join(c for _, c in kept)
        conf = sum(slot_score(i, c) for i, c in kept) / len(kept)
        return text, conf

    primary, primary_conf = assemble(chosen)
    if len(primary) < min_chars:
        return None

    # Alternatives, for the fuzzy matching in P3. Two kinds, and the order
    # between them matters more than the scores within each: a string the model
    # actually produced is real evidence, while a one-character substitution is
    # a guess about how it might have gone wrong. Observed reads therefore fill
    # the list first, including the ones whose length lost the vote -- a
    # dropped character is precisely what fuzzy matching exists to survive.
    observed = {}
    for read in stripped:
        text = "".join(char for char, _ in read)
        if text != primary:
            score = float(np.mean([prob for _, prob in read]))
            observed[text] = max(score, observed.get(text, 0.0))

    synthetic = {}
    for i in range(width):
        for char in rank(i)[1:3]:
            swapped = list(chosen)
            swapped[i] = char
            text, conf = assemble(swapped)
            if text and text != primary and text not in observed:
                if len(text) >= min_chars:
                    synthetic[text] = max(conf, synthetic.get(text, 0.0))

    by_score = lambda pair: pair[1]  # noqa: E731
    ranked = sorted(observed.items(), key=by_score, reverse=True)
    ranked += sorted(synthetic.items(), key=by_score, reverse=True)

    candidates = [{"text": primary, "score": round(primary_conf, 4)}]
    candidates += [
        {"text": text, "score": round(score, 4)}
        for text, score in ranked[: max(0, top_k - 1)]
    ]

    return {
        "plate_raw": primary,
        "plate_conf": round(primary_conf, 4),
        "candidates": candidates,
        "frames_voted": n,
    }
