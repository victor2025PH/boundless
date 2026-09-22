#!/usr/bin/env python3
"""营销歌产线 · 第二段：9:16 竖屏拼装（方案 §4.1 结构模板）。

  python make_vertical.py V1 --take 423 [--footage ..\video_sample_20260827\footage_zh.webm]
                            [--hook "凌晨三点，一条阿拉伯语询盘进来。"] [--footage-offset 3]

时间线（默认 2 + 15 + 5 = 22 s）：
  0~2 s   痛点钩子卡（品牌 9:16 底 + 大字），歌声压低
  2~17 s  hub 15 秒高光 MV 全幅（口型演唱）+ 下三分之一录屏画中画 + 逐句歌词字幕（ASS，按 take 的 ASR 时间轴）
  17~22 s CTA 尾卡：免费开始 · 官网 · 二维码 · 「歌声与口型由 幻声·幻境 生成」；歌声淡出
产物：out/{ID}_h{take}_vertical.mp4（1080×1920/30fps/H.264+AAC），落盘后 ffprobe 双流验收 + 抽 3 帧目检 PNG。
audience=private 的条目：水印/CTA 改隔离域占位（不写 bd2026.cc）。
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
BG = ROOT.parent / "huanyan-brand-bg" / "cosmos-tall.jpg"          # 干净宇宙底（品牌海报 png 自带大字会撞文案）
MARK = ROOT.parent / "huanyan-brand-bg" / "boundless-mark-512.png"
FONT_B = "C:/Windows/Fonts/msyhbd.ttc"
FONT_R = "C:/Windows/Fonts/msyh.ttc"
W, H = 1080, 1920

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    print("  $", " ".join(str(c) for c in cmd)[:220])
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def ffprobe(path: Path) -> dict:
    r = run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    if r.returncode != 0:
        raise SystemExit(f"[FAIL] ffprobe {path.name}: {r.stderr[:200]}")
    return json.loads(r.stdout)


# ── 卡片 ─────────────────────────────────────────────────────────────────────

def _bg(*, mark_y: int | None = 150, dim: float = 0.35) -> Image.Image:
    """cover-crop 到 1080×1920 + 压暗 + 顶部品牌 mark。"""
    im = Image.open(BG).convert("RGB")
    s = max(W / im.width, H / im.height)
    im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
    x0, y0 = (im.width - W) // 2, (im.height - H) // 2
    im = im.crop((x0, y0, x0 + W, y0 + H))
    if dim:
        im = Image.blend(im, Image.new("RGB", (W, H), (5, 6, 15)), dim)
    if mark_y is not None and MARK.exists():
        mk = Image.open(MARK).convert("RGBA").resize((220, 220), Image.LANCZOS)
        im.paste(mk, ((W - 220) // 2, mark_y), mk)
    return im


def _center_text(d: ImageDraw.ImageDraw, y: int, text: str, font, fill=(255, 255, 255)) -> int:
    box = d.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    d.text(((W - tw) // 2 - box[0], y), text, font=font, fill=fill)
    return y + th


def hook_card(lines: list[str], dst: Path) -> None:
    im = _bg()
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype(FONT_B, 84)
    y = 700
    for ln in lines:
        y = _center_text(d, y, ln, f) + 36
    im.save(dst)


def end_card(*, private: bool, site: str, dst: Path) -> None:
    import qrcode
    im = _bg()
    d = ImageDraw.Draw(im)
    f1 = ImageFont.truetype(FONT_B, 110)
    f2 = ImageFont.truetype(FONT_B, 64)
    f3 = ImageFont.truetype(FONT_R, 40)
    y = _center_text(d, 380, "智聊 ChatX", f1) + 30
    y = _center_text(d, y, "免费开始 · 标准翻译永久免费", f2) + 60
    if not private:
        url = f"https://{site}/download/chatx?utm_source=video"
        qr = qrcode.QRCode(box_size=12, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        qim = qr.make_image(fill_color="black", back_color="white").convert("RGB").resize((520, 520))
        im.paste(qim, ((W - 520) // 2, y + 20))
        y += 520 + 70
        y = _center_text(d, y, f"搜 {site}", f2) + 40
    else:
        y = _center_text(d, y + 40, "私域专用 · 联系代理获取试用", f2) + 40
    _center_text(d, H - 260, "歌声与口型由 幻声 VoiceX · 幻境 STUDIO 生成", f3, fill=(200, 210, 230))
    _center_text(d, H - 190, "无界科技 BOUNDLESS · 让沟通，无界", f3, fill=(200, 210, 230))
    im.save(dst)


# ── 歌词字幕 ─────────────────────────────────────────────────────────────────

def _ass_ts(t: float) -> str:
    cs = int(round(t * 100))
    return f"{cs//360000}:{cs%360000//6000:02d}:{cs%6000//100:02d}.{cs%100:02d}"


def lyric_ass(take: dict, sent_lyrics: str, mv: dict, offset: float, dst: Path) -> int:
    """取 MV 窗口 [start_s, start_s+seconds] 内的 ASR 分段当时间轴，按序贴上副歌歌词行。"""
    start, secs = float(mv["start_s"]), float(mv["seconds"])
    segs = [s for s in (take.get("asr") or []) if s["start"] >= start - 1.0 and s["start"] < start + secs - 0.5]
    # 歌词行：优先取 [chorus] 段
    chorus, cur = [], None
    for l in sent_lyrics.splitlines():
        s = l.strip()
        if s.startswith("["):
            cur = s.lower()
            continue
        if s and cur == "[chorus]":
            chorus.append(s)
    lines = chorus or [l for l in sent_lyrics.splitlines() if l.strip() and not l.startswith("[")]
    n = min(len(lines), max(1, len(segs))) if segs else len(lines)
    events = []
    for i in range(n):
        if segs and i < len(segs):
            t0 = max(start, segs[i]["start"]) - start + offset
            t1 = (segs[i + 1]["start"] if i + 1 < len(segs) else min(segs[i]["end"] + 0.6, start + secs)) - start + offset
        else:  # 无 ASR：均分
            t0 = offset + secs * i / n
            t1 = offset + secs * (i + 1) / n
        t1 = min(t1, offset + secs)
        if t1 - t0 < 0.4:
            continue
        events.append(f"Dialogue: 0,{_ass_ts(t0)},{_ass_ts(t1)},Lyric,,0,0,0,,{lines[i]}")
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        "Style: Lyric,Microsoft YaHei,78,&H00FFFFFF,&H000000FF,&H9A000000,&H64000000,-1,0,0,0,100,100,2,0,1,4,2,8,60,60,300,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    dst.write_text(head + "\n".join(events) + "\n", encoding="utf-8")
    return len(events)


# ── 拼装 ─────────────────────────────────────────────────────────────────────

def assemble(item: dict, take: dict, mv: dict, *, mv_path: Path, wav: Path, footage: Path,
             footage_offset: float, hook_lines: list[str], site: str, out: Path) -> None:
    private = item["audience"] == "private"
    hook_d, end_d = 2.0, 5.0
    mv_d = float(mv["seconds"])
    total = hook_d + mv_d + end_d
    cards = OUT / f"{item['id']}_cards"
    cards.mkdir(exist_ok=True)
    hook_png, end_png, ass = cards / "hook.png", cards / "end.png", cards / "lyric.ass"
    hook_card(hook_lines, hook_png)
    end_card(private=private, site=site, dst=end_png)
    n_ev = lyric_ass(take, (OUT / f"{item['id']}_{take.get('cut','short')}_s{take['seed']}.lyrics.txt").read_text(encoding="utf-8"),
                     mv, hook_d, ass)
    print(f"  歌词字幕 {n_ev} 条")
    ass_esc = str(ass).replace("\\", "/").replace(":", "\\:")
    wm = "" if private else (f",drawtext=text='{site}':fontfile='C\\:/Windows/Fonts/arial.ttf':fontsize=34:"
                             f"fontcolor=white@0.6:x=w-tw-40:y=90")
    a_start = max(0.0, float(mv["start_s"]) - hook_d)
    fc = (
        f"[0:v]scale={W}:{H},setsar=1,fps=30,format=yuv420p,trim=duration={hook_d},setpts=PTS-STARTPTS[hook];"
        f"[1:v]scale=-2:{H},crop={W}:{H},setsar=1,fps=30,format=yuv420p,setpts=PTS-STARTPTS[mvv];"
        f"[2:v]trim=start={footage_offset}:duration={mv_d},setpts=PTS-STARTPTS,scale=900:-2,fps=30,"
        f"pad=iw+12:ih+12:6:6:color=white@0.85,format=yuv420p[pip];"
        f"[mvv][pip]overlay=x=(W-w)/2:y=1290:shortest=1[mvp];"
        f"[mvp]subtitles='{ass_esc}'{wm},setsar=1[mvs];"
        f"[3:v]scale={W}:{H},setsar=1,fps=30,format=yuv420p,trim=duration={end_d},setpts=PTS-STARTPTS[end];"
        f"[hook][mvs][end]concat=n=3:v=1:a=0[vcat];"
        f"[vcat]fade=t=in:st=0:d=0.5,fade=t=out:st={total-0.8:.2f}:d=0.8[v];"
        f"[4:a]atrim=start={a_start:.2f}:duration={total:.2f},asetpts=PTS-STARTPTS,"
        f"volume='if(lt(t,{hook_d}),0.22,1)':eval=frame,afade=t=out:st={total-1.6:.2f}:d=1.6[a]"
    )
    r = run([
        "ffmpeg", "-y", "-loop", "1", "-t", f"{hook_d}", "-i", str(hook_png),
        "-i", str(mv_path), "-i", str(footage),
        "-loop", "1", "-t", f"{end_d}", "-i", str(end_png),
        "-i", str(wav),
        "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-t", f"{total:.2f}", "-movflags", "+faststart", str(out),
    ])
    if r.returncode != 0:
        raise SystemExit(f"[FAIL] ffmpeg:\n{r.stderr[-1500:]}")
    info = ffprobe(out)
    kinds = {s["codec_type"] for s in info["streams"]}
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    d = float(info["format"]["duration"])
    head = out.read_bytes()[:12]
    if head[4:8] != b"ftyp" or kinds != {"video", "audio"} or abs(d - total) > 1.0 \
            or (v["width"], v["height"]) != (W, H) or out.stat().st_size < 800_000:
        raise SystemExit(f"[FAIL] 验收不过 magic={head.hex()} streams={kinds} dur={d} size={v['width']}x{v['height']}")
    print(f"  [OK] {out.name}: {d:.1f}s {v['width']}x{v['height']} 双流 {out.stat().st_size//1024}KB")
    for t in (1.0, hook_d + 6.0, total - 2.5):
        png = OUT / f"{out.stem}_t{int(t)}.png"
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t}", "-i", str(out), "-frames:v", "1", str(png)])
        print(f"  抽帧 {png.name}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("item")
    ap.add_argument("--take", type=int, required=True, help="history_id（out/ 里的 *_mv*.mp4 与 take json）")
    ap.add_argument("--footage", default=str(ROOT.parent / "video_sample_20260827" / "footage_zh.webm"))
    ap.add_argument("--footage-offset", type=float, default=15.0,
                    help="录屏起点秒；20260827 官网录屏 0~14s 是开场遮罩，15s 起才是产品页")
    ap.add_argument("--hook", help="钩子卡文案，用 | 分行")
    ap.add_argument("--site", default="bd2026.cc")
    ap.add_argument("--profile", default="xiaojie_brand_A", help="MV 用的数字人档（决定选哪支 *_mv*_<profile>.mp4）")
    a = ap.parse_args()
    reg = json.loads((ROOT / "registry.json").read_text(encoding="utf-8"))
    item = next(i for i in reg["items"] if i["id"] == a.item)
    mv_path = next(OUT.glob(f"{a.item}_h{a.take}_mv*_{a.profile}.mp4"), None)
    mv_json = next(OUT.glob(f"{a.item}_h{a.take}_mv*_{a.profile}.mv.json"), None)
    if not mv_path or not mv_json:
        raise SystemExit(f"[FAIL] 缺 MV 产物（先 make_song.py {a.item} --mv {a.take}）")
    take_json = next((p for p in OUT.glob(f"{a.item}_*_s*.json")
                      if json.loads(p.read_text(encoding='utf-8')).get("history_id") == a.take), None)
    if not take_json:
        raise SystemExit("[FAIL] 找不到该 history_id 的 take json")
    take = json.loads(take_json.read_text(encoding="utf-8"))
    wav = take_json.with_suffix(".wav")
    mv = json.loads(mv_json.read_text(encoding="utf-8"))
    hook = (a.hook or item.get("hook") or item["title"]).split("|")
    out = OUT / f"{a.item}_h{a.take}_{a.profile}_vertical.mp4"
    assemble(item, take, mv, mv_path=mv_path, wav=wav, footage=Path(a.footage),
             footage_offset=a.footage_offset, hook_lines=hook, site=a.site, out=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
