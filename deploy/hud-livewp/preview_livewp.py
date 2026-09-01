"""无界星门实况壁纸——本地预览截图（playwright headless chromium）。

对 staging 目录写入伪造 data.js（正常态 + svcdown 告警态），分别截图，
供上线前人眼验收。不碰任何生产文件。

用法:
    python deploy/hud-livewp/preview_livewp.py --stage tmp/livewp_stage/shengbei
"""
import argparse
import json
import time
from pathlib import Path


def write_data(stage: Path, state: str, dead_ports=None) -> None:
    now = int(time.time())
    hist_m = {
        "zhongshu": {"s": "o", "l": 0.4, "vu": 21360, "vt": 32607, "u": 9},
        "yunsheng": {"s": "o", "l": 0.6, "vu": 29100, "vt": 32607, "u": 71},
        "shengbei": {"s": "o", "l": 1.2, "vu": -1, "vt": -1, "u": -1},
        "lianbei": {"s": "o", "l": 2.4, "vu": 6700, "vt": 12282, "u": 18},
        "tingxie": {"s": "o", "l": 0.4, "vu": -1, "vt": -1, "u": -1},
        "kouxing": {"s": "o", "l": 0.3, "vu": -1, "vt": -1, "u": -1},
    }
    payload = {
        "self": {
            "id": "shengbei",
            "state": state,
            "since": "20:41:00",
            "checked": time.strftime("%Y-%m-%d %H:%M:%S"),
            "dead_ports": dead_ports or [],
            "vram_used_mb": 7160,
            "vram_total_mb": 12288,
            "gpu_util": 23,
            "wrote_epoch": now,
        },
        "hist": [{"ts": now - 38, "v": "safe", "m": hist_m}],
    }
    js = "window.HUD_DATA=" + json.dumps(payload, ensure_ascii=False) + ";"
    (stage / "data.js").write_text(js, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, help="staging dir containing index.html")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    args = ap.parse_args()
    stage = Path(args.stage).resolve()
    index = stage / "index.html"
    if not index.exists():
        raise SystemExit(f"index.html not found in {stage}")

    from playwright.sync_api import sync_playwright

    url = index.as_uri()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": args.width, "height": args.height})

        write_data(stage, "normal")
        page.goto(url)
        page.wait_for_timeout(4500)
        out1 = stage / "shot_normal_t4.png"
        page.screenshot(path=str(out1))
        page.wait_for_timeout(4000)
        out2 = stage / "shot_normal_t8.png"
        page.screenshot(path=str(out2))

        write_data(stage, "svcdown", dead_ports=[18799])
        page.reload()
        page.wait_for_timeout(4500)
        out3 = stage / "shot_alert.png"
        page.screenshot(path=str(out3))

        browser.close()
    for p in (out1, out2, out3):
        print("SHOT", p)


if __name__ == "__main__":
    main()
