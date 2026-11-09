"""完整街区巡查、巡查阶段拍照，完成后返航至各自起飞基点并降落。"""

import io
import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from numbers import Real
from pathlib import Path
import sys

from PIL import Image
from shapely.geometry import LineString

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "UAV.API"

from ..runtime.airsim_client import AirSimClient
from ..runtime.airsim_state import WorldFrame, vector
from ..runtime.flight_control import FlightControl, ROUTE_TOLERANCE_M
from ..runtime.kill_trees import CameraTreeFilter
from ..utils.plan_flight_path import _default_graph, plan_flight_path
from ..utils.plan_search_path import plan_search_path
from ..utils.select_UAV import _reserve_UAV

DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "output" / "temp"
logger = logging.getLogger(__name__)


class SearchCapture:
    """巡查途中按累计里程下视拍照，保存原图及曝光位置。"""

    def __init__(self, directory, name, origin, scale, client_factory=None):
        self.directory, self.name = Path(directory), name
        self.origin, self.scale = origin, scale
        self.capture_directory = self.directory / "capture"
        self.capture_directory.mkdir(exist_ok=True)
        self.camera = "bottom_center"
        self.camera_fov = None
        self.client = (client_factory or AirSimClient)(timeout=15)
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

    def _image(self):
        self.tree_filter.refresh()
        responses = self.client.call(
            "simGetImages",
            [{"camera_name": self.camera, "image_type": 0, "pixels_as_float": False, "compress": True}],
            self.name,
            False,
        )
        if len(responses) != 1:
            raise RuntimeError("巡查相机未返回图片")
        response = responses[0]
        raw = response.get("image_data_uint8")
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "surrogateescape")
        if not raw or response.get("width", 0) <= 0:
            raise RuntimeError("巡查相机返回空图片")
        with Image.open(io.BytesIO(bytes(raw))) as image:
            image.load()
            image = image.convert("RGB")
            output = io.BytesIO()
            # 保留相机采集的完整分辨率，避免在目标裁剪前丢失细节。
            image.save(output, "JPEG", quality=95, subsampling=0)
        return output.getvalue(), response

    def preflight(self, *, reclaim_stale=False):
        # 起飞前仅检查下视相机存在，不取图。
        info = self.client.call("simGetCameraInfo", self.camera, self.name, False)
        self.camera_fov = float(info["fov"])
        if not math.isfinite(self.camera_fov) or not 0 < self.camera_fov < 180:
            raise RuntimeError("巡查相机视场角无效")
        result = self.tree_filter.start(reclaim_stale=reclaim_stale)
        self.log("events.json", {"type": "camera_trees_hidden", **result})

    def capture_distance(self, point, edge_id, distance, edge_distance):
        return self._capture(tuple(point), {
            "edge_id": edge_id,
            "patrol_distance_m": distance,
            "edge_distance_m": edge_distance,
            "sample_xy_m": list(point),
        })

    def _capture(self, point, sample):
        for attempt in range(2):
            try:
                image, response = self._image()
                # 飞行中使用图像自带的曝光位置，不能用编码/写盘后的遥测冒充。
                local = vector(response["camera_position"])
                position = [
                    self.origin[0] + local[0] * self.scale,
                    self.origin[1] + local[1] * self.scale,
                    self.origin[2] - local[2] * self.scale,
                ]
                if math.dist(position[:2], point) > ROUTE_TOLERANCE_M:
                    self.log("events.json", {
                        "type": "capture_skipped", "reason": "passed_sample",
                        "position_m": position, "trigger": "distance", **sample,
                    })
                    return False
                filename = f"{self.images + 1:06d}.jpg"
                path = self.capture_directory / filename
                temporary = path.with_suffix(".part")
                temporary.write_bytes(image)
                temporary.replace(path)
                frame = {
                    "image": filename,
                    "position_m": position, "edge_id": sample["edge_id"],
                    "camera_orientation_ned": response["camera_orientation"],
                    "camera_fov_deg": self.camera_fov,
                    # 纳秒时间戳保存为字符串，避免网页JSON解析损失整数精度。
                    "capture_time_ns": str(response.get("time_stamp") or ""),
                }
                frames = [*self.records.get("frame.json", []), frame]
                self.write_records(self.capture_directory / "frame.json", frames)
                self.records["frame.json"] = frames
                self.images += 1
                return True
            except Exception as error:
                self.log("events.json", {
                    "type": "capture_error", "error_type": type(error).__name__,
                    **sample, "trigger": "distance", "attempt": attempt + 1,
                })
                if attempt == 1:
                    raise RuntimeError(f"巡查拍照失败：{error}") from error

    def close(self):
        try:
            self.tree_filter.close()
        finally:
            self.client.close()


