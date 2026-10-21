"""只读原始地图；按条件筛选真实区域，几何数据始终留在本地。"""

from collections import Counter
from math import hypot
import json
from pathlib import Path

EPS = 1e-8

MAP_PATH = Path(__file__).resolve().parents[2] / "Map" / "functional_zones.json"


class Clarification(ValueError):
    # 封装需要用户补充的信息，并保留候选区域供上层展示。
    # 输入为原因和候选集合；创建异常对象，不执行查询。
    def __init__(self, message, candidates=()):
        super().__init__(message)
        self.candidates = list(candidates)


class ZoneCatalog:
    # 读取本地功能区地图，校验坐标制并建立类别、区域摘要和几何索引。
    # 输入为地图路径；数据保存在实例中，不连接模型或 AirSim。
    def __init__(self, path=MAP_PATH):
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        metadata = data["coordinate_metadata"]
        if metadata.get("system") != "UE_WORLD_METRES" or metadata.get("unit") != "m":
            raise ValueError("地图必须使用 UE_WORLD_METRES，单位米")
        self.bounds = metadata["bounds_m"]
        self.zones, self._geometries = {}, {}
        self.categories = data["categories"]
        self.category_names = {c["code"]: c["name"] for c in self.categories}
        for feature in data["features"]:
            props, zone_id = feature["properties"], feature["id"]
            if zone_id in self.zones:
                raise ValueError(f"重复区域 ID：{zone_id}")
            self.zones[zone_id] = {
                "zone_id": zone_id, "name": props["name"], "category": props["category"],
                "subtype": props.get("subtype"), "object_type": props.get("object_type"),
                "host_region_id": props.get("host_region_id") or props.get("parent_id"),
                "label_xy_m": props.get("label_xy_m"),
            }
            self._geometries[zone_id] = feature["geometry"]

    # 生成类别统计、区域四字段摘要和地图方向约定。
    # 返回完整摘要目录；规划器再筛选首次发送内容，此处不包含几何坐标。
    def overview(self):
        """发送全部区域和设施的四字段目录，几何和位置仅保留在本地。"""
        counts = Counter(z["category"] for z in self.zones.values())
        return {"categories": [{"code": c["code"], "name": c["name"],
                                "subtypes": c.get("subtypes", []), "count": counts[c["code"]]}
                               for c in self.categories],
                "zones": [{"zone_id": z["zone_id"], "name": z["name"],
                           "category": z["category"], "subtype": z["subtype"]}
                          for z in self.zones.values()],
                "direction_convention": "地图右东(+X)、上北(-Y)；不是地理真北"}

    # 将地图、无人机或区域参照解析成空间计算使用的点和边界。
    # 返回参照点、可选区域几何及 ID；缺失、重名或无位置时抛出澄清异常。
    def _anchor(self, reference, fleet):
        if reference is None:
            raise Clarification("请明确空间参照：地图中心、哪架无人机，或哪个区域？")
        if reference == "map":
            x0, y0, x1, y1 = self.bounds
            return [(x0+x1)/2, (y0+y1)/2], None, None
        drones = [d for d in fleet if d["name"] == reference]
        matches = [z for z in self.zones.values()
                   if z["zone_id"] == reference or z["name"] == reference]
        if drones and matches:
            raise Clarification(f"参照“{reference}”同时匹配无人机和区域，请使用明确的区域 ID 或调整无人机名称。")
        if drones:
            if drones[0]["position_m"] is None:
                raise Clarification(f"缺少 {reference} 的当前位置，请提供 UE 世界米制坐标。")
            return drones[0]["position_m"][:2], None, None
        if len(matches) != 1:
            raise Clarification(f"参照“{reference}”不存在或不唯一，请提供无人机 ID 或真实区域 ID。", matches)
        zone = matches[0]
        if zone["label_xy_m"] is None:
            raise Clarification(f"参照区域 {zone['zone_id']} 缺少代表位置。")
        return zone["label_xy_m"], self._geometries[zone["zone_id"]], zone["zone_id"]

    # 按名称、类别、方向和距离筛选真实区域，并处理最近、全部与唯一选择。
    # 输入为目标条件和无人机快照；返回区域摘要，无匹配或歧义时要求补充。
    def query(self, target, fleet):
        if target.category and target.category not in self.category_names:
            raise Clarification(f"地图没有类别 {target.category}，请使用实际类别。")
        if target.zone_id and target.zone_id not in self.zones:
            raise Clarification(f"地图中不存在区域 ID：{target.zone_id}")
        matches = [dict(z) for z in self.zones.values()
                   if (not target.category or z["category"] == target.category)
                   and (not target.zone_id or z["zone_id"] == target.zone_id)
                   and (not target.name or target.name.casefold() in z["name"].casefold())
                   and (not target.subtype or target.subtype == z["subtype"])]
        spatial = target.direction != "none" or target.radius_m is not None or target.selection == "nearest"
        if spatial:
            point, anchor_geometry, anchor_id = self._anchor(target.reference, fleet)
            filtered = []
            for zone in matches:
                if zone["zone_id"] == anchor_id:
                    continue
                label = zone["label_xy_m"]
                if target.direction != "none" and label is None:
                    raise Clarification(f"区域 {zone['zone_id']} 缺少方向判断所需的代表位置。")
                if target.direction != "none" and not in_direction(label, point, target.direction):
                    continue
                if target.radius_m is not None or target.selection == "nearest":
                    geometry = self._geometries[zone["zone_id"]]
                    distance = (area_distance(anchor_geometry, geometry) if anchor_geometry
                                else point_distance(point, geometry))
                    if target.radius_m is not None and distance > target.radius_m + EPS:
                        continue
                    zone["distance_m"] = round(distance, 6)
                    zone["_distance"] = distance
                filtered.append(zone)
            matches = filtered
        if not matches:
            raise Clarification("没有符合全部条件的区域，请调整名称、方向或距离条件。")
        matches.sort(key=lambda z: (z.get("_distance", 0), z["zone_id"]))
        if target.selection == "nearest":
            tied = [z for z in matches if abs(z["_distance"]-matches[0]["_distance"]) <= EPS]
            if len(tied) > 1:
                raise Clarification("最近区域存在并列，请指定其中一个区域。", self._clean(tied))
            matches = matches[:1]
        elif target.selection == "single" and len(matches) > 1:
            raise Clarification("目标不唯一，请指定区域，或明确搜索全部区域。", self._clean(matches))
        if target.scope == "nearby":
            for zone in matches:
                zone.update(scope="nearby", buffer_m=target.buffer_m)
        return self._clean(matches)

    # 去掉候选区域中仅供内部排序使用的临时字段。
    # 输入为区域列表；返回新的摘要列表，不修改原字典。
    @staticmethod
    def _clean(zones):
        return [{k: v for k, v in z.items() if not k.startswith("_")} for z in zones]


