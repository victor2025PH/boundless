# -*- coding: utf-8 -*-
"""人工「客户情绪」标注 → AI 转向（P1-198 续，2026-08-02）门禁。

守护三件事：
1. **仲裁矩阵**（effective_mood 纯函数）：TTL / 标签在场校验 / 历史标注惰性 /
   不对称覆写（「低落」全链更谨慎，「开朗」只调语气绝不解锁闸门）。
2. **词汇成员资格**（防漂移）：effective_mood 输出的词必须真的被各消费方词表
   认账——GATE_EMOTION_NEG ∈ wellbeing_guard 负面集（且 gate 在强度未知时保守
   soft，这是 proactive 接线依赖的行为）、HINT_EMOTION_NEG/POS 与 goals
   is_negative_emotion 语义一致。
3. **写入口双写**（apply_mood_tag + InboxStore 往返）：conv_tags 投影组内互斥、
   attention 组不碰 arbitration 列、坐席删标签立即停用转向。
"""
from __future__ import annotations

import time

import pytest

from src.inbox.effective_mood import (
    GATE_EMOTION_NEG,
    HINT_EMOTION_NEG,
    HINT_EMOTION_POS,
    MANUAL_NEG_TAG,
    MANUAL_POS_TAG,
    MOOD_TAGS,
    MOOD_TAGS_ATTENTION,
    MOOD_TAGS_EMOTION,
    agent_mood_directive,
    apply_mood_tag,
    gate_emotion_override,
    manual_mood_state,
    manual_negative_active,
    merge_agent_instruction,
    merge_mood_tag,
    resolve_mood_steering_cfg,
    tone_hint_override,
)

NOW = 1_800_000_000.0
TTL = 24.0


def _meta(tag=MANUAL_NEG_TAG, *, age_h=1.0, ts=None, tags=None, by="agent1"):
    """构造 conversation_meta 形状（conv_tags 用 JSON 串——与 get_conv_meta 原样一致）。"""
    import json
    if ts is None:
        ts = NOW - age_h * 3600.0
    if tags is None:
        tags = [tag] if tag else []
    return {
        "mood_manual": tag,
        "mood_manual_ts": ts,
        "mood_manual_by": by,
        "conv_tags": json.dumps(tags, ensure_ascii=False),
    }


# ── 1) merge_mood_tag：组内互斥、跨组共存、非情绪标签原语义 ──────────────────

def test_merge_emotion_group_exclusive():
    tags = merge_mood_tag([], MANUAL_NEG_TAG)
    tags = merge_mood_tag(tags, MANUAL_POS_TAG)
    assert MANUAL_POS_TAG in tags and MANUAL_NEG_TAG not in tags


def test_merge_attention_group_exclusive():
    tags = merge_mood_tag(["需要关注"], "进展顺利")
    assert tags == ["进展顺利"]


def test_merge_cross_group_coexist_and_order():
    tags = merge_mood_tag(["VIP"], MANUAL_NEG_TAG)
    tags = merge_mood_tag(tags, "需要关注")
    assert tags == ["VIP", MANUAL_NEG_TAG, "需要关注"]
    # 换情绪：情绪组旧值剔除、attention 与普通标签不动，新值追加在尾（后打的胜出）
    tags = merge_mood_tag(tags, MANUAL_POS_TAG)
    assert tags == ["VIP", "需要关注", MANUAL_POS_TAG]


def test_merge_non_mood_tag_appends_dedup():
    assert merge_mood_tag(["VIP"], "VIP") == ["VIP"]
    assert merge_mood_tag(["VIP"], "老客") == ["VIP", "老客"]
    assert merge_mood_tag(["VIP"], "") == ["VIP"]


# ── 2) manual_mood_state 仲裁矩阵 ────────────────────────────────────────────

def test_state_active_within_ttl():
    st = manual_mood_state(_meta(age_h=1.0), now=NOW, ttl_hours=TTL)
    assert st["active"] and st["tag"] == MANUAL_NEG_TAG and st["by"] == "agent1"


def test_state_legacy_ts_zero_never_activates():
    """上线前打的旧标签（无时间戳）永不激活——存量部署零行为突变。"""
    st = manual_mood_state(_meta(ts=0), now=NOW, ttl_hours=TTL)
    assert not st["active"]


def test_state_ttl_expired():
    st = manual_mood_state(_meta(age_h=25.0), now=NOW, ttl_hours=TTL)
    assert not st["active"]


