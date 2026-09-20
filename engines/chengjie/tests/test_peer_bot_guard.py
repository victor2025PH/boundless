# -*- coding: utf-8 -*-
"""对方机器人守卫门禁（P0 2026-08-03）。

语料全部来自当日实锤：@SpamBot「Please use buttons to communicate with me.」
80 秒 8 轮空转、@ykj123 真人连发 33 条 [语音]（误伤红线）、「AI 智控王」
连续 5 条「不行，给我你的照片」。重点覆盖**不该拦**的边界。
"""
from types import SimpleNamespace

import pytest

from src.inbox import peer_bot_guard as pbg


@pytest.fixture(autouse=True)
def _reset():
    pbg._reset_for_tests()
    yield
    pbg._reset_for_tests()


def _cfg(**over):
    root = {"inbox": {"peer_bot_guard": dict({"enabled": True}, **over)}}
    return root


def _msg(direction, text, ts):
    return {"direction": direction, "text": text, "ts": ts}


class FakeStore:
    def __init__(self, msgs=None, rows=None, modes=None):
        self.msgs = list(msgs or [])
        self.rows = list(rows or [])
        self.modes = dict(modes or {})
        self.set_calls = []
        self.bot_calls = []   # P1: set_peer_bot_verdict 调用记录

    def list_recent_messages(self, cid, *, limit=50, before_ts=None):
        return list(self.msgs)

    def get_automation_mode_if_set(self, cid):
        return self.modes.get(cid)

    def set_automation_mode(self, cid, mode):
        self.modes[cid] = mode
        self.set_calls.append((cid, mode))

    def list_conversations(self, limit=100):
        return list(self.rows)

    def get_conversation(self, cid):
        for r in self.rows:
            if r.get("conversation_id") == cid:
                return dict(r)
        return {}

    def set_peer_bot_verdict(self, cid, *, is_bot=None, score=None, evidence=None):
        self.bot_calls.append(
            {"cid": cid, "is_bot": is_bot, "score": score, "evidence": evidence})
        return True


# ── 纯函数：信号 ────────────────────────────────────────────────────────

def test_username_is_bot():
    assert pbg.username_is_bot("SpamBot")
    assert pbg.username_is_bot("@TGDBsearchbot_bot")
    assert pbg.username_is_bot("tgzkw_bot")
    # 真人 username 不误伤（含当日两个 AI 嫌疑号——它们不带 bot 后缀，
    # 属灰区，靠行为熔断而非 Tier0）
    assert not pbg.username_is_bot("ai_zkw")
    assert not pbg.username_is_bot("niuniu2233")
    assert not pbg.username_is_bot("")
    assert not pbg.username_is_bot(None)
    assert not pbg.username_is_bot("bot")  # 过短，非合法 TG username


def test_platform_bot_reason_tier0():
    assert pbg.platform_bot_reason(is_bot_flag=True) == "tg_is_bot"
    assert pbg.platform_bot_reason(chat_type="bot") == "tg_chat_type_bot"
    assert pbg.platform_bot_reason(username="SpamBot") == "tg_username_bot"
    assert pbg.platform_bot_reason(has_reply_markup=True) == "tg_inline_keyboard"
    assert pbg.platform_bot_reason(username="ykj123", chat_type="private") == ""
    # 非 Telegram 平台没有这些真值信号，绝不误触发
    assert pbg.platform_bot_reason(
        platform="whatsapp", is_bot_flag=True, username="xbot") == ""


def test_media_placeholder_and_normalize():
    assert pbg.is_media_placeholder("[语音]")
    assert pbg.is_media_placeholder(" [图片] ")
    assert not pbg.is_media_placeholder("[图片内容] 一只猫")
    assert not pbg.is_media_placeholder("请用按钮与我沟通")
    assert (pbg.normalize_text("Please use buttons!")
            == pbg.normalize_text("please USE buttons"))


# ── 纯函数：复读 / 秒回 / 预算 ──────────────────────────────────────────

def test_repeat_streak_spambot_case():
    msgs = []
    t = 1000.0
    for i in range(8):
        msgs.append(_msg("out", f"reply {i}", t))
        msgs.append(_msg("in", "Please use buttons to communicate with me.", t + 0.5))
        t += 12
    assert pbg.inbound_repeat_streak(msgs) == 8


def test_repeat_streak_media_placeholder_exempt():
    # 真人连发 33 条语音（@ykj123 实录形态）：绝不能算复读
    msgs = [_msg("in", "[语音]", 1000 + i) for i in range(33)]
    assert pbg.inbound_repeat_streak(msgs) == 0


def test_repeat_streak_normal_chat_no_trip():
    msgs = [
        _msg("in", "在吗", 1),
        _msg("out", "在呀", 2),
        _msg("in", "在吗", 3),        # 两次「在吗」正常
        _msg("out", "在的在的", 4),
        _msg("in", "今天忙不忙", 5),
    ]
    assert pbg.inbound_repeat_streak(msgs) == 1


