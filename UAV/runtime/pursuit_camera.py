"""UE 内的追踪镜头，按同一帧的目标包围盒生成屏幕框。"""

import json
import math
from pathlib import Path
from runpy import run_path
import sys
import time
import unreal


WIDTH, HEIGHT, FOV = 640, 360, 44.0
_targets = {}  # 公共 UE 请求加载器使用，摄像机不拥有车辆。
_drones = {}
_tick_handle = None
_camera = _capture = _session = None
_lease = 0.0


def _valid(actor):
    try:
        return actor is not None and unreal.SystemLibrary.is_valid(actor)
    except (RuntimeError, TypeError):
        return False


def shutdown():
    global _camera, _capture, _session, _tick_handle
    if _valid(_camera):
        _camera.destroy_actor()
    if _tick_handle is not None:
        unreal.unregister_slate_post_tick_callback(_tick_handle)
    _camera = _capture = _session = _tick_handle = None
    _drones.clear()


def _check_lease(_delta):
    if time.monotonic() > _lease:
        shutdown()


def _camera_for(world, session):
    global _camera, _capture, _session, _tick_handle
    if _session != session or not _valid(_camera):
        shutdown()
        gameplay = unreal.get_default_object(unreal.GameplayStatics)
        transform = unreal.Transform()
        _camera = gameplay.call_method('BeginDeferredActorSpawnFromClass', (
            world, unreal.SceneCapture2D, transform, unreal.SpawnActorCollisionHandlingMethod.ALWAYS_SPAWN, None))
        gameplay.call_method('FinishSpawningActor', (_camera, transform))
        _capture = _camera.get_component_by_class(unreal.SceneCaptureComponent2D)
        texture = unreal.RenderingLibrary.create_render_target2d(
            world, WIDTH, HEIGHT, unreal.TextureRenderTargetFormat.RTF_RGBA8)
        for key, value in (('texture_target', texture), ('capture_source', unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR),
                           ('capture_every_frame', False), ('capture_on_movement', False),
                           ('always_persist_rendering_state', True), ('fov_angle', FOV)):
            _capture.set_editor_property(key, value)
        tree_filter = run_path(str(Path(__file__).with_name('kill_trees.py')))['hide_tree_components']
        tree_filter(unreal, world, _capture)
        _session = session
        _tick_handle = unreal.register_slate_post_tick_callback(_check_lease)
    return _camera, _capture


def _project(points, position, rotation):
    yaw, pitch = math.radians(rotation.yaw), math.radians(rotation.pitch)
    cy, sy, cp, sp = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)
    focal = 0.5 / math.tan(math.radians(FOV) / 2)
    screen = []
    for point in points:
        x, y, z = point.x - position.x, point.y - position.y, point.z - position.z
        depth = (x * cy + y * sy) * cp + z * sp
        if depth <= 1.0:
            continue
        right, up = -x * sy + y * cy, -(x * cy + y * sy) * sp + z * cp
        screen.append((0.5 + focal * right / depth, 0.5 - focal * up / depth * WIDTH / HEIGHT))
    if not screen:
        return None
    xs, ys = zip(*screen)
    box = [max(0.0, min(xs)), max(0.0, min(ys)), min(1.0, max(xs)), min(1.0, max(ys))]
    return box if box[2] > box[0] and box[3] > box[1] else None


def _view(world, pose, focus, center, corners, ignored, check_visibility):
    rotation = unreal.MathLibrary.find_look_at_rotation(pose, focus)
    box = _project(corners, pose, rotation) if check_visibility else None
    if box:
        hit = unreal.SystemLibrary.line_trace_single_for_objects(
            world, pose, center, [unreal.ObjectTypeQuery.OBJECT_TYPE_QUERY1,
                                  unreal.ObjectTypeQuery.OBJECT_TYPE_QUERY2,
                                  unreal.ObjectTypeQuery.OBJECT_TYPE_QUERY3],
            False, ignored, unreal.DrawDebugTrace.NONE, True)
        if hit and hit.to_tuple()[0]:
            box = None
    return pose, rotation, box


