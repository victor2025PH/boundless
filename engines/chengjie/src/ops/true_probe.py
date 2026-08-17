"""true_probe — 四域「真活探针」（2026-08-17 无兜底纪律，实施33 v2 规则 8）。

health 200 不算活：8/16-8/17 事故里 index worker /health 恒 200 但 CPU 爬行 40-120s、
fish 健康 ping 绿但真合成挂死——全天语音断档零告警。本模块对四个域周期性
**真干活**（真合成 / 真翻译 / 真识图 / 真转写），连续 N 次失败＝主机弹窗 +
EventBus(host_alert) 外发 + ERROR 日志；恢复补绿窗。

结构：
  - ``build_probe_specs(cfg)``  纯函数：从运行配置推每域探针规格（未启用的域无规格）
  - ``run_probe(spec)``         同步 HTTP 执行一个规格（watchdog tick 在线程池里跑，可阻塞）
  - ``next_strike_state(...)``  纯函数：连败/恢复状态机 → 该发什么通知
  - watchdog 侧薄包装见 health_watchdog._check_true_probes
"""
from __future__ import annotations

import base64
import json
import logging
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ai_chat_assistant.true_probe")

# ASR 探针语音夹具（hub index_tts 合成「你好你好，今天天气不错」，2026-08-17 生成）
ASR_FIXTURE = Path(__file__).resolve().parents[2] / "assets" / "probe" / "asr_probe.wav"

# 8x8 纯红 PNG（识图探针输入；活性探测不校验内容正确性，只要求模型真跑了推理）
_RED_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAAFklEQVR4nGP8z8Dwn4EIwESM"
    "olGFlCsEAE1oAhLLuJE1AAAAAElFTkSuQmCC"
)

_AUDIO_MAGIC = (b"RIFF", b"OggS", b"ID3", b"fLaC")


