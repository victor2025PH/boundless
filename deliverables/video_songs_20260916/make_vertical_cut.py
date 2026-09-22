#!/usr/bin/env python3
"""横屏成片 → 9:16 竖屏切条（不经 hub 口型 MV，适合先投放）。

  python make_vertical_cut.py R1 --take 455
  python make_vertical_cut.py R1 --take 455 --hook "老客回访·开口就对味"

时间线：0~2s 钩子卡 + 2~末 成片中心裁切（1080×1920）叠角标。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
BG = ROOT.parent / "huanyan-brand-bg" / "cosmos-tall.jpg"
FONT_B, FONT_R = "C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc"
W, H = 1080, 1920


def run(cmd):
    print("  $", " ".join(str(c) for c in cmd)[:200])
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def hook_card(text: str, dst: Path) -> None:
    im = Image.open(BG).convert("RGB")
    s = max(W / im.width, H / im.height)
    im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
    x0, y0 = (im.width - W) // 2, (im.height - H) // 2
    im = Image.blend(im.crop((x0, y0, x0 + W, y0 + H)), Image.new("RGB", (W, H), (5, 6, 15)), 0.4)
    d = ImageDraw.Draw(im)
    d.rectangle((60, 720, W - 60, 1120), fill=(10, 16, 36, 200) if False else (10, 16, 36))
    lines = text.replace("|", "\n").split("\n")
    y = 820
    for line in lines:
        box = d.textbbox((0, 0), line, font=ImageFont.truetype(FONT_B, 64))
        d.text(((W - (box[2] - box[0])) // 2, y), line, font=ImageFont.truetype(FONT_B, 64), fill=(255, 220, 120))
        y += 90
    d.text((W // 2 - 160, 1180), "客户关系养成 · 智聊", font=ImageFont.truetype(FONT_R, 36), fill=(200, 210, 230))
    im.save(dst)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("episode")
    ap.add_argument("--take", type=int, required=True)
    ap.add_argument("--hook", default="")
    a = ap.parse_args()
    src = OUT / a.episode / f"{a.episode}_h{a.take}_episode.mp4"
    if not src.exists():
        raise SystemExit(f"[FAIL] 缺 {src}")
    if src.read_bytes()[4:8] != b"ftyp":
        raise SystemExit("[FAIL] 源非 ftyp")
    info = json.loads(run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(src)]).stdout)
    dur = float(info["format"]["duration"])
    hook = a.hook or {
        "R1": "老客回访|开口就对味",
        "R2": "熟人声|包裹到了",
        "R3": "店长口吻|先试聊再绑",
        "C1": "凌晨询盘|它在回复",
    }.get(a.episode, a.episode)
    cards = OUT / a.episode / "cards"
    cards.mkdir(exist_ok=True)
    hook_png = cards / "v_hook.png"
    hook_card(hook, hook_png)
    out = OUT / a.episode / f"{a.episode}_h{a.take}_vertical_cut.mp4"
    # 中心裁：1920x1080 → 裁 608x1080 再 scale 到 1080x1920 会变形；改为 pad 黑边 + 缩放保比例后居中
    # 更好：scale=1080:608, pad 到 1080x1920（画面在中部），钩子盖前 2s
    vf = (
        f"[0:v]scale=1080:608:force_original_aspect_ratio=decrease,"
        f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=0x0a1020,setsar=1,fps=30[base];"
        f"[1:v]scale=1080:1920,format=rgba[hk];"
        f"[base][hk]overlay=0:0:enable='lt(t,2)'[v]"
    )
    r = run([
        "ffmpeg", "-y", "-i", str(src), "-loop", "1", "-t", "2", "-i", str(hook_png),
        "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-t", f"{dur:.2f}", "-movflags", "+faststart", str(out),
    ])
    if r.returncode != 0:
        raise SystemExit(r.stderr[-800:])
    if out.read_bytes()[4:8] != b"ftyp":
        raise SystemExit("[FAIL] 竖屏非 ftyp")
    print(f"[OK] {out.name} {out.stat().st_size // 1024}KB")
    run(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", str(out), "-frames:v", "1", str(OUT / a.episode / f"{out.stem}_t1.png")])
    run(["ffmpeg", "-y", "-v", "error", "-ss", "8", "-i", str(out), "-frames:v", "1", str(OUT / a.episode / f"{out.stem}_t8.png")])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
