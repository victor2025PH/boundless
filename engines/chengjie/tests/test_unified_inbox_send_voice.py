"""收件箱「语音回复（文本→声音克隆TTS→发语音）」契约回归。

锁定 POST /api/unified-inbox/send-voice 的对外契约：
  1. 缺 text/chat_key → 400（前端 sendVoiceReply 依赖此校验）
  2. 非 protocol/未在线账号 → 501（前端据此提示"需协议多开在线账号"）
合成+发送的正路依赖 TTS 后端 + 在线协议账号，属环境相关，不在单测内打真实外呼。
"""


def test_send_voice_requires_text(auth_client):
    r = auth_client.post(
        "/api/unified-inbox/send-voice",
        json={"platform": "telegram", "account_id": "default", "chat_key": "123"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_send_voice_requires_chat_key(auth_client):
    r = auth_client.post(
        "/api/unified-inbox/send-voice",
        json={"platform": "telegram", "account_id": "default", "text": "你好"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_send_voice_non_protocol_returns_501(auth_client):
    """无在线协议账号时须 501（而非 500/静默），前端据 detail 提示走 RPA voice_output。"""
    r = auth_client.post(
        "/api/unified-inbox/send-voice",
        json={
            "platform": "telegram", "account_id": "no-such-acct",
            "chat_key": "123", "text": "你好，这是一条语音测试",
        },
        follow_redirects=False,
    )
    assert r.status_code == 501


def test_send_voice_lang_mismatch_rejects(auth_client, monkeypatch):
    """语言路由拒发守卫：语种明确但无音色映射 → ok:false + reason=lang_mismatch，
    不进 TTS（发错语言的语音比不发更糟；与 voice_autosend / 原生 TG 同口径）。"""
    import src.ai.lang_voice_route as lvr
    import src.integrations.account_orchestrator as _ao

    class _Orch:
        def owns_media(self, p, a):
            return True

    monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())
    monkeypatch.setattr(
        lvr, "route_voice_cfg_for_text",
        lambda vc, text, cfg: (dict(vc or {}), "reject:xx"))

    synth_called = {"n": 0}
    from src.ai.tts_pipeline import TTSPipeline

    async def _synth(self, *a, **k):
        synth_called["n"] += 1

    monkeypatch.setattr(TTSPipeline, "synthesize", _synth)

    r = auth_client.post(
        "/api/unified-inbox/send-voice",
        json={"platform": "telegram", "account_id": "default",
              "chat_key": "123", "text": "hello world sample text"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    d = r.json()
    assert d.get("ok") is False
    assert d.get("reason") == "lang_mismatch"
    assert synth_called["n"] == 0


def test_send_voice_explicit_override_skips_lang_route(auth_client, monkeypatch):
    """坐席显式覆写 voice → 尊重人工选择，不走语言路由（路由函数不被调用）。"""
    import types as _types

    import src.ai.lang_voice_route as lvr
    import src.integrations.account_orchestrator as _ao

    class _Orch:
        def owns_media(self, p, a):
            return True

    monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())
    route_calls = {"n": 0}

    def _route(vc, text, cfg):
        route_calls["n"] += 1
        return dict(vc or {}), ""

    monkeypatch.setattr(lvr, "route_voice_cfg_for_text", _route)
    from src.ai.tts_pipeline import TTSPipeline

    async def _synth(self, *a, **k):
        return _types.SimpleNamespace(ok=False, error="stop-here", audio_path="")

    monkeypatch.setattr(TTSPipeline, "synthesize", _synth)

    r = auth_client.post(
        "/api/unified-inbox/send-voice",
        json={"platform": "telegram", "account_id": "default",
              "chat_key": "123", "text": "hello world sample text",
              "voice_cfg_override": {"voice": "ja-JP-NanamiNeural"}},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert r.json().get("ok") is False        # 合成失败（预期，fake）
    assert route_calls["n"] == 0              # 显式覆写 → 路由被跳过


def test_send_voice_passes_account_persona(auth_client, monkeypatch):
    """2026-08-05 P0：send-voice 必须带 account_persona_id 进解析器（与 A 线
    sender 同口径）——缺它时 chat 未绑定人设会回落到域默认，与自动语音链分叉
    （同一会话 AI 自动发一种声、坐席手动发另一种声）。"""
    import src.ai.persona_voice as pv
    import src.integrations.account_orchestrator as _ao

    class _Orch:
        def owns_media(self, p, a):
            return True

    monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())
    monkeypatch.setattr(
        pv, "resolve_account_persona_id", lambda cfg, plat, acct: "acct-p42")

    captured = {}
    real = pv.resolve_effective_voice_context

    def _capture(cfg, **kw):
        captured.update(kw)
        return real(cfg, **kw)

    monkeypatch.setattr(pv, "resolve_effective_voice_context", _capture)

    import types as _types
    from src.ai.tts_pipeline import TTSPipeline

    async def _synth(self, *a, **k):
        return _types.SimpleNamespace(ok=False, error="stop-here", audio_path="")

    monkeypatch.setattr(TTSPipeline, "synthesize", _synth)

    r = auth_client.post(
        "/api/unified-inbox/send-voice",
        json={"platform": "telegram", "account_id": "default",
              "chat_key": "123", "text": "hello sample"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert captured.get("account_persona_id") == "acct-p42"
    assert captured.get("chat_key") == "123"


def test_send_voice_hub_down_gives_actionable_message(auth_client, monkeypatch):
    """hub strict 拒发 → reason 保留机器码，message 换成指路人话
    （「换系统通用音色/其他就绪音色」而非裸错误码）。"""
    import types as _types

    import src.integrations.account_orchestrator as _ao
    from src.ai.tts_pipeline import TTSPipeline

    class _Orch:
        def owns_media(self, p, a):
            return True

    monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())

    async def _synth(self, *a, **k):
        return _types.SimpleNamespace(
            ok=False, error="hub_voice_source_unavailable", audio_path="")

    monkeypatch.setattr(TTSPipeline, "synthesize", _synth)

    r = auth_client.post(
        "/api/unified-inbox/send-voice",
        json={"platform": "telegram", "account_id": "default",
              "chat_key": "123", "text": "你好呀"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    d = r.json()
    assert d.get("ok") is False
    assert d.get("reason") == "hub_voice_source_unavailable"
    assert "系统通用音色" in str(d.get("message") or "")


def test_send_voice_failure_feeds_outage_ledger(auth_client, monkeypatch):
    """2026-08-05 P1：坐席手动链失败必须进 voice_outage 台账（source=manual）
    ——否则自动链低流量时，坐席连败是最早断档信号却进不了 watchdog 告警窗。"""
    import types as _types

    import src.integrations.account_orchestrator as _ao
    from src.ai import voice_outage
    from src.ai.tts_pipeline import TTSPipeline

    class _Orch:
        def owns_media(self, p, a):
            return True

    monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())

    async def _synth(self, *a, **k):
        return _types.SimpleNamespace(
            ok=False, error="hub_voice_source_unavailable", audio_path="")

    monkeypatch.setattr(TTSPipeline, "synthesize", _synth)

    voice_outage.reset_for_test()
    try:
        r = auth_client.post(
            "/api/unified-inbox/send-voice",
            json={"platform": "telegram", "account_id": "default",
                  "chat_key": "123", "text": "你好"},
            follow_redirects=False,
        )
        assert r.status_code == 200 and r.json().get("ok") is False
        snap = voice_outage.get_voice_outage().outage_snapshot()
        assert snap["attempts_24h"] == 1
        assert snap["ok_24h"] == 0
        assert snap["by_source"].get("manual", {}).get("attempts") == 1
        assert "hub_voice_source_unavailable" in snap["fail_reasons"]
    finally:
        voice_outage.reset_for_test()


def test_send_voice_policy_reject_not_counted_as_outage(auth_client, monkeypatch):
    """语言路由拒发=策略早退（正常决策），不得记入断档台账（污染告警判据）。"""
    import src.ai.lang_voice_route as lvr
    import src.integrations.account_orchestrator as _ao
    from src.ai import voice_outage

    class _Orch:
        def owns_media(self, p, a):
            return True

    monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())
    monkeypatch.setattr(
        lvr, "route_voice_cfg_for_text",
        lambda vc, text, cfg: (dict(vc or {}), "reject:xx"))

    voice_outage.reset_for_test()
    try:
        r = auth_client.post(
            "/api/unified-inbox/send-voice",
            json={"platform": "telegram", "account_id": "default",
                  "chat_key": "123", "text": "hello world sample"},
            follow_redirects=False,
        )
        assert r.json().get("reason") == "lang_mismatch"
        snap = voice_outage.get_voice_outage().outage_snapshot()
        assert snap["attempts_24h"] == 0
    finally:
        voice_outage.reset_for_test()


def test_voice_profiles_contract(auth_client):
    """按人设选音色：/api/voice/profiles 须返回 {ok, default, profiles[]}，
    每个 profile 带 persona_id/name/is_clone/ready（前端音色下拉依赖这些字段）。"""
    r = auth_client.get("/api/voice/profiles", follow_redirects=False)
    assert r.status_code == 200
    d = r.json()
    assert d.get("ok") is True
    assert isinstance(d.get("default"), dict)
    assert isinstance(d.get("profiles"), list)
    for p in d["profiles"]:
        assert "persona_id" in p and "name" in p
        assert "is_clone" in p and "ready" in p
