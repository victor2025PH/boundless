# -*- coding: utf-8 -*-
"""报障验证按钮回调拉取器（实施82 P2，2026-08-29）。

闭环最后一段：用户在群里点 bot 回访的「✅ 修好了 / ❌ 还是不行」→ 官网
webhook 即时应答并落盘（bd2026.cc /api/telegram/bug-callbacks）→ **本工具
定时拉取回写工单**：y → verified（关单口径与文字「好了」一致）；n →
confirmed + 台账 note + 告警值守（@ai_zkw）——按钮点了没人跟进比没按钮更伤。

    python tools/duty_callback_poll.py --dry-run   # 只拉取打印
    python tools/duty_callback_poll.py             # 拉取 + 回写 + 告警

鉴权＝`x-bug-key`（sha256(bot token) 前 32 位，双方从既有共享 token 派生，
零新密钥）；水位 `.ops/bug_callback_poll.state.json`（按事件 ts 增量，
重复拉到的旧事件天然跳过）。计划任务 **DutyCallbackPoll** 每 5 分钟——
用户侧体感（toast+按钮收起）在点击瞬间已由 webhook 完成，这 5 分钟只是
后台账本同步延迟，用户不可见。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._data_root import resolve_data_roots  # noqa: E402

DEFAULT_SITE = "https://bd2026.cc"
STATE_PATH = Path(r"D:\chengjie-instances\.ops\bug_callback_poll.state.json")


def _out(s: str) -> None:
    sys.stdout.buffer.write((s + "\n").encode("utf-8"))
    sys.stdout.flush()


# ── 纯函数（门禁 tests/test_duty_callback_poll.py）──────────────────────────

def derive_pull_key(token: str) -> str:
    """与官网 bug-callback-store.bugPullKey 同式派生（sha256 hex 前 32）。"""
    t = str(token or "")
    if not t:
        return ""
    return hashlib.sha256(t.encode("utf-8")).hexdigest()[:32]


def decide(evt: Dict[str, Any]) -> Tuple[str, str, bool]:
    """事件 → (新状态, 台账 note, 是否告警值守)。

    y=verified（与文字口令「好了」同关单口径）；n=confirmed + 告警——
    「还是不行」是重开信号，必须有人接住。who 尽量人话（用户名>名>id）。
    """
    who = str(evt.get("username") or "").strip()
    who = ("@" + who) if who else str(
        evt.get("first_name") or evt.get("from_id") or "?")
    if str(evt.get("verdict")) == "y":
        return "verified", f"[按钮验证] {who} 点了「修好了」，已确认修复", False
    return "confirmed", f"[按钮验证] {who} 点了「还是不行」，转回跟进", True


def pick_new(events: List[Dict[str, Any]], watermark: float) -> List[Dict[str, Any]]:
    out = [e for e in (events or [])
           if isinstance(e, dict) and float(e.get("ts") or 0) > watermark
           and int(e.get("ticket") or 0) > 0]
    out.sort(key=lambda e: float(e.get("ts") or 0))
    return out


# ── IO ───────────────────────────────────────────────────────────────────────

def _read_bot_token(data_root: Path) -> str:
    try:
        arr = json.loads((Path(data_root) / "config" / "notify_webhooks.json")
                         .read_text(encoding="utf-8"))
        for ch in arr if isinstance(arr, list) else []:
            if str(ch.get("format")) == "telegram" and ch.get("token"):
                return str(ch["token"])
    except Exception:
        pass
    return ""


def _fetch(site: str, key: str, since: float) -> Dict[str, Any]:
    req = urllib.request.Request(
        f"{site.rstrip('/')}/api/telegram/bug-callbacks?since={since}",
        headers={"x-bug-key": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="报障验证按钮回调拉取回写")
    ap.add_argument("--site", default=DEFAULT_SITE)
    ap.add_argument("--data-root", default="")
    ap.add_argument("--state", default=str(STATE_PATH))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    data_root = resolve_data_roots(args.data_root)[0]
    token = _read_bot_token(data_root)
    key = derive_pull_key(token)
    if not key:
        _out("[err] 未读到 bot token（notify_webhooks.json telegram 渠道）")
        return 1

    state_path = Path(args.state)
    watermark = 0.0
    try:
        if state_path.is_file():
            watermark = float(json.loads(
                state_path.read_text(encoding="utf-8")).get("watermark") or 0)
    except Exception:
        watermark = 0.0

    try:
        resp = _fetch(args.site, key, watermark)
    except Exception as exc:  # noqa: BLE001
        _out(f"[err] 拉取失败：{str(exc)[:160]}")
        return 1
    events = pick_new(resp.get("events") or [], watermark)
    if not events:
        _out(f"[ok] 无新回调（水位 {watermark}）")
        return 0
    _out(f"[pull] {len(events)} 条新回调")

    if args.dry_run:
        for e in events:
            st, note, alert = decide(e)
            _out(f"  #{e.get('ticket')} -> {st}（{note}）"
                 + ("＋告警" if alert else ""))
        _out("[dry-run] 未回写、未推进水位。")
        return 0

    os.environ.setdefault("AITR_DATA_DIR", str(data_root))
    from src.ops.bug_intake import append_ticket_note, set_ticket_status
    alerts: List[str] = []
    for e in events:
        st, note, alert = decide(e)
        tid = int(e.get("ticket") or 0)
        ok = set_ticket_status(tid, st)
        append_ticket_note(tid, note)
        _out(f"[apply] #{tid} -> {st} {'✅' if ok else '❌ 工单不存在?'}")
        if alert:
            alerts.append(f"🔁 工单 #{tid}：{note}")
        watermark = max(watermark, float(e.get("ts") or 0))

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"watermark": watermark}),
                          encoding="utf-8")
    if alerts:
        try:
            from tools.duty_alert import deliver
            deliver("[按钮验证] 有用户反馈修复未生效：\n" + "\n".join(alerts)
                    + "\n处置台：/admin/bug-tickets")
        except Exception as exc:  # noqa: BLE001
            _out(f"[warn] 告警投递失败：{exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
