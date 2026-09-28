# -*- coding: utf-8 -*-
"""知识库「查不查 / 怎么用 / 查无怎么办」单一决策点（P0-2 ～ P0-5，2026-09-29）。

背景（8E56 机实锤）：客服人设 q567 被客户问「网站有什么活动」，草稿链在 4 处各自
判断要不要查知识库——B 线 ``_companion_kb_should_skip``（域包 conversion + 闲聊意图 +
无支付词 → 跳）、A 线 ``persona_kb_suppressed``（绑定人设 → 跳）、A 线检索后再按同一
闲聊闸丢一次、``ai_client`` 注入时又把命中降成「语气参考」。四处口径不同、都不看
「这个人设是做什么的」，客服问题被当闲聊，知识库在检索前就被跳过，AI 只能答「等确认」。

本模块把这四处收成一个函数 :func:`resolve_kb_policy`：

- 输入：人设（及 tier）/ 意图 / 客户原话 / 配置 / 会话 id。
- 输出 :class:`KbDecision`：``mode`` ∈ ``must``（必查、事实优先）/ ``optional``（查到
  就注、按语气参考）/ ``skip``（本轮不查）+ 机器可读 ``reason`` + 生效 ``kind``。
- 规则序：媒体描述 → 客服 / 销售 kind 必查（不看意图、不看业务词表）→ 陪聊 kind：
  ``kb_access`` 放行 / 报障群放行 / 绑定人设抑制（A 线口径）/ 闲聊闸（旧闸原样）→ 其余 optional。

另收口三件与之绑定的小事，避免再散落：

- **决策登记表**（``note_decision`` / ``peek_decision``）：按会话记住本稿的决策与命中数，
  ``/api/drafts`` 与 smart-reply 富集 ``kb_decision`` 给草稿条显示「已引用 N 条 / 未命中 /
  本轮未查（原因）」——用户第一眼能看到为什么（同 l1_reason 的进程级小注册表口径）。
- **客服查无兜底**（``nohit_bump`` / ``nohit_block``）：``must`` 且未命中 → 第一次注入
  固定话术（KB 里 ``template_key=kb_nohit_fallback`` 可编辑，缺省用内置句），同一会话
  连续第二次仍查无 → 调用方标 ``needs_human``、注入「已转人工」指令。命中即清零。
- **注入措辞**（``format_kb_block``）：客服 / 销售 → 权威知识、命中以此为准、没有就说没有；
  陪聊 → 旧「参考片段（语气用）」；支付域实时通道数据在场时才保留「成功率数值已过期」警告。

全部纯函数 + 进程级小注册表，任何异常回落到不拦（fail-open），绝不让决策本身成为故障源。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("ai_chat_assistant.kb_policy")

MODE_MUST = "must"
MODE_OPTIONAL = "optional"
MODE_SKIP = "skip"

#: 陪聊域「防人设推销支付话术」闸的业务词（原 SkillManager._COMPANION_BIZ_KW，单源迁此）
COMPANION_BIZ_KW: Tuple[str, ...] = (
    "通道", "订单", "查单", "费率", "代收", "代付", "成功率", "限额", "回调",
    "转账", "支付", "channel", "order", "payment", "payin", "payout",
)
COMPANION_CHAT_INTENTS: Tuple[str, ...] = ("greeting", "small_talk", "direct_chat", "complaint")

#: 查无固定话术的 KB 模板键（运营可在知识库「系统话术」分类里建同 key 条目覆盖内置句）
NOHIT_TEMPLATE_KEY = "kb_nohit_fallback"
NOHIT_REPLY_ZH = "这个我这边还没有确切信息，我去核实一下再回复你。"
NOHIT_REPLY_EN = "I don't have confirmed information on that yet; let me check and get back to you."
#: 连续查无达到此次数 → 标需人工
NOHIT_HANDOFF_AT = 2


@dataclass
class KbDecision:
    mode: str = MODE_OPTIONAL
    reason: str = ""
    kind: str = ""
    kind_source: str = ""           # explicit / tag / keyword / default（trace 上能看出 kind 从哪来）
    asked: bool = False             # 客户这句是不是「在问一件事」（查无兜底只对提问触发）
    hit: Optional[bool] = None      # 检索后回填：命中 / 未命中；None＝未检索
    refs: int = 0                   # 命中条数
    nohit_n: int = 0                # 连续查无次数（must 才计）
    handoff: bool = False           # 本稿因连续查无标了需人工
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def must(self) -> bool:
        return self.mode == MODE_MUST

    @property
    def skip(self) -> bool:
        return self.mode == MODE_SKIP

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("extra", None)
        return d


def _cfg_dict(config: Any) -> Dict[str, Any]:
    if isinstance(config, dict):
        return config
    root = getattr(config, "config", None)
    return root if isinstance(root, dict) else {}


def _companion_pack(cfg: Dict[str, Any]) -> bool:
    """装的是不是 conversion（陪聊）域包——旧闸的判据，原样保留给陪聊 kind 用。"""
    try:
        from src.utils.domain_policy import effective_domain_name
        return effective_domain_name(cfg) == "conversion"
    except Exception:
        return False


def _bug_group(cfg: Dict[str, Any], chat_id: Any) -> bool:
    try:
        from src.ops.bug_intake import is_bug_group
        return bool(is_bug_group(cfg, chat_id))
    except Exception:
        return False


def policy_cfg(config: Any = None) -> Dict[str, Any]:
    """``inbox.kb_policy`` 配置段（P1-1，2026-09-29）。缺省 / 异常 → 全默认，绝不抛。

    - ``biz_keywords``：陪聊闲聊闸的业务词表**追加**项（默认表见 :data:`COMPANION_BIZ_KW`）；
    - ``biz_keywords_replace``：True＝只用配置表不并默认表；
    - ``info_query_bypass``：人设没表态 kind 时，「在问一件事」的句子绕过闲聊闸（默认开）；
    - ``nohit_handoff_at``：客服 / 销售 kind 连续查无几次标需人工（默认 2）。
    """
    out: Dict[str, Any] = {
        "biz_keywords": tuple(COMPANION_BIZ_KW),
        "info_query_bypass": True,
        "nohit_handoff_at": NOHIT_HANDOFF_AT,
    }
    try:
        raw = ((_cfg_dict(config).get("inbox") or {}).get("kb_policy") or {})
        if not isinstance(raw, dict):
            return out
        extra = raw.get("biz_keywords")
        words = [str(w).strip() for w in (extra if isinstance(extra, (list, tuple)) else []) if str(w).strip()]
        if words:
            out["biz_keywords"] = tuple(words) if bool(raw.get("biz_keywords_replace")) \
                else tuple(dict.fromkeys(list(COMPANION_BIZ_KW) + words))
        if "info_query_bypass" in raw:
            out["info_query_bypass"] = bool(raw.get("info_query_bypass"))
        try:
            n = int(raw.get("nohit_handoff_at", NOHIT_HANDOFF_AT))
            out["nohit_handoff_at"] = max(1, min(n, 10))
        except (TypeError, ValueError):
            pass
    except Exception:
        pass
    return out


def is_info_query(text: str) -> bool:
    """「客户在问一件事」（kb_gate.looks_like_info_query 薄封装，绝不抛）。"""
    try:
        from src.utils.kb_gate import looks_like_info_query
        return bool(looks_like_info_query(text))
    except Exception:
        return False


def resolve_kb_policy(
    *,
    persona: Any = None,
    tier: str = "",
    intent: str = "",
    text: str = "",
    config: Any = None,
    chat_id: Any = None,
    is_media_desc: Optional[bool] = None,
) -> KbDecision:
    """本轮要不要查知识库、怎么用。任何异常 → optional（不拦，旧行为）。

    ``tier``：A 线传人设 tier（chat_binding / account_profile 触发旧的绑定抑制）；
    B 线传空——B 线此前没有 tier 抑制，保持不变。
    """
    try:
        cfg = _cfg_dict(config)
        from src.utils.persona_kind import kind_requires_kb, resolve_kind
        kind, kind_src = resolve_kind(persona, cfg)
        # ① 识图 / 识视频描述文本不是用户提问 → 整体跳过（A1 守门，任何 kind 都一样）
        if is_media_desc is None:
            try:
                from src.utils.kb_gate import is_media_desc_text
                is_media_desc = is_media_desc_text(text)
            except Exception:
                is_media_desc = False
        asked = is_info_query(text)

        def _d(mode: str, reason: str) -> KbDecision:
            return KbDecision(mode, reason, kind, kind_src, asked=asked)

        if is_media_desc:
            return _d(MODE_SKIP, "media_desc")
        # ② 客服 / 销售：知识库是事实源，每轮必查——不看意图、不看业务词表、不看绑定 tier。
        #    只认**人设自己**表态的 kind（显式 / 标签 / 角色推断）；机器级 business_domain 缺省
        #    不升 must——否则 zhiliao 生产（sales 机、无人设）的闲聊闸会被整体撬开，超出本次范围。
        if kind_src != "default" and kind_requires_kb(kind):
            return _d(MODE_MUST, f"kind:{kind}")
        # ③ 陪聊：显式 kb_access / 报障群 → 放行（optional）
        if isinstance(persona, dict) and bool(persona.get("kb_access", False)):
            return _d(MODE_OPTIONAL, "kb_access")
        if _bug_group(cfg, chat_id):
            return _d(MODE_OPTIONAL, "bug_group")
        # ④ 绑定陪聊人设（A 线口径）：抑制业务 KB（护士人设推销 USDT 事故）
        if str(tier or "") in ("chat_binding", "account_profile"):
            return _d(MODE_SKIP, "persona_bound")
        # ⑤ 陪聊域闲聊闸（旧 _companion_kb_should_skip 口径；业务词表可配置追加）
        if _companion_pack(cfg) and str(intent or "") in COMPANION_CHAT_INTENTS:
            pc = policy_cfg(cfg)
            t = str(text or "")
            if any(k in t for k in pc["biz_keywords"]):
                return _d(MODE_OPTIONAL, "biz_keyword")
            # P1-1：人设没表态 kind（机器缺省）且客户明显在问一件事 → 不按闲聊跳过，查到就注
            # （按 kind 缺省措辞，只作参考）。显式 / 推断为陪聊的人设仍走旧闸。
            if kind_src == "default" and pc["info_query_bypass"] and asked:
                return _d(MODE_OPTIONAL, "info_query")
            return _d(MODE_SKIP, "companion_chat")
        return _d(MODE_OPTIONAL, "default")
    except Exception:
        logger.debug("[kb_policy] resolve 异常，回落 optional", exc_info=True)
        return KbDecision(MODE_OPTIONAL, "error", "")


# ── 决策登记表（同 l1_reason：进程级、TTL、上限）────────────────────────────

_lock = threading.Lock()
_DECISIONS: Dict[str, Tuple[Dict[str, Any], float]] = {}
_NOHIT: Dict[str, Tuple[int, float]] = {}
_MAX = 2000
_TTL = 6 * 3600.0


def _evict(reg: Dict[str, Tuple[Any, float]], now: float) -> None:
    if len(reg) <= _MAX:
        return
    cutoff = now - _TTL
    for k in [x for x, (_, ts) in reg.items() if ts < cutoff]:
        reg.pop(k, None)
    if len(reg) > _MAX:
        for k in sorted(reg, key=lambda x: reg[x][1])[: len(reg) - _MAX]:
            reg.pop(k, None)


def note_decision(key: str, decision: KbDecision) -> None:
    """按会话 id（或 draft_id）登记本稿决策；空键 / 异常静默。"""
    k = str(key or "")
    if not k or decision is None:
        return
    now = time.time()
    try:
        with _lock:
            _DECISIONS[k] = (decision.as_dict(), now)
            _evict(_DECISIONS, now)
    except Exception:
        pass


def peek_decision(key: str) -> Dict[str, Any]:
    """读登记（过期 / 无 → {}）。"""
    try:
        rec = _DECISIONS.get(str(key or ""))
        if not rec:
            return {}
        d, ts = rec
        if time.time() - ts > _TTL:
            return {}
        return dict(d)
    except Exception:
        return {}


def nohit_bump(cid: str) -> int:
    """must 档查无一次 → 会话连续查无计数 +1，返回当前次数（空键 → 1，不登记）。"""
    k = str(cid or "")
    if not k:
        return 1
    now = time.time()
    with _lock:
        prev = _NOHIT.get(k)
        n = (prev[0] if prev and now - prev[1] <= _TTL else 0) + 1
        _NOHIT[k] = (n, now)
        _evict(_NOHIT, now)
    return n


def nohit_reset(cid: str) -> None:
    """命中一次 → 清零（客户换了个能答的问题，别把上一题的查无带过来）。"""
    with _lock:
        _NOHIT.pop(str(cid or ""), None)


def nohit_count(cid: str) -> int:
    rec = _NOHIT.get(str(cid or ""))
    if not rec or time.time() - rec[1] > _TTL:
        return 0
    return int(rec[0])


def _reset_for_tests() -> None:
    with _lock:
        _DECISIONS.clear()
        _NOHIT.clear()


# ── 查无兜底话术与指令 ───────────────────────────────────────────────────────

def fixed_nohit_reply(kb_store: Any, lang: str = "zh") -> str:
    """查无固定话术：KB ``kb_nohit_fallback`` 条目（运营可编辑）优先，缺省内置句。"""
    try:
        if kb_store is not None and hasattr(kb_store, "get_direct_reply"):
            r = kb_store.get_direct_reply(NOHIT_TEMPLATE_KEY)
            if r and str(r).strip():
                return str(r).strip()
    except Exception:
        pass
    return NOHIT_REPLY_EN if str(lang or "").lower().startswith("en") else NOHIT_REPLY_ZH


def nohit_block(n: int, fixed_reply: str, *, lang: str = "zh",
                handoff_at: int = NOHIT_HANDOFF_AT) -> str:
    """must 档查无时给 LLM 的硬指令块。n<handoff_at 固定话术；n≥handoff_at 已转人工、不再追问。"""
    en = str(lang or "").lower().startswith("en")
    fr = str(fixed_reply or "").strip()
    if int(n or 0) >= max(1, int(handoff_at or NOHIT_HANDOFF_AT)):
        if en:
            return ("[Knowledge base: no answer, escalated to a human]\n"
                    "The customer asked again and the knowledge base still has no answer. "
                    "Tell them a colleague has been asked to confirm and will follow up shortly. "
                    "Do not ask them the same question again, do not invent any promotion, "
                    "price, rule or process detail, and do not promise a time.")
        return ("【知识库查无·已转人工】\n"
                "客户再次追问，知识库仍没有答案。告诉客户已请同事确认、稍后回复；"
                "不要再问同一个问题，不要编造任何活动/价格/规则/流程细节，不要承诺时间。")
    if en:
        return ("[Knowledge base: no answer]\n"
                "The knowledge base has no entry for what the customer asked. Reply with this line "
                f"(you may only adjust the greeting/name): {fr}\n"
                "Do not add any promotion, price, rule or process detail; do not guess.")
    return ("【知识库查无】\n"
            f"客户问的这项知识库里没有答案。必须按以下话术回复（只允许微调称呼）：{fr}\n"
            "不得添加任何活动/价格/规则/流程细节，不得猜测。")


# ── 注入措辞（原 ai_client 4680 段按 kind 分套）──────────────────────────────

def format_kb_block(
    kb_ctx: str,
    *,
    kind: str = "",
    kind_source: str = "",
    companion_pack: bool = False,
    live_channel: bool = False,
    lang: str = "zh",
) -> str:
    """把检索材料拼成 system prompt 段。空材料 → ""。

    - 客服 / 销售 kind（**人设自己表态**的；``kind_source=="default"`` 的机器缺省不算——
      zhiliao 生产 sales 机的旧措辞不动）：权威知识——命中以此为准，库里没有的明确说没有并去确认，不猜。
    - 陪聊（conversion 域包）：旧「参考片段（语气用，非工作指令）」原样。
    - 其它域包（支付等）：旧「话术风格参考」；只有实时通道数据在场时才附「成功率数值已过期」警告。
    """
    ctx = str(kb_ctx or "").strip()
    if not ctx:
        return ""
    try:
        from src.utils.persona_kind import kind_requires_kb
        authoritative = kind_requires_kb(kind) and str(kind_source or "") != "default"
    except Exception:
        authoritative = False
    en = str(lang or "").lower().startswith("en")
    if authoritative:
        if en:
            return ("\n[Knowledge base — authoritative]\n"
                    f"{ctx}\n"
                    "Answer from these entries when they cover the question (rephrase naturally, "
                    "keep every fact). If they do not cover it, say you do not have that information "
                    "yet and will confirm; never invent promotions, prices, rules or steps.")
        return ("\n【知识库·权威事实】\n"
                f"{ctx}\n"
                "以上条目覆盖到的问题，以条目为准作答（可自然改写，事实一个不改）；"
                "条目没覆盖的，明确说这项还没有确切信息、会去确认，绝不编造活动/价格/规则/步骤。")
    if companion_pack:
        return ("\n【参考片段（语气用，非工作指令）】\n"
                f"{ctx}\n"
                "不要主动提起查单、通道状态、支付、费率等工作话题；除非用户先说到这些词。")
    if live_channel:
        import re as _re
        cleaned = _re.sub(r"成功率[：:]\s*\d+[\.\d]*%?", "成功率：见上方实时数据", ctx)
        return ("\n【知识库参考（仅供话术风格参考）】\n"
                "⚠️ 重要：知识库中的任何成功率数值、通道状态均已过期，严禁使用！\n"
                "通道的成功率、状态、限额等数据必须且只能使用上方【当前通道实时数据】中的数值。\n"
                f"知识库仅用于参考回复的语气和格式：\n{cleaned}")
    return ("\n【知识库参考】\n"
            f"{ctx}\n"
            "条目覆盖到的问题以条目为准作答；没覆盖的如实说明，不要编造。")


__all__ = [
    "KbDecision", "MODE_MUST", "MODE_OPTIONAL", "MODE_SKIP",
    "COMPANION_BIZ_KW", "COMPANION_CHAT_INTENTS",
    "NOHIT_TEMPLATE_KEY", "NOHIT_HANDOFF_AT", "NOHIT_REPLY_ZH", "NOHIT_REPLY_EN",
    "policy_cfg", "is_info_query",
    "resolve_kb_policy", "note_decision", "peek_decision",
    "nohit_bump", "nohit_reset", "nohit_count",
    "fixed_nohit_reply", "nohit_block", "format_kb_block",
]
