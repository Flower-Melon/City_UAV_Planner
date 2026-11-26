"""巡查照片处理：行人检测、异常识别、标注及地面定位。"""

import json
import math
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

LOG_TIMEZONE = timezone(timedelta(hours=8))
# 仅在仿真巡查使用，较低分数的框仍需与曝光时的场景人物唯一匹配。
SIMULATION_PERSON_CONFIDENCE = 0.45
SIMULATION_PERSON_ID = re.compile(r"(patient|armed_person)_[0-9]+")
SIMULATION_DESCRIPTIONS = {
    "patient": "画面中的人员已与仿真场景登记的救助对象匹配。",
    "armed_person": "画面中的人员已与仿真场景登记的持刀人员匹配。",
}


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


def _camera_geometry(frame, width):
    fov = frame["camera_fov_deg"]
    if type(fov) not in (int, float) or not math.isfinite(fov) or not 0 < fov < 180:
        raise ValueError("曝光相机视场角无效")
    q = [frame["camera_orientation_ned"][key + "_val"] for key in "wxyz"]
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in q):
        raise ValueError("曝光相机姿态无效")
    norm = math.hypot(*q)
    if norm < 1e-8:
        raise ValueError("曝光相机姿态无效")
    return tuple(v / norm for v in q), width / (2 * math.tan(math.radians(fov) / 2))


def _rotate(vector, quaternion):
    w, x, y, z = quaternion
    vx, vy, vz = vector
    tx, ty, tz = 2 * (y * vz - z * vy), 2 * (z * vx - x * vz), 2 * (x * vy - y * vx)
    return (vx + w * tx + y * tz - z * ty,
            vy + w * ty + z * tx - x * tz,
            vz + w * tz + x * ty - y * tx)


