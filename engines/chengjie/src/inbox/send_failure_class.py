# -*- coding: utf-8 -*-
"""出站发送失败的「人话分类」与改期决策（实施86 域B-1，工单 #21/#23/#34/#49）。

背景：Messenger 边车（B99 批）早已把失败分层成 reason_code（send_backoff /
account_blocked / e2ee_pin_pending / composer_* …）+ retry_after_ms，但 Python
消费端一直把它们当裸 HTTP 异常字符串透传——坐席看到的是
「Server error '429 Too Many Requests'」（#49 原话）、「500 Internal Server
Error」（#23 原话），autosend 拿不到恢复时刻提示，只能当场终局失败白丢草稿。

本模块只放纯函数（路由 toast / 失败留痕气泡 / autosend 改期三个消费口共用一份
判词，防止各算一套漂移）：
- ``classify_send_failure``：失败文本/reason_code → 五类人话代号
- ``plan_failure_retry``：带 retry_after_ms 提示的失败 → 改期/终局决策
"""
from __future__ import annotations

from typing import Tuple

# 分类代号 → 路由层 i18n 键（tr(request, key) 出人话；键在 i18n_packs/errors.py）
FAILURE_CLASS_I18N = {
    "rate_limited": "err.inbox.sendfail.rate_limited",
    "platform_block": "err.inbox.sendfail.platform_block",
    "e2ee_pin": "err.inbox.sendfail.e2ee_pin",
    "session": "err.inbox.sendfail.session",
    "channel": "err.inbox.sendfail.channel",
}

# 词表刻意保守：只收「确定性出自我方边车/编排器」的标记，宁可漏归类（回落原文）
# 也不错归类误导排查方向。顺序即优先级——限频/风控标记可能与其他词共存于同一串。
_RATE_MARKS = ("send_backoff", "too many requests")
_BLOCK_MARKS = ("account_blocked", "temporarily blocked")
_PIN_MARKS = ("e2ee_pin", "pin prompt", "recovery pin")
_SESSION_MARKS = ("needs_login", "logged_out", "logged out", "not logged in",
                  "session unhealthy", "session_unhealthy", "cookie expired",
                  "needs manual re-login", "login_form")
_CHANNEL_MARKS = ("composer", "upstream", "bubble_fail", "render_timeout",
                  "internal server error", "worker unreachable",
                  "not delivered", "服务不可达")


def classify_send_failure(text: str = "", reason_code: str = "") -> str:
    """失败文本 + 可选结构化 reason_code → 分类代号（认不出返 ""＝保留原文）。

    Args:
        text: 失败描述（httpx 异常串 / 边车 error 字段 / 编排器 error，任意拼合）。
        reason_code: 边车结构化败因（有则优先参与匹配）。
    Returns:
        ``rate_limited|platform_block|e2ee_pin|session|channel|""``。
    """
    low = f"{reason_code} {text}".strip().lower()
    if not low:
        return ""
    if any(m in low for m in _RATE_MARKS):
        return "rate_limited"
    if any(m in low for m in _BLOCK_MARKS):
        return "platform_block"
    if any(m in low for m in _PIN_MARKS):
        return "e2ee_pin"
    if any(m in low for m in _SESSION_MARKS):
        return "session"
    if any(m in low for m in _CHANNEL_MARKS):
        return "channel"
    return ""


# 改期护栏缺省：> defer_cap（如平台风控 2h 冻结）不改期——草稿滞留数小时毫无
# 意义，直接终局留痕让坐席看见；改期次数封顶防「边车一直给提示」的死循环。
DEFER_CAP_SEC = 900.0        # 15min；边车连败退避封顶 5min，冻结 2h 远超此值
DEFER_MAX_TIMES = 3
DEFER_MARGIN_SEC = 5.0       # 贴着退避窗边界重投会撞秒差，加确定性余量


def plan_failure_retry(*, hint_ms: int = 0, deferrals_used: int = 0,
                       permanent: bool = False,
                       max_deferrals: int = DEFER_MAX_TIMES,
                       defer_cap_sec: float = DEFER_CAP_SEC) -> Tuple[str, float]:
    """带恢复时刻提示的投递失败要不要改期重投（实施86 域B-1，#49）。

    与 recoverable 通用重试正交：那是「盲重试撞运气」（默认关），这是「边车
    明确说了几秒后恢复，就按它说的时间等」——边车的退避/冻结窗是权威时钟，
    在窗内重试必然白撞（Node 侧 429 快速失败，虽不碰浏览器但白烧一次往返）。

    Args:
        hint_ms: 边车 retry_after_ms（0=无提示）。
        deferrals_used: 该条草稿已改期次数。
        permanent: 永久性错误（被拉黑/注销等）——永不改期。
    Returns:
        ("defer", 延迟秒) 或 ("fail", 0.0)。
    """
    if permanent or hint_ms <= 0:
        return ("fail", 0.0)
    delay = hint_ms / 1000.0
    if delay > defer_cap_sec:
        return ("fail", 0.0)
    if deferrals_used >= max_deferrals:
        return ("fail", 0.0)
    return ("defer", delay + DEFER_MARGIN_SEC)


__all__ = [
    "FAILURE_CLASS_I18N", "classify_send_failure", "plan_failure_retry",
    "DEFER_CAP_SEC", "DEFER_MAX_TIMES", "DEFER_MARGIN_SEC",
]
