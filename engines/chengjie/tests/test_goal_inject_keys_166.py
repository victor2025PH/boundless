# -*- coding: utf-8 -*-
"""#166 门禁：工作目标「自动推进」不推进——注入口键一致性 + 可见性。

证据（skuio 88MP86，1.0.73）：右栏卡显示 BABY BEAR 会话目标「进行中 · 自动推进 ·
第 1/1 天 · 89%」，同会话 9.5 小时 377 次 auto_generate_draft，backend.log 里
**零行** ``[goal-inject]``——no_goal / 读库异常 / 注入链异常三条路径全是 DEBUG，
诊断包无法区分「拟稿链到底用什么键查、查到没、还是炸了」。

钉住：
1. 键一致性：右栏卡（``goal_routes._split_conversation_id(conv)`` 拆三元组）与
   拟稿链（``generate_inbox_draft(conversation_id, platform, chat_key, account_id)``
   四散值，account 可能是 ''/'default'）喂同一个 ``find_active_goal`` 必须同结果；
   归一只补齐、**绝不放宽账号锁**（P4 2026-08-09 防串号）。
2. 可见性：有会话 id 的 no_goal 出 INFO 且带全部查找键，同会话 10 分钟节流；
   store 读库异常出 WARNING 带异常类名；注入链内部异常出 WARNING 带异常类名。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from src.companion.goals import service
from src.companion.goals.service import (
    build_block_for_chat,
    resolve_inject_lookup_keys,
)
from src.companion.goals.store import get_goal_store, reset_goal_store
from src.web.routes.goal_routes import _split_conversation_id

_CONV = "whatsapp:17345893506:13308422244"
_PLAT, _ACCT, _CK = "whatsapp", "17345893506", "13308422244"
_ON = {"enabled": True, "db_path": ":memory:"}


def _cfg(goals_cfg=None):
    return SimpleNamespace(
        config={"companion": {"goals": dict(goals_cfg or _ON)}}, config_path=None)


def _seed(conv=_CONV, plat=_PLAT, acct=_ACCT, ck=_CK, **kw):
    store = get_goal_store(":memory:")
    goal = store.create_goal(
        conversation_id=conv, platform=plat, account_id=acct, chat_key=ck,
        template=kw.pop("template", "custom"),
        params=kw.pop("params", {"note": "收口"}),
        autonomy=kw.pop("autonomy", "auto"),
        deadline_days=kw.pop("deadline_days", 3))
    assert goal is not None
    return store, goal


def setup_function(_fn):
    reset_goal_store()
    service._inject_log_seen.clear()


def teardown_function(_fn):
    reset_goal_store()
    service._inject_log_seen.clear()


# ── 1. 键归一纯函数 ───────────────────────────────────────────────────────────

def test_lookup_keys_fill_triple_from_conv():
    k = resolve_inject_lookup_keys(conversation_id=_CONV)
    assert k == {"platform": _PLAT, "chat_key": _CK, "account_id": _ACCT,
                 "conversation_id": _CONV}


@pytest.mark.parametrize("acct_in", ["", "default", "DEFAULT"])
def test_lookup_keys_placeholder_account_takes_conv_account(acct_in):
    """拟稿链 account 常是 ''/'default'（enrich_auto_draft: conv.account_id or
    'default'）→ 以 conv 内账号为准，与右栏卡 _split_conversation_id 同口径。"""
    k = resolve_inject_lookup_keys(
        platform=_PLAT, chat_key=_CK, account_id=acct_in, conversation_id=_CONV)
    assert k["account_id"] == _ACCT
    assert (k["platform"], k["chat_key"]) == (_PLAT, _CK)


def test_lookup_keys_explicit_real_account_wins_over_conv():
    """调用方显式给了真实账号（多账号同 peer）→ 尊重调用方，不被 conv 覆盖。"""
    k = resolve_inject_lookup_keys(
        platform=_PLAT, chat_key=_CK, account_id="99999",
        conversation_id=_CONV)
    assert k["account_id"] == "99999"


def test_lookup_keys_synthesize_conv_from_full_triple():
    k = resolve_inject_lookup_keys(platform=_PLAT, chat_key=_CK, account_id=_ACCT)
    assert k["conversation_id"] == _CONV


@pytest.mark.parametrize("acct_in", ["", "default"])
def test_lookup_keys_no_conv_without_real_account(acct_in):
    """A 线拿不到账号（''）或占位 default → 不合成 conv（宽匹配/锁匹配语义原样）。"""
    k = resolve_inject_lookup_keys(platform=_PLAT, chat_key=_CK, account_id=acct_in)
    assert k["conversation_id"] == ""
    assert k["account_id"] == acct_in


def test_lookup_keys_never_raise_on_garbage():
    k = resolve_inject_lookup_keys(
        platform=None, chat_key=None, account_id=None, conversation_id=":::")  # type: ignore[arg-type]
    assert isinstance(k, dict) and set(k) == {
        "platform", "chat_key", "account_id", "conversation_id"}


# ── 2. 键一致性：卡片口径 vs 拟稿链口径喂同一个 find_active_goal ─────────────────

def _card_lookup(store, conv):
    platform, account_id, chat_key = _split_conversation_id(conv)
    return store.find_active_goal(
        conversation_id=conv, platform=platform, chat_key=chat_key,
        account_id=account_id)


def _draft_lookup(store, *, conversation_id, platform, chat_key, account_id):
    k = resolve_inject_lookup_keys(
        platform=platform, chat_key=chat_key, account_id=account_id,
        conversation_id=conversation_id)
    return store.find_active_goal(
        conversation_id=k["conversation_id"], platform=k["platform"],
        chat_key=k["chat_key"], account_id=k["account_id"])


@pytest.mark.parametrize("acct_in", [_ACCT, "", "default"])
def test_card_and_draft_chain_resolve_same_goal(acct_in):
    store, goal = _seed()
    card = _card_lookup(store, _CONV)
    draft = _draft_lookup(store, conversation_id=_CONV, platform=_PLAT,
                          chat_key=_CK, account_id=acct_in)
    assert card is not None and draft is not None
    assert card["goal_id"] == draft["goal_id"] == goal["goal_id"]


def test_draft_chain_without_conv_but_full_triple_matches_card():
    store, goal = _seed()
    draft = _draft_lookup(store, conversation_id="", platform=_PLAT,
                          chat_key=_CK, account_id=_ACCT)
    assert draft is not None and draft["goal_id"] == goal["goal_id"]


def test_goal_created_without_conv_still_found_by_card_keys():
    """建目标只给三元组（goal_routes 会合成 conv，但老库/批量口可能 conv 为空）
    → 卡片按 conv 精确查落空后回落三元组，拟稿链归一后同样回落到三元组。"""
    store, goal = _seed(conv="")
    assert _card_lookup(store, _CONV)["goal_id"] == goal["goal_id"]
    assert _draft_lookup(store, conversation_id=_CONV, platform=_PLAT,
                         chat_key=_CK, account_id="default")["goal_id"] == goal["goal_id"]


def test_account_lock_not_relaxed_by_normalization():
    """P4 账号锁：a1 的目标，拟稿链带 conv=…:a2:… 且 account='' → 归一成 a2 →
    a2 账号内无命中 → None。归一不得借到 a1 的目标（串号）。"""
    store, _ = _seed(conv="telegram:a1:c9", plat="telegram", acct="a1", ck="c9")
    assert _draft_lookup(store, conversation_id="telegram:a2:c9",
                         platform="telegram", chat_key="c9", account_id="") is None
    assert _draft_lookup(store, conversation_id="telegram:a2:c9",
                         platform="telegram", chat_key="c9", account_id="a2") is None
    # 卡片口径同样落空（两边一致地「没有」）
    assert _card_lookup(store, "telegram:a2:c9") is None


def test_build_block_injects_with_placeholder_account_like_draft_chain(caplog):
    """端到端：拟稿链形态（conv + account='default'）→ build_block_for_chat 命中并注入。"""
    _seed()
    ctx = {}
    with caplog.at_level(logging.INFO, logger="GoalService"):
        blk = build_block_for_chat(
            _cfg(), platform=_PLAT, chat_key=_CK, account_id="default",
            conversation_id=_CONV, user_context=ctx, chain="draft",
            inbound_text="hello")
    assert blk and "【工作目标】" in blk
    meta = ctx.get("_goal_inject_meta") or {}
    assert meta.get("injected") is True
    assert any("[goal-inject] injected conv=" + _CONV in r.getMessage()
               for r in caplog.records)


# ── 3. 可见性：no_goal INFO（带键、节流）/ store 异常 WARNING / 注入链异常 WARNING ──

def test_no_goal_with_conv_logs_info_with_all_keys(caplog):
    get_goal_store(":memory:")
    ctx = {}
    with caplog.at_level(logging.INFO, logger="GoalService"):
        blk = build_block_for_chat(
            _cfg(), platform=_PLAT, chat_key=_CK, account_id=_ACCT,
            conversation_id=_CONV, user_context=ctx, chain="draft",
            inbound_text="hi")
    assert blk is None
    assert (ctx.get("_goal_inject_meta") or {}).get("reason") == "no_goal"
    infos = [r for r in caplog.records
             if r.levelno == logging.INFO and "reason=no_goal" in r.getMessage()]
    assert len(infos) == 1, "有会话 id 的 no_goal 必须落一行 INFO"
    msg = infos[0].getMessage()
    for piece in (f"conv={_CONV}", f"plat={_PLAT}", f"chat_key={_CK}",
                  f"acct={_ACCT}", "chain=draft"):
        assert piece in msg, f"no_goal 日志必须带全部查找键，缺 {piece}: {msg}"


def test_no_goal_info_throttled_per_conversation(caplog):
    get_goal_store(":memory:")
    with caplog.at_level(logging.INFO, logger="GoalService"):
        for _ in range(5):
            build_block_for_chat(
                _cfg(), platform=_PLAT, chat_key=_CK, account_id=_ACCT,
                conversation_id=_CONV, user_context={}, chain="draft",
                inbound_text="hi", now=1_700_000_000.0)
        # 另一个会话不受本会话节流影响
        build_block_for_chat(
            _cfg(), platform=_PLAT, chat_key="other", account_id=_ACCT,
            conversation_id=f"{_PLAT}:{_ACCT}:other", user_context={},
            chain="draft", inbound_text="hi", now=1_700_000_000.0)
        # 过了节流窗再放行
        build_block_for_chat(
            _cfg(), platform=_PLAT, chat_key=_CK, account_id=_ACCT,
            conversation_id=_CONV, user_context={}, chain="draft",
            inbound_text="hi", now=1_700_000_000.0 + 601.0)
    infos = [r.getMessage() for r in caplog.records
             if r.levelno == logging.INFO and "reason=no_goal" in r.getMessage()]
    assert sum(1 for m in infos if f"conv={_CONV}" in m) == 2
    assert sum(1 for m in infos if "conv=" + f"{_PLAT}:{_ACCT}:other" in m) == 1


def test_no_goal_without_conv_stays_debug(caplog):
    """主动链/系统调用没有会话 id → 仍 DEBUG（避免无键日志刷屏）。"""
    get_goal_store(":memory:")
    with caplog.at_level(logging.DEBUG, logger="GoalService"):
        build_block_for_chat(
            _cfg(), platform="telegram", chat_key="nobody", account_id="",
            conversation_id="", user_context={}, chain="reply")
    recs = [r for r in caplog.records if "no_goal" in r.getMessage()]
    assert recs and all(r.levelno == logging.DEBUG for r in recs)


def test_disabled_stays_debug(caplog):
    with caplog.at_level(logging.DEBUG, logger="GoalService"):
        build_block_for_chat(
            _cfg({"enabled": False}), platform=_PLAT, chat_key=_CK,
            account_id=_ACCT, conversation_id=_CONV, user_context={})
    recs = [r for r in caplog.records if "skip=disabled" in r.getMessage()]
    assert recs and all(r.levelno == logging.DEBUG for r in recs)


def test_store_find_active_goal_exception_logs_warning(caplog):
    store = get_goal_store(":memory:")

    class _Boom:
        def execute(self, *_a, **_k):
            raise RuntimeError("disk I/O error")

        def close(self):  # teardown 的 reset_goal_store 会关连接
            pass

    store._conn = _Boom()  # type: ignore[assignment]
    with caplog.at_level(logging.WARNING, logger="GoalStore"):
        assert store.find_active_goal(
            conversation_id=_CONV, platform=_PLAT, chat_key=_CK,
            account_id=_ACCT) is None
    warns = [r for r in caplog.records if r.levelno == logging.WARNING
             and "find_active_goal failed" in r.getMessage()]
    assert warns, "读库异常必须 WARNING（此前 DEBUG 与 no_goal 无法区分）"
    msg = warns[0].getMessage()
    assert "RuntimeError" in msg and f"conv={_CONV}" in msg and f"acct={_ACCT}" in msg


def test_build_block_internal_exception_logs_warning_with_class(caplog, monkeypatch):
    _seed()

    def _boom(*_a, **_k):
        raise KeyError("planner exploded")

    monkeypatch.setattr(service, "refresh_goal", _boom)
    ctx = {}
    with caplog.at_level(logging.WARNING, logger="GoalService"):
        blk = build_block_for_chat(
            _cfg(), platform=_PLAT, chat_key=_CK, account_id=_ACCT,
            conversation_id=_CONV, user_context=ctx, chain="draft",
            inbound_text="hi")
    assert blk is None, "契约不变：注入口绝不抛"
    meta = ctx.get("_goal_inject_meta") or {}
    assert meta.get("reason") == "error" and meta.get("error") == "KeyError"
    warns = [r for r in caplog.records if r.levelno == logging.WARNING
             and "[goal-inject] ERROR KeyError" in r.getMessage()]
    assert warns
    assert f"conv={_CONV}" in warns[0].getMessage()
    assert warns[0].exc_info is not None, "首次异常带堆栈"


def test_build_block_internal_exception_throttles_stack(caplog, monkeypatch):
    _seed()

    def _boom(*_a, **_k):
        raise KeyError("planner exploded")

    monkeypatch.setattr(service, "refresh_goal", _boom)
    with caplog.at_level(logging.WARNING, logger="GoalService"):
        for _ in range(3):
            build_block_for_chat(
                _cfg(), platform=_PLAT, chat_key=_CK, account_id=_ACCT,
                conversation_id=_CONV, user_context={}, chain="draft",
                inbound_text="hi", now=1_700_000_000.0)
    warns = [r for r in caplog.records if r.levelno == logging.WARNING
             and "[goal-inject] ERROR" in r.getMessage()]
    assert len(warns) == 3, "每轮都有一行 WARNING（不吞）"
    assert warns[0].exc_info
    # logging 把 exc_info=False 原样存进 record（不是 None）→ 用真值判
    assert all(not r.exc_info for r in warns[1:]), "同会话 10 分钟内只带一次堆栈"


def test_skill_manager_inject_path_uses_service_entry():
    """拟稿链的注入口仍是 skill_manager._inject_goal_context → build_block_for_chat
    （键归一在 service 入口做，调用方零改动即获益；J-1 归属的 skill_manager 不动）。"""
    import inspect

    _SMcls = __import__(
        "src.skills.skill_manager", fromlist=["SkillManager"]).SkillManager
    src = inspect.getsource(_SMcls._inject_goal_context)
    assert "build_block_for_chat(" in src
    assert "conversation_id=str(conversation_id or \"\")" in src
    assert "account_id=str(account_id or \"\")" in src
