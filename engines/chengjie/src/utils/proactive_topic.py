"""P1 主动话题发起：从长期记忆挑一个"值得主动回访"的话题种子（确定性纯函数）。

陪伴型 AI 的差异化能力——沉默一段时间后，不是干巴巴"好久没聊"，而是自然回到对方
真正在意的事（"上次你说在备考，结果怎么样？"）。本模块只做**确定性选择 + 生成一行
prompt 指令**，真实文案由回复生成层产出；何时发由调度层决定（与 empathy_strategy
"选策略→注入一行指令"同构）。

设计取舍：
- **纯函数、零 IO、可单测**：不读 config、不调 LLM、不触网。
- **只回访高置信事实**：优先 ``user_stated`` / 已人工确认（R12/R15），**绝不拿
  ``ai_inferred`` 的"猜测"去回访**——猜错了一开口就尴尬，反噬陪伴信任。
- **排除 stale**：被 R10/R11 推翻的旧事实不再回访。
- **沉默不足不打扰**：活跃用户不主动插话；关系/记忆不足时退化为温和问候。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

# 主动开场模式
MODE_NONE = ""                     # 不主动开场（沉默不足）
MODE_FOLLOW_UP = "follow_up"       # 回访某条高置信记忆
MODE_GENTLE_CHECKIN = "gentle_checkin"  # 无记忆钩子，温和问候

# 长别离阈值（超过则即便有记忆钩子也先柔和重连）
_LONG_ABSENCE_HOURS = 14 * 24

# ── 沉默分档（P0，2026-07-29：措辞对齐事实）──────────────────────────────
# 实锤事故：gentle_checkin 指令硬编码「好久没联系了」，而自适应节奏下沉默
# 4~10 小时就会触发 → 用户昨晚才聊过、今早收到「好久没联系」，一眼机器人。
# 修法＝按真实沉默时长分档给措辞，「好久没联系」只许在 ≥14 天档出现。
GAP_SAME_DAY = "same_day"   # < 24h：今天/昨天才聊过
GAP_FEW_DAYS = "few_days"   # 1~3 天
GAP_WEEK = "week"           # 3~14 天
GAP_LONG = "long"           # ≥ 14 天：唯一允许「好久没联系」的档


def silence_gap_bucket(silent_hours: float) -> str:
    """真实沉默时长 → 措辞档位。非法输入按 same_day（最保守，绝不说「好久」）。"""
    try:
        sh = float(silent_hours or 0.0)
    except (TypeError, ValueError):
        sh = 0.0
    if sh >= _LONG_ABSENCE_HOURS:
        return GAP_LONG
    if sh >= 72:
        return GAP_WEEK
    if sh >= 24:
        return GAP_FEW_DAYS
    return GAP_SAME_DAY


def silence_gap_phrase(silent_hours: float) -> str:
    """给 prompt 用的人话时长（「大约 6 小时 / 2 天」），供框定层引用事实。"""
    try:
        sh = max(0.0, float(silent_hours or 0.0))
    except (TypeError, ValueError):
        sh = 0.0
    if sh < 48:
        return f"大约 {max(1, int(round(sh)))} 小时"
    return f"大约 {int(sh // 24)} 天"


# gentle_checkin 切入点轮换池（P1，2026-07-29）：同一用户同一天取同一切入点
# （crc32(key#日期) 确定性，与 outfit/scene 同哲学），隔天自动换——从源头拉开
# 「每次都问最近怎么样」的同质化。只是参考提示，真实文案仍由 LLM 按人设发挥。
_CHECKIN_ANGLES = (
    "分享你此刻正在做的一件小事（在忙什么/刚忙完什么）",
    "聊聊窗外的天气、光线或此刻的季节感受",
    "说说你刚吃过或正想吃的东西",
    "讲一件今天路上/身边碰到的小事",
    "分享你正在听的歌或在看的剧/书",
    "提一个你明天的小计划或小期待",
    "抛一个你突然想到的小问题，想听听TA的看法",
    "轻轻问一句TA这会儿在忙什么",
)


def checkin_angle(variety_key: str, now: Optional[float] = None) -> str:
    """当天该用户的问候切入点（确定性轮换；key 为空用日期兜底仍有跨天变化）。"""
    import zlib
    ts = now if now is not None else time.time()
    day = time.strftime("%Y%m%d", time.localtime(ts))
    seed = f"{variety_key or ''}#{day}".encode("utf-8", "ignore")
    return _CHECKIN_ANGLES[zlib.crc32(seed) % len(_CHECKIN_ANGLES)]


# 各档 gentle_checkin 指令：短档明令禁止「好久没联系」，并要求带具体内容
# （分享自己此刻的小事）替代空泛的「最近怎么样」模板问候。
_CHECKIN_DIRECTIVES = {
    GAP_SAME_DAY: (
        "主动开场：你们今天早些时候才聊过，像随手想到TA那样自然搭话——"
        "分享一件你此刻正在做或刚碰到的小事，或顺口问问TA在忙什么。"
        "绝不要说「好久没联系/好久不见」（今天才聊过，说这个非常假），"
        "也不要用「最近怎么样/还好吗」这种模板问候。"
    ),
    GAP_FEW_DAYS: (
        "主动开场：有一两天没聊了，自然地打个招呼——先分享你这两天的一件"
        "具体小事，或想到TA可能在忙什么就顺口关心一句。"
        "不要说「好久没联系」（才隔一两天），避免「最近还好吗」这类空泛问候。"
    ),
    GAP_WEEK: (
        "主动开场：好几天没聊了，像朋友忽然想起TA那样自然问候——从你自己"
        "近况的一件具体小事切入，再关心TA这几天过得怎么样。"
        "别用「好久没联系」的生分口吻，也别显得刻意找话。"
    ),
    GAP_LONG: (
        "主动开场：确实很久没联系了，像久违的朋友那样温和重连——轻松问候、"
        "自然承认隔了挺久，关心对方最近过得怎么样，把话题主导权交给对方，"
        "不要强行找话题或显得刻意。"
    ),
}

# P1b：除选中事实外，额外带几条高置信事实作"背景知识"（让开场更有"真记得你"
# 的质感，但只作背景、不罗列、不连环追问——见 directive 的克制约束）。
_DEFAULT_CONTEXT_FACTS = 2


def _eligible_facts(memory_facts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """筛出可回访的高置信事实：非 stale、非 ai_inferred、有内容。"""
    out: List[Dict[str, Any]] = []
    for f in memory_facts or []:
        if not isinstance(f, dict):
            continue
        content = str(f.get("content") or "").strip()
        if not content:
            continue
        tier = str(f.get("tier") or "raw").strip().lower()
        if tier == "stale":
            continue
        # 缺省 source 视为 user_stated（兼容旧库）；ai_inferred 一律排除
        source = str(f.get("source") or "user_stated").strip().lower()
        if source == "ai_inferred":
            continue
        out.append(f)
    return out


def _fact_score(f: Dict[str, Any], now: float, prefer_category: str = "") -> tuple:
    """回访优先级：偏好类目优先 → 稳定层 → 复发多 → 越近越优先。返回可比较元组。

    ``prefer_category`` 非空时，该类目（如剧情回写的 ``story`` 共享经历）领先一档——
    让「一起经历过的事」优先被主动回访（"还记得那次星空下的约定吗"），把
    剧情→记忆→主动回访这条飞轮转起来。默认 ""（行为完全等同旧版）。
    """
    pref = (prefer_category or "").strip().lower()
    cat = str(f.get("category") or "").strip().lower()
    pref_bonus = 1 if (pref and cat == pref) else 0
    tier = str(f.get("tier") or "raw").strip().lower()
    stable_bonus = 1 if tier == "stable" else 0
    try:
        hits = int(f.get("hits") or 1)
    except (TypeError, ValueError):
        hits = 1
    last_seen = f.get("last_seen") or f.get("created_at") or 0
    try:
        last_seen = float(last_seen)
    except (TypeError, ValueError):
        last_seen = 0.0
    return (pref_bonus, stable_bonus, hits, last_seen)


def select_proactive_topic(
    memory_facts: List[Dict[str, Any]],
    *,
    silent_hours: float,
    stage: str = "",
    intimacy: float = 0.0,
    min_silent_hours: float = 24.0,
    max_context_facts: int = _DEFAULT_CONTEXT_FACTS,
    prefer_category: str = "",
    variety_key: str = "",
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """选出一个主动开场话题种子（确定性）。

    Args:
        memory_facts: 该用户长期记忆条目，每项含 ``content`` 及可选 ``source/tier/hits/
            last_seen/created_at/category``（即 episodic ``list_rows`` 行）。
        silent_hours: 距上次互动的小时数。
        stage: 关系阶段（initial/warming/intimate/steady…），用于克制修饰。
        intimacy: 亲密度 0-100（预留）。
        min_silent_hours: 低于此沉默时长不主动开场（避免打扰活跃用户）。
        prefer_category: 优先回访的记忆类目（如 ``story`` 共享经历）；空则不偏好。
        variety_key: 非空则给 gentle_checkin 附「当日切入点」轮换提示（P1 反同质化，
            同用户同天恒定、隔天自动换）；空=不附（旧行为）。
        now: 注入"现在"时间戳（测试用）。

    Returns:
        ``{mode, fact, directive, context_facts, long_absence, silent_hours}``；
        ``mode==""`` 表示不开场。``context_facts`` 是除选中事实外的其他高置信事实
        （供回复层作背景，不罗列、不追问），无则为空 list。
    """
    now = now if now is not None else time.time()
    try:
        sh = float(silent_hours or 0)
    except (TypeError, ValueError):
        sh = -1.0
    empty = {
        "mode": MODE_NONE, "fact": "", "directive": "", "context_facts": [],
        "long_absence": False, "silent_hours": round(sh, 1) if sh >= 0 else 0.0,
        "gap_bucket": "",
    }
    if sh < 0:
        return empty
    if sh < float(min_silent_hours):
        return empty  # 沉默不足，不打扰

    long_absence = sh >= _LONG_ABSENCE_HOURS
    gap_bucket = silence_gap_bucket(sh)
    eligible = _eligible_facts(memory_facts)
    if eligible:
        ranked = sorted(
            eligible, key=lambda f: _fact_score(f, now, prefer_category),
            reverse=True)
        best = ranked[0]
        # P2 事实轮换：argmax 是确定性的 → 同一用户每次回访都盯着同一条事实
        # （「上次你说在备考」问八遍）。有 variety_key 时在得分 Top-K（≤3）里按
        # crc32(key#日期) 当日恒定轮换——今天问备考、明天问旅行，都是真事实、
        # 不降置信门槛；无 variety_key = 旧行为（永远 top-1）。
        if variety_key and len(ranked) >= 2:
            import zlib
            _k = min(3, len(ranked))
            _day = time.strftime("%Y%m%d", time.localtime(now))
            _seed = f"{variety_key}#fact#{_day}".encode("utf-8", "ignore")
            best = ranked[zlib.crc32(_seed) % _k]
        fact = str(best.get("content") or "").strip()
        # P1b：除选中事实外，再挑几条高置信事实作背景（按同一优先级排序，去重）。
        context_facts: List[str] = []
        if max_context_facts > 0:
            for f in ranked:
                if f is best:
                    continue
                c = str(f.get("content") or "").strip()
                if c and c != fact and c not in context_facts:
                    context_facts.append(c)
                if len(context_facts) >= int(max_context_facts):
                    break
        if long_absence:
            directive = (
                f"主动开场：先像久违的朋友自然问候一句，再顺势提起对方之前说过的"
                f"「{fact}」，关心一下后来怎么样；别一上来就追问，给对方主导节奏。"
            )
        else:
            directive = (
                f"主动开场：自然地回到对方之前提过的「{fact}」，关心一下进展或近况，"
                f"像朋友一直惦记着——别生硬罗列、别连环追问，一句关心即可。"
            )
        if str(stage or "").strip().lower() in ("initial", "warming"):
            directive += "（关系还偏新：点到为止，别显得过分热络或越界。）"
        if gap_bucket in (GAP_SAME_DAY, GAP_FEW_DAYS):
            # 短沉默的记忆回访同样不许「久别重逢」腔（框定层也会兜，但指令层先说清）
            directive += "别用「好久没联系/好久不见」这类隔了很久的口吻。"
        return {
            "mode": MODE_FOLLOW_UP, "fact": fact, "directive": directive,
            "context_facts": context_facts,
            "long_absence": long_absence, "silent_hours": round(sh, 1),
            "gap_bucket": gap_bucket,
        }

    # 无可回访记忆 → 温和问候（措辞按真实沉默分档，短档禁「好久没联系」）
    directive = _CHECKIN_DIRECTIVES.get(gap_bucket, _CHECKIN_DIRECTIVES[GAP_WEEK])
    if variety_key:
        directive += f"（今天的切入点参考：{checkin_angle(variety_key, now)}——仅参考，说得自然就好。）"
    return {
        "mode": MODE_GENTLE_CHECKIN, "fact": "", "directive": directive,
        "context_facts": [],
        "long_absence": long_absence, "silent_hours": round(sh, 1),
        "gap_bucket": gap_bucket,
    }


def build_proactive_topic_block(
    memory_facts: List[Dict[str, Any]],
    *,
    silent_hours: float,
    stage: str = "",
    intimacy: float = 0.0,
    min_silent_hours: float = 24.0,
    prefer_category: str = "",
    now: Optional[float] = None,
) -> str:
    """组装【主动话题】prompt 块；无需开场时返回 ""（绝不抛）。"""
    try:
        sel = select_proactive_topic(
            memory_facts, silent_hours=silent_hours, stage=stage,
            intimacy=intimacy, min_silent_hours=min_silent_hours,
            prefer_category=prefer_category, now=now,
        )
    except Exception:
        return ""
    if not sel.get("mode") or not sel.get("directive"):
        return ""
    return f"【主动话题】{sel['directive']}"


__all__ = [
    "MODE_NONE", "MODE_FOLLOW_UP", "MODE_GENTLE_CHECKIN",
    "GAP_SAME_DAY", "GAP_FEW_DAYS", "GAP_WEEK", "GAP_LONG",
    "silence_gap_bucket", "silence_gap_phrase", "checkin_angle",
    "select_proactive_topic", "build_proactive_topic_block",
]
