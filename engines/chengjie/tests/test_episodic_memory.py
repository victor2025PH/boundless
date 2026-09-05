"""Tests for episodic memory store and forget/heuristic helpers."""

import tempfile
from pathlib import Path

import pytest

from src.utils.episodic_memory_store import (
    EpisodicMemoryStore,
    compute_memory_storage_key,
)
from src.utils.episodic_vector import cosine_similarity, vec_to_blob
from src.utils.memory_heuristic import extract_heuristic_facts, matches_forget_intent


@pytest.fixture
def mem_db():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.db"
        store = EpisodicMemoryStore(p)
        yield store
        store.close()


def test_add_dedupe(mem_db: EpisodicMemoryStore):
    uid = "u1"
    assert mem_db.add_fact(uid, "用户自称：小明") is not None
    assert mem_db.add_fact(uid, "用户自称：小明") is None
    assert mem_db.count(uid) == 1


def test_prune(mem_db: EpisodicMemoryStore):
    uid = "u2"
    for i in range(5):
        mem_db.add_fact(uid, f"fact {i} unique {i}")
    mem_db.prune_oldest(uid, 2)
    assert mem_db.count(uid) == 2


def test_heuristic_call_me():
    fs = extract_heuristic_facts("以后叫我阿强就行")
    assert any("阿强" in f for f in fs)


def test_forget_match():
    assert matches_forget_intent("忘掉吧", ["忘掉"]) is True
    assert matches_forget_intent("今天天气好", ["忘掉"]) is False


def test_compute_memory_storage_key():
    assert compute_memory_storage_key("user", "123", -10099) == "123"
    assert compute_memory_storage_key("chat_user", "123", 123) == "123"
    assert compute_memory_storage_key("chat_user", "456", -10099) == "-10099_456"


# ── #96（0830 Steven 实锤）：无溯源称呼类旧条目消费端降权 ────────────────────

def test_no_provenance_name_fact_downweighted(mem_db: EpisodicMemoryStore):
    """1.0.63 前存量「用户称呼自己为X」类条目（无 source_quote）注入 prompt
    时必须带方向存疑标注——方向抽反的旧条目正是「拿人设名叫客户」的病灶。"""
    uid = "u96"
    # 模拟存量旧条目：显式空溯源（旧库 ALTER 后默认空串）
    assert mem_db.add_fact(uid, "用户称呼自己为steven",
                           source="user_stated", source_quote="") is not None
    out = mem_db.get_bullets_for_prompt(uid)
    assert "用户称呼自己为steven" in out
    assert "无原话溯源" in out and "勿据此称呼对方" in out


def test_name_fact_with_provenance_not_marked(mem_db: EpisodicMemoryStore):
    # 新抽取条目带溯源 → 不标（标注只治「对不了质」的存量）
    uid = "u96b"
    assert mem_db.add_fact(uid, "客户自称：steven", source="user_stated",
                           source_quote="my name is steven",
                           source_ts=1.0) is not None
    out = mem_db.get_bullets_for_prompt(uid)
    assert "客户自称：steven" in out
    assert "无原话溯源" not in out


def test_non_name_fact_without_provenance_not_marked(mem_db: EpisodicMemoryStore):
    # 非称呼类旧条目不标——全标＝噪声稀释重点
    uid = "u96c"
    assert mem_db.add_fact(uid, "用户喜欢喝美式咖啡",
                           source="user_stated", source_quote="") is not None
    out = mem_db.get_bullets_for_prompt(uid)
    assert "美式咖啡" in out
    assert "无原话溯源" not in out


def test_ai_inferred_mark_takes_precedence(mem_db: EpisodicMemoryStore):
    # ai_inferred 已有降权标注 → 不叠加溯源标注（elif 语义）
    uid = "u96d"
    assert mem_db.add_fact(uid, "用户希望被称呼为老板",
                           source="ai_inferred", source_quote="") is not None
    out = mem_db.get_bullets_for_prompt(uid)
    assert "AI推断" in out
    assert "无原话溯源" not in out


def test_extract_prompt_pins_direction_rules():
    """#96-B 抽取 prompt 方向/主语铁律接线钉（措辞可改，语义锚不许丢）。"""
    import inspect
    from src.ai.ai_client import AIClient
    # J-10 A1：prompt 本体搬进 extract_memory_facts（extract_memory_bullets 成兼容壳）
    src = inspect.getsource(AIClient.extract_memory_facts)
    assert "称呼方向铁律" in src, "抽取 prompt 丢了呼叫位方向规则（#96）"
    assert "主语无歧义铁律" in src, "抽取 prompt 丢了主语显式规则（#96）"
    assert "用户称呼自己为X" in src, "抽取 prompt 必须显式禁双解句式（#96）"


