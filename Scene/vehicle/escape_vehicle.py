"""让已有异常车离开闭环，沿随机远处街区行驶；仿真计时结束后制动。"""

import bisect
import heapq
import json
import math
from pathlib import Path
import random
import sys

try:
    import unreal
except ImportError:
    unreal = None


def _request(client, operation, **parameters):
    from .spawn_vehicle import request
    return request(client, operation, _controller=__file__,
                   _module_name='city_escape_runtime', **parameters)


def plan_escape_route(graph, position, heading, duration_s, seed=None):
    """从当前同向车道继续前进，随机连接远处路网，不把车辆移到新路线起点。"""
    from shapely.geometry import LineString, Point

    rng = random.Random(seed)
    outgoing = {}
    for edge in graph.edges.values():
        outgoing.setdefault(edge['from_node_id'], []).append(edge)
    # 只向可持续互通的主路网扩展，避免随机选择有去无回的支路。
    reverse, nodes = {}, set()
    for edge in graph.edges.values():
        a, b = edge['from_node_id'], edge['to_node_id']
        reverse.setdefault(b, []).append(a)
        nodes.update((a, b))
    visited, order = set(), []
    for root in sorted(nodes):
        if root in visited:
            continue
        visited.add(root)
        stack = [(root, iter(outgoing.get(root, ())))]
        while stack:
            node, children = stack[-1]
            edge = next(children, None)
            if edge is None:
                order.append(node)
                stack.pop()
            elif edge['to_node_id'] not in visited:
                child = edge['to_node_id']
                visited.add(child)
                stack.append((child, iter(outgoing.get(child, ()))))
    visited, main_nodes = set(), set()
    for root in reversed(order):
        if root in visited:
            continue
        component, stack = set(), [root]
        visited.add(root)
        while stack:
            node = stack.pop()
            component.add(node)
            for previous in reverse.get(node, ()):
                if previous not in visited:
                    visited.add(previous)
                    stack.append(previous)
        if len(component) > len(main_nodes):
            main_nodes = component
    point = Point(position)

    def score(item):
        edge, line = item
        along = line.project(point)
        a, b = line.interpolate(max(0, along - 1)), line.interpolate(min(line.length, along + 1))
        angle = math.atan2(b.y - a.y, b.x - a.x)
        return line.distance(point) + 50 * (1 - math.cos(angle - heading))

    def distant_paths(node, origin):
        queue, best, previous = [(0.0, node)], {node: 0.0}, {}
        candidates = []
        while queue:
            cost, current = heapq.heappop(queue)
            if cost != best[current]:
                continue
            if cost > 1800:
                break
            if current != node and current in main_nodes:
                xy = previous[current][1]['points'][-1]
                if 550 < math.hypot(xy[0] - origin[0], xy[1] - origin[1]) < 1300:
                    candidates.append(current)
            for edge in outgoing.get(current, ()):
                end, candidate = edge['to_node_id'], cost + edge['length']
                if candidate < best.get(end, math.inf):
                    best[end], previous[end] = candidate, (current, edge)
                    heapq.heappush(queue, (candidate, end))
        return candidates, previous

    # 平行车道可能属于局部死路；在车身附近按同向程度选择可通往远处的入口。
    nearby = sorted(((edge, line) for edge, line in zip(graph.roads, graph.lines)
                     if line.distance(point) <= 8.0), key=score)
    first = next((edge for edge, line in nearby
                  if distant_paths(edge['to_node_id'], edge['points'][-1])[0]), None)
    if first is None:
        raise ValueError('车辆附近没有可连通远处街区的机动车道')
    edges, length, node = [first], first['length'], first['to_node_id']
    while length < max(1500, duration_s * 35 + 500):
        candidates, previous = distant_paths(node, edges[-1]['points'][-1])
        if not candidates:
            raise ValueError('当前车道无法连通远处街区')
        end = rng.choice(sorted(candidates))
        path, current = [], end
        while current != node:
            current, edge = previous[current]
            path.append(edge)
        path.reverse()
        edges.extend(path)
        length += sum(e['length'] for e in path)
        node = end
    points = []
    for edge in edges:
        if points and math.hypot(points[-1][0] - edge['points'][0][0],
                                 points[-1][1] - edge['points'][0][1]) > 0.1:
            raise ValueError('逃逸路网连接不连续')
        for xy in edge['points']:
            if not points or math.hypot(points[-1][0] - xy[0], points[-1][1] - xy[1]) > 1e-6:
                points.append(xy)
    return points


