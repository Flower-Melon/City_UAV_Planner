"""让已有异常车辆驶离原街区，沿有向车道持续行驶。"""

from collections import defaultdict, deque, namedtuple
from functools import lru_cache
import heapq
import json
import math
from pathlib import Path
import random
import sys

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "Scene.vehicle"


Road = namedtuple("Road", "id start end points length")


def _distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


@lru_cache(maxsize=1)
def _road_network():
    # 在 UE 内使用标准库加载路网，避免依赖编辑器 Python 环境中的第三方包。
    path = Path(__file__).resolve().parents[2] / "Map/roads/road_network.json"
    network = json.loads(path.read_text(encoding="utf-8"))
    driving = {lane["id"] for lane in network["lanes"] if lane["type"] == "driving"}
    nodes = {node for edge in network["edges"] if edge.get("lane_id") in driving
             for node in (edge["from_node_id"], edge["to_node_id"])}
    positions = {node["id"]: tuple(node["position_m"][:2]) for node in network["nodes"] if node["id"] in nodes}
    roads, endpoints = [], {}
    for edge in network["edges"]:
        start, end = edge["from_node_id"], edge["to_node_id"]
        if edge.get("lane_id") not in driving and not (edge["kind"] == "connection" and start in nodes and end in nodes):
            continue
        points = [tuple(point) for point in edge["polyline_xy_m"]]
        if len(points) < 2:
            continue
        endpoints[edge["id"]] = end, points[-1]
        if _distance(points[0], positions[start]) > 0.1 or _distance(points[-1], positions[end]) > 0.1:
            continue
        # 同一节点的相邻车道使用同一个接点，保证后续拼接连续。
        points[0], points[-1] = positions[start], positions[end]
        line = [points[0]]
        for point in points[1:]:
            if _distance(line[-1], point) > 1e-6:
                line.append(point)
        length = sum(_distance(a, b) for a, b in zip(line, line[1:]))
        roads.append(Road(edge["id"], start, end, line, length))
    return roads, endpoints, positions


@lru_cache(maxsize=4)
def _tile_network(bounds):
    roads, endpoints, positions = _road_network()
    xmin, ymin, xmax, ymax = bounds
    outgoing, incoming = defaultdict(list), defaultdict(list)
    for road in roads:
        if all(xmin <= x <= xmax and ymin <= y <= ymax for x, y in road.points):
            outgoing[road.start].append(road)
            incoming[road.end].append(road)
    return outgoing, incoming, endpoints, positions


def _reachable(root, adjacency, *, reverse=False):
    reached, pending = {root}, [root]
    while pending:
        for road in adjacency.get(pending.pop(), ()):
            node = road.start if reverse else road.end
            if node not in reached:
                reached.add(node)
                pending.append(node)
    return reached


def _continue_escape(node, outgoing, available, original, away, seed, recent):
    rng = random.Random(seed)
    recent = deque(recent, maxlen=80)
    while True:
        # 先穿过所有可达的零长度路口连接，再选一段会实际推进车辆的道路。
        junction, pending, choices = {node}, [node], []
        while pending:
            for road in outgoing.get(pending.pop(), ()):
                if road.end not in available:
                    continue
                if road.length > 1e-6:
                    choices.append(road)
                elif road.end not in junction:
                    junction.add(road.end)
                    pending.append(road.end)
        road = min(choices, key=lambda item: (item.id in original, item.id in recent, item.end not in away, rng.random()))
        recent.append(road.id)
        node = road.end
        yield road.points


def _plan_escape(route, distances, progress, bounds, seed):
    outgoing, incoming, endpoints, positions = _tile_network(bounds)
    original, edge_ids = route["points"], route["edge_ids"]
    original_ids = set(edge_ids)
    departures = []
    index = 0
    for edge_id in edge_ids:
        node, end = endpoints[edge_id]
        index = next(i for i in range(index, len(original)) if _distance(original[i], end) <= 1e-6)
        departures.append((index, node))

    root = next((node for _, node in departures if node in outgoing), None)
    tile = route["tile"].replace("_", "-")
    if root is None:
        raise ValueError(f"{tile} 区内没有可接入的逃逸车道")
    # 只选可持续通行的连通部分，避免续路时进入无出口路段。
    available = _reachable(root, outgoing) & _reachable(root, incoming, reverse=True)
    xs, ys = zip(*original)
    left, right, top, bottom = min(xs), max(xs), min(ys), max(ys)
    separation = {node: math.hypot(max(left - x, 0, x - right), max(top - y, 0, y - bottom))
                  for node in available for x, y in (positions[node],)}
    furthest = max(separation.values())
    if furthest == 0:
        raise ValueError(f"{tile} 区内没有可驶离原路线并持续通行的车道")
    away = {node for node, gap in separation.items() if gap >= min(100.0, furthest)}

    # 用外部路段作为首段的搜索目标，按车辆此刻的路线进度选择前方出口。
    costs = dict.fromkeys(away, 0.0)
    queue = [(0.0, node) for node in away]
    heapq.heapify(queue)
    next_road = {}
    while queue:
        cost, node = heapq.heappop(queue)
        if cost != costs[node]:
            continue
        for road in incoming.get(node, ()):
            candidate = cost + max(road.length, 1e-6) * (2 if road.id in original_ids else 1)
            if road.start in available and candidate < costs.get(road.start, math.inf):
                costs[road.start] = candidate
                next_road[road.start] = road
                heapq.heappush(queue, (candidate, road.start))

    exits = [((distances[i] - progress) % distances[-1], i, node)
             for i, node in departures if node in costs]
    ahead, departure, node = min(exits, key=lambda item: (item[0] + costs[item[2]], item[0]))
    points, recent = [original[departure]], []
    while node in next_road:
        road = next_road[node]
        points.extend(road.points)
        recent.append(road.id)
        node = road.end
    return ahead, points, _continue_escape(node, outgoing, available, original_ids, away, seed, recent)


def escape_vehicle(vehicle_id, *, seed=None) -> None:
    """启动持续逃逸，确认后返回。"""
    from .spawn_vehicle import AirSimClient, request

    with AirSimClient(timeout=45) as client:
        request(client, "escape", vehicle_id=vehicle_id, seed=seed)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vehicle_id", nargs="?", help="异常车 ID，默认选择当前仿真中编号最小的异常车")
    parser.add_argument("--seed", type=int, default=42, help="续路选择种子，默认 %(default)s")
    args = parser.parse_args()
    vehicle_id = args.vehicle_id
    if vehicle_id is None:
        from .spawn_vehicle import AirSimClient

        with AirSimClient() as client:
            vehicles = client.call("simListSceneObjects", r"^abnormal_vehicle_[0-9]+$")
        if not vehicles:
            parser.error("当前仿真中没有异常车辆，请先生成一辆")
        vehicle_id = min(vehicles, key=lambda name: int(name.rsplit("_", 1)[1]))
    escape_vehicle(vehicle_id, seed=args.seed)
    print(vehicle_id)
