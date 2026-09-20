"""自定义目标「像是达成了」信号检测（纯函数 + 一个落库入口，宁漏勿误）。

P1（2026-08-29）：限时自定义目标（「这轮聊完要到联系方式」）此前只能靠坐席
自己盯聊天流点「标成交」——对方真给了微信号，目标卡毫无反应。本模块把
**高置信**的达成信号（联系方式类）检出来记进 ``params.outcome_signal`` +
事件台账，右栏卡出「像是达成了→去标成交」提示行。

刻意边界：
- **只提示不自动结算**——「成没成」的最终确认权在人（与 persona_guard /
  危机兜底同族的保守哲学：AI 判断只做放大器不做裁决者）；
- **只认高精度模式**（邮箱 / 国内手机号 / +国际号 / @handle / 「微信号 xxx」
  式键值），日期、价格、订单号这类数字串刻意不碰——误报一次，坐席就再也
  不信这个提示了；
- 约试用/约时间类达成**刻意不做**（「明天下午可以」的语义判定假阳太多，
  等有语料再说）；
- 每目标只记**一次**（params 幂等守卫），终态目标不记。
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, Optional

# note 在「要联系方式」的关键词（自定义目标的 note 是坐席手写的推进方向）
_NOTE_CONTACT_RE = re.compile(
    r"联系方式|联系电话|手机号|电话|微信|威信|加微|加个好友|加好友|号码|邮箱|"
    r"telegram|whatsapp|(?<![A-Za-z])line\b|contact|phone|email|wechat",
    re.IGNORECASE,
)

# 入站文本里的联系方式（按精度降序；命中即取第一个）
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_CN_MOBILE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_INTL_PHONE_RE = re.compile(r"\+\d{1,3}[\s-]?\d{6,12}(?!\d)")
# @handle：前一字符不能是字母数字/点（排除邮箱域名段）；总长 5-32
# （Telegram 用户名下限就是 5——顺带排除 "@noon/@home" 这类英文 at 简写）
_HANDLE_RE = re.compile(r"(?<![A-Za-z0-9.])@[A-Za-z][A-Za-z0-9_]{4,31}")
# 「微信号 xxx / 微信是xxx」——CJK 键词后允许裸空格分隔（中文语境里键词后
# 紧跟的 ASCII 串几乎必是 id）
_KEYED_ID_CJK_RE = re.compile(
    r"(?:微信号?|威信|v信|电报号?|飞机号?)"
    r"\s*[:：=是]?\s*([A-Za-z][A-Za-z0-9_.\-]{4,31})",
)
# ASCII 键词必须带显式分隔（号/id 或 冒号=是）——裸空格会把
# "Line tomorrow" 的下一个词当 id；词边界防 "deadline/online" 内的子串命中
_KEYED_ID_ASCII_RE = re.compile(
    r"(?<![A-Za-z])(?:vx|wx|line|whatsapp|telegram|tg)"
    r"(?:\s*(?:号|id)\s*[:：=是]?\s*|\s*[:：=是]\s*)"
    r"([A-Za-z][A-Za-z0-9_.\-]{4,31})",
    re.IGNORECASE,
)
# 键值式里排除的「id」——键词后面紧跟的常见非 id 词（"whatsapp group" 等）
_KEYED_STOPWORDS = frozenset({
    "group", "channel", "official", "support", "download", "update",
})


# ── 影子轨（P2 2026-08-29）：约时间 / 付款确认 ──────────────────────────────
# 语义判定天然比联系方式模糊（「明天下午可以」可能在约试用也可能在约饭）——
# 照 care llm_extract 的 shadow 哲学：先只记事件不打扰坐席，报表看「影子命中
# 的目标最终 done/won 多少」，精度达标才升级为正式提示。
_NOTE_APPOINTMENT_RE = re.compile(
    r"约|试用|演示|见面|通话|开会|上课|体验|demo|meeting|call\b|trial|appointment",
    re.IGNORECASE,
)
_NOTE_PAYMENT_RE = re.compile(
    r"付款|付费|转账|下单|购买|订购|充值|买单|成交|"
    r"pay\b|payment|purchase|order\b|checkout|deposit",
    re.IGNORECASE,
)
# 约时间＝同一条消息里「应允词 + 时间词」都在（缺一即不算）
_AGREE_RE = re.compile(
    r"好的|好啊|好呀|好嘞|可以|没问题|行啊|行的|就这么定|约|定了|成交|"
    r"\bok\b|\bokay\b|\bsure\b|\bdeal\b|works for me|sounds good",
    re.IGNORECASE,
)
_TIME_RE = re.compile(
    r"明天|今晚|今天|后天|下午|上午|中午|晚上|周[一二三四五六日天末]|下周|"
    r"\d{1,2}\s*[:点]\s*(?:\d{1,2}|半)?|"
    r"tomorrow|tonight|this afternoon|next week|\d{1,2}\s*(?:am|pm)\b",
    re.IGNORECASE,
)
# 付款确认＝完成时态短语（「想买/要付」这类意向词刻意不算）
_PAID_RE = re.compile(
    r"已付|付了|付好了|付过了|转了|转过去了|转好了|付款成功|下单了|拍下了|"
    r"充好了|买好了|支付成功|"
    r"\bpaid\b|payment (?:sent|done)|transferred|placed the order|just ordered",
    re.IGNORECASE,
)

SHADOW_KINDS = ("appointment", "payment")


def note_outcome_kind(note: Any) -> str:
    """坐席写的推进方向在要什么结果：``contact``（联系方式）或 ""（不判）。"""
    s = str(note or "").strip()
    if not s:
        return ""
    if _NOTE_CONTACT_RE.search(s):
        return "contact"
    return ""


def note_shadow_kinds(note: Any) -> tuple:
    """note 命中哪些影子结果类型（可多类；contact 走正式轨不在此列）。"""
    s = str(note or "").strip()
    if not s:
        return ()
    out = []
    if _NOTE_APPOINTMENT_RE.search(s):
        out.append("appointment")
    if _NOTE_PAYMENT_RE.search(s):
        out.append("payment")
    return tuple(out)


def detect_appointment(text: Any) -> str:
    """应允词 + 时间词同条出现 → 返回时间片段；否则 ""。"""
    s = str(text or "")
    if not s.strip():
        return ""
    if not _AGREE_RE.search(s):
        return ""
    m = _TIME_RE.search(s)
    return m.group(0).strip() if m else ""


def detect_payment(text: Any) -> str:
    """完成时态付款短语 → 返回命中短语；否则 ""。"""
    s = str(text or "")
    if not s.strip():
        return ""
    m = _PAID_RE.search(s)
    return m.group(0).strip() if m else ""


def detect_contact(text: Any) -> str:
    """入站文本 → 首个高置信联系方式片段；无 → ""。"""
    s = str(text or "")
    if not s.strip():
        return ""
    m = _EMAIL_RE.search(s)
    if m:
        return m.group(0)
    m = _CN_MOBILE_RE.search(s)
    if m:
        return m.group(0)
    m = _INTL_PHONE_RE.search(s)
    if m:
        return m.group(0)
    m = _HANDLE_RE.search(s)
    if m:
        return m.group(0)
    for rx in (_KEYED_ID_CJK_RE, _KEYED_ID_ASCII_RE):
        m = rx.search(s)
        if m and m.group(1).lower() not in _KEYED_STOPWORDS:
            return m.group(1)
    return ""


def detect_outcome(kind: str, text: Any) -> str:
    """按结果类型检测达成信号（contact=正式轨；appointment/payment=影子轨）。"""
    k = str(kind or "")
    if k == "contact":
        return detect_contact(text)
    if k == "appointment":
        return detect_appointment(text)
    if k == "payment":
        return detect_payment(text)
    return ""


def maybe_outcome_signal(
    store: Any,
    goal: Dict[str, Any],
    inbound_text: Any,
    *,
    now: Optional[float] = None,
) -> bool:
    """检出达成信号则记 ``params.outcome_signal`` + ``outcome_signal`` 事件。

    幂等（params 已有信号即跳过）、只对 active 的 custom 目标、绝不抛。
    成功时就地更新入参 ``goal["params"]``——同一轮后续的 goal_view 立即可见。
    """
    try:
        if not isinstance(goal, dict):
            return False
        if str(goal.get("status") or "active") != "active":
            return False
        if str(goal.get("template") or "") != "custom":
            return False
        params = goal.get("params") if isinstance(goal.get("params"), dict) \
            else {}
        if params.get("outcome_signal"):
            return False
        kind = note_outcome_kind(params.get("note"))
        if not kind:
            return False
        hit = detect_outcome(kind, inbound_text)
        if not hit:
            return False
        gid = str(goal.get("goal_id") or "")
        if not gid:
            return False
        n = float(now if now is not None else time.time())
        new_params = dict(params)
        new_params["outcome_signal"] = {
            "kind": kind, "v": str(hit)[:80], "ts": round(n, 1)}
        if not store.update_goal_fields(gid, params=new_params):
            return False
        goal["params"] = new_params
        try:
            store.add_event(gid, "outcome_signal", f"{kind}:{str(hit)[:120]}")
        except Exception:
            pass
        return True
    except Exception:
        return False


def maybe_outcome_shadow(
    store: Any,
    goal: Dict[str, Any],
    inbound_text: Any,
    *,
    now: Optional[float] = None,
) -> tuple:
    """影子轨落账：命中即记 ``outcome_shadow_<kind>`` 事件，**不写 params、
    不出 UI 提示**——纯攒精度数据。每目标每类型只记一次；绝不抛。

    返回本次记下的类型元组（观测/测试用）。"""
    del now  # 事件时间由 store.add_event 自记；形参保留与正式轨同签名
    try:
        if not isinstance(goal, dict):
            return ()
        if str(goal.get("status") or "active") != "active":
            return ()
        if str(goal.get("template") or "") != "custom":
            return ()
        params = goal.get("params") if isinstance(goal.get("params"), dict) \
            else {}
        kinds = note_shadow_kinds(params.get("note"))
        if not kinds:
            return ()
        gid = str(goal.get("goal_id") or "")
        if not gid:
            return ()
        recorded = []
        for kind in kinds:
            ev = f"outcome_shadow_{kind}"
            try:
                if store.count_events_since(gid, ev, 0) > 0:
                    continue
                hit = detect_outcome(kind, inbound_text)
                if not hit:
                    continue
                store.add_event(gid, ev, str(hit)[:120])
                recorded.append(kind)
            except Exception:
                continue
        return tuple(recorded)
    except Exception:
        return ()


__all__ = [
    "SHADOW_KINDS",
    "detect_appointment",
    "detect_contact",
    "detect_outcome",
    "detect_payment",
    "maybe_outcome_shadow",
    "maybe_outcome_signal",
    "note_outcome_kind",
    "note_shadow_kinds",
]
