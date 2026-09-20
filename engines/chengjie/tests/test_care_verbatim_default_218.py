"""M-1 A（#214 #218，D-M2，2026-09-06）：关怀真发止血门禁。

实录事故：用户在「补一条关怀」填「kim? are you at cebu now?」（要发的话），系统按
「TA 说过的事」交 LLM 二次创作，未注入客户档案（菲律宾本地人、相识一年），生成
「How are you finding life in the Philippines? Is the food there as good as what I cook?」
并 14:03 真发出去。另：启动行 dry_run=True 而派发器已真发（三处各读一份配置）。

覆盖：
- 默认档 verbatim：add 端点缺省 mode → 原文入队、零 LLM；
- AI 润色档：无 ``preview_confirmed`` 不入队（reason=preview_required + 预览稿 + 档案 +
  校验三态）；确认后入队；预览稿与档案矛盾 → check.verdict=block；
- 事实校验 ``detect_profile_contradiction``：事故原句命中 local_as_foreigner；老客当新客
  命中 old_as_new；档案空 / 不矛盾放行；
- 派发器：档案矛盾句被拦（skipped + 原因）；首次真发 / 含地名 / 含人名 → 强制预览
  （note=hold:*，不发，行留 pending，此后 tick 不再拟稿）；「就这样发」经 deliver_text
  发出并 mark_sent；老客非首发无地名人名 → 正常发；未注入 profile_provider 的旧调用方
  行为不变；
- dry_run 单一真值：effective_dry_run 跟实时配置；翻转打 INFO；plan/health/send-now
  同读一个值；
- 静态钉：background_tasks 恒注入 profile_provider；[care-gen] 日志存在。
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.contacts.care_commitment import CareCommitment
from src.contacts.care_dispatcher import CareDispatcher, build_care_prompt
from src.contacts.care_profile import (
    build_customer_profile,
    check_care_reply,
    forced_preview_reason,
    person_name_candidates,
    profile_block,
)
from src.contacts.care_schedule import CareScheduleStore, is_verbatim_care
from src.utils.proactive_fabrication_guard import (
    detect_profile_contradiction,
    normalize_country_code,
    place_mentions,
)

NOW = datetime(2026, 9, 6, 14, 0, 0).timestamp()
INCIDENT = ("How are you finding life in the Philippines? "
            "Is the food there as good as what I cook?")
PH_PROFILE = {"country": "PH", "residence": "PH", "language": "en",
              "known_since": "years", "known_days": 400, "display_name": "Kxhm"}


# ── ① 事实校验纯函数 ─────────────────────────────────────────────────────────
def test_incident_sentence_contradicts_local_profile():
    bad, why = detect_profile_contradiction(INCIDENT, PH_PROFILE)
    assert bad is True and why == "local_as_foreigner"


@pytest.mark.parametrize("text", [
    "Welcome to the Philippines! How's life over there so far?",
    "你到菲律宾了吗？那边的生活习惯了吗？",
    "Have you settled in Cebu yet?",
])
def test_outsider_frame_variants_hit(text):
    assert detect_profile_contradiction(text, PH_PROFILE)[0] is True


@pytest.mark.parametrize("text", [
    "Nice to meet you! What do you do for a living?",
    "很高兴认识你呀，以后多聊～",
])
def test_old_customer_treated_as_new_hits(text):
    bad, why = detect_profile_contradiction(text, {"known_since": "years"})
    assert bad is True and why == "old_as_new"
    bad2, why2 = detect_profile_contradiction(text, {"known_days": 45})
    assert bad2 is True and why2 == "old_as_new"


@pytest.mark.parametrize("text,profile", [
    (INCIDENT, {}),                                        # 档案全空 → 不参与判定
    (INCIDENT, {"country": "US", "known_since": "years"}),  # 客户不是菲律宾人 → 不矛盾
    ("How was the interview today? Fingers crossed!", PH_PROFILE),
    ("Nice to meet you!", {"known_since": "recent"}),       # 新客说初识合法
    ("Nice to meet you!", {"known_days": 3}),
    ("Kim, did you get home safe? It's raining hard in Manila", PH_PROFILE),  # 提到本地地名但非外来者框架
])
def test_no_contradiction_cases(text, profile):
    assert detect_profile_contradiction(text, profile) == (False, "")


def test_normalize_country_and_place_mentions():
    assert normalize_country_code("菲律宾") == "PH"
    assert normalize_country_code("Cebu, Philippines") == "PH"
    assert normalize_country_code("filipino") == "PH"
    assert normalize_country_code("ph") == "PH"
    assert normalize_country_code("Atlantis") == ""
    assert place_mentions(INCIDENT) == ["PH"]
    assert "JP" in place_mentions("下周去东京玩")
    assert place_mentions("How was your day?") == []


# ── ② 强制预览判定 ───────────────────────────────────────────────────────────
def test_forced_preview_reasons():
    assert forced_preview_reason("hi there", {}, first_real_send=True) == "first_send"
    assert forced_preview_reason("Enjoy Bangkok!", {}, first_real_send=False) == "place"
    assert forced_preview_reason("say hi to Kim for me", {}, first_real_send=False) == "person"
    assert forced_preview_reason("How did the interview go? Fingers crossed 🤞",
                                 {}, first_real_send=False) == ""
    # 客户显示名词元出现在文案 → 人名
    assert forced_preview_reason("kxhm are you free tonight?",
                                 {"display_name": "Kxhm"}, first_real_send=False) == "person"


def test_person_candidates_skip_sentence_initial_and_stopwords():
    assert person_name_candidates("Good morning! Take care today.") == []
    assert person_name_candidates("Happy Monday, I hope Netflix has something good") == []
    assert person_name_candidates("Tell Maria I said hi") == ["Maria"]


def test_check_care_reply_priority_block_over_preview():
    v = check_care_reply(INCIDENT, PH_PROFILE, first_real_send=True)
    assert v["verdict"] == "block" and v["reason"].endswith("local_as_foreigner")
    v2 = check_care_reply("Hope the interview went well!", PH_PROFILE, first_real_send=True)
    assert v2 == {"verdict": "preview", "reason": "first_send"}
    v3 = check_care_reply("Hope the interview went well!", PH_PROFILE, first_real_send=False)
    assert v3 == {"verdict": "pass", "reason": ""}


# ── ③ 档案组装 + 注入块 ────────────────────────────────────────────────────────
class _Contact:
    country_hint = "菲律宾"
    language_hint = "en"


class _Contacts:
    def get_contact(self, cid):
        return _Contact() if cid == "c1" else None

    def get_contact_profile(self, cid):
        return {"known_since": "years"} if cid == "c1" else None


class _Inbox:
    def __init__(self, tz_source="stated_city", contact_id="c1"):
        self._src = tz_source
        self._cid = contact_id

    def get_conversation(self, cid):
        return {"conversation_id": cid, "display_name": "Kxhm", "language": "unknown",
                "first_seen": NOW - 400 * 86400, "created_at": NOW - 380 * 86400,
                "contact_id": self._cid}

    def get_conv_meta(self, cid):
        return {"tz_country": "PH", "tz_source": self._src}


def test_build_customer_profile_merges_sources():
    p = build_customer_profile(
        {"contact_key": "telegram:a:1", "platform": "telegram", "account_id": "a",
         "chat_key": "1"}, inbox_store=_Inbox(), contacts_store=_Contacts(), now=NOW)
    assert p["country"] == "PH" and p["residence"] == "PH"
    assert p["language"] == "en"
    assert p["known_since"] == "years" and p["known_days"] == 400
    assert p["display_name"] == "Kxhm"
    blk = profile_block(p)
    assert "菲律宾" in blk and "本地人" in blk and "一年以上" in blk and "英语" in blk


def test_build_customer_profile_ignores_behavior_inferred_country():
    p = build_customer_profile(
        {"contact_key": "telegram:a:1", "platform": "telegram", "account_id": "a",
         "chat_key": "1"}, inbox_store=_Inbox(tz_source="behavior", contact_id=""),
        contacts_store=None, now=NOW)
    assert p["residence"] == "" and p["country"] == ""
    assert p["known_days"] == 400
    assert profile_block({}) == ""


def test_build_customer_profile_never_raises_without_stores():
    p = build_customer_profile({"contact_key": "x"}, inbox_store=None, contacts_store=None)
    assert p["country"] == "" and p["known_days"] == 0


def test_prompt_contains_profile_block_first():
    item = {"topic": "面试", "event_at": NOW, "due_at": NOW, "source_text": "明天面试"}
    p = build_care_prompt(item, context_block="ctx", now=NOW,
                          persona_line="沉稳", profile_block=profile_block(PH_PROFILE))
    assert "【客户档案" in p and "菲律宾" in p
    assert p.index("【客户档案") < p.index("你的说话风格")
    assert "【客户档案" not in build_care_prompt(item, context_block="ctx", now=NOW)


# ── ④ 派发器：拦 / 预览 / 放行 ───────────────────────────────────────────────
class _AI:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    async def chat(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply


def _sender(rec, row_id=88):
    async def _send(channel, account_id, chat_name, reply, defer_until, reason, staleness, extra):
        rec.append({"reply": reply, "reason": reason, "extra": extra})
        return row_id
    return _send


def _event_store(topic="面试", source="明天面试", contact="tg:kx"):
    s = CareScheduleStore(":memory:")
    c = CareCommitment(due_at=NOW - 60, event_at=NOW - 60, topic=topic,
                       sentiment="neutral", anchor_text="x", source_text=source, confidence=1.0)
    rid = s.add_commitment(c, contact_key=contact, platform="telegram",
                           account_id="default", chat_key="6088992099")
    return s, rid


def _disp(store, ai, rec, profile=None, **kw):
    return CareDispatcher(
        store=store, ai_client=ai, send_callback=_sender(rec),
        context_provider=lambda ck: "ctx",
        profile_provider=(lambda it: dict(profile or {})), **kw)


async def test_dispatch_blocks_profile_contradiction():
    s, rid = _event_store()
    rec, ai = [], _AI(INCIDENT)
    n = await _disp(s, ai, rec, PH_PROFILE).run_once(now=NOW)
    assert n == 0 and rec == []
    it = s.get(rid)
    assert it["status"] == "skipped"
    assert it["note"] == "profile_contradiction:local_as_foreigner"
    assert "【客户档案" in ai.prompts[0] and "菲律宾" in ai.prompts[0]  # 档案确已注入


async def test_dispatch_first_real_send_is_held_then_confirmed_by_operator():
    s, rid = _event_store()
    rec, ai = [], _AI("How did the interview go? Fingers crossed!")
    d = _disp(s, ai, rec, PH_PROFILE)
    assert await d.run_once(now=NOW) == 0
    assert rec == []
    it = s.get(rid)
    assert it["status"] == "pending"
    assert it["note"] == "hold:first_send"
    assert it["sent_text"] == "How did the interview go? Fingers crossed!"
    assert CareScheduleStore.hold_reason(it) == "first_send"
    # 下一 tick 不再拟稿、不发
    n_prompts = len(ai.prompts)
    assert await d.run_once(now=NOW + 600) == 0
    assert len(ai.prompts) == n_prompts and rec == []
    # 运营「就这样发」→ deliver_text 送队列 + mark_sent
    res = await d.deliver_text(s.get(rid), it["sent_text"], now=NOW + 700)
    assert res["ok"] is True
    assert rec[-1]["reply"] == "How did the interview go? Fingers crossed!"
    assert s.get(rid)["status"] == "sent"
    assert str(s.get(rid)["note"]).startswith("manual:deferred:")


async def test_dispatch_place_or_person_forces_preview_even_for_old_customer():
    for reply, why in (("Enjoy your weekend in Manila!", "place"),
                       ("Tell Maria I said hi~", "person")):
        s, rid = _event_store()
        s.add_verbatim(contact_key="tg:kx", due_at=NOW - 7200, text="早上好",
                       platform="telegram", account_id="default", chat_key="6088992099")
        s.mark_sent(2, note="deferred:1", sent_text="早上好")   # 非首次真发
        assert s.has_real_sent("tg:kx") is True
        rec, ai = [], _AI(reply)
        assert await _disp(s, ai, rec, PH_PROFILE).run_once(now=NOW) == 0
        assert rec == [] and s.get(rid)["note"] == f"hold:{why}"


async def test_dispatch_plain_reply_to_known_customer_sends():
    s, rid = _event_store()
    s.add_verbatim(contact_key="tg:kx", due_at=NOW - 7200, text="早上好",
                   platform="telegram", account_id="default", chat_key="6088992099")
    s.mark_sent(2, note="deferred:1", sent_text="早上好")
    rec, ai = [], _AI("How did the interview go today? Fingers crossed!")
    assert await _disp(s, ai, rec, PH_PROFILE).run_once(now=NOW) == 1
    assert rec and rec[0]["reply"].startswith("How did the interview")
    assert s.get(rid)["status"] == "sent"


async def test_dispatch_without_profile_provider_keeps_legacy_behavior():
    """旧调用方 / 未注入档案：闸不启用（生产 background_tasks 恒注入，见静态钉）。"""
    s, rid = _event_store()
    rec, ai = [], _AI(INCIDENT)
    d = CareDispatcher(store=s, ai_client=ai, send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx")
    assert await d.run_once(now=NOW) == 1 and len(rec) == 1
    # 显式开闸则拦
    s2, rid2 = _event_store()
    rec2, ai2 = [], _AI(INCIDENT)
    d2 = CareDispatcher(store=s2, ai_client=ai2, send_callback=_sender(rec2),
                        context_provider=lambda ck: "ctx", reply_gates=True)
    assert await d2.run_once(now=NOW) == 0 and rec2 == []
    assert s2.get(rid2)["note"] == "hold:first_send"   # 无档案 → 矛盾判不了，首发仍预览


async def test_dry_run_samples_do_not_hold_or_block():
    s, rid = _event_store()
    rec, ai = [], _AI(INCIDENT)
    d = _disp(s, ai, rec, PH_PROFILE, dry_run=True)
    assert await d.run_once(now=NOW) == 1 and rec == []
    it = s.get(rid)
    assert it["status"] == "pending" and it["dry_sampled_at"] > 0
    assert CareScheduleStore.hold_reason(it) == ""


async def test_verbatim_row_never_touches_llm_or_gates():
    s = CareScheduleStore(":memory:")
    s.add_verbatim(contact_key="tg:kx", due_at=NOW - 60, text="kim? are you at cebu now?",
                   platform="telegram", account_id="default", chat_key="6088992099")
    rec, ai = [], _AI(INCIDENT)
    assert await _disp(s, ai, rec, PH_PROFILE).run_once(now=NOW) == 1
    assert ai.prompts == [] and rec[0]["reply"] == "kim? are you at cebu now?"


async def test_care_gen_log_line_emitted(caplog):
    s, rid = _event_store()
    rec, ai = [], _AI("How did the interview go today?")
    s.add_verbatim(contact_key="tg:kx", due_at=NOW - 7200, text="早上好",
                   platform="telegram", account_id="default", chat_key="6088992099")
    s.mark_sent(2, note="deferred:1", sent_text="早上好")
    with caplog.at_level(logging.INFO, logger="src.contacts.care_dispatcher"):
        await _disp(s, ai, rec, PH_PROFILE).run_once(now=NOW)
    lines = [r.getMessage() for r in caplog.records if "[care-gen]" in r.getMessage()]
    assert any("mode=event" in ln and "profile=" in ln and "reply=" in ln for ln in lines)
    assert any("decision=enqueued" in ln for ln in lines)


# ── ⑤ dry_run 单一真值 ───────────────────────────────────────────────────────
async def test_effective_dry_run_follows_live_cfg_and_logs_flip(caplog):
    s, rid = _event_store()
    live = {"enabled": True, "dry_run": True}
    rec, ai = [], _AI("How did the interview go?")
    s.add_verbatim(contact_key="tg:kx", due_at=NOW - 7200, text="早上好",
                   platform="telegram", account_id="default", chat_key="6088992099")
    s.mark_sent(2, note="deferred:1", sent_text="早上好")
    d = CareDispatcher(store=s, ai_client=ai, send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx", dry_run=False,
                       cfg_provider=lambda: dict(live))
    assert d.effective_dry_run() is True            # 实时值压过构造参数
    assert d.health_snapshot()["dry_run_effective"] is True
    await d.run_once(now=NOW)
    assert rec == []                                # dry：不发
    live["dry_run"] = False
    assert d.effective_dry_run() is False
    with caplog.at_level(logging.INFO, logger="src.contacts.care_dispatcher"):
        await d.run_once(now=NOW + 600)             # 翻真发后不吃 dry 重拟冷却
    assert any("dry_run 实时值变化 True → False" in r.getMessage() for r in caplog.records)
    assert len(rec) == 1                            # 翻转后真发


# ── ⑥ 路由 ──────────────────────────────────────────────────────────────────
class _Disp:
    def __init__(self, store, dry=False):
        self.store = store
        self.dry = dry
        self._interval = 300.0
        self.calls = []

    def effective_dry_run(self):
        return self.dry

    def prompt_extras(self, item):
        return {}

    def customer_profile(self, item):
        return dict(PH_PROFILE)

    def health_snapshot(self):
        return {"running": True, "interval_sec": 300.0, "last_tick_ts": 0.0,
                "last_tick_scheduled": 0, "last_tick_gated": False,
                "dry_run_effective": self.dry}

    async def deliver_text(self, item, text, *, now=None):
        self.calls.append((int(item["id"]), text))
        self.store.mark_sent(int(item["id"]), note="manual:deferred:5", sent_text=text)
        return {"ok": True, "reason": "", "row_id": 5}


class _CM:
    def __init__(self, dry=False):
        self.config = {"companion": {"proactive_care": {"enabled": True, "dry_run": dry}}}
        self.config_path = ""

    def get_ai_config(self):
        return {"ai_name": "小雅"}


def _client(ai_reply=INCIDENT, dry=False, cm_dry=None):
    from src.web.routes.care_routes import register_care_routes

    app = FastAPI()
    store = CareScheduleStore(":memory:")
    app.state.care_schedule_store = store
    app.state.ai_client = _AI(ai_reply)
    app.state.care_engine = {"dispatcher": _Disp(store, dry=dry)}
    app.state.config_manager = _CM(dry if cm_dry is None else cm_dry)

    def _auth(request: Request):
        return True

    register_care_routes(app, api_auth=_auth, config_manager=app.state.config_manager)
    return TestClient(app), app


BASE = {"contact_key": "tg:kx", "platform": "telegram", "account_id": "default",
        "chat_key": "6088992099", "due_in_hours": 2}


def test_route_default_mode_is_verbatim_zero_llm():
    c, app = _client()
    r = c.post("/api/care/schedule", json=dict(BASE, topic="kim? are you at cebu now?")).json()
    assert r["ok"] is True and r["mode"] == "verbatim"
    it = app.state.care_schedule_store.get(r["id"])
    assert is_verbatim_care(it) and it["source_text"] == "kim? are you at cebu now?"
    assert app.state.ai_client.prompts == []


def test_route_event_requires_preview_and_blocks_contradiction():
    c, app = _client(ai_reply=INCIDENT)
    r = c.post("/api/care/schedule", json=dict(BASE, topic="面试", mode="event")).json()
    assert r["ok"] is False and r["reason"] == "preview_required"
    assert r["preview"] == INCIDENT
    assert r["check"]["verdict"] == "block"
    assert r["check"]["reason"].endswith("local_as_foreigner")
    assert r["profile"]["country"] == "PH"
    assert r["understanding"]["mode"] == "event"
    assert app.state.care_schedule_store.count(status="pending") == 0   # 未入队
    assert "【客户档案" in app.state.ai_client.prompts[0]


def test_route_event_confirmed_enqueues():
    c, app = _client(ai_reply="How did the interview go?")
    r = c.post("/api/care/schedule", json=dict(BASE, topic="面试", mode="event")).json()
    assert r["reason"] == "preview_required" and r["check"]["verdict"] == "preview"
    r2 = c.post("/api/care/schedule",
                json=dict(BASE, topic="面试", mode="event", preview_confirmed=True)).json()
    assert r2["ok"] is True and r2["mode"] == "event"
    assert app.state.care_schedule_store.count(status="pending") == 1


def test_route_plan_and_sendnow_expose_hold_and_dry_run():
    c, app = _client(ai_reply="How did the interview go?", dry=True)
    store = app.state.care_schedule_store
    r = c.post("/api/care/schedule",
               json=dict(BASE, topic="面试", mode="event", preview_confirmed=True)).json()
    assert r["ok"] is True
    app.state.ai_client.prompts.clear()
    sid = store.list_pending()[0]["id"]
    store.mark_hold_for_preview(sid, sent_text="Hope the interview went well!",
                                reason="first_send")
    plan = c.get("/api/care/plan").json()
    assert plan["engine"]["dry_run"] is True and plan["engine"]["interval_sec"] == 300.0
    items = [it for g in plan["groups"] for it in g["items"]]
    assert items[0]["hold_reason"] == "first_send"
    assert items[0]["hold_text"] == "Hope the interview went well!"
    assert c.get("/api/care/health").json()["dry_run"] is True
    # N-1 A（#243）：「立即发」改同步直投——待确认行不发、结构化回 decision=held（要点「就这样发」）
    sn = c.post(f"/api/care/schedule/{sid}/send-now", json={}).json()
    assert sn["ok"] is False and sn["decision"] == "held"
    assert sn["dry_run"] is True and sn["held"] == "first_send"
    # 预览端点：held 行直接回派发器扣下的那稿，零 LLM
    pv = c.post(f"/api/care/schedule/{sid}/preview", json={}).json()
    assert pv["ok"] is True and pv["held"] == "first_send"
    assert pv["preview"] == "Hope the interview went well!"
    assert app.state.ai_client.prompts == []


def test_route_dry_run_truth_prefers_dispatcher_over_config():
    """派发器实时值 False、配置快照 True → 面板跟派发器（同一真值源）。"""
    c, _ = _client(dry=False, cm_dry=True)
    assert c.get("/api/care/health").json()["dry_run"] is False
    assert c.get("/api/care/plan").json()["engine"]["dry_run"] is False


# ── ⑦ 静态钉 ────────────────────────────────────────────────────────────────
def test_background_tasks_injects_profile_provider():
    src = (Path(__file__).resolve().parents[1]
           / "src" / "bootstrap" / "background_tasks.py").read_text(encoding="utf-8")
    assert "profile_provider=_care_profile" in src
    assert "build_customer_profile" in src


def test_care_template_defaults_to_verbatim():
    tpl = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "templates" / "care_schedule.html").read_text(encoding="utf-8")
    assert "var _csKind='verbatim'" in tpl
    assert 'class="cs-chip on" data-kind="verbatim"' in tpl
    assert "preview_required" in tpl and "preview_confirmed" in tpl
    assert "cs7_sendnow_dry_confirm" in tpl
