"""RVC 变声客户端（.176:6242 /convert）——把一段人声换成 66 个成品音色之一。

用途：项目里「文字→中性 TTS 音频→RVC 变声成指定名人/角色音色」的第二段。RVC 是**变声**
（audio→audio），不是 text→speech；配合一段底 TTS（如本机 CosyVoice/edge）即可让人设用
丁真/古天乐/各种男女声（.176 本地 66 个 .pth，见 ``主控机调用API文档``）。

鉴权：非 /health 请求需带头 ``X-AH-Svc: <token>``。令牌**绝不写进 config.yaml/代码**——
config 填 ``svc_token_env``＝密钥名，运行时从 env / .env.local / config/secrets.local.json 读
（与 voice_enroll.load_local_secret 同源）。健康探测用 GET /inputDevices（RVC 无 /health）。

可单测纯函数（无网络）：``build_convert_payload`` / ``parse_convert_response`` /
``resolve_pth_path``。网络方法薄封装（``health_ok`` / ``convert``），失败抛异常由上层回落。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://192.168.0.176:6242"
_DEFAULT_WEIGHTS_DIR = (
    r"D:\projects\模仿音色\Retrieval-based-Voice-Conversion-WebUI"
    r"\assets\weights\weights"
)
_DEFAULT_F0METHOD = "rmvpe"


def build_convert_payload(
    audio_b64: str, pth_path: str, *,
    index_path: str = "", pitch: int = 0, index_rate: float = 0.0,
    f0method: str = _DEFAULT_F0METHOD, protect: float = 0.33,
) -> bytes:
    """RVC /convert 请求体（JSON bytes）。``pth_path`` 为 .176 本地音色 .pth 绝对路径。"""
    body: Dict[str, Any] = {
        "audio_base64": str(audio_b64 or ""),
        "pth_path": str(pth_path or ""),
        "index_path": str(index_path or ""),
        "pitch": int(pitch or 0),
        "index_rate": float(index_rate or 0.0),
        "f0method": str(f0method or _DEFAULT_F0METHOD),
        "protect": float(protect if protect is not None else 0.33),
    }
    return json.dumps(body).encode("utf-8")


def parse_convert_response(body: bytes) -> bytes:
    """解析 /convert 响应为音频字节（WAV）。``ok:false`` 抛错；缺 audio_base64 抛错。"""
    if not body:
        raise RuntimeError("rvc: empty response")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body  # 裸音频字节兜底
    if not isinstance(data, dict):
        raise RuntimeError("rvc: unexpected response shape")
    if data.get("ok") is False:
        msg = data.get("error") or data.get("message") or "convert failed"
        raise RuntimeError(f"rvc: {str(msg)[:200]}")
    b64 = data.get("audio_base64") or data.get("audio")
    if not b64:
        raise RuntimeError(f"rvc: no audio in response keys={list(data.keys())}")
    return base64.b64decode(b64)


def resolve_pth_path(voice_name: str, weights_dir: str = _DEFAULT_WEIGHTS_DIR) -> str:
    """音色名 → .176 本地 .pth 绝对路径（如 ``CN_丁真`` → ``<weights_dir>\\CN_丁真.pth``）。

    已带 .pth/绝对路径的原样返回；空名 → ""。路径由 **.176 本地** 解释（本机不校验存在性）。
    """
    v = str(voice_name or "").strip()
    if not v:
        return ""
    if v.lower().endswith(".pth") or ("/" in v) or ("\\" in v):
        return v
    return os.path.join(str(weights_dir or _DEFAULT_WEIGHTS_DIR), v + ".pth")


class RvcClient:
    """RVC 变声 HTTP 客户端（薄封装；失败抛异常，由上层回落）。"""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None) -> None:
        cfg = cfg or {}
        self.base_url: str = str(cfg.get("base_url") or _DEFAULT_BASE_URL).rstrip("/")
        self.weights_dir: str = str(cfg.get("weights_dir") or _DEFAULT_WEIGHTS_DIR)
        self.convert_path: str = str(cfg.get("convert_path") or "/convert")
        self.health_path: str = str(cfg.get("health_path") or "/inputDevices")
        self.timeout_sec: float = float(cfg.get("timeout_sec") or 60.0)
        self.health_timeout_sec: float = float(cfg.get("health_timeout_sec") or 3.0)
        self.f0method: str = str(cfg.get("f0method") or _DEFAULT_F0METHOD)
        self.svc_header: str = str(cfg.get("svc_header") or "X-AH-Svc")
        _tok = str(cfg.get("svc_token") or "")
        if not _tok and cfg.get("svc_token_env"):
            try:
                from src.ai.voice_enroll import load_local_secret
                _tok = str(load_local_secret(str(cfg.get("svc_token_env"))) or "")
            except Exception:
                _tok = ""
        self.svc_token: str = _tok

    def _headers(self, *, auth: bool) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if auth and self.svc_token:
            h[self.svc_header] = self.svc_token
        return h

    def health_ok(self) -> bool:
        """存活探测（GET /inputDevices，2xx 即活）。RVC 无 /health，``/inputDevices`` 属
        「非 /health」端点 → **需带鉴权头**（否则 401/403 被误判为不可达；实测踩过）。"""
        url = f"{self.base_url}{self.health_path}"
        try:
            req = urllib.request.Request(url, headers=self._headers(auth=True))
            with urllib.request.urlopen(req, timeout=self.health_timeout_sec) as r:
                return 200 <= int(getattr(r, "status", 200)) < 300
        except Exception:
            return False

    def convert(
        self, audio_bytes: bytes, voice_name: str, *,
        pitch: int = 0, index_rate: float = 0.0, protect: float = 0.33,
    ) -> bytes:
        """把 ``audio_bytes``(WAV) 变声成 ``voice_name`` 音色，返回变声后 WAV 字节。失败抛。"""
        pth = resolve_pth_path(voice_name, self.weights_dir)
        payload = build_convert_payload(
            base64.b64encode(audio_bytes or b"").decode(), pth,
            pitch=pitch, index_rate=index_rate, f0method=self.f0method, protect=protect)
        req = urllib.request.Request(
            f"{self.base_url}{self.convert_path}", data=payload,
            headers=self._headers(auth=True), method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
            body = resp.read()
        return parse_convert_response(body)


__all__ = [
    "build_convert_payload", "parse_convert_response", "resolve_pth_path", "RvcClient",
]
