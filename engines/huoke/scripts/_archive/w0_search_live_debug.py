# -*- coding: utf-8 -*-
"""
W0 live debug: runs search with proper automation, takes screenshots at each step.
Output to file to avoid encoding issues.
"""
import sys, time
from pathlib import Path
base = Path(__file__).parent.parent
sys.path.insert(0, str(base))

DEVICE = "LVHIOZSWDAYLCELN"

out = open(str(base / "data" / "w0_live_debug.txt"), "w", encoding="utf-8")

try:
    from src.app_automation.facebook import FacebookAutomation
    import uiautomator2 as u2
    from src.vision.screen_parser import XMLParser

    fb = FacebookAutomation()
    d = u2.connect(DEVICE)

    # Take step0 screenshot
    d.screenshot(str(base / "data" / "w0_live_s0.png"))
    out.write("step0: home screen\n")

    # Click search icon manually via hb.tap (at correct coord)
    fb.hb.tap(d, 580, 112)
    out.write("clicked search icon at (580, 112)\n")
    time.sleep(1.5)

    d.screenshot(str(base / "data" / "w0_live_s1.png"))
    out.write("step1: after clicking search\n")

    # Dump hierarchy
    xml1 = d.dump_hierarchy()
    (base / "data" / "w0_live_s1.xml").write_text(xml1, encoding="utf-8")
    els1 = XMLParser.parse(xml1)
    out.write(f"step1 elements: {len(els1)}\n")
    for e in els1[:50]:
        if e.text or (hasattr(e, 'content_desc') and e.content_desc):
            b = e.bounds
            out.write(f"  text={repr(e.text):30s}  cd={repr(getattr(e,'content_desc','') or ''):20s}  cls={e.class_name}  focused={e.focused}  bounds={b}\n")

    # Type search query
    out.write("\nTyping 'Yumi Tanaka'...\n")
    fb.hb.type_text(d, "Yumi Tanaka", clear_first=True)
    time.sleep(2)

    d.screenshot(str(base / "data" / "w0_live_s2.png"))
    out.write("step2: after typing\n")

    # Press enter
    d.press("enter")
    time.sleep(4)

    d.screenshot(str(base / "data" / "w0_live_s3.png"))
    out.write("step3: after pressing enter (search results)\n")

    # Dump results hierarchy
    xml3 = d.dump_hierarchy()
    (base / "data" / "w0_live_s3.xml").write_text(xml3, encoding="utf-8")
    els3 = XMLParser.parse(xml3)
    out.write(f"step3 elements: {len(els3)}\n")

    out.write("\n=== Clickable elements in results ===\n")
    for e in els3:
        if e.clickable and e.text:
            b = e.bounds
            out.write(f"  text={repr(e.text):45s}  y={b[1] if b else '?':5}  bounds={b}\n")

    out.write("\n=== All text ===\n")
    for e in els3[:80]:
        if e.text:
            b = e.bounds
            out.write(f"  {repr(e.text):50s}  bounds={b}\n")

except Exception as e:
    out.write(f"ERROR: {e}\n")
    import traceback
    out.write(traceback.format_exc())

out.close()
print("Output: data/w0_live_debug.txt")
