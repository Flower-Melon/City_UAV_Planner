"""在指定地点附近的人行道生成持刀人员，同时承载两类人物的生成和动作逻辑。"""

import json
import math
import os
from pathlib import Path
import random
import re
import sys
import traceback
from functools import lru_cache

try:
    import unreal
except ImportError:
    unreal = None

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "Abnormal.armed_person"


def spawn_armed_person(position) -> str:
    """输入 UE 世界 (x, y) 米制坐标，返回唯一 armed_person_ID。

    在 100 米内选择可用临街人行道，自动贴地，并在同一人行道短段闭环巡游。
    站立、走、跑和威胁按原场景概率切换。每次新增目标，失败时抛出异常。
    """
    return _spawn_person(position, "armed_person")


@lru_cache(maxsize=1)
def _sidewalks():
    from shapely.geometry import LineString
    from shapely.strtree import STRtree

    path = Path(__file__).resolve().parents[2] / "Map/roads/road_network.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    lanes = {lane["id"] for lane in data["lanes"] if lane["type"] == "sidewalk"}
    edges = sorted((e for e in data["edges"] if e.get("lane_id") in lanes), key=lambda e: e["id"])
    records = []
    for edge in edges:
        line = LineString(edge["polyline_xy_m"])
        if line.length > 1.6:
            records.append((edge["id"], line))
    if not records:
        raise ValueError("地图中没有可用人行道")
    return records, STRtree([line for _, line in records])


def _patrol_legs(line, along):
    low, high = max(0.8, along - 12), min(line.length - 0.8, along + 12)
    if high - low < 4:
        return []
    legs = []
    for end in (high, low):
        count = max(1, int(math.ceil(abs(end - along))))
        legs.append([list(line.interpolate(along + (end - along) * i / count).coords[0])
                     for i in range(count + 1)])
    return legs


def _loop_patrol(legs):
    result = []
    for leg in legs:
        for xy in [*leg, *reversed(leg[:-1])]:
            if not result or math.hypot(xy[0] - result[-1][0], xy[1] - result[-1][1]) > 0.001:
                result.append(list(xy))
    return result


def _patrol_points(line, along):
    return _loop_patrol(_patrol_legs(line, along))


def _candidates(position, kind):
    from shapely.geometry import Point

    point = Point(position)
    records, index = _sidewalks()
    choices = []
    for i in index.query(point.buffer(100)):
        edge_id, line = records[int(i)]
        if line.distance(point) > 100:
            continue
        center = line.project(point)
        for offset in (0, 1.5, -1.5, 3, -3, 6, -6, 12, -12, 18, -18, 24, -24):
            along = max(0.8, min(line.length - 0.8, center + offset))
            xy = list(line.interpolate(along).coords[0])
            distance = point.distance(Point(xy))
            if distance > 100:
                continue
            a = line.interpolate(max(0, along - 0.1))
            b = line.interpolate(min(line.length, along + 0.1))
            legs = _patrol_legs(line, along) if kind == "armed_person" else []
            route = _loop_patrol(legs)
            if kind == "armed_person" and not route:
                continue
            choices.append((distance, edge_id, along, {"xy": xy, "yaw": math.atan2(b.y - a.y, b.x - a.x),
                                                       "route": route, "patrol_legs": legs}))
    choices.sort(key=lambda entry: entry[:3])
    result, seen = [], set()
    for _, _, _, candidate in choices:
        key = tuple(round(v, 3) for v in candidate["xy"])
        if key not in seen:
            seen.add(key)
            result.append(candidate)
        if len(result) == 48:
            break
    if not result:
        raise ValueError("指定地点 100 米内没有合适的临街人行道")
    return result


