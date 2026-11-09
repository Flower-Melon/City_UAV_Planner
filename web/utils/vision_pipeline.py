"""巡查照片处理：行人检测、异常识别、标注及地面定位。"""

import json
import math
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont



def target_context(target):
    """给大模型标明当前裁剪图中的主要目标，避免归因到周边其他对象。"""
    x1, y1, x2, y2 = target["bbox_xyxy"]
    left, top, right, bottom = target["crop_bbox_xyxy"]
    box = [round((x1 - left) / (right - left) * 1000),
           round((y1 - top) / (bottom - top) * 1000),
           round((x2 - left) / (right - left) * 1000),
           round((y2 - top) / (bottom - top) * 1000)]
    return (f"本图主要目标为YOLO检出的{target['class_name']}。"
            f"该目标在当前裁剪图中的框为{box}，坐标已归一化到0–1000。"
            "只分析这一目标的可见异常意图，不输出坐标，也不将周边目标的异常归到该目标上。")


def validate_detections(detections):
    """校验由 YOLO 框和意图结果组合的异常目标列表。"""
    if not isinstance(detections, list) or not detections:
        raise ValueError("异常图片需要非空目标列表")
    for detection in detections:
        if (not isinstance(detection, dict) or set(detection) != {"status", "label", "bbox"}
                or detection["status"] not in ("patient", "armed_person")
                or not isinstance(detection["label"], str) or not detection["label"].strip()
                or len(detection["label"]) > 120):
            raise ValueError("异常目标需要status（patient或armed_person）、非空label和bbox")
        box = detection["bbox"]
        if (not isinstance(box, list) or len(box) != 4
                or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1000 for v in box)
                or box[0] >= box[2] or box[1] >= box[3]):
            raise ValueError("bbox必须为0到1000的[x_min,y_min,x_max,y_max]，且具有正面积")
    return detections


def save_annotated_image(image_path, detections, output_path):
    """按0–1000归一化坐标导出红框照片，保留原始巡查图片。"""
    validate_detections(detections)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(image_path) as source:
        image = source.convert("RGB")
    width, height = image.size
    draw = ImageDraw.Draw(image)
    line_width = max(2, min(width, height) // 300)
    try:
        font = ImageFont.truetype("msyh.ttc", max(14, min(width, height) // 45))
    except OSError:
        font = ImageFont.load_default()
    for detection in detections:
        x1, y1, x2, y2 = detection["bbox"]
        box = (x1 * width / 1000, y1 * height / 1000, x2 * width / 1000, y2 * height / 1000)
        draw.rectangle(box, outline="red", width=line_width)
        category = {"patient": "病人", "armed_person": "持刀人员"}[detection["status"]]
        label = f"{category}：{detection['label']}"[:40]
        bounds = draw.textbbox((0, 0), label, font=font)
        label_width, label_height = bounds[2] - bounds[0] + 8, bounds[3] - bounds[1] + 6
        left = max(0, min(box[0], width - label_width))
        top = max(0, box[1] - label_height)
        draw.rectangle((left, top, left + label_width, top + label_height), fill="red")
        draw.text((left + 4 - bounds[0], top + 3 - bounds[1]), label, font=font, fill="white")
    temporary = output.with_suffix(".part")
    image.save(temporary, format="JPEG", quality=95)
    temporary.replace(output)


def locate_target(image_path, bbox):
    """bbox为原图0–1000归一化框；返回地面UE世界XYZ米制坐标。

    不使用模型猜测世界坐标，也不以分析时无人机的位置代替曝光位置。
    缺少相机记录或射线未命中静态地面时直接报错，不派发投递。
    """
    from UAV.runtime.airsim_client import AirSimClient
    from UAV.runtime.airsim_state import _run_ue_json
    from UAV.utils.plan_flight_path import _xyz

    path = Path(image_path)
    if (not isinstance(bbox, list) or len(bbox) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1000 for v in bbox)
            or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]):
        raise ValueError("目标框必须为0–1000归一化的有效bbox")
    frames = json.loads((path.parent / "frame.json").read_text(encoding="utf-8"))
    frame = next((item for item in frames if item.get("image") == path.name), None)
    if not frame or "camera_orientation_ned" not in frame or "camera_fov_deg" not in frame:
        raise ValueError("目标照片缺少曝光相机姿态或视场角，无法定位地面位置")
    position = _xyz(frame["position_m"], "曝光位置")
    fov = frame["camera_fov_deg"]
    if type(fov) not in (int, float) or not math.isfinite(fov) or not 0 < fov < 180:
        raise ValueError("曝光相机视场角无效")
    orientation = frame["camera_orientation_ned"]
    q = [orientation[key] for key in ("w_val", "x_val", "y_val", "z_val")]
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in q):
        raise ValueError("曝光相机姿态无效")
    norm = math.hypot(*q)
    if norm < 1e-8:
        raise ValueError("曝光相机姿态无效")
    w, x, y, z = (v / norm for v in q)
    with Image.open(path) as image:
        width, height = image.size
    focal = width / (2 * math.tan(math.radians(fov) / 2))
    # AirSim相机局部轴：X向前、Y向右、Z向下。使用原图框中心。
    vx, vy, vz = (1.0, ((bbox[0] + bbox[2]) / 2000 * width - width / 2) / focal,
                  ((bbox[1] + bbox[3]) / 2000 * height - height / 2) / focal)
    tx, ty, tz = 2 * (y * vz - z * vy), 2 * (z * vx - x * vz), 2 * (x * vy - y * vx)
    ned = (vx + w * tx + y * tz - z * ty,
           vy + w * ty + z * tx - x * tz,
           vz + w * tz + x * ty - y * tx)
    length = math.hypot(*ned)
    direction = (ned[0] / length, ned[1] / length, -ned[2] / length)
    if direction[2] >= -0.1:
        raise RuntimeError("目标定位射线未朝向地面")
    end = [p + d * 2000 for p, d in zip(position, direction)]
    start_cm, end_cm = ([v * 100 for v in point] for point in (position, end))
    token = "WEB_TARGET_GROUND_" + uuid.uuid4().hex + ":"
    # WorldStatic对象射线排除病人和无人机，取得药箱所需的地面高度。
    code = (
        "import unreal,json; w=unreal.EditorLevelLibrary.get_game_world(); "
        "o=unreal.GameplayStatics.get_world_origin_location(w); "
        "a=" + repr(start_cm) + "; b=" + repr(end_cm) + "; "
        "s=unreal.Vector(a[0]-o.x,a[1]-o.y,a[2]-o.z); "
        "e=unreal.Vector(b[0]-o.x,b[1]-o.y,b[2]-o.z); "
        "hit=unreal.SystemLibrary.line_trace_single_for_objects(w,s,e,"
        "[unreal.ObjectTypeQuery.OBJECT_TYPE_QUERY1],False,[],unreal.DrawDebugTrace.NONE,True); "
        "d=hit.to_tuple() if hit else None; p=d[5] if d and d[0] else None; "
        "unreal.log(" + repr(token) + "+json.dumps({'target_position':"
        "[(p.x+o.x)/100,(p.y+o.y)/100,(p.z+o.z)/100] if p is not None else None}))"
    )
    with AirSimClient() as client:
        result = _run_ue_json(client, code, token, "目标地面定位命令未执行", "UE未返回目标地面定位结果")
    target = result.get("target_position")
    if target is None:
        raise RuntimeError("目标框投射未命中静态地面")
    return list(_xyz(target, "地面投递点"))


