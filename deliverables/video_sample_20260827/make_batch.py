#!/usr/bin/env python3
"""智聊营销视频批量产线（实施77 渠道五 第一批量产，2026-08-27）。

选题注册表驱动：每个选题 = 分段文案（=字幕单源）× 语言 × 画面素材。
复用 make_sample.py 的验证链（magic bytes + ffprobe 双验）。

用法：
    python make_batch.py vo                 # 全部选题配音
    python make_batch.py record_pricing     # 录 T4 专用画面（需本地站点 :3457）
    python make_batch.py assemble           # 合成全部成片
    python make_batch.py all
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

from make_sample import ROOT, SITE, check_audio, dur_of, ffprobe_json, run, _srt_ts

# ── 选题注册表（文案纪律：零业绩数字、零成交时效承诺——防编造闸红线）──────────
TOPICS: dict[str, dict] = {
    # T2 多平台收件箱（中文，复用 footage_zh）
    "t2_inbox": {
        "footage": "footage_zh.webm",
        "langs": {
            "zh": {
                "voice": "zh-CN-YunxiNeural",  # 男声换个质感，与 T1 小晓区分
                "segs": [
                    "客户在 Telegram 问价，在 WhatsApp 催单，在 LINE 发语音。",
                    "切五个软件回消息的日子，该结束了。",
                    "智聊 ChatX：多平台消息，一个收件箱全接住。",
                    "AI 自动拟稿、自动翻译，谁该跟进一目了然。",
                    "免费下载，几分钟接入你的第一个账号。",
                ],
            },
        },
    },
    # T3 AI 真人感（中文，复用 footage_zh）
    "t3_ai": {
        "footage": "footage_zh.webm",
        "langs": {
            "zh": {
                "voice": "zh-CN-XiaoxiaoNeural",
                "segs": [
                    "你的 AI 客服，还在复制粘贴模板话术吗？",
                    "智聊的 AI 有人设、有记忆：记得客户上次聊过什么。",
                    "会主动问候，会用你的声音发语音消息。",
                    "客户以为在和真人聊天——因为它聊得比真人还上心。",
                    "智聊 ChatX，AI 数字员工，免费开始。",
                ],
            },
        },
    },
    # T4 零门槛免费开始（中文，专用 footage_pricing）
    "t4_free": {
        "footage": "footage_pricing.webm",
        "langs": {
            "zh": {
                "voice": "zh-CN-XiaoxiaoNeural",
                "segs": [
                    "不用服务器，不用显卡，不用配 API Key。",
                    "一台普通办公电脑，下载智聊即可开工。",
                    "标准翻译永久免费不限量，每月还送 AI 额度。",
                    "注册 72 小时内，6U 大礼包双倍到账。",
                    "智聊 ChatX，免费开始，用多少充多少。",
                ],
            },
        },
    },
    # T1 询盘接单 · 东南亚翻制（复用 footage_en——英文界面对 SEA 受众正确）
    "t1_inquiry": {
        "footage": "footage_en.webm",
        "langs": {
            "id": {
                "voice": "id-ID-GadisNeural",
                "segs": [
                    "Jam 3 pagi — pertanyaan berbahasa Arab masuk.",
                    "Diterjemahkan, disusun, dibalas — oleh AI, dalam bahasa pelanggan Anda, seperti manusia.",
                    "Telegram, WhatsApp, LINE, Messenger: satu kotak masuk untuk semuanya.",
                    "AI mengingat setiap pelanggan dan menindaklanjuti sampai closing.",
                    "ChatX. Unduh dan langsung pakai — terjemahan standar gratis selamanya.",
                ],
            },
            "th": {
                "voice": "th-TH-PremwadeeNeural",
                "segs": [
                    "ตีสาม — มีข้อความสอบถามภาษาอาหรับเข้ามา",
                    "แปล ร่าง ตอบ — โดย AI ในภาษาของลูกค้า เป็นธรรมชาติเหมือนคนจริง",
                    "Telegram, WhatsApp, LINE, Messenger รวมทุกแชทไว้ในกล่องเดียว",
                    "AI จดจำลูกค้าทุกคน และติดตามจนปิดการขาย",
                    "ChatX ดาวน์โหลดใช้ได้เลย — แปลมาตรฐานฟรีตลอดไป",
                ],
            },
        },
    },
}

# 字幕字体按语言选（泰文用 YaHei 会出豆腐块）
SUB_FONT = {"zh": "Microsoft YaHei", "id": "Segoe UI", "th": "Leelawadee UI"}


def vo_all() -> None:
    import edge_tts

    async def gen(text: str, voice: str, out: Path) -> None:
        await edge_tts.Communicate(text, voice, rate="-4%").save(str(out))

    for tid, spec in TOPICS.items():
        for lang, l in spec["langs"].items():
            out = ROOT / f"vo_{tid}_{lang}.mp3"
            joiner = "" if lang == "zh" else " "
            asyncio.run(gen(joiner.join(l["segs"]), l["voice"], out))
            check_audio(out)


def record_pricing() -> None:
    """T4 专用画面：首页 hero → /pricing（免费横条+新人海报+充值档）→ /download/chatx。"""
    from playwright.sync_api import sync_playwright

    vdir = ROOT / "raw"
    vdir.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            record_video_dir=str(vdir),
            record_video_size={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        page = ctx.new_page()
        page.goto(SITE + "/", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)
        try:
            page.mouse.click(960, 540)
            page.keyboard.press("Escape")
        except Exception:
            pass
        page.wait_for_timeout(2500)
        page.goto(SITE + "/pricing", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)
        for dy, dwell in ((900, 4000), (900, 4000), (1000, 4000), (1200, 4000)):
            page.evaluate(f"window.scrollBy({{top: {dy}, behavior:'smooth'}})")
            page.wait_for_timeout(dwell)
        page.goto(SITE + "/download/chatx", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1500)
        page.evaluate("window.scrollBy({top: 500, behavior:'smooth'})")
        page.wait_for_timeout(4000)
        ctx.close()
        browser.close()
    vids = sorted(vdir.glob("*.webm"), key=lambda f: f.stat().st_mtime)
    if not vids:
        raise SystemExit("[FAIL] 录屏无产物")
    target = ROOT / "footage_pricing.webm"
    if target.exists():
        target.unlink()
    vids[-1].rename(target)
    d = dur_of(target)
    if d < 20:
        raise SystemExit(f"[FAIL] footage_pricing 仅 {d:.1f}s")
    print(f"  [OK] footage_pricing.webm: {d:.1f}s, {target.stat().st_size//1024}KB")


def write_srt(tid: str, lang: str, total: float) -> Path:
    segs = TOPICS[tid]["langs"][lang]["segs"]
    weights = [max(1, len(s)) for s in segs]
    wsum = sum(weights)
    srt, t = [], 0.0
    for i, (seg, w) in enumerate(zip(segs, weights), 1):
        d = total * w / wsum
        srt.append(f"{i}\n{_srt_ts(t)} --> {_srt_ts(min(t + d, total))}\n{seg}\n")
        t += d
    out = ROOT / f"subs_{tid}_{lang}.srt"
    out.write_text("\n".join(srt), encoding="utf-8-sig")
    return out


def assemble_all() -> None:
    for tid, spec in TOPICS.items():
        footage = ROOT / spec["footage"]
        if not footage.exists():
            raise SystemExit(f"[FAIL] 缺画面素材 {spec['footage']}（先跑对应 record）")
        src_d = dur_of(footage)
        for lang in spec["langs"]:
            vo_p = ROOT / f"vo_{tid}_{lang}.mp3"
            vo_d = check_audio(vo_p)
            target = vo_d + 1.2
            srt = write_srt(tid, lang, vo_d)
            srt_esc = str(srt).replace("\\", "/").replace(":", "\\:")
            font = SUB_FONT.get(lang, "Segoe UI")
            speed = target / src_d
            out = ROOT / f"chatx_{tid}_{lang}.mp4"
            vf = (
                f"setpts={speed:.6f}*PTS,scale=1920:1080:flags=lanczos,fps=30,"
                f"subtitles='{srt_esc}':force_style='FontName={font},FontSize=17,"
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
                raise SystemExit(f"[FAIL] ffmpeg {tid}/{lang}:\n{r.stderr[-600:]}")
            info = ffprobe_json(out)
            kinds = {s["codec_type"] for s in info["streams"]}
            d = float(info["format"]["duration"])
            if kinds != {"video", "audio"} or abs(d - target) > 1.5 or out.stat().st_size < 400_000:
                raise SystemExit(f"[FAIL] {out.name} 验收不过: {kinds} {d:.1f}s {out.stat().st_size}")
            print(f"  [OK] {out.name}: {d:.1f}s, {out.stat().st_size//1024//1024}MB")


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    if stage == "vo":
        vo_all()
    elif stage == "record_pricing":
        record_pricing()
    elif stage == "assemble":
        assemble_all()
    else:
        vo_all()
        record_pricing()
        assemble_all()
    print("DONE")
