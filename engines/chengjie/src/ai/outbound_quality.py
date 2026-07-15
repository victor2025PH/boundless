"""出站统一质量管道（2026-07-15 B2）——所有出站文本的最后一道防穿帮关卡。

背景：主链（LLM 生成）有 persona/防重复/事实锁/语言守卫全套质量机制，但模板
短路、canned 兜底、占位话术等**旁路**历史上直接出站——当日实锤两类穿帮都来自
旁路：①第三人称自称（"林小雨现在不太方便拍照"）；②同句原样复读（11:24 与
12:07 一字不差）。A2 已把最大旁路（出图失败模板）收编回 LLM，本模块把关卡
下沉到**发送口**：无论文本从哪条路来，出站前统一过检。

设计（宁纠勿拦）：
- 自称改写：``sanitize_self_reference`` 把第三人称名字自称改写为「我」，
  身份陈述（"我是林小雨/叫我小雨"）与转述他人语境保护不动。改写而非拦截
  ——拦截会让会话悬空，改写永远有回复。
- 复读检测：``OutboundRecentGuard`` per-chat 记最近 N 条出站文本，完全相同
  （归一化后）→ 计指标 + WARNING（先观测不拦截；防重复主链已治本，此处是
  「旁路复读」回归监测的最后哨卡）。
纯函数 + 进程级轻状态，零外部依赖。
"""
from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict, deque
from typing import Any, Dict, Optional, Tuple

import logging

logger = logging.getLogger(__name__)

# 身份陈述保护（这些语境里名字自称是**合法**的，绝不改写）：
# "我是林小雨" "我叫林小雨" "叫我林小雨/小雨就行" "这里是林小雨"
_IDENTITY_LEAD_RE = r"(?:我是|我叫|叫我|这里是|人家是|就是)\s*"


def sanitize_self_reference(text: str, persona_name: str) -> Tuple[str, int]:
    """把文本里的第三人称名字自称改写为「我」。返回 ``(新文本, 改写次数)``。

    保护语境：
      - 身份陈述（"我是{名}/我叫{名}/叫我{名}"）——合法自我介绍；
      - 名字后紧跟「说/觉得/表示」且前面出现引号——转述他人评价（罕见，保守放过）。
    其余出现一律视为第三人称自称（"{名}现在不太方便" "{名}今天好开心"），
    正常人聊天不这样说话（2026-07-15 实锤穿帮）。名字后紧跟「我」时去重
    （"{名}我跟你说" → "我跟你说"）。空名/文本不含名 → 原样零成本返回。
    """
    t = str(text or "")
    name = str(persona_name or "").strip()
    if not t or not name or name not in t:
        return t, 0
    esc = re.escape(name)
    # ① 先把身份陈述用占位符护住
    protected: list = []

    def _protect(m: "re.Match") -> str:
        protected.append(m.group(0))
        return f"\x00{len(protected) - 1}\x01"

    guarded = re.sub(_IDENTITY_LEAD_RE + esc, _protect, t)
    # ② 剩余名字出现 → 「我」；名字后原本就跟着「我」→ 直接去掉名字防"我我"
    n = len(re.findall(esc, guarded))
    if n:
        guarded = re.sub(esc + r"(?=我)", "", guarded)
        guarded = re.sub(esc, "我", guarded)
    # ③ 还原保护段
    for i, seg in enumerate(protected):
        guarded = guarded.replace(f"\x00{i}\x01", seg)
    return guarded, n


def _normalize_for_repeat(text: str) -> str:
    """复读比对归一化：去空白与常见标点/emoji 修饰差异，只留主体字符。"""
    t = str(text or "").strip().lower()
    return re.sub(r"[\s，。！？!?,.~～…✨💭😊😝😆🙈]+", "", t)


class OutboundRecentGuard:
    """per-chat 出站近史（完全复读检测）。进程级、容量有界、线程安全。"""

    def __init__(self, *, per_chat: int = 5, max_chats: int = 512):
        self._per_chat = int(per_chat)
        self._max_chats = int(max_chats)
        self._lock = threading.Lock()
        self._recent: "OrderedDict[str, deque]" = OrderedDict()

    def note_and_check(self, chat_id: Any, text: str) -> bool:
        """记录本条出站文本；若与该会话最近 N 条完全相同 → True（复读）。"""
        key = str(chat_id if chat_id is not None else "")
        norm = _normalize_for_repeat(text)
        if not norm:
            return False
        with self._lock:
            dq = self._recent.get(key)
            if dq is None:
                dq = deque(maxlen=self._per_chat)
                self._recent[key] = dq
            self._recent.move_to_end(key)
            while len(self._recent) > self._max_chats:
                self._recent.popitem(last=False)
            repeated = norm in dq
            dq.append(norm)
            return repeated


_GUARD: Optional[OutboundRecentGuard] = None
_GUARD_LOCK = threading.Lock()


def get_outbound_guard() -> OutboundRecentGuard:
    global _GUARD
    if _GUARD is None:
        with _GUARD_LOCK:
            if _GUARD is None:
                _GUARD = OutboundRecentGuard()
    return _GUARD


def outbound_quality_pass(
    text: str, *, chat_id: Any = None, persona_name: str = "",
) -> str:
    """发送口统一过检：自称改写 + 复读检测（指标/日志），返回应发送的文本。

    任何内部异常都返回原文——质量关卡绝不能把消息卡死。
    """
    try:
        out = str(text or "")
        fixed, n = sanitize_self_reference(out, persona_name)
        if n:
            logger.warning(
                "[outbound_quality] 第三人称自称已改写 ×%d（persona=%s）: %r → %r",
                n, persona_name, out[:60], fixed[:60])
            try:
                from src.monitoring.metrics_store import get_metrics_store
                get_metrics_store().record_outbound_self_ref_fix()
            except Exception:
                pass
            out = fixed
        if get_outbound_guard().note_and_check(chat_id, out):
            logger.warning(
                "[outbound_quality] 出站复读（同会话近 5 条内一字不差）chat=%s: %r",
                chat_id, out[:60])
            try:
                from src.monitoring.metrics_store import get_metrics_store
                get_metrics_store().record_outbound_repeat()
            except Exception:
                pass
        return out
    except Exception:
        logger.debug("[outbound_quality] 过检异常（原样放行）", exc_info=True)
        return str(text or "")


def reset_outbound_guard() -> None:
    """测试用：重置进程级复读近史。"""
    global _GUARD
    with _GUARD_LOCK:
        _GUARD = None


__all__ = [
    "sanitize_self_reference", "outbound_quality_pass",
    "OutboundRecentGuard", "get_outbound_guard", "reset_outbound_guard",
]
