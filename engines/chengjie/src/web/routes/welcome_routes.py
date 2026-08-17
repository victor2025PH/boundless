"""首启向导 /welcome（WP-2 2026-08）：五步串既有能力，不重写任何底层实现。

① 授权/试用 —— 页面 JS 读 ``GET /api/admin/license``、粘贴激活复用
   ``POST /api/admin/license/activate``；缺授权给官网试用/商店深链（本模块只拼链接）。
② 渠道接入 —— 页面 JS 读 ``GET /api/setup/channels``，接入动作深链既有向导页
   ``/workspace/setup?channel=<id>``（复用 /api/setup/* 全套，不重写）。
③ 人设模板 —— 复用 ``persona_create_wizard.js``（5 个内置模板，两步模态），保存走
   Studio 同一路径 ``PUT /api/personas/profiles/{id}``（retired_facts 冲突检测 / 审计 /
   乐观锁全数生效；遵守 chengjie-persona-discipline，绝不直写 YAML）。
④ 自动化档位 —— 本模块 ``POST /api/onboarding/automation-tier``：三档映射为
   capability 注册表意图，逐条过 ``capability_toggle.check_toggle`` 同一护栏后
   ``cm.set_overlay_flag`` 落 overlay（ruamel 保注释写入器）；档位本身写
   ``inbox.auto_draft.automation_mode``。
⑤ 发测试消息 —— 页面 JS 复用 ``POST /api/unified-inbox/send``（chat_key='me' 发到
   账号自己的收藏消息，与 live_multiwin_drill 同一安全语义）；本模块只提供
   可发账号清单（account_registry 只读）。

Feature flag：``onboarding.enabled`` 基线 false —— 关闭时页面与全部 /api/onboarding/*
一律 404（「无此页」语义，老实例零可见变化）；桌面新装经 cloud_light 预设档播种 true。
进度持久化见 ``src/utils/onboarding_state.py``（实例数据根 JSON，中断可续）。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from src.utils.onboarding_state import (
    STEP_IDS,
    load_state,
    mark_step,
    onboarding_enabled,
    set_completed,
)

logger = logging.getLogger(__name__)

#: 三档 → capability 注册表意图（逐条过 check_toggle 护栏；关永远安全先执行）。
#: manual/review 刻意不动 worker：worker 在无 auto_ai 会话时本就无事可做，
#: 关它反而会把「先体验人审、后切全自动」的路径多埋一步。
_TIER_INTENTS: Dict[str, List[tuple]] = {
    "manual": [("l2_autosend_deliver", "enabled", False)],
    "review": [("l2_autosend_deliver", "enabled", False)],
    "auto_ai": [
        ("l2_autosend_worker", "enabled", True),
        ("l2_autosend_deliver", "enabled", True),
    ],
}

#: 三档 → 全局会话档位（inbox.auto_draft.automation_mode；单一事实源见
#: src/inbox/automation_mode.py——auto_ai 档默认自动 bootstrap 新会话，无需另写开关）
_TIER_MODE = {"manual": "manual", "review": "review", "auto_ai": "auto_ai"}


def _sendable_accounts() -> List[Dict[str, Any]]:
    """可发测试消息的协议账号（telegram 协议号才有「收藏消息」语义）。只读绝不抛。"""
    out: List[Dict[str, Any]] = []
    try:
        from src.integrations.account_registry import get_account_registry

        for row in get_account_registry().list():
            plat = str(row.get("platform") or "").lower()
            aid = str(row.get("account_id") or "")
            mode = str(row.get("mode") or "").lower()
            if plat != "telegram" or not aid or aid == "default":
                continue
            if mode == "official":
                continue
            out.append({
                "platform": plat,
                "account_id": aid,
                "label": str(row.get("label") or row.get("display_name") or aid),
                "online": str(row.get("status") or "").lower() in (
                    "online", "running", "ok"),
            })
    except Exception:
        logger.debug("[welcome] 账号清单读取失败（已忽略）", exc_info=True)
    # 在线的排前面，测试消息第一击就选对
    out.sort(key=lambda r: (not r["online"], r["account_id"]))
    return out


def register_welcome_routes(
    app,
    *,
    templates,
    page_auth,
    api_auth,
    config_manager=None,
    user_store=None,
) -> None:
    """挂载 /welcome 页 + /api/onboarding/*。flag 关时全部 404。"""

    def _cfg() -> Dict[str, Any]:
        return getattr(config_manager, "config", None) or {}

    def _ensure_enabled() -> None:
        if not onboarding_enabled(_cfg()):
            # 「无此页」语义（对齐 /workspace/workflows 运营关闭档）：
            # 老实例 flag 关 → 与本功能不存在时零差异。
            raise HTTPException(404)

    def _require_supervisor_or_shell(request: Request) -> None:
        """写端点守卫：session 用户须主管角色；纯 Bearer（桌面壳主进程，
        api_auth 已验 admin token = master 等价）放行——与 /api/setup/* 同语义。"""
        try:
            has_session = bool(
                request.session.get("user_id") or request.session.get("auth"))
        except Exception:
            has_session = False
        if has_session:
            from src.web.routes.unified_inbox_auth import _require_supervisor

            _require_supervisor(request)

    @app.get("/welcome", response_class=HTMLResponse)
    async def welcome_page(request: Request, _=Depends(page_auth)):
        _ensure_enabled()
        return templates.TemplateResponse(request, "welcome.html", {})

    @app.get("/api/onboarding/status")
    async def api_onboarding_status(request: Request, _=Depends(api_auth)):
        """向导单次装载的聚合读面（只含**没有现成端点**的信息 + 深链）。

        授权详情 / 渠道现状 / 人设列表由页面直接打既有端点
        （/api/admin/license、/api/setup/channels、/api/personas/profiles）——
        这里不复算一份，防口径漂移。
        """
        _ensure_enabled()
        cfg = _cfg()
        ad = (cfg.get("inbox") or {}).get("auto_draft") or {}
        l2 = (cfg.get("inbox") or {}).get("l2_autosend") or {}
        lic_cfg = cfg.get("licensing") or {}
        shop_url = str(lic_cfg.get("shop_url") or "")
        trial_url = str(((lic_cfg.get("trial") or {}).get("site_url")) or "") or shop_url
        return {
            "ok": True,
            "enabled": True,
            "state": load_state(),
            "steps": list(STEP_IDS),
            "automation": {
                "mode": str(ad.get("automation_mode") or "auto_ai"),
                "worker_enabled": bool(l2.get("enabled")),
                "deliver_enabled": bool(l2.get("deliver")),
            },
            "accounts": _sendable_accounts(),
            "links": {
                "shop_url": shop_url,
                "trial_url": trial_url,
                "setup": "/workspace/setup",
                "personas": "/personas",
                "workspace": "/workspace",
            },
        }

    @app.post("/api/onboarding/state")
    async def api_onboarding_state(request: Request, _=Depends(api_auth)):
        """进度持久化：单步 done/skipped + 整体 completed（中断可续 / 完成不再弹）。"""
        _ensure_enabled()
        _require_supervisor_or_shell(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        step = str(body.get("step") or "").strip()
        st = None
        if step:
            if step not in STEP_IDS:
                raise HTTPException(400, f"unknown step: {step}")
            data = body.get("data")
            st = mark_step(
                step,
                done=(bool(body.get("done")) if "done" in body else None),
                skipped=(bool(body.get("skipped")) if "skipped" in body else None),
                data=data if isinstance(data, dict) else None,
            )
        if "completed" in body:
            st = set_completed(bool(body.get("completed")))
        return {"ok": True, "state": st if st is not None else load_state()}

    @app.post("/api/onboarding/automation-tier")
    async def api_onboarding_automation_tier(request: Request, _=Depends(api_auth)):
        """自动化档位三选一（manual / review / auto_ai）→ 护栏写 overlay。

        capability 键逐条过 ``check_toggle``（与 /api/companion/capabilities/toggle
        同一护栏）；被拦如实回报（blocked），绝不静默半应用装成功。
        """
        _ensure_enabled()
        _require_supervisor_or_shell(request)
        cm = config_manager
        config = _cfg()
        if cm is None or not hasattr(cm, "set_overlay_flag"):
            return {"ok": False, "message": "config manager unavailable"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        tier = str((body or {}).get("tier") or "").strip().lower()
        if tier not in _TIER_INTENTS:
            raise HTTPException(400, f"unknown tier: {tier}")

        from src.companion.capability_toggle import check_toggle

        modes = None
        store = getattr(request.app.state, "inbox_store", None)
        if store is not None:
            try:
                modes = store.all_automation_modes()
            except Exception:
                modes = None

        applied: List[str] = []
        blocked: List[Dict[str, str]] = []
        warns: List[str] = []
        for key, field, value in _TIER_INTENTS[tier]:
            chk = check_toggle(config, modes, key, field, value)
            if not chk.get("allowed"):
                blocked.append({"key": key,
                                "reason": str(chk.get("reason") or "guard")})
                continue
            path = str(chk.get("flag_path") or "")
            ok, msg = cm.set_overlay_flag(path, value)
            if ok:
                applied.append(path)
                if chk.get("warn"):
                    warns.append(str(chk.get("reason") or ""))
            else:
                blocked.append({"key": key, "reason": str(msg or "write_failed")})

        mode_path = "inbox.auto_draft.automation_mode"
        ok, msg = cm.set_overlay_flag(mode_path, _TIER_MODE[tier])
        if ok:
            applied.append(mode_path)
        else:
            blocked.append({"key": mode_path, "reason": str(msg or "write_failed")})

        actor = ""
        try:
            actor = str(request.session.get("username") or "")
        except Exception:
            pass
        logger.info("[welcome] automation tier=%s actor=%s applied=%s blocked=%s",
                    tier, actor or "shell", applied,
                    [b["key"] for b in blocked])
        out = {"ok": not blocked, "tier": tier,
               "applied": applied, "blocked": blocked}
        if warns:
            out["warns"] = warns
        return out
