"""
Search Italian hashtag → find creators → dump their profile for analysis.
Goal: understand what info is available for country/gender/age detection.
"""
import sys, time, logging, xml.etree.ElementTree as ET
sys.path.insert(0, ".")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from src.device_control.device_manager import get_device_manager

DID = "8D7DWWUKQGJRNN79"
PKG = "com.ss.android.ugc.trill"

dm = get_device_manager("config/devices.yaml")
d = dm.get_u2(DID)

def dump(label):
    xml_str = d.dump_hierarchy()
    root = ET.fromstring(xml_str)
    print(f"\n--- {label} ---")
    for el in root.iter():
        rid = el.get("resource-id", "")
        desc = el.get("content-desc", "")
        text = el.get("text", "")
        click = el.get("clickable", "false")
        bounds = el.get("bounds", "")
        if not (rid or desc or text):
            continue
        if "systemui" in rid or "inputmethod" in rid:
            continue
        parts = []
        if rid:
            parts.append("id=" + rid.replace(PKG + ":", "TT:"))
        if desc:
            parts.append("desc=" + repr(desc[:120]))
        if text:
            parts.append("text=" + repr(text[:120]))
        if click == "true":
            parts.append("CLICK")
        line = " | ".join(parts) + "  " + bounds
        if "miui" not in line.lower() or "icon_title" not in line:
            print(line)

# Launch TikTok
print("1. Launching TikTok...")
d.app_stop(PKG)
time.sleep(1)
d.app_start(PKG)
time.sleep(8)
for txt in ["Skip", "Not now", "Allow", "OK", "Got it", "Close",
            "I agree", "Accept all", "Continue", "CONTINUE"]:
    btn = d(text=txt)
    if btn.exists(timeout=0.3):
        btn.click()
        time.sleep(0.5)

# Search for Italian hashtag
print("2. Searching #italia...")
search = d(description="Search")
if not search.exists(timeout=3):
    search = d(resourceId=PKG + ":id/izy")
search.click()
time.sleep(2)

edit = d(className="android.widget.EditText")
if edit.exists(timeout=3):
    edit.set_text("italia")
    time.sleep(0.5)
    d.press("enter")
    time.sleep(3)

# Switch to Users tab to find Italian creators
print("3. Switching to Users tab...")
for tab in ["Users", "Accounts", "People"]:
    el = d(text=tab)
    if el.exists(timeout=2):
        el.click()
        time.sleep(2)
        break

dump("SEARCH RESULTS - USERS")

# Click the first user result to open their profile
print("\n4. Clicking first user to see profile...")
# Try finding a clickable row that's not the tabs
results = d(className="android.widget.TextView", clickable=True)
clicked = False
if results.exists(timeout=2):
    for i in range(results.count):
        try:
            r = results[i]
            info = r.info
            b = info.get("bounds", {})
            t = info.get("text", "")
            # Skip tab labels and empty text
            if b.get("top", 0) > 300 and t and t not in ("Users", "Accounts",
                "Videos", "Sounds", "LIVE", "Hashtags", "Top"):
                print(f"   Clicking: '{t}' at y={b.get('top')}")
                r.click()
                time.sleep(3)
                clicked = True
                break
        except Exception:
            continue

if not clicked:
    # Try generic clickable approach
    rows = d(clickable=True, className="android.view.ViewGroup")
    if rows.exists(timeout=2):
        for i in range(rows.count):
            try:
                b = rows[i].info.get("bounds", {})
                if b.get("top", 0) > 300 and b.get("bottom", 0) - b.get("top", 0) > 50:
                    rows[i].click()
                    time.sleep(3)
                    clicked = True
                    break
            except Exception:
                continue

if clicked:
    dump("USER PROFILE PAGE")
    
    # Try to find and click on Followers
    print("\n5. Opening Followers list...")
    fol = d(text="Followers")
    if fol.exists(timeout=3):
        fol.click()
        time.sleep(3)
        dump("FOLLOWERS LIST")
        
        # Click first follower to see their profile
        print("\n6. Clicking a follower...")
        follow_btns = d(text="Follow")
        if follow_btns.exists(timeout=2) and follow_btns.count > 0:
            btn = follow_btns[0]
            b = btn.info.get("bounds", {})
            # Click the name area (left side of the row)
            row_y = (b.get("top", 0) + b.get("bottom", 0)) // 2
            d.click(150, row_y)
            time.sleep(3)
            dump("INDIVIDUAL FOLLOWER PROFILE")
        else:
            print("   No Follow buttons found")
    else:
        print("   Followers link not found")
else:
    print("   Could not click any user")

print("\n=== Done ===")
