# -*- coding: utf-8 -*-
"""W0 debug v2: use FacebookAutomation to search, then dump result page."""
from __future__ import annotations
import sys
import time
from pathlib import Path

base = Path(__file__).parent.parent
sys.path.insert(0, str(base))

DEVICE = "LVHIOZSWDAYLCELN"

from src.app_automation.facebook import FacebookAutomation
import uiautomator2 as u2

print("init automation...")
fb = FacebookAutomation()
d = u2.connect(DEVICE)

print("calling search_people('Yumi Tanaka')...")
results = fb.search_people("Yumi Tanaka", device_id=DEVICE, max_results=5)
print(f"search returned: {results}")

# dump current screen
time.sleep(1)
d.screenshot(str(base / "data" / "w0_after_search.png"))
print("screenshot: data/w0_after_search.png")

xml = d.dump_hierarchy()
(base / "data" / "w0_after_search.xml").write_text(xml, encoding="utf-8")
print(f"hierarchy: data/w0_after_search.xml ({len(xml)} bytes)")

# parse XML and print elements
from src.vision.screen_parser import XMLParser
elements = XMLParser.parse(xml)
print(f"\nTotal elements: {len(elements)}")
print("\n=== All clickable with text ===")
for e in elements:
    if e.clickable and e.text:
        b = e.bounds
        desc = getattr(e, 'content_desc', '') or ''
        print(f"  text={e.text!r:40}  desc={desc!r:20}  bounds={b}")

print("\n=== All text elements (no clickable filter) ===")
for e in elements[:80]:
    if e.text:
        b = e.bounds
        print(f"  text={e.text!r:40}  bounds={b}")
