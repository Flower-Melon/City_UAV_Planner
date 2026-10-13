"""读取无人机配置中的初始姿态，以及清理起飞地面基准。"""

import json
import math

from .airsim_client import calibrate, vector


def _euler(quaternion):
    w, x, y, z = (float(quaternion[k]) for k in ("w_val", "x_val", "y_val", "z_val"))
    norm = math.sqrt(w*w + x*x + y*y + z*z)
    if not math.isfinite(norm) or norm == 0:
        raise RuntimeError("初始参考姿态无效")
    w, x, y, z = (v/norm for v in (w, x, y, z))
    return (math.atan2(2*(w*x+y*z), 1-2*(x*x+y*y)),
            math.asin(max(-1.0, min(1.0, 2*(w*y-z*x)))),
            math.atan2(2*(w*z+x*y), 1-2*(y*y+z*z)))


def initial_vehicle_pose(client, name):
    """调用时应暂停物理更新，以一致地测量全局与局部坐标的平移量。"""
    settings = json.loads(client.call("getSettingsString"))
    config = settings.get("Vehicles", {}).get(name)
    if config is None:
        raise RuntimeError(f"AirSim配置中没有 {name} 的初始位置")
    # 对本项目的显式 XYZ 出生配置进行复位；不能从当前位置猜测出生高度。
    try:
        position = [float(config[k]) for k in ("X", "Y", "Z")]
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(f"{name} 的AirSim配置需要完整的初始 X、Y、Z") from error
    if not all(math.isfinite(v) for v in position):
        raise RuntimeError(f"{name} 的初始位置无效")

    keys = ("Roll", "Pitch", "Yaw")
    if all(config.get(key) is not None for key in keys):
        default_angles = (0.0, 0.0, 0.0)
    else:
        anchor = calibrate(client)
        default_angles = _euler(client.call("simGetObjectPose", anchor["actor"])["orientation"])
    angles = [math.radians(float(config[key])) if config.get(key) is not None else default
              for key, default in zip(keys, default_angles)]
    if not all(math.isfinite(v) for v in angles):
        raise RuntimeError(f"{name} 的初始姿态无效")
    roll, pitch, yaw = angles
    cr, cp, cy = (math.cos(v/2) for v in angles)
    sr, sp, sy = (math.sin(v/2) for v in angles)
    orientation = {"w_val": cr*cp*cy + sr*sp*sy,
                   "x_val": sr*cp*cy - cr*sp*sy,
                   "y_val": cr*sp*cy + sr*cp*sy,
                   "z_val": cr*cp*sy - sr*sp*cy}

    local = vector(client.call("simGetVehiclePose", name)["position"])
    global_position = vector(client.call("simGetObjectPose", name)["position"])
    local_position = [spawn - world + current for spawn, world, current
                      in zip(position, global_position, local)]
    return {"position": dict(zip(("x_val", "y_val", "z_val"), local_position)),
            "orientation": orientation}


def clear_ground_reference(client, names):
    # 起飞地面基准位于 UE Pawn 的运行时标签中，复位时清除旧航程的基准。
    code = (
        "import unreal; w=unreal.EditorLevelLibrary.get_game_world(); "
        "names=" + repr(list(names)) + "; "
        "[a.set_editor_property('tags',[t for t in a.get_editor_property('tags') "
        "if not str(t).startswith('CityLLM_FlyGroundZ=')]) "
        "for a in unreal.GameplayStatics.get_all_actors_of_class(w,unreal.Pawn) "
        "if a.get_name() in names]"
    )
    if client.call("simRunConsoleCommand", "py " + code) is not True:
        raise RuntimeError("未能清除无人机的起飞地面基准")
