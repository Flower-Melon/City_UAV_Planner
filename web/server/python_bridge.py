"""网页与 Python 能力之间的通信适配；每条请求只完成一个操作。"""

from contextlib import redirect_stdout
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class PythonBridge:
    """复用模型连接，提供规划、状态读取和单区域搜索。"""

    def __init__(self):
        self.client = None
        self.planner = None

    def handle(self, request):
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
            from UAV.utils.airsim_client import AirSimFleet
            return AirSimFleet().read()
        if operation == "search":
            from UAV.API.search_area import search_area
            flight = request.get("flight")
            if not isinstance(flight, dict):
                raise ValueError("搜索需要飞行参数")
            search_area(request.get("drone_id"), request.get("target"), **flight)
            return {"completed": True}
        raise ValueError("operation 仅支持 plan、fleet、search")

    def serve(self, stream, output):
        try:
            for line in stream:
                request_id, operation = None, None
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise ValueError("请求必须是 JSON 对象")
                    request_id, operation = request.get("id"), request.get("operation")
                    # 能力内部的普通输出不进入 JSON 通信通道。
                    with redirect_stdout(sys.stderr):
                        result = self.handle(request)
                    response = {"id": request_id, "result": result}
                except Exception as error:
                    message = str(error) if isinstance(error, (ValueError, RuntimeError, OSError)) else type(error).__name__
                    if operation == "plan":
                        from llm.utils.client import error_message
                        message = error_message(error)
                    response = {"id": request_id, "error": message, "error_type": type(error).__name__}
                print(json.dumps(response, ensure_ascii=False, allow_nan=False), file=output, flush=True)
        finally:
            self.close()

    def close(self):
        if self.client is not None:
            self.client.close()
            self.client = self.planner = None


if __name__ == "__main__":
    try:
        PythonBridge().serve(sys.stdin, sys.stdout)
    except BrokenPipeError:
        raise SystemExit(1)
