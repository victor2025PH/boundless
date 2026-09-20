# -*- coding: utf-8 -*-
"""小智「电脑操控」上生产逐级就绪预检（实施91，2026-08-31）。

**只读**：读运行态配置（load_runtime_config，合并 overlay）+ 探各受控机 runner
/health（GET），算「逐级灰度」每一级是否就绪、缺什么。**不改任何配置、不落生产、
不发消息、不动 runner 状态**——照它的输出去开对应开关即可。

逐级（每级稳一天再进下一级）：
  ① 只读侦察  = pc_runner.enabled + ≥1 machines + ≥1 runner 在线
  ② UI 动作   = ① + runner 侧 PC_RUNNER_ACTIONS=1
  ③ 视觉点击  = ② + pc_runner.vision.enabled
  ④ 命令执行  = ② + runner 侧 PC_RUNNER_SHELL=1
  ⑤ 信任档    = ② + pc_runner.trust.enabled

用法：python -m tools.pc_runner_preflight [--json]
"""
from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List, Optional


def assess_rollout(pc_cfg: Optional[Dict[str, Any]],
                   healths: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """纯函数：给定 assistant.pc_runner 配置 + 各机 /health（{id: health|None}），
    算五级就绪。返回 [{n, key, name, met, missing:[...]}]。门禁友好、零 IO。"""
    pc_cfg = pc_cfg or {}
    healths = healths or {}
    enabled = bool(pc_cfg.get("enabled"))
    machines = list(pc_cfg.get("machines") or [])
    vision_on = bool((pc_cfg.get("vision") or {}).get("enabled"))
    trust_on = bool((pc_cfg.get("trust") or {}).get("enabled"))
    hs = [h or {} for h in healths.values()]
    any_online = any(h.get("ok") for h in hs)
    any_actions = any(h.get("actions_enabled") for h in hs)
    any_shell = any(h.get("shell_enabled") for h in hs)

    def _stage(n: int, key: str, name: str, conds) -> Dict[str, Any]:
        missing = [msg for ok, msg in conds if not ok]
        return {"n": n, "key": key, "name": name,
                "met": not missing, "missing": missing}

    s1 = _stage(1, "inspect", "只读侦察", [
        (enabled, "assistant.pc_runner.enabled=true"),
        (bool(machines), "配置 ≥1 台 machines"),
        (any_online, "≥1 台 runner 在线（/health ok）"),
    ])
    s2 = _stage(2, "ui_actions", "UI 动作", [
        (s1["met"], "先过①只读侦察"),
        (any_actions, "runner 侧 PC_RUNNER_ACTIONS=1"),
    ])
    s3 = _stage(3, "vision", "视觉点击", [
        (s2["met"], "先过②UI 动作"),
        (vision_on, "assistant.pc_runner.vision.enabled=true"),
    ])
    s4 = _stage(4, "shell", "命令执行", [
        (s2["met"], "先过②UI 动作"),
        (any_shell, "runner 侧 PC_RUNNER_SHELL=1"),
    ])
    s5 = _stage(5, "trust", "信任档免确认", [
        (s2["met"], "先过②UI 动作"),
        (trust_on, "assistant.pc_runner.trust.enabled=true"),
    ])
    return [s1, s2, s3, s4, s5]


def _collect() -> Dict[str, Any]:
    """读运行态配置 + 探各机 health。IO 都在这里，纯逻辑在 assess_rollout。"""
    from src.eval.eval_config import load_runtime_config
    from src.assistant import runner_pairing, runner_client

    config = load_runtime_config()
    pc_cfg = ((config.get("assistant") or {}).get("pc_runner") or {})
    machines = list(pc_cfg.get("machines") or [])
    try:
        runner_pairing.load_machines(machines)
    except Exception:
        pass
    healths: Dict[str, Any] = {}
    for m in machines:
        mid = str((m or {}).get("id") or "").strip()
        if not mid:
            continue
        try:
            healths[mid] = runner_client.probe(mid, config)
        except Exception as ex:  # noqa: BLE001
            healths[mid] = {"ok": False, "error": "probe_error",
                            "detail": str(ex)[:120]}
    return {"pc_cfg": pc_cfg, "machines": machines, "healths": healths,
            "stages": assess_rollout(pc_cfg, healths)}


def main() -> int:
    import sys
    try:  # PS5.1 GBK 控制台无法编码 ✓/✗/⚠ → reconfigure UTF-8（errors=replace 兜底）
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    data = _collect()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    pc = data["pc_cfg"]
    print("== 小智电脑操控 · 上生产逐级预检（只读）==")
    print(f"总闸 assistant.pc_runner.enabled = {bool(pc.get('enabled'))}")
    print(f"vision.enabled = {bool((pc.get('vision') or {}).get('enabled'))}"
          f"   trust.enabled = {bool((pc.get('trust') or {}).get('enabled'))}")
    print(f"machines: {len(data['machines'])} 台")
    for mid, h in (data["healths"] or {}).items():
        h = h or {}
        if h.get("ok"):
            print(f"  - {mid}: 在线 v{h.get('version', '?')} "
                  f"win32={h.get('win32_available')} uia={h.get('uia_available')} "
                  f"actions={h.get('actions_enabled')} shell={h.get('shell_enabled')} "
                  f"frozen={h.get('actions_frozen')}")
        else:
            print(f"  - {mid}: 离线/不可达（{h.get('error', '?')}）")
    print("-" * 60)
    for s in data["stages"]:
        mark = "✓" if s["met"] else "✗"
        line = f"  {s['n']}. {s['name']}  [{mark}]"
        if not s["met"]:
            line += "  需: " + "; ".join(s["missing"])
        print(line)
    nxt = next((s for s in data["stages"] if not s["met"]), None)
    print("-" * 60)
    if nxt:
        print(f"下一步：补齐第{nxt['n']}级「{nxt['name']}」缺项后复跑本预检。")
    else:
        print("五级全部就绪。")
    print("⚠ 开关属改生产配置——本工具只读，请人工在 overlay/runner env 变更。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
