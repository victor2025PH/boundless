# -*- coding: utf-8 -*-
"""出站收口点统一守卫（实施91 · #97/#105/#106 三张 P1 击穿单的同一个答案）。

三案同根：守卫只装在**部分**出站路径——#64 混语守卫罩了翻译出口、直发链裸奔
（#97「I'm 我」四连报）；#24 呼格守卫罩了 A/B 出稿口、语音念稿链裸奔（#105
英文客户听到「…without you, baba」，档案明记偏好 babe）；#74 图片轮语言锚
罩了语言检测源、A 线原生链从不消费 B67「发→X」铆定（#106 铆英仍三连纯中文）。

本模块＝「全平台全链共过的 send 前收口点」所需的**带上下文**校验（呼格 /
铆定语言）。纯文本确定性校验（混语剥除）留在 ``outbound_text_guard``
（那边是零依赖纯函数模块，设计原则不掺 resolver）。挂点：

- ``AccountOrchestrator.send``（B 线 autosend / 主动触达 / 关怀 / 唤醒 /
  deferred——全部自动链文本出门必过）；
- A 线 ``sender._send_reply``（原生 Telegram 回复不经编排器）；
- ``TTSPipeline.synthesize``（语音念稿合成前——音频出门后无法再改，必须在
  合成前纠；interactive=坐席手打逐字链豁免，「所打即所念」不变量优先）。

分层契约：生成端守卫（skill_manager `_enforce_persona_consistency` /
`_apply_outbound_text_guard`）原位不动仍是第一道；本收口点是「无论文本从
哪条链来、中途被谁改写过」的最后一道。**人工手打文本绝不动**（origin=manual
由调用方把关不进本模块）。所有函数软失败：异常一律返回原文，绝不拦断发送。
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 观测计数（进程级，风格对齐 outbound_text_guard._STATS） ─────────────────
_STATS: Dict[str, int] = {
    "vocative_swap": 0,        # peer_calls_you → call_peer 互换纠正
    "vocative_near": 0,        # call_peer 近形变体（baba↔babe）纠正
    "vocative_self": 0,        # 人设名当客户呼格剥除
    "lang_pin_conflict": 0,    # 铆定语言 × 文本文字系统冲突检出
    "lang_hint_conflict": 0,   # #133：客户语言画像（无显式铆定）冲突检出
    "lang_hist_conflict": 0,   # #154：出站历史主语种回落（铆定缺位/被清空）冲突检出
    "lang_pin_translated": 0,  # 冲突 → 翻译修正成功
    "lang_pin_hold": 0,        # 冲突 → 翻译 HOLD（无兜底纪律：别发）
    "lang_pin_passthru": 0,    # 冲突但翻译器缺席/失败 → 原样放行
    "voice_vocative": 0,       # 语音合成前呼格纠正（#105 主指标）
    "voice_lang_mix": 0,       # 语音合成前混语剥除
}
_STATS_LOCK = threading.Lock()


def _bump(key: str) -> None:
    try:
        with _STATS_LOCK:
            _STATS[key] = _STATS.get(key, 0) + 1
    except Exception:
        pass


def sendpoint_guard_stats() -> Dict[str, int]:
    with _STATS_LOCK:
        return dict(_STATS)


# ── 呼格守卫上下文解析 ───────────────────────────────────────────────────────

def resolve_sendpoint_names(
    config: Optional[Dict[str, Any]],
    platform: str,
    account_id: str,
    chat_key: str = "",
    *,
    persona_id: str = "",
    registry: Any = None,
) -> Dict[str, Any]:
    """会话/人设 → 呼格守卫所需名字包。软失败返回空 dict（守卫整体不动手）。

    ``persona_id`` 直给（语音链：TTSPipeline 自带）优先；否则经
    ``resolve_effective_persona_id``（出站链「这条会话以谁的身份说话」单一
    事实源——会话覆写 > 账号档案）解析。
    返回：``{"persona_id", "call_peer", "peer_calls_you", "self_names"}``。
    """
    try:
        pid = str(persona_id or "").strip()
        if not pid:
            from src.ai.persona_voice import resolve_effective_persona_id
            pid = resolve_effective_persona_id(
                config or {}, str(platform or ""), str(account_id or ""),
                str(chat_key or ""), registry=registry)
        if not pid:
            return {}
        from src.utils.persona_manager import PersonaManager
        persona = PersonaManager.get_instance().get_persona_by_id(pid)
        if not isinstance(persona, dict):
            return {}
        names = persona.get("names") or {}
        if not isinstance(names, dict):
            names = {}
        self_names: List[str] = []
        try:
            from src.utils.persona_guard import build_self_name_allowlist
            self_names = [
                s for s in build_self_name_allowlist(persona, []) if s]
        except Exception:
            self_names = []
        return {
            "persona_id": pid,
            "call_peer": str(names.get("call_peer") or "").strip(),
            "peer_calls_you": str(names.get("peer_calls_you") or "").strip(),
            "self_names": self_names,
        }
    except Exception:
        return {}


# ── call_peer 近形变体纠正（#105 补强）────────────────────────────────────────
#
# swap_vocative_peer_call 只认「档案里写了的」peer_calls_you；生产实锤里 LLM
# 会漂到**近形**变体（babe→baba/babi/baby）——档案没这个词，互换守卫不认。
# 近形判定收窄到：同长拉丁 token、恰差一个字母、长度 3..6（爱称形态），且只在
# 与互换守卫**同一套呼格位置**（逗号呼格/问候收尾/句首起头）动手，元语句
# （讨论称呼本身）整句跳过——安全包络与 #24 已在产线上验证的同款。

_LATIN_TOKEN_RE = re.compile(r"^[A-Za-z]+$")


def _near_call_peer_variant(token: str, call_peer: str) -> bool:
    """token 是否 call_peer 的一字之差近形（同长、拉丁、恰差 1、长度 3..6）。"""
    t = str(token or "").strip().lower()
    cp = str(call_peer or "").strip().lower()
    if not (3 <= len(cp) <= 6) or len(t) != len(cp) or t == cp:
        return False
    if not (_LATIN_TOKEN_RE.match(t) and _LATIN_TOKEN_RE.match(cp)):
        return False
    return sum(1 for a, b in zip(t, cp) if a != b) == 1


def near_call_peer_fix(
    text: str, call_peer: str, peer_names: Optional[List[str]] = None,
) -> Tuple[str, List[str]]:
    """呼格位置的 call_peer 近形变体 → call_peer（#105「baba 形纠回 babe」）。

    只扫呼格形态（与 swap_vocative_peer_call 共用 ``_addr_voc_patterns`` 句形，
    单一事实源），token 必须过 ``_near_call_peer_variant`` 窄判；对方真名
    恰为近形（客户就叫 Baba）→ 跳过不纠。纯函数绝不抛。
    """
    t = str(text or "")
    cp = str(call_peer or "").strip()
    if not t.strip() or not cp:
        return t, []
    try:
        from src.utils.persona_guard import (
            _addr_voc_patterns, _ADDR_META_RE, _split_sentences_sn,
        )
        peers_norm = {str(p or "").strip().lower()
                      for p in (peer_names or []) if str(p or "").strip()}
        # 候选 token：文本里所有与 call_peer 同长的拉丁词，近形窄判过关的
        candidates = {
            w for w in set(re.findall(r"[A-Za-z]+", t))
            if _near_call_peer_variant(w, cp) and w.lower() not in peers_norm
        }
        if not candidates:
            return t, []
        hits: List[str] = []
        out_sents: List[str] = []
        for sent in _split_sentences_sn(t):
            s = sent
            if _ADDR_META_RE.search(s):
                out_sents.append(s)  # 讨论称呼本身的句子整句放行
                continue
            for cand in candidates:
                for pat in _addr_voc_patterns(cand):
                    m = pat.search(s)
                    if not m:
                        continue
                    got = m.group(1)
                    fixed = cp.capitalize() if got[:1].isupper() else cp
                    hits.append(got)
                    s = s[:m.start(1)] + fixed + s[m.end(1):]
            out_sents.append(s)
        if not hits:
            return t, []
        cleaned = "".join(out_sents)
        return (cleaned if cleaned.strip() else t), hits
    except Exception:
        return t, []


def sendpoint_vocative_pass(
    text: str, names: Dict[str, Any],
) -> Tuple[str, Dict[str, Any]]:
    """收口点呼格三连（确定性、零 LLM、绝不抛）：

    ① peer_calls_you → call_peer 互换（#24 同函数）；
    ② call_peer 近形变体纠正（baba→babe，#105 补强）；
    ③ 人设名当客户呼格剥除（#96-A 同函数，peer 名未知照判）。
    meta = {"swap_hits", "near_hits", "self_voc_hits"}（空列表=未命中）。
    """
    meta: Dict[str, Any] = {"swap_hits": [], "near_hits": [],
                            "self_voc_hits": []}
    src = str(text or "")
    if not src.strip() or not isinstance(names, dict) or not names:
        return src, meta
    out = src
    try:
        cp = str(names.get("call_peer") or "").strip()
        py = str(names.get("peer_calls_you") or "").strip()
        if cp and py:
            from src.utils.persona_guard import swap_vocative_peer_call
            out2, sw = swap_vocative_peer_call(out, cp, py)
            if sw:
                meta["swap_hits"] = sw
                _bump("vocative_swap")
                out = out2 or out
        if cp:
            out3, near = near_call_peer_fix(out, cp)
            if near:
                meta["near_hits"] = near
                _bump("vocative_near")
                out = out3 or out
        self_names = [s for s in (names.get("self_names") or []) if s]
        if self_names:
            from src.utils.persona_guard import strip_vocative_self_name
            out4, voc = strip_vocative_self_name(out, self_names, [])
            if voc:
                meta["self_voc_hits"] = voc
                _bump("vocative_self")
                out = out4 or out
        if not (out or "").strip():
            return src, meta
        return out, meta
    except Exception:
        return src, meta


# ── 出站语言 == 会话铆定语言（#106，B67 explicit 优先）──────────────────────

def outbound_lang_pin(platform: str, account_id: str, chat_key: str) -> str:
    """会话级「发→X」显式铆定语（B67）。无 store/未设置/auto → ""。"""
    try:
        from src.integrations.protocol_bridge import get_inbox_store
        store = get_inbox_store()
        if store is None or not hasattr(store, "get_outbound_lang_if_set"):
            return ""
        from src.inbox.draft_models import _conv_id
        pin = str(store.get_outbound_lang_if_set(_conv_id(
            str(platform or ""), str(account_id or ""),
            str(chat_key or ""))) or "").strip().lower()
        if not pin or pin == "auto":
            return ""
        from src.inbox.outbound_translate import normalize_target
        return normalize_target(pin)
    except Exception:
        return ""


# 出站历史回落（#154）：样本/占比双闸——「我们一直在用什么语言跟他说」要成为
# 事实上的铆定，得有足够多且足够一致的历史，且**最后一条**也是这个语言
#（刚换语言的会话不许被历史多数拽回去）。
_OUT_HIST_WINDOW = 12
_OUT_HIST_MIN_SAMPLES = 3
_OUT_HIST_MIN_SHARE = 2.0 / 3.0


def outbound_history_lang_pin(platform: str, account_id: str,
                              chat_key: str) -> str:
    """显式铆定缺位时的**保守**回落：本会话出站主语种（#154）。

    #154 击穿机制：铆定被程序清空后（前端偏好同步把空的 `_xlateOut` POST 上来
    删了行），收口点回落的是**客户语言画像**——日语客户 × 日语草稿＝零冲突，
    于是四条日语原文在铆 en 的会话里长驱直出。但「我们前面几十条都在说英文」
    本身就是最强的铆定证据，且与客户画像正交。

    判据（宁漏勿误）：窗口内出站可判语言样本 ≥3、主语种占比 ≥2/3、且最近一条
    出站也是该语种。任一不满足 → ""（收口点不动手，与旧行为一致）。软失败绝不抛。
    """
    try:
        from src.integrations.protocol_bridge import get_inbox_store
        store = get_inbox_store()
        if store is None or not hasattr(store, "list_recent_messages"):
            return ""
        from src.inbox.draft_models import _conv_id
        from src.inbox.outbound_translate import normalize_target
        from src.ai.translation_service import detect_language
        cid = _conv_id(str(platform or ""), str(account_id or ""),
                       str(chat_key or ""))
        if not cid:
            return ""
        rows = store.list_recent_messages(cid, limit=_OUT_HIST_WINDOW) or []
        langs: List[str] = []
        for m in rows:
            if not isinstance(m, dict) or str(m.get("direction") or "in") != "out":
                continue
            text = str(m.get("text") or "").strip()
            # 纯媒体占位（[语音]/[图片]…）不构成语言证据——与 vote_language 同口径
            if not text or (text.startswith("[") and text.endswith("]")
                            and " " not in text):
                continue
            try:
                lang = normalize_target(detect_language(text))
            except Exception:
                continue
            if lang:
                langs.append(lang)
        if len(langs) < _OUT_HIST_MIN_SAMPLES:
            return ""
        top = max(set(langs), key=langs.count)
        if langs.count(top) / float(len(langs)) < _OUT_HIST_MIN_SHARE:
            return ""
        if langs[-1] != top:      # 刚切语言 → 历史多数不作数
            return ""
        return top
    except Exception:
        return ""


def outbound_peer_lang_hint(platform: str, account_id: str,
                            chat_key: str) -> str:
    """客户语言画像回落（#133）：无显式铆定时「该用什么语言跟这个客户说」。

    #133 击穿机制：收口点铆定兜底只认 B67 显式铆定——绝大多数会话从没人手动
    设过「发→X」，于是任何一条绕过上游翻译的 proactive 出站（主动关怀/SOP 步/
    目标推进/未来新链）都能把纯中文原样发给英文客户（0901 viva Mexico 实锤：
    12:57 常规回复过翻译出英文、12:53 两条主动关怀直发中文）。

    证据口径与出站翻译/语言硬闸同源（``peer_language_hint``：入站证据加权投票
    → conversations.language 持久列 → 出站历史参照）。判不出 → ""（收口点不动
    手，与旧行为一致）。软失败绝不抛。
    """
    try:
        from src.integrations.protocol_bridge import get_inbox_store
        store = get_inbox_store()
        if store is None:
            return ""
        from src.inbox.draft_models import _conv_id
        from src.inbox.outbound_translate import peer_language_hint
        return peer_language_hint(store, _conv_id(
            str(platform or ""), str(account_id or ""), str(chat_key or "")))
    except Exception:
        return ""


_CJK_FAMILY_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")


def pin_script_conflict(text: str, pin: str) -> bool:
    """铆定语言与文本主体文字系统**明确相斥**才 True（保守，零误报优先）。

    - 非 CJK 铆定（en/es/…）× 实质性 CJK 文本（#106 原形：铆英发纯中文）；
    - CJK 家族铆定 × 纯拉丁长句（字母 ≥12 且零 CJK/假名/谚文）。
    同文字系统内差异（en vs es、zh vs zh-tw）不判——那是翻译层
    ``translate_outbound_text`` 的语义（含变体/同形豁免），不在收口点重造。
    """
    try:
        t = str(text or "")
        if len(t.strip()) < 4:
            return False
        p = str(pin or "").strip().lower()
        if not p:
            return False
        from src.inbox.outbound_translate import cjk_substantial, lang_is_cjk
        if not lang_is_cjk(p):
            return cjk_substantial(t)
        return (len(_LATIN_LETTER_RE.findall(t)) >= 12
                and not _CJK_FAMILY_RE.search(t))
    except Exception:
        return False


# 翻译器注入（bootstrap 期）：async (platform, account_id, chat_key, text)
# -> Optional[str]。main._maybe_translate_outbound 签名完全一致——内部走
# translate_outbound_text（B67 explicit 在场时 gate_only 自动升级完整翻译、
# 「已是客户语言即跳过」、HOLD=None 无兜底纪律），语义单一事实源在那边。
_TRANSLATOR: Optional[Callable[..., Awaitable[Optional[str]]]] = None


def set_sendpoint_translator(
    fn: Optional[Callable[..., Awaitable[Optional[str]]]],
) -> None:
    global _TRANSLATOR
    _TRANSLATOR = fn


async def sendpoint_lang_pin_fix(
    platform: str, account_id: str, chat_key: str, text: str,
) -> Tuple[Optional[str], str]:
    """收口点铆定语言兜底。返回 ``(应发送文本或 None, action)``。

    action ∈ ``""``（无铆定/无冲突/守卫不动手）、``"translated"``（冲突已
    翻译修正）、``"hold"``（翻译 HOLD——无兜底纪律：发错语言比不发更糟，
    调用方按 None 放弃本条）、``"passthru"``（冲突但翻译器缺席/失败，原样
    放行并计数——收口点绝不把消息卡死在自己手里）。

    先做零成本预检（store 铆定读取 + 文字系统冲突），已被上游翻译过的文本
    （autosend/deferred/proactive 三链都先过 translate_outbound_text）在这里
    零额外开销——本兜底只为「从没吃过铆定」的漏网路径（A 线原生 / 未来新链）。

    #133（2026-09-01）：无显式铆定时回落 **客户语言画像**（``outbound_peer_
    lang_hint``，与出站翻译同一证据口径）。冲突判定仍走 ``pin_script_conflict``
    的保守文字系统口径（CJK↔非 CJK 高置信冲突才动手，en/es 之类不判）——
    「英文会话任何主动消息不得出中文」在总出口一次收口，不再依赖每条 proactive
    新链自觉接翻译。真翻译语义仍在注入的 translate_outbound_text（它自己会重
    算目标语/显式铆定/HOLD 纪律），本函数只负责「该不该叫翻译」。
    """
    src = str(text or "")
    try:
        if not src.strip():
            return src, ""
        pin = outbound_lang_pin(platform, account_id, chat_key)
        _pin_kind = "pin" if pin else ""
        if not pin:
            # #154：显式铆定缺位（含「被程序清空」）→ 先认本会话出站主语种，
            # 再认客户语言画像。前者在「铆 en 的日语客户」这类会话里是唯一能
            # 拦住日语原文的证据——客户画像与日语草稿零冲突，判不出问题。
            pin = outbound_history_lang_pin(platform, account_id, chat_key)
            _pin_kind = "hist" if pin else ""
        if not pin:
            pin = outbound_peer_lang_hint(platform, account_id, chat_key)
            _pin_kind = "hint" if pin else ""
        if not pin or not pin_script_conflict(src, pin):
            return src, ""
        if _pin_kind == "hist":
            _bump("lang_hist_conflict")
        elif _pin_kind == "hint":
            _bump("lang_hint_conflict")
        _bump("lang_pin_conflict")
        fn = _TRANSLATOR
        if fn is None:
            _bump("lang_pin_passthru")
            logger.warning(
                "[sendpoint] 出站语言与铆定冲突（pin=%s）但翻译器未注册，"
                "原样放行 %s:%s → %s: %r",
                pin, platform, account_id, chat_key, src[:60])
            return src, "passthru"
        out = await fn(platform, account_id, chat_key, src)
        if out is None:
            _bump("lang_pin_hold")
            logger.warning(
                "[sendpoint] 铆定语言修正 HOLD（无兜底纪律，放弃本条）"
                "pin=%s %s:%s → %s: %r",
                pin, platform, account_id, chat_key, src[:60])
            return None, "hold"
        out_s = str(out)
        if out_s.strip() and out_s != src:
            _bump("lang_pin_translated")
            logger.warning(
                "[sendpoint] 出站语言已按铆定修正（#106）pin=%s %s:%s → %s: "
                "%r → %r", pin, platform, account_id, chat_key,
                src[:50], out_s[:50])
            return out_s, "translated"
        _bump("lang_pin_passthru")
        return src, "passthru"
    except Exception:
        logger.debug("[sendpoint] 铆定语言兜底异常（原样放行）", exc_info=True)
        return src, ""


# ── 语音合成前收口（#105 语音链主修）────────────────────────────────────────

def presynth_text_guard(
    text: str,
    *,
    persona_id: str = "",
    config: Optional[Dict[str, Any]] = None,
    interactive: bool = False,
) -> str:
    """语音念稿合成前守卫：呼格纠正 + 混语剥除（确定性、绝不抛）。

    音频一旦合成便无法再改——文本收口点罩不住语音链（#105 击穿机制），故在
    ``TTSPipeline.synthesize`` 文本定稿处（清洗后、t2s/预渲染/缓存之前）收口，
    纠正后的文本即音频身份（缓存/预渲染键随之走，零错声窗口）。

    ``interactive=True``＝坐席手打逐字链（send-voice/tts-test），「所打即所念」
    不变量优先——整体豁免。开关随 ``companion.outbound_text_guard``
    （enabled + vocative/lang_mix 子键）。
    """
    src = str(text or "")
    if not src.strip() or interactive:
        return src
    try:
        from src.ai.outbound_text_guard import resolve_cfg
        cfg = resolve_cfg(config)
        if not cfg.get("enabled", True):
            return src
        out = src
        if cfg.get("vocative", True):
            names = resolve_sendpoint_names(
                config, "", "", persona_id=str(persona_id or ""))
            if names:
                out2, meta = sendpoint_vocative_pass(out, names)
                if (meta.get("swap_hits") or meta.get("near_hits")
                        or meta.get("self_voc_hits")):
                    _bump("voice_vocative")
                    logger.warning(
                        "[sendpoint] 语音念稿呼格已纠正（#105）persona=%s: "
                        "%r → %r", persona_id, out[:50], out2[:50])
                    out = out2
        if cfg.get("lang_mix", True):
            from src.ai.outbound_text_guard import sendpoint_lang_mix_pass
            out3, act = sendpoint_lang_mix_pass(out)
            if act == "hard_stripped":
                _bump("voice_lang_mix")
                logger.warning(
                    "[sendpoint] 语音念稿混语已剥 CJK（#97 语音面）persona=%s: "
                    "%r → %r", persona_id, out[:50], out3[:50])
                out = out3
        return out if (out or "").strip() else src
    except Exception:
        return src


__all__ = [
    "resolve_sendpoint_names", "sendpoint_vocative_pass", "near_call_peer_fix",
    "outbound_lang_pin", "outbound_peer_lang_hint",
    "outbound_history_lang_pin", "pin_script_conflict",
    "set_sendpoint_translator", "sendpoint_lang_pin_fix",
    "presynth_text_guard", "sendpoint_guard_stats",
]
