"""P0-1（#341 根因修复）：记忆读侧精确归属 + 视角转换 + 第三方实体守卫。"""
from __future__ import annotations

import pytest

from src.utils.episodic_memory_store import EpisodicMemoryStore, facts_for_key
from src.utils.memory_perspective import (
    PERSPECTIVE_NOTE,
    facts_to_second_person,
    to_second_person,
)
from src.utils.proactive_fabrication_guard import (
    detect_proactive_fabrication,
    detect_ungrounded_third_party,
)
from src.utils.proactive_prompt import build_proactive_prompt
from src.utils.proactive_topic import select_proactive_topic


# ── 读侧：精确键归属 ─────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    s = EpisodicMemoryStore(str(tmp_path / "ep.db"))
    yield s
    s.close()


def test_list_facts_exact_key_never_leaks_substring_neighbors(store):
    # 「12」是「112」「1234」的子串——旧 list_rows(prefix) 用 LIKE %12% 会全命中
    store.add_fact("12", "客户的妈妈在装修")
    store.add_fact("112", "客户有一只狗")
    store.add_fact("1234", "客户在备考")
    store.add_fact("wa:acct:12", "客户喜欢咖啡")

    leak = store.list_rows(prefix="12", limit=50)
    assert len(leak) == 4  # 管理端搜索确实是模糊的（对照）

    mine = store.list_facts("12")
    assert [r["content"] for r in mine] == ["客户的妈妈在装修"]
    assert all(r["memory_key"] == "12" for r in mine)

    assert [r["content"] for r in facts_for_key(store, "12")] == ["客户的妈妈在装修"]
    assert facts_for_key(store, "") == []
    assert facts_for_key(None, "12") == []


def test_list_facts_excludes_ignored_and_stale(store):
    store.add_fact("u1", "客户有一个女儿")
    store.add_fact("u1", "客户住在马尼拉")
    rows = store.list_facts("u1")
    assert len(rows) == 2
    row_id = next(r["id"] for r in rows if r["content"] == "客户住在马尼拉")
    store.ignore_fact(row_id)
    assert [r["content"] for r in store.list_facts("u1")] == ["客户有一个女儿"]


def test_list_facts_source_filter(store):
    store.add_fact("u2", "客户在备考", source="user_stated")
    store.add_fact("u2", "客户大概是学生", source="ai_inferred")
    assert [r["content"] for r in store.list_facts("u2", source="user_stated")] == ["客户在备考"]
    assert len(store.list_facts("u2")) == 2


class _LegacyPrefixStore:
    """只有 list_rows 的旧适配器/测试桩：facts_for_key 必须按 memory_key 过滤。"""

    def __init__(self, rows):
        self._rows = rows

    def list_rows(self, *, prefix="", limit=50, source=""):
        return [r for r in self._rows if prefix in r.get("memory_key", prefix)]


def test_facts_for_key_filters_legacy_prefix_store():
    st = _LegacyPrefixStore([
        {"memory_key": "12", "content": "A"},
        {"memory_key": "112", "content": "B"},
        {"content": "no-key-row"},
    ])
    got = [r["content"] for r in facts_for_key(st, "12")]
    assert got == ["A", "no-key-row"]


def test_facts_for_key_never_raises():
    class _Boom:
        def list_rows(self, **kw):
            raise RuntimeError("db gone")

    assert facts_for_key(_Boom(), "12") == []

    class _Nothing:
        pass

    assert facts_for_key(_Nothing(), "12") == []  # type: ignore[arg-type]


# ── 视角转换 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("src,expected", [
    ("客户的妈妈在装修", "TA的妈妈在装修"),
    ("该客户有一个女儿", "TA有一个女儿"),
    ("这位客户住在马尼拉", "TA住在马尼拉"),
    ("用户喜欢喝咖啡", "TA喜欢喝咖啡"),
    ("对方最近在备考", "TA最近在备考"),
    ("the customer's mom is renovating", "TA's mom is renovating"),
    ("The client lives in Manila", "TA lives in Manila"),
    ("TA的妈妈在装修", "TA的妈妈在装修"),          # 幂等
    ("客户服务电话在墙上", "客户服务电话在墙上"),      # 「客户服务」不是称谓
    ("客户经理姓王", "客户经理姓王"),
    ("", ""),
])
def test_to_second_person(src, expected):
    assert to_second_person(src) == expected
    assert to_second_person(to_second_person(src)) == expected


def test_facts_to_second_person_dedups_and_drops_empty():
    out = facts_to_second_person(["客户在备考", "TA在备考", "", "  ", "客户有猫"])
    assert out == ["TA在备考", "TA有猫"]


# ── 注入链：选题 → prompt ────────────────────────────────────────────────────

def _fact(content, **kw):
    row = {"content": content, "source": "user_stated", "tier": "raw",
           "hits": 1, "last_seen": 1.0, "created_at": 1.0, "category": "general"}
    row.update(kw)
    return row


