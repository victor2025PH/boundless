# -*- coding: utf-8 -*-
"""生成品牌化应用图标 assets/app.ico（多尺寸）与 assets/app.png。

图标源 = 品牌 ∞ 主标 assets/brand/boundless-mark-512.png，合成到深空色圆角瓦片上——
与 App 内 Hero / 系统托盘同一张脸，桌面快捷方式、任务栏、卸载列表全线统一。
主标占满瓦片 ~84%（而非官方头像那种"小标 + 大片留白"），因此 16px 托盘/文件列表里也认得出。
换品牌只需替换 assets/brand 母版后重跑本文件；主标缺失时回退到官方头像母版
avatar-brand-dark-512.png（保证任何时候都能出一张品牌图标，不再回落旧绿色人物图）。
"""
from pathlib import Path
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
BRAND = HERE / "brand"
CANVAS = 512  # 高分辨率母版，再向下生成各尺寸，边缘更平滑
# Windows 各视图所需尺寸：任务栏/大图标要 256/128，小图标/托盘要 16/32/48/64
ICO_SIZES = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]

# 回退合成用的品牌深色底（与 launcher 深色主题一致的深空蓝）
BG_TOP = (22, 26, 42)
BG_BOT = (12, 14, 24)


def _round_rect_mask(size: int, radius: int) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def _vertical_gradient(size: int, top, bot) -> Image.Image:
    base = Image.new("RGB", (size, size), top)
    d = ImageDraw.Draw(base)
    for y in range(size):
        t = y / (size - 1)
        d.line([(0, y), (size, y)],
               fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    return base


def _compose_from_mark() -> Image.Image:
    """深空色圆角瓦片 + 居中放大的 ∞ 主标（主方案）。主标占满 ~84%，
    比官方头像"小标+大留白"在 16/32px 小图标下清晰得多。"""
    img = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    bg = _vertical_gradient(CANVAS, BG_TOP, BG_BOT).convert("RGBA")
    img.paste(bg, (0, 0), _round_rect_mask(CANVAS, int(CANVAS * 0.22)))
    mark = Image.open(BRAND / "boundless-mark-512.png").convert("RGBA")
    box = mark.split()[3].getbbox()   # 去掉透明边，让主标占满可用空间
    if box:
        mark = mark.crop(box)
    w = int(CANVAS * 0.84)
    h = max(1, int(w * mark.height / mark.width))
    mark = mark.resize((w, h), Image.LANCZOS)
    img.alpha_composite(mark, ((CANVAS - w) // 2, (CANVAS - h) // 2))
    return img


def build() -> Image.Image:
    """主方案：把 ∞ 主标合成到深空瓦片上（小尺寸也清晰）。
    主标母版缺失时回退到官方头像母版 avatar-brand-dark-512.png。"""
    if (BRAND / "boundless-mark-512.png").exists():
        return _compose_from_mark()
    master = BRAND / "avatar-brand-dark-512.png"
    img = Image.open(master).convert("RGBA")
    if img.size != (CANVAS, CANVAS):
        img = img.resize((CANVAS, CANVAS), Image.LANCZOS)
    return img


def main():
    master = build()
    png = HERE / "app.png"
    master.resize((256, 256), Image.LANCZOS).save(png)
    ico = HERE / "app.ico"
    # 逐尺寸用 LANCZOS 预生成帧，作为 append_images 直接嵌入，保证每档都清晰（不依赖编码器默认缩放质量）
    frames = [master.resize(s, Image.LANCZOS) for s in ICO_SIZES]
    frames[0].save(ico, format="ICO", sizes=ICO_SIZES, append_images=frames[1:])
    print(f"saved {ico} and {png}")


if __name__ == "__main__":
    main()
