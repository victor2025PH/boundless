# -*- coding: utf-8 -*-
"""AvatarHub 唱歌工作室薄客户端（2026-08-22 P0，仅离线/CLI 消费）。

封装 hub 网关（幻声机 ``:9000``）的歌声端点：

- ``POST /api/song/cover``（multipart：song 源曲 + profile 人设档 + pitch=auto
  自动调门 + ``dry_vocal=true`` 只要清唱干声；reference 缺省＝该档注册克隆音）
- ``GET  /api/song/task/{tid}``（轮询；done 后带 ``audio_url`` 与 hub 侧声纹
  ``similarity``）
- ``GET  /api/song/health`` / ``POST /api/clone_score`` / ``GET /profiles/{name}/
  reference_audio``（备货指纹生命周期用：hub 侧参考音换了 → 唱段重渲）

铁律（与 avatar_voice 同门）：**本进程只 HTTP，绝不加载模型**；运行时聊天链
不 import 本模块（唱段全部预渲染，见 song_stock 模块 docstring）。
产物验证内建（媒体产物验证纪律）：Content-Type → magic bytes → 最小尺寸，
任一步不过抛 ``SingingArtifactError``——绝不把 JSON 信封/空壳当音频落盘。

门禁：tests/test_singing_client.py（纯函数/契约，零网络）。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import struct
import time
import urllib.request
import uuid
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("ai_chat_assistant.singing_client")

MIN_AUDIO_BYTES = 40_000


class SingingArtifactError(RuntimeError):
    """产物验证失败（信封/空壳/未知格式）——调用方按渲染失败处理，绝不落盘。"""


def sniff_audio(data: bytes) -> str:
    """magic bytes 嗅探：wav/ogg/mp3/flac；未知返回 ''。纯函数。"""
    if not data:
        return ""
    if data.startswith(b"RIFF"):
        return "wav"
    if data.startswith(b"OggS"):
        return "ogg"
    if data.startswith(b"ID3") or (len(data) > 2 and data[0] == 0xFF
                                   and (data[1] & 0xE0) == 0xE0):
        return "mp3"
    if data.startswith(b"fLaC"):
        return "flac"
    return ""


def wav_info(data: bytes) -> Tuple[int, float]:
    """RIFF 头解析 (sample_rate, duration_sec)；非 wav/解析失败 → (0, 0.0)。纯函数。"""
    try:
        if not data.startswith(b"RIFF"):
            return 0, 0.0
        pos, sr, byte_rate, data_size = 12, 0, 0, 0
        while pos + 8 <= len(data):
            cid = data[pos:pos + 4]
            csz = struct.unpack("<I", data[pos + 4:pos + 8])[0]
            if cid == b"fmt " and pos + 20 <= len(data):
                sr = struct.unpack("<I", data[pos + 12:pos + 16])[0]
                byte_rate = struct.unpack("<I", data[pos + 16:pos + 20])[0]
            elif cid == b"data":
                data_size = csz
            pos += 8 + csz + (csz % 2)
        dur = (data_size / byte_rate) if byte_rate else 0.0
        return int(sr), float(dur)
    except Exception:
        return 0, 0.0


def validate_audio_bytes(data: bytes, content_type: str = "") -> str:
    """三道闸（纪律内建）：非 JSON 信封 → magic 已知 → 尺寸达标。返回格式名。"""
    if "application/json" in str(content_type or ""):
        raise SingingArtifactError("响应是 JSON 信封而非音频流（拒绝直存）")
    kind = sniff_audio(data)
    if not kind:
        raise SingingArtifactError(f"magic bytes 未知（head={data[:12]!r}）")
    if len(data) < MIN_AUDIO_BYTES:
        raise SingingArtifactError(f"产物过小（{len(data)}B < {MIN_AUDIO_BYTES}B）")
    return kind


def build_cover_multipart(fields: Dict[str, str], file_field: str,
                          filename: str, file_bytes: bytes,
                          boundary: str = "") -> Tuple[bytes, str]:
    """构造 multipart/form-data 请求体（纯函数，boundary 可注入便于测试）。"""
    b = boundary or ("----chengjiesong" + uuid.uuid4().hex)
    buf = io.BytesIO()
    for k, v in fields.items():
        buf.write((f"--{b}\r\nContent-Disposition: form-data; "
                   f"name=\"{k}\"\r\n\r\n{v}\r\n").encode("utf-8"))
    buf.write((f"--{b}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
               f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream"
               f"\r\n\r\n").encode("utf-8"))
    buf.write(file_bytes)
    buf.write(f"\r\n--{b}--\r\n".encode("utf-8"))
    return buf.getvalue(), f"multipart/form-data; boundary={b}"


class SingingClient:
    """hub 歌声端点客户端（同步、串行——离线批渲不需要并发，串行即礼貌）。"""

    def __init__(self, base_url: str, *, timeout_sec: float = 120.0):
        self.base = str(base_url or "").rstrip("/")
        self.timeout = float(timeout_sec)

    # ── 基础 HTTP ────────────────────────────────────────────────────
    def _json(self, method: str, path: str, payload: Optional[dict] = None,
              timeout: Optional[float] = None) -> Dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _bytes(self, path: str, timeout: Optional[float] = None) -> Tuple[bytes, str]:
        with urllib.request.urlopen(self.base + path,
                                    timeout=timeout or self.timeout) as r:
            return r.read(), str(r.headers.get("Content-Type") or "")

    # ── 歌声子系统 ───────────────────────────────────────────────────
    def song_health(self) -> Dict[str, Any]:
        return self._json("GET", "/api/song/health", timeout=15)

    def submit_cover(self, song_bytes: bytes, profile: str, *,
                     pitch: str = "auto", quality: str = "standard",
                     dry_vocal: bool = True, filename: str = "src.wav") -> str:
        """提交翻唱任务，返回 tid。dry_vocal=True＝只要清唱干声（本项目主口径）。"""
        body, ctype = build_cover_multipart(
            {"profile": profile, "pitch": pitch, "quality": quality,
             "dry_vocal": "true" if dry_vocal else "false"},
            "song", filename, song_bytes)
        req = urllib.request.Request(
            self.base + "/api/song/cover", data=body, method="POST",
            headers={"Content-Type": ctype})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            sub = json.loads(r.read().decode("utf-8"))
        tid = str(sub.get("tid") or sub.get("task_id") or "")
        if not tid:
            raise RuntimeError(f"cover 提交未返回 tid: {str(sub)[:200]}")
        return tid

    def poll_task(self, tid: str, *, budget_sec: float = 900.0,
                  interval_sec: float = 5.0) -> Dict[str, Any]:
        """轮询任务至终态；超预算抛 TimeoutError（调用方决定重试策略）。"""
        t0 = time.monotonic()
        task: Dict[str, Any] = {}
        while time.monotonic() - t0 < budget_sec:
            time.sleep(interval_sec)
            task = self._json("GET", f"/api/song/task/{tid}", timeout=30)
            st = str(task.get("status") or "")
            if st in ("done", "error", "failed", "cancelled"):
                return task
        raise TimeoutError(f"song task {tid} 超时（>{budget_sec:.0f}s）: "
                           f"{str(task)[:200]}")

    def fetch_result_audio(self, task: Dict[str, Any]) -> Tuple[bytes, str]:
        """按任务取产物并过三道闸，返回 (bytes, 格式名)。"""
        url = str(task.get("audio_url") or "")
        if not url:
            hid = task.get("history_id")
            if hid:
                url = f"/api/history/{hid}/audio.wav"
        if not url:
            raise SingingArtifactError(f"任务无产物地址: {str(task)[:200]}")
        data, ctype = self._bytes(url)
        kind = validate_audio_bytes(data, ctype)
        return data, kind

    def clone_score(self, profile: str, audio_bytes: bytes) -> Optional[float]:
        """hub 声纹分（campplus 系）；失败返回 None（观测项，不阻断渲染）。"""
        try:
            r = self._json("POST", "/api/clone_score", {
                "profile": profile,
                "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
            })
            for key in ("cosine", "score", "similarity"):
                v = r.get(key)
                if isinstance(v, (int, float)):
                    return float(v)
            sim = r.get("similarity")
            if isinstance(sim, dict) and isinstance(sim.get("cosine"), (int, float)):
                return float(sim["cosine"])
            return None
        except Exception:
            logger.debug("[singing] clone_score 失败（记 None 不阻断）", exc_info=True)
            return None

    def profile_ref_sha1(self, profile: str) -> str:
        """hub 侧该档参考音内容指纹（换声检测）；失败返回 ''（视为未知不判陈旧）。"""
        try:
            data, _ = self._bytes(
                "/profiles/" + urllib.request.quote(profile) + "/reference_audio")
            if not data:
                return ""
            return hashlib.sha1(data).hexdigest()
        except Exception:
            return ""


__all__ = [
    "SingingClient", "SingingArtifactError",
    "sniff_audio", "wav_info", "validate_audio_bytes", "build_cover_multipart",
    "MIN_AUDIO_BYTES",
]
