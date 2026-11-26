"""完整街区巡查、巡查阶段拍照，完成后返航至各自起飞基点并降落。"""

import io
import json
import logging
import math
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from numbers import Real
from pathlib import Path
import sys
import time

from PIL import Image
from shapely.geometry import LineString
from shapely.ops import substring

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "UAV.API"

from ..runtime.airsim_client import AirSimClient
from ..runtime.airsim_state import WorldFrame, vector
from ..runtime.flight_control import FlightControl
from ..runtime.kill_trees import CameraTreeFilter
from ..utils.plan_flight_path import _default_graph, _flight_graph, plan_flight_path
from ..utils.plan_search_path import plan_search_path
from ..utils.select_UAV import _reserve_UAV

DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "output" / "temp"
logger = logging.getLogger(__name__)

OVERLAP = 0.15
# 覆盖范围留出飞控到位误差，以及地图和相机浮点误差。
_POSITION_MARGIN_M = 0.25
_GROUND_SAMPLE_M = 5.0
_SAVE_BUFFER = 2
_CAMERA_TIME_MARGIN = 1.15


@dataclass(frozen=True)
class CameraFrame:
    raw: bytes
    position: tuple
    orientation: dict
    width: int
    height: int
    seconds: float
    people: tuple = ()

    def ground_radius(self, fov, ground_z):
        """下视画面在水平地面上的保守内切半径，适用于任意航向。"""
        w, x, y, z = (self.orientation[key + "_val"] for key in "wxyz")
        norm = math.hypot(w, x, y, z)
        if not math.isfinite(norm) or norm < 1e-8:
            raise RuntimeError("巡查相机姿态无效")
        down = 2 * (x * z - w * y) / norm ** 2
        tilt = math.acos(max(-1.0, min(1.0, down)))
        short_half_angle = math.atan(
            math.tan(math.radians(fov) / 2) * min(1.0, self.height / self.width)
        )
        angle = short_half_angle - tilt
        height = self.position[2] - ground_z
        if height <= 0 or angle <= 0:
            raise RuntimeError("下视相机无法覆盖无人机正下方的道路")
        return height * math.tan(angle)


