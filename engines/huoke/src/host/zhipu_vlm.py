# -*- coding: utf-8 -*-
"""智谱 GLM-4V 视觉模型接入 — 与 ollama_vlm.classify_images 同签名.

2026-05-03 v28: 真机第 30-32 轮 ollama VLM 内存不足无法加载 6GB
qwen2.5vl:7b. 改用智谱云端 vision API (glm-4v-flash) 不占本地内存,
延迟 1-3s, 调用稳定.

Config: config/ai.yaml
  llm:
    provider: zhipu
    api_key: "..."
    base_url: "https://open.bigmodel.cn/api/paas/v4"
    vision_model: glm-4v-flash

接入方式: fb_profile_classifier.py 把
  ``from src.host import ollama_vlm``
改成
  ``from src.host import zhipu_vlm as ollama_vlm``
即可零侵入切换 (其它调用方不变, classify_images 同签名).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# 复用 ollama_vlm 的 JSON 提取器 (同款 prompt 期待 JSON 输出)
from src.host.ollama_vlm import _extract_json  # noqa: E402

logger = logging.getLogger(__name__)


# ── 配置加载 ─────────────────────────────────────────────────────────

_CFG_CACHE: Optional[Dict[str, Any]] = None


def _load_zhipu_config() -> Dict[str, Any]:
    global _CFG_CACHE
    if _CFG_CACHE is not None:
        return _CFG_CACHE
    cfg: Dict[str, Any] = {}
    try:
        import yaml
        with open("config/ai.yaml", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        llm = (data.get("llm") or {})
        cfg["api_key"] = (
            os.environ.get("ZHIPU_API_KEY")
            or llm.get("api_key")
            or ""
        ).strip()
        cfg["base_url"] = (
            llm.get("base_url")
            or "https://open.bigmodel.cn/api/paas/v4"
        ).rstrip("/")
        cfg["vision_model"] = (
            llm.get("vision_model")
            or "glm-4v-flash"
        ).strip()
        cfg["timeout_sec"] = float(llm.get("timeout_sec_l2")
                                    or llm.get("timeout_sec")
                                    or 30.0)
        cfg["max_retries"] = int(llm.get("max_retries") or 2)
    except Exception as e:
        logger.warning("[zhipu_vlm] load config/ai.yaml 失败: %s", e)
        cfg["api_key"] = os.environ.get("ZHIPU_API_KEY", "").strip()
        cfg["base_url"] = "https://open.bigmodel.cn/api/paas/v4"
        cfg["vision_model"] = "glm-4v-flash"
        cfg["timeout_sec"] = 30.0
        cfg["max_retries"] = 2
    _CFG_CACHE = cfg
    return cfg


def _image_to_data_url(path: str) -> Optional[str]:
    """把本地 png/jpg 文件 → base64 data URL (智谱 API 接受).

    返回 None 表示读取失败.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
        if not raw:
            return None
        ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
        if ext == "jpg":
            ext = "jpeg"
        b64 = base64.b64encode(raw).decode("ascii")
        return f"data:image/{ext};base64,{b64}"
    except Exception as e:
        logger.debug("[zhipu_vlm] read image %r 失败: %s", path, e)
        return None


# ── 调用 ─────────────────────────────────────────────────────────────


def _call_zhipu_chat(
    *,
    api_key: str,
    base_url: str,
    model: str,
    prompt: str,
    image_data_urls: List[str],
    timeout_sec: float,
    max_retries: int,
) -> Tuple[str, Dict[str, Any]]:
    """调智谱 chat completions API, 返回 (raw_text, meta).

    meta 含 ok / error / latency_ms / model 等.
    """
    url = f"{base_url}/chat/completions"
    # 构造 OpenAI-style content (text + image_urls)
    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    for du in image_data_urls:
        content.append({
            "type": "image_url",
            "image_url": {"url": du},
        })
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
        "max_tokens": 800,
    }
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    last_err = ""
    for attempt in range(1, max(1, max_retries) + 1):
        t0 = time.time()
        try:
            req = urllib.request.Request(
                url, data=payload, headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                raw = resp.read()
            data = json.loads(raw.decode("utf-8", errors="replace"))
            choices = data.get("choices") or []
            if not choices:
                last_err = f"empty_choices: {str(data)[:200]}"
                continue
            msg = (choices[0] or {}).get("message", {}) or {}
            text = msg.get("content") or ""
            if isinstance(text, list):
                # 某些 SDK 返回 list-of-parts
                text = "".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in text
                )
            latency_ms = int((time.time() - t0) * 1000)
            return text, {
                "ok": True,
                "error": "",
                "latency_ms": latency_ms,
                "model": model,
                "provider": "zhipu",
                "attempt": attempt,
            }
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                err_body = ""
            last_err = f"HTTPError {e.code}: {err_body}"
            logger.warning(
                "[zhipu_vlm] HTTP %d (attempt %d/%d): %s",
                e.code, attempt, max_retries, err_body,
            )
        except urllib.error.URLError as e:
            last_err = f"URLError: {e.reason}"
            logger.warning(
                "[zhipu_vlm] URLError (attempt %d/%d): %s",
                attempt, max_retries, e.reason,
            )
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            logger.warning(
                "[zhipu_vlm] generic err (attempt %d/%d): %s",
                attempt, max_retries, e,
            )
        if attempt < max_retries:
            time.sleep(min(2.0 ** attempt, 8.0))
    return "", {
        "ok": False,
        "error": last_err or "unknown",
        "latency_ms": 0,
        "model": model,
        "provider": "zhipu",
    }


