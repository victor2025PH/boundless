# -*- coding: utf-8 -*-
"""修复回访积压查看/冲刷 CLI（实施81 P0-3b，2026-08-28）。

「修好了用户不知道」的最后一公里：工单标 fixed 时若支持号 worker 离线，
回访发不出去（notify_ts=0 积压）。hotpatch 推送完 / worker 回来后跑本工具：

    python tools/duty_notify_pending.py            # 列出积压（只读）
    python tools/duty_notify_pending.py --flush    # 补发全部积压回访（真发群消息）

flush 走引擎既有端点 ``POST /api/admin/bug-intake/notify-pending``（逐单串行
+ 条间 1.5s 防刷屏，单条失败不中止整批）；本工具不自造发送路径。
deploy/desktop/push_chatx_hotpatch.ps1 推完补丁会提示（或经 -NotifyPending
直接调用）——把「补丁到机」和「告诉报障人」连成一条线。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._data_root import resolve_data_roots  # noqa: E402
from tools.duty_reply import read_admin_token  # noqa: E402

DEFAULT_BASE = "http://127.0.0.1:18799"


def _out(s: str) -> None:
    sys.stdout.buffer.write((s + "\n").encode("utf-8"))
    sys.stdout.flush()


def _req(base: str, path: str, token: str, method: str = "GET",
         params: dict | None = None, timeout: int = 120):
    url = base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, method=method, data=b"{}" if method == "POST" else None,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, {}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)[:200]}


def pending_rows(tickets: list) -> list:
    """fixed 且回访未送达的行（webuser 工单无 TG 会话可发，口径与引擎
    list_pending_notify 一致——两边判据漂移会让「积压 N」与真冲刷数对不上）。"""
    out = []
    for t in tickets or []:
        if not isinstance(t, dict):
            continue
        if str(t.get("status")) != "fixed":
            continue
        if float(t.get("notify_ts") or 0) > 0:
            continue
        if str(t.get("chat_id") or "").startswith("webuser:"):
            continue
        out.append(t)
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="修复回访积压查看/冲刷")
    ap.add_argument("--flush", action="store_true",
                    help="补发全部积压回访（真发群消息）")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default="")
    args = ap.parse_args()

    data_root = resolve_data_roots(args.data_root)[0]
    token = read_admin_token(data_root)
    if not token:
        _out(f"[err] 未读到 web_admin.auth_token（{data_root}）")
        return 1

    code, d = _req(args.base, "/api/admin/bug-intake", token,
                   params={"status": "fixed", "limit": "100"})
    if code != 200:
        _out(f"[err] 工单列表读取失败 HTTP {code} {json.dumps(d, ensure_ascii=False)[:160]}")
        return 1
    rows = pending_rows(d.get("tickets") or [])
    if not rows:
        _out("[ok] 无回访积压（fixed 单都已回访）")
        return 0
    _out(f"[pending] {len(rows)} 单已修复但回访未送达：")
    for t in rows:
        _out(f"  #{t.get('id')} [{t.get('severity')}] "
             f"{str(t.get('title') or '')[:60]} · {t.get('reporter_name') or t.get('reporter_id')}"
             + (f" · 上次失败:{t.get('notify_note')}" if t.get("notify_note") else ""))
    if not args.flush:
        _out("[hint] 补发：python tools/duty_notify_pending.py --flush")
        return 0
    code, d = _req(args.base, "/api/admin/bug-intake/notify-pending", token,
                   method="POST")
    if code != 200:
        _out(f"[err] 冲刷失败 HTTP {code}")
        return 1
    results = d.get("results") or []
    ok_n = sum(1 for r in results if r.get("notified"))
    _out(f"[flush] 共 {d.get('total')} 单，成功 {ok_n}：")
    for r in results:
        _out(f"  #{r.get('ticket_id')} {'✅' if r.get('notified') else '❌ ' + str(r.get('note') or '')}")
    return 0 if ok_n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
