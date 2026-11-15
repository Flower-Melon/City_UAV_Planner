"""实时读取异常车UE全局位置，执行三架无人机统一200米高空追踪。"""

import json
import logging
import math
from pathlib import Path
import threading
import time

from ..runtime.airsim_client import AirSimClient
from ..runtime.airsim_state import WorldFrame, vector
from ..runtime.flight_control import FlightControl, _action, vehicle_lock
from ..runtime.kill_trees import CameraTreeFilter
from ..utils.plan_flight_path import _flight_graph, _buildings, _clear_path, plan_flight_path
from .fly_to_point import _world_position


PURSUIT_HEIGHT_M = 200.0


def _quaternion_product(a, b):
    w, x, y, z = a
    v, i, j, k = b
    return [w*v-x*i-y*j-z*k, w*i+x*v+y*k-z*j,
            w*j-x*k+y*v+z*i, w*k+x*j-y*i+z*v]


def camera_orientation(body, direction):
    """将世界NED中的视线转换为机身坐标相机姿态，补偿飞行俯仰与横滚。"""
    dx, dy, down = direction
    horizontal = math.hypot(dx, dy)
    pitch = -math.atan2(max(1, down), min(horizontal, max(1, down) * math.tan(math.radians(40))))
    yaw = math.atan2(dy, dx)
    cy, sy, cp, sp = math.cos(yaw/2), math.sin(yaw/2), math.cos(pitch/2), math.sin(pitch/2)
    world = [cy*cp, -sy*sp, cy*sp, sy*cp]
    q = [body[k] for k in ('w_val', 'x_val', 'y_val', 'z_val')]
    norm = math.hypot(*q)
    inverse = [q[0]/norm, -q[1]/norm, -q[2]/norm, -q[3]/norm]
    # bottom_center 已带 -90°安装角；simSetCameraPose 施加的是安装坐标中的增量。
    mount_inverse = [math.sqrt(0.5), 0.0, math.sqrt(0.5), 0.0]
    local = _quaternion_product(mount_inverse, _quaternion_product(inverse, world))
    return dict(zip(('w_val', 'x_val', 'y_val', 'z_val'), local))


def _write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


class VehicleTarget:
    """三机共享的车辆实时全局状态，只接受UE直接读回的位置与运动数据。"""
    def __init__(self, observation):
        self.lock = threading.RLock()
        self.updates = 0
        self.update(observation)

    def update(self, observation):
        with self.lock:
            self.position = list(observation['position_m'])
            self.velocity = list(observation['velocity_m_s'])
            self.stamp = observation['simulation_time_s']
            self.heading = observation['heading_rad']
            self.updates += 1

    def snapshot(self):
        with self.lock:
            return {'position_m': list(self.position), 'velocity_m_s': list(self.velocity),
                    'heading_rad': self.heading, 'last_seen_s': self.stamp,
                    'source': 'ue_global_position', 'position_updates': self.updates}


class PursuitTerrain:
    """从公共地图读取地面高度，三机统一保持200米离地高度。"""
    def __init__(self):
        self.graph = _flight_graph()

    def altitude(self, xy):
        return self.graph.ground_near(xy) + PURSUIT_HEIGHT_M