def test_state_tag_removed_from_conv_tags_deactivates():
    """坐席从标签栏删掉情绪标签 = 立即停用转向（在场校验）。"""
    st = manual_mood_state(_meta(tags=["VIP"]), now=NOW, ttl_hours=TTL)
    assert not st["active"]


def test_state_attention_tag_not_eligible():
    st = manual_mood_state(_meta(tag="需要关注"), now=NOW, ttl_hours=TTL)
    assert not st["active"]


def test_state_conv_tags_accepts_list_form():
    m = _meta()
    m["conv_tags"] = [MANUAL_NEG_TAG]  # 测试/调用方可能给 list 而非 JSON 串
    assert manual_mood_state(m, now=NOW, ttl_hours=TTL)["active"]


# ── 3) 不对称覆写 ────────────────────────────────────────────────────────────

def test_gate_override_negative_only():
    assert gate_emotion_override(_meta(MANUAL_NEG_TAG), now=NOW, ttl_hours=TTL) == GATE_EMOTION_NEG
    assert gate_emotion_override(_meta(MANUAL_POS_TAG), now=NOW, ttl_hours=TTL) == ""
    assert gate_emotion_override(_meta(age_h=25.0), now=NOW, ttl_hours=TTL) == ""


def test_tone_hint_both_directions():
    assert tone_hint_override(_meta(MANUAL_NEG_TAG), now=NOW, ttl_hours=TTL) == HINT_EMOTION_NEG
    assert tone_hint_override(_meta(MANUAL_POS_TAG), now=NOW, ttl_hours=TTL) == HINT_EMOTION_POS
    assert tone_hint_override(_meta(ts=0), now=NOW, ttl_hours=TTL) == ""


def test_directive_both_directions_and_inactive_empty():
    assert "低落" in agent_mood_directive(_meta(MANUAL_NEG_TAG), now=NOW, ttl_hours=TTL)
    assert agent_mood_directive(_meta(MANUAL_POS_TAG), now=NOW, ttl_hours=TTL)
    assert agent_mood_directive(_meta(tags=[]), now=NOW, ttl_hours=TTL) == ""


def test_manual_negative_active_helper():
    assert manual_negative_active(_meta(MANUAL_NEG_TAG), now=NOW, ttl_hours=TTL)
    assert not manual_negative_active(_meta(MANUAL_POS_TAG), now=NOW, ttl_hours=TTL)


# ── 4) merge_agent_instruction：显式指令优先 ─────────────────────────────────

def test_merge_instruction_priority_and_cap():
    d = "【坐席标注·客户状态】对方当前情绪低落"
    assert merge_agent_instruction("", d) == d
    assert merge_agent_instruction("回复要简短", d) == "回复要简短\n" + d
    # 放不下 → 丢标注句、绝不截显式指令
    long_instr = "很长的显式指令" * 60  # > 400 - len(d)
    merged = merge_agent_instruction(long_instr, d, cap=400)
    assert merged == long_instr[:400] and d not in merged
    assert merge_agent_instruction("指令", "") == "指令"


# ── 5) 词汇成员资格（防漂移：仲裁词必须被消费方认账）──────────────────────

def test_gate_vocab_recognized_by_wellbeing_guard():
    from src.utils.wellbeing_guard import _NEGATIVE_EMOTIONS, proactive_emotion_gate
    assert GATE_EMOTION_NEG in _NEGATIVE_EMOTIONS
    # proactive 接线依赖的行为：负面标签 + 强度未知(-1/None) → 保守 soft
    assert proactive_emotion_gate(
        None, now=NOW, last_emotion=GATE_EMOTION_NEG,
        last_emotion_intensity=None) == "soft"


def test_hint_vocab_recognized_by_goals():
    from src.companion.goals.service import is_negative_emotion
    assert is_negative_emotion(HINT_EMOTION_NEG)
    assert not is_negative_emotion(HINT_EMOTION_POS)


def test_manual_tags_align_with_mood_groups():
    assert (MANUAL_NEG_TAG, MANUAL_POS_TAG) == MOOD_TAGS_EMOTION
    assert MOOD_TAGS == MOOD_TAGS_EMOTION + MOOD_TAGS_ATTENTION


# ── 6) apply_mood_tag + InboxStore 往返（双写/互斥/删除停用）────────────────

