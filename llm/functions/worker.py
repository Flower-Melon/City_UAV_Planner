"""复用模型连接和地图；默认只规划，显式执行任务由同进程执行器管理。"""

import json
import math
import sys
from contextlib import redirect_stdout

from .catalog import ZoneCatalog
from .client import create_client, error_message
from .fleet import read_live_fleet, snapshot_fleet
from .commands import validate_request
from ..planner import Planner
from ..executor import MissionExecutor


# 逐行处理网页请求，复用连接和地图，同时为每次任务独立读取状态和规划。
# 输入为流及模型依赖；显式 execute 可启动任务，status/cancel 查询或取消，不默认起飞。
def serve(stream, output, client, model, catalog, executor=None):
    executor = executor if executor is not None else MissionExecutor(catalog=catalog)
    try:
        _serve(stream, output, client, model, catalog, executor)
    finally:
        # 管道关闭意味着服务退出；请求停止并等待制动，不能直接遗弃飞行线程。
        try:
            executor.status()
        except ValueError:
            pass
        else:
            executor.cancel()
            executor.wait()


# 处理逐行 JSON 协议；dispatch 只执行已经校验的规划，不重复调用模型。
# 输入为请求流与共享执行器；每条请求返回带相同 id 的结果或受控错误。
def _serve(stream, output, client, model, catalog, executor):
    for line in stream:
        request_id = None
        try:
            data = json.loads(line)
            request_id = data["id"]
            operation = data.get("operation", "plan")
            if operation in ("status", "cancel"):
                mission_id = data.get("mission_id")
                if mission_id is not None and (not isinstance(mission_id, str) or not mission_id) or operation == "cancel" and mission_id is None:
                    raise ValueError("请提供 mission_id")
                result = executor.status(mission_id) if operation == "status" else executor.cancel(mission_id)
                print(json.dumps({"id": request_id, "result": result}, ensure_ascii=False), file=output, flush=True)
                continue
            if operation not in ("plan", "execute", "dispatch"):
                raise ValueError("operation 仅支持 plan、execute、dispatch、status、cancel")
            if operation in ("execute", "dispatch"):
                flight = data.get("flight")
                if not isinstance(flight, dict) or "flight_height" not in flight or set(flight) - {
                    "flight_height", "flight_speed", "capture_interval"
                }:
                    raise ValueError("执行需要 flight.flight_height，可选 flight_speed、capture_interval")
                if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
                       for v in flight.values()):
                    raise ValueError("飞行高度、速度和拍照间隔必须为正的有限数值")
            if operation == "dispatch":
                result = executor.start(data.get("plan"), **flight)
                print(json.dumps({"id": request_id, "result": result}, ensure_ascii=False), file=output, flush=True)
                continue
            instruction, conversation = validate_request(data.get("instruction"), data.get("conversation"))
            fleet = snapshot_fleet(data.get("live_fleet")) if operation == "plan" else None
            if fleet is None:
                fleet = read_live_fleet()
            result = Planner(client, model, catalog).plan(instruction, fleet, conversation=conversation)
            if operation == "execute" and result["status"] == "ready":
                result = {"plan": result, "execution": executor.start(result, **flight)}
            response = {"id": request_id, "result": result}
        except Exception as error:
            response = {"id": request_id, "error": error_message(error)}
        print(json.dumps(response, ensure_ascii=False), file=output, flush=True)


# 初始化网页常驻进程所需的地图与模型连接，并启动请求循环。
# 从标准输入读取任务、向标准输出写结果；退出时关闭连接并返回状态码。
def main():
    try:
        catalog = ZoneCatalog()
        if sys.argv[1:] == ["--execution-only"]:
            output = sys.stdout
            with redirect_stdout(sys.stderr):
                serve(sys.stdin, output, None, None, catalog)
            return 0
        client, model = create_client()
        with client:
            serve(sys.stdin, sys.stdout, client, model, catalog)
    except Exception as error:
        print(json.dumps({"type": "llm_error", "message": error_message(error)}, ensure_ascii=False),
              file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
