# -*- coding: utf-8 -*-
"""Telegram 原生语音通话纯函数核心门禁。

覆盖：配置解析 / 接听决策护栏（安全优先 + 永不冷场）/ 状态机合法转移 /
10ms 帧数学 / FramePacer 不漂移 / 拟人填充调度 / 流式 PCM 协议解析 / 安静时段。
"""
from src.voicecall.core import (
    BackchannelDecider,
    CallAction,
    CallEvent,
    CallState,
    CallsConfig,
    FramePacer,
    ThinkingFiller,
    evaluate_call_budget,
    decide_incoming_call,
    frame_bytes,
    is_quiet_hour,
    is_terminal,
    iter_length_prefixed_pcm,
    pick_supported_language,
    ring_delay_sec,
    silence_frame,
    split_pcm_frames,
    transition,
)


# ── 配置解析 ──────────────────────────────────────────────────────────────
def test_config_default_off():
    cfg = CallsConfig.from_config(None)
    assert cfg.enabled is False
    assert cfg.transport == "ntgcalls"
    assert cfg.brain == "s2s"          # 实测修正：现实默认 s2s（cascade 待专用流式克隆嘴 GPU）
    assert cfg.frame_ms == 10          # 铁律：默认 10ms 帧
    assert cfg.languages == ("zh", "en")


def test_config_parse_overlay():
    cfg = CallsConfig.from_config({
        "telegram_calls": {
            "enabled": True,
            "transport": "tg2sip",
            "brain": "s2s",
            "answer": {"min_intimacy": 55, "languages": ["zh", "en-US"],
                       "max_concurrent": 2, "require_auto_ai": False},
            "frame": {"sample_rate": 48000, "frame_ms": 10},
            "humanize": {"filler_after_ms": 800, "backchannel": False},
            "quiet_hours": {"enabled": True, "start_hour": 23, "end_hour": 8},
        }
    })
    assert cfg.enabled is True
    assert cfg.transport == "tg2sip"
    assert cfg.brain == "s2s"
    assert cfg.min_intimacy == 55
    assert cfg.languages == ("zh", "en")   # en-US 归一到 en
    assert cfg.max_concurrent == 2
    assert cfg.require_auto_ai is False
    assert cfg.backchannel is False


def test_config_invalid_enums_fallback():
    cfg = CallsConfig.from_config({"telegram_calls": {
        "transport": "carrier_pigeon", "brain": "telepathy"}})
    assert cfg.transport == "ntgcalls"
    assert cfg.brain == "s2s"          # 非法枚举回落现实默认 s2s


# ── 接听决策：安全/反风控优先，静默拒接不补偿 ───────────────────────────────
def _live_cfg(**kw):
    base = {"telegram_calls": {"enabled": True, "answer": {"min_intimacy": 30,
            "languages": ["zh", "en"], "max_concurrent": 1}}}
    base["telegram_calls"]["answer"].update(kw)
    return CallsConfig.from_config(base)


def test_decline_silent_when_disabled():
    d = decide_incoming_call(CallsConfig.from_config(None), has_conversation=True)
    assert d.action == CallAction.DECLINE_SILENT
    assert d.compensate is False


def test_decline_silent_group_chat():
    d = decide_incoming_call(_live_cfg(), chat_type="group", has_conversation=True)
    assert d.action == CallAction.DECLINE_SILENT
    assert d.reason == "not_private"


def test_decline_silent_stranger_no_compensation():
    # 陌生人（无会话）→ 静默拒接，绝不自动给陌生人发消息（风控特征）
    d = decide_incoming_call(_live_cfg(), chat_type="private", has_conversation=False)
    assert d.action == CallAction.DECLINE_SILENT
    assert d.reason == "stranger"
    assert d.compensate is False


def test_decline_silent_kill_switch():
    d = decide_incoming_call(_live_cfg(), has_conversation=True, peer_known=True,
                             kill_switch_frozen=True)
    assert d.action == CallAction.DECLINE_SILENT
    assert d.reason == "kill_switch"


def test_decline_silent_not_auto_ai():
    d = decide_incoming_call(_live_cfg(), has_conversation=True,
                             automation_mode="review")
    assert d.action == CallAction.DECLINE_SILENT
    assert d.reason == "not_auto_ai"


