# -*- coding: utf-8 -*-
"""dual_session_bench.py — 双路并发端到端联测（无人值守版，2026-07-05）

替代「第二台设备 10 分钟」的人工联测：用两个并发 SSE 客户端打 /api/converse/stream
(文本直入,同生产事件流),验证 P-Conc 准入 + lipsync 双副本(本机 8090 + .198)真实分担：

  A) 单路基线: 1 路会话,记 TTFA(首音)/整轮耗时/各句时延;
  B) 双路并发: 2 路同时开(K=2 应双双放行,无 queue 事件),对比每路 TTFA/整轮 vs 基线
     —— 若 .198 真分担,双路整轮 ≈ 基线(而非 2×);同时抓 /api/capacity 与池分布佐证;
  C) 三路超载: 3 路同开,第 3 路应收到 queue 事件(位次+ETA)而非静默劣化——准入契约生效。

用法: python tools/dual_session_bench.py [--profile 刘德华] [--rounds 2] [--lipsync]
产物: logs/dual_session_bench_<日期>.json
注: --lipsync 开逐句口型(重负载,真实直播形态);默认关(纯 TTS 链,也能验准入/分担)。
"""
import argparse
import base64
import concurrent.futures as cf
import json
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import requests

HUB = "http://127.0.0.1:9000"
QUESTIONS = [
    "给观众用两句话介绍一下今天的直播内容",
    "有观众问你平时怎么保持好状态，你怎么回答",
    "用一句话夸夸今天弹幕的热情",
    "给新进直播间的朋友打个招呼吧",
]


def one_session(tag: str, text: str, profile: str, lipsync: bool, timeout_s: float = 180.0):
    """跑一轮 SSE 会话，解析事件流，返回时延指标。"""
    t0 = time.time()
    out = {"tag": tag, "ttfa_ms": None, "total_ms": None, "sentences": 0,
           "tts_chunks": 0, "queued": False, "busy": False, "queue_pos": None,
           "audio_ms": 0.0, "err": ""}
    try:
        r = requests.post(f"{HUB}/api/converse/stream",
                          json={"text": text, "session_id": f"bench_{tag}_{int(t0)}",
                                "profile": profile, "speak": True,
                                "generate_lipsync": lipsync, "use_rag": False},
                          stream=True, timeout=timeout_s)
        if r.status_code != 200:
            out["err"] = f"HTTP {r.status_code}"
            return out
        for raw in r.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data:"):
                continue
            try:
                ev = json.loads(raw[5:].strip())
            except Exception:
                continue
            ph = ev.get("phase")
            if ph == "queue":
                out["queued"] = True
                out["queue_pos"] = ev.get("position")
            elif ph == "busy":
                out["busy"] = True
                break
            elif ph == "sentence":
                out["sentences"] += 1
            elif ph == "tts_chunk" and not ev.get("filler"):
                out["tts_chunks"] += 1
                if out["ttfa_ms"] is None:
                    out["ttfa_ms"] = round((time.time() - t0) * 1000)
                b64 = ev.get("audio_base64") or ""
                if b64:
                    raw_wav = base64.b64decode(b64)
                    # WAV 头 28..31 = byte-rate(bytes/s)；解析失败退回 16k/16bit 单声道
                    try:
                        rate = int.from_bytes(raw_wav[28:32], "little") or 32000
                    except Exception:
                        rate = 32000
                    out["audio_ms"] += max(0, len(raw_wav) - 44) * 1000.0 / rate
            elif ph == "error":
                out["err"] = str(ev.get("message"))[:120]
            elif ph == "done":
                break
        out["total_ms"] = round((time.time() - t0) * 1000)
        out["audio_ms"] = round(out["audio_ms"])
    except Exception as e:
        out["err"] = str(e)[:120]
        out["total_ms"] = round((time.time() - t0) * 1000)
    return out


def capacity() -> dict:
    try:
        return requests.get(f"{HUB}/api/capacity", timeout=5).json()
    except Exception:
        return {}


