# -*- coding: utf-8 -*-
"""自动校准产品图标光学补偿系数（P0，2026-07-25）。

背景：8 张产品图标经 boxed_square() 光学烘焙求视觉等大，但系数在
platform/brand/optical-scale.json 里是手工维护的——7/22 图标全量重绘后
系数过时，视觉大小回归 ±18%（智拓/智控偏大、幻声/通传偏小）却无人知晓。

本脚本让「换图标必手调系数」变成「跑一条命令自动归一」：

  python calibrate_optical_scales.py            # 只算 + 写 JSON + 报表
  python calibrate_optical_scales.py --apply    # 再顺跑重烘 + 同步官网 + 复测

度量口径（v3，2026-07-25 晚修正）：
  归一目标不是墨量、也不是外接框，而是二者的加权混合

      visual = eq^(1-w) x bboxGeo^w        w = mix_w，JSON 可调

  w=0 纯墨量（v2 口径）：墨量齐了，但外接框离散到 49%，紧凑实心图标（幻颜/
  智控）被压得明显偏小；w=1 纯外接框（改版前的老口径）：占格齐了，但稀疏
  图标视觉过轻。实测取 w≈0.55 两者兼顾。

  ⚠ v2 的 shape_correction（fill 越高 → 目标越小）方向是错的：它让实心图标
  更小，与真实感知相反（用户实测反馈：幻颜/智控显小）。混合口径的 bboxGeo
  维度数学上等价于 fill 的负幂修正，方向正确且连续，故 shape_correction
  默认置 0 弃用（保留键仅为兼容旧 JSON）。

  visual 严格线性于 k（eq 与 geo 同为线性尺寸量），故 k_ideal 可直接解析求解。

原理（与生产管线同源，零近似）：
  1. 目标 = 全部图标 visual 的几何平均（整体尺寸守恒，不因单张改图而整体漂移）。
  2. 每张图标解 k，使 boxed_square(icon, 256, k) 的实测 visual ≈ 目标。
  3. 裁切护栏：k 超过画布可容纳上限（K_CAP，按 512/256/128 三尺寸取最紧，
     长边距画布边至少 1px）时截断并进 capped 列表。
  4. expected_eq / expected_visual = 用最终 k 真实试烘一遍的实测值（不是线性
     外推），写进 JSON 供 repo_doctor 门禁校验「官网产物 == 校准预期」。

JSON 契约（新增键向后兼容，bake/前端只消费 applyInUi + scales）：
  scales           每图标最终系数（overrides 优先于自动值）
  expected_eq      烘焙产物应实测到的等效直径（256 画布，px）
  expected_visual  混合口径视觉尺寸——门禁的自一致性应校验本项而非 expected_eq
  capped           被裁切护栏截断、允许偏离基线的图标
  overrides        人工微调段（本脚本永不覆盖，留给美术验收后手调）
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
ANCHOR = "chatx"            # 仅报表参考；实际目标是全体 visual 的几何平均
MIX_W = 0.55                # 混合口径权重：0=纯墨量 1=纯外接框（见文件头）
FILL_PIVOT = 0.55           # 弃用（shape_correction 的触发点）
SHAPE_CORRECTION = 0.0      # 弃用：方向与真实感知相反，由 MIX_W 连续替代
K_MIN = 0.6                 # 混合口径下缩放幅度更大，下限相应放宽
SAFE_EDGE = 1               # 长边距画布边最少留 1px（防 paste 负坐标裁像素）

# 画布可容纳的系数上限：按三个产出尺寸取最紧（128px 最紧 ≈ 1.177）
K_CAP = min(
    (s - 2 * SAFE_EDGE) / int(s * (1 - 2 * PAD))
    for s in (512, 256, 128)
)


def alpha_metrics(img):
    """返回 (等效直径, fill率, 外接框几何均值)。fill = alpha面积 / bbox 面积。"""
    a = img.getchannel("A")
    hist = a.histogram()
    area = sum(v * count for v, count in enumerate(hist)) / 255.0
    eq = 2.0 * math.sqrt(area / math.pi)
    bbox = a.getbbox()
    if not bbox:
        return 0.0, 0.0, 0.0
    bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    fill = area / float(bw * bh) if bw and bh else 0.0
    return eq, fill, math.sqrt(float(bw * bh))


def visual_size(eq, geo, w):
    """混合口径视觉尺寸：墨量与外接框的加权几何混合（见文件头）。"""
    if eq <= 0 or geo <= 0:
        return 0.0
    return (eq ** (1.0 - w)) * (geo ** w)


def baked_metrics(master, k, w):
    """用真实生产管线（boxed_square@256）试烘一次，实测 (visual, eq, geo)。"""
    eq, _, geo = alpha_metrics(boxed_square(master, CANVAS, PAD, optical_scale=k))
    return visual_size(eq, geo, w), eq, geo


def load_masters():
    masters = {}
    for k in PRODUCTS:
        path = os.path.join(KEYED, k + "-keyed.png")
        if not os.path.isfile(path):
            raise SystemExit("missing keyed master: " + path)
        masters[k] = Image.open(path).convert("RGBA")
    return masters


def calibrate(existing, mix_w=None):
    masters = load_masters()
    w = MIX_W if mix_w is None else float(mix_w)
    overrides = dict(existing.get("overrides") or {})

    # k=1 基准量测（visual 线性于 k，故一次量测即可解析求解）
    raw = {}
    for key in PRODUCTS:
        eq0, fill0, geo0 = alpha_metrics(
            boxed_square(masters[key], CANVAS, PAD, optical_scale=1.0))
        raw[key] = (visual_size(eq0, geo0, w), eq0, fill0, geo0)

    # 目标 = 全体 visual 的几何平均：整体尺寸守恒，不因单张改图而集体漂移
    target = math.exp(sum(math.log(raw[k][0]) for k in PRODUCTS) / len(PRODUCTS))

    rows = []
    scales, expected, expected_visual, capped = {}, {}, {}, []
    for key in PRODUCTS:
        v0, eq0, fill0, geo0 = raw[key]
        k_ideal = target / v0 if v0 else 1.0
        k_auto = max(K_MIN, min(K_CAP, k_ideal))
        if k_ideal > K_CAP and (k_ideal - K_CAP) / k_ideal > 0.02:
            capped.append(key)
        k_final = float(overrides.get(key, round(k_auto, 3)))
        scales[key] = round(k_final, 3)
        vis, eq, geo = baked_metrics(masters[key], k_final, w)
        expected[key] = round(eq, 1)
        expected_visual[key] = round(vis, 1)
        rows.append({
            "key": key, "raw_eq": eq0, "fill": fill0, "k_ideal": k_ideal,
            "k_final": scales[key], "expected": expected[key], "visual": vis,
            "geo": geo, "capped": key in capped, "override": key in overrides,
        })
    return {
        "mix_w": w, "target_visual": target,
        "eq_anchor": baked_metrics(masters[ANCHOR], 1.0, w)[1],
        "scales": scales, "expected_eq": expected,
        "expected_visual": expected_visual, "capped": capped,
        "overrides": overrides, "rows": rows,
    }


def write_json(existing, result):
    out = {
        "_note": ("产品图标光学补偿单一真相（官网/头像/lockup/桌面图标共用）。"
                  "由 brand-assets/calibrate_optical_scales.py 自动校准生成——"
                  "换图标后重跑该脚本即可，勿手改 scales/expected_eq；"
                  "人工微调请写 overrides。applyInUi=false 表示系数已烘焙进资产，"
                  "前端 ProductIcon 不再叠加 CSS 缩放。"),
        "version": "3.0.0",
        "applyInUi": bool(existing.get("applyInUi", False)),
        "anchor": ANCHOR,
        "mix_w": result["mix_w"],
        "target_visual": round(result["target_visual"], 1),
        "baseline_eq": round(result["eq_anchor"], 1),
        "shape_correction": 0.0,
        "k_cap": round(K_CAP, 4),
        "auto_calibrated_at": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "scales": result["scales"],
        "expected_eq": result["expected_eq"],
        "expected_visual": result["expected_visual"],
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
    base = result["target_visual"]
    print("")
    print("mix_w=%.2f  target_visual=%.1f  k_cap=%.3f  (w=0 纯墨量 / w=1 纯外接框)" % (
        result["mix_w"], base, K_CAP))
    print("-" * 84)
    print("%-8s %6s %8s %8s %8s %8s %8s  %s" % (
        "key", "fill", "k_old", "k_new", "eq", "geo", "vs_base", "flags"))
    for r in result["rows"]:
        flags = []
        if r["capped"]:
            flags.append("CAPPED")
        if r["override"]:
            flags.append("override")
        print("%-8s %6.2f %8.3f %8.3f %8.1f %8.1f %+7.1f%%  %s" % (
            r["key"], r["fill"], float(old.get(r["key"], 1.0)), r["k_final"],
            r["expected"], r["geo"], (r["visual"] / base - 1) * 100,
            " ".join(flags)))
    print("-" * 84)
    eqs = [r["expected"] for r in result["rows"]]
    geos = [r["geo"] for r in result["rows"]]
    print("离散度(max/min-1):  墨量 eq %+.0f%%   外接框 geo %+.0f%%" % (
        (max(eqs) / min(eqs) - 1) * 100, (max(geos) / min(geos) - 1) * 100))


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
        eq, _, _ = alpha_metrics(Image.open(p).convert("RGBA"))
        dev = (eq - exp) / exp if exp else 0.0
        status = "OK " if abs(dev) <= tol else "FAIL"
        if abs(dev) > tol:
            ok = False
        print("  %-8s eq=%7.1f expected=%7.1f dev=%+5.1f%%  %s" % (key, eq, exp, dev * 100, status))
    return ok


def main():
    ap = argparse.ArgumentParser(description="auto-calibrate product icon optical scales")
    ap.add_argument("--apply", action="store_true", help="rebake icons + sync website + verify")
    ap.add_argument("--w", type=float, default=None,
                    help="mix weight: 0=ink only, 1=bbox only (default from JSON/%.2f)" % MIX_W)
    ap.add_argument("--dry", action="store_true", help="report only, do not write JSON")
    args = ap.parse_args()

    existing = {}
    if os.path.isfile(SCALE_JSON):
        with open(SCALE_JSON, "r", encoding="utf-8") as f:
            existing = json.load(f)

    w = args.w if args.w is not None else existing.get("mix_w", MIX_W)
    result = calibrate(existing, mix_w=w)
    if args.dry:
        print_report(existing, result)
        return 0
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
