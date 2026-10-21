"""裁剪目标范围内的道路，逐段巡查；不连接AirSim，不移动无人机。"""

from collections import defaultdict
from functools import lru_cache
import heapq
import json
import math
from pathlib import Path

from shapely.geometry import LineString, Point, shape

from .plan_flight_path import NoPathError, _RoadGraph, _point

MAP_PATH = Path(__file__).resolve().parents[2] / "Map" / "functional_zones.json"


# 读取并缓存本地机动车道路图，供多次搜索规划复用。
# 无输入；返回道路图，不连接 AirSim，也不移动无人机。
@lru_cache(maxsize=1)
def _search_graph():
    """保留机动车道拓扑用于通行；必巡目标另外按道路合并。"""
    data = json.loads(MAP_PATH.with_name("road_network.json").read_text(encoding="utf-8-sig"))
    return _driving_graph(data)


# 从道路数据中保留机动车道及它们之间的路口连接。
# 输入为道路 JSON 对象；返回带巡查车道标识的图，连接边只负责通行。
def _driving_graph(data):
    lanes = {lane["id"] for lane in data["lanes"] if lane["type"] == "driving"}
    roads = [edge for edge in data["edges"] if edge["kind"] == "road" and edge.get("lane_id") in lanes]
    nodes = {node for edge in roads for node in (edge["from_node_id"], edge["to_node_id"])}
    connections = [edge for edge in data["edges"] if edge["kind"] == "connection"
                   and edge["from_node_id"] in nodes and edge["to_node_id"] in nodes]
    graph = _RoadGraph({**data, "edges": roads + connections})
    graph.patrol_lanes = {edge["id"]: edge["lane_id"] for edge in roads}
    return graph


# 在同一道路同一分段内选一条代表车道，减少重复巡查平行车道。
# 输入为道路图和裁剪区间；返回去重后的区间，优先选范围内有效长度最长的车道。
def _merge_lane_clips(graph, clips):
    """同一road/section只巡一条真实车道；连接边只供通行。

    地图车道ID为r<road>:s<section>:l<lane>。优先选择在本次范围内
    有效长度最长的车道，避免选到范围外的车道；不按空间距离合并独立道路。
    无车道元信息的自定义图保留原本逐边覆盖语义。
    """
    lane_ids = getattr(graph, "patrol_lanes", None)
    if lane_ids is None:
        return clips
    lengths = defaultdict(float)
    for index, intervals in clips.items():
        lane = lane_ids.get(graph.edge_ids[index])
        if lane is not None:
            lengths[lane] += sum(b-a for a, b in intervals)
    selected = {}
    for lane, length in lengths.items():
        prefix, separator, number = lane.rpartition(":l")
        group = prefix if separator and number.lstrip("-").isdigit() else lane
        previous = selected.get(group)
        if previous is None or (length, lane) > (lengths[previous], previous):
            selected[group] = lane
    representatives = set(selected.values())
    return defaultdict(list, {index: intervals for index, intervals in clips.items()
                             if lane_ids.get(graph.edge_ids[index]) in representatives})


# 用真实区域边界构造搜索范围：区域内部，或边界外默认 50 米的环带。
# 输入为 target 和地图路径；返回 Shapely 范围及规范化目标，不把附近自动换成居住区。
def search_scope(target, map_path=MAP_PATH):
    """接受现有command.target（允许携带名称等摘要），范围只由ID/scope/buffer决定。"""
    if not isinstance(target, dict) or not isinstance(target.get("zone_id"), str):
        raise ValueError("target需要真实zone_id")
    scope = target.get("scope", "zone")
    if scope not in ("zone", "nearby"):
        raise ValueError("scope只支持zone或nearby")
    width = target.get("buffer_m", 50 if scope == "nearby" else None)
    if scope == "nearby":
        if isinstance(width, bool) or not isinstance(width, (int, float)) or not math.isfinite(width) or width <= 0:
            raise ValueError("buffer_m必须为正的有限米数")
    elif width is not None:
        raise ValueError("buffer_m仅用于nearby")
    data = json.loads(Path(map_path).read_text(encoding="utf-8-sig"))
    if data["coordinate_metadata"].get("system") != "UE_WORLD_METRES":
        raise ValueError("地图必须使用UE世界米制坐标")
    feature = next((f for f in data["features"] if f["id"] == target["zone_id"]), None)
    if feature is None:
        raise ValueError("区域ID不存在")
    geometry = shape(feature["geometry"])
    if not geometry.is_valid or geometry.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError("区域边界无效")
    area = geometry.buffer(width).difference(geometry) if scope == "nearby" else geometry
    return area, {"zone_id": feature["id"], "scope": scope,
                  **({"buffer_m": float(width)} if width is not None else {})}


