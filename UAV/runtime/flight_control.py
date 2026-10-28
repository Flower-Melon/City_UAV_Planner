"""无人机飞行执行与共用任务锁；调用者负责持有对应无人机的锁。"""

from collections import defaultdict
import math
import threading
import time

from .airsim_state import vector
from ..utils.plan_flight_path import _coordinates, _positive, flight_route

_VEHICLE_LOCKS = defaultdict(threading.Lock)


def vehicle_lock(name):
    return _VEHICLE_LOCKS[name]


# 调用 AirSim 飞行动作，并要求服务明确返回成功。
# 输入为 RPC 方法、参数和超时；失败抛出异常，成功无返回值。
def _action(client, method, *args, timeout):
    if client.call(method, *args, timeout=timeout + 5) is not True:
        raise RuntimeError(f"AirSim执行{method}失败或超时")


# 以零水平速度维持给定 NED 高度，作为制动和稳定动作。
# 输入为连接、无人机、目标高度及持续秒数；实际控制无人机，无返回值。
def hold_position(client, name, z, duration=0.5):
    # Native hover can return False without issuing a command for a zero-length path.
    _action(
        client,
        "moveByVelocityZ",
        0.0,
        0.0,
        z,
        duration,
        0,
        {"is_rate": True, "yaw_or_rate": 0.0},
        name,
        timeout=max(5, duration + 2),
    )


# 低速修正到站位置，并核实位置误差和速度都已进入容差。
# 输入为 NED 目标点和米制比例；成功无返回值，无法稳定时抛出异常。
def settle_at_position(client, name, destination, scale):
    """低速到站，允许水平偏差0.75米、高度偏差0.5米、速度0.3米/秒。"""
    initial = vector(client.call("simGetVehiclePose", name)["position"])
    distance = math.dist(initial, destination) * scale
    deadline = time.monotonic() + max(15.0, distance * 2 + 10.0)
    while True:
        current = vector(client.call("simGetVehiclePose", name)["position"])
        delta = [a - b for a, b in zip(destination, current)]
        horizontal = math.hypot(*delta[:2]) * scale
        vertical = abs(delta[2]) * scale
        velocity = vector(
            client.call("getMultirotorState", name)["kinematics_estimated"]["linear_velocity"]
        )
        speed = math.hypot(*velocity) * scale
        if horizontal <= 0.75 and vertical <= 0.5 and speed <= 0.3:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"{name}未能稳定在目标位置：水平误差{horizontal:.2f}米，"
                f"高度误差{vertical:.2f}米，速度{speed:.2f}米/秒"
            )
        if horizontal <= 0.75 and vertical <= 0.5:
            hold_position(client, name, destination[2], 0.5)
            continue
        velocity = [delta[0] * 0.8, delta[1] * 0.8]
        speed = math.hypot(*velocity)
        limit = 1.0 / scale
        if speed > limit:
            velocity = [v * limit / speed for v in velocity]
        _action(
            client,
            "moveByVelocityZ",
            *velocity,
            destination[2],
            0.25,
            0,
            {"is_rate": True, "yaw_or_rate": 0.0},
            name,
            timeout=5,
        )


