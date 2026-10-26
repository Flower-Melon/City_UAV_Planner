"""沿无人机二维拓扑图规划 XY 最短路径及飞行速度分段。"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from functools import lru_cache
import heapq
import json
import math
from numbers import Real
from pathlib import Path
from typing import Sequence

__all__ = ["plan_flight_path", "flight_segments", "NoPathError"]

Point = tuple[float, float]
_TIE_TOLERANCE_M = 1e-9
_CELL_SIZE_M = 50.0
_MAP_PATH = Path(__file__).resolve().parents[2] / "Map" / "roads" / "uav-road-network.json"
_SAMPLE_SPACING_M = 5.0
_CORNER_WINDOW_M = 5.0
_ACCELERATION_M_S2 = 3.0
_LATERAL_ACCELERATION_M_S2 = 3.0
_CORNER_SPEED_M_S = 5.0
_APPROACH_SPEED_M_S = 1.0


class NoPathError(ValueError):
    """起点与目标的最近投影点之间不存在拓扑路径。"""


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
    """无人机双向图及空间索引；临时接入边仅存在于单次查询中。"""

    def __init__(self, data: dict):
        if data.get("coordinate_system") != "UE_WORLD_XY_METRES":
            raise ValueError("无人机路网必须使用 UE_WORLD_XY_METRES 坐标")
        nodes = data["nodes"]
        node_ids = {node["id"]: i for i, node in enumerate(nodes)}
        if len(node_ids) != len(nodes):
            raise ValueError("道路图存在重复节点编号")
        self.node_points = [_point(node["position_xy_m"], "图节点") for node in nodes]
        self.edges: list[_Edge] = []
        self.edge_ids = []
        self.adjacency = defaultdict(list)
        self.cells = defaultdict(list)
        edge_ids = set()
        for record in data["edges"]:
            if record["id"] in edge_ids:
                raise ValueError(f"无人机路网存在重复边编号 {record['id']}")
            edge_ids.add(record["id"])
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
            self.edge_ids.append(record["id"])
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
        _, targets = self.nearest(target)

        # 每个投影点使用独立虚拟节点，避免把同坐标的不同拓扑节点永久合并。
        extra = defaultdict(list)
        # -2 仅作为搜索终点标记；实际终点为被选中的目标投影点。
        virtual_points = {-1: start}
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
                # 起点保留直线接入段；目标投影点以零成本连接搜索终点。
                access_distance = math.dist(start, projection) if side == 0 else 0.0
                extra[terminal].append((virtual, None, 0.0, access_distance))
                extra[virtual].append((terminal, None, 0.0, access_distance))
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
            raise NoPathError("无人机与目标的最近投影点之间不存在道路拓扑路径")

        steps = []
        node = -2
        while node != -1:
            parent, arc = previous[node]
            steps.append((parent, arc))
            node = parent
        path = [start]
        for parent, (neighbor, index, begin, end) in reversed(steps):
            if neighbor == -2:
                break
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
    """导航与搜索共用同一份只读无人机路网。"""
    with _MAP_PATH.open(encoding="utf-8-sig") as source:
        return _RoadGraph(json.load(source))


def plan_flight_path(
    current_position: Sequence[float],
    target_position: Sequence[float],
    flight_speed: float = 25.0,
) -> tuple[list[Point], list[tuple[list[Point], float]]]:
    """一次返回无人机拓扑最短路径及全部飞行分段，不连接仿真或保存文件。

    Args:
        current_position: 当前 UE 世界 (x, y)，单位米，允许位于图外。
        target_position: 目标 UE 世界 (x, y)，图外目标以最近边投影点为终点。
        flight_speed: 正的有限巡航速度上限，单位米/秒，默认 25.0。

    Returns:
        (path, segments)：path 是原始最短 XY 路径；segments 是
        [(XY 路径点列表, 速度), ...]，供调用方按顺序执行。
        无需水平移动时 segments 为空列表。

    Raises:
        ValueError: 坐标、速度或地图无效。
        NoPathError: 起点与目标的最近投影点之间不存在道路拓扑路径。
        OSError: 无法读取项目 Map/roads/uav-road-network.json。

    地图首次调用时读取并在进程内缓存。起点在图外时接入最近边，
    目标在图外时仅到达最近投影点；所有边均按双向通行，长度由 XY
    路径坐标计算。仅规划 XY，高度由调用方设置。
    """
    if isinstance(flight_speed, bool) or not isinstance(flight_speed, Real):
        raise ValueError("飞行速度必须是大于0的有限数值，单位为米/秒")
    try:
        speed = float(flight_speed)
    except (ValueError, OverflowError):
        raise ValueError("飞行速度必须是大于0的有限数值，单位为米/秒") from None
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError("飞行速度必须是大于0的有限数值，单位为米/秒")
    start = _point(current_position, "无人机当前位置")
    target = _point(target_position, "目标位置")
    path = _default_graph().route(start, target)
    return path, _flight_segments(path, speed)


def _resample(path):
    cleaned = [tuple(path[0])]
    for point in path[1:]:
        point = tuple(point)
        if math.dist(cleaned[-1], point) >= 0.01:
            cleaned.append(point)
    endpoint = tuple(path[-1])
    if cleaned[-1] != endpoint:
        if len(cleaned) == 1:
            cleaned.append(endpoint)
        else:
            cleaned[-1] = endpoint
    points = [cleaned[0]]
    for a, b in zip(cleaned, cleaned[1:]):
        count = max(1, math.ceil(math.dist(a, b) / _SAMPLE_SPACING_M))
        for i in range(1, count + 1):
            points.append(b if i == count else
                          (a[0] + (b[0]-a[0])*i/count, a[1] + (b[1]-a[1])*i/count))
    return points


def _position(points, offsets, distance):
    index = min(len(points)-2, max(0, bisect_right(offsets, distance)-1))
    fraction = (distance-offsets[index]) / (offsets[index+1]-offsets[index])
    a, b = points[index:index+2]
    return (a[0] + (b[0]-a[0])*fraction, a[1] + (b[1]-a[1])*fraction)


def _flight_segments(path, cruise_speed):
    """返回 [(XY折线点列表, 米/秒速度), ...]，保留起点、拐点和精确终点。

    纵向加减速和横向加速度使用保守的初始估计值，具体参数需结合仿真验证。
    """
    points = _resample(path)
    if len(points) < 2:
        return []
    offsets = [0.0]
    for a, b in zip(points, points[1:]):
        offsets.append(offsets[-1] + math.dist(a, b))
    limits = [cruise_speed] * len(points)
    for i in range(1, len(points)-1):
        before = _position(points, offsets, max(0.0, offsets[i]-_CORNER_WINDOW_M))
        after = _position(points, offsets, min(offsets[-1], offsets[i]+_CORNER_WINDOW_M))
        incoming = (points[i][0]-before[0], points[i][1]-before[1])
        outgoing = (after[0]-points[i][0], after[1]-points[i][1])
        left, right = math.hypot(*incoming), math.hypot(*outgoing)
        if min(left, right) < 0.5:
            continue
        cosine = sum(a*b for a, b in zip(incoming, outgoing)) / (left*right)
        angle = math.acos(max(-1.0, min(1.0, cosine)))
        if angle > 0.01:
            radius = min(left, right) / angle
            limits[i] = min(limits[i], math.sqrt(_LATERAL_ACCELERATION_M_S2*radius))
            if angle >= math.pi/4:
                limits[i] = min(limits[i], _CORNER_SPEED_M_S)
    limits[-1] = min(cruise_speed, _APPROACH_SPEED_M_S)
    # 反向传播制动距离 v²/(2a)，再正向限制起步和出弯加速。
    for i in range(len(points)-2, -1, -1):
        limits[i] = min(limits[i], math.sqrt(limits[i+1]**2 +
                        2*_ACCELERATION_M_S2*(offsets[i+1]-offsets[i])))
    limits[0] = min(limits[0], _CORNER_SPEED_M_S)
    for i in range(1, len(points)):
        limits[i] = min(limits[i], math.sqrt(limits[i-1]**2 +
                        2*_ACCELERATION_M_S2*(offsets[i]-offsets[i-1])))
    bands = sorted({min(cruise_speed, v) for v in (1, 2, 3, 5, 8, 12, 18, 25)} | {cruise_speed})
    segments = []
    for i in range(len(points)-1):
        cap = min(limits[i], limits[i+1])
        speed = max((v for v in bands if v <= cap), default=cap)
        if segments and segments[-1][1] == speed:
            segments[-1][0].append(points[i+1])
        else:
            segments.append(([points[i], points[i+1]], speed))
    return segments


def flight_segments(path, flight_speed):
    """为已确定的道路巡查折线生成原有加减速分段，不再重新寻路。"""
    if isinstance(flight_speed, bool) or not isinstance(flight_speed, Real) or not math.isfinite(flight_speed) or flight_speed <= 0:
        raise ValueError("飞行速度必须是大于0的有限数值")
    points = [_point(p, "路径点") for p in path]
    if not points:
        raise ValueError("路径不能为空")
    return _flight_segments(points, float(flight_speed))