def start_escape(vehicle_id, duration_s=120.0, seed=None):
    """启动沿路网逃逸；追踪方通过observe持续读取车辆全局状态。"""
    from .spawn_vehicle import AirSimClient, road_graph
    if not math.isfinite(duration_s) or duration_s <= 0:
        raise ValueError('逃逸时长必须大于0')
    graph = road_graph()  # 在读取车辆起点前完成路网加载。
    with AirSimClient(timeout=45) as client:
        initial = _request(client, 'prepare', vehicle_id=vehicle_id)
        points = plan_escape_route(graph, initial['position_m'][:2], initial['heading_rad'], duration_s, seed)
        return _request(client, 'start', vehicle_id=vehicle_id, points=points, duration_s=duration_s)


def escape_status(vehicle_id, *, stop=False):
    from .spawn_vehicle import AirSimClient
    with AirSimClient(timeout=10) as client:
        return _request(client, 'stop' if stop else 'status', vehicle_id=vehicle_id)


class OpenRoute:
    """兼容现有车辆运动模型的开放折线；终点不回绕起点。"""
    def __init__(self, points):
        self.points = points
        self.ends = [0.0]
        for a, b in zip(points, points[1:]):
            self.ends.append(self.ends[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        self.length = self.ends[-1]

    def sample(self, progress, offset=0.0):
        s = max(0.0, min(self.length - 1e-6, progress))
        i = min(len(self.points) - 2, bisect.bisect_right(self.ends, s) - 1)
        a, b = self.points[i:i + 2]
        length = self.ends[i + 1] - self.ends[i]
        dx, dy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
        t = (s - self.ends[i]) / length
        return (a[0] + (b[0] - a[0]) * t - dy * offset,
                a[1] + (b[1] - a[1]) * t + dx * offset), math.atan2(dy, dx)

    def project_near(self, point, progress):
        low = max(0, bisect.bisect_right(self.ends, progress - 25) - 1)
        high = min(len(self.points) - 2, bisect.bisect_left(self.ends, progress + 25))
        return self.project(point, low, high)[1]

    def project(self, point, low=0, high=None):
        best = (math.inf, 0.0)
        for i in range(low, (len(self.points) - 2 if high is None else high) + 1):
            a, b = self.points[i:i + 2]
            dx, dy = b[0] - a[0], b[1] - a[1]
            t = max(0, min(1, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / (dx * dx + dy * dy)))
            candidate = (math.hypot(point[0] - a[0] - dx * t, point[1] - a[1] - dy * t),
                         self.ends[i] + t * (self.ends[i + 1] - self.ends[i]))
            best = min(best, candidate)
        return best

    def curve_limit(self, progress, speed, lateral_acceleration, maximum):
        return sys.modules['city_abnormal_runtime'].Loop.curve_limit(
            self, progress, speed, lateral_acceleration, maximum)


_vehicles = {}
_finished = {}
_tick_handle = None


class Escape:
    def __init__(self, vehicle, points, duration_s, unreal):
        self.vehicle, self.actor = vehicle, vehicle.actor
        route = OpenRoute(points)
        error, along = route.project(vehicle.motion.xy, high=min(len(points) - 2, 150))
        if error > 8:
            raise ValueError('车辆已驶离预备路线，请重新开始逃逸')
        vehicle.motion.loop, vehicle.motion.progress = route, along
        if vehicle.motion.settings['speeding_speed_kmh'] == 0:
            vehicle.motion.settings['speeding_speed_kmh'] = 104.4
        vehicle.motion.set_behavior('speeding')
        vehicle.route = dict(vehicle.route, points=points)
        # 逃逸车不跟随正常车流排队；规划、逐帧运动和恢复都跳过普通交通车。
        vehicle.ignore_normal_traffic = True
        vehicle.recovery_remaining = vehicle.centering_remaining = vehicle.blocked_seconds = 0.0
        vehicle.plan_timer = 0
        self.started = unreal.GameplayStatics.get_time_seconds(self.actor.get_world())
        self.duration_s = duration_s
        self.last_xy = tuple(vehicle.motion.xy)
        self.travelled_m = 0.0
        self.phase = 'escaping'
        self.initial_position = [*vehicle.motion.xy, vehicle.z]
        self.max_departure_m = 0.0
        self.validation_trace = []

    def stop(self):
        self.vehicle.motion.settings['speeding_speed_kmh'] = 0.0
        self.vehicle.recovery_remaining = 0.0
        self.vehicle.plan_timer = 0.0
        self.phase = 'stopping'

    def step(self, now):
        xy = self.vehicle.motion.xy
        travel = math.hypot(xy[0] - self.last_xy[0], xy[1] - self.last_xy[1])
        self.travelled_m += travel
        self.last_xy = tuple(xy)
        self.max_departure_m = max(self.max_departure_m,
                                  math.hypot(xy[0] - self.initial_position[0], xy[1] - self.initial_position[1]))
        if not self.validation_trace or now - self.validation_trace[-1][0] >= 0.2:
            self.validation_trace.append([now, xy[0], xy[1], self.vehicle.z])
        if self.phase == 'escaping' and now - self.started >= self.duration_s:
            self.stop()
        if self.phase == 'stopping' and self.vehicle.motion.speed < 0.1:
            self.vehicle.ignore_normal_traffic = False
            self.phase = 'complete'

    def snapshot(self, now):
        return {'vehicle_id': self.actor.get_name(), 'phase': self.phase,
                'simulation_time_s': now, 'started_at_s': self.started,
                'elapsed_s': now - self.started, 'duration_s': self.duration_s,
                'travelled_m': self.travelled_m, 'initial_position_m': self.initial_position,
                'max_departure_m': self.max_departure_m,
                'error': self.vehicle.error,
                'validation_trace': self.validation_trace if self.phase != 'escaping' else []}


def _tick(_delta):
    import unreal
    global _tick_handle
    for name, item in list(_vehicles.items()):
        if not unreal.SystemLibrary.is_valid(item.actor):
            _vehicles.pop(name)
            continue
        item.step(unreal.GameplayStatics.get_time_seconds(item.actor.get_world()))
        if item.phase == 'complete':
            _finished[name] = item.snapshot(unreal.GameplayStatics.get_time_seconds(item.actor.get_world()))
            _vehicles.pop(name)
    if not _vehicles and _tick_handle is not None:
        unreal.unregister_slate_post_tick_callback(_tick_handle)
        _tick_handle = None


def dispatch(source, result):
    import unreal
    global _tick_handle
    try:
        data = json.loads(Path(source).read_text(encoding='utf-8'))
        if data['operation'] == 'stop_all':
            for item in list(_vehicles.values()):
                if unreal.SystemLibrary.is_valid(item.actor):
                    item.stop()
            value = {'stopped': True}
            response = {'ok': True, 'result': value}
            target = Path(result)
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps(response), encoding='utf-8')
            temporary.replace(target)
            return
        runtime = sys.modules.get('city_abnormal_runtime')
        vehicle = getattr(runtime, '_vehicles', {}).get(data['vehicle_id'])
        if vehicle is None or not unreal.SystemLibrary.is_valid(vehicle.actor):
            raise ValueError('当前仿真中没有指定异常车')
        now = unreal.GameplayStatics.get_time_seconds(vehicle.actor.get_world())
        operation = data['operation']
        if operation == 'prepare':
            value = {'position_m': [*vehicle.motion.xy, vehicle.z], 'heading_rad': vehicle.motion.yaw,
                     'simulation_time_s': now, 'speed_m_s': vehicle.motion.speed,
                     'forward_clearance_m': vehicle.path_clearance(20.0, 8.0, 0.0)[0]}
        elif operation == 'observe':
            # Actor位置加世界原点偏移，直接返回UE全局米制坐标；不依赖相机或探头。
            world = vehicle.actor.get_world()
            location = vehicle.actor.get_actor_location()
            origin = unreal.GameplayStatics.get_world_origin_location(world)
            heading = math.radians(vehicle.actor.get_actor_rotation().yaw)
            speed = vehicle.motion.speed
            item = _vehicles.get(data['vehicle_id'])
            phase = item.phase if item is not None else _finished.get(data['vehicle_id'], {}).get('phase')
            value = {'vehicle_id': data['vehicle_id'],
                     'position_m': [(location.x + origin.x) / 100.0,
                                    (location.y + origin.y) / 100.0,
                                    (location.z + origin.z) / 100.0],
                     'velocity_m_s': [speed * math.cos(heading), speed * math.sin(heading)],
                     'heading_rad': heading, 'speed_m_s': speed, 'simulation_time_s': now,
                     'escape_phase': phase, 'error': vehicle.error,
                     'travelled_m': item.travelled_m if item is not None else
                         _finished.get(data['vehicle_id'], {}).get('travelled_m', 0.0)}
        elif operation == 'start':
            if data['vehicle_id'] in _vehicles and _vehicles[data['vehicle_id']].phase != 'complete':
                raise ValueError('该车辆已有逃逸任务')
            item = Escape(vehicle, data['points'], data['duration_s'], unreal)
            _vehicles[data['vehicle_id']] = item
            if _tick_handle is None:
                _tick_handle = unreal.register_slate_post_tick_callback(_tick)
            value = item.snapshot(now)
        elif operation in ('status', 'stop'):
            item = _vehicles.get(data['vehicle_id'])
            if item is None:
                value = _finished[data['vehicle_id']]
            else:
                if operation == 'stop':
                    item.stop()
                item.step(now)
                value = item.snapshot(now)
        else:
            raise ValueError('未知逃逸操作')
        response = {'ok': True, 'result': value}
    except Exception as error:
        response = {'ok': False, 'error': str(error)}
    target = Path(result)
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(response), encoding='utf-8')
    temporary.replace(target)
