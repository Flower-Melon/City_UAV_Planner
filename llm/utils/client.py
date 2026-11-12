"""规划与视觉共用的云端/Ollama 客户端、响应适配和错误提示。"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from dotenv import dotenv_values

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def read_settings():
    """读取模型配置，已有环境变量优先。"""
    return {**dotenv_values(ENV_PATH), **os.environ}


# 读取 llm/.env 与环境变量，创建基础 OpenAI SDK 客户端。
# 输入为超时和重试配置；返回客户端、模型名，已有环境变量优先。
def create_client(*, timeout=60.0, max_retries=1, vision=False):
    from openai import OpenAI

    settings = read_settings()
    backend = settings.get("LLM_BACKEND", "openai").strip().lower()
    if backend == "ollama":
        model = settings.get("OLLAMA_MODEL", "qwen3.5:4b-q4_K_M").strip()
        if vision:
            model = settings.get("OLLAMA_VISION_MODEL", "").strip() or model
        if not model:
            raise ValueError("OLLAMA_MODEL 不能为空")
        base_url = settings.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").strip()
        if not base_url:
            raise ValueError("OLLAMA_BASE_URL 不能为空")
        client = OllamaClient(base_url=base_url,
                              timeout=float(settings.get("OLLAMA_TIMEOUT", "120")), max_retries=max_retries,
                              num_ctx=int(settings.get("OLLAMA_NUM_CTX", "16384")))
        return client, model
    if backend != "openai":
        raise ValueError("LLM_BACKEND 仅支持 openai 或 ollama")
    if not settings.get("OPENAI_API_KEY", "").strip():
        raise ValueError("请先在 llm/.env 填写 OPENAI_API_KEY（也支持已有环境变量）")
    model = settings.get("OPENAI_MODEL", "gpt-4.1-mini").strip()
    if vision:
        model = settings.get("OPENAI_VISION_MODEL", "").strip() or model
    if not model:
        raise ValueError("OPENAI_MODEL 不能为空")
    client = OpenAI(api_key=settings["OPENAI_API_KEY"].strip(),
                    base_url=settings.get("OPENAI_BASE_URL", "").strip() or None,
                    timeout=timeout, max_retries=max_retries)
    return client, model


# 把校验、网络、鉴权及服务异常转换成用户能理解的提示。
# 输入为异常；返回错误说明，避免暴露底层请求内容或凭据。
def error_message(error):
    # 不打印可能包含输入数据的完整校验异常或底层请求凭据。
    from pydantic import ValidationError

    if isinstance(error, ValidationError):
        return "意图JSON不符合约定，请检查字段、动作、数值和参照定义；未生成指令"
    if isinstance(error, (ValueError, FileNotFoundError, RuntimeError)):
        return str(error)
    import httpx

    if isinstance(error, httpx.TimeoutException):
        return "等待远程 Ollama 响应超时，请检查服务器负载或调整 OLLAMA_TIMEOUT。"
    if isinstance(error, httpx.ConnectError):
        return "无法连接远程 Ollama，请检查 OLLAMA_BASE_URL、远程服务及网络访问规则。"
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        try:
            detail = error.response.json()
        except ValueError:
            detail = {}
        message = str(detail.get("error", "")).lower() if isinstance(detail, dict) else ""
        if "out of memory" in message or "unable to allocate cuda" in message:
            return ("远程 Ollama 显存不足，无法加载模型或创建推理上下文；"
                    f"请使用四位量化模型或释放服务器显存。（HTTP {status}）")
        if status == 404:
            return "Ollama 模型或接口不存在，请核对 OLLAMA_MODEL 和 OLLAMA_BASE_URL。（HTTP 404）"
        if status >= 500:
            return f"远程 Ollama 内部错误，请查看服务器上的 Ollama 日志。（HTTP {status}）"
        return f"远程 Ollama 拒绝请求，请检查服务日志、模型的工具调用与图片支持。（HTTP {status}）"
    try:
        from openai import APIConnectionError, APIStatusError, APITimeoutError
    except ImportError:
        return "缺少 openai SDK，请安装项目根目录 requirements.txt 中的依赖。"
    if isinstance(error, APITimeoutError):
        return "连接模型服务超时，请检查网络或稍后重试。"
    if isinstance(error, APIConnectionError):
        return "无法连接模型服务，请检查网络及 OPENAI_BASE_URL。"
    if isinstance(error, APIStatusError):
        status = error.status_code
        detail = error.body if isinstance(error.body, dict) else {}
        detail = detail.get("error", detail)
        if status == 403 and isinstance(detail, dict) and "free quota exhausted" in str(detail.get("message", "")).lower():
            return "模型免费额度已用尽，当前账号仅允许免费调用，请在模型服务控制台检查额度和计费设置。（HTTP 403）"
        reasons = {
            401: "模型服务认证失败，请检查 OPENAI_API_KEY。",
            403: "当前账号无权访问该模型，请检查模型权限。",
            404: "指定模型或 Responses 接口不存在，请核对 OPENAI_MODEL 和服务地址。",
            429: "模型服务限流或额度不足，请检查额度或稍后重试。",
        }
        reason = reasons.get(status, "模型服务拒绝了请求，请确认该模型支持 Responses API、工具调用及 JSON 模式。"
                             if status in (400, 422) else "模型服务返回异常，请稍后重试。")
        return f"{reason}（HTTP {status}）"
    return (f"{type(error).__name__}：请检查依赖、网络、密钥、模型权限，"
            "以及服务是否支持 Responses API、function calling 和 JSON 模式")


class OllamaClient:
    """Ollama 原生接口客户端，提供规划与视觉共用的连接生命周期。"""

    def __init__(self, *, base_url, timeout, max_retries, num_ctx):
        import httpx

        self.http = httpx.Client(base_url=base_url.rstrip("/") + "/", trust_env=False,
                                 timeout=httpx.Timeout(timeout, connect=10.0),
                                 transport=httpx.HTTPTransport(retries=max_retries))
        self.num_ctx = num_ctx
        self.responses = OllamaResponses(self)

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class OllamaResponses:
    """将 Ollama /api/chat 适配为项目使用的 Responses 接口。"""

    def __init__(self, client):
        self.client = client

    def create(self, *, model, input, instructions, tools=None, parallel_tool_calls=True,
               temperature=0, text=None, store=False, reasoning=None):
        from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

        # store、reasoning 和 parallel_tool_calls 不传给 Ollama。
        messages = [{"role": "system", "content": instructions}]
        call_names = {}
        for item in input:
            kind = item.get("type", "message")
            if kind == "function_call":
                call_names[item["call_id"]] = item["name"]
                if messages[-1]["role"] != "assistant" or "tool_calls" not in messages[-1]:
                    messages.append({"role": "assistant", "content": "", "tool_calls": []})
                messages[-1]["tool_calls"].append({"function": {
                    "name": item["name"], "arguments": json.loads(item["arguments"])}})
            elif kind == "function_call_output":
                messages.append({"role": "tool", "tool_name": call_names[item["call_id"]],
                                 "content": item["output"]})
            else:
                message = self._message(item)
                if item["role"] == "assistant" and messages[-1]["role"] == "assistant" and "tool_calls" in messages[-1]:
                    messages[-1]["content"] = message["content"]
                else:
                    messages.append(message)

        has_images = any(message.get("images") for message in messages)
        request = {"model": model, "messages": messages, "stream": False, "think": False,
                   "keep_alive": "10m", "options": {"temperature": temperature,
                   "num_ctx": self.client.num_ctx, "num_predict": 512 if has_images else 4096}}
        if tools:
            # 查询阶段允许原生工具调用或澄清 JSON，避免 JSON 约束干扰工具选择。
            request["tools"] = [{"type": "function", "function": {
                key: tool[key] for key in ("name", "description", "parameters")}} for tool in tools]
        elif text and text["format"]["type"] == "json_object":
            request["format"] = "json"
        response = self.client.http.post("api/chat", json=request)
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            raise RuntimeError(f"Ollama 服务错误：{data['error']}")
        message, output = data["message"], []
        for call in message.get("tool_calls") or []:
            function = call["function"]
            output.append(ResponseFunctionToolCall(type="function_call", call_id=uuid4().hex,
                name=function["name"], arguments=json.dumps(function["arguments"], ensure_ascii=False)))
        content = message.get("content", "")
        if content:
            output.append(ResponseOutputMessage(id=uuid4().hex, type="message", role="assistant",
                status="completed", content=[ResponseOutputText(type="output_text", text=content, annotations=[])]))
        completed = data.get("done") and data.get("done_reason") != "length"
        return SimpleNamespace(status="completed" if completed else "incomplete",
                               output=output, output_text=content)

    @staticmethod
    def _message(item):
        content = item["content"]
        message = {"role": item["role"], "content": content}
        if isinstance(content, list):
            texts, images = [], []
            for part in content:
                if part["type"] in ("input_text", "output_text"):
                    texts.append(part["text"])
                elif part["type"] == "input_image":
                    url = part["image_url"]
                    if not url.startswith("data:image/") or ";base64," not in url:
                        raise ValueError("Ollama 图片输入需要 Base64 编码的本地图片。")
                    images.append(url.split(";base64,", 1)[1])
                else:
                    raise ValueError(f"Ollama 不支持的消息内容：{part['type']}")
            message["content"] = "\n".join(texts)
            if images:
                message["images"] = images
        return message
