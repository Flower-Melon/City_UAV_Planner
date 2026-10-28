"""生成异常车辆：包含 AirSim 通信、机动车道闭环规划和 UE 控制脚本加载。"""

import heapq
import json
import math
import socket
import sys
import tempfile
import time
import uuid
from functools import lru_cache
from pathlib import Path

import msgpack
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "Abnormal.vehicle"


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


_VEHICLE_SETTINGS = {
    "max_spawn_snap_m": 5.0,
    "cruise_speed_kmh": 75.0,
    "surge_speed_kmh": 104.4,
    "brake_speed_kmh": 36.0,
    "wrong_way_speed_kmh": 75.0,
    "weave_amplitude_m": 3.0,
    "weave_period_s": 5.0,
    "wheelbase_m": 2.7,
    "acceleration_m_s2": 5.5,
    "braking_m_s2": 8.5,
    "jerk_m_s3": 14.0,
    "steering_limit_deg": 36.0,
    "steering_rate_deg_s": 80.0,
    "lateral_acceleration_m_s2": 5.4,
    "traffic_clearance_m": 0.5,
    "road_height_tolerance_m": 0.12,
}


def request(client, operation, _controller=None, _module_name="city_abnormal_runtime",
            _registry="_vehicles", **parameters):
    # 请求和结果独立命名；多个 Python 进程也不会相互覆盖结果。
    with tempfile.TemporaryDirectory(prefix="city-abnormal-") as directory:
        source = Path(directory) / "request.json"
        result = Path(directory) / "response.json"
        source.write_text(json.dumps({"operation": operation, "request_id": uuid.uuid4().hex,
                                      **parameters}, allow_nan=False), encoding="utf-8")
        controller = Path(_controller) if _controller is not None else Path(__file__).with_name("set_vehicle_behavior.py")
        bootstrap = "\n".join([
            "import sys,os,importlib.util",
            "path=" + repr(str(controller)),
            "name=" + repr(_module_name),
            "registry=" + repr(_registry),
            "r=sys.modules.get(name)",
            "def _alive(vehicle):",
            "    try: return r.unreal.SystemLibrary.is_valid(vehicle.actor)",
            "    except Exception: return False",
            "if r is not None: setattr(r,registry,{n:v for n,v in getattr(r,registry).items() if _alive(v)})",
            "stamp=os.stat(path).st_mtime_ns",
            "if r is None or getattr(r,'_source_stamp',None)!=stamp:",
            "    if r is not None and getattr(r,registry): raise RuntimeError('控制脚本已改变，请结束 Play 后重新启动仿真')",
            "    if r is not None and hasattr(r,'shutdown'): r.shutdown()",
            "    if r is not None and r._tick_handle is not None: r.unreal.unregister_slate_post_tick_callback(r._tick_handle)",
            "    spec=importlib.util.spec_from_file_location(name,path)",
            "    r=importlib.util.module_from_spec(spec)",
            "    sys.modules[name]=r",
            "    spec.loader.exec_module(r)",
            "    r._source_stamp=stamp",
            "r.dispatch(" + repr(str(source)) + "," + repr(str(result)) + ")",
        ])
        code = "exec(" + repr(bootstrap) + ")"
        try:
            accepted = client.call("simRunConsoleCommand", "py " + code, timeout=45)
        except OSError as error:
            raise RuntimeError("AirSim 通信失败，请确认仿真已 Play 且 RPC 可连接；操作结果需核实后再重试") from error
        if accepted is not True:
            raise RuntimeError("UE 未接受异常场景命令，请启用 PythonScriptPlugin")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if result.exists():
                response = json.loads(result.read_text(encoding="utf-8"))
                if not response.get("ok"):
                    raise RuntimeError(response.get("error", "异常场景操作失败"))
                return response.get("result")
            time.sleep(0.05)
        raise RuntimeError("UE 未返回异常场景执行结果，请检查 Output Log；操作结果需核实后再重试")


NETWORK = Path(__file__).resolve().parents[2] / "Map/roads/road_network.json"


