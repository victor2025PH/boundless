# -*- coding: utf-8 -*-
"""渠道出站策略层（实施96 P0-3）：声明表 + 静态规则 + 三个收口点的接线。

红线：未登记平台必须逐字节旧行为（无限制）；与实施97 ``kf_window_guard`` 的微信客服
数值同源；抖音的硬规则（1000 字 / 禁链 / 24h·6 条 / 2 气泡 / enforce）钉死。
"""
from __future__ import annotations

import pytest

from src.inbox import channel_policy as cp


# ── 声明表 ──────────────────────────────────────────────────────────────────────

def test_unknown_platform_is_unlimited():
    pol = cp.policy_for("telegram")
    assert pol is cp.UNLIMITED
    long_text = "x" * 20000 + " https://example.com/a"
    assert cp.text_block_reason("telegram", long_text) == ""
    assert cp.media_block_reason("telegram", "audio") == ""
    assert cp.max_bubbles("telegram") is None
    assert cp.cap_max_parts("telegram", 3) == 3
    assert cp.risk_policy_mode("telegram") == ""
    assert cp.window_rule("telegram") is None
    assert cp.is_quota_platform("telegram") is False
    assert cp.snapshot("telegram") == {"platform": "telegram", "unlimited": True}


def test_douyin_hard_rules():
    pol = cp.policy_for("douyin")
    assert pol.max_text_len == 1000 and pol.links == cp.LINKS_DENY
    assert cp.window_rule("douyin") == (24 * 3600.0, 6, 1)
    assert pol.enter_scene_window_sec == 30.0 and pol.enter_scene_cap == 3
    assert cp.max_bubbles("douyin") == 2 and cp.cap_max_parts("douyin", 3) == 2
    assert cp.cap_max_parts("douyin", 1) == 1
    assert cp.risk_policy_mode("douyin") == "enforce"
    assert cp.is_quota_platform("douyin") is True
    assert pol.buttons is False
    # 别名也命中同一策略
    assert cp.policy_for("aweme") == pol


def test_douyin_text_length_and_links_blocked():
    assert cp.text_block_reason("douyin", "好的，稍等") == ""
    r = cp.text_block_reason("douyin", "字" * 1001)
    assert r.startswith(cp.REASON_TEXT_TOO_LONG + ":1001/1000")
    assert cp.text_block_reason("douyin", "详情看 https://bd2026.cc/order") == cp.REASON_LINK_DENIED
    assert cp.text_block_reason("douyin", "官网 bd2026.cc/order 可下单") == cp.REASON_LINK_DENIED
    assert cp.text_block_reason("douyin", "搜 www.baidu.com") == cp.REASON_LINK_DENIED


def test_link_detector_has_no_obvious_false_positives():
    for s in ("价格 3.5 万", "e.g. 明天", "你好。看看这个", "版本 1.0.76", "邮件稍后发", "OK."):
        assert cp.has_link(s) is False, s
    for s in ("http://a.b", "https://x.com/1", "www.x.com", "taobao.com", "shop.douyin.com/x"):
        assert cp.has_link(s) is True, s


def test_douyin_media_whitelist():
    assert cp.media_block_reason("douyin", "image") == ""
    assert cp.media_block_reason("douyin", "image/png") == ""
    # 视频只能按 item_id 分享账号自己发布的作品，上传的视频文件平台不收
    assert cp.media_block_reason("douyin", "video") == f"{cp.REASON_MEDIA_DENIED}:video"
    assert cp.media_block_reason("douyin", "voice") == f"{cp.REASON_MEDIA_DENIED}:voice"
    assert cp.media_block_reason("douyin", "sticker") == f"{cp.REASON_MEDIA_DENIED}:sticker"
    assert cp.media_block_reason("douyin", "") == f"{cp.REASON_MEDIA_DENIED}:unknown"


