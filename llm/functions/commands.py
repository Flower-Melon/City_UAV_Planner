"""任务格式、输入校验、无人机分配和最终指令生成。"""

import json

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .catalog import Clarification
from .fleet import normalize_fleet


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Target(StrictModel):
    scope: Literal["zone", "nearby"] = "zone"
    buffer_m: float | None = Field(default=None, gt=0, allow_inf_nan=False,
                                  description="scope=nearby时，原区域边界外的搜索宽度，省略默认50米；不是其他区域的筛选半径")
    category: str | None = None
    name: str | None = None
    zone_id: str | None = None
    subtype: str | None = None
    direction: Literal["none", "east", "west", "north", "south", "northeast", "northwest", "southeast", "southwest"] = "none"
    reference: str | None = None
    radius_m: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    selection: Literal["single", "nearest", "all"] = "single"

    # 校验可选筛选字段，拒绝空白值并去掉有效字符串的首尾空格。
    # 由 Pydantic 自动调用；返回清理后的字符串或 None。
    @field_validator("category", "name", "zone_id", "subtype", "reference")
    @classmethod
    def nonempty_filter(cls, value):
        if value is not None:
            if not value.strip():
                raise ValueError("未使用的条件请省略，不要填空字符串")
            return value.strip()
        return value

    # 检查目标至少有一个筛选条件，并落实周边范围默认 50 米的规则。
    # 由 Pydantic 自动调用；返回校验后的目标，拒绝区域内部任务携带周边宽度。
    @model_validator(mode="after")
    def check_target(self):
        if not any((self.category, self.name, self.zone_id, self.subtype)):
            raise ValueError("需要目标类别、名称、子类或 ID")
        if self.scope == "nearby" and self.buffer_m is None:
            self.buffer_m = 50.0
        if self.scope == "zone" and self.buffer_m is not None:
            raise ValueError("buffer_m仅用于scope=nearby的周边搜索")
        return self

    # 生成同时包含区域 ID、搜索范围和周边宽度的确认标识。
    # 输入为可选区域 ID；返回元组，避免把区域内部与周边任务混为一谈。
    def search_key(self, zone_id=None):
        return (zone_id or self.zone_id, self.scope, self.buffer_m)


class Task(StrictModel):
    action: Literal["SearchArea"]
    drone_id: str | None = None
    target: Target


class Intent(StrictModel):
    tasks: list[Task] = Field(default_factory=list)
    question: str | None = Field(default=None, min_length=1)
    reply: str | None = Field(default=None, min_length=1)

    # 把模型直接返回的单任务对象转换成统一的 tasks 列表结构。
    # 输入为待校验数据；返回转换后的数据，保留可选的回复文字。
    @model_validator(mode="before")
    @classmethod
    def single_task(cls, data):
        if isinstance(data, dict) and "action" in data:
            return {"tasks": [{k: v for k, v in data.items() if k != "reply"}],
                    **({"reply": data["reply"]} if "reply" in data else {})}
        return data

    # 保证结果在任务和澄清问题之间二选一，并拒绝空白回答。
    # 由 Pydantic 自动调用；合法时返回当前意图对象。
    @model_validator(mode="after")
    def check_content(self):
        if bool(self.tasks) == (self.question is not None):
            raise ValueError("请返回任务或澄清问题，不能同时返回或同时为空")
        if self.question is not None and not self.question.strip():
            raise ValueError("澄清问题不能为空")
        if self.reply is not None and not self.reply.strip():
            raise ValueError("回答不能为空白")
        return self

    # 将意图导出成简短 JSON 结构，省略默认值和空字段。
    # 返回单任务、任务列表或澄清问题对象；用于最终程序输出。
    def compact(self):
        """最终结果也保留简短格式，不回填一堆 null。"""
        if self.question is not None:
            return {"question": self.question}
        tasks = [task.model_dump(exclude_defaults=True) for task in self.tasks]
        return tasks[0] if len(tasks) == 1 else {"tasks": tasks}


# 校验任务长度、历史角色顺序和总文本上限，拒绝无效对话。
# 输入为任务和可选历史；返回去除首尾空格的任务及合法历史列表。
def validate_request(instruction, conversation=None):
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


# 构造统一的待补充结果，限制展示的候选数量。
# 输入为原因和候选区域；返回空指令结果，并标记未执行。
def clarification(reason, candidates=()):
    candidates = list(candidates)
    return {"status": "needs_clarification", "reason": reason, "commands": [], "executed": False,
            "candidate_count": len(candidates), "candidates": candidates[:10]}


