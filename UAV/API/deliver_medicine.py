"""挂载药箱，按分层飞行图飞至指定地面投递点，完成绞盘投递后悬停。"""

import json
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
from ..runtime.airsim_client import AirSimClient
from ..runtime.airsim_state import WorldFrame, editor_log, vehicle_name
from ..runtime.flight_control import FlightControl, hold_position, vehicle_lock
from ..utils.plan_flight_path import _clear_path, _positive, _xyz, plan_flight_path

_DELIVERY_LOCK = threading.Lock()  # UE 插件只有一个 medkit_01。
_HOVER_HEIGHT = 15.0
_POSITION_TOLERANCE = 0.75


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
    hold_position(flight.client, flight.name, flight._ned(anchor)[2], 0.25)
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


def deliver_medicine(drone_id, target_position, max_height, flight_speed=25.0) -> None:
    """向 UE 世界地面 XYZ（米）投递药箱；在离地15米处完成动画后悬停。

    与 fly_to_point 使用相同的四个入参，但目标 Z 表示落点地面高度。
    不推进病人事件、不切换镜头、不返航；失败时制动并抛出异常。
    """
    target = _xyz(target_position, "地面投递点")
    cap = _positive(max_height, "最大离地高度")
    speed = _positive(flight_speed, "飞行速度")
    if cap < _HOVER_HEIGHT:
        raise ValueError("最大离地高度不得低于15米")
    hover = (*target[:2], target[2] + _HOVER_HEIGHT)
    if not _DELIVERY_LOCK.acquire(blocking=False):
        raise RuntimeError("已有药品投递任务，当前插件仅支持一个药箱")
    try:
        with AirSimClient() as client:
            name = vehicle_name(drone_id, client.call("listVehicles"))
            if not re.fullmatch(r"[\w.-]+", name):
                raise ValueError("投递机名称只能包含字母、数字、下划线、点和连字符")
            lock = vehicle_lock(name)
            if not lock.acquire(blocking=False):
                raise RuntimeError(f"{name}已有任务正在执行")
            flight, delivery, attached = None, None, False
            try:
                frame = WorldFrame(client)
                world, local, origin = frame.vehicle(name)
                # 先验证目标可达，插件可用且没有正在执行的动画，再接管无人机。
                plan_flight_path(world, hover, cap, speed)
                delivery = _Delivery(client)
                state = delivery.command("status")
                if state.get("state") not in ("idle", "delivered"):
                    raise RuntimeError("药箱正在使用，请先完成原投递或执行 citypeople medkit reset")
                ground = frame.ground_z(world)
                pickup = (*world[:2], max(world[2], ground + 3.0))
                if not _clear_path([world, pickup], 1.0, 1.0):
                    raise RuntimeError("挂箱升空路径与建筑净空冲突")
                plan_flight_path(pickup, hover, cap, speed)
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
    # 修改地面投递点后，可直接运行本文件；需先在UE中进入Play。
    deliver_medicine("UAV_01", (2270.4, 121.3, 368.0), max_height=60.0, flight_speed=8.0)
