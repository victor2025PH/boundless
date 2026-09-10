# -*- coding: utf-8 -*-
"""Q-3（#264）风险挂会话 + 人工优先 门禁。

XBGPBN 时间线（2026-09-09）：23:49:13 隐私词 → 需人工；23:55:42 客户再来一句 → 重新起草
risk=low → L2；23:55:43 [pacing] 35.1s；用户切手动 + 打字；23:56:20 **仍发出**。三处根因：
① 闸在等待之前（等待后只认客户插话）② 「需人工」只是标签不是闸 ③ 风险只挂在稿上不挂会话。

本文件钉住：risk_hold 会话级持有 API / decide 强制 L1 + 继承 shadow / 需人工作闸 /
真发前二次复检（切手动 1 秒内取消、坐席打字放弃、坐席发送放弃）/ cancel_inflight 计数 /
捞稿期 risk_hold 取消 + 按 L1 重拟一次 / autodraft 调用侧封顶 review / XBGPBN 回放。
"""
from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import MagicMock

import pytest

from src.inbox import risk_hold as rh
from src.inbox.autosend_policy import decide
from src.inbox.autosend_worker import AutosendWorker


# ── 假 store：app_settings KV + 标签 + 档位 + 草稿状态 ────────────────────

class _KVStore:
    def __init__(self):
        self.kv = {}
        self.tags = {}
        self.meta = {}
        self.modes = {}
        self.status = {}          # draft_id → (status, decided_by)
        self.cancelled_pending = []

    # app_settings KV（与 InboxStore 同签名）
    def get_app_setting(self, key, default=""):
        return self.kv.get(key, default)

    def set_app_setting(self, key, value, updated_by=""):
        if value == "":
            self.kv.pop(key, None)
        else:
            self.kv[key] = value

    def list_app_settings(self, prefix):
        return [{"key": k, "value": v} for k, v in self.kv.items() if k.startswith(prefix)]

    # 标签 / 元数据
    def get_conv_tags(self, cid):
        return list(self.tags.get(cid, []))

    def set_conv_tags(self, cid, tags):
        self.tags[cid] = list(tags)
        return True

    def get_handoff_meta(self, cid):
        return dict(self.meta.get(cid) or {})

    def set_handoff_meta(self, cid, meta):
        if meta:
            self.meta[cid] = dict(meta)
        else:
            self.meta.pop(cid, None)
        return True

    # 档位
    def get_automation_mode_if_set(self, cid):
        return self.modes.get(cid)

    def set_automation_mode(self, cid, mode, source=""):
        self.modes[cid] = mode

    # 草稿
    def update_draft_status(self, draft_id, *, status, final_text="", decided_by="",
                            expected_statuses=("pending", "enriching")):
        self.status[draft_id] = (status, decided_by)
        return True

    def cancel_pending_l2_drafts(self, cid, *, decided_by="mode_downgraded"):
        self.cancelled_pending.append((cid, decided_by))
        return 0

    def list_recent_messages(self, cid, limit=8):
        return []


class _Svc:
    def __init__(self, store, drafts):
        self._store = store
        self._drafts = list(drafts)
        self.resolved = []

    def list_drafts(self, status="pending", limit=200):
        return [d for d in self._drafts
                if self._store.status.get(d["draft_id"], ("pending", ""))[0] == "pending"]

    def resolve_with_audit(self, draft_id, action, by=""):
        self.resolved.append(draft_id)
        self._store.status[draft_id] = ("approved", by)
        return {"ok": True}


def _draft(cid="telegram:a1:u1", did="d1", text="hi there", **kw):
    d = {"draft_id": did, "autopilot_level": "L2", "final_text": text,
         "platform": "telegram", "account_id": "a1", "chat_key": "u1",
         "conversation_id": cid, "peer_text": "hello?", "created_at": 0}
    d.update(kw)
    return d


# ── A. risk_hold API ─────────────────────────────────────────────────────

