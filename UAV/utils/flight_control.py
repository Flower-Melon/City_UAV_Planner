"""共用飞行控制；调用者持有任务锁，内部不重新加锁。"""

import math
import time

from .airsim_client import vector
from .plan_flight_path import flight_segments


# 调用 AirSim 飞行动作，并要求服务明确返回成功。
# 输入为 RPC 方法、参数和超时；失败抛出异常，成功无返回值。
def _action(client, method, *args, timeout):
    if client.call(method, *args, timeout=timeout + 5) is not True:
        raise RuntimeError(f"AirSim执行{method}失败或超时")


# 以零水平速度维持给定 NED 高度，作为制动和稳定动作。
# 输入为连接、无人机、目标高度及持续秒数；实际控制无人机，无返回值。
def _hold(client, name, z, duration=.5):
    # Native hover can return False without issuing a command for a zero-length path.
    _action(client, "moveByVelocityZ", 0.0, 0.0, z, duration, 0,
            {"is_rate": True, "yaw_or_rate": 0.0}, name, timeout=max(5, duration+2))


# 低速修正到站位置，并核实位置误差和速度都已进入容差。
# 输入为 NED 目标点和米制比例；成功无返回值，无法稳定时抛出异常。
def _settle(client, name, destination, scale):
    """低速完成最后少量位置误差，并将速度归零。"""
    initial = vector(client.call("simGetVehiclePose", name)["position"])
    distance = math.dist(initial, destination)*scale
    deadline = time.monotonic() + max(15.0, distance*2 + 10.0)
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


