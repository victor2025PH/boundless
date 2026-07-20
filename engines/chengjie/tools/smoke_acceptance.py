# -*- coding: utf-8 -*-
"""坐席工作台验收冒烟（四～九期功能面，单入口可重复执行）。

用法（需 dev 实例已运行 + playwright 已装）:
    python tools/smoke_acceptance.py --base http://127.0.0.1:18901 --token dev-ui-check

覆盖面：
  外壳     viewport interactive-widget / 软键盘 shim / 窄屏顶栏收敛
  CmdK    打开 / 最近会话组 / 全局搜索组+高亮 / @转坐席模式 / #标签模式
  移动端   底部平台条（FIXED_PLATS 兜底）/ ≤560 药丸收敛
  i18n    vi 高频键 / 深链兜底键三语
  API     /api/workspace/search 连通 + message_id 透出

失败以非零码退出；结果 JSON 落 --out。CI 可直接串接（配合 scripts/regression.*）。
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
    ap.add_argument("--out", default="smoke_acceptance.json")
    args = ap.parse_args()

    r: dict = {"base": args.base}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        ctx.request.post(args.base + "/login", form={"auth_token": args.token})
        page = ctx.new_page()
        page.goto(args.base + "/workspace", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)

        # ── 外壳 ──
        r["meta_interactive_widget"] = page.evaluate(
            "() => (document.querySelector('meta[name=viewport]')||{content:''}).content.includes('interactive-widget')"
        )
        r["kb_shim"] = page.evaluate(
            "() => document.documentElement.innerHTML.includes('kbActive')"
        )
        r["ws_state_bridge"] = page.evaluate(
            "() => !!(window.WS_STATE && window.WS_STATE.getSelectedCid && window.WS_STATE.getTagLibrary)"
        )
        r["inbox_messages_mod"] = page.evaluate(
            "() => !!(window.InboxMessages && window.InboxMessages.reconcile && window.InboxMessages.deliveryTick)"
        )

        # ── CmdK ──
        page.evaluate("() => window.wsOpenCmdk && window.wsOpenCmdk()")
        page.wait_for_timeout(400)
        r["cmdk_open"] = page.locator("#cmdk-overlay.open").count() == 1
        html0 = page.locator("#cmdk-list").inner_html()
        r["cmdk_recent"] = any(x in html0 for x in ("最近会话", "Recent conversations", "gần đây"))

        page.locator("#cmdk-input").fill("shipping")
        page.wait_for_timeout(1400)
        h1 = page.locator("#cmdk-list").inner_html()
        r["cmdk_search_grp"] = any(x in h1 for x in ("全局搜索", "Global search", "Tìm kiếm toàn cục"))

        page.locator("#cmdk-input").fill("@")
        page.wait_for_timeout(900)
        h2 = page.locator("#cmdk-list").inner_html()
        r["cmdk_assign_mode"] = any(x in h2 for x in ("转给坐席", "Assign to agent", "Chuyển cho"))

        page.locator("#cmdk-input").fill("#")
        page.wait_for_timeout(500)
        h3 = page.locator("#cmdk-list").inner_html()
        r["cmdk_tag_mode"] = any(x in h3 for x in ("标签筛选", "Tag filter", "Lọc theo thẻ"))

        page.locator("#cmdk-input").fill(">")
        page.wait_for_timeout(400)
        h4 = page.locator("#cmdk-list").inner_html()
        r["cmdk_page_mode"] = any(x in h4 for x in ("页面", "Pages", "Trang"))

        page.locator("#cmdk-input").fill("s 30")
        page.wait_for_timeout(400)
        h5 = page.locator("#cmdk-list").inner_html()
        r["cmdk_snooze_mode"] = any(
            x in h5 for x in ("搁置", "Snooze", "Hoãn", "请先选择", "Select a conversation")
        )
        page.keyboard.press("Escape")

        # ── i18n ──
        r["locate_fail_key"] = page.evaluate(
            "() => window.T('inbox.msg.locate_fail') !== 'inbox.msg.locate_fail'"
        )

        # ── API ──
        api = page.evaluate("""async () => {
          const r = await fetch('/api/workspace/search?q=shipping&limit=5', {credentials:'same-origin'});
          return await r.json();
        }""")
        r["api_search_ok"] = bool(api.get("ok"))
        msgs = [x for x in (api.get("results") or []) if x.get("type") == "message"]
        r["api_msg_has_mid"] = (not msgs) or bool(msgs[0].get("message_id"))

        # 十期：列表分页 API（before_ts 页 ok 即通过；空库时 chats 为空同样 ok）
        pg2 = page.evaluate("""async () => {
          const r = await fetch('/api/unified-inbox/chats?limit=10&before_ts=' + (Date.now()/1000),
            {credentials:'same-origin'});
          return await r.json();
        }""")
        r["api_paging_ok"] = bool(pg2.get("ok")) and "has_more" in pg2
        r["load_more_bar_present"] = page.evaluate(
            "() => !!document.getElementById('conv-load-more')"
        )

        # ── 移动端 ──
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(1000)
        r["mob_bar"] = page.locator("#mob-plat-bar").is_visible()
        r["mob_plats"] = page.locator("#mob-plat-icons .mob-plat-item").count()
        r["gs_hidden_mobile"] = page.evaluate(
            "() => { const g=document.getElementById('gs-wrap'); return !g || getComputedStyle(g).display==='none'; }"
        )
        r["pills_trimmed"] = page.evaluate(
            """() => {
              const l4=document.getElementById('ws-l4-badge');
              if(!l4) return true;
              l4.style.display='inline-flex';
              return getComputedStyle(l4).display==='none';
            }"""
        )

        # ── vi ──
        ctx.add_cookies([{"name": "ui_lang", "value": "vi",
                          "domain": args.base.split("//")[1].split(":")[0], "path": "/"}])
        page.set_viewport_size({"width": 1280, "height": 800})
        page.goto(args.base + "/workspace", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2000)
        r["vi_lang"] = page.evaluate("() => window.WS_LANG") == "vi"
        r["vi_filter"] = page.evaluate("() => window.T('inbox.filter.all')") == "Tất cả"
        r["vi_nav"] = page.evaluate("() => window.T('base.nav.chat')") == "Trò chuyện"

        browser.close()

    Path(args.out).write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(r, ensure_ascii=False, indent=2))
    required = [
        "meta_interactive_widget", "kb_shim", "ws_state_bridge", "inbox_messages_mod",
        "cmdk_open", "cmdk_recent", "cmdk_search_grp", "cmdk_assign_mode", "cmdk_tag_mode",
        "cmdk_page_mode", "cmdk_snooze_mode",
        "locate_fail_key", "api_search_ok", "api_msg_has_mid",
        "api_paging_ok", "load_more_bar_present",
        "mob_bar", "gs_hidden_mobile", "pills_trimmed",
        "vi_lang", "vi_filter", "vi_nav",
    ]
    bad = [k for k in required if not r.get(k)]
    bad += [] if r.get("mob_plats", 0) >= 5 else ["mob_plats"]
    if bad:
        print("FAILED checks:", bad)
        return 2
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
