"""按分层飞行图飞往指定UE世界XYZ，完成后悬停。"""

from pathlib import Path
import math
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "UAV.API"

from ..utils import plan_flight_path
from ..utils.plan_flight_path import _xyz, _positive
from ..runtime.airsim_client import AirSimClient
from ..runtime.airsim_state import WorldFrame, vector, vehicle_name
from ..runtime.flight_control import FlightControl, vehicle_lock


def _world_position(client, name, origin, scale):
    local = vector(client.call("simGetVehiclePose", name)["position"])
    return (
        origin[0] + local[0] * scale,
        origin[1] + local[1] * scale,
        origin[2] - local[2] * scale,
    )


def fly_to_point(drone_id, target_position, max_height, flight_speed=25.0) -> None:
    """飞往世界XYZ目标；高度上限相对沿途地面，速度单位米/秒。

    同步完成后悬停并保留API控制；输入、规划或执行失败时抛出异常。
    规划在解锁前完成，接管后若位置漂移则从实际XYZ重新规划。
    """
    target = _xyz(target_position, "目标位置")
    cap = _positive(max_height, "最大离地高度")
    speed = _positive(flight_speed, "飞行速度")
    with AirSimClient() as client:
        name = vehicle_name(drone_id, client.call("listVehicles"))
        lock = vehicle_lock(name)
        if not lock.acquire(blocking=False):
            raise RuntimeError(f"{name}已有任务正在执行")
        flight = None
        try:
            frame = WorldFrame(client)
            world, local, origin = frame.vehicle(name)
            route = plan_flight_path(world, target, cap, speed)
            flight = FlightControl(client, frame, name, origin)
            flight.prepare(world, local)
            actual = _world_position(client, name, origin, frame.scale)
            if math.dist(actual, route[0][:3]) > 0.1:
                route = plan_flight_path(actual, target, cap, speed)
            flight.fly_route(route)
        except BaseException:
            if flight is not None:
                try:
                    flight.stop()
                except Exception:
                    pass
            raise
        finally:
            lock.release()


if __name__ == "__main__":
    fly_to_point("UAV_01", (1835.464, -467.561, 390.0), max_height=60.0, flight_speed=8.0)
