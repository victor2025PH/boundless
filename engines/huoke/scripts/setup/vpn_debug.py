# -*- coding: utf-8 -*-
"""Debug V2RayNG UI — dump current screen elements."""
import sys
import time
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEVICE_ID = sys.argv[1] if len(sys.argv) > 1 else "AIUKQ8WSKZBUQK4X"

import uiautomator2 as u2

d = u2.connect(DEVICE_ID)

# Screenshot
img = d.screenshot()
img.save("data/vpn_debug_screen.png")
print(f"截图保存: data/vpn_debug_screen.png")

# Current activity
pkg = d.app_current()
print(f"\n当前应用: {pkg}")

# Dump all elements
print(f"\n=== UI 元素 ===")
try:
    xml = d.dump_hierarchy()
    
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml)
    
    for elem in root.iter():
        cls = elem.get("class", "")
        text = elem.get("text", "")
        res_id = elem.get("resource-id", "")
        desc = elem.get("content-desc", "")
        clickable = elem.get("clickable", "")
        bounds = elem.get("bounds", "")
        
        if text or res_id or desc:
            short_cls = cls.split(".")[-1] if cls else ""
            parts = []
            if text:
                parts.append(f'text="{text}"')
            if res_id:
                parts.append(f'id="{res_id}"')
            if desc:
                parts.append(f'desc="{desc}"')
            if clickable == "true":
                parts.append("clickable")
            
            print(f"  [{short_cls}] {' | '.join(parts)}")
except Exception as e:
    print(f"  Dump failed: {e}")
