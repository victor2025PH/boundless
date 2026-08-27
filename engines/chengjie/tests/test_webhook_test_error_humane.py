# -*- coding: utf-8 -*-
"""告警渠道「测试失败」报错人话契约（B24 根治钉死，2026-08-21）。

事故：用户填错 Chat ID → Telegram 返 400 "chat not found" → 面板却显示
「测试失败：HTTP 200」。三层断链：send_test 不带失败原因（只有计数增量）、
路由原样透传、前端在 200+ok:false 时兜底拼 HTTP 状态码。

本文件钉死修复后的三层契约；改 send_test / 该路由错误映射 / 模板兜底前先读。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


# ── 1. notifier 层：send_test 失败必须带 error_raw ───────────────────────
def test_send_test_carries_error_raw(monkeypatch):
    from src.inbox.webhook_notifier import WebhookNotifier

    n = WebhookNotifier(config=[])

    async def _boom(matcher, etype, data):
        n.total_errors += 1
        n.last_error = "HTTP 400: Bad Request: chat not found"

    monkeypatch.setattr(n, "_send", _boom)
    res = asyncio.run(n.send_test(
        {"format": "telegram", "token": "t", "target": "110",
         "name": "x", "events": ["all"]}))
    assert res["ok"] is False
    assert "chat not found" in res["error_raw"]


def test_send_test_success_has_no_error_raw(monkeypatch):
    from src.inbox.webhook_notifier import WebhookNotifier

    n = WebhookNotifier(config=[])

    async def _ok(matcher, etype, data):
        n.total_sent += 1

    monkeypatch.setattr(n, "_send", _ok)
    res = asyncio.run(n.send_test(
        {"format": "telegram", "token": "t", "target": "1",
         "name": "x", "events": ["all"]}))
    assert res["ok"] is True and "error_raw" not in res


def test_http_post_enriches_telegram_description():
    """_http_post 对 HTTPError 必须读响应体的 description——不读体，
    用户永远只能看到裸状态码。用本地假 HTTP 场景太重，钉源码不变量。"""
    src = (ROOT / "src" / "inbox" / "webhook_notifier.py").read_text(
        encoding="utf-8")
    assert 'get("description")' in src
    assert "HTTPError" in src


# ── 2. 路由层：原始原因 → 人话映射（源码接线钉） ─────────────────────────
def test_route_maps_raw_reasons_to_humane_keys():
    src = (ROOT / "src" / "web" / "routes"
           / "unified_inbox_account_routes.py").read_text(encoding="utf-8")
    for key in ("al.ct.err.chat_not_found", "al.ct.err.bad_token",
                "al.ct.err.blocked", "al.ct.err.network",
                "al.ct.err.unknown"):
        assert key in src, f"路由缺人话映射 {key}"
    assert 'res.pop("error_raw"' in src  # 原始原因不外漏，映射后丢弃


# ── 3. 前端层：200+ok:false 绝不兜底拼 HTTP 状态码 ──────────────────────
def test_template_no_status_code_on_2xx_business_failure():
    src = (ROOT / "src" / "web" / "templates"
           / "_alertlink_connect.html").read_text(encoding="utf-8")
    # 修复后的兜底形态：r.ok（=2xx）时用人话 unknown 键，非 2xx 才显示 HTTP 码
    assert "r.ok?_t('al.ct.err.unknown'):('HTTP '+r.status)" in src


# ── 4. i18n 双语齐备 ─────────────────────────────────────────────────────
def test_humane_error_keys_bilingual():
    from src.web.i18n_packs.alert_link_ops import EN, ZH

    for key in ("al.ct.err.chat_not_found", "al.ct.err.bad_token",
                "al.ct.err.blocked", "al.ct.err.network",
                "al.ct.err.unknown"):
        assert ZH.get(key), key
        assert EN.get(key), key
    # 人话铁律的字面保证：映射文案里不许出现「HTTP」字样
    for key in ("al.ct.err.chat_not_found", "al.ct.err.bad_token",
                "al.ct.err.blocked", "al.ct.err.network",
                "al.ct.err.unknown"):
        assert "HTTP" not in ZH[key].upper()
        assert "HTTP" not in EN[key].upper()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
