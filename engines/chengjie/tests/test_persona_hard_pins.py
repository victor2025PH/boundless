# -*- coding: utf-8 -*-
"""实施86 0830 午批·批B 门禁：档案字段硬钉子 + 记忆五件套。

金标全部来自 0830 上午验证潮实录（工单 #24/#29/#82/#41）：
- #24 档案明写「称呼对方 babe」仍被客户满屏「baba」带跑（AI 模仿着叫回去），
  且记忆库「用户不喜欢被叫 babe」的 AI 推断在反向教唆；
- #82 档案居住地=薄荷岛，AI 自称「马尼拉的雨天早晨」（位置虚构 + 天气无数据
  乱断言 → 被索要雨景照连环穿帮）；
- #41 「这条推断记忆从哪来的」无人能答（溯源缺失）+ 确认/删除语义四连问。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

_ENGINE_ROOT = Path(__file__).resolve().parents[1]


# ── #24 称呼双向硬钉子 ───────────────────────────────────────────────────────

def _pm():
    from src.utils.persona_manager import PersonaManager
    return PersonaManager.get_instance()


def _full_block(persona):
    return _pm()._format_persona_instructions(dict(persona))


def _compact_block(persona):
    return _pm()._format_persona_compact(dict(persona))


_P24 = {
    "id": "t24", "name": "小美", "role": "咖啡店店主",
    "names": {"call_peer": "babe", "peer_calls_you": "baba"},
}


def test_address_pin_full_mode_24():
    blk = _full_block(_P24)
    assert "称呼·硬约束" in blk
    assert "babe" in blk and "baba" in blk
    assert "绝不互换" in blk


def test_address_pin_overrides_inferred_memory_24():
    """钉子必须显式声明压过记忆推断（「不喜欢被叫babe」推断反向教唆实锤）。"""
    blk = _full_block(_P24)
    assert "推断" in blk and "档案" in blk


def test_address_pin_compact_mode_24():
    """compact 是生产主用格式——钉子缺了等于没修。"""
    blk = _compact_block(_P24)
    assert "称呼·硬约束" in blk and "babe" in blk


def test_address_pin_single_side_and_absent():
    only_peer = {"id": "t", "name": "A", "role": "r",
                 "names": {"peer_calls_you": "ma'ma"}}
    blk = _full_block(only_peer)
    assert "ma'ma" in blk and "称呼·硬约束" in blk
    no_fields = {"id": "t", "name": "A", "role": "r",
                 "names": {"nickname": "小A"}}
    assert "称呼·硬约束" not in _full_block(no_fields)


# ── #82 位置身份钉子（ai_client） ────────────────────────────────────────────

def _mk_ai():
    from src.ai.ai_client import AIClient
    c = AIClient.__new__(AIClient)
    c.config = SimpleNamespace(config={})
    return c


def test_position_pin_injected_82():
    p = _mk_ai()._build_context_prompt({
        "last_message": "在干嘛呀",
        "_persona_place_label": "薄荷岛",
    })
    assert "【你的位置】" in p and "薄荷岛" in p
    assert "临时行程" in p  # 覆盖层出口=「你刚说过」travel 锚点


def test_position_pin_weather_curb_without_data_82():
    """无天气数据 → 禁断言具体天气现象（「马尼拉雨晨」实锤）。"""
    p = _mk_ai()._build_context_prompt({
        "last_message": "在干嘛呀",
        "_persona_place_label": "薄荷岛",
    })
    assert "下雨" in p and "模糊" in p


def test_position_pin_no_curb_with_real_weather_82():
    """接了真实天气（weather_note 在场）→ 不注入禁令（真数据可以说）。"""
    p = _mk_ai()._build_context_prompt({
        "last_message": "在干嘛呀",
        "_persona_place_label": "薄荷岛",
        "_persona_weather_note": "【当地天气】薄荷岛 28°C 多云。",
    })
    assert "【你的位置】" in p
    assert "不要主动断言正在下雨" not in p


def test_no_position_pin_without_place():
    p = _mk_ai()._build_context_prompt({"last_message": "在干嘛呀"})
    assert "【你的位置】" not in p


# ── #41 记忆五件套：溯源 / 可编辑 / 推断降级标注 ─────────────────────────────

def _store(tmp_path):
    from src.utils.episodic_memory_store import EpisodicMemoryStore
    return EpisodicMemoryStore(tmp_path / "epi.db")


def test_provenance_written_and_listed(tmp_path):
    st = _store(tmp_path)
    rid = st.add_fact("u1", "用户不喜欢被反复称呼为babe", "llm",
                      source="ai_inferred",
                      source_quote="其实你一直叫我babe我也说不上来喜不喜欢",
                      source_ts=1000.0)
    assert rid
    rows = st.list_rows(prefix="u1")
    assert rows and rows[0]["source_quote"].startswith("其实你一直叫我babe")
    assert rows[0]["source_ts"] == 1000.0


def test_provenance_optional_backcompat(tmp_path):
    """旧调用形状（无溯源参数）不塌——早期条目如实显示无来源。"""
    st = _store(tmp_path)
    assert st.add_fact("u1", "喜欢喝咖啡", "heuristic")
    rows = st.list_rows(prefix="u1")
    assert rows[0]["source_quote"] == ""


def test_edit_fact_promotes_and_clears_embedding(tmp_path):
    st = _store(tmp_path)
    rid = st.add_fact("u1", "用户不喜欢被叫babe", "llm", source="ai_inferred",
                      embedding_blob=b"\x00" * 32)
    assert st.update_fact_content(rid, "用户喜欢被叫babe（档案称呼）") is True
    rows = st.list_rows(prefix="u1")
    assert rows[0]["content"] == "用户喜欢被叫babe（档案称呼）"
    assert rows[0]["source"] == "user_stated"      # 人改过=人工核准
    assert rows[0]["has_embedding"] is False        # 旧向量按旧语义召回=错，清空


def test_edit_duplicate_content_rejected(tmp_path):
    st = _store(tmp_path)
    st.add_fact("u1", "喜欢喝咖啡", "heuristic")
    rid2 = st.add_fact("u1", "喜欢遛狗", "heuristic")
    assert st.update_fact_content(rid2, "喜欢喝咖啡") is False
    rows = st.list_rows(prefix="u1")
    assert any(r["content"] == "喜欢遛狗" for r in rows)  # 原行不动


def test_bullets_label_inferred_only(tmp_path):
    """推断条目 prompt 内显式降级标注；用户明说条目零标注。"""
    st = _store(tmp_path)
    st.add_fact("u1", "用户不喜欢被反复称呼为babe", "llm", source="ai_inferred")
    st.add_fact("u1", "用户住在宿务", "heuristic", source="user_stated")
    text = st.get_bullets_for_prompt("u1")
    lines = {ln for ln in text.splitlines()}
    assert any("AI推断" in ln and "babe" in ln for ln in lines)
    assert any("宿务" in ln and "AI推断" not in ln for ln in lines)


def test_memory_block_header_declares_profile_priority():
    """记忆块头必须声明「与档案矛盾以档案为准」（#24 推断教唆的通用面）。"""
    p = _mk_ai()._build_context_prompt({
        "last_message": "hi",
        "_episodic_memory_text": "- 用户不喜欢被反复称呼为babe（AI推断，可信度低于档案）",
    })
    assert "以档案为准" in p


