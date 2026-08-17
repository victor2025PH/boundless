# -*- coding: utf-8 -*-
"""P1 (2026-08-15) 读取召回增强：预览指纹增量 + 通知预扫 端到端。

覆盖:
  * fb_store row_state / thread_map 读写语义 (tmp_db)
  * check_messenger_inbox 的 should_open 三信号门 (未读 / 通知 / 预览变化)
    在真实 fb_store (tmp_db) 上端到端跑通, 验证「未读判假阴仍被救回」。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ─── fb_store row_state ──────────────────────────────────────────────
class TestRowState:
    def test_first_seen_not_changed(self, tmp_db):
        from src.host.fb_store import messenger_row_preview_changed
        # 首见 → False (首轮召回交给未读判定)
        assert messenger_row_preview_changed("d1", "Alice", "fp1") is False

    def test_empty_fp_not_changed(self, tmp_db):
        from src.host.fb_store import (messenger_row_preview_changed,
                                       update_messenger_row_state)
        update_messenger_row_state("d1", "Alice", preview_fp="fp1")
        assert messenger_row_preview_changed("d1", "Alice", "") is False

    def test_same_fp_not_changed(self, tmp_db):
        from src.host.fb_store import (messenger_row_preview_changed,
                                       update_messenger_row_state)
        update_messenger_row_state("d1", "Alice", preview_fp="fp1")
        assert messenger_row_preview_changed("d1", "Alice", "fp1") is False

    def test_different_fp_changed(self, tmp_db):
        from src.host.fb_store import (messenger_row_preview_changed,
                                       update_messenger_row_state)
        update_messenger_row_state("d1", "Alice", preview_fp="fp1")
        assert messenger_row_preview_changed("d1", "Alice", "fp2") is True

    def test_empty_fp_not_overwrite_baseline(self, tmp_db):
        from src.host.fb_store import (messenger_row_preview_changed,
                                       update_messenger_row_state)
        update_messenger_row_state("d1", "Alice", preview_fp="fp1")
        update_messenger_row_state("d1", "Alice", preview_fp="")  # 空不覆盖
        # 基线仍是 fp1 → 换新指纹应判变化
        assert messenger_row_preview_changed("d1", "Alice", "fp2") is True

    def test_device_isolation(self, tmp_db):
        from src.host.fb_store import (messenger_row_preview_changed,
                                       update_messenger_row_state)
        update_messenger_row_state("d1", "Alice", preview_fp="fp1")
        # d2 的 Alice 首见 → False (不受 d1 影响)
        assert messenger_row_preview_changed("d2", "Alice", "fpX") is False


class TestThreadMap:
    def test_insert_and_update(self, tmp_db):
        from src.host.fb_store import upsert_thread_map
        from src.host.database import _connect
        upsert_thread_map("d1", "Alice", "tok1", source="notif")
        upsert_thread_map("d1", "Alice", "tok2", source="header")
        with _connect() as conn:
            row = conn.execute(
                "SELECT thread_token, source FROM messenger_thread_map"
                " WHERE device_id=? AND peer_name=?", ("d1", "Alice")).fetchone()
        assert row["thread_token"] == "tok2"
        assert row["source"] == "header"

    def test_empty_token_noop(self, tmp_db):
        from src.host.fb_store import upsert_thread_map
        from src.host.database import _connect
        upsert_thread_map("d1", "Alice", "")  # 空 token 不写
        with _connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM messenger_thread_map"
                " WHERE device_id=? AND peer_name=?", ("d1", "Alice")).fetchone()
        assert row is None


# ─── check_messenger_inbox should_open 三信号门 (端到端) ───────────────
class TestShouldOpenRecall:
    def _make_fb(self):
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)
        fb.hb = MagicMock()
        return fb

    def _run(self, fb, convs, notif_peers, opened, *,
             search_ok=True, search_mismatch_peers=(),
             phase_cfg=("growth", {}), open_gate=None):
        fake_u2 = MagicMock()

        def _open(d, c, did, entered=False, verify_name="", **kw):
            name = c["name"]
            # 搜索路径 (verify_name) 校验失败模拟
            if verify_name and verify_name in search_mismatch_peers:
                return {"peer_name": verify_name, "search_mismatch": True}
            opened.append(name)
            return {"peer_name": name, "incoming_text": "hi"}

        with patch.object(fb, "_did", return_value="devA"), \
             patch.object(fb, "_u2", return_value=fake_u2), \
             patch.object(fb, "_dismiss_dialogs"), \
             patch.object(fb, "_detect_no_network_banner",
                          return_value=False), \
             patch.object(fb, "_detect_risk_dialog",
                          return_value=(False, "")), \
             patch.object(fb, "_prescan_notifications",
                          return_value=notif_peers), \
             patch.object(fb, "_list_messenger_conversations",
                          return_value=convs), \
             patch.object(fb, "_open_thread_by_search",
                          return_value=search_ok), \
             patch.object(fb, "_open_and_read_conversation",
                          side_effect=_open), \
             patch("src.app_automation.facebook._resolve_phase_and_cfg",
                   return_value=phase_cfg), \
             patch("src.app_automation.facebook.time.sleep"), \
             patch("src.app_automation.facebook.random.uniform",
                   return_value=0.0):
            return fb.check_messenger_inbox(auto_reply=False,
                                            max_conversations=10,
                                            open_gate=open_gate)

    def test_notif_forces_open_when_unread_false(self, tmp_db):
        """未读判定判 False, 但通知里有该 peer → 强制打开 (notif_forced)."""
        fb = self._make_fb()
        opened = []
        convs = [{"name": "Alice", "unread": False, "preview_fp": "fpA"}]
        stats = self._run(fb, convs, {"Alice": "key1"}, opened)
        assert opened == ["Alice"]
        assert stats["notif_forced"] == 1
        assert stats["notif_active_peers"] == 1

    def test_preview_change_forces_open(self, tmp_db):
        """未读 False + 通知无, 但预览内容核变化 → 强制打开 (preview_forced)."""
        from src.host.fb_store import update_messenger_row_state
        fb = self._make_fb()
        update_messenger_row_state("devA", "Bob", preview_fp="OLD")  # 基线
        opened = []
        convs = [{"name": "Bob", "unread": False, "preview_fp": "NEW"}]
        stats = self._run(fb, convs, {}, opened)
        assert opened == ["Bob"]
        assert stats["preview_forced"] == 1

    def test_read_and_unchanged_not_opened(self, tmp_db):
        """已读 + 无通知 + 预览未变 → 不打开 (省一次进出)."""
        from src.host.fb_store import update_messenger_row_state
        fb = self._make_fb()
        update_messenger_row_state("devA", "Carol", preview_fp="SAME")
        opened = []
        convs = [{"name": "Carol", "unread": False, "preview_fp": "SAME"}]
        stats = self._run(fb, convs, {}, opened)
        assert opened == []
        assert stats["notif_forced"] == 0
        assert stats["preview_forced"] == 0

    def test_unread_still_opens_and_records_baseline(self, tmp_db):
        """未读正常打开, 并落基线指纹 → 下次同预览不再重复打开."""
        from src.host.fb_store import messenger_row_preview_changed
        fb = self._make_fb()
        opened = []
        convs = [{"name": "Dave", "unread": True, "preview_fp": "fpD"}]
        self._run(fb, convs, {}, opened)
        assert opened == ["Dave"]
        # 打开后基线已落: 同 fp 不再判变化
        assert messenger_row_preview_changed("devA", "Dave", "fpD") is False

    def test_offscreen_notif_counted(self, tmp_db):
        """通知里有、列表没列出的 peer → notif_offscreen 计数."""
        fb = self._make_fb()
        opened = []
        convs = [{"name": "Alice", "unread": True, "preview_fp": "fpA"}]
        stats = self._run(fb, convs, {"Alice": "k1", "GhostPeer": "k2"}, opened)
        # GhostPeer 在通知但不在列表
        assert stats["notif_offscreen"] == 1


# ─── P2: 屏外搜索打开 (端到端) ───────────────────────────────────────
class TestSearchOpen(TestShouldOpenRecall):
    def test_offscreen_peer_search_opened(self, tmp_db):
        """通知里有、列表没有 → 搜索进入读取 (search_opened, 只读不回)."""
        fb = self._make_fb()
        opened = []
        convs = [{"name": "Alice", "unread": True, "preview_fp": "fpA"}]
        stats = self._run(fb, convs, {"Alice": "k1", "Ghost": "k2"}, opened)
        assert "Ghost" in opened            # 屏外 Ghost 被搜索打开
        assert stats["search_opened"] == 1
        assert stats["notif_offscreen"] == 1

    def test_search_mismatch_aborts(self, tmp_db):
        """搜索误点到别人 (verify 校验失败) → search_mismatch, 不入库."""
        fb = self._make_fb()
        opened = []
        convs = [{"name": "Alice", "unread": True, "preview_fp": "fpA"}]
        stats = self._run(fb, convs, {"Alice": "k1", "Ghost": "k2"}, opened,
                          search_mismatch_peers={"Ghost"})
        assert "Ghost" not in opened        # 误点被放弃, 没读没入库
        assert stats["search_mismatch"] == 1
        assert stats["search_opened"] == 0

    def test_search_entry_failed(self, tmp_db):
        """搜索进入失败 (搜索链抛/无结果) → search_failed."""
        fb = self._make_fb()
        opened = []
        convs = [{"name": "Alice", "unread": True, "preview_fp": "fpA"}]
        stats = self._run(fb, convs, {"Alice": "k1", "Ghost": "k2"}, opened,
                          search_ok=False)
        assert "Ghost" not in opened
        assert stats["search_failed"] == 1
        assert stats["search_opened"] == 0

    def test_max_search_opens_zero_disables(self, tmp_db):
        """playbook max_search_opens=0 → 完全不搜索 (仅计数 offscreen)."""
        fb = self._make_fb()
        opened = []
        convs = [{"name": "Alice", "unread": True, "preview_fp": "fpA"}]
        stats = self._run(fb, convs, {"Alice": "k1", "Ghost": "k2"}, opened,
                          phase_cfg=("cold_start", {"max_search_opens": 0}))
        assert "Ghost" not in opened
        assert stats["search_opened"] == 0
        assert stats["notif_offscreen"] == 1  # 仍计数

    def test_search_limit_capped(self, tmp_db):
        """多个屏外 peer → 限流 max_search_opens (默认 2)."""
        fb = self._make_fb()
        opened = []
        convs = [{"name": "Alice", "unread": True, "preview_fp": "fpA"}]
        notif = {"Alice": "k0", "G1": "k1", "G2": "k2", "G3": "k3"}
        stats = self._run(fb, convs, notif, opened)
        # 3 个屏外, 默认限流 2
        assert stats["search_opened"] == 2
        assert stats["notif_offscreen"] == 3


class TestOpenGate(TestShouldOpenRecall):
    """P4 (117↔176 交接预留): open_gate 可注入谓词 — referral_mode 的落点。

    契约: None=零行为变化; 谓词只在门开后追加过滤; 拒绝不登记预览基线;
    搜索目标集同受约束; 谓词异常 fail-open。176 实装 referral_mode 依赖
    这些语义, 改动前先看交接账本 docs/HANDOFF_176_MESSENGER.md。
    """

    def test_gate_rejects_skips_and_keeps_baseline_unset(self, tmp_db):
        from src.host.fb_store import messenger_row_preview_changed
        fb = self._make_fb()
        opened = []
        convs = [{"name": "Alice", "unread": True, "preview_fp": "fpA"}]
        stats = self._run(fb, convs, {}, opened, open_gate=lambda c: False)
        assert opened == []                      # 门开了但谓词拒 → 不打开
        assert stats["gate_skipped"] == 1
        # 拒绝不登记基线: 若登记过 fpA, 换个 fp 会判 True; 首见应为 False
        assert messenger_row_preview_changed("devA", "Alice", "fpB") is False

    def test_gate_allowlist_filters_conversations(self, tmp_db):
        fb = self._make_fb()
        opened = []
        convs = [
            {"name": "Alice", "unread": True, "preview_fp": "fpA"},
            {"name": "Bob", "unread": True, "preview_fp": "fpB"},
        ]
        stats = self._run(fb, convs, {}, opened,
                          open_gate=lambda c: c.get("name") == "Alice")
        assert opened == ["Alice"]               # Bob 被谓词拦
        assert stats["gate_skipped"] == 1

    def test_gate_filters_search_targets(self, tmp_db):
        fb = self._make_fb()
        opened = []
        convs = [{"name": "Alice", "unread": True, "preview_fp": "fpA"}]
        # Ghost 在通知但不在列表 → 本会走搜索; 谓词只放行 Alice → Ghost 不搜
        stats = self._run(fb, convs, {"Alice": "k1", "Ghost": "k2"}, opened,
                          open_gate=lambda c: c.get("name") == "Alice")
        assert "Ghost" not in opened
        assert stats["search_opened"] == 0
        assert stats["gate_skipped"] >= 1        # Ghost 在搜索目标集被拦

    def test_gate_exception_fails_open(self, tmp_db):
        fb = self._make_fb()
        opened = []
        convs = [{"name": "Alice", "unread": True, "preview_fp": "fpA"}]

        def _boom(c):
            raise RuntimeError("gate bug")
        stats = self._run(fb, convs, {}, opened, open_gate=_boom)
        assert opened == ["Alice"]               # 谓词坏了不阻断收件箱
        assert stats["gate_skipped"] == 0

    def test_gate_none_zero_change(self, tmp_db):
        fb = self._make_fb()
        opened = []
        convs = [{"name": "Alice", "unread": True, "preview_fp": "fpA"}]
        stats = self._run(fb, convs, {}, opened, open_gate=None)
        assert opened == ["Alice"]
        assert stats["gate_skipped"] == 0


class TestEnteredVerify:
    """_open_and_read_conversation 的 entered / verify_name 参数 (P2)."""

    def _make_fb(self):
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)
        fb.hb = MagicMock()
        return fb

    def _patch_common(self, fb, monkeypatch, header, incoming):
        monkeypatch.setattr(fb, "_detect_risk_dialog", lambda d: (False, ""))
        monkeypatch.setattr(fb, "_read_thread_title", lambda d: header)
        monkeypatch.setattr(fb, "_extract_latest_incoming_message",
                            lambda d: incoming)
        monkeypatch.setattr("src.app_automation.facebook.time.sleep",
                            lambda *a: None)
        monkeypatch.setattr("src.app_automation.facebook.random.uniform",
                            lambda *a: 0.0)

    def test_entered_skips_tap(self, tmp_db, monkeypatch):
        fb = self._make_fb()
        d = MagicMock()
        self._patch_common(fb, monkeypatch, "山田花子", "你好呀")
        detail = fb._open_and_read_conversation(
            d, {"name": "山田花子", "bounds": None}, "devA",
            entered=True, verify_name="山田花子")
        assert detail["peer_name"] == "山田花子"
        assert detail["incoming_text"] == "你好呀"
        fb.hb.tap.assert_not_called()   # entered → 不 tap
        d.click.assert_not_called()

    def test_verify_mismatch_aborts_no_write(self, tmp_db, monkeypatch):
        fb = self._make_fb()
        d = MagicMock()
        # 搜「柳原慧」但进去 header 是「萧雅云」(搜索误点)
        self._patch_common(fb, monkeypatch, "萧雅云", "你好")
        writes = []
        import src.host.fb_store as store
        monkeypatch.setattr(store, "record_inbox_message",
                            lambda *a, **k: writes.append(1) or 1)
        detail = fb._open_and_read_conversation(
            d, {"name": "柳原慧", "bounds": None}, "devA",
            entered=True, verify_name="柳原慧")
        assert detail["search_mismatch"] is True
        assert "incoming_text" not in detail
        assert writes == []             # 误点绝不入库

    def test_verify_match_reads_and_writes(self, tmp_db, monkeypatch):
        fb = self._make_fb()
        d = MagicMock()
        self._patch_common(fb, monkeypatch, "柳原慧", "在吗")
        writes = []
        import src.host.fb_store as store
        monkeypatch.setattr(store, "record_inbox_message",
                            lambda did, peer, **k: writes.append(peer) or 1)
        detail = fb._open_and_read_conversation(
            d, {"name": "柳原慧", "bounds": None}, "devA",
            entered=True, verify_name="柳原慧")
        assert detail.get("incoming_text") == "在吗"
        assert "search_mismatch" not in detail
        assert writes == ["柳原慧"]

    def test_verify_no_header_aborts(self, tmp_db, monkeypatch):
        """搜索进入但读不到标题 → 保守放弃 (状态异常, 不冒险入库)."""
        fb = self._make_fb()
        d = MagicMock()
        self._patch_common(fb, monkeypatch, "", "你好")
        detail = fb._open_and_read_conversation(
            d, {"name": "柳原慧", "bounds": None}, "devA",
            entered=True, verify_name="柳原慧")
        assert detail["search_mismatch"] is True
