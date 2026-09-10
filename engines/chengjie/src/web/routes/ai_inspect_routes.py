# -*- coding: utf-8 -*-
"""AI 提示词调试口（2026-09-11）：模型**实际收到**的 messages + usage + 缓存命中率 + 上下文深度档。

  GET /api/ai/prompt-inspect?conv=<子串>&limit=20   摘要列表（新在前）+ 缓存滚动统计 + 当前深度档
  GET /api/ai/prompt-inspect/{seq}                  单条全文（system 全文 + 历史 messages）

只读、admin 令牌口（api_auth）、数据来自进程内环形缓冲（src/ai/prompt_trace.py），重启即清。
用法：出稿后打开 /api/ai/prompt-inspect?conv=<chat_id> 看 system_sections 里有没有
【后台人设定位】【用户长期记忆要点】，budget.over 是否为 0，usage.cache_hit_tokens 占比多少。
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import Depends, Request


def register_ai_inspect_routes(app, *, api_auth: Callable, config_manager: Any = None) -> None:

    def _cfg() -> dict:
        try:
            c = getattr(config_manager, "config", None)
            return c if isinstance(c, dict) else {}
        except Exception:
            return {}

    @app.get("/api/ai/prompt-inspect")
    async def api_ai_prompt_inspect(request: Request, conv: str = "", limit: int = 20,
                                    _=Depends(api_auth)):
        from src.ai import context_depth, prompt_trace
        lim = max(1, min(int(limit or 20), prompt_trace.MAX_ENTRIES))
        items = prompt_trace.list_entries(conv=str(conv or "").strip(), limit=lim)
        return {
            "ok": True,
            "depth": context_depth.describe(_cfg()),
            "cache": prompt_trace.cache_stats(),
            "count": len(items),
            "items": items,
        }

    @app.get("/api/ai/prompt-inspect/{seq}")
    async def api_ai_prompt_inspect_one(request: Request, seq: int, _=Depends(api_auth)):
        from src.ai import prompt_trace
        e = prompt_trace.get_entry(int(seq))
        if not e:
            return {"ok": False, "error": "not_found"}
        return {"ok": True, "item": e}
