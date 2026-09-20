"""设备 RPA 屏幕级风控识别（P6，补 P0 明确留下的空白）。

背景：Telegram 侧发送异常经 ``ban_signal.handle_send_exception`` 进 24h 滚动风控计数
（flood/error → account_health 扣分）。但 WA/LINE **真机 RPA** 的发送失败不是抛
pyrogram 异常，而是返回 ``{ok: False, error: ...}`` dict——平台风控（验证墙 / 临时
限制 / 封号页）只体现在**屏幕 UI 文案**上，从不进反馈环。account_health 因此对设备号
的风控完全失明（flood_waits_24h 恒 0）。

本模块＝**纯函数屏幕分类器**：从一次 uiautomator dump 的文字里保守识别风控屏，
交 runner 侧接线记 ``risk_events``（verify/limit → flood 家族预警；ban → 终态不计，
与 ``ban_signal.risk_kind_for`` 的「ban→None」同决策，另走告警）。

设计（与 opt-out 词表 / 元数据回填同哲学）：**宁可漏报不误报**。词表只收几乎只在
风控屏出现的高置信短语；日常聊天文案绝不命中。零设备依赖、零 IO，可单测。
"""

from __future__ import annotations

from typing import Optional

# 分类结果（供 runner 映射到 risk_events kind）
NONE = "none"
VERIFY = "verify"    # 验证墙（输验证码 / 确认号码）——强风控预警
LIMIT = "limit"      # 临时限制 / 发送过快——限频家族
BAN = "ban"          # 封号页——终态（不进 24h 预警计数）


# 高置信短语（全小写匹配；只收「几乎只在风控屏出现」的，日常聊天不命中）。
# 每类跨平台+多语（WA/LINE 的 en/zh-Hans/zh-Hant/ja 常见文案）。
_BAN_MARKERS = (
    # WhatsApp
    "your phone number is banned",
    "not allowed to use whatsapp",
    "this account is not allowed",
    # LINE
    "account cannot be used",
    "account has been suspended",
    "account is suspended",
    # 中文（简/繁）
    "已被封禁", "已被封号", "账号已被封", "帳號已被封",
    "此帐号无法使用", "此帳號無法使用", "无法继续使用",
    # 日文
    "アカウントが利用できません", "利用できなくなりました", "アカウントは停止",
)

_VERIFY_MARKERS = (
    # WhatsApp / 通用验证码墙
    "verify your phone number", "verify your number", "verify your account",
    "enter the 6-digit code", "enter the code we sent", "we've sent a code",
    "we sent your code", "confirm your phone number",
    # 中文
    "验证你的手机号", "验证你的号码", "验证您的手机号", "驗證你的電話號碼",
    "输入验证码", "輸入驗證碼", "确认你的手机号",
    # 日文
    "認証番号を入力", "電話番号を認証", "認証コード",
)

_LIMIT_MARKERS = (
    # WhatsApp / 通用限速
    "sending messages too fast", "you're sending too many",
    "temporarily restricted", "temporarily banned", "try again later",
    "too many attempts",
    # 中文
    "发送太快", "發送太快", "操作过于频繁", "操作過於頻繁",
    "暂时被限制", "暫時被限制", "请稍后再试", "請稍後再試",
    # 日文
    "送信が速すぎます", "しばらくしてから", "制限されています",
)


def classify_risk_screen(text: Optional[str], *, platform: str = "") -> str:
    """从屏幕文字（uiautomator XML 原文或抽取文本）保守分类风控屏。

    优先级 **ban > verify > limit**（越严重越先判：封号页里也可能带「稍后再试」类
    通用词，不能被 limit 抢先）。空/无命中 → ``none``。``platform`` 目前仅作留痕
    透传，不改判定（词表已跨平台）。
    """
    if not text:
        return NONE
    low = str(text).lower()
    if any(m in low for m in _BAN_MARKERS):
        return BAN
    if any(m in low for m in _VERIFY_MARKERS):
        return VERIFY
    if any(m in low for m in _LIMIT_MARKERS):
        return LIMIT
    return NONE


def note_risk_screen(
    platform: str,
    account_id: str,
    xml_text: Optional[str],
    *,
    risk_recorder=None,
    alert=None,
    now: Optional[float] = None,
) -> str:
    """设备 RPA 发送失败收口的**唯一接线入口**（供 WA/LINE runner 各调一行）。

    classify → verify/limit 记 ``risk_events``（flood 家族，喂 account_health）→ ban
    走告警不计数。**全 best-effort，绝不抛**（记账/告警失败绝不能拖垮 runner 主链，
    与 ``ban_signal.handle_send_exception`` 同纪律）。返回分类结果（供日志/测试）。

    ``risk_recorder``/``alert`` 可注入假对象单测；缺省走 ``record_risk_event``。
    """
    try:
        kind = classify_risk_screen(xml_text, platform=platform)
    except Exception:
        return NONE
    if kind == NONE:
        return kind
    # 记账/告警失败绝不掩盖已算出的分类（返回值供日志/观测）——各自内层吞异常。
    if kind == BAN:
        if alert is not None:
            try:
                alert("account_banned",
                      {"platform": platform, "account_id": account_id},
                      "检测到封号页，账号可能已被平台封禁，待人工核查")
            except Exception:
                pass
        return kind
    try:
        rk = risk_event_kind_for_screen(kind)
        if rk:
            rec = risk_recorder
            if rec is None:
                from src.ops.risk_events import record_risk_event as rec
            rec(platform, account_id, rk, now=now)
    except Exception:
        pass
    return kind


def risk_event_kind_for_screen(screen_kind: str) -> Optional[str]:
    """屏幕分类 → ``risk_events`` 计数类别（喂 account_health）；无则 None。

    - verify / limit → ``flood``（限频/风控家族预警，与 backoff/pause 同轴，降 cap）。
    - ban → None（终态：账号已废，24h 滚动预警无意义；与 ``ban_signal.risk_kind_for``
      的「ban→None」同决策，改由 runner 侧告警 + 人工核查）。
    - none → None。
    """
    from src.ops.risk_events import KIND_FLOOD
    if screen_kind in (VERIFY, LIMIT):
        return KIND_FLOOD
    return None


__all__ = [
    "classify_risk_screen", "risk_event_kind_for_screen", "note_risk_screen",
    "NONE", "VERIFY", "LIMIT", "BAN",
]
