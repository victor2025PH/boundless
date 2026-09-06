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
        """B1：一键导入出厂模板链（幂等——按固定 chain_id 判重，已存在一律跳过
        不覆盖）。实施93：body {pack: sales|guide|auto}——auto（缺省）按
        journey.flavor 选包；显式指定可跨风味补包。"""
        _require_workflows(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}
        body = {}
        if request.headers.get("content-type", "").startswith("application/json"):
            try:
                body = await request.json()
            except Exception:
                body = {}
        pack = str((body or {}).get("pack") or "auto").strip().lower()
        if pack not in ("sales", "guide", "auto"):
            pack = "auto"
        if pack == "auto":
            from src.inbox.journey_stage import resolve_journey_cfg
            cfg = getattr(
                getattr(request.app.state, "config_manager", None),
                "config", None) or {}
            pack = ("guide" if resolve_journey_cfg(cfg)["flavor"] == "guide"
                    else "sales")
        from src.inbox.workflow_starter import (
            ensure_guide_chains,
            ensure_starter_chains,
        )
        res = (ensure_guide_chains(store) if pack == "guide"
               else ensure_starter_chains(store))
        return {"ok": True, "pack": pack, **res}

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

    # ── 实施92：旅程阶段 / 成交事件 / 成交引擎预设 ────────────────────────

    @app.get("/api/workspace/conv/{conversation_id}/journey")
    async def api_conv_journey(conversation_id: str, request: Request):
        """旅程阶段 + 成交台账 + 「为什么是现在」证据（右栏 NBA 卡消费）。

        阶段展示不受 journey.enabled 闸（enabled 只闸自动推导与 stage_enter
        挂链），响应回带 enabled 供前端区分「自动推进中 / 仅手动」。

        实施92d 增量（有界建议卡的证据位）：
        - ``evidence``：{waiting_on: customer|us|"", wait_hours, stage_age_hours}
          ——「客户 26h 未回 / 客户已等 3h」的量化触发原因；
        - ``suggested_chain``：当前阶段有高置信推荐链（STAGE_CHAIN_REC）且
          未在途、7 天内没挂过 → {chain_id, name}；前端在无在途执行时展示
          一键启动。人工建议位，与 stage_enter 自动挂链（需成交引擎开）互补。
        """
        api_auth(request)
        _require_workflows(request)
        store = _inbox_store(request)
        from src.inbox.journey_stage import STAGE_ORDER, resolve_journey_cfg
        cfg = getattr(
            getattr(request.app.state, "config_manager", None), "config", None) or {}
        _jcfg = resolve_journey_cfg(cfg)
        if store is None:
            return {"ok": True, "stage": "", "ts": 0, "src": "", "deals": [],
                    "stages": STAGE_ORDER, "evidence": {}, "suggested_chain": None,
                    "flavor": _jcfg["flavor"],
                    "journey_enabled": _jcfg["enabled"]}
        st = store.get_journey_stage(conversation_id)
        import time as _t
        now = _t.time()
        evidence: Dict[str, Any] = {}
        try:
            last = (store.last_message_dirs([conversation_id])
                    or {}).get(conversation_id) or {}
            l_ts = float(last.get("ts") or 0)
            if l_ts > 0:
                evidence["waiting_on"] = (
                    "customer" if str(last.get("direction")) == "out" else "us")
                evidence["wait_hours"] = round(max(0.0, now - l_ts) / 3600, 1)
            if float(st.get("ts") or 0) > 0 and st.get("stage"):
                evidence["stage_age_hours"] = round(
                    max(0.0, now - float(st["ts"])) / 3600, 1)
        except Exception:
            evidence = {}
        suggested = None
        try:
            from src.inbox.workflow_starter import stage_chain_rec_for
            rec_id = stage_chain_rec_for(_jcfg["flavor"]).get(
                str(st.get("stage") or ""))
            if rec_id:
                chain = store.get_workflow_chain(rec_id)
                if (chain and chain.get("enabled")
                        and not store.has_running_chain(conversation_id, rec_id)
                        and not store.chain_started_since(
                            conversation_id, rec_id, now - 7 * 86400)):
                    suggested = {"chain_id": rec_id,
                                 "name": str(chain.get("name") or rec_id)}
        except Exception:
            suggested = None
        return {
            "ok": True, **st,
            "deals": store.list_deal_events(conversation_id, limit=20),
            "stages": STAGE_ORDER,
            "evidence": evidence,
            "suggested_chain": suggested,
            "flavor": _jcfg["flavor"],
            "journey_enabled": _jcfg["enabled"],
        }

    @app.post("/api/workspace/conv/{conversation_id}/journey/stage")
    async def api_conv_journey_stage_set(
        conversation_id: str, request: Request, _=Depends(api_auth),
    ):
        """坐席显式设置旅程阶段（任意方向；src=manual，自动推导不再覆盖）。"""
        _require_workflows(request)
        body = await request.json()
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        from src.inbox.journey_stage import set_stage_manual
        agent_id, agent_name = _agent_from_request(request)
        res = set_stage_manual(
            store, conversation_id, str(body.get("stage") or ""),
            by=agent_id or agent_name)
        if not res.get("ok"):
            raise HTTPException(422, tr(request, "err.ws.bad_journey_stage"))
        return res

    @app.post("/api/workspace/conv/{conversation_id}/deal")
    async def api_conv_mark_deal(
        conversation_id: str, request: Request, _=Depends(api_auth),
    ):
        """标记成交（P0-3）：{amount?, currency?, note?} → 台账 + 阶段推进
        deal/repeat。金额可空（先记一笔，金额事后补录也行——别让「想不起
        金额」挡住记账）。"""
        _require_workflows(request)
        body = await request.json()
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        try:
            amount = max(0.0, float(body.get("amount") or 0))
        except (TypeError, ValueError):
            amount = 0.0
        from src.inbox.journey_stage import record_deal
        agent_id, agent_name = _agent_from_request(request)
        res = record_deal(
            store, conversation_id, amount=amount,
            currency=str(body.get("currency") or "")[:8],
            note=str(body.get("note") or "")[:200],
            recorded_by=agent_id or agent_name, source="manual")
        if not res.get("ok"):
            raise HTTPException(422, tr(request, "err.ws.exec_op_failed"))
        _goal_chain_event(request, conversation_id, "deal_marked",
                          str(body.get("note") or ""))
        return res

    @app.post("/api/workspace/conv/{conversation_id}/deal/{deal_id}/revoke")
    async def api_conv_revoke_deal(
        conversation_id: str, deal_id: int, request: Request, _=Depends(api_auth),
    ):
        """撤销成交（误标回退：软删台账 + 阶段按事件 prev_stage 还原）。"""
        _require_workflows(request)
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        from src.inbox.journey_stage import revoke_deal
        res = revoke_deal(store, conversation_id, deal_id)
        if not res.get("ok"):
            if res.get("error") == "not_found":
                raise HTTPException(404, tr(request, "err.ws.deal_not_found"))
            raise HTTPException(422, tr(request, "err.ws.exec_op_failed"))
        return res

    # 实施92b P1-7：批量挂链——收件箱筛选级的「给这批客户启动 X 链」。
    # 刻意默认 dry_run（裸 POST 只出预览绝不群发）；每次上限 50；
    # 双重防重：在途链跳过 + 7 天内挂过同链跳过（防连点/隔天重复群发）。
    _BULK_REFIRE_GUARD_SEC = 7 * 86400

    @app.post("/api/workspace/workflow-chains/{chain_id}/bulk-start")
    async def api_bulk_start_chain(
        chain_id: str, request: Request, _=Depends(api_auth),
    ):
        """批量启动：{silent_days_min?: float, stage?: str,
        conversation_ids?: [..], limit?: int, dry_run?: bool=true}。
        silent_days_min=沉默≥N天的私聊；stage=处于某旅程阶段；
        conversation_ids（实施92e）=显式会话清单——收件箱「筛选即选择」入口
        直投当前筛选结果（服务端仍做私聊校验+在途/7天防重，前端清单只是候选）。
        多个筛选并集。dry_run 回候选数+样本，落地调用需显式 dry_run=false。"""
        _require_workflows(request)
        body = await request.json()
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        chain = store.get_workflow_chain(chain_id)
        if not chain:
            raise HTTPException(404, tr(request, "err.ws.chain_not_found"))
        if not chain.get("enabled"):
            raise HTTPException(422, tr(request, "err.ws.chain_disabled"))
        try:
            silent_days = float(body.get("silent_days_min") or 0)
        except (TypeError, ValueError):
            silent_days = 0.0
        stage = str(body.get("stage") or "").strip().lower()
        conv_ids = body.get("conversation_ids")
        if not isinstance(conv_ids, list):
            conv_ids = []
        conv_ids = [str(c).strip() for c in conv_ids[:100] if str(c or "").strip()]
        if silent_days <= 0 and not stage and not conv_ids:
            raise HTTPException(422, tr(request, "err.ws.bulk_filter_required"))
        try:
            limit = max(1, min(int(body.get("limit") or 20), 50))
        except (TypeError, ValueError):
            limit = 20
        import time as _t
        now = _t.time()
        seen: set = set()
        candidates: list = []
        rows: list = []
        if silent_days > 0:
            rows.extend(store.list_silent_private_conversations(
                silent_days, limit=200))
        if stage:
            rows.extend(store.list_conversations_at_journey_stage(
                stage, entered_since=0, limit=200))
        # 显式清单：真实存在且为私聊才进候选（前端清单只是提名，
        # 服务端校验兜底——幽灵 id / 群聊一律静默剔除）
        for cid in conv_ids:
            try:
                conv = store.get_conversation(cid)
            except Exception:
                conv = None
            if conv and str(conv.get("chat_type") or "private") == "private":
                rows.append({"conversation_id": cid})
        # #209 预览三数：matched=符合筛选的去重会话 / skipped=在途或 7 天内挂过 /
        # eligible=可挂；candidates 仍按 limit 截断（真落地上限不变），但计数扫全量
        # ——弹层要能如实告诉坐席「符合 N · 将挂 min(N,50) · 跳过 M」。
        matched = 0
        skipped = 0
        eligible = 0
        for r in rows:
            cid = str(r.get("conversation_id") or "")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            matched += 1
            try:
                if store.has_running_chain(cid, chain_id):
                    skipped += 1
                    continue
                if store.chain_started_since(
                        cid, chain_id, now - _BULK_REFIRE_GUARD_SEC):
                    skipped += 1
                    continue
            except Exception:
                skipped += 1
                continue
            eligible += 1
            if len(candidates) < limit:
                candidates.append(cid)
        dry = bool(body.get("dry_run", True))
        if dry:
            sample = []
            for cid in candidates[:10]:
                conv = store.get_conversation(cid) or {}
                sample.append({"conversation_id": cid,
                               "display_name": str(conv.get("display_name") or "")})
            return {"ok": True, "dry_run": True,
                    "candidates": len(candidates), "sample": sample,
                    "matched": matched, "eligible": eligible, "skipped": skipped,
                    "limit": limit}
        agent = str(request.session.get("username") or "")
        started = 0
        exec_ids: list = []
        for cid in candidates:
            try:
                ex_id = store.start_chain_execution(
                    chain_id, cid, {"bulk": True, "agent": agent},
                    schedule_first_step=True)
                started += 1
                if ex_id:
                    exec_ids.append(str(ex_id))
                _goal_chain_event(request, cid, "chain_started",
                                  str(chain.get("name") or chain_id))
            except Exception:
                logger.debug("bulk-start 单条失败（已跳过）%s", cid,
                             exc_info=True)
        # exec_ids 供前端「已挂 N 个（可撤销）」逐条 POST chain-executions/{id}/cancel
        return {"ok": True, "dry_run": False, "started": started,
                "candidates": len(candidates), "exec_ids": exec_ids,
                "matched": matched, "eligible": eligible, "skipped": skipped}

    @app.get("/api/workspace/journey-funnel")
    async def api_journey_funnel(request: Request, days: int = 14):
        """实施92c/93：旅程阶段分布 + 窗口成交汇总 + CTA 引导/点击读数。"""
        api_auth(request)
        _require_workflows(request)
        store = _inbox_store(request)
        from src.inbox.journey_stage import STAGE_ORDER, resolve_journey_cfg
        cfg = getattr(
            getattr(request.app.state, "config_manager", None), "config", None) or {}
        flavor = resolve_journey_cfg(cfg)["flavor"]
        d = max(1, min(90, int(days or 14)))
        if store is None:
            return {"ok": True, "order": STAGE_ORDER, "stages": {},
                    "flavor": flavor,
                    "deals": {"n": 0, "amount": 0, "days": d},
                    "cta": {"links": 0, "clicked": 0, "click_rate": None,
                            "by_target": []}}
        import time as _t
        since = _t.time() - d * 86400
        evs = store.deal_events_between(since)
        amount = round(sum(float(e.get("amount") or 0) for e in evs), 2)
        return {
            "ok": True,
            "order": STAGE_ORDER,
            "flavor": flavor,
            "stages": store.journey_stage_histogram(),
            "deals": {"n": len(evs), "amount": amount, "days": d},
            "cta": store.cta_stats(since_ts=since),
        }

    # ── 实施93：CTA 转化目标库 / 铸链 / 公开跳转 ─────────────────────────────

    @app.get("/api/workspace/cta-targets")
    async def api_cta_targets_list(request: Request):
        api_auth(request)
        _require_workflows(request)
        store = _inbox_store(request)
        cfg = getattr(
            getattr(request.app.state, "config_manager", None), "config", None) or {}
        from src.inbox.cta_links import resolve_cta_cfg
        return {"ok": True,
                "targets": store.list_cta_targets() if store else [],
                "public_base": resolve_cta_cfg(cfg)["public_base"]}

    @app.post("/api/workspace/cta-public-base")
    async def api_cta_public_base_set(request: Request, _=Depends(api_auth)):
        """#132：界面内配置短链公网基址（写 overlay ``inbox.cta.public_base``）。

        此前该键只有红字提示裸奔在 SOP 页（连反代术语一起怼给运营）——运营
        没有任何界面动作可做。校验只收 http(s) 完整地址；尾斜杠归一。
        """
        _require_workflows(request)
        body = await request.json()
        base = str(body.get("public_base") or "").strip().rstrip("/")
        import re as _re
        if not _re.match(r"^https?://[^\s/]+", base, _re.IGNORECASE):
            raise HTTPException(
                422, tr(request, "err.ws.field_required", field="public_base"))
        cm = getattr(request.app.state, "config_manager", None)
        if cm is None or not hasattr(cm, "set_overlay_flag"):
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        ok, msg = cm.set_overlay_flag("inbox.cta.public_base", base)
        if not ok:
            return {"ok": False, "message": str(msg or "")[:200]}
        return {"ok": True, "public_base": base}

    @app.post("/api/workspace/cta-targets")
    async def api_cta_targets_upsert(request: Request, _=Depends(api_auth)):
        """新建/更新转化目标：{target_id?, name, url, kind?, utm?, enabled?}。"""
        _require_workflows(request)
        body = await request.json()
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        name = str(body.get("name") or "").strip()
        url = str(body.get("url") or "").strip()
        if not body.get("target_id"):
            if not name or not url.lower().startswith(("http://", "https://")):
                raise HTTPException(
                    422, tr(request, "err.ws.field_required", field="name/url"))
        tid = store.upsert_cta_target(body)
        return {"ok": True, "target_id": tid}

    @app.delete("/api/workspace/cta-targets/{target_id}")
    async def api_cta_targets_delete(
        target_id: str, request: Request, _=Depends(api_auth),
    ):
        _require_workflows(request)
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        return {"ok": store.delete_cta_target(target_id)}

    @app.post("/api/workspace/conv/{conversation_id}/cta-link")
    async def api_conv_mint_cta_link(
        conversation_id: str, request: Request, _=Depends(api_auth),
    ):
        """为会话铸造（或复用）追踪短链：{target_id}。铸链即把旅程推进到
        「已引导」。public_base 未配置 → 422 明示（绝不发内网地址）。"""
        _require_workflows(request)
        body = await request.json()
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        cfg = getattr(
            getattr(request.app.state, "config_manager", None), "config", None) or {}
        from src.inbox.cta_links import mint_link
        res = mint_link(store, cfg, conversation_id,
                        str(body.get("target_id") or ""))
        if not res.get("ok"):
            if res.get("error") == "no_public_base":
                raise HTTPException(422, tr(request, "err.ws.cta_no_base"))
            raise HTTPException(422, tr(request, "err.ws.cta_target_invalid"))
        return res

    @app.get("/r/{token}")
    async def public_cta_redirect(token: str, request: Request):
        """公开跳转（无鉴权——客户点击）。只做 302 与计数：无数据回显、
        token 不可枚举、点击计数封顶；未知 token → 404 纯文本。
        刻意不闸 workflows flag：链接已发到客户手里，模块开关不该把它变死链。"""
        import re as _re
        if not _re.fullmatch(r"[A-Za-z0-9_-]{4,64}", str(token or "")):
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse("not found", status_code=404)
        store = _inbox_store(request)
        cfg = getattr(
            getattr(request.app.state, "config_manager", None), "config", None) or {}
        url = None
        if store is not None:
            from src.inbox.cta_links import handle_click
            url = handle_click(store, cfg, token)
        if not url:
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse("not found", status_code=404)
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url, status_code=302)

    @app.post("/api/cta/convert")
    async def public_cta_convert(request: Request):
        """实施93b 深转化回传（落地页/网站/服务商服务端调用，无会话鉴权）。

        body: ``{token 或 conversation_id, kind?, amount?, currency?, ref?, note?}``
        安全对齐 monetize webhook（S6）：必须配置 ``inbox.cta.webhook_secret``
        并校验 ``X-CTA-Secret`` 头（恒定时间比较）；未配置直接拒绝。
        幂等：``ref`` 已记账（含已撤销）→ 跳过。amount>0 走 record_deal
        （营收台账+阶段 deal/repeat）；amount=0 视作轻转化只推阶段。
        刻意不闸 workflows flag：链接/埋点已在站外，模块开关不该断回传。"""
        import hmac as _hmac
        cfg = getattr(
            getattr(request.app.state, "config_manager", None), "config", None) or {}
        from src.inbox.cta_links import resolve_cta_cfg
        secret = str(resolve_cta_cfg(cfg).get("webhook_secret") or "")
        if not secret:
            return {"ok": False, "reason": "webhook_secret_not_configured"}
        got = request.headers.get("x-cta-secret") or ""
        if not _hmac.compare_digest(got, secret):
            return {"ok": False, "reason": "unauthorized"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "reason": "inbox_not_ready"}
        # 会话解析：token（短链跳转时带给落地页）优先，其次直给 conversation_id
        cid = ""
        token = str(body.get("token") or "").strip()
        if token:
            link = store.get_cta_link(token) if hasattr(
                store, "get_cta_link") else None
            cid = str((link or {}).get("conversation_id") or "")
        if not cid:
            cid = str(body.get("conversation_id") or "").strip()
        if not cid:
            return {"ok": False, "reason": "unknown_token"}
        ref = str(body.get("ref") or "").strip()
        if ref:
            dup = store.find_deal_event_by_ref(ref)
            if dup:
                return {"ok": True, "deduped": True,
                        "deal_id": int(dup.get("id") or 0)}
        kind = str(body.get("kind") or "convert").strip()[:24] or "convert"
        try:
            amount = float(body.get("amount") or 0)
        except Exception:
            amount = 0.0
        from src.inbox import journey_stage as _js
        if amount > 0:
            res = _js.record_deal(
                store, cid, amount=amount,
                currency=str(body.get("currency") or "")[:8],
                note=(f"cta:{kind} " + str(body.get("note") or "")).strip()[:200],
                recorded_by="cta_webhook", source="cta_webhook", ref=ref)
            return {"ok": bool(res.get("ok")), "deal_id": res.get("deal_id"),
                    "stage": res.get("stage")}
        # 无金额：轻转化（注册/安装/进群）——推进阶段；带 ref 时也落一笔
        # 0 元台账行（幂等锚点 + 漏斗可见），不带 ref 只推阶段。
        if ref:
            res = _js.record_deal(
                store, cid, amount=0.0,
                note=f"cta:{kind}"[:200],
                recorded_by="cta_webhook", source="cta_webhook", ref=ref)
            return {"ok": bool(res.get("ok")), "deal_id": res.get("deal_id"),
                    "stage": res.get("stage")}
        res = _js.record_conversion(store, cid, source=f"cta_webhook:{kind}")
        return {"ok": bool(res.get("ok")), "stage": res.get("stage"),
                "advanced": bool(res.get("advanced"))}

    @app.get("/api/workspace/workflows/deal-engine")
    async def api_deal_engine_status(request: Request):
        """成交引擎预设状态（workflows 页卡片消费）。"""
        api_auth(request)
        _require_workflows(request)
        store = _inbox_store(request)
        cfg = getattr(
            getattr(request.app.state, "config_manager", None), "config", None) or {}
        from src.inbox.workflow_starter import deal_engine_status
        return {"ok": True, **deal_engine_status(store, cfg)}

    @app.post("/api/workspace/workflows/deal-engine")
    async def api_deal_engine_apply(request: Request, _=Depends(api_auth)):
        """一键开/关成交引擎：{enable: bool, auto_send?: bool}。写 overlay
        （保注释）+ 接/摘种子链 stage_enter 触发 + 开启时回填存量会话阶段。
        ``auto_send``（实施92b）缺省不碰；显式 true/false 才切「话术步自动
        拟稿自动发」档（auto_advance 总闸 + 两条预设链 exec_mode）。"""
        _require_workflows(request)
        body = await request.json()
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        cm = getattr(request.app.state, "config_manager", None)
        from src.inbox.workflow_starter import apply_deal_engine
        _as = body.get("auto_send")
        res = apply_deal_engine(
            store, cm, bool(body.get("enable")),
            auto_send=None if _as is None else bool(_as))
        return res

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
