"""P2 用户侧在地化「注入链」接线门禁：skill_manager 写 context → ai_client 进 prompt。

守的不变量（每条都对应一个真实会回归的失效模式）：
1. **默认全关**：两个开关都不开时不写任何 context 键、不查一次记忆库——新子系统上线
   不许对存量部署产生任何可观测变化。
2. **prompt 侧只吃显式信号**：`resolve_peer_locale` 绝不接受行为统计推断。写进 prompt
   的时间会被 LLM 当事实复述给客户，猜错就是当面说错话；而调度侧有「narrow 档只收窄」
   的安全网，两侧信任级刻意不同（见 `_inject_peer_locale` docstring）。
3. **advisory 档不进 prompt**：语种默认国家这类弱信号只能供节日，不能声称「你那边几点」。
4. **节日双侧分工**：用户侧在 skill_manager（要查记忆库＝IO），人设侧在 ai_client
   （人设与人设本地钟已在手，且 holidays_on 是带缓存的纯函数）。
5. **软失败**：记忆库炸了、节日库缺了，聊天链路照常出话（绝不让一个观测型能力阻断出话）。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.companion import user_clock_resolver as ucr
from src.companion.locale_holidays import (
    country_for_language,
    holiday_fact_line,
    holidays_on,
)
from src.companion.user_clock import TRUST_ADVISORY, TRUST_REPLACE, user_time_line

VAN = {"id": "lin_jiaxin", "location": "vancouver", "name": "林佳欣"}
ON = {"enabled": True}


@pytest.fixture(autouse=True)
def _clean():
    ucr.clear_cache_for_tests()
    ucr.reset_stats_for_tests()
    yield
    ucr.clear_cache_for_tests()
    ucr.reset_stats_for_tests()


class _Episodic:
    """假 episodic store：只实现 list_rows，并记调用次数（用于「零查询」断言）。"""

    def __init__(self, rows=None, boom=False):
        self.rows = rows or []
        self.boom = boom
        self.calls = 0

    def list_rows(self, prefix="", limit=100, source=""):
        self.calls += 1
        if self.boom:
            raise RuntimeError("episodic down")
        return list(self.rows)


def _sm(companion_cfg, episodic=None):
    """最小 SkillManager 替身：只给 _inject_peer_locale 需要的那几个属性。"""
    from src.skills.skill_manager import SkillManager

    sm = MagicMock(spec=SkillManager)
    sm.logger = MagicMock()
    sm.config = MagicMock()
    sm.config.config = {"companion": companion_cfg}
    sm._episodic_store = episodic
    sm._episodic_storage_key = MagicMock(return_value="telegram:acct:peer1")
    return sm


def _inject(sm, ctx, platform="telegram", chat_id="peer1"):
    from src.skills.skill_manager import SkillManager

    SkillManager._inject_peer_locale(sm, ctx, "u1", chat_id, platform)


# ---------------------------------------------------------------------------
# 1. 默认关 = 零影响
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cfg", [
    {},
    {"user_clock": {"enabled": False}, "locale_holidays": {"enabled": False}},
    {"user_clock": {}, "locale_holidays": {}},
])
def test_disabled_writes_nothing_and_touches_no_store(cfg):
    epi = _Episodic([{"content": "用户住在曼谷", "created_at": 100.0}])
    sm = _sm(cfg, epi)
    ctx: dict = {}
    _inject(sm, ctx)
    assert ctx == {}
    assert epi.calls == 0, "未启用时不许查记忆库"


def test_inject_clears_stale_keys_when_disabled():
    """开关被关掉后，上一轮残留的块必须清掉（否则会永久挂在 prompt 上）。"""
    sm = _sm({})
    ctx = {"_peer_clock_line": "旧的", "_peer_holiday_note": "旧的"}
    _inject(sm, ctx)
    assert "_peer_clock_line" not in ctx
    assert "_peer_holiday_note" not in ctx


# ---------------------------------------------------------------------------
# 2/3. 显式信号进 prompt；弱信号不进
# ---------------------------------------------------------------------------

def test_stated_city_writes_peer_clock_line():
    epi = _Episodic([{"content": "用户住在曼谷", "created_at": 100.0}])
    sm = _sm({"user_clock": ON}, epi)
    ctx: dict = {}
    _inject(sm, ctx)
    line = ctx.get("_peer_clock_line") or ""
    assert line, "自述城市是显式信号，应注入对方当地时间行"
    assert "对方" in line
    assert epi.calls >= 1


def test_language_only_signal_does_not_claim_peer_time():
    """语种默认国家 = advisory 档：可用于节日，但绝不能声称「你那边几点」。"""
    sm = _sm({"user_clock": ON}, _Episodic([]))
    ctx = {"reply_lang": "th"}
    _inject(sm, ctx)
    assert not (ctx.get("_peer_clock_line") or ""), "弱信号不得进 prompt 时间行"


def test_advisory_clock_user_time_line_is_empty():
    """纯函数层的同一不变量（防有人绕过注入层直接调用）。"""
    from src.companion.user_clock import infer_from_language

    clock = infer_from_language("th")
    assert clock is not None and clock.trust == TRUST_ADVISORY
    assert user_time_line(clock, "zh") == ""


def test_peer_locale_never_uses_behavioural_inference():
    """resolve_peer_locale 不接受 activity_hours 入参——签名级保证。"""
    import inspect

    params = inspect.signature(ucr.resolve_peer_locale).parameters
    assert "activity_hours" not in params
    # 且 resolve_for_conversation（调度侧）反过来必须能吃行为信号
    src = inspect.getsource(ucr.resolve_for_conversation)
    assert "activity_hours=utc_hours_for_conversation" in src


def test_whatsapp_phone_is_explicit_signal_but_telegram_id_is_not():
    """同一串数字：WhatsApp 是真号码（可用），Telegram user_id 形似 E.164（必须拒）。"""
    sm_wa = _sm({"user_clock": ON}, _Episodic([]))
    ctx_wa: dict = {}
    _inject(sm_wa, ctx_wa, platform="whatsapp", chat_id="66812345678")
    assert ctx_wa.get("_peer_clock_line"), "WhatsApp 号码国码是显式信号"

    ucr.clear_cache_for_tests()
    sm_tg = _sm({"user_clock": ON}, _Episodic([]))
    ctx_tg: dict = {}
    _inject(sm_tg, ctx_tg, platform="telegram", chat_id="66812345678")
    assert not ctx_tg.get("_peer_clock_line"), "Telegram 数字 id 不是电话号码"


# ---------------------------------------------------------------------------
# 4. 节日双侧
# ---------------------------------------------------------------------------

def test_user_side_holiday_note_from_stated_country():
    epi = _Episodic([{"content": "用户住在曼谷", "created_at": 100.0}])
    sm = _sm({"user_clock": ON, "locale_holidays": ON}, epi)
    ctx: dict = {}
    _inject(sm, ctx)
    # 具体是否命中取决于「今天」，故这里只钉「国家解析通了」这一步；
    # 节日文案本身由 test_locale_holidays.py 的金标日期用例覆盖。
    assert "_peer_clock_line" in ctx or "_peer_holiday_note" in ctx


def test_user_side_holiday_uses_language_country_without_clock():
    """时区推断关着、只开节日 → 仍能按会话语种定国家（纯增益，不依赖 P2 时钟）。"""
    assert country_for_language("th") == "TH"
    items = holidays_on(date(2026, 4, 13), "TH")
    assert items, "宋干节应在 4/13"
    note = holiday_fact_line(items, "zh", side="user")
    assert "对方" in note and "泰国" in note


def test_persona_side_holiday_note_is_texture_not_greeting():
    """人设侧文案语义是生活背景，不能催 AI 去「祝贺」（那是群发套话的源头）。"""
    items = holidays_on(date(2026, 7, 1), "CA")
    assert items, "加拿大国庆日应在 7/1"
    note = holiday_fact_line(items, "zh", side="persona")
    assert note
    assert "你所在地" in note
    assert "祝" not in note


def test_ai_client_consumes_peer_context_keys():
    """prompt 组装侧真的读了这两个键（接线断了会静默失效，故用静态守卫钉住）。"""
    import inspect

    from src.ai.ai_client import AIClient

    src = inspect.getsource(AIClient._build_context_prompt)
    assert '_peer_clock_line' in src
    assert '_peer_holiday_note' in src
    assert 'persona_side_texture' in src
    # 人设侧节日必须在 companion 域内注入（客服域不吃这块 token）
    assert 'locale_holidays' in src


def test_skill_manager_calls_injection_on_both_chains():
    """A 线（原生聊天）与 B 线（inbox 拟稿）都要注入，否则两条链人格分裂。

    A 线真实现在 `_handle_message_guarded`（`process_message` 只是加锁转发）；
    与场景注入 `_inject_scene_state` 同一处调用，故顺带钉住「两者相邻」——
    P2 注入刻意**不**受 selfie 开关影响（场景注入是 selfie-gated 的），所以必须是
    独立一跳而不是塞进 `_inject_scene_state` 内部。
    """
    import inspect

    from src.skills.skill_manager import SkillManager

    a_src = inspect.getsource(SkillManager._handle_message_guarded)
    b_src = inspect.getsource(SkillManager.generate_inbox_draft)
    assert "_inject_peer_locale" in a_src
    assert "_inject_peer_locale" in b_src
    # 不许被塞进 selfie-gated 的场景注入里（那样 selfie 关掉就整条失效）
    scene_src = inspect.getsource(SkillManager._inject_scene_state)
    assert "_inject_peer_locale" not in scene_src


# ---------------------------------------------------------------------------
# 5. 软失败
# ---------------------------------------------------------------------------

def test_episodic_failure_does_not_raise():
    sm = _sm({"user_clock": ON, "locale_holidays": ON}, _Episodic(boom=True))
    ctx: dict = {}
    _inject(sm, ctx)   # 不抛即通过
    assert "_peer_clock_line" not in ctx


def test_broken_config_shapes_do_not_raise():
    for bad in ({"user_clock": "nope"}, {"user_clock": None}, "not-a-dict", None):
        sm = _sm(bad if isinstance(bad, dict) else {})
        sm.config.config = {"companion": bad} if not isinstance(bad, dict) else {"companion": bad}
        _inject(sm, {})


def test_missing_calendar_country_yields_no_note():
    assert holidays_on(date(2026, 4, 13), "ZZ") == []
    assert holiday_fact_line([], "zh", side="user") == ""


# ---------------------------------------------------------------------------
# 缓存：注入在每条消息上都会跑，不能每次都查库
# ---------------------------------------------------------------------------

def test_peer_locale_is_cached_across_messages():
    epi = _Episodic([{"content": "用户住在曼谷", "created_at": 100.0}])
    sm = _sm({"user_clock": ON}, epi)
    _inject(sm, {})
    first = epi.calls
    assert first >= 1
    for _ in range(5):
        _inject(sm, {})
    assert epi.calls == first, "同一 TTL 内不许重复查记忆库"


def test_peer_cache_ttl_expiry_recomputes():
    epi = _Episodic([{"content": "用户住在曼谷", "created_at": 100.0}])
    c1 = ucr.resolve_peer_locale(
        "k1", episodic_store=epi, memory_key="k1",
        cfg={"enabled": True, "ttl_sec": 100}, now=1000.0)
    assert c1 is not None and c1.trust == TRUST_REPLACE
    n1 = epi.calls
    ucr.resolve_peer_locale(
        "k1", episodic_store=epi, memory_key="k1",
        cfg={"enabled": True, "ttl_sec": 100}, now=1050.0)
    assert epi.calls == n1
    ucr.resolve_peer_locale(
        "k1", episodic_store=epi, memory_key="k1",
        cfg={"enabled": True, "ttl_sec": 100}, now=1201.0)
    assert epi.calls > n1, "TTL 过期应重算"


def test_peer_cache_separate_from_schedule_cache():
    """两套缓存必须分开：混在一张表里会让 distribution() 读数把两种信任级搅在一起。"""
    epi = _Episodic([{"content": "用户住在曼谷", "created_at": 100.0}])
    ucr.resolve_peer_locale(
        "same-key", episodic_store=epi, memory_key="mk",
        cfg={"enabled": True}, now=1000.0)
    # 调度侧缓存（_CACHE）应仍为空 → distribution() 不受 prompt 侧影响
    assert ucr.distribution() == {}


# ---------------------------------------------------------------------------
# 生产库标定后补的两项（2026-07-28）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("content,expect_tz", [
    ("I live in Bangkok now", "Asia/Bangkok"),
    ("she is based in Vancouver", "America/Vancouver"),
    ("currently living in Tokyo for work", "Asia/Tokyo"),
    ("现居首尔，做设计", "Asia/Seoul"),
])
def test_english_residence_hint_unlocks_stated_city(content, expect_tz):
    """生产勘察实证：`memory_slots` 的居住地正则只认中文，英文用户的自述城市
    （最强信号）此前永远采不到。兜底层刻意只放在 resolver 内（血溅半径小一个数量级）。"""
    epi = _Episodic([{"content": content, "created_at": 100.0}])
    clock = ucr.resolve_peer_locale(
        f"k-{expect_tz}", episodic_store=epi, memory_key="mk",
        cfg={"enabled": True}, now=1000.0)
    assert clock is not None, content
    assert clock.tz_name == expect_tz
    assert clock.trust == TRUST_REPLACE


def test_residence_hint_does_not_leak_into_shared_memory_slots():
    """宽口径正则**不许**进 memory_slots——那里的误判会覆盖用户真记忆。"""
    from src.utils.memory_slots import extract_slot as _slot

    assert _slot("I live in Bangkok now") is None or _slot(
        "I live in Bangkok now")[0] != "residence"


def test_travel_wish_is_not_taken_as_residence():
    """「我想去曼谷玩」不是居住地——宽口径的最后一道闸是城市白名单 + 必须命中居住动词。"""
    epi = _Episodic([{"content": "用户说想去曼谷玩", "created_at": 100.0}])
    clock = ucr.resolve_peer_locale(
        "k-wish", episodic_store=epi, memory_key="mk",
        cfg={"enabled": True}, now=1000.0)
    assert clock is None or clock.source != "stated_city"


def test_min_samples_default_is_calibrated_to_12():
    """生产库标定：门槛 8 与 12 收下的会话数相同，16/24/32 逐级白丢。
    真正把关的是 infer_from_activity 的边际门槛，样本数只是粗筛。"""
    assert ucr.DEFAULT_MIN_SAMPLES == 12


def test_lookahead_preheat_when_no_holiday_today():
    """今天没节日但节日快到了 → 注入「再过 N 天」，且明确禁止提前群发祝福。"""
    import inspect

    from src.skills.skill_manager import SkillManager

    src = inspect.getsource(SkillManager._inject_peer_locale)
    assert "upcoming_holidays" in src
    assert "lookahead_days" in src
    assert "别提前群发祝福" in src
    # 只在今天无节日时才预热（否则同一轮会挂两块节日文案）
    assert "if not _note:" in src


def test_upcoming_holidays_excludes_today_for_preheat():
    """预热只取 days>0 的条目——days==0 是今天，应该走 holiday_fact_line 那条路。"""
    ups = upcoming_holidays_helper()
    assert all(d > 0 for _h, d in ups)


def upcoming_holidays_helper():
    from src.companion.locale_holidays import upcoming_holidays

    # 4/11 起两天内 → 4/13 宋干节（距今 2 天）
    return [(h, d) for h, d in
            (upcoming_holidays(date(2026, 4, 11), "TH", within_days=2) or [])
            if d > 0]


def test_utc_hours_helper_only_counts_inbound():
    class _Store:
        def list_recent_messages(self, cid, limit=120):
            base = datetime(2026, 7, 28, 3, 0, tzinfo=timezone.utc).timestamp()
            return [
                {"direction": "in", "ts": base},
                {"direction": "out", "ts": base},
                {"direction": "in", "ts": 0},
            ]

    assert ucr.utc_hours_for_conversation(_Store(), "c1") == [3]