class SearchFlight:
    """持锁后使用；沿显式路径飞行，不会把指定道路替换成另一条最短路。"""

    # 保存搜索飞行所需连接、坐标转换、无人机名称和取消信号。
    # 初始化控制状态；尚不解锁或启动无人机。
    def __init__(self, client, frame, name, origin, stop_event=None):
        self.client, self.frame, self.name, self.origin = client, frame, name, origin
        self.stop_event, self.controlling, self.z = stop_event, False, None
        self.collision_timestamp = None

    # 检查外部取消信号，让起飞和航段执行能及时中止。
    # 信号已设置时抛出 InterruptedError，否则无返回值。
    def check_cancel(self):
        if self.stop_event is not None and self.stop_event.is_set():
            raise InterruptedError("搜索已取消")

    # 核实飞行高度与净空后接管无人机，解锁并分段升至巡查高度。
    # 输入为当前位置、相对地面高度和校验回调；成功后稳定高度，失败抛出异常。
    def prepare(self, world, local, height, validate_altitude=lambda *_: None):
        self.check_cancel()
        client, name, frame = self.client, self.name, self.frame
        landed = client.call("getMultirotorState", name)["landed_state"] == 0
        ground = frame.ground_z(world if landed else self.origin, name, remember=landed)
        self.z = (self.origin[2] - ground - height) / frame.scale
        validate_altitude(ground + height)
        collision = client.call("simGetCollisionInfo", name)
        self.collision_timestamp = collision.get("time_stamp")
        client.call("enableApiControl", True, name)
        self.controlling = True
        if client.call("armDisarm", True, name) is not True:
            raise RuntimeError("无人机无法解锁")
        if not landed:
            client.call("cancelLastTask", name)
            _hold(client, name, local[2])
        # 分次升高，让取消请求在长距离上升期间也能生效。
        deadline = time.monotonic() + max(30, abs(local[2]-self.z)*frame.scale*2+15)
        while abs(local[2] - self.z) * frame.scale > 1.0:
            self.check_cancel()
            if time.monotonic() > deadline:
                raise RuntimeError("升高阶段超过时限")
            step = max(-5/frame.scale, min(5/frame.scale, self.z-local[2]))
            next_z = local[2] + step
            # 与fly_to_point一致：AirSim可能在已到达高度时返回False，以实际高度核实。
            client.call("moveToZ", next_z, 2/frame.scale, 20,
                        {"is_rate": True, "yaw_or_rate": 0.0}, 1.0, 0.0, name, timeout=25)
            current = vector(client.call("simGetVehiclePose", name)["position"])
            if abs(current[2]-next_z)*frame.scale > 1:
                raise RuntimeError("未到达指定上升高度")
            local = current
        _settle(client, name, [local[0], local[1], self.z], frame.scale)

    # 沿指定折线路点飞行，逐段检查取消、相机状态、到达误差和碰撞。
    # 输入为世界 XY 路点、速度和回调；实际控制飞行，不等待图片识别结果。
    def fly(self, path, speed, checkpoint=lambda: None, observe=lambda *_: None):
        for segment, segment_speed in flight_segments(path, speed):
            # 一次执行最多20米的原折线，保持多路点平滑追踪及取消响应。
            for chunk in _path_chunks(segment):
                self.check_cancel()
                checkpoint()
                points = [{"x_val": (p[0]-self.origin[0])/self.frame.scale,
                           "y_val": (p[1]-self.origin[1])/self.frame.scale, "z_val": self.z} for p in chunk[1:]]
                b = chunk[-1]
                length = sum(math.dist(a, b) for a, b in zip(chunk, chunk[1:]))
                # 每次RPC都可能重新加速；短段不能直接使用连续长航段的巡航速度。
                execution_speed = min(segment_speed, max(1.0, length/3))
                timeout = max(20, length/execution_speed*3+10)
                completed = self.client.call("moveOnPath", points, execution_speed/self.frame.scale, timeout,
                    0, {"is_rate": True, "yaw_or_rate": 0.0}, -1.0, 1.0, self.name, timeout=timeout+5)
                local = vector(self.client.call("simGetVehiclePose", self.name)["position"])
                actual = [self.origin[i]+local[i]*self.frame.scale for i in (0, 1)]
                if completed is not True:
                    if math.dist(actual, b) > 2 or abs(local[2]-self.z)*self.frame.scale > 2:
                        raise RuntimeError(f"巡查路径未完成或被取消：水平误差{math.dist(actual,b):.2f}米，"
                                           f"高度误差{abs(local[2]-self.z)*self.frame.scale:.2f}米，速度{segment_speed:g}米/秒")
                    # AirSim在接近短段终点时也可能返回False；只对已到达附近的点低速校准。
                    self.check_cancel()
                    _hold(self.client, self.name, self.z, 2.0)
                    _settle(self.client, self.name, [points[-1]["x_val"], points[-1]["y_val"], self.z], self.frame.scale)
                    local = vector(self.client.call("simGetVehiclePose", self.name)["position"])
                    actual = [self.origin[i]+local[i]*self.frame.scale for i in (0, 1)]
                if math.dist(actual, b) > 1.5 or abs(local[2]-self.z)*self.frame.scale > 1.5:
                    raise RuntimeError("巡查路径未到达指定路点，停止以免误报完成")
                observe(actual)
                self.check_cancel()
                collision = self.client.call("simGetCollisionInfo", self.name)
                if collision.get("has_collided") and collision.get("time_stamp") != self.collision_timestamp:
                    raise RuntimeError("检测到碰撞，搜索停止")

    # 取消当前飞行动作并重复发送制动指令，确认实际速度已降到悬停范围。
    # 成功无返回值；达到重试上限仍未稳定则抛出异常。
    def stop(self):
        if self.controlling:
            self.client.call("cancelLastTask", self.name)
            z = vector(self.client.call("simGetVehiclePose", self.name)["position"])[2]
            # 从巡航速度制动可能超过4秒；持续给出零速度指令并核实实际速度。
            for _ in range(12):
                _hold(self.client, self.name, z, 1.0)
                velocity = vector(self.client.call("getMultirotorState", self.name)["kinematics_estimated"]["linear_velocity"])
                if math.sqrt(sum(v*v for v in velocity))*self.frame.scale <= .15:
                    return
            raise RuntimeError("12次制动后仍未达到悬停速度范围")


# 按累计长度给原始折线路点分组，限制单次路径调用的规模。
# 输入为点列表和长度阈值；产出共享端点的路径块，不在过长的相邻点间插点。
def _path_chunks(points, limit=20):
    chunk, length = [points[0]], 0.0
    for point in points[1:]:
        step = math.dist(chunk[-1], point)
        if len(chunk) > 1 and length+step > limit:
            yield chunk
            chunk, length = [chunk[-1]], 0.0
        chunk.append(point)
        length += step
    if len(chunk) > 1:
        yield chunk


