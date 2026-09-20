# -*- coding: utf-8 -*-
"""人工接管 → 切回 AI 的**接力摘要**（接力记忆 P0-3，2026-09-12）。

问题：坐席手动聊了一阵（发图、聊家常、答应事）再把会话交回 AI（下拉切全自动 /
静默超时自动接回），切换动作本身**零副作用**——AI 下一轮只靠历史窗口（B 线 30 条、
A 线 10 条）和几条零散记忆钩子接话；接管稍长就出窗，AI 像失忆一样重新打招呼、
重复问已答过的问题、否认坐席替它说过/发过的东西。

本模块在**切换那一刻**做一次确定性归纳（不调 LLM，逐字引原话 → 天然接地）：

- 取接管窗口内的消息（窗口起点＝首条人工出站 ``_takeover_started_ts`` / 手动档
  写入时刻，取更早者；上限 ``MAX_WINDOW_SEC``），
- 分「你（坐席代发）说过 / 发过的图」与「对方说过 / 发过的图」两栏，逐条压成单行
  （图片行折成 ``发了图片：配文（画面：VLM 描述）``），
- 写进 ``user_context["_handoff_note"]``（ContextStore 持久，A/B 两线同桶），
- ``skill_manager._inject_self_state`` 每轮把 :func:`handoff_note` 拼进
  ``_self_state_block``；注入 ``MAX_USES`` 轮或超 ``TTL_SEC`` 后自动消失
  （届时窗口/摘要机制已把它消化进正常上下文）。

刻意不写 episodic 用户事实库（Phase8 教训：系统/人设侧内容混进「关于对方的事实」）。
全部入口 best-effort、绝不抛、零阻断档位切换。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

NOTE_KEY = "_handoff_note"
TAKEOVER_TS_KEY = "_takeover_started_ts"

MAX_WINDOW_SEC = 24 * 3600.0     # 接管窗口最长回看
MAX_ROWS_SCAN = 120              # 从 store 取多少条来筛窗口
MAX_OUT_LINES = 8
MAX_IN_LINES = 6
LINE_MAX = 90
NOTE_MAX_CHARS = 1400
MAX_USES = 10                    # 注入多少轮后撤下
TTL_SEC = 12 * 3600.0            # 或超时撤下

_IMAGE_KINDS = {"image", "photo", "picture", "sticker"}
_VOICE_KINDS = {"voice", "audio"}
_VIDEO_KINDS = {"video", "video_note", "animation", "gif"}
_DESC_MARKERS = ("[图片内容]", "[视频内容]", "[贴纸内容]")
_OUT_LABEL = "[我方发出的"


def _one_line(s: str, limit: int = LINE_MAX) -> str:
    t = " ".join(str(s or "").split())
    return t if len(t) <= limit else t[: limit - 1] + "…"


def _split_caption_desc(text: str) -> tuple:
    """``配文\\n[图片内容] 描述`` → (配文, 描述)；无标记 → (text, "")。"""
    t = str(text or "")
    idx, mk_len = -1, 0
    for mk in _DESC_MARKERS:
        i = t.find(mk)
        if i >= 0 and (idx < 0 or i < idx):
            idx, mk_len = i, len(mk)
    if idx < 0:
        return t.strip(), ""
    return t[:idx].strip(), t[idx + mk_len:].strip()


def _media_word(mt: str) -> str:
    m = str(mt or "").lower()
    if m in _IMAGE_KINDS:
        return "贴纸" if m == "sticker" else "图片"
    if m in _VOICE_KINDS:
        return "语音"
    if m in _VIDEO_KINDS:
        return "视频"
    return "文件"


def _fmt_row(r: Dict[str, Any]) -> str:
    """一条消息 → 单行事实（空串＝不值一提）。"""
    text = str(r.get("text") or "")
    mt = str(r.get("media_type") or "").lower()
    has_media = bool(mt or r.get("media_ref"))
    if text.startswith(_OUT_LABEL):
        # normalize_history 口径的标签行（店里落库的一般没有；防双标）
        j = text.find("]")
        text = text[j + 1:].strip() if j > 0 else text
    cap, desc = _split_caption_desc(text)
    if cap.startswith("[") and cap.endswith("]") and " " not in cap:
        cap = ""          # 裸占位（[图片]/[语音]…）不是内容
    if has_media and mt not in ("document", "file"):
        word = _media_word(mt)
        if mt in _VOICE_KINDS:
            # 语音行的正文就是转写/念出的文字
            return _one_line(f"（语音）{cap}") if cap else "发了一条语音（未转写）"
        parts = [f"发了{word}"]
        if cap:
            parts.append(f"：{_one_line(cap, 50)}")
        if desc:
            # Q-36 #318：「画面」= 识图所见 ≠ TA 自述——规则句由 build_handoff_note 在「对方说过/发过」段尾附
            parts.append(f"（画面：{_one_line(desc, 60)}）")
        return "".join(parts)
    return _one_line(cap) if cap else ""


def _has_peer_media_desc(rows: List[Dict[str, Any]]) -> bool:
    """窗口内对方是否发过带识图描述的图 / 视频（决定要不要附 Q-36 规则句）。"""
    for r in rows or []:
        if not isinstance(r, dict) or str(r.get("direction") or "in") == "out":
            continue
        if _split_caption_desc(str(r.get("text") or ""))[1]:
            return True
    return False


def _fmt_clock(ts: float) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:
        return ""


MERGE_JOIN = " / "


def _is_media_line(line: str) -> bool:
    """_fmt_row 产出的媒体行：「发了图片/视频/贴纸…」「（语音）…」「发了一条语音（未转写）」。"""
    return line.startswith("发了") or line.startswith("（语音）")


def _merge_runs(seq: List[tuple]) -> List[tuple]:
    """相邻同向文本碎片合并成一行（总长 ≤ LINE_MAX），媒体行原样独立。纯函数。"""
    out: List[tuple] = []
    for d, line in seq:
        if out and out[-1][0] == d and not _is_media_line(line) and not _is_media_line(out[-1][1]):
            merged = out[-1][1] + MERGE_JOIN + line
            if len(merged) <= LINE_MAX:
                out[-1] = (d, merged)
                continue
        out.append((d, line))
    return out


def build_handoff_note(
    rows: List[Dict[str, Any]], *, since_ts: float, now: Optional[float] = None,
) -> Dict[str, Any]:
    """纯函数：窗口内消息 → ``{note, out_n, in_n, media_n, since, until}``；无内容 → note=""。"""
    ts_now = float(now if now is not None else time.time())
    since = float(since_ts or 0.0)
    if since <= 0 or since < ts_now - MAX_WINDOW_SEC:
        since = ts_now - MAX_WINDOW_SEC
    out_lines: List[str] = []
    in_lines: List[str] = []
    seq: List[tuple] = []            # (direction, line) 时序，用于切出被挤出的前半段
    out_n = in_n = media_n = 0
    first_ts = last_ts = 0.0
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        try:
            ts = float(r.get("ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts and ts < since - 5.0:
            continue
        d = str(r.get("direction") or "in")
        line = _fmt_row(r)
        if not line:
            continue
        first_ts = first_ts or ts
        last_ts = ts or last_ts
        if r.get("media_type") or r.get("media_ref"):
            media_n += 1
        if d == "out":
            out_n += 1
            out_lines.append(line)
        else:
            in_n += 1
            in_lines.append(line)
        seq.append((d, line))
    # 三期：同向连发合并——坐席常把一句话拆三四条发（"好的" / "我看看" / "晚点回你"），
    # 逐字区 8+6 条配额被碎片吃光、真正有信息的话被挤进前半段。相邻同向、拼起来不超一行
    # 的碎片合成一条（媒体行不合并，保持一图一行）。计数 out_n/in_n 仍按原始条数。
    seq = _merge_runs(seq)
    out_lines = [line for d, line in seq if d == "out"]
    in_lines = [line for d, line in seq if d != "out"]
    # 被挤出逐字区的前半段（按各自方向的截断位切）→ LLM 压缩用的 role/content 序列
    overflow_msgs: List[Dict[str, str]] = []
    _o_cut = max(0, len(out_lines) - MAX_OUT_LINES)
    _i_cut = max(0, len(in_lines) - MAX_IN_LINES)
    _oi = _ii = 0
    for d, line in seq:
        if d == "out":
            if _oi < _o_cut:
                overflow_msgs.append({"role": "assistant", "content": line})
            _oi += 1
        else:
            if _ii < _i_cut:
                overflow_msgs.append({"role": "user", "content": line})
            _ii += 1
    if not out_lines and not in_lines:
        return {"note": "", "out_n": 0, "in_n": 0, "media_n": 0, "since": since, "until": ts_now,
                "overflow": [], "overflow_line": ""}
    span = ""
    if first_ts:
        span = f"{_fmt_clock(first_ts)}–{time.strftime('%H:%M', time.localtime(last_ts or ts_now))}"
    head = (
        f"【刚结束的一段人工接管{('（' + span + '）') if span else ''}——以下「你」的话/图是坐席"
        "以你的身份发出的，是你们之间的既定事实：接着聊，不要重新打招呼、不要再问已答过的、"
        "不要否认或忘记这些】"
    )
    # 长接管窗口（二期）：尾部逐字保留，被挤出的前半段不再静默丢弃——先给一行确定性
    # 提要（首尾各一句原话，诚实标注条数），record_handoff 再调 LLM 把这段压成要点版替换。
    out_over = out_lines[:-MAX_OUT_LINES] if len(out_lines) > MAX_OUT_LINES else []
    in_over = in_lines[:-MAX_IN_LINES] if len(in_lines) > MAX_IN_LINES else []
    overflow_line = _overflow_digest(out_over, in_over)
    parts = [head]
    if overflow_line:
        parts.append(overflow_line)
    if out_lines:
        parts.append("你说过/发过：")
        parts += [f"- {x}" for x in out_lines[-MAX_OUT_LINES:]]
    if in_lines:
        parts.append("对方说过/发过：")
        parts += [f"- {x}" for x in in_lines[-MAX_IN_LINES:]]
        if _has_peer_media_desc(rows):
            # Q-36 #318（VV7BRY「Marina」）：接力摘要里对方图的识图描述是画面所见，不是 TA 说的话
            try:
                from src.inbox.image_observation import OBSERVATION_RULE as _obs_rule
            except Exception:
                _obs_rule = "画面描述是识图所见，不是 TA 说的话：不加量词夸大，画面里的地物 / 地名不当作 TA 的住处或专名。"
            parts.append(f"（{_obs_rule}）")
    note = "\n".join(parts)
    if len(note) > NOTE_MAX_CHARS:
        note = note[: NOTE_MAX_CHARS - 1] + "…"
    return {"note": note, "out_n": out_n, "in_n": in_n, "media_n": media_n,
            "since": since, "until": ts_now,
            "overflow": overflow_msgs[:OVERFLOW_MAX_MSGS] if overflow_line else [],
            "overflow_line": overflow_line}


#: 前半段被挤出行数上限（送 LLM 压缩的条数；summarize_conversation 自身只看最近 40 条）
OVERFLOW_MAX_MSGS = 40
OVERFLOW_SUMMARY_CHARS = 260
_OVERFLOW_PREFIX = "更早（本段人工接管前半段）"


def _overflow_digest(out_over: List[str], in_over: List[str]) -> str:
    """被逐字区挤出的前半段 → 一行确定性提要（LLM 要点版到位前的兜底；绝不空手丢弃）。"""
    n = len(out_over) + len(in_over)
    if n <= 0:
        return ""
    bits: List[str] = []
    if out_over:
        bits.append("你提到：" + "；".join(_one_line(x, 36) for x in (out_over[0], out_over[-1])[: (2 if len(out_over) > 1 else 1)]))
    if in_over:
        bits.append("对方提到：" + "；".join(_one_line(x, 36) for x in (in_over[0], in_over[-1])[: (2 if len(in_over) > 1 else 1)]))
    return f"{_OVERFLOW_PREFIX}还有 {n} 条未逐字列出，其中" + "，".join(bits) + "。"


def handoff_note(user_context: Dict[str, Any], *, now: Optional[float] = None) -> str:
    """供 ``_inject_self_state`` 每轮消费：在用/在窗内 → 返回块并计一次使用；否则清掉返 ''。"""
    try:
        if not isinstance(user_context, dict):
            return ""
        rec = user_context.get(NOTE_KEY)
        if not isinstance(rec, dict):
            return ""
        note = str(rec.get("note") or "").strip()
        ts = float(rec.get("ts") or 0.0)
        used = int(rec.get("used") or 0)
        ts_now = float(now if now is not None else time.time())
        if not note or used >= MAX_USES or (ts and ts_now - ts > TTL_SEC):
            user_context.pop(NOTE_KEY, None)
            return ""
        rec["used"] = used + 1
        return note
    except Exception:
        return ""


#: 接力期（note 在用）拟稿时历史窗口的抬高：从库里多取几行 / 逐字保留到接管起点（上限）
HANDOFF_FETCH_MIN = 60
HANDOFF_VERBATIM_CAP = 40


def active_window_since(user_context: Dict[str, Any], *, now: Optional[float] = None) -> float:
    """接力摘要仍在用（未过 TTL / 次数）→ 返回接管窗口起点 ts；否则 0。**不计使用次数**。

    供拟稿链把历史窗口抬到接管起点（P1-1）：接力期内人工那几轮逐字进上下文，而不是
    刚切回就被摘要压缩掉——摘要是 LLM 改写，人工原话才是最可靠的记忆。
    """
    try:
        rec = user_context.get(NOTE_KEY) if isinstance(user_context, dict) else None
        if not isinstance(rec, dict) or not str(rec.get("note") or "").strip():
            return 0.0
        ts = float(rec.get("ts") or 0.0)
        ts_now = float(now if now is not None else time.time())
        if int(rec.get("used") or 0) >= MAX_USES or (ts and ts_now - ts > TTL_SEC):
            return 0.0
        return float(rec.get("since") or 0.0)
    except Exception:
        return 0.0


def fetch_limit_for_handoff(
    app_state: Any, base_limit: int, *, account_id: str, chat_key: str,
    conversation_id: str = "", now: Optional[float] = None,
) -> int:
    """拟稿链取历史行数：接力期内至少 ``HANDOFF_FETCH_MIN``（否则接管稍长人工那几轮就
    不在 30 行窗口里）。任何取不到 ctx 的情况 → 原值。只读 ctx，不计使用次数。"""
    try:
        from src.inbox.human_outbound_memory import (
            _effective_account_id, resolve_skill_manager,
        )
        sm = resolve_skill_manager(app_state)
        get_ctx = getattr(sm, "_get_user_context", None) if sm is not None else None
        if not callable(get_ctx) or not str(chat_key or "").strip():
            return int(base_limit)
        ctx = get_ctx(str(chat_key).strip(),
                      account_id=_effective_account_id(account_id, conversation_id))
        if active_window_since(ctx, now=now) > 0:
            return max(int(base_limit), HANDOFF_FETCH_MIN)
        return int(base_limit)
    except Exception:
        return int(base_limit)


def verbatim_keep_for_handoff(
    history: List[Dict[str, Any]], user_context: Dict[str, Any], base_keep: int,
    *, cap: int = HANDOFF_VERBATIM_CAP, now: Optional[float] = None,
) -> int:
    """接力期内逐字保留条数：覆盖接管起点以来的全部条目（+2 余量），封顶 ``cap``；
    非接力期 / 不足 base → 原值。``history`` 行需带 ``ts``（normalize_history 透传）。"""
    try:
        since = active_window_since(user_context, now=now)
        if since <= 0:
            return int(base_keep)
        n = 0
        for m in history or []:
            if not isinstance(m, dict):
                continue
            try:
                ts = float(m.get("ts") or 0.0)
            except (TypeError, ValueError):
                ts = 0.0
            if ts >= since - 5.0:
                n += 1
        if n <= int(base_keep):
            return int(base_keep)
        return max(int(base_keep), min(n + 2, int(cap)))
    except Exception:
        return int(base_keep)


def note_human_outbound_started(user_context: Dict[str, Any], *, now: Optional[float] = None) -> None:
    """首条人工出站 → 记接管起点（已有且未过期则不动）。``human_outbound_memory`` 调。"""
    try:
        ts_now = float(now if now is not None else time.time())
        cur = float(user_context.get(TAKEOVER_TS_KEY) or 0.0)
        if cur <= 0 or ts_now - cur > MAX_WINDOW_SEC:
            user_context[TAKEOVER_TS_KEY] = ts_now
    except Exception:
        pass


def _condense_enabled(sm: Any) -> bool:
    """``inbox.handoff_memory.condense_with_llm``（默认开）且 ``ai.summarize_with_llm`` 未关。"""
    try:
        cfg = getattr(getattr(sm, "config", None), "config", None) or {}
        if not isinstance(cfg, dict):
            return True
        hm = ((cfg.get("inbox") or {}).get("handoff_memory") or {})
        if not bool(hm.get("condense_with_llm", True)):
            return False
        return bool((cfg.get("ai") or {}).get("summarize_with_llm", True))
    except Exception:
        return True


async def condense_overflow(
    sm: Any, ctx: Dict[str, Any], cstore: Any, *, note_ts: float,
    overflow: List[Dict[str, str]], overflow_line: str,
    timeout_sec: float = 12.0,
) -> str:
    """把接力摘要里被挤出的前半段压成一句要点（``ai_client.summarize_conversation``），
    替换 note 中的确定性提要行并落盘。note 已被换掉/撤下（ts 不符）→ 不动。返回要点（空＝未替换）。"""
    try:
        ai = getattr(sm, "ai_client", None)
        summ = getattr(ai, "summarize_conversation", None) if ai is not None else None
        if not callable(summ) or not overflow or not overflow_line:
            return ""
        s = await summ(overflow, max_chars=OVERFLOW_SUMMARY_CHARS, timeout_sec=timeout_sec)
        s = " ".join(str(s or "").split())
        if not s:
            return ""
        rec = ctx.get(NOTE_KEY)
        if not isinstance(rec, dict) or float(rec.get("ts") or 0.0) != float(note_ts):
            return ""
        note = str(rec.get("note") or "")
        if overflow_line not in note:
            return ""
        new_line = f"{_OVERFLOW_PREFIX}要点：{s[:OVERFLOW_SUMMARY_CHARS]}"
        note = note.replace(overflow_line, new_line, 1)
        if len(note) > NOTE_MAX_CHARS:
            note = note[: NOTE_MAX_CHARS - 1] + "…"
        rec["note"] = note
        rec["condensed"] = True
        key = str(ctx.get("_context_store_key") or "")
        if key and cstore is not None:
            try:
                cstore.mark_dirty(key)
                cstore.flush(key)
            except Exception:
                pass
        logger.info("[handoff] overflow condensed msgs=%d chars=%d", len(overflow), len(s))
        return s
    except Exception:
        logger.debug("[handoff] condense_overflow 失败（保留确定性提要）", exc_info=True)
        return ""


def _schedule_condense(
    sm: Any, ctx: Dict[str, Any], cstore: Any, *, note_ts: float,
    overflow: List[Dict[str, str]], overflow_line: str, app_state: Any = None,
) -> bool:
    """在运行中的事件循环上调度 :func:`condense_overflow`（路由内直接 create_task；
    守卫线程走 ``assistant._web_loop`` 的 run_coroutine_threadsafe）。调度不成＝留提要行。"""
    if not _condense_enabled(sm):
        return False
    import asyncio
    coro_factory = lambda: condense_overflow(  # noqa: E731
        sm, ctx, cstore, note_ts=note_ts, overflow=overflow, overflow_line=overflow_line)
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro_factory())
        return True
    except RuntimeError:
        pass
    except Exception:
        logger.debug("[handoff] condense 调度失败", exc_info=True)
        return False
    try:
        web_loop = None
        for owner in (sm, getattr(app_state, "telegram_client", None),
                      getattr(app_state, "assistant", None)):
            web_loop = getattr(owner, "_web_loop", None) if owner is not None else None
            if web_loop is not None:
                break
        if web_loop is not None and getattr(web_loop, "is_running", lambda: False)():
            asyncio.run_coroutine_threadsafe(coro_factory(), web_loop)
            return True
    except Exception:
        logger.debug("[handoff] condense 跨线程调度失败", exc_info=True)
    return False


def peek_handoff_note(
    app_state: Any, conversation_id: str, *, now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """只读窥视会话的接力笔记（**不计使用次数、不改任何状态**）——供 GET /automation
    的 ``handoff_note`` 段：watchdog 自动接回（by=rearm）没有任何 UI 信号，坐席回到会话
    应看到「AI 已接力 · 记着 N 条」。None＝无 skill_manager / 无笔记 / 已过期用尽。
    """
    try:
        cid = str(conversation_id or "").strip()
        if not cid:
            return None
        from src.inbox.human_outbound_memory import (
            _effective_account_id, resolve_skill_manager,
        )
        sm = resolve_skill_manager(app_state)
        cstore = getattr(sm, "_context_store", None) if sm is not None else None
        platform, acct_raw, chat_key = _split_cid(cid)
        if not chat_key or cstore is None:
            return None
        acct = _effective_account_id(acct_raw, cid)
        # ContextStore.peek：不凭空建 ctx（GET 只读，不能给每次打开会话都造一条空上下文）
        from src.utils.context_store import make_context_key
        peek = getattr(cstore, "peek", None)
        key = make_context_key(chat_key, acct)
        ctx = peek(key) if callable(peek) else None
        if not isinstance(ctx, dict):
            return None
        rec = ctx.get(NOTE_KEY)
        if not isinstance(rec, dict):
            return None
        note = str(rec.get("note") or "").strip()
        ts = float(rec.get("ts") or 0.0)
        used = int(rec.get("used") or 0)
        ts_now = float(now if now is not None else time.time())
        if not note or used >= MAX_USES or (ts and ts_now - ts > TTL_SEC):
            return None
        return {
            "active": True, "ts": ts, "by": str(rec.get("by") or ""),
            "used": used, "remaining": max(0, MAX_USES - used),
            "out_n": int(rec.get("out_n") or 0), "in_n": int(rec.get("in_n") or 0),
            "media_n": int(rec.get("media_n") or 0),
            "condensed": bool(rec.get("condensed")), "note": note,
        }
    except Exception:
        logger.debug("[handoff] peek 失败（忽略）", exc_info=True)
        return None


_SECTION_HEADS = ("你说过/发过：", "对方说过/发过：")


def drop_handoff_line(
    app_state: Any, conversation_id: str, line: str, *, by: str = "",
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """坐席在「看记忆」里删掉一条接力事实（四期）：从 ``_handoff_note`` 的 note 里移除**第一条**
    与 ``line`` 相同的条目行（"- xxx" 或裸 "xxx" 都认）。段落删空则连标题一起去掉；没有任何
    条目/提要剩下 → 整份笔记撤下。返回 ``{ok, active, note, removed}``；绝不抛。

    只动 note 文本，不改 used/ts（不重置使用次数——删一条不该让笔记多活几轮）。
    """
    res: Dict[str, Any] = {"ok": False, "active": False, "note": "", "removed": False}
    try:
        cid = str(conversation_id or "").strip()
        want = str(line or "").strip()
        if want.startswith("- "):
            want = want[2:].strip()
        if not cid or not want:
            res["reason"] = "bad_args"
            return res
        from src.inbox.human_outbound_memory import (
            _effective_account_id, resolve_skill_manager,
        )
        sm = resolve_skill_manager(app_state)
        cstore = getattr(sm, "_context_store", None) if sm is not None else None
        platform, acct_raw, chat_key = _split_cid(cid)
        if not chat_key or cstore is None:
            res["reason"] = "no_skill_manager"
            return res
        from src.utils.context_store import make_context_key
        key = make_context_key(chat_key, _effective_account_id(acct_raw, cid))
        peek = getattr(cstore, "peek", None)
        ctx = peek(key) if callable(peek) else None
        rec = ctx.get(NOTE_KEY) if isinstance(ctx, dict) else None
        if not isinstance(rec, dict) or not str(rec.get("note") or "").strip():
            res["reason"] = "no_note"
            return res
        lines = str(rec["note"]).split("\n")
        idx = next((i for i, ln in enumerate(lines)
                    if ln.strip() == f"- {want}" or ln.strip() == want), -1)
        if idx < 0:
            res.update({"ok": True, "active": True, "note": rec["note"], "reason": "not_found"})
            return res
        del lines[idx]
        # 段落删空 → 去标题（标题后面紧跟另一个标题 / 文末 / 非条目行）
        cleaned: List[str] = []
        for i, ln in enumerate(lines):
            if ln.strip() in _SECTION_HEADS:
                nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
                if not nxt.startswith("- "):
                    continue
            cleaned.append(ln)
        has_body = any(ln.strip().startswith("- ") or ln.startswith(_OVERFLOW_PREFIX) for ln in cleaned)
        ts_now = float(now if now is not None else time.time())
        if not has_body:
            ctx.pop(NOTE_KEY, None)
            res.update({"ok": True, "active": False, "note": "", "removed": True})
        else:
            rec["note"] = "\n".join(cleaned)
            rec["edited_by"] = str(by or "")[:24]
            rec["edited_ts"] = ts_now
            res.update({"ok": True, "active": True, "note": rec["note"], "removed": True})
        try:
            cstore.mark_dirty(key)
            cstore.flush(key)
        except Exception:
            logger.debug("[handoff] 删行持久化失败（忽略）", exc_info=True)
        logger.info("[handoff] line dropped conv=%s by=%s left_active=%s line=%r",
                    cid, by or "-", res["active"], want[:60])
        return res
    except Exception:
        logger.debug("[handoff] drop_handoff_line 异常（已忽略）", exc_info=True)
        res["reason"] = "error"
        return res


def _split_cid(conversation_id: str) -> tuple:
    parts = str(conversation_id or "").split(":", 2)
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    return "", "", ""


def record_handoff(
    app_state: Any, store: Any, conversation_id: str, *,
    prev_meta: Optional[Dict[str, Any]] = None, new_mode: str = "", by: str = "",
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """会话从 manual 交回 AI 时调一次：算接管窗口 → 归纳 → 写 ``_handoff_note`` 持久。

    ``prev_meta``＝切换**前**的 ``get_automation_mode_meta``（{mode, source, updated_at}），
    为 None 时视为不知道（只靠 ctx 的 ``_takeover_started_ts``）。返回
    ``{ok, reason, out_n, in_n, media_n, chars}`` 供日志/测试。绝不抛。
    """
    res: Dict[str, Any] = {"ok": False, "reason": "", "out_n": 0, "in_n": 0, "media_n": 0, "chars": 0}
    try:
        cid = str(conversation_id or "").strip()
        if not cid or store is None:
            res["reason"] = "no_store"
            return res
        if str(new_mode or "") == "manual":
            res["reason"] = "still_manual"
            return res
        from src.inbox.human_outbound_memory import (
            _effective_account_id, resolve_skill_manager,
        )
        sm = resolve_skill_manager(app_state)
        get_ctx = getattr(sm, "_get_user_context", None) if sm is not None else None
        cstore = getattr(sm, "_context_store", None) if sm is not None else None
        if not callable(get_ctx) or cstore is None:
            res["reason"] = "no_skill_manager"
            return res
        platform, acct_raw, chat_key = _split_cid(cid)
        if not chat_key:
            res["reason"] = "bad_cid"
            return res
        acct = _effective_account_id(acct_raw, cid)
        ctx = get_ctx(chat_key, account_id=acct)
        if not isinstance(ctx, dict):
            res["reason"] = "bad_context"
            return res
        ts_now = float(now if now is not None else time.time())
        cands: List[float] = []
        try:
            t0 = float(ctx.get(TAKEOVER_TS_KEY) or 0.0)
            if t0 > 0:
                cands.append(t0)
        except Exception:
            pass
        if isinstance(prev_meta, dict) and str(prev_meta.get("mode") or "") == "manual":
            try:
                t1 = float(prev_meta.get("updated_at") or 0.0)
                if t1 > 0:
                    cands.append(t1)
            except Exception:
                pass
        if not cands:
            # 既没人工出站、也不知道何时切的手动 → 没有可归纳的接管窗口
            ctx.pop(TAKEOVER_TS_KEY, None)
            res["reason"] = "no_window"
            return res
        since = max(min(cands), ts_now - MAX_WINDOW_SEC)
        rows = store.list_recent_messages(cid, limit=MAX_ROWS_SCAN) or []
        built = build_handoff_note(rows, since_ts=since, now=ts_now)
        ctx.pop(TAKEOVER_TS_KEY, None)
        if not built["note"]:
            ctx.pop(NOTE_KEY, None)
            res["reason"] = "empty_window"
        else:
            ctx[NOTE_KEY] = {
                "ts": ts_now, "note": built["note"], "used": 0, "since": since,
                "by": str(by or "")[:24], "out_n": built["out_n"], "in_n": built["in_n"],
                "media_n": built["media_n"],
            }
            res.update({"ok": True, "out_n": built["out_n"], "in_n": built["in_n"],
                        "media_n": built["media_n"], "chars": len(built["note"]),
                        "note": built["note"], "overflow_n": len(built.get("overflow") or [])})
            if built.get("overflow"):
                # 长窗口：前半段交给 LLM 压成要点版（后台、限时；失败留确定性提要）
                res["condense_scheduled"] = _schedule_condense(
                    sm, ctx, cstore, note_ts=ts_now,
                    overflow=list(built["overflow"]),
                    overflow_line=str(built.get("overflow_line") or ""),
                    app_state=app_state)
        key = str(ctx.get("_context_store_key") or "")
        if key:
            try:
                cstore.mark_dirty(key)
                cstore.flush(key)
            except Exception:
                logger.debug("[handoff] 持久化失败（忽略）", exc_info=True)
        if res["ok"]:
            logger.info(
                "[handoff] note built conv=%s by=%s window=%.0fmin out=%d in=%d media=%d chars=%d",
                cid, by or "-", (ts_now - since) / 60.0, res["out_n"], res["in_n"],
                res["media_n"], res["chars"])
            try:
                from src.inbox.automation_mode_stats import record_handoff_note
                record_handoff_note(conversation_id=cid, out_n=res["out_n"],
                                    in_n=res["in_n"], media_n=res["media_n"])
            except Exception:
                pass
        else:
            logger.info("[handoff] no note conv=%s by=%s reason=%s", cid, by or "-", res["reason"])
        return res
    except Exception:
        logger.debug("[handoff] record_handoff 异常（已忽略）", exc_info=True)
        res["reason"] = "error"
        return res


__all__ = [
    "NOTE_KEY", "TAKEOVER_TS_KEY", "MAX_USES", "TTL_SEC", "HANDOFF_FETCH_MIN",
    "HANDOFF_VERBATIM_CAP",
    "active_window_since", "build_handoff_note", "condense_overflow", "drop_handoff_line",
    "handoff_note", "note_human_outbound_started", "peek_handoff_note", "record_handoff",
    "verbatim_keep_for_handoff",
]
