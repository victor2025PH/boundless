# -*- coding: utf-8 -*-
"""报告到达即登记（L-7 A，2026-09-06）——Cursor field-agent 报告自动挂单 / 立单 + 回单号。

为什么：09-05 skuio 传 52 份报告，值守手写脚本只立了 29 份，28 份没人立单也没人回，
他次日 20 条「仍然没有修复」全部由此而来。D-L9 之后 bug 只走 Cursor 报告不贴群，
报告渠道就是唯一立单入口——登记不能再靠人。

规则（工单是主键；群消息与报告都是附件；先挂后立；每条回执带单号）：
  ① note 含 ``#N``                              → 挂 #N（dup 链跟到主单）
  ② note 以 【复现 / 【复验 / 复验 <码> / 补证 / 关联 开头，或含已知报告码
                                                → 挂该码所在单（取证台账 received.diag /
                                                  工单正文 / 该码目录 ticket.txt 三处反查）；
                                                  码指向的单已 fixed / closed 而同报障人 90 分钟内
                                                  有同实体 / 同主题的开放单 → 开放单优先（N-5 D）
  ③ 同一报障人 **90 分钟内**（建单或最近补录）的开放单里，按三档挂「最近那张」（N-5 D，2026-09-08）：
     a. **同实体**：会话 id（platform:acct:peer）/ 长数字 id / ``id=N`` / 客户名（Name(digits)）有交集
     b. **同主题**：note 首句与单标题主题词重叠 ≥ 0.45（L-7 原规则，窗口 30 → 90 分钟）
     c. **引导段带跟进词**：note 前 40 字到第一个冒号 / 】为止含 复查 / 核实 / 补证 / 复核 / 补 <码> / 第 N 次
        → 挂该报障人窗口内最近一张开放单（没有实体、没有主题重叠时的兜底）
  ④ 都不命中                                    → 自动新立单（record_bug_ticket；标题＝note
                                                  首句 ≤80 字，正文全文，reporter_version=app）
  ``*:verify`` 报告不立单（带 #N 时只补一条复验记录）；``【汇总`` 总表不立单，推值守人工。
  挂单时 note 的严重度高于主单 → 抬主单严重度并记 ``auto_severity``（P0 不能埋进 P2 的补录里）。
  一轮里多份新报告**按报告时间排序**处理（09-07 13:23 实锤：MJGHKQ 比它关联的 RN2SKR 先被处理，
  RN2SKR 的单还不存在，MJGHKQ 就按另一个附带提及的旧码挂到了已修的 #232）。

为什么要有 ③（N-5 D）：09-07 skuio 15:31–16:55 就「主动关怀·立即发」一件事连发七份报告
（核实 / 复查 / 第二次复查 / id=1 …），note 首句主题词各不相同、又都没带单号，L-7 的三条规则
各立了一张新单（#243–#249），回执七个单号；09-08 00:51 / 00:53 同一会话 Vanessa×Sinue 的
「补证」又立了 #253。跟进类报告的共同点不是标题像，是**同一个人、同一段时间、同一个实体**。

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
FOLLOWUP_WINDOW_SEC = 90 * 60      # 同报障人跟进窗口（N-5 D；L-7 原 30 分钟只用于主题相似）
TOPIC_WINDOW_SEC = FOLLOWUP_WINDOW_SEC
TOPIC_MIN_SIM = 0.45
TITLE_MAX = 80
SEVERITY_RANK = {"P0": 3, "P1": 2, "P2": 1, "P3": 0}

_CODE_RE = re.compile(r"\b([A-Z0-9]{6})\b")
_TICKET_RE = re.compile(r"#\s*(\d{1,4})\b")
_ATTACH_PREFIX_RE = re.compile(r"^\s*(?:【\s*(?:复现|复验|补证|关联)|复验\s|补证\s|关联\s)")
_SUMMARY_RE = re.compile(r"^\s*【\s*汇总")
_LEAD_TAG_RE = re.compile(r"^\s*【[^】]{1,24}】\s*")
# 跟进词（③c）：只看 note 引导段（前 40 字、到第一个冒号 / 】为止），正文里的「核实：…」不算
_FOLLOWUP_MARK_RE = re.compile(r"复查|核实|补证|复核|补\s*[A-Z0-9]{6}\b|第\s*[一二三四五六七八九十两\d]+\s*次")
_LEAD_SEG_SPLIT_RE = re.compile(r"[：:】]")
# 实体（③a）
_CONV_ID_RE = re.compile(r"\b([a-z]+:\d{5,}:\d{5,})\b")
_LONG_DIGITS_RE = re.compile(r"(?<![\d.])(\d{8,})(?![\d.])")
_DATE_LIKE_RE = re.compile(r"^20[2-3]\d{5}$")            # 20260907 这类日期不是实体
_ID_EQ_RE = re.compile(r"\bid\s*=\s*(\d{1,6})\b")
# 客户名：Name(digits) / Name telegram(digits) / Name(Telegram digits) / Name telegram digits
_NAMED_PEER_RE = re.compile(
    r"([A-Za-z][A-Za-z.'\-]{0,24}(?: [A-Za-z][A-Za-z.'\-]{1,24}){0,2})"
    r"(?:\s*\(|\s+)\s*(?:telegram|whatsapp|messenger|line|tg|wa)?\s*\(?\s*\+?[\d*]{6,}", re.IGNORECASE)
_PLATFORM_WORDS = {"telegram", "whatsapp", "messenger", "line", "tg", "wa", "id", "chat", "conv",
                   "contact", "account", "acct", "mid", "target", "user"}
# 全角标点 / 换行必切；ASCII ?!;. 只在其后是句尾或中文（不是英文引句里的 "kim? are you…"）才切
_SENT_SPLIT_RE = re.compile(r"[。！？；\n]|[!?;.](?=\s*(?:$|[\u4e00-\u9fff【（]))")


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


def followup_marker(note: str) -> str:
    """③c：note 引导段（前 40 字、到第一个冒号 / 】为止）里的跟进词；没有返空串。

    「主动关怀 立即发 复查：15:28…」→ 复查；「核实 13:14 启动时…」→ 核实；「【补 MKYD7D·文件大小
    已确认】…」→ 补 MKYD7D；「主动关怀 立即发 第二次复查(15:46)：…」→ 第二次。正文里的
    「…。核实：当前版本号…」不算（引导段早在第一个冒号前结束）。
    """
    lead = str(note or "").strip()[:40]
    seg = _LEAD_SEG_SPLIT_RE.split(lead, 1)[0]
    m = _FOLLOWUP_MARK_RE.search(seg)
    return m.group(0) if m else ""


def note_entities(text: str) -> Set[str]:
    """③a：文本里可当「同一个实体」的键，全部小写。

    会话 id（telegram:7092595256:6088992099）/ ≥8 位数字 id（电话 / chat id；排除 20260907
    这类日期）/ ``id=N`` / 客户名（Kxhm(Telegram 7092595256) · Kxhm telegram(7092595256) ·
    Vanessa(17345893728)×Sinue(12134989840) · Sinue Alvarez(121****40) → kxhm / vanessa /
    sinue / sinue alvarez）。平台词 / 「id」等泛词不算名字。会话 id 同时贡献它的两段数字。
    """
    s = str(text or "")
    out: Set[str] = set()
    for cid in _CONV_ID_RE.findall(s.lower()):
        out.add(cid)
        out.update(p for p in cid.split(":")[1:] if len(p) >= 8)
    for d in _LONG_DIGITS_RE.findall(s):
        if not _DATE_LIKE_RE.match(d):
            out.add(d)
    for n in _ID_EQ_RE.findall(s):
        out.add(f"id={int(n)}")
    for raw in _NAMED_PEER_RE.findall(s):
        words = [w for w in raw.strip().lower().split() if w not in _PLATFORM_WORDS]
        while words and words[-1] in _PLATFORM_WORDS:
            words.pop()
        name = " ".join(words).strip(" .'-")
        if len(name) >= 2 and not name.isdigit():
            out.add(name)
    return out


def reporter_window_tickets(con: sqlite3.Connection, reporter_id: str, ts: float,
                            window: float = FOLLOWUP_WINDOW_SEC) -> List[Dict[str, Any]]:
    """同报障人、窗口内（建单**或**最近补录）、未并单、未关闭的单，新→旧。"""
    if not reporter_id or not ts:
        return []
    rows = con.execute(
        "SELECT id, status, title, body FROM bug_tickets WHERE chat_id=? AND reporter_id=? AND dup_of=0"
        " AND (ABS(created_ts-?)<=? OR ABS(updated_ts-?)<=?)"
        " AND status NOT IN ('closed','verified') ORDER BY id DESC LIMIT 30",
        (GROUP, reporter_id, ts, window, ts, window)).fetchall()
    return [{"id": int(r[0]), "status": str(r[1] or ""), "title": str(r[2] or ""), "body": str(r[3] or "")}
            for r in rows]


def followup_candidate(con: sqlite3.Connection, reporter_id: str, ts: float,
                       note: str) -> Optional[Dict[str, Any]]:
    """③：同报障人 90 分钟内该挂的那张单——a 同实体 → b 同主题 → c 引导段跟进词；都没有返 None。

    候选按「开放优先 → id 大优先」；返回 {ticket, reason, rank, via}。纯判定，不写库。
    """
    cands = reporter_window_tickets(con, reporter_id, ts)
    if not cands:
        return None

    def _pick(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        return sorted(rows, key=lambda r: (_status_rank(r["status"]), -r["id"]))[0]

    ents = note_entities(note)
    if ents:
        hits = []
        for r in cands:
            shared = ents & note_entities(r["title"] + "\n" + r["body"])
            if shared:
                hits.append((r, shared))
        if hits:
            best, shared = sorted(hits, key=lambda h: (_status_rank(h[0]["status"]), -h[0]["id"]))[0]
            key = sorted(shared, key=len, reverse=True)[0]
            return {"ticket": best["id"], "rank": _status_rank(best["status"]), "via": "entity",
                    "reason": f"90 分钟内同报障人同实体（{key}）单 #{best['id']}"}
    best_t, best_sim = None, 0.0
    for r in cands:
        sim = topic_similarity(str(note or "")[:60], r["title"])
        if sim > best_sim:
            best_t, best_sim = r, sim
    if best_t is not None and best_sim >= TOPIC_MIN_SIM:
        return {"ticket": best_t["id"], "rank": _status_rank(best_t["status"]), "via": "topic",
                "reason": f"90 分钟内同报障人同主题单 #{best_t['id']}（相似度 {best_sim:.2f}）"}
    mark = followup_marker(note)
    if mark:
        best = _pick(cands)
        return {"ticket": best["id"], "rank": _status_rank(best["status"]), "via": "marker",
                "reason": f"跟进类报告「{mark}」→ 90 分钟内同报障人最近单 #{best['id']}"}
    return None


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
    return sorted(resolved, key=lambda t: (_status_rank(resolved[t]), -t))


def _status_rank(status: str) -> int:
    """挂单优先级：开放单 0 → 已修待验 1 → 已验/已关 2（同级 id 大优先）。"""
    s = str(status or "")
    if s in ("closed", "verified"):
        return 2
    if s == "fixed":
        return 1
    return 0


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
    # ③ 的候选先算出来：② 里「码指向已修单」要拿它做对照
    rep = FP_REPORTER.get(str(head.get("fp4") or ""), ("", ""))[0]
    follow = followup_candidate(con, rep, float(head.get("ts") or 0), note)
    # ② 已知报告码（含 复现/复验/补证/关联 前缀的显式意图）。多个码 → 汇总候选单，
    #    开放单优先、再 id 大优先（0906 12:49 实锤：9YYD44 写「关联 ZQ4ASK / 9K6G7W / M2SHYA」，
    #    按首码挂到了已修的 #182，其实是 2 分钟前 M2SHYA 刚立的 #214 的根因补证）
    mentioned = [c for c in dict.fromkeys(_CODE_RE.findall(note)) if c in codes and c != code]
    pool: Dict[int, str] = {}
    for c in mentioned:
        for t in tickets_for_code(con, c, ledger, diag_root):
            pool.setdefault(t, c)
    if pool:
        st = {t: (con.execute("SELECT status FROM bug_tickets WHERE id=?", (t,)).fetchone() or ("",))[0]
              for t in pool}
        best = sorted(pool, key=lambda t: (_status_rank(st[t]), -t))[0]
        # 0907 13:23 实锤（MJGHKQ）：附带提及的旧码全指向已修单，而同报障人 1 分钟前刚立的
        # 开放同题单（RN2SKR → #237）才是它真正说的事 → 开放单优先
        if _status_rank(st[best]) > 0 and follow and follow["rank"] == 0 and follow["via"] != "marker":
            return {"action": "attach", "ticket": follow["ticket"],
                    "reason": f"关联码 {pool[best]} 指向已修/已关 #{best}；{follow['reason']} 优先",
                    "refs": [follow["ticket"]], "via_code": pool[best]}
        return {"action": "attach", "ticket": best,
                "reason": f"关联报告 {pool[best]} → #{best}", "refs": [best],
                "via_code": pool[best]}
    # ③ 同报障人 90 分钟内：同实体 → 同主题 → 引导段跟进词
    if follow:
        return {"action": "attach", "ticket": follow["ticket"], "reason": follow["reason"],
                "refs": [follow["ticket"]], "via": follow["via"]}
    why = "无 #N / 无已知码 / 90 分钟内无同实体·同主题·跟进类单 → 新立单"
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


def _lift_severity(bi, tid: int, code: str, note: str, rep_id: str) -> str:
    """挂单时 note 的严重度高于主单 → 抬主单（只升不降），记 auto_severity 事件；返回新档或空串。"""
    try:
        want = str(bi.classify_severity(note) or "P2")
        con = sqlite3.connect(str(bi._db_path()), timeout=10)
        try:
            row = con.execute("SELECT severity FROM bug_tickets WHERE id=?", (tid,)).fetchone()
            have = str(row[0] if row else "P2")
            if SEVERITY_RANK.get(want, 0) <= SEVERITY_RANK.get(have, 0):
                return ""
            con.execute("UPDATE bug_tickets SET severity=?, updated_ts=? WHERE id=?", (want, time.time(), tid))
            con.commit()
        finally:
            con.close()
        bi.append_ticket_note(tid, f"[值守自动] 严重度 {have} → {want}（报告 {code} 补录）")
        bi._record_event(GROUP, "auto_severity", rep_id, f"#{tid} {have}->{want} ← 报告 {code}")
        return want
    except Exception as exc:  # noqa: BLE001
        log(f"{code} lift severity #{tid} failed: {type(exc).__name__}: {exc}")
        return ""


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
        lifted = _lift_severity(bi, tid, code, note, rep_id)
        _ledger_received(tid, code)
        write_ticket_txt(code, row)
        log(f"{code} attach → #{tid} :: {res['reason']}" + (f" 严重度抬到 {lifted}" if lifted else ""))
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
        _lift_severity(bi, tid, code, note, rep_id)
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


def order_by_report_time(codes: List[str], diag_root: Path = DIAG) -> List[str]:
    """一轮待处理的码按报告 ``time`` 头升序（解析不到的排最后、保持原序）。

    0907 13:23 实锤：MJGHKQ（13:22:28，「关联 … RN2SKR」）排在 RN2SKR（13:21:12）之前被处理，
    当时 RN2SKR 的单还没立，MJGHKQ 只能按另一个附带提及的旧码挂到已修的 #232。
    """
    keyed = []
    for i, c in enumerate(codes):
        h = parse_report_head(c, diag_root)
        ts = float(h.get("ts") or 0) if h else 0.0
        keyed.append((0 if ts else 1, ts, i, c))
    return [k[3] for k in sorted(keyed)]


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
        st["handled"] = list(dict.fromkeys(codes))
        st["inited"] = True
        if not a.dry_run:
            _save_json(STATE, st)
        log(f"水位初始化 = {len(codes)} 个已下载码（不回放历史；历史归属用 --backfill-index 补）")
        return 0
    handled = set(st.get("handled") or [])
    for code in order_by_report_time([c for c in codes if c not in handled]):
        res = auto_ticket(code, dry_run=a.dry_run)
        if res is None and parse_report_head(code) is None:
            continue            # 还没解包完，下一轮
        handled.add(code)
        log(json.dumps(res, ensure_ascii=False) if res else f"{code} → None")
    if not a.dry_run:
        # 与 watch 水位同序、只保留还在水位里的（字母序截尾会挤掉早字母旧码 → 重复立单）
        st["handled"] = [c for c in codes if c in handled]
        _save_json(STATE, st)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