def test_tiktok_rules():
    pol = cp.policy_for("tiktok")
    assert pol.max_text_len == 6000 and pol.links == cp.LINKS_FIRST_MESSAGE_DENY
    assert cp.window_rule("tiktok") == (48 * 3600.0, 10, 1)
    # first_message_deny 需要轮次状态，本无状态层放行（由窗口执行器接管）
    assert cp.text_block_reason("tiktok", "see https://x.com") == ""
    assert cp.media_block_reason("tiktok", "video") == f"{cp.REASON_MEDIA_DENIED}:video"


def test_tiktok_window_ui_fields_follow_dy_executor():
    """TK → DY 交接项收口（TikTok 线续做 D，2026-09-10）：回复窗 UI 字段名以 window_guard 执行器为准，
    TK-1 规划的 source.reply_window_deadline / window_sent_count 不进 source、不进快照。"""
    from src.inbox import window_guard as wg
    st = wg.WindowState(platform="tiktok", window_sec=48 * 3600.0, cap=10, reserve=1,
                        last_inbound_ts=1_800_000_000.0, sent_since_inbound=3, now=1_800_000_000.0 + 3600)
    d = st.as_dict()
    assert set(cp.WINDOW_UI_FIELDS) <= set(d), d.keys()
    assert d[cp.WINDOW_FIELD_DEADLINE] == 1_800_000_000.0 + 48 * 3600 and d[cp.WINDOW_FIELD_SENT] == 3
    assert d[cp.WINDOW_FIELD_REMAINING] == 7 and d[cp.WINDOW_FIELD_REMAINING_SEC] == 47 * 3600.0
    assert not (set(cp.LEGACY_WINDOW_FIELDS) & set(d)), "旧名不得出现在执行器快照里"
    # 数值仍同源于声明层
    assert (d["window_sec"], d["cap"], d["reserve_for_manual"]) == cp.window_rule("tiktok")
    # 代码里没有任何地方再产出旧名（tiktok 两个传输模块 + 执行器）
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "src"
    for rel in ("integrations/tiktok_official.py", "integrations/tiktok_shop_cs.py", "inbox/window_guard.py"):
        txt = (root / rel).read_text(encoding="utf-8")
        for legacy in cp.LEGACY_WINDOW_FIELDS:
            assert f'"{legacy}"' not in txt, f"{rel} 仍在产出旧字段名 {legacy}"


def test_legacy_window_field_names_normalize_to_dy_names():
    n = cp.normalize_window_fields
    assert n({"reply_window_deadline": 1.5, "window_sent_count": 2, "window_remaining": 8, "cap": 10}) == {
        "deadline_ts": 1.5, "sent": 2, "remaining": 8, "cap": 10}
    # 新旧同在以新名为准（两种键序都成立）
    assert n({"window_sent_count": 9, "sent": 2}) == {"sent": 2}
    assert n({"sent": 2, "window_sent_count": 9}) == {"sent": 2}
    # 已是新名 → 原样；非 dict → {}
    assert n({"deadline_ts": 3.0, "sent": 1}) == {"deadline_ts": 3.0, "sent": 1}
    assert n(None) == {} and n("x") == {}
    assert set(cp.LEGACY_WINDOW_FIELDS.values()) <= set(cp.WINDOW_UI_FIELDS)


def test_mode_specific_key_only_hits_that_mode():
    # WhatsApp Cloud 只声明了 24h 窗、未声明条数 → 不算「窗口+配额」平台（不影响拆条）
    assert cp.window_rule("whatsapp", "official") is None
    assert cp.is_quota_platform("whatsapp", "official") is False
    assert cp.policy_for("whatsapp", "official").reply_window_sec == 24 * 3600.0
    assert cp.policy_for("whatsapp", "protocol") is cp.UNLIMITED
    assert cp.policy_for("whatsapp") is cp.UNLIMITED


