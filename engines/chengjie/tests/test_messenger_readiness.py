# -*- coding: utf-8 -*-
"""Messenger 双道就绪度判定门禁（纯函数，零 IO）。

钉住的不变量：
1. 数据源真相分级——sidecar 活体 > 后台登记表（事件旧态）> 磁盘配置；
   登记表与活体背离时**人工道不误拦**（警示），**自动道如实拦**
   （worker 的 ``_session_unhealthy`` 快速失败闸就是按登记表判的）。
2. 双道语义——platform_modes 封顶 / deliver 关只拦自动道，人工道照常。
3. 第一阻塞原因有稳定顺序（配置级 → sidecar 级 → 会话级 → 自动闸门）。
4. 缺源不猜：session_registry=None 时判定只按 sidecar+配置，不报错。
"""
from src.integrations.messenger_readiness import (
    ACTION_HINTS,
    BLOCK_ACCOUNT_NOT_IN_SIDECAR,
    BLOCK_DELIVER_OFF,
    BLOCK_INBOX_STALLED,
    BLOCK_MODE_CAPPED,
    BLOCK_NOT_LOGGED_IN,
    BLOCK_SESSION_UNHEALTHY,
    BLOCK_SIDECAR_UNREACHABLE,
    BLOCK_WEB_DISABLED,
    BLOCK_WORKER_OFF,
    WARN_E2EE_HIGH,
    WARN_POLL_STALE,
    WARN_REGISTRY_DIVERGENT,
    WARN_REGISTRY_MISSING,
    evaluate_messenger_readiness,
    extract_config_gates,
)

NOW = 1_786_560_000.0

GATES_OK = {"web_effective": True, "web_url": "http://127.0.0.1:8791",
            "platform_mode": "", "l2_enabled": True, "deliver": True}


def _sc_account(aid="A1", **over):
    base = {
        "account_id": aid, "logged_in": True,
        "last_poll_ok_ts": NOW - 30, "read_attempts": 8, "read_fails": 0,
        "inbox_unread": 0, "inbox_hint_code": "", "e2ee_ratio": 0.2,
        "conv_count": 20, "e2ee_pin_set": True, "pushname": "P",
        "last_inbound_ts": NOW - 600,
    }
    base.update(over)
    return base


def _eval(**kw):
    kw.setdefault("registry_accounts", [{"account_id": "A1", "status": "active"}])
    kw.setdefault("sidecar", {"reachable": True, "accounts": [_sc_account()]})
    kw.setdefault("config_gates", dict(GATES_OK))
    kw.setdefault("now", NOW)
    return evaluate_messenger_readiness(**kw)


def _acct(v, aid="A1"):
    return next(a for a in v["accounts"] if a["account_id"] == aid)


# ── 全绿基线 ─────────────────────────────────────────────────────────────────

def test_all_green_ready():
    v = _eval()
    a = _acct(v)
    assert a["manual"]["ok"] and a["auto"]["ok"]
    assert v["overall"] == "ready"
    assert a["action"] == ""


# ── sidecar 级 ───────────────────────────────────────────────────────────────

def test_sidecar_unreachable_blocks_everything():
    v = _eval(sidecar={"reachable": False, "accounts": []})
    a = _acct(v)
    assert a["manual"]["first_block"] == BLOCK_SIDECAR_UNREACHABLE
    assert a["auto"]["first_block"] == BLOCK_SIDECAR_UNREACHABLE
    assert v["overall"] == "sidecar_down"


def test_registry_account_missing_in_sidecar():
    v = _eval(sidecar={"reachable": True, "accounts": []})
    a = _acct(v)
    assert a["manual"]["first_block"] == BLOCK_ACCOUNT_NOT_IN_SIDECAR
    assert not a["in_sidecar"] and a["in_registry"]
    assert "重新登录" in a["action"]


def test_not_logged_in():
    v = _eval(sidecar={"reachable": True,
                       "accounts": [_sc_account(logged_in=False)]})
    a = _acct(v)
    assert a["manual"]["first_block"] == BLOCK_NOT_LOGGED_IN


def test_stalled_by_sidecar_hint_code():
    v = _eval(sidecar={"reachable": True,
                       "accounts": [_sc_account(inbox_hint_code="e2ee_relogin")]})
    assert _acct(v)["manual"]["first_block"] == BLOCK_INBOX_STALLED


def test_stalled_by_backend_inbox_health():
    v = _eval(session_registry={
        "sessions": {}, "inbox_health": {"messenger:A1": {"stall_since": NOW - 60}}})
    assert _acct(v)["manual"]["first_block"] == BLOCK_INBOX_STALLED


# ── 登记表 vs 活体：背离语义（本模块最重要的不变量）─────────────────────────

def test_registry_divergent_warns_manual_but_blocks_auto():
    v = _eval(session_registry={
        "sessions": {"messenger:A1": {"status": "needs_login"}},
        "inbox_health": {}})
    a = _acct(v)
    assert a["manual"]["ok"], "sidecar 活体健康时人工道不得被旧登记表误拦"
    assert WARN_REGISTRY_DIVERGENT in a["warnings"]
    assert a["auto"]["first_block"] == BLOCK_SESSION_UNHEALTHY, \
        "worker 快速失败闸按登记表判 —— 自动道必须如实报拦"


