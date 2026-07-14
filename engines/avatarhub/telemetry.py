# -*- coding: utf-8 -*-
"""telemetry.py — 隐私优先的匿名安装健康回执。

设计红线（务必遵守）：
  - **默认关闭**：未明确开启则只在本地写回执、绝不外发（opt-in）。
  - **最小化**：只采组件级成败/字节/耗时/错误【类名】、源【主机名】、档位、版本、粗粒度 GPU。
    绝不采集文件路径、用户名、错误信息原文（可能含路径）、IP、任何可定位个人的字段。
  - **可离线**：无网络/无 telemetry_url 时一切照常，回执只落本地供用户自查。
  - **可关闭/可重置**：环境变量 AVATARHUB_TELEMETRY=0 强制关；anon_id 仅为去重、随机、可删。

回执去向：始终写 BASE/runtime/telemetry/<ts>.json（本地、可审计）；仅当 enabled() 且
manifest.telemetry_url 存在时，后台 best-effort POST（短超时、永不抛错、永不阻塞安装）。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import app_config

TELE_DIR = app_config.BASE / "runtime" / "telemetry"
TELE_CONF = TELE_DIR / "config.json"
SCHEMA = 1
KEEP_LOCAL = 50            # 本地回执最多保留份数（滚动清理，避免无限堆积）


def _load_conf() -> dict:
    try:
        return json.loads(TELE_CONF.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_conf(d: dict):
    try:
        TELE_DIR.mkdir(parents=True, exist_ok=True)
        TELE_CONF.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def enabled() -> bool:
    """是否允许外发：环境变量优先（AVATARHUB_TELEMETRY=0/off/false 关，1/on/true 开），
    否则看本地设置 config.json["enabled"]，未设视为 False（默认关闭 / opt-in）。"""
    env = os.environ.get("AVATARHUB_TELEMETRY", "").strip().lower()
    if env in ("0", "off", "false", "no"):
        return False
    if env in ("1", "on", "true", "yes"):
        return True
    return bool(_load_conf().get("enabled", False))


def set_enabled(on: bool):
    c = _load_conf()
    c["enabled"] = bool(on)
    _save_conf(c)


def anon_id() -> str:
    """随机匿名标识（仅供服务端对回执去重，不含任何个人信息）；缺失则生成并持久化。"""
    c = _load_conf()
    aid = c.get("anon_id")
    if not aid:
        aid = uuid.uuid4().hex
        c["anon_id"] = aid
        _save_conf(c)
    return aid


def _host(src: str) -> str:
    """只取来源主机名（丢弃路径/查询，避免泄露目录结构）；本地路径记为 'local'。"""
    try:
        if src.lower().startswith(("http://", "https://")):
            return urlparse(src).hostname or "?"
    except Exception:
        pass
    return "local"


class Recorder:
    """累计一次安装/更新/回滚会话的组件级结果，最后 finalize 成匿名回执。"""

    def __init__(self):
        self.t0 = time.time()
        self.items: list[dict] = []

    def add(self, cid: str, ok: bool, size_bytes: int, secs: float, err: str = ""):
        self.items.append({"cid": cid, "ok": bool(ok),
                           "bytes": int(size_bytes or 0), "secs": round(secs, 2),
                           "err": err[:40]})   # err 只应传【类名】，再截断以防意外

    def finalize(self, kind: str, edition: str, manifest: dict,
                 sources: list[str] | None = None, channel: str = "",
                 gpu: dict | None = None) -> dict:
        ok = sum(1 for it in self.items if it["ok"])
        rec = {
            "schema": SCHEMA,
            "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "kind": kind,
            "manifest_version": manifest.get("version", ""),
            "channel": channel or os.environ.get("AVATARHUB_CHANNEL", ""),
            "edition": edition or "",
            "platform": manifest.get("platform", ""),
            "sources": [_host(s) for s in (sources or [])],
            "total_bytes": sum(it["bytes"] for it in self.items),
            "total_secs": round(time.time() - self.t0, 1),
            "ok": ok,
            "fail": len(self.items) - ok,
            "items": self.items,
        }
        if gpu:
            rec["gpu"] = str(gpu.get("name", ""))[:48]
            rec["vram_gb"] = round((gpu.get("total_mb", 0) or 0) / 1024)
        if enabled():
            rec["anon_id"] = anon_id()      # 仅在开启外发时附带去重 id
        return rec


def _write_local(receipt: dict) -> Path | None:
    try:
        TELE_DIR.mkdir(parents=True, exist_ok=True)
        ts = receipt.get("ts", "").replace(":", "").replace("-", "")[:15] or str(int(time.time()))
        out = TELE_DIR / f"{ts}_{receipt.get('kind','x')}.json"
        out.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        files = sorted(TELE_DIR.glob("*_*.json"))
        for old in files[:-KEEP_LOCAL]:        # 滚动清理旧回执
            old.unlink(missing_ok=True)
        return out
    except Exception:
        return None


def submit(receipt: dict, manifest: dict, log=None):
    """始终写本地回执；仅当 enabled() 且有 telemetry_url 时后台 best-effort 外发（永不阻塞/抛错）。"""
    _write_local(receipt)
    url = (manifest.get("telemetry_url") or "").strip()
    if not (enabled() and url.startswith(("http://", "https://"))):
        return

    def work():
        try:
            data = json.dumps(receipt, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=8).close()
            if log:
                log("已发送匿名安装回执（用于改进发布质量，可在维护里关闭）。")
        except Exception:
            pass        # 遥测失败绝不影响主流程

    threading.Thread(target=work, daemon=True).start()
