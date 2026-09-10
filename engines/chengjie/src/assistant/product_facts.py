# -*- coding: utf-8 -*-
"""产品事实卡：小智答「产品能不能做 X」时的第二知识源（2026-08-29）。

**为什么需要它**（老板 8/29 实录「回复的还是没有区别」的结构性根因）：
问「支持抖音吗」时，答案在系统里是**存在**的——`config.yaml::ai.system_prompt`
的渠道边界段写得清清楚楚（对接 TG/WhatsApp/LINE/Messenger/网页；微信抖音等不
支持）。但那段话只喂给**对客销售 AI**，从未进 `help_kb`。于是同一套系统里，
对外的 AI 张口能答，对内的小智一问三不知：实测检索「支持抖音吗」32.90 分命中
「怎么给客户发语音消息」、「支持微信吗」34.58 分命中「信息日志(INFO)」——
分数都不够，走 NO_BASIS 诚实拒答，看起来像模型没用，实为知识源分裂。

**与 help_kb 的分工**：
  · help_kb  = 「怎么做 X」的操作步骤（BM25 检索，按问题取条目）
  · 本模块   = 「产品是什么 / 能不能做 X」的能力边界（**常驻注入**，不检索）
能力边界问题的问法发散度极高（支持抖音吗 / 能接微信不 / 有安卓版吗 / 能同时
几个号），靠关键词检索天然抓不全；而它总共就这么几条事实，直接常驻进 prompt
比逐条建语料更可靠也更省。

**红线**：这里只许写**已经过审的事实**。渠道清单与产品矩阵逐字取自
system_prompt 的对客口径（那是对外承诺过的），客户端与容量口径取自代码实况。
禁止在此处写「我以为有」的功能——助手编造产品能力是一票否决项（实施58）。
两边漂移由 `tests/test_product_facts.py` 钉住（渠道名单必须与 system_prompt 一致）。
"""
from __future__ import annotations

from typing import Any, Dict

# 对接中的聊天渠道。**事实源是代码实况**（`actions.ACTION_PLATFORMS` ＋
# `docs/平台能力矩阵.md`，后者由 scripts/platform_matrix 从 worker 代码生成），
# 不是销售话术。
#
# ⚠ 2026-08-29 建卡时发现的真实缺口：对客 system_prompt 的渠道段只列了
# Telegram/WhatsApp/LINE/Messenger/网页 **五项**，而代码里 ACTION_PLATFORMS 是
# 六项——**Zalo 与 Instagram 都有边车实现**（services/zalo-personal、
# services/instagram-web），能力矩阵里 Zalo（web）也在列，坐席工作台底部的平台
# 条同样是六个图标。也就是说话术在**漏报**两个已交付的平台。事实卡照抄话术就会
# 把这个漏报固化成「小智也说不支持」，所以这里以代码为准；话术那边属产品/销售
# 侧决策，已在交付说明里点名。
SUPPORTED_CHANNELS = ("Telegram", "WhatsApp", "LINE", "Facebook Messenger",
                      "Instagram", "Zalo", "QQ 机器人", "微信客服（企业微信）", "官网网页聊天")
