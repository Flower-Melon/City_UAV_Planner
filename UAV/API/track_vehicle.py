"""在 UE 仿真中演示三机协同追踪。"""

import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
import logging
import math
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
from uuid import uuid4

if __name__ == '__main__' and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = 'UAV.API'

from PIL import Image
from shapely.geometry import LineString, Point

from Scene.vehicle.spawn_vehicle import request as ue_request
from ..runtime.airsim_client import AirSimClient
from ..runtime.airsim_state import WorldFrame
from ..runtime.flight_control import FLIGHT_PERIOD, FlightControl, wait_for_flight_tick
from ..utils.plan_flight_path import _buildings, _clear_path, _flight_graph, _positive
from ..utils.select_UAV import _reserve_pursuit_UAVs


FOLLOW_SECONDS = 8.0
FORMATION_DISTANCE_M = 24.0
FORMATION_GRACE_S = 1.5
VISUAL_MAX_AGE_S = 4.0
PURSUIT_TIMEOUT_S = 120.0
CAPTURE_PERIOD_S = 0.20
ROLES = (('lead', -28, -20, 32), ('scout', 49, 26, 43), ('wing', -5, 61, 57))
CAMERA_SCRIPT = Path(__file__).resolve().parents[1] / 'runtime' / 'pursuit_camera.py'
DEFAULT_SPEED = 30.0
FOLLOW_BLEND = 1 - math.exp(-FLIGHT_PERIOD / 0.15)
logger = logging.getLogger(__name__)


def clearance_height(start, end, minimum):
    roof_map = _buildings()
    path = LineString([start, end]) if start != end else Point(start)
    area = path.buffer(4)
    for item in roof_map.tree.query(area, predicate='intersects'):
        top = roof_map.tops[int(item)]
        if top is not None:
            minimum = max(minimum, float(top) + 8)
    return minimum


class DemoDrone:
    def __init__(self, client, frame, name, returns_to_monitor, slot, speed):
        position, _, origin = frame.vehicle(name)
        self.flight = FlightControl(client, frame, name, origin)
        self.name = name
        self.home = tuple(position if returns_to_monitor else frame.home(name))
        self.returns_to_monitor = returns_to_monitor
        self.role, self.ahead, self.side, self.height = ROLES[slot]
        self.delay = slot * 1.2
        self.speed = speed
        self.vertical_speed = min(15.0, speed)
        self.phase = 'waiting'
        self.active = False
        self.in_position = False
        self.initial_position = position
        self.return_route = []
        self.follow_goal = None

    def follow(self, goal, xy):
        flight = self.flight
        if not self.active:
            flight.prepare()
            self.home_yaw = flight.yaw
            self.active = True
            position = flight.position
            minimum = max(position[2], goal[2])
            if not self.returns_to_monitor:
                minimum = max(minimum, self.home[2] + self.height)
            self.join_point = (*position[:2], clearance_height(position[:2], goal[:2], minimum))
            self.phase = 'joining'

        position = flight.position
        yaw = math.atan2(xy[1] - position[1], xy[0] - position[0])
        if self.phase == 'joining':
            flight.step(self.join_point, self.vertical_speed, yaw=yaw)
            if flight.position == self.join_point:
                self.phase = 'approaching'
            return

        if self.phase == 'following':
            self.follow_goal = tuple(a + (b - a) * FOLLOW_BLEND for a, b in zip(self.follow_goal, goal))
        else:
            self.follow_goal = goal
        target, speed = self.follow_goal, self.speed
        far = self.phase == 'approaching' and math.dist(position[:2], target[:2]) > 45.0
        if far or not _clear_path([position, target], 4, 5):
            altitude = clearance_height(position[:2], target[:2], max(position[2], target[2]))
            if position[2] < altitude - 0.05:
                target, speed = (*position[:2], altitude), self.vertical_speed
            else:
                target = (*target[:2], altitude)
        flight.step(target, speed, yaw=yaw)
        self.in_position = math.dist(flight.position, goal) <= FORMATION_DISTANCE_M
        if self.in_position:
            self.phase = 'following'

    def start_return(self):
        self.phase = 'returning'
        position = self.flight.position
        descent = (self.home, self.vertical_speed, self.home_yaw)
        if math.dist(position[:2], self.home[:2]) <= 0.15:
            self.return_route = [descent]
            return
        altitude = clearance_height(position[:2], self.home[:2], max(position[2], self.home[2] + 15.0))
        self.return_route = [
            ((*position[:2], altitude), self.vertical_speed, None),
            ((*self.home[:2], altitude), self.speed, None),
            descent,
        ]

    def step_return(self):
        point, speed, yaw = self.return_route[0]
        if not self.flight.step(point, speed, yaw=yaw):
            return
        self.return_route.pop(0)
        if self.return_route:
            return
        if self.returns_to_monitor:
            self.flight.confirm_position(self.home, yaw=self.home_yaw)
        else:
            self.flight.finish_landing(self.home, yaw=self.home_yaw)
        self.phase = 'complete'

    def state(self):
        return {'name': self.name, 'role': self.role, 'phase': self.phase,
                'position_m': self.flight.position or self.initial_position}