class SearchCapture:
    """巡查途中按累计里程下视拍照，保存原图及曝光位置。"""

    def __init__(self, directory, name, origin, scale, client_factory=None, *, object_offset=None):
        self.directory, self.name = Path(directory), name
        self.origin, self.scale = origin, scale
        self.capture_directory = self.directory / "capture"
        self.capture_directory.mkdir(exist_ok=True)
        self.camera = "bottom_center"
        self.camera_fov = None
        self.client = (client_factory or AirSimClient)(timeout=15)
        self.object_offset = object_offset
        self.people_client = (client_factory or AirSimClient)(timeout=0.15) if object_offset is not None else None
        self.tree_filter = CameraTreeFilter(self.client, name)
        self.images = 0
        self.records = {}

    def write_records(self, path, records):
        temporary = path.with_suffix(".part")
        temporary.write_text(
            json.dumps(records, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def log(self, filename, value):
        records = [*self.records.get(filename, []), value]
        self.write_records(self.directory / filename, records)
        self.records[filename] = records

    def person_positions(self, names=None):
        """通过独立的轻量 RPC 读取仿真人物中心，失效时继续保存照片。"""
        if self.people_client is None:
            return {}
        deadline = time.monotonic() + 0.15
        try:
            if names is None:
                names = self.people_client.call("simListSceneObjects", r"^(patient|armed_person)_[0-9]+$")
            positions = {}
            for name in names:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {}
                xyz = vector(self.people_client.call("simGetObjectPose", name, timeout=remaining)["position"])
                positions[name] = tuple(a + b * self.scale for a, b in zip(
                    self.object_offset, (xyz[0], xyz[1], -xyz[2])))
            return positions
        except (OSError, RuntimeError, ValueError, KeyError):
            return {}

    def acquire(self):
        started = time.monotonic()
        self.tree_filter.refresh()
        people_before = self.person_positions()
        exposure_started = time.monotonic()
        responses = self.client.call(
            "simGetImages",
            [{"camera_name": self.camera, "image_type": 0, "pixels_as_float": False, "compress": True}],
            self.name,
            False,
        )
        people_after = self.person_positions(people_before) if people_before else {}
        people = ()
        if time.monotonic() - exposure_started <= 1.25 and people_before.keys() == people_after.keys():
            people = tuple({"person_id": name,
                            "center_m": [(a + b) / 2 for a, b in zip(before, people_after[name])],
                            "movement_m": math.dist(before, people_after[name])}
                           for name, before in people_before.items())
        if len(responses) != 1:
            raise RuntimeError("巡查相机未返回图片")
        response = responses[0]
        raw = response.get("image_data_uint8")
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "surrogateescape")
        width, height = response.get("width", 0), response.get("height", 0)
        if not raw or width <= 0 or height <= 0:
            raise RuntimeError("巡查相机返回空图片")
        local = vector(response["camera_position"])
        position = (
            self.origin[0] + local[0] * self.scale,
            self.origin[1] + local[1] * self.scale,
            self.origin[2] - local[2] * self.scale,
        )
        return CameraFrame(bytes(raw), position, response["camera_orientation"], width, height,
                           time.monotonic() - started, people)

    def save(self, frame, sample):
        with Image.open(io.BytesIO(frame.raw)) as image:
            image.load()
            image = image.convert("RGB")
            output = io.BytesIO()
            # 保留相机采集的完整分辨率，避免在目标裁剪前丢失细节。
            image.save(output, "JPEG", quality=95, subsampling=0)
        filename = f"{self.images + 1:06d}.jpg"
        path = self.capture_directory / filename
        temporary = path.with_suffix(".part")
        temporary.write_bytes(output.getvalue())
        temporary.replace(path)
        record = {
            "image": filename, "position_m": frame.position, "edge_id": sample["edge_id"],
            "camera_orientation_ned": frame.orientation, "camera_fov_deg": self.camera_fov,
            "people_at_capture": frame.people,
            "patrol_distance_m": sample["patrol_distance_m"], "sample_xy_m": sample["sample_xy_m"],
        }
        records = [*self.records.get("frame.json", []), record]
        self.write_records(self.capture_directory / "frame.json", records)
        self.records["frame.json"] = records
        self.images += 1
        logger.info("巡查照片已保存", extra={"search_frame_saved": True})

    def preflight(self, *, reclaim_stale=False):
        # 用起飞前的一帧实测分辨率、挂载偏移和取图耗时。
        info = self.client.call("simGetCameraInfo", self.camera, self.name, False)
        self.camera_fov = float(info["fov"])
        if not math.isfinite(self.camera_fov) or not 0 < self.camera_fov < 180:
            raise RuntimeError("巡查相机视场角无效")
        result = self.tree_filter.start(reclaim_stale=reclaim_stale)
        self.log("events.json", {"type": "camera_trees_hidden", **result})
        return self.acquire()

    def close(self):
        try:
            self.tree_filter.close()
        finally:
            self.client.close()
            if self.people_client is not None:
                self.people_client.close()


class PatrolSampler:
    def __init__(self, pipeline, camera, drone_position, ground_at, camera_worker, writer):
        self.pipeline = pipeline
        self.camera = camera
        self.camera_offset = math.dist(camera.position[:2], drone_position[:2])
        self.camera_offset_z = camera.position[2] - drone_position[2]
        self.ground_at = ground_at
        self.camera_worker, self.writer = camera_worker, writer
        self.camera_seconds = max(0.01, camera.seconds)
        self.saves = deque()
        self.covered_edges = set()
        self.distance = 0.0

    def _drain_saves(self):
        while self.saves and (self.saves[0].done() or len(self.saves) >= _SAVE_BUFFER):
            self.saves.popleft().result()

    def finish(self):
        while self.saves:
            self.saves.popleft().result()

    def fly_route(self, flight, points, edge_id):
        line = LineString([point[:2] for point in points])
        length = line.length
        altitude, configured_speed = points[0][2:]
        if length <= 1e-8 or edge_id in self.covered_edges:
            flight.fly_route(points)
            self.distance += length
            return

        ground_samples = max(1, math.ceil(length / _GROUND_SAMPLE_M))
        ground_z = max(self.ground_at(line.interpolate(length * index / ground_samples).coords[0])
                       for index in range(ground_samples + 1))
        # 把该边的最高地面折算为初始测量时等效的地面，复用相机姿态和视场。
        equivalent_ground = ground_z + self.camera.position[2] - (altitude + self.camera_offset_z)
        radius = self.camera.ground_radius(self.pipeline.camera_fov, equivalent_ground)
        radius -= self.camera_offset + _POSITION_MARGIN_M
        if radius <= 0:
            raise RuntimeError(f"{edge_id} 的下视覆盖不足，请检查相机或提高巡查高度")
        spacing = 2 * radius * (1 - OVERLAP)
        samples = max(1, math.ceil(length / spacing))
        step = length / samples
        # 每张负责一个等长的小段，起点、终点和转弯都落在某张照片负责的范围内。
        window = min(radius - step / 2, step / 4)
        cursor = 0.0

        def advance(distance, speed):
            nonlocal cursor
            if distance > cursor + 1e-8:
                part = substring(line, cursor, distance)
                flight.fly_route([(*xy, altitude, speed) for xy in part.coords])
                cursor = distance

        for index in range(samples):
            center = (index + 0.5) * step
            point = line.interpolate(center).coords[0]
            speed = min(configured_speed, 2 * window / (self.camera_seconds * _CAMERA_TIME_MARGIN))
            advance(center - window, speed)
            self._drain_saves()
            pending = self.camera_worker.submit(self.pipeline.acquire)
            advance(center + window, speed)
            sample = {"edge_id": edge_id, "patrol_distance_m": self.distance + center,
                      "edge_distance_m": center, "sample_xy_m": list(point)}
            try:
                frame = pending.result()
                self._check_coverage(frame, point, ground_z, step)
            except Exception as error:
                self.pipeline.log("events.json", {
                    "type": "capture_retry", "error": str(error), **sample,
                })
                # 到采样窗口末端仍有问题时，悬停重新取一帧。
                frame = self.pipeline.acquire()
                self._check_coverage(frame, point, ground_z, step)
            self.camera_seconds = max(frame.seconds, self.camera_seconds * 0.9, 0.01)
            self.saves.append(self.writer.submit(self.pipeline.save, frame, sample))
        advance(length, speed)
        self.covered_edges.add(edge_id)
        self.distance += length

    def _check_coverage(self, frame, center, ground_z, step):
        radius = frame.ground_radius(self.pipeline.camera_fov, ground_z)
        required = step / 2 + math.dist(frame.position[:2], center)
        if radius < required:
            raise RuntimeError(f"曝光画面覆盖不足，所需半径 {required:.2f} 米，实际 {radius:.2f} 米")


def _patrol_chunks(points, node_positions, edge_lookup):
    """按道路拓扑分段，为累计里程采样确定道路编号。"""
    begin = 0
    for i in range(1, len(points)):
        if tuple(points[i][:2]) in node_positions:
            # 道路线形可能经过其他节点，分段须能对应原道路边。
            try:
                _patrol_edge(points[begin : i + 1], edge_lookup)
            except RuntimeError:
                continue
            yield points[begin : i + 1]
            begin = i
    if begin < len(points) - 1:
        yield points[begin:]


def _positive(value, name):
    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name}必须为正的有限数值")
    return float(value)


