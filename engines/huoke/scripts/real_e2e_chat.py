#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实端到端 1 轮聊天 — A 模拟真客户发日文, B 用 OpenClaw 系统 AI 回复.

Scenario:
1. A (4HUSIB4T, さとう たかひろ) 用 u2 set_text 发 1 条日文给 B
2. B (CACAVKLN, しょうぶ あより) 跑 OpenClaw facebook_check_inbox auto_reply=True
3. B 的 chat_brain 用 ollama LLM 真生成日文回复 + 真发出
4. 每步截图存证

不需要 ADBKeyboard - 用 u2 (uiautomator2) AccessibilityNode set_text.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ADB = r"C:\Android\android-sdk\platform-tools\adb.exe"
A_DEVICE = "4HUSIB4TBQC69TJZ"  # さとう たかひろ
B_DEVICE = "CACAVKLNU8SGO74D"  # しょうぶ あより
COORD_BASE = "http://127.0.0.1:8000"

OUT_DIR = f"reports/e2e_chat/{datetime.now().strftime('%Y%m%d_%H%M%S')}"
os.makedirs(OUT_DIR, exist_ok=True)


def ss(device: str, name: str) -> bool:
    """截屏 + pull."""
    try:
        subprocess.run([ADB, "-s", device, "shell", "screencap", "-p", "/sdcard/_ss.png"],
                       capture_output=True, timeout=10)
        path = f"{OUT_DIR}/{name}.png"
        subprocess.run([ADB, "-s", device, "pull", "/sdcard/_ss.png", path],
                       capture_output=True, timeout=10)
        ok = os.path.exists(path) and os.path.getsize(path) > 1000
        print(f"  📸 {name}.png ({'ok' if ok else 'FAIL'})")
        return ok
    except Exception as e:
        print(f"  ⚠ ss {name}: {e}")
        return False


def step(n: int, title: str):
    print()
    print(f"━━━━━━━ STEP {n}: {title} ━━━━━━━")


