# -*- coding: utf-8 -*-
"""M-2 B（D-M1）登录默认半自动契约：重登 → 全会话 semi + 冷静期 L1；连续 3 失败 → 降级 + 红标；
账号级批量切换写入 N 个会话。

1.0.75 首日实录（UE7VM3 / K9CY6R）：Messenger 号 15:40 清数据重登，15:42–15:43 全自动
对 7 个会话批量起草并立即投递，首条 500 → 边车熔断 → 手动也 429。老板决策 D-M1：
任何登录 / 重登 → 半自动（只起草）+ 10 分钟冷静期；全自动按账号显式开启并确认；
连续 3 次失败自动降半自动 + 红标；**已在全自动的老会话不动**。
"""
from __future__ import annotations

import time
import uuid

import pytest

from src.inbox.autosend_worker import AutosendWorker
from src.inbox.store import InboxStore


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    import src.integrations.platform_session_health as psh
    from src.integrations.shared import event_bus as eb
    import src.integrations.account_registry as ar
    import src.inbox.account_channel_gate as gate
    import src.inbox.account_mode_onboarding as amo
    import src.integrations.account_orchestrator as ao
    monkeypatch.setattr(psh, "_SINGLETON", None, raising=False)
    monkeypatch.setattr(psh, "_SEEDED", False, raising=False)
    monkeypatch.setattr(eb, "_bus", None, raising=False)
    monkeypatch.setattr(ar, "_registry", None, raising=False)
    monkeypatch.setattr(ao, "get_orchestrator_if_running", lambda: None, raising=False)
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    gate._reset_for_tests()
    amo._reset_for_tests()
    yield
    gate._reset_for_tests()
    amo._reset_for_tests()


PLAT, ACCT = "messenger", "61584011289581"


def _cid(n: int) -> str:
    return f"{PLAT}:{ACCT}:c{n}"


# ── 配置默认（D-M1 ①）─────────────────────────────────────────────────────

def test_config_yaml_default_is_semi():
    import yaml
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    # config.yaml 被 gitignore；CI 只有出厂 config.example.yaml。
    names = ["config/config.yaml", "config/config.desktop.min.yaml",
             "config/config.desktop.internal.yaml"]
    if not (root / names[0]).is_file():
        names[0] = "config/config.example.yaml"
    for name in names:
        cfg = yaml.safe_load((root / name).read_text("utf-8")) or {}
        ad = ((cfg.get("inbox") or {}).get("auto_draft") or {})
        assert ad.get("automation_mode") == "review", name
        assert ad.get("bootstrap_automation_mode") is False, name


def test_login_default_semi_patch_three_state(tmp_path):
    from src.utils.config_manager import ConfigManager

    class _Stub:
        _LOGIN_SEMI_KEY = ConfigManager._LOGIN_SEMI_KEY

        def __init__(self, cfg, overlay_text=None):
            self.config = cfg
            self._p = tmp_path / f"ov_{uuid.uuid4().hex[:6]}.yaml"
            if overlay_text is not None:
                self._p.write_text(overlay_text, "utf-8")

        def _overlay_path(self):
            return self._p

    # ① 种子快照 auto_ai、overlay 没写过 → 迁 review + bootstrap 关 + 标记
    patch = ConfigManager.login_default_semi_patch(
        _Stub({"inbox": {"auto_draft": {"automation_mode": "auto_ai"}}}))
    ad = patch["inbox"]["auto_draft"]
    assert ad["automation_mode"] == "review" and ad["bootstrap_automation_mode"] is False
    assert ad["login_default_semi"] is True
    # ② overlay 里用户显式写过档位（含 auto_ai）→ 不动
    assert ConfigManager.login_default_semi_patch(_Stub(
        {"inbox": {"auto_draft": {"automation_mode": "auto_ai"}}},
        "inbox:\n  auto_draft:\n    automation_mode: auto_ai\n")) is None
    # ③ 已有标记 → 幂等不再迁
    assert ConfigManager.login_default_semi_patch(_Stub(
        {"inbox": {"auto_draft": {"automation_mode": "auto_ai",
                                  "login_default_semi": True}}})) is None


# ── 登录 → 冷静期 + 账号默认半自动（D-M1 ②③）─────────────────────────────

