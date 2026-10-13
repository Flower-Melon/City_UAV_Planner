"""沿道路拓扑图规划无人机的 XY 最短路径，仅使用 Python 标准库。"""

from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
import heapq
import json
import math
from pathlib import Path
from typing import Sequence

__all__ = ["find_shortest_path", "NoPathError"]

Point = tuple[float, float]
_ON_GRAPH_TOLERANCE_M = 1e-4
_TIE_TOLERANCE_M = 1e-9
_CELL_SIZE_M = 50.0
_MAP_PATH = Path(__file__).resolve().parents[2] / "Map" / "road_network.json"


class NoPathError(ValueError):
    """最近接入位置与目标位置之间不存在拓扑路径。"""


def _point(value: Sequence[float], name: str) -> Point:
    try:
        if len(value) != 2 or any(isinstance(v, (bool, str, bytes)) for v in value):
            raise ValueError
        result = (float(value[0]), float(value[1]))
        if not all(math.isfinite(v) for v in result):
            raise ValueError
        return result
    except (TypeError, ValueError, OverflowError, IndexError, KeyError) as error:
        raise ValueError(f"{name} 必须是两个有限数值组成的 (x, y)，单位为米") from error


class _Edge:
    def __init__(self, source: int, target: int, points: tuple[Point, ...]):
        self.source, self.target, self.points = source, target, points
        self.offsets = [0.0]
        for a, b in zip(points, points[1:]):
            self.offsets.append(self.offsets[-1] + math.dist(a, b))
        self.length = self.offsets[-1]
        self.bounds = (
            min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points),
        )

    def section(self, start: float, end: float, first: Point, last: Point) -> list[Point]:
        low, high = sorted((start, end))
        interior = [p for p, offset in zip(self.points, self.offsets) if low < offset < high]
        if start > end:
            interior.reverse()
        return [first, *interior, last]


