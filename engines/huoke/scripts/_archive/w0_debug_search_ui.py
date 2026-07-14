# -*- coding: utf-8 -*-
"""W0 debug: dump FB search result UI to find People tab selector."""

from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="LVHIOZSWDAYLCELN")
    ap.add_argument("--query", default="Yumi Tanaka")
    args = ap.parse_args()

    base = Path(__file__).parent.parent
    sys.path.insert(0, str(base))

    import uiautomator2 as u2
    import subprocess

    print("connect:", args.device)
    d = u2.connect(args.device)
    print("device:", d.info.get("productName", "?"))

    # Launch FB
    subprocess.run(
        ["C:\\platform-tools\\adb.exe", "-s", args.device, "shell",
         "monkey", "-p", "com.facebook.katana", "-c", "android.intent.category.LAUNCHER", "1"],
        capture_output=True, timeout=10,
    )
    time.sleep(3)

    # Find and click search bar
    search_found = False
    for sel in [
        {"description": "Search Facebook"},
        {"resourceId": "com.facebook.katana:id/search_bar"},
        {"className": "android.widget.EditText"},
    ]:
        el = d(**sel)
        if el.exists(timeout=3):
            el.click()
            print("search bar clicked:", sel)
            search_found = True
            break

    if not search_found:
        print("search bar NOT found, dumping XML...")
        xml = d.dump_hierarchy()
        (base / "data" / "w0_debug_no_search.xml").write_text(xml, encoding="utf-8")
        d.screenshot(str(base / "data" / "w0_debug_no_search.png"))
        print("saved XML + screenshot")
        return

    time.sleep(1.5)

    # Type search query
    active = d(focused=True)
    if active.exists(timeout=2):
        active.set_text(args.query)
    else:
        d.send_keys(args.query)
    time.sleep(1.5)

    d.screenshot(str(base / "data" / "w0_debug_input.png"))
    print("screenshot after input: data/w0_debug_input.png")

    # Press Enter
    d.press("enter")
    time.sleep(4)

    # Screenshot search results
    d.screenshot(str(base / "data" / "w0_debug_search_result.png"))
    print("screenshot search result: data/w0_debug_search_result.png")

    # Dump hierarchy
    xml = d.dump_hierarchy()
    xml_path = base / "data" / "w0_debug_search.xml"
    xml_path.write_text(xml, encoding="utf-8")
    print(f"hierarchy XML: data/w0_debug_search.xml ({len(xml)} bytes)")

    # Parse and print clickable elements
    from src.vision.screen_parser import XMLParser
    elements = XMLParser.parse(xml)
    clickable = [e for e in elements if e.clickable and e.text]
    print(f"\n=== Clickable elements with text ({len(clickable)} total) ===")
    for i, el in enumerate(clickable[:60]):
        bounds = el.bounds if hasattr(el, 'bounds') else None
        desc = getattr(el, 'content_desc', '') or ''
        print(f"  [{i:02d}] text={el.text!r:35}  desc={desc!r:25}  bounds={bounds}")

    # Tab candidates (top of screen, y < 350)
    print("\n=== Tab candidates (y < 350) ===")
    for el in elements:
        bounds = el.bounds if hasattr(el, 'bounds') else None
        if bounds and bounds[1] < 350 and el.clickable:
            desc = getattr(el, 'content_desc', '') or ''
            print(f"  text={el.text!r:35}  desc={desc!r:25}  cls={el.class_name}  bounds={bounds}")

    print("\nDone. Check data/w0_debug_search_result.png")


if __name__ == "__main__":
    main()
