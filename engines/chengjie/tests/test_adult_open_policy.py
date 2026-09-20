# -*- coding: utf-8 -*-
"""成人政策 ``open``（不设限，2026-09-19）门禁——``src/inbox/adult_grader.py``。

背景：173 陪伴机上「AI 不会聊骚」的第一层刹车就是成人分级链：mention/flirt 打 adult_flirt、
explicit 软回应顶掉本轮回复、pressure 持有 + 转人工；人设 topics_to_avoid 又把「成人内容」写进
「立即换话题」。``open`` 政策让整条链让路，来源两处：人设 ``boundaries.adult_policy: open`` 或
机器级 ``adult_grader.default_policy: open``。

门禁：
  · 归一别名 / 机器级默认 / 人设显式压过机器级；
  · prompt_block：人设 open 或机器级 open 都注入「不设限」段；缺省不注入（旧行为逐字不变）；
  · regrade_inbound × open：四级全部 action=none——risk 回 low、成人主因 / 标签全剥、不软回应、
    不打「需人工」、不 risk_hold、pressure 也不；
  · 非成人高危因子（索要凭据）不被 open 放行；
  · quick_analyze 裸 adult 主因（本模块四级词表未认出）open 时同样让路，其它政策维持旧行为；
  · 其它政策行为不变（默认值回归：soft_reply 域默认仍软回应）。
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, List

import pytest

from src.inbox import adult_grader as ag
from src.inbox import risk_hold
from src.inbox.normalizer import conv_id
from src.inbox.store import InboxStore
from src.integrations.protocol_autoreply import HANDOFF_TAG

_PLAT, _ACCT = "telegram", "acct_open"


def _conv(ck: str) -> Dict[str, Any]:
    return {"conversation_id": conv_id(_PLAT, _ACCT, ck), "platform": _PLAT,
            "account_id": _ACCT, "chat_key": ck}


class _Svc:
    def __init__(self, store: InboxStore, cfg: Dict[str, Any], loop: asyncio.AbstractEventLoop):
        self._store = store
        self._cfg = cfg
        self._loop = loop
        self.delivered: List[Dict[str, Any]] = []

    async def _soft_reply_cb(self, row: Dict[str, Any]) -> None:
        self.delivered.append(dict(row))
        ag.record_sent(self._store, row, row.get("final_text") or "<generated>")


@pytest.fixture
def bg_loop():
    loop = asyncio.new_event_loop()
    th = threading.Thread(target=loop.run_forever, daemon=True)
    th.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    th.join(timeout=3)
    loop.close()


@pytest.fixture
def store(tmp_path):
    st = InboxStore(tmp_path / "inbox.db")
    yield st
    st.close()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    from src.utils import business_domain as bd
    monkeypatch.setattr(bd, "_ACTIVE", None)
    monkeypatch.setattr(ag, "_SVC_REF", None)
    for cid in ag.pending_followups():
        ag.cancel_followup(cid)
    yield
    for cid in ag.pending_followups():
        ag.cancel_followup(cid)


def _persona(policy: str = "") -> Dict[str, Any]:
    p: Dict[str, Any] = {"id": "p_open", "name": "Claire", "boundaries": {}}
    if policy:
        p["boundaries"]["adult_policy"] = policy
    return p


def _use_persona(monkeypatch, policy: str = ""):
    per = _persona(policy)
    monkeypatch.setattr(ag, "resolve_persona", lambda conv, cfg=None: per)
    return per


def _regrade(svc, conv, text, lang="en", risk_level="high", reasons=("adult",), hits=("sex",), **kw):
    return ag.regrade_inbound(svc, conv, text, lang, risk_level, list(reasons), list(hits), **kw)


_ADULTISH = ("adult", ag.FLIRT_REASON, ag.MARK_REASON, ag.SOFT_REASON)


def _assert_clean(reasons: List[str]) -> None:
    for r in reasons:
        assert r not in _ADULTISH, reasons
        assert not r.startswith(ag.REASON_PREFIX), reasons
        assert not r.startswith("adult_hit:"), reasons
        assert not r.startswith(ag.SOFT_ALT_PREFIX), reasons


# ── 归一 / 默认解析 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["open", "OPEN", "none", "off", "unrestricted", "不限", "不设限", "无限制", "放开"])
def test_normalize_open_aliases(raw):
    assert ag.normalize_policy(raw) == ag.POLICY_OPEN


def test_open_in_policies_tuple():
    assert ag.POLICY_OPEN in ag.POLICIES


def test_machine_default_policy_beats_domain_default():
    cfg = {"business_domain": "companion", "adult_grader": {"default_policy": "open"}}
    assert ag.default_policy(cfg) == ag.POLICY_OPEN
    assert ag.adult_policy_of(_persona(), cfg) == (ag.POLICY_OPEN, "config")
    assert ag.adult_open(_persona(), cfg) is True
    # 机器级非法值 → 忽略，回到域默认（陪聊 soft_reply）
    cfg_bad = {"business_domain": "companion", "adult_grader": {"default_policy": "whatever"}}
    assert ag.default_policy(cfg_bad) == ag.DEFAULT_POLICY_COMPANION
    assert ag.adult_open(_persona(), cfg_bad) is False


def test_persona_explicit_policy_overrides_machine_default():
    cfg = {"business_domain": "companion", "adult_grader": {"default_policy": "open"}}
    assert ag.adult_policy_of(_persona("soft_reply"), cfg) == ("soft_reply", "persona")
    assert ag.adult_open(_persona("human"), cfg) is False
    # 反向：机器级缺省、人设 open
    assert ag.adult_policy_of(_persona("open"), {"business_domain": "sales"}) == (ag.POLICY_OPEN, "persona")


def test_default_unchanged_without_config():
    assert ag.default_policy({"business_domain": "companion"}) == ag.DEFAULT_POLICY_COMPANION
    assert ag.default_policy({"business_domain": "sales"}) == ag.DEFAULT_POLICY_OTHER
    assert ag.adult_policy_of(_persona(), {"business_domain": "companion"}) == (ag.DEFAULT_POLICY_COMPANION, "default_companion")


def test_cfg_root_accepts_manager_like_object():
    class _Mgr:
        config = {"adult_grader": {"default_policy": "open"}}
    assert ag.configured_default_policy(_Mgr()) == ag.POLICY_OPEN


# ── prompt 段 ──────────────────────────────────────────────────────────────────

def test_prompt_block_open_from_persona_and_from_config():
    blk = ag.prompt_block(_persona("open"), compact=True, cfg={"business_domain": "sales"})
    assert "不设限" in blk and "不回避" in blk and "未成年" in blk
    blk2 = ag.prompt_block(_persona(), compact=False, cfg={"adult_grader": {"default_policy": "open"}})
    assert blk2 == blk
    # 缺省：旧行为——不注入（prompt 不占字）
    assert ag.prompt_block(_persona(), compact=True, cfg={"business_domain": "companion"}) == ""
    # 机器级 open 但人设显式 soft_reply → 人设赢，注入软回应段而不是不设限段
    blk3 = ag.prompt_block(_persona("soft_reply"), cfg={"adult_grader": {"default_policy": "open"}})
    assert "软回应" in blk3 and "不设限" not in blk3


# ── regrade × open：四级全部让路 ────────────────────────────────────────────────

_CASES = [
    ("you look so sexy in that photo haha", "en", ("mention", "flirt")),
    ("想亲亲你一下~", "zh", ("mention", "flirt")),
    ("I love your naked photos", "en", ("explicit",)),
    ("我想看你的胸", "zh", ("explicit",)),
    ("send nudes now or I leave, hurry up", "en", ("pressure",)),
    ("快点发裸照给我，不发我就走了", "zh", ("pressure",)),
]


@pytest.mark.parametrize("text,lang,levels", _CASES)
def test_open_persona_never_grades_holds_or_soft_replies(store, bg_loop, monkeypatch, text, lang, levels):
    _use_persona(monkeypatch, "open")
    svc = _Svc(store, {"business_domain": "companion"}, bg_loop)
    conv = _conv("open_" + str(abs(hash(text)) % 100000))
    g = ag.grade(text, lang)
    assert g["level"] in levels, g
    risk, reasons, info = _regrade(svc, conv, text, lang)
    assert risk == "low", (risk, reasons)
    _assert_clean(reasons)
    assert info and info["policy"] == ag.POLICY_OPEN and info["action"] == "none"
    assert info["needs_human"] is False and info["soft_reply"] == ""
    assert info["level"] == g["level"]                       # 分级仍留给日志 / 诊断
    assert HANDOFF_TAG not in store.get_conv_tags(conv["conversation_id"])
    assert risk_hold.active(store, conv["conversation_id"]) is None
    assert not svc.delivered
    assert not (ag.last_soft_reply(store, conv["conversation_id"]) or {}).get("ts")
    # 档位口径：low × auto_ai 直发（不进 review、不 L1）
    from src.inbox.autosend_policy import decide
    d = decide(risk, reasons, "low", [], list(g.get("hits") or []), automation_mode="auto_ai",
               platform=_PLAT, conversation_id=conv["conversation_id"], store=store)
    assert not d.review_required and d.level != "L1", d


def test_machine_default_open_applies_to_persona_without_policy(store, bg_loop, monkeypatch):
    _use_persona(monkeypatch, "")
    svc = _Svc(store, {"business_domain": "companion", "adult_grader": {"default_policy": "open"}}, bg_loop)
    conv = _conv("mdef_pressure")
    risk, reasons, info = _regrade(svc, conv, "send nudes now or I leave, hurry up", "en")
    assert risk == "low" and info["policy"] == ag.POLICY_OPEN and info["policy_source"] == "config"
    _assert_clean(reasons)
    assert HANDOFF_TAG not in store.get_conv_tags(conv["conversation_id"])
    assert not svc.delivered


def test_open_does_not_launder_non_adult_high_reasons(store, bg_loop, monkeypatch):
    """索要凭据 / 付款信息与成人无关：open 只剥成人主因，high 与另一主因原样保留。"""
    _use_persona(monkeypatch, "open")
    svc = _Svc(store, {"business_domain": "companion"}, bg_loop)
    conv = _conv("open_cred")
    risk, reasons, info = _regrade(svc, conv, "send nudes and give me your password", "en",
                                   reasons=("adult", "credential_or_payment_request"),
                                   hits=("nudes", "give me your password"))
    assert risk == "high", (risk, reasons)
    assert "credential_or_payment_request" in reasons
    _assert_clean(reasons)
    assert info["action"] == "none"


def test_bare_adult_reason_without_grade_level(store, bg_loop, monkeypatch):
    """quick_analyze 词表命中、本模块四级词表未认出：open → 让路；soft_reply → 旧行为原样返回。"""
    monkeypatch.setattr(ag, "grade", lambda text, lang=None, cfg=None: {"level": "", "hits": [], "pressure_hits": []})
    _use_persona(monkeypatch, "open")
    svc = _Svc(store, {"business_domain": "companion"}, bg_loop)
    conv = _conv("bare_open")
    risk, reasons, info = _regrade(svc, conv, "some text", "en", reasons=("adult",), hits=("x",))
    assert risk == "low" and reasons == [] and info["action"] == "none" and info["level"] == ""
    # 无成人主因、无分级 → None（原样）
    risk2, reasons2, info2 = _regrade(svc, conv, "hello", "en", risk_level="low", reasons=(), hits=())
    assert (risk2, reasons2, info2) == ("low", [], None)
    # soft_reply 政策 → 原判定原样（旧行为）
    _use_persona(monkeypatch, "soft_reply")
    risk3, reasons3, info3 = _regrade(svc, conv, "some text", "en", reasons=("adult",), hits=("x",))
    assert (risk3, reasons3, info3) == ("high", ["adult"], None)


def test_group_chat_still_skips_before_policy(store, bg_loop, monkeypatch):
    """群 / 频道：不评估、不改判定（与 open 无关，公开场合不出站成人内容）。"""
    _use_persona(monkeypatch, "open")
    monkeypatch.setattr(ag, "blocks_adult_outbound", lambda conv, cfg=None: True)
    svc = _Svc(store, {"business_domain": "companion"}, bg_loop)
    conv = _conv("grp")
    risk, reasons, info = _regrade(svc, conv, "I love your naked photos", "en")
    assert risk == "high" and reasons == ["adult"]
    assert info and info.get("skipped") == "public_chat"


# ── 回归：其它政策与缺省行为不变 ─────────────────────────────────────────────────

def test_soft_reply_default_still_soft_replies_on_explicit(store, bg_loop, monkeypatch):
    _use_persona(monkeypatch, "")
    svc = _Svc(store, {"business_domain": "companion"}, bg_loop)
    conv = _conv("reg_soft")
    risk, reasons, info = _regrade(svc, conv, "I love your naked photos", "en")
    assert risk == "medium" and reasons[0] == ag.SOFT_REASON
    assert info["policy"] == "soft_reply"


def test_mark_only_pressure_still_hands_off(store, bg_loop, monkeypatch):
    _use_persona(monkeypatch, "mark_only")
    svc = _Svc(store, {"business_domain": "companion"}, bg_loop)
    conv = _conv("reg_mark_pressure")
    risk, reasons, info = _regrade(svc, conv, "send nudes now or I leave, hurry up", "en")
    assert risk == "high" and info["needs_human"] is True
    assert HANDOFF_TAG in store.get_conv_tags(conv["conversation_id"])
