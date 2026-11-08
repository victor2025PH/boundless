"""pair-chat 回复语言。国家和语言分开，注册表决定能不能用。

不读号码国码。不把未知标签交给 ``normalize_lang_code``，避免变体被折成 ``zh``。
新增一种语言只加注册表记录。能力标成 ``unsupported`` 时不准发送。
``unverified`` 只表示还没有真机或模型验收，离线参数测试仍可走通。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "wa-reply-language/1"
CONTRACT = SCHEMA_VERSION
BASE_COMMIT = "cbb9e8534da3089292c98c0d799136469546b6b5"
STRATEGIES = ("fixed", "account_default", "follow_peer")
CAPABILITY_KEYS = ("model_generation", "phone_screen_read", "unicode_input")
CAPABILITY_VALUES = ("supported", "unsupported", "unverified")

_REGISTRY_PATH = Path(__file__).with_name("reply_languages.json")
_CACHE: Optional[List[dict]] = None


def registry_path() -> Path:
    return _REGISTRY_PATH


def load_registry(path: Optional[Path] = None) -> List[dict]:
    """读注册表。文件坏了就抛错，不退回一份内置的印地语。"""
    global _CACHE
    if path is None and _CACHE is not None:
        return _CACHE
    source = Path(path) if path is not None else _REGISTRY_PATH
    data = json.loads(source.read_text(encoding="utf-8"))
    rows = data.get("languages") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"回复语言注册表没有 languages：{source}")
    cleaned: List[dict] = []
    seen = set()
    for row in rows:
        entry = _validate_entry(row)
        key = entry["tag"].casefold()
        if key in seen:
            raise ValueError(f"回复语言标签重复：{entry['tag']}")
        seen.add(key)
        cleaned.append(entry)
    if path is None:
        _CACHE = cleaned
    return cleaned


def _validate_entry(row: Any) -> dict:
    if not isinstance(row, dict):
        raise ValueError("语言记录必须是对象")
    tag = str(row.get("tag") or "").strip()
    if len(tag) < 2 or any(ch.isspace() for ch in tag):
        raise ValueError(f"非法语言标签：{tag!r}")
    direction = str(row.get("direction") or "").strip()
    if direction not in {"ltr", "rtl"}:
        raise ValueError(f"{tag} 的 direction 必须是 ltr 或 rtl")
    caps = row.get("capabilities") if isinstance(row.get("capabilities"), dict) else {}
    capabilities = {}
    for key in CAPABILITY_KEYS:
        value = str(caps.get(key) or "").strip()
        if value not in CAPABILITY_VALUES:
            raise ValueError(f"{tag} 的 {key} 必须是 supported、unsupported 或 unverified")
        capabilities[key] = value
    aliases = []
    for alias in row.get("aliases") or []:
        text = str(alias or "").strip()
        if text:
            aliases.append(text)
    return {
        "tag": tag,
        "display_name": str(row.get("display_name") or tag),
        "direction": direction,
        "aliases": aliases,
        "script_pattern": str(row.get("script_pattern") or ""),
        "min_script_chars": int(row.get("min_script_chars") or 2),
        "offline_acceptance": bool(row.get("offline_acceptance")),
        "capabilities": capabilities,
    }


def lookup_language(raw: str, registry: Optional[List[dict]] = None) -> Optional[dict]:
    """标签或别名。对不上返回 None，不改写成 zh 或 en。"""
    text = str(raw or "").strip()
    if not text:
        return None
    folded = text.casefold()
    for entry in registry if registry is not None else load_registry():
        if entry["tag"].casefold() == folded:
            return entry
        if any(str(alias).casefold() == folded for alias in entry["aliases"]):
            return entry
    return None


def canonical_tag(raw: str, registry: Optional[List[dict]] = None) -> str:
    entry = lookup_language(raw, registry)
    return str(entry["tag"]) if entry else ""


def capability_block_reason(entry: Optional[dict]) -> str:
    if not entry:
        return ""
    blocked = [
        key for key in CAPABILITY_KEYS
        if str((entry.get("capabilities") or {}).get(key) or "") == "unsupported"
    ]
    if not blocked:
        return ""
    return "capability_unsupported:" + ",".join(blocked)


def detect_peer_language(text: str, registry: Optional[List[dict]] = None) -> str:
    """只认注册表里的强文字证据。短拉丁、号码和空文本都不算。"""
    raw = str(text or "").strip()
    rows = registry if registry is not None else load_registry()
    if not raw:
        return ""
    hits: List[str] = []
    for entry in rows:
        pattern = str(entry.get("script_pattern") or "")
        if not pattern:
            continue
        try:
            found = re.findall(pattern, raw)
        except re.error:
            continue
        if len(found) >= int(entry.get("min_script_chars") or 2):
            hits.append(entry["tag"])
    if len(set(hits)) == 1:
        return hits[0]
    if hits:
        return ""
    try:
        from src.ai.lang_policy import EvidenceStrength, classify_evidence
        code, strength = classify_evidence(raw)
    except Exception:
        return ""
    if strength != EvidenceStrength.STRONG:
        return ""
    entry = lookup_language(str(code or ""), rows)
    return str(entry["tag"]) if entry else ""


def resolve_reply_policy(
    strategy: str,
    *,
    requested: str = "",
    peer_text: str = "",
    registry: Optional[List[dict]] = None,
) -> Dict[str, Any]:
    """解析这一侧要交给模型的语言。失败时 ok 为假，调用方不得发送。"""
    rows = registry if registry is not None else load_registry()
    name = str(strategy or "").strip()
    asked = str(requested or "").strip()
    if not name:
        return _decision(True, "", asked, "", "legacy", "")
    if name not in STRATEGIES:
        return _decision(False, name, asked, "", "", "bad_strategy")
    if name == "follow_peer":
        detected = detect_peer_language(peer_text, rows)
        if detected:
            entry = lookup_language(detected, rows)
            reason = capability_block_reason(entry)
            if reason:
                return _decision(False, name, asked, "", "", reason, entry)
            return _decision(True, name, asked, detected, "peer_message", "", entry)
        entry = lookup_language(asked, rows)
        if entry is None:
            return _decision(False, name, asked, "", "", "peer_language_unresolved")
        reason = capability_block_reason(entry)
        if reason:
            return _decision(False, name, asked, "", "", reason, entry)
        return _decision(True, name, asked, entry["tag"], "fallback", "", entry)
    entry = lookup_language(asked, rows)
    if entry is None:
        return _decision(False, name, asked, "", "", "bad_reply_language")
    reason = capability_block_reason(entry)
    if reason:
        return _decision(False, name, asked, "", "", reason, entry)
    source = "fixed" if name == "fixed" else "account_default"
    return _decision(True, name, asked, entry["tag"], source, "", entry)


def _decision(
    ok: bool,
    strategy: str,
    requested: str,
    resolved: str,
    source: str,
    reason: str,
    entry: Optional[dict] = None,
) -> Dict[str, Any]:
    caps = dict((entry or {}).get("capabilities") or {})
    return {
        "ok": bool(ok),
        "strategy": strategy,
        "requested": requested,
        "resolved": resolved if ok else "",
        "source": source if ok else "",
        "reason": reason,
        "direction": str((entry or {}).get("direction") or "") if ok else "",
        "capabilities": caps if ok else {},
        "apply": bool(ok and strategy and resolved),
    }


def public_language_row(entry: dict) -> dict:
    return {
        "tag": entry["tag"],
        "display_name": entry["display_name"],
        "direction": entry["direction"],
        "aliases": list(entry["aliases"]),
        "offline_acceptance": bool(entry["offline_acceptance"]),
        "capabilities": dict(entry["capabilities"]),
    }


def capabilities_payload(registry: Optional[List[dict]] = None) -> dict:
    """引擎真实可接受的策略和语言。languages 是标签列表，细节在 language_details。"""
    rows = registry if registry is not None else load_registry()
    return {
        "schema_version": SCHEMA_VERSION,
        "strategies": list(STRATEGIES),
        "languages": [row["tag"] for row in rows],
        "flags": {
            "language": "--reply-language",
            "strategy": "--reply-language-strategy",
            "fallback": "--reply-language-fallback",
            "account_a": "--a-reply-language",
            "account_b": "--b-reply-language",
        },
        "language_details": [public_language_row(row) for row in rows],
        "offline_acceptance": [row["tag"] for row in rows if row["offline_acceptance"]],
        "device_verified": [],
        "country_does_not_select_language": True,
        "legacy_when_unconfigured": True,
        "compatibility": {"reply_language_without_strategy": "fixed"},
        "http_decide_language_policy": False,
        "send": False,
        "command": "capabilities",
        "base_commit": BASE_COMMIT,
        "note": (
            "languages 来自引擎注册表，不按国家码生成。"
            "offline_acceptance 只含离线参数测试通过的标签。"
            "model_generation、phone_screen_read、unicode_input 为 unverified 时不表示真机可用。"
            "本 manifest 未与智拓联合运行验证。"
        ),
    }