def position_xy(value):
    """拒绝字符串、布尔值和非有限坐标，避免在连接仿真后才发现输入错误。"""
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError("position 必须为 UE 世界坐标 (x, y)，单位米")
    if any(isinstance(v, (bool, str, bytes)) for v in value):
        raise ValueError("position 必须包含两个有限数值")
    try:
        result = tuple(float(v) for v in value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("position 必须包含两个有限数值") from None
    if not all(math.isfinite(v) for v in result):
        raise ValueError("position 必须包含两个有限数值")
    return result


class RoadGraph:
    def __init__(self, network):
        driving = {lane["id"] for lane in network["lanes"] if lane["type"] == "driving"}
        roads = [e for e in network["edges"] if e.get("lane_id") in driving]
        road_ids = {e["id"] for e in roads}
        nodes = {node for e in roads for node in (e["from_node_id"], e["to_node_id"])}
        self.edges = {}
        self.outgoing = {}
        for e in network["edges"]:
            if e["id"] not in road_ids and not (e.get("kind") == "connection"
                                      and e["from_node_id"] in nodes and e["to_node_id"] in nodes):
                continue
            points = [tuple(p) for p in e["polyline_xy_m"]]
            if len(points) < 2 or any(not all(math.isfinite(v) for v in p) for p in points):
                continue
            edge = dict(e, points=points, length=sum(math.dist(a, b) for a, b in zip(points, points[1:])))
            self.edges[e["id"]] = edge
            self.outgoing.setdefault(e["from_node_id"], []).append(edge)
        for edges in self.outgoing.values():
            edges.sort(key=lambda e: e["id"])
        self.roads = sorted((self.edges[e["id"]] for e in roads if e["id"] in self.edges),
                            key=lambda e: e["id"])
        if not self.roads:
            raise ValueError("路网没有机动车道")
        self.lines = [LineString(e["points"]) for e in self.roads]
        self.index = STRtree(self.lines)

    def _return_path(self, source, target):
        queue = [(0.0, source)]
        best = {source: 0.0}
        previous = {}
        while queue:
            cost, node = heapq.heappop(queue)
            if cost != best[node]:
                continue
            if node == target:
                path = []
                while node != source:
                    edge = previous[node]
                    path.append(edge)
                    node = edge["from_node_id"]
                return list(reversed(path))
            for edge in self.outgoing.get(node, ()):
                end = edge["to_node_id"]
                candidate = cost + max(edge["length"], 1e-6)
                if candidate < best.get(end, math.inf):
                    best[end] = candidate
                    previous[end] = edge
                    heapq.heappush(queue, (candidate, end))
        raise ValueError("指定出生点所在车道无法沿机动车道连接形成闭环")

    def loop(self, position, max_snap=5.0):
        point = Point(position_xy(position))
        # 距离并列时按真实车道边 ID 选择，避免 STRtree 的并列顺序影响路线。
        near = self.index.query_nearest(point, all_matches=True)
        i = min((int(i) for i in near), key=lambda i: (self.lines[i].distance(point), self.roads[i]["id"]))
        edge, line = self.roads[i], self.lines[i]
        distance = line.distance(point)
        if distance > max_snap:
            raise ValueError(f"出生点距离最近机动车道 {distance:.2f} 米，超过允许的 {max_snap:g} 米")
        along = line.project(point)
        spawn = tuple(line.interpolate(along).coords[0])
        before, after = [edge["points"][0]], []
        traversed = 0.0
        for a, b in zip(edge["points"], edge["points"][1:]):
            end = traversed + math.dist(a, b)
            if end <= along:
                before.append(b)
            elif traversed < along:
                after.append(b)
            else:
                after.append(b)
            traversed = end
        before.append(spawn)
        parts = [[spawn] + after]
        returning = self._return_path(edge["to_node_id"], edge["from_node_id"])
        parts.extend(e["points"] for e in returning)
        parts.append(before)
        points = []
        for part in parts:
            if points and math.dist(points[-1], part[0]) > 0.1:
                raise ValueError("道路连接存在超过 0.1 米的几何缺口，无法生成连续路线")
            for xy in part:
                if not points or math.dist(points[-1], xy) > 1e-6:
                    points.append(xy)
        if len(points) < 4 or sum(math.dist(a, b) for a, b in zip(points, points[1:])) < 20:
            raise ValueError("出生点所在道路没有足够长度的可行闭环")
        return {"points": points, "spawn_xy": spawn, "snap_distance_m": distance,
                "edge_id": edge["id"], "edge_ids": [edge["id"]] + [e["id"] for e in returning]}


@lru_cache(maxsize=1)
def road_graph():
    return RoadGraph(json.loads(NETWORK.read_text(encoding="utf-8")))


def spawn_vehicle(position) -> str:
    """position 为 UE 世界 (x, y) 米制坐标；返回可供 AirSim 查询的唯一车辆 ID。

    每次调用新增一辆车，独立生成有向机动车道闭环，初始停车。
    输入允许在 5 米范围内贴合最近车道；无法形成闭环时抛出异常。
    返回前已确认车辆在运行中的 UE 世界生成并注册控制器。
    """
    position = position_xy(position)
    settings = _VEHICLE_SETTINGS.copy()
    route = road_graph().loop(position, settings["max_spawn_snap_m"])
    with AirSimClient(timeout=45) as client:
        prepared = request(client, "prepare", route=route, settings=settings)
        name = prepared["vehicle_id"]
        spawned = None
        attached = False
        try:
            spawned = client.call("simSpawnObject", name, "Vh_Car_ToyotaPrius_NOrig",
                                  prepared["pose"], {"x_val": 1.0, "y_val": 1.0, "z_val": 1.0},
                                  False, False, timeout=45)
            if spawned != name:
                raise RuntimeError(f"车辆对象名称发生冲突：期望 {name}，实际 {spawned!r}")
            request(client, "attach", vehicle_id=name)
            attached = True
            return name
        finally:
            if not attached:
                # 失败只清理本次创建的目标，不影响已有车辆。
                try:
                    if spawned:
                        client.call("simDestroyObject", spawned)
                    request(client, "release", vehicle_id=name)
                except Exception:
                    pass


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("x", type=float, help="UE 世界 X，米")
    parser.add_argument("y", type=float, help="UE 世界 Y，米")
    args = parser.parse_args()
    print(spawn_vehicle((args.x, args.y)))
