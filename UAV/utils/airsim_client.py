"""AirSim通信、坐标转换、共用任务管理与飞行控制；调用者持有飞行任务锁。"""

from __future__ import annotations

from collections import defaultdict
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
import uuid

import msgpack

from .plan_flight_path import flight_segments

_VEHICLE_LOCKS = defaultdict(threading.Lock)


def vehicle_name(number, vehicles):
    if isinstance(number, str) and number in vehicles:
        return number
    if isinstance(number, bool):
        raise ValueError("无人机编号应为正整数或AirSim中的名称")
    if isinstance(number, int) or isinstance(number, str) and number.isdecimal():
        if int(number) > 0:
            name = f"UAV_{int(number):02d}"
            if name in vehicles:
                return name
    raise ValueError(f"无人机编号不存在：{number!r}；可用名称：{', '.join(vehicles)}")


def vehicle_lock(name):
    return _VEHICLE_LOCKS[name]


class AirSimClient:
    def __init__(self, timeout=3):
        self.timeout = timeout
        self.socket = None
        self.serial = 0

    def close(self):
        if self.socket:
            self.socket.close()
        self.socket = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def call(self, method, *args, timeout=None):
        try:
            if self.socket is None:
                self.socket = socket.create_connection(("127.0.0.1", 41451), self.timeout)
                self.socket.settimeout(self.timeout)
                self.unpacker = msgpack.Unpacker(raw=False, unicode_errors="surrogateescape", max_buffer_size=64 * 1024 * 1024)
            self.socket.settimeout(self.timeout if timeout is None else timeout)
            self.serial += 1
            self.socket.sendall(msgpack.packb([0, self.serial, method, list(args)], use_bin_type=True))
            while True:
                for response in self.unpacker:
                    if response[0] == 1 and response[1] == self.serial:
                        if response[2] is not None:
                            raise RuntimeError(str(response[2]))
                        return response[3]
                block = self.socket.recv(65536)
                if not block:
                    raise ConnectionError("AirSim RPC connection closed")
                self.unpacker.feed(block)
        except Exception:
            self.close()
            raise


def vector(value):
    result = [float(value[k]) for k in ("x_val", "y_val", "z_val")]
    if not all(math.isfinite(v) for v in result):
        raise ValueError("AirSim returned an invalid position")
    return result


def editor_log():
    if os.name != "nt":
        raise RuntimeError("UE world-coordinate calibration currently requires the local Windows editor")
    query = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-CimInstance Win32_Process | Where-Object {$_.Name -match '^(UE4Editor|UnrealEditor)\\.exe$'} | Select-Object -ExpandProperty CommandLine"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", query], capture_output=True, timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    commands = result.stdout.decode("utf-8", errors="replace")
    matches = re.findall(r'"([^"\r\n]+\.uproject)"', commands)
    if not matches:
        raise RuntimeError("Cannot locate the running Unreal editor project")
    project = Path(matches[0])
    logs = list((project.parent / "Saved" / "Logs").glob("*.log"))
    if not logs:
        raise RuntimeError("Unreal editor log is unavailable")
    return max(logs, key=lambda p: p.stat().st_mtime)


def calibrate(rpc):
    log = editor_log()
    token = "WEB_AIRSIM_ANCHOR_" + uuid.uuid4().hex + ":"
    code = (
        "import unreal,json; "
        "w=unreal.EditorLevelLibrary.get_game_world(); "
        "a=unreal.GameplayStatics.get_all_actors_of_class(w,unreal.PlayerStart)[0]; "
        "p=a.get_actor_location(); o=unreal.GameplayStatics.get_world_origin_location(w); "
        "s=unreal.GameplayStatics.get_all_actors_of_class(w,unreal.WorldSettings)[0]; "
        "unreal.log(" + repr(token) + "+json.dumps({'actor':a.get_name(),"
        "'position_m':[(p.x+o.x)/100,(p.y+o.y)/100,(p.z+o.z)/100],"
        "'scale':s.get_editor_property('world_to_meters')/100}))"
    )
    offset = log.stat().st_size
    if not rpc.call("simRunConsoleCommand", "py " + code):
        raise RuntimeError("UE Python coordinate calibration command was rejected")
    deadline = time.monotonic() + 6
    pending = b""
    while time.monotonic() < deadline:
        with log.open("rb") as stream:
            stream.seek(offset)
            block = stream.read()
            offset = stream.tell()
        pending += block
        for line in pending.decode("utf-8", errors="replace").splitlines():
            if token in line:
                start = line.index(token) + len(token)
                if line[start:].startswith("{") and line[start:].rstrip().endswith("}"):
                    anchor = json.loads(line[start:])
                    vector(rpc.call("simGetObjectPose", anchor["actor"])["position"])
                    return anchor
        time.sleep(.1)
    raise RuntimeError("UE world-coordinate calibration failed; check that the editor Python plugin is enabled")