def test_select_proactive_topic_emits_second_person_facts():
    facts = [_fact("客户的妈妈在装修", hits=3), _fact("客户有一个女儿", hits=2)]
    out = select_proactive_topic(facts, silent_hours=30, max_context_facts=2)
    assert out["mode"] == "follow_up"
    assert out["fact"] == "TA的妈妈在装修"
    assert "客户" not in out["fact"]
    assert all("客户" not in c for c in out.get("context_facts", []))
    assert "客户" not in out["directive"]


def test_prompt_converts_context_facts_and_adds_perspective_note():
    plan = {"mode": "follow_up", "directive": "自然回访一下",
            "fact": "TA的妈妈在装修",
            "context_facts": ["客户的妈妈在装修", "the customer's daughter is 5"]}
    p = build_proactive_prompt("Mia", plan)
    assert "TA的妈妈在装修" in p
    assert "TA's daughter is 5" in p
    assert "客户的妈妈" not in p
    assert "customer's" not in p
    assert PERSPECTIVE_NOTE in p


def test_prompt_without_memory_has_no_perspective_note():
    p = build_proactive_prompt("Mia", {"mode": "gentle_checkin", "directive": "随手问候"})
    assert PERSPECTIVE_NOTE not in p


def test_prompt_adds_note_when_directive_carries_operator_wording():
    p = build_proactive_prompt(
        "Mia", {"mode": "follow_up", "directive": "客户之前提过在备考，顺口问一下"})
    assert PERSPECTIVE_NOTE in p


# ── 第三方实体守卫（#341 实锤句） ────────────────────────────────────────────

def test_third_party_guard_blocks_341_sentence_without_facts():
    text = "Hey, how is your mom doing with the renovation?"
    fab, why = detect_ungrounded_third_party(text, [])
    assert fab and "mom" in why


def test_third_party_guard_blocks_perspective_leak_even_with_facts():
    text = "Wondering how your client's mom is doing with the renovation"
    fab, why = detect_ungrounded_third_party(text, ["TA的妈妈在装修"])
    assert fab and "视角" in why


def test_third_party_guard_passes_when_entity_grounded():
    assert detect_ungrounded_third_party(
        "你妈妈那边装修得怎么样了", ["TA的妈妈在装修"]) == (False, "")
    assert detect_ungrounded_third_party(
        "How's your mom doing with the renovation?", ["TA's mom is renovating"]) == (False, "")
    assert detect_ungrounded_third_party(
        "你家狗最近还闹吗", ["TA养了一只柯基"]) == (False, "")


def test_third_party_guard_ignores_persona_own_family_and_plain_talk():
    assert detect_ungrounded_third_party("我妈今天又催我吃饭", []) == (False, "")
    assert detect_ungrounded_third_party("My mom called me today, so sleepy", []) == (False, "")
    assert detect_ungrounded_third_party("刚下班，你在忙什么", []) == (False, "")
    assert detect_ungrounded_third_party("", []) == (False, "")


def test_combined_guard_keeps_past_claim_semantics_and_uses_entity_facts():
    # 往事守卫口径不变：零背景事实 + 「你以前」→ 拦
    fab, _ = detect_proactive_fabrication("你以前说想去海边", [])
    assert fab
    # 实体守卫看 entity_facts（选中事实 + 背景），背景为空也能放行有据的家人
    fab, why = detect_proactive_fabrication(
        "你妈妈装修完了吗", [], entity_facts=["TA的妈妈在装修"])
    assert (fab, why) == (False, "")
    fab, why = detect_proactive_fabrication("你妈妈装修完了吗", [], entity_facts=[])
    assert fab and "mother" in why


# ── 回复主线（A/B 线）注入点：`_episodic_memory_text` 也必须是对话视角 ────────

def test_reply_path_injection_rewrites_operator_subject(store):
    import logging
    from src.skills.skill_manager import SkillManager

    class _Stub:
        logger = logging.getLogger("t")
        _memory_cfg = {"enabled": True, "scope": "user", "inject_max_items": 8,
                       "inject_max_chars": 1200}
        _cpi = None
        _episodic_store = store
        _episodic_storage_key = SkillManager._episodic_storage_key
        _inject_episodic_into_context = SkillManager._inject_episodic_into_context

    uid = "88001"
    store.add_fact(uid, "客户的妈妈在装修")
    store.add_fact(uid, "该客户有一个女儿", source="ai_inferred")
    ctx = {"conversation_id": "telegram:acct:88001"}
    _Stub()._inject_episodic_into_context(ctx, uid, uid, current_user_text="装修", platform="telegram")
    txt = ctx["_episodic_memory_text"]
    assert "TA的妈妈在装修" in txt and "TA有一个女儿" in txt
    assert "客户" not in txt
