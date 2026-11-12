"""向指定病人投递药品，完成绞盘投递后悬停。"""

import json
import logging
import math
from pathlib import Path
import re
import sys
import threading
import time

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "UAV.API"

from .fly_to_point import _world_position
from web.utils.people_state import get_person_state
from ..runtime.airsim_client import AirSimClient
from ..runtime.airsim_state import WorldFrame, editor_log, vehicle_name
from ..runtime.flight_control import FlightControl, hold_position, vehicle_lock
from ..utils.plan_flight_path import NoPathError, _clear_path, _flight_graph, _positive, _xyz, plan_flight_path
from ..utils.select_UAV import _reserve_nearest_UAV

_DELIVERY_LOCK = threading.Lock()  # UE 插件只有一个 medkit_01。
_HOVER_HEIGHT = 15.0
_DELIVERY_RADIUS = 5.0
_POSITION_TOLERANCE = 0.75
logger = logging.getLogger(__name__)


class _Delivery:
    """通过增量包的命令序号和状态文件确认动画执行结果。"""

    def __init__(self, client):
        self.client = client
        self.path = editor_log().parents[1] / "MedicalDelivery" / "latest_state.json"
        self.sequence = None

    def read(self):
        try:
            state = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if isinstance(state, dict) and state.get("schema_version") == "city_medical_delivery_v1":
                return state
        except (OSError, ValueError):
            pass  # UE 直接覆盖写文件，可能短暂读到不完整 JSON。
        return {}

    def command(self, command):
        before = self.read().get("command_seq")
        if self.client.call("simRunConsoleCommand", "citypeople medkit " + command) is not True:
            raise RuntimeError(f"UE未执行药箱命令：{command}")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            state = self.read()
            if state.get("command_seq") != before and state.get("last_command") == command:
                if state.get("last_result") != "accepted":
                    raise RuntimeError(f"药箱命令被拒绝：{state.get('last_error') or command}")
                self.sequence = state["command_seq"]
                return state
            time.sleep(0.05)
        raise RuntimeError("未收到新的药箱回执，请确认投递插件已加载且当前地图支持 citypeople medkit")

    def wait(self, wanted, flight, anchor, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.read()
            if state:
                observing = state.get("last_command") == "status" or state.get("last_command", "").startswith("view ")
                if state.get("drone") != flight.name or (state.get("command_seq") != self.sequence and not observing):
                    raise RuntimeError("药箱动画被其他命令或无人机接管")
                if state.get("last_error") or state.get("state") == "idle":
                    raise RuntimeError(f"药箱动画中断：{state.get('last_error') or 'idle'}")
            _hold(flight, anchor)
            if state.get("state") == wanted:
                return state
        raise RuntimeError(f"等待药箱状态 {wanted} 超时，当前状态：{self.read().get('state', '无回执')}")


def _position(flight):
    return _world_position(flight.client, flight.name, flight.origin, flight.frame.scale)


def _hold(flight, anchor):
    # 动画期间持续维持高度，避免沿用先前的移动指令。
    hold_position(flight.client, flight.name, flight._ned(anchor)[2], 0.25, scale=flight.frame.scale)
    if flight.client.call("isApiControlEnabled", flight.name) is not True:
        raise RuntimeError("投递中断：无人机API控制权已释放")
    flight._collision()
    actual = _position(flight)
    if math.dist(actual[:2], anchor[:2]) > _POSITION_TOLERANCE or abs(actual[2] - anchor[2]) > _POSITION_TOLERANCE:
        raise RuntimeError("挂箱或投递时无人机偏离悬停点超过0.75米")


def _align(flight, target, approach):
    """沿规划的最后一段原地下降并低速对位，不跨越未检查的障碍。"""
    flight.stop()
    actual = _position(flight)
    if math.dist(actual[:2], target[:2]) > 5.0 or abs(actual[2] - approach[2]) > 1.5:
        raise RuntimeError("无人机未到达投递点附近，停止投递")
    if not _clear_path([actual, target], 1.0, 1.0):
        raise RuntimeError("投递末端对位路径与建筑净空冲突")
    ned = flight._ned(target)
    timeout = max(30.0, math.dist(actual, target) * 3.0 + 15.0)
    completed = flight.client.call(
        "moveToPosition", *ned, 1.0 / flight.frame.scale, timeout,
        0, {"is_rate": True, "yaw_or_rate": 0.0}, -1.0, 1.0, flight.name,
        timeout=timeout + 5.0,
    )
    # SimpleFlight 到达后也可能返回 False，按实际位置、控制权及碰撞核验。
    if type(completed) is not bool:
        raise RuntimeError("无人机投递末端对位返回值无效")
    flight.stop()
    _hold(flight, target)


def _delivery_site(frame, start, requested, cap, speed, radius):
    """优先精确落点，再由近到远寻找同一地面附近可达的开阔投递点。"""
    rules = _flight_graph().rules
    horizontal = rules["gap_margin_m"] + rules["drone_radius_m"]
    vertical = rules["vertical_margin_m"]
    offsets = [(0.0, 0.0)]
    if radius > 0:
        # 半米起步，避免病人仅因靠墙而被迫大幅偏移；所有点仍受原净空约束。
        distances = sorted({min(radius, distance) for distance in (0.5, 1.0, 2.0, 3.0, radius)})
        for distance in distances:
            offsets.extend((distance * math.cos(i * math.pi / 8), distance * math.sin(i * math.pi / 8))
                           for i in range(16))
    for dx, dy in offsets:
        x, y = requested[0] + dx, requested[1] + dy
        estimate = (x, y, requested[2])
        # 先用已有地面高度剔除明显贴墙的候选，减少UE地面查询。
        if not _clear_path([estimate, (x, y, requested[2] + _HOVER_HEIGHT)], horizontal, vertical):
            continue
        try:
            ground = requested[2] if dx == dy == 0 else frame.ground_z(estimate)
        except RuntimeError:
            # 单个候选没有可测地面时，继续检查同一半径内的其他落点。
            continue
        # 相邻楼顶或低处道路不能当作病人附近的同一投递地面。
        if abs(ground - requested[2]) > 1.0:
            continue
        target, hover = (x, y, ground), (x, y, ground + _HOVER_HEIGHT)
        if not _clear_path([target, hover], horizontal, vertical):
            continue
        try:
            plan_flight_path(start, hover, cap, speed)
        except NoPathError:
            continue
        return target, hover
    scope = "精确落点" if radius == 0 else f"病人周围{radius:g}米内"
    raise NoPathError(f"{scope}没有满足建筑净空、地面高差及最大高度{cap:g}米限制的可达投递点")


def deliver_medicine(drone_id, person_id, max_height, flight_speed=25.0) -> None:
    """向指定病人投递药箱；在离地15米处完成动画后悬停。

    person_id指定病人，落点按其实时地面位置确定，必要时在周围5米内选择可达落点。
    drone_id=None时自动选择离病人最近的落地空闲机。
    不推进病人事件、不切换镜头、不返航；失败时制动并抛出异常。
    """
    if not isinstance(person_id, str) or not re.fullmatch(r"patient_[0-9]+", person_id):
        raise ValueError("病人ID应为patient_编号")
    cap = _positive(max_height, "最大离地高度")
    speed = _positive(flight_speed, "飞行速度")
    if cap < _HOVER_HEIGHT:
        raise ValueError("最大离地高度不得低于15米")
    if not _DELIVERY_LOCK.acquire(blocking=False):
        raise RuntimeError("已有药品投递任务，当前插件仅支持一个药箱")
    try:
        with AirSimClient() as client:
            person = get_person_state(person_id, client=client)
            if person["kind"] != "patient":
                raise ValueError("药品投递目标必须是病人")
            requested = _xyz(person["position_m"], "病人真实位置")
            logger.info("病人实时位置已确定", extra={"patient_position_m": list(requested)})
            if drone_id is None:
                name, lock = _reserve_nearest_UAV(requested)
            else:
                name = vehicle_name(drone_id, client.call("listVehicles"))
                lock = vehicle_lock(name)
                if not lock.acquire(blocking=False):
                    raise RuntimeError(f"{name}已有任务正在执行")
            flight, delivery, attached = None, None, False
            try:
                if not re.fullmatch(r"[\w.-]+", name):
                    raise ValueError("投递机名称只能包含字母、数字、下划线、点和连字符")
                logger.info("救援无人机已选定", extra={"selected_drone_id": name})
                frame = WorldFrame(client)
                world, local, origin = frame.vehicle(name)
                # 先确认挂箱升空段及投递点可达，再接管无人机。
                ground = frame.ground_z(world)
                pickup = (*world[:2], max(world[2], ground + 3.0))
                if not _clear_path([world, pickup], 1.0, 1.0):
                    raise RuntimeError("挂箱升空路径与建筑净空冲突")
                target, hover = _delivery_site(frame, pickup, requested, cap, speed, _DELIVERY_RADIUS)
                logger.info("药品投递落点已确定", extra={
                    "delivery_position_m": list(target), "requested_position_m": list(requested),
                })
                delivery = _Delivery(client)
                state = delivery.command("status")
                if state.get("state") not in ("idle", "delivered"):
                    raise RuntimeError("药箱正在使用，请先完成原投递或执行 citypeople medkit reset")
                flight = FlightControl(client, frame, name, origin)
                flight.prepare(world, local)
                flight._vertical(flight._ned(pickup)[2], min(speed, 2.0))
                flight.stop()
                pickup = _position(flight)
                delivery.command(f"attach {name}")
                attached = True
                delivery.wait("carrying", flight, pickup, max(40.0, (pickup[2] - ground) * 2.0 + 20.0))
                # 挂箱期间实际位置可能改变，从当前坐标重新规划最短代价路线。
                route = plan_flight_path(_position(flight), hover, cap, speed)
                approach = len(route) - 1
                while approach > 0 and math.dist(route[approach - 1][:2], hover[:2]) < 1e-8:
                    approach -= 1
                # 最终竖直段保持规划坐标，用低速位置控制完成下降和投递对位。
                flight.fly_route(route[:approach + 1])
                _align(flight, hover, route[approach])
                current = get_person_state(person_id, client=client)
                if math.dist(current["position_m"], requested) > 1.0:
                    raise RuntimeError("病人已离开投递点，停止投递")
                delivery.command(f"drop {target[2]:.6f}")
                delivery.wait("delivered", flight, hover, 90.0)
            except BaseException:
                if flight is not None:
                    try:
                        flight.stop()
                    except Exception:
                        pass
                if attached:
                    try:
                        state = delivery.read()
                        if state.get("drone") == name and state.get("state") != "delivered":
                            delivery.command("reset")
                    except Exception:
                        pass
                raise
            finally:
                lock.release()
    finally:
        _DELIVERY_LOCK.release()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--person_id", help="病人ID，例如patient_01",default="patient_01")
    parser.add_argument("--drone", help="指定无人机；省略时自动选择离病人最近的空闲机")
    parser.add_argument("--max-height", type=float, default=60.0)
    parser.add_argument("--flight-speed", type=float, default=25.0)
    args = parser.parse_args()
    deliver_medicine(args.drone, args.person_id, args.max_height, args.flight_speed)
