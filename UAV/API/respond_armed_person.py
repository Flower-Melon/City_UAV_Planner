"""无人机抵近持刀人员，接管并快速投网，完成后悬停。"""

from concurrent.futures import ThreadPoolExecutor
import json
import logging
import math
from pathlib import Path
import re
import sys
import time
import uuid

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "UAV.API"

from web.utils.people_state import get_person_state
from .fly_to_point import _world_position
from ..runtime.airsim_client import AirSimClient
from ..runtime.airsim_state import WorldFrame, vehicle_name, editor_log, _run_ue_json
from ..runtime.flight_control import FlightControl, hold_position, vehicle_lock
from ..utils.plan_flight_path import _clear_path, _positive, _xyz, plan_flight_path
from ..utils.select_UAV import _reserve_nearest_UAV

_HOVER_HEIGHT = 12.0
_TAKEOVER_RADIUS = 20.0
_TAKEOVER_HEIGHT_TOLERANCE = 6.0
logger = logging.getLogger(__name__)


class _Response:
    def __init__(self, client):
        self.client = client
        self.path = editor_log().parents[1] / "ArmedResponse" / "latest_state.json"

    def read(self):
        try:
            state = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if isinstance(state, dict):
                return state
        except (OSError, ValueError):
            pass  # UE直接覆盖状态文件，写入期间可能读到不完整JSON。
        return {}

    def command(self, command):
        before = self.read().get("command_seq")
        if self.client.call("simRunConsoleCommand", "citypeople response " + command) is not True:
            raise RuntimeError(f"UE未执行处置命令：{command}")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            state = self.read()
            if state.get("command_seq") != before and state.get("last_command") == command:
                if not (state.get("last_result") == "ok" or state.get("last_result", "").startswith("planned ")) or state.get("last_error"):
                    raise RuntimeError(f"处置命令失败：{state.get('last_error') or command}")
                return state
            time.sleep(0.05)
        raise RuntimeError("未收到新的处置命令回执")

    def check(self, person_id, drone=None):
        state = self.read()
        if not state:
            return {}  # 状态文件覆盖写入期间，交由等待方下一轮读取。
        if (state.get("person") != person_id
                or drone is not None and state.get("kit_drone") != drone):
            raise RuntimeError("处置控制权已被其他人物、无人机或命令接管")
        if state.get("last_error"):
            raise RuntimeError("人物处置中断：" + state["last_error"])
        return state


def _external(client, person_id, enabled):
    token = "CITY_RESPONSE_CONTROL_" + uuid.uuid4().hex + ":"
    code = f"""
import sys,unreal,json,math
m=sys.modules.get('city_abnormal_people_api')
p=getattr(m,'_people',{{}}).get({person_id!r})
if p is not None:
    if not {enabled!r}:
        v=p.actor.get_actor_location()
        o=unreal.GameplayStatics.get_world_origin_location(p.actor.get_world())
        p.xy=((v.x+o.x)/100,(v.y+o.y)/100)
        p.z=(v.z+o.z)/100-p.actor.get_editor_property('capsule_component').get_scaled_capsule_half_height()/100-m._GROUND_CLEARANCE_M
        p.yaw=math.radians(p.actor.get_actor_rotation().yaw)
        p.route_index=min(range(len(p.route)),key=lambda i:math.hypot(p.route[i][0]-p.xy[0],p.route[i][1]-p.xy[1]))
        p.action,p.phase_remaining=0,3.0
    p.external={enabled!r}
unreal.log({token!r}+json.dumps({{'ok':True}}))
"""
    _run_ue_json(client, "exec(" + repr(code) + ")", token, "人物接管标记命令未执行", "UE未确认人物接管标记")


class _TargetReached(Exception):
    pass


def _position(flight):
    return _world_position(flight.client, flight.name, flight.origin, flight.frame.scale)


