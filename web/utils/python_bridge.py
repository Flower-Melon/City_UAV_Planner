"""网页与 Python 能力之间的通信适配；每条请求只完成一个操作。"""

from contextlib import redirect_stdout
import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class PythonBridge:
    """复用模型连接，提供规划、搜索监控、病人定位和药品投递。"""

    def __init__(self):
        self.client = None
        self.planner = None
        self.vision = None

    def handle(self, request, *, on_event=None):
        if not isinstance(request, dict):
            raise ValueError("请求必须是 JSON 对象")
        operation = request.get("operation")
        if operation == "plan":
            if self.planner is None:
                from llm.utils.catalog import ZoneCatalog
                from llm.utils.client import create_client
                from llm.planner import Planner
                client, model = create_client()
                try:
                    planner = Planner(client, model, ZoneCatalog())
                except BaseException:
                    client.close()
                    raise
                self.client, self.planner = client, planner
            return self.planner.plan(request.get("instruction"), request.get("fleet"),
                                     conversation=request.get("conversation"))
        if operation == "fleet":
            from UAV.runtime.airsim_state import AirSimFleet
            return AirSimFleet().read()
        if operation == "reset":
            from UAV.API.reset_uav import reset_uav
            reset_uav()
            if request.get("reset_delivery"):
                from UAV.API.deliver_medicine import _Delivery
                from UAV.runtime.airsim_client import AirSimClient
                with AirSimClient() as client:
                    delivery = _Delivery(client)
                    state = delivery.read()
                    if state and state.get("state") != "idle":
                        delivery.command("reset")
            return {"reset": True}
        if operation == "deliver_medicine":
            from UAV.API.deliver_medicine import deliver_medicine
            if "drone_id" in request:
                raise ValueError("网页救援不接受drone_id，每次必须重新自动选机")
            flight = request.get("flight")
            if not isinstance(flight, dict) or set(flight) != {"max_height", "flight_speed"}:
                raise ValueError("药品投递需要max_height和flight_speed")
            drone_id = None
            target_position = request.get("target_position")

            class RescueHandler(logging.Handler):
                def emit(self, record):
                    nonlocal drone_id, target_position
                    selected = getattr(record, "selected_drone_id", None)
                    if selected is not None:
                        drone_id = selected
                        if on_event is not None:
                            on_event({"type": "drone_selected", "drone_id": drone_id})
                    actual = getattr(record, "delivery_position_m", None)
                    if actual is not None:
                        target_position = list(actual)
                        if on_event is not None:
                            on_event({"type": "delivery_target_selected", "target_position": target_position})

            logger = logging.getLogger(deliver_medicine.__module__)
            handler = RescueHandler()
            previous_level = logger.level
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            try:
                deliver_medicine(None, request.get("target_position"), **flight,
                                 zone_id=request.get("zone_id"), delivery_radius_m=5.0)
            finally:
                logger.removeHandler(handler)
                logger.setLevel(previous_level)
                handler.close()
            return {"delivered": True, "drone_id": drone_id, "target_position": target_position}
        if operation == "monitor":
            from UAV.API.monitor_area import monitor_area

            drone_id, position, height = None, None, None

            class MonitorHandler(logging.Handler):
                def emit(self, record):
                    nonlocal drone_id, position, height
                    selected = getattr(record, "selected_drone_id", None)
                    if selected is not None:
                        drone_id = selected
                        if on_event is not None:
                            on_event({"type": "drone_selected", "drone_id": drone_id})
                    actual = getattr(record, "monitor_position_m", None)
                    if actual is not None:
                        position, height = actual, record.monitor_height_m

            logger = logging.getLogger(monitor_area.__module__)
            handler = MonitorHandler()
            previous_level = logger.level
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            try:
                # 网页仅指定区域/复用监控机；高度使用monitor_area脚本中的默认值。
                monitor_area(request.get("zone_id"), request.get("drone_id"))
            finally:
                logger.removeHandler(handler)
                logger.setLevel(previous_level)
                handler.close()
            return {"hovering": True, "drone_id": drone_id, "position_m": position, "flight_height": height}
        if operation == "search":
            from UAV.API.search_area import search_area
            if "drone_id" in request:
                raise ValueError("搜索不接受drone_id，由执行端自动选机")
            flight = request.get("flight")
            if not isinstance(flight, dict):
                raise ValueError("搜索需要飞行参数")
            capture_directory = None
            drone_id = None

            class CaptureDirectoryHandler(logging.Handler):
                def emit(self, record):
                    nonlocal capture_directory, drone_id
                    selected = getattr(record, "selected_drone_id", None)
                    if selected is not None:
                        drone_id = selected
                        if on_event is not None:
                            on_event({"type": "drone_selected", "drone_id": drone_id})
                    directory = getattr(record, "capture_directory", None)
                    if directory is None:
                        return
                    root = Path(__file__).resolve().parents[2]
                    capture_directory = str(Path(directory).resolve().relative_to(root))
                    if on_event is not None:
                        on_event({"type": "capture_directory", "capture_directory": capture_directory})

            # 仅在本次搜索期间订阅目录事件，不向飞行API传入网页回调。
            logger = logging.getLogger(search_area.__module__)
            handler = CaptureDirectoryHandler()
            previous_level = logger.level
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            try:
                search_area(request.get("target"), **flight)
            finally:
                logger.removeHandler(handler)
                logger.setLevel(previous_level)
                handler.close()
            return {"completed": True, "capture_directory": capture_directory, "drone_id": drone_id}
        if operation in ("vision", "locate_patient", "locate_target"):
            image_path = request.get("image_path")
            if not isinstance(image_path, str) or not image_path:
                raise ValueError("视觉分析需要本次任务的图片路径")
            root = Path(__file__).resolve().parents[2]
            path = (root / image_path).resolve(strict=True)
            output_root = (root / "output").resolve()
            if (not path.is_file() or path.parent.name != "capture"
                    or path.parent.parent.parent != output_root
                    or not path.parent.parent.name.isdigit() or len(path.parent.parent.name) < 6
                    or path.suffix.lower() not in (".jpg", ".jpeg", ".png")):
                raise ValueError("仅允许分析output/编号/capture目录内的巡查图片")
            if operation in ("locate_patient", "locate_target"):
                from web.utils.patient_location import locate_target
                return {"target_position": locate_target(path, request.get("bbox"))}
            from web.utils.vision_pipeline import SearchVisionPipeline
            if self.vision is None:
                self.vision = SearchVisionPipeline()
            return self.vision(path)
        if operation == "abnormal_spawn":
            from Abnormal import spawn_vehicle, spawn_armed_person, spawn_patient, set_vehicle_behavior
            factories = {"vehicle": spawn_vehicle, "armed_person": spawn_armed_person, "patient": spawn_patient}
            kind = request["kind"]
            name = factories[kind](request["position"])
            result = {"target_id": name}
            if kind == "vehicle":
                try:
                    set_vehicle_behavior(name, request.get("behavior", "mixed"))
                except Exception as error:
                    result["warning"] = "车辆已生成，但设置行为失败：" + str(error)
            return result
        if operation == "abnormal_behavior":
            from Abnormal import set_vehicle_behavior
            set_vehicle_behavior(request["target_id"], request["behavior"])
            return {"target_id": request["target_id"]}
        if operation == "abnormal_remove":
            from Abnormal.vehicle.spawn_vehicle import AirSimClient, request as ue_request
            target_id = request["target_id"]
            with AirSimClient(timeout=45) as client:
                def removal_info():
                    return ue_request(client, "remove_info", target_id=target_id,
                                      _controller=Path(__file__).with_name("unreal_bridge.py"),
                                      _module_name="city_abnormal_web", _registry="_targets")
                # 仅移除 UE 注册表确认由异常 API 创建的对象；刀具先于人物清除。
                info = removal_info()
                for name in info["object_ids"]:
                    client.call("simDestroyObject", name, timeout=45)
                if removal_info()["object_ids"]:
                    raise RuntimeError("目标未能完全移除，请重试")
            return {"target_id": target_id, "removed": True}
        if operation in ("abnormal_state", "abnormal_frame"):
            from Abnormal.vehicle.spawn_vehicle import AirSimClient, request as ue_request
            import base64
            import io
            import tempfile
            from PIL import Image
            with AirSimClient() as client, tempfile.TemporaryDirectory(prefix="city-observer-") as directory:
                result = ue_request(client, "frame" if operation == "abnormal_frame" else "state",
                                    _controller=Path(__file__).with_name("unreal_bridge.py"),
                                    _module_name="city_abnormal_web", _registry="_targets",
                                    target_id=request.get("target_id"), watch=request.get("watch", False),
                                    directory=directory)
                if operation == "abnormal_frame":
                    with Image.open(Path(directory) / result["filename"]) as source:
                        output = io.BytesIO()
                        source.convert("RGB").save(output, format="JPEG", quality=80)
                    return {"data": base64.b64encode(output.getvalue()).decode("ascii"), "session": result["session"]}
                return result
        raise ValueError("不支持的operation")

    def serve(self, stream, output):
        try:
            for line in stream:
                request_id, operation = None, None
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise ValueError("请求必须是 JSON 对象")
                    request_id, operation = request.get("id"), request.get("operation")
                    def publish(event):
                        print(json.dumps({"id": request_id, "event": event}, ensure_ascii=False,
                                         allow_nan=False), file=output, flush=True)
                    # 能力内部的普通输出不进入 JSON 通信通道。
                    with redirect_stdout(sys.stderr):
                        result = self.handle(request, on_event=publish)
                    response = {"id": request_id, "result": result}
                except Exception as error:
                    message = str(error) if isinstance(error, (ValueError, RuntimeError, OSError)) else type(error).__name__
                    if isinstance(error, ModuleNotFoundError):
                        message = f"Python无法导入模块 {error.name}，请检查目录大小写及当前Python环境的依赖。"
                    elif operation in ("plan", "vision"):
                        try:
                            from llm.utils.client import error_message
                            message = error_message(error)
                        except ImportError:
                            # 错误格式化依赖缺失时仍返回原始错误，不能再次异常导致进程退出。
                            pass
                    response = {"id": request_id, "error": message, "error_type": type(error).__name__}
                print(json.dumps(response, ensure_ascii=False, allow_nan=False), file=output, flush=True)
        finally:
            self.close()

    def close(self):
        if self.vision is not None:
            self.vision.close()
            self.vision = None
        if self.client is not None:
            self.client.close()
            self.client = self.planner = None


if __name__ == "__main__":
    try:
        PythonBridge().serve(sys.stdin, sys.stdout)
    except BrokenPipeError:
        raise SystemExit(1)
