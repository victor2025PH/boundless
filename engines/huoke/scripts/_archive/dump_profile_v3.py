"""
Navigate to a creator's profile via For You feed, then dump profile + follower list.
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

def dump_filtered(label, pkg_filter=PKG):
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
        pkg = el.get("package", "")
        if not (rid or desc or text):
            continue
        if "systemui" in rid or "inputmethod" in rid:
            continue
        if pkg and pkg_filter not in pkg and "android" not in pkg:
            continue
        parts = []
        if rid:
            parts.append("id=" + rid.replace(PKG + ":", "TT:"))
        if desc:
            parts.append("desc=" + repr(desc[:100]))
        if text:
            parts.append("text=" + repr(text[:100]))
        if click == "true":
            parts.append("CLICK")
        print(" | ".join(parts) + "  " + bounds)
        count += 1
    print(f"--- {count} elements ---")

# Step 1: Press back to clear keyboard/search, go home
print("Step 1: Going back to TikTok home...")
d.press("back")
time.sleep(1)
d.press("back")
time.sleep(1)
d.press("back")
time.sleep(1)

# Re-launch TikTok clean
d.app_stop(PKG)
time.sleep(1)
d.app_start(PKG)
time.sleep(8)

# Dismiss popups
for txt in ["Skip", "Not now", "Allow", "While using the app", "OK", "Got it",
            "Close", "I agree", "Accept all", "Continue", "CONTINUE"]:
    btn = d(text=txt)
    if btn.exists(timeout=0.3):
        btn.click()
        time.sleep(0.5)

current = d.app_current()
print(f"   Current: {current.get('package')}")

# Step 2: Click creator name on current video
print("\nStep 2: Click creator name/avatar on video...")
time.sleep(2)

# The creator's name is at id=TT:title, and avatar at id=TT:zkr
# From dump: id=TT:id/title | text='Pppp', id=TT:id/zkr | desc='Pppp profile'
creator_name = d(resourceId=PKG + ":id/title")
if creator_name.exists(timeout=3):
    name = creator_name.get_text()
    print(f"   Creator: {name}")
    creator_name.click()
    time.sleep(4)

    dump_filtered("CREATOR PROFILE PAGE")
else:
    print("   Creator name not found, trying avatar...")
    avatar = d(resourceId=PKG + ":id/zkr")
    if avatar.exists(timeout=3):
        avatar.click()
        time.sleep(4)
        dump_filtered("CREATOR PROFILE PAGE")
    else:
        print("   Neither name nor avatar found!")
        dump_filtered("CURRENT SCREEN")
        sys.exit(1)

# Step 3: Try to find and click Followers
print("\nStep 3: Looking for Followers count/link...")
# Try various selectors
found = False
for sel in [
    {"textContains": "Followers"},
    {"textContains": "followers"},
    {"descriptionContains": "Followers"},
    {"descriptionContains": "followers"},
]:
    el = d(**sel)
    if el.exists(timeout=2):
        txt = el.get_text() or el.info.get("contentDescription", "")
        print(f"   Found: {txt}")
        el.click()
        time.sleep(3)
        found = True
        break

if found:
    dump_filtered("FOLLOWERS LIST")

    # Step 4: Click first follower to see their profile
    print("\nStep 4: Click a follower to see their full profile...")
    # Look for clickable rows/items below the tabs
    time.sleep(1)

    # In follower list, each user row should have avatar + name + Follow button
    follow_btns = d(text="Follow")
    if follow_btns.exists(timeout=2):
        print(f"   Found {follow_btns.count} Follow buttons")
        # Click the area to the LEFT of the first Follow button (the username)
        first_btn = follow_btns[0]
        btn_bounds = first_btn.info.get("bounds", {})
        # Click on the row but not the button - click name area
        click_y = (btn_bounds.get("top", 400) + btn_bounds.get("bottom", 450)) // 2
        d.click(200, click_y)  # click left side of that row
        time.sleep(3)

        dump_filtered("FOLLOWER INDIVIDUAL PROFILE")
    else:
        print("   No Follow buttons found in list")
else:
    print("   Could not find Followers link!")

print("\n=== Done ===")