def _spawn_person(position, kind):
    from ..vehicle.spawn_vehicle import AirSimClient, position_xy, request

    position = position_xy(position)
    candidates = _candidates(position, kind)
    controller = Path(__file__).resolve()

    def command(client, operation, **parameters):
        return request(client, operation, _controller=controller, _module_name="city_abnormal_people_api",
                       _registry="_people", **parameters)

    with AirSimClient(timeout=45) as client:
        request(client, "api_setup")
        prepared = command(client, "prepare", kind=kind, candidates=candidates)
        name, weapon, finished = prepared["person_id"], None, False
        try:
            if kind == "armed_person":
                expected = name + "_weapon"
                weapon = client.call("simSpawnObject", expected, "SM_Knife", prepared["weapon_pose"],
                                     {"x_val": 1.0, "y_val": 1.0, "z_val": 1.0}, False, False, timeout=45)
                if weapon != expected:
                    raise RuntimeError("刀具对象名称冲突")
            command(client, "finish", person_id=name, weapon_name=weapon)
            finished = True
            return name
        finally:
            if not finished:
                try:
                    command(client, "release", person_id=name)
                finally:
                    if weapon:
                        client.call("simDestroyObject", weapon)


# 下方代码由 UE 直接加载；普通 Python 调用时不会创建任何场景对象。
_people = {}
_pending = {}
_reserved_names = set()
_tick_handle = None
_world_key = None
_game_world = None
_last_time = 0.0
_accumulator = 0.0
_surface_cache = {}
_SKELETON = "/Game/Carla/Static/Pedestrian/Male/SK_Male.SK_Male"
_ANIMATION = "/Game/CityNPC/AbnormalPeople/ABP_CityAbnormalPeople.ABP_CityAbnormalPeople_C"
_COLLAPSE = "/Game/CityNPC/AbnormalPeople/Animations/AS_PatientCollapse.AS_PatientCollapse"
_CAPSULE_HALF_HEIGHT_M = 0.88
# 脚本禁用了 CharacterMovement，必须自己保留正常角色贴地时的胶囊间隙。
# 胶囊恰好接触 HISM 人行道时，UE sweep 会报告初始穿透，阻止所有横向移动。
_GROUND_CLEARANCE_M = 0.025


def _base():
    return sys.modules["city_abnormal_runtime"]


def _valid(obj):
    return _base().valid(obj)


def _context():
    return _base()._context


def _surface(xy):
    key = (round(xy[0], 2), round(xy[1], 2))
    if key in _surface_cache:
        return _surface_cache[key]
    context = _context()
    start, end = context.vector(xy[0], xy[1], 1000), context.vector(xy[0], xy[1], -1000)
    for _ in range(24):
        hit = unreal.SystemLibrary.line_trace_single_for_objects(
            context.world, start, end, [unreal.ObjectTypeQuery.OBJECT_TYPE_QUERY1], True,
            [], unreal.DrawDebugTrace.NONE, True)
        h = hit.to_tuple() if hit is not None else None
        if not h or not h[0]:
            return None
        component = h[10]
        mesh = component.get_editor_property("static_mesh") if isinstance(component, unreal.StaticMeshComponent) else None
        description = str(mesh).lower()
        material = None
        if component is not None and h[14] >= 0:
            material, _ = component.get_material_from_collision_face_index(h[14])
        pavement = any(word in description + " " + str(material).lower()
                       for word in ("sidewalk", "side_walk", "pavement", "footpath", "walkway"))
        if pavement:
            if h[7].z < 0.75:
                return None
            z = h[5].z / 100.0 + context.origin()[2]
            if len(_surface_cache) > 16000:
                _surface_cache.clear()
            _surface_cache[key] = z
            return z
        if any(word in description for word in ("roads_road_", "roads_marking_")):
            return None
        start = unreal.Vector(start.x, start.y, h[5].z - 2.0)
    return None


def _support(xy, yaw, patient=False):
    z = _surface(xy)
    if z is None:
        return None
    c, s = math.cos(yaw), math.sin(yaw)
    xs, ys, tolerance = ((-1.6, 0, 1.6), (-0.9, 0, 0.9), 0.06) if patient else ((-0.3, 0.3), (-0.3, 0.3), 0.15)
    for x in xs:
        for y in ys:
            h = _surface((xy[0] + x * c - y * s, xy[1] + x * s + y * c))
            if h is None or abs(h - z) > tolerance:
                return None
    return z


