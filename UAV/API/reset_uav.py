"""复位指定无人机或全部无人机。"""

from __future__ import annotations

import threading
import time

from ..utils.airsim_client import AirSimClient, vehicle_name, vehicle_lock
from ..utils.reset_state import clear_ground_reference, initial_vehicle_pose

_RESET_LOCK = threading.Lock()


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
        pose = initial_vehicle_pose(client, name)
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
        clear_ground_reference(client, [name])
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
                clear_ground_reference(client, names)
            else:
                _reset_one(client, names[0])
        finally:
            for lock in reversed(locks):
                lock.release()
