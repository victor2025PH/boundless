"""ASR 转写质量评测轨（ASR P1，2026-09-12）——换模型 / 调参数之前先有一把尺子。

背景：09-12 在 198 上做解码参数 A/B 时，「放宽 VAD」「钉 min_speech=250」「送热词 prompt」
三项本以为的改进都被真实音频推翻——靠的是**同一批片段 × 多变体 × 看输出**。本模块把那次
手工比对固化成可重复的评测：金标样本（音频 + 逐字稿）→ 转写 → 字错率 CER，按语种 / 时长桶
聚合，给出 passed 与最差样本清单。TTS 那边早有 synth_verify + CER，ASR 一直没有。

金标来源（``load_asr_samples``，两路合并）：
- 清单 ``config/eval/asr_samples.jsonl``：每行 ``{"audio": 路径, "ref": 逐字稿, "lang": "zh",
  "tags": [...]}``；相对路径按清单所在目录解析；音频不存在的行跳过并计入 ``skipped``。
- 坐席改正 ``asr_corrections.jsonl``（``src/inbox/asr_corrections``，数据根 config/）：
  每条人工改正即一条金标（``source="correction"``）——评测集随着运营使用自然长大。

CER（``cer``）：归一化后字符级编辑距离 / 参考长度。归一化＝去空白与标点、小写、中文繁→简
（opencc 缺失恒等）——「今天天氣不錯」与「今天天气不错」不算错，标点/空格差异不算错。

判定：``passed`` = 样本数 ≥ ``min_samples``（默认 5）且平均 CER ≤ ``threshold``（默认 0.10）。
样本不足 → ``status="insufficient"``、``passed=None``（CLI 按 skip 退出 0）：一两条样本的
均值没有统计意义，评测轨在场但不裁决，等改正积攒。转写函数由调用方注入（生产链
``VoiceTranscriberFactory`` / 任意 OpenAI 兼容端点 / 假函数），本模块零网络零模型。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

DEFAULT_MANIFEST = "config/eval/asr_samples.jsonl"
DEFAULT_THRESHOLD = 0.10
DEFAULT_MIN_SAMPLES = 5

_PUNCT_RE = re.compile(r"[\s\u3000，,。.!！?？~～、:：;；\-—_\[\]【】（）()\"'“”‘’…·]+")


@dataclass
class ASRSample:
    audio: str
    ref: str
    lang: str = ""
    tags: List[str] = field(default_factory=list)
    source: str = "manifest"      # manifest | correction
    note: str = ""


def normalize_for_cer(text: str, *, zh_simplified: bool = True) -> str:
    """去空白/标点、小写、繁→简（可关）。纯函数不抛。"""
    t = _PUNCT_RE.sub("", str(text or "")).lower()
    if zh_simplified and t:
        try:
            from src.ai.lang_voice_route import to_simplified_for_tts
            t2 = to_simplified_for_tts(t)
            if t2:
                t = str(t2)
        except Exception:
            pass
    return t


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (0 if ca == cb else 1)))
        prev = cur
    return prev[-1]


def cer(ref: str, hyp: str, *, zh_simplified: bool = True) -> float:
    """字错率：归一化后 编辑距离 / len(ref)。ref 归一后为空 → hyp 也空为 0.0，否则 1.0。"""
    r = normalize_for_cer(ref, zh_simplified=zh_simplified)
    h = normalize_for_cer(hyp, zh_simplified=zh_simplified)
    if not r:
        return 0.0 if not h else 1.0
    return round(_levenshtein(r, h) / len(r), 4)


def duration_bucket(sec: Optional[float]) -> str:
    if not isinstance(sec, (int, float)) or sec <= 0:
        return "unknown"
    if sec < 2:
        return "<2s"
    if sec < 5:
        return "2-5s"
    if sec < 10:
        return "5-10s"
    if sec < 30:
        return "10-30s"
    return ">=30s"


def _audio_duration(path: str) -> Optional[float]:
    try:
        from src.ai.audio_pipeline_intake import probe_audio_file
        d = probe_audio_file(path, use_ffprobe=True).get("duration_sec")
        return float(d) if isinstance(d, (int, float)) and d > 0 else None
    except Exception:
        return None


def _resolve(path_s: str, base: Optional[Path]) -> str:
    p = Path(str(path_s or "").strip())
    if not str(p):
        return ""
    if not p.is_absolute() and base is not None:
        p = (base / p)
    try:
        return str(p.resolve())
    except Exception:
        return str(p)


def load_asr_samples(
    manifest_path: Optional[str] = None,
    *,
    corrections_path: Optional[str] = None,
    base_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """→ ``{"samples": [ASRSample], "skipped": [{"audio", "reason", "source"}]}``。

    清单相对路径按清单目录（或 ``base_dir``）解析；改正文件缺失/坏行静默跳过。
    """
    samples: List[ASRSample] = []
    skipped: List[Dict[str, str]] = []
    mp = Path(manifest_path) if manifest_path else None
    if mp is not None and mp.is_file():
        base = Path(base_dir) if base_dir else mp.parent
        with open(mp, "r", encoding="utf-8") as f:
            for ln, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    skipped.append({"audio": f"line {ln}", "reason": "bad_json", "source": "manifest"})
                    continue
                if not isinstance(d, dict):
                    continue
                audio = _resolve(str(d.get("audio") or ""), base)
                ref = str(d.get("ref") or d.get("text") or "").strip()
                if not audio or not ref:
                    skipped.append({"audio": audio or f"line {ln}", "reason": "missing_field", "source": "manifest"})
                    continue
                if not os.path.isfile(audio):
                    skipped.append({"audio": audio, "reason": "audio_missing", "source": "manifest"})
                    continue
                tags = d.get("tags") if isinstance(d.get("tags"), list) else []
                samples.append(ASRSample(audio=audio, ref=ref, lang=str(d.get("lang") or ""),
                                         tags=[str(t) for t in tags], source="manifest",
                                         note=str(d.get("note") or "")))
    if corrections_path:
        try:
            from src.inbox.asr_corrections import iter_corrections
            for rec in iter_corrections(corrections_path):
                audio = str(rec.get("audio_path") or "")
                ref = str(rec.get("corrected_text") or "").strip()
                if not audio or not ref:
                    continue
                if not os.path.isfile(audio):
                    skipped.append({"audio": audio, "reason": "audio_missing", "source": "correction"})
                    continue
                samples.append(ASRSample(audio=audio, ref=ref, lang=str(rec.get("lang") or ""),
                                         tags=["correction"], source="correction",
                                         note=str(rec.get("machine_text") or "")[:80]))
        except Exception:
            pass
    return {"samples": samples, "skipped": skipped}


def evaluate_asr(
    samples: Iterable[ASRSample],
    transcribe_fn: Callable[[str, str], Any],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    label: str = "",
) -> Dict[str, Any]:
    """逐样本转写 → CER；``transcribe_fn(audio_path, lang) -> str | (str, meta) | None``。

    返回 ``{available, status, passed, summary{n, mean_cer, median_cer, worst}, by_lang, by_bucket,
    rows, threshold, min_samples, label}``；转写函数抛异常 → 该样本 CER=1.0 并记 error。
    """
    rows: List[Dict[str, Any]] = []
    for s in samples:
        hyp, meta, err = "", {}, ""
        try:
            out = transcribe_fn(s.audio, s.lang or "auto")
            if isinstance(out, tuple):
                hyp = str(out[0] or "")
                meta = dict(out[1] or {}) if len(out) > 1 and isinstance(out[1], dict) else {}
            else:
                hyp = str(out or "")
        except Exception as ex:  # noqa: BLE001 - 单样本失败不拖垮整轮
            err = f"{type(ex).__name__}: {ex}"[:160]
        dur = meta.get("duration") if isinstance(meta.get("duration"), (int, float)) else _audio_duration(s.audio)
        c = cer(s.ref, hyp)
        rows.append({
            "audio": os.path.basename(s.audio), "lang": s.lang or (str(meta.get("language") or "") or "?"),
            "bucket": duration_bucket(dur), "duration": round(float(dur), 2) if dur else None,
            "ref": s.ref, "hyp": hyp, "cer": c, "source": s.source, "tags": list(s.tags),
            "error": err,
        })
    n = len(rows)
    if n == 0:
        return {"available": False, "status": "no_samples", "passed": None,
                "summary": {"n": 0, "mean_cer": None, "median_cer": None, "worst": []},
                "by_lang": {}, "by_bucket": {}, "rows": [], "threshold": threshold,
                "min_samples": min_samples, "label": label}
    cers = sorted(r["cer"] for r in rows)
    mean = round(sum(cers) / n, 4)
    median = cers[n // 2] if n % 2 else round((cers[n // 2 - 1] + cers[n // 2]) / 2, 4)

    def _agg(key: str) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            k = str(r.get(key) or "?")
            g = out.setdefault(k, {"n": 0, "_sum": 0.0})
            g["n"] += 1
            g["_sum"] += float(r["cer"])
        for g in out.values():
            g["mean_cer"] = round(g.pop("_sum") / g["n"], 4)
        return dict(sorted(out.items()))

    worst = sorted(rows, key=lambda r: r["cer"], reverse=True)[:5]
    if n < max(1, int(min_samples)):
        status, passed = "insufficient", None
    else:
        passed = mean <= float(threshold)
        status = "pass" if passed else "fail"
    return {
        "available": True, "status": status, "passed": passed,
        "summary": {"n": n, "mean_cer": mean, "median_cer": median,
                    "worst": [{"audio": w["audio"], "cer": w["cer"], "ref": w["ref"][:40],
                               "hyp": w["hyp"][:40]} for w in worst]},
        "by_lang": _agg("lang"), "by_bucket": _agg("bucket"), "rows": rows,
        "threshold": threshold, "min_samples": min_samples, "label": label,
    }


def format_asr_report(report: Dict[str, Any], skipped: Optional[List[Dict[str, str]]] = None) -> str:
    lines: List[str] = []
    lab = f" [{report.get('label')}]" if report.get("label") else ""
    if not report.get("available"):
        lines.append(f"ASR 评测{lab}：无可用样本（清单缺失/音频不在）——SKIP")
    else:
        s = report["summary"]
        st = report.get("status")
        verdict = {"pass": "PASS", "fail": "FAIL", "insufficient": "INSUFFICIENT（样本不足，不裁决）"}.get(st, st)
        lines.append(f"ASR 评测{lab}：{verdict}  n={s['n']}  mean CER={s['mean_cer']}  "
                     f"median={s['median_cer']}  阈值≤{report['threshold']}  最少样本={report['min_samples']}")
        if report.get("by_lang"):
            lines.append("  按语种：" + "  ".join(
                f"{k}: n={v['n']} cer={v['mean_cer']}" for k, v in report["by_lang"].items()))
        if report.get("by_bucket"):
            lines.append("  按时长：" + "  ".join(
                f"{k}: n={v['n']} cer={v['mean_cer']}" for k, v in report["by_bucket"].items()))
        for w in s.get("worst", []):
            lines.append(f"  最差 {w['audio']} cer={w['cer']}  ref={w['ref']!r}  hyp={w['hyp']!r}")
        for r in report.get("rows", []):
            if r.get("error"):
                lines.append(f"  错误 {r['audio']}: {r['error']}")
    for sk in (skipped or [])[:10]:
        lines.append(f"  跳过 {sk.get('audio')}: {sk.get('reason')} ({sk.get('source')})")
    return "\n".join(lines)


def build_transcribe_fn_from_config(cfg: Dict[str, Any]) -> Optional[Callable[[str, str], Any]]:
    """用运行配置的 ``voice_recognition`` 建生产同款转写链（含级联）→ ``fn(path, lang) -> (text, meta)``。

    未启用 / 构建失败 → None（CLI 按 skip）。评测刻意关转写缓存（同一音频要真转）。
    """
    vr = (cfg or {}).get("voice_recognition") if isinstance(cfg, dict) else None
    if not isinstance(vr, dict) or not vr.get("enabled"):
        return None
    try:
        import asyncio
        from src.voice_transcriber import VoiceTranscriberFactory
        cfg2 = dict(vr)
        cfg2["transcript_cache"] = {"enabled": False}
        t = VoiceTranscriberFactory.create_transcriber(cfg2)
    except Exception:
        return None

    def _fn(path: str, lang: str) -> Any:
        async def _run():
            text = await t.transcribe_voice_message(path, lang or "auto")
            return text or "", dict(getattr(t, "last_meta", {}) or {})
        return asyncio.run(_run())

    return _fn


__all__ = [
    "ASRSample", "DEFAULT_MANIFEST", "DEFAULT_MIN_SAMPLES", "DEFAULT_THRESHOLD",
    "build_transcribe_fn_from_config", "cer", "duration_bucket", "evaluate_asr",
    "format_asr_report", "load_asr_samples", "normalize_for_cer",
]
