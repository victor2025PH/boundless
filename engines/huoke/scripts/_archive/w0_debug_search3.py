# -*- coding: utf-8 -*-
"""W0 debug v3: direct u2 search, dump XML, find People tab."""
from __future__ import annotations
import sys
import time
from pathlib import Path

base = Path(__file__).parent.parent
sys.path.insert(0, str(base))

DEVICE = "LVHIOZSWDAYLCELN"

import uiautomator2 as u2

d = u2.connect(DEVICE)
print("connected:", d.info.get("productName"))

# --- Step 1: dump home screen to find search icon ---
xml_home = d.dump_hierarchy()
(base / "data" / "w0_home.xml").write_text(xml_home, encoding="utf-8")
d.screenshot(str(base / "data" / "w0_home_screen.png"))
print("home XML and screenshot saved")

# Parse and print elements near top of screen
from src.vision.screen_parser import XMLParser
els = XMLParser.parse(xml_home)
print("\n=== Home screen - elements with y < 200 ===")
for e in els:
    b = e.bounds
    if b and b[1] < 200:
        desc = getattr(e, 'content_desc', '') or ''
        rid = getattr(e, 'resource_id', '') or ''
        print(f"  text={e.text!r:30}  desc={desc!r:30}  rid={rid!r:50}  click={e.clickable}  bounds={b}")

print("\n=== Home screen - all elements with resourceId containing 'search' ===")
for e in els:
    rid = getattr(e, 'resource_id', '') or ''
    desc = getattr(e, 'content_desc', '') or ''
    if 'search' in rid.lower() or 'search' in desc.lower():
        b = e.bounds
        print(f"  text={e.text!r:20}  desc={desc!r:30}  rid={rid!r:50}  bounds={b}")

# --- Step 2: click search icon (try resourceId approach) ---
print("\n--- Trying to find search icon ---")
search_clicked = False

# Try by resource ID
for rid in ["com.facebook.katana:id/search_button",
            "com.facebook.katana:id/action_search",
            "com.facebook.katana:id/search_bar_text_view",
            "com.facebook.katana:id/search_bar"]:
    el = d(resourceId=rid)
    if el.exists(timeout=1):
        print(f"Found search by resourceId: {rid}")
        el.click()
        search_clicked = True
        break

if not search_clicked:
    # Try coordinates from previous successful run
    print("Trying coordinate tap at (633, 96)...")
    d.click(633, 96)
    search_clicked = True

time.sleep(2)

# dump after click
xml_after_click = d.dump_hierarchy()
(base / "data" / "w0_after_click.xml").write_text(xml_after_click, encoding="utf-8")
d.screenshot(str(base / "data" / "w0_after_click.png"))
print("screenshot after click saved")

# check if search box is now active
els2 = XMLParser.parse(xml_after_click)
print("\n=== After click - focused/EditText elements ===")
for e in els2:
    rid = getattr(e, 'resource_id', '') or ''
    if e.focused or (e.class_name and 'Edit' in e.class_name):
        b = e.bounds
        print(f"  text={e.text!r:20}  focused={e.focused}  rid={rid!r}  bounds={b}")

# --- Step 3: type search query ---
print("\nTyping search query...")
# Find the active input field
typed = False
for e in els2:
    if e.focused or (e.class_name and 'Edit' in e.class_name):
        rid = getattr(e, 'resource_id', '') or ''
        el = d(resourceId=rid) if rid else d(focused=True)
        if el.exists(timeout=2):
            el.set_text("Yumi Tanaka")
            typed = True
            print(f"  typed via: {rid or 'focused'}")
            break

if not typed:
    print("  falling back to send_keys")
    d.send_keys("Yumi Tanaka")

time.sleep(2)
d.press("enter")
time.sleep(5)

# --- Step 4: dump search result page ---
xml_result = d.dump_hierarchy()
(base / "data" / "w0_search_result_v3.xml").write_text(xml_result, encoding="utf-8")
d.screenshot(str(base / "data" / "w0_search_result_v3.png"))
print("search result XML and screenshot saved")

els3 = XMLParser.parse(xml_result)
print(f"\n=== Search result page ({len(els3)} elements) ===")
print("Clickable with text:")
for e in els3:
    if e.clickable and e.text:
        b = e.bounds
        desc = getattr(e, 'content_desc', '') or ''
        rid = getattr(e, 'resource_id', '') or ''
        print(f"  text={e.text!r:40}  y={b[1] if b else '?':4}  desc={desc!r:20}  rid={rid[:40]!r}")

print("\n=== All text elements (first 100) ===")
for e in els3[:100]:
    if e.text:
        b = e.bounds
        print(f"  text={e.text!r}  bounds={b}")

print("\nDone!")