def pool_snapshot() -> dict:
    cap = capacity()
    pools = ((cap.get("gpu_pools") or {}).get("pools") or {})
    lip = pools.get("lipsync") or {}
    return {"k": cap.get("max"), "active": cap.get("active"),
            "lipsync_replicas": [{"url": x.get("url"), "served": x.get("served"),
                                  "inflight": x.get("inflight"), "down": x.get("down")}
                                 for x in (lip.get("replicas") or [])]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="")
    ap.add_argument("--rounds", type=int, default=2, help="A/B 各重复几轮取均值")
    ap.add_argument("--lipsync", action="store_true", help="开逐句口型(重负载,验双副本分担)")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    cap0 = capacity()
    print(f"准入: enabled={cap0.get('enabled')} K={cap0.get('max')} auto={cap0.get('auto')}"
          f" | lipsync={'开' if args.lipsync else '关'} profile={args.profile or '(激活角色)'}")
    if not cap0.get("enabled"):
        print("!! 准入未启用(CONV_MAX_CONCURRENT=0),C 阶段的排队断言不适用")

    res = {"ts": datetime.now().isoformat(timespec="seconds"),
           "capacity": cap0, "lipsync": args.lipsync,
           "single": [], "dual": [], "triple": [], "pool_before": pool_snapshot()}

    print(f"\n[A] 单路基线 ×{args.rounds}…")
    for i in range(args.rounds):
        r = one_session(f"solo{i}", QUESTIONS[i % len(QUESTIONS)], args.profile, args.lipsync)
        res["single"].append(r)
        print(f"  {r['tag']}: TTFA={r['ttfa_ms']}ms 整轮={r['total_ms']}ms "
              f"句数={r['sentences']} 音频≈{r['audio_ms']}ms {r['err']}")

    print(f"\n[B] 双路并发 ×{args.rounds}…")
    for i in range(args.rounds):
        with cf.ThreadPoolExecutor(2) as ex:
            fs = [ex.submit(one_session, f"dual{i}a", QUESTIONS[i % len(QUESTIONS)],
                            args.profile, args.lipsync),
                  ex.submit(one_session, f"dual{i}b", QUESTIONS[(i + 1) % len(QUESTIONS)],
                            args.profile, args.lipsync)]
            pair = [f.result() for f in fs]
        res["dual"].append(pair)
        for r in pair:
            print(f"  {r['tag']}: TTFA={r['ttfa_ms']}ms 整轮={r['total_ms']}ms "
                  f"排队={r['queued']} {r['err']}")

    print("\n[C] 三路超载(第3路应收 queue 事件)…")
    with cf.ThreadPoolExecutor(3) as ex:
        fs = [ex.submit(one_session, f"tri{c}", QUESTIONS[j % len(QUESTIONS)],
                        args.profile, args.lipsync) for j, c in enumerate("abc")]
        res["triple"] = [f.result() for f in fs]
    for r in res["triple"]:
        print(f"  {r['tag']}: TTFA={r['ttfa_ms']}ms 排队={r['queued']}(位次{r['queue_pos']}) "
              f"busy={r['busy']} {r['err']}")

    res["pool_after"] = pool_snapshot()

    # ── 结论 ──
    def _avg(xs):
        xs = [x for x in xs if isinstance(x, (int, float))]
        return round(sum(xs) / len(xs)) if xs else None
    solo_total = _avg([r["total_ms"] for r in res["single"]])
    solo_ttfa = _avg([r["ttfa_ms"] for r in res["single"]])
    dual_total = _avg([r["total_ms"] for p in res["dual"] for r in p])
    dual_ttfa = _avg([r["ttfa_ms"] for p in res["dual"] for r in p])
    dual_queued = any(r["queued"] for p in res["dual"] for r in p)
    tri_queued = sum(1 for r in res["triple"] if r["queued"] or r["busy"])
    served = {x["url"]: x["served"] for x in res["pool_after"]["lipsync_replicas"]}
    served0 = {x["url"]: x["served"] for x in res["pool_before"]["lipsync_replicas"]}
    delta = {u: served.get(u, 0) - served0.get(u, 0) for u in served}
    verdict = {
        "dual_admitted_no_queue": not dual_queued,
        "dual_total_vs_solo": (round(dual_total / solo_total, 2)
                               if solo_total and dual_total else None),
        "dual_ttfa_vs_solo": (round(dual_ttfa / solo_ttfa, 2)
                              if solo_ttfa and dual_ttfa else None),
        "third_route_queued": tri_queued >= 1,
        "lipsync_served_delta": delta,
    }
    verdict["pass"] = bool(verdict["dual_admitted_no_queue"]
                           and (verdict["dual_total_vs_solo"] or 9) < 1.6
                           and (verdict["third_route_queued"] or not cap0.get("enabled")))
    res["verdict"] = verdict

    day = datetime.now().strftime("%Y%m%d")
    p = BASE / "logs" / f"dual_session_bench_{day}.json"
    p.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结论: 双路放行={verdict['dual_admitted_no_queue']} "
          f"双路/单路整轮比={verdict['dual_total_vs_solo']} TTFA比={verdict['dual_ttfa_vs_solo']} "
          f"第3路排队={verdict['third_route_queued']} 口型分担Δ={delta} => "
          f"{'PASS' if verdict['pass'] else 'FAIL'}")
    print(f"已写 {p}")
    sys.exit(0 if verdict["pass"] else 1)


if __name__ == "__main__":
    main()
