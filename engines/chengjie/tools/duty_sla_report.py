# -*- coding: utf-8 -*-
"""报障群「首响时延」SLA 报告（实施81 P2-1，2026-08-28）。

值守缺位看门狗（duty_watchdog）只管「现在有没有人晾着」；本工具回答老板要的
数字：**内测群的提问平均多久有人应答、多少条超了 30 分钟、最坏晾了多久**——
「内测响应 SLA」从体感变成周报口径。

    python tools/duty_sla_report.py                 # 近 7 天，打印
    python tools/duty_sla_report.py --days 14 --json
    python tools/duty_sla_report.py --out-jsonl logs/eval/duty_sla_trend.jsonl
    python tools/duty_sla_report.py --notify        # 摘要投递 @ai_zkw（老板）

口径（纯函数，门禁 tests/test_duty_sla_report.py）：
- 「提问 burst」＝连续客户入站折叠成一段，**按首条计等待**（客户视角最坏值，
  与 reply_latency SSOT 同哲学）；支持号出站或员工亲号入站即闭合。
- 「超时」＝首响 > threshold_min（默认 30，与 duty_watchdog 同刻度）。
- 「未应答」＝窗口内开口至今无任何应答（修复回归时此数先涨）。
- 数据面＝thread API 只读（每群近 500 条，报障群量级下覆盖一周有余；
  截断时如实标 partial，不装完整）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._data_root import load_merged_config, resolve_data_roots  # noqa: E402

ENGINE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE = "http://127.0.0.1:18799"
GROUP_NAMES = {"-1004345824259": "内测bug群", "-1004290740529": "官方报障群"}
# 官方 bot（实施82 起代发回访/公示）＝官方应答方，与 duty_watchdog 同口径
OFFICIAL_BOT_IDS = {"8506426282"}


def _out(s: str) -> None:
    sys.stdout.buffer.write((s + "\n").encode("utf-8"))
    sys.stdout.flush()


# ── 纯函数 ───────────────────────────────────────────────────────────────────

def pair_first_response(
    messages: List[Dict[str, Any]], *, staff_ids: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """消息流 → 提问 burst 配对表 [{ask_ts, resp_ts|None, text, sender}]。

    连续客户入站折叠为一段（按**首条**计等待）；支持号出站 / 员工亲号入站闭合
    当前段；员工消息不开新段；空壳行（无文本无媒体）忽略。输入顺序不限。
    """
    staff = {str(x) for x in (staff_ids or set())}
    pairs: List[Dict[str, Any]] = []
    open_burst: Optional[Dict[str, Any]] = None
    for m in sorted(messages or [], key=lambda x: float(x.get("ts") or 0)):
        if not isinstance(m, dict):
            continue
        direction = str(m.get("direction") or "")
        sender = str(m.get("sender_id") or "")
        ts = float(m.get("ts") or 0)
        is_staff_reply = direction == "out" or (
            direction == "in" and sender and sender in staff)
        if is_staff_reply:
            if open_burst is not None:
                open_burst["resp_ts"] = ts
                open_burst = None
            continue
        if direction != "in":
            continue
        text = str(m.get("text") or "").strip()
        if not text and not str(m.get("media_type") or ""):
            continue
        if open_burst is None:
            open_burst = {
                "ask_ts": ts, "resp_ts": None,
                "text": (text or f"[{m.get('media_type') or '媒体'}]")[:80],
                "sender": str(m.get("sender_name") or sender or "?"),
            }
            pairs.append(open_burst)
    return pairs


def _percentile(sorted_vals: List[float], p: float) -> float:
    """线性插值分位（零依赖；与 proactive_pacing 同法）。空列表返回 0。"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    frac = k - lo
    return float(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac)


def summarize(pairs: List[Dict[str, Any]], *, now: float, days: int,
              threshold_min: int = 30) -> Dict[str, Any]:
    """窗口内 burst → SLA 统计。latency 单位分钟（1 位小数）。"""
    since = now - days * 86400
    window = [p for p in (pairs or []) if float(p.get("ask_ts") or 0) >= since]
    lat: List[float] = []
    unanswered: List[Dict[str, Any]] = []
    for p in window:
        if p.get("resp_ts") is not None:
            lat.append((float(p["resp_ts"]) - float(p["ask_ts"])) / 60.0)
        else:
            unanswered.append(p)
    lat.sort()
    over = sum(1 for v in lat if v > threshold_min)
    # 未应答的「已等时长」也参与超时统计（等了 3 小时没人理不算超时才是假账）
    over += sum(1 for p in unanswered
                if (now - float(p["ask_ts"])) / 60.0 > threshold_min)
    n_all = len(window)
    return {
        "bursts": n_all,
        "answered": len(lat),
        "unanswered": len(unanswered),
        "p50_min": round(_percentile(lat, 50), 1),
        "p95_min": round(_percentile(lat, 95), 1),
        "max_min": round(lat[-1], 1) if lat else 0.0,
        "over_threshold": over,
        "over_rate": round(over / n_all, 3) if n_all else 0.0,
        "threshold_min": threshold_min,
        "window_days": days,
    }