# ── 同 ollama_vlm.classify_images 的对外接口 ────────────────────────


def classify_images(
    prompt: str,
    image_paths: List[str],
    *,
    scene: str = "fb_profile_l2",
    task_id: str = "",
    model: Optional[str] = None,
    device_id: str = "",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """同 ollama_vlm.classify_images 签名. 智谱 GLM-4V flash 实现.

    image_paths: 本地 png/jpg 文件路径列表. 内部转 base64 data URL.
    返回: (insights_dict, meta_dict).
      meta.ok=True 且 insights 非空表示成功.
      meta.error / meta.parse_error 含失败原因.
    """
    cfg = _load_zhipu_config()
    api_key = cfg.get("api_key", "")
    if not api_key:
        return {}, {
            "ok": False,
            "error": "missing_zhipu_api_key (env ZHIPU_API_KEY or "
                      "config/ai.yaml.llm.api_key)",
            "model": "",
            "provider": "zhipu",
        }
    use_model = (model or "").strip() or cfg.get("vision_model", "glm-4v-flash")
    base_url = cfg["base_url"]
    timeout_sec = float(cfg["timeout_sec"])
    max_retries = int(cfg["max_retries"])

    # 转图片 → base64 data URL
    image_urls: List[str] = []
    for p in image_paths or []:
        du = _image_to_data_url(p)
        if du:
            image_urls.append(du)
    if not image_urls and image_paths:
        return {}, {
            "ok": False,
            "error": "all_image_load_failed",
            "model": use_model,
            "provider": "zhipu",
        }

    raw, meta = _call_zhipu_chat(
        api_key=api_key,
        base_url=base_url,
        model=use_model,
        prompt=prompt,
        image_data_urls=image_urls,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
    )
    meta["raw_response"] = (raw or "")[:2000]
    if not meta.get("ok"):
        return {}, meta
    parsed = _extract_json(raw)
    if parsed is None:
        meta["parse_error"] = "GLM-4V 返回无法解析为 JSON"
        logger.warning(
            "[zhipu_vlm] 返回无法解析为 JSON, raw(200): %r",
            (raw or "")[:200],
        )
        return {}, meta
    return parsed, meta


# ── 与 ollama_vlm.generate 同接口的兼容包装 ────────────────────────
# (有些调用方直接用 generate, 比如 _persona_classify 不调 classify_images)


def generate(
    prompt: str,
    image_paths: Optional[List[str]] = None,
    *,
    scene: str = "fb_profile_l2",
    task_id: str = "",
    model: Optional[str] = None,
    device_id: str = "",
    max_tokens: int = 800,
    timeout_sec: Optional[float] = None,
) -> Tuple[str, Dict[str, Any]]:
    """同 ollama_vlm.generate 签名. 内部走智谱.

    主要给历史调用 generate() 的地方留兼容. 现役 fb_profile_classifier 用
    classify_images, 此 generate 是 fallback.
    """
    cfg = _load_zhipu_config()
    api_key = cfg.get("api_key", "")
    if not api_key:
        return "", {
            "ok": False, "error": "missing_zhipu_api_key",
            "model": "", "provider": "zhipu",
        }
    use_model = (model or "").strip() or cfg.get("vision_model", "glm-4v-flash")
    base_url = cfg["base_url"]
    _timeout = float(timeout_sec) if timeout_sec else float(cfg["timeout_sec"])
    image_urls: List[str] = []
    for p in (image_paths or []):
        du = _image_to_data_url(p)
        if du:
            image_urls.append(du)
    return _call_zhipu_chat(
        api_key=api_key,
        base_url=base_url,
        model=use_model,
        prompt=prompt,
        image_data_urls=image_urls,
        timeout_sec=_timeout,
        max_retries=int(cfg["max_retries"]),
    )
