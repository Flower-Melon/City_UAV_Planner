"""同步完成沿路巡查与定时拍照，结束后悬停。"""

from datetime import datetime, timezone
import io
import json
import math
from numbers import Real
from pathlib import Path
import sys
import threading
import time
import uuid

from PIL import Image

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "UAV.API"

from ..utils.airsim_client import AirSimClient, SearchFlight, WorldFrame, vector, vehicle_lock, vehicle_name
from ..utils.plan_search_path import check_building_clearance, plan_search_path

DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "search_results"


class SearchCapture:
    # 创建独立相机连接和图片目录，保存拍照配置。
    # 输入为任务目录、无人机和坐标转换；构造线程但尚不启动。
    def __init__(self, directory, name, target, origin, scale, interval=3.0,
                 client_factory=None):
        self.directory, self.name, self.target = Path(directory), name, target
        self.origin, self.scale = origin, scale
        self.interval, self.camera = interval, "bottom_center"
        self.client = (client_factory or AirSimClient)(timeout=5)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.images, self.patrol_images, self.error = 0, 0, None
        self.context = {"phase": "connection", "edge_id": None, "piece_id": None}
        self.directory.joinpath("images").mkdir(parents=True, exist_ok=True)
        self.producer = threading.Thread(target=self._capture_loop, daemon=True, name="search-camera")

    # 在共享锁保护下追加一条任务 JSONL 日志。
    # 输入为文件名和记录对象；写入当前任务目录，无返回值。
    def log(self, filename, value):
        with self.lock:
            with self.directory.joinpath(filename).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False)+"\n")

    # 从巡查相机取图，验证有效性后转为最长不超过 1600×1200 的 JPEG。
    # 返回图片字节、AirSim 原始响应和实际尺寸；取图失败抛出异常。
    def _image(self):
        responses = self.client.call("simGetImages", [{"camera_name": self.camera, "image_type": 0,
            "pixels_as_float": False, "compress": True}], self.name, False)
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
            size = image.size
        return output.getvalue(), response, size

    # 在起飞前确认指定相机存在且能返回有效图像。
    # 调用相机信息和取图接口，不启动持续拍照。
    def preflight(self):
        self.client.call("simGetCameraInfo", self.camera, self.name, False)
        self._image()

    # 启动定时拍照线程。
    # 无返回值；拍照与飞行独立进行。
    def start(self):
        self.producer.start()

    # 更新当前飞行航段的阶段、道路 ID 和路段 ID，供后续照片记录。
    # 输入为航段字典；只更新线程共享的拍照上下文。
    def set_leg(self, leg):
        with self.lock:
            self.context = {"phase": leg["kind"], "edge_id": leg["edge_id"], "piece_id": leg["piece_id"]}

    # 检查拍照或图片落盘是否发生需要停止搜索的错误。
    # 有拍照错误时抛出异常。
    def check(self):
        if self.error:
            raise RuntimeError(self.error)

    # 按设定间隔取图并原子落盘，记录时间、观察位置和所处航段。
    # 作为相机线程入口；连续两次取图或保存失败时记录错误并停止拍照。
    def _capture_loop(self):
        consecutive_errors = 0
        try:
            while not self.stop_event.is_set():
                started = time.monotonic()
                try:
                    with self.lock:
                        context = dict(self.context)
                    image, response, size = self._image()
                    local = vector(self.client.call("simGetVehiclePose", self.name)["position"])
                    position = [self.origin[0]+local[0]*self.scale,
                                self.origin[1]+local[1]*self.scale,
                                self.origin[2]-local[2]*self.scale]
                    frame_id = f"{self.images+1:06d}"
                    filename = f"images/{frame_id}.jpg"
                    path = self.directory/filename
                    temporary = path.with_suffix(".part")
                    temporary.write_bytes(image)
                    temporary.replace(path)
                    metadata = {"frame_id": frame_id, "image": filename, "drone_id": self.name,
                        "target": self.target, "captured_at_utc": datetime.now(timezone.utc).isoformat(),
                        "width": size[0], "height": size[1],
                        "airsim_image_timestamp": response.get("time_stamp"), "position_m": position,
                        "position_note": "图像返回后采样的UE世界位置",
                        "camera": self.camera, "camera_position_ned": response.get("camera_position"),
                        "camera_orientation_ned": response.get("camera_orientation"), **context}
                    self.log("frames.jsonl", metadata)
                    self.images += 1
                    self.patrol_images += context["phase"] == "patrol"
                    consecutive_errors = 0
                except Exception as error:
                    consecutive_errors += 1
                    self.log("events.jsonl", {"type": "capture_error", "error_type": type(error).__name__,
                                              "consecutive": consecutive_errors})
                    if consecutive_errors >= 2:
                        self.error = "巡查相机连续取图或保存失败，停止搜索"
                        self.stop_event.set()
                self.stop_event.wait(max(0, self.interval-(time.monotonic()-started)))
        except Exception as error:
            self.error = f"图片保存或记录失败：{type(error).__name__}"
            self.stop_event.set()
        finally:
            self.client.close()

    # 通知相机线程停止并限时等待其退出。
    # 返回图片总数、巡查阶段图片数和拍照错误。
    def stop_capture(self):
        """停止拍照并关闭相机连接。"""
        self.stop_event.set()
        if self.producer.ident is not None:
            self.producer.join(timeout=12)
            if self.producer.is_alive():
                self.error = "相机线程未能停止"
        else:
            self.client.close()
        return {"images": self.images, "patrol_images": self.patrol_images, "capture_error": self.error}


