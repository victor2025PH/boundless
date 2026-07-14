"""P2.0 测试: capture_immediate / promote_pending_to_task / cleanup_orphan_pending

不依赖真机 ADB ─ 用 monkeypatch 替换 subprocess.run, 让其返回伪造的截图字节
和 hierarchy XML, 验证文件落盘 + meta.json 字段 + FIFO 滚动 + promote 行为。
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from src.host import task_forensics as tf


# ────────── helpers ──────────

class _FakeProc:
    def __init__(self, returncode=0, stdout=b"", text_stdout=""):
        self.returncode = returncode
        self.stdout = stdout if stdout else text_stdout


@pytest.fixture
def tmp_forensics(tmp_path, monkeypatch):
    """每个测试用临时 FORENSICS_ROOT，避免污染真实 data/forensics"""
    root = tmp_path / "forensics"
    root.mkdir()
    pending = root / "_pending"
    pending.mkdir()
    monkeypatch.setattr(tf, "FORENSICS_ROOT", root)
    monkeypatch.setattr(tf, "PENDING_ROOT", pending)
    return root


def _patch_adb_ok(monkeypatch, png_bytes=b"\x89PNG\r\n\x1a\n" + b"x" * 200,
                   xml_text="<hierarchy>" + "x" * 250 + "</hierarchy>"):
    """monkeypatch subprocess.run 模拟 ADB 命令成功"""
    def fake_run(cmd, **kwargs):
        if "screencap" in cmd:
            return _FakeProc(returncode=0, stdout=png_bytes)
        if "uiautomator" in cmd and "dump" in cmd:
            return _FakeProc(returncode=0, text_stdout="UI hierchary dumped to: ...")
        if "cat" in cmd:
            return _FakeProc(returncode=0, text_stdout=xml_text)
        if "logcat" in cmd:
            return _FakeProc(returncode=0, text_stdout="line1\nline2\n")
        return _FakeProc(returncode=0)
    monkeypatch.setattr(tf.subprocess, "run", fake_run)


# ────────── capture_immediate ──────────

def test_capture_immediate_creates_pending_snapshot(tmp_forensics, monkeypatch):
    _patch_adb_ok(monkeypatch)
    snap = tf.capture_immediate("device-abc", step_name="extract_zero",
                                hint="group=ママ友", reason="0 members")
    assert snap is not None
    assert snap.startswith("extract_zero_")
    snap_dir = tmp_forensics / "_pending" / "device-abc" / snap
    assert snap_dir.is_dir()
    assert (snap_dir / "screencap.png").is_file()
    assert (snap_dir / "hierarchy.xml").is_file()
    meta = json.loads((snap_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["device_id"] == "device-abc"
    assert meta["step"] == "extract_zero"
    assert meta["hint"] == "group=ママ友"
    assert meta["pending"] is True
    assert meta["screencap"]["ok"] is True
    assert meta["hierarchy"]["ok"] is True


def test_capture_immediate_handles_screencap_fail(tmp_forensics, monkeypatch):
    """ADB screencap 返回非 0 时, hierarchy 仍应继续抓且 meta 记录失败原因"""
    def fake_run(cmd, **kwargs):
        if "screencap" in cmd:
            return _FakeProc(returncode=1, stdout=b"")
        if "cat" in cmd:
            return _FakeProc(returncode=0,
                             text_stdout="<hierarchy>" + "x" * 250 + "</hierarchy>")
        return _FakeProc(returncode=0)
    monkeypatch.setattr(tf.subprocess, "run", fake_run)
    snap = tf.capture_immediate("dev1", step_name="test_fail")
    assert snap is not None
    snap_dir = tmp_forensics / "_pending" / "dev1" / snap
    meta = json.loads((snap_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["screencap"]["ok"] is False
    assert meta["hierarchy"]["ok"] is True


def test_capture_immediate_empty_device_id_returns_none(tmp_forensics):
    assert tf.capture_immediate("", step_name="x") is None


def test_capture_immediate_silent_on_exception(tmp_forensics, monkeypatch):
    """ADB 抛异常时 capture_immediate 不应崩, 静默返回 snap_name (空内容)"""
    def fake_run(cmd, **kwargs):
        raise RuntimeError("adb crashed")
    monkeypatch.setattr(tf.subprocess, "run", fake_run)
    snap = tf.capture_immediate("dev2", step_name="x")
    # 仍返回 snap_name (目录已建); 各 ok=False
    assert snap is not None
    meta_path = tmp_forensics / "_pending" / "dev2" / snap / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["screencap"]["ok"] is False
    assert meta["hierarchy"]["ok"] is False


# ────────── FIFO 滚动 ──────────

def test_pending_fifo_rolls_when_exceeds_max(tmp_forensics, monkeypatch):
    _patch_adb_ok(monkeypatch)
    monkeypatch.setattr(tf, "PENDING_PER_DEVICE_MAX", 3)
    # 制造 5 个快照, 间隔 mtime 让排序稳定
    for i in range(5):
        tf.capture_immediate("devX", step_name=f"step_{i}")
        time.sleep(0.02)
    snaps = sorted((tmp_forensics / "_pending" / "devX").iterdir(),
                   key=lambda p: p.stat().st_mtime)
    assert len(snaps) == 3, f"expected 3 after FIFO trim, got {len(snaps)}"
    # 最新 3 个应该是 step_2/3/4
    names = [s.name for s in snaps]
    assert any("step_2" in n for n in names)
    assert any("step_3" in n for n in names)
    assert any("step_4" in n for n in names)
    assert not any("step_0" in n for n in names)
    assert not any("step_1" in n for n in names)


# ────────── promote_pending_to_task ──────────

def test_promote_moves_all_pending_to_task_dir(tmp_forensics, monkeypatch):
    _patch_adb_ok(monkeypatch)
    # 抓 2 个 pending
    s1 = tf.capture_immediate("dev-Y", step_name="enter_failed")
    time.sleep(0.02)
    s2 = tf.capture_immediate("dev-Y", step_name="zero_after_enter")
    # 模拟 task_store 触发: 建立 task 目录并 promote
    task_dir = tmp_forensics / "task-001" / "20260430T080000Z"
    task_dir.mkdir(parents=True)
    moved = tf.promote_pending_to_task("task-001", "dev-Y", task_dir)
    assert moved == 2
    assert (task_dir / s1).is_dir()
    assert (task_dir / s2).is_dir()
    # _pending/dev-Y 应清空
    assert list((tmp_forensics / "_pending" / "dev-Y").iterdir()) == []


def test_promote_returns_zero_when_no_pending(tmp_forensics):
    task_dir = tmp_forensics / "task-002" / "20260430T080001Z"
    task_dir.mkdir(parents=True)
    assert tf.promote_pending_to_task("task-002", "dev-NONE", task_dir) == 0


def test_promote_skips_existing_target(tmp_forensics, monkeypatch):
    """目标目录已存在同名子目录时跳过, 不覆盖"""
    _patch_adb_ok(monkeypatch)
    s1 = tf.capture_immediate("dev-Z", step_name="x")
    task_dir = tmp_forensics / "task-003" / "20260430T080002Z"
    task_dir.mkdir(parents=True)
    # 预先建一个同名空目录
    (task_dir / s1).mkdir()
    moved = tf.promote_pending_to_task("task-003", "dev-Z", task_dir)
    assert moved == 0  # 跳过, pending 仍在
    assert (tmp_forensics / "_pending" / "dev-Z" / s1).is_dir()


# ────────── orphan cleanup ──────────

def test_cleanup_orphan_pending_removes_old(tmp_forensics, monkeypatch):
    _patch_adb_ok(monkeypatch)
    s1 = tf.capture_immediate("dev-O", step_name="old_one")
    # 把 mtime 改成 1 小时前
    snap_path = tmp_forensics / "_pending" / "dev-O" / s1
    old_ts = time.time() - 3600
    import os
    os.utime(snap_path, (old_ts, old_ts))
    # 同时再抓一个新的
    time.sleep(0.02)
    s2 = tf.capture_immediate("dev-O", step_name="fresh_one")
    cleaned = tf.cleanup_orphan_pending()
    assert cleaned == 1
    assert not (tmp_forensics / "_pending" / "dev-O" / s1).exists()
    assert (tmp_forensics / "_pending" / "dev-O" / s2).exists()


def test_startup_cleanup_skips_pending_dir(tmp_forensics, monkeypatch):
    """startup_cleanup 不应误删 _pending/ 目录 (由 cleanup_orphan_pending 管)"""
    _patch_adb_ok(monkeypatch)
    tf.capture_immediate("dev-K", step_name="alive")
    # 把 _pending 目录 mtime 改成 100 天前 (远超 7d retention)
    pending_root = tmp_forensics / "_pending"
    very_old = time.time() - 100 * 86400
    import os
    os.utime(pending_root, (very_old, very_old))
    # 调 startup_cleanup
    tf.startup_cleanup(retention_days=7)
    # _pending 仍存在 (未被误删)
    assert pending_root.is_dir()
    # dev-K 的 alive 快照仍存在 (未达 30min orphan 阈值)
    assert (pending_root / "dev-K").is_dir()
    assert len(list((pending_root / "dev-K").iterdir())) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
