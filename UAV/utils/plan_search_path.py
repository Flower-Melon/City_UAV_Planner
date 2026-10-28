"""按完整街区选择道路并规划覆盖路线；不连接 AirSim，不移动无人机。"""

from collections import defaultdict
from functools import lru_cache
import heapq
import json
import math
from pathlib import Path

from shapely.geometry import LineString, shape
from shapely.ops import polygonize, unary_union

if __name__ == "__main__":
    # 直接运行文件时，使下方相对导入仍指向工程内的UAV包。
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "UAV.utils"

from .plan_flight_path import (
    NoPathError,
    _default_graph,
    _xyz,
    _positive,
    _buildings,
    flight_route,
    plan_flight_path,
)

MAP_DIR = Path(__file__).resolve().parents[2] / "Map"
MAP_PATH = MAP_DIR / "zones" / "functional_zones.json"


def _shortest(adjacency, sources, goals=None):
    distances = dict.fromkeys(sources, 0.0)
    previous = {}
    queue = [(0.0, node) for node in distances]
    heapq.heapify(queue)
    while queue:
        distance, node = heapq.heappop(queue)
        if distance != distances[node]:
            continue
        if goals is not None and node in goals:
            return distances, previous, node
        for arc in adjacency.get(node, ()):
            neighbor, _, length, _ = arc
            candidate = distance + length
            if candidate < distances.get(neighbor, math.inf):
                distances[neighbor] = candidate
                previous[neighbor] = (node, arc)
                heapq.heappush(queue, (candidate, neighbor))
    return distances, previous, None


def _path(previous, end):
    result = []
    while end in previous:
        parent, (neighbor, key, length, reverse) = previous[end]
        result.append((parent, neighbor, key, length, reverse))
        end = parent
    return list(reversed(result))


def cover_edges(adjacency, required_arcs, source):
    """返回从 source 出发、覆盖所有完整必巡边的连续路线，不要求返航。

    多个必巡分量以最短路逐步连接。其余奇度节点不超过 17 个时，
    精确选择自由终点和最小配对；更大任务用贪心配对控制计算量。
    接入与分量连接采用启发式，整体不承诺全局最短解。
    """
    links, multigraph = [], {}

    def add(arc):
        a, b, key, length, reverse = arc
        index = len(links)
        links.append(arc)
        multigraph.setdefault(a, []).append((b, index, reverse))
        multigraph.setdefault(b, []).append((a, index, not reverse))

    required_nodes = set()
    for arc in required_arcs:
        add(arc)
        required_nodes.update(arc[:2])

    _, previous, entry = _shortest(adjacency, [source], required_nodes)
    if entry is None:
        raise NoPathError("当前位置无法接入目标街区，未起飞")
    for arc in _path(previous, entry):
        add(arc)

    def reachable():
        seen, pending = {source}, [source]
        while pending:
            for neighbor, _, _ in multigraph.get(pending.pop(), ()):
                if neighbor not in seen:
                    seen.add(neighbor)
                    pending.append(neighbor)
        return seen

    connected = reachable()
    while missing := required_nodes - connected:
        # 只从真实拓扑节点转场，不能借无人机当前位置跨接断开的图。
        _, previous, entry = _shortest(adjacency, [n for n in connected if n >= 0], missing)
        if entry is None:
            raise NoPathError("目标街区包含无法连通的必巡道路，未起飞")
        for arc in _path(previous, entry):
            add(arc)
        connected = reachable()

    odd = sorted(node for node, arcs in multigraph.items() if len(arcs) % 2 and node != source)
    if source not in multigraph or len(multigraph[source]) != 1 or not odd:
        raise ValueError("搜索接入点必须是覆盖路线的单一入口")

    @lru_cache(maxsize=None)
    def paths(node):
        distances, previous, _ = _shortest(adjacency, [node])
        return distances, previous

    def cost(a, b):
        return paths(a)[0].get(b, math.inf)

    if len(odd) <= 17:

        @lru_cache(maxsize=None)
        def match(mask):
            if not mask:
                return 0.0, ()
            first = (mask & -mask).bit_length() - 1
            remaining = mask ^ (1 << first)
            best = (math.inf, ())
            for second in range(first + 1, len(odd)):
                if remaining & (1 << second):
                    length, pairs = match(remaining ^ (1 << second))
                    candidate = (
                        cost(odd[first], odd[second]) + length,
                        ((odd[first], odd[second]),) + pairs,
                    )
                    if candidate < best:
                        best = candidate
            return best

        full = (1 << len(odd)) - 1
        _, _, pairs = min(
            (match(full ^ (1 << i))[0], node, match(full ^ (1 << i))[1])
            for i, node in enumerate(odd)
        )
    else:
        available, pairs = set(odd), []
        candidates = sorted((cost(a, b), a, b) for i, a in enumerate(odd) for b in odd[i + 1 :])
        for _, a, b in candidates:
            if a in available and b in available:
                pairs.append((a, b))
                available.difference_update((a, b))
                if len(available) == 1:
                    break
    for a, b in pairs:
        for arc in _path(paths(a)[1], b):
            add(arc)

    # 每个复制边都有独立序号，桥边的必要往返不会被误判为已巡查而丢弃。
    used, stack, result = set(), [(source, None)], []
    while stack:
        node = stack[-1][0]
        arcs = multigraph[node]
        while arcs and arcs[-1][1] in used:
            arcs.pop()
        if arcs:
            neighbor, index, reverse = arcs.pop()
            used.add(index)
            _, _, key, length, _ = links[index]
            stack.append((neighbor, (node, neighbor, key, length, reverse)))
        else:
            _, incoming = stack.pop()
            if incoming is not None:
                result.append(incoming)
    if len(used) != len(links):
        raise NoPathError("覆盖路线不连续，未起飞")
    return list(reversed(result))


