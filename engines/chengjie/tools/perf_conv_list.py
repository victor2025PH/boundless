# -*- coding: utf-8 -*-
"""会话列表规模压测（route 拦截喂 mock 数据，量化渲染/翻页/筛选/滚动）。

用法（需 dev 实例已运行 + playwright 已装）:
    python tools/perf_conv_list.py --base http://127.0.0.1:18901 --token dev-ui-check --count 500

十二期基线（500 条 / 本地 headless Chromium）：
    首渲 ~43ms · 热路径重载 ~43ms · 翻页追加 ~13ms · 筛选 9~44ms ·
    滚动最大主线程间隙 ~12ms —— keyed-diff 渲染在该规模无瓶颈，暂不需虚拟滚动。
超阈值退出码非 0（阈值宽松 5x 基线，抓的是量级劣化而非毛刺）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from playwright.sync_api import sync_playwright

PLATS = ["telegram", "whatsapp", "line", "messenger"]


def mk_rows(n, ts0, older=False):
    rows = []
    for i in range(n):
        p = PLATS[i % 4]
        rows.append({
            "conversation_id": f"{p}:acct:{'o' if older else 'n'}{i}",
            "platform": p, "account_id": "acct", "chat_key": f"k{i}",
            "name": f"客户 {'旧' if older else ''}{i}", "last_text": f"消息内容 {i}",
            "last_ts": ts0 - i * 30, "unread": i % 5, "chat_type": "private",
            "automation_mode": ["manual", "review", "auto"][i % 3],
            "conv_tags": (["vip"] if i % 7 == 0 else []),
            "archived": False, "sla_level": ("warn" if i % 11 == 0 else ""),
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18901")
    ap.add_argument("--token", default="dev-ui-check")
    ap.add_argument("--count", type=int, default=500)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    ts0 = time.time()
    r: dict = {"count": args.count}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        ctx.request.post(args.base + "/login", form={"auth_token": args.token})
        page = ctx.new_page()

        def handle(route):
            url = route.request.url
            if "before_ts" in url and "before_ts=0" not in url:
                body = {"ok": True, "ts": ts0,
                        "chats": mk_rows(100, ts0 - (args.count + 100) * 30, older=True),
                        "has_more": False, "oldest_ts": ts0 - (args.count + 200) * 30}
            else:
                body = {"ok": True, "ts": ts0, "chats": mk_rows(args.count, ts0),
                        "platform_status": {}, "has_more": True,
                        "oldest_ts": ts0 - (args.count - 1) * 30}
            route.fulfill(json=body)

        page.route("**/api/unified-inbox/chats*", handle)
        page.goto(args.base + "/workspace", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)

        r["initial_rows"] = page.evaluate("() => window.WS_STATE.getChats().length")
        r["reload_ms"] = page.evaluate(
            "async () => { const t0=performance.now(); await window.loadChats(); return Math.round(performance.now()-t0); }")
        r["reload_warm_ms"] = page.evaluate(
            "async () => { const t0=performance.now(); await window.loadChats(); return Math.round(performance.now()-t0); }")
        r["load_more_ms"] = page.evaluate(
            "async () => { const t0=performance.now(); await window.loadMoreChats(); return Math.round(performance.now()-t0); }")
        r["rows_after_more"] = page.evaluate("() => window.WS_STATE.getChats().length")
        r["filter_unread_ms"] = page.evaluate(
            "() => { const t0=performance.now(); window.setFilter('unread', document.querySelector('.ftab[data-f=unread]')); return Math.round(performance.now()-t0); }")
        r["filter_all_ms"] = page.evaluate(
            "() => { const t0=performance.now(); window.setFilter('all', document.querySelector('.ftab[data-f=all]')); return Math.round(performance.now()-t0); }")
        r["scroll_jank_ms_max"] = page.evaluate(
            """async () => {
              const el=document.getElementById('conv-items');
              let maxGap=0, last=performance.now();
              const id=setInterval(()=>{ const now=performance.now(); maxGap=Math.max(maxGap, now-last); last=now; }, 0);
              for(let i=0;i<10;i++){ el.scrollTop += el.clientHeight; await new Promise(r=>setTimeout(r,50)); }
              clearInterval(id);
              return Math.round(maxGap);
            }""")
        browser.close()

    print(json.dumps(r, ensure_ascii=False, indent=2))
    if args.out:
        from pathlib import Path
        Path(args.out).write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")

    # 宽松阈值（≈5x 十二期基线）：抓量级劣化
    limits = {"reload_ms": 250, "reload_warm_ms": 250, "load_more_ms": 120,
              "filter_unread_ms": 200, "filter_all_ms": 250, "scroll_jank_ms_max": 120}
    bad = {k: r[k] for k, v in limits.items() if r.get(k, 0) > v}
    if bad:
        print("PERF REGRESSION:", bad)
        return 2
    print("PERF OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