# 将 Polygon 与 MultiPolygon 统一成可逐个处理的多边形列表。
# 输入为 GeoJSON 几何；返回含内外环的列表，不支持的类型抛出异常。
def polygons(geometry):
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"]]
    if geometry["type"] == "MultiPolygon":
        return geometry["coordinates"]
    raise ValueError("区域必须使用 Polygon 或 MultiPolygon")


# 遍历轮廓的相邻点，并补上末点到首点的闭合线段。
# 输入为点列表；返回线段端点对的迭代器。
def segments(ring):
    return zip(ring, ring[1:] + ring[:1])


# 用投影计算点到有限线段的最短距离，兼容零长度线段。
# 输入为同一米制平面上的点和两个端点；返回非负距离。
def point_segment_distance(p, a, b):
    dx, dy = b[0]-a[0], b[1]-a[1]
    length2 = dx*dx + dy*dy
    t = 0 if length2 == 0 else max(0, min(1, ((p[0]-a[0])*dx + (p[1]-a[1])*dy)/length2))
    return hypot(p[0]-a[0]-t*dx, p[1]-a[1]-t*dy)


# 通过边界距离和射线交点判断点在单个轮廓中的位置。
# 输入为点和轮廓；返回 -1（外部）、0（边界）或 1（内部）。
def ring_location(point, ring):
    """-1 外部、0 边界、1 内部。"""
    inside = False
    x, y = point[:2]
    for a, b in segments(ring):
        if point_segment_distance(point, a, b) <= EPS:
            return 0
        if (a[1] > y) != (b[1] > y):
            crossing_x = (b[0]-a[0])*(y-a[1])/(b[1]-a[1]) + a[0]
            if x < crossing_x:
                inside = not inside
    return 1 if inside else -1


# 判断点是否落在多边形有效范围内，排除内环孔洞内部。
# 输入为点和含外环、内环的多边形；返回布尔值，边界计入范围。
def contains(point, polygon):
    if ring_location(point, polygon[0]) == -1:
        return False
    return not any(ring_location(point, ring) == 1 for ring in polygon[1:])


# 计算点到区域有效范围的最短距离，支持多块区域和孔洞。
# 输入为点和 GeoJSON 几何；区域内返回 0，否则返回边界距离。
def point_distance(point, geometry):
    parts = polygons(geometry)
    if any(contains(point, part) for part in parts):
        return 0.0
    return min(point_segment_distance(point, a, b)
               for part in parts for ring in part for a, b in segments(ring))


# 计算二维叉积，判断第三个点相对有向线段的方位。
# 输入为三个点；返回带符号数值，供线段相交判断使用。
def cross(a, b, c):
    return (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])


# 判断两线段是否相交，并计算它们的最短距离。
# 输入为四个端点；相交或接触时返回 0，否则返回端点到线段的最小距离。
def segment_distance(a, b, c, d):
    if cross(a, b, c)*cross(a, b, d) < 0 and cross(c, d, a)*cross(c, d, b) < 0:
        return 0.0
    return min(point_segment_distance(a, c, d), point_segment_distance(b, c, d),
               point_segment_distance(c, a, b), point_segment_distance(d, a, b))


# 计算两个区域之间的最短边界距离，兼容包含、相交及多块区域。
# 输入为两个 GeoJSON 几何；重叠时返回 0，否则返回最短米数。
def area_distance(left, right):
    best = float("inf")
    for a in polygons(left):
        for b in polygons(right):
            if contains(a[0][0], b) or contains(b[0][0], a):
                return 0.0
            for ar in a:
                for br in b:
                    for a1, a2 in segments(ar):
                        for b1, b2 in segments(br):
                            best = min(best, segment_distance(a1, a2, b1, b2))
                            if best <= EPS:
                                return 0.0
    return best


# 按地图右东、上北的约定判断目标相对参照点的方向。
# 输入为目标点、参照点和方向名称；返回是否满足方向条件。
def in_direction(point, reference, direction):
    """地图约定右东(+X)、上北(-Y)，四向半平面，斜向象限。"""
    dx, dy = point[0]-reference[0], point[1]-reference[1]
    checks = {"east": dx > EPS, "west": dx < -EPS,
              "north": dy < -EPS, "south": dy > EPS}
    checks.update({"northeast": checks["north"] and checks["east"],
                   "northwest": checks["north"] and checks["west"],
                   "southeast": checks["south"] and checks["east"],
                   "southwest": checks["south"] and checks["west"], "none": True})
    return checks[direction]
