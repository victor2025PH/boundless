# -*- coding: utf-8 -*-
"""未成年硬停 × 成人不设限（2026-09-19，173 陪伴机复盘第二轮）。

背景：R88 起高敏类别默认「只记录」，``minor`` 要运营在回复设置页显式锁定才停 AI。成人政策 ``open``
把露骨 / 施压整条链让路之后，「疑似未成年立刻停」只剩 prompt 里一句话——那是给模型的提示，不是闸。

本文件锁三件事：
  ① ``risk_grader._MINOR`` 词表覆盖常见自述变体（im 16 / 17yo / sixteen / turned 16 / high school /
     我今年16 / 在读高一），且叙述 / 计量 / 相对年龄不误伤；
  ② ``adult_policy=open``（人设或机器默认）⇒ ``minor`` **隐含锁定**：不靠 ``inbox.risk_grading.locked``，
     命中即 high + L1 + 需人工 + risk_hold；非 open 政策维持 R88 只记录（L2）——默认行为零变化；
  ③ drafts 全链：adult_grader（open 让路）→ risk_grader（minor 隐含锁）顺序下，成人主因被剥、
     ``risk:minor`` 仍撑起 high。
"""
from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi import FastAPI, Request   # 顶层导入：__future__ annotations 下 FastAPI 需在模块 globals 解析 Request
from fastapi.testclient import TestClient

from src.ai.chat_assistant_service import quick_risk
from src.inbox import adult_grader as ag
from src.inbox import autosend_policy as pol
from src.inbox import autosend_shadow_log as shadow_log
from src.inbox import risk_grader as rg
from src.inbox import risk_hold
from src.inbox.drafts import DraftService
from src.inbox.normalizer import conv_id
from src.inbox.store import InboxStore
from src.integrations.protocol_autoreply import HANDOFF_TAG

_PLAT, _ACCT = "telegram", "acct_minor"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv(shadow_log.ENV_DIR, str(tmp_path / "shadow"))
    monkeypatch.delenv(pol.ENV_POLICY_MODE, raising=False)
    shadow_log.get_stats().reset()
    shadow_log._reset_pending_for_tests()
    st = InboxStore(tmp_path / "inbox.db")
    yield st
    st.close()


@pytest.fixture
def svc(store, monkeypatch):
    from src.utils import business_domain as bd
    monkeypatch.setattr(bd, "_ACTIVE", None)
    return DraftService(inbox_store=store, risk_fn=quick_risk)


def _conv(ck: str) -> Dict[str, Any]:
    return {"conversation_id": conv_id(_PLAT, _ACCT, ck), "platform": _PLAT,
            "account_id": _ACCT, "chat_key": ck, "display_name": "T"}


# ── ① 词表 ───────────────────────────────────────────────────────────────────

_MINOR_POSITIVE = [
    "I'm 16 and you're so sexy",
    "I am 15 years old btw",
    "im 17 lol",
    "I'm 17yo",
    "I’m 16 y/o",                      # curly apostrophe
    "I'm sixteen, is that a problem?",
    "just turned 16 last week",
    "I'm still in high school",
    "I'm a highschooler",
    "I'm not 18 yet",
    "I'm still 17",
    "I'm under 18",
    "I'm a minor",
    "我今年16",
    "我才15岁",
    "我在读高一",
    "我还是高中生",
    "我还没成年",
    "16歳だよ",
    "高校生です",
]

_MINOR_NEGATIVE = [
    "when I was 15 I loved this band",
    "my nephew is 12 and obsessed with dinosaurs",
    "I'm 18 and just started college",
    "I'm 15 minutes away",
    "I'm 16 years older than you",
    "I'm 17k in debt honestly",
    "I'm 10 mins late sorry",
    "high school was a long time ago",
    "我17号到",
    "我高中时候的事了",
    "我儿子今年16岁",
]


@pytest.mark.parametrize("text", _MINOR_POSITIVE)
def test_minor_regex_positive(text):
    assert rg.grade(text, "in")["category"] == "minor", text


@pytest.mark.parametrize("text", _MINOR_NEGATIVE)
def test_minor_regex_negative(text):
    assert rg.grade(text, "in")["category"] != "minor", text