def test_strip_composite_user_id_defends_key_doubling():
    """P10（2026-07-27）：防「完整记忆键回喂」——conversation_id / canonical 被当
    chat_key 传入时剥回裸形态，否则 CPI 注册 platform:platform:… 翻倍键
    （生产 user_identity_map 实锤 8 行，protocol_autoreply 链自拼 user_id 所致）。"""
    from src.utils.episodic_memory_store import strip_composite_user_id

    # 家族①：platform:acct:peer 整串被当 user_id（协议线自拼）
    assert strip_composite_user_id(
        "whatsapp:639270135480:66803566865", "whatsapp"
    ) == "639270135480:66803566865"
    # 家族②嵌套翻倍也剥干净
    assert strip_composite_user_id(
        "whatsapp:whatsapp:639270135480:66803566865", "whatsapp"
    ) == "639270135480:66803566865"
    # 平台不匹配 / 平台未知：不动（此时不走 CPI resolve，读写自洽）
    assert strip_composite_user_id("whatsapp:639:66", "telegram") == "whatsapp:639:66"
    assert strip_composite_user_id("whatsapp:639:66", "") == "whatsapp:639:66"
    # 正常裸 id / 账号分桶键原样通过
    assert strip_composite_user_id("8595708452", "telegram") == "8595708452"
    assert strip_composite_user_id("acct_a:111", "telegram") == "acct_a:111"
    assert strip_composite_user_id("", "telegram") == ""
    # 大小写容错；纯 "platform:" 退化串不剥成空
    assert strip_composite_user_id("WhatsApp:639:66", "whatsapp") == "639:66"
    assert strip_composite_user_id("whatsapp:", "whatsapp") == "whatsapp:"


class _KeyStub:
    """绑真方法跑真代码的最小假体（同 test_conversation_scope 口径 + logger）。"""
    import logging as _logging
    _memory_cfg = {"scope": "user"}
    _cpi = None
    logger = _logging.getLogger("test_key_stub")


def test_episodic_storage_key_normalizes_composite_ids():
    """_episodic_storage_key 端到端：复合 id 回喂两种家族都归一到 acct:peer，
    且不二次叠账号前缀；裸键行为与旧版逐字节一致。"""
    from src.skills.skill_manager import SkillManager

    call = SkillManager._episodic_storage_key
    # 家族①：user_id=platform:acct:peer + account_id=acct（协议线）→ acct:peer
    assert call(
        _KeyStub(), "whatsapp:639270135480:66803566865", "", "whatsapp",
        account_id="639270135480",
    ) == "639270135480:66803566865"
    # 家族②：同串但 account 缺失 → 剥前缀后已带账号分桶，原样保留
    assert call(
        _KeyStub(), "whatsapp:639270135480:66803566865", "", "whatsapp",
    ) == "639270135480:66803566865"
    # 裸键 + 账号：旧行为不变（acct:peer）
    assert call(
        _KeyStub(), "66803566865", "", "whatsapp", account_id="639270135480",
    ) == "639270135480:66803566865"
    # 裸键无账号：不变
    assert call(_KeyStub(), "66803566865", "", "whatsapp") == "66803566865"
    # default 账号：make_context_key 语义（裸键）
    assert call(
        _KeyStub(), "66803566865", "", "whatsapp", account_id="default",
    ) == "66803566865"


def test_cosine_and_fusion(mem_db: EpisodicMemoryStore):
    a = [1.0, 0.0, 0.0]
    b = [1.0, 0.0, 0.0]
    assert abs(cosine_similarity(a, b) - 1.0) < 1e-6
    uid = "vx"
    rid = mem_db.add_fact(uid, "咖啡燕麦拿铁", embedding_blob=vec_to_blob([0.9, 0.1, 0.0]))
    assert rid is not None
    out = mem_db.get_bullets_for_prompt(
        uid,
        3,
        500,
        query_text="拿铁",
        rerank_keywords=True,
        query_embedding=[0.95, 0.05, 0.0],
        use_vector_fusion=True,
        vector_weight=0.5,
        keyword_weight=0.5,
    )
    assert "拿铁" in out or "咖啡" in out


def test_rerank_prefers_overlap(mem_db: EpisodicMemoryStore):
    uid = "u9"
    mem_db.add_fact(uid, "用户喜欢喝燕麦拿铁")
    mem_db.add_fact(uid, "用户住在北京")
    mem_db.add_fact(uid, "用户讨厌下雨")
    out = mem_db.get_bullets_for_prompt(
        uid, 2, 500, query_text="你记得我喜欢喝什么吗", rerank_keywords=True
    )
    assert "燕麦" in out or "拿铁" in out


