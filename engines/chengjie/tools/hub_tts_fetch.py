#!/usr/bin/env python3
"""hub_tts_fetch.py — 从幻声 hub（/api/tts_only）取语音样本的唯一正确姿势。

2026-08-13 事故沉淀（.cursor/rules/media-artifact-validation.mdc）：tts_only 返回
**JSON 信封**（音频在 ``audio_base64`` 字段），直接 ``-OutFile`` 会把 JSON 存成
假 .wav；且「HTTP 200 + 文件尺寸」对信封同样成立，必须验 magic bytes。
本工具把 解码 + RIFF 验证 + 最小尺寸守卫 焊死在落盘路径上，任一步失败
非零退出——以后所有采样批走这里，别再手搓内联命令。

用法：
    python tools/hub_tts_fetch.py --profile 林小雨-智聊 --text 你好呀 --out out.wav
    python tools/hub_tts_fetch.py --profile 林小雨-智聊 --text 你好 --out o.wav \
        --hub http://192.168.0.176:9000 --best-of 2 --timeout 120
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.request
from pathlib import Path

MAGIC = {
    b"RIFF": "wav",
    b"OggS": "ogg",
    b"ID3": "mp3",
    b"fLaC": "flac",
}
MIN_AUDIO_BYTES = 8_000  # 短句 wav 也远大于此；小于它=大概率错误产物


def sniff(data: bytes) -> str | None:
    for magic, kind in MAGIC.items():
        if data.startswith(magic):
            return kind
    if len(data) > 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    return None


def fetch(hub: str, profile: str, text: str, *, language: str = "zh-cn",
          best_of: int = 1, fmt: str = "wav", timeout: float = 120.0,
          emotion: str = "", tts_engine: str = "") -> bytes:
    body: dict = {
        "profile": profile, "text": text, "language": language,
        "best_of": best_of, "format": fmt,
    }
    # 2026-08-13 盲测事故第二课：不带 emotion=全程平直；不带 tts_engine=
    # 隐式路由（prefer 语义，目标引擎离线会静默回落 fish 且响应无引擎标签）。
    # 采样批必须显式钉引擎 + 事后验采样率归属（index=22.05k / moss=24k / fish=44.1k）。
    if emotion and emotion.strip().lower() != "neutral":
        body["emotion"] = emotion.strip().lower()
    if tts_engine:
        body["tts_engine"] = tts_engine.strip()
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        hub.rstrip("/") + "/api/tts_only", data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = str(resp.headers.get("Content-Type") or "")
        body = resp.read()
    elapsed = time.monotonic() - t0

    if "application/json" in ctype:
        envelope = json.loads(body.decode("utf-8"))
        if not envelope.get("ok", True):
            raise SystemExit(f"hub 返回 ok=false: {str(envelope)[:200]}")
        b64 = envelope.get("audio_base64") or ""
        if not b64:
            raise SystemExit(f"信封缺 audio_base64（keys={list(envelope)}）")
        audio = base64.b64decode(b64)
        print(f"envelope ok elapsed_ms={envelope.get('elapsed_ms')} wall={elapsed:.1f}s")
    else:
        audio = body  # 原始流式响应（当前 hub 不走这形态，防御性保留）
        print(f"raw body content-type={ctype} wall={elapsed:.1f}s")

    kind = sniff(audio)
    if kind is None:
        raise SystemExit(
            f"产物不是已知音频格式（head={audio[:16]!r}）——拒绝落盘。")
    if len(audio) < MIN_AUDIO_BYTES:
        raise SystemExit(f"产物过小（{len(audio)}B < {MIN_AUDIO_BYTES}B）——拒绝落盘。")
    print(f"audio ok: {kind} {len(audio) / 1024:.0f}KB")
    return audio


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", required=True)
    ap.add_argument("--text", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--hub", default="http://192.168.0.176:9000")
    ap.add_argument("--language", default="zh-cn")
    ap.add_argument("--best-of", type=int, default=1)
    ap.add_argument("--format", default="wav")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--emotion", default="", help="happy/sad/excited/calm/gentle/serious…（空=平直默认）")
    ap.add_argument("--tts-engine", default="", help="显式钉引擎（index_tts/moss_ttsd/fish_speech），空=隐式路由（勿用于采样批）")
    args = ap.parse_args()

    audio = fetch(args.hub, args.profile, args.text, language=args.language,
                  best_of=args.best_of, fmt=args.format, timeout=args.timeout,
                  emotion=args.emotion, tts_engine=args.tts_engine)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(audio)
    print(f"saved {args.out} ({args.out.stat().st_size / 1024:.0f}KB)")


if __name__ == "__main__":
    main()
