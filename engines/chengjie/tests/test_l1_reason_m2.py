# -*- coding: utf-8 -*-
"""M-2 E（#235 D-M10 / #223 D-M9）契约：发前确认原因可见 + 手动粘性。

- ``l1_reason.derive_l1_reason``：冷静期 > 无人设 > 会话显式人审 > 其它封顶层 > 语言未定 >
  客户首条 > 证据不足 > 账号默认 > 全局默认；
- ``drafts.auto_generate_draft`` 的 L1 日志行带 ``reason=``（D-M10 唯一许可的一行，只读注册表）；
- ``takeover_rearm``：全局开关关（D-M9 默认）时手动是粘的——只有坐席显式选「30 分钟后接回」
  （source=takeover_opt_from:*）才被 sweep 接回；``rearm_state`` 如实报 sticky / opt_in。
"""
from __future__ import annotations

import logging
import time
import uuid

import pytest

from src.inbox.store import InboxStore


@pytest.fixture(autouse=True)
def _fresh():
    import src.inbox.l1_reason as l1
    l1._reset_for_tests()
    yield
    l1._reset_for_tests()


class _Cap:
    def __init__(self, layer):
        self.layer = layer


class _StoreFirst:
    def __init__(self, inbound_n):
        self._n = inbound_n

    def list_recent_messages(self, cid, limit=6):
        return [{"direction": "in", "text": "x"}] * self._n


def test_derive_priority():
    from src.inbox.l1_reason import derive_l1_reason
    conv_en = {"conversation_id": "messenger:a:c1", "language": "en"}
    # auto_ai 不是 L1
    assert derive_l1_reason(mode="auto_ai") == ""
    # 冷静期最优先
    assert derive_l1_reason(mode="review", caps_applied=[_Cap("login_cooldown"), _Cap("persona_unselected")],
                            conv=conv_en, peer_text="Hello there") == "cooldown"
    assert derive_l1_reason(mode="review", caps_applied=[_Cap("persona_unselected")],
                            conv=conv_en, peer_text="Hello there") == "no_persona"
    # 会话显式人审：原因就是「你设的」
    assert derive_l1_reason(mode="review", explicit_mode="review", conv=conv_en,
                            peer_text="Hi") == "manual_review"
    # 其它封顶层
    assert derive_l1_reason(mode="review", caps_applied=[_Cap("channel_disconnected")],
                            conv=conv_en, peer_text="Hello there",
                            store=_StoreFirst(5)) == "channel_disconnected"
    # 账号/全局默认半自动 → 证据码：语言未定 > 首条 > 证据不足
    conv_unk = {"conversation_id": "messenger:a:c2", "language": "unknown"}
    # 纯表情：无文字系统证据 + 会话语言未知 → 语言未定（「Hllo」这类拉丁拼写错误是有证据的 en，不算）
    assert derive_l1_reason(mode="review", conv=conv_unk, peer_text="👋👋",
                            store=_StoreFirst(5), account_layer="review") == "lang_unknown"
    assert derive_l1_reason(mode="review", conv=conv_en, peer_text="Hello there",
                            store=_StoreFirst(1), account_layer="review") == "first_contact"
    assert derive_l1_reason(mode="review", conv=conv_en, peer_text="ok",
                            store=_StoreFirst(5), account_layer="review") == "weak_evidence"
    assert derive_l1_reason(mode="review", conv=conv_en, peer_text="Hello there again",
                            store=_StoreFirst(5), account_layer="review") == "account_default"
    assert derive_l1_reason(mode="review", conv=conv_en, peer_text="Hello there again",
                            store=_StoreFirst(5)) == "global_default"


def test_registry_note_peek():
    from src.inbox.l1_reason import note, peek
    note("messenger:a:c9", "cooldown")
    assert peek("messenger:a:c9") == "cooldown"
    assert peek("nope") == ""
    note("", "x")
    note("k", "")
    assert peek("") == "" and peek("k") == ""


