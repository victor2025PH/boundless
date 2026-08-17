# -*- coding: utf-8 -*-
"""export_jpg.py — 品牌库 JPG 导出（一键重跑，产物落 06_jpg/ 同结构镜像）。

为什么单列一个导出器：库内产物是透明底 PNG（网页/设计场景的正确形态），但不少
投放位只收 JPG。转 JPG 必须垫底色，规则：
  · 墨字/彩色透明件（*-ink、主标、产品图标）→ 垫白底；
  · 白字/白剪影件（文件名含 -white）→ 垫深空墨底 #0B1020（垫白底=白字消失）；
  · 本就不透明的（头像/背景/壁纸/海报）→ 直转。
范围：01_logos / 02_product-icons / 03_lockups / 04_avatars / 05_backgrounds
（跳过 00_master 内部母版、fonts、_retired_v1 归档、ico/svg）。
用法：python export_jpg.py   （质量 92；重跑=整目录刷新）
"""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "06_jpg"
QUALITY = 92
WHITE = (255, 255, 255)
DARK = (11, 16, 32)   # 深空墨底（与品牌墨色同族），专垫白字/白剪影件

SCAN = ["01_logos", "02_product-icons", "03_lockups", "04_avatars", "05_backgrounds"]


def bg_for(rel: str):
    return DARK if "-white" in rel.replace("\\", "/").lower() else WHITE


def main():
    n = 0
    for top in SCAN:
        base_dir = ROOT / top
        if not base_dir.is_dir():
            continue
        for p in sorted(base_dir.rglob("*.png")):
            rel = p.relative_to(ROOT)
            if "_retired_v1" in rel.parts:
                continue
            dst = OUT / rel.with_suffix(".jpg")
            dst.parent.mkdir(parents=True, exist_ok=True)
            im = Image.open(p)
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                canvas = Image.new("RGB", im.size, bg_for(str(rel)))
                canvas.paste(im, mask=im.getchannel("A"))
                im = canvas
            else:
                im = im.convert("RGB")
            im.save(dst, "JPEG", quality=QUALITY, optimize=True)
            n += 1
    print(f"DONE. {n} JPG -> {OUT}")


if __name__ == "__main__":
    main()
