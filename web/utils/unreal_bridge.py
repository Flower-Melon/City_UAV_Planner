"""由现有 Python bridge 加载到 UE；读取 API 目标并提供独立的跟随镜头。"""
import json
import math
import os
from pathlib import Path
import sys
import time
import traceback
import uuid
import unreal

_targets = {}  # 供公共脚本加载器检查；观察器不拥有异常目标。
_tick_handle = None
_world = None
_session = None
_camera = None
_capture = None
_target_id = None
_lease = 0
_camera_error = None


def valid(obj):
    try:
        return obj is not None and unreal.SystemLibrary.is_valid(obj)
    except Exception:
        return False


def targets():
    result = {}
    for module_name, registry, kind in (("city_abnormal_runtime", "_vehicles", "vehicle"),
                                         ("city_abnormal_people_api", "_people", None)):
        module = sys.modules.get(module_name)
        for name, item in list(getattr(module, registry, {}).items()):
            if valid(item.actor) and item.actor.get_world() == _world:
                result[name] = (item, kind or item.kind)
    return result


def stop_camera():
    global _camera, _capture, _target_id
    if valid(_camera):
        _camera.destroy_actor()
    _camera = _capture = _target_id = None


def shutdown():
    """脚本重新加载时释放它自己创建的取景对象。"""
    stop_camera()


def current_world():
    global _world, _session
    world = unreal.EditorLevelLibrary.get_game_world()
    if not valid(world):
        stop_camera()
        _world = None
        return None
    if not valid(_world) or world != _world:
        stop_camera()
        _world, _session = world, uuid.uuid4().hex
    return world


def follow():
    if not valid(_camera):
        return
    entry = targets().get(_target_id)
    if not entry:
        stop_camera()
        return
    item, kind = entry
    actor = item.actor
    p = actor.get_actor_location()
    yaw = math.radians(actor.get_actor_rotation().yaw)
    back, height = (900, 470) if kind == "vehicle" else (450, 240)
    focus = unreal.Vector(p.x, p.y, p.z + (50 if kind == "vehicle" else 0))
    location = unreal.Vector(p.x - math.cos(yaw) * back, p.y - math.sin(yaw) * back, p.z + height)
    hit = unreal.SystemLibrary.line_trace_single_for_objects(
        _world, focus, location, [unreal.ObjectTypeQuery.OBJECT_TYPE_QUERY1],
        False, [actor, _camera], unreal.DrawDebugTrace.NONE, True)
    h = hit.to_tuple() if hit else None
    if h and h[0]:
        point = h[5]
        location = unreal.Vector(point.x + h[7].x * 35, point.y + h[7].y * 35, point.z + h[7].z * 35)
    rotation = unreal.MathLibrary.find_look_at_rotation(location, focus)
    _camera.set_actor_location_and_rotation(location, rotation, False, True)


def observe(name):
    global _camera, _capture, _target_id, _lease, _tick_handle, _camera_error
    _lease = time.monotonic() + 5
    if name not in targets():
        stop_camera()
        return
    if not valid(_camera):
        gameplay = unreal.get_default_object(unreal.GameplayStatics)
        transform = unreal.Transform()
        _camera = gameplay.call_method("BeginDeferredActorSpawnFromClass", (
            _world, unreal.SceneCapture2D, transform,
            unreal.SpawnActorCollisionHandlingMethod.ALWAYS_SPAWN, None))
        gameplay.call_method("FinishSpawningActor", (_camera, transform))
        _capture = _camera.get_component_by_class(unreal.SceneCaptureComponent2D)
        texture = unreal.RenderingLibrary.create_render_target2d(
            _world, 800, 450, unreal.TextureRenderTargetFormat.RTF_RGBA8)
        for key, value in (("texture_target", texture), ("capture_source", unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR),
                           ("capture_every_frame", False), ("capture_on_movement", False),
                           ("always_persist_rendering_state", True), ("fov_angle", 70.0)):
            _capture.set_editor_property(key, value)
    _target_id = name
    follow()
    _camera_error = None
    if _tick_handle is None:
        _tick_handle = unreal.register_slate_post_tick_callback(tick)


def tick(delta):
    global _camera_error, _tick_handle
    try:
        if not valid(_world) or time.monotonic() > _lease:
            stop_camera()
            unreal.unregister_slate_post_tick_callback(_tick_handle)
            _tick_handle = None
            return
        follow()
    except Exception as error:
        _camera_error = str(error)
        stop_camera()


def execute(data):
    global _camera_error
    if current_world() is None:
        if data["operation"] in ("frame", "remove_info"):
            raise RuntimeError("请启动 AirSim Play 仿真")
        return {"connected": False, "targets": [], "session": None}
    if data["operation"] == "remove_info":
        name = data.get("target_id")
        entry = targets().get(name)
        if entry is None:
            return {"object_ids": []}
        item, kind = entry
        object_ids = []
        if kind == "armed_person" and valid(item.weapon):
            object_ids.append(item.weapon.get_name())
        object_ids.append(item.actor.get_name())
        if _target_id == name:
            stop_camera()
        return {"object_ids": object_ids}
    if data["operation"] == "frame":
        observe(data.get("target_id"))
        if not valid(_capture):
            raise RuntimeError("当前目标已不存在")
        _capture.capture_scene()
        filename = uuid.uuid4().hex + ".png"
        unreal.RenderingLibrary.export_render_target(
            _world, _capture.get_editor_property("texture_target"), data["directory"], filename)
        return {"filename": filename, "session": _session}
    if data.get("watch"):
        try:
            observe(data.get("target_id"))
        except Exception as error:
            _camera_error = str(error)
            stop_camera()
    # 普通状态查询不撤销其他网页的镜头；没有取景心跳后由 lease 自动回收。
    origin = unreal.GameplayStatics.get_world_origin_location(_world)
    records = []
    for name, (item, kind) in sorted(targets().items()):
        p = item.actor.get_actor_location()
        row = {"id": name, "kind": kind,
               "position_m": [(p.x + origin.x) / 100, (p.y + origin.y) / 100, (p.z + origin.z) / 100],
               "yaw_rad": math.radians(item.actor.get_actor_rotation().yaw), "error": item.error}
        if kind == "vehicle":
            row.update(behavior=item.motion.behavior, active_behavior=item.motion.active_behavior,
                       speed_m_s=item.motion.speed)
        else:
            row["action"] = item.action
        records.append(row)
    return {"connected": True, "targets": records, "session": _session, "camera_error": _camera_error}


def dispatch(source, result):
    try:
        response = {"ok": True, "result": execute(json.loads(Path(source).read_text(encoding="utf-8")))}
    except Exception as error:
        response = {"ok": False, "error": str(error)}
        unreal.log_warning(traceback.format_exc())
    temporary = Path(result).with_suffix(".tmp")
    temporary.write_text(json.dumps(response, allow_nan=False), encoding="utf-8")
    os.replace(str(temporary), result)