def build_probe_specs(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从运行配置推四域探针规格（纯函数）。域未启用/缺配置 → 不出规格（静默跳过）。"""
    cfg = cfg if isinstance(cfg, dict) else {}
    specs: List[Dict[str, Any]] = []

    # ── tts：hub /api/tts_only 真合成（显式钉引擎，media-artifact 纪律：验 magic）──
    av = cfg.get("avatar_voice") or {}
    hf = av.get("hub_fish") if isinstance(av.get("hub_fish"), dict) else {}
    if av.get("enabled") and hf.get("enabled") and str(hf.get("base_url") or "").strip():
        pmap = hf.get("profile_map") if isinstance(hf.get("profile_map"), dict) else {}
        profile = ""
        for _v in pmap.values():
            profile = str(_v or "").strip()
            if profile:
                break
        if profile:
            body: Dict[str, Any] = {
                "profile": profile, "text": "好的呀", "best_of": 1,
                "format": "wav", "language": "zh-cn",
            }
            _eng = str(hf.get("tts_engine") or "").strip()
            if _eng:
                body["tts_engine"] = _eng
            specs.append({
                "domain": "tts", "kind": "hub_tts",
                "url": str(hf.get("base_url")).rstrip("/") + "/api/tts_only",
                "json": body, "timeout": 30.0,
            })

    # ── translate：MT 引擎真翻译（ollama /v1 兼容口，单端点契约）──────────────
    eng = ((cfg.get("translation") or {}).get("engines") or {})
    mt = eng.get("ollama_mt") if isinstance(eng.get("ollama_mt"), dict) else {}
    _mt_bases = mt.get("base_urls") or ([mt.get("base_url")] if mt.get("base_url") else [])
    if _mt_bases and str(mt.get("model") or "").strip():
        specs.append({
            "domain": "translate", "kind": "openai_chat",
            "url": str(_mt_bases[0]).rstrip("/") + "/v1/chat/completions",
            "json": {
                "model": str(mt.get("model")).strip(),
                "messages": [{
                    "role": "user",
                    "content": "把下面这句话翻译成英文，只输出译文：你好，今天天气不错",
                }],
                "max_tokens": 60, "temperature": 0,
            },
            "timeout": 30.0,
        })

    # ── vision：VLM 真识图（base_url 已含 /v1；喂 8x8 红图求一句描述）──────────
    vi = cfg.get("vision") or {}
    _vi_base = str(vi.get("base_url") or "").strip()
    if vi.get("enabled") and _vi_base and str(vi.get("model") or "").strip():
        specs.append({
            "domain": "vision", "kind": "openai_chat",
            "url": _vi_base.rstrip("/") + "/chat/completions",
            "json": {
                "model": str(vi.get("model")).strip(),
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "用一句话说出这张图的主色调。"},
                        {"type": "image_url", "image_url": {
                            "url": "data:image/png;base64," + _RED_PNG_B64}},
                    ],
                }],
                "max_tokens": 40, "temperature": 0,
            },
            "timeout": 45.0,
        })

    # ── asr：真转写（OpenAI 兼容 /audio/transcriptions，multipart 夹具 wav）────
    vr = cfg.get("voice_recognition") or {}
    _vr_base = str(vr.get("base_url") or "").strip()
    if vr.get("enabled") and _vr_base and ASR_FIXTURE.is_file():
        specs.append({
            "domain": "asr", "kind": "asr_transcribe",
            "url": _vr_base.rstrip("/") + "/audio/transcriptions",
            "model": str(vr.get("model") or "whisper-1").strip(),
            "timeout": 30.0,
        })

    return specs


def _http_json(url: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": "Bearer probe"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _chat_content(data: Dict[str, Any]) -> str:
    try:
        return str(((data.get("choices") or [{}])[0].get("message") or {})
                   .get("content") or "").strip()
    except Exception:
        return ""


def run_probe(spec: Dict[str, Any]) -> Tuple[bool, str]:
    """执行一个探针规格 → (ok, detail)。阻塞式，调用方须在线程池里跑。绝不抛。"""
    kind = str(spec.get("kind") or "")
    url = str(spec.get("url") or "")
    timeout = float(spec.get("timeout") or 30.0)
    t0 = time.time()
    try:
        if kind == "hub_tts":
            data = _http_json(url, spec.get("json") or {}, timeout)
            if not data.get("ok"):
                return False, f"hub ok=false detail={str(data.get('detail'))[:80]}"
            raw = base64.b64decode(str(data.get("audio_base64") or ""), validate=False)
            if len(raw) < 8000 or not raw.startswith(_AUDIO_MAGIC):
                return False, f"audio invalid ({len(raw)}B)"
            return True, f"{time.time() - t0:.1f}s {len(raw) // 1024}KB"
        if kind == "openai_chat":
            data = _http_json(url, spec.get("json") or {}, timeout)
            content = _chat_content(data)
            if not content:
                return False, "empty completion"
            return True, f"{time.time() - t0:.1f}s {content[:24]!r}"
        if kind == "asr_transcribe":
            wav = ASR_FIXTURE.read_bytes()
            boundary = f"----probe{uuid.uuid4().hex}"
            parts = (
                f"--{boundary}\r\nContent-Disposition: form-data; "
                f"name=\"model\"\r\n\r\n{spec.get('model')}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; "
                f"name=\"file\"; filename=\"probe.wav\"\r\n"
                f"Content-Type: audio/wav\r\n\r\n"
            ).encode("utf-8") + wav + f"\r\n--{boundary}--\r\n".encode("utf-8")
            req = urllib.request.Request(
                url, data=parts,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Authorization": "Bearer probe",
                },
                method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            text = str(data.get("text") or "").strip()
            if not text:
                return False, "empty transcription"
            return True, f"{time.time() - t0:.1f}s {text[:20]!r}"
        return False, f"unknown kind {kind!r}"
    except Exception as ex:
        return False, f"{type(ex).__name__}: {str(ex)[:100]}"


def next_strike_state(
    state: Dict[str, Dict[str, Any]],
    domain: str,
    ok: bool,
    *,
    fail_strikes: int = 2,
    now: Optional[float] = None,
) -> Tuple[Dict[str, Dict[str, Any]], str]:
    """连败状态机（纯函数）。返回 (新 state, action)。

    action ∈ ""（无事）| "alert"（达连败阈值，首报）| "recovered"（曾报过警后恢复）。
    重复失败不重复出 action（弹窗去抖由 notify_host 冷却兜第二层）。
    """
    ts = float(now if now is not None else time.time())
    st = dict(state or {})
    d = dict(st.get(domain) or {"fails": 0, "alerted": False, "last_ok": 0.0})
    action = ""
    if ok:
        if d.get("alerted"):
            action = "recovered"
        d.update({"fails": 0, "alerted": False, "last_ok": ts})
    else:
        d["fails"] = int(d.get("fails", 0)) + 1
        if d["fails"] >= max(1, int(fail_strikes)) and not d.get("alerted"):
            d["alerted"] = True
            action = "alert"
    d["last_run"] = ts
    st[domain] = d
    return st, action


__all__ = [
    "ASR_FIXTURE",
    "build_probe_specs",
    "next_strike_state",
    "run_probe",
]
