"""Export the wildlife finder's two models to ONNX. One-time, offline.

Production only ever runs the ONNX files on onnxruntime. Producing them needs
torch, ultralytics and speciesnet, about 2 GB of packages that have no business
on the NVR, so run this in a THROWAWAY venv (any machine, CPU is fine)::

    python3 -m venv /tmp/wl-export
    /tmp/wl-export/bin/pip install --extra-index-url https://download.pytorch.org/whl/cpu \\
        torch ultralytics onnx onnxslim speciesnet onnxruntime
    /tmp/wl-export/bin/python tools/wildlife_export_models.py OUT_DIR

then copy OUT_DIR's three files to the paths in `wildlife.detector_path`,
`wildlife.classifier_path` and `wildlife.classifier_labels_path` (default
/opt/marcellus/data/models/wildlife/), readable by the service user.

Sources and licenses:

- MegaDetector v6 YOLOv10-compact (`MDV6-yolov10-c.pt`), Zenodo record
  15398270, CC-BY-4.0. Exported with a dynamic input so one file serves any
  frame size; the finder letterboxes to `wildlife.frame_width`.
- SpeciesNet v4.0.3a always-crop classifier, Kaggle
  `google/speciesnet/pyTorch/v4.0.3a/1`, Apache-2.0. The published weights are
  an onnx2torch GraphModule, so this is a round trip back to ONNX; the script
  checks the result against torch on random input (max |diff| ~3e-5 when this
  was written).
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import urllib.request
from pathlib import Path

MD_URL = "https://zenodo.org/records/15398270/files/MDV6-yolov10-c.pt?download=1"
SN_MODEL = "kaggle:google/speciesnet/pyTorch/v4.0.3a/1"


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def export_detector(out: Path) -> Path:
    from ultralytics import YOLO

    pt = out / "MDV6-yolov10-c.pt"
    if not pt.exists():
        print(f"downloading {MD_URL}")
        urllib.request.urlretrieve(MD_URL, pt)  # noqa: S310 - fixed https URL
    onnx_path = Path(YOLO(str(pt)).export(format="onnx", dynamic=True, imgsz=1280, simplify=True))
    dest = out / "MDV6-yolov10-c.onnx"
    if onnx_path != dest:
        shutil.move(str(onnx_path), dest)
    pt.unlink()
    return dest


def export_classifier(out: Path) -> tuple[Path, Path]:
    import numpy as np
    import onnxruntime as ort
    import torch
    from speciesnet.utils import ModelInfo

    info = ModelInfo(SN_MODEL)
    model = torch.load(info.classifier, map_location="cpu", weights_only=False).eval()
    dest = out / "speciesnet-v4.0.3a-crop.onnx"
    x = torch.rand(1, 480, 480, 3)
    torch.onnx.export(
        model, x, str(dest), input_names=["image"], output_names=["logits"],
        dynamic_axes={"image": {0: "batch"}, "logits": {0: "batch"}},
        opset_version=17, dynamo=False,
    )
    sess = ort.InferenceSession(str(dest), providers=["CPUExecutionProvider"])
    with torch.no_grad():
        want = model(x).numpy()
    got = sess.run(None, {"image": x.numpy()})[0]
    diff = float(np.abs(want - got).max())
    print(f"speciesnet onnx vs torch max |diff| = {diff:.2e}")
    if diff > 1e-3:
        raise SystemExit("ONNX export does not match torch -- not writing labels")
    labels = out / "speciesnet-v4.0.3a-crop.labels.txt"
    shutil.copyfile(info.classifier_labels, labels)
    return dest, labels


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    paths = [export_detector(out), *export_classifier(out)]
    for p in paths:
        print(f"{p}  {p.stat().st_size / 1e6:.1f} MB  sha256 {_sha256(p)}")


if __name__ == "__main__":
    main()