def _execute(data):
    global _lease
    if data['operation'] == 'close':
        if _session == data['session']:
            shutdown()
        return None

    world = unreal.EditorLevelLibrary.get_game_world()
    runtime = sys.modules.get('city_abnormal_runtime')
    vehicle = getattr(runtime, '_vehicles', {}).get(data['vehicle_id'])
    if vehicle is None or not _valid(vehicle.actor) or vehicle.actor.get_world() != world:
        raise RuntimeError('仿真中找不到正在追踪的异常车辆')
    if vehicle.error:
        raise RuntimeError('车辆仿真已停止：' + vehicle.error)
    actor = vehicle.actor
    location, heading = actor.get_actor_location(), math.radians(actor.get_actor_rotation().yaw)
    origin = unreal.GameplayStatics.get_world_origin_location(world)
    now = unreal.GameplayStatics.get_time_seconds(world)
    speed = vehicle.motion.speed
    target = {'position_m': [(location.x + origin.x) / 100, (location.y + origin.y) / 100,
                             (location.z + origin.z) / 100],
              'velocity_m_s': [speed * math.cos(heading), speed * math.sin(heading)],
              'intercept_state': getattr(vehicle.motion, 'intercept_state', None),
              'heading_rad': heading, 'last_seen_s': now, 'source': 'ue_global_position'}
    result = {'target': target, 'camera': None, 'visible': False}
    if not data['views']:
        return result

    component = actor.get_component_by_class(unreal.StaticMeshComponent)
    lower, upper = component.get_local_bounds()
    transform = component.get_world_transform()
    corners = [unreal.MathLibrary.transform_location(transform, unreal.Vector(x, y, z))
               for x in (lower.x, upper.x) for y in (lower.y, upper.y) for z in (lower.z, upper.z)]
    center = unreal.MathLibrary.transform_location(transform, unreal.Vector(
        (lower.x + upper.x) / 2, (lower.y + upper.y) / 2, (lower.z + upper.z) / 2))
    focus = unreal.Vector(center.x + math.cos(heading) * 400, center.y + math.sin(heading) * 400, center.z)
    names = data['views']
    if any(not _valid(_drones.get(name)) or
           _drones[name].get_world() != world for name in names):
        _drones.update({pawn.get_name(): pawn for pawn in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Pawn)
                        if pawn.get_name() in names})
    ignored = [actor] + [_drones[name] for name in names]
    views = {}
    ready = set(data['ready'])
    capture_name = data.get('capture_camera')
    for name in names:
        position = _drones[name].get_actor_location()
        pose = unreal.Vector(position.x, position.y, position.z - 70)
        views[name] = _view(world, pose, focus, center, corners, ignored, name in ready or name == capture_name)

    name = data['camera'] if data['camera'] in views else next(iter(views))
    if (name not in ready or views[name][2] is None) and data['allow_switch']:
        name = next((candidate for candidate in views if candidate in ready and views[candidate][2] is not None), name)
    result.update(camera=name, visible=name in ready and views[name][2] is not None)
    if capture_name is not None:
        pose, rotation, box = views[capture_name]
        _lease = time.monotonic() + 10.0
        camera, capture = _camera_for(world, data['session'])
        camera.set_actor_location_and_rotation(pose, rotation, False, True)
        capture.capture_scene()
        unreal.RenderingLibrary.export_render_target(
            world, capture.get_editor_property('texture_target'), data['directory'], 'pursuit.png')
        result['frame'] = {'camera': capture_name, 'bbox': box, 'visible': box is not None}
    return result


def dispatch(source, result):
    try:
        data = json.loads(Path(source).read_text(encoding='utf-8'))
        response = {'ok': True, 'result': _execute(data)}
    except Exception as error:
        response = {'ok': False, 'error': str(error)}
    target = Path(result)
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(response, allow_nan=False), encoding='utf-8')
    temporary.replace(target)
