# -*- coding: utf-8 -*-
"""智聊 ChatX Windows 安装器（electron-builder / NSIS MUI2）品牌位图生成。

与 build_studio_icon.py 同类：客户端级资产，刻意不进 build_brand_assets 主管线
（主管线一跑重刷全部品牌资产；安装器位图只跟安装器一起变）。复用主管线的
渐变 / 辉光 / 点阵 / 文本渲染函数与品牌常量，母版取 00_master/keyed/。

产出（07_installer/）：
  chatx-installer-sidebar.bmp   382x820  MUI_WELCOMEFINISHPAGE_BITMAP（欢迎/完成页左栏）
  chatx-installer-header.bmp    350x148  MUI_HEADERIMAGE_BITMAP（内页页眉右侧）
  *-internal.bmp                 同图 + 琥珀「内测版 INTERNAL」胶囊（非 clean 形态的安装包用）
  *.png                          同图 PNG 预览
  *-1x-nearest.png               按 NSIS 实际缩放方式（StretchBlt 最近邻）缩到 1x 的效果预览
并镜像到 engines/chengjie/desktop/build/installerSidebar[-internal].bmp /
installerHeader[-internal].bmp（package.json build.nsis.installerSidebar / installerHeader 指向
干净版；installer.nsh 按 build/flavor.nsh 的 CX_FLAVOR 在编译期换成 -internal 变体）。

规格约束（改图前先读）：
  · 尺寸 = 控件真实像素 x2。MUI 的位图控件按对话框单位定尺，对话框单位随字体变：
    installer.nsh 用 SetFont 把中英文都定为 Microsoft YaHei UI 9pt，在 96dpi 下侧栏控件
    实测 191x410、页眉控件 175x74（2026-09-05 EnumChildWindows 量得）——不是 NSIS 文档里
    MS Shell Dlg 8pt 的 164x314 / 150x57，按那个出图 ChatX 图标会被横向挤压 ~11%。
    改字体必须回来重量尺寸（installer 门禁钉着这两个数）。
  · 安装器开了 ManifestDPIAware，MUI 以 FitControl 把位图拉到控件大小：200% 缩放的坐席机
    1:1 显示，100% 机器由 Windows 最近邻缩小——所以构图只用软渐变 / 大字 / 低对比点阵，
    不用 1px 细节。
  · 必须 24 位 BMP、无 alpha（RGBA 位图在 STM_SETIMAGE 下会发黑）。
  · 页眉底色必须与 installer.nsh 的 MUI_BGCOLOR（FFFFFF）一致，否则页眉带出现色块拼缝。
  · 颜色全部取 platform/brand/tokens.json：深空底 #1A1D3A→#05060F、∞ 七色渐变、
    辉光紫 #7A3BF5 / 蓝 #1E6BF0、次级文字 #8A9BB0（与 desktop splash.css 同值）。

重跑：python build_installer_art.py
"""

import os
import shutil
import sys

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from build_brand_assets import (  # noqa: E402
    C, WHITE, CYAN, BLUE, VIOLET, MAGENTA, PINK, ORANGE, AMBER,
    COMPANY_ZH, COMPANY_EN, TAGLINE_ZH,
    font_noto, font_mont, vgrad, add_glow, dot_grid, fit,
    render_text, paste_cx,
)

KEYED = os.path.join(ROOT, "00_master", "keyed")
OUT = os.path.join(ROOT, "07_installer")
DESKTOP_BUILD = os.path.normpath(os.path.join(
    ROOT, "..", "engines", "chengjie", "desktop", "build"))

# YaHei UI 9pt 下的控件真实像素 x2（见文件头）
SIDEBAR_W, SIDEBAR_H = 191 * 2, 410 * 2
HEADER_W, HEADER_H = 175 * 2, 74 * 2

DARK_TOP = C("1A1D3A")      # tokens.brand.darkSpace.top
DARK_BOT = C("05060F")      # tokens.brand.darkSpace.bottom
TEXT_SUB = (138, 155, 176)  # splash.css .sp-word-sub / .sp-ver
BRAND_STOPS = [CYAN, BLUE, VIOLET, MAGENTA, PINK, ORANGE, AMBER]  # tokens.brand.gradient.stops