@pytest.fixture
def store(tmp_path):
    from src.inbox.store import InboxStore
    st = InboxStore(tmp_path / "inbox.db")
    yield st
    st.close()


CID = "telegram:acct1:12345"


def test_apply_roundtrip_activates_steering(store):
    out = apply_mood_tag(store, CID, MANUAL_NEG_TAG, by="op7", now=NOW)
    assert out["tags"] == [MANUAL_NEG_TAG]
    meta = store.get_conv_meta(CID)
    assert meta["mood_manual"] == MANUAL_NEG_TAG
    assert meta["mood_manual_by"] == "op7"
    st = manual_mood_state(meta, now=NOW + 60, ttl_hours=TTL)
    assert st["active"]


def test_apply_remark_replaces_emotion(store):
    apply_mood_tag(store, CID, MANUAL_NEG_TAG, now=NOW)
    apply_mood_tag(store, CID, MANUAL_POS_TAG, now=NOW + 10)
    tags = store.get_conv_tags(CID)
    assert MANUAL_POS_TAG in tags and MANUAL_NEG_TAG not in tags
    meta = store.get_conv_meta(CID)
    assert meta["mood_manual"] == MANUAL_POS_TAG
    assert gate_emotion_override(meta, now=NOW + 20, ttl_hours=TTL) == ""


def test_apply_attention_does_not_touch_arbitration(store):
    apply_mood_tag(store, CID, MANUAL_NEG_TAG, now=NOW)
    apply_mood_tag(store, CID, "需要关注", now=NOW + 10)
    meta = store.get_conv_meta(CID)
    assert meta["mood_manual"] == MANUAL_NEG_TAG  # attention 组不写 arbitration 列
    assert sorted(store.get_conv_tags(CID)) == sorted([MANUAL_NEG_TAG, "需要关注"])
    assert manual_mood_state(meta, now=NOW + 20, ttl_hours=TTL)["active"]


def test_agent_removing_tag_deactivates(store):
    apply_mood_tag(store, CID, MANUAL_NEG_TAG, now=NOW)
    store.set_conv_tags(CID, ["VIP"])  # 坐席在标签栏删掉情绪标签
    meta = store.get_conv_meta(CID)
    assert not manual_mood_state(meta, now=NOW + 60, ttl_hours=TTL)["active"]


# ── 7) proactive 快照覆写 helper ────────────────────────────────────────────

def test_snapshot_emotion_override_and_fallthrough():
    from src.companion.proactive_topic import _snapshot_emotion
    m = _meta(MANUAL_NEG_TAG)
    m["last_emotion"] = "满意"
    assert _snapshot_emotion(m, mood_ttl=TTL, now=NOW) == GATE_EMOTION_NEG
    # 转向关闭（ttl<=0）→ 纯机器口径
    assert _snapshot_emotion(m, mood_ttl=0, now=NOW) == "满意"
    # 「积极开朗」不覆写（不对称）
    p = _meta(MANUAL_POS_TAG)
    p["last_emotion"] = "焦虑"
    assert _snapshot_emotion(p, mood_ttl=TTL, now=NOW) == "焦虑"


# ── 7b) 确认/纠正一致性打点（agreement 观测，P2 第一步）────────────────────

def test_agreement_semantics_and_snapshot():
    from src.inbox.effective_mood import (
        mood_steering_snapshot,
        record_mood_agreement,
    )
    base = dict(mood_steering_snapshot().get("agreement") or {})

    def _delta(key):
        cur = dict(mood_steering_snapshot().get("agreement") or {})
        return int(cur.get(key, 0)) - int(base.get(key, 0))

    # 机器判负面 + 人工标低落 = agree；机器判非负 + 人工标低落 = disagree（纠正）
    record_mood_agreement(MANUAL_NEG_TAG, "焦虑")
    assert _delta("agree") == 1
    record_mood_agreement(MANUAL_NEG_TAG, "满意")
    assert _delta("disagree") == 1
    # 人工标开朗：机器非负 = agree、机器负面 = disagree
    record_mood_agreement(MANUAL_POS_TAG, "满意")
    assert _delta("agree") == 2
    record_mood_agreement(MANUAL_POS_TAG, "sad")
    assert _delta("disagree") == 2
    # 机器无判定 → unknown（不掺进 agree/disagree）
    record_mood_agreement(MANUAL_NEG_TAG, "")
    assert _delta("unknown") >= 1 and _delta("agree") == 2
    # 非情绪组标签不打点
    record_mood_agreement("需要关注", "焦虑")
    assert _delta("agree") == 2 and _delta("disagree") == 2
    # 配对计数留痕（machine→manual）
    pairs = mood_steering_snapshot().get("pairs") or {}
    assert pairs.get(f"焦虑→{MANUAL_NEG_TAG}", 0) >= 1