class PursuitDrone:
    def __init__(self, name, return_to_monitor, lock, frame, target, terrain, stop, shared):
        self.name, self.role, self.task_lock = name, 'tracking', lock
        self.return_to_monitor = return_to_monitor
        self.client = AirSimClient(timeout=10)
        self.camera_client = AirSimClient(timeout=10)
        # 每个控制线程独占 RPC 连接；坐标标定值可以复用。
        self.frame = object.__new__(WorldFrame)
        self.frame.client, self.frame.anchor, self.frame.scale = self.client, frame.anchor, frame.scale
        self.target, self.terrain = target, terrain
        self.stop_event, self.shared = stop, shared
        self.start, self.local, self.origin = self.frame.vehicle(name)
        # 落地后的小幅漂移不改变返航基点；增援机使用配置的局部坐标原点。
        self.home = self.start if return_to_monitor else [*self.origin[:2], self.start[2]]
        self.flight = FlightControl(self.client, self.frame, name, self.origin)
        self.filter = CameraTreeFilter(self.client, name)
        self.camera_error = None
        self.phase, self.error = 'preparing', None
        self.position = list(self.start)
        self.thread = None
        self.recovering_height = False
        self.control = {}

    def goal(self, now):
        target = self.target.snapshot()
        point, velocity = target['position_m'], target['velocity_m_s']
        age = max(0, now - target['last_seen_s'])
        offset = self.shared['formation_offsets'][self.name]
        xy = [point[i] + velocity[i] * min(age + 0.75, 1.5) + offset[i] for i in (0, 1)]
        return [*xy, self.terrain.altitude(xy)]

    def launch(self):
        """增援机升至统一追踪高度，按高度误差和垂直速度提前制动。"""
        altitude = self.terrain.altitude(self.start[:2])
        deadline = time.monotonic() + max(60.0, abs(altitude - self.start[2]) / 6.0 * 4.0 + 30.0)
        while not self.stop_event.is_set():
            self.position = list(_world_position(self.client, self.name, self.origin, self.frame.scale))
            state = self.client.call('getMultirotorState', self.name)
            down = vector(state['kinematics_estimated']['linear_velocity'])[2] * self.frame.scale
            error = altitude - self.position[2]
            self.flight._collision()
            if abs(error) <= 2.0 and abs(down) <= 0.5:
                return
            if time.monotonic() > deadline:
                raise RuntimeError(f'{self.name} 追踪起飞超时')
            up = max(-3.0, min(6.0, error * 0.5 + down * 0.8))
            _action(self.client, 'moveByVelocity', 0.0, 0.0, -up / self.frame.scale,
                    0.25, 0, {'is_rate': True, 'yaw_or_rate': 0.0}, self.name, timeout=5)

    def run(self):
        try:
            self.flight.prepare(self.start, self.local)
            try:
                self.filter.start(reclaim_stale=True)
            except Exception as error:
                self.camera_error = str(error)
            if not self.return_to_monitor:
                self.phase = 'launching'
                self.launch()
            self.phase = 'tracking'
            command = [0.0, 0.0]
            while not self.stop_event.is_set():
                self.position = list(_world_position(self.client, self.name, self.origin, self.frame.scale))
                state = self.client.call('getMultirotorState', self.name)
                velocity = [v * self.frame.scale for v in vector(state['kinematics_estimated']['linear_velocity'])]
                self.flight._collision()
                now = self.shared['simulation_time_s']
                if self.shared.get('paused'):
                    goal = None
                elif self.shared.get('active'):
                    goal = self.goal(now)
                else:
                    goal = None
                desired, vertical = [0.0, 0.0], 0.0
                if goal is not None:
                    delta = [goal[i] - self.position[i] for i in (0, 1)]
                    feed = self.target.snapshot()['velocity_m_s']
                    desired = [feed[i] + delta[i] * 0.6 for i in (0, 1)]
                    speed = math.hypot(*desired)
                    if speed > 40.0:
                        desired = [v * 40.0 / speed for v in desired]
                    altitude = goal[2]
                    vertical = max(-5, min(8, (altitude - self.position[2]) * 0.6 + velocity[2] * 0.8))
                change = [desired[i] - command[i] for i in (0, 1)]
                norm = math.hypot(*change)
                command = [command[i] + change[i] * min(1, 1.5 / max(norm, 1e-6)) for i in (0, 1)]
                predicted = [self.position[i] + velocity[i] * 4.0 for i in (0, 1)] + [self.position[2]]
                if not _clear_path([self.position, predicted], 2, 8):
                    command = [0.0, 0.0]
                if goal is not None:
                    if self.position[2] < altitude - 12:
                        self.recovering_height = True
                    elif self.position[2] >= altitude - 3:
                        self.recovering_height = False
                    if self.recovering_height:
                        command = [0.0, 0.0]
                self.control = {'goal_m': goal, 'velocity_command_m_s': [*command, vertical],
                                'recovering_height': self.recovering_height, 'position_m': list(self.position),
                                'height_agl_m': PURSUIT_HEIGHT_M}
                _action(self.client, 'moveByVelocity', command[0] / self.frame.scale,
                        command[1] / self.frame.scale, -vertical / self.frame.scale, 0.25,
                        0, {'is_rate': True, 'yaw_or_rate': 0.0}, self.name, timeout=5)
            self.phase = 'returning'
            self.flight.stop()
            current = _world_position(self.client, self.name, self.origin, self.frame.scale)
            destination = self.home if self.return_to_monitor else [*self.home[:2], self.home[2] + 3]
            cruise = max(current[2], destination[2])
            direct = [current, [*current[:2], cruise], [*destination[:2], cruise]]
            route = ([(*point, 20) for i, point in enumerate(direct)
                      if i == 0 or math.dist(point, direct[i - 1]) > 0.1]
                     if _clear_path(direct, 1, 3) else
                     plan_flight_path(current, [*destination[:2], cruise], 300, 30))
            self.flight.fly_route(route, on_progress=lambda distance, point:
                                  setattr(self, 'position', list(point)))
            # 在巡航高度精确对齐返航基点，再下降，避免航段容差把飞机带到旁边屋檐上。
            align_deadline = time.monotonic() + 45
            while True:
                current = _world_position(self.client, self.name, self.origin, self.frame.scale)
                self.position = list(current)
                state = self.client.call('getMultirotorState', self.name)
                velocity = vector(state['kinematics_estimated']['linear_velocity'])
                error = [destination[i] - current[i] for i in (0, 1)]
                if math.hypot(*error) < 0.5 and math.hypot(*velocity[:2]) * self.frame.scale < 0.2:
                    break
                if time.monotonic() > align_deadline:
                    raise RuntimeError('返航基点对齐超时')
                command = [max(-2, min(2, error[i] * 0.4 - velocity[i] * self.frame.scale * 1.2))
                           for i in (0, 1)]
                _action(self.client, 'moveByVelocity', command[0] / self.frame.scale,
                        command[1] / self.frame.scale, 0.0, 0.25, 0,
                        {'is_rate': True, 'yaw_or_rate': 0.0}, self.name, timeout=5)
            # SimpleFlight 在近地分段 moveToZ 时可能返回 False；用连续速度闭环下降。
            descend_deadline = time.monotonic() + max(90, (cruise - destination[2]) * 2)
            while True:
                current = _world_position(self.client, self.name, self.origin, self.frame.scale)
                self.position = list(current)
                remaining = current[2] - destination[2]
                if remaining <= 2:
                    break
                if time.monotonic() > descend_deadline:
                    raise RuntimeError('返航下降超时')
                down = min(5.0, max(0.25, remaining * 0.3))
                _action(self.client, 'moveByVelocity', 0.0, 0.0, down / self.frame.scale,
                        0.25, 0, {'is_rate': True, 'yaw_or_rate': 0.0}, self.name, timeout=5)
            self.flight.stop()
            self.flight.z = vector(self.client.call('simGetVehiclePose', self.name)['position'])[2]
            if not self.return_to_monitor:
                self.flight.land(self.home)
            self.position = list(_world_position(self.client, self.name, self.origin, self.frame.scale))
            self.phase = 'complete'
        except Exception as failure:
            self.error, self.phase = str(failure), 'failed'
            self.stop_event.set()
            try:
                self.flight.stop()
            except Exception:
                pass
        finally:
            try:
                self.filter.close()
            except Exception as failure:
                self.camera_error = str(failure)
            finally:
                self.client.close()

    def capture(self, save_image=False):
        """相机直接朝向实时位置；截图仅用于查看，不参与目标定位或飞行判断。"""
        if self.phase != 'tracking':
            return None
        body = self.camera_client.call('simGetVehiclePose', self.name)
        local = vector(body['position'])
        observer = [self.origin[0] + local[0] * self.frame.scale,
                    self.origin[1] + local[1] * self.frame.scale,
                    self.origin[2] - local[2] * self.frame.scale]
        point = self.target.snapshot()['position_m']
        direction = [point[0] - observer[0], point[1] - observer[1], observer[2] - point[2]]
        orientation = camera_orientation(body['orientation'], direction)
        self.camera_client.call('simSetCameraPose', 'bottom_center', {
            'position': {'x_val': 0.0, 'y_val': 0.0, 'z_val': 0.0}, 'orientation': orientation}, self.name, False)
        if not save_image:
            return None
        responses = self.camera_client.call('simGetImages', [{'camera_name': 'bottom_center',
            'image_type': 0, 'pixels_as_float': False, 'compress': True}], self.name, False)
        raw = responses[0]['image_data_uint8']
        if isinstance(raw, str):
            raw = raw.encode('utf-8', 'surrogateescape')
        return bytes(raw) if raw else None



