# -*- coding: utf-8 -*-
r"""反编造守卫真实语料验证探针（只读，P1 2026-08-03）。

为什么必须有这个：`strip_fabricated_sentences` 已接进 A/B 两条回复链，是个**会
改写出站文本**的守卫，覆盖每天几百条真实回复。单测只能证「我设想的语料上对」，
证不了「生产语料上不误伤」。上线一个改文本的守卫却不在真实语料上量过，是赌博。

口径（刻意悲观 —— 得出的剥离率是**上界**）：
  依据池只用「该条出站之前的同会话消息」，而线上运行时依据池还含 episodic 记忆 /
  历史摘要 / 用户画像（build_reply_evidence 全收）。故探针判「会被剥」的条数一定
  ≥ 线上实际。上界都接近 0，实际必然更安全。

读什么：各实例 `<data_root>/config/inbox.db` 的 messages（`mode=ro` 只读连接，
对活体生产库零写事务）。不改任何数据、不发任何消息。

用法：
    python tools/verify_fabrication_guard.py                 # 近 30 天全实例
    python tools/verify_fabrication_guard.py --days 90 --samples 12
    python tools/verify_fabrication_guard.py --data-root D:\...\data
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts._data_root import resolve_data_roots  # noqa: E402
from src.inbox.peer_bot_guard import is_media_placeholder  # noqa: E402
from src.utils.proactive_fabrication_guard import (  # noqa: E402
    past_claim_markers_hit,
    strip_fabricated_sentences,
)

_HISTORY_WINDOW = 12   # 与 build_reply_evidence 默认 history_limit 一致


def scan_root(data_root: str, *, days: int, samples: int) -> Dict[str, Any]:
    """扫一个实例数据根，返回统计 + 抽样。"""
    db = os.path.join(str(data_root), "config", "inbox.db")
    stat: Dict[str, Any] = {
        "db": db, "out_total": 0, "claim_hit": 0, "would_strip": 0,
        "all_stripped": 0, "suspect": 0,
        "strip_samples": [], "suspect_samples": [],
    }
    if not os.path.isfile(db):
        stat["skip"] = "无 inbox.db"
        return stat
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    since = time.time() - max(1, int(days)) * 86400
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT conversation_id, direction, original_text, text, ts "
            "FROM messages WHERE ts>=? ORDER BY conversation_id, ts ASC",
            (since,))]
    except Exception as e:                                  # noqa: BLE001
        stat["skip"] = f"读 messages 失败: {e}"
        conn.close()
        return stat
    conn.close()

    # 按会话累积历史，逐条出站在「它之前的历史」上判定（还原线上时序）
    history: Dict[str, List[str]] = {}
    for r in rows:
        cid = str(r.get("conversation_id") or "")
        raw = str(r.get("original_text") or r.get("text") or "").strip()
        if not raw or is_media_placeholder(raw):
            continue
        prior = history.setdefault(cid, [])
        if str(r.get("direction") or "") == "out":
            stat["out_total"] += 1
            if past_claim_markers_hit(raw):
                stat["claim_hit"] += 1
                evidence = prior[-_HISTORY_WINDOW:]
                new_text, info = strip_fabricated_sentences(raw, evidence)
                if info.get("stripped"):
                    stat["would_strip"] += 1
                    if info.get("all_stripped"):
                        stat["all_stripped"] += 1
                    if len(stat["strip_samples"]) < samples:
                        stat["strip_samples"].append({
                            "cid": cid, "text": raw[:110],
                            "kept": new_text[:110],
                            "all": bool(info.get("all_stripped")),
                        })
                elif info.get("suspect"):
                    stat["suspect"] += 1
                    if len(stat["suspect_samples"]) < samples:
                        stat["suspect_samples"].append({
                            "cid": cid, "text": raw[:110],
                        })
        prior.append(raw)
    return stat


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default="")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--samples", type=int, default=8)
    args = ap.parse_args()

    roots = ([args.data_root] if args.data_root
             else [str(p) for p in resolve_data_roots()])
    print(f"=== 反编造守卫真实语料验证（只读，近 {args.days} 天）===")
    print(f"依据池口径：仅同会话历史前 {_HISTORY_WINDOW} 条 → 剥离率是上界\n")

    agg = {"out_total": 0, "claim_hit": 0, "would_strip": 0,
           "all_stripped": 0, "suspect": 0}
    for root in roots:
        st = scan_root(root, days=args.days, samples=args.samples)
        print(f"--- {root}")
        if st.get("skip"):
            print(f"    [skip] {st['skip']}")
            continue
        for k in agg:
            agg[k] += int(st.get(k) or 0)
        print(f"    出站 {st['out_total']} 条 | 含往事断言 {st['claim_hit']} "
              f"| 会被剥离 {st['would_strip']}（其中剥空回落 {st['all_stripped']}）"
              f"| 疑似(只观测) {st['suspect']}")
        for s in st["strip_samples"]:
            tag = "[剥空→回落原文]" if s["all"] else "[剥离]"
            print(f"      {tag} cid={s['cid']}")
            print(f"        原文: {s['text']}")
            if not s["all"]:
                print(f"        剩下: {s['kept']}")
        for s in st["suspect_samples"]:
            print(f"      [疑似·不动手] cid={s['cid']}  {s['text']}")

    total = max(1, agg["out_total"])
    print(f"\n=== 合计 ===")
    print(f"出站 {agg['out_total']} 条")
    print(f"含往事断言 {agg['claim_hit']} 条 "
          f"({agg['claim_hit'] / total * 100:.2f}%)")
    print(f"会被真剥离 {agg['would_strip']} 条 "
          f"({agg['would_strip'] / total * 100:.3f}%) —— 上界")
    print(f"  其中剥空回落原文 {agg['all_stripped']} 条（只计数不改文本）")
    print(f"疑似编造·只观测 {agg['suspect']} 条 "
          f"({agg['suspect'] / total * 100:.2f}%)")
    print("  ⚠ 疑似档在本探针里**天然测不出**：它判的是「往事在长期记忆里有没有"
          "对应」，而探针只读 messages、拿不到 episodic/摘要/画像（窄池为空即不判）。"
          "疑似的真实量级要看线上 fabrication_counters() 的 suspect 桶。")
    print("\n判读：剥离率应≈0（真实会话几乎总有历史依据）。若显著>0，逐条看抽样"
          "确认是真编造还是误伤，误伤则收紧 _PAST_CLAIM_MARKERS 或扩依据池。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
