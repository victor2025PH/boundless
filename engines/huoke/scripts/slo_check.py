#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SLO 红线一键检查 — cron 跑, 红线 exit 1 + 可选 webhook 推送.

用法:
    python scripts/slo_check.py                        # 默认 18080, 检 + 打印 + exit
    python scripts/slo_check.py --webhook              # 红线时推 OPENCLAW_NOTIFY_WEBHOOK
    python scripts/slo_check.py --json                 # 机器可读输出 (cron 入库)

cron 推荐:
    # Linux: 每 5 min 跑一次, 红线时推 webhook
    */5 * * * * cd /path && python scripts/slo_check.py --webhook >> logs/slo.log

红线 (exit 1 + webhook):
    R1: PG / health 不通 → 业务全停
    R2: handoff 超 30min ≥ 5 → 客服跟不上, 客户冷却
    R3: push 失败率 > 30% (total ≥ 50) → coordinator/网络问题
    R4: 7 天平均加好友 ≥ 30 但今天 < 50% → 设备掉线/拉黑潮

黄线 (warning 但 exit 0):
    W1: refer 率 > 30% 或 < 5% (历史不为 0)
    W2: 客服平均响应 > 30 min
    W3: handoff 超时 1-4 个

退出码:
    0  全绿
    1  有红线 (cron failure handler 接管)
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _color(code: str, msg: str) -> str:
    if not sys.stdout.isatty():
        return msg
    return f"\033[{code}m{msg}\033[0m"


# ── HTTP ─────────────────────────────────────────────────────────────
def _http_get(base: str, path: str, api_key: str = "",
               timeout: float = 8.0) -> Optional[Any]:
    url = base.rstrip("/") + path
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def _post_webhook(url: str, title: str, markdown: str,
                   notify_type: str = "generic") -> bool:
    if not url:
        return False
    if notify_type == "dingtalk":
        body = {"msgtype": "markdown",
                "markdown": {"title": title, "text": f"### {title}\n\n{markdown}"}}
    elif notify_type == "feishu":
        body = {"msg_type": "interactive",
                "card": {
                    "header": {"title": {"tag": "plain_text", "content": title}},
                    "elements": [{"tag": "markdown", "content": markdown}],
                }}
    elif notify_type == "slack":
        body = {"text": f"*{title}*\n{markdown}"}
    else:
        body = {"title": title, "markdown": markdown,
                "text": f"{title}\n\n{markdown}"}
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8.0):
            return True
    except Exception:
        return False


# ── checks ──────────────────────────────────────────────────────────
class Check:
    def __init__(self, level: str, name: str, ok: bool,
                  message: str, advice: str = ""):
        self.level = level  # red / yellow / green
        self.name = name
        self.ok = ok
        self.message = message
        self.advice = advice

    def to_dict(self) -> Dict[str, Any]:
        return {"level": self.level, "name": self.name, "ok": self.ok,
                "message": self.message, "advice": self.advice}


def check_health(base: str, api_key: str) -> Check:
    health = _http_get(base, "/health", api_key, timeout=5.0)
    if health and health.get("status") == "ok":
        return Check("green", "health",
                     True, f"version {health.get('version')}")
    return Check("red", "health", False,
                  "/health unreachable",
                  "重启 server.py / 检查端口")


def check_central_store(base: str, api_key: str) -> Check:
    """随便调一个 store 端点, 503 即 store 坏."""
    pending = _http_get(base, "/cluster/customers/handoff/pending?limit=1",
                         api_key)
    if pending is None:
        return Check("red", "central_store", False,
                      "PG store 不可达 (503)",
                      "看 RUNBOOK §0.1 lc_messages 修复")
    return Check("green", "central_store", True, "PG store ok")


