"""规划与视觉共用的模型配置、SDK创建和错误提示。"""

import os
from pathlib import Path


# 读取 llm/.env 与环境变量，创建基础 OpenAI SDK 客户端。
# 输入为超时和重试配置；返回客户端、模型名，已有环境变量优先。
def create_client(*, timeout=60.0, max_retries=1):
    from dotenv import load_dotenv
    from openai import OpenAI

    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    if not os.getenv("OPENAI_API_KEY", "").strip():
        raise ValueError("请先在 llm/.env 填写 OPENAI_API_KEY（也支持已有环境变量）")
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
    if not model:
        raise ValueError("OPENAI_MODEL 不能为空")
    client = OpenAI(base_url=os.getenv("OPENAI_BASE_URL", "").strip() or None,
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