class WorldFrame:
    """由运行中的UE参考点测量全局NED与世界米制坐标之间的关系。"""

    def __init__(self, client):
        self.client = client
        self.anchor = calibrate(client)
        self.scale = float(self.anchor['scale'])
        if not math.isfinite(self.scale) or self.scale <= 0:
            raise RuntimeError('UE坐标尺度无效')

    def vehicle(self, name):
        reference = vector(self.client.call('simGetObjectPose', self.anchor['actor'])['position'])
        offset = [a-b*self.scale for a,b in zip(self.anchor['position_m'], [reference[0],reference[1],-reference[2]])]
        world_ned = vector(self.client.call('simGetObjectPose', name)['position'])
        local_pose = self.client.call('simGetVehiclePose', name)
        local = vector(local_pose['position'])
        world = [a+b*self.scale for a,b in zip(offset,[world_ned[0],world_ned[1],-world_ned[2]])]
        origin = [a-b*self.scale for a,b in zip(world,[local[0],local[1],-local[2]])]
        return world, local, origin

    def ground_z(self, point, vehicle_name=None, remember=False):
        """测量起飞地面；基准保存在仿真机身运行态，支持跨Python调用。"""
        log = editor_log()
        token = 'WEB_UAV_GROUND_' + uuid.uuid4().hex + ':'
        x,y,z = point
        code = (
            "import unreal,json; w=unreal.EditorLevelLibrary.get_game_world(); "
            "o=unreal.GameplayStatics.get_world_origin_location(w); "
            "s=unreal.Vector("+repr(x*100)+"-o.x,"+repr(y*100)+"-o.y,"+repr((z+2)*100)+"-o.z); "
            "e=unreal.Vector(s.x,s.y,s.z-200000); "
            "ignored=unreal.GameplayStatics.get_all_actors_of_class(w,unreal.Pawn); "
            "a=next((a for a in ignored if a.get_name()=="+repr(vehicle_name)+"),None); "
            "prefix='CityLLM_FlyGroundZ='; "
            "cached=next((float(str(t)[len(prefix):]) for t in a.get_editor_property('tags') if str(t).startswith(prefix)),None) if a is not None and not "+repr(remember)+" else None; "
            "hit=unreal.SystemLibrary.line_trace_single(w,s,e,unreal.TraceTypeQuery.TRACE_TYPE_QUERY1,False,ignored,unreal.DrawDebugTrace.NONE,True) if cached is None else None; "
            "details=hit.to_tuple() if hit is not None else None; "
            "ground=cached if cached is not None else (details[5].z+o.z)/100 if details is not None and details[0] else None; "
            "a.set_editor_property('tags',[t for t in a.get_editor_property('tags') if not str(t).startswith(prefix)]+[unreal.Name(prefix+str(ground))]) if a is not None and ground is not None else None; "
            "unreal.log("+repr(token)+"+json.dumps({'ground_z_m':ground}))"
        )
        offset = log.stat().st_size
        if not self.client.call('simRunConsoleCommand', 'py '+code):
            raise RuntimeError('UE地面测量命令未执行')
        pending = b''
        deadline = time.monotonic()+6
        while time.monotonic()<deadline:
            with log.open('rb') as stream:
                stream.seek(offset);pending+=stream.read();offset=stream.tell()
            for line in pending.decode('utf-8',errors='replace').splitlines():
                if token in line:
                    body=line.split(token,1)[1]
                    if body.startswith('{') and body.rstrip().endswith('}'):
                        result=json.loads(body)['ground_z_m']
                        if result is None or not math.isfinite(float(result)):
                            raise RuntimeError('无法测量起飞地面高度')
                        return float(result)
            time.sleep(.1)
        raise RuntimeError('UE未返回起飞地面测量结果')