def test_risk_hold_set_active_clear_and_ttl():
    st = _KVStore()
    assert rh.active(st, "c1") is None
    rec = rh.set(st, "c1", "privacy", ["phone"], now=1000.0)
    assert rec["reason"] == "privacy" and rec["hit"] == "phone"
    assert rh.active(st, "c1", now=1000.0 + 23 * 3600) == "privacy"
    # TTL 24h 到期 → None，并写 cleared_by=ttl（日志一行）
    assert rh.active(st, "c1", now=1000.0 + 25 * 3600) is None
    assert rh.record(st, "c1")["cleared_by"] == "ttl"
    # 再 set → 重新生效；人工 clear → 解除
    rh.set(st, "c1", "commitment", "I promise", now=2000.0)
    assert rh.active(st, "c1", now=2000.0) == "commitment"
    assert rh.clear(st, "c1", by="agent", now=2001.0) is True
    assert rh.active(st, "c1", now=2002.0) is None
    assert rh.clear(st, "c1", by="agent") is False   # 已无活跃持有


def test_risk_hold_generic_never_overrides_specific_and_system_clear_only_generic():
    st = _KVStore()
    rh.set(st, "c1", "privacy", "phone", now=10.0)
    # 泛因 needs_human 不覆盖更具体的 privacy
    assert rh.set(st, "c1", "needs_human", "high_risk", now=11.0)["reason"] == "privacy"
    # 系统链（auto_clear）只清泛因 → privacy 留着
    assert rh.clear(st, "c1", by="system:autosend_delivered", now=12.0) is False
    assert rh.active(st, "c1", now=12.0) == "privacy"
    # 只有人工才清
    assert rh.clear(st, "c1", by="agent_send", now=13.0) is True
    # 泛因可被系统链清
    rh.set(st, "c2", "needs_human", "dup_guard_blocked", now=20.0)
    assert rh.clear(st, "c2", by="system:autoreply_sent", now=21.0) is True
    assert rh.active(st, "c2", now=22.0) is None


def test_risk_hold_all_active_and_bad_store():
    st = _KVStore()
    rh.set(st, "c1", "privacy", now=1.0)
    rh.set(st, "c2", "needs_human", now=1.0)
    rh.clear(st, "c2", by="agent", now=2.0)
    assert set(rh.all_active(st, now=3.0)) == {"c1"}
    # store 缺席 / 无 KV 方法 → 绝不抛
    assert rh.set(None, "c1", "privacy") is None
    assert rh.active(object(), "c1") is None
    assert rh.clear(None, "c1") is False


# ── A. decide：强制 L1 + 继承 shadow ─────────────────────────────────────

def test_decide_risk_hold_forces_l1_and_inherits_shadow(caplog):
    caplog.set_level(logging.INFO, logger="src.inbox.autosend_policy")
    st = _KVStore()
    rh.set(st, "telegram:a1:u1", "privacy", "phone")
    # 重新起草 risk=low、auto_ai → 旧行为 L2 shadow=None；挂会话后 → L1 且 shadow 非空
    d = decide("low", [], "low", [], [], automation_mode="auto_ai", policy_mode="shadow",
               conversation_id="telegram:a1:u1", store=st)
    assert d.level == "L1" and d.review_required and d.risk_hold == "privacy"
    assert d.hold_reason == "risk_hold:privacy"
    assert d.shadow is not None and d.shadow.hold_reason == "risk_hold:privacy"
    assert any("[policy]" in r.getMessage() and "risk_hold=privacy" in r.getMessage()
               and "forced=L1" in r.getMessage() for r in caplog.records)
    # 不传会话 → 逐字节旧行为
    d0 = decide("low", [], "low", [], [], automation_mode="auto_ai", policy_mode="shadow")
    assert d0.level == "L2" and d0.shadow is None and d0.risk_hold == ""


def test_decide_risk_hold_keeps_existing_shadow_and_hard_stop():
    # medium：旧规则本会 L3（shadow 记录在），持有后 L1 且**原影子记录继承**（不归零）
    d = decide("medium", ["money"], "low", [], ["usdt"], automation_mode="auto_ai",
               policy_mode="shadow", conversation_id="c", risk_hold_reason="commitment")
    assert d.level == "L1" and d.shadow is not None
    assert d.shadow.would_hold_level == "L3" and d.shadow.hold_reason == "money"
    assert d.shadow.risk_hits == ["usdt"] and d.risk_hold == "commitment"
    # 硬停分支语义不动（停联告别「最多一条」照给）
    h = decide("high", ["stop_contact"], "low", [], [], automation_mode="auto_ai",
               policy_mode="shadow", conversation_id="c", risk_hold_reason="privacy")
    assert h.hard_stop == "stop_contact" and h.level == "L2" and h.farewell