# **预览中**的渠道（QQ 线 A 段，2026-09-10）：代码 / 边车 / 工作台全链在、CI 全绿，但底层驱动还是演示
# 替身——在真平台上**尚不能真收发**。与「支持」分列，小智被问「QQ 个人号能用吗」必须答「预览，尚不能
# 真收发」而不是「能」。事实源 = platform_registry 的 driver_state=mock（门禁 test_platform_registry
# 钉两边一致；驱动接真后从注册表翻 real，这里随之清空）。
PREVIEW_CHANNELS = ("QQ 个人号（协议登录）",)
# 事实卡里的渠道叫法 → 代码平台键（门禁 test_product_facts 据此核对「声称的能力在代码里
# 真实存在」；能按小写/去 "Facebook " 前缀直接对上的不必登记）。预览渠道同样登记——它在代码里
# 也必须真有实现（只是驱动是替身）。
CHANNEL_PLATFORM_KEYS = {
    "QQ 机器人": "qqbot",
    "QQ 个人号（协议登录）": "qq",
    "微信客服（企业微信）": "wechat_kf",
}
# 明确**不支持**的平台：说不支持比含糊其辞有用得多，也防销售侧谎称支持。
# 「QQ 个人号（协议登录）」＝用自己的 QQ 号登录那条路（智聊内置 QQ 连接边车 services/qq-personal，
# 亦可指向自装的 Milky 协议端；非官方接入、准入区），与「QQ 机器人」（QQ 开放平台官方 API）是两个渠道。
# 「个人微信」：不作为**自动聊天渠道**接入（没有官方接口，封号 + 法律风险），所以留在这张表里；
# 但实施97 线 B 提供了「PC 副驾」——读取电脑上已登录的微信、AI 给建议、半自动按批准代发、全自动
# 需风险确认，属读屏辅助而非渠道接入。边界说明见下面 _CHANNEL_CAVEATS_*，小智答「个人微信」时两句话都要说。
UNSUPPORTED_CHANNELS = ("个人微信", "淘宝", "抖音", "小红书")
# 已支持渠道的边界说明（常驻注入；用户问「QQ 机器人能主动发消息吗」这类必须答得准）
_CHANNEL_CAVEATS_ZH = (
    "QQ 机器人（QQ 开放平台官方 API）只能被动回复：单聊每条来话 60 分钟内最多回 4 条、"
    "群里默认只收 @机器人 的消息、不支持主动消息；发图/语音属下一批次。"
    "QQ 个人号（协议登录）目前是**预览**：智聊内置的 QQ 连接边车已随安装包装好，但底层真驱动尚未接入"
    "（演示态），扫码与收发都是演示数据、尚不能真收发；真驱动通过去风险验证后才开放。它属非官方接入、"
    "有风控风险，届时建议小号 + 固定 IP；能力与 Telegram 协议号相当，但无「正在输入」。"
    "微信客服（企业微信官方 API）需要客户自己的企业微信（自建应用 CorpID/Secret），"
    "微信用户扫客服二维码即可咨询、不用加好友；客户最后一条消息后 48 小时内最多回 5 条、"
    "不能主动发起会话、无正在输入/已读回执，AI 身份披露在该渠道恒开。"
    "个人微信不作为自动聊天渠道接入（无官方接口，有封号与法律风险），但提供「PC 副驾」：读取电脑上"
    "已登录的微信 4.x 窗口，把消息同步进工作台、AI 给建议；半自动档只发坐席批准的回复，全自动档需"
    "在引导页确认风险；仅 Windows，接入流程在工作台「账号管理 → 个人微信 · PC 副驾」。"
)
_CHANNEL_CAVEATS_EN = (
    "QQ Bot (QQ Open Platform official API) is passive-only: at most 4 replies within "
    "60 minutes per inbound private message, groups only deliver @-mentions by default, "
    "no proactive messages; image/voice sending lands in the next batch. "
    "QQ personal account (protocol login) is currently a PREVIEW: ChatX's built-in QQ connector "
    "sidecar ships with the installer, but the real low-level driver is not wired yet (demo mode) - "
    "QR login and messages are demo data and it cannot actually send or receive; it opens once the "
    "driver passes risk validation. It is unofficial with risk-control exposure (use a secondary "
    "account plus a fixed IP) and matches Telegram protocol accounts except for typing indicators. "
    "WeChat Customer Service (WeCom official API) needs the customer's own WeCom "
    "(self-built app CorpID/Secret); WeChat users scan the service QR to chat without "
    "adding a friend; at most 5 replies within 48 hours after the customer's last message, "
    "no proactive conversations, no typing/read receipts, and AI disclosure is always on. "
    "Personal WeChat is not an auto-chat channel (no official API; ban and legal risk), but a "
    "\"PC copilot\" is available: it reads the signed-in WeChat 4.x window on the PC, mirrors messages "
    "into the workspace and drafts suggestions; semi-auto sends only agent-approved replies, full-auto "
    "requires a risk acknowledgement in the guide; Windows only, set up under Accounts → Personal WeChat · PC copilot."
)

