"""本机实例探测：智聊（config.local.yaml / 回环端口）与幻颜 AvatarHub（127.0.0.1:9000 健康检查）。

只接受回环地址。探测失败不抛错——什么都没发现时返回空列表，调用方仍可做纯心跳节点。
"""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

CHATX_PORTS = (18799, 18797)
AVATAR_URL = "http://127.0.0.1:9000"
_SKIP_DIRS = {"node_modules", ".git", "dist", "build", "venv", ".venv", "__pycache__", "Windows", "Program Files", "Program Files (x86)"}


def is_loopback_url(url: str) -> bool:
    try:
        u = urllib.parse.urlsplit(str(url or "").strip())
    except Exception:
        return False
    host = (u.hostname or "").strip().lower()
    return u.scheme in {"http", "https"} and host in {"127.0.0.1", "localhost", "::1"}


def sanitize_instances(items: Any, *, limit: int = 8) -> List[Dict[str, str]]:
    """给主控看的实例摘要：只有回环 URL，没有 token / 配置路径。"""
    out: List[Dict[str, str]] = []
    if not isinstance(items, list):
        return out
    for raw in items:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("base_url") or "").strip().rstrip("/")
        if not is_loopback_url(url):
            continue
        out.append({
            "name": str(raw.get("name") or "")[:40],
            "base_url": url[:160],
            "domain": str(raw.get("domain") or "")[:40],
            "role": str(raw.get("role") or "")[:20],
        })
        if len(out) >= limit:
            break
    return out


def probe_loopback(url: str, *, timeout: float = 0.8) -> bool:
    """有 HTTP 响应（含 401/404）即视为在听；连不上 / 5xx 视为不在。"""
    if not is_loopback_url(url):
        return False
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "chatx-agent-detect"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(getattr(r, "status", 200) or 200) < 500
    except urllib.error.HTTPError as e:
        return int(getattr(e, "code", 500) or 500) < 500
    except Exception:
        return False


def _section_scalar(text: str, section: str, key: str) -> str:
    m = re.search(rf"^{re.escape(section)}:\s*$((?:\n[ \t]+.*)*)", text, re.M)
    block = m.group(1) if m else ""
    m2 = re.search(rf"^[ \t]*{re.escape(key)}:[ \t]*['\"]?([^'\"\n#]*)", block, re.M)
    return m2.group(1).strip() if m2 else ""


def _top_scalar(text: str, key: str) -> str:
    m = re.search(rf"^{re.escape(key)}:[ \t]*['\"]?([^'\"\n#]*)", text, re.M)
    return m.group(1).strip() if m else ""


def _port_of(text: str) -> int:
    raw = _section_scalar(text, "web_admin", "port") or _top_scalar(text, "port")
    try:
        p = int(str(raw).strip() or 0)
    except (TypeError, ValueError):
        return 0
    return p if 1 <= p <= 65535 else 0


def default_search_roots() -> List[Path]:
    roots: List[Path] = []
    for key in ("APPDATA", "LOCALAPPDATA", "ProgramData"):
        v = os.environ.get(key)
        if v:
            roots.append(Path(v))
    if os.name != "nt":
        return roots
    drive = os.environ.get("SystemDrive") or "C:"
    users = Path(drive + "\\Users")
    if users.is_dir():
        try:
            children = list(users.iterdir())
        except OSError:
            children = []
        for user in children:
            if user.name.lower() in {"public", "default", "default user", "all users"}:
                continue
            roots.append(user / "AppData" / "Roaming")
            roots.append(user / "AppData" / "Local")
    return roots


def find_chatx_configs(roots: List[Path], *, max_depth: int = 5, limit: int = 6) -> List[Path]:
    found: List[Path] = []
    seen = set()
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            rel = Path(dirpath).relative_to(root)
            depth = len(rel.parts) if rel.parts != (".",) else 0
            if depth > max_depth:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            names = set(filenames)
            pick = None
            if "config.local.yaml" in names:
                pick = Path(dirpath) / "config.local.yaml"
            elif "config.yaml" in names:
                pick = Path(dirpath) / "config.yaml"
            if pick is None or pick in seen:
                continue
            if "config" not in pick.parts:
                continue
            seen.add(pick)
            found.append(pick)
            if len(found) >= limit:
                return found
    return found


def _listening(base: str, paths: tuple, probe: Callable[[str], bool]) -> bool:
    base = base.rstrip("/")
    for path in paths:
        try:
            if probe(base + path):
                return True
        except Exception:
            continue
    return False


def _from_config(path: Path, probe: Callable[[str], bool]) -> Optional[Dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if "web_admin" not in text:
        return None
    if _top_scalar(text, "domain") == "fleet_control":
        return None
    port = _port_of(text) or CHATX_PORTS[0]
    base = f"http://127.0.0.1:{port}"
    domain = _top_scalar(text, "domain")
    return {
        "name": "chatx",
        "base_url": base,
        "domain": domain[:40],
        "role": "",
        "config_path": str(path),
        "up": "1" if _listening(base, ("/health", "/api/accounts/fleet-health", "/login"), probe) else "0",
    }


def detect_instances(*, probe: Optional[Callable[[str], bool]] = None, config_paths: Optional[List[Any]] = None,
                     search: bool = False, roots: Optional[List[Path]] = None,
                     avatar_url: str = AVATAR_URL, timeout: float = 0.8) -> List[Dict[str, str]]:
    """返回本机应登记的实例。空列表 = 纯心跳节点（等价于安装器 -NoInstance）。"""
    probe_fn = probe or (lambda url: probe_loopback(url, timeout=timeout))
    paths: List[Path] = [Path(p) for p in (config_paths or [])]
    if search:
        paths.extend(find_chatx_configs(roots if roots is not None else default_search_roots()))
    found: List[Dict[str, str]] = []
    seen = set()
    for p in paths:
        inst = _from_config(p, probe_fn)
        if not inst or inst["base_url"] in seen:
            continue
        seen.add(inst["base_url"])
        found.append(inst)
    if not any(i.get("role") != "health" for i in found):
        for port in CHATX_PORTS:
            base = f"http://127.0.0.1:{port}"
            if base in seen:
                continue
            if _listening(base, ("/health", "/api/accounts/fleet-health", "/login"), probe_fn):
                found.append({"name": "chatx", "base_url": base, "domain": "", "role": "", "config_path": "", "up": "1"})
                seen.add(base)
                break
    n = 0
    for inst in found:
        n += 1
        inst["name"] = "chatx" if n == 1 else f"chatx-{n}"
    avatar = str(avatar_url or "").rstrip("/")
    if is_loopback_url(avatar) and _listening(avatar, ("/health", "/api/health"), probe_fn):
        found.append({"name": "avatarhub", "base_url": avatar, "domain": "avatar_hub", "role": "health", "config_path": "", "up": "1"})
    return found
