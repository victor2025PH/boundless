# -*- coding: utf-8 -*-
"""录播增强 UI 卡片渲染探针：无头加载 /ui → 开播页+视觉面板 → 断言卡片/状态/完成条。"""
from playwright.sync_api import sync_playwright

errs = []
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    pg = b.new_page(viewport={"width": 1600, "height": 1100})
    pg.on("pageerror", lambda e: errs.append(str(e).split("\n")[0]))
    pg.goto("http://127.0.0.1:9000/ui", wait_until="domcontentloaded", timeout=25000)
    pg.wait_for_timeout(2500)
    pg.evaluate("() => { const el=document.querySelector('[x-data]');"
                " const d=window.Alpine.$data(el);"
                " if('onboardShow' in d) d.onboardShow=false;"
                " d.tab='stream'; d.streamSimple=false; d.panelOpen.visual=true; }")
    pg.wait_for_timeout(3500)
    body = pg.inner_text("body")
    for k in ("视觉效果", "虚拟背景", "离席画面", "录播增强", "ProRes", "全部入队"):
        print(f"可见[{k}]:", k in body)
    st = pg.evaluate("() => { const d=window.Alpine.$data(document.querySelector('[x-data]'));"
                     " return {inputs:(d.ma.inputs||[]).length,"
                     " jobState:(d.ma.job||{}).state||'', running:d.ma.running,"
                     " tab:d.tab, visual:d.panelOpen.visual}; }")
    print("ma 状态:", st)
    vis = pg.evaluate("() => { const els=[...document.querySelectorAll('label')]"
                      ".filter(e=>e.textContent.includes('录播增强'));"
                      " if(!els.length) return 'DOM里没有该元素';"
                      " const e=els[0];"
                      " return {inDom:true, visible:e.offsetParent!==null,"
                      " h:e.getBoundingClientRect().height}; }")
    print("元素可见性:", vis)
    chain = pg.evaluate("() => { let e=[...document.querySelectorAll('label')]"
                        ".filter(x=>x.textContent.includes('录播增强'))[0]; const out=[];"
                        " while(e && e!==document.body) {"
                        "   const cs=getComputedStyle(e);"
                        "   if(cs.display==='none')"
                        "     out.push((e.tagName+'.'+(e.className||'').toString().slice(0,40))"
                        "       +' [x-show='+(e.getAttribute('x-show')||'')+']');"
                        "   e=e.parentElement; }"
                        " return out; }")
    print("display:none 祖先链:", chain)
    try:
        sec = pg.locator("section", has=pg.locator("h2", has_text="视觉效果")).first
        sec.screenshot(path=r"C:\模仿音色\logs\matting_offline\ui_matting_card.png")
        print("截图: ui_matting_card.png")
    except Exception as e:
        print("截图失败:", str(e)[:80])
    b.close()
print("pageerror:", errs if errs else "无")
