import xml.etree.ElementTree as ET
tree = ET.parse("logs/tiktok_home_dump.xml")
root = tree.getroot()
count = 0
for el in root.iter():
    rid = el.get("resource-id", "")
    desc = el.get("content-desc", "")
    text = el.get("text", "")
    cls = el.get("class", "")
    click = el.get("clickable", "")
    if rid or desc or text:
        bounds = el.get("bounds", "")
        parts = []
        if rid:
            parts.append(f"id={rid}")
        if desc:
            parts.append(f"desc={desc!r}")
        if text:
            parts.append(f"text={text!r}")
        if click == "true":
            parts.append("CLICK")
        print(f"{cls}: {' | '.join(parts)}  {bounds}")
        count += 1
print(f"--- Total: {count} ---")