class Pursuit:
    """同一演示时钟驱动三机队形、接力镜头和完成条件。"""

    def __init__(self, target, drones):
        self.started = self.time = target['last_seen_s']
        self.heading = target['heading_rad']
        self.drones = drones
        self.camera = None
        self.obscured_since = None
        self.visible_since = None
        self.overhead = False
        self.view_mix = 0.0
        self.follow_seconds = 0.0
        self.last_ready = None
        self.last_formation_view = None
        self.update_target(target)

    def update_target(self, target):
        now = target['last_seen_s']
        if now < self.time:
            raise RuntimeError('仿真已重启，追踪结束')
        dt = min(now - self.time, 0.3)
        self.time = now
        self.view_mix += (float(self.overhead) - self.view_mix) * (1 - math.exp(-dt / 0.7))
        difference = math.remainder(target['heading_rad'] - self.heading, math.tau)
        self.heading += difference * (1 - math.exp(-dt / 0.65))
        cos, sin = math.cos(self.heading), math.sin(self.heading)
        x, y, z = target['position_m']
        vx, vy = target['velocity_m_s']
        center_x, center_y = x + vx * 0.35, y + vy * 0.35
        goals = []
        for drone in self.drones:
            mix = self.view_mix if drone.role == 'lead' else 0.0
            ahead = drone.ahead + (-8 - drone.ahead) * mix
            side = drone.side + (-7 - drone.side) * mix
            height = drone.height + (46 - drone.height) * mix
            tx = center_x + ahead * cos - side * sin
            ty = center_y + ahead * sin + side * cos
            point = (tx, ty)
            altitude = max(z, _flight_graph().ground_near(point)) + height
            goals.append(((*point, clearance_height(point, point, altitude)), (x, y)))
        self.goals = goals

    def fly(self, stop):
        """镜头采样在另一条 RPC 连接运行，飞行节拍独立。"""
        launched = time.monotonic()
        while not stop.is_set():
            started = time.monotonic()
            goals = self.goals
            for drone, (goal, xy) in zip(self.drones, goals):
                if started - launched >= drone.delay:
                    drone.follow(goal, xy)
            wait_for_flight_tick(started)

    def observe_view(self, frame, ready):
        now = frame['target']['last_seen_s']
        self.camera = frame['camera']
        self.obscured_since = None if frame['visible'] else (
            now if self.obscured_since is None else self.obscured_since)
        if frame['visible']:
            self.visible_since = now if self.visible_since is None else self.visible_since
            if now - self.visible_since >= 3.0:
                self.overhead = False
        else:
            self.visible_since = None
            if frame['camera'] and now - self.obscured_since >= 1.0:
                self.overhead = True
        if self.last_ready is not None and now - self.last_ready > FORMATION_GRACE_S:
            self.follow_seconds = 0.0
            self.last_formation_view = self.last_ready = None
        if not ready:
            return False
        # 只累计两次连续有效观测之间的时间，短暂掉队期间暂停累计。
        if self.last_ready == self.time:
            self.follow_seconds += min(max(0.0, now - self.time), 0.3)
        self.last_ready = now
        if frame['visible']:
            self.last_formation_view = now
        return (self.follow_seconds >= FOLLOW_SECONDS and self.last_formation_view is not None
                and now - self.last_formation_view <= VISUAL_MAX_AGE_S)


