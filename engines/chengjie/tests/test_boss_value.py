# -*- coding: utf-8 -*-
"""WP-3 老板日报门禁（2026-08-17）。

钉住的不变量：
1. **日窗/周窗口径一致**（规格硬验收）：7 个连续滚动日窗平铺 == 周窗——
   同一套窗口 SQL 的守恒关系，日窗单独实现一套 SQL 时此测必红；
2. 日账聚合正确（近 24h vs 前 24h 环比切窗）；
3. 省时换算：系数解析（默认 3 / 夹界 / 自定义）× drafts.sent（刻意不用
   messages_out——出站总量含人工消息，不该记 AI 的功）；
4. 路由：boss-value 形状 + 300s 缓存 + store 缺席诚实 available:false；
   导出 Markdown 含关键数字表 + attachment 头；
5. **零工程黑话**：bp_* 词条不得出现 草稿/拟稿/autosend/L2/draft（规格明令）；
6. 模板×pack 契约：boss.html 全部静态键 zh/en 齐备。

种子数据与 test_value_report 同风格（直插 reply_drafts/outreach_log/messages）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.inbox.store import InboxStore
from src.ops.value_report import (
    build_daily_value,
    build_weekly_value,
    daily_series,
    estimate_saved_minutes,
    resolve_minutes_per_reply,
)

ENGINE_ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0
DAY = 86400.0


def _mk_store(tmp_path) -> InboxStore:
    return InboxStore(tmp_path / "inbox.db")


def _seed(store: InboxStore, t0: float = NOW) -> None:
    """近 7 天多窗分布的种子（锚 ``t0``）：今天 2 发 1 触达；昨天 1 发；
    第 3/6 天各有流量。纯函数测试锚固定 NOW（显式传 now=NOW，零墙钟依赖）；
    路由测试锚 time.time()（路由内部用真实墙钟建窗）。"""
    with store._lock:
        c = store._conn
        drafts = [
            # (id, conv, created, status, level, decided, sent)
            ("d1", "conv:a", t0 - 0.3 * DAY, "approved", "L1", t0 - 0.2 * DAY, t0 - 0.2 * DAY),
            ("d2", "conv:a", t0 - 0.6 * DAY, "approved", "L2", t0 - 0.5 * DAY, t0 - 0.5 * DAY),
            ("d3", "conv:b", t0 - 1.4 * DAY, "approved", "L2", t0 - 1.3 * DAY, t0 - 1.3 * DAY),
            ("d4", "conv:b", t0 - 2.5 * DAY, "pending", "L1", 0, 0),
            ("d5", "conv:a", t0 - 5.5 * DAY, "approved", "L1", t0 - 5.4 * DAY, t0 - 5.4 * DAY),
            # 上周（周窗之外的对照）
            ("d6", "conv:b", t0 - 8.5 * DAY, "approved", "L1", t0 - 8.4 * DAY, t0 - 8.4 * DAY),
        ]
        for did, conv, created, status, lvl, decided, sent in drafts:
            c.execute(
                "INSERT INTO reply_drafts (draft_id, conversation_id, source_kind, "
                "source_id, status, autopilot_level, decided_at, sent_at, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (did, conv, "inbox", f"src-{did}", status, lvl, decided, sent, created, created))
        outreach = [(t0 - 0.4 * DAY,), (t0 - 3.2 * DAY,), (t0 - 9 * DAY,)]
        for (ts,) in outreach:
            c.execute("INSERT INTO outreach_log (conversation_id, batch_id, status, ts) "
                      "VALUES ('conv:a', 'care:x', 'sent', ?)", (ts,))
        msgs = [
            ("m1", "conv:a", "in", t0 - 0.1 * DAY),
            ("m2", "conv:a", "out", t0 - 0.2 * DAY),
            ("m3", "conv:b", "out", t0 - 1.2 * DAY),
            ("m4", "conv:b", "in", t0 - 2.8 * DAY),
            ("m5", "conv:a", "out", t0 - 6.9 * DAY),
            ("m6", "conv:a", "out", t0 - 8 * DAY),
        ]
        for mid, conv, direction, ts in msgs:
            c.execute(
                "INSERT INTO messages (message_id, conversation_id, direction, ts, ingested_at) "
                "VALUES (?,?,?,?,?)", (mid, conv, direction, ts, ts))
        c.commit()


# ── 1/2. 日窗口径 ───────────────────────────────────────────────────────────

def test_daily_value_windows(tmp_path):
    store = _mk_store(tmp_path)
    _seed(store)
    v = build_daily_value(store, now=NOW)
    td, yd = v["today"], v["yesterday"]
    assert td["drafts"]["created"] == 2 and td["drafts"]["sent"] == 2
    assert yd["drafts"]["created"] == 1 and yd["drafts"]["sent"] == 1
    assert td["outreach"]["sent"] == 1
    assert td["traffic"]["messages_out"] == 1
    assert td["traffic"]["messages_in"] == 1
    assert v["text_lines"]          # 摘要行存在（措辞复用周版，消费面=推送/导出）


def test_daily_series_tiles_weekly_window(tmp_path):
    """守恒不变量：sum(7 个滚动日窗) == 周窗（同一套 SQL 的直接推论）。"""
    store = _mk_store(tmp_path)
    _seed(store)
    series = daily_series(store, days=7, now=NOW)
    assert len(series) == 7
    weekly = build_weekly_value(store, now=NOW)["this_week"]
    assert sum(s["drafts_created"] for s in series) == weekly["drafts"]["created"]
    assert sum(s["drafts_sent"] for s in series) == weekly["drafts"]["sent"]
    assert sum(s["outreach_sent"] for s in series) == weekly["outreach"]["sent"]
    assert sum(s["messages_out"] for s in series) == weekly["traffic"]["messages_out"]
    assert sum(s["messages_in"] for s in series) == weekly["traffic"]["messages_in"]
    # 序列旧→新且窗口相接
    for a, b in zip(series, series[1:]):
        assert abs(a["hi"] - b["lo"]) < 1e-6


def test_daily_value_empty_and_bad_store(tmp_path):
    assert build_daily_value(_mk_store(tmp_path), now=NOW)["today"]["drafts"]["created"] == 0
    assert build_daily_value(object()) == {}          # 坏对象软失败
    assert daily_series(object()) == []


# ── 3. 省时换算 ──────────────────────────────────────────────────────────────

def test_minutes_per_reply_resolution():
    assert resolve_minutes_per_reply({}) == 3.0
    assert resolve_minutes_per_reply(
        {"ops": {"value_report": {"manual_minutes_per_reply": 5}}}) == 5.0
    assert resolve_minutes_per_reply(
        {"ops": {"value_report": {"manual_minutes_per_reply": 0}}}) == 0.5   # 夹下界
    assert resolve_minutes_per_reply(
        {"ops": {"value_report": {"manual_minutes_per_reply": 999}}}) == 60.0
    assert resolve_minutes_per_reply({"ops": {"value_report": {
        "manual_minutes_per_reply": "junk"}}}) == 3.0


def test_estimate_saved_minutes_uses_drafts_sent_only():
    sections = {"drafts": {"sent": 10, "created": 99},
                "traffic": {"messages_out": 500}}
    assert estimate_saved_minutes(sections, 3.0) == 30.0
    assert estimate_saved_minutes({}, 3.0) == 0.0


# ── 4. 路由 ──────────────────────────────────────────────────────────────────

def _build_app(store):
    from fastapi import FastAPI

    from src.web.routes.boss_routes import register_boss_routes

    class _CM:
        config = {"ops": {"value_report": {"manual_minutes_per_reply": 4}}}

    app = FastAPI()
    app.state.inbox_store = store
    register_boss_routes(app, page_auth=lambda: None, api_auth=lambda: None,
                         templates=None, config_manager=_CM())
    return app


@pytest.mark.asyncio
async def test_boss_value_route_shape_and_cache(tmp_path):
    import time as _time

    from httpx import ASGITransport, AsyncClient

    store = _mk_store(tmp_path)
    _seed(store, t0=_time.time())          # 路由用真实墙钟建窗 → 种子同锚
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://t") as c:
        r = await c.get("/api/workspace/boss-value")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] and len(d["series"]) == 7
        assert d["saved"]["minutes_per_reply"] == 4.0
        # 「现在」区随载荷（P1-8）：等段必在（真 store）；d4 pending 计入
        assert d["now"]["waiting"]["replies_pending"] >= 1
        # 今天 2 条真发 × 4 分钟
        assert d["saved"]["today_minutes"] == 8.0
        assert d["daily"]["today"]["drafts"]["sent"] == 2
        # 300s 缓存：再打一发拿到同一 generated_at；force=1 重算
        r2 = await c.get("/api/workspace/boss-value")
        assert r2.json()["generated_at"] == d["generated_at"]
        r3 = await c.get("/api/workspace/boss-value?force=1")
        assert r3.json()["generated_at"] >= d["generated_at"]


@pytest.mark.asyncio
async def test_boss_export_markdown(tmp_path):
    import time as _time

    from httpx import ASGITransport, AsyncClient

    store = _mk_store(tmp_path)
    _seed(store, t0=_time.time())
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://t") as c:
        r = await c.get("/api/workspace/boss-export.md")
        assert r.status_code == 200
        assert "attachment" in (r.headers.get("content-disposition") or "")
        md = r.text
        assert "# AI 价值周报" in md
        assert "| AI 发出的回复 |" in md
        assert "省下人工" in md


@pytest.mark.asyncio
async def test_boss_value_route_honest_without_store():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from src.web.routes.boss_routes import register_boss_routes

    app = FastAPI()          # 不挂 inbox_store
    register_boss_routes(app, page_auth=lambda: None, api_auth=lambda: None,
                         templates=None, config_manager=None)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://t") as c:
        d = (await c.get("/api/workspace/boss-value")).json()
        assert d == {"ok": False, "available": False}


@pytest.mark.asyncio
async def test_boss_page_renders_via_full_app(tmp_path, monkeypatch):
    """全链装配（create_app）下 /workspace/boss 真的注册且能渲染——
    首版曾把注册块放错作用域（_unified_inbox_page_auth 定义在后）被 try 静默吞，
    本测试就是那个坑的回归钉。"""
    import yaml as _yaml
    from httpx import ASGITransport, AsyncClient

    monkeypatch.setenv("AITR_CONFIG_PATH", str(tmp_path / "config.yaml"))
    cfg = {
        "domain": "general",
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {"enabled": []},
        "web_admin": {"auth_token": "test-token", "secret_key": "test-secret"},
    }
    (tmp_path / "config.yaml").write_text(
        _yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text(_yaml.dump({"greeting": ["hi"]}),
                                             encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text(_yaml.dump({"channels": {}}),
                                                  encoding="utf-8")
    from src.utils.config_manager import ConfigManager

    cm = ConfigManager(str(tmp_path / "config.yaml"))
    await cm.load()
    from src.web.admin import create_app

    app = create_app(cm)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://t") as c:
        await c.post("/login", data={"auth_token": "test-token"})
        r = await c.get("/workspace/boss")
        assert r.status_code == 200, r.status_code
        assert "bp-hero" in r.text and "bpExport" in r.text


# ── 7. 「现在」区（P1-8 2026-08-29：钱/单/等/险） ────────────────────────────


def test_build_now_sections(tmp_path):
    """goals 单例热 → 单+钱段；pending 回复 + 24h 未回 → 等段；险段键在。
    段与段互不拖累（peek 纪律：绝不新建 goals 库）。"""
    from src.companion.goals.store import get_goal_store, reset_goal_store
    from src.ops.reply_latency import reset_snapshot_cache
    from src.web.routes import boss_routes as br

    store = _mk_store(tmp_path)
    _seed(store)                       # d4 = pending → 等人拍板 1 条
    reset_snapshot_cache()
    reset_goal_store()
    try:
        gs = get_goal_store(":memory:")
        won = gs.create_goal(
            conversation_id="telegram:a1:n1", platform="telegram",
            account_id="a1", chat_key="n1", template="custom",
            deadline_days=14, now=NOW - 7200)
        assert gs.update_goal_fields(
            won["goal_id"], status="done", done_at=NOW - 100,
            result="manual:won", progress=1.0)
        gs.add_event(won["goal_id"], "won_meta", '{"amount": 99}')
        gs.create_goal(
            conversation_id="telegram:a1:n2", platform="telegram",
            account_id="a1", chat_key="n2", template="custom",
            deadline_days=14, now=NOW)
        out = br._build_now(store, {}, None, now=NOW)
        assert out["goals"]["active"] == 1
        assert out["goals"]["won_24h"] == 1
        assert out["goals"]["won_amount_24h"] == 99.0
        assert out["waiting"]["replies_pending"] == 1
        assert out["waiting"]["unanswered_24h"] >= 0
        assert "sessions_down" in out["risk"]
        assert "alert_link" in out["risk"]
    finally:
        reset_goal_store()
        reset_snapshot_cache()


def test_build_now_soft_fail_and_no_fake_sections(tmp_path):
    """goals 单例未热 → 无 goals 段（绝不摆假零）；坏 store → 等段缺失但
    函数不抛（险段仍独立工作）。"""
    from src.companion.goals.store import reset_goal_store
    from src.web.routes import boss_routes as br

    reset_goal_store()
    out = br._build_now(object(), {}, None, now=NOW)
    assert "goals" not in out
    assert "replies_pending" not in (out.get("waiting") or {})
    assert isinstance(out, dict)       # 全段软失败也返回 dict


# ── 5/6. 词条纪律 ────────────────────────────────────────────────────────────

_JARGON = ("草稿", "拟稿", "autosend", "Autosend", "AUTOSEND", "L2", "draft", "Draft")


def test_boss_pack_has_zero_jargon():
    """规格硬验收：/workspace/boss 页面词条零工程黑话。"""
    from src.web.i18n_packs.boss_page import EN, ZH

    offenders = []
    for lang, table in (("ZH", ZH), ("EN", EN)):
        for k, v in table.items():
            for w in _JARGON:
                if w in str(v):
                    offenders.append(f"{lang}:{k} 含 {w!r}")
    assert not offenders, offenders


def test_boss_template_keys_bilingual():
    from src.web.i18n_packs.boss_page import EN, ZH

    tpl = (ENGINE_ROOT / "src" / "web" / "templates" / "boss.html").read_text(
        encoding="utf-8")
    keys = set(re.findall(r"""(?:window\.)?Tf?\(\s*['"](bp_[a-z0-9_]+)['"]""", tpl))
    keys |= set(re.findall(r"""\(i18n or \{\}\)\.get\('(bp_[a-z0-9_]+)'""", tpl))
    assert keys
    assert not sorted(k for k in keys if k not in ZH), "ZH 缺键"
    assert not sorted(k for k in keys if k not in EN), "EN 缺键"
    assert set(ZH) == set(EN)
