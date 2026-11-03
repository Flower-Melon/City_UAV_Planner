"""区域搜索规划：校验输入、查询地图并整理模型返回的搜索计划。"""

import json
import math
from pathlib import Path
import sys
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "llm"

from .utils.catalog import Clarification, ZoneQuery


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Target(ZoneQuery):
    def to_query(self):
        """提取地图筛选条件，实际巡查范围由街区拓扑确定。"""
        return ZoneQuery.model_validate(self.model_dump())

    # 同一区域对应同一完整街区搜索任务。
    def search_key(self, zone_id=None):
        return zone_id or self.zone_id


class Task(StrictModel):
    action: Literal["SearchArea"]
    drone_id: str | None = None
    target: Target


class Intent(StrictModel):
    tasks: list[Task] = Field(min_length=1)
    reply: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def check_content(self):
        if self.reply is not None and not self.reply.strip():
            raise ValueError("回答不能为空白")
        return self


class QueryQuestion(StrictModel):
    question: str = Field(min_length=1)

    @model_validator(mode="after")
    def check_question(self):
        if not self.question.strip():
            raise ValueError("澄清问题不能为空")
        return self


TOOLS = [{"type": "function", "name": "query_map",
          "description": "按名称、类别和空间条件查询真实区域，返回候选与计算距离；不生成或执行指令。",
          "parameters": Target.model_json_schema(), "strict": False}]


PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
QUERY_PROMPT = (PROMPT_DIR / "query_map.txt").read_text(encoding="utf-8")
TASK_PROMPT = (PROMPT_DIR / "search_area.txt").read_text(encoding="utf-8")
CATALOG_PATH = Path(__file__).resolve().parents[1] / "Map" / "zones" / "zone_catalog.json"
MAX_ITERATIONS = 5


