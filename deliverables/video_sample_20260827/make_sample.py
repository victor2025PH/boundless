#!/usr/bin/env python3
"""智聊营销视频样片产线（实施77 渠道五打样，2026-08-27）。

三段式：vo（edge-tts 配音+验证）→ record（playwright 录官网）→ assemble（ffmpeg 合成）。
用法：
    python make_sample.py vo          # 生成中/英配音 + magic byte/时长验证
    python make_sample.py record      # 录制本地站点（默认 http://127.0.0.1:3457）
    python make_sample.py assemble    # 合成 chatx_sample_zh.mp4 / _en.mp4 + ffprobe 验收
    python make_sample.py all

纪律（media-artifact-validation）：任何音视频产物落盘后必验 magic bytes / ffprobe，
不以「文件存在 + 体积」当验证。配音用中性播音神经声（公开营销素材不用具体人设克隆声；
产线支持换 176 hub fish/index 克隆声——换 vo() 的实现即可，验证链不变）。
"""
from __future__ import annotations

import asyncio
import json
import math
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# 录屏源站：默认本地已构建站点；env SAMPLE_SITE 可切生产（如 https://bd2026.cc）
import os as _os
SITE = _os.environ.get("SAMPLE_SITE", "http://127.0.0.1:3457")

# ── 文案（脚本=字幕分段的单一来源；防编造纪律：无业绩数字、无成交时效承诺）──────
SEGS_ZH = [
    "凌晨三点，一条阿拉伯语询盘进来。",
    "翻译、拟稿、回复——AI 全部搞定，用客户的母语，像真人一样。",
    "Telegram、WhatsApp、LINE、Messenger，一个收件箱全接住。",
    "AI 记住每个客户，主动跟进，把询盘聊成订单。",
    "智聊 ChatX，下载即用，标准翻译永久免费。",
]
SEGS_EN = [
    "Three a.m. — an Arabic inquiry comes in.",
    "Translated, drafted, answered — by AI, in your customer's own language, human-like.",
    "Telegram, WhatsApp, LINE, Messenger: one inbox catches them all.",
    "It remembers every customer and follows up until the deal closes.",
    "ChatX. Download and go — standard translation free forever.",
]
VOICES = {"zh": "zh-CN-XiaoxiaoNeural", "en": "en-US-JennyNeural"}


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("  $", " ".join(str(c) for c in cmd)[:180])
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def ffprobe_json(path: Path) -> dict:
    r = run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    if r.returncode != 0:
        raise SystemExit(f"[FAIL] ffprobe 读不了 {path}: {r.stderr[:200]}")
    return json.loads(r.stdout)


def dur_of(path: Path) -> float:
    return float(ffprobe_json(path)["format"]["duration"])


def check_audio(path: Path) -> float:
    """magic bytes + ffprobe 双验，返回时长秒。"""
    head = path.read_bytes()[:3]
    if not (head == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
        raise SystemExit(f"[FAIL] {path.name} 非 MP3（头 {head.hex()}）——落盘的不是音频")
    d = dur_of(path)
    if d < 10:
        raise SystemExit(f"[FAIL] {path.name} 时长仅 {d:.1f}s，疑似截断")
    print(f"  [OK] {path.name}: MP3 头合法, {d:.1f}s, {path.stat().st_size//1024}KB")
    return d


def vo() -> None:
    import edge_tts

    async def gen(lang: str) -> None:
        text = "".join(SEGS_ZH) if lang == "zh" else " ".join(SEGS_EN)
        out = ROOT / f"vo_{lang}.mp3"
        await edge_tts.Communicate(text, VOICES[lang], rate="-4%").save(str(out))
        check_audio(out)

    for lang in ("zh", "en"):
        asyncio.run(gen(lang))


def record(lang: str = "zh") -> None:
    """按语言录对应路由（zh=/，en=/en）——英文版画面必须是英文页。"""
    from playwright.sync_api import sync_playwright

    base = "" if lang == "zh" else "/en"
    vdir = ROOT / "raw"
    vdir.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            record_video_dir=str(vdir),
            record_video_size={"width": 1920, "height": 1080},
            locale="zh-CN" if lang == "zh" else "en-US",
        )
        page = ctx.new_page()
        page.goto(SITE + (base or "/"), wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2500)
        # IntroCover 开场遮罩：点一下 + Escape 双保险（不存在也无害）
        try:
            page.mouse.click(960, 540)
            page.keyboard.press("Escape")
        except Exception:
            pass
        page.wait_for_timeout(2500)  # hero 动画
        for sel, dwell in (("#autochat", 12000), ("#translate", 9000)):
            try:
                page.eval_on_selector(sel, "el => el.scrollIntoView({behavior:'smooth', block:'start'})")
            except Exception:
                page.evaluate("window.scrollBy({top: 1600, behavior:'smooth'})")
            page.wait_for_timeout(dwell)
        page.goto(SITE + base + "/download/chatx", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1500)
        page.evaluate("window.scrollBy({top: 500, behavior:'smooth'})")
        page.wait_for_timeout(4500)
        ctx.close()  # flush 视频
        browser.close()
    vids = sorted(vdir.glob("*.webm"), key=lambda f: f.stat().st_mtime)
    if not vids:
        raise SystemExit("[FAIL] 录屏无产物")
    latest = vids[-1]
    target = ROOT / f"footage_{lang}.webm"
    if target.exists():
        target.unlink()
    latest.rename(target)
    d = dur_of(target)
    if d < 20:
        raise SystemExit(f"[FAIL] 录屏仅 {d:.1f}s，疑似页面未加载")
    print(f"  [OK] footage_{lang}.webm: {d:.1f}s, {target.stat().st_size//1024}KB")


