#!/usr/bin/env python3
"""横屏教程 T1「三分钟装好智聊」（方案 §4.2 + 附录 B 分镜；B 形态：jingle 片头/片尾 + 主体念白）。

  python make_t1.py vo        # edge-tts 念白（中性播音声）+ 验证
  python make_t1.py record    # playwright 录生产站 /download/chatx 与 /pricing（1920×1080）
  python make_t1.py cards     # 卡片：标题 / 要点 / SmartScreen / 向导 / 主界面（真截图·联系人区打码）/ 6U / 尾卡
  python make_t1.py assemble  # 分段成片 → concat → ffprobe 验收 + 抽帧
  python make_t1.py all

真录屏只有官网部分；安装器 / SmartScreen / 首启向导三步暂用**示意卡**（录真装机需一台干净 Windows，见 README）。
jingle 取 out/JINGLE_full_s*.wav（make_song.py JINGLE --cut full --duration 30）。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out" / "T1"
OUT.mkdir(parents=True, exist_ok=True)
SITE = os.environ.get("SAMPLE_SITE", "https://bd2026.cc")
BG = ROOT.parent / "huanyan-brand-bg" / "cosmos-wide.jpg"
MARK = ROOT.parent / "huanyan-brand-bg" / "boundless-mark-512.png"
SHELL_SHOT = ROOT.parent.parent / "tmp_shell_screen.png"
FONT_B, FONT_R = "C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc"
W, H = 1920, 1080
VOICE = "zh-CN-XiaoxiaoNeural"

# 念白＝字幕单一来源（附录 B；防编造：无业绩数字；版本/体积不念死数）
SEGS = [
    ("s1_learn", "这一集三分钟：下载、安装、首启向导，装好就能开始聊。"),
    ("s2_download", "打开官网 bd2026.cc，进下载页，点免费下载。安装包大约五百兆，页面上有 SHA-256 校验值，介意的话可以核对一下。"),
    ("s3_smartscreen", "第一次运行，Windows 可能提示未知发布者。点更多信息，再点仍要运行，就可以继续安装了。"),
    ("s4_wizard", "安装完成自动打开首启向导：选语言，填注册时拿到的 Token。标准翻译永久免费，注册就送额度。"),
    ("s5_tour", "主界面四个地方记住就行：左边是统一收件箱，上面是各平台账号页签，右边是 AI 副驾，右下角的小球随时可以问它怎么用。"),
    ("s6_newbie", "新人七十二小时内有一个双倍到账的新人包，不急，先把渠道接上试一试再说。"),
    ("s7_next", "下一集：接入 Telegram 和 WhatsApp。有问题，点小球问小智，或者到官网找我们。"),
]


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    print("  $", " ".join(str(c) for c in cmd)[:200])
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def ffprobe(path: Path) -> dict:
    r = run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    if r.returncode != 0:
        raise SystemExit(f"[FAIL] ffprobe {path.name}: {r.stderr[:200]}")
    return json.loads(r.stdout)


def dur(path: Path) -> float:
    return float(ffprobe(path)["format"]["duration"])


def check_mp3(path: Path) -> float:
    head = path.read_bytes()[:3]
    if not (head == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
        raise SystemExit(f"[FAIL] {path.name} 非 MP3（{head.hex()}）")
    d = dur(path)
    print(f"  [OK] {path.name} {d:.1f}s")
    return d


# ── 1. 念白 ──────────────────────────────────────────────────────────────────

def vo() -> None:
    import edge_tts

    async def gen(key: str, text: str) -> None:
        out = OUT / f"vo_{key}.mp3"
        await edge_tts.Communicate(text, VOICE, rate="-2%").save(str(out))
        check_mp3(out)

    for k, t in SEGS:
        asyncio.run(gen(k, t))


# ── 2. 录屏 ──────────────────────────────────────────────────────────────────

def record() -> None:
    from playwright.sync_api import sync_playwright
    raw = OUT / "raw"
    raw.mkdir(exist_ok=True)
    plans = {
        "download": ("/download/chatx", [(0, 3000), (500, 5000), (700, 6000), (900, 6000), (-2100, 4000)]),
        "pricing": ("/pricing", [(0, 3000), (600, 5000), (800, 5000)]),
    }
    with sync_playwright() as p:
        b = p.chromium.launch()
        for key, (path, steps) in plans.items():
            ctx = b.new_context(viewport={"width": W, "height": H}, record_video_dir=str(raw),
                                record_video_size={"width": W, "height": H}, locale="zh-CN")
            page = ctx.new_page()
            page.goto(SITE + path, wait_until="networkidle", timeout=90000)
            page.wait_for_timeout(1500)
            try:
                page.mouse.click(960, 540)
                page.keyboard.press("Escape")
            except Exception:
                pass
            page.wait_for_timeout(2500)  # cookie 条延迟出现
            for label in ("接受", "同意", "Accept"):  # cookie 条不入镜
                try:
                    page.get_by_role("button", name=label).first.click(timeout=2000)
                    break
                except Exception:
                    continue
            try:  # 兜底：把 cookie 条整块隐藏
                page.evaluate("document.querySelectorAll('[class*=cookie],[id*=cookie]').forEach(e=>e.style.display='none')")
            except Exception:
                pass
            page.wait_for_timeout(600)
            for dy, dwell in steps:
                if dy:
                    page.evaluate(f"window.scrollBy({{top:{dy}, behavior:'smooth'}})")
                page.wait_for_timeout(dwell)
            ctx.close()
            vids = sorted(raw.glob("*.webm"), key=lambda f: f.stat().st_mtime)
            tgt = OUT / f"footage_{key}.webm"
            if tgt.exists():
                tgt.unlink()
            vids[-1].rename(tgt)
            d = dur(tgt)
            if d < 10:
                raise SystemExit(f"[FAIL] {tgt.name} 仅 {d:.1f}s")
            print(f"  [OK] {tgt.name} {d:.1f}s")
        b.close()


# ── 3. 卡片 ──────────────────────────────────────────────────────────────────

def _bg(dim=0.4) -> Image.Image:
    im = Image.open(BG).convert("RGB")
    s = max(W / im.width, H / im.height)
    im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
    x0, y0 = (im.width - W) // 2, (im.height - H) // 2
    im = im.crop((x0, y0, x0 + W, y0 + H))
    return Image.blend(im, Image.new("RGB", (W, H), (5, 6, 15)), dim)


def _text(d, xy, s, font, fill=(255, 255, 255), center=False):
    if center:
        box = d.textbbox((0, 0), s, font=font)
        xy = ((W - (box[2] - box[0])) // 2 - box[0], xy[1])
    d.text(xy, s, font=font, fill=fill)


def _mark(im: Image.Image, xy=(80, 60), size=120) -> None:
    """mark 原图 512×300（非正方形）——按高等比缩放，勿拉成正方形（首版变形）。"""
    if MARK.exists():
        mk = Image.open(MARK).convert("RGBA")
        h = size
        w = round(mk.width * h / mk.height)
        mk = mk.resize((w, h), Image.LANCZOS)
        im.paste(mk, xy, mk)


def card_title(dst: Path) -> None:
    im = _bg(0.3)
    d = ImageDraw.Draw(im)
    _mark(im, ((W - 341) // 2, 200), 200)
    _text(d, (0, 450), "三分钟装好智聊", ImageFont.truetype(FONT_B, 120), center=True)
    _text(d, (0, 610), "智聊 ChatX 教程 · 第 1 集 · 下载 → 安装 → 首启向导", ImageFont.truetype(FONT_R, 48), (200, 210, 230), center=True)
    im.save(dst)


def card_bullets(dst: Path, title: str, bullets: list[str], note: str = "") -> None:
    im = _bg(0.45)
    d = ImageDraw.Draw(im)
    _mark(im)
    _text(d, (320, 75), title, ImageFont.truetype(FONT_B, 64))  # mark 等比后宽 205，标题起点让开
    f = ImageFont.truetype(FONT_R, 54)
    y = 260
    for i, b in enumerate(bullets, 1):
        d.rounded_rectangle((200, y - 12, 1720, y + 90), 18, fill=(20, 26, 60), outline=(60, 120, 220), width=2)
        _text(d, (240, y + 8), f"{i}", ImageFont.truetype(FONT_B, 54), (0, 176, 240))
        _text(d, (320, y + 8), b, f)
        y += 130
    if note:
        _text(d, (200, 780), note, ImageFont.truetype(FONT_R, 34), (170, 180, 200))  # 字幕条占 h-230 起，注释放其上
    im.save(dst)


def card_tour(dst: Path) -> None:
    """真实工作台截图 + 联系人列表区打码（客户名/头像不入镜）+ 四处标注。"""
    im = _bg(0.6)
    shot = Image.open(SHELL_SHOT).convert("RGB")
    s = min(1700 / shot.width, 900 / shot.height)
    shot = shot.resize((round(shot.width * s), round(shot.height * s)), Image.LANCZOS)
    # 会话列表区（约 x 3%~19%，y 22%~100%）打码
    x0, y0, x1, y1 = int(shot.width * 0.03), int(shot.height * 0.22), int(shot.width * 0.19), shot.height
    region = shot.crop((x0, y0, x1, y1)).filter(ImageFilter.GaussianBlur(18))
    shot.paste(region, (x0, y0))
    ox, oy = (W - shot.width) // 2, 130
    im.paste(shot, (ox, oy))
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype(FONT_B, 40)
    labels = [("① 统一收件箱", 0.05, 0.30), ("② 平台账号页签", 0.12, 0.06), ("③ AI 副驾", 0.80, 0.30), ("④ 小智球·点哪教哪", 0.66, 0.90)]
    for txt, fx, fy in labels:
        x, y = ox + int(shot.width * fx), oy + int(shot.height * fy)
        box = d.textbbox((0, 0), txt, font=f)
        d.rounded_rectangle((x - 14, y - 10, x + box[2] + 14, y + box[3] + 12), 14, fill=(0, 120, 200))
        d.text((x, y), txt, font=f, fill="white")
    _text(d, (0, 40), "主界面四个地方", ImageFont.truetype(FONT_B, 56), center=True)
    _text(d, (0, 1030), "示意截图：会话列表已打码", ImageFont.truetype(FONT_R, 26), (150, 160, 180), center=True)
    im.save(dst)


def card_end(dst: Path) -> None:
    import qrcode
    im = _bg(0.3)
    d = ImageDraw.Draw(im)
    _mark(im, (300, 200), 160)
    _text(d, (620, 200), "下一集：接入 Telegram / WhatsApp", ImageFont.truetype(FONT_B, 64))
    _text(d, (620, 300), "有问题点小球问小智 · 官网 bd2026.cc", ImageFont.truetype(FONT_R, 44), (200, 210, 230))
    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data("https://bd2026.cc/download/chatx?utm_source=video&utm_campaign=t1-zh")
    qr.make(fit=True)
    q = qr.make_image(fill_color="black", back_color="white").convert("RGB").resize((380, 380))
    im.paste(q, (1380, 560))
    _text(d, (300, 620), "智聊 ChatX · 免费开始 · 标准翻译永久免费", ImageFont.truetype(FONT_B, 48))
    _text(d, (300, 720), "片头片尾歌声由 幻声 VoiceX 生成 · 无界科技 BOUNDLESS", ImageFont.truetype(FONT_R, 34), (170, 180, 200))
    im.save(dst)


def cards() -> None:
    card_title(OUT / "c_title.png")
    card_bullets(OUT / "c_learn.png", "这一集你会学到", ["官网下载安装包", "跳过 Windows 的未知发布者提示", "首启向导：语言 + Token", "主界面四个地方在哪"])
    card_bullets(OUT / "c_smartscreen.png", "第一次运行提示「未知发布者」？", ["点「更多信息」", "点「仍要运行」", "安装继续，等进度条走完"],
                 "示意卡：未签名安装包在 Windows 上的正常提示，不是病毒警告")
    card_bullets(OUT / "c_wizard.png", "首启向导", ["选语言（简中 / 繁中 / English…）", "填注册 Token（官网注册后邮件/页面可见）", "完成 → 进入主界面"],
                 "示意卡：向导画面随版本微调，以你看到的为准")
    card_tour(OUT / "c_tour.png")
    card_bullets(OUT / "c_newbie.png", "新人包（72 小时内）", ["注册后 72 小时内一次", "充值双倍到账", "不急：先接渠道试一试"],
                 "价格与规则以官网 /pricing 为准")
    card_end(OUT / "c_end.png")
    print("  [OK] 7 张卡片")


# ── 4. 拼装 ──────────────────────────────────────────────────────────────────

def _wrap(s: str, width: int = 30) -> str:
    """drawtext 不自动折行：超过 width 单位就在最近的标点处切成两行（1920 宽 44 号字约容 38 个汉字）。"""
    if len(s) <= width:
        return s
    cut = max((i for i, ch in enumerate(s[: width + 4]) if ch in "，。！？、；："), default=width)
    return s[: cut + 1].strip() + "\n" + _wrap(s[cut + 1:].strip(), width)


def _seg(idx: int, *, video: Path, is_image: bool, audio: Path | None, length: float,
         v_offset: float = 0.0, a_gain: float = 1.0, a_fade_out: float = 0.0, sub: str = "") -> Path:
    out = OUT / f"seg_{idx:02d}.mp4"
    vin = ["-loop", "1", "-t", f"{length:.2f}", "-i", str(video)] if is_image else ["-ss", f"{v_offset}", "-t", f"{length:.2f}", "-i", str(video)]
    ain = ["-i", str(audio)] if audio else ["-f", "lavfi", "-t", f"{length:.2f}", "-i", "anullsrc=r=48000:cl=stereo"]
    vf = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p"
    if sub:
        txt = OUT / f"sub_{idx:02d}.txt"
        txt.write_text(_wrap(sub), encoding="utf-8")
        tf = str(txt).replace("\\", "/").replace(":", "\\:")
        # drawbox 里 h/w 是「盒子自身」尺寸，画面高要用 ih（首版写 y=h-170 结果盒子画到了顶部）
        # 字幕条整体上抬（老板 09-16：第二行太靠下）：条高 190、底边留 40，文字在条内垂直居中
        vf += (f",drawbox=x=0:y=ih-230:w=iw:h=190:color=black@0.55:t=fill,"
               f"drawtext=textfile='{tf}':fontfile='C\\:/Windows/Fonts/msyh.ttc':fontsize=42:fontcolor=white:"
               f"borderw=2:bordercolor=black@0.8:line_spacing=0:x=(w-tw)/2:y=h-230+(190-th)/2")
    af = f"volume={a_gain},apad,atrim=duration={length:.2f}"
    if a_fade_out:
        af += f",afade=t=out:st={length - a_fade_out:.2f}:d={a_fade_out:.2f}"
    r = run(["ffmpeg", "-y", *vin, *ain, "-filter_complex", f"[0:v]{vf}[v];[1:a]{af}[a]", "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
             "-ar", "48000", "-t", f"{length:.2f}", str(out)])
    if r.returncode != 0:
        raise SystemExit(f"[FAIL] seg {idx}: {r.stderr[-900:]}")
    return out


def assemble() -> None:
    # jingle：多抽里按 ASR 逐句命中均值挑最能听清词的那把（2026-09-16：425 号 take 命中全 0＝几乎没唱出词，424 号可用）
    cands = []
    for js in (ROOT / "out").glob("JINGLE_full_s*.json"):
        m = json.loads(js.read_text(encoding="utf-8"))
        hits = m.get("line_hits") or [0]
        cands.append((sum(hits) / len(hits), js.with_suffix(".wav")))
    jingle = max(cands)[1] if cands else None
    if not jingle or not jingle.exists():
        raise SystemExit("[FAIL] 缺 jingle：先 make_song.py JINGLE --cut full --duration 30")
    print(f"  jingle 选 {jingle.name}（命中均值 {max(cands)[0]:.2f}）")
    vo_d = {k: check_mp3(OUT / f"vo_{k}.mp3") for k, _ in SEGS}
    text = dict(SEGS)
    segs: list[Path] = []
    # 0 片头：标题卡 + jingle 前 6 s
    segs.append(_seg(0, video=OUT / "c_title.png", is_image=True, audio=jingle, length=6.0, a_gain=0.9, a_fade_out=1.5))
    # 1 要点卡
    segs.append(_seg(1, video=OUT / "c_learn.png", is_image=True, audio=OUT / "vo_s1_learn.mp3", length=vo_d["s1_learn"] + 0.8, sub=text["s1_learn"]))
    # 2 下载页真录屏
    fd = OUT / "footage_download.webm"
    segs.append(_seg(2, video=fd, is_image=False, audio=OUT / "vo_s2_download.mp3", length=max(vo_d["s2_download"] + 1.0, 12.0), v_offset=1.0, sub=text["s2_download"]))
    # 3 SmartScreen 示意
    segs.append(_seg(3, video=OUT / "c_smartscreen.png", is_image=True, audio=OUT / "vo_s3_smartscreen.mp3", length=vo_d["s3_smartscreen"] + 0.8, sub=text["s3_smartscreen"]))
    # 4 向导示意
    segs.append(_seg(4, video=OUT / "c_wizard.png", is_image=True, audio=OUT / "vo_s4_wizard.mp3", length=vo_d["s4_wizard"] + 0.8, sub=text["s4_wizard"]))
    # 5 主界面导览
    segs.append(_seg(5, video=OUT / "c_tour.png", is_image=True, audio=OUT / "vo_s5_tour.mp3", length=vo_d["s5_tour"] + 0.8, sub=text["s5_tour"]))
    # 6 新人包：定价页真录屏
    fp = OUT / "footage_pricing.webm"
    segs.append(_seg(6, video=fp, is_image=False, audio=OUT / "vo_s6_newbie.mp3", length=max(vo_d["s6_newbie"] + 1.0, 8.0), v_offset=1.0, sub=text["s6_newbie"]))
    # 7 尾卡 + 念白
    segs.append(_seg(7, video=OUT / "c_end.png", is_image=True, audio=OUT / "vo_s7_next.mp3", length=vo_d["s7_next"] + 0.8, sub=text["s7_next"]))
    # 8 尾奏：jingle 尾 6 s
    jd = dur(jingle)
    tail = OUT / "jingle_tail.wav"
    run(["ffmpeg", "-y", "-ss", f"{max(0, jd - 7):.2f}", "-i", str(jingle), "-t", "6.5", str(tail)])
    segs.append(_seg(8, video=OUT / "c_end.png", is_image=True, audio=tail, length=6.0, a_gain=0.9, a_fade_out=2.0))

    lst = OUT / "concat.txt"
    lst.write_text("\n".join(f"file '{p.as_posix()}'" for p in segs), encoding="utf-8")
    out = OUT / "T1_install_zh.mp4"
    r = run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(out)])
    if r.returncode != 0:
        raise SystemExit(f"[FAIL] concat: {r.stderr[-600:]}")
    info = ffprobe(out)
    kinds = {s["codec_type"] for s in info["streams"]}
    d = float(info["format"]["duration"])
    expect = sum(dur(p) for p in segs)
    if out.read_bytes()[4:8] != b"ftyp" or kinds != {"video", "audio"} or abs(d - expect) > 1.5:
        raise SystemExit(f"[FAIL] 验收不过 streams={kinds} dur={d:.1f} expect={expect:.1f}")
    print(f"  [OK] {out.name}: {d:.1f}s 1920x1080 双流 {out.stat().st_size//1024//1024}MB")
    for t in (3, 12, 30, d - 12, d - 3):
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.1f}", "-i", str(out), "-frames:v", "1", str(OUT / f"T1_frame_t{int(t)}.png")])
    print("  抽帧 5 张")


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    if stage in ("vo", "all"):
        vo()
    if stage in ("record", "all"):
        record()
    if stage in ("cards", "all"):
        cards()
    if stage in ("assemble", "all"):
        assemble()
    print("DONE")
