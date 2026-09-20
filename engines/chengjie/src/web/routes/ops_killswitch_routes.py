"""G1 全局 Kill-Switch 管理 API（反封号护栏三件套）。

把 ``src/ops/kill_switch.py`` 的紧急停发开关暴露给运营：查看 / 置位 / 解除。
读用 api_auth，写（置位/解除）用 manage_ops 权限（与「确认运维事件」同级）。

端点：
- GET    /api/ops/kill-switch       当前生效的作用域列表
- POST   /api/ops/kill-switch       置位 {scope?, reason?, ttl_sec?}（scope 缺省=global）
- DELETE /api/ops/kill-switch       解除 {scope?}（scope 缺省=global）
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


def scope_parts(scope: str) -> tuple:
    """作用域 → (platform, account_id) 审计归属（纯函数）。

    ``account:<p>:<id>`` 归到具体号（号健康史可见「这号被谁冻过几次」）；
    ``platform:<p>`` 归平台；``global`` 归 ``global`` 伪平台（不污染
    telegram 缺省桶）。
    """
    s = str(scope or "")
    if s.startswith("account:"):
        rest = s[len("account:"):]
        parts = rest.split(":", 1)
        if len(parts) == 2 and parts[0]:
            return parts[0], parts[1]
    if s.startswith("platform:"):
        return s[len("platform:"):], ""
    return "global", ""


def _audit_killswitch(kind: str, scope: str, *, actor: str = "",
                      reason: str = "", detail: str = "") -> None:
    """置位/解除落 ops_events 审计（P1 2026-08-23）。

    此前 kill_switch 表只存当前态、clear 即删行——「谁在何时冻过/解过」查
    无对证。best-effort：审计失败绝不阻断急停操作本身。
    """
    try:
        from src.ops.ops_events import get_ops_event_store
        store = get_ops_event_store()
        if store is None:
            return
        p, aid = scope_parts(scope)
        extra = f"scope={scope}"
        if actor:
            extra += f" actor={actor}"
        if detail:
            extra += f" {detail}"
        store.record(kind, platform=p, account_id=aid,
                     reason=str(reason or ""), detail=extra)
    except Exception:
        logger.debug("[kill-switch] ops_events 审计写入失败（忽略）",
                     exc_info=True)


def register_ops_killswitch_routes(app, ctx) -> None:
    """挂载 /api/ops/kill-switch* 到 app。"""
    _api_auth = ctx.api_auth
    _api_write = ctx.api_write

    def _ks():
        from src.ops.kill_switch import get_kill_switch
        return get_kill_switch()

    def _actor(request: Request) -> str:
        try:
            return str(
                request.session.get("username")
                or request.session.get("role")
                or "admin"
            )
        except Exception:
            return "admin"

    @app.get("/api/ops/kill-switch")
    async def api_ops_killswitch_status(request: Request):
        """当前所有生效的紧急停发作用域（global 优先）。"""
        _api_auth(request)
        items = _ks().status()
        return {
            "ok": True,
            "items": items,
            "count": len(items),
            "frozen": any(i["scope"] == "global" for i in items),
        }

    @app.post("/api/ops/kill-switch")
    async def api_ops_killswitch_set(request: Request):
        """🛑 置位紧急停发。需 manage_ops 权限。

        body：``{scope?, reason?, ttl_sec?}``。scope 缺省 ``global``（一键全停）；
        ttl_sec>0 时到点自动恢复（防「停了忘开」）。
        """
        _api_write("manage_ops")(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        scope = str((body or {}).get("scope") or "global").strip()
        reason = str((body or {}).get("reason") or "")[:500]
        try:
            ttl_sec = float((body or {}).get("ttl_sec") or 0)
        except (TypeError, ValueError):
            ttl_sec = 0.0
        try:
            rec = _ks().set(scope, reason=reason, actor=_actor(request), ttl_sec=ttl_sec)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        _audit_killswitch("kill_switch_set", rec["scope"],
                          actor=str(rec.get("actor") or ""), reason=reason,
                          detail=f"ttl_sec={ttl_sec:g}")
        return {"ok": True, "set": rec}

    @app.delete("/api/ops/kill-switch")
    async def api_ops_killswitch_clear(request: Request):
        """🔓 解除紧急停发。需 manage_ops 权限。body：``{scope?}``（缺省 global）。"""
        _api_write("manage_ops")(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        scope = str((body or {}).get("scope") or "global").strip()
        try:
            from src.ops.kill_switch import normalize_scope
            scope_norm = normalize_scope(scope)
            existed = _ks().clear(scope_norm)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        _audit_killswitch("kill_switch_clear", scope_norm,
                          actor=_actor(request),
                          detail=f"was_active={existed}")
        return {"ok": True, "scope": scope_norm, "was_active": existed}
