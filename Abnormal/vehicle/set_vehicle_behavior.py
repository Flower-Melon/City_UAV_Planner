"""按 ID 控制异常车辆；车辆运动、路面检测与 UE 持续控制均实现在本脚本中。"""

import bisect
import copy
import json
import math
import os
import random
import re
import sys
import traceback
from collections import deque
from pathlib import Path

# 普通 Python 只调用公共接口；UE 加载同一脚本时启用下方控制实现。
try:
    import unreal
except ImportError:
    unreal = None

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "Abnormal.vehicle"


BEHAVIORS = ("cruise", "surge", "weave", "brake", "wrong_way", "mixed", "stop")


def set_vehicle_behavior(vehicle_id, behavior) -> None:
    """设置指定车辆的行为，确认指令生效后返回 None；不等待车辆完成加减速。

    vehicle_id: spawn_vehicle 返回的 ID。
    behavior: cruise / surge / weave / brake / wrong_way / mixed / stop。
    mixed 从巡航开始，按原场景状态机随机切换异常驾驶动作；其他模式保持至下次调用。
    速度仍受弯道、道路净空和交通限制。
    未知 ID、未由本接口管理的对象、无效行为或通信失败均抛出异常。
    """
    if not isinstance(vehicle_id, str) or re.fullmatch(r"abnormal_vehicle_[0-9]{2,}", vehicle_id) is None:
        raise ValueError("vehicle_id 必须为 spawn_vehicle 返回的异常车 ID")
    if not isinstance(behavior, str) or behavior not in BEHAVIORS:
        raise ValueError("behavior 必须为 " + " / ".join(BEHAVIORS))
    from .spawn_vehicle import AirSimClient, request

    with AirSimClient(timeout=10) as client:
        request(client, "behavior", vehicle_id=vehicle_id, behavior=behavior)


