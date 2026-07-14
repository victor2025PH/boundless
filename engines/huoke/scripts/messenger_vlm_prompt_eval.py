#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Messenger VLM prompt 质量回归 — offline eval 工具.

基于已有真机截图 (ground truth bbox 已知) call VLM, 输出命中率 + latency +
503 error 频率。为 VLM Level 4 fallback 的 prompt 工程提供**可重复 regression**
基础 — 改 prompt / 切 Gemini 版本 / 换 provider 前后跑一遍即可验证效果。

**使用场景**:
  * prompt tuning 前后对比 hit rate
  * Gemini 版本升级 (e.g. 2.5 Flash → 3.0 Pro) 回归
  * Ollama 本地 model 切换 (llava → moondream) 验证
  * Messenger UI 改版后 prompt 是否需更新 (hit rate 骤降是 canary)

**不做**: 自动 prompt tuning / VLM 训练 / 真机 UI 交互

用法::

    # default cases (scripts/vlm_eval_dataset/cases.yaml)
    python scripts/messenger_vlm_prompt_eval.py

    # 自定义 dataset
    python scripts/messenger_vlm_prompt_eval.py --cases path/to/cases.yaml

    # JSON 输出供 CI / 对比
    python scripts/messenger_vlm_prompt_eval.py --json > eval_before.json
    # ... 改 prompt ...
    python scripts/messenger_vlm_prompt_eval.py --json > eval_after.json

**Dataset YAML 格式**: 见 scripts/vlm_eval_dataset/cases.yaml

**退出码**:
  * 0 — 所有 case 全 HIT (或 SKIP)
  * 1 — 有 WRONG / MISS / ERROR
  * 2 — 配置/依赖错误 (no dataset / no VLM provider)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Repo root 加到 sys.path, 让 `python scripts/messenger_vlm_prompt_eval.py`
# 和 `python -m scripts.messenger_vlm_prompt_eval` 都能 import `src.*`
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@dataclass
class EvalCase:
    """一个 eval case: 给定 screenshot + target + context + ground truth bbox."""
    screenshot: str
    target: str
    context: str = ""
    ground_truth_bbox: Tuple[int, int, int, int] = (0, 0, 0, 0)
    note: str = ""


@dataclass
class EvalResult:
    """一个 case 跑完后结果."""
    case: EvalCase
    status: str  # HIT / WRONG / MISS / SKIP / ERROR
    coordinates: Optional[Tuple[int, int]] = None
    latency_sec: float = 0.0
    error: str = ""
    raw_response: str = ""


def load_cases(path: Path) -> List[EvalCase]:
    """Load cases from YAML. 空 list 返 []."""
    import yaml
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
    out: List[EvalCase] = []
    for i, d in enumerate(data):
        try:
            # bbox yaml list → tuple
            if "ground_truth_bbox" in d and isinstance(d["ground_truth_bbox"], list):
                d = dict(d)
                d["ground_truth_bbox"] = tuple(d["ground_truth_bbox"])
            out.append(EvalCase(**d))
        except TypeError as e:
            print(f"WARN: skipping malformed case #{i}: {e}", file=sys.stderr)
    return out


def resolve_screenshot(name: str, base_dir: Path) -> Optional[Path]:
    """找 screenshot 文件 — 尝试 base_dir/name, cwd/name, 绝对路径."""
    candidates = [
        base_dir / name,
        base_dir / "screenshots" / name,
        Path.cwd() / name,
        Path(name),
    ]
    for c in candidates:
        try:
            if c.exists() and c.is_file():
                return c.resolve()
        except (OSError, ValueError):
            continue
    return None


def run_one(vf, case: EvalCase, base_dir: Path) -> EvalResult:
    """跑一个 case: screenshot → VLM → bbox 比对."""
    shot = resolve_screenshot(case.screenshot, base_dir)
    if shot is None:
        return EvalResult(
            case=case, status="SKIP",
            error=f"screenshot not found: {case.screenshot}")
    try:
        img_bytes = shot.read_bytes()
    except Exception as e:
        return EvalResult(
            case=case, status="ERROR", error=f"read_bytes: {e}")
    t0 = time.time()
    try:
        r = vf.find_element(
            device=None, target=case.target, context=case.context,
            screenshot_bytes=img_bytes)
    except Exception as e:
        return EvalResult(
            case=case, status="ERROR",
            latency_sec=time.time() - t0, error=str(e)[:200])
    dt = time.time() - t0
    if not r or not r.coordinates:
        return EvalResult(
            case=case, status="MISS", latency_sec=dt,
            raw_response=(r.raw_response if r else "")[:200])
    x, y = r.coordinates
    bb = case.ground_truth_bbox
    in_box = bb[0] <= x <= bb[2] and bb[1] <= y <= bb[3]
    return EvalResult(
        case=case, status="HIT" if in_box else "WRONG",
        coordinates=(x, y), latency_sec=dt,
        raw_response=(r.raw_response or "")[:200])


