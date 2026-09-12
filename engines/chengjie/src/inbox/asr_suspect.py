"""语音转写「可疑」三态——好 / 可疑 / 无（ASR P1，2026-09-12）。

ASR P0 让每次转写都带回元数据（``VoiceTranscriber.last_meta``：检出语种 / 语种概率 /
avg_logprob / no_speech_prob / 先验复核结果…）。本模块把它折成一个**可读原因码**并送到
两个消费口，让「听错还自信作答」（日志实锤「你那边几点怎么会说大造成呢」→ AI 只能瞎接）
变成「像真人一样先确认一句」：

- **prompt**：``user_context["_voice_asr_suspect"] = <reason>`` → ai_client 走「语音可能
  听错 · 先确认」块（与既有 ``_voice_lang_suspect`` 同族；两者同在时只出语种那块）。
- **档位**（可选，默认关）：``voice_recognition.suspect_hold_review: true`` 时可疑转写的
  这一稿封顶 review（L1，原因码 ``asr_suspect``），人过一眼再发。

三态判据（``transcript_suspect``，纯函数，缺什么证据就不判什么）：
- ``lang_conflict``   检出语种与会话先验冲突且按先验重转仍对不上（P0 ``lang_suspect``）
- ``low_confidence``  avg_logprob < -1.0 或（auto 检测时）语种概率 < 0.6（P0 ``low_confidence``）
- ``no_speech``       服务端 no_speech_prob ≥ 0.5（在无人声闸 0.85 以下的灰区）
- ``short_unsure``    音频 < 2s 且语种概率 < 0.6（短片段最容易幻觉）
- ``""``              好——正常喂产线

登记表与 ``l1_reason`` 同款：进程级 ``{conversation_id: (reason, 转写归一串, ts)}``，
TTL 15 分钟、上限 2000；**按会话 + 本条文本匹配**才算命中（客户紧接着发一条文字，
不能还顶着上一条语音的可疑标记）。任何异常 → 不标记（fail-open，宁可少提示不误拦）。
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, Optional, Tuple

_lock = threading.Lock()
_REG: Dict[str, Tuple[str, str, float]] = {}
_MAX = 2000
_TTL = 15 * 60.0

CONTEXT_KEY = "_voice_asr_suspect"
L1_REASON = "asr_suspect"

REASON_LANG_CONFLICT = "lang_conflict"
REASON_LOW_CONFIDENCE = "low_confidence"
REASON_NO_SPEECH = "no_speech"
REASON_SHORT_UNSURE = "short_unsure"
REASONS = (REASON_LANG_CONFLICT, REASON_LOW_CONFIDENCE, REASON_NO_SPEECH, REASON_SHORT_UNSURE)

#: no_speech 灰区下限（≥ 0.85 已在 OpenAITranscriber 直接丢弃）
NO_SPEECH_SUSPECT = 0.5
SHORT_SEC = 2.0
SHORT_MIN_LANG_PROB = 0.6

_NORM_RE = re.compile(r"[\s，,。.!！?？~～、:：;；\-—_\[\]【】]+")
_PREFIX_RE = re.compile(r"^\[(?:语音转录|语音消息[^\]]*|语音)\]\s*")


def _f(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def transcript_suspect(meta: Optional[Dict[str, Any]], text: str) -> str:
    """转写元数据 → 原因码（``""``＝不可疑）。纯函数，缺证据不判，绝不抛。"""
    try:
        if not str(text or "").strip():
            return ""
        m = meta if isinstance(meta, dict) else {}
        if m.get("lang_suspect"):
            return REASON_LANG_CONFLICT
        if m.get("low_confidence"):
            return REASON_LOW_CONFIDENCE
        lp = _f(m.get("avg_logprob"))
        if lp is not None and lp < -1.0:
            return REASON_LOW_CONFIDENCE
        nsp = _f(m.get("no_speech_prob"))
        if nsp is not None and nsp >= NO_SPEECH_SUSPECT:
            return REASON_NO_SPEECH
        dur = _f(m.get("duration"))
        prob = _f(m.get("language_probability"))
        if (dur is not None and 0 < dur < SHORT_SEC
                and prob is not None and prob < SHORT_MIN_LANG_PROB):
            return REASON_SHORT_UNSURE
        return ""
    except Exception:
        return ""


def _norm(text: str) -> str:
    t = _PREFIX_RE.sub("", str(text or "").strip())
    return _NORM_RE.sub("", t).lower()


def note(conversation_id: str, reason: str, text: str = "") -> None:
    """登记本会话最新一条语音的可疑原因（``text``＝转写，供消费方核对是同一条）。"""
    cid = str(conversation_id or "")
    r = str(reason or "")
    if not cid:
        return
    now = time.time()
    with _lock:
        if not r:
            _REG.pop(cid, None)
            return
        _REG[cid] = (r, _norm(text), now)
        if len(_REG) > _MAX:
            cutoff = now - _TTL
            for k in [x for x, (_, _, ts) in _REG.items() if ts < cutoff]:
                _REG.pop(k, None)
            if len(_REG) > _MAX:
                for k in sorted(_REG, key=lambda x: _REG[x][2])[: len(_REG) - _MAX]:
                    _REG.pop(k, None)


def peek(conversation_id: str, text: Optional[str] = None) -> str:
    """读原因码；``text`` 给了则要求与登记的转写匹配（归一后相等或包含），过期 → ``""``。"""
    try:
        rec = _REG.get(str(conversation_id or ""))
        if not rec:
            return ""
        reason, norm_t, ts = rec
        if time.time() - ts > _TTL:
            return ""
        if text is not None:
            cur = _norm(text)
            if not cur or not norm_t:
                return ""
            if not (cur == norm_t or norm_t in cur or cur in norm_t):
                return ""
        return reason
    except Exception:
        return ""


def _vr(cfg_root: Any) -> Dict[str, Any]:
    try:
        vr = (cfg_root or {}).get("voice_recognition") if isinstance(cfg_root, dict) else None
        return vr if isinstance(vr, dict) else {}
    except Exception:
        return {}


def hint_enabled(cfg_root: Any) -> bool:
    """可疑转写是否进 prompt 澄清块（``voice_recognition.suspect_hint``，默认开）。"""
    return bool(_vr(cfg_root).get("suspect_hint", True))


def hold_review_enabled(cfg_root: Any) -> bool:
    """可疑转写的稿是否封顶 review（``voice_recognition.suspect_hold_review``，默认关）。"""
    return bool(_vr(cfg_root).get("suspect_hold_review", False))


def note_from_meta(conversation_id: str, meta: Optional[Dict[str, Any]], text: str,
                   cfg_root: Any = None) -> str:
    """转写完成后一站式登记：算原因码（开关关 / 不可疑 → 清掉旧登记）→ 返回原因码。"""
    try:
        reason = transcript_suspect(meta, text) if hint_enabled(cfg_root) else ""
        note(conversation_id, reason, text)
        return reason
    except Exception:
        return ""


def apply_to_user_context(user_context: Dict[str, Any], *, conversation_id: str,
                          text: str, explicit: Optional[str] = None) -> str:
    """把原因码写进 ``user_context[CONTEXT_KEY]``（A 线 ``explicit`` 优先；B 线查登记表并
    按本条文本核对）；不可疑则**清掉**该键（防陈年标记跨轮驻留）。返回生效的原因码。"""
    reason = ""
    try:
        if explicit:
            reason = str(explicit)
        else:
            reason = peek(conversation_id, text)
    except Exception:
        reason = ""
    if reason:
        user_context[CONTEXT_KEY] = reason
    else:
        user_context.pop(CONTEXT_KEY, None)
    return reason


def _reset_for_tests() -> None:
    with _lock:
        _REG.clear()


__all__ = [
    "CONTEXT_KEY", "L1_REASON", "REASONS", "apply_to_user_context", "hint_enabled",
    "hold_review_enabled", "note", "note_from_meta", "peek", "transcript_suspect",
]
