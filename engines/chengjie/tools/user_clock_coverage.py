# -*- coding: utf-8 -*-
"""user_clock 覆盖率盘点（只读）：回答「敢不敢开 companion.user_clock.schedule」。

背景（2026-08-16 聊真实世界 P2）：早晚安/主动触达当前按**服务器时区**择时，海外
客户可能半夜收到「早安」。开 ``user_clock.schedule`` 前需要知道：活跃会话里有多少
能推断出**可用于调度**的时钟（tz_hint 非空＝replace/narrow 档）、推断来源分布、
以及有多少人的偏移与服务器差到「会改变发送时段」的程度。

数据源＝生产 resolver 已落库的 ``conversation_meta.tz_*``（含负结果缓存），本工具
**零重算零写入**（sqlite mode=ro URI）；多实例数据根自动发现（scripts/_data_root
契约，`--data-root` / env AITR_DATA_ROOT 可覆写）。

用法：
    python tools/user_clock_coverage.py [--days 14] [--json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ENGINE_ROOT))
sys.path.insert(0, str(_ENGINE_ROOT / "scripts"))

SERVER_OFFSET_HOURS = 8.0  # 本机部署恒 UTC+8；如迁移由 CLI 参数覆写
SHIFT_MEANINGFUL_HOURS = 3.0  # 偏移差 ≥3h 视为「会实质改变发送时段」
_PHONE_PLATFORMS = ("whatsapp", "wa")  # 与 user_clock_resolver._PHONE_PLATFORMS 同口径


def _recompute_clock(conn: sqlite3.Connection, cid: str, platform: str,
                     chat_key: str, language: str, now_v: float):
    """离线重算该会话时钟（复刻生产 resolver 的信号装配，**缺 stated_place 一路**——
    情景记忆库不在本工具读取范围，覆盖率因此是下界）。失败 → None。"""
    from datetime import datetime, timezone

    from src.companion.user_clock import resolve_user_clock

    phone = ""
    if str(platform or "").strip().lower() in _PHONE_PLATFORMS:
        phone = str(chat_key or "").strip()
    hours: List[int] = []
    try:
        rows = conn.execute(
            "SELECT ts FROM messages WHERE conversation_id=? AND direction='in'"
            " AND ts>0 ORDER BY ts DESC LIMIT 120", (cid,)).fetchall()
        hours = [
            datetime.fromtimestamp(float(t[0]), tz=timezone.utc).hour
            for t in rows
        ]
    except Exception:
        hours = []
    lang = str(language or "").strip()
    if lang.lower() in ("", "unknown"):
        lang = ""
    try:
        return resolve_user_clock(
            stated_place=None, phone=phone, activity_hours=hours,
            language=lang,
            now=datetime.fromtimestamp(now_v, tz=timezone.utc))
    except Exception:
        return None


def collect(db_path: str, *, days: float = 14.0,
            now: Optional[float] = None,
            server_offset: float = SERVER_OFFSET_HOURS,
            recompute: bool = False) -> Dict[str, Any]:
    """单库覆盖率统计；任何一步失败返回 {"error": ...}（工具软失败不抛）。

    ``recompute=False`` 读 resolver 已落库的 ``conversation_meta.tz_*``；
    ``recompute=True`` 离线重算（落库为空时的 dry-run 口径——生产只在
    ``user_clock.schedule`` 开启后才开始落库，先有鸡还是先有蛋由本模式解开）。
    """
    now_v = float(now if now is not None else time.time())
    since = now_v - float(days) * 86400.0
    try:
        conn = sqlite3.connect(
            "file:" + str(Path(db_path)).replace("\\", "/") + "?mode=ro",
            uri=True)
    except Exception as ex:
        return {"error": f"open failed: {ex}", "db": str(db_path)}
    try:
        rows = conn.execute(
            """
            SELECT c.conversation_id, c.platform, c.chat_type, c.peer_is_bot,
                   c.chat_key, c.language,
                   m.tz_hint, m.tz_confidence, m.tz_source, m.tz_country,
                   m.tz_offset, m.tz_resolved_at
              FROM conversations c
              LEFT JOIN conversation_meta m
                     ON m.conversation_id = c.conversation_id
             WHERE c.last_ts >= ?
            """,
            (since,),
        ).fetchall()
    except Exception as ex:
        try:
            conn.close()
        except Exception:
            pass
        return {"error": f"query failed: {ex}", "db": str(db_path)}

    out: Dict[str, Any] = {
        "db": str(db_path), "days": float(days),
        "mode": "recomputed" if recompute else "persisted",
        "total": 0, "private": 0,
        "resolved": 0, "schedulable": 0, "country_only": 0,
        "negative_cached": 0, "never_resolved": 0,
        "shifted_meaningful": 0,
        "by_source": {}, "offsets": {},
    }

    def _bucket_clockish(*, tz_name: str, country: str, source: str,
                         offset: float, schedulable_hint: bool) -> None:
        src = str(source or "") or "?"
        out["by_source"][src] = int(out["by_source"].get(src, 0)) + 1
        if tz_name and schedulable_hint:
            out["schedulable"] += 1
            off = float(offset or 0.0)
            key = (f"UTC{off:+.0f}" if abs(off - round(off)) < 0.25
                   else f"UTC{off:+.1f}")
            out["offsets"][key] = int(out["offsets"].get(key, 0)) + 1
            if abs(off - float(server_offset)) >= SHIFT_MEANINGFUL_HOURS:
                out["shifted_meaningful"] += 1
        elif country:
            out["country_only"] += 1

    try:
        for (cid, platform, chat_type, peer_is_bot, chat_key, language,
             tz_hint, tz_conf, tz_source, tz_country, tz_offset,
             tz_resolved_at) in rows:
            ct = str(chat_type or "").lower()
            # 群/频道/机器人不参与「问候择时」语义（与主动触达候选口径同向）
            if ct in ("group", "supergroup", "channel"):
                continue
            if int(peer_is_bot or 0) == 1:
                continue
            out["total"] += 1
            if ct in ("", "private", "user", "direct"):
                out["private"] += 1

            if recompute:
                clock = _recompute_clock(
                    conn, str(cid), str(platform or ""), str(chat_key or ""),
                    str(language or ""), now_v)
                out["resolved"] += 1  # 重算口径：每个会话都尝试过
                if clock is None:
                    out["negative_cached"] += 1
                    continue
                trust = str(getattr(clock, "trust", "") or "")
                _bucket_clockish(
                    tz_name=str(getattr(clock, "tz_name", "") or ""),
                    country=str(getattr(clock, "country", "") or ""),
                    source=str(getattr(clock, "source", "") or ""),
                    offset=float(getattr(clock, "offset_hours", 0.0) or 0.0),
                    schedulable_hint=trust in ("replace", "narrow"))
                continue

            resolved_at = float(tz_resolved_at or 0)
            if resolved_at <= 0:
                out["never_resolved"] += 1
                continue
            out["resolved"] += 1
            conf = float(tz_conf if tz_conf is not None else -1)
            if conf < 0:
                out["negative_cached"] += 1
                continue
            _bucket_clockish(
                tz_name=str(tz_hint or ""), country=str(tz_country or ""),
                source=str(tz_source or ""), offset=float(tz_offset or 0.0),
                schedulable_hint=True)  # 落库口径：有 tz_hint 即 replace/narrow
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


def render(stats: Dict[str, Any]) -> str:
    if stats.get("error"):
        return f"[{stats.get('db')}] ERROR: {stats['error']}"
    lines: List[str] = []
    total = int(stats["total"]) or 0
    sched = int(stats["schedulable"])

    def _pct(n: int) -> str:
        return f"{(100.0 * n / total):.0f}%" if total else "-"

    mode = ("落库口径" if stats.get("mode") != "recomputed"
            else "离线重算口径（缺 stated_place 信号，覆盖率为下界）")
    lines.append(
        f"== {stats['db']}（近 {stats['days']:.0f} 天活跃私聊 {total} 会话 · {mode}）==")
    lines.append(
        f"  已解析 {stats['resolved']}（{_pct(stats['resolved'])}）"
        f" · 可调度(有明确时区) {sched}（{_pct(sched)}）"
        f" · 仅国家 {stats['country_only']}"
        f" · 推不出 {stats['negative_cached']}"
        f" · 从未解析 {stats['never_resolved']}")
    if stats["by_source"]:
        src = " ".join(f"{k}={v}" for k, v in sorted(
            stats["by_source"].items(), key=lambda kv: -kv[1]))
        lines.append(f"  来源分布: {src}")
    if stats["offsets"]:
        offs = " ".join(f"{k}×{v}" for k, v in sorted(
            stats["offsets"].items(), key=lambda kv: -kv[1]))
        lines.append(f"  时区偏移分布: {offs}")
    lines.append(
        f"  与服务器(UTC+{SERVER_OFFSET_HOURS:.0f})差≥{SHIFT_MEANINGFUL_HOURS:.0f}h"
        f"（开 schedule 会实质改变其发送时段）: {stats['shifted_meaningful']} 会话")
    verdict = (
        "样本足、可调度占比高 → 可以开 schedule 灰度"
        if total and sched >= 10 and sched / max(total, 1) >= 0.3 else
        "可调度占比低/样本少 → 先保持 schedule 关，继续攒行为样本（min_samples=24）")
    lines.append(f"  判词: {verdict}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="user_clock 调度覆盖率盘点（只读）")
    ap.add_argument("--data-root", default="", help="显式数据根（缺省自动发现活跃实例）")
    ap.add_argument("--days", type=float, default=14.0)
    ap.add_argument("--server-offset", type=float, default=SERVER_OFFSET_HOURS)
    ap.add_argument("--recompute", action="store_true",
                    help="强制离线重算（缺省：落库为空时自动切换）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    from _data_root import resolve_data_roots  # scripts/_data_root 契约

    results: List[Dict[str, Any]] = []
    for root in resolve_data_roots(args.data_root):
        db = Path(root) / "config" / "inbox.db"
        if not db.is_file():
            continue
        st = collect(str(db), days=args.days,
                     server_offset=args.server_offset,
                     recompute=bool(args.recompute))
        results.append(st)
        # 落库为空（schedule 未开过 → resolver 从未跑）→ 自动补一轮离线重算，
        # 否则「用数据决定开不开 schedule」永远无数据（先有鸡还是先有蛋）。
        if (not args.recompute and not st.get("error")
                and int(st.get("total") or 0) > 0
                and int(st.get("resolved") or 0) == 0):
            results.append(collect(
                str(db), days=args.days,
                server_offset=args.server_offset, recompute=True))
    if not results:
        print("no inbox.db found under any data root")
        return 1
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for st in results:
            print(render(st))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