# ── ② 隐含锁定语义 ────────────────────────────────────────────────────────────

def test_implied_lock_only_under_open():
    assert rg.implied_locked({}) == []
    assert rg.implied_locked({"adult_grader": {"default_policy": "soft_reply"}}) == []
    assert rg.implied_locked({"adult_grader": {"default_policy": "open"}}) == ["minor"]
    assert rg.implied_locked({}, {"boundaries": {"adult_policy": "open"}}) == ["minor"]
    assert rg.implied_locked({}, {"boundaries": {"adult_policy": "mark_only"}}) == []


def test_is_locked_effective_reports_source():
    open_cfg = {"adult_grader": {"default_policy": "open"}}
    assert rg.is_locked_effective("minor", open_cfg) == (True, "adult_open")
    assert rg.is_locked_effective("minor", {"inbox": {"risk_grading": {"locked": ["minor"]}}}) == (True, "config")
    assert rg.is_locked_effective("minor", {}) == (False, "")
    # 只锁 minor：open 不牵连别的类别
    assert rg.is_locked_effective("scam", open_cfg) == (False, "")
    assert rg.is_locked_effective("self_harm", open_cfg) == (False, "")
    # 显式 is_locked（UI 开关那份）不受隐含影响
    assert rg.is_locked("minor", open_cfg) is False


def test_first_locked_hit_sees_implied_lock():
    open_cfg = {"adult_grader": {"default_policy": "open"}}
    assert rg.first_locked_hit(["adult_flirt", "risk:minor"], open_cfg) == "minor"
    assert rg.first_locked_hit(["adult_flirt", "risk:minor"], {}) == ""


# ── ③ 全链 ──────────────────────────────────────────────────────────────────

_TEXT = "I'm 16 and you're so sexy, talk dirty to me"


def test_open_policy_minor_hits_l1_needs_human_and_hold(svc, store):
    svc._cfg = {"adult_grader": {"default_policy": "open"}}   # 不写 inbox.risk_grading.locked
    conv = _conv("open_minor")
    cid = conv["conversation_id"]
    did = svc.auto_generate_draft(conv, _TEXT, automation_mode="auto_ai")
    assert did
    row = store.get_draft(did)
    assert row["autopilot_level"] == "L1", row
    assert row["risk_level"] == "high", row
    reasons = list(row.get("risk_reasons") or [])
    assert "risk:minor" in reasons, reasons
    # 成人主因被 open 让路剥掉，撑起 high 的只剩未成年
    assert not any(r == "adult" or str(r).startswith("adult:") for r in reasons), reasons
    assert HANDOFF_TAG in list(store.get_conv_tags(cid) or [])
    assert risk_hold.active(store, cid) is not None
    hm = store.get_handoff_meta(cid) or {}
    assert hm.get("category") == "minor" and hm.get("level") == "high", hm
    # 锁定类留白：不给未成年拟任何回复
    assert not (row.get("draft_text") or "").strip(), row.get("draft_text")


def test_open_policy_adult_only_still_flows_l2(svc, store):
    """对照：同一 open 机器，成年人的露骨 + 施压不被拦（第一轮已验，这里防回归）。"""
    svc._cfg = {"adult_grader": {"default_policy": "open"}}
    conv = _conv("open_adult")
    did = svc.auto_generate_draft(conv, "send me your nudes right now, don't be shy", automation_mode="auto_ai")
    row = store.get_draft(did)
    assert row["autopilot_level"] == "L2", row
    assert HANDOFF_TAG not in list(store.get_conv_tags(conv["conversation_id"]) or [])


def test_non_open_unlocked_minor_stays_record_only(svc, store):
    """R88 默认零变化：非 open 政策、未显式锁定 → minor 只记录，档位 L2。"""
    svc._cfg = {"adult_grader": {"default_policy": "soft_reply"}}
    conv = _conv("soft_minor")
    cid = conv["conversation_id"]
    did = svc.auto_generate_draft(conv, "I'm 16 and you're so sexy", automation_mode="auto_ai")
    row = store.get_draft(did)
    assert row["autopilot_level"] == "L2", row
    assert HANDOFF_TAG not in list(store.get_conv_tags(cid) or [])
    assert risk_hold.active(store, cid) is None


