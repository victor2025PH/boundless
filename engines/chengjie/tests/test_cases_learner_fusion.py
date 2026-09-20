# -*- coding: utf-8 -*-
"""案例跟进 × 学习队列融合 P1 门禁（2026-08-16）。

钉住的决策（改语义请连同本文件一起改）：
1. **结案喂料桥**：cases.html 结案弹窗带「顺手教 AI」勾选 + 可编辑问题文本，
   结案**成功后**才调既有 ``POST /api/learner/feed``（与驾驶舱 offerFeed 同一
   服务端契约；绝不新造第二个喂料写入口）；桥失败只影响提示不影响结案。
2. **页顶跨系统待办条**：/cases 是简洁模式落地页（后台 ``/`` 简洁模式重定向
   至此），条数据＝``/api/todo-summary`` 单次往返（与仪表盘待办条同源），
   刻意**不新建 /todo 页**——驾驶舱（行动）/仪表盘待办条（看数）已在，
   第四个队列面只会加剧「两个队列分不清」的原始主诉。
3. **定位互跳**：learner.html 页头一句话讲清与案例跟进的分工并互跳。
4. **徽标口径**：todo-summary 的 learner_pending 走 ``stats()`` 全表 COUNT，
   不再用 ``len(list_drafts())``（默认 limit=50 封顶，积压超 50 失真）。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from src.web.web_i18n import get_translations

_TPL = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
_CASES = (_TPL / "cases.html").read_text(encoding="utf-8")
_LEARNER = (_TPL / "learner.html").read_text(encoding="utf-8")
_MON = (
    Path(__file__).resolve().parents[1]
    / "src" / "web" / "routes" / "monitoring_routes.py"
).read_text(encoding="utf-8")


# ── i18n：新键 zh+en 双语齐备 ────────────────────────────────────────────────

_NEW_KEYS = (
    "cases.close.feed", "cases.close.feed_ph", "cases.close.feed_ok",
    "cases.close.feed_queued", "cases.close.feed_dup", "cases.close.feed_fail",
    "cases.todo.title", "cases.todo.learn_tip", "cases.todo.here_hint",
    "lr2_pos_body", "lr2_pos_link",
)


def test_fusion_keys_bilingual():
    zh, en = get_translations("zh"), get_translations("en")
    for k in _NEW_KEYS:
        assert zh.get(k), f"zh 缺键 {k}"
        assert en.get(k), f"en 缺键 {k}"


# ── 结案喂料桥（cases.html 静态接线） ────────────────────────────────────────

def test_close_modal_has_feed_row():
    for frag in ('id="cs-feed-cb"', 'id="cs-feed-q"', 'onchange="csFeedToggle()"'):
        assert frag in _CASES, f"结案弹窗缺喂料桥元素 {frag}"
    # 文本框默认禁用（勾选才启用）+ 200 字上限与服务端契约一致
    assert 'maxlength="200"' in _CASES


def test_feed_bridge_reuses_learner_feed_endpoint():
    """桥必须复用既有 /api/learner/feed（单写入口），不许新造端点。"""
    assert "/api/learner/feed" in _CASES
    # 结案成功（d.resolution_bucket 分支内）才喂；长度守卫与服务端 2 字下限一致
    assert "feedQ.length>=2" in _CASES


def test_feed_helpers_defined_top_level():
    """内联 onchange 引用的函数必须顶层定义（哑按钮门禁的显式前置断言）。"""
    for fn in ("function csFeedToggle(", "function _csFeedReset(",
               "async function _csFeedLearner("):
        assert fn in _CASES, f"cases.html 缺顶层函数 {fn}"


def test_feed_result_keys_are_wired():
    """喂料四态提示全部走 i18n 键（duplicate/generated/queued/fail）。"""
    for key in ("cases.close.feed_ok", "cases.close.feed_queued",
                "cases.close.feed_dup", "cases.close.feed_fail"):
        assert key in _CASES, f"喂料结果提示未接 {key}"


# ── 页顶跨系统待办条 ─────────────────────────────────────────────────────────

def test_cases_page_has_todo_strip():
    for frag in ('id="cs-todo-strip"', 'id="cs-todo-pills"', "/api/todo-summary"):
        assert frag in _CASES, f"cases.html 缺待办条 {frag}"
    # 学习队列入口必须在条里（融合的「教 AI」半边）
    assert "db2_todo_learner" in _CASES
    # 工作台壳页面新开标签（保住后台上下文）
    assert "/workspace/drafts" in _CASES


def test_no_new_todo_page_route():
    """刻意不新建 /todo 页（防第四个队列面）——出现即违反本轮融合决策。"""
    routes_dir = Path(__file__).resolve().parents[1] / "src" / "web" / "routes"
    for f in routes_dir.glob("*.py"):
        src = f.read_text(encoding="utf-8")
        assert '@app.get("/todo"' not in src, (
            f"{f.name} 新建了 /todo 页面路由——融合 P1 的决策是升级既有落地页，"
            "如确需独立页请先更新本门禁与方案文档")


# ── 定位互跳 ────────────────────────────────────────────────────────────────

def test_learner_page_links_back_to_cases():
    assert "lr2_pos_body" in _LEARNER
    assert 'href="/cases"' in _LEARNER


def test_cases_strip_carries_positioning_hint():
    assert "cases.todo.here_hint" in _CASES


# ── 徽标口径：learner_pending 走 stats() 全表 COUNT ──────────────────────────

def test_todo_summary_learner_count_uses_stats():
    assert 'learner.stats()' in _MON.replace('"', "'") or "learner.stats()" in _MON, (
        "todo-summary 的 learner_pending 应走 stats() 全表 COUNT")
    assert 'len(\n                        learner.list_drafts' not in _MON


@pytest.fixture()
def _app_client(tmp_path):
    cfg = {
        "telegram": {"api_id": "111", "api_hash": "abc", "phone_number": "+1"},
        "ai": {"api_key": "test"},
        "skills": {"enabled": []},
        "domain": "general",
        "web_admin": {
            "secret_key": "test-secret-very-long-key-for-testing",
            "auth_token": "test-token-123",
            "session_max_age": 3600,
        },
    }
    (tmp_path / "config.yaml").write_text(
        yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text(
        yaml.dump({"greeting": ["hello"]}, allow_unicode=True), encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text(
        yaml.dump({"channels": {}}, allow_unicode=True), encoding="utf-8")
    (tmp_path / "reply_strategies.yaml").write_text(
        yaml.dump({"strategies": {}, "intent_strategy_map": {}},
                  allow_unicode=True), encoding="utf-8")
    (tmp_path / "snapshots").mkdir(exist_ok=True)

    from starlette.testclient import TestClient

    from src.utils.audit_store import AuditStore
    from src.utils.config_manager import ConfigManager
    from src.web.admin import create_app

    cm = ConfigManager(str(tmp_path / "config.yaml"))
    asyncio.run(cm.load())
    audit = AuditStore(db_path=tmp_path / "audit.db")
    app = create_app(cm, audit_store=audit, boot_ts=0)
    with TestClient(app, raise_server_exceptions=True) as c:
        c.headers.update({"Authorization": "Bearer test-token-123"})
        yield c


def test_todo_summary_learner_pending_not_capped_at_50(_app_client):
    """积压 123 条时待办条必须报 123（旧口径 len(list_drafts) 封顶 50）。"""

    class _FakeLearner:
        ai_ready = True

        def stats(self):
            return {"pending": 123, "approved": 0, "rejected": 0,
                    "dup_flagged": 0}

        def list_drafts(self, status="pending", limit=50, sort="priority"):
            return [{"id": str(i)} for i in range(min(50, 123))]

    _app_client.app.state._daily_learner = _FakeLearner()
    try:
        data = _app_client.get("/api/todo-summary").json()
        assert data["learner_pending"] == 123
    finally:
        _app_client.app.state._daily_learner = None
