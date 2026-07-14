"""Final warmup test: 2 minutes to verify everything works."""
import sys, logging
sys.path.insert(0, ".")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from src.device_control.device_manager import get_device_manager
from src.app_automation.tiktok import TikTokAutomation

DID = "8D7DWWUKQGJRNN79"
dm = get_device_manager("config/devices.yaml")
tt = TikTokAutomation(device_manager=dm)
tt.set_current_device(DID)

print("Running 2-min warmup test...")
stats = tt.warmup_session(device_id=DID, duration_minutes=2, like_probability=0.30)
print("=" * 40)
print(f"Watched: {stats['watched']} videos")
print(f"Liked:   {stats['liked']} videos")
print(f"Duration: {stats['duration_sec']}s")
print("=" * 40)