def _hover(response, feet, frm=None):
    values = (*feet, _HOVER_HEIGHT) if frm is None else (*feet, _HOVER_HEIGHT, *frm)
    state = response.command("hoverpick " + " ".join(f"{v:.6f}" for v in values))
    pick = state.get("hover_pick", {})
    if not pick.get("ok"):
        raise RuntimeError("人物附近没有无遮挡的悬停点")
    return _xyz(pick["xyz_world_m"], "处置悬停点")


def _approach(flight, hover, speed):
    flight.stop()
    actual = _position(flight)
    distance = math.dist(actual, hover)
    if distance <= 1.0:
        flight._collision()
        return
    # 制动期间人物仍可能继续巡游，允许这段位移，但不绕过直线路径净空检查。
    if distance > 60.0 or not _clear_path([actual, hover], 1.5, 1.0):
        raise RuntimeError("未到达可安全对位的投网悬停点附近")
    align_speed = min(speed, 4.0)
    timeout = max(15.0, distance / align_speed * 2.0 + 8.0)
    completed = flight.client.call(
        "moveToPosition", *flight._ned(hover), align_speed / flight.frame.scale, timeout,
        0, {"is_rate": True, "yaw_or_rate": 0.0}, 3.0 / flight.frame.scale, 0.0, flight.name,
        timeout=timeout + 5.0,
    )
    flight.stop()
    flight._collision()
    if type(completed) is not bool or math.dist(_position(flight), hover) > 1.0:
        raise RuntimeError("无人机未完成投网悬停对位")


def _rendezvous(flight, response, person_id, cap, speed):
    """进入近距离接管范围即制动，不为移动目标反复制动和回追航点。"""
    person = get_person_state(person_id, client=flight.client)
    initial_distance = math.dist(_position(flight), person["position_m"])
    deadline = time.monotonic() + max(90.0, initial_distance / speed * 3.0 + 45.0)

    def reached(world, feet):
        return (math.dist(world[:2], feet[:2]) <= _TAKEOVER_RADIUS
                and abs(world[2] - feet[2] - _HOVER_HEIGHT) <= _TAKEOVER_HEIGHT_TOLERANCE)

    with ThreadPoolExecutor(max_workers=1) as pool:
        while time.monotonic() < deadline:
            person = get_person_state(person_id, client=flight.client)
            feet = person["position_m"]
            if reached(_position(flight), feet):
                flight.stop()
                return person
            hover = _hover(response, feet)
            route = plan_flight_path(_position(flight), hover, cap, speed)
            pending, next_query = None, 0.0

            def progress(_distance, _world):
                nonlocal pending, next_query, person
                if pending is not None and pending.done():
                    person = pending.result()
                    pending = None
                if reached(_world, person["position_m"]):
                    raise _TargetReached()
                if pending is None and time.monotonic() >= next_query:
                    pending = pool.submit(get_person_state, person_id)
                    next_query = time.monotonic() + 0.75

            try:
                flight.fly_route(route, on_progress=progress)
            except _TargetReached:
                return person  # fly_route已制动，立即进入接管和投网对位。
            finally:
                if pending is not None:
                    pending.result()
            current = get_person_state(person_id, client=flight.client)
            if reached(_position(flight), current["position_m"]):
                flight.stop()
                return current
    raise RuntimeError("持续抵近移动人物超时")


def _phase(value):
    logger.info("持刀人员处置阶段：%s", value, extra={"response_phase": value})


def _wait(response, flight, anchor, person_id):
    deadline = time.monotonic() + 20.0
    next_query = 0.0
    while time.monotonic() < deadline:
        state = response.check(person_id, flight.name)
        if time.monotonic() >= next_query:
            get_person_state(person_id, client=flight.client)
            next_query = time.monotonic() + 1.0
        hold_position(flight.client, flight.name, flight._ned(anchor)[2], 0.25, scale=flight.frame.scale)
        if flight.client.call("isApiControlEnabled", flight.name) is not True:
            raise RuntimeError("无人机API控制权已释放")
        flight._collision()
        if math.dist(_position(flight), anchor) > 1.0:
            raise RuntimeError("无人机偏离悬停点超过1米")
        if state.get("net") == "wrapped" and state.get("act") == "lying" and not state.get("knife_in_hand"):
            return
    raise RuntimeError("等待投网倒地动画完成超时")