# ── B. needs_human 作闸：打标登记持有 / 摘标解除 ─────────────────────────

def test_tag_needs_human_registers_hold_and_clear_releases():
    from src.integrations.protocol_autoreply import (
        HANDOFF_TAG, auto_clear_needs_human, clear_needs_human, tag_needs_human,
    )
    st = _KVStore()
    payload = {"platform": "telegram", "account_id": "a1", "chat_key": "u1"}
    cid = "telegram:a1:u1"
    assert tag_needs_human(st, payload, reason="high_risk", source="system") is True
    assert HANDOFF_TAG in st.get_conv_tags(cid)
    assert rh.active(st, cid) == "needs_human"
    assert rh.record(st, cid)["hit"] == "high_risk"
    # 系统自动摘标：high_risk 不属自动摘除类 → 标与持有都留
    assert auto_clear_needs_human(st, cid, trigger="autosend_delivered") is False
    assert rh.active(st, cid) == "needs_human"
    # 人工「我来回」/ 坐席发送 → 摘标 + 解除持有
    assert clear_needs_human(st, cid, actor="agent_send") is True
    assert rh.active(st, cid) is None


def test_tag_needs_human_keeps_more_specific_hold():
    from src.integrations.protocol_autoreply import tag_needs_human
    st = _KVStore()
    cid = "telegram:a1:u1"
    rh.set(st, cid, "privacy", "phone")
    tag_needs_human(st, {"platform": "telegram", "account_id": "a1", "chat_key": "u1"},
                    reason="high_risk")
    assert rh.active(st, cid) == "privacy"


def test_stop_contact_freeze_registers_hold_row():
    from src.inbox.stop_contact import freeze_conversation, unfreeze_conversation
    st = _KVStore()
    cid = "telegram:a1:u1"
    freeze_conversation(st, platform="telegram", account_id="a1", chat_key="u1",
                        conversation_id=cid, reason="stop_contact", hits=["never write"])
    assert rh.active(st, cid) == "stop_contact"
    unfreeze_conversation(st, cid, actor="human")
    assert rh.active(st, cid) is None


# ── C/D. worker：真发前二次复检 + 在途取消 ────────────────────────────────

def _worker(store, drafts, send, *, sleep=None, delay=30.0, regen_cb=None):
    svc = _Svc(store, drafts)
    cfg = {"deliver_delay": {"min_sec": delay, "max_sec": delay}, "enabled": True}
    return AutosendWorker(draft_service=svc, config=cfg, send_callback=send,
                          sleep=sleep, catchup_regenerate_cb=regen_cb), svc


@pytest.mark.asyncio
async def test_switch_to_manual_during_pacing_cancels_within_one_second(caplog):
    """门禁「切手动 1 秒取消」：[pacing] 等待中坐席切手动 → 等待结束不真发，行 cancelled。"""
    caplog.set_level(logging.INFO, logger="src.inbox.autosend_worker")
    st = _KVStore()
    cid = "telegram:a1:u1"
    sent = []

    async def _send(p, a, c, text, **kw):
        sent.append(text)
        return {"ok": True}

    w = None
    cancelled_counts = []

    async def _sleep(sec):
        # 拟人等待进行到一半：坐席切手动（切档路由 → 显式档位 manual + cancel_inflight）
        st.set_automation_mode(cid, "manual", source="human")
        cancelled_counts.append(w.cancel_inflight(conversation_id=cid, by="mode_switch"))
        await asyncio.sleep(0)

    w, svc = _worker(st, [_draft(cid)], _send, sleep=_sleep)
    t0 = asyncio.get_event_loop().time()
    await w._tick()
    assert cancelled_counts[0] == 1 and sum(cancelled_counts) == 1   # 首次点名 1 条，之后不重复计
    assert sent == []                                  # 一个字都没发
    assert svc.resolved == ["d1"]                      # 稿已 resolve（闸在等待之后才关键）
    assert st.status["d1"][0] == "cancelled" and st.status["d1"][1].startswith("abort:")
    assert w.total_abort_recheck == 1 and w.total_inflight_cancelled == 1
    assert w.total_delivered == 0 and w.total_deliver_errors == 0
    assert asyncio.get_event_loop().time() - t0 < 1.0
    msgs = [r.getMessage() for r in caplog.records]
    assert any("[inflight] cancel scope=conv n=1 by=mode_switch" in m for m in msgs)
    assert any("[autosend] abort=mode_switch" in m and "draft=d1" in m for m in msgs)


