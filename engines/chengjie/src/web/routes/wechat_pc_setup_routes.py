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
- 多账号（双开微信，2026-09-20 P1）：``platform_login.wechat_pc.accounts[]`` 每项一个 supervisor 子进程，
  ``copilot/*`` 端点带 ``?account_id=`` 指定哪一个（不带 = 主账号，老页面零改动）：
  ``GET /api/setup/wechat_pc/accounts`` 全部账号状态卡；``POST`` 新增/改绑定；``DELETE /{account_id}`` 移除；
  ``GET /api/setup/wechat_pc/windows`` 桌面上的微信主窗（hwnd/pid/标题 + 已绑给谁）——设置页「从窗口里选一个」。
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

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


def copilot_presence(registry: Any, account_id: str = "", *, strict: bool = False) -> Optional[Dict[str, Any]]:
    """注册表 → 副驾心跳 presence（优先匹配 ``account_id``，否则取第一个有心跳的 wechat 桌面桥账号；
    ``strict=True`` 不回落——多账号时 B 号的卡片绝不能借 A 号的心跳显示「在线」）。"""
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
    return None if strict else first


def supervisor_binding_kwargs(blk: Dict[str, Any]) -> Dict[str, Any]:
    """配置块 → 驱动的窗口/身份绑定参数（纯函数）：``window_hwnd`` / ``window_pid``（双开微信时绑死一个窗/进程）
    与 ``expect_wxid``（这个 account_id 应登的微信号；窗里不是它 → 驱动冻结发送）。非法/缺省 → 不绑。"""
    def _i(k: str) -> int:
        try:
            v = int(blk.get(k) or 0)
        except (TypeError, ValueError):
            return 0
        return v if v > 0 else 0
    return {"window_hwnd": _i("window_hwnd"), "window_pid": _i("window_pid"),
            "expect_wxid": str(blk.get("expect_wxid") or "").strip()[:64]}


def accounts_from_cfg(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """配置 → 账号行列表（第一项主账号）；见 :func:`supervisor.normalize_accounts`。"""
    from src.integrations.wechat_pc.supervisor import normalize_accounts
    return normalize_accounts(_pc_block(cfg), default_account_id=DEFAULT_ACCOUNT_ID, default_label=DEFAULT_LABEL)


def validate_account_change(body: Dict[str, Any], existing_ids: List[str], *, primary_id: str) -> Dict[str, Any]:
    """校验「新增/改绑定」请求（纯函数）→ 要写进 ``accounts[]`` 的一行；非法 → ``ValueError(reason_code)``。

    - ``account_id``：1–40 字符，只允许字母数字 ``-_.``（要进命令行与文件名）；缺省 = 主账号；
    - ``window_hwnd`` / ``window_pid``：非负整数，0 = 不绑；``expect_wxid`` ≤ 64 字符；
    - 同一个 hwnd/pid 不能绑给两个账号（``binding_conflict`` 由调用方按现有行判断，这里只做形状）。
    """
    body = dict(body or {})
    aid = str(body.get("account_id") or primary_id).strip()[:40]
    if not aid or not all(ch.isalnum() or ch in "-_." for ch in aid):
        raise ValueError("bad_account_id")
    row: Dict[str, Any] = {"account_id": aid}
    for k in ("window_hwnd", "window_pid"):
        if k in body:
            try:
                v = int(body.get(k) or 0)
            except (TypeError, ValueError):
                raise ValueError(f"bad_{k}")
            if v < 0:
                raise ValueError(f"bad_{k}")
            row[k] = v
    if "expect_wxid" in body:
        row["expect_wxid"] = str(body.get("expect_wxid") or "").strip()[:64]
    if "label" in body:
        row["label"] = str(body.get("label") or "").strip()[:60]
    if "autostart" in body:
        row["autostart"] = bool(body.get("autostart"))
    return row


def merge_account_row(accounts: List[Dict[str, Any]], row: Dict[str, Any], *, primary_id: str) -> List[Dict[str, Any]]:
    """把一行合进 ``accounts[]``（纯函数，返回新列表）：同 id 覆盖给到的字段，其它保留；主账号也存在列表里
    （``normalize_accounts`` 会把它并回顶层）。别的账号已经绑了同一个 hwnd/pid → ``ValueError("binding_conflict")``。"""
    aid = row["account_id"]
    hw, pd = int(row.get("window_hwnd") or 0), int(row.get("window_pid") or 0)
    for a in accounts:
        if str(a.get("account_id")) == aid:
            continue
        if (hw and int(a.get("window_hwnd") or 0) == hw) or (pd and int(a.get("window_pid") or 0) == pd):
            raise ValueError("binding_conflict")
    out: List[Dict[str, Any]] = []
    hit = False
    for a in accounts:
        if str(a.get("account_id")) == aid:
            hit = True
            merged = dict(a)
            merged.update(row)
            out.append(merged)
        else:
            out.append(dict(a))
    if not hit:
        out.append(dict(row))
    return out


def windows_view(wins: List[Any], accounts: List[Dict[str, Any]], statuses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """桌面上的微信主窗 + 已绑给哪个账号 + 那个账号驱动实际读到的昵称/微信号（纯函数）——设置页「选一个窗口」。"""
    by_hwnd: Dict[int, str] = {}
    by_pid: Dict[int, str] = {}
    for a in accounts:
        if int(a.get("window_hwnd") or 0):
            by_hwnd[int(a["window_hwnd"])] = str(a["account_id"])
        if int(a.get("window_pid") or 0):
            by_pid[int(a["window_pid"])] = str(a["account_id"])
    hb_by_pid: Dict[int, Dict[str, Any]] = {}
    for st in statuses:
        hb = st.get("heartbeat") if isinstance(st.get("heartbeat"), dict) else None
        if hb and int(hb.get("window_pid") or 0):
            hb_by_pid[int(hb["window_pid"])] = {"account_id": st.get("account_id"), "nick": hb.get("account_nick", ""),
                                                 "wxid": hb.get("account_wxid", "")}
    out = []
    for w in wins:
        hwnd, pid = int(getattr(w, "hwnd", 0)), int(getattr(w, "pid", 0))
        bound = by_hwnd.get(hwnd) or by_pid.get(pid) or ""
        seen = hb_by_pid.get(pid) or {}
        out.append({"hwnd": hwnd, "pid": pid, "title": str(getattr(w, "title", "") or ""),
                    "width": int(getattr(w, "width", 0) or 0), "height": int(getattr(w, "height", 0) or 0),
                    "bound_account_id": bound, "seen_by_account_id": str(seen.get("account_id") or ""),
                    "nick": str(seen.get("nick") or ""), "wxid": str(seen.get("wxid") or "")})
    return out


def get_or_create_pool(app: FastAPI, *, popen: Optional[Callable[..., Any]] = None) -> Any:
    """``app.state.wechat_pc_supervisors``（多账号池）懒创建；``app.state.wechat_pc_supervisor`` 始终指向主账号那个
    （老代码 / 测试把它置 None 即重建整池）。测试可传假 ``popen``。"""
    pool = getattr(app.state, "wechat_pc_supervisors", None)
    if pool is not None and getattr(app.state, "wechat_pc_supervisor", None) is not None:
        return pool
    from src.integrations.wechat_pc.supervisor import WeChatPcSupervisor, WeChatPcSupervisorPool, account_log_name
    cm = getattr(app.state, "config_manager", None)

    def cfg() -> Dict[str, Any]:
        return dict(getattr(cm, "config", None) or {}) if cm is not None else {}

    cfg_path = str(getattr(cm, "config_path", "") or getattr(cm, "path", "") or "")
    engine_root = str(Path(__file__).resolve().parents[3])
    paths = bridge_paths_for(cfg_path, engine_root)
    c0 = cfg()
    blk = _pc_block(c0)
    port = int((c0.get("web_admin") or {}).get("port") or 18799)
    accounts = accounts_from_cfg(c0)
    primary_id = accounts[0]["account_id"]

    def token() -> str:
        return str((cfg().get("web_admin") or {}).get("auth_token") or "").strip()

    def env() -> Dict[str, Any]:
        from src.integrations.wechat_pc.env_check import check_environment
        return check_environment()

    def ready() -> Dict[str, Any]:
        from src.integrations.wechat_pc.env_check import driver_ready
        return driver_ready()

    def factory(row: Dict[str, Any]) -> Any:
        account_id = str(row["account_id"])

        def presence() -> Optional[Dict[str, Any]]:
            try:
                from src.integrations import account_registry as _ar
                reg = getattr(_ar, "_registry", None)
                if reg is None:
                    return None
                # 只有一个账号时沿用「借用任一心跳」（计划任务 / 手动命令起的驱动 account_id 可能不同）
                strict = len(pool_ref[0].account_ids) > 1 if pool_ref else False
                return copilot_presence(reg, account_id, strict=strict)
            except Exception:
                return None

        kw: Dict[str, Any] = dict(engine_root=paths["engine_root"], backend_url=f"http://127.0.0.1:{port}",
                                  token_provider=token, config_file=paths["config_file"], state_dir=paths["state_dir"],
                                  account_id=account_id, label=str(row.get("label") or DEFAULT_LABEL),
                                  presence_provider=presence, env_provider=env, driver_ready_provider=ready,
                                  autostart=bool(row.get("autostart", False)),
                                  interval=float(blk.get("interval_sec") or 3.0),
                                  window_hwnd=int(row.get("window_hwnd") or 0), window_pid=int(row.get("window_pid") or 0),
                                  expect_wxid=str(row.get("expect_wxid") or ""),
                                  log_name=account_log_name(account_id, primary_id))
        if popen is not None:
            kw["popen"] = popen
        return WeChatPcSupervisor(**kw)

    pool_ref: List[Any] = []
    pool = WeChatPcSupervisorPool(factory)
    pool_ref.append(pool)
    pool.sync(accounts)
    app.state.wechat_pc_supervisors = pool
    app.state.wechat_pc_supervisor = pool.primary
    return pool


def get_or_create_supervisor(app: FastAPI, *, popen: Optional[Callable[..., Any]] = None, account_id: str = "") -> Any:
    """某个账号的 supervisor（不带 ``account_id`` = 主账号；未知 id → None）。"""
    pool = get_or_create_pool(app, popen=popen)
    return pool.get(account_id)


def register_wechat_pc_setup_routes(app: FastAPI, api_auth: Any) -> None:
    from src.web.routes.unified_inbox_auth import _require_supervisor

    def _pool(request: Request) -> Any:
        return get_or_create_pool(request.app)

    def _sup(request: Request) -> Any:
        """``?account_id=`` 指定的账号；不带 = 主账号；带了但池里没有 → 404。"""
        q = str(request.query_params.get("account_id") or "").strip()[:40]
        sup = _pool(request).get(q)
        if sup is None:
            raise HTTPException(404, "unknown_account")
        return sup

    def _account_id(request: Request) -> str:
        """手动命令用：``?account_id=`` 任意值（局域网手起的驱动可以不在池里），不带 = 主账号。"""
        q = str(request.query_params.get("account_id") or "").strip()[:40]
        primary = _pool(request).primary
        return q or (primary.account_id if primary is not None else DEFAULT_ACCOUNT_ID)

    def _resync_pool(request: Request) -> Dict[str, Any]:
        """overlay 写完后按新配置对齐池（新增建、改绑定、删掉的停）。"""
        try:
            r = _pool(request).sync(accounts_from_cfg(_cfg(request)))
            request.app.state.wechat_pc_supervisor = _pool(request).primary
            return r
        except Exception:
            logger.debug("[pc_setup] 池对齐失败", exc_info=True)
            return {"added": [], "updated": [], "removed": []}

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
        # 双开：桌面上有几个微信主窗、配置里有几个账号——页面据此提示「第二个窗口还没绑账号」
        try:
            from src.integrations.wechat_pc.win32_windows import find_wechat_main_windows
            mains = await asyncio.get_event_loop().run_in_executor(None, find_wechat_main_windows)
            env["main_windows"] = len(mains)
        except Exception:
            env["main_windows"] = 1 if env.get("main_window") else 0
        env["accounts"] = _pool(request).account_ids
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
        sup = _sup(request)
        primary = _pool(request).primary
        if primary is not None and sup.account_id == primary.account_id:
            saved = _save_patch(request, {"autostart": enabled})
        else:
            rows = merge_account_row(accounts_from_cfg(_cfg(request)), {"account_id": sup.account_id, "autostart": enabled},
                                     primary_id=primary.account_id if primary else DEFAULT_ACCOUNT_ID)
            saved = _save_patch(request, {"accounts": [r for r in rows if r["account_id"] != (primary.account_id if primary else "")]})
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

    # ── 多账号（双开微信）──
    def _extra_rows(request: Request, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """账号行列表 → 要写进 overlay 的补丁：主账号的绑定写回块顶层，其余进 ``accounts[]``。"""
        primary_id = accounts_from_cfg(_cfg(request))[0]["account_id"]
        patch: Dict[str, Any] = {"accounts": []}
        for r in rows:
            if r["account_id"] == primary_id:
                for k in ("window_hwnd", "window_pid", "expect_wxid", "label", "autostart"):
                    if k in r:
                        patch[k] = r[k]
            else:
                patch["accounts"].append(r)
        return patch

    @app.get("/api/setup/wechat_pc/accounts")
    async def api_pc_accounts(request: Request, _=Depends(api_auth)):
        import asyncio
        pool = _pool(request)
        rows = await asyncio.get_event_loop().run_in_executor(None, pool.status_all)
        primary = pool.primary
        return {"ok": True, "primary_account_id": primary.account_id if primary else "", "accounts": rows}

    @app.post("/api/setup/wechat_pc/accounts")
    async def api_pc_accounts_upsert(request: Request, _=Depends(api_auth)):
        """新增账号或改某账号的窗口/身份绑定：``{account_id?, label?, window_hwnd?, window_pid?, expect_wxid?, autostart?}``。
        进程在跑且绑定变了 → 自动 restart 让新绑定生效（``restarted=true``）。"""
        _require_supervisor(request)
        import asyncio
        try:
            body = await request.json()
        except Exception:
            body = {}
        accounts = accounts_from_cfg(_cfg(request))
        primary_id = accounts[0]["account_id"]
        try:
            row = validate_account_change(body or {}, [a["account_id"] for a in accounts], primary_id=primary_id)
            rows = merge_account_row(accounts, row, primary_id=primary_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        saved = _save_patch(request, _extra_rows(request, rows))
        sync = _resync_pool(request)
        sup = _pool(request).get(row["account_id"])
        restarted = False
        if sup is not None and row["account_id"] in sync.get("updated", []) and sup.status().get("managed"):
            await asyncio.get_event_loop().run_in_executor(None, sup.restart)
            restarted = True
        st = await asyncio.get_event_loop().run_in_executor(None, sup.status) if sup is not None else {}
        return {"ok": True, "saved": saved, "restarted": restarted, **sync, "account": st}

    @app.delete("/api/setup/wechat_pc/accounts/{account_id}")
    async def api_pc_accounts_delete(account_id: str, request: Request, _=Depends(api_auth)):
        _require_supervisor(request)
        accounts = accounts_from_cfg(_cfg(request))
        primary_id = accounts[0]["account_id"]
        aid = str(account_id or "").strip()[:40]
        if aid == primary_id:
            raise HTTPException(400, "cannot_remove_primary")
        if aid not in [a["account_id"] for a in accounts]:
            raise HTTPException(404, "unknown_account")
        rows = [a for a in accounts if a["account_id"] != aid]
        saved = _save_patch(request, {"accounts": [r for r in rows if r["account_id"] != primary_id]})
        sync = _resync_pool(request)
        return {"ok": True, "saved": saved, **sync}

    @app.get("/api/setup/wechat_pc/windows")
    async def api_pc_windows(request: Request, _=Depends(api_auth)):
        """桌面上可见的微信主窗（一个进程一个）+ 各自绑给了哪个账号、驱动读到的昵称/微信号。"""
        import asyncio
        try:
            from src.integrations.wechat_pc.win32_windows import find_wechat_main_windows
            wins = await asyncio.get_event_loop().run_in_executor(None, find_wechat_main_windows)
        except Exception:
            wins = []
        pool = _pool(request)
        statuses = await asyncio.get_event_loop().run_in_executor(None, pool.status_all)
        return {"ok": True, "windows": windows_view(wins, accounts_from_cfg(_cfg(request)), statuses),
                "accounts": [{"account_id": a["account_id"], "label": a["label"], "window_hwnd": a["window_hwnd"],
                              "window_pid": a["window_pid"], "expect_wxid": a["expect_wxid"]}
                             for a in accounts_from_cfg(_cfg(request))]}

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
            if os.name == "nt" and any(a.get("autostart") for a in accounts_from_cfg(cfg)):
                get_or_create_pool(app).ensure_loops()
                logger.info("个人微信 PC 副驾：已开启「微信登录后自动启动」轮询")
        except Exception:
            logger.debug("[pc_setup] 自启轮询挂载跳过", exc_info=True)

    @app.on_event("shutdown")
    async def _wechat_pc_supervisor_shutdown():
        pool = getattr(app.state, "wechat_pc_supervisors", None)
        sup = getattr(app.state, "wechat_pc_supervisor", None)
        try:
            if pool is not None:
                await pool.shutdown()
            elif sup is not None:
                await sup.shutdown()
        except Exception:
            pass


__all__ = ["register_wechat_pc_setup_routes", "policy_view", "validate_policy_change", "TIERS", "bridge_paths_for",
           "manual_commands", "copilot_presence", "supervisor_binding_kwargs", "get_or_create_supervisor",
           "get_or_create_pool", "accounts_from_cfg", "validate_account_change", "merge_account_row", "windows_view"]
