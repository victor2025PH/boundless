# -*- coding: utf-8 -*-
"""工单 → 本机 Cursor agent 自动分析/修复派单器（实施81 L2，2026-08-29）。

闭环定位（与老板对齐的三级里的第二级）：
  L1 对账（duty_reconcile）把「其实修过了」清掉 → 剩下的真待修工单由本工具
  派给 **headless Cursor agent**（``agent`` CLI，plan 模式只读分析）逐单产出
  「根因 + 建议补丁(diff) + 验证方案 + 一句话修复说明」，回写工单 note、
  状态转 in_progress、摘要推 @ai_zkw；老板/值守审补丁 → 合入上线 → 标 fixed
  → 既有自动回访。**说「已修复」的权力留给人，准备工作全自动。**

    python tools/duty_autofix_dispatch.py --list          # 可派工单（只读）
    python tools/duty_autofix_dispatch.py --dispatch 44 --dry-run   # 看工单书
    python tools/duty_autofix_dispatch.py --dispatch 44   # 真派（agent 跑分析）
    python tools/duty_autofix_dispatch.py --auto 2        # 按严重度自动挑 2 单

v1 安全边界（刻意）：
- agent 跑 **plan 模式（只读）**：不改共享工作树、不跑重启、不碰生产配置——
  共享树上多条人类 lane 并行，自动 agent 直接编辑是撞车之源；产出补丁给人看。
- 每次派单登记 state（``.ops/autofix/state.json``），不重复派；
- 未登录（``agent status`` 非 Logged in）→ 明确拒绝并指路 ``agent login``。

前置：``irm 'https://cursor.com/install?win32=true' | iex`` 已装（2026-08-29），
一次性 ``agent login``。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._data_root import resolve_data_roots  # noqa: E402

ENGINE_ROOT = Path(__file__).resolve().parent.parent
AGENT_BIN = r"C:\Users\Administrator\AppData\Local\cursor-agent\agent.cmd"
OUT_BASE = Path(r"D:\chengjie-instances\.ops\autofix")
STATE_PATH = OUT_BASE / "state.json"
DEFAULT_TIMEOUT_SEC = 1200
_SEV_RANK = {"P0": 0, "P1": 1, "P2": 2}


def _out(s: str) -> None:
    sys.stdout.buffer.write((s + "\n").encode("utf-8"))
    sys.stdout.flush()


# ── 纯函数（门禁 tests/test_duty_autofix_dispatch.py）───────────────────────

def pick_actionable(tickets: List[Dict[str, Any]], state: Dict[str, Any],
                    limit: int = 0) -> List[Dict[str, Any]]:
    """可派单：bug 类 + new/confirmed + 未派过；P0>P1>P2、同级新单优先。"""
    done = set(str(k) for k in (state or {}))
    out = [
        t for t in (tickets or [])
        if str(t.get("category") or "bug") == "bug"
        and str(t.get("status")) in ("new", "confirmed")
        and str(t.get("id")) not in done
    ]
    out.sort(key=lambda t: (_SEV_RANK.get(str(t.get("severity")), 9),
                            -int(t.get("id") or 0)))
    return out[:limit] if limit and limit > 0 else out


def build_work_order(ticket: Dict[str, Any]) -> str:
    """派给 headless agent 的工单书（约束与产出契约都钉在提示词里）。"""
    tid = int(ticket.get("id") or 0)
    sev = str(ticket.get("severity") or "P2")
    title = " ".join(str(ticket.get("title") or "").split())[:120]
    body = str(ticket.get("body") or "")[:1800]
    reporter = str(ticket.get("reporter_name") or ticket.get("reporter_id") or "")
    return f"""你是智聊（ChatX）报障工单的修复分析工程师，工作目录 D:\\boundless\\engines\\chengjie（多平台 AI 客服引擎，Python FastAPI + pyrogram）。

【工单 #{tid}（{sev}，报障人 {reporter}）】
标题：{title}
台账正文（含收集窗补充/截图 VLM 摘要/遥测快照）：
{body}

【硬约束——先读再动】
1. 先读仓库根 AGENTS.md 的纪律段（共享工作树多 lane 并行）；
2. 本次是**只读分析**：不修改任何文件、不执行任何写命令、不重启服务、不改配置——你的产出是给人审的方案，不是直接落地；
3. 结论必须落在具体代码上（文件路径 + 行号 + 现状行为），禁止泛泛而谈；拿不准的明确说「需要真机复现」并写出复现步骤。

