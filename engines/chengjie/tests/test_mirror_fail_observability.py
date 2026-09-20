# -*- coding: utf-8 -*-
"""接力记忆 P1-3：出站回写收件箱失败可观测（WARNING 节流 + 计数），2026-09-12。"""
from __future__ import annotations

import logging

from src.integrations import account_orchestrator as ao
from src.integrations import protocol_bridge as pb


def test_emit_incoming_sink_failure_warns_once_per_gap(monkeypatch, caplog):
    calls = []

    def _boom(msg):
        calls.append(msg)
        raise RuntimeError("db locked")

    monkeypatch.setattr(pb, "_sink", _boom)
    monkeypatch.setattr(pb, "_sink_fail_last_warn", 0.0)
    monkeypatch.setattr(pb, "_sink_fail_total", 0)
    base = pb.sink_fail_total()
    with caplog.at_level(logging.DEBUG, logger="src.integrations.protocol_bridge"):
        pb.emit_incoming({"platform": "whatsapp", "account_id": "a", "direction": "out",
                          "media_type": "image"})
        pb.emit_incoming({"platform": "whatsapp", "account_id": "a", "direction": "out"})
    assert len(calls) == 2 and pb.sink_fail_total() == base + 2
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1                                   # 第二次被节流成 debug
    assert "dir=out" in warns[0].getMessage() and "media=image" in warns[0].getMessage()
    # sink 未注册 → 静默
    monkeypatch.setattr(pb, "_sink", None)
    pb.emit_incoming({"platform": "x"})
    assert pb.sink_fail_total() == base + 2


def test_orchestrator_mirror_fail_warn_throttled_per_platform(monkeypatch, caplog):
    monkeypatch.setattr(ao, "_mirror_fail_last_warn", {})
    monkeypatch.setattr(ao, "_mirror_fail_total", 0)
    with caplog.at_level(logging.DEBUG, logger="src.integrations.account_orchestrator"):
        ao._warn_mirror_fail("whatsapp", "a", "c1", kind="image")
        ao._warn_mirror_fail("whatsapp", "a", "c2", kind="text")
        ao._warn_mirror_fail("telegram", "b", "c3", kind="text")
    assert ao.mirror_fail_total() == 3
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 2
    assert any("whatsapp:a" in w and "kind=image" in w for w in warns)
    assert any("telegram:b" in w for w in warns)


def test_orchestrator_send_paths_use_warn_helper():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "integrations"
           / "account_orchestrator.py").read_text(encoding="utf-8", errors="ignore")
    assert src.count("_warn_mirror_fail(") >= 3          # 定义 + 文本 + 媒体两处调用
    assert 'logger.debug("[orchestrator] 出站回写收件箱失败"' not in src
    assert 'logger.debug("[orchestrator] 出站媒体回写收件箱失败"' not in src