def test_instant_echo_spambot_vs_human():
    # SpamBot：每轮我方发出后 <1s 收到同一句 → 从第 2 条复读起计数
    msgs = [
        _msg("out", "opener", 100.0),
        _msg("in", "Please use buttons to communicate with me.", 100.3),
        _msg("out", "reply1", 110.0),
        _msg("in", "Please use buttons to communicate with me.", 110.4),
        _msg("out", "reply2", 122.0),
        _msg("in", "Please use buttons to communicate with me.", 122.2),
    ]
    assert pbg.instant_echo_count(msgs, window_sec=3.0) >= 2
    # 真人：隔 30s+ 才回，同样内容也不算秒回
    human = [
        _msg("out", "你好呀", 100.0),
        _msg("in", "好", 150.0),
        _msg("out", "吃了吗", 200.0),
        _msg("in", "好", 260.0),
    ]
    assert pbg.instant_echo_count(human, window_sec=3.0) == 0


def test_daily_out_count_budget():
    # 固定时刻（本地 ~05:33），消息全在「今天」内——避开跑在午夜附近的边界抖动
    now = 1754000000.0
    msgs = [_msg("out", f"m{i}", now - 60 * i) for i in range(10)]
    msgs.append(_msg("out", "yesterday", now - 86400 * 2))
    assert pbg.daily_out_count(msgs, now=now) == 10


# ── evaluate 总判定 ─────────────────────────────────────────────────────

def test_evaluate_disabled_allows_everything():
    cfg = pbg.parse_cfg({"inbox": {"peer_bot_guard": {"enabled": False}}})
    v = pbg.evaluate([], cfg, is_bot_flag=True, username="SpamBot")
    assert not v.blocked


def test_evaluate_tier0_manual_precedence():
    cfg = pbg.parse_cfg(_cfg())
    v = pbg.evaluate([], cfg, is_bot_flag=True, username="SpamBot")
    assert v.blocked and v.reason == "tg_is_bot" and v.downgrade_to == "manual"


def test_evaluate_repeat_downgrades_review():
    cfg = pbg.parse_cfg(_cfg())
    msgs = [_msg("in", "不行，给我你的照片", 1000 + i * 3600) for i in range(5)]
    v = pbg.evaluate(msgs, cfg, username="ai_zkw", chat_type="private")
    assert v.blocked and v.reason == "inbound_repeat" and v.downgrade_to == "review"


def test_evaluate_budget_blocks_without_downgrade():
    now = 1754000000.0   # 固定时刻，见 test_daily_out_count_budget
    cfg = pbg.parse_cfg(_cfg(daily_reply_budget=5))
    msgs = []
    for i in range(6):
        msgs.append(_msg("in", f"话题{i}", now - 600 + i * 60))
        msgs.append(_msg("out", f"回复{i}", now - 590 + i * 60))
    v = pbg.evaluate(msgs, cfg, username="niuniu2233", chat_type="private", now=now)
    assert v.blocked and v.reason == "daily_budget" and v.downgrade_to == ""


def test_evaluate_normal_human_allowed():
    cfg = pbg.parse_cfg(_cfg())
    msgs = [
        _msg("in", "你好", 1),
        _msg("out", "你好呀", 2),
        _msg("in", "今天天气不错", 3600),
    ]
    v = pbg.evaluate(msgs, cfg, username="lc334456", chat_type="private")
    assert not v.blocked


# ── B 线接入口 + 降档幂等 ───────────────────────────────────────────────

def test_auto_draft_skip_bot_and_downgrade_once():
    cid = "telegram:8438080491:178220800"
    store = FakeStore(rows=[{
        "conversation_id": cid, "platform": "telegram",
        "username": "SpamBot", "chat_type": "bot",
        "display_name": "Spam Info Bot",
    }])
    conv = {"conversation_id": cid, "platform": "telegram"}
    r1 = pbg.guard_auto_draft_should_skip(conv=conv, store=store, config=_cfg())
    assert r1 == "tg_chat_type_bot"
    assert store.modes[cid] == "manual"
    n_sets = len(store.set_calls)
    r2 = pbg.guard_auto_draft_should_skip(conv=conv, store=store, config=_cfg())
    assert r2  # 仍然拦
    assert len(store.set_calls) == n_sets  # 但不重复写档位


def test_auto_draft_allows_human():
    cid = "telegram:8244899900:5898110595"
    store = FakeStore(
        rows=[{"conversation_id": cid, "platform": "telegram",
               "username": "lc334456", "chat_type": "private"}],
        msgs=[_msg("in", "没问题", 1000), _msg("out", "哈哈好呀", 1002)])
    conv = {"conversation_id": cid, "platform": "telegram"}
    assert pbg.guard_auto_draft_should_skip(
        conv=conv, store=store, config=_cfg()) == ""
    assert store.set_calls == []


