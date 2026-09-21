# -*- coding: utf-8 -*-
"""个人微信 PC 副驾 · 纯逻辑与服务骨架门禁（实施97 线 B）。全部离线（假后端 + 假桥）。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Tuple

import pytest

from src.integrations.wechat_pc import BRIDGE_MODE, PLATFORM
from src.integrations.wechat_pc import identity as I
from src.integrations.wechat_pc import policy as P
from src.integrations.wechat_pc import risk_screens as R
from src.integrations.wechat_pc.backend import Bubble, FakeBackend, SessionRow, WeChatPcBackend
from src.integrations.wechat_pc.send_guard import GuardedSender, find_echo, verify_composer, verify_title
from src.integrations.wechat_pc.service import BridgeClient, WeChatPcService, bubble_fingerprint


def test_namespace_is_separate_from_wechat_kf():
    assert PLATFORM == "wechat" and BRIDGE_MODE == "desktop"


# ── policy ──────────────────────────────────────────────────────────────────

def test_policy_defaults_to_copilot_and_auto_needs_risk_ack():
    p = P.resolve_policy({})
    assert p.tier == P.TIER_COPILOT and not p.sends_allowed
    p2 = P.resolve_policy({"platform_login": {"wechat_pc": {"tier": "auto_reply"}}})
    assert p2.tier == P.TIER_SEMI, "无风险确认的全自动必须降到半自动"
    p3 = P.resolve_policy({"platform_login": {"wechat_pc": {"tier": "auto_reply", "risk_ack": True,
                                                           "work_hours": [9, 22], "daily_cap": 9999}}})
    assert p3.tier == P.TIER_AUTO_REPLY and p3.work_hours == (9, 22) and p3.daily_cap == 500
    assert P.resolve_policy({"platform_login": {"wechat_pc": {"tier": "nonsense"}}}).tier == P.TIER_COPILOT


def test_never_actions_are_hard_denied():
    for a in ("add_friend", "broadcast", "moments_post", "red_packet", "multi_instance", "inject",
              "AUTO_LOGIN", "join_group"):
        assert P.action_allowed(a) is False
    assert P.action_allowed("read_messages") and P.action_allowed("send_text_reply")
    assert P.action_allowed("send_voice_reply"), "语音回复是显式加白的动作"
    assert P.action_allowed("something_new") is False, "不在白名单即拒"


def test_kind_gating_by_tier():
    semi = P.resolve_policy({"platform_login": {"wechat_pc": {"tier": "semi"}}})
    auto = P.resolve_policy({"platform_login": {"wechat_pc": {"tier": "auto_reply", "risk_ack": True}}})
    assert P.kind_allowed(semi, "manual") and P.kind_allowed(semi, "approved")
    assert not P.kind_allowed(semi, "text"), "半自动不发未经人确认的自动稿"
    assert P.kind_allowed(auto, "text") and P.kind_allowed(auto, "manual")
    assert not P.kind_allowed(P.resolve_policy({}), "manual"), "副驾永不发"
    # 语音：仅全自动档；kind 绑定的白名单动作被摘掉时同样拒（防新 kind 绕白名单）
    assert P.kind_allowed(auto, "voice") and not P.kind_allowed(semi, "voice")
    assert not P.kind_allowed(P.resolve_policy({}), "voice")
    assert P.KIND_ACTIONS["voice"] == "send_voice_reply"
    orig = P.ALLOWED_ACTIONS
    try:
        P.ALLOWED_ACTIONS = frozenset(orig - {"send_voice_reply"})
        assert not P.kind_allowed(auto, "voice")
        assert P.kind_allowed(auto, "text")
    finally:
        P.ALLOWED_ACTIONS = orig


def test_may_send_judgement_order():
    auto = P.resolve_policy({"platform_login": {"wechat_pc": {"tier": "auto_reply", "risk_ack": True,
                                                              "work_hours": [8, 23]}}})
    now = datetime(2026, 9, 7, 10, 0, 0)
    base = dict(kind="text", now=now, connected_at=now.timestamp() - 30 * 86400, sent_today=0,
                sent_today_to_peer=0, last_sent_to_peer_ts=0.0,
                last_inbound_from_peer_ts=now.timestamp() - 60)
    assert P.may_send(auto, **base).allowed
    assert P.may_send(auto, **{**base, "now": datetime(2026, 9, 7, 2, 0)}).reason == "outside_work_hours"
    assert P.may_send(auto, **{**base, "last_inbound_from_peer_ts": 0.0}).reason == "no_inbound_from_peer"
    old = now.timestamp() - 80 * 3600
    assert P.may_send(auto, **{**base, "last_inbound_from_peer_ts": old}).reason == "inbound_too_old"
    assert P.may_send(auto, **{**base, "is_group": True}).reason == "group_not_mentioned"
    assert P.may_send(auto, **{**base, "is_group": True, "mentioned": True}).allowed
    # 新号预热期日上限 20
    fresh = {**base, "connected_at": now.timestamp() - 86400, "sent_today": 20}
    assert P.may_send(auto, **fresh).reason == "daily_cap"
    assert P.may_send(auto, **{**base, "sent_today": 20}).allowed, "成熟号日上限 80"
    assert P.may_send(auto, **{**base, "sent_today_to_peer": 15}).reason == "per_peer_daily_cap"
    assert P.may_send(auto, **{**base, "last_sent_to_peer_ts": now.timestamp() - 5}).reason == "min_gap"


# ── identity ────────────────────────────────────────────────────────────────

def test_identity_normalization_and_wxid_parse():
    assert I.normalize_display_name("  张 三  ") == "张 三"
    assert I.normalize_display_name("张三 12:30 好的，明天见") == "张三"
    assert I.normalize_display_name("Alice 昨天 ok") == "Alice"
    assert I.parse_wxid("微信号: zhang_san88") == "zhang_san88"
    assert I.parse_wxid("WeChat ID：alice-2024") == "alice-2024"
    assert I.parse_wxid("wxid_abc123def") == "wxid_abc123def"
    assert I.parse_wxid("13800138000") == "" and I.parse_wxid("你好") == ""


def test_chat_key_priority_and_group():
    assert I.make_chat_key(display_name="张三", wxid="微信号: zs_1") == "wx:id:zs_1"
    assert I.make_chat_key(display_name="张三", avatar_fp="abcd1234") == "wx:name:张三#abcd1234"
    assert I.make_chat_key(display_name="张三") == "wx:name:张三"
    assert I.make_chat_key(display_name="跨境交流群", is_group=True) == "wx:group:跨境交流群"
    assert I.make_chat_key(display_name="") == ""
    assert I.chat_key_kind("wx:id:x") == "id" and I.chat_key_kind("nope") == ""


def test_identity_cache_merges_rename_under_wxid():
    c = I.ChatIdentityCache()
    k1 = c.resolve(display_name="张三")
    assert k1 == "wx:name:张三" and c.needs_wxid_lookup(k1)
    k2 = c.resolve(display_name="张三", wxid="zhangsan_88")   # 裸 wxid 按官方 6–20 位规则
    assert k2 == "wx:id:zhangsan_88" and not c.needs_wxid_lookup(k2)
    # 我改了备注：同 wxid → 同 key，显示名更新
    k3 = c.resolve(display_name="张三-供应商", wxid="zhangsan_88")
    assert k3 == k2 and c.display_name_for(k2) == "张三-供应商"
    # 之后按新名再来（没传 wxid）也能对回同一 key
    assert c.resolve(display_name="张三-供应商") == k2
    assert c.resolve(display_name="群A", is_group=True) == "wx:group:群A"


# ── risk screens ────────────────────────────────────────────────────────────

def test_risk_screen_classification_order_and_dispositions():
    assert R.classify_pc_screen([], window_class="mmui::LoginWindow") == R.LOGGED_OUT
    assert R.classify_pc_screen(["进入WeChat", "切换账号", "仅传输文件"]) == R.LOGGED_OUT
    assert R.classify_pc_screen(["请在手机上确认登录"]) == R.PHONE_CONFIRM
    assert R.classify_pc_screen(["当前版本过低，请更新微信"]) == R.UPDATE_REQUIRED
    assert R.classify_pc_screen(["登录环境异常，为了你的账号安全"]) == R.ENV_ABNORMAL
    assert R.classify_pc_screen(["操作过于频繁，请稍后再试"]) == R.LIMIT
    assert R.classify_pc_screen(["账号已被封禁", "请稍后再试"]) == R.BAN, "ban 优先于 limit"
    assert R.classify_pc_screen(["你好", "在吗"]) == R.NONE
    d = R.disposition_for(R.LOGGED_OUT)
    assert d.freeze_sends and d.mark_offline and d.notify_owner and d.readonly
    lim = R.disposition_for(R.LIMIT)
    assert lim.freeze_sends and lim.freeze_ttl_sec == 7200 and not lim.readonly
    assert R.disposition_for(R.NONE).freeze_sends is False


# ── send guard ──────────────────────────────────────────────────────────────

def test_verify_helpers():
    assert verify_title("张三", "张三") and verify_title("跨境群(12)", "跨境群") and verify_title("跨境群（3）", "跨境群")
    assert not verify_title("李四", "张三") and not verify_title("", "张三")
    assert verify_composer("你好  世界", "你好 世界") and not verify_composer("你好", "你好吗") and not verify_composer("", "")
    # 微信输入框回读对表情的三种失真（真机实录）
    assert verify_composer("好，继续～ 你发啥我接啥 ?", "好，继续～ 你发啥我接啥 😊"), "原生表情回读成 ?"
    assert verify_composer("带表情\ufffc文本", "带表情[微笑]文本"), "表情码回读成 U+FFFC"
    assert verify_composer("两个 😀😀 emoji 和中文—破折号…省", "两个 😀😀 emoji 和中文—破折号…省略号"), "每个非BMP表情丢 1 尾字符"
    assert not verify_composer("两个 😀😀 emoji 和中文", "两个 😀😀 emoji 和中文—破折号…省略号"), "丢太多不算"
    assert not verify_composer("完全不同的话 😀", "两个 😀 emoji 的原话"), "不是前缀不算"
    assert not verify_composer("你好吗残留", "你好吗"), "残留仍拦"
    assert verify_composer("?", "😊") and not verify_composer("", "😊"), "全表情：输入框非空即可"
    before = [Bubble("你好", is_self=True, runtime_id="a")]
    after = before + [Bubble("你好", is_self=True, runtime_id="b")]
    assert find_echo(before, after, "你好").runtime_id == "b"
    assert find_echo(before, before, "你好") is None, "历史同文不能当送达证据"
    # 回显气泡的表情失真也按容忍规则认（真机：发「…😊」回显读成「…?」，此前被判 echo_not_found 还冻结 10 分钟）
    assert find_echo([], [Bubble("好，继续～ 你发啥我接啥 ?", is_self=True, runtime_id="c")], "好，继续～ 你发啥我接啥 😊") is not None
    # RuntimeId 复用（RecyclerListView）：新条目拿到旧 id 也算新增——只看同文己方气泡数量
    assert find_echo([Bubble("旧的", is_self=True, runtime_id="a")], [Bubble("你好", is_self=True, runtime_id="a")], "你好") is not None
    assert find_echo([], [Bubble("你好", is_self=False, runtime_id="z")], "你好") is None, "对方同文不算"
    nb = [Bubble("ok", is_self=True)]
    assert find_echo(nb, nb + [Bubble("ok", is_self=True)], "ok") is not None
    assert find_echo([], [Bubble("ok", is_self=False)], "ok") is None


def _sender(fb: FakeBackend) -> GuardedSender:
    return GuardedSender(fb, sleep=lambda s: None, read_pause_sec=0, echo_wait_sec=0, echo_retries=2)


def test_guarded_send_happy_path_and_each_failure_stage():
    fb = FakeBackend()
    fb.sessions = [SessionRow("张三")]
    out = _sender(fb).send("张三", "在的，稍等")
    assert out.ok and out.stage == "echo" and out.echo_text == "在的，稍等"
    assert fb.messages["张三"][-1].is_self and fb.composer == ""
    # open 失败
    fb2 = FakeBackend(); fb2.fail_open.add("李四")
    assert _sender(fb2).send("李四", "x").stage == "open"
    def _with(cls=FakeBackend):
        b = cls()
        b.sessions = [SessionRow("张三")]
        return b

    # 会话列表里根本没有这个人 → open 失败（真机：同名格一个都对不上）
    assert _sender(FakeBackend()).send("张三", "x").stage == "open"
    # 标题不符（切换没生效）
    class _Wrong(FakeBackend):
        def current_title(self):
            return "王五"
    assert _sender(_with(_Wrong)).send("张三", "x").stage == "title"
    # 输入框回读不符 → 清残留
    class _Garble(FakeBackend):
        def read_composer(self):
            return self.composer + "残留"
    fb4 = _with(_Garble)
    r4 = _sender(fb4).send("张三", "x")
    assert r4.stage == "fill" and r4.reason == "composer_mismatch" and fb4.composer == ""
    # 回车失败
    fb5 = _with(); fb5.fail_send = True
    assert _sender(fb5).send("张三", "x").stage == "send"
    # 无回显
    fb6 = _with(); fb6.echo_on_send = False
    assert _sender(fb6).send("张三", "x").stage == "echo"
    assert _sender(_with()).send("张三", "   ").reason == "empty_text"


# ── 语音五步（2026-09-19）────────────────────────────────────────────────────
def _voice_backend(sec=9) -> FakeBackend:
    fb = FakeBackend()
    fb.sessions = [SessionRow("张三")]
    fb.recorded_seconds = sec
    return fb


def test_voice_helpers_parse_seconds_and_find_echo():
    from src.integrations.wechat_pc.send_guard import find_voice_echo, parse_voice_seconds
    assert parse_voice_seconds("语音11秒") == 11
    assert parse_voice_seconds('语音 7"') == 7 and parse_voice_seconds("Voice 12s") == 12
    assert parse_voice_seconds("[语音] 3 秒") == 3 and parse_voice_seconds("你好") is None
    v = Bubble("语音11秒", is_self=True, kind="voice")
    assert find_voice_echo([], [v], 9) is not None, "录音比音频长 2s 在容忍内"
    assert find_voice_echo([], [v], 30) is None, "秒数差太多＝别人的语音"
    assert find_voice_echo([v], [v], 11) is None, "数量没增加不算新增"
    assert find_voice_echo([], [Bubble("语音11秒", is_self=False, kind="voice")], 11) is None, "对方语音不算"
    assert find_voice_echo([], [Bubble("语音", is_self=True, kind="voice")], 11) is not None, "没秒数只按数量"
    assert find_voice_echo([], [Bubble("语音5秒", is_self=True, kind="unknown")], 5) is not None, "类名认不出按占位文案"


def test_find_voice_echo_tail_run_survives_top_scroll_out():
    """2026-09-19 真机：分条第二条发出后，顶部最老的语音滚出可视区 → 总数不变；尾部连续数 1→2 仍能认出新增。"""
    from src.integrations.wechat_pc.send_guard import find_voice_echo
    v = lambda: Bubble('语音7"秒', is_self=True, kind="voice")  # noqa: E731
    sep = Bubble("17:59", kind="system")
    peer = Bubble("你在哪里")
    before = [v(), peer, Bubble("在", is_self=True), sep, v()]
    after = [peer, Bubble("在", is_self=True), sep, v(), v()]          # 顶部 v 滚出，尾部多一条
    assert find_voice_echo(before, after, 5) is not None
    # 尾部连续数没变、总数也没变 → 仍不算（例如只是列表上下抖动）
    assert find_voice_echo(before, before, 5) is None
    # 尾部连续数增加但秒数离谱 → 不是我们的
    after_bad = [peer, sep, v(), Bubble("语音40秒", is_self=True, kind="voice")]
    assert find_voice_echo(before, after_bad, 5) is None
    # 对方在中间插了一句：总数增加仍能认（尾部连续数被打断也无妨）
    after_peer_mid = before + [Bubble("嗯", is_self=False), v()]
    assert find_voice_echo(before, after_peer_mid, 5) is not None


def test_guarded_sender_voice_echo_window_is_longer_than_text():
    from src.integrations.wechat_pc.send_guard import GuardedSender
    g = GuardedSender(FakeBackend(), sleep=lambda s: None)
    assert g.voice_echo_retries >= 8 and g.voice_echo_retries >= g.echo_retries
    g2 = GuardedSender(FakeBackend(), sleep=lambda s: None, echo_retries=10, voice_echo_retries=2)
    assert g2.voice_echo_retries == 10, "语音窗口不短于文字窗口"


def test_guarded_send_voice_happy_path_and_failures_always_cancel():
    played = []
    fb = _voice_backend(9)

    def _play():
        played.append(1)
        return 8.6

    out = _sender(fb).send_voice("张三", _play, expected_sec=9)
    assert out.ok and out.stage == "echo" and "语音9秒" in out.echo_text and played
    assert not fb.recording and ("start_voice_record", "张三") in fb.actions and ("finish_voice_record", "张三") in fb.actions
    # 每步带 @累计ms 时间轴（真机排「一条 5s 语音为什么要 17s」用）
    assert any(t.startswith("record ok @") and t.endswith("ms") for t in out.trace), out.trace
    assert any(t.startswith("play ok") for t in out.trace)
    # 老版本没有「发语音」→ record 步失败，没碰任何按钮
    fb2 = _voice_backend(); fb2.voice_supported = False
    r2 = _sender(fb2).send_voice("张三", _play)
    assert r2.stage == "record" and r2.reason == "voice_not_ready" and not any(a[0] == "open_session" for a in fb2.actions)
    # 进不了录音态
    fb3 = _voice_backend(); fb3.fail_start_record = True
    assert _sender(fb3).send_voice("张三", _play).reason == "start_record_failed"
    # 播放炸了 → 必取消，退出录音态
    fb4 = _voice_backend()

    def _boom():
        raise RuntimeError("device gone")

    r4 = _sender(fb4).send_voice("张三", _boom)
    assert r4.stage == "play" and r4.reason.startswith("play_failed") and not fb4.recording
    assert ("cancel_voice_record", "张三") in fb4.actions and "cancel ok" in r4.trace
    # 「发送语音」点不到 → 取消
    fb5 = _voice_backend(); fb5.fail_finish_record = True
    r5 = _sender(fb5).send_voice("张三", _play)
    assert r5.stage == "send" and r5.reason == "finish_record_failed" and not fb5.recording
    # 取消也失败＝录音态卡住 → stage=cancel（service 据此冻结）
    fb6 = _voice_backend(); fb6.fail_finish_record = True; fb6.fail_cancel_record = True
    r6 = _sender(fb6).send_voice("张三", _play)
    assert r6.stage == "cancel" and r6.reason == "finish_record_failed" and fb6.recording
    # 录音期间标题被顶走 → 取消，不发到别人会话
    class _Drift(FakeBackend):
        def current_title(self):
            return "王五" if self.recording else self.current
    fb7 = _Drift(); fb7.sessions = [SessionRow("张三")]
    r7 = _sender(fb7).send_voice("张三", _play)
    assert r7.stage == "send" and r7.reason == "title_changed_before_send" and not fb7.recording
    assert ("finish_voice_record", "张三") not in fb7.actions
    # 无回显
    fb8 = _voice_backend(); fb8.voice_echo_on_finish = False
    assert _sender(fb8).send_voice("张三", _play).stage == "echo"


class _Playback:
    """audio_cable.Playback 同形：warmup / __call__(during) / close，记调用顺序。"""

    def __init__(self, dur=8.6, fail=False):
        self.dur = dur
        self.fail = fail
        self.events: List[str] = []

    @property
    def duration(self):
        return self.dur

    def warmup(self):
        self.events.append("warmup")
        return True

    def __call__(self, during=None):
        self.events.append("play")
        if during is not None:
            during()
        if self.fail:
            raise RuntimeError("device gone")
        return self.dur

    def close(self):
        self.events.append("close")


def test_send_voice_warms_up_before_record_primes_send_button_during_play_and_always_closes():
    """P2 静音收窄：warmup 在进录音态之前；prime 在播放期间（录音态里）；close 成败都要跑。"""
    fb = _voice_backend(9)
    pb = _Playback()
    out = _sender(fb).send_voice("张三", pb, expected_sec=9)
    assert out.ok and pb.events == ["warmup", "play", "close"]
    names = [a[0] for a in fb.actions]
    i_start, i_prime, i_finish = names.index("start_voice_record"), names.index("prime_voice_send"), names.index("finish_voice_record")
    assert i_start < i_prime < i_finish, "提前定位必须发生在录音态里、点发送之前"
    assert any(t.startswith("send ok tail=") and t.endswith("ms") for t in out.trace), out.trace
    # 播放炸了：仍然取消录音、仍然 close
    fb2 = _voice_backend()
    pb2 = _Playback(fail=True)
    r2 = _sender(fb2).send_voice("张三", pb2)
    assert r2.stage == "play" and not fb2.recording and pb2.events[-1] == "close"
    # 进不了录音态：warmup 已做、也要 close，但不会 prime
    fb3 = _voice_backend(); fb3.fail_start_record = True
    pb3 = _Playback()
    assert _sender(fb3).send_voice("张三", pb3).reason == "start_record_failed"
    assert pb3.events == ["warmup", "close"] and "prime_voice_send" not in [a[0] for a in fb3.actions]
    # 老式无参闭包照旧可用（不传 during）
    fb4 = _voice_backend()
    assert _sender(fb4).send_voice("张三", lambda: 3.0).ok


def test_send_voice_prime_retries_within_play_window_only(monkeypatch):
    """「发送语音」按钮晚挂名字：定位失败隔 0.25s 再试，但预算卡在音频结束前 0.4s（短音频只试一次）。"""
    import src.integrations.wechat_pc.send_guard as sg

    class _LatePrime(FakeBackend):
        def __init__(self, ok_at):
            super().__init__()
            self.ok_at, self.prime_calls = ok_at, 0

        def prime_voice_send(self):
            self.prime_calls += 1
            return self.prime_calls >= self.ok_at

    now = [1000.0]
    monkeypatch.setattr(sg.time, "monotonic", lambda: now[0])
    slept: List[float] = []

    def _sleep(s):
        slept.append(s)
        now[0] += s

    # 8.6s 音频：预算 3s → 第 3 次才定位到也来得及
    fb = _LatePrime(ok_at=3); fb.sessions = [SessionRow("张三")]; fb.recorded_seconds = 9
    g = GuardedSender(fb, sleep=_sleep, read_pause_sec=0, echo_wait_sec=0, echo_retries=2)
    assert g.send_voice("张三", _Playback(dur=8.6), expected_sec=9).ok
    assert fb.prime_calls == 3 and slept.count(0.25) == 2
    # 0.5s 音频：预算 0.1s → 只试一次，不睡；发送仍成功（finish 自己重新定位）
    fb2 = _LatePrime(ok_at=99); fb2.sessions = [SessionRow("张三")]; fb2.recorded_seconds = 1
    slept.clear()
    g2 = GuardedSender(fb2, sleep=_sleep, read_pause_sec=0, echo_wait_sec=0, echo_retries=2)
    assert g2.send_voice("张三", _Playback(dur=0.5), expected_sec=1).ok
    assert fb2.prime_calls == 1 and 0.25 not in slept


def test_audio_trim_and_loudness_normalisation():
    np = pytest.importorskip("numpy")
    from src.integrations.wechat_pc import audio_cable as ac
    sr = 16000
    t = np.arange(int(sr * 1.0)) / sr
    tone = (0.05 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)   # 峭峰 -26 dBFS、RMS ≈ -29 dBFS 的小声稿子
    sig = np.concatenate([np.zeros(int(sr * 0.4)), tone, np.zeros(int(sr * 0.3))]).astype(np.float32)
    x = np.stack([sig, sig], axis=1)
    y = ac.trim_silence(x, sr)
    # 两头 0.4s/0.3s 静音只剩各 60ms 呼吸感
    assert abs(len(y) / sr - (1.0 + 0.12)) < 0.01
    # 全静音不裁（上层判 too_short）
    z = np.zeros((sr, 2), dtype=np.float32)
    assert len(ac.trim_silence(z, sr)) == sr
    # 响度归一：有声 RMS → -18 dBFS（正弦 RMS = 峰值/√2），峰值不越 -3 dBFS
    n, gain_db = ac.normalize_loudness(y, sr)
    rms_db = 20 * np.log10(ac.active_rms(n, sr))
    peak_db = 20 * np.log10(float(np.max(np.abs(n))))
    assert abs(rms_db - (-18.0)) < 0.6, rms_db
    assert peak_db <= -3.0 + 1e-3 and gain_db > 0
    # 峭峰稿子：响度目标够不到就以峰值上限为准（不削波）
    spiky = np.zeros((sr, 2), dtype=np.float32)
    spiky[::400] = 0.9          # 稀疏尖峰：峰值高、RMS 极低
    spiky[:, :] += 0.001 * np.sin(2 * np.pi * 220 * np.arange(sr) / sr)[:, None]
    m, g2 = ac.normalize_loudness(spiky, sr)
    assert float(np.max(np.abs(m))) <= 10 ** (-3.0 / 20) + 1e-4 and g2 < 0
    # 增益上限：几乎听不见的稿子最多放大 MAX_GAIN
    faint = (x * 0.0001).astype(np.float32)
    f, g3 = ac.normalize_loudness(faint, sr)
    assert abs(g3 - 20 * np.log10(ac.MAX_GAIN)) < 1e-3


def test_playback_runs_during_hook_and_swallows_its_errors(monkeypatch):
    np = pytest.importorskip("numpy")
    from src.integrations.wechat_pc import audio_cable as ac
    calls: List[str] = []

    class _SD:
        @staticmethod
        def play(data, samplerate, device, blocking):
            calls.append(f"play blocking={blocking}")

        @staticmethod
        def wait():
            calls.append("wait")

    monkeypatch.setattr(ac, "_sd", _SD)
    pb = ac.Playback(np.zeros((1600, 2), dtype=np.float32), 16000, 3)
    assert abs(pb.duration - 0.1) < 1e-9

    def _boom():
        calls.append("during")
        raise RuntimeError("uia hiccup")

    assert abs(pb(during=_boom) - 0.1) < 1e-9
    assert calls == ["play blocking=False", "during", "wait"], "during 在播放窗口内跑、异常不外抛"
    calls.clear()
    pb()
    assert calls == ["play blocking=True"]
    pb.close()   # 没 warmup 过：no-op


# ── service ─────────────────────────────────────────────────────────────────

class FakeBridge(BridgeClient):
    def __init__(self):
        super().__init__("http://x", "t", http=self._http)
        self.ingested: List[Dict[str, Any]] = []
        self.queue: List[Dict[str, Any]] = []
        self.acks: List[Tuple[int, bool, str]] = []
        self.heartbeats: List[Dict[str, Any]] = []

    def _http(self, method, url, body):
        if url.endswith("/api/desktop/ingest"):
            self.ingested.append(body)
            return 200, {"ok": True, "conversation_id": "c"}
        if url.endswith("/api/desktop/heartbeat"):
            self.heartbeats.append(dict(body))
            return 200, {"ok": True}
        if "/api/desktop/outbound/ack" in url:
            self.acks.append((body["id"], body["ok"], body["error"]))
            self.ack_bodies = getattr(self, "ack_bodies", []) + [dict(body)]
            return 200, {"ok": True, "acked": True}
        if "/api/desktop/outbound" in url:
            items, self.queue = self.queue, []
            return 200, {"ok": True, "items": items}
        if "/api/unified-inbox/thread" in url:
            # 后端线程 = 本假桥已入站的全部消息（按 chat_key 过滤）
            from urllib.parse import parse_qs, urlparse
            ck = parse_qs(urlparse(url).query).get("chat_key", [""])[0]
            msgs = [{"direction": p["direction"], "text": p["text"]} for p in self.ingested if p["chat_key"] == ck]
            return 200, {"ok": True, "messages": msgs}
        return 404, {}


def _svc(tier="copilot", **kw):
    fb = FakeBackend()
    br = FakeBridge()
    notes: List[Tuple[str, str]] = []
    clock = {"t": 1_757_200_000.0}  # 2026-09-07 白天
    policy = P.resolve_policy({"platform_login": {"wechat_pc": {"tier": tier, "risk_ack": True,
                                                                "work_hours": [0, 24]}}})
    svc = WeChatPcService(fb, br, account_id="wx-a", policy=policy,
                          sender=_sender(fb), notify=lambda k, d: notes.append((k, d)),
                          now=lambda: clock["t"], connected_at=clock["t"] - 30 * 86400, **kw)
    return svc, fb, br, notes, clock


def test_service_ingests_new_bubbles_once_and_mirrors_self():
    svc, fb, br, notes, clock = _svc()
    fb.sessions = [SessionRow("张三", unread=2), SessionRow("李四", unread=0)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1"), Bubble("我在", is_self=True, runtime_id="2"),
                          Bubble("报个价", runtime_id="3")]
    fb.wxids["张三"] = "微信号: zs_1"
    s = svc.tick()
    assert s["readable"] and s["inbound"] == 3
    keys = {p["chat_key"] for p in br.ingested}
    assert keys == {"wx:id:zs_1"}, "首见会话点开资料卡拿到微信号级身份"
    dirs = [p["direction"] for p in br.ingested]
    assert dirs == ["in", "out", "in"] and svc.stats.inbound == 2 and svc.stats.self_mirrored == 1
    assert br.ingested[0]["platform"] == "wechat" and br.ingested[0]["account_id"] == "wx-a"
    assert br.ingested[0]["bridge"] == "pcui"
    # 第二轮：同样的气泡不重复；未读为 0 的会话不打开
    fb.sessions[0].unread = 1
    svc.tick()
    assert len(br.ingested) == 3
    assert ("open_session", "李四") not in fb.actions
    assert svc.stats.errors == 0


def test_copilot_never_pulls_outbound():
    svc, fb, br, notes, clock = _svc("copilot")
    br.queue = [{"id": 1, "chat_key": "wx:name:张三", "text": "hi", "kind": "manual"}]
    svc.tick()
    assert br.acks == [] and svc.stats.sent == 0 and br.queue, "副驾档连队列都不认领"


def test_semi_sends_only_approved_and_acks_denials():
    svc, fb, br, notes, clock = _svc("semi")
    fb.sessions = [SessionRow("张三", unread=1)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    svc.tick()   # 建立 张三 → wx:name:张三 与最近入站
    br.queue = [{"id": 7, "chat_key": "wx:name:张三", "text": "在的", "kind": "manual"},
                {"id": 8, "chat_key": "wx:name:张三", "text": "自动稿", "kind": "text"}]
    clock["t"] += 30
    svc.tick()
    assert (7, True, "") in br.acks
    assert any(a[0] == 8 and a[1] is False and a[2] == "policy:tier_forbids_kind" for a in br.acks)
    assert svc.stats.sent == 1 and svc.stats.denied == 1
    assert fb.messages["张三"][-1].text == "在的" and fb.messages["张三"][-1].is_self


def test_tick_sends_pending_outbound_before_opening_unread_sessions():
    """出站优先：上一轮生成的回复不等本轮逐个打开未读会话再发；扫完入站有新消息再补认领一次，无新消息不多拉。"""
    svc, fb, br, notes, clock = _svc("semi")
    fb.sessions = [SessionRow("张三", unread=1), SessionRow("王五", unread=1)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    fb.messages["王五"] = [Bubble("hi", runtime_id="9")]
    svc.tick()
    br.queue = [{"id": 7, "chat_key": "wx:name:张三", "text": "在的", "kind": "manual"}]
    fb.messages["王五"].append(Bubble("又来了", runtime_id="10"))
    fb.sessions[1].unread = 1
    fb.actions.clear()
    clock["t"] += 30
    s = svc.tick()
    assert s["sent"] == 1 and s["inbound"] == 1
    opens = [a[1] for a in fb.actions if a[0] == "open_session"]
    assert opens[0] == "张三" and "王五" in opens, opens
    assert (7, True, "") in br.acks
    # 没新入站的一轮：只认领一次（一次 GET），不重复拉
    calls = {"n": 0}
    orig = br.pull_outbound
    br.pull_outbound = lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), orig(*a, **k))[1]
    fb.sessions = [SessionRow("张三", unread=0), SessionRow("王五", unread=0)]
    clock["t"] += 30
    s = svc.tick()
    assert s["inbound"] == 0 and calls["n"] == 1


def test_desktop_input_lock_is_reentrant_and_exclusive_across_holders():
    """双开＝两个驾驶进程共用一套鼠标键盘：同持有者可重入；另一个持有者拿不到就等，超时放行不死锁。
    （Windows 命名互斥量按线程归属，这里用两个线程各持一个实例模拟两个进程；非 Windows 退化为进程内锁只验重入。）"""
    import os
    import threading
    from src.integrations.wechat_pc import desktop_input as D
    a = D.DesktopInputLock("Local\\chengjie_test_desktop_lock", timeout=0.3)
    with a:
        with a:
            assert a._depth == 2
        assert a._depth == 1
        if os.name == "nt":
            got = {}

            def other():
                b = D.DesktopInputLock("Local\\chengjie_test_desktop_lock", timeout=0.3)
                got["first"] = b.acquire()
                b.release()
            t = threading.Thread(target=other)
            t.start()
            t.join(5)
            assert got["first"] is False, "A 持锁期间 B 只能超时放行"
    if os.name == "nt":
        b = D.DesktopInputLock("Local\\chengjie_test_desktop_lock", timeout=0.3)
        assert b.acquire() is True, "A 放了 B 就能拿到"
        b.release()
    assert a._depth == 0


def test_send_and_inbound_scan_hold_desktop_input_lock_only_for_foreground_steps(monkeypatch):
    """桌面输入锁只包「要动鼠标键盘」的步骤（开会话格 / 粘贴 / 点发送），拟人打字时长、等回显、读气泡这些纯等待/纯读
    不持锁——双开时另一个驾驶不用陪着等 A 号「打字」十几秒。"""
    from src.integrations.wechat_pc import send_guard as SG
    from src.integrations.wechat_pc import service as S

    class Rec:
        def __init__(self):
            self.depth = 0
            self.events: List[Tuple[str, int]] = []

        def __enter__(self):
            self.depth += 1
            self.events.append(("enter", self.depth))
            return self

        def __exit__(self, *exc):
            self.events.append(("exit", self.depth))
            self.depth -= 1
    rec = Rec()
    monkeypatch.setattr(SG, "desktop_input", lambda: rec)
    monkeypatch.setattr(S, "desktop_input", lambda: rec)
    svc, fb, br, notes, clock = _svc("semi")
    orig_open, orig_set, orig_send = fb.open_session, fb.set_composer, fb.press_send
    seen = []
    fb.open_session = lambda *a, **k: (seen.append(("open", rec.depth)), orig_open(*a, **k))[1]
    fb.set_composer = lambda *a, **k: (seen.append(("fill", rec.depth)), orig_set(*a, **k))[1]
    fb.press_send = lambda *a, **k: (seen.append(("send", rec.depth)), orig_send(*a, **k))[1]
    orig_read = fb.read_visible_messages
    fb.read_visible_messages = lambda *a, **k: (seen.append(("read", rec.depth)), orig_read(*a, **k))[1]
    svc.sender._sleep = lambda sec: seen.append(("sleep", rec.depth))
    fb.sessions = [SessionRow("张三", unread=1)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    svc.tick()
    br.queue = [{"id": 7, "chat_key": "wx:name:张三", "text": "在的", "kind": "manual"}]
    clock["t"] += 30
    svc.tick()
    assert rec.depth == 0 and rec.events[-1][0] == "exit"
    assert all(d >= 1 for k, d in seen if k in ("open", "fill", "send")), seen
    assert all(d == 0 for k, d in seen if k in ("read", "sleep")), f"纯读/纯等待不该持锁：{seen}"
    assert ("fill", 1) in seen and ("send", 1) in seen and ("sleep", 0) in seen


def test_auto_reply_requires_recent_inbound_and_freezes_on_guard_failure():
    svc, fb, br, notes, clock = _svc("auto_reply")
    # 无入站的会话 → 拒（仅回复原则）
    br.queue = [{"id": 1, "chat_key": "wx:name:王五", "text": "hello", "kind": "text"}]
    svc.tick()
    assert br.acks[-1] == (1, False, "policy:no_inbound_from_peer")
    # 有入站 → 发
    fb.sessions = [SessionRow("张三", unread=1)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    svc.tick()
    br.queue = [{"id": 2, "chat_key": "wx:name:张三", "text": "在的", "kind": "text"}]
    clock["t"] += 30
    svc.tick()
    assert (2, True, "") in br.acks and svc.stats.sent == 1
    # 回显缺失（发错/没发出）→ 冻结 + 通知，不再继续发队列里下一条
    fb.echo_on_send = False
    br.queue = [{"id": 3, "chat_key": "wx:name:张三", "text": "再说一句", "kind": "text"},
                {"id": 4, "chat_key": "wx:name:张三", "text": "第三句", "kind": "text"}]
    clock["t"] += 30
    svc.tick()
    ack3 = next(a for a in br.acks if a[0] == 3)
    assert ack3[1] is False and ack3[2].startswith("guard:echo:")
    # 单个会话守卫失败 → 只冻这个会话，账号级不冻（别的人还能回）
    assert svc.chat_frozen("wx:name:张三") and not svc.frozen()
    assert svc.stats.guard_freezes == 1 and svc.stats.chat_freezes == 1 and notes[-1][0] == "guard_chat_freeze"
    ack4 = next(a for a in br.acks if a[0] == 4)
    assert ack4 == (4, False, "guard:frozen"), "冻结后这个会话已认领的剩余命令立刻回执失败进人审"
    assert fb.messages["张三"][-1].text == "在的", "冻结期间没有再往微信里发任何字"
    svc.tick()
    assert br.heartbeats[-1]["stats"]["chat_frozen"] == 1 and br.heartbeats[-1]["stats"]["freeze_reason"] == ""
    clock["t"] += 700
    assert not svc.chat_frozen("wx:name:张三") and not svc.frozen()


def test_guard_failures_on_two_chats_escalate_to_account_freeze():
    svc, fb, br, notes, clock = _svc("auto_reply")
    fb.sessions = [SessionRow("张三", unread=1), SessionRow("李四", unread=1)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    fb.messages["李四"] = [Bubble("在吗", runtime_id="2")]
    svc.tick()
    fb.echo_on_send = False
    br.queue = [{"id": 1, "chat_key": "wx:name:张三", "text": "a", "kind": "text"}]
    clock["t"] += 30
    svc.tick()
    assert svc.chat_frozen("wx:name:张三") and not svc.frozen()
    # 另一个会话也发不出去 → 不是某个人的问题，是整个微信的问题 → 升级账号级冻结并通知主人
    br.queue = [{"id": 2, "chat_key": "wx:name:李四", "text": "b", "kind": "text"}]
    clock["t"] += 30
    svc.tick()
    assert svc.frozen() and svc.stats.freeze_reason == "guard_echo" and notes[-1][0] == "guard_freeze"
    assert br.heartbeats[-1]["stats"]["freeze_reason"] == "guard_echo"


class FakeVoice:
    """audio_cable.VoiceCable 同形：ready / mic_switched / prepare。"""

    def __init__(self, ready=True, dur=8.6, fail_prepare=False, fail_switch=False):
        self._ready = ready
        self.dur = dur
        self.fail_prepare = fail_prepare
        self.fail_switch = fail_switch
        self.switch_log: List[str] = []
        self.prepared: List[str] = []
        self.played = 0

    def ready(self):
        return self._ready

    def mic_switched(self):
        outer = self

        class _Ctx:
            def __enter__(self_inner):
                if outer.fail_switch:
                    raise RuntimeError("set_default_mic_failed")
                outer.switch_log.append("on")
                return self_inner

            def __exit__(self_inner, *exc):
                outer.switch_log.append("off")

        return _Ctx()

    def prepare(self, path):
        self.prepared.append(path)
        if self.fail_prepare:
            raise RuntimeError("audio_too_long:70.0s")

        def _play():
            self.played += 1
            return self.dur

        return _play, self.dur


def _voice_item(iid=11, **kw):
    it = {"id": iid, "chat_key": "wx:name:张三", "text": "念稿", "kind": "voice",
          "media_ref": __file__, "media_url": "/static/outbound/wechat/wx-a/v.ogg",
          "inbox_text": "念稿文本", "sender_name": "Claire", "duration_ms": 8600}
    it.update(kw)
    return it


def test_service_sends_voice_item_and_skips_placeholder_echo():
    voice = FakeVoice()
    svc, fb, br, notes, clock = _svc("auto_reply", voice=voice)
    fb.recorded_seconds = 9
    fb.sessions = [SessionRow("张三", unread=1)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    svc.tick()
    assert svc.stats.voice_ready is True
    br.queue = [_voice_item()]
    clock["t"] += 30
    svc.tick()
    assert any(a[0] == 11 and a[1] is True for a in br.acks)
    # ack 带 delivered_as/echo（后端据此镜像出站行 + 打 record_voice_sent，P0-6）
    body = next(b for b in br.ack_bodies if b["id"] == 11)
    assert body["delivered_as"] == "voice" and "echo" in body
    assert voice.switch_log == ["on", "off"] and voice.played == 1 and voice.prepared == [__file__]
    assert svc.stats.voice_sent == 1 and svc.stats.sent == 1 and not fb.recording
    # 下一轮扫到回显「语音9秒」占位气泡：后端已在 ack 时镜像了带念稿/音频的行 → 驱动**不落第二行**
    fb.sessions[0].unread = 1
    clock["t"] += 5
    before = len(br.ingested)
    svc.tick()
    assert not [p for p in br.ingested[before:] if p["direction"] == "out" and p.get("media_type") == "voice"]
    assert svc.stats.self_mirrored >= 1
    # 同一颗占位气泡再扫不会重复计（已标已见）
    fb.sessions[0].unread = 1
    clock["t"] += 5
    n = svc.stats.self_mirrored
    svc.tick()
    assert svc.stats.self_mirrored == n
    assert svc.stats.voice_failed == 0


def test_service_mic_busy_fails_voice_before_switching_mic_and_notifies_once():
    """P2-3：坐席正在用麦（开会/通话）→ 不切默认麦克风、不进录音态，ack ``guard:record:mic_busy:<进程>``
    （server 端同稿改发文字）；心跳带 voice_mic_busy/by；空闲→占用只通知一次；空闲后恢复发语音。"""
    voice = FakeVoice()
    busy = {"v": {"busy": True, "sessions": [{"pid": 4242, "process": "Zoom.exe"}]}}
    voice.mic_busy = lambda: busy["v"]
    svc, fb, br, notes, clock = _svc("auto_reply", voice=voice)
    fb.recorded_seconds = 9
    fb.sessions = [SessionRow("张三", unread=1)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    svc.tick()
    assert svc.stats.voice_ready is True
    br.queue = [_voice_item()]
    clock["t"] += 30
    svc.tick()
    a = next(x for x in br.acks if x[0] == 11)
    assert a[1] is False and a[2].startswith("guard:record:mic_busy:Zoom.exe")
    assert voice.switch_log == [] and voice.played == 0 and not fb.recording
    assert svc.stats.voice_mic_busy is True and svc.stats.voice_mic_busy_by == "Zoom.exe"
    assert [n for n in notes if n[0] == "voice_mic_busy"] == [("voice_mic_busy", "Zoom.exe")]
    hb = [b for b in br.heartbeats if b.get("stats", {}).get("voice_mic_busy") is True]
    assert hb and hb[-1]["stats"]["voice_mic_busy_by"] == "Zoom.exe"
    # 仍占用：不重复打扰
    br.queue = [_voice_item(iid=12)]
    clock["t"] += 30
    svc.tick()
    assert len([n for n in notes if n[0] == "voice_mic_busy"]) == 1
    # 麦克风空闲 → 恢复语音
    busy["v"] = {"busy": False, "sessions": []}
    br.queue = [_voice_item(iid=13)]
    clock["t"] += 30
    svc.tick()
    assert any(x[0] == 13 and x[1] is True for x in br.acks)
    assert voice.switch_log == ["on", "off"] and svc.stats.voice_mic_busy is False
    # 文字命令不受麦克风占用影响
    busy["v"] = {"busy": True, "sessions": [{"pid": 1, "process": "Teams.exe"}]}
    br.queue = [{"id": 14, "chat_key": "wx:name:张三", "text": "文字照发", "kind": "text"}]
    clock["t"] += 30
    svc.tick()
    assert any(x[0] == 14 and x[1] is True for x in br.acks)


def test_service_recovers_last_inbound_from_backend_thread_after_restart():
    """驱动重启后 _last_inbound 为空：仅回复档先问后端线程补回「对方最近来信」，不再把回复一律拒成
    no_inbound_from_peer；线程里没有来信照旧拒；同一联系人 120s 内不重复问。"""
    svc, fb, br, notes, clock = _svc("auto_reply")
    fb.sessions = [SessionRow("张三", unread=0)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    calls: List[str] = []
    br.thread_last_inbound_ts = lambda acc, ck: (calls.append(ck), clock["t"] - 300)[1]
    br.queue = [{"id": 61, "chat_key": "wx:name:张三", "text": "回你", "kind": "text"}]
    svc.tick()
    assert (61, True, "") in br.acks and calls == ["wx:name:张三"]
    assert svc._last_inbound["wx:name:张三"] == clock["t"] - 300
    # 线程里没有来信（0.0）→ 仍拒；同一联系人 120s 内只问一次
    svc2, fb2, br2, _, clock2 = _svc("auto_reply")
    fb2.sessions = [SessionRow("李四", unread=0)]
    n = {"c": 0}

    def _none(acc, ck):
        n["c"] += 1
        return 0.0

    br2.thread_last_inbound_ts = _none
    br2.queue = [{"id": 62, "chat_key": "wx:name:李四", "text": "x", "kind": "text"}]
    svc2.tick()
    assert (62, False, "policy:no_inbound_from_peer") in br2.acks and n["c"] == 1
    br2.queue = [{"id": 63, "chat_key": "wx:name:李四", "text": "y", "kind": "text"}]
    clock2["t"] += 30
    svc2.tick()
    assert (63, False, "policy:no_inbound_from_peer") in br2.acks and n["c"] == 1
    clock2["t"] += 200
    br2.queue = [{"id": 64, "chat_key": "wx:name:李四", "text": "z", "kind": "text"}]
    svc2.tick()
    assert n["c"] == 2


def test_service_mic_busy_probe_errors_do_not_block_voice():
    """检测本身异常/COM 不可用 → 按不占用（看不见不能当成一直被占用）。"""
    voice = FakeVoice()

    def _boom():
        raise RuntimeError("com")

    voice.mic_busy = _boom
    svc, fb, br, notes, clock = _svc("auto_reply", voice=voice)
    fb.recorded_seconds = 9
    fb.sessions = [SessionRow("张三", unread=1)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    svc.tick()
    br.queue = [_voice_item()]
    clock["t"] += 30
    svc.tick()
    assert any(x[0] == 11 and x[1] is True for x in br.acks)
    assert svc.stats.voice_mic_busy is False and voice.played == 1


def test_voice_send_is_atomic_within_tick_no_scan_between_record_and_finish():
    """D7：录音态期间绝不能 list_sessions/open_session 别的会话（语音会发进错的聊天）。
    tick 单线程：_drain_outbound 与 _scan_inbound 顺序执行不交错；录音 → 灌音 → 发送在一次 send_voice 里原子完成。"""
    svc, fb, br, notes, clock = _svc("auto_reply", voice=FakeVoice())
    fb.recorded_seconds = 9
    fb.sessions = [SessionRow("张三", unread=1), SessionRow("李四", unread=3)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    fb.messages["李四"] = [Bubble("你好", runtime_id="9")]
    svc.tick()
    br.queue = [_voice_item()]
    clock["t"] += 30
    fb.actions.clear()
    fb.sessions[1].unread = 2          # 本轮仍有别的会话待扫
    svc.tick()
    names = [a[0] for a in fb.actions]
    i0, i1 = names.index("start_voice_record"), names.index("finish_voice_record")
    between = fb.actions[i0 + 1:i1]
    assert not [a for a in between if a[0] in ("list_sessions", "open_session", "read_visible_messages")], between
    # 会话列表在录音前读过（同名计数新鲜）；别的会话（李四）的扫描整体在语音发完之后，绝不夹在中间
    assert names.index("list_sessions") < i0
    assert any(a[0] == "open_session" and a[1] == "李四" for a in fb.actions[i1 + 1:])


def test_service_manual_voice_bubble_still_mirrored_as_placeholder():
    # 坐席亲手在微信里发的语音（没有待回显记录）→ 照常按「语音N秒」占位镜像为 out 行
    svc, fb, br, notes, clock = _svc("auto_reply", voice=FakeVoice())
    fb.recorded_seconds = 6
    fb.sessions = [SessionRow("张三", unread=1)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1"), Bubble("语音6秒", is_self=True, kind="voice", runtime_id="2")]
    svc.tick()
    outs = [p for p in br.ingested if p["direction"] == "out"]
    assert len(outs) == 1 and outs[0].get("media_type") == "voice" and outs[0]["media_ref"] == ""


def test_service_voice_failures_do_not_freeze_unless_recording_stuck():
    # 1) 本机没声卡 → 拒发不冻结，ack 带原因
    svc, fb, br, notes, clock = _svc("auto_reply", voice=FakeVoice(ready=False))
    fb.sessions = [SessionRow("张三", unread=1)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    svc.tick()
    br.queue = [_voice_item(12)]
    clock["t"] += 30
    svc.tick()
    a = next(x for x in br.acks if x[0] == 12)
    assert a[1] is False and a[2] == "guard:record:voice_not_ready" and not svc.frozen()
    assert svc.stats.voice_failed == 1 and svc.stats.voice_ready is False
    # 2) 音频超长（prepare 拒）→ 同样不冻结，没进录音态
    svc2, fb2, br2, _, clock2 = _svc("auto_reply", voice=FakeVoice(fail_prepare=True))
    fb2.sessions = [SessionRow("张三", unread=1)]
    fb2.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    svc2.tick()
    br2.queue = [_voice_item(13)]
    clock2["t"] += 30
    svc2.tick()
    a2 = next(x for x in br2.acks if x[0] == 13)
    assert a2[1] is False and a2[2].startswith("guard:record:prepare_failed") and not svc2.frozen()
    assert not any(x[0] == "start_voice_record" for x in fb2.actions)
    # 3) 录音态卡住（取消失败）→ 冻结 + 专门通知
    svc3, fb3, br3, notes3, clock3 = _svc("auto_reply", voice=FakeVoice())
    fb3.sessions = [SessionRow("张三", unread=1)]
    fb3.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    fb3.fail_finish_record = True
    fb3.fail_cancel_record = True
    svc3.tick()
    br3.queue = [_voice_item(14)]
    clock3["t"] += 30
    svc3.tick()
    a3 = next(x for x in br3.acks if x[0] == 14)
    assert a3[2] == "guard:cancel:finish_record_failed" and svc3.frozen()
    assert notes3[-1][0] == "voice_recording_stuck"
    # 4) 半自动档不认 voice（自动链语音仅全自动）
    svc4, fb4, br4, _, clock4 = _svc("semi", voice=FakeVoice())
    fb4.sessions = [SessionRow("张三", unread=1)]
    fb4.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    svc4.tick()
    br4.queue = [_voice_item(15)]
    clock4["t"] += 30
    svc4.tick()
    assert next(x for x in br4.acks if x[0] == 15)[2] == "policy:tier_forbids_kind"


def test_bridge_presence_carries_voice_ready():
    from src.web.desktop_bridge_presence import bridge_presence, bridge_voice_ready, heartbeat_meta
    meta = {"bridge_heartbeat": heartbeat_meta(
        {"bridge": "wechat_pc", "tier": "auto_reply", "stats": {"voice_ready": True, "voice_sent": 2}}, now=1000.0)}
    pres = bridge_presence(meta, now=1010.0)
    assert pres["voice_ready"] is True and pres["stats"]["voice_sent"] == 2
    assert bridge_voice_ready(meta, now=1010.0) is True
    assert bridge_voice_ready(meta, now=1000.0 + 500) is False, "心跳过期＝不排语音"
    old = {"bridge_heartbeat": heartbeat_meta({"bridge": "wechat_pc", "tier": "auto_reply", "stats": {}}, now=1000.0)}
    assert bridge_presence(old, now=1001.0)["voice_ready"] is None and bridge_voice_ready(old, now=1001.0) is False
    # P2-3：坐席正用麦 → 能力在但此刻不排语音（省一次白合成）；占用进程名 + 配额用量随 presence 透出
    busy = {"bridge_heartbeat": heartbeat_meta(
        {"bridge": "wechat_pc", "tier": "auto_reply",
         "stats": {"voice_ready": True, "voice_mic_busy": True, "voice_mic_busy_by": "Zoom.exe",
                   "voice_today": 3, "voice_daily_cap": 30, "caps_relaxed": ""}}, now=1000.0)}
    pres = bridge_presence(busy, now=1005.0)
    assert pres["voice_ready"] is True and pres["voice_mic_busy"] is True and pres["voice_mic_busy_by"] == "Zoom.exe"
    assert pres["stats"]["voice_today"] == 3 and pres["stats"]["voice_daily_cap"] == 30
    assert bridge_voice_ready(busy, now=1005.0) is False


def test_screen_disposition_freezes_and_notifies():
    svc, fb, br, notes, clock = _svc("auto_reply")
    fb.logged_in = False
    fb.window_class = "mmui::LoginWindow"
    fb.dialogs = ["进入WeChat", "切换账号"]
    s = svc.tick()
    assert s["readable"] is False and svc.stats.offline and svc.frozen()
    assert notes and notes[-1][0] == R.LOGGED_OUT
    # 登录恢复 → 解除 offline，通知恢复
    assert svc.stats.freeze_reason == R.LOGGED_OUT and br.heartbeats[-1]["stats"]["freeze_reason"] == R.LOGGED_OUT
    fb.logged_in = True
    fb.window_class = "mmui::MainWindow"
    fb.dialogs = []
    clock["t"] += 20
    svc.tick()
    assert not svc.stats.offline and notes[-1][0] == "recovered"
    # 登录窗关了 → 那 1h 冻结随之解除（不再静默停发一小时）
    assert not svc.frozen() and svc.stats.freeze_reason == ""
    # 限频弹窗：仍可读屏，但冻结发送 2h
    fb.dialogs = ["操作过于频繁，请稍后再试"]
    clock["t"] += 5000
    s2 = svc.tick()
    assert s2["readable"] is True and svc.stats.frozen_until >= clock["t"] + 7000
    # 限频带 TTL（真风控信号）：弹窗消失也不提前解冻；中途再弹登录窗并恢复，只解登录窗那一份
    fb.dialogs = []
    svc.tick()
    assert svc.frozen() and svc.stats.freeze_reason == R.LIMIT
    fb.logged_in = False
    fb.window_class = "mmui::LoginWindow"
    fb.dialogs = ["进入WeChat"]
    svc.tick()
    fb.logged_in = True
    fb.window_class = "mmui::MainWindow"
    fb.dialogs = []
    svc.tick()
    assert svc.frozen() and svc.stats.freeze_reason == R.LIMIT


def test_bubble_fingerprint_is_content_based_not_runtime_id():
    """RecyclerListView 会复用 RuntimeId（真机实锤）→ 指纹只看内容：同文同向同分钟＝同一条，RuntimeId 不参与。"""
    assert bubble_fingerprint(Bubble("a", runtime_id="x")) == bubble_fingerprint(Bubble("a", runtime_id="y"))
    f1 = bubble_fingerprint(Bubble("a"))
    assert f1.startswith("fp:") and f1 == bubble_fingerprint(Bubble("a"))
    assert f1 != bubble_fingerprint(Bubble("a", is_self=True))
    assert bubble_fingerprint(Bubble("a", ts_hint=60.0)) != bubble_fingerprint(Bubble("a", ts_hint=120.0))
    assert bubble_fingerprint(Bubble("a", ts_hint=60.0)) == bubble_fingerprint(Bubble("a", ts_hint=90.0))


def test_fake_backend_satisfies_protocol():
    assert isinstance(FakeBackend(), WeChatPcBackend)


def test_uia_backend_importable_and_readonly_without_window():
    from src.integrations.wechat_pc import uia_backend as U
    assert set(U.REQUIRED_FOR_READ) <= set(U.ANCHORS) and set(U.REQUIRED_FOR_SEND) <= set(U.ANCHORS)
    b = U.UiaBackend(anchors={"composer": {"aid": ["custom_input"]}})
    assert "custom_input" in b.anchors["composer"]["aid"] and b.anchors["composer"]["type"] == "EditControl"
    assert b.readonly is True, "未自检通过前恒只读"
    assert b.set_composer("x") is False and b.press_send() is False


# ── 窗口绑定（2026-09-21 P0-2：双开微信，驱动不能再「取第一个 MainWindow」）──────────

def _two_wechats():
    from src.integrations.wechat_pc.win32_windows import TopWindow
    a_main = TopWindow(1001, 501, "mmui::MainWindow", "微信", True, "weixin.exe", 900, 700)
    a_dlg = TopWindow(1002, 501, "Qt51514QWindowToolSaveBits", "", True, "weixin.exe", 300, 200)
    b_main = TopWindow(2001, 502, "mmui::MainWindow", "微信", True, "weixin.exe", 900, 700)
    b_login = TopWindow(2002, 502, "mmui::LoginWindow", "", True, "weixin.exe", 300, 400)
    return a_main, a_dlg, b_main, b_login


def test_select_main_window_binds_by_hwnd_then_pid_and_never_switches_accounts():
    from src.integrations.wechat_pc.win32_windows import select_main_window
    a_main, a_dlg, b_main, b_login = _two_wechats()
    wins = [b_login, a_dlg, b_main, a_main]
    assert select_main_window(wins) is b_main, "没绑：单开兼容，取第一个主窗"
    assert select_main_window(wins, hwnd=1001) is a_main
    assert select_main_window(wins, pid=501) is a_main
    assert select_main_window(wins, hwnd=1002) is None, "句柄命中的不是主窗 → 不算"
    assert select_main_window(wins, hwnd=9999) is None and select_main_window(wins, pid=999) is None, "绑了没命中 → 绝不换号"
    assert select_main_window([b_login]) is None


def test_main_windows_recognises_wechat_4x_qt_top_level():
    """4.1.13 真机（2026-09-21 实锤）：顶层类名是 Qt51514QWindowIcon、标题「微信」，mmui::MainWindow 只在 UIA 树里；
    只按类名筛 → 0 个主窗 → 未绑定锚定失效、设置页选不到窗口。"""
    from src.integrations.wechat_pc.win32_windows import TopWindow, main_windows, select_main_window
    qt_main = TopWindow(14880096, 17468, "Qt51514QWindowIcon", "微信", True, "weixin.exe", 916, 668)
    qt_main_min = TopWindow(14880097, 17469, "Qt51514QWindowIcon", "WeChat", True, "weixin.exe", 0, 0)
    qt_login = TopWindow(3001, 17470, "Qt51514QWindowIcon", "WeChat", True, "weixin.exe", 280, 380)
    qt_chat = TopWindow(3002, 17468, "Qt51514QWindowIcon", "张三", True, "weixin.exe", 700, 600)
    old_main = TopWindow(1001, 501, "mmui::MainWindow", "微信", True, "weixin.exe", 900, 700)
    mains = main_windows([qt_login, qt_chat, qt_main, qt_main_min, old_main])
    assert [w.hwnd for w in mains] == [14880096, 14880097, 1001]
    assert select_main_window([qt_login, qt_main], pid=17468) is qt_main
    assert select_main_window([qt_login], pid=17470) is None


def test_uia_backend_unbound_anchors_to_first_process_and_sticks(monkeypatch):
    """没绑时首见主窗锤定其 pid：之后枚举次序变化/另一个微信启动都不换窗；那个进程没了才重新锚定。"""
    from src.integrations.wechat_pc import uia_backend as U
    from src.integrations.wechat_pc import win32_windows as W
    a_main, a_dlg, b_main, b_login = _two_wechats()
    state = {"wins": [a_main, a_dlg]}
    monkeypatch.setattr(W, "find_wechat_windows", lambda **kw: list(state["wins"]))
    b = U.UiaBackend()
    assert not b.bound
    assert [w.hwnd for w in b._bound_windows()] == [1001, 1002] and b.main_pid() == 501
    # 第二个微信登上来并排在前面：仍只看 501 的窗（B 的登录窗/主窗文本不会喂进风控分类）
    state["wins"] = [b_login, b_main, a_dlg, a_main]
    assert [w.hwnd for w in b._bound_windows()] == [1002, 1001]
    assert b.main_pid() == 501 and b.main_window_count == 2
    assert b.window_binding() == {"window_hwnd": 0, "window_pid": 501, "window_bound": False, "main_windows": 2}
    # A 退登只剩登录窗：还是盯 501（不跳去 B）
    a_login = W.TopWindow(1003, 501, "mmui::LoginWindow", "", True, "weixin.exe", 300, 400)
    state["wins"] = [b_main, a_login]
    assert [w.hwnd for w in b._bound_windows()] == [1003]
    # A 进程整个没了（微信重启）→ 重新锚定到现存主窗
    state["wins"] = [b_main]
    assert [w.hwnd for w in b._bound_windows()] == [2001] and b.main_pid() == 502


def test_uia_backend_explicit_binding_follows_process_and_goes_blind_if_target_gone(monkeypatch):
    from src.integrations.wechat_pc import uia_backend as U
    from src.integrations.wechat_pc import win32_windows as W
    a_main, a_dlg, b_main, b_login = _two_wechats()
    state = {"wins": [b_login, b_main, a_dlg, a_main]}
    monkeypatch.setattr(W, "find_wechat_windows", lambda **kw: list(state["wins"]))
    # --pid
    bp = U.UiaBackend(pid=502)
    assert bp.bound and [w.hwnd for w in bp._bound_windows()] == [2002, 2001] and bp.main_pid() == 502
    state["wins"] = [a_main, a_dlg]
    assert bp._bound_windows() == [] and bp.screen_state().window_present is False, "目标进程不在 → 当没窗，绝不去看 A"
    # --hwnd：首次命中记下进程；退登重登句柄换了仍跟着进程走
    state["wins"] = [b_login, b_main, a_dlg, a_main]
    bh = U.UiaBackend(hwnd=1001)
    assert [w.hwnd for w in bh._bound_windows()] == [1002, 1001] and bh.main_pid() == 501
    a_main2 = W.TopWindow(1777, 501, "mmui::MainWindow", "微信", True, "weixin.exe", 900, 700)
    state["wins"] = [b_main, a_main2]
    assert [w.hwnd for w in bh._bound_windows()] == [1777]
    # --hwnd 从没命中过 → 什么都不看（不回落到 B）
    bh2 = U.UiaBackend(hwnd=4242)
    assert bh2._bound_windows() == [] and bh2.main_pid() == 0


def test_uia_backend_retarget_switches_process_and_keeps_binding_explicit(monkeypatch):
    from src.integrations.wechat_pc import uia_backend as U
    from src.integrations.wechat_pc import win32_windows as W
    a_main, a_dlg, b_main, b_login = _two_wechats()
    state = {"wins": [b_login, b_main, a_dlg, a_main]}
    monkeypatch.setattr(W, "find_wechat_windows", lambda **kw: list(state["wins"]))
    # 首见锚定到 A（枚举里 b_main 在前但 mains[0] 是谁不重要，只看切换语义）
    b = U.UiaBackend()
    b._bound_windows()
    cur = b.main_pid()
    other = 502 if cur == 501 else 501
    assert b.other_main_pids() == [other]
    b._last_raw_names = {"旧窗基线"}
    assert b.retarget(other) and b.main_pid() == other and b._last_raw_names == set() and not b.bound
    assert [w.pid for w in b._bound_windows()] == [other, other]
    assert b.retarget(other) is False and b.retarget(0) is False
    # 显式 --hwnd 绑到 A，但真实身份说该盯 B → 改成按 B 的进程绑，仍算显式绑定（不回落首见锚定）
    bh = U.UiaBackend(hwnd=1001)
    bh._bound_windows()
    assert bh.retarget(502) and bh.bound and bh.bind_hwnd == 0 and bh.bind_pid == 502
    assert [w.hwnd for w in bh._bound_windows()] == [2002, 2001]


def test_heartbeat_carries_window_binding_when_backend_supports_it():
    svc, fb, br, notes, clock = _svc()
    svc.tick()
    assert "window_pid" not in br.heartbeats[-1]["stats"], "FakeBackend 没这能力 → 不带"
    fb.window_binding = lambda: {"window_hwnd": 1001, "window_pid": 501, "window_bound": False, "main_windows": 2}
    svc.tick()
    st = br.heartbeats[-1]["stats"]
    assert (st["window_hwnd"], st["window_pid"], st["window_bound"], st["main_windows"]) == (1001, 501, False, 2)


# ── P0-3 account_id ↔ 登录微信真实身份 ─────────────────────────────────────

def test_account_identity_binds_on_first_read_and_persists(tmp_path):
    from src.integrations.wechat_pc.service import AccountBinding
    path = str(tmp_path / "account_wx-a.json")
    svc, fb, br, notes, clock = _svc(account_binding=AccountBinding(path))
    fb.self_nick, fb.self_wxid = "小北", "xb_2020"
    svc.tick()
    st = br.heartbeats[-1]["stats"]
    assert (st["account_nick"], st["account_wxid"], st["account_bound_wxid"], st["account_mismatch"]) == (
        "小北", "xb_2020", "xb_2020", False)
    assert not svc.frozen()
    # 落盘：重启后的驱动直接带着绑定起来
    again = AccountBinding(path)
    assert again.wxid == "xb_2020" and again.nick == "小北"
    # 显式 --expect-wxid 优先于落盘值
    assert AccountBinding(path, expected_wxid="other_9").wxid == "other_9"


def test_account_mismatch_freezes_sends_and_recovers_when_identity_matches_again():
    from src.integrations.wechat_pc.service import AccountBinding
    svc, fb, br, notes, clock = _svc("auto_reply", account_binding=AccountBinding(expected_wxid="xb_2020"))
    fb.self_nick, fb.self_wxid = "小南", "xn_1999"
    fb.sessions = [SessionRow("张三", unread=1)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    br.queue = [{"id": 1, "chat_key": "wx:name:张三", "text": "在的", "kind": "manual"}]
    svc.tick()
    assert svc.frozen() and svc.stats.freeze_reason == svc.ACCOUNT_MISMATCH
    assert ("account_mismatch", "xb_2020->xn_1999:小南") in notes
    st = br.heartbeats[-1]["stats"]
    assert st["account_mismatch"] is True and st["account_wxid"] == "xn_1999" and st["account_bound_wxid"] == "xb_2020"
    assert not any(a[0] == "press_send" for a in fb.actions), "窗里登的是别的号：一条都不能发"
    # 冻结到期但身份仍不对 → 续冻（不会静默恢复发送）
    clock["t"] += 3601
    svc.tick()
    assert svc.frozen() and svc.stats.freeze_reason == svc.ACCOUNT_MISMATCH
    # 换回正确的号（登录窗出现又消失 → 立即复核）
    fb.logged_in, fb.window_class, fb.dialogs = False, "mmui::LoginWindow", ["进入WeChat"]
    svc.tick()
    fb.logged_in, fb.window_class, fb.dialogs = True, "mmui::MainWindow", []
    fb.self_nick, fb.self_wxid = "小北", "xb_2020"
    svc.tick()
    assert not svc.frozen() and svc.account_wxid == "xb_2020"
    assert br.heartbeats[-1]["stats"]["account_mismatch"] is False


def test_account_mismatch_switches_to_other_window_when_bound_account_is_there():
    from src.integrations.wechat_pc.service import AccountBinding
    svc, fb, br, notes, clock = _svc("auto_reply", account_binding=AccountBinding(expected_wxid="xb_2020"))
    fb.self_nick, fb.self_wxid = "小南", "xn_1999"          # 本窗（pid 501）登的是 B 号
    fb.other_wechats = {777: {"nick": "路人", "wxid": "lr_1"}, 502: {"nick": "小北", "wxid": "xb_2020"}}
    fb.sessions = [SessionRow("张三", unread=1)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    svc.tick()
    assert not svc.frozen(), "另一个窗里登着绑定的号 → 改盯它，不冻结"
    assert fb.pid == 502 and svc.account_wxid == "xb_2020" and svc.account_nick == "小北"
    assert [a for a in fb.actions if a[0] == "retarget"] == [("retarget", 777), ("retarget", 502)], "逐个试、对上就停"
    assert ("account_switched", "501->502:xb_2020") in notes
    assert not any(k == "account_mismatch" for k, _ in notes)
    st = br.heartbeats[-1]["stats"]
    assert st["account_mismatch"] is False and st["account_wxid"] == "xb_2020"
    # 切窗后这一轮照常收信（入站不再被串号挡住）
    assert br.ingested and any(a[0] == "open_session" for a in fb.actions)


def test_account_mismatch_restores_origin_window_and_blocks_inbound_when_no_window_matches():
    from src.integrations.wechat_pc.service import AccountBinding
    svc, fb, br, notes, clock = _svc("auto_reply", account_binding=AccountBinding(expected_wxid="xb_2020"))
    fb.self_nick, fb.self_wxid = "小南", "xn_1999"
    fb.other_wechats = {777: {"nick": "路人", "wxid": "lr_1"}}
    fb.sessions = [SessionRow("张三", unread=1)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    svc.tick()
    assert svc.frozen() and svc.stats.freeze_reason == svc.ACCOUNT_MISMATCH
    assert fb.pid == 501, "候选都不是绑定的号 → 切回原窗"
    assert [a for a in fb.actions if a[0] == "retarget"] == [("retarget", 777), ("retarget", 501)]
    assert not br.ingested, "别人号的来信不能记到本账号头上"
    assert not any(a[0] == "open_session" for a in fb.actions)
    # 串号期间按短周期复核（不是 30 分钟）：B 号窗里换回了自己的号 → 恢复
    clock["t"] += svc.ACCOUNT_IDENTITY_RETRY_SEC + 1
    fb.self_nick, fb.self_wxid = "小北", "xb_2020"
    svc.tick()
    assert not svc.frozen() and br.ingested


def test_account_identity_read_is_rate_limited_and_rechecked_after_relogin():
    svc, fb, br, notes, clock = _svc()
    reads = lambda: sum(1 for a in fb.actions if a[0] == "read_self_identity")  # noqa: E731
    svc.tick()
    svc.tick()
    assert reads() == 1, "读不到（资料卡没开）也不能每轮点头像"
    clock["t"] += svc.ACCOUNT_IDENTITY_RETRY_SEC + 1
    fb.self_wxid = "xb_2020"
    svc.tick()
    assert reads() == 2 and svc.account_wxid == "xb_2020"
    clock["t"] += 600
    svc.tick()
    assert reads() == 2, "读到了就按长周期复核"
    clock["t"] += svc.ACCOUNT_IDENTITY_RECHECK_SEC
    svc.tick()
    assert reads() == 3
    # 登录窗出现又消失（可能换号）→ 下一轮立即复核
    fb.logged_in, fb.window_class, fb.dialogs = False, "mmui::LoginWindow", ["进入WeChat"]
    svc.tick()
    fb.logged_in, fb.window_class, fb.dialogs = True, "mmui::MainWindow", []
    svc.tick()
    assert reads() == 4


# ── 真机锚点对应的纯函数（2026-09-08 微信 4.1.12.55 探针实录） ──────────────

def test_session_cell_name_parsing_matches_probe_samples():
    from src.integrations.wechat_pc.uia_backend import parse_session_cell_name as P
    assert P("无界科技BOUNDELESS\n说得好\n02:27\n") == ("无界科技BOUNDELESS", "说得好", "02:27", 0)
    assert P("腾讯新闻\n[4条] \n油价即将调整，9月11日24时或有大变动\n02:25\n") == (
        "腾讯新闻", "油价即将调整，9月11日24时或有大变动", "02:25", 4)
    assert P("文件传输助手\n\n\n") == ("文件传输助手", "", "", 0)
    assert P("张三\n[2条] 在吗\n昨天\n") == ("张三", "在吗", "昨天", 2)
    assert P("") == ("", "", "", 0)


def test_unread_flags_track_content_not_position():
    """真机实锤：新消息把会话顶到第一位；按位置记基线会漏掉新顶上来的、误报被顶下去的。"""
    from src.integrations.wechat_pc.uia_backend import compute_unread_flags as F
    a0, b0 = "A\n说得好\n02:27\n", "B\n急急急\n02:27\n"
    sysrow, ft = "腾讯新闻\n[4条] \n油价\n02:25\n", "文件传输助手\n\n\n"
    # 首轮：非系统、有预览的前 N 个引导读取；系统号按 [N条] 计但不参与引导
    assert F([a0, b0, ft, sysrow], set(), 5) == [1, 1, 0, 4]
    last = {a0, b0, ft, sysrow}
    # 无变化：全 0（系统号仍按角标）
    assert F([a0, b0, ft, sysrow], last, 5) == [0, 0, 0, 4]
    # B 来了新消息并顶到第一位：只有 B 标 1，A 不误报
    b1 = "B\n新消息\n04:11\n"
    assert F([b1, a0, ft, sysrow], last, 5) == [1, 0, 0, 4]
    # 己方给文件传输助手发了一条（顶到第一位、预览出现）：仅它标 1
    ft1 = "文件传输助手\n联调自检 ping\n04:14\n"
    assert F([ft1, b1, a0, sysrow], {b1, a0, ft, sysrow}, 5) == [1, 0, 0, 4]


def test_bubbles_without_time_label_are_deduped_against_backend_thread():
    """真机实锤：聊天变长后可见区顶部的旧气泡前面没有时间条（ts_hint=0）——不能按「现在」再入一遍；
    以后端线程里同向同文是否已存在去重；真正的新消息（线程里没有）照常入站。"""
    svc, fb, br, notes, clock = _svc("copilot")
    fb.sessions = [SessionRow("张三", unread=1)]
    fb.messages["张三"] = [Bubble("急急急", runtime_id="1", ts_hint=1_757_200_000.0),
                          Bubble("收到", is_self=True, runtime_id="2", ts_hint=1_757_200_060.0)]
    svc.tick()
    assert [p["text"] for p in br.ingested] == ["急急急", "收到"]
    # 重启后（进程内状态清空、指纹库也没带）：同样两条现在没有时间条 + 一条真正的新消息
    svc2, fb2, br2, _, clock2 = _svc("copilot")
    br2.ingested = list(br.ingested)                       # 后端线程里已有这两条
    fb2.sessions = [SessionRow("张三", unread=1)]
    fb2.messages["张三"] = [Bubble("急急急", runtime_id="9"), Bubble("收到", is_self=True, runtime_id="8"),
                           Bubble("新问题来了", runtime_id="7")]
    svc2.tick()
    assert [p["text"] for p in br2.ingested[2:]] == ["新问题来了"], br2.ingested
    assert svc2.stats.deduped == 2
    # 客户在没有时间条的位置又发了一条同文的（线程里已有「急急急」）→ 被去重（已知取舍：宁漏同文不重复刷屏）
    fb2.sessions[0].unread = 1
    fb2.messages["张三"].append(Bubble("急急急", runtime_id="6"))
    svc2.tick()
    assert len(br2.ingested) == 3


def test_backfill_ts_hints_uses_next_time_label_as_upper_bound():
    from src.integrations.wechat_pc.uia_backend import backfill_ts_hints
    from src.integrations.wechat_pc.service import content_fingerprint, time_unknown
    T = 1_757_300_000.0
    bubbles = [Bubble("很久以前的话", runtime_id="1"),                 # 顶部：前面的时间条已卷出可见区
               Bubble("也是旧的", is_self=True, runtime_id="2"),
               Bubble("04:47", kind="system", ts_hint=T),
               Bubble("收到测试", runtime_id="3", ts_hint=T),
               Bubble("08:52", kind="system", ts_hint=T + 4 * 3600),
               Bubble("最新一条", runtime_id="4", ts_hint=T + 4 * 3600)]
    out = backfill_ts_hints(bubbles)
    assert out[0].ts_hint == T - 1 and out[0].ts_is_upper_bound is True
    assert out[1].ts_hint == T - 1 and out[1].ts_is_upper_bound is True
    assert out[3].ts_hint == T and out[3].ts_is_upper_bound is False and out[5].ts_hint == T + 4 * 3600
    # 上界时间不参与指纹：与「完全没时间」的同文气泡指纹一致，避免同一条旧消息因上界变化再入一次
    assert time_unknown(out[0]) and not time_unknown(out[3])
    assert content_fingerprint("k", out[0]) == content_fingerprint("k", Bubble("很久以前的话"))
    # 一条时间条都没有：保持 0（服务按「现在」入站 + 线程同文去重）
    plain = backfill_ts_hints([Bubble("x", runtime_id="9")])
    assert plain[0].ts_hint == 0.0 and plain[0].ts_is_upper_bound is False


def test_recycled_runtime_ids_do_not_swallow_new_bubbles():
    """真机实锤：RecyclerListView 回收条目视图，新气泡可能复用旧 RuntimeId → 去重必须按内容而非 RuntimeId。"""
    svc, fb, br, notes, clock = _svc("copilot")
    fb.sessions = [SessionRow("张三", unread=1)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="42", ts_hint=1_757_300_000.0)]
    svc.tick()
    assert [p["text"] for p in br.ingested] == ["在吗"]
    # 新消息复用了 RuntimeId 42（旧条目被回收）：仍要入站
    fb.sessions[0].unread = 1
    fb.messages["张三"] = [Bubble("报个价", runtime_id="42", ts_hint=1_757_300_060.0)]
    svc.tick()
    assert [p["text"] for p in br.ingested] == ["在吗", "报个价"]
    # 同一条气泡再次被读到（RuntimeId 变了也好、没变也好）：按内容只入一次
    fb.sessions[0].unread = 1
    fb.messages["张三"] = [Bubble("报个价", runtime_id="7", ts_hint=1_757_300_060.0)]
    svc.tick()
    assert len(br.ingested) == 2


def test_readonly_lock_is_rechecked_when_window_comes_back():
    """真机实锤：启动时微信收在托盘 → 自检失败锁只读；窗口回来后必须重新自检解锁，否则永远只读。"""
    class _Locked(FakeBackend):
        def __init__(self):
            super().__init__()
            self.readonly = True
            self.checks = 0

        def self_check(self):
            self.checks += 1
            self.readonly = False
            return {"ok": True}
    svc, fb, br, notes, clock = _svc("semi")
    lb = _Locked(); lb.sessions = [SessionRow("张三", unread=0)]
    svc.backend = lb
    svc.tick()
    assert lb.checks == 1 and lb.readonly is False, "可读屏 + 处于只读锁 → 重新自检并解锁"
    svc.tick()
    assert lb.checks == 1, "已解锁不再重复自检"


def test_heartbeat_thread_is_independent_of_slow_ticks():
    """常驻模式：心跳线程按固定间隔报，单轮耗时再长在线态也不抖；停线程后 tick 恢复轮末报。"""
    import threading
    svc, fb, br, notes, clock = _svc("copilot")
    beats: List[Dict[str, Any]] = []
    lock = threading.Lock()

    def _hb(account_id, *, tier, readonly, stats, label=""):
        with lock:
            beats.append({"tier": tier, "readonly": readonly, "label": label, "ticks": stats.get("ticks")})
        return True

    br.heartbeat = _hb
    svc.account_label = "个人微信 · PC 副驾"
    svc.start_heartbeat_thread(interval_sec=2.0)   # 下限 2s
    svc.start_heartbeat_thread(interval_sec=2.0)   # 重复调用无效
    import time as _t
    _t.sleep(0.3)
    with lock:
        n0 = len(beats)
    assert n0 >= 1 and beats[0]["tier"] == "copilot" and beats[0]["readonly"] is True
    assert beats[0]["label"] == "个人微信 · PC 副驾"
    svc.tick()                                     # 线程在跑：轮末不额外报
    with lock:
        assert len(beats) == n0
    svc.stop_heartbeat_thread()
    assert svc._hb_thread is None
    svc.tick()                                     # 线程停了：轮末报一次
    with lock:
        assert len(beats) == n0 + 1 and beats[-1]["ticks"] == 2


def test_bubble_kind_by_class_then_placeholder():
    from src.integrations.wechat_pc.uia_backend import bubble_kind_for as K
    assert K("mmui::ChatTextItemView", "说得好") == "text"
    assert K("mmui::ChatImageItemView", "") == "image" and K("mmui::ChatVoiceItemView", "3''") == "voice"
    assert K("mmui::ChatItemView", "02:27") == "system"
    # 类名认不出 → 看微信占位文本
    assert K("mmui::ChatUnknownItemView", "[图片]") == "image"
    assert K("mmui::ChatUnknownItemView", "[语音] 5\"") == "voice"
    assert K("mmui::ChatUnknownItemView", "[转账]") == "transfer" and K("", "[红包]恭喜发财") == "redpacket"
    assert K("", "[Photo]") == "image" and K("", "[文件] 报价单.pdf") == "file"
    assert K("mmui::ChatUnknownItemView", "普通文字") == "text" and K("mmui::ChatUnknownItemView", "") == "unknown"


def test_avatar_zone_direction_is_theme_agnostic():
    """真机量测（深色主题）：对方行头像在左 4–8%、己方行头像在右 92–96%；行上边缘一律是背景。"""
    from src.integrations.wechat_pc.uia_backend import background_reference as R, classify_bubble_direction as D
    dark_bg, light_bg = 0x1E1E1F, 0xF2F2F2
    avatar = [0x1B2959, 0x1F2253, 0x251E48, 0x060610, 0x0E0B12]          # 头像像素（任意彩色）
    assert R([dark_bg] * 5) == dark_bg and R([dark_bg, dark_bg, dark_bg, 0x424245, dark_bg]) == dark_bg
    assert R([1, 2, 3, 4, 5]) is None and R([]) is None
    # 深色：左区脏、右区净 → 对方；右区脏、左区净 → 己方
    left_dirty = avatar + [dark_bg] * 22
    clean = [dark_bg] * 27
    assert D(dark_bg, left_dirty, clean, [], []) is False
    assert D(dark_bg, clean, left_dirty, [], []) is True
    # 浅色主题同样成立（不依赖任何主题色）
    assert D(light_bg, avatar + [light_bg] * 22, [light_bg] * 27, [], []) is False
    assert D(light_bg, [light_bg] * 27, [light_bg] * 20 + avatar, [], []) is True
    # 两侧都脏（悬停高亮/参考色取错）→ 退回主题色规则；内区也判不出 → None
    assert D(dark_bg, left_dirty, left_dirty, [dark_bg], [dark_bg]) is None
    assert D(dark_bg, left_dirty, left_dirty, [0x35D28D], []) is True, "兜底：右内区深色绿＝己方"
    # 没有参考色 → 直接兜底
    assert D(None, left_dirty, clean, [], [0xFFFFFF]) is False
    # 头像只沾到 1–2 个采样点（极窄窗口）不算：宁判不出也不猜
    assert D(dark_bg, avatar[:2] + [dark_bg] * 25, clean, [], []) is None


def test_bubble_direction_by_pixels():
    from src.integrations.wechat_pc.uia_backend import bubble_direction_by_pixels as D
    green, white, bg_light, bg_dark, dark_green = 0xFF95EC69, 0xFFFFFFFF, 0xFFF2F2F2, 0xFF191919, 0xFF3EB575
    assert D([bg_light, green], [bg_light]) is True, "右侧微信绿＝己方"
    assert D([bg_light, dark_green], [bg_dark]) is True, "深色模式己方绿"
    assert D([bg_light], [bg_light, white]) is False, "左侧白泡＝对方"
    assert D([bg_light], [bg_light]) is None, "两侧都是底色＝判不出"
    assert D([], []) is None


def test_same_name_contacts_resolve_to_distinct_keys_via_profile():
    """真机实锤：两个「无界科技BOUNDELESS」会话名/AutomationId 完全相同，只能靠资料卡微信号区分。"""
    svc, fb, br, notes, clock = _svc("copilot")
    fb.sessions = [SessionRow("无界科技BOUNDELESS", unread=1, index=0),
                   SessionRow("无界科技BOUNDELESS", unread=1, index=1)]
    fb.wxids_by_index = {0: "zhu0396000", 1: "boundless_two"}
    fb.messages["无界科技BOUNDELESS"] = [Bubble("说得好", runtime_id="a")]
    svc.tick()
    keys = [p["chat_key"] for p in br.ingested]
    assert set(keys) == {"wx:id:zhu0396000", "wx:id:boundless_two"}, keys
    assert svc.identity.display_name_for("wx:id:zhu0396000") == "无界科技BOUNDELESS"
    # 两格各自读了一次资料卡
    assert sum(1 for a in fb.actions if a[0] == "read_profile_wxid") == 2


def test_profile_lookup_is_skipped_for_unique_recently_verified_names():
    """资料卡核对＝侧栏+弹窗闪 4 秒：唯一显示名 10 分钟内只核对一次；同名歧义永远核对。"""
    svc, fb, br, notes, clock = _svc("semi")
    fb.sessions = [SessionRow("张三", unread=1), SessionRow("李四", unread=0)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    fb.wxids["张三"] = "zhangsan_1"
    svc.tick()
    lookups = lambda: sum(1 for a in fb.actions if a[0] == "read_profile_wxid")  # noqa: E731
    assert lookups() == 1 and svc.stats.profile_lookups == 1
    # 2 分钟后又来一条：不再开资料卡，仍归到同一微信号级 key
    clock["t"] += 120
    fb.sessions[0].unread = 1
    fb.messages["张三"].append(Bubble("报个价", runtime_id="2"))
    svc.tick()
    assert lookups() == 1 and {p["chat_key"] for p in br.ingested} == {"wx:id:zhangsan_1"}
    # 出站：唯一且刚核对过 → 不带 expected_wxid（省一次弹窗）
    br.queue = [{"id": 1, "chat_key": "wx:id:zhangsan_1", "text": "在的", "kind": "manual"}]
    svc.tick()
    assert (1, True, "") in br.acks
    assert [a for a in fb.actions if a[0] == "open_session"][-1] == ("open_session", "张三", "", -1)
    # 超过 10 分钟 → 重新核对一次
    clock["t"] += 700
    fb.sessions[0].unread = 1
    fb.messages["张三"].append(Bubble("还在吗", runtime_id="3"))
    svc.tick()
    assert lookups() == 2
    # 列表里出现同名第二人 → 两格都核对；此后该名字即使只剩一格可见也永远核对
    fb.sessions = [SessionRow("张三", unread=1, index=0), SessionRow("张三", unread=1, index=1)]
    fb.wxids_by_index = {0: "zhangsan_1", 1: "zhangsan_2"}
    fb.messages["张三"] = [Bubble("hello", runtime_id="4")]
    svc.tick()
    assert lookups() == 4
    fb.sessions = [SessionRow("张三", unread=1, index=0)]
    fb.messages["张三"] = [Bubble("hello again", runtime_id="5")]
    svc.tick()
    assert lookups() == 5, "缓存里该名字对应 2 个微信号 → 即便只剩一格也核对"
    # 出站到有歧义的名字：必须带 expected_wxid 逐格核对
    br.queue = [{"id": 2, "chat_key": "wx:id:zhangsan_2", "text": "给第二个", "kind": "manual"}]
    fb.sessions = [SessionRow("张三", index=0), SessionRow("张三", index=1)]
    svc._last_inbound["wx:id:zhangsan_2"] = clock["t"]
    svc.tick()
    assert (2, True, "") in br.acks and fb.current_index == 1


def test_group_title_detection():
    assert I.parse_group_title("无界产品群 (12)") == ("无界产品群", 12)
    assert I.parse_group_title("无界产品群（5）") == ("无界产品群", 5)
    assert I.parse_group_title("张三") == ("张三", 0)
    assert I.is_group_title("无界产品群 (12)", "无界产品群") is True
    assert I.is_group_title("无界产品群(12)", "无界产品群") is True
    assert I.is_group_title("张三(2)", "张三(2)") is False, "备注本身带括号数字：两边一致不是群"
    assert I.is_group_title("张三", "张三") is False
    assert I.is_group_title("李四 (12)", "张三") is False, "名字对不上不是同一会话"
    from src.integrations.wechat_pc.send_guard import verify_title
    assert verify_title("无界产品群 (12)", "无界产品群") and verify_title("无界产品群(12)", "无界产品群")


def test_groups_are_detected_from_title_and_never_auto_replied():
    """会话格只有群名、聊天标题带成员数 → 标群；auto_reply 档默认群策略 mention_only，没被 @ 就拒发。"""
    svc, fb, br, notes, clock = _svc("auto_reply")
    fb.sessions = [SessionRow("无界产品群", unread=1)]
    fb.titles["无界产品群"] = "无界产品群 (12)"
    fb.messages["无界产品群"] = [Bubble("大家好", runtime_id="1")]
    svc.tick()
    assert svc.stats.groups_seen == 1
    assert [p["chat_key"] for p in br.ingested] == ["wx:group:无界产品群"]
    assert not any(a[0] == "read_profile_wxid" for a in fb.actions), "群聊不开资料卡"
    br.queue = [{"id": 1, "chat_key": "wx:group:无界产品群", "text": "自动稿", "kind": "text"}]
    svc.tick()
    assert any(a[0] == 1 and a[1] is False and a[2] in ("policy:group_not_mentioned", "policy:group_replies_disabled")
               for a in br.acks), br.acks


def test_sender_waits_while_peer_is_typing():
    fb = FakeBackend()
    fb.sessions = [SessionRow("张三")]
    fb.typing_polls_left = 3          # 对方还要「打字」3 次轮询
    slept: List[float] = []
    g = GuardedSender(fb, sleep=lambda s: slept.append(s), read_pause_sec=0, echo_wait_sec=0,
                      typing_wait_max_sec=8.0, typing_poll_sec=1.0)
    out = g.send("张三", "别急，我看看")
    assert out.ok and sum(1 for a in fb.actions if a[0] == "peer_typing") == 4
    assert any(t.startswith("waited typing 3s") for t in out.trace), out.trace
    assert slept.count(1.0) == 3
    # 上限兜底：对方一直在打也最多等 typing_wait_max_sec
    fb2 = FakeBackend(); fb2.sessions = [SessionRow("张三")]; fb2.typing_polls_left = 99
    g2 = GuardedSender(fb2, sleep=lambda s: None, read_pause_sec=0, echo_wait_sec=0,
                       typing_wait_max_sec=3.0, typing_poll_sec=1.0)
    assert g2.send("张三", "x").ok and fb2.typing_polls_left == 96


def test_send_to_same_name_contact_verifies_identity():
    fb = FakeBackend()
    fb.sessions = [SessionRow("无界科技BOUNDELESS", index=0), SessionRow("无界科技BOUNDELESS", index=1)]
    fb.wxids_by_index = {0: "zhu0396000", 1: "boundless_two"}
    out = _sender(fb).send("无界科技BOUNDELESS", "给第二个号的", expected_wxid="boundless_two")
    assert out.ok and fb.current_index == 1, out.as_dict()
    bad = _sender(fb).send("无界科技BOUNDELESS", "x", expected_wxid="nobody")
    assert not bad.ok and bad.stage == "open" and bad.reason == "identity_mismatch"


def test_time_label_parsing():
    import datetime as dt
    from src.integrations.wechat_pc.uia_backend import parse_time_label as T
    now = dt.datetime(2026, 9, 8, 10, 0).timestamp()
    assert dt.datetime.fromtimestamp(T("02:27", now)) == dt.datetime(2026, 9, 8, 2, 27)
    assert dt.datetime.fromtimestamp(T("昨天 23:05", now)) == dt.datetime(2026, 9, 7, 23, 5)
    assert dt.datetime.fromtimestamp(T("9月1日 08:00", now)) == dt.datetime(2026, 9, 1, 8, 0)
    assert dt.datetime.fromtimestamp(T("2025年12月31日 18:30", now)) == dt.datetime(2025, 12, 31, 18, 30)
    wed = dt.datetime.fromtimestamp(T("星期三 09:15", now))
    assert wed.weekday() == 2 and wed < dt.datetime.fromtimestamp(now)
    assert T("说得好", now) == 0.0 and T("", now) == 0.0 and T("99:99", now) == 0.0


def test_persistent_seen_store_dedupes_across_restarts(tmp_path):
    from src.integrations.wechat_pc.service import SeenStore, content_fingerprint
    path = str(tmp_path / "seen.json")
    b = Bubble("你好", ts_hint=1_757_300_000.0)
    fp = content_fingerprint("wx:id:a", b)
    assert fp == content_fingerprint("wx:id:a", Bubble("你好", ts_hint=1_757_300_030.0)), "同一分钟同文同向＝同指纹"
    assert fp != content_fingerprint("wx:id:a", Bubble("你好", is_self=True, ts_hint=1_757_300_000.0))
    s1 = SeenStore(path)
    assert fp not in s1
    s1.add(fp); s1.flush()
    s2 = SeenStore(path)   # 模拟进程重启
    assert fp in s2
    # 服务层：重启后同一批可见气泡不再重复入站
    svc, fb, br, notes, clock = _svc("copilot", seen_store=SeenStore(path))
    fb.sessions = [SessionRow("张三", unread=1)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1", ts_hint=1_757_300_000.0)]
    svc.tick()
    assert len(br.ingested) == 1
    svc2, fb2, br2, _, _ = _svc("copilot", seen_store=SeenStore(path))
    fb2.sessions = [SessionRow("张三", unread=1)]
    fb2.messages["张三"] = [Bubble("在吗", runtime_id="99", ts_hint=1_757_300_010.0)]
    svc2.tick()
    assert br2.ingested == [] and svc2.stats.deduped == 1
    key = svc2.identity.resolve(display_name="张三")
    assert svc2._last_inbound.get(key) == 1_757_300_010.0, "被去重的对方旧气泡仍更新「对方最近来信」（仅回复档依赖）"


def test_identity_cache_persists_wxid_entries(tmp_path):
    path = str(tmp_path / "identity.json")
    c = I.ChatIdentityCache(path=path)
    c.resolve(display_name="张三", wxid="zhangsan_88")
    c.resolve(display_name="临时名")          # 显示名级条目不落盘
    c2 = I.ChatIdentityCache(path=path)     # 重启
    assert c2.display_name_for("wx:id:zhangsan_88") == "张三"
    assert c2.display_name_for("wx:name:临时名") == ""
    assert c2.resolve(display_name="张三") == "wx:id:zhangsan_88", "重启后按名也能对回微信号级 key"


def test_ingest_failure_is_counted_and_retried():
    svc, fb, br, notes, clock = _svc("copilot")
    fb.sessions = [SessionRow("张三", unread=1)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    br._http_ok = False
    orig = br._http

    def _flaky(method, url, body):
        if url.endswith("/api/desktop/ingest") and not br._http_ok:
            return 503, {"ok": False}
        return orig(method, url, body)

    br._http = _flaky
    svc.tick()
    assert svc.stats.ingest_failed == 1 and br.ingested == []
    br._http_ok = True
    fb.sessions[0].unread = 1
    svc.tick()
    assert len(br.ingested) == 1, "失败的那条下轮重试成功"


def test_unknown_direction_bubbles_are_skipped_not_guessed():
    svc, fb, br, notes, clock = _svc("copilot")
    fb.sessions = [SessionRow("张三", unread=1)]
    b = Bubble("模糊的一条", runtime_id="u1")
    b.direction_known = False
    fb.messages["张三"] = [b, Bubble("清楚的一条", runtime_id="u2")]
    svc.tick()
    assert [p["text"] for p in br.ingested] == ["清楚的一条"]
    assert svc.stats.unknown_direction == 1
    assert not any(k == "window_minimized" for k, _ in notes), "窗口没最小化就不误报"
    # 窗口最小化 → 判不出方向时提醒主人一次；还原后再最小化会再提醒
    fb.window_minimized = lambda: True
    fb.sessions[0].unread = 1
    b2 = Bubble("又一条", runtime_id="u3"); b2.direction_known = False
    fb.messages["张三"].append(b2)
    svc.tick(); svc.tick()
    assert sum(1 for k, _ in notes if k == "window_minimized") == 1
    fb.window_minimized = lambda: False
    b3 = Bubble("还原后", runtime_id="u4"); b3.direction_known = False
    fb.messages["张三"].append(b3); fb.sessions[0].unread = 1
    svc.tick()
    fb.window_minimized = lambda: True
    b4 = Bubble("再最小化", runtime_id="u5"); b4.direction_known = False
    fb.messages["张三"].append(b4); fb.sessions[0].unread = 1
    svc.tick()
    assert sum(1 for k, _ in notes if k == "window_minimized") == 2


# ── 无障碍树空壳自愈（2026-09-19：ShowWindow 拉回的主窗 UIA 树为空 → 托盘单击重建）──
def test_a11y_rebuild_only_when_tree_empty_with_cooldown(monkeypatch):
    from src.integrations.wechat_pc import env_check as EC
    svc, fb, br, notes, clock = _svc()
    fb.main_hwnd = lambda: 4242
    calls = {"empty": 0, "rebuild": 0}
    state = {"empty": True}
    monkeypatch.setattr(EC, "accessibility_tree_empty", lambda h: (calls.__setitem__("empty", calls["empty"] + 1), state["empty"])[1])
    monkeypatch.setattr(EC, "rebuild_accessibility_via_tray",
                        lambda h: (calls.__setitem__("rebuild", calls["rebuild"] + 1), {"ok": True, "tree_rebuilt": True, "via": "tray"})[1])
    svc._maybe_rebuild_accessibility()
    assert calls == {"empty": 1, "rebuild": 1}
    assert ("a11y_rebuilt", "tray") in notes
    # 冷却期内再叫：什么都不做（失败不狂点托盘）
    svc._maybe_rebuild_accessibility()
    assert calls == {"empty": 1, "rebuild": 1}
    # 冷却过了、树不空 → 只探不重建
    clock["t"] += svc.A11Y_REBUILD_COOLDOWN_SEC + 1
    state["empty"] = False
    svc._maybe_rebuild_accessibility()
    assert calls == {"empty": 2, "rebuild": 1}


def test_a11y_rebuild_noop_without_hwnd_or_capability(monkeypatch):
    from src.integrations.wechat_pc import env_check as EC
    svc, fb, br, notes, clock = _svc()
    boom = lambda h: (_ for _ in ()).throw(AssertionError("不该探树"))
    monkeypatch.setattr(EC, "accessibility_tree_empty", boom)
    svc._maybe_rebuild_accessibility()          # FakeBackend 没有 main_hwnd → 直接返回
    fb.main_hwnd = lambda: 0
    svc._maybe_rebuild_accessibility()          # 句柄 0 → 直接返回
    assert not notes


def test_readable_check_triggers_a11y_rebuild_when_session_list_missing(monkeypatch):
    from src.integrations.wechat_pc import env_check as EC
    svc, fb, br, notes, clock = _svc()
    fb.main_hwnd = lambda: 4242
    fb.readonly = True
    fb.self_check = lambda: {"ok": False, "missing": ["session_list"], "readonly": True}
    hits = []
    monkeypatch.setattr(EC, "accessibility_tree_empty", lambda h: True)
    monkeypatch.setattr(EC, "rebuild_accessibility_via_tray",
                        lambda h: (hits.append(h), {"ok": True, "tree_rebuilt": True, "via": "tray"})[1])
    svc.tick()
    assert hits == [4242]
    # 只缺 composer（会话没点开）不算空壳 → 不动托盘
    fb.self_check = lambda: {"ok": False, "missing": ["composer"], "readonly": True}
    clock["t"] += svc.A11Y_REBUILD_COOLDOWN_SEC + 1
    svc.tick()
    assert hits == [4242]


def test_tray_match_icon_prefers_pid_then_name():
    from src.integrations.wechat_pc.win32_tray import TrayIcon, match_icon
    a = TrayIcon("visible", 0, "微信", 111, False, (0, 0, 1, 1), 1)
    b = TrayIcon("overflow", 3, "WeChat", 222, True, (0, 0, 1, 1), 2)
    c = TrayIcon("visible", 5, "QQ", 333, False, (0, 0, 1, 1), 1)
    assert match_icon([c, b, a], pids=(222,)) is b
    assert match_icon([c, a, b], pids=(999,), names=("WeChat",)) is b, "pid 没命中回落 tooltip"
    assert match_icon([c, a], names=("微信",)) is a
    assert match_icon([c], pids=(111,), names=("微信",)) is None


# ── min_gap 瞬态拒发 → 本地挂起（2026-09-19 真机：分条语音第二条被 min_gap 判失败进人审）──
def test_min_gap_denial_defers_instead_of_failing_and_sends_next_round():
    svc, fb, br, notes, clock = _svc("auto_reply")
    fb.sessions = [SessionRow("张三", unread=1)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    svc.tick()
    br.queue = [{"id": 21, "chat_key": "wx:name:张三", "text": "第一条", "kind": "text"},
                {"id": 22, "chat_key": "wx:name:张三", "text": "第二条", "kind": "text"}]
    clock["t"] += 30
    svc.tick()
    assert (21, True, "") in br.acks
    assert not [a for a in br.acks if a[0] == 22], "第二条 min_gap 未到：不回执、不算失败"
    assert svc.stats.deferred == 1 and svc.stats.denied == 0 and 22 in svc._deferred
    # 间隔仍未到：继续挂着（不重复计数），队列里也没有它（保持认领态，不被重复认领）
    clock["t"] += 5
    svc.tick()
    assert not [a for a in br.acks if a[0] == 22] and svc.stats.deferred == 1
    # 间隔过去 → 本轮先发挂起的，再处理新认领的
    br.queue = [{"id": 23, "chat_key": "wx:name:李四", "text": "别人的", "kind": "text"}]
    svc._last_inbound["wx:name:李四"] = clock["t"]
    fb.sessions.append(SessionRow("李四"))
    clock["t"] += svc.policy.min_gap_sec
    svc.tick()
    assert (22, True, "") in br.acks and (23, True, "") in br.acks
    assert [a[0] for a in br.acks if a[0] in (22, 23)] == [22, 23], "挂起项先于新认领项"
    assert not svc._deferred and svc.stats.sent == 3
    assert fb.messages["张三"][-1].text == "第二条"


def test_policy_voice_quota_and_part_gap():
    """P2：语音同时计入总配额且单独更保守；同一 reply_group 的分条条间只吃 voice_part_gap_sec。"""
    pol = P.resolve_policy({"platform_login": {"wechat_pc": {
        "tier": "auto_reply", "risk_ack": True, "work_hours": [0, 24],
        "voice_daily_cap": 5, "voice_per_peer_daily_cap": 2, "voice_part_gap_sec": 2.0}}})
    assert (pol.voice_daily_cap, pol.voice_per_peer_daily_cap, pol.voice_part_gap_sec) == (5, 2, 2.0)
    # 默认值 + 夹逼
    d = P.resolve_policy({"platform_login": {"wechat_pc": {"voice_daily_cap": 9999, "voice_part_gap_sec": 0.01}}})
    assert (d.voice_daily_cap, d.voice_per_peer_daily_cap, d.voice_part_gap_sec) == (200, 6, 0.5)
    now = datetime(2026, 9, 19, 12, 0, 0)
    base = dict(now=now, connected_at=now.timestamp() - 30 * 86400, sent_today=0, sent_today_to_peer=0,
                last_sent_to_peer_ts=0.0, last_inbound_from_peer_ts=now.timestamp() - 60)
    assert P.may_send(pol, kind="voice", **base).allowed
    assert P.may_send(pol, kind="voice", voice_sent_today=5, **base).reason == "voice_daily_cap"
    assert P.may_send(pol, kind="voice", voice_sent_today_to_peer=2, **base).reason == "voice_per_peer_daily_cap"
    # 文字不受语音配额影响
    assert P.may_send(pol, kind="text", voice_sent_today=99, voice_sent_today_to_peer=99, **base).allowed
    # 总配额先于语音配额
    assert P.may_send(pol, kind="voice", voice_sent_today=5, **dict(base, sent_today_to_peer=15)).reason == "per_peer_daily_cap"
    # 分条节奏：同组 3s 前发过 → 放行；不同组照旧 min_gap（20s）
    recent = dict(base, last_sent_to_peer_ts=now.timestamp() - 3)
    assert P.may_send(pol, kind="voice", continues_last_group=True, **recent).allowed
    assert P.may_send(pol, kind="voice", **recent).reason == "min_gap"
    assert P.may_send(pol, kind="voice", continues_last_group=True,
                      **dict(base, last_sent_to_peer_ts=now.timestamp() - 1)).reason == "min_gap"
    assert P.VOICE_QUOTA_DENIALS == {"voice_daily_cap", "voice_per_peer_daily_cap"}


def test_service_sends_split_voice_parts_back_to_back_with_part_gap():
    """同一 reply_group 的 3 条语音一轮内连发：条间原地等 voice_part_gap_sec（不挂起到下轮）；随后的独立文字仍吃 min_gap。"""
    slept: List[float] = []
    voice = FakeVoice()
    svc, fb, br, notes, clock = _svc("auto_reply", voice=voice)
    svc._sleep = lambda s: (slept.append(s), clock.__setitem__("t", clock["t"] + s))
    fb.recorded_seconds = 9
    fb.sessions = [SessionRow("张三", unread=1)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    svc.tick()
    br.queue = [_voice_item(41, reply_group="vg-1"), _voice_item(42, reply_group="vg-1"), _voice_item(43, reply_group="vg-1"),
                {"id": 44, "chat_key": "wx:name:张三", "text": "独立文字", "kind": "text"}]
    clock["t"] += 30
    svc.tick()
    oks = [a[0] for a in br.acks if a[1]]
    assert oks == [41, 42, 43], br.acks
    gap = svc.policy.voice_part_gap_sec
    assert len(slept) == 2 and all(gap <= s <= gap + svc.PART_GAP_JITTER[1] + 1e-6 for s in slept), slept
    assert svc.stats.deferred == 1 and 44 in svc._deferred, "组外的文字紧跟其后 → 照旧 min_gap 挂起"
    assert svc._voice_sent_today == 3 and svc._voice_sent_today_peer["wx:name:张三"] == 3
    assert voice.played == 3


def test_split_parts_to_ambiguous_name_verify_identity_once_per_group():
    """同名歧义联系人：同一 reply_group 的首条带 expected_wxid 逐格核对；后续分条在 60s 内、当前会话标题仍是目标
    → 不再开资料卡（真机每次核对 6–8s，三条分条被拉成 18s 一条）；组外/超时的命令照旧核对。"""
    slept: List[float] = []
    voice = FakeVoice()
    svc, fb, br, notes, clock = _svc("auto_reply", voice=voice)
    svc._sleep = lambda s: (slept.append(s), clock.__setitem__("t", clock["t"] + s))
    fb.recorded_seconds = 9
    fb.sessions = [SessionRow("张三", unread=1, index=0), SessionRow("张三", unread=1, index=1)]
    fb.wxids_by_index = {0: "zhangsan_1", 1: "zhangsan_2"}
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    svc.tick()
    opens = lambda: [a for a in fb.actions if a[0] == "open_session"]  # noqa: E731
    n0 = len(opens())
    br.queue = [_voice_item(71, chat_key="wx:id:zhangsan_2", reply_group="vg-9"),
                _voice_item(72, chat_key="wx:id:zhangsan_2", reply_group="vg-9"),
                _voice_item(73, chat_key="wx:id:zhangsan_2", reply_group="vg-9")]
    fb.sessions = [SessionRow("张三", index=0), SessionRow("张三", index=1)]
    clock["t"] += 30
    svc.tick()
    assert [a[0] for a in br.acks if a[1]] == [71, 72, 73], br.acks
    wx = [a[2] for a in opens()[n0:]]
    assert wx == ["zhangsan_2", "", ""], wx
    assert fb.current_index == 1, "后续分条仍停在第二个张三的会话上"
    # 组外命令（另一稿子）→ 重新核对
    br.queue = [_voice_item(74, chat_key="wx:id:zhangsan_2", reply_group="vg-10")]
    clock["t"] += 30
    svc.tick()
    assert opens()[-1][2] == "zhangsan_2"
    # 同组但距上一条已超 TTL → 也重新核对
    br.queue = [_voice_item(75, chat_key="wx:id:zhangsan_2", reply_group="vg-10")]
    clock["t"] += svc.PART_IDENTITY_TTL_SEC + 5
    svc.tick()
    assert opens()[-1][2] == "zhangsan_2"


def test_service_voice_quota_denial_is_policy_ack_and_text_still_flows():
    svc, fb, br, notes, clock = _svc("auto_reply", voice=FakeVoice())
    fb.recorded_seconds = 9
    fb.sessions = [SessionRow("张三", unread=1)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    svc.tick()
    svc._roll_day()
    svc._voice_sent_today_peer["wx:name:张三"] = svc.policy.voice_per_peer_daily_cap
    br.queue = [_voice_item(51)]
    clock["t"] += 30
    svc.tick()
    assert (51, False, "policy:voice_per_peer_daily_cap") in br.acks and svc.stats.denied == 1
    # 语音配额不影响文字
    br.queue = [{"id": 52, "chat_key": "wx:name:张三", "text": "文字照发", "kind": "text"}]
    clock["t"] += 30
    svc.tick()
    assert (52, True, "") in br.acks
    # 心跳带配额用量（今日语音 / 日上限）
    seen: List[Dict[str, Any]] = []
    br.heartbeat = lambda account_id, **kw: (seen.append(kw), True)[1]
    svc._voice_sent_today = 4
    svc._heartbeat()
    assert seen and seen[-1]["stats"]["voice_today"] == 4
    assert seen[-1]["stats"]["voice_daily_cap"] == svc.policy.voice_daily_cap
    # 心跳 stats 只收标量（presence 端 heartbeat_meta 非标量会被 str()）→ 逗号串
    assert seen[-1]["stats"]["caps_relaxed"] == ""
    from dataclasses import replace
    svc.policy = replace(svc.policy, voice_daily_cap=100, min_gap_sec=5.0)
    svc._heartbeat()
    assert seen[-1]["stats"]["caps_relaxed"] == "min_gap_sec,voice_daily_cap"


def test_caps_relaxed_lists_only_looser_than_default():
    assert P.caps_relaxed(P.PcPolicy()) == {}
    loose = P.resolve_policy({"platform_login": {"wechat_pc": {
        "daily_cap": 200, "per_peer_daily_cap": 100, "min_gap_sec": 5, "voice_per_peer_daily_cap": 50,
        "daily_cap_new": 10, "voice_part_gap_sec": 4.0}}})
    r = P.caps_relaxed(loose)
    assert set(r) == {"daily_cap", "per_peer_daily_cap", "min_gap_sec", "voice_per_peer_daily_cap"}
    assert r["daily_cap"] == (200, 80) and r["min_gap_sec"] == (5.0, 20.0)


def test_deferred_item_dedups_against_requeued_pull_and_expires_to_policy_failure():
    svc, fb, br, notes, clock = _svc("auto_reply")
    fb.sessions = [SessionRow("张三", unread=1)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    svc.tick()
    svc._last_sent_peer["wx:name:张三"] = clock["t"]
    item = {"id": 31, "chat_key": "wx:name:张三", "text": "x", "kind": "text"}
    br.queue = [dict(item)]
    svc.tick()
    assert 31 in svc._deferred and not br.acks
    # 队列 180s 回收后又派回来同一条：按 id 去重，不会两份都处理
    svc._last_sent_peer["wx:name:张三"] = clock["t"]
    br.queue = [dict(item)]
    svc.tick()
    assert list(svc._deferred) == [31] and svc.stats.deferred == 1
    # 一直发不出去（每轮都被 min_gap 挡）超过 DEFER_MAX_SEC → 照旧回执 policy:min_gap
    clock["t"] += svc.DEFER_MAX_SEC + 1
    svc._last_sent_peer["wx:name:张三"] = clock["t"]
    svc.tick()
    assert (31, False, "policy:min_gap") in br.acks and not svc._deferred and svc.stats.denied == 1


def test_deferred_items_are_failed_out_when_service_freezes():
    svc, fb, br, notes, clock = _svc("auto_reply")
    fb.sessions = [SessionRow("张三", unread=1)]
    fb.messages["张三"] = [Bubble("在吗", runtime_id="1")]
    svc.tick()
    svc._last_sent_peer["wx:name:张三"] = clock["t"]
    br.queue = [{"id": 41, "chat_key": "wx:name:张三", "text": "x", "kind": "text"}]
    svc.tick()
    assert 41 in svc._deferred
    svc.freeze(600, "test")
    svc.tick()
    assert (41, False, "guard:frozen") in br.acks and not svc._deferred


# ── 会话隔离（2026-09-20 事故门禁）────────────────────────────────────────────

def test_session_isolation_only_fires_when_a_desktop_exists_elsewhere():
    """跨会话 ≠ 无人登录。后者没有可驱动的桌面，是另一种故障，不能混报。"""
    from src.integrations.wechat_pc import env_check as E

    def _iso(engine, console, monkeypatch):
        monkeypatch.setattr(E, "current_session_id", lambda: engine)
        monkeypatch.setattr(E, "console_session_id", lambda: console)
        return E.session_isolation()

    mp = pytest.MonkeyPatch()
    try:
        assert _iso(0, 1, mp)["isolated"] is True, "引擎在 session 0、桌面在 1 = 看不见微信"
        assert _iso(1, 1, mp)["isolated"] is False
        assert _iso(0, -1, mp)["isolated"] is False, "无人登录不算隔离"
        assert _iso(-1, 1, mp)["isolated"] is False, "取不到自己的会话时不妄断"
        assert _iso(1, 2, mp) == {"engine_session": 1, "console_session": 2, "isolated": True}
    finally:
        mp.undo()


def test_cross_session_blindness_is_not_reported_as_wechat_not_running(monkeypatch):
    """窗口枚举为空时，跨会话必须点名 engine_not_interactive。

    2026-09-20：session 0 的引擎枚举恒 0 窗，环境检测照旧答「running=False」，
    人对着屏上开着的微信查了大半天微信本身。空枚举在跨会话下不构成「微信没开」的证据。
    """
    from src.integrations.wechat_pc import env_check as E

    monkeypatch.setattr(E.os, "name", "nt")
    monkeypatch.setitem(__import__("sys").modules, "src.integrations.wechat_pc.win32_windows",
                        type("M", (), {"find_wechat_windows": staticmethod(lambda **kw: [])}))

    monkeypatch.setattr(E, "session_isolation",
                        lambda: {"engine_session": 0, "console_session": 1, "isolated": True})
    assert E.check_environment()["reason"] == "engine_not_interactive"

    # 同会话下空枚举才是「微信真没开」——那时不该甩锅给会话
    monkeypatch.setattr(E, "session_isolation",
                        lambda: {"engine_session": 1, "console_session": 1, "isolated": False})
    assert E.check_environment()["reason"] == ""


def test_driver_is_not_ready_across_sessions(monkeypatch):
    """跨会话起驱动只会得到一个瞎子；start/restart 该在门口 409，而不是拉起来再 blind。"""
    from src.integrations.wechat_pc import env_check as E

    monkeypatch.setattr(E.os, "name", "nt")
    monkeypatch.setattr(E, "session_isolation",
                        lambda: {"engine_session": 0, "console_session": 1, "isolated": True})
    d = E.driver_ready()
    assert d["ok"] is False and d["reason"] == "engine_not_interactive"
    assert d["uiautomation"] is True, "包是装了的——别让人去查 uiautomation"

    monkeypatch.setattr(E, "session_isolation",
                        lambda: {"engine_session": 1, "console_session": 1, "isolated": False})
    assert E.driver_ready()["ok"] is True
