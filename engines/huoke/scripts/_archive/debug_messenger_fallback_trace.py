#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Messenger fallback 链路 trace (Phase 7, 2026-04-24).

目的: 验证 _send_greeting_messenger_fallback 路径在当前设备 + 账号状态下能走
多远,不真实发消息 (支持 --dry-run 在最后 Send 按钮前停手).

测试点:
  1. Messenger app (com.facebook.orca) 启动是否 OK
  2. Messenger 搜索能否找到 pending 好友 (friend_request 已发但未接受)
  3. 搜索结果里目标是否在 "Contacts" 或 "Message requests" 桶
  4. 点开对话后 EditText 是否 focused, 中日文能否 set_text 输入
  5. Send button 是否可点 (dry-run 跳过)

用法::

    # 默认 dry-run: 到输入 greeting 后停手, 不点 Send
    python scripts/debug_messenger_fallback_trace.py \\
        --device 8DWOF6CYY5R8YHX8 --peer 山田花子 --greeting 'はじめまして'

    # 真发 (小心)
    python scripts/debug_messenger_fallback_trace.py --device ... --peer ... --greeting ... --live
"""
from __future__ import annotations

import argparse
import io
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

MESSENGER_PKG = "com.facebook.orca"


class Trace:
    def __init__(self, out: Path):
        self.out = out
        out.mkdir(parents=True, exist_ok=True)
        self.step = 0
        self.rows = []

    def snap(self, d, tag: str):
        self.step += 1
        safe = re.sub(r"[^a-zA-Z0-9_]+", "_", tag)[:36]
        name = f"step{self.step:02d}_{safe}"
        png = self.out / f"{name}.png"
        xml_f = self.out / f"{name}.xml"
        try:
            d.screenshot(str(png))
        except Exception as e:
            print(f"  [!] screenshot: {e}")
        xml = ""
        try:
            xml = d.dump_hierarchy() or ""
            xml_f.write_text(xml, encoding="utf-8")
        except Exception as e:
            print(f"  [!] dump: {e}")
        try:
            pkg = d.info.get("currentPackageName", "?")
        except Exception:
            pkg = "?"
        print(f"[step {self.step:02d}] {tag}  pkg={pkg}  xml_len={len(xml)}  → {png.name}")
        return xml, pkg

    def mark(self, ok: bool, msg: str):
        icon = "✓" if ok else "✗"
        print(f"         {icon} {msg}")
        self.rows.append((self.step, ok, msg))


def _dismiss_popups(d, timeout_per=0.3):
    for t in ("Not Now", "Skip", "OK", "Continue", "Allow",
                "Close", "Got it", "Later", "While using the app",
                "Dismiss", "Cancel"):
        try:
            el = d(text=t)
            if el.exists(timeout=timeout_per):
                el.click()
                time.sleep(0.4)
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", required=True)
    ap.add_argument("--peer", required=True, help="目标好友/pending peer 名字")
    ap.add_argument("--greeting", required=True, help="要发的消息")
    ap.add_argument("--live", action="store_true",
                     help="真发 (默认 dry-run: 到 send 前停手, 不点 Send)")
    args = ap.parse_args()

    out = Path("debug") / (f"messenger_fallback_" +
                             datetime.now().strftime("%Y%m%d_%H%M%S"))
    tr = Trace(out)
    print(f"[init] out={out}  live={'YES' if args.live else 'dry-run'}")

    import uiautomator2 as u2
    d = u2.connect(args.device)

    # ── 1. 强制 stop+start Messenger 确保干净状态 ──────────────────
    print("\n─── 1. 启动 Messenger (com.facebook.orca) ───")
    try:
        d.app_stop(MESSENGER_PKG)
    except Exception:
        pass
    time.sleep(1.0)
    try:
        d.app_start(MESSENGER_PKG)
    except Exception as e:
        print(f"  [!] app_start Messenger 失败: {e}")
    time.sleep(6)
    _dismiss_popups(d)
    xml1, pkg1 = tr.snap(d, "messenger_launched")
    tr.mark(pkg1 == MESSENGER_PKG, f"Messenger 前台 (pkg={pkg1})")

    if pkg1 != MESSENGER_PKG:
        print("[abort] Messenger 未启动, 可能未装或 MIUI 双开弹窗未 dismiss")
        return

    # ── 2. 找搜索入口 ────────────────────────────────────────────
    print("\n─── 2. 找 Messenger 搜索入口 ───")
    # 常见: 顶栏放大镜 icon 或 "Search" text
    search_btn = None
    for sel in (
        {"description": "Search"},
        {"text": "Search"},
        {"descriptionContains": "search"},
    ):
        try:
            el = d(**sel)
            if el.exists(timeout=1.5):
                search_btn = (el, sel)
                break
        except Exception:
            pass
    if search_btn is None:
        tr.mark(False, "无搜索入口")
        tr.snap(d, "no_search_entry")
        return
    search_btn[0].click()
    print(f"  [click] 搜索入口 via {search_btn[1]}")
    time.sleep(1.5)
    xml2, _ = tr.snap(d, "search_opened")
    # Messenger 新版搜索框可能是 EditText 或 AutoCompleteTextView
    has_input = ("android.widget.EditText" in xml2
                  or "android.widget.AutoCompleteTextView" in xml2)
    tr.mark(has_input, "搜索页: 输入控件存在 (EditText/AutoCompleteTextView)")

    # ── 3. 输入 peer 名字 ────────────────────────────────────────
    print(f"\n─── 3. 输入搜索 peer={args.peer!r} ───")
    ed = None
    for sel in (
        {"className": "android.widget.EditText"},
        {"className": "android.widget.AutoCompleteTextView"},
    ):
        cand = d(**sel)
        if cand.exists(timeout=1.0):
            ed = cand
            print(f"  [input_sel] 命中 {sel}")
            break
    if ed is None:
        ed = d(className="android.widget.EditText")  # 保持后续代码结构
    if not ed.exists(timeout=2):
        tr.mark(False, "EditText 不存在")
        return
    ed.click()
    time.sleep(0.5)
    try:
        ed.clear_text()
    except Exception:
        pass
    time.sleep(0.3)
    ed.set_text(args.peer)
    time.sleep(2.0)

    # 验证输入成功
    try:
        ed2 = d(className="android.widget.EditText")
        if ed2.exists(timeout=0.8):
            got = ed2.get_text() or ""
            tr.mark(args.peer[:2] in got,
                      f"EditText.get_text()={got!r} (期望含 {args.peer[:2]!r})")
    except Exception:
        pass
    time.sleep(2.5)
    xml3, _ = tr.snap(d, "after_search_input")

    # ── 4. 看搜索结果 ────────────────────────────────────────────
    print("\n─── 4. 分析搜索结果 ───")
    # 扫描结果 bucket: 先看是不是 Contacts / Message requests / On Messenger
    buckets = []
    for bucket_text in ("Contacts", "On Messenger", "Messages",
                         "Message requests", "You may know", "Chats"):
        if bucket_text in xml3:
            buckets.append(bucket_text)
    print(f"  [analyze] 可见 section buckets: {buckets}")

    # 提取名字候选 — 不限属性顺序, 抓每个 node 的所有属性独立解析.
    # Android XML 里 content-desc 可能出现在 class 之前或之后, 正则一次写死反而漏.
    cands = []
    peer_frag = args.peer[:2]
    seen_bounds = set()
    for nm in re.finditer(r'<node\s[^>]+/>', xml3):
        node_str = nm.group(0)

        def _attr(name):
            mm = re.search(rf'\b{name}="([^"]*)"', node_str)
            return mm.group(1) if mm else ""

        desc = _attr("content-desc")
        text = _attr("text")
        cls = _attr("class")
        bounds_raw = _attr("bounds")
        # 取含 peer 片段的字段 (content-desc 优先)
        label = ""
        if peer_frag in desc:
            label = desc
        elif peer_frag in text:
            label = text
        if not label:
            continue
        # 解析 bounds
        bm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_raw)
        if not bm:
            continue
        x1, y1, x2, y2 = map(int, bm.groups())
        if y1 < 200:  # 顶部搜索框 excluded
            continue
        if x2 - x1 < 400:  # 必须足宽是整行
            continue
        key = (x1, y1, x2, y2)
        if key in seen_bounds:
            continue
        seen_bounds.add(key)
        cands.append({"text": label, "class": cls.split(".")[-1] if cls else "?",
                       "y1": y1, "bounds": key})
    print(f"  [analyze] 含 {args.peer[:2]!r} 的候选 {len(cands)} 个:")
    for c in cands[:6]:
        print(f"    y={c['y1']:4d} {c['class']:20s} text={c['text'][:40]!r}")
    tr.mark(len(cands) > 0,
             f"搜索命中 {len(cands)} 个含 '{args.peer[:2]}' 的候选")

    if not cands:
        print("[abort] 无命中候选, Messenger fallback 无法继续")
        return

    # ── 5. 点第一个命中 ──────────────────────────────────────────
    target = cands[0]
    print(f"\n─── 5. 点第一个候选: {target['text']!r} ───")
    try:
        cx = (target["bounds"][0] + target["bounds"][2]) // 2
        cy = (target["bounds"][1] + target["bounds"][3]) // 2
        d.click(cx, cy)
        time.sleep(3.5)
    except Exception as e:
        print(f"  [!] click: {e}")
    xml5, _ = tr.snap(d, "after_click_contact")

    # 进了对话页: 有 "Type a message" 或 EditText focused
    tr.mark("android.widget.EditText" in xml5, "对话页有 EditText")

    # ── 6. 输入 greeting (unicode) ──────────────────────────────
    print(f"\n─── 6. 输入 greeting={args.greeting!r} ───")
    input_box = None
    for sel in (
        {"className": "android.widget.EditText"},
        {"className": "android.widget.AutoCompleteTextView"},
    ):
        cand = d(**sel)
        if cand.exists(timeout=2.5):
            input_box = cand
            print(f"  [input_sel] 对话页命中 {sel}")
            break
    if input_box is None:
        tr.mark(False, "对话页无 EditText/AutoCompleteTextView (可能进了 info 页)")
        return
    input_box.click()
    time.sleep(0.4)
    try:
        input_box.clear_text()
    except Exception:
        pass
    time.sleep(0.3)
    input_box.set_text(args.greeting)
    time.sleep(1.5)
    # 验证
    try:
        ib2 = d(className="android.widget.EditText")
        if ib2.exists(timeout=0.8):
            got = ib2.get_text() or ""
            tr.mark(args.greeting[:2] in got,
                      f"对话页 EditText 输入后={got!r}")
    except Exception:
        pass
    xml6, _ = tr.snap(d, "after_type_greeting")

    # ── 7. Send 按钮 ─────────────────────────────────────────────
    print("\n─── 7. 查 Send 按钮 ───")
    send_btn = None
    for sel in (
        {"description": "Send"},
        {"text": "Send"},
        {"descriptionContains": "send"},
    ):
        try:
            el = d(**sel)
            if el.exists(timeout=1.2):
                send_btn = (el, sel)
                break
        except Exception:
            pass
    tr.mark(send_btn is not None, f"Send 按钮存在={send_btn is not None}")

    if not args.live:
        print("\n[dry-run] 不点 Send, trace 结束. 看 debug/ 下截图确认各步骤")
        tr.snap(d, "dry_run_stop")
    else:
        if send_btn is None:
            print("[live] 无 Send 按钮, 不能发")
        else:
            print(f"\n─── 8. LIVE 点 Send ───")
            send_btn[0].click()
            time.sleep(3.0)
            tr.snap(d, "after_send")
            tr.mark(True, "Send 已点击 (需人工看截图确认真发出)")

    # summary
    lines = [
        "# Messenger Fallback Trace",
        "",
        f"- 时间: {datetime.now().isoformat(timespec='seconds')}",
        f"- 设备: {args.device}",
        f"- Peer: {args.peer}",
        f"- Greeting: {args.greeting}",
        f"- Mode: {'LIVE' if args.live else 'dry-run'}",
        "",
        "| # | OK | 说明 |",
        "|---|----|-----|",
    ]
    for step, ok, msg in tr.rows:
        icon = "✓" if ok else "✗"
        lines.append(f"| {step} | {icon} | {msg.replace('|', '\\|')} |")
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n→ {out / 'summary.md'}")


if __name__ == "__main__":
    main()
