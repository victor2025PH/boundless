#!/usr/bin/env python3
"""P 系列拼装（B 模式：副歌唱做钩子/尾声 + 念白讲信息）——按录屏标记切，不按歌词行数切，不循环。

  python make_persona_episode.py P1
  python make_persona_episode.py P1 --no-vertical

时间线：
  [0, hook)         人物卡（谁 / 岗位 / 处境）+ 副歌起唱
  [hook, ...)       录屏连续播放（从首段 content_offset 起，到各段 end_t），只改「景别」：
                    标记 zoom=chat/low/list 时推近 1.5×（1280×720 → 1920×1080），narr 沿用上一动作景别
  念白              每个 narr 标记 → edge-tts（按人物选声）→ 放在标记时刻；字幕同源
  语音克隆          voice_preview 标记里的音频文件（已验 magic）→ 放在试听时刻，底床让路
  尾卡 6s           标题 / CTA / 二维码(utm) / 「歌声与念白由幻声生成」+ 副歌回放
产物：out/<P>/<P>_persona.mp4（16:9）+ out/<P>/<P>_persona_v.mp4（9:16，三段布局：人物条 / 聊天栏 / 字幕带）。
素材不够长 → 直接 FAIL（不 -stream_loop）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
BG = ROOT.parent / "huanyan-brand-bg" / "cosmos-wide.jpg"
BG_TALL = ROOT.parent / "huanyan-brand-bg" / "cosmos-tall.jpg"
MARK = ROOT.parent / "huanyan-brand-bg" / "boundless-mark-512.png"
FONT_B, FONT_R = "C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc"
W, H = 1920, 1080
VW, VH = 1080, 1920
HOOK_D, END_D = 3.2, 6.0
ZOOM = {  # 1280×720 取景框（1.5×）
    "zoom": (360, 100), "chat": (360, 100),          # 聊天栏：含会话头（客户名/平台）+ 气泡
    "zoom_low": (360, 360), "low": (360, 360),       # 聊天栏下半：气泡 + 回复框
    "zoom_list": (40, 100), "list": (40, 100),       # 会话列表 + 聊天栏
    "list_clean": (40, 100),                         # 同 zoom_list，但列表已过滤干净 → 出厂不打码
    "zoom_panel": (640, 360), "panel": (640, 360),   # 右栏业务助手（语音卡）+ 聊天栏右半
    "zoom_guide": (400, 100),                        # F2 接入引导页：居中向导卡（步骤栏 + 面板），无会话列表
}
ZOOM_V = {  # 竖屏中段 1080×1080 直裁（不放大）
    "zoom": (360, 60), "chat": (360, 60),
    "zoom_low": (360, 0), "low": (360, 0),
    "zoom_list": (0, 60), "list": (0, 60), "list_clean": (0, 60),
    "zoom_panel": (840, 0), "panel": (840, 0),
    "zoom_guide": (420, 60),
}
OP_ZOOM = {"type_send": "zoom_low", "voice_preview": "zoom_panel"}   # 动作默认景别覆盖（标记里写 zoom 时）
STALL_S, KEEP_HEAD, KEEP_TAIL = 20.0, 6.0, 6.0   # 单个动作超过 20s 视为卡顿，留头 6s 尾 6s
NO_STALL_OPS = {"voice_gen"}                     # 生成等待期间画面有进度，不剪
# ai_reply 尾段是「发送中→落地+译文」，剪掉中段会留下字幕已发出、画面还在转圈（P6 take2）
# 按动作收紧：来信只是「等一行长出来」、开会话只是「点一下」，等超过阈值全是静止画面（2026-09-18 P2 七录 WA 来信等 16s、
# open_stage 重试 8s → 成片 138.8s 撞 130s 上限）
STALL_BY_OP = {"incoming": (8.0, 2.5, 3.0), "open_stage": (6.0, 2.0, 2.5), "ai_reply_send": (24.0, 8.0, 8.0),
               # F1 全自动：拟稿 + 拟人延迟（deliver_delay 10~54s）常等 40~90s，画面静止；留头（来信刚落地）
               # 留尾（气泡长出来 + 得点）——中段是真等待，不是卡顿，但观众不必陪等
               "wait_outbound": (20.0, 6.0, 9.0),
               # F2 拟稿人审：来信后 AI 拟稿 8~20s，画面只有回复框上方等一条草稿条长出来；留头（来信刚落地）留尾（草稿条 + 得点）
               "wait_draft": (14.0, 4.0, 8.0),
               # 引导页第 3 步「发一条测试消息」：注入后轮询 5s 才翻绿卡，等待期画面静止
               "wx_test_inbound": (12.0, 4.0, 6.0), "wx_verify": (12.0, 4.0, 6.0)}
NARR_VOICE_DEFAULT = "zh-CN-XiaoxiaoNeural"
NARR_VOICES = {"P1": "zh-CN-YunxiNeural", "P3": "zh-CN-XiaoxiaoNeural", "P4": "zh-CN-XiaoyiNeural",
               "P2": "zh-CN-YunjianNeural", "P6": "zh-CN-YunjianNeural", "P7": "zh-CN-XiaoxiaoNeural", "P8": "zh-CN-YunyangNeural"}
# edge-tts zh-CN 现存只有 Xiaoxiao/Xiaoyi/Yunjian/Yunxi/Yunxia/Yunyang 六把（2026-09-18 Xiaohan 已下架 → NoAudioReceived）

SC = json.loads((ROOT / "scenarios_persona.json").read_text(encoding="utf-8"))


def run(cmd, check=True):
    print("  $", " ".join(str(c) for c in cmd)[:180])
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        (OUT / "_last_ffmpeg_err.txt").write_text(r.stderr, encoding="utf-8")
        raise SystemExit(f"[FAIL] {cmd[0]}: {r.stderr[:700]} … (全文 out/_last_ffmpeg_err.txt)")
    return r


def ffdur(p: Path) -> float:
    r = run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(p)])
    return float(json.loads(r.stdout)["format"]["duration"])


def ensure_seekable(src: Path) -> Path:
    """Playwright VP8 webm 关键帧极稀（空等 30s 往往整段一个关键帧）。拼装用 -ss 在 -i 前
    会跳到错误画面（P6：群/私聊气泡所在段切成「暂无消息」）。转成 1s GOP 的 H.264 再切。"""
    dst = src.with_name(src.stem + "_seek.mp4")
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime and dst.stat().st_size > 50_000:
        if dst.read_bytes()[4:8] == b"ftyp":
            return dst
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
         "-g", "25", "-keyint_min", "25", "-sc_threshold", "0",
         "-pix_fmt", "yuv420p", "-an", str(dst)])
    if not dst.exists() or dst.read_bytes()[4:8] != b"ftyp":
        raise SystemExit(f"[FAIL] 转码不可跳转 {dst.name}")
    print(f"  seekable {src.name} → {dst.name} {dst.stat().st_size // 1024}KB")
    return dst


def conform_to_wallclock(seek: Path, wall_end: float) -> Path:
    """Playwright 录屏容器时基常比页面墙钟长 6%~18%（F1：a_hook 57s 的动作落在文件 67s）。
    标记（t0/t1）是墙钟真值；此前按比例把标记拉到视频时基——同步是对的，但整段 UI 动作以 0.85× 慢放，
    六段教学片凭空多出 36s。改成把画面压回墙钟（setpts），标记不动、动作真速。文件与墙钟差 ≤8s 不动（噪声）。"""
    real = ffdur(seek)
    if not (wall_end > 10 and real > wall_end + 8):
        return seek
    scale = real / wall_end
    rt = seek.with_name(seek.stem.replace("_seek", "") + "_seekrt.mp4")
    if rt.exists() and rt.stat().st_mtime >= seek.stat().st_mtime and rt.stat().st_size > 50_000 \
            and abs(ffdur(rt) - wall_end) < 1.5:
        return rt
    run(["ffmpeg", "-y", "-v", "error", "-i", str(seek),
         "-vf", f"setpts=PTS/{scale:.5f}", "-r", "25", "-fps_mode", "cfr",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
         "-g", "25", "-keyint_min", "25", "-sc_threshold", "0",
         "-pix_fmt", "yuv420p", "-an", str(rt)])
    got = ffdur(rt)
    if abs(got - wall_end) > 1.5:
        raise SystemExit(f"[FAIL] {rt.name} 回压后 {got:.1f}s ≠ 墙钟 {wall_end:.1f}s")
    print(f"  ~ {seek.name} 时基 {real:.1f}s → 回压到墙钟 {got:.1f}s (÷{scale:.3f})")
    return rt


def ffinfo(p: Path) -> dict:
    return json.loads(run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(p)]).stdout)


# ── 卡片 ─────────────────────────────────────────────────────────────────────

def _bg(w, h, src, dim=0.45):
    im = Image.open(src).convert("RGB")
    s = max(w / im.width, h / im.height)
    im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
    x0, y0 = (im.width - w) // 2, (im.height - h) // 2
    return Image.blend(im.crop((x0, y0, x0 + w, y0 + h)), Image.new("RGB", (w, h), (5, 6, 15)), dim)


def _mark(im, xy, hgt):
    mk = Image.open(MARK).convert("RGBA")
    mk = mk.resize((round(mk.width * hgt / mk.height), hgt), Image.LANCZOS)
    im.paste(mk, xy, mk)


def _center(d, y, s, font, fill=(255, 255, 255), w=W):
    box = d.textbbox((0, 0), s, font=font)
    d.text(((w - (box[2] - box[0])) // 2 - box[0], y), s, font=font, fill=fill)


def persona_card(ep: dict, dst: Path) -> None:
    im = _bg(W, H, BG, 0.5)
    d = ImageDraw.Draw(im)
    _mark(im, (120, 80), 110)
    d.text((330, 105), ep.get("card_label") or "谁在用智聊 · ChatX", font=ImageFont.truetype(FONT_R, 40), fill=(180, 195, 220))
    _center(d, 300, ep["title"], ImageFont.truetype(FONT_B, 104), (255, 224, 130))
    _center(d, 470, f"{ep['persona']} · {ep['job']} · {ep['company']}", ImageFont.truetype(FONT_B, 56))
    _center(d, 580, ep["trigger"], ImageFont.truetype(FONT_R, 44), (200, 210, 230))
    _center(d, 760, f"{ep['platform'].replace('+', ' + ').title()} · {ep['lang_pair']}", ImageFont.truetype(FONT_R, 40), (140, 200, 255))
    d.text((120, 990), "bd2026.cc", font=ImageFont.truetype(FONT_R, 30), fill=(150, 160, 180))
    im.save(dst)


def badge_overlay(ep: dict, dst: Path) -> None:
    """常驻人物角标（RGBA 全幅透明）。badge_pos=br：主体在右上（如档位选择器）时让位到右下。"""
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    txt = f"{ep['persona']} · {ep['job']}"
    f = ImageFont.truetype(FONT_B, 34)
    box = d.textbbox((0, 0), txt, font=f)
    tw = box[2] - box[0]
    x1 = W - 40
    y1 = H - 130 if ep.get("badge_pos") == "br" else 12
    x0, y0 = x1 - tw - 44, y1
    d.rounded_rectangle((x0, y0, x1, y1 + 56), radius=14, fill=(8, 12, 24, 210), outline=(255, 210, 120, 200), width=2)
    d.text((x0 + 22, y0 + 8), txt, font=f, fill=(255, 224, 130))
    d.text((24, H - 44), "bd2026.cc", font=ImageFont.truetype(FONT_R, 26), fill=(190, 200, 220, 200))
    im.save(dst)


def end_card(ep: dict, dst: Path) -> None:
    import qrcode
    im = _bg(W, H, BG, 0.3)
    d = ImageDraw.Draw(im)
    _mark(im, (300, 180), 150)
    d.text((610, 190), ep["hook"], font=ImageFont.truetype(FONT_B, 66), fill="white")
    d.text((610, 290), f"{ep['persona']}的一天，智聊在场", font=ImageFont.truetype(FONT_R, 40), fill=(200, 210, 230))
    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data(f"https://bd2026.cc/download/chatx?utm_source=video&{ep['utm']}")
    qr.make(fit=True)
    im.paste(qr.make_image(fill_color="black", back_color="white").convert("RGB").resize((380, 380)), (1380, 560))
    d.text((300, 600), "智聊 ChatX · 免费开始 · 标准翻译永久免费", font=ImageFont.truetype(FONT_B, 50), fill="white")
    d.text((300, 690), "搜 bd2026.cc · Windows 桌面版下载", font=ImageFont.truetype(FONT_R, 40), fill=(220, 225, 235))
    d.text((300, 800), "歌声与念白由 幻声 VoiceX 生成 · 无界科技 BOUNDLESS", font=ImageFont.truetype(FONT_R, 32), fill=(170, 180, 200))
    im.save(dst)


def persona_card_v(ep: dict, dst: Path) -> None:
    im = _bg(VW, VH, BG_TALL, 0.5)
    d = ImageDraw.Draw(im)
    _mark(im, (60, 160), 120)
    d.text((300, 190), ep.get("card_label") or "谁在用智聊", font=ImageFont.truetype(FONT_R, 44), fill=(180, 195, 220))
    y = 560
    for line in _wrap(ep["title"], 9).split("\\N"):
        _center(d, y, line, ImageFont.truetype(FONT_B, 104), (255, 224, 130), w=VW)
        y += 140
    _center(d, y + 40, f"{ep['persona']} · {ep['job']}", ImageFont.truetype(FONT_B, 60), w=VW)
    _center(d, y + 130, ep["company"], ImageFont.truetype(FONT_R, 44), (200, 210, 230), w=VW)
    _center(d, y + 260, ep["trigger"], ImageFont.truetype(FONT_R, 38), (200, 210, 230), w=VW)
    _center(d, VH - 360, f"{ep['platform'].replace('+', ' + ').title()} · {ep['lang_pair']}", ImageFont.truetype(FONT_R, 38), (140, 200, 255), w=VW)
    d.text((40, VH - 70), "bd2026.cc", font=ImageFont.truetype(FONT_R, 30), fill=(150, 160, 180))
    im.save(dst)


def end_card_v(ep: dict, dst: Path) -> None:
    import qrcode
    im = _bg(VW, VH, BG_TALL, 0.3)
    d = ImageDraw.Draw(im)
    _mark(im, ((VW - 200) // 2, 220), 200)
    _center(d, 480, ep["hook"], ImageFont.truetype(FONT_B, 64), w=VW)
    _center(d, 580, f"{ep['persona']}的一天，智聊在场", ImageFont.truetype(FONT_R, 40), (200, 210, 230), w=VW)
    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data(f"https://bd2026.cc/download/chatx?utm_source=video&{ep['utm']}")
    qr.make(fit=True)
    im.paste(qr.make_image(fill_color="black", back_color="white").convert("RGB").resize((420, 420)), ((VW - 420) // 2, 760))
    _center(d, 1260, "智聊 ChatX · 免费开始", ImageFont.truetype(FONT_B, 56), w=VW)
    _center(d, 1350, "标准翻译永久免费 · 搜 bd2026.cc", ImageFont.truetype(FONT_R, 40), (220, 225, 235), w=VW)
    _center(d, 1560, "歌声与念白由 幻声 VoiceX 生成 · 无界科技", ImageFont.truetype(FONT_R, 30), (170, 180, 200), w=VW)
    im.save(dst)


def vertical_top(ep: dict, dst: Path) -> None:
    im = _bg(VW, 360, BG_TALL, 0.55)
    d = ImageDraw.Draw(im)
    _mark(im, (40, 34), 70)
    d.text((130, 40), ep.get("card_label") or "谁在用智聊", font=ImageFont.truetype(FONT_R, 34), fill=(180, 195, 220))
    d.text((40, 130), f"{ep['persona']} · {ep['job']}", font=ImageFont.truetype(FONT_B, 56), fill=(255, 224, 130))
    d.text((40, 215), ep["title"], font=ImageFont.truetype(FONT_B, 48), fill="white")
    d.text((40, 290), f"{ep['company']} · {ep['lang_pair']}", font=ImageFont.truetype(FONT_R, 32), fill=(160, 200, 255))
    im.save(dst)


def vertical_bottom(ep: dict, dst: Path) -> None:
    im = _bg(VW, 480, BG_TALL, 0.7)
    d = ImageDraw.Draw(im)
    d.text((40, 400), "免费开始 · 搜 bd2026.cc · 歌声与念白由幻声生成", font=ImageFont.truetype(FONT_R, 28), fill=(170, 185, 210))
    im.save(dst)


# ── 字幕 ─────────────────────────────────────────────────────────────────────

def _ts(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    return f"{cs//360000}:{cs%360000//6000:02d}:{cs%6000//100:02d}.{cs%100:02d}"


def _wrap(s: str, n: int) -> str:
    s = s.strip()
    if len(s) <= n:
        return s
    # 优先在标点处折
    for i in range(min(n, len(s) - 1), max(6, n - 10), -1):
        if s[i - 1] in "，。；！？、：":
            return s[:i] + "\\N" + s[i:]
    return s[:n] + "\\N" + s[n:]


def write_ass(lines: list[tuple[float, float, str]], dst: Path, *, playres=(W, H), fontsize=54, margin_v=70, wrap_n=22) -> None:
    ev = [f"Dialogue: 0,{_ts(a)},{_ts(b)},Narr,,0,0,0,,{_wrap(t, wrap_n)}" for a, b, t in lines]
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {playres[0]}\nPlayResY: {playres[1]}\nWrapStyle: 2\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Narr,Microsoft YaHei,{fontsize},&H00FFFFFF,&H00FFFFFF,&HA0000000,&H80000000,-1,0,0,0,100,100,1,0,1,4,1,2,60,60,{margin_v},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    dst.write_text(head + "\n".join(ev) + "\n", encoding="utf-8")


# ── 念白 ─────────────────────────────────────────────────────────────────────

def tts(text: str, voice: str, dst_mp3: Path, rate: str = "-2%") -> float:
    import edge_tts

    async def go():
        await edge_tts.Communicate(text, voice, rate=rate).save(str(dst_mp3))
    asyncio.run(go())
    b = dst_mp3.read_bytes()[:3]
    if not (b[:3] == b"ID3" or b[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
        raise SystemExit(f"[FAIL] edge-tts 产物非 MP3: {dst_mp3.name} magic={b!r}")
    return ffdur(dst_mp3)


# ── 音乐 take ──────────────────────────────────────────────────────────────

def best_take(prefix: str) -> tuple[Path | None, dict]:
    cands = []
    for js in OUT.glob(f"{prefix}_full_s*.json"):
        m = json.loads(js.read_text(encoding="utf-8"))
        wav = js.with_suffix(".wav")
        if not wav.exists() or wav.read_bytes()[:4] != b"RIFF":
            continue
        hits = m.get("line_hits") or []
        mean = sum(hits) / len(hits) if hits else 0.0
        cands.append((mean, wav, m))
    if not cands:
        return None, {}
    cands.sort(key=lambda x: x[0], reverse=True)
    return cands[0][1], cands[0][2]


# ── 主流程 ───────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("episode")
    ap.add_argument("--no-vertical", action="store_true")
    ap.add_argument("--modes", default="h,v", help="只渲染哪些模式：h / v / h,v")
    ap.add_argument("--voice", default="")
    a = ap.parse_args()
    ep = next(e for e in SC["episodes"] if e["id"] == a.episode)
    cur = json.loads((ROOT / "curriculum.json").read_text(encoding="utf-8"))
    cep = next(e for e in cur["episodes"] if e["id"] == a.episode)
    epdir = OUT / ep["id"]
    cards = epdir / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    voice = a.voice or ep.get("narr_voice") or NARR_VOICES.get(ep["id"], NARR_VOICE_DEFAULT)

    # 1) 读各段标记，铺时间线（连续播放，不循环）
    segs: list[tuple[Path, float, float, str]] = []   # (clip, src_a, src_b, zoom)
    narrs: list[tuple[float, str]] = []                # (timeline_t, text)
    voices: list[tuple[float, Path]] = []              # (timeline_t, audio)
    incomings: list[float] = []
    tl = HOOK_D
    for c in cep["footage_actions"]:
        src_webm = epdir / f"footage_{c['clip']}.webm"
        js = src_webm.with_suffix(".json")
        if not src_webm.exists() or not js.exists():
            raise SystemExit(f"[FAIL] 缺 {src_webm.name}/.json —— 先 record_persona.py {ep['id']}")
        m = json.loads(js.read_text(encoding="utf-8"))
        markers = m["markers"]
        if not markers:
            raise SystemExit(f"[FAIL] {src_webm.name} 无标记")
        start = float(m.get("content_offset") or m["ready_offset"])
        end = float(m.get("end_t") or (m["duration"] - 0.3))
        # Playwright VP8 墙钟与容器时基会被拉长（P6：end_t 147s 的「AI 拟稿」实际在文件 175s；按标记切会切到空会话）。
        # 先把画面压回墙钟（conform_to_wallclock）；仍明显更长（回压未触发的边界情形）才按比例把标记映射到视频时基。
        webm = conform_to_wallclock(ensure_seekable(src_webm), end)
        real = ffdur(webm)
        if real > end + 8 and end > 10:
            scale = real / end
            print(f"  ~ {webm.name} 时基 {end:.1f}s → {real:.1f}s ×{scale:.3f}")
            start *= scale
            for k in markers:
                k["t0"] = round(float(k["t0"]) * scale, 3)
                k["t1"] = round(float(k["t1"]) * scale, 3)
                if k.get("audio_at") is not None:
                    k["audio_at"] = round(float(k["audio_at"]) * scale, 3)
            end = real - 0.3
        if end > real:
            end = real - 0.2
        if end - start < 6:
            raise SystemExit(f"[FAIL] {webm.name} 有效镜头仅 {end-start:.1f}s")
        zoom_state = ""
        cur_t = start
        removed = 0.0          # 卡顿剪掉的秒数（等待超时的动作：留头留尾，中间剪掉）

        def tl_of(t: float) -> float:
            return tl + (t - start) - removed

        for k in markers:
            t0, t1 = max(start, float(k["t0"])), min(end, float(k["t1"]))
            if t1 <= cur_t:
                continue
            z = k.get("zoom") or ""
            if k["op"] in OP_ZOOM and z == "zoom":
                z = OP_ZOOM[k["op"]]
            if k["op"] == "narr":
                z = zoom_state
            else:
                zoom_state = z
            if t0 > cur_t:
                segs.append((webm, cur_t, t0, zoom_state if k["op"] == "narr" else ""))
            a0 = max(cur_t, t0)
            if k["op"] == "narr" and k.get("narr"):
                narrs.append((tl_of(float(k["t0"])), k["narr"]))
            stall_s, keep_head, keep_tail = STALL_BY_OP.get(k["op"], (STALL_S, KEEP_HEAD, KEEP_TAIL))
            if k["op"] != "narr" and k["op"] not in NO_STALL_OPS and (t1 - a0) > stall_s:
                # 动作等了太久（如发送等待超时）：保留前 keep_head 与后 keep_tail，中段剪掉
                segs.append((webm, a0, a0 + keep_head, z))
                segs.append((webm, t1 - keep_tail, t1, z))
                cut = (t1 - a0) - keep_head - keep_tail
                removed += cut
                print(f"  ~ {webm.name} {k['op']} 卡顿 {t1-a0:.1f}s → 剪掉中段 {cut:.1f}s")
            else:
                segs.append((webm, a0, t1, z))
            if k["op"] == "incoming":
                # 来信 ding 对齐行长出来的时刻（t1）；须在卡顿剪切计入 removed 之后算，否则剪掉多少 ding 就晚多少
                incomings.append(tl_of(float(k["t1"])) - 1.0)
            if k.get("audio") and k.get("audio_at") is not None:
                ap_ = ROOT / k["audio"]
                if ap_.exists():
                    voices.append((tl_of(float(k["audio_at"])), ap_))
            cur_t = t1
        if cur_t < end:
            segs.append((webm, cur_t, end, zoom_state))
        tl += (end - start) - removed
    footage_d = tl - HOOK_D
    total = HOOK_D + footage_d + END_D
    # 输出时间轴上的景别区间（concat 顺序即输出顺序）：发布版按这个定「会话列表在画面哪里」做脱敏
    t_cursor = HOOK_D
    zoom_timeline: list[tuple[float, float, str]] = []
    for (_w, _sa, _sb, _z) in segs:
        d = _sb - _sa
        zoom_timeline.append((round(t_cursor, 3), round(t_cursor + d, 3), _z))
        t_cursor += d
    print(f"  hook {HOOK_D}s | footage {footage_d:.1f}s ({len(segs)} 段) | end {END_D}s | total {total:.1f}s | narr {len(narrs)} | voice {len(voices)}")

    # 2) 念白 TTS（超过到下一条的间隔就加速重合成）
    narr_audio: list[tuple[float, Path, float, str]] = []
    for i, (t, text) in enumerate(narrs):
        mp3 = cards / f"narr_{i:02d}.mp3"
        d = tts(text, voice, mp3)
        gap = (narrs[i + 1][0] - t - 0.4) if i + 1 < len(narrs) else (total - END_D - t)
        if d > gap and gap > 1.5:
            pct = min(30, int((d / gap - 1) * 100) + 4)
            d = tts(text, voice, mp3, rate=f"+{pct}%")
            print(f"    narr{i} 过长 → +{pct}% → {d:.1f}s (gap {gap:.1f})")
        narr_audio.append((t, mp3, d, text))

    # 3) 卡片
    persona_card(ep, cards / "persona.png")
    badge_overlay(ep, cards / "badge.png")
    end_card(ep, cards / "end.png")

    # 4) 音乐
    # chorus_take：教学片（F1）不另写歌，复用同人物场景片的副歌 take（P1 阿杰「我睁眼单已回」）
    chorus, cm = best_take(ep.get("chorus_take") or f"{ep['id']}_chorus")
    bed, _ = best_take("P_bed")
    if chorus is None:
        print("  ~ WARN 无副歌 take，先用底床/静音拼")
    if bed is None:
        print("  ~ WARN 无底床 take")
    print(f"  chorus={chorus.name if chorus else None} mean_hits={round(sum(cm.get('line_hits') or [0])/max(1,len(cm.get('line_hits') or [1])),2) if cm else '-'} bed={bed.name if bed else None}")

    # 5) 双模渲染：16:9 与 9:16 用同一份段表与音频链，只换「景别规则」和卡片
    chorus_txt = [l.strip() for l in Path(ROOT / ep["chorus"]).read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("[")]
    hook_lines = [(0.3, HOOK_D - 0.1, " / ".join(chorus_txt[:2]))]
    narr_lines = [(t, t + d, text) for (t, _, d, text) in narr_audio]

    def audio_chain(idx: int, inputs: list[str], fc: list[str]) -> tuple[int, list[str]]:
        a_labels = []
        if chorus is not None:
            inputs += ["-i", str(chorus)]
            cd = min(ffdur(chorus), 14.0)
            fc.append(f"[{idx}:a]atrim=duration={cd:.2f},asetpts=PTS-STARTPTS,volume=0.85,afade=t=out:st={max(0.5, cd-4.0):.2f}:d=4.0[ach0];")
            a_labels.append("[ach0]")
            idx += 1
            inputs += ["-i", str(chorus)]
            fc.append(f"[{idx}:a]atrim=duration={END_D+0.5:.2f},asetpts=PTS-STARTPTS,volume=0.8,afade=t=in:st=0:d=0.8,afade=t=out:st={END_D-1.2:.2f}:d=1.2,adelay={int((total-END_D)*1000)}|{int((total-END_D)*1000)}[ach1];")
            a_labels.append("[ach1]")
            idx += 1
        if bed is not None:
            inputs += ["-stream_loop", "-1", "-i", str(bed)]
            bed_start = HOOK_D + 6.0
            fc.append(f"[{idx}:a]atrim=duration={footage_d:.2f},asetpts=PTS-STARTPTS,volume=0.13,afade=t=in:st=0:d=3,afade=t=out:st={max(0.5, footage_d-8.0):.2f}:d=3,adelay={int(bed_start*1000)}|{int(bed_start*1000)}[abed];")
            a_labels.append("[abed]")
            idx += 1
        for i, (t, mp3, d, _) in enumerate(narr_audio):
            inputs += ["-i", str(mp3)]
            fc.append(f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume=1.0,adelay={int(t*1000)}|{int(t*1000)}[an{i}];")
            a_labels.append(f"[an{i}]")
            idx += 1
        for i, (t, ap_) in enumerate(voices):
            inputs += ["-i", str(ap_)]
            fc.append(f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume=1.0,adelay={int(t*1000)}|{int(t*1000)}[avc{i}];")
            a_labels.append(f"[avc{i}]")
            idx += 1
        for i, t in enumerate(incomings):
            fc.append(f"sine=frequency=1320:duration=0.12,aformat=sample_rates=48000:channel_layouts=stereo,volume=0.35,afade=t=out:st=0.02:d=0.1,adelay={int(max(0,t)*1000)}|{int(max(0,t)*1000)}[ding{i}];")
            a_labels.append(f"[ding{i}]")
        if a_labels:
            fc.append("".join(a_labels) + f"amix=inputs={len(a_labels)}:duration=longest:normalize=0,atrim=duration={total:.2f},aformat=sample_rates=48000:channel_layouts=stereo[aout]")
        else:
            fc.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={total:.2f}[aout]")
        return idx, a_labels

    def render(mode: str) -> Path:
        """mode=h：1920×1080，zoom=1280×720 取景框放大 1.5×；mode=v：1080×1920 三段布局，
        中段 1080×1080 直接从原录屏 1:1 裁聊天栏（不放大不失真），全景段缩放居中。"""
        if mode == "h":
            ow, oh = W, H
            hook_png, end_png = cards / "persona.png", cards / "end.png"
        else:
            ow, oh = VW, VH
            hook_png, end_png = cards / "persona_v.png", cards / "end_v.png"
        inputs: list[str] = ["-loop", "1", "-t", f"{HOOK_D}", "-i", str(hook_png)]
        fc = [f"[0:v]scale={ow}:{oh},setsar=1,fps=30,format=yuv420p,trim=duration={HOOK_D},setpts=PTS-STARTPTS[v0];"]
        labels = ["[v0]"]
        idx = 1
        for (webm, sa, sb, z) in segs:
            inputs += ["-ss", f"{sa:.3f}", "-t", f"{sb-sa:.3f}", "-i", str(webm)]
            if mode == "h":
                if z in ZOOM:
                    x, y = ZOOM[z]
                    vf = f"crop=1280:720:{x}:{y},scale={W}:{H}:flags=lanczos"
                else:
                    vf = f"scale={W}:{H}"
            else:
                # 竖屏：没标景别的段也直裁聊天栏（整屏缩到 1080 宽字太小、看不清）
                x, y = ZOOM_V.get(z, ZOOM_V["zoom"])
                vf = f"crop=1080:1080:{x}:{y}"
            fc.append(f"[{idx}:v]{vf},setsar=1,fps=30,format=yuv420p,setpts=PTS-STARTPTS[v{idx}];")
            labels.append(f"[v{idx}]")
            idx += 1
        inputs += ["-loop", "1", "-t", f"{END_D}", "-i", str(end_png)]
        fc.append(f"[{idx}:v]scale={ow}:{oh},setsar=1,fps=30,format=yuv420p,trim=duration={END_D},setpts=PTS-STARTPTS[v{idx}];")
        end_label = f"[v{idx}]"
        idx += 1
        if mode == "h":
            inputs += ["-loop", "1", "-t", f"{total:.2f}", "-i", str(cards / "badge.png")]
            badge_idx = idx
            idx += 1
            fc.append("".join(labels) + end_label + f"concat=n={len(labels)+1}:v=1:a=0[vcat];")
            fc.append(f"[{badge_idx}:v]scale={W}:{H},format=rgba[bdg];")
            fc.append(f"[vcat][bdg]overlay=0:0:enable='between(t,{HOOK_D},{total-END_D:.2f})',format=yuv420p[vout];")
        else:
            inputs += ["-loop", "1", "-t", f"{footage_d:.2f}", "-i", str(cards / "v_top.png")]
            top_idx = idx
            idx += 1
            inputs += ["-loop", "1", "-t", f"{footage_d:.2f}", "-i", str(cards / "v_bottom.png")]
            bot_idx = idx
            idx += 1
            fc.append("".join(labels[1:]) + f"concat=n={len(labels)-1}:v=1:a=0[mid];")
            fc.append(f"[{top_idx}:v]scale={VW}:360,setsar=1,fps=30,format=yuv420p[top];[{bot_idx}:v]scale={VW}:480,setsar=1,fps=30,format=yuv420p[bot];")
            fc.append("[top][mid]vstack=inputs=2[tm];[tm][bot]vstack=inputs=2[stk];")
            fc.append(f"[v0][stk]{end_label}concat=n=3:v=1:a=0,format=yuv420p[vout];")
        idx, _ = audio_chain(idx, inputs, fc)
        nosub = epdir / f"{ep['id']}_persona_{mode}_nosub.mp4"
        fc_file = cards / f"filter_{mode}.txt"
        fc_file.write_text("\n".join(fc), encoding="utf-8")
        run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex_script", str(fc_file), "-map", "[vout]", "-map", "[aout]",
             "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
             "-t", f"{total:.2f}", "-movflags", "+faststart", str(nosub)])
        ass = cards / f"narr_{mode}.ass"
        if mode == "h":
            write_ass(hook_lines + narr_lines, ass, playres=(W, H), fontsize=56, margin_v=60, wrap_n=24)
            final = epdir / f"{ep['id']}_persona.mp4"
        else:
            write_ass(hook_lines + narr_lines, ass, playres=(VW, VH), fontsize=54, margin_v=170, wrap_n=16)
            final = epdir / f"{ep['id']}_persona_v.mp4"
        ass_esc = str(ass).replace("\\", "/").replace(":", "\\:")
        run(["ffmpeg", "-y", "-v", "error", "-i", str(nosub), "-vf", f"subtitles='{ass_esc}'", "-c:v", "libx264", "-crf", "19",
             "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(final)])
        nosub.unlink(missing_ok=True)
        info = ffinfo(final)
        kinds = {s["codec_type"] for s in info["streams"]}
        d = float(info["format"]["duration"])
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        if kinds != {"video", "audio"} or final.read_bytes()[4:8] != b"ftyp" or (v["width"], v["height"]) != (ow, oh):
            raise SystemExit(f"[FAIL] 成片验收 {final.name} streams={kinds} {v.get('width')}x{v.get('height')}")
        print(f"  [OK] {final.name}: {d:.1f}s {ow}x{oh} 双流 {final.stat().st_size//1024}KB")
        for t in (1.5, HOOK_D + 3, *(x + 1.2 for x in incomings[:1]), total * 0.5, total - END_D - 3, total - 2):
            run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(final), "-frames:v", "1", str(epdir / f"{final.stem}_t{int(t)}.png")], check=False)
        return final

    modes = {m.strip() for m in a.modes.split(",") if m.strip()}
    if "h" in modes:
        final = render("h")
    else:
        final = epdir / f"{ep['id']}_persona.mp4"
    dur = ffdur(final) if final.exists() else total
    if not a.no_vertical and "v" in modes:
        persona_card_v(ep, cards / "persona_v.png")
        end_card_v(ep, cards / "end_v.png")
        vertical_top(ep, cards / "v_top.png")
        vertical_bottom(ep, cards / "v_bottom.png")
        render("v")

    meta = {"episode": ep["id"], "total": round(dur, 2), "hook": HOOK_D, "end": END_D, "segments": len(segs),
            "zoom_timeline": [[a, b, z] for a, b, z in zoom_timeline],
            "narr": [(round(t, 2), round(d, 2), text) for (t, _, d, text) in narr_audio],
            "voices": [(round(t, 2), str(p.relative_to(ROOT))) for t, p in voices],
            "chorus": (str(chorus.relative_to(ROOT)) if chorus else None), "bed": (str(bed.relative_to(ROOT)) if bed else None),
            "narr_voice": voice}
    (epdir / f"{ep['id']}_persona.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    raise SystemExit(main())
