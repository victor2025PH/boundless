# -*- coding: utf-8 -*-
"""AutoSelector health 扫描器单测 (Phase 8, 2026-04-24).

注入 tmp YAML 验证 4 条告警规则:
  HIGH: 导航类 key + fallback_coords 非 null
  MEDIUM: 导航类 key 存在 cache (即使无 coords)
  MEDIUM: best.description 带 'Facebook' 字样 (stale label heuristic)
  LOW: hits>=50, misses=0, learned_at > 30 天
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest


def _write_yaml(tmp_path: Path, name: str, body: str) -> Path:
    f = tmp_path / name
    f.write_text(dedent(body).lstrip(), encoding="utf-8")
    return f


class TestRuleHigh:
    def test_nav_key_with_fallback_coords_is_high(self, tmp_path):
        from src.host.autoselector_health import scan_selector_yaml
        f = _write_yaml(tmp_path, "pkg.yaml", """
            package: com.facebook.katana
            selectors:
              "Search bar or search icon":
                alts: []
                best: {description: Search}
                fallback_coords: [633, 96]
                learned_at: '2026-04-20T00:00:00+00:00'
                screen: home
                stats: {hits: 65, misses: 0, last_hit: '2026-04-23T12:00:00Z'}
        """)
        warnings = scan_selector_yaml(f)
        high = [w for w in warnings if w.severity == "HIGH"]
        assert len(high) == 1
        assert "Search bar" in high[0].key
        assert "633" in high[0].issue or "污染" in high[0].issue


class TestRuleMediumNavKey:
    def test_nav_key_no_coords_is_medium(self, tmp_path):
        from src.host.autoselector_health import scan_selector_yaml
        f = _write_yaml(tmp_path, "pkg.yaml", """
            package: com.facebook.katana
            selectors:
              "Menu button on top":
                alts: []
                best: {description: Menu}
                fallback_coords: null
                learned_at: '2026-04-20T00:00:00+00:00'
                screen: home
                stats: {hits: 5, misses: 0, last_hit: ''}
        """)
        warnings = scan_selector_yaml(f)
        mediums = [w for w in warnings if w.severity == "MEDIUM"]
        # 1 个 MEDIUM (nav_key in cache)
        assert len(mediums) >= 1
        assert any("Menu button" in w.key for w in mediums)


class TestRuleMediumFacebookLabel:
    def test_search_facebook_label_is_medium(self, tmp_path):
        from src.host.autoselector_health import scan_selector_yaml
        f = _write_yaml(tmp_path, "pkg.yaml", """
            package: com.facebook.katana
            selectors:
              "Send button":
                alts: []
                best: {description: "Message Facebook friend"}
                fallback_coords: null
                learned_at: '2026-04-20T00:00:00+00:00'
                screen: ''
                stats: {hits: 1, misses: 0, last_hit: ''}
        """)
        warnings = scan_selector_yaml(f)
        # best.description 含 Facebook + message → MEDIUM rule
        mediums = [w for w in warnings if w.severity == "MEDIUM"]
        assert any("Facebook" in w.issue and "Send button" in w.key for w in mediums)


class TestRuleLowStale:
    def test_high_hits_no_miss_old_learned_at_is_low(self, tmp_path):
        from src.host.autoselector_health import scan_selector_yaml
        # 用 100 天前的 learned_at 满足 LOW 规则 (>=30 天)
        f = _write_yaml(tmp_path, "pkg.yaml", """
            package: com.facebook.katana
            selectors:
              "Like button":
                alts: []
                best: {description: Like}
                fallback_coords: null
                learned_at: '2026-01-01T00:00:00+00:00'
                screen: ''
                stats: {hits: 100, misses: 0, last_hit: ''}
        """)
        warnings = scan_selector_yaml(f)
        lows = [w for w in warnings if w.severity == "LOW"]
        assert any("stale" in w.issue or "未刷新" in w.issue or "未再 vision" in w.issue
                     for w in lows)


class TestClean:
    def test_clean_yaml_no_warnings(self, tmp_path):
        """正常 selector — hits 少 + 最新 + 非导航 — 不应出告警."""
        from src.host.autoselector_health import scan_selector_yaml
        import datetime as dt
        recent = dt.datetime.now(dt.timezone.utc).isoformat()
        f = _write_yaml(tmp_path, "pkg.yaml", f"""
            package: com.facebook.katana
            selectors:
              "Share button":
                alts: []
                best: {{description: Share}}
                fallback_coords: null
                learned_at: '{recent}'
                screen: ''
                stats: {{hits: 5, misses: 1, last_hit: ''}}
        """)
        warnings = scan_selector_yaml(f)
        assert len(warnings) == 0


class TestScanAll:
    def test_scan_all_counts_and_aggregates(self, tmp_path):
        from src.host.autoselector_health import scan_all
        _write_yaml(tmp_path, "a.yaml", """
            package: app.a
            selectors:
              "Search bar":
                alts: []
                best: {description: Search}
                fallback_coords: [100, 200]
                learned_at: '2026-04-20T00:00:00+00:00'
                screen: ''
                stats: {hits: 10, misses: 0, last_hit: ''}
        """)
        _write_yaml(tmp_path, "b.yaml", """
            package: app.b
            selectors:
              "OK button":
                alts: []
                best: {description: OK}
                fallback_coords: null
                learned_at: '2026-04-23T00:00:00+00:00'
                screen: ''
                stats: {hits: 2, misses: 1, last_hit: ''}
        """)
        res = scan_all(tmp_path)
        assert res.scanned_yamls == 2
        assert res.scanned_keys == 2
        assert res.high_count == 1  # Search bar + coords
        assert all(w.package == "app.a" for w in res.warnings
                    if w.severity == "HIGH")


class TestFormatText:
    def test_format_empty(self, tmp_path):
        from src.host.autoselector_health import scan_all, format_text_report
        res = scan_all(tmp_path)
        txt = format_text_report(res)
        assert "扫描 0 个 YAML" in txt
        assert "无告警" in txt

    def test_format_with_warnings(self, tmp_path):
        from src.host.autoselector_health import scan_all, format_text_report
        _write_yaml(tmp_path, "a.yaml", """
            package: app.a
            selectors:
              "Search bar":
                alts: []
                best: {description: Search}
                fallback_coords: [1, 2]
                learned_at: '2026-04-20T00:00:00+00:00'
                screen: ''
                stats: {hits: 10, misses: 0, last_hit: ''}
        """)
        res = scan_all(tmp_path)
        txt = format_text_report(res)
        assert "HIGH: 1" in txt
        assert "Search bar" in txt
