# -*- coding: utf-8 -*-
"""Q-5（#263）：抽取两链合一 + 昵称解析预填 + 待确认一键确认 + [profile]/[extract] 日志 + schema 迁移。

A ``profile_fill.run_extraction``：一次 LLM 同时出记忆事实（引文接地）与画像槽位候选（摘录接地）；
  任一开关开即走 LLM；事实回调用方、槽位 ``apply(source=ai_inferred)``；``[extract] conv= facts= slots= llm=``。
B ``nickname_parse``：「Michael/46/New york/union carpenter/5.28」→ 年龄 46 / 城市 New York / 职业 union
  carpenter，三项 ``source=nickname status=mentioned``；日期片段忽略；纯单词昵称不拆；昵称变更再触发。
C 确认写 ``status=confirmed source=confirmed``；✕ 只删 mentioned；AI 值与已确认冲突 → 通知中心一条、不覆盖。
D 每次写入 ``[profile] conv= slot= value= source= status=``；连续 50 次 0/0 → ``stall_status().stalled``。
E 裸字符串 / 旧形 cell 读时兼容、写时升级；导出带 schema_version；Q-1 ``cell_view`` 与旧读者同读一格。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from src.companion.goals import profile_fill as pf
from src.companion.goals.profile_slots import cell_view, facts_line, slot_src, slot_value
from src.companion.goals.store import GoalStore, get_goal_store, reset_goal_store
from src.contacts.nickname_parse import parse_nickname

_ROOT = Path(__file__).resolve().parents[1]
PLAT, ACCT, CK = "telegram", "acc1", "u1001"
CONV = f"{PLAT}:{ACCT}:{CK}"
NICK = "Michael/46/New york/union carpenter/5.28"


@pytest.fixture(autouse=True)
def _reset():
    pf.reset_stall()
    pf.bind_app(None)
    reset_goal_store()
    yield
    pf.reset_stall()
    pf.bind_app(None)
    reset_goal_store()


def _fields(st, pf_=PLAT, ck=CK):
    return dict((st.get_customer_profile(pf_, ck) or {}).get("fields") or {})


# ── E：schema ──────────────────────────────────────────────────────────────────

def test_schema_read_old_write_new_and_q1_reader_agrees():
    # 裸字符串 = 人录 confirmed；旧形 llm_pending = AI mentioned；旧形 agent = confirmed
    assert cell_view(pf.normalize_cell("Tom")) == ("Tom", "user", "confirmed")
    c = pf.normalize_cell({"v": "30", "src": "llm_pending", "ts": 5.0})
    assert cell_view(c) == ("30", "ai_inferred", "mentioned") and c["ts"] == 5.0 and c["src"] == "llm_pending"
    assert cell_view(pf.normalize_cell({"v": "NYC", "src": "agent", "ts": 1}))[1:] == ("user", "confirmed")
    assert pf.normalize_cell("") is None and pf.normalize_cell({"v": ""}) is None and pf.normalize_cell(True) is None
    # 新形单元同时带旧读者键：slot_value / slot_src / facts_line 与 cell_view 读同一格
    cell = pf.make_cell("union carpenter", "ai_inferred")
    f = {"occupation": cell}
    assert cell_view(cell) == ("union carpenter", "ai_inferred", "mentioned")
    assert slot_value(f, "occupation") == "union carpenter" and slot_src(f, "occupation") == "llm_pending"
    assert "(待确认)" in facts_line(f)
    conf = pf.make_cell("x", "confirmed")
    assert cell_view(conf)[2] == "confirmed" and conf["src"] == "agent"
    # 红线：make_cell 不给 status 时 AI / 昵称默认 mentioned
    assert pf.make_cell("a", "nickname")["status"] == "mentioned"
    # 导出 / 导入带版本号；v1 裸 / 旧形 → v2
    exp = pf.export_profile({"name": "Tom", "age": {"v": "30", "src": "llm", "ts": 2.0}, "zzz": "keep"})
    assert exp["schema_version"] == pf.SCHEMA_VERSION == 2
    assert exp["fields"]["name"]["status"] == "confirmed" and exp["fields"]["age"]["source"] == "ai_inferred"
    assert exp["fields"]["zzz"] == "keep"
    back = pf.import_profile(exp)
    assert back["age"]["status"] == "mentioned" and pf.import_profile({"name": "Tom"})["name"]["source"] == "user"
    assert pf.import_profile("junk") == {}
    up, n = pf.upgrade_fields({"name": "Tom", "age": pf.make_cell("30", "ai_inferred")})
    assert n == 1 and pf.is_new_schema(up["name"])


def test_store_upsert_profile_cells_merges_deletes_and_validates_keys():
    st = GoalStore(":memory:")
    st.upsert_customer_profile(PLAT, CK, {"name": "Tom"}, source="agent")     # 旧写口的旧形单元
    st.upsert_profile_cells(PLAT, CK, {"age": pf.make_cell("46", "nickname"), "bogus": pf.make_cell("x", "user")})
    f = _fields(st)
    assert set(f) == {"name", "age"} and f["name"]["src"] == "agent" and f["age"]["source"] == "nickname"
    st.upsert_profile_cells(PLAT, CK, {"age": None})
    assert "age" not in _fields(st)
    assert st.upsert_profile_cells("", CK, {"age": pf.make_cell("1", "user")}) is None


# ── B：昵称解析 ────────────────────────────────────────────────────────────────

def test_nickname_parse_acceptance_and_guards():
    assert parse_nickname(NICK) == {"name": "Michael", "age": "46", "location": "New York",
                                    "occupation": "union carpenter"}
    assert parse_nickname("Michael") == {}                       # 纯单词不拆
    assert parse_nickname("Union Carpenter") == {}               # 大写多词 ≠ 城市
    assert parse_nickname("Lisa 1998") == {}                     # 年份 ≠ 年龄
    assert parse_nickname("Mary/05-28/Sydney") == {"name": "Mary", "location": "Sydney"}
    assert parse_nickname("Anna | 29 | Bangkok")["age"] == "29"
    assert parse_nickname("小美｜24｜深圳｜护士") == {"name": "小美", "age": "24", "location": "Shenzhen",
                                                 "occupation": "护士"}
    assert parse_nickname("John, 99, NYC") == {"name": "John", "location": "New York"}   # 99 超 18–80
    assert parse_nickname("") == {} and parse_nickname(None) == {}


def test_nickname_prefill_writes_mentioned_and_retriggers_on_change(caplog):
    st = GoalStore(":memory:")
    caplog.set_level(logging.INFO, logger="src.companion.goals.profile_fill")
    r = pf.nickname_prefill(st, PLAT, CK, NICK, conversation_id=CONV)
    assert set(r["written"]) == {"name", "age", "location", "occupation"}
    f = _fields(st)
    assert cell_view(f["age"]) == ("46", "nickname", "mentioned")
    assert cell_view(f["location"]) == ("New York", "nickname", "mentioned")
    assert cell_view(f["occupation"]) == ("union carpenter", "nickname", "mentioned")
    logs = [m for m in caplog.messages if m.startswith("[profile] ")]
    assert any(re.match(r"\[profile\] conv=\S+ slot=age value=46 source=nickname status=mentioned", m) for m in logs)
    # 同昵称第二次：memo 短路，不再写
    assert pf.nickname_prefill(st, PLAT, CK, NICK, conversation_id=CONV)["skipped"] == {"*": "memo"}
    # 昵称变更 → 同来源刷新（46 → 47），已确认的槽不动
    pf.confirm(st, PLAT, CK, "occupation")
    r2 = pf.nickname_prefill(st, PLAT, CK, "Michael/47/New york/plumber", conversation_id=CONV)
    f = _fields(st)
    assert "age" in r2["written"] and cell_view(f["age"])[0] == "47"
    assert cell_view(f["occupation"]) == ("union carpenter", "confirmed", "confirmed")
    assert r2["conflicts"] and r2["conflicts"][0]["slot"] == "occupation"
    # 昵称 == chat_key（平台没给名）→ 不解析
    assert pf.nickname_prefill(st, PLAT, "777", "777")["written"] == []


# ── C：apply 规则 / 确认 / 否 / 冲突通知 ─────────────────────────────────────────

def test_apply_never_promotes_ai_and_never_overwrites_confirmed_conflict_notifies():
    st = GoalStore(":memory:")
    app = SimpleNamespace(state=SimpleNamespace())
    pf.bind_app(app)
    # 试图让 AI 值直接 confirmed → 被压回 mentioned
    r = pf.apply(st, PLAT, CK, {"occupation": {"value": "nurse", "evidence": "I am a nurse"}},
                 source="ai_inferred", status="confirmed", conversation_id=CONV)
    assert r["written"] == ["occupation"]
    assert cell_view(_fields(st)["occupation"]) == ("nurse", "ai_inferred", "mentioned")
    assert _fields(st)["occupation"]["evidence"] == "I am a nurse"
    # 昵称（更弱）不覆盖 AI 推断；AI 覆盖昵称
    assert pf.apply(st, PLAT, CK, {"occupation": "carpenter"}, source="nickname")["skipped"] == {"occupation": "weaker_source"}
    pf.apply(st, PLAT, CK, {"age": "46"}, source="nickname")
    assert pf.apply(st, PLAT, CK, {"age": {"value": "47", "evidence": "I'm 47"}}, source="ai_inferred")["written"] == ["age"]
    assert cell_view(_fields(st)["age"]) == ("47", "ai_inferred", "mentioned")
    # 同值 → same；坐席确认 → confirmed/confirmed
    assert pf.apply(st, PLAT, CK, {"age": "47"}, source="ai_inferred")["skipped"] == {"age": "same"}
    c = pf.confirm(st, PLAT, CK, "occupation", conversation_id=CONV)
    assert c["ok"] and c["state"] == "confirmed" and c["source"] == "confirmed"
    assert cell_view(_fields(st)["occupation"]) == ("nurse", "confirmed", "confirmed")
    # 已确认 + AI 推断不同值 → 不覆盖 + 通知中心一条（sys_status，按 id 合并）
    r = pf.apply(st, PLAT, CK, {"occupation": {"value": "teacher", "evidence": "I teach"}},
                 source="ai_inferred", conversation_id=CONV)
    assert r["written"] == [] and r["skipped"] == {"occupation": "conflict"}
    assert r["conflicts"] == [{"slot": "occupation", "confirmed": "nurse", "candidate": "teacher"}]
    assert cell_view(_fields(st)["occupation"])[0] == "nurse"
    nq = app.state.notif_queue
    assert len(nq) == 1 and nq[0]["type"] == "sys_status"
    assert nq[0]["data"]["id"] == f"profile_conflict:{PLAT}:{CK}:occupation"
    assert "nurse" in nq[0]["data"]["text"] and "teacher" in nq[0]["data"]["text"]
    pf.apply(st, PLAT, CK, {"occupation": "driver"}, source="ai_inferred")
    assert len(app.state.notif_queue) == 1 and "driver" in app.state.notif_queue[0]["data"]["text"]
    # 已确认同值 → already_confirmed（不通知）
    assert pf.apply(st, PLAT, CK, {"occupation": "Nurse"}, source="ai_inferred")["skipped"] == {"occupation": "already_confirmed"}
    # ✕ 否：mentioned 删掉回 unknown；confirmed 不动
    rj = pf.reject(st, PLAT, CK, "age", conversation_id=CONV)
    assert rj["ok"] and rj["state"] == "unknown" and rj["rejected"] == "47" and "age" not in _fields(st)
    assert pf.reject(st, PLAT, CK, "occupation")["reason"] == "confirmed"
    assert pf.reject(st, PLAT, CK, "zzz")["reason"] == "unknown_slot"
    # ✎ 改：带值确认
    assert pf.confirm(st, PLAT, CK, "age", value="48")["value"] == "48"
    assert pf.confirm(st, PLAT, CK, "interests") == {"ok": False, "reason": "no_value"}
    # 未知槽 / 空候选 / 坏来源
    assert pf.apply(st, PLAT, CK, {"nope": "x", "age": ""}, source="ai_inferred")["skipped"] == {"nope": "unknown_slot", "age": "empty"}
    assert pf.apply(st, PLAT, CK, {"age": "1"}, source="martian") == {"written": [], "skipped": {}, "conflicts": []}


# ── A：两链合一抽取 ───────────────────────────────────────────────────────────

class _AI:
    def __init__(self, raw):
        self.raw, self.prompts = raw, []

    async def chat(self, prompt):
        self.prompts.append(prompt)
        return self.raw


def _cfg(profile_llm=False, use_llm=None):
    mem = {"extract": {}} if use_llm is None else {"extract": {"use_llm": use_llm}}
    return {"companion": {"goals": {"enabled": True, "db_path": ":memory:",
                                    "profile_llm": {"enabled": profile_llm}}}, "memory": mem}


def test_llm_enabled_any_key_and_prompt_has_both_sections():
    assert pf.llm_extract_enabled(_cfg(True, False)) == (True, "profile_llm")
    assert pf.llm_extract_enabled(_cfg(False, True)) == (True, "memory_use_llm")
    assert pf.llm_extract_enabled(_cfg(False, False)) == (False, "")
    # 缺 use_llm 键按开（与 skill_manager 旧判据一致，不回退）
    assert pf.llm_extract_enabled(_cfg(False, None))[0] is True
    assert pf.llm_extract_enabled({}, {"extract": {"use_llm": False}}) == (False, "")
    p = pf.build_merged_prompt("I'm a union carpenter", "nice", pf.slot_table(_cfg()))
    assert "facts" in p and "slots" in p and "occupation" in p and "evidence" in p and "USER:" in p
    assert "ai_client.py" not in p


def test_parse_merged_tolerant_and_ground_slots():
    raw = ('```json\n{"facts":[{"fact":"客户是木工","evidence":"I am a union carpenter","confidence":0.9},'
           '"客户有狗", {"nofact": 1}],"slots":{"occupation":{"value":"union carpenter","evidence":"I am a union carpenter"},'
           '"age":"46","bogus":"x","need":{"value":""}}}\n```')
    facts, slots = pf.parse_merged(raw)
    assert [f["fact"] for f in facts] == ["客户是木工", "客户有狗"] and facts[0]["confidence"] == 0.9
    assert slots == {"occupation": {"value": "union carpenter", "evidence": "I am a union carpenter"},
                     "age": {"value": "46", "evidence": ""}}
    assert pf.parse_merged("not json") == ([], {}) and pf.parse_merged("") == ([], {})
    kept, dropped = pf.ground_slots("I am a union carpenter in NYC, 46 years old",
                                    {"occupation": {"value": "union carpenter", "evidence": "I am a union carpenter"},
                                     "age": {"value": "46", "evidence": "46 years old"},
                                     "location": {"value": "Tokyo", "evidence": ""},
                                     "interests": {"value": "hiking", "evidence": "I love hiking"}})
    assert set(kept) == {"occupation", "age"}
    assert dropped["location"] == "fact_unanchored" and dropped["interests"] in ("fact_unanchored", "evidence_mismatch")


def test_run_extraction_one_call_dispatches_facts_and_slots_and_logs(caplog):
    caplog.set_level(logging.INFO)
    gs = get_goal_store(":memory:")
    raw = json.dumps({"facts": [{"fact": "客户是工会木工", "evidence": "I'm a union carpenter", "confidence": 0.92},
                                {"fact": "客户住东京", "evidence": "I live in Tokyo", "confidence": 0.9}],
                      "slots": {"occupation": {"value": "union carpenter", "evidence": "I'm a union carpenter"},
                                "location": {"value": "Tokyo", "evidence": "Tokyo"}}}, ensure_ascii=False)
    ai = _AI(raw)
    inbox = SimpleNamespace(get_conversation=lambda cid: {"conversation_id": cid, "display_name": NICK, "chat_key": CK})
    res = asyncio.run(pf.run_extraction(
        ai, _cfg(profile_llm=True, use_llm=False), None, user_msg="Long day. I'm a union carpenter, 46, in NYC",
        reply="that sounds tiring", platform=PLAT, chat_key=CK, account_id=ACCT, inbox_store=inbox, goal_store=gs,
        heuristic_facts=1))
    assert len(ai.prompts) == 1, "两链合一：只许一次 LLM 调用"
    assert res["llm"] == 1 and res["enabled_by"] == "profile_llm"
    # 事实：接地留 1 丢 1（东京只在 LLM 嘴里）
    assert [f[0] for f in res["facts"]] == ["客户是工会木工"] and res["facts"][0][2] == 0.92
    assert res["dropped"] and res["dropped"][0]["fact"] == "客户住东京"
    # 槽位：昵称预填先填了 name/age/location(New York)/occupation；LLM 同值 occupation 过接地 → 来源升
    # ai_inferred（带 evidence）仍 mentioned；Tokyo 被接地丢掉（location 保持昵称值）
    f = _fields(gs)
    assert cell_view(f["occupation"]) == ("union carpenter", "ai_inferred", "mentioned")
    assert f["occupation"]["evidence"] == "I'm a union carpenter"
    assert cell_view(f["location"]) == ("New York", "nickname", "mentioned")
    assert cell_view(f["age"]) == ("46", "nickname", "mentioned")
    assert res["slots_written"] == 1 and res["nickname"]["written"]
    line = [m for m in caplog.messages if m.startswith("[extract] ")]
    assert line == [f"[extract] conv={CONV} facts=2 slots=1 llm=1"], line
    assert pf.stall_status()["zero_streak"] == 0
    # 开关全关 / 无 ai_client → llm=0，仍记账、仍昵称预填（memo 已命中）
    res0 = asyncio.run(pf.run_extraction(None, _cfg(False, False), None, user_msg="hi there", reply="hey",
                                         platform=PLAT, chat_key=CK, account_id=ACCT, inbox_store=inbox, goal_store=gs))
    assert res0["llm"] == 0 and res0["facts"] == [] and res0["slots_written"] == 0
    assert caplog.messages[-1] == f"[extract] conv={CONV} facts=0 slots=0 llm=0"
    # 坏输出 / 超时 → 空、不抛
    res_bad = asyncio.run(pf.extract_facts_and_slots(_AI("garbage"), "I am a nurse", "ok", slots=pf.slot_table()))
    assert res_bad["llm"] == 1 and res_bad["facts"] == [] and res_bad["slots"] == {}

    class _Slow:
        async def chat(self, p):
            await asyncio.sleep(0.2)
            return "{}"
    r_to = asyncio.run(pf.extract_facts_and_slots(_Slow(), "I am a nurse", "ok", timeout=0.01))
    assert r_to == {"facts": [], "dropped": [], "slots": {}, "slots_dropped": {}, "candidates": 0, "llm": 0}


# ── D：停滞计数 ───────────────────────────────────────────────────────────────

def test_stall_after_50_consecutive_zero_runs_and_reset_on_hit():
    for _ in range(49):
        pf.record_extract_result(0, 0, llm=1)
    assert pf.stall_status()["stalled"] is False and pf.stall_status()["zero_streak"] == 49
    pf.record_extract_result(0, 0)
    s = pf.stall_status()
    assert s["stalled"] is True and s["threshold"] == 50 == pf.STALL_THRESHOLD and s["runs"] == 50
    pf.record_extract_result(0, 1)
    assert pf.stall_status() == {**pf.stall_status(), "zero_streak": 0, "stalled": False}


# ── skill_manager 源码钉桩：单一合并调用 + 不再直连 extract_memory_facts ────────────

def test_skill_manager_extract_chain_uses_profile_fill_once():
    src = (_ROOT / "src" / "skills" / "skill_manager.py").read_text(encoding="utf-8", errors="ignore")
    i = src.index("async def _episodic_memory_extract_async(")
    body = src[i:src.index("def episodic_list_for_admin", i)]
    assert body.count("_pf.run_extraction(") == 1
    assert "_pf.llm_extract_enabled(" in body
    assert 'getattr(self.ai_client, "extract_memory_facts"' not in body
    assert ".extract_memory_facts(" not in body and ".extract_memory_bullets(" not in body
    assert "heuristic_facts=n_heuristic" in body
    # 事实仍按既有路径落库（ai_inferred）；老计数行保留
    assert 'source="ai_inferred"' in body and "heuristic_count=%d llm_count=%d" in body


# ── 路由 / 页面 / i18n ───────────────────────────────────────────────────────────

class _Inbox:
    def __init__(self):
        self.msgs, self.kv = [], {}

    def list_recent_messages(self, conv, limit=30):
        return list(self.msgs)[-limit:]

    def get_conversation(self, conv):
        return {"conversation_id": conv, "display_name": NICK, "chat_key": CK, "last_ts": 0}

    def get_conv_meta(self, conv):
        return {}

    def get_automation_mode(self, conv):
        return "auto_ai"

    def get_app_setting(self, key, default=""):
        return self.kv.get(key, default)

    def set_app_setting(self, key, value, updated_by=""):
        self.kv[key] = value
        return True


@pytest.fixture
def _web(monkeypatch):
    import src.integrations.protocol_bridge as pb
    from src.companion.goals import service
    from src.web.routes.goal_routes import register_goal_routes
    inbox = _Inbox()
    monkeypatch.setattr(pb, "_inbox_store_getter", lambda: inbox)
    service._inject_log_seen.clear()
    sess = {"role": "", "user": "tester"}
    app = FastAPI()

    def auth_dep(request: Request) -> None:
        request.scope["session"] = dict(sess)

    cfg = SimpleNamespace(config={"companion": {"goals": {"enabled": True, "db_path": ":memory:"}}}, config_path=None)
    register_goal_routes(app, auth_dep, cfg)
    return TestClient(app), sess, inbox


def test_routes_nickname_prefill_on_read_badge_source_confirm_reject_and_stall(_web):
    client, sess, inbox = _web
    gs = get_goal_store(":memory:")
    gs.create_goal(conversation_id=CONV, platform=PLAT, account_id=ACCT, chat_key=CK, template="profile_discovery",
                   autonomy="auto", deadline_days=3, params={"slots": "occupation,age,location,interests"})
    r = client.get("/api/goals/for-conversation?conversation_id=" + CONV)
    assert r.status_code == 200
    sp = {s["key"]: s for s in r.json()["goal"]["slots_progress"]}
    # 读卡即昵称预填：三项 mentioned + source=nickname；兴趣 unknown
    for k, v in (("age", "46"), ("location", "New York"), ("occupation", "union carpenter")):
        assert sp[k]["state"] == "mentioned" and sp[k]["source"] == "nickname" and sp[k]["value"] == v, sp[k]
    assert sp["interests"]["state"] == "unknown" and sp["interests"]["source"] == ""
    assert "extract_stall" in r.json()["goal"]["capture"] and r.json()["goal"]["capture"]["extract_stall"]["stalled"] is False
    # 画像视图也带 state/source
    p = client.get(f"/api/goals/profile?platform={PLAT}&chat_key={CK}")
    assert p.status_code == 200
    rows = {s["key"]: s for s in p.json()["slots"]}
    assert rows["age"]["state"] == "mentioned" and rows["age"]["source"] == "nickname" and rows["age"]["src"] == "llm_pending"
    # ✓ 确认 → source=confirmed；✕ 否 → 清掉；已确认再否 → ok False reason=confirmed
    r = client.post("/api/goals/profile/confirm", json={"conversation_id": CONV, "slot": "occupation"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["source"] == "confirmed"
    assert cell_view(_fields(gs)["occupation"]) == ("union carpenter", "confirmed", "confirmed")
    r = client.post("/api/goals/profile/reject", json={"conversation_id": CONV, "slot": "age"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["rejected"] == "46" and "age" not in _fields(gs)
    r = client.post("/api/goals/profile/reject", json={"conversation_id": CONV, "slot": "occupation"})
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["reason"] == "confirmed"
    assert client.post("/api/goals/profile/reject", json={"conversation_id": CONV, "slot": "zzz"}).status_code == 400
    assert client.post("/api/goals/profile/reject", json={"slot": "age"}).status_code == 400
    sess["role"] = "viewer"
    assert client.post("/api/goals/profile/reject", json={"conversation_id": CONV, "slot": "age"}).status_code == 403
    sess["role"] = ""
    # D：停滞 → 目标报表 + 卡片黄条数据源
    for _ in range(50):
        pf.record_extract_result(0, 0)
    r = client.get("/api/goals/report/accounts?days=7")
    assert r.status_code == 200 and r.json()["extract_stall"]["stalled"] is True
    r = client.get("/api/goals/for-conversation?conversation_id=" + CONV)
    assert r.json()["goal"]["capture"]["extract_stall"]["stalled"] is True


def test_memory_growth_status_carries_extract_stall_and_frontend_wiring():
    from src.web.routes import episodic_identity_routes as eir
    g = eir.memory_growth_status({"since_install": 3}, 10)
    # 路由层在 growth 上挂 extract_stall（与 summary 端点同源函数）
    g["extract_stall"] = pf.stall_status()
    assert g["extract_stall"]["stalled"] is False
    src = (_ROOT / "src" / "web" / "routes" / "episodic_identity_routes.py").read_text(encoding="utf-8")
    assert 'out["growth"]["extract_stall"] = stall_status()' in src and "_pf_bind_app(app)" in src
    html = (_ROOT / "src" / "web" / "templates" / "episodic_memory.html").read_text(encoding="utf-8")
    assert "g.extract_stall" in html and "em_extract_stalled" in html and "em_extract_stalled_link" in html
    rpt = (_ROOT / "src" / "web" / "templates" / "goal_report.html").read_text(encoding="utf-8")
    assert 'id="gr-extract-stall"' in rpt and "d.extract_stall" in rpt and "goal_rpt_extract_stall" in rpt
    js = (_ROOT / "shared" / "copilot" / "components" / "cp-goal.js").read_text(encoding="utf-8")
    assert js == (_ROOT / "desktop" / "renderer" / "shared" / "copilot" / "components" / "cp-goal.js").read_text(encoding="utf-8")
    for needle in ('data-act="slot_reject"', 'data-act="slot_edit_confirm"', "/api/goals/profile/reject",
                   '"ai_badge"', '"nick_badge"', '"inbox.goal.slots." + badgeKey', "cap.extract_stall",
                   "inbox.goal.capture.extract_stall", 'act === "slot_reject"', 'act === "slot_edit_confirm"',
                   "_slotReject(el)", "_slotEditConfirm(el)", "gl-slot-badge"):
        assert needle in js, needle
    from src.web.i18n_packs import episodic_page as ep
    from src.web.i18n_packs import goals as gp
    for k in ("inbox.goal.slots.ai_badge", "inbox.goal.slots.ai_badge_t", "inbox.goal.slots.nick_badge",
              "inbox.goal.slots.nick_badge_t", "inbox.goal.slots.edit_t", "inbox.goal.slots.reject_t",
              "inbox.goal.slots.rejected_toast", "inbox.goal.profile.conflict_notice",
              "inbox.goal.profile.conflict_notice_nick", "inbox.goal.capture.extract_stall", "goal_rpt_extract_stall"):
        assert k in gp.ZH and k in gp.EN, k
    for k in ("em_extract_stalled", "em_extract_stalled_link"):
        assert k in ep.ZH and k in ep.EN, k
    for k in ("inbox.goal.profile.conflict_notice", "inbox.goal.profile.conflict_notice_nick"):
        for lang in (gp.ZH, gp.EN):
            assert "{label}" in lang[k] and "{confirmed}" in lang[k] and "{candidate}" in lang[k]
    for host in ("shared/copilot/app.html", "desktop/renderer/shared/copilot/app.html"):
        assert "cp-goal.js?v=20260910b" in (_ROOT / host).read_text(encoding="utf-8"), host
