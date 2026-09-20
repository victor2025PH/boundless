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
# official 渠道专属码：creds_missing 的解释文案是 Telegram「在这里直接启用」面板
# 专用的，对 Instagram/Zalo 会把人指去一个不存在的表单——official 的处置永远是
# 「去接入向导」，单列一码让文案可各说各话。
BLOCK_OFFICIAL_CREDS = "official_creds_missing"
BLOCK_SERVICE_DOWN = "service_down"
BLOCK_NEEDS_SERVER_SETUP = "needs_server_setup"
BLOCK_PROVIDER_UNAVAILABLE = "provider_unavailable"
# 「功能不存在」专属码（2026-08-11）：此前与 not_enabled 共用一个码，前端于是给
# 「压根没做」的方式（telegram/web 占位）渲染「填凭据/点一键启用/重新检测」整套
# 指引——全是永远兑现不了的空头支票（实录：用户照指引找一个不存在的表单）。
# 「开关没开（你能开）」与「没实现（做什么都没用，请改用别的方式）」处置完全不同，
# 必须各说各话。
BLOCK_NOT_IMPLEMENTED = "not_implemented"
WARN_ORCHESTRATOR_OFF = "orchestrator_off"

# 需要 sidecar 常驻进程的 (平台, 方式)。值＝该方式的启用判定与服务地址取值来源。
# zalo/web = 个人号扫码（zca-js Node 边车）；instagram/web = 个人号网页托管（Playwright 边车）——
# 与 whatsapp/protocol、messenger/web 同族。
_SIDECAR_MODES = {
    ("whatsapp", "protocol"), ("messenger", "web"),
    ("zalo", "web"), ("instagram", "web"),
}

