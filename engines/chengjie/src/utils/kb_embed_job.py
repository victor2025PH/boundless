"""知识库「启用条目 100% 向量化」任务（2026-10-08 智语 · KB 可用化）。

此前有三处各写各的向量化：启动预热 ``bootstrap.background_tasks.warmup_embeddings``、
管理端「向量化」按钮 ``kb_routes._run_embed_all``、保存即向量化 ``_embed_entry_now``。
拼接文本不一致（预热不带示例回复、触发词不加权），批次返回数对不上就整批放弃、
不逐条重试，也没有地方能回答「为什么 22 条启用只向量化了 1 条」。本模块收成一处：

- ``build_embed_text(entry)``：唯一的条目拼接口径（触发词加权 + 标题 + 场景 + 步骤 + 示例）；
- ``embed_pending_entries(kb, embed_fn)``：增量跑完所有「启用且无向量」的条目，批失败
  逐条重试，返回 done / failed / failed_ids / 前后覆盖率；
- ``call_embedding_api(ai_cfg, texts)``：与运行期同源的端点选择（独立嵌入端点优先）；
- ``describe_embedding_endpoint(ai_cfg)``：只报端点类型和模型名，**绝不回显 key**，
  用来解释覆盖率上不去（如裸 DeepSeek 对话端点没有 /embeddings）。

CLI：``python scripts/kb_embed_all.py``（默认 dry-run，只报覆盖率和待办；``--apply`` 才写库）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

EmbedFn = Callable[[List[str]], Awaitable[Optional[List[List[float]]]]]

_MAX_EMBED_CHARS = 800


def _triggers_text(raw: Any) -> str:
    if isinstance(raw, (list, tuple)):
        return " ".join(str(t) for t in raw if t)
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            if isinstance(v, list):
                return " ".join(str(t) for t in v if t)
        except Exception:
            pass
        return raw
    return ""


def build_embed_text(entry: Dict[str, Any]) -> str:
    """条目 → 向量化文本。触发词出现两次（加权），其余按 标题 / 场景 / 步骤 / 示例回复。"""
    if not isinstance(entry, dict):
        return ""
    trig = _triggers_text(entry.get("triggers")).strip()
    parts = [
        (trig + " " + trig) if trig else "",
        str(entry.get("title") or ""),
        str(entry.get("scenario") or ""),
        str(entry.get("steps") or ""),
        str(entry.get("example_reply_zh") or ""),
    ]
    return " ".join(p.strip() for p in parts if p and p.strip())[:_MAX_EMBED_CHARS]


def _valid_vec(v: Any) -> bool:
    return isinstance(v, (list, tuple)) and len(v) > 0


async def embed_pending_entries(
    kb: Any,
    embed_fn: EmbedFn,
    *,
    batch_size: int = 20,
    pause_s: float = 0.0,
    should_continue: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """把「启用且无向量」的条目全部向量化。幂等：已有向量的条目不会再算。

    批次返回数量对不上 / 抛异常时逐条重试，单条仍失败才记入 failed_ids。
    返回 ``{"pending", "done", "failed", "failed_ids", "coverage_before", "coverage_after"}``。
    """
    cov_before = kb.embedding_coverage()
    pending: List[Dict[str, Any]] = list(kb.get_entries_without_embedding())
    if limit is not None:
        pending = pending[: max(0, int(limit))]
    stat: Dict[str, Any] = {
        "pending": len(pending), "done": 0, "failed": 0, "failed_ids": [],
        "coverage_before": cov_before,
    }

    async def _embed(texts: List[str]) -> Optional[List[List[float]]]:
        try:
            return await embed_fn(texts)
        except Exception as e:  # 端点抖动不让整个任务崩
            logger.warning("[kb_embed] embedding 调用异常: %s", e)
            return None

    bs = max(1, int(batch_size))
    for i in range(0, len(pending), bs):
        if should_continue is not None and not should_continue():
            break
        batch = pending[i: i + bs]
        texts = [build_embed_text(e) for e in batch]
        vecs = await _embed(texts)
        if vecs and len(vecs) == len(batch):
            pairs = list(zip(batch, vecs))
        else:
            # 整批失败 → 逐条重试，定位到底是哪条（或端点本身）不行
            pairs = []
            for e, t in zip(batch, texts):
                one = await _embed([t])
                pairs.append((e, one[0] if one and len(one) == 1 else None))
        for e, v in pairs:
            if _valid_vec(v):
                kb.set_single_embedding(e["id"], list(v))
                stat["done"] += 1
            else:
                stat["failed"] += 1
                stat["failed_ids"].append(str(e.get("id")))
        if progress is not None:
            try:
                progress(dict(stat))
            except Exception:
                pass
        if pause_s:
            await asyncio.sleep(pause_s)
    stat["coverage_after"] = kb.embedding_coverage()
    return stat


def _resolve_endpoint(ai_cfg: Dict[str, Any]) -> Dict[str, str]:
    ai_cfg = ai_cfg or {}
    emb_base = str(ai_cfg.get("embedding_base_url") or "").strip().rstrip("/")
    if emb_base:
        base_url = emb_base if emb_base.endswith("/v1") else emb_base + "/v1"
        return {
            "kind": "dedicated",
            "base_url": base_url,
            "model": str(ai_cfg.get("embedding_model") or "bge-m3"),
            "_key": str(ai_cfg.get("embedding_api_key") or ai_cfg.get("api_key") or "ollama").strip(),
        }
    base_url = str(ai_cfg.get("base_url") or "https://api.deepseek.com").rstrip("/")
    kind = "gateway" if "/api/ai/v1" in base_url else "chat_fallback"
    return {
        "kind": kind,
        "base_url": base_url,
        "model": str(ai_cfg.get("embedding_model") or "text-embedding-v2"),
        "_key": str(ai_cfg.get("api_key") or "").strip() or "ollama",
    }


def describe_embedding_endpoint(ai_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """端点诊断（可进日志 / 报告）：类型、主机、模型、是否大概率支持 /embeddings。不含 key。"""
    ep = _resolve_endpoint(ai_cfg)
    host = ep["base_url"].split("://", 1)[-1].split("/", 1)[0]
    likely = ep["kind"] in ("dedicated", "gateway")
    hint = ""
    if ep["kind"] == "chat_fallback":
        likely = "deepseek" not in host.lower()
        hint = ("未配置 ai.embedding_base_url，回落对话端点；DeepSeek 对话端点没有 /embeddings，"
                "向量化会全失败——配一个独立嵌入端点（如 LAN bge-m3 / Ollama）或官网网关")
    return {"kind": ep["kind"], "host": host, "model": ep["model"],
            "likely_supports_embeddings": bool(likely), "hint": hint}


async def call_embedding_api(ai_cfg: Dict[str, Any], texts: Sequence[str],
                             *, timeout: float = 60.0) -> List[List[float]]:
    """OpenAI 兼容 ``POST {base}/embeddings``；失败返回空列表（调用方按失败处理）。"""
    texts = [str(t) for t in (texts or [])]
    if not texts:
        return []
    ep = _resolve_endpoint(ai_cfg)
    import httpx
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{ep['base_url']}/embeddings",
                headers={"Authorization": f"Bearer {ep['_key']}",
                         "Content-Type": "application/json"},
                json={"model": ep["model"], "input": texts},
            )
            data = resp.json()
            items = sorted(data["data"], key=lambda x: x["index"])
            return [item["embedding"] for item in items]
    except Exception as e:
        logger.warning("[kb_embed] Embedding API 调用失败（%s %s）: %s",
                       ep["kind"], ep["base_url"].split("://", 1)[-1].split("/", 1)[0],
                       type(e).__name__)
        return []