class Planner:
    """封装模型调用、规划数据校验和搜索指令生成，不执行飞行。"""

    def __init__(self, client, model, catalog):
        self.client, self.model, self.catalog = client, model, catalog
        self._directory = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        self._facility_ids = set(self._directory["facility_ids"])

    def plan(self, instruction, fleet, conversation=None):
        """先查询并解决歧义，再生成任务；两个阶段合计最多调用模型五次。"""
        fleet = self._normalize_fleet(fleet)
        base = self._build_model_input(instruction, fleet, conversation)
        if not any(d["available"] for d in fleet):
            return self._clarification("当前没有可用无人机，请更新无人机快照。")
        context = json.loads(base["input"][0]["content"])
        history, confirmed, coverage, queries = list(base["input"]), set(), {}, []
        stage = "query"
        for iteration in range(1, MAX_ITERATIONS + 1):
            # 查询阶段保留工具上下文；任务阶段只携带整理后的成功查询。
            if stage == "query" and iteration == MAX_ITERATIONS:
                break  # 为任务生成保留一次调用。
            request = {**base, "model": self.model, "store": False, "input": list(history),
                       "instructions": base["instructions"] + f"\n当前第 {iteration}/{MAX_ITERATIONS} 次模型调用。"}
            response = self.client.responses.create(**request)
            if response.status != "completed" or any(
                item.type == "message" and any(part.type == "refusal" for part in item.content)
                for item in response.output
            ):
                raise RuntimeError("模型拒绝或响应未完成，未生成指令")
            history.extend(item.model_dump(exclude_none=True) for item in response.output)
            calls = [item for item in response.output if item.type == "function_call"]
            if stage == "query" and calls:
                succeeded = True
                for call in calls:
                    output = self._query_map(call, fleet, confirmed)
                    if output.get("status") == "ok":
                        queries.append(output)
                    else:
                        succeeded = False
                    if output.get("status") == "ok" and output["query"].get("selection") == "all":
                        # all 是完整搜索范围，不能只在文字中承诺覆盖、指令却漏掉目标。
                        for category in {z["category"] for z in output["zones"]}:
                            coverage[category] = {z["zone_id"]
                                                  for z in output["zones"] if z["category"] == category}
                    message = {"type": "function_call_output", "call_id": call.call_id,
                               "output": json.dumps(output, ensure_ascii=False)}
                    history.append(message)
                if succeeded:
                    base = self._build_task_input(context, queries, set().union(*coverage.values()))
                    history, stage = list(base["input"]), "tasks"
                continue
            if stage == "query":
                try:
                    question = QueryQuestion.model_validate_json(response.output_text)
                except (ValidationError, ValueError, TypeError):
                    message = "查询阶段只能调用query_map或返回非空question；请在一次响应中提交全部必要查询。"
                else:
                    return self._clarification(question.question)
            else:
                if calls:
                    raise RuntimeError("任务生成阶段返回了工具调用，未生成指令")
                result = self._check_final(response.output_text, fleet, confirmed,
                                           required=set().union(*coverage.values()))
                if result["status"] == "ready":
                    return result
                message = result["message"]
            # 各阶段只修正自身输出；任务生成阶段不再查询或向用户提问。
            history.append({"role": "user", "content": json.dumps({"validation_error": message,
                            "instruction": "请按当前阶段的输出约定修正，不能擅自更改原任务。"}, ensure_ascii=False)})
        raise RuntimeError(f"在最多{MAX_ITERATIONS}次模型调用的预算内仍未完成查询和任务校验，可重试本次规划。")

    def _build_model_input(self, instruction, fleet, conversation=None):
        instruction, conversation = self._validate_request(instruction, conversation)
        # 固定目录在初始化时读取；每次只筛选设施和用户明确提到的区域。
        mentioned = instruction + " ".join(m["content"] for m in conversation if m["role"] == "user")
        overview = {key: self._directory[key] for key in
                    ("categories", "zone_fields", "direction_convention", "total_objects")}
        overview["zones"] = [zone for zone in self._directory["zones"]
                             if zone[0] in self._facility_ids or zone[0] in mentioned or zone[1] in mentioned]
        context = {
            "catalog": overview,
            "fleet": [{"name": d["name"], "available": d["available"],
                       "position_known": d["position_m"] is not None} for d in fleet],
        }
        if conversation:
            context["conversation"] = conversation
        # 最新输入放在地图和历史之后，避免简短补充被长目录淹没。
        context["instruction"] = instruction
        return {"instructions": QUERY_PROMPT, "tools": TOOLS, "parallel_tool_calls": True,
                "reasoning": {"effort": "minimal"}, "temperature": 0,
                "text": {"format": {"type": "json_object"}},
                "input": [{"role": "user", "content": json.dumps(context, ensure_ascii=False, separators=(",", ":"))}]}

    @staticmethod
    def _build_task_input(context, queries, required):
        """不携带查询提示词、目录、工具定义或完整工具历史。"""
        context = {key: context[key] for key in ("instruction", "conversation", "fleet") if key in context}
        context.update(queries=queries, required_zone_ids=sorted(required))
        return {"instructions": TASK_PROMPT, "reasoning": {"effort": "minimal"}, "temperature": 0,
                "text": {"format": {"type": "json_object"}},
                "input": [{"role": "user", "content": json.dumps(context, ensure_ascii=False, separators=(",", ":"))}]}

    def _query_map(self, call, fleet, confirmed):
        """参数无效或目标不明确时也返回结果，交给模型继续判断。"""
        if call.name != "query_map":
            return {"status": "error", "reason": "未知工具，只支持 query_map。"}
        try:
            target = Target.model_validate_json(call.arguments)
            zones = self.catalog.query(target.to_query(),
                                       [{"name": d["name"], "position_m": d["position_m"]} for d in fleet])
        except ValidationError:
            return {"status": "error", "reason": "查询参数不符合约定，请检查字段、参照和半径。"}
        except Clarification as error:
            return {"status": "needs_clarification", "reason": str(error),
                    "candidates": [{"zone_id": z["zone_id"], "name": z["name"]} for z in error.candidates]}
        confirmed.update(target.search_key(z["zone_id"]) for z in zones)
        return {"status": "ok", "query": target.model_dump(exclude_defaults=True),
                "zones": zones}

    @staticmethod
    def _normalize_fleet(fleet):
        """校验无人机名称、可用状态和世界坐标。"""
        if not isinstance(fleet, list) or not fleet:
            raise ValueError("请提供非空无人机列表")
        result, names = [], set()
        for drone in fleet:
            if not isinstance(drone, dict):
                raise ValueError("每架无人机应为对象")
            name = drone.get("name")
            if not isinstance(name, str) or not name.strip() or name != name.strip() or name in names:
                raise ValueError("无人机 name 必须非空、无首尾空格且不重复")
            if type(drone.get("available")) is not bool:
                raise ValueError(f"{name} 必须明确设置 available=true/false")
            position = drone.get("position_m")
            if position is not None and (
                not isinstance(position, list) or len(position) != 3
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in position)
            ):
                raise ValueError(f"{name} 的 position_m 必须是 UE 世界米制 [X,Y,Z] 或 null")
            names.add(name)
            result.append({"name": name, "available": drone["available"], "position_m": position})
        return sorted(result, key=lambda d: d["name"])

    @staticmethod
    def _validate_request(instruction, conversation=None):
        if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 4000 or "\0" in instruction:
            raise ValueError("请输入 1–4000 字的任务描述。")
        conversation = [] if conversation is None else conversation
        if not isinstance(conversation, list) or len(conversation) > 12 or len(conversation) % 2:
            raise ValueError("对话记录无效或过长，请开始新任务。")
        for index, message in enumerate(conversation):
            if (not isinstance(message, dict) or set(message) != {"role", "content"}
                    or message["role"] != ("user" if index % 2 == 0 else "assistant")
                    or not isinstance(message["content"], str) or not message["content"].strip()
                    or "\0" in message["content"]):
                raise ValueError("对话记录无效，请开始新任务。")
        if len(instruction) + sum(len(m["content"]) for m in conversation) > 16000:
            raise ValueError("对话内容过长，请开始新任务。")
        return instruction.strip(), conversation

    @staticmethod
    def _clarification(message):
        return {"status": "needs_clarification", "message": message, "commands": []}

    def _resolve_intent(self, intent, fleet):
        """将已校验的模型意图整理为搜索指令。"""
        if not isinstance(intent, Intent):
            intent = Intent.model_validate(intent)
        fleet = self._normalize_fleet(fleet)
        available = {d["name"]: d for d in fleet if d["available"]}
        if not available:
            return self._clarification("当前没有可用无人机，请更新无人机快照。")
        try:
            # 先校验全部目标与显式资源，再分配，避免生成部分有效的任务。
            resolved, zone_ids = [], set()
            for task in intent.tasks:
                if task.drone_id is not None:
                    if task.drone_id not in available:
                        raise Clarification(f"无人机 {task.drone_id} 不存在或不可用。")
                zones = self.catalog.query(task.target.to_query(),
                                           [{"name": d["name"], "position_m": d["position_m"]} for d in fleet])
                for zone in zones:
                    key = task.target.search_key(zone["zone_id"])
                    if key in zone_ids:
                        raise Clarification("多个任务指向同一区域，请明确是否需要重复搜索。", [zone])
                    zone_ids.add(key)
                resolved.append((task, zones))
            # 先计入显式任务，再把其余区域分给任务数最少的无人机，同数按 ID。
            loads = dict.fromkeys(sorted(available), 0)
            for task, zones in resolved:
                if task.drone_id:
                    loads[task.drone_id] += len(zones)
            sequences = dict.fromkeys(loads, 0)
            commands = []
            for task, zones in resolved:
                for zone in zones:
                    drone_id = task.drone_id or min(loads, key=lambda name: (loads[name], name))
                    if task.drone_id is None:
                        loads[drone_id] += 1
                    sequences[drone_id] += 1
                    commands.append({"drone_id": drone_id, "sequence": sequences[drone_id],
                                     "target": {"zone_id": zone["zone_id"]}})
            return {"status": "ready", "message": intent.reply or "已生成搜索计划，同一无人机按顺序逐个搜索。",
                    "commands": commands}
        except Clarification as error:
            return self._clarification(str(error))

    def _check_final(self, text, fleet, confirmed, required=()):
        """模型作最终选择；本地只核实来源、资源和指令格式。"""
        # 兼容网关在 JSON 模式下仍包裹一个 Markdown 代码块的情况。
        # 仅移除完整外壳，内容仍经过相同的结构、来源、覆盖和资源校验。
        lines = text.strip().splitlines()
        if len(lines) >= 3 and lines[0].lower() in ("```json", "```") and lines[-1] == "```":
            text = "\n".join(lines[1:-1])
        try:
            data = json.loads(text)
            # sequence 完全由本地重算；修复历史指令回拷，其他未知字段仍严格拒绝。
            if isinstance(data, dict):
                tasks = data.get("tasks", [data] if "action" in data else [])
                if isinstance(tasks, list):
                    for task in tasks:
                        if isinstance(task, dict):
                            task.pop("sequence", None)
            intent = Intent.model_validate(data)
        except (ValidationError, ValueError, TypeError):
            return self._clarification("最终 JSON 格式无效；任务仅允许 action、drone_id、target，target 仅填已查询的 zone_id；"
                                 "不要输出sequence、question等字段，也不要只给reply；请返回非空tasks。")
        for task in intent.tasks:
            if not task.target.zone_id or set(task.target.model_dump(exclude_defaults=True)) - {"zone_id"}:
                return self._clarification("最终 target 只填写 zone_id；筛选条件请通过 query_map 查询。")
            if task.target.search_key() not in confirmed:
                return self._clarification(f"区域 {task.target.zone_id} 及其搜索范围尚未通过 query_map 成功确认，请先查询。")
        missing = set(required) - {task.target.search_key() for task in intent.tasks}
        if missing:
            return self._clarification("完整搜索范围仍缺少这些区域，请在tasks中逐项补全，无需逐个重查：" + ", ".join(
                sorted(missing)))
        return self._resolve_intent(intent, fleet)


if __name__ == "__main__":
    from .utils.catalog import ZoneCatalog
    from .utils.client import create_client, error_message

    # 修改下面的任务和无人机状态即可调试；只调用模型，不连接 AirSim。
    instruction = "让 UAV_01 完整巡查第一学校所在街区"
    fleet = [{"name": "UAV_01", "available": True, "position_m": None}]
    try:
        client, model = create_client()
        with client:
            result = Planner(client, model, ZoneCatalog()).plan(instruction, fleet)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as error:
        print(error_message(error), file=sys.stderr)
        raise SystemExit(1)