def run_adb(device: str, *args, timeout: float = 10.0):
    return subprocess.run([ADB, "-s", device] + list(args),
                          capture_output=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def main():
    step(1, "前置: 启动 A + B 的 Messenger, 截图初态")

    # A
    run_adb(A_DEVICE, "shell",
             "am start -n com.facebook.orca/com.facebook.messaging.activity.MainActivity",
             timeout=15)
    # B
    run_adb(B_DEVICE, "shell",
             "am start -n com.facebook.orca/com.facebook.messaging.activity.MainActivity",
             timeout=15)
    time.sleep(8)
    ss(A_DEVICE, "01_A_messenger_start")
    ss(B_DEVICE, "01_B_messenger_start")

    step(2, "A: 在 Messenger 列表中点 'しょうぶ あより' 进入对话页 (用 u2 by name)")

    # 用 uiautomator2 的 d(text="しょうぶ あより").click()
    # 但 set_text/click 都通过 atx-agent, 用 OpenClaw 内置封装
    import sys
    sys.path.insert(0, os.getcwd())
    try:
        import uiautomator2 as u2
        d_a = u2.connect(A_DEVICE)
        d_b = u2.connect(B_DEVICE)
        print(f"  u2 A device info: {d_a.info.get('displayWidth')}x{d_a.info.get('displayHeight')}")
        print(f"  u2 B device info: {d_b.info.get('displayWidth')}x{d_b.info.get('displayHeight')}")
    except Exception as e:
        print(f"  ❌ u2 connect failed: {e}")
        return 1

    # A 找 "しょうぶ あより" 行并点击进入对话
    peer_b = "しょうぶ あより"
    elem = d_a(textContains="しょうぶ", clickable=True)
    if not elem.exists(timeout=5):
        elem = d_a(textContains="しょうぶ")
    if elem.exists(timeout=3):
        elem.click()
        print(f"  ✓ A clicked on 'しょうぶ' conversation")
    else:
        print(f"  ⚠ 'しょうぶ' not in visible list, try search")
        # tap search bar (顶部 360, 175)
        d_a.click(360, 175)
        time.sleep(1.5)
        # 输入 search
        d_a(focused=True).set_text("しょうぶ あより")
        time.sleep(2)
        # tap 第一个结果
        results = d_a(textContains="しょうぶ").all()
        if results:
            results[0].click()
            print(f"  ✓ A clicked search result")
        else:
            print(f"  ❌ search 也找不到, abort")
            ss(A_DEVICE, "02_A_search_fail")
            return 1
    time.sleep(3)
    ss(A_DEVICE, "02_A_in_conversation")

    step(3, "A: 用 u2 set_text 输入日文消息 + 点 send")

    # 找输入框 (composer)
    msg = "こんばんは、しょうぶさん。今日は本当にお疲れ様でした。お元気ですか?"
    composer = d_a(resourceIdMatches=".*composer.*|.*message_input.*", className="android.widget.EditText")
    if not composer.exists(timeout=3):
        composer = d_a(className="android.widget.EditText").last
    if composer.exists(timeout=3):
        composer.click()
        time.sleep(1)
        # u2 send_keys 用 ADBKeyboard, 用 set_text 走 AccessibilityNode (Unicode safe)
        focused = d_a(focused=True)
        if focused.exists(timeout=2):
            focused.set_text(msg)
            print(f"  ✓ A 输入日文消息: {msg[:30]}...")
            time.sleep(2)
            ss(A_DEVICE, "03_A_typed")
        else:
            print(f"  ❌ no focused widget after click")
            return 1
    else:
        print(f"  ❌ no EditText composer found")
        ss(A_DEVICE, "03_A_no_composer")
        return 1

    # 找 Send button (paper plane). 不同 FB 版本 selector 不同
    send_btn = d_a(descriptionContains="Send", clickable=True)
    if not send_btn.exists(timeout=2):
        send_btn = d_a(description="送信")
    if not send_btn.exists(timeout=2):
        send_btn = d_a(description="发送")
    if not send_btn.exists(timeout=2):
        # 直接按 enter 试试
        d_a.press("enter")
        print(f"  ⚠ Send button selector miss, pressed enter")
    else:
        send_btn.click()
        print(f"  ✓ A clicked Send")
    time.sleep(4)
    ss(A_DEVICE, "04_A_sent")

    step(4, "B: 看 Messenger 是否真收到 unread 消息 (返回主列表)")

    # B 设备返回 Messenger 主列表 (back 几次)
    for _ in range(3):
        d_b.press("back")
        time.sleep(0.5)
    run_adb(B_DEVICE, "shell",
             "am start -n com.facebook.orca/com.facebook.messaging.activity.MainActivity",
             timeout=15)
    time.sleep(5)
    ss(B_DEVICE, "05_B_inbox_with_unread")

    step(5, "B: 派 OpenClaw facebook_check_inbox auto_reply=True 任务")

    body = json.dumps({
        "type": "facebook_check_inbox",
        "device_id": B_DEVICE,
        "params": {
            "auto_reply": True,
            "phase": "growth",
            "persona_key": "jp_caring_male",  # B 这台设备扮演 jp_caring_male bot 给客户回
            "max_conversations": 10,
            "force_send_greeting": True,  # 跳过概率门
            "_real_e2e_test": True,
            "created_via": "real_e2e_test",
        },
        "priority": 50,
    }).encode()
    req = urllib.request.Request(
        f"{COORD_BASE}/tasks", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
        task_id = resp["task_id"]
        print(f"  ✓ task created: {task_id}")
    except urllib.error.HTTPError as e:
        print(f"  ❌ task create failed: {e.code} {e.read().decode()[:200]}")
        return 1

    print(f"  等任务执行 (~120s)...")
    final = None
    for i in range(36):
        time.sleep(5)
        with urllib.request.urlopen(f"{COORD_BASE}/tasks/{task_id}", timeout=5) as r:
            t = json.loads(r.read())
        if t.get("status") in ("completed", "failed", "cancelled"):
            final = t
            print(f"  t={i*5+5:3d}s status={t['status']}")
            break
        if i % 4 == 0:
            print(f"  t={i*5+5:3d}s status={t.get('status')}")

    step(6, "B: 看任务结果 + 当前屏幕")

    ss(B_DEVICE, "06_B_after_auto_reply")
    if final:
        res = final.get("result") or {}
        print(f"  success: {res.get('success')}")
        print(f"  conversations_listed: {res.get('conversations_listed')}")
        print(f"  unread_processed: {res.get('unread_processed')}")
        print(f"  replied: {res.get('replied')}")
        print(f"  wa_referrals: {res.get('wa_referrals')}")
        print(f"  errors: {res.get('errors')}")
        print(f"  messages: {res.get('messages')}")
        if res.get("error"):
            print(f"  error: {res['error'][:300]}")

    step(7, "回 A 设备看是否收到了 B 的 AI 回复")

    # A 设备返回对话页, 看 B 是不是回了
    run_adb(A_DEVICE, "shell",
             "am start -n com.facebook.orca/com.facebook.messaging.activity.MainActivity",
             timeout=15)
    time.sleep(5)
    # 找 B 对话进入
    elem_b = d_a(textContains="しょうぶ", clickable=True)
    if elem_b.exists(timeout=3):
        elem_b.click()
        time.sleep(3)
        ss(A_DEVICE, "07_A_received_reply")
    else:
        ss(A_DEVICE, "07_A_inbox")

    step(8, "看主控数据落库")

    # PG 客户事件
    try:
        with urllib.request.urlopen(
                f"{COORD_BASE}/cluster/customers/funnel/stats?days=1",
                timeout=10) as r:
            f = json.loads(r.read())
        print(f"  近 1 天 events: {f.get('events_by_type')}")
    except Exception as e:
        print(f"  events err: {e}")

    print()
    print(f"📁 全部截图: {OUT_DIR}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
