# -*- coding: utf-8 -*-
"""报告到达即登记（L-7 A，2026-09-06）——Cursor field-agent 报告自动挂单 / 立单 + 回单号。

为什么：09-05 skuio 传 52 份报告，值守手写脚本只立了 29 份，28 份没人立单也没人回，
他次日 20 条「仍然没有修复」全部由此而来。D-L9 之后 bug 只走 Cursor 报告不贴群，
报告渠道就是唯一立单入口——登记不能再靠人。

规则（工单是主键；群消息与报告都是附件；先挂后立；每条回执带单号）：
  ① note 含 ``#N``                              → 挂 #N（dup 链跟到主单）
  ② note 以 【复现 / 【复验 / 复验 <码> / 补证 / 关联 开头，或含已知报告码
                                                → 挂该码所在单（取证台账 received.diag /
                                                  工单正文 / 该码目录 ticket.txt 三处反查）
  ③ 同一报障人 30 分钟内群里已立的单标题与 note 主题词重叠 → 挂那张单
  ④ 都不命中                                    → 自动新立单（record_bug_ticket；标题＝note
                                                  首句 ≤80 字，正文全文，reporter_version=app）
  ``*:verify`` 报告不立单（带 #N 时只补一条复验记录）；``【汇总`` 总表不立单，推值守人工。

每份报告在 ``tmp_diag/<码>/ticket.txt`` 落一行 JSON 记归属（reports_index / 每日对账读它），
并同时写取证台账（duty_evidence.jsonl ``received``）+ bug_events（auto_split / auto_attach）。
回执由 duty_channel_reminder.check_new_reports 发（本模块被它调用，把单号接进同一条
「收到 <码>」回执里；**本模块自己不发消息**）。

用法：python tools/duty_auto_ticket.py --dry-run                 # 处理尚未登记的新报告
      python tools/duty_auto_ticket.py --code E42974 --dry-run   # 单份重跑
      python tools/duty_auto_ticket.py --backfill-index          # 只补历史 ticket.txt（不写库不发消息）
状态：D:\\chengjie-instances\\.ops\\duty_auto_ticket.state.json ；日志同目录 duty_auto_ticket.log
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

OPS = Path(r"D:\chengjie-instances\.ops")
DATA = Path(r"D:\chengjie-instances\zhiliao\data")
DIAG = Path(r"D:\boundless\tmp_diag")
ENGINE = Path(r"D:\boundless\engines\chengjie")
WATCH_STATE = OPS / "duty_watch_loop.state.json"
STATE = OPS / "duty_auto_ticket.state.json"
LOG = OPS / "duty_auto_ticket.log"
GROUP = "-1004345824259"      # 报障群 chat_key（bug_tickets.chat_id 同形）
ACCT = "6834964252"           # 报障群支持号

# 机器指纹前缀 → (reporter_id, reporter_name)；与 bug_tickets 里的实值一致
FP_REPORTER: Dict[str, Tuple[str, str]] = {
    "DC8F": ("8942577244", "skuio 花无缺"),
    "B990": ("8852939166", "钧 JUN"),
}
TOPIC_WINDOW_SEC = 30 * 60
TOPIC_MIN_SIM = 0.45
TITLE_MAX = 80

_CODE_RE = re.compile(r"\b([A-Z0-9]{6})\b")
_TICKET_RE = re.compile(r"#\s*(\d{1,4})\b")
_ATTACH_PREFIX_RE = re.compile(r"^\s*(?:【\s*(?:复现|复验|补证|关联)|复验\s|补证\s|关联\s)")
_SUMMARY_RE = re.compile(r"^\s*【\s*汇总")
_LEAD_TAG_RE = re.compile(r"^\s*【[^】]{1,24}】\s*")
_SENT_SPLIT_RE = re.compile(r"[。！？；;!?\n]|(?<=[^\d])[.](?=\s)")


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        OPS.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        txt = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(txt) > 3000:
            LOG.write_text("\n".join(txt[-2000:]) + "\n", encoding="utf-8")
    except Exception:
        pass
    try:
        sys.stdout.buffer.write((line + "\n").encode("utf-8"))
        sys.stdout.flush()
    except Exception:
        pass


def _load_json(p: Path, default: dict) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return dict(default)


def _save_json(p: Path, obj: dict) -> None:
    try:
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ── 报告头解析 ────────────────────────────────────────────────────────────────

def parse_report_head(code: str, diag_root: Path = DIAG) -> Optional[Dict[str, Any]]:
    """tmp_diag/<码>/*_report*.md 头三行 → {kind, agent, fp4, fp, app, time, ts, note}。

    目录不存在 → None（还没解包完）；无 report md（远程 diag / 自检包）→ kind="diag"。
    """
    d = Path(diag_root) / code
    if not d.is_dir():
        return None
    mds = sorted(d.glob("*_report*.md"))
    if not mds:
        return {"code": code, "kind": "diag", "agent": "", "fp4": "", "fp": "",
                "app": "", "time": "", "ts": 0.0, "note": "", "path": ""}
    md = mds[0]
    lines = md.read_text(encoding="utf-8", errors="replace").splitlines()
    head0 = lines[0] if lines else ""
    m0 = re.search(r"field-agent:([\w-]+):(\w+)", head0)
    agent = m0.group(1) if m0 else ""
    if m0:
        kind = m0.group(2).lower()
    elif "verify" in md.name:
        kind = "verify"
    elif "selfcheck" in md.name or "probe" in md.name:
        kind = "selfcheck"          # 09-02 首装自检 / 值守探针，不是报障
    else:
        kind = "report"
    head1 = lines[1] if len(lines) > 1 else ""
    fp = re.search(r"fp:\s*([A-Z0-9-]{4,})", head1)
    app = re.search(r"app:\s*([\d.]+)", head1)
    tm = re.search(r"time:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", head1)
    ts = 0.0
    if tm:
        try:
            ts = time.mktime(time.strptime(tm.group(1), "%Y-%m-%d %H:%M:%S"))
        except Exception:
            ts = 0.0
    note = next((ln[len("- note:"):].strip() for ln in lines[:8] if ln.startswith("- note:")), "")
    fp_full = fp.group(1) if fp else ""
    return {"code": code, "kind": kind, "agent": agent, "fp4": fp_full[:4], "fp": fp_full,
            "app": app.group(1) if app else "", "time": tm.group(1) if tm else "",
            "ts": ts, "note": note, "path": str(md)}


def known_codes(diag_root: Path = DIAG) -> Set[str]:
    try:
        return {d.name for d in Path(diag_root).iterdir()
                if d.is_dir() and re.fullmatch(r"[A-Z0-9]{6}", d.name)}
    except Exception:
        return set()


def read_ticket_txt(code: str, diag_root: Path = DIAG) -> Optional[Dict[str, Any]]:
    p = Path(diag_root) / code / "ticket.txt"
    if not p.is_file():
        return None
    try:
        first = p.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        return json.loads(first[-1]) if first else None
    except Exception:
        return None


def write_ticket_txt(code: str, row: Dict[str, Any], diag_root: Path = DIAG) -> None:
    d = Path(diag_root) / code
    d.mkdir(parents=True, exist_ok=True)
    with (d / "ticket.txt").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ── 标题 / 相似度（纯函数）──────────────────────────────────────────────────

def title_from_note(note: str, limit: int = TITLE_MAX) -> str:
    """note 首句 ≤limit 字；去掉开头的【标签】（标签留在正文里）。"""
    t = _LEAD_TAG_RE.sub("", str(note or "").strip(), count=1).strip()
    parts = [p.strip() for p in _SENT_SPLIT_RE.split(t) if p and p.strip()]
    first = parts[0] if parts else t
    if len(first) < 12 and len(parts) > 1:      # 首句太短（如「BUG」「复验」）→ 并上第二句
        first = f"{first}：{parts[1]}"
    first = first.strip(" ：:，,")
    return first[:limit].rstrip("，,、 ") if first else str(note or "")[:limit]


_LATIN_RE = re.compile(r"[a-z0-9][a-z0-9_./-]{1,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_STOP_BIGRAMS = {"复现", "复验", "未修", "修复", "仍然", "仍为", "问题", "这个", "没有", "还是",
                 "昨日", "一致", "在1", "复测", "报告", "提交"}


def topic_tokens(s: str) -> Set[str]:
    s = str(s or "").lower()
    toks: Set[str] = set(_LATIN_RE.findall(s))
    cjk = "".join(_CJK_RE.findall(s))
    toks.update(cjk[i:i + 2] for i in range(len(cjk) - 1))
    return {t for t in toks if t not in _STOP_BIGRAMS and not re.fullmatch(r"1\.0\.?\d*", t)}


def topic_similarity(a: str, b: str) -> float:
    ta, tb = topic_tokens(a), topic_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


# ── 反查：报告码 → 工单 ──────────────────────────────────────────────────────

def _ledger_rows(data_root: Path) -> List[Dict[str, Any]]:
    fp = Path(data_root) / "logs" / "duty_evidence.jsonl"
    out: List[Dict[str, Any]] = []
    if not fp.is_file():
        return out
    for ln in fp.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def _resolve_dup(con: sqlite3.Connection, tid: int) -> int:
    """dup_of 链跟到主单；不存在返回 0。"""
    seen = set()
    cur = int(tid or 0)
    while cur and cur not in seen:
        seen.add(cur)
        row = con.execute("SELECT id, dup_of FROM bug_tickets WHERE id=?", (cur,)).fetchone()
        if not row:
            return 0
        if not row[1]:
            return int(row[0])
        cur = int(row[1])
    return cur


def tickets_for_code(con: sqlite3.Connection, code: str, ledger: List[Dict[str, Any]],
                     diag_root: Path = DIAG) -> List[int]:
    """报告码所在的工单（去重、按「未关闭优先 → id 大优先」排）。"""
    ids: Set[int] = set()
    for r in ledger:
        if str(r.get("diag") or "").upper() == code and r.get("ticket"):
            ids.add(int(r["ticket"]))
    for row in con.execute(
            "SELECT id FROM bug_tickets WHERE title LIKE ? OR body LIKE ?",
            (f"%{code}%", f"%{code}%")).fetchall():
        ids.add(int(row[0]))
    tt = read_ticket_txt(code, diag_root)
    if tt and tt.get("ticket"):
        ids.add(int(tt["ticket"]))
    if not ids:
        return []
    q = ",".join("?" for _ in ids)
    rows = con.execute(
        f"SELECT id, status, dup_of FROM bug_tickets WHERE id IN ({q})", list(ids)).fetchall()
    resolved: Dict[int, str] = {}
    for tid, status, dup_of in rows:
        main = _resolve_dup(con, int(dup_of) or int(tid))
        if main:
            st = con.execute("SELECT status FROM bug_tickets WHERE id=?", (main,)).fetchone()
            resolved[main] = str(st[0] if st else status)
    open_first = sorted(resolved, key=lambda t: (resolved[t] in ("closed", "verified"), -t))
    return open_first


def reporter_recent_tickets(con: sqlite3.Connection, reporter_id: str, ts: float,
                            window: float = TOPIC_WINDOW_SEC) -> List[Tuple[int, str]]:
    if not reporter_id or not ts:
        return []
    rows = con.execute(
        "SELECT id, title FROM bug_tickets WHERE chat_id=? AND reporter_id=? AND dup_of=0"
        " AND ABS(created_ts-?)<=? AND status NOT IN ('closed','verified')"
        " ORDER BY id DESC LIMIT 30", (GROUP, reporter_id, ts, window)).fetchall()
    return [(int(r[0]), str(r[1] or "")) for r in rows]


def decide(head: Dict[str, Any], con: sqlite3.Connection, ledger: List[Dict[str, Any]],
           codes: Set[str], diag_root: Path = DIAG) -> Dict[str, Any]:
    """规则 ①②③④ 的纯判定：{action: attach|new|verify|summary|skip, ticket, reason, refs}。"""
    code = head["code"]
    note = str(head.get("note") or "")
    kind = head.get("kind")
    refs = [int(x) for x in _TICKET_RE.findall(note)]
    valid_refs: List[int] = []
    for r in refs:
        main = _resolve_dup(con, r)
        if main and main not in valid_refs:
            valid_refs.append(main)
    if kind == "verify":
        return {"action": "verify", "ticket": valid_refs[0] if valid_refs else 0,
                "reason": "verify 报告不立单" + ("，带 #N 补复验记录" if valid_refs else ""),
                "refs": valid_refs}
    if kind != "report":
        return {"action": "skip", "ticket": 0, "reason": f"kind={kind} 非报告", "refs": []}
    if _SUMMARY_RE.match(note):
        return {"action": "summary", "ticket": 0, "reason": "【汇总】总表不立单，推值守人工", "refs": valid_refs}
    if valid_refs:
        return {"action": "attach", "ticket": valid_refs[0],
                "reason": f"note 带 #{valid_refs[0]}", "refs": valid_refs}
    # ② 已知报告码（含 复现/复验/补证/关联 前缀的显式意图）
    mentioned = [c for c in dict.fromkeys(_CODE_RE.findall(note)) if c in codes and c != code]
    for c in mentioned:
        cands = tickets_for_code(con, c, ledger, diag_root)
        if cands:
            return {"action": "attach", "ticket": cands[0],
                    "reason": f"关联报告 {c} → #{cands[0]}", "refs": [cands[0]],
                    "via_code": c}
    # ③ 同报障人 30 分钟内群里立的单，主题词重叠
    rep = FP_REPORTER.get(str(head.get("fp4") or ""), ("", ""))[0]
    best, best_sim = 0, 0.0
    for tid, title in reporter_recent_tickets(con, rep, float(head.get("ts") or 0)):
        sim = topic_similarity(note[:60], title)
        if sim > best_sim:
            best, best_sim = tid, sim
    if best and best_sim >= TOPIC_MIN_SIM:
        return {"action": "attach", "ticket": best,
                "reason": f"30 分钟内同报障人群消息单 #{best}（相似度 {best_sim:.2f}）",
                "refs": [best]}
    why = "无 #N / 无已知码 / 30 分钟内无同主题单 → 新立单"
    if _ATTACH_PREFIX_RE.match(note):
        why = "写的是复验/关联但码对不上任何单 → 新立单（值守复核）"
    return {"action": "new", "ticket": 0, "reason": why, "refs": []}


# ── 落库 ─────────────────────────────────────────────────────────────────────

def _bi():
    os.environ.setdefault("AITR_DATA_DIR", str(DATA))
    if str(ENGINE) not in sys.path:
        sys.path.insert(0, str(ENGINE))
    from src.ops import bug_intake as bi  # noqa: E402
    p = bi._db_path()
    if not str(p).lower().startswith(str(DATA).lower()):
        raise RuntimeError(f"bug_intake db 落错：{p}")
    return bi


def _ledger_received(tid: int, code: str) -> None:
    if str(ENGINE) not in sys.path:
        sys.path.insert(0, str(ENGINE))
    from tools.duty_evidence import _ledger_row, append_ledger  # noqa: E402
    append_ledger(DATA, _ledger_row(int(tid), "received", diag=code, by="duty_auto_ticket"))


def _stamp(head: Dict[str, Any]) -> str:
    return (f"[报告 {head['code']} {str(head.get('time') or '')[5:16]}"
            f" app {head.get('app') or '?'} {head.get('fp4') or ''}]")


def apply(head: Dict[str, Any], dec: Dict[str, Any], *, dry_run: bool) -> Dict[str, Any]:
    """按判定写工单库 / 取证台账 / ticket.txt；返回给回执链的结果。"""
    code = head["code"]
    note = str(head.get("note") or "")
    act = dec["action"]
    res: Dict[str, Any] = {"code": code, "action": act, "ticket": int(dec.get("ticket") or 0),
                           "is_new": False, "reason": dec.get("reason", ""), "title": ""}
    row = {"code": code, "action": act, "ticket": res["ticket"], "reason": res["reason"],
           "app": head.get("app"), "when": time.strftime("%Y-%m-%d %H:%M:%S"),
           "by": "duty_auto_ticket"}
    if act == "skip":
        return res
    if dry_run:
        log(f"[dry-run] {code} {act} #{res['ticket'] or '-'} :: {res['reason']}"
            + (f" title=「{title_from_note(note)}」" if act == "new" else ""))
        res["title"] = title_from_note(note) if act == "new" else ""
        return res
    bi = _bi()
    rep_id, rep_name = FP_REPORTER.get(str(head.get("fp4") or ""), ("", f"机器 {head.get('fp4')}*"))
    if act == "summary":
        bi._record_event(GROUP, "auto_skip", rep_id, f"{code} 汇总总表，待值守人工核对：{note[:120]}")
        write_ticket_txt(code, row)
        log(f"{code} summary → 不立单，已记事件推值守")
        return res
    if act == "verify":
        if res["ticket"]:
            bi.append_ticket_note(res["ticket"], f"{_stamp(head)} 复验报告：{note[:300]}")
            _ledger_received(res["ticket"], code)
        write_ticket_txt(code, row)
        log(f"{code} verify app={head.get('app')} → #{res['ticket'] or '-'}")
        return res
    if act == "attach":
        tid = res["ticket"]
        bi.append_ticket_note(tid, f"{_stamp(head)} {note[:380]}")
        bi._record_event(GROUP, "auto_attach", rep_id, f"#{tid} ← 报告 {code}（{res['reason']}）")
        _ledger_received(tid, code)
        write_ticket_txt(code, row)
        log(f"{code} attach → #{tid} :: {res['reason']}")
        return res
    # new
    title = title_from_note(note)
    r = bi.record_bug_ticket(chat_id=GROUP, account_id=ACCT, reporter_id=rep_id,
                             reporter_name=rep_name, text=title)
    tid = int(r.get("ticket_id") or 0)
    if not tid:
        log(f"{code} new → record_bug_ticket 失败 {r}")
        res["action"] = "failed"
        return res
    res["ticket"], res["title"] = tid, title
    if r.get("is_new"):
        body = f"{note}\n{_stamp(head)}"[:4000]
        con = sqlite3.connect(str(bi._db_path()), timeout=10)
        try:
            con.execute("UPDATE bug_tickets SET body=?, reporter_version=?, updated_ts=? WHERE id=?",
                        (body, str(head.get("app") or "")[:16], time.time(), tid))
            con.commit()
        finally:
            con.close()
        res["is_new"] = True
        bi._record_event(GROUP, "auto_split", rep_id, f"#{tid} 报告 {code} 自动立单：{title[:100]}")
        log(f"{code} NEW → #{tid} 「{title}」")
    else:
        # 标题与 7 天内开放单相似 → record_bug_ticket 自己归并了，等价挂单
        bi.append_ticket_note(tid, f"{_stamp(head)} {note[:380]}")
        bi._record_event(GROUP, "auto_attach", rep_id, f"#{tid} ← 报告 {code}（标题相似自动归并）")
        res["action"], res["reason"] = "attach", "标题与开放单相似，自动归并"
        log(f"{code} dup-merge → #{tid}")
    _ledger_received(tid, code)
    row.update(action=res["action"], ticket=tid, reason=res["reason"], title=title)
    write_ticket_txt(code, row)
    return res


# ── 对外入口（duty_channel_reminder 调用）────────────────────────────────────

def auto_ticket(code: str, *, dry_run: bool = False) -> Optional[Dict[str, Any]]:
    """处理一份报告：判定 + 落库；已有 ticket.txt 的直接返回既有归属，不重复写。
    任何异常只记日志返回 None（调用方回落到无单号回执）。"""
    try:
        head = parse_report_head(code)
        if head is None:
            return None
        prev = read_ticket_txt(code)
        if prev and prev.get("action") not in (None, "skip", "failed"):
            return {"code": code, "action": prev.get("action"), "ticket": int(prev.get("ticket") or 0),
                    "is_new": prev.get("action") == "new", "reason": "已登记（ticket.txt）",
                    "title": prev.get("title", "")}
        con = sqlite3.connect(f"file:{(DATA / 'config' / 'bug_intake.db').as_posix()}?mode=ro",
                              uri=True, timeout=5)
        try:
            dec = decide(head, con, _ledger_rows(DATA), known_codes())
        finally:
            con.close()
        return apply(head, dec, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        log(f"auto_ticket {code} failed: {type(exc).__name__}: {exc}")
        return None


def receipt_item(code: str, topic: str, res: Optional[Dict[str, Any]]) -> str:
    """回执里一份报告的那一截：「<码>（主题）→ 已立 #N」。"""
    base = f"{code}（{topic}）"
    if not res:
        return base
    act, tid = res.get("action"), int(res.get("ticket") or 0)
    if act == "new" and tid:
        return f"{base}→ 已立 #{tid}"
    if act == "attach" and tid:
        return f"{base}→ 已挂 #{tid}"
    if act == "verify":
        return f"{base}→ 复验记录" + (f"已挂 #{tid}" if tid else "已收")
    if act == "summary":
        return f"{base}→ 总表，值守人工逐项对"
    return base


def backfill_index(dry_run: bool) -> int:
    """历史报告补 ticket.txt：只用既有映射（取证台账 / 工单正文），不写库、不发消息。"""
    con = sqlite3.connect(f"file:{(DATA / 'config' / 'bug_intake.db').as_posix()}?mode=ro",
                          uri=True, timeout=5)
    ledger = _ledger_rows(DATA)
    n = 0
    try:
        for code in sorted(known_codes()):
            if read_ticket_txt(code):
                continue
            head = parse_report_head(code)
            if not head or head["kind"] == "diag":
                continue
            cands = tickets_for_code(con, code, ledger)
            if not cands:
                continue
            row = {"code": code, "action": "index", "ticket": cands[0],
                   "reason": "历史映射回填（取证台账/工单正文）", "app": head.get("app"),
                   "when": time.strftime("%Y-%m-%d %H:%M:%S"), "by": "duty_auto_ticket"}
            n += 1
            if dry_run:
                log(f"[dry-run] index {code} → #{cands[0]}" + (f"（另 {cands[1:]}）" if len(cands) > 1 else ""))
            else:
                write_ticket_txt(code, row)
    finally:
        con.close()
    log(f"backfill_index: {n} 份{'（dry-run）' if dry_run else ''}")
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="报告到达自动挂单/立单（单轮）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--code", default="", help="只处理这一份（忽略已登记状态时配 --force）")
    ap.add_argument("--force", action="store_true", help="配 --code：忽略既有 ticket.txt 重新判定")
    ap.add_argument("--attach", type=int, default=0,
                    help="配 --code：值守人工指定挂到 #N（每日对账「未挂单」的处置口，跳过自动判定）")
    ap.add_argument("--backfill-index", action="store_true",
                    help="历史报告只补 ticket.txt（既有映射），不写库不发消息")
    a = ap.parse_args()
    OPS.mkdir(parents=True, exist_ok=True)
    if a.backfill_index:
        backfill_index(a.dry_run)
        return 0
    if a.code:
        code = a.code.strip().upper()
        if a.force or a.attach:
            head = parse_report_head(code)
            if head is None:
                log(f"{code} 目录不存在")
                return 1
            con = sqlite3.connect(f"file:{(DATA / 'config' / 'bug_intake.db').as_posix()}?mode=ro",
                                  uri=True, timeout=5)
            try:
                if a.attach:
                    main_id = _resolve_dup(con, a.attach)
                    if not main_id:
                        log(f"#{a.attach} 不存在")
                        return 1
                    dec = {"action": "attach", "ticket": main_id, "refs": [main_id],
                           "reason": f"值守人工指定挂 #{main_id}（每日对账处置）"}
                else:
                    dec = decide(head, con, _ledger_rows(DATA), known_codes())
            finally:
                con.close()
            res = apply(head, dec, dry_run=a.dry_run)
        else:
            res = auto_ticket(code, dry_run=a.dry_run)
        log(json.dumps(res, ensure_ascii=False))
        return 0
    # 无参数：处理 watch_loop 已下载、尚未登记的报告（首跑只立水位，不回放历史）
    st = _load_json(STATE, {"handled": [], "inited": False})
    watch = _load_json(WATCH_STATE, {})
    codes: List[str] = list(watch.get("codes") or [])
    if not st.get("inited"):
        st["handled"] = sorted(set(codes))[-400:]
        st["inited"] = True
        if not a.dry_run:
            _save_json(STATE, st)
        log(f"水位初始化 = {len(codes)} 个已下载码（不回放历史；历史归属用 --backfill-index 补）")
        return 0
    handled = set(st.get("handled") or [])
    for code in codes:
        if code in handled:
            continue
        res = auto_ticket(code, dry_run=a.dry_run)
        if res is None and parse_report_head(code) is None:
            continue            # 还没解包完，下一轮
        handled.add(code)
        log(json.dumps(res, ensure_ascii=False) if res else f"{code} → None")
    if not a.dry_run:
        st["handled"] = sorted(handled)[-400:]
        _save_json(STATE, st)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
