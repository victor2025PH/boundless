# -*- coding: utf-8 -*-
"""自动化覆盖率卡 + 停泊草稿回收 门禁（P1 2026-08-09，.198/.104 事故第三批）。

守四层：
1. 聚合核心（automation_coverage.collect_automation_coverage）：按账号聚合的
   档位分布 / 全局默认回落计数 / 接管态计数 / 账号级封顶 → 有效全自动清零 /
   草稿稿龄分桶——口径必须与护栏同源（compute_mode_caps / takeover source）。
2. 路由 /api/admin/automation-coverage：applicable 语义（零会话/无持久层隐藏卡）。
3. ops 卡三件套：section / loader / 注册表 缺一＝静默缺陷（与 value 卡同款钉法）。
4. 停泊草稿回收（watchdog._check_stale_enriching）：enriching 中途重启的卡死行
   超时翻 pending；新鲜 enriching 不动；默认开、可配置关。
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from src.inbox.automation_coverage import collect_automation_coverage
from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore
from src.inbox.takeover_rearm import record_agent_takeover

ENGINE_ROOT = Path(__file__).resolve().parents[1]


def _conv(store, cid, *, platform="telegram", account="a", chat_key="",
          chat_type="private"):
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform=platform, account_id=account,
        chat_key=chat_key or cid.rsplit(":", 1)[-1], chat_type=chat_type,
        display_name=cid))


# ── 1. 聚合核心 ────────────────────────────────────────────────────────


def test_coverage_aggregates_modes_defaults_and_takeover(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cfg = {"inbox": {"auto_draft": {"automation_mode": "auto_ai"}}}
    # 账号 a：2 个显式 auto_ai + 1 个从未设档（回落全局默认 auto_ai）+ 1 个接管态
    _conv(store, "telegram:a:1"); store.set_automation_mode("telegram:a:1", "auto_ai", source="human")
    _conv(store, "telegram:a:2"); store.set_automation_mode("telegram:a:2", "auto_ai", source="bootstrap")
    _conv(store, "telegram:a:3")   # 无显式行
    _conv(store, "telegram:a:4")
    store.set_automation_mode("telegram:a:4", "auto_ai", source="human")
    record_agent_takeover(store, "telegram:a:4")   # → manual + takeover 标
    # 账号 b：1 个显式 review + 1 个群聊 manual（human）
    _conv(store, "telegram:b:5", account="b")
    store.set_automation_mode("telegram:b:5", "review", source="human")
    _conv(store, "telegram:b:6", account="b", chat_type="group")
    store.set_automation_mode("telegram:b:6", "manual", source="human")

    out = collect_automation_coverage(store, cfg)
    t = out["totals"]
    assert out["global_mode"] == "auto_ai"
    assert t["conversations"] == 6 and t["groups"] == 1
    assert t["defaulted"] == 1                      # a:3 按全局默认计入 auto_ai
    assert t["by_mode"]["auto_ai"] == 3             # a:1 a:2 + defaulted a:3
    assert t["by_mode"]["review"] == 1 and t["by_mode"]["manual"] == 2
    assert t["takeover_manual"] == 1                # 只有 a:4；b:6 是显式 human
    by_acct = {(a["platform"], a["account_id"]): a for a in out["accounts"]}
    assert by_acct[("telegram", "a")]["takeover_manual"] == 1
    assert by_acct[("telegram", "b")]["takeover_manual"] == 0
    # 无任何封顶 → 有效全自动 = auto_ai 计数
    assert t["effective_auto"] == 3 and t["capped_auto"] == 0
    store.close()


def test_coverage_platform_cap_zeroes_effective_auto(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    _conv(store, "telegram:a:1"); store.set_automation_mode("telegram:a:1", "auto_ai", source="human")
    _conv(store, "line:c:9", platform="line", account="c", chat_key="9")
    store.set_automation_mode("line:c:9", "auto_ai", source="human")
    # 平台封顶：line → review（telegram 不受影响）
    cfg = {"inbox": {"auto_draft": {"automation_mode": "auto_ai",
                                    "platform_modes": {"line": "review"}}}}
    out = collect_automation_coverage(store, cfg)
    by_acct = {(a["platform"], a["account_id"]): a for a in out["accounts"]}
    tg = by_acct[("telegram", "a")]
    ln = by_acct[("line", "c")]
    assert tg["effective_auto"] == 1 and not tg["caps"]
    assert ln["effective_auto"] == 0                # 封顶 → 有效全自动清零
    assert ln["caps"] and ln["caps"][0]["layer"] == "platform"
    assert out["totals"]["capped_auto"] == 1
    store.close()


def test_coverage_draft_age_buckets(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    _conv(store, "telegram:a:1")
    now = time.time()
    for i, age_h in enumerate((0.5, 5.0, 30.0)):
        did = store.upsert_draft({
            "source_kind": "inbox", "source_id": f"cov_{i}",
            "conversation_id": "telegram:a:1", "platform": "telegram",
            "account_id": "a", "chat_key": "1", "peer_text": "hi",
            "draft_text": "x", "status": "pending",
        })
        with store._lock:
            store._conn.execute(
                "UPDATE reply_drafts SET created_at=? WHERE draft_id=?",
                (now - age_h * 3600, did))
            store._conn.commit()
    out = collect_automation_coverage(store, {}, now=now)
    d = out["drafts"]
    assert d["pending"] == 3
    assert d["by_age"] == {"fresh_2h": 1, "day": 1, "stale": 1}
    assert d["oldest_h"] >= 29.9
    store.close()


def test_coverage_survives_missing_store():
    out = collect_automation_coverage(None, {})
    assert out["ok"] is True
    assert out["totals"]["conversations"] == 0


# ── 2. 路由 ────────────────────────────────────────────────────────────


def _route_client(cfg, store=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.web.routes import ops_overview_routes as R

    app = FastAPI()
    ctx = SimpleNamespace(
        api_auth=lambda request: True,
        api_write=lambda perm: (lambda: True),
        page_auth=lambda request: True,
        templates=None,
        config_manager=SimpleNamespace(config=cfg),
        audit_store=None,
        user_store=None,
        token=None,
        telegram_client=None,
    )
    R.register_ops_overview_routes(app, ctx)
    if store is not None:
        app.state.inbox_store = store
    return TestClient(app, raise_server_exceptions=True)


def test_route_applicable_semantics(tmp_path):
    # 无持久层 → applicable=False（整卡隐藏）
    c = _route_client({})
    d = c.get("/api/admin/automation-coverage").json()
    assert d["ok"] is True and d["applicable"] is False
    # 有会话 → applicable=True + totals 语义
    store = InboxStore(tmp_path / "inbox.db")
    _conv(store, "telegram:a:1")
    store.set_automation_mode("telegram:a:1", "auto_ai", source="human")
    c2 = _route_client({"inbox": {"auto_draft": {"automation_mode": "auto_ai"}}},
                       store=store)
    d2 = c2.get("/api/admin/automation-coverage").json()
    assert d2["applicable"] is True
    assert d2["totals"]["conversations"] == 1
    assert d2["accounts"][0]["account_id"] == "a"
    store.close()


# ── 3. ops 卡三件套（section / loader / 注册表 / i18n 双语）───────────


def test_autocov_card_renders_and_registered():
    src = (ENGINE_ROOT / "src" / "web" / "templates"
           / "ops_overview.html").read_text(encoding="utf-8")
    assert 'id="autoCovSection"' in src
    assert "async function loadAutoCoverage()" in src
    assert "/api/admin/automation-coverage" in src
    assert "anchor:'autoCoverageKpis'" in src
    # 零流量/不适用整卡隐藏的站内惯例
    assert "if(!d.ok || d.applicable === false){ sec.style.display='none'; return; }" in src


def test_autocov_trend_rendering_wired():
    """P3 趋势线三件套：容器 div / loader 消费 d.trend / ≥2 天才画（sparkline 契约）。"""
    src = (ENGINE_ROOT / "src" / "web" / "templates"
           / "ops_overview.html").read_text(encoding="utf-8")
    assert 'id="autoCovTrend"' in src
    assert "Array.isArray(d.trend)" in src
    assert "series.length >= 2" in src


def test_fleet_health_card_renders_and_registered():
    """P3「账号健康分」老板卡三件套：section / loader（薄消费既有 fleet-health）/
    注册表。缺一＝静默缺陷（与 value/autocov 卡同款钉法）。"""
    src = (ENGINE_ROOT / "src" / "web" / "templates"
           / "ops_overview.html").read_text(encoding="utf-8")
    assert 'id="fleetHealthSection"' in src
    assert "async function loadFleetHealth()" in src
    assert "/api/accounts/fleet-health" in src
    assert "anchor:'fleetHealthKpis'" in src
    # 零账号整卡隐藏惯例
    assert "if(!d.ok || !Number(fl.total)){ sec.style.display='none'; return; }" in src


def test_fleet_health_i18n_keys_bilingual():
    from src.web.i18n_packs.ops_fleet_health_card import EN, ZH

    keys = ("ov2_s_fleethealth", "ov2_fh_sub", "ov2_fh_total", "ov2_fh_light",
            "ov2_fh_avg", "ov2_fh_red", "ov2_fh_warming", "ov2_fh_risk",
            "ov2_fh_col_account", "ov2_fh_col_reasons", "ov2_fh_worst_title",
            "ov2_fh_lifecycle", "ov2_fh_churn", "ov2_fh_all_green")
    for k in keys:
        assert ZH.get(k), k
        assert EN.get(k), k


def test_autocov_i18n_keys_bilingual():
    from src.web.i18n_packs.ops_automation_coverage import EN, ZH

    keys = ("ov2_s_autocov", "ov2_ac_sub", "ov2_ac_total", "ov2_ac_eff_auto",
            "ov2_ac_capped", "ov2_ac_takeover", "ov2_ac_pending",
            "ov2_ac_stale", "ov2_ac_col_account", "ov2_ac_col_caps",
            "ov2_ac_drafts_line", "ov2_ac_counters_line",
            "ov2_ac_defaulted_hint")
    for k in keys:
        assert ZH.get(k), k
        assert EN.get(k), k


# ── 4. 停泊草稿卡死回收 ────────────────────────────────────────────────


def _draft_service(store):
    from src.inbox.drafts import DraftService
    return DraftService(inbox_store=store)


def _mk_enriching(store, cid, *, age_min=0.0, source_id="s1"):
    did = store.upsert_draft({
        "source_kind": "inbox", "source_id": source_id,
        "conversation_id": cid, "platform": "telegram", "account_id": "a",
        "chat_key": cid.rsplit(":", 1)[-1], "peer_text": "hello",
        "draft_text": "template placeholder", "status": "enriching",
    })
    if age_min:
        with store._lock:
            store._conn.execute(
                "UPDATE reply_drafts SET created_at=? WHERE draft_id=?",
                (time.time() - age_min * 60, did))
            store._conn.commit()
    return did


def _watchdog(store, svc, cfg=None):
    from src.inbox.health_watchdog import HealthWatchdog
    app = SimpleNamespace(state=SimpleNamespace(
        inbox_store=store, draft_service=svc))
    cm = SimpleNamespace(config=cfg if cfg is not None else {})
    return HealthWatchdog(app=app, config_manager=cm)


def test_stale_enriching_released_to_pending(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    _conv(store, "telegram:a:1")
    svc = _draft_service(store)
    stale = _mk_enriching(store, "telegram:a:1", age_min=30, source_id="old")
    fresh = _mk_enriching(store, "telegram:a:2", age_min=1, source_id="new")
    wd = _watchdog(store, svc)
    wd._check_stale_enriching()
    assert store.get_draft(stale)["status"] == "pending"    # 卡死行被回收
    assert store.get_draft(fresh)["status"] == "enriching"  # 新鲜行不动
    # 回收后的行保留模板占位正文（与产线失败兜底同一条降级路径）
    assert store.get_draft(stale)["draft_text"] == "template placeholder"
    store.close()


def test_stale_enriching_respects_disable_flag(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    _conv(store, "telegram:a:1")
    svc = _draft_service(store)
    stale = _mk_enriching(store, "telegram:a:1", age_min=60)
    wd = _watchdog(store, svc, cfg={
        "health_watchdog": {"stale_enriching_release": {"enabled": False}}})
    wd._check_stale_enriching()
    assert store.get_draft(stale)["status"] == "enriching"
    store.close()


def test_stale_enriching_survives_missing_service():
    wd = _watchdog(None, None)
    wd._app.state.draft_service = None
    wd._check_stale_enriching()   # 不抛即过
