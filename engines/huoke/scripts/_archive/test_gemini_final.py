"""Navigate to TikTok profile, then test Gemini analysis."""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uiautomator2 as u2
from io import BytesIO
import base64

from src.ai.llm_client import get_free_vision_client
from src.app_automation.target_filter import TargetProfile, analyze_profile_screenshot

DEVICE = "8D7DWWUKQGJRNN79"
TIKTOK_PKG = "com.ss.android.ugc.trill"
TARGET = TargetProfile(country="italy", gender="male", min_age=30, min_score=0.4)

client = get_free_vision_client()
d = u2.connect(DEVICE)
w, h = d.window_size()

# Force start TikTok
d.app_start(TIKTOK_PKG, stop=True)
time.sleep(6)

print(f"Current app: {d.app_current()['package']}")

# Dismiss popups
for txt in ["Allow", "OK", "Got it", "Maybe later", "Not now", "Decline"]:
    btn = d(text=txt)
    if btn.exists(timeout=0.5):
        btn.click()
        time.sleep(0.5)

# Go to "Profile" tab (bottom right)
print("Step 1: Go to Profile tab...")
profile_tab = d(text="Profile")
if not profile_tab.exists(timeout=2):
    profile_tab = d(descriptionContains="Profile")
if profile_tab.exists(timeout=2):
    profile_tab.click()
    time.sleep(3)
    print("  Clicked Profile tab")
else:
    # Bottom-right tab
    d.click(int(w * 0.92), int(h * 0.97))
    time.sleep(3)
    print("  Clicked bottom-right area")

# Check if on profile
xml = d.dump_hierarchy()
on_profile = "Following" in xml or "Followers" in xml
print(f"Step 2: On profile page: {on_profile}")

# Take screenshot
screenshot = d.screenshot()
screenshot.save("data/gemini_profile_test.png")
buf = BytesIO()
screenshot.save(buf, format="PNG")
png_bytes = buf.getvalue()
print(f"Step 3: Screenshot {len(png_bytes) // 1024} KB")

# Always send to Gemini regardless
img_b64 = base64.b64encode(png_bytes).decode("ascii")

print("\n--- Gemini direct analysis ---")
resp = client.chat_vision(
    """Analyze this TikTok profile page screenshot. Based on profile photo, display name, bio, video thumbnails, determine:
{"gender": "male" or "female" or "unknown", "age_range": "under25" or "25-35" or "35-50" or "over50" or "unknown", "confidence": 0.0-1.0}
Output ONLY the JSON, no other text.""",
    img_b64, max_tokens=1024
)
print(f"  Response: {resp}")

print("\n--- analyze_profile_screenshot (full pipeline) ---")
result = analyze_profile_screenshot(png_bytes, TARGET, llm_client=client)
print(f"  match={result.is_match}  score={result.score}")
print(f"  reasons={result.reasons}")
print(f"  disqualify={result.disqualify}")
print(f"  needs_ai={result.needs_ai}")

# Now browse to another user's profile from feed
print("\n--- Testing on video creator profile ---")
# Go to home feed
home = d(text="Home")
if home.exists(timeout=2):
    home.click()
    time.sleep(3)

# Swipe a few times
for i in range(3):
    d.swipe(w // 2, int(h * 0.75), w // 2, int(h * 0.25), duration=0.3)
    time.sleep(1.5)

# Click the creator's username at bottom of video
# In TikTok international, username is usually near bottom-left
d.click(int(w * 0.2), int(h * 0.82))
time.sleep(3)

xml2 = d.dump_hierarchy()
on_other = "Following" in xml2 or "Followers" in xml2
print(f"On creator profile: {on_other}")

if not on_other:
    # Try avatar on right side
    d.press("back")
    time.sleep(1)
    d.click(int(w * 0.93), int(h * 0.33))
    time.sleep(3)

screenshot2 = d.screenshot()
screenshot2.save("data/gemini_creator_test.png")
buf2 = BytesIO()
screenshot2.save(buf2, format="PNG")
png2 = buf2.getvalue()
print(f"Creator screenshot: {len(png2) // 1024} KB")

result2 = analyze_profile_screenshot(png2, TARGET, llm_client=client)
print(f"  match={result2.is_match}  score={result2.score}")
print(f"  reasons={result2.reasons}")
print(f"  disqualify={result2.disqualify}")

d.press("back")
print("\nAll tests complete!")
