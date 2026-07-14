#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查 ollama 端点 loaded model + VRAM 占用 (诊断 A/B 两路 VLM 撞 GPU).

背景: A (`fb_profile_classifier` → ollama qwen2.5vl:7b for persona classify)
和 B (`vision_fallback` → 可能 fallback 到 ollama vision model for UI element
finding) 都会用 ollama 本地. 同 host 上多模型 hot loaded 可能撞 VRAM, 真机
smoke 跑慢/OOM 时来这看是否 ollama 资源耗尽.

参考 memory: vlm_topology.md.

用法:
    python scripts/check_ollama_load.py
    python scripts/check_ollama_load.py --host http://192.168.1.10:11434
    python scripts/check_ollama_load.py --json
    python scripts/check_ollama_load.py --max-vram-gb 8 --quiet  # exit 1 if >8GB

退出码:
    0  ollama 可达 + VRAM 在阈值内
    1  ollama 不可达
    2  ollama 可达但 VRAM > --max-vram-gb (用于 cron / pre-smoke gate)
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Tuple

DEFAULT_HOST = "http://127.0.0.1:11434"


def fetch_loaded(host: str, timeout: float = 3.0) -> Tuple[bool, Any]:
    """GET {host}/api/ps. 返 (ok, data_or_error_string)."""
    try:
        import httpx
    except ImportError as e:
        return False, f"httpx 未安装 (requirements.txt 应有): {e}"
    try:
        r = httpx.get(f"{host.rstrip('/')}/api/ps", timeout=timeout)
        r.raise_for_status()
        return True, r.json()
    except Exception as e:
        return False, str(e)


def summarize(data: Dict[str, Any]) -> Dict[str, Any]:
    """从 /api/ps 返回提取关键字段 (与 ollama 0.5+ 兼容).

    Returns:
        {
          'count': int,
          'total_vram_gb': float,
          'models': [{'name', 'vram_gb', 'expires_at', 'parent_model'}, ...]
        }
    """
    raw_models: List[Dict[str, Any]] = data.get("models") or []
    models: List[Dict[str, Any]] = []
    total_vram_bytes = 0
    for m in raw_models:
        vram_b = int(m.get("size_vram", 0) or 0)
        total_vram_bytes += vram_b
        models.append({
            "name": m.get("name") or m.get("model") or "<unknown>",
            "vram_gb": round(vram_b / (1024 ** 3), 2),
            "expires_at": m.get("expires_at") or "n/a",
            "parent_model": m.get("details", {}).get("parent_model") or "",
        })
    return {
        "count": len(models),
        "total_vram_gb": round(total_vram_bytes / (1024 ** 3), 2),
        "models": models,
    }


def render_human(summary: Dict[str, Any], host: str) -> str:
    if summary["count"] == 0:
        return f"✓ ollama @ {host} reachable, 0 model loaded (cold)"
    lines = [f"✓ ollama @ {host} reachable, {summary['count']} model(s) "
             f"loaded, total VRAM {summary['total_vram_gb']:.2f} GB:"]
    for m in summary["models"]:
        lines.append(
            f"  - {m['name']:<40} VRAM={m['vram_gb']:>5.2f} GB "
            f"expires={m['expires_at']}")
    return "\n".join(lines)


def main(argv: List[str] = None) -> int:
    # Phase 10.3 (2026-04-24): Windows console (gbk/cp936) 默认 encoding 不能输出
    # ✓/⚠️/✗ 等 unicode 字符. reconfigure stdout/stderr 为 utf-8 + errors='replace'.
    # 仅 Python 3.7+ 有 reconfigure (我们要求 3.11+, 安全).
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass  # 非 TextIOWrapper (e.g. 重定向到 file 已是 utf-8) → 跳过

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=DEFAULT_HOST,
                    help=f"ollama 端点 URL (default {DEFAULT_HOST})")
    ap.add_argument("--json", action="store_true",
                    help="输出 JSON (machine-readable)")
    ap.add_argument("--quiet", "-q", action="store_true",
                    help="只在异常 / 超阈值时输出")
    ap.add_argument("--max-vram-gb", type=float, default=None,
                    help="VRAM 阈值 (GB). 总 VRAM 超过时 exit 2")
    ap.add_argument("--timeout", type=float, default=3.0)
    args = ap.parse_args(argv)

    ok, payload = fetch_loaded(args.host, timeout=args.timeout)
    if not ok:
        print(f"✗ ollama @ {args.host} unreachable: {payload}", file=sys.stderr)
        return 1

    summary = summarize(payload)

    if args.json:
        print(json.dumps({"host": args.host, **summary}, indent=2))
    elif not args.quiet or summary["count"] > 0:
        print(render_human(summary, args.host))

    if args.max_vram_gb is not None and summary["total_vram_gb"] > args.max_vram_gb:
        print(f"⚠️  VRAM {summary['total_vram_gb']:.2f} GB > "
              f"--max-vram-gb {args.max_vram_gb}, exit 2",
              file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
