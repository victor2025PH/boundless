"""全自动「数字人口播视频」出站（System Z autosend 的视频出站，与 voice/image 对称）。

对方在对话里要「录段视频 / 说句话给我看 / 看看你本人」时，AI 不再只发文字/语音，而是
调 AvatarHub 的口播数字人服务生成一段「人设本人念这段话」的 MP4 视频发出。一处生效、
全平台共用（经 ``orch.send_media(media_type="video")``）。

分工（复用既有件，避免重复造轮子）：
- 触发判定：``detect_video_request``（客户明确要视频）+ ``decide_video`` 的 trigger 档
  （never / on_request（默认）/ always / smart）+ 人设灰度白名单 + 每会话频率上限。
- 合成：AvatarHub ``POST {base_url}/avatar/speak``（generate_lipsync=true）→ 文字→情感 TTS
  →口型同步 MP4（base64 回传）；见 ``主控机调用API文档.md``。**跨机调用 .176(5090)**——
  视频合成吃显存，绝不在本机(3060)跑。
- 落盘：``protocol_bridge.save_outbound_media``（与语音/图片出站同一出站媒体目录 → /static URL）。

**默认关**（``inbox.l2_autosend.video.enabled=false``）→ 全自动仍纯文本/语音/图片，零行为变更。
视频最贵（显存 + 秒级~十几秒合成）：默认仅「客户明确要视频」才发，且每会话每日限量。
任何环节失败/不满足都返回「不发视频」让调用方回落语音/文本，绝不卡住全自动主流程。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_VALID_TRIGGERS = ("never", "on_request", "always", "smart")
_DEFAULT_MAX_CHARS = 120          # 口播不宜太长（念太久 + 合成慢）；超长回落语音/文本
_DEFAULT_BASE_URL = "http://192.168.0.176:9000"   # AvatarHub 主控（源机 5090）
_DEFAULT_LANGUAGE = "zh-cn"        # /avatar/speak 语言（契约字段）
_DEFAULT_TIMEOUT = 240.0          # 同步返回、TTS+口型较慢，官方建议 180-240s
_DEFAULT_DAILY_CAP = 3            # 每会话每日视频上限（视频贵，克制）

# 客户「要视频」意图（多语、保守）。与 companion_selfie.detect_selfie_request（要静态照片）
# 区分：这里抓的是明确要「视频 / 动起来 / 说句话给我看 / 录一段」。
_VIDEO_REQUEST_MARKERS = (
    "视频", "視訊", "視頻", "录个", "錄個", "录段", "錄段", "录一段", "錄一段",
    "拍个视频", "拍個視頻", "发个视频", "發個視頻", "来段视频", "來段視頻",
    "说句话给我看", "說句話給我看", "动起来", "動起來", "讲话给我看", "講話給我看",
    "video", "record a video", "send a video", "say something on video",
    "video of you", "on video", "video message",
)

# ── 可观测性（进程内累计；与 voice_autosend / image_autosend 同风格）──────────────
_METRICS: Dict[str, Any] = {
    "sent": 0, "fallback": 0, "last_reason": "",
    "last_ts": 0.0, "last_duration_ms": 0,
    "video_chosen": 0, "text_chosen": 0,
    "decision_reasons": {}, "last_decision": "",
}
_METRICS_LOCK = threading.Lock()

# 每会话每日发送计数（防刷屏）：conv_key -> (yyyymmdd, count)。bounded LRU。
_DAILY: "OrderedDict[str, Tuple[str, int]]" = OrderedDict()
_DAILY_CAP_ENTRIES = 5000
_DAILY_LOCK = threading.Lock()


def record_video_sent(duration_ms: int = 0) -> None:
    with _METRICS_LOCK:
        _METRICS["sent"] = int(_METRICS["sent"]) + 1
        _METRICS["last_ts"] = time.time()
        if duration_ms and duration_ms > 0:
            _METRICS["last_duration_ms"] = int(duration_ms)


def record_video_fallback(reason: str) -> None:
    with _METRICS_LOCK:
        _METRICS["fallback"] = int(_METRICS["fallback"]) + 1
        _METRICS["last_reason"] = str(reason or "")
        _METRICS["last_ts"] = time.time()


def record_video_decision(send_video: bool, reason: str) -> None:
    with _METRICS_LOCK:
        key = "video_chosen" if send_video else "text_chosen"
        _METRICS[key] = int(_METRICS.get(key, 0)) + 1
        r = str(reason or "")
        reasons = _METRICS.setdefault("decision_reasons", {})
        reasons[r] = int(reasons.get(r, 0)) + 1
        _METRICS["last_decision"] = ("video:" if send_video else "text:") + r


def metrics_snapshot() -> Dict[str, Any]:
    with _METRICS_LOCK:
        snap = dict(_METRICS)
        snap["decision_reasons"] = dict(_METRICS.get("decision_reasons") or {})
        return snap


def resolve_video_autosend_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    """取 ``inbox.l2_autosend.video`` 块（缺失返回空 dict → enabled 视为 false）。"""
    try:
        return dict(
            (((config or {}).get("inbox") or {}).get("l2_autosend") or {}).get("video")
            or {}
        )
    except Exception:
        return {}


def detect_video_request(text: str) -> bool:
    """客户是否在明确要「视频」（多语、保守）。空/无标记 → False。"""
    t = str(text or "").strip().lower()
    if not t:
        return False
    return any(m.lower() in t for m in _VIDEO_REQUEST_MARKERS)


def _daily_key(conv_key: str) -> str:
    return str(conv_key or "").strip()


def daily_count(conv_key: str, *, now: Optional[float] = None) -> int:
    """该会话今日已发视频条数（跨天自动归零）。"""
    ck = _daily_key(conv_key)
    if not ck:
        return 0
    day = time.strftime("%Y%m%d", time.localtime(now if now is not None else time.time()))
    with _DAILY_LOCK:
        rec = _DAILY.get(ck)
        if not rec or rec[0] != day:
            return 0
        return int(rec[1])


def bump_daily(conv_key: str, *, now: Optional[float] = None) -> None:
    """记一次该会话今日视频发送（跨天重置；bounded LRU）。"""
    ck = _daily_key(conv_key)
    if not ck:
        return
    day = time.strftime("%Y%m%d", time.localtime(now if now is not None else time.time()))
    with _DAILY_LOCK:
        rec = _DAILY.get(ck)
        _DAILY[ck] = (day, (int(rec[1]) + 1) if (rec and rec[0] == day) else 1)
        _DAILY.move_to_end(ck)
        while len(_DAILY) > _DAILY_CAP_ENTRIES:
            _DAILY.popitem(last=False)


def reset_daily() -> None:
    """测试钩子。"""
    with _DAILY_LOCK:
        _DAILY.clear()


def decide_video(
    video_block: Dict[str, Any],
    text: str,
    *,
    peer_text: str = "",
    peer_sent_video: bool = False,
    conv_key: str = "",
    crisis_block: bool = False,
    now: Optional[float] = None,
) -> Tuple[bool, str]:
    """决策本条回复是否发**数字人视频**，返回 ``(send_video, reason)``。

    护栏（任一不满足 → 文字/语音，附 reason）：
    - ``enabled=false`` → disabled；空文本 → empty；超 ``max_chars`` → too_long；危机 → crisis_safe。
    - trigger：``never`` / ``always`` / ``on_request``（默认，客户明确要视频或对方发了视频才回）
      / ``smart``（暂等同 on_request，留扩展位）。
    - 每会话每日超 ``daily_cap`` → daily_cap（视频贵，克制）。
    """
    vb = video_block or {}
    if not bool(vb.get("enabled")):
        return False, "disabled"
    t = (text or "").strip()
    if not t:
        return False, "empty"
    try:
        max_chars = int(vb.get("max_chars", _DEFAULT_MAX_CHARS) or _DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        max_chars = _DEFAULT_MAX_CHARS
    if len(t) > max_chars:
        return False, "too_long"
    if crisis_block:
        return False, "crisis_safe"
    trigger = str(vb.get("trigger", "on_request") or "on_request").lower()
    if trigger not in _VALID_TRIGGERS:
        trigger = "on_request"
    if trigger == "never":
        return False, "trigger_never"
    # 频率上限（每会话每日）
    try:
        cap = int(vb.get("daily_cap", _DEFAULT_DAILY_CAP) or _DEFAULT_DAILY_CAP)
    except (TypeError, ValueError):
        cap = _DEFAULT_DAILY_CAP
    if cap > 0 and conv_key and daily_count(conv_key, now=now) >= cap:
        return False, "daily_cap"
    if trigger == "always":
        return True, "trigger_always"
    # on_request / smart：客户明确要视频，或对方刚发了视频（对等）
    if peer_sent_video:
        return True, "peer_video"
    if detect_video_request(peer_text):
        return True, "requested"
    return False, "no_request"


def persona_allowed_for_video(
    video_block: Dict[str, Any], persona_id: Optional[str]
) -> bool:
    """人设级灰度白名单（与 l2_autosend.voice.persona_allowlist 同口径）。

    ``persona_allowlist`` 缺省/空 → 不限制（所有人设放行）。非空 → 仅名单内人设发视频。
    视频需人设备有数字人形象（AvatarHub 角色 / profile）→ 灰度期用白名单收敛到已备形象的人设。
    """
    vb = video_block or {}
    allow = vb.get("persona_allowlist")
    if not allow:
        return True
    try:
        names = {str(x).strip() for x in allow if str(x).strip()}
    except TypeError:
        return True
    if not names:
        return True
    return bool(persona_id) and str(persona_id).strip() in names


def resolve_avatar_profile(video_block: Dict[str, Any], persona_id: str) -> str:
    """人设 → AvatarHub 角色/形象 profile 名。

    ``video_block.persona_profiles = {persona_id: profile}``；缺省回落 persona_id 本身
    （AvatarHub 侧常以 persona_id 命名角色）。空 persona_id → ""。
    """
    pid = str(persona_id or "").strip()
    if not pid:
        return ""
    try:
        m = video_block.get("persona_profiles")
        if isinstance(m, dict) and m.get(pid):
            return str(m[pid]).strip()
    except Exception:
        pass
    return pid


# ── AvatarHub /avatar/speak 合成（纯 stdlib urllib；不引重依赖）─────────────────

def build_speak_payload(
    text: str, *, profile: str = "", emotion: str = "neutral",
    language: str = _DEFAULT_LANGUAGE,
    field_names: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """构造 AvatarHub POST /avatar/speak 请求体（generate_lipsync=true 出口播 MP4）。

    契约字段：``{text, profile, language, generate_lipsync, emotion}``（见 .176 实机核对）；
    可经 ``field_names`` 覆盖以适配命名差异（如 profile→character）。纯函数、可单测。
    ``profile`` 留空 = 用 AvatarHub 当前激活角色。
    """
    fn = field_names or {}
    payload: Dict[str, Any] = {
        fn.get("text", "text"): str(text or ""),
        fn.get("generate_lipsync", "generate_lipsync"): True,
        fn.get("emotion", "emotion"): str(emotion or "neutral"),
        fn.get("language", "language"): str(language or _DEFAULT_LANGUAGE),
    }
    if profile:
        payload[fn.get("profile", "profile")] = str(profile)
    return payload


def parse_speak_video_b64(resp: Any, *, field: str = "lipsync_video_b64") -> str:
    """从 /avatar/speak 响应取口播视频 base64；缺失/非法 → ""。"""
    if not isinstance(resp, dict):
        return ""
    v = resp.get(field) or resp.get("video_b64") or resp.get("video_base64")
    return str(v or "")


def _post_json(url: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw) if raw else {}


async def stage_video_file(
    config: Dict[str, Any],
    platform: str,
    account_id: str,
    persona_id: str,
    text: str,
    *,
    emotion: str = "neutral",
    video_block: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[str, str]]:
    """合成数字人口播视频并落到出站媒体目录，返回 ``(本地路径, /static URL)``；失败返回 None。

    调 AvatarHub ``{base_url}/avatar/speak``（generate_lipsync=true），跨机（.176）合成，
    绝不在本机跑 GPU。任何异常/超时/空返回 → None（调用方回落语音/文本）。
    """
    import asyncio

    vb = video_block if video_block is not None else resolve_video_autosend_cfg(config)
    base_url = str(vb.get("base_url") or _DEFAULT_BASE_URL).rstrip("/")
    speak_path = str(vb.get("speak_path") or "/avatar/speak")
    try:
        timeout = float(vb.get("timeout_sec", _DEFAULT_TIMEOUT) or _DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT
    field_names = vb.get("field_names") if isinstance(vb.get("field_names"), dict) else None
    resp_field = str(vb.get("response_video_field") or "lipsync_video_b64")
    language = str(vb.get("language") or _DEFAULT_LANGUAGE)
    # 情感：显式入参优先，否则取配置默认（neutral）。
    eff_emotion = emotion if emotion and emotion != "neutral" else str(
        vb.get("emotion") or emotion or "neutral")
    profile = resolve_avatar_profile(vb, persona_id)
    payload = build_speak_payload(
        text, profile=profile, emotion=eff_emotion, language=language,
        field_names=field_names)

    def _call() -> Optional[Dict[str, Any]]:
        """返回响应 dict；None=请求失败（不可达/超时/异常，已记日志）。"""
        try:
            return _post_json(f"{base_url}{speak_path}", payload, timeout)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as ex:
            logger.info("[video_autosend] AvatarHub 不可达/超时 %s: %s", base_url, ex)
            return None
        except Exception:
            logger.debug("[video_autosend] /avatar/speak 调用异常", exc_info=True)
            return None

    resp = await asyncio.to_thread(_call)
    if resp is None:
        return None
    b64 = parse_speak_video_b64(resp, field=resp_field)
    if not b64:
        # 请求通了但视频字段空——最常见原因：.176 的口型引擎（MuseTalk 8090 /
        # ditto 8096 / echomimic 8095）未运行，/avatar/speak 只回了音频。回落语音/文本，
        # 并给出明确日志便于运维（区别于「服务不可达」）。
        logger.info(
            "[video_autosend] AvatarHub 返回无口型视频（%s 为空；.176 口型引擎未运行？）"
            " → 回落 profile=%s", resp_field, profile or "(激活角色)")
        return None
    try:
        data = base64.b64decode(b64)
    except Exception:
        logger.debug("[video_autosend] 视频 b64 解码失败", exc_info=True)
        return None
    if not data:
        return None
    try:
        from src.integrations.protocol_bridge import save_outbound_media
        local, url, _mt = save_outbound_media(
            platform, account_id, f"avatar_{int(time.time())}.mp4", data)
        return (local, url)
    except Exception:
        logger.debug("[video_autosend] 落出站媒体失败", exc_info=True)
        return None


__all__ = [
    "resolve_video_autosend_cfg", "detect_video_request", "decide_video",
    "persona_allowed_for_video", "resolve_avatar_profile",
    "build_speak_payload", "parse_speak_video_b64", "stage_video_file",
    "daily_count", "bump_daily", "reset_daily",
    "record_video_sent", "record_video_fallback", "record_video_decision",
    "metrics_snapshot",
]
