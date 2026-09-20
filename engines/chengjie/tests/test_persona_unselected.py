# -*- coding: utf-8 -*-
"""#156 新账号不自动绑定人设（2026-09-03）。

「上线补默认人设」是便利功能，代价是**用户从没选过，AI 就已经以某个身份在说
话了**：账号栏显示的是自动补的那个人设，看不出「其实没人挑过」；要换还得先
意识到已经绑了。与 #63「按账号确认接管」同一哲学——身份和接管方式都该是人的
显式决定。

三段行为：
  · 上线不写库、返回空串（``ensure_account_default_persona``）；
  · 账号栏显示「未选人设」引导选择（``account_persona_unselected`` → chats
    响应的 ``persona_unselected``）；
  · 未选期间 AI 不代答（``compute_mode_caps`` 封顶 review）。
批量铺号的部署可显式 opt-in 回旧行为（``auto_attach_default_persona: true``）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from src.ai.persona_voice import (
    account_persona_unselected,
    ensure_account_default_persona,
)


class _Reg:
    """duck-typed 假注册表。"""

    def __init__(self, rows: Optional[Dict[str, Dict[str, Any]]] = None):
        self._rows = dict(rows or {})
        self.upserts = []

    def get(self, platform, account_id):
        return self._rows.get(f"{platform}:{account_id}")

    def upsert(self, platform, account_id, *, meta=None, merge_meta=False, **k):
        self.upserts.append((platform, account_id, dict(meta or {})))
        row = self._rows.setdefault(f"{platform}:{account_id}", {"meta": {}})
        if merge_meta:
            row["meta"].update(meta or {})
        else:
            row["meta"] = dict(meta or {})


_CFG_DEFAULT = {"platform_login": {"default_persona_id": "lin_xiaoyu"}}
_CFG_OPT_IN = {"platform_login": {"default_persona_id": "lin_xiaoyu",
                                  "auto_attach_default_persona": True}}


# ══ 1. 上线不自动绑 ═════════════════════════════════════════════════════════

def test_new_account_not_bound_and_logs_waiting(caplog):
    import logging

    reg = _Reg({"telegram:A": {"meta": {}}})
    with caplog.at_level(logging.INFO, logger="src.ai.persona_voice"):
        assert ensure_account_default_persona(reg, "telegram", "A",
                                              _CFG_DEFAULT) == ""
    assert not reg.upserts
    assert any("等待用户选择人设" in r.getMessage() for r in caplog.records)


def test_explicit_binding_always_respected():
    """人已经选过 → 原样返回（本改动只管「没人选过」的情形）。"""
    reg = _Reg({"telegram:A": {"meta": {"persona_id": "chen_mo"}}})
    assert ensure_account_default_persona(
        reg, "telegram", "A", _CFG_DEFAULT) == "chen_mo"
    reg2 = _Reg({"telegram:B": {"meta": {"persona_ids": ["su_wan"]}}})
    assert ensure_account_default_persona(
        reg2, "telegram", "B", _CFG_DEFAULT) == "su_wan"


def test_opt_in_restores_legacy_auto_attach():
    reg = _Reg({"telegram:A": {"meta": {}}})
    assert ensure_account_default_persona(
        reg, "telegram", "A", _CFG_OPT_IN) == "lin_xiaoyu"
    assert reg.get("telegram", "A")["meta"]["persona_id"] == "lin_xiaoyu"


# ══ 2. 「未选人设」判定 ══════════════════════════════════════════════════════

def test_unselected_judgement():
    reg = _Reg({
        "telegram:NONE": {"meta": {}},
        "telegram:PICKED": {"meta": {"persona_id": "chen_mo"}},
        "telegram:PLURAL": {"meta": {"persona_ids": ["su_wan"]}},
    })
    assert account_persona_unselected(
        _CFG_DEFAULT, "telegram", "NONE", registry=reg) is True
    # 关键：配置里有全局默认也**不算选过**——那正是让「没人选过」看起来像
    # 「已选好」的东西（#156 根因）
    assert account_persona_unselected(
        _CFG_DEFAULT, "telegram", "PICKED", registry=reg) is False
    assert account_persona_unselected(
        _CFG_DEFAULT, "telegram", "PLURAL", registry=reg) is False


def test_unselected_off_when_opt_in_or_unknown():
    reg = _Reg({"telegram:NONE": {"meta": {}}})
    # opt-in 部署要旧的自动绑定便利 → 不拦
    assert account_persona_unselected(
        _CFG_OPT_IN, "telegram", "NONE", registry=reg) is False
    # 缺平台/账号 → False（判不出不拦）
    assert account_persona_unselected(_CFG_DEFAULT, "", "A", registry=reg) is False
    assert account_persona_unselected(_CFG_DEFAULT, "telegram", "",
                                      registry=reg) is False


def test_no_registry_row_is_unknown_not_unselected():
    """注册表里没有这一行＝**判不出**，不是「没选」。

    A 线 telegram default 号、测试/CLI 装配、注册表暂不可用都属这类；把它们
    一律封成 review 就是拿判不出当判有罪（本条正是实现时踩到的回归：
    effective_automation 的既有用例被整片封顶）。"""
    reg = _Reg({})   # 空注册表
    assert account_persona_unselected(
        _CFG_DEFAULT, "telegram", "default", registry=reg) is False


def test_unselected_fail_open_on_registry_error():
    class _Boom:
        def get(self, *a, **k):
            raise RuntimeError("registry down")

    # fail-open：判定失败绝不把在跑的账号静默降级
    assert account_persona_unselected(
        _CFG_DEFAULT, "telegram", "A", registry=_Boom()) is False


# ══ 3. 未选期间 AI 不代答（与 #63 按账号确认接管联动）══════════════════════

def test_mode_cap_review_until_persona_picked(monkeypatch):
    from src.inbox.effective_automation import apply_mode_caps, compute_mode_caps

    monkeypatch.setattr(
        "src.ai.persona_voice.account_persona_unselected",
        lambda cfg, plat, acct, **k: acct == "NOPICK")
    caps = compute_mode_caps(
        platform="telegram", account_id="NOPICK", config={},
        business_line="", connected_at=1.0)   # 远古接入 → 预热层不叠加
    assert "persona_unselected" in [c.layer for c in caps]
    eff, applied = apply_mode_caps("auto_ai", caps)
    assert eff == "review"
    assert any(c.layer == "persona_unselected" for c in applied)
    # 选过人设的号零影响
    caps2 = compute_mode_caps(
        platform="telegram", account_id="PICKED", config={},
        business_line="", connected_at=1.0)
    assert "persona_unselected" not in [c.layer for c in caps2]


def test_mode_cap_fail_open(monkeypatch):
    from src.inbox.effective_automation import compute_mode_caps

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(
        "src.ai.persona_voice.account_persona_unselected", boom)
    caps = compute_mode_caps(
        platform="telegram", account_id="X", config={},
        business_line="", connected_at=1.0)
    assert "persona_unselected" not in [c.layer for c in caps]


# ══ 4. UI 入口在场（账号栏要能看见并去选） ══════════════════════════════════

def test_chats_response_carries_unselected_flag():
    """写了没挂线：chats 的 platform_status 必须带 persona_unselected，
    否则账号栏无从显示「未选人设」，用户永远不知道该去选。"""
    import pathlib

    repo = pathlib.Path(__file__).resolve().parents[1]
    src = (repo / "src" / "web" / "routes" / "unified_inbox_read_routes.py"
           ).read_text(encoding="utf-8")
    assert '"persona_unselected"' in src
    assert "account_persona_unselected" in src