def _clear(xy, z, ignored=(), end_xy=None, patient=False, yaw=0.0):
    context = _context()
    end_xy = xy if end_xy is None else end_xy
    objects = [unreal.ObjectTypeQuery.OBJECT_TYPE_QUERY1, unreal.ObjectTypeQuery.OBJECT_TYPE_QUERY2,
               unreal.ObjectTypeQuery.OBJECT_TYPE_QUERY3]
    hit = unreal.SystemLibrary.capsule_trace_single_for_objects(
        context.world, context.vector(xy[0], xy[1], z + 0.9),
        context.vector(end_xy[0], end_xy[1], z + 0.9), 35.0, 85.0,
        objects, False, list(ignored), unreal.DrawDebugTrace.NONE, True)
    h = hit.to_tuple() if hit is not None else None
    if h and h[0]:
        return False
    if patient:
        center = context.vector(xy[0], xy[1], z + 0.6)
        hit = unreal.SystemLibrary.box_trace_single_for_objects(
            context.world, center, center, unreal.Vector(160, 90, 55),
            unreal.Rotator(pitch=0, yaw=math.degrees(yaw), roll=0), objects, False,
            list(ignored), unreal.DrawDebugTrace.NONE, True)
        h = hit.to_tuple() if hit is not None else None
        if h and h[0]:
            return False
    return True


def _place(actor, xy, z, yaw, sweep=False):
    actor.set_actor_location_and_rotation(
        _context().vector(xy[0], xy[1], z + _CAPSULE_HALF_HEIGHT_M + _GROUND_CLEARANCE_M),
                                         unreal.Rotator(pitch=0, yaw=math.degrees(yaw), roll=0), sweep, True)


def _safe_patrol_route(candidate, ground):
    """从出生点分别向两侧探测，裁去障碍之后的路线，保留至少4米可通行路段。"""
    safe, total = [], 0.0
    for leg in candidate.get("patrol_legs", [candidate["route"]]):
        usable, z = [candidate["xy"]], ground
        for xy in leg[1:]:
            previous = usable[-1]
            length = math.hypot(xy[0] - previous[0], xy[1] - previous[1])
            if length < 0.001:
                continue
            yaw = math.atan2(xy[1] - previous[1], xy[0] - previous[0])
            next_z = _support(xy, yaw)
            if (next_z is None or abs(next_z - z) > 0.25
                    or not _clear(previous, z, end_xy=xy)):
                break
            usable.append(xy)
            total += length
            z = next_z
        safe.append(usable)
    return _loop_patrol(safe) if total >= 4.0 else None


