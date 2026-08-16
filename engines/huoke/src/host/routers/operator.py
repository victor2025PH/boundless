# -*- coding: utf-8 -*-
"""AI 操盘手路由（2026-08-16 P4 MVP）。

端点全部挂 requires_role("admin","operator")——tick 是控制动作，
状态/审计里含设备与线索信息，viewer 不该看。
引擎逻辑在 src/host/operator_engine.py，这里只是 HTTP 壳。
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .auth import requires_role

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/operator", tags=["operator"])

_OPS_ROLE = Depends(requires_role("admin", "operator"))


class TickBody(BaseModel):
    dry_run: Optional[bool] = None   # None = 用 operator_goals.yaml 里的值


@router.get("/status", dependencies=[_OPS_ROLE])
def operator_status():
    """操盘手当前状态：双闸/目标摘要/最近决策。"""
    from ..operator_engine import status
    return status()


@router.post("/tick", dependencies=[_OPS_ROLE])
def operator_tick(body: TickBody = None):
    """手动跑一轮操盘手（感知→决策→护栏→执行→审计）。

    enabled=false 时安全空转；dry_run 只决策不执行。
    """
    from ..operator_engine import tick
    dry = body.dry_run if body else None
    return tick(dry_run=dry)


@router.get("/decisions", dependencies=[_OPS_ROLE])
def operator_decisions(limit: int = 50, device_id: str = ""):
    """决策审计流水（含 hold/skip/blocked 的理由）。"""
    from ..operator_engine import get_decisions
    return {"decisions": get_decisions(limit=min(limit, 500),
                                       device_id=device_id)}
