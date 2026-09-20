# -*- coding: utf-8 -*-
"""双向全自动「对级验收」纯函数核心（P1 2026-08-07，.104/.198 排障 §98 续）。

与两位邻居的分工（改动前先读）：

- ``tools/why_no_reply.py``：**单会话、负向**——「这个会话为什么没自动回」，
  逐层闸门排障；
- ``src/inbox/duplex_drill.py``（并行 governance 线）：**放行机制**——白名单
  账号对豁免守卫/封顶、演练硬顶；
- 本模块：**账号对、正向**——「双向全自动到底跑起来没有、质量如何」。
  读两个方向的会话镜像，产出轮次/应答率/回复延迟/复读风险/秒回风险，
  给出可交付的验收判词。销售 PoC / 内测机（.104/.198）验收的读数面。

设计约束：纯函数吃 message 行列表（``{direction, ts, text}``，ts 升序），
不触 store/config——CLI（``tools/duplex_report.py``）负责只读取数与拼装；
判定口径**刻意复用** peer_bot_guard 的归一化（同一把尺子量「复读」，报告
警示的阈值才与真实刹车一致）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.inbox.peer_bot_guard import is_media_placeholder, normalize_text

# 与 peer_bot_guard 默认一致的风险阈值（报告仅警示，不动作）
REPEAT_BRAKE_N = 3          # 对面复读刹车：连续同文 ≥3
INSTANT_WINDOW_SEC = 3.0    # 秒回×同文熔断窗口
DEFAULT_MIN_REPLY_RATE = 0.5


@dataclass
class SideStats:
    """一个方向（某账号视角的会话）的验收读数。

    注意 ``answer_rate`` 的数学性质：连发段语义下，未回应的入站会合并进
    同一段，率恒 ∈ {1, (n-1)/n}——它只能当参考读数，**不能**当「半哑火」
    判据；真正的半哑火信号是 ``pending_in_age_s``（最后一段入站悬了多久
    没人回，跨段累积、不受合并稀释）。
    """
    inbound: int = 0                 # 收到对方消息条数（窗口内）
    outbound: int = 0                # 本方发出条数（窗口内）
    in_bursts: int = 0               # 入站连发段数（应答率的分母）
    answered: int = 0                # 有回应的入站段数
    latencies: List[float] = field(default_factory=list)  # 段级应答延迟（秒）
    out_repeat_max: int = 0          # 本方出站最大连续同文（≥3 会触发对面复读刹车）
    fast_replies: int = 0            # <3s 的应答段数（秒回熔断风险因子）
    pending_in_age_s: float = 0.0    # 末段入站悬置时长（0=没有悬置）
    last_in_ts: float = 0.0
    last_out_ts: float = 0.0

    @property
    def answer_rate(self) -> float:
        return (self.answered / self.in_bursts) if self.in_bursts else 0.0


def _runs(messages: List[Dict[str, Any]]) -> List[Tuple[str, float, float, List[str]]]:
    """时间线 → 同方向连发段 ``(direction, first_ts, last_ts, texts)`` 列表。

    连发段是应答语义的自然单位：客户连发 5 条、AI 回 1 条＝答了 1 段（而不是
    「答了 1/5 条」）——与入站爆发合并（inbound_merge）的产品语义一致。
    """
    runs: List[Tuple[str, float, float, List[str]]] = []
    for m in messages or []:
        d = str(m.get("direction") or "")
        if d not in ("in", "out"):
            continue
        try:
            ts = float(m.get("ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        text = str(m.get("text") or "")
        if runs and runs[-1][0] == d:
            prev = runs[-1]
            runs[-1] = (d, prev[1], ts, prev[3] + [text])
        else:
            runs.append((d, ts, ts, [text]))
    return runs


def consecutive_repeat_max(texts: List[str]) -> int:
    """文本序列里的最大连续同文段长（归一化口径与 peer_bot_guard 一致；
    媒体占位符/空文本中断计数——与守卫的豁免语义相同，绝不把「连发 33 条
    语音」算成复读）。"""
    best = 0
    streak = 0
    prev: Optional[str] = None
    for t in texts or []:
        if is_media_placeholder(t):
            prev, streak = None, 0
            continue
        n = normalize_text(t)
        if not n:
            prev, streak = None, 0
            continue
        if n == prev:
            streak += 1
        else:
            prev, streak = n, 1
        best = max(best, streak)
    return best


def build_side_stats(
    messages: List[Dict[str, Any]], *, now: Optional[float] = None,
) -> SideStats:
    """单方向读数（纯函数）。``messages`` = 该会话窗口内的行（ts 升序）。

    ``now`` 用于计算末段入站的悬置时长（CLI 传真实时钟；缺省用消息内
    最大 ts——纯离线重放时悬置恒 0，判词退化为只看死/活与风险面）。
    """
    s = SideStats()
    out_texts: List[str] = []
    for m in messages or []:
        d = str(m.get("direction") or "")
        try:
            ts = float(m.get("ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        if d == "in":
            s.inbound += 1
            s.last_in_ts = max(s.last_in_ts, ts)
        elif d == "out":
            s.outbound += 1
            s.last_out_ts = max(s.last_out_ts, ts)
            out_texts.append(str(m.get("text") or ""))
    s.out_repeat_max = consecutive_repeat_max(out_texts)
    runs = _runs(messages)
    for i, (d, _first, last, _texts) in enumerate(runs):
        if d != "in":
            continue
        s.in_bursts += 1
        if i + 1 < len(runs) and runs[i + 1][0] == "out":
            s.answered += 1
            lat = max(0.0, runs[i + 1][1] - last)
            s.latencies.append(lat)
            if lat < INSTANT_WINDOW_SEC:
                s.fast_replies += 1
    if runs and runs[-1][0] == "in":
        ref = float(now) if now is not None else runs[-1][2]
        s.pending_in_age_s = max(0.0, ref - runs[-1][2])
    return s


def percentile(values: List[float], pct: float) -> float:
    """零依赖分位数（线性插值）；空列表 → 0.0。"""
    if not values:
        return 0.0
    vs = sorted(values)
    if len(vs) == 1:
        return float(vs[0])
    k = (len(vs) - 1) * max(0.0, min(1.0, pct))
    lo = int(k)
    hi = min(lo + 1, len(vs) - 1)
    frac = k - lo
    return float(vs[lo] * (1 - frac) + vs[hi] * frac)


def side_summary(s: SideStats) -> Dict[str, Any]:
    """SideStats → JSON 安全摘要（CLI/测试共用口径）。"""
    return {
        "inbound": s.inbound,
        "outbound": s.outbound,
        "in_bursts": s.in_bursts,
        "answered": s.answered,
        "answer_rate": round(s.answer_rate, 3),
        "latency_p50_s": round(percentile(s.latencies, 0.5), 1),
        "latency_p95_s": round(percentile(s.latencies, 0.95), 1),
        "latency_max_s": round(max(s.latencies), 1) if s.latencies else 0.0,
        "fast_replies": s.fast_replies,
        "out_repeat_max": s.out_repeat_max,
        "pending_in_age_s": round(s.pending_in_age_s, 1),
    }


PENDING_WARN_SEC = 900.0    # 末段入站悬置超过 15min 即警（半哑火信号）


def duplex_verdict(
    a_stats: SideStats,
    b_stats: SideStats,
    *,
    a_label: str = "A",
    b_label: str = "B",
    min_reply_rate: float = DEFAULT_MIN_REPLY_RATE,
    pending_warn_sec: float = PENDING_WARN_SEC,
) -> List[Dict[str, str]]:
    """对级判词（level ∈ ok/warn/block；与 why_no_reply 同一分级语言）。

    只依赖消息动力学；档位/封顶/守卫的**原因**归 why_no_reply 管——这里
    发现单边不回时指路即可，不重复实现闸门解释。半哑火判据用
    ``pending_in_age_s``（悬置时长）而非应答率——率在连发段语义下数学上
    退化（见 SideStats docstring），首版踩过，勿改回。
    """
    out: List[Dict[str, str]] = []

    def _v(level: str, code: str, msg: str) -> None:
        out.append({"level": level, "code": code, "msg": msg})

    sides = ((a_label, a_stats, b_label), (b_label, b_stats, a_label))
    dead = []
    for label, s, peer in sides:
        if s.inbound > 0 and s.outbound == 0:
            dead.append(label)
            _v("block", f"{label}_silent",
               f"{label} 侧窗口内收到 {s.inbound} 条却零回复——单边哑火。"
               f"用 why_no_reply 对 {label} 侧会话逐层定位（档位/封顶/守卫/"
               f"投递开关）")
        elif s.pending_in_age_s > pending_warn_sec:
            _v("warn", f"{label}_pending",
               f"{label} 侧最后一段入站已悬 {s.pending_in_age_s / 60.0:.0f} "
               f"分钟未回——半哑火：查该侧拟人延迟/班表/预算，或刚被降档"
               f"（why_no_reply 可定位）")
    if not dead:
        if a_stats.answered and b_stats.answered:
            rounds = min(a_stats.answered, b_stats.answered)
            _v("ok", "duplex_alive",
               f"双向全自动运转：双方各有应答（往返 ≥{rounds} 轮），"
               f"延迟 p50 {percentile(a_stats.latencies, 0.5):.0f}s / "
               f"{percentile(b_stats.latencies, 0.5):.0f}s")
        elif a_stats.inbound == 0 and b_stats.inbound == 0:
            _v("warn", "no_traffic",
               "窗口内两侧都没有入站消息——先从任一侧手发一条起串")
    for label, s, peer in sides:
        if s.out_repeat_max >= REPEAT_BRAKE_N:
            _v("warn", f"{label}_repeat_risk",
               f"{label} 侧出站出现连续 {s.out_repeat_max} 条同文——已达对面"
               f"复读刹车阈值（{REPEAT_BRAKE_N}），{peer} 侧会被降档 review。"
               f"AI 回复雷同请查 reply_variety；人工测试别复读同一句")
        elif s.fast_replies >= 2 and s.out_repeat_max >= REPEAT_BRAKE_N - 1:
            _v("warn", f"{label}_echo_risk",
               f"{label} 侧 {s.fast_replies} 段 <{INSTANT_WINDOW_SEC:.0f}s 秒回"
               f"且接近同文——逼近对面秒回×同文熔断（连续 2 次即降档）")
    return out


__all__ = [
    "REPEAT_BRAKE_N",
    "INSTANT_WINDOW_SEC",
    "PENDING_WARN_SEC",
    "SideStats",
    "consecutive_repeat_max",
    "build_side_stats",
    "percentile",
    "side_summary",
    "duplex_verdict",
]