def capture_person_matches(frame, detections, width, height):
    """按曝光位置在每个原图框内查找唯一人物，匹配过程不使用分类结果。"""
    matches = [None] * len(detections)
    if not detections or not isinstance(frame, dict) or not frame.get("people_at_capture"):
        return matches
    try:
        cx, cy, cz = frame["position_m"]
        (w, x, y, z), focal = _camera_geometry(frame, width)
    except (ValueError, KeyError, TypeError):
        return matches
    inverse = (w, -x, -y, -z)
    # 站立、弯腰和倒地时，YOLO框可能覆盖胶囊不同高度，而非固定的地面点。
    for index, detection in enumerate(detections):
        left, top, right, bottom = (value * size / 1000
                                    for value, size in zip(detection["bbox"], (width, height, width, height)))
        candidates = set()
        for person in frame["people_at_capture"]:
            if not isinstance(person, dict) or not SIMULATION_PERSON_ID.fullmatch(str(person.get("person_id", ""))):
                continue
            if person["movement_m"] > 2.0:
                continue
            px, py, pz = person["center_m"]
            for offset in (-0.85, 0, 0.85):
                depth, rightward, downward = _rotate((px - cx, py - cy, cz - pz - offset), inverse)
                if depth <= 0:
                    continue
                u, v = width / 2 + focal * rightward / depth, height / 2 + focal * downward / depth
                dx, dy = max(left - u, 0, u - right), max(top - v, 0, v - bottom)
                margin = focal * (0.85 + person["movement_m"] / 2) / depth
                if math.hypot(dx, dy) <= margin:
                    candidates.add(person["person_id"])
                    break
        if len(candidates) == 1:
            matches[index] = candidates.pop()
    return matches


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
    with Image.open(path) as image:
        width, height = image.size
    quaternion, focal = _camera_geometry(frame, width)
    # AirSim相机局部轴：X向前、Y向右、Z向下。使用原图框中心。
    vx, vy, vz = (1.0, ((bbox[0] + bbox[2]) / 2000 * width - width / 2) / focal,
                  ((bbox[1] + bbox[3]) / 2000 * height - height / 2) / focal)
    ned = _rotate((vx, vy, vz), quaternion)
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
        self.analyzer = None
        self.warmed = False

    def _get_analyzer(self):
        if self.analyzer is None:
            from llm.vision import VisionAnalyzer

            self.analyzer = VisionAnalyzer()
        return self.analyzer

    def warmup(self):
        if not self.warmed:
            self.predictor.warmup()
            self.warmed = True

    def __call__(self, image_path, *, frame=None):
        image_path = Path(image_path).resolve()
        simulation = frame is not None
        options = {"conf": SIMULATION_PERSON_CONFIDENCE} if simulation else {}
        yolo = self.predictor.predict(image_path, dual_scale=True, **options)
        if not yolo["detections"]:
            # 只向网页返回计数信息，不创建任何检测或大模型输出文件。
            return {
                "status": "normal", "description": "", "detections": [],
                "yolo_detection_count": 0, "llm_skipped": True,
                "llm_requested": False, "analysis_failed": False, "target_errors": [],
                "llm_duration_ms": 0,
            }
        yolo_dir = Path(yolo["artifacts"]["json"]).parent
        output_dir = image_path.parent.parent / "llm_analysis"
        width, height = yolo["image_size"]["width"], yolo["image_size"]["height"]
        boxes = [
            {"bbox": [x * 1000 / size for x, size in zip(target["bbox_xyxy"], (width, height, width, height))]}
            for target in yolo["detections"]
        ]
        if simulation:
            return self._simulation_result(image_path, output_dir, yolo["detections"], frame, boxes, width, height)
        targets, detections, errors = [], [], []
        llm_requested = False
        for index, target in enumerate(yolo["detections"], 1):
            record = dict(target)
            record["crop_image"] = f"../yolo_detection/{target['crop_image']}"
            record["llm_duration_ms"] = None
            record["llm_request_started_at"] = None
            record["llm_request_finished_at"] = None
            try:
                analyzer = self._get_analyzer()
                llm_requested = True
                context = target_context(target)
                # 计时覆盖目标图片输入到结果返回（含失败），排除 YOLO 和客户端初始化。
                record["llm_request_started_at"] = datetime.now(LOG_TIMEZONE).isoformat(timespec="milliseconds")
                started = time.perf_counter()
                try:
                    analysis = analyzer(yolo_dir / target["crop_image"], context=context)
                finally:
                    record["llm_duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
                    record["llm_request_finished_at"] = datetime.now(LOG_TIMEZONE).isoformat(timespec="milliseconds")
                record.update(analysis)
                if analysis["status"] in ("patient", "armed_person"):
                    # 只转换 YOLO 原图像素框，大模型不生成或修正定位信息。
                    detections.append({
                        "status": analysis["status"],
                        "label": analysis["description"][:120],
                        **boxes[index - 1],
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
            "llm_duration_ms": round(sum(target["llm_duration_ms"] for target in targets
                                         if target["llm_duration_ms"] is not None), 3),
        }
        if not llm_requested:
            return result

        self._write_result(image_path, output_dir, result, targets)
        return result

    def _simulation_result(self, image_path, output_dir, yolo_targets, frame, boxes, width, height):
        associations = capture_person_matches(frame, boxes, width, height)
        targets, detections, person_ids = [], [], []
        for target, box, person_id in zip(yolo_targets, boxes, associations):
            record = {**target, "crop_image": f"../yolo_detection/{target['crop_image']}"}
            targets.append(record)
            if person_id is None or person_id in person_ids:
                record.update(status="unmatched" if person_id is None else "duplicate", person_id=person_id)
                continue
            kind = person_id.rsplit("_", 1)[0]
            description = SIMULATION_DESCRIPTIONS[kind]
            record.update(status=kind, description=description, person_id=person_id)
            detections.append({"status": kind, "label": description, **box})
            person_ids.append(person_id)
        result = {
            "status": "abnormal" if detections else "normal",
            "description": "；".join(detection["label"] for detection in detections)[:2000],
            "detections": detections, "person_matches": person_ids, "analysis_source": "simulation",
            "yolo_detection_count": len(targets), "llm_skipped": True, "llm_requested": False,
            "analysis_failed": False, "target_errors": [], "llm_duration_ms": 0,
        }
        self._write_result(image_path, output_dir, result, targets)
        return result

    @staticmethod
    def _write_result(image_path, output_dir, result, targets):
        output_dir.mkdir(parents=True, exist_ok=True)
        report = {
            "source_image": f"../capture/{image_path.name}",
            "yolo_report": f"../yolo_detection/{image_path.stem}.json",
            **result, "targets": targets,
        }
        if result["detections"]:
            annotation = output_dir / "abnormal" / image_path.name
            save_annotated_image(image_path, result["detections"], annotation)
            report["annotated_image"] = f"abnormal/{image_path.name}"
        report_path = output_dir / f"{image_path.stem}.json"
        temporary = report_path.with_suffix(".json.part")
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(report_path)

    def close(self):
        if self.analyzer is not None:
            self.analyzer.close()
            self.analyzer = None
