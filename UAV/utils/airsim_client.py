"""AirSim通信、坐标转换与共用任务管理，仅依赖msgpack。"""

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