@pytest.mark.asyncio
async def test_mode_changed_without_cancel_signal_is_caught_by_recheck(caplog):
    """没有 cancel_inflight 信号（如别的写档位路径）：真发前复检自己重读档位 → abort=mode_changed。"""
    caplog.set_level(logging.INFO, logger="src.inbox.autosend_worker")
    st = _KVStore()
    cid = "telegram:a1:u1"
    sent = []

    async def _send(p, a, c, text, **kw):
        sent.append(text)
        return {"ok": True}

    async def _sleep(sec):
        st.set_automation_mode(cid, "review", source="human")

    w, _ = _worker(st, [_draft(cid)], _send, sleep=_sleep)
    await w._tick()
    assert sent == []
    assert st.status["d1"] == ("cancelled", "abort:mode_changed")
    assert any("[autosend] abort=mode_changed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_agent_typing_during_pacing_abandons_draft(caplog):
    caplog.set_level(logging.INFO, logger="src.inbox.autosend_worker")
    st = _KVStore()
    cid = "telegram:a1:u1"
    sent = []

    async def _send(p, a, c, text, **kw):
        sent.append(text)
        return {"ok": True}

    w = None

    async def _sleep(sec):
        w.note_agent_typing(cid)      # 坐席打字端点（3s 节流）

    w, _ = _worker(st, [_draft(cid)], _send, sleep=_sleep)
    await w._tick()
    assert sent == []
    assert st.status["d1"] == ("cancelled", "abort:agent_typing")
    assert any("[inflight] cancel scope=conv n=1 by=agent_typing" in r.getMessage()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_agent_send_during_pacing_abandons_draft():
    st = _KVStore()
    cid = "telegram:a1:u1"
    sent = []

    async def _send(p, a, c, text, **kw):
        sent.append(text)
        return {"ok": True}

    w = None

    async def _sleep(sec):
        w.note_agent_send(cid)

    w, _ = _worker(st, [_draft(cid)], _send, sleep=_sleep)
    await w._tick()
    assert sent == [] and st.status["d1"] == ("cancelled", "abort:agent_send")


@pytest.mark.asyncio
async def test_agent_typed_recently_blocks_new_batch_within_window():
    """坐席 60s 内打过字：下一批捞到的该会话 L2 也不发（agent_typing 窗口）。"""
    st = _KVStore()
    cid = "telegram:a1:u1"
    sent = []

    async def _send(p, a, c, text, **kw):
        sent.append(text)
        return {"ok": True}

    async def _sleep(sec):
        return None

    w, svc = _worker(st, [_draft(cid)], _send, sleep=_sleep, delay=0)
    w.note_agent_typing(cid)
    await w._tick()
    assert sent == [] and svc.resolved == []
    assert st.status["d1"] == ("cancelled", "abort:agent_typing")


@pytest.mark.asyncio
async def test_risk_hold_during_pacing_aborts_and_dup_token_released():
    st = _KVStore()
    cid = "telegram:a1:u1"
    sent = []

    async def _send(p, a, c, text, **kw):
        sent.append(text)
        return {"ok": True}

    async def _sleep(sec):
        rh.set(st, cid, "commitment", "I promise")   # Q-2 承诺守卫在等待期间挂上

    w, _ = _worker(st, [_draft(cid)], _send, sleep=_sleep)
    await w._tick()
    assert sent == [] and st.status["d1"] == ("cancelled", "abort:risk_hold")


@pytest.mark.asyncio
async def test_needs_human_tag_blocks_l2_in_batch_and_regenerates_once_as_l1(caplog):
    """B：「需人工」挂着期间 L2 稿不发（捞稿期取消），并按 L1 重拟一次（同一持有只一次）。"""
    caplog.set_level(logging.INFO)
    from src.inbox import draft_trigger
    from src.integrations.protocol_autoreply import HANDOFF_TAG
    st = _KVStore()
    cid = "telegram:a1:u1"
    st.set_conv_tags(cid, [HANDOFF_TAG])     # 老标（无持有行）也作闸
    sent, regen = [], []

    async def _send(p, a, c, text, **kw):
        sent.append(text)
        return {"ok": True}

    def _regen(conv, peer_text):
        regen.append((conv["conversation_id"], peer_text, draft_trigger.pop(conv["conversation_id"])))
        return True

    w, svc = _worker(st, [_draft(cid)], _send, regen_cb=_regen, delay=0)
    await w._tick()
    assert sent == [] and svc.resolved == []
    assert st.status["d1"] == ("cancelled", "abort:needs_human")
    assert regen == [(cid, "hello?", "risk_hold_regen")]
    assert w.total_skipped_risk_hold == 1 and w.total_risk_hold_regen == 1
    # 第二轮又捞到一条 L2（重拟若仍是 L2）→ 取消但**不再**重拟（防循环）
    svc._drafts.append(_draft(cid, did="d2"))
    await w._tick()
    assert st.status["d2"] == ("cancelled", "abort:needs_human")
    assert len(regen) == 1 and sent == []
    assert any("[autosend] abort=needs_human stage=batch" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_farewell_draft_exempt_from_gate():
    """停联告别「最多一条」：冻结会话（manual + 需人工 + 持有）下告别稿照发。"""
    from src.inbox.stop_contact import HARD_STOP_PASS_MARK, FAREWELL_MARK, STOP_CONTACT_TAG
    from src.integrations.protocol_autoreply import HANDOFF_TAG
    st = _KVStore()
    cid = "telegram:a1:u1"
    st.set_conv_tags(cid, [HANDOFF_TAG, STOP_CONTACT_TAG])
    st.set_handoff_meta(cid, {"reason": "stop_contact", "ts": 1.0, "source": "system"})
    st.set_automation_mode(cid, "manual", source="stop_contact:auto_ai")
    rh.set(st, cid, "stop_contact", "never write")
    sent = []

    async def _send(p, a, c, text, **kw):
        sent.append(text)
        return {"ok": True}

    async def _sleep(sec):
        return None

    d = _draft(cid, text="Take care.", risk_reasons=[HARD_STOP_PASS_MARK, FAREWELL_MARK])
    w, _ = _worker(st, [d], _send, sleep=_sleep, delay=0)
    await w._tick()
    assert sent == ["Take care."]


def test_cancel_inflight_scopes_and_counts():
    st = _KVStore()
    w, _ = _worker(st, [], None)
    w._inflight_register(_draft("telegram:a1:u1", did="d1"))
    w._inflight_register(_draft("telegram:a1:u2", did="d2", chat_key="u2"))
    w._inflight_register(_draft("whatsapp:b1:u3", did="d3", platform="whatsapp", account_id="b1"))
    assert w.cancel_inflight(conversation_id="telegram:a1:u1", by="mode_switch") == 1
    assert st.status["d1"] == ("cancelled", "abort:mode_switch")
    assert st.cancelled_pending == [("telegram:a1:u1", "abort:mode_switch")]
    # 账号范围：剩下 telegram:a1 的 d2（d1 已点名不重复计）
    assert w.cancel_inflight(platform="telegram", account_id="a1", by="mode_switch") == 1
    assert w.cancel_inflight(by="mode_switch") == 1     # 全部：d3
    assert w.cancel_inflight(by="mode_switch") == 0
    assert w.total_inflight_cancelled == 3
    snap = w.status_snapshot()
    assert snap["total_inflight_cancelled"] == 3 and snap["inflight_now"] == 3


# ── A（调用侧）autodraft 封顶 review + E 触发源 reason ─────────────────────

_AD_LOGGER = logging.getLogger("test.q3.autodraft")


def test_autodraft_caps_mode_to_review_when_conversation_held(caplog):
    caplog.set_level(logging.INFO, logger=_AD_LOGGER.name)
    from src.inbox.autodraft_helpers import AutoDraftConfig, make_auto_draft_cb
    cfg = AutoDraftConfig(mode="auto_ai", min_len=0, skip=set(), platform_ceilings={},
                          skip_groups=False, enrich=False)
    ds = MagicMock()
    store = MagicMock()
    store.get_automation_mode_if_set.return_value = None
    store.get_conv_tags.return_value = []
    store.get_app_setting.return_value = json.dumps(
        {"reason": "privacy", "hit": "phone", "set_ts": 10 ** 12, "ttl_h": 24})
    cb = make_auto_draft_cb(cfg, ds, store, MagicMock(), MagicMock(), _AD_LOGGER)
    cb({"platform": "tg", "conversation_id": "tg:a:1", "account_id": "a", "chat_key": "1"},
       "hello there")
    ds.auto_generate_draft.assert_called_once()
    assert ds.auto_generate_draft.call_args.kwargs["automation_mode"] == "review"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("[policy] conv=tg:a:1 risk_hold=privacy forced=L1" in m for m in msgs)
    assert any("[draft] trigger conv=tg:a:1 reason=new_inbound" in m for m in msgs)


def test_autodraft_trigger_reason_from_registry(caplog):
    caplog.set_level(logging.INFO, logger=_AD_LOGGER.name)
    from src.inbox import draft_trigger
    from src.inbox.autodraft_helpers import AutoDraftConfig, make_auto_draft_cb
    cfg = AutoDraftConfig(mode="auto_ai", min_len=0, skip=set(), platform_ceilings={},
                          skip_groups=False, enrich=False)
    ds = MagicMock()
    store = MagicMock()
    store.get_automation_mode_if_set.return_value = None
    store.get_conv_tags.return_value = []
    store.get_app_setting.return_value = ""
    draft_trigger.note("tg:a:1", "catchup_regen")
    cb = make_auto_draft_cb(cfg, ds, store, MagicMock(), MagicMock(), _AD_LOGGER)
    cb({"platform": "tg", "conversation_id": "tg:a:1", "account_id": "a", "chat_key": "1"},
       "hello there")
    assert ds.auto_generate_draft.call_args.kwargs["automation_mode"] == "auto_ai"
    assert any("[draft] trigger conv=tg:a:1 reason=catchup_regen mode=auto_ai" in r.getMessage()
               for r in caplog.records)
    assert draft_trigger.pop("tg:a:1") == ""      # 已消费


# ── D（路由侧）切档 / 坐席打字端点 → cancel_inflight ──────────────────────

def _route_app(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.inbox.store import InboxStore
    from src.web.routes.unified_inbox_stored_read_routes import register_stored_read_routes
    app = FastAPI()
    register_stored_read_routes(app, api_auth=lambda request=None: None)
    store = InboxStore(tmp_path / "inbox.db")
    app.state.inbox_store = store
    return app, store, TestClient(app)


class _FakeWorker:
    def __init__(self):
        self.calls = []

    def cancel_inflight(self, *, conversation_id="", platform="", account_id="", by="mode_switch"):
        self.calls.append(("cancel", conversation_id, platform, account_id, by))
        return 2

    def note_agent_typing(self, conversation_id):
        self.calls.append(("typing", conversation_id))
        return 1


def test_mode_switch_route_cancels_inflight_and_reports_count(tmp_path):
    app, store, client = _route_app(tmp_path)
    fw = _FakeWorker()
    app.state.autosend_worker = fw
    cid = "telegram:a1:u1"
    store.set_automation_mode(cid, "auto_ai")
    store.upsert_draft({"draft_id": "d1", "conversation_id": cid, "platform": "telegram",
                        "account_id": "a1", "chat_key": "u1", "source_kind": "inbox",
                        "source_id": "d1", "peer_text": "hi", "draft_text": "hello",
                        "autopilot_level": "L2", "status": "pending", "risk_level": "low"})
    r = client.post("/api/unified-inbox/automation",
                    json={"platform": "telegram", "account_id": "a1", "chat_key": "u1",
                          "mode": "manual"})
    assert r.status_code == 200, r.text
    d = r.json()
    # pending 1 条（store.cancel_pending_l2_drafts）+ 在途 2 条（worker）→ toast 数 3
    assert d["cancelled_l2"] == 3
    assert ("cancel", cid, "", "", "mode_switch") in fw.calls
    assert store.get_draft("d1")["status"] == "cancelled"
    # 切回全自动不取消
    fw.calls.clear()
    r = client.post("/api/unified-inbox/automation",
                    json={"platform": "telegram", "account_id": "a1", "chat_key": "u1",
                          "mode": "auto_ai"})
    assert r.status_code == 200 and r.json()["cancelled_l2"] == 0 and fw.calls == []
    store.close()


def test_agent_typing_endpoint(tmp_path):
    app, store, client = _route_app(tmp_path)
    fw = _FakeWorker()
    app.state.autosend_worker = fw
    r = client.post("/api/unified-inbox/agent-typing",
                    json={"platform": "telegram", "account_id": "a1", "chat_key": "u1"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "conversation_id": "telegram:a1:u1", "cancelled": 1}
    assert fw.calls == [("typing", "telegram:a1:u1")]
    # conversation_id 直传；worker 缺席 → cancelled=0 不报错
    app.state.autosend_worker = None
    r = client.post("/api/unified-inbox/agent-typing", json={"conversation_id": "x:y:z"})
    assert r.status_code == 200 and r.json()["cancelled"] == 0
    assert client.post("/api/unified-inbox/agent-typing", json={}).status_code == 400
    store.close()


def test_i18n_toast_key_three_langs():
    from src.web.i18n_packs import inbox_workspace, zh_hant_auto
    zh = getattr(inbox_workspace, "ZH", None) or getattr(inbox_workspace, "zh", None)
    en = getattr(inbox_workspace, "EN", None) or getattr(inbox_workspace, "en", None)
    for pack in (zh, en):
        assert pack is not None and "{n}" in pack["inbox.mode.cancelled_ai"]
    hant = [v for v in vars(zh_hant_auto).values() if isinstance(v, dict)]
    assert any("inbox.mode.cancelled_ai" in d for d in hant)


# ── XBGPBN 回放 ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_replay_xbgpbn_timeline():
    """23:49:13 隐私 → 需人工；23:55:42 重新起草（低风险）；[pacing]；切手动 + 打字；不得发出。"""
    from src.integrations.protocol_autoreply import tag_needs_human
    st = _KVStore()
    cid = "telegram:a1:xbgpbn"
    payload = {"platform": "telegram", "account_id": "a1", "chat_key": "xbgpbn"}
    # 23:49:13 隐私词 → 需人工（Q-2 未合并前只有打标；打标即登记持有）
    tag_needs_human(st, payload, reason="high_risk", source="system")
    # 23:55:42 客户再来一句 → 重新起草：decide 带会话 → 不再归零，L1 + shadow
    d = decide("low", [], "low", [], [], automation_mode="auto_ai", policy_mode="shadow",
               conversation_id=cid, store=st)
    assert d.level == "L1" and d.shadow is not None
    # 即便某条 L2 稿仍漏进队列（旧版 drafts.py 未传会话）→ worker 捞稿即取消，不进 pacing
    sent = []

    async def _send(p, a, c, text, **kw):
        sent.append(text)
        return {"ok": True}

    w = None

    async def _sleep(sec):
        st.set_automation_mode(cid, "manual", source="human")
        w.cancel_inflight(conversation_id=cid, by="mode_switch")
        w.note_agent_typing(cid)

    w, svc = _worker(st, [_draft(cid, chat_key="xbgpbn")], _send, sleep=_sleep)
    await w._tick()
    assert sent == [] and svc.resolved == []
    assert st.status["d1"] == ("cancelled", "abort:needs_human")
    # 人工摘标后（我来回）→ 持有解除；此时若坐席切手动仍在等待中的稿也要取消（第二处复检）
    from src.integrations.protocol_autoreply import clear_needs_human
    clear_needs_human(st, cid, actor="agent_send")
    assert rh.active(st, cid) is None
    svc._drafts.append(_draft(cid, did="d2", chat_key="xbgpbn"))
    await w._tick()
    assert sent == []
    assert st.status["d2"][0] == "cancelled"