class Person:
    def __init__(self, actor, kind, candidate, z, mesh_asset, animation_class, collapse_length):
        self.actor, self.kind = actor, kind
        self.xy, self.yaw, self.z = tuple(candidate["xy"]), candidate["yaw"], z
        self.route = candidate["route"]
        self.route_index = 1
        self.age, self.phase_remaining = 0.0, 3.0
        self.random = random.Random()
        self.action = 4 if kind == "patient" else 0
        self.collapse_length = collapse_length
        self.weapon = None
        self.error = None
        actor.set_actor_tick_enabled(True)
        capsule = actor.get_editor_property("capsule_component")
        capsule.set_capsule_size(30, 88, True)
        capsule.set_collision_profile_name("Pawn")
        actor.get_editor_property("character_movement").disable_movement()
        self.mesh = actor.get_editor_property("mesh")
        self.mesh.set_skeletal_mesh(mesh_asset, True)
        self.mesh.set_relative_location(unreal.Vector(0, 0, -85.49), False, True)
        self.mesh.set_relative_rotation(unreal.Rotator(pitch=0, yaw=-90, roll=0), False, True)
        self.mesh.set_editor_property("receives_decals", False)
        self.mesh.set_collision_enabled(unreal.CollisionEnabled.NO_COLLISION)
        self.mesh.set_editor_property("visibility_based_anim_tick_option",
                                      unreal.VisibilityBasedAnimTickOption.ALWAYS_TICK_POSE_AND_REFRESH_BONES)
        self.mesh.set_editor_property("enable_update_rate_optimizations", False)
        self.mesh.set_forced_lod(1)
        self.mesh.set_anim_instance_class(animation_class)
        self.animation = self.mesh.get_anim_instance()
        if self.animation is None:
            raise RuntimeError("异常人物动画蓝图未能初始化")
        actor.set_editor_property("tags", [unreal.Name(actor.get_name()), unreal.Name("CityScriptAbnormalPerson")])
        _place(actor, self.xy, self.z, self.yaw)
        self._animate(0.0)

    def _animate(self, speed):
        self.animation.set_editor_property("action_index", self.action)
        self.animation.set_editor_property("ground_speed", speed * 100.0)

    def step(self, dt):
        if self.error:
            return
        self.age += dt
        if self.kind == "patient":
            self.action = 4 if self.age < 6 else 5 if self.age < 6 + self.collapse_length else 6
            self._animate(0.0)
            return
        self.phase_remaining -= dt
        if self.phase_remaining <= 0:
            if self.action in (0, 3):
                self.action = 2 if self.random.random() < 0.35 else 1
                self.phase_remaining = self.random.uniform(6, 10)
            else:
                self.action = 3 if self.random.random() < 0.6 else 0
                self.phase_remaining = self.random.uniform(2.5, 4.5)
        if self.action not in (1, 2):
            self._animate(0.0)
            return
        goal = self.route[self.route_index]
        dx, dy = goal[0] - self.xy[0], goal[1] - self.xy[1]
        distance = math.hypot(dx, dy)
        if distance < 0.3:
            self.route_index = (self.route_index + 1) % len(self.route)
            self._animate(0.0)
            return
        speed = 2.7 if self.action == 2 else 1.4
        travel = min(distance, speed * dt)
        xy = (self.xy[0] + dx / distance * travel, self.xy[1] + dy / distance * travel)
        target_yaw = math.atan2(dy, dx)
        change = math.atan2(math.sin(target_yaw - self.yaw), math.cos(target_yaw - self.yaw))
        yaw = self.yaw + max(-math.radians(260) * dt, min(math.radians(260) * dt, change))
        z = _support(xy, yaw)
        if z is None or abs(z - self.z) > 0.25 or not _clear(self.xy, self.z, [self.actor], end_xy=xy):
            self.action, self.phase_remaining = 3, self.random.uniform(2.5, 4.5)
            self._animate(0.0)
            return
        _place(self.actor, xy, z, yaw, sweep=True)
        actual = self.actor.get_actor_location()
        origin = _context().origin()
        actual_xy = (actual.x / 100 + origin[0], actual.y / 100 + origin[1])
        actual_speed = math.hypot(actual_xy[0] - self.xy[0], actual_xy[1] - self.xy[1]) / dt
        if math.hypot(actual_xy[0] - xy[0], actual_xy[1] - xy[1]) > 0.02:
            self.action, self.phase_remaining = 3, self.random.uniform(2.5, 4.5)
        self.xy, self.z, self.yaw = (
            actual_xy,
            actual.z / 100 + origin[2] - _CAPSULE_HALF_HEIGHT_M - _GROUND_CLEARANCE_M,
            yaw,
        )
        self._animate(actual_speed)

    def update_weapon(self):
        if self.weapon is None or not _valid(self.weapon):
            return
        names = ("crl_handIndex__R", "crl_handPinky__R", "crl_handMiddle__R", "crl_handMiddle01__R")
        if not all(self.mesh.does_socket_exist(name) for name in names):
            raise RuntimeError("人物骨骼缺少刀具握持所需的手指骨骼")
        index, pinky, middle, joint = [self.mesh.get_socket_location(name) for name in names]

        def unit(x, y, z):
            length = math.sqrt(x * x + y * y + z * z)
            if length < 1e-6:
                raise RuntimeError("手部骨骼姿态无效，无法放置刀具")
            return unreal.Vector(x / length, y / length, z / length)

        blade = unit(index.x - pinky.x, index.y - pinky.y, index.z - pinky.z)
        along = unit(joint.x - middle.x, joint.y - middle.y, joint.z - middle.z)
        normal = unit(blade.y * along.z - blade.z * along.y,
                      blade.z * along.x - blade.x * along.z, blade.x * along.y - blade.y * along.x)
        center = unreal.Vector((index.x + pinky.x) * 0.5 + (joint.x - middle.x) * 0.35 - normal.x * 2.2,
                               (index.y + pinky.y) * 0.5 + (joint.y - middle.y) * 0.35 - normal.y * 2.2,
                               (index.z + pinky.z) * 0.5 + (joint.z - middle.z) * 0.35 - normal.z * 2.2)
        rotation = unreal.MathLibrary.make_rot_from_xz(blade, normal)
        self.weapon.set_actor_location_and_rotation(center, rotation, False, True)