def clamp(value, low, high):
    return max(low, min(high, value))


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Loop:
    def __init__(self, points):
        self.points = [tuple(p) for p in points]
        if len(points) < 4 or distance(points[0], points[-1]) > 0.01:
            raise ValueError("车辆路线必须为闭合折线")
        self.ends = [0.0]
        for a, b in zip(self.points, self.points[1:]):
            length = distance(a, b)
            if length < 1e-7:
                raise ValueError("路线包含重复路点")
            self.ends.append(self.ends[-1] + length)
        self.length = self.ends[-1]

    def sample(self, progress, offset=0.0):
        s = progress % self.length
        i = min(bisect.bisect_right(self.ends, s) - 1, len(self.points) - 2)
        a, b = self.points[i:i + 2]
        length = self.ends[i + 1] - self.ends[i]
        dx, dy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
        t = (s - self.ends[i]) / length
        return (a[0] + (b[0] - a[0]) * t - dy * offset,
                a[1] + (b[1] - a[1]) * t + dx * offset), math.atan2(dy, dx)

    def project_near(self, point, progress):
        best = (float("inf"), progress)
        # 仅在当前位置前后 25 米查找，避免交叉路口跳到另一段路线。
        for shift in (-self.length, 0, self.length):
            s = progress % self.length + shift
            low = max(0, bisect.bisect_right(self.ends, s - 25) - 1)
            high = min(len(self.points) - 2, bisect.bisect_left(self.ends, s + 25))
            for i in range(low, high + 1):
                a, b = self.points[i:i + 2]
                dx, dy = b[0] - a[0], b[1] - a[1]
                t = clamp(((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / (dx * dx + dy * dy), 0, 1)
                q = (a[0] + dx * t, a[1] + dy * t)
                candidate = (distance(point, q), self.ends[i] + (self.ends[i + 1] - self.ends[i]) * t)
                if candidate < best:
                    best = candidate
        return best[1] % self.length

    def curve_limit(self, progress, speed, lateral_acceleration, maximum):
        _, previous = self.sample(progress)
        curvature = 0.0
        for ahead in range(2, int(max(18, speed * 2.0)) + 2, 2):
            _, angle = self.sample(progress + ahead)
            change = abs(math.atan2(math.sin(angle - previous), math.cos(angle - previous)))
            curvature = max(curvature, change / 2.0)
            previous = angle
        return min(maximum, math.sqrt(lateral_acceleration / curvature)) if curvature > 1e-7 else maximum


class Motion:
    def __init__(self, points, settings):
        self.loop = Loop(points)
        self.settings = settings
        self.xy, self.yaw = self.loop.sample(0)
        self.progress = self.speed = self.acceleration = self.steering = 0.0
        self.behavior = "stop"
        self.behavior_seconds = 0.0
        self.safe_offset = 0.0
        self.guard = "None"
        self._random = random.Random()
        self._reset_mixed()

    def _reset_mixed(self):
        self._mixed_mode = "cruise"
        self._mixed_next_mode = 1.3
        self._mixed_next_decision = self._mixed_next_lateral = 0.0
        self._mixed_decisions = 0
        self._mixed_last_sign = 1
        self._mixed_pending = (self.settings["cruise_speed_kmh"] / 3.6, 0.0)
        self._mixed_intent = self._mixed_pending

    @property
    def active_behavior(self):
        return self._mixed_mode if self.behavior == "mixed" else self.behavior

    def set_behavior(self, behavior):
        self.behavior = behavior
        self.behavior_seconds = 0.0
        if behavior == "mixed":
            self._reset_mixed()

    def _advance_mixed(self):
        """对应原异常车 ChooseIntent：保留状态转移、持续时间及反应延迟。"""
        rng, s, now = self._random, self.settings, self.behavior_seconds
        amplitude = s["weave_amplitude_m"]
        if now < self._mixed_next_mode:
            if self._mixed_mode in ("weave", "surge") and now >= self._mixed_next_lateral:
                if rng.random() < 0.9:
                    self._mixed_last_sign *= -1
                self._mixed_pending = (self._mixed_pending[0],
                                       self._mixed_last_sign * amplitude * rng.uniform(0.85, 1.0))
                self._mixed_next_decision = now + rng.uniform(0.15, 0.4)
                self._mixed_next_lateral = now + rng.uniform(1.8, 3.2)
        else:
            previous, roll = self._mixed_mode, rng.random()
            if self._mixed_decisions == 0:
                mode = "weave"
            elif previous == "weave":
                mode = "countersteer"
            elif previous == "brake":
                mode = "surge"
            elif previous == "countersteer":
                mode = "surge" if roll < 0.78 else "cruise"
            elif previous == "surge":
                mode = "brake" if roll < 0.12 else "wrong_way" if roll < 0.42 else "weave"
            elif previous == "wrong_way":
                mode = "cruise" if roll < 0.5 else "surge"
            else:
                mode = "weave" if roll < 0.7 else "surge"

            center, heading = self.loop.sample(self.progress)
            lateral = -(self.xy[0] - center[0]) * math.sin(heading) + (self.xy[1] - center[1]) * math.cos(heading)
            if mode == "countersteer" and abs(lateral) > 0.5:
                self._mixed_last_sign = -1 if lateral > 0 else 1
            else:
                self._mixed_last_sign *= -1
            duration = rng.uniform(6.0, 10.0)
            speed = s["cruise_speed_kmh"] * rng.uniform(0.95, 1.1) / 3.6
            offset = 0.0
            if mode == "weave":
                offset = self._mixed_last_sign * amplitude * rng.uniform(0.85, 1.0)
                duration = rng.uniform(8.0, 13.0)
            elif mode == "countersteer":
                offset = self._mixed_last_sign * amplitude * rng.uniform(0.9, 1.0)
                duration = rng.uniform(2.5, 4.0)
            elif mode == "surge":
                speed = s["surge_speed_kmh"] * rng.uniform(0.9, 1.0) / 3.6
                offset = self._mixed_last_sign * amplitude * rng.uniform(0.8, 1.0)
            elif mode == "brake":
                speed = s["brake_speed_kmh"] * rng.uniform(0.85, 1.0) / 3.6
                offset = lateral * 0.6
                duration = rng.uniform(1.0, 1.7)
            elif mode == "cruise":
                duration = rng.uniform(1.4, 2.5)
            elif mode == "wrong_way":
                speed = s["cruise_speed_kmh"] * rng.uniform(1.0, 1.2) / 3.6
                offset = -amplitude
                duration = rng.uniform(8.0, 16.0)
            self._mixed_mode = mode
            self._mixed_pending = (speed, offset)
            self._mixed_next_decision = now + rng.uniform(0.15, 0.4)
            self._mixed_next_lateral = now + rng.uniform(1.8, 3.2)
            self._mixed_next_mode = now + duration
            self._mixed_decisions += 1

        if now >= self._mixed_next_decision:
            self._mixed_intent = self._mixed_pending
        return self._mixed_intent

    def intention(self):
        if self.behavior == "mixed":
            return self._advance_mixed()
        s = self.settings
        speed = {"stop": 0.0, "cruise": s["cruise_speed_kmh"], "surge": s["surge_speed_kmh"],
                 "weave": s["cruise_speed_kmh"], "brake": s["brake_speed_kmh"],
                 "wrong_way": s["wrong_way_speed_kmh"]}[self.behavior] / 3.6
        offset = 0.0
        if self.behavior == "weave":
            offset = s["weave_amplitude_m"] * math.sin(2 * math.pi * self.behavior_seconds / s["weave_period_s"])
        elif self.behavior == "wrong_way":
            offset = -s["weave_amplitude_m"]
        return speed, offset

    def steering_for(self, dt, speed_cap, offset):
        s = self.settings
        # 弯道减速时同步缩短预瞄，避免仍按入弯前的高速预瞄距离切过内侧路沿。
        look = clamp(3.0 + min(self.speed, speed_cap) * 0.4, 3, 12)
        goal, _ = self.loop.sample(self.progress + look, offset)
        alpha = math.atan2(goal[1] - self.xy[1], goal[0] - self.xy[0]) - self.yaw
        desired = math.atan2(2 * s["wheelbase_m"] * math.sin(alpha), max(1, distance(goal, self.xy)))
        limit, slew = math.radians(s["steering_limit_deg"]), math.radians(s["steering_rate_deg_s"]) * dt
        return self.steering + clamp(clamp(desired, -limit, limit) - self.steering, -slew, slew)

    def advance(self, dt, speed, steering):
        yaw = self.yaw + speed / self.settings["wheelbase_m"] * math.tan(steering) * dt
        heading = (self.yaw + yaw) * 0.5
        xy = (self.xy[0] + speed * math.cos(heading) * dt, self.xy[1] + speed * math.sin(heading) * dt)
        return xy, yaw

    def trajectory(self, length, speed_cap, offset):
        """按同一转向模型预演前方轨迹，不推进真实车辆或随机行为状态。"""
        preview = copy.copy(self)
        speed = max(2.0, self.speed)
        along = 0.0
        while along < length:
            travel = min(0.75, length - along)
            dt = travel / speed
            preview.steering = preview.steering_for(dt, speed_cap, offset)
            preview.xy, preview.yaw = preview.advance(dt, speed, preview.steering)
            preview.progress = preview.loop.project_near(preview.xy, preview.progress)
            along += travel
            yield preview.xy, preview.yaw, along

    def candidate(self, dt, speed_cap, offset):
        """计算候选状态；路面验证通过后由调用方提交，失败不会跨越障碍。"""
        s = self.settings
        target, _ = self.intention()
        target = min(target, speed_cap)
        steering = self.steering_for(dt, speed_cap, offset)
        curvature = abs(math.tan(steering) / s["wheelbase_m"])
        if curvature > 1e-7:
            target = min(target, math.sqrt(s["lateral_acceleration_m_s2"] / curvature))
        wanted = clamp((target - self.speed) * 1.5, -s["braking_m_s2"], s["acceleration_m_s2"])
        acceleration = self.acceleration + clamp(wanted - self.acceleration, -s["jerk_m_s3"] * dt, s["jerk_m_s3"] * dt)
        speed = max(0.0, self.speed + acceleration * dt)
        if target == 0 and speed < 0.03:
            speed = acceleration = 0.0
        xy, yaw = self.advance(dt, speed, steering)
        return xy, yaw, speed, acceleration, steering

    def commit(self, candidate, dt):
        self.xy, self.yaw, self.speed, self.acceleration, self.steering = candidate
        self.progress = self.loop.project_near(self.xy, self.progress)
        self.behavior_seconds += dt

    def blocked(self, dt, steering):
        # 车身停住时方向盘仍可转动，否则每帧都会重复同一个失败候选状态。
        self.steering = steering
        self.speed = self.acceleration = 0.0
        self.behavior_seconds += dt


ROAD_OBJECTS = [unreal.ObjectTypeQuery.OBJECT_TYPE_QUERY1] if unreal is not None else []
SOLID_OBJECTS = (ROAD_OBJECTS + [unreal.ObjectTypeQuery.OBJECT_TYPE_QUERY2,
                                unreal.ObjectTypeQuery.OBJECT_TYPE_QUERY3]) if unreal is not None else []


class World:
    def __init__(self, world):
        self.world = world
        self.ground_cache = {}
        self.levels = []
        assets = unreal.AssetRegistryHelpers.get_asset_registry().get_assets_by_class("World")
        for asset in assets:
            name = str(asset.asset_name)
            if name.startswith("Town12_Tile_") and (name.endswith("_Static") or name.endswith("_Buildings_HLOD_Test")):
                level = unreal.GameplayStatics.get_streaming_level(world, name)
                if level is not None and level not in self.levels:
                    self.levels.append(level)
        self.keep_ground()
        unreal.GameplayStatics.flush_level_streaming(world)

    def keep_ground(self):
        # 当前原生流式加载器只认识原来的单车；让脚本车辆沿途的路面保持可测量。
        for level in self.levels:
            level.set_editor_property("should_be_loaded", True)
            level.set_editor_property("should_be_visible", True)

    def origin(self):
        o = unreal.GameplayStatics.get_world_origin_location(self.world)
        return o.x / 100.0, o.y / 100.0, o.z / 100.0

    def vector(self, x, y, z):
        o = self.origin()
        return unreal.Vector((x - o[0]) * 100, (y - o[1]) * 100, (z - o[2]) * 100)

    def ground(self, xy):
        key = (round(xy[0], 2), round(xy[1], 2))
        if key in self.ground_cache:
            return self.ground_cache[key]
        start, end = self.vector(xy[0], xy[1], 1000), self.vector(xy[0], xy[1], -1000)
        h = None
        # 高处的路灯横臂、树冠可能遮住路面；逐层向下查找，不能把首个命中当成地面。
        for _ in range(24):
            hit = unreal.SystemLibrary.line_trace_single_for_objects(
                self.world, start, end, ROAD_OBJECTS, True, [], unreal.DrawDebugTrace.NONE, True)
            h = hit.to_tuple() if hit is not None else None
            if not h or not h[0]:
                return None
            component = h[10]
            mesh = component.get_editor_property("static_mesh") if isinstance(component, unreal.StaticMeshComponent) else None
            description = str(mesh)
            road = "Roads_Road_" in description or "Roads_Marking_" in description
            if not road and any(name in description for name in ("RN_R", "RN_J", "SM_RoadBake", "SM_CityRoadSection")):
                material, _ = component.get_material_from_collision_face_index(h[14])
                road = any(word in str(material).lower() for word in ("road", "asphalt", "marking"))
            if road:
                if h[7].z < 0.65:
                    return None
                break
            start = unreal.Vector(start.x, start.y, h[5].z - 2.0)
        else:
            return None
        z = h[5].z / 100.0 + self.origin()[2]
        if len(self.ground_cache) > 16000:
            self.ground_cache.clear()
        self.ground_cache[key] = z
        return z

    def footprint(self, xy, yaw):
        z = self.ground(xy)
        if z is None:
            return None
        c, s = math.cos(yaw), math.sin(yaw)
        supported = 0
        for x in (-1.3, 1.3):
            for y in (-0.85, 0.85):
                h = self.ground((xy[0] + x * c - y * s, xy[1] + x * s + y * c))
                supported += h is not None and abs(h - z) < 0.45
        return z if supported >= 3 else None

    def clearance(self, actor, xy, yaw, z, length, half_size, margin, heading=None):
        """沿位移方向扫掠真实车身；yaw 为车身朝向，heading 为扫掠方向。"""
        heading = yaw if heading is None else heading
        center = actor.get_actor_bounds(False)[0]
        location = actor.get_actor_location()
        center_z = z + (center.z - location.z) / 100.0
        start = self.vector(xy[0], xy[1], center_z + 0.12)
        end = self.vector(xy[0] + math.cos(heading) * length, xy[1] + math.sin(heading) * length, center_z + 0.12)
        # 抬高盒子底部少许，排除支撑路面的浮点误命中。
        box = unreal.Vector((half_size[0] + margin) * 100, (half_size[1] + margin) * 100,
                            max(0.1, half_size[2] - 0.12) * 100)
        hit = unreal.SystemLibrary.box_trace_single_for_objects(
            self.world, start, end, box, unreal.Rotator(pitch=0, yaw=math.degrees(yaw), roll=0),
            SOLID_OBJECTS, False, [actor], unreal.DrawDebugTrace.NONE, True)
        h = hit.to_tuple() if hit is not None else None
        self.clearance_guard = "TrafficOrObstacle"
        if h and h[0] and h[10] is not None:
            if h[10].get_editor_property("mobility") == unreal.ComponentMobility.STATIC:
                self.clearance_guard = "StaticObstacle"
        return max(0.0, h[3] / 100.0) if h and h[0] else length

    def place(self, actor, xy, yaw, z):
        actor.set_actor_location_and_rotation(self.vector(xy[0], xy[1], z),
                                               unreal.Rotator(pitch=0, yaw=math.degrees(yaw), roll=0), False, True)


VEHICLE_ASSET = "/Game/Carla/Static/Car/4Wheeled/Toyota_Prius/Vh_Car_ToyotaPrius_NOrig.Vh_Car_ToyotaPrius_NOrig"
_context = None
_world_key = None
_pending = {}
_vehicles = {}
_reserved_names = set()
_tick_handle = None
_last_time = None
_accumulator = 0.0


def valid(obj):
    # UE 销毁 UObject 后，Python 包装器本身可能已无法传给 IsValid。
    try:
        return unreal.SystemLibrary.is_valid(obj)
    except Exception:
        return False


class Vehicle:
    def __init__(self, actor, route, settings, lift):
        self.actor = actor
        self.motion = Motion(route["points"], settings)
        self.route = route
        self.lift = lift
        component = actor.get_component_by_class(unreal.StaticMeshComponent)
        component.set_mobility(unreal.ComponentMobility.MOVABLE)
        component.set_simulate_physics(False)
        component.set_collision_profile_name("BlockAllDynamic")
        component.set_collision_enabled(unreal.CollisionEnabled.QUERY_ONLY)
        component.set_editor_property("receives_decals", False)
        low, high = component.get_local_bounds()
        self.half_size = ((high.x - low.x) / 200, (high.y - low.y) / 200, (high.z - low.z) / 200)
        tags = list(actor.get_editor_property("tags"))
        tags.extend([unreal.Name(actor.get_name()), unreal.Name("CityScriptAbnormalVehicle")])
        actor.set_editor_property("tags", tags)
        self.z = _context.footprint(self.motion.xy, self.motion.yaw)
        if self.z is None:
            raise ValueError("出生点车身不能完整落在可行驶路面上")
        _context.place(actor, self.motion.xy, self.motion.yaw, self.z + lift)
        free = _context.clearance(actor, self.motion.xy, self.motion.yaw, self.z + lift,
                                  0.05, self.half_size, settings["traffic_clearance_m"])
        if free < 0.04:
            raise ValueError("出生点被车辆、行人或静态物体占用，请更换坐标")
        self.plan_timer = 0.0
        self.speed_cap = self.offset = 0.0
        self.error = None
        self.trail = deque([(self.motion.xy, self.motion.yaw, self.motion.steering)], maxlen=200)
        self.blocked_seconds = self.recovery_remaining = self.centering_remaining = 0.0

    def note_progress(self, travel, dt):
        model = self.motion
        self.centering_remaining = max(0.0, self.centering_remaining - travel)
        if distance(self.trail[-1][0], model.xy) >= 0.2:
            self.trail.append((model.xy, model.yaw, model.steering))
        if model.guard in ("Road", "StaticObstacle") and model.behavior != "stop" and travel < 0.002:
            self.blocked_seconds += dt
        else:
            self.blocked_seconds = 0.0
        if self.blocked_seconds >= 0.8 and len(self.trail) > 1:
            # 已贴住路沿时仅转方向盘未必有足够转弯空间；沿走过的安全轨迹低速退让。
            self.recovery_remaining = 5.0
            self.centering_remaining = 15.0
            self.blocked_seconds = 0.0
            model.speed = model.acceleration = 0.0

    def recover(self, dt):
        model = self.motion
        model.behavior_seconds += dt
        while len(self.trail) > 1 and distance(model.xy, self.trail[-1][0]) < 1e-6:
            self.trail.pop()
        xy, yaw, steering = self.trail[-1]
        gap = distance(model.xy, xy)
        if gap < 1e-6 or self.recovery_remaining < 0.01:
            self.recovery_remaining = 0.0
            self.plan_timer = 0.0
            return
        travel = min(1.5 * dt, gap, self.recovery_remaining)
        fraction = travel / gap
        xy = tuple(a + (b - a) * fraction for a, b in zip(model.xy, xy))
        turn = math.atan2(math.sin(yaw - model.yaw), math.cos(yaw - model.yaw))
        yaw = model.yaw + turn * fraction
        z = _context.footprint(xy, yaw)
        heading = math.atan2(xy[1] - model.xy[1], xy[0] - model.xy[0])
        free = _context.clearance(self.actor, model.xy, yaw, self.z + self.lift, travel + 0.03,
                                  self.half_size, 0.0, heading)
        if z is None or abs(z - self.z) > max(0.15, travel * 0.8) or free < travel + 0.02:
            model.guard = "Road" if z is None else getattr(_context, "clearance_guard", "TrafficOrObstacle")
            return
        _context.place(self.actor, xy, yaw, z + self.lift)
        model.xy, model.yaw = xy, yaw
        model.steering += (steering - model.steering) * fraction
        model.progress = model.loop.project_near(xy, model.progress)
        model.guard = "Recovering"
        self.z = z
        self.recovery_remaining -= travel

    def path_clearance(self, length, speed_cap, offset):
        """沿可实现的转弯轨迹逐段检查车身，不能把直线延长到弯外路沿。"""
        model = self.motion
        previous, previous_yaw, previous_z = model.xy, model.yaw, self.z
        along = 0.0
        for xy, yaw, end in model.trajectory(length, speed_cap, offset):
            travel = distance(previous, xy)
            z = _context.footprint(xy, yaw)
            if z is None or abs(z - previous_z) > max(0.15, travel * 0.8):
                return along, "Road"
            heading = math.atan2(xy[1] - previous[1], xy[0] - previous[0])
            # 扫掠方向与车身朝向分开；弯道上的车身朝向并不等于位移方向。
            free = _context.clearance(self.actor, previous, previous_yaw, previous_z + self.lift,
                                      travel, self.half_size, model.settings["traffic_clearance_m"], heading)
            if free < travel - 0.01:
                return along + free, getattr(_context, "clearance_guard", "TrafficOrObstacle")
            previous, previous_yaw, previous_z = xy, yaw, z
            along = end
        return length, "None"

    def plan(self):
        model, settings = self.motion, self.motion.settings
        speed, desired = model.intention()
        if self.centering_remaining > 0:
            desired = 0.0
            speed = min(speed, 3.0)
        self.speed_cap = model.loop.curve_limit(model.progress, model.speed,
                                                 settings["lateral_acceleration_m_s2"], speed)
        # 道路和车身支撑限制横摆幅度；不把人行道作为可行驶区域。
        offsets = sorted(set([desired, 0.0] + [i * 0.5 for i in range(-6, 7)]),
                         key=lambda offset: (abs(offset - desired), abs(offset), offset))
        stopping = model.speed * model.speed / (2 * settings["braking_m_s2"]) + model.speed * 0.6 + 4
        best = (-1.0, 0.0, "Road")
        for offset in offsets:
            good = True
            for ahead in (max(3, model.speed * 0.25), max(6, model.speed * 0.5)):
                xy, yaw = model.loop.sample(model.progress + ahead, offset)
                z = _context.footprint(xy, yaw)
                center, _ = model.loop.sample(model.progress + ahead)
                base = _context.ground(center)
                if z is None or base is None or abs(z - base) > settings["road_height_tolerance_m"]:
                    good = False
                    break
            if good:
                free, guard = self.path_clearance(stopping, self.speed_cap, offset)
                if free > best[0]:
                    best = (free, offset, guard)
                if guard not in ("Road", "StaticObstacle"):
                    break
        free, self.offset, model.guard = best
        if free < stopping - 0.01:
            self.speed_cap = min(self.speed_cap, math.sqrt(2 * settings["braking_m_s2"] *
                                      max(0, free - model.speed * 0.6 - 0.5)))
        if model.active_behavior == "wrong_way" and self.offset > -1.5 and not self.centering_remaining:
            self.speed_cap = 0.0
            model.guard = "NoOppositeRoad"

    def step(self, dt):
        model = self.motion
        if self.error:
            return
        if model.behavior == "stop":
            self.recovery_remaining = 0.0
        if self.recovery_remaining > 0:
            self.recover(dt)
            return
        if model.behavior == "stop" and model.speed == 0:
            model.behavior_seconds += dt
            return
        self.plan_timer -= dt
        if self.plan_timer <= 0:
            self.plan()
            self.plan_timer = 0.1
        # 规划检查前方弯曲轨迹；每一帧另验证实际短位移，及时响应新出现的障碍。
        margin = model.settings["traffic_clearance_m"]
        candidate = model.candidate(dt, self.speed_cap, self.offset)
        xy, yaw = candidate[:2]
        z = _context.footprint(xy, yaw)
        travel = math.hypot(xy[0] - model.xy[0], xy[1] - model.xy[1])
        heading = math.atan2(xy[1] - model.xy[1], xy[0] - model.xy[0]) if travel > 1e-7 else model.yaw
        free = _context.clearance(self.actor, model.xy, yaw, self.z + self.lift,
                                  travel + 0.03, self.half_size, margin, heading)
        if z is None or abs(z - self.z) > max(0.15, travel * 0.8) or free < travel + 0.02:
            model.blocked(dt, candidate[4])
            model.guard = "Road" if z is None else getattr(_context, "clearance_guard", "TrafficOrObstacle")
            self.note_progress(0.0, dt)
            return
        _context.place(self.actor, xy, yaw, z + self.lift)
        self.z = z
        model.commit(candidate, dt)
        self.note_progress(travel, dt)

    def snapshot(self):
        m = self.motion
        return {"vehicle_id": self.actor.get_name(), "behavior": m.behavior,
                "active_behavior": m.active_behavior,
                "position_m": [m.xy[0], m.xy[1], self.z], "speed_m_s": m.speed,
                "progress_m": m.progress, "route_length_m": m.loop.length,
                "guard": m.guard, "error": self.error}


def _ensure_world():
    global _context, _world_key, _pending, _vehicles, _last_time, _accumulator
    world = unreal.EditorLevelLibrary.get_game_world()
    if world is None:
        raise RuntimeError("请先在 UE 编辑器中启动 GroundFix3 地图的 Play 仿真")
    key = world.get_path_name()
    if (key != _world_key or _context is None or _context.world != world
            or not valid(_context.world)):
        # 关闭当前 Play 中原插件的成组生成，保留所有由本项目 API 创建的目标。
        _reserved_names.update(a.get_name() for a in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor)
                               if valid(a))
        unreal.SystemLibrary.execute_console_command(world, "citypeople stop", None)
        unreal.SystemLibrary.execute_console_command(world, "citynpc.abnormal return", None)
        unreal.SystemLibrary.execute_console_command(world, "citynpc.abnormal stop", None)
        for actor in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor):
            if not valid(actor):
                continue
            # DestroyActor 后对象仍可能等待 GC，AirSim 按同名生成会触发 UE Fatal。
            _reserved_names.add(actor.get_name())
            tags = {str(tag) for tag in actor.get_editor_property("tags")}
            native = bool(tags.intersection(("CityAbnormalVehicle", "CityAbnormalPerson")))
            scripted = bool(tags.intersection(("CityScriptAbnormalVehicle", "CityScriptAbnormalPerson")))
            if native and not scripted:
                actor.destroy_actor()
        _context = World(world)
        _world_key, _pending, _vehicles = key, {}, {}
        _last_time = unreal.GameplayStatics.get_time_seconds(world)
        _accumulator = 0.0
    return world


