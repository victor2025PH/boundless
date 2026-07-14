# -*- coding: utf-8 -*-
"""品牌化封面:各片最具代表性一帧 + 底部渐暗带 + 大标题/副题 + 品牌角标。
直接覆盖 web117 的 <key>-poster.jpg(publish 之后跑)。
"""
import os
import subprocess
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compose_lib as L

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
WEB = r"C:\web117\public\videos\showcase"
GRAD = os.path.join(L.TMP, "_poster_grad.png")


def _make_gradient():
    """真 alpha 渐变(1920x1080 RGBA):y=640 起黑色从 0 平滑升到 0.96,压住烙在
    视频里的旧字幕并给标题留满对比度;顶部完全透明。"""
    h, w = 1080, 1920
    a = np.zeros((h, w), np.float32)
    y = np.arange(h, dtype=np.float32)
    ramp = np.clip((y - 640) / (880 - 640), 0, 1)          # 640→880 缓升
    alpha = ramp * 0.96
    a[:] = alpha[:, None]
    img = np.zeros((h, w, 4), np.uint8)
    img[:, :, 3] = (a * 255).astype(np.uint8)              # BGR=0(黑) + alpha
    cv2.imwrite(GRAD, img)

# key: (源视频, 取帧秒, 标题, 副题, 强调色)
POSTERS = {
    "voice":    (os.path.join(OUT, "voice_demo_v3.mp4"), 22.0,
                 "声音克隆 · 情感 TTS", "30 秒克隆 · 多语种带情感", L.CYAN),
    "faceswap": (os.path.join(OUT, "faceswap_demo_v3.mp4"), 21.5,
                 "视频换脸 · 前后对比", "同一段视频 · 四张脸逐帧同步", L.MAGENTA),
    "interp":   (os.path.join(OUT, "interp_demo_v3.mp4"), 5.8,
                 "克隆音实时同传", "中文进 · 英文出 · 还是你的声音", L.GREEN),
    "studio":   (os.path.join(OUT, "studio_demo_v3.mp4"), 4.0,
                 "换发型 · 定妆 · 试衣", "真人视频 · 一键换整套形象", L.CYAN),
    "voice-en":    (os.path.join(OUT, "voice_demo_v3_en.mp4"), 22.0,
                    "Voice Cloning · Emotional TTS", "Clone from 30s · Multilingual & emotional", L.CYAN),
    "faceswap-en": (os.path.join(OUT, "faceswap_demo_v3_en.mp4"), 21.5,
                    "Video Face Swap · Before / After", "One video · Four faces frame-locked", L.MAGENTA),
    "interp-en":   (os.path.join(OUT, "interp_demo_v3_en.mp4"), 5.8,
                    "Live Interpreting · Your Own Voice", "Mandarin in · English out", L.GREEN),
    "studio-en":   (os.path.join(OUT, "studio_demo_v3_en.mp4"), 4.0,
                    "Hair · Makeup · Try-on", "Real video · One-click full look", L.CYAN),
    "live":     (os.path.join(OUT, "live_demo_v3.mp4"), 6.0,
                 "直播实时换脸换声", "摄像头进 · 换脸变声出 · 直播级延迟", L.CYAN),
    "live-en":  (os.path.join(OUT, "live_demo_v3_en.mp4"), 6.0,
                 "Live Face & Voice Swap", "Camera in · Swapped out · Stream-ready", L.CYAN),
}


def make(key, src, t, title, sub, accent):
    out = os.path.join(WEB, f"{key}-poster.jpg")
    txt = (f"drawbox=x=80:y=905:w=12:h=132:color={accent}:t=fill,"
           + L._dt(title, "130", "916", 62, L.WHITE, L.FONT_BD) + ","
           + L._dt(sub, "134", "1000", 34, accent, L.FONT))
    fc = f"[0:v][1:v]overlay=0:0,{txt}[v]"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(t), "-i", src,
                    "-i", GRAD, "-filter_complex", fc, "-map", "[v]",
                    "-frames:v", "1", "-q:v", "3", out], check=True)
    kb = os.path.getsize(out) // 1024
    print(f"{key:10} {kb}KB -> {out}")


if __name__ == "__main__":
    _make_gradient()
    for k, (src, t, title, sub, accent) in POSTERS.items():
        make(k, src, t, title, sub, accent)
    print("done")