def test_relogin_starts_cooldown_and_semi_default_not_touching_explicit_rows(tmp_path):
    from src.inbox.account_channel_gate import (
        cooldown_remaining, gate_caps, login_default_mode, note_login,
    )
    from src.inbox.automation_mode import resolve_automation_mode
    from src.inbox.effective_automation import effective_automation
    store = InboxStore(tmp_path / "semi.db")
    store.set_automation_mode(_cid(1), "auto_ai", source="human")   # 老会话：显式全自动
    cfg = {"inbox": {"auto_draft": {"automation_mode": "review"}}}

    assert note_login(PLAT, ACCT, login_id="msg_new1", config=cfg) is True
    assert 590 < cooldown_remaining(PLAT, ACCT) <= 600
    assert login_default_mode(PLAT, ACCT) == "review"
    # 同 login_id 重推（边车/后端重启、WA 重连）不算登录
    assert note_login(PLAT, ACCT, login_id="msg_new1", config=cfg) is False

    # 未显式设置的会话 → 账号层 review（不落盘）
    assert resolve_automation_mode(store, _cid(2), cfg) == "review"
    assert store.get_automation_mode_if_set(_cid(2)) is None
    # 老会话显式 auto_ai 行**不动**，但冷静期内有效档位封 review（只起草）
    assert store.get_automation_mode_if_set(_cid(1)) == "auto_ai"
    assert resolve_automation_mode(store, _cid(1), cfg) == "auto_ai"
    eff = effective_automation(store, cfg, conversation_id=_cid(1),
                               platform=PLAT, account_id=ACCT)
    assert eff["mode"] == "auto_ai" and eff["effective_mode"] == "review"
    assert [c["layer"] for c in eff["caps"]] == ["login_cooldown"]
    assert eff["caps"][0]["until_ts"] > time.time()
    layers = [c.layer for c in gate_caps(PLAT, ACCT, config=cfg)]
    assert layers == ["login_cooldown"]

    # 冷静期过后：老会话全自动恢复，新会话仍是账号默认半自动（直到按账号确认）
    fut = time.time() + 700
    eff2 = effective_automation(store, cfg, conversation_id=_cid(1),
                                platform=PLAT, account_id=ACCT, now=fut)
    assert eff2["effective_mode"] == "auto_ai"
    assert login_default_mode(PLAT, ACCT) == "review"


def test_session_transition_hooks_login_gate(monkeypatch):
    from src.inbox.account_channel_gate import cooldown_remaining, reset_backoff, note_send_fail
    from src.integrations.platform_session_health import report_session_transition
    # 新 login_id 授权 → 冷静期
    report_session_transition(PLAT, ACCT, "authorized", login_id="msg_a")
    assert cooldown_remaining(PLAT, ACCT) > 0
    # 退避水位随登录成功重置（#233「登录成功即解锁」）
    note_send_fail(PLAT, ACCT, error_kind="send_backoff", retry_after_ms=20000)
    report_session_transition(PLAT, ACCT, "authorized", login_id="msg_a")
    from src.inbox.account_channel_gate import backoff_remaining
    assert backoff_remaining(PLAT, ACCT) == 0
    # 从 failed（瞬时故障）恢复不算登录；从 needs_login 恢复算
    import src.inbox.account_channel_gate as gate
    gate._reset_for_tests()
    report_session_transition("whatsapp", "wa1", "authorized")
    report_session_transition("whatsapp", "wa1", "failed", detail="[rc:net] boom")
    report_session_transition("whatsapp", "wa1", "authorized")
    assert cooldown_remaining("whatsapp", "wa1") == 0
    report_session_transition("whatsapp", "wa1", "needs_login")
    report_session_transition("whatsapp", "wa1", "authorized")
    assert cooldown_remaining("whatsapp", "wa1") > 0
    assert reset_backoff("whatsapp", "wa1") is False   # 无退避可清


# ── 连续 3 次失败 → 降级 + 红标（D-M1 ⑦）──────────────────────────────────

