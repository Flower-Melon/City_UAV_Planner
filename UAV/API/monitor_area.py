"""原地升至监控高度，再直线飞往区域中心上空悬停。"""

import logging
import math
from numbers import Real
from pathlib import Path
import sys
import time

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "UAV.API"

from ..runtime.airsim_client import AirSimClient
from ..runtime.airsim_state import WorldFrame, vector, vehicle_name
from ..runtime.flight_control import FlightControl, _action, vehicle_lock
from ..runtime.kill_trees import CameraTreeFilter
from ..utils.plan_search_path import search_scope
from ..utils.select_UAV import _reserve_UAV

DEFAULT_MONITOR_HEIGHT = 200.0
CLIMB_SPEED = 15.0
FLIGHT_SPEED = 25.0
logger = logging.getLogger(__name__)


def _climb(flight, height):
    """连续上升到高度门槛后减速，允许过冲，不校验或回拉位置。"""
    client, name, scale = flight.client, flight.name, flight.frame.scale
    target_z = -height / scale
    local = vector(client.call("simGetVehiclePose", name)["position"])
    if local[2] <= target_z:
        return local
    deadline = time.monotonic() + max(30.0, (local[2] - target_z) * scale / CLIMB_SPEED * 4 + 15)
    while local[2] > target_z:
        if time.monotonic() >= deadline:
            raise RuntimeError(f"{name}快速爬升超时")
        _action(client, "moveByVelocity", 0.0, 0.0, -CLIMB_SPEED / scale,
                0.5, 0, {"is_rate": True, "yaw_or_rate": 0.0}, name, timeout=5)
        local = vector(client.call("simGetVehiclePose", name)["position"])
    # 零速度制动只消除惯性，不用高度闭环把过冲的无人机降回目标高度。
    while True:
        _action(client, "moveByVelocity", 0.0, 0.0, 0.0,
                0.5, 0, {"is_rate": True, "yaw_or_rate": 0.0}, name, timeout=5)
        velocity = vector(client.call("getMultirotorState", name)["kinematics_estimated"]["linear_velocity"])
        if math.hypot(*velocity) * scale <= flight.stop_speed_tolerance:
            return vector(client.call("simGetVehiclePose", name)["position"])
        if time.monotonic() >= deadline:
            raise RuntimeError(f"{name}快速爬升后制动超时")


def monitor_area(zone_id, drone_id=None, flight_height=DEFAULT_MONITOR_HEIGHT) -> None:
    """快速爬升到监控高度门槛，再保持实际高度直线飞到区域中心并悬停。

    flight_height是相对无人机起飞原点的最低监控高度，默认200米，允许爬升过冲。
    drone_id为None时从当前可用机队选取一架；指定ID时可将已有
    悬停监控机转往下一区域，但不能接管仍持有任务锁的无人机。
    爬升设定速度为15米/秒，不分段对位；高度已足够时直接转场。
    不规划路网；水平飞行接近目标提前减速，到达附近或越过终点后制动，
    不折返追点，使用FlightControl的全局到达容差，完成后保留API控制权。下视RGB相机隐藏
    树木，到位返回后仍保留此设置，供后续监控取图使用。
    """
    if (isinstance(flight_height, bool) or not isinstance(flight_height, Real)
            or not math.isfinite(flight_height) or flight_height <= 0):
        raise ValueError("监控飞行高度必须为正的有限数值")
    height = float(flight_height)
    if not isinstance(zone_id, str) or not zone_id.strip() or zone_id != zone_id.strip():
        raise ValueError("zone_id必须为非空区域ID")
    geometry, _ = search_scope({"zone_id": zone_id})
    center = geometry.centroid

    with AirSimClient() as client:
        if drone_id is None:
            name, lock = _reserve_UAV(zone_id)
        else:
            name = vehicle_name(drone_id, client.call("listVehicles"))
            lock = vehicle_lock(name)
            if not lock.acquire(blocking=False):
                raise RuntimeError(f"{name}已有任务正在执行")
        flight = tree_filter = None
        try:
            logger.info("监控无人机已选定", extra={"selected_drone_id": name})
            client.call("simGetCameraInfo", "bottom_center", name, False)
            tree_filter = CameraTreeFilter(client, name)
            # 已持有该机任务锁，可回收上次监控返回或旧进程留下的相机会话。
            tree_filter.start(reclaim_stale=True)
            frame = WorldFrame(client)
            world, local, origin = frame.vehicle(name)
            flight = FlightControl(client, frame, name, origin)
            flight.prepare(world, local)

            logger.info("%s：以 %.1f 米/秒快速爬升至相对起飞点至少 %.1f 米", name, CLIMB_SPEED, height)
            local = _climb(flight, height)
            altitude = origin[2] - local[2] * frame.scale
            flight.z = local[2]
            start = (origin[0] + local[0] * frame.scale,
                     origin[1] + local[1] * frame.scale, altitude)
            target = (center.x, center.y, altitude)
            logger.info("%s：开始直线飞往区域 %s 中心，距离 %.1f 米",
                        name, zone_id, math.dist(start[:2], target[:2]))
            flight.fly_route([(*start, 2.0), (*target, FLIGHT_SPEED)])
            flight.stop()
            local = vector(client.call("simGetVehiclePose", name)["position"])
            actual = (origin[0] + local[0] * frame.scale,
                      origin[1] + local[1] * frame.scale,
                      origin[2] - local[2] * frame.scale)
            if (math.dist(actual[:2], target[:2]) > flight.route_tolerance
                    or abs(actual[2] - altitude) > flight.altitude_tolerance):
                raise RuntimeError("监控机已制动悬停，但超出区域中心到达容差，不折返追点")
            # 目标区域的树木可能在飞行中才加载；到位后补充隐藏并保留设置。
            tree_filter.refresh()
            logger.info("监控无人机已到位悬停", extra={
                "monitor_position_m": list(actual),
                "monitor_height_m": actual[2] - origin[2],
            })
        except BaseException:
            if flight is not None:
                try:
                    flight.stop()
                except Exception:
                    pass
            if tree_filter is not None:
                try:
                    tree_filter.close()
                except Exception:
                    pass
            raise
        finally:
            # 飞抵后由AirSim保持悬停；空中无人机不会进入自动选机候选队列。
            lock.release()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    monitor_area("F-SCHOOL-1")
