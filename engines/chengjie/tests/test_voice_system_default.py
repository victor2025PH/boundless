# -*- coding: utf-8 -*-
"""「系统通用音色」哨兵 + 坐席语音链契约对齐（2026-08-05 P0）。

背景（实录）：坐席下拉的「默认音色」＝空 persona_id ＝会话/账号人设回落链——
在会话绑到 hub allowlist 内人设 + ``voice_consistency=strict`` 时，hub 单点故障
会让「默认音色」硬拒发（hub_voice_source_unavailable），而显选 hub 名单外人设
（如小灵）反而正常 →「默认坏、具名好」的荒诞体验。本批修复：

1. 新哨兵 ``SYSTEM_VOICE_ID="__system__"``（系统通用音色）：钉死
   ``resolve_voice_cfg(None)`` 全局配置，绕过人设回落/contact 路由/hub 人设层
   （voice_cfg 无 persona_id → hub allowlist 早退 → strict 标记永不触发）。
2. 空串语义**不变**（跟随会话人设，防老会话静默换声），仅改 UI 文案。
3. 试听（tts-test）补会话上下文、发送（send-voice）补 account_persona_id ——
   试听=发送=自动链 同一组解析入参。
4. ``/api/voice/profiles`` 的 clone_backends 补 avatar_clone/minicpm_clone：
   生产主力克隆音色的 is_clone/ready 从此如实（缺授权=下拉置灰，而非点发送才炸）。
5. 失败可行动分类 ``classify_voice_error``：hub_source_down / profile_not_ready。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.ai.persona_voice import (
    SYSTEM_VOICE_ID,
    resolve_effective_voice_context,
    resolve_voice_cfg,
)
from src.ai.tts_pipeline import classify_voice_error


def _cfg() -> Dict[str, Any]:
    return {
        "telegram": {"voice_reply": {
            "backend": "avatar_clone",
            "voice": "global_voice",
            "voice_profile": {
                "enabled": True,
                "owner_consent": True,
                "backend": "avatar_clone",
                "speaker_id": "global_voice",
                "reference_audio_path": "config/voice_refs/global.wav",
            },
        }},
        "messenger_rpa": {"voice_output": {"format": "ogg"}},
    }


class _StubPM:
    """最小 PersonaManager 替身：会话绑定 warm_companion（自带 voice_profile）。"""

    def get_persona_by_id(self, pid):
        if pid in ("warm_companion", "p_avatar", "p_noconsent"):
            return dict(_PERSONAS[pid])
        return None

    def get_persona_with_tier(self, chat_key, account_persona_id, conversation_key=""):
        # tier 标签必须用 PersonaManager 真词表（conv_override/chat_binding/
        # account_profile/domain/default）——测试桩造假标签＝钉住假契约
        if chat_key:
            return dict(_PERSONAS["warm_companion"]), "chat_binding"
        if account_persona_id:
            p = self.get_persona_by_id(account_persona_id)
            if p:
                return p, "account_profile"
        return None, "fallback"

    def list_profiles_summary(self):
        return [
            {"id": "p_avatar", "name": "小艾", "has_voice": True},
            {"id": "p_noconsent", "name": "无授权", "has_voice": True},
        ]


_PERSONAS = {
    "warm_companion": {
        "id": "warm_companion", "name": "小灵",
        "voice_profile": {
            "enabled": True, "owner_consent": True, "backend": "avatar_clone",
            "speaker_id": "warm", "reference_audio_path": "config/voice_refs/warm.wav",
        },
    },
    "p_avatar": {
        "id": "p_avatar", "name": "小艾",
        "voice_profile": {
            "enabled": True, "owner_consent": True, "backend": "avatar_clone",
            "speaker_id": "ai", "reference_audio_path": "config/voice_refs/ai.wav",
        },
    },
    "p_noconsent": {
        "id": "p_noconsent", "name": "无授权",
        "voice_profile": {
            "enabled": True, "backend": "avatar_clone",
            "speaker_id": "nc", "reference_audio_path": "config/voice_refs/nc.wav",
            # 刻意缺 owner_consent —— 合成链会硬拒（voice_profile_requires_owner_consent）
        },
    },
}


@pytest.fixture
def stub_pm(monkeypatch):
    from src.utils.persona_manager import PersonaManager
    stub = _StubPM()
    monkeypatch.setattr(PersonaManager, "get_instance", classmethod(lambda cls: stub))
    return stub


# ── 1. 哨兵解析语义 ──────────────────────────────────────────────────────────


def test_system_sentinel_pins_global_and_skips_persona(stub_pm):
    """选「系统通用音色」：即使会话绑了人设，也钉死全局配置。"""
    ctx = resolve_effective_voice_context(
        _cfg(), persona_id=SYSTEM_VOICE_ID, chat_key="12345",
        platform="telegram", account_id="acct1")
    assert ctx["persona_source"] == "system"
    assert ctx["persona_id"] == ""
    assert ctx["persona"] == {}
    vc = ctx["voice_cfg"]
    # 全局声线，而非会话绑定人设（warm_companion）的声线
    assert vc["voice"] == "global_voice"
    assert vc["voice_profile"]["speaker_id"] == "global_voice"
    # hub 人设层早退的前提：voice_cfg 不携带 persona_id
    assert "persona_id" not in vc


def test_system_sentinel_keeps_variety_key(stub_pm):
    """听感连续性键与「选哪把声音」无关：哨兵路径仍注入 variety_key。"""
    ctx = resolve_effective_voice_context(
        _cfg(), persona_id=SYSTEM_VOICE_ID, chat_key="12345",
        platform="telegram", account_id="acct1")
    assert ctx["voice_cfg"]["variety_key"] == "telegram:acct1:12345"


def test_empty_persona_still_follows_chat_binding(stub_pm):
    """回归钉：空串语义不变——跟随会话绑定人设（老会话不换声）。"""
    ctx = resolve_effective_voice_context(
        _cfg(), persona_id=None, chat_key="12345",
        platform="telegram", account_id="acct1")
    assert ctx["persona_id"] == "warm_companion"
    assert ctx["persona_source"] == "chat_binding"
    assert ctx["voice_cfg"]["voice_profile"]["speaker_id"] == "warm"


def test_empty_persona_uses_account_persona_when_no_chat_binding(stub_pm):
    """account_persona_id 参与回落（send-voice/tts-test 本批补齐的入参）。"""
    ctx = resolve_effective_voice_context(
        _cfg(), persona_id=None, chat_key=None,
        account_persona_id="p_avatar", platform="telegram", account_id="a1")
    assert ctx["persona_id"] == "p_avatar"
    assert ctx["persona_source"] == "account_profile"


def test_resolve_voice_cfg_normalizes_sentinel(stub_pm):
    """散装调用方把哨兵当人设 id 传进 resolve_voice_cfg → 按 None 处理。"""
    assert resolve_voice_cfg(SYSTEM_VOICE_ID, _cfg()) == resolve_voice_cfg(None, _cfg())


# ── 2. 失败可行动分类 ────────────────────────────────────────────────────────


def test_classify_voice_error_buckets():
    assert classify_voice_error("hub_voice_source_unavailable") == "hub_source_down"
    assert classify_voice_error(
        "voice_profile_requires_owner_consent") == "profile_not_ready"
    assert classify_voice_error(
        "voice_profile_reference_audio_missing:/x/y.wav") == "profile_not_ready"
    assert classify_voice_error("avatar_clone_unreachable") == ""
    assert classify_voice_error("") == ""
    assert classify_voice_error(None) == ""


# ── 3. /api/voice/profiles 元数据修复 ───────────────────────────────────────


def _noop_auth(request: Request):
    return None


class _CfgMgr:
    def __init__(self, config):
        self.config = config


@pytest.fixture
def voice_client(stub_pm):
    from src.web.routes.voice_routes import register_voice_routes
    app = FastAPI()
    register_voice_routes(app, api_auth=_noop_auth, config_manager=_CfgMgr(_cfg()))
    return TestClient(app)


def test_profiles_marks_avatar_clone_as_clone_with_honest_ready(voice_client):
    """avatar_clone 计入克隆后端：is_clone 如实（修复前恒 False → ready 恒 True）。

    注意 p_noconsent 在**全局也是 avatar_clone** 的配置下 ready=True——
    `_merge_voice_profile` 同后端时人设 profile 继承全局 owner_consent，合成链
    读的也是这份合并结果（会放行）。ready 的不变量是「与合成行为一致」，
    不是「人设自己写没写 consent」。真正该置灰的场景见下一条（全局非克隆）。
    """
    d = voice_client.get("/api/voice/profiles").json()
    assert d["ok"] is True
    # 全局默认（=「系统通用音色」选项的实况）：avatar_clone + 授权齐 → 克隆且就绪
    assert d["default"]["is_clone"] is True
    assert d["default"]["ready"] is True
    rows = {p["persona_id"]: p for p in d["profiles"]}
    assert rows["p_avatar"]["is_clone"] is True
    assert rows["p_avatar"]["ready"] is True
    assert rows["p_noconsent"]["is_clone"] is True
    assert rows["p_noconsent"]["ready"] is True   # 继承全局 consent（与合成一致）


@pytest.fixture
def voice_client_plain_global(stub_pm):
    """全局=纯 edge（无克隆 profile）：人设克隆档无处继承 consent。"""
    from src.web.routes.voice_routes import register_voice_routes
    app = FastAPI()
    cfg = {
        "telegram": {"voice_reply": {"backend": "edge_tts",
                                     "voice": "zh-CN-XiaoxiaoNeural"}},
        "messenger_rpa": {"voice_output": {}},
    }
    register_voice_routes(app, api_auth=_noop_auth, config_manager=_CfgMgr(cfg))
    return TestClient(app)


def test_profiles_flags_unconsented_clone_when_no_global_inherit(
        voice_client_plain_global):
    """全局非克隆 → 无授权的 avatar_clone 人设 ready=False（下拉置灰=合成会拒）。

    这正是修复前的事故形态：is_clone=False → ready 恒 True → 坐席看着正常，
    点发送才撞 voice_profile_requires_owner_consent 硬失败。
    """
    d = voice_client_plain_global.get("/api/voice/profiles").json()
    assert d["ok"] is True
    assert d["default"]["is_clone"] is False     # 系统通用音色=纯 edge，恒就绪
    assert d["default"]["ready"] is True
    rows = {p["persona_id"]: p for p in d["profiles"]}
    assert rows["p_avatar"]["is_clone"] is True
    assert rows["p_avatar"]["ready"] is True     # 自带 consent+ref
    assert rows["p_noconsent"]["is_clone"] is True
    assert rows["p_noconsent"]["ready"] is False  # 缺 consent 且无处继承


# ── 3b. 断档台账持久化（P2：跨重启记忆）────────────────────────────────────


def test_outage_ledger_survives_restart(monkeypatch, tmp_path):
    """台账落盘：重启（新实例）后滚动窗/连败/来源分桶完整恢复——
    watchdog 告警与 hub recent_failures 预告不再因攒批重启失忆。"""
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    from src.ai.voice_outage import VoiceOutageLedger
    led = VoiceOutageLedger()
    led.record_voice_attempt(False, "manual", "hub_voice_source_unavailable")
    led.record_voice_attempt(False, "aline", "synth_failed")
    led2 = VoiceOutageLedger()          # 直接构造=模拟重启后重建
    snap = led2.outage_snapshot()
    assert snap["attempts_24h"] == 2 and snap["ok_24h"] == 0
    assert snap["consecutive_fails"] == 2
    assert "hub_voice_source_unavailable" in snap["fail_reasons"]
    assert snap["by_source"]["manual"]["attempts"] == 1
    assert snap["by_source"]["aline"]["attempts"] == 1


def test_outage_ledger_corrupt_file_starts_empty(monkeypatch, tmp_path):
    """坏文件＝空台账起步（软失败），下次记账原子覆盖坏文件后恢复可持久。"""
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    (tmp_path / "logs").mkdir(parents=True)
    (tmp_path / "logs" / "voice_outage_ledger.json").write_text(
        "{broken json", encoding="utf-8")
    from src.ai.voice_outage import VoiceOutageLedger
    led = VoiceOutageLedger()
    assert led.outage_snapshot()["attempts_24h"] == 0
    led.record_voice_attempt(True, "manual")
    led2 = VoiceOutageLedger()
    assert led2.outage_snapshot()["ok_24h"] == 1


def test_reset_for_test_clears_persisted_file(monkeypatch, tmp_path):
    """reset_for_test 契约保持：「下次 get＝空台账」——落盘后必须连文件一起清，
    否则所有依赖该契约的既有测试被静默串味。"""
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    from src.ai import voice_outage
    voice_outage.reset_for_test()
    voice_outage.get_voice_outage().record_voice_attempt(False, "manual", "x")
    assert (tmp_path / "logs" / "voice_outage_ledger.json").is_file()
    voice_outage.reset_for_test()
    assert not (tmp_path / "logs" / "voice_outage_ledger.json").is_file()
    assert voice_outage.get_voice_outage().outage_snapshot()["attempts_24h"] == 0
    voice_outage.reset_for_test()


def test_avatar_status_carries_outage(voice_client, monkeypatch, tmp_path):
    """ops 卡「出站健康」行的数据源：avatar-status 无条件带 outage 快照。"""
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    from src.ai import voice_outage
    voice_outage.reset_for_test()
    try:
        voice_outage.get_voice_outage().record_voice_attempt(
            False, "manual", "hub_voice_source_unavailable")
        d = voice_client.get("/api/voice/avatar-status").json()
        assert d["ok"] is True
        assert d["outage"]["attempts_24h"] == 1
        assert d["outage"]["ok_24h"] == 0
    finally:
        voice_outage.reset_for_test()


# ── 3c. 音色体检徽标（P3：录了但不像 → 下拉即示警）─────────────────────────


def test_profiles_carry_voice_quality_badge(voice_client, monkeypatch, tmp_path):
    """profiles 每行带 quality/quality_score（探针 jsonl 最新行；A/B 噪声行
    prosody=off 不算——那是基线数据，混入会把好音色标坏）。"""
    import json as _json

    import src.web.routes.voice_routes as vr
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    vr._VQ_CACHE["until"] = 0.0
    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    rows = [
        {"persona": "p_avatar", "score": 0.82, "label": "ok",
         "prosody": "on", "date": "2026-08-05"},
        {"persona": "p_noconsent", "score": 0.55, "label": "critical",
         "prosody": "on", "date": "2026-08-05"},
        # 同人设更晚的 off 行：必须被跳过，不得顶掉生产路径 on 行
        {"persona": "p_avatar", "score": 0.30, "label": "critical",
         "prosody": "off", "date": "2026-08-05"},
    ]
    (logs / "voice_similarity.jsonl").write_text(
        "\n".join(_json.dumps(r) for r in rows), encoding="utf-8")
    try:
        d = voice_client.get("/api/voice/profiles").json()
        m = {p["persona_id"]: p for p in d["profiles"]}
        assert m["p_avatar"]["quality"] == "ok"
        assert abs(float(m["p_avatar"]["quality_score"]) - 0.82) < 1e-6
        assert m["p_noconsent"]["quality"] == "critical"
    finally:
        vr._VQ_CACHE["until"] = 0.0


def test_profiles_quality_empty_without_jsonl(voice_client, monkeypatch, tmp_path):
    """无体检数据 → quality 空串（不出徽标），绝不因体检面阻塞选音色。"""
    import src.web.routes.voice_routes as vr
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    vr._VQ_CACHE["until"] = 0.0
    try:
        d = voice_client.get("/api/voice/profiles").json()
        assert all(p["quality"] == "" for p in d["profiles"])
    finally:
        vr._VQ_CACHE["until"] = 0.0


def test_spawn_voice_quality_probe_contract(monkeypatch):
    """enroll 即体检子进程：argv 契约（单人设 + 钉数据根 + 免 A/B）+ 单槽防重复。"""
    import subprocess

    import src.web.routes.voice_routes as vr

    calls = {}

    class _Proc:
        def poll(self):
            return None   # 仍在跑

    def _fake_popen(argv, **kw):
        calls["argv"] = list(argv)
        calls["cwd"] = kw.get("cwd")
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    vr._VQ_PROBE_PROC["proc"] = None
    try:
        assert vr._spawn_voice_quality_probe("lin_x") is True
        argv = calls["argv"]
        assert "scripts.voice_similarity_probe" in argv
        assert "--persona" in argv and "lin_x" in argv
        assert "--prosody-ab" in argv and "off" in argv
        assert "--data-root" in argv
        # 单槽防重复：上一轮仍在跑 → 不再拉第二个
        assert vr._spawn_voice_quality_probe("another") is False
        # 空 persona 拒绝
        vr._VQ_PROBE_PROC["proc"] = None
        assert vr._spawn_voice_quality_probe("") is False
    finally:
        vr._VQ_PROBE_PROC["proc"] = None


# ── 4. tts-test 契约：会话上下文 + 失败人话 ─────────────────────────────────


class _FakeResult:
    def __init__(self, ok=True, error="", path=""):
        self.ok = ok
        self.error = error
        self.audio_path = path
        self.provider = "fake"
        self.voice = "v"
        self.format = "mp3"
        self.duration_sec = 1.0
        self.extra = {}


def test_tts_test_passes_chat_context(voice_client, monkeypatch, tmp_path):
    """试听=发送 契约：body 里的会话上下文必须完整进 resolve（含账号人设）。"""
    import src.ai.persona_voice as pv
    import src.web.routes.voice_routes as vr
    monkeypatch.setattr(vr, "_TTS_PREVIEW_DIR", tmp_path / "pv")

    captured: Dict[str, Any] = {}
    real = pv.resolve_effective_voice_context

    def _capture(cfg, **kw):
        captured.update(kw)
        return real(cfg, **kw)

    monkeypatch.setattr(pv, "resolve_effective_voice_context", _capture)
    monkeypatch.setattr(
        pv, "resolve_account_persona_id", lambda cfg, plat, acct: "acct-p9")

    class _FakePipeline:
        def __init__(self, cfg=None):
            self.cfg = dict(cfg or {})

        async def synthesize(self, text, **kw):
            out = Path(self.cfg.get("out_dir") or ".")
            out.mkdir(parents=True, exist_ok=True)
            p = out / "a.mp3"
            p.write_bytes(b"0" * 1024)
            return _FakeResult(path=str(p))

    monkeypatch.setattr("src.ai.tts_pipeline.TTSPipeline", _FakePipeline)
    monkeypatch.setattr("src.licensing.quota_store.check_license_quota",
                        lambda *a, **k: {"allowed": True})

    r = voice_client.post("/api/voice/tts-test", json={
        "text": "你好", "chat_key": "777", "platform": "telegram",
        "account_id": "acct1"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert captured["chat_key"] == "777"
    assert captured["contact_key"] == "777"
    assert captured["platform"] == "telegram"
    assert captured["account_id"] == "acct1"
    assert captured["account_persona_id"] == "acct-p9"


# ── 5. hub 严格辖区判定 + 风险预告（P1 状态条数据源）────────────────────────


def test_hub_strict_scope_mirrors_pipeline_gate():
    from src.ai.tts_pipeline import hub_strict_scope
    av = {"hub_fish": {"enabled": True,
                       "persona_allowlist": ["lin_xiaoyu", "warm_companion"]},
          "voice_consistency": "strict"}
    assert hub_strict_scope(av, "lin_xiaoyu") is True
    assert hub_strict_scope(av, "not_listed") is False
    assert hub_strict_scope(av, "") is False          # 系统通用音色/无人设=辖区外
    assert hub_strict_scope(av, None) is False
    # 空 allowlist=全员辖区（与 _try_hub_fish 同口径）
    av_all = {"hub_fish": {"enabled": True}, "voice_consistency": "strict"}
    assert hub_strict_scope(av_all, "anyone") is True
    # lenient / 未启用 → 不构成「宁缺毋滥」辖区
    av_len = {"hub_fish": {"enabled": True}, "voice_consistency": "lenient"}
    assert hub_strict_scope(av_len, "lin_xiaoyu") is False
    av_off = {"hub_fish": {"enabled": False}, "voice_consistency": "strict"}
    assert hub_strict_scope(av_off, "lin_xiaoyu") is False
    assert hub_strict_scope(None, "x") is False


def test_probe_hub_reachable_semantics(monkeypatch):
    import src.ai.tts_pipeline as tp
    # 无 base_url → 无从探测 → True（不告警）
    assert tp.probe_hub_reachable({"hub_fish": {}}) is True
    # TCP 预检抛 RuntimeError → 确定不可达 → False
    monkeypatch.setattr(tp, "_assert_http_reachable",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("tts_host_unreachable:h:9000")))
    assert tp.probe_hub_reachable(
        {"hub_fish": {"base_url": "http://h:9000"}}) is False
    # 预检静默通过 → True
    monkeypatch.setattr(tp, "_assert_http_reachable", lambda *a, **k: None)
    assert tp.probe_hub_reachable(
        {"hub_fish": {"base_url": "http://h:9000"}}) is True


@pytest.fixture
def voice_client_hub(stub_pm):
    """会话绑定 warm_companion 且其在 hub 严格辖区内的实例形态。"""
    from src.web.routes.voice_routes import register_voice_routes
    app = FastAPI()
    cfg = _cfg()
    cfg["avatar_voice"] = {
        "hub_fish": {"enabled": True, "base_url": "http://192.0.2.1:9000",
                     "persona_allowlist": ["warm_companion"]},
        "voice_consistency": "strict",
    }
    register_voice_routes(app, api_auth=_noop_auth, config_manager=_CfgMgr(cfg))
    return TestClient(app)


def test_effective_config_resolves_chat_binding_and_flags_hub_risk(
        voice_client_hub, monkeypatch):
    """状态条契约：带 chat_key 时解析=发送同源（会话绑定人设），hub 不可达时
    hub_risk=unreachable —— 坐席在点发送**之前**就能看到「这音色现在发不出去」。"""
    import src.ai.tts_pipeline as tp
    monkeypatch.setattr(tp, "_assert_http_reachable",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("tts_host_unreachable:h:9000")))
    d = voice_client_hub.get(
        "/api/voice/effective-config",
        params={"platform": "telegram", "chat_key": "12345",
                "account_id": "acct1"}).json()
    assert d["ok"] is True
    assert d["persona_id"] == "warm_companion"     # 会话绑定，而非域默认
    assert d["persona_source"] == "chat_binding"
    assert d["is_clone"] is True and d["ready"] is True
    assert d["hub_strict"] is True
    assert d["hub_risk"] == "unreachable"


def test_effective_config_system_voice_never_hub_strict(voice_client_hub,
                                                        monkeypatch):
    """系统通用音色无 persona_id → 永远在 hub 辖区外（hub 全灭也照发）。"""
    import src.ai.tts_pipeline as tp
    monkeypatch.setattr(tp, "_assert_http_reachable",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("tts_host_unreachable:h:9000")))
    d = voice_client_hub.get(
        "/api/voice/effective-config",
        params={"platform": "telegram", "persona_id": "__system__",
                "chat_key": "12345", "account_id": "acct1"}).json()
    assert d["ok"] is True
    assert d["persona_source"] == "system"
    assert d["hub_strict"] is False
    assert d["hub_risk"] == ""


def test_effective_config_without_context_keeps_old_behavior(voice_client):
    """不带 chat_key/account_id = 渠道中心旧调用形态，解析行为不变。"""
    d = voice_client.get("/api/voice/effective-config",
                         params={"platform": "telegram"}).json()
    assert d["ok"] is True
    assert d["hub_strict"] is False and d["hub_risk"] == ""


def test_tts_test_failure_carries_actionable_reason(voice_client, monkeypatch,
                                                    tmp_path):
    """hub strict 拒发 → reason=hub_source_down + 本地化 message（人话）。"""
    import src.web.routes.voice_routes as vr
    monkeypatch.setattr(vr, "_TTS_PREVIEW_DIR", tmp_path / "pv")

    class _DownPipeline:
        def __init__(self, cfg=None):
            self.cfg = dict(cfg or {})

        async def synthesize(self, text, **kw):
            return _FakeResult(ok=False, error="hub_voice_source_unavailable")

    monkeypatch.setattr("src.ai.tts_pipeline.TTSPipeline", _DownPipeline)
    monkeypatch.setattr("src.licensing.quota_store.check_license_quota",
                        lambda *a, **k: {"allowed": True})

    d = voice_client.post("/api/voice/tts-test", json={"text": "你好"}).json()
    assert d["ok"] is False
    assert d["error"] == "hub_voice_source_unavailable"   # 机器码原样保留
    assert d["reason"] == "hub_source_down"
    assert "系统通用音色" in d.get("message", "")           # 指路下一步的人话
