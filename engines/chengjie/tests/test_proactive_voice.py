"""Phase13 主动打招呼语音化单测：voice_gate 纯函数 + 配置语义。

全离线。发送链路（stage_voice_file/orch.send_media）各自已有套件覆盖
（test_voice_autosend / orchestrator 系列），此处守语音开场的决策语义。
"""
from __future__ import annotations

from src.companion.proactive_topic import voice_gate


def test_voice_gate_disabled_by_default():
    assert voice_gate({}, "早安呀，想你了", 0.0) is False
    assert voice_gate({"enabled": False, "probability": 1.0}, "早安呀", 0.0) is False


def test_voice_gate_probability():
    cfg = {"enabled": True, "probability": 0.5}
    assert voice_gate(cfg, "早安呀，想你了", 0.49) is True
    assert voice_gate(cfg, "早安呀，想你了", 0.50) is False
    assert voice_gate(cfg, "早安呀，想你了", 0.99) is False
    # probability=1.0 全语音；0 全文本
    assert voice_gate({"enabled": True, "probability": 1.0}, "早安呀，想你了", 0.999) is True
    assert voice_gate({"enabled": True, "probability": 0.0}, "早安呀，想你了", 0.0) is False


def test_voice_gate_length_band():
    cfg = {"enabled": True, "probability": 1.0, "min_chars": 4, "max_chars": 20}
    assert voice_gate(cfg, "早呀", 0.0) is False            # 太短（2 字）没内容
    assert voice_gate(cfg, "早安呀，想你了哦", 0.0) is True
    assert voice_gate(cfg, "这" * 21, 0.0) is False           # 超长回落文本
    assert voice_gate(cfg, "  ", 0.0) is False               # 空白
    # 概率越界钳制 [0,1]；脏配置不炸
    assert voice_gate({"enabled": True, "probability": 5}, "早安呀想你", 0.99) is True
    assert voice_gate({"enabled": True, "probability": "x"}, "早安呀想你", 0.0) is False


def test_voice_gate_defaults():
    """缺省参数：min 4 / max 80 / prob 0.5。"""
    cfg = {"enabled": True}
    assert voice_gate(cfg, "早安呀，今天想我了吗", 0.49) is True
    assert voice_gate(cfg, "早安呀，今天想我了吗", 0.51) is False
    assert voice_gate(cfg, "这" * 81, 0.0) is False


# ── opener 签名契约：planner 传的 kwargs 与 proactive_topic 包装必须同步 ──
# 真机事故（2026-07-13）：plan_proactive_sends 传 last_emotion_intensity，
# 而 proactive_topic._opener 包装缺该参数 → TypeError 被逐会话吞掉 →
# preview candidates 恒 0、主动开场静默失效。此处用「严格签名 opener」钉死契约：
# planner 若再加新 kwarg，本测试立即失败提醒同步三处包装（opener/ritual/milestone）。

def _strict_opener(*, memory_key, silent_hours, stage, intimacy,
                   last_emotion="", last_emotion_intensity=-1.0, contact_key="",
                   min_silent_hours=None):
    """与 proactive_topic._opener 相同的参数集（keyword-only、无 **kwargs）。"""
    return {"mode": "gentle_checkin", "directive": "问候一句", "fact": ""}


def test_planner_opener_kwargs_contract():
    import time as _t

    from src.integrations.companion_proactive import plan_proactive_sends

    # 钉死在当天中午：planner 含安静时段(23-8)过滤，now=time.time() 深夜跑必挂。
    _lt = _t.localtime()
    now = _t.mktime((_lt.tm_year, _lt.tm_mon, _lt.tm_mday, 12, 0, 0,
                     _lt.tm_wday, _lt.tm_yday, -1))
    convs = [{
        "conversation_id": "telegram:acc:1", "platform": "telegram",
        "account_id": "acc", "chat_key": "1",
        "last_ts": now - 10 * 3600.0, "last_direction": "out",
        "archived": False, "memory_key": "1", "stage": "warming",
        "intimacy": 30.0, "last_emotion": "calm", "last_emotion_intensity": 0.2,
    }]
    plans = plan_proactive_sends(
        convs, cooldown_map={}, opener_fn=_strict_opener,
        now=now, min_silent_hours=4.0, cooldown_hours=6.0,
        max_per_tick=3, quiet_start_hour=23.0, quiet_end_hour=8.0)
    # 严格签名 opener 未抛 TypeError → 会话成为候选（契约成立）
    assert len(plans) == 1 and plans[0]["mode"] == "gentle_checkin"


