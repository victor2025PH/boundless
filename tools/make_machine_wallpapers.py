# -*- coding: utf-8 -*-
"""六机桌面壁纸生成器 v2（2026-08-05 功能命名改版）。

由 deploy/machines.json (v2) 驱动，每台机产出：
  <id>-wallpaper.png          ops 版（身份 + 集群星座 + 互称名册 + 台账版本水印）
  <id>-showcase.png           上镜版（无 IP/账号/名册，屏幕分享/来访/直播机专用）
  states/<id>-netdown.png     警告态模板：本机断网（红）
  states/<id>-gpufault.png    警告态模板：GPU 异常（深红）
  states/<id>-hubdown.png     警告态模板：中枢不可达（琥珀）
  states/<id>-svcdown.png     警告态模板：本机算力服务异常（橙）

设计：
  - 按台账 resolution 原生渲染（韵声 4K、听写 16:10），2x 超采样后 LANCZOS 下缩，文字锐利。
  - 背景 = accent 渐变 + 双色 aurora 辉光 + 细网格；中景 = 六节点星座拓扑（中枢居中，本机高亮）。
  - 名册装进玻璃拟态卡片；左下角预留哨兵 ASCII 标注区（sentinel.ps1 盖时间戳/端口详情）。
  - 警告态 = ops 版压暗 35% + 全宽色带大字横幅（中文预烘焙，哨兵只需盖 ASCII 详情）。
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "brand-assets"
FONTS = BRAND / "fonts"
OUT = BRAND / "05_backgrounds" / "machines"
STATES_OUT = OUT / "states"
MACHINES = ROOT / "deploy" / "machines.json"

SS = 2  # 超采样倍率


def C(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def blend(a, b, t: float):
    return tuple(int(a[i] * (1 - t) + b[i] * t) for i in range(3))


_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    key = (name, size)
    if key not in _font_cache:
        p = FONTS / name
        _font_cache[key] = ImageFont.truetype(str(p), size=size) if p.exists() else ImageFont.load_default()
    return _font_cache[key]


F_BLACK = "NotoSansCJKsc-Black.otf"
F_BOLD = "NotoSansCJKsc-Bold.otf"
F_MED = "NotoSansCJKsc-Medium.otf"

# 星座图：独立放在左上"星图区"（不与中心图标/标题抢位；hub 居星图中心，五外围机绕行）
PENTA_ANGLES = {"kouxing": -90, "shengbei": -18, "tingxie": 54, "lianbei": 126, "yunsheng": 198}
CONST_CENTER = (0.225, 0.205)  # 星图中心（W,H 比例）
CONST_R = (0.135, 0.118)       # 椭圆半径（W,H 比例）
HUB_ID = "zhongshu"

ROLE_LABEL = {"hub": "中枢机 HUB", "dev": "开发机 DEV", "compute": "算力节点 COMPUTE"}

STATE_SPEC = {
    "netdown": {"band": (206, 44, 49), "fg": (255, 255, 255), "title": "本机断网 · 已脱离集群",
                 "sub": "网线 / 路由器排查 · 网络恢复后哨兵自动换回正常壁纸"},
    "gpufault": {"band": (150, 26, 34), "fg": (255, 255, 255), "title": "GPU 异常 · 算力不可用",
                  "sub": "nvidia-smi 无响应 · 检查驱动 / 供电 / 掉卡"},
    "ipdrift": {"band": (156, 66, 214), "fg": (255, 255, 255), "title": "本机 IP 漂移",
                 "sub": "实际 IP ≠ 台账 IP · 集群路由将找不到本机 · 路由器做静态租约或更新台账后重发配置"},
    # hubdown 的 sub 在 main() 里按台账 hub 条目动态填（写死 IP 会跟着 DHCP 漂移变成错误情报）
    "hubdown": {"band": (232, 179, 65), "fg": (26, 18, 8), "title": "中枢不可达",
                 "sub": "本机网络正常 · 中枢 Hub 无响应 · 若中枢在重启请忽略"},
    "svcdown": {"band": (222, 108, 16), "fg": (255, 255, 255), "title": "本机算力服务异常",
                 "sub": "关键端口未监听 · 具体端口见左下角哨兵标注 · 集群正依赖本机算力"},
}


def load_icon(m: dict, size: int) -> Image.Image:
    if m["id"] == HUB_ID:
        p = BRAND / "01_logos" / "mark" / "boundless-mark-512.png"
    else:
        b = m.get("primary_brand", "voicex")
        p = BRAND / "02_product-icons" / b / f"{b}-512.png"
        if not p.exists():
            p = BRAND / "01_logos" / "mark" / "boundless-mark-512.png"
    im = Image.open(p).convert("RGBA")
    im.thumbnail((size, size), Image.Resampling.LANCZOS)
    return im


def make_background(W: int, H: int, accent) -> Image.Image:
    top = blend((5, 6, 15), accent, 0.16)
    bot = (4, 5, 12)
    col = Image.new("RGB", (1, H))
    px = col.load()
    for y in range(H):
        t = y / max(1, H - 1)
        px[0, y] = blend(top, bot, t)
    base = col.resize((W, H)).convert("RGBA")

    # aurora 双辉光
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    accent2 = blend(accent, (48, 80, 192), 0.45)
    cx1, cy1 = int(W * 0.50), int(H * 0.28)
    cx2, cy2 = int(W * 0.72), int(H * 0.10)
    for r, a in ((int(H * 0.46), 34), (int(H * 0.33), 46), (int(H * 0.21), 60)):
        gd.ellipse((cx1 - r, cy1 - r, cx1 + r, cy1 + r), fill=(*accent, a))
    for r, a in ((int(H * 0.30), 26), (int(H * 0.18), 36)):
        gd.ellipse((cx2 - int(r * 1.6), cy2 - r, cx2 + int(r * 1.6), cy2 + r), fill=(*accent2, a))
    glow = glow.filter(ImageFilter.GaussianBlur(int(H * 0.055)))
    base = Image.alpha_composite(base, glow)

    # 细网格
    grid = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gdr = ImageDraw.Draw(grid)
    step = max(64, int(H / 12))
    gc = (*blend(accent, (255, 255, 255), 0.5), 7)
    for x in range(0, W, step):
        gdr.line((x, 0, x, H), fill=gc, width=1)
    for y in range(0, H, step):
        gdr.line((0, y, W, y), fill=gc, width=1)
    base = Image.alpha_composite(base, grid)

    # 暗角
    vig = Image.new("L", (W, H), 0)
    vd = ImageDraw.Draw(vig)
    vd.ellipse((-int(W * 0.25), -int(H * 0.25), int(W * 1.25), int(H * 1.25)), fill=255)
    vig = vig.filter(ImageFilter.GaussianBlur(int(H * 0.12)))
    dark = Image.new("RGBA", (W, H), (0, 0, 0, 90))
    base = Image.composite(base, Image.alpha_composite(base, dark), vig)
    return base


def node_pos(mid: str, W: int, H: int) -> tuple[int, int]:
    cx, cy = W * CONST_CENTER[0], H * CONST_CENTER[1]
    if mid == HUB_ID:
        return int(cx), int(cy)
    ang = math.radians(PENTA_ANGLES.get(mid, -90))
    rx, ry = W * CONST_R[0], H * CONST_R[1]
    return int(cx + rx * math.cos(ang)), int(cy + ry * math.sin(ang))


def draw_constellation(base: Image.Image, fleet: list[dict], me: dict, accent, s: float) -> None:
    W, H = base.size
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    pos = {m["id"]: node_pos(m["id"], W, H) for m in fleet}
    hub_xy = pos.get(HUB_ID, (W // 2, int(H * 0.30)))

    peers = [m for m in fleet if m["id"] != HUB_ID]
    # 外环弱连线
    ring = sorted(peers, key=lambda m: PENTA_ANGLES.get(m["id"], 0))
    for i, m in enumerate(ring):
        nxt = ring[(i + 1) % len(ring)]
        d.line((*pos[m["id"]], *pos[nxt["id"]]), fill=(*blend(accent, (255, 255, 255), 0.4), 16), width=max(1, int(1 * s)))
    # 星型主连线（一切经中枢）
    for m in peers:
        w = max(1, int((3 if m["id"] == me["id"] else 2) * s))
        a = 90 if m["id"] == me["id"] else 40
        d.line((*pos[m["id"]], *hub_xy), fill=(*accent, a), width=w)

    f_lab = font(F_MED, int(17 * s))
    f_lab_me = font(F_BOLD, int(20 * s))
    f_cap = font(F_MED, int(15 * s))
    for m in fleet:
        x, y = pos[m["id"]]
        is_me = m["id"] == me["id"]
        r = int((14 if is_me else (10 if m["id"] == HUB_ID else 7)) * s)
        node_c = C(m.get("accent", "#8888AA"))
        if is_me:
            gl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            gld = ImageDraw.Draw(gl)
            gld.ellipse((x - r * 3.2, y - r * 3.2, x + r * 3.2, y + r * 3.2), fill=(*node_c, 90))
            gl = gl.filter(ImageFilter.GaussianBlur(int(10 * s)))
            layer = Image.alpha_composite(layer, gl)
            d = ImageDraw.Draw(layer)
        d.ellipse((x - r, y - r, x + r, y + r), fill=(*node_c, 255 if is_me else 170))
        if m["id"] == HUB_ID and not is_me:
            d.ellipse((x - r - int(4 * s), y - r - int(4 * s), x + r + int(4 * s), y + r + int(4 * s)),
                      outline=(*node_c, 120), width=max(1, int(1.5 * s)))
        lab = m["zh"]
        fnt = f_lab_me if is_me else f_lab
        tw = d.textlength(lab, font=fnt)
        # 下排节点(54°/126°)标签放节点下方，其余放上方，避免与连线/邻文字打架
        below = PENTA_ANGLES.get(m["id"], -90) in (54, 126) and m["id"] != HUB_ID
        if m["id"] == HUB_ID:
            ly = y + r + int(8 * s)
        elif below:
            ly = y + r + int(6 * s)
        else:
            ly = y - r - int((30 if is_me else 24) * s)
        fill = (255, 255, 255, 255) if is_me else (190, 195, 210, 150)
        d.text((x - tw / 2, ly), lab, font=fnt, fill=fill)
    # 星图小标题
    cap = "BOUNDLESS MESH · 六机一网"
    cx, cy = W * CONST_CENTER[0], H * CONST_CENTER[1]
    capw = d.textlength(cap, font=f_cap)
    d.text((cx - capw / 2, cy + H * CONST_R[1] + int(34 * s)), cap, font=f_cap, fill=(150, 155, 172, 130))
    base.alpha_composite(layer)


def rounded_panel(d: ImageDraw.ImageDraw, box, radius, fill, outline, width):
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def compose(m: dict, fleet: list[dict], version: str, showcase: bool) -> Image.Image:
    W0, H0 = (int(v) for v in m["resolution"].split("x"))
    W, H = W0 * SS, H0 * SS
    s = (H / 1080.0)
    accent = C(m["accent"])
    base = make_background(W, H, accent)
    draw_constellation(base, fleet, m, accent, s)
    d = ImageDraw.Draw(base)

    # 顶左品牌角标
    mark_p = BRAND / "01_logos" / "mark" / "boundless-mark-128.png"
    if mark_p.exists():
        mk = Image.open(mark_p).convert("RGBA")
        mk.thumbnail((int(64 * s), int(64 * s)), Image.Resampling.LANCZOS)
        base.paste(mk, (int(44 * s), int(38 * s)), mk)
        d.text((int(120 * s), int(52 * s)), "无界 BOUNDLESS", font=font(F_MED, int(22 * s)), fill=(175, 180, 195, 200))

    # 中心图标 + 机名
    icon = load_icon(m, int((300 if showcase else 240) * s))
    ix, iy = (W - icon.width) // 2, int(H * (0.115 if showcase else 0.135))
    base.paste(icon, (ix, iy), icon)

    f_title = font(F_BLACK, int((136 if showcase else 116) * s))
    title = m["zh"]
    tw = d.textlength(title, font=f_title)
    ty = iy + icon.height + int(14 * s)
    # 标题辉光
    tl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(tl).text(((W - tw) / 2, ty), title, font=f_title, fill=(*accent, 110))
    base.alpha_composite(tl.filter(ImageFilter.GaussianBlur(int(9 * s))))
    d = ImageDraw.Draw(base)
    d.text(((W - tw) / 2, ty), title, font=f_title, fill=(255, 255, 255, 255))

    y = ty + int((165 if showcase else 142) * s)
    if showcase:
        f_meta = font(F_MED, int(34 * s))
        meta = "·".join([""] + []) or f"{' / '.join(m.get('products', []) or ['无界产品矩阵'])}"
        meta = f"服务产品  {meta}"
        mw = d.textlength(meta, font=f_meta)
        d.text(((W - mw) / 2, y), meta, font=f_meta, fill=(*accent, 255))
        slog = "让沟通，无界 — BOUNDLESS AI CLUSTER"
        f_slog = font(F_MED, int(26 * s))
        sw = d.textlength(slog, font=f_slog)
        d.text(((W - sw) / 2, y + int(58 * s)), slog, font=f_slog, fill=(200, 205, 220, 220))
    else:
        # 身份行 + 角色徽章
        f_meta = font(F_BOLD, int(30 * s))
        meta = f"{m['ssh'][0]}  ·  {m['ip']}  ·  {m['gpu']}"
        badge = ROLE_LABEL.get(m.get("role", ""), "节点")
        f_badge = font(F_BOLD, int(22 * s))
        bw = d.textlength(badge, font=f_badge)
        mw = d.textlength(meta, font=f_meta)
        pad = int(16 * s)
        total = bw + pad * 2 + int(18 * s) + mw
        x0 = (W - total) / 2
        # 徽章胶囊
        bh = int(40 * s)
        by = y - int(4 * s)
        rounded_panel(d, (x0, by, x0 + bw + pad * 2, by + bh), radius=int(bh / 2),
                      fill=(*accent, 46), outline=(*accent, 160), width=max(1, int(1.5 * s)))
        d.text((x0 + pad, by + (bh - int(30 * s)) / 2), badge, font=f_badge, fill=(255, 255, 255, 235))
        d.text((x0 + bw + pad * 2 + int(18 * s), y), meta, font=f_meta, fill=(*accent, 255))
        # 现役职能行
        f_role = font(F_MED, int(24 * s))
        role = m.get("role_now", "")
        rw = d.textlength(role, font=f_role)
        d.text(((W - rw) / 2, y + int(52 * s)), role, font=f_role, fill=(195, 200, 214, 230))

        # ===== 名册玻璃卡 =====
        rows = list(fleet)
        card_w = int(1290 * s)
        row_h = int(31 * s)
        head_h = int(46 * s)
        card_h = head_h + row_h * len(rows) + int(40 * s)
        cx0 = (W - card_w) / 2
        cy0 = H * 0.618
        panel = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        pd = ImageDraw.Draw(panel)
        rounded_panel(pd, (cx0, cy0, cx0 + card_w, cy0 + card_h), radius=int(14 * s),
                      fill=(16, 18, 30, 150), outline=(*accent, 70), width=max(1, int(1 * s)))
        base.alpha_composite(panel)
        d = ImageDraw.Draw(base)

        f_head = font(F_BOLD, int(23 * s))
        head = "集群互称 · ssh 别名直呼"
        hw = d.textlength(head, font=f_head)
        d.text(((W - hw) / 2, cy0 + int(14 * s)), head, font=f_head, fill=(210, 214, 226, 240))
        d.line((cx0 + int(24 * s), cy0 + head_h, cx0 + card_w - int(24 * s), cy0 + head_h),
               fill=(*accent, 80), width=max(1, int(1 * s)))

        f_row = font(F_MED, int(21 * s))
        f_row_me = font(F_BOLD, int(21 * s))
        col = {"star": 26, "zh": 56, "alias": 200, "ip": 400, "gpu": 615, "role": 770}
        mrk = "★ 本机"
        mrk_w = d.textlength(mrk, font=f_row_me)
        role_max = card_w - int(770 * s) - mrk_w - int(56 * s)
        ry = cy0 + head_h + int(12 * s)
        for peer in rows:
            is_me = peer["id"] == m["id"]
            fnt = f_row_me if is_me else f_row
            fill = (255, 255, 255, 255) if is_me else (176, 181, 196, 215)
            pa = C(peer.get("accent", "#8888AA"))
            d.ellipse((cx0 + col["star"] * s, ry + int(7 * s), cx0 + col["star"] * s + int(10 * s), ry + int(17 * s)),
                      fill=(*pa, 255 if is_me else 165))
            gpu_short = peer["gpu"].replace("RTX ", "").replace(" ", "·")
            role_txt = peer.get("role_short", "")
            while role_txt and d.textlength(role_txt + "…", font=fnt) > role_max:
                role_txt = role_txt[:-1]
            if role_txt != peer.get("role_short", ""):
                role_txt += "…"
            cells = [("zh", peer["zh"]), ("alias", peer["ssh"][0]), ("ip", peer["ip"]),
                     ("gpu", gpu_short), ("role", role_txt)]
            for key, txt in cells:
                d.text((cx0 + col[key] * s, ry), txt, font=fnt, fill=fill)
            if is_me:
                d.text((cx0 + card_w - mrk_w - int(24 * s), ry), mrk, font=f_row_me, fill=(*accent, 255))
            ry += row_h
        # [2026-08-06 VI/安全] 公网 VPS 一行（IP+账号名）从壁纸退役：屏幕分享/直播/来访
        # 拍照都在泄漏公网凭据线索；运维明细回归 deploy/ 文档与 ssh config，桌面不印。

    # 页脚 + 版本水印
    foot = "无界科技 BOUNDLESS · 让沟通，无界"
    f_foot = font(F_MED, int(19 * s))
    fw = d.textlength(foot, font=f_foot)
    d.text(((W - fw) / 2, H - int(52 * s)), foot, font=f_foot, fill=(140, 145, 160, 210))
    ver = f"台账 v{version} · 生成 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    f_ver = font(F_MED, int(15 * s))
    vw2 = d.textlength(ver, font=f_ver)
    d.text((W - vw2 - int(34 * s), H - int(44 * s)), ver, font=f_ver, fill=(120, 124, 140, 160))

    return base.resize((W0, H0), Image.Resampling.LANCZOS).convert("RGB")


def make_state(ops_img: Image.Image, state: str, s: float) -> Image.Image:
    spec = STATE_SPEC[state]
    W, H = ops_img.size
    img = ImageEnhance.Brightness(ops_img.convert("RGB")).enhance(0.35).convert("RGBA")
    d = ImageDraw.Draw(img)
    band_h = int(240 * s)
    by0 = int(H * 0.36)
    band = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bd = ImageDraw.Draw(band)
    bd.rectangle((0, by0, W, by0 + band_h), fill=(*spec["band"], 232))
    bd.rectangle((0, by0, W, by0 + int(5 * s)), fill=(255, 255, 255, 60))
    bd.rectangle((0, by0 + band_h - int(5 * s), W, by0 + band_h), fill=(0, 0, 0, 70))
    img.alpha_composite(band)
    d = ImageDraw.Draw(img)
    f_t = font(F_BLACK, int(88 * s))
    tw = d.textlength(spec["title"], font=f_t)
    d.text(((W - tw) / 2, by0 + int(34 * s)), spec["title"], font=f_t, fill=(*spec["fg"], 255))
    f_s = font(F_MED, int(30 * s))
    sw = d.textlength(spec["sub"], font=f_s)
    sub_fg = tuple(spec["fg"])
    d.text(((W - sw) / 2, by0 + int(158 * s)), spec["sub"], font=f_s, fill=(*sub_fg, 235))
    # 左下角哨兵标注区（暗底，等 sentinel 盖 ASCII 详情）
    zx0, zy0 = int(20 * s), H - int(132 * s)
    d.rounded_rectangle((zx0, zy0, zx0 + int(760 * s), zy0 + int(76 * s)), radius=int(8 * s),
                        fill=(8, 9, 14, 215), outline=(*spec["band"], 200), width=max(1, int(1 * s)))
    d.text((zx0 + int(16 * s), zy0 + int(10 * s)), "SENTINEL", font=font(F_BOLD, int(20 * s)),
           fill=(*spec["band"], 255) if state != "gpufault" else (235, 120, 120, 255))
    return img.convert("RGB")


def main() -> None:
    data = json.loads(MACHINES.read_text(encoding="utf-8"))
    fleet = data["machines"]
    version = data.get("version", "?")
    hub = data.get("hub") or {}
    if hub.get("ip"):
        STATE_SPEC["hubdown"]["sub"] = ("本机网络正常 · 中枢 %s:%s 无响应 · 若中枢在重启请忽略"
                                        % (hub["ip"], hub.get("port", 9000)))
    only = sys.argv[1] if len(sys.argv) > 1 else None
    OUT.mkdir(parents=True, exist_ok=True)
    STATES_OUT.mkdir(parents=True, exist_ok=True)
    for m in fleet:
        if only and only not in m["id"]:
            continue
        W0, H0 = (int(v) for v in m["resolution"].split("x"))
        s = H0 / 1080.0
        ops = compose(m, fleet, version, showcase=False)
        p1 = OUT / f"{m['id']}-wallpaper.png"
        ops.save(p1, "PNG", optimize=True)
        print(f"OK {p1.name} {ops.size}")
        show = compose(m, fleet, version, showcase=True)
        p2 = OUT / f"{m['id']}-showcase.png"
        show.save(p2, "PNG", optimize=True)
        print(f"OK {p2.name}")
        for st in STATE_SPEC:
            img = make_state(ops, st, s)
            p3 = STATES_OUT / f"{m['id']}-{st}.png"
            img.save(p3, "PNG", optimize=True)
        print(f"OK states x{len(STATE_SPEC)} for {m['id']}")
    print(f"done -> {OUT}")


if __name__ == "__main__":
    main()
