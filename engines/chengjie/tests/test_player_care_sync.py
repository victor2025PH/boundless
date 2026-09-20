"""player_care B3：player_sync 定时拉网关写画像 + 沉默/充值事件起 goals + 域内模板。

    $env:PYTHONPATH=""; .\\.venv\\Scripts\\python.exe -m pytest tests\\test_player_care_sync.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from domains.player_care import goal_templates as gt
from domains.player_care.gateway import PlayerGateway
from domains.player_care.profile import (
    STAGE_CHATTING, STAGE_DEPOSITING, STAGE_DORMANT, STAGE_REGISTERED,
    PlayerProfileService, set_profile_service,
)
from domains.player_care.sync import (
    CREATED_BY, maybe_create_player_goal, resolve_sync_cfg, run_player_sync,
)
from src.companion.goals.store import GoalStore
from src.companion.goals.templates import TEMPLATES, PUSH_LEVELS, get_template
from src.contacts.store import ContactStore

DAY = 86400.0
T0 = 1_800_000_000.0
GW_CFG = {"enabled": True, "url": "http://gw.test", "key": "k", "timeout_sec": 2, "cache_ttl_sec": 0}


@pytest.fixture(autouse=True)
def _clean_templates():
    gt.unregister_goal_templates()
    yield
    gt.unregister_goal_templates()


@pytest.fixture
def store(tmp_path):
    s = ContactStore(tmp_path / "contacts.db")
    yield s
    s.close()


@pytest.fixture
def svc(store):
    s = PlayerProfileService(store, dormant_after_days=7, active_min_deposit_days=2)
    set_profile_service(s)
    yield s
    set_profile_service(None)


@pytest.fixture
def goals():
    return GoalStore(":memory:")


def _fake_gw(responses):
    """responses: phone → (status, body dict)；记录被查过的手机号。"""
    seen = []

    def transport(url, headers, body, timeout):
        payload = json.loads(body.decode("utf-8"))
        seen.append(payload.get("phone") or payload.get("uid"))
        status, data = responses.get(payload.get("phone") or payload.get("uid"), (404, {}))
        return status, json.dumps(data).encode("utf-8")

    gw = PlayerGateway(GW_CFG, transport=transport)
    gw.seen = seen  # type: ignore[attr-defined]
    return gw


def _seed(svc, key, *, now, phone="", acct="wa-01", n=2):
    row = None
    for i in range(n):
        row = svc.record_inbound(key=key, text="hello", platform="whatsapp", account_id=acct,
                                 external_id=key.split(":")[-1], phone=phone, now=now + i)
    return row


CFG = {"companion": {"goals": {"enabled": True}}, "player_care": {"sync": {"interval_min": 30}}}


# ── 模板 ───────────────────────────────────────────────────────────────────

def test_domain_templates_are_registered_only_on_demand_and_never_push():
    assert gt.TEMPLATE_REENGAGE not in TEMPLATES
    assert gt.register_goal_templates() == 2
    assert gt.register_goal_templates() == 0            # 幂等
    for tid in (gt.TEMPLATE_REENGAGE, gt.TEMPLATE_AFTER_DEPOSIT):
        t = get_template(tid)
        assert t is not None
        n = len(t["milestones"])
        assert len(t["push_curve"]) == n and set(t["push_curve"]) == {"none"}
        assert all(lvl in PUSH_LEVELS for lvl in t["push_curve"])
        assert set(t["intents"]) == set(range(n))
        for pool in t["intents"].values():
            for s in pool:
                assert not re.search(r"(?<!不)催", s), s        # 意图池里的「催」只能是「不催」
                assert not re.search(r"(?<!不承诺)赢", s), s
    assert gt.unregister_goal_templates() == 2
    assert gt.TEMPLATE_REENGAGE not in TEMPLATES


def test_register_never_overrides_core_template():
    core = TEMPLATES["engagement_reactivate"]
    gt.PLAYER_GOAL_TEMPLATES_BAK = dict(gt.PLAYER_GOAL_TEMPLATES)
    try:
        gt.PLAYER_GOAL_TEMPLATES["engagement_reactivate"] = {"name_zh": "x"}
        gt.register_goal_templates()
        assert TEMPLATES["engagement_reactivate"] is core
        assert gt.unregister_goal_templates() == 2      # 只删自己挂的两个
    finally:
        gt.PLAYER_GOAL_TEMPLATES.clear()
        gt.PLAYER_GOAL_TEMPLATES.update(gt.PLAYER_GOAL_TEMPLATES_BAK)
        del gt.PLAYER_GOAL_TEMPLATES_BAK


# ── 配置 ───────────────────────────────────────────────────────────────────

def test_resolve_sync_cfg_defaults_and_floor():
    c = resolve_sync_cfg(None)
    assert c["enabled"] and c["interval_min"] == 30 and c["active_days"] == 7 and c["batch"] == 50
    assert c["goals"] == {"enabled": True, "autonomy": "auto", "max_per_day": 20,
                          "reengage_days": 7, "after_deposit_days": 3}
    c = resolve_sync_cfg({"player_care": {"sync": {"interval_min": 1, "goals": {"autonomy": "Suggest"}}}})
    assert c["interval_min"] == 5 and c["goals"]["autonomy"] == "suggest"
    c = resolve_sync_cfg(SimpleNamespace(config={"player_care": {"sync": {"enabled": False}}}))
    assert not c["enabled"]


# ── record_sync ────────────────────────────────────────────────────────────

def test_record_sync_updates_facts_without_touching_last_seen(svc):
    _seed(svc, "639170000001", now=T0, phone="639170000001")
    before = svc.store.get_player_profile("639170000001")
    out = svc.record_sync("639170000001", {"found": True, "text": "Games: Jili Slots. deposit: 500 PHP",
                                           "games": [{"name": "Jili Slots"}]}, now=T0 + 3600)
    row = out["row"]
    assert out["events"] == ["registered", "deposit"]
    assert row["stage"] == STAGE_DEPOSITING
    assert row["last_seen"] == before["last_seen"] and row["inbound_count"] == before["inbound_count"]
    assert row["lookups"] == 1 and row["last_found"] and row["facts_text"].startswith("Games:")
    assert row["games"] == [{"name": "Jili Slots"}]
    # 同日再刷：不重复报 deposit
    out2 = svc.record_sync("639170000001", {"found": True, "text": "deposit: 500 PHP"}, now=T0 + 7200)
    assert out2["events"] == [] and out2["row"]["lookups"] == 2
    assert svc.store.get_player_profile("nope") is None
    assert svc.record_sync("nope", {"found": True, "text": "x"}, now=T0)["row"] is None


def test_record_sync_not_found_keeps_stage(svc):
    _seed(svc, "639170000002", now=T0, phone="639170000002")
    out = svc.record_sync("639170000002", {"found": False, "error": "timeout"}, now=T0 + 10)
    assert out["events"] == [] and out["row"]["stage"] == STAGE_CHATTING
    assert out["row"]["last_error"] == "timeout" and not out["row"]["last_found"]


def test_record_sync_on_dormant_only_updates_wake_stage(svc):
    _seed(svc, "639170000003", now=T0, phone="639170000003")
    assert svc.apply_dormancy(now=T0 + 8 * DAY) == 1
    out = svc.record_sync("639170000003", {"found": True, "text": "uid 77 games: x"}, now=T0 + 8 * DAY + 1)
    assert out["row"]["stage"] == STAGE_DORMANT
    assert out["row"]["stage_before_dormant"] == STAGE_REGISTERED
    # 再来消息 → 回到 registered
    row = svc.record_inbound(key="639170000003", text="hi", platform="whatsapp", account_id="wa-01",
                             external_id="639170000003", phone="639170000003", now=T0 + 8 * DAY + 2)
    assert row["stage"] == STAGE_REGISTERED


# ── 目标护栏 ───────────────────────────────────────────────────────────────

def test_maybe_create_player_goal_idempotent_and_budget(goals):
    gcfg = resolve_sync_cfg(None)["goals"]
    row = {"platform": "whatsapp", "account_id": "wa-01", "external_id": "u1"}
    g = maybe_create_player_goal(goals, row, gt.TEMPLATE_REENGAGE, gcfg=gcfg, now=T0,
                                 params={"stage_before": "chatting"})
    assert g and g["template"] == gt.TEMPLATE_REENGAGE and g["created_by"] == CREATED_BY
    assert g["conversation_id"] == "whatsapp:wa-01:u1" and g["autonomy"] == "auto"
    assert json.loads(g["params"] if isinstance(g["params"], str) else json.dumps(g["params"]))["stage_before"] == "chatting"
    assert maybe_create_player_goal(goals, row, gt.TEMPLATE_AFTER_DEPOSIT, gcfg=gcfg, now=T0) is None  # 同会话已有 active
    # 预算
    gcfg2 = dict(gcfg, max_per_day=1)
    row2 = {"platform": "whatsapp", "account_id": "wa-01", "external_id": "u2"}
    assert maybe_create_player_goal(goals, row2, gt.TEMPLATE_REENGAGE, gcfg=gcfg2, now=T0) is None
    # 缺 platform / external_id 不建；goals 关不建；store None 不建
    assert maybe_create_player_goal(goals, {"platform": "", "external_id": "x"}, gt.TEMPLATE_REENGAGE, gcfg=gcfg, now=T0) is None
    assert maybe_create_player_goal(goals, row2, gt.TEMPLATE_REENGAGE, gcfg=dict(gcfg, enabled=False), now=T0) is None
    assert maybe_create_player_goal(None, row2, gt.TEMPLATE_REENGAGE, gcfg=gcfg, now=T0) is None


# ── run_player_sync 整轮 ───────────────────────────────────────────────────

def test_run_sync_dormant_spawns_reengage_goal(svc, goals):
    _seed(svc, "whatsapp:u-dormant", now=T0)
    _seed(svc, "639170000009", now=T0 + 7.5 * DAY, phone="639170000009")   # 还活跃
    gw = _fake_gw({"639170000009": (200, {"chatx_text": "Games: Jili Slots"})})
    s = run_player_sync(CFG, gateway=gw, profile=svc, goal_store=goals, now=T0 + 8 * DAY, sleep=lambda _s: None)
    assert s["dormant"] == 1 and s["goals"] == 1
    assert svc.store.get_player_profile("whatsapp:u-dormant")["stage"] == STAGE_DORMANT
    g = goals.find_active_goal(conversation_id="whatsapp:wa-01:u-dormant", account_id="wa-01")
    assert g and g["template"] == gt.TEMPLATE_REENGAGE
    assert get_template(gt.TEMPLATE_REENGAGE) is not None          # 整轮里已注册
    # 活跃那位：有手机号 → 查了网关 → registered
    assert s["scanned"] == 1 and s["looked_up"] == 1 and s["found"] == 1 and s["deposits"] == 0
    assert gw.seen == ["639170000009"]
    assert svc.store.get_player_profile("639170000009")["stage"] == STAGE_REGISTERED
    # 再跑一轮：dormant 不重复；30min 内不重查
    s2 = run_player_sync(CFG, gateway=gw, profile=svc, goal_store=goals, now=T0 + 8 * DAY + 60, sleep=lambda _s: None)
    assert s2["dormant"] == 0 and s2["looked_up"] == 0 and s2["goals"] == 0


def test_run_sync_new_deposit_spawns_after_deposit_goal(svc, goals):
    _seed(svc, "639170000010", now=T0, phone="639170000010")
    _seed(svc, "whatsapp:no-ident", now=T0)                              # 没手机号/UID 不查
    gw = _fake_gw({"639170000010": (200, {"chatx_text": "Last deposit: 300 PHP\nGames: Super Ace"})})
    s = run_player_sync(CFG, gateway=gw, profile=svc, goal_store=goals, now=T0 + 3600, sleep=lambda _s: None)
    assert s["looked_up"] == 1 and s["found"] == 1 and s["deposits"] == 1 and s["goals"] == 1
    row = svc.store.get_player_profile("639170000010")
    assert row["stage"] == STAGE_DEPOSITING and row["deposit_days"] == 1
    assert row["facts_text"].startswith("Last deposit: 300 PHP")       # 原样落，不算不推
    g = goals.find_active_goal(conversation_id="whatsapp:wa-01:639170000010", account_id="wa-01")
    assert g and g["template"] == gt.TEMPLATE_AFTER_DEPOSIT
    # 次日再查同一事实：deposit_last_day 变了 → 又算一次充值日 → active；同会话已有目标 → 不再建
    s2 = run_player_sync(CFG, gateway=gw, profile=svc, goal_store=goals, now=T0 + DAY + 3600, sleep=lambda _s: None)
    assert s2["deposits"] == 1 and s2["goals"] == 0
    assert svc.store.get_player_profile("639170000010")["stage"] == "active"


def test_run_sync_errors_counted_and_goals_disabled(svc):
    _seed(svc, "639170000011", now=T0, phone="639170000011")
    gw = _fake_gw({"639170000011": (500, {})})
    cfg = {"companion": {"goals": {"enabled": False}}}
    s = run_player_sync(cfg, gateway=gw, profile=svc, now=T0 + 3600, sleep=lambda _s: None)
    assert s["looked_up"] == 1 and s["errors"] == 1 and s["found"] == 0 and s["goals"] == 0
    row = svc.store.get_player_profile("639170000011")
    assert row["last_error"] == "http_500" and row["stage"] == STAGE_CHATTING


def test_run_sync_skips_when_disabled_or_unconfigured(svc):
    s = run_player_sync({"player_care": {"sync": {"enabled": False}}}, profile=svc, now=T0)
    assert s["skipped"] == "disabled"
    s = run_player_sync({}, profile=svc, gateway=PlayerGateway({"enabled": False}), now=T0)
    assert s["skipped"] == "gateway_unconfigured"
    set_profile_service(None)
    s = run_player_sync({}, now=T0)     # 裸 dict 没 config_path → 无画像库
    assert s["skipped"] == "no_profile_store"


def test_run_sync_batch_limit(svc):
    for i in range(5):
        _seed(svc, f"63917000010{i}", now=T0 + i, phone=f"63917000010{i}")
    gw = _fake_gw({})
    cfg = {"player_care": {"sync": {"batch": 2}}}
    s = run_player_sync(cfg, gateway=gw, profile=svc, now=T0 + 3600, sleep=lambda _s: None)
    assert s["looked_up"] == 2 and len(gw.seen) == 2


# ── watchdog 接线 ──────────────────────────────────────────────────────────

def test_watchdog_check_player_sync_only_for_player_care_domain(monkeypatch, svc):
    from src.inbox.health_watchdog import HealthWatchdog
    calls = []

    def fake_run(cfg_root, config_path=None, *, now=None, **_kw):
        calls.append(now)
        return {"scanned": 0, "looked_up": 0, "found": 0, "deposits": 0, "dormant": 0,
                "goals": 0, "errors": 0, "skipped": ""}

    monkeypatch.setattr("domains.player_care.sync.run_player_sync", fake_run)
    wd = HealthWatchdog.__new__(HealthWatchdog)
    wd._config_manager = SimpleNamespace(config={"domain": "story_matrix"}, config_path="c.yaml")
    wd._last_player_sync_ts = 0.0
    wd.total_player_sync_runs = 0
    wd.last_player_sync = {}
    assert wd._check_player_sync(now=T0) is None and calls == []

    wd._config_manager = SimpleNamespace(config={"domain": "player_care"}, config_path="c.yaml")
    assert wd._check_player_sync(now=T0) is not None and calls == [T0]
    assert wd.total_player_sync_runs == 1 and wd.last_player_sync["ts"] == T0
    assert wd._check_player_sync(now=T0 + 10 * 60) is None      # 30min 节流
    assert wd._check_player_sync(now=T0 + 31 * 60) is not None and len(calls) == 2
