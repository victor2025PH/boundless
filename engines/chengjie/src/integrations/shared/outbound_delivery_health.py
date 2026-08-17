"""出站投递健康判定（纯函数，供 HealthWatchdog 与 ops 卡共用）。

`outbound_delivery_stats` 只负责**数**，这里负责**判**——两者分开是因为「什么算
不健康」比「怎么计数」难得多，而且必须能脱离常驻服务被测试。

## 为什么不能只用一个「失败率 > X」阈值

看板上线后第一个诱惑就是 `failure_rate > 0.3 → 告警`。那条规则会在第一天就死掉：

- **Discord 403**（不能主动私信陌生人）是**产品信号**——说明社群覆盖不够，该去
  运营，不是该去救火；
- **WhatsApp/Messenger 24h 窗口过期**同理，是业务规则本身，不是故障；
- **peer_invalid**（对方注销了账号）永远会有一个自然底噪。

这三类每天都在发生。把它们和「网络断了」混在同一个阈值里，运维会在一周内学会
无视这条告警——本仓 `AGENTS.md` 里那条「L3 草稿在 SLA 覆盖内却烂了 167h、根因是
报进了虚空」记的就是这种死法。**告警的价值 = 触发时接收者会去做点什么**，所以判据
必须按「有没有人能修」分家。

## 三类信号，各自独立

1. ``infra``——基础设施类失败（超时/网络/鉴权/没有 worker/平台 5xx）在窗口内既
   够多又够密。这类**一定有人能修**，值得半夜轰人。
2. ``blocked``——Kill-Switch / 发送闸门拦下的量激增。这不是失败，是**有人把开关
   关了没打开**（或风控闸门被触发）。客户同样没收到，但处置动作完全不同：去看
   开关，而不是去看网络。混进 ``infra`` 会让运维找错方向。
3. ``stuck_queue``——RPA 的 ``queued`` 在涨、而同平台的 ``attempts`` 一动不动。
   ``queued`` 只表示「交给了队列」，手机可能关机数小时；队列不消耗＝那些话一句
   都没说出去，而且**没有任何失败计数**会出现（这正是把 queued 单列的理由）。

三类都**按平台分开判**：否则某平台的业务性 403 洪水会把另一平台真正的网络故障
淹掉（分母被撑大，率被稀释）。

判据一律用**滚动窗增量**（当前快照 - 窗口内最老快照），基线取最老样本 ⇒ 增量
只会低估不会高估，宁可漏报不误报。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Tuple

#: 「有人能修」的失败原因——只有这些进 infra 告警。
#: 未登记的原因（含 ``exc_*`` 这类兜底类名）**按 infra 处理**：新出现的、我们还没
#: 见过的失败更可能是真故障；把未知归进「业务正常」会让新故障静默，那是更贵的错。
BUSINESS_REASONS = frozenset({
    # 平台业务规则：不是坏了，是不允许
    "forbidden",           # Discord 不能私信陌生人 / 频道权限不足
    "window_expired",      # Messenger / IG / WA 的 24h 客服窗口
    "peer_invalid",        # 对方注销 / 换号 / 拉黑（自然底噪）
    "dead_peer",           # 本仓自己的死 peer 拉黑名单
    "media_too_large",     # 素材超平台上限——该换素材，不是该救火
    "empty_text",          # 空文本，上游拟稿问题
    "rate_limited",        # 平台限流：退避即可，本就是设计内
    "unsupported",         # 该平台不支持这种消息形态
})

#: ``blocked`` 里属于「开关被人关着」的族——与平台拒收区分开。
GATE_BLOCKED = frozenset({"kill_switch", "send_gate"})


def is_infra_reason(reason: str) -> bool:
    """这条失败原因是不是「有人能修」的基础设施类。"""
    return str(reason or "unspecified") not in BUSINESS_REASONS


def snapshot(dump: Mapping[str, Any]) -> Dict[str, Any]:
    """从 ``OutboundDeliveryStats.dump()`` 取出判定需要的最小快照。

    只留判定用得上的字段：样本要按 tick 存一串，存整个 dump 会把内存和日志都撑肥。
    """
    by_p = dump.get("by_platform") or {}
    by_r = dump.get("by_reason") or {}
    return {
        "by_platform": {str(p): {k: int(v or 0) for k, v in (row or {}).items()}
                        for p, row in by_p.items() if isinstance(row, Mapping)},
        "by_reason": {str(p): {str(r): int(n or 0) for r, n in (rs or {}).items()}
                      for p, rs in by_r.items() if isinstance(rs, Mapping)},
    }


def _delta(cur: Mapping[str, int], base: Mapping[str, int]) -> Dict[str, int]:
    """逐键增量，夹 0。计数器只增，负值只可能来自 reset（重启/测试）——
    那时按 0 处理比按负数处理安全（重启后不该立刻报一个假的「恢复」）。"""
    out: Dict[str, int] = {}
    for k, v in cur.items():
        d = int(v or 0) - int(base.get(k, 0) or 0)
        if d > 0:
            out[k] = d
    return out


def diagnose(
    base: Mapping[str, Any],
    cur: Mapping[str, Any],
    *,
    min_infra: int = 5,
    min_infra_rate: float = 0.25,
    min_blocked: int = 10,
    min_queued_stuck: int = 5,
) -> List[Dict[str, Any]]:
    """两份快照 → 需要告警的发现列表（按严重度与规模排序）。

    ``min_infra`` 与 ``min_infra_rate`` **必须同时满足**：只看绝对数，高流量平台
    的正常底噪会一直触发；只看比率，一个平台窗口内只发了 2 条挂了 1 条就会 50%
    ——两个条件都过才既不吵也不漏。
    """
    cb_p = dict(base.get("by_platform") or {})
    cc_p = dict(cur.get("by_platform") or {})
    cb_r = dict(base.get("by_reason") or {})
    cc_r = dict(cur.get("by_reason") or {})

    findings: List[Dict[str, Any]] = []
    for plat in sorted(cc_p):
        row = _delta(cc_p.get(plat) or {}, cb_p.get(plat) or {})
        reasons = _delta(cc_r.get(plat) or {}, cb_r.get(plat) or {})
        attempts = row.get("sent", 0) + row.get("failed", 0) + row.get("blocked", 0)

        # ① 基础设施类失败
        infra = {r: n for r, n in reasons.items()
                 if is_infra_reason(r) and r not in GATE_BLOCKED}
        # by_reason 里 failed 与 blocked 混在一起；blocked 的原因族已被 GATE_BLOCKED
        # 排掉，剩下的按 failed 计——比再开一个维度便宜，且误差方向是低估。
        infra_n = min(sum(infra.values()), row.get("failed", 0)) if infra else 0
        if infra_n >= min_infra and attempts > 0 and infra_n / attempts >= min_infra_rate:
            findings.append({
                "kind": "infra",
                "platform": plat,
                "count": infra_n,
                "attempts": attempts,
                "rate": round(infra_n / attempts, 3),
                "reasons": _top(infra),
            })

        # ② 闸门拦截激增（有人把开关关着）
        blocked_n = row.get("blocked", 0)
        if blocked_n >= min_blocked:
            findings.append({
                "kind": "blocked",
                "platform": plat,
                "count": blocked_n,
                "attempts": attempts,
                "reasons": _top({r: n for r, n in reasons.items()
                                 if r in GATE_BLOCKED} or reasons),
            })

        # ③ 队列只进不出（RPA 手机离线 / runner 卡死）
        queued_n = row.get("queued", 0)
        if queued_n >= min_queued_stuck and attempts == 0:
            findings.append({
                "kind": "stuck_queue",
                "platform": plat,
                "count": queued_n,
                "attempts": 0,
                "reasons": {},
            })

    order = {"infra": 0, "stuck_queue": 1, "blocked": 2}
    findings.sort(key=lambda f: (order.get(f["kind"], 9), -int(f["count"])))
    return findings


def _top(d: Mapping[str, int], n: int = 3) -> Dict[str, int]:
    return dict(sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))[:n])


def summarize(findings: List[Dict[str, Any]]) -> str:
    """发现列表 → 一行人话（webhook / 日志共用，别在两处各写一遍）。"""
    if not findings:
        return ""
    label = {"infra": "链路故障", "blocked": "发送闸门拦截", "stuck_queue": "队列积压未消耗"}
    parts = []
    for f in findings[:4]:
        rs = "、".join(f"{r}×{n}" for r, n in (f.get("reasons") or {}).items())
        seg = f"{f['platform']} {label.get(f['kind'], f['kind'])} {f['count']} 条"
        if f.get("attempts"):
            seg += f"/{f['attempts']}"
        if rs:
            seg += f"（{rs}）"
        parts.append(seg)
    return "；".join(parts)


def worst_kind(findings: List[Dict[str, Any]]) -> str:
    """最严重的一类（用于 rate_key / 文案分支）。"""
    for kind in ("infra", "stuck_queue", "blocked"):
        if any(f.get("kind") == kind for f in findings):
            return kind
    return ""


__all__ = [
    "BUSINESS_REASONS",
    "GATE_BLOCKED",
    "diagnose",
    "is_infra_reason",
    "snapshot",
    "summarize",
    "worst_kind",
]
