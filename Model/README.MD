# 模型调用说明

## 1 YOLO 检测 API

使用 `yolov26/weights.pt` 检测单张图片，返回目标类别、置信度和检测框，同时保存人车裁剪放大图。当前权重只有 `car`、`person` 两类，异常意图由大模型判断。

| 入参 | 类型 | 含义 |
|---|---|---|
| `image_path` | `str` 或 `Path` | 图像路径，相对路径以当前工作目录为准 |
| `conf` | `float` | 可选，置信度阈值，范围 `0～1`，默认 `0.25` |
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
        "class_id": 1,
        "class_name": "person",
        "confidence": 0.936854,
        "bbox_xyxy": [1006.04, 705.15, 1049.54, 760.89],
    }
]
```

输入为 `output/编号/capture/图片.jpg` 时，输出到同一编号下的 `yolo_detection/`：