def locate_patient(image_path, bbox):
    """保留病人定位入口；两类异常人员共用同一坐标计算方法。"""
    return locate_target(image_path, bbox)


class SearchVisionPipeline:
    def __init__(self):
        from Model.yolov26.predict import YoloPredictor

        self.predictor = YoloPredictor()
        # 没有目标的照片不创建大模型客户端，也不发出图片请求。
        self.analyzer = None

    def __call__(self, image_path):
        image_path = Path(image_path).resolve()
        yolo = self.predictor.predict(image_path)
        if not yolo["detections"]:
            # 只向网页返回计数信息，不创建任何检测或大模型输出文件。
            return {
                "status": "normal", "description": "", "detections": [],
                "yolo_detection_count": 0, "llm_skipped": True,
                "llm_requested": False, "analysis_failed": False, "target_errors": [],
            }
        yolo_dir = Path(yolo["artifacts"]["json"]).parent
        output_dir = image_path.parent.parent / "llm_analysis"
        width, height = yolo["image_size"]["width"], yolo["image_size"]["height"]
        targets, detections, errors = [], [], []
        llm_requested = False
        for index, target in enumerate(yolo["detections"], 1):
            record = dict(target)
            record["crop_image"] = f"../yolo_detection/{target['crop_image']}"
            try:
                if self.analyzer is None:
                    from llm.vision import VisionAnalyzer

                    self.analyzer = VisionAnalyzer()
                llm_requested = True
                analysis = self.analyzer(yolo_dir / target["crop_image"], context=target_context(target))
                record.update(analysis)
                if analysis["status"] in ("patient", "armed_person"):
                    x1, y1, x2, y2 = target["bbox_xyxy"]
                    # 只转换 YOLO 原图像素框，大模型不生成或修正定位信息。
                    detections.append({
                        "status": analysis["status"],
                        "label": analysis["description"][:120],
                        "bbox": [x1 / width * 1000, y1 / height * 1000,
                                 x2 / width * 1000, y2 / height * 1000],
                    })
            except Exception as error:
                from llm.utils.client import error_message

                message = error_message(error)
                record.update(status="error", error=message)
                errors.append({"target_index": index, "error": message})
            targets.append(record)

        if detections:
            status = "abnormal"
        elif errors:
            status = "uncertain"
        else:
            status = "normal"
        descriptions = [f"目标{index}：{target['description']}" for index, target in enumerate(targets, 1)
                        if target.get("description")]
        if errors:
            descriptions.append(f"{len(errors)}个目标未完成意图识别，详见目标记录。")
        description = "；".join(descriptions)[:2000]

        result = {
            "status": status, "description": description, "detections": detections,
            "yolo_detection_count": len(targets), "llm_skipped": False,
            "llm_requested": llm_requested, "analysis_failed": len(errors) == len(targets),
            "target_errors": errors,
        }
        if not llm_requested:
            return result

        output_dir.mkdir(parents=True, exist_ok=True)
        report = {
            "source_image": f"../capture/{image_path.name}",
            "yolo_report": f"../yolo_detection/{image_path.stem}.json",
            **result, "targets": targets,
        }
        if detections:
            annotation = output_dir / "abnormal" / image_path.name
            save_annotated_image(image_path, detections, annotation)
            report["annotated_image"] = f"abnormal/{image_path.name}"
        report_path = output_dir / f"{image_path.stem}.json"
        temporary = report_path.with_suffix(".json.part")
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(report_path)
        return result

    def close(self):
        if self.analyzer is not None:
            self.analyzer.close()
            self.analyzer = None