def render_text(results: List[EvalResult]) -> str:
    """Text report — 对齐列, 末尾 summary."""
    lines: List[str] = []
    for r in results:
        coord = f"{r.coordinates}" if r.coordinates else "None"
        bb = r.case.ground_truth_bbox
        line = (f"[{r.status:5s}] {r.case.screenshot:30s} | "
                f"{r.case.target[:40]:40s} | "
                f"{coord} in {bb} | {r.latency_sec:.1f}s")
        if r.error:
            line += f" | err={r.error[:60]}"
        lines.append(line)
    hit = sum(1 for r in results if r.status == "HIT")
    wrong = sum(1 for r in results if r.status == "WRONG")
    miss = sum(1 for r in results if r.status == "MISS")
    skip = sum(1 for r in results if r.status == "SKIP")
    err = sum(1 for r in results if r.status == "ERROR")
    attempted = hit + wrong + miss + err
    lat_total = sum(r.latency_sec for r in results)
    avg_lat = lat_total / max(1, attempted) if attempted else 0.0
    hit_pct = (hit * 100 // max(1, attempted)) if attempted else 0
    lines.append("")
    lines.append(
        f"=== Summary: HIT {hit} · WRONG {wrong} · MISS {miss} · "
        f"ERROR {err} · SKIP {skip} · total {len(results)} ===")
    lines.append(
        f"=== Hit rate: {hit}/{attempted} ({hit_pct}%) · "
        f"Avg latency: {avg_lat:.1f}s ===")
    return "\n".join(lines)


def results_to_dicts(results: List[EvalResult]) -> List[Dict[str, Any]]:
    """Results → JSON-ready dict list."""
    out = []
    for r in results:
        d = asdict(r.case)
        d["status"] = r.status
        d["coordinates"] = list(r.coordinates) if r.coordinates else None
        d["latency_sec"] = round(r.latency_sec, 2)
        d["error"] = r.error
        d["raw_response"] = r.raw_response
        out.append(d)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--cases", default="scripts/vlm_eval_dataset/cases.yaml",
        help="dataset YAML path (default: scripts/vlm_eval_dataset/cases.yaml)")
    parser.add_argument(
        "--json", action="store_true",
        help="输出 JSON (供 CI / before-after 对比)")
    parser.add_argument(
        "--no-color", action="store_true", help="(保留 — 当前输出无 color)")
    parser.add_argument(
        "--throttle-sec", type=float, default=0.0,
        help=("每 case 间 sleep 秒数, 避 free-tier RPM (default 0). "
              "Gemini 2.5 Flash 免费 ~60 RPM, 14 cases 背靠背会打爆限额 "
              "导致 MISS latency 飙到 ~32s (429 retry cycle 耗尽) 而非 "
              "真 prompt 问题。建议 Gemini: 30s; Ollama/付费: 0"))
    parser.add_argument(
        "--progress", action="store_true",
        help="每 case 结束时往 stderr 打一行 status — 长跑时观察进度")
    args = parser.parse_args(argv)

    cases_path = Path(args.cases).resolve()
    if not cases_path.exists():
        print(f"ERROR: cases file not found: {cases_path}", file=sys.stderr)
        return 2
    base_dir = cases_path.parent

    try:
        from src.ai.vision_fallback import VisionFallback
        from src.ai.llm_client import get_free_vision_client
    except Exception as e:
        print(f"ERROR: import VisionFallback/LLMClient failed: {e}",
              file=sys.stderr)
        return 2

    client = get_free_vision_client()
    if client is None:
        print(
            "ERROR: no free VLM provider. Set GEMINI_API_KEY or run Ollama "
            "with a vision model (llava:7b / moondream / minicpm / bakllava).",
            file=sys.stderr)
        return 2

    vf = VisionFallback(client=client)

    try:
        cases = load_cases(cases_path)
    except Exception as e:
        print(f"ERROR: failed to load cases: {e}", file=sys.stderr)
        return 2
    if not cases:
        print(f"ERROR: no cases in dataset {cases_path}", file=sys.stderr)
        return 2

    results: List[EvalResult] = []
    for i, c in enumerate(cases):
        if i > 0 and args.throttle_sec > 0:
            time.sleep(args.throttle_sec)
        r = run_one(vf, c, base_dir)
        results.append(r)
        if args.progress:
            coord = r.coordinates if r.coordinates else "---"
            print(
                f"  [{i + 1:2d}/{len(cases)}] {r.status:5s} "
                f"{c.screenshot:26s} | {c.target[:35]:35s} "
                f"coords={coord} lat={r.latency_sec:.1f}s",
                file=sys.stderr, flush=True)

    if args.json:
        print(json.dumps(
            {"results": results_to_dicts(results),
             "provider": client.config.provider,
             "vision_model": client.config.vision_model},
            ensure_ascii=False, indent=2))
    else:
        print(render_text(results))
        print(f"\nProvider: {client.config.provider} · "
              f"Model: {client.config.vision_model}")

    # exit 0 only if no WRONG/MISS/ERROR (SKIP OK for missing screenshots)
    any_fail = any(r.status in ("WRONG", "MISS", "ERROR") for r in results)
    return 1 if any_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
