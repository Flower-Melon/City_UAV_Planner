"""可由实验脚本独立控制的异常目标。"""

from .vehicle import set_vehicle_behavior, spawn_vehicle
from .armed_person import spawn_armed_person
from .patient import spawn_patient

__all__ = ["spawn_vehicle", "set_vehicle_behavior", "spawn_armed_person", "spawn_patient"]
