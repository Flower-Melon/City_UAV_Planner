"""读取模型配置并持久化网页选择的后端，不向网页返回密钥。"""

import os
from pathlib import Path

from dotenv import dotenv_values, set_key

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def read_settings():
    return {**dotenv_values(ENV_PATH), **os.environ}


def get_backend():
    settings = read_settings()
    backend = settings.get("LLM_BACKEND", "openai").strip().lower()
    if backend not in ("openai", "ollama"):
        raise ValueError("LLM_BACKEND 仅支持 openai 或 ollama")
    prefix = "OLLAMA" if backend == "ollama" else "OPENAI"
    default_model = "qwen3.5:4b-q4_K_M" if backend == "ollama" else "gpt-4.1-mini"
    model = settings.get(f"{prefix}_MODEL", default_model).strip()
    if not model:
        raise ValueError(f"{prefix}_MODEL 不能为空")
    return {"backend": backend, "model": model,
            "vision_model": settings.get(f"{prefix}_VISION_MODEL", "").strip() or model}


def set_backend(backend):
    if backend not in ("openai", "ollama"):
        raise ValueError("backend 仅支持 openai 或 ollama")
    if "LLM_BACKEND" in os.environ and os.environ["LLM_BACKEND"].strip().lower() != backend:
        raise ValueError("启动环境中的 LLM_BACKEND 正在覆盖配置，请先移除该环境变量再切换。")
    settings = read_settings()
    if backend == "openai" and not settings.get("OPENAI_API_KEY", "").strip():
        raise ValueError("请先在 llm/.env 填写 OPENAI_API_KEY，再切换云端模型。")
    prefix = "OLLAMA" if backend == "ollama" else "OPENAI"
    if f"{prefix}_MODEL" in settings and not settings[f"{prefix}_MODEL"].strip():
        raise ValueError(f"{prefix}_MODEL 不能为空")
    set_key(ENV_PATH, "LLM_BACKEND", backend)
    return get_backend()
