# -*- coding: utf-8 -*-
"""本地 Ollama VLM 适配器（qwen2.5vl:7b / 32b 等）。

职责：
    * 对接本地 Ollama /api/generate，发送「多图 + prompt」，拿结构化 JSON 回复。
    * 图片预处理：长边 <= max_image_side_px，JPEG 压缩，减少显存/推理耗时。
    * 容错：JSON markdown 包裹、额外前后文、极端失败回退 {}。
    * 审计：每次调用写入 ai_cost_events（本地 provider=ollama, cost_usd=0）。

调用示例::

    from src.host.ollama_vlm import classify_images
    result, meta = classify_images(
        prompt="日本語で...", image_paths=["a.jpg", "b.jpg"],
        scene="fb_profile_l2", task_id="task-1",
    )
    # result 是 dict（已从 JSON 解析），meta 含 latency_ms, ok, error 等
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")   # 贪心抓取最外层 {...}

# ════════════════════════════════════════════════════════════════════
# 单卡 GPU 串行保护（Sprint C-1 P1）
# ────────────────────────────────────────────────────────────────────
# 背景: 多设备并发 profile_hunt 时, 两个 generate 同时打到 Ollama,
#       Ollama 侧虽有排队但无反馈, 客户端看到总耗时被拉长且接近 timeout。
# 做法: 进程内一把锁保证同一时刻只有一个 VLM 请求在飞,
#       并且把"等锁时间 queue_wait_ms"作为指标记录到 ai_cost_events,
#       使 Dashboard 能直观看到并发瓶颈。
# 不做: 跨进程互斥 / 分布式队列 —— 本项目目前单进程 ThreadPoolExecutor 足够。
# ════════════════════════════════════════════════════════════════════
_VLM_LOCK = threading.Lock()
_VLM_CONCURRENCY_STATS = {"peak_wait_ms": 0, "total_calls": 0, "total_wait_ms": 0}
_VLM_STATS_LOCK = threading.Lock()

# ════════════════════════════════════════════════════════════════════
# Sprint E-0.1: VLM warmup（预热）
# ────────────────────────────────────────────────────────────────────
# 背景: 真机 smoke 实测，冷启动时 qwen2.5vl:7b 第一次 classify 要 56s
#       （含 1 次 timeout 重试），后续调用约 3~5s/张。首张 profile_hunt
#       因此从用户视角看是"卡死"。
# 做法:
#   - warmup() 同步发一个极小 prompt (1~3 tokens)，让 Ollama 把模型装进 GPU
#   - warmup_async() fire-and-forget 后台线程版，不阻塞调用方
#   - 幂等 TTL: 10 分钟内已经 warmup 过且 generate() 有成功记录 → 跳过
#   - 若 Ollama 离线/模型未拉取 → 快速返回失败，不卡 warmup
# 不做: 不在 import 时自动触发 (会污染单元测试和纯读服务)
# ════════════════════════════════════════════════════════════════════
_WARMUP_STATE = {"last_ok_ts": 0.0, "last_error": "", "in_progress": False}
_WARMUP_LOCK = threading.Lock()
_WARMUP_TTL_SEC = 600  # 10 分钟


def get_concurrency_stats() -> Dict[str, int]:
    """返回 VLM 排队指标（便于 Dashboard/smoke 观测）。"""
    with _VLM_STATS_LOCK:
        return dict(_VLM_CONCURRENCY_STATS)


def get_warmup_state() -> Dict[str, Any]:
    """返回 warmup 状态（便于 API / smoke 观测）。"""
    with _WARMUP_LOCK:
        st = dict(_WARMUP_STATE)
    now = time.time()
    st["age_sec"] = int(now - st["last_ok_ts"]) if st["last_ok_ts"] else None
    st["fresh"] = bool(st["last_ok_ts"] and (now - st["last_ok_ts"]) < _WARMUP_TTL_SEC)
    return st


def warmup(force: bool = False, timeout: float = 90.0) -> Dict[str, Any]:
    """同步预热 VLM：发一个最小 prompt 让模型装进 GPU。

    幂等：10 min 内已成功 warmup 过则直接返回，除非 force=True。

    返回::
        {"ok": bool, "skipped": bool, "latency_ms": int, "model": str, "error": str}
    """
    with _WARMUP_LOCK:
        now = time.time()
        if not force and _WARMUP_STATE["last_ok_ts"] and (now - _WARMUP_STATE["last_ok_ts"]) < _WARMUP_TTL_SEC:
            return {"ok": True, "skipped": True,
                    "age_sec": int(now - _WARMUP_STATE["last_ok_ts"]),
                    "model": _load_vlm_config().get("model", ""),
                    "latency_ms": 0, "error": ""}
        if _WARMUP_STATE["in_progress"]:
            return {"ok": False, "skipped": True, "reason": "already_in_progress",
                    "latency_ms": 0, "error": ""}
        _WARMUP_STATE["in_progress"] = True

    t0 = time.time()
    model = _load_vlm_config().get("model", "")
    # 先 health check，避免 Ollama 没起时 warmup 卡住
    hc = check_health(timeout=3.0)
    if not hc.get("online"):
        with _WARMUP_LOCK:
            _WARMUP_STATE["in_progress"] = False
            _WARMUP_STATE["last_error"] = "ollama_offline"
        return {"ok": False, "skipped": False, "latency_ms": int((time.time() - t0) * 1000),
                "model": model, "error": "ollama_offline"}
    if not hc.get("model_available"):
        with _WARMUP_LOCK:
            _WARMUP_STATE["in_progress"] = False
            _WARMUP_STATE["last_error"] = f"model_not_pulled:{model}"
        return {"ok": False, "skipped": False, "latency_ms": int((time.time() - t0) * 1000),
                "model": model, "error": f"model_not_pulled:{model}"}

    # 真 warmup: 1 个 token 的极小 prompt，触发模型装显存
    # 不用 images，避免 warmup 本身成为 56s 的那一次
    try:
        _, meta = generate(
            prompt="Reply with exactly: OK",
            scene="vlm_warmup",
            task_id="warmup",
            device_id="_warmup_",
            max_tokens=4,
            temperature=0.0,
        )
        ok = bool(meta.get("ok"))
        err = meta.get("error", "") if not ok else ""
    except Exception as e:
        ok, err = False, f"EXC:{type(e).__name__}:{e}"

    latency_ms = int((time.time() - t0) * 1000)
    with _WARMUP_LOCK:
        _WARMUP_STATE["in_progress"] = False
        if ok:
            _WARMUP_STATE["last_ok_ts"] = time.time()
            _WARMUP_STATE["last_error"] = ""
        else:
            _WARMUP_STATE["last_error"] = err
    logger.info("[vlm_warmup] ok=%s model=%s latency=%dms err=%s",
                ok, model, latency_ms, err)
    return {"ok": ok, "skipped": False, "latency_ms": latency_ms,
            "model": model, "error": err}


def warmup_async(force: bool = False) -> bool:
    """Fire-and-forget 后台预热。立刻返回 True（已排队）/ False（已经在跑）。"""
    with _WARMUP_LOCK:
        if _WARMUP_STATE["in_progress"]:
            return False
        now = time.time()
        if not force and _WARMUP_STATE["last_ok_ts"] and (now - _WARMUP_STATE["last_ok_ts"]) < _WARMUP_TTL_SEC:
            return False

    def _run():
        try:
            warmup(force=force)
        except Exception as e:
            logger.warning("[vlm_warmup_async] 异常: %s", e)

    t = threading.Thread(target=_run, name="vlm-warmup", daemon=True)
    t.start()
    return True


def _load_vlm_config() -> Dict[str, Any]:
    """延迟导入避免循环依赖。

    环境变量（压测/切换模型，不改 YAML）::
        FB_VLM_MODEL      — 覆盖 ``model``（默认走 OCR/通用）
        FB_VLM_MODEL_L2   — 覆盖 ``model_l2``（L2 判读专用）
        FB_VLM_MODEL_OCR  — 覆盖 ``model_ocr``（profile OCR 专用）
    """
    try:
        from src.host.fb_target_personas import get_vlm_config
        cfg: Dict[str, Any] = dict(get_vlm_config())
    except Exception as e:
        logger.warning("读取 fb_target_personas.vlm 失败，用内置默认: %s", e)
        cfg = {
            "provider": "ollama",
            "model": "qwen2.5vl:7b",
            "endpoint": "http://127.0.0.1:11434",
            "timeout_sec": 30,
            "max_retries": 2,
            "temperature": 0.2,
            "max_images_per_call": 3,
            "max_image_side_px": 1280,
            "jpeg_quality": 85,
            "num_ctx": 4096,
            "keep_alive": "30m",
        }
    ow = (os.environ.get("FB_VLM_MODEL") or "").strip()
    if ow:
        cfg["model"] = ow
        logger.info("VLM: env FB_VLM_MODEL overrides model -> %s", ow)
    ow_l2 = (os.environ.get("FB_VLM_MODEL_L2") or "").strip()
    if ow_l2:
        cfg["model_l2"] = ow_l2
        logger.info("VLM: env FB_VLM_MODEL_L2 overrides model_l2 -> %s", ow_l2)
    ow_ocr = (os.environ.get("FB_VLM_MODEL_OCR") or "").strip()
    if ow_ocr:
        cfg["model_ocr"] = ow_ocr
        logger.info("VLM: env FB_VLM_MODEL_OCR overrides model_ocr -> %s", ow_ocr)
    return cfg


def _log_ai_cost(
    *,
    provider: str,
    model: str,
    task_id: str,
    scene: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    image_count: int = 0,
    latency_ms: int = 0,
    cost_usd: float = 0.0,
    ok: bool = True,
    error: str = "",
    queue_wait_ms: int = 0,
    device_id: str = "",
) -> None:
    try:
        from src.host.database import get_conn
    except Exception:
        return
    try:
        with get_conn() as conn:
            # 优先写入 queue_wait_ms + device_id（如果迁移列已存在）。
            # 若迁移尚未跑（极老数据库），降级为不含新字段的 INSERT。
            try:
                conn.execute(
                    """INSERT INTO ai_cost_events (
                        provider, model, task_id, scene, input_tokens, output_tokens,
                        image_count, latency_ms, cost_usd, ok, error,
                        queue_wait_ms, device_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (provider, model, task_id or "", scene or "",
                     int(input_tokens), int(output_tokens), int(image_count),
                     int(latency_ms), float(cost_usd), 1 if ok else 0, (error or "")[:500],
                     int(queue_wait_ms), (device_id or "")),
                )
            except Exception:
                conn.execute(
                    """INSERT INTO ai_cost_events (
                        provider, model, task_id, scene, input_tokens, output_tokens,
                        image_count, latency_ms, cost_usd, ok, error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (provider, model, task_id or "", scene or "",
                     int(input_tokens), int(output_tokens), int(image_count),
                     int(latency_ms), float(cost_usd), 1 if ok else 0, (error or "")[:500]),
                )
    except Exception as e:
        logger.debug("ai_cost_events 写入失败（忽略）: %s", e)


def _compress_image_to_b64(path: str, max_side: int, jpeg_quality: int) -> Optional[str]:
    """读取图片，长边压到 max_side，返回 base64（JPEG）。
    PIL 缺失时直接 base64 原文件，让 Ollama 自己处理。"""
    p = Path(path)
    if not p.exists():
        logger.warning("图片不存在: %s", path)
        return None
    try:
        from PIL import Image  # 懒加载
    except ImportError:
        logger.warning("PIL 未安装，跳过压缩直传原图 %s", path)
        return base64.b64encode(p.read_bytes()).decode()

    try:
        with Image.open(p) as img:
            img = img.convert("RGB")
            w, h = img.size
            long_side = max(w, h)
            if long_side > max_side:
                scale = max_side / long_side
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
            return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        logger.warning("图片压缩失败 %s: %s；回退到原文件", path, e)
        try:
            return base64.b64encode(p.read_bytes()).decode()
        except Exception:
            return None


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从 VLM 原始回复中抠出 JSON。兼容:
    - 纯 JSON: `{...}`
    - ```json\n{...}\n``` 代码块
    - 前后带说明文字: `判定結果: {...} 以上`
    """
    if not text:
        return None
    text = text.strip()

    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except Exception:
            pass

    try:
        return json.loads(text)
    except Exception:
        pass

    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            fix = m.group(0).replace("'", '"').replace("，", ",").replace("：", ":")
            try:
                return json.loads(fix)
            except Exception:
                return None
    return None


def check_health(timeout: float = 3.0) -> Dict[str, Any]:
    """快速探测 Ollama 是否在线 & 目标模型是否已存在。"""
    cfg = _load_vlm_config()
    url = f"{cfg['endpoint'].rstrip('/')}/api/tags"
    model = cfg.get("model", "")
    out = {"online": False, "model": model, "model_available": False, "models": [], "endpoint": cfg["endpoint"]}
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        names = [m.get("name", "") for m in (data.get("models") or [])]
        out["online"] = True
        out["models"] = names
        out["model_available"] = any(n == model or n.startswith(model.split(":")[0] + ":") for n in names)
    except Exception as e:
        out["error"] = str(e)
    return out


def generate(
    prompt: str,
    image_paths: Optional[List[str]] = None,
    *,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: int = 512,
    scene: str = "fb_vlm",
    task_id: str = "",
    device_id: str = "",
    timeout_sec: Optional[float] = None,
) -> Tuple[str, Dict[str, Any]]:
    """底层生成函数。返回 (raw_text, meta)。不做 JSON 解析。

    Sprint C-1: 用全局 _VLM_LOCK 保证单卡 GPU 的串行，meta 里报告 queue_wait_ms。
    """
    cfg = _load_vlm_config()
    use_model = model or cfg["model"]
    endpoint = cfg["endpoint"].rstrip("/")
    timeout = int(timeout_sec if timeout_sec is not None else cfg.get("timeout_sec", 30))
    max_retries = int(cfg.get("max_retries", 2))
    temp = float(temperature if temperature is not None else cfg.get("temperature", 0.2))
    max_imgs = int(cfg.get("max_images_per_call", 3))
    max_side = int(cfg.get("max_image_side_px", 1280))
    jq = int(cfg.get("jpeg_quality", 85))

    imgs_b64: List[str] = []
    for p in (image_paths or [])[:max_imgs]:
        b = _compress_image_to_b64(p, max_side, jq)
        if b:
            imgs_b64.append(b)

    # 关键：num_ctx 必须显式限定！Ollama 对 qwen2.5vl 默认会拉到 128K，
    # 在 12GB 显卡上会 OOM（KV cache 要 6.8GB + compute graph 34GB）。
    # 4096 对"头像+主页几张图"够用，GPU 显存需求从 47GB 降到 ~7GB。
    num_ctx = int(cfg.get("num_ctx", 4096))
    keep_alive = cfg.get("keep_alive", "30m")
    payload: Dict[str, Any] = {
        "model": use_model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": keep_alive,
        "options": {
            "temperature": temp,
            "num_predict": max_tokens,
            "num_ctx": num_ctx,
        },
    }
    if imgs_b64:
        payload["images"] = imgs_b64

    raw_text = ""
    meta: Dict[str, Any] = {
        "ok": False,
        "model": use_model,
        "image_count": len(imgs_b64),
        "latency_ms": 0,      # 纯 VLM 推理耗时（不含排队）
        "queue_wait_ms": 0,   # 等待 _VLM_LOCK 的时间
        "total_ms": 0,        # queue_wait + latency
        "attempts": 0,
        "error": "",
    }

    # 测量排队时间：lock acquire 前后时间差
    wait_t0 = time.time()
    with _VLM_LOCK:
        queue_wait_ms = int((time.time() - wait_t0) * 1000)
        meta["queue_wait_ms"] = queue_wait_ms

        t0 = time.time()
        last_err = ""
        for attempt in range(max_retries + 1):
            meta["attempts"] = attempt + 1
            try:
                req = urllib.request.Request(
                    f"{endpoint}/api/generate",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                raw_text = data.get("response", "") or ""
                meta["ok"] = True
                meta["input_tokens"] = int(data.get("prompt_eval_count", 0) or 0)
                meta["output_tokens"] = int(data.get("eval_count", 0) or 0)
                last_err = ""
                break
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="ignore")[:300]
                except Exception:
                    pass
                last_err = f"HTTP {e.code}: {body}"
                logger.warning("Ollama generate HTTP %s (attempt %d/%d): %s",
                               e.code, attempt + 1, max_retries + 1, body)
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                logger.warning("Ollama generate error (attempt %d/%d): %s",
                               attempt + 1, max_retries + 1, last_err)

        meta["latency_ms"] = int((time.time() - t0) * 1000)

    meta["total_ms"] = meta["queue_wait_ms"] + meta["latency_ms"]
    if not meta["ok"]:
        meta["error"] = last_err

    # 更新进程级 VLM 并发统计
    with _VLM_STATS_LOCK:
        _VLM_CONCURRENCY_STATS["total_calls"] += 1
        _VLM_CONCURRENCY_STATS["total_wait_ms"] += meta["queue_wait_ms"]
        if meta["queue_wait_ms"] > _VLM_CONCURRENCY_STATS["peak_wait_ms"]:
            _VLM_CONCURRENCY_STATS["peak_wait_ms"] = meta["queue_wait_ms"]

    _log_ai_cost(
        provider="ollama",
        model=use_model,
        task_id=task_id,
        scene=scene,
        input_tokens=int(meta.get("input_tokens", 0)),
        output_tokens=int(meta.get("output_tokens", 0)),
        image_count=meta["image_count"],
        latency_ms=meta["latency_ms"],
        cost_usd=0.0,
        ok=meta["ok"],
        error=meta.get("error", ""),
        queue_wait_ms=meta["queue_wait_ms"],
        device_id=device_id,
    )
    return raw_text, meta


def classify_images(
    prompt: str,
    image_paths: List[str],
    *,
    scene: str = "fb_profile_l2",
    task_id: str = "",
    model: Optional[str] = None,
    device_id: str = "",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """高层：发 prompt+图片 → 期望模型返回 JSON → 解析成 dict。
    解析失败返回 {} 并在 meta.error 标记。

    模型选择（``model`` 为 None 时）::
        * scene=fb_profile_l2 → cfg ``model_l2`` 或回退 ``model``（高端判读）
        * scene=fb_profile_ocr → cfg ``model_ocr`` 或回退 ``model``（OCR 可保持小模型）
    """
    cfg = _load_vlm_config()
    use_model = model
    if not use_model:
        if scene == "fb_profile_l2":
            use_model = (cfg.get("model_l2") or cfg.get("model") or "").strip() or None
        elif scene == "fb_profile_ocr":
            use_model = (cfg.get("model_ocr") or cfg.get("model") or "").strip() or None
    timeout_override: Optional[float] = None
    if scene == "fb_profile_l2" and cfg.get("timeout_sec_l2") is not None:
        try:
            timeout_override = float(cfg["timeout_sec_l2"])
        except (TypeError, ValueError):
            timeout_override = None
    elif scene == "fb_profile_ocr" and cfg.get("timeout_sec_ocr") is not None:
        try:
            timeout_override = float(cfg["timeout_sec_ocr"])
        except (TypeError, ValueError):
            timeout_override = None

    raw, meta = generate(
        prompt=prompt,
        image_paths=image_paths,
        scene=scene,
        task_id=task_id,
        model=use_model,
        max_tokens=800,
        device_id=device_id,
        timeout_sec=timeout_override,
    )
    meta["raw_response"] = raw[:2000]
    if not meta["ok"]:
        return {}, meta
    parsed = _extract_json(raw)
    if parsed is None:
        meta["parse_error"] = "VLM 返回无法解析为 JSON"
        logger.warning("VLM 返回无法解析为 JSON，raw(100): %r", raw[:200])
        return {}, meta
    return parsed, meta
