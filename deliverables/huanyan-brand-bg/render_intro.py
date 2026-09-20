# -*- coding: utf-8 -*-
"""
开场首屏（IntroCover 复刻）素材渲染器
  python render_intro.py still16   -> boundless-intro-16x9-4k.png  (3840x2160)
  python render_intro.py still9    -> boundless-intro-9x16-4k.png  (2160x3840)
  python render_intro.py frames    -> frames_intro/f_0000.jpg ... (12s x 30fps)
  python render_intro.py probe     -> 3 帧测速
"""
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
PAGE = (ROOT / "intro.html").as_uri()

FPS = 30
DURATION = 12
N_FRAMES = FPS * DURATION
STILL_T = 2.5


def open_page(pw, viewport, url):
    browser = pw.chromium.launch(
        headless=True,
        args=[
            "--force-color-profile=srgb",
            "--disable-lcd-text",
            "--hide-scrollbars",
            "--force-device-scale-factor=2",
        ],
    )
    ctx = browser.new_context(
        viewport=viewport,
        device_scale_factor=2,
        reduced_motion="no-preference",
    )
    page = ctx.new_page()
    page.goto(url)
    page.wait_for_function("window.__ready === true", timeout=30000)
    return browser, page


def still(pw, aspect: str, out: str):
    vp = {"width": 1920, "height": 1080} if aspect == "16x9" else {"width": 1080, "height": 1920}
    url = f"{PAGE}?still=1" + ("&aspect=9x16" if aspect == "9x16" else "")
    browser, page = open_page(pw, vp, url)
    page.evaluate(f"window.__seek({STILL_T})")
    page.wait_for_timeout(250)
    page.screenshot(path=str(ROOT / out), type="png")
    browser.close()
    print(f"[ok] {out}")


def frames(pw, n_frames: int):
    vp = {"width": 1920, "height": 1080}
    browser, page = open_page(pw, vp, PAGE)
    outdir = ROOT / "frames_intro"
    outdir.mkdir(exist_ok=True)
    t_start = time.time()
    for i in range(n_frames):
        t = i / FPS
        page.evaluate(f"window.__seek({t})")
        page.screenshot(path=str(outdir / f"f_{i:04d}.jpg"), type="jpeg", quality=94)
        if i % 30 == 0 or i == n_frames - 1:
            el = time.time() - t_start
            eta = el / (i + 1) * (n_frames - i - 1)
            print(f"[frame] {i + 1}/{n_frames}  elapsed={el:.0f}s eta={eta:.0f}s", flush=True)
    browser.close()
    print("[ok] frames done")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "still16"
    with sync_playwright() as pw:
        if mode == "still16":
            still(pw, "16x9", "boundless-intro-16x9-4k.png")
        elif mode == "still9":
            still(pw, "9x16", "boundless-intro-9x16-4k.png")
        elif mode == "frames":
            frames(pw, N_FRAMES)
        elif mode == "probe":
            frames(pw, 3)
        else:
            raise SystemExit(f"unknown mode: {mode}")


if __name__ == "__main__":
    main()
