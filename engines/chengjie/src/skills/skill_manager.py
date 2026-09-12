"""
# Skill管理�?管理和执行Skill工作流，复用Camille的意图识���回�生成逻辑
"""

import asyncio
import json
import logging
import os
import time
import random
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Any, Optional, List, Callable, Tuple
import re
import hashlib
import copy
import uuid


def _safe_int_chat_id(v: Any) -> int:
    """Convert any chat_id to int safely.
    Telegram 使用数字 ID；WA/LINE/Messenger 使用字符串 chat_key。
    非数字值通过 MD5 派生稳定 32 bit int，保持每个账号唯一性。
    """
    try:
        return int(v)
    except (TypeError, ValueError):
        return int(hashlib.md5(str(v).encode()).hexdigest()[:8], 16)


from src.utils.audit_store import AuditStore
from src.utils.domain_policy import effective_domain_name, payment_plugin_enabled
from src.utils.channel_status_format import (
    DISABLED_STATUSES as _CHANNEL_DISABLED_STATUSES,
    format_live_channel_status_text,
    is_channel_disabled,
)
from src.utils.greeting_lexicon import (
    has_request_context, is_greeting_message, merge_greeting_substrings,
)
from src.utils.logger import LoggerMixin
from src.ai.ai_client import AIClient
from src.skills.base import Skill

# 通道成功率多轮：短追问（? / 正常吗 / 波动）不应再整段复述 JC/EP
_CHANNEL_FAMILY_FOR_FOLLOWUP = frozenset({"channel_info", "status_check"})


def _business_domain_label() -> str:
    """启动日志用的业务域标签（P-4 #254）：companion → 陪伴 / sales → 销售；读不到给 '?'。
    域包名（conversion）与业务域（陪伴 / 销售）是两个维度，日志只打前者会误导排障。"""
    try:
        from src.utils.business_domain import active_business_domain
        bd = str(active_business_domain() or "").strip().lower()
    except Exception:
        bd = ""
    return {"companion": "companion/陪伴", "sales": "sales/销售"}.get(bd, bd or "?")


def _last_reply_looks_like_channel_summary(reply: str) -> bool:
    r = (reply or "").strip()
    if len(r) < 18:
        return False
    has_metric = "%" in r or "成功率" in r
    rl = r.lower()
    has_ch = any(
        x in r
        for x in ("JC", "EP", "通道", "Jazz", "Easypaisa", "Pay")
    ) or "jazzcash" in rl or "easypaisa" in rl
    return bool(has_metric and has_ch)


# 仅含通道代号（拉丁字母）时，语言检测易误判为英文；用于继承上句/会话语言
_CHANNEL_AMBIGUOUS_TOKENS = frozenset({
    "ep", "jc", "jp", "jazz", "easypaisa", "easypay", "jazzcash",
})


def _is_ambiguous_channel_token_message(s: str) -> bool:
    """整条消息只有通道名缩写/别名（可多个、可带标点），无自然语言内容。"""
    t = (s or "").strip()
    if not t or len(t) > 48:
        return False
    t = re.sub(r"[?？!！.。,，、]+$", "", t)
    parts = re.split(r"[\s,，/&+]+", t)
    parts = [p for p in parts if p]
    if not parts:
        return False
    for p in parts:
        if p.lower().rstrip("?？") not in _CHANNEL_AMBIGUOUS_TOKENS:
            return False
    return True


# 纯语气词/填充音：不算「通道短追问」，避免「啊」「嗯」继承 channel_info 后整段复述
_INTERJECTION_ONLY_CHARS = frozenset(
    "啊嗯哦噢哈唉额诶哎呀吧呢嘛哼啧哟喽咯哇哒咯呐咯"
)


def _is_meaningless_interjection_only(text: str) -> bool:
    """仅语气词、标点装饰、无业务字；有疑问号/数字/字母则不视为无意义。"""
    t = (text or "").strip()
    if not t:
        return True
    if "?" in t or "？" in t:
        return False
    if any(ch.isdigit() for ch in t):
        return False
    if re.search(r"[a-zA-Z]", t):
        return False
    core = re.sub(r"[\s—－\-~～·…。，、!！]+", "", t)
    if not core:
        return True
    if len(core) > 8:
        return False
    for ch in core:
        if ch not in _INTERJECTION_ONLY_CHARS:
            return False
    return True


_EXPLICIT_QUERY_KW = ("成功率", "额度", "限额", "费率", "手续费", "代收", "代付")


def _is_channel_short_followup(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _is_meaningless_interjection_only(t):
        return False
    if any(k in t for k in _EXPLICIT_QUERY_KW):
        return False
    if len(t) <= 10:
        return True
    tl = t.lower()
    if len(t) <= 22:
        short_kw = (
            "正常吗", "波动", "稳定吗", "能跑吗", "还行吗", "可以吗",
            "稳吗", "行吗", "好吗", "怎么样", "如何", "大吗", "厉害吗",
            "normal", "stable", "working", "ok?", "fine?", "good?",
            "available", "active", "running", "issue", "problem",
        )
        if any(k in tl for k in short_kw):
            return True
    if len(t) <= 14 and any(k in tl for k in ("波动", "通道", "正常", "channel", "status")):
        return True
    return False

# �€�€ 查�向量 LRU 缓存 �€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€
# key = 归一化查询文����?200 字�），value = embedding vector
# 使用 OrderedDict 实现 O(1) LRU 逻辑（Python 3.7+ 保序�?
_EMBED_CACHE: "OrderedDict[str, List[float]]" = OrderedDict()
_EMBED_CACHE_MAX = 500  # �€多缓�?500 条查询向量（�?3MB�?
_BM25_STRONG_THRESHOLD = 0.30  # BM25 分数 >= 此�€��为强命中，跳过向量化

# �€�€ Embedding API 用量统�（模块级，session 内累计）�€�€�€�€�€�€�€�€
_EMBED_STATS: dict = {
    "api_calls":  0,   # 实际调用 API 次数
    "cache_hits": 0,   # 命中缓存次数
    "kb_queries": 0,   # 总 KB 查询次数
    "kb_hits":    0,   # KB 命中次数
    "session_start": time.time(),
}

# episodic backfill 日预算（UTC 日期；按本次参与嵌入的行数累加）
_EPISODIC_BACKFILL_BUDGET_DAY: Optional[str] = None
_EPISODIC_BACKFILL_BUDGET_USED: int = 0


def _episodic_backfill_charge_budget(n: int, mvec: Dict[str, Any]) -> None:
    """补全任务在调用 embed 后按行数计入日预算（需与 daily_embed_budget 配置一致）。"""
    global _EPISODIC_BACKFILL_BUDGET_DAY, _EPISODIC_BACKFILL_BUDGET_USED
    if n <= 0:
        return
    bud = (mvec.get("daily_embed_budget") or {})
    if not bud.get("enabled", False):
        return
    day = time.strftime("%Y-%m-%d", time.gmtime())
    if _EPISODIC_BACKFILL_BUDGET_DAY != day:
        _EPISODIC_BACKFILL_BUDGET_DAY = day
        _EPISODIC_BACKFILL_BUDGET_USED = 0
    _EPISODIC_BACKFILL_BUDGET_USED += n


# 陪伴/闲聊类意图族（与 P0-G 的 _INTENT_FAMILIES["chat"] 一致）——陪伴记忆抽取的默认目标
CHAT_FAMILY_INTENTS = frozenset({
    "greeting", "small_talk", "direct_chat", "casual_chat", "chitchat", "free_chat",
})


def resolve_salience_rerank_cfg(memory_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """解析「情绪显著性重排」配置（R2/REMT-lite），容忍两种键名。

    历史/预设里既出现过 ``memory.salience_rerank`` 也出现过简写 ``memory.salience``
    （companion 预设曾误用后者，导致这条护城河特性被静默关掉——配置在、代码却读不到）。
    统一在此解析：优先 ``salience_rerank``，回退 ``salience``，让两种拼写都生效，
    并作为预设契约测试的单一事实源（防再次漂移）。
    """
    mcfg = memory_cfg or {}
    return dict(mcfg.get("salience_rerank") or mcfg.get("salience") or {})


def should_extract_intent(intent: str, ex_cfg: Dict[str, Any]) -> bool:
    """记忆抽取意图闸（Phase D：可单测的纯函数，替代内联 ``intent not in intents``）。

    语义（**保持存量部署零回归**）：
    - ``extract.match_all: true`` → 任何意图都抽（陪伴产品「全记」开关，需显式开）。
    - 否则按 ``extract.intents`` 白名单；未配置/空 → 不抽（与历史行为一致，不给存量加 token 成本）。
    """
    if (ex_cfg or {}).get("match_all"):
        return True
    intents = (ex_cfg or {}).get("intents")
    if not intents:
        return False
    return intent in set(intents)


def episodic_startup_banner(memory_cfg: Dict[str, Any], db_path: Any) -> Tuple[int, str]:
    """启动一行「情景记忆」状态 → ``(logging 级别, 文案)``，纯函数可单测。

    D-O5 / O-2 A（2026-09-08，WYNN22 #201）：此前无论白名单如何都写「情景记忆已启用」，
    skuio 机 clean 包 8 小时 30 次 schedule run 全 skip（intents=[]）时卡片与日志仍在
    说「已启用」——日志必须说真话：

    - 抽取总闸关 / 白名单空（且未开 match_all）→ **WARNING**，文案写明「记忆不会新增」，
      **不得**出现「已启用」四个字（诊断包一眼分清是库开着还是抽取活着）；
    - match_all → INFO「可抽取意图：全部」；
    - 白名单非空 → INFO「可抽取意图 N 个：…」（排序去重，值守对照配置零歧义）。
    """
    ex = dict((memory_cfg or {}).get("extract") or {})
    if not ex.get("enabled", True):
        return (
            logging.WARNING,
            "[episodic] 情景记忆库已就绪但抽取总闸已关（memory.extract.enabled=False），"
            f"记忆不会新增: {db_path}",
        )
    if ex.get("match_all"):
        return (
            logging.INFO,
            "[episodic] 情景记忆已启用 · 可抽取意图：全部（memory.extract.match_all=True）"
            f": {db_path}",
        )
    whitelist = sorted({str(x).strip() for x in (ex.get("intents") or []) if str(x).strip()})
    if not whitelist:
        return (
            logging.WARNING,
            "[episodic] 抽取白名单为空（memory.extract.intents=[] · match_all=False），"
            f"记忆不会新增——每条入站都会 skip: intent not extractable: {db_path}",
        )
    return (
        logging.INFO,
        f"[episodic] 情景记忆已启用 · 可抽取意图 {len(whitelist)} 个：{', '.join(whitelist)}"
        f": {db_path}",
    )


def _guard_offer_claims(
    reply: str,
    info: Dict[str, Any],
    *,
    cfg_root: Optional[Dict[str, Any]] = None,
    logger: Any = None,
    log_prefix: str = "",
) -> str:
    """出站优惠承诺守卫（P14）：未授权的折扣/券码/赠送 → 剥掉那一小句。

    定价是官网权限，LLM 编出来的折扣兑现不了——承诺类事故比链接越纪律更贵。
    白名单＝当日在效授权活动文案（``_goal_cta.offer_texts``，P13 目录里运营
    真授权过的那几条）+ 授权免费时长（``offer_free_days``，目录 claims 段——
    客户端真给得出的按天试用才登记；2026-08-11 免费档改按字符量后该表已清空，
    「免费试用 N 天」一律按编的剥）；婉拒语气（「暂时没有
    折扣」）刻意放行。整条都是承诺则换成合规话术。开关
    ``companion.goals.offer_guard.enabled``（默认开）。无目标会话由
    ``service.catalog_guard_facts`` 供同一份白名单（见调用点注释）。
    """
    try:
        _og = (((dict(cfg_root or {}).get("companion") or {}).get("goals") or {})
               .get("offer_guard") or {})
        if not bool(_og.get("enabled", True)):
            return reply
        from src.companion.goals.offer_guard import sanitize_offer_claims
        out, n, hits = sanitize_offer_claims(
            reply, allowed_texts=(info or {}).get("offer_texts") or [],
            allowed_free_days=(info or {}).get("offer_free_days") or [])
        if n and logger is not None:
            logger.warning(
                "%s[goal-offer-guard] 未授权优惠承诺已剥离 %d 处: %s",
                log_prefix, n, " | ".join(hits[:3]))
        if n:
            try:
                from src.companion.goals.stats import get_goal_stats
                get_goal_stats().record_offer_claim_stripped(
                    n, samples=hits, source="chat",
                    persona=str((info or {}).get("persona_id") or ""))
            except Exception:
                pass
        return out
    except Exception:
        if logger is not None:
            logger.debug("%s[goal-offer-guard] 守卫异常，保留原回复",
                         log_prefix, exc_info=True)
        return reply


def _guard_outbound_claims(
    reply: str,
    info: Dict[str, Any],
    *,
    cfg_root: Optional[Dict[str, Any]] = None,
    logger: Any = None,
    log_prefix: str = "",
    product_context: Optional[bool] = None,
) -> str:
    """出站事实声明守卫（P15）：报价/试用时长与目录不符、gated 线泄漏、
    内部指令泄漏 → 剥掉那一小句 / 摘掉指令标记。

    与 ``_guard_offer_claims`` 互补：那边管**承诺措辞**（N折/券码/赠送），
    这边管**事实对不对得上登记**——2026-07-28 对练实录里「团队版198…算下来
    一个月168」「官网有14天客户端试用」「免费图片换脸」三类都从措辞轴溜过去了。
    白名单同源 ``_goal_cta``（``catalog_prices`` / ``offer_free_days``），无目标
    会话由 ``service.catalog_guard_facts`` 供同一份事实。开关
    ``companion.goals.claim_guard.enabled``（默认开）。

    跨轮报价（``price_cross_turn``，默认开，kill-switch 可关）——``product_context``
    **按会话是否真在带货**取值，不再恒 True：
    - 带货会话（``_goal_cta`` 存在＝模板带 catalog，本身就等于「在谈我方产品」）
      → True，修 2026-07-28 二次实录逃逸：LLM 把产品词说在上一轮、越权报价说在
      下一轮（「团队版每月198美金」→ 下轮「一个月摊下来也就168」），单条口径
      整条跳过。开关前实测误报面：ROI 话术（「一个月省下2000块」「客服月薪4500块」）
      由 ``_OTHER_MONEY_CTX_RE`` 小句级排除，59 轮真实流量 + 9 例反例零误报。
    - 无目标会话（守卫覆盖扩到全会话后新增的那部分）→ **False**，回保守口径
      「产品/套餐词同句才校验数字」。否则纯陪聊里人设自家生意报价（「芒果干大包
      450比索」）会被当成越权报价剥掉。
    """
    try:
        _cg = (((dict(cfg_root or {}).get("companion") or {}).get("goals") or {})
               .get("claim_guard") or {})
        if not bool(_cg.get("enabled", True)):
            return reply
        if product_context is None:
            product_context = bool(_cg.get("price_cross_turn", True))
        else:
            product_context = bool(product_context) and bool(
                _cg.get("price_cross_turn", True))
        from src.companion.goals.claim_guard import sanitize_outbound_claims
        out, n, hits = sanitize_outbound_claims(
            reply,
            allowed_prices=(info or {}).get("catalog_prices") or [],
            allowed_free_days=(info or {}).get("offer_free_days") or [],
            product_context=product_context)
        if n:
            if logger is not None:
                logger.warning(
                    "%s[goal-claim-guard] 不实声明已处置 %d 处: %s",
                    log_prefix, n, " | ".join(hits[:3]))
            try:
                from src.companion.goals.stats import get_goal_stats
                get_goal_stats().record_claim_stripped(n)
            except Exception:
                pass
        return out
    except Exception:
        if logger is not None:
            logger.debug("%s[goal-claim-guard] 守卫异常，保留原回复",
                         log_prefix, exc_info=True)
        return reply


def _camp_context_text(user_context: Optional[Dict[str, Any]],
                       *, max_user_msgs: int = 6) -> str:
    """#147 跨轮推广语境：客户本条 + 近几条客户消息合并（纯读，绝不抛）。

    实录四句贬损一句都没点名「佳士得」，全靠「top-up bonuses / ad / auction /
    deposits」这类机制词在贬——只有知道客户刚提过登记活动，才敢把这些泛词句判成
    在贬自家。只取**客户侧**消息：AI 自己上一轮提过自家词不算「客户在谈」。
    """
    parts: List[str] = []
    try:
        uc = user_context or {}
        lm = str(uc.get("last_message") or "").strip()
        if lm:
            parts.append(lm)
        hist = uc.get("_conversation_history")
        if isinstance(hist, list):
            users = [str((m or {}).get("content") or "") for m in hist
                     if isinstance(m, dict) and str((m or {}).get("role") or "") == "user"]
            parts.extend(x for x in users[-max_user_msgs:] if x.strip())
    except Exception:
        pass
    return "\n".join(parts)[:4000]


def _guard_camp_disparagement(
    reply: str,
    info: Dict[str, Any],
    *,
    cfg_root: Optional[Dict[str, Any]] = None,
    logger: Any = None,
    log_prefix: str = "",
    context_text: str = "",
) -> str:
    """出站贬损守卫（#147）：句子命中【自家活动/产品词 + 负面定性词】→ 整句剥离；
    客户刚提过登记活动时，【活动机制泛词 + 负面词】也剥。剥空换中性兜底。

    词表 ``info["camp_terms"]`` 与 ``_guard_outbound_claims`` 的目录事实同源
    （``_goal_cta`` 暂存 / ``catalog_guard_facts``）。开关
    ``companion.goals.camp_guard.enabled``（默认开）；无登记词＝不判。
    """
    try:
        from src.companion.goals.service import camp_guard_cfg
        if not bool(camp_guard_cfg(cfg_root).get("enabled", True)):
            return reply
        terms = (info or {}).get("camp_terms") or []
        if not terms:
            return reply
        from src.companion.goals.claim_guard import sanitize_camp_disparagement
        out, n, hits = sanitize_camp_disparagement(
            reply, camp_terms=terms, context_text=context_text)
        if n:
            if logger is not None:
                logger.warning(
                    "%s[goal-camp-guard] 贬损自家阵营推广已剥离 %d 句: %s",
                    log_prefix, n, " | ".join(h[:60] for h in hits[:3]))
            try:
                from src.companion.goals.stats import get_goal_stats
                get_goal_stats().record_camp_stripped(n, samples=hits)
            except Exception:
                pass
        return out
    except Exception:
        if logger is not None:
            logger.debug("%s[goal-camp-guard] 守卫异常，保留原回复",
                         log_prefix, exc_info=True)
        return reply


# 轮级入站信号键（媒体/语音/重复/提示类）：语义只属**当前这条消息**，绝不跨轮驻留。
# 2026-08-16 实锤事故：_line_merge_keys 合并「只写不清」→ 8/01 键盘照片的识图描述随
# _media_desc 驻留 user_context 15 天；8/16 一条语音消息把 _peer_message_is_media 置位
# （语音不写 desc），prompt 遂把陈年键盘当「对方刚发来的媒体」喂给模型 → 客户问
# 「聊聊今天的新闻」、AI 答「诶这个键盘挺酷的嘛」。会话级粘性键（channel/account_id/
# 亲密度/画像等）不在此列，保持原粘性语义。
_TURN_SIGNAL_KEYS = (
    "_is_repeated_message", "_prev_reply_for_repeat",
    "_peer_message_is_voice", "_voice_duration", "_voice_lang_suspect",
    "_voice_asr_suspect",   # ASR P1：本条语音转写可疑原因码（asr_suspect）
    "_spoken_variant_request",
    "_peer_message_is_media", "_media_kind", "_media_desc", "_media_ref",
    "_inbox_peer_kind", "_inbound_short_hint", "_topic_switch_hint",
)


def clear_turn_signal_keys(user_context: Dict[str, Any]) -> None:
    """新一轮入站合并前清掉上一轮的瞬态信号（纯函数，见 _TURN_SIGNAL_KEYS 注释）。"""
    for _k in _TURN_SIGNAL_KEYS:
        user_context.pop(_k, None)


class SkillManager(LoggerMixin):
    """Skill管理�?"""

    def __init__(self, config, ai_client: AIClient):
        """
        # 初�化Skill管理�?
        Args:
            # config: 配置管理器实�?            ai_client: AI客户����?        """
        self.config = config
        self.ai_client = ai_client
        self.skills: Dict[str, 'Skill'] = {}
        self.reply_cache: Dict[str, float] = {}  # 内�哈希 -> �€后发送时�?
        self.global_last_reply_time = 0

        from src.utils.context_store import ContextStore
        cfg_dir = Path(config.config_path).parent if hasattr(config, "config_path") else Path("config")
        ttl_days = int(config.config.get("context_store", {}).get("ttl_days", 30)
                       if hasattr(config, "config") else 30)
        self._context_store = ContextStore(db_path=cfg_dir / "bot.db", ttl_days=ttl_days)

        # 从配���取��?        skills
        skills_config = config.get_skills_config()
        intent_config = config.get_intent_config()
        templates_config = config.get_templates_config()

        # 冷却时间设置
        _cd = skills_config.get('cooldown', {}) or {}
        self.cooldown_per_user = _cd.get('per_user', 60)
        self.cooldown_per_content = _cd.get('per_content', 120)
        self.cooldown_global = _cd.get('global', 0)
        self.cooldown_per_chat_user = _cd.get('per_chat_user', 1)
        self._chat_user_last_reply: Dict[str, float] = {}
        self._user_locks: Dict[str, asyncio.Lock] = {}
        # 按意图最小间隔（秒）；per_user �?0 时仍�闲聊等意图单����?
        self.cooldown_by_intent = _cd.get('by_intent') or {}
        if not isinstance(self.cooldown_by_intent, dict):
            self.cooldown_by_intent = {}
        # 「被吞回复」跳过信号（2026-08-09 修「回复等了 11 分钟」实录）：冷却拦截
        # 本身是静默的（只有一行日志），上层（telegram_client 等）无从区分这条
        # 入站是「刻意不回」还是「被冷却吃了、稍后应当补答」。此处按
        # (account,chat,user) 记最近一次拦截的原因与可重试秒数，由调用方
        # consume_reply_skip() 一次性取走（取走即删，防陈旧信号误触发补答）。
        # dict 按插入序 + 容量上限剪最旧，防长期运行涨内存。
        self._reply_skip_signals: Dict[str, Dict[str, float]] = {}
        # 收窄回复：仅允许部分意图（见 config narrow_reply）
        self._narrow_reply_cfg: Dict[str, Any] = {}
        if hasattr(config, "config") and isinstance(getattr(config, "config", None), dict):
            self._narrow_reply_cfg = dict((config.config or {}).get("narrow_reply") or {})

        # 意图识别配置
        self.intent_keywords = intent_config.get('keywords', {})
        self.intent_patterns = intent_config.get('patterns', {})

        # 回�模板
        self.templates = templates_config

        # 回�策略系统（从��� YAML 文件加载，支�?mtime ���新）
        self._load_strategies_from_config()

        # 策略 A/B 效果追踪
        from src.utils.strategy_tracker import StrategyTracker
        self._strategy_tracker = StrategyTracker(db_path=cfg_dir / "strategy_events.db")

        # Auto-Pilot 控制
        self._autopilot_msg_counter = 0
        self._autopilot_check_interval = 100  # �?100 条消�查一�?
        self._autopilot_last_check = 0.0

        # J1: 人工升级冷却 {user
        # FIXME: _id: last_escalation_ts}
        self._escalation_cooldown: Dict[str, float] = {}
        # R8: 危机人工接管冷却 {user_id: last_crisis_escalation_ts}
        self._crisis_escalation_cooldown: Dict[str, float] = {}

        # 人设一致性守卫：默认开（仅当人设声明了 forbidden_phrases / deny_ai 才实际生效，
        # 故对无禁用项的默认人设是零影响）。可经 companion.persona_guard.enabled 关闭。
        self._persona_guard_enabled: bool = True
        try:
            if hasattr(config, "config") and isinstance(getattr(config, "config", None), dict):
                _pg = ((config.config or {}).get("companion") or {}).get("persona_guard") or {}
                self._persona_guard_enabled = bool(_pg.get("enabled", True))
        except Exception:
            self._persona_guard_enabled = True

        self._memory_cfg: Dict[str, Any] = {}
        self._episodic_store = None
        self._memory_llm_last: Dict[str, float] = {}
        if hasattr(config, "config") and isinstance(getattr(config, "config", None), dict):
            self._memory_cfg = dict((config.config or {}).get("memory") or {})
        # _epath 供情景记忆与 CrossPlatformIdentity 共用——必须在 memory 开关分支外
        # 定义（修复：memory.enabled=False 时 CPI 初始化 NameError → 静默降级为 None）
        _mdb = self._memory_cfg.get("db_path")
        _epath = Path(str(_mdb)) if _mdb else (cfg_dir / "bot.db")
        if self._memory_cfg.get("enabled", True):
            try:
                from src.utils.episodic_memory_store import EpisodicMemoryStore
                self._episodic_store = EpisodicMemoryStore(_epath)
                # D-O5 / O-2 A（#201）：启动一行说真话——白名单空即 WARNING「记忆不会
                # 新增」，不再无条件写「已启用」（WYNN22：库开着、抽取死了 8 小时无人知）。
                _bn_level, _bn_msg = episodic_startup_banner(self._memory_cfg, _epath)
                self.logger.log(_bn_level, _bn_msg)
                # J-10 A2：例外打标阈值透传（memory.review.low_confidence_threshold，默认 0.6）
                _rv_cfg = self._memory_cfg.get("review") or {}
                _rv_thr = _rv_cfg.get("low_confidence_threshold")
                if _rv_thr is not None:
                    self._episodic_store.low_confidence_threshold = float(_rv_thr)
                # J-10 三期：高影响类别「只标红不进队列」（memory.review.high_impact_no_queue:
                # [family, ...]；默认空＝D8 原表六类都进队列）
                _no_q = _rv_cfg.get("high_impact_no_queue")
                if isinstance(_no_q, (list, tuple, set)):
                    self._episodic_store.high_impact_no_queue = tuple(
                        str(x).strip().lower() for x in _no_q if str(x).strip())
                # P8：启动时 observe-only 扫一轮，ops 去重卡立刻有读数
                try:
                    _obs = self._episodic_store.observe_dedup_all_users()
                    if _obs.get("users"):
                        self.logger.info(
                            "[episodic] boot dedup observe users=%s gray=%s held=%s",
                            _obs.get("users"), _obs.get("gray_pairs"),
                            _obs.get("held_merges"),
                        )
                except Exception:
                    self.logger.debug("boot dedup observe skipped", exc_info=True)
            except Exception as _mem_err:
                self.logger.warning("情景记忆初始化失败（将禁用）: %s", _mem_err)
                self._episodic_store = None

        self._cpi = None  # S5: CrossPlatformIdentity
        try:
            from src.utils.cross_platform_identity import CrossPlatformIdentity
            self._cpi = CrossPlatformIdentity(_epath)
        except Exception as _cpi_err:
            self.logger.warning("CrossPlatformIdentity 初始化失败: %s", _cpi_err)

        # R9: 危机事件审计库（默认随 wellbeing.crisis_audit 开；落 bot.db 同库独立表）
        self._crisis_store = None
        try:
            from src.utils.crisis_event_store import CrisisEventStore
            self._crisis_store = CrisisEventStore(cfg_dir / "bot.db")
        except Exception as _ce_err:
            self.logger.warning("危机事件库初始化失败（将禁用审计）: %s", _ce_err)
            self._crisis_store = None

        self.logger.info("Skill管理器初始化")

    def _kb_store_if_exists(self):
        """仅当 knowledge_base.db 已存在时返回共享实例（不仅为副作用新建空库）。"""
        try:
            from src.utils.kb_registry import get_kb_store
            return get_kb_store(self.config, require_exists=True)
        except Exception as e:
            self.logger.debug("KB 侧载失败: %s", e)
            return None

    @staticmethod
    def _cr_skip(user_context: Optional[Dict[str, Any]], layer: str) -> bool:
        """会话级「无限制」是否跳过某守卫层（conv_route.skip_guard 薄封装；标准会话恒 False）。"""
        try:
            from src.ai.conv_route import skip_guard
            return skip_guard(user_context, layer)
        except Exception:
            return False

    def _load_strategies_from_config(self) -> None:
        """�?config_manager 加载策略配置（独�?YAML 文件，自动迁�?+ mtime ���新）"""
        if hasattr(self.config, 'get_strategies_config'):
            rs = self.config.get_strategies_config()
        elif hasattr(self.config, 'config'):
            rs = self.config.config.get('reply_strategies', {})
        else:
            rs = {}
        rs = rs or {}
        self._strategies = rs.get('strategies', {}) or {}
        self._intent_strategy_map = rs.get('intent_strategy_map', {}) or {}
        self._ab_tests = rs.get('ab_tests', {}) or {}

    def _refresh_strategies(self) -> None:
        """�?reply_strategies.yaml 文件变化则重新加载（���新）"""
        if hasattr(self.config, 'get_strategies_config'):
            rs = self.config.get_strategies_config() or {}
            self._strategies = rs.get('strategies', {}) or {}
            self._intent_strategy_map = rs.get('intent_strategy_map', {}) or {}
            self._ab_tests = rs.get('ab_tests', {}) or {}

    def get_strategy_for_intent(self, intent: str, user_id: str = "") -> tuple:
        """根据意图获取回�策略，支�?A/B 灰度分流�?
        Returns:
            (strategy_dict, strategy_id)
        """
        # �€查是否有活跃�?A/B 测试
        ab = self._ab_tests.get(intent)
        if ab and ab.get("enabled") and ab.get("variants") and user_id:
            resolved = self._resolve_ab_variant(ab, user_id, intent)
            if resolved:
                return resolved

        strategy_id = (self._intent_strategy_map or {}).get(intent, 'S3_standard')
        strategies = self._strategies or {}
        strategy = strategies.get(strategy_id, {}) or {}
        if not strategy.get('enabled', True):
            strategy_id = 'S3_standard'
            strategy = strategies.get('S3_standard', {}) or {}
        return strategy, strategy_id

    def _resolve_ab_variant(self, ab: Dict, user_id: str, intent: str):
        """用一致�€�哈希从 A/B 测试变体�€�择策略"""
        variants = ab.get("variants", [])
        if not variants:
            return None
        bucket = int(hashlib.md5(f"{intent}:{user_id}".encode()).hexdigest(), 16) % 100
        cumulative = 0
        for v in variants:
            cumulative += v.get("weight", 0)
            if bucket < cumulative:
                sid = v.get("strategy_id", "")
                strat = (self._strategies or {}).get(sid)
                if strat and strat.get("enabled", True):
                    return strat, sid
        fallback_id = (self._intent_strategy_map or {}).get(intent, 'S3_standard')
        return (self._strategies or {}).get(fallback_id, {}) or {}, fallback_id

    @property
    def strategy_tracker(self):
        """暴露 StrategyTracker 实例给�层（Web ���盘等�?"""
        return self._strategy_tracker

    def get_all_strategies(self) -> Dict[str, Any]:
        """返回�€有策略（�?Web API 使用�?"""
        return self._strategies

    def get_intent_strategy_map(self) -> Dict[str, str]:
        """返回意图-策略映射（供 Web API 使用�?"""
        return self._intent_strategy_map

    def update_strategy(self, strategy_id: str, updates: Dict[str, Any]) -> bool:
        """���新单���略参数并持久化到 YAML 文件"""
        if strategy_id not in self._strategies:
            return False
        self._strategies[strategy_id].update(updates)
        return self._persist_strategies()

    def update_intent_mapping(self, intent: str, strategy_id: str) -> bool:
        """���新意�?策略映射并持久化�?YAML 文件"""
        if strategy_id not in self._strategies:
            return False
        self._intent_strategy_map[intent] = strategy_id
        return self._persist_strategies()

    def _persist_strategies(self) -> bool:
        """将当前内存中的策略写�?reply_strategies.yaml，保�?autopilot/data_retention 等附属配�?"""
        existing = {}
        if hasattr(self.config, 'get_strategies_config'):
            existing = self.config.get_strategies_config()
        data = {
            "strategies": copy.deepcopy(self._strategies),
            "intent_strategy_map": dict(self._intent_strategy_map),
            "ab_tests": copy.deepcopy(self._ab_tests) if self._ab_tests else {},
        }
        for preserve_key in ("autopilot", "data_retention"):
            if preserve_key in existing:
                data[preserve_key] = existing[preserve_key]
        if hasattr(self.config, 'save_strategies'):
            ok, msg = self.config.save_strategies(data)
            if not ok:
                self.logger.warning("策略持久化失�? %s", msg)
            return ok
        return False

    async def initialize(self) -> bool:
        """初始化Skill管理器"""
        try:
            self._register_skills()
            self.logger.info(f"已注册 {len(self.skills)} 个技能")
            return True
        except Exception as e:
            self.logger.error(f"初始化Skill管理器失败: {e}")
            return False

    def _register_skills(self):
        """Register all skills: generic built-ins + domain pack + plugins."""
        skills_config = self.config.get_skills_config()
        enabled_skills = skills_config.get('enabled', [])

        generic_classes = {
            'greeting': GreetingSkill,
            'complaint': ComplaintSkill,
            'small_talk': SmallTalkSkill,
            'test': TestSkill,
        }

        # 1) Register generic (built-in) skills
        for skill_name in enabled_skills:
            if skill_name in generic_classes:
                self.skills[skill_name] = generic_classes[skill_name](self.config, self.ai_client)
                self.logger.debug(f"注册通用技能: {skill_name}")

        # 2) Load active domain pack and register its skills
        self._load_domain_pack(enabled_skills)

        # 3) Ensure greeting is always registered
        if 'greeting' not in self.skills:
            self.skills['greeting'] = GreetingSkill(self.config, self.ai_client)

        # 4) Load plugins
        self._load_plugins()

        # 5) Drop intent keyword/pattern entries whose skills are not registered (e.g. payment disabled)
        self._prune_intent_config_to_loaded_skills()

    def _prune_intent_config_to_loaded_skills(self) -> None:
        """Remove intent.keywords / intent.patterns for intents with no registered skill (avoids mis-routing)."""
        available = set(self.skills.keys())
        dropped: List[str] = []
        new_kw: Dict[str, Any] = {}
        for intent, kws in (self.intent_keywords or {}).items():
            if intent in available:
                new_kw[intent] = kws
            else:
                dropped.append(intent)
        new_pat: Dict[str, Any] = {}
        for intent, pats in (self.intent_patterns or {}).items():
            if intent in available:
                new_pat[intent] = pats
            else:
                dropped.append(intent)
        if dropped:
            self.logger.info(
                "Pruned intent config for unregistered skills: %s",
                sorted(set(dropped)),
            )
        self.intent_keywords = new_kw
        self.intent_patterns = new_pat

    def _load_domain_pack(self, enabled_skills: list):
        """Load the active domain pack via DomainLoader."""
        from src.utils.domain_loader import DomainLoader, resolve_domains_dir

        config_obj = self.config.config if hasattr(self.config, 'config') else {}
        if not isinstance(config_obj, dict):
            config_obj = {}
        raw_domain = (config_obj.get("domain") or "").strip()
        domain_name = effective_domain_name(config_obj)
        if raw_domain == "payment" and domain_name != "payment":
            self.logger.info(
                "Payment domain plugin disabled; loading domain pack '%s' instead.",
                domain_name,
            )

        domains_dir = resolve_domains_dir(
            getattr(self.config, "config_path", None), domain_name)

        loader = DomainLoader(domains_dir)
        pack = loader.load(domain_name, Skill, self.ai_client, self.config)

        if pack is None:
            self.logger.warning("Domain pack '%s' failed to load.", domain_name)
            if payment_plugin_enabled(config_obj):
                self.logger.warning("Falling back to direct payment skill import.")
                self._fallback_direct_import(enabled_skills)
            return

        self._domain_pack = pack

        # Register domain hook if provided
        if pack.hook_class:
            try:
                from src.hooks.registry import HookRegistry
                hook_instance = pack.hook_class(config=self.config)
                HookRegistry.get_instance().register(hook_instance, domain_name)
                self.logger.info("Domain hook registered: %s", pack.hook_class.__name__)
            except Exception as e:
                self.logger.warning("Failed to register domain hook: %s", e)

        # Register domain persona if provided
        if pack.persona:
            try:
                from src.utils.persona_manager import PersonaManager
                PersonaManager.get_instance().set_domain_persona(pack.persona)
                self.logger.info("Domain persona set: %s", pack.persona.get("name", "?"))
            except Exception as e:
                self.logger.warning("Failed to set domain persona: %s", e)

        # 运行时人设覆盖（Web 保存的 persona_runtime.yaml，重启后仍生效）
        try:
            from src.utils.persona_manager import PersonaManager

            _cp = getattr(self.config, "config_path", None)
            if _cp:
                _cfg_dict = self.config.config if hasattr(self.config, "config") else {}
                if PersonaManager.get_instance().load_runtime_default_persona(
                    Path(_cp), _cfg_dict
                ):
                    self.logger.info("已应用 config/persona_runtime.yaml 人设覆盖")
        except Exception as _e:
            self.logger.debug("runtime persona: %s", _e)

        # Apply domain KB categories if provided
        if pack.kb_categories:
            from src.utils.kb_store import set_kb_categories
            cat_names = [c["name"] if isinstance(c, dict) else c for c in pack.kb_categories]
            set_kb_categories(cat_names)
            # P-4 #254（MTRCH2）：日志标签带业务域名——分类表其实按 business_domain 选
            # （categories.companion.yaml），只打域包名 'conversion' 让人以为域包没切。
            self.logger.info("KB categories set from domain '%s' (business_domain=%s): %s",
                             domain_name, _business_domain_label(), cat_names)

        # Push domain prompt/terminology to AI client
        if self.ai_client and hasattr(self.ai_client, 'set_domain_pack'):
            self.ai_client.set_domain_pack(
                system_prompt=pack.system_prompt,
                terminology=pack.terminology,
                context_supplements=pack.context_supplements,
            )

        # Merge domain i18n keys
        if pack.i18n:
            try:
                from src.utils.i18n import I18n
                i18n_instance = I18n()
                i18n_instance.merge_domain_keys(pack.i18n)
            except Exception as e:
                self.logger.warning("Failed to merge domain i18n keys: %s", e)

        # Set domain config directory for kb_direct_render
        domain_config_dir = pack.root / "config"
        if domain_config_dir.exists():
            from src.utils.kb_direct_render import set_domain_config_dir
            set_domain_config_dir(domain_config_dir)

        # Set domain-specific emotion enhancer skip rules
        defaults = pack.config_data.get('defaults', {})
        emotion_skip = defaults.get('emotion_enhancer_skip', {})
        if emotion_skip:
            from src.skills.emotion_enhancer import EmotionEnhancer
            EmotionEnhancer.set_domain_skip_rules(
                skip_phrases=emotion_skip.get('phrases', []),
                skip_patterns=[
                    (p.get('all_of', []), p.get('any_of', []))
                    for p in emotion_skip.get('patterns', [])
                ],
            )

        for skill_name, skill_class in pack.skill_classes.items():
            if skill_name in enabled_skills and skill_name not in self.skills:
                try:
                    self.skills[skill_name] = skill_class(self.config, self.ai_client)
                    self.logger.debug(f"注册域技能: {skill_name} (from {domain_name})")
                except Exception as e:
                    self.logger.error(f"域技能 {skill_name} 实例化失败: {e}")

    def _fallback_direct_import(self, enabled_skills: list):
        """Fallback: import payment skills directly if DomainLoader fails (payment plugin only)."""
        cfg = self.config.config if hasattr(self.config, "config") else {}
        if not payment_plugin_enabled(cfg if isinstance(cfg, dict) else {}):
            self.logger.debug("Payment skill fallback skipped (domain_plugins.payment.enabled is false).")
            return
        try:
            from domains.payment.skills import (
                GxpCommandSkill, OrderQuerySkill, PriceCheckSkill,
                StatusCheckSkill, ChannelInfoSkill, QuotaConfigSkill,
                EnhancedQuotaConfigSkill,
            )
            fallback_map = {
                'gxp_command': GxpCommandSkill,
                'order_query': OrderQuerySkill,
                'price_check': PriceCheckSkill,
                'status_check': StatusCheckSkill,
                'channel_info': ChannelInfoSkill,
                'quota_config': QuotaConfigSkill,
                'enhanced_quota_config': EnhancedQuotaConfigSkill,
            }
            for skill_name in enabled_skills:
                if skill_name in fallback_map and skill_name not in self.skills:
                    self.skills[skill_name] = fallback_map[skill_name](self.config, self.ai_client)
                    self.logger.debug(f"注册技能(fallback): {skill_name}")
        except ImportError as e:
            self.logger.error(f"Fallback import also failed: {e}")

    def _load_plugins(self):
        from src.utils.plugin_loader import PluginLoader
        plugin_dir = Path(self.config.config_path).parent.parent / "plugins" if hasattr(self.config, "config_path") else Path("plugins")
        cfg = self.config.config if hasattr(self.config, "config") else {}
        self._plugin_loader = PluginLoader(plugin_dir, cfg)
        plugins = self._plugin_loader.load_all(Skill, self.ai_client, self.config)
        for name, skill in plugins.items():
            intent_name = f"plugin_{name}"
            self.skills[intent_name] = skill
            self.logger.info("插件�€能已注册: %s �?%s", intent_name, skill.__class__.__name__)

    async def process_message(
        self,
        text: str,
        user_id: Any,
        context: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """
        # 处理用户消息（Per-Chat-User 串�锁：同群同用户串行保序， 不同群可并�处理，避免跨群阻塞）
        """
        chat_id = (context or {}).get('chat_id', '')
        # 双号隔离：同 peer 不同 account_id 必须各锁各的，否则串行互踩 send 回调
        _acct = str((context or {}).get('account_id') or '').strip()
        _base = f"{chat_id}_{user_id}" if chat_id else str(user_id)
        lock_key = f"{_acct}:{_base}" if _acct and _acct != "default" else _base
        lock = self._user_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            return await self._handle_message_guarded(text, user_id, context)

    async def _handle_message_guarded(
        self,
        text: str,
        user_id: Any,
        context: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        user_ctx_for_cleanup: Optional[Dict[str, Any]] = None
        _cr_scope_a = None
        try:
            user_id_str = str(user_id)
            context = context or {}
            if not context.get("request_id"):
                context["request_id"] = f"r-{uuid.uuid4().hex[:12]}"

            req_id = context.get("request_id", "")
            log_prefix = f"[{req_id}] " if req_id else ""

            self._refresh_strategies()

            _chat_id = context.get('chat_id', '')
            _acct_id = str(context.get('account_id') or '').strip()
            # P1-2：群聊分窗（仅显式群信号；私聊/协议链复合键不受影响）
            _chat_scope = self._context_chat_scope(context, user_id_str)

            # 1. 获取或创建用户上下文（遗忘指令需优先于冷却）
            # 双号隔离：store key = account_id:user_id，避免 Katie/Jason 共用 peer 历史
            user_context = self._get_user_context(
                user_id_str, account_id=_acct_id, chat_scope=_chat_scope)
            _ctx_store_key = str(
                user_context.get("_context_store_key") or user_id_str)
            user_ctx_for_cleanup = user_context
            # 会话级模型路由（conv_route，2026-09-12）：A 线与 B 线同一读点——协议线 user_id
            # 即三段式会话 id，原生 TG 按 platform/account/chat_id 拼。标准默认＝只清键。
            try:
                from src.ai import conv_route as _cr_a
                _cr_route_a = _cr_a.attach(
                    user_context, _cr_a.conv_id_from_context(context, user_id_str),
                    config=self.config)
                if not _cr_route_a.is_default:
                    _cr_scope_a = _cr_a.generation_scope(_cr_route_a, self.config)
                    _cr_scope_a.__enter__()
            except Exception:
                self.logger.debug("%sconv_route 解析跳过", log_prefix, exc_info=True)
                _cr_scope_a = None
            # 平台/账号软标记（与 B 线 generate_inbox_draft 同口径，写-if-absent）：
            # case 深链（conv_ref）等消费方读 ctx 键——A 线此前不落，RPA 会话的
            # 「打开会话」深链会被误拼成 telegram。同一会话的平台/账号恒定，无覆写面。
            _plat_soft = str(context.get("platform") or "").strip()
            if _plat_soft and not user_context.get("platform"):
                user_context["platform"] = _plat_soft
            if _acct_id and not user_context.get("account_id"):
                user_context["account_id"] = _acct_id
            # 入站消息 id（TG user_msg_id / 协议 message_id…）→ 案例事件锚点
            # （open_case 自吸 mid → 深链 &mid= 直达质疑气泡）。每轮覆盖：本条
            # 才是立案触发点；旧值留着会把新案锚到上一轮气泡。
            _inbound_mid = (
                context.get("user_msg_id")
                or context.get("message_id")
                or context.get("platform_msg_id")
                or ""
            )
            if _inbound_mid not in (None, "", 0, "0"):
                user_context["user_msg_id"] = _inbound_mid
            last_intent = user_context.get('current_intent', '')

            # P3-2：群聊场景提示（每轮重算防陈旧）——群窗回复注入群感知约束，
            # 修「把群当私聊」（自我介绍/亲昵开场/长篇输出，2026-07-25 实测反馈）。
            user_context.pop("_group_chat_hint", None)
            if _chat_scope:
                user_context["_group_chat_hint"] = self._group_chat_hint_text()

            # 引用上下文运输（2026-08-20）：user_context 合并是选择性键清单，
            # _quoted_note 不在清单内 → 照 _bug_intake_block 模式显式搬运；
            # 每轮先清（引用只属于本条消息，绝不跨轮粘住）。
            user_context.pop("_quoted_note", None)
            try:
                _qn_in = str((context or {}).get("_quoted_note") or "").strip()
                if _qn_in:
                    user_context["_quoted_note"] = _qn_in
            except Exception:
                pass

            # 报障群值守（bug_intake，2026-08-18）：观察+分类+工单登记 →
            # prompt 块注入；回执 footer 在 5c2 终稿层由代码追加（LLM 复述
            # 编号会抄错/漏写）。非报障群 active=False 全程 no-op。
            user_context.pop("_bug_intake_block", None)
            _bug_intake_res: Optional[Dict[str, Any]] = None
            if _chat_scope:
                try:
                    from src.ops.bug_intake import observe_group_message
                    _bi_cfg = self.config.config if hasattr(
                        self.config, "config") else (
                        self.config if isinstance(self.config, dict) else {})
                    # 引用文本并入分类判定（2026-08-20：「引用报障消息+『分析』」
                    # 该判 bug 而非闲聊）。只喂 observe 的分类/工单链，不动正文。
                    _bi_text = text
                    try:
                        _qn = str((context or {}).get("_quoted_note") or "")
                        if _qn:
                            _qraw = (context or {}).get("quoted") or {}
                            _bi_text = (text + " [引用] "
                                        + str(_qraw.get("text") or _qn)[:200])
                    except Exception:
                        _bi_text = text
                    _bi_res = observe_group_message(
                        _bi_cfg, chat_id=_chat_id, account_id=_acct_id,
                        reporter_id=user_id_str,
                        reporter_name=str(
                            (context or {}).get("_peer_display_name")
                            or (context or {}).get("user_name") or ""),
                        text=_bi_text)
                    if _bi_res.get("active"):
                        _bug_intake_res = _bi_res
                        if _bi_res.get("prompt_block"):
                            user_context["_bug_intake_block"] = _bi_res[
                                "prompt_block"]
                except Exception:
                    self.logger.debug("[bug_intake] observe 接线异常（跳过）",
                                      exc_info=True)

            # 报障群 AI 全静默（2026-08-21 11:05 老板纪律，B36 收紧版）：这个群
            # 是「真正解决问题的群」——登记/回复/整理全部由值守人工来，本地模型
            # 与云端一条不发。观察层上面已照常记工单/告警/收集窗；此处在**生成
            # 之前**短路（None=不回复，与超时路径同契约），模型压根不跑。
            if _bug_intake_res and _bug_intake_res.get("ai_silent"):
                self.logger.info(
                    "%s[bug_intake] 报障群 AI 全静默：已登记（工单/事件照记），"
                    "回复交值守人工", log_prefix)
                return None

            # P7-1：将调用方单次请求的 LINE RPA 上下文并入 user_context，
            # 供 AIClient._build_context_prompt 读取 channel / line_rpa_style_hint 等
            _line_merge_keys = (
                "channel", "request_id", "reply_lang",
                "reply_lang_locked",
                # 会话语言契约（lang_policy）：runner 侧持久偏好回灌
                "user_lang_pref", "user_lang_pref_input",
                "line_rpa_style_hint", "line_rpa_chat_key",
                "messenger_rpa_style_hint", "messenger_rpa_chat_key",
                "messenger_rpa_peer_kind",
                "whatsapp_rpa_chat_key", "whatsapp_rpa_peer_name",
                "whatsapp_rpa_style_hint",
                "account_persona_id",
                "suppress_global_ai_identity",
                "disable_episodic_memory",
                "is_group", "mentioned", "vision_room",
                # Phase 1：用户画像上下文 — 由 runner 从 ContactGateway 渲染好后注入
                "contact_id", "_contact_portrait_block",
                # W2-D1：IntimacyEngine 的 score → companion_relationship 双信号融合
                "intimacy_score",
                # P10-C：漏斗阶段 → PersonaManager 平台感知 prompt 注入
                "funnel_stage",
                # 单条主消息（用于语言注入，避免多条合并文本污染检测）
                "_current_user_message_for_lang",
                # S5: 平台标识，用于 CrossPlatformIdentity canonical_id 解析
                "platform",
                # 重复消息标记（runner 检测到用户发了跟上次完全一样的消息）
                "_is_repeated_message", "_prev_reply_for_repeat",
                # 语音消息标记（对方发的是语音，AI 回复应更口语化）
                "_peer_message_is_voice", "_voice_duration",
                # 可疑语音转写（ASR 语种与会话语言冲突）→ 澄清话术提示
                "_voice_lang_suspect",
                # ASR P1：转写置信度可疑原因码（A 线由 telegram_client 按 last_meta 算好带入）
                "_voice_asr_suspect",
                # 生成层口语分叉（Phase G）：本条回复可能发语音 → prompt 多要
                # 一个 [口语版] 段（ai_client 消费+剥离，spoken_variant 暂存）
                "_spoken_variant_request",
                # 入站媒体 / 短消息 / 语言切换提示（Telegram 收件箱与 RPA 共用）
                "_peer_message_is_media", "_media_kind", "_media_desc", "_media_ref",
                "_inbox_peer_kind", "_inbound_short_hint", "_topic_switch_hint",
                # 2026-07-22 真机复盘：selfie/媒体发送走编排器路需要 account_id
                # ——协议线（WA 等）ctx 带了它但没进 user_context，
                # _try_send_selfie_media 读到 None → 图生成了也发不出（静默回落文字）。
                "account_id",
            )
            # 瞬态轮级信号先清再合并（2026-08-16 键盘实锤：只写不清＝陈年媒体描述
            # 跨轮驻留，见模块级 _TURN_SIGNAL_KEYS 注释）——本轮 context 带了才写回。
            clear_turn_signal_keys(user_context)
            for _mk in _line_merge_keys:
                if _mk in context and context[_mk] is not None:
                    user_context[_mk] = context[_mk]

            # 2026-07-22 真机复盘（TG 首条要图必失败）：媒体发送回调必须在
            # Stage 0/A/B **之前**入 user_context——原 merge 点在 Stage A 之后，
            # 首条消息发图时通道恒缺失（第二条才"继承"上一轮写入的回调），
            # 且重启后 user_context 从磁盘恢复（callable 不落盘）再次全丢。
            for _cbk in ("_send_to_chat", "_send_photo_to_chat",
                         "_send_video_to_chat"):
                if _cbk in context and context[_cbk] is not None:
                    user_context[_cbk] = context[_cbk]

            _forget_reply = self._handle_episodic_forget_command(
                text, user_id_str, user_context, _chat_id
            )
            if _forget_reply is not None:
                self._context_store.mark_dirty(_ctx_store_key)
                self._context_store.flush(_ctx_store_key)
                return _forget_reply

            # Stage 1：剧情/成长指令前懒解析端用户真实付费权益进 user_context——
            # 让付费剧情闸（story_engine.require_unlock）据真实拥有判准入（付费用户进得去、
            # 免费看到锁）。resolver 未注册（变现未就绪）→ entitlement 维持 None，零回归。
            self._ensure_entitlement(user_id_str, user_context)
            # Phase ③：剧情指令（列表/开始/结束）。返回字符串=短路；None=非指令或开始成功
            # （开始成功只置 state，后续正常生成带【剧情场景】块的开场）。
            try:
                _story_reply = self._handle_story_command(
                    text, user_context, _chat_id
                )
            except Exception:
                _story_reply = None
                self.logger.debug("story command skipped", exc_info=True)
            if _story_reply is not None:
                self._context_store.mark_dirty(_ctx_store_key)
                self._context_store.flush(_ctx_store_key)
                return _story_reply

            # Phase ④续³：关系/成长面板（端用户在对话内查询自己的成长——把记忆/成长/剧情
            # 整条链的进度一屏看见）。返回字符串=短路；None=非指令。
            try:
                _growth_reply = self._handle_growth_command(
                    text, user_context, _chat_id
                )
            except Exception:
                _growth_reply = None
                self.logger.debug("growth command skipped", exc_info=True)
            if _growth_reply is not None:
                return _growth_reply

            # 清上一轮残留的发图协同 hint（若上一轮设了 hint 却因冷却等提前返回，
            # 不让它渗漏进本轮 prompt）；本轮如需会由 Stage B/draft 路径重新设置。
            user_context.pop("_media_coherence_hint", None)
            user_context.pop("_media_pending_hint", None)
            user_context.pop("_song_coherence_hint", None)

            # 唱歌协同 hint：实施66 起随 Stage S（_handle_song_request）注入——
            # A 线已接真唱短路，「要歌」由 Stage S 统一判定（含粘性窗逼唱），
            # 发不出时在那里注入 REFUSAL；这里只保留轮首清残留（上方 pop）。

            # 收图后质疑信号（P0 一致性观测）：最近发过媒体且本条像在质疑图
            # （重复/不像/假图）→ 计数 + 设纠偏 hint（别争辩/别再重发同图）。
            self._maybe_flag_media_complaint(text, user_context)
            # 悬置媒体请求（实施69）：客户要过图/AI 承诺过而媒体还没到位 →
            # 跨轮记住（含主体「烤串」），供 claim 门控/Stage 主体兜底/hint 消费。
            _media_pending_kind = ""
            try:
                from src.ai.media_pending import note_peer_turn as _mp_note
                _media_pending_kind = _mp_note(
                    user_context, text,
                    history=[{"role": "assistant",
                              "content": str(user_context.get("last_reply")
                                             or "")}])
            except Exception:
                self.logger.debug("media_pending note skipped", exc_info=True)
            # P1 承诺循环熔断：已连续 ≥2 次答应发图却没发出 + 本条仍在要图 →
            # 注入「别再答应」hint，打断「每轮说马上拍→撤回→下轮又说」死循环
            # （对练 T9/T10 实证）。仅当无更高优先的质疑 hint 时设。
            try:
                if not user_context.get("_media_coherence_hint"):
                    from src.ai.outbound_promise_guard import wants_media as _wm3
                    _streak = int(
                        user_context.get("_photo_promise_streak", 0) or 0)
                    if _streak >= 2 and _wm3(text) == "image":
                        user_context["_media_coherence_hint"] = (
                            "重要：你已经连续好几次答应发照片却始终没真的发出，"
                            "对方已经在质疑你说话不算话。这一轮**绝对不要再答应**"
                            "「等我拍/马上发/而家拍俾你」这类话（再空口承诺只会更"
                            "像骗子），坦诚说这会儿真的拍不了/暂时没有合适的，"
                            "然后自然把话题岔开、多聊聊对方。")
            except Exception:
                pass
            # 悬置常驻 hint（实施69）：只要客户还在等一张没到的图，每一轮都
            # 明示 LLM「别承诺、别称已发」——检测词表是概率性防御，prompt 从
            # 源头少产谎。**独立键** `_media_pending_hint`：与 `_media_coherence_
            # hint` 分开是刻意的——后者兼作 Stage B「本轮已判定发不出」的防重烧
            # 标志（_handle_object_image 见它即让路），悬置提示若占同一键会把
            # 真出图部署的物体图链误关。措辞两头不穿帮（本轮 Stage 若真发出图，
            # 媒体日志让悬置自动熄灭；prompt 侧「只有真发出才带图」依然成立）。
            try:
                if _media_pending_kind == "image":
                    from src.ai.media_pending import (
                        pending_subject as _mp_subj,
                        pending_urges as _mp_urges,
                    )
                    _psub = _mp_subj(user_context)
                    _what = f"「{_psub}」的照片" if _psub else "照片"
                    _hint = (
                        f"对方在等你发{_what}，到现在还没真的发出去。"
                        "系统只有真发出照片时才会带图；你这条文字**不许**说"
                        "「已经发了/这就拍/马上发/来了/信号不好没传出去」这类话"
                        "（没兑现的承诺和假称已发都会被对方当场戳穿），也不要"
                        "否认你能拍照。要么等系统真发出，要么用人设口吻给个"
                        "自然的缘由（比如手上正忙腾不开），并把话题聊开。")
                    # 催促升级（实施69「IOU 即 hint 升级」）：对方已催 ≥2 次，
                    # 再「聊开」就是装傻——主动给可信交代并收住这个话头。
                    if _mp_urges(user_context) >= 2:
                        _hint += (
                            "注意：对方已经为这张照片催了你好几次、耐心快用完了。"
                            "这一轮必须**主动给个交代**：用人设口吻把「这会儿真"
                            "发不了」的缘由说清楚（一句就够，别解释一堆），可以"
                            "给个不带具体时限的软性后续（比如『回头得空拍了给你』），"
                            "然后把注意力放回对方身上；绝不许再拖、再承诺马上、"
                            "或假装已经发过。")
                    user_context["_media_pending_hint"] = _hint
            except Exception:
                pass

            # Stage 0：人设注册相册（DB 预制图/视频，按触发词命中即发）。先于生成，秒发零成本。
            # 返回 ""=媒体已发出不再补文字；None=未命中/未开/发送不可用（交 Stage A/B）。
            try:
                _media_reply = await self._handle_persona_media_request(
                    text, user_id_str, user_context, _chat_id
                )
            except Exception:
                _media_reply = None
                self.logger.debug("persona media request skipped", exc_info=True)
            if _media_reply is not None:
                # 短路轮回写（修「发图后失忆」）：媒体成功轮记 "[图片] 配文"
                # （handler 预置 _stage_media_note），搪塞轮记搪塞文字——下一轮
                # 经既有「上一轮补录进 _conversation_history」机制进入上下文。
                self._record_stage_turn(user_context, text, _media_reply)
                self._context_store.mark_dirty(_ctx_store_key)
                self._context_store.flush(_ctx_store_key)
                return _media_reply or None

            # Stage A：形象照/自拍请求（「给我看看你」）。返回字符串=短路（搪塞/付费引导/
            # 出图配文/兜底）；""=媒体已发出不再补文字；None=非请求或功能未开。
            try:
                _selfie_reply = await self._handle_selfie_request(
                    text, user_id_str, user_context, _chat_id
                )
            except Exception:
                _selfie_reply = None
                self.logger.debug("selfie request skipped", exc_info=True)
            if _selfie_reply is not None:
                self._record_stage_turn(user_context, text, _selfie_reply)
                self._context_store.mark_dirty(_ctx_store_key)
                self._context_store.flush(_ctx_store_key)
                return _selfie_reply or None

            # Stage B：对话上下文「按需生图」（"你煮的面拍张照给我看"——对话里提到的东西）。
            # 返回字符串=短路；""=图已发出不再补文字；None=非要图/未开/无真出图后端。
            try:
                _ctx_img_reply = await self._handle_contextual_image_request(
                    text, user_id_str, user_context, _chat_id
                )
            except Exception:
                _ctx_img_reply = None
                self.logger.debug("contextual image request skipped", exc_info=True)
            if _ctx_img_reply is not None:
                self._record_stage_turn(user_context, text, _ctx_img_reply)
                self._context_store.mark_dirty(_ctx_store_key)
                self._context_store.flush(_ctx_store_key)
                return _ctx_img_reply or None

            # Stage C：人生 K 线卡片（companion.bazi，「画一下我的运势曲线」→ 渲染 PNG 发图）。
            # 返回 ""=图已发出（caption 随图）；None=非请求/未开/缺生辰（交注入路径顺势采集）。
            try:
                _kline_reply = await self._handle_bazi_kline_request(
                    text, user_id_str, user_context, _chat_id
                )
            except Exception:
                _kline_reply = None
                self.logger.debug("bazi kline request skipped", exc_info=True)
            if _kline_reply is not None:
                self._record_stage_turn(user_context, text, _kline_reply)
                self._context_store.mark_dirty(_ctx_store_key)
                self._context_store.flush(_ctx_store_key)
                return _kline_reply or None

            # Stage S：点名/逼唱要歌 → 发预渲染真唱段（实施66 P0-1，A 线兑现短路；
            # 位序镜像 B 线 _autosend_deliver：kline 之后、泛化要图之前已被 0/A/B
            # 覆盖故紧随 kline）。返回 ""=唱段已发出；None=非要歌/发不出（发不出
            # 时已注入 REFUSAL hint 禁文字假唱/空头承诺，回落文字链）。
            try:
                _song_reply = await self._handle_song_request(
                    text, user_id_str, user_context, _chat_id
                )
            except Exception:
                _song_reply = None
                self.logger.debug("song request skipped", exc_info=True)
            if _song_reply is not None:
                self._record_stage_turn(user_context, text, _song_reply)
                self._context_store.mark_dirty(_ctx_store_key)
                self._context_store.flush(_ctx_store_key)
                return _song_reply or None

            # 2. 冷却（按 account_id 分桶，双号互不踩；群聊读群窗同键）
            #    无限制会话（conv_route）：冷却是业务护栏，让路
            _cd_left = 0.0
            if not self._cr_skip(user_context, "skill_cooldown"):
                _cd_left = self._cooldown_remaining(
                    text, user_id_str, chat_id=_chat_id, account_id=_acct_id,
                    chat_scope=_chat_scope)
            if _cd_left > 0:
                # 静默拦截 → 显式信号（2026-08-09）：让上层能区分「刻意不回」
                # 与「被冷却吃了」，为该 mid 安排冷却结束后的补答重试。
                self._note_reply_skip(
                    _chat_id, user_id_str, _acct_id,
                    reason="cooldown", retry_after=_cd_left)
                self.logger.warning(
                    "%s用户 %s 处于冷却期，跳过回复（剩余 %.0fs）",
                    log_prefix, user_id_str, _cd_left)
                return None

            # 3. 注入情景记忆（关键词 / 向量融合 + 分桶）
            if user_context.get("disable_episodic_memory"):
                user_context.pop("_episodic_memory_text", None)
            else:
                _q_emb = None
                _mvec = (self._memory_cfg or {}).get("vector") or {}
                if (
                    self._episodic_store
                    and (self._memory_cfg or {}).get("enabled", True)
                    and _mvec.get("enabled", False)
                    and self.ai_client
                ):
                    _q_emb = await self._embed_user_message_for_episodic(text)
                self._inject_episodic_into_context(
                    user_context,
                    user_id_str,
                    _chat_id,
                    current_user_text=text,
                    query_embedding=_q_emb,
                    platform=user_context.get("platform", ""),  # S5
                )

            # 3a2. 场景状态注入（Phase18 图文同源）：聊天文本与生图共用同一个
            # 「AI 此刻在哪」，附带「最近发过的照片」事实块（防失忆抵赖）。
            self._inject_scene_state(user_context)

            # 3a2b. 已知画像硬注入 + 禁复问（B50）与人设自述状态衔接（B52），
            # 实施64 P1-2，与 B 线 3b3b 同口径。
            try:
                _kp_key = self._episodic_storage_key(
                    user_id_str, _chat_id, user_context.get("platform", ""),
                    user_context=user_context)
                self._inject_known_profile(user_context, _kp_key)
            except Exception:
                self.logger.debug("known_profile inject skipped", exc_info=True)
            self._inject_self_state(user_context)

            # 3a3. 用户侧在地化（P2，默认关）：对方当地时间 + 对方那边的节日。
            # 不受 selfie 开关影响（3a2 是 selfie-gated 的），故单独一跳。
            self._inject_peer_locale(
                user_context, user_id_str, _chat_id,
                user_context.get("platform", ""),
            )

            # 3b. 情感智能上下文引擎（情绪分析 + 时间感知 + 记忆反思 + 关系温度）
            try:
                from src.utils.emotional_context import build_emotional_context_block
                _epi_text = (user_context.get("_episodic_memory_text") or "").strip()
                # 共情策略选择器开关（默认开；纯 prompt 提示，零行为风险）
                _cfg_es = self.config.config if hasattr(self.config, "config") else {}
                _es_on = bool(
                    ((_cfg_es.get("companion") or {}).get("empathy_strategy") or {})
                    .get("enabled", True)
                ) if isinstance(_cfg_es, dict) else True
                # R4 安全守卫开关（默认开，与 persona_guard 同为安全家族；纯 prompt 提示）
                _wb_cfg = (
                    ((_cfg_es.get("companion") or {}).get("wellbeing") or {})
                    if isinstance(_cfg_es, dict) else {}
                )
                _wb_on = bool(_wb_cfg.get("enabled", True))
                _antisyc_on = bool(_wb_cfg.get("anti_sycophancy", True))
                _wb_hotline = str(_wb_cfg.get("crisis_resources", "") or "")
                _emo_block = build_emotional_context_block(
                    text, user_context, _epi_text, chat_id=_chat_id,
                    enable_strategy=_es_on,
                    enable_wellbeing=_wb_on,
                    enable_anti_sycophancy=_antisyc_on,
                    wellbeing_hotline=_wb_hotline,
                )
                if _emo_block:
                    user_context["_emotional_context_block"] = _emo_block
                    self.logger.info(
                        "%s情感上下文引擎: emotion=%s valence=%s warmth_label=%s",
                        log_prefix,
                        user_context.get("_prev_emotion", "?"),
                        user_context.get("_prev_valence", "?"),
                        "active",
                    )
            except Exception:
                self.logger.warning("emotional_context inject skipped", exc_info=True)

            # 4. 合并传入的上下文信息（上下文分析、图�?OCR、群内机器人消息、request_id、chat�?
            if 'context_analysis' in context:
                user_context['context_analysis'] = context['context_analysis']
            if 'image_ocr_text' in context and context['image_ocr_text']:
                user_context['image_ocr_text'] = context['image_ocr_text']
            if 'recent_bot_messages' in context and context['recent_bot_messages']:
                user_context['recent_bot_messages'] = context['recent_bot_messages']
            if context.get('request_id'):
                user_context['request_id'] = context['request_id']
            if 'chat_id' in context:
                user_context['chat_id'] = context['chat_id']
            if 'chat_title' in context:
                user_context['chat_title'] = context['chat_title']
            if context.get('user_emotion_hint'):
                user_context['user_emotion_hint'] = context['user_emotion_hint']
            # P1-198 续（2026-08-02）：坐席人工「客户情绪」标注（TTL 窗内）覆写语气
            # hint——A 线全自动会话的 emotion_guide / goals 让路随之跟随坐席判断。
            # 判据与 B 线拟稿指令/主动闸门同源（effective_mood 单一仲裁）。协议线
            # user_id 即三段式 conversation_id；原生会话按 context 尽力构造，查不到
            # 会话 meta（账号键不匹配等）按无标注处理，绝不影响出话。
            try:
                from src.integrations.protocol_bridge import (
                    get_inbox_store as _mood_gis,
                )
                _mood_store = _mood_gis()
            except Exception:
                _mood_store = None
            if _mood_store is not None:
                try:
                    from src.inbox.effective_mood import (
                        record_mood_consume as _mood_consumed,
                        resolve_mood_steering_cfg as _mood_cfg,
                        tone_hint_override as _mood_tone_ovr,
                    )
                    _ms_a = _mood_cfg(
                        self.config.config
                        if hasattr(self.config, "config") else {})
                    if _ms_a["enabled"]:
                        _uid_s = str(user_id or "")
                        if _uid_s.count(":") >= 2:
                            _cid_a = _uid_s
                        else:
                            from src.inbox.normalizer import conv_id as _cidf_a
                            _plat_a = str(context.get("platform") or "")
                            _key_a = str(context.get("chat_id") or _uid_s or "")
                            _cid_a = _cidf_a(
                                _plat_a,
                                str(context.get("account_id") or "default"),
                                _key_a,
                            ) if (_plat_a and _key_a) else ""
                        if _cid_a:
                            _meta_a = _mood_store.get_conv_meta(_cid_a) or {}
                            _ovr_a = _mood_tone_ovr(
                                _meta_a, now=time.time(),
                                ttl_hours=_ms_a["ttl_hours"])
                            if _ovr_a:
                                user_context["user_emotion_hint"] = _ovr_a
                                _mood_consumed("tone_a")
                except Exception:
                    self.logger.debug(
                        "mood tone override skipped", exc_info=True)
            if '_send_to_chat' in context:
                user_context['_send_to_chat'] = context['_send_to_chat']
            # 2026-07-22 真机复盘：A 线照片/视频回调必须一并落 user_context——
            # _try_send_selfie_media 读的是 user_context，不是本次 context；
            # 此前只 merge 了 _send_to_chat（文本），TG 原生会话 selfie 生成成功
            # 却"无可用媒体通道"静默回落文字（客户要图永远要不到）。
            if '_send_photo_to_chat' in context:
                user_context['_send_photo_to_chat'] = context['_send_photo_to_chat']
            if '_send_video_to_chat' in context:
                user_context['_send_video_to_chat'] = context['_send_video_to_chat']
            if context.get('triggered_by_mention') is not None:
                user_context['triggered_by_mention'] = context['triggered_by_mention']

            # P1 陪伴关系阶段（conversion；持久化于 companion_relationship[chat_key]）
            try:
                _cfg_dom = self.config.config if hasattr(self.config, "config") else {}
                _comp_cfg = (_cfg_dom.get("companion") or {}) if isinstance(_cfg_dom, dict) else {}
                if effective_domain_name(_cfg_dom) == "conversion" and _comp_cfg.get(
                    "enabled", True
                ):
                    from src.utils.companion_relationship import (
                        build_relationship_prompt_block,
                        downgrade_from_user_text,
                        get_rel_state,
                    )

                    _rst = get_rel_state(user_context, _chat_id)
                    downgrade_from_user_text(_rst, text, _comp_cfg)
                    _ain = ""
                    try:
                        _ain = str(
                            (self.config.get_ai_config() or {}).get("ai_name") or ""
                        ).strip()
                    except Exception:
                        pass
                    # W2-D1：拉 IntimacyEngine 的 score（runner 已注入到 context）
                    # 没传则 fusion 自动跳过 → 完全向后兼容
                    _intim_score = context.get("intimacy_score")
                    try:
                        _intim_score = (
                            float(_intim_score) if _intim_score is not None else None
                        )
                    except (TypeError, ValueError):
                        _intim_score = None
                    user_context["_relationship_prompt_block"] = (
                        build_relationship_prompt_block(
                            _rst, _comp_cfg, ai_name=_ain, user_message=text,
                            intimacy_score=_intim_score,
                        )
                    )
                    user_context["relationship_stage"] = str(_rst.get("stage") or "")
            except Exception:
                self.logger.debug("companion relationship inject skipped", exc_info=True)

            # W3-3M：RelationshipStager — 跨域轻量语气指令
            # 与 companion_relationship 互不干扰：conversion 域有完整关系块，
            # 其他域（或 companion 未启用时）通过此注入获得漏斗语气校准。
            try:
                _fstage = (context or {}).get("funnel_stage") or ""
                _fscore = (context or {}).get("intimacy_score")
                if _fstage:
                    from src.contacts.relationship_stager import stage_directive
                    _directive = stage_directive(_fstage, _fscore)
                    if _directive:
                        user_context["_funnel_directive"] = _directive
                        self.logger.debug(
                            "[3M] funnel_directive stage=%s score=%s",
                            _fstage, _fscore,
                        )
            except Exception:
                self.logger.debug("relationship_stager inject skipped", exc_info=True)

            # Phase ②：关系成长「厚度/里程碑」感知块（默认关，companion.bond_level.enabled）。
            # 复用上面已取的 intimacy_score；只在 intimate/steady 或刚达成里程碑时产出一句，
            # 由 build_bond_level_block 内部克制（initial/warming 无里程碑 → 空，不打扰）。
            try:
                _bl_cfg = ((self.config.config or {}).get("companion") or {}).get(
                    "bond_level"
                ) if hasattr(self.config, "config") else None
                if _bl_cfg and _bl_cfg.get("enabled", False):
                    from src.contacts.relationship_level import build_bond_level_block
                    # Phase ④：基础 intimacy + 剧情累计加成（完成深度剧情真实推动 bond）
                    _bscore = self._effective_intimacy(user_context, _chat_id)
                    if _bscore is None:
                        _bscore = (context or {}).get("intimacy_score")
                    _days_known = (context or {}).get("relationship_days")
                    # Phase ④续：一次性消费剧情完成纪念点（持久于 user_context，致意后清除）
                    _fresh = (
                        user_context.pop("bond_fresh_milestone", None)
                        or (context or {}).get("bond_fresh_milestone")
                    )
                    _bl_block = build_bond_level_block(
                        _bscore,
                        days_known=_days_known,
                        fresh_milestone=_fresh,
                    )
                    if _bl_block:
                        user_context["_bond_level_block"] = _bl_block
            except Exception:
                self.logger.debug("bond_level inject skipped", exc_info=True)

            # Phase ③：活动剧情 → 注入当前 beat 的【剧情场景】导演指令（默认关）。
            try:
                _scfg = self._story_cfg()
                if _scfg.get("enabled", False):
                    _sstate = self._get_story_state(user_context, _chat_id)
                    if _sstate:
                        from src.skills.story_engine import build_story_prompt_block
                        _sblk = build_story_prompt_block(
                            _sstate, self._story_scenarios())
                        if _sblk:
                            user_context["_story_block"] = _sblk
            except Exception:
                self.logger.debug("story inject skipped", exc_info=True)

            # 命理技能（companion.bazi，默认关）：聊到算命/运势 → 注入命盘参考或
            # 顺势采集生辰 directive（注入而非短路——命理融进人设对话，不切报告腔）。
            try:
                self._inject_bazi_context(
                    user_context, text, user_id_str, _chat_id,
                    user_context.get("platform", ""),
                )
            except Exception:
                self.logger.debug("bazi inject skipped", exc_info=True)

            # 跨平台档案叙事（contacts.origin_profile，默认关）：客户从哪个平台来/
            # 在那边聊过什么 → _origin_block（与 B 线 3c3 同口径；A 线 chat_key
            # 即 peer id，与 contacts hooks 记账的 external_id 同源）。
            self._inject_origin_context(
                user_context,
                platform=user_context.get("platform", ""),
                account_id=str(user_context.get("account_id") or ""),
                chat_key=str(user_id_str or ""),
            )

            # 人设长传记检索（personas.bio_retrieval，默认关）：客户追问人设长尾
            # 细节 → 关键词命中原始文档块才注入（不命中零开销；注入而非短路）。
            try:
                self._inject_persona_bio_context(user_context, text)
            except Exception:
                self.logger.debug("persona bio inject skipped", exc_info=True)

            # 营销目标（companion.goals，默认关）：会话有活跃目标 → 注入「今日拍」
            # 方向块（settle-on-read；情绪低落/沉默熔断时 service 侧自动 hold）。
            # account_id 显式透传：多账号同 peer（同 chat_key 不同协议号）各有目标时
            # 精确命中本账号的那条，防串号（store 侧无 account 时才回落宽匹配）。
            self._inject_goal_context(
                user_context,
                platform=user_context.get("platform", ""),
                chat_key=str(_chat_id),
                account_id=_acct_id,
                chain="reply",
                inbound_text=text,
            )

            # 回复新鲜感（2026-08-02，默认双关）：出站口头禅账本 → 多样性
            # 硬约束 + 今日话题包（冷场素材）。enabled=false 时零调用零开销
            # （方法内先判配置再算）；出站文本就地取 _conversation_history
            # assistant 侧 + last_reply。
            self._inject_reply_freshness(
                user_context, text,
                convo_key=(
                    f"{user_context.get('platform', '')}:{_acct_id}:{_chat_id}"),
            )

            from src.hooks.registry import HookRegistry as _HR
            _hooks = _HR.get_instance()

            # 3b. 会话级语言决策（lang_policy 单一事实源）——治三类语言事故：
            # ① 用户明确说「用日语聊」被无视（请求内容从不参与决策）
            # ② 中文会话发一个「whatsapp」整条回复翻成英文（中性词被当英语强证据）
            # ③ 单条误判直接改写 reply_lang 并粘住后续轮次
            # 决策优先级：明确请求(持久偏好) > 强证据立即跟随 > 弱证据粘住上一轮。
            # 若调用方提供了 _current_user_message_for_lang（单条主消息），优先以此判断，
            # 避免 [对方连发] 合并文本中的英文干扰中/日文消息的语言判断。
            # ★ 锁是「单次请求级」语义——只认本次调用 context 里的锁；user_context 里
            # 可能残留旧调用（收件箱 draft）写入的 True，若采信会永久跳过语言决策。
            if context.get("reply_lang_locked"):
                user_context["reply_lang_locked"] = True  # 本次调用生效（merge 已写，双保险）
            else:
                user_context.pop("reply_lang_locked", None)  # 清残留锁，恢复自动决策
                from src.ai.lang_policy import (
                    classify_evidence as _lang_classify,
                    resolve_conversation_language as _lang_resolve,
                )
                _lang_detect_src = (
                    (context.get("_current_user_message_for_lang") or "").strip() or text
                )
                _prev_lang = str(user_context.get('reply_lang') or '').strip()
                _stripped = (text or "").strip()
                # Domain-specific ambiguous tokens (e.g. EP/JC)：无语言证据 →
                # 传空文本，策略自动粘住上一轮语言（旧逻辑的回看 last_message 不再需要，
                # 上一轮 reply_lang 本就由 last_message 决策而来）。
                if _hooks.is_ambiguous_token_message(_stripped):
                    _lang_detect_src = ""
                # ★ 时序补齐（E2E 验收实测发现）：_conversation_history 滞后一轮——
                # 上一轮的用户消息要到本轮 3b **之后**的 K1 块才补录进历史。偏好漂移
                # 释放需要「最近一条用户消息」参与连续段判断，否则释放比收件箱产线
                # （传入含当前消息的完整历史）晚一轮。把 last_message（= 上一轮用户
                # 消息）临时并入扫描窗口——只影响本次决策，不写回历史。
                _lang_hist = list(user_context.get("_conversation_history") or [])
                _lm_prev = str(user_context.get("last_message") or "").strip()
                if _lm_prev and _lm_prev != _stripped:
                    _lang_hist.append({"role": "user", "content": _lm_prev})
                # #95 语音轮语言锚（实施91，0830 停电期实锤：中文客户语音被回落
                # 转写器转出韩语乱码 → 旧链当强证据采信 → 韩语回话+韩语 TTS）。
                # 判定纯函数 lang_policy.voice_turn_lang_suspect（与 #74 图片轮
                # 同族原则、与 WA 线 voice_lang_suspect 同契约）：转写语种 ≠ 会话
                # 稳定语言 → 本条不参与语言决策（粘住会话语言）+ 置嫌疑标记
                # （prompt 走「听不清请确认」块）。换语言坐实通道：客户**文字**
                # 消息（非语音轮不进锚）/ 连续两条同语种语音（纯函数内豁免）。
                try:
                    _voice_turn = str(context.get("media_type") or "")\
                        .strip().lower() in ("voice", "audio")
                    if _voice_turn and _lang_detect_src:
                        from src.ai.lang_policy import voice_turn_lang_suspect
                        _vl_stable = (
                            str(user_context.get("user_lang_pref") or "").strip()
                            or _prev_lang)
                        if voice_turn_lang_suspect(
                                _lang_detect_src, stable_lang=_vl_stable,
                                prev_user_text=_lm_prev):
                            self.logger.info(
                                "%s[lang] 语音轮语言锚：转写语种偏离会话稳定"
                                "语言（=%s），本条不参与语言决策（#95）",
                                log_prefix, _vl_stable)
                            _lang_detect_src = ""
                            user_context["_voice_lang_suspect"] = True
                            try:
                                from src.monitoring.metrics_store import (
                                    get_metrics_store,
                                )
                                get_metrics_store().record_lang_event(
                                    "voice_suspect")
                            except Exception:
                                pass
                except Exception:
                    self.logger.debug("%s语音轮语言锚异常（按旧行为放行）",
                                      log_prefix, exc_info=True)
                # 初始语言先验（lang_prior，默认关）：新会话首条 "Hi"/emoji 类
                # 中性消息全链落空时，按账号配置/WA 国码给 default 供个先验语言
                # （治「给菲律宾新好友第一句回中文」）。仅无 prev_lang 时计算——
                # 会话一旦有语言状态，先验彻底让位。
                _default_lang = _prev_lang
                if not _default_lang:
                    try:
                        from src.ai.lang_prior import initial_lang_hint
                        _default_lang = initial_lang_hint(
                            platform=str(user_context.get("platform")
                                         or context.get("platform") or ""),
                            account_id=str(user_context.get("account_id")
                                           or context.get("account_id") or ""),
                            chat_key=str(_chat_id or ""),
                            config=(self.config.config
                                    if hasattr(self.config, "config") else {}),
                        )
                    except Exception:
                        _default_lang = ""
                _decision = _lang_resolve(
                    _lang_detect_src,
                    _lang_hist,
                    prev_lang=_prev_lang,
                    lang_pref=str(user_context.get("user_lang_pref") or ""),
                    lang_pref_input=str(user_context.get("user_lang_pref_input") or ""),
                    default=_default_lang or "zh",
                )
                # 先验实际生效（决策落到 default 且 default 来自 lang_prior）→ 埋点
                if _decision.source == "default" and _default_lang and not _prev_lang:
                    try:
                        from src.monitoring.metrics_store import get_metrics_store
                        get_metrics_store().record_lang_event("initial_prior")
                    except Exception:
                        pass
                # 间接表达 LLM 短判兜底（正则未命中 && 提及语言名 && 短消息）：
                # 覆盖 "my chinese is bad, can we not use it" 这类隐晦请求。
                # 廉价门控保证极低触发率；结果码经 valid_lang_code 校验防幻觉。
                if (
                    not _decision.request
                    and _lang_detect_src
                    and len(_stripped) <= 80
                    and self.ai_client is not None
                ):
                    try:
                        from src.ai.lang_policy import contains_language_alias
                        if contains_language_alias(_stripped):
                            _llm_req = await self._llm_judge_language_request(_stripped)
                            if _llm_req:
                                from src.ai.lang_policy import PolicyDecision as _PD
                                _decision = _PD(
                                    lang=_llm_req, source="explicit_request",
                                    request=_llm_req, stable=True,
                                )
                                try:
                                    from src.monitoring.metrics_store import get_metrics_store
                                    get_metrics_store().record_lang_event("llm_request_fallback")
                                except Exception:
                                    pass
                    except Exception:
                        self.logger.debug("%sLLM 语言请求短判跳过", log_prefix, exc_info=True)
                user_context['reply_lang'] = _decision.lang
                if _decision.request:
                    # 明确语言请求 → 持久偏好（随 ContextStore 落库），并记录
                    # 请求时的书写语言（漂移释放的豁免基准，防「中文求日语后
                    # 继续打中文」被误释放）。
                    user_context['user_lang_pref'] = _decision.request
                    user_context['user_lang_pref_input'] = (
                        _lang_classify(_stripped)[0] or ""
                    )
                    self.logger.info(
                        "%s语言请求命中: %r → %s（已持久为会话偏好）",
                        log_prefix, _stripped[:40], _decision.request,
                    )
                    try:
                        from src.monitoring.metrics_store import get_metrics_store
                        get_metrics_store().record_lang_event("explicit_request")
                    except Exception:
                        pass
                elif _decision.source == "stable_switch":
                    # 用户稳定漂移到新语言 → 释放旧偏好，回到跟随模式
                    user_context.pop('user_lang_pref', None)
                    user_context.pop('user_lang_pref_input', None)
                    self.logger.info(
                        "%s语言偏好释放: 稳定漂移 → %s", log_prefix, _decision.lang,
                    )
                    try:
                        from src.monitoring.metrics_store import get_metrics_store
                        get_metrics_store().record_lang_event("stable_switch")
                    except Exception:
                        pass

            # 3b2. B67「发→X」显式铆定 ≻ 一切自动语言决策（#106 实施91，0831
            # 击穿实锤：会话铆「发→英」，图片轮仍三连纯中文——A 线从无 B67 消费
            # 点）。生成端直接按铆定语言写稿（root fix）；发送口另有文字系统
            # 冲突兜底翻译（sendpoint_lang_pin_fix，第二道）。变体铆定
            # （zh-tw/yue）生成按 zh 写，字形转换归翻译层（实施89 契约：检测端
            # 不产变体码、OpenCC/引擎管字形）。锁定分支（reply_lang_locked）
            # 同样覆写——铆定是坐席显式声明，优先级高于调用方预设。
            try:
                from src.ai.sendpoint_guard import outbound_lang_pin
                _b67_pin = outbound_lang_pin(
                    str(user_context.get("platform")
                        or context.get("platform") or "telegram"),
                    str(_acct_id or "default"), str(_chat_id or ""))
                if _b67_pin:
                    _b67_gen = {"zh-tw": "zh", "zh-hk": "zh",
                                "yue": "zh"}.get(_b67_pin, _b67_pin)
                    if _b67_gen and _b67_gen != str(
                            user_context.get("reply_lang") or ""):
                        self.logger.info(
                            "%s[lang] B67 出站铆定生效：reply_lang %s → %s"
                            "（#106，explicit 优先）", log_prefix,
                            user_context.get("reply_lang"), _b67_gen)
                        user_context["reply_lang"] = _b67_gen
                        try:
                            from src.monitoring.metrics_store import (
                                get_metrics_store,
                            )
                            get_metrics_store().record_lang_event(
                                "outbound_pin")
                        except Exception:
                            pass
            except Exception:
                self.logger.debug("%sB67 铆定读取异常（按原决策）",
                                  log_prefix, exc_info=True)

            # 携带上条语音的声学情绪（SER）→ 供情感上下文/危机联动/出站语气共用。
            _pae = context.get("_peer_audio_emotion") if context else None
            if _pae:
                user_context["_peer_audio_emotion"] = _pae

            # 4. Intent recognition + domain hook override
            text_stripped = text.strip()
            if (
                "order_query" in self.skills
                and last_intent == "order_query"
                and text_stripped.isdigit()
                and 6 <= len(text_stripped) <= 24
            ):
                intent = "order_query"
            else:
                intent = self._recognize_intent(text)

            # Domain hook: allow domain pack to override intent
            from src.hooks.base import HookContext as _HookCtx
            _hook_ctx = _HookCtx(
                text=text, user_id=user_id_str, chat_id=str(_chat_id),
                intent=intent, last_intent=last_intent,
                last_message=user_context.get("last_message", ""),
                last_reply=user_context.get("last_reply", ""),
                reply_lang=user_context.get("reply_lang", "zh"),
                user_context=user_context,
                extra={"available_skills": set(self.skills.keys())},
            )
            intent = await _hooks.dispatch_intent_resolved(intent, _hook_ctx)

            # Intent inheritance: direct_chat fallback inherits recent business intent
            # 且上条是具体业务意图且在 120 秒内，继承上条意图以保持多轮连贯
            _INHERITABLE_INTENTS = {
                k for k in (
                    "order_query", "channel_info", "complaint", "status_check", "price_check",
                ) if k in self.skills
            }
            if (intent == "direct_chat"
                    and last_intent in _INHERITABLE_INTENTS
                    and user_context.get("last_message_time")
                    and (time.time() - user_context["last_message_time"]) < 120):
                if _hooks.is_meaningless_interjection(text_stripped):
                    self.logger.debug(
                        "%s意图继承跳过: 纯语气词/无实质内容 '%s'",
                        log_prefix, text_stripped[:20],
                    )
                else:
                    intent = last_intent
                    self.logger.info(f"{log_prefix}意图继承: direct_chat -> {intent}（上条意图 {last_intent}，120s 内追问）")

            self.logger.debug(f"{log_prefix}识别到意图: {intent} (消息: {text[:50]}...)")

            if not self._narrow_reply_allows(text, intent, last_intent, user_context):
                self.logger.warning("%snarrow_reply: 非允许范围，跳过回复 (intent=%s)", log_prefix, intent)
                return None

            # 4b 按意图冷却：配置 by_intent 时，距上次回复不足N秒数则跳过
            need_gap = self.cooldown_by_intent.get(intent)
            _tp = context.get('_trigger_path')
            if need_gap and isinstance(need_gap, (int, float)) and need_gap > 0:
                _exempt = False
                if (
                    "order_query" in self.skills
                    and intent == "order_query"
                    and text_stripped.isdigit()
                    and 6 <= len(text_stripped) <= 24
                ):
                    _exempt = True
                if _tp and _tp in ("l1_rule", "mention", "reply_chain"):
                    _exempt = True
                if user_context.get("_bot_question_ts") and (time.time() - user_context.get("_bot_question_ts", 0)) < 120:
                    _exempt = True
                if context.get('triggered_by_mention'):
                    _exempt = True
                if not _exempt:
                    last_rt = user_context.get('last_reply_time') or 0
                    now = time.time()
                    if now - last_rt < float(need_gap):
                        self.logger.warning(
                            f"{log_prefix}意图 {intent} 处于 by_intent 冷却期 ({need_gap}s)，距上次回复 {now-last_rt:.1f}s，跳过"
                        )
                        return None

            _saved_prev_message = user_context.get('last_message', '')
            _saved_prev_reply = user_context.get('last_reply', '')

            # 时间断层感知：距上一轮的间隔（秒），供 inbound_enrich 生成【时间提示】——
            # 隔了 10 天回来，历史窗口里的旧轮次不该被当"刚才"（真实事故：AI 把
            # 10 天前的话题当刚聊过，"你刚才说想去大阪玩"）。必须在 update 前取旧值。
            _prev_msg_ts = float(user_context.get('last_message_time') or 0)
            user_context['_turn_gap_sec'] = (
                (time.time() - _prev_msg_ts) if _prev_msg_ts > 0 else 0.0
            )

            user_context.update({
                'last_message': text,
                'last_message_time': time.time(),
                'current_intent': intent
            })

            # H3: 意图链跟�?�?记录去重意图序列 + 模式识别
            _intent_chain = user_context.get('_intent_chain', [])
            if not isinstance(_intent_chain, list):
                _intent_chain = []
            if not _intent_chain or _intent_chain[-1] != intent:
                _intent_chain.append(intent)
            if len(_intent_chain) > 15:
                _intent_chain = _intent_chain[-10:]
            user_context['_intent_chain'] = _intent_chain
            # Case-center（2026-08-03）：陪聊域立案信号（保守词表，宁漏不误）。
            # 明确要人工 / 怀疑是机器人 → 开案或升级现有案例；软失败零阻断。
            try:
                from src.utils.case_center import (
                    detect_ai_doubt, detect_human_request, open_case,
                )
                if detect_human_request(text):
                    open_case(user_context, user_id_str, "human_request",
                              "case.reason.human_request", quote=text)
                elif detect_ai_doubt(text):
                    open_case(user_context, user_id_str, "ai_doubt",
                              "case.reason.ai_doubt", quote=text)
            except Exception:
                self.logger.debug("%scase 信号检测跳过", log_prefix, exc_info=True)
            _chain_hint = self._detect_chain_pattern(_intent_chain)
            if _chain_hint:
                user_context['_chain_pattern'] = _chain_hint
                try:
                    from src.utils.case_center import chain_reason, open_case
                    _cc_code, _cc_params = chain_reason(
                        str(_chain_hint.get("pattern") or ""),
                        str(_chain_hint.get("desc") or ""))
                    open_case(user_context, user_id_str, "intent_chain",
                              _cc_code, _cc_params, quote=text)
                except Exception:
                    if not user_context.get('_case_id'):
                        user_context['_case_id'] = f"CASE-{user_id_str[-6:]}-{int(time.time()) % 100000}"
                # Q-35 #316：占位符 4 个 / 实参 3 个（第 4 参曾被乱码注释掉）→ 每次命中都
                # 抛 `--- Logging error --- not enough arguments for format string`（4HK54G）。
                self.logger.info(
                    "%s意图链模式 %s case=%s chain=%s",
                    log_prefix, _chain_hint["pattern"],
                    user_context.get('_case_id', ''),
                    " -> ".join(str(x) for x in _intent_chain[-5:]),
                )

            # K1: 对话历史窗口 + 摘�压缩（保留最�?3 ����?+ 早期摘��?
            _conv_hist = user_context.get('_conversation_history', [])
            if not isinstance(_conv_hist, list):
                _conv_hist = []
            # Fix D: retroactive sanitize on loaded history（修复自我强化幻觉污染；失败不阻断流程）
            try:
                _conv_hist = self._sanitize_history_name_claims(_conv_hist, user_context)
            except Exception as _se1:
                self.logger.debug("sanitize_history skipped: %s", _se1)
            if _saved_prev_message and _saved_prev_reply:
                try:
                    _clean_prev_reply = self._sanitize_assistant_reply(_saved_prev_reply, user_context)
                except Exception as _se2:
                    self.logger.debug("sanitize_prev_reply skipped: %s", _se2)
                    _clean_prev_reply = _saved_prev_reply
                _conv_hist.append({"role": "user", "content": _saved_prev_message[:200]})
                _conv_hist.append({"role": "assistant", "content": _clean_prev_reply[:300]})
            _KEEP_VERBATIM = 5  # 与 reply 策略 context_rounds≈5 对齐，避免本地先裁成 3 轮导致模型「失忆」
            _COMPRESS_THRESHOLD = 8  # 更长对话才摘要，减少过早丢轮次
            # 上下文深度档（ai.context_depth 深度/最大/超大）抬高逐字保留轮数；standard 零变化
            try:
                from src.ai import context_depth as _cd
                _KEEP_VERBATIM = _cd.verbatim_rounds(self.config, _KEEP_VERBATIM)
                _COMPRESS_THRESHOLD = _cd.compress_threshold(_KEEP_VERBATIM, _COMPRESS_THRESHOLD)
            except Exception:
                pass
            _total_rounds = len(_conv_hist) // 2
            if _total_rounds > _COMPRESS_THRESHOLD:
                _old_msgs = _conv_hist[:(-_KEEP_VERBATIM * 2)]
                # ★ Phase 2：默认用 LLM 摘要（更连贯），rule-based 作 fallback
                _summary = await self._summarize_history_with_fallback(_old_msgs)
                _conv_hist = _conv_hist[-_KEEP_VERBATIM * 2:]
                user_context['_conversation_summary'] = _summary
            elif _total_rounds > _KEEP_VERBATIM:
                _conv_hist = _conv_hist[-_KEEP_VERBATIM * 2:]
            user_context['_conversation_history'] = _conv_hist

            # 3b. 入站媒体 / 短消息 / 多语言切换补全。
            # 原生 Telegram 直接回复与收件箱 auto-draft 都在这里对齐到同一套
            # “真人感”上下文：贴纸/图片别当空文本，短消息短回，语言切换可自然点出来。
            try:
                from src.inbox.inbound_enrich import apply_inbound_enrichments
                apply_inbound_enrichments(
                    user_context,
                    text=text,
                    history=_conv_hist,
                    reply_lang=str(user_context.get("reply_lang") or ""),
                    media_type=str(
                        context.get("media_type") or context.get("_media_kind") or ""
                    ),
                    media_ref=str(
                        context.get("media_ref") or context.get("_media_ref") or ""
                    ),
                    media_desc=str(
                        context.get("media_desc")
                        or context.get("_media_desc")
                        or context.get("image_ocr_text")
                        or ""
                    ),
                    platform=str(context.get("platform") or ""),
                )
            except Exception:
                self.logger.debug("%s入站上下文补全跳过", log_prefix, exc_info=True)

            # 追问回填 �?用户新消���达，回溯标�前一条策略事�?
            _chat_id = _safe_int_chat_id(context.get("chat_id", 0))
            try:
                self._strategy_tracker.backfill_follow_up(user_id_str, _chat_id, intent)
            except Exception:
                pass

            # 4a. 解析回复策略（支持 A/B 灰度分流）
            strategy, strategy_id = self.get_strategy_for_intent(intent, user_id_str)
            strategy = strategy or {}

            # LINE RPA（私聊）：可选整策略覆盖 + 默认跳过「静默概率」以免实测/私聊被 S5 随机吞掉
            if context.get("channel") == "line_rpa" and hasattr(
                self.config, "get_line_rpa_config"
            ):
                lr = self.config.get_line_rpa_config() or {}
                ro = (lr.get("reply_strategy_override") or "").strip()
                if ro and (self._strategies or {}).get(ro, {}).get("enabled", True):
                    strategy = dict((self._strategies or {}).get(ro) or {})
                    strategy_id = ro
                    self.logger.info(
                        "%sLINE RPA reply_strategy_override -> %s",
                        log_prefix,
                        strategy_id,
                    )
                if lr.get("skip_silent_probability", True):
                    strategy = dict(strategy)
                    strategy["reply_probability"] = 1.0

            # Messenger RPA（1v1 私聊）：同 LINE 一致的整策略覆盖路径
            # 默认跳过 skip_ai（复读机的根源），并默认跳过 S5 静默概率
            if context.get("channel") == "messenger_rpa" and hasattr(
                self.config, "get_messenger_rpa_config"
            ):
                mr = self.config.get_messenger_rpa_config() or {}
                ro = (mr.get("reply_strategy_override") or "").strip()
                if ro and (self._strategies or {}).get(ro, {}).get("enabled", True):
                    strategy = dict((self._strategies or {}).get(ro) or {})
                    strategy_id = ro
                    self.logger.info(
                        "%sMessenger RPA reply_strategy_override -> %s (was intent=%s)",
                        log_prefix,
                        strategy_id,
                        intent,
                    )
                # 不允许 skip_ai（即使 override 没配，也强制让 greeting 走 AI，避免复读机）
                if mr.get("disable_skip_ai_templates", True):
                    strategy = dict(strategy)
                    if strategy.get("skip_ai"):
                        self.logger.info(
                            "%sMessenger RPA: disable skip_ai (intent=%s) → 走 AI 生成",
                            log_prefix,
                            intent,
                        )
                    strategy["skip_ai"] = False
                if mr.get("skip_silent_probability", True):
                    strategy = dict(strategy)
                    strategy["reply_probability"] = 1.0

            # WhatsApp RPA（1v1 私聊）：同 Messenger RPA 一致，强制 reply_probability=1.0 + 禁止 skip_ai
            if context.get("channel") == "whatsapp_rpa":
                strategy = dict(strategy)
                strategy["skip_ai"] = False
                strategy["reply_probability"] = 1.0

            # S5 静默观察：按概率决定不回复
            # 触发系统已判定应回复（_trigger_path 存在）或 @ 时，跳过概率检查
            rp = strategy.get('reply_probability')
            if rp is not None and isinstance(rp, (int, float)) and rp < 1.0:
                _tp = context.get('_trigger_path')
                if context.get('triggered_by_mention') or _tp:
                    self.logger.info(f"{log_prefix}触发���={_tp or 'mention'}，跳�?S5 概率�€�?")
                elif self._outbound_unlimited():
                    # outbound.unlimited_mode：S5 概率静默属业务频控，一键放行
                    self._record_outbound_bypass("s5_probability")
                    self.logger.info(f"{log_prefix}策略 {strategy_id} S5 概率 {rp} 已被 unlimited_mode 放行")
                elif random.random() > rp:
                    self._record_outbound_block("business", "s5_probability", context)
                    self.logger.warning(f"{log_prefix}策略 {strategy_id} 静默跳过 (概率 {rp})")
                    return None

            user_context['_reply_strategy'] = strategy
            user_context['_reply_strategy_id'] = strategy_id

            # �€�€ 隐式反��€�?�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€
            # 若上�€条是 AI 回�，本条是用户���应（3 分钟内），�测情����?
            _pending_fb = user_context.get("_awaiting_kb_feedback")
            if _pending_fb and isinstance(_pending_fb, dict):
                _fb_ts = _pending_fb.get("ts", 0)
                if time.time() - _fb_ts < 180:  # 3 分钟窗口
                    _signal = self._detect_implicit_feedback(text)
                    if _signal:
                        try:
                            _kb2 = self._kb_store_if_exists()
                            if _kb2:
                                _kb2.add_feedback({
                                    "user_message": _pending_fb.get("user_msg", ""),
                                    "ai_reply":     _pending_fb.get("ai_reply", ""),
                                    "score":        1 if _signal == "pos" else -1,
                                    "correction":   text if _signal == "neg" else "",
                                    "operator":     "auto_detect",
                                })
                                self.logger.debug("隐式反�已��? %s", _signal)
                        except Exception as _fb_err:
                            self.logger.debug("隐式反�记录失败: %s", _fb_err)
                user_context.pop("_awaiting_kb_feedback", None)

            # �€�€ KB 混合�€�?�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€
            #策略：BM25 优先，弱命中时懒触发向量搜索（节�?~70% Embedding API 调用�?
            _hook_ctx.intent = intent
            _, _skip_kb_for_channel_metrics = await _hooks.dispatch_kb_pre_search(text, _hook_ctx)
            if _skip_kb_for_channel_metrics:
                self.logger.info(
                    "%sKB 跳过：域包 hook 指示跳过 KB 搜索（意图=%s）",
                    log_prefix, intent,
                )
            try:
                if not _skip_kb_for_channel_metrics:
                    _kb = self._kb_store_if_exists()
                else:
                    _kb = None
                # ── A1 KB 注入守门（2026-07-22，真机事故复盘）────────────────
                # ① 识图/识视频描述文本不是用户提问 → 整体跳过 KB（环境词噪声
                #    实测把「怪味胡豆图」撞上「客户端安装指引」）。
                # ② 显式绑定陪聊人设（chat_binding/account_profile）→ 抑制业务 KB
                #    （护士人设推销 USDT 付款客服号事故；人设 kb_access: true 可恢复）。
                if _kb is not None:
                    from src.utils.kb_gate import (
                        is_media_desc_text,
                        lexical_overlap_ok,
                        persona_kb_suppressed,
                        should_log_kb_miss,
                    )
                    if is_media_desc_text(text):
                        _kb = None
                        self.logger.info(
                            "%sKB 跳过：入站为媒体识别描述（识图/识视频），非用户提问",
                            log_prefix,
                        )
                    else:
                        try:
                            from src.utils.persona_manager import PersonaManager
                            _kbg_p, _kbg_tier = (
                                PersonaManager.get_instance().get_persona_with_tier(
                                    str(context.get("chat_id", "") or ""),
                                    str((user_context or {}).get(
                                        "account_persona_id") or ""),
                                )
                            )
                            if persona_kb_suppressed(_kbg_p, _kbg_tier):
                                _kb = None
                                self.logger.info(
                                    "%sKB 跳过：陪聊人设会话（tier=%s）抑制业务 KB 注入",
                                    log_prefix, _kbg_tier,
                                )
                        except Exception:
                            self.logger.debug(
                                "%sKB 人设守门解析失败，放行", log_prefix, exc_info=True)
                if _kb:
                    _lang = (user_context or {}).get("reply_lang", "zh")

                    # Step 1: BM25 先�
                    _bm25_result = _kb.search(text, top_k=3, lang=_lang)
                    _top_bm25_score = (
                        _bm25_result["entries"][0].get("_score", 0)
                        if _bm25_result["entries"] else 0
                    )

                    # ③ 词汇重叠守门：加权 BM25 原始分与 0.30 阈值口径不匹配（实测
                    # 14~292，一字重叠即"强命中"）。分数外再要求足够实词重叠（≥2 个
                    # 非停用实词或单个 ≥4 字强词），否则视为弱命中 → 交向量语义裁决。
                    _bm25_top_entry = (
                        _bm25_result["entries"][0]
                        if _bm25_result.get("entries") else None
                    )
                    _overlap_ok, _overlap_why = (
                        lexical_overlap_ok(text, _bm25_top_entry)
                        if _bm25_top_entry is not None else (False, "no_entries")
                    )

                    if _top_bm25_score >= _BM25_STRONG_THRESHOLD and _overlap_ok:
                        _search_result = _bm25_result
                        self.logger.info(
                            "%sKB BM25 强命中(%.3f, %s)，跳过向量化",
                            log_prefix, _top_bm25_score, _overlap_why,
                        )
                    else:
                        if _top_bm25_score >= _BM25_STRONG_THRESHOLD:
                            self.logger.info(
                                "%sKB BM25 分数达标(%.3f)但词汇重叠不足(%s) → 向量语义裁决",
                                log_prefix, _top_bm25_score, _overlap_why,
                            )
                        #BM25 弱命�?�?尝试向量搜索（懒触发，最多等 8 秒，防� Embedding API 挂起�?
                        try:
                            _query_vec = await asyncio.wait_for(
                                self._get_embedding_cached(text), timeout=8.0
                            )
                        except asyncio.TimeoutError:
                            self.logger.warning("KB Embedding 调用超时（>8s），降级为纯 BM25")
                            _query_vec = None
                        if _query_vec:
                            _search_result = _kb.search(
                                text, top_k=3, lang=_lang, query_vec=_query_vec
                            )
                            _mode = _search_result.get("search_mode", "bm25")
                            self.logger.info(
                                "%sKB 混合搜索模式 %s，BM25原始=%.3f",
                                log_prefix, _mode, _top_bm25_score
                            )
                        elif _overlap_ok:
                            _search_result = _bm25_result
                        else:
                            # 词汇重叠不足且向量不可用 → 宁可不注入，绝不让一字
                            # 重叠的无关条目污染提示词（USDT 串台事故根因）。
                            _search_result = {"entries": [], "search_mode": "bm25"}
                            self.logger.info(
                                "%sKB 放弃注入：BM25=%.3f 重叠=%s 且向量不可用",
                                log_prefix, _top_bm25_score, _overlap_why,
                            )

                    _kb_ctx = _kb.build_ai_context_from_result(_search_result, lang=_lang)
                    _hit = bool(_kb_ctx)
                    _mode = _search_result.get("search_mode", "bm25")
                    _cat  = (_search_result["entries"][0]["category"]
                             if _search_result.get("entries") else "")

                    _matched_eid = (_search_result["entries"][0].get("id", "")
                                    if _search_result.get("entries") else "")

                    if _hit:
                        _top_entry = _search_result["entries"][0] if _search_result.get("entries") else {}
                        _top_title = _top_entry.get("title", "")
                        _top_reply_mode = _top_entry.get("reply_mode", "ai_guided")

                        # Guard: channel_info 意图时，跳过通道数据相关的 direct 条目
                        _CH_BLOCK_CATS = {"通道状态"}
                        _CH_BLOCK_KW = ("成功率", "费率", "额度", "限额", "代收", "代付",
                                        "通道状态", "channel")
                        _is_ch_intent = intent in ("channel_info", "status_check")
                        _e_blob = f"{_top_entry.get('category', '')} {_top_title} {_top_entry.get('triggers', '')}".lower()
                        _e_is_ch = (
                            _top_entry.get("category") in _CH_BLOCK_CATS
                            or any(kw in _e_blob for kw in _CH_BLOCK_KW)
                        )
                        if _is_ch_intent and _e_is_ch and _top_reply_mode == "direct":
                            self.logger.info(
                                "%sKB direct 跳过（通道数据由程序化回复处理）: '%s'",
                                log_prefix, _top_title,
                            )
                            _top_reply_mode = "ai_guided"

                        if _is_ch_intent and not _e_is_ch and _top_reply_mode == "direct":
                            self.logger.info(
                                "%sKB direct 降级（channel_info意图但KB条目非通道类）: '%s'",
                                log_prefix, _top_title,
                            )
                            _top_reply_mode = "ai_guided"

                        # E2a: 非中文用户 direct 降级为 ai_guided，让 AI 翻译
                        if _top_reply_mode == "direct" and _lang != "zh":
                            self.logger.info(
                                "%sKB direct 降级 ai_guided（非中文 lang=%s）: '%s'",
                                log_prefix, _lang, _top_title,
                            )
                            _top_reply_mode = "ai_guided"

                        # E2b: 短查询或低分 direct 降级为 ai_guided
                        _KB_DIRECT_MIN_SCORE = 0.45
                        _KB_DIRECT_MIN_QUERY_LEN = 6
                        if _top_reply_mode == "direct":
                            _text_len = len(text.strip())
                            if _top_bm25_score < _KB_DIRECT_MIN_SCORE:
                                self.logger.info(
                                    "%sKB direct 降级 ai_guided（BM25分数 %.3f < %.2f）: '%s'",
                                    log_prefix, _top_bm25_score, _KB_DIRECT_MIN_SCORE, _top_title,
                                )
                                _top_reply_mode = "ai_guided"
                            elif _text_len < _KB_DIRECT_MIN_QUERY_LEN:
                                self.logger.info(
                                    "%sKB direct 降级 ai_guided（短查询 len=%d < %d）: '%s'",
                                    log_prefix, _text_len, _KB_DIRECT_MIN_QUERY_LEN, _top_title,
                                )
                                _top_reply_mode = "ai_guided"

                        # E2: direct 模式 �?跳过 AI；支�?reply_direct_spec（�€�道/分支/片�/受控����?
                        if _top_reply_mode == "direct":
                            _cfg_dir = Path(self.config.config_path).parent if hasattr(
                                self.config, "config_path"
                            ) else Path("config")
                            try:
                                from src.utils.kb_direct_render import render_kb_direct_reply
                                _direct, _dm = await render_kb_direct_reply(
                                    _top_entry, text, _cfg_dir, self.ai_client
                                )
                            except Exception as _dr_err:
                                self.logger.warning("KB direct 渲染异常，降�?legacy: %s", _dr_err)
                                from src.utils.kb_direct_render import legacy_direct_text
                                _direct = legacy_direct_text(_top_entry)
                                _dm = {"path": ["error", str(_dr_err)]}
                            if _direct:
                                # companion guard: skip KB 直出 for companion domain + non-biz messages
                                _cfg_gd = self.config.config if hasattr(self.config, "config") else {}
                                _is_comp_gd = (
                                    isinstance(_cfg_gd, dict)
                                    and effective_domain_name(_cfg_gd) == "conversion"
                                )
                                if _is_comp_gd:
                                    _biz_kws_gd = (
                                        "通道", "订单", "查单", "费率", "代收", "代付",
                                        "成功率", "限额", "回调", "转账", "支付",
                                        "channel", "order", "payment", "payin", "payout",
                                    )
                                    _has_biz_gd = any(k in (text or "") for k in _biz_kws_gd)
                                    _comp_intents = ("greeting", "small_talk", "direct_chat", "complaint")
                                    if intent in _comp_intents and not _has_biz_gd:
                                        self.logger.info(
                                            "%s companion: skip KB direct 直出 (intent=%s no biz kw)",
                                            log_prefix, intent,
                                        )
                                        _direct = None  # fall through → normal AI with persona
                                if _direct:
                                    _kb.inc_use_count(_matched_eid)
                                self.logger.info(
                                    "%sKB direct 命中 [%s] '%s' �?直出 path=%s branch=%s router=%s",
                                    log_prefix, _mode, _top_title,
                                    _dm.get("path"), _dm.get("branch"), _dm.get("router"),
                                )
                                try:
                                    _kb.log_query(
                                        text, hit=True, search_mode=_mode,
                                        category=_cat, lang=_lang,
                                        score=_top_bm25_score,
                                        matched_entry_id=_matched_eid,
                                    )
                                except Exception:
                                    pass
                                if _direct is not None:
                                    return _direct
                                # companion cleared _direct -> fall through to normal AI

                        _skip_kb_inject = _is_ch_intent and not _e_is_ch
                        if not _skip_kb_inject:
                            user_context["kb_context"] = _kb_ctx
                        else:
                            self.logger.info(
                                "%sKB context skipped (channel_info but KB not channel): '%s'",
                                log_prefix, _top_title,
                            )
                        user_context["_kb_search_mode"] = _mode
                        self.logger.info(
                            "%sKB 命中 [%s] %s (score=%.3f mode=%s) → 注入 %d 字符",
                            log_prefix, _mode, _top_title,
                            _top_bm25_score, _top_reply_mode, len(_kb_ctx)
                        )
                        # M4: 非中文用户命���翻译条目时，记录翻译�€�?
                        if _lang != "zh" and _matched_eid:
                            try:
                                _entry_data = _search_result["entries"][0]
                                _has_trans = bool(_entry_data.get(f"example_reply_{_lang}"))
                                if not _has_trans:
                                    _kb.log_miss(f"[TRANSLATE:{_lang}:{_matched_eid}] {_top_title}")
                                    self.logger.info(
                                        "%s翻译缺口: entry=%s lang=%s",
                                        log_prefix, _matched_eid, _lang
                                    )
                            except Exception:
                                pass
                    else:
                        # 学习漏斗入池守门：占位符/闲聊不进池（2026-08-02，
                        # 陪聊语句 cnt 恒 1 灌爆 top_k、占位符生成荒谬草稿的复盘）
                        if should_log_kb_miss(text):
                            _kb.log_miss(text)
                        self.logger.info(
                            "%sKB 未命中 (BM25=%.3f) msg='%s'",
                            log_prefix, _top_bm25_score, text[:30]
                        )

                    #写入查�日志（含分数 + 匹配条目ID，用于弱命中分析�?
                    try:
                        _kb.log_query(
                            text, hit=_hit,
                            search_mode=_mode, category=_cat, lang=_lang,
                            score=_top_bm25_score,
                            matched_entry_id=_matched_eid,
                        )
                        _EMBED_STATS["kb_queries"] += 1
                        if _hit:
                            _EMBED_STATS["kb_hits"] += 1
                    except Exception:
                        pass
            except Exception as _kb_err:
                self.logger.warning("KB 检索失败（非阻塞）: %s", _kb_err)

            # Domain hook: inject live状态（仅支付等业务域；陪聊域不注入，避免模型主动推销查通道）
            _cfg_dom = self.config.config if hasattr(self.config, "config") else {}
            _companion_dom = isinstance(_cfg_dom, dict) and effective_domain_name(_cfg_dom) == "conversion"
            _live_status = None if _companion_dom else _hooks.get_channel_status_info()
            if _live_status:
                user_context["channel_status_info"] = _live_status
            else:
                user_context.pop("channel_status_info", None)

            if _companion_dom:
                _biz_kw = (
                    "通道", "订单", "查单", "费率", "代收", "代付", "成功率", "限额", "回调",
                    "转账", "支付", "channel", "order", "payment", "payin", "payout",
                )
                _raw_t = text or ""
                _user_biz = any(k in _raw_t for k in _biz_kw)
                # 报障群豁免（bug_intake，2026-08-18 实测）：这道闸的语义是
                # 「防陪聊人设推销支付话术」，但值守群的 KB 就是产品答案本体
                # ——「怎么登录」的正确 KB 命中曾被它丢弃成泛答。
                _bug_group_kb_exempt = False
                try:
                    from src.ops.bug_intake import is_bug_group
                    _bug_group_kb_exempt = is_bug_group(_cfg_dom, _chat_id)
                except Exception:
                    _bug_group_kb_exempt = False
                if (intent in ("greeting", "small_talk", "direct_chat",
                               "complaint")
                        and not _user_biz and not _bug_group_kb_exempt):
                    if user_context.pop("kb_context", None):
                        self.logger.info(
                            "%s companion: dropped KB inject for intent=%s (no biz keywords)",
                            log_prefix, intent,
                        )

            # 4b. 选择并执行技能
            skill = self._select_skill(intent, user_context)
            if not skill:
                self.logger.warning(f"{log_prefix}未找到适合意图 {intent} 的技能（channel=%s）", (context or {}).get('channel',''))
                return None

            # 4c-fix. 意图切换时：清理上文回�记忆，防AI����€话�"带偏"
            _prev_reply = (user_context.get("last_reply") or "").strip()
            _intent_switched = last_intent and intent != last_intent
            if _intent_switched and _prev_reply:
                # P0-G fix (2026-05-03): chat-class intents 同族，
                # 避免 messenger greeting/small_talk/direct_chat 切换
                # 触发 _conversation_history 清空 -> hist=0 冷启动 ->
                # 角色错乱、cross-chat persona 串戏、hallucinate.
                _INTENT_FAMILIES = {
                    "channel": {"channel_info", "status_check"},
                    "order": {"order_query", "complaint"},
                    "chat": {
                        "greeting", "small_talk", "direct_chat",
                        "casual_chat", "chitchat", "free_chat",
                    },
                }
                _old_family = next((f for f, s in _INTENT_FAMILIES.items() if last_intent in s), last_intent)
                _new_family = next((f for f, s in _INTENT_FAMILIES.items() if intent in s), intent)
                if _old_family != _new_family:
                    user_context["_topic_switch_hint"] = (
                        # f"用户刚从「{last_intent}」话题切换到了「{intent}」话题。" f"请忘掉上一个话题的内容，100%专注回答当前话题。"
                    )
                    user_context.pop("last_reply", None)
                    user_context["_conversation_history"] = []
                    # ★ Phase 2：保留 _conversation_summary 跨话题切换（摘要承载长期事实）
                    user_context["_intent_chain"] = [intent]
                    user_context.pop("_chain_pattern", None)
                    # Case-center：话题切换不再销案——案例跟人不跟话题；生命周期
                    # （结案→归档→复发新案）由 src/utils/case_center 统一管理。
                    self.logger.info(f"{log_prefix}话�切换: {last_intent} �?{intent}，已清理上文记忆+对话历史+摘�+意图�?")

            # Domain hook: short followup detection (e.g. channel status brief reply)
            _pr_follow = (user_context.get("last_reply") or "").strip()
            _fc = _hooks.get_followup_config()
            _followup_intents = _fc.get("followup_intents", set())
            if (
                intent in _followup_intents
                and last_intent in _followup_intents
                and _pr_follow
                and _hooks.last_reply_looks_like_summary(_pr_follow)
                and _hooks.is_short_followup(text_stripped)
            ):
                user_context["_channel_followup_brief"] = True
                self.logger.info("%s域包短追问检测: 注入简短回复约束", log_prefix)
            else:
                user_context.pop("_channel_followup_brief", None)

            # 4d. 角度���系统：连���意图+相似�消息强制切换表达角度 + 拟人�?
            _prev_reply = (user_context.get("last_reply") or "").strip()
            _prev_msg = (_saved_prev_message or "").strip()
            _same_intent = intent == last_intent and _prev_reply
            _msg_similar = self._reply_similarity(text, _prev_msg) > 0.40 if (_same_intent and _prev_msg) else False
            _consecutive_same = _same_intent and _msg_similar
            _angle_idx = 0
            if _consecutive_same:
                _angle_idx = user_context.get("_consecutive_same_intent", 0) + 1
                user_context["_consecutive_same_intent"] = _angle_idx
            else:
                if _same_intent and not _msg_similar and _prev_msg:
                    self.logger.info(f"{log_prefix}同意图但不同义: '{_prev_msg[:15]}' → '{text[:15]}'，计数器重置")
                user_context["_consecutive_same_intent"] = 0

            # M2: 重�提问质量追踪 �?用户重��?= 上�回�没解决问�?
            if _consecutive_same and _angle_idx >= 2 and _prev_reply:
                try:
                    _qkb = self._kb_store_if_exists()
                    if _qkb:
                        _qkb.add_feedback({
                            "user_message": _prev_msg[:200],
                            "ai_reply":     _prev_reply[:300],
                            "score":        -1,
                            # "correction":   f"用户第{_angle_idx}次重复提����€�",
                            "operator":     "auto_repeat_detect",
                        })
                        self.logger.info(
                            "%s重复提问质量反馈: 第%d次追问, msg='%s'",
                            log_prefix, _angle_idx, text[:25]
                        )
                except Exception:
                    pass

            _frustration_signals = ("到底", "怎么回事", "为什么", "有没有人", "还要等",
                                      "什么时候", "催", "急", "投诉", "你们", "搞什么",
                                      "坑", "骗", "不行", "烂", "垃圾", "服了", "无语")
            _is_frustrated = any(w in text for w in _frustration_signals) and _angle_idx >= 2
            _needs_escalation = _consecutive_same and (_angle_idx >= 5 or _is_frustrated)

            if _needs_escalation:
                if _companion_dom:
                    user_context["_anti_repeat_hint"] = (
                        "对方已经连着说了好几次，可能有点烦或委屈。你要：\n"
                        "1. 先贴一下情绪，别讲道理抢话\n"
                        "2. 换种说法陪她把同一件事说完，别复制粘贴上一条\n"
                        "3. 两三句就够，可以轻轻问一句「想我陪你换个话题缓一下吗」\n"
                        "4. 不要主动扯工作、订单、通道、支付。\n"
                    )
                else:
                    user_context["_anti_repeat_hint"] = (
                        "对方情绪有些低落，多追问了好几次。以你的人设自然回应：\n"
                        "1. 先表达感受到了，比如'感觉你现在挺烦的'\n"
                        "2. 坦诚但温柔地说：'我能说的就这些了，不想糊弄你'\n"
                        "3. 轻轻提个别的话头或换个方向聊聊\n"
                        "4. 简短自然，两三句就好，不要正式腔。"
                    )
                so = user_context.get("_reply_strategy") or {}
                so["temperature"] = 0.6
                user_context["_reply_strategy"] = so
                self.logger.info(f"{log_prefix}情绪升级�€�? 追问#{_angle_idx} frustrated={_is_frustrated}，建���人工")

            elif _consecutive_same:
                if _angle_idx >= 3:
                    user_context["_anti_repeat_hint"] = (
                        "对方一直在问同一件事，你已经说过了。按照你的性格自然应对：\n"
                        "- 补一个之前没提过的小细节（如果有）\n"
                        "- 或者轻松问一句'是哪个地方没说明白吗？'\n"
                        "- 绝对不要一字不差重复之前的话。一两句搞定。"
                    )
                    so = user_context.get("_reply_strategy") or {}
                    so["temperature"] = min(float(so.get("temperature", 0.7)) + 0.2, 1.0)
                    user_context["_reply_strategy"] = so
                    self.logger.info(f"{log_prefix}重�追问 #{_angle_idx}: 注入�€���认指�?")
                else:
                    _ROTATION = _hooks.get_reply_angle_rotation()
                    _DEFAULT_ANGLES = [
                        "换一种完全不同的开头和语气来回答。",
                        "用更简洁直接的方式回答，像老朋友对话。",
                    ]
                    _angles = _ROTATION.get(intent, _DEFAULT_ANGLES)
                    _angle = _angles[(_angle_idx - 1) % len(_angles)]
                    user_context["_anti_repeat_hint"] = _angle
                    so = user_context.get("_reply_strategy") or {}
                    base_temp = float(so.get("temperature", 0.7))
                    so["temperature"] = min(base_temp + 0.1 * min(_angle_idx, 2), 1.0)
                    user_context["_reply_strategy"] = so
                    self.logger.info(f"{log_prefix}角度��� #{_angle_idx}: {_angle[:40]}...")

            await self._maybe_slow_think(intent, text, user_context, log_prefix)

            self.logger.info(f"{log_prefix}执行 {intent} (消息: {text[:30]}...)")
            # 5. 执行技能（无执行超时上限，见下方 2026-08-22 注）
            # 2026-08-22 老板指令：取消技能执行超时上限。背景＝primary=local_only 期间
            # 本地主链慢（冷载/排队 10~65s+），旧 45s wait_for 把已在生成的回复整轮丢弃
            # ＝客户视角「已读不回」（08-22 凌晨三连实录，轮询兜底补答那次也被同一闸掐死）。
            # 回复宁可迟到、绝不因人为时限被丢弃；时长天然上界由 LLM 客户端自身 HTTP
            # 超时兜底（ai.timeout / ai.fallback.timeout + 有限重试），不再加第二道闸。
            _t0 = time.time()
            reply = await skill.execute(text, user_id_str, user_context)
            _elapsed_ms = int((time.time() - _t0) * 1000)

            # 5b. 相似度�测：如果回�与上条重复度 >65%，强指令重试
            # 无论用户消息是否相似，只要 bot 即将重复自己的上条回复就应重试
            if reply:
                _win, _thr = self._anti_repeat_params()
                _combined, _is_rep, _sim, _ssim = await self._anti_repeat_score(
                    reply, user_context)
                if _is_rep:
                    self.logger.info(
                        f"{log_prefix}回�相似�?{_sim:.0%}，强制重试换角度")
                    user_context["_anti_repeat_hint"] = (
                        f"1. 换一个完全不同的开头（禁止用上次的前5个字）\n"
                        f"2. 换一个不同的重点（上次说了什么，这次说别的）\n"
                        f"3. 风格要有明显区别，就像换了个心情在聊"
                    )
                    so = user_context.get("_reply_strategy") or {}
                    so["temperature"] = min(float(so.get("temperature", 0.85)) + 0.15, 1.0)
                    user_context["_reply_strategy"] = so
                    try:
                        from src.monitoring.metrics_store import get_metrics_store
                        get_metrics_store().record_anti_repeat_rewrite_attempt()
                    except Exception:
                        pass
                    try:
                        retry_reply = await asyncio.wait_for(
                            skill.execute(text, user_id_str, user_context),
                            timeout=30.0
                        )
                        if retry_reply:
                            _rc, _, _sim2, _ = await self._anti_repeat_score(
                                retry_reply, user_context, record=False)
                            if _rc < _combined:
                                reply = retry_reply
                                try:
                                    from src.monitoring.metrics_store import get_metrics_store
                                    get_metrics_store().record_anti_repeat_rewrite_adopted()
                                except Exception:
                                    pass
                                self.logger.info(
                                    f"{log_prefix}重试成功: 新相似度 {_sim2:.0%}")
                            else:
                                self.logger.info(
                                    f"{log_prefix}重试����?({_sim2:.0%})，保留原回�")
                    except asyncio.TimeoutError:
                        self.logger.warning(f"{log_prefix}重试超时，保留原回�")
                user_context.pop("_anti_repeat_hint", None)
            user_context.pop("_topic_switch_hint", None)
            user_context.pop("_media_coherence_hint", None)
            user_context.pop("_media_pending_hint", None)
            user_context.pop("_song_coherence_hint", None)
            user_context.pop("_current_scene_note", None)
            user_context.pop("_media_sent_note", None)

            # 5c0. LLM 发图指令（2026-07-14 决策权上移）：解析并剥净 [PHOTO …] 标记；
            # llm/hybrid 模式下有效指令 → 准入闸门后真出图（PuLID 锁脸）随正文发出。
            # 关键词 Stage A/B 未命中而 LLM 理解了要图时，这里是新的救回通道。
            # 任何模式都剥净标记——泄漏给客户=穿帮。
            if reply:
                reply = await self._apply_photo_directive(
                    reply, user_id_str, user_context, _chat_id,
                    log_prefix=log_prefix)

            # 5c. 人设一致性守卫：剥离 LLM 漏出的禁用语/AI 自我暴露（保护陪聊沉浸感）
            if reply:
                reply = self._enforce_persona_consistency(
                    reply,
                    chat_id=str(_chat_id if _chat_id not in (None, "") else context.get("chat_id", "") or ""),
                    account_persona_id=str(user_context.get("account_persona_id", "") or ""),
                    log_prefix=log_prefix,
                    # 错误自称名守卫的对方名锚点（A 线经 _sm_context 透传，2026-08-08）
                    peer_name=str((context or {}).get("_peer_display_name") or ""),
                    # B74：生成时 prompt 锁定名（ai_client 写回）同源直传白名单
                    resolved_name=str((context or {}).get("_resolved_persona_name") or ""),
                )

            # 5c1b. 出站文本形态守卫（实施74：B118 括号独白 / B121 语种混杂 /
            # B104 无出处引用）——确定性最后防线，紧跟人设守卫（同为「出稿形态」
            # 层，先于媒体/危机语义层）。user_context 供 B104 判「新联系人」。
            if reply:
                reply = self._apply_outbound_text_guard(
                    reply, log_prefix=log_prefix, user_context=user_context)

            # 5c2. 出站媒体承诺守卫：走到这=本轮没有真发媒体（Stage 全未短路），
            # 回复却承诺「等我拍/发你照片/发条语音」→ 异步兑现（Phase18，预检过
            # 则保留承诺+后台真拍真发/失败补台阶）→ 否则句级剥离（剥空换婉转话术）。
            # 判定方（客户入站关键词）与承诺方（LLM 出站文本）从不核对是实录事故根因。
            # 例外：5c0 刚按 LLM 指令真发了图（_photo_just_sent）——正文说「刚拍的」
            # 是真话，不剥。（无条件 pop：即使 reply 为空也必须清标志，防残留到下一轮）
            _photo_sent_now = bool(user_context.pop("_photo_just_sent", False))
            if reply and not _photo_sent_now:
                reply = self._apply_media_promise_guard(
                    reply, user_context, log_prefix=log_prefix,
                    user_id_str=user_id_str, chat_id=_chat_id, user_text=text)

            # 5c2s. 文字假唱出站守卫（实施66 P0-3，确定性最后防线）：hint 是
            # 概率性防御（2026-08-23 实锤：高压逼唱上下文里 LLM 会突破劝导，
            # 用文字演完整套假唱）。表演体命中 → 能真唱：先发真唱段再剥假唱
            # 文字（把谎变真，LLM 自己就是漏检兜底的检测器）；不能：句级剥离，
            # 剥空换台阶句。跑在 voice_reply 之前＝顺带防 TTS 念歌词回魂。
            if reply:
                reply = await self._apply_song_claim_guard(
                    reply, user_context, chat_id=_chat_id, user_text=text,
                    log_prefix=log_prefix)

            # 报障群回执 footer（bug_intake）：工单号必须确定性出现在回复里
            # （代码拼接，不信 LLM 复述）；放媒体守卫之后=终稿层追加。
            try:
                if (reply and _bug_intake_res
                        and _bug_intake_res.get("footer")):
                    _bi_ft = str(_bug_intake_res["footer"])
                    if _bi_ft not in reply:
                        reply = reply.rstrip() + "\n\n" + _bi_ft
            except Exception:
                pass

            # 5c2b. 时空接轨守卫：当地墙钟 vs 出站时段问候/错城「我在 X」
            # （温哥华深夜说下午好 / 说人在宿务 —— 2026-07 穿帮）。
            if reply:
                reply = self._apply_world_clock_guard(
                    reply, user_context, log_prefix=log_prefix)

            # 5c2c. 反编造共同往事守卫（P1 2026-08-03，从主动触达平移到应答链）：
            # 零上下文的首轮（新会话 / bot 会话）里 LLM 会为了热络凭空断言「你以前
            # 拽我去打球」这类根本不存在的共同经历——真人一眼识破，还会反噬之前所有
            # 真实感。依据池刻意宽收（长期记忆+历史+摘要+**用户本条消息**——客户自己
            # 提起往事时 AI 复述是合法对话），有任何依据即退到只计数不动手；只有真·
            # 零依据才句级剥离。剥空如实回落原文（应答不能失语，价值在被看见）。
            if reply:
                try:
                    from src.utils.proactive_fabrication_guard import (
                        build_precise_evidence,
                        build_reply_evidence,
                        record_fabrication_guard,
                        strip_fabricated_sentences,
                    )
                    _fab_text, _fab_info = strip_fabricated_sentences(
                        reply, build_reply_evidence(user_context, text),
                        precise_evidence=build_precise_evidence(user_context))
                    record_fabrication_guard(_fab_info, source="a_line")
                    if _fab_info.get("stripped"):
                        self.logger.warning(
                            "%s[fabrication_guard] 零依据编造往事，剥离 %d 句%s：%r",
                            log_prefix, len(_fab_info["stripped"]),
                            "（剥空→回落原文）"
                            if _fab_info.get("all_stripped") else "",
                            _fab_info["stripped"][:2])
                    reply = _fab_text
                except Exception:
                    self.logger.debug(
                        "%s[fabrication_guard] 守卫异常（放行原文）",
                        log_prefix, exc_info=True)
                # 5c2d. 近况陈述编造守卫（#208 L-1 C）：往事守卫管「我们过去…」，这条管
                # 「我此刻…」——无来源的天气/地点行程/正在做的事句级剥离（FTK6S7）。
                reply = self._apply_status_fabrication_guard(
                    reply, user_context, text, log_prefix=log_prefix, source="a_line")

            # 5c3. 带货链接纪律守卫（P4）：soft/hold 日 LLM 从历史复读出官网
            # 下单链 → 按当日 CTA 档确定性剥离（读 _goal_cta 后即焚）。
            if reply:
                reply = self._apply_goal_link_guard(
                    reply, user_context, log_prefix=log_prefix)

            # 5c4. 骂战/记仇轮出站否决（P1-b）：剥认输句/秒原谅句——指令层
            # 服从性不足时的确定性最后防线（危机兜底仍是最后一道，在其前）。
            if reply:
                reply = self._apply_temper_output_guard(
                    reply, user_context, log_prefix=log_prefix)

            # 5d. 危机事后兜底（R6）：回复自身触自伤红线 → 覆盖安全兜底；
            #     severe 危机可选补附求助资源。预防(R4)+兜底(R6) 双保险。
            if reply:
                reply = self._apply_crisis_safety_net(
                    reply, user_context=user_context, log_prefix=log_prefix,
                )

            # 5e. 危机人工接管/升级（R8）：severe 连续命中 → 触发 handoff 告警（默认关）。
            #     机器兜底之上让真人介入——自动陪聊对真实危机最负责任的处理。
            self._maybe_escalate_crisis(
                user_id=user_id_str, chat_id=_chat_id,
                user_context=user_context, log_prefix=log_prefix,
            )

            if reply:
                # 设置隐式反�等待标志（KB 上下文注入过说明知识库参与了�回��?
                if user_context.get("kb_context"):
                    user_context["_awaiting_kb_feedback"] = {
                        "user_msg":  text[:200],
                        "ai_reply":  reply[:300],
                        "ts":        time.time(),
                    }
                # 维护
                # _bot_question_ts 追问窗口�?                # (A) 回�����?�?新建/刷新窗口
                # (B) 当前意图�€�道类且窗口尚在 �?刷新（用户可继续�?EP/JC 等其他�€�道�?                # (C) 其他情况 �?
                _reply_ends_with_q = reply.strip().endswith("？") or reply.strip().endswith("?")
                _in_channel_conv = intent in ("channel_info", "status_check") and bool(
                    user_context.get("_bot_question_ts")
                    and (time.time() - user_context.get("_bot_question_ts", 0)) < 120
                )
                if _reply_ends_with_q or "���" in reply or "吗？" in reply:
                    user_context["_bot_question_ts"] = time.time()
                    user_context["_bot_question_intent"] = intent
                elif _in_channel_conv:
                    # 通道多轮对话：EP 后继续问 JC，刷新窗口时间但保留 intent
                    user_context["_bot_question_ts"] = time.time()
                else:
                    user_context.pop("_bot_question_ts", None)
                    user_context.pop("_bot_question_intent", None)
                # 6. 更新状�€?
                self._update_after_reply(reply, user_id_str, user_context,
                                         chat_id=context.get('chat_id', ''),
                                         user_msg=text)
                # P3-deep diag (2026-05-04)
                _flag_dis = user_context.get("disable_episodic_memory")
                self.logger.info(
                    "[episodic] handle_msg checkpoint user=%s intent=%s "
                    "reply_len=%d disable_flag=%s",
                    user_id_str, intent, len(reply or ""), _flag_dis,
                )
                if not _flag_dis:
                    self._schedule_episodic_memory_extract(
                        user_id_str, text, reply, intent, _chat_id,
                        platform=user_context.get("platform", ""),  # S5
                        account_id=str(user_context.get("account_id") or ""),
                    )
                # J1: escalation suggestion via domain hook
                if user_context.pop("_escalation_triggered", False):
                    reply += _hooks.get_escalation_line()
                tracker = context.get("_event_tracker")
                if tracker:
                    tracker.track(
                        event_type=intent,
                        chat_id=_safe_int_chat_id(context.get("chat_id", 0)),
                        user_id=user_id_str,
                        detail=text[:100],
                        response_ms=_elapsed_ms,
                    )
                try:
                    from src.monitoring.metrics_store import get_metrics_store
                    get_metrics_store().record_skill_hit(intent)
                except Exception:
                    pass
                # 策略效果追踪
                try:
                    _used_ai = not strategy.get("skip_ai", False)
                    _model_id = strategy.get("model", "") or (
                        self.ai_client.model if hasattr(self.ai_client, "model") else "")
                    self._strategy_tracker.record(
                        strategy_id=strategy_id,
                        intent=intent,
                        user_id=user_id_str,
                        chat_id=_chat_id,
                        response_ms=_elapsed_ms,
                        used_ai=_used_ai,
                        model_id=_model_id,
                    )
                except Exception:
                    pass
                # Auto-Pilot 周期性��?
                self._autopilot_msg_counter += 1
                if self._autopilot_msg_counter >= self._autopilot_check_interval:
                    self._autopilot_msg_counter = 0
                    try:
                        self._run_autopilot()
                    except Exception as _ap_err:
                        self.logger.debug("Auto-Pilot �€查异�? %s", _ap_err)
                return reply
            else:
                self.logger.info(f"{log_prefix}�€�?{intent} 返回空，不回复（�€查技能�€�辑�?AI ���返回空）")
                return None

        except Exception as e:
            self.logger.error(f"处理消息失败: {e}")
            return None
        finally:
            if _cr_scope_a is not None:
                try:
                    _cr_scope_a.__exit__(None, None, None)
                except Exception:
                    pass
            if user_ctx_for_cleanup is not None:
                user_ctx_for_cleanup.pop("_slow_think_outline", None)
                for _rk in ("_route", "_route_strict", "_thinking", "_unrestricted",
                            "_unrestricted_bypass_safety", "_conv_route", "_route_offline"):
                    user_ctx_for_cleanup.pop(_rk, None)

    async def generate_inbox_draft(
        self,
        *,
        text: str,
        chat_key: str,
        platform: str,
        history: Optional[List[Dict[str, Any]]] = None,
        persona_id: str = "",
        reply_lang: str = "",
        channel: str = "inbox",
        risk_level: str = "",
        media_type: str = "",
        media_ref: str = "",
        media_desc: str = "",
        conversation_id: str = "",
        peer_audio_emotion: Optional[Dict[str, Any]] = None,
        account_id: str = "",
        agent_instruction: str = "",
        inbound_msg_id: str = "",
        extra_hint: str = "",
    ) -> Optional[Dict[str, Any]]:
        """收件箱草稿生成的「统一规则引擎」（单一事实源）。

        与 ``process_message`` 共用全部规则 helper（情景记忆读写、情感智能上下文、
        陪伴关系阶段推进、慢思考、人设守卫、危机兜底、回复后状态推进），让**全自动/
        手动收件箱草稿**与原生 bot/RPA 产线规则**一次性对齐**。

        与 ``process_message`` 的两点关键差异（这正是本方法存在的理由）：

          1. **无状态历史**：收件箱对话历史以 ``inbox.db`` 为权威，每次按需重算。
             故本方法用调用方传入的 ``history`` **覆盖** ``_conversation_history``，
             不依赖、也不污染 ``process_message`` 自维护的流式历史累积。
          2. **零发送副作用**：不走「自拍/形象照直接发媒体」「冷却/ S5 静默跳过回复」
             这些会绕过收件箱风险闸（L2/L3/L4）或吞掉草稿的分支——是否真正外发由
             下游 autosend 风险闸决定，本方法只负责「拟一条对齐全部规则的草稿」。

        关系/记忆状态按 ``account_id:chat_key`` 分桶持久化（与 A 线 ContextStore
        同口径），同 peer 对不同协议号互不串上下文。

        ``agent_instruction``（P22）：坐席显式指令进 ``_agent_instruction`` 高权重块；
        空=旧行为（全自动路径不受影响）。

        ``extra_hint``（P0 2026-08-12 时间推理）：调用方（persona_reply 锚点判定）
        产出的语境提示（当前时刻锚点/迟回复/复读重写指令），追加进
        ``_topic_switch_hint`` 既有消费口；空=旧行为。

        返回 ``{"reply": str, "intent": str}``；无法生成时返回 ``None``（调用方回落）。
        """
        text = str(text or "").strip()
        if not text or not self.ai_client:
            return None
        user_id = str(chat_key or "").strip()
        if not user_id:
            return None
        log_prefix = "[inbox_draft] "
        chat_id = ""  # 记忆 key == platform:chat_key（与注入/写回/历史 backfill 一致）
        _acct_id = str(account_id or "").strip()
        if (not _acct_id or _acct_id == "default") and conversation_id:
            # conversation_id = platform:account:chat_key
            _parts = str(conversation_id).split(":", 2)
            if len(_parts) >= 3 and _parts[1]:
                _acct_id = str(_parts[1]).strip()

        def _metric(_name: str) -> None:
            """规则栈生效埋点（best-effort，绝不影响生成）。"""
            try:
                from src.monitoring.metrics_store import get_metrics_store
                get_metrics_store().record_inbox_draft_event(_name)
            except Exception:
                pass

        def _skipg(_layer: str) -> bool:
            """无限制会话是否跳过某一守卫层（conv_route.skip_guard；标准会话恒 False）。"""
            try:
                from src.ai.conv_route import skip_guard as _sg
                return _sg(user_context, _layer)
            except Exception:
                return False

        _t0 = time.time()

        # 风险分档：低风险（占自动发送大头）走快路省延迟，仅中/高风险吃满全栈。
        # 调用方传入 risk_level（单一事实源）优先；缺省时用廉价关键词分类兜底（纯正则，无 LLM）。
        _eff_risk = str(risk_level or "").strip().lower()
        if not _eff_risk:
            try:
                from src.inbox.drafts import keyword_risk_level
                _eff_risk = (keyword_risk_level(text) or "low")
            except Exception:
                _eff_risk = "low"
        try:
            _cfg_fp = self.config.config if hasattr(self.config, "config") else {}
            _fast_path_on = bool(
                ((_cfg_fp.get("inbox") or {}).get("auto_draft") or {})
                .get("fast_path_low_risk", True)
            ) if isinstance(_cfg_fp, dict) else True
        except Exception:
            _fast_path_on = True
        _is_fast_path = _fast_path_on and _eff_risk not in ("high", "medium")

        try:
            self._refresh_strategies()
        except Exception:
            pass

        # 双号隔离：与 process_message 同键（account_id:chat_key）
        user_context = self._get_user_context(user_id, account_id=_acct_id)
        # P22：坐席显式指令（「采纳并拟稿」）。只在本轮 user_context 上挂，不污染持久化
        # ContextStore——_get_user_context 返回的是可变 dict，但指令是瞬时态，生成后清掉。
        _ainst = str(agent_instruction or "").strip()[:400]
        if _ainst:
            user_context["_agent_instruction"] = _ainst
        else:
            user_context.pop("_agent_instruction", None)
        if _acct_id:
            user_context["account_id"] = _acct_id
        if persona_id:
            user_context["account_persona_id"] = str(persona_id)
        if platform:
            user_context["platform"] = platform
        # 让 autodraft 路径也带上会话身份：chat_id 供人设解析（cid=— 修复 + 支持 chat 绑定），
        # conversation_id 供深度人设 store 层（与 ingest 写入同键，解锁关系画像/回指/经历/语义召回）。
        if chat_key:
            user_context["chat_id"] = str(chat_key)
        if conversation_id:
            user_context["conversation_id"] = str(conversation_id)
        # 会话级模型路由（conv_route，2026-09-12 composer 模型选择器）：读会话自选的
        # 模型档 / 深度 / 力度 / 思考开关写进 user_context（_route/_thinking/_unrestricted…），
        # 并开生成作用域（深度档 contextvar + persona 规则块让路）。标准默认＝只清键，零变化。
        # 作用域在下方 finally 关闭；这里不能用 with——函数体上千行，重缩进＝热区大改。
        _cr_scope = None
        try:
            from src.ai import conv_route as _cr
            _cr_route = _cr.attach(
                user_context, str(conversation_id or "")
                or _cr.conv_id(platform, _acct_id or "default", chat_key),
                config=self.config)
            if not _cr_route.is_default:
                _cr_scope = _cr.generation_scope(_cr_route, self.config)
                _cr_scope.__enter__()
                if _cr_route.unrestricted:
                    _metric("unrestricted")
        except Exception:
            self.logger.debug("%sconv_route 解析跳过", log_prefix, exc_info=True)
            _cr_scope = None
        # P3：B 线入站 mid → open_case 自吸（与 A 线 process_message 同键 user_msg_id）。
        # 每轮覆盖：本条才是立案触发点；旧值留着会把新案锚到上一轮气泡。
        _inbound_mid = str(inbound_msg_id or "").strip()
        if _inbound_mid and _inbound_mid not in ("0",):
            user_context["user_msg_id"] = _inbound_mid
        if peer_audio_emotion:
            user_context["_peer_audio_emotion"] = peer_audio_emotion
        # ASR P1：本条若是语音且转写被判「可疑」（协议入站 / AutoDraft 转写时按 last_meta
        # 登记到 asr_suspect 注册表，按会话 + 本条文本核对）→ prompt 走「可能听错 · 先确认」
        # 块；不可疑则清键（防跨稿驻留）。任何异常按不可疑处理。
        try:
            from src.inbox.asr_suspect import apply_to_user_context as _asr_sus_apply
            _asr_sus_apply(user_context, conversation_id=conversation_id, text=text)
        except Exception:
            user_context.pop("_voice_asr_suspect", None)
        if reply_lang:
            # 收件箱已是语言决策单一事实源 → 锁定，避免引擎二次猜测
            user_context["reply_lang"] = reply_lang
            user_context["reply_lang_locked"] = True

        # P5：权益懒解析（与 A 线同款同键——B 线 user_id 即 chat_key，与
        # entitlement/tx_ledger 身份键一致）：让人审/自动发草稿也按会员档进
        # ai.tiers 分级（P4 级联在 ai_client 消费；5min TTL 随持久 user_context
        # 跨稿复用）。story 与 ai.tiers 全关时该方法直接 return，零开销。
        self._ensure_entitlement(user_id, user_context)

        # 生成层口语分叉（Phase G，B 线）：本草稿可能经 autosend 发语音 → 让 LLM
        # 同次调用多产 [口语版]（voice_autosend 投递时凭草稿哈希取用）。恒写键防
        # 持久 user_context 粘住旧 True；门控读 inbox.l2_autosend.voice 口径。
        try:
            from src.ai.spoken_variant import should_request_spoken_variant_autosend
            _cfg_sv = self.config.config if hasattr(self.config, "config") else {}
            user_context["_spoken_variant_request"] = (
                should_request_spoken_variant_autosend(
                    _cfg_sv,
                    peer_sent_voice=str(media_type or "").lower() in ("voice", "audio"),
                    text=text))
        except Exception:
            user_context["_spoken_variant_request"] = False

        # 历史：以 inbox 为权威覆盖（去掉末条「待回复」锚点，避免与本轮 text 重复）
        _hist: List[Dict[str, Any]] = []
        for _m in (history or []):
            if not isinstance(_m, dict):
                continue
            _c = str(_m.get("content") or "").strip()
            if not _c:
                continue
            _role = "user" if _m.get("role") == "user" else "assistant"
            _hist.append({"role": _role, "content": _c})
        if _hist and _hist[-1]["role"] == "user" and _hist[-1]["content"] == text:
            _hist = _hist[:-1]
        # 长会话历史摘要复用：超过阈值时把更早的消息压成摘要（承载早期长期事实，
        # 否则只取最近 N 条会「失忆」），最近 N 条逐字保留。摘要写入持久 user_context，
        # 并按已覆盖条数缓存——只在新增足够多（>=6 条）时才重算，避免每稿一次 LLM。
        _KEEP_VERBATIM = 10
        _COMPRESS_AT = 16
        # 上下文深度档（ai.context_depth）抬高逐字保留条数；standard 零变化
        try:
            from src.ai import context_depth as _cd
            _KEEP_VERBATIM = _cd.verbatim_msgs(self.config, _KEEP_VERBATIM)
            _COMPRESS_AT = _cd.compress_threshold(_KEEP_VERBATIM, _COMPRESS_AT)
        except Exception:
            pass
        # 接力记忆 P1-1：刚从人工接管交回（_handoff_note 在用）→ 逐字保留抬到接管起点，
        # 人工那几轮原话直接在窗口里，而不是刚切回就被 LLM 摘要改写掉。封顶 40 条。
        try:
            from src.inbox.handoff_memory import verbatim_keep_for_handoff
            _kv2 = verbatim_keep_for_handoff(history or [], user_context, _KEEP_VERBATIM)
            if _kv2 > _KEEP_VERBATIM:
                _KEEP_VERBATIM = _kv2
                _COMPRESS_AT = max(_COMPRESS_AT, _KEEP_VERBATIM + 3)
                _metric("handoff_verbatim_lift")
        except Exception:
            pass
        if len(_hist) > _COMPRESS_AT:
            _old = _hist[:-_KEEP_VERBATIM]
            _cached = (user_context.get("_conversation_summary") or "").strip()
            _upto = int(user_context.get("_inbox_summary_upto") or 0)
            if not (_cached and (len(_old) - _upto) < 6):
                try:
                    _summary = await self._summarize_history_with_fallback(_old)
                    if _summary:
                        user_context["_conversation_summary"] = _summary
                        user_context["_inbox_summary_upto"] = len(_old)
                        _metric("history_summarized")
                except Exception:
                    self.logger.debug("%s历史摘要跳过", log_prefix, exc_info=True)
            _hist = _hist[-_KEEP_VERBATIM:]
        user_context["_conversation_history"] = _hist

        try:
            # 1. 情景记忆注入（与 process_message step 3 同逻辑/同 key）
            if not user_context.get("disable_episodic_memory"):
                try:
                    _q_emb = None
                    _mvec = (self._memory_cfg or {}).get("vector") or {}
                    if (
                        self._episodic_store
                        and (self._memory_cfg or {}).get("enabled", True)
                        and _mvec.get("enabled", False)
                        and self.ai_client
                    ):
                        _q_emb = await self._embed_user_message_for_episodic(text)
                    self._inject_episodic_into_context(
                        user_context, user_id, chat_id,
                        current_user_text=text, query_embedding=_q_emb,
                        platform=platform,
                    )
                    if (user_context.get("_episodic_memory_text") or "").strip():
                        _metric("memory_hit")
                except Exception:
                    self.logger.debug("%s记忆注入跳过", log_prefix, exc_info=True)

            # 2. 情感智能上下文引擎（与 process_message step 3b 同逻辑）
            try:
                from src.utils.emotional_context import build_emotional_context_block
                _epi_text = (user_context.get("_episodic_memory_text") or "").strip()
                _cfg_es = self.config.config if hasattr(self.config, "config") else {}
                _es_on = bool(
                    ((_cfg_es.get("companion") or {}).get("empathy_strategy") or {})
                    .get("enabled", True)
                ) if isinstance(_cfg_es, dict) else True
                _wb_cfg = (
                    ((_cfg_es.get("companion") or {}).get("wellbeing") or {})
                    if isinstance(_cfg_es, dict) else {}
                )
                _emo_block = build_emotional_context_block(
                    text, user_context, _epi_text, chat_id=chat_id,
                    enable_strategy=_es_on,
                    enable_wellbeing=bool(_wb_cfg.get("enabled", True)),
                    enable_anti_sycophancy=bool(_wb_cfg.get("anti_sycophancy", True)),
                    wellbeing_hotline=str(_wb_cfg.get("crisis_resources", "") or ""),
                )
                if _emo_block:
                    user_context["_emotional_context_block"] = _emo_block
                    _metric("emotional_active")
            except Exception:
                self.logger.debug("%s情感上下文跳过", log_prefix, exc_info=True)

            # 3. 陪伴关系阶段（conversion 域；与 process_message P1 同逻辑）
            try:
                _cfg_dom = self.config.config if hasattr(self.config, "config") else {}
                _comp_cfg = (_cfg_dom.get("companion") or {}) if isinstance(_cfg_dom, dict) else {}
                if effective_domain_name(_cfg_dom) == "conversion" and _comp_cfg.get("enabled", True):
                    from src.utils.companion_relationship import (
                        build_relationship_prompt_block,
                        downgrade_from_user_text,
                        get_rel_state,
                    )
                    _rst = get_rel_state(user_context, chat_id)
                    downgrade_from_user_text(_rst, text, _comp_cfg)
                    _ain = ""
                    try:
                        _ain = str((self.config.get_ai_config() or {}).get("ai_name") or "").strip()
                    except Exception:
                        pass
                    user_context["_relationship_prompt_block"] = build_relationship_prompt_block(
                        _rst, _comp_cfg, ai_name=_ain, user_message=text,
                    )
                    user_context["relationship_stage"] = str(_rst.get("stage") or "")
                    if (user_context.get("_relationship_prompt_block") or "").strip():
                        _metric("companion_active")
            except Exception:
                self.logger.debug("%s陪伴关系跳过", log_prefix, exc_info=True)

            # 3b. 入站媒体 / 短消息 / 多语言切换（Telegram 收件箱与 RPA 对齐）
            # 先清上一轮残留：user_context 是持久 dict，而 enrich 只在「本轮有
            # hints」时才覆盖本键（A 线在 process_message 消费后 pop，B 线此前
            # 从不 pop）→ 本轮无 hints 时上一稿的语境提示会跨轮泄漏；下方 3b1b
            # 每轮必写时间锚点，不清则无限堆积进持久 context。
            user_context.pop("_topic_switch_hint", None)
            # #113：行程清除水位——运营在会话头点过「清除行程」后，早于水位的
            # 行程自述不再被当「当前状态」注入（enrich 内 self_claims 消费）。
            try:
                from src.integrations.protocol_bridge import (
                    get_inbox_store as _tc_gis,
                )
                user_context["_travel_cleared_ts"] = _tc_gis(
                ).get_travel_cleared_ts(conversation_id or "")
            except Exception:
                user_context["_travel_cleared_ts"] = 0.0
            try:
                from src.inbox.inbound_enrich import apply_inbound_enrichments
                apply_inbound_enrichments(
                    user_context,
                    text=text,
                    history=_hist,
                    reply_lang=str(reply_lang or user_context.get("reply_lang") or ""),
                    media_type=media_type,
                    media_ref=media_ref,
                    media_desc=media_desc,
                    platform=platform,
                )
            except Exception:
                self.logger.debug("%s入站 enrich 跳过", log_prefix, exc_info=True)

            # 3b1b. 时间推理提示（P0 2026-08-12，persona_reply 锚点判定产物）：
            # 当前时刻锚点/迟回复时间感/复读重写指令，追加进 _topic_switch_hint
            # 单一消费口（在 enrich 之后追加，不覆盖其语言/断层等既有提示）。
            _xh = str(extra_hint or "").strip()
            if _xh:
                _prev_hint = str(
                    user_context.get("_topic_switch_hint") or "").strip()
                user_context["_topic_switch_hint"] = (
                    f"{_prev_hint}\n{_xh}" if _prev_hint else _xh)
                _metric("time_hint_active")

            # 3b2. 发图协同 hint（文图一致性第一防线）：对方这条在要照片（或短肯定
            # 接受了上一轮的照片 offer）时，给草稿 LLM 讲清楚投递语义——图发成功则
            # 本草稿被**丢弃**（配文由 llm_caption 另生成）；图发失败才发本草稿。
            # 所以草稿必须写成「没有照片也成立」的兜底文本：不承诺去拍、不说已发出。
            # （出站承诺守卫在投递层仍兜底；这里是源头预防，减少撤回改写。）
            try:
                from src.inbox.image_autosend import (
                    plan_autosend_image,
                    resolve_image_autosend_cfg,
                )
                _iscfg = resolve_image_autosend_cfg(
                    self.config.config if hasattr(self.config, "config") else {})
                _wants_img = False
                if _iscfg.get("enabled", False):
                    _wants_img = bool(plan_autosend_image(text, _hist, _iscfg))
                    if not _wants_img and bool(
                            _iscfg.get("offer_accept_bridge", True)):
                        from src.ai.outbound_promise_guard import offer_accepted
                        _wants_img = offer_accepted(text, _hist) == "image"
                if _wants_img:
                    user_context["_media_coherence_hint"] = (
                        "对方在向你要照片。系统会尝试自动发送一张真实照片；"
                        "你现在写的这条文字只会在「照片没能发出」时才发送。"
                        "因此不要写「等我去拍」「马上发你」「照片来了」这类话；"
                        "就当这一轮发不了照片，用人设口吻自然回应"
                        "（可以撒娇、岔开话题或改天再说），也不要否认你能拍照。")
                    _metric("media_hint")
                elif not user_context.get("_media_coherence_hint"):
                    # 收图后质疑信号（P0，与 A 线同口径）：不是在要图 → 查是否在
                    # 质疑刚发的图（重复/不像/假图），命中则计数 + 设纠偏 hint。
                    self._maybe_flag_media_complaint(text, user_context)
            except Exception:
                self.logger.debug("%s发图协同 hint 跳过", log_prefix, exc_info=True)

            # 3b2s+. 专属歌订单 intake（实施58 P2）：客户求**定制**歌（写/唱
            # 一首关于我们的）→ 建订单（幂等：同会话活单去重+日帽）+「可以
            # 答应稍后唱」hint——与 photo async_fulfill 同哲学：**真会兑现**
            # （worker 填词渲染→人审→送达）才许承诺；绝不现场编词。
            try:
                from src.companion.song_orders import (
                    detect_custom_song_request as _dcsr,
                    get_order_store as _gos,
                    resolve_custom_cfg as _rcc,
                )
                _ccfg = _rcc(
                    self.config.config if hasattr(self.config, "config") else {})
                if _ccfg.get("enabled") and _dcsr(text):
                    _parts = str(conversation_id or "").split(":", 2)
                    _acct = _parts[1] if len(_parts) == 3 else ""
                    _facts: list = []
                    for _m in reversed(list(history or [])):
                        _dirn = str(_m.get("direction") or _m.get("role")
                                    or "in")
                        _txt = str(_m.get("text") or _m.get("content")
                                   or "").strip()
                        if _dirn in ("in", "user") and _txt:
                            _facts.append(_txt[:80])
                        if len(_facts) >= 4:
                            break
                    _oid = None
                    if _acct:
                        _oid = _gos().create_order(
                            platform=platform, account_id=_acct,
                            chat_key=str(chat_key), persona_id=persona_id or "",
                            request_text=str(text)[:400], facts=_facts,
                            daily_cap=int(_ccfg.get("daily_orders_cap", 10)))
                    _cid_full = conversation_id or ""
                    if _oid or (_acct and _gos().has_active(_cid_full)):
                        user_context["_song_coherence_hint"] = (
                            "对方在求一首专属定制歌。系统已登记制作订单"
                            "（会离线录制、人工审核后真的发给对方）。你可以"
                            "自然地答应「我给你写一首，录好了唱给你听」——"
                            "这是真的会兑现的承诺；但不要现场用文字编歌词，"
                            "也不要承诺具体时间点。")
                        _metric("custom_song_order")
                        try:
                            from src.companion.song_stock import get_song_stats
                            get_song_stats().bump("custom_ordered"
                                                  if _oid else "custom_repeat")
                        except Exception:
                            pass
            except Exception:
                self.logger.debug("%s专属歌 intake 跳过", log_prefix,
                                  exc_info=True)

            # 3b2s. 唱歌协同 hint（实施58 P1，与 3b2 发图协同同哲学）：对方在
            # 要歌时告诉草稿 LLM 投递语义——会真唱（autosend_song 短路）则本稿
            # 只是失败兜底；发不出则明令禁文字假唱/空头承诺。persona 用本次
            # 草稿的生效人设（行级 eff_persona 已在上游解析进 persona_id 形参）。
            # 定制订单 hint（3b2s+）已在场时让位——定制语义更具体。
            try:
                if not user_context.get("_song_coherence_hint"):
                    from src.companion.song_stock import (
                        SONG_STICKY_SEC as _song_win,
                        detect_song_request as _dsr_b,
                        song_coherence_hint as _schint,
                    )
                    # 粘性证据（实施66 P0-2）：30min 窗内的既往入站——「不行，
                    # 必须唱」这类追加逼唱靠它补判；压力=窗内 strict 次数+本条
                    # （与 autosend_song 同口径，冷却豁免两处同判防 hint 漂移）。
                    _sg_recent: list = []
                    try:
                        from src.integrations.protocol_bridge import (
                            get_inbox_store as _sg_gis,
                        )
                        _sg_now = time.time()
                        for _m_s in (_sg_gis().list_recent_messages(
                                conversation_id or "", limit=8) or []):
                            if str(_m_s.get("direction") or "") != "in":
                                continue
                            _ts_s = float(_m_s.get("ts") or 0)
                            if _ts_s and (_sg_now - _ts_s) > _song_win:
                                continue
                            _t_s = str(_m_s.get("text") or "")
                            if _t_s and _t_s != text:
                                _sg_recent.append(_t_s)
                    except Exception:
                        _sg_recent = []
                    _sg_pressure = 1 + sum(
                        1 for _t in _sg_recent if _dsr_b(_t))
                    _sg_hint = _schint(
                        self.config.config
                        if hasattr(self.config, "config") else {},
                        peer_text=text, persona_id=persona_id or "",
                        conv_id=conversation_id or "", can_deliver=True,
                        recent_texts=_sg_recent,
                        demand_pressure=_sg_pressure)
                    if _sg_hint:
                        user_context["_song_coherence_hint"] = _sg_hint
                        _metric("song_hint")
            except Exception:
                self.logger.debug("%s唱歌协同 hint 跳过", log_prefix, exc_info=True)

            # 3b3. 场景状态注入（Phase18 图文同源，与 A 线 3a2 同口径）：
            # 草稿文本与 autosend 生图共用同一个「AI 此刻在哪」。
            self._inject_scene_state(user_context)

            # 3b3b. 已知画像硬注入 + 禁复问（B50）与人设自述状态衔接（B52），
            # 与 A 线 3a2b 同口径（实施64 P1-2）。
            try:
                _kp_key = self._episodic_storage_key(
                    user_id, chat_id, platform, user_context=user_context)
                self._inject_known_profile(user_context, _kp_key)
            except Exception:
                self.logger.debug("%s已知画像注入跳过", log_prefix, exc_info=True)
            self._inject_self_state(user_context)

            # 3b4. 用户侧在地化（P2，默认关，与 A 线 3a3 同口径）。
            self._inject_peer_locale(user_context, user_id, chat_id, platform)

            # 3c. 命理技能（与 process_message 同规则；记忆 key 同 §1 的 user_id+chat_id 口径）
            try:
                self._inject_bazi_context(
                    user_context, text, user_id, chat_id, platform)
                if (user_context.get("_bazi_block") or "").strip():
                    _metric("bazi_active")
            except Exception:
                self.logger.debug("%s命理注入跳过", log_prefix, exc_info=True)

            # 3c2. 人设长传记检索（与 process_message 同规则）：追问人设长尾细节
            # → 关键词命中原始文档块才注入（不命中零开销）。
            try:
                self._inject_persona_bio_context(user_context, text)
                if (user_context.get("_persona_bio_block") or "").strip():
                    _metric("bio_block_active")
            except Exception:
                self.logger.debug("%s人设传记注入跳过", log_prefix, exc_info=True)

            # 3c3. 跨平台档案叙事（contacts.origin_profile，默认关）：客户从哪个
            # 平台来/在那边聊过什么话题域 → _origin_block（叙事层；具体记忆事实
            # 在 Stage 1 episodic 轨道，两层不重叠）。provider 未注册/关闸 = 零开销。
            self._inject_origin_context(
                user_context,
                platform=platform, account_id=_acct_id, chat_key=chat_key)
            if (user_context.get("_origin_block") or "").strip():
                _metric("origin_active")

            # 3d. 营销目标（companion.goals）：会话有活跃目标 → 注入「今日拍」方向块。
            # 必须在 3c 之后——service 会读 _bazi_block 判定同轮已有变现引导时把
            # direct 降 soft（防同一条回复双线推销）。
            self._inject_goal_context(
                user_context,
                platform=platform,
                chat_key=chat_key,
                account_id=account_id,
                conversation_id=conversation_id,
                chain="draft",
                inbound_text=text,
            )
            if (user_context.get("_goal_block") or "").strip():
                _metric("goal_active")

            # 3e. 回复新鲜感（2026-08-02，与 A 线同口径同消费口）：出站文本用
            # inbox 权威历史（调用方经 store.list_recent_messages 取得后传入，
            # 此处取**截断前**的 assistant 侧，比压缩后的 _conversation_history
            # 覆盖更全）；异常静默跳过，绝不阻塞拟稿。
            try:
                _fresh_outs: Optional[List[str]] = [
                    str(_m.get("content") or "") for _m in (history or [])
                    if isinstance(_m, dict) and _m.get("role") != "user"
                ]
            except Exception:
                _fresh_outs = None
            self._inject_reply_freshness(
                user_context, text,
                recent_outbound=_fresh_outs,
                convo_key=str(conversation_id or f"{platform}:{user_id}"),
            )
            if (user_context.get("_variety_hint") or "").strip():
                _metric("variety_hint")
            if (user_context.get("_daily_topics_hint") or "").strip():
                _metric("daily_topics")

            # 4. 意图识别（草稿模式不做意图继承/链跟踪，保持无状态纯净）
            intent = self._recognize_intent(text)
            user_context["current_intent"] = intent

            # Case-center：与 A 线同口径的立案信号（要人工/怀疑机器人）。
            # B 线本就有人审，但案例中心是跨会话的「谁需要重点盯」视图，
            # 立案让该会话在 /cases 与待办条可见。软失败零阻断。
            try:
                from src.utils.case_center import (
                    detect_ai_doubt, detect_human_request, open_case,
                )
                if detect_human_request(text):
                    open_case(user_context, str(user_id), "human_request",
                              "case.reason.human_request", quote=text)
                elif detect_ai_doubt(text):
                    open_case(user_context, str(user_id), "ai_doubt",
                              "case.reason.ai_doubt", quote=text)
            except Exception:
                pass

            # 5. 回复策略
            try:
                strategy, strategy_id = self.get_strategy_for_intent(intent, user_id)
            except Exception:
                strategy, strategy_id = ({}, "")
            strategy = dict(strategy or {})
            # 草稿必出（是否真发由下游风险闸决定）：禁静默概率/禁 skip_ai 复读
            strategy["reply_probability"] = 1.0
            strategy["skip_ai"] = False
            user_context["_reply_strategy"] = strategy
            user_context["_reply_strategy_id"] = strategy_id

            # 6. KB 混合检索（与 smart-reply 一致）
            _kb_refs: list = []
            try:
                _kb = self._kb_store_if_exists()
                if _kb:
                    _lang = (user_context or {}).get("reply_lang", "zh")
                    _res = _kb.search(text, top_k=3, lang=_lang)
                    _kbc = _kb.build_ai_context_from_result(_res, lang=_lang)
                    if _kbc:
                        user_context["kb_context"] = _kbc
                        # P2 证据链：留住本稿引用的条目（title/snippet），随返回
                        # 透传给前端做「知识依据」chips——注入 prompt 的知识不再黑盒。
                        try:
                            from src.utils.kb_refs import extract_kb_refs
                            _kb_refs = extract_kb_refs(_res)
                        except Exception:
                            _kb_refs = []
                    else:
                        # 学习漏斗 B 线采集（2026-08-02 断粮复盘）：主流量已迁
                        # 收件箱草稿链，而未命中采集此前只挂在 A 线——陪聊人设
                        # 上线后 A 线整体跳过 KB，学习队列断粮。只收「问题样式」
                        # 文本（占位符/闲聊不进池，防陪伴域灌爆）。
                        try:
                            from src.utils.kb_gate import should_log_kb_miss
                            if should_log_kb_miss(text):
                                _kb.log_miss(text)
                        except Exception:
                            pass
            except Exception:
                self.logger.debug("%sKB 检索跳过", log_prefix, exc_info=True)

            # 7. 慢思考（与 process_message 同配置门控；默认关→零额外开销）。
            #    低风险快路跳过慢思考这一额外 LLM 调用——延迟预算的主要省点。
            if _is_fast_path:
                _metric("fast_path")
            else:
                try:
                    await self._maybe_slow_think(intent, text, user_context, log_prefix)
                    if (user_context.get("_slow_think_outline") or "").strip():
                        _metric("slow_think")
                except Exception:
                    self.logger.debug("%s慢思考跳过", log_prefix, exc_info=True)

            # 8. 生成（PersonaManager 注入人设 + 全部上下文块由 _build_context_prompt 消费）
            _so: Dict[str, Any] = {}
            for _sk in ("temperature", "max_tokens", "context_rounds", "model", "thinking_budget"):
                if _sk in strategy:
                    _so[_sk] = strategy[_sk]
            # 会话级力度档（conv_route：低/中/高 ≈ 答复长度 / 温度）覆盖策略值；空档不动
            try:
                if _cr_scope is not None:
                    _so.update(_cr_route.strategy_overrides())
            except Exception:
                pass
            reply = await self.ai_client.generate_reply_with_intent(
                user_message=text,
                intent=intent,
                user_context=user_context,
                strategy_overrides=_so or None,
            )
            reply = (reply or "").strip()
            if not reply:
                _metric("empty")
                _ro = str(user_context.get("_route_offline") or "")
                if _ro and user_context.get("_unrestricted"):
                    # 无限制端点离线：不回落云端、不出稿——把原因带回给调用方
                    # （persona_reply → smart-reply 提示「模型离线」/ 自动链挂起）
                    _metric("unrestricted_offline")
                    return {"reply": "", "intent": intent, "route_offline": _ro}
                return None

            # 8b. 相似度重试（与 process_message 5b 同思路）：与「最近 N 条」回复重复
            #     → 换角度 + 抬温度重生一次；综合评分更低才采纳，否则保留。深度=N（默认 6）
            #     且叠加**语义层**（可选，需嵌入）抓「换词不换意」的改写复读。
            _combined, _is_rep, _csim, _ssim = await self._anti_repeat_score(
                reply, user_context)
            if _is_rep:
                self.logger.info(
                    "%s回复复读 char=%.0f%% sem=%.0f%% combined=%.2f，换角度重生一次",
                    log_prefix, _csim * 100, _ssim * 100, _combined)
                _metric("repeat_detected")
                user_context["_anti_repeat_hint"] = (
                    "1. 换一个完全不同的开头（禁止用上次的前5个字）\n"
                    "2. 换一个不同的重点（上次说了什么，这次说别的）\n"
                    "3. 风格要有明显区别，就像换了个心情在聊"
                )
                _so2 = dict(_so)
                _so2["temperature"] = min(
                    float(_so2.get("temperature", 0.85)) + 0.15, 1.0
                )
                try:
                    from src.monitoring.metrics_store import get_metrics_store
                    get_metrics_store().record_anti_repeat_rewrite_attempt()
                except Exception:
                    pass
                try:
                    _retry = await self.ai_client.generate_reply_with_intent(
                        user_message=text, intent=intent,
                        user_context=user_context,
                        strategy_overrides=_so2 or None,
                    )
                    _retry = (_retry or "").strip()
                    _retry_ok = False
                    if _retry:
                        _rc, _, _, _ = await self._anti_repeat_score(
                            _retry, user_context, record=False)
                        if _rc < _combined:
                            reply = _retry
                            _metric("retry_applied")
                            try:
                                from src.monitoring.metrics_store import get_metrics_store
                                get_metrics_store().record_anti_repeat_rewrite_adopted()
                            except Exception:
                                pass
                            _retry_ok = True
                    if not _retry_ok:
                        _metric("repeat_persisted")
                except Exception:
                    self.logger.debug("%s重试跳过", log_prefix, exc_info=True)
                user_context.pop("_anti_repeat_hint", None)

            # 9. 人设一致性守卫 + 链接纪律守卫 + 危机事后兜底（与 process_message
            #    5c/5c3/5d 同族同序）
            _before_guard = reply
            if not _skipg("persona_guard"):
                reply = self._enforce_persona_consistency(
                    reply, chat_id=str(chat_id or user_id),
                    account_persona_id=str(persona_id or ""), log_prefix=log_prefix,
                    # B74：生成时 prompt 锁定名（ai_client 写回 user_context）同源直传
                    resolved_name=str(
                        user_context.get("_resolved_persona_name") or ""),
                )
            if reply != _before_guard:
                _metric("persona_guard_intercept")
            # 9a2. 出站文本形态守卫（实施74：B118/B121/B104，与 A 线 5c1b 同口径同序）
            if reply and not _skipg("outbound_text_guard"):
                _before_otg = reply
                reply = self._apply_outbound_text_guard(
                    reply, log_prefix=log_prefix, user_context=user_context)
                if reply != _before_otg:
                    _metric("outbound_text_guard_intercept")
            # 9b. 发图能力关闭时的出站消毒（2026-07-31 补缺口）：inbox 草稿路径
            # 刻意零发送副作用、也不走 process_message 的 promise_guard——若能力关
            # 时 LLM 仍写出「翻翻相册/这张是…」，草稿会进待审/自动发队列变成真
            # 空头支票。与试聊 sanitize_no_photo_reply 同口径；能力开时只剥
            # [PHOTO] 协议标记（草稿无执行层，标记直显=穿帮）。
            if reply and _skipg("photo_capability_sanitize"):
                # 无限制会话：不消毒承诺文案，但 [PHOTO] 协议标记仍剥（草稿无执行层，直显＝穿帮）
                try:
                    from src.ai.photo_directive import strip_photo_directives as _spd_unr
                    reply = _spd_unr(reply)
                except Exception:
                    pass
            elif reply:
                try:
                    from src.companion.photo_capability import (
                        prompt_photos_allowed,
                        sanitize_no_photo_reply,
                    )
                    from src.ai.photo_directive import strip_photo_directives
                    if prompt_photos_allowed(user_context):
                        reply = strip_photo_directives(reply)
                    else:
                        _mctx = False
                        try:
                            from src.ai.outbound_promise_guard import (
                                detect_media_offer,
                                detect_media_promise,
                                wants_media,
                            )
                            _lr = str(user_context.get("last_reply") or "")
                            _mctx = bool(
                                wants_media(text)
                                or detect_media_promise(_lr)
                                or detect_media_offer(_lr))
                        except Exception:
                            _mctx = False
                        _before_pc = reply
                        reply = sanitize_no_photo_reply(
                            reply, media_context=_mctx, source="inbox_draft")
                        if reply != _before_pc:
                            _metric("photo_capability_sanitize")
                except Exception:
                    self.logger.debug(
                        "%sphoto_capability sanitize skipped",
                        log_prefix, exc_info=True)
            # 9b2. 反编造共同往事守卫（P1 2026-08-03）：与 A 线 5c2c 同一入口同一
            # 口径（依据池宽收、有依据只观测、零依据才句级剥离）。B 线更需要它——
            # 草稿经人审/autosend 发出，编造的往事对坐席看来只是「挺自然的寒暄」，
            # 没有任何环节会去核对「他们真一起打过球吗」。
            if reply and not _skipg("fabrication_guard"):
                try:
                    from src.utils.proactive_fabrication_guard import (
                        build_precise_evidence,
                        build_reply_evidence,
                        record_fabrication_guard,
                        strip_fabricated_sentences,
                    )
                    _fab_text, _fab_info = strip_fabricated_sentences(
                        reply, build_reply_evidence(user_context, text),
                        precise_evidence=build_precise_evidence(user_context))
                    record_fabrication_guard(_fab_info, source="b_line")
                    if _fab_info.get("stripped"):
                        _metric("fabrication_strip")
                        self.logger.warning(
                            "%s[fabrication_guard] 零依据编造往事，剥离 %d 句%s：%r",
                            log_prefix, len(_fab_info["stripped"]),
                            "（剥空→回落原文）"
                            if _fab_info.get("all_stripped") else "",
                            _fab_info["stripped"][:2])
                    elif _fab_info.get("suspect"):
                        _metric("fabrication_suspect")
                    reply = _fab_text
                except Exception:
                    self.logger.debug(
                        "%s[fabrication_guard] 守卫异常（放行原文）",
                        log_prefix, exc_info=True)
                # 9b3. 近况陈述编造守卫（#208 L-1 C）：与 A 线 5c2d 同一入口同一口径——
                # B 线草稿经人审/autosend 发出，「刚散完步这边下着小雨」在坐席眼里只是
                # 自然寒暄，没人会核对档案里有没有雨（FTK6S7 正是 B 线 autosend）。
                _before_st = reply
                if not _skipg("status_fabrication_guard"):
                    reply = self._apply_status_fabrication_guard(
                        reply, user_context, text, log_prefix=log_prefix, source="b_line")
                if reply != _before_st:
                    _metric("fabrication_status_strip")

            # 9c. 时空接轨守卫（2026-08-02 补缺口）：此前只接 A 线——B 线草稿
            # 经人审/autosend 发出的文本没有任何时段/星期/错城剥离，跨时区
            # 人设在收件箱链路穿帮无人拦。与 A 线 5c2 后同一入口同一口径。
            if not _skipg("world_clock_guard"):
                reply = self._apply_world_clock_guard(
                    reply, user_context, log_prefix=log_prefix)
            if not _skipg("goal_link_guard"):
                reply = self._apply_goal_link_guard(
                    reply, user_context, log_prefix=log_prefix)
            # 骂战/记仇轮出站否决（P1-b，与 A 线 5c4 同口径同序：危机兜底前）
            if not _skipg("temper_output_guard"):
                reply = self._apply_temper_output_guard(
                    reply, user_context, log_prefix=log_prefix)
            # 危机自伤兜底是安全刹车：无限制会话仍生效，除非该会话 / 全局把刹车全关
            if not _skipg("crisis_safety_net"):
                reply = self._apply_crisis_safety_net(
                    reply, user_context=user_context, log_prefix=log_prefix,
                )
            if user_context.get("_wellbeing_safety_override"):
                _metric("crisis_override")
            # #152 F1：守卫链把稿剥空（退化循环整条截空等）→ 不产出草稿。此前空稿
            # 会继续走状态推进/记忆写回并以 reply="" 返回给 autodraft——上游按
            # 「有稿」处理就把空/坏稿送进了发送队列。B 线空稿的正确终局是「无稿」，
            # autodraft 据此不投递并把会话留在待处理清单（客户消息不会被静默吞：
            # 无稿 → SLA 徽标计时照常 → 坐席接手）。
            if not (reply or "").strip():
                _metric("guard_emptied")
                self.logger.warning(
                    "%s[inbox_draft] 出站守卫链剥空稿件（#152 F1 退化截空等）→ 本轮"
                    "不产出草稿，会话留待处理", log_prefix)
                return None

            # 10. 回复后状态推进（陪伴 exchange_count/stage、剧情）+ 记忆写回
            try:
                self._update_after_reply(
                    reply, user_id, user_context, chat_id=chat_id, user_msg=text,
                )
            except Exception:
                self.logger.debug("%s状态推进跳过", log_prefix, exc_info=True)
            if not user_context.get("disable_episodic_memory"):
                try:
                    self._schedule_episodic_memory_extract(
                        user_id, text, reply, intent, chat_id, platform=platform,
                        account_id=str(
                            (user_context or {}).get("account_id") or ""),
                    )
                except Exception:
                    self.logger.debug("%s记忆写回跳过", log_prefix, exc_info=True)

            try:
                _sk = str(
                    (user_context or {}).get("_context_store_key") or user_id)
                self._context_store.mark_dirty(_sk)
                self._context_store.flush(_sk)
            except Exception:
                pass

            _metric("generated")
            try:
                from src.monitoring.metrics_store import get_metrics_store
                get_metrics_store().record_inbox_draft_latency(
                    (time.time() - _t0) * 1000.0
                )
            except Exception:
                pass
            return {
                "reply": reply, "intent": intent, "kb_refs": _kb_refs,
                # P25 观测：目标注入结果（injected/reason/push_level/intent）
                # → persona_reply → smart-reply API 的 goal_applied，坐席可见
                "goal_applied": user_context.get("_goal_inject_meta"),
            }
        except Exception:
            self.logger.warning("%s生成失败，回落上层兜底", log_prefix, exc_info=True)
            return None
        finally:
            if _cr_scope is not None:
                try:
                    _cr_scope.__exit__(None, None, None)
                except Exception:
                    pass
            # 会话路由键是瞬时态（每轮 attach 重算），不随 ContextStore 落库粘到别的链路
            for _rk in ("_route", "_route_strict", "_thinking", "_unrestricted",
                        "_unrestricted_bypass_safety", "_conv_route", "_route_offline"):
                user_context.pop(_rk, None)
            user_context.pop("_slow_think_outline", None)
            user_context.pop("_media_coherence_hint", None)
            user_context.pop("_media_pending_hint", None)
            user_context.pop("_song_coherence_hint", None)
            user_context.pop("_current_scene_note", None)
            user_context.pop("_media_sent_note", None)
            # P22：坐席指令是瞬时态，绝不能随 ContextStore flush 落库污染下轮自动草稿
            user_context.pop("_agent_instruction", None)

    def _run_autopilot(self) -> None:
        """Auto-Pilot：�查策略健康状态，���重映射持���效策略�€?
        # 安全设�:
          - 仅在 reply_strategies.yaml �?autopilot.enabled=true 时运�?          - �€ >= AUTO_MIN_SAMPLES 条数�?          - ���策略评分必须显著高于当前（GAP >= 15�?          - 每�切换写入审�日志
        """
        rs = {}
        if hasattr(self.config, 'get_strategies_config'):
            rs = self.config.get_strategies_config() or {}
        else:
            rs = {}
        ap_cfg = rs.get("autopilot", {})
        if not ap_cfg.get("enabled", False):
            return

        self._strategy_tracker.mark_no_follow_up()
        hours = int(ap_cfg.get("observation_hours", 24))
        summary = self._strategy_tracker.strategy_summary(hours)
        if not summary:
            return

        from src.utils.strategy_advisor import generate_auto_actions
        actions = generate_auto_actions(
            summary, self._intent_strategy_map,
            self._strategies,
        )
        if not actions:
            return

        switched = 0
        for act in actions:
            intent = act["intent"]
            to_sid = act["to_strategy"]
            from_sid = act["from_strategy"]
            if to_sid not in self._strategies:
                continue
            old_sid = self._intent_strategy_map.get(intent)
            if old_sid == to_sid:
                continue
            self._intent_strategy_map[intent] = to_sid
            switched += 1
            # Q-35 #316：格式串曾被乱码注释掉——intent 本身当 msg、后面三个实参多余
            # → `not all arguments converted`；下方 %d 无实参 → `not enough arguments`。
            self.logger.warning(
                "[Auto-Pilot] %s: %s -> %s (%s)",
                intent, from_sid, to_sid, act["reason"])

        if switched > 0:
            self._persist_strategies()
            self.logger.info("[Auto-Pilot] 已自动切换 %d 个意图映射", switched)

        # L3: A/B 测试���评估 �?有结论时���晋级胜�€?
        if self._ab_tests:
            try:
                from src.utils.strategy_advisor import evaluate_ab_tests
                ab_results = evaluate_ab_tests(
                    self._ab_tests, summary, self._strategies
                )
                for r in ab_results:
                    if r["action"] == "promote" and r["winner"]:
                        intent = r["intent"]
                        winner = r["winner"]
                        self._intent_strategy_map[intent] = winner
                        self._ab_tests[intent]["enabled"] = False
                        self._ab_tests[intent]["concluded"] = {
                            "winner": winner,
                            "scores": r["scores"],
                            "reason": r["reason"],
                            "ts": time.time(),
                        }
                        self._persist_strategies()
                        self.logger.warning(
                            # "[Auto-AB] %s 测试结�: 胜�€?%s (%s)",
                            intent, winner, r["reason"]
                        )
            except Exception as _ab_err:
                self.logger.debug("A/B ���评估异常: %s", _ab_err)

        # J4: 策略参数������ �?渐进式调整，每��€�?1 ����?1 ����?
        if ap_cfg.get("auto_tune", False):
            try:
                from src.utils.strategy_advisor import suggest_param_adjustments
                suggestions = suggest_param_adjustments(summary, self._strategies)
                if suggestions:
                    s = suggestions[0]  # 取最高优先级的一条
                    sid = s["strategy_id"]
                    param = s["param"]
                    new_val = s["suggested"]
                    old_val = s["current"]
                    # 安全边界�€�?
                    _BOUNDS = {
                        "temperature": (0.1, 1.5),
                        "max_tokens": (128, 4096),
                        "context_rounds": (1, 20),
                        "thinking_budget": (0, 2048),
                    }
                    lo, hi = _BOUNDS.get(param, (None, None))
                    if lo is not None and (new_val < lo or new_val > hi):
                        new_val = max(lo, min(hi, new_val))
                    if new_val != old_val and sid in self._strategies:
                        self._strategies[sid][param] = new_val
                        self._persist_strategies()
                        self.logger.warning(
                            # "[Auto-Tune] %s.%s: %s �?%s (%s)",
                            sid, param, old_val, new_val, s["reason"])
            except Exception as _at_err:
                self.logger.debug("J4 参数���异常: %s", _at_err)

    def _is_bare_order_no(self, text: str) -> tuple:
        """
        判定是否为「仅单号」消息（无需求关键词）：纯6~24位数字，
        或「单号/订单号 + 数字」且无查代收/回调等词。
        """
        raw = (text or "").strip()
        if not raw:
            return False, None
        # 需求词：有则视为「需要查单号」或其它，不当作仅单号
        intent_words = re.compile(
            r"查询代收|代收(订单)?查询|回调(代收|交易)|代收回调|查询提现|提现(订单)?查询|回调提现|查询|回调",
            re.IGNORECASE
        )
        if intent_words.search(raw):
            return False, None
        # 纯6~24 位数字
        m = re.match(r"^\s*(\d{6,24})\s*$", raw)
        if m:
            return True, m.group(1)
        # 单号/订单号 + 数字
        for pat in [r"^(?:单号|订单号)\s*[：:]?\s*(\d{6,24})\s*$", r"^(?:单|订单)\s+(\d{6,24})\s*$"]:
            m = re.match(pat, raw, re.IGNORECASE)
            if m:
                return True, m.group(1)
        return False, None

    def _recognize_intent(self, text: str) -> str:
        """
        # 识别用户意图

        Args:
            # text: 用户消息文本

        Returns:
            # 意图名称
        """
        text_lower = text.lower().strip()
        raw = text or ""

        # 媒体消息（图片 Vision/OCR 解析出的「[图片内容] …」、视频/动图占位「[视频]」等）
        # 不能落到本函数末尾的 'greeting' 兜底——长描述文本无问号/无业务词时会被误判为
        # 问候，导致 AI 回「在的，有什么可以帮您的」这类答非所问。统一交 direct_chat：
        # text 里已含图片识别内容，AI 直接「看图说话」生成针对性回复。
        _media_prefixed = raw.lstrip()
        if _media_prefixed.startswith(("[图片", "[视频", "[表情", "[贴纸", "[动态表情")):
            return "direct_chat"

        if is_greeting_message(raw):
            return "greeting"

        # Generic composite intent detection
        if "order_query" in self.skills and any(w in text_lower for w in (
            "没收到钱", "没到账", "未到账", "钱没到", "钱没收到",
            "not received", "didn't receive", "money not arrived", "payment not received",
        )):
            return "order_query"

        if "complaint" in self.skills and any(w in text_lower for w in (
            "退款", "退钱", "退回来", "退单", "把钱退",
            "refund", "return money", "chargeback",
        )):
            return "complaint"

        # Config-driven keyword matching (domain pack can inject extra keywords)
        for intent, keywords in self.intent_keywords.items():
            for keyword in keywords:
                if keyword.lower() in text_lower:
                    return intent

        for intent, patterns in self.intent_patterns.items():
            for pattern in patterns:
                if re.search(pattern, text_lower, re.IGNORECASE):
                    return intent

        _question_markers = (
            "？", "?", "吗", "呢", "谁", "什么", "怎么", "为什么", "哪", "多少", "几",
            "how", "what", "why", "when", "where", "which", "can you", "could you",
            "please", "help",
        )
        if any(m in text_lower for m in _question_markers):
            return 'direct_chat'

        # #208（L-1 C，2026-09-06）：带交易 / 收据 / 购买 / 媒体请求语（「I want to buy
        # vitamins」「Send me the receipt」）→ 直聊接话：不进 greeting（S1 秒回 / 寒暄
        # prompt 答非所问，FTK6S7），也不进 small_talk（S5 可能静默不回）。
        if has_request_context(raw):
            return 'direct_chat'
        if len(text_lower) <= 10:
            return 'direct_chat'
        if len(text_lower) < 20:
            return 'small_talk'
        # ≥20 字、无问号、无业务词的陈述句此前兜底判 greeting——greeting 自此只由
        # is_greeting_message 的寒暄词库正向判定，长陈述按直聊接话。
        return 'direct_chat'


    def _select_skill(self, intent: str, user_context: Dict[str, Any]) -> Optional['Skill']:
        """
        # 选择适合的Skill

        Args:
            # intent: 意图名称 user_context: 用户上下�?
        Returns:
            # Skill实例，�果找不到则返回None
        """
        # 直接匹配意图
        if intent in self.skills:
            return self.skills[intent]

        # 尝试回退到默认技能
        default_skills = ['greeting', 'small_talk']
        for skill_name in default_skills:
            if skill_name in self.skills:
                return self.skills[skill_name]

        return None

    def _check_cooldown(
        self, text: str, user_id: str, chat_id: Any = '',
        account_id: str = "",
        chat_scope: str = "",
    ) -> bool:
        """检查冷却时间（per_chat_user；双号按 account_id 分桶）。

        ``chat_scope`` 与主路径 ``_get_user_context`` 同口径（群聊分窗后
        必须读同一窗，否则 gxp/order 续答判定看错上下文）。

        签名被 ``test_check_cooldown_accepts_chat_scope`` 钉住；剩余时长的
        真实逻辑在 ``_cooldown_remaining``（2026-08-09 起，供「被吞回复
        补答调度」取可重试时刻），本方法保持布尔壳。
        """
        return self._cooldown_remaining(
            text, user_id, chat_id=chat_id, account_id=account_id,
            chat_scope=chat_scope) <= 0

    def _cooldown_remaining(
        self, text: str, user_id: str, chat_id: Any = '',
        account_id: str = "",
        chat_scope: str = "",
    ) -> float:
        """冷却剩余秒数：0=放行；>0=被拦，且值=**全部**失败桶的最大剩余。

        取最大而非首个失败桶：补答调度按该值重试，若只看首桶，重试时可能
        撞上更长的另一桶（如 per_content 120s > per_user 60s）——而水位闸
        只给一次重试机会，撞掉就永久沉默。四个桶均为纯读，无副作用。

        ``outbound.unlimited_mode``（2026-09-04）：四桶冷却属业务频控 → 恒 0
        放行；本会被拦的次数记 bypass 供 P5 观测（先算再判，观测口径不失真）。
        """
        remaining = self._cooldown_remaining_raw(
            text, user_id, chat_id=chat_id, account_id=account_id,
            chat_scope=chat_scope)
        if remaining > 0 and self._outbound_unlimited():
            self._record_outbound_bypass("skill_cooldown")
            return 0.0
        return remaining

    # ── outbound.unlimited_mode 读取面 + 拦截计数（src/ops/outbound_policy）──
    @staticmethod
    def _outbound_unlimited() -> bool:
        try:
            from src.ops.outbound_policy import is_unlimited
            return is_unlimited()
        except Exception:
            return False

    @staticmethod
    def _record_outbound_bypass(reason: str) -> None:
        try:
            from src.ops.outbound_policy import record_unlimited_bypass
            record_unlimited_bypass(reason)
        except Exception:
            pass

    @staticmethod
    def _record_outbound_block(layer: str, reason: str, context: Any = None) -> None:
        try:
            from src.ops.outbound_policy import record_block
            pf = ""
            if isinstance(context, dict):
                pf = str(context.get("channel") or context.get("platform") or "")
            record_block(layer, reason, platform=pf)
        except Exception:
            pass

    def _cooldown_remaining_raw(
        self, text: str, user_id: str, chat_id: Any = '',
        account_id: str = "",
        chat_scope: str = "",
    ) -> float:
        """四桶冷却原始计算（不看 unlimited_mode），供 ``_cooldown_remaining`` 包装。"""
        current_time = time.time()
        user_context = self._get_user_context(
            user_id, account_id=account_id, chat_scope=chat_scope)

        last_intent = user_context.get('current_intent', '')
        text_stripped = text.strip()
        gxp_last_ask = user_context.get("gxp_last_ask")
        if gxp_last_ask in ("what", "intent") and re.match(r"^[1-5]\s*$", text_stripped):
            return 0.0

        is_likely_order_number = text_stripped.isdigit() and 6 <= len(text_stripped) <= 24
        if "order_query" in self.skills and last_intent == "order_query" and is_likely_order_number:
            content_hash = self._hash_content(text, chat_id, account_id=account_id)
            last_content_time = self.reply_cache.get(content_hash, 0)
            return max(
                0.0,
                self.cooldown_per_content - (current_time - last_content_time))

        _bot_q_ts = user_context.get("_bot_question_ts", 0)
        if _bot_q_ts and (current_time - _bot_q_ts) < 120:
            return 0.0

        remaining = 0.0

        # 1. per_chat_user 冷却（含 account_id，双协议号互不影响）
        if self.cooldown_per_chat_user > 0 and chat_id:
            from src.utils.context_store import make_context_key
            cu_key = make_context_key(f"{chat_id}_{user_id}", account_id)
            last_cu = self._chat_user_last_reply.get(cu_key, 0)
            remaining = max(
                remaining,
                self.cooldown_per_chat_user - (current_time - last_cu))

        # 2. 全局冷却（保留为 0 则不生效，作为向后兼容）
        if self.cooldown_global > 0:
            remaining = max(
                remaining,
                self.cooldown_global - (current_time - self.global_last_reply_time))

        # 3. 用户冷却（读分桶后的 user_context.last_reply_time）
        if self.cooldown_per_user > 0:
            last_reply_time = user_context.get('last_reply_time', 0)
            remaining = max(
                remaining,
                self.cooldown_per_user - (current_time - last_reply_time))

        # 4. 内容重复检查（按账号+会话隔开）
        content_hash = self._hash_content(text, chat_id, account_id=account_id)
        last_content_time = self.reply_cache.get(content_hash, 0)
        remaining = max(
            remaining,
            self.cooldown_per_content - (current_time - last_content_time))

        return max(0.0, remaining)

    # ── 「被吞回复」跳过信号 + 冷却记账快照/回滚（2026-08-09）────────────────
    #
    # 修复对象＝198↔104 实录「回复等了 11 分钟」的两个成因：
    # ① 冷却拦截静默返回 None，上层无从安排补答 → 靠轮询兜底的 600s 去重 TTL
    #    「碰巧」重拾（skip 信号 + telegram_client._defer_swallowed_inbound 收口）；
    # ② interject 在 client 层丢弃**已生成**的回复，但本层 _update_after_reply
    #    早已提交冷却记账/last_reply → 一条从未发出的回复占住冷却位，把后续
    #    整个消息爆发全吞掉，且 AI 记忆里多出一条对方从未收到的话
    #    （快照/回滚收口；生成前拍快照，丢弃时按「本轮写入才动」精确撤销）。

    _REPLY_SKIP_CAP = 512  # 信号表容量上限（插入序剪最旧）

    @staticmethod
    def _reply_skip_key(chat_id: Any, user_id: Any, account_id: str = "") -> str:
        return (
            f"{str(account_id or '').strip()}|{str(chat_id or '').strip()}"
            f"|{str(user_id or '').strip()}"
        )

    def _note_reply_skip(
        self, chat_id: Any, user_id: Any, account_id: str = "", *,
        reason: str, retry_after: float,
    ) -> None:
        """记录一次「本该回但被闸门吞掉」的拦截（best-effort，绝不抛）。"""
        try:
            key = self._reply_skip_key(chat_id, user_id, account_id)
            self._reply_skip_signals.pop(key, None)  # 重插保插入序=时间序
            self._reply_skip_signals[key] = {
                "reason": str(reason),
                "retry_after": max(0.0, float(retry_after)),
                "ts": time.time(),
            }
            while len(self._reply_skip_signals) > self._REPLY_SKIP_CAP:
                self._reply_skip_signals.pop(
                    next(iter(self._reply_skip_signals)), None)
        except Exception:
            pass

    def consume_reply_skip(
        self, chat_id: Any, user_id: Any, account_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        """取走（并清除）最近一次跳过信号；无信号返回 None。

        取走即删：信号只服务「紧随其后的这一次」补答决策，陈旧信号残留会
        让下一条正常处理的消息被误安排重试。
        """
        try:
            return self._reply_skip_signals.pop(
                self._reply_skip_key(chat_id, user_id, account_id), None)
        except Exception:
            return None

    def snapshot_reply_accounting(
        self, user_id: Any, context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """在 process_message **之前**拍冷却记账快照，供上层丢弃回复时回滚。

        键派生与 ``_handle_message_guarded``/``_update_after_reply`` 完全同口径
        （chat_scope 经 ``_context_chat_scope``、记账账号取 user_context 里已
        持久化的 account_id、缺省回落 context 的 account_id）。任何异常返回
        None——快照只是回滚的前提，拍不到就退回旧行为（不回滚），绝不影响
        主链路。
        """
        try:
            user_id_str = str(user_id)
            ctx = context or {}
            _chat_id = ctx.get('chat_id', '')
            _acct_id = str(ctx.get('account_id') or '').strip()
            _chat_scope = self._context_chat_scope(ctx, user_id_str)
            uc = self._get_user_context(
                user_id_str, account_id=_acct_id, chat_scope=_chat_scope)
            # _update_after_reply 用 user_context["account_id"]（write-if-absent
            # 后的值）派生 cu_key；快照按「写入后状态」预演同一派生。
            keys_acct = str(uc.get("account_id") or _acct_id or "")
            from src.utils.context_store import make_context_key
            cu_key = make_context_key(f"{_chat_id}_{user_id_str}", keys_acct)
            return {
                "user_id": user_id_str,
                "chat_id": _chat_id,
                "lookup_acct": _acct_id,
                "keys_acct": keys_acct,
                "chat_scope": _chat_scope,
                "taken_at": time.time(),
                "cu_key": cu_key,
                "cu_ts": self._chat_user_last_reply.get(cu_key),
                "global_ts": float(self.global_last_reply_time or 0),
                "last_reply": uc.get("last_reply"),
                "last_reply_time": uc.get("last_reply_time"),
                "reply_count": int(uc.get("reply_count", 0) or 0),
                "recent_len": len(uc.get("recent_replies") or []),
            }
        except Exception:
            return None

    def rollback_reply_accounting(
        self, snapshot: Optional[Dict[str, Any]],
    ) -> bool:
        """撤销一条「生成了但从未发出」回复的冷却记账（interject 丢弃收口）。

        只动**本轮**写入（时间戳 >= 快照时刻才恢复），逐项 best-effort：
        - per_chat_user / global 冷却戳恢复快照值（快照时不存在则删除）；
        - per_content 哈希（按 last_message 现值重算，与写入口同表达式）删除；
        - user_context 的 last_reply/last_reply_time/reply_count 恢复快照值——
          防「AI 记忆里存在一条对方从未收到的话」的记忆分叉；
        - recent_replies 防复读环弹出本轮新增（防拿幻影回复触发换角度重生）。

        刻意**不**回滚：关系 exchange_count / 剧情推进 / 画像 / 质量评估——
        软性统计多计一次的代价远小于回滚错状态机的风险。
        已知边界：_bot_question_ts 120s 旁路窗内可能存在真实的近期冷却戳被
        一并恢复为更早值——那扇旁路本就是产品要求快答的场景，可接受。
        """
        if not isinstance(snapshot, dict):
            return False
        rolled = False
        taken_at = float(snapshot.get("taken_at") or 0)
        if taken_at <= 0:
            return False
        try:
            cu_key = str(snapshot.get("cu_key") or "")
            if cu_key:
                cur = self._chat_user_last_reply.get(cu_key)
                if cur is not None and cur >= taken_at:
                    prev = snapshot.get("cu_ts")
                    if prev is None:
                        self._chat_user_last_reply.pop(cu_key, None)
                    else:
                        self._chat_user_last_reply[cu_key] = prev
                    rolled = True
        except Exception:
            pass
        try:
            if float(self.global_last_reply_time or 0) >= taken_at:
                self.global_last_reply_time = float(
                    snapshot.get("global_ts") or 0)
                rolled = True
        except Exception:
            pass
        try:
            uc = self._get_user_context(
                str(snapshot.get("user_id") or ""),
                account_id=str(snapshot.get("lookup_acct") or ""),
                chat_scope=str(snapshot.get("chat_scope") or ""))
            # per_content：与 _update_after_reply 同表达式重算哈希（last_message
            # 在本轮已被更新为当前入站文本，两边算出同一桶）。
            try:
                content_hash = self._hash_content(
                    uc.get('last_message', ''),
                    snapshot.get("chat_id"),
                    account_id=str(snapshot.get("keys_acct") or ""))
                cur_ct = self.reply_cache.get(content_hash)
                if cur_ct is not None and cur_ct >= taken_at:
                    self.reply_cache.pop(content_hash, None)
                    rolled = True
            except Exception:
                pass
            try:
                lrt = float(uc.get("last_reply_time") or 0)
                if lrt >= taken_at:
                    if snapshot.get("last_reply") is None:
                        uc.pop("last_reply", None)
                        uc.pop("last_reply_time", None)
                    else:
                        uc["last_reply"] = snapshot.get("last_reply")
                        uc["last_reply_time"] = snapshot.get("last_reply_time")
                    uc["reply_count"] = int(snapshot.get("reply_count") or 0)
                    rolled = True
            except Exception:
                pass
            try:
                _rr = uc.get("recent_replies")
                _keep = int(snapshot.get("recent_len") or 0)
                if isinstance(_rr, list) and len(_rr) > _keep:
                    del _rr[_keep:]
                    rolled = True
            except Exception:
                pass
            try:
                _sk = str(uc.get("_context_store_key") or "")
                if rolled and _sk and getattr(self, "_context_store", None):
                    self._context_store.mark_dirty(_sk)
            except Exception:
                pass
        except Exception:
            pass
        return rolled

    @staticmethod
    def _reply_similarity(a: str, b: str) -> float:
        """计算两条回�的字符级 Jaccard 相似度（去标点后的字�?bigram�?"""
        import re as _re
        def _bigrams(s: str):
            s = _re.sub(r'[^\w]', '', s)
            return set(s[i:i+2] for i in range(len(s) - 1)) if len(s) > 1 else {s}
        ba, bb = _bigrams(a), _bigrams(b)
        if not ba or not bb:
            return 0.0
        return len(ba & bb) / len(ba | bb)

    # ── 防复读（anti-repeat）：从「只比上一条」升级到「比最近 N 条」──────────────
    #   语音复读事故的根因是文字复读：旧逻辑仅拿 last_reply（深度=1）比对，
    #   中间隔一条不同回复就漏判「跟前天/昨天一样」。改为窗口化比对（默认 6 条，
    #   可经 inbox.auto_draft.anti_repeat.{window,threshold} 调），命中 → 换角度重生。
    _ANTI_REPEAT_WINDOW_DEFAULT = 6
    _ANTI_REPEAT_THRESHOLD_DEFAULT = 0.65

    def _anti_repeat_params(self) -> Tuple[int, float]:
        """读取防复读窗口/阈值（config inbox.auto_draft.anti_repeat.*，含安全默认）。"""
        window = self._ANTI_REPEAT_WINDOW_DEFAULT
        threshold = self._ANTI_REPEAT_THRESHOLD_DEFAULT
        try:
            _cfg = self.config.config if hasattr(self.config, "config") else {}
            if isinstance(_cfg, dict):
                _ar = (((_cfg.get("inbox") or {}).get("auto_draft") or {})
                       .get("anti_repeat") or {})
                window = int(_ar.get("window", window) or window)
                threshold = float(_ar.get("threshold", threshold) or threshold)
        except Exception:
            pass
        return max(1, window), min(max(threshold, 0.0), 1.0)

    def _recent_reply_pool(self, user_context: Dict[str, Any], window: int) -> List[str]:
        """本会话最近 window 条已发回复（含 last_reply 兜底），供防复读比对。"""
        pool: List[str] = []
        _rr = user_context.get("recent_replies")
        if isinstance(_rr, list):
            for _r in _rr:
                if isinstance(_r, str) and _r.strip():
                    pool.append(_r.strip())
        _lr = (user_context.get("last_reply") or "").strip()
        if _lr and _lr not in pool:
            pool.append(_lr)
        return pool[-window:] if window > 0 else pool

    def _reply_repeat_max_sim(self, reply: str, user_context: Dict[str, Any],
                              window: int) -> float:
        """reply 与最近 window 条历史回复的最大 Jaccard 相似度（0=无历史/不重复）。"""
        reply = (reply or "").strip()
        if not reply:
            return 0.0
        best = 0.0
        for _r in self._recent_reply_pool(user_context, window):
            try:
                _s = self._reply_similarity(_r, reply)
            except Exception:
                _s = 0.0
            if _s > best:
                best = _s
                if best >= 1.0:
                    break
        return best

    # ── 语义层防复读（可选）：字符 Jaccard 抓不住「换词不换意」的改写复读，用嵌入
    #   余弦兜底。绝对值/阈值随嵌入模型而异（如 nomic-embed-text 基线偏高、需高阈~0.91
    #   且建议加 query_prefix；bge-m3 可原文低阈），故阈值/前缀走配置。嵌入不可用/失败
    #   一律回落纯字符层（零阻断）。默认关，走 inbox.auto_draft.anti_repeat.semantic.* 开。
    _ANTI_REPEAT_SEM_THRESHOLD_DEFAULT = 0.90

    def _anti_repeat_semantic_cfg(self) -> Tuple[bool, float, str]:
        """(enabled, threshold, query_prefix)——语义层配置（含安全默认，默认关）。"""
        enabled, threshold, prefix = False, self._ANTI_REPEAT_SEM_THRESHOLD_DEFAULT, ""
        try:
            _cfg = self.config.config if hasattr(self.config, "config") else {}
            if isinstance(_cfg, dict):
                _s = (((((_cfg.get("inbox") or {}).get("auto_draft") or {})
                        .get("anti_repeat") or {}).get("semantic")) or {})
                enabled = bool(_s.get("enabled", enabled))
                threshold = float(_s.get("threshold", threshold) or threshold)
                prefix = str(_s.get("query_prefix", prefix) or "")
        except Exception:
            pass
        return enabled, min(max(threshold, 0.0), 1.0), prefix

    def _embed_cache_max(self) -> int:
        """防复读嵌入缓存 LRU 上限（config ...semantic.embed_cache_max，默认类常量 512）。

        配置化后可由 anti_repeat_advisor 依实测命中率给出「上调/下调」的可落地建议。
        """
        try:
            _cfg = self.config.config if hasattr(self.config, "config") else {}
            if isinstance(_cfg, dict):
                _s = (((((_cfg.get("inbox") or {}).get("auto_draft") or {})
                        .get("anti_repeat") or {}).get("semantic")) or {})
                v = int(_s.get("embed_cache_max", 0) or 0)
                if v > 0:
                    return v
        except Exception:
            pass
        return self._EMBED_VEC_CACHE_MAX

    @staticmethod
    def _cosine_sim(a: List[float], b: List[float]) -> float:
        # 维度不一致（如换 embedding 模型后新旧向量混存：nomic 768 vs bge-m3 1024）直接判 0，
        # 绝不 min() 截断——截断会拿两个不同向量空间的前 N 维算出「看似合理」的假相似度，
        # 使防复读误判/漏判。与 episodic_vector.cosine_similarity 同口径（不等长→0）。
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = na = nb = 0.0
        for x, y in zip(a, b):
            dot += x * y; na += x * x; nb += y * y
        if na <= 0 or nb <= 0:
            return 0.0
        return dot / ((na ** 0.5) * (nb ** 0.5))

    _EMBED_VEC_CACHE_MAX = 512

    async def _embed_cached(self, texts: List[str]) -> List[List[float]]:
        """带进程内 LRU 的批量嵌入：只对**未命中**的文本发一次批量请求，命中直接复用。

        向量对 (文本 + 模型) 确定 → 跨轮次/跨用户可安全共享。防复读里「最近 N 条」在
        上一轮作为候选时已被嵌入过 → 本轮命中缓存，稳态每轮只需嵌「当前候选」1 条
        （由 O(N) 次嵌入降到 O(1)）。任何失败该位置回 []（不缓存失败）；绝不抛。
        """
        import hashlib
        from collections import OrderedDict as _OD
        if not hasattr(self, "_embed_vec_cache"):
            self._embed_vec_cache = _OD()  # type: ignore[attr-defined]
        cache = self._embed_vec_cache
        keys = [hashlib.sha1(t.encode("utf-8")).hexdigest() for t in texts]
        out: List[Optional[List[float]]] = [None] * len(texts)
        miss_idx: List[int] = []
        for i, k in enumerate(keys):
            v = cache.get(k)
            if v is not None:
                cache.move_to_end(k)
                out[i] = v
            else:
                miss_idx.append(i)
        if miss_idx:
            ai = self.ai_client
            try:
                miss_vecs = await ai.embed_with_fallback([texts[i] for i in miss_idx])
            except Exception:
                miss_vecs = []
            for j, i in enumerate(miss_idx):
                v = miss_vecs[j] if (miss_vecs and j < len(miss_vecs)) else []
                out[i] = v
                if v:  # 只缓存成功向量（失败位下轮重试）
                    cache[keys[i]] = v
                    cache.move_to_end(keys[i])
            _cap = self._embed_cache_max()
            while len(cache) > _cap:
                cache.popitem(last=False)
        try:
            from src.monitoring.metrics_store import get_metrics_store
            get_metrics_store().record_embed_cache(
                hits=len(texts) - len(miss_idx), misses=len(miss_idx))
        except Exception:
            pass
        return [v if v else [] for v in out]

    async def _semantic_repeat_max_sim(
        self, reply: str, user_context: Dict[str, Any], window: int, prefix: str,
    ) -> float:
        """reply 与最近 window 条历史回复的最大嵌入余弦（嵌入不可用/失败→0.0）。"""
        reply = (reply or "").strip()
        ai = self.ai_client
        if not reply or ai is None or not hasattr(ai, "embed_with_fallback"):
            return 0.0
        pool = self._recent_reply_pool(user_context, window)
        if not pool:
            return 0.0
        try:
            texts = [prefix + reply] + [prefix + p for p in pool]
            vecs = await self._embed_cached(texts)
        except Exception:
            return 0.0
        if not vecs or len(vecs) != len(pool) + 1 or not vecs[0]:
            return 0.0
        rv = vecs[0]
        best = 0.0
        for v in vecs[1:]:
            s = self._cosine_sim(rv, v)
            if s > best:
                best = s
        return best

    async def _anti_repeat_score(
        self, reply: str, user_context: Dict[str, Any], *, record: bool = True,
    ) -> Tuple[float, bool, float, float]:
        """复读综合评分 → (combined, is_repeat, char_sim, sem_sim)。

        combined = max(char_sim/char_thr, sem_sim/sem_thr)；>1 判定复读。
        字符层已触发即跳过嵌入（省调用）；语义层仅在开启且字符未触发时算，任何
        嵌入失败自动回落纯字符层（零阻断）。可跨 5b/8b 复用（含重试后再评分）。

        record=False 用于重试候选的复评（不计入观测的「主判定」，避免触发率虚高）。
        """
        _win, _thr = self._anti_repeat_params()
        char_sim = self._reply_repeat_max_sim(reply, user_context, _win)
        char_trig = (_thr > 0 and char_sim / _thr > 1.0)
        combined = (char_sim / _thr) if _thr > 0 else 0.0
        sem_sim = 0.0
        if combined <= 1.0:
            sem_on, sem_thr, prefix = self._anti_repeat_semantic_cfg()
            if sem_on and sem_thr > 0:
                sem_sim = await self._semantic_repeat_max_sim(
                    reply, user_context, _win, prefix)
                if sem_sim > 0:
                    combined = max(combined, sem_sim / sem_thr)
        is_rep = combined > 1.0
        if record:
            _layer = "char" if char_trig else ("semantic" if is_rep else "none")
            try:
                from src.monitoring.metrics_store import get_metrics_store
                get_metrics_store().record_anti_repeat_check(_layer)
            except Exception:
                pass
        return combined, is_rep, char_sim, sem_sim

    def _push_recent_reply(self, user_context: Dict[str, Any], reply: str) -> None:
        """把刚发出的回复压入环形缓冲（略大于窗口留冗余），供后续轮次防复读。"""
        reply = (reply or "").strip()
        if not reply:
            return
        try:
            _win, _ = self._anti_repeat_params()
        except Exception:
            _win = self._ANTI_REPEAT_WINDOW_DEFAULT
        _rr = user_context.get("recent_replies")
        if not isinstance(_rr, list):
            _rr = []
        _rr.append(reply[:500])
        _keep = max(1, _win) + 2
        if len(_rr) > _keep:
            _rr = _rr[-_keep:]
        user_context["recent_replies"] = _rr

    def _hash_content(
        self, text: str, chat_id: str = "", account_id: str = "",
    ) -> str:
        """内容哈希：含 account_id+chat_id，双号/不同群相同文本互不阻断。"""
        text_simple = text.lower().strip()
        parts = [p for p in (str(account_id or "").strip(),
                             str(chat_id or "").strip()) if p and p != "default"]
        raw = f"{':'.join(parts)}:{text_simple}" if parts else text_simple
        return hashlib.md5(raw.encode()).hexdigest()[:8]

    async def _llm_judge_language_request(self, text: str) -> str:
        """间接语言请求 LLM 短判（lang_policy 正则漏网的隐晦表达兜底）。

        触发前提由调用方门控（提及语言名 + 短消息 + 正则未命中），这里只负责
        一次 yes/no 短答与结果校验。带进程级 LRU 缓存（同句只判一次）。
        返回目标语言码或 ""。
        """
        t = str(text or "").strip()
        if not t or self.ai_client is None:
            return ""
        cache = getattr(self, "_llm_lang_req_cache", None)
        if cache is None:
            cache = {}
            self._llm_lang_req_cache = cache  # type: ignore[attr-defined]
        key = t[:120]
        if key in cache:
            return cache[key]
        try:
            raw = await self.ai_client.chat(
                "You are a strict intent classifier. Question: in the following chat "
                "message, is the user explicitly or implicitly ASKING the assistant to "
                "switch the conversation to a different language? Reply with EXACTLY "
                "one token: `no`, or `yes:<iso-639-1 code>` (e.g. yes:ja, yes:en, "
                "yes:zh). Statements about语言 that are not requests (e.g. \"Japanese "
                "is hard\", \"he speaks English\") are `no`.\n\n"
                f"Message: {t!r}",
                strategy_overrides={"max_tokens": 8, "temperature": 0.0},
            )
        except Exception:
            self.logger.debug("LLM 语言请求短判调用失败", exc_info=True)
            return ""
        out = ""
        m = re.search(r"yes\s*[:：]\s*([a-zA-Z\-_]{2,6})", str(raw or ""), re.IGNORECASE)
        if m:
            from src.ai.lang_policy import valid_lang_code
            out = valid_lang_code(m.group(1))
        if len(cache) > 512:
            cache.clear()
        cache[key] = out
        if out:
            self.logger.info("LLM 语言请求短判命中: %r → %s", t[:40], out)
        return out

    @staticmethod
    def _group_chat_hint_text() -> str:
        """群聊场景约束（P3-2）：注入 prompt 的群感知提示。

        群窗（``_chat_scope`` 非空）每轮注入；私聊窗从不出现。修实测三连击：
        进群自我介绍像私聊开场、把群里闲聊当「对你说」、答非所问长篇输出。
        """
        return (
            "你此刻在一个多人群聊里发言（不是一对一私聊）：\n"
            "- 回复对全体群成员可见；不要自我介绍、不要用私聊式亲昵开场\n"
            "- 只回应当前这条消息本身；群里别人的对话不一定是对你说的，"
            "不确定时宽泛自然地接话即可\n"
            "- 不要提及或编造你和某个成员的私聊经历/照片/约定\n"
            "- 群聊回复要简短口语化（一两句为宜），像群友插话，不要长篇输出"
        )

    @staticmethod
    def _context_chat_scope(
        context: Optional[Dict[str, Any]], user_id_str: str,
    ) -> str:
        """群聊分窗判据（P1-2）：仅显式群信号才返回群 id 作窗口前缀。

        只信 ``context.is_group`` / ``chat_type in (group, supergroup, channel)``
        ——**不能**用 ``chat_id != user_id`` 判群：protocol 链 user_id 为复合键
        （``platform:acct:chat``）而 chat_id 为裸键，字符串恒不等会把全部协议
        私聊误判断窗。无显式信号一律返 ""（私聊键格式一个字节不变）。
        键格式与 ``ConversationScope.context_key``（chat_id 非空 →
        ``{chat_id}:{chat_key}`` 为 base）同构，有门禁锁定。
        """
        ctx = context or {}
        ctype = str(ctx.get("chat_type") or "").strip().lower()
        grouped = bool(ctx.get("is_group")) or ctype in (
            "group", "supergroup", "channel")
        if not grouped:
            return ""
        raw = ctx.get("chat_id")
        cid = str(raw if raw is not None else "").strip()
        if not cid or cid == str(user_id_str):
            return ""
        return cid

    def _get_user_context(
        self, user_id: str, account_id: str = "",
        chat_scope: str = "",
    ) -> Dict[str, Any]:
        """获取或创建用户上下文（持久化 SQLite）。

        ``account_id`` 非空时用 ``{account_id}:{user_id}`` 作存储键（双协议号隔离）；
        ``chat_scope`` 非空（群聊分窗，P1-2）时 base 为 ``{chat_scope}:{user_id}``
        ——同一用户的群聊发言与私聊各开一窗，互不污染；episodic 记忆键刻意
        **不带** chat_scope（用户事实是用户级的，跨窗共享）。
        ``user_context['user_id']`` 仍为逻辑 peer id，供 prompt/记忆业务使用。
        """
        from src.utils.context_store import make_context_key
        base = f"{chat_scope}:{user_id}" if chat_scope else str(user_id)
        key = make_context_key(base, account_id)
        ctx = self._context_store.get(key)
        ctx["user_id"] = str(user_id)
        ctx["_context_store_key"] = key
        return ctx

    def _get_persona_name_for_context(self, user_context: Dict[str, Any]) -> str:
        """Return the correct persona name for this user_context, or '' if unavailable."""
        persona_id = (user_context or {}).get("account_persona_id") or ""
        if not persona_id:
            return ""
        try:
            from src.utils.persona_manager import PersonaManager
            pm = PersonaManager.get_instance()
            persona = pm.get_persona_by_id(str(persona_id))
            if not persona:
                return ""
            return (persona.get("name") or "").strip()
        except Exception:
            return ""

    def _forbidden_self_names(self) -> set:
        """Names the bot must never speak as its *own* (role labels, not real names).

        The domain persona's display name (domain ``conversion`` → ``线上陪伴``) is an
        operator-facing label, explicitly **not** a spoken name (see ``persona.yaml``:
        「展示名以 config ai.ai_name / 主系统提示为准」). When the account persona fails
        to resolve, the reply engine falls back to this domain persona and the model
        happily answers 「我叫线上陪伴」— which then gets sent to the customer (observed
        2026-07-01 on conv ``8921664288``). This floor guarantees such role labels are
        neutralized regardless of whether a real persona name is available.

        Returns a set of lowercased forbidden self-name tokens.
        """
        names = {
            "线上陪伴", "在线陪伴", "線上陪伴",
            "线上客服", "在线客服", "線上客服",
            "客服", "小客服", "助手", "小助手", "ai助手", "机器人",
        }
        try:
            from src.utils.persona_manager import PersonaManager
            dp = getattr(PersonaManager.get_instance(), "_domain_persona", None) or {}
            dn = str(dp.get("name") or "").strip()
            if dn:
                names.add(dn)
        except Exception:
            pass
        return {n.lower() for n in names if n}

    def _sanitize_assistant_reply(self, reply: str, user_context: Dict[str, Any]) -> str:
        """Strip wrong / forbidden self-name claims before storing to history.

        Prevents self-reinforcing hallucination (bot says wrong name once → sees it in
        history → keeps repeating) and identity leaks of the domain role label.

        Two passes:
          1. **Name-declaration rewrite** — only when a real persona name is resolved:
             「我叫X」「我的名字是/叫X」「My name is X / I'm X」 → replace X with the
             correct persona name (unless it already matches).
          2. **Forbidden self-name floor** — works even with no resolved name:
             「我叫/我是/叫我/我就是 <role-label>」 (e.g. 线上陪伴/客服) is rewritten to the
             persona name when available, otherwise the whole false claim is dropped.
             This is the safety net for the 「我叫线上陪伴」leak when the account persona
             failed to resolve.
        """
        if not reply or not isinstance(reply, str):
            return reply

        import re as _re

        correct = self._get_persona_name_for_context(user_context)
        forbidden = self._forbidden_self_names()
        if correct:
            forbidden.discard(correct.lower())

        out = reply

        # ---- Pass 1: name-declaration rewrite (needs a correct name to substitute) ----
        if correct:
            def _zh_repl(m):
                prefix = m.group(1)
                claimed = m.group(2).strip()
                # already correct (allow a trailing particle like 呀/啦 to survive)
                if claimed == correct or claimed.startswith(correct):
                    return m.group(0)
                return f"{prefix}{correct}"

            out = _re.sub(r"(我叫)([^\s，。！？,.\!\?\n]{1,8})", _zh_repl, out)
            out = _re.sub(r"(我的名字(?:是|叫))([^\s，。！？,.\!\?\n]{1,8})", _zh_repl, out)

            def _en_repl(m):
                prefix = m.group(1)
                claimed = m.group(2).strip()
                if claimed.lower() == correct.lower() or claimed.lower().startswith(correct.lower()):
                    return m.group(0)
                return f"{prefix}{correct}"

            out = _re.sub(
                r"(?i)(my name is\s+|i['' ]?m\s+|i am\s+)([A-Z][a-zA-Z]{1,15})",
                _en_repl, out,
            )

        # ---- Pass 2: forbidden self-name floor (works even without a correct name) ----
        # Only fires when the claimed token is a known role label, so it cannot corrupt
        # legitimate sentences like 「我是说真的」/「我是学生」.
        if forbidden:
            _alt = "|".join(
                _re.escape(f) for f in sorted(forbidden, key=len, reverse=True)
            )
            _pat = _re.compile(
                r"(我就是|我叫|我是|叫我|我的名字(?:是|叫))"
                r"(?:" + _alt + r")"
                r"([呀啊哦喔啦呢的。，,！!？?~\s]|$)",
                _re.IGNORECASE,
            )

            def _forbid_repl(m):
                prefix = m.group(1)
                tail = m.group(2)
                if correct:
                    return f"{prefix}{correct}{tail}"
                return ""  # no name to offer → drop the false claim entirely

            out = _re.sub(_pat, _forbid_repl, out)
            # tidy any leading punctuation left behind by a stripped claim
            out = _re.sub(r"^[\s，,。、~]+", "", out)

        return out

    def _sanitize_history_name_claims(
        self, history: List[Dict[str, Any]], user_context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Sanitize all assistant turns in conversation history (retroactive cleanup)."""
        if not history or not isinstance(history, list):
            return history
        correct = self._get_persona_name_for_context(user_context)
        if not correct:
            return history
        cleaned: List[Dict[str, Any]] = []
        for turn in history:
            if not isinstance(turn, dict):
                cleaned.append(turn)
                continue
            if turn.get("role") == "assistant" and turn.get("content"):
                new_content = self._sanitize_assistant_reply(
                    str(turn["content"]), user_context,
                )
                cleaned.append({**turn, "content": new_content})
            else:
                cleaned.append(turn)
        return cleaned

    def _episodic_storage_key(
        self, user_id_str: str, chat_id: Any, platform: str = "",
        account_id: str = "",
        user_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        from src.utils.episodic_memory_store import (
            compute_memory_storage_key, strip_composite_user_id,
        )
        from src.utils.context_store import make_context_key

        if not account_id and user_context:
            account_id = str(user_context.get("account_id") or "")
        scope = (self._memory_cfg or {}).get("scope", "user")
        base_key = compute_memory_storage_key(str(scope), user_id_str, chat_id)
        # 防复合 id 回喂：conversation_id / 完整 canonical 被当 chat_key 传入时
        # 剥回裸形态——否则 resolve 注册 platform:platform:… 翻倍键、记忆落错桶
        # （2026-07-27 生产 user_identity_map 实锤 8 行）。留 info 日志溯源真调用方。
        _stripped = strip_composite_user_id(base_key, platform)
        if _stripped != base_key:
            self.logger.info(
                "[episodic] composite user_id normalized %r -> %r (acct=%r)",
                base_key, _stripped, account_id,
            )
            base_key = _stripped
        # 双号隔离：同 peer 不同协议号记忆分桶（与 ContextStore 同口径）；
        # 复合 id 剥出的 ``acct:peer`` 已带同账号分桶 → 不二次前缀。
        _acct = str(account_id or "").strip()
        if not (_acct and _acct != "default"
                and base_key.startswith(_acct + ":")):
            base_key = make_context_key(base_key, account_id)
        # S5: resolve to cross-platform canonical_id when platform is known
        if self._cpi and platform:
            return self._cpi.resolve(platform, base_key)
        return base_key

    async def _embed_user_message_for_episodic(self, text: str) -> Optional[List[float]]:
        t = (text or "").strip()
        if len(t) < 2:
            return None
        _mvec = (self._memory_cfg or {}).get("vector") or {}
        try:
            vecs = await self.ai_client.embed([t[:500]])
            out = vecs[0] if vecs and vecs[0] else None
            if out is None and _mvec.get("enabled", False):
                try:
                    from src.monitoring.metrics_store import get_metrics_store
                    get_metrics_store().record_embed_fail()
                except Exception:
                    pass
            return out
        except Exception as _e:
            self.logger.debug("episodic query embed: %s", _e)
            if _mvec.get("enabled", False):
                try:
                    from src.monitoring.metrics_store import get_metrics_store
                    get_metrics_store().record_embed_fail()
                except Exception:
                    pass
            return None

    async def _maybe_slow_think(
        self,
        intent: str,
        text: str,
        user_context: Dict[str, Any],
        log_prefix: str,
    ) -> None:
        st = (self._memory_cfg or {}).get("slow_think") or {}
        if not st.get("enabled", False):
            return
        if intent not in set(st.get("intents") or []):
            return
        if len((text or "").strip()) < int(st.get("min_message_chars", 10)):
            return
        strat = user_context.get("_reply_strategy") or {}
        if strat.get("skip_ai"):
            return
        if not self.ai_client:
            return
        try:
            outline = await self.ai_client.slow_think_outline(
                user_message=(text or "").strip(),
                context=user_context,
                stage1_max_tokens=int(st.get("stage1_max_tokens", 400)),
            )
            if not outline:
                return
            user_context["_slow_think_outline"] = outline
            so = dict(strat)
            s2 = int(st.get("stage2_max_tokens", 0))
            if s2 > 0:
                cur = int(so.get("max_tokens", 512)) if so.get("max_tokens") else 512
                so["max_tokens"] = max(cur, s2)
            if st.get("stage2_temperature") is not None:
                so["temperature"] = float(st["stage2_temperature"])
            user_context["_reply_strategy"] = so
            try:
                from src.monitoring.metrics_store import get_metrics_store
                get_metrics_store().record_slow_think()
            except Exception:
                pass
            self.logger.info("%sslow_think outline_chars=%s", log_prefix, len(outline))
        except Exception as _e:
            self.logger.debug("slow_think skipped: %s", _e)

    def _enforce_persona_consistency(
        self, reply: str, *, chat_id: str = "", account_persona_id: str = "",
        log_prefix: str = "", peer_name: str = "", resolved_name: str = "",
    ) -> str:
        """后置人设守卫：剥离回复中漏出的禁用语 / AI 自曝身份（陪聊沉浸感保护）。

        仅当人设声明了 ``speaking.forbidden_phrases`` 或 ``identity.deny_ai`` 才有实际效果；
        守卫异常或剥离后为空时一律保留原回复（绝不因守卫吞掉回复）。

        ``peer_name``（2026-08-08「David Lin」事故）：对方显示名。传入后追加
        **错误自称名守卫**——AI 拿对方的名字（或对方名＋自己姓氏缝合）自称属
        身份级穿帮，按 persona_guard 分级判罚：hard 剥除 / soft 只记日志观测。

        ``resolved_name``（B74 实施67）：**生成时** prompt 身份硬锁用的名字
        （ai_client 写回 context ``_resolved_persona_name``）。守卫在此独立重解析
        人设构建白名单——两次解析在绑定断链/时序分叉时会不一致，AI 按 prompt
        指令自报的名字反被当「陌生名」抹掉（`_352`「叫我。」实录）。生成时名字
        直传白名单＝同源契约：prompt 让说的名字绝不该被守卫抹。
        """
        if not reply or not getattr(self, "_persona_guard_enabled", True):
            return reply
        out = reply
        persona: Optional[Dict[str, Any]] = None
        try:
            from src.utils.persona_manager import PersonaManager
            persona = PersonaManager.get_instance().get_persona(
                chat_id=chat_id, account_persona_id=account_persona_id
            )
        except Exception:
            persona = None
        try:
            from src.utils.persona_guard import sanitize
            # WP-4 诚实身份（compliance.disclosure.honest_identity，基线关）：
            # 开启时 AI 如实自认不再被剥（客服腔/禁语照剥）。flag 经进程级
            # provider 读实时配置；任何异常按关处理，守卫行为零风险。
            try:
                from src.compliance.runtime import honest_identity_active
                _honest = honest_identity_active()
            except Exception:
                _honest = False
            # #175 客服/销售组织框架腔（my assistant will / account details /
            # setup guide…）：陪聊域（conversion）一律开——账号人设未必配
            # deny_ai，但产品语义就是「真人朋友」；其他域交人设 deny_ai/
            # claim_human 推断（None）。子开关 companion.persona_guard.
            # service_frame.enabled（默认开）可整体关。
            _sf: Optional[bool] = None
            try:
                _pgc = (((self.config.config or {}).get("companion") or {})
                        .get("persona_guard") or {}) if self.config else {}
                _sfc = (_pgc.get("service_frame") or {}) if isinstance(
                    _pgc, dict) else {}
                if isinstance(_sfc, dict) and _sfc.get("enabled", True) is False:
                    _sf = False
                else:
                    from src.utils.domain_policy import effective_domain_name
                    if effective_domain_name(
                            self.config.config or {}) == "conversion":
                        _sf = True
            except Exception:
                _sf = None
            cleaned, violations = sanitize(out, persona or {},
                                           honest_identity=_honest,
                                           service_frame=_sf)
            if violations:
                # 日志说实话（2026-07-20）：sanitize 删光会回退原文（绝不返回空），
                # 此时并没有剥离任何内容——旧日志一律喊「已剥离」造成排查误导。
                if cleaned.strip() == out.strip():
                    self.logger.warning(
                        "%s[persona_guard] 命中人设违规片段 %r 但无法安全剥离"
                        "（整段违规→保留原文出站）", log_prefix, violations[:5],
                    )
                else:
                    self.logger.warning(
                        "%s[persona_guard] 拦截人设违规片段 %r（已剥离，保护沉浸感）",
                        log_prefix, violations[:5],
                    )
                out = cleaned or out
        except Exception:
            self.logger.debug("[persona_guard] 守卫异常，保留原回复", exc_info=True)
        # ── 错误自称名守卫（2026-08-08，随 persona_guard 总开关；子开关可单关）──
        try:
            _sn_on = True
            try:
                _pg_cfg = (((self.config.config or {}).get("companion") or {})
                           .get("persona_guard") or {}) if self.config else {}
                _sn_on = bool((_pg_cfg.get("self_name") or {}).get("enabled", True)) \
                    if isinstance(_pg_cfg.get("self_name"), dict) else True
            except Exception:
                _sn_on = True
            if _sn_on and out:
                from src.utils.persona_guard import (
                    build_self_name_allowlist, sanitize_self_name,
                )
                _extra: List[str] = []
                try:
                    from src.utils.persona_manager import PersonaManager as _PM_sn
                    _ai_cfg = ((self.config.config or {}).get("ai") or {}) \
                        if self.config else {}
                    _extra = [x for x in (
                        _PM_sn.resolve_spoken_name(
                            persona or {},
                            name_override=str(_ai_cfg.get("ai_name") or ""),
                            fallback=str(_ai_cfg.get("fallback_display_name") or ""),
                        ),
                    ) if x]
                except Exception:
                    _extra = []
                # B74（实施67）：生成时 prompt 锁定名直入白名单——守卫独立重解析
                # 与生成解析分叉时，AI 按身份硬锁自报的名字绝不该被抹。
                if str(resolved_name or "").strip():
                    _extra.append(str(resolved_name).strip())
                _allowed = build_self_name_allowlist(persona or {}, _extra)
                _peers = [peer_name] if str(peer_name or "").strip() else []
                cleaned2, _hard, _soft = sanitize_self_name(out, _allowed, _peers)
                if _hard:
                    if cleaned2.strip() == out.strip():
                        self.logger.warning(
                            "%s[persona_guard] 错误自称名 %r 但无法安全剥离"
                            "（保留原文出站）", log_prefix, _hard[:3],
                        )
                    else:
                        self.logger.warning(
                            "%s[persona_guard] 拦截错误自称名 %r（已剥离；"
                            "对方名=%r）", log_prefix, _hard[:3], peer_name[:40],
                        )
                    out = cleaned2 or out
                elif _soft:
                    self.logger.info(
                        "%s[persona_guard] 自称名观测（未拦）：%r 不在人设白名单",
                        log_prefix, _soft[:3],
                    )
                # ── 称呼混淆守卫（B42 2026-08-22，_236 实录「you're not that
                # old, Steven」）：上面守「拿对方名自称」，这里守镜像方向——
                # 用**自己的**人设名呼叫对方（呼格形态）。只抹名字 token 不动
                # 句子本体；同名客户不判（纯函数内兜）；对方名未知**照判**
                # （#96 0830 实锤改判——B 线无 peer 名管道曾致该路裸奔）。
                # 子开关 persona_guard.vocative.enabled（默认开，随总开关）。
                _voc_on = True
                try:
                    _voc_cfg = _pg_cfg.get("vocative") if isinstance(
                        _pg_cfg, dict) else None
                    if isinstance(_voc_cfg, dict):
                        _voc_on = bool(_voc_cfg.get("enabled", True))
                except Exception:
                    _voc_on = True
                if _voc_on and out:
                    from src.utils.persona_guard import strip_vocative_self_name
                    cleaned3, _voc_hits = strip_vocative_self_name(
                        out, _allowed, _peers)
                    if _voc_hits:
                        if cleaned3.strip() == out.strip():
                            self.logger.warning(
                                "%s[persona_guard] 用自己人设名呼叫对方 %r 但"
                                "无法安全剥离（保留原文出站）",
                                log_prefix, _voc_hits[:3],
                            )
                        else:
                            self.logger.warning(
                                "%s[persona_guard] 拦截「用自己人设名称呼对方」"
                                "%r（已抹呼格名；对方名=%r）",
                                log_prefix, _voc_hits[:3], peer_name[:40],
                            )
                        out = cleaned3 or out
        except Exception:
            self.logger.debug("[persona_guard] 自称名守卫异常，保留原回复",
                              exc_info=True)
        # ── #24 称呼互换守卫（0830 babe/baba 实锤）：档案配了双向称呼字段时，
        # 呼格位置的「对方叫你的称呼」出站前换回「你叫对方的称呼」。prompt
        # 硬钉子（_build_address_pin）是第一道，这里兜「钉了仍被带跑」的漏网。
        # 子开关 companion.persona_guard.address_swap.enabled（默认开）。
        try:
            _asw_on = True
            try:
                _pg_cfg2 = (((self.config.config or {}).get("companion") or {})
                            .get("persona_guard") or {}) if self.config else {}
                _asw_cfg = _pg_cfg2.get("address_swap") if isinstance(
                    _pg_cfg2, dict) else None
                if isinstance(_asw_cfg, dict):
                    _asw_on = bool(_asw_cfg.get("enabled", True))
            except Exception:
                _asw_on = True
            if _asw_on and out and isinstance(persona, dict):
                _nm24 = persona.get("names") or {}
                if isinstance(_nm24, dict) and (
                        _nm24.get("call_peer") or _nm24.get("peer_calls_you")):
                    from src.utils.persona_guard import swap_vocative_peer_call
                    cleaned4, _sw_hits = swap_vocative_peer_call(
                        out,
                        str(_nm24.get("call_peer") or ""),
                        str(_nm24.get("peer_calls_you") or ""))
                    if _sw_hits:
                        self.logger.warning(
                            "%s[persona_guard] 称呼互换已纠正（#24）：%r → %r",
                            log_prefix, _sw_hits[:3],
                            str(_nm24.get("call_peer") or "")[:20])
                        out = cleaned4 or out
        except Exception:
            self.logger.debug("[persona_guard] 称呼互换守卫异常，保留原回复",
                              exc_info=True)
        return out

    def _apply_outbound_text_guard(
        self, reply: str, log_prefix: str = "",
        user_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """出站文本形态守卫（实施74：B118 内心独白 + B121 语种混杂 + B104 无出处引用）。

        确定性纯文本后处理（`src/ai/outbound_text_guard`）：整段括号独白去壳、
        内嵌旁白剥除、拉丁主体句剥 CJK、新联系人「你之前说过」式编造引用剥句。
        生成端硬禁（persona_manager prompt 行）是第一道，这里保证坏形态绝不出站。
        A/B 两线同口径；任何异常保留原回复。

        B104 判据（从 ``user_context`` 推导，缺席=该守卫不动手）：用户轮数取
        ``_conversation_history`` 的 user 角色条数；**有 ``_conversation_summary``
        ＝历史被压缩过的老会话 → 轮数视为未知**（压缩后 len 变小，绝不能误判
        成新联系人）；``_episodic_memory_text`` 非空＝有长期记忆同样放行。
        """
        if not reply:
            return reply
        # #32② / #145⑤：出站不得主动提起对方已撤回的内容（对方本轮又说了则不剥）
        try:
            from src.inbox.withdrawn_cite import apply_to_reply
            _cid = ""
            _inbound = ""
            if isinstance(user_context, dict):
                _cid = str(
                    user_context.get("conversation_id")
                    or user_context.get("chat_id") or "")
                _hist = user_context.get("_conversation_history") or []
                for _m in reversed(_hist):
                    if isinstance(_m, dict) and _m.get("role") == "user":
                        _inbound = str(_m.get("content") or "")
                        break
            _cleaned, _hits = apply_to_reply(
                reply, conversation_id=_cid, inbound=_inbound)
            if _hits:
                self.logger.info(
                    "%s[withdrawn_cite] 剥离主动引用 %r", log_prefix, _hits[:5])
                reply = _cleaned
        except Exception:
            self.logger.debug("[withdrawn_cite] skip", exc_info=True)
        try:
            from src.ai.outbound_text_guard import (
                apply_outbound_text_guard, resolve_cfg,
            )
            cfg = resolve_cfg(
                (self.config.config or {}) if self.config else {})
            turns: Optional[int] = None
            has_mem = False
            _user_texts: List[str] = []
            _mem_text = ""
            _asst_texts: List[str] = []
            if isinstance(user_context, dict):
                try:
                    hist = user_context.get("_conversation_history") or []
                    turns = sum(
                        1 for m in hist
                        if isinstance(m, dict) and m.get("role") == "user")
                    if user_context.get("_conversation_summary"):
                        turns = None
                    _mem_text = str(
                        user_context.get("_episodic_memory_text") or "").strip()
                    has_mem = bool(_mem_text)
                    # #91-A/C 语料：用户侧原话（回忆断言接地）+ assistant 近史
                    # （认错口癖去重）。history 缺席=对应守卫自动不动手。
                    _user_texts = [
                        str(m.get("content") or "") for m in hist
                        if isinstance(m, dict) and m.get("role") == "user"]
                    _asst_texts = [
                        str(m.get("content") or "") for m in hist
                        if isinstance(m, dict) and m.get("role") == "assistant"]
                except Exception:
                    turns, has_mem = None, False
                    _user_texts, _mem_text, _asst_texts = [], "", []
            _rel_stage = ""
            _q36_cid = ""
            if isinstance(user_context, dict):
                _rel_stage = str(
                    user_context.get("relationship_stage") or "")
                # Q-36 #313：量词软改按会话取 24h 内入站图 caption（KV；缺席回落 _user_texts 解析）
                _q36_cid = str(
                    user_context.get("conversation_id")
                    or user_context.get("chat_id") or "")
            cleaned, meta = apply_outbound_text_guard(
                reply, cfg, user_turns=turns, has_memory=has_mem,
                user_texts=_user_texts, memory_text=_mem_text,
                recent_assistant_texts=_asst_texts,
                relationship_stage=_rel_stage,
                conversation_id=_q36_cid)
            if meta.get("caption_quantifier_hits"):
                # Q-36 #313（2JK95C「几个菜 → 一桌菜」）：可观测行——改了什么、依据哪条 caption
                self.logger.info(
                    "%s[caption-guard] conv=%s soft_rewrite=%s caption=%r（Q-36 量词改回识图原词）",
                    log_prefix, _q36_cid or "-",
                    "|".join(f"{h.get('from')}->{h.get('to')}" for h in meta["caption_quantifier_hits"][:3]),
                    str((meta["caption_quantifier_hits"][0] or {}).get("caption") or "")[:40],
                )
            # #152 F1：LLM 退化循环。整条都是循环（截空）→ 绝不回落原文照发——
            # 「越来越多×300」直达客户就是这条 `cleaned or reply` 回落造成的；
            # 返回空串让 A/B 线走各自的「空稿」路径（不发+进待处理）。截后有残文
            # 则照常放行残文并留 WARNING（残文是退化前的正常开头）。
            if meta.get("degenerate_unit") is not None:
                self.logger.warning(
                    "%s[outbound_text_guard] 拦截 LLM 退化循环 unit=%r "
                    "empty=%s（#152 F1）出站前=%r", log_prefix,
                    str(meta["degenerate_unit"])[:20],
                    bool(meta.get("degenerate_empty")), reply[:60])
                if meta.get("degenerate_empty"):
                    return ""
            if meta.get("monologue_hits"):
                self.logger.warning(
                    "%s[outbound_text_guard] 拦截内心独白/旁白 %r（B118）",
                    log_prefix, [h[:40] for h in meta["monologue_hits"][:3]],
                )
            if meta.get("recall_hits"):
                self.logger.warning(
                    "%s[outbound_text_guard] 拦截无出处引用 %r（B104，新联系人"
                    "turns=%s）", log_prefix,
                    [h[:40] for h in meta["recall_hits"][:3]], turns,
                )
            if meta.get("recall_grounding_hits"):
                self.logger.warning(
                    "%s[outbound_text_guard] 拦截现编回忆断言 %r（#91-A，"
                    "对不上历史/记忆原话）", log_prefix,
                    [h[:40] for h in meta["recall_grounding_hits"][:3]],
                )
            if meta.get("shared_past_hits"):
                self.logger.warning(
                    "%s[outbound_text_guard] 拦截虚构共同经历叙事 %r"
                    "（#110，stage=%s）", log_prefix,
                    [h[:40] for h in meta["shared_past_hits"][:3]],
                    _rel_stage or "-",
                )
            if meta.get("apology_hits"):
                self.logger.info(
                    "%s[outbound_text_guard] 认错口癖已换变体 %r（#91-C）",
                    log_prefix, [h[:20] for h in meta["apology_hits"][:3]],
                )
            if meta.get("lang_mix"):
                _lvl = self.logger.warning if str(
                    meta["lang_mix"]).startswith("hard") else self.logger.info
                _lvl(
                    "%s[outbound_text_guard] 语种混杂 %s（B121）出站前=%r",
                    log_prefix, meta["lang_mix"], reply[:60],
                )
            # #104（实施91，0831 原图 860「我这边也在下着小雨」实锤）：
            # 第一人称本地天气断言 vs 事实兜底——无天气事实（weather 关/无
            # 所在地/取数失败）全剥、有事实剥与 bucket 相斥的断言。prompt 层
            # 「无事实禁报天气」钉子是第一道，这里保证编造断言绝不出站。
            # 子开关 companion.weather.claim_guard（默认开）。
            try:
                _wx_guard_on = True
                _wcfg104 = (((self.config.config or {}).get("companion") or {})
                            .get("weather") or {}) if self.config else {}
                if isinstance(_wcfg104, dict):
                    _wx_guard_on = bool(_wcfg104.get("claim_guard", True))
                if _wx_guard_on and (cleaned or reply):
                    from src.companion.weather_state import (
                        strip_weather_claim_conflicts,
                    )
                    _wx_snap = (user_context or {}).get(
                        "_persona_weather_snap") if isinstance(
                            user_context, dict) else None
                    _wx_bucket = str(getattr(_wx_snap, "bucket", "") or "")
                    _wx_out, _wx_hits = strip_weather_claim_conflicts(
                        cleaned or reply, _wx_bucket)
                    if _wx_hits:
                        self.logger.warning(
                            "%s[outbound_text_guard] 拦截无接地/冲突天气断言"
                            " %r（#104，bucket=%s）", log_prefix,
                            [h[:40] for h in _wx_hits[:3]],
                            _wx_bucket or "无事实")
                        cleaned = _wx_out
            except Exception:
                self.logger.debug("[outbound_text_guard] 天气断言守卫异常"
                                  "（保留原文）", exc_info=True)
            return cleaned or reply
        except Exception:
            self.logger.debug(
                "[outbound_text_guard] 守卫异常，保留原回复", exc_info=True)
            return reply

    def _apply_temper_output_guard(
        self, reply: str, user_context: Dict[str, Any], log_prefix: str = "",
    ) -> str:
        """P1-b 出站认输句否决（2026-08-22）：指令层压不住时的确定性最后防线。

        骂战轮剥认输句（「懒得跟你吵/我睡觉去了/换个话题」）、记仇轮剥秒原谅句
        （「没事啦/我不生气了」）——实录规律认输句几乎总在结尾，剥尾保头零成本；
        整条被剥光才用按档位的确定性兜底短句。标记 ``_temper_out_guard`` 由
        temper 注入层按轮语义设置（熔断/翻篇收尾轮各自的退场/和解语义合法，
        不设标记），**读后即焚**。挂在危机兜底之前（危机覆盖永远是最后一道）。
        纯文本后处理，任何异常保留原回复。
        """
        mode = ""
        try:
            mode = str((user_context or {}).pop("_temper_out_guard", "") or "")
        except Exception:
            mode = ""
        if not reply or not mode:
            return reply
        try:
            from src.companion.temper import (
                parse_temper_cfg,
                record_out_guard,
                strip_surrender_lines,
            )
            _cfg = self.config.config if hasattr(self.config, "config") else {}
            if not parse_temper_cfg(
                    _cfg if isinstance(_cfg, dict) else None)["output_guard"]:
                return reply
            _lang = str(user_context.get("reply_lang") or "zh")
            cleaned, removed, used_fb = strip_surrender_lines(
                reply, mode, _lang)
            if removed:
                record_out_guard(fallback=used_fb)
                self.logger.info(
                    "%s[temper] 出站否决剥句 mode=%s removed=%d fallback=%s",
                    log_prefix, mode, removed, used_fb)
                return cleaned
        except Exception:
            self.logger.debug("temper 出站否决跳过", exc_info=True)
        return reply

    def _apply_crisis_safety_net(
        self, reply: str, *, user_context: Dict[str, Any], log_prefix: str = "",
    ) -> str:
        """R6 危机事后兜底（预防 R4 之上加一道事后保险）：

        ① **红线兜底**（默认开，无论是否检出输入危机）：若回复**自身**鼓励/认同自伤
           （如"那就去死吧"），整段覆盖为温柔的安全兜底——这是最不可接受的失败，必须拦下；
        ② **资源保障**（``crisis_resource_assurance`` 默认关）：severe 危机且配了热线且回复
           未提及求助时，温柔补一句资源。

        纯文本后处理，任何异常都保留原回复（绝不因兜底吞掉回复）。
        """
        if not reply:
            return reply
        try:
            from src.utils.wellbeing_guard import (
                detect_harmful_reply,
                safe_fallback_reply,
            )
            _cfg = self.config.config if hasattr(self.config, "config") else {}
            _wb = (
                ((_cfg.get("companion") or {}).get("wellbeing") or {})
                if isinstance(_cfg, dict) else {}
            )
            if not _wb.get("enabled", True):
                return reply
            hotline = str(_wb.get("crisis_resources", "") or "")
            level = str(user_context.get("_wellbeing_crisis_level", "") or "")

            # WP-4 危机转介持久计数（SB 243 年报数字；record 绝不抛不阻塞处置）
            def _referral(kind: str) -> None:
                try:
                    from src.compliance.crisis_referrals import (
                        record_crisis_referral)
                    record_crisis_referral(kind)
                except Exception:
                    pass

            if level == "severe":
                _referral("severe_detected")

            harmful = detect_harmful_reply(reply)
            if harmful:
                self.logger.error(
                    "%s[wellbeing] 回复触自伤红线 %r → 覆盖安全兜底",
                    log_prefix, harmful[:3],
                )
                user_context["_wellbeing_safety_override"] = True
                _referral("safe_reply_override")
                return safe_fallback_reply(level or "severe", hotline=hotline)

            if (
                level == "severe"
                and _wb.get("crisis_resource_assurance", False)
                and hotline
                and not any(k in reply for k in ("热线", "求助", "咨询", hotline))
            ):
                self.logger.warning(
                    "%s[wellbeing] severe 危机补附求助资源", log_prefix,
                )
                _referral("resource_appended")
                return reply.rstrip() + f"\n如果你愿意，也可以找人聊聊：{hotline}。"
            return reply
        except Exception:
            self.logger.debug("[wellbeing] crisis safety net skipped", exc_info=True)
            return reply

    def _inject_episodic_into_context(
        self,
        user_context: Dict[str, Any],
        user_id_str: str,
        chat_id: Any,
        current_user_text: str = "",
        query_embedding: Optional[List[float]] = None,
        platform: str = "",  # S5
    ) -> None:
        user_context.pop("_episodic_memory_text", None)
        if not self._episodic_store:
            return
        mcfg = self._memory_cfg or {}
        if not mcfg.get("enabled", True):
            return
        mx = int(mcfg.get("inject_max_items", 8))
        mc = int(mcfg.get("inject_max_chars", 1200))
        try:  # 上下文深度档只抬地板（standard 零变化）
            from src.ai import context_depth as _cd
            mx, mc = _cd.memory_limits(self.config, mx, mc)
        except Exception:
            pass
        key = self._episodic_storage_key(
            user_id_str, chat_id, platform, user_context=user_context)
        rr = bool(mcfg.get("inject_rerank_keywords", True))
        vcfg = mcfg.get("vector") or {}
        use_fusion = bool(vcfg.get("inject_fusion", True)) and bool(query_embedding)
        vw = float(vcfg.get("vector_weight", 0.5))
        kw_w = float(vcfg.get("keyword_weight", 0.5))
        # R2（REMT-lite）：情绪显著性 + 时间衰减重排（默认关 → 行为同旧版）。
        # 经 resolve_salience_rerank_cfg 容忍 salience_rerank / salience 两种键名。
        scfg = resolve_salience_rerank_cfg(mcfg)
        use_sal = bool(scfg.get("enabled", False))
        sw = float(scfg.get("salience_weight", 0.15))
        rw = float(scfg.get("recency_weight", 0.10))
        hl = float(scfg.get("recency_half_life_days", 30.0))
        _gb_kwargs = dict(  # J-10 A3：两种取法共用同一套参数
            query_text=(current_user_text or "").strip(),
            rerank_keywords=rr,
            query_embedding=query_embedding,
            use_vector_fusion=use_fusion,
            vector_weight=vw,
            keyword_weight=kw_w,
            use_salience_rerank=use_sal,
            salience_weight=sw,
            recency_weight=rw,
            recency_half_life_days=hl,
        )
        # J-10 A3：能给行 id 的 store 走 with_ids（召回记账用）；旧/假 store 走旧口
        _with_ids = getattr(self._episodic_store, "get_bullets_with_ids", None)
        _used_ids: List[int] = []
        if callable(_with_ids):
            txt, _used_ids = _with_ids(key, mx, mc, **_gb_kwargs)
        else:
            txt = self._episodic_store.get_bullets_for_prompt(key, mx, mc, **_gb_kwargs)
        if txt:
            user_context["_episodic_memory_text"] = txt
            try:
                from src.monitoring.metrics_store import get_metrics_store
                get_metrics_store().record_episodic_inject()
            except Exception:
                pass
            # J-10 A3：召回记账一行（best-effort 零阻断）——哪条记忆在哪个会话哪一轮被用
            try:
                _conv = str(user_context.get("conversation_id") or "")
                self._episodic_store.record_recall(
                    _used_ids, memory_key=key, conversation_id=_conv,
                    chain=("inbox" if _conv else "direct"),
                    inbound_msg_id=str(user_context.get("user_msg_id") or ""))
            except Exception:
                self.logger.debug("[episodic] recall log skipped", exc_info=True)
            if use_fusion:
                self.logger.debug(
                    "episodic inject fusion key=%s chars=%s", key, len(txt)
                )

    def _episodic_embeddings_needed(self) -> bool:
        """R7：写入期/补全是否需要落 embedding——任一向量消费方开启即需要。

        既有 ``memory.vector.enabled``（检索向量融合）或 R5
        ``memory.consolidation.semantic_dedup``（近义去重）任一为真，就应保证事实带
        embedding——覆盖率**跟随需求**自动普及，成本仍由各功能各自的显式开关把关。
        """
        mcfg = self._memory_cfg or {}
        if (mcfg.get("vector") or {}).get("enabled", False):
            return True
        return bool((mcfg.get("consolidation") or {}).get("semantic_dedup"))

    def _current_embedding_model(self) -> str:
        """当前 embedding 模型名（落库标注 episodic_memory.embedding_model 列，便于换模型后
        识别/清理异维度旧向量）。取 ai_client 实际生效的嵌入模型；取不到返回空串。"""
        try:
            return str(getattr(self.ai_client, "_embedding_model", "") or "")
        except Exception:
            return ""

    async def _episodic_patch_embedding(self, row_id: Optional[int], fact_text: str) -> None:
        if not row_id or not self._episodic_store:
            return
        if not self._episodic_embeddings_needed() or not self.ai_client:
            return
        ft = (fact_text or "").strip()
        if len(ft) < 2:
            return
        try:
            from src.utils.episodic_vector import vec_to_blob

            vecs = await self.ai_client.embed([ft[:500]])
            if vecs and vecs[0]:
                self._episodic_store.update_embedding(
                    row_id, vec_to_blob(vecs[0]), self._current_embedding_model()
                )
        except Exception as _e:
            self.logger.debug("episodic_patch_embedding id=%s: %s", row_id, _e)

    def _handle_episodic_forget_command(
        self, text: str, user_id_str: str, user_context: Dict[str, Any], chat_id: Any
    ) -> Optional[str]:
        """若用户要求清空记忆，清空库并返回确认语；否则 None。"""
        if not self._episodic_store:
            return None
        mcfg = self._memory_cfg or {}
        if not mcfg.get("enabled", True):
            return None
        phrases = list(mcfg.get("forget_phrases") or [])
        from src.utils.memory_heuristic import matches_forget_intent
        if not matches_forget_intent((text or "").strip(), phrases):
            return None
        _plat = (user_context or {}).get("platform", "")  # S5
        key = self._episodic_storage_key(
            user_id_str, chat_id, _plat, user_context=user_context)
        n = self._episodic_store.clear_user(key)
        user_context["last_message"] = (text or "").strip()
        user_context["last_message_time"] = time.time()
        user_context["current_intent"] = "direct_chat"
        self._memory_llm_last.pop(key, None)
        self._memory_llm_last.pop(user_id_str, None)
        self.logger.info("episodic memory cleared key=%s rows=%s", key, n)
        return "好的，已清空我这边为你记下的聊天要点，我们从头聊～"

    def _schedule_episodic_memory_extract(
        self, user_id: str, user_msg: str, reply: str, intent: str, chat_id: Any,
        platform: str = "",  # S5
        account_id: str = "",
        source: str = "",
    ) -> None:
        if not self._episodic_store:
            self.logger.info(
                "[episodic] schedule skip: no _episodic_store user=%s", user_id,
            )
            return
        if not (self._memory_cfg or {}).get("enabled", True):
            self.logger.info(
                "[episodic] schedule skip: memory.enabled=False user=%s", user_id,
            )
            return
        ex = (self._memory_cfg.get("extract") or {})
        if not ex.get("enabled", True):
            self.logger.info(
                "[episodic] schedule skip: memory.extract.enabled=False user=%s",
                user_id,
            )
            return
        skip_why = self._episodic_extract_skip_reason(
            user_id, chat_id, ex, source=source)
        if skip_why:
            self.logger.info(
                "[episodic] schedule skip: %s user=%s%s", skip_why, user_id,
                (f" source={source}" if source else ""),
            )
            return
        # source 只在非入站路径（O-2 B manual_out）带上，入站行文案与历史逐字一致
        self.logger.info(
            "[episodic] schedule run user=%s intent=%s msg_len=%d%s",
            user_id, intent, len(user_msg or ""),
            (f" source={source}" if source else ""),
        )

        async def _run():
            await self._episodic_memory_extract_async(
                user_id, user_msg, reply, intent, chat_id, platform,
                account_id=account_id,
            )

        try:
            asyncio.get_running_loop().create_task(_run())
        except RuntimeError:
            self.logger.warning(
                "[episodic] schedule failed: no running loop user=%s", user_id,
            )

    _MANUAL_OUT_MARK_KEY = "_episodic_manual_out_mark"

    @staticmethod
    def _episodic_extract_skip_reason(
        user_id: Any, chat_id: Any, ex: Dict[str, Any], *, source: str = "",
    ) -> str:
        """记忆抽取的**成本门禁**（2026-09-08 成本对账 P0）→ 跳过原因，空串＝放行。

        0908 账单实锤：LLM 抽取每天 40+ 次里绝大多数 ``llm_count=0``，且相当一部分
        打在根本不可能有「客户事实」的会话上——夜跑演练假号（990001xxx，还会把演练
        台词写进记忆库）、运维/报障群里机器人自己发的报告（manual_out 把群里多人的
        话拼成一段送去抽「客户」事实）。三条规则，都可配、默认从紧：

        - ``memory.extract.skip_drill``（默认 true）：演练号段一律不抽；
        - ``memory.extract.skip_chat_ids``（默认空）：显式点名的会话/群 id 不抽；
        - ``memory.extract.manual_out_groups``（默认 false）：坐席出站触发的抽取
          只对私聊做，群（负数 id）不做——群里的「客户」不是一个人。
        入站路径的群消息**不受**第三条影响（user_id 是说话的那个人，语义成立）。
        """
        try:
            uid = str(user_id or "").strip()
            cid = str(chat_id or "").strip()
            if ex.get("skip_drill", True):
                from src.utils.case_center import is_drill_uid
                if is_drill_uid(uid) or is_drill_uid(cid):
                    return "drill uid"
            skip_ids = ex.get("skip_chat_ids") or []
            if isinstance(skip_ids, (list, tuple, set)):
                wanted = {str(x).strip() for x in skip_ids if str(x).strip()}
                for cand in (uid, cid, uid.rsplit(":", 1)[-1], cid.rsplit(":", 1)[-1]):
                    if cand and cand in wanted:
                        return "chat id in memory.extract.skip_chat_ids"
            if source == "manual_out" and not ex.get("manual_out_groups", False):
                tail = uid.rsplit(":", 1)[-1]
                if tail.startswith("-") and tail[1:].isdigit():
                    return "manual_out on group chat"
            # 预算闸（P1）：当日云端消耗已超 ai.cost_guard.daily_budget_cny → 记忆抽取
            # 这类非关键用途先停，客户回复不受影响。启发式抽取（免费）仍在 async 侧照跑。
            from src.ai.cost_recon import allow_purpose
            if not allow_purpose("memory_extract"):
                return "daily budget exceeded (ai.cost_guard)"
        except Exception:
            return ""
        return ""

    def schedule_manual_outbound_extract(
        self, *, platform: str, account_id: str, chat_key: str, operator_text: str,
        conversation_id: str = "", inbox_store: Any = None,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """O-2 B（#201 / DY534Y）：坐席 ``origin=manual`` 出站成功后，把**客户这一轮对人工
        说的话**送进同一条抽取链。

        此前抽取只挂在「AI 回复之后」（A 线 process_message / B 线 generate_inbox_draft /
        persona_reply 写回）：人工接管中会话不拟稿 → 客户说的约定（地点 / 时间 / 生日）
        从不进记忆；#177（K-1 D）做的是坐席替人设说的**自述**进 ``_human_said_log``，
        不是客户事实抽取。本方法只补这条腿，抽取本体 / 意图门 / 白名单 / 冷却全部复用：

        - ``user_msg``＝会话里**上一条出站之后**的连续入站原文（客户这一轮说的），
          ``reply``＝坐席这条出站原文——与正常轮次 ``(user_msg, reply)`` 同结构：启发式正则
          只吃客户的话（坐席「我住在曼谷」绝不会被抽成客户事实），LLM 抽取拿坐席的确认
          作语境（「周六三点星巴克见」在客户句 / 坐席句里都能锚定）；
        - ``user=`` 仍是客户号（chat_key）；键派生与 B 线 ``generate_inbox_draft`` 同口径
          （``chat_id=""`` / ``account_id`` 缺省或 default 时从 conversation_id 补）→ 同一记忆桶；
        - 去重两道：① ``user_context["user_msg_id"]``＝B 线最近拟稿过的入站 message_id，
          与本轮最新入站相等＝那条入站已在拟稿时走过抽取（人审后手发的形态）→ 跳过；
          ② ``_episodic_manual_out_mark`` 记本方法消费过的最新入站 ts，坐席连发两条只抽一次；
        - 日志 ``[episodic] schedule run user=… source=manual_out``；各 skip 原因同前缀。

        返回 ``{"ok", "reason", "n_inbound", "intent"}`` 供测试 / 观测；绝不抛、零阻断发送。
        """
        res: Dict[str, Any] = {"ok": False, "reason": "", "n_inbound": 0, "intent": ""}
        peer = str(chat_key or "").strip()
        try:
            body = str(operator_text or "").strip()
            if not peer or not body:
                res["reason"] = "empty"
                return res
            if inbox_store is None or not str(conversation_id or "").strip():
                res["reason"] = "no_store"
                return res
            rows = list(inbox_store.list_recent_messages(str(conversation_id), limit=30) or [])
            # 尾部先跳过刚发出的出站行（本条 manual 已落库），再收「上一条出站之后」的连续入站
            i = len(rows) - 1
            while i >= 0 and str(rows[i].get("direction") or "") == "out":
                i -= 1
            run: List[Dict[str, Any]] = []
            while i >= 0 and str(rows[i].get("direction") or "") != "out":
                run.append(rows[i])
                i -= 1
            run.reverse()
            ctx = user_context if isinstance(user_context, dict) else {}
            try:
                mark = float(ctx.get(self._MANUAL_OUT_MARK_KEY) or 0.0)
            except (TypeError, ValueError):
                mark = 0.0
            run = [r for r in run if float(r.get("ts") or 0.0) > mark]
            if not run:
                res["reason"] = "no_fresh_inbound"
                self.logger.info(
                    "[episodic] schedule skip: source=manual_out no fresh inbound user=%s", peer)
                return res
            newest = run[-1]
            newest_ts = float(newest.get("ts") or 0.0)
            drafted_mid = str(ctx.get("user_msg_id") or "").strip()
            if drafted_mid and drafted_mid == str(newest.get("message_id") or "").strip():
                ctx[self._MANUAL_OUT_MARK_KEY] = newest_ts
                res["reason"] = "drafted"
                self.logger.info(
                    "[episodic] schedule skip: source=manual_out inbound already drafted "
                    "(mid=%s) user=%s", drafted_mid, peer)
                return res
            texts = [str(r.get("text") or "").strip() for r in run]
            texts = [t for t in texts if t]
            if not texts:
                ctx[self._MANUAL_OUT_MARK_KEY] = newest_ts
                res["reason"] = "no_text"
                self.logger.info(
                    "[episodic] schedule skip: source=manual_out inbound has no text user=%s", peer)
                return res
            user_msg = "\n".join(texts)
            if len(user_msg) > 1500:
                user_msg = user_msg[-1500:]   # 保留最近的话（约定通常在最后几句）
            try:
                intent = str(self._recognize_intent(user_msg) or "direct_chat")
            except Exception:
                intent = "direct_chat"
            acct = str(account_id or "").strip()
            if (not acct or acct == "default") and conversation_id:
                _parts = str(conversation_id).split(":", 2)
                if len(_parts) >= 3 and _parts[1]:
                    acct = str(_parts[1]).strip()
            self._schedule_episodic_memory_extract(
                peer, user_msg, body, intent, "", platform=str(platform or ""),
                account_id=acct, source="manual_out",
            )
            ctx[self._MANUAL_OUT_MARK_KEY] = newest_ts
            res.update({"ok": True, "n_inbound": len(texts), "intent": intent})
            return res
        except Exception:
            self.logger.debug("[episodic] manual_out schedule failed user=%s", peer, exc_info=True)
            res["reason"] = "error"
            return res

    async def _capture_birthday_fact(
        self, user_id: str, user_msg: str, reply: str, chat_id: Any,
        platform: str = "",
        account_id: str = "",
    ) -> None:
        """Stage S：本轮若出现用户生日（原话或 AI 确认）→ 规范化落库为 user_stated 事实。

        幂等：已知且相同 → 跳过；未知或**不同（用户更正）**→ 写入新规范事实。
        复用 ``extract_birthday``（关键词门控）作单一解析源，``resolve_birthday`` 复解析。
        """
        if not self._episodic_store:
            return
        from src.utils.birthday import birthday_fact_text, birthday_from_turn
        bd = birthday_from_turn(user_msg, reply)
        if bd is None:
            return
        key = self._episodic_storage_key(
            user_id, chat_id, platform, account_id=account_id)
        if not key:
            return
        try:
            if self.resolve_birthday(key) == bd:
                return  # 已知且一致，不重复落库
        except Exception:
            pass
        fact = birthday_fact_text(bd[0], bd[1])
        rid = self._episodic_store.add_fact(key, fact, "heuristic", source="user_stated")
        await self._episodic_patch_embedding(rid, fact)
        self.logger.info("[episodic] birthday captured user=%s %s", user_id, fact)

    async def _capture_residence_fact(
        self, user_id: str, user_msg: str, chat_id: Any,
        platform: str = "",
        account_id: str = "",
    ) -> None:
        """本轮若用户说出居住城市 → 落库为 user_stated 事实并作废时钟缓存。

        抽取出 ``stated_place_from_reply_text``（入站句式 + 短答白名单城）。
        已知且相同 → 跳过；不同（搬家）→ 写新事实。时钟 ``invalidate`` 用
        ``platform:account:peer`` 会话 id，下次 tick 才会按新城市 replace。
        """
        if not self._episodic_store:
            return
        from src.companion.user_clock_resolver import (
            invalidate,
            stated_place_from_reply_text,
        )
        place = stated_place_from_reply_text(user_msg)
        if not place:
            return
        key = self._episodic_storage_key(
            user_id, chat_id, platform, account_id=account_id)
        if not key:
            return
        try:
            known = self.resolve_residence(key)
            if known and str(known).strip().lower() == str(place).strip().lower():
                return
        except Exception:
            pass
        fact = f"我住在{place}"
        rid = self._episodic_store.add_fact(key, fact, "heuristic", source="user_stated")
        await self._episodic_patch_embedding(rid, fact)
        try:
            plat = str(platform or "").strip()
            acct = str(account_id or "").strip()
            peer = str(user_id or "").strip()
            if plat and acct and peer:
                invalidate(f"{plat}:{acct}:{peer}")
        except Exception:
            pass
        self.logger.info("[episodic] residence captured user=%s %s", user_id, fact)

    async def _episodic_memory_extract_async(
        self, user_id: str, user_msg: str, reply: str, intent: str, chat_id: Any,
        platform: str = "",  # S5
        account_id: str = "",
    ) -> None:
        if not self._episodic_store:
            self.logger.info(
                "[episodic] skip: no _episodic_store user=%s intent=%s",
                user_id, intent,
            )
            return
        # Stage S：生日即时回写——**独立于 intent/长度门控**，收到即解析落库，闭合 Stage R
        # 的采集环（问→答→立刻记住→当天庆）。即便本轮意图不可抽取，也不漏掉用户主动报的生日。
        try:
            await self._capture_birthday_fact(
                user_id, user_msg, reply, chat_id, platform, account_id=account_id)
        except Exception:
            self.logger.debug("[episodic] birthday capture skipped", exc_info=True)
        # 命理生辰即时回写（companion.bazi 开启时）——同 Stage S 机制：问→答→AI 复述
        # 确认→落库，下一轮追问即可排盘（闭合命理采集环，capture 内部自判开关）。
        try:
            await self._capture_birth_info_fact(
                user_id, user_msg, reply, chat_id, platform, account_id=account_id)
        except Exception:
            self.logger.debug("[episodic] birth info capture skipped", exc_info=True)
        # 居住地即时回写：问城市后的短答（「曼谷」）或入站自述（「我在曼谷」）
        # → 规范事实 + 作废用户时钟缓存。与生日同属「收到即解析」，不吃 intent 门。
        try:
            await self._capture_residence_fact(
                user_id, user_msg, chat_id, platform, account_id=account_id)
        except Exception:
            self.logger.debug("[episodic] residence capture skipped", exc_info=True)
        ex = (self._memory_cfg.get("extract") or {})
        if not should_extract_intent(intent, ex):
            self.logger.info(
                "[episodic] skip: intent=%r not extractable (match_all=%s intents=%s) user=%s",
                intent, bool(ex.get("match_all")), sorted(set(ex.get("intents") or [])), user_id,
            )
            return
        mu = (user_msg or "").strip()
        if len(mu) < int(ex.get("min_user_chars", 3)):
            self.logger.info(
                "[episodic] skip: msg too short len=%d user=%s",
                len(mu), user_id,
            )
            return

        key = self._episodic_storage_key(
            user_id, chat_id, platform, account_id=account_id)  # S5

        from src.utils.memory_heuristic import extract_heuristic_facts

        # P1 2026-08-19：记忆抽取只吃客户自己的话——剥掉 [图片内容]/[视频内容] 识别
        # 描述（聊天截图里被 VLM 抄录的「我是XX」会被正则当成**用户本人**自称入库）。
        # LLM 侧在 extract_memory_bullets 内同口径再剥一次（其他调用方同享）。
        try:
            from src.inbox.media_enrich import strip_media_desc
            mu_facts = strip_media_desc(mu or "")
        except Exception:
            mu_facts = mu

        try:
            # 五件套·溯源（#41 0830）：本轮抽取的事实一律登记「抽取自哪句原话
            # +何时」——「这条推断从哪来的」从无人能答变成条目自带答案。
            _prov_quote = str(mu_facts or mu or "").strip()[:200]
            _prov_ts = time.time()
            n_heuristic = 0
            _scope_facts: List[str] = []   # O-2 C：本轮全部事实文本，供去向日志判锚点
            for fact in extract_heuristic_facts(mu_facts):
                # R12：启发式事实从用户原话正则提取 → user_stated（高置信）
                rid = self._episodic_store.add_fact(
                    key, fact, "heuristic", source="user_stated",
                    source_quote=_prov_quote, source_ts=_prov_ts,
                )
                await self._episodic_patch_embedding(rid, fact)
                n_heuristic += 1
                _scope_facts.append(str(fact))

            # J-10 A1（#183）：LLM 抽取走引文级接地——每条事实带客户原话逐字引文
            # ``evidence``（原语言），护栏只核引文 → 外语客户的中文事实不再整条被丢。
            # 引文即 source_quote（比整句更准的溯源）；无引文时回退整句。
            # J-10 二期：三元组多带 LLM 数值置信（None＝模型没给）→ add_fact(confidence=)
            # 由 memory_review 判 low_confidence 进例外队列。
            facts_llm: List[Tuple[str, str, Optional[float]]] = []
            grounding_dropped: List[Dict[str, Any]] = []
            cooldown = float(ex.get("cooldown_seconds", 20))
            now = time.time()
            # Q-5 A（#263）：两链合一——**一次** LLM 调用同时产出记忆事实与画像槽位候选
            # （profile_fill.run_extraction）：companion.goals.profile_llm.enabled 与
            # memory.extract.use_llm 任一开即走；事实回到下面既有 add_fact 路径（ai_inferred），
            # 槽位在 profile_fill 内分发（apply source=ai_inferred status=mentioned，绝不自动
            # 升 confirmed）；昵称预填（B）与 [extract] 行 / 停滞计数（D）同在其内。
            # 冷却中 / 开关关 → 不传 ai_client（只做昵称预填 + 记账），日志 llm=0。
            try:
                from src.companion.goals import profile_fill as _pf
                _cfg_root = getattr(self.config, "config", None) or {}
                _llm_on, _ = _pf.llm_extract_enabled(_cfg_root, self._memory_cfg)
                _llm_go = bool(
                    _llm_on and self.ai_client
                    and (now - self._memory_llm_last.get(key, 0) >= cooldown))
                _ibx_pf = None
                try:
                    from src.integrations.protocol_bridge import get_inbox_store as _pf_gis
                    _ibx_pf = _pf_gis()
                except Exception:
                    _ibx_pf = None
                _ext = await _pf.run_extraction(
                    self.ai_client if _llm_go else None, _cfg_root,
                    getattr(self.config, "config_path", None),
                    user_msg=mu, reply=reply, platform=str(platform or ""),
                    chat_key=str(chat_id or "").strip() or str(user_id or ""),
                    account_id=str(account_id or ""), memory_cfg=self._memory_cfg,
                    inbox_store=_ibx_pf, heuristic_facts=n_heuristic,
                )
                facts_llm = [
                    (str(f), str(ev or ""), (float(c) if c is not None else None))
                    for f, ev, c in (_ext.get("facts") or []) if f
                ]
                grounding_dropped = list(_ext.get("dropped") or [])
                if _llm_go:
                    self._memory_llm_last[key] = time.time()
            except Exception:
                self.logger.debug("[episodic] merged extract skipped", exc_info=True)

            # Q-25 C（#295 #300）：记忆链与画像 / 目标回填**同一道门**——add_fact 前过共享事实门
            # fact_gate.check(kind=fact)：必须带客户原句 evidence 且逐字在客户入站里（无原句不写）；
            # 主体守卫：事实主体只能是客户或客户明确提到的人（「我出差去越南」绝不能写成「一起去
            # 越南」）；事实里的名字 / 数字须在原句里。不过 → 不写 + `[memory] drop reason=` 一行
            # + 计入 grounding_dropped 记账；门缺席 / 异常 → 不写（宁可漏记）。
            _gated_llm: List[Tuple[str, str, Optional[float]]] = []
            # Q-36 #318（VV7BRY「Marina」）：本条入站若带识图描述，evidence 落在 caption 而非客户原话的事实
            # 不当客户事实——改按 kind=observation 过门，写成固定措辞「TA 发过一张照片（识图所见：…）」
            # （画面所见 ≠ 客户自述；LLM 推断本身不写）。注：抽取 LLM 的输入已剥 caption（P1 08-19），
            # 这里是上游一旦不剥时的安全网；正常路径 caption 事实只经 image_observation KV。
            try:
                from src.inbox.image_observation import caption_from_inbound as _obs_cap_of
                _obs_caption = _obs_cap_of(mu)[0] if mu != (mu_facts or "") else ""
            except Exception:
                _obs_caption = ""
            for f, _ev, _conf in facts_llm:
                try:
                    from src.companion.fact_gate import check as _fg_check
                    _fg_ok, _fg_why = _fg_check(
                        f, slot_or_kind="fact", evidence=_ev,
                        inbound_texts=[mu_facts or mu])
                except Exception:
                    _fg_ok, _fg_why = False, "gate_error"
                if not _fg_ok and _obs_caption and _fg_why in ("unanchored", "no_evidence"):
                    try:
                        from src.companion.fact_gate import KIND_OBSERVATION as _K_OBS, check as _fg_check2
                        from src.inbox.image_observation import observation_line as _obs_line
                        _ev_in_cap = bool(str(_ev or "").strip()) and str(_ev).strip() in _obs_caption
                        _ob_ok, _ = _fg_check2(_obs_caption, slot_or_kind=_K_OBS, evidence=_obs_caption,
                                              inbound_texts=[mu]) if _ev_in_cap else (False, "")
                    except Exception:
                        _ob_ok = False
                    if _ob_ok:
                        _obs_fact = _obs_line(_obs_caption)
                        rid = self._episodic_store.add_fact(
                            key, _obs_fact, "observation", source="ai_inferred",
                            source_quote=_obs_caption[:200], source_ts=_prov_ts, confidence=_conf)
                        await self._episodic_patch_embedding(rid, _obs_fact)
                        self.logger.info(
                            "[memory] observation conv=%s fact=%s dropped_inference=%s（evidence 来自识图 caption，标 observation）",
                            key, _obs_fact[:60], str(f)[:40].replace("\n", " "))
                        continue
                if not _fg_ok:
                    self.logger.info(
                        "[memory] drop conv=%s fact=%s evidence=%s reason=%s",
                        key, str(f)[:60].replace("\n", " "),
                        str(_ev or "")[:40].replace("\n", " "), _fg_why or "unanchored")
                    grounding_dropped.append(
                        {"fact": str(f), "evidence": str(_ev or ""), "reason": _fg_why or "unanchored"})
                    continue
                _gated_llm.append((f, _ev, _conf))
            facts_llm = _gated_llm

            for f, _ev, _conf in facts_llm:
                # R12：LLM 抽取是对话推断/概括 → ai_inferred（晋升/推翻 stable 需更高置信）
                rid = self._episodic_store.add_fact(
                    key, f, "llm", source="ai_inferred",
                    source_quote=(_ev.strip()[:200] or _prov_quote), source_ts=_prov_ts,
                    confidence=_conf,   # J-10 二期
                )
                await self._episodic_patch_embedding(rid, f)

            # O-2 C（#201 / W7ZTSB 收口）：去向一行。抽取链**只写该客户 episodic**（上面两个
            # add_fact 都按客户键落库，没有任何共享 KB / 通用事实写口）——scope 恒 customer；
            # reason 给出本轮事实里的私事锚点（memory_scope 与 DailyLearner 分流同一口径），
            # 无锚点写 episodic_only（客户偏好等仍是客户记忆，不因无锚点被丢）。
            _n_facts_total = n_heuristic + len(facts_llm)
            if _n_facts_total:
                try:
                    from src.utils.memory_scope import personal_anchors
                    _scope_facts.extend(f for f, _e, _c in facts_llm)
                    _anchors = personal_anchors("\n".join(_scope_facts))
                    self.logger.info(
                        "[episodic] scope=customer reason=%s facts=%d user=%s",
                        ("anchors:" + ",".join(_anchors)) if _anchors else "episodic_only",
                        _n_facts_total, user_id,
                    )
                except Exception:
                    self.logger.debug("[episodic] scope log skipped", exc_info=True)

            if grounding_dropped:
                # 观测：按原因记账 → admin_summary → 页面「有 N 条因无法核对原话未记录」
                try:
                    _rec = getattr(self._episodic_store, "record_grounding_drops", None)
                    if callable(_rec):
                        _rec(key, grounding_dropped)
                except Exception:
                    self.logger.debug("[episodic] grounding drop record failed", exc_info=True)

            # R3：裁剪前先做离线巩固——把复发/情绪浓的事实晋升 stable（永不被裁剪）
            ccfg = self._memory_cfg.get("consolidation") or {}
            if ccfg.get("enabled", False):
                try:
                    _ms = ccfg.get("min_salience")
                    # R5：语义近似去重阈值（None=关；开则先并近义再晋升）
                    _dd = ccfg.get("semantic_dedup")
                    _dd_thr = None
                    if _dd:
                        _dd_thr = float(_dd) if not isinstance(_dd, bool) else 0.92
                    # J-10 A2（D8）：第二条晋升路径「N 天无冲突且被召回 ≥1」——
                    # memory.consolidation.auto_promote.{days, min_recalls}；false 关。
                    _ap = ccfg.get("auto_promote", {})
                    _ap_days: Optional[float] = 7.0
                    _ap_min = 1
                    if _ap is False:
                        _ap_days = None
                    elif isinstance(_ap, dict):
                        if _ap.get("enabled", True) is False:
                            _ap_days = None
                        else:
                            _ap_days = float(_ap.get("days", 7))
                            _ap_min = int(_ap.get("min_recalls", 1))
                    res = self._episodic_store.consolidate(
                        key,
                        min_hits=int(ccfg.get("min_hits", 2)),
                        min_salience=(float(_ms) if _ms is not None else None),
                        dedup_threshold=_dd_thr,
                        auto_promote_days=_ap_days,
                        auto_promote_min_recalls=_ap_min,
                        # #96（实施91）：默认开——0831 实锤同客户三条年龄条目
                        # 并存打架（22岁/21岁/今年21岁），矛盾消解保留最新、
                        # 旧值标 stale（保留备查，绝不硬删）。显式 false 仍可关。
                        resolve_contradictions=bool(
                            ccfg.get("resolve_contradictions", True)
                        ),
                        # R11：新证据推翻旧 stable 结论（搬家/分手）；默认关
                        supersede_stable=bool(ccfg.get("supersede_stable", False)),
                        stable_min_hits=int(ccfg.get("stable_min_hits", 2)),
                        # R12：按来源分级置信（ai_inferred 晋升/推翻门槛更高）；默认关
                        source_aware=bool(ccfg.get("source_aware", False)),
                        inferred_min_hits=(
                            int(ccfg["inferred_min_hits"])
                            if ccfg.get("inferred_min_hits") is not None
                            else None
                        ),
                    )
                    if (
                        res.get("promoted") or res.get("merged")
                        or res.get("superseded") or res.get("stable_superseded")
                        or res.get("gray_pairs") or res.get("held_merges")
                    ):
                        self.logger.info(
                            "[episodic] consolidate key=%s promoted=%s merged=%s "
                            "gray=%s held=%s observe_only=%s superseded=%s "
                            "stable_superseded=%s stable=%s",
                            key, res.get("promoted"), res.get("merged"),
                            res.get("gray_pairs"), res.get("held_merges"),
                            res.get("observe_only"),
                            res.get("superseded"), res.get("stable_superseded"),
                            res.get("stable_total"),
                        )
                except Exception:
                    self.logger.debug("episodic consolidate failed", exc_info=True)
            keep = int(self._memory_cfg.get("max_items_per_user", 40))
            pr = self._episodic_store.prune_oldest(key, keep)
            if pr:
                self.logger.debug("episodic pruned key=%s removed=%s", key, pr)
            # P3-deep 诊断：写入数量统计
            self.logger.info(
                "[episodic] extract done key=%s heuristic_count=%d llm_count=%d "
                "intent=%s msg_len=%d grounding_dropped=%d",
                key, n_heuristic, len(facts_llm), intent, len(mu), len(grounding_dropped),
            )
        except Exception as _e:
            self.logger.warning("[episodic] extract failed key=%s: %s", key, _e)

    def episodic_list_for_admin(
        self, prefix: str = "", limit: int = 100, source: str = "",
        q: str = "", q_keys: Optional[List[str]] = None, offset: int = 0,
        status: str = "", review: str = "",
    ) -> List[Dict[str, Any]]:
        """后台记忆列表。``q``/``q_keys``＝身份化联合搜索；``offset``＝加载更多分页；
        J-10 A2 ``status``（ignored / all）/ ``review``（pending / 原因）例外标记透传
        （均见 store.list_rows）。

        新参缺省时保持旧三参调用形状——测试里大量老签名 fake store 依赖该形状。
        """
        if not self._episodic_store:
            return []
        extra: Dict[str, Any] = {}
        if q:
            extra.update(q=q, q_keys=q_keys or [])
        if offset:
            extra["offset"] = int(offset)
        if status and status != "active":
            extra["status"] = str(status)   # J-10 A2：软删视图（ignored / all）
        if review:
            extra["review"] = str(review)   # J-10 A2：例外队列筛选（pending / 原因）
        if extra:
            return self._episodic_store.list_rows(
                prefix=prefix, limit=limit, source=source, **extra,
            )
        return self._episodic_store.list_rows(prefix=prefix, limit=limit, source=source)

    def episodic_delete_for_admin(self, row_id: int) -> bool:
        if not self._episodic_store:
            return False
        return self._episodic_store.delete_by_id(int(row_id))

    def episodic_confirm_for_admin(self, row_id: int) -> Optional[str]:
        """R15/R16：确认一条 AI 推断，升格 user_stated + 置 stable。

        返回被确认的 ``content``（供路由层写审计），未命中/未启用返回 ``None``。
        """
        store = getattr(self, "_episodic_store", None)
        if not store or not hasattr(store, "confirm_inferred_fact"):
            return None
        try:
            return store.confirm_inferred_fact(int(row_id))
        except Exception:
            return None

    @staticmethod
    def _story_progress_from_context(ctx: Dict[str, Any]):
        """从持久化的 user_context 汇总剧情完成足迹 + 累计加成（跨 rel_state 键 union）。

        proactive 路径用 ``memory_key`` 取 context，但 rel_state 按 ``chat_storage_key``
        分桶、键不必等于 memory_key；私聊一个对端通常仅一桶，故**并集**所有桶的
        ``story_done``/``story_outcomes`` 最稳——避免键不匹配导致漏判/误邀。
        返回 ``(completed: {sid: ending}, story_bonus: float)``。
        """
        completed: Dict[str, str] = {}
        bonus = 0.0
        root = ctx.get("companion_relationship") if isinstance(ctx, dict) else None
        if isinstance(root, dict):
            for st in root.values():
                if not isinstance(st, dict):
                    continue
                for sid in (st.get("story_done") or []):
                    completed.setdefault(str(sid), "")
                oc = st.get("story_outcomes")
                if isinstance(oc, dict):
                    for sid, end in oc.items():
                        completed[str(sid)] = str(end or "")
                try:
                    bonus = max(bonus, float(st.get("story_bonus", 0) or 0))
                except (TypeError, ValueError):
                    pass
        return completed, bonus

    def _proactive_crisis_window_days(self) -> float:
        """主动护栏的危机回看窗（天）：``companion.proactive_topic.crisis_guard_days``，默认 14。"""
        try:
            cfg = self.config.config if hasattr(self.config, "config") else {}
            pt = (cfg.get("companion") or {}).get("proactive_topic") or {}
            return float(pt.get("crisis_guard_days", 14) or 14)
        except Exception:
            return 14.0

    def _proactive_emotion_gate(
        self, memory_key: str, last_emotion: str = "",
        last_emotion_intensity: float = -1.0,
    ) -> str:
        """主动开场前的情绪护栏档位：``"block"`` / ``"soft"`` / ``""``（见 wellbeing_guard）。

        以 memory_key 反查该用户最近危机事件（crisis_event_store，已就绪才查）；窗口内
        severe→block、elevated→soft；末条负面情绪→soft。任何失败 → ``""``（不抑制，
        交后续正常关怀兜底——护栏只做「该静默时静默」，绝不反向阻断关怀）。
        """
        try:
            latest = None
            if getattr(self, "_crisis_store", None) is not None:
                latest = (self.crisis_summary_for_user(memory_key, limit=3)
                          or {}).get("latest")
            if latest is None and not str(last_emotion or "").strip():
                return ""
            from src.utils.wellbeing_guard import proactive_emotion_gate
            _ei = (float(last_emotion_intensity)
                   if last_emotion_intensity is not None and float(last_emotion_intensity) >= 0
                   else None)
            return proactive_emotion_gate(
                latest, now=time.time(),
                window_days=self._proactive_crisis_window_days(),
                last_emotion=last_emotion,
                last_emotion_intensity=_ei,
            )
        except Exception:
            self.logger.debug("proactive emotion gate skipped", exc_info=True)
            return ""

    def _proactive_story_invite(
        self, memory_key: str, intimacy: float
    ) -> Optional[Dict[str, Any]]:
        """沉默期主动剧情邀约：挑一个「已解锁但未经历」的免费剧情发出温暖邀约。

        准入复用 ``story_engine.select_story_invite``（关系/前置已满足 + 免费 + 未完成）；
        关系等级用 **effective intimacy**（基础分 + 封顶剧情加成）算，与对话面/健康卡同源。
        story 未启用 / 关闭 invite / 无 context / 无可邀约 → 返回 None（回落记忆话题）。
        """
        scfg = self._story_cfg()
        if not scfg.get("enabled", False):
            return None
        if not bool(scfg.get("proactive_invite", True)):
            return None
        scenarios = self._story_scenarios()
        store = getattr(self, "_context_store", None)
        key = str(memory_key or "").strip()
        if not scenarios or store is None or not key:
            return None
        try:
            ctx = store.get(key)
        except Exception:
            return None
        completed, bonus = self._story_progress_from_context(ctx if isinstance(ctx, dict) else {})
        try:
            eff = max(0.0, min(100.0, float(intimacy or 0.0)
                               + min(self._story_bonus_cap(), bonus)))
        except (TypeError, ValueError):
            eff = float(intimacy or 0.0)
        try:
            from src.contacts.relationship_level import compute_bond_level
            bond_level = int(compute_bond_level(eff).get("level", 0))
        except Exception:
            bond_level = 0
        from src.skills.story_engine import (
            ending_memory,
            satisfied_prerequisite,
            select_story_invite,
        )
        inv = select_story_invite(
            scenarios, bond_level=bond_level, completed=completed)
        if not inv:
            return None
        sid = inv["scenario_id"]
        title = inv["title"]
        scn = scenarios.get(sid) or {}
        # 个性化召回（Phase ④续⁶）：若是「续作」且用户已走过前传，把那次的共同经历
        # （前传标题 + 该结局回写的共享记忆）自然织进邀约 → 召回有回忆钩子、不空泛。
        callback = ""
        prereq = satisfied_prerequisite(scn, completed)
        if prereq:
            pid, pend = prereq
            ptitle = self._scenario_title(scenarios, pid)
            pmem = ending_memory(scenarios.get(pid) or {}, pend)
            callback = f"《{ptitle}》" + (f"（{pmem}）" if pmem else "")
        if callback:
            directive = (
                f"你和TA一起经历过{callback}。你想顺着那段共同经历，邀TA一起开启续作《{title}》。"
                f"用一句温暖、不突兀的话发出邀约——先自然提起上次那段经历，再顺势提议要不要"
                f"一起继续这段故事。别用菜单/命令口吻、别罗列、别催。"
            )
        else:
            directive = (
                f"你想邀TA一起开启一段你们还没经历过的新故事《{title}》。用一句温暖、不突兀的话"
                f"发出邀约——可先轻轻提一下你们关系的靠近，再顺势提议要不要一起经历这段故事。"
                f"别用菜单/命令口吻、别罗列、别催。"
            )
        return {
            "mode": "story_invite",
            "fact": title,
            "directive": directive,
            "scenario_id": sid,
            "context_facts": [],
            "silent_hours": 0.0,
        }

    def _proactive_story_teaser(
        self, memory_key: str, intimacy: float, contact_key: str
    ) -> Optional[Dict[str, Any]]:
        """Stage 2 付费解锁预告：挑一个用户「关系/前置已满足、只差付费」的剧情发温暖预告。

        准入：story 启用 + ``paid_teaser`` 开 + 有 context + **解析到真实权益**（经 Stage 1
        ``resolve_entitlement``）。复用 ``story_engine.select_paid_teaser``（仅选 ``need_unlock``-only
        场景；已解锁者 reason 为空 → 不会被选 → 不骚扰付费用户）。关系等级用 effective intimacy 算。
        无 contact_key / 无权益源（变现未就绪）/ 无可预告 → None（回落记忆话题，不空推）。
        """
        scfg = self._story_cfg()
        if not scfg.get("enabled", False):
            return None
        if not bool(scfg.get("paid_teaser", False)):
            return None
        scenarios = self._story_scenarios()
        store = getattr(self, "_context_store", None)
        key = str(memory_key or "").strip()
        ck = str(contact_key or "").strip()
        if not scenarios or store is None or not key or not ck:
            return None
        # 解析端用户真实权益；无权益源（resolver 未注册/查不到）→ 不预告（不对未知状态乱推）
        from src.utils.companion_context import resolve_entitlement
        ent = resolve_entitlement(ck)
        if not isinstance(ent, dict):
            return None
        try:
            ctx = store.get(key)
        except Exception:
            return None
        completed, bonus = self._story_progress_from_context(
            ctx if isinstance(ctx, dict) else {})
        try:
            eff = max(0.0, min(100.0, float(intimacy or 0.0)
                               + min(self._story_bonus_cap(), bonus)))
        except (TypeError, ValueError):
            eff = float(intimacy or 0.0)
        try:
            from src.contacts.relationship_level import compute_bond_level
            bond_level = int(compute_bond_level(eff).get("level", 0))
        except Exception:
            bond_level = 0
        from src.skills.story_engine import select_paid_teaser
        tea = select_paid_teaser(
            scenarios, bond_level=bond_level, completed=completed, entitlement=ent)
        if not tea:
            return None
        title = tea["title"]
        directive = (
            f"你心里惦记着一段你和TA还没一起经历、但你很想带TA去体验的特别故事《{title}》。"
            f"用一句温暖、带点向往的话自然提起这段「专属故事」，让TA感到你想和TA一起解锁这段"
            f"特别的经历——只勾起期待与靠近感。别报价格、别像广告推销、别催、别罗列菜单。"
        )
        return {
            "mode": "story_teaser",
            "fact": title,
            "directive": directive,
            "scenario_id": tea["scenario_id"],
            "feature": tea.get("feature", ""),
            "context_facts": [],
            "silent_hours": 0.0,
        }

    def build_proactive_opener(
        self,
        memory_key: str,
        *,
        silent_hours: float,
        stage: str = "",
        intimacy: float = 0.0,
        min_silent_hours: float = 24.0,
        last_emotion: str = "",
        last_emotion_intensity: float = -1.0,
        contact_key: str = "",
    ) -> Dict[str, Any]:
        """P1：为某用户挑一个"主动开场话题"（从其高置信记忆回访）。

        返回 ``{mode, fact, directive, ...}``；记忆库不可用或沉默不足时 mode 为空。
        只回访 user_stated/已确认事实（不拿 AI 推断去回访，猜错伤信任）。
        """
        empty = {"mode": "", "fact": "", "directive": "", "silent_hours": 0.0}
        # Phase ④续⁷：情绪自适应护栏——近期危机/低落时抑制主动打扰，绝不在情绪低谷
        # 推「播放性」内容（如约会剧情邀约）。severe 近期危机 → 完全不主动；elevated/负面
        # → 仅抑制剧情邀约、保留温和问候。护栏失效不阻断正常关怀（异常→无抑制）。
        _gate = self._proactive_emotion_gate(
            memory_key, last_emotion, last_emotion_intensity)
        if _gate == "block":
            # severe 近期危机：不静默放弃，而是带「危机关怀升级」信号交由派发层
            # 把该用户排进 care 队列（人工/关怀兜底）——把"静默"变"接住"。
            # mode 仍为空 → 不会被当作普通主动文案发出；blocked 字段供 plan 识别升级。
            return {"mode": "", "fact": "", "directive": "",
                    "silent_hours": 0.0, "blocked": "crisis_severe"}
        # Phase ④续⁵：主动剧情邀约——沉默期把「已解锁但未经历」的新剧情接进 re-engagement
        # 闭环（剧情解锁→主动邀约→回流→更多剧情）。优先于记忆回访（新内容钩子更强），
        # 无可邀约时无缝回落到记忆话题。soft 档（情绪低落）抑制邀约，仅走温和记忆问候。
        if _gate != "soft":
            try:
                _inv = self._proactive_story_invite(memory_key, intimacy)
                if _inv:
                    _inv["silent_hours"] = round(float(silent_hours or 0.0), 1)
                    return _inv
            except Exception:
                self.logger.debug("proactive story invite skipped", exc_info=True)
            # Stage 2：付费解锁预告——无免费可邀约时，若用户已"够格"某付费剧情（只差付费），
            # 发温暖预告勾起向往、引导解锁（转化驱动）。同样 soft/block 抑制（不在低谷推销）。
            try:
                _tea = self._proactive_story_teaser(memory_key, intimacy, contact_key)
                if _tea:
                    _tea["silent_hours"] = round(float(silent_hours or 0.0), 1)
                    return _tea
            except Exception:
                self.logger.debug("proactive story teaser skipped", exc_info=True)
        store = getattr(self, "_episodic_store", None)
        key = str(memory_key or "").strip()
        if not store or not key or not hasattr(store, "list_rows"):
            return empty
        try:
            from src.utils.proactive_topic import select_proactive_topic
            facts = store.list_rows(prefix=key, limit=50) or []
            # Phase ④：优先回访剧情回写的「共享经历」（story 类目）→ 转动飞轮。
            _pref = "story"
            try:
                _pref = str(
                    ((self._story_cfg() or {}).get("proactive_prefer_category"))
                    or "story"
                )
            except Exception:
                _pref = "story"
            _topic = select_proactive_topic(
                facts, silent_hours=silent_hours, stage=stage,
                intimacy=intimacy, min_silent_hours=min_silent_hours,
                prefer_category=_pref,
                variety_key=str(contact_key or key),
            )
            # C2：无记忆话题可回访时，用人设自己的生活片段主动分享近况（更像真人在过日子）。
            # P1 修死路径（2026-07-29）：select 在沉默达标时**必返回** gentle_checkin
            # （mode 恒非空），旧判断 `if not mode` 永假 → 生活分享/天气开场从未触发
            # （生产 outreach_log 48/48 全是 gentle_checkin 的根因之一）。改为：
            # 无记忆钩子（gentle_checkin）先试 生活分享 → 天气强信号，都无货再落回
            # 温和问候；有记忆钩子（follow_up）优先级不变。
            # 配额语义同步修正：opener 构建不再扣生活分享周配额（record=False——
            # 规划器对每个过闸候选都会构建 opener，构建即扣会把每周 2 次的配额
            # 烧在从未发出的计划上），真发成功后经 mark_life_share_sent 落账。
            from src.utils.proactive_topic import MODE_GENTLE_CHECKIN as _GC
            if str((_topic or {}).get("mode") or "") == _GC:
                _gapb = str((_topic or {}).get("gap_bucket") or "")
                _life = self._life_beat_opener(
                    contact_key or key, gate=_gate, record=False)
                if _life:
                    _life["silent_hours"] = round(float(silent_hours or 0.0), 1)
                    _life["gap_bucket"] = _gapb
                    return _life
                # 天气强信号开场（暴雨/雷暴/极端气温）：记忆与生活线都空时的轻量钩子
                _wx = self._weather_opener(contact_key or key, gate=_gate)
                if _wx:
                    _wx["silent_hours"] = round(float(silent_hours or 0.0), 1)
                    _wx["gap_bucket"] = _gapb
                    return _wx
                # 平常天气也给事实（2026-08-16 聊真实世界 P1）：checkin 切入角里
                # 「聊聊窗外的天气」此前只有方向没有值——非强信号时把人设城市的
                # 真实天气拼进 directive（不改变开场主题选择；软失败含无此绑定的
                # 轻量调用方；soft 情绪档保持克制不带素材）。
                if _gate != "soft":
                    try:
                        _wl = self._checkin_weather_line(contact_key or key)
                        if _wl and str(_topic.get("directive") or ""):
                            _topic["directive"] = str(_topic["directive"]) + _wl
                    except Exception:
                        pass
            return _topic
        except Exception:
            return empty

    def _weather_opener(self, contact_key: str, *, gate: str = "") -> Dict[str, Any]:
        """天气强信号主动开场（companion.weather.enabled + proactive_hook）。

        仅 storm/大雨/大雪/极端气温触发；soft 情绪档仍可发（天气关怀比剧情邀约克制）。
        """
        try:
            if gate == "block":
                return {}
            _cfg = self.config.config if hasattr(self.config, "config") else (
                self.config if isinstance(self.config, dict) else {})
            _wcfg = (_cfg.get("companion", {}) or {}).get("weather") or {}
            if not (isinstance(_wcfg, dict) and _wcfg.get("enabled")
                    and _wcfg.get("proactive_hook", True)):
                return {}
            from src.utils.persona_manager import PersonaManager
            from src.companion.persona_location import resolve_place_with_fallback
            from src.companion.weather_state import (
                fetch_weather, weather_proactive_hook,
            )
            pm = PersonaManager.get_instance()
            persona, _ = pm.get_persona_with_tier(str(contact_key or ""), "")
            place = resolve_place_with_fallback(persona if isinstance(persona, dict) else {})
            if place is None:
                return {}
            snap = fetch_weather(
                place,
                ttl_sec=int(_wcfg.get("ttl_sec") or 1800),
                max_stale_sec=int(_wcfg.get("max_stale_sec") or 10800),
            )
            hook = weather_proactive_hook(snap, "zh")
            if not hook:
                return {}
            return {
                "mode": "weather_hook",
                "fact": "",
                "directive": hook,
                "context_facts": [],
            }
        except Exception:
            self.logger.debug("weather opener skipped", exc_info=True)
            return {}

    def _ritual_weather_line(self, contact_key: str, slot: str) -> str:
        """每日仪式问候的「真实天气」素材行（companion.weather.greeting_inject）。

        「从天气/窗外说起」的切入角此前只有方向没有事实——反编造钉子会正确拦住
        LLM 编数值，问候只能落在安全壳（2026-08-16 聊真实世界 P1）。软失败：
        关闭/缺人设坐标/取数失败一律返回 ""，问候主流程零改变。
        """
        try:
            _cfg = self.config.config if hasattr(self.config, "config") else (
                self.config if isinstance(self.config, dict) else {})
            _wcfg = (_cfg.get("companion", {}) or {}).get("weather") or {}
            if not (isinstance(_wcfg, dict) and _wcfg.get("enabled")):
                return ""  # 便宜闸门：关着就不碰 PersonaManager 单例
            from src.companion.daily_brief import ritual_weather_line
            from src.utils.persona_manager import PersonaManager
            pm = PersonaManager.get_instance()
            persona, _ = pm.get_persona_with_tier(str(contact_key or ""), "")
            return ritual_weather_line(persona, _cfg, slot=slot)
        except Exception:
            self.logger.debug("ritual weather line skipped", exc_info=True)
            return ""

    def _checkin_weather_line(self, contact_key: str) -> str:
        """温和问候的「真实天气」素材行——与 ``_weather_opener``（暴雨/极端气温
        强信号才把天气当**开场主题**）互补：平常天气也给真实值，喂给「从窗外/
        天气说起」的切入角（2026-08-16 聊真实世界 P1）。软失败同上。"""
        try:
            _cfg = self.config.config if hasattr(self.config, "config") else (
                self.config if isinstance(self.config, dict) else {})
            _wcfg = (_cfg.get("companion", {}) or {}).get("weather") or {}
            if not (isinstance(_wcfg, dict) and _wcfg.get("enabled")):
                return ""
            from src.companion.daily_brief import checkin_weather_line
            from src.utils.persona_manager import PersonaManager
            pm = PersonaManager.get_instance()
            persona, _ = pm.get_persona_with_tier(str(contact_key or ""), "")
            return checkin_weather_line(persona, _cfg)
        except Exception:
            self.logger.debug("checkin weather line skipped", exc_info=True)
            return ""

    def _life_beat_opener(
        self, contact_key: str, *, gate: str = "", record: bool = True,
    ) -> Dict[str, Any]:
        """C2：解析当前人设 → 生成"生活线主动分享"开场（deep_persona.life_line 开才生效）。

        ``record=False``（P1 规划路径）：只构建不扣周配额——真发成功后由
        ``mark_life_share_sent`` 落账；默认 True 保持旧调用方行为。"""
        try:
            _cfg = self.config.config if hasattr(self.config, "config") else (
                self.config if isinstance(self.config, dict) else {})
            _dp = (_cfg.get("companion", {}) or {}).get("deep_persona", {})
            if not (isinstance(_dp, dict) and _dp.get("enabled") and _dp.get("life_line")):
                return {}
            from src.utils.persona_manager import PersonaManager
            from src.companion.deep_persona import (
                build_life_beat_opener, life_share_allowed, life_share_time_ok)
            from datetime import datetime as _dt
            pm = PersonaManager.get_instance()
            persona, _ = pm.get_persona_with_tier(str(contact_key or ""), "")
            if not isinstance(persona, dict) or not persona:
                return {}
            # 安静时段按**人设当地**时钟（温哥华凌晨不应按北京时间放行生活分享）
            try:
                from src.companion.persona_location import resolve_persona_now
                _now = resolve_persona_now(persona)
            except Exception:
                _now = _dt.now()
            # E5 时段闸：静默时段（默认深夜/清晨 0-8 点）不主动分享
            _qs = int(_dp.get("life_share_quiet_start_hour", 0) or 0)
            _qe = int(_dp.get("life_share_quiet_end_hour", 8) or 8)
            if not life_share_time_ok(_now, quiet_start_hour=_qs, quiet_end_hour=_qe):
                return {}
            # 实施55「聊过即退役」：该会话聊过的素材不再作 life_share 开场；
            # 全用完 → {}（升级链落到新闻/天气/问候，新闻为主的语义由此成立）。
            _skip = None
            try:
                from src.companion.life_beat_ledger import skip_fn_for
                _skip = skip_fn_for(str(contact_key or ""))
            except Exception:
                _skip = None
            # D2 反打扰：按频次/间隔闸门，避免"天天主动汇报生活"
            try:
                from src.companion.deep_persona_store import get_deep_persona_store
                _st = get_deep_persona_store()
                _cid = str(contact_key or "")
                if _st is not None and _cid:
                    # 「0=不限」：键缺失才取默认；显式 0 = 不限（旧 ``or 2`` 会把 0 改回 2）。
                    _mpw_raw = _dp.get("life_share_max_per_week", 2)
                    _gap_raw = _dp.get("life_share_min_gap_hours", 48)
                    _mpw = int(2 if _mpw_raw is None else _mpw_raw)
                    _gap = float(48 if _gap_raw is None else _gap_raw)
                    # outbound.unlimited_mode：周配额/间隔属业务频控，一键放开
                    try:
                        from src.ops.outbound_policy import is_unlimited as _ou
                        if _ou():
                            _mpw, _gap = 0, 0.0
                    except Exception:
                        pass
                    if not life_share_allowed(
                        _st.get_life_shares(_cid), _now,
                        max_per_week=_mpw, min_gap_hours=_gap,
                    ):
                        return {}
                    _op = build_life_beat_opener(
                        persona, _now, gate=gate, skip_fn=_skip)
                    if _op and record:
                        _st.record_life_share(_cid)
                    return _op
            except Exception:
                pass
            return build_life_beat_opener(persona, _now, gate=gate, skip_fn=_skip)
        except Exception:
            self.logger.debug("life_beat opener 解析失败（忽略）", exc_info=True)
            return {}

    def mark_life_share_sent(self, contact_key: str, beat: str = "") -> None:
        """生活分享开场**真发成功**后落账（P1：规划不扣配额、发出才扣）。绝不抛。

        ``beat``（实施55）＝真发出去的素材原文：一并记进「聊过即退役」会话
        账本（主动分享过＝确定聊过，不依赖提及判定）。空串=旧调用方零变化。
        """
        try:
            from src.companion.deep_persona_store import get_deep_persona_store
            _st = get_deep_persona_store()
            _cid = str(contact_key or "")
            if _st is not None and _cid:
                _st.record_life_share(_cid)
            if _cid and str(beat or "").strip():
                try:
                    from src.companion.life_beat_ledger import record_beat_used
                    record_beat_used(_cid, str(beat))
                except Exception:
                    pass
            try:
                from src.companion.deep_persona_stats import get_deep_persona_stats
                get_deep_persona_stats().incr("life_shares")
            except Exception:
                pass
        except Exception:
            self.logger.debug("life_share sent 落账失败（忽略）", exc_info=True)

    def build_ritual_opener(
        self,
        slot: str,
        *,
        memory_key: str = "",
        stage: str = "",
        intimacy: float = 0.0,
        last_emotion: str = "",
        last_emotion_intensity: float = -1.0,
        contact_key: str = "",
    ) -> Dict[str, Any]:
        """每日仪式问候 directive（晨安 / 晚安）——含情绪护栏 + 可选记忆钩子。

        与 ``build_proactive_opener`` 共用情绪护栏：severe 近期危机 → 不发欢快问候
        （``blocked``，交派发层视情升级 care）；低落（soft）→ 改克制陪伴口吻、不带记忆钩子；
        其余档可自然轻提一句 TA 在意的高置信记忆（一句带过、不追问）。
        """
        s = str(slot or "").strip().lower()
        if s not in ("morning", "night"):
            return {"mode": "", "directive": "", "fact": ""}
        gate = self._proactive_emotion_gate(
            memory_key, last_emotion, last_emotion_intensity)
        if gate == "block":
            # severe 危机：不道早晚安，带 blocked 信号交派发层升级 care（同 proactive_opener）
            return {"mode": "", "directive": "", "fact": "", "blocked": "crisis_severe"}
        fact = ""
        _interest_words: list = []
        if gate != "soft":
            try:
                store = getattr(self, "_episodic_store", None)
                key = str(memory_key or "").strip()
                if store and key and hasattr(store, "list_rows"):
                    from src.utils.proactive_topic import select_proactive_topic
                    facts = store.list_rows(prefix=key, limit=50) or []
                    # variety_key（2026-08-18 素材化）：旧调用不带 → argmax 恒取
                    # top-1，同一条记忆被早晚安天天复读（「轻轻提一句备考」x14）；
                    # 带上后在高分 Top-K 内按日轮换，都是真事实、天天不重样。
                    sel = select_proactive_topic(
                        facts, silent_hours=10 ** 6, min_silent_hours=0.0,
                        variety_key=str(contact_key or memory_key or ""))
                    fact = str(sel.get("fact") or "")
                    # 兴趣词（2026-08-18 轻话题个性化）：记忆事实的内容 token
                    # （CJK bigram/拉丁词，与反编造守卫同一取词口径）喂给话题
                    # 挑选器做「聊过球的人优先轮到球赛话题」。token 噪声（如
                    # 「喜欢」bigram）只影响**优先级排序**不影响准入——话题仍
                    # 全量过 smalltalk_topic 轻话题闸，最坏＝退回原轮换。
                    try:
                        from src.ai.memory_grounding import _content_tokens
                        _seen: set = set()
                        for _f in facts[:12]:
                            # 返回 (拉丁词/数字集, CJK bigram 集) 二元组
                            _lat, _cjk = _content_tokens(
                                str((_f or {}).get("content") or ""))
                            for _tok in list(_lat) + list(_cjk):
                                if _tok and _tok not in _seen:
                                    _seen.add(_tok)
                                    _interest_words.append(_tok)
                            if len(_interest_words) >= 24:
                                break
                        _interest_words = _interest_words[:24]
                    except Exception:
                        _interest_words = []
            except Exception:
                self.logger.debug("ritual memory hook skipped", exc_info=True)
                fact = ""
        if s == "morning":
            directive = "主动给TA道一句早安，温暖自然、像每天醒来都会惦记着TA的人；"
        else:
            directive = "主动给TA道一句晚安，温柔放松、像睡前会想起TA的人；"
        if gate == "soft":
            directive += "语气轻柔克制，别过分欢快，只是静静陪着、让TA知道有人在。"
        else:
            # 仪式素材化（2026-08-18）：早晚安占主动发送 96.7%，此前 directive 只有
            # 「道一句早安」+可选记忆——每天同一句式即「生硬」体感的大头。切入角
            # 轮换池给「今天为什么想起TA」一个具体由头（与 gentle_checkin 的
            # checkin_angle 同哲学）；低概率轻话题佐料（默认关）可顶替当日切入角。
            _angle_line = ""
            try:
                _small = self._ritual_smalltalk_line(
                    str(contact_key or memory_key or ""), s,
                    interests=_interest_words)
            except Exception:
                _small = ""
            if _small:
                directive += _small
            else:
                try:
                    from src.utils.proactive_topic import ritual_angle
                    _angle_line = ritual_angle(
                        s, str(contact_key or memory_key or ""))
                except Exception:
                    _angle_line = ""
                if _angle_line:
                    directive += f"今天的开场切入（参考方向，自然融入就好）：{_angle_line}。"
            if fact:
                directive += (
                    f"如果顺势，可以再轻轻带一句TA在意的「{fact}」"
                    "（一句带过、别追问、别罗列）。"
                )
            directive += "整条 1-2 句、口语化，不查岗、不连环发问。"
        # 命理灵签增强（companion.bazi）：仅晨安 + 情绪正常 + TA 给过生辰（说明吃这套）
        # 才附一句「顺手翻签」素材——没画像的用户不推命理内容，零骚扰。
        # 软失败（含无此绑定的轻量调用方）：任何异常都不阻断问候主流程。
        if s == "morning" and gate != "soft":
            try:
                card_line = self._ritual_daily_card_line(memory_key)
                if card_line:
                    directive += card_line
            except Exception:
                pass
        # 真实天气素材（companion.weather.greeting_inject，2026-08-16 聊真实世界 P1）：
        # 早晚安从「只有切入角方向」升级为「方向 + 人设城市当地真实值」。
        # 软失败（含无此绑定的轻量调用方）：任何异常不阻断问候主流程。
        if gate != "soft":
            try:
                wline = self._ritual_weather_line(contact_key, s)
                if wline:
                    directive += wline
            except Exception:
                pass
        if str(stage or "").strip().lower() in ("initial", "warming"):
            directive += "（关系还偏新：点到为止、别过分亲密。）"
        return {"mode": f"ritual_{s}", "directive": directive, "fact": fact,
                "context_facts": []}

    def _ritual_smalltalk_line(
        self, contact_key: str, slot: str, interests: Any = None,
    ) -> str:
        """早/晚安的「今日轻话题」佐料行；任何条件不满足 → ""（绝不阻断问候）。

        默认关（``daily_ritual.smalltalk_probability`` 缺省 0）：news_share 主动
        开场 14 天回复率 16.7% 被数据止损——这里刻意**不做新闻播报**，只把
        allowlist 轻话题（体育/文娱/生活方式，``smalltalk_topic`` 判定 + 按人
        轮换盐防 2026-08-04 全员同题群发复发）当「今天想到TA的由头」低概率
        混进切入角池。确定性抽签（crc32(user#档#日)）＝同用户同日恒定、可单测。
        素材只有一句标题 → 反编造钉子随行；非中文会话由行内指令让 LLM 忽略
        （P1 待办：把会话语言接进 opener 后改为硬闸）。

        ``interests``（2026-08-18 个性化）：客户记忆的内容词——命中的话题
        优先轮到（聊过球的人先看到球赛），news_share 泛发 16.7% 的教训正是
        「话题与人无关」；无兴趣词/不命中＝原轮换，行为零回退风险。
        """
        try:
            _cfg = self.config.config if hasattr(self.config, "config") else (
                self.config if isinstance(self.config, dict) else {})
            comp = _cfg.get("companion", {}) or {}
            dr = ((comp.get("proactive_topic") or {}).get("daily_ritual") or {})
            try:
                prob = float(dr.get("smalltalk_probability") or 0.0)
            except (TypeError, ValueError):
                prob = 0.0
            if prob <= 0:
                return ""
            if not ((comp.get("daily_topics") or {}).get("enabled")):
                return ""  # 话题包没开=无素材源
            import time as _t
            from zlib import crc32 as _crc
            day = _t.strftime("%Y%m%d")
            seed = f"{contact_key}#rst#{slot}#{day}".encode("utf-8", "ignore")
            if (_crc(seed) % 1000) / 1000.0 >= max(0.0, min(1.0, prob)):
                return ""
            from src.companion.daily_topics import (
                parse_topics_cfg,
                pick_topics_for,
                smalltalk_topic,
            )
            # 实施84 P2：仪式闲聊选题同样吃内容策略（不做赛事、社会/娱乐为主）
            _tc = parse_topics_cfg(_cfg)
            _tastes = [str(w) for w in (interests or []) if str(w).strip()]
            topics = pick_topics_for(
                _tastes or None, k=4, variety_key=f"rst:{contact_key}",
                cache_path=_tc.get("cache_path"),
                exclude_sports=bool(_tc.get("exclude_sports", True)),
                prefer_kinds=_tc.get("prefer_kinds")) or []
            light = [t for t in topics if smalltalk_topic(t)]
            if not light:
                return ""
            title = str(light[0].get("title") or "").strip()[:60]
            if not title:
                return ""
            return (
                f"今天的开场切入：你最近刷到一条「{title}」，顺嘴当闲聊提一嘴，"
                "就当是今天想到TA的由头——素材只有这一句标题，细节你不知道、"
                "绝不编造，被追问就大方说没细看；若对方平时不用中文聊天，"
                "这条忽略、换成分享你此刻正在做的一件小事。"
            )
        except Exception:
            self.logger.debug("ritual smalltalk line skipped", exc_info=True)
            return ""

    def _ritual_daily_card_line(self, memory_key: str) -> str:
        """晨安 ritual 的灵签附加行；任何条件不满足 → ""（绝不阻断问候主流程）。"""
        try:
            cfg = self._bazi_cfg()
            if not cfg.get("enabled", False) or not cfg.get("daily_card_in_ritual", True):
                return ""
            from src.companion.bazi_engine import bazi_available, compute_bazi
            if not bazi_available():
                return ""
            info = self.resolve_birth_info(memory_key)
            if info is None:
                return ""  # 没给过生辰的用户不推命理内容
            chart = compute_bazi(info)
            if not chart:
                return ""
            from src.companion.bazi_daily import daily_card, ritual_card_line
            card = daily_card(
                day_master_gan=(chart.get("day_master") or " ")[0],
                seed_key=str(memory_key or ""))
            line = ritual_card_line(card) if card else ""
            if line:
                from src.companion.bazi_stats import get_bazi_stats
                get_bazi_stats().record_daily_card("ritual")
            return line
        except Exception:
            self.logger.debug("ritual daily card skipped", exc_info=True)
            return ""

    def build_milestone_opener(
        self,
        *,
        event_type: str,
        event_label: str = "",
        days: int = 0,
        memory_key: str = "",
        stage: str = "",
        intimacy: float = 0.0,
        last_emotion: str = "",
        last_emotion_intensity: float = -1.0,
        contact_key: str = "",
    ) -> Dict[str, Any]:
        """纪念日/节日仪式 directive（认识 N 天 / 节日）——含情绪护栏 + 可选记忆钩子。

        与 ``build_ritual_opener`` 同一护栏：severe 近期危机 → ``blocked``（不发庆祝、
        交派发层升级 care）；低落（soft）→ 克制陪伴口吻、不带记忆钩子；其余可自然轻提
        一句 TA 在意的高置信记忆。节点文案的「具体场合」由 directive 承载，框定走
        build_proactive_prompt 的 milestone 分支（不套「久别重逢」）。
        """
        et = str(event_type or "").strip().lower()
        if et not in ("anniversary", "holiday", "birthday"):
            return {"mode": "", "directive": "", "fact": ""}
        gate = self._proactive_emotion_gate(
            memory_key, last_emotion, last_emotion_intensity)
        if gate == "block":
            return {"mode": "", "directive": "", "fact": "", "blocked": "crisis_severe"}
        fact = ""
        if gate != "soft":
            try:
                store = getattr(self, "_episodic_store", None)
                key = str(memory_key or "").strip()
                if store and key and hasattr(store, "list_rows"):
                    from src.utils.proactive_topic import select_proactive_topic
                    facts = store.list_rows(prefix=key, limit=50) or []
                    sel = select_proactive_topic(
                        facts, silent_hours=10 ** 6, min_silent_hours=0.0)
                    fact = str(sel.get("fact") or "")
            except Exception:
                self.logger.debug("milestone memory hook skipped", exc_info=True)
                fact = ""
        if et == "birthday":
            directive = (
                "今天是TA的生日！送上温暖真诚、独一无二的生日祝福，"
                "让TA感到被记得、被在乎；自然走心、别像贺卡套话；")
            mode = "milestone_birthday"
        elif et == "anniversary":
            n = max(0, int(days or 0))
            directive = (
                f"今天是你们认识的第{n}天，自然温暖地和TA说一句这个小纪念日的心情，"
                f"像一个真的记得这个日子的人；别太隆重、别煽情；")
            mode = "milestone_anniversary"
        else:
            label = str(event_label or "节日").strip() or "节日"
            directive = (
                f"今天是「{label}」，给TA送上应景而真诚的节日祝福，温暖自然、不要套话；")
            mode = "milestone_holiday"
        if gate == "soft":
            directive += "语气轻柔克制，照顾TA最近的低落，只是静静陪着、别强求TA高兴。"
        elif fact:
            directive += f"可以很自然地轻轻提一句TA在意的「{fact}」（一句带过、别追问）。"
        else:
            directive += "一句心意即可，别强行延展话题。"
        if str(stage or "").strip().lower() in ("initial", "warming"):
            directive += "（关系还偏新：点到为止、别过分亲密。）"
        return {"mode": mode, "directive": directive, "fact": fact,
                "context_facts": []}

    def resolve_birthday(self, memory_key: str):
        """从某用户 episodic 记忆里扫出生日 (月, 日)；扫不到 → None（Stage Q）。

        保守：优先 ``user_stated``（用户亲口说的），其次全部；命中第一条可解析的即返回。
        生日的「日期」抽取在 ``src.utils.birthday.extract_birthday``（要求带生日关键词）。
        """
        store = getattr(self, "_episodic_store", None)
        key = str(memory_key or "").strip()
        if not store or not key or not hasattr(store, "list_rows"):
            return None
        try:
            from src.utils.birthday import extract_birthday
            rows = store.list_rows(prefix=key, limit=80, source="user_stated") or []
            if not rows:
                rows = store.list_rows(prefix=key, limit=80) or []
            for r in rows:
                bd = extract_birthday((r or {}).get("content") or "")
                if bd is not None:
                    return bd
        except Exception:
            self.logger.debug("resolve_birthday failed", exc_info=True)
        return None

    # ── 命理技能（FateX 幻缘产品；配置 fatex.* 新命名空间 + companion.bazi 兼容）──

    def _inject_goal_context(
        self, user_context: Dict[str, Any], *,
        platform: str = "", chat_key: str = "", account_id: str = "",
        conversation_id: str = "", chain: str = "reply",
        inbound_text: str = "",
    ) -> None:
        """营销目标（companion.goals）→ 注入 ``_goal_block``。

        单一入口 ``goals.service.build_block_for_chat``（settle-on-read + 当日拍）
        ——右栏卡/看板/本注入读同一份结算口径。未启用/无目标/hold/observe 档 →
        清残留不注入。任何异常零阻断主链。
        ``inbound_text``＝对方本条消息（getting_to_know 类模板顺手采画像槽位，
        高置信正则、只填空槽）。
        """
        user_context.pop("_goal_block", None)
        user_context.pop("_goal_cta", None)
        user_context.pop("_goal_inject_meta", None)
        user_context.pop("_camp_block", None)
        # #147 第一层：自家阵营在推活动硬约束块——**独立于目标系统**（有无漏斗目标、
        # goals 开没开都注），空登记零开销。与出站贬损守卫同源同文件（site_catalog）。
        try:
            from src.companion.goals.service import build_camp_block_for_chat
            _hist_u: List[str] = []
            _h = user_context.get("_conversation_history")
            if isinstance(_h, list):
                _hist_u = [str((m or {}).get("content") or "") for m in _h[-8:]
                           if isinstance(m, dict)
                           and str((m or {}).get("role") or "") == "user"]
            _camp = build_camp_block_for_chat(
                getattr(self.config, "config", None) or {},
                getattr(self.config, "config_path", None),
                lang=str(user_context.get("reply_lang")
                         or user_context.get("target_lang") or "zh"),
                inbound_text=str(inbound_text or user_context.get("last_message") or ""),
                history_texts=_hist_u,
            )
            if _camp:
                user_context["_camp_block"] = _camp
        except Exception:
            self.logger.debug("camp block inject skipped", exc_info=True)
        try:
            from src.companion.goals.service import build_block_for_chat
            # P26：inbox store 经协议桥单例补齐（best-effort）——坐席手动
            # 「情绪低落」标注的让路判据（manual_negative_active）需要读
            # conv_meta，此前注入口不传 store，该 hold 在所有链路都是死路。
            _ibx = None
            try:
                from src.integrations.protocol_bridge import (
                    get_inbox_store as _goal_gis,
                )
                _ibx = _goal_gis()
            except Exception:
                _ibx = None
            block = build_block_for_chat(
                self.config,
                platform=str(platform or "") or "telegram",
                chat_key=str(chat_key or ""),
                account_id=str(account_id or ""),
                conversation_id=str(conversation_id or ""),
                user_context=user_context,
                chain=chain,
                inbound_text=str(inbound_text or ""),
                inbox_store=_ibx,
                ai_client=getattr(self, "ai_client", None),
            )
            if block:
                user_context["_goal_block"] = block
        except Exception:
            self.logger.debug("goal inject skipped", exc_info=True)

    def _apply_goal_link_guard(
        self, reply: str, user_context: Dict[str, Any], *,
        log_prefix: str = "",
    ) -> str:
        """出站守卫家族入口：优惠承诺（P14）+ 事实声明（P15）+ 链接纪律（P4）。

        CTA 分级只是 prompt 叮嘱——soft/hold 日 LLM 可能从历史上下文复读出
        官网下单链，纪律形同虚设。这里按注入侧暂存的当日档位（``_goal_cta``，
        读后即焚）确定性剥离越纪律的**本域**链接：order 档全放行、cs/roi 档
        剥下单深链、空档剥全部本域链。别家域名一概不碰。开关
        ``companion.goals.link_guard.enabled``（默认开）。

        **无目标会话也要守事实**（2026-07-28 预检实锤的覆盖漏洞）：``_goal_cta``
        只有建了漏斗目标的会话才有，而 ``auto_create`` 有日预算上限——预算打满后
        新会话全部拿不到暂存，于是「报价与目录不符 / 试用时长编造 / gated 线泄漏 /
        未授权折扣」这些**与目标无关的商业事实**守卫一起静默失效（6 场预检里唯一
        有目标的那场 0 缺陷，其余 5 场七折·85折·14天试用·免费换脸全出街）。
        故无暂存时改用目录事实兜底（``catalog_guard_facts``）跑事实/优惠两轴，
        仅跳过需要档位的 ``link_guard``。
        """
        info = user_context.pop("_goal_cta", None)
        if not reply:
            return reply
        _cfg_root_g = getattr(self.config, "config", None) or {}
        _has_goal_ctx = isinstance(info, dict)
        if not _has_goal_ctx:
            try:
                from src.companion.goals.service import catalog_guard_facts
                info = catalog_guard_facts(
                    _cfg_root_g, getattr(self.config, "config_path", None))
            except Exception:
                self.logger.debug("%s[goal-guard] 目录事实兜底不可用，跳过",
                                  log_prefix, exc_info=True)
                return reply
            if not any(info.get(k) for k in
                       ("offer_texts", "offer_free_days", "catalog_prices",
                        "camp_terms")):
                return reply   # 目录空/未配 → 没有可比对的事实，零影响
        # #147 贬损自家阵营推广守卫：跑在事实/优惠两轴**之前**——它剥的是整句
        # 立场（「充值奖励真蠢」），先剥掉再让报价/试用轴看剩下的小句；有无目标
        # 都跑（词表与目录事实同源）。跨轮语境取客户本条 + 近轮客户消息。
        reply = _guard_camp_disparagement(
            reply, info, cfg_root=_cfg_root_g,
            logger=self.logger, log_prefix=log_prefix,
            context_text=_camp_context_text(user_context))
        if not reply:
            return reply
        # 措辞轴（N折/券码/赠送）在无目标会话需先确认这段在谈**我方**商业事项：
        # 它只问「有没有 N 折」不问「谁在打折」，扩到全会话后会把陪聊的
        # 「楼下奶茶店今天打八折」剥成「我下班去买一杯」（实测 3/12 误报）。
        # 事实轴无此问题（product_context=False 即要求产品词同句），照常跑。
        _run_offer_axis = True
        if not _has_goal_ctx:
            try:
                from src.companion.goals.claim_guard import mentions_our_commerce
                _run_offer_axis = mentions_our_commerce(reply)
            except Exception:
                _run_offer_axis = False   # 判不出就不剥（宁漏勿误伤陪聊）
        if _run_offer_axis:
            reply = _guard_offer_claims(
                reply, info, cfg_root=_cfg_root_g,
                logger=self.logger, log_prefix=log_prefix)
        reply = _guard_outbound_claims(
            reply, info, cfg_root=_cfg_root_g,
            logger=self.logger, log_prefix=log_prefix,
            product_context=_has_goal_ctx)
        if not _has_goal_ctx:
            return reply   # link_guard 需要当日 CTA 档位，无目标无档位可言
        try:
            _cfg = getattr(self.config, "config", None) or {}
            _lg = (((_cfg.get("companion") or {}).get("goals") or {})
                   .get("link_guard") or {})
            if not bool(_lg.get("enabled", True)):
                return reply
            from src.companion.goals.link_guard import sanitize_goal_links
            out, n = sanitize_goal_links(
                reply,
                cta=str(info.get("cta") or ""),
                base_url=str(info.get("base_url") or ""),
                order_path=str(info.get("order_path") or "/order"),
            )
            if n:
                self.logger.info(
                    "%s[goal-link-guard] 越纪律链接已剥离 %d 条（当日档=%s）",
                    log_prefix, n, info.get("cta") or "none")
                try:
                    from src.companion.goals.stats import get_goal_stats
                    get_goal_stats().record_link_stripped(n)
                except Exception:
                    pass
            return out
        except Exception:
            self.logger.debug("%s[goal-link-guard] 守卫异常，保留原回复",
                              log_prefix, exc_info=True)
            return reply

    def _bazi_cfg(self) -> Dict[str, Any]:
        """FateX 生效配置（单一咽喉：本方法即全部 skill 链的配置出口）。

        产品分离（2026-07-25）后经 ``fatex_cfg`` 合并视图取值——顶层 ``fatex.*``
        优先、旧 ``companion.bazi.*`` 兜底，两处任一开着都生效（存量 overlay 零迁移）。
        """
        try:
            from src.fatex.config import fatex_cfg
            cfg = self.config.config if hasattr(self.config, "config") else {}
            return fatex_cfg(cfg)
        except Exception:
            return {}

    def resolve_birth_info(self, memory_key: str):
        """从 episodic 记忆扫完整生辰（BirthInfo）；扫不到 → None。

        优先 ``user_stated``；行序 created_at DESC → 用户更正/补时辰的新事实自然胜出。
        """
        store = getattr(self, "_episodic_store", None)
        key = str(memory_key or "").strip()
        if not store or not key or not hasattr(store, "list_rows"):
            return None
        try:
            from src.companion.bazi_profile import extract_birth_info
            rows = store.list_rows(prefix=key, limit=80, source="user_stated") or []
            if not rows:
                rows = store.list_rows(prefix=key, limit=80) or []
            for r in rows:
                info = extract_birth_info((r or {}).get("content") or "")
                if info is not None:
                    return info
        except Exception:
            self.logger.debug("resolve_birth_info failed", exc_info=True)
        return None

    def resolve_birth_info_scoped(
        self, platform: str, account_id: str, chat_key: str,
    ):
        """FateX 产品级生辰解析（P0-2 根治）：结构化库优先，记忆扫描兜底。

        读序：① FateX 独立库（(platform, account, chat) 结构化行，账号天然隔离）
        → ② 账号作用域记忆键前缀扫描（新写入的 episodic 事实）
        → ③ 存量裸键前缀扫描（键格式升级前的老数据，Phase 2 迁移后自然清空）。
        任何一层异常软失败进下一层，绝不阻断调用链。
        """
        try:
            from src.fatex.store import get_fatex_store
            _fx = get_fatex_store()
            if _fx is not None:
                info = _fx.get_birth(platform, account_id, chat_key)
                if info is not None:
                    return info
        except Exception:
            self.logger.debug("[fatex] get_birth failed", exc_info=True)
        try:
            key = self._episodic_storage_key(
                str(chat_key), "", platform, account_id=account_id)
            info = self.resolve_birth_info(key) if key else None
            if info is not None:
                return info
            legacy = self._episodic_storage_key(str(chat_key), "", platform)
            if legacy and legacy != key:
                return self.resolve_birth_info(legacy)
        except Exception:
            self.logger.debug("resolve_birth_info_scoped failed", exc_info=True)
        return None

    def _persona_bio_cfg(self) -> Dict[str, Any]:
        """人设长传记检索配置（``personas.bio_retrieval``，默认关）。"""
        try:
            cfg = self.config.config if hasattr(self.config, "config") else {}
            if not isinstance(cfg, dict):
                return {}
            return dict((cfg.get("personas") or {}).get("bio_retrieval") or {})
        except Exception:
            return {}

    def _inject_persona_bio_context(
        self, user_context: Dict[str, Any], text: str,
    ) -> None:
        """人设长传记检索 → 往 user_context 注入 ``_persona_bio_block``。

        客户追问人设长尾细节（大学城市/前夫名字……不在 16 条核心记忆里）时，
        从原始人设文档分块库按关键词检索，命中才注入（预算内截断），不命中
        零开销。每轮重算、非命中轮清残留（与 ``_bazi_block`` 同模式）。
        persona_id 与 ``_get_persona_name_for_context`` 同源
        （``user_context.account_persona_id``）；拿不到就静默跳过。
        """
        user_context.pop("_persona_bio_block", None)
        try:
            if not self._persona_bio_cfg().get("enabled", False):
                return
            persona_id = str(
                (user_context or {}).get("account_persona_id") or "").strip()
            if not persona_id or not str(text or "").strip():
                return
            from src.companion.persona_bio_store import build_bio_block
            block = build_bio_block(persona_id, text)
            if block:
                user_context["_persona_bio_block"] = block
        except Exception:
            self.logger.debug("persona bio retrieval skipped", exc_info=True)

    def _inject_peer_locale(
        self, user_context: Dict[str, Any],
        user_id_str: str, chat_id: Any, platform: str = "",
    ) -> None:
        """P2 用户侧在地化注入：往 user_context 写 ``_peer_clock_line``（对方当地时间）
        与 ``_peer_holiday_note``（对方那边今天的节日）两个内部事实块。

        为什么与调度侧分两条路（重要）：调度侧（主动触达择时）敢吃行为统计推断，因为
        `user_clock.in_quiet_hours` 对 ``narrow`` 档只做「收窄」——推断错了最多少发一条。
        prompt 侧没有这层安全网：写进去的时间会被 LLM 当事实复述给客户，猜错就是当面
        说错话。故这里走 `resolve_peer_locale`（**只吃显式信号**：自述城市 / WhatsApp
        号码国码 / 语种默认国家），且 advisory 档的 `user_time_line` 本身返回空串。

        人设侧节日在 `ai_client._build_context_prompt` 内算（那里已有人设与人设本地钟，
        且是纯函数无 IO）——两侧刻意分开，避免本方法为了拿人设去依赖 selfie 那套解析。
        默认全关：`companion.user_clock.enabled` / `companion.locale_holidays.enabled`。
        """
        user_context.pop("_peer_clock_line", None)
        user_context.pop("_peer_holiday_note", None)
        user_context.pop("_peer_local_now", None)
        user_context.pop("_peer_country", None)
        try:
            _cfg = self.config.config if hasattr(self.config, "config") else (
                self.config if isinstance(self.config, dict) else {})
            _comp = (_cfg.get("companion") or {}) if isinstance(_cfg, dict) else {}
            uc_cfg = _comp.get("user_clock") or {}
            hol_cfg = _comp.get("locale_holidays") or {}
            want_clock = bool(uc_cfg.get("enabled")) and bool(
                uc_cfg.get("inject_chat", True))
            want_holiday = bool(hol_cfg.get("enabled")) and bool(
                hol_cfg.get("inject_chat", True))
            if not (want_clock or want_holiday):
                return

            lang = str(
                user_context.get("reply_lang")
                or user_context.get("user_lang_pref")
                or "").strip()
            clock = None
            if bool(uc_cfg.get("enabled")):
                from src.companion.user_clock_resolver import resolve_peer_locale
                key = self._episodic_storage_key(
                    user_id_str, chat_id, platform, user_context=user_context)
                clock = resolve_peer_locale(
                    key or str(user_id_str or ""),
                    episodic_store=self._episodic_store,
                    memory_key=key,
                    phone=user_context.get("peer_phone") or chat_id,
                    platform=platform or str(user_context.get("platform") or ""),
                    language=lang,
                    cfg=uc_cfg,
                )
            # 实施84 P2：用户国别顺手入 context（同一次解析零额外成本）——
            # daily_topics 的「新闻以用户所在国家为准」消费它；显式信号
            # （自述城市/号码国码/语种默认国）解析不出＝不落键。
            if clock is not None:
                try:
                    _cc = str(getattr(clock, "country", "") or "").strip().upper()
                    if len(_cc) == 2:
                        user_context["_peer_country"] = _cc
                except Exception:
                    pass
            if want_clock and clock is not None:
                from src.companion.user_clock import user_now, user_time_line
                _line = user_time_line(clock, "zh")
                if _line:
                    user_context["_peer_clock_line"] = _line
                    # 客户当地 naive 时间：供出站守卫（星期合法集第二框架）与
                    # 时差桥注入消费。刻意与 user_time_line 同一信任门槛
                    # （advisory 弱推断返回空行 → 这里也不落键），弱信号不该
                    # 扩大守卫合法集、更不该触发时差桥。
                    try:
                        _pnow = user_now(clock)
                        if _pnow is not None:
                            user_context["_peer_local_now"] = _pnow
                    except Exception:
                        pass
            if want_holiday:
                from src.companion.locale_holidays import (
                    country_for_language, holiday_fact_line, holidays_on,
                    upcoming_holidays,
                )
                country = str(getattr(clock, "country", "") or "").strip()
                if not country:
                    country = country_for_language(lang)
                if country:
                    from datetime import datetime as _dt_now
                    from src.companion.user_clock import user_now
                    _day = (user_now(clock) if clock is not None
                            else _dt_now.now()).date()
                    _note = holiday_fact_line(
                        holidays_on(_day, country), "zh", side="user")
                    if not _note:
                        # 今天没节日 → 看「快到了」（真人会提前聊「你们下周不是放假吗」；
                        # 只取最近一个、且只在 lookahead 窗内，避免变成节日播报机）。
                        try:
                            _ahead = int(hol_cfg.get("lookahead_days", 2) or 0)
                        except Exception:
                            _ahead = 2
                        if _ahead > 0:
                            _up = upcoming_holidays(
                                _day, country, within_days=_ahead)
                            _soon = [(h, d) for h, d in (_up or []) if d > 0]
                            if _soon:
                                _h, _d = _soon[0]
                                _note = (
                                    f"【对方那边的节日（内部事实）】再过 {_d} 天是"
                                    f"{_h.name_zh}。只在话题自然相关时提一句"
                                    "（比如问 TA 有没有安排），别提前群发祝福。")
                    if _note:
                        user_context["_peer_holiday_note"] = _note
        except Exception:
            self.logger.debug("peer locale inject skipped", exc_info=True)

    def _inject_reply_freshness(
        self,
        user_context: Dict[str, Any],
        text: str,
        *,
        recent_outbound: Optional[List[str]] = None,
        convo_key: str = "",
    ) -> None:
        """回复新鲜感两件套（2026-08-02）：出站口头禅账本 + 今日话题包。

        ① ``ai.reply_variety``（默认关）：统计最近出站回复的超限口头禅
           （哈哈家族/句尾语气/重复开头/场景词）→ ``_variety_hint``；
        ② ``companion.daily_topics``（默认关）：RSS 今日话题缓存（后台
           daemon 刷新，绝不阻塞）→ 冷场/被问起时 ``_daily_topics_hint``。
        两键都由 ``ai_client._build_context_prompt`` 消费（有键即消费，
        与 ``_bazi_block`` 同模式；本方法入口清残留同 bazi 口径）。
        出站文本源：B 线调用方传 inbox 权威历史的 assistant 侧（截断前）；
        A 线缺省从 ``_conversation_history`` + ``last_reply`` 就地取。
        全程 best-effort：任何异常静默跳过，绝不阻塞出话。
        """
        user_context.pop("_variety_hint", None)
        user_context.pop("_daily_topics_hint", None)
        user_context.pop("_temper_hint", None)
        user_context.pop("_temper_out_guard", None)
        try:
            cfg = self.config.config if hasattr(self.config, "config") else {}
            if not isinstance(cfg, dict):
                return
        except Exception:
            return
        _lang = "zh" if str(
            user_context.get("reply_lang") or "zh"
        ).lower().startswith("zh") else "en"
        # 人设词一次提取两处共用（tastes.likes + selfie_scenes 的中文名词；
        # 取不到就空，绝不为此新增重查询）。人设 dict 一并留存（实施55 地区
        # 分源要按人设 id/居住地国家挑 feeds）。
        persona_words: List[str] = []
        _persona_obj = None
        try:
            _pid = str(user_context.get("account_persona_id") or "").strip()
            if _pid:
                from src.utils.persona_manager import PersonaManager
                from src.ai.reply_variety import extract_persona_words
                _persona_obj = PersonaManager.get_instance().get_persona_by_id(_pid)
                persona_words = extract_persona_words(_persona_obj)
        except Exception:
            persona_words = []
            _persona_obj = None
        # ① 口头禅账本 → 多样性硬约束
        try:
            from src.ai.reply_variety import (
                build_variety_hint,
                collect_overused,
                parse_variety_cfg,
            )
            vcfg = parse_variety_cfg(cfg)
            if vcfg["enabled"]:
                outs = recent_outbound
                if outs is None:
                    _hist_v = user_context.get("_conversation_history") or []
                    outs = [
                        str(m.get("content") or "") for m in _hist_v
                        if isinstance(m, dict) and m.get("role") == "assistant"
                    ]
                    _lr = str(user_context.get("last_reply") or "").strip()
                    if _lr and (not outs or outs[-1] != _lr):
                        outs.append(_lr)
                outs = [str(t) for t in (outs or []) if str(t or "").strip()]
                outs = outs[-vcfg["window"]:]
                if outs:
                    overused = collect_overused(
                        outs,
                        scene_words=persona_words,
                        laugh_limit=vcfg["laugh_limit"],
                        tail_limit=vcfg["tail_limit"],
                        head_limit=vcfg["head_limit"],
                        scene_limit=vcfg["scene_limit"],
                        keyword_limit=vcfg["keyword_limit"],
                    )
                    if overused:
                        _vh = build_variety_hint(
                            overused, lang=_lang,
                            max_items=vcfg["max_items"])
                        if _vh:
                            user_context["_variety_hint"] = _vh
        except Exception:
            self.logger.debug("reply_variety 注入跳过", exc_info=True)
        # ② 今日话题包（冷场素材）
        try:
            from src.companion.daily_topics import (
                build_no_topics_hint,
                build_topics_hint,
                is_news_question,
                last_offered,
                note_offered,
                parse_topics_cfg,
                pick_topics_for,
                refresh_if_stale,
                should_offer_topics,
            )
            tcfg = parse_topics_cfg(cfg)
            if tcfg["enabled"]:
                # 分源优先级（实施84 P2 > 实施55）：**用户**所在国家（显式信号：
                # 自述城市/号码国码，经 _inject_peer_locale 落 _peer_country）
                # → 人设居住地 → 全局池。「用户在哪」优先于「人设在哪」——
                # 老板拍板「新闻以用户所在城市和国家为准」。
                _rcfg = tcfg
                try:
                    from src.companion.daily_topics import (
                        cfg_for_region,
                        cfg_for_user_region,
                        region_key_for,
                    )
                    _ucc = str(user_context.get("_peer_country") or "").strip()
                    if _ucc:
                        _ucfg = cfg_for_user_region(
                            tcfg, _ucc,
                            lang=str(user_context.get("reply_lang") or "zh"))
                        if _ucfg is not None:
                            _rcfg = _ucfg
                    if _rcfg is tcfg:
                        _rk = region_key_for(
                            _persona_obj, tcfg.get("region_feeds"))
                        if _rk:
                            _rcfg = cfg_for_region(tcfg, _rk)
                except Exception:
                    _rcfg = tcfg
                refresh_if_stale(_rcfg)  # 后台 daemon 线程，绝不阻塞本轮
                _key = str(
                    convo_key
                    or user_context.get("conversation_id")
                    or user_context.get("user_id")
                    or "") or "_"
                # 实施84 P2 兴趣路由：**用户**聊天里的兴趣词（episodic 记忆文本
                # 的内容 token，与 ritual 兴趣词同一取词口径）为主词、人设口味
                # 为次词——「根据用户聊天中喜欢的事情做新话题」。提取失败回落
                # 纯人设词（旧行为）。
                _user_words: List[str] = []
                try:
                    _epi_txt = str(
                        user_context.get("_episodic_memory_text") or "")
                    if _epi_txt.strip():
                        from src.ai.memory_grounding import _content_tokens
                        _lat, _cjk = _content_tokens(_epi_txt)
                        _user_words = (list(_lat) + list(_cjk))[:24]
                except Exception:
                    _user_words = []
                _pick_kw = {
                    "k": tcfg["pick_k"],
                    "variety_key": _key if _key != "_" else "",
                    "exclude_sports": bool(tcfg.get("exclude_sports", True)),
                    "prefer_kinds": tcfg.get("prefer_kinds"),
                }
                if _user_words:
                    _pick_kw["secondary_tastes"] = persona_words
                # 直球新闻问句 → 强指令（弱指令实测被 LLM 忽略答「没什么新闻」）
                _news_ask = is_news_question(text)
                if should_offer_topics(
                        text, last_offered(_key),
                        cooldown_hours=tcfg["cooldown_hours"],
                        offer_percent=tcfg.get("offer_percent", 15)):
                    # variety_key=会话键：素材轮换按会话分散（同会话当日恒定），
                    # 防「所有人同一天拿到同一批头条」的机器人味（news_share 同修）
                    _topics = pick_topics_for(
                        _user_words or persona_words,
                        cache_path=_rcfg["cache_path"], **_pick_kw)
                    # 区域缓存冷启动（刚配好还没刷出来）→ 回落全局池，
                    # 宁可聊全局新闻也不空手（区域缓存刷出后自动切回）
                    if not _topics and _rcfg is not tcfg:
                        _topics = pick_topics_for(
                            _user_words or persona_words,
                            cache_path=tcfg["cache_path"], **_pick_kw)
                    def _news_metric(_name: str) -> None:
                        try:
                            from src.monitoring.metrics_store import (
                                get_metrics_store,
                            )
                            get_metrics_store().record_inbox_draft_event(_name)
                        except Exception:
                            pass

                    if _topics:
                        _th = build_topics_hint(
                            _topics, lang=_lang, direct_ask=_news_ask)
                        if _th:
                            user_context["_daily_topics_hint"] = _th
                            note_offered(_key)
                            if _news_ask:
                                _news_metric("news_direct_ask")
                    elif _news_ask:
                        # 直球问新闻但缓存无货（冷启动/feeds 全败）→ 诚实兜底，
                        # 防 LLM 徒手编新闻或敷衍「没什么新闻」两个方向都穿帮。
                        user_context["_daily_topics_hint"] = (
                            build_no_topics_hint(_lang))
                        _news_metric("news_ask_no_stock")
        except Exception:
            self.logger.debug("daily_topics 注入跳过", exc_info=True)
        # ③ 被骂回应（人设脾气分级，2026-08-12）：客服腔安抚是陪伴场景穿帮点。
        # 粘性窗（同日补）：词表精确命中开窗，窗口内后续带敌意的变体骂法
        # （逐词打地鼠永远有漏网）继续保持怼的姿态；对方收手立即清窗。
        # 治理层（同日 P0）：companion.temper（总开关/force 全员强制/default
        # 兜底/脏字闸/粘性窗时长），生效档位单一判定链在 resolve_temper_level；
        # enabled=false ＝ kill switch，整块不跑（回 global_rules 旧行为）。
        # P2（2026-08-13）：连怼熔断（_insult_streak 超 max_rounds → 冷处理
        # 收场，真人不无限对轰）+ 激将接梗（「你太没脾气了」不是骂战，拽回去
        # 而非客服腔）+ 平台封顶（platform_caps 风险护栏，压过 force）。
        # 战线贯彻（2026-08-22，实施54）：① streak 传入 build_temper_hint
        # （≥2 轮加码递进+撤退话术负面清单——模型第 2 轮起顺着「我睡觉去了」
        # 历史惯性自行降级）；② 命中辱骂即 pop _emotional_context_block（骂词
        # 不在情感词典、「呀」记 playful → 实录 03:48 被判 playful 0.97，与
        # 回怼指令同 prompt 打架）；③ record_fight_turn 登记骂战态（语音链读
        # 它决定「骂回去的话不许用 happy 语调念」，见 persona_voice）。
        # P1 消气曲线（同日，老板实录「开玩笑的啦」秒停火＝机器人破绽）：
        # 求和不再瞬间清零——骂战收手先进「记仇期」（级别∝骂战轮数、真道歉
        # 打折、敷衍开脱会被点破），逐轮消气 + TTL 时间冲淡；记仇期再犯＝
        # 重燃且 streak 续算（假道歉后再骂更快熔断）；危机信号即刻散仇让位
        # （安全 > 脾气）。grudge_max_level=0 ＝回「立刻停火」旧行为。
        try:
            from src.companion.temper import (
                apply_platform_cap,
                build_feud_break_hint,
                build_grudge_hint,
                build_taunt_hint,
                build_temper_hint,
                clear_fight_turn,
                decay_grudge,
                detect_insult,
                detect_taunt,
                initial_grudge,
                is_de_escalation,
                is_flippant_retraction,
                is_sincere_apology,
                looks_hostile,
                parse_temper_cfg,
                record_deescalation,
                record_emo_block_suppressed,
                record_escalated_hint,
                record_feud_break,
                record_fight_turn,
                record_grudge_closeout,
                record_grudge_reignite,
                record_grudge_set,
                record_grudge_turn,
                record_hint,
                record_insult,
                record_platform_capped,
                record_sticky_hit,
                record_suppressed_off,
                record_taunt,
                resolve_temper_level,
            )
            _tcfg = parse_temper_cfg(cfg)
            if _tcfg["enabled"]:
                _now_t = time.time()
                _via_sticky = False
                _taunt = False
                _deesc = False
                _grudge_plain = False
                # 记仇窗 TTL：时间冲淡（连轮数残留一并散掉——隔了很久的旧账
                # 不该让新一句轻微冒犯直接撞熔断）。
                _grudge_now = int(user_context.get("_grudge_level") or 0)
                if _grudge_now > 0:
                    _g_ts = float(user_context.get("_grudge_ts") or 0)
                    _g_ttl = float(_tcfg.get("grudge_ttl_sec") or 5400.0)
                    if not _g_ts or (_now_t - _g_ts) > _g_ttl:
                        _grudge_now = 0
                        user_context.pop("_grudge_level", None)
                        user_context.pop("_grudge_ts", None)
                        user_context.pop("_insult_streak", None)
                _hit = detect_insult(text)
                if _hit:
                    record_insult()
                elif is_de_escalation(text):
                    _deesc = True
                else:
                    _last_t = float(user_context.get("_insult_ts") or 0)
                    if (_last_t
                            and (_now_t - _last_t)
                            <= _tcfg["sticky_window_sec"]
                            and looks_hostile(text)):
                        _hit = True
                        _via_sticky = True
                        record_sticky_hit()
                    elif _grudge_now > 0 and looks_hostile(text):
                        # 记仇期再犯＝重燃骂战：假求和后再骂，streak 续算
                        # （更快撞熔断——真人对二进宫更没耐心）。
                        _hit = True
                        _via_sticky = True
                        record_grudge_reignite()
                        self.logger.info(
                            "[temper] 记仇期再犯重燃骂战 grudge=%d",
                            _grudge_now)
                    elif _grudge_now > 0:
                        # 记仇期的普通轮（没道歉也没再骂）：端着回 + 自然消气
                        _grudge_plain = True
                    elif _tcfg["taunt_response"] and detect_taunt(text):
                        _taunt = True

                def _resolve_persona_level():
                    """人设查找 + 判定链 + 平台封顶（hit/taunt 两分支共用）。"""
                    _persona = None
                    _pid = ""
                    try:
                        _pid = str(
                            user_context.get("account_persona_id")
                            or "").strip()
                        if _pid:
                            from src.utils.persona_manager import (
                                PersonaManager,
                            )
                            _persona = PersonaManager.get_instance(
                            ).get_persona_by_id(_pid)
                    except Exception:
                        _persona = None
                    _lv = resolve_temper_level(_tcfg, _persona)
                    _cap = apply_platform_cap(
                        _lv["level"],
                        str(user_context.get("platform") or ""),
                        _tcfg["platform_caps"])
                    if _cap["capped"]:
                        record_platform_capped()
                    return _pid, _lv, _cap["level"], _cap["capped"]

                def _grudge_all_clear() -> None:
                    """散仇（含轮数残留）——off 档/危机让位/翻篇/旧行为共用。"""
                    user_context.pop("_grudge_level", None)
                    user_context.pop("_grudge_ts", None)
                    user_context.pop("_insult_streak", None)
                    clear_fight_turn(convo_key)

                def _grudge_crisis(txt: str) -> bool:
                    """记仇期危机让位：真实痛苦/求助 → 立刻放下脾气。"""
                    try:
                        from src.utils.wellbeing_guard import detect_crisis
                        return str(
                            detect_crisis(txt).get("level") or "none",
                        ) != "none"
                    except Exception:
                        return False

                if _hit:
                    # 骂战态压过记仇态（重燃/新开骂都回怼的姿态）
                    user_context.pop("_grudge_level", None)
                    user_context.pop("_grudge_ts", None)
                    # 窗口外的新精确命中＝新一场骂战，轮数从头计
                    _prev_ts = float(user_context.get("_insult_ts") or 0)
                    if (_prev_ts and not _via_sticky
                            and (_now_t - _prev_ts)
                            > _tcfg["sticky_window_sec"]):
                        user_context.pop("_insult_streak", None)
                    user_context["_insult_ts"] = _now_t
                    _streak = int(
                        user_context.get("_insult_streak") or 0) + 1
                    user_context["_insult_streak"] = _streak
                    _pid_t, _lv, _eff_level, _pcapped = (
                        _resolve_persona_level())
                    if _eff_level == "off":
                        record_suppressed_off()
                        self.logger.info(
                            "[temper] 命中辱骂但档位 off 不注入 persona=%s",
                            _pid_t or "-")
                    elif (_tcfg["max_rounds"] > 0
                            and _streak > _tcfg["max_rounds"]):
                        _th2 = build_feud_break_hint(_lang)
                        if _th2:
                            user_context["_temper_hint"] = _th2
                            record_feud_break()
                            if user_context.pop(
                                    "_emotional_context_block", None):
                                record_emo_block_suppressed()
                            record_fight_turn(convo_key, kind="feud")
                            self.logger.info(
                                "[temper] 连怼熔断（第 %d 轮 > %d）冷处理 "
                                "persona=%s", _streak,
                                _tcfg["max_rounds"], _pid_t or "-")
                    else:
                        _th2 = build_temper_hint(
                            _eff_level, _lang, streak=_streak)
                        if _th2:
                            user_context["_temper_hint"] = _th2
                            # P1-b 出站否决标记（读后即焚）：本轮回复过
                            # 认输句剥离；熔断轮退场语义合法不标。
                            user_context["_temper_out_guard"] = "fight"
                            if _streak >= 2:
                                record_escalated_hint()
                            if user_context.pop(
                                    "_emotional_context_block", None):
                                record_emo_block_suppressed()
                            record_fight_turn(convo_key, kind="insult")
                            record_hint(
                                _eff_level, persona_id=_pid_t,
                                forced=(_lv["source"] == "force"),
                                capped=bool(_lv["profanity_capped"]))
                            self.logger.info(
                                "[temper] 被骂回应注入 level=%s source=%s "
                                "sticky=%s streak=%d pcap=%s persona=%s",
                                _eff_level, _lv["source"], _via_sticky,
                                _streak, _pcapped, _pid_t or "-")
                elif _deesc:
                    # 求和/道歉轮（P1 消气曲线）：不再瞬间停火——按骂战烈度
                    # 与道歉诚意进「记仇期」，逐轮消气；「开玩笑的」这类开脱
                    # 消得慢且会被点破。危机/off 档/未开闸 → 旧行为全清。
                    _last_fts = user_context.pop("_insult_ts", None)
                    if _last_fts:
                        record_deescalation()
                    _fight_fresh = bool(
                        _last_fts
                        and (_now_t - float(_last_fts or 0))
                        <= _tcfg["sticky_window_sec"])
                    _g_max = int(_tcfg.get("grudge_max_level") or 0)
                    if (_g_max <= 0
                            or not (_fight_fresh or _grudge_now > 0)
                            or _grudge_crisis(text)):
                        _grudge_all_clear()
                    else:
                        _sincere = is_sincere_apology(text)
                        _flip = (is_flippant_retraction(text)
                                 and not _sincere)
                        _pid_t, _lv, _eff_level, _pcapped = (
                            _resolve_persona_level())
                        if _eff_level == "off":
                            _grudge_all_clear()
                        else:
                            if _fight_fresh:
                                _streak0 = int(
                                    user_context.get("_insult_streak")
                                    or 0) or 1
                                _new_g = initial_grudge(
                                    _streak0, _sincere, _g_max)
                                record_grudge_set()
                            else:
                                _new_g = decay_grudge(_grudge_now, _sincere)
                            if _new_g > 0:
                                _gh = build_grudge_hint(
                                    _new_g, _eff_level, _lang,
                                    flippant=_flip)
                                if _gh:
                                    user_context["_temper_hint"] = _gh
                                    # 记仇轮出站否决：剥秒原谅句
                                    # （翻篇收尾轮和解语义合法不标）
                                    user_context["_temper_out_guard"] = (
                                        "grudge")
                                    user_context["_grudge_level"] = _new_g
                                    user_context["_grudge_ts"] = _now_t
                                    record_grudge_turn()
                                    if user_context.pop(
                                            "_emotional_context_block",
                                            None):
                                        record_emo_block_suppressed()
                                    record_fight_turn(
                                        convo_key, kind="grudge")
                                    self.logger.info(
                                        "[temper] 求和进记仇期 level=%d "
                                        "sincere=%s flippant=%s persona=%s",
                                        _new_g, _sincere, _flip,
                                        _pid_t or "-")
                                else:
                                    _grudge_all_clear()
                            else:
                                # 一步消完＝翻篇收尾（接受和解+一句边界，
                                # 语气偏淡不秒甜；语音同走 grudge 冷淡档）
                                _gh = build_grudge_hint(
                                    0, _eff_level, _lang, closeout=True)
                                if _gh:
                                    user_context["_temper_hint"] = _gh
                                    record_grudge_closeout()
                                    if user_context.pop(
                                            "_emotional_context_block",
                                            None):
                                        record_emo_block_suppressed()
                                    record_fight_turn(
                                        convo_key, kind="grudge")
                                    self.logger.info(
                                        "[temper] 翻篇收尾（带边界）"
                                        "persona=%s", _pid_t or "-")
                                user_context.pop("_grudge_level", None)
                                user_context.pop("_grudge_ts", None)
                                user_context.pop("_insult_streak", None)
                elif _taunt:
                    _pid_t, _lv, _eff_level, _pcapped = (
                        _resolve_persona_level())
                    _th3 = build_taunt_hint(_eff_level, _lang)
                    if _th3:
                        user_context["_temper_hint"] = _th3
                        record_taunt()
                        self.logger.info(
                            "[temper] 激将接梗注入 level=%s persona=%s",
                            _eff_level, _pid_t or "-")
                elif _grudge_plain:
                    # 记仇期普通轮（没道歉也没再骂，比如硬转话题）：本轮仍
                    # 端着回（用当前级别），气自然消一格；消到 0 静默回暖
                    # （没道歉就不给「翻篇宣言」）。危机/off 即刻散仇。
                    if _grudge_crisis(text):
                        _grudge_all_clear()
                    else:
                        _pid_t, _lv, _eff_level, _pcapped = (
                            _resolve_persona_level())
                        if _eff_level == "off":
                            _grudge_all_clear()
                        else:
                            _gh = build_grudge_hint(
                                _grudge_now, _eff_level, _lang)
                            if _gh:
                                user_context["_temper_hint"] = _gh
                                user_context["_temper_out_guard"] = "grudge"
                                record_grudge_turn()
                                if user_context.pop(
                                        "_emotional_context_block", None):
                                    record_emo_block_suppressed()
                                record_fight_turn(convo_key, kind="grudge")
                                self.logger.info(
                                    "[temper] 记仇期端着回应 level=%d "
                                    "persona=%s", _grudge_now, _pid_t or "-")
                            _left = decay_grudge(_grudge_now, False)
                            if _left > 0:
                                user_context["_grudge_level"] = _left
                                user_context["_grudge_ts"] = _now_t
                            else:
                                user_context.pop("_grudge_level", None)
                                user_context.pop("_grudge_ts", None)
                                user_context.pop("_insult_streak", None)
        except Exception:
            self.logger.debug("temper 注入跳过", exc_info=True)

    def _inject_origin_context(
        self, user_context: Dict[str, Any], *,
        platform: str = "", account_id: str = "", chat_key: str = "",
    ) -> None:
        """跨平台档案叙事 → ``_origin_block``（「有键即消费」，与 ``_bazi_block`` 同模式）。

        「客户从哪个平台来 / 在那边叫什么 / 聊过哪些话题域」的**叙事层**；具体记忆
        事实仍走 episodic 轨道，两层不重叠。数据经 ``companion_context.resolve_origin_block``
        进程级 provider（contacts 子系统就绪时注册；``contacts.origin_profile.enabled``
        热闸在 provider 内部）——未注册/关闸/无档案 → 不注入，零行为变化。
        每轮先清残留（换会话/档案被删后不得粘住旧叙事）。
        """
        user_context.pop("_origin_block", None)
        ck = str(chat_key or "").strip()
        if not ck:
            return
        try:
            from src.utils.companion_context import resolve_origin_block
            block = resolve_origin_block(
                account_id, ck, channel=str(platform or "telegram"))
            if block:
                user_context["_origin_block"] = block
        except Exception:
            self.logger.debug("origin inject skipped", exc_info=True)

    def _inject_bazi_context(
        self, user_context: Dict[str, Any], text: str,
        user_id_str: str, chat_id: Any, platform: str = "",
    ) -> None:
        """命理话题 → 往 user_context 注入 ``_bazi_block``。

        块内容按意图组合（每轮重算）：今日灵签 / 命盘参考（+所问年份流年数据）/
        详批深读或付费软引导（变现门控）/ 生辰采集 directive。
        非命理轮清掉残留块（粘性窗内的追问轮除外）；缺生辰的询问有冷却防连环逼问。
        """
        user_context.pop("_bazi_block", None)
        cfg = self._bazi_cfg()
        if not cfg.get("enabled", False):
            return
        from src.companion.bazi_engine import bazi_available
        if not bazi_available():
            return  # 缺 lunar_python：不注入也不采集（收了生辰也排不了盘，别空许诺）
        from src.companion.bazi_context import (
            build_bazi_prompt_block, build_birth_ask_directive,
            detect_bazi_topic, detect_daily_card_intent,
            detect_deep_reading_intent, extract_target_year,
            topic_active, touch_topic,
        )
        now = time.time()
        sticky_min = float(cfg.get("topic_sticky_minutes", 10) or 0)
        daily_intent = detect_daily_card_intent(text)
        deep_intent = detect_deep_reading_intent(text)
        topical = detect_bazi_topic(text) or daily_intent or deep_intent
        if not topical and not topic_active(
                user_context, sticky_minutes=sticky_min, now=now):
            return
        if topical:
            touch_topic(user_context, now)
        key = self._episodic_storage_key(
            user_id_str, chat_id, platform, user_context=user_context)
        # 同轮闭环：本条消息自带完整生辰（「帮我算八字，我1995年3月5日早上8点生」）
        # → 即时排盘，别让 AI 反问一遍；落库仍由回复后的 capture 完成。
        from src.companion.bazi_profile import extract_birth_info
        info = extract_birth_info(text)
        _same_turn = info is not None
        if info is None:
            info = self.resolve_birth_info(key) if key else None

        blocks: List[str] = []
        chart = None
        if info is not None:
            from src.companion.bazi_engine import compute_bazi
            chart = compute_bazi(info)

        from src.companion.bazi_stats import get_bazi_stats
        _stats = get_bazi_stats()

        # ① 今日灵签（有盘→个性化能量日；无盘→通用签，且不阻断后面的生辰采集）
        if daily_intent and cfg.get("daily_card", True):
            try:
                from src.companion.bazi_daily import build_daily_card_block, daily_card
                card = daily_card(
                    day_master_gan=(((chart or {}).get("day_master")) or " ")[0].strip(),
                    seed_key=key or user_id_str, now_ts=now)
                if card:
                    blk = build_daily_card_block(card)
                    if blk:
                        blocks.append(blk)
                        _stats.record_daily_card("chat")
            except Exception:
                self.logger.debug("bazi daily card skipped", exc_info=True)

        if chart:
            # ② 命盘参考（所问年份 → 附该年流年真数据，防 LLM 编干支）
            from src.companion.bazi_engine import (
                format_chart_summary, format_dayun_line, format_liunian_line,
                liunian_detail,
            )
            summary = format_chart_summary(chart)
            try:
                yr = extract_target_year(
                    text, time.localtime(now).tm_year)
                # 出生年防误判：「我1995年3月5日生的」里的 1995 是生年不是所问流年
                if yr and str(yr) == str(chart.get("solar_date") or "")[:4]:
                    yr = None
                if yr:
                    ln_line = format_liunian_line(
                        liunian_detail((chart.get("day_master") or " ")[0], yr))
                    if ln_line:
                        summary = f"{summary}\n{ln_line}"
            except Exception:
                self.logger.debug("bazi liunian enrich skipped", exc_info=True)
            # ③ 详批深读：变现门控（gate 关或已解锁 → 深读指令+大运真数据；
            #    未解锁 → 软引导+免费轻量——详批级数据也不进盘面，指令与数据同口径）
            deep_allowed = False
            if deep_intent:
                deep_allowed = self._bazi_deep_allowed(user_context, user_id_str)
                if deep_allowed:
                    dy_line = format_dayun_line(chart)
                    if dy_line:
                        summary = f"{summary}\n{dy_line}"
            block = build_bazi_prompt_block(
                summary,
                hour_known=bool(chart.get("hour_known")),
                has_dayun=bool(chart.get("dayun")),
            )
            if block:
                blocks.append(block)
                _stats.record_chart_injection(same_turn=_same_turn)
            if deep_intent:
                if deep_allowed:
                    from src.companion.bazi_context import build_deep_reading_directive
                    self._record_bazi_funnel(user_id_str, "bazi_deep")
                    _stats.record_deep_reading(allowed=True)
                    blocks.append(build_deep_reading_directive())
                else:
                    # 软引导带冷却：冷却窗内反复问 → 只给免费盘面不再提会员
                    # （复读推销是陪伴产品大忌；漏斗只记真实曝光）
                    up_cd_h = float(cfg.get("upsell_cooldown_hours", 6) or 0)
                    last_up = float(user_context.get("_bazi_upsell_ts") or 0)
                    if not last_up or (now - last_up) >= up_cd_h * 3600.0:
                        user_context["_bazi_upsell_ts"] = now
                        self._record_bazi_funnel(user_id_str, "bazi_upsell")
                        _stats.record_deep_reading(allowed=False)
                        blocks.append(
                            self._bazi_upsell_block(user_context, user_id_str))
        elif not daily_intent or detect_bazi_topic(text):
            # ④ 缺生辰 → 顺势采集（带冷却防连环逼问；纯问「今日签」的轮不逼生辰）
            ask_cd_h = float(cfg.get("ask_cooldown_hours", 24) or 0)
            last_ask = float(user_context.get("_bazi_ask_ts") or 0)
            if not last_ask or (now - last_ask) >= ask_cd_h * 3600.0:
                bd = None
                try:
                    bd = self.resolve_birthday(key) if key else None
                except Exception:
                    bd = None
                user_context["_bazi_ask_ts"] = now
                _stats.record_ask_directive()
                blocks.append(build_birth_ask_directive(bd))

        blocks = [b for b in blocks if b]
        if blocks:
            _stats.record_topic_turn()
            user_context["_bazi_block"] = "\n\n".join(blocks)

    def _bazi_entitlement(self, user_context: Dict[str, Any], user_id_str: str):
        """端用户权益（selfie 同款链）：context 懒解析 → 即时解析 → None。"""
        ent = user_context.get("entitlement")
        if isinstance(ent, dict):
            return ent
        try:
            from src.utils.companion_context import resolve_entitlement
            ent = resolve_entitlement(user_id_str)
        except Exception:
            ent = None
        return ent if isinstance(ent, dict) else None

    def _bazi_deep_allowed(
        self, user_context: Dict[str, Any], user_id_str: str,
    ) -> bool:
        """详批是否放行：monetization gate 总闸关 → 恒放行（零破坏）；开 → 按权益判定。"""
        from src.utils.monetization import feature_allowed
        feature = str(self._bazi_cfg().get("premium_feature", "bazi_reading") or "")
        return feature_allowed(
            self._bazi_entitlement(user_context, user_id_str), feature,
            gate_enabled=self._monetization_gate_enabled())

    def _bazi_upsell_block(
        self, user_context: Dict[str, Any], user_id_str: str,
    ) -> str:
        """详批被门控拦下 → 软引导块（附目录里最划算的报价话术，绝不硬拒）。"""
        from src.companion.bazi_context import build_premium_upsell_directive
        feature = str(self._bazi_cfg().get("premium_feature", "bazi_reading") or "")
        pitch = ""
        try:
            from src.utils.monetization import (
                merge_catalog, upsell_offer, upsell_pitch_hint,
            )
            cfg = self.config.config if hasattr(self.config, "config") else {}
            mon = (cfg.get("monetization") or {}) if isinstance(cfg, dict) else {}
            offer = upsell_offer(
                self._bazi_entitlement(user_context, user_id_str), feature,
                catalog=merge_catalog(mon.get("catalog")), gate_enabled=True)
            pitch = upsell_pitch_hint(
                offer, persona_name=self._get_persona_name_for_context(user_context))
        except Exception:
            pitch = ""
        return build_premium_upsell_directive(pitch)

    async def _handle_bazi_kline_request(
        self, text: str, user_id_str: str, user_context: Dict[str, Any], chat_id: Any,
    ) -> Optional[str]:
        """Stage C：「人生 K 线/运势曲线」出图请求——排盘→逐年评分→PNG→发图。

        返回 ""=图已发出（配文随图，不再补文字）；None=非请求/未开/缺生辰/渲染或
        发送不可用（回落文字聊天，命盘注入块仍会让 AI 口头讲）。图片本体免费
        （分享传播面），逐年详解文本仍走详批变现门控——图引流、深度变现。
        """
        cfg = self._bazi_cfg()
        if not cfg.get("enabled", False) or not cfg.get("kline", True):
            return None
        from src.companion.bazi_context import detect_kline_intent
        if not detect_kline_intent(text):
            return None
        from src.companion.bazi_engine import bazi_available, compute_bazi
        if not bazi_available():
            return None
        from src.companion.bazi_profile import extract_birth_info
        key = self._episodic_storage_key(
            user_id_str, chat_id, user_context.get("platform", ""),
            user_context=user_context)
        info = extract_birth_info(text)
        if info is None:
            info = self.resolve_birth_info(key) if key else None
        if info is None:
            return None  # 缺生辰 → 交注入路径顺势采集（AI 自然要生辰）
        chart = compute_bazi(info)
        if not chart:
            return None
        from src.companion.bazi_kline import build_kline_series, render_kline_png
        from src.companion.bazi_stats import get_bazi_stats
        now_year = time.localtime().tm_year
        series = build_kline_series(
            chart,
            start_year=now_year - 2,
            years=int(cfg.get("kline_years", 10) or 10),
        )
        if not series:
            return None
        out_dir = Path(str(cfg.get("kline_out_dir") or "tmp_bazi"))
        out_path = out_dir / (
            f"kline-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.png")
        if not render_kline_png(series, str(out_path)):
            get_bazi_stats().record_kline(ok=False)
            return None
        caption = str(cfg.get("kline_caption") or "").strip() or (
            "给你画好啦～这是你近十年的运势曲线（仅供参考哦），"
            "想细看哪一年跟我说😊")
        sent = await self._try_send_selfie_media(
            user_context, chat_id, str(out_path), caption)
        get_bazi_stats().record_kline(ok=bool(sent))
        if sent:
            # 媒体轮回写：下一轮 LLM 要知道"我刚发过运势曲线图"
            user_context["_stage_media_note"] = "[图片] " + caption
            return ""  # 图+配文已发出
        return None  # 发送链路不可用 → 回落文字聊天（注入块会带上命盘）

    async def _handle_song_request(
        self, text: str, user_id_str: str, user_context: Dict[str, Any],
        chat_id: Any,
    ) -> Optional[str]:
        """Stage S：点名/逼唱要歌 → 发预渲染真唱段（实施66 P0-1，A 线兑现短路）。

        判定＝双档检测（strict 词表 ∨ 粘性窗内含「唱」追加，2026-08-23
        「不行，必须唱」实录）；兑现闸序镜像 B 线 ``autosend_song``。
        返回 ""=唱段已发出（framing 配文随语音）；None=非要歌/发不出——
        发不出时注入 REFUSAL hint（禁文字假唱/空头承诺）后回落文字链。
        粘性/压力记账落 user_context（随 ContextStore 持久，供冷却豁免与
        5c2s 假唱守卫的语境判定）。
        """
        from src.companion.song_stock import (
            _HINT_REFUSAL,
            detect_song_request,
            get_song_stats,
            resolve_singing_cfg,
            song_pressure,
            song_sticky_active,
            song_topic_state,
            touch_song_request,
        )
        state = song_topic_state(
            text, sticky=song_sticky_active(user_context))
        if not state:
            return None
        stats = get_song_stats()
        if state == "demand":
            pressure = touch_song_request(user_context)
            if not detect_song_request(text):
                stats.bump("sticky_demand")
        else:
            pressure = song_pressure(user_context)
        scfg = resolve_singing_cfg(
            self.config.config if hasattr(self.config, "config") else {})
        if state != "demand" or not scfg.get("enabled", False):
            # loose 催促（不兑现，防误塞歌）/ 能力未开：都先禁假唱
            user_context["_song_coherence_hint"] = _HINT_REFUSAL
            stats.bump("refusal_hint_a")
            return None
        # 定制求歌让位（与 B 线同口径）：要的是「写一首关于我们的」，拿现货
        # 顶上=答非所问。A 线暂无订单 intake（P1）→ 先禁假唱禁空头承诺。
        try:
            from src.companion.song_orders import (
                detect_custom_song_request as _dcsr_a,
                resolve_custom_cfg as _rcc_a,
            )
            if (_rcc_a(self.config.config
                       if hasattr(self.config, "config") else {})
                    .get("enabled") and _dcsr_a(text)):
                user_context["_song_coherence_hint"] = _HINT_REFUSAL
                stats.bump("custom_yield_a")
                return None
        except Exception:
            pass
        delivered = await self._deliver_song(
            text, user_context, chat_id, demand_pressure=pressure)
        if delivered:
            return ""  # 唱段+配文已发出，不再补文字
        user_context["_song_coherence_hint"] = _HINT_REFUSAL
        stats.bump("refusal_hint_a")
        return None

    async def _deliver_song(
        self, text: str, user_context: Dict[str, Any], chat_id: Any, *,
        demand_pressure: int = 1,
    ) -> bool:
        """A 线唱段兑现核心（Stage S 与 5c2s 假唱守卫共用）。True=已发出。

        闸序镜像 B 线 ``autosend_song``：enabled → 人设 → 频控（被动逼唱
        豁免冷却）→ 备货过滤选曲 → 发送（① 编排器受管 ② ``_send_voice_to_chat``
        直发缝，与 selfie 双路同构）→ 账本/观测/媒体日志。绝不现场合成、
        无备货绝不冒充（回 False 交诚实文字）。
        """
        from src.companion.song_stock import (
            day_start_ts,
            find_stock_file,
            framing_caption,
            get_song_ledger,
            get_song_stats,
            load_song_manifest,
            pick_song,
            requested_song_scene,
            resolve_singing_cfg,
            song_gate_verdict,
            stock_root,
            templates_dir,
        )
        cfg_root = self.config.config if hasattr(self.config, "config") else {}
        scfg = resolve_singing_cfg(cfg_root)
        if not scfg.get("enabled", False):
            return False
        stats = get_song_stats()
        stats.bump("a_requests")
        pid = str(user_context.get("account_persona_id") or "").strip()
        if not pid:
            stats.bump("no_persona")
            return False
        platform = str(user_context.get("platform") or "telegram").strip()
        account_id = str(user_context.get("account_id") or "").strip()
        try:
            from src.inbox.normalizer import conv_id as _cidf
            conv = (_cidf(platform, account_id, str(chat_id))
                    if account_id else f"tg::{chat_id}")
        except Exception:
            conv = f"tg::{chat_id}"
        now = time.time()
        ledger = get_song_ledger()
        ok, why = song_gate_verdict(
            scfg,
            today_count=ledger.count_since(conv, day_start_ts(now)),
            last_ts=ledger.last_ts(conv), now=now,
            demand_pressure=demand_pressure)
        if not ok:
            stats.bump(why)
            return False
        if why == "pressure_exempt":
            stats.bump("pressure_exempt")
        templates = load_song_manifest(templates_dir(scfg))
        if not templates:
            stats.bump("no_template")
            return False
        sroot = stock_root(scfg)
        stocked = [t for t in templates
                   if find_stock_file(pid, t.id, sroot=sroot)]
        if not stocked:
            stats.bump("no_stock")
            return False
        han = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        alpha = sum(1 for c in text if c.isascii() and c.isalpha())
        lang = "en" if (han == 0 and alpha >= 4) else "zh"
        tmpl = pick_song(
            stocked, lang=lang,
            scene_hint=requested_song_scene(text),
            exclude_ids=ledger.recent_template_ids(
                conv, float(scfg.get("repeat_window_days", 7) or 7), now=now),
            variety_key=conv,
            day_key=time.strftime("%Y%m%d", time.localtime(now)),
            allow_lang_fallback=bool(scfg.get("allow_lang_fallback", False)))
        if tmpl is None:
            stats.bump("no_pick")
            return False
        spath = find_stock_file(pid, tmpl.id, sroot=sroot)
        if spath is None:
            stats.bump("no_stock")
            return False
        caption = framing_caption(scfg, conv_id=conv, now=now)
        first_line = next(
            (ln.strip() for ln in str(tmpl.lyrics or "").splitlines()
             if ln.strip()), "")
        label = (f"[唱歌]《{tmpl.title}》"
                 + (f" ♪ {first_line[:40]}" if first_line else ""))
        sent = False
        # ① 编排器受管媒体（与 selfie/kline 同缝）
        try:
            if platform and account_id:
                from src.integrations.account_orchestrator import (
                    get_orchestrator,
                )
                orch = get_orchestrator(cfg_root)
                if orch.owns_media(platform, account_id):
                    res = await orch.send_media(
                        platform, account_id, str(chat_id),
                        media_path=str(spath), media_type="voice",
                        caption=caption, inbox_text=label)
                    sent = bool(isinstance(res, dict) and res.get("delivered"))
        except Exception:
            self.logger.debug("song orchestrator send failed", exc_info=True)
        # ② A 线主客户端直发（telegram_client 注入的语音文件缝）
        if not sent:
            try:
                vsender = user_context.get("_send_voice_to_chat")
                if callable(vsender):
                    sent = bool(await vsender(
                        chat_id, str(spath), caption, label))
            except Exception:
                self.logger.debug("song direct send failed", exc_info=True)
        if not sent:
            stats.bump("send_failed")
            return False
        ledger.record(conv, tmpl.id, now=now)
        stats.note_sent(tmpl.id, pid)
        stats.bump("a_line_sent")
        try:
            self._record_media_sent(
                user_context, note=f"[唱歌]《{tmpl.title}》", scene="",
                series=f"song:{tmpl.id}")
        except Exception:
            pass
        self.logger.info(
            "[song] A 线唱段已发 persona=%s tmpl=%s conv=%s pressure=%d",
            pid, tmpl.id, conv, int(demand_pressure))
        user_context["_stage_media_note"] = "[语音] " + (caption or label)
        return True

    async def _apply_song_claim_guard(
        self, reply: str, user_context: Dict[str, Any], *, chat_id: Any,
        user_text: str = "", log_prefix: str = "",
    ) -> str:
        """5c2s. 文字假唱出站守卫（实施66 P0-3）。

        表演体（宣告+唱词引用/自评组合）命中 → ① 语境内（要歌/粘性窗）能真唱：
        先发真唱段再剥假唱文字——LLM 领会了词表漏掉的要歌意图时，把它的
        「表演决定」升级成真兑现（与 5c0 LLM 发图指令同哲学的救回通道）；
        ② 发不出/无语境：句级剥离，剥空换台阶句。任何异常原样放行（守卫
        绝不阻塞出话）。
        """
        try:
            if not reply:
                return reply
            from src.companion.song_stock import (
                detect_song_performance,
                get_song_stats,
                song_deflection_line,
                song_pressure,
                song_sticky_active,
                song_topic_state,
                strip_song_performance,
            )
            sticky = song_sticky_active(user_context)
            state = song_topic_state(user_text, sticky=sticky)
            ctx = bool(state) or sticky
            if not detect_song_performance(reply, song_context=ctx):
                return reply
            stats = get_song_stats()
            stats.bump("claim_detected")
            delivered = False
            if ctx:
                try:
                    delivered = await self._deliver_song(
                        user_text, user_context, chat_id,
                        demand_pressure=song_pressure(user_context))
                except Exception:
                    delivered = False
            stripped = strip_song_performance(reply)
            if delivered:
                stats.bump("claim_fulfilled")
                self.logger.info(
                    "%s[song] 文字假唱→已兑现真唱段并剥离表演文字", log_prefix)
            else:
                stats.bump("claim_blocked")
                self.logger.info(
                    "%s[song] 文字假唱已拦截（无法兑现），表演段已剥离",
                    log_prefix)
            if not stripped.strip():
                lang = "zh"
                try:
                    lang = str(self._stage_lang(user_context, user_text)
                               or "zh")
                except Exception:
                    lang = "zh"
                stripped = song_deflection_line(lang, key=str(chat_id))
            return stripped
        except Exception:
            self.logger.debug("song claim guard skipped", exc_info=True)
            return reply

    def _record_bazi_funnel(self, contact_key: str, kind: str) -> None:
        """详批放行/软引导埋点进转化漏斗（teaser 事件复用，best-effort 绝不抛）。"""
        try:
            from src.utils.companion_funnel_store import peek_companion_funnel_store
            store = peek_companion_funnel_store()
            if store is not None:
                feature = str(
                    self._bazi_cfg().get("premium_feature", "bazi_reading") or "")
                store.record_teaser(str(contact_key or ""), kind, feature)
        except Exception:
            self.logger.debug("record_bazi_funnel skipped", exc_info=True)

    async def _capture_birth_info_fact(
        self, user_id: str, user_msg: str, reply: str, chat_id: Any,
        platform: str = "",
        account_id: str = "",
    ) -> None:
        """本轮若出现完整生辰（用户原话或 AI 复述确认）→ 规范化落库 user_stated 事实。

        幂等：已知且完全一致 → 跳过；补时辰/更正 → 写新事实（resolve 新行胜出）。
        """
        if not self._episodic_store or not self._bazi_cfg().get("enabled", False):
            return
        from src.companion.bazi_profile import (
            birth_info_fact_text, birth_info_from_turn,
        )
        key = self._episodic_storage_key(
            user_id, chat_id, platform, account_id=account_id)
        if not key:
            return
        info = birth_info_from_turn(user_msg, reply)
        if info is None or not info.valid():
            # 性别补录：报完生辰后下一轮才说「我是女生」——命理话题窗口内、已知生辰
            # 缺性别时，把性别并进既有事实（解锁大运；窗口外的性别闲聊不采，防误归因）。
            await self._complete_birth_gender(
                user_id, user_msg, key, account_id=account_id,
                platform=platform)
            return
        try:
            known = self.resolve_birth_info(key)
            if known is not None and not info.gender and known.gender:
                info.gender = known.gender  # 新事实缺性别时继承已知，防降级
            if known is not None and known.cache_key() == info.cache_key():
                return  # 已知且一致（继承补全后再比，防「缺性别复述」重复落库）
        except Exception:
            pass
        fact = birth_info_fact_text(info)
        rid = self._episodic_store.add_fact(key, fact, "heuristic", source="user_stated")
        await self._episodic_patch_embedding(rid, fact)
        # FateX 产品库双写：权威生辰进独立库（结构化、账号隔离）；episodic 那行
        # 保留为「说过生辰」的对话记忆。软失败不阻断主链。
        try:
            from src.fatex.store import get_fatex_store
            _fx = get_fatex_store()
            if _fx is not None:
                _fx.upsert_birth(
                    platform, account_id, str(user_id), info,
                    source="user_stated", raw_text=fact)
        except Exception:
            self.logger.debug("[fatex] upsert_birth failed", exc_info=True)
        try:
            from src.companion.bazi_stats import get_bazi_stats
            get_bazi_stats().record_birth_captured()
        except Exception:
            pass
        self.logger.info("[episodic] birth info captured user=%s %s", user_id, fact)

    async def _complete_birth_gender(
        self, user_id: str, user_msg: str, memory_key: str,
        account_id: str = "",
        platform: str = "",
    ) -> None:
        """命理采集补录：已知生辰但缺性别，且本轮用户报了性别 → 写入补全事实。

        仅在命理话题粘性窗口内触发（由用户 context 的 ``_bazi_topic_ts`` 判定），
        避免把无关闲聊里的「男生/女生」错并进生辰画像。
        ``account_id`` 须与写入 ``_bazi_topic_ts`` 的 process_message 同口径分桶，
        否则双号场景读裸键空桶 → 话题窗恒 False → 补录静默失效。
        已知边界（P1-2 群聊分窗后）：群聊窗的话题时间戳写在群窗 ctx，本函数读
        私窗 → 群内性别补录不触发——命理主打私聊陪伴，宁缺勿错，刻意不透传。
        """
        from src.companion.bazi_profile import birth_info_fact_text, extract_gender
        g = extract_gender(user_msg)
        if not g:
            return
        try:
            ctx = self._get_user_context(str(user_id), account_id)
            from src.companion.bazi_context import topic_active
            sticky_min = float(self._bazi_cfg().get("topic_sticky_minutes", 10) or 0)
            if not topic_active(ctx, sticky_minutes=sticky_min):
                return
        except Exception:
            return
        known = self.resolve_birth_info(memory_key)
        if known is None or known.gender:
            return
        known.gender = g
        fact = birth_info_fact_text(known)
        rid = self._episodic_store.add_fact(
            memory_key, fact, "heuristic", source="user_stated")
        await self._episodic_patch_embedding(rid, fact)
        # FateX 库同步补性别（merge 语义保留已知时辰）
        try:
            from src.fatex.store import get_fatex_store
            _fx = get_fatex_store()
            if _fx is not None:
                _fx.upsert_birth(
                    platform, account_id, str(user_id), known,
                    source="user_stated", raw_text=fact)
        except Exception:
            self.logger.debug("[fatex] gender upsert failed", exc_info=True)
        try:
            from src.companion.bazi_stats import get_bazi_stats
            get_bazi_stats().record_gender_completed()
        except Exception:
            pass
        self.logger.info("[episodic] birth gender completed user=%s %s", user_id, fact)

    def build_profile_ask_opener(
        self,
        slot: str,
        *,
        memory_key: str = "",
        stage: str = "",
        intimacy: float = 0.0,
        last_emotion: str = "",
        last_emotion_intensity: float = -1.0,
        contact_key: str = "",
    ) -> Dict[str, Any]:
        """主动采集某画像槽位（生日 / 称呼 / …）的开场 directive（Stage T 通用版）。

        仅在情绪护栏正常（非危机/非低落）时问；危机/低落不合时宜 → 返回空（交回原开场）。
        门槛（关系够深 / 槽位未知 / 冷却）由上层 ``should_ask_profile_slot`` 决定，本方法只产文案。
        ``mode`` 为 ``ask_<slot>``（如 ask_birthday / ask_name）。
        """
        from src.utils.profile_collect import ask_directive
        s = str(slot or "").strip().lower()
        directive = ask_directive(s, stage=stage)
        if not directive:  # 未登记的槽位
            return {"mode": "", "directive": "", "fact": ""}
        gate = self._proactive_emotion_gate(
            memory_key, last_emotion, last_emotion_intensity)
        if gate != "":  # block(危机) 或 soft(低落) 都不主动采集
            return {"mode": "", "directive": "", "fact": ""}
        return {"mode": f"ask_{s}", "directive": directive, "fact": "",
                "context_facts": []}

    def build_birthday_ask_opener(
        self,
        *,
        memory_key: str = "",
        stage: str = "",
        intimacy: float = 0.0,
        last_emotion: str = "",
        last_emotion_intensity: float = -1.0,
        contact_key: str = "",
    ) -> Dict[str, Any]:
        """主动采集生日开场（Stage R）——Stage T 起为 build_profile_ask_opener('birthday') 的兼容入口。"""
        return self.build_profile_ask_opener(
            "birthday", memory_key=memory_key, stage=stage, intimacy=intimacy,
            last_emotion=last_emotion, last_emotion_intensity=last_emotion_intensity,
            contact_key=contact_key)

    def resolve_preferred_name(self, memory_key: str):
        """从某用户 episodic 记忆里扫出 TA 希望的称呼/名字；扫不到 → None（Stage T）。

        复用 ``memory_slots.extract_slot``（单值身份槽 name）解析既有记忆事实——称呼 capture
        早已由 ``memory_heuristic``（叫我X/我是X/call me/my name）落库，这里只读不写。
        """
        store = getattr(self, "_episodic_store", None)
        key = str(memory_key or "").strip()
        if not store or not key or not hasattr(store, "list_rows"):
            return None
        try:
            from src.utils.memory_slots import SLOT_NAME, extract_slot
            rows = store.list_rows(prefix=key, limit=80, source="user_stated") or []
            if not rows:
                rows = store.list_rows(prefix=key, limit=80) or []
            for r in rows:
                slot = extract_slot((r or {}).get("content") or "")
                if slot and slot[0] == SLOT_NAME and slot[1]:
                    return slot[1]
        except Exception:
            self.logger.debug("resolve_preferred_name failed", exc_info=True)
        return None

    def resolve_residence(self, memory_key: str):
        """从某用户 episodic 记忆里扫出居住地；扫不到 → None。

        复用 ``memory_slots.extract_slot`` 的 residence 槽（「我住在X」规范事实，
        与 ``_capture_residence_fact`` 写入口径一致）。只读不写。
        """
        store = getattr(self, "_episodic_store", None)
        key = str(memory_key or "").strip()
        if not store or not key or not hasattr(store, "list_rows"):
            return None
        try:
            from src.utils.memory_slots import SLOT_RESIDENCE, extract_slot
            rows = store.list_rows(prefix=key, limit=80, source="user_stated") or []
            if not rows:
                rows = store.list_rows(prefix=key, limit=80) or []
            for r in rows:
                slot = extract_slot((r or {}).get("content") or "")
                if slot and slot[0] == SLOT_RESIDENCE and slot[1]:
                    return slot[1]
        except Exception:
            self.logger.debug("resolve_residence failed", exc_info=True)
        return None

    def resolve_age(self, memory_key: str):
        """从某用户 episodic 记忆里扫出年龄（B50）；扫不到 → None。只读不写。"""
        store = getattr(self, "_episodic_store", None)
        key = str(memory_key or "").strip()
        if not store or not key or not hasattr(store, "list_rows"):
            return None
        try:
            from src.utils.memory_slots import SLOT_AGE, extract_slot
            rows = store.list_rows(prefix=key, limit=80, source="user_stated") or []
            if not rows:
                rows = store.list_rows(prefix=key, limit=80) or []
            for r in rows:
                slot = extract_slot((r or {}).get("content") or "")
                if slot and slot[0] == SLOT_AGE and slot[1]:
                    return slot[1]
        except Exception:
            self.logger.debug("resolve_age failed", exc_info=True)
        return None

    def _inject_known_profile(self, user_context: Dict[str, Any],
                              memory_key: str) -> None:
        """B50（`_299` 三次自报 38 岁仍被问年龄段）：已知画像硬注入 + 禁复问。

        关键词召回是按相关性挑记忆——对方聊别的话题时「38 岁」那条永远不进
        prompt，LLM 不知道就会问。已知身份槽（称呼/年龄/居住地/生日）是小而
        恒真的事实，**每轮无条件注入**并明令禁止复问。与 ``_bazi_block`` 同
        「有键即消费」模式；每轮重建、无已知项清残留。绝不抛。
        """
        user_context.pop("_known_profile_block", None)
        try:
            key = str(memory_key or "").strip()
            if not key:
                return
            parts = []
            name = self.resolve_preferred_name(key)
            if name:
                parts.append(f"称呼：{name}")
            age = self.resolve_age(key)
            if age:
                parts.append(f"年龄：{age}岁")
            residence = self.resolve_residence(key)
            if residence:
                parts.append(f"居住地：{residence}")
            try:
                birth = self.resolve_birth_info(key)
                if birth is not None and getattr(birth, "month", 0) and getattr(birth, "day", 0):
                    parts.append(f"生日：{birth.month}月{birth.day}日")
            except Exception:
                pass
            if not parts:
                return
            user_context["_known_profile_block"] = (
                "【对方已经告诉过你的信息——绝不要再问】\n"
                + "；".join(parts) + "\n"
                "（需要时可自然引用；再次询问这些已知项＝没在认真听的穿帮。"
                "对方主动更正时以新说法为准。）"
            )
        except Exception:
            self.logger.debug("_inject_known_profile failed", exc_info=True)

    def _inject_self_state(self, user_context: Dict[str, Any]) -> None:
        """B52（`_287`）：人设自述近况（要睡了/去健身…）在 TTL 窗内注入衔接指令。

        与 ``_bazi_block`` 同「有键即消费」模式；每轮重建、窗外清残留。绝不抛。
        """
        user_context.pop("_self_state_block", None)
        try:
            from src.companion.self_state import self_state_note
            note = self_state_note(user_context)
            # #177：人工接管期间坐席以人设身份亲口说过的事实（持久、无 TTL；
            # ``human_outbound_memory`` 于手动发送成功后写入）——与短期自述状态
            # 同一注入口，A/B 两线同经此处，ai_client 零改动。
            try:
                from src.inbox.human_outbound_memory import (
                    human_media_note, human_said_note,
                )
                _hn = human_said_note(user_context)
                # 接力记忆 P0-1：坐席替人设发过的图。selfie 链的「你最近发过的照片」块
                # （_media_sent_note，_inject_scene_state 先于本方法跑）已含全部条目时
                # 不重复；selfie 关着（客服场景）→ 这里是唯一注入口。
                if not str(user_context.get("_media_sent_note") or "").strip():
                    _hm = human_media_note(user_context)
                    if _hm:
                        _hn = "\n".join(x for x in (_hn, _hm) if x)
            except Exception:
                _hn = ""
            # J-10 三期（#177/#171）：未兑现承诺块——memory.promises.inject 出厂关；
            # 开后只列近 max_age_days 内、最多 max_items 条，块头钉「别自相矛盾、不要每轮道歉」。
            _pn = ""
            try:
                _pc = (self._memory_cfg or {}).get("promises") or {}
                if isinstance(_pc, dict) and _pc.get("inject", False):
                    from src.utils.memory_promises import promise_note
                    _pn = promise_note(
                        user_context,
                        max_items=int(_pc.get("max_items", 3)),
                        max_age_days=float(_pc.get("max_age_days", 14)))
            except Exception:
                _pn = ""
            # 接力记忆 P0-3：刚从人工接管交回 AI → 接管窗口的接力摘要（有限轮次/TTL 自撤）
            try:
                from src.inbox.handoff_memory import handoff_note
                _hf = handoff_note(user_context)
            except Exception:
                _hf = ""
            note = "\n".join(x for x in (_hf, note, _hn, _pn) if x)
            if note:
                user_context["_self_state_block"] = note
        except Exception:
            self.logger.debug("_inject_self_state failed", exc_info=True)

    def episodic_inferred_counts(self) -> Dict[str, int]:
        """R17：全库 AI 推断计数（pending 待确认 / total），供校正质量看板。"""
        store = getattr(self, "_episodic_store", None)
        if not store or not hasattr(store, "inferred_counts"):
            return {"pending": 0, "total": 0}
        try:
            return store.inferred_counts()
        except Exception:
            return {"pending": 0, "total": 0}

    def episodic_key_health(self, sample: int = 10) -> Dict[str, Any]:
        """记忆 key 健康概览（裸 key 漂移监测），供运维探针/看板。

        裸 key（无 ``platform:`` 前缀）下的记忆对收件箱引擎不可见 → 拉低命中率。
        一次性迁移清存量后，本探针让复发可观测。未启用记忆返回 ``{"enabled": False}``。
        """
        store = getattr(self, "_episodic_store", None)
        if not store or not hasattr(store, "key_health"):
            return {"enabled": False}
        try:
            return {"enabled": True, **store.key_health(sample=sample)}
        except Exception:
            return {"enabled": False}

    def episodic_plan_key_migration(
        self, platform: str, *, only_simple: bool = True,
    ) -> Dict[str, Any]:
        """裸 key → canonical 迁移 dry-run（只读），供后台预览影响面。"""
        store = getattr(self, "_episodic_store", None)
        if not store:
            return {"enabled": False, "plan": []}
        try:
            from src.utils.episodic_key_migration import plan_canonical_migration
            plan = plan_canonical_migration(store, platform, only_simple=only_simple)
            return {"enabled": True, "platform": platform,
                    "candidates": len(plan), "plan": plan}
        except Exception:
            return {"enabled": False, "plan": []}

    def episodic_apply_key_migration(
        self, platform: str, *, only_simple: bool = True,
    ) -> Dict[str, Any]:
        """裸 key → canonical 迁移落地（幂等、按 content_hash 去重）。供后台一键修复。"""
        store = getattr(self, "_episodic_store", None)
        if not store:
            return {"enabled": False, "moved_rows": 0, "merged_keys": 0}
        try:
            from src.utils.episodic_key_migration import apply_canonical_migration
            rep = apply_canonical_migration(store, platform, only_simple=only_simple)
            return {"enabled": True, **rep}
        except Exception:
            return {"enabled": False, "moved_rows": 0, "merged_keys": 0}

    # ── R9b: 危机事件审计后台读写包装 ──────────────────────────────────
    def crisis_list_for_admin(
        self, *, limit: int = 50, only_unhandled: bool = False, user_prefix: str = "",
    ) -> List[Dict[str, Any]]:
        store = getattr(self, "_crisis_store", None)
        if not store:
            return []
        return store.list_recent(
            limit=limit, only_unhandled=only_unhandled, user_prefix=user_prefix,
        )

    def crisis_count_for_admin(self, *, only_unhandled: bool = False) -> int:
        store = getattr(self, "_crisis_store", None)
        return store.count(only_unhandled=only_unhandled) if store else 0

    def crisis_mark_handled_for_admin(
        self, event_id: int, *, handled_by: str = "", note: str = "",
    ) -> bool:
        store = getattr(self, "_crisis_store", None)
        if not store:
            return False
        return store.mark_handled(int(event_id), handled_by=handled_by, note=note)

    def crisis_summary_for_user(self, user_key: str, *, limit: int = 5) -> Dict[str, Any]:
        """R9d/R9e：某用户/会话的危机概览，供坐席工作台侧栏一眼掌握。

        以 ``user_key`` 同时匹配 ``user_id`` 前缀**或** ``chat_id`` 精确——一个 key 覆盖
        1:1 私聊（key=对端 user_id）与群聊（key=群 chat_id）两种场景。返回最近若干条
        + 其中未处理数 + 最新一条精简信息；store 不可用或无命中时返回空概览（绝不抛）。
        """
        store = getattr(self, "_crisis_store", None)
        key = str(user_key or "").strip()
        empty = {"total": 0, "unhandled": 0, "has_more": False, "latest": None, "recent": []}
        if not store or not key:
            return empty
        try:
            lim = max(1, min(int(limit or 5), 20))
            rows = store.list_recent(limit=lim, match_key=key)
        except Exception:
            return empty
        if not rows:
            return empty

        def _compact(r: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "id": r.get("id"),
                "level": r.get("level"),
                "category": r.get("category"),
                "escalated": bool(r.get("escalated")),
                "handled": bool(r.get("handled")),
                "created_at": r.get("created_at"),
            }

        unhandled = sum(1 for r in rows if not r.get("handled"))
        return {
            "total": len(rows),
            "unhandled": unhandled,
            "has_more": len(rows) >= lim,
            "latest": _compact(rows[0]),
            "recent": [_compact(r) for r in rows],
        }

    def episodic_profile_summary(self, memory_key: str, *, top_stable: int = 3) -> Dict[str, Any]:
        """R14：某 memory_key 的记忆画像聚合（tier/source 计数 + top stable）。"""
        empty = {
            "total": 0, "stable": 0, "raw": 0,
            "user_stated": 0, "ai_inferred": 0, "top_stable": [],
        }
        store = getattr(self, "_episodic_store", None)
        if not store or not str(memory_key or "").strip():
            return empty
        try:
            return store.profile_summary(str(memory_key), top_stable=top_stable)
        except Exception:
            return empty

    async def episodic_backfill_embeddings(
        self, limit: int = 20, memory_key_prefix: str = "", force: bool = False
    ) -> Dict[str, Any]:
        """Admin: fill episodic row embeddings (vector search). Batches via embed_with_fallback.

        ``force=True`` 重嵌**所有**行（换 embedding 模型后重建全量向量用），否则只补缺失的。
        """
        if not self._episodic_store or not self.ai_client:
            return {"ok": False, "error": "no_store"}
        mvec = (self._memory_cfg or {}).get("vector") or {}
        # R7：向量融合或 R5 近义去重任一开启即允许补全（覆盖率跟随需求）
        if not self._episodic_embeddings_needed():
            return {"ok": False, "error": "vector_disabled"}
        lim = max(1, min(int(limit or 20), 100))
        budcfg = mvec.get("daily_embed_budget") or {}
        if budcfg.get("enabled", False):
            global _EPISODIC_BACKFILL_BUDGET_DAY, _EPISODIC_BACKFILL_BUDGET_USED
            day = time.strftime("%Y-%m-%d", time.gmtime())
            if _EPISODIC_BACKFILL_BUDGET_DAY != day:
                _EPISODIC_BACKFILL_BUDGET_DAY = day
                _EPISODIC_BACKFILL_BUDGET_USED = 0
            max_day = max(0, int(budcfg.get("max_calls", 4000)))
            rem = max_day - _EPISODIC_BACKFILL_BUDGET_USED
            if rem <= 0:
                return {
                    "ok": False,
                    "error": "daily_embed_budget_exceeded",
                    "processed": 0,
                    "updated": 0,
                    "budget_remaining": 0,
                }
            lim = min(lim, rem)
        rows = self._episodic_store.fetch_rows_missing_embedding(
            lim, memory_key_prefix=memory_key_prefix, force=force
        )
        if not rows:
            return {"ok": True, "processed": 0, "updated": 0}
        from src.utils.episodic_vector import vec_to_blob

        work: List[Tuple[int, str]] = []
        for rid, _mk, content in rows:
            ft = (content or "").strip()
            if len(ft) < 2:
                continue
            work.append((rid, ft[:500]))

        updated = 0
        if not work:
            try:
                from src.monitoring.metrics_store import get_metrics_store

                get_metrics_store().record_episodic_backfill(0)
            except Exception:
                pass
            return {"ok": True, "processed": len(rows), "updated": 0}

        texts = [t for _, t in work]
        vecs = []
        try:
            try:
                vecs = await self.ai_client.embed_with_fallback(texts)
            except Exception:
                vecs = []
        finally:
            _episodic_backfill_charge_budget(len(work), mvec)
        if not vecs:
            try:
                from src.monitoring.metrics_store import get_metrics_store

                for _ in texts:
                    get_metrics_store().record_embed_fail()
            except Exception:
                pass
            try:
                from src.monitoring.metrics_store import get_metrics_store

                get_metrics_store().record_episodic_backfill(0)
            except Exception:
                pass
            return {"ok": True, "processed": len(rows), "updated": 0}

        ms = None
        try:
            from src.monitoring.metrics_store import get_metrics_store

            ms = get_metrics_store()
        except Exception:
            pass

        _emb_model = self._current_embedding_model()
        for i, (rid, _) in enumerate(work):
            vec = vecs[i] if i < len(vecs) else None
            if vec and self._episodic_store.update_embedding(
                rid, vec_to_blob(vec), _emb_model
            ):
                updated += 1
            elif ms:
                ms.record_embed_fail()

        try:
            from src.monitoring.metrics_store import get_metrics_store

            get_metrics_store().record_episodic_backfill(updated)
        except Exception:
            pass
        await asyncio.sleep(0.06)
        return {"ok": True, "processed": len(rows), "updated": updated}

    async def _get_embedding_cached(self, text: str) -> Optional[List[float]]:
        """
        # LRU 缓存�?Embedding 调用�?        - 相同查�直接命中缓存�?1ms），无需 API 调用 - 缓存满时淘汰�€旧条���FIFO LRU via OrderedDict�?        - API 失败时返�?None（调用方降级为纯 BM25�?        """
        key = text[:200].strip().lower()
        if key in _EMBED_CACHE:
            _EMBED_CACHE.move_to_end(key)
            _EMBED_STATS["cache_hits"] += 1
            return _EMBED_CACHE[key]
        try:
            vecs = await self.ai_client.embed([text])
            if not vecs:
                return None
            vec = vecs[0]
            if len(_EMBED_CACHE) >= _EMBED_CACHE_MAX:
                _EMBED_CACHE.popitem(last=False)
            _EMBED_CACHE[key] = vec
            _EMBED_STATS["api_calls"] += 1
            return vec
        except Exception as _e:
            self.logger.debug("Embedding 缓存调用失败: %s", _e)
            return None

    def _detect_implicit_feedback(self, text: str) -> str:
        """
        # �€测用户短消息���隐式反�情绪�?        返回 'pos'（好评）�?neg'（差�?纠�）或 None（无明确信号）�€?        仅� �?0 字的������效，避免���新问题为反��?        """
        t = (text or "").strip()
        if not t or len(t) > 60:
            return None
        t_lower = t.lower()
        _POS = {
            # "谢谢", "感谢", "好的", "收到", "ok", "okay", "thanks", "thank you", "شكرا", "obrigado", "�?, "�?, "👍", "�?, "明白�?, "知道�?, "alright", "got it", "noted", "شكراً",
        }
        _NEG = {
            # "不�", "错了", "不是", "有�", "重新", "重发", "再发", "纠�", "wrong", "incorrect", "not right", "错�", "不准�?, "خطأ", "errado", "不是这个", "不是这样", "你没回答", "没�清�", "答非�€�?, "我问的不�?, "说的不�", "你�的不�?, "重新�?, "没用",
        }
        for pos in _POS:
            if pos in t_lower:
                return "pos"
        for neg in _NEG:
            if neg in t_lower:
                return "neg"
        return None

    DISABLED_STATUSES = _CHANNEL_DISABLED_STATUSES

    @staticmethod
    def _is_channel_metrics_query(text: str) -> bool:
        """成功率 / 手续费费率 类咨询：应走实时通道数据，避免 KB「去后台查费率」直出抢答。"""
        raw = text or ""
        t = raw.lower()
        if "成功率" in raw or "success rate" in t or "success_rate" in t.replace(" ", ""):
            return True
        if "费率" in raw or "手续费" in raw:
            return True
        if any(w in t for w in ("fee rate", "commission", "service fee", "taux", "gebühr", "tariffa", "комиссия")):
            return True
        return False

    def _narrow_reply_greeting_allows(self, raw: str, cfg: Dict[str, Any]) -> bool:
        """收窄模式下 greeting：客服在线用语 + greeting_substrings + 内置多语种词库。"""
        tl = (raw or "").lower()
        cs = list(cfg.get("cs_online_substrings") or [])
        if not cs:
            cs = [
                "在吗", "在不", "有人吗", "客服", "人工", "在线吗", "上班吗",
                "有没有客服", "真人", "在不在",
            ]
        gr = merge_greeting_substrings(cfg.get("greeting_substrings") or [])
        if any((s or "").lower() in tl for s in cs):
            return True
        if any((s or "").lower() in tl for s in gr):
            return True
        # 与 _recognize_intent 一致（含单独「在」、哈喽/hola 等）
        if is_greeting_message(raw):
            return True
        return False

    def _narrow_reply_allows(
        self,
        text: str,
        intent: str,
        last_intent: str,
        user_context: Dict[str, Any],
    ) -> bool:
        """收窄模式：仅允许客服在线类 greeting、通道/限额/成功率类 channel_info、status_check。"""
        cfg = getattr(self, "_narrow_reply_cfg", None) or {}
        if not cfg.get("enabled"):
            return True
        allowed = set(cfg.get("allowed_intents") or [])
        if intent not in allowed:
            return False
        raw = text or ""
        tl = raw.lower()
        for d in cfg.get("deny_substrings") or []:
            ds = (d or "").strip().lower()
            if ds and ds in tl:
                return False
        if intent == "greeting":
            return self._narrow_reply_greeting_allows(raw, cfg)
        if intent in ("channel_info", "status_check"):
            subs = list(cfg.get("channel_topic_substrings") or [])
            if not subs:
                subs = [
                    "通道", "额度", "限额", "成功率", "代收", "代付", "维护", "波动",
                    "稳定", "单笔", "限制", "状态", "费率", "手续费",
                    "channel", "limit", "success rate", "payin", "payout", "fee",
                    "maintenance", "collection", "disburs", "deposit", "withdraw",
                    "quota", "commission", "transfer", "balance",
                ]
            if any((s or "").lower() in tl for s in subs):
                return True
            from src.hooks.registry import HookRegistry as _HReg
            _hr = _HReg.get_instance()
            if _hr.is_domain_metrics_query(raw):
                return True
            if _hr.is_meaningless_interjection(raw.strip()):
                return False
            sec = float(cfg.get("inherit_followup_seconds") or 120)
            lm = float(user_context.get("last_message_time") or 0)
            _fc = _hr.get_followup_config()
            _fi = _fc.get("followup_intents", {"channel_info", "status_check"})
            if last_intent in _fi and (time.time() - lm) < sec:
                if _hr.is_short_followup(raw.strip()):
                    return True
            return False
        return True

    def _is_channel_disabled(self, ch: dict) -> bool:
        return is_channel_disabled(ch)

    def _get_live_channel_status(self, include_fee: Optional[bool] = None) -> str:
        """读取后台通道实时数据。默认不在对话中展示手续费百分比。

        include_fee=None 时读取 ai.channel_status_include_fee（默认 false）。
        """
        if include_fee is None:
            try:
                ai_cfg = self.config.get_ai_config() if hasattr(self.config, "get_ai_config") else {}
                include_fee = bool((ai_cfg or {}).get("channel_status_include_fee", False))
            except Exception:
                include_fee = False
        try:
            rates = getattr(self.config, 'get_exchange_rates_config', lambda: None)()
            channels = (rates or {}).get('channels', {})
            result = format_live_channel_status_text(channels, include_fee=include_fee)
            if result:
                n_active = sum(
                    1 for ch in channels.values()
                    if isinstance(ch, dict) and not is_channel_disabled(ch)
                )
                n_dis = sum(
                    1 for ch in channels.values()
                    if isinstance(ch, dict) and is_channel_disabled(ch)
                )
                self.logger.info(
                    "channel_status_info (%d active, %d disabled): %s",
                    n_active, n_dis, result[:300],
                )
            return result
        except Exception:
            return ""

    # ── Phase ③ 剧情/场景 roleplay ────────────────────────────────
    def _story_cfg(self) -> Dict[str, Any]:
        try:
            cfg = self.config.config if hasattr(self.config, "config") else {}
            if not isinstance(cfg, dict):
                return {}
            return (cfg.get("companion") or {}).get("story") or {}
        except Exception:
            return {}

    def _selfie_cfg(self) -> Dict[str, Any]:
        """Stage A：陪伴形象照配置（companion.selfie）。缺/异常 → {}（默认关）。"""
        try:
            cfg = self.config.config if hasattr(self.config, "config") else {}
            if not isinstance(cfg, dict):
                return {}
            sc = (cfg.get("companion") or {}).get("selfie")
            return sc if isinstance(sc, dict) else {}
        except Exception:
            return {}

    # 人设级发图闸（2026-07-31「默认关闭相册」）：A 线五个媒体入口统一走
    # photo_capability.prompt_photos_allowed（模块级函数而非方法——测试桩类
    # 零改动 + monkeypatch 单点控门）。语义：全局 selfie 开着也要**当前人设**
    # 显式开了 capabilities.photos 才许发（注册相册+生成+指令+异步兑现同门）。

    def _monetization_gate_enabled(self) -> bool:
        """变现门控总闸：monetization.enabled 且 gate.enabled。任一关 → False（不计费）。"""
        try:
            cfg = self.config.config if hasattr(self.config, "config") else {}
            mon = (cfg.get("monetization") or {}) if isinstance(cfg, dict) else {}
            return bool(mon.get("enabled") and (mon.get("gate") or {}).get("enabled"))
        except Exception:
            return False

    def _get_selfie_cap(self, cap: int):
        """全局出图预算跟踪器（进程级单例，复用 DailyCapTracker，按 tz 0 点自动归零）。

        护住出图 API（OpenAI images 等）账单：**跨所有端用户/所有账号**的当日出图总次数硬上限——
        与「按端用户免费额度」互补（后者限单人、前者限全局爆发面，防 N 个新用户各刷免费图烧钱）。
        0=不限。运行时 set_cap 跟随 config 调整。Stage J：提升为单例后 Web 看板可 peek 同一份取快照。
        """
        from src.utils.selfie_cap import get_selfie_cap_tracker
        return get_selfie_cap_tracker(int(cap))

    def _record_selfie_event(self, contact_key: str, kind: str) -> None:
        """Stage B：把自拍准入结果(too_soon/locked/delivered)埋点进转化漏斗（best-effort）。

        只 ``peek`` 已存在的漏斗单例（monetization 就绪才有）——未初始化则静默 no-op，
        绝不在自拍主流程里误建 ``:memory:`` store，也绝不抛。``contact_key`` 取 ``user_id``
        （与 entitlement/tx_ledger 同一身份键），保证后续 ``exclusive_album`` 付费可归因。
        """
        try:
            from src.utils.companion_funnel_store import peek_companion_funnel_store
            store = peek_companion_funnel_store()
            if store is not None:
                store.record_selfie(str(contact_key or ""), kind)
        except Exception:
            self.logger.debug("record_selfie_event skipped", exc_info=True)

    async def _handle_persona_media_request(
        self, text: str, user_id_str: str, user_context: Dict[str, Any], chat_id: Any,
    ) -> Optional[str]:
        """Stage 0：人设「注册相册」（DB）——运营在后台预制的图/视频，按触发词命中即发。

        与 Stage A/B（生成）分工：这里发的是**已上传的现成媒体**（秒发、零出图成本、可含视频）。
        命中语义（见 ``persona_media.pick_media``）：
          - 条目带触发词且客户文本命中 → 精确发（**独立于**自拍/物体意图，如"跳个舞"→跳舞视频）；
          - 泛化「要照片/自拍」请求（``detect_selfie_request``）→ 额外放开无触发词的通用相册池。
        每条目可设 ``min_bond_level`` 关系闸门；命中即 ``record_hit`` + 会话内避重（不连发同一条）。
        返回 ""=媒体已发出（不再补文字）；None=未命中/未开/发送不可用（交 Stage A/B/文字兜底）。
        """
        scfg = self._selfie_cfg()
        if not scfg.get("enabled", False):
            return None
        # 人设级发图闸（2026-07-31，默认关）：人设没开相册 → 注册相册也不发。
        from src.companion.photo_capability import prompt_photos_allowed
        if not prompt_photos_allowed(user_context):
            return None
        try:
            from src.ai.companion_selfie import detect_selfie_request
            from src.companion.persona_media import caption_for, pick_media
            from src.companion.persona_media_store import get_persona_media_store
        except Exception:
            return None
        store = get_persona_media_store()
        if store is None:
            return None
        pid = str(user_context.get("account_persona_id")
                  or self._selfie_album_key(user_context) or "").strip()
        if not pid:
            return None
        generic_ok = bool(detect_selfie_request(text)) or \
            self._selfie_offer_accept_bridge(text, user_context, scfg)
        try:
            bond = int(self._bond_level_from_context(user_context, chat_id))
        except Exception:
            bond = None
        avoid = str(user_context.get("_persona_media_last") or "")
        # P0 一致性（与 B 线 pick_registered_media 同口径）：持久防复读账本 +
        # 重发冷却 + 时段过滤 + 服装连续窗 + 客户点名场景硬匹配。
        _conv_key = f"tg:{chat_id}"
        try:
            _rsd = float(scfg.get("resend_after_days", 90) or 0)
        except (TypeError, ValueError):
            _rsd = 90.0
        try:
            from src.inbox.image_autosend import resolve_consistency_cfg
            _cc = resolve_consistency_cfg(scfg)
        except Exception:
            _cc = {"now_hour": None, "resend_cooldown_hours": 0,
                   "continuity_minutes": 0}
        _scene_cls = ""
        try:
            from src.ai.companion_selfie import (
                extract_requested_scene, wants_same_scene,
            )
            from src.companion.persona_media import scene_class_of
            _req = extract_requested_scene(text)
            if not _req and wants_same_scene(text):
                _req = self._last_sent_media_scene(user_context)
            if _req:
                _scene_cls = scene_class_of(_req) or _req.strip().lower()
        except Exception:
            _scene_cls = ""
        # 实施69：对方点名想看的**非人像主体**（「烤串」；含悬置状态记住的
        # 1-N 轮前点名）→ 通用人像池关闭（keyword 触发条目不受影响——运营真
        # 给「烤串」配过触发词就照发）；主体可归场景类且本条没点名场景 →
        # 升为场景类硬匹配。与 B 线 pick_registered_media 的 deny_generic 同口径。
        try:
            from src.ai.outbound_promise_guard import wanted_media_subject
            _wsub = wanted_media_subject(
                text,
                [{"role": "assistant",
                  "content": str(user_context.get("last_reply") or "")}],
                generic_request=bool(generic_ok))
            if not _wsub:
                from src.ai.media_pending import pending_subject as _mp_s0
                _wsub = _mp_s0(user_context)
            if _wsub:
                from src.companion.persona_media import scene_class_of as _sco69
                _ws_cls = _sco69(_wsub)
                if _ws_cls and not _scene_cls:
                    _scene_cls = _ws_cls
                elif not _ws_cls:
                    generic_ok = False
        except Exception:
            pass
        # 实施90 季节/地点门（与 B 线 pick_registered_media 同口径）：
        # 人设「此刻季节 + 所在国」上下文，软失败=不设门。
        _geo90 = {"season": "", "country": ""}
        if _cc.get("season_gate") or _cc.get("place_gate"):
            try:
                from src.companion.media_taxonomy import persona_geo_context
                _geo90 = persona_geo_context(pid)
            except Exception:
                _geo90 = {"season": "", "country": ""}
        try:
            row = pick_media(store, pid, text, generic_ok=generic_ok,
                             avoid_id=avoid, bond_level=bond,
                             conv_key=_conv_key, resend_after_days=_rsd,
                             now_hour=_cc.get("now_hour"),
                             required_scene_class=_scene_cls,
                             resend_cooldown_hours=_cc.get(
                                 "resend_cooldown_hours", 0),
                             continuity_minutes=_cc.get(
                                 "continuity_minutes", 0),
                             no_resend=bool(_cc.get("no_resend")),
                             now_season=(str(_geo90.get("season") or "")
                                         if _cc.get("season_gate") else ""),
                             home_country=(str(_geo90.get("country") or "")
                                           if _cc.get("place_gate") else ""))
        except Exception:
            row = None
        if not row:
            return None
        mt = str(row.get("media_type") or "photo")
        # 多语配文：优先客户当前消息语种（与后续 reply_lang 同源检测器），回落
        # 上一轮 reply_lang。#143：统一走 _stage_lang（内含系统注入行剥离——
        # 贴纸/图片轮的 text 是中文识别标注，直接检测会把外语会话判成中文）。
        _lang = self._stage_lang(user_context, text)
        # 配文：条目多语配文 → 运营 caption_album（旧照口径）→ 双语 old-photo
        # 文案池。**不**回落全局 caption（那是「刚拍」口径——注册相册是备货旧照，
        # 「刚拍的～」配同一张图反复出现是实录穿帮点）。
        cap = caption_for(row, _lang, fallback="")
        # 时刻词守卫（2026-08-12 实锤）：固定配文写死「下午的阳光」这类现在时态
        # 时刻词，凌晨 3 点原样发出＝当场穿帮。冲突 → 弃固定配文回落中性文案。
        try:
            from src.companion.persona_media import caption_tod_conflict
            import datetime as _dt_cap
            if cap and caption_tod_conflict(cap, _dt_cap.datetime.now().hour):
                self.logger.info(
                    "[persona_media] 配文时刻词与当前小时冲突，回落中性配文 "
                    "pid=%s id=%s", pid, row.get("id"))
                cap = ""
        except Exception:
            pass
        _cap_from_pool = False
        if not cap:
            from src.ai.companion_selfie import selfie_stage_text
            cap = str(scfg.get("caption_album") or "") or selfie_stage_text(
                "caption_album", self._stage_lang(user_context, text),
                chat_key=str(chat_id))
            _cap_from_pool = not str(scfg.get("caption_album") or "")
        sent = await self._try_send_selfie_media(
            user_context, chat_id, str(row.get("file_path") or ""), cap,
            media_type=("video" if mt == "video" else "image"),
            media_url=str(row.get("url") or ""))
        if sent:
            # 池配文真发出后记近用账本（实施69）：A 线此前只选不记，同会话同日
            # crc32 种子恒定 → 逐字复读（实录 53 秒同句两遍）。
            if _cap_from_pool and cap:
                try:
                    from src.ai.companion_selfie import note_caption_used
                    note_caption_used(str(chat_id), cap)
                except Exception:
                    pass
            user_context["_persona_media_last"] = str(row.get("id"))
            try:
                store.record_hit(str(row.get("id")))
            except Exception:
                pass
            # 持久防复读账本（与 B 线同一张表）：同图同会话冷却/系列连续性据此判。
            # file_key＝文件名：文件系统相册链按文件名记账，不补这个键它就认不出
            # 「这张图刚发过」，24h 冷却会被绕过（2026-07-28 重复发图事故）。
            try:
                from pathlib import Path as _PathFK

                from src.companion.persona_media import series_of
                _fk = _PathFK(str(row.get("file_path")
                                  or row.get("url") or "")).name
                store.record_send(_conv_key, str(row.get("id")),
                                  persona_id=pid, series=series_of(row),
                                  file_key=_fk)
            except Exception:
                pass
            self.logger.info(
                "[persona_media] 已发注册相册媒体 pid=%s type=%s id=%s",
                pid, mt, row.get("id"))
            # 媒体轮回写：下一轮 LLM 要知道"我刚发过图/视频+配文"；场景记条目
            # scene:* 标签（「跟上次一样的」复刻据此指对场景）。
            _tag = "[视频] " if mt == "video" else "[图片] "
            user_context["_stage_media_note"] = (_tag + (cap or "")).strip()
            try:
                from src.companion.persona_media import row_scene_class
                user_context["_stage_media_scene"] = row_scene_class(row)
            except Exception:
                pass
            return ""  # 媒体已发出，短路（不再补文字）
        return None  # 发送不可用 → 交 Stage A/B/文字

    _MEDIA_COMPLAINT_WINDOW_SEC = 24 * 3600.0

    def _maybe_flag_media_complaint(
        self, text: str, user_context: Dict[str, Any],
    ) -> None:
        """收图后「质疑」信号（P0 观测补盲 + 回应纠偏，A/B 线共用）。

        最近窗口内真发过媒体（``_media_sent_log``）且本条入站像在质疑图
        （重复/不像本人/假图）→ ①计数进 autosend 观测（图文一致性劣化的第一
        现场，此前只能人工翻聊天记录）②设回应纠偏 hint（别争辩、别坚称真实、
        别马上再发一张——那是二次穿帮的标准路径）。纯启发式，软失败零阻断。
        """
        try:
            from src.ai.companion_selfie import (
                detect_apology_spiral, detect_media_complaint,
            )
            kind = detect_media_complaint(text)
            if not kind:
                return
            # 采信闸：图文质疑（repeat/not_you/fake/content_mismatch）须最近真发
            # 过媒体（压误报）；lie_caught/distrust（没收到/说话不算话/失望）本就
            # 没媒体台账 → 不设该闸。
            if kind in ("repeat", "not_you", "fake", "content_mismatch"):
                log = user_context.get("_media_sent_log")
                if not isinstance(log, list) or not log:
                    return
                last_ts = float((log[-1] or {}).get("ts") or 0)
                if (time.time() - last_ts) > self._MEDIA_COMPLAINT_WINDOW_SEC:
                    return
            try:
                from src.inbox.image_autosend import record_media_complaint
                record_media_complaint(
                    f"{kind}:{str(text or '')[:60]}", kind=kind,
                    persona_id=self._selfie_album_key(user_context))
            except Exception:
                pass
            # unfulfilled（实施69）：「照片呢/图呢」是催兑现不是抓包——只计数不设
            # 纠偏 hint：措辞由悬置常驻 hint（_media_pending_hint，带主体与催促
            # 升级）负责；这里再占 _media_coherence_hint 会把 Stage B 物体图链
            # 误关（该键兼作防重烧标志），且「止损认错」语气对首次催促也过重。
            if kind == "unfulfilled":
                self.logger.info("[media_complaint] kind=unfulfilled text=%r",
                                 str(text or "")[:80])
                return
            # Case-center：媒体质疑=穿帮风险第一现场，开案给人跟进（A/B 线共用）。
            # 显式传 mid（P4）：open_case 虽能自吸 user_msg_id，但媒体投诉常在
            # 上下文被多轮覆写后触发——显式 resolve 保证锚到**本条质疑**气泡。
            try:
                from src.utils.case_center import open_case, resolve_inbound_mid
                open_case(user_context,
                          str(user_context.get("user_id") or ""),
                          "media_complaint", f"case.reason.media_{kind}",
                          quote=text,
                          mid=resolve_inbound_mid(user_context))
            except Exception:
                pass
            # 上一轮 AI 是否已在道歉/解释 → 升级为「停止解释」纠偏（防连环圆场）。
            _spiral = detect_apology_spiral(
                str(user_context.get("last_reply") or ""))
            if kind in ("lie_caught", "distrust") or _spiral:
                # 被抓包/失望/已在螺旋：核心是**止损**——认错要短、别讲来龙去脉。
                # 实施69 补⑤：实录 23:07 该 hint 已注入，LLM 仍编出「信号不好照片
                # 卡在半道」——旧文只禁「新承诺」没禁「谎称已发/传输借口」，明令补齐
                # （硬地板在 claim 剥离，这里是源头减产）。
                user_context["_media_coherence_hint"] = (
                    "注意：对方在质疑你说话不算话/没收到你说的东西/对你失望。"
                    "回应铁律：①只回一两句，绝不长篇解释来龙去脉、时间线、找借口"
                    "（越解释越假）；②别再道歉连篇，最多轻轻认一句（『嗯是我不好啦』）；"
                    "③绝不做新的承诺（不要再说『这就发/马上拍/明天给你』）；"
                    "④绝不要说『我发过了/已经发了/可能信号不好没传出去/卡在半道』"
                    "这类已发断言和传输借口——对方手机里没有就是没有，越编越像骗子；"
                    "⑤用人设口吻把话题轻轻带开或反过来关心对方。像真人被戳穿时"
                    "大方一笑带过，而不是慌张辩解。")
            elif kind == "content_mismatch":
                # 实施69：质疑的是**照片内容与你说的对不上**（「照片里没有X/背景
                # 不对」）——最忌把它听成「没收到」然后编传输借口（实录 23:07 就是
                # 这么把小疑点滚成连环谎的）。
                user_context["_media_coherence_hint"] = (
                    "注意：对方在说你刚发的照片内容和你说的对不上（比如说好的"
                    "场景/东西在图里看不到）。绝不要解释成『没传出去/信号不好』，"
                    "也不要赌咒发誓或再承诺重拍；只回一两句，如实轻松带过"
                    "（比如角度没拍到、下次拍全点），把话题引回对方身上。")
            else:
                desc = {"repeat": "觉得这张图之前发过/重复了",
                        "not_you": "觉得图里的人不像你",
                        "fake": "觉得图是假的/网图/生成的"}.get(kind, "对图有质疑")
                user_context["_media_coherence_hint"] = (
                    f"注意：对方似乎在质疑你刚发的照片（{desc}）。不要争辩，"
                    "不要赌咒发誓说照片绝对真实，也不要立刻承诺再拍/再发一张；"
                    "只回一两句，别长篇解释；用人设口吻自然轻松地回应（可以撒娇带过、"
                    "坦然一点、把话题引回对方身上），绝不要重复发同样的照片。")
            self.logger.info("[media_complaint] kind=%s spiral=%s text=%r",
                             kind, _spiral, str(text or "")[:80])
        except Exception:
            self.logger.debug("media complaint check skipped", exc_info=True)

    def _set_cant_send_photo_hint(self, user_context: Dict[str, Any]) -> None:
        """「要图但这轮发不出」→ 注入发图协同提示后交回普通 LLM 回复。

        2026-07-15 复盘（A2）：Stage A 出图失败此前直接 return 固定模板——绕过
        LLM 也绕过全部质量机制（自称"林小雨"、一字不差复读、吞掉用户原话题
        "吃饭了吗"三连穿帮同源于此）。对齐 Stage B 的既有设计：设 hint + 返回
        None，让 LLM 带着约束自然回应（回答原话题 + 婉转带过拍照不便）；
        LLM 也不可用时按全链失败纪律本轮不回复（2026-08-15 罐头兜底已移除）。
        """
        user_context["_media_coherence_hint"] = (
            "对方想要照片/图片，但这一轮系统发不出任何照片。回复时不要答应"
            "「等我拍/马上发你」，也不要说「已经发了」；先自然回应对方话里的"
            "其它内容（比如对方的问题要答），再用人设口吻婉转带过这会儿不方便"
            "拍照（可以聊别的），不要否认你能拍照这件事，也不要过度道歉。")

    async def _handle_selfie_request(
        self, text: str, user_id_str: str, user_context: Dict[str, Any], chat_id: Any,
    ) -> Optional[str]:
        """Stage A：处理「给我看看你/发张自拍」——按关系等级 + 付费权益判准入。

        返回字符串=短路（搪塞/付费引导/出图后的配文/兜底文字）；None=非自拍请求或功能未开。
        关系浅→温柔搪塞；gate 开+未拥有 exclusive_album+免费额度用尽→软付费引导（驱动解锁）；
        准入→provider 出图（默认 disabled 则退回文字陪伴），有受管媒体 worker 时经编排器发出。
        """
        scfg = self._selfie_cfg()
        if not scfg.get("enabled", False):
            return None
        # 人设级发图闸（2026-07-31，默认关）：人设没开 → 不搪塞不引导，直接交
        # 普通文字回复（prompt 层已注入「无发图能力」硬约束，语言自然带过）。
        from src.companion.photo_capability import prompt_photos_allowed
        if not prompt_photos_allowed(user_context):
            return None
        from src.ai.companion_selfie import (
            build_selfie_prompt,
            decide_selfie,
            detect_selfie_request,
            get_selfie_provider,
            resolve_persona_lora,
            resolve_variety_salt,
            stable_selfie_seed,
        )
        if not detect_selfie_request(text) and not \
                self._selfie_offer_accept_bridge(text, user_context, scfg):
            return None
        # P1「无货不发」补接 A 线（实施69——此前只有 B 线 autosend 消费该判定，
        # 而 auto_ai 全自动会话恰恰走 A 线）：对方点名想看的**非人像主体**
        # （「烤串/大腰子」；含悬置状态记住的 1-N 轮前点名），能归场景类的
        # （「卧室」→bedroom）转场景硬匹配继续走自拍链；归不到的（食物/物件）
        # 在相册/禁用后端下**不发图**——Stage 0 注册相册触发词已在前面试过
        # （走到 Stage A=真无货），拿通用自拍顶包=实录穿帮。真出图后端则让路
        # 给 Stage B 物体图链按主体生成。
        _wanted_subject = ""
        _wanted_scene_cls = ""
        try:
            from src.ai.outbound_promise_guard import wanted_media_subject
            _wanted_subject = wanted_media_subject(
                text,
                [{"role": "assistant",
                  "content": str(user_context.get("last_reply") or "")}],
                generic_request=True)
            if not _wanted_subject:
                from src.ai.media_pending import pending_subject as _mp_subj2
                _wanted_subject = _mp_subj2(user_context)
            if _wanted_subject:
                from src.companion.persona_media import scene_class_of
                _wanted_scene_cls = scene_class_of(_wanted_subject)
        except Exception:
            _wanted_subject, _wanted_scene_cls = "", ""
        if _wanted_subject and not _wanted_scene_cls:
            _bk0 = str(((scfg.get("provider") or {}).get("backend"))
                       or "").lower()
            if _bk0 in ("", "disabled", "album"):
                try:
                    from src.inbox.image_autosend import record_image_fallback
                    record_image_fallback("wanted_subject_no_stock",
                                          detail=str(_wanted_subject)[:40])
                except Exception:
                    pass
                self.logger.info(
                    "[selfie] 对方想看「%s」而相册后端无对应货 → 不发图交诚实"
                    "文字（A 线，实施69）", _wanted_subject)
                user_context["_media_coherence_hint"] = (
                    f"对方想看「{_wanted_subject}」的照片，但你这一轮发不出"
                    "这样的照片。不要答应「等我拍/马上发」，也不要说「已经发了/"
                    "信号不好没传出去」；用人设口吻自然给个缘由（比如这会儿腾不"
                    f"出手拍），可以多聊聊「{_wanted_subject}」本身把话题接住，"
                    "不要否认你能拍照，也不要过度道歉。")
                return None
            return None  # 真出图后端：交 Stage B 物体图按主体生成，不发人像顶包
        persona_name = self._get_persona_name_for_context(user_context) or "我"
        # Stage 文案语言对齐：搪塞/兜底/配文按会话语言出（英文会话不再蹦中文）。
        from src.ai.companion_selfie import selfie_stage_text
        _lang = self._stage_lang(user_context, text)
        # 免费额度按天计（仅 gate 开且未拥有相册时才消耗；拥有者/不计费时不限）
        today = time.strftime("%Y%m%d")
        if user_context.get("_selfie_date") != today:
            user_context["_selfie_date"] = today
            user_context["_selfie_used"] = 0
        free_used = int(user_context.get("_selfie_used") or 0)
        # 权益：复用已懒解析的 entitlement，否则即时解析（best-effort）
        ent = user_context.get("entitlement")
        if not isinstance(ent, dict):
            try:
                from src.utils.companion_context import resolve_entitlement
                ent = resolve_entitlement(user_id_str)
            except Exception:
                ent = None
        decision = decide_selfie(
            entitlement=ent if isinstance(ent, dict) else None,
            gate_enabled=self._monetization_gate_enabled(),
            free_used=free_used,
            free_daily=int(scfg.get("free_daily", 1) or 0),
            bond_level=self._bond_level_from_context(user_context, chat_id),
            min_bond_level=int(scfg.get("min_bond_level", 2) or 0),
        )
        action = decision.get("action")
        if action == "too_soon":
            self._record_selfie_event(user_id_str, "too_soon")
            return selfie_stage_text("too_soon", _lang, persona_name=persona_name)
        if action == "locked":
            self._record_selfie_event(user_id_str, "locked")
            return self._selfie_upsell_text(ent, persona_name, lang=_lang)
        # action == allow：尝试出图（默认 disabled → 退回文字）
        provider = get_selfie_provider(scfg.get("provider") or {})
        will_generate = bool(getattr(provider, "enabled", False)) and \
            str(getattr(provider, "backend", "")).lower() not in ("", "disabled")
        cap = int(scfg.get("daily_global_cap", 0) or 0)
        if will_generate and cap > 0 and self._get_selfie_cap(cap).would_exceed(1):
            # 全局出图预算用尽：优雅兜底——不记 delivered、不消耗用户免费额度，护住出图 API 账单。
            # A2：交 LLM 带提示自然回应（回答原话题+婉转带过），不再模板顶掉回复。
            self.logger.info("selfie daily_global_cap=%d 已达上限，软兜底", cap)
            self._record_selfie_event(user_id_str, "capped")
            self._set_cant_send_photo_hint(user_context)
            return None
        self._record_selfie_event(user_id_str, "delivered")
        _sp = self._selfie_persona_for_prompt(user_context)
        # 场景（Phase18 优先级）：客户显式点名（"发张你在海边的"）→「跟上次一样」
        # 取已发媒体日志上次场景 → 场景状态轮换（resolve_current_scene，与聊天
        # prompt 注入、autosend 链同一事实源——图文同源防打脸）。
        from src.ai.companion_selfie import (
            extract_requested_scene,
            resolve_current_scene,
            wants_same_scene,
        )
        _scene = extract_requested_scene(text)
        if not _scene and wants_same_scene(text):
            _scene = self._last_sent_media_scene(user_context)
        # 实施69：主体可归场景类（「卧室」→bedroom，含悬置记忆里 1-N 轮前点名
        # 的）→ 视同点名场景（本条文本抽不出时的跨轮兜底）。
        if not _scene and _wanted_scene_cls:
            _scene = _wanted_scene_cls
        # P0 一致性：点名/复刻上次＝**说出口的场景**→硬要求（相册兜底必须同场景
        # 类，挑不到如实回落文字，绝不发不相干场景的图）；轮换场景只是默认值不加硬。
        _scene_strict = bool(str(_scene or "").strip())
        # 叙事自称场景（实施69 P2）：上一轮 AI 亲口说「我在夜市摊」→ 泛化要图的
        # 软偏好优先贴它（客户听到的现场）而非轮换值；上一轮距今 >30min 的旧
        # 叙事不采（场景多半已翻篇），键缺失按新鲜放行（软偏好误差代价低）。
        _narrative_scene = ""
        try:
            _gap69 = float(user_context.get("_turn_gap_sec") or 0)
            if _gap69 <= 1800:
                from src.ai.companion_selfie import extract_self_claimed_scene
                _narrative_scene = extract_self_claimed_scene(
                    str(user_context.get("last_reply") or ""))
        except Exception:
            _narrative_scene = ""
        # 天气快照（2026-08-18）：轮换池滤天气冲突 + 生图 prompt 强天气氛围
        # （复用时空接地已解析的快照，零额外请求；不满足闸门/无此绑定的轻量
        # 调用方 → None=旧行为）。
        try:
            _wxsnap = self._selfie_weather_snap(user_context)
        except Exception:
            _wxsnap = None
        if not _scene:
            try:
                from src.companion.persona_location import resolve_persona_now
                _scene = resolve_current_scene(
                    _sp, scfg, now=resolve_persona_now(_sp),
                    weather_snap=_wxsnap)
            except Exception:
                _scene = resolve_current_scene(_sp, scfg, weather_snap=_wxsnap)
        # album 后端按人设分册挑图 + 尽量避开上一张（连发不重复）；其它后端忽略这两参。
        _album_key = self._selfie_album_key(user_context)
        # 防复读账本（2026-07-22）：该会话收过的相册文件（含系列排除，见
        # _pick_from_album 分层回落）。A 线会话键 tg:<chat_id>，与 autosend 的
        # platform:acct:peer 空间天然不冲突。
        _conv_key = f"tg:{chat_id}"
        _sent_files: Any = None
        _series_pref = ""
        # 一致性护栏（P0，与 B 线 stage_image_file 同口径）：时段过滤 + 服装连续窗。
        try:
            from src.inbox.image_autosend import resolve_consistency_cfg
            _cc = resolve_consistency_cfg(scfg)
        except Exception:
            _cc = {"now_hour": None, "continuity_minutes": 0}
        try:
            from src.companion.persona_media_store import get_persona_media_store
            _pms = get_persona_media_store()
            if _pms is not None:
                try:
                    _rsd = float(scfg.get("resend_after_days", 90) or 0)
                except (TypeError, ValueError):
                    _rsd = 90.0
                _hist = _pms.sent_history(_conv_key, max_age_days=_rsd)
                # 排除面必须并上 file_keys：注册相册链按 DB uuid 记账，只拿 ids
                # 比对文件名永远不命中 → 同一张图会被"没发过"地再发一次。
                _sent_files = (set(_hist.get("ids") or ())
                               | set(_hist.get("file_keys") or ())) or None
                # 服装连续性：连续窗内最近一次发过的系列 → 相册优先同系列
                # （半小时前海边连衣裙、现在居家睡衣的「瞬移换装」收口）。
                _cont_min = float(_cc.get("continuity_minutes") or 0)
                if _cont_min > 0:
                    _now_ts = time.time()
                    _best_ts = 0.0
                    for _it in (_hist.get("items") or []):
                        _its = float(_it.get("ts") or 0)
                        if (_it.get("series") and _its > _best_ts
                                and (_now_ts - _its) <= _cont_min * 60.0):
                            _best_ts = _its
                            _series_pref = str(_it.get("series"))
        except Exception:
            _sent_files = None
        # 今日衣着状态（P1）：连续窗系列（账本→媒体日志兜底）跟随刚发照片，
        # 否则「今天穿什么」确定性取值——生图 prompt 与聊天状态块同源。
        # P2：传本次场景做 场景×季节 适配（泳装进咖啡馆/健身房穿西装收口）。
        _outfit = ""
        try:
            from src.companion.outfit_state import current_outfit
            _rec_series = _series_pref or self._last_sent_media_series(
                user_context, within_minutes=float(
                    _cc.get("continuity_minutes") or 0))
            _outfit = current_outfit(
                _sp, scfg, persona_key=_album_key,
                recent_series=_rec_series, scene=_scene)["outfit"]
        except Exception:
            _outfit = ""
        # 多样性 salt（治千篇一律，默认关）：开时每次取随机 salt → 姿态/表情/构图各异。
        _vsalt = resolve_variety_salt(scfg)
        _lora = resolve_persona_lora(_sp, scfg)   # per-persona 角色 LoRA spec
        # 时段光线兜底（P1-2，Phase19 语义）：场景没带时间词 → 按当前小时补光线
        # 氛围（凌晨要图不出正午烈日照）。只作用于生图 prompt——相册硬匹配
        # （album_scene）仍用原始场景短语，词表零耦合。
        from src.ai.companion_selfie import ensure_time_of_day
        prompt = build_selfie_prompt(
            _sp,
            scene_hint=ensure_time_of_day(_scene, weather_snap=_wxsnap),
            style=str(scfg.get("style") or ""),
            default_appearance=str(scfg.get("appearance") or ""),
            content_rating=str(scfg.get("content_rating") or ""),
            variety_salt=_vsalt,
            lora_trigger=_lora["trigger"],
            outfit=_outfit,
        )
        caption = str(scfg.get("caption") or "") or selfie_stage_text(
            "caption", _lang, persona_name=persona_name,
            chat_key=str(chat_id))
        # 配文是否出自双语池（实施69）：池选取有 crc32(会话+日期) 确定性种子，
        # 发出后**必须**记近用账本才会避重——A 线此前只传 chat_key 从不记账，
        # 同会话同日永远选中同一条（实录 53 秒逐字复读）。运营配置串不记
        # （与 B 线 _note_caption_sent 同口径：账本只服务池指纹）。
        _cap_from_pool = not str(scfg.get("caption") or "")
        if will_generate and cap > 0:
            self._get_selfie_cap(cap).record_sent(1)
        _avoid = str(user_context.get("_selfie_last_img") or "")
        # openai/command 后端：拿相册里一张当"锁脸基础图"(img2img)，让生成的自拍保持同一张脸。
        _base = ""
        try:
            if str(getattr(provider, "backend", "")).lower() in ("openai", "command"):
                _base = provider.reference_image(_album_key)
        except Exception:
            _base = ""
        # 固定种子（按人设派生，与 autosend 链同口径）：同一人设自拍外观漂移最小化。
        # 开 variety 时掺 salt → 每次底噪不同（构图各异，身份靠 PuLID/角色 LoRA）。
        _seed = stable_selfie_seed(_album_key, salt=(_vsalt or 0)) if bool(
            scfg.get("stable_seed", True)) else -1
        self.logger.info("[selfie] prompt=%r seed=%s base=%s", prompt, _seed, bool(_base))
        try:
            # 出图自检闸门（与 autosend 链同口径）：VLM 体检不合格换种子重试→回落文字。
            from src.ai.image_gate import generate_with_gate, resolve_gate_cfg
            _root_cfg = self.config.config if hasattr(self.config, "config") else {}
            res = await generate_with_gate(
                provider, prompt, persona=_sp,
                root_config=_root_cfg if isinstance(_root_cfg, dict) else {},
                gate_cfg=resolve_gate_cfg(scfg), seed=_seed,
                expect_scene=(_scene if _scene_strict else ""),
                expect_hour=_cc.get("now_hour"),
                album_key=_album_key, avoid_path=_avoid, base_image=_base,
                lora=_lora["file"], lora_weight=_lora["weight"],
                exclude_paths=_sent_files,
                album_scene=(_scene if _scene_strict else ""),
                now_hour=_cc.get("now_hour"),
                prefer_series=_series_pref,
                # 实施69 P1/P2：未点名场景的泛化要图 → 软偏好贴相册存货，优先级
                # ＝**叙事自称场景**（AI 上一轮亲口说「我在夜市」——客户听到的
                # 现场）＞ 轮换场景；上一轮距今 >30min 叙事视为过期不采。
                prefer_scene=("" if _scene_strict
                              else (_narrative_scene or _scene)))
        except Exception:
            res = None
            self.logger.debug("selfie generate error", exc_info=True)
        if res is not None and getattr(res, "ok", False):
            user_context["_selfie_last_img"] = getattr(res, "image_path", "") or ""
            _extra = dict(getattr(res, "extra", {}) or {})
            # provider=album＝相册来源（后端直选/生成失败兜底两路都是；
            # fallback_from 记的是失败的主后端名，不能用它判相册）。
            _from_album = (str(getattr(res, "provider", "")) == "album")
            # P0 配文口径对齐：相册图（后端直选/生成失败兜底）＝「之前拍的」——
            # 「刚拍的给你看～」配旧图是实录穿帮点（同图两次都"刚拍"）。运营
            # 显式配 caption_album 优先，否则用双语 old-photo 文案池。
            if _from_album:
                caption = str(scfg.get("caption_album") or "") or selfie_stage_text(
                    "caption_album", _lang, persona_name=persona_name,
                    chat_key=str(chat_id))
                _cap_from_pool = not str(scfg.get("caption_album") or "")
            sent = await self._try_send_selfie_media(
                user_context, chat_id, res.image_path, caption)
            if sent:
                # 池配文真发出后记近用账本（实施69，与 B 线同口径）：下一次
                # pick_caption 才会避开这条，修同会话同日逐字复读。
                if _cap_from_pool and caption:
                    try:
                        from src.ai.companion_selfie import note_caption_used
                        note_caption_used(str(chat_id), caption)
                    except Exception:
                        pass
                # 防复读账本：相册图记文件名（相册文件不在注册 DB，无 media_id）；
                # 生成图不记（每次都是新图，账本排除无意义）。
                try:
                    if _from_album:
                        from pathlib import Path as _P

                        from src.ai.companion_selfie import album_series_of_path
                        from src.companion.persona_media_store import (
                            get_persona_media_store as _gpms,
                        )
                        _st2 = _gpms()
                        if _st2 is not None:
                            _fn = _P(str(res.image_path)).name
                            _st2.record_send(
                                _conv_key, _fn,
                                persona_id=str(_album_key or ""),
                                series=(str(_extra.get("series") or "")
                                        or album_series_of_path(res.image_path)),
                                file_key=_fn)
                except Exception:
                    pass
                # 只在客户真收到图时才消耗免费额度（生成成功但没送达不扣）
                if decision.get("used_free"):
                    user_context["_selfie_used"] = free_used + 1
                # 媒体轮回写：下一轮 LLM 要知道"我刚发过一张照片+配文"
                user_context["_stage_media_note"] = "[图片] " + caption
                # 场景以实际产出为准：相册图记条目场景类（可能≠轮换场景），
                # 「跟上次一样的」复刻才不会指错场景。
                user_context["_stage_media_scene"] = (
                    str(_extra.get("scene_class") or "") or _scene)
                # 衣着系列以实际产出为准（P1）：相册图记条目系列，生成图记
                # 本次注入衣着的 slug——媒体日志连续窗（跨 A/B）据此跟装。
                try:
                    from src.companion.outfit_state import outfit_slug
                    if _from_album:
                        from src.ai.companion_selfie import album_series_of_path
                        user_context["_stage_media_series"] = (
                            str(_extra.get("series") or "")
                            or album_series_of_path(res.image_path))
                    else:
                        user_context["_stage_media_series"] = outfit_slug(_outfit)
                except Exception:
                    pass
                # 点名场景兑现计数（P1 补货需求侧）
                if _scene_strict:
                    try:
                        from src.inbox.image_autosend import record_scene_request
                        record_scene_request(_scene, unmet=False)
                    except Exception:
                        pass
                return ""  # 媒体已发出，无需再发文字（空串=已处理、不再生成普通回复）
            # 出图成功但发送失败/无媒体通道 → 绝不能把「这是刚拍的，给你看～」
            # 这类"图已到手"配文当文字发出去（图根本没到，实录谎言事故）。
            # A2：设「发不出图」提示后交 LLM 自然回应（与 Stage B 同款设计）。
            try:
                from src.inbox.image_autosend import record_image_fallback
                record_image_fallback("a_line_send_failed")
                if _scene_strict:
                    from src.inbox.image_autosend import record_scene_request
                    record_scene_request(_scene, unmet=True)
            except Exception:
                pass
            self.logger.info(
                "[selfie] 出图成功但发送失败（无可用媒体通道）platform=%s "
                "account_id=%s img=%s",
                user_context.get("platform"), user_context.get("account_id"),
                getattr(res, "image_path", ""))
            self._set_cant_send_photo_hint(user_context)
            return None
        # provider 未配/失败 → 优雅退回文字陪伴（不报错给用户）。
        # 客户没收到图 → 不消耗免费额度（额度只在真送达时扣）。
        # A2：同样交 LLM 带提示回应；模板不再顶掉用户的原话题。
        self.logger.info(
            "[selfie] 出图失败回落文字 error=%s provider=%s",
            getattr(res, "error", None) if res is not None else "generate_exception",
            getattr(res, "provider", "") if res is not None else "")
        if _scene_strict:
            try:
                from src.inbox.image_autosend import record_scene_request
                record_scene_request(_scene, unmet=True)
            except Exception:
                pass
        self._set_cant_send_photo_hint(user_context)
        return None

    async def _handle_contextual_image_request(
        self, text: str, user_id_str: str, user_context: Dict[str, Any], chat_id: Any,
    ) -> Optional[str]:
        """Stage B：对话上下文「按需生图」——对方要"你煮的面"这类**对话里提到的东西**的照片。

        与自拍(Stage A)分工：这里只处理物体/场景图（非人设肖像）。仅在开 ``contextual_images``
        且接了**真出图后端**(openai/command——album 无法凭空生成任意图)时生效；否则返回 None，
        交由普通回复自然带过（不硬答）。返回 ""=图已发出不再补文字；None=非要图/未开/无后端/失败。
        """
        scfg = self._selfie_cfg()
        if not scfg.get("enabled", False) or not scfg.get("contextual_images", False):
            return None
        # 人设级发图闸（2026-07-31，默认关）：人设没开 → 物体图也不生成。
        from src.companion.photo_capability import prompt_photos_allowed
        if not prompt_photos_allowed(user_context):
            return None
        # A2 防重复烧卡：Stage A 已在本轮判定「出不了图」并设了协同提示 →
        # 别再走一次昂贵的生成/失败（同一轮两次 ComfyUI 调用 + 两次失败）。
        if user_context.get("_media_coherence_hint"):
            return None
        from src.ai.companion_selfie import get_selfie_provider, selfie_stage_text
        from src.ai.contextual_image import (
            build_llm_prompt_refine_instruction,
            plan_contextual_image,
        )
        history = user_context.get("_conversation_history") or []
        plan = plan_contextual_image(text, history, style=str(scfg.get("style") or ""))
        if not plan:
            return None

        def _cant_send_hint() -> None:
            """要图但这轮发不出（无后端/关系浅/预算尽/生成失败）→ 复用共享提示。"""
            self._set_cant_send_photo_hint(user_context)

        provider = get_selfie_provider(scfg.get("provider") or {})
        will_generate = bool(getattr(provider, "enabled", False)) and \
            str(getattr(provider, "backend", "")).lower() not in ("", "disabled", "album")
        if not will_generate:
            # 没接真出图后端(或只有 album 预制相册)：无法凭空生成"你煮的面" → 交普通文字回复。
            _cant_send_hint()
            return None
        try:
            bond = int(self._bond_level_from_context(user_context))
        except Exception:
            bond = 0
        if bond < int(scfg.get("min_bond_level", 0) or 0):
            _cant_send_hint()
            return None
        # 全局出图预算 cap（护 API 账单，与自拍共用同一份跟踪器）。
        cap = int(scfg.get("daily_global_cap", 0) or 0)
        if cap > 0 and self._get_selfie_cap(cap).would_exceed(1):
            self.logger.info("contextual image daily_global_cap=%d 已达上限，软兜底", cap)
            _cant_send_hint()
            return None
        prompt = str(plan.get("prompt") or "")
        # 可选：用 LLM 把 prompt 提炼得更准（heuristic 抽主体不稳时）。失败/超时回落 heuristic。
        if scfg.get("contextual_images_llm_prompt", False) and getattr(self, "ai_client", None):
            try:
                instruction = build_llm_prompt_refine_instruction(text, history)
                refined = str(await self.ai_client.chat(instruction) or "").strip().strip('"').strip()
                if refined and len(refined) <= 400:
                    prompt = refined
            except Exception:
                self.logger.debug("contextual prompt LLM refine skipped", exc_info=True)
        if not prompt.strip():
            _cant_send_hint()
            return None
        if cap > 0:
            self._get_selfie_cap(cap).record_sent(1)
        caption = str(scfg.get("contextual_caption") or "") or selfie_stage_text(
            "caption_object", self._stage_lang(user_context, text),
            chat_key=str(chat_id))
        try:
            # 物体图走 text2img，不带人设的脸。P0：生成失败**绝不回落相册人像**
            # （要面条/海景发车内自拍=实录「你真会骗人」事故）——如实失败回落文字。
            res = await provider.generate(prompt, allow_album_fallback=False)
        except Exception:
            res = None
            self.logger.debug("contextual image generate error", exc_info=True)
        if res is not None and getattr(res, "ok", False):
            sent = await self._try_send_selfie_media(
                user_context, chat_id, res.image_path, caption)
            if sent:
                # 媒体轮回写：下一轮 LLM 要知道"我刚发过一张照片+配文"
                user_context["_stage_media_note"] = "[图片] " + caption
                return ""  # 图已发出，不再补文字
        _cant_send_hint()
        return None  # 出图/发送失败 → 交普通回复自然带过（带「别承诺」提示）

    def _selfie_album_key(self, user_context: Dict[str, Any]) -> str:
        """album 后端分册键：多人设时用 persona id/name 选 ``album_dir/<key>`` 子目录；缺则空（用根目录）。

        2026-07-22：会话未绑人设时回落 ``selfie.default_album_key``（单人设部署的
        默认相册）——真机实录：TG 私聊未绑人设 → album_key 空 → 相册根目录无图 →
        锁脸基础图与相册兜底双双落空，客户要图永远只能收到文字婉拒。
        """
        key = ""
        try:
            p = self._selfie_persona_for_prompt(user_context)
            if isinstance(p, dict):
                key = str(p.get("id") or p.get("persona_id") or p.get("name") or "").strip()
            else:
                key = str(p or "").strip()
        except Exception:
            key = ""
        if not key:
            try:
                key = str(self._selfie_cfg().get("default_album_key") or "").strip()
            except Exception:
                key = ""
        return key

    def _selfie_persona_for_prompt(self, user_context: Dict[str, Any]) -> Any:
        """取出图用 persona（dict 含 name/appearance 等）；拿不到则回 name 字符串/空。"""
        try:
            persona_id = (user_context or {}).get("account_persona_id") or ""
            if persona_id:
                from src.utils.persona_manager import PersonaManager
                p = PersonaManager.get_instance().get_persona_by_id(str(persona_id))
                if isinstance(p, dict):
                    return p
        except Exception:
            pass
        return self._get_persona_name_for_context(user_context)

    def _selfie_upsell_text(
        self, entitlement: Any, persona_name: str, *, lang: str = "",
    ) -> str:
        """付费相册软引导（不硬推销，贴人设）。复用 monetization.upsell_*。
        lead/兜底句按会话语言取（upsell_pitch_hint 仍为中文，P1 待 i18n）。"""
        from src.ai.companion_selfie import SELFIE_FEATURE, selfie_stage_text
        try:
            from src.utils.monetization import (
                merge_catalog,
                upsell_offer,
                upsell_pitch_hint,
            )
            cfg = self.config.config if hasattr(self.config, "config") else {}
            mon = (cfg.get("monetization") or {}) if isinstance(cfg, dict) else {}
            catalog = merge_catalog(mon.get("catalog"))
            offer = upsell_offer(
                entitlement if isinstance(entitlement, dict) else None,
                SELFIE_FEATURE, catalog=catalog, gate_enabled=True)
            hint = upsell_pitch_hint(offer, persona_name=persona_name)
        except Exception:
            hint = ""
        lead = selfie_stage_text("upsell_lead", lang, persona_name=persona_name)
        return (lead + hint) if hint else (
            lead + selfie_stage_text(
                "upsell_fallback", lang, persona_name=persona_name))

    def _stage_lang(self, user_context: Dict[str, Any], text: str) -> str:
        """Stage 短路文案的语言判定：当前消息语种（与 reply_lang 同源检测器）→
        上一轮 reply_lang → ''（=中文默认）。

        #143（0902，接 #74）：先剥系统注入行再检测——贴纸/图片轮的 text 是
        「[贴纸内容]/[图片内容]/[表情] 中文标注」，直接喂检测会把英文会话的
        媒体轮判成中文 → 配文/搪塞文案全落中文池。剥空（纯媒体轮）→ 回落
        会话级 reply_lang（其证据链早已豁免媒体行）。
        """
        _lang = ""
        try:
            from src.ai.lang_policy import strip_system_injected as _ssi
            _lang_src = _ssi(text)
        except Exception:
            _lang_src = str(text or "")
        try:
            if _lang_src.strip() and hasattr(
                    self.ai_client, "_detect_message_language"):
                _lang = self.ai_client._detect_message_language(_lang_src) or ""
        except Exception:
            _lang = ""
        return _lang or str(user_context.get("reply_lang") or "")

    def _record_stage_turn(
        self, user_context: Dict[str, Any], text: str, reply_note: str,
    ) -> None:
        """媒体/搪塞短路轮的最小状态回写（修「发图后失忆」）。

        Stage 0/A/B/C 短路 return 后不经 ``_update_after_reply`` 与历史窗口逻辑，
        ``last_message``/``last_reply`` 停在上一轮 → 下一轮 LLM 完全不知道自己刚
        发过图/说过什么（用户："这张照片真好看" → AI："什么照片？"）。这里把本轮
        (用户消息, "[图片] 配文"/搪塞文字) 写进 last_* 字段，复用既有「下一轮把
        上一轮补录进 _conversation_history」机制，媒体轮从此进入上下文窗口与
        「关键信息锚定」块。reply_note 为空时取 ``_stage_media_note``（媒体成功
        路径由 handler 预置，如 "[图片] 这是刚拍的…"）；媒体轮同时落
        ``_media_sent_log``（Phase18 已发媒体日志，供"上次那张"指涉与 prompt 注入）。
        """
        try:
            media_note = str(
                user_context.pop("_stage_media_note", "") or "").strip()
            media_scene = str(
                user_context.pop("_stage_media_scene", "") or "").strip()
            media_series = str(
                user_context.pop("_stage_media_series", "") or "").strip()
            note = str(reply_note or "").strip() or media_note
            if not note:
                return
            now = time.time()
            user_context["last_message"] = str(text or "")[:500]
            user_context["last_message_time"] = now
            user_context["last_reply"] = note[:500]
            user_context["last_reply_time"] = now
            user_context["reply_count"] = int(
                user_context.get("reply_count", 0) or 0) + 1
            if media_note:
                self._record_media_sent(
                    user_context, note=media_note, scene=media_scene,
                    series=media_series, ts=now)
        except Exception:
            self.logger.debug("record_stage_turn skipped", exc_info=True)

    _MEDIA_SENT_LOG_MAX = 5

    def _record_media_sent(
        self, user_context: Dict[str, Any], *, note: str, scene: str = "",
        series: str = "", ts: float = 0.0, desc: str = "", author: str = "",
    ) -> None:
        """已发媒体日志（Phase18，bounded=5，随 ContextStore 持久化）。

        记「什么时候发过什么图/什么场景/什么衣着系列」——供 ①「跟上次一样的」
        场景指涉（``_last_sent_media_scene``）②prompt「你最近发过的照片」块
        （防"我没发过照片"失忆抵赖）③衣着连续窗（``_last_sent_media_series``，
        P1）。刻意**不**写 episodic memory：那是"用户事实"存储，
        系统行为混进去会污染记忆语义（Phase8 治理过的教训）。

        ``desc`` / ``author``（接力记忆 P0-1，2026-09-12）：坐席手动发的图由
        ``human_outbound_memory`` 写进同一账本（author=human，desc=VLM 画面描述），
        prompt 块把 desc 当「画面」事实 surface——AI 切回全自动后知道自己"发过什么"。
        """
        try:
            log = user_context.get("_media_sent_log")
            if not isinstance(log, list):
                log = []
            row: Dict[str, Any] = {
                "ts": float(ts or time.time()),
                "note": str(note or "")[:160],
                "scene": str(scene or "")[:120],
                "series": str(series or "")[:80],
            }
            if desc:
                row["desc"] = str(desc)[:160]
            if author:
                row["author"] = str(author)[:16]
            log.append(row)
            user_context["_media_sent_log"] = log[-self._MEDIA_SENT_LOG_MAX:]
            # 真发出媒体 → 清「连续空头承诺」计数（P1 承诺循环熔断，2026-07-29）。
            user_context["_photo_promise_streak"] = 0
        except Exception:
            self.logger.debug("record_media_sent skipped", exc_info=True)

    def _last_sent_media_scene(self, user_context: Dict[str, Any]) -> str:
        """最近一次带场景的已发媒体的 scene（「跟上次一样的」指涉用；无则空串）。"""
        try:
            log = user_context.get("_media_sent_log")
            if isinstance(log, list):
                for row in reversed(log):
                    sc = str((row or {}).get("scene") or "").strip()
                    if sc:
                        return sc
        except Exception:
            pass
        return ""

    def _last_sent_media_series(
        self, user_context: Dict[str, Any], *, within_minutes: float = 0.0,
    ) -> str:
        """连续窗内最近一次已发媒体的衣着系列（P1 跨 A/B 跟装用；过窗/无则空串）。

        媒体日志是 A/B 两线合流后的视图（B 线经 on_sent 回写）——比只看
        persona_media 账本多覆盖「生成图」的衣着（生成图不进相册账本）。
        """
        try:
            if within_minutes <= 0:
                return ""
            log = user_context.get("_media_sent_log")
            if isinstance(log, list):
                now_ts = time.time()
                for row in reversed(log):
                    sr = str((row or {}).get("series") or "").strip()
                    if not sr:
                        continue
                    ts = float((row or {}).get("ts") or 0)
                    if (now_ts - ts) <= within_minutes * 60.0:
                        return sr
                    return ""  # 日志按时间序，最近一条带系列的已过窗 → 不再回看
        except Exception:
            pass
        return ""

    def _selfie_weather_snap(self, user_context: Dict[str, Any]) -> Any:
        """图链天气快照（2026-08-18 天气匹配生活照）：复用时空接地已解析的
        ``_persona_weather_snap``，过 ``companion.weather.scene_filter`` 闸
        （与聊天状态块 8037 段同口径）。任何不满足 → None（=旧行为）。"""
        try:
            snap = (user_context or {}).get("_persona_weather_snap")
            if snap is None:
                return None
            _wcfg = (
                ((self.config.config or {}).get("companion") or {})
                .get("weather") or {}
            ) if getattr(self, "config", None) else {}
            if not (isinstance(_wcfg, dict) and _wcfg.get("enabled", True)
                    and _wcfg.get("scene_filter", True)):
                return None
            return snap
        except Exception:
            return None

    def _inject_time_grounding(self, user_context: Dict[str, Any]) -> None:
        """时空接地注入（2026-08-02 与 selfie 解耦）：人设居住地当地时间/
        与服务器时差/当地天气写进 user_context，供 prompt 时间行、时差桥、
        出站 world_clock_guard 消费。

        原先整段活在 ``_inject_scene_state`` 的 selfie.enabled 闸内——「不发图
        就没有图文一致问题」对**场景**成立，对**时间**不成立：selfie 关的部署
        里跨时区人设照样按服务器时钟聊天（WA 实录：温哥华人设周日凌晨说
        Saturday evening，被手机 DataDetector 下划线钉在屏幕上）。
        开关 ``companion.time_grounding.enabled``（**默认开**——这是正确性
        事实不是新能力，显式 false 才关，供排障回退）；天气仍受自己的
        ``companion.weather.enabled``（默认关）约束。无 location 人设零改变
        （不落键，prompt 走服务器时间兜底）。
        """
        try:
            _cc = (
                ((self.config.config or {}).get("companion") or {})
                if getattr(self, "config", None) else {}
            )
            _tcfg = _cc.get("time_grounding")
            if isinstance(_tcfg, dict) and not _tcfg.get("enabled", True):
                return
            # user_context 跨轮持久：先清旧键，防「人设换绑/location 摘除后
            # 还按上一轮的城市说话」（原实现只写不清的隐性残留，一并修掉）。
            for _k in ("_persona_place_label", "_persona_local_now",
                       "_persona_local_time_line", "_persona_time_gap_line",
                       "_persona_weather_snap", "_persona_weather_note",
                       "_persona_weather_hook"):
                user_context.pop(_k, None)
            persona = self._selfie_persona_for_prompt(user_context)
            from src.companion.persona_location import (
                resolve_place_with_fallback,
                persona_now as _p_now,
                local_time_line,
                time_gap_line,
            )
            _place = resolve_place_with_fallback(persona)
            if _place is None:
                return
            _local_now = _p_now(_place)
            user_context["_persona_place_label"] = _place.display("zh")
            user_context["_persona_local_now"] = _local_now
            _lt = local_time_line(_place, "zh", _local_now)
            if _lt:
                user_context["_persona_local_time_line"] = _lt
            _gap = time_gap_line(_place, "zh", _local_now)
            if _gap:
                user_context["_persona_time_gap_line"] = _gap
            # 当地天气事实（companion.weather.enabled；#104 实施91 起默认开
            # ——Open-Meteo 免 key + TTL 缓存 + 软失败，无所在地人设零改变；
            # 显式 false 可关。默认关的旧态=「AI 对人设当地天气零事实来源」，
            # 共情镜像客户天气纯靠编，0831 原图 860 实锤）
            try:
                _wcfg = _cc.get("weather") or {}
                if isinstance(_wcfg, dict) and _wcfg.get("enabled", True):
                    from src.companion.weather_state import (
                        fetch_weather, weather_chat_note,
                        weather_proactive_hook,
                    )
                    _wx_snap = fetch_weather(
                        _place,
                        ttl_sec=int(_wcfg.get("ttl_sec") or 1800),
                        max_stale_sec=int(
                            _wcfg.get("max_stale_sec") or 10800),
                    )
                    if _wx_snap is not None:
                        user_context["_persona_weather_snap"] = _wx_snap
                        if _wcfg.get("inject_chat", True):
                            _wn = weather_chat_note(_wx_snap, "zh")
                            if _wn:
                                user_context["_persona_weather_note"] = _wn
                        if _wcfg.get("proactive_hook", True):
                            _hook = weather_proactive_hook(_wx_snap, "zh")
                            if _hook:
                                user_context["_persona_weather_hook"] = _hook
            except Exception:
                self.logger.debug("inject weather skipped", exc_info=True)
        except Exception:
            self.logger.debug("inject_time_grounding skipped", exc_info=True)

    def _inject_scene_state(self, user_context: Dict[str, Any]) -> None:
        """Phase18 场景状态注入（图文同源的「因」）：把 resolve_current_scene 的
        确定性场景写进 ``_current_scene_note`` 供 prompt 消费——聊天文本与生图
        从此共用同一个「AI 此刻在哪」，从源头消灭"文本说上班、图发海边"打脸。

        gated：selfie.enabled（没发图能力就没有图文一致问题）+ ``scene_in_chat``
        （默认开，可关）。同日同时段场景恒定（pick_scene_hint 语义），十分钟内
        不会"瞬移"。已发媒体日志随场景注入一并挂上（同一消费口）。
        时间/时差/天气接地已拆到 ``_inject_time_grounding``（本方法开头无条件
        先跑——那是正确性事实，绝不能被 selfie 闸住）。
        """
        # 时空接地先行（与 selfie 解耦，2026-08-02）
        self._inject_time_grounding(user_context)
        try:
            scfg = self._selfie_cfg()
            if not scfg.get("enabled", False) or not bool(
                    scfg.get("scene_in_chat", True)):
                return
            from src.ai.companion_selfie import (
                build_day_itinerary,
                meal_state_note,
                resolve_current_scene,
                scene_chat_note,
            )
            persona = self._selfie_persona_for_prompt(user_context)
            # 人设本地时钟：时空接地已解析（无 location 人设不落键 → None，
            # 下游场景函数按服务器 now 兜底，与旧行为一致）。
            _local_now = user_context.get("_persona_local_now")
            _wx_for_scene = user_context.get("_persona_weather_snap")
            try:
                _wcfg2 = (
                    ((self.config.config or {}).get("companion") or {})
                    .get("weather") or {}
                ) if getattr(self, "config", None) else {}
                if not (isinstance(_wcfg2, dict) and _wcfg2.get("enabled")
                        and _wcfg2.get("scene_filter", True)):
                    _wx_for_scene = None
            except Exception:
                _wx_for_scene = None
            # Phase20 行程线：场景从「点」到「线」——今天四时段动线随 note 注入，
            # LLM 可自然引用"早上去过哪/晚点打算干嘛"（scene_itinerary 可关）。
            _itin = (build_day_itinerary(
                persona, scfg, now=_local_now, weather_snap=_wx_for_scene)
                     if bool(scfg.get("scene_itinerary", True)) else None)
            _scene_now = resolve_current_scene(
                persona, scfg, now=_local_now, weather_snap=_wx_for_scene)
            # 今日衣着（P1）：与生成链同 key 同函数取值——被问「穿了什么」的
            # 回答和照片里的衣服同源。连续窗内刚发过照片 → 跟随照片衣着。
            # P2：传当前场景做 场景×季节 适配（状态块与照片同一套换装逻辑）。
            _outfit = ""
            try:
                from src.companion.outfit_state import current_outfit
                from src.inbox.image_autosend import resolve_consistency_cfg
                _ccc = resolve_consistency_cfg(scfg)
                _outfit = current_outfit(
                    persona, scfg,
                    persona_key=self._selfie_album_key(user_context),
                    recent_series=self._last_sent_media_series(
                        user_context, within_minutes=float(
                            _ccc.get("continuity_minutes") or 0)),
                    scene=_scene_now,
                    now=_local_now)["outfit"]
            except Exception:
                _outfit = ""
            note = scene_chat_note(
                _scene_now, itinerary=_itin, outfit=_outfit, now=_local_now)
            # 饮食状态事实源（2026-07-15 矛盾事故修复）：吃没吃饭从「LLM 现编」
            # 改为确定性事实（persona+日期 hash），防重复系统换角度时事实漂移。
            if bool(scfg.get("meal_state_in_chat", True)):
                _pid = str((persona or {}).get("id") or "") \
                    if isinstance(persona, dict) else ""
                _meal = meal_state_note(_pid, now=_local_now)
                if _meal:
                    note = (note + "\n" + _meal) if note else _meal
            if note:
                user_context["_current_scene_note"] = note
            log = user_context.get("_media_sent_log")
            if isinstance(log, list) and log:
                lines = []
                _has_video = False
                # 四期口径统一：最近 3 条 + 更早 10 条内坐席替发的（author=human）——selfie 链
                # 开着时本块替代 human_media_note 注入，坐席发过的图/视频不该因 AI 又发了
                # 三张自拍就掉出记忆；总数封顶 6、按时间序。
                _recent = list(log[-3:])
                _human_extra = [r for r in log[-10:-3]
                                if isinstance(r, dict) and str(r.get("author") or "") == "human"]
                _rows_show = (_human_extra + _recent)[-6:]
                for row in _rows_show:
                    if not isinstance(row, dict):
                        continue
                    try:
                        _t = time.strftime(
                            "%m-%d %H:%M", time.localtime(float(row.get("ts") or 0)))
                    except Exception:
                        _t = ""
                    if str(row.get("note") or "").lstrip().startswith("[视频]"):
                        _has_video = True
                    _sc = str(row.get("scene") or "").strip()
                    # series slug（如 white-hat-red-jacket / black-and-white）＝相册图
                    # 的画面 ground truth——scene 常空串（"不误标"），但 series 携带
                    # 服装/风格事实，surface 出来防 LLM 现编画面（T3「手冲壶」根因）。
                    _sr = str(row.get("series") or "").strip().replace("-", " ")
                    # 坐席手动发图的 VLM 描述（接力记忆 P0-1）：与 series 同为画面事实
                    _dsc = str(row.get("desc") or "").strip()
                    _n = str(row.get("note") or "").strip()
                    _facts = "；".join(
                        x for x in (f"场景：{_sc}" if _sc else "",
                                    f"画面：{_sr}" if _sr else "",
                                    f"画面：{_dsc}" if _dsc and not _sr else "") if x)
                    if str(row.get("author") or "") == "human":
                        _facts = "；".join(x for x in (_facts, "坐席替你发的") if x)
                    lines.append(
                        f"- {_t} {_n}" + (f"（{_facts}）" if _facts else ""))
                if lines:
                    # 接力记忆三期：发过视频时措辞跟上（不再满口「照片/那张」）
                    _kind = "照片/视频" if _has_video else "照片"
                    _mention = "你发的照片/上次那张/那个视频" if _has_video else "你发的照片/上次那张"
                    user_context["_media_sent_note"] = (
                        f"【你最近发过的{_kind}（事实）】\n" + "\n".join(lines) + "\n"
                        f"对方提到「{_mention}」时按此回应，不要否认发过。"
                        "**图文一致铁律**：只能说标注的『场景』里真实有的东西；"
                        "没标注场景=你也不知道画面细节，就含糊说（『那张呀～』），"
                        "**绝对不要编造照片里没有的具体物件、地点或动作**"
                        "（例：没标注却说『手冲壶入镜/在窗边拍的』=编造，一旦对方"
                        "看图对不上就穿帮）；场景为英文短语，引用时口语化转述。"
                        "**别把之前发的照片说成『刚拍的/此刻正在发生』**——这些多是"
                        "相册存货，后续聊到就按发出时的口径（之前拍的），别改口成"
                        "『而家新鲜/海风正吹』这类此刻进行时（与首次配文自相矛盾=穿帮）。")
        except Exception:
            self.logger.debug("inject_scene_state skipped", exc_info=True)

    async def _apply_photo_directive(
        self, reply: str, user_id_str: str, user_context: Dict[str, Any],
        chat_id: Any, log_prefix: str = "",
    ) -> str:
        """5c0. LLM 发图指令执行（A 线，2026-07-14 决策权上移）。

        主 LLM 读完整上下文后在正文末行用 ``[PHOTO selfie|object 场景]`` 声明发图
        （协议见 ``photo_directive`` 模块），本方法解析并剥净标记；llm/hybrid 模式
        下有效指令 → 与 Stage A/B 同款准入（decide_selfie/bond/cap/vision_gate）→
        真出图（selfie=PuLID 锁脸 + LLM 对话内场景；object=text2img）→ 图先发
        （空配文），正文继续走 5c/5c2/5d 全部守卫后照常发出（先图后文）。

        发图成功 → 置 ``_photo_just_sent``（5c2 对「刚拍的」这句真话免剥）。
        闸门拒绝/出图失败 → 只剥标记不发图，正文里的承诺句交 5c2 撤回——
        绝不让「照片来了」和没有照片同时发生。任何异常兜底强制剥标记（防泄漏）。
        """
        try:
            from src.ai.photo_directive import (
                extract_photo_directive, resolve_intent_mode,
            )
            clean, directive = extract_photo_directive(reply)
            if clean == reply and directive is None:
                return reply  # 无标记（绝大多数消息）：零开销直返
            scfg = self._selfie_cfg()
            mode = resolve_intent_mode(scfg)
            if directive:
                self.logger.info(
                    "%s[photo_directive] LLM指令 kind=%s scene=%r mode=%s",
                    log_prefix, directive.get("kind"),
                    directive.get("scene"), mode)
            if (not directive or mode == "keyword"
                    or not scfg.get("enabled", False)):
                return clean  # 只剥不执行（回退模式/协议未启用仍防泄漏）
            sent = False
            if directive.get("kind") == "selfie":
                sent = await self._photo_directive_selfie(
                    str(directive.get("scene") or ""), user_id_str,
                    user_context, chat_id, scfg, log_prefix)
            elif directive.get("kind") == "object":
                sent = await self._photo_directive_object(
                    str(directive.get("scene") or ""),
                    user_context, chat_id, scfg, log_prefix)
            if sent:
                user_context["_photo_just_sent"] = True
                # 指令图进已发媒体日志（Phase18）：消费 _stage_media_note（此路径
                # 不经 _record_stage_turn，不消费会残留污染下一次 stage 短路轮），
                # scene=LLM 对话内场景 → 「跟上次一样的」可复刻、下一轮可引用。
                _note = str(user_context.pop(
                    "_stage_media_note", "") or "").strip()
                if _note:
                    self._record_media_sent(
                        user_context, note=_note,
                        scene=str(directive.get("scene") or ""))
            else:
                # 已尝试执行但没发出（闸门拒/生成失败）→ 标记之，5c2 承诺兑现
                # 不再用同一管线重试（闸门结果不会变、生成重试=双倍 GPU 烧卡）。
                user_context["_photo_attempt_failed"] = True
            return clean
        except Exception:
            self.logger.debug(
                "%sphoto directive 执行异常（已忽略）", log_prefix, exc_info=True)
            try:  # 防泄漏兜底：无论发生什么，标记必须剥掉
                from src.ai.photo_directive import strip_photo_directives
                return strip_photo_directives(reply)
            except Exception:
                return reply

    async def _photo_directive_selfie(
        self, scene: str, user_id_str: str, user_context: Dict[str, Any],
        chat_id: Any, scfg: Dict[str, Any], log_prefix: str = "",
        *, scene_strict: bool = True,
    ) -> bool:
        """按 LLM 指令出一张人设自拍（准入/预算/质检与 Stage A 完全同款）。

        与 Stage A 的差异只有两点：场景来自 LLM 对话内理解（贴正文、贴时间），
        配文交由正文承担（图本身空 caption，先图后文）。True=图已真实送达。
        ``scene_strict``（P0 一致性）：场景是否「说出口」级硬要求——LLM 指令
        场景与正文出自同一次思考（默认 True，相册兜底必须同场景类）；异步兑现
        的**轮换**场景没人说过（False，相册兜底可放宽）。
        """
        from src.ai.companion_selfie import (
            build_selfie_prompt, decide_selfie, get_selfie_provider,
            resolve_persona_lora, resolve_variety_salt, stable_selfie_seed,
        )
        # 人设级发图闸（2026-07-31，默认关）：LLM 打了 [PHOTO] 标记也不放行
        # （能力关时协议本不该注入，这里是执行层的第二道防线）。
        from src.companion.photo_capability import prompt_photos_allowed
        if not prompt_photos_allowed(user_context):
            return False
        provider = get_selfie_provider(scfg.get("provider") or {})
        will_generate = bool(getattr(provider, "enabled", False)) and \
            str(getattr(provider, "backend", "")).lower() not in ("", "disabled")
        if not will_generate:
            return False
        # 免费额度按天计（与 Stage A 同一份 user_context 计数，两入口互认额度）
        today = time.strftime("%Y%m%d")
        if user_context.get("_selfie_date") != today:
            user_context["_selfie_date"] = today
            user_context["_selfie_used"] = 0
        free_used = int(user_context.get("_selfie_used") or 0)
        ent = user_context.get("entitlement")
        if not isinstance(ent, dict):
            try:
                from src.utils.companion_context import resolve_entitlement
                ent = resolve_entitlement(user_id_str)
            except Exception:
                ent = None
        decision = decide_selfie(
            entitlement=ent if isinstance(ent, dict) else None,
            gate_enabled=self._monetization_gate_enabled(),
            free_used=free_used,
            free_daily=int(scfg.get("free_daily", 1) or 0),
            bond_level=self._bond_level_from_context(user_context, chat_id),
            min_bond_level=int(scfg.get("min_bond_level", 2) or 0),
        )
        if decision.get("action") != "allow":
            self._record_selfie_event(user_id_str, str(decision.get("action")))
            self.logger.info(
                "%s[photo_directive] 闸门拒绝 action=%s（正文承诺交 promise_guard 撤回）",
                log_prefix, decision.get("action"))
            return False
        cap = int(scfg.get("daily_global_cap", 0) or 0)
        if cap > 0 and self._get_selfie_cap(cap).would_exceed(1):
            self._record_selfie_event(user_id_str, "capped")
            return False
        _sp = self._selfie_persona_for_prompt(user_context)
        # 时间兜底（Phase19）：LLM 场景没带时间词时补当前时段光线（凌晨要图
        # 不出正午烈日照）；已带时间词原样尊重（可能在复刻"上次白天那张"）。
        from src.ai.companion_selfie import ensure_time_of_day
        _vsalt = resolve_variety_salt(scfg)  # 多样性 salt（治千篇一律，默认关）
        _lora = resolve_persona_lora(_sp, scfg)   # per-persona 角色 LoRA spec
        _album_key = self._selfie_album_key(user_context)
        # 今日衣着状态（P1，与 Stage A/聊天注入同源）：连续窗跟随刚发照片。
        # P2：传 LLM 指令场景做 场景×季节 适配。
        _outfit = ""
        try:
            from src.companion.outfit_state import current_outfit
            from src.inbox.image_autosend import resolve_consistency_cfg
            _ccd = resolve_consistency_cfg(scfg)
            _outfit = current_outfit(
                _sp, scfg, persona_key=_album_key,
                recent_series=self._last_sent_media_series(
                    user_context, within_minutes=float(
                        _ccd.get("continuity_minutes") or 0)),
                scene=str(scene or ""))["outfit"]
        except Exception:
            _outfit = ""
        try:
            _wxsnap_pd = self._selfie_weather_snap(user_context)
        except Exception:
            _wxsnap_pd = None  # 无此绑定的轻量调用方 → 无天气=旧行为
        prompt = build_selfie_prompt(
            _sp,
            scene_hint=(ensure_time_of_day(scene, weather_snap=_wxsnap_pd)
                        or str(scfg.get("scene_hint") or "")),
            style=str(scfg.get("style") or ""),
            default_appearance=str(scfg.get("appearance") or ""),
            content_rating=str(scfg.get("content_rating") or ""),
            variety_salt=_vsalt,
            lora_trigger=_lora["trigger"],
            outfit=_outfit,
        )
        if cap > 0:
            self._get_selfie_cap(cap).record_sent(1)
        _avoid = str(user_context.get("_selfie_last_img") or "")
        _base = ""
        try:
            if str(getattr(provider, "backend", "")).lower() in ("openai", "command"):
                _base = provider.reference_image(_album_key)
        except Exception:
            _base = ""
        _seed = stable_selfie_seed(_album_key, salt=(_vsalt or 0)) if bool(
            scfg.get("stable_seed", True)) else -1
        self.logger.info("%s[photo_directive] selfie prompt=%r seed=%s base=%s",
                         log_prefix, prompt, _seed, bool(_base))
        # P0 一致性：LLM 指令场景＝**正文亲口说的场景**→相册兜底硬匹配（正文
        # "图书馆自习"配车内自拍=打脸）；时段过滤 + 防复读账本与 Stage A 同口径。
        _conv_key = f"tg:{chat_id}"
        _sent_files: Any = None
        try:
            from src.inbox.image_autosend import resolve_consistency_cfg
            _cc = resolve_consistency_cfg(scfg)
        except Exception:
            _cc = {"now_hour": None}
        try:
            from src.companion.persona_media_store import (
                get_persona_media_store as _gpms0,
            )
            _pms0 = _gpms0()
            if _pms0 is not None:
                try:
                    _rsd = float(scfg.get("resend_after_days", 90) or 0)
                except (TypeError, ValueError):
                    _rsd = 90.0
                _h0 = _pms0.sent_history(_conv_key, max_age_days=_rsd)
                _sent_files = (set(_h0.get("ids") or ())
                               | set(_h0.get("file_keys") or ())) or None
        except Exception:
            _sent_files = None
        try:
            from src.ai.image_gate import generate_with_gate, resolve_gate_cfg
            _root_cfg = self.config.config if hasattr(self.config, "config") else {}
            res = await generate_with_gate(
                provider, prompt, persona=_sp,
                root_config=_root_cfg if isinstance(_root_cfg, dict) else {},
                gate_cfg=resolve_gate_cfg(scfg), seed=_seed,
                expect_scene=(str(scene or "").strip() if scene_strict else ""),
                expect_hour=_cc.get("now_hour"),
                album_key=_album_key, avoid_path=_avoid, base_image=_base,
                lora=_lora["file"], lora_weight=_lora["weight"],
                exclude_paths=_sent_files,
                album_scene=(str(scene or "").strip() if scene_strict else ""),
                now_hour=_cc.get("now_hour"))
        except Exception:
            res = None
            self.logger.debug("photo_directive selfie 生成异常", exc_info=True)
        if res is None or not getattr(res, "ok", False):
            if scene_strict:
                try:
                    from src.inbox.image_autosend import record_scene_request
                    record_scene_request(str(scene or ""), unmet=True)
                except Exception:
                    pass
            return False
        user_context["_selfie_last_img"] = getattr(res, "image_path", "") or ""
        sent = await self._try_send_selfie_media(
            user_context, chat_id, res.image_path, "")
        if sent:
            self._record_selfie_event(user_id_str, "delivered")
            if decision.get("used_free"):
                user_context["_selfie_used"] = free_used + 1
            # 防复读账本：相册兜底图记账（与 Stage A 同口径；生成图不记）。
            try:
                if str(getattr(res, "provider", "")) == "album":
                    from pathlib import Path as _P

                    from src.ai.companion_selfie import album_series_of_path
                    _st2 = _gpms0()
                    if _st2 is not None:
                        _extra0 = dict(getattr(res, "extra", {}) or {})
                        _fn0 = _P(str(res.image_path)).name
                        _st2.record_send(
                            _conv_key, _fn0,
                            persona_id=str(_album_key or ""),
                            series=(str(_extra0.get("series") or "")
                                    or album_series_of_path(res.image_path)),
                            file_key=_fn0)
            except Exception:
                pass
            user_context["_stage_media_note"] = "[图片] （刚按对话情境发出一张自拍）"
            user_context["_stage_media_scene"] = str(scene or "").strip()
            # 衣着系列（P1）：相册兜底记条目系列，生成图记本次注入衣着 slug。
            try:
                from src.companion.outfit_state import outfit_slug
                if str(getattr(res, "provider", "")) == "album":
                    from src.ai.companion_selfie import album_series_of_path
                    _extra1 = dict(getattr(res, "extra", {}) or {})
                    user_context["_stage_media_series"] = (
                        str(_extra1.get("series") or "")
                        or album_series_of_path(res.image_path))
                else:
                    user_context["_stage_media_series"] = outfit_slug(_outfit)
            except Exception:
                pass
            if scene_strict:
                try:
                    from src.inbox.image_autosend import record_scene_request
                    record_scene_request(str(scene or ""), unmet=False)
                except Exception:
                    pass
            return True
        try:
            from src.inbox.image_autosend import record_image_fallback
            record_image_fallback("a_line_directive_send_failed")
            if scene_strict:
                from src.inbox.image_autosend import record_scene_request
                record_scene_request(str(scene or ""), unmet=True)
        except Exception:
            pass
        return False

    async def _photo_directive_object(
        self, scene: str, user_context: Dict[str, Any], chat_id: Any,
        scfg: Dict[str, Any], log_prefix: str = "",
    ) -> bool:
        """按 LLM 指令出一张物体/场景图（非人像，闸门与 Stage B 同款）。

        主体直接用 LLM 给的英文描述（它比正则从中文抽主体准得多），
        不再需要 ``contextual_images_llm_prompt`` 的二次提炼调用。
        """
        if not scfg.get("contextual_images", False) or not scene.strip():
            return False
        from src.ai.companion_selfie import get_selfie_provider
        from src.ai.contextual_image import build_object_image_prompt
        provider = get_selfie_provider(scfg.get("provider") or {})
        will_generate = bool(getattr(provider, "enabled", False)) and \
            str(getattr(provider, "backend", "")).lower() not in (
                "", "disabled", "album")
        if not will_generate:
            return False
        try:
            bond = int(self._bond_level_from_context(user_context))
        except Exception:
            bond = 0
        if bond < int(scfg.get("min_bond_level", 0) or 0):
            return False
        cap = int(scfg.get("daily_global_cap", 0) or 0)
        if cap > 0 and self._get_selfie_cap(cap).would_exceed(1):
            return False
        prompt = build_object_image_prompt(
            scene, style=str(scfg.get("style") or ""))
        if not prompt.strip():
            return False
        if cap > 0:
            self._get_selfie_cap(cap).record_sent(1)
        self.logger.info("%s[photo_directive] object prompt=%r", log_prefix, prompt)
        try:
            # 物体图 text2img，不带人设的脸。P0：失败绝不回落相册人像（如实失败，
            # 正文承诺由 promise_guard 撤回）。
            res = await provider.generate(prompt, allow_album_fallback=False)
        except Exception:
            res = None
            self.logger.debug("photo_directive object 生成异常", exc_info=True)
        if res is None or not getattr(res, "ok", False):
            return False
        sent = await self._try_send_selfie_media(
            user_context, chat_id, res.image_path, "")
        if sent:
            user_context["_stage_media_note"] = "[图片] （刚按对话情境发出一张照片）"
            return True
        return False

    def _promise_guard_cfg(self) -> Dict[str, Any]:
        """companion.media_promise_guard 配置块（默认开——它只在「承诺无法兑现」
        时改动文本，属出站正确性守卫，与 persona_guard/crisis net 同族）。"""
        try:
            cfg = self.config.config if hasattr(self.config, "config") else {}
            pg = ((cfg.get("companion") or {}).get("media_promise_guard") or {})
            return pg if isinstance(pg, dict) else {}
        except Exception:
            return {}

    def _apply_status_fabrication_guard(
        self, reply: str, user_context: Dict[str, Any], user_text: str = "",
        *, log_prefix: str = "", source: str = "a_line",
    ) -> str:
        """近况陈述编造守卫（#208 L-1 C，2026-09-06）：剥无来源的自指「天气 / 地点·行程 /
        正在做的事」句。与 media_promise_guard / persona_guard 同层，确定性最后防线。

        FTK6S7：档案无雨、无天气源、记忆 0 条，问候却答「Just got back from a walk, the
        rain's light here」，且编造进历史后被当事实反复提。来源池 = 人设档案（background /
        role / hobbies / schedule / specific_memories / likes）+ 天气源 + 场景状态 + 记忆 /
        摘要 + 客户入站；**AI 自己的历史句不算来源**。剥空 → 如实回落原文（应答不能失语，
        计数 + WARNING 让它被看见）。子开关 companion.fabrication_guard.status.enabled
        （默认开）。任何异常放行原文。
        """
        if not reply:
            return reply
        try:
            cfg = self.config.config if hasattr(self.config, "config") else {}
            _fg = (((cfg or {}).get("companion") or {}).get("fabrication_guard") or {})
            _st = (_fg.get("status") or {}) if isinstance(_fg, dict) else {}
            if isinstance(_st, dict) and _st.get("enabled") is False:
                return reply
            from src.utils.proactive_fabrication_guard import (
                build_status_evidence,
                persona_status_facts,
                record_fabrication_guard,
                strip_status_claims,
            )
            ev = build_status_evidence(user_context, user_text)
            pid = str((user_context or {}).get("account_persona_id") or "").strip()
            if pid:
                try:
                    from src.utils.persona_manager import PersonaManager
                    _pf = persona_status_facts(
                        PersonaManager.get_instance().get_persona_by_id(pid))
                    if _pf:
                        ev["texts"].append(_pf)
                except Exception:
                    pass
            new_text, info = strip_status_claims(reply, ev)
            record_fabrication_guard(info, source=source)
            if info.get("status_stripped"):
                self.logger.info(
                    "%s[fabrication_guard] 无来源近况陈述（%s），剥离 %d 句%s：%r",
                    log_prefix, "/".join(info.get("status_cats") or []),
                    len(info["status_stripped"]),
                    "（剥空→回落原文）" if info.get("all_stripped") else "",
                    info["status_stripped"][:2])
                if info.get("all_stripped"):
                    self.logger.warning(
                        "%s[fabrication_guard] 整条回复都是无来源近况陈述，原文放行待观测：%r",
                        log_prefix, str(reply)[:120])
            return new_text
        except Exception:
            self.logger.debug(
                "%s[fabrication_guard] 近况守卫异常（放行原文）", log_prefix, exc_info=True)
            return reply

    def _apply_world_clock_guard(
        self, reply: str, user_context: Dict[str, Any], log_prefix: str = "",
    ) -> str:
        """出站时空接轨：剥与人设当地小时冲突的问候/错城现居断言。默认开。"""
        try:
            if not reply:
                return reply
            cfg = self.config.config if hasattr(self.config, "config") else {}
            wcfg = ((cfg.get("companion") or {}).get("world_clock_guard") or {})
            if isinstance(wcfg, dict) and wcfg.get("enabled") is False:
                return reply
            from src.companion.world_clock_guard import apply_world_clock_guard
            local_now = user_context.get("_persona_local_now")
            if local_now is None:
                # 无居住地人设：服务器钟即人设钟（prompt 时间行同口径回落）——
                # 让时段/场所断言守卫对本地人设同样在岗（2026-08-22 补：此前
                # 无 place 时 hour=-1，凌晨「我在书店」这类断言对本地人设裸奔）。
                import datetime as _dt_wcg
                local_now = _dt_wcg.datetime.now()
            persona = None
            try:
                persona = self._selfie_persona_for_prompt(user_context)
            except Exception:
                persona = user_context.get("_resolved_persona")
            out, info = apply_world_clock_guard(
                reply, persona=persona, local_now=local_now,
                peer_now=user_context.get("_peer_local_now"))
            if info.get("changed"):
                self.logger.info(
                    "%s[world_clock_guard] daypart=%s venue=%s weekday=%s "
                    "wrong_place=%s",
                    log_prefix,
                    info.get("daypart_conflict"),
                    info.get("venue_conflict"),
                    info.get("weekday_conflict"),
                    info.get("wrong_place"),
                )
                return out if out.strip() else reply
            return reply
        except Exception:
            self.logger.debug("[world_clock_guard] 跳过", exc_info=True)
            return reply

    def _apply_media_promise_guard(
        self, reply: str, user_context: Dict[str, Any], log_prefix: str = "",
        *, user_id_str: str = "", chat_id: Any = "", user_text: str = "",
    ) -> str:
        """5c2. 出站媒体承诺守卫（A 线，同步路径零 LLM 延迟）。

        走到 LLM 生成=Stage 0/A/B/C 全未短路=本轮**没有**真发媒体；回复却承诺
        「等我拍/发你照片/发条语音」时按序处理：

        1. **异步兑现**（Phase18，``media_promise_guard.async_fulfill`` 默认关）：
           发图承诺 + 同步预检全过（selfie 开/真出图后端/decide_selfie 允许/
           预算未满/图文通道都在/无 in-flight）→ **保留承诺原文**，spawn 后台任务
           延迟数秒真拍真发（文本先到、图随后到，像真人"去拍了"）；生成/发送失败
           → 自动补一条语言对齐的台阶文本（"手机抽风传不上去…改天补"），
           闭环诚实：要么图到、要么圆场，绝不静默装死。
        2. **撤回兜底**（原 P0 行为）：预检不过/开关关/语音承诺 → 句级剥离
           （剥空换语言对齐婉转话术）。photo_directive 刚试过且失败
           （``_photo_attempt_failed``）→ 直接撤回，不再用同一管线重试烧卡。

        B 线（autosend）在投递层做「兑现优先→撤回兜底」；offer 疑问句不受影响。
        """
        try:
            if not reply:
                return reply
            pg_cfg = self._promise_guard_cfg()
            _directive_failed = bool(
                user_context.pop("_photo_attempt_failed", False))
            if not pg_cfg.get("enabled", True):
                return reply
            from src.ai.outbound_promise_guard import (
                deflection_line,
                detect_media_claim,
                detect_media_promise,
                detect_sent_claim,
                strip_media_claims,
                strip_media_promises,
                strip_sent_claims,
                wants_media,
            )
            # media_context：客户在**索要**媒体 → 才把「这不就来了嘛/你看看这张」
            # 当完成断言判（防误伤评论对方图）。粘性（当轮要图 or 上轮 AI offer +
            # 本轮短肯定 or 上轮 AI 自己在承诺/offer 发图=话题仍开 or **悬置媒体
            # 请求在场**——实施69：要图在 1-3 轮前、催促句「拍吧/我不信」不带媒体
            # 名词，单轮判定全程漏）；但**客户本轮发了图**时抑制（此时 AI 多半
            # 在评论对方的图，「这张真好看」不能误剥）。
            _mctx = False
            try:
                _cur_is_img = bool(user_context.get("image_ocr_text"))
                if not _cur_is_img:
                    from src.ai.outbound_promise_guard import (
                        detect_media_offer, is_short_affirmative,
                    )
                    _lr = str(user_context.get("last_reply") or "")
                    _mctx = bool(
                        wants_media(user_text)
                        or (is_short_affirmative(user_text)
                            and detect_media_offer(_lr))
                        or detect_media_promise(_lr)
                        or detect_media_offer(_lr))
                    if not _mctx:
                        # 悬置仅 image 轨开门：语音发出不落媒体日志、无可靠
                        # 熄灭信号，误开会剥掉「刚发了条语音」这类真话。
                        from src.ai.media_pending import pending_kind as _mp_k
                        _mctx = _mp_k(user_context) == "image"
            except Exception:
                _mctx = False
            kind = detect_media_promise(reply) or detect_media_claim(
                reply, media_context=_mctx)
            # #171 过去时假声明（「I just sent it, you should have it now」
            # 「我再发一次」）：不吃 media_context（实录里门全程没开），真伪对照
            # 本会话 _media_sent_log 近窗——近 window_min 内真发过＝真话放行。
            _sent_claim = False
            if not kind:
                try:
                    _scg = pg_cfg.get("sent_claim", {}) or {}
                    if not isinstance(_scg, dict):
                        _scg = {}
                    if _scg.get("enabled", True):
                        from src.ai.media_pending import (
                            media_sent_within as _msw,
                        )
                        _sck = detect_sent_claim(reply)
                        if _sck and not _msw(
                                user_context,
                                float(_scg.get("window_min", 30) or 30) * 60.0):
                            kind = _sck
                            _sent_claim = True
                except Exception:
                    _sent_claim = False
            if not kind:
                # 无承诺/断言，但可能是 offer（「要不要看照片」）→ 记悬置：
                # 客户下轮短肯定后若图迟迟不到，后续谎言仍有门可拦（实施69）。
                try:
                    from src.ai.media_pending import note_ai_turn as _mp_out
                    _mp_out(user_context, reply)
                except Exception:
                    pass
                return reply
            try:
                from src.inbox.image_autosend import (
                    record_promise_event, record_sent_claim_event,
                )
                if _sent_claim:
                    record_sent_claim_event("detected")
                else:
                    record_promise_event("detected")
            except Exception:
                pass
            # ── 1) 异步兑现（仅发图承诺；photo_directive 刚失败则跳过防复烧）──
            if (kind == "image" and not _directive_failed
                    and bool(pg_cfg.get("async_fulfill", False))
                    and self._async_fulfill_precheck(
                        user_id_str, user_context, chat_id)):
                # P0 一致性：承诺句点名了场景（「拍张海边的发你」）→ 兑现的图
                # 必须贴承诺场景（词表外/没点名 → 空串走场景轮换）。
                _pscene = ""
                try:
                    from src.ai.outbound_promise_guard import promised_scene
                    _pscene = promised_scene(reply)
                except Exception:
                    _pscene = ""
                self._spawn_promise_fulfill_task(
                    user_id_str, user_context, chat_id, log_prefix,
                    promised_scene=_pscene)
                try:
                    from src.inbox.image_autosend import (
                        record_promise_event, record_sent_claim_event,
                    )
                    (record_sent_claim_event if _sent_claim
                     else record_promise_event)("fulfill_scheduled")
                except Exception:
                    pass
                self.logger.info(
                    "%s[promise_guard] 发图%s→异步兑现已排队（保留原文）",
                    log_prefix, "「已发」假声明" if _sent_claim else "承诺")
                # 承诺出站=客户进入等待态（实施69）：兑现成功由媒体日志熄灭，
                # 失败则悬置压住后续轮的「来了嘛/发过了」。
                try:
                    from src.ai.media_pending import note_ai_turn as _mp_out
                    _mp_out(user_context, reply)
                except Exception:
                    pass
                return reply  # 承诺保留：图马上真的会到
            # ── 2) 撤回兜底（原行为）────────────────────────────────────────
            # 先剥「将发」承诺句，再剥「已发」断言句（claim；本轮无媒体=谎）。
            stripped = strip_media_promises(reply)
            stripped = strip_media_claims(stripped, media_context=_mctx)
            if _sent_claim:
                stripped = strip_sent_claims(stripped)
            # 二次校验：剥完仍残留承诺/断言（跨句拼接漏网）→ 整条兜底话术。
            if stripped.strip() and (
                    detect_media_promise(stripped)
                    or detect_media_claim(stripped, media_context=_mctx)
                    or (_sent_claim and detect_sent_claim(stripped))):
                stripped = ""
            if not stripped.strip():
                # 假声明剥空 → 如实「这边没发出去、稍后补」，不卖关子（客户刚说没收到）
                stripped = deflection_line(reply, kind, sent_claim=_sent_claim)
            try:
                from src.inbox.image_autosend import (
                    record_promise_event, record_sent_claim_event,
                )
                (record_sent_claim_event if _sent_claim
                 else record_promise_event)("retracted")
            except Exception:
                pass
            # 连续空头承诺计数（P1 熔断）：发图承诺/断言被撤回=又一次「说了没发」→
            # 累加；下一轮 pre-gen 读到 ≥2 会注入「别再答应」hint，打断
            # 「每轮都说马上拍→撤回→下轮又说」的死循环（对练 T9/T10 实证）。
            if kind == "image":
                try:
                    user_context["_photo_promise_streak"] = int(
                        user_context.get("_photo_promise_streak", 0) or 0) + 1
                except Exception:
                    pass
            self.logger.info(
                "%s[promise_guard] 出站%s%s已撤回（本轮无媒体真发，streak=%s）",
                log_prefix, "发图" if kind == "image" else "发语音",
                "「已发」假声明" if _sent_claim
                else ("断言" if detect_media_claim(reply, media_context=_mctx)
                      and not detect_media_promise(reply) else "承诺"),
                user_context.get("_photo_promise_streak", 0))
            # 撤回后的文本按理已无承诺；若剥残（跨句拼接）仍有 → 照记悬置兜底。
            try:
                from src.ai.media_pending import note_ai_turn as _mp_out
                _mp_out(user_context, stripped)
            except Exception:
                pass
            return stripped
        except Exception:
            self.logger.debug("media promise guard skipped", exc_info=True)
            return reply

    def _async_fulfill_precheck(
        self, user_id_str: str, user_context: Dict[str, Any], chat_id: Any,
    ) -> bool:
        """异步兑现的**同步高置信预检**：只有「几乎必然能发出」才保留承诺原文。

        任何一项不确定 → False 走撤回（宁可少一次惊喜，不可多一次空头支票）。
        检查面：selfie 开 + 真出图后端 + 关系/权益闸门 allow + 全局预算未满 +
        图通道与文本补偿通道都在 + 该用户无 in-flight 兑现任务。
        """
        try:
            if not user_id_str:
                return False
            scfg = self._selfie_cfg()
            if not scfg.get("enabled", False):
                return False
            # 人设级发图闸（2026-07-31，默认关）：人设没开 → 承诺一律撤回，
            # 绝不异步兑现（兑现=真发图，能力关时不存在这条路）。
            from src.companion.photo_capability import prompt_photos_allowed
            if not prompt_photos_allowed(user_context):
                return False
            from src.ai.companion_selfie import decide_selfie, get_selfie_provider
            provider = get_selfie_provider(scfg.get("provider") or {})
            backend = str(getattr(provider, "backend", "")).lower()
            if not bool(getattr(provider, "enabled", False)) or backend in (
                    "", "disabled"):
                return False
            # 发送通道：图（编排器受管 或 A 线照片回调）+ 文本补偿回调都得在。
            has_photo_ch = callable(user_context.get("_send_photo_to_chat"))
            if not has_photo_ch:
                try:
                    platform = str(user_context.get("platform") or "").strip()
                    account_id = str(user_context.get("account_id")
                                     or user_context.get("account_persona_id")
                                     or "").strip()
                    if platform and account_id and str(chat_id or "").strip():
                        from src.integrations.account_orchestrator import (
                            get_orchestrator,
                        )
                        has_photo_ch = get_orchestrator(
                            self.config.config or {}).owns_media(
                            platform, account_id)
                except Exception:
                    has_photo_ch = False
            if not has_photo_ch or not callable(user_context.get("_send_to_chat")):
                return False
            # 关系/权益/免费额度闸门（与 Stage A 同款判定）
            ent = user_context.get("entitlement")
            decision = decide_selfie(
                entitlement=ent if isinstance(ent, dict) else None,
                gate_enabled=self._monetization_gate_enabled(),
                free_used=int(user_context.get("_selfie_used") or 0),
                free_daily=int(scfg.get("free_daily", 1) or 0),
                bond_level=self._bond_level_from_context(user_context, chat_id),
                min_bond_level=int(scfg.get("min_bond_level", 2) or 0),
            )
            if decision.get("action") != "allow":
                return False
            cap = int(scfg.get("daily_global_cap", 0) or 0)
            if cap > 0 and self._get_selfie_cap(cap).would_exceed(1):
                return False
            inflight = getattr(self, "_promise_fulfill_inflight", None)
            if inflight and user_id_str in inflight:
                return False
            return True
        except Exception:
            self.logger.debug("async fulfill precheck failed", exc_info=True)
            return False

    def _spawn_promise_fulfill_task(
        self, user_id_str: str, user_context: Dict[str, Any], chat_id: Any,
        log_prefix: str = "", promised_scene: str = "",
    ) -> None:
        """把「真拍真发」排进当前事件循环（不阻塞本轮文本回复）。"""
        if not hasattr(self, "_promise_fulfill_inflight"):
            self._promise_fulfill_inflight = set()
        self._promise_fulfill_inflight.add(user_id_str)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._fulfill_promised_selfie_async(
                user_id_str, user_context, chat_id, log_prefix,
                promised_scene=promised_scene))
        except Exception:
            self._promise_fulfill_inflight.discard(user_id_str)
            self.logger.debug("spawn promise fulfill failed", exc_info=True)

    async def _fulfill_promised_selfie_async(
        self, user_id_str: str, user_context: Dict[str, Any], chat_id: Any,
        log_prefix: str = "", *, promised_scene: str = "",
    ) -> None:
        """异步兑现主体：延迟数秒（文本先到 + "去拍了"的真人时间感）→ 复用
        ``_photo_directive_selfie`` 全套管线（decide/cap/PuLID/vision_gate/发送/
        额度）→ 成功记媒体日志；失败发语言对齐的台阶补偿文本。软失败绝不抛。

        ``promised_scene``（P0 一致性）＝承诺句点名的场景（硬要求，兑现必须贴）；
        空＝没点名 → 场景轮换（与聊天注入同源，相册兜底可放宽）。"""
        import random as _rnd
        try:
            await asyncio.sleep(_rnd.uniform(4.0, 9.0))
            scfg = self._selfie_cfg()
            # 场景：承诺点名 →（否则）场景轮换（与聊天注入同源，图文一致）。
            _scene = str(promised_scene or "").strip()
            _strict = bool(_scene)
            if not _scene:
                try:
                    _wxsnap2 = self._selfie_weather_snap(user_context)
                except Exception:
                    _wxsnap2 = None  # 无此绑定的轻量调用方 → 无天气
                try:
                    from src.ai.companion_selfie import resolve_current_scene
                    from src.companion.persona_location import resolve_persona_now
                    _sp2 = self._selfie_persona_for_prompt(user_context)
                    _scene = resolve_current_scene(
                        _sp2, scfg, now=resolve_persona_now(_sp2),
                        weather_snap=_wxsnap2)
                except Exception:
                    _scene = ""
            sent = False
            try:
                sent = await self._photo_directive_selfie(
                    _scene, user_id_str, user_context, chat_id,
                    scfg, log_prefix, scene_strict=_strict)
            except Exception:
                sent = False
                self.logger.debug("promise fulfill selfie error", exc_info=True)
            if sent:
                note = str(user_context.pop("_stage_media_note", "") or
                           "[图片] （承诺兑现的自拍）").strip()
                self._record_media_sent(
                    user_context, note=note, scene=_scene)
                # 让下一轮 LLM 知道图已随承诺送达（last_reply 追加事实）
                _lr = str(user_context.get("last_reply") or "")
                user_context["last_reply"] = (_lr + "（照片已随后发出）")[:500]
                try:
                    from src.inbox.image_autosend import record_promise_event
                    record_promise_event("fulfilled_async")
                except Exception:
                    pass
                self.logger.info(
                    "%s[promise_guard] 异步兑现成功：承诺的自拍已送达 user=%s",
                    log_prefix, user_id_str)
            else:
                # 无兜底纪律（2026-08-17 老板拍板）：兑现失败**不再补台阶话术**
                # （「手机抽风改天补」类圆场句拆除）——静默 + 弹窗「AI 不可用」
                # + ERROR 上报主机，坐席人工接管该会话的兑现。
                try:
                    from src.ops.delivery_block import report_block
                    report_block(
                        "vision", reason="promise_fulfill_failed",
                        platform=str(user_context.get("platform") or "telegram"),
                        conversation_id=str(
                            user_context.get("_context_store_key")
                            or user_id_str),
                        detail="承诺的自拍未能生成/送达，未发任何圆场话术")
                except Exception:
                    self.logger.debug(
                        "promise fulfill delivery_block 上报失败", exc_info=True)
                try:
                    from src.inbox.image_autosend import record_promise_event
                    record_promise_event("fulfill_failed")
                except Exception:
                    pass
                self.logger.warning(
                    "%s[promise_guard] 异步兑现失败 → 静默（无兜底纪律，"
                    "不补台阶话术）user=%s", log_prefix, user_id_str)
            try:
                _sk = str(
                    user_context.get("_context_store_key") or user_id_str)
                self._context_store.mark_dirty(_sk)
                self._context_store.flush(_sk)
            except Exception:
                pass
        finally:
            try:
                self._promise_fulfill_inflight.discard(user_id_str)
            except Exception:
                pass

    def _selfie_offer_accept_bridge(
        self, text: str, user_context: Dict[str, Any], scfg: Dict[str, Any],
    ) -> bool:
        """「offer-接受」桥（A 线）：上一轮 AI 提议「要不要看照片」、本条客户只回
        「好呀/要」——``detect_selfie_request("好呀")`` 抓不住，offer 会变空头支票。
        命中=视同一次自拍请求（准入/预算闸门照常走）。"""
        try:
            if not bool(scfg.get("offer_accept_bridge", True)):
                return False
            from src.ai.outbound_promise_guard import (
                detect_media_offer,
                is_short_affirmative,
            )
            if not is_short_affirmative(text):
                return False
            hit = detect_media_offer(
                str(user_context.get("last_reply") or "")) == "image"
            if hit:
                try:
                    from src.inbox.image_autosend import record_promise_event
                    record_promise_event("offer_accept")
                except Exception:
                    pass
            return hit
        except Exception:
            return False

    async def _media_presend_pacing(
        self, user_context: Dict[str, Any], chat_id: Any, *,
        _sleep=None,
    ) -> None:
        """媒体出站前的「翻相册挑图/上传」拟人延迟（P3，2026-08-12 实测秒发图后加）。

        实录（03:03 zhiliao）：客户说「出来喝咖啡」→ **0.8s** 后相册图落地——文本链
        有完整 humanize 序列（思考→正在输入→发送），媒体 Stage 0 短路在它**之前**，
        一直裸发。真人翻相册挑图至少要几秒，秒发照片与秒回文字是同一种机器指纹。
        配置 ``companion.selfie.media_pacing``：``false``/缺省=关（**代码默认关**，与
        min_gap/min_residual 同约定——行为变更经种子/overlay 显式开）；``true``=默认
        区间；``{enabled, min_sec, max_sec}`` 可调。区间刻意压短（默认 2.5~6.5s）——
        生成链出图本身已耗时数秒，这里只补「挑/传」的时间感，不叠大延迟。
        等待期每 ~4s 续挂「正在发送照片」chat action（A 线注入 ``_send_media_action``，
        气泡 ~5s 过期；无回调=纯静默等待）。任何异常直接放行（节奏是增强不是闸门）。
        """
        try:
            cfg = (self._selfie_cfg() or {}).get("media_pacing")
            if isinstance(cfg, dict):
                enabled = bool(cfg.get("enabled", True))
                lo = float(cfg.get("min_sec", 2.5) or 0.0)
                hi = float(cfg.get("max_sec", 6.5) or 0.0)
            elif isinstance(cfg, bool):
                enabled, lo, hi = cfg, 2.5, 6.5
            else:
                enabled, lo, hi = False, 2.5, 6.5
            if not enabled or hi <= 0:
                return
            if lo > hi:
                lo, hi = hi, lo
            import random as _rnd
            delay = _rnd.uniform(max(0.0, lo), hi)
            sleep_fn = _sleep or asyncio.sleep
            action = user_context.get("_send_media_action")
            while delay > 0:
                if callable(action):
                    try:
                        await action(chat_id)
                    except Exception:
                        action = None   # 回调坏了不再重试，退化纯等待
                step = min(4.0, delay)
                await sleep_fn(step)
                delay -= step
        except Exception:
            self.logger.debug("[media_pacing] 拟人延迟失败（放行）", exc_info=True)

    async def _try_send_selfie_media(
        self, user_context: Dict[str, Any], chat_id: Any,
        image_path: str, caption: str, *,
        media_type: str = "image", media_url: str = "",
    ) -> bool:
        """best-effort 发出媒体（图/视频）。① 编排器受管媒体 worker（B 线/受管账号）；
        ② A 线主客户端直发（``_send_photo_to_chat`` / 视频用 ``_send_video_to_chat`` 回调）。

        两路都不可用 → False（调用方退回文字陪伴）。任一路成功即 True。
        ``media_type='video'`` 时经编排器发视频；A 线仅在注入了视频回调时才发，否则 False（回落）。
        """
        if not image_path and not media_url:
            return False
        # P3 拟人「挑图」延迟（配置门控，默认关；所有 7 个媒体调用点的单一汇口）
        await self._media_presend_pacing(user_context, chat_id)
        _mt = "video" if str(media_type or "").lower() == "video" else "image"
        # ① 编排器受管媒体 worker（需 platform+account+chat_key 且 owns_media）
        try:
            platform = str(user_context.get("platform") or "").strip()
            account_id = str(user_context.get("account_id")
                             or user_context.get("account_persona_id") or "").strip()
            chat_key = str(chat_id or "").strip()
            if platform and account_id and chat_key:
                from src.integrations.account_orchestrator import get_orchestrator
                orch = get_orchestrator(self.config.config or {})
                if orch.owns_media(platform, account_id):
                    _sres = await orch.send_media(
                        platform, account_id, chat_key,
                        media_path=image_path, media_url=media_url, media_type=_mt,
                        caption=caption)
                    # 2026-07-22 真机复盘：必须验返回值——sidecar 读不到文件等
                    # 失败以 {delivered:False} 返回而非抛异常，旧代码盲返 True
                    # 会让上层以为图已发出（配文"这张够诚意了吧"实则没图，穿帮）。
                    if isinstance(_sres, dict) and (
                            _sres.get("delivered") is False or _sres.get("blocked")):
                        self.logger.info(
                            "[selfie] 编排器媒体投递失败 %s:%s blocked=%s",
                            platform, account_id, _sres.get("blocked") or "-")
                        return False
                    return True
        except Exception:
            self.logger.debug("selfie orchestrator media send failed", exc_info=True)
        # ② A 线主客户端直发（Pyrogram 经回调注入；主平台 Telegram 无受管 worker 时兜底）
        try:
            if _mt == "video":
                vsender = user_context.get("_send_video_to_chat")
                if callable(vsender) and image_path:
                    return bool(await vsender(chat_id, image_path, caption))
                return False  # A 线未注入视频回调 → 交回落（不误当照片发）
            sender = user_context.get("_send_photo_to_chat")
            if callable(sender) and image_path:
                ok = await sender(chat_id, image_path, caption)
                return bool(ok)
        except Exception:
            self.logger.debug("selfie direct send failed", exc_info=True)
        return False

    def _story_scenarios(self) -> Dict[str, Any]:
        sc = self._story_cfg().get("scenarios")
        return sc if isinstance(sc, dict) else {}

    def _story_state_root(self, user_context: Dict[str, Any]) -> Dict[str, Any]:
        root = user_context.get("story_state")
        if not isinstance(root, dict):
            root = {}
            user_context["story_state"] = root
        return root

    def _get_story_state(self, user_context: Dict[str, Any], chat_id: Any):
        from src.utils.companion_relationship import chat_storage_key
        return self._story_state_root(user_context).get(chat_storage_key(chat_id))

    def _set_story_state(self, user_context: Dict[str, Any], chat_id: Any, state) -> None:
        from src.utils.companion_relationship import chat_storage_key
        root = self._story_state_root(user_context)
        key = chat_storage_key(chat_id)
        if state is None:
            root.pop(key, None)
        else:
            root[key] = state

    def _writeback_story_memory(
        self, user_id: str, chat_id: Any, user_context: Dict[str, Any], memory: str
    ) -> None:
        """剧情收场 → 把「共享经历」回写情景记忆（Phase ④ 闭环核心）。

        共享经历在虚构里真实发生过 → 以 ``user_stated`` 高置信入库：可被 consolidate
        晋升 stable、可被 proactive_topic 日后主动回访（"还记得那次……吗"）。add_fact
        以内容哈希去重，重复收场不会灌水。任何失败都不得打断回复管线。
        """
        store = getattr(self, "_episodic_store", None)
        mem = (memory or "").strip()
        if not store or not mem:
            return
        try:
            platform = str(user_context.get("platform", "") or "")
            key = self._episodic_storage_key(
                user_id, chat_id, platform, user_context=user_context)
            store.add_fact(key, mem, "story", source="user_stated")
            self.logger.info(
                "[story] writeback shared-memory key=%s mem=%r", key, mem[:60]
            )
        except Exception:
            self.logger.debug("story memory writeback failed", exc_info=True)

    def _story_bonus_cap(self) -> float:
        """剧情累计加成上限（防止刷剧情把关系刷满；默认 12，约够升一个等级带）。"""
        try:
            return float(self._story_cfg().get("max_intimacy_bonus", 12) or 12)
        except Exception:
            return 12.0

    def _apply_story_intimacy_bonus(
        self, user_context: Dict[str, Any], chat_id: Any, bonus: float
    ) -> None:
        """剧情收场 → 累加一份「共同经历」关系加成（Phase ④「剧情→成长」边）。

        intimacy_score 由 IntimacyEngine 拥有（不可从此处直写），故把剧情加成作为
        **独立累加项**存 rel_state.story_bonus（随 user_context 持久化、按 chat 维度），
        在 ``_effective_intimacy`` 处叠加进 bond 计算——既不篡改事实源、又让「完成深度
        剧情」真实推动关系等级与更深剧情解锁。封顶防刷；失败不打断管线。
        """
        try:
            b = float(bonus or 0.0)
        except (TypeError, ValueError):
            return
        if b <= 0:
            return
        try:
            from src.utils.companion_relationship import get_rel_state
            st = get_rel_state(user_context, chat_id)
            cur = float(st.get("story_bonus", 0) or 0)
            st["story_bonus"] = round(min(self._story_bonus_cap(), cur + b), 2)
            self.logger.info(
                "[story] intimacy bonus +%.1f → story_bonus=%.1f chat=%s",
                b, st["story_bonus"], chat_id,
            )
        except Exception:
            self.logger.debug("story intimacy bonus apply failed", exc_info=True)

    def _record_story_completion(
        self, user_context: Dict[str, Any], chat_id: Any,
        scenario_id: str, title: str, bonus: float, ending: str = "",
    ) -> None:
        """剧情收场结算（Phase ④续）：首次完成才给加成 + 关系纪念点；重复完成不刷分。

        - **防刷**：``rel_state.story_done`` 记已完成场景；重复完成 intimacy_bonus 归零
          （记忆仍照常回写、复发自然累积，但关系深度只认「真实的新经历」）。
        - **跨场景因果（Phase ④续³）**：``rel_state.story_outcomes[sid]=ending`` 记下所取结局，
          供后续剧情的 ``requires_story`` 前置 gate 判定——孤立剧情连成有因果的故事线。
        - **情感闭环**：首次完成 → 置一次性 ``bond_fresh_milestone``，下一轮 bond 块自然
          致意（"我们刚一起经历了那次约会，感觉更近了"）——剧情→成长→真情流露。
        任何失败不打断回复管线。
        """
        sid = str(scenario_id or "").strip()
        try:
            from src.utils.companion_relationship import get_rel_state
            st = get_rel_state(user_context, chat_id)
            done = st.get("story_done")
            if not isinstance(done, list):
                done = []
                st["story_done"] = done
            # 结局足迹（首次/重复都刷新为最近一次所取结局，供因果 gate）
            outcomes = st.get("story_outcomes")
            if not isinstance(outcomes, dict):
                outcomes = {}
                st["story_outcomes"] = outcomes
            if sid:
                outcomes[sid] = str(ending or "")
            is_first = bool(sid) and sid not in done
            if not is_first:
                self.logger.info("[story] replay (no bonus) scenario=%s", sid)
                return
            done.append(sid)
            if float(bonus or 0.0) > 0:
                self._apply_story_intimacy_bonus(user_context, chat_id, bonus)
            t = (title or "").strip()
            if t:
                user_context["bond_fresh_milestone"] = f"story:一起经历了《{t}》"
            # 统一镜像（best-effort）：把首次收场写进 contacts journey 事件流，
            # 让运营健康卡用同一公式算出与会话侧一致的 effective bond。
            # 仅首次（防刷已在上面 gate），与会话侧加成同源同量、不双算（健康卡读事件、
            # 会话读 rel_state，两条互不叠加）。provider 未注册 → no-op，零行为变化。
            self._mirror_story_completion_to_journey(
                user_context, chat_id, sid, t, float(bonus or 0.0), str(ending or ""))
        except Exception:
            self.logger.debug("story completion record failed", exc_info=True)

    def _mirror_story_completion_to_journey(
        self, user_context: Dict[str, Any], chat_id: Any,
        scenario_id: str, title: str, bonus: float, ending: str = "",
    ) -> bool:
        """把剧情首次收场镜像进 contacts journey（供运营健康卡统一 effective bond）。

        寻址需要 ``account_id`` + ``platform``（A 线 telegram_client 已注入 account_id）；
        缺任一 → 跳过。任何失败都吞掉、绝不影响回复管线（会话侧加成已独立生效）。
        """
        try:
            account_id = user_context.get("account_id")
            channel = str(user_context.get("platform") or "").strip() or "telegram"
            if not account_id or chat_id in (None, ""):
                return False
            from src.utils.companion_context import record_story_completion
            return record_story_completion(
                account_id, chat_id, scenario_id,
                channel=channel, ending=ending,
                intimacy_bonus=bonus, title=title,
            )
        except Exception:
            self.logger.debug("story journey mirror skipped", exc_info=True)
            return False

    def _story_outcomes(self, user_context: Dict[str, Any], chat_id: Any) -> Dict[str, str]:
        """已完成剧情 → 所取结局 ``{scenario_id: ending_id_or_""}``（供 requires_story gate）。"""
        try:
            from src.utils.companion_relationship import get_rel_state
            oc = get_rel_state(user_context, chat_id).get("story_outcomes")
            return oc if isinstance(oc, dict) else {}
        except Exception:
            return {}

    def _effective_intimacy(self, user_context: Dict[str, Any], chat_id: Any = ""):
        """基础 intimacy_score + 剧情累计加成（封顶 100）；无基础信号时返回原值（不臆造）。"""
        base = user_context.get("intimacy_score")
        if base is None:
            return None
        try:
            b = float(base)
        except (TypeError, ValueError):
            return base
        try:
            from src.utils.companion_relationship import get_rel_state
            bonus = float(get_rel_state(user_context, chat_id).get("story_bonus", 0) or 0)
        except Exception:
            bonus = 0.0
        return max(0.0, min(100.0, b + bonus))

    def _bond_level_from_context(
        self, user_context: Dict[str, Any], chat_id: Any = ""
    ) -> int:
        try:
            from src.contacts.relationship_level import compute_bond_level
            return int(compute_bond_level(
                self._effective_intimacy(user_context, chat_id)).get("level", 0))
        except Exception:
            return 0

    def _match_scenario(self, scenarios: Dict[str, Any], name: str):
        n = (name or "").strip().lower()
        if not n:
            return None
        for sid, scn in scenarios.items():
            title = str((scn or {}).get("title", "")).strip().lower()
            if n == str(sid).lower() or n == title:
                return sid
        for sid, scn in scenarios.items():
            title = str((scn or {}).get("title", "")).strip().lower()
            if (title and n in title) or n in str(sid).lower():
                return sid
        return None

    @staticmethod
    def _scenario_title(scenarios: Dict[str, Any], sid: str) -> str:
        scn = (scenarios or {}).get(str(sid)) or {}
        return str(scn.get("title") or sid)

    def _ensure_entitlement(self, user_id: Any, user_context: Dict[str, Any]) -> None:
        """Stage 1：把端用户真实付费权益懒解析进 ``user_context["entitlement"]``。

        story 或 ``ai.tiers``（P4 分级路由按会员档自动选档）任一启用才解析（普通
        消息零开销）；两个消费方共用同一份 ``user_context["entitlement"]``（5 分钟
        TTL 缓存——权益变动罕见，避免每条消息查库）。resolver 未注册（monetization
        未就绪）→ 不动 user_context（entitlement 维持原值/None → 付费场景锁 +
        分级走默认档，零回归）。绝不抛——任何失败退回旧行为。
        """
        try:
            _tiers_cfg: Dict[str, Any] = {}
            try:
                _cfg = self.config.config if hasattr(self.config, "config") else {}
                if isinstance(_cfg, dict):
                    _tiers_cfg = (_cfg.get("ai") or {}).get("tiers") or {}
            except Exception:
                _tiers_cfg = {}
            if not (self._story_cfg().get("enabled", False)
                    or (isinstance(_tiers_cfg, dict)
                        and _tiers_cfg.get("enabled", False))):
                return
            cached = user_context.get("entitlement")
            try:
                ts = float(user_context.get("_entitlement_at") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            if isinstance(cached, dict) and (time.time() - ts) < 300.0:
                return  # 近 5 分钟已解析，复用（避免每条消息查库）
            from src.utils.companion_context import resolve_entitlement
            ent = resolve_entitlement(user_id)
            if isinstance(ent, dict):
                user_context["entitlement"] = ent
                user_context["_entitlement_at"] = time.time()
        except Exception:
            self.logger.debug("entitlement resolve skipped", exc_info=True)

    def _handle_story_command(self, text: str, user_context: Dict[str, Any], chat_id: Any):
        """剧情指令（默认关 companion.story.enabled）：列表 / 开始 / 结束。

        返回回复字符串=短路（列表/结束/锁定提示）；返回 None=非剧情指令或「开始成功」
        （成功时只置 state，让正常回复流程带着【剧情场景】块自然开场）。
        """
        cfg = self._story_cfg()
        if not cfg.get("enabled", False):
            return None
        t = (text or "").strip()
        scenarios = self._story_scenarios()
        if not scenarios:
            return None
        ent = user_context.get("entitlement")
        ent = ent if isinstance(ent, dict) else None
        bond = self._bond_level_from_context(user_context, chat_id)
        completed = self._story_outcomes(user_context, chat_id)

        if t in ("结束剧情", "退出剧情", "/story stop", "story stop"):
            if self._get_story_state(user_context, chat_id):
                self._set_story_state(user_context, chat_id, None)
                return "好呀，那我们先回到平常聊天～"
            return None

        if t in ("剧情列表", "剧情", "/story", "story", "story list"):
            from src.skills.story_engine import list_scenarios
            rows = list_scenarios(
                scenarios, entitlement=ent, bond_level=bond, completed=completed)
            if not rows:
                return None
            lines = []
            for r in rows:
                if r["available"]:
                    lines.append(f"· {r['title']}（发「开始剧情 {r['title']}」）")
                elif r["locked_reason"].startswith("need_bond"):
                    lines.append(f"· {r['title']}（我们再熟一点就能解锁）")
                elif r["locked_reason"].startswith("need_story"):
                    pre = self._scenario_title(
                        scenarios, r["locked_reason"].split(":", 1)[-1])
                    lines.append(f"· {r['title']}（经历过《{pre}》后解锁）")
                else:
                    lines.append(f"· {r['title']}（专属剧情，需解锁）")
            return "想一起经历点什么吗？\n" + "\n".join(lines)

        prefix = None
        for p in ("开始剧情", "/story start ", "story start "):
            if t.startswith(p):
                prefix = p
                break
        if prefix is None:
            return None
        name = t[len(prefix):].strip()
        sid = self._match_scenario(scenarios, name)
        if not sid:
            return "嗯…我还不会这个剧情呢，发「剧情列表」看看有哪些？"
        from src.skills.story_engine import scenario_locked_reason, start_scenario
        state = start_scenario(
            sid, scenarios, entitlement=ent, bond_level=bond, completed=completed)
        if state is None:
            reason = scenario_locked_reason(
                scenarios.get(sid) or {}, entitlement=ent, bond_level=bond,
                completed=completed)
            if reason.startswith("need_bond"):
                return "这个故事要我们更熟一些才能解锁哦，再多陪我聊聊吧～"
            if reason.startswith("need_story"):
                pre = self._scenario_title(scenarios, reason.split(":", 1)[-1])
                return f"这段故事是后续呢，我们先一起经历《{pre}》吧～"
            if reason.startswith("need_unlock"):
                return "这是一段专属剧情，解锁后我们就能一起体验啦。"
            return None
        self._set_story_state(user_context, chat_id, state)
        self.logger.info("[story] start scenario=%s chat=%s", sid, chat_id)
        return None

    _GROWTH_TRIGGERS = frozenset({
        "我们的关系", "关系进度", "关系状态", "我的等级", "成长", "成长进度",
        "/status", "/relationship", "我们的故事",
    })

    @staticmethod
    def _progress_bar(progress: float, cells: int = 10) -> str:
        try:
            p = max(0.0, min(1.0, float(progress)))
        except (TypeError, ValueError):
            p = 0.0
        filled = int(round(p * cells))
        return "▮" * filled + "▯" * (cells - filled)

    def _handle_growth_command(
        self, text: str, user_context: Dict[str, Any], chat_id: Any
    ):
        """关系/成长面板（端用户在对话内一屏看见成长）：等级+进度+里程碑+剧情足迹。

        返回字符串=短路；None=非指令 / 非陪伴域。纯读已算好的数据（compute_bond_level /
        bond_milestones / list_scenarios / rel_state.story_done），不写库、不调 LLM。
        """
        t = (text or "").strip()
        if t not in self._GROWTH_TRIGGERS:
            return None
        cfg0 = self.config.config if hasattr(self.config, "config") else {}
        comp = (cfg0.get("companion") or {}) if isinstance(cfg0, dict) else {}
        if effective_domain_name(cfg0) != "conversion" or not comp.get("enabled", True):
            return None

        from src.contacts.relationship_level import (
            bond_milestones as _bm,
            compute_bond_level as _cbl,
            level_unlocks as _lu,
        )

        eff = self._effective_intimacy(user_context, chat_id)
        if eff is None:
            eff = user_context.get("intimacy_score")
        lvl = _cbl(eff)
        days_known = user_context.get("relationship_days")

        lines: List[str] = []
        if lvl.get("level", 0) >= 1:
            head = f"💞 我们现在是「{lvl['name']}」的关系"
            if not lvl.get("is_max") and lvl.get("next_name"):
                head += f"（再深一点就是「{lvl['next_name']}」啦）"
            lines.append(head)
            bar = self._progress_bar(lvl.get("progress", 0.0))
            if lvl.get("is_max"):
                lines.append(f"{bar}  已经是最亲密的关系了呢")
            else:
                stn = lvl.get("score_to_next")
                tail = f"，再积累一点点就能更进一步" if stn else ""
                lines.append(f"{bar}  {int(round(lvl.get('progress', 0.0) * 100))}%{tail}")
        else:
            lines.append("💞 我们才刚认识不久，多陪我聊聊，关系会慢慢变深的～")

        # 里程碑（含相识时长 + 升级；剧情纪念点单独在下方剧情足迹体现）
        try:
            ms = _bm(intimacy_score=eff, days_known=days_known)
            if ms:
                labels = "、".join(m["label"] for m in ms[:5])
                lines.append(f"🌱 一起走过：{labels}")
        except Exception:
            self.logger.debug("growth milestones skipped", exc_info=True)

        # 剧情足迹：经历过 / 还能一起经历 / 待解锁
        scfg = self._story_cfg()
        if scfg.get("enabled", False):
            scenarios = self._story_scenarios()
            if scenarios:
                from src.skills.story_engine import list_scenarios
                ent = user_context.get("entitlement")
                ent = ent if isinstance(ent, dict) else None
                bond = self._bond_level_from_context(user_context, chat_id)
                completed = self._story_outcomes(user_context, chat_id)
                try:
                    from src.utils.companion_relationship import get_rel_state
                    done_ids = set(get_rel_state(user_context, chat_id).get("story_done") or [])
                except Exception:
                    done_ids = set()
                rows = list_scenarios(
                    scenarios, entitlement=ent, bond_level=bond, completed=completed)
                done_titles, avail, locked = [], [], []
                for r in rows:
                    if r["id"] in done_ids:
                        done_titles.append(r["title"])
                    elif r["available"]:
                        avail.append(r["title"])
                    else:
                        locked.append(r["title"])
                if done_titles:
                    lines.append("📖 我们一起经历过：" + "、".join(f"《{x}》" for x in done_titles))
                if avail:
                    lines.append("✨ 还能一起经历：" + "、".join(f"《{x}》" for x in avail)
                                 + "（发「开始剧情 名称」）")
                if locked:
                    lines.append("🔒 等关系更深/解锁后可体验：" + "、".join(f"《{x}》" for x in locked))

        # 等级解锁预览（若配置了 bond_level.unlocks）
        try:
            bl_cfg = comp.get("bond_level") or {}
            if bl_cfg.get("enabled", False):
                unlocked = _lu(lvl.get("level", 0), bl_cfg.get("unlocks"))
                if unlocked:
                    lines.append("🎁 当前等级已解锁：" + "、".join(unlocked))
        except Exception:
            self.logger.debug("growth unlocks skipped", exc_info=True)

        return "\n".join(lines)

    def _update_after_reply(self, reply: str, user_id: str, user_context: Dict[str, Any],
                            chat_id: Any = '', user_msg: str = ''):
        """回�后更新状�?"""
        current_time = time.time()

        # 实施55「聊过即退役」：本轮 prompt 注入的生活素材（ai_client 栈进
        # context）若被出站回复**真提及**（内容 token 重叠）→ 记进会话级已用
        # 账本，该会话此后不再注入这条素材。A/B 两线都经本方法＝单点收口；
        # pop 语义防残留跨轮误标。绝不阻塞主链。
        try:
            _lb_beat = str(user_context.pop("_life_beat_current", "") or "")
            _lb_ck = str(user_context.pop("_life_beat_ck", "") or "")
            if _lb_beat and _lb_ck and reply:
                from src.companion.life_beat_ledger import (
                    beat_mentioned, record_beat_used,
                )
                if beat_mentioned(reply, _lb_beat):
                    record_beat_used(_lb_ck, _lb_beat)
        except Exception:
            pass

        # B52（实施64 P1-2）：出站自述状态（要睡了/去健身…）记进短期状态 log——
        # A/B 两线都经本方法＝单点捕获；后续轮次/proactive 开场据此衔接不矛盾。
        try:
            from src.companion.self_state import record_self_state
            record_self_state(user_context, reply)
        except Exception:
            pass
        # J-10 二期（#177/#171）：出站承诺账本——「明天给你打电话 / 下次拍给你看」这类
        # 第一人称跨天承诺记进 _promise_log（bounded、随 ContextStore 持久），档案抽屉
        # 「承诺过」按 open/overdue/done 展示。只记账、不注入 prompt；零阻断。
        try:
            from src.utils.memory_promises import record_promises
            record_promises(user_context, reply, author="ai")
        except Exception:
            pass

        # Fix D: sanitize reply before persisting (any failure must NOT break the pipeline)
        try:
            _clean_reply = self._sanitize_assistant_reply(reply, user_context)
        except Exception as _se:
            self.logger.debug("sanitize_reply skipped: %s", _se)
            _clean_reply = reply
        user_context.update({
            'last_reply': _clean_reply[:500],
            'last_reply_time': current_time,
            'reply_count': user_context.get('reply_count', 0) + 1
        })
        # 防复读窗口：维护最近 N 条已发回复环形缓冲（供 5b/8b 深度比对，持久化）
        try:
            self._push_recent_reply(user_context, _clean_reply)
        except Exception:
            pass

        try:
            _cfg0 = self.config.config if hasattr(self.config, "config") else {}
            _comp_cfg = (_cfg0.get("companion") or {}) if isinstance(_cfg0, dict) else {}
            if (
                effective_domain_name(_cfg0) == "conversion"
                and _comp_cfg.get("enabled", True)
                and (reply or "").strip()
            ):
                from src.utils.companion_relationship import (
                    get_rel_state,
                    reconcile_stage_after_assistant_reply,
                )

                st = get_rel_state(user_context, chat_id)
                st["exchange_count"] = int(st.get("exchange_count", 0) or 0) + 1
                reconcile_stage_after_assistant_reply(st, _comp_cfg)
        except Exception:
            pass

        # Phase ③/④：活动剧情按用户轮次确定性推进 beat（用户回应驱动分支路由），
        # 剧终自动收场并把「共享经历」回写情景记忆——闭环到 ①（被巩固/被 proactive_topic 回访）。
        try:
            _scfg = self._story_cfg()
            if _scfg.get("enabled", False) and (reply or "").strip():
                _sstate = self._get_story_state(user_context, chat_id)
                if _sstate:
                    from src.skills.story_engine import advance_state
                    _at = int(_scfg.get("advance_turns", 0) or 0)
                    _kw = {"advance_turns": _at} if _at > 0 else {}
                    _sid = str(_sstate.get("scenario_id") or "")
                    _ending = str(_sstate.get("ending_id") or "")
                    _new, _fin, _payload = advance_state(
                        _sstate, self._story_scenarios(),
                        user_message=user_msg, **_kw)
                    self._set_story_state(
                        user_context, chat_id, None if _fin else _new)
                    if _fin and isinstance(_payload, dict):
                        _mem = str(_payload.get("memory") or "").strip()
                        if _mem:
                            self._writeback_story_memory(
                                user_id, chat_id, user_context, _mem)
                        _bonus = float(_payload.get("intimacy_bonus") or 0.0)
                        _title = str(
                            (self._story_scenarios().get(_sid) or {}).get("title")
                            or _sid
                        )
                        self._record_story_completion(
                            user_context, chat_id, _sid, _title, _bonus,
                            ending=_ending)
        except Exception:
            self.logger.debug("story advance skipped", exc_info=True)

        self.global_last_reply_time = current_time
        _acct = str((user_context or {}).get("account_id") or "")
        if chat_id:
            from src.utils.context_store import make_context_key
            self._chat_user_last_reply[
                make_context_key(f"{chat_id}_{user_id}", _acct)
            ] = current_time

        content_hash = self._hash_content(
            user_context.get('last_message', ''), chat_id, account_id=_acct)
        self.reply_cache[content_hash] = current_time

        # L4: 更新用户画像标�
        self._update_user_profile(user_context, current_time)

        # G4: 回�质量���（�则引擎，零成���
        self._evaluate_reply_quality(reply, user_id, user_context)

        # J1: �€测是否需要人工升�?
        self._check_escalation(user_id, user_context, chat_id, current_time)

        _sk = str(
            (user_context or {}).get("_context_store_key") or user_id)
        self._context_store.mark_dirty(_sk)
        if int(current_time) % 5 == 0:
            self._context_store.flush(_sk)

        self._cleanup_cache()

    # �€�€ H3: 意图链模式��?�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€
    _CHAIN_PATTERNS = [
        {
            "pattern": "escalation_complaint",
            "desc": "用户从咨询升级到投诉",
            "sequences": [
                ["order_query", "complaint"],
                ["channel_info", "complaint"],
                ["status_check", "complaint"],
            ],
            "hint": "用户已从咨询升级为投诉，请优先安抚情绪并承诺，给出明确解决时间和方案。",
        },
        {
            "pattern": "repeated_failure",
            "desc": "用户反复查询未解决",
            "sequences": [
                ["order_query", "order_query", "complaint"],
                ["status_check", "status_check", "complaint"],
            ],
            "hint": "用户多次查询同一问题未得到解决，已产生不满。请直接给出最终答复，避免再次要求提供信息。",
        },
        {
            "pattern": "refund_flow",
            "desc": "投诉后要求退款",
            "sequences": [
                ["complaint", "order_query"],
                ["complaint", "direct_chat"],
            ],
            "hint": "用户投诉后继续追问，可能在要求退款或补偿方案。请主动提供解决选项。",
        },
        {
            "pattern": "channel_troubleshoot",
            "desc": "通道问题排查流程",
            "sequences": [
                ["channel_info", "order_query"],
                ["channel_info", "status_check"],
            ],
            "hint": "用户正在排查通道问题对具体订单的影响，请结合通道状态和订单信息综合回答。",
        },
    ]

    def _detect_chain_pattern(self, chain: list) -> Optional[Dict]:
        """�€测意图链���匹配已知模式，返回最长匹配的模式信息"""
        if len(chain) < 2:
            return None
        best = None
        for pat in self._CHAIN_PATTERNS:
            for seq in pat["sequences"]:
                slen = len(seq)
                if len(chain) >= slen and chain[-slen:] == seq:
                    if best is None or len(seq) > len(best.get("_match_len", [])):
                        best = {
                            "pattern": pat["pattern"],
                            "desc": pat["desc"],
                            "hint": pat["hint"],
                            "_match_len": seq,
                        }
        if best:
            best.pop("_match_len", None)
        return best

    # �€�€ K1: 规则引擎摘�压缩（零延迟、零成本�?�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€
    _ENTITY_PATTERNS = re.compile(
        r"(?:订单|单号|order)[#:\s]*([A-Za-z0-9]{6,24})"
        r"|(\d{6,24})"
        r"|(?:额度|limit)[�?\s]*([0-9,.]+)"
        r"|(?:EP|JC|代收|代付|提现)\S{0,8}",
        re.IGNORECASE,
    )

    def _compress_history(self, old_messages: list) -> str:
        """将早期�话轮次压缩为�€行摘要（规则引擎，零 API 调用）�€?
        # 提取：关���体（订单�?金��? 意图流转 + 核心结��?        """
        entities = set()
        intents_seen = []
        conclusions = []

        for msg in old_messages:
            text = msg.get("content", "")
            role = msg.get("role", "user")
            for m in self._ENTITY_PATTERNS.finditer(text):
                val = m.group(1) or m.group(2) or m.group(3) or m.group(0)
                if val and len(val) >= 3:
                    entities.add(val.strip())
            if role == "assistant":
                for kw in ("已", "正常", "维护", "成功", "失败", "处理", "提交", "联系"):
                    if kw in text:
                        snippet = text[:60].replace("\n", " ")
                        conclusions.append(snippet)
                        break

        parts = []
        if entities:
            parts.append("提及: " + ", ".join(list(entities)[:6]))
        if conclusions:
            parts.append("结论片段: " + "; ".join(conclusions[:3]))

        summary = " | ".join(parts) if parts else "（早期对话无关键业务实体，可依近期轮次理解）"
        return summary[:300]

    async def _summarize_history_with_fallback(
        self, old_messages: list,
    ) -> str:
        """Phase 2：优先用 LLM (`ai_client.summarize_conversation`) 生成连贯摘要；
        失败 / 超时 / 配置关闭 → 回退到 rule-based `_compress_history`。

        config 项 `ai.summarize_with_llm` (默认 true)。
        """
        cfg_root = (self.config.config or {}) if self.config and hasattr(self.config, "config") else {}
        use_llm = bool((cfg_root.get("ai") or {}).get("summarize_with_llm", True))
        if use_llm and getattr(self, "ai_client", None) is not None:
            ai = self.ai_client
            if hasattr(ai, "summarize_conversation"):
                try:
                    s = await ai.summarize_conversation(
                        old_messages, max_chars=300, timeout_sec=10.0,
                    )
                    if s and isinstance(s, str) and s.strip():
                        return s.strip()[:300]
                except Exception as ex:
                    self.logger.warning(
                        "[summary] LLM summarize 失败 (%s:%s)，回退 rule-based",
                        type(ex).__name__, ex,
                    )
        return self._compress_history(old_messages)

    # �€�€ L4: 用户画像���推断 �€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€
    _URGENCY_WORDS = re.compile(
        r"赶快[点些]|赶紧|马上|立[刻即]|催|等不了|asap|urgent|hurry|尽快",
        re.IGNORECASE
    )

    def _update_user_profile(self, ctx: Dict[str, Any], now: float):
        """基于�行为特征更新用户画像（�则引擎，零延迟）"""
        profile = ctx.get("_user_profile")
        if not isinstance(profile, dict):
            profile = {
                "msg_count": 0,
                "first_seen": now,
                "intent_dist": {},
                "urgency_count": 0,
                "type": "new",
                "tone": "standard",
            }
        profile["msg_count"] = profile.get("msg_count", 0) + 1

        intent = ctx.get("current_intent", "small_talk")
        dist = profile.get("intent_dist", {})
        dist[intent] = dist.get(intent, 0) + 1
        profile["intent_dist"] = dist

        msg = ctx.get("last_message", "")
        if self._URGENCY_WORDS.search(msg):
            profile["urgency_count"] = profile.get("urgency_count", 0) + 1

        mc = profile["msg_count"]
        age_hours = (now - profile.get("first_seen", now)) / 3600

        # 类型推断
        if mc >= 30 or age_hours >= 168:
            profile["type"] = "veteran"
        elif mc >= 10 or age_hours >= 48:
            profile["type"] = "regular"
        else:
            profile["type"] = "new"

        # 高价值用户：高频 order_query / channel_info
        biz_intents = dist.get("order_query", 0) + dist.get("channel_info", 0)
        if biz_intents >= 15:
            profile["type"] = "vip"

        # ���推断
        urg_ratio = profile.get("urgency_count", 0) / max(mc, 1)
        if urg_ratio >= 0.3:
            profile["tone"] = "impatient"
        elif dist.get("complaint", 0) >= 3:
            profile["tone"] = "frustrated"
        elif dist.get("greeting", 0) / max(mc, 1) >= 0.3:
            profile["tone"] = "friendly"
        else:
            profile["tone"] = "standard"

        # K3: 实时满意度评分（0-100，低于40 是 at_risk）
        sat_score = 80.0  # 基准分
        # 负向因子
        _complaint_ratio = dist.get("complaint", 0) / max(mc, 1)
        sat_score -= _complaint_ratio * 60  # 投诉占比越高越差

        _consecutive = ctx.get("_consecutive_same_intent", 0)
        sat_score -= min(_consecutive, 5) * 6  # 连续同问题追�?
        if urg_ratio >= 0.2:
            sat_score -= urg_ratio * 30  # 急躁程度

        # 正向因子
        _greeting_ratio = dist.get("greeting", 0) / max(mc, 1)
        sat_score += _greeting_ratio * 15  # 有问候�明�€�度友好

        if mc >= 5 and _complaint_ratio < 0.1:
            sat_score += 5  # 长期用户无投�?
        sat_score = max(0, min(100, round(sat_score)))
        profile["satisfaction"] = sat_score
        profile["at_risk"] = sat_score < 40

        ctx["_user_profile"] = profile

    # �€�€ G4: 回�质量���（�则引擎） �€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€
    _QUALITY_LOW_THRESHOLD = 40

    def _evaluate_reply_quality(self, reply: str, user_id: str,
                                ctx: Dict[str, Any]):
        """
        # 基于多维度�则评估回复质量（0-100）�€?        维度：KB 命中强度、回复长度合理�€��€�实体�盖�€�画像�€�配�?        低于阈�€�时记录日志�?        """
        score = 60.0
        reasons = []

        # 1. KB 命中强度
        kb_mode = ctx.get("_kb_search_mode", "")
        kb_ctx = ctx.get("kb_context", "")
        if kb_ctx:
            score += 15
        else:
            score -= 10
            reasons.append("无KB命中")

        # 2. 回�长度合理性（������敷�，太长可能啰嗦）
        rlen = len(reply)
        intent = ctx.get("current_intent", "")
        if intent == "greeting":
            if 5 <= rlen <= 100:
                score += 5
        elif intent in ("order_query", "channel_info", "complaint"):
            if rlen < 15:
                score -= 15
                reasons.append("回�过短")
            elif 30 <= rlen <= 500:
                score += 10
            elif rlen > 800:
                score -= 5
                reasons.append("回�过长")
        else:
            if rlen < 10:
                score -= 10
                reasons.append("回�过短")

        # 3. 实体覆盖—�€�用户提到�单号/通道名，回����引用
        user_msg = (ctx.get("last_message") or "").upper()
        reply_upper = reply.upper()
        _channel_kw = {"EP", "JC", "JAZZ", "EASYPAISA"}
        for kw in _channel_kw:
            if kw in user_msg and kw not in reply_upper:
                score -= 8
                reasons.append(f"用户提及{kw}但回复未提及")
                break

        # 4. 画像适配—�€�frustrated 用户���得到安抚
        profile = ctx.get("_user_profile", {})
        if isinstance(profile, dict):
            tone = profile.get("tone", "standard")
            if tone in ("frustrated", "impatient"):
                comfort_kw = ("抱歉", "理解", "尽快", "麻烦", "对不起", "感谢您的耐心", "不好意思")
                if not any(k in reply for k in comfort_kw):
                    score -= 10
                    reasons.append(f"{tone}用户���到安�?")

        # 5. 空洞/模板回��€�?
        # 5. 空洞/模板回复检测
        _EMPTY_PATTERNS = ("如有其他问题", "请问还有什么", "希望以上信息")
        empty_count = sum(1 for p in _EMPTY_PATTERNS if p in reply)
        if empty_count >= 2 and rlen < 100:
            score -= 10
            reasons.append("模板化回�?")

        score = max(0, min(100, round(score)))
        ctx["_reply_quality"] = score

        if score < self._QUALITY_LOW_THRESHOLD:
            self.logger.warning(
                "[G4-LowQuality] user=%s score=%d intent=%s reasons=%s reply='%s'",
                user_id, score, intent, "|".join(reasons), reply[:80]
            )
            ctx["_low_quality_flag"] = True
            # F1: 分类����
            self._auto_fix_low_quality(ctx, reasons, intent)
        else:
            ctx.pop("_low_quality_flag", None)

    def _auto_fix_low_quality(self, ctx: Dict[str, Any],
                              reasons: list, intent: str):
        """F1: 低质量回复自动触发修复动作（异� fire-and-forget�?"""
        try:
            _kb = self._kb_store_if_exists()
            if not _kb:
                return
            user_msg = (ctx.get("last_message") or "").strip()
            ai_reply = (ctx.get("last_reply") or "").strip()
            if not user_msg:
                return

            has_kb = bool(ctx.get("kb_context"))
            if not has_kb and "无KB命中" in reasons:
                from src.utils.kb_gate import should_log_kb_miss
                if should_log_kb_miss(user_msg):
                    _kb.log_miss(user_msg)
                    self.logger.info("[F1] 无KB命中低分 �?miss_log: '%s'", user_msg[:50])
            elif has_kb:
                _kb.add_feedback({
                    "user_message": user_msg[:200],
                    "ai_reply": ai_reply[:300],
                    "score": -1,
                    "correction": "",
                    "operator": "auto_quality",
                })
                self.logger.info("[F1] 有KB但低�?�?负面反�: '%s'", user_msg[:50])

            if "..." in reasons:
                _kb.add_feedback({
                    "user_message": user_msg[:200],
                    "ai_reply": ai_reply[:300],
                    "score": -1,
                    # "correction": "模板化回复，�€丰富内�",
                    "operator": "auto_quality",
                })
        except Exception as _e:
            self.logger.debug("F1 ����异常: %s", _e)

    # �€�€ J1: 智能人工升级 �€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€
    _ESCALATION_COOLDOWN_SEC = 3600  # 同一用户 1 小时内最多触�?1 �?
    def _check_escalation(self, user_id: str, ctx: Dict[str, Any],
                          chat_id: Any, now: float):
        """�?at_risk 用户连续���决时，触发人工升级�€�知�?
        触发条件（全部满足）�?          1. at_risk = True（满意度 < 40�?          2. 连续同意图追�?>= 3 �?          3. 距上次升�?> 1 小时
        """
        profile = ctx.get("_user_profile", {})
        if not profile.get("at_risk"):
            return
        consecutive = ctx.get("_consecutive_same_intent", 0)
        if consecutive < 3:
            return
        last_esc = self._escalation_cooldown.get(user_id, 0)
        if now - last_esc < self._ESCALATION_COOLDOWN_SEC:
            return

        self._escalation_cooldown[user_id] = now
        ctx["_escalation_triggered"] = True
        ctx["_escalation_ts"] = now
        # Case-center：升级触发即开案/升级（webhook 只响一声，案例留到有人处理）。
        try:
            from src.utils.case_center import open_case
            open_case(ctx, str(user_id), "escalation",
                      "case.reason.escalation", {"n": int(consecutive)},
                      quote=str(ctx.get("last_message") or ""))
        except Exception:
            pass

        sat = profile.get("satisfaction", 0)
        intent = ctx.get("current_intent", "unknown")
        last_msg = (ctx.get("last_message") or "")[:100]
        chat_title = ctx.get("chat_title", "")

        self.logger.warning(
            "[J1-Escalation] user=%s sat=%s intent=%s consecutive=%s chat=%s msg='%s'",
            user_id, sat, intent, consecutive, chat_title, last_msg
        )

        #通过 webhook 通知（异�?fire-and-forget�?
        try:
            import asyncio
            loop = asyncio.get_running_loop()
            if loop.is_running():
                loop.create_task(self._fire_escalation_webhook(
                    user_id, chat_id, sat, intent, consecutive, last_msg, chat_title
                ))
        except Exception:
            pass

    async def _fire_escalation_webhook(self, user_id, chat_id, sat,
                                        intent, consecutive, last_msg, chat_title):
        """发�€�人工升�?webhook 通知（独立实现，不依�?admin.py�?"""
        try:
            cfg_dir = Path(self.config.config_path).parent if hasattr(self.config, "config_path") else Path("config")
            wh_path = cfg_dir / "webhook_settings.json"
            if not wh_path.exists():
                return
            wh_cfg = json.loads(wh_path.read_text(encoding="utf-8"))
            if not wh_cfg.get("enabled") or not wh_cfg.get("url"):
                return
            events = wh_cfg.get("events", [])
            if "escalation_needed" not in events and "config_change" not in events:
                return
            import httpx
            payload = json.dumps({
                "event": "escalation_needed",
                "actor": "system",
                "target": f"user:{user_id}",
                "summary": (
                    # f"�?人工升级请求\n" f"用户: {user_id}\n" f"群组: {chat_title or chat_id}\n" f"满意�? {sat}/100\n"
                    f"连续追问: {consecutive} �?({intent})\n"
                    # f"�€近消�? {last_msg}"
                ),
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, ensure_ascii=False)
            headers = {"Content-Type": "application/json"}
            async with httpx.AsyncClient(timeout=8) as client:
                await client.post(wh_cfg["url"], content=payload, headers=headers)
        except Exception as e:
            self.logger.debug("J1 升级 webhook 发�€�失�? %s", e)

    # ── R8: 危机人工接管/升级 ───────────────────────────────────────────
    _CRISIS_ESCALATION_COOLDOWN_SEC = 1800  # 危机比普通升级更急，30 分钟冷却

    def _maybe_escalate_crisis(
        self, *, user_id: str, chat_id: Any,
        user_context: Dict[str, Any], log_prefix: str = "",
    ) -> None:
        """R8：severe 危机连续命中 → 触发人工接管告警（复用既有 escalation webhook）。

        始终维护危机连击计数（severe 自增、非危机清零、elevated 维持）；仅当
        ``companion.wellbeing.crisis_escalation`` 开（默认关，需配 webhook + 真人值守）
        且连击 ≥ ``escalate_after``（默认 1）且过冷却时才真正告警。纯旁路，任何异常不影响回复。
        """
        try:
            level = str(user_context.get("_wellbeing_crisis_level", "") or "")
            streak = int(user_context.get("_wellbeing_crisis_streak", 0) or 0)
            if level == "severe":
                streak += 1
            elif level != "elevated":
                streak = 0
            user_context["_wellbeing_crisis_streak"] = streak
            # Case-center：severe 危机一律开案/升级（独立于 crisis_escalation 告警
            # 开关——告警要不要外发是运营决策，「有人该看一眼」是事实本身）。
            if level == "severe":
                try:
                    from src.utils.case_center import open_case
                    open_case(user_context, str(user_id), "crisis",
                              "case.reason.crisis",
                              quote=str(user_context.get("last_message") or ""))
                except Exception:
                    pass
            # safety_override 是上一步(_apply_crisis_safety_net)的本轮信号，读后清零
            safety_override = bool(user_context.pop("_wellbeing_safety_override", False))

            _cfg = self.config.config if hasattr(self.config, "config") else {}
            _wb = (
                ((_cfg.get("companion") or {}).get("wellbeing") or {})
                if isinstance(_cfg, dict) else {}
            )
            wb_enabled = bool(_wb.get("enabled", True))

            escalated_now = False
            if wb_enabled and _wb.get("crisis_escalation", False) and level == "severe":
                escalate_after = max(1, int(_wb.get("escalate_after", 1)))
                if streak >= escalate_after:
                    now = time.time()
                    last = self._crisis_escalation_cooldown.get(str(user_id), 0.0)
                    if now - last >= self._CRISIS_ESCALATION_COOLDOWN_SEC:
                        self._crisis_escalation_cooldown[str(user_id)] = now
                        user_context["_crisis_escalation_triggered"] = True
                        user_context["_crisis_escalation_ts"] = now
                        escalated_now = True
                        self.logger.warning(
                            "%s[wellbeing] 危机人工接管触发 user=%s streak=%s"
                            "（已告警/待真人介入）",
                            log_prefix, user_id, streak,
                        )
                        try:
                            loop = asyncio.get_running_loop()
                            if loop.is_running():
                                loop.create_task(self._fire_crisis_webhook(
                                    user_id, chat_id, streak,
                                    str(user_context.get("chat_title", "") or ""),
                                ))
                        except RuntimeError:
                            pass

            # R9 审计落库（独立于升级开关；默认关，由 crisis_audit 控制）
            if (
                wb_enabled and _wb.get("crisis_audit", False)
                and level in ("severe", "elevated")
                and getattr(self, "_crisis_store", None) is not None
            ):
                try:
                    self._crisis_store.record(
                        user_id=str(user_id), chat_id=str(chat_id), level=level,
                        category=str(user_context.get("_wellbeing_crisis_category", "") or ""),
                        streak=streak, escalated=escalated_now,
                        safety_override=safety_override,
                        excerpt=str(user_context.get("last_message", "") or ""),
                    )
                except Exception:
                    self.logger.debug("[wellbeing] crisis audit record failed", exc_info=True)
        except Exception:
            self.logger.debug("[wellbeing] crisis escalation skipped", exc_info=True)

    async def _fire_crisis_webhook(self, user_id, chat_id, streak, chat_title):
        """危机告警 webhook（复用 escalation_needed 事件通道，附 category=crisis）。"""
        try:
            cfg_dir = (
                Path(self.config.config_path).parent
                if hasattr(self.config, "config_path") else Path("config")
            )
            wh_path = cfg_dir / "webhook_settings.json"
            if not wh_path.exists():
                return
            wh_cfg = json.loads(wh_path.read_text(encoding="utf-8"))
            if not wh_cfg.get("enabled") or not wh_cfg.get("url"):
                return
            events = wh_cfg.get("events", [])
            if "escalation_needed" not in events and "config_change" not in events:
                return
            import httpx
            payload = json.dumps({
                "event": "escalation_needed",
                "category": "crisis",
                "severity": "high",
                "actor": "system",
                "target": f"user:{user_id}",
                "summary": (
                    f"⚠️ 危机人工接管请求\n"
                    f"用户: {user_id}\n群组: {chat_title or chat_id}\n"
                    f"连续危机信号: {streak} 次（疑似自伤/轻生）\n"
                    f"请尽快人工介入。"
                ),
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, ensure_ascii=False)
            headers = {"Content-Type": "application/json"}
            async with httpx.AsyncClient(timeout=8) as client:
                await client.post(wh_cfg["url"], content=payload, headers=headers)
        except Exception as e:
            self.logger.debug("R8 危机 webhook 发送失败: %s", e)

    def _cleanup_cache(self):
        """清理过期的缓存条�?"""
        current_time = time.time()
        expire_time = current_time - 3600

        keys_to_remove = [k for k, ts in self.reply_cache.items() if ts < expire_time]
        for key in keys_to_remove:
            del self.reply_cache[key]

        cu_expire = current_time - 300
        cu_stale = [k for k, ts in self._chat_user_last_reply.items() if ts < cu_expire]
        for k in cu_stale:
            del self._chat_user_last_reply[k]

        if len(self._user_locks) > 200:
            unlocked = [k for k, v in self._user_locks.items() if not v.locked()]
            for k in unlocked[:100]:
                del self._user_locks[k]

    async def cleanup(self):
        """清理资源"""
        self._context_store.flush_all()
        self._context_store.close()
        if getattr(self, "_episodic_store", None):
            try:
                self._episodic_store.close()
            except Exception:
                pass
        self.logger.info("...")
# ==================== Generic Skills ====================
# Skill base class is imported from src.skills.base


class GreetingSkill(Skill):
    """问候处理技能 — 支持 S1 列表配置（跳过 AI 秒回）和标准 AI 回复"""

    def __init__(self, config, ai_client):
        super().__init__(config, ai_client)
        self.priority = 1

    _IDENTITY_KW = (
        "你是谁", "你是什么", "哪个客服", "什么客服", "介绍一下", "你们是机器人", "你是ai",
        "who are you", "are you a bot", "are you ai", "are you real",
    )

    async def execute(self, text: str, user_id: str, context: Dict[str, Any]) -> Optional[str]:
        strategy = context.get('_reply_strategy', {})
        _txt = (text or '').lower()
        _is_identity = any(k in _txt for k in self._IDENTITY_KW)
        _lang = (context or {}).get('reply_lang', 'zh')

        if strategy.get('skip_ai') and not context.get('kb_context') and not _is_identity:
            return self._kb_fallback('greeting', lang=_lang)

        so = {}
        if 'temperature' in strategy:
            so['temperature'] = strategy['temperature']
        raw_tokens = strategy.get('max_tokens', 0)
        if isinstance(raw_tokens, (int, float)) and raw_tokens < 256:
            so['max_tokens'] = 256
        elif raw_tokens:
            so['max_tokens'] = int(raw_tokens)
        if 'context_rounds' in strategy:
            so['context_rounds'] = strategy['context_rounds']

        try:
            reply = await self.ai_client.generate_reply_with_intent(
                user_message=text, intent='greeting',
                user_context=context, strategy_overrides=so or None
            )
            if reply:
                return reply
        except Exception as e:
            self.logger.warning(f"AI生成问候回复失败: {e}")

        # 2026-08-15：AI 失败不再落罐头兜底（可以不回复，不能乱回复）。
        # skip_ai 秒回路径不受影响——那是刻意设计的即答，不是失败掩饰。
        return None


class ComplaintSkill(Skill):
    """投诉处理技能"""

    def __init__(self, config, ai_client):
        super().__init__(config, ai_client)
        self.priority = 7

    async def execute(self, text: str, user_id: str, context: Dict[str, Any]) -> Optional[str]:
        _lang = (context or {}).get('reply_lang', 'zh')
        try:
            reply = await self.ai_client.generate_reply_with_intent(
                text, 'complaint',
                context,
                strategy_overrides=self._get_strategy_overrides(context)
            )
            if reply:
                return reply
        except Exception as e:
            self.logger.warning(f"AI生成投诉处理回复失败: {e}")

        # 2026-08-15：AI 失败不再落罐头兜底（可以不回复，不能乱回复）。
        return None


class SmallTalkSkill(Skill):
    """闲聊技能 — 支持 S5 静默观察（概率不回复由 SkillManager 控制）"""

    def __init__(self, config, ai_client):
        super().__init__(config, ai_client)
        self.priority = 8

    async def execute(self, text: str, user_id: str, context: Dict[str, Any]) -> Optional[str]:
        strategy = context.get('_reply_strategy', {})
        _lang = (context or {}).get('reply_lang', 'zh')

        if strategy.get('skip_ai') and not context.get('kb_context'):
            return self._kb_fallback('small_talk', lang=_lang)

        try:
            reply = await self.ai_client.generate_reply_with_intent(
                user_message=text, intent='small_talk',
                user_context=context,
                strategy_overrides=self._get_strategy_overrides(context)
            )
            if reply:
                return reply
        except Exception as e:
            self.logger.warning(f"AI生成闲聊回复失败: {e}")

        # 2026-08-15：AI 失败不再落罐头兜底（可以不回复，不能乱回复）。
        return None


class TestSkill(Skill):
    """测试功能技能"""

    def __init__(self, config, ai_client):
        super().__init__(config, ai_client)
        self.priority = 2

    async def execute(self, text: str, user_id: str, context: Dict[str, Any]) -> Optional[str]:
        if "测试" not in text and "test" not in text.lower():
            return None

        try:
            reply = await self.ai_client.generate_reply_with_intent(
                text, 'test',
                context,
                strategy_overrides=self._get_strategy_overrides(context)
            )
            if reply:
                return reply
        except Exception as e:
            self.logger.warning(f"AI生成测试回复失败: {e}")

        r = self._kb_reply('test_reply')
        if r:
            return r
        # 2026-08-15：AI 失败且无 KB 模板 → 不回复（可以不回复，不能乱回复）。
        return None


# 供测试与外部统一从 skill_manager 导入（实现仍在 domains.payment.skills.enhanced_quota_config）
from domains.payment.skills.enhanced_quota_config import EnhancedQuotaConfigSkill  # noqa: E402
