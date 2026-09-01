# -*- coding: utf-8 -*-
"""LINE 接收链实测探针（只读，B98 / P1-15，2026-08-28）。

背景：两个 LINE 账号的 worker 一直 ``receiver 轮回 N 次 clean=True 入站累计 0``
——SSE 开得成、通讯录同步也正常，就是一条 op 都不来，用户侧表现「只能发不能收」。
``account_orchestrator`` 的注释推测「那是 token 该续期」，但 SSE 开流要求 HTTP 200
才继续（``okline/operations.py:102``），**200 本身就证明鉴权是过的**——所以那个推测
需要实证，不能照着改。

本探针直接开一路 SSE，把**每一个**事件（含 ping / connInfoRevision / reconnect 这些
`iter_operations` 会跳过的控制事件）如实打出来，回答三个问题：
  1. 网关到底推不推东西？推的是什么？
  2. 有没有 message 事件而我们的解析把它丢了？
  3. 流的生命周期是不是真的 ~10s 就正常关（那样每轮都要重连，消息可能落在缝里）？

**只读**：不发消息、不写 token 文件、不动任何生产状态。会短暂多开一路 SSE 连接
（worker 本来每 ~10s 就重连一次，影响面等同一次正常重连）。

用法：
    python tools/probe_line_ops.py                 # 自动发现账号，各探 60s
    python tools/probe_line_ops.py --mid U... --seconds 90
    python tools/probe_line_ops.py --list          # 只列账号与 token 年龄
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _sessions_dir() -> Path:
    """会话目录：优先按运行中实例的数据根解析，回落引擎根。"""
    try:
        from scripts._data_root import resolve_data_roots  # type: ignore
        for root in resolve_data_roots():
            p = Path(root) / "sessions" / "line"
            if p.is_dir():
                return p
    except Exception:
        pass
    for cand in (Path(r"D:\chengjie-instances\zhiliao\data\sessions\line"),
                 REPO / "sessions" / "line"):
        if cand.is_dir():
            return cand
    return REPO / "sessions" / "line"


def _accounts(sdir: Path):
    out = []
    for p in sorted(sdir.glob("*.json")):
        try:
            mid = json.loads(p.read_text(encoding="utf-8")).get("mid") or p.stem
        except Exception:
            mid = p.stem
        age_d = (time.time() - p.stat().st_mtime) / 86400.0
        out.append((str(mid), p, age_d))
    return out


def _ensure_node() -> None:
    """okline 每个请求都要 Node 桥算 X-Hmac —— 与 worker 同款前置。"""
    try:
        from src.integrations.line_protocol_login import ensure_node_runtime
        from src.utils.config_manager import ConfigManager
        cfg = {}
        try:
            cfg = ConfigManager().config or {}
        except Exception:
            pass
        if not ensure_node_runtime(cfg):
            print("  ! 未探到 Node 运行时，X-Hmac 桥可能起不来", flush=True)
    except Exception as exc:
        print(f"  ! ensure_node_runtime 跳过：{exc}", flush=True)


def _summarize(ev) -> str:
    """事件摘要——**绝不打印消息正文**（生产会话内容），只出结构与类型。"""
    d = ev.data
    if isinstance(d, dict):
        ops = d.get("operations")
        if isinstance(ops, list):
            types = [o.get("type") for o in ops if isinstance(o, dict)]
            return f"operations x{len(ops)} types={types}"
        return "dict keys=" + ",".join(sorted(d.keys())[:8])
    if isinstance(d, list):
        return f"list len={len(d)}"
    s = str(d or "")
    return f"<{type(d).__name__} len={len(s)}>"


def probe(mid: str, path: Path, seconds: float) -> dict:
    from okline import OkLine

    print(f"\n=== {mid[:20]}…  (tokens 写入于 {(time.time()-path.stat().st_mtime)/86400:.1f} 天前)")
    api = OkLine.from_tokens_file(str(path))
    stats = {"mid": mid, "events": 0, "by_event": {}, "operations": 0,
             "stream_opens": 0, "errors": []}
    t_end = time.time() + seconds
    t0 = time.time()
    try:
        # reconnect=True：worker 用的就是本侧监督器 + 一击语义，这里为观测流的
        # 生命周期特意让它自动重开，好数出「N 秒内开了几次流」。
        for ev in api.ops.stream(reconnect=True):
            stats["events"] += 1
            name = ev.event or "message"
            stats["by_event"][name] = stats["by_event"].get(name, 0) + 1
            if isinstance(ev.data, dict) and isinstance(ev.data.get("operations"), list):
                stats["operations"] += len(ev.data["operations"])
            print(f"  [{time.time()-t0:6.1f}s] event={name:<18} {_summarize(ev)}", flush=True)
            if time.time() >= t_end:
                break
    except Exception as exc:
        stats["errors"].append(f"{type(exc).__name__}: {exc}")
        print(f"  ! 流异常：{type(exc).__name__}: {exc}", flush=True)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mid", default="", help="只探这一个账号（缺省=全部）")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--list", action="store_true", help="只列账号")
    args = ap.parse_args()

    sdir = _sessions_dir()
    print(f"sessions_dir = {sdir}")
    accts = _accounts(sdir)
    if not accts:
        print("没有找到任何 LINE 会话文件")
        return 2
    for mid, p, age in accts:
        print(f"  - {mid}  tokens 写入于 {age:.1f} 天前")
    if args.list:
        return 0

    _ensure_node()
    results = []
    for mid, p, _age in accts:
        if args.mid and args.mid != mid:
            continue
        results.append(probe(mid, p, args.seconds))

    print("\n=== 结论 ===")
    for r in results:
        print(f"{r['mid'][:20]}…  事件 {r['events']} 条  分布={r['by_event']}  "
              f"真 operations={r['operations']}  错误={r['errors'] or '无'}")
    print("\n读法：")
    print("  · 只有 ping/connInfoRevision、operations=0  → 网关认为该设备没有待收 op；")
    print("    鉴权是过的（开流 200），所以不是 token 过期，多半是副设备未被列为接收端。")
    print("  · 出现 operations>0 而生产 inbound 累计仍为 0 → 是我们这侧的解析/落库断了。")
    print("  · 开流即抛 HTTP 401/403 → 那才是 token 问题。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
