# -*- coding: utf-8 -*-
"""sent_by 历史回填 · 只读预览（不写库、不读消息正文）。

用途：在把存量出站行的 ``messages.sent_by=''`` 回填成五态（agent/ai/phone/script/system）
之前，先看「能推断出多少条、各凭什么推断」。本脚本**没有** ``--apply``，也不产出 UPDATE
语句；真正回填另行评审。

只读保证：
- 所有库都用 ``file:<path>?mode=ro`` 的 URI 打开，并 ``PRAGMA query_only=1``；
- 所有 SQL 都过 :func:`_guard_sql`：引用了正文类列（text/original_text/reply/...）直接抛错；
- 只读 id / 会话 id / 时间戳 / 方向 / 状态 / 坐席 id 这类元数据。

推断规则（按优先级，先命中者为准；每条事件最多认领 1 条消息，取同会话时间最近的）：
  R1 agent_claimed   agent_sends.claimed_mid 精确指向该消息                → agent（强）
  R2 script_account  会话所属账号在脚本测试号名单里（名单与匹配口径同限速闸，见下）  → script（强）
  R3 draft_sent      reply_drafts.sent_at 与消息同会话、时间差 ≤ 窗口：
                     decided_by 为空或含 auto/ai/pilot/system → ai；否则（坐席 id）→ agent
  R4 autoreply_audit autoreply_audit 同会话、时间差 ≤ 窗口、decision 属发送类    → ai
  R5 outreach_sent   outreach_log status='sent' 同会话、时间差 ≤ 窗口          → ai（主动触达）
  R6 agent_window    agent_sends 未认领行（claimed_mid=''）同会话、时间差 ≤ 窗口 → agent（弱）
  R0 no_evidence     以上都没命中                                             → phone（缺省口径）

脚本测试号名单（与限速闸 ``src/compliance/send_rate_gate.script_accounts`` 同一读法）：
  ``--script-accounts``（显式，逗号分隔）> 环境变量 ``CHENGJIE_SCRIPT_SENDER_ACCOUNTS``（设了就整体
  覆盖，空串＝清空）> ``--config`` 给的配置文件里 ``compliance.send_rate_gate.script_accounts``
  （可多次，按顺序深合并，如实例 config.yaml 再叠 config.local.yaml）> 空。
  条目写 ``platform:account_id`` 或纯 ``account_id`` 都认；匹配直接复用限速闸的 ``_match_list``
  （仓库不在旁边时用内置的同口径副本）。输出只报名单条数，不回显账号。

用法（173 上在临时目录里跑，跑完删目录）::

    python preview_sent_by_backfill.py --instance-root D:\\chengjie-instances\\zhiliao
    python preview_sent_by_backfill.py --db path\\to\\unified_inbox.db --json
    python preview_sent_by_backfill.py --instance-root ... --config <实例>\\config.local.yaml
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

DEFAULT_SCRIPT_ACCOUNTS: tuple = ()   # 真实号不入库：命令行或环境变量给
DEFAULT_WINDOW_SEC = 180.0

# 正文/可识别内容列：任何 SQL 引用即拒绝（防手滑把正文读出来）。
FORBIDDEN_COLUMNS = frozenset({
    "text", "original_text", "translated_text", "reply_to_text", "inbound", "reply",
    "peer_text", "draft_text", "final_text", "translated_preview", "note", "last_text",
    "display_name", "sender_name", "media_ref", "reactions_json", "mentions_json",
})

AUTO_DECIDER_RX = re.compile(r"(auto|^ai$|^ai[_:-]|pilot|system|^bot)", re.I)
SEND_DECISIONS = frozenset({"send", "sent", "auto_send", "autosend", "auto", "reply", "replied", "auto_reply"})

RULES = {
    "R1_agent_claimed": ("agent", "agent_sends.claimed_mid 精确指向该消息（坐席发送认领）"),
    "R2_script_account": ("script", "会话账号在脚本测试号名单内（Morgan 确认的脚本测试号；名单同限速闸）"),
    "R3_draft_sent_ai": ("ai", "reply_drafts 已投递(sent_at)且 decided_by 为空/自动类，同会话时间差≤窗口"),
    "R3_draft_sent_agent": ("agent", "reply_drafts 已投递(sent_at)且 decided_by 为坐席 id，同会话时间差≤窗口"),
    "R4_autoreply_audit": ("ai", "autoreply_audit 发送类 decision，同会话时间差≤窗口（协议自动回复）"),
    "R5_outreach_sent": ("ai", "outreach_log status=sent，同会话时间差≤窗口（主动触达）"),
    "R6_agent_window": ("agent", "agent_sends 未认领行，同会话时间差≤窗口（弱证据）"),
    "R0_no_evidence": ("phone", "无任何旁证 → 缺省口径 phone（手机/外部客户端自发）"),
}

_IDENT_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

SCRIPT_ACCOUNTS_ENV = "CHENGJIE_SCRIPT_SENDER_ACCOUNTS"


def _builtin_match_list(items: Iterable, *cands: str) -> bool:
    """限速闸 ``send_rate_gate._match_list`` 的同口径副本（仓库不在旁边时用；测试保证两者一致）。"""
    for it in items or []:
        s = str(it or "").strip()
        if not s:
            continue
        for c in cands:
            c = str(c or "").strip()
            if c and (s == c or (len(s) >= 5 and s in c) or (len(c) >= 5 and c in s)):
                return True
    return False


def _builtin_script_accounts(config) -> list:
    """限速闸 ``send_rate_gate.script_accounts`` 的同口径副本：环境变量 > 配置 > 空。"""
    raw = os.environ.get(SCRIPT_ACCOUNTS_ENV)
    if raw is not None:
        return [x.strip() for x in raw.split(",") if x.strip()]
    try:
        node = ((config or {}).get("compliance") or {}).get("send_rate_gate") or {}
        v = node.get("script_accounts") if isinstance(node, dict) else None
        if isinstance(v, str):
            v = v.split(",")
        return [str(x).strip() for x in (v or []) if str(x or "").strip()]
    except Exception:
        return []


def _gate_funcs():
    """优先复用限速闸本体（同仓库时），否则内置副本。返回 (script_accounts, match_list, 来源)。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.isfile(os.path.join(root, "src", "compliance", "send_rate_gate.py")):
        if root not in sys.path:
            sys.path.insert(0, root)
        try:
            from src.compliance import send_rate_gate as g
            return g.script_accounts, g._match_list, "send_rate_gate"
        except Exception:
            pass
    return _builtin_script_accounts, _builtin_match_list, "builtin"


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in (b or {}).items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(paths: Sequence[str]) -> dict:
    """按顺序读 YAML 并深合并（只取配置结构，不改文件）。"""
    cfg: dict = {}
    for pth in paths or []:
        import yaml  # 惰性：不给 --config 就不需要 PyYAML
        with open(pth, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if isinstance(data, dict):
            cfg = _deep_merge(cfg, data)
    return cfg


def resolve_script_accounts(cli: Optional[str] = None, config: Optional[dict] = None) -> Tuple[list, str]:
    """脚本测试号名单 + 来源（cli / env / config / none）。与限速闸同一读法，显式命令行优先。"""
    if cli is not None:
        return [x.strip() for x in str(cli).split(",") if x.strip()], "cli"
    reader, _m, _src = _gate_funcs()
    lst = list(reader(config if isinstance(config, dict) else {}))
    if os.environ.get(SCRIPT_ACCOUNTS_ENV) is not None:
        return lst, "env"
    return lst, ("config" if lst else "none")


def is_script_conversation(platform: str, account_id: str, accounts: Sequence[str], match=None) -> bool:
    """会话账号是否在名单里：``platform:account_id`` 与纯 ``account_id`` 两种写法都认（同限速闸）。"""
    acct = str(account_id or "").strip()
    if not acct or not accounts:
        return False
    m = match or _gate_funcs()[1]
    return bool(m(accounts, f"{platform or ''}:{acct}", acct))


def _guard_sql(sql: str) -> str:
    """拒绝写语句和正文列。返回原 SQL 便于链式使用。"""
    head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    if head not in ("SELECT", "PRAGMA", "WITH"):
        raise ValueError(f"只读预览只允许 SELECT/PRAGMA：{head}")
    if head == "PRAGMA" and "=" in sql and "query_only" not in sql.lower():
        raise ValueError("不允许写 PRAGMA")
    toks = {t.lower() for t in _IDENT_RX.findall(re.sub(r"'[^']*'", "''", sql))}
    bad = sorted(toks & FORBIDDEN_COLUMNS)
    if bad:
        raise ValueError(f"SQL 引用了正文类列：{bad}")
    return sql


def open_ro(path: str) -> sqlite3.Connection:
    uri = "file:" + os.path.abspath(path).replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.execute("PRAGMA query_only=1")
    return conn


def q(conn: sqlite3.Connection, sql: str, args: Sequence = ()) -> List[tuple]:
    return conn.execute(_guard_sql(sql), tuple(args)).fetchall()


def _tables(conn) -> set:
    return {r[0] for r in q(conn, "SELECT name FROM sqlite_master WHERE type='table'")}


def _cols(conn, table: str) -> set:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        return set()
    return {r[1] for r in q(conn, f"PRAGMA table_info({table})")}


def is_inbox_db(conn) -> bool:
    t = _tables(conn)
    return "messages" in t and "sent_by" in _cols(conn, "messages")


def find_inbox_dbs(root: str) -> List[str]:
    out = []
    for d, _subs, files in os.walk(os.path.join(root, "data")):
        if any(x in d.lower() for x in ("backup", "snapshot", "bak")):
            continue
        for f in files:
            if f.endswith(".db") and ("inbox" in f.lower()):
                out.append(os.path.join(d, f))
    return sorted(out)


class _Matcher:
    """每会话一份按 ts 排序的候选消息；事件认领时间最近且未被认领的 1 条。"""

    def __init__(self, rows: Iterable[Tuple[str, str, float]]):
        self.by_conv: Dict[str, List[Tuple[float, str]]] = defaultdict(list)
        for mid, cid, ts in rows:
            self.by_conv[cid].append((float(ts or 0), mid))
        for v in self.by_conv.values():
            v.sort()
        self.assigned: Dict[str, str] = {}

    def claim(self, cid: str, ts: float, window: float) -> Optional[str]:
        arr = self.by_conv.get(cid)
        if not arr:
            return None
        i = bisect.bisect_left(arr, (ts, ""))
        best, best_d = None, None
        for j in range(max(0, i - 3), min(len(arr), i + 4)):
            t, mid = arr[j]
            if mid in self.assigned:
                continue
            d = abs(t - ts)
            if d <= window and (best_d is None or d < best_d):
                best, best_d = mid, d
        return best


def preview(conn, *, audit_conn=None, script_accounts: Sequence[str] = DEFAULT_SCRIPT_ACCOUNTS,
            window: float = DEFAULT_WINDOW_SEC, since_ts: float = 0.0) -> dict:
    tables = _tables(conn)
    res: dict = {"window_sec": window, "since_ts": since_ts, "notes": []}
    cur = q(conn, "SELECT sent_by, COUNT(*) FROM messages WHERE direction='out' AND ts>=? "
                  "GROUP BY sent_by ORDER BY 2 DESC", (since_ts,))
    res["current_out_by_sent_by"] = {str(k or "(空)"): int(n) for k, n in cur}
    res["total_out"] = sum(res["current_out_by_sent_by"].values())
    cand = q(conn, "SELECT message_id, conversation_id, ts FROM messages "
                   "WHERE direction='out' AND sent_by='' AND ts>=?", (since_ts,))
    res["empty_out"] = len(cand)
    m = _Matcher(cand)
    cand_ids = {r[0] for r in cand}
    hits: Counter = Counter()

    def put(mid, rule):
        if mid and mid in cand_ids and mid not in m.assigned:
            m.assigned[mid] = rule
            hits[rule] += 1

    # R1
    if "agent_sends" in tables and "claimed_mid" in _cols(conn, "agent_sends"):
        for (mid,) in q(conn, "SELECT claimed_mid FROM agent_sends WHERE claimed_mid!=''"):
            put(mid, "R1_agent_claimed")
    else:
        res["notes"].append("agent_sends.claimed_mid 不存在，R1 跳过")
    # R2（名单两种写法都认，匹配口径同限速闸）
    sa = [str(a).strip() for a in script_accounts if str(a).strip()]
    res["script_accounts_n"] = len(sa)
    if sa and "conversations" in tables:
        match = _gate_funcs()[1]
        conv_ids = {str(cid) for cid, plat, acct in
                    q(conn, "SELECT conversation_id, platform, account_id FROM conversations")
                    if is_script_conversation(str(plat or ""), str(acct or ""), sa, match)}
        for cid in conv_ids:
            for _t, mid in m.by_conv.get(cid, []):
                put(mid, "R2_script_account")
    # R3
    deciders: Counter = Counter()
    if "reply_drafts" in tables:
        for cid, sent_at, who in q(conn, "SELECT conversation_id, sent_at, decided_by FROM reply_drafts "
                                         "WHERE sent_at>0 AND sent_at>=?", (since_ts,)):
            who = str(who or "")
            auto = (not who) or bool(AUTO_DECIDER_RX.search(who))
            deciders["(空)" if not who else ("auto类:" + who if auto else "坐席类")] += 1
            mid = m.claim(str(cid), float(sent_at), window)
            put(mid, "R3_draft_sent_ai" if auto else "R3_draft_sent_agent")
    res["draft_deciders"] = dict(deciders.most_common(12))
    # R4
    decisions: Counter = Counter()
    if audit_conn is not None and "autoreply_audit" in _tables(audit_conn):
        for cid, ts, dec in q(audit_conn, "SELECT conversation_id, ts, decision FROM autoreply_audit WHERE ts>=?",
                              (since_ts,)):
            dec = str(dec or "")
            decisions[dec or "(空)"] += 1
            if dec.lower() in SEND_DECISIONS:
                put(m.claim(str(cid), float(ts), window), "R4_autoreply_audit")
    elif audit_conn is None:
        res["notes"].append("未提供 autoreply_audit 库，R4 跳过")
    res["autoreply_decisions"] = dict(decisions.most_common(12))
    # R5
    if "outreach_log" in tables:
        for cid, ts in q(conn, "SELECT conversation_id, ts FROM outreach_log WHERE status='sent' AND ts>=?",
                         (since_ts,)):
            put(m.claim(str(cid), float(ts), window), "R5_outreach_sent")
    # R6
    if "agent_sends" in tables:
        has_claim = "claimed_mid" in _cols(conn, "agent_sends")
        sql = ("SELECT conversation_id, ts FROM agent_sends WHERE ts>=?" +
               (" AND claimed_mid=''" if has_claim else ""))
        for cid, ts in q(conn, sql, (since_ts,)):
            put(m.claim(str(cid), float(ts), min(window, 120.0)), "R6_agent_window")
    hits["R0_no_evidence"] = len(cand) - sum(hits.values())
    by_value: Counter = Counter()
    for rule, n in hits.items():
        by_value[RULES[rule][0]] += n
    res["by_rule"] = {r: {"count": int(hits.get(r, 0)), "sent_by": RULES[r][0], "basis": RULES[r][1]}
                      for r in RULES}
    res["by_value"] = dict(by_value)
    res["inferable"] = int(len(cand) - hits["R0_no_evidence"])
    return res


def _fmt(db: str, r: dict) -> str:
    lines = [f"== {db}", f"出站总数 {r['total_out']}；sent_by 现状 {r['current_out_by_sent_by']}",
             f"待回填（sent_by 空）{r['empty_out']}；有旁证可推断 {r['inferable']}；窗口 {r['window_sec']}s"]
    for k, v in r["by_rule"].items():
        lines.append(f"  {k:<22} → {v['sent_by']:<6} {v['count']:>8}   依据：{v['basis']}")
    lines.append(f"  按取值汇总：{r['by_value']}")
    if "script_accounts_n" in r:
        lines.append(f"  脚本测试号名单 {r['script_accounts_n']} 条（来源 {r.get('script_accounts_source', '-')}，"
                     f"匹配 {r.get('matcher', '-')}）")
    if r.get("draft_deciders"):
        lines.append(f"  reply_drafts 已投递 decided_by 分布：{r['draft_deciders']}")
    if r.get("autoreply_decisions"):
        lines.append(f"  autoreply_audit decision 分布：{r['autoreply_decisions']}")
    for n in r.get("notes") or []:
        lines.append(f"  注：{n}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="sent_by 历史回填只读预览（无 --apply）")
    ap.add_argument("--db", action="append", default=[], help="收件箱库（可多次）")
    ap.add_argument("--instance-root", default="", help="实例根目录，自动找 data/**/*inbox*.db")
    ap.add_argument("--audit-db", default="", help="autoreply_audit.db；缺省在 <root>/data/config/ 下找")
    ap.add_argument("--script-accounts", default=None,
                    help="脚本测试号，逗号分隔；platform:account_id 或 account_id。缺省同限速闸：环境变量 > --config")
    ap.add_argument("--config", action="append", default=[],
                    help="实例配置 YAML（可多次，按顺序深合并），读 compliance.send_rate_gate.script_accounts")
    ap.add_argument("--window", type=float, default=DEFAULT_WINDOW_SEC)
    ap.add_argument("--days", type=float, default=0.0, help="只看最近 N 天（0=全量）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    dbs = list(a.db)
    if a.instance_root:
        dbs += find_inbox_dbs(a.instance_root)
    audit_path = a.audit_db or (os.path.join(a.instance_root, "data", "config", "autoreply_audit.db")
                                if a.instance_root else "")
    audit_conn = open_ro(audit_path) if audit_path and os.path.exists(audit_path) else None
    since = time.time() - a.days * 86400 if a.days > 0 else 0.0
    accounts, acc_src = resolve_script_accounts(a.script_accounts, load_config(a.config))
    out = {}
    for db in dbs:
        try:
            conn = open_ro(db)
        except sqlite3.Error as e:
            out[db] = {"error": str(e)}
            continue
        try:
            if not is_inbox_db(conn):
                out[db] = {"skipped": "无 messages.sent_by"}
                continue
            out[db] = preview(conn, audit_conn=audit_conn, window=a.window, since_ts=since,
                              script_accounts=accounts)
            out[db]["script_accounts_source"] = acc_src
            out[db]["matcher"] = _gate_funcs()[2]
        finally:
            conn.close()
    if audit_conn is not None:
        audit_conn.close()
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        for db, r in out.items():
            print(_fmt(db, r) if "by_rule" in r else f"== {db}: {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
