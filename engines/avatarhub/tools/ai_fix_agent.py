# -*- coding: utf-8 -*-
"""ai_fix_agent.py — 崩溃归因任务 → Cursor SDK agent 产【修复草案】（AI 升级闭环末端，2026-07-13 P4）。

闭环：ingest 聚簇 → ai_triage 出带源码定位+影响面权重的 tasks.jsonl → 本工具按权重取任务，
用 Cursor SDK 在本仓跑一次性 agent 产"根因分析 + 修复 diff 草案"，落 logs/ai_fixes/。
**红线：只产草案，绝不自动改代码/提交**——人审后再手工 apply → build_packs --build-app →
rollout_ctl set 10 灰度热修。这是"让 AI 升级服务"既提效又不失控的颗粒度。

分级降级（无依赖也可用）：
  · 装了 cursor-sdk 且设了 CURSOR_API_KEY 且 --run → 真跑 Agent.prompt(local)，落 .md 草案；
  · 否则 → 为每个任务生成"可直接粘进 Cursor / 稍后跑"的 prompt 文件，不阻断闭环。
去重：已处理任务的 sig 记 logs/ai_fixes/processed.json，重跑不重复。

用法：
  python tools/ai_fix_agent.py --tasks logs/ai_triage/tasks.jsonl            # 仅生成 prompt 文件
  set CURSOR_API_KEY=cursor_...
  python tools/ai_fix_agent.py --tasks ... --run --top 3 --model composer-2.5 # 真跑 agent 产草案
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
OUT = HERE / "logs" / "ai_fixes"
PROCESSED = OUT / "processed.json"


def _load_processed() -> set:
    try:
        return set(json.loads(PROCESSED.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_processed(s: set):
    OUT.mkdir(parents=True, exist_ok=True)
    PROCESSED.write_text(json.dumps(sorted(s)), encoding="utf-8")


def _read_tasks(path: Path) -> list[dict]:
    out, seen = [], set()
    try:
        for ln in path.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            t = json.loads(ln)
            sig = t.get("sig")
            if sig and sig not in seen:   # 文件内也去重（cron 追加会重复）
                seen.add(sig)
                out.append(t)
    except FileNotFoundError:
        pass
    # 按权重/优先级排序（ai_triage 已排；这里兜底再排一次）
    out.sort(key=lambda t: t.get("weight", t.get("count", 0)), reverse=True)
    return out


def _build_prompt(t: dict) -> str:
    return (
        f"你是本仓库的资深工程师。线上崩溃上报聚簇显示以下错误需要修复。\n\n"
        f"- 异常类型：{t.get('exc')}\n"
        f"- 服务：{t.get('service')}\n"
        f"- 源码定位：{t.get('file')}:{t.get('line')}\n"
        f"- 影响面：{t.get('count')} 次上报，跨版本 {t.get('versions')}，权重 {t.get('weight')}\n"
        f"- 栈签名：{t.get('sig')}\n"
        f"- 脱敏样例：{t.get('sample','')}\n\n"
        f"请：1) 阅读 {t.get('file')} 该行附近代码，给出根因分析；"
        f"2) 给出最小化、防御性的修复 diff（只改必要处，保持既有风格与降级语义）；"
        f"3) 说明如何验证。**只输出分析与 diff 草案，不要直接提交或改动其它文件。**"
    )


def _write_prompt_file(t: dict) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    safe = (t.get("exc", "err") + "_" + str(t.get("line", "")) + "_" + str(abs(hash(t.get("sig", ""))) % 100000))
    p = OUT / f"{safe}.prompt.txt"
    p.write_text(_build_prompt(t), encoding="utf-8")
    return p


def _run_agent(t: dict, model: str, api_key: str) -> tuple[bool, str]:
    """用 Cursor SDK 一次性跑 agent 产草案（local 运行时，仅内联配置）。返回 (ok, 文本/错误)。"""
    from cursor_sdk import Agent, AgentOptions, LocalAgentOptions, CursorAgentError
    try:
        result = Agent.prompt(
            _build_prompt(t),
            AgentOptions(
                api_key=api_key,
                model=model,
                local=LocalAgentOptions(cwd=str(HERE)),   # 本仓上下文；settingSources 默认内联
            ),
        )
    except CursorAgentError as e:
        return False, f"[startup] {getattr(e, 'message', str(e))} retryable={getattr(e, 'is_retryable', '?')}"
    if getattr(result, "status", "") == "error":
        return False, f"[run] status=error id={getattr(result,'id','?')}"
    return True, getattr(result, "result", "") or ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default=str(HERE / "logs" / "ai_triage" / "tasks.jsonl"))
    ap.add_argument("--run", action="store_true", help="真跑 Cursor SDK agent（需 cursor-sdk + CURSOR_API_KEY）")
    ap.add_argument("--top", type=int, default=3, help="本次最多处理的高权重任务数")
    ap.add_argument("--model", default="composer-2.5")
    args = ap.parse_args()

    tasks = _read_tasks(Path(args.tasks))
    if not tasks:
        print("[ai_fix] 无任务（tasks.jsonl 为空或不存在）"); return 0
    processed = _load_processed()
    todo = [t for t in tasks if t.get("sig") not in processed][:args.top]
    if not todo:
        print(f"[ai_fix] {len(tasks)} 个任务均已处理，无新任务"); return 0

    OUT.mkdir(parents=True, exist_ok=True)
    have_sdk = False
    try:
        import cursor_sdk  # noqa: F401
        have_sdk = True
    except Exception:
        have_sdk = False
    api_key = os.environ.get("CURSOR_API_KEY", "").strip()
    can_run = args.run and have_sdk and bool(api_key)
    if args.run and not can_run:
        why = "未装 cursor-sdk" if not have_sdk else ("未设 CURSOR_API_KEY" if not api_key else "")
        print(f"[ai_fix] --run 请求但无法真跑（{why}）→ 降级为生成 prompt 文件（pip install cursor-sdk + 设 CURSOR_API_KEY 后可真跑）")

    done = 0
    for t in todo:
        if can_run:
            print(f"[ai_fix] 跑 agent：{t.get('exc')} @ {t.get('file')}:{t.get('line')}（权重 {t.get('weight')}）…")
            ok, text = _run_agent(t, args.model, api_key)
            draft = OUT / f"{t.get('exc','err')}_{t.get('line','')}.md"
            draft.write_text(f"# 修复草案（人审后再 apply）\n\n任务：{t.get('task')}\n\n---\n\n{text}\n",
                             encoding="utf-8")
            print(f"    {'✓ 草案' if ok else '✗ 失败'} → {draft}" + ("" if ok else f"（{text}）"))
        else:
            pf = _write_prompt_file(t)
            print(f"[ai_fix] 生成 prompt：{t.get('exc')} @ {t.get('file')}:{t.get('line')} → {pf}")
        processed.add(t.get("sig"))
        done += 1
    _save_processed(processed)
    print(f"[ai_fix] 完成 {done} 个（草案/prompt 落 {OUT}）；人审后 apply → build-app → rollout 灰度热修。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
