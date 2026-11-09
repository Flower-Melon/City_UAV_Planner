"""高空车辆监测：取图、道路定位、测速和证据保存；由网页统一调度。

UE也会加载本文件执行道路测量，因此相机和模型依赖在调用时导入。
"""

from collections import deque
import json
import math
import time
from pathlib import Path
import uuid


TRACK_MAX_GAP_S = 3.0
TRACK_WINDOW_S = 3.0


def start_session(drone_id, zone_id, directory, speed_limit_kmh=60.0):
    """监控机到位后创建会话；不执行飞行或调用飞行API。"""
    from Model.yolov8.predict import YoloPredictor
    from UAV.runtime.airsim_client import AirSimClient
    from UAV.runtime.airsim_state import WorldFrame, editor_log, vehicle_name
    from UAV.runtime.flight_control import vehicle_lock
    from UAV.runtime.kill_trees import CameraTreeFilter
    from UAV.utils.plan_search_path import search_scope

    search_scope({'zone_id': zone_id})
    if not math.isfinite(speed_limit_kmh) or speed_limit_kmh <= 0:
        raise ValueError("车辆限速必须大于0")
    predictor = YoloPredictor()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    session = {'client': AirSimClient(timeout=15), 'lock': None, 'filter': None,
               'predictor': predictor, 'directory': directory,
               'speed_limit_kmh': float(speed_limit_kmh), 'tracks': {},
               'next_track_id': 1, 'last_track_time': None,
               'zone_id': zone_id, 'frames': 0, 'last_stamp': None}
    try:
        client = session['client']
        name = vehicle_name(drone_id, client.call('listVehicles'))
        session['name'] = name
        lock = vehicle_lock(name)
        if not lock.acquire(blocking=False):
            raise RuntimeError('监控无人机仍有飞行任务，无法开始测速')
        session['lock'] = lock
        session['log_path'] = editor_log()
        frame = WorldFrame(client)
        _, _, session['origin'] = frame.vehicle(name)
        session['scale'] = frame.scale
        session['filter'] = CameraTreeFilter(client, name)
        session['filter'].start(reclaim_stale=True)
        info = client.call('simGetCameraInfo', 'bottom_center', name, False)
        session['fov'] = float(info['fov'])
        if not math.isfinite(session['fov']) or not 0 < session['fov'] < 180:
            raise RuntimeError('监控相机视场角无效')
    except BaseException:
        close_session(session)
        raise
    return session


def step_session(session):
    """处理一帧，返回车辆速度和新增超速证据。"""
    try:
        return _step_session(session)
    except Exception:
        _reset_tracks(session)
        raise


def close_session(session):
    """结束监测，撤销相机过滤并释放无人机任务锁。"""
    try:
        if session['filter'] is not None:
            session['filter'].close()
    finally:
        session['filter'] = None
        session['client'].close()
        if session['lock'] is not None:
            session['lock'].release()
            session['lock'] = None


def _step_session(session):
    image, exposure = _read_frame(session)
    if image is None:
        _reset_tracks(session)
        return {**exposure, 'frames': session['frames'], 'vehicles': [], 'alerts': []}
    result = session['predictor'].predict(image, save=False)
    detections = result['detections']
    size = result['image_size']
    rays = [(exposure['position_m'], camera_ray(detection['center_xy'],
             (size['width'], size['height']), exposure['camera_fov_deg'], exposure['camera_orientation_ned']))
            for detection in detections]
    positions = _project(session, rays) if rays else []
    localized = []
    for detection, position in zip(detections, positions):
        if position is not None:
            localized.append({**detection, 'position_m': position})
    vehicles, speeding = _update_tracks(session, localized, exposure['simulation_time_s'],
                                                 time_uncertainty_s=exposure['time_uncertainty_s'])
    session['frames'] += 1
    alerts = _save_evidence(session, image, exposure, speeding) if speeding else []
    report = {'status': 'running', 'frames': session['frames'], 'vehicles': vehicles, 'alerts': alerts,
              'unlocated_count': len(detections) - len(localized), **exposure}
    _write_json(session['directory'] / 'latest.json', report)
    return report


def _reset_tracks(session):
        # 不复用旧编号，避免网页把复位后的新轨迹当作之前的车辆。
        session['tracks'].clear()
        session['last_track_time'] = None


