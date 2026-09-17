#!/usr/bin/env python3
"""官网发布版出厂：E1–E12 + T1 → out/_web/（2026-09-17，官网 /chatx/tutorials 上线）。

每条做五件事，任一步不过即非零退出（媒体产物验证纪律）：
  1. 脱敏：检测「工作台会话列表」可见的时间段，对列表第 3 行以下（真实联系人昵称/头像）区域高斯模糊；
     演示会话 BOUNDLESS / 智聊支持（前两行）保持清晰。检测靠列表表头模板匹配（cv2），对行序变化鲁棒。
  2. 裁尾卡：网页端由播放列表负责「下一集」，二维码对着自己屏幕没意义 → 去掉片尾卡（E* 7 s / T1 6 s 尾奏），
     补 0.6 s 视频淡出 + 1.5 s 音频淡出。
  3. 转码兼容：源片 H.264 High@L6.2 超出 iOS/多数移动硬解上限 → High@L4.1、yuv420p、faststart、AAC 48k。
  4. 海报：1280×720 统一模板（ink-950 底 + 品牌渐变集号 + 标题 + 该集 UI 截图局部），JPEG ≤ 160 KB。
  5. 双验：magic bytes（ftyp / FFD8）+ ffprobe（profile/level/流/时长）+ moov 在 mdat 前；并抽 3 帧到 _web/qa/ 供目检。

  python publish_web.py            # 全部
  python publish_web.py E1 E12     # 指定集
  python publish_web.py --no-blur E9  # 该集无工作台画面时可跳过检测（默认仍检测，检测不到就不模糊）

产物：out/_web/chatx-e01-inbox.mp4 / .jpg … + manifest.json（供 website/lib/chatx-tutorials.ts 抄写）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
WEB = OUT / "_web"
QA = WEB / "qa"
FONT_B, FONT_R = "C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc"
FONT_NUM = next((f for f in ("C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf") if Path(f).exists()), FONT_B)
W, H = 1920, 1080

# 集 → (源片, slug, 尾卡秒数, 功能名)。E12 定版 h452（较新一版）。
EPISODES: dict[str, tuple[str, str, float, str]] = {
    "T1": ("T1/T1_install_zh.mp4", "install", 6.0, "安装与首启"),
    "E1": ("E1/E1_h426_episode.mp4", "inbox", 7.0, "统一收件箱"),
    "E2": ("E2/E2_h428_episode.mp4", "channels", 7.0, "渠道接入"),
    "E3": ("E3/E3_h429_episode.mp4", "translate", 7.0, "拟人互译"),
    "E4": ("E4/E4_h430_episode.mp4", "ai-reply", 7.0, "AI 拟稿与自动回复"),
    "E5": ("E5/E5_h431_episode.mp4", "persona", 7.0, "人设工作室"),
    "E6": ("E6/E6_h432_episode.mp4", "knowledge", 7.0, "知识库"),
    "E7": ("E7/E7_h433_episode.mp4", "voice", 7.0, "克隆语音"),
    "E8": ("E8/E8_h434_episode.mp4", "goals", 7.0, "工作目标与今日拍"),
    "E9": ("E9/E9_h435_episode.mp4", "care-memory", 7.0, "主动关怀与记忆"),
    "E10": ("E10/E10_h436_episode.mp4", "xiaozhi", 7.0, "小智助手球"),
    "E11": ("E11/E11_h437_episode.mp4", "guardrails", 7.0, "风险与护栏"),
    "E12": ("E12/E12_h452_episode.mp4", "billing", 7.0, "充值与额度"),
}
INTRO_S = 8.0  # 标题卡时长（curriculum.format.intro_song_s）

# 工作台会话列表几何（1920×1080 源坐标）：
#   表头（「AI+人工会话」+ 搜索框 + 私聊/群组/全部 三个页签）用于判定「列表可见」；
#   前两行（BOUNDLESS / 智聊支持 演示会话）模板用于判定「行序是演示顺序」：
#     是 → 只模糊第 3 行起（BLUR_PARTIAL）；否（切了群组/未读筛选、行序变化）→ 整列模糊（BLUR_FULL）。
HEADER_BOX = (66, 105, 366, 215)     # x0,y0,x1,y1
DEMO_ROWS_BOX = (66, 278, 290, 392)  # 去掉右侧时间戳/角标（录制时刻不同会变）
BLUR_PARTIAL = (66, 396, 366, 1060)
BLUR_FULL = (66, 268, 366, 1060)
DETECT_STRIP = (40, 60, 400, 420)    # 表头允许上下漂移的搜索带
DEMO_STRIP = (40, 240, 400, 470)
HEADER_THRESHOLD = 0.60              # 实测：列表可见 0.85~1.0，抽屉/弹层压暗时 0.69~0.72，非工作台页 ≤0.1
DEMO_THRESHOLD = 0.68                # 实测：演示顺序 0.93+（选中/红圈压住 0.72~0.81）；群组筛选等非演示行序 ≤0.60
SMOOTH_WIN = 5                       # 分类做 5 采样（1.25 s）多数表决，消除闪烁
SAMPLE_FPS = 4


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def ffprobe(p: Path) -> dict:
    r = run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(p)])
    if r.returncode != 0:
        raise SystemExit(f"[FAIL] ffprobe {p.name}: {r.stderr[:300]}")
    return json.loads(r.stdout)


def frame_at(p: Path, t: float) -> np.ndarray:
    cap = cv2.VideoCapture(str(p))
    cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
    ok, fr = cap.read()
    cap.release()
    if not ok:
        raise SystemExit(f"[FAIL] 抽帧失败 {p.name} @ {t}s")
    return fr


# 前两行「演示顺序」参考帧：常态 / BOUNDLESS 被选中高亮 / 智聊支持被选中+指针红圈——取各模板最高分，
# 否则行被选中变色就会误判成「行序变化」而整列模糊，把教程正在指的那一行也糊掉（E3 首版实测踩坑）。
DEMO_REFS: tuple[tuple[str, float], ...] = (("E1", 11.0), ("E3", 12.0), ("E4", 22.0))


def templates() -> tuple[np.ndarray, list[np.ndarray]]:
    """E1 t=11 s 的会话列表表头 + 多状态前两行模板（缓存到 _web/_tpl_*.png）。"""
    th = WEB / "_tpl_header.png"
    tds = [WEB / f"_tpl_demo_{ep}_{int(t)}.png" for ep, t in DEMO_REFS]
    if th.exists() and all(p.exists() for p in tds):
        return cv2.imread(str(th), cv2.IMREAD_GRAYSCALE), [cv2.imread(str(p), cv2.IMREAD_GRAYSCALE) for p in tds]
    fr = cv2.cvtColor(frame_at(OUT / EPISODES["E1"][0], 11.0), cv2.COLOR_BGR2GRAY)
    x0, y0, x1, y1 = HEADER_BOX
    cv2.imwrite(str(th), fr[y0:y1, x0:x1])
    x0, y0, x1, y1 = DEMO_ROWS_BOX
    for (ep, t), p in zip(DEMO_REFS, tds):
        g = cv2.cvtColor(frame_at(OUT / EPISODES[ep][0], t), cv2.COLOR_BGR2GRAY)
        cv2.imwrite(str(p), g[y0:y1, x0:x1])
    return templates()


def _score(fr_gray: np.ndarray, strip: tuple[int, int, int, int], tpl: np.ndarray) -> float:
    sx0, sy0, sx1, sy1 = strip
    res = cv2.matchTemplate(fr_gray[sy0:sy1, sx0:sx1], tpl, cv2.TM_CCOEFF_NORMED)
    return float(res.max())


def _merge(hits: list[float], total: float) -> list[tuple[float, float]]:
    if not hits:
        return []
    pad = 0.3
    ranges: list[list[float]] = []
    for t in hits:
        if ranges and t - ranges[-1][1] <= (1.0 / SAMPLE_FPS) * 1.5 + 0.01:
            ranges[-1][1] = t
        else:
            ranges.append([t, t])
    return [(max(0.0, a - pad), min(total, b + 1.0 / SAMPLE_FPS + pad)) for a, b in ranges]


def detect_list_ranges(src: Path, total: float, tpls: tuple[np.ndarray, list[np.ndarray]],
                       probe: bool = False) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """按 SAMPLE_FPS 采样：表头命中=列表可见；前两行（任一状态模板）命中=演示顺序。
    返回 (partial_ranges, full_ranges)：partial 只糊第 3 行起，full 整列糊。"""
    tpl_h, tpl_ds = tpls
    cap = cv2.VideoCapture(str(src))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, int(round(fps / SAMPLE_FPS)))
    samples: list[tuple[float, int, float, float]] = []  # (t, cls, sh, sd) cls: 0=无列表 1=partial 2=full
    i = 0
    while True:
        if not cap.grab():
            break
        if i % step == 0:
            t = i / fps
            if t > total:
                break
            ok, fr = cap.retrieve()
            if ok:
                g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
                sh = _score(g, DETECT_STRIP, tpl_h)
                sd = max(_score(g, DEMO_STRIP, td) for td in tpl_ds) if sh >= HEADER_THRESHOLD else 0.0
                cls = 0 if sh < HEADER_THRESHOLD else (1 if sd >= DEMO_THRESHOLD else 2)
                samples.append((t, cls, sh, sd))
        i += 1
    cap.release()
    # 多数表决平滑（列表可见性与行序都不会以 <1 s 频率真实切换）
    half = SMOOTH_WIN // 2
    smoothed: list[int] = []
    for k in range(len(samples)):
        win = [c for _, c, _, _ in samples[max(0, k - half):k + half + 1]]
        smoothed.append(max(set(win), key=lambda v: (win.count(v), v)))  # 并列时偏向更保守（full > partial > 无）
    partial = [t for (t, _, _, _), c in zip(samples, smoothed) if c == 1]
    full = [t for (t, _, _, _), c in zip(samples, smoothed) if c == 2]
    if probe:
        prev = None
        for (t, _, sh, sd), c in zip(samples, smoothed):
            if c != prev:
                print(f"    t={t:6.2f} → {['无列表', 'partial', 'FULL'][c]}  (header={sh:.2f} demo={sd:.2f})")
                prev = c
    return _merge(partial, total), _merge(full, total)


def _blur_chain(label_in: str, label_out: str, box: tuple[int, int, int, int]) -> str:
    x0, y0, x1, y1 = box
    return (f"[{label_in}]crop={x1 - x0}:{y1 - y0}:{x0}:{y0},"
            f"boxblur=luma_radius=16:luma_power=2:chroma_radius=8:chroma_power=2[{label_out}];")


def encode(src: Path, dst: Path, total: float,
           partial: list[tuple[float, float]], full: list[tuple[float, float]]) -> None:
    fade_v, fade_a = 0.6, 1.5
    fade = f"fade=t=out:st={total - fade_v:.2f}:d={fade_v}"
    layers = [(BLUR_PARTIAL, partial), (BLUR_FULL, full)]
    layers = [(box, rng) for box, rng in layers if rng]
    if layers:
        n = len(layers)
        vf = f"[0:v]split={n + 1}[base]" + "".join(f"[t{k}]" for k in range(n)) + ";"
        cur = "base"
        for k, (box, rng) in enumerate(layers):
            vf += _blur_chain(f"t{k}", f"bl{k}", box)
            enable = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in rng)
            nxt = f"o{k}"
            vf += f"[{cur}][bl{k}]overlay={box[0]}:{box[1]}:enable='{enable}'[{nxt}];"
            cur = nxt
        vf += f"[{cur}]{fade}[v]"
    else:
        vf = f"[0:v]{fade}[v]"
    af = f"[0:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,afade=t=out:st={total - fade_a:.2f}:d={fade_a}[a]"
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src),
           "-filter_complex", vf + ";" + af, "-map", "[v]", "-map", "[a]",
           "-t", f"{total:.3f}",
           "-c:v", "libx264", "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p",
           "-crf", "21", "-preset", "slow", "-maxrate", "3M", "-bufsize", "6M",
           "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
           "-movflags", "+faststart", str(dst)]
    r = run(cmd)
    if r.returncode != 0:
        raise SystemExit(f"[FAIL] ffmpeg {dst.name}:\n{r.stderr[-1500:]}")


def verify_mp4(p: Path, expect_dur: float) -> dict:
    data = p.read_bytes()
    if data[4:8] != b"ftyp":
        raise SystemExit(f"[FAIL] {p.name} 不是 MP4（magic）")
    # moov 必须在 mdat 之前（faststart）
    pos, order = 0, []
    while pos + 8 <= len(data) and len(order) < 6:
        size, = struct.unpack(">I", data[pos:pos + 4])
        typ = data[pos + 4:pos + 8].decode("latin1")
        order.append(typ)
        if size == 0 or typ == "mdat":
            break
        pos += size
    if "moov" not in order or ("mdat" in order and order.index("moov") > order.index("mdat")):
        raise SystemExit(f"[FAIL] {p.name} moov 不在前：{order}")
    info = ffprobe(p)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if a is None:
        raise SystemExit(f"[FAIL] {p.name} 无音轨")
    if v["codec_name"] != "h264" or v.get("profile") != "High" or int(v.get("level", 0)) > 41:
        raise SystemExit(f"[FAIL] {p.name} 编码不合规：{v['codec_name']} {v.get('profile')} L{v.get('level')}")
    if (v["width"], v["height"]) != (W, H) or v.get("pix_fmt") != "yuv420p":
        raise SystemExit(f"[FAIL] {p.name} 画幅/像素格式：{v['width']}x{v['height']} {v.get('pix_fmt')}")
    d = float(info["format"]["duration"])
    if abs(d - expect_dur) > 0.6:
        raise SystemExit(f"[FAIL] {p.name} 时长 {d:.2f} ≠ {expect_dur:.2f}")
    return {"duration": round(d, 2), "size": p.stat().st_size, "level": int(v["level"]),
            "sha256": hashlib.sha256(data).hexdigest()[:16]}


# ── 海报 ─────────────────────────────────────────────────────────────────────
PW, PH = 1280, 720


def _glow(im: Image.Image, cx: int, cy: int, r: int, rgb: tuple[int, int, int], alpha: int) -> None:
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=rgb + (alpha,))
    layer = layer.filter(ImageFilter.GaussianBlur(r * 0.6))
    im.alpha_composite(layer)


def _gradient_text(im: Image.Image, xy: tuple[int, int], text: str, font: ImageFont.FreeTypeFont,
                   c0=(34, 211, 238), c1=(139, 92, 246)) -> tuple[int, int]:
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).text(xy, text, font=font, fill=255)
    box = mask.getbbox() or (xy[0], xy[1], xy[0], xy[1])
    grad = Image.new("RGBA", im.size, (0, 0, 0, 0))
    gp = grad.load()
    x0, x1 = box[0], max(box[2], box[0] + 1)
    for x in range(x0, x1):
        k = (x - x0) / (x1 - x0)
        col = tuple(round(c0[i] + (c1[i] - c0[i]) * k) for i in range(3)) + (255,)
        for y in range(box[1], box[3]):
            gp[x, y] = col
    im.paste(grad, (0, 0), mask)
    return box[2] - box[0], box[3] - box[1]


def _rounded(im: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.width - 1, im.height - 1), radius=radius, fill=255)
    out = im.convert("RGBA")
    out.putalpha(mask)
    return out


def poster(ep_id: str, ep_no: int, title: str, feature: str, dur_s: float, shot_bgr: np.ndarray, dst: Path) -> None:
    im = Image.new("RGBA", (PW, PH), (5, 6, 15, 255))
    _glow(im, int(PW * 0.18), int(PH * 0.08), 360, (139, 92, 246), 70)
    _glow(im, int(PW * 0.85), 0, 320, (34, 211, 238), 60)
    # 细网格
    grid = Image.new("RGBA", im.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(grid)
    for x in range(0, PW, 64):
        gd.line((x, 0, x, PH), fill=(255, 255, 255, 10))
    for y in range(0, PH, 64):
        gd.line((0, y, PW, y), fill=(255, 255, 255, 10))
    im.alpha_composite(grid)

    # 右侧 UI 截图：取源帧中部偏右区域（避开左栏联系人列表），圆角卡 + 描边
    shot = Image.fromarray(cv2.cvtColor(shot_bgr, cv2.COLOR_BGR2RGB))
    crop = shot.crop((380, 40, 1900, 1040))            # 1520×1000
    card_w = 600
    card = crop.resize((card_w, round(card_w * crop.height / crop.width)), Image.LANCZOS)
    card = _rounded(card, 16)
    cx, cy = PW - card_w - 44, (PH - card.height) // 2 + 6
    shadow = Image.new("RGBA", im.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((cx + 8, cy + 18, cx + card_w + 8, cy + card.height + 18), radius=16, fill=(0, 0, 0, 160))
    im.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(22)))
    im.alpha_composite(card, (cx, cy))
    ImageDraw.Draw(im).rounded_rectangle((cx, cy, cx + card_w - 1, cy + card.height - 1), radius=16, outline=(255, 255, 255, 40), width=1)

    d = ImageDraw.Draw(im)
    left = 64
    # 系列名
    d.text((left, 64), "智聊 ChatX 视频教程", font=ImageFont.truetype(FONT_R, 24), fill=(148, 163, 184, 255))
    # 集号（渐变）
    num = "安装" if ep_id == "T1" else f"{ep_no:02d}"
    _gradient_text(im, (left - 6, 108), num, ImageFont.truetype(FONT_NUM if ep_id != "T1" else FONT_B, 168 if ep_id != "T1" else 120))
    # 标题（自动换行，最多两行）
    tf = ImageFont.truetype(FONT_B, 46)
    maxw = PW - card_w - 44 - left - 36
    lines, cur = [], ""
    for ch in title:
        if d.textlength(cur + ch, font=tf) > maxw and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    y = 330
    for ln in lines[:2]:
        d.text((left, y), ln, font=tf, fill=(255, 255, 255, 255))
        y += 60
    # 副题：集号 · 功能 · 时长
    m, s = divmod(int(round(dur_s)), 60)
    d.text((left, y + 14), f"{ep_id} · {feature} · {m}:{s:02d}", font=ImageFont.truetype(FONT_R, 22), fill=(148, 163, 184, 255))
    # 底部：真机录屏 + 歌声由幻声生成
    pill = Image.new("RGBA", im.size, (0, 0, 0, 0))
    ImageDraw.Draw(pill).rounded_rectangle((left, PH - 92, left + 104, PH - 62), radius=15,
                                           fill=(34, 211, 238, 36), outline=(34, 211, 238, 90))
    im.alpha_composite(pill)  # 单独图层再合成：直接 draw 的 alpha 会在转 RGB 时被丢掉变成实心块
    d = ImageDraw.Draw(im)
    d.text((left + 16, PH - 88), "真机录屏", font=ImageFont.truetype(FONT_B, 17), fill=(34, 211, 238, 255))
    d.text((left + 118, PH - 88), "歌声与说唱由幻声 VoiceX 生成 · bd2026.cc", font=ImageFont.truetype(FONT_R, 17), fill=(100, 116, 139, 255))

    rgb = im.convert("RGB")
    q = 86
    while True:
        rgb.save(dst, "JPEG", quality=q, optimize=True, progressive=True)
        if dst.stat().st_size <= 160 * 1024 or q <= 60:
            break
        q -= 6
    if dst.read_bytes()[:2] != b"\xff\xd8":
        raise SystemExit(f"[FAIL] {dst.name} 不是 JPEG")


def series_og(entries: list[dict], dst: Path) -> None:
    """合集 OG 图 1200×630：标题 + 12 个集号。"""
    ow, oh = 1200, 630
    im = Image.new("RGBA", (ow, oh), (5, 6, 15, 255))
    _glow(im, 200, 40, 340, (139, 92, 246), 70)
    _glow(im, 1050, 0, 300, (34, 211, 238), 60)
    d = ImageDraw.Draw(im)
    eps_all = [e for e in entries if e["ep"] > 0]
    minutes = round(sum(e["durationSec"] for e in eps_all) / 60)
    d.text((72, 72), "智聊 ChatX 视频教程", font=ImageFont.truetype(FONT_R, 28), fill=(148, 163, 184, 255))
    _gradient_text(im, (68, 110), f"{len(eps_all)} 集 · 约 {minutes} 分钟", ImageFont.truetype(FONT_B, 84))
    d.text((72, 230), "一个收件箱接住所有平台 · 拟人互译 · AI 拟稿 · 人设 · 知识库 · 克隆语音 · 护栏 · 免费开始",
           font=ImageFont.truetype(FONT_R, 24), fill=(203, 213, 225, 255))
    x, y = 72, 320
    nf = ImageFont.truetype(FONT_NUM, 40)
    tf = ImageFont.truetype(FONT_R, 18)
    eps = [e for e in entries if e["id"] != "T1"]
    cells = Image.new("RGBA", im.size, (0, 0, 0, 0))  # 半透明卡底单独图层合成（直接 draw 会变实心白块）
    cd = ImageDraw.Draw(cells)
    for i, e in enumerate(eps):
        col, row = i % 6, i // 6
        bx, by = x + col * 178, y + row * 130
        cd.rounded_rectangle((bx, by, bx + 160, by + 110), radius=14, fill=(255, 255, 255, 14), outline=(255, 255, 255, 34))
    im.alpha_composite(cells)
    d = ImageDraw.Draw(im)
    for i, e in enumerate(eps):
        col, row = i % 6, i // 6
        bx, by = x + col * 178, y + row * 130
        _gradient_text(im, (bx + 16, by + 8), f"{e['ep']:02d}", nf)
        d.text((bx + 16, by + 68), e["feature"][:9], font=tf, fill=(203, 213, 225, 255))
    d.text((72, oh - 56), "真机录屏 · 歌声与说唱由幻声 VoiceX 生成 · bd2026.cc/chatx/tutorials",
           font=ImageFont.truetype(FONT_R, 18), fill=(100, 116, 139, 255))
    im.convert("RGB").save(dst, "JPEG", quality=86, optimize=True, progressive=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("eps", nargs="*", help="E1 E2 … T1；缺省全部")
    ap.add_argument("--no-blur", action="store_true")
    ap.add_argument("--probe", action="store_true", help="只打印检测分数，不转码")
    ap.add_argument("--posters-only", action="store_true", help="只用已出厂的 _web/*.mp4 重做海报与合集 OG 图")
    a = ap.parse_args()
    ids = a.eps or list(EPISODES)
    WEB.mkdir(exist_ok=True)
    QA.mkdir(exist_ok=True)
    cur = json.loads((ROOT / "curriculum.json").read_text(encoding="utf-8"))
    titles = {e["id"]: e["title"] for e in cur["episodes"]}
    tpls = None if a.no_blur else templates()

    manifest_path = WEB / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    for ep_id in ids:
        rel, slug, tail, feature = EPISODES[ep_id]
        src = OUT / rel
        if not src.exists():
            raise SystemExit(f"[FAIL] 源片不存在 {src}")
        ep_no = 0 if ep_id == "T1" else int(ep_id[1:])
        name = f"chatx-{ep_id.lower() if ep_id == 'T1' else f'e{ep_no:02d}'}-{slug}"
        dst, jpg = WEB / f"{name}.mp4", WEB / f"{name}.jpg"
        if a.posters_only:
            if not dst.exists() or ep_id not in manifest:
                raise SystemExit(f"[FAIL] {dst.name} 尚未出厂，不能只做海报")
            total = float(manifest[ep_id]["durationSec"])
            shot = frame_at(dst, INTRO_S + (total - INTRO_S) * 0.45)
            poster(ep_id, ep_no, titles[ep_id], feature, total, shot, jpg)
            print(f"  [OK] {jpg.name} {jpg.stat().st_size // 1024}KB（重做）")
            continue
        src_dur = float(ffprobe(src)["format"]["duration"])
        total = src_dur - tail
        print(f"[{ep_id}] {src.name} {src_dur:.1f}s → 裁尾 {tail}s = {total:.1f}s")
        partial, full = detect_list_ranges(src, total, tpls, probe=a.probe) if tpls is not None else ([], [])
        fmt = lambda r: ", ".join(f"{x:.1f}-{y:.1f}" for x, y in r) or "—"  # noqa: E731
        print(f"  第3行起模糊 {sum(b - a for a, b in partial):.1f}s：{fmt(partial)}")
        print(f"  整列模糊   {sum(b - a for a, b in full):.1f}s：{fmt(full)}")
        if a.probe:
            continue
        encode(src, dst, total, partial, full)
        meta = verify_mp4(dst, total)
        print(f"  [OK] {dst.name} {meta['duration']}s L{meta['level']} {meta['size'] // 1024}KB sha={meta['sha256']}")
        # 海报：用成片（已脱敏）中段一帧的 UI
        shot = frame_at(dst, INTRO_S + (total - INTRO_S) * 0.45)
        poster(ep_id, ep_no, titles[ep_id], feature, total, shot, jpg)
        print(f"  [OK] {jpg.name} {jpg.stat().st_size // 1024}KB")
        # QA 抽帧：唱段刚开、中段、尾
        for tag, t in (("a", INTRO_S + 2.5), ("b", INTRO_S + (total - INTRO_S) * 0.5), ("c", total - 1.2)):
            fr = frame_at(dst, t)
            cv2.imwrite(str(QA / f"{name}_{tag}_t{int(t)}.jpg"), cv2.resize(fr, (960, 540)), [cv2.IMWRITE_JPEG_QUALITY, 80])
        manifest[ep_id] = {"id": ep_id, "ep": ep_no, "slug": slug, "file": dst.name, "poster": jpg.name,
                           "title": titles[ep_id], "feature": feature, "durationSec": meta["duration"],
                           "size": meta["size"], "sha256_16": meta["sha256"], "source": rel,
                           "blurPartial": [[round(x, 2), round(y, 2)] for x, y in partial],
                           "blurFull": [[round(x, 2), round(y, 2)] for x, y in full]}
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    if len(manifest) >= 13:
        entries = sorted(manifest.values(), key=lambda e: e["ep"])
        series_og(entries, WEB / "chatx-tutorials-og.jpg")
        print(f"[OK] 合集 OG 图 {(WEB / 'chatx-tutorials-og.jpg').stat().st_size // 1024}KB")
    print(f"[DONE] manifest → {manifest_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    sys.exit(main())
