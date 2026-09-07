"""协议栈联调自检（M6③）。

protocol 方式（Telegram pyrogram / WhatsApp Baileys）**无法在无真账号环境联调**，
本模块把「能不能跑起来」收敛成一份**结构化就绪报告**，供：
- CLI ``scripts/protocol_doctor.py`` 一键自检；
- API ``GET /api/accounts/protocol/readiness`` 供前端展示；
- 单测（纯函数 + 可注入，静态部分零网络）。

报告分两层：``readiness_static`` 只读配置 + 进程内状态（不触网，秒回）；``readiness``
在其上补一次 WhatsApp Node 可达性探测（异步）。每项都带 ``ready`` 与人话 ``hints``。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _telegram_report(config: Dict[str, Any]) -> Dict[str, Any]:
    from src.integrations.telegram_protocol_login import (
        is_pyrogram_available, protocol_enabled, resolve_credentials,
    )
    enabled = protocol_enabled(config)
    pyrogram = is_pyrogram_available()
    creds = resolve_credentials(config) is not None
    hints: List[str] = []
    if not enabled:
        hints.append("未启用：config.platform_login.telegram.protocol_enabled: true")
    if not pyrogram:
        hints.append("缺少 pyrogram：pip install pyrogram tgcrypto")
    if not creds:
        hints.append("缺少 api_id/api_hash：填 config.telegram.api_id / api_hash")
    ready = enabled and pyrogram and creds
    if ready:
        hints.append("就绪：用 scripts/create_pyrogram_session.py 生成测试号 session 后扫码新增")
    return {
        "mode_enabled": enabled,
        "pyrogram_available": pyrogram,
        "credentials": creds,
        "ready": ready,
        "hints": hints,
    }


def _whatsapp_report_static(config: Dict[str, Any]) -> Dict[str, Any]:
    from src.integrations.whatsapp_baileys_login import (
        protocol_enabled, service_base_url,
    )
    enabled = protocol_enabled(config)
    url = service_base_url(config)
    hints: List[str] = []
    if not enabled:
        hints.append("未启用：config.platform_login.whatsapp.protocol_enabled: true")
    hints.append(f"Baileys 微服务地址：{url}（cd services/whatsapp-baileys && npm install && node server.js）")
    return {
        "mode_enabled": enabled,
        "service_url": url,
        "service_reachable": None,   # 由 readiness() 异步补
        "ready": enabled,            # 静态层只能判到「已启用」，可达性见异步层
        "hints": hints,
    }


def _orchestrator_report() -> Dict[str, Any]:
    try:
        from src.integrations.account_orchestrator import _orchestrator
        if _orchestrator is None:
            return {"instantiated": False, "loop_running": False, "managed_total": 0}
        st = _orchestrator.status()
        return {
            "instantiated": True,
            "loop_running": bool(st.get("running_loop")),
            "managed_total": int(st.get("total") or 0),
            "by_state": st.get("by_state") or {},
        }
    except Exception:
        logger.debug("[diag] 读取编排器状态失败", exc_info=True)
        return {"instantiated": False, "loop_running": False, "managed_total": 0}


def _ingest_report() -> Dict[str, Any]:
    try:
        from src.integrations.protocol_bridge import get_inbox_sink
        return {"sink_registered": get_inbox_sink() is not None}
    except Exception:
        return {"sink_registered": False}


_SELF_PROFILE_EMPTY_STATS: Dict[str, int] = {
    "calls": 0, "written": 0, "skipped": 0,
    "avatar_downloaded": 0, "avatar_reused": 0, "errors": 0,
}


def _self_profile_report(config: Dict[str, Any]) -> Dict[str, Any]:
    """账号身份采集（self_profile）就绪度：发现「开关开了但实际没生效」。

    判定：开关未开 → ready=True（未启用不算故障）；开关已开 → 至少成功富集
    （written）或幂等跳过（skipped）过一次才算就绪。不参与 overall_ready
    （身份采集是增强项，非协议栈可用性的必要条件）。
    """
    from src.integrations.account_self_profile import (
        get_self_profile_stats, self_avatar_enabled, self_profile_enabled,
    )
    flag = self_profile_enabled(config)
    avatar = self_avatar_enabled(config)
    try:
        stats = dict(get_self_profile_stats())
    except Exception:
        logger.debug("[diag] 读取 self_profile 计数失败", exc_info=True)
        stats = dict(_SELF_PROFILE_EMPTY_STATS)
    calls = int(stats.get("calls") or 0)
    written = int(stats.get("written") or 0)
    skipped = int(stats.get("skipped") or 0)
    errors = int(stats.get("errors") or 0)
    ready = True if not flag else (written > 0 or skipped > 0)
    hints: List[str] = []
    if not flag:
        hints.append(
            "未启用账号身份采集：config.accounts.self_profile.enabled: true"
            "（开启后登录/重连自动提取账号昵称头像）"
        )
    elif calls == 0:
        hints.append(
            "已启用但从未触发富集：确认已扫码登录且 Baileys/后端已重启"
            "（登录轮询/session-status/健康轮询三路任一会触发）"
        )
    elif written == 0 and skipped == 0 and errors > 0:
        hints.append(
            f"富集持续失败（errors={errors}）：查看后端日志 [self_profile]，"
            "可能 Node 微服务未回传 pushname/avatar_url 字段"
        )
    if flag and ready:
        hints.append(f"就绪：已富集 written={written} / 跳过 skipped={skipped}")
    if flag and avatar and written > 0 \
            and int(stats.get("avatar_downloaded") or 0) == 0:
        hints.append("头像子开关已开但未下载过头像：可能账号无头像或 Node 未回传 avatar_url")
    return {
        "flag_enabled": flag,
        "avatar_enabled": avatar,
        "stats": stats,
        "ready": ready,
        "hints": hints,
    }


def _platform_matrix(
    config: Dict[str, Any], service_results: Optional[Dict[str, bool]] = None,
) -> Dict[str, Any]:
    """四平台 × 各登录方式的结构化诊断（与接入弹窗同一口径）。

    与上面 `_telegram_report` / `_whatsapp_report_static` 的人话 hints 并存：hints 面向
    运维读日志，本矩阵面向前端渲染「为什么不能用 / 该怎么办」。二者共用底层判定函数，
    但**结构化的那份才是弹窗的数据源**，避免两处口径分裂。
    """
    try:
        from src.integrations.platform_login import DEFAULT_PLATFORM_MODES
        from src.integrations.platform_readiness import diagnose_platform
    except Exception:  # noqa: BLE001
        logger.debug("[diag] 平台诊断矩阵不可用", exc_info=True)
        return {}
    svc = service_results or {}
    out: Dict[str, Any] = {}
    for plat, pdef in DEFAULT_PLATFORM_MODES.items():
        modes = list(pdef.get("modes") or [])
        if not modes:
            continue
        try:
            # 刻意不传 provider_registered_fn：本报告可能在 CLI / 后台进程生成，
            # 那里 provider 注册表恒空，据此判定会把「依赖齐全」误报成「未启用」。
            out[plat] = diagnose_platform(
                plat, modes, config, service_ok=svc.get(plat))
        except Exception:  # noqa: BLE001
            logger.debug("[diag] 诊断平台 %s 失败", plat, exc_info=True)
    return out


def readiness_static(config: Dict[str, Any]) -> Dict[str, Any]:
    """只读配置 + 进程内状态的就绪报告（不触网）。"""
    cfg = config or {}
    pl = cfg.get("platform_login", {}) or {}
    tg = _telegram_report(cfg)
    wa = _whatsapp_report_static(cfg)
    orch = _orchestrator_report()
    ingest = _ingest_report()
    overall = bool(
        pl.get("enabled")
        and (tg["ready"] or wa["ready"])
        and ingest["sink_registered"]
    )
    return {
        "platform_login_enabled": bool(pl.get("enabled")),
        "orchestrator_enabled": bool(pl.get("orchestrator_enabled")),
        "telegram": tg,
        "whatsapp": wa,
        "orchestrator": orch,
        "inbox_ingest": ingest,
        # 增强项：不参与 overall_ready（见 _self_profile_report 文档）
        "self_profile": _self_profile_report(cfg),
        # 四平台诊断矩阵（LINE / Messenger 也在内）。overall_ready 刻意仍只看 TG/WA
        # ——它是「协议栈」口径的既有契约，扩语义会静默改变已有告警与门禁的判定。
        "platforms": _platform_matrix(cfg),
        "overall_ready": overall,
    }


async def check_whatsapp_reachable(config: Dict[str, Any]) -> bool:
    """探测 Baileys Node 的 /health（best-effort，失败/超时返回 False）。"""
    from src.integrations.whatsapp_baileys_login import _get_json, service_base_url
    try:
        res = await _get_json(f"{service_base_url(config)}/health", timeout=5.0)
        return bool((res or {}).get("ok", False))
    except Exception:
        return False


async def check_messenger_reachable(config: Dict[str, Any]) -> bool:
    """探测 messenger-web Node 的 /health（与 WhatsApp 对称，best-effort）。"""
    from src.integrations.messenger_web_login import _get_json, service_base_url
    try:
        res = await _get_json(f"{service_base_url(config)}/health", timeout=5.0)
        return bool((res or {}).get("ok", False))
    except Exception:
        return False


async def check_qq_milky_reachable(config: Dict[str, Any]) -> bool:
    """探测 QQ 协议端（Milky）：``get_impl_info`` 有回即可达（Milky 无 /health；未登录也算
    「协议端在」——登没登录由 get_login_info / worker 另判）。best-effort。"""
    from src.integrations.qq_milky import MilkyClient, MilkyError, service_base_url, service_token
    client = MilkyClient(service_base_url(config), service_token(config), timeout=5.0)
    try:
        await client.call("get_impl_info")
        return True
    except MilkyError as ex:
        # 协议端在但拒绝（token 不对 / 未登录 / 不支持该 API）——服务本身可达
        return ex.retcode in (-403, -401, -404)
    except Exception:
        return False


# sidecar 探测结果缓存：{platform: (ts, ok)}。接入弹窗每次打开都要一份可达性，
# 不缓存就是每次开弹窗多付两次 HTTP 往返（服务挂着时还要等满超时）。
_SERVICE_PROBE_CACHE: Dict[str, Any] = {}
_SERVICE_PROBES = {
    "whatsapp": lambda cfg: check_whatsapp_reachable(cfg),
    "messenger": lambda cfg: check_messenger_reachable(cfg),
    "qq": lambda cfg: check_qq_milky_reachable(cfg),
}


async def probe_services(
    config: Dict[str, Any], *, ttl: float = 30.0, force: bool = False,
) -> Dict[str, bool]:
    """并行探测已启用的 sidecar（带 TTL 缓存）。返回 ``{platform: reachable}``。

    只探「该方式已启用」的服务（见 `platform_readiness.service_probe_targets`）——
    对没打算用的功能去探端口，白等一次超时还会把它误报成「服务挂了」。
    """
    from src.integrations.platform_readiness import service_probe_targets
    targets = service_probe_targets(config)
    now = time.time()
    out: Dict[str, bool] = {}
    todo: List[str] = []
    for plat in targets:
        hit = _SERVICE_PROBE_CACHE.get(plat)
        if not force and hit and (now - hit[0]) < ttl:
            out[plat] = bool(hit[1])
        else:
            todo.append(plat)
    if todo:
        results = await asyncio.gather(
            *[_SERVICE_PROBES[p](config) for p in todo if p in _SERVICE_PROBES],
            return_exceptions=True,
        )
        for plat, res in zip([p for p in todo if p in _SERVICE_PROBES], results):
            ok = bool(res) if not isinstance(res, BaseException) else False
            _SERVICE_PROBE_CACHE[plat] = (now, ok)
            out[plat] = ok
    return out


async def readiness(config: Dict[str, Any]) -> Dict[str, Any]:
    """完整就绪报告：静态报告 + sidecar 可达性探测（WhatsApp / Messenger）。"""
    report = readiness_static(config)
    wa = report["whatsapp"]
    wa_on = bool(wa.get("mode_enabled"))
    mg_on = False
    try:
        from src.integrations.messenger_web_login import web_enabled as _mg_on
        mg_on = bool(_mg_on(config))
    except Exception:  # noqa: BLE001
        logger.debug("[diag] 读取 messenger 开关失败", exc_info=True)

    # 两个 sidecar 并行探：单次超时 5s，串行时「两个都挂」要等满 10s——而 ops 卡每 60s
    # 刷一次，这 10s 全落在运维眼前。并行后最差仍是 5s。
    tasks, order = [], []
    if wa_on:
        tasks.append(check_whatsapp_reachable(config)); order.append("whatsapp")
    if mg_on:
        tasks.append(check_messenger_reachable(config)); order.append("messenger")
    svc: Dict[str, bool] = {}
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for name, res in zip(order, results):
            svc[name] = bool(res) if not isinstance(res, BaseException) else False

    if wa_on:
        reachable = svc.get("whatsapp", False)
        wa["service_reachable"] = reachable
        wa["ready"] = bool(wa_on and reachable)
        if not reachable:
            wa["hints"].append("服务不可达：确认 Baileys Node 已启动且 baileys_url 正确")
        # 可达性影响整体（WA 启用却不可达 → 整体未就绪）
        report["overall_ready"] = bool(
            report["platform_login_enabled"]
            and (report["telegram"]["ready"] or wa["ready"])
            and report["inbox_ingest"]["sink_registered"]
        )
    if svc:
        report["platforms"] = _platform_matrix(config, svc)
    report["services"] = svc
    return report


def format_report(report: Dict[str, Any]) -> str:
    """把就绪报告渲染成 CLI 友好文本（✅/⚠️/❌）。"""
    def mark(ok: Any) -> str:
        if ok is True:
            return "✅"
        if ok is None:
            return "⏳"
        return "❌"

    lines: List[str] = []
    lines.append(f"{mark(report['overall_ready'])} 协议栈整体就绪")
    lines.append(f"   platform_login.enabled = {report['platform_login_enabled']}")
    lines.append(f"   orchestrator_enabled   = {report['orchestrator_enabled']}")

    tg = report["telegram"]
    lines.append(f"\n{mark(tg['ready'])} Telegram protocol")
    lines.append(f"   mode_enabled={tg['mode_enabled']} pyrogram={tg['pyrogram_available']} creds={tg['credentials']}")
    for h in tg["hints"]:
        lines.append(f"   - {h}")

    wa = report["whatsapp"]
    lines.append(f"\n{mark(wa['ready'])} WhatsApp Baileys")
    lines.append(f"   mode_enabled={wa['mode_enabled']} reachable={wa['service_reachable']}")
    for h in wa["hints"]:
        lines.append(f"   - {h}")

    orch = report["orchestrator"]
    lines.append(f"\n{mark(orch['loop_running'])} 编排器")
    lines.append(f"   instantiated={orch['instantiated']} loop={orch['loop_running']} managed={orch['managed_total']}")

    ing = report["inbox_ingest"]
    lines.append(f"\n{mark(ing['sink_registered'])} 收件箱入站 sink: registered={ing['sink_registered']}")

    mtx = report.get("platforms") or {}
    if mtx:
        lines.append("\n── 各平台登录方式 ──")
        for plat in sorted(mtx):
            pr = mtx[plat]
            lines.append(f"{mark(pr.get('ready'))} {plat}")
            for m, d in (pr.get("modes") or {}).items():
                bks = d.get("blockers") or []
                tail = ""
                if bks:
                    tail = "  ← " + ", ".join(
                        b["code"] + ("!" if b["severity"] == "block" else "?")
                        for b in bks
                    )
                lines.append(f"   {mark(d.get('ready'))} {m}{tail}")

    sp = report["self_profile"]
    sps = sp["stats"]
    lines.append(f"\n{mark(sp['ready'])} 账号身份采集(self_profile)")
    lines.append(
        f"   flag_enabled={sp['flag_enabled']} avatar={sp['avatar_enabled']}"
        f" written={sps.get('written', 0)} calls={sps.get('calls', 0)}"
    )
    for h in sp["hints"]:
        lines.append(f"   - {h}")
    return "\n".join(lines)