def test_edit_route_registered():
    src = (_ENGINE_ROOT / "src" / "web" / "routes"
           / "episodic_identity_routes.py").read_text(encoding="utf-8")
    assert '@app.put("/api/episodic-memory/{row_id}")' in src
    assert "update_fact_content" in src
    assert "episodic_edit" in src  # 审计动作名


# ── #24 出站互换守卫（swap_vocative_peer_call） ─────────────────────────────

def _swap(text):
    from src.utils.persona_guard import swap_vocative_peer_call
    return swap_vocative_peer_call(text, "babe", "baba")


def test_swap_incident_greeting_form_24():
    """事故原形：「good morning baba」无逗号问候呼格 → 换回 babe。"""
    out, hits = _swap("Good morning baba")
    assert out == "Good morning babe" and hits


def test_swap_comma_vocative_forms_24():
    out1, h1 = _swap("I miss you so much, baba.")
    assert "babe" in out1 and "baba" not in out1 and h1
    out2, h2 = _swap("baba，你睡了吗")
    assert out2.startswith("babe") and h2


def test_swap_sentence_initial_form_24():
    out, hits = _swap("baba 今天想我了没有呀")
    assert out.startswith("babe") and hits


def test_swap_never_touches_meta_or_possessive_24():
    """讨论称呼的元语句与所有格自指（我是你的baba）绝不动——换了才是穿帮。"""
    for s in ("你叫我 baba 的时候好可爱",
              "我是你的baba呀",
              "you can call me baba anytime",
              "别叫我 baba 啦"):
        out, hits = _swap(s)
        assert out == s, s
        assert not hits, s


def test_swap_noop_without_fields():
    from src.utils.persona_guard import swap_vocative_peer_call
    assert swap_vocative_peer_call("Good morning baba", "", "baba")[1] == []
    assert swap_vocative_peer_call("Good morning babe", "babe", "babe")[1] == []


def test_swap_wired_into_enforce():
    src = (_ENGINE_ROOT / "src" / "skills" / "skill_manager.py").read_text(
        encoding="utf-8")
    assert "swap_vocative_peer_call" in src
    assert "address_swap" in src  # 子开关