def gradient_fill(layer, stops, vertical=False):
    """用 ∞ 渐变给一张透明图层的可见像素上色（保留原 alpha）。"""
    w, h = layer.size
    grad = Image.new("RGBA", (w, h))
    d = ImageDraw.Draw(grad)
    n = h if vertical else w
    for i in range(n):
        t = i / max(1, n - 1)
        k = t * (len(stops) - 1)
        j = min(int(k), len(stops) - 2)
        f = k - j
        c = tuple(int(round(stops[j][q] + (stops[j + 1][q] - stops[j][q]) * f)) for q in range(3))
        if vertical:
            d.line([(0, i), (w, i)], fill=c + (255,))
        else:
            d.line([(i, 0), (i, h)], fill=c + (255,))
    grad.putalpha(layer.getchannel("A"))
    return grad


def soft_dot_grid(cv, spacing, r, alpha):
    """真正 alpha 混合的点阵（主管线 dot_grid 直接写像素，convert("RGB") 后会变成纯白点）。"""
    layer = Image.new("RGBA", cv.size, (0, 0, 0, 0))
    dot_grid(layer, spacing, r, (255, 255, 255, alpha))
    cv.alpha_composite(layer)
    return cv


def load_keyed(name):
    return Image.open(os.path.join(KEYED, name + "-keyed.png")).convert("RGBA")


def build_sidebar():
    w, h = SIDEBAR_W, SIDEBAR_H
    cv = vgrad(w, h, DARK_TOP, DARK_BOT)
    # 中心辉光：紫为主、蓝为辅（splash.css ::before 两层 radial-gradient 的位图版）
    cv = add_glow(cv, w * 0.5, h * 0.31, w * 0.95, VIOLET, 0.30)
    cv = add_glow(cv, w * 0.5, h * 0.34, w * 0.60, BLUE, 0.24)
    # 品牌暖端在右下角轻微收尾（与 bg_base 的橙色角光同源，强度压低）
    cv = add_glow(cv, w * 1.05, h * 1.02, w * 0.9, ORANGE, 0.10)
    # 低对比点阵：只做质感，不做细节（半径 2.4 保证最近邻缩到 1x 后每个点都还在，
    # alpha 压到 26 让它退到肉眼阈值附近，缩放不均匀也看不出）
    soft_dot_grid(cv, 40, 2.4, 26)

    # 左缘 ∞ 渐变竖条（品牌签名色带；1x 下 2px）
    strip = Image.new("RGBA", (4, h), (255, 255, 255, 255))
    cv.alpha_composite(gradient_fill(strip, BRAND_STOPS, vertical=True), (0, 0))

    # ChatX 产品标：先在标后垫一层紫/蓝辉光（splash .sp-cx drop-shadow 的位图等价）
    mark = fit(load_keyed("chatx"), int(w * 0.54))
    mark_cx, mark_cy = w / 2, h * 0.31
    cv = add_glow(cv, mark_cx, mark_cy, mark.width * 0.9, MAGENTA, 0.30)
    cv = add_glow(cv, mark_cx, mark_cy, mark.width * 1.4, BLUE, 0.22)
    y = paste_cx(cv, mark, mark_cx, mark_cy - mark.height / 2)

    # 字标：智聊（Noto Black 白）+ ChatX（Montserrat 800 自然宽度 + 轻字距，∞ 渐变填充；
    # 与 splash .sp-cx-en 的 letter-spacing .1em 同语汇，不做撑满对齐——撑开后像散字）
    zh = render_text("智聊", font_noto(54, "black"), WHITE, tracking=9)
    y = paste_cx(cv, zh, w / 2, y + int(h * 0.035)) + 10
    en_white = render_text("ChatX", font_mont(34, 800), WHITE, tracking=3)
    en = gradient_fill(en_white, BRAND_STOPS)
    y = paste_cx(cv, en, w / 2, y) + int(h * 0.045)

    # 口号（次级文字色）
    tg = render_text(TAGLINE_ZH, font_noto(29, "medium"), TEXT_SUB + (235,), tracking=5)
    paste_cx(cv, tg, w / 2, y)

    # 底部母品牌落款：无界科技 / BOUNDLESS 两行（Montserrat 字距拉开，与 splash .sp-ver 同语汇）
    co_zh = render_text(COMPANY_ZH, font_noto(23, "bold"), TEXT_SUB + (200,), tracking=6)
    co_en = render_text(COMPANY_EN, font_mont(16, 700), TEXT_SUB + (170,), tracking=5)
    by = h - int(h * 0.075) - co_zh.height - co_en.height - 6
    by = paste_cx(cv, co_zh, w / 2, by) + 6
    paste_cx(cv, co_en, w / 2, by)
    return cv.convert("RGB")


