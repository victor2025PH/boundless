# -*- coding: utf-8 -*-
"""手动测试: 用正确坐标点搜索，截图看结果页。输出到文件。"""
import sys, time
from pathlib import Path
base = Path(__file__).parent.parent
sys.path.insert(0, str(base))

DEVICE = "LVHIOZSWDAYLCELN"

import uiautomator2 as u2
from src.vision.screen_parser import XMLParser

out = open(str(base / "data" / "w0_search_test_out.txt"), "w", encoding="utf-8")

d = u2.connect(DEVICE)
out.write(f"device: {d.info.get('productName')}\n")

# Step 1: Screenshot current state
d.screenshot(str(base / "data" / "w0_test_step0.png"))
out.write("step0 screenshot saved\n")

# Step 2: Click search icon via description "搜索"
search_found = False
el = d(description="搜索")
if el.exists(timeout=3):
    info = el.info
    out.write(f"found search icon: {info}\n")
    el.click()
    search_found = True
    out.write("clicked via description=搜索\n")

if not search_found:
    # Fallback: click at correct coord (580, 112)
    d.click(580, 112)
    out.write("clicked via coord (580, 112)\n")

time.sleep(2)

# Screenshot after click
d.screenshot(str(base / "data" / "w0_test_step1.png"))
out.write("step1 screenshot (after search icon click)\n")

# Step 3: dump hierarchy to see search input
xml1 = d.dump_hierarchy()
(base / "data" / "w0_test_step1.xml").write_text(xml1, encoding="utf-8")
els1 = XMLParser.parse(xml1)
out.write(f"step1 elements: {len(els1)}\n")

# Find search input
for e in els1[:50]:
    rid = getattr(e, 'resource_id', '') or ''
    cls = e.class_name or ''
    out.write(f"  text={repr(e.text):30s}  rid={repr(rid[:60]):65s}  cls={cls}  focused={e.focused}\n")

# Step 4: type search text
out.write("\nTyping search text...\n")
# find EditText
edit_el = d(className="android.widget.EditText")
if edit_el.exists(timeout=3):
    edit_el.set_text("Yumi Tanaka")
    out.write("typed via EditText.set_text\n")
else:
    # Try direct keyboard
    d.send_keys("Yumi Tanaka")
    out.write("typed via send_keys\n")

time.sleep(2)
d.screenshot(str(base / "data" / "w0_test_step2.png"))
out.write("step2 screenshot (after typing)\n")

# Step 5: press search/enter
d.press("enter")
time.sleep(5)

d.screenshot(str(base / "data" / "w0_test_step3.png"))
out.write("step3 screenshot (search results)\n")

xml3 = d.dump_hierarchy()
(base / "data" / "w0_test_step3.xml").write_text(xml3, encoding="utf-8")
els3 = XMLParser.parse(xml3)
out.write(f"step3 total elements: {len(els3)}\n\n")

out.write("=== Clickable elements on result page ===\n")
for e in els3:
    if e.clickable and e.text:
        b = e.bounds
        out.write(f"  text={repr(e.text):45s}  y={b[1] if b else '?':5}  bounds={b}\n")

out.write("\n=== All text elements (first 100) ===\n")
for e in els3[:100]:
    if e.text:
        out.write(f"  {repr(e.text):50s}  bounds={e.bounds}\n")

out.close()
print("Output written to data/w0_search_test_out.txt")
print("Screenshots: w0_test_step0.png to w0_test_step3.png")
