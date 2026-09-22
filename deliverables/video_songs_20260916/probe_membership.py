#!/usr/bin/env python3
"""探 /membership 与额度 pill 可得点控件。"""
from chatx_session import session, BASE
import json

with session() as (page, _):
    page.goto(BASE + "/membership", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    print("url", page.url)
    info = page.evaluate(
        """() => {
          const picks = [];
          const add = (s) => {
            const els = [...document.querySelectorAll(s)].slice(0, 6);
            for (const e of els) {
              const r = e.getBoundingClientRect();
              if (r.width < 2 || r.height < 2) continue;
              picks.push({
                s, tag: e.tagName, id: e.id,
                t: (e.innerText||'').trim().replace(/\\s+/g,' ').slice(0,60),
                w: Math.round(r.width), h: Math.round(r.height), top: Math.round(r.top)
              });
            }
          };
          ['#mb-buy','#mb-activate','#mb-claim','#mb-invite','#ws-quota','.plan-card','.mb-card',
           '[href*=pricing]', 'a', 'button', 'h1', 'h2'].forEach(add);
          return {
            title: document.title,
            h1: (document.querySelector('h1')||{}).innerText,
            anchors: [...document.querySelectorAll('[id^=mb-]')].map(e => e.id),
            picks: picks.slice(0, 40)
          };
        }"""
    )
    print(json.dumps(info, ensure_ascii=False, indent=2)[:5000])
    page.screenshot(path="out/probe/membership.png", full_page=False)

    # also workspace quota pill
    page.goto(BASE + "/workspace", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2000)
    q = page.evaluate(
        """() => {
          const e = document.querySelector('#ws-quota, a.ws-pill.quota, .ws-plan-lb, [href="/membership"]');
          if (!e) return null;
          const r = e.getBoundingClientRect();
          return {t:(e.innerText||'').trim().slice(0,80), id:e.id, href:e.getAttribute('href'),
            w:Math.round(r.width), h:Math.round(r.height), vis:r.width>2};
        }"""
    )
    print("quota", q)
