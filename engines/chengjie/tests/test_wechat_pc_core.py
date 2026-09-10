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
    assert P.action_allowed("something_new") is False, "不在白名单即拒"


def test_kind_gating_by_tier():
    semi = P.resolve_policy({"platform_login": {"wechat_pc": {"tier": "semi"}}})
    auto = P.resolve_policy({"platform_login": {"wechat_pc": {"tier": "auto_reply", "risk_ack": True}}})
    assert P.kind_allowed(semi, "manual") and P.kind_allowed(semi, "approved")
    assert not P.kind_allowed(semi, "text"), "半自动不发未经人确认的自动稿"
    assert P.kind_allowed(auto, "text") and P.kind_allowed(auto, "manual")
    assert not P.kind_allowed(P.resolve_policy({}), "manual"), "副驾永不发"


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


# ── service ─────────────────────────────────────────────────────────────────

class FakeBridge(BridgeClient):
    def __init__(self):
        super().__init__("http://x", "t", http=self._http)
        self.ingested: List[Dict[str, Any]] = []
        self.queue: List[Dict[str, Any]] = []
        self.acks: List[Tuple[int, bool, str]] = []

    def _http(self, method, url, body):
        if url.endswith("/api/desktop/ingest"):
            self.ingested.append(body)
            return 200, {"ok": True, "conversation_id": "c"}
        if "/api/desktop/outbound/ack" in url:
            self.acks.append((body["id"], body["ok"], body["error"]))
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
    assert svc.frozen() and svc.stats.guard_freezes == 1 and notes[-1][0] == "guard_freeze"
    ack4 = next(a for a in br.acks if a[0] == 4)
    assert ack4 == (4, False, "guard:frozen"), "冻结后已认领的剩余命令立刻回执失败进人审"
    assert fb.messages["张三"][-1].text == "在的", "冻结期间没有再往微信里发任何字"
    clock["t"] += 700
    assert not svc.frozen()


def test_screen_disposition_freezes_and_notifies():
    svc, fb, br, notes, clock = _svc("auto_reply")
    fb.logged_in = False
    fb.window_class = "mmui::LoginWindow"
    fb.dialogs = ["进入WeChat", "切换账号"]
    s = svc.tick()
    assert s["readable"] is False and svc.stats.offline and svc.frozen()
    assert notes and notes[-1][0] == R.LOGGED_OUT
    # 登录恢复 → 解除 offline，通知恢复
    fb.logged_in = True
    fb.window_class = "mmui::MainWindow"
    fb.dialogs = []
    svc.tick()
    assert not svc.stats.offline and notes[-1][0] == "recovered"
    # 限频弹窗：仍可读屏，但冻结发送 2h
    fb.dialogs = ["操作过于频繁，请稍后再试"]
    clock["t"] += 5000
    s2 = svc.tick()
    assert s2["readable"] is True and svc.stats.frozen_until >= clock["t"] + 7000


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
