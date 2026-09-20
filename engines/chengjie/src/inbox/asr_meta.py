"""逐条语音消息的 ASR 元数据持久化（ASR P2，2026-09-12）——坐席台「这条转写靠不靠谱」的数据源。

P0 让每次转写带回 ``last_meta``（检出语种 / 概率 / avg_logprob / no_speech / 时长 / 是否命中缓存 /
是否按先验重转…），P1 把它折成「可疑」原因码送进 prompt；但这些都活在**进程内**，坐席台
媒体行看不到、重启即没。本模块把它按消息落到 ``app_settings`` KV（与 Q-3 risk_hold 同一
「不动 store.py 表结构」哲学）：

    键   ``asr_meta:<conversation_id>:<message_id>``
    值   JSON（``compact`` 产物：公开字段 + ts + machine_text）
    TTL  7 天（读时懒清理；徽标只对近期会话有意义）

消费口：
- ``attach(store, cid, msgs)``：``/api/unified-inbox/thread`` 给语音行挂 ``asr`` 字段
  ``{suspect, confidence, language, language_probability, avg_logprob, no_speech_prob,
    duration, provider, level, cache_hit, lang_retry, corrected, corrected_by, machine_text}``
  ——前端据此画徽标 / 「改正」入口；
- ``mark_corrected``：坐席改正后写回（清 suspect、记改正人与原机器转写）。

全部 best-effort：store 缺方法 / KV 异常 → 返回空/False，绝不影响转写与落库主链。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Iterable, List, Optional

KEY_PREFIX = "asr_meta:"
TTL_SEC = 7 * 86400

#: 进 KV / 进 thread 响应的字段白名单（绝不带音频路径以外的内部键）
_FIELDS = (
    "suspect", "language", "language_probability", "avg_logprob", "no_speech_prob",
    "duration", "provider", "level", "cache_hit", "lang_retry", "low_confidence",
    "lang_suspect", "corrected", "corrected_by", "machine_text", "ts",
)
_FLOATS = ("language_probability", "avg_logprob", "no_speech_prob", "duration")
_BOOLS = ("cache_hit", "lang_retry", "low_confidence", "lang_suspect", "corrected")


def key(conversation_id: str, message_id: str) -> str:
    return f"{KEY_PREFIX}{str(conversation_id or '').strip()}:{str(message_id or '').strip()}"


def compact(meta: Optional[Dict[str, Any]], *, suspect: str = "", machine_text: str = "",
            ts: Optional[float] = None) -> Dict[str, Any]:
    """转写元数据 → 可落 KV 的紧凑 dict（纯函数；缺什么字段就不带什么）。"""
    m = meta if isinstance(meta, dict) else {}
    out: Dict[str, Any] = {"ts": float(ts if ts is not None else time.time())}
    if suspect:
        out["suspect"] = str(suspect)
    lang = str(m.get("language") or "").strip().lower()
    if lang:
        out["language"] = lang[:16]
    prov = str(m.get("provider") or "").strip()
    if prov:
        out["provider"] = prov[:64]
    for k in _FLOATS:
        v = m.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = round(float(v), 4)
    lvl = m.get("level")
    if isinstance(lvl, int) and not isinstance(lvl, bool):
        out["level"] = int(lvl)
    for k in ("cache_hit", "lang_retry", "low_confidence", "lang_suspect"):
        if m.get(k):
            out[k] = True
    mt = str(machine_text or "").strip()
    if mt:
        out["machine_text"] = mt[:500]
    return out


def confidence_label(meta: Optional[Dict[str, Any]]) -> str:
    """``low`` / ``medium`` / ``high`` / ``""``（无证据）——给 UI 徽标用的三档。"""
    m = meta if isinstance(meta, dict) else {}
    if not m:
        return ""
    if m.get("suspect") or m.get("low_confidence") or m.get("lang_suspect"):
        return "low"
    lp = m.get("avg_logprob")
    pr = m.get("language_probability")
    nsp = m.get("no_speech_prob")
    if (isinstance(lp, (int, float)) and lp < -0.7) or \
            (isinstance(pr, (int, float)) and pr < 0.8) or \
            (isinstance(nsp, (int, float)) and nsp >= 0.3):
        return "medium"
    if isinstance(lp, (int, float)) or isinstance(pr, (int, float)):
        return "high"
    return ""


def save(store: Any, conversation_id: str, message_id: str, meta: Optional[Dict[str, Any]], *,
         suspect: str = "", machine_text: str = "") -> bool:
    """写一条（覆盖同键）。store 无 set_app_setting / 键不全 → False。"""
    cid = str(conversation_id or "").strip()
    mid = str(message_id or "").strip()
    if not cid or not mid or store is None or not hasattr(store, "set_app_setting"):
        return False
    try:
        rec = compact(meta, suspect=suspect, machine_text=machine_text)
        return bool(store.set_app_setting(key(cid, mid), json.dumps(rec, ensure_ascii=False),
                                          updated_by="asr"))
    except Exception:
        return False


def load_map(store: Any, conversation_id: str, *, now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
    """会话内全部条目 ``{message_id: meta}``；过期条目懒删。任何异常 → {}。"""
    cid = str(conversation_id or "").strip()
    if not cid or store is None or not hasattr(store, "list_app_settings"):
        return {}
    pfx = f"{KEY_PREFIX}{cid}:"
    out: Dict[str, Dict[str, Any]] = {}
    try:
        rows = store.list_app_settings(pfx) or []
    except Exception:
        return {}
    t_now = float(now if now is not None else time.time())
    for r in rows:
        try:
            k = str((r or {}).get("key") or "")
            if not k.startswith(pfx):
                continue
            mid = k[len(pfx):]
            rec = json.loads(str((r or {}).get("value") or "{}"))
            if not isinstance(rec, dict):
                continue
            ts = rec.get("ts")
            if isinstance(ts, (int, float)) and t_now - float(ts) > TTL_SEC:
                try:
                    store.set_app_setting(k, "")   # 懒清理
                except Exception:
                    pass
                continue
            out[mid] = rec
        except Exception:
            continue
    return out


def public_view(meta: Dict[str, Any]) -> Dict[str, Any]:
    """KV 记录 → thread 响应字段（白名单 + confidence 三档）。"""
    m = meta if isinstance(meta, dict) else {}
    out = {k: m[k] for k in _FIELDS if k in m}
    out["confidence"] = confidence_label(m)
    out.setdefault("suspect", "")
    out.setdefault("corrected", False)
    return out


def attach(store: Any, conversation_id: str, msgs: Iterable[Dict[str, Any]]) -> int:
    """给 thread 消息列表里的入站语音/音频行挂 ``asr`` 字段；返回挂上的条数。"""
    try:
        rows: List[Dict[str, Any]] = [m for m in (msgs or []) if isinstance(m, dict)]
        if not any(str(m.get("media_type") or "").lower() in ("voice", "audio") for m in rows):
            return 0
        table = load_map(store, conversation_id)
        if not table:
            return 0
        n = 0
        for m in rows:
            if str(m.get("media_type") or "").lower() not in ("voice", "audio"):
                continue
            # 键既可能是 store 的 message_id（AutoDraft 自转时用它），也可能是边车的
            # platform_msg_id（协议入站落库前只知道这个）——两个都认。
            rec = table.get(str(m.get("message_id") or "")) or \
                table.get(str(m.get("platform_msg_id") or ""))
            if rec:
                m["asr"] = public_view(rec)
                n += 1
        return n
    except Exception:
        return 0


def mark_corrected(store: Any, conversation_id: str, message_id: str, *, corrected_text: str,
                   agent: str = "", machine_text: str = "",
                   platform_msg_id: str = "") -> Optional[Dict[str, Any]]:
    """坐席改正后回写：清 suspect / 低置信标记，记 corrected_by；无旧记录也新建一条。

    旧记录可能落在 message_id 键或 platform_msg_id 键下（见 attach），先找到再覆写。"""
    cid = str(conversation_id or "").strip()
    mid = str(message_id or "").strip()
    if not cid or not mid or store is None or not hasattr(store, "set_app_setting"):
        return None
    try:
        rec: Dict[str, Any] = {}
        target = mid
        for cand in (mid, str(platform_msg_id or "").strip()):
            if not cand or not hasattr(store, "get_app_setting"):
                continue
            raw = store.get_app_setting(key(cid, cand), "")
            if raw:
                try:
                    loaded = json.loads(raw)
                    rec = loaded if isinstance(loaded, dict) else {}
                except Exception:
                    rec = {}
                target = cand
                break
        mid = target
        if machine_text and not rec.get("machine_text"):
            rec["machine_text"] = str(machine_text)[:500]
        rec.update({
            "corrected": True, "corrected_by": str(agent or "")[:80],
            "corrected_text": str(corrected_text or "")[:500],
            "suspect": "", "low_confidence": False, "lang_suspect": False,
            "corrected_ts": time.time(),
        })
        rec.setdefault("ts", time.time())
        store.set_app_setting(key(cid, mid), json.dumps(rec, ensure_ascii=False), updated_by="asr_correction")
        return rec
    except Exception:
        return None


__all__ = ["KEY_PREFIX", "TTL_SEC", "attach", "compact", "confidence_label", "key", "load_map",
           "mark_corrected", "public_view", "save"]
