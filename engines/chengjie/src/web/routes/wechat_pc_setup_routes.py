# -*- coding: utf-8 -*-
"""个人微信 PC 副驾 · 接入引导后端（实施97 线 B · 引导页 ``/workspace/connect/wechat_pc``）。

- ``GET  /api/setup/wechat_pc/env``      环境检测：电脑微信是否安装/运行/登录、版本是否 ≥ 4（登录用户即可读）
- ``GET  /api/setup/wechat_pc/policy``   当前档位 / 工作时段 / 风险确认（登录用户即可读）
- ``POST /api/setup/wechat_pc/policy``   写档位（主管）：``{tier, work_hours?, risk_ack?}``；
  全自动档**服务端强制**要求 ``risk_ack=true``（知情同意落 overlay：谁、何时确认）——命令行 ``--risk-ack`` 只是旧入口。
- ``GET  /api/setup/wechat_pc/start-command`` 启动命令（PowerShell，一键复制；令牌只给文件路径，不回显）
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict

from fastapi import Depends, FastAPI, HTTPException, Request

logger = logging.getLogger(__name__)

TIERS = ("copilot", "semi", "auto_reply")


def _cfg(request: Request) -> Dict[str, Any]:
    cm = getattr(request.app.state, "config_manager", None)
    return dict(getattr(cm, "config", None) or {}) if cm is not None else {}


def _pc_block(cfg: Dict[str, Any]) -> Dict[str, Any]:
    blk = ((cfg.get("platform_login") or {}).get("wechat_pc")) or {}
    return dict(blk) if isinstance(blk, dict) else {}


def policy_view(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """配置块 → 引导页第 ④ 步展示：``{tier, work_hours, risk_ack, risk_ack_by, risk_ack_at}``（纯函数）。"""
    blk = _pc_block(cfg)
    tier = str(blk.get("tier") or "copilot").strip().lower()
    if tier not in TIERS:
        tier = "copilot"
    wh = blk.get("work_hours")
    if not (isinstance(wh, (list, tuple)) and len(wh) == 2):
        wh = [9, 22]
    return {"tier": tier, "work_hours": [int(wh[0]), int(wh[1])], "risk_ack": bool(blk.get("risk_ack", False)),
            "risk_ack_by": str(blk.get("risk_ack_by") or ""), "risk_ack_at": float(blk.get("risk_ack_at") or 0.0)}


def validate_policy_change(body: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    """校验档位变更（纯函数）：返回要写入的补丁；非法 → ``ValueError(reason_code)``。

    - ``tier`` ∈ copilot/semi/auto_reply；
    - ``auto_reply`` 必须带 ``risk_ack: true``（或此前已确认过）；
    - ``work_hours`` 形如 ``[start, end]``，0 ≤ start < end ≤ 24。
    """
    tier = str((body or {}).get("tier") or current.get("tier") or "copilot").strip().lower()
    if tier not in TIERS:
        raise ValueError("bad_tier")
    ack_now = bool((body or {}).get("risk_ack", False))
    if tier == "auto_reply" and not (ack_now or current.get("risk_ack")):
        raise ValueError("risk_ack_required")
    patch: Dict[str, Any] = {"tier": tier}
    if "work_hours" in (body or {}):
        wh = (body or {}).get("work_hours")
        try:
            a, b = int(wh[0]), int(wh[1])
        except Exception:
            raise ValueError("bad_work_hours")
        if not (0 <= a < b <= 24):
            raise ValueError("bad_work_hours")
        patch["work_hours"] = [a, b]
    if ack_now:
        patch["risk_ack"] = True
    return patch


def register_wechat_pc_setup_routes(app: FastAPI, api_auth: Any) -> None:
    from src.web.routes.unified_inbox_auth import _require_supervisor

    @app.get("/api/setup/wechat_pc/env")
    async def api_pc_env(request: Request, _=Depends(api_auth)):
        import asyncio
        from src.integrations.wechat_pc.env_check import check_environment
        env = await asyncio.get_event_loop().run_in_executor(None, check_environment)
        # 副驾进程是否在线：复用注册表心跳
        env["copilot"] = None
        try:
            from src.integrations import account_registry as _ar
            from src.web.desktop_bridge_presence import bridge_presence
            reg = getattr(_ar, "_registry", None)
            if reg is not None:
                for row in reg.list(platform="wechat") or []:
                    pres = bridge_presence(row.get("meta") if isinstance(row.get("meta"), dict) else None)
                    if pres:
                        env["copilot"] = {"account_id": row.get("account_id"), "label": row.get("label"), **pres}
                        break
        except Exception:
            pass
        return {"ok": True, **env}

    @app.get("/api/setup/wechat_pc/policy")
    async def api_pc_policy_get(request: Request, _=Depends(api_auth)):
        return {"ok": True, **policy_view(_cfg(request))}

    @app.post("/api/setup/wechat_pc/policy")
    async def api_pc_policy_set(request: Request, _=Depends(api_auth)):
        _require_supervisor(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        cfg = _cfg(request)
        current = policy_view(cfg)
        try:
            patch = validate_policy_change(body or {}, current)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if patch.get("risk_ack"):
            try:
                who = str(request.session.get("username") or "")
            except Exception:
                who = ""
            patch["risk_ack_by"] = who
            patch["risk_ack_at"] = round(time.time(), 3)
        cm = getattr(request.app.state, "config_manager", None)
        saved = False
        fn = getattr(cm, "save_overlay_patch", None) if cm is not None else None
        if callable(fn):
            try:
                saved = bool(fn({"platform_login": {"wechat_pc": patch}}))
            except Exception:
                logger.debug("[pc_setup] 写档位失败", exc_info=True)
        # 驱动进程启动时读配置：改档要重启驱动才生效
        return {"ok": True, "saved": saved, **policy_view(_cfg(request)), "restart_required": True}

    def _bridge_paths(request: Request) -> Dict[str, str]:
        """副驾驱动的状态目录与令牌文件（都在后端数据根下：`<config_dir>/wechat_pc/`），引擎根用于 tools 脚本路径。"""
        from pathlib import Path
        cm = getattr(request.app.state, "config_manager", None)
        cfg_path = str(getattr(cm, "config_path", "") or getattr(cm, "path", "") or "")
        config_dir = Path(cfg_path).resolve().parent if cfg_path else Path.cwd() / "config"
        state_dir = config_dir / "wechat_pc"
        engine_root = Path(__file__).resolve().parents[3]
        return {"state_dir": str(state_dir), "token_file": str(state_dir / "TOKEN.txt"),
                "config_file": str(config_dir / "config.local.yaml"), "engine_root": str(engine_root)}

    @app.post("/api/setup/wechat_pc/prepare")
    async def api_pc_prepare(request: Request, _=Depends(api_auth)):
        """把驱动要用的令牌落到后端数据目录（同一台机器、同一 Windows 用户可读）——启动命令不再有占位符。主管专属。"""
        _require_supervisor(request)
        cfg = _cfg(request)
        token = str((cfg.get("web_admin") or {}).get("auth_token") or "").strip()
        if not token:
            raise HTTPException(409, "admin_token_missing")
        paths = _bridge_paths(request)
        from pathlib import Path
        try:
            Path(paths["state_dir"]).mkdir(parents=True, exist_ok=True)
            tf = Path(paths["token_file"])
            tf.write_text(token, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"write_failed: {exc}")
        return {"ok": True, **paths}

    @app.get("/api/setup/wechat_pc/start-command")
    async def api_pc_start_command(request: Request, _=Depends(api_auth)):
        cfg = _cfg(request)
        pol = policy_view(cfg)
        web = cfg.get("web_admin") or {}
        port = int(web.get("port") or 18799)
        base = f"http://127.0.0.1:{port}"
        account_id = str((request.query_params.get("account_id") or "wechat-pc")).strip()[:40] or "wechat-pc"
        paths = _bridge_paths(request)
        from pathlib import Path
        prepared = Path(paths["token_file"]).exists()
        root = paths["engine_root"]
        cmd = (f"cd \"{root}\"; powershell -ExecutionPolicy Bypass -File tools\\wechat_pc_devlink.ps1 -Tier {pol['tier']} "
               f"-BackendUrl {base} -AccountId {account_id} -TokenFile \"{paths['token_file']}\" "
               f"-ConfigFile \"{paths['config_file']}\" -StateDir \"{paths['state_dir']}\"")
        autostart = (f"cd \"{root}\"; powershell -ExecutionPolicy Bypass -File tools\\wechat_pc_autostart.ps1 -Install "
                     f"-Tier {pol['tier']} -BackendUrl {base} -AccountId {account_id} -TokenFile \"{paths['token_file']}\" "
                     f"-ConfigFile \"{paths['config_file']}\" -StateDir \"{paths['state_dir']}\"")
        return {"ok": True, "command": cmd, "autostart_command": autostart, "backend_url": base,
                "account_id": account_id, "tier": pol["tier"], "prepared": prepared, **paths}


__all__ = ["register_wechat_pc_setup_routes", "policy_view", "validate_policy_change", "TIERS"]