class AirSimFleet:
    """读取真实无人机位置与可用状态，不接管飞行。"""

    def __init__(self, client_factory=AirSimClient):
        self.client_factory = client_factory

    def read(self):
        """当前位置使用 UE 世界米制坐标，不使用配置中的出生点。"""
        try:
            with self.client_factory() as rpc:
                settings = json.loads(rpc.call("getSettingsString"))
                configured = settings.get("Vehicles", {})
                names = [name for name in rpc.call("listVehicles")
                         if configured.get(name, {}).get("VehicleType", "SimpleFlight")
                         in ("SimpleFlight", "PX4Multirotor")]
                if not names:
                    raise RuntimeError("AirSim 当前没有运行中的多旋翼无人机。")
                if (any(not isinstance(name, str) or not name.strip() or name != name.strip() for name in names)
                        or len(set(names)) != len(names)):
                    raise RuntimeError("AirSim 返回的无人机名称无效或重复。")

                frame = WorldFrame(rpc)
                fleet = []
                for name in names:
                    position, _, _ = frame.vehicle(name)
                    if len(position) != 3 or any(type(value) not in (int, float) or not math.isfinite(value) for value in position):
                        raise RuntimeError(f"无法读取 {name} 的有效世界坐标。")
                    state = rpc.call("getMultirotorState", name)
                    landed = state.get("landed_state")
                    if type(landed) is not int or landed not in (0, 1):
                        raise RuntimeError(f"无法确认 {name} 的落地状态，未生成指令。")
                    motion = rpc.call("simGetGroundTruthKinematics", name)
                    speed_m_s = math.hypot(*vector(motion["linear_velocity"])) * frame.scale
                    # 初版的候选规则，不把在线直接当成空闲；不接管飞行或任务锁。
                    fleet.append({"name": name, "position_m": position,
                                  "available": landed == 0 and speed_m_s <= 0.1})
                return sorted(fleet, key=lambda drone: drone["name"])
        except OSError as error:
            raise RuntimeError("无法读取 AirSim（127.0.0.1:41451），请确认仿真已启动且 RPC 可连接。") from error


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
    """低速到站，允许水平偏差0.75米、高度偏差0.5米、速度0.3米/秒。"""
    initial = vector(client.call("simGetVehiclePose", name)["position"])
    distance = math.dist(initial, destination)*scale
    deadline = time.monotonic() + max(15.0, distance*2 + 10.0)
    while True:
        current = vector(client.call("simGetVehiclePose", name)["position"])
        delta = [a-b for a,b in zip(destination,current)]
        horizontal = math.hypot(*delta[:2])*scale
        vertical = abs(delta[2])*scale
        velocity = vector(client.call("getMultirotorState",name)["kinematics_estimated"]["linear_velocity"])
        speed = math.hypot(*velocity)*scale
        if horizontal <= .75 and vertical <= .5 and speed <= .3:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"{name}未能稳定在目标位置：水平误差{horizontal:.2f}米，"
                               f"高度误差{vertical:.2f}米，速度{speed:.2f}米/秒")
        if horizontal <= .75 and vertical <= .5:
            _hold(client,name,destination[2],.5)
            continue
        velocity = [delta[0]*.8,delta[1]*.8]
        speed = math.hypot(*velocity)
        limit = 1.0/scale
        if speed > limit:
            velocity = [v*limit/speed for v in velocity]
        _action(client,"moveByVelocityZ",*velocity,destination[2],.25,0,
                {"is_rate": True,"yaw_or_rate": 0.0},name,timeout=5)


