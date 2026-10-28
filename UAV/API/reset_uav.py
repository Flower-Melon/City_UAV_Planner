"""复位指定无人机或全部无人机。"""

from __future__ import annotations

import json
import math
from pathlib import Path
import sys
import threading
import time

# 支持 python UAV/API/reset_uav.py 直接运行。
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "UAV.API"

from ..runtime.airsim_client import AirSimClient
from ..runtime.airsim_state import calibrate, vector, vehicle_name
from ..runtime.flight_control import vehicle_lock

_RESET_LOCK = threading.Lock()


def _euler(quaternion):
    w, x, y, z = (float(quaternion[k]) for k in ("w_val", "x_val", "y_val", "z_val"))
    norm = math.sqrt(w*w + x*x + y*y + z*z)
    if not math.isfinite(norm) or norm == 0:
        raise RuntimeError("初始参考姿态无效")
    w, x, y, z = (v/norm for v in (w, x, y, z))
    return (math.atan2(2*(w*x+y*z), 1-2*(x*x+y*y)),
            math.asin(max(-1.0, min(1.0, 2*(w*y-z*x)))),
            math.atan2(2*(w*z+x*y), 1-2*(y*y+z*z)))


def _initial_vehicle_pose(client, name):
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


def _clear_ground_reference(client, names):
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


def _stop_and_lock(client, name):
    lock = vehicle_lock(name)
    deadline = time.monotonic() + 30.0
    # 取消正在执行的 RPC 动作，等待现有飞行函数完成自身清理后再复位。
    while True:
        client.call("cancelLastTask", name)
        if lock.acquire(timeout=0.1):
            return lock
        if time.monotonic() >= deadline:
            raise RuntimeError(f"{name} 的飞行任务未能停止，无法复位")


def _reset_one(client, name):
    paused = client.call("simIsPaused")
    client.call("simPause", True)
    controlling = False
    try:
        pose = _initial_vehicle_pose(client, name)
        client.call("enableApiControl", True, name)
        controlling = True
        if client.call("isApiControlEnabled", name) is not True:
            raise RuntimeError(f"无法取得 {name} 的API控制权")
        # SimpleFlight 在已经上锁时也会返回 False，因此复位允许重复调用。
        client.call("armDisarm", False, name)
        zero = {"x_val": 0.0, "y_val": 0.0, "z_val": 0.0}
        state = {**pose, "linear_velocity": zero, "angular_velocity": zero,
                 "linear_acceleration": zero, "angular_acceleration": zero}
        client.call("simSetKinematics", state, True, name)
        _clear_ground_reference(client, [name])
    finally:
        try:
            if controlling:
                client.call("enableApiControl", False, name)
        finally:
            client.call("simPause", paused)


def reset_uav(drone_id: int | str | None = None) -> None:
    """停止飞行并复位；不传编号时复位全部无人机，无返回值。

    drone_id: 正整数编号（1 对应 UAV_01）或完整无人机名称；None 表示全部。
    指定无人机恢复 AirSim 配置的初始位置与姿态，清零运动并上锁、释放控制权。
    全部复位使用 AirSim 内置 reset。输入或复位失败时抛出异常。
    """
    with _RESET_LOCK, AirSimClient(timeout=10) as client:
        vehicles = client.call("listVehicles")
        names = sorted(vehicles) if drone_id is None else [vehicle_name(drone_id, vehicles)]
        if not names:
            return
        locks = []
        try:
            for name in names:
                locks.append(_stop_and_lock(client, name))
            if drone_id is None:
                client.call("reset", timeout=30)
                _clear_ground_reference(client, names)
            else:
                _reset_one(client, names[0])
        finally:
            for lock in reversed(locks):
                lock.release()


if __name__ == "__main__":
    reset_uav()
