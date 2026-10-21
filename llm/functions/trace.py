"""按轮记录真实输入输出；完整初始目录只展示一次。"""

from copy import deepcopy
import json
from pathlib import Path


# 校验过程记录路径，限定为 llm 目录内的 Markdown 文件。
# 输入为路径；返回解析后的绝对路径，越界或扩展名错误时抛出异常。
def trace_path(path):
    path = Path(path).resolve()
    if not path.is_relative_to(Path(__file__).resolve().parents[1]) or path.suffix.lower() != ".md":
        raise ValueError("过程记录必须是 llm 文件夹内的 .md 文件。")
    return path


# 把文字或 JSON 包装成带标题的 Markdown 代码块。
# 输入为标题、内容和语言；返回可拼接的 Markdown 字符串。
def block(title, value, language="json"):
    content = json.dumps(value, ensure_ascii=False, indent=2) if language == "json" else value
    return f"### {title}\n\n```{language}\n{content}\n```\n"


# 在副本中展开消息内的 JSON 字符串，便于阅读模型输入和工具反馈。
# 输入为消息列表；返回展示用副本，不修改实际请求记录。
def readable(messages):
    messages = deepcopy(messages)
    for message in messages:
        for key in ("content", "arguments", "output"):
            if isinstance(message.get(key), str):
                try:
                    message[key] = json.loads(message[key])
                except ValueError:
                    pass
    return messages


# 将各轮系统提示词、累计输入增量、原始输出和本地校验保存成 Markdown。
# 输入为记录路径、任务、无人机、逐轮记录和最终结果；写文件，无返回值。
def write_trace(path, instruction, fleet, trace, result):
    path = trace_path(path)
    lines = [f"# {instruction}\n", f"实际请求模型 {len(trace)} 次，上限5次；本文件仅记录规划，飞行状态以执行层结果为准。\n",
             "每轮输入 = 已有对话 + 本轮新增消息。按下文顺序即可还原完整对话；JSON字符串展开缩进展示。\n",
             block("本次无人机快照", fleet)]
    if trace:
        config = {k: v for k, v in trace[0]["request"].items()
                  if k not in ("instructions", "input", "tool_choice")}
        lines += ["<details>\n<summary>共享请求配置和工具定义</summary>\n", block("配置", config), "</details>\n"]
    input_count = 0
    for record in trace:
        request = record["request"]
        new_input = readable(request["input"][input_count:])
        input_count = len(request["input"])
        lines += [f"## 第 {record['iteration']} 轮\n",
                  block("系统提示词（实际全文）", request["instructions"], "text"),
                  f"本轮 tool_choice：`{request['tool_choice']}`。\n"]
        if record["iteration"] == 1:
            context = new_input[0]["content"]
            lines += [block("用户任务和无人机摘要", {k: context[k] for k in ("instruction", "fleet")}),
                      "<details>\n<summary>完整初始用户消息（设施与任务相关区域索引）</summary>\n",
                      block("完整输入", new_input), "</details>\n"]
        else:
            lines += [block("新增输入（此前模型输出及本地反馈）", new_input)]
        output = record.get("response", {}).get("output", [])
        lines += [block("大模型原始输出", output)]
        for item in output:
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    lines += [block("模型回复文本原文", content["text"], "text")]
        if record["tool_results"]:
            lines += [block("Python 工具返回，交给下一轮模型", readable(record["tool_results"]))]
        if "validation" in record:
            lines += [block("最终回复校验", record["validation"])]
        if "error" in record:
            lines += [f"请求失败：`{record['error']}`；未生成指令。\n"]
    lines += [block("最终程序返回", result)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
