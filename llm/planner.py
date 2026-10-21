"""主流程：模型查询 → 本地返回结果 → 模型判断，最多5轮。"""

from copy import deepcopy
import json
from pathlib import Path

from pydantic import ValidationError

from .functions.catalog import Clarification
from .functions.commands import Target, check_final, clarification, validate_request
from .functions.fleet import normalize_fleet

TOOLS = [{"type": "function", "name": "query_map",
          "description": "按名称、类别和空间条件查询真实区域，返回候选与计算距离；不生成或执行指令。",
          "parameters": Target.model_json_schema(), "strict": False}]


# 执行模型提出的地图查询，将参数错误和目标歧义也转换成工具反馈。
# 输入为工具调用、地图、无人机及已确认集合；返回查询结果，并更新已确认范围。
def query_map(call, catalog, fleet, confirmed):
    """参数无效或目标不明确时也返回结果，交给模型继续判断。"""
    if call.name != "query_map":
        return {"status": "error", "reason": "未知工具，只支持 query_map。"}
    try:
        target = Target.model_validate_json(call.arguments)
        zones = catalog.query(target, fleet)
    except ValidationError:
        return {"status": "error", "reason": "查询参数不符合约定，请检查字段、参照和半径。"}
    except Clarification as error:
        return {"status": "needs_clarification", "reason": str(error),
                "candidates": [{"zone_id": z["zone_id"], "name": z["name"]} for z in error.candidates]}
    confirmed.update(target.search_key(z["zone_id"]) for z in zones)
    return {"status": "ok", "query": target.model_dump(exclude_defaults=True),
            "zones": [{k: z[k] for k in ("zone_id", "name", "category", "subtype", "distance_m", "scope", "buffer_m") if k in z}
                      for z in zones]}


PROMPT = (Path(__file__).resolve().parent / "prompts" / "search_area.txt").read_text(encoding="utf-8")
MAX_ITERATIONS = 5


# 组装首次模型请求：系统提示词、设施索引、无人机摘要和用户对话。
# 输入为任务、地图和无人机快照；返回请求参数，几何边界和坐标留在本地。
def build_model_input(instruction, catalog, fleet, conversation=None):
    instruction, conversation = validate_request(instruction, conversation)
    overview = catalog.overview()
    fields = ["zone_id", "name", "category", "subtype"]
    overview["zone_fields"] = fields
    # 初始只发设施索引及用户明确提到的区域；完整375对象仍由 query_map 查询。
    mentioned = instruction + " ".join(m["content"] for m in conversation if m["role"] == "user")
    overview["total_objects"] = len(overview["zones"])
    overview["zones"] = [[zone[key] for key in fields] for zone in overview["zones"]
                         if catalog.zones[zone["zone_id"]].get("object_type") != "region"
                         or zone["zone_id"] in mentioned or zone["name"] in mentioned]
    context = {
        "catalog": overview,
        "fleet": [{"name": d["name"], "available": d["available"],
                   "position_known": d["position_m"] is not None} for d in fleet],
    }
    if conversation:
        context["conversation"] = conversation
    # 最新输入放在地图和历史之后，避免简短补充被长目录淹没。
    context["instruction"] = instruction
    return {"instructions": PROMPT, "tools": TOOLS, "parallel_tool_calls": False,
            "reasoning": {"effort": "minimal"}, "temperature": 0,
            "text": {"format": {"type": "json_object"}},
            "input": [{"role": "user", "content": json.dumps(context, ensure_ascii=False, separators=(",", ":"))}]}


class Planner:
    # 保存模型客户端、模型名称和只读地图，供后续规划调用。
    # 只初始化依赖，不发起模型请求。
    def __init__(self, client, model, catalog):
        self.client, self.model, self.catalog = client, model, catalog

    # 运行最多 5 轮的查询、工具反馈和模型判断，并校验最终搜索指令。
    # 输入为任务、无人机、可选历史和记录列表；返回可执行规划或待补充原因，不控制飞行。
    def plan(self, instruction, fleet, trace=None, conversation=None):
        fleet = normalize_fleet(fleet)
        base = build_model_input(instruction, self.catalog, fleet, conversation)
        if not any(d["available"] for d in fleet):
            return {**clarification("当前没有可用无人机，请更新无人机快照。"), "iterations": 0}
        history, confirmed, coverage = list(base["input"]), set(), {}
        latest_query = None
        for iteration in range(1, MAX_ITERATIONS + 1):
            # 每轮带回完整对话：模型查询、工具结果和校验反馈都不会丢失。
            request = {**base, "model": self.model, "store": False, "input": list(history),
                       "instructions": PROMPT + f"\n当前第 {iteration}/{MAX_ITERATIONS} 轮。" +
                           ("这是最后一轮，必须返回最终 JSON 或 question，不能再查询。" if iteration == MAX_ITERATIONS else ""),
                       "tool_choice": "none" if iteration == MAX_ITERATIONS else "auto"}
            record = None
            if trace is not None:
                record = {"iteration": iteration, "request": deepcopy(request), "tool_results": []}
                trace.append(record)
            try:
                response = self.client.responses.create(**request)
            except Exception as error:
                if record is not None:
                    record["error"] = type(error).__name__
                raise
            if record is not None:
                record["response"] = response.model_dump(exclude_none=True)
            if response.status != "completed" or any(
                item.type == "message" and any(part.type == "refusal" for part in item.content)
                for item in response.output
            ):
                raise RuntimeError("模型拒绝或响应未完成，未生成指令")
            history.extend(item.model_dump(exclude_none=True) for item in response.output)
            calls = [item for item in response.output if item.type == "function_call"]
            if calls:
                if iteration == MAX_ITERATIONS:
                    break
                for call in calls:
                    output = query_map(call, self.catalog, fleet, confirmed)
                    latest_query = output if output.get("status") == "ok" else None
                    if output.get("status") == "ok" and output["query"].get("selection") == "all":
                        # all 是完整搜索范围，不能只在文字中承诺覆盖、指令却漏掉目标。
                        for category in {z["category"] for z in output["zones"]}:
                            coverage[category] = {(z["zone_id"], z.get("scope", "zone"), z.get("buffer_m"))
                                                  for z in output["zones"] if z["category"] == category}
                    message = {"type": "function_call_output", "call_id": call.call_id,
                               "output": json.dumps(output, ensure_ascii=False)}
                    history.append(message)
                    if record is not None:
                        record["tool_results"].append(message)
                continue
            result = check_final(response.output_text, self.catalog, fleet, confirmed,
                                 required=set().union(*coverage.values()), latest_query=latest_query)
            if record is not None:
                record["validation"] = result
            if result["status"] == "ready" or result.get("stop_reason") == "needs_clarification":
                return {**result, "iterations": iteration}
            # 指令校验失败交回模型修正，不直接输出未经确认的指令。
            history.append({"role": "user", "content": json.dumps({"validation_error": result["reason"],
                            "instruction": "请修正、继续查询，或提出澄清问题；不能擅自更改原任务。"}, ensure_ascii=False)})
        return {**clarification(f"已达到{MAX_ITERATIONS}轮上限，模型仍未生成通过校验的完整指令，可重试本次规划。"),
                "iterations": MAX_ITERATIONS, "stop_reason": "max_iterations"}
