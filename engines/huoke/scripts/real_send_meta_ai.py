# -*- coding: utf-8 -*-
"""真机给 Meta AI 真发测试文字 (验证 OPT-FP1-FP6 完整防御链).

设备来源 (优先级):
  1. CLI: --devices serial1,serial2,...   (显式覆盖)
  2. env OPENCLAW_SMOKE_DEVICES=s1,s2,... (CI/脚本批量)
  3. config/device_aliases.json 中 host_scope == "coordinator" 的设备
     (默认全部, 按 slot/number 排序; --limit N 取前 N)

设备 label 来自 aliases 的 display_label / alias 字段; 找不到时用 serial.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_ALIASES_PATH = _ROOT / "config" / "device_aliases.json"


def _load_aliases() -> dict:
    try:
        return json.loads(_ALIASES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"[WARN] aliases 不存在: {_ALIASES_PATH}", file=sys.stderr)
        return {}
    except json.JSONDecodeError as e:
        print(f"[WARN] aliases 解析失败: {e}", file=sys.stderr)
        return {}


def _label_for(serial: str, aliases: dict) -> str:
    info = aliases.get(serial) or {}
    return (
        info.get("display_label")
        or info.get("alias")
        or serial
    )


def _coordinator_devices(aliases: dict) -> List[str]:
    """按 slot/number 排序返回 host_scope=coordinator 的 serial 列表."""
    coords = [
        (info.get("slot") or info.get("number") or 999, serial)
        for serial, info in aliases.items()
        if info.get("host_scope") == "coordinator"
    ]
    coords.sort()
    return [s for _, s in coords]


def _resolve_devices(
    cli_devices: Optional[str],
    limit: Optional[int],
    aliases: Optional[dict] = None,
) -> List[Tuple[str, str]]:
    """决定本次要 smoke 哪些 (serial, label).

    边界:
      - CLI/env 给的 serial 即使不在 aliases 也允许 (label 退化 serial)
      - 0 设备时 main() 应给清晰错误
      - aliases 注入参数让单测可以脱离磁盘
    """
    if aliases is None:
        aliases = _load_aliases()

    serials: List[str] = []
    if cli_devices:
        serials = [s.strip() for s in cli_devices.split(",") if s.strip()]
    elif os.getenv("OPENCLAW_SMOKE_DEVICES"):
        serials = [
            s.strip()
            for s in os.environ["OPENCLAW_SMOKE_DEVICES"].split(",")
            if s.strip()
        ]
    else:
        serials = _coordinator_devices(aliases)

    if limit and limit > 0:
        serials = serials[:limit]

    return [(s, _label_for(s, aliases)) for s in serials]


def send_one(fb, device: str, label: str) -> dict:
    timestamp = time.strftime("%H:%M:%S")
    msg = f"hi meta-ai {timestamp}"
    print(f"\n{'-' * 60}")
    print(f"  {label}\n  serial:  {device}\n  message: {msg}")
    print(f"{'-' * 60}")
    t0 = time.time()
    result: dict = {"device": device, "label": label, "message": msg}
    try:
        from src.app_automation.facebook import MessengerError  # noqa: F401
        fb.send_message(
            "Meta AI", msg, device_id=device, raise_on_error=True)
        elapsed = time.time() - t0
        result["status"] = "PASS"
        result["elapsed_s"] = round(elapsed, 1)
        result["error"] = ""
        print(f"  ✅ PASS — 用时 {elapsed:.1f}s")
    except Exception as e:
        elapsed = time.time() - t0
        from src.app_automation.facebook import MessengerError
        if isinstance(e, MessengerError):
            result["status"] = f"MessengerError({e.code})"
            result["error"] = f"{e}"
            result["hint"] = e.hint
            print(f"  ⚠️  code={e.code} 用时 {elapsed:.1f}s\n  hint: {e.hint}")
        else:
            result["status"] = "EXCEPTION"
            result["error"] = f"{type(e).__name__}: {e}"
            print(f"  ❌ {type(e).__name__}: {e}")
        result["elapsed_s"] = round(elapsed, 1)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--devices",
        help="逗号分隔 serial, 覆盖 aliases + env",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="只跑前 N 台 (按 slot/number 排序)",
    )
    args = parser.parse_args()

    devices = _resolve_devices(args.devices, args.limit)
    if not devices:
        print(
            "[FATAL] 0 设备入选. 检查 config/device_aliases.json 中是否有 "
            "host_scope=coordinator 的条目, 或用 --devices / "
            "OPENCLAW_SMOKE_DEVICES 显式指定.",
            file=sys.stderr,
        )
        return 2

    print("=" * 60)
    print(
        f"L5 v2: {len(devices)} 真机给 Meta AI 真发 — "
        "验证 OPT-FP1-FP6 完整防线"
    )
    print("=" * 60)

    from src.app_automation.facebook import FacebookAutomation
    from src.device_control.device_manager import get_device_manager
    dm = get_device_manager()
    fb = FacebookAutomation(device_manager=dm)

    results = []
    for serial, label in devices:
        r = send_one(fb, serial, label)
        results.append(r)
        time.sleep(2)

    print(f"\n{'=' * 60}\n  汇总\n{'=' * 60}")
    for r in results:
        emoji = "✅" if r["status"] == "PASS" else "⚠️ "
        print(
            f"  {r['device'][:18]} {emoji} {r['status']:<35} "
            f"{r['elapsed_s']}s"
        )
        if r.get("hint"):
            print(f"      hint: {r['hint'][:100]}")

    pass_count = sum(1 for r in results if r["status"] == "PASS")
    print(f"\n  PASS: {pass_count}/{len(results)}")
    return 0 if pass_count == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
