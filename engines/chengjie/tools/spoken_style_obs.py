# -*- coding: utf-8 -*-
"""spoken_style 灰度复盘读数 CLI（只读；外语污染 + 中文出站口语形态前后对比）。

背景
----
2026-08-11 03:09 zhiliao 重启起 spoken_style L1-L3 灰度（`ai.spoken_style` L2 轮变尾注，
zh_only 只对中文主体消息注入）。交接约定的两天观察读数此前只能人工翻库：

  ① 外语污染——非中文会话的出站不应出现「嘛/呢/啦」式中文语气尾
     （zh_only 失守 / L1 指纹泄漏到外语对话的直接证据）；
  ② 中文会话出站形态——长度分布 + 语气词占比，以灰度分界（--cutoff）前后对比
     （L2 生效的可见效果：更口语、往往更短）。

本 CLI 把两个读数做成一条命令，复盘当天直接读。**只读**（sqlite ``mode=ro`` URI），
DB 不存在不创建；数据根按 ``scripts/_data_root`` 契约（CLI → AITR_DATA_ROOT →
自动发现活跃实例 → 引擎根），多实例逐根输出。

口径备注（与 bridge 判据对齐，见 src/ai/spoken_style_bridge.py）：
  · 会话语言取 conversations.language（zh* = 中文；unknown/空 = 不分类，两个读数都不进）；
  · 出站镜像占位（「[图片] …」「[语音]×2」等以 ``[`` 开头的文本）一律剔除；
  · 污染判定＝语气词命中（高置信）；另报 han 占比 ≥30% 的宽口径计数（含品牌名误报，
    只作参考量级，不逐条列样本）。

用法
----
    python tools/spoken_style_obs.py [--days 14] [--cutoff "2026-08-11 03:09"]
                                     [--samples 8] [--data-root PATH] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from scripts._data_root import resolve_data_roots  # noqa: E402

DEFAULT_CUTOFF = "2026-08-11 03:09"      # zhiliao spoken_style L1-L3 灰度装载重启
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")
# 中文口语语气词（L2 模板的典型痕迹）。「哦/噢/喔」只认句尾（句中常见于外语人名音译）。
_MODAL_RE = re.compile(r"[嘛呢啦咯嘞哟呗哒呀]|[哦噢喔](?=$|[!！?？。~～\s])")
_PLACEHOLDER_RE = re.compile(r"^\s*\[")   # 出站媒体镜像（[图片] / [语音]×N / [文件]…）


def _han_ratio(text: str) -> float:
    t = (text or "").strip()
    if not t:
        return 0.0
    return len(_HAN_RE.findall(t)) / len(t)


def _percentile(sorted_vals: Sequence[int], q: float) -> int:
    """最近秩法分位数（无 numpy 依赖；q∈[0,1]，空序列返 0）。"""
    if not sorted_vals:
        return 0
    idx = max(0, min(len(sorted_vals) - 1, int(round(q * (len(sorted_vals) - 1)))))
    return int(sorted_vals[idx])


def _tone_stats(lengths: List[int], particle_hits: int) -> Dict[str, Any]:
    ls = sorted(lengths)
    n = len(ls)
    return {
        "n": n,
        "mean_len": round(sum(ls) / n, 1) if n else 0.0,
        "p50_len": _percentile(ls, 0.50),
        "p90_len": _percentile(ls, 0.90),
        "particle_rate": round(particle_hits / n, 4) if n else 0.0,
    }


def classify_lang(language: str) -> str:
    """conversations.language → zh / foreign / unknown 三类。"""
    lang = (language or "").strip().lower()
    if not lang or lang == "unknown":
        return "unknown"
    return "zh" if lang.startswith("zh") else "foreign"


def build_report(rows: List[Dict[str, Any]], *, cutoff_ts: float,
                 samples_cap: int = 8) -> Dict[str, Any]:
    """把出站消息行聚合成复盘读数（纯函数：喂 rows 即可单测，无需 DB）。

    每行字段：text / ts / language / platform / display_name / chat_type。
    调用方保证 rows 已按时间窗过滤且 direction='out'。
    """
    pollution_hits: List[Dict[str, Any]] = []
    pollution = {"n_out_foreign": 0, "modal_hits_before": 0, "modal_hits_after": 0,
                 "han_wide_before": 0, "han_wide_after": 0}
    tone: Dict[str, Dict[str, Any]] = {}
    tone_acc = {"before": ([], 0), "after": ([], 0)}  # (lengths, particle_hits)
    unknown_out = 0

    for r in rows:
        text = str(r.get("text") or "").strip()
        if not text or _PLACEHOLDER_RE.match(text):
            continue
        bucket = "after" if float(r.get("ts") or 0) >= cutoff_ts else "before"
        cls = classify_lang(str(r.get("language") or ""))
        if cls == "unknown":
            unknown_out += 1
            continue
        if cls == "foreign":
            pollution["n_out_foreign"] += 1
            modal = bool(_MODAL_RE.search(text))
            if modal:
                pollution[f"modal_hits_{bucket}"] += 1
                if len(pollution_hits) < samples_cap:
                    pollution_hits.append({
                        "when": bucket,
                        "ts": float(r.get("ts") or 0),
                        "platform": str(r.get("platform") or ""),
                        "language": str(r.get("language") or ""),
                        "conv": str(r.get("display_name") or r.get("conversation_id") or ""),
                        "text": text[:80],
                    })
            elif _han_ratio(text) >= 0.30:
                pollution[f"han_wide_{bucket}"] += 1
        else:  # zh
            lengths, hits = tone_acc[bucket]
            lengths.append(len(text))
            if _MODAL_RE.search(text):
                hits += 1
            tone_acc[bucket] = (lengths, hits)

    for bucket in ("before", "after"):
        lengths, hits = tone_acc[bucket]
        tone[bucket] = _tone_stats(lengths, hits)

    return {
        "pollution": pollution,
        "pollution_samples": pollution_hits,
        "tone_zh": tone,
        "unknown_out_skipped": unknown_out,
    }


def _load_rows(db: Path, *, since_ts: float) -> List[Dict[str, Any]]:
    """只读拉取窗口内出站消息（conversations JOIN 取语言/平台；绝不写库）。"""
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT m.text, m.ts, m.conversation_id,
                      c.language, c.platform, c.display_name, c.chat_type
                 FROM messages m JOIN conversations c
                   ON c.conversation_id = m.conversation_id
                WHERE m.direction = 'out' AND m.ts >= ? AND m.text != ''
                ORDER BY m.ts""",
            (since_ts,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _parse_cutoff(val: str) -> float:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(val.strip(), fmt))
        except ValueError:
            continue
    raise SystemExit(f"--cutoff 无法解析: {val!r}（期望 YYYY-MM-DD [HH:MM[:SS]] 本地时间）")