def test_wechat_kf_numbers_mirror_kf_window_guard():
    """声明层与实施97 的状态执行器**同源**：数值一旦分叉这里必红。

    ``kf_window_guard`` 由实施97 线落地；它未进 HEAD 前本例跳过（不把他线的在途文件
    变成本线提交的隐式依赖——stage_hunks 模块头第 2 个坑）。"""
    kf = pytest.importorskip("src.inbox.kf_window_guard")
    assert cp.window_rule("wechat_kf") == (
        kf.DEFAULT_WINDOW_SEC, kf.DEFAULT_QUOTA_PER_TURN, kf.DEFAULT_RESERVE_FOR_MANUAL)
    for p in kf.QUOTA_WINDOW_PLATFORMS:
        assert cp.is_quota_platform(p), f"kf_window_guard 认 {p} 为配额平台，channel_policy 未声明"
    assert cp.max_bubbles("wechat_kf") == 1 and cp.max_bubbles("qqbot") == 1


def test_config_overrides_numbers_only():
    cfg = {"channel_policy": {"douyin": {"max_text_len": 500, "max_bubbles": "bad",
                                          "links": "allow", "risk_policy_mode": "weird"}}}
    pol = cp.policy_for("douyin", config=cfg)
    assert pol.max_text_len == 500
    assert pol.max_bubbles == 2          # 坏值忽略
    assert pol.links == cp.LINKS_ALLOW   # 合法覆写生效
    assert pol.risk_policy_mode == "enforce"  # 非法值忽略
    assert cp.text_block_reason("douyin", "字" * 600, config=cfg).startswith(cp.REASON_TEXT_TOO_LONG)
    assert cp.text_block_reason("douyin", "https://x.com", config=cfg) == ""
    # 覆写不外溢到其它平台
    assert cp.policy_for("tiktok", config=cfg).max_text_len == 6000


def test_registered_platforms_are_in_platform_registry():
    from src.integrations import platform_registry as reg
    for p in cp.registered_platforms():
        assert reg.get(p) is not None, f"channel_policy 声明了未登记平台 {p}"


# ── 收口点接线 ───────────────────────────────────────────────────────────────────

def test_reply_split_respects_policy():
    from src.inbox.reply_split import (
        cap_max_parts_for_platform, should_split_for_delivery, split_reply_parts)
    cfg = {"enabled": True, "orch_only": False, "skip_groups": True}
    # 总闸不因策略层改变（未登记 / 有声明的平台都照旧进入拆条判定）
    assert should_split_for_delivery(cfg=cfg, platform="telegram", chat_key="1", orch_owns=True)
    assert should_split_for_delivery(cfg=cfg, platform="douyin", chat_key="1", orch_owns=True)
    # 条数封顶：抖音 ≤2、qqbot 1（封成 1 后 split_reply_parts 自然不拆）、未登记原值
    assert cap_max_parts_for_platform(3, "douyin") == 2
    assert cap_max_parts_for_platform(3, "telegram") == 3
    assert cap_max_parts_for_platform(5, "qqbot") == 1
    text = "第一句话说完了。\n第二句话也说完了。\n第三句话还在说。"
    assert len(split_reply_parts(text, max_parts=cap_max_parts_for_platform(3, "qqbot"),
                                 max_chars=60, min_tail_chars=0, min_total_chars=0)) == 1
    assert len(split_reply_parts(text, max_parts=cap_max_parts_for_platform(3, "douyin"),
                                 max_chars=60, min_tail_chars=0, min_total_chars=0)) <= 2


def test_autosend_policy_platform_override(monkeypatch):
    from src.inbox import autosend_policy as ap
    monkeypatch.delenv("AITR_AUTOSEND_POLICY_MODE", raising=False)
    monkeypatch.setattr(ap, "current_policy_mode", lambda: ap.POLICY_SHADOW)
    kw = dict(peer_risk="low", reply_risk="high", reply_reasons=["keyword"],
              risk_hits=["转账"], automation_mode="auto_ai")
    base = ap.decide(**kw)                       # 不传 platform：全局 shadow + 影子记录
    # D-O1（O-1 A，2026-09-08）：shadow 下 high 转人审 L1（不再直发），影子记录照有
    assert base.policy_mode == ap.POLICY_SHADOW and base.level == "L1" and base.shadow is not None
    assert base.review_required is True
    same = ap.decide(platform="telegram", **kw)  # 未声明平台：与不传完全一致
    assert (same.level, same.hold_reason, same.policy_mode) == (base.level, base.hold_reason, base.policy_mode)
    dy = ap.decide(platform="douyin", **kw)      # 抖音声明 enforce：真扣
    assert dy.policy_mode == ap.POLICY_ENFORCE and dy.level in ap.HOLD_LEVELS and dy.shadow is None
    explicit = ap.decide(platform="douyin", policy_mode="shadow", **kw)  # 显式 policy_mode 优先
    assert explicit.policy_mode == ap.POLICY_SHADOW


