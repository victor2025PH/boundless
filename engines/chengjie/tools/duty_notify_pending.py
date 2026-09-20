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

    python tools/duty_notify_pending.py --alert    # 积压超阈值 → 值守告警（计划任务 DutyNotifyBacklog）

``--alert``（I-6 F1①，2026-09-04）：61 单积压事故的直接教训——回访只在处置台
``/status`` 路由自动触发，修复线用 ``set_ticket_status``/``duty_reply --status fixed``
标 fixed 不会发任何通知，而本工具此前**没有任何计划任务调用**，积压从 08-30 静默
长到 09-04 才被人肉对账发现。现在按 ``backlog_verdict``（积压 ≥3 单且最老 ≥24h，
12h 去抖）经 tools/duty_alert 轰值守；**刻意不自动 flush**：逐单推 N 条是轰炸，且
§G 要求回访前先核报障人版本——发不发、怎么发（逐单 / 汇总 duty_notify_summary）
永远由人决定，机器只负责「别让它再静默躺着」。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._data_root import resolve_data_roots  # noqa: E402
from tools.duty_reply import read_admin_token  # noqa: E402

DEFAULT_BASE = "http://127.0.0.1:18799"
ALERT_STATE = Path(r"D:\chengjie-instances\.ops\duty_notify_backlog.state.json")


def backlog_verdict(rows: list, now: float, *, min_count: int = 3,
                    min_age_h: float = 24.0, last_alert_ts: float = 0.0,
                    re_alert_h: float = 12.0) -> tuple:
    """纯函数：(是否告警, 最老积压小时数, 积压条数)。

    积压 ≥min_count **且**最老一单 ≥min_age_h 小时才响（刚标 fixed 等发版的单不算
    事故）；距上次告警 <re_alert_h 小时不重提（去抖，防「告警变背景音」）。
    最老口径用 ``updated_ts``（标 fixed 时刷新；后续补 note 会把它推新，故是下界）。
    """
    n = len(rows)
    if n == 0:
        return False, 0.0, 0
    oldest = min(float(r.get("updated_ts") or now) for r in rows)
    oldest_h = max(0.0, (float(now) - oldest) / 3600.0)
    if n < int(min_count) or oldest_h < float(min_age_h):
        return False, oldest_h, n
    if last_alert_ts and float(now) - float(last_alert_ts) < float(re_alert_h) * 3600.0:
        return False, oldest_h, n
    return True, oldest_h, n


def _by_reporter(rows: list) -> str:
    cnt: dict = {}
    for t in rows:
        k = str(t.get("reporter_name") or t.get("reporter_id") or "?")
        cnt[k] = cnt.get(k, 0) + 1
    return "、".join(f"{k} {v} 单" for k, v in sorted(cnt.items(), key=lambda kv: -kv[1]))


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
    ap.add_argument("--alert", action="store_true",
                    help="积压超阈值时经 duty_alert 告警值守（不发群、不写台账）")
    ap.add_argument("--alert-min-count", type=int, default=3)
    ap.add_argument("--alert-min-age-h", type=float, default=24.0)
    ap.add_argument("--alert-re-h", type=float, default=12.0)
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
    if args.alert:
        now = time.time()
        last_ts = 0.0
        try:
            if ALERT_STATE.is_file():
                last_ts = float(json.loads(
                    ALERT_STATE.read_text(encoding="utf-8")).get("last_alert_ts") or 0)
        except Exception:
            last_ts = 0.0
        fire, oldest_h, n = backlog_verdict(
            rows, now, min_count=args.alert_min_count,
            min_age_h=args.alert_min_age_h, last_alert_ts=last_ts,
            re_alert_h=args.alert_re_h)
        _out(f"[alert-check] 积压 {n} 单，最老 {oldest_h:.1f}h，"
             f"上次告警 {time.strftime('%m-%d %H:%M', time.localtime(last_ts)) if last_ts else '-'}"
             f" → {'ALERT' if fire else 'quiet'}")
        if not fire:
            return 0
        text = (f"[回访积压] {n} 单已标 fixed 但从未回访（最老 {oldest_h:.0f}h）：{_by_reporter(rows)}\n"
                "标 fixed 走 CLI 不会自动回访，别等用户复报。处置（先核 §G 报障人版本）：\n"
                "· 少量逐单：python tools\\duty_notify_pending.py --flush\n"
                "· 一人多单：群里发一条汇总回访 + python tools\\duty_notify_summary.py --reporter <id> --apply")
        try:
            from tools.duty_alert import deliver
            ok, note = deliver(text)
        except Exception as exc:  # noqa: BLE001
            ok, note = False, str(exc)[:120]
        _out(f"[alert] deliver ok={ok} {note}")
        if ok:
            ALERT_STATE.parent.mkdir(parents=True, exist_ok=True)
            ALERT_STATE.write_text(json.dumps({"last_alert_ts": now, "n": n,
                                               "oldest_h": round(oldest_h, 1)}),
                                   encoding="utf-8")
        return 0 if ok else 1
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
