# -*- coding: utf-8 -*-
"""坐席工作台视觉回归——基线对比器。

对比 baseline/ 与 shots/current/ 下同名 png：先比尺寸，再逐像素 diff。
任一图差异像素占比超过阈值（默认 0.5%）→ 判失败，并输出红色高亮 diff 图
到 shots/diff/（差异像素为纯红、其余为压暗灰底，肉眼秒定位回归区域）。

用法：
    python compare.py [--baseline DIR] [--current DIR] [--threshold PCT]

退出码：0=全部通过；1=有超阈值/尺寸不一致/基线图缺失；2=环境或目录错误。
"""

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_BASELINE = HERE / "baseline"
DEFAULT_CURRENT = HERE / "shots" / "current"
DEFAULT_DIFF = HERE / "shots" / "diff"
DEFAULT_THRESHOLD = 0.5  # 差异像素占比阈值（百分数）

try:
    from PIL import Image, ImageChops
except ImportError:
    print("缺少 Pillow：请先 `pip install pillow` 再运行 compare.py")
    sys.exit(2)


def diff_stats(img_a, img_b):
    """返回 (差异像素数, 总像素数, 差异掩码 L 图)。任一通道不同即计差异。"""
    a = img_a.convert("RGB")
    b = img_b.convert("RGB")
    delta = ImageChops.difference(a, b)
    r, g, bl = delta.split()
    mask = ImageChops.lighter(ImageChops.lighter(r, g), bl)  # 每像素取三通道最大差
    hist = mask.histogram()
    total = a.width * a.height
    return total - hist[0], total, mask


def write_diff_image(current_img, mask, out_path):
    """生成红色高亮 diff 图：差异像素纯红，其余为压暗的当前图灰底。"""
    bg = current_img.convert("L").point(lambda v: v // 3 + 40).convert("RGB")
    red = Image.new("RGB", bg.size, (255, 0, 0))
    binary = mask.point(lambda v: 255 if v > 0 else 0)
    Image.composite(red, bg, binary).save(out_path)


def compare(baseline_dir, current_dir, diff_dir, threshold_pct):
    baseline_dir = Path(baseline_dir)
    current_dir = Path(current_dir)
    diff_dir = Path(diff_dir)

    if not baseline_dir.is_dir():
        print(f"基线目录不存在：{baseline_dir}（先跑 python make_baseline.py）")
        return 2
    if not current_dir.is_dir():
        print(f"当前截图目录不存在：{current_dir}（先跑 python capture.py）")
        return 2

    base_pngs = sorted(p.name for p in baseline_dir.glob("*.png"))
    cur_pngs = {p.name for p in current_dir.glob("*.png")}
    if not base_pngs:
        print(f"基线目录没有 png：{baseline_dir}")
        return 2

    failures = []
    print(f"threshold={threshold_pct}%  baseline={baseline_dir}  "
          f"current={current_dir}")
    for name in base_pngs:
        if name not in cur_pngs:
            failures.append(name)
            print(f"[FAIL] {name}: current 缺少该图（场景消失/被跳过）")
            continue
        img_b = Image.open(baseline_dir / name)
        img_c = Image.open(current_dir / name)
        if img_b.size != img_c.size:
            failures.append(name)
            print(f"[FAIL] {name}: 尺寸不一致 baseline={img_b.size} "
                  f"current={img_c.size}")
            continue
        n_diff, total, mask = diff_stats(img_b, img_c)
        pct = n_diff / total * 100.0
        if pct > threshold_pct:
            failures.append(name)
            diff_dir.mkdir(parents=True, exist_ok=True)
            out = diff_dir / name
            write_diff_image(img_c, mask, out)
            print(f"[FAIL] {name}: diff={pct:.4f}% ({n_diff}/{total} px) "
                  f"-> {out}")
        else:
            print(f"[pass] {name}: diff={pct:.4f}% ({n_diff}/{total} px)")

    extras = sorted(cur_pngs - set(base_pngs))
    for name in extras:
        print(f"[warn] {name}: 基线没有此图（新场景？需要时重建基线收录）")

    if failures:
        print(f"FAILED: {len(failures)}/{len(base_pngs)} 张超阈值或缺失："
              + ", ".join(failures))
        return 1
    print(f"ALL PASS: {len(base_pngs)} 张全部在阈值内")
    return 0


def main(argv=None):
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="对比 baseline 与 current 截图")
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE),
                    help=f"基线目录（默认 {DEFAULT_BASELINE}）")
    ap.add_argument("--current", default=str(DEFAULT_CURRENT),
                    help=f"当前截图目录（默认 {DEFAULT_CURRENT}）")
    ap.add_argument("--diff-dir", default=str(DEFAULT_DIFF),
                    help=f"diff 高亮图输出目录（默认 {DEFAULT_DIFF}）")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help=f"差异像素占比阈值，百分数（默认 {DEFAULT_THRESHOLD}）")
    args = ap.parse_args(argv)
    return compare(args.baseline, args.current, args.diff_dir, args.threshold)


if __name__ == "__main__":
    sys.exit(main())
