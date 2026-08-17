"""统一收件箱——工作链 / 工作链执行可视化路由域（巨石拆分 slice 20）。

把工作链子域从 ``register_unified_inbox_routes`` 巨型闭包中整体外移为
``register_workflow_routes(app, *, api_auth)``，由主 register 在**原位置**调用：

- 工作链 CRUD：``workflow-chains``
- Phase 47 工作链执行可视化：
  ``chain-executions`` / ``conv/{id}/chain-executions`` /
  ``chain-executions/{exec_id}/cancel`` / ``conv/{id}/start-chain``

（原 Phase 37「AI 下一步」推荐/执行端点 ``conv/{id}/next-actions`` /
``conv/{id}/execute-action`` 与自定义动作 ``workflow-actions`` CRUD 已于
2026-08-14 随「AI 下一步」面板整体下线；情绪标签词表/仲裁仍在
``src.inbox.effective_mood``，工作链 tag 步照常消费。）

依赖全部朝下：services 存储、auth._agent_from_request；工作链监控/event_bus
均为 handler 内局部 import。只收 api_auth 一个参数（零闭包私有依赖）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import Depends, HTTPException, Request

from src.web.routes.unified_inbox_auth import _agent_from_request
from src.web.routes.unified_inbox_services import _contacts_store, _inbox_store
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def workflows_disabled_reason_cfg(cfg: Any) -> str:
    """C3/D1/E1：工作流模块可用性判定（config 版单一事实源），返回
    ``""``（可用）/ ``config`` / ``license``。

    两道闸（顺序有意义——运营显式关闭是明确意图，报「未启用」而非「请升级」）：
    1. ``inbox.workflows.enabled``（缺省 True=旧行为零变化，运营 kill-switch）；
    2. 授权档位 ``feature_gate.feature_enabled("workflows")``（FEATURE_MIN_PLAN
       登记为 pro；gate 总开关默认关=全放行，licensing 层异常恒放行）。

    消费方：本文件 API 闸门（`_require_workflows` → 403 两种文案分流）+
    admin.py 的 ``/workspace/workflows`` 页面路由（license 锁 → 302 /membership
    升级引导；运营关闭 → 404 模块不存在于此部署）。判定逻辑必须同源——
    「API 拦了页面没拦」或口径漂移都是缝。

    语义边界（商业化分层打包用，不是安全闸门）：
    - 关闭/锁定时链管理/执行/漏斗端点返回 403，工作台「工作链执行」卡据此自动隐藏；
    - **只拦新动作，不中断在途执行**——WorkflowRunner 刻意不闸（运行中的客户跟进
      不该因打包开关被悄悄掐断；要停用先去 /workflows 取消）；
    - 读配置失败按启用处理（配置系统异常不应把已出货模块打死）。
    """
    try:
        cfg = cfg or {}
        if not isinstance(cfg, dict):
            return ""
        wf = (cfg.get("inbox") or {}).get("workflows") or {}
        if not bool(wf.get("enabled", True)):
            return "config"
        try:
            from src.licensing.feature_gate import feature_enabled
            if not feature_enabled("workflows", cfg):
                return "license"
        except Exception:
            pass  # licensing 层异常恒放行（与 feature_gate 内部口径一致）
        return ""
    except Exception:
        return ""


def _workflows_disabled_reason(request: Request) -> str:
    try:
        cfg = getattr(
            getattr(request.app.state, "config_manager", None), "config", None) or {}
    except Exception:
        cfg = {}
    return workflows_disabled_reason_cfg(cfg)


def _workflows_enabled(request: Request) -> bool:
    return _workflows_disabled_reason(request) == ""


def _require_workflows(request: Request) -> None:
    """链域端点统一闸门：不可用 → 403（前端组件按 status 隐藏整卡）。
    文案分流：运营关闭=「未启用」；档位不够=「升级解锁」——坐席该做的事不同。
    仅 license 分支计锁触达（E6 定价信号；config 关闭是部署选择，刻意不计）。"""
    reason = _workflows_disabled_reason(request)
    if reason == "config":
        raise HTTPException(403, tr(request, "err.ws.workflows_disabled"))
    if reason == "license":
        try:
            from src.web.feature_lock_stats import get_feature_lock_stats
            get_feature_lock_stats().record("workflows", "api")
        except Exception:
            pass
        raise HTTPException(403, tr(request, "err.lic.feature_locked"))


def register_workflow_routes(app, *, api_auth) -> None:
    """挂载动作推荐 / 自定义动作·工作链 CRUD / 工作链执行可视化端点。"""

    def _goal_chain_event(request: Request, conversation_id: str, kind: str, detail: str = "") -> None:
        """C 弱联动：链生命周期回写当前会话活跃目标的事件台账（best-effort）。"""
        try:
            cm = getattr(request.app.state, "config_manager", None)
            if cm is None:
                return
            from src.companion.goals.service import record_chain_event
            record_chain_event(
                getattr(cm, "config", None) or {},
                getattr(cm, "config_path", None),
                conversation_id, kind, detail)
        except Exception:
            logger.debug("goal chain event 回写失败（已忽略）", exc_info=True)

    # （原 Phase 37「AI 下一步」推荐/执行端点与自定义动作 CRUD 已于 2026-08-14
    #   随「AI 下一步」面板整体下线；工作链域保留如下。）

    # ── 工作链管理 ────────────────────────────────────────────────────────

    @app.get("/api/workspace/workflow-chains")
    async def api_workflow_chains_list(request: Request):
        """AA1：列出所有工作链。"""
        api_auth(request)
        _require_workflows(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "chains": []}
        import json as _j
        chains = store.list_workflow_chains()
        for c in chains:
            try:
                c["steps"] = _j.loads(c.get("steps_json") or "[]")
            except Exception:
                c["steps"] = []
            try:
                c["trigger_conditions"] = _j.loads(c.get("trigger_conditions") or "{}")
            except Exception:
                c["trigger_conditions"] = {}
        return {"ok": True, "chains": chains}

    @app.post("/api/workspace/workflow-chains")
    async def api_workflow_chains_create(request: Request, _=Depends(api_auth)):
        _require_workflows(request)
        body = await request.json()
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}
        chain_id = store.upsert_workflow_chain(body)
        return {"ok": True, "chain_id": chain_id}

    @app.put("/api/workspace/workflow-chains/{chain_id}")
    async def api_workflow_chains_update(chain_id: str, request: Request, _=Depends(api_auth)):
        _require_workflows(request)
        body = await request.json()
        body["chain_id"] = chain_id
        store = _inbox_store(request)
        if store is None:
            return {"ok": False}
        store.upsert_workflow_chain(body)
        return {"ok": True, "chain_id": chain_id}

    @app.delete("/api/workspace/workflow-chains/{chain_id}")
    async def api_workflow_chains_delete(chain_id: str, request: Request, _=Depends(api_auth)):
        _require_workflows(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False}
        ok = store.delete_workflow_chain(chain_id)
        return {"ok": ok}

    @app.post("/api/workspace/workflow-chains/seed")
    async def api_workflow_chains_seed(request: Request, _=Depends(api_auth)):
        """B1：一键导入出厂模板链（幂等——按固定 chain_id 判重，已存在一律跳过不覆盖）。"""
        _require_workflows(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}
        from src.inbox.workflow_starter import ensure_starter_chains
        res = ensure_starter_chains(store)
        return {"ok": True, **res}

    # ─── Phase 47: 工作链执行可视化 ─────────────────────────────────────

    @app.get("/api/workspace/chain-executions")
    async def api_chain_executions_list(
        request: Request,
        status: str = "",
        conversation_id: str = "",
        limit: int = 50,
    ):
        """P47：全局工作链执行监控列表。"""
        api_auth(request)
        _require_workflows(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "executions": [], "count": 0}
        from src.inbox.workflow_monitor import enrich_executions
        rows = store.list_chain_executions(
            status=status.strip(), conversation_id=conversation_id.strip(), limit=limit,
        )
        enriched = enrich_executions(rows)
        running = sum(1 for e in enriched if e.get("status") == "running")
        return {
            "ok": True,
            "executions": enriched,
            "count": len(enriched),
            "running_count": running,
        }

    @app.get("/api/workspace/chain-funnel")
    async def api_chain_funnel(request: Request, days: int = 14):
        """B2：链效果漏斗——窗口内启动/完成/失败/取消 + 启动后 72h 回复率（近似归因，
        分母只算已满窗口期的执行）。监控页与 ops 消费同一口径。

        J 推荐跟随率：``rec_follow = {followed, eligible, rate}``——目标归因的
        启动里，goal 模板**有推荐链**的进分母（eligible），启动的恰是推荐链的
        进分子（followed）。模板无推荐 / 目标已删 / goals 未启用 → 不进分母
        （诚实：没有推荐可跟就不算「没跟」）。goal_id 明细在路由层消费后剔除，
        不对外暴露。"""
        api_auth(request)
        _require_workflows(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "total": {}, "chains": [],
                    "rec_follow": {"followed": 0, "eligible": 0, "rate": None}}
        from src.inbox.workflow_monitor import chain_funnel
        data = chain_funnel(store, days=max(1, min(90, int(days or 14))))
        pairs = data.pop("goal_chain_starts", {}) or {}
        followed = eligible = 0
        try:
            if pairs:
                from src.inbox.workflow_starter import GOAL_CHAIN_REC
                cm = getattr(request.app.state, "config_manager", None)
                gstore = None
                if cm is not None:
                    from src.companion.goals.service import (
                        get_configured_store, goals_enabled,
                    )
                    cfg = getattr(cm, "config", None) or {}
                    if goals_enabled(cfg):
                        gstore = get_configured_store(
                            cfg, getattr(cm, "config_path", None))
                if gstore is not None:
                    for gid, chains_map in pairs.items():
                        try:
                            goal = gstore.get_goal(str(gid)) or {}
                        except Exception:
                            goal = {}
                        rec = GOAL_CHAIN_REC.get(
                            str(goal.get("template") or ""), "")
                        if not rec:
                            continue
                        eligible += sum(int(n) for n in chains_map.values())
                        followed += int(chains_map.get(rec, 0))
        except Exception:
            logger.debug("chain-funnel 推荐跟随计算失败（已忽略）", exc_info=True)
        data["rec_follow"] = {
            "followed": followed,
            "eligible": eligible,
            "rate": round(followed / eligible, 3) if eligible else None,
        }
        # P3 2026-08-09：链推进循环心跳（bootstrap 挂 app.state；空 dict=循环
        # 没挂载）——链推进曾挂在 report.enabled 闸死的调度器上**从未运行**，
        # 「点了启动步骤永不走」这类静默断线从此在监控口可见。
        data["autorun"] = dict(
            getattr(request.app.state, "workflow_autorun_state", None) or {})
        # P1.5 2026-08-13 行动闭环读数：右栏「采用并拟稿/暂停/跳步/重试」的
        # ui-event 进程计数（chain_ 前缀）——「提醒→行动」转化率的直接读数。
        # 进程口径重启清零（与 goal 漏斗 chips 同精度承诺），持久趋势属 ui_event_trend。
        try:
            from src.web.ui_event_stats import get_ui_event_stats
            _ba = (get_ui_event_stats().dump() or {}).get("by_action") or {}
            data["adoption"] = {
                k: int(v) for k, v in _ba.items() if str(k).startswith("chain_")}
        except Exception:
            data["adoption"] = {}
        return data

    @app.get("/api/workspace/conv/{conversation_id}/chain-executions")
    async def api_conv_chain_executions(
        conversation_id: str, request: Request, status: str = "", limit: int = 20,
    ):
        """P47：会话级工作链执行记录。"""
        api_auth(request)
        _require_workflows(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "executions": [], "conversation_id": conversation_id}
        from src.inbox.workflow_monitor import enrich_executions
        rows = store.list_chain_executions(
            conversation_id=conversation_id, status=status.strip(), limit=limit,
        )
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "executions": enrich_executions(rows),
            "count": len(rows),
            # P1 2026-08-12 能力位：暂停/恢复/跳步/重试端点已装载。前端按此显隐
            # 操作按钮——旧后端（未重启）响应无 caps → 按钮不出现，绝不出「点了 404」。
            "caps": {"exec_ops": True},
        }

    @app.post("/api/workspace/chain-executions/{exec_id}/cancel")
    async def api_cancel_chain_execution(
        exec_id: str, request: Request, _=Depends(api_auth),
    ):
        """P47：取消运行中的工作链执行。"""
        _require_workflows(request)
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        ex = store.get_workflow_execution(exec_id)
        if not ex:
            raise HTTPException(404, tr(request, "err.ws.exec_record_not_found"))
        # P1 2026-08-12：paused 也可取消（暂停的链必须有收尾出口）
        if ex.get("status") not in ("running", "paused"):
            raise HTTPException(422, tr(request, "err.ws.only_cancel_running_chain"))
        body = {}
        if request.headers.get("content-type", "").startswith("application/json"):
            try:
                body = await request.json()
            except Exception:
                body = {}
        reason = str(body.get("reason") or "坐席手动取消").strip()
        agent_id, agent_name = _agent_from_request(request)
        ok = store.cancel_workflow_execution(exec_id)
        if not ok:
            raise HTTPException(422, tr(request, "msg_js_2058"))
        try:
            from src.integrations.shared.event_bus import get_event_bus
            import time as _t
            get_event_bus().publish("workflow_execution_cancelled", {
                "exec_id": exec_id,
                "conversation_id": ex.get("conversation_id"),
                "chain_id": ex.get("chain_id"),
                "chain_name": ex.get("chain_name", ""),
                "agent_id": agent_id,
                "agent_name": agent_name,
                "reason": reason,
                "ts": _t.time(),
            })
        except Exception:
            pass
        from src.inbox.workflow_monitor import enrich_execution
        _goal_chain_event(request, str(ex.get("conversation_id") or ""), "chain_cancelled",
                          str(ex.get("chain_name") or ex.get("chain_id") or ""))
        refreshed = store.get_workflow_execution(exec_id)
        return {
            "ok": True,
            "exec_id": exec_id,
            "execution": enrich_execution(refreshed or ex),
        }

    # ── P1 2026-08-12：执行操作补齐（暂停/恢复/跳步/重试）────────────────────
    # 四端点同构：状态守卫（422 带各自可读文案）→ store 专用方法 → 返回
    # 富化后的最新执行行（前端就地替换零二次拉取）。语义细节在 store 层注释
    # （resume 时钟停走 / skip 等价「该步刚完成」/ retry 清引擎重试预算）。

    def _exec_op_common(request: Request, exec_id: str):
        _require_workflows(request)
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        ex = store.get_workflow_execution(exec_id)
        if not ex:
            raise HTTPException(404, tr(request, "err.ws.exec_record_not_found"))
        return store, ex

    def _exec_op_result(store, exec_id: str, ex: Dict[str, Any]):
        from src.inbox.workflow_monitor import enrich_execution
        refreshed = store.get_workflow_execution(exec_id)
        return {"ok": True, "exec_id": exec_id,
                "execution": enrich_execution(refreshed or ex)}

    @app.post("/api/workspace/chain-executions/{exec_id}/pause")
    async def api_pause_chain_execution(
        exec_id: str, request: Request, _=Depends(api_auth),
    ):
        """暂停运行中的执行（时钟停走；恢复后按原节奏继续）。"""
        store, ex = _exec_op_common(request, exec_id)
        if ex.get("status") != "running":
            raise HTTPException(422, tr(request, "err.ws.exec_not_running"))
        if not store.pause_workflow_execution(exec_id):
            raise HTTPException(422, tr(request, "err.ws.exec_op_failed"))
        return _exec_op_result(store, exec_id, ex)

    @app.post("/api/workspace/chain-executions/{exec_id}/resume")
    async def api_resume_chain_execution(
        exec_id: str, request: Request, _=Depends(api_auth),
    ):
        """恢复暂停的执行。"""
        store, ex = _exec_op_common(request, exec_id)
        if ex.get("status") != "paused":
            raise HTTPException(422, tr(request, "err.ws.exec_not_paused"))
        if not store.resume_workflow_execution(exec_id):
            raise HTTPException(422, tr(request, "err.ws.exec_op_failed"))
        return _exec_op_result(store, exec_id, ex)

    @app.post("/api/workspace/chain-executions/{exec_id}/skip-step")
    async def api_skip_chain_step(
        exec_id: str, request: Request, _=Depends(api_auth),
    ):
        """跳过即将执行的一步（后续步骤保持原节奏；跳过最后一步＝整链完结）。"""
        store, ex = _exec_op_common(request, exec_id)
        if ex.get("status") != "running":
            raise HTTPException(422, tr(request, "err.ws.exec_not_running"))
        if not store.skip_workflow_step(exec_id):
            raise HTTPException(422, tr(request, "err.ws.exec_op_failed"))
        return _exec_op_result(store, exec_id, ex)

    @app.post("/api/workspace/chain-executions/{exec_id}/retry")
    async def api_retry_chain_execution(
        exec_id: str, request: Request, _=Depends(api_auth),
    ):
        """失败执行原步重试（引擎自动重试预算重新计满）。"""
        store, ex = _exec_op_common(request, exec_id)
        if ex.get("status") != "failed":
            raise HTTPException(422, tr(request, "err.ws.exec_not_failed"))
        if not store.retry_workflow_execution(exec_id):
            raise HTTPException(422, tr(request, "err.ws.exec_op_failed"))
        return _exec_op_result(store, exec_id, ex)

    @app.post("/api/workspace/conv/{conversation_id}/start-chain")
    async def api_conv_start_chain(conversation_id: str, request: Request, _=Depends(api_auth)):
        """AA1：为会话启动指定工作链。

        C1/C2：可选 ``goal_id``——坐席从「目标推荐」入口启动时带上，落执行 context
        （``workflow_executions.context_json``），为漏斗按目标分段/归因留数据地基；
        纯透传，无 goal_id 时行为零变化。
        """
        _require_workflows(request)
        body = await request.json()
        chain_id = str(body.get("chain_id") or "")
        store = _inbox_store(request)
        if not chain_id or store is None:
            return {"ok": False, "error": tr(request, "err.ws.missing_chain_id")}
        if store.has_running_chain(conversation_id, chain_id):
            return {"ok": False, "error": tr(request, "err.ws.chain_already_running")}
        ctx: Dict[str, Any] = {"agent": request.session.get("username") or ""}
        goal_id = str(body.get("goal_id") or "").strip()[:64]
        if goal_id:
            ctx["goal_id"] = goal_id
        exec_id = store.start_chain_execution(
            chain_id, conversation_id, ctx, schedule_first_step=True,
        )
        _ch = store.get_workflow_chain(chain_id) or {}
        _goal_chain_event(request, conversation_id, "chain_started",
                          str(_ch.get("name") or chain_id))
        return {"ok": True, "exec_id": exec_id, "conversation_id": conversation_id}
