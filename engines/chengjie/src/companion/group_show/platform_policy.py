# -*- coding: utf-8 -*-
"""群戏的**平台风险治理层**——「这个平台能不能真发、真发时最多几个号开口」。

## 为什么单独一个模块（而不是塞进 ``live.py`` 的双锁里）

``live.py`` 的双锁（``live.enabled`` 配置锁 + ``confirm_live`` 参数锁）是**全局急停**，
性质是「今天要不要让群戏发消息」。本模块管的是**另一个维度**：「同样开着闸，
Messenger 这种网页自动化平台该不该跟 Telegram 一视同仁地真发」。两件事正交：

* 双锁齐开、但平台在红区 → 仍然不发（平台治理拦下）；
* 平台放行、但双锁没开齐 → 仍然不发（急停在拦）。

把它们混进一个开关，就会出现「为了在 TG 上真发而打开配置锁，结果 Messenger 也跟着
开始真发」——而 Messenger 的网页 RPA 链路封号风险最高，既有治理早已把它的自动化档位
封顶 ``review``（见 ``inbox.auto_draft.platform_modes``）。本模块让群戏真发继承同一
红区判定——这是部署级治理，性质等同 ``cap_automation_mode``。

**判定挂在引擎里、实现留在这里**：``live.plan_live`` 消费 :func:`platform_live_allowed`
（一行 import 纯函数，零副作用零新依赖面），于是 web 路由 / CLI / 将来任何新调用方都
自动过同一道闸——治理挂在路由层的话，CLI ``scripts/group_show_live.py --platform
messenger`` 会绕过它。本模块自身仍不 import 任何引擎/发送件，方向是单向的。

## 缺省表即策略

内置 :data:`DEFAULT_PLATFORM_RISK` 就是出厂策略；运营只在要**偏离**缺省时才写
``companion.group_show.platform_risk.<平台>`` overlay。两条缺省决策：

* **Messenger 缺省禁真发**（红区）：网页 RPA，封号代价与既有 review 封顶同源。
  解禁是运营决策，改前应过意向板——所以缺省关，让「打开」是一个显式动作。
* **未列出的平台**（zalo / instagram / 将来新接的）缺省**也禁**：新平台先在
  排练/排班/观察里证明自己，再由运营显式开闸。「默认能发」对一个没验证过群语义的
  平台是最危险的缺省。

* **WhatsApp 缺省 ``max_speakers=1``**：手机号是最贵的号，且多号在同一 WA 群一唱一和
  是关联封号最典型的姿势。首场先按单号（solo 剧本）跑，灰度两周读数干净再由运营放开。

## 纯函数，零副作用

不 import store / orchestrator / 任何发送件——与 ``ledgers`` 同样只吃鸭子类型配置 dict，
可离线 dry-run 打分。路由层的真发 / 体检共用本模块的判定（一份判词，杜绝「体检说绿、
真发拒演」的口径漂移）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_PLATFORM_RISK",
    "KNOWN_PLATFORMS",
    "clamp_platform_speakers",
    "known_platforms",
    "normalize_platform",
    "platform_live_allowed",
    "resolve_platform_risk",
]

#: 编排器能接管的平台全集（镜像 ``AccountOrchestrator`` 的 worker 类）。
#: 用作入参白名单：非白名单平台一律拒（防错拼 / 注入的平台串静默读到空数据、
#: 假绿灯）。新增 worker 时同步这里，门禁 ``test_group_show_platform_policy`` 钉住
#: 它与编排器 worker 集合一致。
KNOWN_PLATFORMS: Tuple[str, ...] = (
    "telegram", "whatsapp", "line", "messenger", "zalo", "instagram",
    # QQ 协议登录（个人号，Milky；2026-09-07）——未登记风险策略 → _FALLBACK_RISK 禁真发
    "qq",
)

#: 未在 :data:`DEFAULT_PLATFORM_RISK` 显式登记的平台走这条兜底——**禁真发**。
#: 一个没验证过群语义的平台「默认能发」是最危险的缺省。
_FALLBACK_RISK: Dict[str, Any] = {"live": False, "reason": "platform_unvetted"}

#: 出厂平台风险策略。键＝平台，值＝该平台的治理档：
#: - ``live``（bool）：缺省是否允许真发；
#: - ``max_speakers``（int，可选）：本平台每场开口人数硬顶（与预算 cap 取更紧者）；
#: - ``reason``（str，可选）：``live=False`` 时给运营看的原因码（→ i18n）。
DEFAULT_PLATFORM_RISK: Dict[str, Dict[str, Any]] = {
    "telegram":  {"live": True},
    "whatsapp":  {"live": True,  "max_speakers": 1},
    "line":      {"live": True,  "max_speakers": 2},
    "messenger": {"live": False, "reason": "platform_red_zone"},
    # zalo / instagram / 未知 → _FALLBACK_RISK（禁真发，先证明再开闸）
}


def known_platforms() -> Tuple[str, ...]:
    """合法平台白名单（供入参校验）。"""
    return KNOWN_PLATFORMS


def normalize_platform(raw: Any) -> str:
    """归一平台串（strip + lower）；**不**做白名单校验（那是调用方的 400 决策）。

    空 / 非字符串 → ``""``（调用方按缺省 telegram 处理或拒）。
    """
    try:
        return str(raw or "").strip().lower()
    except Exception:  # noqa: BLE001 —— 归一绝不抛
        return ""


def resolve_platform_risk(
    app_config: Optional[Dict[str, Any]], platform: str,
) -> Dict[str, Any]:
    """合并「内置缺省 × overlay 覆写」，返回该平台的生效风险档。

    overlay 路径：``companion.group_show.platform_risk.<平台>``（浅合并，逐键覆写）。
    读配置的任何异常都退回内置缺省（安全侧：读不出配置不等于放开红区）。
    """
    plat = normalize_platform(platform) or "telegram"
    base = dict(DEFAULT_PLATFORM_RISK.get(plat, _FALLBACK_RISK))
    try:
        node = ((app_config or {}).get("companion") or {}).get("group_show") or {}
        overrides = (node.get("platform_risk") or {}).get(plat)
        if isinstance(overrides, dict):
            base.update({str(k): v for k, v in overrides.items()})
    except Exception:  # noqa: BLE001 —— 配置形状不由我们保证；坏配置回落缺省
        logger.debug("[group_show.platform_policy] platform_risk 解析失败 plat=%s",
                     plat, exc_info=True)
    return base


def platform_live_allowed(
    app_config: Optional[Dict[str, Any]], platform: str,
) -> Tuple[bool, str]:
    """本平台**缺省是否允许真发** → ``(allowed, reason)``。

    这是双锁**之上**的第三道闸：与 ``live.enabled`` / ``confirm_live`` 全部齐开才发。
    ``allowed=True`` 时 ``reason=""``；否则 ``reason`` 是原因码（红区 / 未验证 / 显式关）。
    """
    risk = resolve_platform_risk(app_config, platform)
    allowed = bool(risk.get("live"))
    if allowed:
        return True, ""
    return False, str(risk.get("reason") or "platform_off")


def clamp_platform_speakers(
    app_config: Optional[Dict[str, Any]], platform: str, requested: int,
) -> int:
    """把「本场开口人数」夹到平台硬顶之内（纯上界，安全侧）。

    语义：
    * 平台无 ``max_speakers`` → 原样返回（``requested`` 不变，含 0＝按预算自动限）；
    * ``requested > 0`` → ``min(requested, 平台顶)``；
    * ``requested == 0``（自动/不限）→ 收成**平台顶**——否则预算在无台账的新平台上
      会自动解成 4 人，直接冲破平台安全上限（首场 WA 最需要这条兜住）。

    刻意**不**受 ``over_budget`` 影响：那是「预算」层的越界预览开关，平台顶是安全上限，
    两者不是一回事。夹紧永远生效。
    """
    try:
        req = int(requested or 0)
    except (TypeError, ValueError):
        req = 0
    risk = resolve_platform_risk(app_config, platform)
    try:
        cap = int(risk.get("max_speakers") or 0)
    except (TypeError, ValueError):
        cap = 0
    if cap <= 0:
        return req
    if req > 0:
        return min(req, cap)
    return cap
