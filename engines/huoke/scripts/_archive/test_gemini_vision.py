"""Test Gemini vision: analyze a TikTok profile screenshot."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.ai.llm_client import get_free_vision_client

client = get_free_vision_client()
if client is None:
    print("No free vision client")
    sys.exit(1)

print(f"Using: {client.config.provider} / {client.config.vision_model}")

prompt = """Analyze this TikTok user profile screenshot. Answer ONLY in this exact JSON format:
{"gender": "male" or "female" or "unknown", "age_range": "under25" or "25-35" or "35-50" or "over50" or "unknown", "confidence": 0.0-1.0}

Clues to look for:
- Profile photo: face shape, hair, beard/mustache, makeup
- Display name style
- Bio text content and language
- Overall aesthetic of the profile

Be concise. Output ONLY the JSON, nothing else."""

# Use a connected device to get a real screenshot
try:
    import uiautomator2 as u2
    d = u2.connect("8D7DWWUKQGJRNN79")
    print("Taking screenshot from device...")
    screenshot = d.screenshot()

    from io import BytesIO
    import base64
    buf = BytesIO()
    screenshot.save(buf, format="PNG")
    img_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    print(f"Screenshot size: {len(img_b64) // 1024} KB (base64)")
    print("Sending to Gemini...")

    response = client.chat_vision(prompt, img_b64, max_tokens=1024)
    print(f"\nGemini response:\n{response}")

except ImportError:
    print("uiautomator2 not installed, skipping device test")
except Exception as e:
    print(f"Device error: {e}")
    print("Testing with a simple text query instead...")
    resp = client.chat("Say 'hello' in Italian", max_tokens=20)
    print(f"Text response: {resp}")
