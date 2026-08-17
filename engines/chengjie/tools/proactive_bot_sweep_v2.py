# -*- coding: utf-8 -*-
r"""消息级历史 bot 清扫 V2（P1 2026-08-03）——扫 messages 回填 peer_is_bot。

背景：``peer_bot_guard`` 的入站守卫 + 存量 sweep 只看**会话行级**信号
（chat_type / username 后缀 / is_bot 平台真值）。但生产 30% 的 Telegram 私聊
``username`` 空缺、``chat_type`` 常缺 → 一批 bot 会话行级检不出（实锤：神马搜索
``username=''`` + ``chat_type='private'``，行级三信号全空）。它们靠 **messages
历史**才认得出：我方早期发过 ``/start`` 查询（真人绝不对真人发斜杠命令）、对方
秒回广告/菜单话术。本工具补这个「消息级」维度，把躺在候选池里的历史地雷洗出来。

判定同源：复用 ``peer_bot_guard`` 的 ``conversation_row_is_bot`` /
``heuristic_bot_score``（纯函数，只读依赖，绝不改那个活跃文件），新增本工具独有的
「我方发过命令」强信号。落库走直接 SQL（明确可控，与语义等价于
``store.set_peer_bot_verdict`` + ``set_automation_mode``）。

用法（默认只读预扫，--apply 才写库）：
    python tools/proactive_bot_sweep_v2.py                 # 只读，全实例
    python tools/proactive_bot_sweep_v2.py --apply         # 回填 + 降 manual
    python tools/proactive_bot_sweep_v2.py --data-root D:\...\data --apply
    python tools/proactive_bot_sweep_v2.py --min-suspect 0.6

可逆：工具写的 peer_is_bot=1 / manual 与 UI「标记为机器人」同语义，运营在工作台
徽章点「确认真人 / 恢复自动判定」即可撤销；--apply 尊重运营覆写(-1)与已 manual/review。
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts._data_root import resolve_data_roots  # noqa: E402
from src.inbox.peer_bot_guard import (  # noqa: E402
    conversation_row_is_bot,
    heuristic_bot_score,
    is_media_placeholder,
)

# 我方出站斜杠命令：真人对真人绝不发（/start /help /menu…）。纯命令 token
# （无空格、字母开头），排除「发了个含 / 的路径/网址」误判。高置信 bot 信号。
_CMD_RE = re.compile(r"^/[a-zA-Z][a-zA-Z0-9_]{1,30}$")


def outbound_command_count(messages: List[Dict[str, Any]]) -> int:
    """我方出站中「纯斜杠命令」条数（去媒体占位符）。"""
    n = 0
    for m in messages or []:
        if str(m.get("direction") or "") != "out":
            continue
        raw = str(m.get("original_text") or m.get("text") or "").strip()
        if not raw or is_media_placeholder(raw):
            continue
        if _CMD_RE.match(raw):
            n += 1
    return n


def classify(
    row: Dict[str, Any],
    messages: List[Dict[str, Any]],
    *,
    min_suspect: float,
) -> Tuple[str, str, float]:
    """综合会话行 + 消息历史 → (verdict, evidence, score)。

    verdict ∈ {bot, suspect, none}。所有命中信号都汇进 evidence（即便某条已判
    bot，也报 command 命中——用于验证「消息级信号本可独立抓住它」）。
    """
    signals: List[str] = []
    is_bot = False

    if conversation_row_is_bot(row):
        signals.append("tier0(行级)")
        is_bot = True

    cmds = outbound_command_count(messages)
    if cmds > 0:
        signals.append(f"我方发过命令×{cmds}")
        is_bot = True   # 高置信：真人不对真人发 /start

    score, h_ev = heuristic_bot_score(
        messages,
        display_name=str(row.get("display_name") or ""),
        username=str(row.get("username") or ""))
    if h_ev:
        signals.append(h_ev)

    # 运营已覆写「确认真人」(-1) → 尊重，绝不回判 bot（行级信号除外由
    # conversation_row_is_bot 内部已把 -1 判 False）
    try:
        override = int(row.get("peer_is_bot") or 0)
    except (TypeError, ValueError):
        override = 0
    if override == -1:
        return ("none", "运营覆写=真人", score)

    if is_bot:
        return ("bot", "; ".join(signals), score)
    if score >= min_suspect:
        return ("suspect", "; ".join(signals) or f"score={score:.2f}", score)
    return ("none", "; ".join(signals), score)


def _recent_messages(conn: sqlite3.Connection, cid: str, limit: int = 120):
    rows = conn.execute(
        "SELECT direction, original_text, text, ts FROM messages "
        "WHERE conversation_id=? ORDER BY ts DESC LIMIT ?",
        (cid, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]   # 还原时间升序（判定器口径）


def sweep_root(data_root, *, apply: bool, min_suspect: float,
               limit: int) -> Dict[str, int]:
    db = os.path.join(str(data_root), "config", "inbox.db")
    if not os.path.isfile(db):
        print(f"  [skip] 无 inbox.db: {db}")
        return {}
    uri = f"file:{db}" + ("" if apply else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        convs = [dict(r) for r in conn.execute(
            "SELECT * FROM conversations WHERE platform='telegram' "
            "ORDER BY last_ts DESC LIMIT ?", (limit,))]
    except Exception as e:
        print(f"  [err] 读会话失败: {e}")
        conn.close()
        return {}

    stat = {"bot": 0, "suspect": 0, "applied_flag": 0, "applied_mode": 0,
            "applied_score": 0}
    now = time.time()
    if apply:
        conn.execute("BEGIN IMMEDIATE")
    for c in convs:
        cid = str(c.get("conversation_id") or "")
        if not cid:
            continue
        msgs = _recent_messages(conn, cid)
        verdict, ev, score = classify(c, msgs, min_suspect=min_suspect)
        if verdict == "none":
            continue
        name = str(c.get("display_name") or c.get("chat_key") or "")[:20]
        already = int(c.get("peer_is_bot") or 0) == 1
        tag = "BOT" if verdict == "bot" else f"SUSPECT({score:.2f})"
        flag_note = " [已标记]" if already and verdict == "bot" else ""
        print(f"  {tag:<14} {cid:<38} {name:<20}{flag_note}  {ev}")
        stat[verdict] = stat.get(verdict, 0) + 1

        if not apply:
            continue
        if verdict == "bot":
            if not already:
                conn.execute(
                    "UPDATE conversations SET peer_is_bot=1, bot_evidence=? "
                    "WHERE conversation_id=? AND COALESCE(peer_is_bot,0)!=-1",
                    (f"sweep_v2: {ev}"[:200], cid))
                stat["applied_flag"] += 1
            # 降 manual（尊重已 manual/review；缺行则插 manual）
            cur = conn.execute(
                "SELECT automation_mode FROM conversation_settings "
                "WHERE conversation_id=?", (cid,)).fetchone()
            if cur is None:
                conn.execute(
                    "INSERT INTO conversation_settings"
                    "(conversation_id, automation_mode, updated_at) "
                    "VALUES(?, 'manual', ?)", (cid, now))
                stat["applied_mode"] += 1
            elif cur[0] not in ("manual", "review"):
                conn.execute(
                    "UPDATE conversation_settings SET automation_mode='manual', "
                    "updated_at=? WHERE conversation_id=?", (now, cid))
                stat["applied_mode"] += 1
        elif verdict == "suspect" and not already:
            # 疑似只落分/证据，不强制降档（人审）
            conn.execute(
                "UPDATE conversations SET bot_score=?, bot_evidence=? "
                "WHERE conversation_id=? AND COALESCE(peer_is_bot,0)=0",
                (score, f"sweep_v2(suspect): {ev}"[:200], cid))
            stat["applied_score"] += 1
    if apply:
        conn.commit()
    conn.close()
    return stat


def main() -> int:
    ap = argparse.ArgumentParser(description="消息级历史 bot 清扫 V2")
    ap.add_argument("--data-root", default="", help="显式实例数据根（默认自动发现）")
    ap.add_argument("--apply", action="store_true", help="回填库（默认只读预扫）")
    ap.add_argument("--min-suspect", type=float, default=0.6,
                    help="疑似阈值（默认 0.6，与 peer_bot_guard 一致）")
    ap.add_argument("--limit", type=int, default=2000, help="每实例扫描会话上限")
    args = ap.parse_args()

    roots = resolve_data_roots(args.data_root)
    print(f"=== sweep V2 {'(APPLY 写库)' if args.apply else '(只读预扫)'} "
          f"数据根 {len(roots)} 个 ===")
    total = {"bot": 0, "suspect": 0, "applied_flag": 0, "applied_mode": 0,
             "applied_score": 0}
    for root in roots:
        print(f"\n[{os.path.basename(os.path.dirname(str(root)))}] {root}")
        st = sweep_root(root, apply=args.apply, min_suspect=args.min_suspect,
                        limit=args.limit)
        for k, v in st.items():
            total[k] = total.get(k, 0) + v
    print(f"\n汇总: bot={total['bot']} suspect={total['suspect']}"
          + (f" | 已回填 flag={total['applied_flag']} "
             f"降manual={total['applied_mode']} 落分={total['applied_score']}"
             if args.apply else "（只读，加 --apply 才写库）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