class _W:
    def __init__(self):
        self.sent = []
        self.media = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def healthy(self):
        return True

    def status(self):
        return {}

    async def send(self, chat_key, text):
        self.sent.append((chat_key, text))
        return {"delivered": True, "message_id": "m"}

    async def send_media(self, chat_key, *, media_path, media_type, caption=""):
        self.media.append((chat_key, media_type))
        return {"delivered": True, "message_id": "mm"}


def _managed(orch, platform, account_id, worker):
    from src.integrations import account_orchestrator as ao
    key = ao.account_key(platform, account_id)
    orch._managed[key] = ao._Managed(key=key, platform=platform, account_id=account_id,
                                     mode="web", worker=worker, state="running")


@pytest.fixture()
def _no_store():
    """本文件只测静态规则：把收件箱 store getter 钉成 None → window_guard 无事实可读即放行
    （窗口执行器自身在 test_window_guard 里用真 store 测）。"""
    from src.integrations import protocol_bridge as pb
    pb.register_inbox_store_getter(lambda: None)
    pb.register_inbox_sink(None)
    yield
    pb.register_inbox_store_getter(None)
    pb.register_inbox_sink(None)


@pytest.mark.asyncio
async def test_orchestrator_send_blocked_by_channel_policy(_no_store):
    from src.integrations import account_orchestrator as ao
    from src.integrations import protocol_bridge as pb
    orch = ao.AccountOrchestrator(config={})
    w = _W()
    _managed(orch, "douyin", "dy1", w)
    pb.register_inbox_sink(None)
    res = await orch.send("douyin", "dy1", "douyin:user:u1", "戳这里 https://bd2026.cc/order",
                          origin="manual")
    assert res == {"delivered": False, "blocked": cp.REASON_LINK_DENIED}
    res2 = await orch.send("douyin", "dy1", "douyin:user:u1", "字" * 1001)
    assert res2["delivered"] is False and res2["blocked"].startswith(cp.REASON_TEXT_TOO_LONG)
    assert w.sent == []
    ok = await orch.send("douyin", "dy1", "douyin:user:u1", "好的，稍等我看看", origin="manual")
    assert ok["delivered"] is True and w.sent == [("douyin:user:u1", "好的，稍等我看看")]
    # 未登记平台：长文 + 外链照发（零行为变化）
    t = _W()
    _managed(orch, "telegram", "tg1", t)
    ok2 = await orch.send("telegram", "tg1", "1", "x" * 3000 + " https://a.com", origin="manual")
    assert ok2["delivered"] is True


@pytest.mark.asyncio
async def test_orchestrator_send_media_blocked_by_channel_policy(tmp_path, _no_store):
    from src.integrations import account_orchestrator as ao
    from src.integrations import protocol_bridge as pb
    orch = ao.AccountOrchestrator(config={})
    w = _W()
    _managed(orch, "douyin", "dy1", w)
    pb.register_inbox_sink(None)
    f = tmp_path / "a.ogg"
    f.write_bytes(b"\0" * 10)
    res = await orch.send_media("douyin", "dy1", "douyin:user:u1", media_path=str(f),
                                media_url="", media_type="voice", origin="manual")
    assert res == {"delivered": False, "blocked": f"{cp.REASON_MEDIA_DENIED}:voice"}
    assert w.media == []


# ── TK-3（2026-09-10）：TikTok 个人号 mode 键 + 编排器传 mode ──────────────────────────

