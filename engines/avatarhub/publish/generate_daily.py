# -*- coding: utf-8 -*-
"""generate_daily.py — （可选）Veo API 每日自动生成一条演示视频进发布队列。

不配置就不启用：publish_daily.bat 每天先跑本脚本再跑 publish_daily.py，
没有 secrets/gemini_api_key.txt 时本脚本直接安静退出，队列只靠手动投喂（Flow 批量生成）。

启用方法：
  1. https://aistudio.google.com/apikey 创建 API Key（需绑结算账号，Veo 无免费额度）
  2. 把 Key 存成一行文本：D:\\projects\\模仿音色\\secrets\\gemini_api_key.txt
费用参考：Veo 3.1 Fast 1080p 8 秒约 $1.2/条（每天一条 ≈ $36/月）；
  改用 veo-3.1-generate-preview（画质档）约 $3.2/条。分镜提示词轮换 14 天一循环。

注意：队列里已有存货时本脚本跳过生成（先把手动投喂的精品发完，不浪费 API 钱）。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
QUEUE = BASE / "queue"
KEY_FILE = ROOT / "secrets" / "gemini_api_key.txt"
API = "https://generativelanguage.googleapis.com/v1beta"
MODEL = "veo-3.1-fast-generate-preview"   # 换画质档：veo-3.1-generate-preview
RESOLUTION = "1080p"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

STYLE = (" Cinematic tech-noir atmosphere, neon cyan and violet accent lighting, photorealistic,"
         " shallow depth of field, smooth gimbal camera, 35mm film look,"
         " no on-screen text, no captions, no watermarks.")

# 14 天轮换的营销短片提示词（8 秒单镜头，独立成片；周更主题与 /order 展示位互补）
PROMPTS: list[dict] = [
    {"slug": "live", "prompt": "Split-screen: left, a young East Asian man in a black hoodie talks energetically to a webcam in a dim streaming room; right, the SAME motion and lip-sync but he appears as a bearded European man. A floating latency meter pulses '42 ms' in cyan. Audio: one sentence heard twice, young voice then deep voice." + STYLE},
    {"slug": "voice", "prompt": "Macro shot of a studio condenser microphone with cyan rim light. A cyan waveform lifts off the desk as a hologram, turns violet and speaks on its own in the same male voice but laughing with joy. Audio: the same timbre shifting from calm to excited laughter." + STYLE},
    {"slug": "faceswap", "prompt": "A young Asian woman in a red jacket walks toward camera through a neon night market; a thin cyan scan-line sweeps across the frame and behind it she has a completely different face — a European woman with an auburn bob — while motion, jacket and lighting stay identical. Audio: night market ambience with a soft electronic shimmer." + STYLE},
    {"slug": "interp", "prompt": "Split-screen video call: a Chinese businesswoman speaks Mandarin on the left; a glowing translation node converts her cyan waveform to violet, and the American client on the right hears fluent English in the SAME female voice. A meter reads '1.2 s'. Audio: Mandarin sentence flowing into English, one voice." + STYLE},
    {"slug": "studio", "prompt": "A young woman faces a floor-to-ceiling smart mirror in a futuristic fitting room; a band of cyan light sweeps down and her long black hair and white shirt transform into a silver bob with a black leather jacket, same pose and smile. Audio: airy whoosh synced to the sweep." + STYLE},
    {"slug": "avatar", "prompt": "A portrait photo lying on a glass desk ripples like water; the person blinks, lifts their head out of the frame in parallax and starts speaking to camera with perfect lip-sync while cyan data particles flow from the photo edges. Audio: a confident voice saying 'One photo. One script.'" + STYLE},
    {"slug": "live", "prompt": "Over-the-shoulder shot of a streamer's desk at night: on the monitor, his live stream shows a completely different face mirroring his every head turn in perfect sync; he waves and the on-screen persona waves simultaneously. Audio: keyboard clicks, soft synthwave, a chuckle in two different voices." + STYLE},
    {"slug": "voice", "prompt": "Three glowing orbs orbit slowly in a dark studio, each pulsing as the SAME female voice performs three moods in sequence: an excited announcement, a soft whisper, a calm news-anchor read. Audio: three emotional reads of one voice, seamless transitions." + STYLE},
    {"slug": "faceswap", "prompt": "Extreme slow-motion close-up of a woman's face as a thin cyan scan-line crosses it; on one side she is herself, on the other a different person — skin texture, freckles and neon reflections continuous across the line, tiny particles sparkling at the edge. Audio: low sub-bass pulse and delicate glass chimes." + STYLE},
    {"slug": "interp", "prompt": "A businessman on a video call replies in English; the glowing node between the panels reverses the flow and the Chinese partner hears natural Mandarin in HIS voice. Both nod and laugh at the same joke simultaneously. Audio: English then the same male timbre speaking Mandarin, shared laughter." + STYLE},
    {"slug": "studio", "prompt": "Camera orbits a young woman as a smart mirror shows four different looks of her side by side as living reflections — evening dress, streetwear, business suit, casual — she touches the glass to pick one and a soft sparkle confirms. Audio: elegant chord with a glass ting." + STYLE},
    {"slug": "avatar", "prompt": "A digital presenter delivers a product pitch to camera in a sleek studio; behind her, the same presenter appears on three phone screens speaking different languages with matching lip-sync. Audio: overlapping multilingual speech resolving into one confident line in English." + STYLE},
    {"slug": "live", "prompt": "A gamer in RGB-lit room raises his hand for a high-five toward the camera; on the stream preview beside him, a fantasy-styled avatar mirrors the exact gesture with zero visible delay, then both point at the viewer. Audio: energetic voice line heard in two different voices, crowd notification pings." + STYLE},
    {"slug": "voice", "prompt": "A waveform hologram orbits a small glowing globe with soft light ribbons; the same voice speaks one sentence in English, then flows mid-breath into Japanese with identical timbre while the ribbons shift color. Audio: bilingual line, one voice, gentle outro pad." + STYLE},
]


def http(url: str, payload: dict | None = None, key: str = "") -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def main():
    if not KEY_FILE.exists():
        return  # 未启用 API 生成：静默退出，等手动投喂
    key = KEY_FILE.read_text(encoding="utf-8").strip()
    QUEUE.mkdir(parents=True, exist_ok=True)
    if list(QUEUE.glob("*.mp4")):
        print("[生成] 队列已有存货，跳过 API 生成（先发手动投喂的素材）。")
        return

    day = datetime.now().timetuple().tm_yday
    item = PROMPTS[day % len(PROMPTS)]
    print(f"[生成] {MODEL} · {RESOLUTION} · 主题 {item['slug']}")
    op = http(f"{API}/models/{MODEL}:predictLongRunning", {
        "instances": [{"prompt": item["prompt"]}],
        "parameters": {"aspectRatio": "16:9", "resolution": RESOLUTION},
    }, key)
    name = op.get("name")
    if not name:
        print(f"[错误] 提交失败：{op}")
        sys.exit(1)

    for _ in range(90):  # 最多等 15 分钟
        time.sleep(10)
        st = http(f"{API}/{name}", key=key)
        if st.get("done"):
            samples = (st.get("response", {}).get("generateVideoResponse", {}) or {}).get("generatedSamples", [])
            uri = samples[0]["video"]["uri"] if samples else None
            if not uri:
                print(f"[错误] 生成失败：{json.dumps(st)[:400]}")
                sys.exit(1)
            out = QUEUE / f"{datetime.now():%Y%m%d}-{item['slug']}.mp4"
            req = urllib.request.Request(uri, headers={"x-goog-api-key": key})
            with urllib.request.urlopen(req, timeout=600) as r, out.open("wb") as f:
                f.write(r.read())
            print(f"[生成] ✓ {out.name}（{out.stat().st_size / 1e6:.1f}MB）已进队列")
            return
    print("[错误] 等待超时（15 分钟未完成）。")
    sys.exit(1)


if __name__ == "__main__":
    main()
