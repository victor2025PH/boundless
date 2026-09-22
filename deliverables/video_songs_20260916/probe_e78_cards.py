#!/usr/bin/env python3
from chatx_session import session
import json

with session() as (page, _):
    page.wait_for_timeout(1500)
    page.locator(".conv-item").nth(0).click(timeout=5000)
    page.wait_for_timeout(2000)
    page.evaluate("() => { const b=document.querySelector('#info-toggle-btn'); if(b) b.click(); }")
    page.wait_for_timeout(1000)
    tabs = page.locator("button.ws-cp-tab, .ws-cp-tab")
    print("tabs", tabs.count())
    for i in range(min(tabs.count(), 8)):
        t = (tabs.nth(i).inner_text() or "").strip().replace("\n", " ")
        print(" ", i, repr(t), "vis", tabs.nth(i).is_visible())
    # try 工具箱 / 关系与目标 / 客户关系
    for name in ("工具箱", "关系与目标", "客户关系", "回复台"):
        loc = page.locator("button.ws-cp-tab, .ws-cp-tab, button").filter(has_text=name)
        for j in range(min(loc.count(), 3)):
            el = loc.nth(j)
            if el.is_visible():
                print("click", name)
                el.click(timeout=4000)
                page.wait_for_timeout(1000)
                break
    cards = page.evaluate(
        """() => [...document.querySelectorAll('[data-cp-card]')].map(e => ({
          k: e.getAttribute('data-cp-card'),
          collapsed: e.classList.contains('collapsed'),
          t: (e.innerText||'').trim().replace(/\\s+/g,' ').slice(0,80),
          vis: (()=>{const r=e.getBoundingClientRect(); return r.width>5&&r.height>5;})()
        }))"""
    )
    print("cards", json.dumps(cards, ensure_ascii=False, indent=2)[:3000])
    for key in ("voice", "goal", "persona"):
        el = page.locator(f'[data-cp-card="{key}"]').first
        if el.count() == 0:
            print(key, "MISSING")
            continue
        try:
            el.scroll_into_view_if_needed(timeout=2000)
            el.click(timeout=4000)
        except Exception as e:
            print(key, "click fail", str(e)[:120])
            continue
        page.wait_for_timeout(1200)
        st = page.evaluate(
            """(k) => {
              const e = document.querySelector('[data-cp-card="'+k+'"]');
              if (!e) return null;
              const t = (e.innerText||'').trim().replace(/\\s+/g,' ');
              return {
                collapsed: e.classList.contains('collapsed'),
                t: t.slice(0, 220),
                markers: {
                  voice: /生成语音|试听|音色|克隆/.test(t),
                  goal: /今日|采纳|目标|推进/.test(t),
                  persona: /人设|绑定|切换/.test(t),
                }[k]
              };
            }""",
            key,
        )
        print("after", key, st)
    page.screenshot(path="out/probe/e78_cards.png")