def _fmt_ts(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def render_text(report: Dict[str, Any], root: Path, *, days: int, cutoff_ts: float) -> str:
    p = report["pollution"]
    t = report["tone_zh"]
    lines = [f"== spoken_style 灰度复盘 · {root} ==",
             f"   窗口 {days} 天 · 灰度分界 {_fmt_ts(cutoff_ts)}（分界前=基线，后=灰度）"]

    after_hits = p["modal_hits_after"]
    verdict = ("zh_only 在岗（灰度后外语出站零语气词命中）" if after_hits == 0
               else f"⚠ 灰度后外语出站语气词命中 {after_hits} 条——逐条核查下方样本")
    lines.append("")
    lines.append(f"   [读数①·外语污染] 外语会话出站 {p['n_out_foreign']} 条 · "
                 f"语气词命中 前 {p['modal_hits_before']} / 后 {after_hits} · "
                 f"宽口径(han≥30%) 前 {p['han_wide_before']} / 后 {p['han_wide_after']}")
    lines.append(f"     判词  {verdict}")
    for s in report["pollution_samples"]:
        lines.append(f"     [{s['when']}] {_fmt_ts(s['ts'])} {s['platform']}/{s['language']} "
                     f"{s['conv']}: {s['text']}")

    lines.append("")
    b, a = t["before"], t["after"]
    lines.append(f"   [读数②·中文出站形态] 基线 n={b['n']} · 灰度 n={a['n']}")
    lines.append(f"     长度 mean/p50/p90   {b['mean_len']}/{b['p50_len']}/{b['p90_len']}"
                 f"  →  {a['mean_len']}/{a['p50_len']}/{a['p90_len']}")
    lines.append(f"     语气词占比          {b['particle_rate']:.1%}  →  {a['particle_rate']:.1%}")
    if report["unknown_out_skipped"]:
        lines.append(f"   （另有 {report['unknown_out_skipped']} 条出站属 language=unknown 会话，"
                     "两个读数均未计入）")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="spoken_style 灰度复盘读数（只读）")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--cutoff", default=DEFAULT_CUTOFF)
    ap.add_argument("--samples", type=int, default=8)
    ap.add_argument("--data-root", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:  # Windows 控制台默认 GBK，中文读数会花——软性切 UTF-8，失败不阻断
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

    cutoff_ts = _parse_cutoff(args.cutoff)
    since_ts = time.time() - args.days * 86400
    out_json: List[Dict[str, Any]] = []
    text_blocks: List[str] = []
    for root in resolve_data_roots(args.data_root):
        db = Path(root) / "config" / "inbox.db"
        if not db.is_file():
            text_blocks.append(f"== {root} ==\n   未见 inbox.db（该根不是运行实例数据根）")
            out_json.append({"root": str(root), "db": False})
            continue
        rep = build_report(_load_rows(db, since_ts=since_ts),
                           cutoff_ts=cutoff_ts, samples_cap=args.samples)
        rep["root"] = str(root)
        out_json.append(rep)
        text_blocks.append(render_text(rep, root, days=args.days, cutoff_ts=cutoff_ts))

    if args.json:
        print(json.dumps(out_json, ensure_ascii=False, indent=2))
    else:
        print("\n\n".join(text_blocks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
