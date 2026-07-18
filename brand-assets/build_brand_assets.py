# -*- coding: utf-8 -*-
"""
无界科技 BOUNDLESS 品牌资产统一生成脚本
=========================================
从 00_master/src 的白底母版出发，一键生成：
  01_logos          公司主标多尺寸（透明底）+ 单色版 + favicon.ico
  02_product-icons  7 个产品图标多尺寸（透明底，防高光穿孔抠图）
  03_lockups        公司/产品 中英组合标（横排/竖排 × 深色字/白色字）
  04_avatars        客服号/资源号/产品号头像（512 + 128 预览）
  05_backgrounds    多平台背景图（TG贴文/竖屏故事/桌面/X/FB/YouTube/公众号）+ 产品矩阵海报
  MANIFEST.md       全部产物清单（自动生成）

重跑：  python build_brand_assets.py
依赖：  Pillow >= 10, numpy；fonts/ 下 NotoSansCJKsc(Black/Bold/Medium) + Montserrat 可变字体
"""

import os
import sys
import math
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "00_master", "src")
KEYED = os.path.join(ROOT, "00_master", "keyed")
FONTS = os.path.join(ROOT, "fonts")

DIR_LOGOS = os.path.join(ROOT, "01_logos")
DIR_PICONS = os.path.join(ROOT, "02_product-icons")
DIR_LOCKUPS = os.path.join(ROOT, "03_lockups")
DIR_AVATARS = os.path.join(ROOT, "04_avatars")
DIR_BG = os.path.join(ROOT, "05_backgrounds")

# ---------------------------------------------------------------- 品牌常量

def C(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

INK = C("0B1020")          # 品牌墨色（浅底文字）
WHITE = (255, 255, 255)
DARK_TOP = C("1A1D3A")     # 深空背景（与既有 TG 头像一致）
DARK_BOT = C("05060F")
LIGHT_TOP = C("FAFBFF")
LIGHT_BOT = C("E9EDF8")

BLUE = C("1E6BF0")
CYAN = C("00B0F0")
VIOLET = C("7A3BF5")
MAGENTA = C("D030F0")
PINK = C("F0509A")
ORANGE = C("F07800")
AMBER = C("F0A010")

# 主渐变（沿主标从左到右）
RING_STOPS = [CYAN, BLUE, VIOLET, MAGENTA, PINK, ORANGE, AMBER, CYAN]

TAGLINE_ZH = "让沟通，无界"
TAGLINE_EN = "Communication, Boundless."
COMPANY_ZH = "无界科技"
COMPANY_EN = "BOUNDLESS"

# 三系 × 七产品（与 ai-p0-integration/website/lib/brand.ts 对齐）
CATEGORIES = {
    "growth": {"zh": "智连", "en": "Growth", "tag": "社交增长 · 从触达到成交", "accent": C("1E8CF2"),
               "ring": [C("0070F0"), C("00C2FF"), C("0070F0")]},
    "studio": {"zh": "幻境", "en": "Studio", "tag": "数字分身 · 容貌声音身份", "accent": C("C43BF0"),
               "ring": [C("B62BF5"), C("F050C8"), C("B62BF5")]},
    "lingo": {"zh": "通达", "en": "Lingo", "tag": "跨语沟通 · 语言无障碍", "accent": C("F07800"),
              "ring": [C("F06A00"), C("FFB020"), C("F06A00")]},
}
PRODUCTS = {  # 顺序即展示顺序
    "reachx": {"zh": "智拓", "en": "ReachX", "cat": "growth", "desc": "真机多号自动获客引流"},
    "chatx": {"zh": "智聊", "en": "ChatX", "cat": "growth", "desc": "多平台 AI 聊天与成交"},
    "facex": {"zh": "幻颜", "en": "FaceX", "cat": "studio", "desc": "图片 / 视频 AI 换脸"},
    "voicex": {"zh": "幻声", "en": "VoiceX", "cat": "studio", "desc": "零样本声音克隆配音"},
    "livex": {"zh": "幻影", "en": "LiveX", "cat": "studio", "desc": "直播实时换脸换声"},
    "lingox": {"zh": "通译", "en": "LingoX", "cat": "lingo", "desc": "多平台实时聊天互译"},
    "voxx": {"zh": "通传", "en": "VoxX", "cat": "lingo", "desc": "会议直播同声传译"},
}

MANIFEST = []  # (relpath, "WxH", 用途)

# ---------------------------------------------------------------- 基础工具

def font_noto(size, weight="bold"):
    f = {"black": "NotoSansCJKsc-Black.otf", "bold": "NotoSansCJKsc-Bold.otf",
         "medium": "NotoSansCJKsc-Medium.otf"}[weight]
    return ImageFont.truetype(os.path.join(FONTS, f), int(size))

def font_mont(size, wght=800):
    f = ImageFont.truetype(os.path.join(FONTS, "Montserrat-VF.ttf"), int(size))
    f.set_variation_by_axes([wght])
    return f

def save_png(img, path, purpose):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, "PNG")
    rel = os.path.relpath(path, ROOT).replace("\\", "/")
    MANIFEST.append((rel, "%dx%d" % img.size, purpose))
    print("[ok] " + rel)

