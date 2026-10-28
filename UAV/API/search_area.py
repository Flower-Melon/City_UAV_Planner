"""完整街区巡查、巡查阶段拍照，完成后返航至各自起飞基点并降落。"""

import io
import json
import math
from numbers import Real
from pathlib import Path
import sys

from PIL import Image
from shapely.geometry import LineString

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "UAV.API"

from ..runtime.airsim_client import AirSimClient
from ..runtime.airsim_state import WorldFrame, vector, vehicle_name
from ..runtime.flight_control import FlightControl, settle_at_position, vehicle_lock
from ..utils.plan_flight_path import _default_graph, plan_flight_path
from ..utils.plan_search_path import check_building_clearance, plan_search_path

DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "output"


def _quaternion(value):
    q = tuple(float(value[k]) for k in ("w_val", "x_val", "y_val", "z_val"))
    norm = math.hypot(*q)
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("相机姿态无效")
    return tuple(v / norm for v in q)


def _multiply(a, b):
    w, x, y, z = a
    v, i, j, k = b
    return (
        w * v - x * i - y * j - z * k,
        w * i + x * v + y * k - z * j,
        w * j - x * k + y * v + z * i,
        w * k + x * j - y * i + z * v,
    )


def _inverse(q):
    return (q[0], -q[1], -q[2], -q[3])


def _camera_pose(position, orientation):
    return {
        "position": dict(zip(("x_val", "y_val", "z_val"), position)),
        "orientation": dict(zip(("w_val", "x_val", "y_val", "z_val"), orientation)),
    }


def new_capture_directory(output_dir):
    """每次搜索分配一个 output/编号/ 任务目录，支持并发创建。"""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    number = (
        max((int(p.name) for p in output.iterdir() if p.is_dir() and p.name.isdecimal()), default=0)
        + 1
    )
    while True:
        directory = output / f"{number:06d}"
        try:
            directory.mkdir()
            return directory
        except FileExistsError:
            number += 1