def test_auto_draft_disabled_is_noop():
    cid = "telegram:1:178220800"
    store = FakeStore(rows=[{
        "conversation_id": cid, "platform": "telegram",
        "username": "SpamBot", "chat_type": "bot"}])
    conv = {"conversation_id": cid, "platform": "telegram"}
    assert pbg.guard_auto_draft_should_skip(
        conv=conv, store=store,
        config={"inbox": {"peer_bot_guard": {"enabled": False}}}) == ""


# ── 存量 sweep ─────────────────────────────────────────────────────────

def test_sweep_downgrades_legacy_bots_once():
    rows = [
        {"conversation_id": "telegram:1:178220800", "platform": "telegram",
         "username": "SpamBot", "chat_type": "bot", "display_name": "Spam Info Bot"},
        {"conversation_id": "telegram:1:8506426282", "platform": "telegram",
         "username": "tgzkw_bot", "chat_type": "private", "display_name": "BOUNDLESS"},
        {"conversation_id": "telegram:1:5898110595", "platform": "telegram",
         "username": "lc334456", "chat_type": "private", "display_name": "刘纯"},
        # 坐席已显式 review 的 bot：尊重人工选择不动
        {"conversation_id": "telegram:1:553147242", "platform": "telegram",
         "username": "ChatKeeperBot", "chat_type": "bot"},
    ]
    store = FakeStore(rows=rows, modes={"telegram:1:553147242": "review"})
    cfg = pbg.parse_cfg(_cfg())
    pbg._ensure_sweep(store, cfg)
    assert store.modes.get("telegram:1:178220800") == "manual"
    assert store.modes.get("telegram:1:8506426282") == "manual"
    assert "telegram:1:5898110595" not in store.modes          # 真人不动
    assert store.modes.get("telegram:1:553147242") == "review"  # 人工覆写不动
    # P1 补口：sweep 命中＝Tier0 确定信号 → 判定持久化（徽章/DB 单一真相），
    # 已是 review 档的 ChatKeeperBot 同样补写身份（只是不动档位）
    flagged = {c["cid"] for c in store.bot_calls if c["is_bot"] == 1}
    assert {"telegram:1:178220800", "telegram:1:8506426282",
            "telegram:1:553147242"} <= flagged
    assert "telegram:1:5898110595" not in flagged              # 真人绝不落标
    n = len(store.set_calls)
    pbg._ensure_sweep(store, cfg)   # 每进程只跑一次
    assert len(store.set_calls) == n


def test_sweep_respects_flag():
    store = FakeStore(rows=[{
        "conversation_id": "telegram:1:178220800", "platform": "telegram",
        "username": "SpamBot", "chat_type": "bot"}])
    pbg._ensure_sweep(store, pbg.parse_cfg(_cfg(sweep_legacy=False)))
    assert store.set_calls == []


# ── 主动触达候选过滤 ────────────────────────────────────────────────────

def test_proactive_exclude_row():
    bot_row = {"platform": "telegram", "chat_type": "bot", "username": "SpamBot"}
    human_row = {"platform": "telegram", "chat_type": "private",
                 "username": "niuniu2233"}
    assert pbg.proactive_exclude_row(bot_row, _cfg())
    assert not pbg.proactive_exclude_row(human_row, _cfg())
    # 守卫关闭 = 零行为变化（哪怕是 bot 也不动旧候选逻辑）
    assert not pbg.proactive_exclude_row(
        bot_row, {"inbox": {"peer_bot_guard": {"enabled": False}}})
    assert not pbg.proactive_exclude_row(bot_row, _cfg(proactive_filter=False))


# ── A 线接入口 ─────────────────────────────────────────────────────────

def _tg_message(*, is_bot=False, username="", reply_markup=None,
                chat_type="private"):
    return SimpleNamespace(
        from_user=SimpleNamespace(
            is_bot=is_bot, username=username, first_name="X"),
        reply_markup=reply_markup,
        chat=SimpleNamespace(type=chat_type),
    )


