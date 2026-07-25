# -*- coding: utf-8 -*-
"""自动校准产品图标光学补偿系数（P0，2026-07-25）。

背景：8 张产品图标经 boxed_square() 光学烘焙求视觉等大，但系数在
platform/brand/optical-scale.json 里是手工维护的——7/22 图标全量重绘后
系数过时，视觉大小回归 ±18%（智拓/智控偏大、幻声/通传偏小）却无人知晓。

本脚本让「换图标必手调系数」变成「跑一条命令自动归一」：

  python calibrate_optical_scales.py            # 只算 + 写 JSON + 报表
  python calibrate_optical_scales.py --apply    # 再顺跑重烘 + 同步官网 + 复测

原理（与生产管线同源，零近似）：
  1. 以 chatx（智聊）为锚：eq_anchor = 实测 boxed_square(chatx, 256, k=1) 的
     alpha 等效直径 eq = 2*sqrt(sum(alpha/255)/pi)。
  2. 每张图标解 k，使 boxed_square(icon, 256, k) 的实测 eq ≈ 目标。目标含
     「形状修正」：满轮廓图标（fill 率高，如实心方阵的 matrixx）同等 eq 下
     感知更大，温和下调 —— target = eq_anchor * (1 - s*max(0, fill-0.55))，
     s = shape_correction（JSON 可调，0 = 关闭）。
  3. 裁切护栏：k 超过画布可容纳上限（K_CAP，按 512/256/128 三尺寸取最紧，
     长边距画布边至少 1px）时截断并进 capped 列表——宽扁构图（voicex）物理
     上无法再放大，属诚实妥协，重构图归 P2 美术。
  4. expected_eq = 用最终 k 真实试烘一遍的实测值（不是线性外推），写进
     JSON 供 repo_doctor 门禁校验「官网产物 == 校准预期」，从此漂移即红。

JSON 契约（新增键向后兼容，bake/前端只消费 applyInUi + scales）：
  scales        每图标最终系数（overrides 优先于自动值）
  expected_eq   烘焙产物应实测到的等效直径（256 画布，px）
  capped        被裁切护栏截断、允许偏离基线的图标
  overrides     人工微调段（本脚本永不覆盖，留给美术验收后手调）
"""
import argparse
import datetime
import json
import math
import os
import shutil
import subprocess
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from build_brand_assets import PRODUCTS, KEYED, boxed_square  # noqa: E402

SCALE_JSON = os.path.normpath(os.path.join(ROOT, "..", "platform", "brand", "optical-scale.json"))
VENDOR_JSON = os.path.normpath(os.path.join(ROOT, "..", "website", "vendor", "brand", "optical-scale.json"))
SITE_PROD = os.path.normpath(os.path.join(ROOT, "..", "website", "public", "brand", "products"))

CANVAS = 256
PAD = 0.08
ANCHOR = "chatx"            # 基线锚：智聊（居中饱满的对话气泡构图，最接近感知均值）
FILL_PIVOT = 0.55           # fill 率超过此值才触发形状修正
SHAPE_CORRECTION = 0.10     # 温和默认值；JSON 里可调，0 = 关闭
K_MIN = 0.7                 # 与 boxed_square 的 clamp 下限一致
SAFE_EDGE = 1               # 长边距画布边最少留 1px（防 paste 负坐标裁像素）

# 画布可容纳的系数上限：按三个产出尺寸取最紧（128px 最紧 ≈ 1.177）
K_CAP = min(
    (s - 2 * SAFE_EDGE) / int(s * (1 - 2 * PAD))
    for s in (512, 256, 128)
)


def alpha_metrics(img):
    """返回 (等效直径, fill率)。fill = alpha面积 / alpha bbox 面积。"""
    a = img.getchannel("A")
    hist = a.histogram()
    area = sum(v * count for v, count in enumerate(hist)) / 255.0
    eq = 2.0 * math.sqrt(area / math.pi)
    bbox = a.getbbox()
    if not bbox:
        return 0.0, 0.0
    bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    fill = area / float(bw * bh) if bw and bh else 0.0
    return eq, fill


def baked_eq(master, k):
    """用真实生产管线（boxed_square@256）试烘一次，实测等效直径。"""
    eq, _ = alpha_metrics(boxed_square(master, CANVAS, PAD, optical_scale=k))
    return eq


def load_masters():
    masters = {}
    for k in PRODUCTS:
        path = os.path.join(KEYED, k + "-keyed.png")
        if not os.path.isfile(path):
            raise SystemExit("missing keyed master: " + path)
        masters[k] = Image.open(path).convert("RGBA")
    return masters


