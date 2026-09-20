# -*- coding: utf-8 -*-
"""情绪参考音自动化质检（2026-08-03）——部分替代人工抽听。

对 ``bootstrap_emotion_refs`` 的产物（tmp_emotion_refs/<pid>/<emotion>.wav）做
两道确定性检查（声纹分在生成时已把过第一道关，这里补它抓不到的两类事故）：

  1. **转写回验**（140:7854 Whisper，走既有 AvatarVoiceClient.stt 通道）：
     参考音念的必须就是台词本身——抓「合成翻车/截断/串字」（campplus 声纹分
     对这类事故不敏感：音色对了但话念错，一样是坏参考音）。归一化后
     difflib 相似度 ≥0.80 过、0.60~0.80 警、<0.60 拒（建议重生成）。
  2. **韵律自然度**（mfys prosody_scorer，CPU）：抓「播音腔」倾向。刻度未稳
     （2026-07 校准期结论），**只报告不判罚**——横向对比用。

用法：python tools/qc_emotion_refs.py [--personas all|a,b] [--previews] [--data-root PATH]
退出码：0=全过；2=存在转写回验不合格项（清单见输出，逐项重生成即可）。
"""
from __future__ import annotations

import argparse
import base64
import difflib
import io
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ENGINE))
_MFYS = Path(r"D:/faceX/mfys")

_NORM_RE = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]")

# Whisper 140 对部分中文样本会出繁体（陈默 sad 实锤：「有點/說話/心裡…」）。
# 无第三方依赖：覆盖聊天语体高频繁体字即可，不全量 OpenCC。
_TRAD_TO_SIMP = str.maketrans({
    "點": "点", "話": "话", "語": "语", "裡": "里", "裏": "里", "悶": "闷",
    "會": "会", "嗎": "吗", "麼": "么", "這": "这", "們": "们", "聽": "听",
    "說": "说", "對": "对", "開": "开", "還": "还", "來": "来", "過": "过",
    "時": "时", "間": "间", "覺": "觉", "東": "东", "西": "西", "個": "个",
    "為": "为", "沒": "没", "關": "关", "係": "系", "經": "经", "現": "现",
    "發": "发", "後": "后", "從": "从", "應": "应", "該": "该", "讓": "让",
    "給": "给", "當": "当", "樣": "样", "種": "种", "與": "与", "並": "并",
    "唄": "呗", "喲": "哟", "妳": "你",
})


def _norm(text: str) -> str:
    """去标点 + 小写 + 繁→简（抓 STT 繁体假阳性）。"""
    return _NORM_RE.sub("", str(text or "")).lower().translate(_TRAD_TO_SIMP)


def text_match_ratio(expected: str, transcript: str) -> float:
    """台词 vs 转写 的归一化相似度（0..1）。纯函数。

    允许转写漏句首叹词（「唉/嗯/诶」）——Whisper 常吞掉，内容主体对就算过。
    """
    a, b = _norm(expected), _norm(transcript)
    if not a or not b:
        return 0.0
    base = difflib.SequenceMatcher(None, a, b).ratio()
    # 句首语气词漏检：剥常见叹词再比，取较高分（Whisper 常吞「唉/嗯」）
    for lead in ("唉", "嗯", "诶", "欸", "哎", "啊"):
        if a.startswith(lead) and len(a) > len(lead) + 2:
            base = max(
                base,
                difflib.SequenceMatcher(None, a[len(lead):], b).ratio(),
            )
    return base


def _load_prosody():
    if not _MFYS.is_dir():
        return None
    sys.path.insert(0, str(_MFYS))
    try:
        from prosody_scorer import naturalness_score
        return naturalness_score
    except Exception:
        return None


