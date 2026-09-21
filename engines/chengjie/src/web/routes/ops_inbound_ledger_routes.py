"""入站结局台账只读 API（#279 / #44 / #68「消息发了但收件箱没有」排障出口）。

端点（api_auth）：
- GET /api/ops/inbound-ledger
    ?limit=50&conversation_id=&chat_key=&drops_only=0
    → {ok, stats:{inserted, duplicate, tombstone, no_content, ..., dropped_total}, recent:[...]}
"""

from __future__ import annotations

from fastapi import Request


def register_ops_inbound_ledger_routes(app, ctx) -> None:
    """挂载 GET /api/ops/inbound-ledger。"""
    _api_auth = ctx.api_auth

    @app.get("/api/ops/inbound-ledger")
    async def api_ops_inbound_ledger(
        request: Request,
        limit: int = 50,
        conversation_id: str = "",
        chat_key: str = "",
        drops_only: int = 0,
    ):
        _api_auth(request)
        from src.inbox.inbound_ledger import dump_stats, recent
        return {
            "ok": True,
            "stats": dump_stats(),
            "recent": recent(
                limit, conversation_id=conversation_id, chat_key=chat_key,
                drops_only=bool(drops_only),
            ),
        }
