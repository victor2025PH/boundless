# -*- coding: utf-8 -*-
"""报障渠道提醒 + 报告回执（2026-09-06 老板指令：「下次他再在群里提交，提醒他去 Cursor 报告，
不要提交到群里——那才是真实的证据来源」）。

跟 tools/duty_watch_loop.py 同一节拍（计划任务 DutyWatchTick 每 2 分钟，见 duty_watch_tick.ps1），
但职责相反：watch_loop 只读不发，本工具**只发两类固定文案**、不改工单库：

  ① 群提交提醒：bug_events 里出现受控报障人的 bug_new / photo_report / screenshot /
     rate_capped_report（= bot 判定他在群里提交了 bug）→ 回一条提醒「请让 Cursor 提交报告，
     不要贴群」。同一报障人 6 小时内只提醒一次（连发 20 张图也只提醒一次）；bot 已立单则带
     --ticket（@报障人 + 工单 footer），否则正文自带称呼。
  ② 报告回执：watch_loop 新下载到的 field-agent 报告（report 类；verify / 远程 diag 不发）→
     群里回一条「收到 <码>（<主题>）→ 已立/已挂 #N」，同一轮多份合并成一条。没有这条，把人推去
     Cursor 就是再演一次 09-05 的零回音（52 份报告只立 29 份）。
     单号来自 tools/duty_auto_ticket.py（L-7 A，2026-09-06）：回执前先跑它的判定+落库，
     结果接进同一条回执；它失败/超时就回落成无单号的「收到 <码>」，绝不因它丢回执。

护栏：
  - 首次运行只初始化水位（event_id = 当前 max；receipted = 已下载全部码），不回放历史；
  - 发送走 engines/chengjie/tools/duty_reply.py（token 从实例配置读、对外红线闸门、回复台账）；
  - 任何异常只写日志，绝不抛出；--dry-run 只打印不发不写状态；
  - 文案可被 D:\\chengjie-instances\\.ops\\channel_reminder_text.txt / channel_receipt_text.txt 覆盖
    （占位符 {name} {ticket_clause} / {name} {items}），改文案不用改代码。

用法：python tools/duty_channel_reminder.py [--dry-run] [--cooldown-hours 6] [--no-receipt]
        python tools/duty_channel_reminder.py --dry-run --replay-events 30   # 拿最近 30 条事件演练
日志：D:\\chengjie-instances\\.ops\\duty_channel_reminder.log
状态：D:\\chengjie-instances\\.ops\\duty_channel_reminder.state.json
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

OPS = Path(r"D:\chengjie-instances\.ops")
DATA = Path(r"D:\chengjie-instances\zhiliao\data")
DIAG = Path(r"D:\boundless\tmp_diag")
ENGINE = Path(r"D:\boundless\engines\chengjie")
WATCH_STATE = OPS / "duty_watch_loop.state.json"
STATE = OPS / "duty_channel_reminder.state.json"
LOG = OPS / "duty_channel_reminder.log"
TEXT_REMIND = OPS / "channel_reminder_text.txt"
TEXT_RECEIPT = OPS / "channel_receipt_text.txt"
GROUP_ID = "4345824259"   # 报障群 chat_key 去负号（inbox conversation_id 模糊匹配用）

# 受控报障人：群里提交 → 提醒走 Cursor。老板 0906 指令只点了 skuio；钧要加就补一行。
REMIND_REPORTERS: Dict[str, str] = {
    "8942577244": "skuio 花无缺",
}
# 机器指纹前缀 → 报障人（报告回执用；zl_collect 报告头 `fp: DC8F-…`）
FP_OWNER: Dict[str, str] = {
    "DC8F": "skuio 花无缺",
    "B990": "钧 JUN",
}
SUBMIT_KINDS = ("bug_new", "photo_report", "screenshot", "rate_capped_report")

DEFAULT_REMIND = (
    "{name} 新规矩（老板定的，从这条起执行）：bug 请让你本机的 Cursor 助手提交报告，不要贴到群里。\n"
    "为什么：群里的图我们只能看现象；Cursor 报告自带那一刻的日志、运行状态和你助手的分析，"
    "是能直接查根因的证据——之前 28 份报告漏立单，也是两条渠道并行、我们只盯了群造成的。\n"
    "怎么做：对 Cursor 说一句现象（例如「批量挂链弹层看不清，跑 report」），它会打包上传并给你一个 6 位码；"
    "复验旧问题就在里面带上单号 #N。报告到达后群里会自动回一条「收到 <码>，已立/已挂 #N」；"
    "群里只用来回答我们的追问。\n"
    "你刚发的这条{ticket_clause}。"
)
DEFAULT_RECEIPT = ("{name} 报告已收到：{items}。不必再在群里贴同一件；"
                   "复验请对着单号看我们发的验收清单，进展会按单回访。")


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
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


def load_json(p: Path, default: dict) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return dict(default)


def save_json(p: Path, obj: dict) -> None:
    try:
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def read_template(p: Path, default: str) -> str:
    try:
        t = p.read_text(encoding="utf-8").strip()
        return t or default
    except Exception:
        return default


# ── 发送（走 duty_reply：token / 红线闸门 / 台账都在那里）──────────────────────

def send_group(text: str, ticket: int = 0, *, dry_run: bool) -> bool:
    if dry_run:
        log(f"[dry-run] ticket={ticket or '-'} would send:\n{text}")
        return True
    fd, tmp = tempfile.mkstemp(prefix="duty_chan_", suffix=".txt")
    Path(tmp).write_text(text, encoding="utf-8")
    cmd = [sys.executable, "tools/duty_reply.py", "--group", "neice", "--receipt",
           "--text-file", tmp]
    if ticket:
        cmd += ["--ticket", str(ticket)]
    try:
        r = subprocess.run(cmd, cwd=str(ENGINE), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=150)
        tail = (r.stdout or "").strip().splitlines()
        log(f"send rc={r.returncode} :: {tail[-1] if tail else (r.stderr or '')[-160:]}")
        return r.returncode == 0
    except Exception as exc:  # noqa: BLE001
        log(f"send failed: {type(exc).__name__}: {exc}")
        return False
    finally:
        try:
            Path(tmp).unlink()
        except Exception:
            pass


# ── ① 群提交提醒 ─────────────────────────────────────────────────────────────

def _ticket_from_event(con: sqlite3.Connection, kind: str, detail: str, ts: float,
                       reporter: str) -> int:
    m = re.search(r"#(\d{2,4})", detail or "")
    if m and kind in ("screenshot", "rate_capped_report", "photo_report"):
        return int(m.group(1))
    if kind == "bug_new":
        row = con.execute(
            "SELECT id FROM bug_tickets WHERE reporter_id=? AND ABS(created_ts-?)<=120"
            " ORDER BY id DESC LIMIT 1", (reporter, ts)).fetchone()
        if row:
            return int(row[0])
    return 0


def _is_reply_to_us(ts: float, reporter: str) -> bool:
    """他那条消息若是「回复我们的追问」（引用了支持号/老板的消息）→ 不算主动提交，不提醒。"""
    try:
        con = sqlite3.connect(str(DATA / "config" / "inbox.db"), timeout=5)
        rows = con.execute(
            "SELECT reply_to_id, reply_to_sender FROM messages"
            " WHERE conversation_id LIKE ? AND direction='in' AND sender_id=? AND ABS(ts-?)<=8",
            (f"%{GROUP_ID}%", reporter, ts)).fetchall()
        con.close()
        for rid, rsender in rows:
            if rid and re.search(r"BOUNDLESS|无界|BOUNDELESS", str(rsender or ""), re.I):
                return True
    except Exception:
        pass
    return False


def check_group_submissions(st: dict, *, cooldown_h: float, dry_run: bool,
                            replay: int = 0) -> None:
    try:
        con = sqlite3.connect(str(DATA / "config" / "bug_intake.db"), timeout=10)
    except Exception as exc:
        log(f"bug_intake open failed: {exc}")
        return
    try:
        mx = int(con.execute("SELECT MAX(id) FROM bug_events").fetchone()[0] or 0)
        low = int(st.get("event_id") or 0)
        if replay:
            low = max(0, mx - replay)
        elif low <= 0:
            st["event_id"] = mx
            log(f"event 水位初始化 = {mx}（不回放历史）")
            return
        rows = con.execute(
            "SELECT id, ts, kind, reporter_id, detail FROM bug_events WHERE id>? ORDER BY id",
            (low,)).fetchall()
        hits: Dict[str, Tuple[float, str, str, int]] = {}
        for eid, ts, kind, rep, detail in rows:
            rep = str(rep or "")
            if kind in SUBMIT_KINDS and rep in REMIND_REPORTERS:
                if _is_reply_to_us(float(ts), rep):
                    log(f"submission-like event {kind} by {REMIND_REPORTERS[rep]} 是回复我们的追问，跳过")
                    continue
                tid = _ticket_from_event(con, kind, detail or "", float(ts), rep)
                prev = hits.get(rep)
                # 同一轮多条：保留最早一条的时间、最新的工单号
                hits[rep] = (prev[0] if prev else float(ts), kind, (detail or "")[:60],
                             tid or (prev[3] if prev else 0))
        if not replay:
            st["event_id"] = mx
        now = time.time()
        for rep, (ts, kind, detail, tid) in hits.items():
            last = float((st.get("last_remind") or {}).get(rep) or 0)
            if now - last < cooldown_h * 3600:
                log(f"submission by {REMIND_REPORTERS[rep]} ({kind} {detail}) — 冷却中，"
                    f"距上次提醒 {int((now-last)/60)} 分钟，不重复提醒")
                continue
            name = REMIND_REPORTERS[rep]
            clause = (f" bot 已记 #{tid}，麻烦让 Cursor 补一份带日志的报告并带上 #{tid}"
                      if tid else " 我先收下，麻烦让 Cursor 补一份带日志的报告")
            tpl = read_template(TEXT_REMIND, DEFAULT_REMIND)
            text = tpl.replace("{ticket_clause}", clause)
            # /reply 端点自己会 @报障人；send 路径要自带称呼
            text = text.replace("{name}", "" if tid else name).lstrip()
            log(f"REMIND {name}: trigger={kind} {detail} ticket={tid or '-'}")
            if send_group(text, tid, dry_run=dry_run) and not dry_run:
                st.setdefault("last_remind", {})[rep] = now
    except Exception as exc:  # noqa: BLE001
        log(f"check_group_submissions failed: {type(exc).__name__}: {exc}")
    finally:
        try:
            con.close()
        except Exception:
            pass


# ── ② 报告回执 ───────────────────────────────────────────────────────────────

def _auto_ticket(code: str, *, dry_run: bool) -> Optional[dict]:
    """tools/duty_auto_ticket.auto_ticket 的防御性包装：模块缺失/异常都只记日志。"""
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import duty_auto_ticket as dat  # noqa: E402
        return dat.auto_ticket(code, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        log(f"auto_ticket {code} unavailable: {type(exc).__name__}: {exc}")
        return None


def _receipt_item(code: str, topic: str, res: Optional[dict]) -> str:
    try:
        import duty_auto_ticket as dat  # noqa: E402
        return dat.receipt_item(code, topic, res)
    except Exception:
        return f"{code}（{topic}）"


def _report_head(code: str) -> Optional[Tuple[str, str, str]]:
    """(kind, fp4, note) —— 非 report 类返回 kind 供跳过；目录不存在返回 None。"""
    d = DIAG / code
    if not d.is_dir():
        return None
    mds = list(d.glob("*_report*.md"))
    if not mds:
        return ("diag", "", "")
    lines = mds[0].read_text(encoding="utf-8", errors="replace").splitlines()
    kind = "verify" if "verify" in mds[0].name else "report"
    head = lines[1] if len(lines) > 1 else ""
    fp = re.search(r"fp:\s*([A-Z0-9]{4})", head)
    note = next((l[len("- note:"):].strip() for l in lines[:6] if l.startswith("- note:")), "")
    return (kind, fp.group(1) if fp else "", note)


def check_new_reports(st: dict, *, dry_run: bool) -> None:
    try:
        watch = load_json(WATCH_STATE, {})
        codes: List[str] = list(watch.get("codes") or [])
        receipted = set(st.get("receipted") or [])
        if not st.get("receipt_inited"):
            st["receipted"] = sorted(set(codes))[-400:]
            st["receipt_inited"] = True
            log(f"receipt 水位初始化 = {len(codes)} 个已下载码（不回放历史）")
            return
        pending = [c for c in codes if c not in receipted]
        if not pending:
            return
        by_owner: Dict[str, List[str]] = {}
        owner_codes: Dict[str, List[str]] = {}
        for code in pending:
            head = _report_head(code)
            if head is None:          # 还没解包完，下一轮再看
                continue
            kind, fp4, note = head
            if kind not in ("report", "verify"):
                receipted.add(code)
                log(f"pack {code} kind={kind} — 不回执")
                continue
            # L-7 A：先挂单/立单，单号接进这条回执（工具自身失败 → res=None → 无单号回执）
            res = _auto_ticket(code, dry_run=dry_run)
            if kind == "verify" and not (res and res.get("ticket")):
                receipted.add(code)
                log(f"pack {code} kind=verify 无 #N — 不回执")
                continue
            owner = FP_OWNER.get(fp4, f"机器 {fp4}*")
            topic = re.sub(r"^【[^】]+】", "", note).strip()[:36] or "（无备注）"
            by_owner.setdefault(owner, []).append(_receipt_item(code, topic, res))
            owner_codes.setdefault(owner, []).append(code)
        for owner, items in by_owner.items():
            tpl = read_template(TEXT_RECEIPT, DEFAULT_RECEIPT)
            text = tpl.replace("{name}", owner).replace("{items}", "、".join(items))
            log(f"RECEIPT {owner}: {len(items)} 份 {owner_codes.get(owner)}")
            # 发成功才算已回执；失败（0906 12:49 实锤：连发两份同题报告，第二条回执被
            # send 的 120 秒近重复守卫 409 拒掉）留到下一轮重发，报告不会零回音
            if send_group(text, 0, dry_run=dry_run):
                receipted.update(owner_codes.get(owner, []))
            else:
                log(f"receipt for {owner_codes.get(owner)} not sent — 下一轮重试")
        if not dry_run:
            st["receipted"] = sorted(receipted)[-400:]
    except Exception as exc:  # noqa: BLE001
        log(f"check_new_reports failed: {type(exc).__name__}: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description="报障渠道提醒 + 报告回执（单轮）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cooldown-hours", type=float, default=6.0)
    ap.add_argument("--no-receipt", action="store_true", help="只做群提交提醒，不发报告回执")
    ap.add_argument("--replay-events", type=int, default=0,
                    help="演练：不管水位，处理最近 N 条事件（配 --dry-run 用）")
    a = ap.parse_args()
    OPS.mkdir(parents=True, exist_ok=True)
    st = load_json(STATE, {"event_id": 0, "last_remind": {}, "receipted": [], "receipt_inited": False})
    check_group_submissions(st, cooldown_h=a.cooldown_hours, dry_run=a.dry_run,
                            replay=a.replay_events)
    if not a.no_receipt:
        check_new_reports(st, dry_run=a.dry_run)
    if not a.dry_run:
        save_json(STATE, st)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
