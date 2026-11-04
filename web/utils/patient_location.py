"""按巡查曝光时的相机姿态，将异常人员框中心投射到 UE 静态地面。"""

import json
import math
from pathlib import Path
import uuid

from PIL import Image

from UAV.runtime.airsim_client import AirSimClient
from UAV.runtime.airsim_state import _run_ue_json
from UAV.utils.plan_flight_path import _xyz


def locate_target(image_path, bbox):
    """bbox为原图0–1000归一化框；返回地面UE世界XYZ米制坐标。

    不使用模型猜测世界坐标，也不以分析时无人机的位置代替曝光位置。
    缺少相机记录或射线未命中静态地面时直接报错，不派发投递。
    """
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
