"""批量翻译端点 /api/unified-inbox/translate-batch（2026-08-09 提速批次）。

契约：一次往返译整批消息；逐条复用与 /translate 同一 TranslationService
（缓存/术语/会话首选引擎），每条独立成败不拖垮整批；auto 解析不出整批不译；
条目 50 封顶超出计 skipped。
"""
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.ai.translation_engines import EngineResult
from src.ai.translation_service import TranslationService


class _TextSwitchEngine:
    """按文本内容分流成败的桩引擎：含 FAIL 的文本抛异常，其余回「译:<原文>」。"""

    name = "ai"
    available = True

    def supports_target(self, target_lang):
        return True

    async def translate(self, text, *, source_lang, target_lang, style="chat",
                        glossary_hint=""):
        if "FAIL" in text:
            raise RuntimeError("boom")
        return EngineResult(f"译:{text}", self.name, True)


def _app(svc):
    from src.web.routes.unified_inbox_translate_routes import register_translate_routes

    app = FastAPI()

    def api_auth(request: Request):
        return True

    register_translate_routes(app, api_auth=api_auth)
    app.state.translation_service = svc
    return app


def _client():
    return TestClient(_app(TranslationService(engines=[_TextSwitchEngine()])))


def test_batch_translates_items_and_keeps_ids():
    client = _client()
    r = client.post("/api/unified-inbox/translate-batch", json={
        "target_lang": "zh",
        "items": [
            {"id": "m1", "text": "hello there", "source_lang": "en"},
            {"id": "m2", "text": "how are you", "source_lang": "en"},
            {"id": "m3", "text": "你好呀"},  # 源语==目标语 → identity，零引擎调用
        ],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["resolved_target"] == "zh"
    assert body["skipped"] == 0
    by = {it["id"]: it["translation"] for it in body["items"]}
    assert by["m1"]["ok"] and by["m1"]["translated_text"] == "译:hello there"
    assert by["m2"]["ok"] and by["m2"]["translated_text"] == "译:how are you"
    assert by["m3"]["ok"] and by["m3"]["provider"] == "identity"


def test_batch_item_failure_does_not_sink_the_rest():
    client = _client()
    r = client.post("/api/unified-inbox/translate-batch", json={
        "target_lang": "zh",
        "items": [
            {"id": "good", "text": "see you tomorrow", "source_lang": "en"},
            {"id": "bad", "text": "FAIL this one", "source_lang": "en"},
        ],
    })
    body = r.json()
    assert body["ok"] is True  # 整批请求成功，成败在条目级
    by = {it["id"]: it["translation"] for it in body["items"]}
    assert by["good"]["ok"] is True
    assert by["bad"]["ok"] is False
    # 失败条回落原文（调用方直接显示原文，不显示半截译文）
    assert by["bad"]["translated_text"] == "FAIL this one"


def test_batch_auto_target_unresolved_returns_empty():
    # 无 inbox_store / 会话语言未知 → auto 解析不出 → 整批不译，前端回落原文
    client = _client()
    r = client.post("/api/unified-inbox/translate-batch", json={
        "target_lang": "auto",
        "platform": "telegram", "account_id": "default", "chat_key": "c1",
        "items": [{"id": "m1", "text": "hola", "source_lang": "es"}],
    })
    body = r.json()
    assert body["ok"] is False and body["resolved_target"] == ""
    assert body["items"] == []


def test_batch_caps_items_and_reports_skipped():
    client = _client()
    items = [{"id": f"m{i}", "text": f"msg number {i}", "source_lang": "en"}
             for i in range(55)]
    r = client.post("/api/unified-inbox/translate-batch",
                    json={"target_lang": "zh", "items": items})
    body = r.json()
    assert body["ok"] is True
    assert len(body["items"]) == 50
    assert body["skipped"] == 5


def test_batch_empty_items_ok():
    client = _client()
    r = client.post("/api/unified-inbox/translate-batch",
                    json={"target_lang": "zh", "items": []})
    body = r.json()
    assert body["ok"] is True and body["items"] == [] and body["skipped"] == 0
