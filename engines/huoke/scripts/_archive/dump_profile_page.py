"""
Dump TikTok profile page and follower list UI hierarchy.

Flow: launch TikTok → search for a popular user → open profile → dump UI
"""
import sys, time, logging
sys.path.insert(0, ".")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from src.device_control.device_manager import get_device_manager
from src.app_automation.tiktok import TikTokAutomation, TT
import xml.etree.ElementTree as ET

DID = "8D7DWWUKQGJRNN79"
dm = get_device_manager("config/devices.yaml")
tt = TikTokAutomation(device_manager=dm)
tt.set_current_device(DID)
d = dm.get_u2(DID)

# Step 1: Launch TikTok
print("=" * 60)
print("1. Launching TikTok...")
tt.launch(DID)
time.sleep(2)

# Step 2: Go to For You, click a creator's avatar to open profile
print("2. Going to For You, clicking creator avatar...")
tt.go_for_you(d)
time.sleep(3)

# Click the creator avatar to go to their profile
clicked = tt._click_multi(d, TT.CREATOR_AVATAR, timeout=3)
if not clicked:
    # Try clicking the creator name text area
    clicked = tt._click_multi(d, [TT.CREATOR_NAME], timeout=3)
print(f"   Clicked creator: {clicked}")
time.sleep(3)

# Step 3: Dump profile page UI
print("3. Dumping profile page UI...")
xml_str = d.dump_hierarchy()
root = ET.fromstring(xml_str)

print("\n--- PROFILE PAGE ELEMENTS ---")
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
            parts.append("desc=" + repr(desc[:100]))
        if text:
            parts.append("text=" + repr(text[:100]))
        if click == "true":
            parts.append("CLICK")
        print(" | ".join(parts) + "  " + bounds)

# Step 4: Try to click Followers to open follower list
print("\n4. Looking for Followers link...")
time.sleep(1)

# Try different approaches to find followers count/link
for sel_text in ["Followers", "followers"]:
    el = d(textContains=sel_text)
    if el.exists(timeout=2):
        print(f"   Found: textContains='{sel_text}'")
        el.click()
        time.sleep(3)
        break
else:
    # Try content-desc
    el = d(descriptionContains="Followers")
    if el.exists(timeout=2):
        print("   Found: descContains='Followers'")
        el.click()
        time.sleep(3)
    else:
        print("   Could not find Followers link!")
        # Dump again to see what's there
        sys.exit(0)

# Step 5: Dump follower list UI
print("5. Dumping follower list UI...")
xml_str2 = d.dump_hierarchy()
root2 = ET.fromstring(xml_str2)

print("\n--- FOLLOWER LIST ELEMENTS ---")
for el in root2.iter():
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
            parts.append("desc=" + repr(desc[:100]))
        if text:
            parts.append("text=" + repr(text[:100]))
        if click == "true":
            parts.append("CLICK")
        print(" | ".join(parts) + "  " + bounds)

# Step 6: Click into a follower's profile to see location/bio
print("\n6. Clicking first follower to inspect profile...")
# Look for the first user row in the list
user_rows = d(className="android.view.ViewGroup", clickable=True)
if user_rows.exists(timeout=2):
    for i in range(user_rows.count):
        row = user_rows[i]
        row_info = row.info
        b = row_info.get("bounds", {})
        # Skip navigation/header elements (usually at top)
        if b.get("top", 0) > 300:
            row.click()
            time.sleep(3)
            print("   Clicked a follower row, dumping their profile...")

            xml_str3 = d.dump_hierarchy()
            root3 = ET.fromstring(xml_str3)
            print("\n--- FOLLOWER PROFILE ---")
            for el3 in root3.iter():
                rid3 = el3.get("resource-id", "")
                desc3 = el3.get("content-desc", "")
                text3 = el3.get("text", "")
                if rid3 or desc3 or text3:
                    parts = []
                    if rid3:
                        parts.append("id=" + rid3)
                    if desc3:
                        parts.append("desc=" + repr(desc3[:100]))
                    if text3:
                        parts.append("text=" + repr(text3[:100]))
                    print(" | ".join(parts))
            break

print("\n=== Done ===")
