"""无人机快照校验、来源选择与只读AirSim位置查询。"""

import json
import math
import time
from pathlib import Path

from UAV.utils.airsim_client import AirSimClient, WorldFrame, vector


# 校验无人机名称、可用状态和 UE 世界米制位置，统一字段并按名称排序。
# 输入为非空无人机列表；返回规范化的新列表，无效数据抛出异常。
def normalize_fleet(fleet):
    if not isinstance(fleet, list) or not fleet:
        raise ValueError("请提供非空无人机列表")
    result, names = [], set()
    for drone in fleet:
        if not isinstance(drone, dict):
            raise ValueError("每架无人机应为对象")
        name = drone.get("name")
        if not isinstance(name, str) or not name.strip() or name != name.strip() or name in names:
            raise ValueError("无人机 name 必须非空、无首尾空格且不重复")
        if type(drone.get("available")) is not bool:
            raise ValueError(f"{name} 必须明确设置 available=true/false")
        position = drone.get("position_m")
        if position is not None and (
            not isinstance(position, list) or len(position) != 3
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in position)
        ):
            raise ValueError(f"{name} 的 position_m 必须是 UE 世界米制 [X,Y,Z] 或 null")
        names.add(name)
        result.append({"name": name, "available": drone["available"], "position_m": position})
    return sorted(result, key=lambda d: d["name"])


# 检查网页监控快照时间，仅接受最近 2 秒且不是未来时间的状态。
# 输入为带采样时间的快照；返回规范化无人机列表，过期或无时间戳时返回 None。
def snapshot_fleet(snapshot):
    if isinstance(snapshot, dict) and type(snapshot.get("sampled_at")) in (int, float):
        age = time.time() * 1000 - snapshot["sampled_at"]
        if 0 <= age <= 2000:
            return normalize_fleet(snapshot.get("drones"))
    return None


# 按文件、手工 ID、已校验监控快照、实时 AirSim 的顺序选择状态来源。
# 返回本次无人机列表；手工 ID 的位置记为未知，不用出生点代替当前位置。
def load_fleet(*, fleet_file=None, drones=None, live_fleet=None):
    """按显式文件、手工ID、监控快照、实时AirSim的优先级读取。"""
    if fleet_file:
        return normalize_fleet(json.loads(Path(fleet_file).read_text(encoding="utf-8-sig")))
    if drones:
        return normalize_fleet([{"name": name, "available": True, "position_m": None}
                                for name in drones])
    if live_fleet is not None:
        return live_fleet
    return read_live_fleet()


# 只读查询 AirSim 中的真实多旋翼位置、落地状态和速度。
# 返回无人机列表；当前规则仅把已落地且基本静止的无人机标记为可用。
def read_live_fleet():
    """当前位置使用 UE 世界米制坐标，不使用配置中的出生点。"""
    try:
        with AirSimClient() as rpc:
            settings = json.loads(rpc.call("getSettingsString"))
            configured = settings.get("Vehicles", {})
            names = [name for name in rpc.call("listVehicles")
                     if configured.get(name, {}).get("VehicleType", "SimpleFlight")
                     in ("SimpleFlight", "PX4Multirotor")]
            if not names:
                raise RuntimeError("AirSim 当前没有运行中的多旋翼无人机。")

            frame = WorldFrame(rpc)
            fleet = []
            for name in names:
                position, _, _ = frame.vehicle(name)
                state = rpc.call("getMultirotorState", name)
                landed = state.get("landed_state")
                if type(landed) is not int or landed not in (0, 1):
                    raise RuntimeError(f"无法确认 {name} 的落地状态，未生成指令。")
                motion = rpc.call("simGetGroundTruthKinematics", name)
                speed_m_s = math.hypot(*vector(motion["linear_velocity"])) * frame.scale
                # 初版的候选规则，不把在线直接当成空闲；不接管飞行或任务锁。
                fleet.append({"name": name, "position_m": position,
                              "available": landed == 0 and speed_m_s <= 0.1})
            return normalize_fleet(fleet)
    except OSError as error:
        raise RuntimeError("无法读取 AirSim（127.0.0.1:41451），请确认仿真已启动且 RPC 可连接。") from error
