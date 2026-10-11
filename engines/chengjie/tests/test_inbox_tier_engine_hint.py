"""收件箱翻译引擎提示按会话档，不按整箱。

标准档且本地模型没配好 → local_mt_gap。
同一部署里专业档会话不报这个缺口，effective 可以是付费引擎。
"""
from types import SimpleNamespace

from src.ai.translation_engines import DeepLEngine, EngineRouter
from src.ai.translation_service import TranslationService


def _paid_only_service():
    return TranslationService(
        engine_router=EngineRouter([DeepLEngine("k")]),
        free_tier_zero_cost=True,
    )


def test_engine_matrix_gap_only_on_free_tier():
    svc = _paid_only_service()
    free = svc.engine_matrix("en", tier="")
    assert free["tier"] == "std"
    assert free["local_mt_gap"] == "local_mt_unconfigured"
    assert free["effective"] == "none"
    pro = svc.engine_matrix("en", tier="pro")
    assert pro["tier"] == "pro"
    assert pro["local_mt_gap"] == ""
    assert pro["local_mt_ready"] is True
    assert pro["effective"] == "deepl"


def test_engine_matrix_legacy_has_no_gap():
    svc = TranslationService(
        engine_router=EngineRouter([DeepLEngine("k")]),
        free_tier_zero_cost=False,
    )
    m = svc.engine_matrix("en", tier="std")
    assert m["local_mt_gap"] == ""
    assert m["effective"] == "deepl"


def _store(tmp_path):
    from src.inbox.models import InboxConversation
    from src.inbox.normalizer import conv_id
    from src.inbox.store import InboxStore

    store = InboxStore(str(tmp_path / "inbox.db"))
    cid = conv_id("line", "default", "line:user:U1")
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="line", account_id="default",
        chat_key="line:user:U1", display_name="U1", language="en",
    ))
    return store, cid


def test_store_pref_tier_roundtrip(tmp_path):
    store, cid = _store(tmp_path)
    assert store.get_conversation(cid)["pref_tier"] == ""
    assert store.set_conversation_pref_tier(cid, "Pro") is True
    assert store.get_conversation(cid)["pref_tier"] == "pro"
    assert store.set_conversation_pref_tier(cid, "nope") is False
    assert store.get_conversation(cid)["pref_tier"] == "pro"
    assert store.set_conversation_pref_tier(cid, "") is True
    assert store.get_conversation(cid)["pref_tier"] == ""
    assert store.set_conversation_pref_tier("missing", "pro") is False


def test_effective_tier_remembers_explicit_and_reads_back(tmp_path):
    from src.web.routes.unified_inbox_services import (
        _effective_conv_translation_tier,
    )

    store, _cid = _store(tmp_path)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(inbox_store=store)))
    assert _effective_conv_translation_tier(
        request, "line", "default", "line:user:U1", "certified") == "certified"
    assert _effective_conv_translation_tier(
        request, "line", "default", "line:user:U1", "") == "certified"


def test_engines_endpoint_follows_conversation_tier(tmp_path):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from src.web.routes.unified_inbox_translate_routes import register_translate_routes

    store, cid = _store(tmp_path)
    store.set_conversation_pref_tier(cid, "pro")
    app = FastAPI()

    def api_auth(request: Request):
        return True

    register_translate_routes(app, api_auth=api_auth)
    app.state.inbox_store = store
    app.state.translation_service = _paid_only_service()
    client = TestClient(app)

    pro = client.get("/api/unified-inbox/translation-engines", params={
        "target_lang": "en", "platform": "line", "account_id": "default",
        "chat_key": "line:user:U1",
    })
    assert pro.status_code == 200
    body = pro.json()["matrix"]
    assert body["tier"] == "pro" and body["local_mt_gap"] == ""
    assert body["effective"] == "deepl"

    # 没开会话：按标准档，缺口仍在（健康巡检同口径）
    whole = client.get(
        "/api/unified-inbox/translation-engines", params={"target_lang": "en"})
    gap = whole.json()["matrix"]
    assert gap["tier"] == "std"
    assert gap["local_mt_gap"] == "local_mt_unconfigured"


def test_unknown_tier_does_not_replace_pinned_paid(tmp_path):
    from src.web.routes.unified_inbox_services import (
        _effective_conv_translation_tier,
    )

    store, _cid = _store(tmp_path)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(inbox_store=store)))
    assert _effective_conv_translation_tier(
        request, "line", "default", "line:user:U1", "pro") == "pro"
    assert _effective_conv_translation_tier(
        request, "line", "default", "line:user:U1", "nope") == "pro"
    assert store.get_conversation(_cid)["pref_tier"] == "pro"
    assert _effective_conv_translation_tier(
        request, "line", "default", "line:user:U1", "") == "pro"


def test_outbound_and_batch_read_stored_tier(tmp_path):
    import asyncio

    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from src.ai.translation_service import TranslationResult, TranslationService
    from src.inbox.outbound_translate import translate_outbound_text
    from src.web.routes.unified_inbox_translate_routes import register_translate_routes

    store, cid = _store(tmp_path)
    store.set_conversation_pref_tier(cid, "pro")

    class _Spy(TranslationService):
        def __init__(self):
            super().__init__(free_tier_zero_cost=True)
            self.tiers = []

        async def translate(self, text, **kw):
            self.tiers.append(kw.get("tier", ""))
            return TranslationResult(text, "hello", "zh", "en", True, provider="deepl")

        def detect_language(self, text):
            return "zh"

    spy = _Spy()

    class _Store:
        def get_outbound_lang_if_set(self, _cid):
            return "en"

        def get_conversation(self, _cid):
            return store.get_conversation(_cid)

        def record_outbound_translation(self, *args, **kwargs):
            return None

    out = asyncio.run(translate_outbound_text(
        {"conversation_id": cid, "text": "你好"},
        translation_service=spy, store=_Store(),
    ))
    assert out == "hello"
    assert spy.tiers == ["pro"]

    app = FastAPI()

    def api_auth(request: Request):
        return True

    register_translate_routes(app, api_auth=api_auth)
    app.state.inbox_store = store
    batch_spy = _Spy()
    app.state.translation_service = batch_spy
    client = TestClient(app)
    resp = client.post("/api/unified-inbox/translate-batch", json={
        "items": [{"id": "1", "text": "你好"}],
        "target_lang": "en",
        "platform": "line",
        "account_id": "default",
        "chat_key": "line:user:U1",
    })
    assert resp.status_code == 200
    assert batch_spy.tiers == ["pro"]
    assert store.get_conversation(cid)["pref_tier"] == "pro"
