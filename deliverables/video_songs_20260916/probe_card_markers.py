#!/usr/bin/env python3
from chatx_session import session
import json

with session() as (page, _):
    page.wait_for_timeout(1200)
    page.locator(".conv-item").nth(0).click(timeout=5000)
    page.wait_for_timeout(2000)

    def open_card(tab, key):
        page.evaluate(
            """({tab, key}) => {
              const b = document.querySelector('button.ws-cp-tab[data-tab="'+tab+'"]');
              if (b) b.click();
            }""",
            {"tab": tab, "key": key},
        )
        page.wait_for_timeout(700)
        page.evaluate(
            """(key) => {
              const card = document.querySelector('[data-cp-card="'+key+'"]');
              if (card) card.scrollIntoView({block:'center'});
              const hd = document.querySelector('[data-cp-toggle="'+key+'"]');
              if (hd) hd.click();
              // if still collapsed, click again
            }""",
            key,
        )
        page.wait_for_timeout(1200)
        # ensure open
        page.evaluate(
            """(key) => {
              const card = document.querySelector('[data-cp-card="'+key+'"]');
              if (card && card.classList.contains('collapsed')) {
                const hd = document.querySelector('[data-cp-toggle="'+key+'"]');
                if (hd) hd.click();
              }
            }""",
            key,
        )
        page.wait_for_timeout(1000)

    for tab, key in (("tools", "voice"), ("customer", "goal")):
        open_card(tab, key)
        info = page.evaluate(
            """(key) => {
              const card = document.querySelector('[data-cp-card="'+key+'"]');
              const host = card ? card.querySelector('cp-voice, cp-goal, #ws-cp-voice, #ws-cp-goal') : null;
              const root = host && host.shadowRoot ? host.shadowRoot : host;
              const texts = [];
              if (root) {
                root.querySelectorAll('button, a, [role=button], label, .btn, h1, h2, h3, .ttl, .title').forEach(el => {
                  const t = (el.innerText || el.textContent || '').trim().replace(/\\s+/g,' ');
                  if (t) texts.push(t.slice(0,60));
                });
              }
              const html = root ? (root.innerHTML || '').slice(0, 1200) : null;
              return {
                collapsed: card ? card.classList.contains('collapsed') : null,
                h: card ? Math.round(card.getBoundingClientRect().height) : 0,
                host: host ? host.tagName : null,
                texts: texts.slice(0, 30),
                html,
              };
            }""",
            key,
        )
        print("====", key)
        print(json.dumps({k: info[k] for k in ("collapsed", "h", "host", "texts")}, ensure_ascii=False, indent=2))
        print("html snippet:", (info.get("html") or "")[:600])
        page.screenshot(path=f"out/probe/e78_{key}_open.png")
