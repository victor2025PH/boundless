"""人工「客户情绪」标注 → AI 行为的单一仲裁入口（P1-198 续，2026-08-02）。

背景：坐席在「AI 下一步」卡点「情绪低落/积极开朗」此前只写会话标签（列表徽章/
筛选），AI 拟稿、主动触达、营销目标读的全是机器自动分析（analyze_emotion →
conversation_meta.last_emotion），两套情绪系统零接线——「标了没差别」。

本模块把人工标注升格为**有时效窗的转向信号**，全链消费统一走这里，防口径分裂：

  仲裁不变量（安全设计，勿轻易放宽）：
  1. **不对称覆写**——人工「情绪低落」让全链更谨慎（拟稿放柔、主动触达 soft、
     营销目标让路、语音安抚加权）；人工「积极开朗」**只影响语气提示**，绝不解锁
     任何被机器负面/危机信号压住的推进闸门（人可以让 AI 更小心，不能让 AI 更莽）。
  2. **危机最高优先**——危机安全网（wellbeing_guard）独立运行且先于本信号，
     人工标注永远无法覆盖 severe/elevated 的处置。
  3. **标签在场校验**——坐席从标签栏删掉情绪标签 = 立即停用转向（arbitration
     列还在但 conv_tags 里没有 → inactive），杜绝「徽章没了 AI 还在悄悄软化」。
  4. **历史标注惰性**——上线前打的旧标签没有 mood_manual_ts（列为空）→ 永不
     激活转向，存量部署零行为突变；只有部署后新打的标注才生效。

数据落点：conversation_meta.mood_manual / mood_manual_ts / mood_manual_by
（store migration T-mood）；conv_tags 继续作展示/筛选投影，由
``apply_mood_tag``（唯一写入口）同步双写。

词汇表映射（各消费方词汇不同，集中钉死，配套门禁校验成员资格防漂移）：
  - GATE_EMOTION_NEG="sad"     → wellbeing_guard._NEGATIVE_EMOTIONS（主动触达闸门）
  - HINT_EMOTION_NEG="negative"→ ai_client emotion_guide + goals NEGATIVE_EMOTIONS
  - HINT_EMOTION_POS="positive"→ ai_client emotion_guide（仅语气，见不变量 1）

配置：``inbox.next_actions.mood_steering.{enabled,ttl_hours}``（默认开/24h——
gate 需要坐席对单个会话显式动作才触发，未标注的部署零行为变化，故默认开是安全的）。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

# 情绪状态标签词表（单一事实源；2026-08-14「AI 下一步」面板下线时自
# next_action_recommender 迁入——词表/互斥合并的消费方是工作链 runner 与
# 本模块仲裁链，与推荐面板无关）。两个正交维度：
#   - emotion   组：客户情绪（人工标注 → effective_mood 仲裁 → AI 语气/主动节奏/让路）
#   - attention 组：跟进状态（纯工作流维度，不进 AI 仲裁）
# 组内互斥、跨组共存；MOOD_TAGS 保持四词合集（顺序不变）供旧消费方/门禁零改动。
MOOD_TAGS_EMOTION = ("情绪低落", "积极开朗")
MOOD_TAGS_ATTENTION = ("需要关注", "进展顺利")
MOOD_TAGS = MOOD_TAGS_EMOTION + MOOD_TAGS_ATTENTION


def merge_mood_tag(existing: Optional[List[str]], tag: str) -> List[str]:
    """会话标签合并的**唯一入口**（工作链 runner / 批量打标共用）。

    旧病：互斥规则曾散落各写入点，裸 append 会让「情绪低落」「积极开朗」并存，
    「当前情绪」读数取决于数组顺序。收口为纯函数：
    - tag ∈ emotion 组 → 剔除组内旧值（情绪是单选状态）
    - tag ∈ attention 组 → 剔除组内旧值（跟进状态同理）
    - 其他标签 → 原语义（append 去重），零行为变化
    """
    tag = str(tag or "").strip()
    out = [str(t) for t in (existing or []) if str(t or "").strip()]
    if not tag:
        return out
    if tag in MOOD_TAGS_EMOTION:
        out = [t for t in out if t not in MOOD_TAGS_EMOTION]
    elif tag in MOOD_TAGS_ATTENTION:
        out = [t for t in out if t not in MOOD_TAGS_ATTENTION]
    if tag not in out:
        out.append(tag)
    return out


# 人工标注 canonical 值（与 MOOD_TAGS_EMOTION 对齐；门禁钉一致性）
MANUAL_NEG_TAG = "情绪低落"
MANUAL_POS_TAG = "积极开朗"

# 各消费方词汇映射（见模块 docstring）
GATE_EMOTION_NEG = "sad"
HINT_EMOTION_NEG = "negative"
HINT_EMOTION_POS = "positive"

DEFAULT_TTL_HOURS = 24.0

# B 线拟稿指令（agent_instruction 通道；显式指令优先，标注句仅在余量内追加）
_DIRECTIVES = {
    MANUAL_NEG_TAG: (
        "【坐席标注·客户状态】对方当前情绪低落：先共情倾听、语气放柔放慢，"
        "暂缓目标推进与玩笑调侃，不追问隐私。"
    ),
    MANUAL_POS_TAG: (
        "【坐席标注·客户状态】对方当前心情不错：语气可以自然轻快些，"
        "顺势把话题聊深，但别过度亢奋。"
    ),
}

# 进程级观测计数（风格对齐 frontend_error_stats：轻量单例，重启即清；
# DB 口径的长期观测属下一阶段）
_STATS: Dict[str, Dict[str, int]] = {"marks": {}, "consumed": {}}

# (machine→manual) 配对计数上限（防标签面爆炸撑爆进程内存）
_PAIRS_CAP = 40


def resolve_mood_steering_cfg(cfg_root: Any) -> Dict[str, Any]:
    """读 ``inbox.next_actions.mood_steering``；任何异常回默认（开 / 24h）。"""
    enabled, ttl = True, DEFAULT_TTL_HOURS
    try:
        if isinstance(cfg_root, dict):
            ms = (((cfg_root.get("inbox") or {}).get("next_actions") or {})
                  .get("mood_steering") or {})
            if isinstance(ms, dict):
                enabled = bool(ms.get("enabled", True))
                ttl = float(ms.get("ttl_hours", DEFAULT_TTL_HOURS))
            elif isinstance(ms, bool):
                enabled = ms
    except Exception:
        enabled, ttl = True, DEFAULT_TTL_HOURS
    if not (ttl > 0):
        ttl = DEFAULT_TTL_HOURS
    return {"enabled": enabled, "ttl_hours": ttl}


def _tags_of(meta: Optional[Dict[str, Any]]) -> List[str]:
    """conv_tags 防御式解析：get_conv_meta 原样回 JSON 串，测试/调用方可能给 list。"""
    raw = (meta or {}).get("conv_tags")
    if isinstance(raw, list):
        return [str(t) for t in raw]
    if isinstance(raw, str) and raw.strip():
        try:
            val = json.loads(raw)
            return [str(t) for t in val] if isinstance(val, list) else []
        except Exception:
            return []
    return []


def manual_mood_state(
    meta: Optional[Dict[str, Any]], *, now: float, ttl_hours: float,
) -> Dict[str, Any]:
    """人工情绪标注状态（纯函数）。

    active 判据（全部满足）：mood_manual ∈ emotion 组 且 mood_manual_ts>0
    且 未过 TTL 且 该标签仍在 conv_tags（在场校验，见模块不变量 3/4）。
    """
    meta = meta or {}
    tag = str(meta.get("mood_manual") or "").strip()
    try:
        ts = float(meta.get("mood_manual_ts") or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    by = str(meta.get("mood_manual_by") or "")
    age_hours = max(0.0, (float(now) - ts) / 3600.0) if ts > 0 else -1.0
    active = bool(
        tag in MOOD_TAGS_EMOTION
        and ts > 0
        and ttl_hours > 0
        and age_hours <= float(ttl_hours)
        and tag in _tags_of(meta)
    )
    return {"tag": tag, "ts": ts, "by": by,
            "age_hours": age_hours, "active": active}


def manual_negative_active(
    meta: Optional[Dict[str, Any]], *, now: float, ttl_hours: float,
) -> bool:
    """人工「情绪低落」是否在时效窗内生效（goals 让路 / 语音安抚共用判据）。"""
    st = manual_mood_state(meta, now=now, ttl_hours=ttl_hours)
    return bool(st["active"] and st["tag"] == MANUAL_NEG_TAG)


def tone_hint_override(
    meta: Optional[Dict[str, Any]], *, now: float, ttl_hours: float,
) -> str:
    """语气 hint 覆写（A 线 user_emotion_hint 词汇）：低落→negative，开朗→positive。

    这是唯一允许「积极开朗」生效的通道（只调语气，不解锁闸门，见不变量 1）。
    """
    st = manual_mood_state(meta, now=now, ttl_hours=ttl_hours)
    if not st["active"]:
        return ""
    if st["tag"] == MANUAL_NEG_TAG:
        return HINT_EMOTION_NEG
    if st["tag"] == MANUAL_POS_TAG:
        return HINT_EMOTION_POS
    return ""


def gate_emotion_override(
    meta: Optional[Dict[str, Any]], *, now: float, ttl_hours: float,
) -> str:
    """主动触达情绪闸门覆写（wellbeing_guard 词汇）：**仅负向**。

    人工「低落」→ "sad"（gate 判 soft：抑制剧情邀约、留温和问候）；
    人工「开朗」→ ""（不放松机器信号——人不能让 AI 更莽）。
    """
    if manual_negative_active(meta, now=now, ttl_hours=ttl_hours):
        return GATE_EMOTION_NEG
    return ""


def agent_mood_directive(
    meta: Optional[Dict[str, Any]], *, now: float, ttl_hours: float,
) -> str:
    """B 线拟稿指令句（走 agent_instruction 通道；prompt 语言=系统中文，与
    既有指令块一致，回复语言由语言守卫独立决定）。"""
    st = manual_mood_state(meta, now=now, ttl_hours=ttl_hours)
    if not st["active"]:
        return ""
    return _DIRECTIVES.get(st["tag"], "")


def merge_agent_instruction(
    instruction: Any, mood_directive: str, *, cap: int = 400,
) -> str:
    """坐席显式指令与情绪标注句合并：**显式指令优先**（每稿一次的明确意图 >
    常驻标注），标注句仅在 cap 余量内追加，放不下就丢标注句、绝不截显式指令。"""
    instr = str(instruction or "").strip()
    d = str(mood_directive or "").strip()
    if not d:
        return instr[:cap]
    if not instr:
        return d[:cap]
    if len(instr) + 1 + len(d) <= cap:
        return instr + "\n" + d
    return instr[:cap]


def apply_mood_tag(
    store: Any, conversation_id: str, tag: str, *,
    by: str = "", now: Optional[float] = None,
) -> Dict[str, Any]:
    """打标签的**唯一 IO 入口**（execute-action / 工作链 / 批量共用）。

    - conv_tags 经 ``merge_mood_tag`` 组内互斥合并（展示/筛选投影）；
    - tag ∈ emotion 组 → 同步落 arbitration 三列（mood_manual/ts/by），
      列写失败不影响标签（best-effort：标签是既有语义，转向是增量能力）。
    """
    cid = str(conversation_id or "").strip()
    tag = str(tag or "").strip()
    out: Dict[str, Any] = {"tags": [], "mood_manual": {}}
    if not cid or not tag or store is None:
        return out
    ts = float(now if now is not None else time.time())
    existing = list(store.get_conv_tags(cid) or [])
    merged = merge_mood_tag(existing, tag)
    if merged != existing:
        store.set_conv_tags(cid, merged)
    out["tags"] = merged
    if tag in MOOD_TAGS_EMOTION:
        try:
            store.set_manual_mood(cid, tag, by=by, ts=ts)
            out["mood_manual"] = {"tag": tag, "ts": ts, "by": by}
            record_mood_mark(tag)
        except Exception:
            pass
        # 确认/纠正打点：标注瞬间机器怎么判的（best-effort，绝不影响打标）
        try:
            _meta = store.get_conv_meta(cid) or {}
            record_mood_agreement(tag, str(_meta.get("last_emotion") or ""))
        except Exception:
            pass
    return out


# ── 进程级观测 ──────────────────────────────────────────────────────────────

def record_mood_mark(tag: str) -> None:
    try:
        key = str(tag or "?")
        _STATS["marks"][key] = int(_STATS["marks"].get(key, 0)) + 1
    except Exception:
        pass


def record_mood_consume(channel: str) -> None:
    """某条消费链真的用上了人工标注（channel: draft_directive / tone_a /
    proactive_gate / goal_hold / voice）。"""
    try:
        key = str(channel or "?")
        _STATS["consumed"][key] = int(_STATS["consumed"].get(key, 0)) + 1
    except Exception:
        pass


def _machine_polarity(label: str) -> str:
    """机器情绪标签 → 极性（neg / nonneg / unknown）。负面词表与主动闸门同源
    （wellbeing_guard._NEGATIVE_EMOTIONS）；导入失败/空标签 → unknown（不计入
    agree/disagree，宁可少记不错记）。"""
    lab = str(label or "").strip().lower()
    if not lab:
        return "unknown"
    try:
        from src.utils.wellbeing_guard import _NEGATIVE_EMOTIONS as _NEG
    except Exception:
        return "unknown"
    return "neg" if lab in _NEG else "nonneg"


def record_mood_agreement(manual_tag: str, machine_label: str) -> None:
    """人工标注 vs 机器判定 一致性打点（确认/纠正闭环第一步，P2）。

    agree 高＝机器情绪分类在真实客群上可信；disagree 高＝坐席在系统性纠正机器
    ——(machine→manual) 配对计数就是将来校准 ``analyze_emotion`` 词表/阈值的
    原料（先进程级看板读数，攒出形状再决定要不要落库成金标集）。"""
    try:
        pol = _machine_polarity(machine_label)
        if manual_tag == MANUAL_NEG_TAG:
            key = ("unknown" if pol == "unknown"
                   else "agree" if pol == "neg" else "disagree")
        elif manual_tag == MANUAL_POS_TAG:
            key = ("unknown" if pol == "unknown"
                   else "agree" if pol == "nonneg" else "disagree")
        else:
            return
        agg = _STATS.setdefault("agreement", {})
        agg[key] = int(agg.get(key, 0)) + 1
        pairs = _STATS.setdefault("pairs", {})
        pkey = f"{str(machine_label or '').strip() or '-'}→{manual_tag}"
        if pkey in pairs or len(pairs) < _PAIRS_CAP:
            pairs[pkey] = int(pairs.get(pkey, 0)) + 1
    except Exception:
        pass


def mood_steering_snapshot() -> Dict[str, Any]:
    return {
        "marks": dict(_STATS["marks"]),
        "consumed": dict(_STATS["consumed"]),
        "agreement": dict(_STATS.get("agreement", {})),
        "pairs": dict(_STATS.get("pairs", {})),
    }


__all__ = [
    "MOOD_TAGS_EMOTION", "MOOD_TAGS_ATTENTION", "MOOD_TAGS", "merge_mood_tag",
    "MANUAL_NEG_TAG", "MANUAL_POS_TAG",
    "GATE_EMOTION_NEG", "HINT_EMOTION_NEG", "HINT_EMOTION_POS",
    "resolve_mood_steering_cfg", "manual_mood_state", "manual_negative_active",
    "tone_hint_override", "gate_emotion_override",
    "agent_mood_directive", "merge_agent_instruction", "apply_mood_tag",
    "record_mood_mark", "record_mood_consume", "record_mood_agreement",
    "mood_steering_snapshot",
]
