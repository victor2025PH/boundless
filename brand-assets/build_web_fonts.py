# -*- coding: utf-8 -*-
"""
把 brand-assets/fonts/ 的品牌字体做成 web 可用的子集 woff2，并分发到各消费方。

为什么要子集化（而不是整档转 woff2）：
  · Montserrat-VF.ttf 整档 727KB；只保留拉丁字形后仅 ~30KB（小 24 倍），
    可变字重轴 wght 100-900 完整保留。
  · CJK（NotoSansCJKsc-*.otf 各 16MB）**刻意不转不发**：web 端由
    unicode-range 让 CJK 回落系统字体（PingFang/微软雅黑），
    既零体积代价、又与各系统原生中文观感一致。
    若将来确实要自带中文字体，必须先按站内实际用字做子集，绝不可整档上 web。

消费方 CSS 侧写法见 workspace_base.html 的 @font-face：
  unicode-range 必须与本脚本的 LATIN_UNICODES 保持一致，否则会出现
  「范围内字符没有字形」的豆腐块。两处改动要同时改。

依赖：fontTools + brotli（pip install fonttools brotli）。
用法：python brand-assets/build_web_fonts.py
"""

import os
import sys

# 与 workspace_base.html 的 @font-face unicode-range 严格一致：
# ASCII 可见字符 + Latin-1 补充 + 两个常用拉丁扩展字形。
# 刻意不含 U+2000-206F（通用标点：— … " "）——那些在中文排版里也会用到，
# 交给中文字体渲染才协调。
LATIN_UNICODES = "U+0020-007E,U+00A0-00FF,U+0131,U+0152-0153"

ROOT = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(ROOT)  # boundless 仓库根

SRC_TTF = os.path.join(ROOT, "fonts", "Montserrat-VF.ttf")
OUT_NAME = "Montserrat-Latin-VF.woff2"

# 分发目标（目录的父级存在才写，与 sync_brand_targets.py 同口径）
TARGETS = [
    os.path.join(WS, "engines", "chengjie", "src", "web", "static", "brand", "fonts"),
    os.path.join(WS, "engines", "chengjie", "desktop", "renderer", "brand", "fonts"),
]


def build_subset(src, dst):
    from fontTools import subset

    args = [
        src,
        "--unicodes=%s" % LATIN_UNICODES,
        "--flavor=woff2",
        "--output-file=%s" % dst,
    ]
    subset.main(args)


def main():
    if not os.path.isfile(SRC_TTF):
        print("[skip] 源字体不存在：%s" % SRC_TTF)
        return 0
    try:
        import fontTools  # noqa: F401
        import brotli  # noqa: F401
    except ImportError as e:
        print("[fail] 缺依赖（pip install fonttools brotli）：%s" % e)
        return 1

    written = 0
    for d in TARGETS:
        if not os.path.isdir(os.path.dirname(d)):
            continue  # 该消费方不在本机，跳过
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, OUT_NAME)
        build_subset(SRC_TTF, dst)
        kb = os.path.getsize(dst) / 1024.0
        print("[ok] %s (%.1f KB)" % (os.path.relpath(dst, WS), kb))
        written += 1

    print("DONE. %d target(s)." % written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
