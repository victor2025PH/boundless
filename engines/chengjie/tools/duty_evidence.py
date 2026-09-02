# -*- coding: utf-8 -*-
"""值守取证台账 CLI（值守循环 v3，2026-09-02）——「日志前置 + 版本钉死」的工具化。

背景（0902 复盘）：多单陷入「修了→用户说还没修好」循环，三个断点全在取证侧：
① 日志没到手就给根因定性（抢答）；② 代码分析没钉用户版本（最新源码的结论
盖到 1.067 的机器上）；③ 「没修好」和「没升级」在回访时无法区分。
本工具把 v3 的 B/C/D/G 四条纪律收口成一个参数化命令：

    python tools/duty_evidence.py check 145         # 分类+版本+证据状态+定性门槛
    python tools/duty_evidence.py backfill          # 全库回填 reporter_version
    python tools/duty_evidence.py set-version 139 1.0.67
    python tools/duty_evidence.py request 145 --what "诊断包：媒体发送失败+克隆声"
    python tools/duty_evidence.py received 144 --diag dr-1a0604f9836
    python tools/duty_evidence.py list              # 在途证据与超时档（30m/2h/24h）

行为：
1. ``reporter_version`` 列不存在时自动 ALTER TABLE 补列（与 notify_ts 当年
   同一加列模式；引擎 INSERT 是显式列名，加列零破坏）。
2. 版本抽取只认「版本/version/v 前缀 + 1.0.NN / 1.0NN」形态，统一成
   ``1.0.NN`` 规范形；时间戳（0:06）、机器码（B990-…）绝不误吞。
3. 工单分类**保守优先**：命中任何「行为链路」关键词即判「必须日志」
   （无日志禁止定性）；只命中「出站内容」关键词才判「图即证据」；
   两边都不命中按「必须日志」。
4. 证据台账 JSONL 落 ``<数据根>/logs/duty_evidence.jsonl``，重放得状态；
   超时刻度 30 分钟/2 小时/24 小时与 v2③ 追问规则同刻度。

只读写工单库与自家台账，不发消息、不碰引擎运行态。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._data_root import resolve_data_roots  # noqa: E402

# ── 纯函数（门禁 tests/test_duty_evidence.py）─────────────────────────────────

#: 「版本」邻近提及（最可信）：版本: 1.067 / version 1.0.68 / v1.0.69
_VER_NEAR_RE = re.compile(
    r"(?:版本|version|ver|[vV])\s*[:：=]?\s*1\.0\.?(\d{2})\b")
#: 裸提及兜底：1.0.68 / 1.068（要求两位小版本，避开 1.5、0:06 这类）
_VER_BARE_RE = re.compile(r"\b1\.0\.?(\d{2})\b")
#: body 里非报障人内容的起始标记：值守回复回写（bug_intake_routes L292）。
#: 0902 实锤：回复里的「修复在 v1.0.68」被抽成报障人版本——报障人的原话
#: 永远在第一个值守标记之前，抽取一律先截断。
_NON_REPORTER_MARKERS = ("[值守回复]", "[值守]")


def reporter_text(body: str) -> str:
    """工单 body → 报障人自述部分（截到第一个值守回写标记）。"""
    t = str(body or "")
    idx = min((i for i in (t.find(m) for m in _NON_REPORTER_MARKERS)
               if i >= 0), default=-1)
    return t[:idx] if idx >= 0 else t


def extract_version(text: str) -> str:
    """从工单正文抽**报障人**版本 → 规范形 ``1.0.NN``；抽不到返空串。

    先截掉值守回写段（回写里的修复版本号不是用户版本）；剩余文本里
    「版本」邻近提及优先于裸提及；多个裸提及取**第一个**（正文开头通常是
    用户自报的产品信息块，越靠后越可能是转述）。
    """
    t = reporter_text(text)
    m = _VER_NEAR_RE.search(t)
    if not m:
        m = _VER_BARE_RE.search(t)
    return f"1.0.{int(m.group(1))}" if m else ""


def version_at_least(have: str, need: str) -> Optional[bool]:
    """``have >= need``；任一边非规范形返回 None（未知，按未知处置）。"""
    def _key(v: str) -> Optional[Tuple[int, ...]]:
        p = str(v or "").strip().lstrip("vV").split(".")
        try:
            return tuple(int(x) for x in p) if len(p) == 3 else None
        except ValueError:
            return None
    a, b = _key(have), _key(need)
    if a is None or b is None:
        return None
    return a >= b


#: 行为链路类（必须日志才可定性）——命中任何一个即 must_log，保守优先。
_MUST_LOG_KEYWORDS = (
    "失败", "发不出", "发不了", "不回复", "没有自动回复", "没有回复", "不自动回复",
    "收不到", "没收到", "连不上", "掉线", "断线", "崩溃", "闪退", "卡死", "卡住",
    "超时", "很慢", "克隆", "标准声", "换声", "没声音", "无声",
    "未读", "徽标", "同步", "登录", "登不上", "悬死", "白屏", "打不开",
)
#: 出站内容类（截图即完整现场，可直接进代码分析）。
_IMAGE_EVIDENCE_KEYWORDS = (
    "贬低", "评论", "嘲笑", "笑话", "说起", "话术", "称呼", "人称", "自称",
    "切中文", "切换为中文", "切换成中文", "蹦中文", "语言", "翻译",
    "泄漏", "泄露", "承诺", "编造", "穿帮", "配文", "内心独白", "旁白",
)

CLASS_MUST_LOG = "must_log"
CLASS_IMAGE = "image_evidence"

CLASS_LABEL = {
    CLASS_MUST_LOG: "必须日志（无日志禁止给根因定性）",
    CLASS_IMAGE: "图即证据（截图=完整现场，可直接代码分析）",
}


def classify_ticket(text: str) -> Tuple[str, List[str]]:
    """工单文本 → (分类, 命中词)。保守优先：must_log 命中即 must_log；
    仅命中内容词才 image_evidence；全不中回落 must_log。"""
    t = str(text or "")
    must = [k for k in _MUST_LOG_KEYWORDS if k in t]
    if must:
        return CLASS_MUST_LOG, must
    img = [k for k in _IMAGE_EVIDENCE_KEYWORDS if k in t]
    if img:
        return CLASS_IMAGE, img
    return CLASS_MUST_LOG, []


#: 超时刻度（秒）→ 档名；与 v2③ 追问规则同刻度。
_OVERDUE_TIERS = ((24 * 3600, "24h+：停止追问，按保守处理挂「证据不足」"),
                  (2 * 3600, "2h+：第二次追问 + 启动替代取证"),
                  (30 * 60, "30m+：第一次轻追问（带理由）"))


def overdue_tier(requested_ts: float, now: Optional[float] = None) -> str:
    age = float(now if now is not None else time.time()) - float(requested_ts)
    for sec, label in _OVERDUE_TIERS:
        if age >= sec:
            return label
    return ""


def replay_ledger(rows: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """台账事件重放 → {ticket: {state, requested_ts, what, diag, received_ts,
    class_override}}。同一工单多轮请求以最后一轮为准；received 关闭在途态；
    set_class 是人工分类覆写（启发式误判时的修正通道，最后一次为准）。"""
    state: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        tid = int(r.get("ticket") or 0)
        if not tid:
            continue
        ev = str(r.get("event") or "")
        s = state.setdefault(tid, {})
        if ev == "request":
            s.update(state="pending", requested_ts=float(r.get("ts") or 0),
                     what=str(r.get("what") or ""), diag="", received_ts=0.0)
        elif ev == "received":
            s.update(state="received", diag=str(r.get("diag") or ""),
                     received_ts=float(r.get("ts") or 0))
        elif ev == "set_class":
            v = str(r.get("value") or "")
            if v in (CLASS_MUST_LOG, CLASS_IMAGE):
                s["class_override"] = v
    return state


def evidence_gate(cls: str, evidence_state: str, *, ack: bool = False,
                  ticket: int = 0) -> Tuple[bool, str]:
    """定性回复的发送门禁（v3 §B 的机器强制）→ (放行?, 说明)。

    must_log 类且证据未到 → 拦；``ack``（--ack-no-evidence）显式放行但
    带说明（调用方落台账审计）。图即证据类 / 证据已到 → 无声放行。
    """
    if cls != CLASS_MUST_LOG or evidence_state == "received":
        return True, ""
    if ack:
        return True, (f"#{ticket} 为「必须日志」类且证据未到——已显式放行"
                      "（--ack-no-evidence，进台账审计）")
    return False, (
        f"#{ticket} 是「必须日志」类且证据未到手（v3 §B：无日志禁止根因定性）。"
        "受理回执/追问用 --receipt；确要定性加 --ack-no-evidence（落台账自担）。")


def ticket_send_gate(data_root: Any, ticket: int, *,
                     ack: bool = False) -> Tuple[bool, str]:
    """duty_reply 发送前调用的门禁入口。**软失败**：工单库/台账任何异常一律
    放行——取证台账绝不能反过来瘫痪值守发送通道。"""
    try:
        root = Path(data_root)
        con = sqlite3.connect(str(_db_path(root)))
        try:
            row = con.execute(
                "select title, body from bug_tickets where id=?",
                (int(ticket),)).fetchone()
        finally:
            con.close()
        if not row:
            return True, ""
        st = replay_ledger(read_ledger(root)).get(int(ticket)) or {}
        cls = (st.get("class_override")
               or classify_ticket(f"{row[0]}\n{row[1]}")[0])
        return evidence_gate(cls, str(st.get("state") or ""),
                             ack=ack, ticket=int(ticket))
    except Exception:
        return True, ""


# ── 库/台账 IO ────────────────────────────────────────────────────────────────

def _db_path(data_root: Path) -> Path:
    return Path(data_root) / "config" / "bug_intake.db"


def _ledger_path(data_root: Path) -> Path:
    return Path(data_root) / "logs" / "duty_evidence.jsonl"


def ensure_reporter_version_column(con: sqlite3.Connection) -> bool:
    """缺列则补（幂等）；返回是否本次新增。"""
    cols = [c[1] for c in con.execute("pragma table_info(bug_tickets)")]
    if "reporter_version" in cols:
        return False
    con.execute("ALTER TABLE bug_tickets ADD COLUMN"
                " reporter_version TEXT NOT NULL DEFAULT ''")
    con.commit()
    return True


def backfill_versions(con: sqlite3.Connection) -> List[Tuple[int, str]]:
    """对 reporter_version 为空的工单从 body 抽版本回填；返回 [(id, ver)]。"""
    ensure_reporter_version_column(con)
    done: List[Tuple[int, str]] = []
    rows = con.execute("select id, body from bug_tickets"
                       " where reporter_version=''").fetchall()
    for tid, body in rows:
        ver = extract_version(body)
        if ver:
            con.execute("update bug_tickets set reporter_version=? where id=?",
                        (ver, int(tid)))
            done.append((int(tid), ver))
    con.commit()
    return done


def append_ledger(data_root: Path, row: Dict[str, Any]) -> None:
    fp = _ledger_path(data_root)
    fp.parent.mkdir(parents=True, exist_ok=True)
    with fp.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_ledger(data_root: Path) -> List[Dict[str, Any]]:
    fp = _ledger_path(data_root)
    if not fp.is_file():
        return []
    out: List[Dict[str, Any]] = []
    for line in fp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _ledger_row(ticket: int, event: str, **extra: Any) -> Dict[str, Any]:
    ts = time.time()
    row = {"ts": round(ts, 3),
           "when": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
           "ticket": int(ticket), "event": event}
    row.update({k: v for k, v in extra.items() if v})
    return row


# ── CLI ───────────────────────────────────────────────────────────────────────

def _p(s: str = "") -> None:
    sys.stdout.buffer.write((s + "\n").encode("utf-8"))


def cmd_check(con: sqlite3.Connection, data_root: Path, ticket: int) -> int:
    ensure_reporter_version_column(con)
    row = con.execute(
        "select id, status, severity, reporter_name, reporter_version,"
        " title, body from bug_tickets where id=?", (ticket,)).fetchone()
    if not row:
        _p(f"[err] 工单 #{ticket} 不存在")
        return 1
    tid, status, sev, name, ver, title, body = row
    ver = ver or extract_version(body)
    st = replay_ledger(read_ledger(data_root)).get(int(tid))
    cls_auto, hits = classify_ticket(f"{title}\n{body}")
    override = str((st or {}).get("class_override") or "")
    cls = override or cls_auto
    _p(f"#{tid} [{status}/{sev}] {name}  版本={ver or '未知'}")
    _p(f"标题：{str(title)[:80]}")
    _p(f"分类：{CLASS_LABEL[cls]}" + ("（人工覆写）" if override else ""))
    _p(f"命中：{'、'.join(hits) if hits else '（无关键词，按保守档）'}")
    if st and st.get("state") == "pending":
        tier = overdue_tier(st["requested_ts"])
        _p(f"证据：在途（{st.get('what') or '未注明'}）"
           + (f" ⚠ {tier}" if tier else ""))
    elif st and st.get("state") == "received":
        _p(f"证据：已到（{st.get('diag')}）")
    else:
        _p("证据：未登记（must_log 类先 request 再定性）")
    if cls == CLASS_MUST_LOG and not (st and st.get("state") == "received"):
        _p("→ 定性门槛未满足：只发受理回执/要包，不给根因定性（v3 §A/§B）")
    if not ver:
        _p("→ 版本未知：回访话术必须走「先升级」分支（v3 §G）")
    return 0


def cmd_list(data_root: Path) -> int:
    st = replay_ledger(read_ledger(data_root))
    pending = {t: s for t, s in st.items() if s.get("state") == "pending"}
    if not pending:
        _p("无在途证据请求")
        return 0
    for tid, s in sorted(pending.items()):
        tier = overdue_tier(s["requested_ts"])
        when = time.strftime("%m-%d %H:%M", time.localtime(s["requested_ts"]))
        _p(f"#{tid} 自 {when} 在途：{s.get('what') or '未注明'}"
           + (f" ⚠ {tier}" if tier else ""))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="值守取证台账（v3 §B/§C/§D/§G）")
    ap.add_argument("--data-root", default="")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check"); p.add_argument("ticket", type=int)
    sub.add_parser("backfill")
    p = sub.add_parser("set-version")
    p.add_argument("ticket", type=int); p.add_argument("version")
    p = sub.add_parser("request")
    p.add_argument("ticket", type=int); p.add_argument("--what", default="")
    p = sub.add_parser("received")
    p.add_argument("ticket", type=int); p.add_argument("--diag", default="")
    p = sub.add_parser("set-class")
    p.add_argument("ticket", type=int)
    p.add_argument("value", choices=[CLASS_MUST_LOG, CLASS_IMAGE])
    sub.add_parser("list")
    a = ap.parse_args(argv)

    data_root = resolve_data_roots(a.data_root)[0]
    dbp = _db_path(data_root)
    if not dbp.is_file():
        _p(f"[err] 工单库不存在：{dbp}")
        return 1
    con = sqlite3.connect(str(dbp))
    try:
        if a.cmd == "check":
            return cmd_check(con, data_root, a.ticket)
        if a.cmd == "backfill":
            ensure_reporter_version_column(con)
            done = backfill_versions(con)
            _p(f"回填 {len(done)} 单：" + " ".join(f"#{t}={v}" for t, v in done)
               if done else "无可回填（全部已有版本或抽不到）")
            return 0
        if a.cmd == "set-version":
            ensure_reporter_version_column(con)
            con.execute("update bug_tickets set reporter_version=? where id=?",
                        (str(a.version), a.ticket))
            con.commit()
            _p(f"#{a.ticket} reporter_version={a.version}")
            return 0
        if a.cmd == "request":
            append_ledger(data_root,
                          _ledger_row(a.ticket, "request", what=a.what))
            _p(f"#{a.ticket} 证据请求已登记（{a.what or '未注明'}）")
            return 0
        if a.cmd == "received":
            append_ledger(data_root,
                          _ledger_row(a.ticket, "received", diag=a.diag))
            _p(f"#{a.ticket} 证据到手（{a.diag or '未注明'}）")
            return 0
        if a.cmd == "set-class":
            append_ledger(data_root,
                          _ledger_row(a.ticket, "set_class", value=a.value))
            _p(f"#{a.ticket} 分类覆写 → {CLASS_LABEL[a.value]}")
            return 0
        if a.cmd == "list":
            return cmd_list(data_root)
        return 2
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
