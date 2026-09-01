# -*- coding: utf-8 -*-
"""值守内部告警统一投递（实施81 P2 补，2026-08-29 老板指定收件人 @ai_zkw）。

消费方＝duty_watchdog（值守缺位告警）+ duty_sla_report --notify（SLA 周报）。
此前两工具复用 SeatLogMonitor 的投递链 → 落 katie(8244899900/@Sousaun) 信箱；
老板 0829 拍板：**值守内部告警投他本人 @ai_zkw（5433982810）**。

投递链：
1. 主通道：@tgzkw_bot 私聊 @ai_zkw（复用 monitor_seat_logs 的 bot 机制，
   经 chat_id 参数改目标；bot token 从 notify_webhooks.json 读，不入库）。
   前提＝@ai_zkw 对该 bot /start 过，否则 TG 拒投（chat not found）。
2. 回落：支持号 6834964252 直接私聊 @ai_zkw（两号既有对话，实例在线即达；
   走 unified-inbox send，复用 duty_reply 的 token/幂等键机制）。

SeatLogMonitor 自身（坐席日志巡检）**保持投 @Sousaun 不动**——那是它上线时
的既有约定，要不要跟着切由老板另行拍板。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ENGINE_ROOT = Path(__file__).resolve().parent.parent
SEAT_MONITOR = ENGINE_ROOT.parent.parent / "deploy" / "desktop" / "monitor_seat_logs.py"

# 内部告警收件人：@ai_zkw（老板本人，2026-08-29 拍板）
DUTY_ALERT_TG_ID = "5433982810"
# 回落发件账号：报障群支持号（与 @ai_zkw 有既有私聊）
FALLBACK_ACCOUNT = "6834964252"
INSTANCE_BASE = "http://127.0.0.1:18799"


def _load_monitor():
    spec = importlib.util.spec_from_file_location(
        "monitor_seat_logs", str(SEAT_MONITOR))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _fallback_instance_dm(text: str) -> Tuple[bool, str]:
    """支持号 → @ai_zkw 私聊（unified-inbox send；skip_translate 防告警被翻译）。"""
    from scripts._data_root import resolve_data_roots
    from tools.duty_reply import build_send_payload, read_admin_token

    data_root = resolve_data_roots("")[0]
    tok = read_admin_token(data_root)
    if not tok:
        return False, "no web_admin token"
    payload = build_send_payload(
        DUTY_ALERT_TG_ID, text, account_id=FALLBACK_ACCOUNT,
        client_msg_id=f"dutyalert-{uuid.uuid4().hex[:8]}")
    req = urllib.request.Request(
        INSTANCE_BASE + "/api/unified-inbox/send",
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {tok}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode("utf-8", "replace"))
        if isinstance(resp, dict) and resp.get("ok") is False:
            return False, f"instance resp={str(resp)[:120]}"
        return True, "instance dm ok"
    except Exception as e:  # noqa: BLE001
        return False, f"instance err={str(e)[:120]}"


def deliver(text: str) -> Tuple[bool, str]:
    """bot 直投 @ai_zkw → 失败回落支持号私聊。返回 (ok, note)。"""
    try:
        mod = _load_monitor()
        ok, note = mod.deliver_bot(text, chat_id=DUTY_ALERT_TG_ID)
        if ok:
            return True, "bot->ai_zkw ok"
    except Exception as e:  # noqa: BLE001
        note = f"bot loader err={str(e)[:120]}"
    ok2, note2 = _fallback_instance_dm(text)
    if ok2:
        return True, f"{note2}（bot 失败回落：{note}）"
    return False, f"bot：{note}；instance：{note2}"


if __name__ == "__main__":
    # 手动验证最后一公里：python tools/duty_alert.py "测试文案"
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    msg = sys.argv[1] if len(sys.argv) > 1 else (
        "[值守告警通道测试] " + time.strftime("%m-%d %H:%M:%S"))
    ok, note = deliver(msg)
    print(("[ok] " if ok else "[FAIL] ") + note)
    raise SystemExit(0 if ok else 1)
