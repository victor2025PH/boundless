# -*- coding: utf-8 -*-
"""迁移包导出路由（账号资产保全 · 实施47 §5「工单 2」，2026-08-28）。

- ``GET /api/accounts/{platform}/{account_id}/export-migration`` — 下载迁移包 zip
- ``GET /api/accounts/{platform}/{account_id}/migration-preview`` — 零副作用体量预览

打包逻辑全在 ``src.inbox.migration_export``（单一事实源），这里是薄路由：鉴权、
参数校验、注册表读取、`FileResponse` + 后台清理、审计。

## 为什么自成一个路由模块

同主题的 `reconnect_claim_routes` 就是这么做的（实施47 P1 的另一条车道）。
`unified_inbox_account_routes.py` 已经 5900+ 行且是多线热区，往里塞一条独立
能力只会加重合并冲突面；资产中心页面的 `features.export_migration` 探测按
**路由路径**做（`any(p.endswith("/export-migration"))`），不关心谁注册的。

## 权限与既有导出同闸

`_require_account_manager` 的语义（agent/viewer 拒绝）在本仓被复刻过三次
（`asset_center_routes._require_manager`、这里、原文件）——**刻意各自持有 5 行而
不跨模块 import**：那个函数住在 5900 行模块的模块级，为一条守卫把整套依赖拖进
测试装配不值得。三处 deny 集必须一致，由
`tests/test_migration_export_routes.py::test_deny_roles_match_account_routes` 钉住。

## 导出是读操作 → 任意账号可导

与 `export-history` 同一判断：读操作零破坏、无复活风险，活跃号导出＝备份语义。
**不套用 `purge-history` 的 `_purge_history_blocked` 闸门**——那道闸是防「误删在线
号」的，用它挡导出等于「越是正在服务的号越不许备份」，正好反了。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from src.web.web_i18n import tr

logger = logging.getLogger(__name__)

#: 与 `unified_inbox_account_routes._ACCOUNT_MANAGE_DENY_ROLES` 同集（导出是数据外携）
_MANAGE_DENY_ROLES = {"agent", "viewer"}


def _require_manager(request: Request) -> None:
    try:
        role = str(request.session.get("role", "") or "")
    except Exception:
        role = ""
    if role in _MANAGE_DENY_ROLES:
        raise HTTPException(403, tr(request, "err.perm.supervisor_required"))


def _actor(request: Request) -> str:
    try:
        return str(request.session.get("username", "") or "") or "api"
    except Exception:
        return "api"


def _registry_facts(platform: str, account_id: str) -> Dict[str, Any]:
    """注册表侧事实：``label`` + 封禁信息（缺失一律空，绝不阻断导出）。

    封禁语义住在 registry ``meta.banned/ban_reason``（``ban_signal`` 写入，刻意
    不改 status 列）——迁移包的 manifest 要带上它，因为「这个包是在什么处境下导
    出的」本身就是资产交付的一部分。
    """
    out: Dict[str, Any] = {"label": "", "ban": {}}
    try:
        from src.integrations.account_registry import get_account_registry
        row = get_account_registry().get(platform, account_id)
    except Exception:
        logger.debug("[migration_export] 注册表不可用（按无标签导出）",
                     exc_info=True)
        return out
    if not row:
        return out
    out["label"] = str(row.get("label") or "")
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    if meta.get("banned"):
        out["ban"] = {
            "banned": True,
            "reason": str(meta.get("ban_reason") or ""),
            "banned_at": float(meta.get("banned_at") or 0),
        }
    return out


def _crm_lookup(request: Request, platform: str, account_id: str):
    """→ ``callable(chat_key) -> dict|None``，contacts 子系统缺席时返回 None。

    CRM 字段（漏斗阶段/亲密度/标签）只影响**加回顺序建议**，不是加回的必要条件，
    所以整条链是 best-effort：子系统没启用就让 manifest 里那几列留空，而不是让
    导出失败。
    """
    try:
        from src.web.routes.unified_inbox_services import _contacts_store
        cstore = _contacts_store(request)
    except Exception:
        logger.debug("[migration_export] contacts 子系统不可用（CRM 列留空）",
                     exc_info=True)
        return None
    if cstore is None:
        return None

    def _lookup(chat_key: str) -> Optional[Dict[str, Any]]:
        try:
            ci = cstore.get_ci_by_external(platform, account_id, chat_key)
            if ci is None:
                return None
            cid = str(getattr(ci, "contact_id", "") or
                      (ci.get("contact_id") if isinstance(ci, dict) else "") or "")
            if not cid:
                return None
            c = cstore.get_contact(cid)
            if c is None:
                return {"contact_id": cid}

            def _g(name: str) -> Any:
                if isinstance(c, dict):
                    return c.get(name)
                return getattr(c, name, None)

            return {
                "contact_id": cid,
                "funnel_stage": _g("funnel_stage") or "",
                "intimacy_score": _g("intimacy_score"),
                "tags": _g("tags") or [],
                "notes": _g("notes") or "",
                "follow_up_at": _g("follow_up_at") or 0,
            }
        except Exception:
            logger.debug("[migration_export] CRM 单条查询失败 ck=%s", chat_key,
                         exc_info=True)
            return None

    return _lookup


def _kit_out_dir() -> Optional[str]:
    """迁移包临时落点：实例数据根下的 ``tmp_migration``（跟着实例走、进不了备份）。

    ⚠ 相对路径在本仓是**迁移后静默失真**的老病（见 `tests/test_static_asset_paths`）：
    这里刻意走 `AITR_CONFIG_PATH`/`AITR_DATA_DIR` 契约，两者都没有就返回 None 让
    调用方回落系统临时目录——绝不用 CWD 相对路径拼一个「看起来对」的落点。
    包是**一次性产物**（下载完即删），不需要进备份。
    """
    try:
        env_cfg = (os.environ.get("AITR_CONFIG_PATH") or "").strip()
        if env_cfg:
            return str(os.path.join(
                os.path.dirname(os.path.dirname(env_cfg)), "tmp_migration"))
        env_dir = (os.environ.get("AITR_DATA_DIR") or "").strip()
        if env_dir:
            return str(os.path.join(env_dir, "tmp_migration"))
    except Exception:
        logger.debug("[migration_export] 落点解析失败（回落系统临时目录）",
                     exc_info=True)
    return None


def _cleanup(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        logger.debug("[migration_export] 临时包清理失败：%s", path)


def register_migration_export_routes(app, *, api_auth) -> None:
    """挂载迁移包导出端点（与 export-history 同权限闸）。"""

    @app.get("/api/accounts/{platform}/{account_id}/migration-preview")
    async def api_migration_preview(platform: str, account_id: str,
                                    request: Request):
        """迁移包体量 + 可加回覆盖率预览（零副作用，供 UI 在下载前给预期）。

        刻意与真导出走**同一套取数**（`collect_contacts` + `count_account_data`）：
        预览说 62 人可加回、导出出来 3 人，比没有预览更糟（本仓 purge-history 的
        「预览=真删同一判定单点」纪律同源）。代价是预览也要跑一次联系人并集查询，
        故不适合放进高频轮询面。
        """
        api_auth(request)
        _require_manager(request)
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            raise HTTPException(503, tr(request, "err.ws.store_unavailable"))
        from src.inbox.migration_export import (
            collect_contacts, summarize_reachability,
        )
        plat = str(platform or "").lower()
        acct = str(account_id or "")
        try:
            counts = dict(store.count_account_data(plat, acct) or {})
        except Exception:
            logger.debug("[migration_export] 预览体量失败", exc_info=True)
            counts = {}
        rows = collect_contacts(store, plat, acct,
                                crm_lookup=_crm_lookup(request, plat, acct))
        facts = _registry_facts(plat, acct)
        return {
            "ok": True, "platform": plat, "account_id": acct,
            "label": facts["label"], "ban": facts["ban"],
            "conversations": int(counts.get("conversations") or 0),
            "messages": int(counts.get("messages") or 0),
            "contacts": len(rows),
            "reachability": summarize_reachability(rows),
        }

    @app.get("/api/accounts/{platform}/{account_id}/export-migration")
    async def api_export_migration(platform: str, account_id: str,
                                   request: Request, media: int = 0):
        """导出迁移包 zip（``?media=1`` 连媒体原件一起打，默认不打）。

        审计 ``account_export_migration`` **只在包成功落盘后**落一条（与
        export-history「完整走完才审计」同纪律：半个文件不算一次导出），detail
        只记行数与覆盖率，绝不含联系人内容。
        """
        api_auth(request)
        _require_manager(request)
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            raise HTTPException(503, tr(request, "err.ws.store_unavailable"))
        from src.inbox.migration_export import build_migration_kit
        plat = str(platform or "").lower()
        acct = str(account_id or "")
        facts = _registry_facts(plat, acct)
        try:
            from src.utils.app_identity import app_version
            ver = str(app_version() or "")
        except Exception:
            ver = ""
        try:
            kit = build_migration_kit(
                store, plat, acct,
                label=facts["label"], ban=facts["ban"],
                include_media=bool(int(media or 0)),
                crm_lookup=_crm_lookup(request, plat, acct),
                out_dir=_kit_out_dir(), app_version=ver)
        except Exception:
            logger.error("[migration_export] 打包失败 %s:%s", plat, acct,
                         exc_info=True)
            raise HTTPException(500, tr(request, "err.ws.export_failed"))

        try:
            from src.ops.ops_events import get_ops_event_store
            evs = get_ops_event_store()
            if evs is not None:
                r = kit.reachability
                evs.record(
                    "account_export_migration", account_id=acct, platform=plat,
                    reason="ok",
                    detail=(f"convs={kit.counts.get('conversations', 0)};"
                            f"msgs={kit.counts.get('messages', 0)};"
                            f"contacts={kit.counts.get('contacts', 0)};"
                            f"reach={r.get('covered', 0)}/{r.get('total', 0)};"
                            f"media={kit.counts.get('media_files', 0)};"
                            f"bytes={kit.size_bytes};"
                            f"actor={_actor(request)};"
                            f"reconciled={int(kit.reconciled)}"))
        except Exception:
            logger.debug("[migration_export] 审计失败（忽略）", exc_info=True)

        if not kit.reconciled:
            # 不拦下载（包本身是自洽的、行数如实写在 manifest 里），但要留下
            # WARNING：导出期间库在变动，或有孤儿消息——这是「导出完整率」指标
            # 唯一的线上信号来源。
            logger.warning(
                "[migration_export] 体量对账不一致 %s:%s 写入=%s 预估=%s",
                plat, acct, kit.counts, kit.store_counts)

        return FileResponse(
            kit.path, media_type="application/zip", filename=kit.filename,
            background=BackgroundTask(_cleanup, kit.path))