# 检查高度、速度或拍照间隔是否为正的有限数值。
# 输入为数值和参数名；返回 float，无效时抛出 ValueError。
def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name}必须为正的有限数值")
    return float(value)


# 串联沿路搜索：规划路线、检查高度、控制飞行，并启动独立拍照线程。
# 输入为无人机、目标区域及飞行参数；飞行及拍照结束返回 None，失败抛出异常。
def search_area(drone_id, target, flight_height, flight_speed=5.0, *, capture_interval=3.0,
           output_dir=DEFAULT_OUTPUT) -> None:
    """同步完成飞行与拍照，悬停后返回None。

    target接受SearchArea.target：zone_id，buffer_m(周边默认50米)。
    巡查区域边界外环带内道路；连接路段可在范围外。
    固定使用bottom_center相机，保存巡查照片和飞行记录。
    固定高度需足够净空。
    """
    height = _positive(flight_height, "flight_height")
    speed = _positive(flight_speed, "flight_speed")
    interval = _positive(capture_interval, "capture_interval")
    with AirSimClient() as client:
        name = vehicle_name(drone_id, client.call("listVehicles"))
        lock = vehicle_lock(name)
        if not lock.acquire(blocking=False):
            raise RuntimeError(f"{name}已有任务正在执行")
        pipeline = flight = None
        failure = None
        try:
            directory = Path(output_dir)/datetime.now().strftime("%Y%m%d-%H%M%S")/uuid.uuid4().hex[:10]/name
            directory.mkdir(parents=True, exist_ok=False)
            frame = WorldFrame(client)
            world, local, origin = frame.vehicle(name)
            plan = plan_search_path(world[:2], target)
            directory.joinpath("plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
            pipeline = SearchCapture(directory, name, plan["target"], origin, frame.scale, interval)
            pipeline.preflight()
            flight = SearchFlight(client, frame, name, origin)
            flight.prepare(world, local, height, lambda altitude: check_building_clearance(plan, altitude))
            # 升高后检查实际接入位置；漂移后重新规划，不能忽略当前位置与首路点的偏差。
            current = vector(client.call("simGetVehiclePose", name)["position"])
            actual = [origin[i]+current[i]*frame.scale for i in (0, 1)]
            if math.dist(actual, plan["legs"][0]["points"][0]) > .5:
                plan = plan_search_path(actual, target)
                check_building_clearance(plan, origin[2]-flight.z*frame.scale)
                directory.joinpath("plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
            pipeline.start()

            # 将飞行到达检查后的当前位置写入本次任务进度日志。
            # 输入为 UE 世界 XY 米制位置；只保存记录，不改变飞行。
            def observe(position):
                pipeline.log("progress.jsonl", {"position_xy_m": position, "observed_at_utc": datetime.now(timezone.utc).isoformat()})

            for leg in plan["legs"]:
                pipeline.set_leg(leg)
                pipeline.check()
                flight.fly(leg["points"], speed, pipeline.check, observe)
                pipeline.log("progress.jsonl", {"completed_leg": leg["piece_id"], "kind": leg["kind"]})
            pipeline.check()
        except BaseException as error:
            failure = error
        finally:
            try:
                if flight is not None:
                    try:
                        flight.stop()
                    except Exception as error:
                        if failure is None:
                            failure = error
                if pipeline is not None:
                    try:
                        pipeline.stop_capture()
                        pipeline.check()
                    except Exception as error:
                        if failure is None:
                            failure = error
            finally:
                lock.release()
        if failure is not None:
            raise failure


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="实际执行指定区域的沿路巡查与拍照")
    parser.add_argument("zone_id")
    parser.add_argument("--drone", default="UAV_01")
    parser.add_argument("--height", required=True, type=float)
    parser.add_argument("--speed", type=float, default=5)
    parser.add_argument("--nearby", type=float, default=50, help="边界外周边米数，默认50米")
    parser.add_argument("--interval", type=float, default=3)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    target = {"zone_id": args.zone_id, "buffer_m": args.nearby}
    search_area(args.drone, target, args.height, args.speed, capture_interval=args.interval, output_dir=args.output)
