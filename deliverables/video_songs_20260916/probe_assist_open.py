#!/usr/bin/env python3
"""探：如何真正打开业务助手并滚到 voice/goal 卡。"""
from chatx_session import session
import json

with session() as (page, _):
    page.wait_for_timeout(1500)
    page.locator(".conv-item").nth(0).click(timeout=5000)
    page.wait_for_timeout(2000)
    # 点「助手」
    btn = page.locator("#info-toggle-btn")
    print("info-toggle", btn.count(), "vis", btn.count() and btn.first.is_visible())
    if btn.count():
        btn.first.click(timeout=4000)
        page.wait_for_timeout(1500)
    layout = page.evaluate(
        """() => {
          const ids = ['#info-sidebar','#ws-cp','#copilot','#right-pane','.info-sidebar','#infoPane'];
          const out = {};
          for (const s of ids) {
            const e = document.querySelector(s);
            if (!e) { out[s] = null; continue; }
            const r = e.getBoundingClientRect();
            out[s] = {w: Math.round(r.width), h: Math.round(r.height), x: Math.round(r.left),
                      cls: (e.className||'').toString().slice(0,80)};
          }
          return out;
        }"""
    )
    print("layout", json.dumps(layout, ensure_ascii=False, indent=2))
    # force open via API if any
    page.evaluate(
        """() => {
          if (typeof toggleInfoSidebar === 'function') try { toggleInfoSidebar(true); } catch(e) {}
          if (typeof openInfoSidebar === 'function') try { openInfoSidebar(); } catch(e) {}
          document.body.classList.add('info-open','cp-open');
        }"""
    )
    page.wait_for_timeout(1000)
    # scroll ws-cp and expand voice
    res = page.evaluate(
        """() => {
          const root = document.querySelector('#ws-cp, #info-sidebar, .ws-cp');
          const voice = document.querySelector('[data-cp-card="voice"]');
          const goal = document.querySelector('[data-cp-card="goal"]');
          if (voice) {
            voice.scrollIntoView({block:'center'});
            const h = voice.querySelector('.cp-card-hd, .cp-card-ttl, .cp-card-head') || voice;
            h.click();
          }
          if (goal) {
            goal.scrollIntoView({block:'center'});
            const h = goal.querySelector('.cp-card-hd, .cp-card-ttl, .cp-card-head') || goal;
            h.click();
          }
          const pack = (e) => e ? {
            vis: (()=>{const r=e.getBoundingClientRect(); return r.width>5&&r.height>5 && r.top<innerHeight;})(),
            collapsed: e.classList.contains('collapsed'),
            t: (e.innerText||'').trim().replace(/\\s+/g,' ').slice(0,180)
          } : null;
          return {root: !!root, voice: pack(voice), goal: pack(goal),
                  rootRect: root ? (()=>{const r=root.getBoundingClientRect(); return [r.left,r.width];})() : null};
        }"""
    )
    print("expand", json.dumps(res, ensure_ascii=False, indent=2))
    page.screenshot(path="out/probe/e78_assist.png")
