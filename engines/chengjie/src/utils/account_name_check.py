"""账号显示名 × 人设名一致性（O-1 E，#255 TDNJHQ / 8FJDUK ①，2026-09-08）。

事故：账号昵称 Vanessa、人设名 Mizuki——客户看到聊天头像叫一个名字、聊天里自称另一个，
当场察觉「不是同一个人」。这里只做**纯函数比对**：账号自身显示名（``account_registry``
``meta.self_name``，登录后由 ``account_self_profile`` 富集）与绑定人设 ``profile.name``
归一后比对；**昵称含人设名算一致**（「Mizuki 🌸」「Mizuki | 東京」都是 Mizuki）。

改名动作不在本模块：TG / WA 走既有 ``account_profile_push``（``POST /api/accounts/{p}/{a}/profile``
带 ``name``）；LINE / Messenger 官方无写口 → 只能「改人设名」或到手机 App 改（能力表
``PLATFORM_CAPS`` 是单一事实源，本模块只读它）。

与 ``test_self_name_guard`` / ``persona_guard.self_name`` 无关——那是「AI 不得自称域角色名」。
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)

# 去掉表情 / 符号 / 空白 / 全角标点，保留字母数字与 CJK；大小写折叠
_STRIP_RE = re.compile(r"[^0-9a-z\u00c0-\u024f\u0400-\u04ff\u0e00-\u0e7f\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]+")
# 平台常见后缀噱头（不影响身份判定）
_NOISE_WORDS = ("official", "real", "私人", "本人", "小号")

_LOGGED_LOCK = threading.Lock()
_LOGGED: Set[str] = set()


def normalize_display_name(name: Any) -> str:
    """显示名归一：小写、去表情 / 标点 / 空白 / 噱头词。空 → ""。"""
    s = str(name or "").strip().lower()
    if not s:
        return ""
    s = _STRIP_RE.sub("", s)
    for w in _NOISE_WORDS:
        s = s.replace(w, "")
    return s


def names_consistent(account_name: Any, persona_name: Any) -> Optional[bool]:
    """账号显示名与人设名是否一致。任一为空 → None（判不出，不报）。

    一致 = 归一后相等，或**账号名包含人设名**（「Mizuki 🌸」/「Mizuki Tanaka」），或人设名
    含账号名且账号名 ≥3 字符（人设「Mizuki Tanaka」账号「Mizuki」）。
    """
    a = normalize_display_name(account_name)
    p = normalize_display_name(persona_name)
    if not a or not p:
        return None
    if a == p or p in a:
        return True
    if len(a) >= 3 and a in p:
        return True
    return False


def can_push_name(platform: str) -> bool:
    """该平台能否由后台直接改账号昵称（读 account_profile_push.PLATFORM_CAPS，单一事实源）。"""
    try:
        from src.integrations.account_profile_push import PLATFORM_CAPS
        cap = PLATFORM_CAPS.get(str(platform or "").lower()) or {}
        return bool(cap.get("mode") == "direct" and cap.get("name"))
    except Exception:
        return False


def check_name_mismatch(
    platform: str, account_id: str, account_name: Any, persona_name: Any,
    *, persona_id: str = "",
) -> Optional[Dict[str, Any]]:
    """比对并给前端一个可直接渲染的结构；一致 / 判不出 → None。

    返回 ``{platform, account_id, account_name, persona_id, persona_name, can_push}``：
    ``can_push=True`` → 页面给「按人设改账号名」（POST profile name=人设名）；
    False → 只给「改人设名」+「到手机 App 改昵称」提示。
    """
    ok = names_consistent(account_name, persona_name)
    if ok is None or ok:
        return None
    return {
        "platform": str(platform or "").lower(),
        "account_id": str(account_id or ""),
        "account_name": str(account_name or "").strip(),
        "persona_id": str(persona_id or ""),
        "persona_name": str(persona_name or "").strip(),
        "can_push": can_push_name(platform),
    }


def log_mismatch_once(m: Optional[Dict[str, Any]]) -> bool:
    """``[self-name] account=… persona=… mismatch=1`` 每 (平台, 账号, 账号名, 人设名) 组合
    进程内只落一次（状态接口被轮询，不能每次刷屏）。返回是否真落了日志。"""
    if not m:
        return False
    key = "%s:%s|%s|%s" % (m.get("platform"), m.get("account_id"),
                           m.get("account_name"), m.get("persona_name"))
    with _LOGGED_LOCK:
        if key in _LOGGED:
            return False
        if len(_LOGGED) > 512:
            _LOGGED.clear()
        _LOGGED.add(key)
    logger.info(
        "[self-name] account=%s:%s account_name=%r persona=%s persona_name=%r mismatch=1 can_push=%d",
        m.get("platform"), m.get("account_id"), m.get("account_name"),
        m.get("persona_id") or "-", m.get("persona_name"), 1 if m.get("can_push") else 0)
    return True


def reset_logged() -> None:
    with _LOGGED_LOCK:
        _LOGGED.clear()


__all__ = [
    "normalize_display_name", "names_consistent", "can_push_name",
    "check_name_mismatch", "log_mismatch_once", "reset_logged",
]
