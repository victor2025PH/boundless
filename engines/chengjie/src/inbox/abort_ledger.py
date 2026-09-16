# -*- coding: utf-8 -*-
"""拦截台账（Q-18 D #293，2026-09-11）：把「AI 为什么没回」的两类日志落成可见数据。

来源只有两处既有日志点（不新造判定）：
- ``[autosend] abort=<code> stage=batch|presend|deferred``（autosend_worker 人工优先闸 /
  让位等待中被人接）；
- ``[needs_human] 打标 … reason=<reason> category=<cat>``（protocol_autoreply.tag_needs_human，
  含 adult / risk_grader 三级 / privacy / commitment…）。

存储：InboxStore ``app_settings`` KV 单键 :data:`KEY`，JSON 数组**滚动 200 条**（不建表）。
行结构 ``{ts, conv, code, stage, hit, source, draft_id, reason}``；``code`` 归一到
:data:`REASONS`（``agent_send`` → ``agent_sent``，``mode_switch / mode_downgraded`` →
``mode_changed``，``adult:*`` / category=adult → ``adult``，其余打标 → ``needs_human``）。

消费：回复设置页「今日拦截」卡（``GET /api/reply-settings/abort-ledger`` → :func:`summary`：
24h 按原因码计数 + 最近 5 条）、``tools/why_no_reply.py``（只读 SQLite 直接解 JSON →
:func:`rows_for_conv`）。写入 best-effort：任何异常只 debug 日志，绝不影响发送 / 打标主流程。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

KEY = "abort_ledger:v1"
MAX_ROWS = 200
# 「今日拦截」卡的原因码表（顺序 = 卡片展示顺序）
REASONS = ("adult", "risk_hold", "needs_human", "agent_sent", "agent_typing",
           "mode_changed", "work_schedule", "dup_suppressed")
_ALIASES = {
    "agent_send": "agent_sent",
    "mode_switch": "mode_changed",
    "mode_downgraded": "mode_changed",
    "high_risk": "needs_human",
}
_lock = threading.Lock()


def normalize_code(code: Any, *, category: str = "") -> str:
    """原因码归一：``adult:pressure`` / category=adult → adult；别名表；未知码原样（小写、≤32）。"""
    c = str(code or "").strip().lower()
    cat = str(category or "").strip().lower()
    if cat == "adult" or c.startswith("adult"):
        return "adult"
    if c.startswith("abort:"):
        c = c[6:]
    c = _ALIASES.get(c, c)
    if c in REASONS:
        return c
    if c.startswith("risk:") or c.startswith("risk_hold"):
        return "risk_hold"
    if c in ("needs_human", "handoff", "review_required") or cat:
        return "needs_human"
    return c[:32] or "unknown"


def _load(store: Any) -> List[Dict[str, Any]]:
    try:
        raw = store.get_app_setting(KEY, "")
    except Exception:
        return []
    return parse_rows(raw)


def parse_rows(raw: Any) -> List[Dict[str, Any]]:
    """把 KV 原文（JSON 数组字串）解成行列表；坏数据 → 空表（不抛）。"""
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict)]


def record(store: Any, *, conversation_id: str, code: str, stage: str = "",
           hit: Any = "", source: str = "autosend", draft_id: str = "",
           reason: str = "", category: str = "", ts: Optional[float] = None,
           detail: str = "") -> bool:
    """追加一行（滚动 200）。store 缺席 / 无 KV 能力 → False。绝不抛。

    ``detail``（R87 #331）：给人看的一行补充（如「与 07:39 那条相近 · 相似度 0.87 · 已重写 2 次」），
    「今日拦截」卡第二行灰字；原码只在 title。"""
    if store is None or not hasattr(store, "get_app_setting") or not hasattr(store, "set_app_setting"):
        return False
    cid = str(conversation_id or "")
    if not cid:
        return False
    if isinstance(hit, (list, tuple)):
        hit = ", ".join(str(h) for h in hit if str(h))
    row = {
        "ts": float(ts if ts is not None else time.time()),
        "conv": cid,
        "code": normalize_code(code, category=category),
        "stage": str(stage or "")[:16],
        "hit": str(hit or "")[:80],
        "source": str(source or "")[:16],
        "draft_id": str(draft_id or "")[:40],
        "reason": str(reason or code or "")[:48],
    }
    if detail:   # R87 #331
        row["detail"] = str(detail).replace("\n", " ")[:120]
    try:
        with _lock:
            rows = _load(store)
            rows.append(row)
            if len(rows) > MAX_ROWS:
                rows = rows[-MAX_ROWS:]
            store.set_app_setting(KEY, json.dumps(rows, ensure_ascii=False), updated_by="abort_ledger")
        return True
    except Exception:
        logger.debug("[abort_ledger] 写入失败 conv=%s code=%s（忽略）", cid, code, exc_info=True)
        return False


def annotate_last(store: Any, *, conversation_id: str, reason: str, hit: str = "",
                  detail: str = "", draft_id: str = "", within_sec: float = 900.0,
                  now: Optional[float] = None) -> bool:
    """R87 #331：给某会话**最近一条**同 ``reason`` 的行补 ``hit`` / ``detail``（打标钩子写行时
    拿不到相似度 / 对照句，dup 拦截处事后补）。找不到（或超 ``within_sec``）→ False。绝不抛。"""
    if store is None or not hasattr(store, "get_app_setting") or not hasattr(store, "set_app_setting"):
        return False
    cid = str(conversation_id or "")
    want = str(reason or "")
    if not cid or not want:
        return False
    _now = float(now if now is not None else time.time())
    try:
        with _lock:
            rows = _load(store)
            for r in reversed(rows):
                if str(r.get("conv") or "") != cid or str(r.get("reason") or "") != want:
                    continue
                if _now - float(r.get("ts") or 0.0) > float(within_sec):
                    return False
                if hit:
                    r["hit"] = str(hit)[:80]
                if detail:
                    r["detail"] = str(detail).replace("\n", " ")[:120]
                if draft_id and not r.get("draft_id"):
                    r["draft_id"] = str(draft_id)[:40]
                store.set_app_setting(KEY, json.dumps(rows, ensure_ascii=False), updated_by="abort_ledger")
                return True
        return False
    except Exception:
        logger.debug("[abort_ledger] annotate 失败 conv=%s（忽略）", cid, exc_info=True)
        return False


def rows(store: Any) -> List[Dict[str, Any]]:
    """全部行（旧 → 新）。"""
    if store is None or not hasattr(store, "get_app_setting"):
        return []
    return _load(store)


def rows_for_conv(all_rows: Iterable[Dict[str, Any]], conversation_id: str, *,
                  now: Optional[float] = None, window_h: float = 24.0) -> List[Dict[str, Any]]:
    """纯函数：某会话窗口内的行（新 → 旧）。why_no_reply CLI 直接喂 parse_rows 的结果。"""
    _now = float(now if now is not None else time.time())
    lo = _now - float(window_h) * 3600.0
    cid = str(conversation_id or "")
    out = [r for r in all_rows if str(r.get("conv") or "") == cid and float(r.get("ts") or 0.0) >= lo]
    out.sort(key=lambda r: float(r.get("ts") or 0.0), reverse=True)
    return out


def summary(store: Any, *, now: Optional[float] = None, window_h: float = 24.0,
            last_n: int = 5) -> Dict[str, Any]:
    """「今日拦截」卡数据：``{window_h, total, counts:{code:n}（含 0 的全部 REASONS + 出现过的其他码）,
    recent:[…最近 last_n 条，新 → 旧], since}``。"""
    _now = float(now if now is not None else time.time())
    lo = _now - float(window_h) * 3600.0
    all_rows = rows(store)
    win = [r for r in all_rows if float(r.get("ts") or 0.0) >= lo]
    counts: Dict[str, int] = {c: 0 for c in REASONS}
    for r in win:
        c = normalize_code(r.get("code"))
        counts[c] = counts.get(c, 0) + 1
    win.sort(key=lambda r: float(r.get("ts") or 0.0), reverse=True)
    recent = [{
        "ts": float(r.get("ts") or 0.0),
        "conv": str(r.get("conv") or ""),
        "code": normalize_code(r.get("code")),
        "stage": str(r.get("stage") or ""),
        "hit": str(r.get("hit") or ""),
        "reason": str(r.get("reason") or ""),
        "source": str(r.get("source") or ""),
        "detail": str(r.get("detail") or ""),        # R87 #331：人话细节（卡片第二行）
        "draft_id": str(r.get("draft_id") or ""),    # R87 #331
    } for r in win[:max(0, int(last_n))]]
    return {"window_h": float(window_h), "since": lo, "total": len(win),
            "counts": counts, "recent": recent, "capacity": MAX_ROWS, "stored": len(all_rows)}


__all__ = ["KEY", "MAX_ROWS", "REASONS", "normalize_code", "parse_rows", "record",
           "annotate_last",   # R87 #331
           "rows", "rows_for_conv", "summary"]
