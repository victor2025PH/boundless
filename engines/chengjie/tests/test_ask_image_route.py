# -*- coding: utf-8 -*-
"""P2 2026-08-19：/api/unified-inbox/ask-image「问这张图」端点契约测试。

覆盖参数闸/媒体类型闸/解析失败/配置门禁/成功路径（VLM 打桩，零 GPU）；
prompt 纯函数不变量一并钉住（不编造纪律 + 长度帽）。
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.media_enrich import build_ask_image_prompt
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes


class _Templates:
    def TemplateResponse(self, *a, **k):
        raise AssertionError("not used")


class FakeCM:
    def __init__(self, cfg):
        self.config = cfg


def _client(cfg=None):
    app = FastAPI()

    def _auth(request: Request):
        return True

    register_unified_inbox_routes(
        app, page_auth=_auth, api_auth=_auth, templates=_Templates())
    app.state.config_manager = FakeCM(cfg or {})
    return TestClient(app)


def _post(client, body):
    return client.post("/api/unified-inbox/ask-image", json=body)


def test_prompt_pure_invariants():
    p = build_ask_image_prompt("  这是什么   单据？ ")
    assert "这是什么 单据？" in p          # 空白折叠后原样入题
    assert "不要编造" in p and "图中看不到" in p   # 不编造纪律
    long_q = "问" * 500
    assert len(build_ask_image_prompt(long_q)) < 400   # 200 字问题帽生效


def test_question_required():
    r = _post(_client(), {"media_ref": "/x.png"})
    assert r.status_code == 400


def test_non_image_kind_rejected():
    r = _post(_client(), {"media_ref": "/x.ogg", "media_type": "voice", "question": "问"})
    assert r.status_code == 400


def test_media_not_found():
    r = _post(_client(cfg={"vision": {"enabled": True}}),
              {"media_ref": "/no/such/file_zzz.png", "media_type": "image", "question": "图里有什么"})
    assert r.status_code == 404


def test_vision_disabled():
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        r = _post(_client(cfg={"vision": {"enabled": False}}),
                  {"media_ref": path, "media_type": "image", "question": "图里有什么"})
        assert r.status_code == 503
    finally:
        os.remove(path)


def test_ok_with_stubbed_vlm():
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        with patch(
            "src.vision_client.VisionClient.describe_image_with_ollama_zhipu_fallback",
            new=AsyncMock(return_value=("一把机械键盘和一台显示器", "ollama_ok")),
        ) as vlm:
            r = _post(_client(cfg={"vision": {"enabled": True}}),
                      {"media_ref": path, "media_type": "image", "question": "图里有什么"})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True and d["answer"] == "一把机械键盘和一台显示器"
        assert d["provider"] == "ollama_ok"
        # prompt 必须走 build_ask_image_prompt（带问题 + 不编造纪律）
        sent_prompt = vlm.await_args.kwargs.get("prompt") or ""
        assert "图里有什么" in sent_prompt and "不要编造" in sent_prompt
    finally:
        os.remove(path)


def test_vlm_empty_answer_fails_honestly():
    """VLM 返空 → 502 如实失败（no_cloud_fallback 纪律，绝不静默编造）。"""
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        with patch(
            "src.vision_client.VisionClient.describe_image_with_ollama_zhipu_fallback",
            new=AsyncMock(return_value=("", "")),
        ):
            r = _post(_client(cfg={"vision": {"enabled": True}}),
                      {"media_ref": path, "media_type": "image", "question": "图里有什么"})
        assert r.status_code == 502
    finally:
        os.remove(path)