class _RoadGraph:
    """只读图及空间索引；每次查询的临时接入边仅存在于局部变量中。"""

    def __init__(self, data: dict):
        rules = data.get("routing_rules", {})
        if rules.get("distance_metric", "xy") != "xy":
            raise ValueError("道路图必须使用 XY 距离")
        if rules.get("uav", {}).get("direction", "both") != "both":
            raise ValueError("道路图必须允许无人机双向通行")
        nodes = data["nodes"]
        node_ids = {node["id"]: i for i, node in enumerate(nodes)}
        if len(node_ids) != len(nodes):
            raise ValueError("道路图存在重复节点编号")
        self.node_points = [_point(node["position_m"][:2], "图节点") for node in nodes]
        self.edges: list[_Edge] = []
        self.adjacency = defaultdict(list)
        self.cells = defaultdict(list)
        for record in data["edges"]:
            if record.get("enabled") is False:
                continue
            points = tuple(_point(p, "边折线点") for p in record["polyline_xy_m"])
            if len(points) < 2:
                raise ValueError(f"边 {record['id']} 至少需要两个折线点")
            try:
                source = node_ids[record["from_node_id"]]
                target = node_ids[record["to_node_id"]]
            except KeyError as error:
                raise ValueError(f"边 {record['id']} 引用了不存在的节点") from error
            if points[0] != self.node_points[source] or points[-1] != self.node_points[target]:
                raise ValueError(f"边 {record['id']} 的端点与图节点坐标不一致")
            edge = _Edge(source, target, points)
            index = len(self.edges)
            self.edges.append(edge)
            # 弧记录：终点、边索引、沿原折线的起止偏移。
            self.adjacency[source].append((target, index, 0.0, edge.length))
            self.adjacency[target].append((source, index, edge.length, 0.0))
            x0, y0, x1, y1 = edge.bounds
            for x in range(math.floor(x0 / _CELL_SIZE_M), math.floor(x1 / _CELL_SIZE_M) + 1):
                for y in range(math.floor(y0 / _CELL_SIZE_M), math.floor(y1 / _CELL_SIZE_M) + 1):
                    self.cells[x, y].append(index)
        if not self.edges:
            raise ValueError("道路图没有可通行的边")

    @staticmethod
    def _box_distance(point: Point, bounds: tuple[float, float, float, float]) -> float:
        x0, y0, x1, y1 = bounds
        return math.hypot(max(x0 - point[0], 0.0, point[0] - x1),
                          max(y0 - point[1], 0.0, point[1] - y1))

    def nearest(self, point: Point) -> tuple[float, list[tuple[int, float, Point]]]:
        """逐段作垂足投影，超出线段范围时取端点；保留等距接入点。"""
        queue = []
        for x, y in self.cells:
            bounds = (x * _CELL_SIZE_M, y * _CELL_SIZE_M,
                      (x + 1) * _CELL_SIZE_M, (y + 1) * _CELL_SIZE_M)
            queue.append((self._box_distance(point, bounds), x, y))
        heapq.heapify(queue)
        seen = set()
        best = math.inf
        candidates = []
        while queue:
            lower_bound, x, y = heapq.heappop(queue)
            if lower_bound > best + _TIE_TOLERANCE_M:
                break
            for index in self.cells[x, y]:
                if index in seen:
                    continue
                seen.add(index)
                edge = self.edges[index]
                if self._box_distance(point, edge.bounds) > best + _TIE_TOLERANCE_M:
                    continue
                for segment, (a, b) in enumerate(zip(edge.points, edge.points[1:])):
                    dx, dy = b[0] - a[0], b[1] - a[1]
                    squared_length = dx * dx + dy * dy
                    fraction = (max(0.0, min(1.0, ((point[0] - a[0]) * dx +
                                                 (point[1] - a[1]) * dy) / squared_length))
                                if squared_length else 0.0)
                    projection = (a if fraction == 0.0 else b if fraction == 1.0
                                  else (a[0] + fraction * dx, a[1] + fraction * dy))
                    distance = math.dist(point, projection)
                    offset = edge.offsets[segment] + fraction * (edge.offsets[segment + 1] - edge.offsets[segment])
                    if distance < best - _TIE_TOLERANCE_M:
                        best, candidates = distance, [(index, offset, projection)]
                    elif distance <= best + _TIE_TOLERANCE_M:
                        best = min(best, distance)
                        candidates.append((index, offset, projection))
        # 同一折线顶点可能由相邻两段重复投影。
        unique = {}
        for index, offset, projection in candidates:
            if math.dist(point, projection) <= best + _TIE_TOLERANCE_M:
                unique[index, offset] = (index, offset, projection)
        return best, list(unique.values())

    def route(self, start: Point, target: Point) -> list[Point]:
        _, starts = self.nearest(start)
        target_distance, targets = self.nearest(target)
        if target_distance > _ON_GRAPH_TOLERANCE_M:
            raise ValueError(f"目标位置不在道路拓扑图边上（距最近边 {target_distance:.6f} 米）")
        if start == target:
            return [start]

        # 每个投影点使用独立虚拟节点，避免把同坐标的不同拓扑节点永久合并。
        extra = defaultdict(list)
        virtual_points = {-1: start, -2: target}
        attachments = defaultdict(lambda: [[], []])
        next_id = -3
        for side, candidates in enumerate((starts, targets)):
            terminal = -1 if side == 0 else -2
            for index, offset, projection in candidates:
                virtual = next_id
                next_id -= 1
                virtual_points[virtual] = projection
                edge = self.edges[index]
                attachments[index][side].append((virtual, offset))
                # 无边索引的弧仅用于原位置与投影点的直线接入。
                extra[terminal].append((virtual, None, 0.0, math.dist(virtual_points[terminal], projection)))
                extra[virtual].append((terminal, None, 0.0, math.dist(virtual_points[terminal], projection)))
                for node, endpoint in ((edge.source, 0.0), (edge.target, edge.length)):
                    extra[virtual].append((node, index, offset, endpoint))
                    extra[node].append((virtual, index, endpoint, offset))
        for index, (edge_starts, edge_targets) in attachments.items():
            for source, start_offset in edge_starts:
                for destination, target_offset in edge_targets:
                    extra[source].append((destination, index, start_offset, target_offset))
                    extra[destination].append((source, index, target_offset, start_offset))

        distances = {-1: 0.0}
        previous = {}
        queue = [(0.0, -1)]
        while queue:
            distance, node = heapq.heappop(queue)
            if distance != distances[node]:
                continue
            if node == -2:
                break
            for arc in (*self.adjacency.get(node, ()), *extra.get(node, ())):
                neighbor, index, begin, end = arc
                candidate = distance + abs(end - begin)
                if candidate < distances.get(neighbor, math.inf):
                    distances[neighbor] = candidate
                    previous[neighbor] = (node, arc)
                    heapq.heappush(queue, (candidate, neighbor))
        else:
            raise NoPathError("无人机最近接入位置与目标位置之间不存在道路拓扑路径")

        steps = []
        node = -2
        while node != -1:
            parent, arc = previous[node]
            steps.append((parent, arc))
            node = parent
        path = [start]
        for parent, (neighbor, index, begin, end) in reversed(steps):
            first = virtual_points[parent] if parent < 0 else self.node_points[parent]
            last = virtual_points[neighbor] if neighbor < 0 else self.node_points[neighbor]
            points = ([first, last] if index is None
                      else self.edges[index].section(begin, end, first, last))
            for point in points:
                if point != path[-1]:
                    path.append(point)
        return path


@lru_cache(maxsize=1)
def _default_graph() -> _RoadGraph:
    with _MAP_PATH.open(encoding="utf-8") as source:
        return _RoadGraph(json.load(source))


def find_shortest_path(current_position: Sequence[float], target_position: Sequence[float]) -> list[Point]:
    """返回从无人机当前位置到道路边上目标点的最短 XY 飞行路径。

    Args:
        current_position: 当前 (x, y)，UE 世界坐标，单位米；允许位于图外。
        target_position: 目标 (x, y)，同一坐标系；必须在可通行拓扑边上。

    Returns:
        新的 list[tuple[float, float]]，按飞行顺序包含原起点、最近边投影点、
        沿途折线点和原目标。相邻重复点被移除；起终点相同时返回单点列表。
        接入段也计入最短距离，图内路径沿所有已启用边双向通行。

    Raises:
        ValueError: 坐标无效，或目标距可通行边超过 0.0001 米，或地图无效。
        NoPathError: 最近接入点与目标之间不存在道路拓扑路径。
        OSError: 无法读取项目 Map/road_network.json。

    地图首次调用时读取并在进程内缓存；入参、返回值与临时导航节点不写入文件。
    仅规划 XY，不进行高度规划、AirSim 坐标转换或动态避障。
    """
    start = _point(current_position, "无人机当前位置")
    target = _point(target_position, "目标位置")
    return _default_graph().route(start, target)
