"""按道路拓扑最短路径飞往指定UE世界坐标。"""

from __future__ import annotations

import math
import time
from numbers import Real

from ..utils import find_shortest_path
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


def _action(client, method, *args, timeout):
    if client.call(method, *args, timeout=timeout + 5) is not True:
        raise RuntimeError(f"AirSim执行{method}失败或超时")


def _hold(client, name, z, duration=.5):
    # Native hover can return False without issuing a command for a zero-length path.
    _action(client, "moveByVelocityZ", 0.0, 0.0, z, duration, 0,
            {"is_rate": True, "yaw_or_rate": 0.0}, name, timeout=max(5, duration+2))


def _settle(client, name, destination, scale):
    """低速完成最后少量位置误差，并将速度归零。"""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        current = vector(client.call("simGetVehiclePose", name)["position"])
        delta = [a-b for a,b in zip(destination,current)]
        if math.hypot(delta[0],delta[1])*scale <= .25 and abs(delta[2])*scale <= .25:
            _hold(client,name,destination[2],.8)
            stopped = vector(client.call("simGetVehiclePose",name)["position"])
            velocity = vector(client.call("getMultirotorState",name)["kinematics_estimated"]["linear_velocity"])
            if math.dist(stopped,destination)*scale <= .35 and math.sqrt(sum(v*v for v in velocity))*scale <= .15:
                return
            continue
        velocity = [delta[0]*.8,delta[1]*.8]
        speed = math.hypot(*velocity)
        limit = 1.0/scale
        if speed > limit:
            velocity = [v*limit/speed for v in velocity]
        _action(client,"moveByVelocityZ",*velocity,destination[2],.25,0,
                {"is_rate": True,"yaw_or_rate": 0.0},name,timeout=5)
    raise RuntimeError(f"{name}未能稳定在目标位置")


def fly_to_point(drone_id, target_position, flight_height, flight_speed=25.0) -> None:
    """飞往道路图上的二维目标点，完成后悬停，无返回值。

    drone_id: 正整数编号（1对应UAV_01）或AirSim中的完整无人机名称。
    target_position: UE世界(x, y)，单位米，必须位于道路拓扑图边上。
    flight_height: 相对起飞地面的正高度，单位米；航程内保持此高度平面。
    flight_speed: 沿路径飞行的正速度，单位米/秒，默认25.0。

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
            path = find_shortest_path(world[:2], target)
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
                path = find_shortest_path(current_xy, target)
            points = [{"x_val": (x-origin[0])/frame.scale,
                       "y_val": (y-origin[1])/frame.scale, "z_val": z} for x,y in path]
            length = math.fsum(math.dist(a,b) for a,b in zip(path,path[1:]))
            route_timeout = max(60.0, length / flight_speed * 3 + 30)
            yaw_forward = {"is_rate": False, "yaw_or_rate": 0.0}
            client.call("moveOnPath", points, flight_speed / frame.scale, route_timeout,
                        1, yaw_forward, lookahead, 0.0, name, timeout=route_timeout+5)
            near_target = vector(client.call("simGetVehiclePose",name)["position"])
            destination = [points[-1][k] for k in ("x_val","y_val","z_val")]
            if math.dist(near_target,destination)*frame.scale > 2:
                raise RuntimeError(f"{name}路径飞行中断或未到达目标附近")
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
