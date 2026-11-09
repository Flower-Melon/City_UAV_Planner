"""可复用的 YOLOv8 高空俯视车辆旋转框检测与目标裁剪。"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image


MODEL_DIR = Path(__file__).resolve().parent
DEFAULT_WEIGHTS = MODEL_DIR / "weights.pt"
DEFAULT_OUTPUT_DIR = MODEL_DIR.parents[1] / "output" / "yolov8"
CONFIDENCE_THRESHOLD = 0.80
IMAGE_SIZE = 1920
CROP_SIZE = 640
DEFAULT_DEVICE = "0"
TARGET_CLASSES = {"vehicle"}


class YoloPredictor:
    """每个实例只加载一次权重，供后续监控处理复用。"""

    def __init__(self):
        if not DEFAULT_WEIGHTS.is_file():
            raise FileNotFoundError(f"Weights file does not exist: {DEFAULT_WEIGHTS}")
        from ultralytics import YOLO

        self.model = YOLO(str(DEFAULT_WEIGHTS), task="obb")

    @staticmethod
    def _output_directory(image_path: Path) -> Path:
        if image_path.parent.name == "capture":
            return image_path.parent.parent / "yolov8_detection"
        return DEFAULT_OUTPUT_DIR

    def predict(
        self,
        image_path: str | Path | np.ndarray,
        *,
        conf: float = CONFIDENCE_THRESHOLD,
        device: str = DEFAULT_DEVICE,
        save: bool | None = None,
    ) -> dict:
        """检测路径或内存中的BGR图像；内存输入默认只返回结果，不写文件。"""
        if not 0 <= conf <= 1:
            raise ValueError("conf must be between 0 and 1")
        if isinstance(image_path, np.ndarray):
            if (image_path.dtype != np.uint8 or image_path.ndim != 3
                    or image_path.shape[2] != 3 or min(image_path.shape[:2]) <= 0):
                raise ValueError("Image must be a nonempty uint8 BGR array")
            image, image_path = image_path, None
        else:
            image_path = Path(image_path).expanduser().resolve()
            if not image_path.is_file():
                raise FileNotFoundError(f"Image file does not exist: {image_path}")
            image = str(image_path)
        if save is None:
            save = image_path is not None
        if save and image_path is None:
            raise ValueError("Saving detection artifacts requires an image path")
        result = self.model.predict(
            source=image, conf=conf, imgsz=IMAGE_SIZE,
            device=device, save=False, verbose=False,
        )[0]
        output_dir = self._output_directory(image_path) if save else None
        height, width = result.orig_shape
        # 使用推理时的原图裁剪，保留未经画框的车辆细节。
        source = None
        detections = []
        if result.obb is not None:
            for box in result.obb.cpu():
                class_id = int(box.cls.item())
                class_name = result.names[class_id]
                if class_name.lower() not in TARGET_CLASSES:
                    continue
                corners = box.xyxyxyxy[0].tolist()
                x1 = min(p[0] for p in corners)
                y1 = min(p[1] for p in corners)
                x2 = max(p[0] for p in corners)
                y2 = max(p[1] for p in corners)
                if x1 >= x2 or y1 >= y2:
                    continue
                cx, cy = [float(v) for v in box.xywhr[0, :2].tolist()]
                detection = {
                    "class_id": class_id, "class_name": class_name,
                    "confidence": round(float(box.conf.item()), 6),
                    "bbox_xyxy": [round(v, 2) for v in (x1, y1, x2, y2)],
                    "bbox_xyxyxyxy": [[round(x, 2), round(y, 2)] for x, y in corners],
                    "center_xy": [round(cx, 2), round(cy, 2)],
                }
                if not save:
                    detections.append(detection)
                    continue
                # 旋转框的外接矩形四周保留道路上下文，裁剪范围限制在图内。
                pad_x, pad_y = max(32, (x2 - x1) / 2), max(32, (y2 - y1) / 2)
                crop_box = [max(0, math.floor(x1 - pad_x)), max(0, math.floor(y1 - pad_y)),
                            min(width, math.ceil(x2 + pad_x)), min(height, math.ceil(y2 + pad_y))]
                if crop_box[0] >= crop_box[2] or crop_box[1] >= crop_box[3]:
                    continue
                if source is None:
                    source = Image.fromarray(result.orig_img[..., ::-1])
                crop = source.crop(crop_box)
                scale = max(1.0, CROP_SIZE / max(crop.size))
                if scale > 1:
                    crop = crop.resize(
                        (max(1, round(crop.width * scale)), max(1, round(crop.height * scale))),
                        Image.Resampling.LANCZOS,
                    )
                crop_dir = output_dir / "crops"
                crop_dir.mkdir(parents=True, exist_ok=True)
                crop_name = f"{image_path.stem}_{len(detections) + 1:03d}_{class_name.lower()}.jpg"
                crop.save(crop_dir / crop_name, format="JPEG", quality=95)
                detection.update({
                    "crop_image": f"crops/{crop_name}",
                    "crop_bbox_xyxy": crop_box,
                    "crop_size": {"width": crop.width, "height": crop.height},
                })
                detections.append(detection)

        report = {
            "image_path": str(image_path) if image_path is not None else None, "weights_path": str(DEFAULT_WEIGHTS),
            "image_size": {"width": width, "height": height},
            "classes": dict(result.names), "confidence_threshold": conf,
            "detection_count": len(detections), "detections": detections,
            "artifacts": {},
        }
        if not detections or not save:
            return report

        annotated_path = output_dir / f"{image_path.stem}.annotated.jpg"
        report_path = output_dir / f"{image_path.stem}.json"
        # 高空车辆很小，使用细框和小字号，避免默认标签遮住车身。
        result.save(filename=str(annotated_path), line_width=2, font_size=16)
        report["artifacts"] = {"annotated_image": str(annotated_path), "json": str(report_path)}
        temporary = report_path.with_suffix(".json.part")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(report_path)
        return report


if __name__ == "__main__":
    # 直接运行时修改以下参数；命令行传参可覆盖这些设置。
    image_path = MODEL_DIR.parents[1] / "output/000001/capture/000001.jpg"
    conf = CONFIDENCE_THRESHOLD
    device = DEFAULT_DEVICE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image_path", nargs="?", type=Path, default=image_path)
    parser.add_argument("--conf", type=float, default=conf)
    parser.add_argument("--device", default=device, help="cpu, 0, etc.; GPU 0 by default")
    args = parser.parse_args()
    try:
        predictor = YoloPredictor()
        report = predictor.predict(args.image_path, conf=args.conf, device=args.device)
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        print(f"Detection failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(report, ensure_ascii=False, indent=2))