def test_ritual_planner_opener_kwargs_contract():
    import time as _t

    from src.utils.daily_ritual import plan_daily_rituals

    def _strict_ritual_opener(*, slot, memory_key, stage, intimacy,
                              last_emotion="", last_emotion_intensity=-1.0,
                              contact_key=""):
        return {"mode": f"ritual_{slot}", "directive": "道声早安"}

    # 固定在晨间窗口起点（本地 7 点）——无活跃样本时 target=窗口起点
    lt = _t.localtime()
    morning7 = _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 7, 0, 0,
                          lt.tm_wday, lt.tm_yday, -1))
    convs = [{
        "conversation_id": "telegram:acc:2", "platform": "telegram",
        "account_id": "acc", "chat_key": "2",
        "last_ts": morning7 - 9 * 3600.0, "last_direction": "out",
        "archived": False, "memory_key": "2", "stage": "steady",
        "intimacy": 55.0, "last_emotion": "", "last_emotion_intensity": 0.0,
    }]
    plans = plan_daily_rituals(
        convs, ritual_sent={}, opener_fn=_strict_ritual_opener,
        now=morning7, morning_window=(7, 10), night_window=(21, 24),
        min_intimacy=10.0, min_quiet_gap_hours=3.0, max_per_tick=5)
    assert len(plans) == 1 and plans[0]["mode"] == "ritual_morning"


def test_proactive_prompt_peer_language():
    """非中文会话 → prompt 硬性要求用对方语言写；中文/未知 → 不加约束。"""
    from src.utils.proactive_prompt import build_proactive_prompt

    plan = {"mode": "gentle_checkin", "directive": "问候一句"}
    p_en = build_proactive_prompt("小雨", plan, peer_language="en")
    assert "英语" in p_en and "绝不要用中文" in p_en
    p_ja = build_proactive_prompt("小雨", plan, peer_language="ja")
    assert "日语" in p_ja
    # 语言表未收录的代码 → 用代码本身，仍加约束
    p_xx = build_proactive_prompt("小雨", plan, peer_language="tl")
    assert "tl" in p_xx and "绝不要用中文" in p_xx
    for lang in ("zh", "zh-CN", "", "unknown"):
        p = build_proactive_prompt("小雨", plan, peer_language=lang)
        assert "绝不要用中文" not in p


def test_milestone_planner_opener_kwargs_contract():
    import time as _t

    from src.utils.milestone_ritual import plan_milestone_rituals

    def _strict_m_opener(*, event_type, event_label="", days=0, memory_key="",
                         stage="", intimacy=0.0, last_emotion="",
                         last_emotion_intensity=-1.0, contact_key=""):
        return {"mode": "milestone", "directive": f"庆祝{event_label}"}

    lt = _t.localtime()
    at10 = _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 10, 0, 0,
                      lt.tm_wday, lt.tm_yday, -1))
    convs = [{
        "conversation_id": "telegram:acc:3", "platform": "telegram",
        "account_id": "acc", "chat_key": "3",
        "last_ts": at10 - 5 * 3600.0, "last_direction": "out",
        "archived": False, "memory_key": "3", "stage": "steady",
        "intimacy": 60.0, "last_emotion": "", "last_emotion_intensity": 0.0,
        "first_seen_ts": at10 - 100 * 86400.0,  # 认识 100 天整
    }]
    plans = plan_milestone_rituals(
        convs, ritual_sent={}, opener_fn=_strict_m_opener,
        now=at10, greet_hour=10, min_intimacy=10.0, max_per_tick=5,
        anniversary_milestones=[100], holiday_calendar={})
    assert len(plans) == 1 and plans[0]["mode"] == "milestone"
