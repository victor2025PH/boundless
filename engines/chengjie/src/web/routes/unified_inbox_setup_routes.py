"""统一收件箱——渠道接入向导路由域（P1-1）。

给后台管理员的引导式接入：选渠道 → 看「还缺什么」→ 填关键凭证（即时校验）→
写入凭证 overlay（config.local.yaml，保住主配置注释）→ 交棒现有扫码登录流程。

复用：
- ``channel_setup.channel_status`` 出每渠道现状（密钥打码回显）；
- ``ConfigManager.save_channel_credentials`` 落盘 overlay 并即时生效 + 自检；
- 现有 ``/api/platforms/{platform}/login/*`` 完成扫码（本域不重复实现）。

``register_setup_routes(app, *, api_auth, config_manager)`` 挂 ``/api/setup/*``（管理员）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from fastapi import Request

from src.web.routes.unified_inbox_auth import _require_supervisor
from src.web.routes.unified_inbox_services import _inbox_store
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)

# 主对话链模式（ai.primary）：与 AIClient._primary_mode 词表对齐。
_AI_PRIMARY_MODES = frozenset({"cloud", "local", "local_only"})


def _local_endpoint_ready(ai_cfg: Dict[str, Any]) -> bool:
    """``ai.fallback`` 是否齐备到足以承担本地主链（enabled + base_url + model）。"""
    fb = (ai_cfg or {}).get("fallback") or {}
    if not isinstance(fb, dict) or not fb.get("enabled"):
        return False
    return bool(str(fb.get("base_url") or "").strip() and str(fb.get("model") or "").strip())


def _ai_primary_snapshot(config_manager, ai_client=None) -> Dict[str, Any]:
    """主对话模式快照（运营可见，零密钥）。

    ``configured``＝overlay/主配置声明值；``effective``＝运行时 AIClient 实际值
    （声明 local* 但端点缺 → 启动时退回 cloud，两者会分叉——看板必须显式对照）。
    """
    config = getattr(config_manager, "config", None) or {} if config_manager else {}
    ai = config.get("ai") or {}
    configured = str(ai.get("primary") or "cloud").strip().lower()
    if configured not in _AI_PRIMARY_MODES:
        configured = "cloud"
    local_ready = _local_endpoint_ready(ai)
    fb = ai.get("fallback") if isinstance(ai.get("fallback"), dict) else {}
    effective = configured
    if ai_client is not None:
        effective = str(getattr(ai_client, "_primary_mode", None) or configured).strip().lower()
        if effective not in _AI_PRIMARY_MODES:
            effective = "cloud"
    return {
        "configured": configured,
        "effective": effective,
        "local_ready": local_ready,
        "local_model": str((fb or {}).get("model") or "").strip() or None,
        "divergent": configured != effective,
    }


def _usage_tier_group(tier: str) -> str:
    """llm_cost tier → 出话分布分组。

    ``local_primary`` 必须独立成组——并进 ``primary`` 会让本地优先部署的看板
    看起来「全是云主链」，隐私/算力归因双失真。
    """
    t = str(tier or "default")
    if t in ("key_pool", "local_fallback", "local_primary"):
        return t
    return "primary"


def _count_online_agents(request: Request) -> int:
    """近 120s 内 status=online 的坐席数（上线清单「坐席在线」信号）。"""
    try:
        inbox = _inbox_store(request)
        if inbox is None or not hasattr(inbox, "list_agent_presence"):
            return 0
        rows = inbox.list_agent_presence(active_within_sec=120)
        return sum(1 for r in rows if str(r.get("status") or "") == "online")
    except Exception:
        logger.debug("统计在线坐席失败（已忽略）", exc_info=True)
        return 0


def _kb_readiness_for(config_manager) -> dict:
    """取 KB 冷启动现状（无 store 时返回不可用）。"""
    try:
        from src.utils.kb_registry import get_kb_store
        from src.utils.kb_starter import kb_readiness
        kb = get_kb_store(config_manager, require_exists=True)
        return kb_readiness(kb)
    except Exception:
        logger.debug("读取 KB readiness 失败（已忽略）", exc_info=True)
        return {"available": False, "is_cold": True, "enabled_entries": 0}


def _channel_health_snapshot() -> Dict[str, Any]:
    """平台通道离线快照（坐席工作台「通道离线」警示横幅数据源，P0-2 延伸）。

    读 ``platform_session_health.unhealthy_sessions()``（进程级内存登记表，零 IO），
    把 ``platform:acct`` key 拆成结构化条目并按已离线时长降序（最久的排最前 =
    横幅首条明细）。任何异常吞掉返回空快照——本函数挂在坐席状态条轮询接口上，
    绝不能反过来把它拖垮（坐席端全靠该接口）。

    ``logged_out``（运营主动登出 / 启动自注册表种子）**不进横幅**——那不是故障，
    收件箱账号 chip 已有「已退出」语义；横幅只催 ``needs_login`` / ``expired`` /
    ``failed`` 这类「该有人去修」的意外掉线。
    """
    try:
        from src.integrations.platform_session_health import (
            ensure_seeded_from_registry, get_platform_session_health,
        )
        ensure_seeded_from_registry()
        now = time.time()
        items = []
        for key, sess in get_platform_session_health().unhealthy_sessions().items():
            st = str(sess.get("status") or "")
            if st == "logged_out":
                continue
            platform, _, account_id = str(key).partition(":")
            since = (float(sess.get("unhealthy_since") or 0.0)
                     or float(sess.get("ts") or 0.0) or now)
            items.append({
                "platform": platform,
                "account_id": account_id,
                "status": st,
                "down_min": int(max(0.0, now - since) // 60),
            })
        items.sort(key=lambda it: -it["down_min"])
        return {"unhealthy": items, "count": len(items)}
    except Exception:
        logger.debug("平台通道健康快照失败（已忽略）", exc_info=True)
        return {"unhealthy": [], "count": 0}


def _session_present(request: Request) -> bool:
    """请求是否携带已登录 session（区别于桌面壳主进程的纯 Bearer 调用）。"""
    try:
        if "session" in request.scope:
            s = request.session
            return bool(s.get("user_id") or s.get("auth"))
    except Exception:
        pass
    return False


def _require_supervisor_or_shell(request: Request) -> None:
    """AI 凭证端点守卫：session 用户须主管角色；纯 Bearer（桌面壳主进程，
    api_auth 已验 admin token = master 等价）放行。"""
    if _session_present(request):
        _require_supervisor(request)


async def reload_ai_runtime(app, config_manager) -> bool:
    """P0-1：AI 凭证落盘后热重建 AIClient 并换绑运行中服务（best-effort，绝不抛）。

    覆盖：``app.state.ai_client`` / ``translation_service``（含路由内 AIEngine）/
    ``chat_assistant_service`` / ``skill_manager.ai_client``——即「填 Key → 翻译/智能
    回复生效」主链路免重启。其余在启动期快照旧 client 的子系统（companion worker 等）
    仍需重启进程；初始化失败（key 无效/网络不通）时**不换绑**，保持旧 client。
    返回：新 client 连接自检是否通过（可直接当「翻译就绪」绿灯）。
    """
    try:
        from src.ai.ai_client import AIClient
        client = AIClient(config_manager)
        if not bool(await client.initialize()):
            return False
        app.state.ai_client = client
        svc = getattr(app.state, "translation_service", None)
        if svc is not None and hasattr(svc, "rebind_ai_client"):
            svc.rebind_ai_client(client)
        cas = getattr(app.state, "chat_assistant_service", None)
        if cas is not None and hasattr(cas, "ai_client"):
            cas.ai_client = client
        sm = getattr(app.state, "skill_manager", None)
        if sm is not None and hasattr(sm, "ai_client"):
            sm.ai_client = client
        return True
    except Exception:
        logger.warning("AI runtime 热重建失败（已忽略；重启后生效）", exc_info=True)
        return False


def _provision_official_account(channel_id: str, config: Dict[str, Any]) -> str:
    """纯官方 API 渠道（Instagram/Zalo）凭证就绪后，自动在账号注册表开通
    ``mode=official`` 账号行（幂等 upsert）。

    没有这一行：官方 worker 工厂注册了、webhook 也在收消息，但编排器的期望集
    （``desired_accounts`` 只认注册表 ``status=online``）里没有它 → 出站 worker
    永不拉起，坐席在收件箱看得见客户消息却发不出去，且没有任何一处会提示为什么。
    account_id 必须与 webhook 入站镜像口径一致（见 Channel.official_account_id_key）。

    返回开通的 account_id；非官方渠道 / 必填凭证未齐 / 失败一律返回 ""（best-effort，
    绝不阻塞凭证保存本身）。
    """
    try:
        from src.utils.channel_setup import _dig, _required_ready, get_channel
        ch = get_channel(channel_id)
        if ch is None or not ch.official_platform:
            return ""
        # 必填凭证未齐（如只填了一半）不开账号：开了也起不来，反而在账号栏挂个错误行
        if not _required_ready(ch, {}, config):
            return ""
        account_id = ""
        if ch.official_account_id_key:
            account_id = str(_dig(config, ch.official_account_id_key) or "").strip()
        account_id = account_id or "official"
        from src.integrations.account_registry import get_account_registry
        get_account_registry().upsert(
            ch.official_platform, account_id, mode="official", status="online",
            merge_meta=True)
        return account_id
    except Exception:
        logger.debug("official 账号自动开通失败（已忽略；可手动重试保存）", exc_info=True)
        return ""


async def _maybe_probe_messenger_page(
    channel_id: str, config_manager: Any,
) -> Optional[Dict[str, Any]]:
    """Messenger 官方渠道保存后的 Graph ``/me`` 探针（best-effort，2026-08-10）。

    ① 验 token 真伪：抄错一个字符当场在保存响应里暴露（含 Graph 原始报错），
       而不是等第一次真实出站失败才发现；
    ② 成功顺带带回主页身份（page_id/name/picture）——config 缺 page_id 时自动
       回填 overlay（用户不必去 Meta 后台抄 id），并让紧随其后的
       ``_provision_official_account`` 开出的账号行 id 与 webhook 入站镜像口径
       （``page_id or "official"``）天然一致，堵住「先存一半、后补 page_id →
       新旧账号行分裂」的边界；
    ③ 探针失败（网络/鉴权）只随响应报告，**绝不阻塞保存本身**——离线环境照样
       能把凭证存进去。

    返回探针结果 dict；非 messenger 渠道 / token 未填 / 探针异常返回 None
    （响应里不出现 ``probe`` 字段＝前端不渲染，旧行为零变化）。
    """
    if str(channel_id or "").lower() != "messenger":
        return None
    try:
        cfg = getattr(config_manager, "config", None) or {}
        block = cfg.get("facebook_messenger") or {}
        token = str(block.get("page_access_token") or "").strip()
        if not token:
            return None
        from src.integrations.facebook_webhook import fb_probe_page
        probe = await fb_probe_page(token, timeout_sec=6.0)
        if (probe.get("ok") and probe.get("page_id")
                and not str(block.get("page_id") or "").strip()):
            try:
                config_manager.save_channel_credentials(
                    "messenger", {"page_id": str(probe["page_id"])})
            except Exception:
                logger.debug("page_id 自动回填失败（已忽略）", exc_info=True)
        return probe
    except Exception:
        logger.debug("messenger 保存探针失败（已忽略）", exc_info=True)
        return None


async def _probe_public_media_url(url: str) -> Dict[str, Any]:
    """公网媒体 URL 自检（best-effort）：本机向该地址发一次 GET，抓 DNS 拼错/隧道
    掉线/TLS 坏/端口不通这几类最常见配置错误。

    语义边界（如实告知，不装成完整验证）：本机可达 ≠ 平台可达（Meta/LINE 是从公网
    访问），但本机不可达则平台几乎必不可达——结果只作提示，绝不拦截保存。
    """
    u = str(url or "").strip().rstrip("/")
    if not u:
        return {"state": "unset", "detail": ""}
    if not u.lower().startswith("https://"):
        # LINE 音频 originalContentUrl / Meta 附件拉取都强制 https，http 填了也白填
        return {"state": "fail", "detail": "must_be_https"}
    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=4)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(u + "/login", allow_redirects=True) as resp:
                return {"state": "ok" if resp.status < 500 else "fail",
                        "detail": f"http_{resp.status}"}
    except Exception as ex:  # noqa: BLE001
        return {"state": "fail", "detail": type(ex).__name__}


def _accounts_by_platform() -> dict:
    """各平台已接入的账号数（账号注册表，不含 removed）。

    向导「就绪」判定的第一手信号：扫码/协议登录进来的账号**不写任何渠道 yaml 键**，
    只看配置就会把一台正在收发消息的机器报成「已就绪 0/4」（实机反馈）。
    取数失败一律返回空 → 判定退回纯配置口径，绝不让向导因此报错。
    """
    try:
        from src.integrations.account_registry import get_account_registry
        out: dict = {}
        for row in get_account_registry().list():
            p = str(row.get("platform") or "").lower()
            if p:
                out[p] = out.get(p, 0) + 1
        return out
    except Exception:
        logger.debug("读取账号注册表失败（向导退回配置口径）", exc_info=True)
        return {}


def _login_ready(config: dict) -> dict:
    """各平台「扫码/协议登录这条路今天通不通」（复用登录弹窗那套诊断，单一事实源）。"""
    try:
        from src.integrations.platform_login import (
            DEFAULT_PLATFORM_MODES, mode_available)
        from src.integrations.platform_readiness import diagnose_platform
        pl_cfg = (config or {}).get("platform_login") or {}
        out: dict = {}
        for platform, pdef in DEFAULT_PLATFORM_MODES.items():
            modes = ((pl_cfg.get(platform) or {}).get("modes")) or pdef["modes"]
            if not modes:
                continue
            rep = diagnose_platform(
                platform, modes, config or {},
                provider_registered_fn=mode_available)
            out[platform] = bool(rep.get("ready"))
        return out
    except Exception:
        logger.debug("平台登录就绪诊断失败（向导按可用呈现）", exc_info=True)
        return {}


def register_setup_routes(app, *, api_auth, config_manager=None) -> None:
    """挂载渠道接入向导端点（/api/setup/channels[/{channel}]）。"""

    @app.get("/api/setup/channels")
    async def api_setup_channels(request: Request, probe_media: int = 0):
        """所有渠道的接入现状（两条接入路径各自状态 + 「能不能收发消息」完成度）。

        ``probe_media=1``：顺带对 ``official_media.public_base_url`` 做一次可达性
        自检（IG/LINE 官方通道发媒体的前置；默认不探，向导「检测」按钮触发）。
        """
        api_auth(request)
        _require_supervisor(request)
        from src.utils.channel_setup import channel_status
        config = getattr(config_manager, "config", None) or {}
        channels = channel_status(
            config,
            accounts_by_platform=_accounts_by_platform(),
            login_ready=_login_ready(config),
        )
        ready = sum(1 for c in channels if c["ready"])
        out: Dict[str, Any] = {"ok": True, "channels": channels,
                               "ready_count": ready, "total": len(channels)}
        # official 媒体 URL 状态：只有在「需要它的渠道」（IG/LINE 官方）已配凭证
        # 或 URL 已填时才有意义；前端两者皆空则不渲染该块。
        try:
            from src.integrations.official_api_worker import (
                OFFICIAL_MEDIA_URL_PLATFORMS,
            )
            base = str((((config or {}).get("official_media") or {})
                        .get("public_base_url")) or "").strip()
            needed = [c["id"] for c in channels
                      if c["id"] in OFFICIAL_MEDIA_URL_PLATFORMS
                      and c.get("configured")]
            media_block: Dict[str, Any] = {"url": base, "needed_by": needed}
            if probe_media:
                media_block["probe"] = await _probe_public_media_url(base)
            out["official_media"] = media_block
        except Exception:
            logger.debug("official_media 状态汇总失败（已忽略）", exc_info=True)
        return out

    @app.get("/api/setup/checklist")
    async def api_setup_checklist(request: Request):
        """上线自检清单：AI/渠道/配置/知识库/坐席 就绪信号 + 总体红绿灯。"""
        api_auth(request)
        _require_supervisor(request)
        from src.utils.channel_setup import channel_status
        from src.utils.config_check import check_config
        from src.utils.golive import build_checklist
        config = getattr(config_manager, "config", None) or {}
        channels = channel_status(config)
        errors = warns = 0
        try:
            issues = check_config(
                config, config_path=getattr(config_manager, "config_path", None))
            errors = sum(1 for i in issues if i.severity == "error")
            warns = sum(1 for i in issues if i.severity == "warn")
        except Exception:
            logger.debug("清单内配置自检失败（已忽略）", exc_info=True)
        return build_checklist(
            config=config,
            channel_statuses=channels,
            config_errors=errors,
            config_warnings=warns,
            kb_ready=_kb_readiness_for(config_manager),
            online_agents=_count_online_agents(request),
        )

    @app.get("/api/setup/companion-preflight")
    async def api_setup_companion_preflight(request: Request):
        """Phase N：真号扫码陪聊上线前自检——开关一致性 + 反封号护栏就绪红绿灯。

        把 N-Line checklist §1/§4 的开关一致性固化成可机检的红绿灯，operator 扫码前先看。
        """
        api_auth(request)
        _require_supervisor(request)
        from src.ops.companion_preflight import build_companion_preflight
        config = getattr(config_manager, "config", None) or {}
        return build_companion_preflight(config)

    @app.get("/api/setup/ai")
    async def api_setup_ai_status(request: Request):
        """AI 大模型配置现状（key 打码回显；供首启向导/接入向导预填）。

        另回 ``primary`` 段（``ai.primary`` 声明值 / 运行时生效值 / 本地端点就绪），
        自托管「全本地」切换入口的读侧单一事实源。
        """
        api_auth(request)
        _require_supervisor_or_shell(request)
        from src.utils.golive import _is_placeholder
        config = getattr(config_manager, "config", None) or {}
        ai = config.get("ai") or {}
        key = str(ai.get("api_key") or "")
        configured = not _is_placeholder(key)
        masked = (key[:4] + "…" + key[-4:]) if len(key) > 12 else ("…" if key.strip() else "")
        ai_client = getattr(request.app.state, "ai_client", None)
        return {
            "ok": True,
            "configured": configured,
            "provider": str(ai.get("provider") or ""),
            "base_url": str(ai.get("base_url") or ""),
            "model": str(ai.get("model") or ""),
            "api_key_masked": masked,
            "primary": _ai_primary_snapshot(config_manager, ai_client),
        }

    @app.post("/api/setup/ai-primary")
    async def api_setup_ai_primary_save(request: Request):
        """切换主对话链模式（``ai.primary`` → overlay）并热重建 AI 运行时。

        body: ``{primary: "cloud"|"local"|"local_only"}``

        - ``local`` / ``local_only`` 前置：``ai.fallback`` 端点齐备，否则拒写
          （避免「写了却启动退回 cloud」的静默分叉）；
        - 写盘走 ``set_overlay_flag``（主 config 注释不动）；成功后
          ``reload_ai_runtime`` 热生效，免重启。
        """
        api_auth(request)
        _require_supervisor_or_shell(request)
        if config_manager is None:
            return {"ok": False, "detail": tr(request, "err.svc.config_manager_not_ready")}
        try:
            body: Dict[str, Any] = await request.json()
        except Exception:
            body = {}
        mode = str((body or {}).get("primary") or "").strip().lower()
        if mode not in _AI_PRIMARY_MODES:
            return {"ok": False, "detail": tr(request, "err.setup.ai_primary_invalid")}
        ai_cfg = ((getattr(config_manager, "config", None) or {}).get("ai")) or {}
        if mode != "cloud" and not _local_endpoint_ready(ai_cfg):
            return {"ok": False, "detail": tr(request, "err.setup.ai_primary_need_local")}
        ok, msg = config_manager.set_overlay_flag("ai.primary", mode)
        if not ok:
            return {"ok": False, "detail": tr(
                request, "err.setup.ai_primary_save_failed", reason=msg)}
        ai_ready = await reload_ai_runtime(request.app, config_manager)
        ai_client = getattr(request.app.state, "ai_client", None)
        return {
            "ok": True,
            "detail": tr(request, "setup.ai_primary.saved"),
            "ai_ready": bool(ai_ready),
            "primary": _ai_primary_snapshot(config_manager, ai_client),
        }

    @app.post("/api/setup/ai-key")
    async def api_setup_ai_key_save(request: Request):
        """保存 AI 凭证到 overlay（config.local.yaml）并热重建 AI 运行时（P0-1 A2）。

        写 overlay 而非主 config.yaml：保住注释/结构，密钥不进 git 跟踪文件。
        成功后返回 ``ai_ready``（新 client 连接自检结果）——即「翻译就绪」绿灯。
        """
        api_auth(request)
        _require_supervisor_or_shell(request)
        if config_manager is None:
            return {"ok": False, "detail": tr(request, "err.svc.config_manager_not_ready")}
        try:
            body: Dict[str, Any] = await request.json()
        except Exception:
            body = {}
        from src.utils.golive import _is_placeholder
        api_key = str((body or {}).get("api_key") or "").strip()
        if _is_placeholder(api_key):
            return {"ok": False, "detail": tr(request, "err.auth.api_key_required")}
        values = {
            "api_key": api_key,
            "provider": (body or {}).get("provider"),
            "base_url": (body or {}).get("base_url"),
            "model": (body or {}).get("model"),
        }
        ok, msg = config_manager.save_ai_credentials(values)
        if not ok:
            return {"ok": False, "detail": tr(request, "err.setup.ai_save_failed", reason=msg)}
        ai_ready = await reload_ai_runtime(request.app, config_manager)
        return {
            "ok": True,
            "detail": tr(request, "setup.ai.saved"),
            "ai_ready": bool(ai_ready),
            "provider": str(((config_manager.config or {}).get("ai") or {}).get("provider") or ""),
        }

    @app.get("/api/setup/cloud-credentials")
    async def api_setup_cloud_credentials(request: Request, probe: int = 0):
        """云端凭证总览（P-KeyPool）：主/备各 Key 余额水位 + 备用 Key 探活结果 +
        池运行态（冷却/顶班统计）。密钥一律打码，绝不回显全量。

        ``probe=1``：绕过 TTL/节流立即重探（余额 + chat ping），供「立即体检」按钮。
        """
        api_auth(request)
        _require_supervisor_or_shell(request)
        config = getattr(config_manager, "config", None) or {}
        out: Dict[str, Any] = {"ok": True}
        try:
            from src.utils.cloud_credentials import (
                collect_cloud_balances, credentials_config,
                ping_state_snapshot, run_chat_pings,
            )
            out["enabled"] = credentials_config(config)["enabled"]
            out["balances"] = collect_cloud_balances(config, force=bool(probe))
            if probe:
                run_chat_pings(config, force=True)
            out["pings"] = ping_state_snapshot()
        except Exception:
            logger.debug("cloud-credentials 汇总失败（已忽略）", exc_info=True)
            out.setdefault("enabled", False)
            out.setdefault("balances", [])
            out.setdefault("pings", {})
        # 出话分布（进程启动以来）：云主链 / 备用池 / 本地主链 / 本地兜底。
        # 每组带平均延迟（P3）——本地档「能用但慢」（.173 14B 全人设 prompt 实测 56s）
        # 必须让运营在看板直接看到，分层换模型才有读数依据。
        try:
            from src.ai.llm_cost import get_llm_cost
            usage: Dict[str, Dict[str, float]] = {}
            for row in (get_llm_cost().dump().get("rows") or []):
                group = _usage_tier_group(str(row.get("tier") or "default"))
                g = usage.setdefault(group, {"calls": 0, "tokens": 0, "cost_usd": 0.0,
                                             "latency_ms_sum": 0})
                g["calls"] += int(row.get("calls") or 0)
                g["tokens"] += int(row.get("prompt_tokens") or 0) + int(row.get("completion_tokens") or 0)
                g["cost_usd"] += float(row.get("cost_usd") or 0.0)
                g["latency_ms_sum"] += int(row.get("latency_ms_sum") or 0)
            for g in usage.values():
                g["latency_avg_ms"] = (
                    int(g["latency_ms_sum"] / g["calls"]) if g["calls"] else 0)
                g.pop("latency_ms_sum", None)
            out["usage"] = usage
        except Exception:
            out["usage"] = {}
        # 池配置（掩码）+ 运行态
        kp = ((config.get("ai") or {}).get("key_pool")) or {}
        keys_cfg = []
        for i, item in enumerate(kp.get("keys") or []):
            if not isinstance(item, dict):
                continue
            k = str(item.get("api_key") or "")
            keys_cfg.append({
                "name": str(item.get("name") or f"key{i + 1}"),
                "api_key_masked": (k[:4] + "…" + k[-4:]) if len(k) > 12 else ("…" if k.strip() else ""),
                "base_url": str(item.get("base_url") or ""),
                "model": str(item.get("model") or ""),
            })
        out["pool"] = {"enabled": bool(kp.get("enabled", True)), "keys": keys_cfg}
        ai_client = getattr(request.app.state, "ai_client", None)
        try:
            out["pool"]["runtime"] = ai_client.pool_status() if ai_client is not None else []
            st = ai_client.get_stats() if ai_client is not None else {}
            out["pool"]["stats"] = {
                "calls": int(st.get("key_pool_calls") or 0),
                "ok": int(st.get("key_pool_ok") or 0),
                "last_key": st.get("key_pool_last_key"),
            }
        except Exception:
            out["pool"]["runtime"] = []
            out["pool"]["stats"] = {}
        out["primary"] = _ai_primary_snapshot(config_manager, ai_client)
        return out

    @app.get("/api/workspace/hosted-quota")
    async def api_workspace_hosted_quota(request: Request):
        """托管 AI 试用当日额度（绿条徽章数据源；任意登录用户，60s 进程缓存）。

        非托管部署（自建 Key / 未接网关）→ ``{enabled: false}``，前端不渲染徽章。
        探针软失败也不抛——徽章缺席即可，绝不给工作台添新报错面。
        """
        api_auth(request)
        if config_manager is None:
            return {"ok": True, "enabled": False}
        import asyncio

        try:
            from src.ai.hosted_gateway import quota_probe
            out = await asyncio.to_thread(quota_probe, config_manager)
            return {"ok": True, **out}
        except Exception:
            return {"ok": True, "enabled": False}

    @app.get("/api/workspace/ai-runtime-status")
    async def api_workspace_ai_runtime_status(request: Request):
        """云端 AI 降级态 + 平台通道离线态（坐席工作台状态条轮询用，任意登录用户可读，
        纯内存零开销）。

        只报「降级/正常 + 谁在顶班」与「哪些通道离线多久」（结构化字段 + 英文枚举），
        不含密钥/端点/余额等敏感细节（那些在 supervisor 专属的 /api/setup/cloud-credentials）。
        ``channels`` 复用同一 60s 轮询驱动 #ws-chandown 横幅——WhatsApp 假死 2h 坐席
        毫无感知的事故不再重演。
        """
        api_auth(request)
        channels = _channel_health_snapshot()
        # Phase3: restart cooldown for THIS instance → workbench soft banner
        # (same 60s poll as degrade/chandown; seat-safe, no filesystem paths).
        try:
            from src.utils.instance_restart_status import seat_restart_banner
            restart_banner = seat_restart_banner()
        except Exception:
            restart_banner = {"cooldown_active": False, "instance_id": None}
        # 托管到期提醒（P4，2026-08-08）：厂商巡检写进实例数据区的轻量 JSON →
        # 同一 60s 轮询捎带（零新增轮询）；非托管部署无此文件 → None（前端不渲染）。
        try:
            from src.utils.tenant_notice import read_tenant_notice
            tenant_notice = read_tenant_notice()
        except Exception:
            tenant_notice = None
        ai_client = getattr(request.app.state, "ai_client", None)
        # 主对话模式（cloud/local/local_only）——坐席条只读 effective，不含密钥/端点。
        primary_mode = "cloud"
        try:
            if ai_client is not None:
                primary_mode = str(
                    getattr(ai_client, "_primary_mode", None) or "cloud"
                ).strip().lower()
            if primary_mode not in _AI_PRIMARY_MODES:
                primary_mode = "cloud"
        except Exception:
            primary_mode = "cloud"
        if ai_client is None or not hasattr(ai_client, "degradation_snapshot"):
            return {"ok": True, "degraded": False, "mode": "primary",
                    "primary": primary_mode,
                    "channels": channels, "instance_restart": restart_banner,
                    "tenant_notice": tenant_notice}
        try:
            snap = ai_client.degradation_snapshot()
        except Exception:
            return {"ok": True, "degraded": False, "mode": "primary",
                    "primary": primary_mode,
                    "channels": channels, "instance_restart": restart_banner,
                    "tenant_notice": tenant_notice}
        return {"ok": True, **snap, "primary": primary_mode,
                "channels": channels, "instance_restart": restart_banner,
                "tenant_notice": tenant_notice}

    @app.post("/api/setup/key-pool")
    async def api_setup_key_pool_save(request: Request):
        """保存备用 Key 池到 overlay（config.local.yaml ``ai.key_pool``）并热重建 AI 运行时。

        - body: ``{keys: [{name, api_key, base_url?, model?}]}``（≤10 条；base_url/model
          留空=继承主链）；
        - 防覆盖真密钥：api_key 为空或以 ``…/***`` 掩码样式回传时，沿用同名旧条目的真 key；
        - 写盘走 set_overlay_flag（主 config 注释不动），成功后 reload_ai_runtime 热生效。
        """
        api_auth(request)
        _require_supervisor_or_shell(request)
        if config_manager is None:
            return {"ok": False, "detail": tr(request, "err.svc.config_manager_not_ready")}
        try:
            body: Dict[str, Any] = await request.json()
        except Exception:
            body = {}
        raw_keys = (body or {}).get("keys")
        if not isinstance(raw_keys, list):
            return {"ok": False, "detail": tr(request, "err.setup.pool_keys_required")}
        if len(raw_keys) > 10:
            return {"ok": False, "detail": tr(request, "err.setup.pool_too_many", max=10)}
        old_by_name: Dict[str, str] = {}
        _kp = (((getattr(config_manager, "config", None) or {}).get("ai") or {})
               .get("key_pool")) or {}
        for item in (_kp.get("keys") or []):
            if isinstance(item, dict) and item.get("name"):
                old_by_name[str(item["name"])] = str(item.get("api_key") or "")
        cleaned = []
        seen_names: set = set()
        for i, item in enumerate(raw_keys):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or f"key{i + 1}").strip()[:40] or f"key{i + 1}"
            if name in seen_names:
                return {"ok": False,
                        "detail": tr(request, "err.setup.pool_dup_name", name=name)}
            seen_names.add(name)
            key = str(item.get("api_key") or "").strip()
            if (not key) or ("…" in key) or key.endswith("***"):
                key = old_by_name.get(name, "")   # 掩码回传 → 沿用旧真值
            if not key:
                return {"ok": False,
                        "detail": tr(request, "err.setup.pool_key_empty", name=name)}
            entry: Dict[str, Any] = {"name": name, "api_key": key[:512]}
            base = str(item.get("base_url") or "").strip()[:300]
            model = str(item.get("model") or "").strip()[:120]
            if base:
                entry["base_url"] = base
            if model:
                entry["model"] = model
            cleaned.append(entry)
        ok, msg = config_manager.set_overlay_flag(
            "ai.key_pool", {"enabled": True, "keys": cleaned})
        if not ok:
            return {"ok": False, "detail": tr(request, "err.setup.ai_save_failed", reason=msg)}
        ai_ready = await reload_ai_runtime(request.app, config_manager)
        return {"ok": True, "detail": tr(request, "setup.pool.saved"),
                "count": len(cleaned), "ai_ready": bool(ai_ready)}

    @app.post("/api/setup/channels/{channel}")
    async def api_setup_channel_save(channel: str, request: Request):
        """保存某渠道凭证到 overlay 并即时生效；返回该渠道最新现状 + 自检问题。"""
        api_auth(request)
        _require_supervisor(request)
        if config_manager is None:
            return {"ok": False, "detail": tr(request, "err.svc.config_manager_not_ready")}
        # C0-3：套餐渠道 gating —— enforce 开且该渠道不在授权范围时拒绝接入
        try:
            from src.licensing import channel_allowed, get_license_manager

            _lic = get_license_manager().status()
            if not channel_allowed(_lic, str(channel).lower()):
                return {
                    "ok": False,
                    "error": "channel_not_licensed",
                    "detail": tr(request, "err.ws.plan_channel_not_included", plan=_lic.plan, channel=channel),
                }
        except Exception:
            logger.debug("渠道 gating 检查跳过（已忽略）", exc_info=True)
        try:
            body: Dict[str, Any] = await request.json()
        except Exception:
            body = {}
        values = dict((body or {}).get("values") or {})
        ok, msg, issues = config_manager.save_channel_credentials(channel, values)
        if not ok:
            return {"ok": False, "detail": msg}
        # Messenger 官方通道：保存后 Graph 探针（验 token 真伪 + 自动回填 page_id +
        # 把主页名称/头像带回响应）。必须在 _provision_official_account **之前**——
        # 回填的 page_id 决定账号行 id，与 webhook 入站镜像（page_id or "official"）
        # 同口径。探针 6s 超时、绝不阻塞保存（离线也能存）。
        page_probe = await _maybe_probe_messenger_page(
            str(channel).lower(), config_manager)
        # 纯官方 API 渠道（Instagram/Zalo）：凭证齐 → 自动开通注册表 official 账号行，
        # 下面的编排器热拉起才有东西可认领（没有这行 = 收得到发不出，且无处报因）。
        official_account = _provision_official_account(
            str(channel).lower(), config_manager.config or {})
        # 凭据齐全会顺带开 platform_login（channel_setup.enable_on_ready 桥接）。
        # 编排器原本只在 app 启动时拉起——这里 best-effort 热拉起，免得「向导配完
        # 还得重启一次，扫上的号才会上线」。ensure/start_loop 均幂等，已在跑零副作用。
        try:
            from src.integrations.account_orchestrator import (
                ensure_builtin_workers, get_orchestrator, orchestrator_enabled)
            cfg_now = config_manager.config or {}
            if orchestrator_enabled(cfg_now):
                ensure_builtin_workers(cfg_now)
                await get_orchestrator(cfg_now).start_loop()
        except Exception:
            logger.debug("保存凭据后热拉起编排器失败（已忽略；重启后生效）", exc_info=True)
        from src.utils.channel_setup import channel_status
        status = next(
            (c for c in channel_status(config_manager.config or {})
             if c["id"] == str(channel).lower()), None)
        # 只回与该渠道相关的自检问题（按 enable_key/字段前缀粗筛）
        prefix = str(channel).lower()
        rel = [
            {"severity": i.severity, "path": i.path, "message": i.message}
            for i in issues
            if prefix in i.path or (status and any(
                f["key"] in i.path for f in status.get("fields", [])))
        ]
        out = {"ok": True, "detail": msg, "channel": status, "issues": rel}
        if official_account:
            out["official_account"] = official_account
        # 探针结论随保存响应直达前端（None＝非 messenger/无 token，不出字段）：
        # ok 时前端可显示「已连接：<主页名>」确认时刻；失败带 Graph 原始报错就地纠错
        if page_probe is not None:
            out["probe"] = page_probe
        return out
