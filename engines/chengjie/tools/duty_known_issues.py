# -*- coding: utf-8 -*-
"""「已知问题与修复」周公示生成器（实施81 P1-3，2026-08-28）。

内测群同一个坑多人各报一遍的根因：系统内部有查重归并，但**用户侧看不见**
「哪些已知、哪些在修、哪些已修好」。本工具从工单台账生成一段群公示文案：

    python tools/duty_known_issues.py                 # 打印（只读）
    python tools/duty_known_issues.py --days 7
    python tools/duty_known_issues.py --send neice    # 真发进群（显式 opt-in）
    python tools/duty_known_issues.py --send neice --via account  # 强制旧链

数据面＝bug_intake.db 只读连接（sqlite ro URI，house 惯例：绝不对活体生产库
开写事务）。发送身份（实施82 P0，0829 老板拍板）默认 **官方 bot**——但必须
先探测引擎已装载「bot 自咬环守卫」（GET /api/admin/bug-intake 的 ``bot_guard``
旗）：守卫未装载（引擎待重启）时 bot 发的公示会被登记链当成新报障，此时
**拒发并指路**，绝不带病发送；``--via account`` 走旧用户账号链。
建议节奏：每周一早值守跑一次 --send；平时报障回复里可直接引用编号。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._data_root import resolve_data_roots  # noqa: E402


def _out(s: str) -> None:
    sys.stdout.buffer.write((s + "\n").encode("utf-8"))
    sys.stdout.flush()


# ── 纯函数（门禁 tests/test_duty_known_issues.py）────────────────────────────

_SECTION_CAP = 10   # 每节最多列几条（超出折叠成「…等 N 项」——公示不是台账转储）


def _clean_title(raw: Any) -> str:
    """标题净化：压掉换行/连空格（VLM 图片摘要常携带多行），截 60 字。

    2026-08-28 真实台账实测：不少工单标题是「文字 + [图片内容] 类型=B…」
    多行拼接，原样进公示会把「· #N 标题」的行版式打碎。
    """
    t = " ".join(str(raw or "").split())
    return t[:60]


def compose_digest(rows: List[Dict[str, Any]], *, days: int,
                   now: float) -> str:
    """工单行 → 群公示文案。空账本返回空串（没有内容就不该发）。

    分组：🔧 已修复（fixed/verified，窗口内更新，带修复说明）/
    🚧 处理中（confirmed/in_progress）/ 🆕 新报待确认（new，窗口内）。
    closed 不进公示（已闭环旧账）；窗口外的 new 也不进（陈年积压刷 40 行
    ＝没人看，2026-08-28 真实台账实测）；多人报告标 ×N。
    """
    since = now - days * 86400
    fixed: List[str] = []
    doing: List[str] = []
    fresh: List[str] = []
    skipped_fresh = 0
    for r in rows or []:
        st = str(r.get("status") or "")
        title = _clean_title(r.get("title"))
        if not title:
            continue
        tid = int(r.get("id") or 0)
        n = int(r.get("report_count") or 1)
        tag = f" ×{n}" if n > 1 else ""
        line = f"· #{tid} {title}{tag}"
        in_window = float(r.get("updated_ts") or 0) >= since
        if st in ("fixed", "verified"):
            if not in_window:
                continue                      # 修好很久的旧账不再刷屏
            note = str(r.get("fix_note") or "").strip()
            if note:
                line += f"（{note[:60]}）"
            if st == "fixed":
                line += "——请报障的朋友验证"
            fixed.append(line)
        elif st in ("confirmed", "in_progress"):
            doing.append(line)
        elif st == "new":
            if in_window:
                fresh.append(line)
            else:
                skipped_fresh += 1
    if not (fixed or doing or fresh):
        return ""

    def _capped(lines: List[str]) -> List[str]:
        if len(lines) <= _SECTION_CAP:
            return lines
        rest = len(lines) - _SECTION_CAP
        return lines[:_SECTION_CAP] + [f"  …等 {rest} 项（详见工单处置台）"]

    day = time.strftime("%m-%d", time.localtime(now))
    parts = [f"📋 已知问题与修复进展（{day}，近 {days} 天）"]
    if fixed:
        parts.append("🔧 已修复：")
        parts.extend(_capped(fixed))
    if doing:
        parts.append("🚧 处理中：")
        parts.extend(_capped(doing))
    if fresh:
        parts.append("🆕 新报待确认：")
        parts.extend(_capped(fresh))
    parts.append("—— 已在列的问题不用重复报；有新细节（版本/步骤/截图）欢迎补充，直接 @我。")
    return "\n".join(parts)


def load_rows(db_path: Path) -> List[Dict[str, Any]]:
    """只读拉非 closed 主单（dup 已归并主单，report_count 是聚合值）。

    ``SELECT *`` 而非点名列：``fix_note`` 列由引擎侧幂等迁移在**重启后**才加，
    本 CLI 只读连接不做迁移——点名列会在「代码已更新、引擎未重启」的窗口期
    对老 schema 直接崩（2026-08-28 实弹抓到）；compose_digest 按 .get 消费，
    新旧 schema 同一行为。
    """
    uri = f"file:{db_path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM bug_tickets WHERE dup_of=0 AND status != 'closed'"
            " ORDER BY id DESC LIMIT 200").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="已知问题与修复周公示")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--data-root", default="")
    ap.add_argument("--send", default="",
                    help="真发进群（neice|official；默认只打印）")
    ap.add_argument("--via", default="bot", choices=("bot", "account"),
                    help="发送身份：bot=官方 bot（默认，需引擎 bot_guard 已装载）"
                         "；account=支持号旧链")
    args = ap.parse_args()

    data_root = resolve_data_roots(args.data_root)[0]
    db = Path(data_root) / "config" / "bug_intake.db"
    if not db.is_file():
        _out(f"[skip] 无工单台账（{db}）")
        return 0
    try:
        rows = load_rows(db)
    except Exception as exc:  # noqa: BLE001
        _out(f"[err] 台账读取失败：{exc}")
        return 1
    text = compose_digest(rows, days=args.days, now=time.time())
    if not text:
        _out("[ok] 无可公示条目（空账本/全部 closed）")
        return 0
    _out(text)
    if not args.send:
        _out("\n[hint] 发进群：python tools/duty_known_issues.py --send neice")
        return 0

    from tools.duty_reply import (
        build_client_msg_id, build_send_payload, read_admin_token,
        resolve_group,
    )
    import urllib.request

    chat_key = resolve_group(args.send)
    token = read_admin_token(data_root)
    if not token:
        _out("[err] 未读到 web_admin.auth_token")
        return 1

    if args.via == "bot":
        # ① 能力探测：引擎必须已装载 bot 自咬环守卫（否则 bot 发的公示会被
        #    登记链当新报障——宁可拒发，绝不带病发送）
        guard = False
        try:
            req0 = urllib.request.Request(
                "http://127.0.0.1:18799/api/admin/bug-intake?limit=1",
                headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req0, timeout=15) as r0:
                guard = bool(json.loads(
                    r0.read().decode("utf-8", "replace")).get("bot_guard"))
        except Exception as exc:  # noqa: BLE001
            _out(f"[err] 引擎能力探测失败：{str(exc)[:120]}")
            return 1
        if not guard:
            _out("[refuse] 引擎尚未装载 bot 自咬环守卫（待重启窗装载后即可）"
                 "——现在用 bot 发公示会被登记链当成新报障。"
                 "临时可 --via account 走旧链。")
            return 1
        # ② bot 直发（纯文本公示；token 从 notify_webhooks.json 读）
        nw = Path(data_root) / "config" / "notify_webhooks.json"
        btok = ""
        try:
            for ch in json.loads(nw.read_text(encoding="utf-8")):
                if str(ch.get("format")) == "telegram" and ch.get("token"):
                    btok = str(ch["token"])
                    break
        except Exception:
            btok = ""
        if not btok:
            _out(f"[err] 未读到 bot token（{nw}）")
            return 1
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{btok}/sendMessage",
            data=json.dumps({"chat_id": int(chat_key), "text": text,
                             "disable_web_page_preview": True}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode("utf-8", "replace"))
        ok = bool(resp.get("ok"))
        _out(f"[send] via=bot {'ok' if ok else 'FAIL ' + str(resp)[:120]}")
        return 0 if ok else 1

    payload = build_send_payload(
        chat_key, text, account_id="6834964252",
        client_msg_id=build_client_msg_id())
    req = urllib.request.Request(
        "http://127.0.0.1:18799/api/unified-inbox/send",
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.loads(r.read().decode("utf-8", "replace"))
    ok = bool(resp.get("ok", True))
    _out(f"[send] via=account {'ok' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