# ── 接听决策：够格熟人但此刻不便 → 必补偿（绝不冷场）───────────────────────────
def test_decline_compensate_low_intimacy():
    d = decide_incoming_call(_live_cfg(min_intimacy=50), has_conversation=True,
                             intimacy=20, conversation_language="zh")
    assert d.action == CallAction.DECLINE_COMPENSATE
    assert d.reason == "low_intimacy"
    assert d.compensate is True


def test_decline_compensate_language_unsupported():
    d = decide_incoming_call(_live_cfg(), has_conversation=True, intimacy=90,
                             conversation_language="th")
    assert d.action == CallAction.DECLINE_COMPENSATE
    assert d.reason == "language_unsupported"
    assert d.compensate is True


def test_decline_compensate_busy():
    d = decide_incoming_call(_live_cfg(max_concurrent=1), has_conversation=True,
                             intimacy=90, conversation_language="zh",
                             host_warm=True, concurrent_active=1)
    assert d.action == CallAction.DECLINE_COMPENSATE
    assert d.reason == "busy"
    assert d.compensate is True


def test_decline_compensate_quiet_hours_and_cold_host():
    d1 = decide_incoming_call(_live_cfg(), has_conversation=True, intimacy=90,
                              conversation_language="zh", quiet_hours=True)
    assert d1.reason == "quiet_hours" and d1.compensate is True
    d2 = decide_incoming_call(_live_cfg(), has_conversation=True, intimacy=90,
                              conversation_language="zh", host_warm=False)
    assert d2.reason == "host_cold" and d2.compensate is True


def test_accept_all_clear():
    d = decide_incoming_call(_live_cfg(min_intimacy=30), chat_type="private",
                             has_conversation=True, peer_known=True,
                             automation_mode="auto_ai", intimacy=75,
                             conversation_language="zh", host_warm=True,
                             concurrent_active=0, kill_switch_frozen=False,
                             quiet_hours=False)
    assert d.action == CallAction.ACCEPT
    assert d.reason == "ok"


def test_decision_order_safety_before_courtesy():
    # 群聊 + 低亲密度 + 冻结：安全维度（not_private/kill_switch）必须先于补偿维度命中
    d = decide_incoming_call(_live_cfg(min_intimacy=99), chat_type="group",
                             has_conversation=True, intimacy=0,
                             kill_switch_frozen=True)
    assert d.action == CallAction.DECLINE_SILENT
    assert d.reason == "not_private"   # chat_type 判定在最前


# ── 状态机 ──────────────────────────────────────────────────────────────────
def test_state_happy_path():
    s = CallState.IDLE
    for ev, expect in [
        (CallEvent.RING, CallState.RINGING),
        (CallEvent.ACCEPT, CallState.CONNECTING),
        (CallEvent.CONNECTED, CallState.LIVE),
        (CallEvent.WRAP, CallState.WRAPPING),
        (CallEvent.WRAP_DONE, CallState.ENDED),
    ]:
        s = transition(s, ev)
        assert s == expect
    assert is_terminal(s)


def test_state_illegal_transition_returns_none():
    assert transition(CallState.IDLE, CallEvent.ACCEPT) is None
    assert transition(CallState.LIVE, CallEvent.RING) is None
    assert transition(CallState.ENDED, CallEvent.ACCEPT) is None


def test_state_peer_hangup_any_active_to_ended():
    for st in (CallState.RINGING, CallState.CONNECTING, CallState.LIVE,
               CallState.WRAPPING):
        assert transition(st, CallEvent.PEER_HANGUP) == CallState.ENDED


def test_state_decline_from_ringing():
    assert transition(CallState.RINGING, CallEvent.DECLINE) == CallState.ENDED


# ── 帧数学（10ms）────────────────────────────────────────────────────────────
def test_frame_bytes_48k_10ms_mono():
    assert frame_bytes(48000, 10, 1, 2) == 960     # 480 采样 × 2 字节
    assert frame_bytes(48000, 20, 1, 2) == 1920
    assert frame_bytes(16000, 10, 1, 2) == 320