def test_apply_mood_tag_records_agreement(store):
    """打标瞬间按会话 meta 的机器判定落 agreement（best-effort，不影响打标）。"""
    from src.inbox.effective_mood import mood_steering_snapshot
    store.set_conv_tags(CID, [])  # 建 meta 行
    store._conn.execute(
        "UPDATE conversation_meta SET last_emotion='焦虑' WHERE conversation_id=?",
        (CID,))
    store._conn.commit()
    before = int((mood_steering_snapshot().get("agreement") or {}).get("agree", 0))
    apply_mood_tag(store, CID, MANUAL_NEG_TAG, now=NOW)
    after = int((mood_steering_snapshot().get("agreement") or {}).get("agree", 0))
    assert after == before + 1


# ── 7d) 五条消费链接线在场（防重构静默丢线：判据函数 + 消费打点成对出现）──

def test_consumer_wiring_all_channels_present():
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    wiring = {
        # A 线语气（process_message → emotion_guide / goals hint 透传）
        "src/skills/skill_manager.py": ("tone_hint_override", '"tone_a"'),
        # B 线拟稿指令（唯一入口 persona_reply）
        "src/inbox/persona_reply.py": ("agent_mood_directive", '"draft_directive"'),
        # 营销目标当日让路
        "src/companion/goals/service.py": ("manual_negative_active", '"goal_hold"'),
        # 主动触达闸门（快照情绪覆写，仅负向）
        "src/companion/proactive_topic.py": ("gate_emotion_override", '"proactive_gate"'),
        # 语音安抚加权
        "src/inbox/autosend_helpers.py": (
            "manual_negative_active", 'record_mood_consume("voice")'),
    }
    for rel, (fn, mark) in wiring.items():
        src = (repo / rel).read_text(encoding="utf-8")
        assert fn in src, f"{rel} 丢了 {fn} 接线"
        assert mark in src, f"{rel} 丢了 {mark} 消费打点"


# ── 8) 迁移记账按内容哈希（2026-08-02 生产半迁移事故回归钉）────────────────

def test_migration_tracking_survives_midlist_insert(tmp_path, monkeypatch):
    """位置记账错位事故钉死：向 _MIGRATIONS **中段**插入新条目后重开库，新条目必须被应用。

    实锤：T-mood 三条 ALTER 相对历史列表形状发生索引错位，positional mig_id 撞上
    已记录的旧 id → 静默跳过 → 生产库半迁移（mood_manual/mood_manual_ts 缺失，
    转向功能静默失效）。记账键已改 SQL 内容哈希，本测试防再用回位置键。
    """
    from src.inbox import store as store_mod
    db = tmp_path / "mig.db"
    base = list(store_mod._MIGRATIONS)
    st = store_mod.InboxStore(db)
    st.close()
    new_sql = ("ALTER TABLE conversation_meta ADD COLUMN _mig_gate_probe "
               "TEXT NOT NULL DEFAULT ''")
    monkeypatch.setattr(store_mod, "_MIGRATIONS", base[:5] + [new_sql] + base[5:])
    st2 = store_mod.InboxStore(db)
    try:
        cols = [r[1] for r in st2._conn.execute(
            "PRAGMA table_info(conversation_meta)")]
        assert "_mig_gate_probe" in cols, "中段插入的迁移被位置记账跳过（错位事故复发）"
        assert st2.migration_errors == 0
    finally:
        st2.close()


# ── 9) 配置解析 ─────────────────────────────────────────────────────────────

def test_resolve_cfg_defaults_and_disable():
    assert resolve_mood_steering_cfg({}) == {"enabled": True, "ttl_hours": 24.0}
    cfg = {"inbox": {"next_actions": {"mood_steering": {
        "enabled": False, "ttl_hours": 6}}}}
    assert resolve_mood_steering_cfg(cfg) == {"enabled": False, "ttl_hours": 6.0}
    bad = {"inbox": {"next_actions": {"mood_steering": {"ttl_hours": -1}}}}
    assert resolve_mood_steering_cfg(bad)["ttl_hours"] == 24.0
