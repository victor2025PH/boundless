"""入站多媒体能力（识图 / 识别语音 / 识别视频 / 自动发自拍）就绪自检 + 一键预设（纯函数）。

与 ``capability_status`` 的分工：那张表管「全自动文本回复 / 主动触达 / 语音发送」等**出站**
能力；本模块管「**入站媒体理解** + 出图」这组媒体能力——出厂默认全关，运营最常见的坑是
「以为开了识图，其实没开 / 开了但没配后端」。所以每项都给两件事：

  ① ``enabled``：开关是否打开（读 config 点路径）。
  ② ``backend_ready``：后端是否真配好（识图有 Ollama/智谱？ASR provider 可用？出图后端非 disabled？）。

只有两者皆真才算 ``active``；开了但没后端 → ``needs_backend``（自检要报的正是这种"通了电没接线"）。

纯函数、零副作用、可单测；路由层只做 config 取值 + 写 overlay（复用 set_overlay_flag + 审计）。
"""

from __future__ import annotations

import os as _os
from typing import Any, Dict, List, Optional


def _dig(config: Any, path: str, default: Any = None) -> Any:
    cur = config
    for part in str(path or "").split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


# ── 后端就绪探测（与各消费方同口径，best-effort，绝不抛）─────────────────────

def vision_backend_ready(config: Any) -> bool:
    """识图/识视频后端：Ollama(base_url) 或 智谱(api_key) 任一可用即真。"""
    vcfg = (config or {}).get("vision") or {}
    try:
        from src.vision_client import has_any_vision_backend
        return bool(has_any_vision_backend(vcfg, vcfg))
    except Exception:
        return False


def asr_backend_ready(config: Any) -> bool:
    """语音转写后端：本地 whisper 系无需 key（视为可用）；openai 需 api_key。"""
    vr = (config or {}).get("voice_recognition") or {}
    provider = str(vr.get("provider") or "faster_whisper").strip().lower()
    if provider == "openai":
        return bool(str((vr.get("openai") or {}).get("api_key") or "").strip())
    # faster_whisper / whisper_local / whisper / 其它本地 → 本地推理，视为可用
    return True


def selfie_backend_ready(config: Any) -> bool:
    """出图后端：album 需 album_dir；openai 需 api_key；command 需 command_args/template。"""
    sc = ((config or {}).get("companion") or {}).get("selfie") or {}
    prov = sc.get("provider") or {}
    if not isinstance(prov, dict):
        return False
    backend = str(prov.get("backend") or "disabled").strip().lower()
    if backend in ("", "disabled"):
        return False
    if backend == "album":
        return bool(str(prov.get("album_dir") or "").strip())
    if backend == "openai":
        return bool(str(prov.get("api_key") or "").strip())
    if backend == "command":
        return bool(prov.get("command_args") or str(prov.get("command_template") or "").strip())
    return False


_BACKEND_PROBES = {
    "vision": vision_backend_ready,
    "asr": asr_backend_ready,
    "selfie": selfie_backend_ready,
}

# 入站媒体能力表（key / 展示标签 / 开关路径 / 后端探针 / 风险 / 说明）
MEDIA_CAPS: List[Dict[str, str]] = [
    {"key": "vision_inbound", "label": "入站识图（图片理解）",
     "flag": "vision.enabled", "backend": "vision", "risk": "low",
     "desc": "对方发来的图片/贴纸 → VLM 识别成文字描述喂 AI（Ollama→智谱链）。仅 API 成本。"},
    {"key": "asr_inbound", "label": "入站识别语音（转写）",
     "flag": "voice_recognition.enabled", "backend": "asr", "risk": "low",
     "desc": "对方语音条 → Whisper 转写成文字喂 AI，AI 才能接住内容而非搪塞「听不了语音」。"},
    {"key": "video_inbound", "label": "入站识别视频",
     "flag": "vision.enabled", "backend": "vision", "risk": "low",
     "desc": "对方视频 → 抽关键帧看画面 + 抽音轨转写。复用识图后端 + 语音转写，随识图一起开。"},
    {"key": "selfie_outbound", "label": "自动发自拍 / 按需生图",
     "flag": "companion.selfie.enabled", "backend": "selfie", "risk": "high",
     "desc": "客户要照片时 AI 出人设自拍/物体图发出（相册/生成后端）。出站媒体，需配出图后端。"},
]

CAP_BY_KEY = {c["key"]: c for c in MEDIA_CAPS}


