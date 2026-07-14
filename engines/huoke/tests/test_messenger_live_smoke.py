# -*- coding: utf-8 -*-
"""P11 `scripts/messenger_live_smoke.py` 的 meta 测试。

live smoke 需要真机才能完整跑, 本测试只覆盖:
  * CLI --list / --cleanup 模式
  * 每个 step 函数在无真机时的 graceful FAIL
  * LiveStep dataclass 语义
  * cleanup 的 DB 行为

真机端到端在 `docs/MESSENGER_WORKFLOW_GUIDE.md` 描述的 real-device smoke
覆盖, 本测试只保护 runner 本身不回归。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# P2-⑫: spawn 子 Python 进程时强制 UTF-8 防 Windows cp936 emoji 解码挂.
_UTF8_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "messenger_live_smoke.py"


# ─── CLI modes ───────────────────────────────────────────────────────────────

class TestCliList:
    def test_list_flag_lists_steps(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--list", "--no-color"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=_UTF8_ENV, timeout=30,
        )
        assert r.returncode == 0
        # 所有 step key 应该在输出里
        for key in ("adb", "init", "conversations", "friend_requests",
                    "inbox", "funnel", "extended_funnel"):
            assert key in r.stdout

    def test_no_args_prints_error(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--no-color"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=_UTF8_ENV, timeout=30,
        )
        # argparse error → exit 2
        assert r.returncode == 2
        # 错误消息提示 --device 必需
        err = (r.stderr or "") + (r.stdout or "")
        assert "--device" in err

    def test_bad_step_key_rejected(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--device", "fake",
             "--step", "bogus_step_xyz", "--no-color"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=_UTF8_ENV, timeout=30,
        )
        assert r.returncode == 2


# ─── LiveStep dataclass ──────────────────────────────────────────────────────

class TestLiveStep:
    def test_initial_status_not_run(self):
        from scripts.messenger_live_smoke import LiveStep
        s = LiveStep("x")
        assert s.status == "NOT_RUN"
        assert s.elapsed_ms == 0

    def test_render_contains_status_and_data(self):
        from scripts.messenger_live_smoke import LiveStep
        s = LiveStep("my_step", status="PASS", elapsed_ms=123,
                     data={"found": 5, "first_names": ["A", "B"]})
        text = s.render()
        assert "my_step" in text
        assert "PASS" in text
        assert "123ms" in text
        assert "found=5" in text


# ─── Step 函数 graceful 行为 (无真机) ───────────────────────────────────────

class TestStepsWithoutDevice:
    def test_adb_missing_device_returns_fail_or_skip(self):
        from scripts.messenger_live_smoke import step_device_reachable
        r = step_device_reachable("nonexistent-device-xyz")
        # adb 要么 FAIL (设备找不到), 要么 SKIP (adb 没装)
        assert r.status in ("FAIL", "SKIP")
        assert r.name == "device_reachable"

    def test_fb_automation_init_may_fail_without_device_manager(self):
        """没真正设备时, FacebookAutomation 构造可能失败(或 graceful 创建)。
        关键是不崩 → 返 LiveStep。"""
        from scripts.messenger_live_smoke import step_fb_automation_init
        r = step_fb_automation_init("nonexistent-device")
        # 不关心 PASS/FAIL, 确认返 LiveStep 不抛
        assert r.name == "fb_automation_init"
        assert r.status in ("PASS", "FAIL", "SKIP")

    def test_funnel_metrics_snapshot_uses_real_db(self, tmp_db):
        """funnel 是纯 DB 读, 不需要真机, 应 PASS。"""
        from scripts.messenger_live_smoke import step_funnel_metrics_snapshot
        r = step_funnel_metrics_snapshot("anydevice")
        assert r.status == "PASS"
        # 空 DB 时 stage_* 值应为 0
        for k, v in r.data.items():
            assert isinstance(v, (int, float))

    def test_extended_funnel_uses_real_db(self, tmp_db):
        from scripts.messenger_live_smoke import step_extended_funnel
        r = step_extended_funnel("anydevice")
        assert r.status == "PASS"
        # intent_health 空数据时应是 no_data
        assert r.data.get("intent_health") in ("no_data", "healthy",
                                                "needs_rules", "degraded")


# ─── Cleanup ─────────────────────────────────────────────────────────────────

class TestCleanup:
    def test_cleanup_deletes_only_smoke_preset_rows(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from scripts.messenger_live_smoke import cleanup_smoke_rows, PRESET_KEY
        # 写 2 条 smoke + 1 条非 smoke
        record_inbox_message("d1", "A", direction="incoming",
                             message_text="x", preset_key=PRESET_KEY)
        record_inbox_message("d1", "B", direction="incoming",
                             message_text="y", preset_key=PRESET_KEY)
        record_inbox_message("d1", "C", direction="incoming",
                             message_text="z", preset_key="production_preset")
        n = cleanup_smoke_rows()
        assert n == 2
        # 验 production_preset 的行还在
        from src.host.database import _connect
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM facebook_inbox_messages"
                " WHERE preset_key=?", ("production_preset",),
            ).fetchone()
        assert row[0] == 1

    def test_cleanup_empty_returns_zero(self, tmp_db):
        from scripts.messenger_live_smoke import cleanup_smoke_rows
        assert cleanup_smoke_rows() == 0


# ─── CLI cleanup flag ────────────────────────────────────────────────────────

class TestCliCleanup:
    def test_cleanup_flag_runs_without_device(self):
        """--cleanup 不需要 --device。"""
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--cleanup", "--no-color"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=_UTF8_ENV, timeout=30,
        )
        assert r.returncode == 0
        # 日志应该有"删除"提示
        combined = (r.stdout or "") + (r.stderr or "")
        assert "preset_key" in combined or "smoke" in combined.lower() \
            or "删除" in combined
