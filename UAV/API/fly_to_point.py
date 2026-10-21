"""按道路拓扑最短路径飞往指定UE世界坐标。"""

from __future__ import annotations

import math
from pathlib import Path
import sys
from numbers import Real

# 支持 python UAV/API/fly_to_point.py 直接运行。
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "UAV.API"

from ..utils import plan_flight_path
from ..utils.flight_control import _action, _hold, _settle
from ..utils.airsim_client import AirSimClient, WorldFrame, vector, vehicle_name, vehicle_lock



def _target(value):
    try:
        if len(value) != 2 or any(isinstance(v, (bool, str, bytes)) for v in value):
            raise ValueError
        point = tuple(float(v) for v in value)
        if not all(math.isfinite(v) for v in point):
            raise ValueError
        return point
    except (TypeError, ValueError, OverflowError):
        raise ValueError("目标位置必须为两个有限数值组成的(x, y)，单位为米") from None


def fly_to_point(drone_id, target_position, flight_height, flight_speed=25.0) -> None:
    """飞往道路图上的二维目标点，完成后悬停，无返回值。

    drone_id: 正整数编号（1对应UAV_01）或AirSim中的完整无人机名称。
    target_position: UE世界(x, y)，单位米；图外目标以最近拓扑边投影点为终点。
    flight_height: 相对起飞地面的正高度，单位米；航程内保持此高度平面。
    flight_speed: 巡航速度上限，单位米/秒，默认25.0；拐弯与到站前自动降速。

    同步执行；连接、输入、路径或飞行失败时抛出异常。成功后保留API控制。
    """
    target = _target(target_position)
    if isinstance(flight_height, bool) or not isinstance(flight_height, Real) or not math.isfinite(float(flight_height)) or flight_height <= 0:
        raise ValueError("飞行高度必须是大于0的有限数值，单位为米")
    if isinstance(flight_speed, bool) or not isinstance(flight_speed, Real):
        raise ValueError("飞行速度必须是大于0的有限数值，单位为米/秒")
    try:
        flight_speed = float(flight_speed)
    except (ValueError, OverflowError):
        raise ValueError("飞行速度必须是大于0的有限数值，单位为米/秒") from None
    if not math.isfinite(flight_speed) or flight_speed <= 0:
        raise ValueError("飞行速度必须是大于0的有限数值，单位为米/秒")
    with AirSimClient() as client:
        name = vehicle_name(drone_id, client.call("listVehicles"))
        lock = vehicle_lock(name)
        if not lock.acquire(blocking=False):
            raise RuntimeError(f"{name}已有fly_to_point任务正在执行")
        controlling = False
        try:
            frame = WorldFrame(client)
            world, local, origin = frame.vehicle(name)
            # Invalid targets or disconnected routes are rejected before arming.
            path, segments = plan_flight_path(world[:2], target, flight_speed)
            target = path[-1]
            landed = client.call("getMultirotorState", name)["landed_state"] == 0
            ground = frame.ground_z(world if landed else origin, name, remember=landed)
            altitude_world = ground + float(flight_height)
            z = (origin[2] - altitude_world) / frame.scale
            client.call("enableApiControl", True, name)
            controlling = True
            if not client.call("armDisarm", True, name):
                raise RuntimeError(f"{name}无法解锁")
            yaw_hold = {"is_rate": True, "yaw_or_rate": 0.0}
            lookahead = max(1.0, 1.0 / frame.scale)
            if not landed:
                client.call("cancelLastTask", name)
                _hold(client,name,local[2])
            vertical_timeout = max(30.0, abs(z - local[2]) * frame.scale / 2 * 3)
            client.call("moveToZ", z, 2.0 / frame.scale, vertical_timeout, yaw_hold,
                        lookahead, 0.0, name, timeout=vertical_timeout+5)
            at_height = vector(client.call("simGetVehiclePose",name)["position"])
            if abs(at_height[2]-z)*frame.scale > 2:
                raise RuntimeError(f"{name}未升至指定高度")
            _settle(client,name,[at_height[0],at_height[1],z],frame.scale)
            # Replan from the actual position after climbing, including any drift.
            current_local = vector(client.call("simGetVehiclePose", name)["position"])
            current_xy = [origin[i] + current_local[i] * frame.scale for i in (0, 1)]
            if math.dist(current_xy, path[0]) > 1e-4:
                path, segments = plan_flight_path(current_xy, target, flight_speed)
                target = path[-1]
            for segment, segment_speed in segments:
                # AirSim 会自动加入当前位置，省略共享起点可避免切换速度时回头追点。
                points = [{"x_val": (x-origin[0])/frame.scale,
                           "y_val": (y-origin[1])/frame.scale, "z_val": z} for x,y in segment[1:]]
                length = math.fsum(math.dist(a,b) for a,b in zip(segment,segment[1:]))
                route_timeout = max(30.0, length / segment_speed * 3 + 15)
                # 自动前视及自适应修正；保持航向，避免每次纠偏都改变机头方向。
                completed = client.call("moveOnPath", points, segment_speed / frame.scale, route_timeout,
                                        0, yaw_hold, -1.0, 1.0, name, timeout=route_timeout+5)
                if completed is not True:
                    raise RuntimeError(f"{name}路径段未完成，可能超时或被取消（速度 {segment_speed:.2f} 米/秒）")
            destination = [(target[0]-origin[0])/frame.scale,
                           (target[1]-origin[1])/frame.scale, z]
            _hold(client, name, z, duration=1.0)
            near_target = vector(client.call("simGetVehiclePose",name)["position"])
            xy_error = math.dist(near_target[:2], destination[:2])*frame.scale
            z_error = abs(near_target[2]-z)*frame.scale
            if xy_error > 10 or z_error > 3:
                raise RuntimeError(f"{name}路径结束后仍明显偏离目标（水平误差 {xy_error:.2f} 米，高度误差 {z_error:.2f} 米）")
            _settle(client,name,destination,frame.scale)
            final = vector(client.call("simGetVehiclePose", name)["position"])
            actual = [origin[i] + final[i]*frame.scale for i in (0, 1)]
            if math.dist(actual, target) > 1.0 or abs(final[2]-z)*frame.scale > 1.0:
                raise RuntimeError(f"{name}未到达目标位置或指定高度")
        except BaseException:
            if controlling:
                try:
                    client.call("cancelLastTask", name)
                    _hold(client,name,vector(client.call("simGetVehiclePose",name)["position"])[2])
                except Exception:
                    pass
            raise
        finally:
            lock.release()


if __name__ == "__main__":
    drone_id = "UAV_01"
    target_position = (1802.468, -2467.915)
    flight_height = 20.0
    flight_spead = 50.0
    fly_to_point(drone_id, target_position, flight_height,flight_spead)