def _camera_request(client, session, operation='sample', **data):
    return ue_request(client, operation, _controller=CAMERA_SCRIPT, _module_name='city_pursuit_camera',
                      _registry='_targets', session=session, **data)


def _return_drones(drones, publish):
    pending = [(slot, drone) for slot, drone in enumerate(drones) if drone.active]
    start = time.monotonic()
    next_log = start
    failures = []
    last_progress = {}
    while pending:
        started = time.monotonic()
        remaining = []
        for index, drone in pending:
            if started - start >= index * 0.7:
                try:
                    if drone.phase != 'returning':
                        drone.start_return()
                        last_progress[drone.name] = started
                    previous = (drone.flight.position, drone.flight.yaw, len(drone.return_route))
                    drone.step_return()
                    if previous != (drone.flight.position, drone.flight.yaw, len(drone.return_route)):
                        last_progress[drone.name] = started
                    elif started - last_progress[drone.name] > 5.0:
                        raise RuntimeError(f'{drone.name} 返航位姿未更新')
                except Exception as error:
                    drone.phase = 'failed'
                    failures.append(error)
                    continue
            if drone.phase != 'complete':
                remaining.append((index, drone))
        pending = remaining
        if started >= next_log or not pending:
            publish()
            next_log = started + 0.15
        wait_for_flight_tick(started)
    if failures:
        raise RuntimeError(f'无人机归位失败：{failures[0]}') from failures[0]


def track_vehicle(vehicle_id, observer_id):
    """启动一次追踪演示。传入 None 时自动选车或选机。"""
    return _track_vehicle(vehicle_id, observer_id, threading.Event())