def test_three_real_failures_degrade_backoff_does_not_count():
    from src.inbox.account_channel_gate import (
        account_snapshot, clear_degraded, degraded_state, hold_reason, note_send_fail,
        note_send_ok,
    )
    # 退避类不计连续失败，只记水位
    r = note_send_fail(PLAT, ACCT, error_kind="send_backoff", retry_after_ms=13000)
    assert r["streak"] == 0 and r["backoff_until"] > time.time()
    assert hold_reason(PLAT, ACCT) == "backoff"
    note_send_ok(PLAT, ACCT)
    assert hold_reason(PLAT, ACCT) == ""
    # 三次真实失败 → 降级
    for i in range(2):
        r = note_send_fail(PLAT, ACCT, error_kind="composer_not_found")
        assert r["degraded"] is False
    r = note_send_fail(PLAT, ACCT, error_kind="exception")
    assert r["degraded_now"] is True and r["streak"] == 3
    assert degraded_state(PLAT, ACCT)["streak"] == 3
    assert hold_reason(PLAT, ACCT) == "degraded"
    snap = account_snapshot(PLAT, ACCT)
    assert snap["degraded"] is True and snap["hold_reason"] == "degraded"
    # 成功送达不自动解除红标（要人确认）；确认后归零
    note_send_ok(PLAT, ACCT)
    assert degraded_state(PLAT, ACCT) is not None
    assert clear_degraded(PLAT, ACCT, actor="tester") is True
    assert degraded_state(PLAT, ACCT) is None and hold_reason(PLAT, ACCT) == ""


# ── 账号级批量切换写入 N 个会话（D-M1 ②）────────────────────────────────────

def test_account_bulk_plan_and_apply(tmp_path):
    from src.inbox.account_bulk_mode import apply_account_bulk, plan_account_bulk
    from src.inbox.account_channel_gate import login_default_mode, note_login
    from src.inbox.account_mode_onboarding import decided_mode
    from src.inbox.models import InboxConversation
    store = InboxStore(tmp_path / "bulk.db")
    now = time.time()
    rows = []
    for i in range(1, 5):
        rows.append(InboxConversation(conversation_id=_cid(i), platform=PLAT,
                                      account_id=ACCT, chat_key=f"c{i}",
                                      display_name=f"客户{i}", last_ts=now - i))
    rows.append(InboxConversation(conversation_id=f"{PLAT}:{ACCT}:g1", platform=PLAT,
                                  account_id=ACCT, chat_key="g1", display_name="群",
                                  last_ts=now, chat_type="group"))
    rows.append(InboxConversation(conversation_id=f"{PLAT}:other:x", platform=PLAT,
                                  account_id="other", chat_key="x", display_name="别号",
                                  last_ts=now))
    for r in rows:
        store.upsert_conversation(r)
    store.set_automation_mode(_cid(1), "manual", source="human")     # 个别设置
    store.set_automation_mode(_cid(2), "auto_ai", source="bootstrap")
    note_login(PLAT, ACCT, login_id="msg_x")
    assert login_default_mode(PLAT, ACCT) == "review"

    plan = plan_account_bulk(store, PLAT, ACCT, "auto_ai")
    assert plan["total"] == 5                 # 别号不算
    assert plan["skipped_groups"] == 1
    assert plan["already"] == 1               # c2 已是 auto_ai
    assert plan["override_individual"] == 1   # c1 human
    assert plan["will_change"] == 3 and set(plan["targets"]) == {_cid(1), _cid(3), _cid(4)}

    res = apply_account_bulk(store, PLAT, ACCT, "auto_ai", actor="tester")
    assert res["changed"] == 3
    for i in (1, 2, 3, 4):
        assert store.get_automation_mode_if_set(_cid(i)) == "auto_ai"
    assert store.get_automation_mode_if_set(f"{PLAT}:{ACCT}:g1") is None   # 群跳过
    assert store.get_automation_mode_if_set(f"{PLAT}:other:x") is None     # 别号不动
    assert decided_mode(PLAT, ACCT) == "auto_ai"
    assert login_default_mode(PLAT, ACCT) is None    # 按账号确认过 → 登录后默认到此为止

    # 降档：取消待投递 L2
    store.upsert_draft({"source_kind": "inbox", "source_id": _cid(3),
                        "conversation_id": _cid(3), "platform": PLAT,
                        "account_id": ACCT, "chat_key": "c3", "draft_text": "x",
                        "autopilot_level": "L2", "risk_level": "low", "status": "pending"})
    res2 = apply_account_bulk(store, PLAT, ACCT, "review", actor="tester")
    # 降档方向群不跳过（4 私聊 + 1 群），auto_ai 会话的待投递 L2 被取消
    assert res2["changed"] == 5 and res2["cancelled_l2"] == 1


