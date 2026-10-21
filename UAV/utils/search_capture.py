"""独立连接定时拍照、原子保存；后台识别从磁盘清单读取，不施加飞行背压。"""

from datetime import datetime, timezone
import io
import json
from pathlib import Path
import threading
import time

from PIL import Image

from .airsim_client import AirSimClient, vector
from .search_analysis import SearchAnalysis


class SearchCapture:
    # 创建独立相机连接、图片目录和后台识别对象，保存拍照配置。
    # 输入为任务目录、无人机、坐标转换和分析回调；构造线程但尚不启动。
    def __init__(self, directory, name, target, origin, scale, analyzer, interval=3.0,
                 camera="bottom_center", client_factory=None, close_analyzer=False):
        self.directory, self.name, self.target = Path(directory), name, target
        self.origin, self.scale = origin, scale
        self.interval, self.camera = interval, camera
        self.client = (client_factory or AirSimClient)(timeout=5)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.images, self.patrol_images, self.error = 0, 0, None
        self.context = {"phase": "connection", "edge_id": None, "piece_id": None}
        self.directory.joinpath("images").mkdir(parents=True, exist_ok=True)
        self.analysis = SearchAnalysis(directory, analyzer, close_analyzer=close_analyzer, lock=self.lock)
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
    # 调用相机信息和取图接口，不启动持续拍照，也不提交识别。
    def preflight(self):
        self.client.call("simGetCameraInfo", self.camera, self.name, False)
        self._image()

    # 分别启动定时拍照线程和后台识别线程。
    # 无返回值；两个线程独立工作，模型响应不会阻塞飞行循环。
    def start(self):
        self.producer.start()
        self.analysis.start()

    # 更新当前飞行航段的阶段、道路 ID 和路段 ID，供后续照片记录。
    # 输入为航段字典；只更新线程共享的拍照上下文。
    def set_leg(self, leg):
        with self.lock:
            self.context = {"phase": leg["kind"], "edge_id": leg["edge_id"], "piece_id": leg["piece_id"]}

    # 检查拍照或图片落盘是否发生需要停止搜索的错误。
    # 有拍照错误时抛出异常；不因模型缓慢或识别失败而阻塞、停止飞行。
    def check(self):
        # 模型慢、请求失败、结果写入失败均由后台报告，不改变飞行控制。
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
                        "position_note": "图像返回后采样的UE世界位置，并非异常精确位置",
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
                        self.error = "巡查相机连续取图或保存失败，停止搜索；已拍图片继续分析"
                        self.stop_event.set()
                self.stop_event.wait(max(0, self.interval-(time.monotonic()-started)))
        except Exception as error:
            self.error = f"图片保存或记录失败：{type(error).__name__}"
            self.stop_event.set()
        finally:
            self.client.close()

    # 通知相机线程停止并限时等待其退出，不等待后台图片识别。
    # 返回图片总数、巡查阶段图片数和拍照错误，供飞行结束报告使用。
    def stop_capture(self):
        """只停止相机，不等待图片识别。"""
        self.stop_event.set()
        if self.producer.ident is not None:
            self.producer.join(timeout=12)
            if self.producer.is_alive():
                self.error = "相机线程未能停止"
        else:
            self.client.close()
        return {"images": self.images, "patrol_images": self.patrol_images, "capture_error": self.error}

    # 结束拍照并把飞行结果交给后台识别，确保起飞前失败也能释放资源。
    # 输入为飞行摘要；无返回值，后续识别和最终汇总仍由后台完成。
    def finish(self, summary):
        """停止拍照并发布飞行结果，识别与最终报告由后台继续完成。"""
        summary = {**summary, **self.stop_capture()}
        # 起飞前失败也须关闭相机、释放分析器和目录锁。
        if self.analysis.thread.ident is None:
            self.analysis.start()
        self.analysis.complete_capture(summary)
