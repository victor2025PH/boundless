"""Quick UI dump for the current TikTok screen."""
import sys, xml.etree.ElementTree as ET
sys.path.insert(0, ".")
from src.device_control.device_manager import get_device_manager

dm = get_device_manager("config/devices.yaml")
d = dm.get_u2("8D7DWWUKQGJRNN79")
xml_str = d.dump_hierarchy()
root = ET.fromstring(xml_str)

for el in root.iter():
    rid = el.get("resource-id", "")
    desc = el.get("content-desc", "")
    text = el.get("text", "")
    click = el.get("clickable", "false")
    bounds = el.get("bounds", "")
    if rid or desc or text:
        parts = []
        if rid:
            parts.append("id=" + rid)
        if desc:
            parts.append("desc=" + repr(desc[:80]))
        if text:
            parts.append("text=" + repr(text[:80]))
        if click == "true":
            parts.append("CLICK")
        print(" | ".join(parts) + "  " + bounds)