def _new_person(world, name, candidate, kind, z, mesh, animation, collapse):
    mode = unreal.GameplayStatics.get_game_mode(world)
    if mode is None:
        raise RuntimeError("当前仿真没有可用于创建人物的 GameMode")
    previous = mode.get_editor_property("default_pawn_class")
    location = _context().vector(
        candidate["xy"][0], candidate["xy"][1], z + _CAPSULE_HALF_HEIGHT_M + _GROUND_CLEARANCE_M
    )
    transform = unreal.Transform(
        location, unreal.Rotator(pitch=0, yaw=math.degrees(candidate["yaw"]), roll=0), unreal.Vector(1, 1, 1))
    try:
        mode.set_editor_property("default_pawn_class", unreal.Character)
        actor = mode.spawn_default_pawn_at_transform(None, transform)
    finally:
        mode.set_editor_property("default_pawn_class", previous)
    if actor is None:
        raise RuntimeError("人物创建失败，出生点可能已被占用")
    try:
        if not actor.rename(name):
            raise RuntimeError("人物 ID 冲突：" + name)
        return Person(actor, kind, candidate, z, mesh, animation, collapse)
    except Exception:
        if _valid(actor):
            actor.destroy_actor()
        raise


def _prepare(data):
    world = _context().world
    kind = data["kind"]
    if kind not in ("armed_person", "patient"):
        raise ValueError("不支持的人物类型")
    mesh, animation = unreal.load_asset(_SKELETON), unreal.load_class(None, _ANIMATION)
    collapse = unreal.load_asset(_COLLAPSE)
    if mesh is None or animation is None or collapse is None:
        raise RuntimeError("当前工程缺少异常人物模型或动画素材")
    if kind == "armed_person" and unreal.load_asset("/Game/CityNPC/AbnormalPeople/Props/SM_Knife.SM_Knife") is None:
        raise RuntimeError("当前工程缺少刀具素材")
    used = set(_people) | set(_pending) | _reserved_names | _base()._reserved_names
    for actor in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor):
        used.add(actor.get_name())
        used.update(str(tag) for tag in actor.get_editor_property("tags"))
    numbers = [int(match.group(1)) for name in used
               for match in [re.fullmatch(re.escape(kind) + r"_([0-9]+)", name)] if match]
    number = max(numbers, default=0) + 1
    while True:
        name = kind + "_{:02d}".format(number)
        # 被销毁但等待 GC 的对象不在 Actor 列表中，仍不能重用其名称。
        if (unreal.find_object(world, "PersistentLevel." + name) is None
                and unreal.find_object(world, "PersistentLevel." + name + "_weapon") is None):
            break
        number += 1
    _reserved_names.add(name)
    for candidate in data["candidates"]:
        xy, yaw = candidate["xy"], candidate["yaw"]
        z = _support(xy, yaw, patient=kind == "patient")
        if z is None or not _clear(xy, z, patient=kind == "patient", yaw=yaw):
            continue
        if kind == "armed_person":
            route = _safe_patrol_route(candidate, z)
            if route is None:
                continue
            candidate = dict(candidate, route=route)
        person = _new_person(world, name, candidate, kind, z, mesh, animation, collapse.get_play_length())
        _pending[name] = person
        anchors = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.PlayerStart)
        try:
            if len(anchors) != 1:
                raise RuntimeError("无法唯一确定 AirSim 坐标参考点")
            a, origin = anchors[0].get_actor_location(), _context().origin()
            scale = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.WorldSettings)[0].get_editor_property("world_to_meters") / 100.0
            if not math.isfinite(scale) or scale <= 0:
                raise RuntimeError("UE 世界尺度无效")
            pose = {"position": {"x_val": (xy[0] - a.x / 100 - origin[0]) / scale,
                                 "y_val": (xy[1] - a.y / 100 - origin[1]) / scale,
                                 "z_val": (a.z / 100 + origin[2] - z
                                           - _CAPSULE_HALF_HEIGHT_M - _GROUND_CLEARANCE_M) / scale},
                    "orientation": {"w_val": 1.0, "x_val": 0.0, "y_val": 0.0, "z_val": 0.0}}
            return {"person_id": name, "weapon_pose": pose}
        except Exception:
            _release(name)
            raise
    raise ValueError("指定地点 100 米内没有平整、净空足够且未被占用的人行道落点")


