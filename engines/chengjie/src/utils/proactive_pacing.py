"""主动触达节奏：亲密度 + 关系阶段双轴自适应（Phase14/15 纯函数）。

Phase14：亲密度越高 → 更短沉默/冷却阈值。
Phase15：funnel stage 叠加——warming/steady 即使 intimacy 低也可更早主动
（「聊了很多轮但亲密度分还没涨」不再被 8h 硬门槛卡住）。

组合口径（默认 ``blend=max``）：``pacing_score = max(intimacy, stage_score)`` 再线性插值。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# funnel stage → 0..100  pacing 分（与 intimacy 同量纲）
_STAGE_SCORE: Dict[str, float] = {
    "initial": 0.0,
    "warming": 28.0,
    "reunion": 32.0,
    "re_engagement": 32.0,
    "steady": 52.0,
    "intimate": 78.0,
    "close": 85.0,
    "lover": 90.0,
}


def _clamp_intimacy(intimacy: float) -> float:
    try:
        v = float(intimacy or 0.0)
    except (TypeError, ValueError):
        v = 0.0
    return max(0.0, min(100.0, v))


def stage_pacing_score(stage: str) -> float:
    """关系阶段映射到 pacing 分（未知阶段给保守中间值 12）。"""
    s = str(stage or "").strip().lower().replace("-", "_")
    if not s:
        return 0.0
    return float(_STAGE_SCORE.get(s, 12.0))


def combined_pacing_score(
    intimacy: float,
    stage: str = "",
    *,
    blend: str = "max",
) -> float:
    """双轴合成 pacing 分（0..100）。

    ``max``（默认）：取 intimacy 与 stage 分较高者——任一维度够熟就可更早问候。
    ``avg``：二者平均，更保守。
    """
    i = _clamp_intimacy(intimacy)
    ss = stage_pacing_score(stage)
    mode = str(blend or "max").strip().lower()
    if mode == "avg":
        return (i + ss) / 2.0
    return max(i, ss)


def _lerp_hours(score: float, at_0: float, at_100: float) -> float:
    t = _clamp_intimacy(score) / 100.0
    return float(at_0) + (float(at_100) - float(at_0)) * t


def parse_adaptive_pacing_cfg(
    proactive_cfg: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """从 ``companion.proactive_topic.adaptive_pacing`` 解析，缺省 enabled=false。"""
    pc = proactive_cfg or {}
    ap = pc.get("adaptive_pacing") if isinstance(pc.get("adaptive_pacing"), dict) else {}
    base_silent = float(pc.get("min_silent_hours", 24) or 24)
    base_cool = float(pc.get("cooldown_hours", 72) or 72)
    silent_blk = (ap.get("min_silent_hours") or {}) if isinstance(ap, dict) else {}
    cool_blk = (ap.get("cooldown_hours") or {}) if isinstance(ap, dict) else {}
    return {
        "enabled": bool((ap or {}).get("enabled", False)),
        "blend": str((ap or {}).get("blend") or "max").strip().lower(),
        "min_silent_base": float(silent_blk.get("base", base_silent) or base_silent),
        "min_silent_at_0": float(silent_blk.get("at_intimacy_0", base_silent * 2) or base_silent * 2),
        "min_silent_at_100": float(silent_blk.get("at_intimacy_100", max(1.0, base_silent * 0.5))
                              or max(1.0, base_silent * 0.5)),
        "cooldown_base": float(cool_blk.get("base", base_cool) or base_cool),
        "cooldown_at_0": float(cool_blk.get("at_intimacy_0", base_cool * 1.5) or base_cool * 1.5),
        "cooldown_at_100": float(cool_blk.get("at_intimacy_100", max(1.0, base_cool * 0.65))
                            or max(1.0, base_cool * 0.65)),
    }


def effective_min_silent_hours(
    intimacy: float,
    *,
    stage: str = "",
    base_hours: float,
    pacing_cfg: Optional[Dict[str, Any]] = None,
) -> float:
    """该会话生效的沉默阈值（小时）。"""
    p = pacing_cfg or {}
    if not p.get("enabled"):
        return max(0.0, float(base_hours or 0))
    score = combined_pacing_score(
        intimacy, stage, blend=str(p.get("blend") or "max"))
    return max(0.0, _lerp_hours(
        score,
        p.get("min_silent_at_0", float(base_hours) * 2),
        p.get("min_silent_at_100", max(1.0, float(base_hours) * 0.5)),
    ))


def effective_cooldown_hours(
    intimacy: float,
    *,
    stage: str = "",
    base_hours: float,
    pacing_cfg: Optional[Dict[str, Any]] = None,
) -> float:
    """该会话生效的主动冷却（小时）。"""
    p = pacing_cfg or {}
    if not p.get("enabled"):
        return max(0.0, float(base_hours or 0))
    score = combined_pacing_score(
        intimacy, stage, blend=str(p.get("blend") or "max"))
    return max(0.0, _lerp_hours(
        score,
        p.get("cooldown_at_0", float(base_hours) * 1.5),
        p.get("cooldown_at_100", max(1.0, float(base_hours) * 0.65)),
    ))


# ── P0 未回退避（2026-07-29）────────────────────────────────────────────────
# 实锤根因：冷却只看时间不看响应 → 对从不回复的联系人每到冷却点就再发一条
# 「好久没联系」（近 14 天 45% 的主动发送落在「上一条没回又发下一条」）。
# 语义：上一条主动开场之后对方一直没回 → 下一次冷却按倍数拉长（48h→6天→18天→
# 封顶月频）；对方一开口 streak 立即归零、回到正常节奏。默认开（防骚扰护栏，
# 只会让发送更少更克制，不新增任何发送）。

def parse_no_reply_backoff_cfg(
    proactive_cfg: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """从 ``companion.proactive_topic.no_reply_backoff`` 解析（缺省 enabled=true）。"""
    pc = proactive_cfg or {}
    blk = pc.get("no_reply_backoff") if isinstance(
        pc.get("no_reply_backoff"), dict) else {}

    def _f(key: str, default: float) -> float:
        try:
            return float(blk.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    return {
        "enabled": bool(blk.get("enabled", True)),
        "multiplier": max(1.0, _f("multiplier", 3.0)),
        "max_backoff_hours": max(1.0, _f("max_backoff_hours", 720.0)),
        "stop_after": max(0, int(_f("stop_after", 0.0))),
        # P1：从未开口过的联系人（last_in_ts==0）连续 N 条主动没回 → 彻底出圈。
        # 「从未回复」不属于陪伴回访语义（那是获客场景）：给足 2 次尝试后停，
        # 比对普通联系人的月频保底更严——0 互动连记忆都无从谈起。0=关。
        "stop_after_never_replied": max(0, int(_f("stop_after_never_replied", 2.0))),
    }


def unanswered_streak(
    last_proactive_ts: float, stored_streak: int, last_inbound_ts: float,
) -> int:
    """生效的「连续未回」次数：对方在上次主动之后回过话 → 归零。

    ``last_proactive_ts<=0``（从未主动过）→ 0；``last_inbound_ts`` 未知（0）时
    保守按存量 streak 走（宁可少打扰）。
    """
    try:
        lp = float(last_proactive_ts or 0.0)
        li = float(last_inbound_ts or 0.0)
        st = int(stored_streak or 0)
    except (TypeError, ValueError):
        return 0
    if lp <= 0:
        return 0
    if li >= lp:
        return 0  # 上次主动之后对方回过话
    return max(0, st)


def backoff_cooldown_hours(
    base_hours: float, streak: int, backoff_cfg: Optional[Dict[str, Any]],
) -> float:
    """按未回 streak 拉长冷却：base × multiplier^streak，封顶 max_backoff_hours。"""
    b = max(0.0, float(base_hours or 0.0))
    cfg = backoff_cfg or {}
    if not cfg.get("enabled") or int(streak or 0) <= 0:
        return b
    mult = max(1.0, float(cfg.get("multiplier", 3.0) or 3.0))
    cap = max(1.0, float(cfg.get("max_backoff_hours", 720.0) or 720.0))
    try:
        scaled = b * (mult ** int(streak))
    except OverflowError:
        return cap
    return min(scaled, cap)


def backoff_exhausted(streak: int, backoff_cfg: Optional[Dict[str, Any]]) -> bool:
    """连续未回达到 stop_after（>0 才启用）→ 彻底停发，只等对方先开口。"""
    cfg = backoff_cfg or {}
    if not cfg.get("enabled"):
        return False
    stop = int(cfg.get("stop_after", 0) or 0)
    return stop > 0 and int(streak or 0) >= stop


# ── P2 回复率反哺（2026-07-29）────────────────────────────────────────────────
# streak 是**急性**信号（这一轮连着没回几条）；长期回复率是**慢性**信号（TA 历来
# 回不回我们的主动开场）。账本 obs_n/obs_replied 半衰滑窗累计「主动→是否得到回应」，
# 慢性低回复 → 冷却×stretch（在退避之外再拉长记性）；慢性高回复 → 可配 relax<1
# 略提频（对方明显喜欢被想起）。观察数不足不判——比率信号必须攒够样本才可信。

def parse_response_pacing_cfg(
    proactive_cfg: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """从 ``companion.proactive_topic.response_pacing`` 解析（缺省 enabled=true）。

    relax 默认 1.0＝不提频（提频会**增加**发送，是运营决策，overlay 显式开）；
    stretch 只减发送，默认即生效。``auto_thresholds``（P6，默认关）＝分位阈值
    自适应：low/high 不再用冷启动固定值，改按**本库**各会话长期回复率分布的
    分位数校准（人群整体回复率高/低的部署各得其所）。
    """
    pc = proactive_cfg or {}
    blk = pc.get("response_pacing") if isinstance(
        pc.get("response_pacing"), dict) else {}

    def _f(key: str, default: float) -> float:
        try:
            return float(blk.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    auto_blk = blk.get("auto_thresholds") if isinstance(
        blk.get("auto_thresholds"), dict) else {}

    def _af(key: str, default: float) -> float:
        try:
            return float(auto_blk.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    return {
        "enabled": bool(blk.get("enabled", True)),
        "min_obs": max(1, int(_f("min_obs", 4.0))),
        "low_rate": min(1.0, max(0.0, _f("low_rate", 0.15))),
        "stretch": max(1.0, _f("stretch", 1.5)),
        "high_rate": min(1.0, max(0.0, _f("high_rate", 0.6))),
        # relax 夹在 [0.5, 1.0]：低于 0.5 的提频配置视为手滑，防骚扰底线
        "relax": min(1.0, max(0.5, _f("relax", 1.0))),
        "auto_thresholds": {
            "enabled": bool(auto_blk.get("enabled", False)),
            "low_percentile": min(45.0, max(5.0, _af("low_percentile", 25.0))),
            "high_percentile": min(95.0, max(55.0, _af("high_percentile", 75.0))),
            "min_population": max(4, int(_af("min_population", 12.0))),
            "min_separation": min(0.5, max(0.05, _af("min_separation", 0.15))),
        },
    }


def _percentile(sorted_vals: list, pct: float) -> float:
    """线性插值分位数（输入须已升序；空列表返回 0.0）。零依赖小实现。"""
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_vals[0])
    pos = (max(0.0, min(100.0, float(pct))) / 100.0) * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_vals[lo]) * (1 - frac) + float(sorted_vals[hi]) * frac


def calibrate_response_thresholds(
    observations: list,
    response_cfg: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """按本库回复率分布校准 low/high 阈值（P6 纯函数）。

    ``observations``＝账本各会话 ``(obs_n, obs_replied)`` 对；只取 obs_n≥min_obs
    的会话（与 response_rate_factor 同一适用人群——阈值就是为它们定的）。

    返回 ``{low_rate, high_rate, source, population}``：
    - 合格人群 < min_population → 原配置值（source="config"，冷启动期）；
    - 分位数塌缩（high-low < min_separation＝人群回复率同质）→ 原配置值
      （在同质人群上按微小差异分层是追噪声）；
    - 否则 low=P{low_percentile}、high=P{high_percentile}（source="percentile"），
      并保底间隔。
    """
    cfg = response_cfg or {}
    auto = cfg.get("auto_thresholds") or {}
    fixed = {
        "low_rate": float(cfg.get("low_rate", 0.15) or 0.15),
        "high_rate": float(cfg.get("high_rate", 0.6) or 0.6),
        "source": "config", "population": 0,
    }
    if not auto.get("enabled"):
        return fixed
    min_obs = int(cfg.get("min_obs", 4) or 4)
    rates = []
    for pair in observations or []:
        try:
            n, hits = int(pair[0] or 0), int(pair[1] or 0)
        except (TypeError, ValueError, IndexError):
            continue
        if n >= min_obs:
            rates.append(min(1.0, max(0.0, hits / n)))
    fixed["population"] = len(rates)
    if len(rates) < int(auto.get("min_population", 12) or 12):
        return fixed
    rates.sort()
    low = _percentile(rates, float(auto.get("low_percentile", 25.0)))
    high = _percentile(rates, float(auto.get("high_percentile", 75.0)))
    sep = float(auto.get("min_separation", 0.15) or 0.15)
    if (high - low) < sep:
        return fixed  # 同质人群：校准无意义，守住配置值
    return {
        "low_rate": round(min(0.5, max(0.0, low)), 4),
        "high_rate": round(min(0.95, max(low + sep, high)), 4),
        "source": "percentile", "population": len(rates),
    }


def response_rate_factor(
    obs_n: int, obs_replied: int,
    response_cfg: Optional[Dict[str, Any]],
) -> float:
    """长期回复率 → 冷却倍率：低回复 stretch（≥1）、高回复 relax（≤1）、中间/样本
    不足/未启用 → 1.0（不动）。纯函数。"""
    cfg = response_cfg or {}
    if not cfg.get("enabled"):
        return 1.0
    try:
        n = int(obs_n or 0)
        hits = max(0, int(obs_replied or 0))
    except (TypeError, ValueError):
        return 1.0
    if n < int(cfg.get("min_obs", 4) or 4):
        return 1.0
    rate = min(1.0, hits / n) if n > 0 else 0.0
    if rate <= float(cfg.get("low_rate", 0.15) or 0.0):
        return max(1.0, float(cfg.get("stretch", 1.5) or 1.0))
    if rate >= float(cfg.get("high_rate", 0.6) or 1.0):
        return min(1.0, max(0.5, float(cfg.get("relax", 1.0) or 1.0)))
    return 1.0


def never_replied_exhausted(
    streak: int, last_inbound_ts: float,
    backoff_cfg: Optional[Dict[str, Any]],
) -> bool:
    """「从未回复」出圈判定（P1）：一条入站都没有 + 连续 N 条主动未回 → 停。

    与 ``backoff_exhausted``（对所有人的硬停，默认关）互补：本判定只针对
    0 互动联系人（默认 2 次后停）——他们没有记忆、没有关系，不属于陪伴
    回访语义；继续投喂只会烧账号信誉。对方任何时候开口即自动恢复
    （last_inbound_ts>0 本判定即失效）。
    """
    cfg = backoff_cfg or {}
    if not cfg.get("enabled"):
        return False
    stop = int(cfg.get("stop_after_never_replied", 0) or 0)
    if stop <= 0:
        return False
    try:
        li = float(last_inbound_ts or 0.0)
    except (TypeError, ValueError):
        li = 0.0
    return li <= 0 and int(streak or 0) >= stop


__all__ = [
    "stage_pacing_score",
    "combined_pacing_score",
    "parse_adaptive_pacing_cfg",
    "effective_min_silent_hours",
    "effective_cooldown_hours",
    "parse_no_reply_backoff_cfg",
    "unanswered_streak",
    "backoff_cooldown_hours",
    "backoff_exhausted",
    "never_replied_exhausted",
    "parse_response_pacing_cfg",
    "response_rate_factor",
    "calibrate_response_thresholds",
]
