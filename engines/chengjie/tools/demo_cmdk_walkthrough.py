# -*- coding: utf-8 -*-
"""售前演示走查：seed 示例数据 → 打开工作台 → Ctrl+K 全局搜索 → 命中示例消息。

用法（需 dev 实例已运行 + playwright 已装）:
    python tools/demo_cmdk_walkthrough.py --base http://127.0.0.1:18901 --token dev-ui-check

产物: 截图落 --out 目录（默认 ./demo_shots），命中失败以非零码退出。
demo 数据带 ``demo:`` 前缀，可在设置页或 POST /api/admin/demo/clear 一键清除。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18901")
    ap.add_argument("--token", default="dev-ui-check")
    ap.add_argument("--query", default="shipping", help="CmdK 演示搜索词（与 demo_seeder._QA 对齐）")
    ap.add_argument("--out", default="demo_shots")
    ap.add_argument("--headed", action="store_true", help="有头模式（现场演示投屏用）")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    result: dict = {"base": args.base, "query": args.query}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        ctx = browser.new_context(viewport={"width": 1366, "height": 850})
        ctx.request.post(args.base + "/login", form={"auth_token": args.token})

        page = ctx.new_page()
        page.goto(args.base + "/workspace", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)

        # 页面内 fetch 铺数据（同源 cookie 语义与坐席端一致）
        seed = page.evaluate(
            """async () => {
              const r = await fetch('/api/admin/demo/seed', {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
              });
              try { return await r.json(); } catch (e) { return {ok: false, status: r.status}; }
            }"""
        )
        result["seed"] = {k: seed.get(k) for k in ("ok", "conversations", "messages", "search_hints")}
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        page.screenshot(path=str(out / "01_workspace.png"))

        page.evaluate("() => window.wsOpenCmdk && window.wsOpenCmdk()")
        page.wait_for_timeout(400)
        result["palette_open"] = page.locator("#cmdk-overlay.open").count() == 1
        page.screenshot(path=str(out / "02_cmdk_open.png"))

        page.locator("#cmdk-input").fill(args.query)
        page.wait_for_timeout(1500)
        html = page.locator("#cmdk-list").inner_html()
        result["hit"] = ("<em>" in html) and ("cmdk-item" in html)
        page.screenshot(path=str(out / "03_cmdk_results.png"))

        # 列表 API 可能过滤掉部分 demo 会话（按已接入账号）；选中「列表内存在」的
        # 那条命中，保证 Enter 必能打开会话（CmdK 渲染顺序与 API 消息命中顺序一致）
        pick_idx = page.evaluate(
            """async (q) => {
              const chats = (window.WS_STATE && window.WS_STATE.getChats()) || [];
              const have = new Set(chats.map(c => c.conversation_id));
              const r = await fetch('/api/workspace/search?' + new URLSearchParams(
                {q: q, types: 'messages,contacts,notes', limit: '12'}), {credentials: 'same-origin'});
              const d = await r.json();
              const msgs = (d.results || []).filter(x => x.type !== 'contact').slice(0, 6);
              for (let i = 0; i < msgs.length; i++) {
                if (have.has(msgs[i].conversation_id)) return i;
              }
              return -1;
            }""",
            args.query,
        )
        result["pick_idx"] = pick_idx

        if result["hit"] and pick_idx >= 0:
            for _ in range(pick_idx):
                page.keyboard.press("ArrowDown")
                page.wait_for_timeout(120)
            page.keyboard.press("Enter")
            opened = False
            for _ in range(12):  # 会话选中 + 线程加载轮询（最长约 6s）
                page.wait_for_timeout(500)
                opened = page.evaluate(
                    """() => {
                      const cc = document.getElementById('chat-content');
                      const visible = cc && getComputedStyle(cc).display !== 'none';
                      return visible || document.querySelectorAll('.msg-row').length > 0;
                    }"""
                )
                if opened:
                    break
            result["opened_conv"] = opened
            page.wait_for_timeout(800)
            page.screenshot(path=str(out / "04_conversation.png"))

        browser.close()

    print(json.dumps(result, ensure_ascii=False, indent=2))
    ok = (result.get("seed", {}).get("ok") and result.get("palette_open")
          and result.get("hit") and result.get("opened_conv", False))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
