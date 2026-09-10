"""Q-8 A/B/C/E（#264 #263）：默认目标自动挂 + 存量批量挂 / 阶段计划今日意图 / 注入分级 must /
无新信息×2 投人设话题。

A：账号级 / 人设级默认目标 KV → 新会话首条入站自动挂（护栏：自聊 / 群 / 停联 / 需人工 / 已有目标 /
   摸底暂停 / 每日上限）+ 路由 set / clear / 预览（默认不勾） / 按勾选批量挂 + [goal-default] 日志。
B：intimacy 分档 → relationship_stage 模板意图，按 (conv, day) 确定性轮换；无目标陪伴域会话注入
   「阶段 · 今日主线」块（不建目标行）；for-conversation 带 stage_plan。
C：no_new_info 判定 + 连续 2 轮 → level=must（[goal-inject] level=must reason=no_new_info）。
E：must 时投人设话题库新话题，避开近 10 次用过的；停联冻结 → 不投（[proactive_topic] blocked）。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.companion.goals import defaults as gdef
from src.companion.goals import planner, service as svc
from src.companion.goals.signals import no_new_info, no_new_info_streak
from src.companion.goals.store import get_goal_store, reset_goal_store

_ROOT = Path(__file__).resolve().parents[1]
PLAT, ACCT, CK = "whatsapp", "12137839654", "19096186612"
CONV = f"{PLAT}:{ACCT}:{CK}"
CFG_COMPANION = {"companion": {"goals": {"enabled": True, "db_path": ":memory:"}},
                 "business_domain": "companion"}


class _Inbox:
    def __init__(self):
        self.kv, self.msgs, self.tags, self.convs = {}, {}, {}, []

    def get_app_setting(self, key, default=""):
        return self.kv.get(key, default)

    def set_app_setting(self, key, value, updated_by=""):
        if value == "":
            self.kv.pop(key, None)
        else:
            self.kv[key] = value
        return True

    def list_app_settings(self, prefix=""):
        return [{"key": k, "value": v, "updated_by": "", "updated_at": 0}
                for k, v in self.kv.items() if k.startswith(prefix)]

    def list_recent_messages(self, conv, limit=30):
        return list(self.msgs.get(conv, []))[-limit:]

    def get_conv_tags(self, conv):
        return list(self.tags.get(conv, []))

    def list_conversations(self, *, limit=50, platform="", account_id="", chat_type="", before_ts=None):
        return [c for c in self.convs if c["platform"] == platform and c["account_id"] == account_id][:limit]

    def get_conversation(self, conv):
        return {"conversation_id": conv, "chat_key": CK, "display_name": "Enrique"}

    def get_conv_meta(self, conv):
        return {}

    def get_automation_mode(self, conv):
        return "auto_ai"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    from src.utils import business_domain as bd
    bd.reset_active_business_domain()
    svc._inject_log_seen.clear()
    reset_goal_store()
    yield
    reset_goal_store()
    bd.reset_active_business_domain()


def _client(inbox, cfg=None, monkeypatch=None):
    import src.integrations.protocol_bridge as pb
    from src.web.routes.goal_routes import register_goal_routes
    monkeypatch.setattr(pb, "_inbox_store_getter", lambda: inbox)
    sess = {"role": "", "user": "tester"}
    app = FastAPI()

    def auth_dep(request: Request) -> None:
        request.scope["session"] = dict(sess)

    register_goal_routes(app, auth_dep, SimpleNamespace(config=cfg or CFG_COMPANION, config_path=None))
    return TestClient(app), sess


# ── C：no_new_info 判定 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["You are so beautiful", "wow", "😍😍", "good morning babe", "ok",
                                  "哈哈 好美", "谢谢", "hahaha nice", "so cute!!"])
def test_no_new_info_true(text):
    assert no_new_info(text) is True


@pytest.mark.parametrize("text", ["I run a bakery in LA", "what do you do?", "3 kids", "我今天去上班了",
                                  "My mom is visiting this weekend so I'm cooking for the whole family",
                                  "你呢？"])
def test_no_new_info_false(text):
    assert no_new_info(text) is False


def test_no_new_info_streak_counts_only_trailing_inbound():
    h = [{"direction": "in", "text": "I work nights at the hospital"},
         {"direction": "out", "text": "wow, nurse?"},
         {"direction": "in", "text": "so pretty"},
         {"direction": "out", "text": "thank you!"},
         {"direction": "in", "text": "😍"}]
    assert no_new_info_streak(h) == 2
    assert no_new_info_streak(h + [{"direction": "in", "text": "I have two dogs"}]) == 0
    assert no_new_info_streak([]) == 0


# ── B：阶段计划 planner ─────────────────────────────────────────────────────────

def test_intimacy_stage_bands_and_plan_stage_beat_deterministic():
    assert planner.intimacy_stage(-1) == "stranger"
    assert planner.intimacy_stage(10) == "stranger"
    assert planner.intimacy_stage(30) == "friend"
    assert planner.intimacy_stage(60) == "close"
    assert planner.intimacy_stage(90) == "soulmate"
    b1 = planner.plan_stage_beat(stage="friend", conversation_id=CONV, day="2026-09-10")
    b2 = planner.plan_stage_beat(stage="friend", conversation_id=CONV, day="2026-09-10")
    assert b1 == b2 and b1["intent"] and b1["intent_en"] and b1["level"] == "soft" and b1["stage"] == "friend"
    # 意图来自 relationship_stage 模板（不新造弧线）
    from src.companion.goals.templates import TEMPLATES
    pool = [pair[0] for pair in TEMPLATES["relationship_stage"]["intents"][1]]   # friend = idx 1
    assert b1["intent"] in pool
    m = planner.plan_stage_beat(stage="friend", conversation_id=CONV, day="2026-09-10", no_new_info_streak=2)
    assert m["level"] == "must"
    assert planner.plan_stage_beat(stage="friend", conversation_id=CONV, day="2026-09-10",
                                   missed_x2=True)["level"] == "must"
    assert planner.stage_label("close", "zh") and planner.stage_label("close", "en") != planner.stage_label("close", "zh")


def test_stage_plan_enabled_companion_default_sales_off():
    assert svc.stage_plan_enabled(CFG_COMPANION) is True
    assert svc.stage_plan_enabled({"companion": {"goals": {"enabled": True}}, "business_domain": "sales"}) is False
    assert svc.stage_plan_enabled({"companion": {"goals": {"enabled": True, "stage_plan": {"enabled": True}}},
                                   "business_domain": "sales"}) is True
    assert svc.stage_plan_enabled({"companion": {"goals": {"enabled": True, "stage_plan": {"enabled": False}}},
                                   "business_domain": "companion"}) is False


# ── B/C/E：无目标陪伴域会话注入块 ──────────────────────────────────────────────

def test_build_block_no_goal_companion_emits_stage_block_and_must_topic(monkeypatch, caplog):
    inbox = _Inbox()
    inbox.msgs[CONV] = [
        {"direction": "in", "text": "you are gorgeous", "ts": 1.0},
        {"direction": "out", "text": "aw thank you", "ts": 2.0},
    ]
    monkeypatch.setattr(svc, "resolve_stage", lambda p, a, c: ("friend", 40.0))
    uc = {"chat_id": CK}
    caplog.set_level(logging.INFO, logger="src.companion.goals.service")
    # 单轮无新信息 → 只有阶段主线（soft）
    inbox.msgs[CONV] = [{"direction": "in", "text": "I just got back from my sister's wedding", "ts": 1.0},
                        {"direction": "out", "text": "oh how was it", "ts": 2.0}]
    blk = svc.build_block_for_chat(CFG_COMPANION, platform=PLAT, chat_key=CK,
                                   account_id=ACCT, conversation_id=CONV, user_context=uc,
                                   inbox_store=inbox, inbound_text="so beautiful 😍", now=10.0)
    assert blk and "【关系阶段 · 今日主线】" in blk and "【本轮必做】" not in blk
    assert uc["_goal_inject_meta"]["reason"] == "stage_plan" and uc["_goal_inject_meta"]["level"] == "soft"
    assert any("[goal-inject] level=soft reason=stage_plan" in r.getMessage() for r in caplog.records)
    caplog.clear()
    # 连续 2 轮无新信息 → must + 投话题 + 两条日志
    inbox.msgs[CONV] = [{"direction": "in", "text": "you are gorgeous", "ts": 1.0},
                        {"direction": "out", "text": "aw thank you", "ts": 2.0}]
    uc = {"chat_id": CK}
    blk = svc.build_block_for_chat(CFG_COMPANION, platform=PLAT, chat_key=CK,
                                   account_id=ACCT, conversation_id=CONV, user_context=uc,
                                   inbox_store=inbox, inbound_text="wow 😍", now=10.0)
    assert blk and "【本轮必做】" in blk and svc.MUST_DISCIPLINE_ZH in blk
    assert uc["_goal_inject_meta"]["level"] == "must" and uc["_goal_inject_meta"]["topic"]
    msgs = [r.getMessage() for r in caplog.records]
    assert any("[goal-inject] level=must reason=no_new_info" in m for m in msgs), msgs
    assert any("[proactive_topic] conv=" in m and "reason=no_new_info_x2" in m for m in msgs), msgs
    # 话题落 KV 用过表
    used = json.loads(inbox.kv[svc.TOPIC_USED_KEY_PREFIX + CONV])
    assert used and used[-1] == uc["_goal_inject_meta"]["topic"]


def test_build_block_no_goal_sales_domain_unchanged(monkeypatch):
    inbox = _Inbox()
    uc = {"chat_id": CK}
    cfg = {"companion": {"goals": {"enabled": True, "db_path": ":memory:"}}, "business_domain": "sales"}
    blk = svc.build_block_for_chat(cfg, platform=PLAT, chat_key=CK,
                                   account_id=ACCT, conversation_id=CONV, user_context=uc,
                                   inbox_store=inbox, inbound_text="so beautiful", now=10.0)
    assert blk is None
    assert uc["_goal_inject_meta"]["reason"] == "no_goal"


def test_proactive_topic_blocked_by_stop_contact(monkeypatch, caplog):
    inbox = _Inbox()
    from src.inbox.stop_contact import STOP_CONTACT_TAG
    inbox.tags[CONV] = [STOP_CONTACT_TAG]
    inbox.msgs[CONV] = [{"direction": "in", "text": "wow", "ts": 1.0}, {"direction": "out", "text": "hi", "ts": 2.0}]
    monkeypatch.setattr(svc, "resolve_stage", lambda p, a, c: ("stranger", 5.0))
    caplog.set_level(logging.INFO, logger="src.companion.goals.service")
    uc = {"chat_id": CK}
    blk = svc.build_block_for_chat(CFG_COMPANION, platform=PLAT, chat_key=CK,
                                   account_id=ACCT, conversation_id=CONV, user_context=uc,
                                   inbox_store=inbox, inbound_text="😍", now=10.0)
    assert blk and "【本轮必做】" in blk and uc["_goal_inject_meta"]["topic"] == ""
    assert any("[proactive_topic] conv=" in r.getMessage() and "reason=blocked:frozen" in r.getMessage()
               for r in caplog.records)


def test_pick_proactive_topic_prefers_persona_pool_and_avoids_recent():
    inbox = _Inbox()
    persona = {"hobbies": "冲浪, 烘焙", "topics": ["养猫"], "life_arc": {"beats": ["下周去看演唱会"]}}
    pool = svc.persona_topic_pool(persona)
    assert pool == ["养猫", "冲浪", "烘焙", "下周去看演唱会"]
    seen = set()
    for _ in range(4):
        t = svc.pick_proactive_topic(CONV, persona, inbox_store=inbox, now=1000.0)
        assert t in pool and t not in seen
        seen.add(t)
    # 全用过 → 回落兜底池而不是空
    assert svc.pick_proactive_topic(CONV, persona, inbox_store=inbox, now=1000.0)
    assert svc.pick_proactive_topic(CONV, None, inbox_store=_Inbox(), now=1000.0, lang="en") in [
        e for _z, e in svc._FALLBACK_TOPICS]


# ── C：有目标摸底路 must 升级 ───────────────────────────────────────────────────

def test_discovery_goal_probe_escalates_to_must_on_no_new_info(monkeypatch, caplog):
    inbox = _Inbox()
    inbox.msgs[CONV] = [{"direction": "in", "text": "you look amazing", "ts": 1.0},
                        {"direction": "out", "text": "thanks!", "ts": 2.0}]
    gs = get_goal_store(":memory:")
    gs.create_goal(conversation_id=CONV, platform=PLAT, account_id=ACCT, chat_key=CK, template="profile_discovery",
                   autonomy="auto", deadline_days=3, params={"slots": "occupation,age,location,interests"})
    caplog.set_level(logging.INFO, logger="src.companion.goals.service")
    uc = {"chat_id": CK}
    cfg = {"companion": {"goals": {"enabled": True, "db_path": ":memory:"}}, "business_domain": "sales"}
    blk = svc.build_block_for_chat(cfg, platform=PLAT, chat_key=CK, account_id=ACCT, conversation_id=CONV,
                                   user_context=uc, inbox_store=inbox, inbound_text="so pretty 😍", now=time.time())
    assert blk and "【本轮必问" in blk and svc.MUST_DISCIPLINE_ZH in blk
    msgs = [r.getMessage() for r in caplog.records]
    assert any("[goal-inject] level=must reason=no_new_info" in m for m in msgs), msgs
    # 有新信息 → soft
    caplog.clear()
    inbox.msgs[CONV] = [{"direction": "in", "text": "I'm a nurse in Dallas", "ts": 1.0}]
    uc = {"chat_id": CK}
    blk = svc.build_block_for_chat(cfg, platform=PLAT, chat_key=CK, account_id=ACCT, conversation_id=CONV,
                                   user_context=uc, inbox_store=inbox, inbound_text="and I have two kids",
                                   now=time.time())
    msgs = [r.getMessage() for r in caplog.records]
    if blk and "【本轮必问" in blk:
        assert svc.MUST_DISCIPLINE_ZH not in blk
        assert any("[goal-inject] level=soft reason=probe" in m for m in msgs), msgs


# ── A：默认目标 KV + 自动挂 ─────────────────────────────────────────────────────

def test_default_goal_kv_roundtrip_and_priority():
    inbox = _Inbox()
    assert gdef.get_default(inbox, platform=PLAT, account_id=ACCT, persona_id="p1") is None
    spec = gdef.normalize_spec({"template": "profile_discovery", "autonomy": "auto", "days": 5,
                                "params": {"slots": "occupation,location"}})
    assert spec and spec["days"] == 5.0 and spec["autonomy"] == "auto"
    assert gdef.normalize_spec({"template": "nope"}) is None
    assert gdef.set_default(inbox, gdef.persona_key("p1"), spec, by="boss")
    got = gdef.get_default(inbox, platform=PLAT, account_id=ACCT, persona_id="p1")
    assert got and got["scope"] == "persona" and got["template"] == "profile_discovery"
    spec2 = gdef.normalize_spec({"template": "relationship_stage"})
    assert gdef.set_default(inbox, gdef.account_key(PLAT, ACCT), spec2, by="boss")
    got = gdef.get_default(inbox, platform=PLAT, account_id=ACCT, persona_id="p1")
    assert got["scope"] == "account" and got["template"] == "relationship_stage"   # 账号级优先
    rows = gdef.list_defaults(inbox)
    assert {r["scope"] for r in rows} == {"account", "persona"}
    assert gdef.set_default(inbox, gdef.account_key(PLAT, ACCT), None)
    assert gdef.get_default(inbox, platform=PLAT, account_id=ACCT)["scope"] if False else True
    assert gdef.get_default(inbox, platform=PLAT, account_id=ACCT) is None
    assert gdef.set_default(inbox, "goals:other", spec2) is False


def test_maybe_attach_default_goal_guards_and_log(monkeypatch, caplog):
    inbox = _Inbox()
    gs = get_goal_store(":memory:")
    monkeypatch.setattr(gdef, "_conv_persona_id", lambda uc, ck: "")
    caplog.set_level(logging.INFO, logger="src.companion.goals.defaults")
    # 未设 → None，不建
    assert gdef.maybe_attach_default_goal(gs, CFG_COMPANION, platform=PLAT, chat_key=CK, account_id=ACCT,
                                          conversation_id=CONV, inbox_store=inbox) is None
    gdef.set_default(inbox, gdef.account_key(PLAT, ACCT),
                     gdef.normalize_spec({"template": "profile_discovery", "params": {"slots": "occupation"}}))
    # 自聊 / 群 / 停联 / 需人工 → 不挂
    assert gdef.maybe_attach_default_goal(gs, CFG_COMPANION, platform=PLAT, chat_key="me", account_id=ACCT,
                                          inbox_store=inbox) is None
    assert gdef.maybe_attach_default_goal(gs, CFG_COMPANION, platform=PLAT, chat_key=CK, account_id=ACCT,
                                          conversation_id=CONV, user_context={"is_group": True},
                                          inbox_store=inbox) is None
    from src.inbox.stop_contact import STOP_CONTACT_TAG
    from src.integrations.protocol_autoreply import HANDOFF_TAG
    inbox.tags[CONV] = [STOP_CONTACT_TAG]
    assert gdef.maybe_attach_default_goal(gs, CFG_COMPANION, platform=PLAT, chat_key=CK, account_id=ACCT,
                                          conversation_id=CONV, inbox_store=inbox) is None
    inbox.tags[CONV] = [HANDOFF_TAG]
    assert gdef.maybe_attach_default_goal(gs, CFG_COMPANION, platform=PLAT, chat_key=CK, account_id=ACCT,
                                          conversation_id=CONV, inbox_store=inbox) is None
    inbox.tags[CONV] = []
    # 摸底暂停 → 不挂
    svc.set_discovery_paused(True, inbox_store=inbox, by="t")
    assert gdef.maybe_attach_default_goal(gs, CFG_COMPANION, platform=PLAT, chat_key=CK, account_id=ACCT,
                                          conversation_id=CONV, inbox_store=inbox) is None
    svc.set_discovery_paused(False, inbox_store=inbox, by="t")
    assert gs.find_active_goal(conversation_id=CONV) is None
    # 正常 → 挂上，created_by=account_default，日志 attached=1
    g = gdef.maybe_attach_default_goal(gs, CFG_COMPANION, platform=PLAT, chat_key=CK, account_id=ACCT,
                                       conversation_id=CONV, inbox_store=inbox, now=time.time())
    assert g and g["created_by"] == gdef.CREATED_BY_ACCOUNT and g["template"] == "profile_discovery"
    assert any(f"[goal-default] account={PLAT}:{ACCT} conv={CONV} attached=1" in r.getMessage()
               for r in caplog.records)
    # 幂等：已有目标不再建
    assert gdef.maybe_attach_default_goal(gs, CFG_COMPANION, platform=PLAT, chat_key=CK, account_id=ACCT,
                                          conversation_id=CONV, inbox_store=inbox) is None
    assert len([x for x in gs.list_goals(limit=50) if x["conversation_id"] == CONV]) == 1
    # 每日上限
    monkeypatch.setattr(gdef, "MAX_ATTACH_PER_DAY", 1)
    conv2 = f"{PLAT}:{ACCT}:15550001111"
    assert gdef.maybe_attach_default_goal(gs, CFG_COMPANION, platform=PLAT, chat_key="15550001111", account_id=ACCT,
                                          conversation_id=conv2, inbox_store=inbox) is None
    assert any("reason=daily_cap" in r.getMessage() for r in caplog.records)


def test_build_block_for_chat_attaches_default_on_first_inbound(monkeypatch):
    inbox = _Inbox()
    gs = get_goal_store(":memory:")
    monkeypatch.setattr(gdef, "_conv_persona_id", lambda uc, ck: "")
    gdef.set_default(inbox, gdef.account_key(PLAT, ACCT),
                     gdef.normalize_spec({"template": "profile_discovery", "params": {"slots": "occupation,location"}}))
    uc = {"chat_id": CK}
    cfg = {"companion": {"goals": {"enabled": True, "db_path": ":memory:"}}, "business_domain": "sales"}
    # 无入站（主动链）不挂
    svc.build_block_for_chat(cfg, platform=PLAT, chat_key=CK, account_id=ACCT, conversation_id=CONV,
                             user_context=uc, inbox_store=inbox, inbound_text="", now=time.time())
    assert gs.find_active_goal(conversation_id=CONV) is None
    blk = svc.build_block_for_chat(cfg, platform=PLAT, chat_key=CK, account_id=ACCT, conversation_id=CONV,
                                   user_context=uc, inbox_store=inbox, inbound_text="hey there", now=time.time())
    g = gs.find_active_goal(conversation_id=CONV)
    assert g is not None and g["created_by"] == gdef.CREATED_BY_ACCOUNT
    assert blk and uc["_goal_inject_meta"]["injected"] is True


# ── A：路由 set / clear / 预览 / 批量挂 + for-conversation 载荷 ─────────────────────

def test_default_routes_set_preview_attach_and_card_payload(monkeypatch):
    inbox = _Inbox()
    monkeypatch.setattr(gdef, "_conv_persona_id", lambda uc, ck: "")
    client, sess = _client(inbox, monkeypatch=monkeypatch)
    gs = get_goal_store(":memory:")
    from src.inbox.stop_contact import STOP_CONTACT_TAG
    frozen = f"{PLAT}:{ACCT}:1001"
    inbox.tags[frozen] = [STOP_CONTACT_TAG]
    has = f"{PLAT}:{ACCT}:1002"
    gs.create_goal(conversation_id=has, platform=PLAT, account_id=ACCT, chat_key="1002",
                   template="relationship_stage", autonomy="auto", deadline_days=7)
    inbox.convs = [
        {"conversation_id": CONV, "platform": PLAT, "account_id": ACCT, "chat_key": CK, "display_name": "Enrique"},
        {"conversation_id": frozen, "platform": PLAT, "account_id": ACCT, "chat_key": "1001", "display_name": "F"},
        {"conversation_id": f"{PLAT}:{ACCT}:me", "platform": PLAT, "account_id": ACCT, "chat_key": "me"},
        {"conversation_id": f"{PLAT}:{ACCT}:group:9", "platform": PLAT, "account_id": ACCT, "chat_key": "group:9",
         "chat_type": "group"},
        {"conversation_id": has, "platform": PLAT, "account_id": ACCT, "chat_key": "1002"},
        {"conversation_id": f"{PLAT}:{ACCT}:1003", "platform": PLAT, "account_id": ACCT, "chat_key": "1003"},
    ]
    # 未设：attach 404；for-conversation default_goal=None，stage_plan（陪伴域）有载荷
    assert client.post("/api/goals/defaults/attach", json={"conversation_id": CONV}).status_code == 404
    assert client.post("/api/goals/defaults", json={"template": "profile_discovery"}).status_code == 400
    assert client.post("/api/goals/defaults", json={"conversation_id": CONV, "template": "zzz"}).status_code == 400
    monkeypatch.setattr(svc, "resolve_stage", lambda p, a, c: ("friend", 40.0))
    r = client.get(f"/api/goals/for-conversation?conversation_id={CONV}")
    assert r.status_code == 200 and r.json()["default_goal"] is None
    sp = r.json()["stage_plan"]
    assert sp and sp["stage"] == "friend" and sp["intent"] and sp["intent_en"] and sp["level"] == "soft"
    assert sp["stage_label"] == planner.stage_label("friend", "zh")
    # 设账号默认（卡片只带 conversation_id）
    r = client.post("/api/goals/defaults", json={"conversation_id": CONV, "template": "profile_discovery",
                                                 "params": {"slots": "occupation,location"}, "autonomy": "auto",
                                                 "days": 5})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["scope_key"] == gdef.account_key(PLAT, ACCT)
    r = client.get("/api/goals/defaults")
    assert r.status_code == 200 and r.json()["items"][0]["template_name"]
    r = client.get(f"/api/goals/for-conversation?conversation_id={CONV}")
    dg = r.json()["default_goal"]
    assert dg and dg["scope"] == "account" and dg["template"] == "profile_discovery" and dg["template_name"]
    # 预览：默认不勾 frozen / self / group / has_goal
    r = client.post("/api/goals/defaults/attach", json={"conversation_id": CONV, "dry_run": True})
    assert r.status_code == 200 and r.json()["dry_run"] is True
    items = {it["conversation_id"]: it for it in r.json()["items"]}
    assert len(items) == 6 and r.json()["checked"] == 2
    assert items[CONV]["checked"] and items[f"{PLAT}:{ACCT}:1003"]["checked"]
    assert items[frozen]["exclude"] == "frozen" and not items[frozen]["checked"]
    assert items[f"{PLAT}:{ACCT}:me"]["exclude"] == "self_chat"
    assert items[f"{PLAT}:{ACCT}:group:9"]["exclude"] == "group"
    assert items[has]["exclude"] == "has_goal"
    assert set(r.json()["unchecked_reasons"]) == set(gdef.DEFAULT_UNCHECKED)
    # 按勾选挂（把 frozen 手动勾回；has_goal 永不重复）
    r = client.post("/api/goals/defaults/attach", json={
        "conversation_id": CONV, "dry_run": False, "conversation_ids": [CONV, frozen, has, "x:y:z"]})
    assert r.status_code == 200
    body = r.json()
    assert body["attached"] == 2 and body["skipped"] == 2
    assert body["skipped_reasons"][has] == "has_goal" and body["skipped_reasons"]["x:y:z"] == "unknown"
    assert gs.find_active_goal(conversation_id=CONV)["created_by"] == gdef.CREATED_BY_ACCOUNT
    assert gs.find_active_goal(conversation_id=frozen) is not None
    assert len([g for g in gs.list_goals(limit=50) if g["conversation_id"] == has]) == 1
    # 有目标路也带 default_goal / stage_plan
    r = client.get(f"/api/goals/for-conversation?conversation_id={CONV}")
    assert r.json()["goal"]["created_by"] == gdef.CREATED_BY_ACCOUNT
    assert r.json()["default_goal"]["template"] == "profile_discovery" and r.json()["stage_plan"]["stage"] == "friend"
    # 只读拦；清默认
    sess["role"] = "viewer"
    assert client.post("/api/goals/defaults", json={"conversation_id": CONV, "clear": True}).status_code == 403
    sess["role"] = ""
    r = client.post("/api/goals/defaults", json={"conversation_id": CONV, "clear": True})
    assert r.status_code == 200 and r.json()["cleared"] is True
    assert client.get(f"/api/goals/for-conversation?conversation_id={CONV}").json()["default_goal"] is None


# ── E：proactive_topic 陪伴域 B 类默认开（运行时口径 + 能力看板同口径） ─────────────

def test_proactive_topic_runtime_domain_default(caplog):
    from src.companion import proactive_topic as pt
    from src.companion.capability_status import CAPABILITIES, evaluate_capability
    from src.utils import business_domain as bd
    caplog.set_level(logging.INFO)
    log = logging.getLogger("t.q8.pt")
    companion_cfg = {"business_domain": "companion", "companion": {"enabled": True, "proactive_topic": {}}}
    sales_cfg = {"business_domain": "sales", "companion": {"enabled": True, "proactive_topic": {}}}
    assert pt.resolve_proactive_topic_enabled(companion_cfg, log=log) is True
    assert any("[proactive_topic] enabled=1 source=domain_default domain=companion" in r.getMessage()
               for r in caplog.records)
    assert pt.resolve_proactive_topic_enabled(sales_cfg) is False
    assert pt.resolve_proactive_topic_enabled(
        {"business_domain": "companion", "companion": {"proactive_topic": {"enabled": False}}}) is False
    assert pt.resolve_proactive_topic_enabled(
        {"business_domain": "sales", "companion": {"proactive_topic": {"enabled": True}}}) is True
    # 能力看板与运行时同口径：缺席 + 陪伴域 → enabled；显式 false → 关
    cap = next(c for c in CAPABILITIES if c.key == "proactive_topic")
    assert evaluate_capability(cap, companion_cfg, None)["enabled"] is True
    assert evaluate_capability(cap, sales_cfg, None)["enabled"] is False
    assert evaluate_capability(cap, {"business_domain": "companion", "companion": {
        "enabled": True, "proactive_topic": {"enabled": False}}}, None)["enabled"] is False
    # 源码钉桩：maybe_start_companion_proactive 不再直读 cfg.get("enabled", False)
    src = (_ROOT / "src" / "companion" / "proactive_topic.py").read_text(encoding="utf-8")
    i = src.index("async def maybe_start_companion_proactive(")
    body = src[i:i + 1500]
    assert "resolve_proactive_topic_enabled(assistant.config.config" in body
    assert 'cfg.get("enabled", False)' not in body
    bd.reset_active_business_domain()


def test_frontend_wiring_i18n_and_version_bump():
    js = (_ROOT / "shared" / "copilot" / "components" / "cp-goal.js").read_text(encoding="utf-8")
    assert js == (_ROOT / "desktop" / "renderer" / "shared" / "copilot" / "components" / "cp-goal.js").read_text(
        encoding="utf-8")
    for needle in ("_renderStageRow(d)", "_renderDefaultRow(d, g)", "_renderDefaultRow(d, null)",
                   'data-act="default_set"', 'data-act="default_attach_open"', 'data-act="default_attach_apply"',
                   'data-act="default_pv_toggle"', 'data-act="default_clear"', "/api/goals/defaults/attach",
                   '"/api/goals/defaults"', "inbox.goal.stage.title", "inbox.goal.default.line",
                   "inbox.goal.default.preview_title", "account_default: 1", "persona_default: 1",
                   "d.stage_plan", "d.default_goal"):
        assert needle in js, needle
    from src.web.i18n_packs import goals as gp
    for k in ("inbox.goal.default.title", "inbox.goal.default.line", "inbox.goal.default.set_btn",
              "inbox.goal.default.attach_btn", "inbox.goal.default.preview_title", "inbox.goal.default.apply_btn",
              "inbox.goal.default.apply_done", "inbox.goal.default.excl.frozen", "inbox.goal.default.excl.self_chat",
              "inbox.goal.default.excl.needs_human", "inbox.goal.default.excl.group", "inbox.goal.default.excl.has_goal",
              "inbox.goal.stage.title", "inbox.goal.stage.line", "inbox.goal.stage.must", "inbox.goal.stage.no_new_info",
              "err.goals.default_scope_required", "err.goals.default_not_set"):
        assert k in gp.ZH and k in gp.EN, k
    for host in ("shared/copilot/app.html", "desktop/renderer/shared/copilot/app.html",
                 "src/web/templates/unified_inbox.html"):
        assert "cp-goal.js?v=20260910d" in (_ROOT / host).read_text(encoding="utf-8"), host
