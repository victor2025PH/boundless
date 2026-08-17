# -*- coding: utf-8 -*-
"""翻译引擎 A/B 横比 CLI——「换不换这个 MT 模型」的一条命令答案。

与 `run_eval --translation`（评单个引擎、攒时序趋势）互补：本命令在**同一语料、同一
第三方回译裁判**下把两个候选跑成配对实验，输出统计判词与 per_lang_order 建议。

引擎写法（``--a`` / ``--b`` / ``--judge``）:

  config                     配置里在跑的 ollama_mt（生产基线）
  ollama:MODEL               沿用配置端点，只换模型名
  ollama:MODEL@URL[,URL2]    端点也自定（多端点逗号分隔）
  deterministic              DeepL/Google（需 key）
  ai                         主对话 LLM（DeepSeek 等，走真实 API 有成本）

典型用法——拿一个新模型挑战生产基线，用云端 LLM 当中立裁判::

    python -m scripts.xlate_ab \
        --a config \
        --b ollama:milmmt-46-4b \
        --judge ai \
        --dataset config/eval/translation_samples_hymt.yaml

裁判为什么必须是第三方：候选自己回译自己，量的是「复读自己措辞」的自洽度而非质量
（2026-07-12 周审实证过这个偏置）。裁判与某候选同源时本命令会显式标注污染并拒绝
把数值当选型依据。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# PS5.1 控制台默认 GBK，报告里的中文判词与 ± 号会直接抛 UnicodeEncodeError 或糊成乱码。
# 本命令的全部价值就是给人读那句结论，读不了等于没跑。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from src.eval.translation_ab import (  # noqa: E402
    DEFAULT_MIN_EFFECT,
    DEFAULT_MIN_PAIR_N,
    DEFAULT_TIE_EPS,
    compare,
    format_ab_report,
    suggest_per_lang_order,
)
from src.eval.translation_eval import (  # noqa: E402
    _load_config,
    _probe_ollama_model,
    build_ai_evaluator,
    build_deterministic_evaluator,
    build_local_mt_evaluator,
    evaluate_translation_quality,
    load_translation_samples,
)

DEFAULT_DATASET = "config/eval/translation_samples.yaml"

# 构建结果：(translate_fn, detect_fn, 展示标签, engine 名)
Built = Tuple[Optional[Callable], Optional[Callable], str, str]


def _ollama_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return ((cfg.get("translation") or {}).get("engines") or {}).get("ollama_mt") or {}


def _cfg_urls(cfg: Dict[str, Any]) -> List[str]:
    mc = _ollama_cfg(cfg)
    raw = mc.get("base_urls") or mc.get("base_url") or ""
    if isinstance(raw, (list, tuple)):
        return [str(u or "").strip() for u in raw if str(u or "").strip()]
    return [u.strip() for u in str(raw).split(",") if u.strip()]


def parse_ollama_spec(spec: str, cfg: Dict[str, Any]) -> Tuple[str, List[str]]:
    """拆 ``ollama:MODEL[@URL,...]`` → (model, urls)。

    按**最后一个** ``@`` 切分：Ollama 模型名里带冒号是常态（``hy-mt2-7b:latest``），
    带 ``@`` 不是，所以 rsplit 比 split 安全。省略 ``@URL`` 时沿用配置端点——「只想换个
    模型试试」是最常见的用法，不该逼人重打一遍 URL。
    """
    body = spec.split(":", 1)[1] if ":" in spec else ""
    if "@" in body:
        model, url_part = body.rsplit("@", 1)
        urls = [u.strip() for u in url_part.split(",") if u.strip()]
    else:
        model, urls = body, _cfg_urls(cfg)
    return model.strip(), urls


def _build_ollama(model: str, urls: List[str], cfg: Dict[str, Any], *,
                  probe: bool = True, profile: str = "") -> Built:
    """按显式 model/urls 装配一个 ollama_mt 评测器（temperature=0，与既有评测同口径）。

    ``profile`` 决定 prompt 格式与调用模式。留空则按模型名猜——**猜错就是把新模型
    按旧模型的 prompt 喂**，横比会输在格式而不是质量上，故档名一并写进标签，让报告
    读者一眼看见「这一轮是按哪个契约调的」。
    """
    if not model or not urls:
        return (None, None, "", "")
    if probe:
        urls = [u for u in urls if _probe_ollama_model(u, model)]
        if not urls:
            return (None, None, "", "")
    mc = _ollama_cfg(cfg)
    try:
        from src.ai.mt_profiles import get_profile, guess_profile
        from src.ai.translation_engines import OllamaMTEngine
        from src.ai.translation_service import TranslationService
        prof = get_profile(profile or guess_profile(model))
        eng = OllamaMTEngine(
            base_url=urls,
            model=model,
            api_key=str(mc.get("api_key", "ollama") or "ollama"),
            timeout=float(mc.get("timeout_sec", 20) or 20),
            temperature=0.0,
            max_tokens=int(mc.get("max_tokens", 1024) or 1024),
            keep_alive=str(mc.get("keep_alive", "30m") or ""),
            profile=prof,
        )
        if not eng.available:
            return (None, None, "", "")
        ts = TranslationService(ai_client=None, engines=[eng])
    except Exception:
        return (None, None, "", "")

    async def _translate(text: str, source_lang: str, target_lang: str) -> str:
        res = await ts.translate(text, target_lang=target_lang, source_lang=source_lang)
        return res.translated_text if getattr(res, "ok", False) else ""

    return (_translate, ts.detect_language, f"ollama_mt:{model}[{prof.name}]", "ollama_mt")


def build_candidate(spec: str, cfg: Dict[str, Any], *, probe: bool = True,
                    profile: str = "") -> Built:
    """把引擎写法解析成可调用的评测器。不可用一律返回空 Built，由调用方 skip。"""
    spec = (spec or "").strip()
    if spec.startswith("ollama:"):
        model, urls = parse_ollama_spec(spec, cfg)
        return _build_ollama(model, urls, cfg, probe=probe, profile=profile)
    if spec == "config":
        ev = build_local_mt_evaluator(cfg, probe=probe)
        if ev is None:
            return (None, None, "", "")
        model = str(_ollama_cfg(cfg).get("model") or "?")
        return (ev[0], ev[1], f"ollama_mt:{model}", "ollama_mt")
    if spec == "deterministic":
        ev = build_deterministic_evaluator(cfg)
        return (ev[0], ev[1], "deepl/google", "deterministic") if ev else (None, None, "", "")
    if spec == "ai":
        ev = build_ai_evaluator(cfg)
        return (ev[0], ev[1], "ai(LLM)", "ai") if ev else (None, None, "", "")
    return (None, None, "", "")


def pin_source_langs(samples: List[Any], detect_fn: Optional[Callable],
                     *, fallback: str = "zh") -> List[Any]:
    """跑前把每个样本的源语解析一次并钉进样本。

    否则源语在两轮里各探测一次——探测器（这里是裁判 LLM 的 detect）只要对某个短句
    给出不同答案，该样本在两轮里的对齐键就不同，于是**静默丢掉一个配对**。丢的还
    不是随机样本，正是那些语种歧义的边界样本，等于系统性地把最难的例子从比较里剔除。
    """
    from dataclasses import replace
    out: List[Any] = []
    for s in samples:
        src = str(getattr(s, "source_lang", "") or "").strip().lower()
        if not src and detect_fn is not None:
            try:
                src = (detect_fn(s.text) or "").strip().lower().split("-")[0]
            except Exception:
                src = ""
        out.append(replace(s, source_lang=src or fallback))
    return out


def _build_embed(cfg: Dict[str, Any], enabled: bool):
    if not enabled:
        return None
    try:
        from src.eval.embedding_providers import build_embed_fn
        return build_embed_fn(cfg)
    except Exception:
        return None


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="xlate_ab",
        description="翻译引擎 A/B 横比（同语料 + 同第三方回译裁判 + 配对统计）",
    )
    ap.add_argument("--a", default="config", help="基线候选（默认 config = 生产在跑的 ollama_mt）")
    ap.add_argument("--b", required=True, help="挑战者候选，如 ollama:milmmt-46-4b")
    ap.add_argument("--judge", default="ai",
                    help="回译裁判，须为第三方（默认 ai）；与候选同源会被标注污染")
    ap.add_argument("--a-profile", default="", help="A 的 MT 契约档（留空按模型名猜）")
    ap.add_argument("--b-profile", default="",
                    help="B 的 MT 契约档：hunyuan_mt | milmmt46（留空按模型名猜）")
    ap.add_argument("--allow-self-judge", action="store_true",
                    help="明知裁判污染仍继续（只用于调试，结论不可用于选型）")
    ap.add_argument("--dataset", default=None, help=f"样本集（默认 {DEFAULT_DATASET}）")
    ap.add_argument("--metric", default="auto", choices=["auto", "semantic", "char"],
                    help="主判据轨道（auto=有语义轨就用语义轨）")
    ap.add_argument("--semantic", default="auto", choices=["auto", "off"], help="语义轨开关")
    ap.add_argument("--sample-threshold", type=float, default=0.5, help="单样本合格字符相似度阈")
    ap.add_argument("--sem-threshold", type=float, default=0.8, help="语义救回阈")
    ap.add_argument("--min-effect", type=float, default=DEFAULT_MIN_EFFECT,
                    help="可行动的最小均值差（低于此值即便统计显著也判 marginal）")
    ap.add_argument("--tie-eps", type=float, default=DEFAULT_TIE_EPS, help="逐样本平局带宽")
    ap.add_argument("--min-pair-n", type=int, default=DEFAULT_MIN_PAIR_N, help="语对级结论最小样本数")
    ap.add_argument("--no-probe", action="store_true", help="跳过 Ollama /api/show 就位探测")
    ap.add_argument("--verbose", action="store_true", help="保留引擎侧 INFO 日志（排障用）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--out-jsonl", default=None, help="追加一行横比摘要到 JSONL（攒选型历史）")
    return ap


def _quiet_engine_logs() -> None:
    """把引擎侧 INFO 噪声压到 WARNING——跑 100 次翻译会刷几十行「客户端初始化成功」，
    结论那几行会被淹掉。WARNING 以上保留：裁判调用失败正是需要看见的信号。"""
    import logging
    logging.getLogger("ai_chat_assistant").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.verbose:
        _quiet_engine_logs()
    cfg = _load_config(None)
    probe = not args.no_probe

    a_fn, a_detect, a_label, a_engine = build_candidate(
        args.a, cfg, probe=probe, profile=args.a_profile)
    if a_fn is None:
        print(f"[note] 候选 A 不可用（--a {args.a}）：端点不可达 / 模型未拉 / 缺 key。跳过。")
        return 0
    b_fn, b_detect, b_label, b_engine = build_candidate(
        args.b, cfg, probe=probe, profile=args.b_profile)
    if b_fn is None:
        print(f"[note] 候选 B 不可用（--b {args.b}）：端点不可达 / 模型未拉 / 缺 key。跳过。")
        return 0

    j_fn, j_detect, j_label, _je = build_candidate(args.judge, cfg, probe=probe)
    if j_fn is None:
        print(f"[note] 回译裁判不可用（--judge {args.judge}）。A/B 必须有恒定裁判，跳过。")
        return 0

    contaminated = j_label in (a_label, b_label)
    if contaminated and not args.allow_self_judge:
        print(
            f"[abort] 裁判「{j_label}」与候选同源——该候选会拿到复读自己措辞的不当加分，"
            "跑出来的数值不能用于选型。请换第三方裁判（如 --judge ai），"
            "或明确 --allow-self-judge 仅作调试。"
        )
        return 0

    # 源语用中立方（裁判）探测且只探一次，两侧共用 → 消除「同句被判成不同源语」的混淆。
    detect_fn = j_detect or a_detect
    embed_fn = _build_embed(cfg, args.semantic == "auto")
    dataset = args.dataset or DEFAULT_DATASET
    samples = pin_source_langs(load_translation_samples(dataset), detect_fn)

    common = dict(
        detect_fn=None,  # 源语已钉死在样本里
        per_sample_threshold=args.sample_threshold,
        back_translate_fn=j_fn,
        embed_fn=embed_fn,
        semantic_threshold=args.sem_threshold,
    )
    print(f"[run] A={a_label}  B={b_label}  裁判={j_label}  样本={len(samples)}  集={dataset}")
    print("[run] 跑候选 A …", flush=True)
    a_report = asyncio.run(evaluate_translation_quality(a_fn, samples, **common))
    print("[run] 跑候选 B …", flush=True)
    b_report = asyncio.run(evaluate_translation_quality(b_fn, samples, **common))

    comparison = compare(
        a_report, b_report,
        a_label=a_label, b_label=b_label, judge_label=j_label,
        metric=args.metric, tie_eps=args.tie_eps,
        min_effect=args.min_effect, min_pair_n=args.min_pair_n,
    )
    suggestion = suggest_per_lang_order(comparison, a_engine=a_engine, b_engine=b_engine)

    if args.out_jsonl:
        try:
            import datetime as _dt
            import os as _os
            line = {
                "ts": _dt.datetime.now().isoformat(timespec="seconds"),
                "dataset": dataset,
                **{k: v for k, v in comparison.items() if k != "by_pair"},
            }
            _os.makedirs(_os.path.dirname(args.out_jsonl) or ".", exist_ok=True)
            with open(args.out_jsonl, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
            print(f"[trend] 已追加 → {args.out_jsonl}")
        except Exception as ex:  # noqa: BLE001 - 落盘失败不该吃掉已经跑出来的结论
            print(f"[warn] 横比摘要写入失败: {ex}")

    if args.json:
        print(json.dumps({"comparison": comparison, "per_lang_order": suggestion},
                         ensure_ascii=False, indent=2))
    else:
        print()
        print(format_ab_report(comparison, suggestion))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