# ── bullets 年龄标注（2026-07-26：修「记忆当现在」——陈旧 raw 事实无年龄被 LLM
#    当实时状态复述；≥48h 缀「（X天前提到）」，stable 无时效结论不标注）──────


def _backdate(store: EpisodicMemoryStore, rid: int, age_sec: float, tier: str = "raw"):
    import time as _t
    store._conn.execute(
        "UPDATE episodic_memory SET created_at = ?, tier = ? WHERE id = ?",
        (_t.time() - age_sec, tier, rid),
    )
    store._conn.commit()


def test_age_hint_fresh_fact_unannotated(mem_db: EpisodicMemoryStore):
    uid = "age1"
    mem_db.add_fact(uid, "用户那边下雨了")
    out = mem_db.get_bullets_for_prompt(uid, 5, 500)
    assert out == "- 用户那边下雨了"


def test_age_hint_old_raw_fact_annotated(mem_db: EpisodicMemoryStore):
    uid = "age2"
    rid = mem_db.add_fact(uid, "用户那边下雨了")
    _backdate(mem_db, rid, 3 * 86400 + 3600)
    out = mem_db.get_bullets_for_prompt(uid, 5, 500)
    assert out == "- 用户那边下雨了（3天前提到）"


def test_age_hint_stable_tier_never_annotated(mem_db: EpisodicMemoryStore):
    uid = "age3"
    rid = mem_db.add_fact(uid, "用户养了一只猫")
    _backdate(mem_db, rid, 90 * 86400, tier="stable")
    out = mem_db.get_bullets_for_prompt(uid, 5, 500)
    assert out == "- 用户养了一只猫"


def test_age_hint_optout(mem_db: EpisodicMemoryStore):
    uid = "age4"
    rid = mem_db.add_fact(uid, "用户说下周去东京")
    _backdate(mem_db, rid, 10 * 86400)
    out = mem_db.get_bullets_for_prompt(uid, 5, 500, age_hints=False)
    assert out == "- 用户说下周去东京"


def test_age_hint_survives_rerank_paths(mem_db: EpisodicMemoryStore):
    # 关键词重排 + 向量融合两条打分路径都必须把 ts/tier 带到渲染层
    uid = "age5"
    rid = mem_db.add_fact(
        uid, "用户喜欢喝燕麦拿铁", embedding_blob=vec_to_blob([1.0, 0.0, 0.0]))
    _backdate(mem_db, rid, 5 * 86400)
    kw = mem_db.get_bullets_for_prompt(
        uid, 3, 500, query_text="喜欢喝什么", rerank_keywords=True)
    assert "（5天前提到）" in kw
    fused = mem_db.get_bullets_for_prompt(
        uid, 3, 500, query_text="拿铁", rerank_keywords=True,
        query_embedding=[1.0, 0.0, 0.0], use_vector_fusion=True)
    assert "（5天前提到）" in fused


def test_age_hint_label_buckets():
    from src.utils.episodic_memory_store import _age_hint_label
    assert _age_hint_label(3 * 3600) == "3小时"
    assert _age_hint_label(2 * 86400) == "2天"
    assert _age_hint_label(13 * 86400) == "13天"
    assert _age_hint_label(14 * 86400) == "2周"
    assert _age_hint_label(59 * 86400) == "8周"
    assert _age_hint_label(60 * 86400) == "2个月"
    assert _age_hint_label(200 * 86400) == "6个月"


def test_transient_fact_age_hint_at_6h(mem_db: EpisodicMemoryStore):
    """瞬态（下雨）≥6h 即标注；持久事实同龄仍裸行。"""
    uid = "age_tr"
    rain = mem_db.add_fact(uid, "用户那边下雨了")
    love = mem_db.add_fact(uid, "用户说'我们来谈恋爱吧'")
    _backdate(mem_db, rain, 10 * 3600)
    _backdate(mem_db, love, 10 * 3600)
    out = mem_db.get_bullets_for_prompt(uid, 5, 500)
    assert "下雨了（10小时前提到）" in out
    assert "谈恋爱吧" in out and "谈恋爱吧」（" not in out


def test_fetch_rows_missing_embedding_and_prefix(mem_db: EpisodicMemoryStore):
    mem_db.add_fact("alpha_1", "无向量事实一")
    mem_db.add_fact("beta_2", "无向量事实二")
    r1 = mem_db.add_fact(
        "alpha_1",
        "已有向量",
        embedding_blob=vec_to_blob([0.1, 0.2, 0.3, 0.4]),
    )
    assert r1 is not None
    all_missing = mem_db.fetch_rows_missing_embedding(10)
    assert len(all_missing) == 2
    only_alpha = mem_db.fetch_rows_missing_embedding(10, memory_key_prefix="alpha")
    assert len(only_alpha) == 1
    assert only_alpha[0][1] == "alpha_1"
