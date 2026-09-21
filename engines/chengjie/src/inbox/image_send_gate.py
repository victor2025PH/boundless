# -*- coding: utf-8 -*-
"""Q-39 发送真相四闸——出图前的**意图闸 / 跟随预算 / 配文防重 / 同 mid 去重**（2026-09-15）。

事故背景（全部 skuio 1.0.85.0）：
- **#327 9PYWPG**：WhatsApp 客户连发自拍，我方全自动每次跟发一张相册图。真闸＝入站图的
  ``peer_text`` 是系统识图行「[图片内容] 一名男子的自拍…」——``detect_selfie_request``
  命中「自拍」→ 通用池放开 → ``pick_media`` 出图 → LLM 旧照口径配文「Found this old one of
  me…」。识图描述不是客户的话，从来不该当索图意图。
- **#323 XG3UZU**：「I try to take my time an enjoy the view」——``requested_scene_kind`` 的
  「the view」泛匹配 outdoor → ``_ask`` 置真 → 拟稿期 ``persona_reply`` 自探一次 + 投递期
  ``run_autosend_image`` 再跑一次 → ``candidates=0`` 记两次 miss → 红条「今天 2 次 · AI 已改口」。

本模块只在既有链**前面加闸**，不改 Q-6 匹配打分 / ``pick_media`` / ``note_album_miss`` /
``album_miss_addendum`` / ``detect_offer_media`` 的行为：

- :func:`compute_image_intent`：``intent = detect_selfie_request ∨ 严格 requested_scene_kind
  （须与索图动词共现）∨ 运营触发词命中 ∨ offer-accept ∨ 承诺兑现 / LLM 指令``；入站是图
  （识图行 / 占位 / media_type）时只看客户**自己敲的配文**（``strip_media_desc``），
  识图描述一律不算——图入站且无索图 → 承诺兑现 / LLM 指令也不放行（交撤回改写）。
- :func:`follow_budget_check` / :func:`note_follow_sent`：同会话「图片跟随」冷却（默认 30min，
  只对非显式索图触发：承诺兑现 / offer-accept / LLM 指令）+ 每日上限（默认 6，对全部
  autosend 出图）。配置 ``inbox.image_autosend.{follow_cooldown_min, follow_daily_max}``。
- :func:`dedup_caption` / :func:`note_caption_sent`：与最近 3 张出图配文归一后相似 ≥0.8 →
  换备选（注册配文 / 固定池）或空配文；日志 ``[album_send] caption_dedup``。
- :func:`album_match_seen` / :func:`mark_album_match`：同一入站 mid 的相册匹配只跑一次。
- :func:`format_album_send_log`：每次出图一行
  ``[album_send] trigger= intent= candidates= picked= caption_src=``。

全部纯函数 + 进程内 bounded 账本；任何异常都视为「无信号」，绝不阻塞主链。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

TRIGGER_ASK = "ask"
TRIGGER_KEYWORD = "keyword"
TRIGGER_OFFER_ACCEPT = "offer_accept"
TRIGGER_COMMITMENT = "commitment"
TRIGGER_DIRECTIVE = "directive"
TRIGGER_NONE = "none"

#: 显式索图（客户亲口要）——跟随冷却**不**对它们生效，只吃每日上限
EXPLICIT_TRIGGERS = frozenset({TRIGGER_ASK, TRIGGER_KEYWORD})

REASON_NO_INTENT = "no_intent"
REASON_INBOUND_IMAGE = "inbound_image_no_intent"
REASON_MENTION_ONLY = "photo_mention_no_ask"
REASON_DUP_MID = "dup_mid"
REASON_FOLLOW_COOLDOWN = "follow_cooldown"
REASON_FOLLOW_DAILY_MAX = "follow_daily_max"
REASON_PROMISE_STREAK = "promise_streak_pause"

DEFAULT_FOLLOW_COOLDOWN_MIN = 30.0
DEFAULT_FOLLOW_DAILY_MAX = 6
DEFAULT_CAPTION_SIM = 0.8
CAPTION_HISTORY_N = 3

_IMAGE_MEDIA_TYPES = frozenset({
    "image", "photo", "picture", "sticker", "sticker_image", "gif", "animation", "video",
})
_IMAGE_DESC_MARKERS = ("[图片内容]", "[贴纸内容]", "[视频内容]")
_MEDIA_PLACEHOLDERS = frozenset({
    "[图片]", "[贴纸]", "[视频]", "[动图]", "[gif]", "[GIF]", "[媒体]", "[表情]", "[动态表情]",
})
_VOICE_MARK = "[语音转录]"

# 索图动词 / 结构（与 companion_selfie 词表同族，只用于「场景 kind 泛匹配须与索图共现」）：
# 「enjoy the view」没有一个索图动词 → 不算；「send me a landscape pic」「can I see the view」算。
_ASK_VERB_RE = re.compile(
    r"(?:\b(?:send|show|share|shoot|drop|gimme|give|see)\b"
    r"|发|發|传|傳|寄|来张|來張|来个|來個|来一|來一|给我|給我|看看|睇下|睇睇|想看|想睇"
    r"|要张|要張|要个|要個|要一|拍张|拍張|拍个|拍個|拍一)",
    re.I,
)


# P0-2（2026-09-21，#339 / #332-A）：``detect_selfie_request`` 是宽口径「话题里有照片」
# 判定（「love your picture」「that's a cute selfie」「你的照片真好看」「I just took a selfie」
# 全为真）——它服务生成端多条链，不能收窄；出图闸这里再加一层**索要句形**：索图动词
# （has_ask_verb）∨ 求图句式（look like / any pics / 长什么样 / 照片呢 / 问号收尾）。
# 客户只是**谈**照片 → 不匹配相册、不记 miss、不刷红条、更不跟发。
_ASK_SHAPE_RE = re.compile(
    r"look\s+like|\bhow\s+do\s+(?:you|u)\s+look\b|"
    r"\b(?:got|have|has)\s+(?:any|more|new|other)\b|"
    r"\b(?:another|one\s+more|more|again|pls|plz|please|want|wanna|need)\b|"
    r"再来|再來|再发|再發|再拍|再给|再給|一张|一張|几张|幾張|多来|多來|想要|要看|"
    r"\bany\s+(?:more\s+|new\s+|other\s+)?(?:pics?|photos?|pictures?|selfies?)\b|"
    r"长什么样|長什麼樣|长啥样|長啥樣|样子|樣子|照片呢|图呢|圖呢|相呢|"
    r"没收到|沒收到|没看到|沒看到|有相未|有照片未|"
    r"送|見せ|くれ|보내|보여|manda|env[ií]a|envia|ส่ง|gửi|"
    r"[?？]\s*$",
    re.I,
)


def explicit_photo_ask(words: str) -> bool:
    """客户**在索要**我方照片：宽口径 ``detect_selfie_request`` ∧ 索要句形。纯函数、绝不抛。"""
    t = str(words or "").strip()
    if not t:
        return False
    try:
        from src.ai.companion_selfie import detect_selfie_request
        if not detect_selfie_request(t):
            return False
    except Exception:
        return False
    return has_ask_verb(t) or bool(_ASK_SHAPE_RE.search(t))


@dataclass
class IntentGate:
    """出图意图判定结果（``intent`` 为假时调用方不匹配、不写 miss、不刷红条）。"""

    intent: bool
    trigger: str = TRIGGER_NONE
    reason: str = ""
    words: str = ""
    inbound_image: bool = False
    scene_kind: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "intent": bool(self.intent), "trigger": self.trigger, "reason": self.reason,
            "inbound_image": bool(self.inbound_image), "scene_kind": self.scene_kind,
        }


# ── 入站形态 ──────────────────────────────────────────────────────────────


def inbound_is_image(peer_text: str, media_type: str = "") -> bool:
    """入站是不是图 / 贴纸 / 视频（客户发媒体，我方不该「跟发」）。

    三判据任一：``media_type`` 属媒体类；文本含识图标记 ``[图片内容]`` 等；文本整串是媒体占位。
    """
    mt = str(media_type or "").strip().lower()
    if mt in _IMAGE_MEDIA_TYPES:
        return True
    t = str(peer_text or "").strip()
    if not t:
        return False
    if any(mk in t for mk in _IMAGE_DESC_MARKERS):
        return True
    return t in _MEDIA_PLACEHOLDERS


def customer_words(peer_text: str) -> str:
    """客户**自己敲的字**：剥识图描述（``strip_media_desc``）与行首语音转写标记；纯占位 → ""。"""
    t = str(peer_text or "")
    try:
        from src.inbox.media_enrich import strip_media_desc
        t = strip_media_desc(t)
    except Exception:
        ts = t.lstrip()
        if ts.startswith(_VOICE_MARK):
            ts = ts[len(_VOICE_MARK):]
        idx = -1
        for mk in _IMAGE_DESC_MARKERS:
            i = ts.find(mk)
            if i >= 0 and (idx < 0 or i < idx):
                idx = i
        t = ts[:idx] if idx >= 0 else ts
    t = str(t or "").strip()
    if t in _MEDIA_PLACEHOLDERS:
        return ""
    return t


def has_ask_verb(text: str) -> bool:
    """索图动词 / 求图句式在场（本模块词表 ∪ Q-6 ``persona_media._MEDIA_REQ_RE`` 同一口径）。"""
    t = str(text or "")
    if _ASK_VERB_RE.search(t):
        return True
    try:
        from src.companion.persona_media import _MEDIA_REQ_RE
        return bool(_MEDIA_REQ_RE.search(t))
    except Exception:
        return False


def strict_requested_scene_kind(text: str) -> str:
    """``requested_scene_kind`` 的严格版：场景词须与索图动词 / 索图句式**共现**才算点名要图。

    「I try to take my time an enjoy the view」→ ""；「send me a landscape pic」→ outdoor。
    """
    t = str(text or "").strip()
    if not t or len(t) > 200:
        return ""
    try:
        from src.companion.persona_media import requested_scene_kind
        kind = str(requested_scene_kind(t) or "")
    except Exception:
        return ""
    if not kind:
        return ""
    if has_ask_verb(t) or explicit_photo_ask(t):
        return kind
    return ""


def _normalize_terms(text: str) -> str:
    try:
        from src.companion.persona_media import normalize_text
        return str(normalize_text(text) or "")
    except Exception:
        return str(text or "").strip().lower()


def album_trigger_terms(persona_id: str, *, suggest_ok: bool = True) -> List[str]:
    """该人设注册相册的运营触发词（+ 采纳档下的 AI 建议词）——客户说出它们＝显式索图。"""
    pid = str(persona_id or "").strip()
    if not pid:
        return []
    try:
        from src.companion.persona_media_store import get_persona_media_store
        st = get_persona_media_store()
        if st is None:
            return []
        rows = st.list(pid, enabled_only=True) or []
    except Exception:
        return []
    out: List[str] = []
    for r in rows:
        for x in (r.get("triggers") or []):
            s = str(x or "").strip()
            if s:
                out.append(s)
        if suggest_ok:
            am = r.get("auto_meta") if isinstance(r.get("auto_meta"), dict) else {}
            for x in ((am or {}).get("triggers_suggest") or []):
                s = str(x or "").strip()
                if s:
                    out.append(s)
    return out


def keyword_hit(words: str, trigger_terms: Optional[Iterable[str]]) -> bool:
    if not words or not trigger_terms:
        return False
    nt = _normalize_terms(words)
    if not nt:
        return False
    try:
        from src.companion.persona_media import trigger_term_hit
    except Exception:
        trigger_term_hit = None
    for term in trigger_terms:
        s = _normalize_terms(term)
        if not s:
            continue
        if trigger_term_hit is not None:
            if trigger_term_hit(s, nt):
                return True
        elif s in nt:
            return True
    return False


def compute_image_intent(
    peer_text: str,
    history: Optional[Sequence[Dict[str, Any]]] = None,
    *,
    inbound_media_type: str = "",
    assume_intent: str = "",
    directive_override: Optional[Dict[str, Any]] = None,
    trigger_terms: Optional[Iterable[str]] = None,
    offer_bridge: bool = True,
) -> IntentGate:
    """出图意图闸（纯函数，只读词表）。

    - 入站是图：只看客户自己敲的配文；识图描述里的「自拍 / 海边」一个字都不算。
      配文里也没索图词 → **任何路径**（承诺兑现 / offer-accept「好的/ok」 / LLM 指令）
      都不放行——承诺交撤回改写。短肯定配自拍是在回自己的图，不是要我方相册（#332）。
    - 入站是文本：``detect_selfie_request`` ∨ 严格场景 kind ∨ 触发词 ∨ offer-accept 之一 → 显式索图；
      否则 ``assume_intent``（承诺兑现）/ ``directive_override``（LLM 指令）照旧放行。
    """
    img = inbound_is_image(peer_text, inbound_media_type)
    words = customer_words(peer_text)
    ask = False
    kind = ""
    kw = False
    offer = False
    mention_only = False
    if words:
        ask = explicit_photo_ask(words)
        if not ask:
            try:
                from src.ai.companion_selfie import detect_selfie_request
                mention_only = bool(detect_selfie_request(words))
            except Exception:
                mention_only = False
        kind = strict_requested_scene_kind(words)
        kw = keyword_hit(words, trigger_terms)
        if offer_bridge and not (ask or kind or kw):
            try:
                from src.ai.outbound_promise_guard import offer_accepted
                offer = offer_accepted(words, list(history or [])) == "image"
            except Exception:
                offer = False
            if not offer:
                # 带主体的 offer-接受（AI「我煮了燕窝粥要不要看」→ 客户「好呀」）：客户点名
                # 想看具体东西，同属显式意图（下游 wanted_subject 链决定有货发 / 无货诚实）
                try:
                    from src.ai.outbound_promise_guard import wanted_media_subject
                    offer = bool(wanted_media_subject(
                        words, list(history or []), generic_request=False))
                except Exception:
                    pass
    asked_on_caption = ask or bool(kind) or kw
    # 入站是图时 offer-accept 不算索图：「好的」配自拍常是回应自己刚发的图，
    # 不是接受上一轮「要不要看我的照片」（#332 / 9PYWPG 复验）。
    if img and not asked_on_caption:
        trig = (TRIGGER_DIRECTIVE if directive_override
                else (TRIGGER_COMMITMENT if assume_intent
                      else (TRIGGER_OFFER_ACCEPT if offer else TRIGGER_NONE)))
        return IntentGate(False, trig, REASON_INBOUND_IMAGE, words, True, kind)
    if directive_override:
        return IntentGate(True, TRIGGER_DIRECTIVE, "", words, img, kind)
    if assume_intent:
        return IntentGate(True, TRIGGER_COMMITMENT, "", words, img, kind)
    if ask or kind:
        return IntentGate(True, TRIGGER_ASK, "", words, img, kind)
    if kw:
        return IntentGate(True, TRIGGER_KEYWORD, "", words, img, kind)
    if offer:
        return IntentGate(True, TRIGGER_OFFER_ACCEPT, "", words, img, kind)
    if mention_only:
        return IntentGate(False, TRIGGER_NONE, REASON_MENTION_ONLY, words, img, kind)
    return IntentGate(False, TRIGGER_NONE, REASON_NO_INTENT, words, img, kind)


# ── 同 mid 只跑一次 ────────────────────────────────────────────────────────

_SEEN: "OrderedDict[str, float]" = OrderedDict()
_SEEN_CAP = 2000
_SEEN_TTL_SEC = 6 * 3600.0
_SEEN_LOCK = threading.Lock()


def _seen_key(conv_key: str, mid: str) -> str:
    ck = str(conv_key or "").strip()
    m = str(mid or "").strip()
    return f"{ck}:{m}" if ck and m else ""


def album_match_seen(conv_key: str, mid: str, *, now: Optional[float] = None) -> bool:
    k = _seen_key(conv_key, mid)
    if not k:
        return False
    t = float(now if now is not None else time.time())
    with _SEEN_LOCK:
        ts = _SEEN.get(k)
        if ts is None:
            return False
        if t - float(ts) > _SEEN_TTL_SEC:
            _SEEN.pop(k, None)
            return False
        return True


def mark_album_match(conv_key: str, mid: str, *, now: Optional[float] = None) -> None:
    k = _seen_key(conv_key, mid)
    if not k:
        return
    t = float(now if now is not None else time.time())
    with _SEEN_LOCK:
        _SEEN[k] = t
        _SEEN.move_to_end(k)
        while len(_SEEN) > _SEEN_CAP:
            _SEEN.popitem(last=False)


# ── 跟随冷却 + 每日上限 ────────────────────────────────────────────────────

_FOLLOW: "OrderedDict[str, List[float]]" = OrderedDict()
_FOLLOW_CAP = 4000
_FOLLOW_LOCK = threading.Lock()


def follow_cfg(config: Any) -> Dict[str, float]:
    """``inbox.image_autosend.{follow_cooldown_min, follow_daily_max}``（缺省 30 / 6；0 = 关该闸）。"""
    try:
        ia = (((config or {}).get("inbox") or {}).get("image_autosend") or {})
    except Exception:
        ia = {}
    if not isinstance(ia, dict):
        ia = {}
    try:
        cd = float(ia.get("follow_cooldown_min", DEFAULT_FOLLOW_COOLDOWN_MIN))
    except (TypeError, ValueError):
        cd = DEFAULT_FOLLOW_COOLDOWN_MIN
    try:
        dm = int(ia.get("follow_daily_max", DEFAULT_FOLLOW_DAILY_MAX))
    except (TypeError, ValueError):
        dm = DEFAULT_FOLLOW_DAILY_MAX
    return {"follow_cooldown_min": max(0.0, cd), "follow_daily_max": max(0, dm)}


def _day_start(now: float) -> float:
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def follow_stats(conv_key: str, *, now: Optional[float] = None) -> Dict[str, Any]:
    """``{today: 今日已出图数, last_ts: 最近一次出图时刻}``。"""
    t = float(now if now is not None else time.time())
    day0 = _day_start(t)
    with _FOLLOW_LOCK:
        _load_follow_ledger_unlocked()
        arr = list(_FOLLOW.get(str(conv_key or ""), []) or [])
    today = [x for x in arr if x >= day0]
    return {"today": len(today), "last_ts": (max(arr) if arr else 0.0)}


def follow_budget_check(
    conv_key: str, config: Any, trigger: str, *, now: Optional[float] = None,
) -> str:
    """跟随预算：返回 "" = 放行；``follow_cooldown`` / ``follow_daily_max`` = 本轮只发文字。

    冷却只拦**非显式索图**（承诺兑现 / offer-accept / LLM 指令）——客户亲口「再来一张」不该被
    冷却吃掉；每日上限对所有 autosend 出图生效。
    """
    if not str(conv_key or "").strip():
        return ""
    cfg = follow_cfg(config)
    t = float(now if now is not None else time.time())
    st = follow_stats(conv_key, now=t)
    dmax = int(cfg["follow_daily_max"])
    if dmax > 0 and st["today"] >= dmax:
        return REASON_FOLLOW_DAILY_MAX
    cd = float(cfg["follow_cooldown_min"])
    if (cd > 0 and str(trigger or "") not in EXPLICIT_TRIGGERS
            and st["last_ts"] > 0 and (t - st["last_ts"]) < cd * 60.0):
        return REASON_FOLLOW_COOLDOWN
    return ""


def note_follow_sent(conv_key: str, *, now: Optional[float] = None) -> None:
    ck = str(conv_key or "").strip()
    if not ck:
        return
    t = float(now if now is not None else time.time())
    with _FOLLOW_LOCK:
        _load_follow_ledger_unlocked()
        arr = list(_FOLLOW.get(ck, []) or [])
        arr = [x for x in arr if t - x < 48 * 3600.0]
        arr.append(t)
        _FOLLOW[ck] = arr
        _FOLLOW.move_to_end(ck)
        while len(_FOLLOW) > _FOLLOW_CAP:
            _FOLLOW.popitem(last=False)
        _save_follow_ledger_unlocked()


_FOLLOW_LOADED = False


def _follow_ledger_path() -> Optional[Path]:
    try:
        base = str(os.environ.get("AITR_DATA_DIR") or "").strip()
        if not base:
            return None
        return Path(base) / "logs" / "image_send_follow_ledger.json"
    except Exception:
        return None


def _load_follow_ledger_unlocked() -> None:
    global _FOLLOW_LOADED
    if _FOLLOW_LOADED:
        return
    _FOLLOW_LOADED = True
    p = _follow_ledger_path()
    if p is None or not p.is_file():
        return
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        now = time.time()
        for ck, arr in (raw.get("follow") or {}).items():
            ts = [float(x) for x in (arr or []) if now - float(x) < 48 * 3600.0]
            if ts:
                _FOLLOW[str(ck)] = ts
        for ck, arr in (raw.get("captions") or {}).items():
            caps = [str(x) for x in (arr or []) if str(x).strip()][-CAPTION_HISTORY_N:]
            if caps:
                with _CAPTIONS_LOCK:
                    _CAPTIONS[str(ck)] = caps
    except Exception:
        logger.debug("[image_send_gate] follow ledger 读取失败", exc_info=True)


def _save_follow_ledger_unlocked() -> None:
    p = _follow_ledger_path()
    if p is None:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        follow_out: Dict[str, List[float]] = {}
        for ck, arr in _FOLLOW.items():
            ts = [float(x) for x in arr if now - float(x) < 48 * 3600.0]
            if ts:
                follow_out[str(ck)] = ts[-48:]
        cap_out: Dict[str, List[str]] = {}
        with _CAPTIONS_LOCK:
            items = list(_CAPTIONS.items())
        for ck, arr in items:
            caps = [str(x) for x in arr if str(x).strip()][-CAPTION_HISTORY_N:]
            if caps:
                cap_out[str(ck)] = caps
        p.write_text(json.dumps({"follow": follow_out, "captions": cap_out},
                                ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.debug("[image_send_gate] follow ledger 写入失败", exc_info=True)


# ── 承诺发图未兑现的跟轮账（R87 P1-3，X9B22T 15:09–15:50「Sure, give me a second, I'll take one now」×4）──
# 每次 promise 链把「承诺发图 / 假称已发」撤回改写就记一笔；同会话 30 分钟内 ≥2 次 → 拟稿侧注入
# 「本轮绝不承诺发图」硬提示（skill_manager._media_coherence_hint 同一消费口），承诺不再一轮一轮复发。

_PROMISE_RETRACTS: "OrderedDict[str, List[float]]" = OrderedDict()
_PROMISE_CAP = 4000
_PROMISE_LOCK = threading.Lock()
_PROMISE_LOADED = False
PROMISE_STREAK_WINDOW_SEC = 30 * 60.0
PROMISE_STREAK_HINT_N = 2


def _promise_ledger_path() -> Optional[Path]:
    try:
        base = str(os.environ.get("AITR_DATA_DIR") or "").strip()
        if not base:
            return None
        return Path(base) / "logs" / "promise_streak_ledger.json"
    except Exception:
        return None


def _load_promise_ledger_unlocked(now: float) -> None:
    """持锁调用。缺文件 / 坏 JSON → 空账本。"""
    global _PROMISE_LOADED
    if _PROMISE_LOADED:
        return
    _PROMISE_LOADED = True
    p = _promise_ledger_path()
    if p is None or not p.is_file():
        return
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        items = raw.items() if isinstance(raw, dict) else []
        for ck, arr in items:
            ts = [float(x) for x in (arr or []) if now - float(x) < PROMISE_STREAK_WINDOW_SEC]
            if ts:
                _PROMISE_RETRACTS[str(ck)] = ts
    except Exception:
        logger.debug("[image_send_gate] promise ledger 读取失败", exc_info=True)


def _save_promise_ledger_unlocked(now: float) -> None:
    p = _promise_ledger_path()
    if p is None:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        out: Dict[str, List[float]] = {}
        for ck, arr in _PROMISE_RETRACTS.items():
            ts = [float(x) for x in arr if now - float(x) < PROMISE_STREAK_WINDOW_SEC]
            if ts:
                out[str(ck)] = ts[-20:]
        p.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.debug("[image_send_gate] promise ledger 写入失败", exc_info=True)


def note_promise_retracted(conv_key: str, *, now: Optional[float] = None) -> int:
    """承诺 / 假称已发被撤回改写（或转人审）记一笔；返回窗内累计次数。"""
    ck = str(conv_key or "").strip()
    if not ck:
        return 0
    t = float(now if now is not None else time.time())
    with _PROMISE_LOCK:
        _load_promise_ledger_unlocked(t)
        arr = [x for x in (_PROMISE_RETRACTS.get(ck) or []) if t - x < PROMISE_STREAK_WINDOW_SEC]
        arr.append(t)
        _PROMISE_RETRACTS[ck] = arr
        _PROMISE_RETRACTS.move_to_end(ck)
        while len(_PROMISE_RETRACTS) > _PROMISE_CAP:
            _PROMISE_RETRACTS.popitem(last=False)
        n = len(arr)
        _save_promise_ledger_unlocked(t)
        return n


def promise_streak(conv_key: str, *, now: Optional[float] = None) -> int:
    """窗内撤回次数（无记录 = 0）。"""
    t = float(now if now is not None else time.time())
    with _PROMISE_LOCK:
        _load_promise_ledger_unlocked(t)
        arr = _PROMISE_RETRACTS.get(str(conv_key or "").strip()) or []
    return len([x for x in arr if t - x < PROMISE_STREAK_WINDOW_SEC])


def promise_streak_blocks_follow(conv_key: str, trigger: str, *, now: Optional[float] = None) -> bool:
    """#332：同会话连续假发图/空头承诺 ≥2 → 暂停**非显式索图**的自动出图。

    客户亲口再要一张（ask/keyword）仍放行——暂停的是跟发/兑现/指令，不是拒绝真人请求。
    """
    if str(trigger or "") in EXPLICIT_TRIGGERS:
        return False
    return promise_streak(conv_key, now=now) >= PROMISE_STREAK_HINT_N


def promise_streak_hint(conv_key: str, *, now: Optional[float] = None) -> str:
    """≥ :data:`PROMISE_STREAK_HINT_N` 次 → 给拟稿 LLM 的硬提示；否则空串。"""
    n = promise_streak(conv_key, now=now)
    if n < PROMISE_STREAK_HINT_N:
        return ""
    return (
        f"这个会话里你已经 {n} 次「说要发照片 / 说拍好了」但都没能发出去，客户在等。"
        "本轮**绝不**再承诺发图，不要写「等我去拍」「马上发你」「刚拍好」「照片来了」这类话，"
        "也不要解释为什么发不了；就当这一轮发不了照片，先回应对方刚说的内容，把话聊下去。"
    )


def reset_promise_streak_for_tests() -> None:
    global _PROMISE_LOADED
    with _PROMISE_LOCK:
        _PROMISE_RETRACTS.clear()
        _PROMISE_LOADED = True  # 测试不从磁盘灌回；persist 用例会再翻回 False


# ── 配文近重复 ─────────────────────────────────────────────────────────────

_CAPTIONS: "OrderedDict[str, List[str]]" = OrderedDict()
_CAPTIONS_CAP = 4000
_CAPTIONS_LOCK = threading.Lock()


def caption_similarity(a: str, b: str) -> float:
    try:
        from src.utils.proactive_variety import similarity
        return float(similarity(a, b))
    except Exception:
        from difflib import SequenceMatcher
        na = re.sub(r"[\W_]+", "", str(a or "").lower())
        nb = re.sub(r"[\W_]+", "", str(b or "").lower())
        if not na or not nb:
            return 0.0
        return SequenceMatcher(None, na, nb).ratio()


def recent_captions(conv_key: str) -> List[str]:
    with _FOLLOW_LOCK:
        _load_follow_ledger_unlocked()
    with _CAPTIONS_LOCK:
        return list(_CAPTIONS.get(str(conv_key or ""), []) or [])


def note_caption_sent(conv_key: str, caption: str) -> None:
    ck = str(conv_key or "").strip()
    cap = str(caption or "").strip()
    if not ck or not cap:
        return
    with _FOLLOW_LOCK:
        _load_follow_ledger_unlocked()
        with _CAPTIONS_LOCK:
            arr = list(_CAPTIONS.get(ck, []) or [])
            arr.append(cap)
            _CAPTIONS[ck] = arr[-CAPTION_HISTORY_N:]
            _CAPTIONS.move_to_end(ck)
            while len(_CAPTIONS) > _CAPTIONS_CAP:
                _CAPTIONS.popitem(last=False)
        _save_follow_ledger_unlocked()


def dedup_caption(
    conv_key: str, caption: str, caption_src: str,
    alternatives: Sequence[Tuple[str, str]] = (),
    *, threshold: float = DEFAULT_CAPTION_SIM,
) -> Tuple[str, str, float]:
    """配文与最近 3 张出图配文归一后相似 ≥ threshold → 换第一个**不雷同**的备选
    ``(caption, src)``；备选全雷同 / 为空 → 空配文（src="dedup_empty"）。

    返回 ``(配文, 来源, 命中的最高相似度)``；未命中原样返回（sim < threshold）。
    """
    cap = str(caption or "").strip()
    if not cap:
        return cap, caption_src, 0.0
    prev = recent_captions(conv_key)
    if not prev:
        return cap, caption_src, 0.0
    sim = max((caption_similarity(cap, p) for p in prev), default=0.0)
    if sim < threshold:
        return cap, caption_src, sim
    for alt, src in alternatives or ():
        a = str(alt or "").strip()
        if not a or a == cap:
            continue
        if max((caption_similarity(a, p) for p in prev), default=0.0) < threshold:
            return a, str(src or "alt"), sim
    return "", "dedup_empty", sim


# ── 出图日志 ───────────────────────────────────────────────────────────────


def format_album_send_log(
    conv_key: str, *, trigger: str, intent: Any, candidates: Any, picked: str,
    caption_src: str, mid: str = "", source: str = "",
) -> str:
    """``[album_send] conv= trigger= intent= candidates= picked= caption_src=``（每次出图一行）。"""
    if isinstance(intent, IntentGate):
        intent_s = f"{int(bool(intent.intent))}:{intent.trigger}"
    elif isinstance(intent, bool):
        intent_s = str(int(intent))
    else:
        intent_s = str(intent or "-")
    try:
        cand = int(candidates)
    except (TypeError, ValueError):
        cand = -1
    return (
        f"[album_send] conv={conv_key or '-'} trigger={trigger or '-'} intent={intent_s} "
        f"candidates={cand if cand >= 0 else '-'} picked={picked or '-'} "
        f"caption_src={caption_src or '-'}"
        + (f" src={source}" if source else "")
        + (f" mid={mid}" if mid else "")
    )


def _reset_for_tests() -> None:
    global _FOLLOW_LOADED
    with _SEEN_LOCK:
        _SEEN.clear()
    with _FOLLOW_LOCK:
        _FOLLOW.clear()
        _FOLLOW_LOADED = True
    with _CAPTIONS_LOCK:
        _CAPTIONS.clear()


__all__ = [
    "IntentGate", "compute_image_intent", "inbound_is_image", "customer_words",
    "strict_requested_scene_kind", "has_ask_verb", "album_trigger_terms", "keyword_hit",
    "album_match_seen", "mark_album_match",
    "follow_cfg", "follow_stats", "follow_budget_check", "note_follow_sent",
    "note_promise_retracted", "promise_streak", "promise_streak_hint",
    "promise_streak_blocks_follow",
    "reset_promise_streak_for_tests", "PROMISE_STREAK_WINDOW_SEC", "PROMISE_STREAK_HINT_N",
    "dedup_caption", "note_caption_sent", "recent_captions", "caption_similarity",
    "format_album_send_log",
    "TRIGGER_ASK", "TRIGGER_KEYWORD", "TRIGGER_OFFER_ACCEPT", "TRIGGER_COMMITMENT",
    "TRIGGER_DIRECTIVE", "TRIGGER_NONE", "EXPLICIT_TRIGGERS",
    "REASON_NO_INTENT", "REASON_INBOUND_IMAGE", "REASON_DUP_MID", "REASON_MENTION_ONLY",
    "REASON_FOLLOW_COOLDOWN", "REASON_FOLLOW_DAILY_MAX", "REASON_PROMISE_STREAK",
    "explicit_photo_ask",
]
