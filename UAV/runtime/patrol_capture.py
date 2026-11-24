"""按道路地面覆盖采样，取图和保存与巡查飞行并行。"""

from collections import deque
from dataclasses import dataclass
import math

from shapely.geometry import LineString
from shapely.ops import substring

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
