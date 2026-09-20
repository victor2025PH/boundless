# -*- coding: utf-8 -*-
"""生成「小圆脸」启动表情包（官方本地包资产，2026-08-17 表情包 P2）。

为什么程序化画而不是 AI 生图/外采素材：
- 十张风格必须统一（同一套几何语言），生成式模型做不到逐张一致且无透明底；
- 确定性可重跑（固定几何、零随机、零字体依赖——"?"/"Z" 都是几何笔画，
  不依赖系统字体，跨机器渲染一致）；
- 品牌正式素材日后由设计师出图后**直接替换本目录文件 + 改 manifest**，
  播种管线零改动（seed-official 的 files 条目机制）。

产物：``assets/sticker_packs/official/starter/*.webp``（512×512 透明底，
twemoji 风格扁平圆脸）。写盘后逐张验证：RIFF/WEBP magic bytes（媒体产物
验证纪律：尺寸/退出码不构成内容验证）+ PIL 复读 + 过一遍生产规范化管线
``normalize_sticker``（证明 seed-official 一定能导入）。任一步失败非零退出。

用法::

    python scripts/gen_starter_stickers.py            # 写入 assets/.../starter/
    python scripts/gen_starter_stickers.py --out DIR  # 写到别处（测试用）
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path
from typing import Callable, Dict, List, Tuple

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = _ENGINE_ROOT / "assets" / "sticker_packs" / "official" / "starter"

# 4x 超采样再 LANCZOS 缩到 512：PIL 无抗锯齿，超采样是拿平滑边缘的唯一便宜办法。
SS = 4
BASE = 512
C = BASE * SS

# twemoji 风格配色（扁平、无描边、无高光）
FACE = (255, 203, 76, 255)      # 经典表情黄
BROWN = (101, 71, 27, 255)      # 五官深棕
BLUSH = (255, 154, 95, 255)     # 腮红（预混色：ImageDraw 直画半透明会打穿 alpha）
TEAR = (93, 173, 236, 255)
HEART_RED = (238, 68, 88, 255)
OK_GREEN = (88, 192, 92, 255)
WHITE = (255, 255, 255, 255)

# (文件名, emoji 标签, 检索关键词)——manifest 与门禁按本表对齐（单一事实源）。
STICKERS: List[Tuple[str, str, List[str]]] = [
    ("smile", "😊", ["微笑", "开心", "smile", "happy"]),
    ("laugh", "😄", ["大笑", "哈哈", "laugh", "haha"]),
    ("love", "😍", ["爱心眼", "喜欢", "love", "adore"]),
    ("wow", "😮", ["惊讶", "哇", "wow", "surprised"]),
    ("cry", "😢", ["哭", "难过", "cry", "sad"]),
    ("sleep", "😴", ["晚安", "睡觉", "sleep", "goodnight"]),
    ("think", "🤔", ["思考", "想想", "think", "hmm"]),
    ("angry", "😠", ["生气", "哼", "angry", "mad"]),
    ("heart", "❤️", ["爱心", "比心", "heart", "love"]),
    ("ok", "✅", ["好的", "收到", "ok", "done"]),
]


def _s(v: float) -> int:
    return int(round(v * SS))


def _bb(cx: float, cy: float, rx: float, ry: float = 0) -> List[int]:
    ry = ry or rx
    return [_s(cx - rx), _s(cy - ry), _s(cx + rx), _s(cy + ry)]


def _face(d) -> None:
    d.ellipse(_bb(256, 260, 205), fill=FACE)


def _happy_eyes(d, y: float = 225) -> None:
    for cx in (176, 336):
        d.arc(_bb(cx, y + 14, 40), start=200, end=340, fill=BROWN, width=_s(15))


def _dot_eyes(d, r: float = 22, y: float = 220) -> None:
    for cx in (176, 336):
        d.ellipse(_bb(cx, y, r), fill=BROWN)


def _blush(d, y: float = 318) -> None:
    for cx in (140, 372):
        d.ellipse(_bb(cx, y, 36, 20), fill=BLUSH)


def _smile_mouth(d, w: float = 90, y: float = 300, width: float = 17) -> None:
    d.arc(_bb(256, y, w, 62), start=20, end=160, fill=BROWN, width=_s(width))


def _heart_shape(d, cx: float, cy: float, s: float, fill) -> None:
    r = 0.30 * s
    for dx in (-0.26, 0.26):
        d.ellipse(_bb(cx + dx * s, cy - 0.18 * s, r), fill=fill)
    d.polygon([(_s(cx - 0.549 * s), _s(cy - 0.10 * s)),
               (_s(cx + 0.549 * s), _s(cy - 0.10 * s)),
               (_s(cx), _s(cy + 0.52 * s))], fill=fill)


def _z_glyph(d, cx: float, cy: float, s: float, w: float) -> None:
    x0, x1 = _s(cx - s / 2), _s(cx + s / 2)
    y0, y1 = _s(cy - s / 2), _s(cy + s / 2)
    for pts in ([(x0, y0), (x1, y0)], [(x1, y0), (x0, y1)], [(x0, y1), (x1, y1)]):
        d.line(pts, fill=BROWN, width=_s(w))


def _draw_smile(d) -> None:
    _face(d)
    _happy_eyes(d)
    _smile_mouth(d)
    _blush(d)


def _draw_laugh(d) -> None:
    _face(d)
    _happy_eyes(d, y=210)
    d.pieslice(_bb(256, 300, 102, 82), start=0, end=180, fill=BROWN)
    d.pieslice(_bb(256, 302, 74, 30), start=0, end=180, fill=WHITE)


def _draw_love(d) -> None:
    _face(d)
    for cx in (176, 336):
        _heart_shape(d, cx, 218, 108, HEART_RED)
    _smile_mouth(d, w=78, y=318)


def _draw_wow(d) -> None:
    _face(d)
    _dot_eyes(d, r=26, y=218)
    for cx in (176, 336):
        d.arc(_bb(cx, 172, 44), start=200, end=340, fill=BROWN, width=_s(13))
    d.ellipse(_bb(256, 338, 34, 44), fill=BROWN)


def _draw_cry(d) -> None:
    _face(d)
    for cx in (176, 336):  # 八字愁眉
        d.arc(_bb(cx, 196, 42), start=200, end=340, fill=BROWN, width=_s(13))
    _dot_eyes(d, r=20, y=238)
    d.arc(_bb(256, 396, 76, 52), start=200, end=340, fill=BROWN, width=_s(17))
    d.polygon([(_s(118), _s(258)), (_s(92), _s(322)), (_s(144), _s(322))],
              fill=TEAR)
    d.ellipse(_bb(118, 330, 27), fill=TEAR)


def _draw_sleep(d) -> None:
    _face(d)
    for cx in (176, 336):  # 闭目 ∪ 弧
        d.arc(_bb(cx, 218, 40), start=20, end=160, fill=BROWN, width=_s(15))
    d.ellipse(_bb(238, 348, 24, 28), fill=BROWN)
    _z_glyph(d, 380, 128, 62, 13)
    _z_glyph(d, 432, 84, 44, 11)
    _z_glyph(d, 468, 50, 30, 9)


def _draw_think(d) -> None:
    _face(d)
    d.arc(_bb(176, 196, 42), start=210, end=330, fill=BROWN, width=_s(13))
    d.arc(_bb(336, 176, 44), start=200, end=340, fill=BROWN, width=_s(13))
    d.ellipse(_bb(184, 232, 20), fill=BROWN)
    d.ellipse(_bb(344, 224, 20), fill=BROWN)
    d.line([(_s(196), _s(348)), (_s(300), _s(330))], fill=BROWN, width=_s(17))
    # 几何 "?"：钩弧（缺口朝左下才像问号，扫角别超 230°）+ 斜落笔 + 点
    d.arc(_bb(424, 96, 34), start=170, end=40, fill=BROWN, width=_s(13))
    d.line([(_s(448), _s(119)), (_s(436), _s(146))], fill=BROWN, width=_s(13))
    d.ellipse(_bb(433, 171, 11), fill=BROWN)


def _draw_angry(d) -> None:
    _face(d)
    d.line([(_s(128), _s(178)), (_s(214), _s(212))], fill=BROWN, width=_s(16))
    d.line([(_s(384), _s(178)), (_s(298), _s(212))], fill=BROWN, width=_s(16))
    _dot_eyes(d, r=21, y=248)
    d.arc(_bb(256, 400, 74, 50), start=200, end=340, fill=BROWN, width=_s(17))


def _draw_heart(d) -> None:
    _heart_shape(d, 256, 242, 340, HEART_RED)


def _draw_ok(d) -> None:
    d.ellipse(_bb(256, 256, 205), fill=OK_GREEN)
    pts = [(162, 262), (232, 332), (356, 186)]
    d.line([(_s(x), _s(y)) for x, y in pts], fill=WHITE, width=_s(38),
           joint="curve")
    for x, y in (pts[0], pts[-1]):  # 圆头端帽
        d.ellipse(_bb(x, y, 19), fill=WHITE)


_DRAWERS: Dict[str, Callable] = {
    "smile": _draw_smile, "laugh": _draw_laugh, "love": _draw_love,
    "wow": _draw_wow, "cry": _draw_cry, "sleep": _draw_sleep,
    "think": _draw_think, "angry": _draw_angry, "heart": _draw_heart,
    "ok": _draw_ok,
}


def render_sticker(name: str) -> bytes:
    """单张渲染 → 512×512 透明底 webp bytes（纯函数，供门禁直调）。"""
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (C, C), (0, 0, 0, 0))
    _DRAWERS[name](ImageDraw.Draw(im))
    im = im.resize((BASE, BASE), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="WEBP", quality=90, method=6)
    return buf.getvalue()


def verify_webp(data: bytes) -> str:
    """内容级验证；返回问题描述，空串=通过。"""
    if len(data) < 16 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return "magic bytes 不是 RIFF/WEBP"
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as ex:  # noqa: BLE001
        return f"PIL 复读失败: {ex}"
    if im.size != (BASE, BASE):
        return f"尺寸 {im.size} != ({BASE},{BASE})"
    if im.convert("RGBA").getpixel((2, 2))[3] != 0:
        return "角落不透明（应为透明底）"
    try:
        sys.path.insert(0, str(_ENGINE_ROOT))
        from src.inbox.sticker_normalize import normalize_sticker
        norm = normalize_sticker(data, source_ext=".webp")
        if norm["animated"]:
            return "规范化误判为动图"
    except Exception as ex:  # noqa: BLE001
        return f"规范化管线不通过: {ex}"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--only", default="",
                    help="只重渲指定名字（逗号分隔）——微调单张免全量重跑")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    todo = [t for t in STICKERS if not only or t[0] in only]
    bad = 0
    for name, _emoji, _kw in todo:
        data = render_sticker(name)
        problem = verify_webp(data)
        if problem:
            print(f"[FAIL] {name}: {problem}")
            bad += 1
            continue
        fp = out / f"{name}.webp"
        fp.write_bytes(data)
        # 控制台不打 emoji（GBK 编码坑，ASCII-only 输出纪律）
        print(f"[ok] {fp.name:12s} {len(data):6d} B")
    print(f"\n{len(todo) - bad}/{len(todo)} 张写入 {out}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
