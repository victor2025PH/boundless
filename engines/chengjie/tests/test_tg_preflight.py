"""Telegram 直连预检（P1-⑥）门禁：探测语义 + 缓存 + 前后端接线静态轨。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from src.integrations import tg_preflight as pf

_ROOT = Path(__file__).resolve().parents[1]


def _conn(result_map):
    async def connector(host, port, timeout):
        v = result_map.get((host, port), False)
        if isinstance(v, Exception):
            raise v
        return bool(v)
    return connector


def setup_function(_fn):
    pf.reset_cache_for_tests()


def test_any_anchor_reachable_wins():
    probes = [("h1", 443), ("h2", 443)]
    res = asyncio.run(pf.probe_telegram_reachable(
        probes=probes, connector=_conn({("h1", 443): False, ("h2", 443): True})))
    assert res["reachable"] is True
    assert isinstance(res["latency_ms"], int)
    assert res["cached"] is False


def test_all_blocked_reports_unreachable():
    probes = [("h1", 443), ("h2", 443)]
    res = asyncio.run(pf.probe_telegram_reachable(
        probes=probes, connector=_conn({})))
    assert res["reachable"] is False
    assert res["latency_ms"] is None


def test_connector_exception_counts_as_blocked():
    probes = [("h1", 443)]
    res = asyncio.run(pf.probe_telegram_reachable(
        probes=probes, connector=_conn({("h1", 443): OSError("boom")})))
    assert res["reachable"] is False


def test_cache_ttl_and_force():
    probes = [("h1", 443)]
    calls = []

    async def connector(host, port, timeout):
        calls.append(host)
        return True

    r1 = asyncio.run(pf.probe_telegram_reachable(probes=probes, connector=connector))
    r2 = asyncio.run(pf.probe_telegram_reachable(probes=probes, connector=connector))
    assert r1["cached"] is False and r2["cached"] is True
    assert len(calls) == 1, "TTL 窗口内不得重探（弹窗反复开合）"
    r3 = asyncio.run(pf.probe_telegram_reachable(
        probes=probes, connector=connector, force=True))
    assert r3["cached"] is False and len(calls) == 2


def test_default_probes_are_telegram_dcs():
    # 锚点是公开 DC 段常量；改动它属产品决策，先红这里
    assert all(p[1] == 443 for p in pf.DC_PROBES)
    assert any(h.startswith("149.154.") for h, _ in pf.DC_PROBES)


# ── 静态轨：路由与前端接线（声明了就必须有人消费）───────────────────────────

def test_preflight_route_wired():
    src = (_ROOT / "src" / "web" / "routes" / "unified_inbox_login_routes.py").read_text(
        encoding="utf-8")
    assert "/api/platforms/telegram/login/preflight" in src
    assert "probe_telegram_reachable" in src


def test_frontend_banner_wired_and_i18n_present():
    tpl = (_ROOT / "src" / "web" / "templates" / "unified_inbox.html").read_text(
        encoding="utf-8")
    assert "connect-preflight-notice" in tpl, "预检黄条容器缺失"
    assert "_runTgPreflight" in tpl and "_showQrView" in tpl
    assert "login/preflight" in tpl, "前端未调用预检端点"
    # 黄条文案键 zh/en 双语齐备（window.T 无中文兜底，缺键=裸键名直显）
    from src.web.web_i18n import get_translations
    for lang in ("zh", "en"):
        assert str(get_translations(lang).get(
            "inbox.connect.preflight_tg_blocked") or "").strip(), f"缺 {lang} 文案"