def test_split_pcm_frames_pads_last():
    fs = frame_bytes(48000, 10)      # 960
    pcm = b"\x01" * (fs + 100)       # 一帧多 100 字节
    frames = split_pcm_frames(pcm, fs)
    assert len(frames) == 2
    assert all(len(f) == fs for f in frames)        # 尾帧补齐
    assert frames[1][:100] == b"\x01" * 100
    assert frames[1][100:] == b"\x00" * (fs - 100)  # 静音填充


def test_split_pcm_frames_no_pad():
    fs = 960
    frames = split_pcm_frames(b"\x01" * (fs + 100), fs, pad_last=False)
    assert len(frames) == 1          # 不足的尾帧被丢弃


def test_split_pcm_empty():
    assert split_pcm_frames(b"", 960) == []


def test_silence_frame():
    assert silence_frame(960) == b"\x00" * 960


# ── FramePacer 不漂移 ────────────────────────────────────────────────────────
def test_frame_pacer_no_drift_over_1000_frames():
    p = FramePacer(10)               # 10ms/帧
    p.start(1000.0)
    # 第 1000 帧应发时刻 = 起点 + 10s，与逐帧累加无关（绝对锚定，不漂）
    assert abs(p.due(1000) - (1000.0 + 10.0)) < 1e-9
    # 若某帧处理慢了（now 已越过 due），sleep_for 返回 0 立即发、不追赶式狂发
    assert p.sleep_for(5, now=1000.20) == 0.0
    # 正常情况下 sleep_for = due - now
    assert abs(p.sleep_for(100, now=1000.5) - (1000.0 + 1.0 - 1000.5)) < 1e-9


def test_frame_pacer_lazy_start():
    p = FramePacer(10)
    # 未显式 start → 首次 sleep_for 以传入 now 为起点
    assert p.sleep_for(0, now=50.0) == 0.0
    assert abs(p.due(10) - (50.0 + 0.1)) < 1e-9


# ── 拟人：响铃延迟 + 语言归一 ─────────────────────────────────────────────────
def test_ring_delay_within_range():
    cfg = _live_cfg()
    assert ring_delay_sec(cfg, rand=lambda: 0.0) == cfg.ring_delay_min_sec
    assert ring_delay_sec(cfg, rand=lambda: 1.0) == cfg.ring_delay_max_sec
    mid = ring_delay_sec(cfg, rand=lambda: 0.5)
    assert cfg.ring_delay_min_sec <= mid <= cfg.ring_delay_max_sec


def test_pick_supported_language():
    assert pick_supported_language("zh") == "zh"
    assert pick_supported_language("en-US") == "en"
    assert pick_supported_language("th", default="zh") == "zh"
    assert pick_supported_language("", default="en") == "en"


# ── 拟人：思考填充调度 ────────────────────────────────────────────────────────
def test_thinking_filler_fires_after_threshold():
    cfg = _live_cfg()   # filler_after_ms=700, gap=2500 默认
    f = ThinkingFiller(cfg)
    f.on_user_turn_end(now=100.0)
    assert f.should_fill(now=100.3) is False   # 才 300ms，未到阈值
    assert f.should_fill(now=100.8) is True    # 800ms，触发
    # 立刻再问 → 距上次填充不足 gap，不重复填
    assert f.should_fill(now=100.9) is False


def test_thinking_filler_stops_when_reply_started():
    f = ThinkingFiller(_live_cfg())
    f.on_user_turn_end(now=0.0)
    assert f.should_fill(now=1.0, reply_started=True) is False
    f.on_reply_audio()
    assert f.should_fill(now=2.0) is False      # 已清空本轮


# ── 拟人：倾听反馈（backchannel）────────────────────────────────────────────
def test_backchannel_fires_and_caps():
    cfg = _live_cfg()   # after=3.5s, gap=4.0s, max=3 默认
    b = BackchannelDecider(cfg)
    b.on_user_speech_start(now=0.0)
    assert b.should_backchannel(now=2.0) is False   # 才 2s，未到 after
    assert b.should_backchannel(now=4.0) is True     # 4s 首插
    assert b.should_backchannel(now=5.0) is False    # 距上次不足 gap
    assert b.should_backchannel(now=8.5) is True      # 第二声
    assert b.should_backchannel(now=13.0) is True     # 第三声
    assert b.should_backchannel(now=18.0) is False    # 已达每轮上限 3