【必须产出（按此结构输出）】
## 根因分析
（哪个文件哪段逻辑导致工单症状；证据行号）
## 建议补丁
（unified diff 格式，最小改动；若纯配置/文案问题给出具体键与值）
## 验证方案
（跑哪些既有测试/新增什么测试/真机怎么验）
## 修复说明一句话
（≤60 字，将来标 fixed 时写进群回访「本次改动：…」，用户能看懂的话）
## 置信度
（high/medium/low + 一句为什么）"""


def parse_report_sections(text: str) -> Dict[str, str]:
    """agent 输出 → 按「## 标题」切段（缺段返空串，消费方自兜底）。

    切分正则必须钉「## + 空白」：`^##\\s*` 会把 `### 子标题` 也切开
    （\\s* 允许零空格 → ### 的第三个 # 被当内容），「根因分析」若以
    ### 子节开头就被切成空串（2026-08-29 工单#20 实弹踩中）。
    """
    out = {"根因分析": "", "建议补丁": "", "验证方案": "",
           "修复说明一句话": "", "置信度": ""}
    if not text:
        return out
    parts = re.split(r"^##[ \t]+", str(text), flags=re.M)
    for seg in parts:
        seg = seg.strip()
        for key in out:
            if seg.startswith(key):
                out[key] = seg[len(key):].strip()[:4000]
    return out


def build_summary(tid: int, sections: Dict[str, str], report_path: str) -> str:
    conf = " ".join((sections.get("置信度") or "?").split())[:60]
    fix = " ".join((sections.get("修复说明一句话") or "（缺）").split())[:80]
    root = " ".join((sections.get("根因分析") or "（缺）").split())[:140]
    return (f"[自动修复分析] 工单 #{tid} 分析完成（置信度 {conf}）\n"
            f"根因：{root}\n"
            f"修复说明（草）：{fix}\n"
            f"完整报告+补丁：{report_path}\n"
            f"批准后：审补丁→合入→上线→处置台标 fixed（自动回访）")


# ── 数据/执行 ────────────────────────────────────────────────────────────────

def _load_tickets(db_path: Path) -> List[Dict[str, Any]]:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True,
                          timeout=5)
    try:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(
            "SELECT * FROM bug_tickets WHERE dup_of=0"
            " ORDER BY id DESC LIMIT 200").fetchall()]
    finally:
        con.close()


def _load_state() -> Dict[str, Any]:
    try:
        if STATE_PATH.is_file():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                          encoding="utf-8")


def _agent_logged_in() -> bool:
    try:
        r = subprocess.run([AGENT_BIN, "status"], capture_output=True,
                           timeout=30)
        return b"Logged in" in (r.stdout or b"") + (r.stderr or b"")
    except Exception:
        return False


def _run_agent(prompt: str, timeout_sec: int) -> str:
    """plan 模式只读跑一单，返回文本输出（超时/异常返错误说明）。

    prompt 必须走 **stdin** 而非 argv（2026-08-29 实弹教训）：AGENT_BIN 是
    .cmd 垫片，argv 里含换行/中文的长工单书会被 cmd 的 %* 重切参数——
    `--trust` 被搅进烂参数 → headless 撞「Workspace Trust Required」1.2s 退。
    `-p` 是布尔 print 旗标不是 prompt 参数，stdin 是 print 模式的正路。

    输出必须走 **stream-json 并提取 plan 工件**（同日第二发实弹教训）：
    plan 模式下 agent 把完整分析写进 createPlanToolCall 的 plan 字段，最终
    文本消息只剩一句过场——`--output-format text` 只打印最终消息 ⇒ 284s
    真分析换回空 stdout。故解析事件流：优先取最后一个 plan 工件全文，
    没有 plan 再拼 result 文本兜底。
    """
    cmd = [AGENT_BIN, "--trust", "--mode", "plan", "-p",
           "--output-format", "stream-json", "--workspace", str(ENGINE_ROOT)]
    try:
        r = subprocess.run(cmd, input=prompt.encode("utf-8"),
                           capture_output=True, timeout=timeout_sec,
                           cwd=str(ENGINE_ROOT))
        out = (r.stdout or b"").decode("utf-8", "replace")
        err = (r.stderr or b"").decode("utf-8", "replace")
        plan, result_text = "", ""
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "tool_call":
                p = (((ev.get("tool_call") or {}).get("createPlanToolCall")
                      or {}).get("args") or {}).get("plan")
                if p:
                    plan = str(p)
            elif ev.get("type") == "result":
                result_text = str(ev.get("result") or "")
        best = plan or result_text
        if best.strip():
            return best
        return (f"[agent no plan/result] rc={r.returncode}\n"
                f"[stdout tail]\n{out[-1500:]}\n[agent stderr]\n{err[:1500]}")
    except subprocess.TimeoutExpired:
        return f"[timeout] agent 超过 {timeout_sec}s 未完成"
    except Exception as e:  # noqa: BLE001
        return f"[error] {e}"


def _writeback(data_root: Path, tid: int, sections: Dict[str, str]) -> None:
    """分析摘要回写工单 note + 状态转 in_progress（与 duty_reply 同姿势）。"""
    import os
    os.environ.setdefault("AITR_DATA_DIR", str(data_root))
    try:
        from src.ops.bug_intake import append_ticket_note, set_ticket_status
        fix = " ".join((sections.get("修复说明一句话") or "").split())[:120]
        root = " ".join((sections.get("根因分析") or "").split())[:200]
        append_ticket_note(tid, f"[自动分析] 根因：{root or '见报告'}"
                                + (f"；修复说明草：{fix}" if fix else ""))
        set_ticket_status(tid, "in_progress")
    except Exception as e:  # noqa: BLE001
        _out(f"[warn] 工单回写失败（报告已落盘）：{e}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="工单自动分析派单（agent plan 只读）")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dispatch", type=int, default=0, help="指定工单号")
    ap.add_argument("--auto", type=int, default=0, help="按严重度自动挑 N 单")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC)
    ap.add_argument("--data-root", default="")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印工单书，不跑 agent 不写状态")
    args = ap.parse_args()

    data_root = resolve_data_roots(args.data_root)[0]
    db = Path(data_root) / "config" / "bug_intake.db"
    if not db.is_file():
        _out(f"[skip] 无工单台账（{db}）")
        return 0
    tickets = _load_tickets(db)
    state = _load_state()
    actionable = pick_actionable(tickets, state)

    if args.list or not (args.dispatch or args.auto):
        _out(f"[可派] {len(actionable)} 单（bug 类 · new/confirmed · 未派过）：")
        for t in actionable[:30]:
            title = " ".join(str(t.get("title") or "").split())[:70]
            _out(f"  #{t['id']} [{t.get('severity')}] {title}")
        _out("派单：--dispatch <id>（--dry-run 先看工单书）；批量：--auto N")
        return 0

    todo: List[Dict[str, Any]] = []
    if args.dispatch:
        hit = next((t for t in tickets if int(t.get("id") or 0) == args.dispatch),
                   None)
        if not hit:
            _out(f"[err] 工单 #{args.dispatch} 不存在")
            return 1
        todo = [hit]
    else:
        todo = pick_actionable(tickets, state, limit=args.auto)
        if not todo:
            _out("[ok] 无可派工单")
            return 0

    if args.dry_run:
        for t in todo:
            _out("=" * 60)
            _out(build_work_order(t))
        _out("[dry-run] 未跑 agent、未写状态。")
        return 0

    if not Path(AGENT_BIN).is_file():
        _out(f"[err] 未装 Cursor CLI（{AGENT_BIN}）——"
             "irm 'https://cursor.com/install?win32=true' | iex")
        return 1
    if not _agent_logged_in():
        _out("[err] Cursor CLI 未登录——在 117 跑一次 `agent login`"
             "（或设 CURSOR_API_KEY），登录后本命令即可用")
        return 1

    rc = 0
    for t in todo:
        tid = int(t.get("id") or 0)
        _out(f"[dispatch] #{tid} 分析中（plan 只读，超时 {args.timeout}s）…")
        started = time.time()
        report = _run_agent(build_work_order(t), args.timeout)
        out_dir = OUT_BASE / f"t{tid}"
        out_dir.mkdir(parents=True, exist_ok=True)
        report_path = out_dir / "report.md"
        report_path.write_text(report, encoding="utf-8")
        sections = parse_report_sections(report)
        ok = bool(sections.get("根因分析")) and not report.startswith("[")
        _out(f"[done] #{tid} {'✅' if ok else '⚠️ 输出不完整'} "
             f"{round(time.time() - started, 1)}s -> {report_path}")
        state[str(tid)] = {"ts": time.time(), "ok": ok}
        _save_state(state)
        if ok:
            _writeback(data_root, tid, sections)
            try:
                from tools.duty_alert import deliver
                deliver(build_summary(tid, sections, str(report_path)))
            except Exception as e:  # noqa: BLE001
                _out(f"[warn] 摘要投递失败：{e}")
        else:
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
