# -*- coding: utf-8 -*-
"""Dump FB search result XML, output to file."""
import sys, time, json
from pathlib import Path

base = Path(__file__).parent.parent
sys.path.insert(0, str(base))
out = base / "data" / "w0_search_dump_out.txt"
fh = open(str(out), "w", encoding="utf-8")

import uiautomator2 as u2
DEVICE = "LVHIOZSWDAYLCELN"
d = u2.connect(DEVICE)
fh.write(f"device: {d.info.get('productName')}\n")

# Click search icon by content-desc="搜索" or coord
for sel in [{"description": "搜索"}, {"resourceId": "com.facebook.katana:id/action_search"}]:
    el = d(**sel)
    if el.exists(timeout=2):
        el.click()
        fh.write(f"search icon clicked via: {sel}\n")
        break
else:
    # fallback coord
    d.click(580, 112)
    fh.write("search icon clicked via coord (580,112)\n")

time.sleep(2)

# Find search input
search_input = d(resourceId="com.facebook.katana:id/search_query_text_view")
if not search_input.exists(timeout=3):
    search_input = d(focused=True)
if not search_input.exists(timeout=2):
    search_input = d(className="android.widget.EditText")

if search_input.exists(timeout=2):
    search_input.set_text("Yumi Tanaka")
    fh.write("typed: Yumi Tanaka\n")
else:
    d.send_keys("Yumi Tanaka")
    fh.write("typed via send_keys\n")

time.sleep(1.5)
d.press("enter")
time.sleep(5)

# Screenshot
d.screenshot(str(base / "data" / "w0_search_result_v4.png"))
fh.write("screenshot: w0_search_result_v4.png\n")

# Dump XML
xml = d.dump_hierarchy()
(base / "data" / "w0_search_result_v4.xml").write_text(xml, encoding="utf-8")
fh.write(f"xml saved ({len(xml)} bytes)\n")

# Parse elements
from src.vision.screen_parser import XMLParser
els = XMLParser.parse(xml)
fh.write(f"total elements: {len(els)}\n\n")

fh.write("=== Clickable with text ===\n")
for e in els:
    if e.clickable and e.text:
        b = e.bounds
        desc = getattr(e, 'content_desc', '') or ''
        rid = getattr(e, 'resource_id', '') or ''
        fh.write(f"  text={repr(e.text):40s}  y={b[1] if b else '?':5}  desc={repr(desc):30s}  rid={repr(rid[:50])}\n")

fh.write("\n=== All text elements ===\n")
for e in els[:150]:
    if e.text:
        b = e.bounds
        fh.write(f"  {repr(e.text):50s}  bounds={b}\n")

fh.close()
print(f"Output written to: {out}")