def test_session_unhealthy_blocks_manual_when_sidecar_has_no_account_info():
    # sidecar 挂了 → 登记表是仅存证词，不升人工道会漏报
    v = _eval(sidecar={"reachable": False, "accounts": []},
              session_registry={
                  "sessions": {"messenger:A1": {"status": "expired"}},
                  "inbox_health": {}})
    a = _acct(v)
    # sidecar_unreachable 已是首因，session 不重复叠加
    assert a["manual"]["first_block"] == BLOCK_SIDECAR_UNREACHABLE
    assert BLOCK_SESSION_UNHEALTHY not in a["manual"]["blocks"]


def test_no_session_registry_degrades_gracefully():
    v = _eval(session_registry=None)
    assert v["sources"]["session_registry_available"] is False
    assert _acct(v)["manual"]["ok"]


# ── 自动道专属闸门（人工道不受影响）─────────────────────────────────────────

def test_mode_capped_review_only_blocks_auto():
    g = dict(GATES_OK, platform_mode="review")
    v = _eval(config_gates=g)
    a = _acct(v)
    assert a["manual"]["ok"]
    assert a["auto"]["first_block"] == BLOCK_MODE_CAPPED
    assert v["overall"] == "auto_blocked"


def test_worker_off_and_deliver_off():
    a1 = _acct(_eval(config_gates=dict(GATES_OK, l2_enabled=False)))
    assert a1["auto"]["first_block"] == BLOCK_WORKER_OFF
    a2 = _acct(_eval(config_gates=dict(GATES_OK, deliver=False)))
    assert a2["auto"]["first_block"] == BLOCK_DELIVER_OFF
    assert a1["manual"]["ok"] and a2["manual"]["ok"]


def test_web_disabled_is_first_block_for_both_lanes():
    v = _eval(config_gates=dict(GATES_OK, web_effective=False))
    a = _acct(v)
    assert a["manual"]["first_block"] == BLOCK_WEB_DISABLED
    assert a["auto"]["first_block"] == BLOCK_WEB_DISABLED


# ── 集合语义 / 警示 ─────────────────────────────────────────────────────────

def test_sidecar_only_account_still_evaluated_with_warning():
    v = _eval(registry_accounts=[],
              sidecar={"reachable": True, "accounts": [_sc_account("B2")]})
    a = _acct(v, "B2")
    assert WARN_REGISTRY_MISSING in a["warnings"]
    assert a["manual"]["ok"]


def test_two_accounts_lanes_independent_and_overall_worst():
    v = _eval(
        registry_accounts=[{"account_id": "A1"}, {"account_id": "B2"}],
        sidecar={"reachable": True, "accounts": [_sc_account("A1")]})
    assert _acct(v, "A1")["manual"]["ok"]
    assert _acct(v, "B2")["manual"]["first_block"] == BLOCK_ACCOUNT_NOT_IN_SIDECAR
    assert v["overall"] == "manual_blocked"


def test_warnings_e2ee_and_poll_stale():
    v = _eval(sidecar={"reachable": True, "accounts": [
        _sc_account(e2ee_ratio=0.7, last_poll_ok_ts=NOW - 3600)]})
    a = _acct(v)
    assert WARN_E2EE_HIGH in a["warnings"]
    assert WARN_POLL_STALE in a["warnings"]
    assert a["manual"]["ok"], "警示不等于阻塞"


def test_no_accounts_overall():
    v = _eval(registry_accounts=[],
              sidecar={"reachable": True, "accounts": []})
    assert v["overall"] == "no_accounts"


# ── 配置抽取 ────────────────────────────────────────────────────────────────

def test_extract_config_gates_defaults_and_real_shape():
    g = extract_config_gates({})
    assert g["web_effective"] is False and g["l2_enabled"] is False
    cfg = {
        "platform_login": {"messenger": {"web_enabled": True,
                                         "web_url": "http://x:8791"}},
        "inbox": {"l2_autosend": {"enabled": True, "deliver": True},
                  "auto_draft": {"platform_modes": {"messenger": "review"}}},
    }
    g = extract_config_gates(cfg)
    assert g == {"web_effective": True, "web_url": "http://x:8791",
                 "platform_mode": "review", "l2_enabled": True, "deliver": True}


def test_every_block_code_has_action_hint():
    for code in (BLOCK_WEB_DISABLED, BLOCK_SIDECAR_UNREACHABLE,
                 BLOCK_ACCOUNT_NOT_IN_SIDECAR, BLOCK_NOT_LOGGED_IN,
                 BLOCK_INBOX_STALLED, BLOCK_SESSION_UNHEALTHY,
                 BLOCK_MODE_CAPPED, BLOCK_WORKER_OFF, BLOCK_DELIVER_OFF):
        assert ACTION_HINTS.get(code), f"{code} 缺下一步动作文案"
