# -*- coding: utf-8 -*-
"""#166 门禁：目标引擎真相（sprint_engine_status）——UI 不再兜售引擎不执行的事。

skuio 88MP86（1.0.73）实况：goals 开、sprint 关、bridge 关、proactive_care dry_run 开
——右栏卡却写「自动推进 · 进行中 · 第 1/1 天 · 89%」（89% 是时间进度）。

钉住：
- ``sprint_engine_status`` 把整条链的每道闸点名（sprint 开关 / 派发终点 care 关闸·
  dry_run / 平台白名单；自然档 bridge / proactive_topic）；
- ``GET /api/goals/engine-status`` goals 关也 200；``/api/goals/templates`` caps 带
  有效性+阻塞点（旧三键保留）；``for-conversation`` 带 ``engine`` + ``beats``；
- ``sprint_live.ticker_on`` 必须是**整条链有效**（sprint 开但 sprint.dry_run → False；
  D1b 2026-09-05 起 care 的 enabled/dry_run 不再是冲刺的闸——冲刺行 live 绕过）；
- ``beats.progress_kind``：custom/未知模板自然档=time（89% 是时间）；与 ledger 分支表同步；
- 启动日志：sprint 关 → WARNING「未启用」，开但被拦 → WARNING「派发被拦」，全通 → ✅。
"""
from __future__ import annotations

import inspect
import re
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from src.companion.goals.service import sprint_engine_status
from src.companion.goals.store import reset_goal_store
from src.web.routes import goal_routes
from src.web.routes.goal_routes import register_goal_routes

CONV = "whatsapp:17345893506:13308422244"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    import src.integrations.protocol_bridge as pb
    import src.utils.companion_context as cc

    monkeypatch.setattr(cc, "_REL_PROVIDERS", {})
    monkeypatch.setattr(pb, "_inbox_store_getter", None)
    reset_goal_store()
    yield
    reset_goal_store()


def _cfg(*, goals=None, care=None, topic=None):
    g = {"enabled": True, "db_path": ":memory:"}
    g.update(goals or {})
    comp = {"goals": g}
    if care is not None:
        comp["proactive_care"] = care
    if topic is not None:
        comp["proactive_topic"] = topic
    return {"companion": comp}


def _client(cfg):
    sess = {"role": "", "user": "tester"}
    app = FastAPI()

    def auth_dep(request: Request) -> None:
        request.scope["session"] = dict(sess)

    register_goal_routes(app, auth_dep, SimpleNamespace(config=cfg, config_path=None))
    return TestClient(app)


# ── 纯函数 ───────────────────────────────────────────────────────────────────

def test_status_skuio_shape_all_blocked():
    """skuio 实况：goals 开 / sprint 关 / care dry_run / bridge 关 → 两档全部无效。
    D1b 起 care dry_run 不再是冲刺的闸（冲刺行 live），但 sprint 关本身仍拦；
    自然档在 sprint 关时回落 bridge 链 → bridge_disabled。"""
    es = sprint_engine_status(_cfg(
        care={"enabled": True, "dry_run": True},
        topic={"enabled": True, "dry_run": False}), platform="whatsapp")
    assert es["enabled"] is True and es["sprint_enabled"] is False
    assert es["sprint_effective"] is False
    # whatsapp 已在 D1b 出厂白名单 → 只剩 sprint_disabled 一道闸
    assert es["sprint_blockers"] == ["sprint_disabled"]
    assert es["care_dry_run"] is True, "care 状态仍回传供看板参考"
    assert es["natural_auto_effective"] is False
    assert es["natural_auto_via"] == "bridge"
    assert es["natural_auto_blockers"] == ["bridge_disabled"]


def test_status_sprint_on_care_dry_run_no_longer_blocks():
    """D1b P0-1：桌面种子 care dry_run=true 上只翻 sprint.enabled 现在**够了**——
    冲刺行按 goal_row_policy.live 绕过 care 灰度门真发；只有 sprint 自己的
    dry_run 才拦。"""
    es = sprint_engine_status(_cfg(
        goals={"sprint": {"enabled": True, "platforms": ["telegram", "whatsapp"]}},
        care={"enabled": True, "dry_run": True}), platform="whatsapp")
    assert es["sprint_enabled"] is True
    assert es["sprint_effective"] is True and es["sprint_blockers"] == []
    es2 = sprint_engine_status(_cfg(
        goals={"sprint": {"enabled": True, "dry_run": True,
                          "platforms": ["whatsapp"]}}), platform="whatsapp")
    assert es2["sprint_dry_run"] is True
    assert es2["sprint_blockers"] == ["sprint_dry_run"]


