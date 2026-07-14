"""Quick test: Gemini free vision client connectivity."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

print(f"GEMINI_API_KEY = {os.environ.get('GEMINI_API_KEY', '')[:10]}...")

from src.ai.llm_client import get_free_vision_client

client = get_free_vision_client()
if client is None:
    print("ERROR: No free vision client found")
    sys.exit(1)

print(f"Provider: {client.config.provider}")
print(f"Model:    {client.config.vision_model}")
print(f"URL:      {client.config.base_url}")

print("\nTesting connection...")
ok, msg = client.test_connection()
print(f"Result: {'OK' if ok else 'FAIL'} - {msg}")

if ok:
    print("\nGemini ready for AI screening!")
