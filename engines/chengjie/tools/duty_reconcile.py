# -*- coding: utf-8 -*-
"""工单 ↔ B 案台账对账（实施81 L1「自动修复闭环」第一级，2026-08-29）。

背景：报障群工单（bug_intake.db）与修复台账（docs/实施49/64/68 的 B 案表）
是两本账——修完的活登在 B 案里，工单状态却常年停在 new，于是「已修复」回访
一直发不出去。本工具把两本账自动对上：

    python tools/duty_reconcile.py                 # 打印建议清单（只读）
    python tools/duty_reconcile.py --json
    python tools/duty_reconcile.py --min-score 0.22
    python tools/duty_reconcile.py --apply "41=B114"   # 显式逐单批准 → 标
        fixed（fix_note 自动带 B 案摘要）→ 既有自动回访 @报障人（真发群消息！）

判定口径（纯函数，门禁 tests/test_duty_reconcile.py）：
- 匹配＝CJK bigram + 拉丁词 token 集合的 Jaccard 相似度（与 memory_grounding
  同族的零依赖词面法；语义嵌入对这种「同一句事故话术两处记账」场景是杀鸡用牛刀）；
- B 案「已修」判定＝行内显式标记（✅ / 已修 / 已上线 / 已修待发版 / DONE）——
  **没有已修标记的匹配只出「已在跟进」建议，绝不建议标 fixed**（把待开发的案
  说成已修复＝对用户撒谎，比不回访更糟）；
- ``--apply`` 必须显式列 ``工单=B案`` 对（宁繁勿滥：每一条都是真回访消息）。

工具只做「提案 + 显式批准执行」，不做静默批量——匹配是词面近似，最后一眼
永远留给人。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._data_root import resolve_data_roots  # noqa: E402

ENGINE_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = ENGINE_ROOT.parent.parent / "docs"
DEFAULT_LEDGERS = (
    "实施49_内测反馈集中修复与页面优化_四视角方案_2026-08.md",
    "实施64_0823值守报障批次集中修复_1.0.52指令_2026-08.md",
    "实施68_0826未修派工_给117.md",
)
DEFAULT_BASE = "http://127.0.0.1:18799"

_BROW_RE = re.compile(r"^\|\s*(B\d+)\b(.*)$")
_FIXED_MARKERS = ("✅", "已修", "已上线", "已修待发版", "已修复", "DONE",
                  "已热更", "已生效")
# 「已修」标记出现在这些否定语境时不算（宁可漏提案不错提案）
_FIXED_NEGATIONS = ("未修", "没修", "待修", "未解决", "没有解决")


def _out(s: str) -> None:
    sys.stdout.buffer.write((s + "\n").encode("utf-8"))
    sys.stdout.flush()


# ── 纯函数 ───────────────────────────────────────────────────────────────────

def parse_bcase_rows(md_text: str, source: str = "") -> List[Dict[str, Any]]:
    """markdown 里的 B 案表行 → [{bid, text, fixed, source}]。

    同一 B 号多行（追加/复报）合并文本；fixed 任一行命中即真（修复状态通常
    登在最新一行）。
    """
    rows: Dict[str, Dict[str, Any]] = {}
    for line in (md_text or "").splitlines():
        m = _BROW_RE.match(line.strip())
        if not m:
            continue
        bid = m.group(1)
        body = m.group(2).strip().strip("|").strip()
        entry = rows.setdefault(
            bid, {"bid": bid, "text": "", "fixed": False, "source": source})
        entry["text"] = (entry["text"] + " " + body).strip()[:800]
        has_marker = any(k in body for k in _FIXED_MARKERS)
        negated = any(k in body for k in _FIXED_NEGATIONS)
        if has_marker and not negated:
            entry["fixed"] = True
    return list(rows.values())


_LATIN_RE = re.compile(r"[a-z0-9][a-z0-9_./-]{1,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def text_tokens(s: str) -> Set[str]:
    """CJK bigram + 拉丁词 token 集（词面匹配的最小可靠单元）。"""
    s = str(s or "").lower()
    toks: Set[str] = set(_LATIN_RE.findall(s))
    cjk = "".join(_CJK_RE.findall(s))
    toks.update(cjk[i:i + 2] for i in range(len(cjk) - 1))
    return toks


def similarity(a: str, b: str) -> float:
    ta, tb = text_tokens(a), text_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return inter / min(len(ta), len(tb))   # 短句偏置：工单标题短、B 案行长


def propose(tickets: List[Dict[str, Any]], cases: List[Dict[str, Any]], *,
            min_score: float = 0.25) -> List[Dict[str, Any]]:
    """每张开放工单 → 最优 B 案候选。action: mark_fixed | follow_up | none。"""
    out: List[Dict[str, Any]] = []
    for t in tickets or []:
        text = f"{t.get('title') or ''} {t.get('body') or ''}"[:400]
        best: Optional[Dict[str, Any]] = None
        best_score = 0.0
        for c in cases or []:
            sc = similarity(text, c.get("text") or "")
            if sc > best_score:
                best, best_score = c, sc
        if best is None or best_score < min_score:
            out.append({"ticket_id": int(t.get("id") or 0),
                        "title": str(t.get("title") or "")[:60],
                        "action": "none", "score": round(best_score, 3)})
            continue
        action = "mark_fixed" if best.get("fixed") else "follow_up"
        out.append({
            "ticket_id": int(t.get("id") or 0),
            "title": str(t.get("title") or "")[:60],
            "action": action,
            "bid": best["bid"],
            "score": round(best_score, 3),
            "case_excerpt": str(best.get("text") or "")[:160],
            "case_source": best.get("source") or "",
            "suggested_fix_note": (
                f"对应台账 {best['bid']}（{str(best.get('text') or '')[:80]}…）"
                if action == "mark_fixed" else ""),
        })
    return out


def render_proposals(props: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    fixed = [p for p in props if p["action"] == "mark_fixed"]
    follow = [p for p in props if p["action"] == "follow_up"]
    none = [p for p in props if p["action"] == "none"]
    if fixed:
        lines.append(f"🔧 建议标 fixed（{len(fixed)} 条——批准后自动群回访 @报障人）：")
        for p in fixed:
            lines.append(f"  #{p['ticket_id']} {p['title']}")
            lines.append(f"      ↳ {p['bid']}({p['case_source']}, 相似度 {p['score']})："
                         f"{p['case_excerpt'][:100]}")
            lines.append(f"      批准命令：--apply \"{p['ticket_id']}={p['bid']}\"")
    if follow:
        lines.append(f"🚧 已在台账跟进（{len(follow)} 条——建议工单标 confirmed，勿标 fixed）：")
        for p in follow:
            lines.append(f"  #{p['ticket_id']} {p['title']} ↳ {p['bid']}"
                         f"（相似度 {p['score']}，台账未标已修）")
    if none:
        lines.append(f"❓ 台账无匹配（{len(none)} 条——真待修，进自动修复派单/人工）：")
        lines.append("  " + " ".join(f"#{p['ticket_id']}" for p in none))
    return "\n".join(lines) if lines else "（无开放工单）"


# ── 数据读取 / 应用 ──────────────────────────────────────────────────────────

def load_open_tickets(db_path: Path) -> List[Dict[str, Any]]:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM bug_tickets WHERE dup_of=0"
            " AND status IN ('new','confirmed','in_progress')"
            " ORDER BY id DESC LIMIT 200").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def load_ledger_cases(docs_dir: Path = DOCS_DIR) -> List[Dict[str, Any]]:
    cases: List[Dict[str, Any]] = []
    for name in DEFAULT_LEDGERS:
        fp = docs_dir / name
        if not fp.is_file():
            continue
        short = name.split("_")[0]
        cases.extend(parse_bcase_rows(
            fp.read_text(encoding="utf-8", errors="replace"), source=short))
    return cases


def apply_fixed(base: str, token: str, ticket_id: int, fix_note: str) -> tuple:
    body = {"status": "fixed", "fix_note": fix_note}
    req = urllib.request.Request(
        f"{base.rstrip('/')}/api/admin/bug-intake/{int(ticket_id)}/status",
        data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode("utf-8", "replace"))
        return True, resp
    except Exception as e:  # noqa: BLE001
        return False, {"error": str(e)[:200]}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="工单↔B案台账对账（提案默认只读）")
    ap.add_argument("--min-score", type=float, default=0.25)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--data-root", default="")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--apply", default="",
                    help='显式批准，逗号分隔 "41=B114,34=B116"——标 fixed 并'
                         "触发自动群回访（真发消息！）")
    args = ap.parse_args()

    data_root = resolve_data_roots(args.data_root)[0]
    db = Path(data_root) / "config" / "bug_intake.db"
    if not db.is_file():
        _out(f"[skip] 无工单台账（{db}）")
        return 0
    tickets = load_open_tickets(db)
    cases = load_ledger_cases()
    _out(f"[scan] 开放工单 {len(tickets)} · 台账 B 案 {len(cases)}"
         f"（{'/'.join(n.split('_')[0] for n in DEFAULT_LEDGERS)}）")
    props = propose(tickets, cases, min_score=args.min_score)

    if args.json:
        _out(json.dumps(props, ensure_ascii=False, indent=1))
    else:
        _out(render_proposals(props))

    if not args.apply:
        return 0
    # ── 显式批准执行（真发群回访）──
    from tools.duty_reply import read_admin_token
    token = read_admin_token(data_root)
    if not token:
        _out("[err] 未读到 web_admin.auth_token")
        return 1
    by_ticket = {p["ticket_id"]: p for p in props}
    rc = 0
    for pair in [x.strip() for x in args.apply.split(",") if x.strip()]:
        try:
            tid_s, bid = pair.split("=", 1)
            tid = int(tid_s)
        except ValueError:
            _out(f"[err] 批准项格式错：{pair}（应为 41=B114）")
            rc = 1
            continue
        p = by_ticket.get(tid)
        if not p or p.get("action") != "mark_fixed" or p.get("bid") != bid.strip():
            _out(f"[refuse] #{tid}={bid} 不在本轮 mark_fixed 提案里"
                 "（提案与批准必须同轮对得上，防拿旧提案误批）")
            rc = 1
            continue
        ok, resp = apply_fixed(args.base, token, tid, p["suggested_fix_note"])
        _out(f"[apply] #{tid} -> fixed "
             + ("✅ " + json.dumps(resp, ensure_ascii=False)[:120] if ok
                else "❌ " + json.dumps(resp, ensure_ascii=False)[:120]))
        if not ok:
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
