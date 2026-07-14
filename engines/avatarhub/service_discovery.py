#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Discover offloaded service hosts after LAN/IP changes.

Strategy:
1. Try cached/last-known hosts first (fast path).
2. If any service is missing, scan the local /24 for SSH ED25519 host keys and
   match configured SHA256 fingerprints.
3. Emit Windows batch `set "SVC_*=http://ip:port"` lines for env_config.bat.

No third-party dependencies. This deliberately treats SSH host key fingerprints
as the machine identity because they survive DHCP/subnet changes.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import hashlib
import ipaddress
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "service_discovery.json"
CACHE = BASE / "data" / "service_discovery_cache.json"


DEFAULT_CONFIG = {
    "timeout_sec": 0.8,
    "services": {
        "SVC_STT": {
            "service": "stt",
            "port": 7854,
            "task": "STT",
            "ssh_ed25519_sha256": "SHA256:iEqaHX1pUKEK89elY4YKH+yf7yfUMVgKoflX26bjcjo",
            "last_known_host": "192.168.1.51",
        },
        "SVC_FACESWAP": {
            "service": "faceswap",
            "port": 8000,
            "task": "FaceSwap",
            "ssh_ed25519_sha256": "SHA256:iqXWpxlW2Xp6g7ryjMFF7w4sOEvw3m1ep8grz3C71do",
            "last_known_host": "192.168.1.43",
        },
        "SVC_EMOTION_TTS": {
            "service": "emotion_tts",
            "port": 7852,
            "task": "EmotionTTS",
            "ssh_ed25519_sha256": "SHA256:iqXWpxlW2Xp6g7ryjMFF7w4sOEvw3m1ep8grz3C71do",
            "last_known_host": "192.168.1.43",
        },
    },
}


def _load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _save_json(path: Path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _local_subnet() -> str:
    override = os.environ.get("AVATARHUB_DISCOVERY_SUBNET", "").strip()
    if override:
        return override
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    finally:
        s.close()
    net = ipaddress.ip_network(ip + "/24", strict=False)
    return str(net)


# 探活端点缓存：记住每个 base 上一次成功的健康端点，避免对只有 /health 的服务
#   每轮都先打一次必定 404 的 /healthz。
_PROBE_EP_CACHE: dict = {}


def _service_ok(host: str, port: int, timeout: float) -> bool:
    base = f"http://{host}:{port}"
    cached = _PROBE_EP_CACHE.get(base)
    eps = ("/healthz", "/health")
    if cached:
        eps = (cached,) + tuple(e for e in eps if e != cached)
    for ep in eps:
        try:
            with urllib.request.urlopen(base + ep, timeout=timeout) as r:
                if r.status == 200:
                    _PROBE_EP_CACHE[base] = ep
                    return True
                if ep == "/healthz" and r.status in (404, 405):
                    continue
                return False
        except Exception:
            if ep == "/healthz":
                continue
            return False
    return False


def _fingerprint_from_keyscan_line(line: str) -> tuple[str, str] | None:
    parts = line.strip().split()
    if len(parts) < 3 or parts[1] != "ssh-ed25519":
        return None
    try:
        raw = base64.b64decode(parts[2].encode("ascii"))
        fp = "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")
        host = parts[0].split(",", 1)[0].strip("[]")
        return host, fp
    except Exception:
        return None


def _keyscan_one(host: str, timeout: float) -> tuple[str, str] | None:
    try:
        r = subprocess.run(
            ["ssh-keyscan", "-T", str(max(1, int(timeout + 0.5))), "-t", "ed25519", host],
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
            timeout=timeout + 1.5,
        )
        for line in (r.stdout or "").splitlines():
            item = _fingerprint_from_keyscan_line(line)
            if item:
                return item
    except Exception:
        return None
    return None


def _scan_subnet(subnet: str, wanted_fps: set[str], timeout: float) -> dict[str, str]:
    hosts = [str(ip) for ip in ipaddress.ip_network(subnet, strict=False).hosts()]
    found: dict[str, str] = {}
    with cf.ThreadPoolExecutor(max_workers=48) as ex:
        futs = {ex.submit(_keyscan_one, h, timeout): h for h in hosts}
        for fut in cf.as_completed(futs):
            item = fut.result()
            if not item:
                continue
            host, fp = item
            if fp in wanted_fps and fp not in found:
                found[fp] = host
                if len(found) >= len(wanted_fps):
                    break
    return found


def discover(force_scan: bool = False) -> dict:
    cfg = _load_json(CONFIG, DEFAULT_CONFIG)
    cache = _load_json(CACHE, {})
    timeout = float(cfg.get("timeout_sec") or 0.8)
    services = cfg.get("services") or {}
    out = {}
    missing_fps = set()

    for env_name, spec in services.items():
        port = int(spec["port"])
        candidates = []
        if not force_scan:
            candidates.extend([
                (cache.get(env_name) or {}).get("host", ""),
                spec.get("last_known_host", ""),
            ])
        host = ""
        for cand in candidates:
            if cand and _service_ok(cand, port, timeout):
                host = cand
                break
        if host:
            out[env_name] = {**spec, "host": host, "url": f"http://{host}:{port}", "source": "cache"}
        else:
            missing_fps.add(str(spec.get("ssh_ed25519_sha256", "")))

    missing_fps.discard("")
    if missing_fps:
        fp_to_host = _scan_subnet(_local_subnet(), missing_fps, timeout)
        for env_name, spec in services.items():
            if env_name in out:
                continue
            fp = str(spec.get("ssh_ed25519_sha256", ""))
            host = fp_to_host.get(fp, "")
            if host and _service_ok(host, int(spec["port"]), timeout):
                out[env_name] = {**spec, "host": host, "url": f"http://{host}:{spec['port']}", "source": "scan"}

    _save_json(CACHE, {k: {"host": v["host"], "url": v["url"], "ts": int(time.time())}
                       for k, v in out.items()})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit-bat", action="store_true", help='print set "SVC_*=http://..." lines')
    ap.add_argument("--json", action="store_true", help="print JSON discovery result")
    ap.add_argument("--force-scan", action="store_true", help="skip cached hosts and scan subnet")
    args = ap.parse_args()

    res = discover(force_scan=args.force_scan)
    if args.json or not args.emit_bat:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    if args.emit_bat:
        for env_name in sorted(res):
            print(f'set "{env_name}={res[env_name]["url"]}"')
    return 0 if res else 1


if __name__ == "__main__":
    sys.exit(main())