def pursue_vehicle(vehicle_id, observer_id='UAV_04', *, duration_s=120, seed=None,
                   output_dir='output/vehicle_pursuit'):
    """直接读取车辆全局位置启动三机追踪；高空机须已就位，等待追踪和返航完成。"""
    from Scene.vehicle.escape_vehicle import start_escape, escape_status, _request
    from ..utils.select_UAV import _reserve_nearest_UAV

    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    terrain = PursuitTerrain()
    _buildings()
    control = AirSimClient(timeout=10)
    observer_lock = vehicle_lock(observer_id)
    if not observer_lock.acquire(blocking=False):
        control.close()
        raise RuntimeError('高空机仍被其他任务占用，请先停止原监测会话')
    locks, drones, stop = [observer_lock], [], threading.Event()
    started_escape = False
    summary = {'status': 'preparing', 'vehicle_id': vehicle_id, 'duration_s': duration_s,
               'position_source': 'ue_global_position', 'position_updates': 0, 'trail_m': [], 'evidence': []}

    def publish():
        for drone in drones:
            if drone.camera_error:
                summary.setdefault('camera_errors', {})[drone.name] = drone.camera_error
        summary['drone_states'] = [{'name': d.name, 'role': d.role, 'phase': d.phase,
                                    'position_m': d.position, 'error': d.error} for d in drones]
        _write_json(directory / 'latest.json', summary)
        state = {key: summary[key] for key in ('status', 'vehicle_id', 'duration_s', 'elapsed_s',
                 'target', 'position_source', 'position_updates', 'travelled_m', 'trail_m',
                 'drone_states', 'error', 'errors') if key in summary}
        if 'escape' in summary:
            state['travelled_m'] = summary['escape'].get('travelled_m', 0)
        state['evidence_count'] = len(summary['evidence'])
        logging.getLogger(__name__).info('车辆追踪状态', extra={'pursuit_state': state})

    try:
        frame = WorldFrame(control)
        observer_position, _, _ = frame.vehicle(observer_id)
        if control.call('getMultirotorState', observer_id)['landed_state'] == 0:
            raise RuntimeError('高空机尚未升空，请先启动区域监控')
        names = [(observer_id, True, observer_lock)]
        for _ in range(2):
            name, lock = _reserve_nearest_UAV(observer_position)
            locks.append(lock)
            names.append((name, False, lock))
        observation = _request(control, 'observe', vehicle_id=vehicle_id)
        if observation['error']:
            raise RuntimeError(observation['error'])
        target = VehicleTarget(observation)
        shared = {'simulation_time_s': observation['simulation_time_s'], 'paused': False, 'active': False,
                  'formation_offsets': {name: offset for (name, _, _), offset in
                                        zip(names, ((0.0, -18.0), (-16.0, 10.0), (16.0, 10.0)))}}
        for name, return_to_monitor, lock in names:
            drone = PursuitDrone(name, return_to_monitor, lock, frame, target, terrain, stop, shared)
            drones.append(drone)
            if return_to_monitor:
                drone.thread = threading.Thread(target=drone.run, name='pursuit-' + name)
                drone.thread.start()
        summary['drones'] = [{'name': d.name, 'role': d.role} for d in drones]
        publish()
        ready_deadline = time.monotonic() + 45
        while drones[0].phase == 'preparing':
            if (directory / 'stop.request').exists():
                raise RuntimeError('追踪已取消，未启动逃逸')
            if time.monotonic() > ready_deadline:
                raise RuntimeError('高空机接管超时')
            time.sleep(0.05)
        if drones[0].error:
            raise RuntimeError(drones[0].error)
        if (directory / 'stop.request').exists():
            raise RuntimeError('追踪已取消，未启动逃逸')
        initial = start_escape(vehicle_id, duration_s, seed)
        started_escape = True
        observation = _request(control, 'observe', vehicle_id=vehicle_id)
        target.update(observation)
        shared.update(simulation_time_s=observation['simulation_time_s'], active=True)
        for drone in drones[1:]:
            drone.thread = threading.Thread(target=drone.run, name='pursuit-' + drone.name)
            drone.thread.start()
        summary['status'] = 'tracking'
        publish()
        next_status, next_camera, next_image, image_drone = 0.0, 0.0, 0.0, 0
        deadline = time.monotonic() + duration_s * 6 + 120
        with (directory / 'observations.jsonl').open('w', encoding='utf-8') as records:
            while not stop.is_set():
                cycle_started = time.monotonic()
                if (directory / 'stop.request').exists():
                    raise RuntimeError('收到追踪停止请求')
                if cycle_started > deadline:
                    raise RuntimeError('追踪等待仿真时间推进超时')
                shared['paused'] = control.call('simIsPaused')
                if shared['paused']:
                    stop.wait(0.1)
                    continue
                observation = _request(control, 'observe', vehicle_id=vehicle_id)
                if observation['error']:
                    raise RuntimeError(observation['error'])
                now = observation['simulation_time_s']
                if now < initial['started_at_s']:
                    raise RuntimeError('仿真时间已回退，追踪结束')
                target.update(observation)
                shared['simulation_time_s'] = now
                summary.update(target=target.snapshot(), elapsed_s=now - initial['started_at_s'],
                               position_updates=target.updates, travelled_m=observation['travelled_m'])
                if not summary['trail_m'] or math.dist(summary['trail_m'][-1], observation['position_m'][:2]) >= 3.0:
                    summary['trail_m'].append(observation['position_m'][:2])
                    summary['trail_m'] = summary['trail_m'][-600:]
                records.write(json.dumps({'simulation_time_s': now, 'target': target.snapshot(),
                    'drones': [{'name': d.name, 'role': d.role, 'flight': dict(d.control)} for d in drones]}) + '\n')
                records.flush()
                if observation['escape_phase'] == 'complete':
                    break
                if now - initial['started_at_s'] > duration_s + 15:
                    raise RuntimeError('逃逸计时结束后车辆未能完成制动')
                # 相机不反馈轨迹。轮流保存截图，避免三机同时取图拖慢位置读取。
                if now >= next_camera:
                    save_image = now >= next_image
                    for index, drone in enumerate(drones):
                        try:
                            raw = drone.capture(save_image=save_image and index == image_drone)
                            if raw:
                                filename = '{}_{:06d}.png'.format(drone.name, len(summary['evidence']) + 1)
                                (directory / filename).write_bytes(raw)
                                summary['evidence'].append(filename)
                        except Exception as error:
                            summary.setdefault('camera_errors', {})[drone.name] = str(error)
                    if save_image:
                        next_image, image_drone = now + 2.0, (image_drone + 1) % len(drones)
                    next_camera = now + 0.5
                publish()
                if now >= next_status:
                    print(json.dumps({'elapsed_s': round(summary['elapsed_s'], 1),
                                      'source': 'ue_global_position', 'position_updates': target.updates,
                                      'drones': [(d.name, d.phase) for d in drones]}), flush=True)
                    next_status = now + 10
                stop.wait(max(0.0, 0.1 - (time.monotonic() - cycle_started)))
        failures = [d.error for d in drones if d.error]
        summary['escape'] = escape_status(vehicle_id)
        if failures:
            raise RuntimeError('; '.join(failures))
        summary['status'] = 'returning'
        publish()
    except BaseException as error:
        summary.update(status='failed', error=str(error))
        raise
    finally:
        stop.set()
        if summary['status'] == 'failed':
            summary['outcome'] = 'failed'
            summary['status'] = 'returning'
        publish()
        if started_escape:
            try:
                summary['escape'] = escape_status(vehicle_id, stop=True)
            except Exception as error:
                summary['escape_error'] = str(error)
        for drone in drones:
            if drone.thread is not None:
                next_return_log = 0.0
                while drone.thread.is_alive():
                    drone.thread.join(timeout=0.2)
                    if time.monotonic() >= next_return_log:
                        print(json.dumps({'returning': drone.name, 'phase': drone.phase}), flush=True)
                        next_return_log = time.monotonic() + 10.0
                    publish()
            try:
                drone.camera_client.call('simSetCameraPose', 'bottom_center', {
                    'position': {'x_val': 0.0, 'y_val': 0.0, 'z_val': 0.0},
                    'orientation': {'w_val': 1.0, 'x_val': 0.0,
                                    'y_val': 0.0, 'z_val': 0.0}}, drone.name, False)
            except Exception as error:
                summary.setdefault('camera_errors', {})[drone.name] = str(error)
            finally:
                drone.camera_client.close()
                if drone.thread is None:
                    drone.client.close()
        failures = [d.error for d in drones if d.error]
        summary['drone_states'] = [{'name': d.name, 'role': d.role, 'phase': d.phase,
                                    'position_m': d.position, 'error': d.error} for d in drones]
        if summary.get('outcome') == 'failed':
            summary['status'] = 'failed'
        elif summary['status'] == 'returning':
            if summary.get('escape', {}).get('travelled_m', 0) < 20:
                failures.append('车辆未实际驶离起点，逃逸试验未完成')
            elif summary.get('escape', {}).get('max_departure_m', 0) < min(80, duration_s * 2):
                failures.append('车辆未能离开起点附近，逃逸试验未完成')
            summary['status'] = 'failed' if failures else 'complete'
            summary['errors'] = failures
        _write_json(directory / 'result.json', summary)
        publish()
        for lock in reversed(locks):
            lock.release()
        control.close()
    if summary['status'] != 'complete':
        raise RuntimeError('追踪或返航失败：' + '; '.join(summary.get('errors', [])))
    return summary
