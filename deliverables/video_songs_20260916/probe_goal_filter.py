#!/usr/bin/env python3
"""确认「今日有推进」筛芯片可见选择器。"""
from chatx_session import session

with session() as (page, _):
    page.wait_for_timeout(1200)
    page.locator(".conv-item").nth(0).click(timeout=5000)
    page.wait_for_timeout(1500)
    info = page.evaluate(
        """() => {
          const sels = [
            '.goal-af-chip[data-gaf=push]',
            'button.goal-af-chip',
            '#goal-af',
            '.goal-af',
            '[data-gaf=push]'
          ];
          return sels.map(s => {
            const e = document.querySelector(s);
            if (!e) return {s, miss:true};
            const r = e.getBoundingClientRect();
            return {s, t:(e.innerText||'').trim().slice(0,40),
              w:Math.round(r.width), h:Math.round(r.height),
              vis: r.width>2 && r.height>2};
          });
        }"""
    )
    print(info)