class FlightControl:
    """持锁后使用；沿显式路径飞行，不会把指定道路替换成另一条最短路。"""

    # 保存飞行所需连接、坐标转换和无人机名称。
    # 初始化控制状态；尚不解锁或启动无人机。
    def __init__(self, client, frame, name, origin):
        self.client, self.frame, self.name, self.origin = client, frame, name, origin
        self.controlling, self.z = False, None
        self.collision_timestamp = None

    def prepare(self, world, local, height=None, validate_altitude=lambda *_: None):
        """接管并解锁。三维航线不传height，后续升降完全按航点执行。"""
        client, name, frame = self.client, self.name, self.frame
        landed = client.call("getMultirotorState", name)["landed_state"] == 0
        ground = frame.ground_z(world if landed else self.origin, name, remember=landed)
        self.z = local[2]
        if height is not None:
            height = _positive(height, "飞行高度")
            validate_altitude(ground + height)
        self.collision_timestamp = client.call("simGetCollisionInfo", name).get("time_stamp")
        client.call("enableApiControl", True, name)
        self.controlling = True
        if client.call("armDisarm", True, name) is not True:
            raise RuntimeError("无人机无法解锁")
        if not landed:
            client.call("cancelLastTask", name)
            hold_position(client, name, local[2])
        if height is not None:
            target_z = (self.origin[2] - ground - height) / frame.scale
            self._vertical(target_z, 2.0)

    def _ned(self, point):
        return [
            (point[0] - self.origin[0]) / self.frame.scale,
            (point[1] - self.origin[1]) / self.frame.scale,
            (self.origin[2] - point[2]) / self.frame.scale,
        ]

    def _collision(self):
        collision = self.client.call("simGetCollisionInfo", self.name)
        if (
            collision.get("has_collided")
            and collision.get("time_stamp") != self.collision_timestamp
        ):
            raise RuntimeError("检测到新碰撞，飞行停止")

    def _vertical(self, target_z, speed):
        """分段原地升降，确认高度后才允许执行下一条水平航段。"""
        self.stop()
        position = vector(self.client.call("simGetVehiclePose", self.name)["position"])
        deadline = time.monotonic() + max(
            30, abs(position[2] - target_z) * self.frame.scale / speed * 3 + 15
        )
        while abs(position[2] - target_z) * self.frame.scale > 0.5:
            if time.monotonic() > deadline:
                raise RuntimeError("原地升降超时")
            step = max(-5 / self.frame.scale, min(5 / self.frame.scale, target_z - position[2]))
            next_z = position[2] + step
            timeout = max(20.0, abs(step) * self.frame.scale / speed * 3 + 10)
            completed = self.client.call(
                "moveToZ",
                next_z,
                speed / self.frame.scale,
                timeout,
                {"is_rate": True, "yaw_or_rate": 0.0},
                1.0 / self.frame.scale,
                0.0,
                self.name,
                timeout=timeout + 5,
            )
            position = vector(self.client.call("simGetVehiclePose", self.name)["position"])
            if completed is not True or abs(position[2] - next_z) * self.frame.scale > 0.75:
                raise RuntimeError("原地升降未完成或被取消")
            self._collision()
        self.z = target_z

    def fly_route(self, route):
        """执行[(世界X,Y,Z,到达速度), ...]，按速度和升降边界分段。"""
        if not self.controlling:
            raise RuntimeError("执行航线前必须先prepare")
        points = [_coordinates(p, 4, "航点") for p in route]
        if not points:
            raise ValueError("航线不能为空")
        for p in points:
            _positive(p[3], "航点速度")
        position = vector(self.client.call("simGetVehiclePose", self.name)["position"])
        if math.dist(position, self._ned(points[0])) * self.frame.scale > 1.5:
            raise RuntimeError("航线起点偏离实际位置，需要重新规划")
        for chunk, vertical in _route_chunks(points):
            destination = self._ned(chunk[-1])
            length = sum(math.dist(a[:3], b[:3]) for a, b in zip(chunk, chunk[1:]))
            speed = min(chunk[-1][3], max(0.2, length / 3))
            if vertical:
                self.stop()
                current = vector(self.client.call("simGetVehiclePose", self.name)["position"])
                settle_at_position(
                    self.client,
                    self.name,
                    [destination[0], destination[1], current[2]],
                    self.frame.scale,
                )
                self._vertical(destination[2], min(speed, 2.0))
            else:
                timeout = max(20.0, length / speed * 3 + 10)
                path = [dict(zip(("x_val", "y_val", "z_val"), self._ned(p))) for p in chunk[1:]]
                completed = self.client.call(
                    "moveOnPath",
                    path,
                    speed / self.frame.scale,
                    timeout,
                    0,
                    {"is_rate": True, "yaw_or_rate": 0.0},
                    1.0 / self.frame.scale,
                    0.0,
                    self.name,
                    timeout=timeout + 5,
                )
                if completed is not True:
                    raise RuntimeError("航段未完成，可能超时或被取消")
            current = vector(self.client.call("simGetVehiclePose", self.name)["position"])
            horizontal = math.dist(current[:2], destination[:2]) * self.frame.scale
            height = abs(current[2] - destination[2]) * self.frame.scale
            if horizontal > 2 or height > 2:
                raise RuntimeError(
                    f"航段偏离目标：水平误差{horizontal:.2f}米，高度误差{height:.2f}米"
                )
            self.z = destination[2]
            self._collision()
            if vertical or horizontal > 0.75 or height > 0.5:
                self.stop()
                settle_at_position(self.client, self.name, destination, self.frame.scale)
                self._collision()
        self.z = self._ned(points[-1])[2]
        self.stop()
        settle_at_position(self.client, self.name, self._ned(points[-1]), self.frame.scale)
        self._collision()

    def fly(self, path, speed):
        """执行指定二维巡查折线；高度沿用当前值，不重新寻路。"""
        if self.z is None:
            raise RuntimeError("执行路径前必须先prepare")
        altitude = self.origin[2] - self.z * self.frame.scale
        self.fly_route(flight_route([(*p, altitude) for p in path], speed))

    def land(self, home):
        """在各自起飞基点精确对位、降落，上锁并释放 API 控制。"""
        self.stop()
        destination = [(home[i] - self.origin[i]) / self.frame.scale for i in (0, 1)] + [self.z]
        settle_at_position(self.client, self.name, destination, self.frame.scale)
        ground = self.frame.ground_z([home[0], home[1], self.origin[2]])
        position = vector(self.client.call("simGetVehiclePose", self.name)["position"])
        height = self.origin[2] - position[2] * self.frame.scale - ground
        # AirSim land 的下降速度为0.2米/秒；局部NED原点可能高于地面，不能用abs(self.z)估计航程。
        timeout = max(30.0, max(0.0, height) / 0.2 + 30.0)
        completed = self.client.call("land", timeout, self.name, timeout=timeout + 5)
        # SimpleFlight可能在上锁后才更新landed_state；先确认近地且持续静止，再上锁。
        deadline, stable_since = time.monotonic() + 3.0, None
        while True:
            state = self.client.call("getMultirotorState", self.name)
            position = vector(self.client.call("simGetVehiclePose", self.name)["position"])
            height = self.origin[2] - position[2] * self.frame.scale - ground
            speed = (
                math.hypot(*vector(state["kinematics_estimated"]["linear_velocity"]))
                * self.frame.scale
            )
            now = time.monotonic()
            if (
                (completed is True or state["landed_state"] == 0)
                and abs(height) <= 0.75
                and speed <= 0.15
            ):
                if stable_since is None:
                    stable_since = now
                if now - stable_since >= 1.0:
                    break
            else:
                stable_since = None
            if now >= deadline:
                raise RuntimeError(
                    f"无人机未完成降落：离地高度{height:.2f}米，速度{speed:.2f}米/秒，"
                    f"降落时限{timeout:.1f}秒，AirSim返回值{completed!r}"
                )
            time.sleep(0.1)
        self.client.call("armDisarm", False, self.name)
        deadline = time.monotonic() + 3.0
        while self.client.call("getMultirotorState", self.name)["landed_state"] != 0:
            if time.monotonic() >= deadline:
                raise RuntimeError("无人机着地上锁后仍未确认落地状态")
            time.sleep(0.1)
        self.client.call("enableApiControl", False, self.name)
        self.controlling = False
        if math.dist(position[:2], destination[:2]) * self.frame.scale > 1.0 or abs(height) > 2.0:
            raise RuntimeError("无人机未在起飞基点落地")

    # 取消当前飞行动作并重复发送制动指令，确认实际速度已降到悬停范围。
    # 成功无返回值；达到重试上限仍未稳定则抛出异常。
    def stop(self):
        if self.controlling:
            self.client.call("cancelLastTask", self.name)
            z = vector(self.client.call("simGetVehiclePose", self.name)["position"])[2]
            # 从巡航速度制动可能超过4秒；持续给出零速度指令并核实实际速度。
            for _ in range(12):
                hold_position(self.client, self.name, z, 1.0)
                velocity = vector(
                    self.client.call("getMultirotorState", self.name)["kinematics_estimated"][
                        "linear_velocity"
                    ]
                )
                if math.sqrt(sum(v * v for v in velocity)) * self.frame.scale <= 0.15:
                    return
            raise RuntimeError("12次制动后仍未达到悬停速度范围")


def _route_chunks(points, limit=20.0):
    """相同到达速度合并；升降和急弯处断开，长度只使用XYZ。"""
    chunk, length, previous_speed = [points[0]], 0.0, None
    for i, (a, b) in enumerate(zip(points, points[1:]), 1):
        distance = math.dist(a[:3], b[:3])
        if distance < 1e-9:
            continue
        vertical = math.dist(a[:2], b[:2]) < 1e-8
        if vertical or (len(chunk) > 1 and (b[3] != previous_speed or length + distance > limit)):
            if len(chunk) > 1:
                yield chunk, False
            chunk, length = [a], 0.0
        if vertical:
            yield [a, b], True
            chunk, previous_speed = [b], None
            continue
        chunk.append(b)
        length += distance
        previous_speed = b[3]
        if i < len(points) - 1:
            c = points[i + 1]
            u, v = (b[0] - a[0], b[1] - a[1]), (c[0] - b[0], c[1] - b[1])
            norms = math.hypot(*u) * math.hypot(*v)
            if norms and sum(x * y for x, y in zip(u, v)) / norms < math.cos(math.pi / 4):
                yield chunk, False
                chunk, length, previous_speed = [b], 0.0, None
    if len(chunk) > 1:
        yield chunk, False
