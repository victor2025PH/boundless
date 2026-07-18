#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""从集团 sync API 拉取人设 grants 清单，写入引擎侧本地缓存（仅标准库）。

契约：platform/identity/PERSONA_BUS.md §4.1（运行时软门控 v1.2）
API：GET <base>/api/sync/personas/grants?system=<engine>
     Authorization: Bearer <EVENT_INGEST_KEY>

写出缓存格式（供 platform/identity/grant_gate.py 消费）::

    {"version":1,"fetched_at":"<ISO8601>","system":"<engine>",
     "grants":[{"source_key":"...","product_id":"...","status":"granted|revoked"}]}

用法::

    python tools/persona_bus/fetch_grants.py --base https://bd2026.cc --key $env:EVENT_INGEST_KEY \
        --system avatarhub --out data/persona_bus_out/avatarhub_grants.json

失败退出非 0；stderr 含可重试说明（网络/5xx/鉴权）。可挂在 deploy/cron
export 之后周期执行。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SOURCE_SYSTEMS = ("avatarhub", "chengjie", "huoke")
DEFAULT_BASE = "https://bd2026.cc"
GRANTS_PATH = "/api/sync/personas/grants"
TIMEOUT_S = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_grants(base: str, key: str, system: str, timeout: float = TIMEOUT_S) -> dict:
    """拉取并规整为本地缓存文档。Raises urllib.error / ValueError / RuntimeError。"""
    root = base.rstrip("/")
    url = f"{root}{GRANTS_PATH}?system={urllib.parse.quote(system)}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "boundless-persona-bus-fetch-grants/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise RuntimeError(
            f"HTTP {e.code} from grants API: {detail or e.reason}. "
            "可重试：确认 EVENT_INGEST_KEY、base URL、网络后再次执行本命令。"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"网络错误拉取 grants：{e.reason!r}。"
            "可重试：检查 DNS/防火墙/VPN 后再次执行；离线期间沿用旧缓存，门控默认 warn 不挡业务。"
        ) from e

    if status != 200:
        raise RuntimeError(f"unexpected HTTP status {status}: {body[:300]}")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        raise ValueError(f"grants API 返回非 JSON：{e}") from e

    if not isinstance(payload, dict) or not payload.get("ok"):
        raise RuntimeError(f"grants API 业务失败：{payload!r}")

    grants_raw = payload.get("grants")
    if not isinstance(grants_raw, list):
        raise ValueError("grants API 缺少 grants 数组")

    grants: list[dict[str, str]] = []
    for i, g in enumerate(grants_raw):
        if not isinstance(g, dict):
            raise ValueError(f"grants[{i}] 非对象")
        sk = str(g.get("source_key") or "").strip()
        pid = str(g.get("product_id") or "").strip()
        st = str(g.get("status") or "").strip().lower()
        if not sk or not pid:
            raise ValueError(f"grants[{i}] 缺 source_key/product_id")
        if st not in ("granted", "revoked"):
            # 向前兼容：未知 status 保留原串，门控只认 granted
            st = st or "granted"
        grants.append({"source_key": sk, "product_id": pid, "status": st})

    return {
        "version": 1,
        "fetched_at": _now_iso(),
        "system": system,
        "grants": grants,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="从集团 /api/sync/personas/grants 拉取授权清单并写本地缓存",
    )
    ap.add_argument(
        "--base",
        default=(os.environ.get("PERSONA_SYNC_BASE") or "").strip() or DEFAULT_BASE,
        help=f"集团 base URL（缺省 env PERSONA_SYNC_BASE 或 {DEFAULT_BASE}）",
    )
    ap.add_argument(
        "--key",
        default=(os.environ.get("EVENT_INGEST_KEY") or "").strip(),
        help="Bearer 密钥（缺省 env EVENT_INGEST_KEY）",
    )
    ap.add_argument(
        "--system",
        required=True,
        choices=SOURCE_SYSTEMS,
        help="引擎 source_system：avatarhub|chengjie|huoke",
    )
    ap.add_argument(
        "--out",
        required=True,
        help="本地缓存输出路径（JSON）",
    )
    ap.add_argument(
        "--timeout",
        type=float,
        default=TIMEOUT_S,
        help=f"HTTP 超时秒（缺省 {TIMEOUT_S}）",
    )
    args = ap.parse_args(argv)

    if not args.key:
        print(
            "错误：缺少 --key / EVENT_INGEST_KEY。可重试：设置机器级密钥后重跑。",
            file=sys.stderr,
        )
        return 2

    out = Path(args.out)
    try:
        doc = fetch_grants(args.base, args.key, args.system, timeout=args.timeout)
    except Exception as e:
        print(f"fetch_grants 失败：{e}", file=sys.stderr)
        print(
            "可重试：修复网络/鉴权/服务端后再次执行本命令；"
            "旧缓存可继续供 grant_gate 离线使用（默认 warn 放行）。",
            file=sys.stderr,
        )
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    # 原子写：先写临时再 replace，避免半截缓存
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(out)

    print(
        f"OK system={args.system} count={len(doc['grants'])} "
        f"fetched_at={doc['fetched_at']} -> {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
