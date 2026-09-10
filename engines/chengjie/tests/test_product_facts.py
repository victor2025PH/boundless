# -*- coding: utf-8 -*-
"""产品事实卡门禁（2026-08-29 P0）。

存在理由：这次事故的结构性根因是**知识源分裂**——渠道边界清单只活在
`config::ai.system_prompt`（喂对客销售 AI），从没进过 `help_kb`，于是同一套
系统里对外的 AI 张口能答「支持哪些平台」，对内的小智却拒答（实测「支持抖音吗」
32.90 分命中「怎么给客户发语音消息」）。

`product_facts.py` 是补上的第二知识源。它与 system_prompt 是**两处副本**，
副本必然漂移——今天补了抖音，明天销售侧加了个新渠道而这边没跟，用户就会
从两个 AI 那里得到互相矛盾的答案，比只有一个错误答案更糟。故本门禁钉住：
两边的渠道名单必须逐个对得上。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.assistant.product_facts import (  # noqa: E402
    CHANNEL_PLATFORM_KEYS,
    SUPPORTED_CHANNELS,
    UNSUPPORTED_CHANNELS,
    facts_fingerprint,
    product_facts_block,
)

def test_claimed_channels_exist_in_code():
    """事实卡声称支持的渠道，**代码里必须真有实现**。

    这才是「不编造产品功能」的硬锚点。刻意**不**拿对客 system_prompt 当基准：
    ① 它是可白标定制的话术（出厂模板只有 72 字，根本没有渠道段）；
    ② 建卡当天实测它在**漏报**——话术列 5 项，而 ACTION_PLATFORMS 有 6 项
       （Zalo / Instagram 都有边车实现）。拿话术当真相会把漏报固化。
    所以方向是单向的：事实卡 ⊆ 代码能力。反过来（代码有而卡里没写）不算错，
    那只是还没对外介绍。

    「代码能力」= worker 平台（ACTION_PLATFORMS）∪ 官方 API 平台（OFFICIAL_PLATFORMS）
    ——QQ 机器人（2026-09-07）只有官方形态、不在 worker 表里，但出站/入站都是真实现。
    叫法对不上平台键的（「QQ 机器人」→ qqbot）经 CHANNEL_PLATFORM_KEYS 显式登记。
    """
    from src.assistant.actions import ACTION_PLATFORMS
    from src.integrations.official_api_worker import OFFICIAL_PLATFORMS

    known = {p.lower() for p in ACTION_PLATFORMS} | {p.lower() for p in OFFICIAL_PLATFORMS}
    # 网页聊天不是 worker 平台（它是本产品自己的页面），单独放行
    web_aliases = {"官网网页聊天", "web", "网页"}
    for ch in SUPPORTED_CHANNELS:
        if ch in web_aliases:
            continue
        key = CHANNEL_PLATFORM_KEYS.get(ch) or ch.lower().replace("facebook ", "").strip()
        assert key in known, (
            f"事实卡声称支持「{ch}」，但 ACTION_PLATFORMS ∪ OFFICIAL_PLATFORMS 里没有它"
            f"（现有 {sorted(known)}）——助手声称的能力必须在代码里真实存在"
        )
    for ch, key in CHANNEL_PLATFORM_KEYS.items():
        assert ch in SUPPORTED_CHANNELS, f"登记了叫法映射却不在支持清单里：{ch}"
        assert key in known, f"叫法映射指向了不存在的平台键：{ch} → {key}"


def test_unsupported_list_covers_the_common_asks():
    """常被问到的国内平台必须显式列进「不支持」。

    「没提到」和「明确不支持」对用户是两种体验：前者让他继续追问/以为在路上，
    后者他当场就能决策。老板实录问的正是「支持抖音吗」。
    """
    # 「微信」以「个人微信」条目承载（实施97：企业微信的「微信客服」已支持，个人号明确不支持）
    for must in ("微信", "抖音"):
        assert any(must in ch for ch in UNSUPPORTED_CHANNELS), (
            f"{must} 是高频提问，必须显式回答")


def test_facts_block_carries_boundaries_in_both_langs():
    """中英事实卡都必须写清支持什么、不支持什么。"""
    for lang in ("zh", "en"):
        blk = product_facts_block(lang)
        assert len(blk) > 200, f"{lang} 事实卡过短，多半被误删"
        assert "Telegram" in blk and "WhatsApp" in blk
        # 不支持清单是重点：只说支持什么，用户仍会追问「那抖音呢」
        assert "抖音" in blk or "抖音" in "".join(UNSUPPORTED_CHANNELS)
        assert ("不支持" in blk) or ("Not supported" in blk)


def test_docless_prompt_wires_facts_and_keeps_red_lines():
    """零命中链的 prompt 必须同时带：事实卡 + 编造红线 + 两枚哨兵。

    少任何一样都会让「零命中也作答」从功能退化成事故：没有事实卡＝没依据可用；
    没有红线＝LLM 拿通用知识猜产品行为；没有 NO_BASIS＝再也无法诚实拒答；
    没有 GENERAL＝通用回答被当成产品承诺。
    """
    from src.web.routes.assistant_routes import build_docless_prompt

    p = build_docless_prompt("支持抖音吗", "ctx", "", "zh")
    assert "产品事实" in p and "Telegram" in p and "抖音" in p
    assert "绝不编造产品功能" in p
    assert "NO_BASIS" in p and "GENERAL" in p
    pe = build_docless_prompt("does it support douyin", "ctx", "", "en")
    assert "PRODUCT FACTS" in pe and "NEVER invent product features" in pe


def test_general_sentinel_roundtrip():
    """GENERAL 前缀的识别与剥离（漏剥＝回答开头挂个英文单词给用户看）。"""
    from src.web.routes.assistant_routes import _is_general, _strip_general

    for raw in ("GENERAL: 你好", "GENERAL 你好", "general：你好",
                "  GENERAL\n你好"):
        assert _is_general(raw), raw
        assert _strip_general(raw).startswith("你好"), raw
    # 正文里出现 general 不算哨兵（哨兵必须在开头）
    assert not _is_general("这是 general 的用法")
    assert _strip_general("这是 general 的用法") == "这是 general 的用法"


def test_fingerprint_shape():
    fp = facts_fingerprint()
    assert fp["supported"] and fp["unsupported"]
    assert fp["zh_chars"] > 200 and fp["en_chars"] > 200
