# -*- coding: utf-8 -*-
"""对练台「客户方」LLM 调用（scripts/duel_runner.py 与 tools/duel_runner_tg.py 共用）。

2026-09-22 实施102 阶段 0 实锤：两个对练脚本默认打 **176 Ollama qwen3:30b**（``/api/chat`` +
``keep_alive: 30m``）——176 是语音与情绪主力机，一场对练就把 18.7G 显存钉住半小时，
IndexTTS 随即超时熔断、语音静默漂到 104 平声（Ollama server.log 02:14–02:17 每 8s 一发，
来源 117）。客户方改打 **173 vLLM chatx**（OpenAI 兼容 ``/v1/chat/completions``，主链模型
常驻，零额外显存），176 Ollama 不再对外供 chat。

协议按 base_url 自动判：以 ``/v1`` 结尾＝OpenAI 兼容（vLLM / 网关），否则＝Ollama 原生
``/api/chat``（保留给显式指回 Ollama 的场景，keep_alive 收到 5m，不再 30m 钉显存）。
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any, Dict, List, Sequence, Tuple

DEFAULT_BASE = "http://192.168.0.173:8001/v1"
DEFAULT_MODEL = "chatx"
OLLAMA_KEEP_ALIVE = "5m"


def is_openai_base(base: str) -> bool:
    return str(base or "").rstrip("/").endswith("/v1")


def build_chat_request(
    base: str, model: str, messages: Sequence[Dict[str, str]],
    *, temperature: float, max_tokens: int,
) -> Tuple[str, Dict[str, Any]]:
    """返回 (url, body)。纯函数，可单测。"""
    b = str(base or "").rstrip("/")
    msgs = [dict(m) for m in messages]
    if is_openai_base(b):
        return f"{b}/chat/completions", {
            "model": model, "messages": msgs, "stream": False,
            "temperature": float(temperature), "max_tokens": int(max_tokens),
        }
    return f"{b}/api/chat", {
        "model": model, "messages": msgs, "stream": False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "options": {"temperature": float(temperature), "num_predict": int(max_tokens)},
    }


def parse_chat_response(base: str, out: Dict[str, Any]) -> str:
    if is_openai_base(base):
        choices = out.get("choices") or []
        if not choices:
            return ""
        msg = (choices[0] or {}).get("message") or {}
        return str(msg.get("content") or "").strip()
    return str((out.get("message") or {}).get("content") or "").strip()


def chat_once(
    base: str, model: str, messages: Sequence[Dict[str, str]],
    *, temperature: float, max_tokens: int, timeout: float,
    api_key: str = "vllm",
) -> str:
    url, body = build_chat_request(base, model, messages,
                                   temperature=temperature, max_tokens=max_tokens)
    headers = {"Content-Type": "application/json"}
    if is_openai_base(base):
        # vLLM 未开 --api-key 时任意非空即可；网关形态由调用方传真 key
        headers["Authorization"] = f"Bearer {api_key or 'vllm'}"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read().decode("utf-8", "replace"))
    return parse_chat_response(base, out)
