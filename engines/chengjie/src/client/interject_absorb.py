"""A 线插话吸收（Stephanie2「主动等待」，2026-08-02。纯函数核心，全可单测）。

现状问题：A 线消息经队列串行处理，客户连发两条短消息会得到两条独立回复；更糟的
是生成回复的 4-8 秒里客户补了一句，AI 仍按旧输入把回复发出去——「答非所问/
自说自话」。两段式治理（接线在 ``telegram_client``，本模块零 IO 零副作用，
唯一例外是进程级「最新入站时间」注册表）：

- **出队合并**：worker 出队一条可合并消息后，把队列中**紧随的**同 chat 可合并
  消息（≤max_merge-1 条）一并吸收，文本合并成一条输入 → 一次生成覆盖多条。
- **过期中止**：handler 入队时经 ``note_inbound`` 更新本模块「最新入站时间」；
  发送前两道关口（诚实回落改写后 / humanize 思考延迟后）用
  ``should_abort_stale_reply`` 判「生成期间对方又说话了」→ 放弃本条回复，
  队列里那条新消息的处理天然带着旧消息上下文，回复覆盖两条。
- **生成前安静窗 + 未答连发合并**（2026-08-12，修「逐条生成逐条丢弃」实录：
  6 条 3-21s 间隔的连发烧了 6 次生成、丢 5 条，幸存回复只顾最后一条，新闻
  问句的回复被中途丢弃）：入队时 ``note_inbound_text`` 把纯文本登记进
  **未答连发注册表**；生成前任务先等安静窗（``pregen_quiet_sec``，碎片字
  加倍，总等待封顶 ``pregen_max_wait_sec``）——等待期对方又来消息 →
  **生成前零成本让位**（不烧 LLM，新任务统一处理）；安静达成 →
  ``drain_pending_texts`` 取走该会话全部未答文本、碎片感知合并成一条输入
  （新闻/辱骂等检测天然跑在合并后全文上）。生成后仍被过期中止丢弃时经
  ``requeue_texts`` 还回注册表，下一任务照样合并。
  ``pregen_quiet_sec`` 默认 0＝整段关闭（零行为变化）。

「可合并」＝纯文本类：普通文本 + ``[语音转录] `` 前缀文本（A 线语音在入队前已
转录）；``[图片…``/``[视频]``/``[贴纸]`` 等媒体占位与带媒体附件的消息一律不并
（媒体处理语义不动）。只在私聊生效（群聊由接线侧 ``looks_like_group_chat``
旁路）。开关 ``telegram.interject_absorb.enabled``（**默认关**，新子系统铁律）：
关闭时接线两段都不激活＝零行为变化。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_MAX_MERGE = 3
DEFAULT_MAX_AGE_SEC = 45.0
STALE_TOLERANCE_SEC = 0.5   # 浮点容差：同刻入站不算「补话」

# ── 生成前安静窗（默认全关：pregen_quiet_sec=0）──────────────────────────
DEFAULT_PREGEN_QUIET_SEC = 0.0
DEFAULT_PREGEN_MAX_WAIT_SEC = 20.0
FRAG_PIECE_MAX_CHARS = 2        # ≤2 字视为「逐字打」碎片 → 安静窗加倍
_PENDING_MAX_PER_CHAT = 12      # 每会话未答文本上限（超出丢最旧）
_PENDING_TTL_SEC = 900.0        # 未答文本寿命（陈旧条目不再参与合并）

# 与 telegram_client 的 _VOICE_PREFIX 同值（入队前转录产物，AI 消费端会剥）
VOICE_TRANSCRIPT_PREFIX = "[语音转录] "
# 队列元素 item["message"]（pyrogram Message）上的媒体附件属性——任一存在即拒并。
# voice/audio 刻意不在列：语音已转录成文本（前缀判据放行），媒体语义不受影响。
_MEDIA_ATTRS = ("photo", "document", "video", "video_note", "animation",
                "sticker")

# ── 进程级「最新入站时间」注册表（bounded，线程安全）────────────────────────
_REG_LOCK = threading.Lock()
_LATEST_INBOUND: Dict[str, float] = {}
_REG_MAX = 512
_REG_TRIM_TO = 384

# ── 进程级「未答连发文本」注册表（bounded，线程安全）────────────────────────
_PENDING_LOCK = threading.Lock()
_PENDING_TEXTS: Dict[str, List[Tuple[float, str]]] = {}
_PENDING_CHAT_MAX = 512


def parse_interject_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """解析 ``telegram.interject_absorb``（缺失/坏形一律回默认：关）。"""
    try:
        blk = ((config or {}).get("telegram") or {}).get("interject_absorb")
        blk = dict(blk) if isinstance(blk, dict) else {}
    except Exception:
        blk = {}
    out: Dict[str, Any] = {"enabled": bool(blk.get("enabled", False))}
    try:
        out["max_merge"] = max(1, int(blk.get("max_merge", DEFAULT_MAX_MERGE)))
    except Exception:
        out["max_merge"] = DEFAULT_MAX_MERGE
    try:
        out["max_age_sec"] = max(
            0.0, float(blk.get("max_age_sec", DEFAULT_MAX_AGE_SEC)))
    except Exception:
        out["max_age_sec"] = DEFAULT_MAX_AGE_SEC
    try:
        out["pregen_quiet_sec"] = max(0.0, float(
            blk.get("pregen_quiet_sec", DEFAULT_PREGEN_QUIET_SEC)))
    except Exception:
        out["pregen_quiet_sec"] = DEFAULT_PREGEN_QUIET_SEC
    try:
        out["pregen_frag_quiet_sec"] = max(0.0, float(
            blk.get("pregen_frag_quiet_sec",
                    out["pregen_quiet_sec"] * 2.0)))
    except Exception:
        out["pregen_frag_quiet_sec"] = out["pregen_quiet_sec"] * 2.0
    try:
        out["pregen_max_wait_sec"] = max(0.0, float(
            blk.get("pregen_max_wait_sec", DEFAULT_PREGEN_MAX_WAIT_SEC)))
    except Exception:
        out["pregen_max_wait_sec"] = DEFAULT_PREGEN_MAX_WAIT_SEC
    return out


def _text_is_mergeable(text: Any) -> bool:
    """纯文本类判据：普通文本放行；``[语音转录] `` 前缀且有实体内容放行；
    其余 ``[``/``【`` 开头一律视为媒体/占位（[图片内容]/[视频]/[贴纸]/
    [语音消息 - 转录失败]…）拒并。"""
    s = str(text or "").strip()
    if not s:
        return False
    if s.startswith(VOICE_TRANSCRIPT_PREFIX):
        return bool(s[len(VOICE_TRANSCRIPT_PREFIX):].strip())
    if s.startswith(("[", "【")):
        return False
    return True


def can_merge_queued(item: Any, *, chat_id: Any, now: float,
                     max_age_sec: float) -> bool:
    """队列元素（``_process_message`` 入队的 msg_data dict）是否可并入
    chat_id 的本轮输入。仅同 chat、纯文本类、带媒体附件为否、入队年龄
    ≤max_age_sec；结构不符/缺 ``_enq_ts`` 一律 False（宁可不并）。"""
    try:
        if not isinstance(item, dict):
            return False
        if item.get("chat_id") != chat_id:
            return False
        if not _text_is_mergeable(item.get("text")):
            return False
        msg = item.get("message")
        if msg is not None:
            for attr in _MEDIA_ATTRS:
                if getattr(msg, attr, None):
                    return False
        enq = float(item.get("_enq_ts") or 0.0)
        if enq <= 0.0:
            return False
        if float(now) - enq > float(max_age_sec):
            return False
        return True
    except Exception:
        return False


def merge_texts(texts: List[str]) -> str:
    """按顺序 ``\\n`` 连接（各段剥首尾空白、剔空段）。"""
    parts: List[str] = []
    for t in texts or []:
        s = str(t or "").strip()
        if s:
            parts.append(s)
    return "\n".join(parts)


def should_abort_stale_reply(msg_ts: float, latest_inbound_ts: float) -> bool:
    """生成期间对方又说话（latest > msg_ts，容差 0.5s）→ 本条回复已过期。
    任一时间戳缺失（≤0）＝信息不足不中止（宁可发也不无声吞掉回复）。"""
    try:
        m = float(msg_ts or 0.0)
        latest = float(latest_inbound_ts or 0.0)
        if m <= 0.0 or latest <= 0.0:
            return False
        return (latest - m) > STALE_TOLERANCE_SEC
    except Exception:
        return False


def note_inbound(chat_id: Any, ts: Optional[float] = None) -> None:
    """handler 入队成功后登记该会话最新入站时间（只进不退；bounded 裁剪）。"""
    try:
        key = str(chat_id)
        val = float(ts if ts is not None else time.time())
        with _REG_LOCK:
            if val > _LATEST_INBOUND.get(key, 0.0):
                _LATEST_INBOUND[key] = val
            if len(_LATEST_INBOUND) > _REG_MAX:
                overflow = len(_LATEST_INBOUND) - _REG_TRIM_TO
                for k in sorted(_LATEST_INBOUND,
                                key=_LATEST_INBOUND.get)[:overflow]:
                    _LATEST_INBOUND.pop(k, None)
    except Exception:
        pass


def latest_inbound_ts(chat_id: Any) -> float:
    """该会话已知最新入站时间（无记录返回 0.0，关口据此不中止）。"""
    try:
        with _REG_LOCK:
            return float(_LATEST_INBOUND.get(str(chat_id), 0.0))
    except Exception:
        return 0.0


def pregen_quiet_for(text: Any, cfg: Dict[str, Any]) -> float:
    """本条消息适用的生成前安静窗秒数（0＝不等）。

    碎片字（剥空白后 ≤2 字）用 ``pregen_frag_quiet_sec``（默认＝普通档 2 倍）
    ——逐字打的人下一「字」通常比下一「句」来得慢；普通句用
    ``pregen_quiet_sec``。语音转录前缀先剥再量长度。
    """
    try:
        s = str(text or "").strip()
        if s.startswith(VOICE_TRANSCRIPT_PREFIX):
            s = s[len(VOICE_TRANSCRIPT_PREFIX):].strip()
        if not s:
            return 0.0
        if len(s) <= FRAG_PIECE_MAX_CHARS:
            return float(cfg.get("pregen_frag_quiet_sec") or 0.0)
        return float(cfg.get("pregen_quiet_sec") or 0.0)
    except Exception:
        return 0.0


def _strip_voice_prefix(text: str) -> str:
    s = str(text or "").strip()
    if s.startswith(VOICE_TRANSCRIPT_PREFIX):
        return s[len(VOICE_TRANSCRIPT_PREFIX):].strip()
    return s


def note_inbound_text(chat_id: Any, ts: float, text: Any) -> None:
    """入队时登记未答文本（仅纯文本类；bounded + TTL 裁剪；绝不抛）。"""
    try:
        if not _text_is_mergeable(text):
            return
        key = str(chat_id)
        val = float(ts or 0.0) or time.time()
        entry = (val, _strip_voice_prefix(str(text)))
        if not entry[1]:
            return
        now = time.time()
        with _PENDING_LOCK:
            lst = _PENDING_TEXTS.setdefault(key, [])
            lst.append(entry)
            lst[:] = [e for e in lst if now - e[0] <= _PENDING_TTL_SEC]
            if len(lst) > _PENDING_MAX_PER_CHAT:
                del lst[:len(lst) - _PENDING_MAX_PER_CHAT]
            if len(_PENDING_TEXTS) > _PENDING_CHAT_MAX:
                for k in sorted(
                        _PENDING_TEXTS,
                        key=lambda k2: (_PENDING_TEXTS[k2][-1][0]
                                        if _PENDING_TEXTS[k2] else 0.0),
                )[:len(_PENDING_TEXTS) - _PENDING_CHAT_MAX]:
                    _PENDING_TEXTS.pop(k, None)
    except Exception:
        pass


def drain_pending_texts(chat_id: Any,
                        upto_ts: float) -> List[Tuple[float, str]]:
    """取走该会话 ``ts ≤ upto_ts+容差`` 的全部未答文本（时序保序）。

    返回 ``(ts, text)`` 条目列表——调用方留存以便过期中止时 ``requeue_texts``
    还回。更晚的条目（安静窗判定后才到的极限竞态）留在表里给下一任务。
    """
    try:
        key = str(chat_id)
        cutoff = float(upto_ts or 0.0) + STALE_TOLERANCE_SEC
        now = time.time()
        with _PENDING_LOCK:
            lst = _PENDING_TEXTS.get(key) or []
            take = [e for e in lst
                    if e[0] <= cutoff and now - e[0] <= _PENDING_TTL_SEC]
            keep = [e for e in lst if e[0] > cutoff]
            if keep:
                _PENDING_TEXTS[key] = keep
            else:
                _PENDING_TEXTS.pop(key, None)
        return sorted(take, key=lambda e: e[0])
    except Exception:
        return []


def requeue_texts(chat_id: Any,
                  entries: Optional[List[Tuple[float, str]]]) -> None:
    """过期中止丢弃回复时把已消费的未答文本还回注册表（保序去重）。"""
    try:
        if not entries:
            return
        key = str(chat_id)
        with _PENDING_LOCK:
            lst = _PENDING_TEXTS.setdefault(key, [])
            have = {(round(float(t), 3), s) for t, s in lst}
            for t, s in entries:
                if (round(float(t), 3), str(s)) not in have:
                    lst.append((float(t), str(s)))
            lst.sort(key=lambda e: e[0])
            if len(lst) > _PENDING_MAX_PER_CHAT:
                del lst[:len(lst) - _PENDING_MAX_PER_CHAT]
    except Exception:
        pass


def merge_pending_entries(entries: List[Tuple[float, str]]) -> str:
    """把未答条目合并成一条输入——碎片感知（复用 B 线 ``merge_burst_texts``：
    连续 ≤2 字碎片无缝拼句、整句间换行）。B 线模块不可用时回退 ``\\n`` 连接。"""
    texts = [str(s) for _, s in entries or [] if str(s or "").strip()]
    if not texts:
        return ""
    try:
        from src.inbox.inbound_debounce import merge_burst_texts
        return merge_burst_texts(texts)
    except Exception:
        return merge_texts(texts)


def _reset_registry_for_tests() -> None:
    """仅测试用：清空进程级最新入站注册表 + 未答文本注册表。"""
    with _REG_LOCK:
        _LATEST_INBOUND.clear()
    with _PENDING_LOCK:
        _PENDING_TEXTS.clear()


__all__ = [
    "DEFAULT_MAX_AGE_SEC", "DEFAULT_MAX_MERGE", "DEFAULT_PREGEN_MAX_WAIT_SEC",
    "DEFAULT_PREGEN_QUIET_SEC", "FRAG_PIECE_MAX_CHARS", "STALE_TOLERANCE_SEC",
    "VOICE_TRANSCRIPT_PREFIX", "can_merge_queued", "drain_pending_texts",
    "latest_inbound_ts", "merge_pending_entries", "merge_texts",
    "note_inbound", "note_inbound_text", "parse_interject_cfg",
    "pregen_quiet_for", "requeue_texts", "should_abort_stale_reply",
]
