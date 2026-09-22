#!/usr/bin/env python3
from chatx_session import session, BASE
import json

with session() as (page, _):
    page.goto(BASE + "/membership", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    info = page.evaluate(
        """() => {
          const hero = document.querySelector('#mb-hero');
          const buy = document.querySelector('#mb-buy');
          const quota = document.querySelector('#mb-hosted-quota, #mb-hosted-line, #mb-hosted-bar');
          const pack = (e) => e ? {
            id: e.id,
            t: (e.innerText||'').trim().replace(/\\s+/g,' ').slice(0,200),
            top: Math.round(e.getBoundingClientRect().top),
            h: Math.round(e.getBoundingClientRect().height)
          } : null;
          const texts = [...document.querySelectorAll('h1,h2,h3,.mb-title,.mb-card-title,button,a.btn,.btn')]
            .map(e => (e.innerText||'').trim().replace(/\\s+/g,' ').slice(0,50))
            .filter(Boolean).slice(0,40);
          return {hero: pack(hero), buy: pack(buy), quota: pack(quota), texts,
            body: (document.body.innerText||'').replace(/\\s+/g,' ').slice(0,800)};
        }"""
    )
    print(json.dumps(info, ensure_ascii=False, indent=2))
    # scroll buy into view
    page.evaluate("() => document.querySelector('#mb-buy')?.scrollIntoView({block:'center'})")
    page.wait_for_timeout(500)
    page.screenshot(path="out/probe/membership_buy.png")