def calibrate(existing):
    masters = load_masters()
    s_corr = float(existing.get("shape_correction", SHAPE_CORRECTION))
    overrides = dict(existing.get("overrides") or {})

    # 锚点基线（k=1 烘焙后的实测 eq）
    eq_anchor = baked_eq(masters[ANCHOR], 1.0)

    rows = []          # 报表
    scales = {}
    expected = {}
    capped = []
    for key in PRODUCTS:
        m = masters[key]
        raw_eq, fill = alpha_metrics(boxed_square(m, CANVAS, PAD, optical_scale=1.0))
        target = eq_anchor * (1.0 - s_corr * max(0.0, fill - FILL_PIVOT))
        k_ideal = target / raw_eq if raw_eq else 1.0
        k_auto = max(K_MIN, min(K_CAP, k_ideal))
        if k_ideal > K_CAP and (k_ideal - K_CAP) / k_ideal > 0.02:
            capped.append(key)
        k_final = float(overrides.get(key, round(k_auto, 3)))
        scales[key] = round(k_final, 3)
        expected[key] = round(baked_eq(m, k_final), 1)
        rows.append({
            "key": key, "raw_eq": raw_eq, "fill": fill, "k_ideal": k_ideal,
            "k_final": scales[key], "expected": expected[key],
            "capped": key in capped, "override": key in overrides,
        })
    return {
        "eq_anchor": eq_anchor, "shape_correction": s_corr,
        "scales": scales, "expected_eq": expected, "capped": capped,
        "overrides": overrides, "rows": rows,
    }


def write_json(existing, result):
    out = {
        "_note": ("产品图标光学补偿单一真相（官网/头像/lockup/桌面图标共用）。"
                  "由 brand-assets/calibrate_optical_scales.py 自动校准生成——"
                  "换图标后重跑该脚本即可，勿手改 scales/expected_eq；"
                  "人工微调请写 overrides。applyInUi=false 表示系数已烘焙进资产，"
                  "前端 ProductIcon 不再叠加 CSS 缩放。"),
        "version": "2.0.0",
        "applyInUi": bool(existing.get("applyInUi", False)),
        "anchor": ANCHOR,
        "baseline_eq": round(result["eq_anchor"], 1),
        "shape_correction": result["shape_correction"],
        "fill_pivot": FILL_PIVOT,
        "k_cap": round(K_CAP, 4),
        "auto_calibrated_at": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "scales": result["scales"],
        "expected_eq": result["expected_eq"],
        "capped": result["capped"],
        "overrides": result["overrides"],
    }
    for path in (SCALE_JSON, VENDOR_JSON):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print("[ok] wrote " + path)


def print_report(existing, result):
    old = existing.get("scales") or {}
    base = result["eq_anchor"]
    print("")
    print("anchor=%s  baseline_eq=%.1f  shape_correction=%.2f  k_cap=%.3f" % (
        ANCHOR, base, result["shape_correction"], K_CAP))
    print("-" * 78)
    print("%-8s %8s %6s %8s %8s %9s %8s  %s" % (
        "key", "raw_eq", "fill", "k_old", "k_new", "expected", "vs_base", "flags"))
    for r in result["rows"]:
        flags = []
        if r["capped"]:
            flags.append("CAPPED")
        if r["override"]:
            flags.append("override")
        print("%-8s %8.1f %6.2f %8.3f %8.3f %9.1f %+7.1f%%  %s" % (
            r["key"], r["raw_eq"], r["fill"], float(old.get(r["key"], 1.0)),
            r["k_final"], r["expected"], (r["expected"] / base - 1) * 100,
            " ".join(flags)))
    print("-" * 78)


def verify_site(expected, tol=0.01):
    """复测官网产物：实测 eq 应等于校准预期（±1%）。"""
    ok = True
    print("")
    print("verify website/public/brand/products vs expected_eq (tol %.1f%%):" % (tol * 100))
    for key, exp in expected.items():
        p = os.path.join(SITE_PROD, key + ".png")
        if not os.path.isfile(p):
            print("  %-8s MISSING %s" % (key, p))
            ok = False
            continue
        eq, _ = alpha_metrics(Image.open(p).convert("RGBA"))
        dev = (eq - exp) / exp if exp else 0.0
        status = "OK " if abs(dev) <= tol else "FAIL"
        if abs(dev) > tol:
            ok = False
        print("  %-8s eq=%7.1f expected=%7.1f dev=%+5.1f%%  %s" % (key, eq, exp, dev * 100, status))
    return ok


def main():
    ap = argparse.ArgumentParser(description="auto-calibrate product icon optical scales")
    ap.add_argument("--apply", action="store_true", help="rebake icons + sync website + verify")
    args = ap.parse_args()

    existing = {}
    if os.path.isfile(SCALE_JSON):
        with open(SCALE_JSON, "r", encoding="utf-8") as f:
            existing = json.load(f)

    result = calibrate(existing)
    print_report(existing, result)
    write_json(existing, result)

    if not args.apply:
        print("\n(dry) JSON updated. run with --apply to rebake + sync + verify.")
        return 0

    # 备份旧官网产物（供前后对比图）
    bak = os.path.join(ROOT, "_tmp_prev_site_icons")
    os.makedirs(bak, exist_ok=True)
    for key in PRODUCTS:
        src = os.path.join(SITE_PROD, key + ".png")
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(bak, key + ".png"))
    print("[ok] previous site icons backed up to " + bak)

    # 重烘 + 同步（子进程重新 import，读到新 JSON）
    rc = subprocess.call([sys.executable, os.path.join(ROOT, "bake_product_optical.py")])
    if rc != 0:
        print("[fail] bake_product_optical.py rc=%d" % rc)
        return rc

    return 0 if verify_site(result["expected_eq"]) else 2


if __name__ == "__main__":
    sys.exit(main())