def render_report(per_group: Dict[str, Dict[str, Any]], total: Dict[str, Any],
                  *, now: Optional[float] = None,
                  partial_groups: Optional[set] = None) -> str:
    day = time.strftime("%m-%d", time.localtime(now if now is not None else time.time()))
    th = total.get("threshold_min", 30)
    lines = [f"[值守SLA] {day} 报障群首响时延（近 {total.get('window_days')} 天，阈值 {th} 分钟）"]
    for chat_key, s in per_group.items():
        gname = GROUP_NAMES.get(str(chat_key), str(chat_key))
        flag = "🟢" if s["over_threshold"] == 0 else "🔴"
        part = "（数据截断，仅近端）" if partial_groups and chat_key in partial_groups else ""
        lines.append(
            f"{flag} {gname}{part}：提问 {s['bursts']} · 已答 {s['answered']}"
            f" · p50 {s['p50_min']}m · p95 {s['p95_min']}m · 最坏 {s['max_min']}m"
            f" · 超{th}m {s['over_threshold']} 条"
            + (f" · 未应答 {s['unanswered']}" if s["unanswered"] else ""))
    lines.append(
        f"合计：提问 {total['bursts']} · 超时率 {round(total['over_rate'] * 100, 1)}%"
        f" · 未应答 {total['unanswered']}")
    return "\n".join(lines)


def merge_totals(per_group: Dict[str, Dict[str, Any]], *, days: int,
                 threshold_min: int) -> Dict[str, Any]:
    tot = {"bursts": 0, "answered": 0, "unanswered": 0, "over_threshold": 0,
           "threshold_min": threshold_min, "window_days": days}
    for s in per_group.values():
        for k in ("bursts", "answered", "unanswered", "over_threshold"):
            tot[k] += int(s.get(k) or 0)
    tot["over_rate"] = (round(tot["over_threshold"] / tot["bursts"], 3)
                        if tot["bursts"] else 0.0)
    return tot


# ── 数据读取 ─────────────────────────────────────────────────────────────────

def _read_token(data_root: Path) -> str:
    try:
        import yaml
    except Exception:
        return ""
    for name in ("config.local.yaml", "config.yaml"):
        fp = Path(data_root) / "config" / name
        if not fp.is_file():
            continue
        try:
            cfg = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        tok = str((cfg.get("web_admin") or {}).get("auth_token") or "")
        if tok:
            return tok
    return ""


def _get_json(base: str, path: str, token: str, params: Dict[str, str],
              timeout: int = 30) -> Dict[str, Any]:
    url = base.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="报障群首响时延 SLA 报告（只读）")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--threshold-min", type=int, default=30)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out-jsonl", default="",
                    help="趋势行追加落点（如 logs/eval/duty_sla_trend.jsonl）")
    ap.add_argument("--notify", action="store_true",
                    help="摘要投递 @ai_zkw（周批用；tools/duty_alert.py 通道）")
    args = ap.parse_args()

    data_root = resolve_data_roots(args.data_root)[0]
    cfg = load_merged_config(data_root)
    bi = (cfg.get("bug_intake") or {}) if isinstance(cfg, dict) else {}
    groups = [str(g).strip() for g in (bi.get("groups") or []) if str(g).strip()]
    staff = {str(a).strip() for a in (bi.get("support_accounts") or [])
             if str(a).strip()}
    staff |= OFFICIAL_BOT_IDS          # bot 的回访/公示＝官方应答（同 watchdog）
    if not groups:
        _out("[skip] bug_intake.groups 为空（未启用报障群值守）")
        return 0
    account = sorted(staff)[0] if staff else "default"
    token = _read_token(data_root)
    if not token:
        _out(f"[err] 未读到 web_admin.auth_token（{data_root}）")
        return 1

    now = time.time()
    per_group: Dict[str, Dict[str, Any]] = {}
    partial: set = set()
    for chat_key in groups:
        try:
            d = _get_json(args.base, "/api/unified-inbox/thread", token, {
                "platform": "telegram", "account_id": account,
                "chat_key": chat_key, "limit": "500"})
        except Exception as exc:  # noqa: BLE001
            _out(f"[warn] 读取 {chat_key} 线程失败：{str(exc)[:120]}")
            continue
        msgs = [m for m in (d.get("messages") or []) if isinstance(m, dict)]
        # 截断判定：拿满 500 且最旧一条仍在窗口内 → 更旧的提问看不见，如实标注
        if len(msgs) >= 500:
            oldest = min(float(m.get("ts") or 0) for m in msgs)
            if oldest > now - args.days * 86400:
                partial.add(chat_key)
        pairs = pair_first_response(msgs, staff_ids=staff)
        per_group[chat_key] = summarize(
            pairs, now=now, days=args.days, threshold_min=args.threshold_min)
    if not per_group:
        _out("[err] 所有群线程都读不到")
        return 1
    total = merge_totals(per_group, days=args.days,
                         threshold_min=args.threshold_min)

    if args.json:
        _out(json.dumps({"groups": per_group, "total": total,
                         "partial": sorted(partial)}, ensure_ascii=False,
                        indent=1))
    else:
        _out(render_report(per_group, total, now=now, partial_groups=partial))

    if args.out_jsonl:
        row = {"day": time.strftime("%Y-%m-%d", time.localtime(now)),
               "ts": round(now, 1), "groups": per_group, "total": total,
               "partial": sorted(partial)}
        p = Path(args.out_jsonl)
        if not p.is_absolute():
            p = ENGINE_ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        _out(f"[trend] appended -> {p}")

    if args.notify:
        from tools.duty_alert import deliver
        text = render_report(per_group, total, now=now, partial_groups=partial)
        ok, note = deliver(text)
        _out(f"[deliver] {'ok' if ok else 'FAIL'} {note}")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