def _track_velocity(history):
        if len(history) < 2:
            return (0.0, 0.0), math.inf
        times = [sample[0] - history[-1][0] for sample in history]
        mean_t = sum(times) / len(times)
        means = [sum(sample[axis] for sample in history) / len(history) for axis in (1, 2)]
        denominator = sum((t - mean_t) ** 2 for t in times)
        if denominator < 1e-8:
            return (0.0, 0.0), math.inf
        velocity = tuple(sum((t - mean_t) * (sample[axis] - means[axis - 1])
                             for t, sample in zip(times, history)) / denominator for axis in (1, 2))
        residual = math.sqrt(sum(sum((sample[axis + 1] - means[axis] - velocity[axis] * (t - mean_t)) ** 2
                                    for axis in (0, 1)) for t, sample in zip(times, history)) / len(history))
        return velocity, residual


def _update_tracks(session, detections, timestamp_s, *, time_uncertainty_s=0.0):
        """detections需包含position_m；只对连续、无歧义的观测生成超速事件。"""
        if not math.isfinite(timestamp_s) or timestamp_s < 0:
            raise ValueError("拍摄时间无效")
        if not math.isfinite(time_uncertainty_s) or not 0 <= time_uncertainty_s <= 0.15:
            raise ValueError("拍摄时间误差过大")
        if session['last_track_time'] is not None and timestamp_s <= session['last_track_time']:
            _reset_tracks(session)
        session['last_track_time'] = timestamp_s
        session['tracks'] = {key: track for key, track in session['tracks'].items()
                       if timestamp_s - track['history'][-1][0] <= TRACK_MAX_GAP_S}
        positions = []
        for detection in detections:
            point = detection["position_m"]
            if len(point) != 3 or any(not math.isfinite(v) for v in point):
                raise ValueError("车辆世界坐标无效")
            positions.append(point)

        # 互为最近邻且不存在相近候选时才沿用ID；交会歧义宁可重新积累轨迹。
        distances = {}
        for key, track in session['tracks'].items():
            history = track['history']
            dt = timestamp_s - history[-1][0]
            velocity, _ = _track_velocity(history)
            expected = [history[-1][axis + 1] + velocity[axis] * dt for axis in (0, 1)]
            gate = min(65.0, 2.0 + (55.0 if len(history) < 2 else 15.0) * dt)
            for index, position in enumerate(positions):
                distance = math.dist(expected, position[:2])
                if distance <= gate and abs(position[2] - track['z']) <= 1.5:
                    distances[key, index] = distance
        matches = {}
        for key in session['tracks']:
            candidates = sorted((distance, index) for (other, index), distance in distances.items() if other == key)
            if not candidates or len(candidates) > 1 and candidates[1][0] - candidates[0][0] < 1.5:
                continue
            distance, index = candidates[0]
            rivals = sorted((value, other) for (other, target), value in distances.items() if target == index)
            if rivals[0][1] != key or len(rivals) > 1 and rivals[1][0] - distance < 1.5:
                continue
            matches[index] = key

        measurements, alerts = [], []
        for index, (detection, position) in enumerate(zip(detections, positions)):
            key = matches.get(index)
            if key is None:
                key = session['next_track_id']
                session['next_track_id'] += 1
                session['tracks'][key] = {'history': deque(), 'z': position[2], 'over_since': None, 'alerted': False}
            track = session['tracks'][key]
            history = track['history']
            history.append((timestamp_s, *position[:2], time_uncertainty_s))
            # 正常保留最近3秒；处理较慢时仍保留测速所需的最近3次观测。
            while len(history) > 3 and timestamp_s - history[0][0] > TRACK_WINDOW_S:
                history.popleft()
            track['z'] = position[2]
            velocity, residual = _track_velocity(history)
            span = timestamp_s - history[0][0]
            speed = math.hypot(*velocity)
            ready = len(history) >= 3 and span >= 1.0 and residual <= 0.75 and speed <= 55
            # 位置抖动和曝光时间区间都进入速度误差；以速度下界持续超限才告警。
            error = (3.6 * (max(0.4, 2 * residual) + 2 * speed * max(p[3] for p in history)) / span
                     if ready else None)
            kmh = speed * 3.6 if ready else None
            overspeed = ready and kmh - error > session['speed_limit_kmh']
            if overspeed:
                if track['over_since'] is None:
                    track['over_since'] = timestamp_s
            else:
                track['over_since'] = None
            measurement = {
                **detection, 'track_id': key, 'speed_kmh': round(kmh, 1) if ready else None,
                'speed_error_kmh': round(error, 1) if ready else None,
                'speed_limit_kmh': session['speed_limit_kmh'],
                'overspeed': bool(overspeed and timestamp_s - track['over_since'] >= 0.75),
                'trajectory': [[round(p[0], 6), round(p[1], 3), round(p[2], 3)] for p in history],
            }
            measurements.append(measurement)
            if measurement['overspeed'] and not track['alerted']:
                track['alerted'] = True
                alerts.append(measurement)
        observed = set(matches.values())
        for key, track in session['tracks'].items():
            if key not in observed and track['history'][-1][0] < timestamp_s:
                track['over_since'] = None
        return measurements, alerts