def _tick(delta):
    global _last_time, _accumulator, _tick_handle
    if _context is None:
        return
    if not valid(_context.world):
        _vehicles.clear()
        _pending.clear()
        if _tick_handle is not None:
            unreal.unregister_slate_post_tick_callback(_tick_handle)
            _tick_handle = None
        return
    for name, vehicle in list(_vehicles.items()):
        if not valid(vehicle.actor):
            _vehicles.pop(name, None)
    if not _vehicles:
        if _tick_handle is not None:
            unreal.unregister_slate_post_tick_callback(_tick_handle)
            _tick_handle = None
        return
    now = unreal.GameplayStatics.get_time_seconds(_context.world)
    elapsed = max(0.0, now - _last_time)
    _last_time = now
    if elapsed == 0:
        return
    _context.keep_ground()
    _accumulator += min(elapsed, 0.5)
    steps = min(30, int(_accumulator * 60 + 1e-7))
    for _ in range(steps):
        for name, vehicle in list(_vehicles.items()):
            if not valid(vehicle.actor):
                _vehicles.pop(name, None)
                continue
            try:
                vehicle.step(1.0 / 60)
            except Exception as error:
                vehicle.error = str(error)
                vehicle.motion.speed = vehicle.motion.acceleration = 0.0
                vehicle.motion.behavior = "stop"
                unreal.log_error("CITY_SCRIPT_VEHICLE {} stopped: {}".format(name, traceback.format_exc()))
        _accumulator -= 1.0 / 60