def build_header():
    w, h = HEADER_W, HEADER_H
    cv = Image.new("RGBA", (w, h), WHITE + (255,))
    mark = fit(load_keyed("chatx"), 10 ** 9, int(h * 0.74))
    cv.alpha_composite(mark, (w - mark.width - 16, (h - mark.height) // 2))
    return cv.convert("RGB")


def badge_pill(text_zh, text_en, scale=1.0):
    """「内测版 INTERNAL」琥珀胶囊（tokens.semantic.warning-500 #F59E0B 底 + 墨字）。"""
    zh = render_text(text_zh, font_noto(int(24 * scale), "bold"), C("0B1020"), tracking=2)
    en = render_text(text_en, font_mont(int(14 * scale), 700), C("0B1020"), tracking=3)
    pad_x, gap = int(18 * scale), int(12 * scale)
    w = pad_x * 2 + zh.width + gap + en.width
    h = int(40 * scale)
    pill = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(pill).rounded_rectangle([0, 0, w - 1, h - 1], radius=h // 2, fill=C("F59E0B") + (255,))
    pill.alpha_composite(zh, (pad_x, (h - zh.height) // 2))
    pill.alpha_composite(en, (pad_x + zh.width + gap, (h - en.height) // 2 + 1))
    return pill


def with_internal_badge(img, corner="top-right", scale=1.0, margin=18):
    """在已合成的 RGB 位图上叠内测角标（非 clean 形态专用变体）。"""
    cv = img.convert("RGBA")
    pill = badge_pill("内测版", "INTERNAL", scale)
    if corner == "top-right":
        pos = (cv.width - pill.width - margin, margin)
    else:  # header: left of the mark, vertically centred
        pos = (margin, (cv.height - pill.height) // 2)
    cv.alpha_composite(pill, pos)
    return cv.convert("RGB")


def save_all(img, stem, one_x):
    os.makedirs(OUT, exist_ok=True)
    bmp = os.path.join(OUT, stem + ".bmp")
    img.save(bmp, "BMP")  # RGB -> 24 位 BMP
    img.save(os.path.join(OUT, stem + ".png"), "PNG")
    # NSIS/Windows 实际缩放方式的预览：最近邻缩到 1x，肉眼核对无锯齿/无文字发碎
    img.resize(one_x, Image.NEAREST).save(os.path.join(OUT, stem + "-1x-nearest.png"), "PNG")
    print("[ok] %s  %dx%d" % (os.path.relpath(bmp, ROOT), img.width, img.height))
    return bmp


def main():
    side_img, head_img = build_sidebar(), build_header()
    one_x_side, one_x_head = (SIDEBAR_W // 2, SIDEBAR_H // 2), (HEADER_W // 2, HEADER_H // 2)
    outputs = [
        (save_all(side_img, "chatx-installer-sidebar", one_x_side), "installerSidebar.bmp"),
        (save_all(head_img, "chatx-installer-header", one_x_head), "installerHeader.bmp"),
        # 非 clean 形态（内测/lite）变体：同图 + 琥珀「内测版」胶囊，installer.nsh 按
        # build/flavor.nsh 的 CX_FLAVOR 选用（缺文件按内测处理，角标常亮）
        (save_all(with_internal_badge(side_img, "top-right", 1.0), "chatx-installer-sidebar-internal", one_x_side),
         "installerSidebar-internal.bmp"),
        (save_all(with_internal_badge(head_img, "left", 0.8, 14), "chatx-installer-header-internal", one_x_head),
         "installerHeader-internal.bmp"),
    ]
    if os.path.isdir(DESKTOP_BUILD):
        for src, dst in outputs:
            target = os.path.join(DESKTOP_BUILD, dst)
            shutil.copyfile(src, target)
            print("[ok] -> " + os.path.relpath(target, os.path.dirname(ROOT)))
    else:
        print("[skip] desktop/build 不存在，未镜像: " + DESKTOP_BUILD)
    print("DONE.")


if __name__ == "__main__":
    main()