def test_backchannel_resets_on_turn_end():
    b = BackchannelDecider(_live_cfg())
    b.on_user_speech_start(now=0.0)
    assert b.should_backchannel(now=4.0) is True
    b.on_user_turn_end()
    assert b.should_backchannel(now=5.0) is False    # 轮结束，无正在进行的倾听


def test_backchannel_disabled():
    cfg = CallsConfig.from_config({"telegram_calls": {"enabled": True,
        "humanize": {"backchannel": False}}})
    b = BackchannelDecider(cfg)
    b.on_user_speech_start(now=0.0)
    assert b.should_backchannel(now=100.0) is False


# ── 流式 PCM 协议解析 ────────────────────────────────────────────────────────
def test_iter_length_prefixed_pcm_complete_and_partial():
    import struct
    b1 = b"\xaa" * 6
    b2 = b"\xbb" * 4
    stream = struct.pack("<I", len(b1)) + b1 + struct.pack("<I", len(b2)) + b2
    blocks, tail = iter_length_prefixed_pcm(stream)
    assert blocks == [b1, b2]
    assert tail == b""
    # 半个块：只给前缀+部分数据 → 该块留在 tail 等后续
    partial = struct.pack("<I", 10) + b"\x01\x02\x03"
    blocks2, tail2 = iter_length_prefixed_pcm(partial)
    assert blocks2 == []
    assert tail2 == partial


def test_iter_length_prefixed_pcm_zero_marker():
    import struct
    stream = struct.pack("<I", 0)     # 异常结束标记
    blocks, tail = iter_length_prefixed_pcm(stream)
    assert blocks == [b""]            # 空块＝停止信号
    assert tail == b""


# ── 安静时段（跨午夜）────────────────────────────────────────────────────────
def test_quiet_hours_cross_midnight():
    cfg = _live_cfg()   # 23→8
    assert is_quiet_hour(23, cfg) is True
    assert is_quiet_hour(2, cfg) is True
    assert is_quiet_hour(7, cfg) is True
    assert is_quiet_hour(8, cfg) is False      # 边界：end 不含
    assert is_quiet_hour(12, cfg) is False


def test_quiet_hours_disabled():
    cfg = CallsConfig.from_config({"telegram_calls": {"enabled": True,
        "quiet_hours": {"enabled": False}}})
    assert is_quiet_hour(3, cfg) is False


# ── 账号级通话预算/健康闸 ────────────────────────────────────────────────────
def _budget_cfg(**b):
    base = {"telegram_calls": {"enabled": True, "budget": {}}}
    base["telegram_calls"]["budget"].update(b)
    return CallsConfig.from_config(base)


def test_budget_config_defaults():
    cfg = _budget_cfg()
    assert cfg.daily_calls_cap == 20
    assert cfg.daily_minutes_cap == 60.0
    assert cfg.budget_block_on_red is True


def test_budget_allows_when_under_caps():
    v = evaluate_call_budget(_budget_cfg(daily_calls_cap=20, daily_minutes_cap=60),
                             calls_today=5, minutes_today=20.0, account_light="green")
    assert v.allowed is True and v.reason == "ok"


def test_budget_blocks_on_red_light():
    v = evaluate_call_budget(_budget_cfg(), calls_today=0, minutes_today=0.0,
                             account_light="red")
    assert v.allowed is False and v.reason == "account_unhealthy"


def test_budget_red_light_ignored_when_block_off():
    v = evaluate_call_budget(_budget_cfg(block_on_red=False), account_light="red")
    assert v.allowed is True


def test_budget_daily_calls_exhausted():
    v = evaluate_call_budget(_budget_cfg(daily_calls_cap=10), calls_today=10)
    assert v.allowed is False and v.reason == "daily_calls_exhausted"


def test_budget_daily_minutes_exhausted():
    v = evaluate_call_budget(_budget_cfg(daily_minutes_cap=60), minutes_today=61.0)
    assert v.allowed is False and v.reason == "daily_minutes_exhausted"


def test_budget_cap_zero_means_unlimited():
    v = evaluate_call_budget(_budget_cfg(daily_calls_cap=0, daily_minutes_cap=0),
                             calls_today=9999, minutes_today=9999.0)
    assert v.allowed is True