def check_handoff_breach(base: str, api_key: str) -> Check:
    pending = _http_get(base, "/cluster/customers/handoff/pending?limit=200",
                         api_key) or {}
    items = pending.get("handoffs") or []
    import time as _t
    now = _t.time()
    breach = 0
    for h in items:
        init_at = h.get("initiated_at")
        if not init_at:
            continue
        try:
            ts = datetime.datetime.fromisoformat(
                init_at.replace("Z", "+00:00")).timestamp()
            if now - ts > 1800:
                breach += 1
        except Exception:
            pass
    if breach >= 5:
        return Check("red", "handoff_breach", False,
                      f"超 30min 未接管: {breach} 个 (≥ 5 红线)",
                      "加客服或调 SLA 阈值")
    if breach >= 1:
        return Check("yellow", "handoff_breach", True,
                      f"超 30min 未接管: {breach} 个",
                      "盯盘 / 主动接管")
    return Check("green", "handoff_breach", True, "无超时")


def check_push_fail_rate(base: str, api_key: str) -> Check:
    push = _http_get(base, "/cluster/customers/push/metrics", api_key) or {}
    m = push.get("metrics") or {}
    total = int(m.get("push_total") or 0)
    fail = int(m.get("push_failure") or 0)
    if total < 50:
        return Check("green", "push_fail_rate", True,
                      f"样本不足 (total={total})")
    rate = fail / total
    if rate > 0.30:
        return Check("red", "push_fail_rate", False,
                      f"{rate:.0%} (fail={fail}/total={total})",
                      "查 coordinator 网络 / PG 健康")
    if rate > 0.15:
        return Check("yellow", "push_fail_rate", True,
                      f"{rate:.0%} (fail={fail}/total={total})")
    return Check("green", "push_fail_rate", True,
                  f"{rate:.0%} (fail={fail}/total={total})")


def check_refer_rate(base: str, api_key: str) -> Check:
    rd = _http_get(base, "/cluster/referral-decisions/aggregate?days=7",
                    api_key) or {}
    total = rd.get("total") or 0
    rate = rd.get("refer_rate") or 0.0
    if total < 20:
        return Check("green", "refer_rate", True,
                      f"样本不足 (total={total})")
    if rate > 0.30:
        return Check("yellow", "refer_rate", True,
                      f"{rate:.0%} 偏高",
                      "调高 early_refer_readiness 或 min_emotion_score")
    if rate < 0.05:
        return Check("yellow", "refer_rate", True,
                      f"{rate:.0%} 偏低",
                      "调低 delay_refer_readiness 或 min_turns")
    return Check("green", "refer_rate", True, f"{rate:.0%} (健康区 5-30%)")


def check_daily_friend_request(base: str, api_key: str) -> Check:
    """对比 7 天均值与今天."""
    funnel = _http_get(base, "/cluster/customers/funnel/timeseries?days=7",
                        api_key) or {}
    series = funnel.get("series") or []
    if len(series) < 4:
        return Check("green", "daily_friend_request", True,
                      f"样本不足 (天数={len(series)})")
    vals = [int(s.get("friend_request_sent") or 0) for s in series[:-1]]
    today = int(series[-1].get("friend_request_sent") or 0)
    avg = sum(vals) / len(vals) if vals else 0
    if avg >= 30 and today < avg * 0.5:
        return Check("red", "daily_friend_request", False,
                      f"今 {today} ≪ 7 天均值 {avg:.0f}",
                      "查设备在线 / 拉黑潮 / 网络")
    if avg >= 10 and today < avg * 0.7:
        return Check("yellow", "daily_friend_request", True,
                      f"今 {today} 低于均值 {avg:.0f}")
    return Check("green", "daily_friend_request", True,
                  f"今 {today} (均值 {avg:.0f})")


