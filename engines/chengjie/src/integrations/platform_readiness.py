"""每平台 × 每登录方式的「为什么不能用 / 该怎么办」单一诊断源。

背景
====
`platform_login.list_modes` 只回答「能不能用」（provider 注册没注册），不回答**为什么**。
不可用原因来自一张静态覆盖表，只有两个值：`not_enabled` / `needs_server_setup`。于是：

- LINE 开了 `protocol_enabled` 但没装 okline → provider 注册失败 → 界面说「尚未启用」，
  运维照着这句话去翻开关，开关明明是开的，白跑一趟；
- WhatsApp 开关开了、provider 也注册了，但 Baileys sidecar 没起 → 界面显示「可用·推荐」，
  用户点下去等二维码，等来的是一句 service_down；
- Telegram 缺 api_id/api_hash 与「压根没开开关」在界面上长得一模一样。

三种完全不同的处置动作被压成同一句话，排障成本全转嫁给用户。本模块把判定收敛成
**结构化 blocker 列表**（含严重度与可本地化的原因码），供三个消费方共用同一口径：

- `GET /api/platforms/{p}/modes`（接入弹窗的「为什么/怎么办」说明卡）
- `protocol_diagnostics.readiness`（`GET /api/accounts/protocol/readiness` + ops 卡）
- `scripts/protocol_doctor.py`（命令行自检）

纯函数：不触网、不读全局状态。`provider_registered` 与 `service_ok` 由调用方注入
（sidecar 可达性是一次 HTTP 探测，留在 `protocol_diagnostics.probe_services`），
因此本模块可被完整单测覆盖。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 严重度：block = 点了也走不通；warn = 能走通，但有隐患（如扫上了不会 7×24 常驻）
SEV_BLOCK = "block"
SEV_WARN = "warn"

# 原因码。前端按 `inbox.connect.rc_<code>_why` / `_how` 取本地化解释与处置建议，
# 短标签取 `inbox.connect.bk_<code>`；后端只吐码与参数，不吐文案。
BLOCK_LOGIN_DISABLED = "login_disabled"
BLOCK_NOT_ENABLED = "not_enabled"
BLOCK_DEP_MISSING = "dep_missing"
BLOCK_CREDS_MISSING = "creds_missing"
BLOCK_SERVICE_DOWN = "service_down"
BLOCK_NEEDS_SERVER_SETUP = "needs_server_setup"
BLOCK_PROVIDER_UNAVAILABLE = "provider_unavailable"
WARN_ORCHESTRATOR_OFF = "orchestrator_off"

# 需要 sidecar 常驻进程的 (平台, 方式)。值＝该方式的启用判定与服务地址取值来源。
_SIDECAR_MODES = {("whatsapp", "protocol"), ("messenger", "web")}

# 本系统**真正实现了**的 (平台, 方式)。不在表内＝功能不存在（如 telegram/web、
# whatsapp/web 只在 modes 清单里占位），恒报「未启用」。
#
# 为什么不靠「provider 有没有注册」判断：注册是**进程内运行时状态**，只有 web 进程
# 在 /modes 前调过 _ensure_login_providers 才有意义。CLI 自检、后台 worker 等进程里
# 它恒为 False —— 早期版本据此兜底，于是 protocol_doctor 把「依赖齐全、开关已开」的
# Telegram 报成 not_enabled，正是本模块要消灭的那种误导。判定必须与进程状态无关。
_IMPLEMENTED_MODES = {
    ("telegram", "protocol"), ("line", "protocol"),
    ("whatsapp", "protocol"), ("messenger", "web"),
}


def _blocker(code: str, severity: str = SEV_BLOCK, **params: Any) -> Dict[str, Any]:
    """一条 blocker。``params`` 会原样透给前端做文案插值（如 {dep} / {url}）。"""
    out: Dict[str, Any] = {"code": code, "severity": severity}
    if params:
        out["params"] = {k: str(v) for k, v in params.items() if v not in (None, "")}
    return out


def _login_enabled(config: Dict[str, Any]) -> bool:
    pl = (config or {}).get("platform_login", {}) or {}
    return bool(pl.get("enabled", True))


def _orchestrator_on(config: Dict[str, Any]) -> bool:
    pl = (config or {}).get("platform_login", {}) or {}
    return bool(pl.get("orchestrator_enabled", False))


def service_probe_targets(config: Dict[str, Any]) -> Dict[str, str]:
    """需要探 ``/health`` 的 sidecar：``{platform: base_url}``。

    只在该方式**已启用**时才纳入——没开的功能去探它的端口，既浪费一次超时，
    又会把「本来就没打算用」误报成「服务挂了」。
    """
    targets: Dict[str, str] = {}
    try:
        from src.integrations.whatsapp_baileys_login import (
            protocol_enabled as wa_on, service_base_url as wa_url,
        )
        if wa_on(config):
            targets["whatsapp"] = wa_url(config)
    except Exception:  # noqa: BLE001
        logger.debug("[readiness] 解析 whatsapp sidecar 地址失败", exc_info=True)
    try:
        from src.integrations.messenger_web_login import (
            service_base_url as mg_url, web_enabled as mg_on,
        )
        if mg_on(config):
            targets["messenger"] = mg_url(config)
    except Exception:  # noqa: BLE001
        logger.debug("[readiness] 解析 messenger sidecar 地址失败", exc_info=True)
    return targets


def _telegram_protocol_blockers(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    from src.integrations.telegram_protocol_login import (
        is_pyrogram_available, protocol_enabled, resolve_credentials,
    )
    out: List[Dict[str, Any]] = []
    if not protocol_enabled(config):
        out.append(_blocker(BLOCK_NOT_ENABLED))
    if not is_pyrogram_available():
        out.append(_blocker(BLOCK_DEP_MISSING, dep="pyrogram",
                            install="pip install pyrogram tgcrypto"))
    if resolve_credentials(config) is None:
        pooled = False
        try:
            from src.integrations.credpool_bridge import credpool_enabled
            pooled = credpool_enabled(config)
        except Exception:  # noqa: BLE001
            logger.debug("[readiness] 读取中央凭据池开关失败", exc_info=True)
        # 开了中央池就不需要自备 api_id/api_hash——这正是「新用户免申请」的设计，
        # 此时不该报缺凭据。
        if not pooled:
            out.append(_blocker(BLOCK_CREDS_MISSING, field="telegram.api_id / api_hash"))
    return out


def _line_protocol_blockers(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    from src.integrations.line_protocol_login import (
        is_node_available, is_okline_available, protocol_enabled,
    )
    out: List[Dict[str, Any]] = []
    if not protocol_enabled(config):
        out.append(_blocker(BLOCK_NOT_ENABLED))
    if not is_okline_available():
        out.append(_blocker(BLOCK_DEP_MISSING, dep="okline", install="pip install okline"))
    elif not is_node_available(config):
        # okline 靠一个 Node 子进程加载 ltsm.wasm 算 X-Hmac，没有 node 扫码必失败——
        # 而失败发生在协议层，界面上看起来只是「扫了没反应」。如实拦下并给可照做的指引，
        # 而不是挂着绿色「推荐」骗点击（Messenger 的 web·推荐但 service_down 就是这个教训）。
        # 装了 App 的机器通常已由随包 Electron 兜住，走到这一档多是非桌面部署。
        out.append(_blocker(
            BLOCK_DEP_MISSING, dep="Node.js 18+",
            install="安装 Node.js 18+（https://nodejs.org）并确保 node 在 PATH 上，然后重开应用"))
    return out


def _sidecar_blockers(
    platform: str, config: Dict[str, Any], service_ok: Optional[bool],
) -> List[Dict[str, Any]]:
    """WhatsApp(protocol) / Messenger(web) 共用：开关 + sidecar 可达性。"""
    out: List[Dict[str, Any]] = []
    if platform == "whatsapp":
        from src.integrations.whatsapp_baileys_login import (
            protocol_enabled as on, service_base_url as url_of,
        )
        svc, not_enabled_code = "whatsapp-baileys", BLOCK_NOT_ENABLED
    else:
        from src.integrations.messenger_web_login import (
            service_base_url as url_of, web_enabled as on,
        )
        # Messenger 不是「打开开关就能用」：还要在服务器的浏览器里人工完成一次 FB 登录，
        # 语义上属于「需运维配置」，与单纯忘了开开关是两回事。
        svc, not_enabled_code = "messenger-web", BLOCK_NEEDS_SERVER_SETUP
    if not on(config):
        out.append(_blocker(not_enabled_code, svc=svc))
    elif service_ok is False:
        out.append(_blocker(BLOCK_SERVICE_DOWN, svc=svc, url=url_of(config)))
    return out


def diagnose_mode(
    platform: str,
    mode: str,
    config: Dict[str, Any],
    *,
    provider_registered: Optional[bool] = None,
    service_ok: Optional[bool] = None,
) -> Dict[str, Any]:
    """诊断单个 (平台, 方式)。

    ``provider_registered``：`platform_login.mode_available` 的结果。**只用于异常兜底**
    （见 `_IMPLEMENTED_MODES` 注释）：``None`` = 调用方不掌握该信息（CLI / 后台进程）。
    ``service_ok``：sidecar `/health` 探测结果；``None`` = 未探测 / 不适用。

    返回 ``{"ready": bool, "blockers": [...], "reason_code": str}``。
    ``reason_code`` 取第一条 block 级 blocker 的码，兼容既有前端字段。
    """
    platform = str(platform or "").lower()
    mode = str(mode or "").lower()
    blockers: List[Dict[str, Any]] = []

    if not _login_enabled(config):
        blockers.append(_blocker(BLOCK_LOGIN_DISABLED))

    if mode != "device":
        implemented = (platform, mode) in _IMPLEMENTED_MODES
        if not implemented:
            blockers.append(_blocker(BLOCK_NOT_ENABLED))
        else:
            try:
                if platform == "telegram":
                    blockers += _telegram_protocol_blockers(config)
                elif platform == "line":
                    blockers += _line_protocol_blockers(config)
                elif (platform, mode) in _SIDECAR_MODES:
                    blockers += _sidecar_blockers(platform, config, service_ok)
            except Exception:  # noqa: BLE001
                # 诊断本身绝不能把登录弹窗弄崩：拿不到细节就退回笼统原因。
                logger.debug("[readiness] 诊断 %s/%s 失败", platform, mode, exc_info=True)
                blockers.append(_blocker(BLOCK_NOT_ENABLED))

            # 异常兜底：开关依赖全对却仍未装载（典型＝运行时刚改开关、尚未重启）。
            # 只有调用方明确知道注册状态时才判，否则会把 CLI 这类「本就不注册」的
            # 进程误报成故障。
            if (provider_registered is False
                    and not any(b["severity"] == SEV_BLOCK for b in blockers)):
                blockers.append(_blocker(BLOCK_PROVIDER_UNAVAILABLE))

        # 编排器关着＝扫得上但不会常驻在线，属隐患不属阻断。
        if not blockers and not _orchestrator_on(config):
            blockers.append(_blocker(WARN_ORCHESTRATOR_OFF, severity=SEV_WARN))

    hard = [b for b in blockers if b["severity"] == SEV_BLOCK]
    return {
        "ready": not hard,
        "blockers": blockers,
        "reason_code": hard[0]["code"] if hard else "",
    }


def diagnose_platform(
    platform: str,
    modes: List[str],
    config: Dict[str, Any],
    *,
    provider_registered_fn=None,
    service_ok: Optional[bool] = None,
) -> Dict[str, Any]:
    """整平台诊断：``{"ready": bool, "modes": {mode: diagnose_mode(...)}}``。

    ``ready`` = 至少一种方式可用（运营视角：这个平台今天能不能接号）。
    ``provider_registered_fn`` 省略即不做「已装载」兜底判定——诊断报告可能在任何
    进程里生成，而 provider 注册表只在 web 进程里有意义。
    """
    per: Dict[str, Any] = {}
    for m in modes:
        reg = None if provider_registered_fn is None else bool(
            provider_registered_fn(platform, m))
        per[m] = diagnose_mode(
            platform, m, config, provider_registered=reg, service_ok=service_ok)
    return {"ready": any(v["ready"] for v in per.values()), "modes": per}