class _DistanceCapture:
    """按巡查折线累计里程取图，最多一张在途。"""

    def __init__(self, pipeline, interval):
        self.pipeline = pipeline
        self.interval = _positive(interval, "capture_interval_m")
        self.distance = 0.0
        self.next_distance = self.interval

    def fly_route(self, flight, points, edge_id):
        line = LineString([p[:2] for p in points])
        pending = None
        skipped = []

        def skip(reason, first, count):
            if count:
                skipped.append({
                    "type": "capture_skipped", "trigger": "distance", "reason": reason,
                    "edge_id": edge_id, "count": count,
                    "first_distance_m": first,
                    "last_distance_m": first + (count - 1) * self.interval,
                })

        def report(progress, _position):
            nonlocal pending
            if pending is not None and pending.done():
                pending.result()
                pending = None
            distance = self.distance + min(line.length, max(0.0, progress))
            if distance + 1e-8 < self.next_distance:
                return
            count = math.floor((distance - self.next_distance + 1e-8) / self.interval) + 1
            latest = self.next_distance + (count - 1) * self.interval
            skip("progress_jump", self.next_distance, count - 1)
            self.next_distance = latest + self.interval
            if pending is not None:
                skip("capture_busy", latest, 1)
                return
            edge_distance = max(0.0, latest - self.distance)
            point = line.interpolate(edge_distance)
            pending = executor.submit(
                self.pipeline.capture_distance, (point.x, point.y), edge_id, latest, edge_distance
            )

        try:
            # 只由拍照线程使用pipeline的RPC与记录，退出前等待在途拍照完成。
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="search-capture") as executor:
                flight.fly_route(points, on_progress=report)
                if pending is not None:
                    pending.result()
            self.distance += line.length
            # 飞控允许在端点前约1.5米结束；记录未触达的阈值，不挪到下一条边冒充补拍。
            if self.next_distance <= self.distance + 1e-8:
                count = math.floor((self.distance - self.next_distance + 1e-8) / self.interval) + 1
                skip("end_not_reached", self.next_distance, count)
                self.next_distance += count * self.interval
        finally:
            for event in skipped:
                self.pipeline.log("events.json", event)


def _patrol_chunks(points, node_positions, edge_lookup):
    """按道路拓扑分段，为累计里程采样确定道路编号。"""
    begin = 0
    for i in range(1, len(points)):
        if tuple(points[i][:2]) in node_positions:
            # 几何采样可能经过其他节点，分段须能对应原道路边。
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


def _complete_plan(segments, target, origin, home, cap, speed):
    return {
        "target": {"zone_id": target["zone_id"]},
        "waypoint_fields": ["x_m", "y_m", "z_m", "speed_mps"],
        "segments": segments,
        "home_position_m": list(origin),
        "return_path": _return_path(segments[-1][0][-1][:3], home, cap, speed),
    }


def _patrol_edge(points, edge_lookup):
    """速度采样可增加共线点；用端点及完整几何对应原道路边。"""
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
    capture_interval_m=15.0,
    output_dir=None,
) -> None:
    """三维导航接入街区、沿道路巡查拍照，完成后返航降落。

    flight_height是相对起飞地面的巡查高度；max_height是导航最大
    当地离地高度，默认60米。target仅需zone_id，由select_UAV实时选机。
    巡查途中每累计capture_interval_m米触发下视拍照，默认15米，不在路口额外拍照。
    搜索期间下视RGB相机隐藏树木，停止拍照或异常退出时清理隐藏设置。
    创建照片目录后发布包含capture_directory的日志事件，返回值为None。
    output_dir指定本次巡查的保存目录；不传时使用项目output/temp，照片写入capture子目录。
    """
    height, speed = _positive(flight_height, "flight_height"), _positive(
        flight_speed, "flight_speed"
    )
    cap = _positive(max_height, "max_height")
    interval = _positive(capture_interval_m, "capture_interval_m")
    if not isinstance(target, dict) or set(target) != {"zone_id"}:
        raise ValueError("target仅接受zone_id")
    with AirSimClient() as client:
        name, lock = _reserve_UAV(target["zone_id"])
        pipeline = flight = None
        failure = None
        try:
            logger.info("搜索无人机已选定", extra={"selected_drone_id": name})
            frame = WorldFrame(client)
            world, local, origin = frame.vehicle(name)
            landed = client.call("getMultirotorState", name)["landed_state"] == 0
            ground = frame.ground_z(world if landed else origin, name, remember=landed)
            altitude = ground + height
            # 基点地面独立测量，避免无人机在其他地点起飞时复用错误地面缓存。
            home_ground = frame.ground_z(origin)
            home = (*origin[:2], home_ground + min(height, cap))

            def make_plan(start):
                segments = plan_search_path(start, target, altitude, cap, speed)
                return _complete_plan(segments, target, origin, home, cap, speed)

            plan = make_plan(world)
            directory = DEFAULT_OUTPUT if output_dir is None else Path(output_dir)
            directory.mkdir(parents=True, exist_ok=True)
            pipeline = SearchCapture(directory, name, origin, frame.scale)
            logger.info("搜索照片目录已创建", extra={
                "capture_directory": pipeline.capture_directory.resolve(),
            })
            distance_capture = _DistanceCapture(pipeline, interval)
            # 此时已持有 _reserve_UAV 返回的跨进程独占锁，可以回收旧进程的残留会话。
            pipeline.preflight(reclaim_stale=True)
            flight = FlightControl(client, frame, name, origin)
            flight.prepare(world, local)
            actual = _world_xyz(client, name, origin, frame.scale)
            if math.dist(actual, plan["segments"][0][0][0][:3]) > 0.1:
                plan = make_plan(actual)
            plan["drone_id"] = name
            plan["capture_interval_m"] = interval
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
            for points, patrol in plan["segments"]:
                if not patrol:
                    flight.fly_route(points)
                    continue
                for chunk in _patrol_chunks(points, node_positions, edge_lookup):
                    distance_capture.fly_route(flight, chunk, _patrol_edge(chunk, edge_lookup))
            pipeline.close()
            actual = _world_xyz(client, name, origin, frame.scale)
            plan["return_path"] = _return_path(actual, home, cap, speed)
            pipeline.write_records(route_file, plan)
            flight.fly_route(plan["return_path"])
            flight.land(origin[:2])
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
    capture_interval_m = 20.0

    search_area(
        target, flight_height, flight_speed,
        capture_interval_m=capture_interval_m,
    )
