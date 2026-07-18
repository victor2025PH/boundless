# -*- coding: utf-8 -*-
"""为五台开发/算力机生成 1920x1080 桌面壁纸（中文名 + IP + GPU + 集群互称）。"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "brand-assets"
FONTS = BRAND / "fonts"
OUT = BRAND / "05_backgrounds" / "machines"
MACHINES = ROOT / "deploy" / "machines.json"

W, H = 1920, 1080


def C(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


PALETTE = {
    "voicex": {"top": C("1A1030"), "bot": C("05060F"), "accent": C("C43BF0")},
    "livex": {"top": C("1A1030"), "bot": C("05060F"), "accent": C("F050C8")},
    "facex": {"top": C("1A1030"), "bot": C("05060F"), "accent": C("B62BF5")},
    "voxx": {"top": C("2A1808"), "bot": C("0A0604"), "accent": C("F07800")},
    "lingox": {"top": C("2A1808"), "bot": C("0A0604"), "accent": C("F0A010")},
    "chatx": {"top": C("071828"), "bot": C("030810"), "accent": C("00B0F0")},
    "reachx": {"top": C("071828"), "bot": C("030810"), "accent": C("1E6BF0")},
}


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    path = FONTS / name
    if not path.exists():
        return ImageFont.load_default()
    return ImageFont.truetype(str(path), size=size)


def gradient(top, bot) -> Image.Image:
    img = Image.new("RGB", (W, H), top)
    px = img.load()
    for y in range(H):
        t = y / (H - 1)
        r = int(top[0] * (1 - t) + bot[0] * t)
        g = int(top[1] * (1 - t) + bot[1] * t)
        b = int(top[2] * (1 - t) + bot[2] * t)
        for x in range(W):
            px[x, y] = (r, g, b)
    return img


def load_icon(brand: str, size: int = 420) -> Image.Image:
    candidates = [
        BRAND / "02_product-icons" / brand / f"{brand}-512.png",
        BRAND / "00_master" / "keyed" / f"{brand}-keyed.png",
        BRAND / "03_lockups" / "products" / f"{brand}-lockup-white.png",
    ]
    for p in candidates:
        if p.exists():
            im = Image.open(p).convert("RGBA")
            im.thumbnail((size, size), Image.Resampling.LANCZOS)
            return im
    mark = BRAND / "01_logos" / "mark" / "boundless-mark-512.png"
    im = Image.open(mark).convert("RGBA")
    im.thumbnail((size, size), Image.Resampling.LANCZOS)
    return im


def role_label(role: str) -> str:
    return "开发机 Dev" if role == "dev" else "算力节点 Compute"


def ssh_name(m: dict) -> str:
    ssh = m.get("ssh") or []
    return str(ssh[0]) if ssh else m["id"]


def compose(m: dict, fleet: list[dict]) -> Path:
    brand = m["primary_brand"]
    pal = PALETTE.get(brand, PALETTE["voicex"])
    accent = pal["accent"]
    base = gradient(pal["top"], pal["bot"])

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    cx, cy = W // 2, int(H * 0.30)
    for r, a in ((480, 36), (340, 50), (220, 64)):
        gdraw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*accent, a))
    glow = glow.filter(ImageFilter.GaussianBlur(48))
    base = Image.alpha_composite(base.convert("RGBA"), glow)

    icon = load_icon(brand, 300)
    ix = (W - icon.width) // 2
    iy = int(H * 0.10)
    base.paste(icon, (ix, iy), icon)

    # 中文一律用 Noto CJK，避免 □□□□；英文数字同字体也支持
    f_zh = font("NotoSansCJKsc-Black.otf", 88)
    f_meta = font("NotoSansCJKsc-Medium.otf", 30)
    f_mesh_h = font("NotoSansCJKsc-Medium.otf", 24)
    f_mesh = font("NotoSansCJKsc-Medium.otf", 22)
    f_foot = font("NotoSansCJKsc-Medium.otf", 20)

    draw = ImageDraw.Draw(base)
    title = m["zh"]
    bbox = draw.textbbox((0, 0), title, font=f_zh)
    tw = bbox[2] - bbox[0]
    tx = (W - tw) // 2
    ty = iy + icon.height + 18
    draw.text((tx, ty), title, font=f_zh, fill=(255, 255, 255, 255))

    # 本机：互称名 / IP / GPU
    call = ssh_name(m)
    meta = f"{role_label(m.get('role', ''))}  ·  {call}  ·  {m['ip']}  ·  {m.get('gpu', '')}"
    bbox2 = draw.textbbox((0, 0), meta, font=f_meta)
    mw = bbox2[2] - bbox2[0]
    draw.text(((W - mw) // 2, ty + 100), meta, font=f_meta, fill=(*accent, 255))

    # 集群互称名册：每台都能认出彼此
    mesh_title = "集群互称  ·  ssh 别名互相呼叫"
    bbox_h = draw.textbbox((0, 0), mesh_title, font=f_mesh_h)
    hw = bbox_h[2] - bbox_h[0]
    mesh_top = H - 64 - 36 - len(fleet) * 30 - 36
    draw.text(((W - hw) // 2, mesh_top), mesh_title, font=f_mesh_h, fill=(170, 175, 190, 230))

    # 分隔线
    line_y = mesh_top + 34
    draw.line((W // 2 - 420, line_y, W // 2 + 420, line_y), fill=(*accent, 90), width=1)

    row_y = line_y + 14
    for peer in fleet:
        p_call = ssh_name(peer)
        mark = "★ " if peer["id"] == m["id"] else "   "
        row = (
            f"{mark}{peer['zh']}  /  {p_call}  ·  {peer['ip']}  ·  {peer.get('gpu', '')}"
        )
        fill = (255, 255, 255, 255) if peer["id"] == m["id"] else (175, 180, 195, 220)
        bbox_r = draw.textbbox((0, 0), row, font=f_mesh)
        rw = bbox_r[2] - bbox_r[0]
        draw.text(((W - rw) // 2, row_y), row, font=f_mesh, fill=fill)
        row_y += 30

    foot = "无界科技 BOUNDLESS  ·  让沟通，无界"
    bbox4 = draw.textbbox((0, 0), foot, font=f_foot)
    fw = bbox4[2] - bbox4[0]
    draw.text(((W - fw) // 2, H - 52), foot, font=f_foot, fill=(140, 145, 160, 220))

    mark_path = BRAND / "01_logos" / "mark" / "boundless-mark-128.png"
    if mark_path.exists():
        mark_im = Image.open(mark_path).convert("RGBA")
        mark_im.thumbnail((72, 72), Image.Resampling.LANCZOS)
        base.paste(mark_im, (48, 40), mark_im)

    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"{m['id']}-wallpaper.png"
    base.convert("RGB").save(out, "PNG", optimize=True)
    print(f"OK {out}")
    return out


def main():
    data = json.loads(MACHINES.read_text(encoding="utf-8"))
    fleet = data["machines"]
    for m in fleet:
        compose(m, fleet)
    print(f"done -> {OUT}")


if __name__ == "__main__":
    main()
