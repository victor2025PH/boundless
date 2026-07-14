"""命理技能接线门禁：话题检测 / 粘性窗 / prompt 块 / skill_manager 注入 / 生辰落库 /
ai_client 消费。轻量绑定（免全量 init，与 test_selfie_wiring 同模式），全部离线。"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest

from src.companion.bazi_context import (
    build_bazi_prompt_block,
    build_birth_ask_directive,
    detect_bazi_topic,
    topic_active,
    touch_topic,
)

_SMcls = __import__(
    "src.skills.skill_manager", fromlist=["SkillManager"]
).SkillManager


# ── 话题检测 ─────────────────────────────────────────────────────────────────

def test_detect_topic_positive():
    assert detect_bazi_topic("帮我算算八字呗")
    assert detect_bazi_topic("我今年运势怎么样")
    assert detect_bazi_topic("can you read my fortune?")
    assert detect_bazi_topic("幫我看看命盤")


def test_detect_topic_negative():
    assert not detect_bazi_topic("今天天气不错")
    assert not detect_bazi_topic("我不信这些算命的")
    assert not detect_bazi_topic("")
    assert not detect_bazi_topic("命" * 301)  # 超长不判


def test_topic_sticky_window():
    ctx = {}
    assert not topic_active(ctx, sticky_minutes=10)
    touch_topic(ctx, now=1000.0)
    assert topic_active(ctx, sticky_minutes=10, now=1000.0 + 9 * 60)
    assert not topic_active(ctx, sticky_minutes=10, now=1000.0 + 11 * 60)


# ── prompt 块 ────────────────────────────────────────────────────────────────

def test_prompt_block_contains_safety_and_tone():
    blk = build_bazi_prompt_block("四柱：乙亥 戊寅 乙未 庚辰")
    assert "命理参考" in blk
    assert "安全红线" in blk
    assert "绝不预言死亡" in blk
    assert "人设" in blk


def test_prompt_block_caveats():
    blk = build_bazi_prompt_block("四柱：…", hour_known=False, has_dayun=False)
    assert "时辰未知" in blk
    assert "大运未排" in blk
    full = build_bazi_prompt_block("四柱：…", hour_known=True, has_dayun=True)
    assert "时辰未知" not in full
    assert "大运未排" not in full


def test_prompt_block_empty_summary_empty():
    assert build_bazi_prompt_block("") == ""


def test_ask_directive_with_known_birthday():
    d = build_birth_ask_directive((3, 5))
    assert "3月5日" in d and "出生年份" in d
    d2 = build_birth_ask_directive(None)
    assert "出生年月日" in d2
    assert "复述" in d2  # 引导 AI 确认复述 → 触发 capture 落库


# ── skill_manager 注入（轻量绑定） ─────────────────────────────────────────────

class _Store:
    """最小 episodic store stub：list_rows / add_fact。"""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.added = []

    def list_rows(self, prefix="", limit=100, source=""):
        return [{"content": c} for c in self.rows]

    def add_fact(self, key, content, category, source=""):
        self.added.append((key, content, source))
        self.rows.insert(0, content)  # 新事实靠前（created_at DESC 语义）
        return len(self.added)


class _SM:
    _bazi_cfg = _SMcls._bazi_cfg
    resolve_birth_info = _SMcls.resolve_birth_info
    resolve_birthday = _SMcls.resolve_birthday
    _inject_bazi_context = _SMcls._inject_bazi_context
    _capture_birth_info_fact = _SMcls._capture_birth_info_fact
    _complete_birth_gender = _SMcls._complete_birth_gender
    _bazi_entitlement = _SMcls._bazi_entitlement
    _bazi_deep_allowed = _SMcls._bazi_deep_allowed
    _bazi_upsell_block = _SMcls._bazi_upsell_block
    _record_bazi_funnel = _SMcls._record_bazi_funnel
    _ritual_daily_card_line = _SMcls._ritual_daily_card_line
    _monetization_gate_enabled = _SMcls._monetization_gate_enabled

    def __init__(self, *, bazi_cfg=None, rows=None, gate=False):
        comp = {}
        if bazi_cfg is not None:
            comp["bazi"] = bazi_cfg
        mon = {"enabled": True, "gate": {"enabled": True}} if gate else {}
        self.config = SimpleNamespace(
            config={"companion": comp, "monetization": mon})
        self.logger = logging.getLogger("test_bazi")
        self._episodic_store = _Store(rows)
        self._memory_cfg = {"scope": "user"}
        self._cpi = None
        self._user_ctx = {}

    def _episodic_storage_key(self, user_id_str, chat_id, platform=""):
        return f"u:{user_id_str}"

    async def _episodic_patch_embedding(self, rid, fact):
        return None

    def _get_user_context(self, user_id):
        return self._user_ctx

    def _get_persona_name_for_context(self, user_context):
        return "小雨"


_ON = {"enabled": True, "topic_sticky_minutes": 10, "ask_cooldown_hours": 24}
_FACT = "用户的出生信息：公历1995年3月5日 8时30分出生 性别女"


def test_inject_disabled_noop():
    sm = _SM(bazi_cfg={"enabled": False})
    ctx = {}
    sm._inject_bazi_context(ctx, "帮我算八字", "u1", "c1")
    assert "_bazi_block" not in ctx


def test_inject_off_topic_noop_and_clears_stale():
    sm = _SM(bazi_cfg=_ON)
    ctx = {"_bazi_block": "残留"}
    sm._inject_bazi_context(ctx, "今天天气不错", "u1", "c1")
    assert "_bazi_block" not in ctx


def test_inject_topic_known_birth_gets_chart_block():
    pytest.importorskip("lunar_python")
    sm = _SM(bazi_cfg=_ON, rows=[_FACT])
    ctx = {}
    sm._inject_bazi_context(ctx, "帮我看看八字", "u1", "c1")
    blk = ctx.get("_bazi_block") or ""
    assert "命理参考" in blk
    assert "乙亥" in blk  # 真排盘
    assert "安全红线" in blk


def test_inject_sticky_followup_without_keyword():
    pytest.importorskip("lunar_python")
    sm = _SM(bazi_cfg=_ON, rows=[_FACT])
    ctx = {}
    sm._inject_bazi_context(ctx, "帮我看看八字", "u1", "c1")
    assert "_bazi_block" in ctx
    # 追问无关键词，但在粘性窗内 → 继续注入
    sm._inject_bazi_context(ctx, "那我明年怎么样", "u1", "c1")
    assert "命理参考" in (ctx.get("_bazi_block") or "")


def test_inject_same_turn_birth_in_message_charts_immediately():
    """消息自带完整生辰（「帮我算八字，我1995年3月5日早上8点半生的」）→ 当轮排盘，
    不注入采集 directive（别让 AI 明知故问）。"""
    pytest.importorskip("lunar_python")
    sm = _SM(bazi_cfg=_ON)  # 记忆里没有生辰
    ctx = {}
    sm._inject_bazi_context(
        ctx, "帮我算八字，我1995年3月5日早上8点半出生的", "u1", "c1")
    blk = ctx.get("_bazi_block") or ""
    assert "命理参考" in blk and "乙亥" in blk
    assert "需要生辰" not in blk


def test_inject_missing_birth_asks_with_cooldown():
    sm = _SM(bazi_cfg=_ON)
    ctx = {}
    sm._inject_bazi_context(ctx, "帮我算算命", "u1", "c1")
    blk = ctx.get("_bazi_block") or ""
    assert "需要生辰" in blk
    # 冷却内再问 → 不再注入采集 directive（不逼问）
    sm._inject_bazi_context(ctx, "快帮我算算八字嘛", "u1", "c1")
    assert "_bazi_block" not in ctx


def test_inject_ask_mentions_known_birthday():
    sm = _SM(bazi_cfg=_ON, rows=["用户的生日：3月5日"])
    ctx = {}
    sm._inject_bazi_context(ctx, "帮我算算命", "u1", "c1")
    blk = ctx.get("_bazi_block") or ""
    assert "3月5日" in blk  # 体现「记得你生日」


def test_resolve_birth_info_newest_wins():
    sm = _SM(bazi_cfg=_ON, rows=[
        "用户的出生信息：公历1995年3月5日 8时30分出生 性别女",  # 新（含时辰）
        "用户的出生信息：公历1995年3月5日 时辰未知出生",          # 旧
    ])
    info = sm.resolve_birth_info("u:u1")
    assert info is not None and info.hour == 8


# ── capture 落库 ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_capture_writes_fact_and_dedups():
    sm = _SM(bazi_cfg=_ON)
    await sm._capture_birth_info_fact(
        "u1", "1995年3月5日早上8点半，女生",
        "记住啦，你是1995年3月5日早上8点半出生的", "c1")
    assert len(sm._episodic_store.added) == 1
    _, fact, source = sm._episodic_store.added[0]
    assert source == "user_stated"
    assert "1995年3月5日" in fact and "8时30分" in fact and "性别女" in fact
    # 同信息再来一轮 → 幂等不重复落库
    await sm._capture_birth_info_fact(
        "u1", "我是1995年3月5日早上8点半出生的女生", "好的", "c1")
    assert len(sm._episodic_store.added) == 1


@pytest.mark.asyncio
async def test_capture_disabled_noop():
    sm = _SM(bazi_cfg={"enabled": False})
    await sm._capture_birth_info_fact(
        "u1", "我1995年3月5日出生", "记住啦你是1995年3月5日出生的", "c1")
    assert sm._episodic_store.added == []


@pytest.mark.asyncio
async def test_capture_inherits_known_gender():
    """补时辰的新事实缺性别 → 继承已知性别，防画像降级。"""
    sm = _SM(bazi_cfg=_ON, rows=["用户的出生信息：公历1995年3月5日 时辰未知出生 性别女"])
    await sm._capture_birth_info_fact(
        "u1", "对了我是早上8点出生的", "记住啦，你是1995年3月5日早上8点出生的", "c1")
    assert len(sm._episodic_store.added) == 1
    assert "性别女" in sm._episodic_store.added[0][1]


@pytest.mark.asyncio
async def test_gender_completion_in_topic_window():
    """报完生辰后下一轮才说性别 → 话题窗内补录（解锁大运）；窗外闲聊不采。"""
    from src.companion.bazi_context import touch_topic
    sm = _SM(bazi_cfg=_ON, rows=["用户的出生信息：公历1995年3月5日 8时30分出生"])
    # 话题窗外：不补录
    await sm._capture_birth_info_fact("u1", "我是女生", "好呀", "c1")
    assert sm._episodic_store.added == []
    # 话题窗内：补录性别
    touch_topic(sm._user_ctx)
    await sm._capture_birth_info_fact("u1", "我是女生", "好呀", "c1")
    assert len(sm._episodic_store.added) == 1
    assert "性别女" in sm._episodic_store.added[0][1]
    # 复解析：新事实胜出且带性别
    info = sm.resolve_birth_info("u:u1")
    assert info.gender == "female" and info.hour == 8


# ── Phase 2：每日灵签注入 ─────────────────────────────────────────────────────

def test_inject_daily_card_with_chart():
    pytest.importorskip("lunar_python")
    sm = _SM(bazi_cfg=_ON, rows=[_FACT])
    ctx = {}
    sm._inject_bazi_context(ctx, "帮我看看今日运势", "u1", "c1")
    blk = ctx.get("_bazi_block") or ""
    assert "今日灵签" in blk
    assert "对TA日主为" in blk  # 有盘 → 个性化能量日


def test_inject_daily_card_generic_no_birth_no_ask():
    """没生辰纯问「抽个签」→ 通用签照出，且**不**逼问生辰（轻场景轻处理）。"""
    pytest.importorskip("lunar_python")
    sm = _SM(bazi_cfg=_ON)
    ctx = {}
    sm._inject_bazi_context(ctx, "抽个签看看", "u1", "c1")
    blk = ctx.get("_bazi_block") or ""
    assert "今日灵签" in blk and "通用签" in blk
    assert "需要生辰" not in blk


def test_inject_daily_card_off_by_config():
    pytest.importorskip("lunar_python")
    sm = _SM(bazi_cfg=dict(_ON, daily_card=False))
    ctx = {}
    sm._inject_bazi_context(ctx, "抽个签看看", "u1", "c1")
    assert "_bazi_block" not in ctx


# ── Phase 2：所问年份流年数据 ──────────────────────────────────────────────────

def test_inject_target_year_liunian_line():
    """「2027年运势」→ 盘面附 2027 丁未流年真数据（防 LLM 编干支）。"""
    pytest.importorskip("lunar_python")
    sm = _SM(bazi_cfg=_ON, rows=[_FACT])
    ctx = {}
    sm._inject_bazi_context(ctx, "帮我看看2027年运势怎么样", "u1", "c1")
    blk = ctx.get("_bazi_block") or ""
    assert "所问2027年流年：丁未" in blk
    assert "食神" in blk  # 丁对乙日主


def test_extract_target_year_pure():
    from src.companion.bazi_context import extract_target_year
    assert extract_target_year("明年怎么样", 2026) == 2027
    assert extract_target_year("大后年呢", 2026) == 2029
    assert extract_target_year("2031年运势", 2026) == 2031
    assert extract_target_year("看看我的八字", 2026) is None
    assert extract_target_year("我1995年出生", 2026) == 1995  # 年份词面命中（由生辰路径先行消化）


def test_birth_year_not_mistaken_as_target_year():
    """同轮报生辰（「我1995年3月5日早上8点生的」）→ 1995 不当成所问流年。"""
    pytest.importorskip("lunar_python")
    sm = _SM(bazi_cfg=_ON)
    ctx = {}
    sm._inject_bazi_context(
        ctx, "帮我算八字，我1995年3月5日早上8点半出生的", "u1", "c1")
    blk = ctx.get("_bazi_block") or ""
    assert "命理参考" in blk
    assert "所问1995年流年" not in blk


# ── Phase 2：详批变现门控 ─────────────────────────────────────────────────────

_ENT_NONE = {"grants": [], "unlocked": []}
_ENT_OWNED = {"grants": [], "unlocked": ["bazi_reading"]}


def test_deep_reading_gate_off_full_depth():
    """变现门控关（默认）→ 详批直接放行（零破坏），且盘面带大运序列真数据。"""
    pytest.importorskip("lunar_python")
    sm = _SM(bazi_cfg=_ON, rows=[_FACT], gate=False)
    ctx = {"entitlement": _ENT_NONE}
    sm._inject_bazi_context(ctx, "帮我详批一下事业运", "u1", "c1")
    blk = ctx.get("_bazi_block") or ""
    assert "详批模式" in blk
    assert "详批引导" not in blk
    assert "大运序列：" in blk  # 详批时附大运真数据（_FACT 带性别女 → 可排）


def test_deep_reading_gate_on_locked_upsell():
    """门控开 + 未解锁 → 免费大方向 + 软引导（含目录报价话术），绝不硬拒；
    且详批级数据（大运序列）不进盘面——指令与数据同口径。"""
    pytest.importorskip("lunar_python")
    sm = _SM(bazi_cfg=_ON, rows=[_FACT], gate=True)
    ctx = {"entitlement": _ENT_NONE}
    sm._inject_bazi_context(ctx, "仔细讲讲我的财运", "u1", "c1")
    blk = ctx.get("_bazi_block") or ""
    assert "详批引导" in blk
    assert "命理详批" in blk  # upsell_pitch_hint 带出目录 label
    assert "详批模式" not in blk
    assert "大运序列：" not in blk  # 未解锁不给详批级数据


def test_deep_reading_gate_on_owned_full_depth():
    pytest.importorskip("lunar_python")
    sm = _SM(bazi_cfg=_ON, rows=[_FACT], gate=True)
    ctx = {"entitlement": _ENT_OWNED}
    sm._inject_bazi_context(ctx, "帮我详批一下姻缘", "u1", "c1")
    blk = ctx.get("_bazi_block") or ""
    assert "详批模式" in blk


def test_upsell_cooldown_no_repeat_pitch():
    """软引导有冷却：冷却窗内反复求详批 → 只给免费盘面，不复读推销。"""
    pytest.importorskip("lunar_python")
    sm = _SM(bazi_cfg=_ON, rows=[_FACT], gate=True)
    ctx = {"entitlement": _ENT_NONE}
    sm._inject_bazi_context(ctx, "仔细讲讲我的财运", "u1", "c1")
    assert "详批引导" in (ctx.get("_bazi_block") or "")
    sm._inject_bazi_context(ctx, "帮我详批一下事业运", "u1", "c1")
    blk2 = ctx.get("_bazi_block") or ""
    assert "命理参考" in blk2      # 免费盘面照给
    assert "详批引导" not in blk2  # 不复读推销


def test_no_deep_intent_no_gate_texts():
    """普通命理闲聊不触发详批/引导块（免费体验不被污染）。"""
    pytest.importorskip("lunar_python")
    sm = _SM(bazi_cfg=_ON, rows=[_FACT], gate=True)
    ctx = {"entitlement": _ENT_NONE}
    sm._inject_bazi_context(ctx, "帮我看看八字", "u1", "c1")
    blk = ctx.get("_bazi_block") or ""
    assert "详批模式" not in blk and "详批引导" not in blk


# ── Phase 2：晨安 ritual 灵签行 ────────────────────────────────────────────────

def test_ritual_card_line_with_birth_info():
    pytest.importorskip("lunar_python")
    sm = _SM(bazi_cfg=_ON, rows=[_FACT])
    line = sm._ritual_daily_card_line("u:u1")
    assert "顺手" in line and "一句带过" in line


def test_ritual_card_line_gates():
    pytest.importorskip("lunar_python")
    # 无生辰画像 → 不推
    assert _SM(bazi_cfg=_ON)._ritual_daily_card_line("u:u1") == ""
    # bazi 关 → 不推
    assert _SM(bazi_cfg={"enabled": False}, rows=[_FACT])._ritual_daily_card_line("u:u1") == ""
    # ritual 开关关 → 不推
    sm = _SM(bazi_cfg=dict(_ON, daily_card_in_ritual=False), rows=[_FACT])
    assert sm._ritual_daily_card_line("u:u1") == ""


# ── ai_client 消费 ────────────────────────────────────────────────────────────

def test_ai_client_consumes_bazi_block():
    from src.ai.ai_client import AIClient

    class _Cfg:
        config_path = None
        config = {"web_admin": {"site_name": "T"}, "ai": {}}

        def get_ai_config(self):
            return {}

    client = AIClient(_Cfg())
    out = client._build_context_prompt({
        "channel": "telegram",
        "_bazi_block": "【命理参考 · 内部资料】\n四柱：乙亥 戊寅 乙未 庚辰",
    })
    assert "命理参考" in out
    out2 = client._build_context_prompt({"channel": "telegram"})
    assert "命理参考" not in out2
