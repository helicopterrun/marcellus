"""ONNX models for the wildlife finder -- the ONLY part that needs the extra.

Two onnxruntime sessions, CPU-only, lazily created:

- **Detector**: MegaDetector v6 YOLOv10-compact (CC-BY-4.0 weights from
  Zenodo record 15398270), exported with a dynamic input. Output is
  ``[1, 7, anchors]``: cx, cy, w, h, then one score per class
  (animal, person, vehicle). Only "animal" is kept.
- **Classifier**: SpeciesNet v4.0.3a always-crop (Apache-2.0), exported back to
  ONNX from its onnx2torch GraphModule. Input is NHWC float32 in [0, 1] at
  480x480, one crop per detector box, no padding -- the same preprocessing as
  ``speciesnet.classifier.SpeciesNetClassifier.preprocess``.

onnxruntime rather than OpenVINO: measured 0.32s vs 0.24s per 1280px frame on
this host, which is not worth a second ~100 MB inference runtime when the
`[enrich]` extra already ships onnxruntime. The iGPU is unreachable from the
LXC (no Intel compute runtime), so there is no GPU path to choose.

Everything here takes and returns plain Python values; numpy never leaks out,
so the rest of the package (and its tests) runs without the extra.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CLASSIFIER_SIZE = 480
_TOP_K = 30
_NMS_IOU = 0.45
_PAD_VALUE = 114


class WildlifeUnavailable(RuntimeError):
    """onnxruntime / numpy not installed, or a model file is missing."""


@dataclass(frozen=True)
class Frame:
    """One decoded RGB frame. `pts` is seconds from the segment start."""

    pts: float
    width: int
    height: int
    rgb: bytes


@dataclass(frozen=True)
class Detection:
    """Normalized [0, 1] corner box and MegaDetector's animal score."""

    x1: float
    y1: float
    x2: float
    y2: float
    score: float


def iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def nms(dets: list[Detection], *, iou_threshold: float = _NMS_IOU) -> list[Detection]:
    """Greedy NMS, highest score first. Pure Python: post-threshold boxes number
    in the tens, not thousands."""
    keep: list[Detection] = []
    for d in sorted(dets, key=lambda d: -d.score):
        box = (d.x1, d.y1, d.x2, d.y2)
        if all(iou(box, (k.x1, k.y1, k.x2, k.y2)) < iou_threshold for k in keep):
            keep.append(d)
    return keep


def _require() -> tuple[Any, Any]:
    try:
        import numpy as np
        import onnxruntime as ort
    except ImportError as exc:
        raise WildlifeUnavailable(
            "onnxruntime / numpy not installed. "
            'Install with `pip install "marcellus[wildlife]"`.'
        ) from exc
    return np, ort


def check_available(*paths: Path) -> None:
    """Raise WildlifeUnavailable without loading anything heavy."""
    _require()
    for p in paths:
        if not Path(p).is_file():
            raise WildlifeUnavailable(
                f"model file {p} is missing -- see tools/wildlife_export_models.py"
            )