def test_status_sprint_on_care_disabled_no_longer_blocks():
    """example 默认 proactive_care.enabled=false：cloud_light 只开 sprint 也真发
    （派发器 goal_only 模式只放 live 目标行）。"""
    es = sprint_engine_status(_cfg(goals={"sprint": {"enabled": True}}))
    assert es["care_enabled"] is False
    assert es["sprint_blockers"] == [] and es["sprint_effective"] is True


def test_status_platform_whitelist_default_three_platforms():
    """D1b P0-2：出厂白名单 telegram/whatsapp/line；messenger 刻意不入。"""
    cfg = _cfg(goals={"sprint": {"enabled": True}})
    assert sprint_engine_status(cfg)["sprint_effective"] is True
    for p in ("telegram", "whatsapp", "line"):
        assert sprint_engine_status(cfg, platform=p)["sprint_effective"] is True, p
    es = sprint_engine_status(cfg, platform="messenger")
    assert es["sprint_effective"] is False and es["sprint_blockers"] == ["platform"]
    assert es["sprint_platforms"] == ["telegram", "whatsapp", "line"]
    assert es["sprint_platforms_explicit"] is False
    es2 = sprint_engine_status(
        _cfg(goals={"sprint": {"enabled": True, "platforms": ["telegram"]}}),
        platform="whatsapp")
    assert es2["sprint_platforms_explicit"] is True
    assert es2["sprint_blockers"] == ["platform"]


def test_status_natural_auto_via_daily_when_sprint_on():
    """D1b P0-3：sprint 开 + natural_daily（默认开）→ 自然档走推进器每日拍，
    与冲刺同闸；natural_daily 关 → 回落 bridge 链。"""
    es = sprint_engine_status(_cfg(goals={"sprint": {"enabled": True}}))
    assert es["natural_auto_via"] == "daily"
    assert es["natural_auto_effective"] is True and es["natural_auto_blockers"] == []
    es = sprint_engine_status(
        _cfg(goals={"sprint": {"enabled": True}}), platform="messenger")
    assert es["natural_auto_blockers"] == ["platform"]
    es = sprint_engine_status(_cfg(
        goals={"sprint": {"enabled": True, "natural_daily": False},
               "bridge": {"enabled": True}},
        topic={"enabled": True, "dry_run": False}))
    assert es["natural_auto_via"] == "bridge"
    assert es["natural_auto_effective"] is True


def test_status_natural_auto_bridge_needs_live_topic():
    cfg = _cfg(goals={"bridge": {"enabled": True}},
               topic={"enabled": True, "dry_run": False})
    assert sprint_engine_status(cfg)["natural_auto_effective"] is True
    es = sprint_engine_status(_cfg(goals={"bridge": {"enabled": True}},
                                   topic={"enabled": True, "dry_run": True}))
    assert es["natural_auto_blockers"] == ["proactive_dry_run"]
    es = sprint_engine_status(_cfg(goals={"bridge": {"enabled": True}}))
    assert es["natural_auto_blockers"] == ["proactive_disabled"]


def test_status_goals_disabled_first_blocker_and_never_raises():
    es = sprint_engine_status({"companion": {"goals": {"enabled": False}}})
    assert es["enabled"] is False
    assert es["sprint_blockers"][0] == "goals_disabled"
    assert es["natural_auto_blockers"][0] == "goals_disabled"
    assert sprint_engine_status(None)["sprint_effective"] is False  # type: ignore[arg-type]
    assert sprint_engine_status({"companion": "garbage"})["enabled"] is False


# ── 路由 ─────────────────────────────────────────────────────────────────────

def test_engine_status_route_200_even_when_goals_disabled():
    c = _client({"companion": {"goals": {"enabled": False}}})
    r = c.get("/api/goals/engine-status")
    assert r.status_code == 200
    d = r.json()
    assert d["enabled"] is False and "goals_disabled" in d["sprint_blockers"]


def test_engine_status_route_platform_param():
    c = _client(_cfg(goals={"sprint": {"enabled": True}},
                     care={"enabled": True, "dry_run": False}))
    assert c.get("/api/goals/engine-status").json()["sprint_effective"] is True
    d = c.get("/api/goals/engine-status?platform=messenger").json()
    assert d["sprint_effective"] is False and d["sprint_blockers"] == ["platform"]