def autocrop(img, pad_ratio=0.0):
    bbox = img.getchannel("A").getbbox()
    if not bbox:
        return img
    img = img.crop(bbox)
    if pad_ratio > 0:
        pw = int(img.width * pad_ratio)
        ph = int(img.height * pad_ratio)
        c = Image.new("RGBA", (img.width + 2 * pw, img.height + 2 * ph), (0, 0, 0, 0))
        c.paste(img, (pw, ph), img)
        img = c
    return img

def fit(img, w, h=None):
    """等比缩放到 w×h 盒内（h 省略时按宽）。"""
    if h is None:
        h = 10 ** 9
    s = min(w / img.width, h / img.height)
    return img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))), Image.LANCZOS)

def boxed_square(img, size, pad_ratio=0.08):
    """等比放入正方形透明画布（统一光学边距）。官网 / 头像 / 导航共用此规格，
    避免「裁切非方图 → object-contain 下宽扁图标视觉偏小」。"""
    cv = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    box = int(size * (1 - 2 * pad_ratio))
    m = fit(img, box, box)
    cv.paste(m, ((size - m.width) // 2, (size - m.height) // 2), m)
    return cv

def scale_alpha(img, k):
    img = img.copy()
    a = img.getchannel("A").point(lambda v: int(v * k))
    img.putalpha(a)
    return img

def lerp(c1, c2, t):
    return tuple(int(round(c1[i] + (c2[i] - c1[i]) * t)) for i in range(3))

def color_at(stops, t):
    t = max(0.0, min(1.0, t))
    n = len(stops) - 1
    i = min(int(t * n), n - 1)
    return lerp(stops[i], stops[i + 1], t * n - i)

def vgrad(w, h, c_top, c_bot):
    t = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None, None]
    arr = np.array(c_top, np.float32) * (1 - t) + np.array(c_bot, np.float32) * t
    arr = np.repeat(arr, w, axis=1).astype(np.uint8)
    return Image.fromarray(arr, "RGB").convert("RGBA")

def add_glow(base, cx, cy, radius, color, strength):
    """加色发光（cx cy radius 均为像素）。"""
    w, h = base.size
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    inten = np.clip(1.0 - d / radius, 0, 1) ** 2 * strength
    arr = np.asarray(base.convert("RGB"), np.float32)
    arr = np.clip(arr + np.array(color, np.float32)[None, None, :] * inten[..., None], 0, 255)
    out = Image.fromarray(arr.astype(np.uint8), "RGB").convert("RGBA")
    return out

def dot_grid(img, spacing, r, fill):
    d = ImageDraw.Draw(img)
    w, h = img.size
    y = spacing // 2
    while y < h:
        x = spacing // 2
        while x < w:
            d.ellipse([x - r, y - r, x + r, y + r], fill=fill)
            x += spacing
        y += spacing
    return img

# ---------------------------------------------------------------- 抠图（防高光穿孔）

def key_out_white(src_path):
    """白底转透明（三层判定，兼顾既有品牌外观与高光保留）：
      1) 外部背景：四角洪水填充可达的白 → 透明；
      2) 大块封闭白（>0.4% 画面，如 ∞ 外框内屏、面具眼口、靶环间隙）→ 结构性
         镂空，按既有品牌暗色场景外观抠透明（与旧管线一致）；
      3) 小块封闭白（高光星芒）→ 保留（旧管线会把它们抠穿，是已知缺陷）。
    最后做白边中和（defringe）去除 1px 白色包边。"""
    from scipy import ndimage
    im = Image.open(src_path).convert("RGB")
    w, h = im.size
    marker = (255, 0, 255)
    flood = im.copy()
    for xy in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1), (w // 2, 0), (w // 2, h - 1)]:
        if flood.getpixel(xy) != marker:
            ImageDraw.floodfill(flood, xy, marker, thresh=26)
    f = np.asarray(flood)
    ext = (f[:, :, 0] == 255) & (f[:, :, 1] == 0) & (f[:, :, 2] == 255)
    arr = np.asarray(im, np.int16)
    near_white = arr.min(axis=2) >= 240
    enclosed = near_white & ~ext
    lab, n = ndimage.label(enclosed)
    bg = ext.copy()
    if n:
        sizes = ndimage.sum(enclosed, lab, range(1, n + 1))
        big = {i + 1 for i, s in enumerate(sizes) if s > w * h * 0.004}
        if big:
            bg |= np.isin(lab, list(big))
    mask = Image.fromarray((bg * 255).astype(np.uint8), "L").filter(ImageFilter.GaussianBlur(1.1))
    alpha = np.asarray(mask, np.float32) / 255.0        # 1=背景
    rgb = np.asarray(im, np.float32)
    a = 1.0 - alpha                                      # 前景不透明度
    safe = np.maximum(a, 1e-3)[..., None]
    rgb = np.clip((rgb - 255.0 * alpha[..., None]) / safe, 0, 255)  # 去白色包边
    out = np.dstack([rgb, a[..., None] * 255.0]).astype(np.uint8)
    return Image.fromarray(out, "RGBA")

# ---------------------------------------------------------------- 文本渲染

def render_text(s, font, fill, tracking=0.0):
    """带字距的紧致文本图层（tracking 单位 px）。"""
    total = sum(font.getlength(ch) for ch in s) + tracking * max(0, len(s) - 1)
    asc, dsc = font.getmetrics()
    pad = 8
    layer = Image.new("RGBA", (int(total) + pad * 2, asc + dsc + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x = float(pad)
    for ch in s:
        d.text((x, pad + asc), ch, font=font, fill=fill, anchor="ls")
        x += font.getlength(ch) + tracking
    return autocrop(layer)

def render_en_matched(s, target_w, fill, wght=800, base_ratio=0.82):
    """英文字标：先按 target_w*base_ratio 选字号，再用字距铺满 target_w。"""
    size = 24
    f = font_mont(size, wght)
    nat = sum(f.getlength(ch) for ch in s)
    size = max(10, int(size * target_w * base_ratio / nat))
    f = font_mont(size, wght)
    nat = sum(f.getlength(ch) for ch in s)
    tr = (target_w - nat) / max(1, len(s) - 1)
    return render_text(s, f, fill, tracking=max(0.0, tr))

def paste_cx(canvas, layer, cx, y):
    canvas.paste(layer, (int(cx - layer.width / 2), int(y)), layer)
    return y + layer.height

# ---------------------------------------------------------------- 母版

MASTERS = {}

def build_masters():
    os.makedirs(KEYED, exist_ok=True)
    jobs = {"mark": "boundless-mark-white.png"}
    for k in PRODUCTS:
        jobs[k] = k + "-white.png"
    for name, fn in jobs.items():
        keyed = key_out_white(os.path.join(SRC, fn))
        keyed = autocrop(keyed, 0.02)
        MASTERS[name] = keyed
        save_png(keyed, os.path.join(KEYED, name + "-keyed.png"), "透明底母版（抠白，防高光穿孔）")

# ---------------------------------------------------------------- 01 主标

def build_logos():
    mark = MASTERS["mark"]
    for s in [1024, 512, 256, 128, 64, 32]:
        save_png(fit(mark, s, s), os.path.join(DIR_LOGOS, "mark", "boundless-mark-%d.png" % s),
                 "公司主标 透明底 %dpx" % s)
    # 单色剪影（水印 / 单色印刷 / 深浅底受限场景）
    for cname, col in [("white", WHITE), ("ink", INK)]:
        mono = Image.new("RGBA", mark.size, col + (0,))
        mono.putalpha(mark.getchannel("A"))
        save_png(fit(mono, 1024, 1024), os.path.join(DIR_LOGOS, "mono", "boundless-mark-mono-%s-1024.png" % cname),
                 "公司主标 单色%s版" % ("白" if cname == "white" else "墨"))
    # favicon
    sq = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    m = fit(mark, 960, 960)
    sq.paste(m, ((1024 - m.width) // 2, (1024 - m.height) // 2), m)
    ico_path = os.path.join(DIR_LOGOS, "favicon", "boundless.ico")
    os.makedirs(os.path.dirname(ico_path), exist_ok=True)
    sq.save(ico_path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    MANIFEST.append((os.path.relpath(ico_path, ROOT).replace("\\", "/"), "16-64", "网站 favicon（多尺寸 ico）"))
    print("[ok] 01_logos/favicon/boundless.ico")

def build_product_icons():
    for k in PRODUCTS:
        icon = MASTERS[k]
        for s in [512, 256, 128]:
            save_png(
                boxed_square(icon, s, 0.08),
                os.path.join(DIR_PICONS, k, "%s-%d.png" % (k, s)),
                "%s %s 图标 透明底正方形 %dpx（pad 8%%）" % (PRODUCTS[k]["zh"], PRODUCTS[k]["en"], s),
            )

# ---------------------------------------------------------------- 03 组合标

def company_lockup(orient="horizontal", text_color=INK, tagline=False):
    mark = MASTERS["mark"]
    zh_f = font_noto(300, "black")
    zh = render_text(COMPANY_ZH, zh_f, text_color, tracking=10)
    en = render_en_matched(COMPANY_EN, zh.width, text_color, wght=800)
    gap_ze = 40
    if orient == "horizontal":
        m = fit(mark, 10 ** 9, 900)
        block_h = zh.height + gap_ze + en.height
        gap = 100
        w = m.width + gap + max(zh.width, en.width)
        h = max(m.height, block_h)
        cv = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        cv.paste(m, (0, (h - m.height) // 2), m)
        ty = (h - block_h) // 2
        cv.paste(zh, (m.width + gap, ty), zh)
        cv.paste(en, (m.width + gap, ty + zh.height + gap_ze), en)
    else:  # stacked
        m = fit(mark, 10 ** 9, 780)
        parts_h = m.height + 90 + zh.height + gap_ze + en.height
        tg_zh = tg_en = None
        if tagline:
            tg_zh = render_text(TAGLINE_ZH, font_noto(110, "medium"), text_color, tracking=6)
            tg_en = render_text(TAGLINE_EN, font_mont(84, 500), text_color, tracking=2)
            parts_h += 70 + tg_zh.height + 26 + tg_en.height
        w = max(m.width, zh.width, en.width, 1300) + 120
        cv = Image.new("RGBA", (w, parts_h + 40), (0, 0, 0, 0))
        cx = w / 2
        y = 0
        y = paste_cx(cv, m, cx, y) + 90
        y = paste_cx(cv, zh, cx, y) + gap_ze
        y = paste_cx(cv, en, cx, y)
        if tagline:
            y += 70
            y = paste_cx(cv, tg_zh, cx, y) + 26
            paste_cx(cv, tg_en, cx, y)
    return autocrop(cv, 0.03)

def product_lockup(key, text_color=INK):
    p = PRODUCTS[key]
    icon = fit(MASTERS[key], 10 ** 9, 560)
    zh = render_text(p["zh"], font_noto(260, "black"), text_color, tracking=8)
    en = render_en_matched(p["en"], zh.width, text_color, wght=700, base_ratio=0.8)
    gap = 70
    block_h = zh.height + 34 + en.height
    w = icon.width + gap + max(zh.width, en.width)
    h = max(icon.height, block_h)
    cv = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    cv.paste(icon, (0, (h - icon.height) // 2), icon)
    ty = (h - block_h) // 2
    cv.paste(zh, (icon.width + gap, ty), zh)
    cv.paste(en, (icon.width + gap, ty + zh.height + 34), en)
    return autocrop(cv, 0.03)

def build_lockups():
    variants = [
        ("horizontal", False), ("stacked", False), ("stacked", True),
    ]
    for orient, tg in variants:
        for cname, col in [("ink", INK), ("white", WHITE)]:
            im = company_lockup(orient, col, tg)
            name = "company-%s%s-%s" % (orient, "-tagline" if tg else "", cname)
            save_png(fit(im, 2400, 2400), os.path.join(DIR_LOCKUPS, "company", name + ".png"),
                     "公司组合标 %s%s（%s字，透明底）" % (orient, " + 口号" if tg else "", "墨色" if cname == "ink" else "白色"))
    for k in PRODUCTS:
        for cname, col in [("ink", INK), ("white", WHITE)]:
            im = product_lockup(k, col)
            save_png(fit(im, 1800, 1800), os.path.join(DIR_LOCKUPS, "products", "%s-lockup-%s.png" % (k, cname)),
                     "%s %s 组合标（%s字，透明底）" % (PRODUCTS[k]["zh"], PRODUCTS[k]["en"], "墨色" if cname == "ink" else "白色"))

# ---------------------------------------------------------------- 04 头像

def avatar_canvas(size, dark=True):
    if dark:
        cv = vgrad(size, size, DARK_TOP, DARK_BOT)
        cv = add_glow(cv, size * 0.18, size * 0.10, size * 0.85, BLUE, 0.22)
        cv = add_glow(cv, size * 0.88, size * 0.92, size * 0.85, ORANGE, 0.20)
        cv = add_glow(cv, size * 0.80, size * 0.20, size * 0.70, MAGENTA, 0.10)
    else:
        cv = vgrad(size, size, LIGHT_TOP, LIGHT_BOT)
        cv = add_glow(cv, size * 0.15, size * 0.08, size * 0.8, (200, 220, 255), 0.35)
    return cv

def draw_ring(cv, stops, inset_ratio=0.028, thick_ratio=0.055):
    size = cv.width
    ss = 3
    big = size * ss
    layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    inset = big * inset_ratio
    thick = int(big * thick_ratio)
    bbox = [inset + thick / 2, inset + thick / 2, big - inset - thick / 2, big - inset - thick / 2]
    steps = 1080
    for i in range(steps):
        t = i / steps
        a0 = -90 + 360 * t
        a1 = a0 + 360 / steps + 0.6
        d.arc(bbox, a0, a1, fill=color_at(stops, t), width=thick)
    layer = layer.resize((size, size), Image.LANCZOS)
    cv.paste(layer, (0, 0), layer)
    return cv

def put_mark(cv, art, pad_ratio, dy_ratio=0.0):
    size = cv.width
    box = int(size * (1 - 2 * pad_ratio))
    m = fit(art, box, box)
    x = (size - m.width) // 2
    y = int((size - m.height) / 2 + size * dy_ratio)
    cv.paste(m, (x, y), m)
    return cv

def badge_pill(cv, text, zh=True):
    """底部胶囊徽标（客服 / SUPPORT）。"""
    size = cv.width
    pw, ph = int(size * 0.56), int(size * 0.165)
    cx, cy = size // 2, int(size * 0.815)
    # 阴影
    sh = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([cx - pw // 2, cy - ph // 2 + 6, cx + pw // 2, cy + ph // 2 + 6],
                                         radius=ph // 2, fill=(0, 0, 0, 130))
    cv.alpha_composite(sh.filter(ImageFilter.GaussianBlur(size * 0.012)))
    # 渐变胶囊
    grad = np.linspace(0.0, 1.0, pw, dtype=np.float32)
    row = np.array([color_at([MAGENTA, PINK, ORANGE], t) for t in grad], np.uint8)[None, :, :]
    pill = Image.fromarray(np.repeat(row, ph, axis=0), "RGB").convert("RGBA")
    mask = Image.new("L", (pw, ph), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, pw - 1, ph - 1], radius=ph // 2, fill=255)
    cv.paste(pill, (cx - pw // 2, cy - ph // 2), mask)
    f = font_noto(ph * 0.56, "bold") if zh else font_mont(ph * 0.46, 700)
    t = render_text(text, f, WHITE, tracking=(6 if zh else 3))
    cv.paste(t, (cx - t.width // 2, cy - t.height // 2), t)
    return cv

def build_avatars():
    S = 1024

    def out(img, sub, name, purpose):
        save_png(img.resize((512, 512), Image.LANCZOS), os.path.join(DIR_AVATARS, sub, name + "-512.png"), purpose)
        save_png(img.resize((128, 128), Image.LANCZOS), os.path.join(DIR_AVATARS, sub, "thumbs", name + "-128.png"),
                 purpose + "（128 预览）")

    mark = MASTERS["mark"]
    # 公司 · 通用（深/浅）
    out(put_mark(avatar_canvas(S, True), mark, 0.185), "company", "avatar-brand-dark",
        "公司主头像 深空底（TG Bot/官方号通用）")
    out(put_mark(avatar_canvas(S, False), mark, 0.185), "company", "avatar-brand-light",
        "公司主头像 浅色底（浅色场景备用）")
    # 公司 · 资源号（渐变光环 = 官方频道识别符）
    cv = avatar_canvas(S, True)
    cv = draw_ring(cv, RING_STOPS)
    out(put_mark(cv, mark, 0.26), "company", "avatar-channel-ring",
        "资源号/频道头像 渐变光环版（官方识别符）")
    # 公司 · 客服号（底部客服徽标）
    cv = put_mark(avatar_canvas(S, True), mark, 0.26, dy_ratio=-0.075)
    out(badge_pill(cv, "客服", zh=True), "company", "avatar-support-zh", "客服号头像 中文徽标")
    cv = put_mark(avatar_canvas(S, True), mark, 0.26, dy_ratio=-0.075)
    out(badge_pill(cv, "SUPPORT", zh=False), "company", "avatar-support-en", "客服号头像 英文徽标")
    # 产品号（普通 + 光环资源号）
    for k, p in PRODUCTS.items():
        icon = MASTERS[k]
        out(put_mark(avatar_canvas(S, True), icon, 0.16), "products", "avatar-%s-dark" % k,
            "%s %s 产品号头像" % (p["zh"], p["en"]))
        cv = avatar_canvas(S, True)
        cv = draw_ring(cv, CATEGORIES[p["cat"]]["ring"])
        out(put_mark(cv, icon, 0.235), "products", "avatar-%s-ring" % k,
            "%s %s 产品资源号头像（%s系光环）" % (p["zh"], p["en"], CATEGORIES[p["cat"]]["zh"]))

# ---------------------------------------------------------------- 05 背景

def bg_base(w, h, style="dark"):
    if style == "dark":
        cv = vgrad(w, h, C("11142C"), DARK_BOT)
        cv = add_glow(cv, -w * 0.05, -h * 0.15, max(w, h) * 0.75, C("1E4BC8"), 0.35)
        cv = add_glow(cv, w * 1.02, h * 1.05, max(w, h) * 0.8, C("C84800"), 0.30)
        cv = add_glow(cv, w * 0.85, h * 0.12, max(w, h) * 0.45, C("8A20B0"), 0.16)
        dot_grid(cv, max(36, w // 34), 1.6, (255, 255, 255, 12))
    else:
        cv = vgrad(w, h, LIGHT_TOP, LIGHT_BOT)
        cv = add_glow(cv, -w * 0.05, -h * 0.2, max(w, h) * 0.7, (210, 228, 255), 0.5)
        cv = add_glow(cv, w * 1.02, h * 1.1, max(w, h) * 0.75, (255, 224, 200), 0.45)
        dot_grid(cv, max(36, w // 34), 1.6, (11, 16, 32, 14))
    return cv

def add_watermark(cv, k=0.07):
    w, h = cv.size
    m = fit(MASTERS["mark"], 10 ** 9, int(h * 1.12))
    wm = scale_alpha(m, k)
    cv.alpha_composite(wm, (int(w - m.width * 0.46), int((h - m.height) / 2)))
    return cv

def product_strip(cv, y_center, icon_h):
    w = cv.width
    icons = [fit(MASTERS[k], 10 ** 9, icon_h) for k in PRODUCTS]
    gap = int(icon_h * 0.42)
    total = sum(i.width for i in icons) + gap * (len(icons) - 1)
    x = (w - total) // 2
    for i in icons:
        cv.paste(i, (x, int(y_center - i.height / 2)), i)
        x += i.width + gap
    return cv

def lockup_block(style, max_w, mark_h, zh_size, dark=True, tagline=True):
    """居中竖排：标 + 公司名 + 英文 + 口号（返回透明图层）。"""
    col = WHITE if dark else INK
    sub = (255, 255, 255, 200) if dark else INK + (200,)
    mark = fit(MASTERS["mark"], 10 ** 9, mark_h)
    zh = render_text(COMPANY_ZH, font_noto(zh_size, "black"), col, tracking=8)
    en = render_en_matched(COMPANY_EN, zh.width, col, wght=800)
    items = [(mark, int(mark_h * 0.14)), (zh, int(zh_size * 0.13)), (en, 0)]
    if tagline:
        tg_zh = render_text(TAGLINE_ZH, font_noto(zh_size * 0.36, "medium"), sub, tracking=4)
        tg_en = render_text(TAGLINE_EN, font_mont(zh_size * 0.27, 500), sub, tracking=1.5)
        items += [(None, int(zh_size * 0.24)), (tg_zh, int(zh_size * 0.09)), (tg_en, 0)]
    tw = max(i.width for i, _ in items if i is not None)
    th = sum((i.height if i is not None else 2) + g for i, g in items)
    layer = Image.new("RGBA", (min(tw + 20, max_w), th + 10), (0, 0, 0, 0))
    cx = layer.width / 2
    y = 0
    for i, g in items:
        if i is None:  # 分隔线
            d = ImageDraw.Draw(layer)
            lw = zh.width * 0.5
            d.line([cx - lw / 2, y + 1, cx + lw / 2, y + 1], fill=(col + (70,)) if len(col) == 3 else col, width=2)
            y += 2 + g
        else:
            y = paste_cx(layer, i, cx, y) + g
    return layer

def lockup_row(mark_h, dark=True):
    """横排：标 + 右侧 中文/英文 两行（返回透明图层）。"""
    col = WHITE if dark else INK
    mark = fit(MASTERS["mark"], 10 ** 9, mark_h)
    zh = render_text(COMPANY_ZH, font_noto(mark_h * 0.40, "black"), col, tracking=6)
    en = render_en_matched(COMPANY_EN, zh.width, col, wght=800)
    gap = int(mark_h * 0.13)
    block_h = zh.height + int(mark_h * 0.05) + en.height
    w = mark.width + gap + zh.width + 10
    h = max(mark.height, block_h)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    layer.paste(mark, (0, (h - mark.height) // 2), mark)
    ty = (h - block_h) // 2
    layer.paste(zh, (mark.width + gap, ty), zh)
    layer.paste(en, (mark.width + gap, ty + zh.height + int(mark_h * 0.05)), en)
    return layer

def tagline_row(size, dark=True):
    col = (255, 255, 255, 205) if dark else INK + (205,)
    zh = render_text(TAGLINE_ZH, font_noto(size, "medium"), col, tracking=3)
    dot = render_text("·", font_noto(size, "medium"), col)
    en = render_text(TAGLINE_EN, font_mont(size * 0.78, 500), col, tracking=1)
    gap = int(size * 0.45)
    w = zh.width + gap + dot.width + gap + en.width
    h = max(zh.height, en.height, dot.height)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    x = 0
    for seg in [zh, dot, en]:
        layer.paste(seg, (x, (h - seg.height) // 2), seg)
        x += seg.width + gap
    return layer

def build_backgrounds():
    # ---- 居中类（TG 贴文 / 桌面）----
    for name, w, h, style, strip in [
        ("tg-post-1280x720", 1280, 720, "dark", True),
        ("desktop-1920x1080-dark", 1920, 1080, "dark", True),
        ("desktop-1920x1080-light", 1920, 1080, "light", True),
    ]:
        cv = bg_base(w, h, style)
        cv = add_watermark(cv, 0.06 if style == "dark" else 0.05)
        blk = lockup_block("stack", int(w * 0.8), int(h * 0.30), int(h * 0.115), dark=(style == "dark"))
        cv.alpha_composite(blk, (int((w - blk.width) / 2), int(h * 0.42 - blk.height / 2)))
        if strip:
            product_strip(cv, h * 0.885, int(h * 0.075))
        save_png(cv.convert("RGB").convert("RGBA"), os.path.join(DIR_BG, name + ".png"),
                 {"tg-post-1280x720": "TG 频道贴文 / OG 分享图（16:9）",
                  "desktop-1920x1080-dark": "桌面壁纸 / 直播垫片 / PPT 封面（深色）",
                  "desktop-1920x1080-light": "桌面壁纸 / PPT 封面（浅色）"}[name])

    # ---- 竖屏故事 / 手机壁纸 / 朋友圈背景 ----
    w, h = 1080, 1920
    cv = bg_base(w, h, "dark")
    m = fit(MASTERS["mark"], 10 ** 9, int(h * 0.13))
    paste_cx(cv, m, w / 2, int(h * 0.155))
    zh = render_text(COMPANY_ZH, font_noto(150, "black"), WHITE, tracking=10)
    y = paste_cx(cv, zh, w / 2, int(h * 0.335)) + 22
    en = render_en_matched(COMPANY_EN, zh.width, WHITE, wght=800)
    y = paste_cx(cv, en, w / 2, y) + 90
    tg_zh = render_text(TAGLINE_ZH, font_noto(64, "medium"), (255, 255, 255, 210), tracking=4)
    y = paste_cx(cv, tg_zh, w / 2, y) + 18
    tg_en = render_text(TAGLINE_EN, font_mont(46, 500), (255, 255, 255, 190), tracking=1.5)
    paste_cx(cv, tg_en, w / 2, y)
    product_strip(cv, h * 0.755, 96)
    ft = render_text("BOUNDLESS TECHNOLOGY", font_mont(40, 600), (255, 255, 255, 110), tracking=14)
    paste_cx(cv, ft, w / 2, int(h * 0.925))
    save_png(cv, os.path.join(DIR_BG, "story-1080x1920.png"), "TG/IG 故事 · 手机壁纸 · 朋友圈封面（竖屏）")

    # ---- 左排横幅类 ----
    for name, w, h, style, mh, tg_size, purpose in [
        ("x-header-1500x500", 1500, 500, "dark", 210, 40, "X(Twitter) 资源号头图"),
        ("facebook-cover-820x312", 820, 312, "dark", 128, 26, "Facebook 主页封面"),
        ("wechat-banner-900x383-dark", 900, 383, "dark", 150, 30, "公众号封面 / 横版卡片（深色）"),
        ("wechat-banner-900x383-light", 900, 383, "light", 150, 30, "公众号封面 / 横版卡片（浅色）"),
    ]:
        cv = bg_base(w, h, style)
        cv = add_watermark(cv, 0.06 if style == "dark" else 0.05)
        row = lockup_row(mh, dark=(style == "dark"))
        x0 = int(w * 0.055)
        cv.alpha_composite(row, (x0, int(h * 0.40 - row.height / 2)))
        tr = tagline_row(tg_size, dark=(style == "dark"))
        cv.alpha_composite(tr, (x0 + int(mh * 0.02), int(h * 0.40 + row.height / 2 + h * 0.055)))
        save_png(cv, os.path.join(DIR_BG, name + ".png"), purpose)

    # ---- YouTube 横幅（安全区 1546x423 居中）----
    w, h = 2560, 1440
    cv = bg_base(w, h, "dark")
    cv = add_watermark(cv, 0.05)
    row = lockup_row(240, dark=True)
    safe_cx, safe_cy = w / 2, h / 2
    cv.alpha_composite(row, (int(safe_cx - row.width / 2), int(safe_cy - row.height / 2 - 40)))
    tr = tagline_row(44, dark=True)
    cv.alpha_composite(tr, (int(safe_cx - tr.width / 2), int(safe_cy + row.height / 2 + 20)))
    save_png(cv, os.path.join(DIR_BG, "youtube-banner-2560x1440.png"),
             "YouTube 频道横幅（关键内容已置于 1546x423 安全区内）")

def build_matrix_poster():
    w, h = 1920, 1080
    cv = bg_base(w, h, "dark")
    margin = 80
    # 头部：横排组合标 + 右侧口号
    row = lockup_row(150, dark=True)
    cv.alpha_composite(row, (margin, 66))
    tr = tagline_row(30, dark=True)
    cv.alpha_composite(tr, (w - margin - tr.width, 66 + row.height // 2 - tr.height // 2))
    sub = render_text("三大产品系 · 七款产品 · 打破每一种「界」", font_noto(40, "bold"), (255, 255, 255, 225), tracking=4)
    cv.paste(sub, (margin, 66 + row.height + 34), sub)
    # 三列卡片（画在独立 overlay 上再 alpha_composite——ImageDraw 直接带 alpha
    # 画到画布会“替换”像素而非叠加，导致卡片区域透明、白字不可见）
    top = 330
    card_h = 620
    gap = 40
    card_w = (w - margin * 2 - gap * 2) // 3
    overlay = Image.new("RGBA", cv.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    x = margin
    for ck in ["growth", "studio", "lingo"]:
        cat = CATEGORIES[ck]
        od.rounded_rectangle([x, top, x + card_w, top + card_h], radius=28, fill=(255, 255, 255, 26),
                             outline=(255, 255, 255, 48), width=2)
        od.rounded_rectangle([x + 28, top + 24, x + 28 + 64, top + 30], radius=3, fill=cat["accent"] + (255,))
        x += card_w + gap
    cv.alpha_composite(overlay)
    x = margin
    for ck in ["growth", "studio", "lingo"]:
        cat = CATEGORIES[ck]
        head_zh = render_text(cat["zh"], font_noto(62, "black"), WHITE, tracking=4)
        head_en = render_text(cat["en"].upper(), font_mont(34, 700), cat["accent"] + (255,), tracking=6)
        cv.paste(head_zh, (x + 28, top + 52), head_zh)
        cv.paste(head_en, (x + 28 + head_zh.width + 22, top + 52 + head_zh.height - head_en.height - 10), head_en)
        tag = render_text(cat["tag"], font_noto(28, "medium"), (255, 255, 255, 165), tracking=2)
        cv.paste(tag, (x + 28, top + 52 + head_zh.height + 12), tag)
        # 产品行
        py = top + 210
        for k, p in PRODUCTS.items():
            if p["cat"] != ck:
                continue
            icon = fit(MASTERS[k], 96, 96)
            cv.paste(icon, (x + 30, py + (110 - icon.height) // 2), icon)
            nm_zh = render_text(p["zh"], font_noto(42, "bold"), WHITE, tracking=2)
            nm_en = render_text(p["en"], font_mont(30, 700), cat["accent"] + (255,), tracking=2)
            cv.paste(nm_zh, (x + 30 + 110, py + 8), nm_zh)
            cv.paste(nm_en, (x + 30 + 110 + nm_zh.width + 16, py + 8 + nm_zh.height - nm_en.height - 4), nm_en)
            ds = render_text(p["desc"], font_noto(26, "medium"), (255, 255, 255, 150), tracking=1)
            cv.paste(ds, (x + 30 + 110, py + 14 + nm_zh.height), ds)
            py += 128
        x += card_w + gap
    ft = render_text("无界底座 BOUNDLESS Engine — 一套底座，托起三系七款产品",
                     font_noto(30, "medium"), (255, 255, 255, 140), tracking=2)
    paste_cx(cv, ft, w / 2, h - 68)
    save_png(cv, os.path.join(DIR_BG, "matrix-poster-1920x1080.png"),
             "产品矩阵总览海报（母品牌 + 三系七产品，一图讲清）")

# ---------------------------------------------------------------- MANIFEST

def write_manifest():
    lines = ["# 无界科技 BOUNDLESS · 品牌资产清单（自动生成）", "",
             "重新生成：`python build_brand_assets.py`", "",
             "| 文件 | 尺寸 | 用途 |", "|---|---|---|"]
    for rel, size, purpose in MANIFEST:
        lines.append("| `%s` | %s | %s |" % (rel, size, purpose))
    with open(os.path.join(ROOT, "MANIFEST.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("[ok] MANIFEST.md (%d files)" % len(MANIFEST))

def main():
    build_masters()
    build_logos()
    build_product_icons()
    build_lockups()
    build_avatars()
    build_backgrounds()
    build_matrix_poster()
    write_manifest()
    print("DONE.")

if __name__ == "__main__":
    main()
