"""LAN GPU 显存水位（Ollama ``/api/ps`` 聚合）——「备机过载」提前可见。

背景：140(4070,12G) 兼任嵌入双活备点 + 视觉备点，176(5090,32G) 主力跑
对话兜底/MT/VL/ASR。哪台被同时压上多个模型会挤爆（Ollama 静默换入换出 →
延迟毛刺），此前只能 SSH 上去 ``ollama ps`` 肉眼看。本模块把各主机
``/api/ps``（Ollama 原生接口，报每模型 ``size_vram`` 字节）聚成水位。

口径说明：统计的是 **Ollama 管理的模型显存**，不是 nvidia-smi 全卡占用
（ASR/SER 等独立进程不计入）——对「模型会不会挤爆 Ollama 预算」这个问题
是准确口径；卡上另有他用时 total_gb 可在配置里按可分配额度填小。

纯函数（summarize_host / summarize_fleet）+ 探针（probe_hosts，30s TTL）分离。
配置 ``ops.gpu_watermark``（新子系统默认 enabled:false）::

    ops:
      gpu_watermark:
        enabled: true
        hosts:
          - {name: "176-5090", base_url: "http://192.168.0.176:11434", vram_gb: 32}
          - {name: "140-4070", base_url: "http://192.168.0.140:11434", vram_gb: 12}
          - {name: "173-5090", base_url: "http://192.168.0.173:8001", vram_gb: 32,
             kind: vllm, resident_gb: 28}

``kind: vllm``（2026-09-17）：173 出话口 08-28 已从 Ollama :11434 迁到 vLLM :8001，
条目若仍指 :11434 会把「主链正在服务的 5090」画成**空卡 0 GB / 无模型**（Ollama 守护
进程还活着但不管这张卡）。vLLM 没有 ``/api/ps``：改探 ``/v1/models``（驻留模型目录）
+ ``/metrics`` 的 ``vllm:kv_cache_usage_perc``（KV cache 水位）。vLLM 启动即按
``gpu_memory_utilization`` 一次性预留显存、不按模型逐个算，显存占用量本模块**不猜**：
配置给了 ``resident_gb``（运维按 vLLM 启动参数填）才显示占用，否则 used_gb=None、
卡面只显「常驻 · 模型名 · KV cache x%」。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 水位分级阈值（占 vram_gb 百分比）
WARN_PCT = 75.0
HIGH_PCT = 90.0


def config_state(config: Dict[str, Any]) -> str:
    """配置三态（纯函数）：``off`` / ``misconfigured`` / ``ready``。

    动机（2026-07-29 实测事故）：overlay 里 ``enabled: true`` 但漏配 ``hosts`` →
    ``parse_hosts`` 返回空 → ``probe_hosts`` 返回 None → API 报 ``enabled:false``
    → 前端整卡隐藏，**全程零提示**。运营以为开了、文档也写着开了，实际静默无效
    （同类病：flag 开启缺少「是否真生效」的反馈）。区分出 ``misconfigured`` 后，
    「开了但配置不全」不再与「没开」混为一谈。
    """
    ops = (config.get("ops") or {}) if isinstance(config, dict) else {}
    gw = ops.get("gpu_watermark") or {}
    if not gw.get("enabled", False):
        return "off"
    return "ready" if parse_hosts(config) else "misconfigured"


def parse_hosts(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 config 取启用的主机列表（enabled + 至少一台合法主机才非空）。"""
    ops = (config.get("ops") or {}) if isinstance(config, dict) else {}
    gw = ops.get("gpu_watermark") or {}
    if not gw.get("enabled", False):
        return []
    out: List[Dict[str, Any]] = []
    for h in gw.get("hosts") or []:
        if not isinstance(h, dict):
            continue
        base = str(h.get("base_url") or "").strip().rstrip("/")
        if not base or "://" not in base:
            continue
        kind = str(h.get("kind") or "ollama").strip().lower()
        if kind not in ("ollama", "vllm"):
            kind = "ollama"
        row: Dict[str, Any] = {
            "name": str(h.get("name") or base),
            "base_url": base,
            "vram_gb": float(h.get("vram_gb") or 0),
            "kind": kind,
        }
        if kind == "vllm":
            try:
                rg = float(h.get("resident_gb") or 0)
            except (TypeError, ValueError):
                rg = 0.0
            row["resident_gb"] = rg if rg > 0 else None
        out.append(row)
    return out


