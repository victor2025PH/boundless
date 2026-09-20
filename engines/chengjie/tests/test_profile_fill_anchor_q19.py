# -*- coding: utf-8 -*-
"""Q-19（#294 / #272 追加）：画像 AI 推断只写可锚定事实。

0911 实锤：粤语客户全程聊岚山散步，画像被写进 occupation='織り機の前に座って、新しい帯の色合わせ'
（日语整句，己方日语咖啡馆稿串台幻觉）、residence='我住在去嵐山散步'（整句进地名槽），
右栏「摸底 1/10」把幻觉当已采集。

A 逐槽类型校验 ``profile_slots.slot_validate``：age 16–99 / 年龄段；occupation·location·residence
  ≤24 字短语禁整句；name ≤12；婚恋 / 家庭枚举 → 不过 ``skipped=invalid:<why>`` + ``[profile] drop``。
B 原话锚定：ai_inferred 候选必须带 evidence，且能在最近 30 条客户入站逐字找到、值在 evidence 里
  → 否则 ``unanchored``。
C 只吃客户入站：``run_extraction`` 不把 reply（出站 / 译文 / 关怀稿）送进 prompt；值语种 ≠ 客户
  主语种 → ``lang_mismatch``。
D 进度只数 confirmed（``slots_progress[].confirmed`` / ``slots_counts``）、待确认另显、age 控件。
E ``tools/profile_purge_unanchored.py --dry-run / --apply`` 清洗存量；confirmed 值一字不动。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.companion.goals import profile_fill as pf
from src.companion.goals import profile_slots as ps
from src.companion.goals.profile_slots import cell_view
from src.companion.goals.store import get_goal_store, reset_goal_store
from src.companion.user_clock_resolver import stated_place_from_inbound_text

_ROOT = Path(__file__).resolve().parents[1]
PLAT, ACCT, CK = "telegram", "7331682688", "7340576921"
CONV = f"{PLAT}:{ACCT}:{CK}"
JA_JUNK = "織り機の前に座って、新しい帯の色合わせ"
CANTO = [
    "今日好攰呀",
    "聽日去嵐山散步",
    "週末想試新配搭，深藍搭少少金",
    "我係護士，做夜班",
    "我今年32歲",
]


@pytest.fixture(autouse=True)
def _reset():
    pf.reset_stall()
    pf.bind_app(None)
    reset_goal_store()
    yield
    pf.reset_stall()
    pf.bind_app(None)
    reset_goal_store()


def _fields(st):
    return dict((st.get_customer_profile(PLAT, CK) or {}).get("fields") or {})


# ── A：逐槽类型校验 ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key,value,ok", [
    ("age", "32", True), ("age", "32岁", True), ("age", "三十多", True), ("age", "90后", True),
    ("age", "30s", True), ("age", "12", False), ("age", "120", False), ("age", "織り機", False),
    ("age", JA_JUNK, False),
    ("occupation", "union carpenter", True), ("occupation", "護士", True), ("occupation", JA_JUNK, False),
    ("occupation", "我是一名在东京工作的护士，做夜班", False),
    ("residence", "我住在去嵐山散步", False), ("location", "去嵐山散步", False), ("location", "得慣呀", False),
    ("location", "河邊行", False), ("location", "旺角", True), ("location", "New York", True), ("location", "嵐山", True),
    ("name", "Michael", True), ("name", "hi Michael how are you doing", False),
    ("marital_status", "单身", True), ("marital_status", "single", True), ("marital_status", "有点复杂", False),
    ("family_status", "有孩子", True), ("family_status", "has kids", True), ("family_status", "热闹", False),
    ("interests", "喜欢爬山、游泳", True),
])
def test_slot_validate_shapes(key, value, ok):
    nv, why = ps.slot_validate(key, value)
    assert (why == "") is ok, (key, value, nv, why)
    if ok:
        assert nv


def test_slot_validate_normalizes_age_and_enum_aliases():
    assert ps.slot_validate("age", "32岁") == ("32", "")
    assert ps.slot_validate("marital_status", "single") == ("单身", "")
    assert ps.slot_validate("family_status", "has kids") == ("有孩子", "")
    assert ps.slot_input_kind("age")[0] == "age" and "30s" in ps.slot_input_kind("age")[1]
    assert ps.slot_input_kind("marital_status") == ("enum", ("已婚", "离异", "恋爱中", "单身"))
    assert ps.slot_input_kind("occupation") == ("", ())


def test_apply_drops_invalid_and_logs_reason(caplog):
    caplog.set_level(logging.INFO)
    st = get_goal_store(":memory:")
    res = pf.apply(st, PLAT, CK, {
        "occupation": {"value": JA_JUNK, "evidence": JA_JUNK},
        "residence": {"value": "我住在去嵐山散步", "evidence": "我住在去嵐山散步"},
        "age": {"value": "32", "evidence": "我今年32歲"},
    }, source="ai_inferred", conversation_id=CONV)
    assert res["written"] == ["age"]
    assert res["skipped"] == {"occupation": "invalid:sentence", "residence": "invalid:sentence"}
    drops = [m for m in caplog.messages if m.startswith("[profile] drop ")]
    assert any("slot=occupation" in m and "reason=invalid:sentence" in m for m in drops)
    assert any("slot=residence" in m and "reason=invalid:sentence" in m for m in drops)
    assert cell_view(_fields(st)["age"]) == ("32", "ai_inferred", "mentioned")
    # 昵称来源同样过校验；坐席手录不校验、一字不动
    assert pf.apply(st, PLAT, CK, {"age": "abc"}, source="nickname")["skipped"] == {"age": "invalid:age_format"}
    assert pf.apply(st, PLAT, CK, {"occupation": JA_JUNK}, source="user")["written"] == ["occupation"]
    assert cell_view(_fields(st)["occupation"]) == (JA_JUNK, "user", "confirmed")


def test_residence_heuristic_uses_same_place_guard():
    # 0911 实锤三条整句居住地都不再被抽成 user_stated 事实
    assert stated_place_from_inbound_text("我住在去嵐山散步") == ""
    assert stated_place_from_inbound_text("我住得慣呀") == ""
    assert stated_place_from_inbound_text("我住河邊行") == ""
    assert stated_place_from_inbound_text("我住在曼谷") == "曼谷"
    assert ps.place_value_ok("Bangkok") and not ps.place_value_ok("去嵐山散步")


# ── B：原话锚定 ───────────────────────────────────────────────────────────────────

def test_anchor_requires_evidence_found_in_recent_inbound():
    assert pf.anchor_reason("occupation", "護士", "", CANTO) == "unanchored"
    assert pf.anchor_reason("occupation", "護士", "我係護士", CANTO) == ""
    assert pf.anchor_reason("occupation", "護士", "我 係 護 士 ，", CANTO) == "", "空白/标点归一后仍算逐字"
    assert pf.anchor_reason("occupation", "護士", "我係醫生", CANTO) == "unanchored", "evidence 不在入站里"
    assert pf.anchor_reason("occupation", "醫生", "我係護士", CANTO) == "unanchored", "值不在 evidence 里"
    assert pf.anchor_reason("age", "32", "我今年32歲", CANTO) == ""
    # 枚举槽：标签不逐字出现，改验 evidence 含触发词
    assert pf.anchor_reason("marital_status", "单身", "我一直單身", ["我一直單身，冇拖"]) == ""
    assert pf.anchor_reason("marital_status", "单身", "今日好攰呀", CANTO) == "unanchored"


def test_apply_with_recent_inbound_drops_unanchored_but_keeps_direct_calls_compatible(caplog):
    caplog.set_level(logging.INFO)
    st = get_goal_store(":memory:")
    res = pf.apply(st, PLAT, CK, {
        "occupation": {"value": "護士", "evidence": ""},              # 无 evidence
        "location": {"value": "東京", "evidence": "我住東京"},        # evidence 不在入站
        "age": {"value": "32", "evidence": "我今年32歲"},             # 锚定通过
    }, source="ai_inferred", conversation_id=CONV, recent_inbound=CANTO)
    assert res["written"] == ["age"]
    assert res["skipped"] == {"occupation": "unanchored", "location": "unanchored"}
    assert any("slot=occupation" in m and "reason=unanchored" in m for m in caplog.messages)
    assert _fields(st)["age"]["evidence"] == "我今年32歲"
    # 不传 recent_inbound（旧调用方 / 单测直调）→ 不做锚定，行为不变
    assert pf.apply(st, PLAT, CK, {"occupation": "driver"}, source="ai_inferred")["written"] == ["occupation"]


def test_recent_inbound_texts_only_takes_direction_in_and_last_30():
    rows = ([{"direction": "in", "text": f"in{i}"} for i in range(40)]
            + [{"direction": "out", "text": "我方出站稿"}, {"direction": "in", "text": "[图片内容] 截图里写着我是老板"},
               {"direction": "in", "text": ""}])
    ibx = SimpleNamespace(list_recent_messages=lambda conv, limit=50: rows)
    got = pf.recent_inbound_texts(ibx, CONV)
    assert len(got) == pf.ANCHOR_WINDOW == 30
    assert "我方出站稿" not in got and got[0] == "in10" and got[-1] == "in39"
    assert not any(t.startswith("[图片内容]") for t in got)
    assert pf.recent_inbound_texts(None, CONV) == [] and pf.recent_inbound_texts(SimpleNamespace(), CONV) == []


# ── C：只吃客户入站 + 语种守卫 ────────────────────────────────────────────────────

def test_lang_guard_japanese_value_vs_cantonese_customer():
    assert pf.dominant_lang(CANTO) == "zh" and pf.text_lang(JA_JUNK) == "ja"
    assert pf.lang_mismatch_reason(JA_JUNK, CANTO) == "lang_mismatch"
    assert pf.lang_mismatch_reason(JA_JUNK, CANTO, evidence=JA_JUNK) == "lang_mismatch"
    assert pf.lang_mismatch_reason("護士", CANTO) == "" and pf.lang_mismatch_reason("32", CANTO) == ""
    # 客户自己中英夹杂写的英文 token（evidence 是中文句）放行；纯英文值 × 纯中文客户拦
    assert pf.lang_mismatch_reason("HSBC", CANTO + ["我在HSBC返工"], evidence="我在HSBC返工") == ""
    assert pf.lang_mismatch_reason("nurse", CANTO) == "lang_mismatch"
    # 日文客户：汉字值放行（日文汉字）
    assert pf.lang_mismatch_reason("看護師", ["私は看護師です", "今日は疲れた"]) == ""
    assert pf.lang_mismatch_reason("nurse", ["I am a nurse", "so tired today"]) == ""


class _AI:
    def __init__(self, raw):
        self.raw, self.prompts = raw, []

    async def chat(self, prompt):
        self.prompts.append(prompt)
        return self.raw


def _cfg():
    return {"companion": {"goals": {"enabled": True, "db_path": ":memory:", "profile_llm": {"enabled": True}}},
            "memory": {"extract": {"use_llm": False}}}


def test_run_extraction_incident_replay_writes_nothing_dirty(caplog):
    """0911 复盘：LLM 把日语整句塞进 occupation、把整句塞进 residence、把数字年龄摘对——
    只有 age 能落，且 prompt 里没有我方 ASSISTANT 稿。"""
    caplog.set_level(logging.INFO)
    gs = get_goal_store(":memory:")
    raw = json.dumps({"facts": [], "slots": {
        "occupation": {"value": JA_JUNK, "evidence": JA_JUNK},
        "residence": {"value": "我住在去嵐山散步", "evidence": "聽日去嵐山散步"},
        "age": {"value": "32", "evidence": "我今年32歲"},
        "location": {"value": "嵐山", "evidence": "聽日去嵐山散步"},
    }}, ensure_ascii=False)
    ai = _AI(raw)
    hist = [{"direction": "in", "text": t} for t in CANTO] + [{"direction": "out", "text": "金曜の午後はやっぱりいいよね、こっちのカフェも静かで"}]
    inbox = SimpleNamespace(get_conversation=lambda cid: {"conversation_id": cid, "display_name": "華哥", "chat_key": CK},
                            list_recent_messages=lambda conv, limit=50: hist)
    # 客户消息里夹了引用的日文（实锤形态：接地能过），整句居住地也「在原话里」——
    # 出处护栏拦不住，形态校验必须拦
    res = asyncio.run(pf.run_extraction(
        ai, _cfg(), None, user_msg=f"我今年32歲，聽日去嵐山散步。「{JA_JUNK}」係咩意思？",
        reply="金曜の午後はやっぱりいいよね、こっちのカフェも静かで",
        platform=PLAT, chat_key=CK, account_id=ACCT, inbox_store=inbox, goal_store=gs))
    assert len(ai.prompts) == 1 and "ASSISTANT:" not in ai.prompts[0] and "カフェ" not in ai.prompts[0]
    assert "USER:" in ai.prompts[0] and "evidence" in ai.prompts[0]
    f = _fields(gs)
    assert cell_view(f["age"]) == ("32", "ai_inferred", "mentioned")
    assert "occupation" not in f and "residence" not in f, "整句 / 日语幻觉不落库"
    # 嵐山：值在 evidence 里、evidence 在入站里、地名形态合格 → 可落（客户确实提了）
    assert cell_view(f["location"]) == ("嵐山", "ai_inferred", "mentioned")
    assert res["slots_written"] == 2
    assert res["slots_dropped"]["occupation"] == "invalid:sentence"
    assert "residence" in res["slots_dropped"]
    drops = [m for m in caplog.messages if m.startswith("[profile] drop ")]
    assert any("slot=occupation" in m and "reason=invalid:sentence" in m for m in drops)


def test_run_extraction_japanese_value_grounded_in_quoted_text_still_dropped_by_lang_guard():
    """接地能过（客户消息里夹了引用的日文）但语种守卫拦：日语值 × 粤语客户 = drop。"""
    gs = get_goal_store(":memory:")
    ja_occ = "看護師をしています"
    raw = json.dumps({"facts": [], "slots": {"occupation": {"value": ja_occ, "evidence": ja_occ}}}, ensure_ascii=False)
    ai = _AI(raw)
    hist = [{"direction": "in", "text": t} for t in CANTO]
    inbox = SimpleNamespace(get_conversation=lambda cid: None, list_recent_messages=lambda conv, limit=50: hist)
    res = asyncio.run(pf.run_extraction(
        ai, _cfg(), None, user_msg=f"你講咩？「{ja_occ}」", reply="", platform=PLAT, chat_key=CK,
        account_id=ACCT, inbox_store=inbox, goal_store=gs))
    assert res["slots_written"] == 0 and res["slots_dropped"] == {"occupation": "lang_mismatch"}
    assert "occupation" not in _fields(gs)


def test_confirmed_values_never_touched_by_ai_candidates():
    st = get_goal_store(":memory:")
    pf.apply(st, PLAT, CK, {"occupation": "老师", "age": "45"}, source="confirmed")
    res = pf.apply(st, PLAT, CK, {"occupation": {"value": "護士", "evidence": "我係護士"},
                                  "age": {"value": "32", "evidence": "我今年32歲"}},
                   source="ai_inferred", recent_inbound=CANTO, notify=False)
    assert res["written"] == [] and res["skipped"] == {"occupation": "conflict", "age": "conflict"}
    f = _fields(st)
    assert cell_view(f["occupation"]) == ("老师", "confirmed", "confirmed")
    assert cell_view(f["age"]) == ("45", "confirmed", "confirmed")


# ── D：进度只数 confirmed + 控件 ───────────────────────────────────────────────────

def test_goal_routes_add_readonly_fields_only_and_frontend_wired():
    gr = (_ROOT / "src" / "web" / "routes" / "goal_routes.py").read_text(encoding="utf-8")
    assert '"confirmed": str((_states.get(k) or ("", ""))[0] or "") == "confirmed"' in gr
    assert 'view["slots_counts"] = {"total": len(sel), "confirmed": _n_conf, "mentioned": _n_ment}' in gr
    assert "slot_input_kind" in gr
    js = (_ROOT / "shared" / "copilot" / "components" / "cp-goal.js").read_text(encoding="utf-8")
    mirror = (_ROOT / "desktop" / "renderer" / "shared" / "copilot" / "components" / "cp-goal.js").read_bytes()
    assert (_ROOT / "shared" / "copilot" / "components" / "cp-goal.js").read_bytes() == mirror, "双树必须字节一致"
    assert "rows.filter((s) => s && s.confirmed).length" in js, "N/10 只数 confirmed"
    assert "inbox.goal.slots.pending_n" in js and "_clipVal(" in js and "_ageValueOk(" in js
    assert 'data-prof-kind="age"' in js and 'inputmode="numeric"' in js
    assert 'data-act="slot_reject"' in js and 'data-act="slot_confirm"' in js, "ai_inferred 卡必须有 ✓ / ✕"
    from src.web.i18n_packs import goals as gp
    for k in ("inbox.goal.slots.pending_n", "inbox.goal.slots.pending_t",
              "inbox.goal.profile.age_hint", "inbox.goal.profile.age_invalid"):
        assert k in gp.ZH and k in gp.EN, k
    assert "{n}" in gp.ZH["inbox.goal.slots.pending_n"] and "{n}" in gp.EN["inbox.goal.slots.pending_n"]
    for host in ("shared/copilot/app.html", "desktop/renderer/shared/copilot/app.html",
                 "src/web/templates/unified_inbox.html"):
        assert "cp-goal.js?v=20260912a" in (_ROOT / host).read_text(encoding="utf-8"), host


# ── E：清洗脚本 ───────────────────────────────────────────────────────────────────

def _load_purge():
    p = _ROOT / "tools" / "profile_purge_unanchored.py"
    spec = importlib.util.spec_from_file_location("profile_purge_unanchored", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_purge_tool_dry_run_lists_and_apply_deletes_only_auto_sources(capsys):
    mod = _load_purge()
    st = get_goal_store(":memory:")
    pf.apply(st, PLAT, CK, {"occupation": "老师"}, source="confirmed")
    pf.apply(st, PLAT, CK, {"age": "32"}, source="user")
    st.upsert_profile_cells(PLAT, CK, {
        "location": pf.make_cell(JA_JUNK, "ai_inferred"),                       # invalid
        "residence": pf.make_cell("我住在去嵐山散步", "ai_inferred"),              # invalid
        "interests": pf.make_cell("hiking", "ai_inferred"),                     # 无 evidence → unanchored
        "need": pf.make_cell("想学粤语", "ai_inferred", evidence="我想学粤语"),      # 带 evidence → 保留
        "name": pf.make_cell("Michael", "nickname"),                            # 昵称：形态合格 → 保留
        "channel": pf.make_cell("我们主要是靠朋友介绍的，偶尔发发朋友圈，效果一般，转化不高", "nickname"),  # 昵称整句 → invalid
    })
    rows = mod.scan_store(st)
    bad = {(r["slot"]): r["reason"] for r in rows}
    assert set(bad) == {"location", "residence", "interests", "channel"}
    assert bad["location"].startswith("invalid:") and bad["interests"] == "unanchored"
    n = mod.purge(st, rows, apply=False)
    assert n == 0
    f = _fields(st)
    assert "location" in f and "interests" in f, "dry-run 不动库"
    n = mod.purge(st, rows, apply=True)
    assert n == 4
    f = _fields(st)
    assert set(f) == {"occupation", "age", "need", "name"}
    assert cell_view(f["occupation"]) == ("老师", "confirmed", "confirmed")
    assert cell_view(f["age"]) == ("32", "user", "confirmed")
    # 二次扫描：干净
    assert mod.scan_store(st) == []
    # CLI 形态：--dry-run 默认、--apply 才删；usage 文本带回访说明
    src = (_ROOT / "tools" / "profile_purge_unanchored.py").read_text(encoding="utf-8")
    assert "--dry-run" in src and "--apply" in src and "回访" in src
