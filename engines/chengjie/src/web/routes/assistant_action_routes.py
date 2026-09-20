# -*- coding: utf-8 -*-
"""小智动作注册表路由（实施58 P1，2026-08-23）——薄包装。

契约（与 shared/assistant 前端 & 未来智能体层钉死）：
  GET  /api/assistant/actions          → {ok, levels, actions:[catalog]}
  POST /api/assistant/act              → 无 token：L0 执行 / L1 回 goto /
                                         L2 回 {need_confirm, token, diff}
                                         带 token：核销后应用 → {applied, undo_id}
  POST /api/assistant/act/undo         → 按快照回写 → {restored}

护栏：assistant.enabled 总闸（与问答同灰度）；坐席角色仅 L0/L1
（allowed_levels_for_role）；每 uid 0.6s 节流；确认 token 单次核销 +
uid 绑定；全部写经 actions.apply_plan（overlay+审计+undo 快照）。
set_reply_delay 应用后对活体 AutosendWorker best-effort 热更
（merged_delay_block → apply_deliver_delay，与 reply-settings 页同链），
hot_applied 如实回传，绝不谎报生效。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

from fastapi import HTTPException, Request

from src.web.web_i18n import tr
from src.assistant import actions as act
from src.assistant import runner_pairing
from src.assistant import pc_trust
from src.web.routes.assistant_routes import _assistant_cfg, _session_user

logger = logging.getLogger(__name__)

_ACT_MIN_INTERVAL_SEC = 0.6

_DELAY_PREFIX = "inbox.l2_autosend.deliver_delay."
# 与 reply_settings_routes._HUMANIZE_FLAGS 同表（改一处必改另一处，
# 门禁 test_humanize_flags_mapping_synced 钉住）
_HUMANIZE_FLAGS = {
    "inbox.l2_autosend.mark_read_before_reply": "mark_read",
    "inbox.l2_autosend.typing_indicator": "typing",
}


def _nav_whitelist() -> set:
    """nav_schema 提取（多键名防御），失败回落保守核心集。"""
    paths: set = set(act.CORE_NAV_PATHS)
    try:
        from src.web.nav_schema import NAV_ITEMS  # type: ignore

        items = NAV_ITEMS.values() if isinstance(NAV_ITEMS, dict) else NAV_ITEMS
        for it in items:
            if not isinstance(it, dict):
                continue
            for k in ("path", "href", "url", "to"):
                v = it.get(k)
                if isinstance(v, str) and v.startswith("/"):
                    paths.add(v.split("?", 1)[0])
    except Exception:
        logger.debug("nav_schema 提取失败，goto 白名单用核心集", exc_info=True)
    return paths


def register_assistant_action_routes(app, ctx) -> None:
    _api_auth = ctx.api_auth
    config_manager = ctx.config_manager
    telegram_client = getattr(ctx, "telegram_client", None)
    _last_act: Dict[str, float] = {}

    def _cfg() -> Dict[str, Any]:
        c = getattr(config_manager, "config", None)
        return c if isinstance(c, dict) else {}

    def _run_pc_plan(r: Dict[str, Any], cfg: Dict[str, Any],
                     actor: str) -> Dict[str, Any]:
        """执行一个 runner plan：``click_target`` 走 VLM grounding 编排（截图→定位
        →click_point，实施91 P3-B，坐标服务端导出非 LLM）；其余走单次 inspect。
        vision.enabled 未开时 click_target 回 {ok:False,error:vision_disabled}。
        返回 runner_client 风格 dict（两条 apply 分支共用，编排只此一处）。"""
        from src.assistant import runner_client
        machine = str(r.get("machine") or "")
        tool = str(r.get("tool") or "")
        args = r.get("args") or {}
        if tool == "click_target":
            pc_cfg = _assistant_cfg(cfg).get("pc_runner") or {}
            if not (pc_cfg.get("vision") or {}).get("enabled"):
                return {"ok": False, "error": "vision_disabled"}
            from src.assistant import pc_click_target as pct
            vcfg = cfg.get("vision") if isinstance(cfg, dict) else None
            return pct.resolve_and_click(
                machine, str(args.get("window") or ""),
                str(args.get("target") or ""), cfg, actor,
                vision_cfg=vcfg, global_vision=vcfg)
        return runner_client.inspect(machine, tool, args, cfg, actor)

    def _enabled_or_403(request: Request) -> None:
        if not _assistant_cfg(_cfg()).get("enabled"):
            raise HTTPException(403, tr(request, "asb.err.disabled"))

    def _mobile_guard(request: Request) -> str:
        """手机配对会话逐调用校验（实施58 P4）：被踢/过期/实例重启后注册表
        为空 → 立即 401（页面出重扫提示）。返回审计后缀（''｜'@mobile'）。"""
        msid = str(request.session.get("xz_mobile") or "")
        if not msid:
            return ""
        from src.assistant import pairing

        if not pairing.mobile_ok(msid):
            raise HTTPException(401, tr(request, "asb.pair.revoked"))
        return "@mobile"

    def _throttle(uid: str, request: Request) -> None:
        now = time.time()
        last = _last_act.get(uid, 0.0)
        if now - last < _ACT_MIN_INTERVAL_SEC:
            raise HTTPException(429, tr(request, "asb.err.rate_limited"))
        _last_act[uid] = now
        if len(_last_act) > 500:  # 防长期进程 uid 膨胀
            _last_act.clear()
            _last_act[uid] = now

    def _plan_err(request: Request, plan: Dict[str, Any]) -> HTTPException:
        err = plan.get("error")
        detail = str(plan.get("detail") or "")
        if err == "unknown_action":
            return HTTPException(400, tr(request, "asb.act.unknown"))
        if err == "path_not_allowed":
            return HTTPException(400, tr(request, "asb.act.path_na"))
        return HTTPException(
            400, tr(request, "asb.act.bad_params", detail=detail))

    def _try_hot_apply(plan: Dict[str, Any]) -> bool:
        """hot=="worker" 键的活体 worker 热更（best-effort，失败如实报 False）。

        按写入路径分派（与 reply-settings 保存路由同链）：deliver_delay 族 →
        merged_delay_block + apply_deliver_delay（set_reply_delay /
        toggle_adaptive_delay 同吃）；拟人链开关 → apply_humanize_flags
        （set_typing_indicator / set_mark_read）。其余按注册表 hot 语义回答。
        """
        diff = list(plan.get("diff") or [])
        clean = {d["path"]: d["new"] for d in diff}
        worker = getattr(app.state, "autosend_worker", None)
        delay_paths = [p for p in clean if p.startswith(_DELAY_PREFIX)]
        if delay_paths:
            try:
                from src.inbox.reply_pacing_settings import merged_delay_block

                if worker is not None and hasattr(worker,
                                                  "apply_deliver_delay"):
                    worker.apply_deliver_delay(
                        merged_delay_block(_cfg(), clean))
                    return True
            except Exception:
                logger.debug("deliver_delay 热更失败（按重启口径）",
                             exc_info=True)
            return False
        flag_kw = {_HUMANIZE_FLAGS[p]: bool(v) for p, v in clean.items()
                   if p in _HUMANIZE_FLAGS}
        if flag_kw:
            try:
                if worker is not None and hasattr(worker,
                                                  "apply_humanize_flags"):
                    worker.apply_humanize_flags(**flag_kw)
                    return True
            except Exception:
                logger.debug("拟人链开关热更失败（按重启口径）", exc_info=True)
            return False
        return bool(plan.get("hot") is True)

    # ------------------------------------------------------------ catalog
    @app.get("/api/assistant/actions")
    async def api_assistant_actions(request: Request):
        _api_auth(request)
        _enabled_or_403(request)
        _uid, _name, role = _session_user(request)
        lang = str(request.query_params.get("lang") or "zh")
        return {"ok": True,
                "levels": list(act.allowed_levels_for_role(role)),
                "actions": act.catalog(role, lang)}

    # ------------------------------------------------------------ act
    @app.post("/api/assistant/act")
    async def api_assistant_act(request: Request):
        _api_auth(request)
        _enabled_or_403(request)
        src_tag = _mobile_guard(request)
        uid, uname, role = _session_user(request)
        _throttle(uid, request)
        data = await request.json()
        token = str(data.get("confirm_token") or "").strip()

        # ── 二段：核销确认并应用 ──
        if token:
            plan = act.pop_confirm(token, uid)
            if not plan:
                raise HTTPException(409, tr(request, "asb.act.confirm_expired"))
            # 实施91 P1-2：runner 写动作（pc_launch/pc_focus）——确认后 apply=调
            # 受控机 runner（不写 config）。复用同一确认/审计骨架，绝不开第二条
            # 写通道。三重闸：总闸 enabled + 机器白名单（plan 期已校验）+ runner
            # 侧 PC_RUNNER_ACTIONS/kill-switch/app 白名单。
            if plan.get("kind") == "runner":
                pc_cfg = _assistant_cfg(_cfg()).get("pc_runner") or {}
                if not pc_cfg.get("enabled"):
                    raise HTTPException(403, tr(request, "asb.pc.disabled"))

                r = plan["runner"]
                actor = ((uname or uid) + src_tag
                         + runner_pairing.actor_suffix(r["machine"]))
                res = _run_pc_plan(r, _cfg(), actor)
                act._audit(config_manager, {
                    "op": "pc_act", "actor": actor, "machine": r["machine"],
                    "tool": r["tool"], "ok": res.get("ok"),
                    "via": res.get("via"), "error": res.get("error")})
                if not res.get("ok"):
                    err = str(res.get("error") or "")
                    if err == "vision_disabled":
                        raise HTTPException(
                            403, tr(request, "asb.pc.vision_disabled"))
                    if err == "not_located":
                        raise HTTPException(422, tr(
                            request, "asb.pc.not_located",
                            target=str(res.get("target") or "")))
                    raise HTTPException(502, tr(
                        request, "asb.pc.act_failed", detail=err))
                note = tr(request, "asb.pc.act_done")
                if r["tool"] == "click_target":
                    note = tr(request, "asb.pc.click_target_done",
                              target=str(res.get("target") or ""))
                if r["tool"] == "run_command":
                    # 命令回显：runner 侧已消毒并按 4000/流截断——这里整段回显
                    # （不再二次截到 300），坐席要看完整命令结果；stdout/stderr
                    # 都在时都给（stderr 带技术前缀），合并再兜一层 4000 防超大 note。
                    note = tr(request, "asb.pc.cmd_done",
                              code=res.get("returncode"))
                    parts = []
                    so = str(res.get("stdout") or "")
                    se = str(res.get("stderr") or "")
                    if so:
                        parts.append(so)
                    if se:
                        parts.append("[stderr] " + se)
                    if parts:
                        note = note + "\n" + ("\n".join(parts))[:4000]
                return {"ok": True, "action": plan.get("action"),
                        "machine": r["machine"], "tool": r["tool"],
                        "result": res, "runner_note": note}
            actor = (uname or uid) + src_tag
            res = act.apply_plan(plan, config_manager, actor)
            if not res.get("ok"):
                first = (res.get("failed") or [{}])[0]
                raise HTTPException(500, tr(
                    request, "asb.act.apply_failed",
                    detail=str(first.get("msg") or "")))
            hot_applied = _try_hot_apply(plan)
            out = {"ok": True, "action": plan.get("action"),
                   "applied": res.get("applied"),
                   "failed": res.get("failed"),
                   "undo_id": res.get("undo_id"),
                   "hot_applied": hot_applied}
            # 存量档位对齐（set_automation_mode 专属）：只写 overlay 的档位
            # 切换对被 bootstrap/坐席固化过显式档位的会话是空话——切档必须
            # 连存量一起同步，否则「关闭全自动」后老会话照常 A 线直发
            # （2026-08-30 实录）。对齐明细挂进 undo 快照，撤销一并回。
            if plan.get("action") == act.ALIGN_ACTION:
                try:
                    store = getattr(app.state, "inbox_store", None)
                    mode = str((plan.get("clean_params") or {})
                               .get("mode") or "")
                    if store is not None and mode:
                        ares = act.align_conversation_modes(
                            store, mode, cm=config_manager, actor=actor)
                        act.attach_undo_conversations(
                            str(res.get("undo_id") or ""), ares["olds"])
                        out["aligned"] = int(ares["changed"])
                        out["aligned_groups"] = int(ares["groups"])
                except Exception:
                    logger.warning("存量档位对齐失败（overlay 已写、存量未同步）",
                                   exc_info=True)
                    out["aligned_error"] = True
            return out

        # ── 一段：出计划 ──
        # runner 受控机白名单从配置载入（幂等，热改 machines 即生效）——
        # plan_action 对 pc_inspect 会查注册表，必须先载入（实施91）。
        runner_pairing.load_machines(
            (_assistant_cfg(_cfg()).get("pc_runner") or {}).get("machines"))
        plan = act.plan_action(
            str(data.get("action") or ""),
            data.get("params") if isinstance(data.get("params"), dict) else {},
            _cfg(),
            nav_paths=_nav_whitelist(),
            lang="en" if str(data.get("lang") or "").lower().startswith("en")
            else "zh",
        )
        if not plan.get("ok"):
            raise _plan_err(request, plan)
        if plan["level"] not in act.allowed_levels_for_role(role):
            raise HTTPException(403, tr(request, "asb.act.forbidden"))

        if plan["kind"] == "query":
            lang = "en" if str(data.get("lang") or "").lower().startswith(
                "en") else "zh"
            if plan["action"] == "diagnose_autoreply":
                from src.inbox.autoreply_factors import collect_autoreply_health

                return {"ok": True, "action": plan["action"],
                        "result": collect_autoreply_health(_cfg())}
            if plan["action"] == "query_ai_status":
                snap = None
                try:
                    ai = getattr(app.state, "ai_client", None)
                    if ai is not None and hasattr(ai, "degradation_snapshot"):
                        snap = ai.degradation_snapshot()
                except Exception:
                    snap = None
                return {"ok": True, "action": plan["action"],
                        "result": {"verdict":
                                   act.summarize_ai_status(snap, lang)}}
            if plan["action"] == "query_reply_budget":
                n_rows = n_ex = n_rel = 0
                guard = None
                try:
                    from src.inbox.peer_bot_guard import (budget_flags,
                                                          parse_cfg,
                                                          today_key)
                    from src.web.routes.unified_inbox_services import (
                        _inbox_store,
                    )

                    guard = parse_cfg(_cfg())
                    store = _inbox_store(request)
                    rows = []
                    if store is not None and hasattr(
                            store, "list_reply_budget_today"):
                        rows = store.list_reply_budget_today(
                            today_key(), limit=200) or []
                    n_rows = len(rows)
                    for r in rows:
                        fl = budget_flags(r.get("used"), r.get("relieved"),
                                          guard)
                        if fl.get("exhausted") or fl.get("hard_stopped"):
                            n_ex += 1
                        if r.get("relieved"):
                            n_rel += 1
                except Exception:
                    logger.debug("query_reply_budget 读数失败（如实降级）",
                                 exc_info=True)
                return {"ok": True, "action": plan["action"],
                        "result": {"verdict": act.summarize_reply_budget(
                            n_rows, n_ex, n_rel, guard, lang)}}
            if plan["action"] == "query_platform_health":
                dump = None
                try:
                    from src.integrations.platform_session_health import (
                        get_platform_session_health,
                    )

                    dump = get_platform_session_health().dump()
                except Exception:
                    dump = None
                return {"ok": True, "action": plan["action"],
                        "result": {"verdict":
                                   act.summarize_platform_sessions(dump,
                                                                   lang)}}
            if plan["action"] == "query_version":
                ver = ""
                try:
                    from src.utils.app_identity import app_version

                    ver = str(app_version() or "").strip()
                except Exception:
                    logger.debug("query_version 版本读取失败（如实降级）",
                                 exc_info=True)
                return {"ok": True, "action": plan["action"],
                        "result": {"verdict":
                                   act.summarize_version(ver, lang)}}
            raise HTTPException(400, tr(request, "asb.act.unknown"))

        if plan["kind"] == "nav":
            return {"ok": True, "action": plan["action"],
                    "goto": plan.get("goto")}

        if plan["kind"] == "ui":
            # DOM 动作总线（实施88 P0）：与 nav 同信任级（L1 揭示类手势），
            # 执行在前端；这里回经注册表换出的 sel/gesture（绝不回传 LLM
            # 生成的选择器——plan_action 已按 ui_anchors 白名单校验）。
            return {"ok": True, "action": plan["action"],
                    "ui": plan.get("ui")}

        # runner 直执行分支（实施91）：L0 只读永远直执行；L2 动作面在**信任档**下
        # （trust.enabled + 该机受信 + 非 always_confirm）也免确认卡直接执行（P2-C）
        # ——run_command 等 always_confirm 永不免，落到下面 L2 确认卡分支。
        _pc_cfg = _assistant_cfg(_cfg()).get("pc_runner") or {}
        _rspec = act.ACTIONS.get(plan.get("action")) or {}
        _rmachine = str((plan.get("runner") or {}).get("machine") or "")
        _trusted_auto = act.runner_auto_execute(
            kind=plan["kind"], level=plan["level"],
            always_confirm=bool(_rspec.get("always_confirm")),
            trust_enabled=bool((_pc_cfg.get("trust") or {}).get("enabled")),
            is_trusted=pc_trust.is_trusted(_rmachine))
        if plan["kind"] == "runner" and (plan["level"] == "L0" or _trusted_auto):
            # 三重闸——总闸 assistant.pc_runner.enabled（默认关）+ 机器白名单
            # （plan_action 已校验）+ runner 侧 token/PC_RUNNER_ACTIONS/kill-switch。
            if not _pc_cfg.get("enabled"):
                raise HTTPException(403, tr(request, "asb.pc.disabled"))

            r = plan["runner"]
            actor = (uname or uid) + runner_pairing.actor_suffix(r["machine"])
            res = _run_pc_plan(r, _cfg(), actor)
            act._audit(config_manager, {
                "op": ("pc_inspect" if plan["level"] == "L0" else "pc_act"),
                "actor": actor, "machine": r["machine"], "tool": r["tool"],
                "trusted": bool(_trusted_auto), "via": res.get("via"),
                "ok": res.get("ok"), "error": res.get("error")})
            # L2 信任档直执行失败=如实 502（只读失败仍回 result 交前端渲染）
            if _trusted_auto and not res.get("ok"):
                raise HTTPException(502, tr(
                    request, "asb.pc.act_failed",
                    detail=str(res.get("error") or "")))
            return {"ok": bool(res.get("ok")), "action": plan["action"],
                    "machine": r["machine"], "tool": r["tool"], "result": res}

        # L2：出确认卡素材（token 绑定 uid，单次核销）
        out = {"ok": True, "action": plan["action"], "need_confirm": True,
               "level": plan["level"], "hot": plan.get("hot"),
               "diff": plan.get("diff"),
               "token": act.issue_confirm(plan, uid),
               "ttl_sec": int(act.CONFIRM_TTL_SEC)}
        # 切档确认卡带存量对齐预览（先看后做：用户确认前就知道会同步多少
        # 个已固化档位的会话、其中几个群）。预览失败不拦确认（apply 期以
        # 现算为准），零写入。
        if plan["action"] == act.ALIGN_ACTION:
            try:
                pv = act.preview_mode_alignment(
                    getattr(app.state, "inbox_store", None),
                    str((plan.get("clean_params") or {}).get("mode") or ""))
                if pv.get("total"):
                    out["align_preview"] = pv
            except Exception:
                logger.debug("对齐预览失败（忽略）", exc_info=True)
        return out

    # ------------------------------------------------------------ agent plan
    @app.post("/api/assistant/agent/plan")
    async def api_assistant_agent_plan(request: Request):
        """「替我做」规划（实施58 P2）：一句话目标 → LLM 严格 JSON 计划 →
        逐步 plan_action 结构性复验（未知/坏参/越权全丢弃）。**零副作用**：
        执行由前端逐步转投 /api/assistant/act（确认/撤销/审计在那层）。
        LLM 走主链 generate_reply（自带 key 池/本地兜底/熔断）。"""
        _api_auth(request)
        _enabled_or_403(request)
        _mobile_guard(request)
        acfg = _assistant_cfg(_cfg()).get("agent")
        acfg = acfg if isinstance(acfg, dict) else {}
        if not acfg.get("enabled"):
            raise HTTPException(403, tr(request, "asb.agent.disabled"))
        uid, _uname, role = _session_user(request)
        _throttle(uid, request)
        data = await request.json()
        goal = str(data.get("goal") or "").strip()
        if not goal:
            raise HTTPException(400, tr(request, "asb.agent.goal_empty"))
        lang = "en" if str(data.get("lang") or "").lower().startswith("en") \
            else "zh"
        page = str(data.get("page") or "")[:120]

        # ── 流程直通（实施58 P5）：确定性触发在 LLM 之前——「我要登陆飞机」
        #    不需要大模型来懂；命中即回完整流程规格（前端模式运行器接管）。
        from src.assistant import flows as fl

        fid = fl.detect_flow_intent(goal)
        if fid:
            if not fl.flow_allowed_for_role(fid, role):
                raise HTTPException(403, tr(request, "asb.act.forbidden"))
            spec = fl.client_spec(fid, lang)
            if spec:
                return {"ok": True, "goal": goal, "plan_ok": True,
                        "say": (spec.get("texts") or {}).get("say", ""),
                        "steps": [], "dropped": [], "flow": spec}

        from src.assistant import agent_planner as planner

        try:
            from src.web.web_context import resolve_skill_manager

            sm = resolve_skill_manager(telegram_client, app)
        except Exception:
            sm = None
        if sm is None or not hasattr(sm, "ai_client"):
            raise HTTPException(503, tr(request, "asb.agent.llm_down"))

        max_steps = max(1, min(int(acfg.get("max_steps", 5) or 5), 8))
        nav = _nav_whitelist()
        pc_cfg = _assistant_cfg(_cfg()).get("pc_runner") or {}
        pc_machines: list = []
        if pc_cfg.get("enabled"):
            runner_pairing.load_machines(pc_cfg.get("machines"))
            pc_machines = runner_pairing.machine_ids()
        prompt = planner.build_planner_prompt(
            goal, role, sorted(nav), lang=lang, page=page,
            max_steps=max_steps, config=_cfg(), pc_machines=pc_machines)
        try:
            raw = str(await sm.ai_client.generate_reply(
                user_message=prompt,
                context={"current_intent": "assistant_agent_plan",
                         "kb_context": ""},
                strategy_overrides={"temperature": 0.1, "max_tokens": 800},
                route="assistant_planner",
            ) or "")
        except Exception:
            logger.warning("agent 规划 LLM 调用失败", exc_info=True)
            raise HTTPException(503, tr(request, "asb.agent.llm_down"))
        verdict = planner.validate_plan(
            planner.parse_plan_json(raw), _cfg(), role,
            nav_paths=nav, max_steps=max_steps, lang=lang, page=page)
        if not verdict.get("ok") and verdict.get("reason") == "parse_failed":
            raise HTTPException(502, tr(request, "asb.agent.plan_failed"))
        return {"ok": True, "goal": goal, "say": verdict.get("say"),
                "plan_ok": bool(verdict.get("ok")),
                "steps": verdict.get("steps"),
                "dropped": verdict.get("dropped"),
                "ask": verdict.get("ask") or ""}

    # ------------------------------------------------------------ flows
    @app.get("/api/assistant/flows")
    async def api_assistant_flows(request: Request):
        """流程目录（实施58 P5）：角色过滤后的可用业务流程清单。"""
        _api_auth(request)
        _enabled_or_403(request)
        _mobile_guard(request)
        _uid, _uname, role = _session_user(request)
        lang = str(request.query_params.get("lang") or "zh")
        from src.assistant import flows as fl

        return {"ok": True, "flows": fl.catalog(role, lang)}

    # ---------------------------------------------------- pc runner mgmt
    @app.get("/api/assistant/pc/machines")
    async def api_pc_machines(request: Request):
        """受控机清单（实施91）：白名单 + 踢下线态 + 逐台探活（在线/UIA）。
        绝不含 token。enabled=false 时只列配置态、不探活。"""
        _api_auth(request)
        _enabled_or_403(request)
        pc_cfg = _assistant_cfg(_cfg()).get("pc_runner") or {}
        enabled = bool(pc_cfg.get("enabled"))
        if enabled:
            runner_pairing.load_machines(pc_cfg.get("machines"))
        rows = runner_pairing.list_machines()
        if enabled:
            from src.assistant import runner_client

            for row in rows:
                if row.get("revoked"):
                    continue
                h = runner_client.probe(row["id"], _cfg())
                row["online"] = bool(h.get("ok"))
                row["uia"] = bool(h.get("uia_available"))
        # 信任档状态（实施91 P2-C）：每台机是否受信 + 到期戳（供面板显示/急停）
        trust_on = bool((pc_cfg.get("trust") or {}).get("enabled"))
        for row in rows:
            row["trusted"] = bool(trust_on and pc_trust.is_trusted(row["id"]))
            row["trust_expires"] = pc_trust.expires_at(row["id"]) if trust_on else 0.0
        return {"ok": True, "enabled": enabled, "trust_enabled": trust_on,
                "machines": rows}

    @app.post("/api/assistant/pc/trust")
    async def api_pc_trust(request: Request):
        """授予某受控机**信任档**（免确认连跑，计时窗口，需 L2 角色）——实施91 P2-C。
        受 trust.enabled 总闸；ttl 按 max_ttl_sec 夹紧。always_confirm 动作仍弹确认。"""
        _api_auth(request)
        _enabled_or_403(request)
        _uid, _uname, role = _session_user(request)
        if "L2" not in act.allowed_levels_for_role(role):
            raise HTTPException(403, tr(request, "asb.act.forbidden"))
        pc_cfg = _assistant_cfg(_cfg()).get("pc_runner") or {}
        tcfg = pc_cfg.get("trust") or {}
        if not tcfg.get("enabled"):
            raise HTTPException(403, tr(request, "asb.pc.trust_disabled"))
        data = await request.json()
        machine = str(data.get("machine") or "")
        if not runner_pairing.get_machine(machine):
            raise HTTPException(400, tr(request, "asb.pc.machine_unavailable"))
        try:
            ttl = int(data.get("ttl_sec") or tcfg.get("default_ttl_sec") or 1800)
        except Exception:
            ttl = 1800
        ttl = max(1, min(ttl, int(tcfg.get("max_ttl_sec") or 28800)))
        exp = pc_trust.grant(machine, ttl)
        act._audit(config_manager, {
            "op": "pc_trust_grant", "actor": (_uname or _uid),
            "machine": machine, "ttl_sec": ttl})
        return {"ok": True, "machine": machine, "expires": round(exp, 1)}

    @app.post("/api/assistant/pc/trust/revoke")
    async def api_pc_trust_revoke(request: Request):
        """收回信任档（自动驾驶急停，需 L2 角色）。"""
        _api_auth(request)
        _enabled_or_403(request)
        _uid, _uname, role = _session_user(request)
        if "L2" not in act.allowed_levels_for_role(role):
            raise HTTPException(403, tr(request, "asb.act.forbidden"))
        data = await request.json()
        machine = str(data.get("machine") or "")
        was = pc_trust.revoke(machine)
        act._audit(config_manager, {
            "op": "pc_trust_revoke", "actor": (_uname or _uid),
            "machine": machine, "was_trusted": bool(was)})
        return {"ok": True, "machine": machine, "was_trusted": bool(was)}

    @app.post("/api/assistant/pc/revoke")
    async def api_pc_revoke(request: Request):
        """踢受控机下线（管理操作，需 L2 角色）——对 runner 接口立即不可用。"""
        _api_auth(request)
        _enabled_or_403(request)
        _uid, _uname, role = _session_user(request)
        if "L2" not in act.allowed_levels_for_role(role):
            raise HTTPException(403, tr(request, "asb.act.forbidden"))
        data = await request.json()
        ok = runner_pairing.revoke(str(data.get("machine") or ""))
        return {"ok": bool(ok)}

    @app.post("/api/assistant/pc/restore")
    async def api_pc_restore(request: Request):
        _api_auth(request)
        _enabled_or_403(request)
        _uid, _uname, role = _session_user(request)
        if "L2" not in act.allowed_levels_for_role(role):
            raise HTTPException(403, tr(request, "asb.act.forbidden"))
        data = await request.json()
        ok = runner_pairing.restore(str(data.get("machine") or ""))
        return {"ok": bool(ok)}

    # ------------------------------------------------------------ history
    @app.get("/api/assistant/act/history")
    async def api_assistant_act_history(request: Request):
        """「小智做过什么」（P1 任务历史+撤销中心）：审计尾部 N 条 +
        逐条标注是否仍可撤销（undo 快照 24h 在册）。桌面/手机同一端点。"""
        _api_auth(request)
        _enabled_or_403(request)
        _mobile_guard(request)
        _uid, _uname, role = _session_user(request)
        if "L2" not in act.allowed_levels_for_role(role):
            raise HTTPException(403, tr(request, "asb.act.forbidden"))
        lang = str(request.query_params.get("lang") or "zh")
        zh = not lang.lower().startswith("en")
        try:
            limit = max(1, min(int(request.query_params.get("limit") or 30),
                               100))
        except Exception:
            limit = 30
        items = []
        for rec in act.read_history(config_manager, limit=limit):
            aid = str(rec.get("action") or "")
            spec = act.ACTIONS.get(aid) or {}
            path = str(rec.get("path") or "")
            label, _f = act.describe_path(path, lang)
            op = str(rec.get("op") or "")
            if op == "undo":
                old_h, new_h = "", act.humanize_value(
                    path, rec.get("restored"), lang)
            else:
                old_h = act.humanize_value(path, rec.get("old"), lang)
                new_h = act.humanize_value(path, rec.get("new"), lang)
            undo_id = str(rec.get("undo_id") or "")
            items.append({
                "ts": rec.get("ts"),
                "op": op,
                "actor": str(rec.get("actor") or ""),
                "action": aid,
                "action_label": str(
                    spec.get("label_zh" if zh else "label_en") or aid),
                "label": label,
                "old_h": old_h,
                "new_h": new_h,
                "undo_id": undo_id,
                "undoable": op == "apply" and act.has_undo(undo_id),
            })
        return {"ok": True, "items": items}

    # ------------------------------------------------------------ undo
    @app.post("/api/assistant/act/undo")
    async def api_assistant_act_undo(request: Request):
        _api_auth(request)
        _enabled_or_403(request)
        src_tag = _mobile_guard(request)
        uid, uname, role = _session_user(request)
        if "L2" not in act.allowed_levels_for_role(role):
            raise HTTPException(403, tr(request, "asb.act.forbidden"))
        data = await request.json()
        res = act.apply_undo(str(data.get("undo_id") or ""),
                             config_manager, (uname or uid) + src_tag,
                             store=getattr(app.state, "inbox_store", None))
        if res is None:
            raise HTTPException(404, tr(request, "asb.act.undo_missing"))
        return {"ok": bool(res.get("ok")), "restored": res.get("restored"),
                "failed": res.get("failed"), "action": res.get("action"),
                "conv_restored": int(res.get("conv_restored") or 0)}
