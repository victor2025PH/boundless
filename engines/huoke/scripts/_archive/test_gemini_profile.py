"""Navigate to a real TikTok profile, screenshot it, send to Gemini for analysis."""
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
if not client:
    print("No free vision client")
    sys.exit(1)

print(f"Using: {client.config.provider} / {client.config.vision_model}")

d = u2.connect(DEVICE)
w, h = d.window_size()

# Start TikTok fresh
d.app_start("com.zhiliaoapp.musically")
time.sleep(4)

# Go to home feed first
home = d(text="Home") or d(text="首页")
if home.exists(timeout=2):
    home.click()
    time.sleep(2)

# Swipe through a few videos to find one with a creator
print("Browsing feed to find a profile...")
for i in range(3):
    d.swipe(w // 2, int(h * 0.7), w // 2, int(h * 0.3), duration=0.3)
    time.sleep(2)

# Click the creator's avatar on the right side of the video
# In TikTok, the avatar is on the right side, roughly 90% from left, 30-40% from top
print("Clicking creator avatar...")
d.click(int(w * 0.93), int(h * 0.32))
time.sleep(3)

# Check if we're on a profile page
screenshot = d.screenshot()
screenshot.save("data/gemini_test_profile.png")
print("Screenshot saved to data/gemini_test_profile.png")

# Convert to bytes for the target_filter function
buf = BytesIO()
screenshot.save(buf, format="PNG")
png_bytes = buf.getvalue()
print(f"Screenshot: {len(png_bytes) // 1024} KB")

# Test 1: Direct Gemini call
img_b64 = base64.b64encode(png_bytes).decode("ascii")
prompt = """Look at this TikTok profile screenshot carefully. Based on ALL visual clues:
- Profile photo (face, hair, beard, makeup, accessories)
- Display name and username
- Bio text and language
- Video thumbnails visible
- Overall profile aesthetic

Answer in this exact JSON format only:
{"gender": "male" or "female" or "unknown", "age_range": "under25" or "25-35" or "35-50" or "over50" or "unknown", "nationality_clue": "any nationality/language hints", "confidence": 0.0-1.0}

Output ONLY the JSON."""

print("\n--- Direct Gemini call ---")
resp = client.chat_vision(prompt, img_b64, max_tokens=1024)
print(f"Response: {resp}")

# Test 2: Through analyze_profile_screenshot (the actual function used in our flow)
print("\n--- Through analyze_profile_screenshot ---")
result = analyze_profile_screenshot(png_bytes, TARGET, llm_client=client)
print(f"is_match: {result.is_match}")
print(f"score:    {result.score}")
print(f"reasons:  {result.reasons}")
print(f"disqualify: {result.disqualify}")
print(f"needs_ai: {result.needs_ai}")

d.press("back")
print("\nDone!")
