"""合规只读导出（WP-4 2026-08）。

``GET /api/admin/crisis-referrals`` —— SB 243 年报用「危机转介计数」导出 +
合规开关现状回显（支持排障/合同附件采数的单一读面）。只读零副作用；
计数写入面在 ``src.compliance.crisis_referrals.record_crisis_referral``
（危机处置链打点，接线见模块 docstring 的 rider 说明）。
"""
from __future__ import annotations

import logging

from fastapi import Depends, Request

logger = logging.getLogger(__name__)


def register_compliance_routes(app, *, api_auth, config_manager=None) -> None:
    """挂载合规导出端点（管理员只读）。"""

    @app.get("/api/admin/crisis-referrals")
    async def api_crisis_referrals(request: Request, days: int = 90,
                                   _=Depends(api_auth)):
        from src.compliance import (
            crisis_protocol_url, honest_identity_enabled, notice_enabled)
        from src.compliance.crisis_referrals import referral_snapshot
        from src.compliance.disclosure import marks_count

        config = getattr(config_manager, "config", None) or {}
        return {
            "ok": True,
            "switches": {
                "notice": notice_enabled(config),
                "honest_identity": honest_identity_enabled(config),
                "crisis_protocol_url": crisis_protocol_url(config),
            },
            "referrals": referral_snapshot(days=days),
            "disclosure_marks": marks_count(),
        }
