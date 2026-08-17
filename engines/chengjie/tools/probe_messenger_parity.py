#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Messenger 对等态只读探针（P4）——不发消息、不触发重登。

用法（引擎根）::
    python tools/probe_messenger_parity.py
    python tools/probe_messenger_parity.py --json

检查：
1. messenger-web ``/accounts``：hint / e2ee_ratio / conv_count
2. 智聊 metrics ``platform_sessions``：inbox_stalled / stall_funnel
3. 可选：实例 inbox.db 抽样 messenger 消息是否带 ``platform_msg_id``
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


def _get_json(url: str, timeout: float = 8.0) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _probe_node(base: str) -> Dict[str, Any]:
    data = _get_json(f"{base.rstrip('/')}/accounts")
    rows = []
    for a in (data.get("accounts") or []):
        rows.append({
            "account_id": a.get("account_id") or a.get("id") or "",
            "inbox_hint_code": a.get("inbox_hint_code") or "",
            "e2ee_ratio": a.get("e2ee_ratio"),
            "conv_count": a.get("conv_count"),
            "unread": a.get("unread"),
            "status": a.get("status") or "",
        })
    return {"ok": True, "accounts": rows}


def _probe_metrics(metrics_url: str, token: str = "") -> Dict[str, Any]:
    req = urllib.request.Request(
        metrics_url, headers={"Accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    ps = (data or {}).get("platform_sessions") or {}
    return {
        "ok": True,
        "inbox_stalled": list(ps.get("inbox_stalled") or []),
        "stall_funnel": ps.get("stall_funnel") or {},
        "inbox_health": {
            k: {
                "stall_kind": (v or {}).get("stall_kind"),
                "hint_code": (v or {}).get("hint_code"),
                "e2ee_ratio": (v or {}).get("e2ee_ratio"),
                "stall_since": (v or {}).get("stall_since"),
            }
            for k, v in (ps.get("inbox_health") or {}).items()
        },
    }


def _probe_store(db_path: Path, *, limit: int = 40) -> Dict[str, Any]:
    if not db_path.is_file():
        return {"ok": False, "error": f"missing {db_path}"}
    uri = f"file:{db_path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        cur = con.execute(
            "SELECT conversation_id, platform_msg_id, direction, substr(text,1,40) "
            "FROM messages WHERE conversation_id LIKE 'messenger:%' "
            "ORDER BY ts DESC LIMIT ?",
            (int(limit),),
        )
        rows = cur.fetchall()
    finally:
        con.close()
    with_id = sum(1 for r in rows if (r[1] or "").strip())
    return {
        "ok": True,
        "sampled": len(rows),
        "with_platform_msg_id": with_id,
        "ratio": (with_id / len(rows)) if rows else None,
        "examples": [
            {"cid": r[0], "pmid": r[1] or "", "dir": r[2], "text": r[3] or ""}
            for r in rows[:5]
        ],
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--node", default="http://127.0.0.1:8791")
    ap.add_argument("--metrics",
                    default="http://127.0.0.1:18799/api/workspace/metrics")
    ap.add_argument("--token", default=os.environ.get("AITR_AUTH_TOKEN", ""))
    ap.add_argument("--data-root",
                    default=os.environ.get(
                        "AITR_DATA_ROOT",
                        r"D:\chengjie-instances\zhiliao\data"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    out: Dict[str, Any] = {"node": None, "metrics": None, "store": None}
    try:
        out["node"] = _probe_node(args.node)
    except Exception as exc:
        out["node"] = {"ok": False, "error": str(exc)}
    out["metrics"] = _probe_metrics(args.metrics, token=args.token)
    db = Path(args.data_root) / "config" / "inbox.db"
    if not db.is_file():
        db = Path(args.data_root) / "inbox.db"
    out["store"] = _probe_store(db)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out["node"].get("ok") else 1

    def _safe(s: str) -> None:
        try:
            print(s)
        except UnicodeEncodeError:
            sys.stdout.buffer.write((s + "\n").encode("utf-8", "replace"))

    _safe("=== messenger parity probe (read-only) ===")
    n = out["node"]
    if not n.get("ok"):
        _safe(f"[node] FAIL {n.get('error')}")
    else:
        _safe(f"[node] {len(n['accounts'])} account(s)")
        for a in n["accounts"]:
            _safe(
                f"  {a['account_id']}: hint={a['inbox_hint_code'] or '-'} "
                f"e2ee={a['e2ee_ratio']} convs={a['conv_count']} "
                f"unread={a['unread']}")
    m = out["metrics"]
    if not m.get("ok"):
        _safe(f"[metrics] SKIP/FAIL {m.get('error')} "
              "(login cookie or --token may be required)")
    else:
        _safe(f"[metrics] stalled={m['inbox_stalled']} "
              f"funnel={m['stall_funnel']}")
        for k, v in (m.get("inbox_health") or {}).items():
            _safe(f"  {k}: {v}")
    s = out["store"]
    if not s.get("ok"):
        _safe(f"[store] SKIP {s.get('error')}")
    else:
        ratio = s.get("ratio")
        note = ""
        if ratio is not None and ratio < 0.1:
            note = "  # low=historical debt or preview-path empty id; not live fail"
        _safe(
            f"[store] sampled={s['sampled']} "
            f"with_msg_id={s['with_platform_msg_id']} ratio={ratio}{note}")
        for ex in s.get("examples") or []:
            _safe(
                f"  {ex['pmid'] or '(no id)'} {ex['dir']} "
                f"{(ex.get('text') or '')[:40]!r}")
    return 0 if n.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
