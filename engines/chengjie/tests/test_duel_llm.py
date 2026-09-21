# -*- coding: utf-8 -*-
"""对练客户方 LLM 调用（scripts/_duel_llm.py）门禁——实施102 阶段 0。

钉住：默认端点是 173 vLLM chatx（不是 176 Ollama qwen3:30b）；/v1 走 OpenAI 体、否则 Ollama 原生
且 keep_alive 不再 30m；两个对练脚本的默认值都取自同一处。
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

_ENGINE = Path(__file__).resolve().parents[1]
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

from scripts import _duel_llm as dl  # noqa: E402


def test_default_is_173_vllm_not_176_ollama():
    assert dl.DEFAULT_BASE == "http://192.168.0.173:8001/v1"
    assert dl.DEFAULT_MODEL == "chatx"
    assert "176" not in dl.DEFAULT_BASE and "30b" not in dl.DEFAULT_MODEL


def test_openai_request_shape():
    url, body = dl.build_chat_request(
        "http://192.168.0.173:8001/v1/", "chatx",
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        temperature=0.9, max_tokens=120)
    assert url == "http://192.168.0.173:8001/v1/chat/completions"
    assert body["model"] == "chatx" and body["stream"] is False
    assert body["temperature"] == 0.9 and body["max_tokens"] == 120
    assert "keep_alive" not in body and "options" not in body
    assert body["messages"][1] == {"role": "user", "content": "u"}


def test_ollama_request_shape_keep_alive_short():
    url, body = dl.build_chat_request(
        "http://192.168.0.176:11434", "qwen3:30b-a3b-instruct-2507-q4_K_M",
        [{"role": "user", "content": "u"}], temperature=0.85, max_tokens=90)
    assert url == "http://192.168.0.176:11434/api/chat"
    assert body["keep_alive"] == "5m"                  # 不再 30m 钉显存
    assert body["options"] == {"temperature": 0.85, "num_predict": 90}
    assert "max_tokens" not in body


def test_parse_both_protocols():
    assert dl.parse_chat_response(
        "http://x/v1", {"choices": [{"message": {"content": "  hi "}}]}) == "hi"
    assert dl.parse_chat_response("http://x/v1", {"choices": []}) == ""
    assert dl.parse_chat_response(
        "http://x:11434", {"message": {"content": " yo\n"}}) == "yo"


def test_chat_once_openai_adds_bearer(monkeypatch):
    seen = {}

    class _Resp:
        def __init__(self, payload):
            self._p = payload

        def read(self):
            import json
            return json.dumps(self._p).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        return _Resp({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(dl.urllib.request, "urlopen", fake_urlopen)
    out = dl.chat_once("http://192.168.0.173:8001/v1", "chatx",
                       [{"role": "user", "content": "u"}],
                       temperature=0.5, max_tokens=10, timeout=3)
    assert out == "ok"
    assert seen["url"].endswith("/v1/chat/completions")
    assert seen["auth"] == "Bearer vllm"


@pytest.mark.parametrize("rel", ["scripts/duel_runner.py", "tools/duel_runner_tg.py"])
def test_runners_default_to_shared_llm_and_drop_176(rel):
    src = (_ENGINE / rel).read_text(encoding="utf-8")
    # 176 Ollama 只许出现在注释/说明里，不许再当默认值
    for line in src.splitlines():
        if "192.168.0.176:11434" in line:
            assert "default=" not in line, line
    assert "LLM_DEFAULT_BASE" in src and "LLM_DEFAULT_MODEL" in src
    assert "chat_once(" in src
    assert 'keep_alive": "30m"' not in src
    spec = importlib.util.spec_from_file_location(rel.replace("/", "_"), _ENGINE / rel)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)          # 可导入：import 路径接线正确
    assert mod.LLM_DEFAULT_BASE == dl.DEFAULT_BASE
