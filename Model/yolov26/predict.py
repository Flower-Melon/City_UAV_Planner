"""可复用的 YOLO 行人检测与目标裁剪，不调用大模型。"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from PIL import Image


MODEL_DIR = Path(__file__).resolve().parent
DEFAULT_WEIGHTS = MODEL_DIR / "weights.pt"
DEFAULT_OUTPUT_DIR = MODEL_DIR.parents[1] / "output" / "yolov26"
CONFIDENCE_THRESHOLD = 0.8
IMAGE_SIZE = 640
DEFAULT_DEVICE = "0"
TARGET_CLASSES = {"person"}


class YoloPredictor:
    """每个实例只加载一次权重，供网页连续处理巡查图片。"""

    def __init__(self):
        if not DEFAULT_WEIGHTS.is_file():
            raise FileNotFoundError(f"Weights file does not exist: {DEFAULT_WEIGHTS}")
        from ultralytics import YOLO

        self.model = YOLO(str(DEFAULT_WEIGHTS), task="detect")
        self.class_ids = [class_id for class_id, name in self.model.names.items()
                          if name.lower() in TARGET_CLASSES]
        if not self.class_ids:
            raise ValueError("Weights do not contain the person class")

    @staticmethod
    def _output_directory(image_path: Path) -> Path:
        if image_path.parent.name == "capture":
            return image_path.parent.parent / "yolo_detection"
        return DEFAULT_OUTPUT_DIR

    def predict(
        self,
        image_path: str | Path,
        *,
        conf: float = CONFIDENCE_THRESHOLD,
        device: str = DEFAULT_DEVICE,
    ) -> dict:
        """检测并裁剪一张图片中的行人，连续调用复用同一份权重。"""
        if not 0 <= conf <= 1:
            raise ValueError("conf must be between 0 and 1")
        image_path = Path(image_path).expanduser().resolve()
        if not image_path.is_file():
            raise FileNotFoundError(f"Image file does not exist: {image_path}")
        result = self.model.predict(
            source=str(image_path), conf=conf, imgsz=IMAGE_SIZE,
            device=device, classes=self.class_ids, save=False, verbose=False,
        )[0]
        output_dir = self._output_directory(image_path)
        crop_dir = output_dir / "crops"
        height, width = result.orig_shape
        # 与 YOLO 使用同一幅解码图，裁剪时不绘制检测框，保留原始细节。
        source = None
        detections = []
        if result.boxes is not None:
            for box in result.boxes.cpu():
                class_id = int(box.cls.item())
                class_name = result.names[class_id]
                if class_name.lower() not in TARGET_CLASSES:
                    continue
                x1, y1, x2, y2 = [float(v) for v in box.xyxy[0].tolist()]
                if x1 >= x2 or y1 >= y2:
                    continue
                if source is None:
                    source = Image.fromarray(result.orig_img[..., ::-1])
                # 四周各扩展半个目标尺寸，至少32像素，保留手部、刀具及道路上下文。
                pad_x, pad_y = max(32, (x2 - x1) / 2), max(32, (y2 - y1) / 2)
                crop_box = [max(0, math.floor(x1 - pad_x)), max(0, math.floor(y1 - pad_y)),
                            min(width, math.ceil(x2 + pad_x)), min(height, math.ceil(y2 + pad_y))]
                crop = source.crop(crop_box)
                scale = max(1.0, IMAGE_SIZE / max(crop.size))
                if scale > 1:
                    crop = crop.resize(
                        (max(1, round(crop.width * scale)), max(1, round(crop.height * scale))),
                        Image.Resampling.LANCZOS,
                    )
                crop_dir.mkdir(parents=True, exist_ok=True)
                crop_name = f"{image_path.stem}_{len(detections) + 1:03d}_{class_name.lower()}.jpg"
                crop.save(crop_dir / crop_name, format="JPEG", quality=95)
                detections.append({
                    "class_id": class_id, "class_name": class_name,
                    "confidence": round(float(box.conf.item()), 6),
                    "bbox_xyxy": [round(v, 2) for v in (x1, y1, x2, y2)],
                    "crop_image": f"crops/{crop_name}",
                    "crop_bbox_xyxy": crop_box,
                    "crop_size": {"width": crop.width, "height": crop.height},
                })

        report = {
            "image_path": str(image_path), "weights_path": str(DEFAULT_WEIGHTS),
            "image_size": {"width": width, "height": height},
            "classes": {class_id: name for class_id, name in result.names.items()
                        if class_id in self.class_ids}, "confidence_threshold": conf,
            "detection_count": len(detections), "detections": detections,
            "artifacts": {},
        }
        if not detections:
            return report

        annotated_path = output_dir / f"{image_path.stem}.annotated.jpg"
        report_path = output_dir / f"{image_path.stem}.json"
        result.save(filename=str(annotated_path))
        report["artifacts"] = {"annotated_image": str(annotated_path), "json": str(report_path)}
        temporary = report_path.with_suffix(".json.part")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(report_path)
        return report


if __name__ == "__main__":
    # 直接运行时修改以下参数；命令行传参可覆盖这些设置。
    image_path = MODEL_DIR.parents[1] / "output/000002/yolo_detection/000003.annotated.jpg"
    conf = 0.25
    device = "0"
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
