# -*- coding: utf-8 -*-
"""
stream_out.py — Phase E StreamOut 插件层

统一抽象多种输出通道，Hub 通过 /api/stream_out/* 调度：
  - vcam      : OBS 虚拟摄像头（pyvirtualcam @ vcam_server:7870）
  - webrtc    : 浏览器/手机 WebRTC 拉流（同中枢，共享帧源）
  - rtmp      : RTMP 推流（占位，待 ffmpeg 集成）
  - recorder  : 本地 MP4 录制（占位）
  - avatar3d  : 3D 全身数字人输出（Phase 10 占位）
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

import app_config
VCAM_URL = os.environ.get("VCAM_URL", "http://127.0.0.1:7870")
RECORD_DIR = os.environ.get("STREAMOUT_RECORD_DIR", str(app_config.BASE / "recordings"))


# ── 探测结果短 TTL 缓存 ──────────────────────────────────────────────
#   vcam_server 离线时，status_all 的 5 插件 × 多探测各付一次连接超时，
#   曾把看板冷快照拖到 12s+（页面全程「加载中…」）。同一 URL 的 GET 探测
#   在 TTL 内共享一次结果；控制类 POST(start/stop/record) 不走缓存。
_PROBE_TTL = float(os.environ.get("STREAMOUT_PROBE_TTL", "4"))
_probe_cache: dict[str, tuple[float, tuple[bool, dict]]] = {}
_probe_locks: dict[str, asyncio.Lock] = {}


async def _get_json(url: str, timeout: float = 3.0) -> tuple[bool, dict]:
    hit = _probe_cache.get(url)
    if hit and time.time() - hit[0] < _PROBE_TTL:
        return hit[1]
    lock = _probe_locks.setdefault(url, asyncio.Lock())
    async with lock:
        hit = _probe_cache.get(url)
        if hit and time.time() - hit[0] < _PROBE_TTL:
            return hit[1]
        res: tuple[bool, dict] = (False, {})
        try:
            import httpx
            async with httpx.AsyncClient(timeout=timeout) as cli:
                r = await cli.get(url)
                if r.status_code == 200:
                    res = (True, r.json())
        except Exception:
            pass
        _probe_cache[url] = (time.time(), res)
        return res


async def _vcam_health() -> tuple[bool, dict]:
    try:
        return await _get_json(f"{VCAM_URL}/health")
    except Exception:
        pass
    return False, {}


class StreamOutPlugin(ABC):
    name: str = ""
    label: str = ""
    description: str = ""

    @abstractmethod
    async def status(self) -> dict:
        ...

    @abstractmethod
    async def start(self, ctx: dict) -> dict:
        ...

    @abstractmethod
    async def stop(self) -> dict:
        ...


class _VcamBase(StreamOutPlugin):
    """vcam_server 广播中枢的共用探测/控制。"""

    async def _health(self) -> tuple[bool, dict]:
        return await _get_json(f"{VCAM_URL}/health")

    async def _hub_status(self) -> tuple[bool, dict]:
        return await _get_json(f"{VCAM_URL}/status")


class VcamPlugin(_VcamBase):
    name = "vcam"
    label = "虚拟摄像头"
    description = "OBS Virtual Camera · 会议/直播软件选用"

    async def status(self) -> dict:
        (ok, info), (st_ok, st) = await asyncio.gather(self._health(), self._hub_status())
        return {
            "plugin": self.name, "available": ok, "active": bool(st.get("playing")),
            "reachable": ok, "url": VCAM_URL, "detail": st if st_ok else info,
        }

    async def start(self, ctx: dict) -> dict:
        ok, _ = await self._health()
        if not ok:
            return {"ok": False, "plugin": self.name, "detail": "vcam_server 不可达"}
        return {"ok": True, "plugin": self.name, "detail": "虚拟摄像头已就绪（对话口型自动推流）"}

    async def stop(self) -> dict:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as cli:
                await cli.post(f"{VCAM_URL}/clear")
        except Exception:
            pass
        return {"ok": True, "plugin": self.name}


class WebrtcPlugin(_VcamBase):
    name = "webrtc"
    label = "WebRTC 直播"
    description = "手机/浏览器低延迟连续流 · 与 vcam 共享帧源"

    async def status(self) -> dict:
        (ok, info), (st_ok, st) = await asyncio.gather(self._health(), self._hub_status())
        pcs = st.get("webrtc_peers", st.get("pcs", 0)) if st_ok else 0
        return {
            "plugin": self.name, "available": ok,
            "active": bool(pcs), "peers": pcs,
            "preview_url": f"{VCAM_URL}/" if ok else "",
            "signaling": "/api/vcam/webrtc/offer",
        }

    async def start(self, ctx: dict) -> dict:
        ok, _ = await self._health()
        if not ok:
            return {"ok": False, "plugin": self.name, "detail": "广播中枢不可达"}
        return {
            "ok": True, "plugin": self.name,
            "preview_url": f"{VCAM_URL}/",
            "signaling": "/api/vcam/webrtc/offer",
            "detail": "WebRTC 已就绪；phone 页点 📺 直播或打开 preview_url",
        }

    async def stop(self) -> dict:
        return {"ok": True, "plugin": self.name, "detail": "WebRTC 由对端断开即停"}


class RtmpPlugin(StreamOutPlugin):
    name = "rtmp"
    label = "RTMP 推流"
    description = "抖音/B站 RTMP · ffmpeg 从广播中枢扇出"

    async def _so_status(self) -> dict:
        return (await _get_json(f"{VCAM_URL}/stream_out/status"))[1]

    async def status(self) -> dict:
        url = os.environ.get("STREAMOUT_RTMP_URL", "")
        so = await self._so_status()
        active = bool(so.get("rtmp_active"))
        return {
            "plugin": self.name,
            "available": bool(so.get("ffmpeg")) or bool(_ffmpeg_available()),
            "active": active,
            "rtmp_url_configured": bool(url),
            "rtmp_url": so.get("rtmp_url") or url,
            "detail": "需 ffmpeg + RTMP 推流地址",
        }

    async def start(self, ctx: dict) -> dict:
        url = (ctx.get("rtmp_url") or os.environ.get("STREAMOUT_RTMP_URL", "")).strip()
        if not url:
            return {"ok": False, "plugin": self.name, "detail": "请设置 RTMP URL 或环境变量 STREAMOUT_RTMP_URL"}
        ok, _ = await _vcam_health()
        if not ok:
            return {"ok": False, "plugin": self.name, "detail": "vcam_server 不可达"}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15.0) as cli:
                r = await cli.post(f"{VCAM_URL}/stream_out/rtmp/start", json={"url": url})
                data = r.json() if r.status_code == 200 else {"detail": r.text[:200]}
                if r.status_code != 200:
                    return {"ok": False, "plugin": self.name, "detail": data.get("detail", r.text[:120])}
                return {"ok": True, "plugin": self.name, "url": url, **data}
        except Exception as e:
            return {"ok": False, "plugin": self.name, "detail": str(e)}

    async def stop(self) -> dict:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as cli:
                r = await cli.post(f"{VCAM_URL}/stream_out/rtmp/stop")
                return r.json() if r.status_code == 200 else {"ok": True, "plugin": self.name}
        except Exception:
            return {"ok": True, "plugin": self.name}


class RecorderPlugin(StreamOutPlugin):
    name = "recorder"
    label = "本地录制"
    description = "录制广播中枢输出为 MP4"

    async def _so_status(self) -> dict:
        return (await _get_json(f"{VCAM_URL}/stream_out/status"))[1]

    async def status(self) -> dict:
        out = os.environ.get("STREAMOUT_RECORD_DIR", str(app_config.BASE / "recordings"))
        so = await self._so_status()
        return {
            "plugin": self.name, "available": True,
            "active": bool(so.get("recording")),
            "output_dir": out,
            "record_path": so.get("record_path", ""),
        }

    async def start(self, ctx: dict) -> dict:
        ok, _ = await _vcam_health()
        if not ok:
            return {"ok": False, "plugin": self.name, "detail": "vcam_server 不可达"}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as cli:
                r = await cli.post(
                    f"{VCAM_URL}/stream_out/record/start",
                    json={"profile": ctx.get("profile", "")})
                data = r.json() if r.status_code == 200 else {}
                if r.status_code != 200:
                    return {"ok": False, "plugin": self.name,
                            "detail": data.get("detail", r.text[:120])}
                return {"ok": True, "plugin": self.name, **data}
        except Exception as e:
            return {"ok": False, "plugin": self.name, "detail": str(e)}

    async def stop(self) -> dict:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as cli:
                r = await cli.post(f"{VCAM_URL}/stream_out/record/stop")
                return r.json() if r.status_code == 200 else {"ok": True, "plugin": self.name}
        except Exception:
            return {"ok": True, "plugin": self.name}


def _ffmpeg_available() -> bool:
    try:
        import shutil
        import imageio_ffmpeg as iff
        return bool(iff.get_ffmpeg_exe())
    except Exception:
        import shutil
        return bool(shutil.which("ffmpeg"))


class Avatar3DPlugin(StreamOutPlugin):
    name = "avatar3d"
    label = "3D 全身数字人"
    description = "Gaussian Splatting / 3D 驱动（Phase 10 占位）"

    async def status(self) -> dict:
        return {
            "plugin": self.name, "available": False, "active": False,
            "phase": 10, "planned": True,
            "roadmap": "Gaussian Splatting + 音频驱动全身",
            "detail": "当前使用 2D 口型+换脸；3D 需单独素材与 GPU 管线",
        }

    async def start(self, ctx: dict) -> dict:
        return {"ok": False, "plugin": self.name, "detail": "3D 数字人 Phase 10 尚未接入"}

    async def stop(self) -> dict:
        return {"ok": True, "plugin": self.name}


_PLUGINS: dict[str, StreamOutPlugin] = {
    p.name: p for p in (VcamPlugin(), WebrtcPlugin(), RtmpPlugin(),
                        RecorderPlugin(), Avatar3DPlugin())
}
_active: set[str] = set()
_started_at: Optional[float] = None


def list_plugins() -> list[dict]:
    return [{"name": p.name, "label": p.label, "description": p.description}
            for p in _PLUGINS.values()]


def _parse_recording_name(name: str) -> dict:
    """从文件名解析 profile 与时间戳（格式 profile_YYYYMMDD_HHMMSS.mp4）。"""
    stem = Path(name).stem
    m = re.match(r"^(.+)_(\d{8})_(\d{6})$", stem)
    if m:
        return {
            "profile": m.group(1),
            "recorded_at": f"{m.group(2)}_{m.group(3)}",
        }
    return {"profile": stem, "recorded_at": ""}


def list_recordings(*, limit: int = 40) -> list[dict]:
    """扫描本地录制目录，按修改时间倒序。"""
    root = Path(RECORD_DIR)
    if not root.is_dir():
        return []
    items: list[dict] = []
    for fp in root.glob("*.mp4"):
        try:
            st = fp.stat()
        except OSError:
            continue
        meta = _parse_recording_name(fp.name)
        items.append({
            "name": fp.name,
            "profile": meta["profile"],
            "recorded_at": meta["recorded_at"],
            "size_kb": st.st_size // 1024,
            "mtime": st.st_mtime,
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items[:max(1, min(limit, 200))]


def resolve_recording(name: str) -> Path | None:
    """安全解析录制文件名，防路径穿越。"""
    base = (name or "").strip().replace("\\", "/").split("/")[-1]
    if not base or base in (".", "..") or ".." in base:
        return None
    if not re.fullmatch(r"[\w\u4e00-\u9fff\-]+\.mp4", base):
        return None
    fp = Path(RECORD_DIR) / base
    try:
        fp = fp.resolve()
        root = Path(RECORD_DIR).resolve()
        if not str(fp).startswith(str(root)):
            return None
        if fp.is_file():
            return fp
    except OSError:
        pass
    return None


async def status_all() -> dict:
    # 并发收集（配合探测缓存）：离线冷状态从「5 插件顺序各付超时」变「一轮探测封顶」
    names = list(_PLUGINS.keys())
    stats = await asyncio.gather(*(_PLUGINS[n].status() for n in names),
                                 return_exceptions=True)
    rows = []
    for name, st in zip(names, stats):
        if isinstance(st, Exception):
            st = {"plugin": name, "available": False, "error": str(st)[:80]}
        st["running"] = name in _active
        rows.append(st)
    rec = next((r for r in rows if r.get("plugin") == "recorder"), {})
    vcam = next((r for r in rows if r.get("plugin") == "vcam"), {})
    webrtc = next((r for r in rows if r.get("plugin") == "webrtc"), {})
    rtmp = next((r for r in rows if r.get("plugin") == "rtmp"), {})
    return {
        "plugins": rows,
        "active": sorted(_active),
        "started_at": _started_at,
        "vcam_url": VCAM_URL,
        "record_dir": RECORD_DIR,
        "summary": {
            "running_count": sum(1 for r in rows if r.get("running")),
            "vcam_active": bool(vcam.get("running")),
            "webrtc_active": bool(webrtc.get("running")),
            "rtmp_active": bool(rtmp.get("active")),
            "recording": bool(rec.get("active")),
            "record_path": rec.get("record_path") or "",
            "vcam_reachable": bool(vcam.get("available")),
            "uptime_sec": int(time.time() - _started_at) if _started_at else 0,
        },
    }


async def start_plugins(names: list[str], ctx: dict | None = None) -> dict:
    ctx = ctx or {}
    results = []
    for n in names:
        p = _PLUGINS.get(n)
        if not p:
            results.append({"plugin": n, "ok": False, "detail": "未知插件"})
            continue
        r = await p.start(ctx)
        results.append(r)
        if r.get("ok"):
            _active.add(n)
    global _started_at
    if _active and _started_at is None:
        _started_at = time.time()
    return {"ok": any(r.get("ok") for r in results), "results": results,
            "active": sorted(_active)}


async def stop_plugins(names: list[str] | None = None) -> dict:
    global _started_at
    targets = names or list(_active)
    results = []
    for n in targets:
        p = _PLUGINS.get(n)
        if not p:
            continue
        results.append(await p.stop())
        _active.discard(n)
    if not _active:
        _started_at = None
    return {"ok": True, "results": results, "active": sorted(_active)}


async def set_idle_face(face_bytes: bytes) -> dict:
    """推空闲画面到广播中枢。"""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as cli:
            r = await cli.post(f"{VCAM_URL}/set_idle",
                               files={"face": ("face.jpg", face_bytes, "image/jpeg")})
            return {"ok": r.status_code == 200}
    except Exception as e:
        return {"ok": False, "detail": str(e)}
