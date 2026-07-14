# -*- coding: utf-8 -*-
"""_gen_starter_voices.py — 生成「启动角色包」占位声音样本（开发工具，不随安装器分发）。

用 edge-tts 合成 6 条中/英/日 男女中性示例语音 → ffmpeg 转 44.1k 单声道 WAV，
连同同名 .txt 参考文本写入 data/starter_profiles/voices/。这些是【占位素材】，
正式发布前请用已授权的录音替换（保持文件名/清单不变即可）。

用法（facefusion 环境）：
  python tools/_gen_starter_voices.py
"""
from __future__ import annotations

import asyncio
import os
import subprocess

import edge_tts
import imageio_ffmpeg

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "data", "starter_profiles", "voices")
FF = imageio_ffmpeg.get_ffmpeg_exe()

# (文件名, edge-tts 声音, 参考文本)。文本与音频内容一致 = Fish/CosyVoice 零样本克隆的 reference_text。
VOICES = [
    ("starter_zh_f", "zh-CN-XiaoxiaoNeural",
     "大家好，很高兴认识你。这是一段用于声音克隆的示例语音，语气自然、发音清晰。希望我们今天的交流顺利愉快。"),
    ("starter_zh_m", "zh-CN-YunxiNeural",
     "你好，这里是一段中文示例音色。声音沉稳，节奏平缓，很适合用来做声音克隆的参考。感谢你的聆听。"),
    ("starter_en_f", "en-US-AriaNeural",
     "Hello, it is a pleasure to meet you. This is a sample voice used for cloning, with clear pronunciation and a natural, friendly tone. I hope you have a wonderful day."),
    ("starter_en_m", "en-US-GuyNeural",
     "Hi there. This is an English sample voice for cloning reference. The pacing is steady and the articulation is clear. Thank you for listening today."),
    ("starter_ja_f", "ja-JP-NanamiNeural",
     "こんにちは、お会いできて嬉しいです。これは音声クローン用のサンプル音声です。発音は明瞭で、落ち着いた口調になっています。"),
    ("starter_ja_m", "ja-JP-KeitaNeural",
     "こんにちは。これは日本語のサンプル音声です。声は穏やかで、はっきりとした発音を心がけています。よろしくお願いします。"),
]


async def synth(stem: str, voice: str, text: str):
    os.makedirs(OUT, exist_ok=True)
    mp3 = os.path.join(OUT, stem + ".mp3")
    wav = os.path.join(OUT, stem + ".wav")
    await edge_tts.Communicate(text, voice).save(mp3)
    subprocess.run([FF, "-y", "-i", mp3, "-ac", "1", "-ar", "44100", wav],
                   check=True, capture_output=True)
    os.remove(mp3)
    with open(os.path.join(OUT, stem + ".txt"), "w", encoding="utf-8") as f:
        f.write(text)
    print(f"OK {stem}  {os.path.getsize(wav)} bytes")


async def main():
    for stem, voice, text in VOICES:
        try:
            await synth(stem, voice, text)
        except Exception as e:
            print(f"FAIL {stem}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
