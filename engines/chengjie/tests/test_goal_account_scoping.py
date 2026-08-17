# -*- coding: utf-8 -*-
"""目标账号锁 + 自聊排除门禁（P4 2026-08-09，两个生产实锤 bug 的回归钉）。

实锤一（跨账号串号）：两个账号的收藏消息（chat_key 同为 'me'）经
``find_active_goal`` 的 (platform, chat_key) 通配回落解析到**同一个目标**；
订单回流 ``settle_order_ref`` 的兜底匹配走同一口 → 跨账号误结算面。
修法＝通配回落只服务「没给 account_id」的调用方（A 线拿不到账号的历史语义
保留）；给了账号而账号内无命中 → 如实 None。

实锤二（自聊进获客漏斗）：auto_create 把账号自己的收藏消息当新好友建了
「获客转化」目标——chat_key='me' 一律不建。
"""

from __future__ import annotations

import pytest

from src.companion.goals import service as goal_service
from src.companion.goals.store import GoalStore, reset_goal_store


@pytest.fixture(autouse=True)
def _reset():
    reset_goal_store()
    yield
    reset_goal_store()


def _mk_store() -> GoalStore:
    return GoalStore(":memory:")


def _mk_goal(store, *, account_id, chat_key="me"):
    g = store.create_goal(
        conversation_id=f"telegram:{account_id}:{chat_key}",
        platform="telegram", account_id=account_id, chat_key=chat_key,
        template="conversion_unlock")
    assert g is not None
    return g


# ── find_active_goal 账号锁 ─────────────────────────────────────────────────

def test_cross_account_me_collision_fixed():
    """生产实锤复刻：Katie(8244899900) 的 me 目标，不得被 Vivian(8041810715)
    的 me 会话解析到。"""
    store = _mk_store()
    g = _mk_goal(store, account_id="8244899900")
    # 串号路径（修前会命中 Katie 的目标）
    hit = store.find_active_goal(
        conversation_id="telegram:8041810715:me",
        platform="telegram", chat_key="me", account_id="8041810715")
    assert hit is None
    # 正主照常命中（conversation_id 精确 / 账号限定两条路都通）
    assert store.find_active_goal(
        conversation_id="telegram:8244899900:me")["goal_id"] == g["goal_id"]
    assert store.find_active_goal(
        platform="telegram", chat_key="me",
        account_id="8244899900")["goal_id"] == g["goal_id"]


def test_wildcard_fallback_preserved_for_account_less_callers():
    """A 线「拿不到 account_id」的历史语义保留：不传账号仍可按
    (platform, chat_key) 命中。"""
    store = _mk_store()
    g = _mk_goal(store, account_id="8244899900", chat_key="777")
    hit = store.find_active_goal(platform="telegram", chat_key="777")
    assert hit and hit["goal_id"] == g["goal_id"]


def test_order_settle_no_longer_crosses_accounts():
    """订单回流兜底匹配（settle_order_ref 的 (platform,account,chat_key) 口）
    不得把 A 账号的单结到 B 账号的目标上。"""
    store = _mk_store()
    _mk_goal(store, account_id="8244899900", chat_key="555")
    out = goal_service.settle_order_ref(
        store, ref="telegram:8041810715:555", order_id="O-X", plan="pro")
    assert out["matched"] is False          # 修前：matched=True 误结算
    out2 = goal_service.settle_order_ref(
        store, ref="telegram:8244899900:555", order_id="O-Y", plan="pro")
    assert out2["matched"] is True and out2["updated"] is True


# ── auto_create 自聊排除 ────────────────────────────────────────────────────

class _StubPM:
    def get_persona_with_tier(self, chat_id, account_pid):
        return {"id": "su_wan"}, "account"


def test_auto_create_skips_saved_messages(monkeypatch):
    import src.utils.persona_manager as pm
    monkeypatch.setattr(
        pm.PersonaManager, "get_instance", staticmethod(lambda: _StubPM()))
    store = _mk_store()
    cfg = {"companion": {"goals": {
        "enabled": True,
        "auto_create": {"enabled": True, "personas": ["su_wan"]},
    }}}
    # 收藏消息：一律不建
    g = goal_service.maybe_auto_create_goal(
        store, cfg, platform="telegram", chat_key="me",
        account_id="8244899900",
        conversation_id="telegram:8244899900:me")
    assert g is None
    # 对照组：真客户会话照常建（证明是 me 守卫在拦，不是别的闸）
    g2 = goal_service.maybe_auto_create_goal(
        store, cfg, platform="telegram", chat_key="12345",
        account_id="8244899900",
        conversation_id="telegram:8244899900:12345")
    assert g2 is not None and g2["created_by"] == "auto_create"