def evaluate_media_cap(cap: Dict[str, str], config: Any) -> Dict[str, Any]:
    """单个媒体能力就绪度（纯函数）。stage: off | needs_backend | active。"""
    enabled = bool(_dig(config, cap["flag"], False))
    probe = _BACKEND_PROBES.get(cap["backend"])
    backend_ready = bool(probe(config)) if probe else False
    if not enabled:
        stage = "off"
        hint = "未开启（默认关）——一键预设可开齐入站识别"
    elif not backend_ready:
        stage = "needs_backend"
        hint = _backend_hint(cap["backend"])
    else:
        stage = "active"
        hint = "已开启且后端就绪"
    return {
        "key": cap["key"], "label": cap["label"], "flag": cap["flag"],
        "risk": cap["risk"], "desc": cap["desc"],
        "enabled": enabled, "backend_ready": backend_ready,
        "stage": stage, "hint": hint,
    }


def _is_managed_edition() -> bool:
    """托管版（成品，客户不配后端）——env 标记由桌面壳/实例注入。"""
    return str(_os.environ.get("AITR_MANAGED_EDITION") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _backend_hint(backend: str, managed: Optional[bool] = None) -> str:
    """就绪度提示。**托管版**不向用户暴露 yaml 键/密钥概念——那是集团/服务端的事；
    自建版保留可操作的具体键名。managed 缺省自动探测 env。"""
    if managed is None:
        managed = _is_managed_edition()
    if managed:
        # 托管版：不是用户的活，也没有「填密钥」这回事——说人话，不吓人、不甩黑话
        if backend == "vision":
            return "识图能力由服务端统一提供，无需在本机配置；如未生效请联系客服开启"
        if backend == "asr":
            return "语音识别由服务端统一提供，无需在本机配置；如未生效请联系客服开启"
        if backend == "selfie":
            return "发图能力由服务端统一提供，无需在本机配置；如未生效请联系客服开启"
        return "该能力由服务端统一提供，无需在本机配置"
    if backend == "vision":
        return "开关已开但未配识图后端：填 vision.base_url(Ollama) 或 vision.api_key(智谱)"
    if backend == "asr":
        return "开关已开但语音后端不可用：faster_whisper 本地即可，或给 voice_recognition.openai.api_key"
    if backend == "selfie":
        return "开关已开但未配出图后端：companion.selfie.provider.backend 需为 album/openai/command"
    return "开关已开但后端未就绪"


def media_runtime_signals() -> Dict[str, Any]:
    """近期识别运行质量的紧凑信号（best-effort，读进程内 stats 单例）。

    与 ``collect_media_status``（config 就绪度，纯函数）互补：这里是**真流量质量**——
    让运营在开启处就看到「近期识图成功率/缓存命中/语音成功率」，而不必翻运维总览。
    任何导入/读取失败一律返回空段，绝不抛、绝不阻断自检端点。返回 ``{}`` 或含
    ``vision`` / ``asr`` / ``video`` 子段（仅在有真流量时给出）。
    """
    out: Dict[str, Any] = {}
    try:
        from src.ai.provider_stats import get_provider_stats
        vz = get_provider_stats("vision", "vision").dump()
        attempts = int(vz.get("total_attempts") or 0)
        cache_hits = int(vz.get("cache_hits") or 0)
        if attempts + cache_hits > 0:
            ok = sum(int(r.get("ok") or 0) for r in (vz.get("rows") or []))
            out["vision"] = {
                "attempts": attempts + cache_hits,
                "success_rate": round(ok / attempts, 4) if attempts else 0.0,
                "cache_hit_rate": float(vz.get("cache_hit_rate") or 0.0),
                "fallbacks": int(vz.get("fallbacks") or 0),
            }
    except Exception:
        pass
    try:
        from src.ai.asr_stats import get_asr_stats
        a = get_asr_stats().dump()
        att = int(a.get("attempts") or 0)
        if att > 0:
            ok = int(a.get("primary_ok") or 0) + int(a.get("fallback_ok") or 0)
            out["asr"] = {
                "attempts": att,
                "success_rate": round(ok / att, 4) if att else 0.0,
                "fallback_rate": float(a.get("fallback_rate") or 0.0),
            }
    except Exception:
        pass
    try:
        from src.ai.inbound_video_stats import get_inbound_video_stats
        v = get_inbound_video_stats().dump()
        if v.get("active"):
            out["video"] = {
                "attempts": int(v.get("attempts") or 0),
                "success_rate": float(v.get("success_rate") or 0.0),
            }
    except Exception:
        pass
    try:
        from src.integrations.shared.media_dedup import dedup_metrics_snapshot
        d = dedup_metrics_snapshot()
        total = int(d.get("total_perturbed") or 0)
        fb = int(d.get("fallback") or 0)
        if total + fb > 0:  # 有真流量才带（未开/无出站不显示）
            out["dedup"] = {
                "image_perturbed": int(d.get("image_perturbed") or 0),
                "video_perturbed": int(d.get("video_perturbed") or 0),
                "fallback": fb,
                "fallback_reasons": dict(d.get("fallback_reasons") or {}),
            }
    except Exception:
        pass
    # T3（2026-07-22）：相册投放账本——库存/消耗/覆盖，运营据此判断"素材快用完了
    # 该补货"。sends 分布来自防复读账本 persona_media_sends（跨重启持久）。
    try:
        from src.companion.persona_media_store import get_persona_media_store
        st = get_persona_media_store()
        if st is not None:
            s = st.send_ledger_stats()
            if int(s.get("total_sends") or 0) > 0 or int(
                    s.get("media_total") or 0) > 0:
                out["album"] = s
    except Exception:
        pass
    return out


def collect_media_status(config: Any) -> Dict[str, Any]:
    """聚合入站媒体能力就绪度 + 摘要。"""
    caps = [evaluate_media_cap(c, config) for c in MEDIA_CAPS]
    by_stage: Dict[str, int] = {"off": 0, "needs_backend": 0, "active": 0}
    for c in caps:
        by_stage[c["stage"]] = by_stage.get(c["stage"], 0) + 1
    out: Dict[str, Any] = {
        "capabilities": caps,
        "summary": {
            "total": len(caps),
            "by_stage": by_stage,
            "understand_active": all(
                c["stage"] == "active" for c in caps if c["key"] != "selfie_outbound"),
        },
    }
    # 近期真流量识别质量（有流量才有；空则不加，前端零流量不渲染健康行）
    signals = media_runtime_signals()
    if signals:
        out["runtime"] = signals
    return out


# ── 一键预设（只翻开关，不碰出站发送侧；发送侧用 capability_presets）───────────
MEDIA_PRESETS: Dict[str, Dict[str, Any]] = {
    "understand_all": {
        "label": "开齐入站识别（识图 + 识别语音 + 识别视频）",
        "flags": {"vision.enabled": True, "voice_recognition.enabled": True},
    },
    "understand_and_selfie": {
        "label": "入站识别 + 自动发自拍（需已配出图后端）",
        "flags": {"vision.enabled": True, "voice_recognition.enabled": True,
                  "companion.selfie.enabled": True},
    },
    "media_off": {
        "label": "关闭全部媒体能力（回到纯文本）",
        "flags": {"vision.enabled": False, "voice_recognition.enabled": False,
                  "companion.selfie.enabled": False},
    },
}


def build_media_preset(name: str) -> Optional[Dict[str, Any]]:
    """预设名 → {label, flags:{path:value}}；未知返回 None。"""
    spec = MEDIA_PRESETS.get(str(name or "").strip())
    return dict(spec) if spec else None


def preset_backend_warnings(name: str, config: Any) -> List[str]:
    """开启类预设：逐项检查将开启的能力后端是否已就绪，返回人类可读告警（可空）。"""
    spec = MEDIA_PRESETS.get(str(name or "").strip()) or {}
    warns: List[str] = []
    flags = spec.get("flags") or {}
    # flag 路径 → 需要的后端探针
    _flag_backend = {
        "vision.enabled": "vision",
        "voice_recognition.enabled": "asr",
        "companion.selfie.enabled": "selfie",
    }
    for path, val in flags.items():
        if not val:
            continue
        backend = _flag_backend.get(path)
        probe = _BACKEND_PROBES.get(backend or "")
        if probe and not probe(config):
            warns.append(_backend_hint(backend))
    return warns


__all__ = [
    "MEDIA_CAPS", "MEDIA_PRESETS", "CAP_BY_KEY",
    "vision_backend_ready", "asr_backend_ready", "selfie_backend_ready",
    "evaluate_media_cap", "collect_media_status", "media_runtime_signals",
    "build_media_preset", "preset_backend_warnings",
]
