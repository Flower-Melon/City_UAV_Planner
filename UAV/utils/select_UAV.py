"""按区域与 AirSim 实时状态选机；每个区域返回一架可用无人机的名称。"""

from pathlib import Path
import sys

from shapely.geometry import Point

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "UAV.utils"

from ..runtime.airsim_state import AirSimFleet
from ..runtime.flight_control import vehicle_lock
from .plan_search_path import search_scope


def select_UAV(zone_id: str) -> str:
    """读取真实区域边界和机队状态，按区域距离、中心距离、ID选机。

    仅考虑已落地、速度不超过0.1m/s、未被任务锁占用的多旋翼。
    坐标统一为UE世界米制；位于区域内的无人机区域距离为0。
    返回ID不代表已占用；执行方须在起飞前取得任务锁。
    不存在的区域或无可用无人机直接报错，不接管飞行中的无人机。
    """
    if not isinstance(zone_id, str) or not zone_id.strip() or zone_id != zone_id.strip():
        raise ValueError("zone_id必须为非空区域ID")
    geometry, _ = search_scope({"zone_id": zone_id})
    center = geometry.centroid
    candidates = []
    for drone in AirSimFleet().read():
        name = drone["name"]
        if not drone["available"] or vehicle_lock(name).locked():
            continue
        point = Point(drone["position_m"][:2])
        candidates.append((geometry.distance(point), center.distance(point), name))
    if not candidates:
        raise RuntimeError(f"区域 {zone_id} 当前没有可用无人机（需落地、静止且未被任务占用）")
    return min(candidates)[2]


def _reserve_UAV(zone_id):
    """串行完成实时选机与占用，防止多个搜索进程选择同一架。"""
    with vehicle_lock("__select_UAV__"):
        while True:
            name = select_UAV(zone_id)
            lock = vehicle_lock(name)
            if lock.acquire(blocking=False):
                return name, lock
            # 其他飞行API可能刚取得该机任务锁，重新读取状态并选机。


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="按区域ID自动选择AirSim无人机，不执行飞行")
    parser.add_argument("zone_id")
    print(select_UAV(parser.parse_args().zone_id))