class SearchCapture:
    """节点下视拍照，进入巡查边前沿边拍照；节点与边分别去重。"""

    def __init__(self, directory, name, origin, scale, client_factory=None):
        self.directory, self.name = Path(directory), name
        self.origin, self.scale = origin, scale
        self.capture_directory = self.directory / "capture"
        self.capture_directory.mkdir()
        self.camera = "bottom_center"
        self.street_camera = "front_center"
        self.client = (client_factory or AirSimClient)(timeout=5)
        self.images = 0
        self.records = {}
        self.captured_nodes = set()
        self.captured_edges = set()

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

    def _image(self, camera):
        responses = self.client.call(
            "simGetImages",
            [{"camera_name": camera, "image_type": 0, "pixels_as_float": False, "compress": True}],
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
            image.thumbnail((1600, 1200))
            output = io.BytesIO()
            image.save(output, "JPEG", quality=90)
        return output.getvalue()

    def preflight(self):
        # 起飞前仅检查相机存在，不取图。
        self.client.call("simGetCameraInfo", self.camera, self.name, False)
        self.client.call("simGetCameraInfo", self.street_camera, self.name, False)

    def _aim_street(self, points):
        """以边的首段为方向斜下45度取景，返回原相机的相对姿态供拍完恢复。"""
        original = self.client.call("simGetCameraInfo", self.street_camera, self.name, False)[
            "pose"
        ]
        self.client.call(
            "simSetCameraPose",
            self.street_camera,
            _camera_pose((0, 0, 0), (1, 0, 0, 0)),
            self.name,
            False,
        )
        mount = self.client.call("simGetCameraInfo", self.street_camera, self.name, False)["pose"]
        inverse = _inverse(_quaternion(mount["orientation"]))
        offset = tuple(
            a - b for a, b in zip(vector(original["position"]), vector(mount["position"]))
        )
        relative_position = _multiply(_multiply(inverse, (0, *offset)), _inverse(inverse))[1:]
        restore = _camera_pose(
            relative_position, _multiply(inverse, _quaternion(original["orientation"]))
        )
        dx, dy = (points[1][i] - points[0][i] for i in (0, 1))
        yaw, pitch = math.atan2(dy, dx) / 2, math.radians(-45) / 2
        world_rotation = (
            math.cos(yaw) * math.cos(pitch),
            -math.sin(yaw) * math.sin(pitch),
            math.cos(yaw) * math.sin(pitch),
            math.sin(yaw) * math.cos(pitch),
        )
        try:
            self.client.call(
                "simSetCameraPose",
                self.street_camera,
                _camera_pose(relative_position, _multiply(inverse, world_rotation)),
                self.name,
                False,
            )
        except BaseException:
            self.client.call("simSetCameraPose", self.street_camera, restore, self.name, False)
            raise
        return restore

    def capture_node(self, point):
        node = tuple(point)
        if node in self.captured_nodes:
            return False
        self._capture(node, self.camera, "node")
        self.captured_nodes.add(node)
        return True

    def capture_edge(self, points, edge_id):
        if edge_id in self.captured_edges:
            return False
        self._capture(tuple(points[0]), self.street_camera, "edge", (edge_id, points))
        self.captured_edges.add(edge_id)
        return True

    def _capture(self, node, camera, trigger, edge=None):
        for attempt in range(2):
            try:
                restore = self._aim_street(edge[1]) if edge is not None else None
                try:
                    image = self._image(camera)
                finally:
                    if restore is not None:
                        self.client.call("simSetCameraPose", camera, restore, self.name, False)
                local = vector(self.client.call("simGetVehiclePose", self.name)["position"])
                position = [
                    self.origin[0] + local[0] * self.scale,
                    self.origin[1] + local[1] * self.scale,
                    self.origin[2] - local[2] * self.scale,
                ]
                if math.dist(position[:2], node) > 1.0:
                    raise RuntimeError("拍照时未稳定在目标巡查节点")
                filename = f"{self.images + 1:06d}.jpg"
                path = self.capture_directory / filename
                temporary = path.with_suffix(".part")
                temporary.write_bytes(image)
                temporary.replace(path)
                frame = {
                    "image": filename,
                    "type": trigger,
                    "position_m": position,
                    "node_xy_m": list(node),
                }
                if edge is not None:
                    frame["edge_id"] = edge[0]
                frames = [*self.records.get("frame.json", []), frame]
                self.write_records(self.capture_directory / "frame.json", frames)
                self.records["frame.json"] = frames
                self.images += 1
                return True
            except Exception as error:
                self.log(
                    "events.json",
                    {
                        "type": "capture_error",
                        "error_type": type(error).__name__,
                        "node_xy_m": list(node),
                        "trigger": trigger,
                        "attempt": attempt + 1,
                    },
                )
                if attempt == 1:
                    raise RuntimeError(f"巡查节点拍照失败：{error}") from error

    def close(self):
        self.client.close()


def _patrol_chunks(points, node_positions, edge_lookup):
    """只在真实拓扑节点分段，保留弯道内部几何点而不把它们当成拍照节点。"""
    begin = 0
    if tuple(points[0][:2]) in node_positions:
        yield [points[0]], tuple(points[0][:2])
    for i in range(1, len(points)):
        if tuple(points[i][:2]) in node_positions:
            # 采样点碰巧经过其他节点时，不新增原道路图中不存在的拓扑路口。
            try:
                _patrol_edge(points[begin : i + 1], edge_lookup)
            except RuntimeError:
                continue
            yield points[begin : i + 1], tuple(points[i][:2])
            begin = i
    if begin < len(points) - 1:
        yield points[begin:], None


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
    raise RuntimeError("巡查路径与道路拓扑边不一致，无法确定街道拍照方向")


def search_area(
    drone_id,
    target,
    flight_height,
    flight_speed=5.0,
    *,
    max_height=60.0,
    output_dir=DEFAULT_OUTPUT,
) -> None:
    """三维导航接入街区、沿道路巡查拍照，完成后返航降落。

    flight_height是相对起飞地面的巡查高度；max_height是导航最大
    当地离地高度，默认60米。target仅需zone_id。节点和道路边分别只拍一次。
    """
    height, speed = _positive(flight_height, "flight_height"), _positive(
        flight_speed, "flight_speed"
    )
    cap = _positive(max_height, "max_height")
    with AirSimClient() as client:
        name = vehicle_name(drone_id, client.call("listVehicles"))
        lock = vehicle_lock(name)
        if not lock.acquire(blocking=False):
            raise RuntimeError(f"{name}已有任务正在执行")
        pipeline = flight = None
        failure = None
        try:
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
            directory = new_capture_directory(output_dir)
            pipeline = SearchCapture(directory, name, origin, frame.scale)
            pipeline.preflight()
            flight = FlightControl(client, frame, name, origin)
            flight.prepare(world, local)
            actual = _world_xyz(client, name, origin, frame.scale)
            if math.dist(actual, plan["segments"][0][0][0][:3]) > 0.1:
                plan = make_plan(actual)
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
                chunks = list(_patrol_chunks(points, node_positions, edge_lookup))
                for offset, (chunk, node) in enumerate(chunks):
                    if len(chunk) > 1:
                        flight.fly_route(chunk)
                    upcoming = (
                        chunks[offset + 1][0]
                        if node is not None and offset + 1 < len(chunks)
                        else None
                    )
                    edge_id = _patrol_edge(upcoming, edge_lookup) if upcoming else None
                    if node is not None and (
                        node not in pipeline.captured_nodes
                        or edge_id is not None
                        and edge_id not in pipeline.captured_edges
                    ):
                        flight.stop()
                        destination = [(node[i] - origin[i]) / frame.scale for i in (0, 1)] + [
                            (origin[2] - chunk[-1][2]) / frame.scale
                        ]
                        settle_at_position(client, name, destination, frame.scale)
                        pipeline.capture_node(node)
                        if edge_id is not None:
                            pipeline.capture_edge([p[:2] for p in upcoming], edge_id)
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
    drone_id = "UAV_01"
    target = {"zone_id": "F-UAV-BASE"}
    flight_height = 20.0
    flight_speed = 5.0
    output_dir = DEFAULT_OUTPUT

    search_area(drone_id, target, flight_height, flight_speed, output_dir=output_dir)