# 校验任务与资源后查询地图，分配无人机并为连续任务生成顺序编号。
# 输入为意图、地图和无人机；返回完整 SearchArea 规划或待补充结果，不触发飞行。
def resolve_intent(intent, catalog, fleet):
    """离线传入意图 JSON，可单独验证地图查询和指令生成。"""
    if not isinstance(intent, Intent):
        intent = Intent.model_validate(intent)
    fleet = normalize_fleet(fleet)
    if intent.question is not None:
        return clarification(intent.question)
    available = {d["name"]: d for d in fleet if d["available"]}
    if not available:
        return clarification("当前没有可用无人机，请更新无人机快照。")
    try:
        # 先校验全部目标与显式资源，再分配，避免生成部分有效的任务。
        resolved, zone_ids = [], set()
        for task in intent.tasks:
            if task.drone_id is not None:
                if task.drone_id not in available:
                    raise Clarification(f"无人机 {task.drone_id} 不存在或不可用。")
            zones = catalog.query(task.target, fleet)
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
                commands.append({"action": "SearchArea", "drone_id": drone_id,
                                 "sequence": sequences[drone_id], "zone_id": zone["zone_id"], "target": zone})
        return {"status": "ready", "reason": "已覆盖全部选定区域；自动均衡分配，同一无人机按 sequence 顺序逐个搜索。仅规划，未执行。",
                "commands": commands, "executed": False, "intent": intent.compact()}
    except Clarification as error:
        return clarification(str(error), error.candidates)


# 解析模型最终 JSON，核实查询来源、范围完整性和无人机条件。
# 输入为模型文本及本轮查询记录；返回已校验规划或修正原因，禁止未查询目标直接通过。
def check_final(text, catalog, fleet, confirmed, required=(), latest_query=None):
    """模型作最终选择；本地只核实来源、资源和指令格式。"""
    # 兼容网关在 JSON 模式下仍包裹一个 Markdown 代码块的情况。
    # 仅移除完整外壳，内容仍经过相同的结构、来源、覆盖和资源校验。
    lines = text.strip().splitlines()
    if len(lines) >= 3 and lines[0].lower() in ("```json", "```") and lines[-1] == "```":
        text = "\n".join(lines[1:-1])
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "use_query_result" in data:
            if (data.get("use_query_result") is not True or
                    set(data) - {"use_query_result", "reply", "drone_id", "drone_ids"} or
                    ("drone_id" in data and "drone_ids" in data)):
                return clarification("确认查询结果时仅填 use_query_result:true、reply 和可选 drone_id 或 drone_ids。")
            if not latest_query or latest_query.get("status") != "ok" or not latest_query.get("zones"):
                return clarification("本轮尚无可确认的最新成功查询，请先成功调用 query_map。")
            drones = data.get("drone_ids", [data.get("drone_id")])
            if (not isinstance(drones, list) or not drones or
                    ("drone_ids" in data and any(not isinstance(d, str) or not d.strip() for d in drones))):
                return clarification("drone_ids 必须是非空无人机 ID 列表。")
            data = {"tasks": [{"action": "SearchArea", "drone_id": drones[i % len(drones)],
                               "target": {"zone_id": z["zone_id"], **{k: z[k] for k in ("scope", "buffer_m") if k in z}}}
                              for i, z in enumerate(latest_query["zones"])],
                    **({"reply": data["reply"]} if "reply" in data else {})}
        # sequence 完全由本地重算；修复历史指令回拷，其他未知字段仍严格拒绝。
        if isinstance(data, dict):
            tasks = data.get("tasks", [data] if "action" in data else [])
            if isinstance(tasks, list):
                for task in tasks:
                    if isinstance(task, dict):
                        task.pop("sequence", None)
        intent = Intent.model_validate(data)
    except (ValidationError, ValueError, TypeError):
        return clarification("最终 JSON 格式无效；任务仅允许 action、drone_id、target，target 填 zone_id及已查询的scope、buffer_m；"
                             "不要输出 sequence 等本地字段，也不要只给 reply。或用 question 提问。")
    if intent.question is not None:
        return {**clarification(intent.question), "stop_reason": "needs_clarification"}
    for task in intent.tasks:
        if not task.target.zone_id or set(task.target.model_dump(exclude_defaults=True)) - {"zone_id", "scope", "buffer_m"}:
            return clarification("最终 target 只填写 zone_id 和已确认的 scope、buffer_m；筛选条件请通过 query_map 查询。")
        if task.target.search_key() not in confirmed:
            return clarification(f"区域 {task.target.zone_id} 及其搜索范围尚未通过 query_map 成功确认，请先查询。")
    missing = set(required) - {task.target.search_key() for task in intent.tasks}
    if missing:
        return clarification("完整搜索范围仍缺少这些区域，请完整确认查询结果，无需逐个重查：" + ", ".join(
            f"{zone_id}周边{buffer_m:g}米" if scope == "nearby" else zone_id
            for zone_id, scope, buffer_m in sorted(missing)))
    result = resolve_intent(intent, catalog, fleet)
    if result["status"] == "ready" and intent.reply is not None:
        result["reply"] = intent.reply
    return result