def _release(name):
    person = _pending.pop(name, None) or _people.pop(name, None)
    if person is not None:
        if person.weapon is not None and _valid(person.weapon):
            person.weapon.destroy_actor()
        if _valid(person.actor):
            person.actor.destroy_actor()


def _tick(delta):
    global _tick_handle, _last_time, _accumulator
    context = _context()
    if context is None or not _valid(context.world):
        _people.clear()
        _pending.clear()
    for name, person in list(_people.items()):
        if not _valid(person.actor):
            _release(name)
    if not _people:
        if _tick_handle is not None:
            unreal.unregister_slate_post_tick_callback(_tick_handle)
            _tick_handle = None
        return
    now = unreal.GameplayStatics.get_time_seconds(context.world)
    elapsed, _last_time = max(0.0, now - _last_time), now
    if elapsed == 0:
        return
    context.keep_ground()
    _accumulator += min(elapsed, 0.5)
    steps = min(30, int(_accumulator * 60 + 1e-7))
    for _ in range(steps):
        for name, person in list(_people.items()):
            try:
                person.step(1.0 / 60)
            except Exception as error:
                person.error = str(error)
                person.action = 0 if person.kind == "armed_person" else 4
                person._animate(0)
                unreal.log_error("CITY_SCRIPT_PERSON {} stopped: {}".format(name, traceback.format_exc()))
        _accumulator -= 1.0 / 60
    for name, person in list(_people.items()):
        if person.error is None:
            try:
                person.update_weapon()
            except Exception as error:
                person.error = str(error)
                person._animate(0)
                unreal.log_error("CITY_SCRIPT_PERSON {} weapon: {}".format(name, traceback.format_exc()))


def _execute(data):
    global _world_key, _game_world, _tick_handle, _last_time, _accumulator
    context = _context()
    if context is None or not _valid(context.world):
        raise RuntimeError("请先在 UE 编辑器中启动 Play")
    key = context.world.get_path_name()
    if _world_key != key or _game_world != context.world:
        if _tick_handle is not None:
            unreal.unregister_slate_post_tick_callback(_tick_handle)
            _tick_handle = None
        _people.clear()
        _pending.clear()
        _surface_cache.clear()
        _world_key, _game_world = key, context.world
        _last_time, _accumulator = unreal.GameplayStatics.get_time_seconds(context.world), 0.0
    operation = data["operation"]
    if operation == "prepare":
        return _prepare(data)
    name = data.get("person_id")
    if operation == "release":
        _release(name)
        return None
    if operation != "finish" or name not in _pending:
        raise ValueError("无效的人物生成请求")
    person = _pending[name]
    if person.kind == "armed_person":
        weapon_name = data["weapon_name"]
        actors = unreal.GameplayStatics.get_all_actors_of_class(context.world, unreal.Actor)
        person.weapon = next((a for a in actors if a.get_name() == weapon_name), None)
        if person.weapon is None:
            raise RuntimeError("未找到本次创建的刀具对象")
        person.weapon.set_actor_enable_collision(False)
        component = person.weapon.get_component_by_class(unreal.StaticMeshComponent)
        component.set_mobility(unreal.ComponentMobility.MOVABLE)
        person.update_weapon()
    _people[name] = person
    _pending.pop(name)
    if _tick_handle is None:
        _last_time = unreal.GameplayStatics.get_time_seconds(context.world)
        _accumulator = 0.0
        _tick_handle = unreal.register_slate_post_tick_callback(_tick)
    return {"person_id": name}


def dispatch(source, result):
    try:
        data = json.loads(Path(source).read_text(encoding="utf-8"))
        response = {"ok": True, "result": _execute(data)}
    except Exception as error:
        response = {"ok": False, "error": str(error)}
        unreal.log_warning("CITY_SCRIPT_PERSON request failed: " + traceback.format_exc())
    target = Path(result)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(response, allow_nan=False), encoding="utf-8")
    os.replace(str(temporary), str(target))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("x", type=float, help="参考地点 UE 世界 X，米")
    parser.add_argument("y", type=float, help="参考地点 UE 世界 Y，米")
    args = parser.parse_args()
    print(spawn_armed_person((args.x, args.y)))