class SearchFlight:
    """持锁后使用；沿显式路径飞行，不会把指定道路替换成另一条最短路。"""

    # 保存搜索飞行所需连接、坐标转换和无人机名称。
    # 初始化控制状态；尚不解锁或启动无人机。
    def __init__(self, client, frame, name, origin):
        self.client, self.frame, self.name, self.origin = client, frame, name, origin
        self.controlling, self.z = False, None
        self.collision_timestamp = None

    # 核实飞行高度与净空后接管无人机，解锁并分段升至巡查高度。
    # 输入为当前位置、相对地面高度和校验回调；成功后稳定高度，失败抛出异常。
    def prepare(self, world, local, height, validate_altitude=lambda *_: None):
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
        # 分次升高，逐段核实实际高度。
        deadline = time.monotonic() + max(30, abs(local[2]-self.z)*frame.scale*2+15)
        while abs(local[2] - self.z) * frame.scale > 1.0:
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

    # 沿指定折线路点飞行，逐段检查相机状态、到达误差和碰撞。
    # 输入为世界 XY 路点、速度和回调；实际控制飞行。
    def fly(self, path, speed, checkpoint=lambda: None, observe=lambda *_: None):
        for segment, segment_speed in flight_segments(path, speed):
            # 一次执行最多20米的原折线，保持多路点平滑追踪。
            for chunk in _path_chunks(segment):
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
                horizontal_error = math.dist(actual, b)
                vertical_error = abs(local[2]-self.z)*self.frame.scale
                if completed is not True or horizontal_error > 1.5 or vertical_error > 1.5:
                    if horizontal_error > 2 or vertical_error > 2:
                        raise RuntimeError(f"巡查路径未到达终点：水平误差{horizontal_error:.2f}米，"
                                           f"高度误差{vertical_error:.2f}米，指令速度{execution_speed:g}米/秒，"
                                           f"AirSim返回值{completed!r}")
                    # True只表示路径跟踪结束，并不保证精确到站或停稳；附近偏差统一制动校准。
                    self.stop()
                    _settle(self.client, self.name, [points[-1]["x_val"], points[-1]["y_val"], self.z], self.frame.scale)
                    local = vector(self.client.call("simGetVehiclePose", self.name)["position"])
                    actual = [self.origin[i]+local[i]*self.frame.scale for i in (0, 1)]
                if math.dist(actual, b) > 1.5 or abs(local[2]-self.z)*self.frame.scale > 1.5:
                    raise RuntimeError(f"巡查路径校准后仍未到达指定路点：水平误差{math.dist(actual,b):.2f}米，"
                                       f"高度误差{abs(local[2]-self.z)*self.frame.scale:.2f}米")
                observe(actual)
                collision = self.client.call("simGetCollisionInfo", self.name)
                if collision.get("has_collided") and collision.get("time_stamp") != self.collision_timestamp:
                    raise RuntimeError("检测到碰撞，搜索停止")

    def land(self, home):
        """在各自起飞基点精确对位、降落，上锁并释放 API 控制。"""
        self.stop()
        destination = [(home[i] - self.origin[i]) / self.frame.scale for i in (0, 1)] + [self.z]
        _settle(self.client, self.name, destination, self.frame.scale)
        ground = self.frame.ground_z([home[0], home[1], self.origin[2]], self.name)
        position = vector(self.client.call("simGetVehiclePose", self.name)["position"])
        height = self.origin[2] - position[2]*self.frame.scale - ground
        # AirSim land 的下降速度为0.2米/秒；局部NED原点可能高于地面，不能用abs(self.z)估计航程。
        timeout = max(30.0, max(0.0, height)/0.2 + 30.0)
        completed = self.client.call("land", timeout, self.name, timeout=timeout + 5)
        # SimpleFlight可能在上锁后才更新landed_state；先确认近地且持续静止，再上锁。
        deadline, stable_since = time.monotonic() + 3.0, None
        while True:
            state = self.client.call("getMultirotorState", self.name)
            position = vector(self.client.call("simGetVehiclePose", self.name)["position"])
            height = self.origin[2] - position[2]*self.frame.scale - ground
            speed = math.hypot(*vector(state["kinematics_estimated"]["linear_velocity"]))*self.frame.scale
            now = time.monotonic()
            if (completed is True or state["landed_state"] == 0) and abs(height) <= .75 and speed <= .15:
                if stable_since is None:
                    stable_since = now
                if now - stable_since >= 1.0:
                    break
            else:
                stable_since = None
            if now >= deadline:
                raise RuntimeError(f"无人机未完成降落：离地高度{height:.2f}米，速度{speed:.2f}米/秒，"
                                   f"降落时限{timeout:.1f}秒，AirSim返回值{completed!r}")
            time.sleep(.1)
        self.client.call("armDisarm", False, self.name)
        deadline = time.monotonic() + 3.0
        while self.client.call("getMultirotorState", self.name)["landed_state"] != 0:
            if time.monotonic() >= deadline:
                raise RuntimeError("无人机着地上锁后仍未确认落地状态")
            time.sleep(.1)
        self.client.call("enableApiControl", False, self.name)
        self.controlling = False
        if (math.dist(position[:2], destination[:2])*self.frame.scale > 1.0
                or abs(height) > 2.0):
            raise RuntimeError("无人机未在起飞基点落地")

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
