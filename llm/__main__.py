"""命令行入口：参数解析、离线预览与任务规划。"""

import argparse
import json
import math
from pathlib import Path
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm.functions.catalog import ZoneCatalog
from llm.functions.client import create_client, error_message
from llm.functions.commands import resolve_intent
from llm.functions.fleet import load_fleet, snapshot_fleet
from llm.planner import Planner, build_model_input


# 解析命令行参数，并校验标准输入任务、无人机快照和过程记录路径。
# 输入来自命令行或标准输入；返回参数对象，不调用模型或控制飞行。
def parse_arguments():
    parser = argparse.ArgumentParser(description="自然语言 → 模型解析 → 本地选区 → 指令")
    parser.add_argument("instruction", nargs="?", help="中文任务指令")
    parser.add_argument("--trace-file", type=Path, help="将每轮完整输入输出写成 llm 内的 Markdown 文件")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--drones", nargs="+", help="本次确认可用的无人机ID，位置未知")
    source.add_argument("--fleet-file", type=Path, help="无人机状态JSON（UE世界米制坐标）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--inspect", metavar="CATEGORY", help="离线查看某类区域，例如 education")
    mode.add_argument("--intent-file", type=Path, help="离线使用目标条件JSON，不调用模型")
    mode.add_argument("--show-prompt", action="store_true", help="展示完整模型输入，不读取密钥、不调用模型")
    mode.add_argument("--show-fleet", action="store_true", help="查看无人机名称、可用性和 UE 世界坐标，不调用模型")
    mode.add_argument("--request-stdin", action="store_true", help="从标准输入读取网页任务和对话 JSON")
    mode.add_argument("--execute", action="store_true", help="规划通过后实际调用无人机搜索，须指定 --height")
    parser.add_argument("--height", type=float, help="实际执行时相对地面的飞行高度，单位米")
    parser.add_argument("--speed", type=float, default=5.0, help="执行速度上限，默认5米/秒")
    parser.add_argument("--interval", type=float, default=3.0, help="拍照间隔，默认3秒")
    args = parser.parse_args()
    if args.execute:
        if args.height is None or any(not math.isfinite(v) or v <= 0 for v in (args.height, args.speed, args.interval)):
            parser.error("--execute 需要正的有限 --height、--speed 和 --interval")
        if args.drones or args.fleet_file:
            parser.error("实际执行必须读取真实无人机状态，不能使用 --drones 或 --fleet-file")
    args.conversation = None
    args.live_fleet = None
    if args.request_stdin:
        from llm.functions.commands import validate_request
        data = json.loads(sys.stdin.read(65537))
        if not isinstance(data, dict):
            raise ValueError("任务请求必须是 JSON 对象。")
        args.instruction, args.conversation = validate_request(data.get("instruction"), data.get("conversation"))
        args.live_fleet = snapshot_fleet(data.get("live_fleet"))
    if args.trace_file:
        from llm.functions.trace import trace_path
        args.trace_file = trace_path(args.trace_file)
    if not args.inspect:
        if not args.intent_file and not args.show_fleet and not args.instruction:
            parser.error("请提供自然语言指令，或使用 --intent-file 离线运行")
    return args


# 读取本地 JSON 文件，兼容 UTF-8 文件开头的 BOM。
# 输入为文件路径；返回解析后的对象，读取或格式错误直接抛出。
def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


# 根据参数选择目录查看、无人机查看、离线校验、提示词预览或模型规划。
# 输入为参数对象；默认只规划，显式 --execute 时接通实际搜索并等待飞行结果。
def run(args):
    catalog = ZoneCatalog()
    if args.inspect:
        if args.inspect not in catalog.category_names:
            raise ValueError("未知类别，可用类别：" + ", ".join(catalog.category_names))
        return [z for z in catalog.zones.values() if z["category"] == args.inspect]

    fleet = load_fleet(fleet_file=args.fleet_file, drones=args.drones, live_fleet=args.live_fleet)
    if args.show_fleet:
        return {"coordinate_system": "UE_WORLD_METRES", "drones": fleet}
    if args.intent_file:
        return resolve_intent(read_json(args.intent_file), catalog, fleet)
    if args.show_prompt:
        return build_model_input(args.instruction, catalog, fleet)

    client, model = create_client()
    trace = [] if args.trace_file else None
    result = {"status": "error", "reason": "流程异常结束，未生成指令。", "commands": [], "executed": False}
    try:
        with client:
            result = Planner(client, model, catalog).plan(args.instruction, fleet, trace=trace,
                                                       conversation=args.conversation)
        if args.execute and result["status"] == "ready":
            from llm.executor import MissionExecutor
            executor = MissionExecutor(catalog=catalog)
            executor.start(result, args.height, args.speed, args.interval)
            try:
                execution = executor.wait()
            except KeyboardInterrupt:
                executor.cancel()
                execution = executor.wait()
            return {"plan": result, "execution": execution}
        return result
    finally:
        if trace is not None:
            from llm.functions.trace import write_trace
            write_trace(args.trace_file, args.instruction, fleet, trace, result)


# 执行命令行任务并将结果输出为 JSON，统一处理失败信息。
# 结果写入标准输出、错误写入标准错误；返回退出码 0 或 1。
def main():
    try:
        result = run(parse_arguments())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        execution = result.get("execution") if isinstance(result, dict) else None
        return 1 if execution and execution["status"] != "completed" else 0
    except Exception as error:
        # 结构化错误让网页显示具体原因，不转发底层异常或请求信息。
        print(json.dumps({"type": "llm_error", "message": error_message(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
