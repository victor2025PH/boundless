# -*- coding: utf-8 -*-
"""平台/账号视角切换 ↔ 中栏会话联动 真浏览器验证（Playwright；2026-08-05 沉淀）。

**为什么需要它**：2026-08-05 坐席实录——点左侧平台 rail 切到 WhatsApp，中栏仍显示
上一平台（Telegram）的旧会话：坐席对着错位会话继续输入＝错发风险，读者也会误以为
「这就是新平台的内容」。修复全部是**纯前端行为**（`_syncThreadToScope`：不匹配收起
+ 桌面端按平台工作记忆恢复），静态门禁只能证「函数存在」，证不了「切平台后中栏真的
跟着变」，而模板热更新直上生产。故用真浏览器把用户级不变量钉住。

与 ``tools/verify_account_rail_ui.py`` 同族（同一实例 + token 登录 + Playwright），
只读：只点已读会话行与平台 rail，不发消息、不改配置。

覆盖的不变量：
  1. 打开会话后切到**另一平台** → 中栏收起回空态（绝不残留旧平台会话），
     且空态驾驶舱标出「当前视角：<平台>」上下文（#ce-scope，_updateTodayStrip 驱动）。
  2. 切回原平台 → 桌面端自动恢复刚才浏览的会话（平台工作记忆）。
  3. 切「全部」视角 → 会话属于全集，保持打开不被误关；视角上下文行隐藏。

token 从实例数据根读取，**绝不打印**。缺 playwright / 实例不可达 / 无会话数据
→ SKIP exit 0（挂 gate_sweep -Full 的前提：环境缺失不污染回归信号）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_account_rail_ui import (  # noqa: E402
    Checker, DEFAULT_BASE, DEFAULT_DATA_ROOT, CANDIDATE_PLATFORMS, read_token,
)

_STATE_JS = """() => ({
  content: document.getElementById('chat-content').style.display,
  empty: document.getElementById('chat-empty').style.display,
  active: window.__wsActiveConvId || null,
  scopeLine: (() => {
    const el = document.getElementById('ce-scope');
    return (el && el.style.display !== 'none') ? (el.textContent || '').trim() : '';
  })(),
})"""

# 优先挑已读行（不动未读状态）；没有再退回首行
_PICK_ROW_JS = """() => {
  const sel = document.querySelector('#conv-items .conv-item:not(.has-unread)')
           || document.querySelector('#conv-items .conv-item');
  return sel ? sel.dataset.key : null;
}"""


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("[SKIP] playwright 不可用")
        return 0
    token = read_token(DEFAULT_DATA_ROOT)
    if not token:
        print("[SKIP] 读不到 auth_token")
        return 0

    ck = Checker()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 860})
        try:
            r = ctx.request.post(DEFAULT_BASE + "/login", form={"auth_token": token})
            if not r.ok:
                print("[SKIP] 实例不可达/登录失败")
                browser.close()
                return 0
        except Exception:
            print("[SKIP] 实例不可达")
            browser.close()
            return 0
        page = ctx.new_page()
        page.goto(DEFAULT_BASE + "/workspace", wait_until="domcontentloaded")
        try:
            page.wait_for_function(
                "() => typeof window.setPlatFilter === 'function'", timeout=15000)
        except Exception:
            print("[ABORT] 工作台 JS 未就绪（登录失败或模板异常）")
            browser.close()
            return 1
        page.wait_for_timeout(2500)

        # 找一个有会话行的平台，打开一条（优先已读）会话
        plat1, key = "", None
        for cand in CANDIDATE_PLATFORMS:
            page.evaluate(f"window.setPlatFilter('{cand}')")
            page.wait_for_timeout(600)
            key = page.evaluate(_PICK_ROW_JS)
            if key:
                plat1 = cand
                break
        if not key:
            print("[SKIP] 各平台均无会话行，无从验证")
            browser.close()
            return 0
        page.click(f'#conv-items .conv-item[data-key="{key}"]')
        page.wait_for_timeout(1200)
        st = page.evaluate(_STATE_JS)
        ck.check(f"打开会话（{plat1}）：中栏可见",
                 st["content"] == "flex" and st["active"], f"active={st['active']}")
        opened = st["active"]

        # 1. 切到另一平台 → 中栏收起（该平台无工作记忆 → 必为空态）
        plat2 = next(x for x in CANDIDATE_PLATFORMS if x != plat1)
        page.evaluate(f"window.setPlatFilter('{plat2}')")
        page.wait_for_timeout(800)
        st2 = page.evaluate(_STATE_JS)
        ck.check(f"切 {plat2}：不残留 {plat1} 旧会话（中栏空态）",
                 st2["content"] == "none" and st2["active"] != opened, f"state={st2}")
        ck.check(f"空态标出当前视角（{plat2}）",
                 bool(st2["scopeLine"]), f"scope={st2['scopeLine']!r}")

        # 2. 切回原平台 → 桌面端恢复刚才的会话
        page.evaluate(f"window.setPlatFilter('{plat1}')")
        page.wait_for_timeout(1200)
        st3 = page.evaluate(_STATE_JS)
        ck.check(f"切回 {plat1}：自动恢复刚才的会话（工作记忆）",
                 st3["content"] == "flex" and st3["active"] == opened,
                 f"active={st3['active']} want={opened}")

        # 3. 切「全部」→ 会话属于全集，保持打开
        page.evaluate("window.setPlatFilter('all')")
        page.wait_for_timeout(800)
        st4 = page.evaluate(_STATE_JS)
        ck.check("切「全部」：会话保持打开（属于全集不误关）",
                 st4["content"] == "flex" and st4["active"] == opened, f"state={st4}")
        ck.check("「全部」视角不显示上下文行（无需解释）",
                 not st4["scopeLine"], f"scope={st4['scopeLine']!r}")

        browser.close()
    return ck.summary()


if __name__ == "__main__":
    sys.exit(main())
