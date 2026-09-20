"""I-4 灰度账本：地区语气档 / 自动链引用回复 的持久观测（JSONL）。

为什么要有这个文件
------------------
`persona_region.stats()` 与 `reply_quote_policy.stats_snapshot()` 都是**进程级计数**，
本机每天重启 ~8 次 → ops 卡上的数字永远只有几小时寿命，「灰度一周后按
`low_relevance` 占比调 `min_relevance`」「禁用词命中率高才上 L4 负样本改写」这两个
数据闸口根本攒不出样本。本模块把每次决策/观测**追加一行 JSONL**，重启不丢，
`tools/region_quote_gray_report.py` 只读汇总出可直接拍参数的读数。

落点：``logs/i4_gray/<kind>_YYYYMMDD.jsonl``（相对 CWD——生产进程 CWD 是实例数据根，
与 autosend_shadow_log 同口径）；测试/CLI 用 ``AITR_I4_GRAY_DIR`` 覆写；置为 ``off``
整体关闭。任何异常吞掉只记 DEBUG——观测绝不伤主链。

记录只含决策字段（分数/原因/地区/命中词），**不落用户原文**（引用候选只记长度）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

DEFAULT_DIR = "logs/i4_gray"
ENV_DIR = "AITR_I4_GRAY_DIR"
KINDS = ("quote", "region")

_LOCK = threading.Lock()


def gray_dir() -> Optional[Path]:
    v = os.environ.get(ENV_DIR)
    if v is not None and v.strip().lower() in ("off", "0", "false", "none"):
        return None
    return Path(v) if v else Path(DEFAULT_DIR)


def _day_key(ts: float) -> str:
    return time.strftime("%Y%m%d", time.localtime(ts))


def file_for(kind: str, ts: float, *, base: Optional[Path] = None) -> Optional[Path]:
    d = base if base is not None else gray_dir()
    if d is None:
        return None
    return d / f"{kind}_{_day_key(ts)}.jsonl"


def append(kind: str, rec: Dict[str, Any]) -> bool:
    """追加一行；成功 True。关闭/异常 → False（不抛）。"""
    if kind not in KINDS:
        return False
    try:
        ts = float(rec.get("ts") or time.time())
        rec = dict(rec)
        rec["ts"] = round(ts, 3)
        p = file_for(kind, ts)
        if p is None:
            return False
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        with _LOCK:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("[i4_gray] append %s failed: %s", kind, e)
        return False


def iter_records(kind: str, days: int = 30, *, base: Optional[Path] = None,
                 now: Optional[float] = None) -> Iterator[Dict[str, Any]]:
    """只读遍历近 N 天的记录（坏行跳过）。"""
    d = base if base is not None else gray_dir()
    if d is None or not d.exists():
        return
    now = float(now or time.time())
    wanted = {_day_key(now - i * 86400) for i in range(max(1, int(days)))}
    for p in sorted(d.glob(f"{kind}_*.jsonl")):
        day = p.stem[len(kind) + 1:]
        if day not in wanted:
            continue
        try:
            with open(p, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    if isinstance(rec, dict):
                        yield rec
        except OSError:
            continue


# ── 汇总（CLI 与测试共用的纯函数）───────────────────────────────────────────

def summarize_quote(records, *, cur_min_relevance: float,
                    target_quote_rate: float = 0.25) -> Dict[str, Any]:
    """引用决策汇总 + 阈值建议。

    * ``skipped`` 原因分布、引用率。
    * ``low_relevance`` 那批的最高分直方图（0.05 桶）——「把阈值降到 X 会多引用多少」。
    * ``suggest_min_relevance``：在「有候选的决策」里，让引用率落到 target 的分位阈值；
      只在 low_relevance ≥ 20 条时给建议（样本闸门），否则 None。
    """
    n = 0
    quoted = 0
    skipped: Dict[str, int] = {}
    low_scores = []
    applied = 0
    fallback = 0
    scored = []            # 所有「有候选」决策的 best score（含已引用）
    for r in records:
        if r.get("applied"):
            applied += 1
            continue
        if r.get("fallback_plain"):
            fallback += 1
            continue
        if "quote" not in r:
            continue
        n += 1
        if r.get("quote"):
            quoted += 1
            if r.get("score") is not None:
                scored.append(float(r["score"]))
        else:
            reason = str(r.get("reason") or "unknown")
            skipped[reason] = skipped.get(reason, 0) + 1
            if reason == "low_relevance" and r.get("score") is not None:
                s = float(r["score"])
                low_scores.append(s)
                scored.append(s)
    hist: Dict[str, int] = {}
    for s in low_scores:
        b = int(s * 20) / 20.0
        k = f"{b:.2f}"
        hist[k] = hist.get(k, 0) + 1
    suggest = None
    if len(low_scores) >= 20 and scored:
        srt = sorted(scored, reverse=True)
        idx = max(0, min(len(srt) - 1, int(len(srt) * target_quote_rate) - 1))
        cand = round(srt[idx], 2)
        # 只建议**下调**（上调=更保守，看板已能读；建议是为了「放量」决策），且别低于 0.1
        if cand < cur_min_relevance and cand >= 0.1:
            suggest = cand
    return {
        "decided": n,
        "quoted": quoted,
        "quote_rate": round(quoted / n, 4) if n else 0.0,
        "skipped": dict(sorted(skipped.items(), key=lambda kv: -kv[1])),
        "applied": applied,
        "fallback_plain": fallback,
        "low_relevance_hist": dict(sorted(hist.items())),
        "cur_min_relevance": cur_min_relevance,
        "suggest_min_relevance": suggest,
        "sample_gate_ok": len(low_scores) >= 20,
    }


def summarize_region(records, *, l4_hit_rate: float = 0.10,
                     l4_min_observed: int = 30) -> Dict[str, Any]:
    """地区档出站观测汇总 + L4 判词。

    按地区：observed / banned_hit / script_hit + 命中率 + Top 命中词。
    ``l4_verdict``：某档 observed ≥ 30 且 (banned+script) 命中率 ≥ 10% → 建议上 L4
    负样本改写；否则「观测即可」。
    """
    per: Dict[str, Dict[str, Any]] = {}
    for r in records:
        reg = str(r.get("region") or "?")
        d = per.setdefault(reg, {"observed": 0, "banned_hit": 0, "script_hit": 0,
                                 "top_banned": {}, "top_script": {}})
        d["observed"] += 1
        b = r.get("banned") or []
        s = r.get("script") or []
        if b:
            d["banned_hit"] += 1
            for w in b:
                d["top_banned"][w] = d["top_banned"].get(w, 0) + 1
        if s:
            d["script_hit"] += 1
            for w in s:
                d["top_script"][w] = d["top_script"].get(w, 0) + 1
    verdicts = []
    for reg, d in per.items():
        obs = d["observed"]
        hit_rate = round((d["banned_hit"] + d["script_hit"]) / obs, 4) if obs else 0.0
        d["hit_rate"] = hit_rate
        d["top_banned"] = dict(sorted(d["top_banned"].items(), key=lambda kv: -kv[1])[:8])
        d["top_script"] = dict(sorted(d["top_script"].items(), key=lambda kv: -kv[1])[:8])
        if obs >= l4_min_observed and hit_rate >= l4_hit_rate:
            d["l4_verdict"] = "recommend_l4_rewrite"
            verdicts.append(f"{reg}: 命中率 {hit_rate:.0%} (n={obs}) ≥ {l4_hit_rate:.0%} → 建议开 L4 负样本改写")
        elif obs < l4_min_observed:
            d["l4_verdict"] = "insufficient_sample"
        else:
            d["l4_verdict"] = "observe_only"
    return {"by_region": per, "verdicts": verdicts,
            "l4_hit_rate": l4_hit_rate, "l4_min_observed": l4_min_observed}


__all__ = [
    "DEFAULT_DIR", "ENV_DIR", "KINDS", "gray_dir", "file_for", "append",
    "iter_records", "summarize_quote", "summarize_region",
]