def _prepare(data, world):
    route, settings = data["route"], data["settings"]
    model = Motion(route["points"], settings)
    z = _context.footprint(model.xy, model.yaw)
    if z is None:
        raise ValueError("出生点未找到支持车身的机动车路面；请确认地图地块已加载")
    mesh = unreal.load_asset(VEHICLE_ASSET)
    if mesh is None:
        raise RuntimeError("当前工程缺少异常车使用的 Toyota Prius 模型")
    lift = max(0.0, -mesh.get_bounding_box().min.z / 100.0) + 0.015
    used = set(_pending) | set(_vehicles) | _reserved_names
    for actor in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor):
        used.add(actor.get_name())
        used.update(str(t) for t in actor.get_editor_property("tags"))
    numbers = [int(match.group(1)) for name in used
               for match in [re.fullmatch(r"abnormal_vehicle_([0-9]+)", name)] if match]
    number = max(numbers, default=0) + 1
    name = "abnormal_vehicle_{:02d}".format(number)
    while unreal.find_object(world, "PersistentLevel." + name) is not None:
        number += 1
        name = "abnormal_vehicle_{:02d}".format(number)
    _reserved_names.add(name)
    anchors = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.PlayerStart)
    if len(anchors) != 1:
        raise RuntimeError("无法唯一确定 AirSim PlayerStart 坐标参考点")
    a, o = anchors[0].get_actor_location(), _context.origin()
    anchor = (a.x / 100.0 + o[0], a.y / 100.0 + o[1], a.z / 100.0 + o[2])
    scale = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.WorldSettings)[0].get_editor_property("world_to_meters") / 100.0
    if scale <= 0:
        raise RuntimeError("UE 世界坐标尺度无效")
    pose = {"position": {"x_val": (model.xy[0] - anchor[0]) / scale,
                         "y_val": (model.xy[1] - anchor[1]) / scale,
                         "z_val": (anchor[2] - z - lift) / scale},
            "orientation": {"w_val": math.cos(model.yaw / 2), "x_val": 0.0,
                            "y_val": 0.0, "z_val": math.sin(model.yaw / 2)}}
    _pending[name] = (route, settings, lift)
    return {"vehicle_id": name, "pose": pose}