def search_scope(target):
    """读取目标真实边界；搜索任务由 zone_id 标识，不再接受缓冲宽度。"""
    if not isinstance(target, dict) or not isinstance(target.get("zone_id"), str):
        raise ValueError("target需要真实zone_id")
    if "buffer_m" in target:
        raise ValueError("buffer_m已取消；请仅指定zone_id，按完整街区巡查")
    data = json.loads(MAP_PATH.read_text(encoding="utf-8-sig"))
    if data["coordinate_metadata"].get("system") != "UE_WORLD_METRES":
        raise ValueError("地图必须使用UE世界米制坐标")
    feature = next((f for f in data["features"] if f["id"] == target["zone_id"]), None)
    if feature is None:
        raise ValueError("区域ID不存在")
    geometry = shape(feature["geometry"])
    if (
        geometry.is_empty
        or not geometry.is_valid
        or geometry.geom_type not in ("Polygon", "MultiPolygon")
    ):
        raise ValueError("区域边界无效")
    return geometry, {"zone_id": feature["id"]}


@lru_cache(maxsize=1)
def _street_blocks(graph):
    # 不对线网做 unary_union/noding：空间交叉不能自动新增拓扑路口。
    lines = tuple(LineString(edge.points) for edge in graph.edges)
    return lines, tuple(face for face in polygonize(lines) if face.is_valid and face.area > 1e-6)


def _required_edges(graph, geometry):
    """覆盖目标所占街区及区域内部道路，选中后保留整条原始边。"""
    lines, faces = _street_blocks(graph)
    blocks = [
        face
        for face in faces
        if face.intersects(geometry) and face.intersection(geometry).area > 1e-6
    ]
    # 区域还可能包含未形成闭合街区的道路，仍按完整边纳入。
    coverage = unary_union([geometry, *blocks])
    selected = {i for i, line in enumerate(lines) if line.intersection(coverage).length > 1e-7}
    if not selected:
        raise NoPathError("目标区域没有可关联的街区或道路，未起飞")
    return selected


