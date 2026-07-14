"""Reliable profile navigation + Gemini vision test."""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uiautomator2 as u2
from io import BytesIO
import base64

from src.ai.llm_client import get_free_vision_client
from src.app_automation.target_filter import TargetProfile, analyze_profile_screenshot

DEVICE = "8D7DWWUKQGJRNN79"
TARGET = TargetProfile(country="italy", gender="male", min_age=30, min_score=0.4)

client = get_free_vision_client()
d = u2.connect(DEVICE)
w, h = d.window_size()

# Force restart TikTok
d.app_stop("com.zhiliaoapp.musically")
time.sleep(1)
d.app_start("com.zhiliaoapp.musically")
time.sleep(5)

# Check current state
xml = d.dump_hierarchy()

# Dismiss any popups
for dismiss_text in ["Allow", "OK", "Got it", "Maybe later", "Not now", "关闭", "允许"]:
    btn = d(text=dismiss_text)
    if btn.exists(timeout=0.5):
        btn.click()
        time.sleep(1)

# Go to feed
print("Step 1: Go to home feed")
d.click(int(w * 0.1), int(h * 0.95))  # Home tab (bottom left)
time.sleep(3)

# Swipe through videos
print("Step 2: Browse videos")
for i in range(5):
    d.swipe(w // 2, int(h * 0.75), w // 2, int(h * 0.25), duration=0.4)
    time.sleep(1.5)

# Save current feed screen
d.screenshot().save("data/test_feed.png")
print("  Saved feed screen")

# Try to find username text on the video
print("Step 3: Find and click a creator name")
# TikTok shows @username at the bottom of the video
username_el = d(resourceId="com.zhiliaoapp.musically:id/title")
if username_el.exists(timeout=2):
    username_el.click()
    print(f"  Clicked username element")
    time.sleep(3)
else:
    # Try clicking on the profile name area at bottom-left of video
    # or the avatar on the right side
    print("  No username element, trying avatar area...")

    # Right side: avatar is typically at y=30-35% of screen, x=90-95%
    d.click(int(w * 0.92), int(h * 0.33))
    time.sleep(3)

    # Check if landed on profile
    xml2 = d.dump_hierarchy()
    on_profile = ("Following" in xml2 and "Followers" in xml2) or \
                 ("关注" in xml2 and "粉丝" in xml2)

    if not on_profile:
        print("  Avatar click didn't work, trying username area...")
        d.press("back")
        time.sleep(1)
        # Bottom-left area where @username appears
        d.click(int(w * 0.25), int(h * 0.82))
        time.sleep(3)

# Verify we're on a profile
xml3 = d.dump_hierarchy()
on_profile = ("Following" in xml3 and "Followers" in xml3) or \
             ("关注" in xml3 and "粉丝" in xml3)
print(f"Step 4: On profile page? {on_profile}")

# Extract some text from the page for debugging
for node_text in ["Following", "Followers", "Likes", "关注", "粉丝", "获赞"]:
    el = d(text=node_text)
    if el.exists(timeout=0.3):
        print(f"  Found: '{node_text}'")

# Take and save screenshot
screenshot = d.screenshot()
screenshot.save("data/test_profile_final.png")
print(f"Step 5: Screenshot saved")

# Send to Gemini
buf = BytesIO()
screenshot.save(buf, format="PNG")
png_bytes = buf.getvalue()
img_b64 = base64.b64encode(png_bytes).decode("ascii")
print(f"  Size: {len(png_bytes) // 1024} KB")

print("\nStep 6: Gemini analysis...")
resp = client.chat_vision(
    """Analyze this screenshot. If it shows a TikTok user profile, determine:
1. Gender (male/female/unknown) based on profile photo, name, bio
2. Age range (under25/25-35/35-50/over50/unknown)
3. Any nationality or language clues

Answer ONLY as JSON:
{"is_profile": true/false, "gender": "...", "age_range": "...", "nationality_clue": "...", "confidence": 0.0-1.0}""",
    img_b64, max_tokens=1024
)
print(f"Gemini: {resp}")

# Also test through our scoring function
print("\nStep 7: analyze_profile_screenshot...")
result = analyze_profile_screenshot(png_bytes, TARGET, llm_client=client)
print(f"  match={result.is_match} score={result.score}")
print(f"  reasons={result.reasons}")
print(f"  disqualify={result.disqualify}")

d.press("back")
print("\nDone!")