def _execute(data):
    global _tick_handle
    world = _ensure_world()
    operation = data["operation"]
    if operation == "api_setup":
        return None
    if operation == "prepare":
        return _prepare(data, world)
    name = data.get("vehicle_id")
    if operation == "release":
        _pending.pop(name, None)
        _vehicles.pop(name, None)
        return None
    if operation == "attach":
        route, settings, lift = _pending[name]
        actors = [a for a in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor) if a.get_name() == name]
        if len(actors) != 1:
            raise RuntimeError("AirSim 未生成预期的车辆对象 " + name)
        vehicle = Vehicle(actors[0], route, settings, lift)
        _vehicles[name] = vehicle
        _pending.pop(name)
        if _tick_handle is None:
            _tick_handle = unreal.register_slate_post_tick_callback(_tick)
        unreal.log("CITY_SCRIPT_VEHICLE created {} length_m={:.2f}".format(name, vehicle.motion.loop.length))
        return vehicle.snapshot()
    if name not in _vehicles or not valid(_vehicles[name].actor):
        raise ValueError("当前仿真中没有由本接口管理的车辆 " + str(name))
    vehicle = _vehicles[name]
    if operation == "behavior":
        behavior = data["behavior"]
        if behavior not in BEHAVIORS:
            raise ValueError("不支持的车辆行为 " + str(behavior))
        if vehicle.error:
            raise RuntimeError("车辆运行时已停止：" + vehicle.error)
        vehicle.motion.set_behavior(behavior)
        vehicle.plan_timer = 0.0
        return vehicle.snapshot()
    if operation == "inspect":
        return vehicle.snapshot()
    raise ValueError("未知异常车辆操作 " + str(operation))


def dispatch(source, result):
    """每条指令以原子写入的 JSON 结果确认；命令被接收不等于操作成功。"""
    try:
        data = json.loads(Path(source).read_text(encoding="utf-8"))
        response = {"ok": True, "result": _execute(data)}
    except Exception as error:
        response = {"ok": False, "error": str(error)}
        unreal.log_warning("CITY_SCRIPT_VEHICLE request failed: " + traceback.format_exc())
    target = Path(result)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(response, allow_nan=False), encoding="utf-8")
    os.replace(str(temporary), str(target))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vehicle_id")
    parser.add_argument("behavior", choices=BEHAVIORS)
    args = parser.parse_args()
    set_vehicle_behavior(args.vehicle_id, args.behavior)