def _nat_value(fn, synth_b64: str, ref_b64: str) -> Optional[float]:
    """prosody_scorer 返回形态防御式取分（float / dict 均兼容）。"""
    try:
        rv = fn(synth_b64, ref_b64)
        if isinstance(rv, (int, float)):
            return float(rv)
        if isinstance(rv, dict):
            for k in ("naturalness", "score", "value"):
                if isinstance(rv.get(k), (int, float)):
                    return float(rv[k])
    except Exception:
        pass
    return None


def main(argv: Optional[List[str]] = None) -> int:
    if sys.stdout and hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="情绪参考音质检（转写回验+韵律）")
    ap.add_argument("--personas", default="all")
    ap.add_argument("--previews", action="store_true",
                    help="连 hub 预览（preview_*.wav）一起验")
    ap.add_argument("--min-match", type=float, default=0.80)
    ap.add_argument("--data-root", default="")
    args = ap.parse_args(argv)

    from scripts._data_root import resolve_data_roots
    from src.ai.avatar_voice import AvatarVoiceClient, load_reference_b64

    root = resolve_data_roots(args.data_root)[0]
    base_dir = root / "tmp_emotion_refs"
    if not base_dir.is_dir():
        print(f"[!] 无产物目录 {base_dir}")
        return 1
    want = ([p.strip() for p in args.personas.split(",") if p.strip()]
            if args.personas != "all" else
            sorted(d.name for d in base_dir.iterdir() if d.is_dir()))
    client = AvatarVoiceClient({"enabled": True})
    prosody = _load_prosody()
    if prosody is None:
        print("[-] prosody_scorer 不可用，只做转写回验")

    flagged: List[str] = []
    warned: List[str] = []
    for pid in want:
        pdir = base_dir / pid
        mf = pdir / "manifest.json"
        if not mf.is_file():
            print(f"[skip] {pid}: 无 manifest")
            continue
        manifest = json.loads(mf.read_text(encoding="utf-8"))
        ref_b64 = ""
        if prosody is not None:
            try:
                ref_b64 = load_reference_b64(str(manifest.get("ref") or ""))
            except Exception:
                ref_b64 = ""
        print(f"\n--- {pid} ---")
        for emo, entry in (manifest.get("emotions") or {}).items():
            if not entry.get("accepted"):
                continue
            script = str(entry.get("script") or "")
            targets = [("ref", pdir / f"{emo}.wav", script)]
            if args.previews:
                # 预览念的是固定试听句（bootstrap_emotion_refs.PREVIEW_TEXT）
                from tools.bootstrap_emotion_refs import PREVIEW_TEXT
                targets.append(("preview", pdir / f"preview_{emo}.wav",
                                PREVIEW_TEXT))
            for kind, wav_path, expect in targets:
                if not wav_path.is_file():
                    continue
                wav = wav_path.read_bytes()
                transcript = client.stt(wav, language="") or ""
                ratio = text_match_ratio(expect, transcript)
                nat = None
                if prosody is not None and ref_b64:
                    nat = _nat_value(
                        prosody, base64.b64encode(wav).decode("ascii"), ref_b64)
                tag = "过"
                if ratio < 0.60:
                    tag = "拒 ⚠"
                    flagged.append(f"{pid}/{emo}/{kind}")
                elif ratio < args.min_match:
                    tag = "警"
                    warned.append(f"{pid}/{emo}/{kind}")
                nat_s = f"  韵律 {nat:.3f}" if nat is not None else ""
                print(f"  {emo:<8}{kind:<8} 回验 {ratio:.2f} [{tag}]{nat_s}"
                      f"  转写: {transcript[:26]}")
    print(f"\n汇总：拒 {len(flagged)} 项，警 {len(warned)} 项。")
    if flagged:
        print("  拒收清单（建议重生成后 --reuse 上传覆盖）：")
        for f in flagged:
            print(f"    - {f}")
    if warned:
        print("  警告清单（Whisper 对语气词转写常有出入，建议抽听确认）：")
        for w in warned:
            print(f"    - {w}")
    return 2 if flagged else 0


if __name__ == "__main__":
    raise SystemExit(main())