def test_tiktok_personal_modes_have_no_official_window():
    """个人号（huoke 真机 personal_rpa / 网页边车 web）不是 Business Messaging API：没有 48h·10 条窗，
    ≤1000 字、首条禁链、无按钮；平台级 ``tiktok`` 键（官方 / Shop）原样不动。"""
    official = cp.policy_for("tiktok")
    assert official.has_window and official.max_text_len == 6000
    for mode in ("personal_rpa", "web"):
        pol = cp.policy_for("tiktok", mode=mode)
        assert pol.mode == mode and pol.platform == "tiktok"
        assert not pol.has_window and cp.window_rule("tiktok", mode=mode) is None
        assert pol.max_text_len == 1000 and pol.links == cp.LINKS_FIRST_MESSAGE_DENY and pol.buttons is False
        assert "非官方" in pol.note and "notice_unofficial" in pol.note
        assert cp.text_block_reason("tiktok", "x" * 1001, mode=mode).startswith(cp.REASON_TEXT_TOO_LONG)
        assert cp.text_block_reason("tiktok", "see https://a.com", mode=mode, first_message=True) == cp.REASON_LINK_DENIED
        assert cp.text_block_reason("tiktok", "see https://a.com", mode=mode, first_message=False) == ""
    assert cp.media_block_reason("tiktok", "image", mode="personal_rpa") == ""
    assert cp.media_block_reason("tiktok", "image", mode="web").startswith(cp.REASON_MEDIA_DENIED)   # 边车阶段 1 不发媒体
    # 官方 / Shop 的 shop:official 未登记 → 回落平台级键（TK-1 行为不变）
    assert cp.policy_for("tiktok", mode="official") is not None and cp.policy_for("tiktok", mode="official").has_window
    # 配置覆写：mode 键优先于平台键，且个人号配窗口不殃及官方号
    cfg = {"channel_policy": {"tiktok": {"max_text_len": 500}, "tiktok:personal_rpa": {"max_text_len": 800}}}
    assert cp.policy_for("tiktok", config=cfg).max_text_len == 500
    assert cp.policy_for("tiktok", mode="personal_rpa", config=cfg).max_text_len == 800
    assert cp.policy_for("tiktok", mode="web", config=cfg).max_text_len == 500          # 无 mode 键 → 回落平台键（旧行为）
    assert cp.policy_for("whatsapp", mode="official", config={"channel_policy": {"whatsapp": {"reply_window_sec": 3600}}}).reply_window_sec == 3600.0


@pytest.mark.asyncio
async def test_orchestrator_passes_managed_mode_to_channel_policy(_no_store):
    """编排器 send/send_media 按账号登记的 mode 取策略：tiktok:web 按个人号规则（1000 字拦、6000 字规则不再套用）；
    whatsapp:official 只声明窗长无条数 → 传 mode 后 WA 零行为变化；未登记 mode → 平台键（旧行为）。"""
    from src.integrations import account_orchestrator as ao
    from src.integrations import protocol_bridge as pb
    orch = ao.AccountOrchestrator(config={})
    pb.register_inbox_sink(None)
    assert orch._managed_mode("tiktok", "nobody") == ""
    w = _W()
    _managed(orch, "tiktok", "tt1", w)                       # _managed 登记 mode="web"
    assert orch._managed_mode("tiktok", "tt1") == "web"
    res = await orch.send("tiktok", "tt1", "tiktok:user:u1", "x" * 1001, origin="manual")
    assert res["delivered"] is False and res["blocked"] == f"{cp.REASON_TEXT_TOO_LONG}:1001/1000"
    ok = await orch.send("tiktok", "tt1", "tiktok:user:u1", "x" * 999, origin="manual")
    assert ok["delivered"] is True and len(w.sent) == 1
    # WhatsApp 官方号：传 mode 前后都放行（窗长声明无配额 → window_rule None）
    wa = _W()
    key = ao.account_key("whatsapp", "wa1")
    orch._managed[key] = ao._Managed(key=key, platform="whatsapp", account_id="wa1", mode="official", worker=wa, state="running")
    assert cp.window_rule("whatsapp", mode="official") is None
    ok2 = await orch.send("whatsapp", "wa1", "1", "hello " * 200 + "https://a.com", origin="auto")
    assert ok2["delivered"] is True and len(wa.sent) == 1
