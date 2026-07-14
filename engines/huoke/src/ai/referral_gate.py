# -*- coding: utf-8 -*-
"""引流时机决策闸 (P5 — B 机统一的 wa_referral 决策层)。

把 P3 的 should_block_referral + P4 的 intent-based 硬触发 + ChatBrain 的
ref_score 判断整合到一个可独立测试的模块。_ai_reply_and_send 只调一次
``should_refer`` 拿 GateDecision,再也不用关心分层决策的细节。

决策分 3 层 (按优先级高→低):

  1. **hard_block** (安全优先 — 下列任一命中就不引流):
     * 没有引流目标 (``has_contact=False``)
     * 历史画像显示上次引流对方未回 (P3 ``should_block_referral``),
       且本轮 intent 不是 ``referral_ask`` (对方主动要就放行)
     * 最近 ``refer_cooldown_hours`` 内已引流过,且 intent 不是 ``referral_ask``

  2. **hard_allow** (意图层硬信号 — 对方明确要):
     * ``intent == 'referral_ask'`` → 对方主动问联系方式,必须回
     * ``intent == 'buying'`` → 强购买信号,该引流就引

  3. **soft score** (累加打分,阈值 ``score_threshold`` 判通过):
     * total_turns ≥ min_turns (累积信任度): +1
     * intent == 'interest' (对方表现兴趣): +1
     * ref_score > 0.5 (ChatBrain LLM 判断值得引流): +1
     * lead_score ≥ min_lead_score (A 打分高质量素人): +1
     * peer_reply_count ≥ 2 (对方已多次回复,对话活跃): +1
     分数 ≥ ``score_threshold`` (默认 3) → refer=True,否则 False

**Why 分层而不是纯打分**: hard_block 和 hard_allow 是 "必须遵守的规则",
soft score 是 "综合判断"。把 referral_ask 或 no contact 的 edge case 混进
打分层,边界表达不清晰,也让 gate 不可解释。

**Why intent=referral_ask 覆盖 cooldown**: 对方明确问"你 LINE 几号"时,
用冷却门硬怼回去会让对话出戏 (像真人会说 "我刚才发过了哦")。改用应答 +
重发 ID 是更自然的行为。buying 不覆盖 cooldown: 刚引流对方未回又说要买,
继续引可能是 spam 感知,让 LLM 走 reply 路径回答价格更稳。
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 默认配置
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG: Dict[str, Any] = {
    "min_turns": 3,                # soft: 累积对话轮数门槛
    "min_lead_score": 0,           # soft: A 打分门槛; **0 = 禁用此信号**
                                   # (A 机 review 反馈: fb_lead_scorer_v2 融合分
                                   # 均值约 45-55,60 会筛掉一半早期 lead。等积累
                                   # 2 周 ≥500 leads 数据后查 P75 分位再调)
    "min_peer_replies": 2,         # soft: 对方回复次数门槛
    "score_threshold": 3,          # soft: 5 档评分里满足 3 档即通过
    "refer_cooldown_hours": 1,     # hard: 同 peer 再次引流的冷却期
    "ref_score_threshold": 0.5,    # soft: ChatBrain referral_score 门槛
    "rejection_cooldown_days": 0,  # hard: 客户明确拒绝后冷却天数 (0 = 禁用)
    "min_emotion_score": 0.0,      # soft: L3 情感综合分门槛 (0 = 禁用)
                                   # jp_female_midlife yaml 里设 0.5
                                   # (聊得有温度才允许引)
    # Phase-9: LLM 洞察驱动的智能引流
    "max_frustration": 0.0,        # hard_block: frustration > 此值时不引流 (referral_ask 除外). 0 = 禁用
    "early_refer_readiness": 0.0,  # hard_allow: readiness ≥ 此值且 turns ≥ early_refer_min_turns → 早引流. 0 = 禁用
    "early_refer_min_turns": 5,
    "delay_refer_readiness": 0.0,  # hard_block: readiness ≤ 此值且 turns ≤ delay_refer_max_turns → 延后. 0 = 禁用
    "delay_refer_max_turns": 10,
}


# ─────────────────────────────────────────────────────────────────────────────
# Persona 配置加载 (从 config/referral_strategies.yaml)
# ─────────────────────────────────────────────────────────────────────────────

_PERSONA_CFG_CACHE: Optional[Dict[str, Dict[str, Any]]] = None
_PERSONA_CFG_CACHE_MTIME: float = 0.0
_PERSONA_CFG_LOCK_PATH = "config/referral_strategies.yaml"


def _load_persona_strategies_yaml() -> Dict[str, Dict[str, Any]]:
    """读 config/referral_strategies.yaml, 失败返 {}.

    Phase-11 hot reload: 按文件 mtime 检查, 变了就重读.
    运营改 yaml 后无需重启 30 worker.
    """
    global _PERSONA_CFG_CACHE, _PERSONA_CFG_CACHE_MTIME
    try:
        import yaml
        from pathlib import Path
        path = Path(_PERSONA_CFG_LOCK_PATH)
        if not path.exists():
            _PERSONA_CFG_CACHE = {}
            _PERSONA_CFG_CACHE_MTIME = 0.0
            return _PERSONA_CFG_CACHE
        mtime = path.stat().st_mtime
        # cache 命中且 mtime 没变 → 直接返
        if _PERSONA_CFG_CACHE is not None and mtime == _PERSONA_CFG_CACHE_MTIME:
            return _PERSONA_CFG_CACHE
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        _PERSONA_CFG_CACHE = data if isinstance(data, dict) else {}
        _PERSONA_CFG_CACHE_MTIME = mtime
        if mtime > 0:
            logger.info("[referral_gate] persona strategies yaml reloaded (mtime=%.0f)",
                        mtime)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[referral_gate] load persona strategies failed: %s", exc)
        if _PERSONA_CFG_CACHE is None:
            _PERSONA_CFG_CACHE = {}
    return _PERSONA_CFG_CACHE


def reload_persona_strategies_for_tests() -> None:
    """仅测试用. 清掉 cache, 让下次 load 重新读 yaml."""
    global _PERSONA_CFG_CACHE
    _PERSONA_CFG_CACHE = None


def load_persona_config(persona_key: Optional[str]) -> Dict[str, Any]:
    """获取 persona 特定 config (DEFAULT 覆盖 < yaml.default 覆盖 < yaml.<persona>).

    persona_key=None 或未在 yaml 配置: 返回 DEFAULT_CONFIG 副本.
    """
    cfg = dict(DEFAULT_CONFIG)
    yaml_data = _load_persona_strategies_yaml()
    if not yaml_data:
        return cfg
    # yaml.default 覆盖 module DEFAULT
    if isinstance(yaml_data.get("default"), dict):
        cfg.update(yaml_data["default"])
    # persona 段覆盖 default
    if persona_key and isinstance(yaml_data.get(persona_key), dict):
        cfg.update(yaml_data[persona_key])
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# 关键词命中 (trigger / rejection)
# ─────────────────────────────────────────────────────────────────────────────

_TRIGGER_KW_CACHE: Optional[Dict[str, List[str]]] = None
_TRIGGER_KW_CACHE_MTIME: float = 0.0
_TRIGGER_KW_PATH = "config/referral_trigger_keywords.yaml"


def _load_trigger_keywords() -> Dict[str, List[str]]:
    """读 config/referral_trigger_keywords.yaml. 失败返 {}.

    Phase-11 hot reload 同 _load_persona_strategies_yaml.
    """
    global _TRIGGER_KW_CACHE, _TRIGGER_KW_CACHE_MTIME
    try:
        import yaml
        from pathlib import Path
        path = Path(_TRIGGER_KW_PATH)
        if not path.exists():
            _TRIGGER_KW_CACHE = {}
            _TRIGGER_KW_CACHE_MTIME = 0.0
            return _TRIGGER_KW_CACHE
        mtime = path.stat().st_mtime
        if _TRIGGER_KW_CACHE is not None and mtime == _TRIGGER_KW_CACHE_MTIME:
            return _TRIGGER_KW_CACHE
        with path.open(encoding="utf-8") as f:
            _TRIGGER_KW_CACHE = yaml.safe_load(f) or {}
        _TRIGGER_KW_CACHE_MTIME = mtime
        if mtime > 0:
            logger.info("[referral_gate] trigger keywords yaml reloaded (mtime=%.0f)",
                        mtime)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[referral_gate] load trigger keywords failed: %s", exc)
        if _TRIGGER_KW_CACHE is None:
            _TRIGGER_KW_CACHE = {}
    return _TRIGGER_KW_CACHE


def reload_trigger_keywords_for_tests() -> None:
    global _TRIGGER_KW_CACHE
    _TRIGGER_KW_CACHE = None


def hits_trigger_keyword(text: str, lang: str = "ja") -> bool:
    """text 是否命中"客户主动要 LINE"关键词 (按 lang 字段查 yaml)."""
    if not text:
        return False
    kw = _load_trigger_keywords()
    candidates = kw.get(lang) or []
    if not candidates:
        return False
    t = text.lower()
    for k in candidates:
        if k and k.lower() in t:
            return True
    return False


def hits_rejection_keyword(text: str, lang: str = "ja") -> bool:
    """text 是否命中"客户拒绝引流"关键词 (查 yaml.rejection_<lang>)."""
    if not text:
        return False
    kw = _load_trigger_keywords()
    candidates = kw.get(f"rejection_{lang}") or []
    if not candidates:
        return False
    t = text.lower()
    for k in candidates:
        if k and k.lower() in t:
            return True
    return False


@dataclass
class GateDecision:
    refer: bool
    level: str              # "hard_block" | "hard_allow" | "soft_pass" | "soft_fail"
    score: int = 0          # soft gate 累计分 (level="soft_*" 时有意义)
    threshold: int = 0      # 阈值 (用于日志/telemetry)
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "refer": self.refer,
            "level": self.level,
            "score": self.score,
            "threshold": self.threshold,
            "reasons": list(self.reasons),
        }


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────

def should_refer(*,
                 intent: str = "smalltalk",
                 ref_score: float = 0.0,
                 memory_ctx: Optional[Dict[str, Any]] = None,
                 lead_score: int = 0,
                 has_contact: bool = False,
                 config: Optional[Dict[str, Any]] = None,
                 now: Optional[_dt.datetime] = None,
                 incoming_text: str = "",
                 persona_key: Optional[str] = None,
                 emotion_overall: Optional[float] = None,
                 emotion_frustration: Optional[float] = None,
                 conversion_readiness: Optional[float] = None) -> GateDecision:
    """返回引流闸决策 — graceful, 永不抛。

    Args:
        intent: 来自 chat_intent.classify_intent 的 intent tag
        ref_score: ChatBrain 生成 reply 时给的 referral_score (0.0-1.0)
        memory_ctx: 来自 chat_memory.build_context_block 的记忆块,包含:
            - should_block_referral: 上次引流未回的标记
            - referral_rejected_at: 客户上次明确拒绝引流的 ISO 时间戳 (可选)
            - profile: { total_turns, peer_reply_count, last_referral_at, ... }
        lead_score: A 机 fb_lead_scorer_v2 落库的 leads.score (0-100)
        has_contact: 是否配置了 referral_contact (WhatsApp/LINE ID)
        config: 覆盖 DEFAULT_CONFIG 部分字段,未指定字段取默认
        now: 注入时钟用于测试 cooldown,默认 utcnow
        incoming_text: 当前入站消息原文, 用于 trigger / rejection 关键词命中检查
        persona_key: 当前 persona (e.g. "jp_female_midlife"), 决定 trigger keyword
            的语言 + cooldown 天数等. 未传则按 DEFAULT_CONFIG.

    Returns:
        GateDecision,调用方 if .refer: decision='wa_referral' else 'reply'
    """
    # 优先用 persona config (含 yaml.default + yaml.<persona>), 然后 caller config 覆盖
    cfg = load_persona_config(persona_key)
    if config:
        cfg.update(config)
    memory_ctx = memory_ctx or {}
    profile = memory_ctx.get("profile") or {}
    now = now or _dt.datetime.utcnow()
    trigger_lang = cfg.get("trigger_keywords_lang") or "ja"

    reasons: List[str] = []

    # ── 层 0: 关键词命中 (拒绝词优先于触发词) ─────────────────────────
    if incoming_text:
        if hits_rejection_keyword(incoming_text, lang=trigger_lang):
            reasons.append(
                f"客户拒绝引流关键词命中 (lang={trigger_lang}, "
                f"会触发 {cfg.get('rejection_cooldown_days')} 天冷却)"
            )
            return GateDecision(refer=False, level="hard_block",
                                reasons=reasons)
        if hits_trigger_keyword(incoming_text, lang=trigger_lang):
            reasons.append(
                f"客户主动要 LINE 关键词命中 (lang={trigger_lang})"
            )
            if has_contact:
                return GateDecision(refer=True, level="hard_allow",
                                    reasons=reasons)
            # 没 contact 还是不能引, 落到正常 has_contact 检查

    # ── 层 1: hard_block ────────────────────────────────────────────────
    if not has_contact:
        reasons.append("无引流目标(has_contact=False)")
        return GateDecision(refer=False, level="hard_block",
                            reasons=reasons)

    # 客户拒绝冷却 (rejection_cooldown_days) — 完全 hard_block, 不放行 referral_ask
    rejection_days = int(cfg.get("rejection_cooldown_days", 0))
    if rejection_days > 0:
        rejected_at_iso = memory_ctx.get("referral_rejected_at") or \
            profile.get("referral_rejected_at") or ""
        if rejected_at_iso:
            rejected_dt = _parse_iso(rejected_at_iso)
            if rejected_dt is not None:
                delta_sec = (now - rejected_dt).total_seconds()
                if 0 <= delta_sec < rejection_days * 86400:
                    days_left = (rejection_days * 86400 - delta_sec) / 86400.0
                    reasons.append(
                        f"客户拒绝冷却中 (剩 {days_left:.1f} 天 / "
                        f"配置 {rejection_days} 天)"
                    )
                    return GateDecision(refer=False, level="hard_block",
                                        reasons=reasons)

    # should_block_referral (上次引流对方未回) — referral_ask 除外
    if memory_ctx.get("should_block_referral"):
        if intent != "referral_ask":
            reasons.append("上次引流对方未回复(should_block_referral=True)")
            return GateDecision(refer=False, level="hard_block",
                                reasons=reasons)
        reasons.append("should_block_referral 但 intent=referral_ask,放行")

    # cooldown — referral_ask 除外
    cooldown_hours = int(cfg.get("refer_cooldown_hours", 1))
    if cooldown_hours > 0 and intent != "referral_ask":
        last_ref_iso = profile.get("last_referral_at") or ""
        if last_ref_iso:
            last_ref_dt = _parse_iso(last_ref_iso)
            if last_ref_dt is not None:
                delta = (now - last_ref_dt).total_seconds()
                if 0 <= delta < cooldown_hours * 3600:
                    reasons.append(
                        f"冷却期未过({delta/60:.0f}min < {cooldown_hours}h)"
                    )
                    return GateDecision(refer=False, level="hard_block",
                                        reasons=reasons)

    # ── 层 2: hard_allow ────────────────────────────────────────────────
    if intent == "referral_ask":
        reasons.append("intent=referral_ask (对方主动要联系方式)")
        return GateDecision(refer=True, level="hard_allow",
                            reasons=reasons)
    if intent == "buying":
        reasons.append("intent=buying (强购买信号)")
        return GateDecision(refer=True, level="hard_allow",
                            reasons=reasons)

    # ── 层 2.5: Phase-9 LLM 洞察智能层 ────────────────────────────────────
    # 顺序: frustration 优先 (拒绝信号) > 早引流 (高 readiness) > 延后引流 (低 readiness)
    _max_frust = float(cfg.get("max_frustration", 0.0) or 0.0)
    if _max_frust > 0 and emotion_frustration is not None:
        try:
            f = float(emotion_frustration)
            if f > _max_frust:
                reasons.append(
                    f"frustration={f:.2f} > {_max_frust:.2f} (客户烦躁, 暂不引流)"
                )
                return GateDecision(refer=False, level="hard_block",
                                    reasons=reasons)
        except (TypeError, ValueError):
            pass

    _total_turns_seen = int(profile.get("total_turns", 0) or 0)
    _early = float(cfg.get("early_refer_readiness", 0.0) or 0.0)
    _early_min_turns = int(cfg.get("early_refer_min_turns", 5))
    if _early > 0 and conversion_readiness is not None:
        try:
            r = float(conversion_readiness)
            if r >= _early and _total_turns_seen >= _early_min_turns:
                reasons.append(
                    f"readiness={r:.2f} ≥ {_early:.2f} & turns={_total_turns_seen} ≥ "
                    f"{_early_min_turns} (高意向早引流)"
                )
                return GateDecision(refer=True, level="hard_allow",
                                    reasons=reasons)
        except (TypeError, ValueError):
            pass

    _delay = float(cfg.get("delay_refer_readiness", 0.0) or 0.0)
    _delay_max_turns = int(cfg.get("delay_refer_max_turns", 10))
    if _delay > 0 and conversion_readiness is not None:
        try:
            r = float(conversion_readiness)
            if r <= _delay and _total_turns_seen <= _delay_max_turns:
                reasons.append(
                    f"readiness={r:.2f} ≤ {_delay:.2f} & turns={_total_turns_seen} ≤ "
                    f"{_delay_max_turns} (低意向延后引流)"
                )
                return GateDecision(refer=False, level="hard_block",
                                    reasons=reasons)
        except (TypeError, ValueError):
            pass

    # ── 层 3: soft score ────────────────────────────────────────────────
    score = 0
    total_turns = int(profile.get("total_turns", 0) or 0)
    if total_turns >= int(cfg["min_turns"]):
        score += 1
        reasons.append(f"total_turns={total_turns} ≥ {cfg['min_turns']}")

    if intent == "interest":
        score += 1
        reasons.append("intent=interest")

    try:
        if float(ref_score) > float(cfg["ref_score_threshold"]):
            score += 1
            reasons.append(
                f"ref_score={float(ref_score):.2f} > "
                f"{float(cfg['ref_score_threshold']):.2f}"
            )
    except (TypeError, ValueError):
        pass

    # min_lead_score=0 禁用此信号,避免 "lead_score >= 0" 总是 True 给 +1
    _min_ls = int(cfg["min_lead_score"])
    if _min_ls > 0 and int(lead_score) >= _min_ls:
        score += 1
        reasons.append(f"lead_score={lead_score} ≥ {_min_ls}")

    # PR-5: L3 情感综合分门槛. min_emotion_score=0 时禁用 (兼容现有).
    # jp_female_midlife yaml 设 0.5 → 聊得有温度才允许引.
    _min_emo = float(cfg.get("min_emotion_score", 0.0) or 0.0)
    if _min_emo > 0 and emotion_overall is not None:
        try:
            if float(emotion_overall) >= _min_emo:
                score += 1
                reasons.append(
                    f"emotion_overall={float(emotion_overall):.2f} ≥ {_min_emo:.2f}"
                )
            else:
                # emotion 不达标视为强 negative — 不计 +1, 但 reason 记录
                reasons.append(
                    f"emotion_overall={float(emotion_overall):.2f} < {_min_emo:.2f} "
                    f"(温度不够, 不该引)"
                )
        except (TypeError, ValueError):
            pass

    peer_replies = int(profile.get("peer_reply_count", 0) or 0)
    if peer_replies >= int(cfg["min_peer_replies"]):
        score += 1
        reasons.append(
            f"peer_reply_count={peer_replies} ≥ {cfg['min_peer_replies']}"
        )

    threshold = int(cfg["score_threshold"])
    passed = score >= threshold
    return GateDecision(
        refer=passed,
        level="soft_pass" if passed else "soft_fail",
        score=score,
        threshold=threshold,
        reasons=reasons,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_iso(s: str) -> Optional[_dt.datetime]:
    """兼容 'YYYY-MM-DDTHH:MM:SSZ' 和 'YYYY-MM-DD HH:MM:SS' 两种格式。"""
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return _dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    # 尝试 fromisoformat (允许带时区)
    try:
        return _dt.datetime.fromisoformat(s.rstrip("Z"))
    except Exception:
        return None
