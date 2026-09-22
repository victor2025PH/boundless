#!/usr/bin/env python3
"""活体闸：point_in_card 能命中 shadow 内「生成语音」「设定目标」。"""
from __future__ import annotations

import sys
from chatx_session import session
from record_chatx import inject_pointer, point_and_maybe_click


def open_card(page, tab: str, key: str) -> None:
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
        }""",
        key,
    )
    page.wait_for_timeout(900)
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
    page.wait_for_timeout(600)


def find_in_card(page, key: str, text: str):
    card = page.locator(f'[data-cp-card="{key}"]').first
    if not card.count():
        return None
    loc = card.get_by_text(text, exact=False)
    for i in range(min(loc.count(), 8)):
        cand = loc.nth(i)
        try:
            if cand.is_visible():
                return cand
        except Exception:
            continue
    return None


def main() -> int:
    fails = []
    with session() as (page, _):
        inject_pointer(page)
        page.locator(".conv-item").nth(0).click(timeout=5000)
        page.wait_for_timeout(1800)
        page.evaluate(
            """() => {
              if (typeof window.toggleInfoSidebar === 'function') {
                const m = window._cpPanelModeGet && window._cpPanelModeGet();
                if (m === 'rail' || m === 'hidden') window.toggleInfoSidebar();
              }
            }"""
        )
        for tab, key, text in (
            ("tools", "voice", "生成语音"),
            ("customer", "goal", "设定目标"),
        ):
            open_card(page, tab, key)
            el = find_in_card(page, key, text)
            ok = el is not None
            print(f"  {key}/{text} → {'OK' if ok else 'MISS'}")
            if not ok:
                fails.append(f"{key}:{text}")
            else:
                point_and_maybe_click(page, el, f"测【{text}】", click=False, hold_ms=600)
    print("== test_point_in_card ==")
    print("RESULT", "FAIL" if fails else "PASS", fails or "")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
