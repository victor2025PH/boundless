# -*- coding: utf-8 -*-
"""Q-25（#295 / #300 / #272 追加 · Y39D8U）：事实门二期——画像 / 记忆 / 目标回填同一道门。

Y39D8U 实锤：AI 自己问「inspection work 做多久」，客户只答「Not so long dear been a couple months.」，
画像 occupation 被写成「AI work」（来源=AI 推断）。三条线索：① 锚定用 token 部分匹配（``work`` 撞
``inspection work``）；② 「AI 问 → 客户答」被当回填；③ AI 问句实体成候选。

A ``src/companion/fact_gate.check``：类型尺子 + 来源守卫 + 同条逐字锚定 + 值核心 token 全在 evidence
  （禁部分匹配）+ 语种守卫 + kind=fact 主体守卫；``profile_fill.apply`` 改调它。
B 目标回填：``profile_slots.answer_only_kind`` 命中（时长 / 是 / 数字）→ ``service.py`` 不调 LLM 回填；
  ``profile_llm.run_llm_capture`` 候选过事实门（evidence = 客户原句）。
C 记忆链：``skill_manager`` LLM 事实 ``add_fact`` 前过事实门 + 主体守卫，``[memory] drop reason=``。
D 卡片：无值 mentioned → 「AI 有线索，待补值」不可确认；有值确认必发请求 + toast；空值禁用；取数重试。
E ``tools/profile_purge_unanchored.py --memory --dry-run / --apply``。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.companion import fact_gate as fg
from src.companion.goals import profile_fill as pf
from src.companion.goals import profile_llm as pl
from src.companion.goals import profile_slots as ps
from src.companion.goals.profile_slots import cell_view
from src.companion.goals.store import get_goal_store, reset_goal_store

_ROOT = Path(__file__).resolve().parents[1]
PLAT, ACCT, CK = "telegram", "7543790794", "6963389583"
CONV = f"{PLAT}:{ACCT}:{CK}"

AI_ASK = "How long have you been doing inspection work?"
CUSTOMER_ANSWER = "Not so long dear been a couple months."
INBOUND = [
    "Hey there",
    "I am from Manila, work at the port",
    "Wish I can see you how you look sweetheart.",
    CUSTOMER_ANSWER,
]


@pytest.fixture(autouse=True)
def _reset():
    pf.reset_stall()
    pf.bind_app(None)
    pl.reset_state()
    reset_goal_store()
    yield
    pf.reset_stall()
    pf.bind_app(None)
    pl.reset_state()
    reset_goal_store()


def _fields(st, plat=PLAT, ck=CK):
    return dict((st.get_customer_profile(plat, ck) or {}).get("fields") or {})


# ── A：共享事实门 ──────────────────────────────────────────────────────────────────

def test_gate_blocks_ai_question_entities_from_answer_only_reply():
    """Y39D8U 核心：客户只答「couple months」，AI 问句里的 inspection / AI 永不成值。"""
    assert fg.check("AI work", slot_or_kind="occupation", evidence=CUSTOMER_ANSWER,
                    inbound_texts=INBOUND) == (False, "unanchored")
    assert fg.check("inspection work", slot_or_kind="occupation", evidence=CUSTOMER_ANSWER,
                    inbound_texts=INBOUND) == (False, "unanchored")
    # evidence 是 AI 自己的问句（不在客户入站里）→ 同样丢
    assert fg.check("inspection work", slot_or_kind="occupation", evidence=AI_ASK,
                    inbound_texts=INBOUND) == (False, "unanchored")
    # 客户真说过的才过
    assert fg.check("port", slot_or_kind="occupation", evidence="work at the port",
                    inbound_texts=INBOUND) == (True, "")


def test_gate_token_partial_match_not_allowed():
    """禁部分匹配：值的**每个**核心 token 都要整词在 evidence 里。"""
    assert fg.value_anchored_in_evidence("AI work", "inspection work") is False, "ai 缺席"
    assert fg.value_anchored_in_evidence("work", "network engineer") is False, "非整词"
    assert fg.value_anchored_in_evidence("work", "I work at the port") is True
    assert fg.value_anchored_in_evidence("union carpenter", "I'm a union carpenter") is True
    assert fg.value_anchored_in_evidence("carpenter", "carpenters here") is True, "复数词尾放行"
    assert fg.check("work", slot_or_kind="occupation", evidence="my network is slow",
                    inbound_texts=["my network is slow"]) == (False, "unanchored")
    # 汉字：整段子串 / 摘录省字放行；拆字重组不放
    assert fg.value_anchored_in_evidence("護士", "我係護士") is True
    assert fg.value_anchored_in_evidence("客服回不过来", "我们客服都回不过来，做民宿代运营的") is True
    assert fg.value_anchored_in_evidence("老师", "老板和师傅") is False
    assert fg.value_anchored_in_evidence("醫生", "我係護士") is False


def test_gate_evidence_must_be_one_inbound_sentence_no_splice():
    inbound = ["I live in Manila", "I am a nurse"]
    assert fg.evidence_in_inbound("I am a nurse", inbound) is True
    assert fg.evidence_in_inbound("i am a NURSE!", inbound) is True, "归一后逐字"
    assert fg.evidence_in_inbound("Manila nurse", inbound) is False, "禁跨句拼接"
    assert fg.evidence_in_inbound("", inbound) is False
    assert fg.check("nurse", slot_or_kind="occupation", evidence="nurse in Manila",
                    inbound_texts=inbound) == (False, "unanchored")
    assert fg.check("nurse", slot_or_kind="occupation", evidence="", inbound_texts=inbound) == (False, "unanchored")


def test_gate_source_guard_only_customer_inbound():
    rows = [{"direction": "out", "text": "So you are a nurse, right?"},
            {"direction": "in", "text": "yes"}]
    assert fg.inbound_only(rows) == ["yes"]
    assert fg.check("nurse", slot_or_kind="occupation", evidence="you are a nurse",
                    inbound_texts=rows) == (False, "unanchored"), "出站永不成语料"
    rows.append({"direction": "in", "text": "I am a nurse"})
    assert fg.check("nurse", slot_or_kind="occupation", evidence="I am a nurse",
                    inbound_texts=rows) == (True, "")


def test_gate_type_ruler_and_lang_guard_reuse_q19():
    assert fg.check("織り機の前に座って、新しい帯の色合わせ", slot_or_kind="occupation",
                    evidence="織り機の前に座って、新しい帯の色合わせ",
                    inbound_texts=["織り機の前に座って、新しい帯の色合わせ"]) == (False, "invalid:sentence")
    assert fg.check("12", slot_or_kind="age", evidence="12", inbound_texts=["12"]) == (False, "invalid:age_range")
    assert fg.check("32", slot_or_kind="age", evidence="我今年32歲", inbound_texts=["我今年32歲"],
                    raw_value="32岁") == (True, "")
    canto = ["今日好攰呀", "聽日去嵐山散步", "我係護士，做夜班", "週末想試新配搭", "我今年32歲"]
    ja_occ = "看護師をしています"
    assert fg.check(ja_occ, slot_or_kind="occupation", evidence=f"你講咩？「{ja_occ}」",
                    inbound_texts=canto + [f"你講咩？「{ja_occ}」"]) == (False, "lang_mismatch")
    assert fg.check("nurse", slot_or_kind="occupation", evidence="I am a nurse",
                    inbound_texts=["I am a nurse"], customer_lang="latin") == (True, "")
    assert fg.check("nurse", slot_or_kind="occupation", evidence="「nurse」",
                    inbound_texts=canto + ["「nurse」"], customer_lang="zh") == (False, "lang_mismatch")
    # 枚举槽：改验触发词
    assert fg.check("单身", slot_or_kind="marital_status", evidence="我一直單身",
                    inbound_texts=["我一直單身，冇拖"]) == (True, "")


def test_gate_kind_fact_subject_guard_vietnam():
    """「我出差去越南」绝不能变「一起去越南」。"""
    inbound = ["下周我出差去越南", "hi"]
    assert fg.subject_reason("客户和我一起去越南", "下周我出差去越南") == "subject"
    assert fg.check("客户下周和我们一起去越南", slot_or_kind="fact", evidence="下周我出差去越南",
                    inbound_texts=inbound) == (False, "subject")
    assert fg.check("客户下周要去越南出差", slot_or_kind="fact", evidence="下周我出差去越南",
                    inbound_texts=inbound) == (True, "")
    assert fg.check("Customer will travel to Vietnam with us", slot_or_kind="fact",
                    evidence="I am going to Vietnam next week",
                    inbound_texts=["I am going to Vietnam next week"]) == (False, "subject")
    # 客户自己说的「我们一起」才放行
    assert fg.check("客户和太太一起去越南", slot_or_kind="fact", evidence="我和太太一起去越南",
                    inbound_texts=["我和太太一起去越南"]) == (True, "")
    # 无原句不写；引文不在入站不写；名字 / 数字须在原句
    assert fg.check("客户去越南出差", slot_or_kind="fact", evidence="", inbound_texts=inbound) == (False, "no_evidence")
    assert fg.check("客户去越南出差", slot_or_kind="fact", evidence="我出差去泰国", inbound_texts=inbound) == (False, "unanchored")
    assert fg.check("客户自称Tom", slot_or_kind="fact", evidence="I am Bob", inbound_texts=["I am Bob"]) == (False, "unanchored")
    assert fg.check("客户自称Bob", slot_or_kind="fact", evidence="I am Bob", inbound_texts=["I am Bob"]) == (True, "")


def test_gate_never_raises_and_check_many():
    assert fg.check(None, slot_or_kind="occupation", evidence=None, inbound_texts=None) == (False, "empty")
    assert fg.check(object(), slot_or_kind=None, evidence=123, inbound_texts=[None, 5, {"x": 1}])[0] is False
    got = fg.check_many([("occupation", "AI work", CUSTOMER_ANSWER), ("age", "32", "I am 32")],
                        inbound_texts=INBOUND + ["I am 32"])
    assert [(k, ok, why) for k, _v, ok, why in got] == [("occupation", False, "unanchored"), ("age", True, "")]


def test_apply_goes_through_gate_and_logs_drop(caplog):
    caplog.set_level(logging.INFO)
    st = get_goal_store(":memory:")
    res = pf.apply(st, PLAT, CK, {
        "occupation": {"value": "AI work", "evidence": "inspection work"},
        "location": {"value": "Manila", "evidence": "I am from Manila"},
    }, source="ai_inferred", conversation_id=CONV, recent_inbound=INBOUND)
    assert res["written"] == ["location"]
    assert res["skipped"] == {"occupation": "unanchored"}
    assert "occupation" not in _fields(st)
    assert cell_view(_fields(st)["location"]) == ("Manila", "ai_inferred", "mentioned")
    assert any(m.startswith("[profile] drop ") and "slot=occupation" in m and "value=AI work" in m
               and "reason=unanchored" in m for m in caplog.messages)
    # Q-19 口径不变：anchor_reason 薄封装
    assert pf.anchor_reason("occupation", "AI work", "inspection work", INBOUND + ["inspection work"]) == "unanchored"
    assert pf.anchor_reason("location", "Manila", "I am from Manila", INBOUND) == ""


# ── B：目标回填 ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,kind", [
    (CUSTOMER_ANSWER, "duration"), ("couple months", "duration"), ("a few years now", "duration"),
    ("2 years", "duration"), ("几个月了", "duration"), ("两年多了", "duration"),
    ("yes", "yes_no"), ("Yes honey", "yes_no"), ("no dear", "yes_no"), ("ok", "yes_no"),
    ("是的", "yes_no"), ("好的", "yes_no"), ("嗯", "yes_no"),
    ("3", "number"), ("32", "number"),
    ("hahaha", "filler"),
    ("I am a nurse", ""), ("Manila", ""), ("2 years in Manila", ""), ("我在曼谷", ""), ("我是护士", ""),
    ("I work in inspection", ""), ("30s", ""), ("", ""),
    (CUSTOMER_ANSWER + " I do inspection work at the port", ""),
])
def test_answer_only_kind(text, kind):
    assert ps.answer_only_kind(text) == kind, (text, ps.answer_only_kind(text))


class _AI:
    def __init__(self, raw):
        self.raw, self.prompts = raw, []

    async def chat(self, prompt):
        self.prompts.append(prompt)
        return self.raw


def test_replay_y39d8u_old_llm_track_writes_no_occupation(caplog):
    """回放 Y39D8U（旧 LLM 回填轨 run_llm_capture）：客户「couple months」/ 「I work at the port」，
    LLM 硬给 occupation=AI work → 一律不写；日志 [profile] drop … reason=。"""
    caplog.set_level(logging.INFO)
    st = get_goal_store(":memory:")
    miss = [{"key": "occupation"}, {"key": "location"}]
    n = asyncio.run(pl.run_llm_capture(_AI('{"occupation": "AI work"}'), st, platform=PLAT, chat_key=CK,
                                       text=CUSTOMER_ANSWER, missing=miss))
    assert n == 0 and "occupation" not in _fields(st)
    # 客户历史里有 work 一词（旧接地「任一 token 重叠即过」会放行）→ 事实门整词 + 全 token 拦
    n = asyncio.run(pl.run_llm_capture(_AI('{"occupation": "AI work", "location": "Manila"}'), st,
                                       platform=PLAT, chat_key=CK,
                                       text="I am from Manila, work at the port", missing=miss))
    assert n == 1
    f = _fields(st)
    assert "occupation" not in f and cell_view(f["location"])[0] == "Manila"
    assert any("[profile] drop" in m and "slot=occupation" in m and "value=AI work" in m
               and "reason=unanchored" in m for m in caplog.messages)


def test_replay_y39d8u_merged_extraction_drops_ai_question_entity(caplog):
    """合并抽取链（run_extraction）：LLM 把 AI 问句里的 inspection work 当值、引文却是 AI 的话 →
    unanchored，occupation 不写；客户真说的 Manila 落。"""
    caplog.set_level(logging.INFO)
    gs = get_goal_store(":memory:")
    raw = json.dumps({"facts": [], "slots": {
        "occupation": {"value": "AI work", "evidence": "inspection work"},
        "location": {"value": "Manila", "evidence": "I am from Manila"},
    }})
    hist = [{"direction": "in", "text": t} for t in INBOUND[:-1]] + \
           [{"direction": "out", "text": AI_ASK}, {"direction": "in", "text": CUSTOMER_ANSWER}]
    inbox = SimpleNamespace(get_conversation=lambda cid: None, list_recent_messages=lambda conv, limit=50: hist)
    cfg = {"companion": {"goals": {"enabled": True, "db_path": ":memory:", "profile_llm": {"enabled": True}}},
           "memory": {"extract": {"use_llm": False}}}
    ai = _AI(raw)
    res = asyncio.run(pf.run_extraction(ai, cfg, None, user_msg=CUSTOMER_ANSWER, reply=AI_ASK,
                                        platform=PLAT, chat_key=CK, account_id=ACCT, inbox_store=inbox, goal_store=gs))
    f = _fields(gs)
    assert "occupation" not in f and "location" not in f
    assert res["slots_written"] == 0
    assert res["slots_dropped"].get("occupation") in ("unanchored", "fact_unanchored", "evidence_mismatch")
    # Manila 引自客户早先那条而非本条 → Q-5 出处护栏 evidence_mismatch（本条只有时长，什么都不该落）
    assert res["slots_dropped"].get("location") in ("evidence_mismatch", "fact_unanchored", "unanchored")
    # AI 问句从不进 prompt（Q-19 C）——「inspection」只出现在 AI 的话里
    assert len(ai.prompts) == 1 and "ASSISTANT:" not in ai.prompts[0] and "inspection" not in ai.prompts[0]
    assert CUSTOMER_ANSWER in ai.prompts[0]


def test_service_backfill_spot_uses_answer_only_and_probe_target_untouched():
    src = (_ROOT / "src" / "companion" / "goals" / "service.py").read_text(encoding="utf-8")
    assert "answer_only_kind(inbound_text)" in src
    assert 'reason=answer_only:%s' in src
    # 只动回填一处：answer_only 判定紧贴 schedule_llm_capture 调用，且全文只此一处
    assert src.count("answer_only_kind(") == 1
    i, j = src.index("answer_only_kind(inbound_text)"), src.index("schedule_llm_capture(\n")
    assert 0 < j - i < 1500
    # decide_probe_target / verify_pending_probe 零改动（签名存在且 verify 不调事实门）
    assert "def decide_probe_target(" in src and "def verify_pending_probe(" in src
    vp = src[src.index("def verify_pending_probe("):src.index("def decide_probe_target(")]
    assert "fact_gate" not in vp and "answer_only" not in vp
    # 旧 LLM 轨过事实门（evidence = 客户原句）
    pls = (_ROOT / "src" / "companion" / "goals" / "profile_llm.py").read_text(encoding="utf-8")
    assert "_gate_check(v, slot_or_kind=k, evidence=text, inbound_texts=[text])" in pls


# ── C：记忆链同门 ─────────────────────────────────────────────────────────────────

def test_skill_manager_gates_llm_facts_before_add_fact_once():
    sm = (_ROOT / "src" / "skills" / "skill_manager.py").read_text(encoding="utf-8")
    body = sm[sm.index("async def _episodic_memory_extract_async("):]
    body = body[:body.index("if grounding_dropped:")]
    assert body.count('slot_or_kind="fact"') == 1, "抽取调用一处"
    gate_at = body.index('slot_or_kind="fact"')
    write_at = body.index('key, f, "llm", source="ai_inferred"')
    assert gate_at < write_at, "门在 add_fact 之前"
    assert '"[memory] drop conv=%s fact=%s evidence=%s reason=%s"' in body
    assert "grounding_dropped.append(" in body
    # 启发式 user_stated 事实（客户原话正则）不过门、不改
    assert 'fact, "heuristic", source="user_stated"' in body
    assert body.index('fact, "heuristic", source="user_stated"') < gate_at


def test_memory_gate_semantics_on_real_shapes():
    """skill_manager 传的形状：evidence=LLM 引文、inbound_texts=[客户本条原句]。"""
    mu = "下周我出差去越南，可能两周"
    ok, why = fg.check("客户下周出差去越南两周", slot_or_kind="fact", evidence="下周我出差去越南", inbound_texts=[mu])
    assert (ok, why) == (True, "")
    assert fg.check("客户和我一起去越南", slot_or_kind="fact", evidence="下周我出差去越南", inbound_texts=[mu]) == (False, "subject")
    assert fg.check("客户去越南", slot_or_kind="fact", evidence="", inbound_texts=[mu]) == (False, "no_evidence")
    assert fg.check("客户去越南", slot_or_kind="fact", evidence="我明天去越南", inbound_texts=[mu]) == (False, "unanchored")


# ── D：#295 卡片 ──────────────────────────────────────────────────────────────────

def test_cp_goal_clue_only_not_confirmable_and_confirm_sends_request():
    js_path = _ROOT / "shared" / "copilot" / "components" / "cp-goal.js"
    mirror = _ROOT / "desktop" / "renderer" / "shared" / "copilot" / "components" / "cp-goal.js"
    assert js_path.read_bytes() == mirror.read_bytes(), "双树必须字节一致"
    js = js_path.read_text(encoding="utf-8")
    # 无值 mentioned：显「AI 有线索，待补值」，确认钮 disabled + title，不显「已提及（未确认）」
    render = js[js.index("_renderSlotsProgress(g) {"):js.index("const pendingTxt = pendingN")]
    clue = render[render.index("if (!s.value) {"):render.index("const head = (badgeKey && s.value)")]
    assert 'inbox.goal.slots.clue_only' in clue and 'data-act="slot_confirm" disabled' in clue
    assert 'inbox.goal.slots.clue_only_confirm_t' in clue and 'inbox.goal.slots.mentioned"' not in clue
    assert 'data-act="slot_edit_confirm"' in clue, "无值行只留 ✎ 录值"
    # 有值确认：必发请求 + toast + 行内 confirmed + 失败红字；空值禁用 + title，不再 uiPrompt / 静默 return
    confirm = js[js.index("async _slotConfirm(el) {"):js.index("async _slotEditConfirm(el) {")]
    assert '"/api/goals/profile/confirm"' in confirm and 'method: "POST"' in confirm
    assert "inbox.goal.slots.confirmed_toast" in confirm and "_beacon(\"goal_slot_confirm\")" in confirm
    assert "uiPrompt" not in confirm
    assert "el.disabled = true;" in confirm and "inbox.goal.slots.clue_only_confirm_t" in confirm
    assert "_slotInlineErr(el, " in confirm and "inbox.goal.slots.confirm_failed" in confirm
    assert "setTimeout(rs, 800)" in confirm, "Failed to fetch 自动重试一次"
    assert '"gl-slotchk on src-agent"' in confirm, "成功即刻转 confirmed"
    assert ".gl-slot-err {" in js and ".gl-slot-confirm:disabled {" in js
    # 取数失败：三次尝试 + 带原文的错误行 + 5s 自动重试
    fetch = js[js.index("async fetchData(ctx) {"):js.index("renderData(d) {")]
    assert "attempt < 3" in fetch and "__errMsg" in fetch
    rd = js[js.index("renderData(d) {"):js.index("const g = d.goal || null;")]
    assert "inbox.goal.err_retry_detail" in rd and "_autoRetryTimer" in rd
    from src.web.i18n_packs import goals as gp
    for k in ("inbox.goal.slots.clue_only", "inbox.goal.slots.clue_only_t", "inbox.goal.slots.clue_only_confirm_t",
              "inbox.goal.slots.confirm_failed", "inbox.goal.err_retry_detail"):
        assert k in gp.ZH and k in gp.EN, k
    assert "{label}" in gp.ZH["inbox.goal.slots.clue_only"] and "{err}" in gp.EN["inbox.goal.slots.confirm_failed"]
    for host in ("shared/copilot/app.html", "desktop/renderer/shared/copilot/app.html",
                 "src/web/templates/unified_inbox.html"):
        assert "cp-goal.js?v=20260912a" in (_ROOT / host).read_text(encoding="utf-8"), host
    # goal_routes 只读字段零改动（Q-19 pin 仍在）
    gr = (_ROOT / "src" / "web" / "routes" / "goal_routes.py").read_text(encoding="utf-8")
    assert '"confirmed": str((_states.get(k) or ("", ""))[0] or "") == "confirmed"' in gr


# ── E：清洗二期 --memory ───────────────────────────────────────────────────────────

def _load_purge():
    p = _ROOT / "tools" / "profile_purge_unanchored.py"
    spec = importlib.util.spec_from_file_location("profile_purge_unanchored_q25", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_purge_memory_dry_run_lists_and_apply_deletes_only_ai_inferred(tmp_path, capsys):
    from src.utils.episodic_memory_store import EpisodicMemoryStore
    db = tmp_path / "bot.db"
    st = EpisodicMemoryStore(db)
    key = "telegram:7543790794"
    st.add_fact(key, "客户住在马尼拉", "heuristic", source="user_stated", source_quote="")            # user_stated：一字不动
    st.add_fact(key, "客户做AI巡检工作", "llm", source="ai_inferred", source_quote="")              # no_evidence
    st.add_fact(key, "客户和我一起去越南", "llm", source="ai_inferred", source_quote="我出差去越南")    # subject
    st.add_fact(key, "客户自称Tom", "llm", source="ai_inferred", source_quote="I am Bob")            # unanchored
    st.add_fact(key, "客户去越南出差", "llm", source="ai_inferred", source_quote="我出差去越南")       # 保留
    st.add_fact("telegram:9", "客户有一个女儿", "llm", source="ai_inferred", source_quote="I have a daughter")  # 保留
    st._conn.close()
    mod = _load_purge()
    conn = mod._memory_conn(db, readonly=True)
    rows = mod.scan_memory(conn)
    bad = {r["fact"]: r["reason"] for r in rows}
    assert bad == {"客户做AI巡检工作": "no_evidence", "客户和我一起去越南": "subject", "客户自称Tom": "unanchored"}
    assert mod.scan_memory(conn, user_id="telegram:9") == []
    assert mod.purge_memory(conn, rows, apply=False) == 0
    conn.close()
    # CLI dry-run：只列出，不动库
    assert mod.main(["--memory", "--memory-db", str(db), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "mode=dry-run hits=3" in out and "reason=subject" in out and "未改库" in out
    # apply：只删 3 条 ai_inferred 脏行；user_stated / 合格 ai_inferred 留
    assert mod.main(["--memory", "--memory-db", str(db), "--apply"]) == 0
    out = capsys.readouterr().out
    assert "deleted 3 memory row(s)" in out
    conn = mod._memory_conn(db, readonly=True)
    left = sorted(r[0] for r in conn.execute("SELECT content FROM episodic_memory").fetchall())
    assert left == sorted(["客户住在马尼拉", "客户去越南出差", "客户有一个女儿"])
    assert mod.scan_memory(conn) == []
    conn.close()
    # 画像侧 judge_cell / scan_store 旧口径不变
    assert mod.judge_cell("occupation", {"value": "老师", "source": "confirmed", "status": "confirmed", "v": "老师", "src": "agent"}) == ""
    src = (_ROOT / "tools" / "profile_purge_unanchored.py").read_text(encoding="utf-8")
    assert "--memory" in src and "--memory-db" in src and "--user-id" in src and "fact_gate" in src
