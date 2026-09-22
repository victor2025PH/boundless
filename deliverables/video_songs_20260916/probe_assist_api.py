#!/usr/bin/env python3
"""探：用官方 API 展开面板与卡片。"""
from chatx_session import session
import json

with session() as (page, _):
    page.wait_for_timeout(1500)
    page.locator(".conv-item").nth(0).click(timeout=5000)
    page.wait_for_timeout(2000)
    st0 = page.evaluate(
        """() => ({
          mode: typeof _cpPanelModeGet==='function' ? _cpPanelModeGet() : null,
          pinned: typeof _cpPinnedWide==='function' ? _cpPinnedWide() : null,
          infoW: (()=>{const e=document.getElementById('info-sidebar'); if(!e) return null;
            const r=e.getBoundingClientRect(); return Math.round(r.width);})()
        })"""
    )
    print("before", st0)
    st1 = page.evaluate(
        """() => {
          if (typeof _cpSetPanelMode === 'function') _cpSetPanelMode('expanded');
          return {
            mode: typeof _cpPanelModeGet==='function' ? _cpPanelModeGet() : null,
            infoW: (()=>{const e=document.getElementById('info-sidebar'); if(!e) return null;
              const r=e.getBoundingClientRect(); return Math.round(r.width);})()
          };
        }"""
    )
    print("after expand panel", st1)
    page.wait_for_timeout(800)
    # switch tabs via official API if any
    for tab, card in (("tools", "voice"), ("customer", "goal")):
        res = page.evaluate(
            """({tab, card}) => {
              // tab switch
              if (window._wsCpTabs && typeof window._wsCpTabs.set === 'function') {
                window._wsCpTabs.set(tab);
              } else {
                const b = document.querySelector('button.ws-cp-tab[data-tab="'+tab+'"]');
                if (b) b.click();
              }
              let expandApi = typeof _cpExpandAndScroll;
              try {
                if (typeof _cpExpandAndScroll === 'function') _cpExpandAndScroll(card, tab);
                else if (typeof _toggleCpCard === 'function') {
                  const e = document.querySelector('[data-cp-card="'+card+'"]');
                  if (e && e.classList.contains('collapsed')) _toggleCpCard(card);
                }
              } catch (err) { return {err: String(err), expandApi}; }
              const e = document.querySelector('[data-cp-card="'+card+'"]');
              if (!e) return {missing: true, expandApi};
              const r = e.getBoundingClientRect();
              const t = (e.innerText||'').trim().replace(/\\s+/g,' ');
              return {
                expandApi,
                collapsed: e.classList.contains('collapsed'),
                vis: r.width>5 && r.height>5 && r.top < innerHeight && r.bottom > 0,
                w: Math.round(r.width), h: Math.round(r.height),
                top: Math.round(r.top),
                t: t.slice(0, 220),
                hasVoice: /生成语音|试听|音色/.test(t),
                hasGoal: /今日|目标|采纳|推进/.test(t),
              };
            }""",
            {"tab": tab, "card": card},
        )
        print(tab, card, json.dumps(res, ensure_ascii=False, indent=2))
        page.wait_for_timeout(600)
    page.screenshot(path="out/probe/e78_api.png")
