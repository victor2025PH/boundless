# -*- coding: utf-8 -*-
"""报障 outbox 暂存/补传门禁（实施86 域A-2①，#51/#17）。

事故背景：skuio 机 2026-08-29 10:11 点「一键发给客服」报「连不上官网服务」——
重试 + mini 降级都过不去的长断网里，诊断包被直接丢弃。本批加三层：
① 传输 unreachable（含 mini 兜底也失败）→ 全尺寸包暂存 ``logs/diag_outbox``；
② 托管刷新轮 / 下次成功上传时自动补传（拒收=弃件，连不上=停轮）；
③ support 路由对 staged 场景换「已暂存会自动补传」文案，不再让用户干着急。
"""
from __future__ import annotations

import json
import time
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

from src.utils.diag_upload import (
    OUTBOX_MAX_BUNDLES,
    build_and_upload,
    flush_diag_outbox,
    list_staged,
    outbox_dir,
    stage_bundle,
)


class _CM:
    """最小 config_manager 替身：config_path 指向 tmp 实例布局。"""

    def __init__(self, root: Path):
        cfg_dir = root / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = str(cfg_dir / "config.yaml")
        self.config = {}


def test_stage_and_list(tmp_path):
    logs = tmp_path / "logs"
    assert stage_bundle(logs, b"PK-blob", note="页面X | 报错Y", fp="AAAA", ver="1.0.61")
    staged = list_staged(logs)
    assert len(staged) == 1
    side = staged[0].with_suffix(".json")
    meta = json.loads(side.read_text(encoding="utf-8"))
    assert meta["note"] == "页面X | 报错Y"
    assert meta["fp"] == "AAAA"


def test_stage_rotation_caps_count(tmp_path):
    logs = tmp_path / "logs"
    for i in range(OUTBOX_MAX_BUNDLES + 3):
        assert stage_bundle(logs, b"x" * (i + 1))
        # mtime 同秒会让排序不稳，隔开一点
        time.sleep(0.02)
    assert len(list_staged(logs)) == OUTBOX_MAX_BUNDLES


def test_stage_none_logs_dir_is_noop():
    assert stage_bundle(None, b"x") is False


def _fake_urlopen_ok(req, timeout=0):
    class _R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"ok": True, "code": "654321"}).encode()

    return _R()


def test_flush_success_removes_files(tmp_path):
    cm = _CM(tmp_path)
    logs = tmp_path / "logs"
    stage_bundle(logs, b"PK-1", note="n1")
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen_ok):
        out = flush_diag_outbox(cm)
    assert out["sent"] == 1
    assert out["remaining"] == 0
    assert list_staged(logs) == []


def test_flush_rejected_drops_file(tmp_path):
    cm = _CM(tmp_path)
    logs = tmp_path / "logs"
    stage_bundle(logs, b"PK-1")

    def _reject(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 413, "too big", None, None)

    with patch("urllib.request.urlopen", side_effect=_reject):
        out = flush_diag_outbox(cm)
    # 拒收＝包已到达对端被明确拒绝 → 弃件，绝不无限重投
    assert out["dropped"] == 1
    assert list_staged(logs) == []


def test_flush_unreachable_keeps_and_stops(tmp_path):
    cm = _CM(tmp_path)
    logs = tmp_path / "logs"
    stage_bundle(logs, b"PK-1")
    time.sleep(0.02)
    stage_bundle(logs, b"PK-2")

    calls = {"n": 0}

    def _dead(req, timeout=0):
        calls["n"] += 1
        raise OSError("network down")

    with patch("urllib.request.urlopen", side_effect=_dead):
        out = flush_diag_outbox(cm)
    # 第一件连不上就停轮（网络没恢复，逐件硬试只是拖时间），两件全保留
    assert calls["n"] == 1
    assert out["sent"] == 0 and out["dropped"] == 0
    assert out["remaining"] == 2
    assert len(list_staged(logs)) == 2


@pytest.mark.asyncio
async def test_build_and_upload_stages_on_long_outage(tmp_path):
    """全尺寸×2 + mini 都 unreachable → staged:true 且全尺寸包落 outbox。"""
    cm = _CM(tmp_path)

    def _dead(req, timeout=0):
        raise OSError("network down")

    with patch("src.utils.diagnostic_bundle.build_diagnostic_bundle",
               return_value=b"PK-full-bundle"), \
         patch("urllib.request.urlopen", side_effect=_dead):
        out = await build_and_upload(cm, note="工作链失败")
    assert out["ok"] is False
    assert out["error"] == "upstream_unreachable"
    assert out["staged"] is True
    _, logs_dir = (Path(cm.config_path).parent, Path(cm.config_path).parent.parent / "logs")
    staged = list_staged(logs_dir)
    assert len(staged) == 1
    assert staged[0].read_bytes() == b"PK-full-bundle"
    meta = json.loads(staged[0].with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["note"] == "工作链失败"


@pytest.mark.asyncio
async def test_build_and_upload_rejected_not_staged(tmp_path):
    """服务端拒收（连得上）→ 不暂存：重投同一份被拒的包毫无意义。"""
    cm = _CM(tmp_path)

    def _reject(req, timeout=0):
        raise urllib.error.HTTPError(
            getattr(req, "full_url", "x"), 413, "too big", None, None)

    with patch("src.utils.diagnostic_bundle.build_diagnostic_bundle",
               return_value=b"PK-full-bundle"), \
         patch("urllib.request.urlopen", side_effect=_reject):
        out = await build_and_upload(cm, note="")
    assert out["ok"] is False
    assert out["error"].startswith("upload_rejected")
    assert not out.get("staged")
    logs_dir = Path(cm.config_path).parent.parent / "logs"
    assert list_staged(logs_dir) == []


def test_outbox_dir_none_safe():
    assert outbox_dir(None) is None
    assert list_staged(None) == []
