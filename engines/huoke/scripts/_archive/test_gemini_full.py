"""Full test: Navigate to a TikTok profile, screenshot, send to Gemini."""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uiautomator2 as u2
from io import BytesIO
import base64

from src.ai.llm_client import get_free_vision_client

DEVICE = "8D7DWWUKQGJRNN79"

client = get_free_vision_client()
if not client:
    print("No free vision client")
    sys.exit(1)

print(f"Using: {client.config.provider} / {client.config.vision_model}")

d = u2.connect(DEVICE)
print(f"Device: {d.info.get('productName', DEVICE)}")

# Launch TikTok and go to profile tab
d.app_start("com.zhiliaoapp.musically")
time.sleep(3)

# Save current screen
screenshot = d.screenshot()
screenshot.save("data/screen_before.png")
print("Saved current screen to data/screen_before.png")

# Try to go to "me" / profile from main feed
# Swipe left on feed to see a random user's video, then click their avatar
print("Looking for a user profile to analyze...")

# Click on a video creator's avatar (usually on the right side)
# First let's check what's on screen
xml = d.dump_hierarchy()
has_profile = "com.zhiliaoapp.musically:id/profile" in xml or "个人资料" in xml

# Try clicking a user avatar on feed
avatar = d(resourceId="com.zhiliaoapp.musically:id/avatar")
if avatar.exists(timeout=3):
    avatar.click()
    time.sleep(2)
    print("Clicked avatar, now on profile")
else:
    # Click on the profile picture overlay on video
    # Usually the round avatar on right side of feed
    right_panel = d(className="android.widget.ImageView", 
                    resourceId="com.zhiliaoapp.musically:id/biz_profile_avatar")
    if right_panel.exists(timeout=2):
        right_panel.click()
        time.sleep(2)
        print("Clicked profile avatar overlay")
    else:
        print("Looking for any clickable avatar...")
        # Click approximate location of video creator avatar (right side)
        w, h = d.window_size()
        d.click(int(w * 0.93), int(h * 0.35))
        time.sleep(2)

# Take screenshot of current screen (should be a profile now)
screenshot2 = d.screenshot()
screenshot2.save("data/screen_profile.png")
print("Saved profile screen to data/screen_profile.png")

# Send to Gemini
buf = BytesIO()
screenshot2.save(buf, format="PNG")
img_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

prompt = """Look at this TikTok profile screenshot carefully. Based on ALL visual clues:
- Profile photo (face, hair, beard, makeup, accessories)
- Display name and username
- Bio text and language
- Video thumbnails visible
- Overall profile aesthetic

Answer in this exact JSON format only:
{"gender": "male" or "female" or "unknown", "age_range": "under25" or "25-35" or "35-50" or "over50" or "unknown", "nationality_clue": "any nationality/language hints", "confidence": 0.0-1.0}

Output ONLY the JSON."""

print(f"\nSending {len(img_b64)//1024}KB screenshot to Gemini...")
response = client.chat_vision(prompt, img_b64, max_tokens=1024)
print(f"\nGemini analysis:\n{response}")

# Go back
d.press("back")
