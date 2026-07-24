"""出站媒体去重微扰（anti-ban）单测。需 Pillow；缺失则相关用例 skip。"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess

import pytest

from src.integrations.shared.media_dedup import (
    cleanup_temp, dedup_enabled, dedup_metrics_snapshot, perturb_for_send,
)
import src.integrations.shared.media_dedup as _md

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _make_png(path, color=(120, 130, 140)):
    Image.new("RGB", (48, 48), color).save(path, format="PNG")


def _make_jpg(path, color=(120, 130, 140)):
    Image.new("RGB", (48, 48), color).save(path, format="JPEG", quality=92)


# ── 开关 ────────────────────────────────────────────────────────────────

def test_dedup_disabled_by_default():
    assert dedup_enabled({}) is False
    assert dedup_enabled({"outbound_media": {"dedup": {"enabled": False}}}) is False
    assert dedup_enabled({"outbound_media": {"dedup": {"enabled": True}}}) is True


def test_disabled_returns_original(tmp_path):
    p = str(tmp_path / "a.png"); _make_png(p)
    out, is_temp = perturb_for_send(p, "image", {})
    assert out == p and is_temp is False


# ── 微扰：视觉无差、字节唯一 ─────────────────────────────────────────────

def test_png_perturb_changes_hash(tmp_path):
    p = str(tmp_path / "a.png"); _make_png(p)
    cfg = {"outbound_media": {"dedup": {"enabled": True}}}
    out, is_temp = perturb_for_send(p, "image", cfg)
    assert is_temp is True
    assert out != p and os.path.isfile(out)
    assert _md5(out) != _md5(p)                 # 哈希确已变
    # 尺寸/模式不变（视觉等价前提）
    with Image.open(out) as im, Image.open(p) as orig:
        assert im.size == orig.size
    cleanup_temp(out, is_temp)
    assert not os.path.isfile(out)              # 清理生效


def test_jpg_perturb_changes_hash(tmp_path):
    p = str(tmp_path / "a.jpg"); _make_jpg(p)
    cfg = {"outbound_media": {"dedup": {"enabled": True}}}
    out, is_temp = perturb_for_send(p, "image", cfg)
    assert is_temp is True
    assert _md5(out) != _md5(p)
    cleanup_temp(out, is_temp)


def test_two_sends_differ(tmp_path):
    """同一张图两次发送 → 两个不同哈希（这正是 anti-ban 的目的）。"""
    p = str(tmp_path / "a.png"); _make_png(p)
    cfg = {"outbound_media": {"dedup": {"enabled": True}}}
    o1, t1 = perturb_for_send(p, "image", cfg)
    o2, t2 = perturb_for_send(p, "image", cfg)
    assert _md5(o1) != _md5(o2)
    cleanup_temp(o1, t1); cleanup_temp(o2, t2)


# ── 范围：非图片 / 缺文件 跳过 ───────────────────────────────────────────

def test_voice_skipped(tmp_path):
    p = str(tmp_path / "v.ogg"); open(p, "wb").write(b"fakeogg")
    cfg = {"outbound_media": {"dedup": {"enabled": True}}}
    out, is_temp = perturb_for_send(p, "voice", cfg)
    assert out == p and is_temp is False


def test_video_perturb_changes_hash(tmp_path):
    """视频走 ffmpeg 流复制去重（需 ffmpeg；缺失或造不出真视频则 skip）。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg 不在 PATH")
    src = str(tmp_path / "c.mp4")
    # 用 ffmpeg 生成一个 1s 纯色测试视频（testsrc）→ 真实可流复制的 mp4
    rc = subprocess.run(
        [ffmpeg, "-nostdin", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=1",
         "-pix_fmt", "yuv420p", src],
        capture_output=True, timeout=60).returncode
    if rc != 0 or not os.path.isfile(src) or os.path.getsize(src) == 0:
        pytest.skip("本机 ffmpeg 无法生成测试视频")
    cfg = {"outbound_media": {"dedup": {"enabled": True}}}
    out, is_temp = perturb_for_send(src, "video", cfg)
    assert is_temp is True
    assert out != src and os.path.isfile(out)
    assert _md5(out) != _md5(src)      # 容器字节已变
    cleanup_temp(out, is_temp)
    assert not os.path.isfile(out)


def test_missing_file_returns_original(tmp_path):
    p = str(tmp_path / "nope.png")
    cfg = {"outbound_media": {"dedup": {"enabled": True}}}
    out, is_temp = perturb_for_send(p, "image", cfg)
    assert out == p and is_temp is False


def test_ext_fallback_when_type_missing(tmp_path):
    """media_type 缺失时按扩展名判图片。"""
    p = str(tmp_path / "a.png"); _make_png(p)
    cfg = {"outbound_media": {"dedup": {"enabled": True}}}
    out, is_temp = perturb_for_send(p, "", cfg)
    assert is_temp is True and _md5(out) != _md5(p)
    cleanup_temp(out, is_temp)


def test_cleanup_noop_for_original(tmp_path):
    p = str(tmp_path / "a.png"); _make_png(p)
    cleanup_temp(p, False)         # is_temp=False → 不该删原图
    assert os.path.isfile(p)


# ── 观测计数 ─────────────────────────────────────────────────────────────

def _reset_metrics():
    with _md._METRICS_LOCK:
        _md._METRICS.update(image_perturbed=0, video_perturbed=0, fallback=0,
                            fallback_reasons={}, last_ts=0.0)


def test_metrics_count_image_perturb(tmp_path):
    _reset_metrics()
    p = str(tmp_path / "a.png"); _make_png(p)
    cfg = {"outbound_media": {"dedup": {"enabled": True}}}
    out, is_temp = perturb_for_send(p, "image", cfg)
    cleanup_temp(out, is_temp)
    snap = dedup_metrics_snapshot()
    assert snap["image_perturbed"] == 1
    assert snap["total_perturbed"] == 1
    assert snap["fallback"] == 0


def test_metrics_not_counted_when_disabled(tmp_path):
    """未启用 → 不记账（正常关闭态不算 attempt/fallback）。"""
    _reset_metrics()
    p = str(tmp_path / "a.png"); _make_png(p)
    perturb_for_send(p, "image", {})            # dedup 未开
    perturb_for_send(p, "voice", {"outbound_media": {"dedup": {"enabled": True}}})  # 非范围
    snap = dedup_metrics_snapshot()
    assert snap["total_perturbed"] == 0 and snap["fallback"] == 0


def test_metrics_fallback_reason_recorded(monkeypatch, tmp_path):
    """启用+在范围但微扰失败 → 计 fallback 且记原因（如 Pillow 不可用）。"""
    _reset_metrics()
    p = str(tmp_path / "a.png"); _make_png(p)
    # 强制图片微扰返回失败原因（模拟 no_pillow），验证 fallback 记账
    monkeypatch.setattr(_md, "_perturb_image", lambda s, e, h: (None, "no_pillow"))
    cfg = {"outbound_media": {"dedup": {"enabled": True}}}
    out, is_temp = perturb_for_send(p, "image", cfg)
    assert out == p and is_temp is False        # 回落原图
    snap = dedup_metrics_snapshot()
    assert snap["fallback"] == 1
    assert snap["fallback_reasons"].get("no_pillow") == 1
