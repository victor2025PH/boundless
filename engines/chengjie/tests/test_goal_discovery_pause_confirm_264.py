# -*- coding: utf-8 -*-
"""Q-1 E（#264）：目标页「暂停全部摸底目标」总开关 + 卡片槽位「已提及（未确认）」→「确认」。

- 总开关落 InboxStore app_settings KV（goals store.py 不动）；开着 → 摸底类目标不注入
  （reason=discovery_paused）；关掉即恢复；GET/POST 端点 + viewer 403。
- slots_progress 逐槽带 state=unknown|mentioned|confirmed（同注入口径：字段来源 + 最近 30 轮）。
- 确认端点：mentioned → confirmed（旧形 cell src=agent，两步写绕开同值跳过）；确认后注入链不再问。
- 前端两棵树同文 + ?v= 三处一致 + i18n 三语。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from src.companion.goals import service
from src.companion.goals.profile_slots import cell_view
from src.companion.goals.service import build_block_for_chat, confirm_profile_slot
from src.companion.goals.store import get_goal_store, reset_goal_store
from src.web.routes.goal_routes import register_goal_routes

_ROOT = Path(__file__).resolve().parents[1]
CONV = "whatsapp:12137839654:13105551234"
PLAT, ACCT, CK = "whatsapp", "12137839654", "13105551234"
T0 = time.mktime((2026, 9, 8, 20, 30, 0, 0, 0, -1))


class _Inbox:
    def __init__(self):
        self.msgs, self.kv, self.tags = [], {}, {}

    def add(self, direction, text, ts):
        self.msgs.append({"direction": direction, "text": text, "ts": ts})

    def list_recent_messages(self, conv, limit=30):
        return list(self.msgs)[-limit:]

    def get_conversation(self, conv):
        return {"last_ts": max((m["ts"] for m in self.msgs), default=0)}

    def get_conv_meta(self, conv):
        return {}

    def get_automation_mode(self, conv):
        return "auto_ai"

    def get_app_setting(self, key, default=""):
        return self.kv.get(key, default)

    def set_app_setting(self, key, value, updated_by=""):
        self.kv[key] = value
        return True

    def get_conv_tags(self, cid):
        return list(self.tags.get(cid, []))

    def set_conv_tags(self, cid, tags):
        self.tags[cid] = list(tags)
        return True


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    import src.integrations.protocol_bridge as pb
    inbox = _Inbox()
    monkeypatch.setattr(pb, "_inbox_store_getter", lambda: inbox)
    reset_goal_store()
    service._inject_log_seen.clear()
    yield inbox
    reset_goal_store()


def _cfg():
    return SimpleNamespace(config={"companion": {"goals": {"enabled": True, "db_path": ":memory:"}}},
                           config_path=None)


def _client():
    sess = {"role": "", "user": "tester"}
    app = FastAPI()

    def auth_dep(request: Request) -> None:
        request.scope["session"] = dict(sess)

    register_goal_routes(app, auth_dep, _cfg())
    return TestClient(app), sess


def _goal(gs, slots="occupation,age,location"):
    return gs.create_goal(conversation_id=CONV, platform=PLAT, account_id=ACCT, chat_key=CK,
                          template="profile_discovery", autonomy="auto", deadline_days=3,
                          params={"slots": slots}, now=T0) or {}


def _inject(inbox, text, now):
    uc = {}
    blk = build_block_for_chat(_cfg(), platform=PLAT, chat_key=CK, account_id=ACCT, conversation_id=CONV,
                               user_context=uc, chain="draft", inbound_text=text, inbox_store=inbox, now=now)
    return blk, uc.get("_goal_inject_meta") or {}


def test_master_switch_blocks_discovery_injection_and_resumes(_env):
    inbox = _env
    gs = get_goal_store(":memory:")
    _goal(gs)
    assert service.discovery_paused(inbox) is False
    blk, m = _inject(inbox, "long day at work", T0)
    assert blk and m.get("injected")
    assert service.set_discovery_paused(True, inbox_store=inbox, by="tester")
    assert service.discovery_paused(inbox) is True
    blk2, m2 = _inject(inbox, "long day at work", T0 + 60)
    assert blk2 is None and m2.get("reason") == "discovery_paused"
    service.set_discovery_paused(False, inbox_store=inbox)
    blk3, m3 = _inject(inbox, "long day at work", T0 + 120)
    assert blk3 and m3.get("injected")
    # 无 InboxStore（且 protocol_bridge 无回落）→ 视为未暂停，不因缺存储把目标全停
    import src.integrations.protocol_bridge as pb
    pb._inbox_store_getter = None
    assert service.discovery_paused(None) is False
    assert service.set_discovery_paused(True) is False


def test_discovery_pause_endpoints_and_viewer_403(_env):
    client, sess = _client()
    r = client.get("/api/goals/discovery-pause")
    assert r.status_code == 200 and r.json() == {"paused": False}
    r = client.post("/api/goals/discovery-pause", json={"paused": True})
    assert r.status_code == 200 and r.json()["paused"] is True
    assert _env.kv.get(service.DISCOVERY_PAUSE_KEY) == "1"
    # for-conversation 带开关现状（有目标 / 无目标两条路都带）
    r = client.get("/api/goals/for-conversation?conversation_id=" + CONV)
    assert r.status_code == 200 and r.json()["discovery_paused"] is True
    gs = get_goal_store(":memory:")
    _goal(gs)
    r = client.get("/api/goals/for-conversation?conversation_id=" + CONV)
    assert r.status_code == 200 and r.json()["discovery_paused"] is True and r.json()["goal"]
    sess["role"] = "viewer"
    assert client.post("/api/goals/discovery-pause", json={"paused": False}).status_code == 403
    assert client.post("/api/goals/profile/confirm",
                       json={"conversation_id": CONV, "slot": "occupation", "value": "x"}).status_code == 403


def test_slots_progress_state_and_confirm_flow(_env):
    inbox = _env
    gs = get_goal_store(":memory:")
    _goal(gs)
    # 客户说过职业（字段仍空）→ 卡上 occupation=mentioned；age 从没聊 → unknown
    inbox.add("out", "how was your day?", T0 - 700)
    inbox.add("in", "long one. i'm a union carpenter, worked the convention center", T0 - 600)
    client, _ = _client()
    r = client.get("/api/goals/for-conversation?conversation_id=" + CONV)
    assert r.status_code == 200
    sp = {s["key"]: s for s in r.json()["goal"]["slots_progress"]}
    assert sp["occupation"]["state"] == "mentioned" and sp["occupation"]["filled"] is False
    assert sp["age"]["state"] == "unknown"
    # 确认（无现值 → 必须带 value；不带 → 400）
    r = client.post("/api/goals/profile/confirm", json={"conversation_id": CONV, "slot": "occupation"})
    assert r.status_code == 400
    r = client.post("/api/goals/profile/confirm",
                    json={"conversation_id": CONV, "slot": "occupation", "value": "union carpenter"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["state"] == "confirmed"
    fields = (gs.get_customer_profile(PLAT, CK) or {}).get("fields") or {}
    assert cell_view(fields.get("occupation")) == ("union carpenter", "agent", "confirmed")
    r = client.get("/api/goals/for-conversation?conversation_id=" + CONV)
    sp = {s["key"]: s for s in r.json()["goal"]["slots_progress"]}
    assert sp["occupation"]["state"] == "confirmed" and sp["occupation"]["filled"] is True
    # 确认后注入链：职业永不再问（硬问句「做什么生意/工作」不得出现，连 deepen 都不出）
    blk, m = _inject(inbox, "my boss wants me on the night shift again", T0)
    assert m.get("injected") and blk
    assert "做什么生意/工作" not in blk and "绝不要再问" not in blk
    # 未知槽位 → 400
    assert client.post("/api/goals/profile/confirm",
                       json={"conversation_id": CONV, "slot": "nope", "value": "x"}).status_code == 400


def test_confirm_promotes_llm_mentioned_cell_same_value(_env):
    """AI 摘录到的值（src=llm → mentioned）点确认：值不变也要翻成 confirmed（同值覆盖被 store
    跳过，故两步写）。"""
    gs = get_goal_store(":memory:")
    gs.upsert_customer_profile(PLAT, CK, {"occupation": "carpenter"}, source="llm")
    fields = (gs.get_customer_profile(PLAT, CK) or {}).get("fields") or {}
    assert cell_view(fields.get("occupation"))[2] == "mentioned"
    res = confirm_profile_slot(gs, PLAT, CK, "occupation")
    assert res["ok"] and res["value"] == "carpenter" and res["state"] == "confirmed"
    fields = (gs.get_customer_profile(PLAT, CK) or {}).get("fields") or {}
    assert cell_view(fields.get("occupation")) == ("carpenter", "agent", "confirmed")
    assert confirm_profile_slot(gs, PLAT, CK, "age") == {"ok": False, "reason": "no_value"}
    assert confirm_profile_slot(gs, PLAT, CK, "zzz")["reason"] == "unknown_slot"


def test_frontend_two_trees_same_and_v_bumped_and_i18n_trilingual():
    a = (_ROOT / "shared" / "copilot" / "components" / "cp-goal.js").read_bytes()
    b = (_ROOT / "desktop" / "renderer" / "shared" / "copilot" / "components" / "cp-goal.js").read_bytes()
    assert a == b, "cp-goal.js 双树必须同文"
    js = a.decode("utf-8")
    for needle in ('data-act="dpause_toggle"', 'data-act="slot_confirm"', "/api/goals/discovery-pause",
                   "/api/goals/profile/confirm", "inbox.goal.slots.mentioned", "_renderDiscoveryBar(d)",
                   'String(s.state || "") === "mentioned"'):
        assert needle in js, needle
    stamps = set()
    for rel in ("shared/copilot/app.html", "desktop/renderer/shared/copilot/app.html",
                "src/web/templates/unified_inbox.html"):
        m = re.search(r"cp-goal\.js\?v=(\w+)", (_ROOT / rel).read_text(encoding="utf-8", errors="replace"))
        assert m, rel
        stamps.add(m.group(1))
    assert stamps == {"20260910a"}, stamps
    from src.web.i18n_packs import goals as g
    hant = (_ROOT / "src" / "web" / "i18n_packs" / "zh_hant_auto.py").read_text(encoding="utf-8")
    for k in ("inbox.goal.slots.mentioned", "inbox.goal.slots.mentioned_t", "inbox.goal.slots.confirm",
              "inbox.goal.slots.confirm_t", "inbox.goal.slots.confirm_prompt", "inbox.goal.slots.confirmed_toast",
              "inbox.goal.dpause.label", "inbox.goal.dpause.on", "inbox.goal.dpause.pause",
              "inbox.goal.dpause.resume", "inbox.goal.dpause.hint", "inbox.goal.dpause.toast_on",
              "inbox.goal.dpause.toast_off", "inbox.goal.skip.exposure_paused", "inbox.goal.skip.goals_paused",
              "inbox.goal.skip.discovery_paused"):
        assert k in g.ZH and k in g.EN, k
        assert f"'{k}'" in hant, f"繁中缺键 {k}"
    assert "{label}" in g.ZH["inbox.goal.slots.mentioned"] and "{label}" in g.EN["inbox.goal.slots.mentioned"]
