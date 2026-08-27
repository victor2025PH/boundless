"""智能养号 —— 后台 API（工具箱 cp-nurture 卡，2026-08-21）。

挂 ``/api/nurture/*``。P0 职责：
- **状态总览**：复用既有 ``fleet_overview``（账号健康红绿灯 + 生命周期分布），
  并把每个账号已配的养护方案并进去——零新造机群统计。
- **用户自配养护**：per-account 养护方案读写（``ops.nurture.accounts."platform:account_id"``），
  写经 ``config_manager.set_overlay_flag``（保注释、热生效）。

养护「方案」= 该账号怎么养（节奏档 + 天数 + 行为开关）；P0 只**保存配置 + 可视化**，
真正替账号产生平台动作（上线/已读/点反应/自号互聊）的**执行引擎属 P1**（独立模块 +
金丝雀白名单 + kill-switch 联动 + 完整门禁）。P0 保存的 ``self_chat`` 等行为开关是
P1 执行器的输入，此刻不产生任何真实动作——UI 明示「已保存·执行引擎下一阶段启用」。

合规红线（写进 config.example）：养号是**主动自动化行为**，风险高于被动限流；
默认总闸关、金丝雀先行、自号互聊仅自有账号池内 + 安全语料、严格尊重风控 kill-switch。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import Depends, HTTPException, Request

from src.web.web_i18n import tr

logger = logging.getLogger("ai_chat_assistant.nurture_routes")

_ROLE_VIEWER = "viewer"
_PROFILES = ("conservative", "balanced", "aggressive")
_BEHAVIOR_KEYS = ("browse", "read", "react", "self_chat")
_ENGINE_ACTIONS = ("enable_dry", "go_live", "pause")


def register_nurture_routes(app, auth_dep, audit_store=None, config_manager=None,
                            page_auth=None):
    """挂载智能养号后台 API。``auth_dep``=登录校验；``audit_store``=操作审计（可选）。"""

    def _root_cfg() -> Dict[str, Any]:
        try:
            return (config_manager.config if config_manager is not None else {}) or {}
        except Exception:
            return {}

    def _nurture_cfg() -> Dict[str, Any]:
        return ((_root_cfg().get("ops") or {}).get("nurture") or {})

    def _actor(request: Request) -> str:
        try:
            return str(request.session.get("username") or "web_admin")
        except Exception:
            return "web_admin"

    def _is_viewer(request: Request) -> bool:
        try:
            return str(request.session.get("role") or "") == _ROLE_VIEWER
        except Exception:
            return False

    def _audit(request: Request, action: str, target: str = "", detail: str = "") -> None:
        if audit_store is None:
            return
        try:
            audit_store.log(actor=_actor(request), action=action,
                            target=target, detail=detail)
        except Exception:
            logger.debug("nurture audit 失败", exc_info=True)

    def _acct_key(platform: str, account_id: str) -> str:
        return f"{str(platform or '').strip()}:{str(account_id or 'default').strip()}"

    def _default_profile() -> str:
        p = str(_nurture_cfg().get("default_profile") or "balanced").lower()
        return p if p in _PROFILES else "balanced"

    def _plan_for(key: str) -> Dict[str, Any]:
        """某账号的生效养护方案：per-account 覆写 ∪ 全局缺省（默认档 + 全行为关）。"""
        accts = _nurture_cfg().get("accounts") or {}
        raw = accts.get(key) if isinstance(accts, dict) else None
        plan = {
            "enabled": False,
            "profile": _default_profile(),
            "ramp_days": 0,
            "behaviors": {k: False for k in _BEHAVIOR_KEYS},
        }
        if isinstance(raw, dict):
            plan["enabled"] = bool(raw.get("enabled", False))
            prof = str(raw.get("profile") or plan["profile"]).lower()
            plan["profile"] = prof if prof in _PROFILES else plan["profile"]
            try:
                plan["ramp_days"] = int(raw.get("ramp_days") or 0)
            except Exception:
                plan["ramp_days"] = 0
            beh = raw.get("behaviors") or {}
            if isinstance(beh, dict):
                plan["behaviors"] = {k: bool(beh.get(k, False)) for k in _BEHAVIOR_KEYS}
        return plan

    def _engine_snapshot(request: Request) -> Dict[str, Any]:
        """执行引擎运行态快照（无则给未接线兜底，与旧后端/未装载中间态自洽）。"""
        try:
            eng = getattr(request.app.state, "nurture_engine", None)
            if eng is not None:
                return eng.health_snapshot()
        except Exception:
            logger.debug("nurture engine snapshot 失败", exc_info=True)
        ncfg = _nurture_cfg()
        return {"running": False, "enabled": bool(ncfg.get("enabled", False)),
                "dry_run": bool(ncfg.get("dry_run", True)), "wired": False}

    def _fleet_overview() -> Dict[str, Any]:
        cfg = _root_cfg()
        from src.skills.account_signals import fleet_overview
        from src.integrations.account_registry import get_account_registry
        from src.integrations.protocol_autoreply_limits import get_autoreply_limiter
        from src.integrations.protocol_autoreply_settings import cfg_with_settings
        reg = get_account_registry()
        try:
            lim = get_autoreply_limiter(cfg_with_settings(cfg))
        except Exception:
            lim = None
        accounts = [
            (r.get("platform"), r.get("account_id"), r.get("status", ""))
            for r in reg.list()
        ]
        return fleet_overview(accounts, registry=reg, limiter=lim, config=cfg)

    def _label_map() -> Dict[str, str]:
        """人话显示名（P1 2026-08-22）：注册表 ``label`` 列 → meta.self_name →
        meta.self_username → 空串（前端回落短 key）。fleet_overview 行不带 label，
        这里按注册表补一次（singleton，量级=在册账号数，零额外 IO 压力）。"""
        out: Dict[str, str] = {}
        try:
            from src.integrations.account_registry import get_account_registry
            for r in get_account_registry().list():
                k = _acct_key(r.get("platform"), r.get("account_id"))
                meta = r.get("meta") or {}
                lb = str(r.get("label") or meta.get("self_name")
                         or meta.get("self_username") or "").strip()
                if lb:
                    out[k] = lb
        except Exception:
            logger.debug("nurture label map 失败", exc_info=True)
        return out

    # ── GET /api/nurture/status：机群健康 + 生命周期 + 每号养护方案 ───────────
    @app.get("/api/nurture/status")
    async def api_nurture_status(request: Request, _=Depends(auth_dep)):
        ncfg = _nurture_cfg()
        try:
            overview = _fleet_overview()
        except Exception:
            logger.debug("nurture status fleet_overview 失败", exc_info=True)
            overview = {"fleet": {}, "lifecycle": {}, "accounts": [], "total": 0}
        labels = _label_map()
        canary = {str(x) for x in (ncfg.get("canary_accounts") or [])}
        # 每号并入养护方案（per-account 覆写 ∪ 缺省），前端一行展示健康 + 方案。
        # is_canary（P1 2026-08-22 试点 UI 化）：前端按此渲染「试点」chip；
        # 旧前端忽略该字段零影响，旧后端缺字段=前端 fail-hidden 不出 chip。
        accts_out: List[Dict[str, Any]] = []
        for a in (overview.get("accounts") or []):
            key = _acct_key(a.get("platform"), a.get("account_id"))
            item = dict(a)
            item["nurture_key"] = key
            item["nurture_plan"] = _plan_for(key)
            item["label"] = labels.get(key, "")
            item["is_canary"] = key in canary
            accts_out.append(item)
        nurtured = sum(1 for a in accts_out if a["nurture_plan"]["enabled"])
        eng = _engine_snapshot(request)
        # 配置防呆（P2）：检出 go_live 常见误配，前端/CLI 照单提示。registry_keys=
        # 机群在册号（accts_out 来自 fleet_overview 全量注册表）→ 可查陈旧配置号。
        try:
            from src.nurture.nurture_scheduler import lint_nurture_config
            reg_keys = [str(a.get("nurture_key") or "") for a in accts_out]
            warnings = lint_nurture_config(ncfg, registry_keys=reg_keys)
        except Exception:
            warnings = []
        # 执行引擎 P1 已上线：executor_ready = 引擎循环在跑（可 dry/go_live）。
        # can_write：viewer 只读（与本文件全部写路由的 403 判定同源）——前端据此
        # 隐藏引擎按钮/保存/探针等写控件；旧前端忽略该字段零影响。
        return {
            "ok": True,
            "can_write": not _is_viewer(request),
            "enabled": bool(ncfg.get("enabled", False)),
            "dry_run": bool(ncfg.get("dry_run", True)),
            "default_profile": _default_profile(),
            "profiles": list(_PROFILES),
            "behaviors": list(_BEHAVIOR_KEYS),
            "fleet": overview.get("fleet") or {},
            "lifecycle": overview.get("lifecycle") or {},
            "accounts": accts_out,
            "total": int(overview.get("total") or 0),
            "nurtured": nurtured,
            "engine": eng,
            "warnings": warnings,
            "executor_ready": bool(eng.get("running", False)),
        }

    # ── GET /api/nurture/config：读全局 + 每号方案（供设置回填）────────────────
    @app.get("/api/nurture/config")
    async def api_nurture_config(request: Request, _=Depends(auth_dep)):
        ncfg = _nurture_cfg()
        accts = ncfg.get("accounts") or {}
        out_accts: Dict[str, Any] = {}
        if isinstance(accts, dict):
            for k in accts.keys():
                out_accts[str(k)] = _plan_for(str(k))
        return {
            "ok": True,
            "enabled": bool(ncfg.get("enabled", False)),
            "default_profile": _default_profile(),
            "profiles": list(_PROFILES),
            "behaviors": list(_BEHAVIOR_KEYS),
            "accounts": out_accts,
            "executor_ready": False,
        }

    # ── POST /api/nurture/config：写全局开关 / 缺省档 / 单号养护方案 ────────────
    @app.post("/api/nurture/config")
    async def api_nurture_config_save(request: Request, _=Depends(auth_dep)):
        if _is_viewer(request):
            raise HTTPException(403, tr(request, "err.nurture.viewer_denied"))
        if config_manager is None:
            raise HTTPException(500, tr(request, "err.nurture.no_config_mgr"))
        try:
            body = await request.json()
        except Exception:
            body = {}

        # 全局：总开关 + 缺省档（可选，缺省不动）
        if "enabled" in body:
            ok, msg = config_manager.set_overlay_flag(
                "ops.nurture.enabled", bool(body.get("enabled")))
            if not ok:
                raise HTTPException(500, tr(request, "err.nurture.save_failed", err=msg))
        if "default_profile" in body:
            dp = str(body.get("default_profile") or "").lower()
            if dp not in _PROFILES:
                raise HTTPException(400, tr(request, "err.nurture.profile_bad", name=dp))
            ok, msg = config_manager.set_overlay_flag("ops.nurture.default_profile", dp)
            if not ok:
                raise HTTPException(500, tr(request, "err.nurture.save_failed", err=msg))

        # 试点账号增删（P1 2026-08-22 试点 UI 化）：{canary: {key: "platform:id", pilot: bool}}
        # ——ops.nurture.canary_accounts 此前只能改 YAML，这是它的单一 UI 写入口。
        # 写整表（追加/剔除后回写），保序去重；key 必须形如 platform:account_id。
        can = body.get("canary")
        if isinstance(can, dict):
            key = str(can.get("key") or "").strip()
            if not key or ":" not in key:
                raise HTTPException(400, tr(request, "err.nurture.platform_empty"))
            want = bool(can.get("pilot"))
            cur: List[str] = []
            for x in (_nurture_cfg().get("canary_accounts") or []):
                sx = str(x)
                if sx and sx not in cur and sx != key:
                    cur.append(sx)
            if want:
                cur.append(key)
            ok, msg = config_manager.set_overlay_flag("ops.nurture.canary_accounts", cur)
            if not ok:
                raise HTTPException(500, tr(request, "err.nurture.save_failed", err=msg))
            _audit(request, "nurture.set_canary", target=key,
                   detail=f"pilot={want} total={len(cur)}")
            return {"ok": True, "key": key, "pilot": want, "canary_accounts": cur}

        # 单号养护方案：{platform, account_id, plan:{enabled,profile,ramp_days,behaviors}}
        acct = body.get("account")
        if isinstance(acct, dict):
            platform = str(acct.get("platform") or "").strip()
            account_id = str(acct.get("account_id") or "default").strip()
            if not platform:
                raise HTTPException(400, tr(request, "err.nurture.platform_empty"))
            plan_in = acct.get("plan") or {}
            prof = str(plan_in.get("profile") or _default_profile()).lower()
            if prof not in _PROFILES:
                raise HTTPException(400, tr(request, "err.nurture.profile_bad", name=prof))
            try:
                ramp = int(plan_in.get("ramp_days") or 0)
            except Exception:
                ramp = 0
            ramp = max(0, min(60, ramp))
            beh_in = plan_in.get("behaviors") or {}
            behaviors = {k: bool(beh_in.get(k, False)) for k in _BEHAVIOR_KEYS}
            plan = {
                "enabled": bool(plan_in.get("enabled", False)),
                "profile": prof,
                "ramp_days": ramp,
                "behaviors": behaviors,
            }
            key = _acct_key(platform, account_id)
            # key 含冒号（platform:account_id）作为 overlay 末段单键，不再嵌点。
            ok, msg = config_manager.set_overlay_flag(
                f"ops.nurture.accounts.{key}", plan)
            if not ok:
                raise HTTPException(500, tr(request, "err.nurture.save_failed", err=msg))
            _audit(request, "nurture.set_account", target=key,
                   detail=f"profile={prof} on={plan['enabled']}")
            return {"ok": True, "key": key, "plan": plan, "executor_ready": False}

        _audit(request, "nurture.set_global", detail="global toggle/profile")
        return {"ok": True}

    # ── POST /api/nurture/engine：三档状态机 enable_dry → go_live → pause ────────
    @app.post("/api/nurture/engine")
    async def api_nurture_engine(request: Request, _=Depends(auth_dep)):
        if _is_viewer(request):
            raise HTTPException(403, tr(request, "err.nurture.viewer_denied"))
        if config_manager is None:
            raise HTTPException(500, tr(request, "err.nurture.no_config_mgr"))
        try:
            body = await request.json()
        except Exception:
            body = {}
        action = str(body.get("action") or "").strip()
        if action not in _ENGINE_ACTIONS:
            raise HTTPException(400, tr(request, "err.nurture.engine_action_bad", name=action))
        ncfg = _nurture_cfg()
        # go_live 前置：必须已 enabled（强制先灰度，与 care 同哲学）
        if action == "go_live" and not bool(ncfg.get("enabled", False)):
            return {"ok": False, "reason": "not_enabled"}
        if action == "enable_dry":
            flags = [("ops.nurture.enabled", True), ("ops.nurture.dry_run", True)]
        elif action == "go_live":
            flags = [("ops.nurture.dry_run", False)]
        else:  # pause
            flags = [("ops.nurture.enabled", False)]
        for path, value in flags:
            ok, msg = config_manager.set_overlay_flag(path, value)
            if not ok:
                raise HTTPException(500, tr(request, "err.nurture.save_failed", err=msg))
        _audit(request, "nurture.engine", detail=action)
        return {"ok": True, "action": action, "engine": _engine_snapshot(request)}

    # ── POST /api/nurture/probe：手动单动作探针（go_live 真机验证）────────────────
    @app.post("/api/nurture/probe")
    async def api_nurture_probe(request: Request, _=Depends(auth_dep)):
        if _is_viewer(request):
            raise HTTPException(403, tr(request, "err.nurture.viewer_denied"))
        eng = getattr(request.app.state, "nurture_engine", None)
        if eng is None:
            return {"ok": False, "reason": "engine_not_loaded"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        platform = str(body.get("platform") or "").strip()
        account_id = str(body.get("account_id") or "").strip()
        kind = str(body.get("kind") or "").strip().lower()
        confirm = bool(body.get("confirm", False))
        if not platform or not account_id:
            raise HTTPException(400, tr(request, "err.nurture.platform_empty"))
        if kind not in ("read", "self_chat", "online", "browse", "react"):
            raise HTTPException(400, tr(request, "err.nurture.engine_action_bad", name=kind))
        try:
            res = await eng.probe_action(platform, account_id, kind, confirm=confirm)
        except Exception as ex:  # noqa: BLE001
            raise HTTPException(502, tr(request, "err.nurture.save_failed", err=str(ex)[:200]))
        _audit(request, "nurture.probe", target=f"{platform}:{account_id}",
               detail=f"{kind} confirm={confirm} -> {res.get('detail')}")
        return {"ok": True, "result": res}

    # ── GET /api/nurture/shadow：影子样本（dry_run 会怎么养 / go_live 执行记录）────
    @app.get("/api/nurture/shadow")
    async def api_nurture_shadow(request: Request, limit: int = 50, _=Depends(auth_dep)):
        try:
            led = getattr(request.app.state, "nurture_ledger", None)
            if led is None:
                return {"ok": True, "samples": [], "stats": {}}
            return {"ok": True,
                    "samples": led.recent_shadow(int(max(1, min(200, limit)))),
                    "stats": led.stats()}
        except Exception:
            logger.debug("nurture shadow 读取失败", exc_info=True)
            return {"ok": True, "samples": [], "stats": {}}

    logger.info("[nurture] 养号路由已挂载 /api/nurture/*")
