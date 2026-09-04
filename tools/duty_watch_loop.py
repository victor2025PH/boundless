# -*- coding: utf-8 -*-
"""值守常驻监控循环（0903；老板要求「一直监控不能停」）。

每轮（默认 90s）盯四件事，增量追加到滚动日志，值守/新会话随时 tail 即可：
  1. VPS 诊断包水位 —— 新 code 出现即 scp 下载并解包到 tmp_diag\\<code>\\；
  2. 群消息水位（inbox.db platform_msg_id）—— 新消息 + 跳号告警（SOP C1）；
  3. bug_events 水位 —— 全 kind（含 rate_capped_report/usage 静默类，SOP §J）；
  4. 工单 max(id) 变化。
只读 + 只下载：绝不发消息、绝不改库。播报/定性永远由值守本人做。

用法：python tools/duty_watch_loop.py [--interval 90]
日志：D:\\chengjie-instances\\.ops\\duty_watch_loop.log（滚动保留 2000 行）
水位：同目录 duty_watch_loop.state.json（重启不回放历史）

前身是同名 .ps1（0903 15:1x）：PowerShell↔JSON 往返在空水位/空事件时反复
ConvertFrom-Json 失败，且宿主进程被回收后循环即死——纯 Python 版消掉这两个坑。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path

OPS = Path(r"D:\chengjie-instances\.ops")
DATA = Path(r"D:\chengjie-instances\zhiliao\data")
DIAG = Path(r"D:\boundless\tmp_diag")
LOG = OPS / "duty_watch_loop.log"
STATE = OPS / "duty_watch_loop.state.json"
GROUP = "-1004345824259"
VPS = "bd2026"
REMOTE_UPLOADS = "/home/ubuntu/hualing-leads/diag/uploads.jsonl"
REMOTE_DIAG = "/home/ubuntu/hualing-leads/diag"
_CODE_RE = re.compile(r'"code":"([A-Z0-9]{6})"')


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


def load_state() -> dict:
    try:
        s = json.loads(STATE.read_text(encoding="utf-8"))
        s.setdefault("codes", [])
        return s
    except Exception:
        return {"codes": [], "mid": 0, "event_id": 0, "ticket_id": 0}


def save_state(s: dict) -> None:
    try:
        STATE.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def sh(cmd: list, timeout: int = 40) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return r.stdout or ""
    except Exception:
        return ""


def sh_full(cmd: list, timeout: int = 40) -> tuple:
    """(stdout, 失败原因) —— 原因＝stderr 首行 / 'timeout' / 异常名，空串＝成功。

    I-6 F4①（2026-09-04）：旧 ``sh`` 把 stderr 与超时全吞成空串，日志只能写
    「network/ssh」猜原因；对照 net_probe_117.log 发现失败分钟链路全绿
    （vps=3/3 pub=3/3），红噪音全是探针自己的——不带原因的失败日志等于没有日志。
    """
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        if (r.stdout or "").strip():
            return r.stdout, ""
        err = (r.stderr or "").strip().splitlines()
        return "", (err[0][:160] if err else f"rc={r.returncode} empty stdout")
    except subprocess.TimeoutExpired:
        return "", f"timeout>{timeout}s"
    except Exception as exc:  # noqa: BLE001
        return "", f"{type(exc).__name__}: {str(exc)[:120]}"


def probe_packs(st: dict) -> None:
    cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", VPS,
           f"tail -12 {REMOTE_UPLOADS}"]
    out, why = sh_full(cmd)
    if not out.strip():
        # 一次短退避重试：同一分钟 net_probe_117 显示链路全绿时的空输出多为
        # ssh 握手瞬时失败（sshd MaxStartups / 与 LedgerBackup、callback_poll 等
        # 并发 ssh 撞车），重试即过；两次都空才算失败，并带出真实原因。
        time.sleep(3)
        out, why2 = sh_full(cmd)
        if not out.strip():
            log(f"vps probe failed ({why or 'empty'} | retry: {why2 or 'empty'})")
            return
        log(f"vps probe recovered on retry (first: {why or 'empty'})")
    seen = set(st["codes"])
    for line in out.splitlines():
        m = _CODE_RE.search(line)
        if not m:
            continue
        code = m.group(1)
        if code in seen:
            continue
        b = re.search(r'"bytes":(\d+)', line)
        app = re.search(r'"app":"([^"]*)"', line)
        fp = re.search(r'"fp":"([^"]*)"', line)
        note = re.search(r'"note":"([^"]*)"', line)
        log(f"NEW PACK {code} bytes={b.group(1) if b else '?'} "
            f"app={app.group(1) if app else '?'} "
            f"fp={fp.group(1)[:4] if fp else '?'}* "
            f"note={note.group(1)[:60] if note else '-'}")
        zp = DIAG / f"{code}.zip"
        sh(["scp", "-o", "ConnectTimeout=15", f"{VPS}:{REMOTE_DIAG}/{code}.zip", str(zp)], 120)
        if zp.is_file():
            try:
                dest = DIAG / code
                shutil.rmtree(dest, ignore_errors=True)
                with zipfile.ZipFile(zp) as z:
                    z.extractall(dest)
                inner = [f"{p.name}({p.stat().st_size}B)"
                         for p in sorted(dest.rglob("*")) if p.is_file()][:6]
                log(f"  unpacked -> {dest}  {', '.join(inner)}")
            except Exception as exc:
                log(f"  unpack failed: {exc}")
            seen.add(code)
        else:
            log("  download FAILED (retry next round)")
    st["codes"] = sorted(seen)[-200:]


def probe_msgs(st: dict) -> None:
    try:
        con = sqlite3.connect(str(DATA / "config" / "inbox.db"))
        rows = con.execute(
            "select direction, ts, platform_msg_id, substr(text,1,120), media_ref"
            " from messages where conversation_id like ? order by ts desc limit 60",
            (f"%{GROUP}%",)).fetchall()
        con.close()
    except Exception as exc:
        log(f"inbox probe failed: {exc}")
        return
    new = []
    for d, ts, mid, txt, mref in rows:
        try:
            m = int(mid)
        except (TypeError, ValueError):
            continue
        if m > int(st.get("mid") or 0):
            new.append((m, d, txt, mref))
    prev = int(st.get("mid") or 0)
    for m, d, txt, mref in sorted(new):
        if prev and m - prev > 1:
            log(f"MID GAP {prev}->{m}（入站可能漏收，核 seq_gap 补拉）")
        prev = m
        tag = "MSG-IN " if d == "in" else "msg-out"
        log(f"{tag} mid={m} {str(txt or '').replace(chr(10), ' / ')}"
            f"{'  [media]' if mref else ''}")
    if prev:
        st["mid"] = prev


def probe_events(st: dict) -> None:
    try:
        con = sqlite3.connect(str(DATA / "config" / "bug_intake.db"))
        low = int(st.get("event_id") or 0)
        if low <= 0:
            mx = con.execute("select max(id) from bug_events").fetchone()[0] or 0
            st["event_id"] = int(mx)
            log(f"event 水位初始化 = {mx}（不回放历史）")
        else:
            for r in con.execute(
                    "select id,kind,reporter_id,substr(detail,1,90) from bug_events"
                    " where id>? order by id", (low,)).fetchall():
                log(f"EVENT {r[0]} {r[1]} {r[2]} | "
                    f"{str(r[3] or '').replace(chr(10), ' / ')}")
                st["event_id"] = int(r[0])
        mt = con.execute("select max(id) from bug_tickets").fetchone()[0] or 0
        if int(mt) > int(st.get("ticket_id") or 0):
            if st.get("ticket_id"):
                log(f"NEW TICKET max={mt}（was {st['ticket_id']}）")
            st["ticket_id"] = int(mt)
        con.close()
    except Exception as exc:
        log(f"bug_intake probe failed: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=90)
    # 值守会话里的常驻进程会被宿主回收（0903 实测活不过几分钟）→ 生产形态是
    # 计划任务 DutyWatchTick 每 2 分钟 --once 调一轮（会话断了也照跑）。
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    OPS.mkdir(parents=True, exist_ok=True)
    DIAG.mkdir(parents=True, exist_ok=True)
    st = load_state()
    if not a.once:
        log(f"watch loop start (interval={a.interval}s mid={st.get('mid')} "
            f"event={st.get('event_id')} ticket={st.get('ticket_id')} "
            f"codes={len(st.get('codes') or [])})")
    while True:
        probe_packs(st)
        probe_msgs(st)
        probe_events(st)
        save_state(st)
        if a.once:
            return 0
        time.sleep(max(20, a.interval))


if __name__ == "__main__":
    raise SystemExit(main())