def _world_xyz(client, name, origin, scale):
    local = vector(client.call("simGetVehiclePose", name)["position"])
    return (
        origin[0] + local[0] * scale,
        origin[1] + local[1] * scale,
        origin[2] - local[2] * scale,
    )


def _return_path(start, home, cap, speed):
    return plan_flight_path(start, home, cap, speed)


def _complete_plan(segments, target, base, home, cap, speed):
    return {
        "target": {"zone_id": target["zone_id"]},
        "waypoint_fields": ["x_m", "y_m", "z_m", "speed_mps"],
        "segments": segments,
        "home_position_m": list(base),
        "return_path": _return_path(segments[-1][0][-1][:3], home, cap, speed),
    }


def _patrol_edge(points, edge_lookup):
    """用端点及完整几何对应原道路边。"""
    xy = [tuple(p[:2]) for p in points]
    line = LineString(xy)
    for edge_id, geometry in edge_lookup.get((xy[0], xy[-1]), ()):
        if abs(line.length - geometry.length) < 1e-6 and line.hausdorff_distance(geometry) < 1e-6:
            return edge_id
    raise RuntimeError("巡查路径与道路拓扑边不一致，无法确定道路编号")


def search_area(
    target,
    flight_height,
    flight_speed=30.0,
    *,
    max_height=60.0,
    output_dir=None,
    by_drone=None,
) -> None:
    """三维导航接入街区、沿道路巡查拍照，完成后返航降落。

    flight_height是相对起飞地面的巡查高度；max_height是导航最大
    当地离地高度，默认60米。target仅需zone_id，由select_UAV实时选机。
    巡查拍照间距按地面覆盖自动计算。
    搜索期间下视RGB相机隐藏树木，停止拍照或异常退出时清理隐藏设置。
    创建照片目录后发布包含capture_directory的日志事件，返回值为None。
    output_dir指定本次巡查的保存目录；不传时使用项目output/temp，照片写入capture子目录。
    """
    height, speed = _positive(flight_height, "flight_height"), _positive(
        flight_speed, "flight_speed"
    )
    cap = _positive(max_height, "max_height")
    if not isinstance(target, dict) or set(target) != {"zone_id"}:
        raise ValueError("target仅接受zone_id")
    with AirSimClient() as client:
        name, lock = _reserve_UAV(target["zone_id"])
        pipeline = flight = None
        failure = None
        try:
            profile = (by_drone or {}).get(name, {})
            height = _positive(profile.get("flight_height", height), "flight_height")
            speed = _positive(profile.get("flight_speed", speed), "flight_speed")
            cap = _positive(profile.get("max_height", cap), "max_height")
            logger.info("搜索无人机已选定", extra={"selected_drone_id": name})
            frame = WorldFrame(client)
            world, _, origin = frame.vehicle(name)
            # 选机时已确认无人机在基地待命。
            ground = frame.ground_z(world, name, remember=True)
            altitude = ground + height
            base = frame.home(name)
            home = (*base[:2], ground + min(height, cap))

            def make_plan(start):
                segments = plan_search_path(start, target, altitude, cap, speed)
                return _complete_plan(segments, target, base, home, cap, speed)

            plan = make_plan(world)
            directory = DEFAULT_OUTPUT if output_dir is None else Path(output_dir)
            directory.mkdir(parents=True, exist_ok=True)
            pipeline = SearchCapture(directory, name, origin, frame.scale, object_offset=frame.world_offset())
            logger.info("搜索照片目录已创建", extra={
                "capture_directory": pipeline.capture_directory.resolve(),
            })
            # 此时已持有 _reserve_UAV 返回的跨进程独占锁，可以回收旧进程的残留会话。
            camera = pipeline.preflight(reclaim_stale=True)
            flight = FlightControl(client, frame, name, origin)
            flight.prepare()
            actual = _world_xyz(client, name, origin, frame.scale)
            if math.dist(actual, plan["segments"][0][0][0][:3]) > 0.1:
                plan = make_plan(actual)
            plan["drone_id"] = name
            plan["capture_profile"] = {
                "camera_fov_deg": pipeline.camera_fov, "image_size": [camera.width, camera.height],
                "target_overlap": OVERLAP, "initial_acquire_seconds": camera.seconds,
            }
            route_file = pipeline.capture_directory / "route.json"
            pipeline.write_records(route_file, plan)

            graph = _default_graph()
            node_positions = frozenset(graph.node_points)
            edge_lookup = {}
            for edge, edge_id in zip(graph.edges, graph.edge_ids):
                for points in (edge.points, tuple(reversed(edge.points))):
                    edge_lookup.setdefault((points[0], points[-1]), []).append(
                        (edge_id, LineString(points))
                    )
            with ThreadPoolExecutor(1, thread_name_prefix="search-camera") as grabber, \
                    ThreadPoolExecutor(1, thread_name_prefix="search-writer") as writer:
                sampler = PatrolSampler(pipeline, camera, world, _flight_graph().ground_near,
                                        grabber, writer)
                searching = False
                for points, patrol in plan["segments"]:
                    if not patrol:
                        flight.fly_route(points)
                        continue
                    for chunk in _patrol_chunks(points, node_positions, edge_lookup):
                        if not searching:
                            searching = True
                            logger.info("无人机开始区域搜索", extra={"search_started": True})
                        sampler.fly_route(flight, chunk, _patrol_edge(chunk, edge_lookup))
                sampler.finish()
            pipeline.close()
            actual = _world_xyz(client, name, origin, frame.scale)
            plan["return_path"] = _return_path(actual, home, cap, speed)
            pipeline.write_records(route_file, plan)
            flight.fly_route(plan["return_path"])
            flight.land(base[:2], speed=min(2.0, speed))
            if pipeline.images == 0:
                raise RuntimeError("巡查未保存任何照片，本次搜索未完成")
        except BaseException as error:
            failure = error
            if pipeline is not None:
                try:
                    pipeline.log("events.json", {
                        "type": "flight_error", "error": str(error),
                        "diagnostics": flight.diagnostics if flight is not None else {},
                    })
                except Exception:
                    pass
        finally:
            try:
                if pipeline is not None:
                    try:
                        pipeline.close()
                    except Exception as error:
                        if failure is None:
                            failure = error
                if flight is not None:
                    try:
                        flight.stop()
                    except Exception as error:
                        if failure is None:
                            failure = error
            finally:
                lock.release()
        if failure is not None:
            raise failure


if __name__ == "__main__":
    # 在此修改参数后，可直接运行本文件或设置断点调试。
    target = {"zone_id": "F-SPORTS-2"}
    flight_height = 15.0
    flight_speed = 50.0
    search_area(target, flight_height, flight_speed)
