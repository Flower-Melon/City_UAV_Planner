"""无人机飞行执行与共用任务锁；调用者负责持有对应无人机的锁。"""

from bisect import bisect_right
from collections import defaultdict
import math
import threading
import time

from .airsim_state import vector
from ..utils.plan_flight_path import _clear_path, _coordinates, _positive, flight_route

_VEHICLE_LOCKS = defaultdict(threading.Lock)
ROUTE_TOLERANCE_M = 5.0
_CONTROL_PERIOD = 0.2
_ARRIVAL_RADIUS_M = 1.5
_ACCELERATION = 2.0
# 本地SimpleFlight实测：零速度制动的速度衰减时间常数约2.8秒。
_BRAKING_RESPONSE_SECONDS = 3.0


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


class _ForwardTrack:
    """沿有序折线单向推进；采样航点只用于描述轨迹，不作为必须到达的目标。"""

    def __init__(self, points):
        self.points = points
        self.offsets = [0.0]
        for a, b in zip(points, points[1:]):
            self.offsets.append(self.offsets[-1] + math.dist(a[:2], b[:2]))
        self.index = 0
        self.progress = 0.0

    def update(self, position):
        # 仅依次检查前方路段，不能在闭环/交叉口吸附到远处的后续路段。
        while True:
            a, b = self.points[self.index : self.index + 2]
            length = self.offsets[self.index + 1] - self.offsets[self.index]
            tangent = [(b[j] - a[j]) / length for j in (0, 1)]
            along = sum((position[j] - a[j]) * tangent[j] for j in (0, 1))
            near_end = along >= length - 0.75 and math.dist(position[:2], b[:2]) <= 2.0
            if (along >= length or near_end) and self.index < len(self.points) - 2:
                # 一次遥测可能跨过多个采样点，先推进，再检查当前前方路段偏差。
                self.progress = max(self.progress, self.offsets[self.index + 1])
                self.index += 1
                continue
            # 已过终点时只判断横向偏离；沿前进方向过冲不等于偏离轨迹。
            projected = [a[j] + max(0.0, along) * tangent[j] for j in (0, 1)]
            error = math.dist(position[:2], projected)
            if error > ROUTE_TOLERANCE_M:
                raise RuntimeError(f"偏离飞行轨迹{error:.2f}米，停止飞行，不折返追补航点")
            self.progress = max(
                self.progress, self.offsets[self.index] + max(0.0, min(length, along))
            )
            return tangent, along >= length

    def sample(self, offset):
        offset = max(0.0, min(self.offsets[-1], offset))
        i = min(len(self.points) - 2, bisect_right(self.offsets, offset) - 1)
        fraction = (offset - self.offsets[i]) / (self.offsets[i + 1] - self.offsets[i])
        return tuple(a + (b - a) * fraction for a, b in zip(self.points[i], self.points[i + 1]))

    def command(self, position, measured_speed):
        tangent, passed = self.update(position)
        remaining = self.offsets[-1] - self.progress
        end = self.points[-1]
        arrived = passed or (
            remaining <= _ARRIVAL_RADIUS_M
            and math.dist(position[:2], end[:2]) <= ROUTE_TOLERANCE_M
        )
        if arrived:
            if abs(position[2] - end[2]) > 1.0:
                raise RuntimeError("已越过终点但高度偏差过大，停止飞行，不折返追补航点")
            return None

        # 每次按前方限速与制动距离更新速度，不再把速度档位切成独立到站任务。
        speed = self.points[self.index + 1][3]
        for offset, point in zip(self.offsets[self.index + 1 :], self.points[self.index + 1 :]):
            speed = min(
                speed,
                math.sqrt(point[3] ** 2 + 2 * _ACCELERATION * max(0, offset - self.progress)),
            )
        speed = min(
            speed,
            math.sqrt(2 * _ACCELERATION * max(0.0, remaining - _ARRIVAL_RADIUS_M / 2)),
            # 先扣除实测速度继续滑行的距离，不能到点后才开始发零速度。
            max(0.0, (remaining - measured_speed * _BRAKING_RESPONSE_SECONDS) / 2.0),
            measured_speed + _ACCELERATION * _CONTROL_PERIOD,
        )
        lookahead = min(12.0, max(2.0, speed * 0.8))
        # 急弯前限制前视距离，避免高速抄近路切过街角。
        for i in range(self.index + 1, len(self.points) - 1):
            a, b, c = self.points[i - 1 : i + 2]
            u, v = [b[j] - a[j] for j in (0, 1)], [c[j] - b[j] for j in (0, 1)]
            if (sum(x * y for x, y in zip(u, v))
                    < math.cos(math.pi / 6) * math.hypot(*u) * math.hypot(*v)):
                lookahead = min(lookahead, max(1.0, self.offsets[i] - self.progress))
                break
        target = self.sample(self.progress + lookahead)
        direction = [target[j] - position[j] for j in (0, 1)]
        forward = sum(x * y for x, y in zip(direction, tangent))
        if forward < 0:
            direction = [x - forward * y for x, y in zip(direction, tangent)]
        norm = math.hypot(*direction)
        if norm < 1e-9:
            raise RuntimeError("前方轨迹无法继续跟随，停止飞行，不反向修正")
        return [v * speed / norm for v in direction], target[2]