# ── autosend：冷静期 / 降级 → L2 留 pending 不投递 ─────────────────────────

class _Svc:
    def __init__(self, store):
        self.queue = []
        self._store = store
        self.resolved = []

    def list_drafts(self, status="pending", limit=200):
        batch, self.queue = self.queue, []
        return batch

    def resolve_with_audit(self, draft_id, action, by=""):
        self.resolved.append(draft_id)
        return {"ok": True}


def _item(conv, draft_id):
    return {"draft_id": draft_id, "autopilot_level": "L2", "final_text": "hi",
            "platform": PLAT, "account_id": ACCT,
            "chat_key": conv.rsplit(":", 1)[-1], "conversation_id": conv,
            "source_id": conv}


@pytest.mark.asyncio
async def test_worker_holds_l2_during_cooldown_then_releases(tmp_path):
    from src.inbox.account_channel_gate import end_cooldown, note_login
    store = InboxStore(tmp_path / "hold.db")
    svc = _Svc(store)
    sent = []

    async def _cb(platform, account_id, chat_key, text):
        sent.append(text)
        return {"ok": True, "delivered": True}

    async def _sleep(d):
        return None

    did = store.upsert_draft({"source_kind": "inbox", "source_id": _cid(9),
                              "conversation_id": _cid(9), "platform": PLAT,
                              "account_id": ACCT, "chat_key": "c9", "draft_text": "hi",
                              "autopilot_level": "L2", "risk_level": "low",
                              "status": "pending"})
    note_login(PLAT, ACCT, login_id="msg_hold")
    w = AutosendWorker(draft_service=svc, send_callback=_cb, sleep=_sleep)
    svc.queue = [_item(_cid(9), did)]
    await w._tick()
    assert sent == [] and svc.resolved == []
    assert w.total_skipped_channel_gate == 1
    assert store.get_draft(did)["status"] == "pending"     # 积压待过目，不取消
    assert w.status_snapshot()["total_skipped_channel_gate"] == 1
    # 冷静期结束 → 下一 tick 接续投递
    assert end_cooldown(PLAT, ACCT) is True
    svc.queue = [_item(_cid(9), did)]
    await w._tick()
    assert sent == ["hi"]


@pytest.mark.asyncio
async def test_worker_three_failures_degrade_then_hold(tmp_path):
    from src.inbox.account_channel_gate import degraded_state
    store = InboxStore(tmp_path / "deg.db")
    svc = _Svc(store)
    calls = []

    async def _cb(platform, account_id, chat_key, text):
        calls.append(text)
        return {"ok": False, "delivered": False, "error": "messenger send failed: 500",
                "error_kind": "composer_not_found"}

    async def _sleep(d):
        return None

    w = AutosendWorker(draft_service=svc, send_callback=_cb, sleep=_sleep)
    for i in range(3):
        svc.queue = [_item(_cid(20 + i), f"d{i}")]
        await w._tick()
    assert len(calls) == 3
    assert degraded_state(PLAT, ACCT) is not None
    assert w.total_account_degraded == 1
    # 降级后：后续 L2 扣住不再打通道
    svc.queue = [_item(_cid(30), "d30")]
    await w._tick()
    assert len(calls) == 3 and w.total_skipped_channel_gate >= 1


@pytest.mark.asyncio
async def test_worker_backoff_kind_does_not_degrade(tmp_path):
    from src.inbox.account_channel_gate import backoff_remaining, degraded_state
    store = InboxStore(tmp_path / "bo.db")
    svc = _Svc(store)

    async def _cb(platform, account_id, chat_key, text):
        return {"ok": False, "delivered": False, "error": "429 send backoff",
                "error_kind": "send_backoff", "retry_after_ms": 13000}

    async def _sleep(d):
        return None

    w = AutosendWorker(draft_service=svc, send_callback=_cb, sleep=_sleep)
    for i in range(4):
        svc.queue = [_item(_cid(40 + i), f"b{i}")]
        await w._tick()
    assert degraded_state(PLAT, ACCT) is None
    assert backoff_remaining(PLAT, ACCT) > 0
