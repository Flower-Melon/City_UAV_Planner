"""无人机抵近持刀人员并执行固定拒绝分支的投网处置演示。"""

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


class _TargetMoved(Exception):
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


def _approach(flight, hover):
    flight.stop()
    actual = _position(flight)
    if math.dist(actual, hover) > 12.0 or not _clear_path([actual, hover], 1.5, 1.0):
        raise RuntimeError("未到达可安全对位的投网悬停点附近")
    timeout = max(30.0, math.dist(actual, hover) * 3.0 + 15.0)
    completed = flight.client.call(
        "moveToPosition", *flight._ned(hover), 1.0 / flight.frame.scale, timeout,
        0, {"is_rate": True, "yaw_or_rate": 0.0}, -1.0, 0.5 / flight.frame.scale, flight.name,
        timeout=timeout + 5.0,
    )
    flight.stop()
    flight._collision()
    if type(completed) is not bool or math.dist(_position(flight), hover) > 1.0:
        raise RuntimeError("无人机未完成投网悬停对位")


def _rendezvous(flight, response, person_id, cap, speed):
    """抵近巡游中的同一人物；后台查询位置，主飞行回调只读取已完成结果。"""
    person = get_person_state(person_id, client=flight.client)
    initial_distance = math.dist(_position(flight), person["position_m"])
    deadline = time.monotonic() + max(180.0, initial_distance / speed * 6.0 + 120.0)
    with ThreadPoolExecutor(max_workers=1) as pool:
        while time.monotonic() < deadline:
            person = get_person_state(person_id, client=flight.client)
            feet = person["position_m"]
            hover = _hover(response, feet)
            route = plan_flight_path(_position(flight), hover, cap, speed)
            pending, next_query = None, 0.0

            def progress(_distance, _world):
                nonlocal pending, next_query
                if pending is not None and pending.done():
                    current = pending.result()
                    pending = None
                    if (math.dist(current["position_m"][:2], feet[:2]) > 6.0
                            and math.dist(_world[:2], current["position_m"][:2]) < 50.0):
                        raise _TargetMoved()
                if pending is None and time.monotonic() >= next_query:
                    pending = pool.submit(get_person_state, person_id)
                    next_query = time.monotonic() + 0.75

            try:
                flight.fly_route(route, on_progress=progress)
            except _TargetMoved:
                continue  # fly_route已制动，从实际机位按人物新位置重新规划。
            finally:
                if pending is not None:
                    pending.result()
            current = get_person_state(person_id, client=flight.client)
            if math.dist(current["position_m"][:2], feet[:2]) <= 6.0:
                flight.stop()
                return current
    raise RuntimeError("持续抵近移动人物超时")


def _phase(value):
    logger.info("持刀人员处置阶段：%s", value, extra={"response_phase": value})


def _wait(response, flight, anchor, person_id, *, seconds=0.0, stopped=False, resolved=False):
    deadline = time.monotonic() + (30.0 if stopped else 20.0 if resolved else seconds)
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
        if stopped and state.get("act") == "threat":
            if state.get("flee_progress_m", 0) < state.get("flee_planned_m", 0) - 1.0:
                raise RuntimeError("人物未完成跑开路线")
            return
        if resolved and state.get("net") == "wrapped" and state.get("act") == "lying" and not state.get("knife_in_hand"):
            return
    if stopped or resolved:
        raise RuntimeError("等待跑开或投网倒地动画完成超时")


def _follow(flight, response, person_id, cap, speed):
    here = _position(flight)
    state = response.command(f"flee 50 4.5 {here[0]:.6f} {here[1]:.6f}")
    end = _xyz(state["flee_end_world_m"], "跑开终点")
    hover = _hover(response, end)
    duration = state["flee_planned_m"] / 4.5 + 2.0
    follow_speed = min(speed, max(2.0, min(8.0, math.dist(here[:2], hover[:2]) / duration)))
    route = plan_flight_path(here, hover, cap, follow_speed)
    flight.fly_route(route, on_progress=lambda *_: response.check(person_id, flight.name))
    _approach(flight, hover)
    _wait(response, flight, hover, person_id, stopped=True)
    return hover


def respond_armed_person(drone_id, person_id, max_height, flight_speed=25.0) -> None:
    """抵近指定持刀人员，演示威胁、跑开跟随、转身对抗和投网倒地，完成后悬停。

    person_id指定目标，全程使用其实时位置；人物动作随本次投网演示自动触发。
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
                hover = _hover(response, feet)
                flight.fly_route(plan_flight_path(_position(flight), hover, cap, min(speed, 5.0)))
                _approach(flight, hover)
                response.command(f"kit attach {name}")
                response.command("kit strobe on")
                response.command("kit spot on")
                _phase("threat")
                response.command(f"threat {hover[0]:.6f} {hover[1]:.6f}")
                _wait(response, flight, hover, person_id, seconds=3.0)
                _phase("following")
                hover = _follow(flight, response, person_id, cap, speed)
                _phase("confronting")
                response.command(f"threat {hover[0]:.6f} {hover[1]:.6f}")
                feet = get_person_state(person_id, client=client)["position_m"]
                yaw = math.degrees(math.atan2(feet[1] - hover[1], feet[0] - hover[0]))
                if client.call("rotateToYaw", yaw, 10.0, 5.0, name, timeout=15.0) is not True:
                    raise RuntimeError("无人机未完成朝向调整")
                _wait(response, flight, hover, person_id, seconds=2.0)
                _phase("netting")
                response.command("net fire")
                _wait(response, flight, hover, person_id, resolved=True)
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
