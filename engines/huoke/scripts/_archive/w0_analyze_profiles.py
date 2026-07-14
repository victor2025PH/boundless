# -*- coding: utf-8 -*-
"""
W0-3: 对已抓取的 29 个日本女性 FB profile 做 L1+L2 分类分析。
读取 data/w0_jp_ground_truth_v2.json，对每个 profile 运行 classify()，
生成分析报告 data/w0_classify_report.json 和控制台摘要。
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

INPUT_JSON = "data/w0_jp_ground_truth_v2.json"
OUTPUT_JSON = "data/w0_classify_report.json"


def clean_display_name(raw: str, search_name: str = "") -> str:
    """清理 display_name：移除乱码 location/university 前缀"""
    if not raw:
        return search_name
    # 如果包含逗号且有乱码，取逗号前部分
    if "," in raw:
        parts = raw.split(",")
        first = parts[0].strip()
        # 如果第一部分看起来像人名（2个词，首字母大写），使用它
        words = first.split()
        if len(words) >= 2 and all(w[0].isupper() for w in words if w):
            return first
    return raw


def main():
    if not os.path.exists(INPUT_JSON):
        log.error("找不到输入文件: %s", INPUT_JSON)
        sys.exit(1)

    with open(INPUT_JSON, encoding="utf-8") as f:
        data = json.load(f)

    profiles = data.get("profiles", [])
    log.info("共 %d 个 profile 待分类", len(profiles))

    # 预热 VLM
    try:
        from src.host.ollama_vlm import warmup
        log.info("正在预热 VLM (qwen2.5vl:7b)...")
        warmup(force=True)
        log.info("VLM 预热完成")
    except Exception as e:
        log.warning("VLM 预热失败（继续）: %s", e)

    from src.host.fb_profile_classifier import classify

    results = []
    stats = {
        "total": len(profiles),
        "l1_pass": 0,
        "l1_fail": 0,
        "l2_run": 0,
        "l2_match": 0,
        "errors": 0,
    }

    for p in profiles:
        seq = p["seq"]
        search_name = p.get("search_name", "")
        raw_name = p.get("display_name", "")
        display_name = clean_display_name(raw_name, search_name)
        bio = p.get("bio", "")
        image_paths = [x for x in p.get("image_paths", []) if os.path.exists(x)]
        target_key = p.get("target_key", f"search:{search_name}")

        log.info("\n=== [%02d/%d] %s ===", seq, len(profiles), display_name)
        log.info("  images: %d  bio[:60]: %r", len(image_paths), bio[:60])

        t0 = time.time()
        try:
            result = classify(
                device_id="w0_analysis",
                task_id=f"w0_analyze_{seq:03d}",
                persona_key="jp_female_midlife",
                target_key=target_key,
                display_name=display_name,
                bio=bio,
                username="",
                locale="ja",
                image_paths=image_paths,
                l2_image_paths=image_paths,
                do_l2=True,
                dry_run=False,
            )
        except Exception as e:
            log.error("  分类失败: %s", e)
            stats["errors"] += 1
            result = {"match": False, "stage_reached": "error", "error": str(e)}

        elapsed = time.time() - t0

        l1 = result.get("l1") or {}
        l1_pass = l1.get("pass", False)
        l1_score = l1.get("score", 0)
        stage = result.get("stage_reached", "?")
        match = result.get("match", False)
        score = result.get("score", 0)
        insights = result.get("insights") or {}

        if l1_pass:
            stats["l1_pass"] += 1
        else:
            stats["l1_fail"] += 1
        if stage == "L2":
            stats["l2_run"] += 1
        if match:
            stats["l2_match"] += 1

        log.info("  L1 score=%.1f pass=%s | stage=%s match=%s score=%.1f  [%.1fs]",
                 l1_score, l1_pass, stage, match, score, elapsed)
        if l1.get("reasons"):
            log.info("  L1 reasons: %s", l1["reasons"])
        if insights:
            log.info("  insights: gender=%s age_band=%s is_jp=%s conf=%.2f",
                     insights.get("gender", "?"),
                     insights.get("age_band", "?"),
                     insights.get("is_japanese", "?"),
                     float(insights.get("overall_confidence", 0) or 0))

        results.append({
            "seq": seq,
            "search_name": search_name,
            "display_name": display_name,
            "bio_snippet": bio[:120],
            "image_count": len(image_paths),
            "l1_score": l1_score,
            "l1_pass": l1_pass,
            "l1_reasons": l1.get("reasons", []),
            "stage_reached": stage,
            "match": match,
            "score": score,
            "gender": insights.get("gender", ""),
            "age_band": insights.get("age_band", ""),
            "is_japanese": insights.get("is_japanese", False),
            "is_japanese_confidence": insights.get("is_japanese_confidence", 0),
            "overall_confidence": insights.get("overall_confidence", 0),
            "interests": insights.get("interests", []),
            "elapsed_s": round(elapsed, 1),
            "from_cache": result.get("from_cache", False),
        })

        # 小间隔防止 GPU OOM
        if stage == "L2":
            time.sleep(3)

    # 保存报告
    report = {
        "stats": stats,
        "results": results,
    }
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # 打印摘要
    print("\n" + "="*60)
    print("W0-3 分类分析完成")
    print("="*60)
    print(f"总计:     {stats['total']} 个 profile")
    print(f"L1 通过:  {stats['l1_pass']} ({stats['l1_pass']/max(1,stats['total'])*100:.0f}%)")
    print(f"L1 失败:  {stats['l1_fail']}")
    print(f"L2 运行:  {stats['l2_run']}")
    print(f"L2 命中:  {stats['l2_match']} (精准客户)")
    print(f"错误:     {stats['errors']}")
    print()

    # 命中的精准客户
    matched = [r for r in results if r["match"]]
    if matched:
        print(f"✅ 精准客户 ({len(matched)} 人):")
        for r in matched:
            print(f"  [{r['seq']:02d}] {r['display_name']} "
                  f"gender={r['gender']} age={r['age_band']} "
                  f"conf={r['overall_confidence']:.2f}")
    else:
        print("⚠️  未找到精准匹配 (L2 未命中)")

    # L1 通过但 L2 未命中
    l1_only = [r for r in results if r["l1_pass"] and not r["match"]]
    if l1_only:
        print(f"\nL1 通过但 L2 未命中 ({len(l1_only)} 人):")
        for r in l1_only:
            print(f"  [{r['seq']:02d}] {r['display_name']} "
                  f"gender={r['gender']} age={r['age_band']} "
                  f"ja_conf={r['is_japanese_confidence']:.2f}")

    print(f"\n详细报告已保存: {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