def summarize_host(name: str, vram_gb: float,
                   ps_payload: Optional[Dict[str, Any]],
                   *, error: str = "") -> Dict[str, Any]:
    """把一台主机的 /api/ps 响应聚成水位行（纯函数）。

    ps_payload=None 表示探测失败 → reachable:false（error 附原因）。
    """
    if ps_payload is None:
        return {"name": name, "reachable": False, "error": error[:120],
                "total_gb": vram_gb, "used_gb": None, "used_pct": None,
                "level": "unknown", "models": []}
    models = []
    used_bytes = 0
    for m in (ps_payload.get("models") or []):
        if not isinstance(m, dict):
            continue
        sv = int(m.get("size_vram") or 0)
        used_bytes += sv
        models.append({
            "name": str(m.get("name") or m.get("model") or "?"),
            "size_gb": round(sv / 1e9, 1),
            "until": str(m.get("expires_at") or ""),
        })
    used_gb = used_bytes / 1e9
    pct = (used_gb / vram_gb * 100.0) if vram_gb > 0 else 0.0
    level = "high" if pct >= HIGH_PCT else ("warn" if pct >= WARN_PCT else "ok")
    # 按占用降序，最大头一眼可见
    models.sort(key=lambda x: -x["size_gb"])
    return {"name": name, "reachable": True, "error": "",
            "total_gb": vram_gb, "used_gb": round(used_gb, 1),
            "used_pct": round(pct, 1), "level": level, "models": models}


_KV_CACHE_RE = re.compile(r"^vllm:kv_cache_usage_perc(?:\{[^}]*\})?\s+([0-9.eE+-]+)", re.M)


def parse_vllm_kv_cache_pct(metrics_text: Optional[str]) -> Optional[float]:
    """从 vLLM ``/metrics`` 文本取 KV cache 占用百分比（0–100）；缺失/不可解析 → None。

    vLLM 该指标是 0–1 小数（``vllm:kv_cache_usage_perc{...} 0.03``），这里换成百分比
    与 ``used_pct`` 同单位；多引擎行取最大（最挤的那个才是水位）。
    """
    if not metrics_text:
        return None
    vals: List[float] = []
    for m in _KV_CACHE_RE.finditer(metrics_text):
        try:
            vals.append(float(m.group(1)))
        except ValueError:
            continue
    if not vals:
        return None
    v = max(vals)
    # 兼容将来直接给百分比的版本：>1 视作已是百分比
    return round(v * 100.0 if v <= 1.0 else v, 1)


