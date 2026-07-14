#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""观众线并发压测 / 演练 / 容量校准 (P-Conc13)。

两种模式：

  sim  ── 进程内离散事件仿真（用【真实】 audience.AudienceQueue + 虚拟时钟驱动），
          不需 GPU / 网络，秒级跑完“一小时直播”。可校准 AUDIENCE_HOT_HALFLIFE /
          AUDIENCE_AUTO_POLL / AUDIENCE_AUTO_COOLDOWN / 并行 worker 数，量化积压与
          等待延迟，验证“人气优先”排序是否真的让高赞问题先被答、是否出现饿死，以及
          多卡池（最少在途）在该负载下的均衡度。  ← 默认、可在任何机器跑

  http ── 对【在线 Hub】黑盒打压（真实端点 /api/audience/*、/api/capacity、
          /api/ops/snapshot）。注意：单机同 IP 会触发每-IP 限流，压测前请
          `set AUDIENCE_RATE_SEC=0` 重启 Hub，否则全被 429 节流。

用法示例：
  python audience_loadtest.py sim --duration 1800 --qps 1.2 --workers 1
  python audience_loadtest.py sim --sweep 1,2,3,4 --qps 1.5
  python audience_loadtest.py sim --compare-service 8,4 --qps 0.1   # 量化“简答”收益(吞吐/等待)
  python audience_loadtest.py sim --selftest
  python audience_loadtest.py http --target http://127.0.0.1:8890 --duration 120 --qps 2 --enable-auto
  # 流式 TTFA 量化（两次跑、跨重启对照；A/B 即“关流式 vs 开流式”）：
  #   1) 关流式重启 Hub → http ... --save-baseline base_off.json
  #   2) 开流式重启 Hub → http ... --compare-baseline base_off.json
"""
import os
import sys
import math
import json
import time
import heapq
import random
import argparse
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:                       # Windows GBK 控制台也能打印 ✓/✅/⚠ 等（否则 UnicodeEncodeError）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ── 小工具：百分位 / 统计 ────────────────────────────────────────────
def pctl(xs, p):
    if not xs:
        return 0.0
    s = sorted(xs)
    k = (len(s) - 1) * (p / 100.0)
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return float(s[lo])
    return float(s[lo] + (s[hi] - s[lo]) * (k - lo))


def cv(xs):
    """变异系数 std/mean —— 多卡均衡度，越小越均衡（0=完美均衡）。"""
    if not xs:
        return 0.0
    m = sum(xs) / len(xs)
    if m == 0:
        return 0.0
    var = sum((x - m) ** 2 for x in xs) / len(xs)
    return math.sqrt(var) / m


# ── 虚拟时钟：把 audience 模块内部所有 time.time() 接到仿真时钟 ─────────
class _VClock:
    __slots__ = ("now",)

    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now


# ── 仿真用最少在途多卡池（镜像 avatar_hub._SvcPool.pick/done 的均衡算法）──
# 与生产同算法：选 inflight 最小的副本，并列则轮转打散；done 计 served。
class SimPool:
    def __init__(self, n):
        self.inflight = [0] * n
        self.served = [0] * n
        self._rr = 0

    def pick(self):
        m = min(self.inflight)
        cands = [i for i, v in enumerate(self.inflight) if v == m]
        self._rr += 1
        i = cands[self._rr % len(cands)]
        self.inflight[i] += 1
        return i

    def done(self, i):
        self.inflight[i] = max(0, self.inflight[i] - 1)
        self.served[i] += 1


# ── 离散事件仿真核心 ────────────────────────────────────────────────
# 事件类型常量
_ARR, _LIKE, _WAKE, _DONE, _SAMPLE, _STOPINTAKE = range(6)


def run_sim(cfg):
    """跑一场仿真。返回报告 dict。所有“时间”单位为秒（仿真时间）。"""
    import audience as _aud
    vt = _VClock()
    _aud.time = vt   # 关键：真实队列内部 time.time()→虚拟时钟，热度衰减/速率窗全部按仿真时间

    rng = random.Random(cfg["seed"])
    q = _aud.AudienceQueue(max_q=cfg["max_q"], per_ip_sec=0.0, max_len=cfg["max_q"] * 3,
                           dedup_sec=0.0, hot_halflife=cfg["halflife"],
                           drop_policy=cfg.get("drop_policy", "reject"))
    pool = SimPool(cfg["cards"])

    # 事件堆：(t, seq, kind, data)
    heap = []
    seq = [0]

    def push(t, kind, data=None):
        seq[0] += 1
        heapq.heappush(heap, (t, seq[0], kind, data))

    # 问题元数据：qid -> {submit_t, appeal, answered_t, answer_start, likes_at_answer}
    meta = {}
    # 每个问题分配“内在吸引力”：少数热门(hot_frac)高吸引、其余低吸引 → 模拟真实点赞长尾
    def draw_appeal():
        if rng.random() < cfg["hot_frac"]:
            return cfg["hot_appeal"] * (0.5 + rng.random())
        return cfg["base_appeal"] * rng.random()

    # 到达过程：总速率 lam（问/秒），指数间隔
    def schedule_next_arrival(t):
        if t >= cfg["horizon"]:
            return
        gap = rng.expovariate(cfg["lam"]) if cfg["lam"] > 0 else 1e9
        push(t + gap, _ARR)

    backlog_samples = []   # (t, pending)
    worker_busy_time = [0.0]   # 累计 worker 忙时（估算利用率）

    # ── 事件处理 ──
    def on_arrival(t):
        appeal = draw_appeal()
        text = f"问题#{seq[0]}_{rng.randint(0, 1<<30)}"   # 唯一，避免去重干扰
        r = q.submit(text, name=f"v{rng.randint(0, cfg['viewers']-1)}", ip="")  # ip 空→不限流
        if r.get("ok"):
            qid = r["id"]
            meta[qid] = {"submit_t": t, "appeal": appeal, "answer_start": None,
                         "answered_t": None, "likes_at_answer": 0}
            # 该问题的首个点赞事件（仅在 pending 期间持续）
            rate = max(1e-6, appeal * cfg["like_rate"])
            push(t + rng.expovariate(rate), _LIKE, qid)
        schedule_next_arrival(t)

    def on_like(t, qid):
        m = meta.get(qid)
        if m is None:
            return
        res = q.like(qid, ip=f"liker{seq[0]}")   # 唯一 liker → 每次都计一票
        if res is None:   # 已被答/不存在 → 停止该问题的点赞流
            return
        rate = max(1e-6, m["appeal"] * cfg["like_rate"])
        push(t + rng.expovariate(rate), _LIKE, qid)

    def on_wake(t, w):
        # worker w 尝试取一题作答（单飞由“每个 worker 同一时刻仅一个在飞”保证）
        nxt = q.next_pending()
        if not nxt:
            push(t + cfg["poll"], _WAKE, w)   # 空转：poll 后再看
            return
        ans = q.answer(nxt["id"])             # 先标记已答（与真实 worker 一致，杜绝重复/毒题）
        if not ans:
            push(t + 0.001, _WAKE, w)
            return
        qid = ans["id"]
        m = meta.get(qid)
        if m is not None:
            m["answer_start"] = t
            m["likes_at_answer"] = ans.get("likes", 0)
        rep = pool.pick()
        svc = max(0.05, rng.lognormvariate(math.log(max(0.1, cfg["service"])), cfg["service_cv"]))
        worker_busy_time[0] += svc
        push(t + svc, _DONE, (w, qid, rep))

    def on_done(t, data):
        w, qid, rep = data
        pool.done(rep)
        m = meta.get(qid)
        if m is not None:
            m["answered_t"] = t
        # 冷却后再取下一题（与真实 worker 的 COOLDOWN 一致）
        push(t + cfg["cooldown"], _WAKE, w)

    def on_sample(t):
        backlog_samples.append((t, q.pending_count()))
        if t + cfg["sample"] <= cfg["horizon"]:
            push(t + cfg["sample"], _SAMPLE)

    # ── 初始化事件 ──
    schedule_next_arrival(0.0)
    for w in range(cfg["workers"]):
        push(w * 0.01, _WAKE, w)   # 错开 worker 起始，避免完全同相
    push(0.0, _SAMPLE)

    # ── 主循环 ──
    last_t = 0.0
    while heap:
        t, _, kind, data = heapq.heappop(heap)
        if t > cfg["horizon"]:
            # 只让“已经在飞”的 DONE 收尾以归还 worker，其余截断
            if kind == _DONE:
                pass
            else:
                continue
        vt.now = t
        last_t = t
        if kind == _ARR:
            on_arrival(t)
        elif kind == _LIKE:
            on_like(t, data)
        elif kind == _WAKE:
            on_wake(t, data)
        elif kind == _DONE:
            on_done(t, data)
        elif kind == _SAMPLE:
            on_sample(t)

    # ── 汇总 ──
    vt.now = cfg["horizon"]
    snap = q.snapshot()
    stats = snap["stats"]
    answered = [m for m in meta.values() if m["answered_t"] is not None]
    started = [m for m in meta.values() if m["answer_start"] is not None]
    waits = [m["answer_start"] - m["submit_t"] for m in started]
    # 结束时仍未答（积压）的问题
    pending_meta = [m for m in meta.values() if m["answer_start"] is None]
    horizon_min = cfg["horizon"] / 60.0

    # 人气有效性：答过的平均赞 vs 仍积压的平均赞（前者应明显更高=高赞优先生效）
    ans_likes = [m["likes_at_answer"] for m in answered]
    pend_likes_now = []
    # 估算积压问题“当前赞”：用 appeal*like_rate*等待时长近似（无法回放，用代理）
    # 更直接：直接读队列里 pending 的真实赞
    try:
        pend_qs = q.list(status="pending", limit=10**6, order="hot")
        pend_likes_now = [x["likes"] for x in pend_qs]
    except Exception:
        pass

    util = worker_busy_time[0] / max(1e-6, cfg["workers"] * cfg["horizon"])
    answer_rate_min = len(answered) / max(1e-6, horizon_min)
    # 单 worker 理论上限（问/分）：1 / (服务 + 冷却)（poll 在繁忙时基本不计）
    ceil_one = 60.0 / max(1e-6, cfg["service"] + cfg["cooldown"])
    ceil_total = ceil_one * cfg["workers"]
    arrival_rate_min = cfg["lam"] * 60.0

    bl = [p for _, p in backlog_samples]
    report = {
        "config": cfg,
        "intake": {
            "arrivals_submitted_ok": stats["submitted"],
            "rejected_full": stats["rejected_full"],
            "evicted_stale": stats.get("evicted", 0),
            "rejected_rate": stats["rejected_rate"],
            "rejected_dup": stats["rejected_dup"],
            "arrival_rate_per_min": round(arrival_rate_min, 2),
            "drop_policy": cfg.get("drop_policy", "reject"),
        },
        "throughput": {
            "answered": len(answered),
            "answered_per_min": round(answer_rate_min, 2),
            "ceiling_per_min_one_worker": round(ceil_one, 2),
            "ceiling_per_min_total": round(ceil_total, 2),
            "worker_utilization": round(util, 3),
            "saturated": arrival_rate_min > ceil_total,
        },
        "latency_wait_s": {
            "p50": round(pctl(waits, 50), 1),
            "p95": round(pctl(waits, 95), 1),
            "max": round(max(waits), 1) if waits else 0.0,
            "mean": round(sum(waits) / len(waits), 1) if waits else 0.0,
        },
        "backlog": {
            "max": max(bl) if bl else 0,
            "mean": round(sum(bl) / len(bl), 1) if bl else 0,
            "final": bl[-1] if bl else 0,
            "queue_full_hits": stats["rejected_full"],
        },
        "popularity": {
            "avg_likes_answered": round(sum(ans_likes) / len(ans_likes), 2) if ans_likes else 0,
            "avg_likes_still_backlogged": round(sum(pend_likes_now) / len(pend_likes_now), 2) if pend_likes_now else 0,
            "max_likes_still_backlogged": max(pend_likes_now) if pend_likes_now else 0,
        },
        "pool_balance": {
            "cards": cfg["cards"],
            "served_per_card": pool.served,
            "cv": round(cv(pool.served), 3),
        },
    }
    report["advice"] = _advise(report)
    return report


def _advise(r):
    """据仿真结果给出可操作的校准建议。"""
    a = []
    cfg = r["config"]
    tp = r["throughput"]
    lat = r["latency_wait_s"]
    bl = r["backlog"]
    pop = r["popularity"]

    if tp["saturated"]:
        need = math.ceil(r["intake"]["arrival_rate_per_min"] / max(1e-6, tp["ceiling_per_min_one_worker"]))
        a.append(f"❗饱和：到达 {r['intake']['arrival_rate_per_min']}/分 > 应答上限 "
                 f"{tp['ceiling_per_min_total']}/分（W={cfg['workers']}）。积压将持续增长。"
                 f" 要追上需 ≈{need} 个并行 worker（AUDIENCE_AUTO_WORKERS），"
                 f"或降低 COOLDOWN/POLL，或缩短单轮服务时长。")
        a.append("注意：当前自动应答是【单飞】(W=1)，加显卡也提不了观众线吞吐——"
                 "并行 worker 才能让多卡真正分担观众问答。")
    else:
        a.append(f"✅ 未饱和：应答上限 {tp['ceiling_per_min_total']}/分 ≥ 到达 "
                 f"{r['intake']['arrival_rate_per_min']}/分，worker 利用率 {tp['worker_utilization']}。")

    # 人气优先有效性
    if pop["avg_likes_answered"] >= pop["avg_likes_still_backlogged"]:
        a.append(f"✅ 人气优先生效：已答均赞 {pop['avg_likes_answered']} ≥ 积压均赞 "
                 f"{pop['avg_likes_still_backlogged']}（高赞问题被优先答）。")
    else:
        a.append(f"⚠ 人气优先未生效：已答均赞 {pop['avg_likes_answered']} < 积压均赞 "
                 f"{pop['avg_likes_still_backlogged']}——可能 HOT_HALFLIFE 太短导致高赞老题被衰减掉。")

    # halflife 校准：建议 ≈ 中位等待，让高赞问题在其等待期内保持优先
    mw = lat["p50"]
    hl = cfg["halflife"]
    if hl <= 0:
        a.append("HOT_HALFLIFE=0（纯赞数，无衰减）。积压大时老题永久霸榜、新热题难冒头；"
                 f"建议设为 ≈中位等待 {mw:.0f}s 起步。")
    elif mw > hl * 2 and mw > 0:
        a.append(f"⚠ HOT_HALFLIFE={hl:.0f}s 远小于中位等待 {mw:.0f}s：高赞问题没轮到就被衰减成冷门。"
                 f" 建议提到 ≈{mw:.0f}s（与等待同量级）。")
    elif mw > 0:
        a.append(f"HOT_HALFLIFE={hl:.0f}s 与中位等待 {mw:.0f}s 量级匹配，较合理。")

    # 多卡均衡
    if cfg["cards"] > 1:
        if r["pool_balance"]["cv"] <= 0.12:
            a.append(f"✅ 多卡均衡良好（served CV={r['pool_balance']['cv']}）。")
        else:
            a.append(f"⚠ 多卡不均衡（served CV={r['pool_balance']['cv']}）——样本少或并发不足时正常。")

    if bl["final"] > cfg["max_q"] * 0.8:
        a.append(f"⚠ 终态积压 {bl['final']} 接近队列上限 {cfg['max_q']}，已丢弃 {bl['queue_full_hits']} 条提问。")
    # 队满策略建议
    ik = r["intake"]
    if ik["drop_policy"] == "reject" and ik["rejected_full"] > ik["arrivals_submitted_ok"] * 0.2:
        a.append(f"💡 队满丢弃了 {ik['rejected_full']} 条新问题（含可能的新热点）。高负载建议 "
                 f"set AUDIENCE_DROP_POLICY=evict_stale：队满时挤掉最冷最旧的，让新热点进场攒赞。")
    elif ik["drop_policy"] == "evict_stale":
        a.append(f"✅ evict_stale 生效：挤掉 {ik['evicted_stale']} 条最冷旧问题给新问题让位，"
                 f"队列保持新鲜（新热点不会被旧积压挡在门外）。")
    return a


def _recommend_coalesce(stream, whole, coalesce, slo):
    """据实测流式 TTFA 分位 + 当前合并参数，给出可操作的调参建议（纯函数，可单测）。
    stream/whole: {samples,p50_ms,p95_ms}; coalesce: {first_ms,chunk_ms,max_ms}; slo: int(ms)。"""
    a = []
    n = stream.get("samples", 0) or 0
    s_p50 = stream.get("p50_ms", 0) or 0
    s_p95 = stream.get("p95_ms", 0) or 0
    first = coalesce.get("first_ms", 0) or 0
    if n < 10:
        a.append(f"样本不足(n={n})：拉长 --duration 或提高 --qps，让流式样本≥10 再读分位。")
        return a
    w_p50 = whole.get("p50_ms", 0) or 0
    w_n = whole.get("samples", 0) or 0
    if w_n >= 10 and w_p50 > 0:
        delta = w_p50 - s_p50
        pct = (delta / w_p50 * 100) if w_p50 else 0
        if delta > 0:
            a.append(f"✅ 流式首音 p50 {s_p50:.0f}ms vs 整句 {w_p50:.0f}ms：快 {delta:.0f}ms（{pct:.0f}%）。")
        else:
            a.append(f"⚠ 流式 p50 {s_p50:.0f}ms 未快于整句 {w_p50:.0f}ms："
                     f"查 fish 是否真走流式 / 首块阈值是否过大。")
    else:
        a.append(f"流式 p50 {s_p50:.0f}ms（无整句对照样本；用 --save-baseline 关流式跑一遍再 --compare-baseline）。")
    if slo and s_p95 > slo:
        a.append(f"⚠ 流式 p95 {s_p95:.0f}ms 超 SLO {slo}ms。")
        if first > 200:
            a.append(f"建议调小 CONV_TTS_STREAM_FIRST_MS：{first}→{max(150, int(first*0.6))}（更早起播降 p95）。")
        else:
            a.append("首块阈值已很小仍超标 → 多半是 TTS 卡算力/排队瓶颈，考虑分流 cosy 或加卡。")
    elif slo:
        a.append(f"✅ 流式 p95 {s_p95:.0f}ms ≤ SLO {slo}ms（达标）。")
    return a


def _compare_service(cfg, services):
    """量化「简答(brevity)」收益：同负载下，缩短单轮服务时长对吞吐/等待/积压的影响。
    简答让 LLM 少说话→单轮更短→观众线吞吐更高、等待更短。这是 brevity 的真实收益所在
    （流式只改单答首音，不改吞吐；故 brevity 用 sim 量化、流式用 http 量化）。"""
    print(f"\n简答收益量化（缩短单轮服务时长）  ·  到达≈{cfg['lam']*60:.1f}/分  worker={cfg['workers']}\n")
    print(f"{'单轮服务s':>9} | {'已答/分':>8} | {'利用率':>6} | {'等待p50':>8} | {'等待p95':>8} | {'积压终态':>8} | 饱和")
    print("-" * 74)
    rows = []
    for sv in services:
        r = run_sim({**cfg, "service": sv})
        tp, lat, bl = r["throughput"], r["latency_wait_s"], r["backlog"]
        rows.append((sv, r))
        print(f"{sv:>9.1f} | {tp['answered_per_min']:>8.1f} | {tp['worker_utilization']:>6.2f} |"
              f" {lat['p50']:>8.0f} | {lat['p95']:>8.0f} | {bl['final']:>8d} | "
              f"{'是' if tp['saturated'] else '否'}")
    print("-" * 74)
    if len(rows) >= 2:
        (sl, rl), (ss, rs) = rows[0], rows[-1]
        d_tp = rs["throughput"]["answered_per_min"] - rl["throughput"]["answered_per_min"]
        d_w = rl["latency_wait_s"]["p50"] - rs["latency_wait_s"]["p50"]
        tp_pct = (d_tp / max(1e-6, rl["throughput"]["answered_per_min"]) * 100)
        print(f"结论：单轮 {sl:.1f}s→{ss:.1f}s（简答），吞吐 "
              f"{rl['throughput']['answered_per_min']:.1f}→{rs['throughput']['answered_per_min']:.1f}/分"
              f"（{'+' if d_tp>=0 else ''}{tp_pct:.0f}%），等待 p50 降 {d_w:.0f}s。"
              f" → 配 AUDIENCE_AUTO_BREVITY / AUDIENCE_AUTO_MAXTOK 压短观众答复。")
    return rows


def _compare_drop(cfg):
    print(f"\n队满策略对比  ·  到达≈{cfg['lam']*60:.1f}/分  worker={cfg['workers']}  "
          f"max_q={cfg['max_q']}  halflife={cfg['halflife']:.0f}s\n")
    rows = []
    for pol in ("reject", "evict_stale"):
        r = run_sim({**cfg, "drop_policy": pol})
        ik, tp, lat, pop = r["intake"], r["throughput"], r["latency_wait_s"], r["popularity"]
        rows.append((pol, ik, tp, lat, pop))
    print(f"{'策略':>11} | {'已答':>5} | {'已答均赞':>8} | {'等待p50':>7} | {'丢新问题':>8} | {'挤旧让位':>8}")
    print("-" * 70)
    for pol, ik, tp, lat, pop in rows:
        print(f"{pol:>11} | {tp['answered']:>5} | {pop['avg_likes_answered']:>8.1f} |"
              f" {lat['p50']:>7.0f} | {ik['rejected_full']:>8} | {ik['evicted_stale']:>8}")
    print("-" * 70)
    rj, ev = rows[0], rows[1]
    dl = ev[4]["avg_likes_answered"] - rj[4]["avg_likes_answered"]
    print(f"结论：evict_stale 把“队满丢新问题”从 {rj[1]['rejected_full']} 降到 "
          f"{ev[1]['rejected_full']}，已答均赞 {rj[4]['avg_likes_answered']:.1f}→"
          f"{ev[4]['avg_likes_answered']:.1f}（{'+' if dl>=0 else ''}{dl:.1f}）。"
          f" 高负载下让新热点不被旧积压挡门外。")


def _print_report(r):
    print("=" * 64)
    print(f"观众线仿真报告  ·  时长 {r['config']['horizon']:.0f}s  ·  worker={r['config']['workers']}"
          f"  cards={r['config']['cards']}  halflife={r['config']['halflife']:.0f}s")
    print("=" * 64)
    ik, tp, lat, bl, pop, pb = (r["intake"], r["throughput"], r["latency_wait_s"],
                                r["backlog"], r["popularity"], r["pool_balance"])
    print(f"[进件] 提交成功 {ik['arrivals_submitted_ok']}  到达 {ik['arrival_rate_per_min']}/分"
          f"  队满丢弃 {ik['rejected_full']}  挤旧让位 {ik['evicted_stale']}  (策略={ik['drop_policy']})")
    print(f"[吞吐] 已答 {tp['answered']}  = {tp['answered_per_min']}/分"
          f"  | 上限 {tp['ceiling_per_min_total']}/分  利用率 {tp['worker_utilization']}"
          f"  {'★饱和' if tp['saturated'] else ''}")
    print(f"[等待] p50 {lat['p50']}s  p95 {lat['p95']}s  max {lat['max']}s  mean {lat['mean']}s")
    print(f"[积压] 峰值 {bl['max']}  均值 {bl['mean']}  终态 {bl['final']}  (上限 {r['config']['max_q']})")
    print(f"[人气] 已答均赞 {pop['avg_likes_answered']}  积压均赞 {pop['avg_likes_still_backlogged']}"
          f"  积压最高赞 {pop['max_likes_still_backlogged']}")
    print(f"[多卡] {pb['cards']}卡 served={pb['served_per_card']}  CV={pb['cv']}")
    print("-" * 64)
    print("校准建议：")
    for line in r["advice"]:
        print("  • " + line)
    print("=" * 64)


def _sweep(cfg, ws):
    print(f"\n并行 worker 扫描（找“能压住积压”的最小 W）  到达≈{cfg['lam']*60:.1f}/分\n")
    print(f"{'W':>3} | {'已答/分':>8} | {'利用率':>6} | {'等待p50':>7} | {'等待p95':>7} | {'积压峰值':>7} | {'终态积压':>7} | 饱和")
    print("-" * 78)
    best = None
    for w in ws:
        c = dict(cfg)
        c["workers"] = w
        r = run_sim(c)
        tp, lat, bl = r["throughput"], r["latency_wait_s"], r["backlog"]
        sat = "是" if tp["saturated"] else "否"
        print(f"{w:>3} | {tp['answered_per_min']:>8.1f} | {tp['worker_utilization']:>6.2f} |"
              f" {lat['p50']:>7.0f} | {lat['p95']:>7.0f} | {bl['max']:>7d} | {bl['final']:>7d} | {sat}")
        if best is None and not tp["saturated"] and bl["final"] <= cfg["max_q"] * 0.3:
            best = w
    print("-" * 78)
    if best:
        print(f"建议：W={best} 起即可压住该负载（未饱和且终态积压低）。"
              f" 设 AUDIENCE_AUTO_WORKERS={best}。")
    else:
        print("提示：所测 W 均未压住积压——继续加大 W、或降 COOLDOWN/POLL、或缩短服务时长。")


# ── HTTP 模式：对在线 Hub 黑盒打压 ──────────────────────────────────
def run_http(args):
    import asyncio
    import httpx

    base = args.target.rstrip("/")
    bank = [f"{w}{i}" for w in ["这首歌能唱吗", "讲个笑话", "你最喜欢什么", "聊聊今天的事",
                                 "推荐部电影", "怎么看待AI", "教我句方言", "明天直播几点"]
            for i in range(10000)]
    rng = random.Random(args.seed)
    state = {"sent": 0, "ok": 0, "r429": 0, "reasons": defaultdict(int),
             "samples": [], "likes_ok": 0, "stop": False}

    async def submit_loop(client):
        gap = 1.0 / max(0.01, args.qps)
        while not state["stop"]:
            txt = rng.choice(bank) + f"_{rng.randint(0,1<<30)}"
            try:
                r = await client.post(f"{base}/api/audience/ask",
                                      json={"text": txt, "name": f"v{rng.randint(0,args.viewers)}"},
                                      timeout=10)
                state["sent"] += 1
                if r.status_code == 200:
                    state["ok"] += 1
                elif r.status_code == 429:
                    state["r429"] += 1
                    try:
                        state["reasons"][r.json().get("detail", "429")] += 1
                    except Exception:
                        pass
            except Exception as e:
                state["reasons"][f"err:{type(e).__name__}"] += 1
            await asyncio.sleep(gap)

    async def like_loop(client):
        while not state["stop"]:
            try:
                r = await client.get(f"{base}/api/audience/questions?order=likes&limit=10", timeout=10)
                qs = (r.json() or {}).get("questions", []) if r.status_code == 200 else []
                if qs:
                    target = rng.choice(qs[:max(1, len(qs)//2)])  # 偏向已靠前的→制造马太效应
                    rr = await client.post(f"{base}/api/audience/like", json={"id": target["id"]}, timeout=10)
                    if rr.status_code == 200:
                        state["likes_ok"] += 1
            except Exception:
                pass
            await asyncio.sleep(1.0 / max(0.01, args.like_qps))

    async def sample_loop(client):
        while not state["stop"]:
            row = {"t": time.time()}
            try:
                cap = (await client.get(f"{base}/api/capacity", timeout=10)).json()
                row["active"] = cap.get("active"); row["waiting"] = cap.get("waiting")
                row["k"] = cap.get("max")
                pools = (cap.get("gpu_pools") or {}).get("pools") or {}
                row["pool_inflight"] = {n: [x["inflight"] for x in p.get("replicas", [])]
                                        for n, p in pools.items()}
                row["streaming_tts"] = cap.get("streaming_tts") or {}
            except Exception:
                pass
            try:
                ops = (await client.get(f"{base}/api/ops/snapshot", timeout=10)).json()
                au = ops.get("audience") or {}
                row["pending"] = au.get("pending"); row["qpm"] = au.get("qpm")
                row["answered"] = (au.get("auto") or {}).get("answered")
                row["errors"] = (au.get("auto") or {}).get("errors")
                row["highlights"] = au.get("highlights")
            except Exception:
                pass
            state["samples"].append(row)
            await asyncio.sleep(args.sample)

    async def main():
        async with httpx.AsyncClient() as client:
            # 预检
            try:
                cap = (await client.get(f"{base}/api/capacity", timeout=10)).json()
                print(f"[预检] /api/capacity ok: K={cap.get('max')} auto={cap.get('auto')} "
                      f"active={cap.get('active')} waiting={cap.get('waiting')}")
            except Exception as e:
                print(f"[预检] 连不上 Hub {base}: {e}")
                return
            if args.enable_auto:
                try:
                    rr = await client.post(f"{base}/api/audience/auto", json={"on": True}, timeout=10)
                    print(f"[预检] 已开启自动应答: {rr.json()}")
                except Exception as e:
                    print(f"[预检] 开启自动应答失败: {e}")
            tasks = [asyncio.create_task(submit_loop(client))]
            if args.like_qps > 0:
                tasks.append(asyncio.create_task(like_loop(client)))
            tasks.append(asyncio.create_task(sample_loop(client)))
            await asyncio.sleep(args.duration)
            state["stop"] = True
            await asyncio.sleep(0.5)
            for t in tasks:
                t.cancel()
            if args.enable_auto and args.disable_auto_after:
                try:
                    await client.post(f"{base}/api/audience/auto", json={"on": False}, timeout=10)
                except Exception:
                    pass

    asyncio.run(main())

    # 报告
    smp = state["samples"]
    pend = [s.get("pending", 0) or 0 for s in smp]
    ans = [s.get("answered") for s in smp if s.get("answered") is not None]
    err = [s.get("errors") for s in smp if s.get("errors") is not None]
    answered_delta = (ans[-1] - ans[0]) if len(ans) >= 2 else 0
    err_delta = (err[-1] - err[0]) if len(err) >= 2 else 0
    dur_min = args.duration / 60.0
    print("=" * 64)
    print(f"HTTP 压测报告  ·  目标 {base}  ·  {args.duration}s  ·  目标 QPS={args.qps}")
    print("=" * 64)
    print(f"[进件] 发出 {state['sent']}  成功 {state['ok']}  429限流 {state['r429']}  点赞成功 {state['likes_ok']}")
    if state["r429"] > state["ok"]:
        print("  ⚠ 大量 429——多半是每-IP 限流(同机同IP)。压测前 set AUDIENCE_RATE_SEC=0 重启 Hub。")
    if state["reasons"]:
        print("  原因 top:", dict(sorted(state["reasons"].items(), key=lambda x: -x[1])[:5]))
    print(f"[吞吐] 自动已答增量 {answered_delta}  = {answered_delta/max(1e-6,dur_min):.1f}/分"
          f"  错误增量 {err_delta}")
    print(f"[积压] pending 峰值 {max(pend) if pend else 0}  均值 "
          f"{sum(pend)/len(pend):.1f}" if pend else "[积压] 无样本")
    if smp:
        last = smp[-1]
        print(f"[末态] {json.dumps({k: last.get(k) for k in ['active','waiting','k','pending','highlights']}, ensure_ascii=False)}")
        pf = last.get("pool_inflight") or {}
        for n, infl in pf.items():
            print(f"  多卡[{n}] inflight={infl}")

    # ── 流式 TTS 量化（取末样本的滚动分位，含 SLO 与调参建议）──
    stt = (smp[-1].get("streaming_tts") if smp else None) or {}
    if stt:
        m = stt.get("metrics") or {}
        cs, cw = m.get("conv_stream") or {}, m.get("conv_whole") or {}
        au = m.get("audience_stream") or {}
        slo = stt.get("slo_ttfa_p95_ms") or 0
        co = stt.get("coalesce") or {}
        print("-" * 64)
        print(f"[流式TTS] 开={stt.get('enabled')} 就绪={stt.get('eligible')} 引擎={stt.get('engine')}"
              f"  合并 first/chunk/max={co.get('first_ms')}/{co.get('chunk_ms')}/{co.get('max_ms')}ms")
        print(f"  对话流式首块 p50/p95 = {cs.get('p50_ms','-')}/{cs.get('p95_ms','-')}ms (n={cs.get('samples',0)})"
              f"  | 整句对照 {cw.get('p50_ms','-')}/{cw.get('p95_ms','-')}ms (n={cw.get('samples',0)})")
        if au.get("samples"):
            print(f"  观众自动应答首音 p50/p95 = {au.get('p50_ms','-')}/{au.get('p95_ms','-')}ms (n={au.get('samples',0)})")
        # 基线对照：上一轮(关流式)存的 conv_whole → 本轮(开流式) conv_stream
        whole_ref = cw
        if args.compare_baseline:
            try:
                base_data = json.load(open(args.compare_baseline, encoding="utf-8"))
                whole_ref = base_data.get("conv_stream") or base_data.get("conv_whole") or cw
                print(f"  [基线] 载入 {args.compare_baseline}: p50/p95="
                      f"{whole_ref.get('p50_ms','-')}/{whole_ref.get('p95_ms','-')}ms (n={whole_ref.get('samples',0)})")
            except Exception as e:
                print(f"  [基线] 载入失败 {args.compare_baseline}: {e}")
        print("  建议：")
        for line in _recommend_coalesce(cs, whole_ref, co, slo):
            print("    • " + line)
        if args.save_baseline:
            # 存“本轮主导桶”（关流式时是 conv_whole；开流式时是 conv_stream）作为下次对照基线
            keep = cs if (cs.get("samples", 0) >= cw.get("samples", 0)) else cw
            try:
                json.dump({"conv_stream": cs, "conv_whole": cw, "dominant": keep,
                           "coalesce": co, "slo_ttfa_p95_ms": slo, "ts": time.time()},
                          open(args.save_baseline, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
                print(f"  [基线] 已保存到 {args.save_baseline}（下次用 --compare-baseline 对照）")
            except Exception as e:
                print(f"  [基线] 保存失败: {e}")

    if args.json:
        print("\nJSON:\n" + json.dumps(state["samples"], ensure_ascii=False))


# ── selftest：可在本机直接跑的断言（验证仿真器自身逻辑）──────────────
def run_selftest():
    base = dict(_DEFAULT_CFG)
    base.update(horizon=600, seed=7)
    fail = []

    # 1) 单 worker + 高到达 → 必然饱和且积压增长
    c = dict(base); c.update(workers=1, lam=1.0, service=3.0, cooldown=2.0, poll=3.0)
    r = run_sim(c)
    if not r["throughput"]["saturated"]:
        fail.append("[1] 期望饱和(单worker+高到达)但未饱和")
    if r["backlog"]["final"] <= r["backlog"]["mean"] * 0.3 and r["backlog"]["max"] < 5:
        fail.append("[1] 期望积压增长但积压很小")
    print(f"[1] 单worker高负载: 饱和={r['throughput']['saturated']} 终态积压={r['backlog']['final']} ✓")

    # 2) 多 worker → 吞吐随 W 增长（W=4 应明显 > W=1）
    r1 = run_sim({**base, "workers": 1, "lam": 1.0, "service": 3.0, "cooldown": 2.0})
    r4 = run_sim({**base, "workers": 4, "lam": 1.0, "service": 3.0, "cooldown": 2.0})
    if not (r4["throughput"]["answered_per_min"] > r1["throughput"]["answered_per_min"] * 1.5):
        fail.append(f"[2] W=4 吞吐({r4['throughput']['answered_per_min']}) 未明显高于 W=1({r1['throughput']['answered_per_min']})")
    print(f"[2] 吞吐随并行增长: W1={r1['throughput']['answered_per_min']}/分 → W4={r4['throughput']['answered_per_min']}/分 ✓")

    # 3) 人气优先：高赞问题应被优先答（已答均赞 > 积压均赞），且不饱和时验证更稳
    c = dict(base); c.update(workers=1, lam=0.5, service=3.0, cooldown=1.0, halflife=300,
                             hot_frac=0.1, hot_appeal=2.0, base_appeal=0.1, like_rate=1.5)
    r = run_sim(c)
    if r["popularity"]["avg_likes_answered"] < r["popularity"]["avg_likes_still_backlogged"]:
        fail.append("[3] 人气优先失效：已答均赞 < 积压均赞")
    print(f"[3] 人气优先: 已答均赞 {r['popularity']['avg_likes_answered']} ≥ 积压均赞 "
          f"{r['popularity']['avg_likes_still_backlogged']} ✓")

    # 4) 多卡均衡：4 卡足够并发下 served CV 应较低
    c = dict(base); c.update(workers=4, cards=4, lam=1.5, service=3.0, cooldown=0.5)
    r = run_sim(c)
    if r["pool_balance"]["cv"] > 0.25:
        fail.append(f"[4] 多卡不均衡 CV={r['pool_balance']['cv']}")
    print(f"[4] 多卡均衡: 4卡 served={r['pool_balance']['served_per_card']} CV={r['pool_balance']['cv']} ✓")

    # 5) 队列上限：超高到达 → 触发 rejected_full
    c = dict(base); c.update(workers=1, lam=5.0, service=3.0, cooldown=2.0, max_q=30)
    r = run_sim(c)
    if r["backlog"]["queue_full_hits"] <= 0:
        fail.append("[5] 期望触发队满丢弃但未触发")
    print(f"[5] 队满保护: 丢弃 {r['backlog']['queue_full_hits']} 条 ✓")

    # 6) 简答收益：缩短单轮服务时长 → 吞吐升、等待降（brevity 的真实收益处）
    #    用未饱和负载(到达<上限)，让“等待随服务缩短而下降”这一关系干净显现。
    c = dict(base); c.update(workers=1, lam=0.08, cooldown=1.0)
    rows = _compare_service(c, [8.0, 4.0])
    long_w = rows[0][1]["latency_wait_s"]["p50"]
    short_w = rows[-1][1]["latency_wait_s"]["p50"]
    long_tp = rows[0][1]["throughput"]["answered_per_min"]
    short_tp = rows[-1][1]["throughput"]["answered_per_min"]
    if not (short_w <= long_w + 1e-6):
        fail.append(f"[6] 简答未降等待(未饱和): 8s p50={long_w} 4s p50={short_w}")
    print(f"[6] 简答收益(未饱和): 等待p50 {long_w}s → {short_w}s ✓")

    # 7) 流式调参建议（纯函数）：p95 超 SLO 且首块大 → 建议调小 FIRST_MS
    adv = _recommend_coalesce({"samples": 50, "p50_ms": 900, "p95_ms": 1800},
                              {"samples": 50, "p50_ms": 3000, "p95_ms": 4000},
                              {"first_ms": 300, "chunk_ms": 550, "max_ms": 1500}, 1500)
    joined = " ".join(adv)
    if "FIRST_MS" not in joined or "快" not in joined:
        fail.append(f"[7] 调参建议异常: {adv}")
    print(f"[7] 流式调参建议: 命中降幅+超SLO调参 ✓")
    # 样本不足 → 应提示样本不足、不给分位结论
    adv2 = _recommend_coalesce({"samples": 3, "p50_ms": 0, "p95_ms": 0}, {}, {"first_ms": 300}, 1500)
    if "样本不足" not in " ".join(adv2):
        fail.append(f"[7b] 样本不足未提示: {adv2}")
    print(f"[7b] 样本不足保护: ✓")

    print("-" * 50)
    if fail:
        print("SELFTEST 失败:")
        for f in fail:
            print("  " + f)
        sys.exit(1)
    print("ALL SELFTESTS PASSED")


_DEFAULT_CFG = {
    "viewers": 200, "lam": 1.0, "horizon": 1800.0, "service": 4.0, "service_cv": 0.4,
    "poll": 3.0, "cooldown": 2.0, "workers": 1, "cards": 1, "halflife": 600.0,
    "like_rate": 0.8, "hot_frac": 0.12, "hot_appeal": 2.0, "base_appeal": 0.2,
    "max_q": 200, "sample": 1.0, "seed": 42, "drop_policy": "reject",
}


def _cfg_from_args(a):
    c = dict(_DEFAULT_CFG)
    c.update(viewers=a.viewers, lam=a.qps, horizon=a.duration, service=a.service,
             service_cv=a.service_cv, poll=a.poll, cooldown=a.cooldown,
             workers=a.workers, cards=a.cards, halflife=a.halflife, like_rate=a.like_rate,
             hot_frac=a.hot_frac, max_q=a.max_q, seed=a.seed,
             drop_policy=a.drop_policy)
    return c


def main():
    ap = argparse.ArgumentParser(description="观众线并发压测/演练/容量校准")
    sub = ap.add_subparsers(dest="mode", required=True)

    s = sub.add_parser("sim", help="进程内仿真（默认，无需 GPU/网络）")
    s.add_argument("--duration", type=float, default=1800, help="仿真时长(秒)")
    s.add_argument("--qps", type=float, default=1.0, help="观众提问到达率(问/秒)")
    s.add_argument("--viewers", type=int, default=200)
    s.add_argument("--workers", type=int, default=1, help="并行自动应答 worker 数")
    s.add_argument("--cards", type=int, default=1, help="多卡副本数(均衡度验证)")
    s.add_argument("--service", type=float, default=4.0, help="单轮问答平均服务时长(秒)")
    s.add_argument("--service-cv", dest="service_cv", type=float, default=0.4)
    s.add_argument("--poll", type=float, default=3.0, help="取题轮询间隔(对齐 AUDIENCE_AUTO_POLL)")
    s.add_argument("--cooldown", type=float, default=2.0, help="两答间隔(对齐 AUDIENCE_AUTO_COOLDOWN)")
    s.add_argument("--halflife", type=float, default=600.0, help="人气热度半衰期(对齐 AUDIENCE_HOT_HALFLIFE)")
    s.add_argument("--like-rate", dest="like_rate", type=float, default=0.8, help="点赞强度基数")
    s.add_argument("--hot-frac", dest="hot_frac", type=float, default=0.12, help="热门问题占比")
    s.add_argument("--max-q", dest="max_q", type=int, default=200)
    s.add_argument("--drop-policy", dest="drop_policy", choices=["reject", "evict_stale"],
                   default="reject", help="队满策略：reject(默认)/evict_stale(挤旧让位)")
    s.add_argument("--compare-drop", dest="compare_drop", action="store_true",
                   help="同负载下对比 reject vs evict_stale")
    s.add_argument("--compare-service", dest="compare_service", type=str, default="",
                   help="量化简答收益：逗号分隔的单轮服务时长(秒)，如 8,4 表示长答→简答")
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--sweep", type=str, default="", help="逗号分隔的 worker 数列表，如 1,2,3,4")
    s.add_argument("--json", action="store_true", help="额外输出 JSON 报告")
    s.add_argument("--selftest", action="store_true")

    h = sub.add_parser("http", help="对在线 Hub 黑盒打压")
    h.add_argument("--target", type=str, required=True)
    h.add_argument("--duration", type=float, default=120)
    h.add_argument("--qps", type=float, default=2.0)
    h.add_argument("--viewers", type=int, default=200)
    h.add_argument("--like-qps", dest="like_qps", type=float, default=1.0)
    h.add_argument("--sample", type=float, default=1.0)
    h.add_argument("--enable-auto", dest="enable_auto", action="store_true")
    h.add_argument("--disable-auto-after", dest="disable_auto_after", action="store_true")
    h.add_argument("--seed", type=int, default=42)
    h.add_argument("--json", action="store_true")
    h.add_argument("--save-baseline", dest="save_baseline", type=str, default="",
                   help="把本轮流式 TTFA 分位存为基线 JSON（关流式跑一遍存基线）")
    h.add_argument("--compare-baseline", dest="compare_baseline", type=str, default="",
                   help="载入基线 JSON 与本轮对照（开流式跑一遍对比降幅）")

    a = ap.parse_args()
    if a.mode == "sim":
        if a.selftest:
            run_selftest(); return
        cfg = _cfg_from_args(a)
        if a.compare_drop:
            _compare_drop(cfg)
        elif a.compare_service:
            svs = [float(x) for x in a.compare_service.split(",") if x.strip()]
            _compare_service(cfg, svs)
        elif a.sweep:
            ws = [int(x) for x in a.sweep.split(",") if x.strip()]
            _sweep(cfg, ws)
        else:
            r = run_sim(cfg)
            _print_report(r)
            if a.json:
                print("\nJSON:\n" + json.dumps(r, ensure_ascii=False, default=str))
    elif a.mode == "http":
        run_http(a)


if __name__ == "__main__":
    main()
