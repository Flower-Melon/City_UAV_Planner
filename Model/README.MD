# 模型调用说明

## 1 YOLOv26 行人检测 API

使用 `yolov26/weights.pt` 检测单张图片中的行人（`person`），返回类别、置信度和检测框，同时保存行人裁剪放大图，异常意图由大模型判断。

| 入参 | 类型 | 含义 |
|---|---|---|
| `image_path` | `str` 或 `Path` | 图像路径，相对路径以当前工作目录为准 |
| `conf` | `float` | 可选，置信度阈值，范围 `0～1`，默认 `0.85` |
| `device` | `str` | 可选，默认 `"0"`，使用第一张 GPU；`"cpu"` 使用 CPU |

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `dict` | 包含图像信息、类别表、目标数量、`detections` 检测列表及 `artifacts` 输出路径；未检出时列表为空，`artifacts` 为 `{}` |

入参样例与调用：

```python
from Model.yolov26.predict import YoloPredictor

predictor = YoloPredictor()
report = predictor.predict("output/000002/capture/000003.jpg", conf=0.25, device="0")
```

出参样例（`report["detections"]` 的主要字段）：

```python
[
    {
        "class_id": 0,
        "class_name": "person",
        "confidence": 0.936854,
        "bbox_xyxy": [1006.04, 705.15, 1049.54, 760.89],
    }
]
```

输入为 `output/编号/capture/图片.jpg` 时，输出到同一编号下的 `yolo_detection/`：

```text
output/000002/yolo_detection/
├─ 000003.annotated.jpg       行人检测标注图
├─ 000003.json                检测记录及输出路径
└─ crops/                    行人裁剪放大图
   └─ 000003_001_person.jpg
```

其他路径的图片输出到 `output/yolov26/`。未检出目标时不生成输出文件。

## 2 YOLOv8 高空车辆检测 API

检测高空俯视图片中的车辆（`vehicle`），返回随车身方向旋转的检测框。图片路径输入默认保存标注图和车辆裁剪图，内存图像默认只返回检测结果。

| 入参 | 类型 | 含义 |
|---|---|---|
| `image_path` | `str`、`Path` 或 `numpy.ndarray` | 图片路径或相机的 BGR 图像数组 |
| `conf` | `float` | 可选，置信度阈值，范围 `0～1`，默认 `0.80` |
| `device` | `str` | 可选，默认 `"0"`，使用第一张 GPU；`"cpu"` 使用 CPU |
| `save` | `bool` 或 `None` | 可选；默认按输入类型决定是否保存；`False` 仅返回结果，`True` 需要图片路径 |

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `dict` | 包含图像信息、类别表、目标数量、`detections` 检测列表及 `artifacts` 输出路径；未检出时列表为空，`artifacts` 为 `{}` |

入参样例与调用：

```python
from Model.yolov8.predict import YoloPredictor

predictor = YoloPredictor()
report = predictor.predict("output/000002/capture/000003.jpg", conf=0.80, device="0")
```

相机图像数组可直接调用 `predictor.predict(frame, save=False)`，连续处理时复用同一个 `predictor`。

出参样例（`report["detections"]` 的主要字段，坐标仅为示意）：

```python
[
    {
        "class_id": 0,
        "class_name": "vehicle",
        "confidence": 0.95,
        "bbox_xyxy": [1000.0, 700.0, 1040.0, 740.0],
        "bbox_xyxyxyxy": [[1000.0, 710.0], [1010.0, 700.0],
                          [1040.0, 730.0], [1030.0, 740.0]],
        "center_xy": [1020.0, 720.0],
    }
]
```

`bbox_xyxyxyxy` 为四个角点，`center_xy` 为车辆中心，`bbox_xyxy` 为包住旋转框的横平竖直矩形。坐标均为原图像素坐标。

输入图片位于 `output/编号/capture/` 时，结果保存到同一编号下的 `yolov8_detection/`；其他图片保存到 `output/yolov8/`。

网页启动任务后，高空监控机到位即开始车辆测速，默认限速 **60 km/h**，可在任务参数中修改。超速车辆会显示速度和证据图。转场时重新跟踪，最后一个区域持续监测至点击「新任务」。
