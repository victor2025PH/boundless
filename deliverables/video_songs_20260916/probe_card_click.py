#!/usr/bin/env python3
"""探：DOM 点击卡头能否展开 + 子控件 marker。"""
from chatx_session import session
import json

with session() as (page, _):
    page.wait_for_timeout(1500)
    page.locator(".conv-item").nth(0).click(timeout=5000)
    page.wait_for_timeout(2000)
    page.evaluate("() => { if (typeof _cpSetPanelMode==='function') _cpSetPanelMode('expanded'); }")
    page.wait_for_timeout(500)

    def switch_tab(tab):
        page.evaluate(
            """(tab) => {
              const b = document.querySelector('button.ws-cp-tab[data-tab="'+tab+'"]');
              if (b) b.click();
              else if (typeof setWsCpTab==='function') setWsCpTab(tab);
            }""",
            tab,
        )
        page.wait_for_timeout(800)

    for tab, card, markers in (
        ("tools", "voice", ["生成语音", "试听", "音色", "克隆"]),
        ("customer", "goal", ["今日", "采纳", "推进", "目标"]),
    ):
        switch_tab(tab)
        info = page.evaluate(
            """(card) => {
              const e = document.querySelector('[data-cp-card="'+card+'"]');
              if (!e) return {missing:true};
              e.scrollIntoView({block:'center'});
              const hd = e.querySelector('.cp-card-hd, .cp-card-ttl, .cp-card-head, summary') || e;
              hd.click();
              const r = e.getBoundingClientRect();
              const t = (e.innerText||'').trim().replace(/\\s+/g,' ');
              // also peek shadow / custom els
              let deep = t;
              e.querySelectorAll('*').forEach(n => {
                if (n.shadowRoot) deep += ' ' + (n.shadowRoot.textContent||'');
              });
              deep = deep.replace(/\\s+/g,' ').trim();
              return {
                collapsed: e.classList.contains('collapsed'),
                h: Math.round(r.height),
                top: Math.round(r.top),
                t: t.slice(0,260),
                deep: deep.slice(0,360),
                hdTag: hd.tagName + '.' + (hd.className||'').toString().slice(0,40)
              };
            }""",
            card,
        )
        print(card, json.dumps(info, ensure_ascii=False, indent=2))
        # Playwright click fallback
        if info.get("collapsed"):
            loc = page.locator(f'[data-cp-card="{card}"] .cp-card-hd, [data-cp-card="{card}"]').first
            try:
                loc.click(timeout=3000, force=True)
                page.wait_for_timeout(800)
            except Exception as ex:
                print("  force click fail", str(ex)[:100])
            info2 = page.evaluate(
                """(card) => {
                  const e = document.querySelector('[data-cp-card="'+card+'"]');
                  if (!e) return null;
                  return {collapsed: e.classList.contains('collapsed'),
                    h: Math.round(e.getBoundingClientRect().height),
                    t: (e.innerText||'').trim().replace(/\\s+/g,' ').slice(0,260)};
                }""",
                card,
            )
            print("  after force", info2)
        page.screenshot(path=f"out/probe/e78_{card}.png")