def summarize_vllm_host(name: str, vram_gb: float,
                        models_payload: Optional[Dict[str, Any]],
                        *, metrics_text: Optional[str] = None,
                        resident_gb: Optional[float] = None,
                        error: str = "") -> Dict[str, Any]:
    """vLLM 主机 → 水位行（纯函数，形状与 ``summarize_host`` 同款，前端同一渲染）。

    - ``models_payload=None`` → 不可达（与 Ollama 行同语义）。
    - 可达但 ``/v1/models`` 目录为空 → ``level=warn``（进程活着模型没挂，出话会 404）。
    - 显存占用：只有配置 ``resident_gb`` 才填 used_gb/used_pct（vLLM 预留式占用不猜数）；
      分级优先看 KV cache 水位（真正会挤爆的是它），其次看 resident/vram。
    - ``note`` 供卡面显示（used_gb=None 时前端用它代替「x / y GB」）。
    """
    if models_payload is None:
        row = {"name": name, "reachable": False, "error": error[:120],
               "total_gb": vram_gb, "used_gb": None, "used_pct": None,
               "level": "unknown", "models": [], "kind": "vllm"}
        return row
    ids: List[str] = []
    for m in (models_payload.get("data") or []):
        if isinstance(m, dict) and m.get("id"):
            ids.append(str(m["id"]))
    kv_pct = parse_vllm_kv_cache_pct(metrics_text)
    used_gb: Optional[float] = None
    used_pct: Optional[float] = None
    if resident_gb and resident_gb > 0:
        used_gb = round(float(resident_gb), 1)
        used_pct = round(used_gb / vram_gb * 100.0, 1) if vram_gb > 0 else 0.0
    # 分级：模型没挂=warn；KV cache 水位有则按它分级；否则按预留占比（有则）分级
    if not ids:
        level = "warn"
    else:
        gauge = kv_pct if kv_pct is not None else used_pct
        if gauge is None:
            level = "ok"
        else:
            level = "high" if gauge >= HIGH_PCT else ("warn" if gauge >= WARN_PCT else "ok")
    per_model = (round(used_gb / len(ids), 1) if (used_gb and ids) else None)
    models = [{"name": i, "size_gb": per_model, "until": "常驻（vLLM）"} for i in ids]
    note_parts = ["vLLM 常驻" if ids else "vLLM 进程在、模型目录为空"]
    if ids:
        note_parts.append("、".join(ids[:3]))
    if kv_pct is not None:
        note_parts.append(f"KV cache {kv_pct:g}%")
    return {"name": name, "reachable": True, "error": "",
            "total_gb": vram_gb, "used_gb": used_gb, "used_pct": used_pct,
            "level": level, "models": models, "kind": "vllm",
            "kv_cache_pct": kv_pct, "note": " · ".join(note_parts)}


def summarize_fleet(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """整队汇总：整体 level 取最差（unknown 视作 warn——探不到该报修不该装绿）。"""
    rank = {"ok": 0, "warn": 1, "unknown": 1, "high": 2}
    worst = "ok"
    for r in rows:
        lv = str(r.get("level") or "unknown")
        if rank.get(lv, 1) > rank.get(worst, 0):
            worst = lv
    return {"level": worst if rows else "ok", "hosts": rows}


# ── 探针（30s TTL 缓存；看板轮询不打爆 LAN） ─────────────────────────
_CACHE: Dict[str, Any] = {"ts": 0.0, "key": "", "result": None}
_TTL_SEC = 30.0


async def probe_hosts(config: Dict[str, Any], *, force: bool = False) -> Optional[Dict[str, Any]]:
    """并发探测全部主机 /api/ps 并聚合。未启用 → None。"""
    hosts = parse_hosts(config)
    if not hosts:
        return None
    key = "|".join(h["base_url"] for h in hosts)
    now = time.time()
    if (not force and _CACHE["result"] is not None and _CACHE["key"] == key
            and now - _CACHE["ts"] < _TTL_SEC):
        return _CACHE["result"]

    import asyncio

    import httpx

    async def _one(h: Dict[str, Any]) -> Dict[str, Any]:
        if h.get("kind") == "vllm":
            try:
                async with httpx.AsyncClient(timeout=3.0) as hc:
                    resp = await hc.get(h["base_url"] + "/v1/models")
                    resp.raise_for_status()
                    payload = resp.json()
                    metrics_text: Optional[str] = None
                    try:
                        mr = await hc.get(h["base_url"] + "/metrics")
                        if mr.status_code == 200:
                            metrics_text = mr.text
                    except Exception:
                        metrics_text = None   # 指标口关着不算不可达
                return summarize_vllm_host(
                    h["name"], h["vram_gb"], payload, metrics_text=metrics_text,
                    resident_gb=h.get("resident_gb"))
            except Exception as e:
                return summarize_vllm_host(h["name"], h["vram_gb"], None, error=str(e))
        try:
            async with httpx.AsyncClient(timeout=3.0) as hc:
                resp = await hc.get(h["base_url"] + "/api/ps")
                resp.raise_for_status()
                return summarize_host(h["name"], h["vram_gb"], resp.json())
        except Exception as e:
            return summarize_host(h["name"], h["vram_gb"], None, error=str(e))

    rows = list(await asyncio.gather(*(_one(h) for h in hosts)))
    result = summarize_fleet(rows)
    _CACHE.update({"ts": now, "key": key, "result": result})
    return result
