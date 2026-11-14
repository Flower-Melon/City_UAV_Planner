"""仿真场景交互：车辆、人员与交通探头。"""

from importlib import import_module
from functools import lru_cache
import json
from pathlib import Path

__all__ = ["spawn_vehicle", "set_vehicle_behavior", "spawn_armed_person",
           "spawn_patient", "get_probe_records"]


def __getattr__(name):
    modules = {"spawn_vehicle": "vehicle", "set_vehicle_behavior": "vehicle",
               "spawn_armed_person": "armed_person", "spawn_patient": "patient",
               "get_probe_records": "probe"}
    if name not in modules:
        raise AttributeError(name)
    value = getattr(import_module(f".{modules[name]}", __name__), name)
    globals()[name] = value
    return value


@lru_cache(maxsize=1)
def _zones():
    """场景内部共用的街区索引，首次查询时加载。"""
    from shapely.geometry import Polygon, shape
    from shapely.strtree import STRtree

    data = json.loads((Path(__file__).resolve().parents[1] / "Map/zones/functional_zones.json")
                      .read_text(encoding="utf-8"))
    parts = []
    for feature in data["features"]:
        if feature["properties"]["category"] == "transport":
            continue
        geometry = shape(feature["geometry"])
        polygons = geometry.geoms if geometry.geom_type == "MultiPolygon" else [geometry]
        parts.extend((feature, Polygon(p.exterior)) for p in polygons)
    return parts, STRtree([polygon for _, polygon in parts])


def zone_at(position):
    """内部点取所在街区，外部点取最近街区；等距时按ID确定唯一归属。"""
    from shapely.geometry import Point

    parts, index = _zones()
    matches = index.query_nearest(Point(position[:2]))
    return min((parts[int(i)][0] for i in matches), key=lambda feature: feature["id"])


def get_zone(zone_id):
    """按ID读取可生成异常目标的街区。"""
    parts, _ = _zones()
    feature = next((feature for feature, _ in parts if feature["id"] == zone_id), None)
    if feature is None:
        raise ValueError("所选街区不存在或不支持生成目标")
    return feature
