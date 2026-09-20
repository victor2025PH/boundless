# -*- coding: utf-8 -*-
"""个人微信 PC 副驾 · 接入引导后端（实施97 线 B · 引导页 ``/workspace/connect/wechat_pc``）。

- ``GET  /api/setup/wechat_pc/env``      环境检测：电脑微信是否安装/运行/登录、版本是否 ≥ 4、后端能否托管驱动
  （``can_manage`` = 后端在 Windows 且 ``uiautomation`` 可用）、副驾心跳（登录用户即可读）
- ``GET  /api/setup/wechat_pc/policy``   当前档位 / 工作时段 / 风险确认（登录用户即可读）
- ``POST /api/setup/wechat_pc/policy``   写档位（主管）：``{tier, work_hours?, risk_ack?}``；
  全自动档**服务端强制**要求 ``risk_ack=true``（知情同意落 overlay：谁、何时确认）。驱动按配置文件热生效，不必重启。
- ``POST /api/setup/wechat_pc/launch-wechat``  拉起电脑微信 / 把登录窗置前（主管）
- ``POST /api/setup/wechat_pc/copilot/start|stop|restart``  一键启停后端托管的驱动进程（主管；2026-09-19 P1）
- ``GET  /api/setup/wechat_pc/copilot/status``  状态机（idle/starting/online/blind/offline/error）+ 日志尾
- ``POST /api/setup/wechat_pc/autostart``  ``{enabled}`` 微信登录后自动拉起（落 overlay ``autostart``）
- ``GET  /api/setup/wechat_pc/start-command`` 手动命令（局域网部署 / 高级折叠区；令牌只给文件路径，不回显）
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request

logger = logging.getLogger(__name__)

TIERS = ("copilot", "semi", "auto_reply")
DEFAULT_ACCOUNT_ID = "wechat-pc"
DEFAULT_LABEL = "个人微信 · PC 副驾"
# 与驱动 ``PcPolicy.work_hours`` 默认一致（门禁 test_pc_policy_view_and_validation 钉住）。此前页面写 9–22、
# 驱动跑 8–23，第③步「发送时段」显示的与实际拒发时段不是同一个数。
DEFAULT_WORK_HOURS = (8, 23)
# semi/auto_reply 的出站要经桌面桥受控队列（``inbox.l2_autosend.desktop_bridge``）。默认关——2026-09-19 实测：
# 用户按引导选了档位，工作台手发仍 400「不支持的平台」、全自动只拟稿不发。保存档位时顺手打开；只开不关
# （Electron 内嵌网页账号也走这条桥，降回 copilot 不能替别人关闸）。
SENDING_TIERS = ("semi", "auto_reply")


def _cfg(request: Request) -> Dict[str, Any]:
    cm = getattr(request.app.state, "config_manager", None)
    return dict(getattr(cm, "config", None) or {}) if cm is not None else {}


def _pc_block(cfg: Dict[str, Any]) -> Dict[str, Any]:
    blk = ((cfg.get("platform_login") or {}).get("wechat_pc")) or {}
    return dict(blk) if isinstance(blk, dict) else {}


def bridge_enabled(cfg: Dict[str, Any]) -> bool:
    """``inbox.l2_autosend.desktop_bridge.enabled``（纯函数）——桌面桥出站队列开关。"""
    try:
        br = ((((cfg.get("inbox") or {}).get("l2_autosend") or {}).get("desktop_bridge")) or {})
        return bool(br.get("enabled"))
    except Exception:
        return False


def bridge_patch_for_tier(tier: str, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """档位需要发送而桥未开 → 返回要写的 overlay 补丁；否则 ``None``（纯函数，只开不关）。"""
    if str(tier or "").strip().lower() not in SENDING_TIERS or bridge_enabled(cfg):
        return None
    return {"inbox": {"l2_autosend": {"desktop_bridge": {"enabled": True}}}}


def policy_view(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """配置块 → 引导页「设置副驾」步展示：``{tier, work_hours, risk_ack, risk_ack_by, risk_ack_at, autostart, saved_at,
    bridge_enabled}``（纯函数）。``saved_at`` = 主管最近一次在引导页保存的时间（0 = 从未保存，页面据此判断第 ② 步
    是否真完成）；``bridge_enabled`` = 出站通道是否已开（semi/auto_reply 没有它就是「只读」）。"""
    blk = _pc_block(cfg)
    tier = str(blk.get("tier") or "copilot").strip().lower()
    if tier not in TIERS:
        tier = "copilot"
    wh = blk.get("work_hours")
    if not (isinstance(wh, (list, tuple)) and len(wh) == 2):
        wh = list(DEFAULT_WORK_HOURS)
    return {"tier": tier, "work_hours": [int(wh[0]), int(wh[1])], "risk_ack": bool(blk.get("risk_ack", False)),
            "risk_ack_by": str(blk.get("risk_ack_by") or ""), "risk_ack_at": float(blk.get("risk_ack_at") or 0.0),
            "autostart": bool(blk.get("autostart", False)), "saved_at": float(blk.get("saved_at") or 0.0),
            "bridge_enabled": bridge_enabled(cfg)}


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


def bridge_paths_for(config_path: str, engine_root: str) -> Dict[str, str]:
    """副驾驱动的状态目录与令牌文件（都在后端数据根下 ``<config_dir>/wechat_pc/``）+ 引擎根（纯函数）。"""
    config_dir = Path(config_path).resolve().parent if config_path else Path.cwd() / "config"
    state_dir = config_dir / "wechat_pc"
    return {"state_dir": str(state_dir), "token_file": str(state_dir / "TOKEN.txt"),
            "config_file": str(config_dir / "config.local.yaml"), "engine_root": str(engine_root)}


def manual_commands(*, engine_root: str, account_id: str, backend_url: str, token_file: str, config_file: str,
                    state_dir: str) -> Dict[str, str]:
    """手动 PowerShell 命令（局域网部署 / 高级区；纯函数）。不带 ``-Tier``：档位以配置文件为准并热生效。"""
    common = (f"-BackendUrl {backend_url} -AccountId {account_id} -TokenFile \"{token_file}\" "
              f"-ConfigFile \"{config_file}\" -StateDir \"{state_dir}\"")
    return {
        "command": f"cd \"{engine_root}\"; powershell -ExecutionPolicy Bypass -File tools\\wechat_pc_devlink.ps1 {common}",
        "autostart_command": (f"cd \"{engine_root}\"; powershell -ExecutionPolicy Bypass -File tools\\wechat_pc_autostart.ps1 "
                              f"-Install {common}"),
    }


def copilot_presence(registry: Any, account_id: str = "") -> Optional[Dict[str, Any]]:
    """注册表 → 副驾心跳 presence（优先匹配 ``account_id``，否则取第一个有心跳的 wechat 桌面桥账号）。"""
    try:
        from src.web.desktop_bridge_presence import bridge_presence
        rows = registry.list(platform="wechat") or []
    except Exception:
        return None
    first = None
    for row in rows:
        pres = bridge_presence(row.get("meta") if isinstance(row.get("meta"), dict) else None)
        if not pres:
            continue
        item = {"account_id": row.get("account_id"), "label": row.get("label"), **pres}
        if account_id and str(row.get("account_id")) == str(account_id):
            return item
        first = first or item
    return first


def get_or_create_supervisor(app: FastAPI, *, popen: Optional[Callable[..., Any]] = None) -> Any:
    """``app.state.wechat_pc_supervisor`` 懒创建（测试可传假 ``popen``）。"""
    sup = getattr(app.state, "wechat_pc_supervisor", None)
    if sup is not None:
        return sup
    from src.integrations.wechat_pc.supervisor import WeChatPcSupervisor
    cm = getattr(app.state, "config_manager", None)

    def cfg() -> Dict[str, Any]:
        return dict(getattr(cm, "config", None) or {}) if cm is not None else {}

    cfg_path = str(getattr(cm, "config_path", "") or getattr(cm, "path", "") or "")
    engine_root = str(Path(__file__).resolve().parents[3])
    paths = bridge_paths_for(cfg_path, engine_root)
    c0 = cfg()
    blk = _pc_block(c0)
    port = int((c0.get("web_admin") or {}).get("port") or 18799)

    def token() -> str:
        return str((cfg().get("web_admin") or {}).get("auth_token") or "").strip()

    def presence() -> Optional[Dict[str, Any]]:
        try:
            from src.integrations import account_registry as _ar
            reg = getattr(_ar, "_registry", None)
            return copilot_presence(reg, account_id) if reg is not None else None
        except Exception:
            return None

    def env() -> Dict[str, Any]:
        from src.integrations.wechat_pc.env_check import check_environment
        return check_environment()

    def ready() -> Dict[str, Any]:
        from src.integrations.wechat_pc.env_check import driver_ready
        return driver_ready()

    account_id = str(blk.get("account_id") or DEFAULT_ACCOUNT_ID).strip()[:40] or DEFAULT_ACCOUNT_ID
    kw: Dict[str, Any] = dict(engine_root=paths["engine_root"], backend_url=f"http://127.0.0.1:{port}",
                              token_provider=token, config_file=paths["config_file"], state_dir=paths["state_dir"],
                              account_id=account_id, label=str(blk.get("label") or DEFAULT_LABEL),
                              presence_provider=presence, env_provider=env, driver_ready_provider=ready,
                              autostart=bool(blk.get("autostart", False)),
                              interval=float(blk.get("interval_sec") or 3.0))
    if popen is not None:
        kw["popen"] = popen
    sup = WeChatPcSupervisor(**kw)
    app.state.wechat_pc_supervisor = sup
    return sup


def register_wechat_pc_setup_routes(app: FastAPI, api_auth: Any) -> None:
    from src.web.routes.unified_inbox_auth import _require_supervisor

    def _sup(request: Request) -> Any:
        return get_or_create_supervisor(request.app)

    def _account_id(request: Request) -> str:
        q = str(request.query_params.get("account_id") or "").strip()[:40]
        return q or _sup(request).account_id

    @app.get("/api/setup/wechat_pc/env")
    async def api_pc_env(request: Request, _=Depends(api_auth)):
        import asyncio
        from src.integrations.wechat_pc.env_check import check_environment, driver_ready, find_wechat_exe
        env = await asyncio.get_event_loop().run_in_executor(None, check_environment)
        drv = driver_ready()
        # 后端能否托管驱动：Windows + uiautomation。微信进程可见（running）= 后端与微信同机的直接证据。
        env["driver"] = drv
        env["can_manage"] = bool(drv.get("ok"))
        env["same_host"] = bool(env.get("running"))
        if not env.get("exe_path"):
            try:
                env["exe_path"] = await asyncio.get_event_loop().run_in_executor(None, find_wechat_exe)
            except Exception:
                env["exe_path"] = ""
        env["installed"] = bool(env.get("exe_path"))
        # 语音通路（可选能力，不参与第 ① 步完成判定）：版本门 / 音频库 / 虚拟声卡 / 采样率。静态探测 <100ms，
        # 不做回环自测（那是用户点「自测」才跑的 1.2s 有声操作）。
        try:
            from src.integrations.wechat_pc.env_check import voice_environment
            _ver = str(env.get("version") or "")
            env["voice"] = await asyncio.get_event_loop().run_in_executor(
                None, lambda: voice_environment(version=_ver))
        except Exception:
            env["voice"] = None
        sup = _sup(request)
        env["copilot"] = None
        try:
            env["copilot"] = sup.presence_provider()
        except Exception:
            pass
        try:
            st = await asyncio.get_event_loop().run_in_executor(None, sup.status)
            env["supervisor"] = {k: st.get(k) for k in ("state", "reason", "attached", "managed", "pid", "autostart")}
        except Exception:
            env["supervisor"] = None
        return {"ok": True, **env}

    @app.get("/api/setup/wechat_pc/policy")
    async def api_pc_policy_get(request: Request, _=Depends(api_auth)):
        return {"ok": True, **policy_view(_cfg(request))}

    def _save_overlay(request: Request, patch: Dict[str, Any]) -> bool:
        """任意 overlay 深合并补丁（``save_overlay_patch``：落盘 + 内存即时生效）。失败 False，绝不抛。"""
        cm = getattr(request.app.state, "config_manager", None)
        fn = getattr(cm, "save_overlay_patch", None) if cm is not None else None
        if not callable(fn):
            return False
        try:
            return bool(fn(patch))
        except Exception:
            logger.debug("[pc_setup] 写 overlay 失败", exc_info=True)
            return False

    def _save_patch(request: Request, patch: Dict[str, Any]) -> bool:
        return _save_overlay(request, {"platform_login": {"wechat_pc": patch}})

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
            if not who and getattr(request.state, "auth_via_admin_token", False):
                who = "admin_token"  # Bearer 管理员令牌确认：记来源而非留空
            patch["risk_ack_by"] = who
            patch["risk_ack_at"] = round(time.time(), 3)
        patch["saved_at"] = round(time.time(), 3)
        saved = _save_patch(request, patch)
        # 出站通道随档位打开：没有它 semi/auto_reply 只是「看起来能发」（手发 400、全自动只拟稿）
        bridge_patch = bridge_patch_for_tier(patch.get("tier", ""), _cfg(request))
        bridge_saved = _save_overlay(request, bridge_patch) if bridge_patch else None
        # 驱动按配置文件 mtime 热生效（__main__ 里每轮 sleep 检查）；显式 --tier 的旧脚本例外
        return {"ok": True, "saved": saved, "bridge_saved": bridge_saved, **policy_view(_cfg(request)),
                "restart_required": False, "hot_reload": True}

    @app.post("/api/setup/wechat_pc/autostart")
    async def api_pc_autostart(request: Request, _=Depends(api_auth)):
        _require_supervisor(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        enabled = bool((body or {}).get("enabled", False))
        saved = _save_patch(request, {"autostart": enabled})
        sup = _sup(request)
        sup.set_autostart(enabled)
        if enabled:
            sup.reset_backoff()
            sup.ensure_loop()
        return {"ok": True, "saved": saved, "autostart": enabled}

    @app.post("/api/setup/wechat_pc/launch-wechat")
    async def api_pc_launch_wechat(request: Request, _=Depends(api_auth)):
        _require_supervisor(request)
        import asyncio
        from src.integrations.wechat_pc.env_check import launch_wechat
        r = await asyncio.get_event_loop().run_in_executor(None, launch_wechat)
        return {"ok": bool(r.get("ok")), **r}

    @app.post("/api/setup/wechat_pc/voice/selftest")
    async def api_pc_voice_selftest(request: Request, _=Depends(api_auth)):
        """语音通路回环自测：1 kHz 正弦播进 CABLE Input、从 CABLE Output 录回（≈1.2s，对用户无声——不经扬声器）。
        不碰微信、不走出站策略（发到文件传输助手会被 reply_only/系统会话双重拒掉，不值得为自测开策略口子）。"""
        _require_supervisor(request)
        import asyncio
        from src.integrations.wechat_pc.env_check import voice_environment
        r = await asyncio.get_event_loop().run_in_executor(None, lambda: voice_environment(selftest=True))
        st = (r.get("cable") or {}).get("selftest") or {}
        return {"ok": bool(r.get("ready")), "selftest": st, **r}

    @app.post("/api/setup/wechat_pc/voice/align")
    async def api_pc_voice_align(request: Request, _=Depends(api_auth)):
        """把 CABLE Output（录音端）的共享模式格式对齐到 CABLE Input（IPolicyConfig，需要管理员）。"""
        _require_supervisor(request)
        import asyncio
        try:
            from src.integrations.wechat_pc import audio_cable
        except Exception:
            return {"ok": False, "error": "audio_libs_missing"}
        if not audio_cable.available():
            return {"ok": False, "error": "audio_libs_missing"}
        loop = asyncio.get_event_loop()
        r = await loop.run_in_executor(None, audio_cable.align_formats)
        cable = await loop.run_in_executor(None, audio_cable.cable_status)
        return {"ok": bool(r.get("ok")), "align": r, "cable": cable, "error": r.get("error", "")}

    @app.get("/api/setup/wechat_pc/copilot/status")
    async def api_pc_copilot_status(request: Request, _=Depends(api_auth)):
        import asyncio
        st = await asyncio.get_event_loop().run_in_executor(None, _sup(request).status)
        return {"ok": True, **st}

    @app.post("/api/setup/wechat_pc/copilot/start")
    async def api_pc_copilot_start(request: Request, _=Depends(api_auth)):
        _require_supervisor(request)
        import asyncio
        from src.integrations.wechat_pc.env_check import driver_ready
        _drv = driver_ready()
        if not _drv.get("ok"):
            # 跨会话时说「driver_not_ready」会让人去查 uiautomation；点名 engine_not_interactive
            raise HTTPException(409, str(_drv.get("reason") or "driver_not_ready"))
        sup = _sup(request)
        sup.reset_backoff()
        r = await asyncio.get_event_loop().run_in_executor(None, lambda: sup.start(force=False))
        if not r.get("ok"):
            raise HTTPException(409, str(r.get("reason") or "start_failed"))
        return r

    @app.post("/api/setup/wechat_pc/copilot/stop")
    async def api_pc_copilot_stop(request: Request, _=Depends(api_auth)):
        _require_supervisor(request)
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(None, _sup(request).stop)

    @app.post("/api/setup/wechat_pc/copilot/restart")
    async def api_pc_copilot_restart(request: Request, _=Depends(api_auth)):
        _require_supervisor(request)
        import asyncio
        from src.integrations.wechat_pc.env_check import driver_ready
        _drv = driver_ready()
        if not _drv.get("ok"):
            raise HTTPException(409, str(_drv.get("reason") or "driver_not_ready"))
        sup = _sup(request)
        sup.reset_backoff()
        r = await asyncio.get_event_loop().run_in_executor(None, sup.restart)
        if not r.get("ok"):
            raise HTTPException(409, str(r.get("reason") or "start_failed"))
        return r

    def _bridge_paths(request: Request) -> Dict[str, str]:
        cm = getattr(request.app.state, "config_manager", None)
        cfg_path = str(getattr(cm, "config_path", "") or getattr(cm, "path", "") or "")
        return bridge_paths_for(cfg_path, str(Path(__file__).resolve().parents[3]))

    @app.post("/api/setup/wechat_pc/prepare")
    async def api_pc_prepare(request: Request, _=Depends(api_auth)):
        """把驱动要用的令牌落到后端数据目录（同一台机器、同一 Windows 用户可读）——手动命令不再有占位符。主管专属。"""
        _require_supervisor(request)
        cfg = _cfg(request)
        token = str((cfg.get("web_admin") or {}).get("auth_token") or "").strip()
        if not token:
            raise HTTPException(409, "admin_token_missing")
        paths = _bridge_paths(request)
        try:
            Path(paths["state_dir"]).mkdir(parents=True, exist_ok=True)
            Path(paths["token_file"]).write_text(token, encoding="utf-8")
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
        account_id = _account_id(request)
        paths = _bridge_paths(request)
        prepared = Path(paths["token_file"]).exists()
        cmds = manual_commands(engine_root=paths["engine_root"], account_id=account_id, backend_url=base,
                               token_file=paths["token_file"], config_file=paths["config_file"],
                               state_dir=paths["state_dir"])
        return {"ok": True, **cmds, "backend_url": base, "account_id": account_id, "tier": pol["tier"],
                "prepared": prepared, **paths}

    @app.on_event("startup")
    async def _wechat_pc_supervisor_autostart():
        # 自启轮询只在配置开了 autostart 且后端在 Windows 时挂（纯 Win32 枚举，15 秒一拍，几毫秒）
        try:
            cfg = dict(getattr(getattr(app.state, "config_manager", None), "config", None) or {})
            if os.name == "nt" and _pc_block(cfg).get("autostart"):
                get_or_create_supervisor(app).ensure_loop()
                logger.info("个人微信 PC 副驾：已开启「微信登录后自动启动」轮询")
        except Exception:
            logger.debug("[pc_setup] 自启轮询挂载跳过", exc_info=True)

    @app.on_event("shutdown")
    async def _wechat_pc_supervisor_shutdown():
        sup = getattr(app.state, "wechat_pc_supervisor", None)
        if sup is not None:
            try:
                await sup.shutdown()
            except Exception:
                pass


__all__ = ["register_wechat_pc_setup_routes", "policy_view", "validate_policy_change", "TIERS", "bridge_paths_for",
           "manual_commands", "copilot_presence", "get_or_create_supervisor"]
