"""Short warmup test: 2-minute session to verify the full warmup loop."""
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

print("Starting 2-minute warmup test...")
stats = tt.warmup_session(
    device_id=DID,
    duration_minutes=2,
    like_probability=0.30,
)
print(f"\nResult: {stats}")
print(f"  Watched: {stats['watched']} videos")
print(f"  Liked:   {stats['liked']} videos")
print(f"  Duration: {stats['duration_sec']}s")
