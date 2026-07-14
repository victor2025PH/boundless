"""换脸后处理层（FaceSwap v2 @ .176:8003）——人设自拍/生活照的「同一张脸」终极方案。

背景：PuLID-Flux 靠人脸嵌入约束采样，一致性好但仍有漂移（换场景/表情时脸会飘）。
inswapper 换脸是**像素级**把 face_ref 的脸贴到生成图上——同一张脸的一致性远强于 PuLID，
且 3 秒级、不占 ComfyUI 出图显存（独立服务）。

链路（``companion.selfie.face_swap.enabled=true`` 时于 image_gate 前插入）：

    FLUX 文生图(任意好看的人像) → faceswap2 把 face_ref 的脸换上去 → vision_gate 体检 → 发出

设计：
- **纯函数可单测**：``resolve_face_swap_cfg`` / ``build_swap_payload`` / ``parse_swap_result``
  零 IO；``swap_face_file`` 只做 HTTP + 落盘。
- **软失败放行**：服务不可达/无脸/超时 → 返回原图路径（不换脸也能发，只是回落到 PuLID/
  纯生成的一致性），绝不因换脸服务抖动阻断发图。
- **鉴权**：X-AH-Svc 令牌运行时读 ``config.vision`` 同源的 secrets 文件（与 ASR/视频链一致）。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://192.168.0.176:8003"
_DEFAULT_TIMEOUT = 60.0

_METRICS: Dict[str, Any] = {
    "swapped": 0, "passthrough": 0, "failed": 0, "last_reason": "", "last_ts": 0.0,
}
_METRICS_LOCK = threading.Lock()


def _record(key: str, reason: str = "") -> None:
    with _METRICS_LOCK:
        _METRICS[key] = int(_METRICS.get(key, 0)) + 1
        if reason:
            _METRICS["last_reason"] = str(reason)
        _METRICS["last_ts"] = time.time()


def metrics_snapshot() -> Dict[str, Any]:
    with _METRICS_LOCK:
        return dict(_METRICS)


def reset_metrics() -> None:
    with _METRICS_LOCK:
        for k in ("swapped", "passthrough", "failed"):
            _METRICS[k] = 0
        _METRICS["last_reason"] = ""


def resolve_face_swap_cfg(scfg: Dict[str, Any]) -> Dict[str, Any]:
    """取 ``companion.selfie.face_swap``（默认关——换脸是增强项，需显式开）。"""
    raw = (scfg or {}).get("face_swap")
    cfg = dict(raw) if isinstance(raw, dict) else {}
    cfg.setdefault("enabled", False)
    cfg.setdefault("base_url", _DEFAULT_BASE_URL)
    cfg.setdefault("enhance", "codeformer")
    cfg.setdefault("timeout_sec", _DEFAULT_TIMEOUT)
    return cfg


def _read_token(token_file: str) -> str:
    """读 X-AH-Svc 令牌（GPU 服务面鉴权）；缺失返回空串（回环调用本就免令牌）。"""
    tf = str(token_file or "").strip()
    if not tf:
        # 与 config.voice_recognition.fallback / 视频链同一约定路径
        tf = "D:/faceX/mfys/secrets/service_token.txt"
    try:
        p = Path(tf)
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def build_swap_payload(source_b64: str, target_b64: str, *, enhance: str = "codeformer") -> Dict[str, Any]:
    """构造 faceswap2 /faceswap 请求体（source=要贴的脸 face_ref，target=生成的人像）。"""
    return {
        "source_image": str(source_b64 or ""),
        "target_image": str(target_b64 or ""),
        "enhance": str(enhance or ""),
    }


def parse_swap_result(resp: Any) -> str:
    """从 /faceswap 响应取结果图 base64；缺失/非法 → ""。"""
    if not isinstance(resp, dict):
        return ""
    return str(resp.get("result_image") or resp.get("image") or "")


def _b64_file(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def _post_json(url: str, payload: Dict[str, Any], timeout: float, token: str) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-AH-Svc"] = token
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw) if raw else {}


def swap_face_file(
    image_path: str, face_ref_path: str, cfg: Dict[str, Any],
) -> str:
    """把 ``face_ref_path`` 的脸换到 ``image_path`` 上，成功返回**新文件路径**（同目录，
    ``*.swap.png``）；任何失败/不满足 → 返回原 ``image_path``（软放行，调用方无感）。

    同步实现（供 asyncio.to_thread 调用），HTTP 走 stdlib 无重依赖。
    """
    if not bool(cfg.get("enabled", False)):
        return image_path
    if not (image_path and face_ref_path and os.path.isfile(image_path)
            and os.path.isfile(face_ref_path)):
        _record("passthrough", "missing_input")
        return image_path
    base_url = str(cfg.get("base_url") or _DEFAULT_BASE_URL).rstrip("/")
    try:
        timeout = float(cfg.get("timeout_sec", _DEFAULT_TIMEOUT) or _DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT
    token = _read_token(str(cfg.get("token_file") or ""))
    try:
        payload = build_swap_payload(
            _b64_file(face_ref_path), _b64_file(image_path),
            enhance=str(cfg.get("enhance") or "codeformer"))
    except Exception:
        _record("passthrough", "encode_fail")
        return image_path
    t0 = time.monotonic()
    try:
        resp = _post_json(f"{base_url}/faceswap", payload, timeout, token)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as ex:
        logger.info("[face_swap] 服务不可达/超时 %s: %s（回落原图）", base_url, ex)
        _record("passthrough", "unreachable")
        return image_path
    except Exception:
        logger.debug("[face_swap] 调用异常（回落原图）", exc_info=True)
        _record("passthrough", "error")
        return image_path
    out_b64 = parse_swap_result(resp)
    if not out_b64:
        _record("passthrough", "no_result")
        return image_path
    try:
        data = base64.b64decode(out_b64)
    except Exception:
        _record("passthrough", "decode_fail")
        return image_path
    if not data:
        _record("passthrough", "empty")
        return image_path
    try:
        out = str(Path(image_path).with_suffix(".swap.png"))
        with open(out, "wb") as f:
            f.write(data)
    except Exception:
        _record("passthrough", "write_fail")
        return image_path
    _record("swapped")
    logger.info("[face_swap] 换脸成功 %s → %s（%.1fs）",
                os.path.basename(face_ref_path), os.path.basename(out),
                time.monotonic() - t0)
    return out


async def maybe_swap_face(
    image_path: str, face_ref_path: str, cfg: Dict[str, Any],
) -> str:
    """``swap_face_file`` 的异步封装（线程池执行 HTTP，不阻塞事件循环）。"""
    import asyncio

    if not bool((cfg or {}).get("enabled", False)):
        return image_path
    return await asyncio.to_thread(swap_face_file, image_path, face_ref_path, cfg)


__all__ = [
    "resolve_face_swap_cfg",
    "build_swap_payload",
    "parse_swap_result",
    "swap_face_file",
    "maybe_swap_face",
    "metrics_snapshot",
    "reset_metrics",
]
