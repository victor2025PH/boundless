# -*- coding: utf-8 -*-
"""媒体谎言旁路审计（实施69 P1-4，只读）——防线与观测同源共盲的解药。

背景：promise/claim 计数器长在守卫内部，检测词表漏什么、看板就瞎什么
（2026-08-24 夜市摊事故 ops 全绿）。本工具用**当前**词表离线重扫出站消息：
词表升级后历史漏网立即现形；每周跑一次即知「守卫是否又出现新盲区」。

口径（与 tests/test_media_pending.py 端到端重放同源语义）：
- 媒体语境窗＝该轮出站前 ``--lookback`` 条消息里，任一**入站**命中
  wants_media/detect_selfie_request，或任一**出站**命中承诺/offer；
- 语境闭合＝窗内其后已有媒体出站（media_type 非空，或镜像行「[图片]/[视频]/
  [语音]」前缀——A 线媒体镜像 media_type 常为空文本行）；
- 未闭合语境下出站命中 detect_media_claim(ctx=True)=「已发断言」、
  detect_media_promise=「将发承诺」；随后 ``--fulfill-window`` 秒内无媒体出站
  → 记一笔「无背书断言 / 未兑现承诺」。

用法：
    python tools/audit_media_claims.py                 # 全部活跃实例，近 30 天
    python tools/audit_media_claims.py --days 7 --json
    python tools/audit_media_claims.py --out-jsonl logs/eval/media_claims_trend.jsonl

周批注册（人工决定，与 TranslationEvalWeekly 同模式）：
    schtasks /Create /TN MediaClaimsAuditWeekly /SC WEEKLY /D SAT /ST 07:40 /F ^
      /TR "python D:\\boundless\\engines\\chengjie\\tools\\audit_media_claims.py --out-jsonl logs/eval/media_claims_trend.jsonl"
"""
from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

_ENGINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ENGINE_ROOT))
sys.path.insert(0, str(_ENGINE_ROOT / "scripts"))

_MEDIA_MIRROR_PREFIXES = ("[图片]", "[视频]", "[语音]", "[圖片]", "[視頻]", "[語音]")


def _is_media_out(row: Dict[str, Any]) -> bool:
    """出站行是否携带/镜像了真实媒体（media_type 或 A 线镜像前缀）。"""
    if str(row.get("direction") or "") != "out":
        return False
    if str(row.get("media_type") or "").strip():
        return True
    t = str(row.get("text") or "").lstrip()
    return any(t.startswith(p) for p in _MEDIA_MIRROR_PREFIXES)


def classify_conversation(msgs: List[Dict[str, Any]], *, lookback: int = 6,
                          fulfill_window_sec: float = 600.0) -> Dict[str, Any]:
    """单会话消息序列（按 ts 升序）→ 审计计数与样本（纯函数，可单测）。"""
    from src.ai.companion_selfie import detect_selfie_request
    from src.ai.outbound_promise_guard import (
        detect_media_claim, detect_media_offer, detect_media_promise,
        wants_media,
    )
    out = {"out_total": 0, "ctx_out": 0, "claims": 0, "promises": 0,
           "claims_unbacked": 0, "promises_unfulfilled": 0,
           "samples": []}  # type: Dict[str, Any]
    for i, m in enumerate(msgs):
        if str(m.get("direction") or "") != "out":
            continue
        text = str(m.get("text") or "").strip()
        if not text or _is_media_out(m):
            continue
        out["out_total"] += 1
        # 语境窗：最近 lookback 条里最后一次请求/承诺 vs 最后一次媒体出站
        req_ts, media_ts = 0.0, 0.0
        for j in range(max(0, i - lookback), i):
            r = msgs[j]
            rts = float(r.get("ts") or 0)
            rtxt = str(r.get("text") or "")
            if str(r.get("direction") or "") == "in":
                if wants_media(rtxt) or detect_selfie_request(rtxt):
                    req_ts = max(req_ts, rts)
            else:
                if detect_media_promise(rtxt) or detect_media_offer(rtxt):
                    req_ts = max(req_ts, rts)
                if _is_media_out(r):
                    media_ts = max(media_ts, rts)
        if req_ts <= 0 or media_ts >= req_ts:
            continue
        out["ctx_out"] += 1
        claim = detect_media_claim(text, media_context=True)
        promise = detect_media_promise(text)
        if not (claim or promise):
            continue
        # 兑现检查：本轮起 fulfill_window 内有无媒体出站
        backed = False
        mts = float(m.get("ts") or 0)
        for k in range(i, len(msgs)):
            if float(msgs[k].get("ts") or 0) - mts > fulfill_window_sec:
                break
            if _is_media_out(msgs[k]):
                backed = True
                break
        if claim:
            out["claims"] += 1
            if not backed:
                out["claims_unbacked"] += 1
                out["samples"].append(("claim", text[:80]))
        elif promise:
            out["promises"] += 1
            if not backed:
                out["promises_unfulfilled"] += 1
                out["samples"].append(("promise", text[:80]))
    return out