# 从几何裁剪结果中递归提取线段，忽略不构成道路长度的其他几何。
# 输入为 Shapely 几何；逐个产出 LineString。
def _lines(geometry):
    if geometry.geom_type == "LineString":
        yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from _lines(part)


# 在真实道路拓扑内调整巡查顺序与方向，减少重复掉头和连接距离。
# 输入为带节点的行走序列、邻接表和必巡集合；返回不更长且仍覆盖全部目标的路线。
def _shorten_route(walk, adjacency, targets):
    blocks, seen, consecutive = [], set(), False
    for arc in walk:
        if arc[2] in targets and arc[2] not in seen:
            if not blocks or not consecutive:
                blocks.append([])
            blocks[-1].append(arc)
            seen.add(arc[2])
            consecutive = True
        else:
            consecutive = False
    # 限制局部搜索规模；大范围任务保留原方案，避免二次优化显著拖慢规划。
    if not blocks or len(blocks) > 200:
        return walk
    endpoints = {node for block in blocks for node in (block[0][0], block[-1][1])}

    # 一次最短路供多个候选顺序复用；只使用真实节点连接，不靠坐标合并路口。
    @lru_cache(maxsize=None)
    def paths(start):
        distances, previous, queue = {start: 0.0}, {}, [(0.0, start)]
        pending = set(endpoints)
        while queue:
            distance, node = heapq.heappop(queue)
            if distance != distances[node]:
                continue
            pending.discard(node)
            if not pending:
                break
            for neighbor, key, length, reverse in adjacency.get(node, ()):
                candidate = distance + length
                if candidate < distances.get(neighbor, math.inf):
                    distances[neighbor] = candidate
                    previous[neighbor] = (node, neighbor, key, length, reverse)
                    heapq.heappush(queue, (candidate, neighbor))
        return distances, previous

    # 读取缓存距离；不存在拓扑连接时按无穷大处理，不生成直线捷径。
    def distance(start, end):
        return paths(start)[0].get(end, math.inf)

    source = walk[0][0]
    # 2-opt 同时尝试反转单个巡查块及整段顺序；不要求返回起点。
    for _ in range(min(100, 2 * len(blocks))):
        best, change = -1e-7, None
        for i, first in enumerate(blocks):
            before = source if i == 0 else blocks[i-1][-1][1]
            for j in range(i, len(blocks)):
                last = blocks[j]
                after = blocks[j+1][0][0] if j+1 < len(blocks) else None
                old = distance(before, first[0][0])
                new = distance(before, last[-1][1])
                if after is not None:
                    old += distance(last[-1][1], after)
                    new += distance(first[0][0], after)
                if new - old < best:
                    best, change = new - old, (i, j)
        if change is not None:
            i, j = change
            blocks[i:j+1] = [[(v, u, key, length, not reverse)
                              for u, v, key, length, reverse in reversed(block)]
                             for block in reversed(blocks[i:j+1])]
            continue
        # 单纯反转已无法缩短时，尝试把一个巡查块搬到更合适的位置。
        relocation = None
        for i, block in enumerate(blocks):
            others = blocks[:i] + blocks[i+1:]
            before = source if i == 0 else blocks[i-1][-1][1]
            after = blocks[i+1][0][0] if i+1 < len(blocks) else None
            removed = distance(before, block[0][0])
            removed += distance(block[-1][1], after) if after is not None else 0
            saved = removed - (distance(before, after) if after is not None else 0)
            for j in range(len(others)+1):
                left = source if j == 0 else others[j-1][-1][1]
                right = others[j][0][0] if j < len(others) else None
                for reverse in (False, True):
                    entry, exit = (block[-1][1], block[0][0]) if reverse else (block[0][0], block[-1][1])
                    added = distance(left, entry)
                    if right is not None:
                        added += distance(exit, right) - distance(left, right)
                    if added - saved < best:
                        best, relocation = added - saved, (i, j, reverse)
        if relocation is None:
            break
        i, j, reverse = relocation
        block = blocks.pop(i)
        if reverse:
            block = [(v, u, key, length, not rev) for u, v, key, length, rev in reversed(block)]
        blocks.insert(j, block)

    result, seen, current = [], set(), source
    for block in blocks:
        if all(arc[2] in seen for arc in block):
            continue
        _, previous = paths(current)
        connection, cursor = [], block[0][0]
        while cursor != current:
            arc = previous[cursor]
            connection.append(arc)
            cursor = arc[0]
        for arc in list(reversed(connection)) + block:
            result.append(arc)
            current = arc[1]
            seen.add(arc[2])
            if targets <= seen:
                break
        if targets <= seen:
            break
    if targets <= seen and sum(arc[3] for arc in result) <= sum(arc[3] for arc in walk) + 1e-7:
        return result
    return walk


