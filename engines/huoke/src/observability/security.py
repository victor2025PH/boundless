"""
Security utilities — encrypted config storage + validation + sanitization.

Features:
- Fernet symmetric encryption for API keys and secrets
- Config validation (required fields, format checks)
- Log sanitization (mask sensitive data before logging)
- Secure file permissions check

Design: Zero external dependencies beyond cryptography (ships with Python).
If cryptography not installed, falls back to base64 obfuscation (not secure,
but better than plaintext — with a loud warning).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.host.device_registry import data_dir

log = logging.getLogger(__name__)

try:
    from cryptography.fernet import Fernet
    _HAS_FERNET = True
except ImportError:
    _HAS_FERNET = False


class SecureStore:
    """
    Encrypted key-value store for sensitive configuration.

    Usage:
        store = SecureStore()  # auto-generates key on first use
        store.set("deepseek_api_key", "sk-xxx...")
        key = store.get("deepseek_api_key")
    """

    def __init__(self, store_path: Optional[str] = None, key_path: Optional[str] = None):
        base = data_dir()
        base.mkdir(parents=True, exist_ok=True)
        self._store_path = Path(store_path) if store_path else base / ".secrets.enc"
        self._key_path = Path(key_path) if key_path else base / ".secret.key"
        self._lock = threading.Lock()
        self._fernet = self._init_encryption()

    def _init_encryption(self):
        if not _HAS_FERNET:
            log.warning("cryptography not installed — secrets stored with base64 only (NOT SECURE)")
            return None

        if self._key_path.exists():
            key = self._key_path.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            self._key_path.write_bytes(key)
            try:
                os.chmod(str(self._key_path), 0o600)
            except OSError:
                pass
            log.info("Generated new encryption key: %s", self._key_path)

        return Fernet(key)

    def _load(self) -> dict:
        if not self._store_path.exists():
            return {}
        raw = self._store_path.read_bytes()
        if self._fernet:
            try:
                decrypted = self._fernet.decrypt(raw)
                return json.loads(decrypted)
            except Exception:
                log.error("Failed to decrypt secrets store")
                return {}
        else:
            try:
                decoded = base64.b64decode(raw)
                return json.loads(decoded)
            except Exception:
                return {}

    def _save(self, data: dict):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if self._fernet:
            encrypted = self._fernet.encrypt(raw)
        else:
            encrypted = base64.b64encode(raw)
        self._store_path.write_bytes(encrypted)
        try:
            os.chmod(str(self._store_path), 0o600)
        except OSError:
            pass

    def set(self, key: str, value: str):
        with self._lock:
            data = self._load()
            data[key] = value
            self._save(data)

    def get(self, key: str, default: str = "") -> str:
        with self._lock:
            data = self._load()
            return data.get(key, default)

    def delete(self, key: str) -> bool:
        with self._lock:
            data = self._load()
            if key in data:
                del data[key]
                self._save(data)
                return True
            return False

    def list_keys(self) -> List[str]:
        with self._lock:
            return list(self._load().keys())

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._load()


# ---------------------------------------------------------------------------
# Config Validation
# ---------------------------------------------------------------------------

class ConfigValidator:
    """Validate configuration files before use."""

    @staticmethod
    def validate_devices(config: dict) -> List[str]:
        """Validate devices.yaml structure."""
        errors = []
        if "devices" not in config:
            errors.append("Missing 'devices' section")
            return errors
        for i, dev in enumerate(config.get("devices", [])):
            if "device_id" not in dev and "serial" not in dev:
                errors.append(f"Device #{i}: missing device_id or serial")
            if "display_name" not in dev:
                errors.append(f"Device #{i}: missing display_name")
        return errors

    @staticmethod
    def validate_compliance(config: dict) -> List[str]:
        """Validate compliance.yaml structure."""
        errors = []
        for platform in ("telegram", "linkedin", "whatsapp"):
            if platform not in config:
                errors.append(f"Missing platform: {platform}")
                continue
            pdata = config[platform]
            if "actions" not in pdata:
                errors.append(f"{platform}: missing 'actions' section")
            for action, limits in pdata.get("actions", {}).items():
                for field in ("hourly", "daily", "cooldown_sec"):
                    if field not in limits:
                        errors.append(f"{platform}.{action}: missing '{field}'")
        return errors

    @staticmethod
    def validate_ai(config: dict) -> List[str]:
        """Validate ai.yaml structure."""
        errors = []
        if "llm" not in config:
            errors.append("Missing 'llm' section")
        else:
            llm = config["llm"]
            if llm.get("provider") not in ("deepseek", "openai", "local", ""):
                errors.append(f"Unknown LLM provider: {llm.get('provider')}")
        return errors


# ---------------------------------------------------------------------------
# Log Sanitization
# ---------------------------------------------------------------------------

# Patterns that should be masked in logs
_SENSITIVE_PATTERNS = [
    (re.compile(r'(sk-[a-zA-Z0-9]{20,})'), r'sk-***REDACTED***'),
    (re.compile(r'(api[_-]?key["\s:=]+)["\']?([a-zA-Z0-9_-]{16,})', re.IGNORECASE),
     r'\1***REDACTED***'),
    (re.compile(r'(password["\s:=]+)["\']?(\S{4,})', re.IGNORECASE),
     r'\1***REDACTED***'),
    (re.compile(r'(bearer\s+)([a-zA-Z0-9._-]{20,})', re.IGNORECASE),
     r'\1***REDACTED***'),
    (re.compile(r'(\+?\d{1,3}[-.\s]?\d{3,4}[-.\s]?\d{3,4}[-.\s]?\d{2,4})'),
     r'***PHONE***'),
]


def sanitize(text: str) -> str:
    """Remove sensitive data from a string before logging."""
    result = text
    for pattern, replacement in _SENSITIVE_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def sanitize_dict(d: dict) -> dict:
    """Sanitize all string values in a dict."""
    result = {}
    sensitive_keys = {"api_key", "password", "secret", "token", "key",
                      "authorization", "phone", "phone_number"}
    for k, v in d.items():
        if k.lower() in sensitive_keys:
            result[k] = "***REDACTED***"
        elif isinstance(v, str):
            result[k] = sanitize(v)
        elif isinstance(v, dict):
            result[k] = sanitize_dict(v)
        else:
            result[k] = v
    return result


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_secure_store: Optional[SecureStore] = None
_ss_lock = threading.Lock()


def get_secure_store() -> SecureStore:
    global _secure_store
    if _secure_store is None:
        with _ss_lock:
            if _secure_store is None:
                _secure_store = SecureStore()
    return _secure_store
