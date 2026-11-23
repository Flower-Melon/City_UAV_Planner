"""原地升至监控高度，再直线飞往区域中心上空悬停。"""

import logging
import math
from pathlib import Path
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "UAV.API"

from ..runtime.airsim_client import AirSimClient
from ..runtime.airsim_state import WorldFrame, vehicle_name
from ..runtime.flight_control import FlightControl, vehicle_lock
from ..runtime.kill_trees import CameraTreeFilter
from ..utils.plan_flight_path import _positive
from ..utils.plan_search_path import search_scope
from ..utils.select_UAV import _reserve_UAV

DEFAULT_MONITOR_HEIGHT = 200.0
CLIMB_SPEED = 15.0
FLIGHT_SPEED = 25.0
logger = logging.getLogger(__name__)


def monitor_area(zone_id, drone_id=None, flight_height=DEFAULT_MONITOR_HEIGHT, *,
                 flight_speed=FLIGHT_SPEED, by_drone=None) -> str:
    """爬升到设定高度，直线飞到区域中心上空悬停并返回机号。

    高度相对起飞原点，默认200米。指定ID可调动已悬停且未被占用的监控机。
    """
    height = _positive(flight_height, "监控高度")
    speed = _positive(flight_speed, "飞行速度")
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
            profile = (by_drone or {}).get(name, {})
            height = _positive(profile.get("flight_height", height), "监控高度")
            speed = _positive(profile.get("flight_speed", speed), "飞行速度")
            climb_speed = min(CLIMB_SPEED, speed)
            logger.info("监控无人机已选定", extra={"selected_drone_id": name})
            client.call("simGetCameraInfo", "bottom_center", name, False)
            tree_filter = CameraTreeFilter(client, name)
            # 已持有该机任务锁，可回收上次监控返回或旧进程留下的相机会话。
            tree_filter.start(reclaim_stale=True)
            frame = WorldFrame(client)
            _, _, origin = frame.vehicle(name)
            flight = FlightControl(client, frame, name, origin)
            flight.prepare()

            altitude = max(flight.position[2], origin[2] + height)
            logger.info("%s：爬升至监控高度 %.1f 米", name, altitude - origin[2])
            flight.climb(altitude, climb_speed)
            start = flight.position
            target = (center.x, center.y, altitude)
            logger.info("%s：飞往区域 %s 中心，距离 %.1f 米",
                        name, zone_id, math.dist(start[:2], target[:2]))
            flight.fly_route([(*start, speed), (*target, speed)])
            actual = flight.position
            # 目标区域的树木可能在飞行中才加载；到位后补充隐藏并保留设置。
            tree_filter.refresh()
            logger.info("监控无人机已到位悬停", extra={
                "monitor_position_m": list(actual),
                "monitor_height_m": actual[2] - origin[2],
            })
            return name
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
