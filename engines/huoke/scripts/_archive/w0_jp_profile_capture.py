# -*- coding: utf-8 -*-
"""
W0-2/W0-3: 搜索日本女性名字，截图并分析 30 个 profile，建立 ground truth。

流程:
  1. 从关键词列表中逐一搜索
  2. 对搜索结果的每个候选人:
     a. navigate_to_profile（进主页）
     b. capture_profile_snapshots（截 3 张图）
     c. classify_current_profile（L1 + L2 VLM 判定）
     d. 结果写入 JSON
  3. 自动统计 L1/L2 通过率，输出 ground truth

用法:
  cd d:\mobile-auto-0327\mobile-auto-project
  $env:PYTHONPATH = "$pwd"
  python scripts/w0_jp_profile_capture.py --device 8DWOF6CYY5R8YHX8 --target 30

注意:
  - 需要 FB 已登录
  - 每个候选间自动随机等待 8-18 秒（模拟真人）
  - 截图保存到 data/w0_jp_profiles/{serial}_{timestamp}/
  - Ground truth 写到 data/w0_jp_ground_truth.json
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            Path(__file__).parent.parent / "data" / "w0_capture.log",
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("w0_capture")

# W0 日本女性搜索关键词（分三层：人名 / 兴趣复合 / 罗马字）
SEARCH_KEYWORDS = [
    # W0 专用：全部用 ASCII 罗马字（可靠输入，覆盖使用英文FB的日本女性）
    # 层A：常见日本女性全名（姓+名 罗马字）
    "Yumi Tanaka",
    "Keiko Suzuki",
    "Hanako Yamada",
    "Noriko Sato",
    "Michiko Nakamura",
    "Yoko Ito",
    "Kazuko Kobayashi",
    "Fumiko Kato",
    "Hiroko Yoshida",
    "Masako Yamamoto",
    "Sachiko Nakajima",
    "Yoshiko Hayashi",
    "Kimiko Shimizu",
    "Haruko Inoue",
    "Fujiko Kimura",
    # 层B：仅姓氏（搜索范围更广）
    "Tanaka Japan",
    "Suzuki Japan",
    "Watanabe Japan",
    "Ito Japan",
    "Yamamoto Japan",
    # 层C：兴趣关键词（部分在 FB 个人简介可搜到）
    "Japanese housewife blog",
    "Japan mom life",
    "Japanese woman yoga",
    "Japan cooking mom",
    "Japanese travel blog woman",
    "Japan mama lifestyle",
    "Japanese knitting hobby",
    "Japan senior woman",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", required=True, help="ADB 序列号（必填）")
    ap.add_argument("--target", type=int, default=30, help="目标 profile 数量（默认 30）")
    ap.add_argument("--max-per-query", type=int, default=3,
                    help="每个关键词最多取几个结果（默认 3）")
    ap.add_argument("--persona", default="jp_female_midlife", help="目标画像 key")
    ap.add_argument("--skip-l2", action="store_true", help="跳过 L2 VLM（仅做 L1 文本）")
    ap.add_argument("--output", default="",
                    help="输出 JSON 路径（默认 data/w0_jp_ground_truth.json）")
    args = ap.parse_args()

    # 设置 data 目录
    base_dir = Path(__file__).parent.parent
    data_dir = base_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.output) if args.output else data_dir / "w0_jp_ground_truth.json"

    log.info("=== W0-2/W0-3: Profile 抓取与分析 ===")
    log.info("设备: %s  目标数量: %d  Persona: %s", args.device, args.target, args.persona)

    # 导入自动化类
    sys.path.insert(0, str(base_dir))
    try:
        from src.app_automation.facebook import FacebookAutomation
        from src.host.fb_target_personas import get_persona
    except ImportError as e:
        log.error("导入失败: %s\n请确认 PYTHONPATH 已设置为项目根目录", e)
        sys.exit(1)

    persona = get_persona(args.persona)
    if not persona:
        log.error("Persona '%s' 不存在", args.persona)
        sys.exit(1)

    log.info("使用 Persona: %s (%s)", persona.get("name"), args.persona)

    fb = FacebookAutomation()
    shot_dir_root = data_dir / "w0_jp_profiles" / args.device

    captured: list[dict] = []
    failed_queries: list[str] = []
    stats = {
        "total_searched": 0,
        "nav_ok": 0,
        "nav_fail": 0,
        "l1_pass": 0,
        "l1_fail": 0,
        "l2_run": 0,
        "l2_match": 0,
        "l2_reject": 0,
    }

    # W0 改进版：直接用名字调 navigate_to_profile，不依赖 search_people 结果解析
    # 原因：_extract_search_results 在中文 FB 界面返回 0（需要 People tab 过滤）
    # navigate_to_profile(display_name) 内部会搜索+点第一条，效果等同
    for keyword in SEARCH_KEYWORDS:
        if len(captured) >= args.target:
            log.info("已达目标数量 %d，停止搜索", args.target)
            break

        log.info("\n=== 处理名字: 「%s」 ===", keyword)
        # 直接把 keyword 当作 display_name 候选进行 navigate
        results_to_try = [keyword]  # 每个关键词就是一个候选
        stats["total_searched"] += 1

        for candidate_name in results_to_try:
            if len(captured) >= args.target:
                break

            name = candidate_name.strip()
            if not name:
                continue

            log.info("  → 候选: 「%s」", name)

            # 人随机等待（模拟真人翻看结果）
            time.sleep(random.uniform(3, 6))

            # Step 1: 进入 profile
            try:
                nav = fb.navigate_to_profile(
                    name,
                    device_id=args.device,
                    post_open_dwell_sec=(2.5, 5.0),
                )
            except Exception as e:
                log.warning("    navigate_to_profile 异常: %s", e)
                stats["nav_fail"] += 1
                continue

            if not nav.get("ok"):
                log.warning("    进入 profile 失败: %s", nav.get("reason"))
                stats["nav_fail"] += 1
                time.sleep(random.uniform(2, 4))
                continue

            stats["nav_ok"] += 1
            target_key = nav.get("target_key", f"search:{name}")
            log.info("    target_key: %s  via: %s", target_key, nav.get("via"))

            # Step 2: 截图
            ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            shot_dir = str(shot_dir_root / f"{ts_str}_{name[:10]}")
            try:
                snap = fb.capture_profile_snapshots(
                    shot_count=3,
                    scroll_between=True,
                    device_id=args.device,
                    save_dir=shot_dir,
                    tag=f"jp_{len(captured)+1:03d}",
                )
            except Exception as e:
                log.warning("    截图失败: %s", e)
                snap = {"image_paths": [], "display_name": name, "bio_text": "", "shot_count": 0}

            log.info("    截图 %d 张  display_name: 「%s」  bio: 「%s」",
                     snap.get("shot_count", 0),
                     snap.get("display_name", ""),
                     snap.get("bio_text", "")[:80])

            # Step 3: L1 + L2 分类
            try:
                clf_result = fb.classify_current_profile(
                    target_key=target_key,
                    persona_key=args.persona,
                    task_id=f"w0_{len(captured)+1:03d}",
                    shot_count=0 if args.skip_l2 else len(snap.get("image_paths", [])),
                    device_id=args.device,
                )
            except Exception as e:
                log.warning("    分类失败: %s", e)
                clf_result = {"match": False, "score": 0, "reason": str(e)}

            l1_pass = (clf_result.get("l1") or {}).get("pass", False)
            match = bool(clf_result.get("match"))
            score = float(clf_result.get("score") or 0)
            stage = clf_result.get("stage_reached", "")

            if l1_pass:
                stats["l1_pass"] += 1
            else:
                stats["l1_fail"] += 1

            if "L2" in stage:
                stats["l2_run"] += 1
                if match:
                    stats["l2_match"] += 1
                else:
                    stats["l2_reject"] += 1

            log.info("    L1 通过: %s  L2: %s  分类: %s  分数: %.1f",
                     l1_pass, stage, "✅ 精准客户" if match else "❌ 不匹配", score)

            # 记录结果
            entry = {
                "idx": len(captured) + 1,
                "keyword": keyword,
                "name": name,
                "target_key": target_key,
                "nav_kind": nav.get("kind"),
                "nav_via": nav.get("via"),
                "display_name": snap.get("display_name", ""),
                "bio_text": snap.get("bio_text", ""),
                "shot_count": snap.get("shot_count", 0),
                "image_paths": snap.get("image_paths", []),
                "l1_pass": l1_pass,
                "l1_score": float((clf_result.get("l1") or {}).get("score") or 0),
                "l1_reasons": (clf_result.get("l1") or {}).get("reasons") or [],
                "l2_match": match,
                "l2_score": score,
                "stage_reached": stage,
                "insights": clf_result.get("insights") or {},
                "captured_at": datetime.now().isoformat(),
            }
            captured.append(entry)

            log.info("    [%d/%d] 已记录", len(captured), args.target)

            # 回到 feed
            try:
                from src.app_automation.facebook import FacebookAutomation as _FA
                # 按返回键回到主页
                import subprocess
                subprocess.run(
                    [r"C:\platform-tools\adb.exe", "-s", args.device, "shell",
                     "input", "keyevent", "4"],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass

            # 候选间等待
            wait = random.uniform(8, 18)
            log.info("    等待 %.1f 秒...", wait)
            time.sleep(wait)

        # 关键词间额外等待
        time.sleep(random.uniform(5, 12))

    # 最终统计
    log.info("\n=== W0 抓取完成 ===")
    log.info("成功抓取: %d  目标: %d", len(captured), args.target)
    log.info("搜索成功率: %.1f%%  (nav_ok=%d / total_searched=%d)",
             100 * stats["nav_ok"] / max(stats["total_searched"], 1),
             stats["nav_ok"], stats["total_searched"])
    log.info("L1 通过率: %.1f%%  (pass=%d / total=%d)",
             100 * stats["l1_pass"] / max(stats["l1_pass"] + stats["l1_fail"], 1),
             stats["l1_pass"], stats["l1_pass"] + stats["l1_fail"])
    if stats["l2_run"]:
        log.info("L2 通过率: %.1f%%  (match=%d / l2_run=%d)",
                 100 * stats["l2_match"] / stats["l2_run"],
                 stats["l2_match"], stats["l2_run"])
    if failed_queries:
        log.warning("搜索失败关键词: %s", failed_queries)

    # 写 ground truth JSON
    output = {
        "w0_version": "1.0",
        "captured_at": datetime.now().isoformat(),
        "device": args.device,
        "persona_key": args.persona,
        "target_count": args.target,
        "actual_count": len(captured),
        "stats": stats,
        "failed_queries": failed_queries,
        "profiles": captured,
    }
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Ground truth 已写入: %s", out_path)

    # 分析报告
    _print_analysis(captured)
    return captured


def _print_analysis(profiles: list[dict]):
    """输出简短分析报告。"""
    total = len(profiles)
    if total == 0:
        log.info("无数据可分析")
        return

    l1_ok = [p for p in profiles if p.get("l1_pass")]
    l2_ok = [p for p in profiles if p.get("l2_match")]
    has_bio = [p for p in profiles if p.get("bio_text")]
    has_pics = [p for p in profiles if p.get("shot_count", 0) > 0]

    log.info("\n====== W0 分析报告 ======")
    log.info("总 profile 数 : %d", total)
    log.info("L1 通过       : %d (%.0f%%)", len(l1_ok), 100 * len(l1_ok) / total)
    log.info("L2 精准客户   : %d (%.0f%%)", len(l2_ok), 100 * len(l2_ok) / total)
    log.info("有 bio 文本   : %d (%.0f%%)", len(has_bio), 100 * len(has_bio) / total)
    log.info("截图成功      : %d (%.0f%%)", len(has_pics), 100 * len(has_pics) / total)

    # L2 通过的样本
    if l2_ok:
        log.info("\n精准客户样本（L2 通过）:")
        for p in l2_ok[:5]:
            log.info("  • %s  score=%.1f  bio=「%s」",
                     p["name"], p["l2_score"], p["bio_text"][:60])

    # keyword 层效果
    from collections import defaultdict
    kw_stats: dict[str, dict] = defaultdict(lambda: {"total": 0, "l2_match": 0})
    for p in profiles:
        kw = p["keyword"]
        kw_stats[kw]["total"] += 1
        if p.get("l2_match"):
            kw_stats[kw]["l2_match"] += 1
    log.info("\n关键词精准率排名（Top 10）:")
    ranked = sorted(kw_stats.items(), key=lambda x: x[1]["l2_match"] / max(x[1]["total"], 1),
                    reverse=True)
    for kw, s in ranked[:10]:
        log.info("  「%s」: %d/%d 精准 (%.0f%%)",
                 kw, s["l2_match"], s["total"], 100 * s["l2_match"] / max(s["total"], 1))


if __name__ == "__main__":
    main()
