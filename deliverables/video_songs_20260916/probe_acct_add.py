#!/usr/bin/env python3
from chatx_session import session

with session() as (page, _):
    page.wait_for_timeout(1500)

    def dump(tag):
        info = page.evaluate(
            """() => [...document.querySelectorAll('.acct-add-btn')].map(el => {
              const r = el.getBoundingClientRect();
              const s = getComputedStyle(el);
              return {
                t: (el.innerText||'').trim(),
                r: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
                display: s.display, visibility: s.visibility, opacity: s.opacity,
                pe: s.pointerEvents,
                parent: ((el.closest('[class*=panel],[class*=drawer],[class*=modal],#acct-panel,#acct-drawer')||{}).className||'').toString().slice(0,80),
              };
            })"""
        )
        print(tag, info)

    dump("before")
    page.locator("#acct-nav-btn").click(timeout=5000)
    page.wait_for_timeout(1500)
    page.screenshot(path="out/probe/acct_nav.png")
    dump("after_nav")
    try:
        page.locator(".acct-add-btn").first.click(timeout=4000, force=True)
        print("force click ok")
        page.wait_for_timeout(2000)
        page.screenshot(path="out/probe/acct_add_after.png")
        print("url", page.url)
    except Exception as e:
        print("force fail", str(e)[:300])