_FACTS_ZH = f"""【本产品是什么】
「智聊 ChatX」＝聚合多平台的 AI 聊天客服/获客系统：把多个平台的账号聚到一个
坐席工作台，AI 按人设自动接待、拟稿或全自动回复，人只处理需要人的会话。
它属于无界科技（BOUNDLESS）的产品矩阵：通译 LingoX（聊天翻译）、通传 VoxX
（语音同传）、智拓 ReachX（自动获客）、智聊 ChatX（本产品）、幻声 VoiceX
（声音克隆）、幻颜 FaceX（图片视频换脸）、幻影 LiveX（直播换脸换声）。

【支持的聊天渠道】{"、".join(SUPPORTED_CHANNELS)}。
【预览中（尚不能真收发）】{"、".join(PREVIEW_CHANNELS)}——被问「能用吗」答「预览，尚不能真收发」，不说「能」。
【渠道边界】{_CHANNEL_CAVEATS_ZH}
【暂不支持】{"、".join(UNSUPPORTED_CHANNELS)}等国内平台——如实说明即可，
不要为了让答案好听而谎称支持；用户如果需要，可以让他把需求提给产品团队评估。

【客户端形态】① 浏览器打开的后台与坐席工作台；② Windows 桌面客户端
（ChatX Desktop）；③ 手机扫码后的网页控制端（用于远程指挥，不是独立 App）。
目前没有上架的安卓/iOS 原生 App。

【能力边界的回答方式】被问到「能不能做某件事」而上面没写、帮助文档也没有的，
不要猜——按不知道处理。"""

_FACTS_EN = f"""[What this product is]
ChatX is a multi-platform AI customer-service / outreach system: it aggregates
accounts from several chat platforms into one agent workspace where AI replies
automatically or drafts for a human, so people only handle what needs people.
It is part of the BOUNDLESS suite (LingoX translation, VoxX interpreting,
ReachX outreach, ChatX this product, VoiceX voice cloning, FaceX face swap,
LiveX live avatar).

[Supported chat channels] {", ".join(SUPPORTED_CHANNELS)}.
[Preview - cannot really send/receive yet] {", ".join(PREVIEW_CHANNELS)} - when asked "does it work", answer "preview, not yet able to send/receive", never "yes".
[Channel limits] {_CHANNEL_CAVEATS_EN}
[Not supported] {", ".join(UNSUPPORTED_CHANNELS)} and other China-domestic
platforms - say so honestly, never claim support to make an answer nicer.

[Clients] Browser admin + agent workspace; a Windows desktop client
(ChatX Desktop); a phone web console reached by QR (remote control, not a
standalone app). There is no published Android/iOS native app.

[On capability questions] If asked whether something is possible and it is
neither above nor in the help docs, do not guess - treat it as unknown."""


def product_facts_block(lang: str = "zh") -> str:
    """常驻注入 prompt 的产品事实卡（纯文本，无外部依赖，绝不抛）。"""
    return _FACTS_EN if str(lang or "").lower().startswith("en") else _FACTS_ZH


def facts_fingerprint() -> Dict[str, Any]:
    """给门禁与 ops 用的结构化快照（比对 system_prompt 是否漂移）。"""
    return {
        "supported": list(SUPPORTED_CHANNELS),
        "preview": list(PREVIEW_CHANNELS),
        "unsupported": list(UNSUPPORTED_CHANNELS),
        "zh_chars": len(_FACTS_ZH),
        "en_chars": len(_FACTS_EN),
    }


__all__ = ["product_facts_block", "facts_fingerprint",
           "SUPPORTED_CHANNELS", "UNSUPPORTED_CHANNELS", "CHANNEL_PLATFORM_KEYS"]