def check_sla_response(base: str, api_key: str) -> Check:
    sla = _http_get(base, "/cluster/customers/sla/agents?days=7",
                     api_key) or {}
    agents = sla.get("agents") or []
    accept_n_total = 0
    weighted = 0.0
    for a in agents:
        n = int(a.get("accept_n") or 0)
        m = a.get("avg_accept_minutes")
        if n > 0 and m is not None:
            accept_n_total += n
            weighted += float(m) * n
    if accept_n_total < 5:
        return Check("green", "sla_response", True,
                      f"样本不足 (n={accept_n_total})")
    avg_min = weighted / accept_n_total
    if avg_min > 30:
        return Check("yellow", "sla_response", True,
                      f"平均 {avg_min:.1f} min (> 30)",
                      "加客服 / 调班次")
    if avg_min > 60:
        return Check("red", "sla_response", False,
                      f"平均 {avg_min:.1f} min (> 60)",
                      "客服明显跟不上, 立即加人")
    return Check("green", "sla_response", True,
                  f"{avg_min:.1f} min (n={accept_n_total})")


# ── runner ──────────────────────────────────────────────────────────
def run_all(base: str, api_key: str) -> List[Check]:
    return [
        check_health(base, api_key),
        check_central_store(base, api_key),
        check_handoff_breach(base, api_key),
        check_push_fail_rate(base, api_key),
        check_refer_rate(base, api_key),
        check_daily_friend_request(base, api_key),
        check_sla_response(base, api_key),
    ]


def render_text(checks: List[Check]) -> str:
    lines = []
    icons = {"red": "🔴", "yellow": "🟡", "green": "🟢"}
    for c in checks:
        line = f"{icons[c.level]} {c.name:24s} {c.message}"
        if c.advice:
            line += f"  💡 {c.advice}"
        lines.append(line)
    return "\n".join(lines)


def render_markdown_alert(checks: List[Check]) -> str:
    """webhook 推时只显示 red/yellow."""
    issues = [c for c in checks if c.level in ("red", "yellow")]
    if not issues:
        return "全部健康 ✅"
    lines = []
    icons = {"red": "🔴", "yellow": "🟡"}
    for c in issues:
        line = f"- {icons[c.level]} **{c.name}**: {c.message}"
        if c.advice:
            line += f"  💡 {c.advice}"
        lines.append(line)
    return "\n".join(lines)


# ── main ────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", default=os.environ.get(
        "OPENCLAW_E2E_BASE", "http://127.0.0.1:18080"))
    p.add_argument("--api-key", default=os.environ.get("OPENCLAW_API_KEY", ""))
    p.add_argument("--webhook", action="store_true",
                   help="红线 / 黄线时推 OPENCLAW_NOTIFY_WEBHOOK")
    p.add_argument("--json", action="store_true",
                   help="JSON 输出 (机器可读)")
    args = p.parse_args()

    checks = run_all(args.base, args.api_key)
    has_red = any(c.level == "red" for c in checks)
    has_issue = any(c.level in ("red", "yellow") for c in checks)

    if args.json:
        print(json.dumps([c.to_dict() for c in checks],
                         ensure_ascii=False, indent=2))
    else:
        print(_color("1;36",
                      f"SLO check · {args.base} · "
                      f"{datetime.datetime.now().strftime('%H:%M:%S')}"))
        print(render_text(checks))
        print()
        if has_red:
            print(_color("31;1", "❌ 有红线, exit 1"))
        elif has_issue:
            print(_color("33", "⚠️  有黄线, exit 0"))
        else:
            print(_color("32;1", "✅ 全绿"))

    # webhook 推
    if args.webhook and has_issue:
        wb = (os.environ.get("OPENCLAW_NOTIFY_WEBHOOK") or "").strip()
        notify_type = (os.environ.get("OPENCLAW_NOTIFY_TYPE")
                        or "generic").strip().lower()
        if wb:
            title = ("🔴 SLO 红线" if has_red else "🟡 SLO 黄线")
            ok = _post_webhook(wb, title,
                                render_markdown_alert(checks), notify_type)
            if ok:
                print(_color("90", f"  webhook sent ({notify_type})"))

    return 1 if has_red else 0


if __name__ == "__main__":
    sys.exit(main())
