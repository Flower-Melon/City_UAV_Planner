"""仿真场景交互：车辆、人员与交通探头。"""

from importlib import import_module

__all__ = ["spawn_vehicle", "set_vehicle_behavior", "spawn_armed_person", "spawn_patient", "ProbeMonitor"]


def __getattr__(name):
    modules = {"spawn_vehicle": "vehicle", "set_vehicle_behavior": "vehicle",
               "spawn_armed_person": "armed_person", "spawn_patient": "patient",
               "ProbeMonitor": "probe"}
    if name not in modules:
        raise AttributeError(name)
    value = getattr(import_module(f".{modules[name]}", __name__), name)
    globals()[name] = value
    return value