def test_a_line_skips_bot_flag(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(
        "src.integrations.protocol_bridge.get_inbox_store", lambda: store)
    reason = pbg.guard_a_line_should_skip(
        config=_cfg(), account_id="8438080491", chat_id=178220800,
        message=_tg_message(is_bot=True, username="SpamBot"),
        current_text="Please use buttons to communicate with me.")
    assert reason == "tg_is_bot"
    assert store.modes.get("telegram:8438080491:178220800") == "manual"


def test_a_line_inline_keyboard_signal(monkeypatch):
    monkeypatch.setattr(
        "src.integrations.protocol_bridge.get_inbox_store", lambda: None)
    reason = pbg.guard_a_line_should_skip(
        config=_cfg(), account_id="a", chat_id=1,
        message=_tg_message(reply_markup=object()))
    assert reason == "tg_inline_keyboard"


def test_a_line_repeat_with_pending_inbound(monkeypatch):
    """镜像异步未落库时，current_text 补上最后一条 → 第 3 条即熔断。

    P1 起菜单话术会先被启发式（suspected_bot）认出——三个原因码都合法，
    核心不变量＝拦下 + 降 review。
    """
    import time as _t
    now = _t.time()
    store = FakeStore(msgs=[
        _msg("out", "opener", now - 40),
        _msg("in", "请用按钮与我沟通", now - 39),
        _msg("out", "reply1", now - 20),
        _msg("in", "请用按钮与我沟通", now - 19),
        _msg("out", "reply2", now - 5),
    ])
    monkeypatch.setattr(
        "src.integrations.protocol_bridge.get_inbox_store", lambda: store)
    reason = pbg.guard_a_line_should_skip(
        config=_cfg(), account_id="a", chat_id=99,
        message=_tg_message(username="someuser"),
        current_text="请用按钮与我沟通")
    assert reason in ("suspected_bot", "inbound_repeat", "instant_echo")
    assert store.modes.get("telegram:a:99") == "review"


def test_a_line_group_not_touched(monkeypatch):
    monkeypatch.setattr(
        "src.integrations.protocol_bridge.get_inbox_store", lambda: None)
    assert pbg.guard_a_line_should_skip(
        config=_cfg(), account_id="a", chat_id=-100123,
        message=_tg_message(is_bot=True, chat_type="supergroup")) == ""


def test_a_line_human_allowed(monkeypatch):
    store = FakeStore(msgs=[
        _msg("in", "你好", 1), _msg("out", "你好呀", 2)])
    monkeypatch.setattr(
        "src.integrations.protocol_bridge.get_inbox_store", lambda: store)
    assert pbg.guard_a_line_should_skip(
        config=_cfg(), account_id="a", chat_id=5,
        message=_tg_message(username="lc334456"),
        current_text="今天天气不错") == ""


def test_append_pending_inbound_dedup():
    import time as _t
    now = _t.time()
    recent = [_msg("in", "请用按钮与我沟通", now - 3)]
    out = pbg._append_pending_inbound(recent, "请用按钮与我沟通", now=now)
    assert len(out) == 1  # 15s 内同文入站 = 已是本条镜像，不重复计
    out2 = pbg._append_pending_inbound(recent, "另一句话", now=now)
    assert len(out2) == 2


# ── 观测快照 ────────────────────────────────────────────────────────────

def test_stats_snapshot_counts():
    store = FakeStore(rows=[{
        "conversation_id": "telegram:1:178220800", "platform": "telegram",
        "username": "SpamBot", "chat_type": "bot"}])
    conv = {"conversation_id": "telegram:1:178220800", "platform": "telegram"}
    # 关掉 sweep：让降档明确归属于「入站检测」路径（sweep 有专属计数）
    pbg.guard_auto_draft_should_skip(
        conv=conv, store=store, config=_cfg(sweep_legacy=False))
    snap = pbg.stats_snapshot()
    assert snap["detected"].get("tg_chat_type_bot") == 1
    assert snap["suppressed"].get("b_line") == 1
    assert snap["downgrades"] >= 1


# ── P1 启发式疑似评分（词表用 2026-08-03 真实语料播种）────────────────────

_SCRIPTED_3 = [
    "请点击下方按钮选择服务", "回复数字 1 查看套餐", "请用按钮与我沟通",
]


def test_heuristic_scripted_menu_trips():
    """菜单话术单类高频（营销广播/菜单 bot 形态）即达阈。"""
    msgs = [_msg("in", t, 100 + i) for i, t in enumerate(_SCRIPTED_3)]
    score, ev = pbg.heuristic_bot_score(msgs)
    assert score >= 0.6
    assert "scripted" in ev


def test_heuristic_link_spam_plus_name():
    """「超搜」形态：t.me 引流链接刷屏 + 名字含自动化词 → 复合达阈。"""
    msgs = [_msg("in", f"最新资源尽在 https://t.me/chaosou{i}", 100 + i)
            for i in range(5)]
    score, ev = pbg.heuristic_bot_score(msgs, display_name="超搜引流助手")
    assert score >= 0.6
    assert "links" in ev and "name" in ev


def test_heuristic_solicit_loop_ai_zkw():
    """「AI 智控王」形态：高频索取照片/语音 + 名字含「智控」→ 复合达阈。"""
    texts = ["发张照片看看", "发个语音听听", "发张自拍呗",
             "说句话听听", "来张照片", "发个照片好不好"]
    msgs = [_msg("in", t, 100 + i * 60) for i, t in enumerate(texts)]
    score, ev = pbg.heuristic_bot_score(
        msgs, display_name="AI 智控王", username="ai_zkw")
    assert score >= 0.6
    assert "solicit" in ev and "name" in ev


def test_heuristic_negatives_stay_below_threshold():
    """反例红线：真人偶发要照片/媒体占位刷屏/名字弱信号——都不得达阈。"""
    msgs = [
        _msg("in", "[语音]", 1), _msg("in", "[语音]", 2),
        _msg("in", "今天好累啊", 3), _msg("out", "抱抱", 4),
        _msg("in", "发张照片看看嘛", 5), _msg("in", "哈哈好可爱", 6),
    ]
    score, _ = pbg.heuristic_bot_score(msgs, display_name="阿龙")
    assert score < 0.6
    # 名字含「客服」但内容正常：弱信号绝不独立触发
    score2, _ = pbg.heuristic_bot_score(
        [_msg("in", "在吗", 1)], display_name="平台客服小美")
    assert score2 < 0.6
    # 真人分享一两个链接：不构成刷屏
    score3, _ = pbg.heuristic_bot_score(
        [_msg("in", "这个视频好好笑 https://t.me/funny/1", 1),
         _msg("in", "你看了吗", 2)])
    assert score3 < 0.6


def test_evaluate_suspected_bot_review():
    cfg = pbg.parse_cfg(_cfg())
    msgs = [_msg("in", t, 100 + i) for i, t in enumerate(_SCRIPTED_3)]
    v = pbg.evaluate(msgs, cfg, username="someuser", chat_type="private")
    assert v.blocked and v.reason == "suspected_bot"
    assert v.downgrade_to == "review"
    assert v.score >= 0.6 and "scripted" in v.evidence


def test_evaluate_heuristics_flag_off():
    cfg = pbg.parse_cfg(_cfg(heuristics=False))
    msgs = [_msg("in", t, 100 + i) for i, t in enumerate(_SCRIPTED_3)]
    v = pbg.evaluate(msgs, cfg, username="someuser", chat_type="private")
    assert not v.blocked
    assert v.score == 0.0


def test_evaluate_threshold_zero_score_only():
    """suspect_threshold=0 = 只算分不动作（灰度观察档）。"""
    cfg = pbg.parse_cfg(_cfg(suspect_threshold=0))
    msgs = [_msg("in", t, 100 + i) for i, t in enumerate(_SCRIPTED_3)]
    v = pbg.evaluate(msgs, cfg, username="someuser", chat_type="private")
    assert not v.blocked
    assert v.score >= 0.6


# ── P1 覆写语义：身份可被人纠正，行为安全底线不能 ───────────────────────

def test_override_disarms_heuristic_not_tier0_nor_behavior():
    cfg = pbg.parse_cfg(_cfg())
    scripted = [_msg("in", t, 100 + i) for i, t in enumerate(_SCRIPTED_3)]
    # 覆写 -1 → 疑似档只算分不动作
    v = pbg.evaluate(scripted, cfg, username="u", chat_type="private",
                     peer_override=-1)
    assert not v.blocked and v.score >= 0.6
    # Tier0 平台真值不受覆写影响（与确定的 bot 全自动互聊没有正当场景）
    v2 = pbg.evaluate([], cfg, is_bot_flag=True, peer_override=-1)
    assert v2.blocked and v2.reason == "tg_is_bot"
    # 行为刹车（复读）同样不受覆写影响
    rep = [_msg("in", "同一句话", 100 + i * 30) for i in range(3)]
    v3 = pbg.evaluate(rep, cfg, peer_override=-1)
    assert v3.blocked and v3.reason == "inbound_repeat"


def test_row_is_bot_honors_persisted_flag():
    # 持久判定优先于行级信号；跨平台生效
    assert pbg.conversation_row_is_bot(
        {"peer_is_bot": 1, "platform": "whatsapp", "chat_type": "private"})
    # 覆写「真人」压过 Tier0 行级信号（主动触达不再剔除、sweep 不再降档）
    assert not pbg.conversation_row_is_bot(
        {"peer_is_bot": -1, "platform": "telegram", "chat_type": "bot",
         "username": "SpamBot"})
    # 未标注回落旧行为
    assert pbg.conversation_row_is_bot(
        {"peer_is_bot": 0, "platform": "telegram", "chat_type": "bot"})


def test_row_is_bot_knows_botfather_first_contact():
    """知名服务号 ID（P2 2026-08-04）：@BotFather username 不带 bot 后缀、
    chat_type 记 private，行为标记（peer_is_bot）要等它回话才有——
    2026-08-04 实锤 news_share 给它发了霍尔木兹问候。ID 表补首次接触盲区。"""
    row = {"peer_is_bot": 0, "platform": "telegram", "chat_type": "private",
           "chat_key": "93372553", "username": "BotFather",
           "display_name": "BotFather"}
    assert pbg.conversation_row_is_bot(row) is True
    # 运营覆写「确认真人」仍最高优先（语义不变）
    assert pbg.conversation_row_is_bot({**row, "peer_is_bot": -1}) is False
    # 名单只认 telegram；同 ID 其他平台不受影响
    assert pbg.conversation_row_is_bot(
        {**row, "platform": "whatsapp"}) is False


# ── P1 判定持久化（徽章/覆写/统计的地基）────────────────────────────────

def test_b_line_persists_tier0_flag():
    cid = "telegram:1:178220800"
    store = FakeStore(rows=[{
        "conversation_id": cid, "platform": "telegram",
        "username": "SpamBot", "chat_type": "bot",
        "display_name": "Spam Info Bot"}])
    conv = {"conversation_id": cid, "platform": "telegram"}
    pbg.guard_auto_draft_should_skip(
        conv=conv, store=store, config=_cfg(sweep_legacy=False))
    assert store.bot_calls, "Tier0 检出必须持久化 peer_is_bot"
    assert store.bot_calls[-1]["is_bot"] == 1
    assert "tg_chat_type_bot" in str(store.bot_calls[-1]["evidence"])


def test_b_line_persists_suspected_score_not_flag():
    cid = "telegram:1:999"
    store = FakeStore(
        rows=[{"conversation_id": cid, "platform": "telegram",
               "username": "someuser", "chat_type": "private"}],
        msgs=[_msg("in", t, 100 + i) for i, t in enumerate(_SCRIPTED_3)])
    conv = {"conversation_id": cid, "platform": "telegram"}
    r = pbg.guard_auto_draft_should_skip(
        conv=conv, store=store, config=_cfg(sweep_legacy=False))
    assert r == "suspected_bot"
    assert store.bot_calls
    last = store.bot_calls[-1]
    assert last["is_bot"] is None, "疑似≠定论，is_bot 保持不写"
    assert last["score"] >= 0.6
    assert store.modes.get(cid) == "review"


def test_b_line_override_suppresses_suspected():
    cid = "telegram:1:888"
    store = FakeStore(
        rows=[{"conversation_id": cid, "platform": "telegram",
               "username": "someuser", "chat_type": "private",
               "peer_is_bot": -1}],
        msgs=[_msg("in", t, 100 + i) for i, t in enumerate(_SCRIPTED_3)])
    conv = {"conversation_id": cid, "platform": "telegram"}
    assert pbg.guard_auto_draft_should_skip(
        conv=conv, store=store, config=_cfg(sweep_legacy=False)) == ""
    assert store.modes.get(cid) is None  # 不降档
    assert store.bot_calls == []          # 不落库


def test_proactive_exclude_honors_override():
    row = {"platform": "telegram", "chat_type": "bot", "username": "SpamBot",
           "peer_is_bot": -1}
    assert not pbg.proactive_exclude_row(row, _cfg())
    row2 = {"platform": "whatsapp", "chat_type": "private", "peer_is_bot": 1}
    assert pbg.proactive_exclude_row(row2, _cfg())


# ── P1 灰区拟稿感知块 ───────────────────────────────────────────────────

def test_draft_awareness_hint_grey_zone():
    cfg = _cfg()
    row = {"bot_score": 0.45, "bot_evidence": "solicit×4", "peer_is_bot": 0}
    hint = pbg.draft_awareness_hint(row, cfg)
    assert "疑似自动化" in hint and "solicit×4" in hint
    assert "验证码" in hint    # 操作性要求防线必须在提示里
    # 低分 → 不注入
    assert pbg.draft_awareness_hint({"bot_score": 0.1}, cfg) == ""
    # 已判定 bot → 注入（无论分数）
    assert "疑似自动化" in pbg.draft_awareness_hint({"peer_is_bot": 1}, cfg)
    # 运营覆写真人 → 恒不注入（哪怕分数很高）
    assert pbg.draft_awareness_hint(
        {"peer_is_bot": -1, "bot_score": 0.9}, cfg) == ""
    # 守卫关 / heuristics 关 → 不注入；空行安全
    assert pbg.draft_awareness_hint(
        row, {"inbox": {"peer_bot_guard": {"enabled": False}}}) == ""
    assert pbg.draft_awareness_hint(row, _cfg(heuristics=False)) == ""
    assert pbg.draft_awareness_hint(None, cfg) == ""


def test_persona_reply_wires_awareness_hint():
    """静态接线钉：B 线唯一入口必须消费 draft_awareness_hint，且注入先于
    统一引擎调用（否则草稿生成时读不到提示）。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "inbox"
           / "persona_reply.py").read_text(encoding="utf-8")
    assert "draft_awareness_hint" in src, "persona_reply 未接 peer_bot 感知块"
    assert (src.index("draft_awareness_hint")
            < src.index("统一规则引擎")), "感知块注入必须先于统一引擎调用"


# ── P1 触达统计剔除 bot 会话（KPI 数据卫生）────────────────────────────

def test_outreach_stats_exclude_bot_peers(tmp_path):
    """SpamBot 的「秒回」绝不能进回复率/形态反哺/模式门控的分母。"""
    import time as _t

    from src.inbox.models import InboxConversation, InboxMessage
    from src.inbox.store import InboxStore

    st = InboxStore(tmp_path / "inbox.db")
    now = _t.time()
    human, bot, marked = "telegram:a:h1", "telegram:a:178220800", "telegram:a:m1"
    st.upsert_conversation(InboxConversation(
        conversation_id=human, platform="telegram", account_id="a",
        chat_key="h1", display_name="刘纯", chat_type="private"))
    st.upsert_conversation(InboxConversation(
        conversation_id=bot, platform="telegram", account_id="a",
        chat_key="178220800", display_name="Spam Info Bot",
        username="SpamBot", chat_type="bot"))
    st.upsert_conversation(InboxConversation(
        conversation_id=marked, platform="telegram", account_id="a",
        chat_key="m1", display_name="别的平台bot", chat_type="private"))
    st.set_peer_bot_verdict(marked, is_bot=1)   # 持久判定（跨平台口径）
    for cid in (human, bot, marked):
        st.record_outreach(cid, batch_id="proactive_topic:text",
                           note="gentle_checkin", ts=now - 3600)
        st.ingest_batch(
            InboxConversation(conversation_id=cid, platform="telegram",
                              account_id="a", chat_key=cid.split(":")[-1]),
            [InboxMessage(conversation_id=cid, platform_msg_id=f"r-{cid}",
                          direction="in", text="回", ts=now - 3000)])

    # 默认剔除：只有真人进分母
    s = st.outreach_response_stats("proactive_topic:text",
                                   response_window_days=3)
    assert s["sent"] == 1 and s["responded"] == 1
    hist = st.outreach_mode_histogram("proactive_topic:", days=14)
    assert hist.get("gentle_checkin") == 1
    ns = st.outreach_note_response_stats("proactive_topic:", lookback_days=14)
    assert ns["gentle_checkin"]["sent"] == 1

    # 显式关剔除 = 旧口径（全部计入）
    s_all = st.outreach_response_stats(
        "proactive_topic:text", response_window_days=3,
        exclude_bot_peers=False)
    assert s_all["sent"] == 3

    # 运营覆写「确认真人」→ 重新计入
    st.set_peer_bot_verdict(bot, is_bot=-1)
    s2 = st.outreach_response_stats("proactive_topic:text",
                                    response_window_days=3)
    assert s2["sent"] == 2

    # 会话行不存在（历史孤儿触达）→ 保守计入，不误剔
    st.record_outreach("telegram:a:ghost", batch_id="proactive_topic:text",
                       note="gentle_checkin", ts=now - 1800)
    s3 = st.outreach_response_stats("proactive_topic:text",
                                    response_window_days=3)
    assert s3["sent"] == 3
    st.close()


# ── P1.5 覆写误判反馈环（词表/阈值校准的分子）───────────────────────────

def test_evidence_reason_parsing():
    assert pbg.evidence_reason("suspected_bot: scripted×3, name(x)") == "suspected_bot"
    assert pbg.evidence_reason("tg_is_bot: @SpamBot") == "tg_is_bot"
    assert pbg.evidence_reason("operator:admin 手动标记为机器人") == "other"
    assert pbg.evidence_reason("") == "unlabeled"
    assert pbg.evidence_reason(None) == "unlabeled"


def test_record_override_counts():
    pbg.record_override("human", "suspected_bot")
    pbg.record_override("human", "suspected_bot")
    pbg.record_override("bot", "unlabeled")
    pbg.record_override("nope", "x")   # 非法 kind 静默忽略
    snap = pbg.stats_snapshot()
    assert snap["overrides"]["human:suspected_bot"] == 2
    assert snap["overrides"]["bot:unlabeled"] == 1
    assert not any(k.startswith("nope:") for k in snap["overrides"])


# ── P1.5 SQL × Python 判定一致性门禁（防双实现分叉）─────────────────────

def test_sql_and_python_bot_predicates_agree(tmp_path):
    """「什么算 bot 会话」有两份实现：Python ``conversation_row_is_bot``
    （主动触达过滤/sweep）与 SQL ``_NOT_BOT_PEER_SQL``（触达统计剔除）。
    矩阵全等钉死——改词表/口径只改一边就在这红。"""
    from src.inbox.models import InboxConversation
    from src.inbox.store import InboxStore

    st = InboxStore(tmp_path / "inbox.db")
    matrix = []
    i = 0
    for flag in (-1, 0, 1):
        for chat_type in ("bot", "private"):
            for username in ("SpamBot", "lc334456", ""):
                for platform in ("telegram", "whatsapp"):
                    i += 1
                    cid = f"{platform}:a:m{i}"
                    st.upsert_conversation(InboxConversation(
                        conversation_id=cid, platform=platform, account_id="a",
                        chat_key=f"m{i}", username=username,
                        chat_type=chat_type))
                    if flag:
                        st.set_peer_bot_verdict(cid, is_bot=flag)
                    st.record_outreach(cid, batch_id="pred:x", note="t",
                                       ts=1000.0)
                    matrix.append(cid)
    sql = ("SELECT o.conversation_id FROM outreach_log o "
           "WHERE o.batch_id LIKE ? AND o.status='sent'"
           + InboxStore._NOT_BOT_PEER_SQL)
    with st._lock:
        included = {r[0] for r in st._conn.execute(sql, ("pred:%",)).fetchall()}
    for cid in matrix:
        row = st.get_conversation(cid)
        py_bot = pbg.conversation_row_is_bot(row)
        sql_excluded = cid not in included
        assert py_bot == sql_excluded, (
            f"{cid}: python={py_bot} sql_excluded={sql_excluded} row={row}")
    st.close()


# ── P1 一键覆写路由 ─────────────────────────────────────────────────────

def test_bot_flag_routes_roundtrip(tmp_path):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from src.inbox.models import InboxConversation
    from src.inbox.store import InboxStore
    from src.web.routes.unified_inbox_stored_read_routes import (
        register_stored_read_routes,
    )

    app = FastAPI()

    def api_auth(request: Request):
        return True

    register_stored_read_routes(app, api_auth=api_auth)
    st = InboxStore(tmp_path / "inbox.db")
    app.state.inbox_store = st
    c = TestClient(app)
    cid = "telegram:a:77"
    st.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="telegram", account_id="a",
        chat_key="77", display_name="X", chat_type="private"))

    r = c.get("/api/unified-inbox/bot-flag"
              "?platform=telegram&account_id=a&chat_key=77")
    assert r.status_code == 200 and r.json()["peer_is_bot"] == 0

    # 标记 bot → 落库 + 会话降 manual（停自动链，语义与 sweep 一致）
    r = c.post("/api/unified-inbox/bot-flag", json={
        "platform": "telegram", "account_id": "a", "chat_key": "77",
        "value": "bot"})
    assert r.status_code == 200 and r.json()["peer_is_bot"] == 1
    assert st.get_automation_mode_if_set(cid) == "manual"
    assert "operator" in st.get_conversation(cid)["bot_evidence"]

    # 先造一个「启发式疑似」现场，再覆写真人 → 反馈环必须计到 human:suspected_bot
    st.set_peer_bot_verdict(cid, score=0.7,
                            evidence="suspected_bot: scripted×3")
    r = c.post("/api/unified-inbox/bot-flag", json={
        "platform": "telegram", "account_id": "a", "chat_key": "77",
        "value": "human"})
    assert r.status_code == 200 and r.json()["peer_is_bot"] == -1
    assert st.get_conversation(cid)["bot_score"] == 0.0
    assert st.get_automation_mode_if_set(cid) == "manual"
    snap = pbg.stats_snapshot()
    assert snap["overrides"].get("human:suspected_bot") == 1, (
        "覆写反馈环没记到误判纠正——校准环断了")

    # clear → 回未标注，证据清空
    r = c.post("/api/unified-inbox/bot-flag", json={
        "platform": "telegram", "account_id": "a", "chat_key": "77",
        "value": "clear"})
    assert r.status_code == 200 and r.json()["peer_is_bot"] == 0
    assert st.get_conversation(cid)["bot_evidence"] == ""

    # 非法值 → 400；缺 chat_key → 400
    assert c.post("/api/unified-inbox/bot-flag", json={
        "platform": "telegram", "account_id": "a", "chat_key": "77",
        "value": "nope"}).status_code == 400
    assert c.post("/api/unified-inbox/bot-flag", json={
        "platform": "telegram", "value": "bot"}).status_code == 400
    st.close()


def test_store_peer_bot_columns_roundtrip(tmp_path):
    """真 InboxStore：迁移列存在 + set_peer_bot_verdict 部分更新语义。"""
    from src.inbox.models import InboxConversation
    from src.inbox.store import InboxStore
    st = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:a:77"
    st.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="telegram", account_id="a",
        chat_key="77", display_name="X", chat_type="private"))
    row = st.get_conversation(cid)
    assert row["peer_is_bot"] == 0 and row["bot_score"] == 0
    assert st.set_peer_bot_verdict(cid, is_bot=1, evidence="tg_is_bot: @x")
    assert st.set_peer_bot_verdict(cid, score=0.73)   # 部分更新不动 is_bot
    row = st.get_conversation(cid)
    assert row["peer_is_bot"] == 1
    assert abs(row["bot_score"] - 0.73) < 1e-6
    assert row["bot_evidence"].startswith("tg_is_bot")
    # 覆写「真人」+ 越界值防御
    assert st.set_peer_bot_verdict(cid, is_bot=-1, score=99, evidence="op:clear")
    row = st.get_conversation(cid)
    assert row["peer_is_bot"] == -1 and row["bot_score"] == 1.0
    # 不存在的会话 / 空更新
    assert not st.set_peer_bot_verdict("nope", is_bot=1)
    assert not st.set_peer_bot_verdict(cid)
    st.close()