def audit_root(root: Path, *, days: int, lookback: int,
               fulfill_window_sec: float) -> Dict[str, Any]:
    db = Path(root) / "config" / "inbox.db"
    agg = {"root": str(root), "db": str(db), "conversations": 0,
           "out_total": 0, "ctx_out": 0, "claims": 0, "promises": 0,
           "claims_unbacked": 0, "promises_unfulfilled": 0, "samples": []}
    if not db.is_file():
        agg["error"] = "no_inbox_db"
        return agg
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "select conversation_id, direction, text, media_type, ts "
            "from messages where ts >= ? order by conversation_id, ts asc",
            (time.time() - days * 86400,)).fetchall()
    finally:
        con.close()
    convs: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        convs.setdefault(str(r["conversation_id"]), []).append(dict(r))
    for cid, msgs in convs.items():
        res = classify_conversation(
            msgs, lookback=lookback, fulfill_window_sec=fulfill_window_sec)
        if res["out_total"] <= 0:
            continue
        agg["conversations"] += 1
        for k in ("out_total", "ctx_out", "claims", "promises",
                  "claims_unbacked", "promises_unfulfilled"):
            agg[k] += res[k]
        for kind, sample in res["samples"][:4]:
            if len(agg["samples"]) < 20:
                agg["samples"].append(
                    {"conv": cid[-14:], "kind": kind, "text": sample})
    return agg


def main() -> int:
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="媒体谎言旁路审计（只读）")
    ap.add_argument("--data-root", default="", help="显式数据根（缺省自动发现活跃实例）")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--lookback", type=int, default=6, help="语境窗消息条数")
    ap.add_argument("--fulfill-window", type=float, default=600.0,
                    help="承诺/断言后多少秒内有媒体出站算兑现")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out-jsonl", default="", help="趋势行追加落盘（周批用）")
    args = ap.parse_args()

    from _data_root import resolve_data_roots
    roots = resolve_data_roots(args.data_root)
    reports = [audit_root(r, days=args.days, lookback=args.lookback,
                          fulfill_window_sec=args.fulfill_window)
               for r in roots]
    if args.out_jsonl:
        line = {"ts": time.time(), "days": args.days,
                "roots": [{k: v for k, v in rep.items() if k != "samples"}
                          for rep in reports]}
        p = Path(args.out_jsonl)
        if not p.is_absolute():
            p = _ENGINE_ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
        return 0
    for rep in reports:
        print(f"=== {rep['root']} (近 {args.days} 天) ===")
        if rep.get("error"):
            print(f"  跳过：{rep['error']}")
            continue
        print(f"  会话 {rep['conversations']} / 文本出站 {rep['out_total']} / "
              f"媒体语境轮 {rep['ctx_out']}")
        print(f"  已发断言 {rep['claims']}（无背书 {rep['claims_unbacked']}） / "
              f"将发承诺 {rep['promises']}（未兑现 {rep['promises_unfulfilled']}）")
        for s in rep["samples"]:
            print(f"    [{s['kind']:7}] ({s['conv']}) {s['text']}")
        if not rep["samples"]:
            print("    （本窗零漏网样本——守卫在岗）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
