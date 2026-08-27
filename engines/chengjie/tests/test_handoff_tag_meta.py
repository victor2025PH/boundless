# -*- coding: utf-8 -*-
"""实施74 阶段4 门禁（实施69 P1-1/P1-2）：「需人工」可解释 + 右键标签直达。

实施69 §1.2 实录：打标不存原因/时间/来源，用户只能来问「为什么」；右键菜单
9 项里没有「移除标签」直达，用户断定「没这功能」。
"""
from __future__ import annotations

from pathlib import Path

from src.inbox.store import InboxStore
from src.integrations.protocol_autoreply import (
    HANDOFF_TAG,
    clear_needs_human,
    tag_needs_human,
)

_ENGINE_ROOT = Path(__file__).resolve().parents[1]

_PAYLOAD = {"platform": "whatsapp", "account_id": "a1", "chat_key": "c1"}


def _cid() -> str:
    from src.inbox.normalizer import conv_id
    return conv_id("whatsapp", "a1", "c1")


# ── store 层 ─────────────────────────────────────────────────────────────────

def test_handoff_meta_roundtrip(tmp_path):
    st = InboxStore(tmp_path / "inbox.db")
    assert st.get_handoff_meta("x") == {}
    st.set_handoff_meta("x", {"reason": "empty_reply", "ts": 123.0,
                              "source": "system"})
    got = st.get_handoff_meta("x")
    assert got["reason"] == "empty_reply" and got["source"] == "system"
    st.set_handoff_meta("x", None)
    assert st.get_handoff_meta("x") == {}
    st.close()


def test_list_conv_tags_map_carries_handoff_meta(tmp_path):
    st = InboxStore(tmp_path / "inbox.db")
    st.set_conv_tags("c-1", [HANDOFF_TAG])
    st.set_handoff_meta("c-1", {"reason": "send_error", "ts": 9.0,
                                "source": "system"})
    got = st.list_conv_tags_map(["c-1"])
    assert got["c-1"]["handoff_meta"]["reason"] == "send_error"
    assert HANDOFF_TAG in got["c-1"]["tags"]
    st.close()


# ── 打标/清标链 ──────────────────────────────────────────────────────────────

def test_tag_needs_human_stores_reason_ts_source(tmp_path):
    st = InboxStore(tmp_path / "inbox.db")
    assert tag_needs_human(st, _PAYLOAD, reason="empty_reply", now=1000.0)
    meta = st.get_handoff_meta(_cid())
    assert meta == {"reason": "empty_reply", "ts": 1000.0, "source": "system"}
    # 已打标 → 跳过且不覆写元数据（首因保留）
    assert not tag_needs_human(st, _PAYLOAD, reason="send_error", now=2000.0)
    assert st.get_handoff_meta(_cid())["reason"] == "empty_reply"
    st.close()


def test_clear_needs_human_clears_meta(tmp_path):
    st = InboxStore(tmp_path / "inbox.db")
    tag_needs_human(st, _PAYLOAD, reason="quota_day", now=1.0)
    assert clear_needs_human(st, _cid())
    assert st.get_handoff_meta(_cid()) == {}
    assert HANDOFF_TAG not in st.get_conv_tags(_cid())
    st.close()


def test_old_store_without_meta_method_still_tags():
    """旧 store（无 set_handoff_meta）→ 打标照常成功，绝不因元数据翻车。"""
    class _OldStore:
        def __init__(self):
            self.tags = {}

        def get_conv_tags(self, cid):
            return list(self.tags.get(cid, []))

        def set_conv_tags(self, cid, tags):
            self.tags[cid] = list(tags)

    st = _OldStore()
    assert tag_needs_human(st, _PAYLOAD, reason="high_risk")
    assert HANDOFF_TAG in st.tags[_cid()]


# ── 接线契约（静态）─────────────────────────────────────────────────────────

def test_autoreply_callsite_passes_reason():
    src = (_ENGINE_ROOT / "src" / "integrations" / "protocol_autoreply.py"
           ).read_text(encoding="utf-8")
    assert 'reason=str((res or {}).get("reason") or "")' in src


def test_tags_put_route_clears_meta_on_removal():
    src = (_ENGINE_ROOT / "src" / "web" / "routes"
           / "unified_inbox_workspace_tags_routes.py").read_text(encoding="utf-8")
    assert "set_handoff_meta(conversation_id, None)" in src


def test_chats_rows_carry_handoff_meta():
    src = (_ENGINE_ROOT / "src" / "web" / "routes"
           / "unified_inbox_read_routes.py").read_text(encoding="utf-8")
    assert 'c["handoff_meta"]' in src


def test_frontend_tooltip_and_ctx_removal_wired():
    src = (_ENGINE_ROOT / "src" / "web" / "templates" / "unified_inbox.html"
           ).read_text(encoding="utf-8")
    assert "function _handoffTip(" in src
    assert "_HANDOFF_REASON_KEYS" in src          # 查表取词，勿拼键名
    assert "_tagChipHtml(t,c)" in src             # 行 chip 带会话上下文
    assert "tag_ctx_remove" in src                # 右键直摘埋点
    assert "inbox.ctx.rm_tag_t" in src


def test_i18n_keys_bilingual():
    from src.web.i18n_packs.inbox_workspace import EN, ZH
    keys = ["inbox.handoff.tip", "inbox.handoff.tip_generic",
            "inbox.handoff.src_system", "inbox.handoff.src_manual",
            "inbox.ctx.rm_tag_t"] + [
        f"inbox.handoff.r_{r}" for r in (
            "high_risk", "empty_reply", "generate_error", "send_error",
            "quota_hour", "quota_day", "circuit_open", "off_hours")]
    for k in keys:
        assert k in ZH and k in EN, k
