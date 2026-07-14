# -*- coding: utf-8 -*-
"""把 demo_record/out 的四条成片 web 优化(1080p/faststart/crf23) + 生成 poster,
拷到 web117 的 public/videos/showcase/，供 /order 页展示位使用。
用法: facefusion python demo_record/publish_showcase.py
"""
import os
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
WEB = r"C:\web117\public\videos\showcase"
os.makedirs(WEB, exist_ok=True)

# (源片, 目标 key, poster 取帧秒)
JOBS = [
    ("voice_demo_v3.mp4",       "voice",       22.0),  # v3:统一设计系统(片头/下三分之一/尾卡/BGM)
    ("faceswap_demo_v3.mp4",    "faceswap",     4.0),  # v3:统一设计+单一主体+变声
    ("interp_demo_v3.mp4",      "interp",       5.0),  # v3:会说话数字人+降噪+专业版式
    ("studio_demo_v3.mp4",      "studio",      11.0),  # v3:全动态(视频妆/试衣/微动发型)
    ("live_demo_v3.mp4",        "live",         6.0),  # v3:直播工作台(输出主画面+原始PiP+LIVE)
    ("voice_demo_v3_en.mp4",    "voice-en",    22.0),  # 英文站(pricing.ts srcEn)
    ("faceswap_demo_v3_en.mp4", "faceswap-en",  4.0),
    ("interp_demo_v3_en.mp4",   "interp-en",    5.0),
    ("studio_demo_v3_en.mp4",   "studio-en",   11.0),
    ("live_demo_v3_en.mp4",     "live-en",      6.0),
]
# 注:publish 出的 poster 是裸帧兜底;跑完再执行 gen_posters.py 覆盖为品牌化封面。


def run(cmd):
    subprocess.run(cmd, check=True)


for src_name, key, poster_t in JOBS:
    src = os.path.join(OUT, src_name)
    if not os.path.isfile(src):
        print("跳过(缺源):", src_name); continue
    dst = os.path.join(WEB, f"{key}.mp4")
    poster = os.path.join(WEB, f"{key}-poster.jpg")
    # web 优化:720p、crf25、slow、aac96k、faststart。
    # 实测跨境链路 ~400KB/s,1080p/6MB 点开要黑屏缓冲数秒像"点了没反应";
    # 展示卡渲染宽 ~400px,720p 无感知差异,体积减半换"点击即播"。
    run(["ffmpeg", "-y", "-v", "error", "-i", src,
         "-vf", "scale='min(1280,iw)':-2",
         "-c:v", "libx264", "-crf", "25", "-preset", "slow", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", dst])
    run(["ffmpeg", "-y", "-v", "error", "-ss", str(poster_t), "-i", dst,
         "-frames:v", "1", "-q:v", "3", poster])
    mb = os.path.getsize(dst) / 1024 / 1024
    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", dst], capture_output=True, text=True).stdout.strip()
    print(f"{key:10} {float(dur):5.1f}s  {mb:5.1f}MB  -> {dst}")

print("done ->", WEB)