def test_reply_settings_api_reports_machine_default_and_implied_lock(tmp_path):
    """回复设置页 API：机器级默认 → ``adult_source=config``；open ⇒ ``implied_locked=[minor]``（UI 据此亮徽章 / 锁标）。"""
    from src.utils.persona_manager import PersonaManager
    from src.web.routes.reply_settings_routes import register_reply_settings_routes

    class _CM:
        def __init__(self, cfg):
            self.config_path = str(tmp_path / "config" / "config.yaml")
            (tmp_path / "config").mkdir(parents=True, exist_ok=True)
            self.config = cfg

    async def _noop(request: Request):
        return None

    PersonaManager.reset()
    try:
        for cfg, want_src, want_locked in (
            ({"adult_grader": {"default_policy": "open"}}, "config", ["minor"]),
            ({"adult_grader": {"default_policy": "soft_reply"}}, "config", []),
            ({}, None, []),
        ):
            app = FastAPI()
            cm = _CM(cfg)
            app.state.config_manager = cm
            register_reply_settings_routes(app, page_auth=_noop, api_auth=_noop, templates=None, config_manager=cm)
            resp = TestClient(app).get("/api/reply-settings/risk-grader")
            d = resp.json()
            assert d.get("ok") is True, (resp.status_code, resp.text[:400])
            assert d["implied_locked"] == want_locked, (cfg, d["implied_locked"])
            assert d["locked"] == []                       # 显式锁定那份不受影响
            row = next(r for r in d["categories"] if r["id"] == "minor")
            assert row["locked"] is False                  # 开关那份仍是「运营没勾」
            if want_locked:
                assert row["locked_by"] == "adult_open" and row["outcome"] == "stop", row
            else:
                assert row["locked_by"] == "" and row["outcome"] == "record", row
            pd = d["policy_defaults"]
            if want_src:
                assert pd["adult_source"] == want_src and pd["adult"] == cfg["adult_grader"]["default_policy"], pd
            else:
                assert pd["adult_source"] in ("default", "default_companion"), pd
    finally:
        PersonaManager.reset()


def test_regrade_chain_open_then_minor_direct(svc, store):
    """两个钩子按 drafts 顺序直连：adult 让路后 risk_grader 仍把 minor 撑到 high（info 带 locked_by）。"""
    cfg = {"adult_grader": {"default_policy": "open"}}
    svc._cfg = cfg
    conv = _conv("chain")
    risk, reasons, ainfo = ag.regrade_inbound(svc, conv, _TEXT, "en", "high", ["adult"], ["sexy"], cfg=cfg)
    assert ainfo and ainfo["policy"] == "open" and ainfo["needs_human"] is False
    assert risk == "low" and "adult" not in reasons
    risk2, reasons2, rinfo = rg.regrade_inbound(svc, conv, _TEXT, "en", risk, reasons, ["sexy"], cfg=cfg)
    assert risk2 == "high"
    assert rinfo["category"] == "minor" and rinfo["locked"] is True and rinfo["locked_by"] == "adult_open"
    assert "risk:minor" in reasons2


def test_first_locked_hit_persona_open_without_machine_default():
    """人设 open、机器默认未开：隐含锁仍必须看见 minor（协议链漏 persona 就会放行）。"""
    persona = {"boundaries": {"adult_policy": "open"}}
    assert rg.first_locked_hit(["risk:minor"], {}, persona) == "minor"
    assert rg.first_locked_hit(["risk:minor"], {}) == ""


