"""
Try multiple strategies to find Italian users and dump their profiles.
Strategy 1: Search specific Italian creator name
Strategy 2: Search #italiano hashtag → Videos tab → click creator
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

def dump(label, max_lines=80):
    xml_str = d.dump_hierarchy()
    root = ET.fromstring(xml_str)
    print(f"\n--- {label} ---")
    count = 0
    for el in root.iter():
        rid = el.get("resource-id", "")
        desc = el.get("content-desc", "")
        text = el.get("text", "")
        click = el.get("clickable", "false")
        bounds = el.get("bounds", "")
        if not (rid or desc or text):
            continue
        if "systemui" in rid or "inputmethod" in rid or "miui" in rid.lower():
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
        print(" | ".join(parts) + "  " + bounds)
        count += 1
        if count >= max_lines:
            print(f"  ... (truncated at {max_lines})")
            break

# TikTok should still be running; go back to clear state
print("1. Clearing state...")
for _ in range(5):
    d.press("back")
    time.sleep(0.5)

# Verify TikTok is still up, relaunch if needed
cur = d.app_current()
if cur.get("package") != PKG:
    d.app_start(PKG)
    time.sleep(6)

# Click Search
print("2. Opening search...")
search = d(description="Search")
if not search.exists(timeout=3):
    search = d(resourceId=PKG + ":id/izy")
    if not search.exists(timeout=3):
        # Try the search bar area
        search = d(resourceId=PKG + ":id/e3y")
if search.exists(timeout=3):
    search.click()
    time.sleep(2)

# Search for a known Italian creator
print("3. Searching for 'khaby.lame' (big Italian creator)...")
edit = d(className="android.widget.EditText")
if edit.exists(timeout=3):
    edit.clear_text()
    time.sleep(0.3)
    edit.set_text("khaby.lame")
    time.sleep(0.5)
    d.press("enter")
    time.sleep(4)

    # Switch to Users
    for tab in ["Users", "Accounts", "People"]:
        el = d(text=tab)
        if el.exists(timeout=2):
            el.click()
            time.sleep(2)
            break

    dump("SEARCH khaby.lame - USERS", 50)

    # Click first result
    # Usually the first clickable user result
    user_text = d(textContains="khaby")
    if not user_text.exists(timeout=2):
        user_text = d(textContains="Khaby")
    if not user_text.exists(timeout=2):
        user_text = d(textContains="Khabane")

    if user_text.exists(timeout=2):
        print(f"\n4. Found user: {user_text.get_text()}")
        user_text.click()
        time.sleep(4)
        dump("KHABY PROFILE", 80)

        # Look for followers link
        print("\n5. Looking for Followers...")
        fol = d(text="Followers")
        if not fol.exists(timeout=2):
            fol = d(textContains="Followers")
        if fol.exists(timeout=2):
            fol_text = fol.get_text()
            print(f"   Found: '{fol_text}'")
            fol.click()
            time.sleep(4)
            dump("FOLLOWERS LIST", 80)

            # Click into first follower
            print("\n6. Clicking a follower to see their profile...")
            follow_btns = d(text="Follow")
            if follow_btns.exists(timeout=2) and follow_btns.count > 0:
                btn = follow_btns[0]
                b = btn.info.get("bounds", {})
                row_y = (b.get("top", 0) + b.get("bottom", 0)) // 2
                d.click(150, row_y)
                time.sleep(4)
                dump("FOLLOWER PROFILE", 80)
            else:
                print("   No Follow buttons visible")
    else:
        print("   User not found in search results")
        dump("CURRENT SCREEN", 40)
else:
    print("   Edit text not found")

print("\n=== Done ===")
