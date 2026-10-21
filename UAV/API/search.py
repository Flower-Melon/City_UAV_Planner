"""同步完成沿路巡查与拍照，识别独立在后台继续；不等待模型返回。"""

from datetime import datetime, timezone
import json
import math
from numbers import Real
from pathlib import Path
import sys
import uuid

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "UAV.API"

from ..utils.airsim_client import AirSimClient, WorldFrame, vector, vehicle_lock, vehicle_name
from ..utils.flight_control import SearchFlight
from ..utils.plan_search_path import check_building_clearance, plan_search_path
from ..utils.search_capture import SearchCapture
from ..utils.search_analysis import write_summary

DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "search_results"


# 检查高度、速度或拍照间隔是否为正的有限数值。
# 输入为数值和参数名；返回 float，无效时抛出 ValueError。
def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name}必须为正的有限数值")
    return float(value)


# 串联沿路搜索：规划路线、检查高度、控制飞行，并启动独立拍照和识别线程。
# 输入为无人机、目标区域及飞行参数；飞行结束返回 None，后台识别可继续，失败抛出异常。
def search(drone_id, target, flight_height, flight_speed=5.0, *, capture_interval=3.0,
           output_dir=DEFAULT_OUTPUT, analyze_image=None, camera="bottom_center",
           stop_event=None) -> None:
    """和fly_to_point一样同步完成飞行并返回None；图片识别不阻塞返回或下一次飞行。

    target接受SearchArea.target：zone_id，scope(zone/nearby)，buffer_m(周边默认50米)。
    普通区域巡查内部道路，nearby巡查边界外环带内道路；连接路段可在范围外。
    analyze_image(path, metadata)返回status与description；省略时使用llm.VisionAnalyzer。
    stop_event为可选threading.Event；异常只记录，暂不自动处置。固定高度需足够净空。
    """
    height = _positive(flight_height, "flight_height")
    speed = _positive(flight_speed, "flight_speed")
    interval = _positive(capture_interval, "capture_interval")
    if not isinstance(camera, str) or not camera.strip() or camera == "back_center":
        raise ValueError("请选择独立巡查相机，不能使用网页back_center")
    if analyze_image is not None and not callable(analyze_image):
        raise ValueError("analyze_image必须为可调用函数")
    owned_analyzer = None
    with AirSimClient() as client:
        name = vehicle_name(drone_id, client.call("listVehicles"))
        lock = vehicle_lock(name)
        if not lock.acquire(blocking=False):
            raise RuntimeError(f"{name}已有任务正在执行")
        directory = None
        pipeline = flight = None
        summary = {"drone_id": name, "status": "failed", "completed_piece_ids": [],
                   "flight_completed": False, "started_at_utc": datetime.now(timezone.utc).isoformat(),
                   "settings": {"flight_height_m": height, "flight_speed_m_s": speed,
                                "capture_interval_s": interval, "camera": camera}}
        failure = None
        try:
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError("搜索已取消")
            task_directory = Path(output_dir)/datetime.now().strftime("%Y%m%d-%H%M%S")/uuid.uuid4().hex[:10]/name
            task_directory.mkdir(parents=True, exist_ok=False)
            directory = task_directory
            if analyze_image is None:
                from llm.vision import VisionAnalyzer
                owned_analyzer = VisionAnalyzer()
                analyze_image = owned_analyzer
            summary["vision_model"] = getattr(analyze_image, "model", "custom_callback")
            frame = WorldFrame(client)
            world, local, origin = frame.vehicle(name)
            plan = plan_search_path(world[:2], target)
            summary["target"] = plan["target"]
            summary["target_piece_ids"] = plan["target_piece_ids"]
            summary["planned_distance_m"] = plan["distance_m"]
            directory.joinpath("plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
            pipeline = SearchCapture(directory, name, plan["target"], origin, frame.scale, analyze_image,
                                     interval, camera, close_analyzer=owned_analyzer is not None)
            write_summary(directory, {**summary, "status": "running", "flight_status": "running",
                                      "analysis_status": "running", "images": 0})
            pipeline.preflight()
            flight = SearchFlight(client, frame, name, origin, stop_event)
            flight.prepare(world, local, height, lambda altitude: check_building_clearance(plan, altitude))
            # 升高后检查实际接入位置；漂移后重新规划，不能忽略当前位置与首路点的偏差。
            current = vector(client.call("simGetVehiclePose", name)["position"])
            actual = [origin[i]+current[i]*frame.scale for i in (0, 1)]
            if math.dist(actual, plan["legs"][0]["points"][0]) > .5:
                plan = plan_search_path(actual, target)
                check_building_clearance(plan, origin[2]-flight.z*frame.scale)
                summary.update(target_piece_ids=plan["target_piece_ids"], planned_distance_m=plan["distance_m"])
                directory.joinpath("plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
            pipeline.start()

            # 将飞行到达检查后的当前位置写入本次任务进度日志。
            # 输入为 UE 世界 XY 米制位置；只保存记录，不调用模型或改变飞行。
            def observe(position):
                pipeline.log("progress.jsonl", {"position_xy_m": position, "observed_at_utc": datetime.now(timezone.utc).isoformat()})

            for leg in plan["legs"]:
                flight.check_cancel()
                pipeline.set_leg(leg)
                pipeline.check()
                flight.fly(leg["points"], speed, pipeline.check, observe)
                if leg["kind"] == "patrol" and leg["piece_id"] not in summary["completed_piece_ids"]:
                    summary["completed_piece_ids"].append(leg["piece_id"])
                pipeline.log("progress.jsonl", {"completed_leg": leg["piece_id"], "kind": leg["kind"]})
            summary["flight_completed"] = set(summary["completed_piece_ids"]) == set(plan["target_piece_ids"])
            pipeline.check()
            summary["status"] = "completed"
        except BaseException as error:
            failure = error
            summary.update(status="cancelled" if isinstance(error, (InterruptedError, KeyboardInterrupt)) else "failed",
                           error_type=type(error).__name__)
        finally:
            try:
                if flight is not None:
                    try:
                        flight.stop()
                    except Exception as error:
                        summary["stop_error"] = type(error).__name__
                        summary["status"] = "failed"
                        if failure is None:
                            failure = error
                if pipeline is not None:
                    try:
                        summary["flight_status"] = summary["status"]
                        summary["flight_finished_at_utc"] = datetime.now(timezone.utc).isoformat()
                        pipeline.finish(summary)
                    except Exception as error:
                        summary["capture_finish_error"] = type(error).__name__
                        if failure is None:
                            failure = error
                            summary["status"] = "failed"
                if directory is not None and pipeline is None:
                    summary["flight_status"] = summary["status"]
                    summary["analysis_status"] = "not_started"
                    summary["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
                    write_summary(directory, summary)
            finally:
                try:
                    # 已创建pipeline时，分析线程负责在真正用完后关闭SDK连接。
                    if owned_analyzer is not None and pipeline is None:
                        owned_analyzer.close()
                finally:
                    lock.release()
        if failure is not None:
            raise failure


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="实际执行指定区域的沿路巡查、拍照与视觉分析")
    parser.add_argument("zone_id")
    parser.add_argument("--drone", default="UAV_01")
    parser.add_argument("--height", required=True, type=float)
    parser.add_argument("--speed", type=float, default=5)
    parser.add_argument("--nearby", type=float, help="边界外周边米数，省略表示区域内部道路")
    parser.add_argument("--interval", type=float, default=3)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    target = {"zone_id": args.zone_id, **({"scope": "nearby", "buffer_m": args.nearby} if args.nearby is not None else {})}
    search(args.drone, target, args.height, args.speed, capture_interval=args.interval, output_dir=args.output)
