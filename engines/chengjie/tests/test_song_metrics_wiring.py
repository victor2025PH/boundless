# -*- coding: utf-8 -*-
"""唱歌观测接线契约（实施58 P1）：SongStats.dump_prom 格式 + drafts_routes 双点并入。

哲学同 bazi/goal stats：零流量=零行（不污染 /metrics）；workspace metrics 段与
Prom 计数同一进程单例——两个消费面绝不各算一套。
"""
from __future__ import annotations

from pathlib import Path

from src.companion.song_stock import SongStats


def test_dump_prom_empty_is_empty():
    assert SongStats().dump_prom() == ""


def test_dump_prom_counters_and_templates():
    s = SongStats()
    s.bump("requests", 3)
    s.bump("no_stock")
    s.note_sent("origin_moon", "chen_meiling")
    out = s.dump_prom()
    assert "singing_requests_total 3" in out
    assert "singing_no_stock_total 1" in out
    assert "singing_sent_total 1" in out
    assert 'singing_sent_by_template_total{template="origin_moon"} 1' in out
    assert out.endswith("\n")


def test_dump_prom_sanitizes_keys():
    s = SongStats()
    s.bump("weird-Key:值")
    s.note_sent("tmpl-月光", "p")
    out = s.dump_prom()
    # 计数键小写化 + 非法字符归 _；模板保大小写但同样消毒
    assert "singing_weird_key___total 1" in out
    assert 'template="tmpl___"' in out


def test_drafts_routes_wiring_pinned():
    src = (Path(__file__).resolve().parent.parent / "src" / "web" / "routes"
           / "drafts_routes.py").read_text(encoding="utf-8", errors="replace")
    assert 'metrics["singing"]' in src, "workspace metrics 并入点缺席"
    assert "get_song_stats().dump_prom()" in src, "Prometheus 并入点缺席"