def test_templates_caps_keep_legacy_keys_and_add_effectiveness():
    c = _client(_cfg(goals={"sprint": {"enabled": True, "dry_run": True}},
                     care={"enabled": True, "dry_run": False}))
    caps = c.get("/api/goals/templates").json()["caps"]
    for k in ("bridge_enabled", "proactive_enabled", "sprint_enabled"):
        assert k in caps, f"旧前端契约键 {k} 不能丢"
    assert caps["sprint_enabled"] is True
    assert caps["sprint_effective"] is False
    assert caps["sprint_blockers"] == ["sprint_dry_run"]
    assert caps["natural_auto_effective"] is False
    assert isinstance(caps["sprint_platforms"], list)


def test_for_conversation_carries_engine_and_beats_time_kind():
    """自定义目标自然档（skuio 形态）：beats.progress_kind=time、cap=总天数、
    engine 按会话平台判白名单（会话平台 whatsapp，白名单只给 telegram → platform）。"""
    c = _client(_cfg(goals={"sprint": {"enabled": True, "dry_run": True,
                                       "platforms": ["telegram"]}},
                     care={"enabled": True, "dry_run": False}))
    r = c.post("/api/goals", json={
        "template": "custom", "conversation_id": CONV, "autonomy": "auto",
        "deadline_days": 3, "params": {"note": "收口"}})
    assert r.status_code == 200, r.text
    d = c.get(f"/api/goals/for-conversation?conversation_id={CONV}").json()
    g = d["goal"]
    assert g["status"] == "active" and g["autonomy"] == "auto"
    beats = g["beats"]
    assert beats["progress_kind"] == "time"
    assert beats["cap"] == 3 and beats["used"] == 0
    assert 0 <= beats["time_pct"] <= 100
    eng = d["engine"]
    assert eng["platform"] == "whatsapp"
    assert eng["sprint_effective"] is False
    assert "sprint_dry_run" in eng["sprint_blockers"] and "platform" in eng["sprint_blockers"]
    assert eng["natural_auto_effective"] is False


def test_for_conversation_beats_used_counts_consumed_non_none():
    from src.companion.goals.store import get_goal_store
    c = _client(_cfg())
    r = c.post("/api/goals", json={
        "template": "custom", "conversation_id": CONV, "deadline_days": 5})
    gid = r.json()["goal"]["goal_id"]
    store = get_goal_store()
    store.upsert_action(gid, "2026-09-01", intent="a", push_level="soft")
    store.mark_action(store.get_action(gid, "2026-09-01")["action_id"], "consumed")
    store.upsert_action(gid, "2026-09-02", intent="b", push_level="none")
    store.mark_action(store.get_action(gid, "2026-09-02")["action_id"], "consumed")
    store.upsert_action(gid, "2026-09-03", intent="c", push_level="direct")  # planned
    g = c.get(f"/api/goals/for-conversation?conversation_id={CONV}").json()["goal"]
    assert g["beats"]["used"] == 1, "只算 consumed/sent 且力度非 none 的拍"
    assert g["beats"]["cap"] == 5


def test_sprint_live_ticker_on_requires_effective_chain():
    """限时档：sprint 开 + sprint.dry_run → ticker_on=False、nudgeable=False、
    blockers 点名；dry_run 关 → ticker_on=True。D1b 起 care 的 dry_run 对冲刺
    无效（冲刺行 live），所以这里 care 恒 dry_run=True 以钉住「不再被拦」。"""
    def _mk(sprint_dry):
        c = _client(_cfg(
            goals={"sprint": {"enabled": True, "dry_run": sprint_dry,
                              "platforms": ["telegram", "whatsapp"]}},
            care={"enabled": True, "dry_run": True}))
        r = c.post("/api/goals", json={
            "template": "conversion_unlock", "conversation_id": CONV,
            "autonomy": "auto", "pace": "today", "deadline_days": 0.2})
        assert r.status_code == 200, r.text
        return c.get(f"/api/goals/for-conversation?conversation_id={CONV}").json()["goal"]

    g = _mk(True)
    live = g["sprint_live"]
    assert live["ticker_enabled"] is True
    assert live["ticker_on"] is False and live["nudgeable"] is False
    assert live["blockers"] == ["sprint_dry_run"]
    assert g["beats"]["progress_kind"] == "milestone"
    reset_goal_store()
    g2 = _mk(False)
    assert g2["sprint_live"]["ticker_on"] is True
    assert g2["sprint_live"]["nudgeable"] is True
    assert g2["sprint_live"]["blockers"] == []


