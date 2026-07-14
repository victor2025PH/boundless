"""Smoke test: launch TikTok → swipe 3 videos → like 1 → get creator info."""
import sys, time, logging
sys.path.insert(0, ".")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from src.device_control.device_manager import get_device_manager
from src.app_automation.tiktok import TikTokAutomation

DID = "8D7DWWUKQGJRNN79"

dm = get_device_manager("config/devices.yaml")
tt = TikTokAutomation(device_manager=dm)
tt.set_current_device(DID)

print("=" * 50)
print("1. Launching TikTok...")
ok = tt.launch(DID)
print(f"   Launch result: {ok}")
if not ok:
    print("   FAILED — exiting")
    sys.exit(1)

d = dm.get_u2(DID)
time.sleep(2)

print("2. Navigating to For You...")
tt.go_for_you(d)
time.sleep(3)

print("3. Browsing 3 videos...")
for i in range(3):
    creator = tt._get_creator_name(d)

    # Also try content-desc on avatar area for creator
    like_exists = tt._exists_multi(d, [
        {"descriptionContains": "Like video"},
        {"descriptionContains": "like"},
    ], timeout=2)
    follow_exists = tt._exists_multi(d, [
        {"descriptionContains": "Follow"},
    ], timeout=1)

    print(f"   Video #{i+1}: creator={creator!r}, like_btn={like_exists}, follow_btn={follow_exists}")

    if i == 1:
        print("   >> Double-tap to like...")
        tt._like_current_video(d)
        time.sleep(1)

    print("   >> Swipe next...")
    tt._swipe_next_video(d)
    time.sleep(3)

print("=" * 50)
print("Smoke test PASSED!")
