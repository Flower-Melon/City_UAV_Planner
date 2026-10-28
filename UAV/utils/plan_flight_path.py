"""无人机三维导航、速度规划及道路巡查共用的路网读取。"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from functools import lru_cache
import heapq
import json
import math
from numbers import Real
from pathlib import Path

from shapely.geometry import LineString, Point, box, shape
from shapely.strtree import STRtree

__all__ = ["plan_flight_path", "flight_route", "NoPathError"]
MAP_DIR = Path(__file__).resolve().parents[2] / "Map"
_GAP_SPEED = 3.0
_VERTICAL_SPEED = 2.0


class NoPathError(ValueError):
    """给定高度和净空条件下没有可执行航线。"""


def _positive(value, name):
    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name}必须是正的有限数值")
    return float(value)


def _coordinates(value, dimensions, name):
    try:
        if len(value) != dimensions or any(isinstance(v, (bool, str, bytes)) for v in value):
            raise ValueError
        point = tuple(float(v) for v in value)
        if not all(math.isfinite(v) for v in point):
            raise ValueError
        return point
    except (TypeError, ValueError, OverflowError, IndexError, KeyError) as error:
        raise ValueError(f"{name}必须包含{dimensions}个有限坐标，单位米") from error


def _point(value, name):
    return _coordinates(value, 2, name)


def _xyz(value, name):
    return _coordinates(value, 3, name)


class _Edge:
    def __init__(self, record, node_ids):
        self.source = node_ids[record["from_node_id"]]
        self.target = node_ids[record["to_node_id"]]
        self.points = tuple(_point(p, "路段坐标") for p in record["polyline_xy_m"])
        if len(self.points) < 2:
            raise ValueError("路段至少需要两个坐标")
        self.offsets = [0.0]
        for a, b in zip(self.points, self.points[1:]):
            self.offsets.append(self.offsets[-1] + math.dist(a, b))
        self.length = self.offsets[-1]
        if self.length <= 0:
            raise ValueError("路段长度必须大于零")


class _RoadGraph:
    """原二维道路图，用于选街区、覆盖道路和定位拍照节点。"""

    def __init__(self, data):
        if data.get("coordinate_system") != "UE_WORLD_XY_METRES":
            raise ValueError("道路图必须使用UE世界XY米制坐标")
        self.node_points = [_point(n["position_xy_m"], "道路节点") for n in data["nodes"]]
        ids = {n["id"]: i for i, n in enumerate(data["nodes"])}
        self.edge_ids = [e["id"] for e in data["edges"]]
        if len(ids) != len(self.node_points) or len(set(self.edge_ids)) != len(self.edge_ids):
            raise ValueError("道路图编号重复")
        self.edges = [_Edge(e, ids) for e in data["edges"]]
        for e in self.edges:
            if (
                e.points[0] != self.node_points[e.source]
                or e.points[-1] != self.node_points[e.target]
            ):
                raise ValueError("道路端点与节点坐标不一致")


@lru_cache(maxsize=1)
def _default_graph():
    with (MAP_DIR / "roads/uav_road_network.json").open(encoding="utf-8-sig") as source:
        return _RoadGraph(json.load(source))


class _Buildings:
    """按建筑轮廓和楼顶高度核验接入段、垂直升降和固定高度巡查。"""

    def __init__(self, features):
        self.polygons = [shape(f["geometry"]) for f in features]
        self.tops = [f["properties"].get("z_max_m") for f in features]
        self.ids = [f["id"] for f in features]
        self.tree = STRtree(self.polygons)

    @lru_cache(maxsize=20000)
    def buffered(self, index, radius):
        return self.polygons[index].buffer(radius)

    def obstruction(self, a, b, horizontal, vertical):
        length = math.dist(a[:2], b[:2])
        line = LineString([a[:2], b[:2]]) if length > 1e-9 else Point(a[:2])
        for index in self.tree.query(line.buffer(horizontal)):
            index = int(index)
            overlap = self.buffered(index, horizontal).intersection(line)
            if overlap.is_empty:
                continue
            top = self.tops[index]
            if top is None:
                return self.ids[index]
            if length <= 1e-9:
                low = min(a[2], b[2])
            else:

                def coordinates(g):
                    if hasattr(g, "geoms"):
                        return [p for part in g.geoms for p in coordinates(part)]
                    return list(g.coords)

                fractions = [line.project(Point(p)) / length for p in coordinates(overlap)]
                low = min(a[2] + (b[2] - a[2]) * t for t in fractions)
            if low <= float(top) + vertical:
                return self.ids[index]
        return None


@lru_cache(maxsize=1)
def _buildings():
    with (MAP_DIR / "objects/buildings.json").open(encoding="utf-8-sig") as source:
        return _Buildings(json.load(source)["features"])


def _clear_path(points, horizontal, vertical):
    return all(
        _buildings().obstruction(a, b, horizontal, vertical) is None
        for a, b in zip(points, points[1:])
    )


class _FlightGraph:
    def __init__(self, data):
        if data.get("coordinate_system") != "UE_WORLD_METRES":
            raise ValueError("飞行图必须使用UE世界米制坐标")
        self.rules = data["altitude_rules"]
        self.layers = tuple(float(v) for v in self.rules["layers_agl_m"])
        if (
            not self.layers
            or any(not math.isfinite(v) or v <= 0 for v in self.layers)
            or sorted(set(self.layers)) != list(self.layers)
        ):
            raise ValueError("飞行高度层必须是递增的正数")
        self.nodes = [_xyz(n["position_m"], "飞行节点") for n in data["nodes"]]
        ids = {n["id"]: i for i, n in enumerate(data["nodes"])}
        if len(ids) != len(self.nodes):
            raise ValueError("飞行节点编号重复")
        self.records = data["edges"]
        self.edges = [_Edge(e, ids) for e in self.records]
        if not self.edges or len({e["id"] for e in self.records}) != len(self.edges):
            raise ValueError("飞行图为空或边编号重复")
        self.adjacency = defaultdict(list)
        self.lines = []
        for k, (edge, record) in enumerate(zip(self.edges, self.records)):
            if (
                edge.points[0] != self.nodes[edge.source][:2]
                or edge.points[-1] != self.nodes[edge.target][:2]
            ):
                raise ValueError("飞行路段端点与节点不一致")
            profile = record["ground_profile_m"]
            if len(profile) < 2 or any(
                len(p) != 2 or not all(math.isfinite(v) for v in p) for p in profile
            ):
                raise ValueError("地面剖面无效")
            if (
                profile[0][0] != 0
                or any(a[0] >= b[0] for a, b in zip(profile, profile[1:]))
                or abs(profile[-1][0] - edge.length) > 0.02
            ):
                raise ValueError("地面剖面里程与路段不一致")
            edge.profile = profile
            edge.ground_offsets = [p[0] for p in profile]
            edge.minimum = _positive(record["min_altitude_agl_m"], "路段最低高度")
            edge.width = _positive(record["min_passage_width_m"], "通道宽度")
            self.adjacency[edge.source].append((edge.target, k, 0.0, edge.length))
            self.adjacency[edge.target].append((edge.source, k, edge.length, 0.0))
            self.lines.append(LineString(edge.points))
        self.tree = STRtree(self.lines)
        self.road_indices = [k for k, e in enumerate(self.records) if e["kind"] == "road"]
        self.road_tree = (
            STRtree([self.lines[k] for k in self.road_indices]) if self.road_indices else None
        )
        self.indexes = {}

    def ground(self, k, offset):
        edge = self.edges[k]
        i = min(len(edge.profile) - 2, max(0, bisect_right(edge.ground_offsets, offset) - 1))
        a, b = edge.profile[i : i + 2]
        fraction = max(0.0, min(1.0, (offset - a[0]) / (b[0] - a[0])))
        return a[1] + (b[1] - a[1]) * fraction

    def ground_near(self, xy):
        point = Point(xy)
        k = int(self.tree.nearest(point))
        return self.ground(k, self.lines[k].project(point))

    def projections(self, xy, layers):
        point, candidates = Point(xy), {}
        width = 2 * (self.rules["gap_margin_m"] + self.rules["drone_radius_m"])
        for layer in layers:
            if layer not in self.indexes:
                indices = [
                    k for k, e in enumerate(self.edges) if e.minimum <= layer and e.width > width
                ]
                self.indexes[layer] = (indices, STRtree([self.lines[k] for k in indices]))
            indices, tree = self.indexes[layer]
            if not indices:
                continue
            nearest = int(tree.nearest(point))
            radius = self.lines[indices[nearest]].distance(point) + 20.0
            ranked = sorted(
                (self.lines[indices[int(i)]].distance(point), indices[int(i)])
                for i in tree.query(
                    box(xy[0] - radius, xy[1] - radius, xy[0] + radius, xy[1] + radius)
                )
            )
            selected = [k for distance, k in ranked if distance <= radius + 1e-9][:6]
            if self.road_tree is not None:
                road = self.road_indices[int(self.road_tree.nearest(point))]
                if self.edges[road].minimum <= layer and self.edges[road].width > width:
                    selected.append(road)
            for k in selected:
                offset = self.lines[k].project(point)
                xy_projected = self.lines[k].interpolate(offset).coords[0]
                candidates[k] = (offset, (*xy_projected, self.ground(k, offset)))
        return candidates

    def points(self, k, begin, end, height):
        edge = self.edges[k]
        low, high = sorted((begin, end))
        marks = {begin, end}
        marks.update(s for s in edge.offsets if low < s < high)
        marks.update(s for s in edge.ground_offsets if low < s < high)
        for s in sorted(marks, reverse=begin > end):
            i = min(len(edge.points) - 2, max(0, bisect_right(edge.offsets, s) - 1))
            a, b = edge.points[i : i + 2]
            span = edge.offsets[i + 1] - edge.offsets[i]
            t = 0.0 if span == 0 else max(0.0, min(1.0, (s - edge.offsets[i]) / span))
            xy = a if t == 0 else b if t == 1 else tuple(x + (y - x) * t for x, y in zip(a, b))
            yield (*xy, self.ground(k, s) + height)

    def route(self, start, target, layers, speed, altitude_cap):
        starts, goals = self.projections(start[:2], layers), self.projections(target[:2], layers)
        if not starts or not goals:
            raise NoPathError("给定高度内没有可接入的飞行路段")
        grounds = [self.ground_near(p[:2]) for p in (start, target)]
        for name, point, ground in zip(("起点", "目标"), (start, target), grounds):
            if point[2] < ground - 0.5 or point[2] > ground + altitude_cap + 1e-6:
                raise NoPathError(f"{name}高度超出当地地面至允许高度层的范围")
        if start == target:
            if not _clear_path(
                [start, target],
                self.rules["gap_margin_m"] + self.rules["drone_radius_m"],
                self.rules["vertical_margin_m"],
            ):
                raise NoPathError("当前位置与建筑净空冲突")
            return [(*start, min(speed, 1.0))]
        # 每条边上的接入投影作为临时节点，避免借空间相交跨接不同路段。
        extra, positions, attached = (
            defaultdict(list),
            dict(enumerate(self.nodes)),
            defaultdict(list),
        )
        terminals = [{}, {}]
        next_node = -1
        for side, projections in enumerate((starts, goals)):
            for k, (offset, point) in projections.items():
                n = next_node
                next_node -= 1
                positions[n] = point
                attached[k].append((offset, n))
                terminals[side][n] = k
        for k, anchors in attached.items():
            edge = self.edges[k]
            ordered = sorted([(0.0, edge.source), *anchors, (edge.length, edge.target)])
            for (begin, a), (end, b) in zip(ordered, ordered[1:]):
                extra[a].append((b, k, begin, end))
                extra[b].append((a, k, end, begin))
        gap_radius = self.rules["gap_margin_m"] + self.rules["drone_radius_m"]
        cruise_radius = self.rules["cruise_margin_m"] + self.rules["drone_radius_m"]
        vertical = self.rules["vertical_margin_m"]
        connectors, connector_speeds, vertical_checks = {}, {}, {}

        def connector(side, n, li):
            key = (side, n, li)
            if key not in connectors:
                actual, ground, q, height = (
                    (start, target)[side],
                    grounds[side],
                    positions[n],
                    layers[li],
                )
                points = [actual, (*actual[:2], ground + height), (*q[:2], q[2] + height)]
                if side:
                    points.reverse()
                if _clear_path(points, cruise_radius, vertical):
                    connectors[key], connector_speeds[key] = points, min(speed, 5.0)
                elif _clear_path(points, gap_radius, vertical):
                    connectors[key], connector_speeds[key] = points, min(speed, _GAP_SPEED)
                else:
                    connectors[key] = None
            return connectors[key]

        def cost(points):
            return sum(
                math.dist(a[:2], b[:2]) + (2.0 if b[2] >= a[2] else 0.5) * abs(b[2] - a[2])
                for a, b in zip(points, points[1:])
            )

        queue, distances, previous = [], {}, {}
        for n, k in terminals[0].items():
            for li, layer in enumerate(layers):
                if self.edges[k].minimum > layer:
                    continue
                points = connector(0, n, li)
                if points is not None:
                    state, distance = (n, li), cost(points)
                    distances[state] = distance
                    previous[state] = None
                    heapq.heappush(
                        queue, (distance + math.dist(positions[n][:2], target[:2]), distance, n, li)
                    )
        best, goal = math.inf, None
        while queue:
            bound, distance, n, li = heapq.heappop(queue)
            if bound >= best:
                break
            if distance != distances.get((n, li)):
                continue
            if n in terminals[1] and self.edges[terminals[1][n]].minimum <= layers[li]:
                points = connector(1, n, li)
                if points is not None and distance + cost(points) < best:
                    best, goal = distance + cost(points), (n, li)
            neighbors = [
                (m, li, abs(end - begin), (k, begin, end))
                for m, k, begin, end in (*self.adjacency.get(n, ()), *extra.get(n, ()))
                if self.edges[k].minimum <= layers[li] and self.edges[k].width > 2 * gap_radius
            ]
            for lj in (li - 1, li + 1):
                if not 0 <= lj < len(layers):
                    continue
                key = (n, min(li, lj), max(li, lj))
                if key not in vertical_checks:
                    p = positions[n]
                    vertical_checks[key] = _clear_path(
                        [(*p[:2], p[2] + layers[li]), (*p[:2], p[2] + layers[lj])],
                        gap_radius,
                        vertical,
                    )
                if vertical_checks[key]:
                    dh = layers[lj] - layers[li]
                    neighbors.append((n, lj, abs(dh) * (2.0 if dh > 0 else 0.5), None))
            for m, lj, weight, arc in neighbors:
                candidate, state = distance + weight, (m, lj)
                if candidate < distances.get(state, math.inf):
                    distances[state] = candidate
                    previous[state] = ((n, li), arc)
                    heapq.heappush(
                        queue,
                        (candidate + math.dist(positions[m][:2], target[:2]), candidate, m, lj),
                    )
        if goal is None:
            raise NoPathError("给定高度和建筑净空条件下无法到达目标")
        steps, state = [], goal
        while previous[state] is not None:
            parent, arc = previous[state]
            steps.append((parent, state, arc))
            state = parent
        raw, caps = [], []

        def add(point, cap):
            point = tuple(point)
            if raw and point == raw[-1]:
                return
            raw.append(point)
            caps.append(min(speed, cap))

        def append_connector(side, state):
            points = connector(side, *state)
            horizontal_speed = connector_speeds[(side, *state)]
            for i, p in enumerate(points):
                cap = (
                    _VERTICAL_SPEED
                    if i and math.dist(points[i - 1][:2], p[:2]) < 1e-8
                    else horizontal_speed
                )
                add(p, cap)

        append_connector(0, state)
        for (n, li), (m, lj), arc in reversed(steps):
            if arc is None:
                p = positions[m]
                add((*p[:2], p[2] + layers[lj]), _VERTICAL_SPEED)
            else:
                k, begin, end = arc
                cap = _GAP_SPEED if self.edges[k].width <= 2 * cruise_radius else speed
                for point in self.points(k, begin, end, layers[li]):
                    add(point, cap)
        append_connector(1, goal)
        return flight_route(raw, speed, limits=caps)


@lru_cache(maxsize=1)
def _flight_graph():
    with (MAP_DIR / "roads/uav_flight_network.json").open(encoding="utf-8-sig") as source:
        return _FlightGraph(json.load(source))


def flight_route(path, flight_speed, *, limits=None, stop_points=()):
    """为指定XYZ折线标注到达速度；保留拐点、升降点及精确终点。"""
    speed = _positive(flight_speed, "飞行速度")
    original = [_xyz(p, "路径点") for p in path]
    if not original:
        raise ValueError("路径不能为空")
    caps = [speed] * len(original) if limits is None else [_positive(v, "路段限速") for v in limits]
    if len(caps) != len(original):
        raise ValueError("路段限速数量与路径不一致")
    points, incoming = [original[0]], [min(speed, caps[0])]
    for a, b, cap in zip(original, original[1:], caps[1:]):
        length = math.dist(a, b)
        if length < 1e-9:
            continue
        vertical = math.dist(a[:2], b[:2]) < 1e-8
        count = 1 if vertical else max(1, math.ceil(length / 5.0))
        for i in range(1, count + 1):
            points.append(b if i == count else tuple(x + (y - x) * i / count for x, y in zip(a, b)))
            incoming.append(min(speed, cap, _VERTICAL_SPEED if vertical else speed))
    if len(points) == 1:
        return [(*points[0], min(speed, 1.0))]
    stops = set(stop_points)
    limits_at = [
        min(incoming[i], incoming[i + 1] if i + 1 < len(points) else speed)
        for i in range(len(points))
    ]
    offsets = [0.0]
    for a, b in zip(points, points[1:]):
        offsets.append(offsets[-1] + math.dist(a, b))
    for i in range(1, len(points) - 1):
        a, b, c = points[i - 1 : i + 2]
        u, v = tuple(y - x for x, y in zip(a, b)), tuple(y - x for x, y in zip(b, c))
        left, right = math.hypot(*u), math.hypot(*v)
        angle = math.acos(max(-1.0, min(1.0, sum(x * y for x, y in zip(u, v)) / (left * right))))
        if angle > math.radians(15):
            cap = min(speed, 3.0 if angle > math.pi / 4 else 5.0)
            low, high = bisect_right(offsets, offsets[i] - 20), bisect_right(
                offsets, offsets[i] + 10
            )
            for j in range(max(0, low - 1), high):
                limits_at[j] = min(limits_at[j], cap)
    for i, p in enumerate(points):
        if p[:2] in stops:
            limits_at[i] = min(limits_at[i], 1.0)
    limits_at[0] = min(limits_at[0], 2.0)
    limits_at[-1] = min(limits_at[-1], 1.0)
    for i in range(len(points) - 2, -1, -1):
        limits_at[i] = min(
            limits_at[i], math.sqrt(limits_at[i + 1] ** 2 + 6 * (offsets[i + 1] - offsets[i]))
        )
    for i in range(1, len(points)):
        limits_at[i] = min(
            limits_at[i], math.sqrt(limits_at[i - 1] ** 2 + 6 * (offsets[i] - offsets[i - 1]))
        )
    bands = sorted({min(speed, v) for v in (1, 2, 3, 5, 8, 12, 18, 25)} | {speed})
    route = [(*points[0], min(speed, 2.0))]
    for i in range(1, len(points)):
        cap = min(limits_at[i - 1], limits_at[i], incoming[i])
        velocity = max((v for v in bands if v <= cap), default=cap)
        route.append((*points[i], velocity))
    return route


def plan_flight_path(current_position, target_position, max_height, flight_speed=25.0):
    """返回完整[(世界X,Y,Z,到达速度), ...]，不连接仿真或保存文件。

    起终点均为UE世界XYZ米制坐标。高度上限相对沿途地面；按图中可用层
    规划道路、楼间通道和跨楼路线。路径包含起终点及必要升降点。
    起终点接入和换层用建筑轮廓检查；树、杆及动态障碍不在静态图内。
    不可达时抛出NoPathError，输入无效时抛出ValueError。
    """
    start, target = _xyz(current_position, "当前位置"), _xyz(target_position, "目标位置")
    cap, speed = _positive(max_height, "最大离地高度"), _positive(flight_speed, "飞行速度")
    graph = _flight_graph()
    layers = tuple(h for h in graph.layers if h <= cap)
    if not layers:
        raise ValueError(f"最大离地高度不得低于{graph.layers[0]:g}米")
    return graph.route(start, target, layers, speed, cap)


if __name__ == "__main__":
    from PIL import Image, ImageDraw, ImageFont

    # 修改这里的参数后，可直接运行本文件；不会连接AirSim或移动无人机。
    current_position = (2120.0, -2297.0, 360.0)
    target_position = (1727.7, -1940.2, 383.0)
    max_height = 60.0
    flight_speed = 8.0

    route = plan_flight_path(current_position, target_position, max_height, flight_speed)
    root = Path(__file__).resolve().parents[2]
    output_path = root / "output" / "temp" / "plan_flight_path.png"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(root / "web" / "assets" / "town12.png") as background:
        picture = background.convert("RGB")
    drawing = ImageDraw.Draw(picture, "RGBA")
    try:
        font = ImageFont.truetype("msyh.ttc", 26)
    except OSError:
        font = ImageFont.load_default()

    def pixel(point):
        # 底图范围：X[-1000,3000]、Y[-3000,1000]；+Y向下。
        return (
            (point[0] + 1000) / 4000 * (picture.width - 1),
            (point[1] + 3000) / 4000 * (picture.height - 1),
        )

    points = [pixel(point) for point in route]
    color = (0, 174, 255, 255)
    if len(points) > 1:
        drawing.line(points, fill="white", width=11, joint="curve")
        drawing.line(points, fill=color, width=6, joint="curve")
    remaining = 90.0
    for a, b in zip(points, points[1:]):
        distance = math.dist(a, b)
        if distance < 1e-9:
            continue
        ux, uy = (b[0] - a[0]) / distance, (b[1] - a[1]) / distance
        while remaining <= distance:
            x, y = a[0] + remaining * ux, a[1] + remaining * uy
            drawing.polygon(
                [
                    (x, y),
                    (x - 18 * ux + 8 * uy, y - 18 * uy - 8 * ux),
                    (x - 18 * ux - 8 * uy, y - 18 * uy + 8 * ux),
                ],
                fill=color,
            )
            remaining += 90
        remaining -= distance
    for a, b in zip(route, route[1:]):
        if math.dist(a[:2], b[:2]) < 1e-8 and abs(a[2] - b[2]) > 1e-8:
            x, y = pixel(b)
            drawing.ellipse((x - 7, y - 7, x + 7, y + 7), outline=(174, 62, 226), width=3)
    x, y = points[0]
    drawing.ellipse((x - 12, y - 12, x + 12, y + 12), fill=(35, 184, 112), outline="white", width=3)
    x, y = points[-1]
    drawing.rectangle(
        (x - 11, y - 11, x + 11, y + 11), fill=(235, 66, 66), outline="white", width=3
    )
    drawing.rounded_rectangle((24, 24, 1040, 132), radius=12, fill=(17, 38, 56, 235))
    drawing.text(
        (44, 38), "Flight path | blue: route | green: start | red: goal", font=font, fill="white"
    )
    drawing.text(
        (44, 80),
        f"Max AGL {max_height:g} m | Speed cap {flight_speed:g} m/s | {len(route)} waypoints",
        font=font,
        fill="white",
    )
    picture.save(output_path)
    print(f"规划完成：{len(route)}个航点；PNG：{output_path}")