def _track_vehicle(vehicle_id, observer_id, stop, *, on_frame=None, auto_observer=True,
                   flight_speed=DEFAULT_SPEED, by_drone=None):
    selected, drones = [], []
    session = uuid4().hex
    summary = {'status': 'preparing', 'vehicle_id': vehicle_id, 'trail_m': [],
               'position_source': 'ue_global_position', 'position_updates': 0}
    client = AirSimClient(timeout=5)
    motion_client = AirSimClient(timeout=5)
    motion_stop = threading.Event()
    motion_pool = motion = None

    def publish():
        summary['drone_states'] = [drone.state() for drone in drones]
        logger.info('车辆追踪状态', extra={'pursuit_state': dict(summary)})

    try:
        if vehicle_id is None:
            names = client.call('simListSceneObjects', r'^abnormal_vehicle_[0-9]+$')
            if not names:
                raise RuntimeError('当前仿真中没有异常车辆，请先生成一辆')
            vehicle_id = min(names, key=lambda name: int(name.rsplit('_', 1)[1]))
        summary['vehicle_id'] = vehicle_id
        _flight_graph()
        _buildings()
        initial = _camera_request(client, session, vehicle_id=vehicle_id, views=[])['target']
        frame = WorldFrame(client)
        selected = _reserve_pursuit_UAVs(client, frame, observer_id, initial['position_m'],
                                         auto_observer=auto_observer)
        for slot, (name, _, monitor) in enumerate(selected):
            speed = _positive((by_drone or {}).get(name, {}).get('flight_speed', flight_speed), '飞行速度')
            drones.append(DemoDrone(motion_client, frame, name, monitor, slot, speed))
        pursuit = Pursuit(initial, drones)
        target = initial
        last_xy = target['position_m'][:2]
        travelled = 0.0
        last_frame = next_log = 0.0
        last_capture = {}
        brake_deadline = None
        pursuit_deadline = time.monotonic() + PURSUIT_TIMEOUT_S
        summary['status'] = 'tracking'
        publish()
        print(json.dumps({'vehicle_id': vehicle_id, 'roles': {d.role: d.name for d in drones}}), flush=True)
        motion_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='pursuit-flight')
        motion = motion_pool.submit(pursuit.fly, motion_stop)

        with TemporaryDirectory(prefix='city-pursuit-camera-') as directory:
            while not stop.is_set():
                started = time.monotonic()
                if motion.done():
                    motion.result()
                if brake_deadline is None and started >= pursuit_deadline:
                    summary.update(outcome='timeout', error='追踪超时，未满足截停条件')
                    break
                phases = [(drone.name, drone.phase, drone.in_position) for drone in drones]
                views = [name for name, phase, _ in phases if phase != 'waiting']
                available = [name for name, _, in_position in phases if in_position]
                ready = len(available) >= 2
                allow_switch = pursuit.camera is None or (pursuit.obscured_since is not None
                               and pursuit.time - pursuit.obscured_since >= 0.5)
                capture_camera = None
                if on_frame is not None and views and started >= last_frame + CAPTURE_PERIOD_S:
                    capture_camera = min(views, key=lambda name: last_capture.get(name, 0.0))
                sample = _camera_request(client, session, vehicle_id=vehicle_id, views=views, ready=available,
                                         camera=pursuit.camera, allow_switch=allow_switch,
                                         capture_camera=capture_camera, directory=directory if capture_camera else None)
                target = sample['target']
                complete = pursuit.observe_view(sample, ready)
                pursuit.update_target(target)
                xy = target['position_m'][:2]
                travelled += math.dist(last_xy, xy)
                last_xy = xy
                summary.update(target=target, elapsed_s=target['last_seen_s'] - pursuit.started,
                               travelled_m=travelled, active_camera=sample['camera'],
                               target_visible=sample['visible'],
                               formation_size=len(available), formation_seconds=round(pursuit.follow_seconds, 2),
                               position_updates=summary['position_updates'] + 1)
                if not summary['trail_m'] or math.dist(summary['trail_m'][-1], xy) >= 3:
                    summary['trail_m'] = [*summary['trail_m'][-599:], xy]
                if capture_camera:
                    metadata = sample['frame']
                    with Image.open(Path(directory) / 'pursuit.png') as image, io.BytesIO() as encoded:
                        image.convert('RGB').save(encoded, format='JPEG', quality=82)
                        on_frame({**metadata, 'image': base64.b64encode(encoded.getvalue()).decode('ascii')})
                    last_frame = last_capture[capture_camera] = started
                if brake_deadline is not None:
                    if target['intercept_state'] == 'stopped' and math.hypot(*target['velocity_m_s']) == 0.0:
                        summary['outcome'] = 'intercepted'
                        break
                    if time.monotonic() >= brake_deadline:
                        raise RuntimeError('未能确认异常车辆已停稳')
                elif complete and not stop.is_set():
                    ue_request(client, 'intercept', vehicle_id=vehicle_id)
                    brake_deadline = time.monotonic() + 15.0
                publish()
                if on_frame is None and started >= next_log:
                    print(json.dumps({'status': summary['status'], 'visible': sample['visible'],
                                      'formation_size': len(available), 'formation_seconds': summary['formation_seconds'],
                                      'drones': [(d.name, d.phase) for d in drones]}), flush=True)
                    next_log = started + 3.0
                stop.wait(max(0.0, 0.085 - (time.monotonic() - started)))
    except KeyboardInterrupt:
        stop.set()
    except Exception as error:
        summary.update(status='failed', error=str(error))
        publish()
        raise
    finally:
        try:
            motion_stop.set()
            if motion_pool is not None:
                motion_pool.shutdown(wait=True)
            motion_error = motion.exception() if motion is not None else None
            try:
                if on_frame is not None and summary['position_updates']:
                    _camera_request(client, session, operation='close')
            except Exception:
                logger.exception('清理追踪镜头失败')
            if any(drone.active for drone in drones):
                if summary['status'] != 'failed':
                    summary['status'] = 'returning'
                    publish()
                try:
                    _return_drones(drones, publish)
                except Exception:
                    if summary['status'] != 'failed' and motion_error is None:
                        raise
                    logger.exception('无人机归位失败')
            if motion_error is not None and summary['status'] != 'failed':
                raise motion_error
        finally:
            for _, lock, _ in reversed(selected):
                lock.release()
            motion_client.close()
            client.close()

    summary['status'] = {'intercepted': 'complete', 'timeout': 'failed'}.get(summary.get('outcome'), 'stopped')
    publish()
    return summary


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('vehicle_id', nargs='?', help='异常车 ID，默认自动选择')
    parser.add_argument('observer_id', nargs='?', help='高空监控机名称，默认自动选择')
    args = parser.parse_args()
    result = track_vehicle(args.vehicle_id, args.observer_id)
    print(json.dumps(result, ensure_ascii=False))
