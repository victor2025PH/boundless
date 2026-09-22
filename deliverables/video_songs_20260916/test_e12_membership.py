#!/usr/bin/env python3
"""活体闸：E12 /membership 关键控件可得点。"""
from __future__ import annotations

import sys
from chatx_session import session, BASE


def main() -> int:
    fails = []
    with session() as (page, _):
        page.goto(BASE + "/membership", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)
        if "/membership" not in page.url:
            fails.append(f"url={page.url}")
        for sel, label in (("#mb-hero", "hero"), ("#mb-buy", "buy")):
            el = page.locator(sel).first
            if el.count() == 0:
                fails.append(f"missing {label}")
                continue
            try:
                el.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            box = el.bounding_box()
            if not box or box["width"] < 40 or box["height"] < 40:
                fails.append(f"{label} box={box}")
            else:
                print(f"  OK {label} {int(box['width'])}x{int(box['height'])}")
        for text in ("购买 / 续费", "免费标准翻译"):
            loc = page.get_by_text(text, exact=False)
            hit = False
            for i in range(min(loc.count(), 8)):
                try:
                    if loc.nth(i).is_visible():
                        hit = True
                        break
                except Exception:
                    continue
            print(f"  text {text!r} → {'OK' if hit else 'MISS'}")
            if not hit:
                fails.append(f"text:{text}")
    print("== test_e12_membership ==")
    print("RESULT", "FAIL" if fails else "PASS", fails or "")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