def plan_search_path(
    current_position, target, search_height, max_height, flight_speed=5.0
):
    """返回[(XYZ速度航点列表, 是否巡查), ...]，不包含返航。

    current_position为实际世界XYZ；search_height为巡查世界Z。
    首次接入使用三维飞行图，后续道路覆盖保留完整原始边及真实节点。
    """
    start = _xyz(current_position, "当前位置")
    altitude = float(search_height)
    if isinstance(search_height, (bool, str, bytes)) or not math.isfinite(altitude):
        raise ValueError("巡查世界高度必须是有限数值")
    cap, speed = _positive(max_height, "最大离地高度"), _positive(flight_speed, "飞行速度")
    graph = _default_graph()
    geometry, _ = search_scope(target)
    required = _required_edges(graph, geometry)
    check_building_clearance([graph.edges[i].points for i in required], altitude)
    adjacency, pieces, required_arcs = defaultdict(list), {}, []
    for index, edge in enumerate(graph.edges):
        key = graph.edge_ids[index]
        pieces[key] = list(edge.points)
        adjacency[edge.source].append((edge.target, key, edge.length, False))
        adjacency[edge.target].append((edge.source, key, edge.length, True))
        if index in required:
            required_arcs.append((edge.source, edge.target, key, edge.length, False))
    candidates = {node for a, b, _, _, _ in required_arcs for node in (a, b)}
    # 欧氏距离是接入代价下界；逐步扩展候选，避免最近入口不可达就放弃任务。
    best, entry, navigation = math.inf, None, None
    for lower, node in sorted((math.dist(start[:2], graph.node_points[n]), n) for n in candidates):
        if lower > best:
            break
        destination = (*graph.node_points[node], altitude)
        try:
            route = plan_flight_path(start, destination, cap, speed)
        except NoPathError:
            continue
        cost = sum(
            math.dist(a[:2], b[:2]) + (2.0 if b[2] >= a[2] else 0.5) * abs(b[2] - a[2])
            for a, b in zip(route, route[1:])
        )
        if cost < best:
            best, entry, navigation = cost, node, route
    if entry is None:
        raise NoPathError("给定高度下无法到达目标街区的任何巡查入口")
    # 单一虚拟源保留原覆盖算法的入口约束，不把入口切成部分巡查边。
    source = -1
    adjacency[source].append((entry, "entry", 0.0, False))
    pieces["entry"] = [graph.node_points[entry], graph.node_points[entry]]
    walk = cover_edges(adjacency, required_arcs, source)
    target_ids = {graph.edge_ids[index] for index in required}
    if not target_ids <= {arc[2] for arc in walk}:
        raise NoPathError("覆盖路线遗漏必巡道路")
    phases = []
    for _, _, key, length, reverse in walk:
        if length == 0:
            continue
        points = pieces[key][::-1] if reverse else pieces[key]
        patrol = key in target_ids
        if phases and phases[-1][1] == patrol:
            phases[-1][0].extend(points[1:])
        else:
            phases.append((list(points), patrol))
    check_building_clearance([points for points, _ in phases], altitude)
    nodes = frozenset(graph.node_points)
    result = [(navigation, False)]
    for points, patrol in phases:
        route = flight_route(
            [(*p, altitude) for p in points], speed, stop_points=nodes if patrol else ()
        )
        result.append((route, patrol))
    return result


def check_building_clearance(paths, altitude_world_m, margin_m=3):
    """核验固定高度道路巡查；输入XY折线和世界Z，冲突时拒绝任务。"""
    altitude = float(altitude_world_m)
    margin = _positive(margin_m, "建筑垂直余量")
    if not math.isfinite(altitude):
        raise ValueError("世界高度必须是有限数值")
    for path in paths:
        for a, b in zip(path, path[1:]):
            building = _buildings().obstruction((*a[:2], altitude), (*b[:2], altitude), 1.0, margin)
            if building is not None:
                raise NoPathError(f"巡查高度不足以避开建筑{building}，请调整巡查高度")


if __name__ == "__main__":
    from PIL import Image, ImageDraw, ImageFont

    # 修改这里的参数后，可直接运行本文件；不会连接AirSim或移动无人机。
    current_position = (2120.0, -2297.0, 360.0)
    target = {"zone_id": "F-SCHOOL-1"}
    search_height = 399.0
    max_height = 60.0
    flight_speed = 5.0

    segments = plan_search_path(
        current_position, target, search_height, max_height, flight_speed
    )
    root = Path(__file__).resolve().parents[2]
    output_path = root / "output" / "temp" / "plan_search_path.png"
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

    geometry, _ = search_scope(target)
    polygons = [geometry] if geometry.geom_type == "Polygon" else geometry.geoms
    for polygon in polygons:
        drawing.line([pixel(p) for p in polygon.exterior.coords], fill=(255, 218, 76), width=4)
    # 导航先画，巡查后画，避免重复路段遮住巡查轨迹。
    for patrol in (False, True):
        color = (27, 216, 139, 255) if patrol else (255, 145, 42, 255)
        for route, is_patrol in segments:
            if is_patrol != patrol:
                continue
            points = [pixel(p) for p in route]
            if len(points) < 2:
                continue
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
    x, y = pixel(segments[0][0][0])
    drawing.ellipse((x - 12, y - 12, x + 12, y + 12), fill=(35, 184, 112), outline="white", width=3)
    x, y = pixel(segments[-1][0][-1])
    drawing.rectangle(
        (x - 11, y - 11, x + 11, y + 11), fill=(235, 66, 66), outline="white", width=3
    )
    drawing.rounded_rectangle((24, 24, 1180, 132), radius=12, fill=(17, 38, 56, 235))
    drawing.text(
        (44, 38),
        "Search path | orange: transit | green: patrol | red: finish",
        font=font,
        fill="white",
    )
    drawing.text(
        (44, 80),
        f"{target['zone_id']} | Patrol Z {search_height:g} m | Max AGL {max_height:g} m",
        font=font,
        fill="white",
    )
    picture.save(output_path)
    print(f"规划完成：{len(segments)}个阶段；PNG：{output_path}")
