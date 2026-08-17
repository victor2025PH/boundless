"""部署能力预设档（deploy profile，WP-1 纯云起步档 2026-08）。

「一台没有 LAN GPU 拓扑的陌生机器怎么跑」收成三件事：

- **预设文件** ``config/profiles/<name>.yaml``（随包分发，纯 YAML 零代码路径）：
  把该拓扑下应显式关闭/清空的键钉成一个 patch，内含 ``deploy.profile`` 标记；
- **首启播种**（``ConfigManager._ensure_deploy_profile`` 消费本模块）：
  ``AITR_DEPLOY_PROFILE=<name>`` 且本次 config 为全新播种 → patch 深合并进
  config.local.yaml overlay（ruamel 保注释写入器），一次性、幂等、绝不动老机器；
- **就绪自检**（``capability_snapshot``，``/api/setup/deploy-profile`` 消费）：
  当前档位 + 各能力 开/关/降级 一屏——向导与支持排障不再靠翻 YAML 猜。

全模块纯函数：只读 config dict 与预设目录，零网络零副作用（写盘动作在
ConfigManager 侧走既有 ``save_overlay_patch``）。状态语义：

- ``on``＝已启用且配置层依赖齐备；
- ``degraded``＝已启用但配置层依赖缺失 → 运行时会走既有软降级路径；
- ``off``＝未启用（含「刻意不配」——cloud_light 档的常态）。

门禁 ``tests/test_deploy_profiles.py``：预设文件零内网 IP、与 feature_registry
的 lan/infra 分级对齐、合并后 LAN 探针决策函数全静默、播种语义（新装才应用）。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List

from src.utils.feature_registry import _has_embedding, dig

logger = logging.getLogger("ai_chat_assistant.deploy_profile")

_NAME_RE = re.compile(r"^[a-z0-9_]{1,40}$")


def profiles_dir() -> Path:
    """预设档目录（引擎根/打包 _internal 下的 ``config/profiles/``）。

    与 ``ConfigManager._bundled_example_path`` 同一套根定位（``Path(__file__)``
    上三级），冻结态（PyInstaller onedir）自动落 ``_internal/config/profiles/``。
    """
    return Path(__file__).resolve().parent.parent.parent / "config" / "profiles"


def list_profiles() -> List[str]:
    """可用预设档名列表（扫描目录，异常返回空——预设缺席不是故障）。"""
    try:
        return sorted(
            p.stem for p in profiles_dir().glob("*.yaml")
            if _NAME_RE.match(p.stem))
    except Exception:
        return []


def load_profile(name: str) -> Dict[str, Any]:
    """读取并解析预设档 patch；名字非法/文件缺失/解析失败一律返回 ``{}``。

    名字白名单 ``[a-z0-9_]`` 从源头杜绝路径穿越（env 值不可信）。
    """
    key = str(name or "").strip().lower()
    if not _NAME_RE.match(key):
        return {}
    path = profiles_dir() / f"{key}.yaml"
    try:
        if not path.is_file():
            return {}
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.warning("预设档解析失败（忽略，不应用）: %s", path, exc_info=True)
        return {}


def active_profile(cfg: Dict[str, Any]) -> str:
    """合并视图里的当前档位标记（未选档/自建部署返回空串）。"""
    return str(dig(cfg or {}, "deploy.profile") or "").strip()


def _state(enabled: bool, dep_ok: bool = True) -> str:
    if not enabled:
        return "off"
    return "on" if dep_ok else "degraded"


def capability_snapshot(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """各能力 开/关/降级 一屏快照（确定性，只读 config，零网络零密钥）。

    行集合按 WP-1 的排障需要取：主对话/本地容灾/语音三链/视觉/嵌入/向量记忆/
    翻译/LAN 运维探针/出图/RPA。detail 只放非敏感结构字段（数量/枚举/模型名），
    文案翻译属前端职责——本快照是 API 数据不是 UI 词条。
    """
    cfg = cfg or {}
    ai = cfg.get("ai") or {}
    key = str(ai.get("api_key") or "").strip()
    key_configured = bool(key) and not key.upper().startswith("YOUR_")
    primary = str(ai.get("primary") or "cloud").strip().lower()

    vr = (cfg.get("telegram") or {}).get("voice_reply") or {}
    asr = cfg.get("voice_recognition") or {}
    vision = cfg.get("vision") or {}
    vision_endpoints = [
        u for u in (vision.get("base_urls") or [])
        if str(u or "").strip()]
    if not vision_endpoints and str(vision.get("base_url") or "").strip():
        vision_endpoints = [str(vision.get("base_url")).strip()]
    vision_cloud_key = bool(
        str(vision.get("api_key") or "").strip()
        or str(vision.get("zhipu_api_key") or "").strip())
    vision_enabled = bool(vision.get("enabled"))

    embedding_ok = _has_embedding(cfg)
    memvec_enabled = bool(dig(cfg, "memory.vector.enabled"))

    engines = dig(cfg, "translation.engines") or {}
    order = [str(x) for x in (engines.get("order") or []) if str(x or "").strip()]
    mt = engines.get("ollama_mt") or {}
    lan_mt = bool(
        str(mt.get("base_url") or "").strip()
        or any(str(u or "").strip() for u in (mt.get("base_urls") or [])))

    rpa = {
        p: bool(dig(cfg, f"{p}_rpa.enabled"))
        for p in ("line", "messenger", "whatsapp")}

    return {
        "chat_llm": {
            "state": _state(True, key_configured),
            "mode": primary,
            "key_configured": key_configured,
            "model": str(ai.get("model") or ""),
        },
        "local_llm_fallback": {
            "state": _state(bool(dig(cfg, "ai.fallback.enabled")))},
        "voice_clone": {"state": _state(bool(dig(cfg, "avatar_voice.enabled")))},
        "tts_voice_reply": {
            "state": _state(bool(vr.get("enabled"))),
            "backend": str(vr.get("backend") or "edge_tts"),
        },
        "asr": {
            "state": _state(bool(asr.get("enabled"))),
            "provider": str(asr.get("provider") or ""),
        },
        "speech_emotion": {
            "state": _state(bool(dig(cfg, "speech_emotion.enabled")))},
        "realtime_voice": {
            "state": _state(bool(dig(cfg, "realtime_voice.enabled")))},
        "vision": {
            "state": _state(
                vision_enabled, bool(vision_endpoints) or vision_cloud_key),
            "endpoints": len(vision_endpoints),
            "cloud_key": vision_cloud_key,
        },
        "embedding": {
            "state": _state(embedding_ok),
            # off 时记忆召回退关键词、去重退 hash（既有软降级，非故障）
            "fallback": "keyword_recall" if not embedding_ok else "",
        },
        "memory_vector": {"state": _state(memvec_enabled, embedding_ok)},
        "translation": {
            "state": _state(bool(order)),
            "engines": order,
            "lan_mt": lan_mt,
        },
        "selfie": {"state": _state(bool(dig(cfg, "companion.selfie.enabled")))},
        "gpu_watermark": {
            "state": _state(bool(dig(cfg, "ops.gpu_watermark.enabled")))},
        "cloud_credentials_probe": {
            "state": _state(bool(dig(cfg, "ops.cloud_credentials.enabled")))},
        "rpa": {
            "state": _state(any(rpa.values())),
            **rpa,
        },
    }