class FlightControl:
    """持锁后使用；沿显式路径飞行，不会把指定道路替换成另一条最短路。"""

    # 保存飞行所需连接、坐标转换和无人机名称。
    # 初始化控制状态；尚不解锁或启动无人机。
    def __init__(self, client, frame, name, origin):
        self.client, self.frame, self.name, self.origin = client, frame, name, origin
        self.controlling, self.z = False, None
        self.collision_timestamp = None
        self.diagnostics = {}

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

    def _confirm_move(self, completed, destination, horizontal_limit, vertical_limit, label):
        """仅核验升降结果；误差过大就停止，不发出回拉位置指令。"""
        position = vector(self.client.call("simGetVehiclePose", self.name)["position"])
        horizontal = math.dist(position[:2], destination[:2]) * self.frame.scale
        vertical = abs(position[2] - destination[2]) * self.frame.scale
        if (
            type(completed) is not bool
            or horizontal > horizontal_limit
            or vertical > vertical_limit
        ):
            raise RuntimeError(
                f"{self.name}{label}未到达目标：AirSim返回值{completed!r}，"
                f"水平误差{horizontal:.2f}米，高度误差{vertical:.2f}米，"
                f"目标NED高度{destination[2]:.3f}，实际{position[2]:.3f}"
            )
        if self.client.call("isApiControlEnabled", self.name) is not True:
            raise RuntimeError(f"{self.name}{label}中断：API控制权已释放")
        self._collision()

    def _vertical(self, target_z, speed):
        """分段原地升降，确认高度后才允许执行下一条水平航段。"""
        position = vector(self.client.call("simGetVehiclePose", self.name)["position"])
        if abs(position[2] - target_z) * self.frame.scale <= 0.5:
            # 地面剖面形成的厘米级换层不能再触发长时间制动。
            self.z = position[2]
            return
        self.stop()
        position = vector(self.client.call("simGetVehiclePose", self.name)["position"])
        deadline = time.monotonic() + max(
            30, abs(position[2] - target_z) * self.frame.scale / speed * 3 + 15
        )
        while abs(position[2] - target_z) * self.frame.scale > 0.5:
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"{self.name}原地升降超时：高度误差"
                    f"{abs(position[2] - target_z) * self.frame.scale:.2f}米"
                )
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
            self._confirm_move(completed, [*position[:2], next_z], ROUTE_TOLERANCE_M, 0.75, "原地升降")
            position = vector(self.client.call("simGetVehiclePose", self.name)["position"])
        self.z = target_z

    def fly_route(self, route, *, on_progress=None):
        """连续跟随折线；可回调水平累计米数及世界XYZ，回调不得阻塞控制。"""
        if not self.controlling:
            raise RuntimeError("执行航线前必须先prepare")
        points = [_coordinates(p, 4, "航点") for p in route]
        if not points:
            raise ValueError("航线不能为空")
        for p in points:
            _positive(p[3], "航点速度")
        position = vector(self.client.call("simGetVehiclePose", self.name)["position"])
        sections = list(_route_sections(points))
        if (not sections or sections[0][1]) and (
            math.dist(position[:2], self._ned(points[0])[:2]) * self.frame.scale > ROUTE_TOLERANCE_M
        ):
            raise RuntimeError("原地升降起点偏离实际位置，需要重新规划")
        completed_distance = 0.0

        def report(progress, position):
            if on_progress is not None:
                on_progress(completed_distance + progress, position)

        try:
            for section, vertical in sections:
                if vertical:
                    self._vertical(self._ned(section[-1])[2], min(section[-1][3], 2.0))
                else:
                    self._fly_forward(section, report if on_progress is not None else None)
                    completed_distance += sum(
                        math.dist(a[:2], b[:2]) for a, b in zip(section, section[1:])
                    )
            self._brake()
            actual = vector(self.client.call("simGetVehiclePose", self.name)["position"])
            target = self._ned(points[-1])
            horizontal = math.dist(actual[:2], target[:2]) * self.frame.scale
            vertical = abs(actual[2] - target[2]) * self.frame.scale
            cross_track = horizontal
            for point in reversed(points[:-1]):
                start = self._ned(point)
                length = math.dist(start[:2], target[:2])
                if length > 1e-8:
                    tangent = [(target[j] - start[j]) / length for j in (0, 1)]
                    overshoot = sum((actual[j] - target[j]) * tangent[j] for j in (0, 1))
                    if overshoot > 0:
                        cross_track = math.hypot(*[
                            actual[j] - target[j] - overshoot * tangent[j] for j in (0, 1)
                        ]) * self.frame.scale
                    break
            self.diagnostics.update(
                actual_ned=actual, target_ned=target, horizontal_error_m=horizontal,
                cross_track_error_m=cross_track, vertical_error_m=vertical,
            )
            if cross_track > ROUTE_TOLERANCE_M or vertical > 1.5:
                raise RuntimeError(
                    f"航线结束时横向偏差{cross_track:.2f}米、高度偏差{vertical:.2f}米"
                    f"（距端点{horizontal:.2f}米），已制动，不折返对位"
                )
            self.z = actual[2]
            self._collision()
        except BaseException:
            try:
                self.stop()
            except Exception:
                pass
            raise

    def _brake(self):
        """短暂制动即可拍照/切换航段，不要求精确坐标和极低的停稳速度。"""
        self.z = vector(self.client.call("simGetVehiclePose", self.name)["position"])[2]
        hold_position(self.client, self.name, self.z, 0.3)

    def _fly_forward(self, points, on_progress=None):
        # 跟踪器统一使用米制局部NED，比例仅在RPC边界转换。
        scale = self.frame.scale
        track = _ForwardTrack([(*[v * scale for v in self._ned(p)], p[3]) for p in points])

        def world(point):
            return (self.origin[0] + point[0], self.origin[1] + point[1], self.origin[2] - point[2])

        expected = sum(
            math.dist(a[:3], b[:3]) / max(0.2, b[3]) for a, b in zip(points, points[1:])
        )
        deadline = time.monotonic() + max(30.0, expected * 5 + 30)
        last_progress, progress_time = -1.0, time.monotonic()
        while True:
            position = [
                v * scale for v in vector(self.client.call("simGetVehiclePose", self.name)["position"])
            ]
            state = self.client.call("getMultirotorState", self.name)
            velocity = vector(state["kinematics_estimated"]["linear_velocity"])
            if self.client.call("isApiControlEnabled", self.name) is not True:
                raise RuntimeError(f"{self.name}飞行中断：API控制权已释放")
            self._collision()
            command = track.command(position, math.hypot(*velocity[:2]) * scale)
            self.diagnostics = {
                "position_local_m": position, "velocity_mps": [v * scale for v in velocity],
                "progress_m": track.progress, "remaining_m": track.offsets[-1] - track.progress,
            }
            if on_progress is not None:
                on_progress(track.progress, world(position))
            if command is None:
                return
            now = time.monotonic()
            if track.progress - last_progress >= 0.25:
                last_progress, progress_time = track.progress, now
            if now >= deadline or now - progress_time > 15:
                raise RuntimeError("沿轨迹向前飞行超时，停止飞行，不回头追补航点")
            horizontal, z = command
            # 容许轨迹误差不等于容许穿楼；短时运动和惯性余量仍检查建筑净空。
            horizon = _CONTROL_PERIOD + 0.3
            predicted = [position[j] + horizontal[j] * horizon for j in (0, 1)] + [
                position[2] + max(-2 * horizon, min(2 * horizon, z - position[2]))
            ]

            if not _clear_path([world(position), world(predicted)], 1.0, 3.0):
                raise RuntimeError("前方轨迹建筑净空不足，停止飞行，不反向修正")
            _action(
                self.client, "moveByVelocityZ", *[v / scale for v in horizontal], z / scale,
                _CONTROL_PERIOD, 0, {"is_rate": True, "yaw_or_rate": 0.0}, self.name,
                timeout=5,
            )

    def fly(self, path, speed):
        """执行指定二维巡查折线；高度沿用当前值，不重新寻路。"""
        if self.z is None:
            raise RuntimeError("执行路径前必须先prepare")
        altitude = self.origin[2] - self.z * self.frame.scale
        self.fly_route(flight_route([(*p, altitude) for p in path], speed))

    def land(self, home):
        """在基点附近直接降落；不为精确对位而反向平移。"""
        self.stop()
        destination = [(home[i] - self.origin[i]) / self.frame.scale for i in (0, 1)] + [self.z]
        position = vector(self.client.call("simGetVehiclePose", self.name)["position"])
        if math.dist(position[:2], destination[:2]) * self.frame.scale > ROUTE_TOLERANCE_M:
            raise RuntimeError("偏离返航基点超过5米，停止降落，不折返对位")
        ground = self.frame.ground_z([
            self.origin[0] + position[0] * self.frame.scale,
            self.origin[1] + position[1] * self.frame.scale,
            self.origin[2] - position[2] * self.frame.scale,
        ])
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
        if math.dist(position[:2], destination[:2]) * self.frame.scale > ROUTE_TOLERANCE_M or abs(height) > 2.0:
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


def _route_sections(points):
    """只在原地升降处断开；速度变化、采样点及20米边界不再切分任务。"""
    section = [points[0]]
    for a, b in zip(points, points[1:]):
        distance = math.dist(a[:3], b[:3])
        if distance < 1e-9:
            continue
        vertical = math.dist(a[:2], b[:2]) < 1e-8
        if vertical:
            if len(section) > 1:
                yield section, False
            yield [a, b], True
            section = [b]
        else:
            section.append(b)
    if len(section) > 1:
        yield section, False
