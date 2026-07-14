# -*- coding: utf-8 -*-
"""盲听 A/B 样本包 — 活人感改造的最终人耳验收材料。

同一批句子各合成两版（顺序随机化，防先入为主）：
  - 旧链：改造前行为 = 无口语化 + 无副语言标记 + 固定 CFM 噪声（服务端 off 路径）
  - 新链：生产现状 = 口语化(llm/规则) + 副语言标记 + 韵律方差(flow_temp) + 裁剪后参考音

产物：tmp_blind_ab/<ts>/pairNN_{a,b}.wav + manifest.md（先听）+ answers.txt（后对）。
两链都绕过预渲染/TTS 缓存（必须真合成）；GPU 串行锁内跑，约 2~4 分钟。

用法：python -m scripts.blind_ab_samples [--persona lin_xiaoyu] [--seed 42]
"""
from __future__ import annotations

import argparse
import asyncio
import random
import shutil
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:
    pass

# 覆盖不同情境的固定句集（改动会破坏跨批可比性，勿轻改）
SENTENCES = [
    ("greeting", "在呀在呀，刚忙完就看到你消息了，今天过得怎么样？"),
    ("empathy", "听起来你今天真的挺累的，工作再重要也要记得照顾好自己呀。"),
    ("playful", "哈哈你也太逗了吧，我笑得停不下来了，快再给我讲一个！"),
    ("daily", "我下午去了趟超市，买了点水果和酸奶，顺便看了看新出的零食。"),
    ("inform", "明天下午三点之前我都有空，如果你想聊的话随时找我就行。"),
    ("night", "不早啦，早点休息吧，晚安，做个好梦，明天见。"),
]


def _mk_pipelines(persona: str):
    from scripts.voice_similarity_probe import _load_config
    from src.ai.persona_voice import resolve_voice_cfg
    from src.ai.tts_pipeline import TTSPipeline

    cfg = _load_config()
    base = resolve_voice_cfg(persona, cfg)
    if not base:
        raise RuntimeError(f"resolve_voice_cfg 为空（persona={persona}）")

    def _variant(new: bool) -> TTSPipeline:
        vc = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
        vc["enabled"] = True
        vc["tts_cache"] = {"enabled": False}
        av = dict(vc.get("avatar_voice") or {})
        av["prerender"] = {"enabled": False}          # 必须真合成
        if new:
            # 生产现状（colloquial/paralinguistic/prosody 按 overlay）
            pass
        else:
            av["colloquial"] = {"enabled": False}
            av["paralinguistic"] = {"enabled": False}
            av["prosody"] = {"enabled": False}        # 服务端 off=原始固定噪声
        vc["avatar_voice"] = av
        return TTSPipeline(vc)

    return _variant(False), _variant(True)


async def _run(persona: str, seed: int) -> int:
    old_tts, new_tts = _mk_pipelines(persona)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_dir = _ROOT / "tmp_blind_ab" / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    manifest = ["# 盲听 A/B 样本包", "",
                f"- persona: {persona} · 生成时间: {ts}",
                "- 每对 a/b 为同句两条链（顺序随机）。先全部听完做记录，再看 answers.txt。",
                "- 记录建议：每对写下「哪条更像真人（a/b/差不多）」。", ""]
    answers = [f"seed={seed}", ""]
    ok_pairs = 0
    for i, (tag, sent) in enumerate(SENTENCES, 1):
        print(f"[{i}/{len(SENTENCES)}] {tag}: {sent[:18]}…")
        r_old = await old_tts.synthesize(sent, timeout_sec=120.0)
        r_new = await new_tts.synthesize(sent, timeout_sec=120.0)
        if not (r_old.ok and r_old.audio_path and r_new.ok and r_new.audio_path):
            print(f"  ✗ 合成失败 old={r_old.error} new={r_new.error}，跳过本对")
            continue
        new_is_a = rng.random() < 0.5
        pa = out_dir / f"pair{i:02d}_a{Path(r_new.audio_path).suffix}"
        pb = out_dir / f"pair{i:02d}_b{Path(r_old.audio_path).suffix}"
        if new_is_a:
            shutil.move(r_new.audio_path, pa)
            shutil.move(r_old.audio_path, pb)
        else:
            pa = out_dir / f"pair{i:02d}_a{Path(r_old.audio_path).suffix}"
            pb = out_dir / f"pair{i:02d}_b{Path(r_new.audio_path).suffix}"
            shutil.move(r_old.audio_path, pa)
            shutil.move(r_new.audio_path, pb)
        manifest.append(f"- pair{i:02d}（{tag}）：{sent}")
        answers.append(
            f"pair{i:02d}: 新链={'a' if new_is_a else 'b'}"
            f"（新 provider={r_new.provider}"
            f"{' 口语化' if r_new.extra.get('colloquial') or r_new.extra.get('colloquial_llm') else ''}"
            f"{' 副语言' if r_new.extra.get('paralinguistic') else ''}）")
        ok_pairs += 1

    (out_dir / "manifest.md").write_text("\n".join(manifest), encoding="utf-8")
    (out_dir / "answers.txt").write_text("\n".join(answers), encoding="utf-8")
    print(f"\n[*] 完成 {ok_pairs}/{len(SENTENCES)} 对 → {out_dir}")
    print("    先听 pairNN_a/b 并记录，再开 answers.txt 对答案")
    return 0 if ok_pairs else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="盲听 A/B 样本包生成")
    ap.add_argument("--persona", default="lin_xiaoyu")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    return asyncio.run(_run(args.persona, args.seed))


if __name__ == "__main__":
    raise SystemExit(main())