def _read_frame(session):
    import numpy as np
    from UAV.runtime.airsim_state import vector

    motion = session['client'].call('simGetGroundTruthKinematics', session['name'])
    if math.hypot(*vector(motion['linear_velocity'])) * session['scale'] > 0.5:
        return (None, {'status': 'stabilizing'})
    before = _clock(session)
    responses = session['client'].call('simGetImages', [
        {'camera_name': 'bottom_center', 'image_type': 0, 'pixels_as_float': False, 'compress': False}
    ], session['name'], False)
    after = _clock(session)
    if after['simulation_time_s'] <= before['simulation_time_s']:
        return (None, {'status': 'paused'})
    if len(responses) != 1:
        raise RuntimeError('监控相机未返回图片')
    response = responses[0]
    stamp = int(response.get('time_stamp') or 0)
    if stamp <= 0:
        raise RuntimeError('监控图片缺少曝光时间戳')
    timestamp, uncertainty = exposure_time(before, after, stamp)
    if uncertainty > 0.15:
        return (None, {'status': 'timing_unstable'})
    if session['last_stamp'] is not None and stamp <= session['last_stamp']:
        session['last_stamp'] = stamp
        return (None, {'status': 'paused'})
    session['last_stamp'] = stamp
    raw = response.get('image_data_uint8')
    if isinstance(raw, str):
        raw = raw.encode('utf-8', 'surrogateescape')
    width, height = response.get('width', 0), response.get('height', 0)
    if not raw or width <= 0 or height <= 0 or len(raw) != width * height * 3:
        raise RuntimeError('监控相机返回空图片')
    # AirSim未压缩的Scene图像为BGR，避免4K PNG编码/解码阻塞下一帧采样。
    image = np.frombuffer(bytes(raw), dtype=np.uint8).reshape(height, width, 3)
    local = vector(response['camera_position'])
    position = [session['origin'][0] + local[0] * session['scale'],
                session['origin'][1] + local[1] * session['scale'],
                session['origin'][2] - local[2] * session['scale']]
    return image, {
        'status': 'running', 'position_m': position,
        'camera_orientation_ned': response['camera_orientation'], 'camera_fov_deg': session['fov'],
        'capture_time_ns': str(stamp), 'simulation_time_s': timestamp, 'time_uncertainty_s': uncertainty,
    }


def _clock(session):
    first = _clock_sample(session)
    result = geometry_request(session['client'], 'clock', log_path=session['log_path'])
    second = _clock_sample(session)
    value = result['simulation_time_s']
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise RuntimeError('UE仿真时间无效')
    wall = int(result['wall_time_ns'])
    elapsed = second[0] - first[0]
    delta = second[1] - first[1]
    if elapsed > 0 and delta > 0 and (first[0] <= wall <= second[0]):
        fraction = (wall - first[0]) / elapsed
        result['capture_clock_ns'] = str(round(first[1] + fraction * delta))
        result['clock_error_ns'] = (max(first[2], second[2]) / 2 + 16000000) * delta / elapsed
    return result


