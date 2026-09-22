#!/usr/bin/env python3
from chatx_session import session
import json

with session() as (page, _):
    page.wait_for_timeout(1200)
    page.locator(".conv-item").nth(1).click(timeout=5000)
    page.wait_for_timeout(2000)
    page.evaluate("() => { const b=document.querySelector('#info-toggle-btn'); if(b) b.click(); }")
    page.wait_for_timeout(1000)
    # 新版助手顶部分段：关系与目标 / 回复工坊 / …
    btn = page.locator("button").filter(has_text="回复工坊")
    print("reply workshop buttons", btn.count())
    for i in range(min(btn.count(), 5)):
        el = btn.nth(i)
        try:
            print(i, "vis", el.is_visible(), "txt", (el.inner_text() or "")[:40])
            if el.is_visible():
                el.click(timeout=4000)
                break
        except Exception as e:
            print("click fail", e)
    page.wait_for_timeout(2500)
    info = page.evaluate(
        """() => {
          const vis = el => { const r=el.getBoundingClientRect(); return r.width>5&&r.height>5; };
          return {
            btns: [...document.querySelectorAll('button')].filter(vis)
              .map(e => (e.innerText||'').trim().replace(/\\s+/g,' ').slice(0,50))
              .filter(t => t && /草稿|生成|续写|采用|工坊|接管|AI/.test(t)).slice(0,40),
            acts: [...document.querySelectorAll('[data-act]')].map(e => ({
              act: e.getAttribute('data-act'), t:(e.innerText||'').trim().slice(0,40), vis: vis(e)
            })).slice(0,20),
            text: ((document.querySelector('#ws-cp')||{}).innerText||'').slice(0,1200)
          };
        }"""
    )
    print(json.dumps(info, ensure_ascii=False, indent=2)[:4000])
    page.screenshot(path="out/probe/e4_workshop2.png")
