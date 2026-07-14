"""P2-② 失败任务取证 单测.

不调真 adb (那是集成层). 这里覆盖:
- 路径安全 (forensics_path 拒绝穿越)
- 参数脱敏 (_redact_params 屏蔽 token/password/cookie 等)
- list_forensics 空目录 / 含目录 / 含 meta 各种情况
- startup_cleanup 仅清 > N 天的目录
- _maybe_trigger_cleanup 24h 节流 (避免高频 IO)
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from src.host import task_forensics as tf


@pytest.fixture(autouse=True)
def _reset_globals():
    """每个测试隔离: 清自动清理时间戳 + 重置 FORENSICS_ROOT."""
    saved_root = tf.FORENSICS_ROOT
    saved_ts = tf._LAST_CLEANUP_TS
    yield
    tf.FORENSICS_ROOT = saved_root
    tf._LAST_CLEANUP_TS = saved_ts


def test_safe_seg_strips_unsafe_chars():
    assert tf._safe_seg("abc-123") == "abc-123"
    assert tf._safe_seg("abc/def") == "abc_def"
    assert tf._safe_seg("../etc/passwd") == ".._etc_passwd"
    assert tf._safe_seg("normal_id.123") == "normal_id.123"
    # 长度截断
    long_id = "a" * 200
    assert len(tf._safe_seg(long_id)) <= 128


def test_safe_seg_empty_returns_underscore():
    assert tf._safe_seg("") == "_"
    assert tf._safe_seg(None) == "None"  # str(None) → 'None' 后过 sanitize


def test_redact_params_blacklist():
    p = {
        "username": "alice",
        "password": "s3cret",
        "api_key": "sk-xxx",
        "session_token": "abc",
        "cookie_value": "v=1",
        "group_name": "ママ友",
    }
    out = tf._redact_params(p)
    assert out["username"] == "alice"
    assert out["password"] == "<redacted>"
    assert out["api_key"] == "<redacted>"
    assert out["session_token"] == "<redacted>"
    assert out["cookie_value"] == "<redacted>"
    assert out["group_name"] == "ママ友"  # 业务参数保留


def test_redact_params_non_dict():
    assert tf._redact_params(None) == {}
    assert tf._redact_params("not a dict") == {}
    assert tf._redact_params([]) == {}


def test_list_forensics_empty(tmp_path):
    tf.FORENSICS_ROOT = tmp_path / "forensics"
    assert tf.list_forensics("nonexistent_task") == []
    assert tf.list_forensics("") == []


def test_list_forensics_with_snapshots(tmp_path):
    tf.FORENSICS_ROOT = tmp_path / "forensics"
    task_id = "abc-123"
    ts1 = "20260428T100000Z"
    ts2 = "20260428T101500Z"
    for ts in (ts1, ts2):
        d = tf.FORENSICS_ROOT / task_id / ts
        d.mkdir(parents=True)
        (d / "screencap.png").write_bytes(b"\x89PNG fakepng")
        (d / "logcat.txt").write_text("line1\nline2\n", encoding="utf-8")
        (d / "meta.json").write_text(json.dumps({"task_id": task_id, "captured_at_utc": ts}), encoding="utf-8")

    rows = tf.list_forensics(task_id)
    assert len(rows) == 2
    # 按时间倒序: ts2 在前
    assert rows[0]["ts"] == ts2
    assert rows[1]["ts"] == ts1
    # files 含 png + txt + json
    fnames = {f["name"] for f in rows[0]["files"]}
    assert fnames == {"screencap.png", "logcat.txt", "meta.json"}
    # meta 已解析
    assert rows[0]["meta"]["task_id"] == task_id


def test_forensics_path_security_rejects_traversal(tmp_path):
    tf.FORENSICS_ROOT = tmp_path / "forensics"
    task_id = "abc"
    ts = "20260428T100000Z"
    d = tf.FORENSICS_ROOT / task_id / ts
    d.mkdir(parents=True)
    (d / "screencap.png").write_bytes(b"fake")

    # 合法路径
    p = tf.forensics_path(task_id, ts, "screencap.png")
    assert p is not None and p.is_file()

    # 路径穿越尝试
    assert tf.forensics_path("../../../etc", ts, "passwd") is None
    assert tf.forensics_path(task_id, "../etc", "passwd") is None
    assert tf.forensics_path(task_id, ts, "../../../../etc/passwd") is None
    assert tf.forensics_path(task_id, ts, "nonexistent.txt") is None


def test_forensics_path_rejects_empty_segments(tmp_path):
    tf.FORENSICS_ROOT = tmp_path / "forensics"
    assert tf.forensics_path("", "ts", "f.png") is None
    assert tf.forensics_path("t", "", "f.png") is None
    assert tf.forensics_path("t", "ts", "") is None


def test_startup_cleanup_removes_old(tmp_path):
    tf.FORENSICS_ROOT = tmp_path / "forensics"
    old_dir = tf.FORENSICS_ROOT / "old_task" / "ts1"
    fresh_dir = tf.FORENSICS_ROOT / "fresh_task" / "ts1"
    old_dir.mkdir(parents=True)
    fresh_dir.mkdir(parents=True)
    (old_dir / "x.txt").write_text("x")
    (fresh_dir / "x.txt").write_text("x")

    # 把 old_task 整体 mtime 改成 10 天前
    old_mtime = time.time() - 10 * 86400
    os.utime(tf.FORENSICS_ROOT / "old_task", (old_mtime, old_mtime))

    cleaned = tf.startup_cleanup(retention_days=7)
    assert cleaned == 1
    assert not (tf.FORENSICS_ROOT / "old_task").exists()
    assert (tf.FORENSICS_ROOT / "fresh_task").exists()


def test_startup_cleanup_no_root_returns_zero(tmp_path):
    tf.FORENSICS_ROOT = tmp_path / "nonexistent"
    assert tf.startup_cleanup() == 0


def test_capture_forensics_skips_empty_inputs():
    """task_id 或 device_id 为空时跳过 (不开线程不写盘)."""
    assert tf.capture_forensics("", "DEVICE_A") is None
    assert tf.capture_forensics("task1", "") is None
    assert tf.capture_forensics(None, None) is None


def test_maybe_trigger_cleanup_throttle():
    """24h 内只触发一次清理 (避免高频 IO)."""
    tf._LAST_CLEANUP_TS = time.time()  # 刚清过
    # 立即调不触发新线程 (检查 _LAST_CLEANUP_TS 不变)
    before = tf._LAST_CLEANUP_TS
    tf._maybe_trigger_cleanup()
    assert tf._LAST_CLEANUP_TS == before

    # 模拟 25h 前清过 → 应触发
    tf._LAST_CLEANUP_TS = time.time() - 25 * 3600
    tf._maybe_trigger_cleanup()
    assert tf._LAST_CLEANUP_TS > time.time() - 60  # 已更新到 ~now