def _srt_ts(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms//3600000:02d}:{ms%3600000//60000:02d}:{ms%60000//1000:02d},{ms%1000:03d}"


def write_srt(lang: str, total: float) -> Path:
    segs = SEGS_ZH if lang == "zh" else SEGS_EN
    weights = [len(s) for s in segs]
    wsum = sum(weights)
    srt, t = [], 0.0
    for i, (seg, w) in enumerate(zip(segs, weights), 1):
        d = total * w / wsum
        srt.append(f"{i}\n{_srt_ts(t)} --> {_srt_ts(min(t + d, total))}\n{seg}\n")
        t += d
    out = ROOT / f"subs_{lang}.srt"
    out.write_text("\n".join(srt), encoding="utf-8-sig")
    return out


def assemble() -> None:
    for lang in ("zh", "en"):
        footage = ROOT / f"footage_{lang}.webm"
        if not footage.exists():
            footage = ROOT / "footage.webm"  # 兜底：单素材双配音
        src_d = dur_of(footage)
        vo_p = ROOT / f"vo_{lang}.mp3"
        vo_d = check_audio(vo_p)
        target = vo_d + 1.2  # 音后留 1.2s 尾帧
        srt = write_srt(lang, vo_d)
        # subtitles 滤镜的 Windows 路径转义：绝对路径 + 冒号转义 + 正斜杠
        srt_esc = str(srt).replace("\\", "/").replace(":", "\\:")
        speed = target / src_d
        out = ROOT / f"chatx_sample_{lang}.mp4"
        vf = (
            f"setpts={speed:.6f}*PTS,scale=1920:1080:flags=lanczos,fps=30,"
            f"subtitles='{srt_esc}':force_style='FontName=Microsoft YaHei,FontSize=17,"
            f"PrimaryColour=&HFFFFFF&,OutlineColour=&H80000000&,Outline=2,MarginV=46',"
            f"drawtext=text='bd2026.cc':fontfile='C\\:/Windows/Fonts/arial.ttf':"
            f"fontsize=30:fontcolor=white@0.55:x=w-tw-42:y=h-th-36,"
            f"fade=t=in:st=0:d=0.6,fade=t=out:st={target-0.8:.2f}:d=0.8"
        )
        r = run([
            "ffmpeg", "-y", "-i", str(footage), "-i", str(vo_p),
            "-filter_complex", f"[0:v]{vf}[v]",
            "-map", "[v]", "-map", "1:a",
            "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k",
            "-t", f"{target:.2f}", "-movflags", "+faststart", str(out),
        ])
        if r.returncode != 0:
            raise SystemExit(f"[FAIL] ffmpeg 合成 {lang} 失败:\n{r.stderr[-800:]}")
        info = ffprobe_json(out)
        kinds = {s["codec_type"] for s in info["streams"]}
        d = float(info["format"]["duration"])
        if kinds != {"video", "audio"} or abs(d - target) > 1.5 or out.stat().st_size < 500_000:
            raise SystemExit(f"[FAIL] {out.name} 验收不过: streams={kinds} dur={d:.1f}s size={out.stat().st_size}")
        print(f"  [OK] {out.name}: {d:.1f}s, 视频+音频双流, {out.stat().st_size//1024//1024}MB")


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    if stage == "vo":
        vo()
    elif stage == "record":
        record(sys.argv[2] if len(sys.argv) > 2 else "zh")
    elif stage == "assemble":
        assemble()
    else:  # all
        vo()
        record("zh")
        record("en")
        assemble()
    print("DONE")