def _clock_sample(session):
    wall = time.time_ns()
    start = time.perf_counter_ns()
    state = session['client'].call('getMultirotorState', session['name'])
    elapsed = time.perf_counter_ns() - start
    timestamp = int(state.get('timestamp') or 0)
    if timestamp <= 0:
        raise RuntimeError('AirSim状态时间戳无效')
    return (wall + elapsed // 2, timestamp, elapsed)


def _project(session, rays):
    result = geometry_request(session['client'], 'project', rays=rays, log_path=session['log_path'])
    if len(result['points']) != len(rays):
        raise RuntimeError('道路定位结果数量不一致')
    return result['points']


def exposure_time(before, after, stamp):
    start, end = (before['simulation_time_s'], after['simulation_time_s'])
    clock_key = 'capture_clock_ns' if 'capture_clock_ns' in before and 'capture_clock_ns' in after else 'wall_time_ns'
    wall_start = int(before.get(clock_key, 0))
    wall_end = int(after.get(clock_key, 0))
    deltas = [before.get('frame_delta_s', 0), after.get('frame_delta_s', 0)]
    # 图像时间戳先与AirSim时钟对齐，再换算成UE游戏时间。
    if (wall_start <= stamp <= wall_end and wall_end > wall_start and end - start <= 2.0
            and all(math.isfinite(v) and v > 0 for v in deltas)
            and before.get('dilation') == after.get('dilation')):
        fraction = (stamp - wall_start) / (wall_end - wall_start)
        sync_error = max(before.get('clock_error_ns', 0), after.get('clock_error_ns', 0))
        uncertainty = max(deltas) + sync_error / (wall_end - wall_start) * (end - start)
        return (start + fraction * (end - start), uncertainty)
    return ((start + end) / 2, (end - start) / 2)


def camera_ray(center, size, fov, orientation):
    width, height = size
    if not 0 < fov < 180 or min(width, height) <= 0:
        raise ValueError('相机尺寸或视场角无效')
    q = [float(orientation[key]) for key in ('w_val', 'x_val', 'y_val', 'z_val')]
    if not all((math.isfinite(v) for v in (*q, *center))) or math.hypot(*q) < 1e-08:
        raise ValueError('曝光相机姿态或目标中心无效')
    norm = math.hypot(*q)
    w, x, y, z = (v / norm for v in q)
    focal = width / (2 * math.tan(math.radians(fov) / 2))
    vx, vy, vz = (1.0, (center[0] - width / 2) / focal, (center[1] - height / 2) / focal)
    tx, ty, tz = (2 * (y * vz - z * vy), 2 * (z * vx - x * vz), 2 * (x * vy - y * vx))
    direction = (vx + w * tx + y * tz - z * ty, vy + w * ty + z * tx - x * tz, -(vz + w * tz + x * ty - y * tx))
    length = math.hypot(*direction)
    if direction[2] / length > -0.5:
        raise ValueError('监控相机未朝向下方')
    return [v / length for v in direction]


def geometry_request(client, operation, *, rays=(), log_path=None):
    from UAV.runtime.airsim_state import _run_ue_json

    source = Path(__file__).resolve()
    token = 'WEB_TRAFFIC_GEOMETRY_' + uuid.uuid4().hex + ':'
    request = {'operation': operation, 'rays': list(rays)}
    script = '\n'.join([
        'import importlib.util,sys,unreal,json',
        "name='city_traffic_pipeline'",
        'module=sys.modules.get(name)',
        'stamp=' + repr(source.stat().st_mtime_ns),
        "if module is None or getattr(module,'_stamp',None)!=stamp:",
        '    spec=importlib.util.spec_from_file_location(name,' + repr(str(source)) + ')',
        '    module=importlib.util.module_from_spec(spec)',
        '    spec.loader.exec_module(module)',
        '    module._stamp=stamp; sys.modules[name]=module',
        'try:',
        "    result={'ok':True,'result':module._dispatch(" + repr(request) + ')}',
        'except Exception as error:',
        "    result={'ok':False,'error':str(error)}",
        'unreal.log(' + repr(token) + '+json.dumps(result))',
    ])
    response = _run_ue_json(client, 'exec(' + repr(script) + ')', token,
                           'UE未接受车辆测量请求', 'UE未返回车辆测量结果', log_path=log_path)
    if not response['ok']:
        raise RuntimeError(response['error'])
    return response['result']


def _save_evidence(session, image, exposure, speeding):
    from PIL import Image, ImageDraw, ImageFont

    capture = session['directory'] / 'capture'
    capture.mkdir(exist_ok=True)
    stem = f"{session['frames']:06d}"
    filename = stem + '.jpg'
    source = Image.fromarray(image[..., ::-1])
    source.save(capture / filename, quality=95)
    annotated = source.copy()
    draw = ImageDraw.Draw(annotated)
    try:
        font = ImageFont.truetype('arial.ttf', 18)
    except OSError:
        font = ImageFont.load_default()
    width, height = source.size
    alerts = []
    for target in speeding:
        x1, y1, x2, y2 = target['bbox_xyxy']
        box = [max(0, min(1000, x1 / width * 1000)), max(0, min(1000, y1 / height * 1000)),
               max(0, min(1000, x2 / width * 1000)), max(0, min(1000, y2 / height * 1000))]
        label = f"ID {target['track_id']}: {target['speed_kmh']:.1f} km/h"
        draw.line([tuple(p) for p in target['bbox_xyxyxyxy']] + [tuple(target['bbox_xyxyxyxy'][0])], fill='red', width=3)
        draw.text((max(0, x1), max(0, y1 - 24)), label, fill='red', font=font)
        alerts.append({
            'event_id': f"vehicle_{target['track_id']}", 'track_id': target['track_id'],
            'image': filename, 'source_image': f'capture/{filename}',
            'speed_kmh': target['speed_kmh'], 'speed_limit_kmh': target['speed_limit_kmh'],
            'speed_error_kmh': target['speed_error_kmh'],
            'position_m': target['position_m'], 'trajectory': target['trajectory'],
            'description': f"车辆 {target['track_id']} 车速 {target['speed_kmh']:.1f} km/h，限速 {target['speed_limit_kmh']:g} km/h",
            'detections': [{'status': 'speeding', 'label': label, 'bbox': box}],
            **exposure,
        })
    annotated.save(session['directory'] / (stem + '.annotated.jpg'), quality=95)
    _write_json(session['directory'] / (stem + '.json'), {'alerts': alerts})
    return alerts


def _write_json(path, value):
    temporary = path.with_suffix('.json.part')
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def _dispatch(request):
    import unreal

    world = unreal.EditorLevelLibrary.get_game_world()
    if world is None:
        raise RuntimeError('请先启动UE Play仿真')
    if request['operation'] == 'clock':
        import time
        return {'simulation_time_s': unreal.GameplayStatics.get_time_seconds(world),
                'wall_time_ns': str(time.time_ns()),
                'frame_delta_s': unreal.GameplayStatics.get_world_delta_seconds(world),
                'dilation': unreal.GameplayStatics.get_global_time_dilation(world)}
    if request['operation'] != 'project':
        raise ValueError('未知车辆测量操作')
    origin = unreal.GameplayStatics.get_world_origin_location(world)
    ignored = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Pawn)
    points = []
    for position, direction in request['rays']:
        start = unreal.Vector(position[0] * 100 - origin.x, position[1] * 100 - origin.y, position[2] * 100 - origin.z)
        end = unreal.Vector(start.x + direction[0] * 200000, start.y + direction[1] * 200000, start.z + direction[2] * 200000)
        point = None
        for _ in range(24):
            # 沿曝光射线跳过树冠、灯杆和车身，定位实际路面。
            hit = unreal.SystemLibrary.line_trace_single_for_objects(
                world, start, end, [unreal.ObjectTypeQuery.OBJECT_TYPE_QUERY1],
                True, ignored, unreal.DrawDebugTrace.NONE, True)
            h = hit.to_tuple() if hit is not None else None
            if not h or not h[0]:
                break
            component = h[10]
            mesh = component.get_editor_property('static_mesh') if isinstance(component, unreal.StaticMeshComponent) else None
            description = str(mesh)
            road = 'Roads_Road_' in description or 'Roads_Marking_' in description
            if not road and any((name in description for name in ('RN_R', 'RN_J', 'SM_RoadBake', 'SM_CityRoadSection'))):
                material, _ = component.get_material_from_collision_face_index(h[14])
                road = any((word in str(material).lower() for word in ('road', 'asphalt', 'marking')))
            if road and h[7].z >= 0.65:
                point = [(h[5].x + origin.x) / 100, (h[5].y + origin.y) / 100, (h[5].z + origin.z) / 100]
                break
            if not any((word in description.lower() for word in ('tree', 'vegetation', 'lamp', 'pole', '/static/car/', '/vehicles/'))):
                break
            start = unreal.Vector(h[5].x + direction[0] * 5, h[5].y + direction[1] * 5, h[5].z + direction[2] * 5)
        points.append(point)
    return {'points': points}