def respond_armed_person(drone_id, person_id, max_height, flight_speed=25.0) -> None:
    """抵近指定持刀人员，接管后一次对位并投网倒地，完成后悬停。

    person_id指定目标；水平20米内且接近离地12米处置高度时接管，停止人物巡游。
    不安排跑开跟随或固定对抗等待；保留净空、碰撞及投网动画完成确认。
    drone_id=None时自动选择最近的落地空闲机。完成后返回None，失败抛出异常。
    """
    if not isinstance(person_id, str) or not re.fullmatch(r"armed_person_[0-9]+", person_id):
        raise ValueError("目标ID应为armed_person_编号")
    cap, speed = _positive(max_height, "最大离地高度"), _positive(flight_speed, "飞行速度")
    if cap < 15.0:
        raise ValueError("最大离地高度不得低于15米")
    response_lock = vehicle_lock("__armed_response__")
    if not response_lock.acquire(blocking=False):
        raise RuntimeError("已有投网处置演示正在执行")
    try:
        with AirSimClient() as client:
            person = get_person_state(person_id, client=client)
            response = _Response(client)
            state = response.command("status")
            if state.get("person") and state.get("act") != "lying":
                raise RuntimeError("已有接管中的人物，请先结束原演示")
            if drone_id is None:
                name, lock = _reserve_nearest_UAV(person["position_m"])
            else:
                name = vehicle_name(drone_id, client.call("listVehicles"))
                lock = vehicle_lock(name)
                if not lock.acquire(blocking=False):
                    raise RuntimeError(f"{name}已有任务正在执行")
            flight, taken = None, False
            try:
                if not re.fullmatch(r"[\w.-]+", name):
                    raise ValueError("无人机名称只能包含字母、数字、下划线、点和连字符")
                logger.info("处置无人机已选定", extra={"selected_drone_id": name})
                frame = WorldFrame(client)
                world, local, origin = frame.vehicle(name)
                response.command("reset")
                hover = _hover(response, person["position_m"])
                plan_flight_path(world, hover, cap, speed)
                flight = FlightControl(client, frame, name, origin)
                flight.prepare(world, local)
                _phase("approaching")
                _rendezvous(flight, response, person_id, cap, speed)
                _external(client, person_id, True)
                taken = True
                state = response.command("takeover " + person_id)
                if not state.get("knife_in_hand"):
                    raise RuntimeError("未取得人物刀具的控制权")
                feet = get_person_state(person_id, client=client)["position_m"]
                hover = _hover(response, feet, _position(flight))
                if hover[2] - feet[2] > cap:
                    raise RuntimeError("投网悬停点超过最大离地高度")
                _approach(flight, hover, speed)
                response.command(f"kit attach {name}")
                response.command("kit strobe on")
                response.command("kit spot on")
                feet = get_person_state(person_id, client=client)["position_m"]
                yaw = math.degrees(math.atan2(feet[1] - hover[1], feet[0] - hover[0]))
                if client.call("rotateToYaw", yaw, 10.0, 5.0, name, timeout=15.0) is not True:
                    raise RuntimeError("无人机未完成朝向调整")
                _phase("netting")
                response.command("net fire")
                _wait(response, flight, hover, person_id)
                response.command("kit spot off")
                _phase("resolved")
            except BaseException:
                if flight is not None:
                    try:
                        flight.stop()
                    except Exception:
                        pass
                if taken:
                    try:
                        response.command("reset")
                        _external(client, person_id, False)
                    except Exception:
                        logger.exception("演示失败后的清理未完成")
                raise
            finally:
                lock.release()
    finally:
        response_lock.release()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--person_id",default="armed_person_01")
    parser.add_argument("--drone", help="指定无人机；省略时自动选择最近的空闲机")
    parser.add_argument("--max-height", type=float, default=60.0)
    parser.add_argument("--flight-speed", type=float, default=25.0)
    args = parser.parse_args()
    respond_armed_person(args.drone, args.person_id, args.max_height, args.flight_speed)