class Engine:
    """Detector + classifier sessions. The classifier (~224 MB) loads on first
    use, so a run whose boxes are all learned scenery never pays for it."""

    def __init__(
        self, detector_path: Path, classifier_path: Path, labels_path: Path, *, threads: int
    ) -> None:
        check_available(detector_path, classifier_path, labels_path)
        np, ort = _require()
        self._np = np
        self._ort = ort
        self._threads = max(1, int(threads))
        self._detector = self._session(detector_path)
        self._det_input = self._detector.get_inputs()[0].name
        self._classifier_path = classifier_path
        self._classifier: Any = None
        with open(labels_path, encoding="utf-8") as fh:
            self.labels = [line.strip() for line in fh if line.strip()]

    def _session(self, path: Path) -> Any:
        so = self._ort.SessionOptions()
        so.intra_op_num_threads = self._threads
        so.inter_op_num_threads = 1
        return self._ort.InferenceSession(
            str(path), so, providers=["CPUExecutionProvider"]
        )

    def _image(self, frame: Frame) -> Any:
        from PIL import Image

        return Image.frombuffer("RGB", (frame.width, frame.height), frame.rgb, "raw", "RGB", 0, 1)

    def detect(self, frame: Frame, *, size: int, min_score: float) -> list[Detection]:
        """Animal boxes on one frame, letterboxed to `size` on the long edge."""
        from PIL import Image

        np = self._np
        w, h = frame.width, frame.height
        r = size / float(max(w, h))
        nw, nh = round(w * r), round(h * r)
        pw, ph = (32 - nw % 32) % 32, (32 - nh % 32) % 32
        img = self._image(frame)
        if (nw, nh) != (w, h):
            img = img.resize((nw, nh), Image.Resampling.BILINEAR)
        canvas = Image.new("RGB", (nw + pw, nh + ph), (_PAD_VALUE,) * 3)
        canvas.paste(img, (pw // 2, ph // 2))
        x = np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0

        out = self._detector.run(None, {self._det_input: x})[0][0]  # [7, anchors]
        scores = out[4:]
        keep = (scores.argmax(0) == 0) & (scores[0] >= min_score)
        if not keep.any():
            return []
        cx, cy, bw, bh = out[:4, keep]
        sc = scores[0, keep]
        dets = []
        for i in range(len(sc)):
            x1 = (cx[i] - bw[i] / 2 - pw // 2) / r / w
            y1 = (cy[i] - bh[i] / 2 - ph // 2) / r / h
            x2 = (cx[i] + bw[i] / 2 - pw // 2) / r / w
            y2 = (cy[i] + bh[i] / 2 - ph // 2) / r / h
            x1, y1 = max(0.0, float(x1)), max(0.0, float(y1))
            x2, y2 = min(1.0, float(x2)), min(1.0, float(y2))
            if x2 > x1 and y2 > y1:
                dets.append(Detection(x1, y1, x2, y2, float(sc[i])))
        return nms(dets)

    def classify(
        self, frame: Frame, boxes: list[Detection]
    ) -> list[list[tuple[str, float]]]:
        """Top-k `(class string, probability)` per box, in input order."""
        if not boxes:
            return []
        from PIL import Image

        np = self._np
        if self._classifier is None:
            self._classifier = self._session(self._classifier_path)
            self._cls_input = self._classifier.get_inputs()[0].name
        img = self._image(frame)
        crops = []
        for b in boxes:
            box = (
                int(b.x1 * frame.width),
                int(b.y1 * frame.height),
                max(int(b.x1 * frame.width) + 1, int(b.x2 * frame.width)),
                max(int(b.y1 * frame.height) + 1, int(b.y2 * frame.height)),
            )
            crop = img.crop(box).resize(
                (CLASSIFIER_SIZE, CLASSIFIER_SIZE), Image.Resampling.BILINEAR
            )
            crops.append(np.asarray(crop, dtype=np.float32) / 255.0)
        out: list[list[tuple[str, float]]] = []
        for i in range(0, len(crops), 8):
            logits = self._classifier.run(None, {self._cls_input: np.stack(crops[i : i + 8])})[0]
            logits = logits - logits.max(axis=1, keepdims=True)
            probs = np.exp(logits)
            probs /= probs.sum(axis=1, keepdims=True)
            for row in probs:
                idx = row.argsort()[::-1][:_TOP_K]
                out.append([(self.labels[j], float(row[j])) for j in idx])
        return out


def render_jpeg(
    frame: Frame,
    *,
    crop: tuple[float, float, float, float] | None = None,
    pad: float = 0.0,
    outline: tuple[float, float, float, float] | None = None,
    max_edge: int,
    quality: int = 80,
) -> bytes:
    """A JPEG of the frame (optionally padded-cropped, optionally with a box
    drawn). Pillow only -- a core dependency -- so callers need no extra."""
    from PIL import Image, ImageDraw

    img = Image.frombuffer("RGB", (frame.width, frame.height), frame.rgb, "raw", "RGB", 0, 1)
    w, h = img.size
    if outline is not None:
        img = img.copy()
        x1, y1, x2, y2 = outline
        ImageDraw.Draw(img).rectangle(
            (x1 * w, y1 * h, x2 * w, y2 * h), outline=(255, 64, 0), width=max(2, w // 400)
        )
    if crop is not None:
        x1, y1, x2, y2 = crop
        px, py = (x2 - x1) * pad, (y2 - y1) * pad
        img = img.crop(
            (
                int(max(0.0, x1 - px) * w),
                int(max(0.0, y1 - py) * h),
                max(int(max(0.0, x1 - px) * w) + 1, int(min(1.0, x2 + px) * w)),
                max(int(max(0.0, y1 - py) * h) + 1, int(min(1.0, y2 + py) * h)),
            )
        )
    img.thumbnail((max_edge, max_edge))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()