@pytest.mark.asyncio
async def test_protocol_autoreply_stops_minor_when_machine_open(monkeypatch):
    """协议直发链：quick_analyze 没有 minor 词表，必须走 risk_grader 才能停。"""
    from src.integrations import protocol_autoreply as pa
    from src.utils.persona_manager import PersonaManager

    pa._last_reply.clear()
    pa._last_sent.clear()
    sent: list = []
    gen_called: list = []

    async def _gen(**kw):
        gen_called.append(kw)
        return "should not generate"

    async def _send(**kw):
        sent.append(kw)
        return {"delivered": True}

    class _Reg:
        def get(self, platform, account_id):
            return {"platform": platform, "account_id": account_id,
                    "meta": {"auto_reply": True, "persona_id": "claire_brennan"}}

    PersonaManager.reset()
    try:
        monkeypatch.setattr(
            PersonaManager, "get_persona_by_id",
            lambda self, pid: {"id": pid, "boundaries": {}},  # 无人设政策 → 吃机器默认 open
        )
        res = await pa.run_autoreply(
            {"platform": "telegram", "account_id": "tg1", "chat_key": "99",
             "text": "I'm 16 and you're so sexy", "direction": "in"},
            registry=_Reg(),
            cfg={"protocol_autoreply": {"enabled": True},
                 "adult_grader": {"default_policy": "open"}},
            generate=_gen, send=_send, risk_fn=lambda t: "low",
        )
    finally:
        PersonaManager.reset()
        pa._last_reply.clear()
        pa._last_sent.clear()
    assert res.get("skipped") == "minor", res
    assert sent == [] and gen_called == []


@pytest.mark.asyncio
async def test_protocol_autoreply_stops_minor_when_only_persona_open(monkeypatch):
    """机器默认不是 open、只人设 open：协议链也必须停（修 first_locked_hit 不传 persona 的洞）。"""
    from src.integrations import protocol_autoreply as pa
    from src.utils.persona_manager import PersonaManager

    pa._last_reply.clear()
    pa._last_sent.clear()
    sent: list = []

    async def _gen(**kw):
        return "should not generate"

    async def _send(**kw):
        sent.append(kw)
        return {"delivered": True}

    class _Reg:
        def get(self, platform, account_id):
            return {"platform": platform, "account_id": account_id,
                    "meta": {"auto_reply": True, "persona_id": "claire_brennan"}}

    PersonaManager.reset()
    try:
        monkeypatch.setattr(
            PersonaManager, "get_persona_by_id",
            lambda self, pid: {"id": pid, "boundaries": {"adult_policy": "open"}},
        )
        res = await pa.run_autoreply(
            {"platform": "telegram", "account_id": "tg1", "chat_key": "98",
             "text": "I'm 16 and you're so sexy", "direction": "in"},
            registry=_Reg(),
            cfg={"protocol_autoreply": {"enabled": True}},
            generate=_gen, send=_send, risk_fn=lambda t: "low",
        )
    finally:
        PersonaManager.reset()
        pa._last_reply.clear()
        pa._last_sent.clear()
    assert res.get("skipped") == "minor", res
    assert sent == []


@pytest.mark.asyncio
async def test_protocol_autoreply_adult_without_minor_still_sends_when_open(monkeypatch):
    """对照：open 下成年人露骨仍直发（未成年闸不得误伤）。"""
    from src.integrations import protocol_autoreply as pa
    from src.utils.persona_manager import PersonaManager

    pa._last_reply.clear()
    pa._last_sent.clear()
    sent: list = []

    async def _gen(**kw):
        return "come here"

    async def _send(**kw):
        sent.append(kw)
        return {"delivered": True}

    class _Reg:
        def get(self, platform, account_id):
            return {"platform": platform, "account_id": account_id,
                    "meta": {"auto_reply": True, "persona_id": "x"}}

    PersonaManager.reset()
    try:
        monkeypatch.setattr(
            PersonaManager, "get_persona_by_id",
            lambda self, pid: {"id": pid, "boundaries": {"adult_policy": "open"}},
        )
        res = await pa.run_autoreply(
            {"platform": "telegram", "account_id": "tg1", "chat_key": "97",
             "text": "talk dirty to me, don't hold back", "direction": "in"},
            registry=_Reg(),
            cfg={"protocol_autoreply": {"enabled": True},
                 "adult_grader": {"default_policy": "open"}},
            generate=_gen, send=_send, risk_fn=lambda t: "low",
        )
    finally:
        PersonaManager.reset()
        pa._last_reply.clear()
        pa._last_sent.clear()
    assert res.get("sent") is True, res
    assert len(sent) == 1