# 裁剪目标道路、合并同路车道，并按拓扑连接各段直到全部目标路段被覆盖。
# 输入为当前位置和目标；返回航段、必巡路段及总距离，可有重复连接，不保证全局最短。
def plan_search_path(current_position, target, *, graph=None, map_path=MAP_PATH):
    """同路车道去重，按拓扑覆盖后优化方向和顺序；允许必要重复，不保证全局最短。"""
    start = _point(current_position, "当前位置")
    graph = graph or _search_graph()
    area, target = search_scope(target, map_path)
    bounds = area.bounds
    clips = defaultdict(list)
    for index, edge in enumerate(graph.edges):
        x0, y0, x1, y1 = edge.bounds
        if x1 < bounds[0] or x0 > bounds[2] or y1 < bounds[1] or y0 > bounds[3]:
            continue
        line = LineString(edge.points)
        for part in _lines(line.intersection(area)):
            if part.length > 0.05:
                a, b = ((0.0, line.length) if part.is_closed else
                        sorted((line.project(Point(part.coords[0])), line.project(Point(part.coords[-1])))))
                # 校界本身不属于校界外环带。
                if target["scope"] == "nearby" and not area.contains(part.interpolate(.5, normalized=True)):
                    continue
                clips[index].append((a, b))
    clips = _merge_lane_clips(graph, clips)
    if not clips:
        if target["scope"] == "zone":
            raise NoPathError(f"区域 {target['zone_id']} 内部没有可巡查道路，未起飞；可改为搜索该区域周边（默认50米）")
        raise NoPathError(f"区域 {target['zone_id']} 周边{target['buffer_m']:g}米内没有可巡查道路，未起飞")
    _, anchors = graph.nearest(start)
    cuts = defaultdict(set)
    for index, intervals in clips.items():
        for a, b in intervals:
            cuts[index].update((a, b))
    for index, offset, _ in anchors:
        cuts[index].add(offset)

    adjacency = defaultdict(list)
    coordinates = {i: point for i, point in enumerate(graph.node_points)}
    pieces, targets, anchor_nodes = {}, set(), {}
    next_node = len(coordinates)
    for index, edge in enumerate(graph.edges):
        if edge.length <= 1e-7:
            # 地图用零长度边连接不同拓扑节点；丢弃它们会把真实路口切断。
            key = graph.edge_ids[index]+"@connection"
            pieces[key] = {"points": list(edge.points), "length_m": 0.0,
                           "edge_id": graph.edge_ids[index], "target": False}
            adjacency[edge.source].append((edge.target, key, 0.0, False))
            adjacency[edge.target].append((edge.source, key, 0.0, True))
            anchor_nodes[index, 0.0] = edge.source
            continue
        offsets = sorted({0.0, edge.length, *cuts[index]})
        line = LineString(edge.points)
        nodes = []
        for offset in offsets:
            if offset <= 1e-7:
                node = edge.source
            elif edge.length - offset <= 1e-7:
                node = edge.target
            else:
                node = next_node
                next_node += 1
                coordinates[node] = tuple(line.interpolate(offset).coords[0])
            nodes.append(node)
            anchor_nodes[index, offset] = node
        for j, (a, b) in enumerate(zip(offsets, offsets[1:])):
            if b-a <= 1e-7:
                continue
            key = f"{graph.edge_ids[index]}@{a:.6f}:{b:.6f}"
            selected = any(lo-1e-7 <= a and b <= hi+1e-7 for lo, hi in clips[index])
            points = edge.section(a, b, coordinates[nodes[j]], coordinates[nodes[j+1]])
            pieces[key] = {"points": points, "length_m": b-a, "edge_id": graph.edge_ids[index],
                           "target": selected}
            if selected:
                targets.add(key)
            adjacency[nodes[j]].append((nodes[j+1], key, b-a, False))
            adjacency[nodes[j+1]].append((nodes[j], key, b-a, True))
    source = -1
    coordinates[source] = start
    for index, offset, point in anchors:
        node = anchor_nodes[index, offset]
        key = f"access:{index}:{offset}"
        length = math.dist(start, point)
        pieces[key] = {"points": [start, point], "length_m": length, "edge_id": None, "target": False}
        adjacency[source].append((node, key, length, False))

    # 先核实全部目标可达，无法完整巡查则在取得飞行控制前报错。
    reachable, pending = {source}, [source]
    while pending:
        node = pending.pop()
        for neighbor, *_ in adjacency[node]:
            if neighbor not in reachable:
                reachable.add(neighbor)
                pending.append(neighbor)
    reachable_targets = {key for node in reachable for _, key, _, _ in adjacency[node] if key in targets}
    if reachable_targets != targets:
        raise NoPathError(f"有{len(targets-reachable_targets)}个目标道路段不连通，未起飞")

    remaining, walk, current = set(targets), [], source
    while remaining:
        distances, previous, queue = {current: 0.0}, {}, [(0.0, current)]
        while queue:
            distance, node = heapq.heappop(queue)
            if distance != distances[node]:
                continue
            choices = [arc for arc in adjacency[node] if arc[1] in remaining]
            if choices:
                chosen = min(choices, key=lambda arc: (arc[2], arc[1]))
                break
            for arc in adjacency[node]:
                neighbor, key, length, reverse = arc
                candidate = distance + length
                if candidate < distances.get(neighbor, math.inf):
                    distances[neighbor] = candidate
                    previous[neighbor] = (node, arc)
                    heapq.heappush(queue, (candidate, neighbor))
        else:
            raise NoPathError("剩余巡查道路不可达")
        steps, cursor = [], node
        while cursor != current:
            parent, arc = previous[cursor]
            steps.append(arc)
            cursor = parent
        steps = list(reversed(steps)) + [chosen]
        for neighbor, key, length, reverse in steps:
            walk.append((current, neighbor, key, length, reverse))
            remaining.discard(key)
            current = neighbor
    route = []
    for _, _, key, length, reverse in _shorten_route(walk, adjacency, targets):
        piece = pieces[key]
        points = list(reversed(piece["points"])) if reverse else piece["points"]
        route.append({"piece_id": key, "edge_id": piece["edge_id"], "points": points,
                      "kind": "patrol" if key in targets else "connection", "length_m": length})
    return {"target": target, "target_piece_ids": sorted(targets), "legs": route,
            "distance_m": sum(leg["length_m"] for leg in route),
            "target_distance_m": sum(pieces[key]["length_m"] for key in targets)}


# 检查计划路径周围的已知建筑是否满足给定高度和净空余量。
# 输入为计划、世界高度和余量；不满足时抛出异常，此检查不是实时避障。
def check_building_clearance(plan, altitude_world_m, margin_m=3):
    """已知道路/接入段与建筑冲突时拒绝，固定高度不是实时避障。"""
    from shapely.ops import unary_union
    paths = [LineString(leg["points"]) for leg in plan["legs"] if leg["length_m"] > 0.01]
    corridor = unary_union(paths).buffer(1.0)
    data = json.loads(MAP_PATH.with_name("buildings.json").read_text(encoding="utf-8-sig"))
    for feature in data["features"]:
        height = feature["properties"].get("z_max_m")
        if height is None:
            if shape(feature["geometry"]).intersects(corridor):
                raise ValueError("路径附近建筑缺少高度，无法核实飞行高度")
        elif altitude_world_m <= height + margin_m and shape(feature["geometry"]).intersects(corridor):
            raise ValueError(f"飞行高度不足以避开路径上的建筑{feature['id']}，请调整高度")
