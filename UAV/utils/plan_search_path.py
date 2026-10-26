"""按完整街区选择道路并规划覆盖路线；不连接 AirSim，不移动无人机。"""

from collections import defaultdict
from functools import lru_cache
import heapq
import json
import math
from pathlib import Path

from shapely.geometry import LineString, shape
from shapely.ops import polygonize, unary_union

from .plan_flight_path import NoPathError, _default_graph, _point

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
                    candidate = (cost(odd[first], odd[second]) + length,
                                 ((odd[first], odd[second]),) + pairs)
                    if candidate < best:
                        best = candidate
            return best

        full = (1 << len(odd)) - 1
        _, _, pairs = min((match(full ^ (1 << i))[0], node,
                           match(full ^ (1 << i))[1]) for i, node in enumerate(odd))
    else:
        available, pairs = set(odd), []
        candidates = sorted((cost(a, b), a, b)
                            for i, a in enumerate(odd) for b in odd[i + 1:])
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
    if geometry.is_empty or not geometry.is_valid or geometry.geom_type not in ("Polygon", "MultiPolygon"):
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
    blocks = [face for face in faces if face.intersects(geometry)
              and face.intersection(geometry).area > 1e-6]
    # 区域还可能包含未形成闭合街区的道路，仍按完整边纳入。
    coverage = unary_union([geometry, *blocks])
    selected = {i for i, line in enumerate(lines)
                if line.intersection(coverage).length > 1e-7}
    if not selected:
        raise NoPathError("目标区域没有可关联的街区或道路，未起飞")
    return selected


def plan_search_path(current_position, target) -> list[tuple[list[tuple[float, float]], bool]]:
    """选完整街区、完整边，再生成开放式边覆盖路线；不要求返回起点。

    仅当前位置的路网接入可能是部分边，必巡目标始终是完整原始边。
    返回按执行顺序排列的 [(XY路点列表, 是否巡查), ...]。
    True 表示巡查并拍照，False 表示接入或转场；连续同阶段航段合并。
    """
    start = _point(current_position, "当前位置")
    graph = _default_graph()
    geometry, _ = search_scope(target)
    required = _required_edges(graph, geometry)
    adjacency, pieces, required_arcs = defaultdict(list), {}, []
    for index, edge in enumerate(graph.edges):
        key = graph.edge_ids[index]
        pieces[key] = list(edge.points)
        adjacency[edge.source].append((edge.target, key, edge.length, False))
        adjacency[edge.target].append((edge.source, key, edge.length, True))
        if index in required:
            required_arcs.append((edge.source, edge.target, key, edge.length, False))

    # 接入片段只用于走到真实端点，不把完整巡查任务裁成多个小段。
    source = -1
    _, anchors = graph.nearest(start)
    for number, (index, offset, projection) in enumerate(anchors):
        edge = graph.edges[index]
        entry = -2 - number
        key = f"access:{number}"
        length = math.dist(start, projection)
        pieces[key] = [start, projection]
        adjacency[source].append((entry, key, length, False))
        for suffix, node, endpoint, position in (("start", edge.source, 0.0, edge.points[0]),
                                                  ("end", edge.target, edge.length, edge.points[-1])):
            key = f"entry:{number}:{suffix}"
            length = abs(endpoint - offset)
            pieces[key] = edge.section(offset, endpoint, projection, position)
            adjacency[entry].append((node, key, length, False))

    walk = cover_edges(adjacency, required_arcs, source)
    target_ids = {graph.edge_ids[index] for index in required}
    if not target_ids <= {arc[2] for arc in walk}:
        raise NoPathError("覆盖路线遗漏必巡道路，未起飞")
    route = []
    for _, _, key, length, reverse in walk:
        if length == 0:
            continue
        points = pieces[key][::-1] if reverse else pieces[key]
        patrol = key in target_ids
        if route and route[-1][1] == patrol:
            route[-1][0].extend(points[1:])
        else:
            route.append((list(points), patrol))
    return route


# 检查计划路径周围的已知建筑是否满足给定高度和净空余量。
# 输入为 XY 路径列表、世界高度和余量；不满足时抛出异常，此检查不是实时避障。
def check_building_clearance(paths, altitude_world_m, margin_m=3):
    """已知道路/接入段与建筑冲突时拒绝，固定高度不是实时避障。"""
    from shapely.ops import unary_union
    lines = [LineString(points) for points in paths if len(points) >= 2]
    corridor = unary_union([line for line in lines if line.length > 0.01]).buffer(1.0)
    data = json.loads((MAP_DIR / "objects" / "buildings.json").read_text(encoding="utf-8-sig"))
    for feature in data["features"]:
        height = feature["properties"].get("z_max_m")
        if height is None:
            if shape(feature["geometry"]).intersects(corridor):
                raise ValueError("路径附近建筑缺少高度，无法核实飞行高度")
        elif altitude_world_m <= height + margin_m and shape(feature["geometry"]).intersects(corridor):
            raise ValueError(f"飞行高度不足以避开路径上的建筑{feature['id']}，请调整高度")