# ── 分支表同步 / 启动日志 ─────────────────────────────────────────────────────

def test_signal_progress_templates_match_ledger_branches():
    from src.companion.goals import ledger
    src = inspect.getsource(ledger.settle_goal)
    ids = set(re.findall(r'(?:if|elif) tid == "([a-z_]+)"', src))
    assert ids == set(goal_routes._SIGNAL_PROGRESS_TEMPLATES), (
        "ledger.settle_goal 的模板分支表变了：同步 goal_routes._SIGNAL_PROGRESS_TEMPLATES")


def test_startup_log_no_longer_checkmarks_disabled_sprint():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "bootstrap"
           / "background_tasks.py").read_text(encoding="utf-8")
    assert "sprint_engine_status" in src
    assert "⚠ 冲刺推进器未启用" in src and "⚠ 冲刺推进器已开但派发被拦" in src
    assert "✅ 冲刺推进器已常备（goals.sprint.enabled=%s）" not in src, (
        "enabled=False 不许再打 ✅")


def test_cp_goal_ui_consumes_engine_truth_in_both_trees():
    """cp-goal.js（双树逐字节同步）：① 单一判定口 _autoEngine（限时看 sprint_effective、
    自然看 natural_auto_effective，旧字段回落，缺字段不猜）；② 表单 auto 卡灰掉+原因、
    推荐徽标让位、缺省不落灰卡；③ 目标卡 auto 标签打叉 + 卡身原因行；④ 进度口径
    「时间 X% · 动作 N/M 拍」；⑤ sprintLine 点名阻塞点；⑥ 所有新键 zh/en 齐备。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    a = (root / "shared" / "copilot" / "components" / "cp-goal.js").read_bytes()
    b = (root / "desktop" / "renderer" / "shared" / "copilot" / "components"
         / "cp-goal.js").read_bytes()
    assert a == b, "cp-goal.js 双树不同步（谁改组件谁同步两份）"
    js = a.decode("utf-8")
    for needle in (
        "_autoEngine(pace, src)", "sprint_effective", "natural_auto_effective",
        "sprint_blockers", "natural_auto_blockers", "_engineWhy(",
        'const off = autoOff && lvl === "auto"', ".gl-auto-card.off",
        'class="gl-tag${engOff ? " off" : ""}"', "gl-engine-note",
        "inbox.goal.engine.card_off", "inbox.goal.meta.time_pct",
        "inbox.goal.meta.beats", 'bt.progress_kind === "time"',
        "inbox.goal.sprint.engine_blocked", "inbox.goal.autonomy.auto_note_sprint_off",
        "inbox.goal.autonomy.auto_ineffective_t",
    ):
        assert needle in js, f"cp-goal.js 缺 #166 接线: {needle}"
    from src.web.i18n_packs import goals as g
    keys = re.findall(r'"(inbox\.goal\.(?:engine|meta)\.[a-z_.]+|inbox\.goal\.autonomy\.auto_(?:note_sprint_off|note_why|ineffective_t)|inbox\.goal\.sprint\.engine_blocked)"', js)
    assert keys
    for k in set(keys):
        if k.endswith("."):
            continue  # 动态拼接前缀（inbox.goal.engine.blk. + 码），下面逐码验
        assert k in g.ZH and k in g.EN, f"i18n 键 {k} 缺 zh/en"
    for blk in ("goals_disabled", "sprint_disabled", "sprint_dry_run",
                "care_disabled", "care_dry_run",
                "platform", "bridge_disabled", "proactive_disabled", "proactive_dry_run"):
        k = f"inbox.goal.engine.blk.{blk}"
        assert k in g.ZH and k in g.EN, f"阻塞点码 {blk} 缺 zh/en 人话"


def test_desktop_seeds_ship_sprint_enabled():
    """决策 D1：cloud_light 预设档 + 桌面 internal 种子 出厂 sprint.enabled=true。"""
    import yaml
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "config"
    for name in ("profiles/cloud_light.yaml", "config.desktop.internal.yaml"):
        data = yaml.safe_load((root / name).read_text(encoding="utf-8")) or {}
        sprint = (((data.get("companion") or {}).get("goals") or {})
                  .get("sprint") or {})
        assert sprint.get("enabled") is True, f"{name} 缺 companion.goals.sprint.enabled: true"
