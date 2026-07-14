# -*- coding: utf-8 -*-
"""
设备指纹注册表。

通过 IMEI / ro.serialno / android_id 持久识别设备，
即使 USB 重插导致 ADB 串号改变也能继承原编号、分组和资产。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.host.device_registry import PROJECT_ROOT

log = logging.getLogger(__name__)


class DeviceRegistry:
    """
    Persistent device identity registry backed by config/device_registry.json.

    Each entry is keyed by the device's fingerprint (IMEI preferred, then
    ro.serialno, then android_id) and stores:
      - current_serial: the ADB serial currently associated
      - previous_serials: list of past ADB serials
      - imei, hw_serial, android_id: raw fingerprint sources
      - model: device model string
      - number: assigned display number (1, 2, ...)
      - alias: human label ("01号")
      - created_at: first-seen timestamp
    """

    def __init__(self, config_root: str | Path):
        self._path = Path(config_root) / "config" / "device_registry.json"
        self._lock = threading.Lock()

    # ── Persistence ──────────────────────────────────────────────────────

    def _load(self) -> dict:
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save(self, data: dict):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ── Core API ─────────────────────────────────────────────────────────

    def lookup(self, fingerprint: str) -> Optional[dict]:
        """Look up a device by fingerprint. Returns registry entry or None."""
        if not fingerprint:
            return None
        with self._lock:
            registry = self._load()
            entry = registry.get(fingerprint)
            if entry:
                return entry
            for fp, entry in registry.items():
                if (fingerprint == entry.get("imei") or
                        fingerprint == entry.get("hw_serial") or
                        fingerprint == entry.get("android_id")):
                    return entry
        return None

    def lookup_by_serial(self, serial: str) -> Optional[dict]:
        """Find a registry entry whose current_serial matches."""
        with self._lock:
            registry = self._load()
            for entry in registry.values():
                if entry.get("current_serial") == serial:
                    return entry
        return None

    def register(self, fingerprint: str, serial: str,
                 number: int, alias: str,
                 imei: str = "", hw_serial: str = "",
                 android_id: str = "", model: str = "") -> dict:
        """Register a new device or update an existing entry."""
        with self._lock:
            registry = self._load()
            existing = registry.get(fingerprint, {})
            prev = existing.get("previous_serials", [])
            old_serial = existing.get("current_serial", "")
            if old_serial and old_serial != serial and old_serial not in prev:
                prev.append(old_serial)

            registry[fingerprint] = {
                "current_serial": serial,
                "previous_serials": prev,
                "imei": imei or existing.get("imei", ""),
                "hw_serial": hw_serial or existing.get("hw_serial", ""),
                "android_id": android_id or existing.get("android_id", ""),
                "model": model or existing.get("model", ""),
                "number": number,
                "alias": alias,
                "created_at": existing.get("created_at",
                                           datetime.now().isoformat()),
            }
            self._save(registry)
            return registry[fingerprint]

    def update_serial(self, fingerprint: str, new_serial: str) -> Optional[str]:
        """
        Update current_serial for a fingerprint entry.
        Returns the old serial if changed, None otherwise.
        """
        with self._lock:
            registry = self._load()
            entry = registry.get(fingerprint)
            if not entry:
                return None
            old = entry.get("current_serial", "")
            if old == new_serial:
                return None
            prev = entry.get("previous_serials", [])
            if old and old not in prev:
                prev.append(old)
            entry["current_serial"] = new_serial
            entry["previous_serials"] = prev
            self._save(registry)
            return old

    def get_number(self, fingerprint: str) -> Optional[int]:
        entry = self.lookup(fingerprint)
        return entry.get("number") if entry else None

    def set_number(self, fingerprint: str, number: int):
        """Update the assigned number for an entry."""
        with self._lock:
            registry = self._load()
            if fingerprint in registry:
                registry[fingerprint]["number"] = number
                registry[fingerprint]["alias"] = f"{number:02d}号"
                self._save(registry)

    def get_all(self) -> dict:
        with self._lock:
            return self._load()

    def all_used_numbers(self) -> set[int]:
        """Return set of all numbers currently assigned."""
        registry = self._load()
        used = set()
        for entry in registry.values():
            n = entry.get("number")
            if isinstance(n, int) and n > 0:
                used.add(n)
        return used

    def next_available_number(self) -> int:
        used = self.all_used_numbers()
        num = 1
        while num in used:
            num += 1
        return num

    # ── Migration ────────────────────────────────────────────────────────

    def migrate_serial(self, old_serial: str, new_serial: str,
                       config_root: str | Path = ""):
        """
        Update all references from old_serial to new_serial across:
          - device_aliases.json
          - device_assets.json
          - SQLite device_group_members
        """
        root = Path(config_root) if config_root else self._path.parent.parent
        aliases_path = root / "config" / "device_aliases.json"
        assets_path = root / "config" / "device_assets.json"

        self._migrate_json_file(aliases_path, old_serial, new_serial)
        self._migrate_json_file(assets_path, old_serial, new_serial)
        self._migrate_sqlite_groups(old_serial, new_serial)

        log.info("[注册表] 串号迁移完成: %s → %s", old_serial[:8], new_serial[:8])

    @staticmethod
    def _migrate_json_file(path: Path, old_key: str, new_key: str):
        if not path.exists():
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if old_key in data:
                data[new_key] = data.pop(old_key)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                log.info("[注册表] 迁移 %s: %s → %s",
                         path.name, old_key[:8], new_key[:8])
        except Exception as e:
            log.warning("[注册表] 迁移文件 %s 失败: %s", path.name, e)

    @staticmethod
    def _migrate_sqlite_groups(old_id: str, new_id: str):
        try:
            from src.host.database import get_conn
            with get_conn() as conn:
                conn.execute(
                    "UPDATE device_group_members SET device_id = ? "
                    "WHERE device_id = ?",
                    (new_id, old_id),
                )
            log.info("[注册表] SQLite 分组成员迁移: %s → %s", old_id[:8], new_id[:8])
        except Exception as e:
            log.warning("[注册表] SQLite 迁移失败: %s", e)

    # ── Bootstrap: build registry from existing aliases ───────────────────

    def bootstrap_from_aliases(self, aliases_path: Path, manager=None):
        """
        One-time migration: populate registry from an existing
        device_aliases.json keyed by ADB serial.
        Only adds entries that aren't already in the registry.
        """
        if not aliases_path.exists():
            return
        try:
            with open(aliases_path, "r", encoding="utf-8") as f:
                aliases = json.load(f)
        except Exception:
            return

        with self._lock:
            registry = self._load()
            known_serials = set()
            for entry in registry.values():
                known_serials.add(entry.get("current_serial", ""))
                known_serials.update(entry.get("previous_serials", []))

            added = 0
            for serial, info in aliases.items():
                if serial in known_serials:
                    continue
                fp = ""
                model = ""
                imei = hw_serial = android_id = ""
                if manager:
                    dev = manager.get_device_info(serial)
                    if dev:
                        fp = dev.fingerprint
                        model = dev.model
                        imei = dev.imei
                        hw_serial = dev.hw_serial
                        android_id = dev.android_id
                if not fp:
                    fp = f"serial:{serial}"
                registry[fp] = {
                    "current_serial": serial,
                    "previous_serials": [],
                    "imei": imei,
                    "hw_serial": hw_serial,
                    "android_id": android_id,
                    "model": model,
                    "number": info.get("number", 0),
                    "alias": info.get("alias", ""),
                    "created_at": datetime.now().isoformat(),
                }
                added += 1

            if added:
                self._save(registry)
                log.info("[注册表] 从 aliases 导入 %d 条设备记录", added)


# ── Singleton ────────────────────────────────────────────────────────────

_registry: Optional[DeviceRegistry] = None
_registry_lock = threading.Lock()


def get_device_registry(config_root: str | Path = "") -> DeviceRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                if not config_root:
                    config_root = PROJECT_ROOT
                _registry = DeviceRegistry(config_root)
    return _registry
