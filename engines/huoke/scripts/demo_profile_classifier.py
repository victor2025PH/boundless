# -*- coding: utf-8 -*-
"""P2-4 Sprint A Demo: Facebook 目标画像分类器 CLI。

用途：
    * 不依赖真机 / Facebook，本地验证 L1 规则 + L2 VLM 全链路。
    * 可以用 --benchmark 跑一组预置 case，得到准确率/耗时报告，
      用于「再优化」阶段的对比基线。

使用示例::

    # 单样本快速测试（没有图就只走 L1）
    python scripts/demo_profile_classifier.py single --name "山田花子" \\
        --bio "東京在住、45 歳、料理と旅行が趣味" --locale ja-JP

    # 单样本加图（走 L2）
    python scripts/demo_profile_classifier.py single --name "山田花子" \\
        --bio "..." --image path/to/avatar.jpg --image path/to/page1.jpg

    # 跑内置 benchmark（12 个合成 case），打印准确率/耗时
    python scripts/demo_profile_classifier.py benchmark
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.host import fb_profile_classifier, fb_target_personas  # noqa: E402


def _green(s): return f"\033[32m{s}\033[0m"
def _red(s): return f"\033[31m{s}\033[0m"
def _yellow(s): return f"\033[33m{s}\033[0m"


# 合成 benchmark cases（无需真图时只走 L1，有图走 L2）
BENCH_CASES = [
    {"name": "山田花子", "bio": "東京在住、45 歳、料理と旅行が趣味です",
     "username": "hanako_jp", "locale": "ja-JP", "expected_l1_pass": True},
    {"name": "佐藤美恵", "bio": "主婦です。毎日お弁当作り頑張ってます",
     "username": "mie.jp", "locale": "ja-JP", "expected_l1_pass": True},
    {"name": "鈴木智子", "bio": "子育てと仕事の両立、頑張る 50 代ママ",
     "username": "tomoko_52", "locale": "ja-JP", "expected_l1_pass": True},
    {"name": "田中理恵", "bio": "旅行好きなおばさんです。ペットは柴犬 2 匹",
     "username": "rie_tanaka", "locale": "ja-JP", "expected_l1_pass": True},
    # 日文但性别 / 年龄可能不符合（L1 能过，L2 看图才分辨）
    {"name": "山田太郎", "bio": "東京在住、プログラマー",
     "username": "taro_yamada", "locale": "ja-JP", "expected_l1_pass": True},
    # 英文用户（不该过 L1）
    {"name": "John Smith", "bio": "NYC based, dad of 2",
     "username": "john.smith", "locale": "en-US", "expected_l1_pass": False},
    {"name": "Sarah Johnson", "bio": "Florida, retired teacher",
     "username": "sarah_j", "locale": "en-US", "expected_l1_pass": False},
    # 空字段（极端防御）
    {"name": "", "bio": "", "username": "", "locale": "",
     "expected_l1_pass": False},
    # 混合日英
    {"name": "Miyuki Tanaka", "bio": "料理 blog を書いています",
     "username": "miyuki_t", "locale": "", "expected_l1_pass": True},
    {"name": "みゆき", "bio": "", "username": "miyu", "locale": "",
     "expected_l1_pass": True},
    {"name": "Ken Sato", "bio": "Tokyo resident", "username": "ken_sato",
     "locale": "en-US", "expected_l1_pass": False},
    # 边缘案例：日文 bio 但昵称英文且无其他信号
    {"name": "Anna", "bio": "日本が大好き", "username": "anna",
     "locale": "", "expected_l1_pass": False},
]


def run_single(args):
    image_paths = [str(p) for p in (args.image or []) if Path(p).exists()]
    missing = [p for p in (args.image or []) if not Path(p).exists()]
    if missing:
        print(_yellow(f"跳过不存在的图片: {missing}"))

    t0 = time.time()
    r = fb_profile_classifier.classify(
        device_id=args.device_id,
        task_id=args.task_id,
        persona_key=args.persona,
        target_key=args.target_key or args.name or "cli_sample",
        display_name=args.name,
        bio=args.bio,
        username=args.username,
        locale=args.locale,
        image_paths=image_paths,
        l2_image_paths=image_paths,
        do_l2=args.do_l2 and bool(image_paths),
        dry_run=args.dry_run,
    )
    elapsed = int((time.time() - t0) * 1000)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print(f"\nelapsed: {elapsed} ms  match={r['match']}  "
          f"stage={r['stage_reached']}  score={r['score']:.1f}")


def run_benchmark(args):
    persona = fb_target_personas.get_persona(args.persona)
    print(f"persona: {persona['persona_key']} ({persona.get('name')})")
    print(f"pass_threshold: {persona.get('l1',{}).get('pass_threshold')}")
    print("=" * 72)

    l1_correct = 0
    l1_total = len(BENCH_CASES)
    total_ms = 0
    for idx, c in enumerate(BENCH_CASES, 1):
        t0 = time.time()
        r = fb_profile_classifier.classify(
            device_id="BENCH_DEVICE",
            task_id="bench",
            persona_key=args.persona,
            target_key=f"bench_{idx}_{c.get('username') or c.get('name','x')}",
            display_name=c["name"],
            bio=c["bio"],
            username=c.get("username", ""),
            locale=c.get("locale", ""),
            image_paths=[],
            do_l2=False,
            dry_run=True,
        )
        ms = int((time.time() - t0) * 1000)
        total_ms += ms
        l1 = r["l1"]
        pass_ = l1["pass"] if l1 else False
        expected = c["expected_l1_pass"]
        ok = pass_ == expected
        l1_correct += int(ok)
        tag = _green("OK ") if ok else _red("BAD")
        print(f"  {tag} #{idx:02d}  L1={l1['score']:>3.0f}  pass={pass_}  "
              f"(期望={expected})  ms={ms:>3d}  name={c['name']!r}  bio={c['bio'][:30]!r}")
        if not ok:
            print(f"       reasons={l1['reasons']}")

    acc = 100 * l1_correct / l1_total
    avg_ms = total_ms / max(1, l1_total)
    print("=" * 72)
    color = _green if acc >= 90 else (_yellow if acc >= 70 else _red)
    print(color(f"L1 准确率: {l1_correct}/{l1_total} = {acc:.1f}%   平均耗时: {avg_ms:.2f} ms/case"))

    # VLM 健康检查
    try:
        from src.host import ollama_vlm
        h = ollama_vlm.check_health()
        print(f"\nVLM 健康: online={h['online']}  model_available={h['model_available']}  "
              f"model={h['model']}")
    except Exception as e:
        print(f"\nVLM 健康检查失败: {e}")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("single", help="单样本测试")
    ps.add_argument("--name", default="", help="display_name")
    ps.add_argument("--bio", default="")
    ps.add_argument("--username", default="")
    ps.add_argument("--locale", default="")
    ps.add_argument("--image", action="append", help="可多次指定；有图则走 L2")
    ps.add_argument("--persona", default=None)
    ps.add_argument("--device-id", default="CLI_DEV", dest="device_id")
    ps.add_argument("--task-id", default="cli-demo", dest="task_id")
    ps.add_argument("--target-key", default="", dest="target_key")
    ps.add_argument("--do-l2", action="store_true", dest="do_l2",
                    help="显式要求走 L2（需提供 --image）")
    ps.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="不写库（适合试参数）")
    ps.set_defaults(func=run_single)

    pb = sub.add_parser("benchmark", help="内置 12 个合成 case，跑 L1 准确率")
    pb.add_argument("--persona", default=None)
    pb.set_defaults(func=run_benchmark)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
