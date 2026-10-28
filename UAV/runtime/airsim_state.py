"""AirSim 机队状态、UE 坐标标定与地面测量。"""

import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from .airsim_client import AirSimClient


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


def _run_ue_json(client, code, token, rejected_message, timeout_message):
    """执行 UE Python 命令并读取带标记的 JSON 日志结果。"""
    log = editor_log()
    offset = log.stat().st_size
    if not client.call("simRunConsoleCommand", "py " + code):
        raise RuntimeError(rejected_message)
    deadline = time.monotonic() + 6
    pending = b""
    while time.monotonic() < deadline:
        with log.open("rb") as stream:
            stream.seek(offset)
            pending += stream.read()
            offset = stream.tell()
        for line in pending.decode("utf-8", errors="replace").splitlines():
            if token in line:
                body = line.split(token, 1)[1]
                if body.startswith("{") and body.rstrip().endswith("}"):
                    return json.loads(body)
        time.sleep(.1)
    raise RuntimeError(timeout_message)


def calibrate(rpc):
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
    anchor = _run_ue_json(
        rpc, code, token,
        "UE Python coordinate calibration command was rejected",
        "UE world-coordinate calibration failed; check that the editor Python plugin is enabled",
    )
    vector(rpc.call("simGetObjectPose", anchor["actor"])["position"])
    return anchor


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
        result = _run_ue_json(
            self.client, code, token,
            'UE地面测量命令未执行', 'UE未返回起飞地面测量结果',
        )['ground_z_m']
        if result is None or not math.isfinite(float(result)):
            raise RuntimeError('无法测量起飞地面高度')
        return float(result)


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