# 本系统**真正实现了**的 (平台, 方式)。不在表内＝功能不存在（如 whatsapp/web 只在
# modes 清单里占位），恒报 not_implemented（规划中）——诚实说「没做」，绝不再伪装成
# 「未启用」误导用户去翻开关/填凭据（telegram/web 占位曾因此成为死胡同，2026-08-11
# 已从默认清单摘除；protocol 本就是官方关联设备扫码，体验等同）。
#
# 为什么不靠「provider 有没有注册」判断：注册是**进程内运行时状态**，只有 web 进程
# 在 /modes 前调过 _ensure_login_providers 才有意义。CLI 自检、后台 worker 等进程里
# 它恒为 False —— 早期版本据此兜底，于是 protocol_doctor 把「依赖齐全、开关已开」的
# Telegram 报成 not_enabled，正是本模块要消灭的那种误导。判定必须与进程状态无关。
_IMPLEMENTED_MODES = {
    ("telegram", "protocol"), ("telegram", "phone"),
    ("line", "protocol"),
    ("whatsapp", "protocol"), ("messenger", "web"),
    # 个人号扫码/网页托管（Node 边车）：给原本纯官方的 Zalo / Instagram 补的第二路
    # （默认关，由 platform_login.<p>.web_enabled 门控是否对外呈现，见 platform_login）。
    ("zalo", "web"), ("instagram", "web"),
    # 官方 API 通道（凭证经 /workspace/setup 接入向导；无扫码会话）：
    # IG/Zalo 是唯一形态；LINE/Messenger/WhatsApp Cloud 是与扫码/托管并列的合规第二路
    ("instagram", "official"), ("zalo", "official"),
    ("line", "official"), ("messenger", "official"),
    ("whatsapp", "official"),
    # QQ 机器人（QQ 开放平台，2026-09-07）：独立平台，唯一形态 official；
    # 个人号协议登录是另一个平台 qq（见 qq_milky / qq_protocol_login）。
    ("qqbot", "official"),
    # QQ 协议登录（个人号，经用户自装协议端的 Milky 接口；默认关、准入区）
    ("qq", "protocol"),
    # 微信客服（企业微信官方通道，实施97 线 A，2026-09-07）：独立平台，唯一形态 official
    # （企微自建应用凭证经向导）；个人微信 PC 副驾是另一个平台 wechat（mode=pcui，准入区）。
    ("wechat_kf", "official"),
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
    # 与运行时 account_orchestrator.orchestrator_enabled 同口径（三态，见
    # platform_login.resolve_login_switch）：桌面升级安装未写过时默认开，故连接弹窗
    # 不再对「其实会 7×24 常驻」的号误报 orchestrator_off。诊断类读取
    # （config_check / companion_preflight / protocol_diagnostics）刻意仍读字面值——
    # 那是「配置里到底写没写」的运维审计视角，与本处「实际会不会常驻」正交。
    from src.integrations.platform_login import resolve_login_switch
    return resolve_login_switch(config, "platform_login.orchestrator_enabled")


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
    try:
        from src.integrations.zalo_personal_login import (
            service_base_url as zl_url, web_enabled as zl_on,
        )
        if zl_on(config):
            targets["zalo"] = zl_url(config)
    except Exception:  # noqa: BLE001
        logger.debug("[readiness] 解析 zalo sidecar 地址失败", exc_info=True)
    try:
        from src.integrations.instagram_web_login import (
            service_base_url as ig_url, web_enabled as ig_on,
        )
        if ig_on(config):
            targets["instagram"] = ig_url(config)
    except Exception:  # noqa: BLE001
        logger.debug("[readiness] 解析 instagram sidecar 地址失败", exc_info=True)
    try:
        # QQ 协议端（用户自装，Milky 接口）：探 get_impl_info 而非 /health（Milky 无 /health）
        from src.integrations.qq_milky import (
            protocol_enabled as qq_on, service_base_url as qq_url,
        )
        if qq_on(config):
            targets["qq"] = qq_url(config)
    except Exception:  # noqa: BLE001
        logger.debug("[readiness] 解析 qq 协议端地址失败", exc_info=True)
    return targets


def _telegram_runtime_blockers(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Telegram 协议栈公共依赖（pyrogram + 凭据/池）——扫码与手机号登录共用。"""
    from src.integrations.telegram_protocol_login import (
        is_pyrogram_available, resolve_credentials,
    )
    out: List[Dict[str, Any]] = []
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


def _telegram_protocol_blockers(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    from src.integrations.telegram_protocol_login import protocol_enabled
    out: List[Dict[str, Any]] = []
    if not protocol_enabled(config):
        out.append(_blocker(BLOCK_NOT_ENABLED))
    out += _telegram_runtime_blockers(config)
    return out


def _telegram_phone_blockers(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """手机号登录与扫码共用 pyrogram/凭据，但开关是独立的 ``phone_enabled``。"""
    from src.integrations.telegram_phone_login import phone_enabled
    out: List[Dict[str, Any]] = []
    if not phone_enabled(config):
        out.append(_blocker(BLOCK_NOT_ENABLED))
    out += _telegram_runtime_blockers(config)
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
    """WhatsApp(protocol) / Messenger(web) / Zalo(web) / Instagram(web) 共用：开关 + sidecar 可达性。"""
    out: List[Dict[str, Any]] = []
    if platform == "whatsapp":
        from src.integrations.whatsapp_baileys_login import (
            protocol_enabled as on, service_base_url as url_of,
        )
        svc, not_enabled_code = "whatsapp-baileys", BLOCK_NOT_ENABLED
    elif platform == "zalo":
        from src.integrations.zalo_personal_login import (
            service_base_url as url_of, web_enabled as on,
        )
        # Zalo 个人号同 Messenger：开开关还不够，须先起 Node 边车并用手机扫码登录一次，
        # 属「需运维配置」，与忘了开开关分开说。
        svc, not_enabled_code = "zalo-personal", BLOCK_NEEDS_SERVER_SETUP
    elif platform == "instagram":
        from src.integrations.instagram_web_login import (
            service_base_url as url_of, web_enabled as on,
        )
        # IG 个人号同 Messenger：须先起 Playwright 边车并在服务器隔离浏览器内登录一次。
        svc, not_enabled_code = "instagram-web", BLOCK_NEEDS_SERVER_SETUP
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


def _qq_protocol_blockers(
    config: Dict[str, Any], service_ok: Optional[bool],
) -> List[Dict[str, Any]]:
    """QQ 协议登录（个人号）：开关 + 自研连接边车可达性 + 本机 QQ 是否已安装。

    连接边车（``services/qq-personal``）随桌面壳打包、由壳自动拉起，用户不需自装任何东西，
    故「开关没开」＝单纯没启用（``BLOCK_NOT_ENABLED``，翻开关即可）。边车起来了但探测到本机
    未安装 QQ → ``qq_not_installed``（可操作：一键下载 QQ）；边车不可达 → ``service_down``
    （可操作：重启连接服务）。QQ 安装态由边车 ``/health.qq_installed`` 透出，经
    ``check_qq_milky_reachable`` 的探测结果传入 ``service_ok`` 之外的 ``qq_installed`` 旁路。
    """
    from src.integrations.qq_milky import protocol_enabled, service_base_url
    out: List[Dict[str, Any]] = []
    if not protocol_enabled(config):
        out.append(_blocker(BLOCK_NOT_ENABLED, svc="qq-personal"))
    elif service_ok is False:
        out.append(_blocker(BLOCK_SERVICE_DOWN, svc="qq-personal", url=service_base_url(config)))
    elif service_ok is None:
        # 探测未跑（如 diagnose_mode 未传 service_ok）——不误报，交由前端 poll 实时判定
        pass
    return out


def _official_blockers(platform: str, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """official 模式（Instagram/Zalo 官方 API）：凭证与开关就绪判定。

    单一事实源＝`channel_setup` 的渠道声明（必填字段清单与向导表单同一份），
    缺哪个字段就点名哪个——「去接入向导填 X」比笼统「未启用」可操作得多。
    """
    out: List[Dict[str, Any]] = []
    try:
        from src.utils.channel_setup import _dig, _is_placeholder, get_channel
        ch = get_channel(platform)
        if ch is None:
            return [_blocker(BLOCK_NOT_ENABLED)]
        missing = [
            f.label for f in ch.fields
            if f.required and _is_placeholder(_dig(config or {}, f.key))
        ]
        if missing:
            # params 只带纯字段名（后端吐码与参数、前端出文案的 i18n 约定——
            # 「缺少」这类散文在 rc_official_creds_missing_why 的文案模板里，
            # 否则英文界面会中英夹杂）。多字段用语言中立的 " / " 连接。
            out.append(_blocker(BLOCK_OFFICIAL_CREDS, field=" / ".join(missing)))
        elif not _dig(config or {}, ch.enable_key):
            # 凭证齐但渠道开关没开：处置同样是去向导（保存会自动置 enabled），
            # 沿用同一码（门禁 test_readiness_official_enabled_missing_uses_same_code
            # 钉住「码不该变」）；差异经结构化参数 state 传给前端选文案变体
            # （rc_official_switch_off_*），不再把中文散文塞进 params。
            out.append(_blocker(BLOCK_OFFICIAL_CREDS, state="switch_off"))
    except Exception:  # noqa: BLE001
        logger.debug("[readiness] official 凭证诊断失败", exc_info=True)
        out.append(_blocker(BLOCK_NOT_ENABLED))
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
            # 与 not_enabled 分码：这不是「开关没开」，是功能本身不存在——
            # 前端据此隐藏「重新检测」（永不就绪）并给「改用可用方式」的直达按钮。
            blockers.append(_blocker(BLOCK_NOT_IMPLEMENTED))
        else:
            try:
                if mode == "official":
                    blockers += _official_blockers(platform, config)
                elif platform == "telegram" and mode == "phone":
                    blockers += _telegram_phone_blockers(config)
                elif platform == "telegram":
                    blockers += _telegram_protocol_blockers(config)
                elif platform == "line":
                    blockers += _line_protocol_blockers(config)
                elif platform == "qq":
                    blockers += _qq_protocol_blockers(config, service_ok)
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
