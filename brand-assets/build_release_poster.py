# -*- coding: utf-8 -*-
"""
发版/功能海报生成器（TG 频道贴文 1280x720）
================================================
用法：python build_release_poster.py            # 按内置 SPECS 全量出图
输出：06_posters/<name>.png

设计语言与 build_brand_assets.py 同源：深空底 #1A1D3A→#05060F + 点阵网格 +
品牌渐变辉光；左列 = ChatX 组合标 + 版本徽章 + 大标题 + 要点列表 + 下载地址，
右侧 = 功能主题插画（06_posters/motifs/，AI 生成的无文字素材，羽化融入背景）。

发版海报纪律（2026-08-28 老板拍板）：频道通知必须配「与本次新功能对应」的图，
不许拿裸 logo 卡顶包。新版本 = 在 SPECS 加一条（headline/bullets 对齐公告稿）+
必要时新生成 motif，跑本脚本即得成品。
"""

import os
import sys
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(ROOT, "fonts")
DIR_OUT = os.path.join(ROOT, "06_posters")
DIR_MOTIF = os.path.join(DIR_OUT, "motifs")
LOCKUP_CHATX = os.path.join(ROOT, "03_lockups", "products", "chatx-lockup-white.png")

W, H = 1280, 720


def C(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


DARK_TOP = C("1A1D3A")
DARK_BOT = C("05060F")
CYAN = C("00B0F0")
BLUE = C("1E6BF0")
VIOLET = C("7A3BF5")
MAGENTA = C("D030F0")
ORANGE = C("F07800")
WHITE = (255, 255, 255)


def font_noto(size, weight="Bold"):
    return ImageFont.truetype(os.path.join(FONTS, f"NotoSansCJKsc-{weight}.otf"), size)


def font_mont(size):
    return ImageFont.truetype(os.path.join(FONTS, "Montserrat-VF.ttf"), size)


def bg_canvas():
    """深空垂直渐变 + 点阵 + 双角辉光（与品牌背景同语言）。"""
    cv = Image.new("RGBA", (W, H))
    px = cv.load()
    for y in range(H):
        t = y / (H - 1)
        r = int(DARK_TOP[0] + (DARK_BOT[0] - DARK_TOP[0]) * t)
        g = int(DARK_TOP[1] + (DARK_BOT[1] - DARK_TOP[1]) * t)
        b = int(DARK_TOP[2] + (DARK_BOT[2] - DARK_TOP[2]) * t)
        for x in range(W):
            px[x, y] = (r, g, b, 255)
    dots = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dd = ImageDraw.Draw(dots)
    for gy in range(24, H, 46):
        for gx in range(24, W, 46):
            dd.ellipse([gx - 1, gy - 1, gx + 1, gy + 1], fill=(255, 255, 255, 13))
    cv.alpha_composite(dots)
    for (gx, gy, col, rad, alpha) in [
        (W - 180, 130, VIOLET, 300, 60),
        (220, H - 90, CYAN, 280, 42),
    ]:
        glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.ellipse([gx - rad, gy - rad, gx + rad, gy + rad], fill=col + (alpha,))
        cv.alpha_composite(glow.filter(ImageFilter.GaussianBlur(130)))
    return cv


def feathered_motif(path, size):
    """插画方图 → 羽化圆角遮罩，边缘融进海报背景。"""
    im = Image.open(path).convert("RGBA")
    im = im.resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    pad = int(size * 0.06)
    md.rounded_rectangle([pad, pad, size - pad, size - pad], radius=int(size * 0.24), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(int(size * 0.055)))
    im.putalpha(mask)
    return im


def grad_bar(w, h):
    bar = Image.new("RGBA", (w, h))
    bp = bar.load()
    stops = [CYAN, BLUE, VIOLET, MAGENTA]
    for x in range(w):
        t = x / max(w - 1, 1)
        seg = min(int(t * (len(stops) - 1)), len(stops) - 2)
        lt = t * (len(stops) - 1) - seg
        c1, c2 = stops[seg], stops[seg + 1]
        col = tuple(int(c1[i] + (c2[i] - c1[i]) * lt) for i in range(3))
        for y in range(h):
            bp[x, y] = col + (255,)
    return bar


def compose(spec):
    cv = bg_canvas()

    # 右侧功能插画（垂直居中偏右）
    motif = feathered_motif(os.path.join(DIR_MOTIF, spec["motif"]), spec.get("motif_size", 560))
    mx = W - motif.width - 44
    my = (H - motif.height) // 2 + 8
    cv.alpha_composite(motif, (mx, my))

    d = ImageDraw.Draw(cv)
    LX = 68  # 左列基线

    # 组合标
    lockup = Image.open(LOCKUP_CHATX).convert("RGBA")
    lh = 66
    lockup = lockup.resize((int(lockup.width * lh / lockup.height), lh), Image.LANCZOS)
    cv.alpha_composite(lockup, (LX - 4, 46))

    # 版本徽章（组合标右侧垂直居中；Noto 渲染——Montserrat 无中文字形，CJK 会成方框）
    pill_f = font_noto(24, "Medium")
    pill_txt = spec["badge"]
    tw = d.textlength(pill_txt, font=pill_f)
    bx0 = LX + lockup.width + 26
    by0 = 46 + (lh - 42) // 2
    d.rounded_rectangle([bx0, by0, bx0 + tw + 40, by0 + 42], radius=21,
                        fill=VIOLET + (52,), outline=VIOLET + (215,), width=2)
    d.text((bx0 + 20, by0 + 7), pill_txt, font=pill_f, fill=(226, 216, 255, 255))

    # 大标题（两行）
    ty = 176
    for i, line in enumerate(spec["headline"]):
        f = font_noto(74 if i == 0 else 60, "Black" if i == 0 else "Bold")
        col = WHITE if i == 0 else (196, 206, 235, 255)
        d.text((LX, ty), line, font=f, fill=col)
        ty += (96 if i == 0 else 78)

    # 渐变强调条
    bar = grad_bar(150, 8)
    cv.alpha_composite(bar, (LX + 2, ty + 2))
    ty += 34

    # 要点列表（菱形弹点循环渐变色）
    bullet_cols = [CYAN, VIOLET, MAGENTA, ORANGE]
    bf = font_noto(29, "Medium")
    for i, line in enumerate(spec["bullets"]):
        col = bullet_cols[i % len(bullet_cols)]
        cyc = ty + 21
        d.polygon([(LX + 9, cyc - 8), (LX + 17, cyc), (LX + 9, cyc + 8), (LX + 1, cyc)],
                  fill=col + (255,))
        d.text((LX + 34, ty), line, font=bf, fill=(255, 255, 255, 212))
        ty += 52

    # 底部：下载地址 + 口号
    fy = H - 74
    uf = font_mont(27)
    d.text((LX, fy), spec.get("cta", "bd2026.cc/download/chatx"), font=uf, fill=CYAN + (255,))
    sf = font_noto(24, "Medium")
    slog = spec.get("slogan", "让沟通，无界 · Communication, Boundless.")
    sw = d.textlength(slog, font=sf)
    d.text((W - sw - 56, fy + 2), slog, font=sf, fill=(255, 255, 255, 118))

    out = os.path.join(DIR_OUT, spec["name"] + ".png")
    cv.convert("RGB").save(out, "PNG")
    print("built:", os.path.relpath(out, ROOT))


SPECS = [
    {
        "name": "release-v1059-feedback-loop",
        "motif": "motif_feedback.png",
        "badge": "v1.0.59  ·  2026-08-29",
        "headline": ["报障有回音", "修好主动通知你"],
        "bullets": ["小智答疑不再中途断线", "账号资产一键导出迁移", "AI 回复长短更自然", "翻译 · 语音链路更稳"],
    },
    {
        "name": "release-v1058-hotpatch",
        "motif": "motif_hotpatch.png",
        "badge": "v1.0.58  ·  2026-08-27",
        "headline": ["后台热修复上线", "小问题重启即更新"],
        "bullets": ["不用再下完整安装包", "多账号身份错乱根治", "AI 回复不再『乱回忆』", "收发链路更稳更快"],
    },
    {
        "name": "feature-identity-guard",
        "motif": "motif_identity.png",
        "badge": "ChatX  ·  多账号",
        "headline": ["多账号身份守护", "会话归属永不错位"],
        "bullets": ["身份决议引擎升级", "新增账号交接向导", "用对身份回对话"],
    },
    {
        "name": "feature-ai-reliable",
        "motif": "motif_ai.png",
        "badge": "ChatX  ·  AI 质量",
        "headline": ["AI 回复更靠谱", "只说真实发生过的"],
        "bullets": ["无据回忆守卫上线", "首次接触开场更自然", "答不上来会如实直说"],
    },
    {
        "name": "feature-notify-center",
        "motif": "motif_notify.png",
        "badge": "ChatX  ·  体验",
        "headline": ["全站提醒统一", "轻提醒 · 不打扰"],
        "bullets": ["右下角 Toast 轻提醒", "消息中心集中回看", "重要事项不再刷屏"],
    },
]


if __name__ == "__main__":
    os.makedirs(DIR_OUT, exist_ok=True)
    names = set(sys.argv[1:])
    for spec in SPECS:
        if names and spec["name"] not in names:
            continue
        compose(spec)