def test_drafts_l1_log_line_carries_reason(tmp_path, caplog):
    """D-M10：drafts.py 的 L1 日志行只读注册表输出 reason=，不改判定。"""
    from src.inbox.drafts import DraftService
    from src.inbox.l1_reason import note
    store = InboxStore(tmp_path / "l1.db")
    svc = DraftService(inbox_store=store, cfg={})
    cid = f"messenger:a:{uuid.uuid4().hex[:6]}"
    note(cid, "cooldown")
    caplog.set_level(logging.INFO, logger="src.inbox.drafts")
    did = svc.auto_generate_draft(
        {"conversation_id": cid, "platform": "messenger", "account_id": "a", "chat_key": "k"},
        "Hello there, how are you today?", automation_mode="review", enrich=False)
    assert did
    lines = [r.getMessage() for r in caplog.records if "auto_generate_draft L1" in r.getMessage()]
    assert lines and "reason=cooldown" in lines[0]
    assert store.get_draft(did)["autopilot_level"] == "L1"


# ── D-M9 手动粘性 ────────────────────────────────────────────────────────────

def test_rearm_sticky_by_default_opt_in_still_rearms(tmp_path):
    from src.inbox.takeover_rearm import (
        is_rearm_opt_in_source, is_takeover_source, opt_in_rearm_source,
        record_agent_takeover, rearm_state, sweep_takeover_rearm, takeover_prev_mode,
    )
    store = InboxStore(tmp_path / "rearm.db")
    cfg_off = {"inbox": {"takeover_rearm": {"enabled": False, "after_minutes": 30},
                         "auto_draft": {"automation_mode": "review"}}}
    c_send = "messenger:a:send"    # 手动发送触发的接管 → 粘
    c_opt = "messenger:a:opt"      # 坐席显式选「30 分钟后接回」→ 接回
    c_human = "messenger:a:human"  # 下拉「一直手动」→ 粘
    store.set_automation_mode(c_send, "auto_ai", source="human")
    record_agent_takeover(store, c_send)
    store.set_automation_mode(c_opt, "auto_ai", source="human")
    store.set_automation_mode(c_opt, "manual", source=opt_in_rearm_source("auto_ai"))
    store.set_automation_mode(c_human, "manual", source="human")
    assert is_takeover_source(opt_in_rearm_source("auto_ai")) is True
    assert is_rearm_opt_in_source(opt_in_rearm_source("auto_ai")) is True
    assert takeover_prev_mode(opt_in_rearm_source("auto_ai")) == "auto_ai"
    assert takeover_prev_mode(opt_in_rearm_source("manual")) == ""   # 无接管前档 → 全局默认
    # 把三行的时间戳都推到 31 分钟前
    store._conn.execute("UPDATE conversation_settings SET updated_at = ?",
                        (time.time() - 31 * 60,))
    store._conn.commit()
    out = sweep_takeover_rearm(store, cfg_off)
    assert out["enabled"] is False
    assert out["restored_cids"] == [c_opt]
    assert store.get_automation_mode_if_set(c_opt) == "auto_ai"
    assert store.get_automation_mode_if_set(c_send) == "manual"    # 手动发送接管：粘
    assert store.get_automation_mode_if_set(c_human) == "manual"   # 一直手动：粘
    # rearm_state：粘性态 sticky=True；opt-in 态 enabled=True（横幅倒计时）
    meta_send = store.get_automation_mode_meta(c_send)
    st = rearm_state(meta_send, cfg_off)
    assert st is not None and st["sticky"] is True and st["enabled"] is False
    store.set_automation_mode(c_opt, "manual", source=opt_in_rearm_source("auto_ai"))
    st2 = rearm_state(store.get_automation_mode_meta(c_opt), cfg_off)
    assert st2["opt_in"] is True and st2["enabled"] is True and st2["restore_mode"] == "auto_ai"


def test_desktop_seeds_rearm_off():
    import yaml
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for name in ("config/config.desktop.min.yaml", "config/config.desktop.internal.yaml"):
        cfg = yaml.safe_load((root / name).read_text("utf-8")) or {}
        tr = ((cfg.get("inbox") or {}).get("takeover_rearm") or {})
        assert tr.get("enabled") is False, name
