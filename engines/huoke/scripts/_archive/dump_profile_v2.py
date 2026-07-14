"""
Robust profile/followers dump: use adb directly to launch, then navigate.
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

def dump_ui(label):
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
        if (rid or desc or text) and (PKG in pkg or "systemui" not in rid):
            parts = []
            if rid:
                parts.append("id=" + rid)
            if desc:
                parts.append("desc=" + repr(desc[:100]))
            if text:
                parts.append("text=" + repr(text[:100]))
            if click == "true":
                parts.append("CLICK")
            line = " | ".join(parts) + "  " + bounds
            # Filter out system UI noise
            if "systemui" not in line and "miui" not in line.lower():
                print(line)
                count += 1
    print(f"--- Total: {count} elements ---")

# Step 1: Force-launch TikTok
print("1. Force-launching TikTok...")
d.app_stop(PKG)
time.sleep(1)
d.app_start(PKG)
time.sleep(8)

# Dismiss any popups
for txt in ["Skip", "Not now", "Allow", "While using the app", "OK", "Got it",
            "I agree", "Accept all", "Continue", "CONTINUE", "Close"]:
    btn = d(text=txt)
    if btn.exists(timeout=0.5):
        btn.click()
        time.sleep(0.5)

# Check current app
current = d.app_current()
print(f"   Current app: {current}")

# Step 2: Verify we're on TikTok
if current.get("package") != PKG:
    print("   NOT on TikTok! Trying again...")
    d.app_start(PKG)
    time.sleep(5)
    current = d.app_current()
    print(f"   Current app: {current}")

dump_ui("TIKTOK HOME SCREEN")

# Step 3: Click Search icon
print("\n2. Clicking Search...")
search = d(description="Search")
if not search.exists(timeout=3):
    search = d(resourceId=PKG + ":id/izy")
if search.exists(timeout=3):
    search.click()
    time.sleep(2)
    print("   Search opened")
else:
    print("   Search NOT found")

# Step 4: Search for a user
print("3. Searching for a popular user...")
edit = d(className="android.widget.EditText")
if edit.exists(timeout=3):
    edit.set_text("charlidamelio")
    time.sleep(0.5)
    d.press("enter")
    time.sleep(3)
    print("   Search submitted")

    # Click Users/Accounts tab
    for tab_text in ["Users", "Accounts", "People"]:
        tab = d(text=tab_text)
        if tab.exists(timeout=2):
            tab.click()
            time.sleep(2)
            print(f"   Clicked '{tab_text}' tab")
            break

    dump_ui("SEARCH RESULTS - USERS")

    # Click first result
    print("4. Clicking first user result...")
    # Try clicking the first row that has user info
    first = d(textContains="charli")
    if not first.exists(timeout=2):
        first = d(textContains="Charli")
    if first.exists(timeout=2):
        first.click()
        time.sleep(3)
        print("   Clicked user")

        dump_ui("USER PROFILE PAGE")

        # Step 5: Click Followers
        print("5. Looking for Followers...")
        fol = d(textContains="Followers")
        if fol.exists(timeout=3):
            print(f"   Found Followers text: {fol.get_text()}")
            fol.click()
            time.sleep(3)

            dump_ui("FOLLOWERS LIST")
        else:
            # Try desc
            fol2 = d(descriptionContains="Followers")
            if fol2.exists(timeout=2):
                fol2.click()
                time.sleep(3)
                dump_ui("FOLLOWERS LIST")
            else:
                print("   Followers not found")
    else:
        print("   Could not find user in results")
else:
    print("   Search input not found")

print("\n=== Done ===")
