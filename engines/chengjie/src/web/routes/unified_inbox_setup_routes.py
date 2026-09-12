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
    lock = ""
    try:
        from src.ai.ai_primary_audit import resolve_lock
        lock = resolve_lock(ai)
    except Exception:
        lock = ""
    return {
        "configured": configured,
        "effective": effective,
        "local_ready": local_ready,
        "local_model": str((fb or {}).get("model") or "").strip() or None,
        "divergent": configured != effective,
        # 老板锁（2026-08-22）：非空=档位锁死，越权切换被拒/被强制回锁值
        "lock": lock,
        "locked": bool(lock),
    }


def _request_actor(request: "Request") -> str:
    """审计行的操作者标识（session 用户名/ID → Bearer 壳 → unknown；绝不抛）。"""
    try:
        u = request.session.get("username") or request.session.get("user_id")
        if u:
            return f"user:{u}"
    except Exception:
        pass
    try:
        if (request.headers.get("Authorization") or "").startswith("Bearer "):
            return "bearer-token"
    except Exception:
        pass
    return "unknown"


def _request_ip(request: "Request") -> str:
    try:
        return str(request.client.host or "") if request.client else ""
    except Exception:
        return ""


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
    ``failed`` 这类「该有人去修」的意外掉线。（``abandoned``＝放弃的登录尝试，
    压根不在不健康集合里，天然不会到这。）

    「期望在线」策略过滤（2026-08-27 状态中心 v2）：与看门狗催办**同一判据**
    ``session_expected_online``——运营已登出（operator）/ 登录位已被接替
    （superseded:*）/ 已删除 / 登录尝试幽灵（无注册表行的 msg_* 临时 id）一律
    不亮。此前口径分裂：worker 重启用陈旧 cookie 重推一次 needs_login，就能把
    处理完的号再点红（Calixa 僵尸的最后一条上游通路）。ops 卡走 ``dump()`` 原样
    全量（管理员要看全部真相），本过滤只作用于坐席横幅。

    每条目尽力富集 ``name``（注册表 meta.self_name / label）——红条只写
    ``messenger:6158…`` 坐席不知道是谁掉线（2026-08-14 实录），有昵称才可操作。
    另带 ``relogin_ts/relogin_by``（最近一次人工重登触发的痕迹）——多坐席值守
    时「已有人在处理」全员可见，防同一个号被两个人各触发一遍。
    """
    try:
        from src.integrations.platform_session_health import (
            channel_alert_muted, ensure_seeded_from_registry,
            get_platform_session_health, session_expected_online,
        )
        ensure_seeded_from_registry()
        now = time.time()
        items = []
        reg = None
        try:
            from src.integrations.account_registry import get_account_registry
            reg = get_account_registry()
        except Exception:
            reg = None
        for key, sess in get_platform_session_health().unhealthy_sessions().items():
            st = str(sess.get("status") or "")
            if st == "logged_out":
                continue
            if not session_expected_online(key):
                continue
            # #196：坐席选了「不再提醒此账号」/「24 小时」→ 服务端静默（换机不丢），
            # 快照直接不给前端；「标为已停用」走 expected_online=False 在上一行已剔。
            if channel_alert_muted(key, now):
                continue
            platform, _, account_id = str(key).partition(":")
            since = (float(sess.get("unhealthy_since") or 0.0)
                     or float(sess.get("ts") or 0.0) or now)
            name = ""
            if reg is not None and account_id:
                try:
                    row = reg.get(platform, account_id) or {}
                    meta = row.get("meta") or {}
                    name = str(meta.get("self_name") or row.get("label") or "").strip()
                except Exception:
                    name = ""
            items.append({
                "platform": platform,
                "account_id": account_id,
                "name": name[:48],
                "status": st,
                "down_min": int(max(0.0, now - since) // 60),
                "relogin_ts": float(sess.get("last_relogin_ts") or 0.0),
                "relogin_by": str(sess.get("last_relogin_by") or "")[:24],
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


def _probe_model_endpoint(base_url: str, api_key: str = "", timeout: float = 3.0) -> Dict[str, Any]:
    """轻量探活模型端点（GET {base}/v1/models，OpenAI 兼容）：只读、不耗 token、短超时。

    返回 ``{reachable, status, latency_ms}``。主机应答 4xx/5xx（如 401 鉴权）仍算
    ``reachable=True``（端点在线，只是鉴权/路径问题）；连接失败/超时/坏 URL=False。
    任何异常都不抛（体检不能把设置页打崩）。
    """
    import time as _t
    import urllib.error
    import urllib.request
    b = str(base_url or "").strip().rstrip("/")
    if not b or "://" not in b:
        return {"reachable": False, "status": None, "latency_ms": 0, "error": "bad_url"}
    if not b.endswith("/v1"):
        b = b + "/v1"
    url = b + "/models"
    headers = {"Authorization": "Bearer " + (api_key or "probe")}
    t0 = _t.time()
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"reachable": True,
                    "status": int(getattr(resp, "status", 200) or 200),
                    "latency_ms": int((_t.time() - t0) * 1000)}
    except urllib.error.HTTPError as he:
        return {"reachable": True, "status": int(getattr(he, "code", 0) or 0),
                "latency_ms": int((_t.time() - t0) * 1000)}
    except Exception as ex:
        return {"reachable": False, "status": None,
                "latency_ms": int((_t.time() - t0) * 1000), "error": str(ex)[:80]}


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
    """各平台「当前能收发消息」的账号数——向导徽标「已接 N 个账号」与头部
    「能收发消息的渠道 X/7」的计数源。

    向导「就绪」判定的第一手信号：扫码/协议登录进来的账号**不写任何渠道 yaml 键**，
    只看配置就会把一台正在收发消息的机器报成「已就绪 0/4」（实机反馈）。

    #126（2026-09-01 skuio）：多账号登录后徽标常年停在「已接 1 个账号」——与
    #61/#78 同族的账号枚举病换了个消费面。真相单一源＝运行时账号注册表
    ``platform_accounts``（``live_accounts_by_platform``，与人设「应用到」弹窗
    #78 二轮同一张表）。此前本函数直接 ``list()`` 把 **offline（已登出）** 账号
    一并计入，登出一个号计数不减＝违背「登录登出实时跟随」；现收敛到统一源，
    offline/removed 一律不计——登出即时 -1，头部就绪口径连带跟随。
    取数失败一律返回空 → 判定退回纯配置口径，绝不让向导因此报错。
    """
    from src.integrations.account_registry import live_accounts_by_platform
    return live_accounts_by_platform()


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

    @app.get("/api/setup/deploy-profile")
    async def api_setup_deploy_profile(request: Request):
        """部署能力预设档就绪自检（WP-1，只读零网络）。

        「当前档位 + 各能力 开/关/降级」一屏——首启向导与支持排障的单一读面。
        全部来自合并后 config（含 overlay 与托管 env 注入），零密钥零探活；
        state 语义见 ``deploy_profile.capability_snapshot``（on/off/degraded）。
        """
        api_auth(request)
        _require_supervisor_or_shell(request)
        from src.utils.deploy_profile import (
            active_profile, capability_snapshot, list_profiles)
        config = getattr(config_manager, "config", None) or {} if config_manager else {}
        return {
            "ok": True,
            "profile": active_profile(config) or None,
            "available": list_profiles(),
            "capabilities": capability_snapshot(config),
        }

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
        _pa_append = None
        _lock = ""
        try:
            from src.ai.ai_primary_audit import append_event as _pa_append  # noqa: F811
            from src.ai.ai_primary_audit import resolve_lock as _pa_lock
            _lock = _pa_lock(ai_cfg)
        except Exception:
            _lock = ""
        # 老板锁（2026-08-22）：与锁不符的切换一律拒绝 + 审计 + 告警。
        # 解锁是显式人工动作（overlay 删改 ai.primary_lock），不给接口留后门。
        if _lock and mode != _lock:
            if _pa_append:
                _pa_append(
                    "switch_rejected", requested=mode, lock=_lock,
                    actor=_request_actor(request), ip=_request_ip(request),
                    via="endpoint")
            try:
                from src.integrations.shared.event_bus import get_event_bus
                get_event_bus().publish("ai_primary_guard_alert", {
                    "kind": "lock_rejected",
                    "requested": mode,
                    "lock": _lock,
                    "actor": _request_actor(request),
                    "rate_key": "ai_primary_guard:lock",
                })
            except Exception:
                pass
            return {
                "ok": False,
                "locked": True,
                "detail": tr(request, "err.setup.ai_primary_locked", mode=_lock),
                "primary": _ai_primary_snapshot(
                    config_manager, getattr(request.app.state, "ai_client", None)),
            }
        if mode != "cloud" and not _local_endpoint_ready(ai_cfg):
            return {"ok": False, "detail": tr(request, "err.setup.ai_primary_need_local")}
        _mode_before = str(ai_cfg.get("primary") or "cloud").strip().lower()
        ok, msg = config_manager.set_overlay_flag("ai.primary", mode)
        if not ok:
            return {"ok": False, "detail": tr(
                request, "err.setup.ai_primary_save_failed", reason=msg)}
        ai_ready = await reload_ai_runtime(request.app, config_manager)
        ai_client = getattr(request.app.state, "ai_client", None)
        if _pa_append:
            _pa_append(
                "switch_saved", mode_from=_mode_before, mode_to=mode,
                actor=_request_actor(request), ip=_request_ip(request),
                via="endpoint", ai_ready=bool(ai_ready))
        return {
            "ok": True,
            "detail": tr(request, "setup.ai_primary.saved"),
            "ai_ready": bool(ai_ready),
            "primary": _ai_primary_snapshot(config_manager, ai_client),
        }

    @app.get("/api/setup/ai-primary/audit")
    async def api_setup_ai_primary_audit(request: Request):
        """主链切换审计台账（最近 50 行）+ 当前锁态（supervisor/壳专属）。

        2026-08-22 沉淀：此前切换不留痕，「谁把主链翻回 local_only」查无对证。
        """
        api_auth(request)
        _require_supervisor_or_shell(request)
        rows: list = []
        lock = ""
        try:
            from src.ai.ai_primary_audit import read_tail, resolve_lock
            rows = read_tail(50)
            ai_cfg = ((getattr(config_manager, "config", None) or {}).get("ai")) or {} \
                if config_manager is not None else {}
            lock = resolve_lock(ai_cfg)
        except Exception:
            rows = []
        return {"ok": True, "lock": lock, "locked": bool(lock), "rows": rows}

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
            by_tier: Dict[str, Dict[str, float]] = {}
            for row in (get_llm_cost().dump().get("rows") or []):
                raw_tier = str(row.get("tier") or "default")
                group = _usage_tier_group(raw_tier)
                g = usage.setdefault(group, {"calls": 0, "tokens": 0, "cost_usd": 0.0,
                                             "latency_ms_sum": 0})
                g["calls"] += int(row.get("calls") or 0)
                g["tokens"] += int(row.get("prompt_tokens") or 0) + int(row.get("completion_tokens") or 0)
                g["cost_usd"] += float(row.get("cost_usd") or 0.0)
                g["latency_ms_sum"] += int(row.get("latency_ms_sum") or 0)
                # 原始 tier 行透出（2026-08-13）：分组视图会把 tool/default 并进
                # primary，读数排障（「vLLM 收到 N 次 vs 看板 M 次」）需要未分组真相。
                b = by_tier.setdefault(raw_tier, {"calls": 0, "tokens": 0})
                b["calls"] += int(row.get("calls") or 0)
                b["tokens"] += int(row.get("prompt_tokens") or 0) + int(row.get("completion_tokens") or 0)
            for g in usage.values():
                g["latency_avg_ms"] = (
                    int(g["latency_ms_sum"] / g["calls"]) if g["calls"] else 0)
                g.pop("latency_ms_sum", None)
            out["usage"] = usage
            out["usage_by_tier"] = by_tier
        except Exception:
            out["usage"] = {}
            out["usage_by_tier"] = {}
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

    @app.post("/api/workspace/channel-alert/mute")
    async def api_workspace_channel_alert_mute(request: Request):
        """#196 断线提醒按账号静默（服务端落注册表 meta，换机不丢）。

        body ``{platform, account_id, hours}``：``hours`` 缺省/``null``/``"forever"``＝
        不再提醒此账号；``<=0``＝取消静默；否则静默 N 小时。任意登录坐席可用——
        这是提醒偏好不是账号状态；账号不在注册表（config/适配器来源）→ ``stored=false``
        由前端回落本机 localStorage。
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        plat = str((body or {}).get("platform") or "").strip().lower()
        acct = str((body or {}).get("account_id") or "").strip()
        if not plat or not acct:
            return {"ok": False, "error": tr(request, "err.ws.field_required",
                                              field="platform/account_id")}
        raw_hours = (body or {}).get("hours", None)
        hours: Optional[float]
        if raw_hours is None or str(raw_hours).strip().lower() in ("", "forever", "never"):
            hours = None
        else:
            try:
                hours = float(raw_hours)
            except (TypeError, ValueError):
                hours = None
        from src.integrations.platform_session_health import set_channel_alert_mute
        until = set_channel_alert_mute(plat, acct, hours=hours)
        return {"ok": True, "platform": plat, "account_id": acct,
                "until": until, "stored": bool(until != 0.0 or (hours is not None and hours <= 0))}

    @app.post("/api/workspace/channel-alert/disable")
    async def api_workspace_channel_alert_disable(request: Request):
        """#196「标为已停用」：账号进 offline + operator:disabled——看门狗跳过、横幅不亮、
        账号栏灰显；凭据不清，重新登录一次即归位。主管权限（改的是账号状态）。
        """
        api_auth(request)
        _require_supervisor(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        plat = str((body or {}).get("platform") or "").strip().lower()
        acct = str((body or {}).get("account_id") or "").strip()
        if not plat or not acct:
            return {"ok": False, "error": tr(request, "err.ws.field_required",
                                              field="platform/account_id")}
        actor = ""
        try:
            actor = str(request.session.get("username") or "")
        except Exception:
            actor = ""
        # 先停 worker（best-effort）：停用了还让编排器反复重连＝红噪音源头不断
        try:
            from src.integrations.account_orchestrator import (
                account_key, get_orchestrator,
            )
            cfg = (config_manager.config if config_manager is not None else {}) or {}
            await get_orchestrator(cfg).stop_account(account_key(plat, acct))
        except Exception:
            logger.debug("[channel-alert] 停用前停 worker 失败（忽略）", exc_info=True)
        from src.integrations.platform_session_health import mark_account_disabled
        ok = mark_account_disabled(plat, acct, actor=actor)
        if not ok:
            return {"ok": False, "error": tr(request, "err.ws.account_not_in_registry")}
        return {"ok": True, "platform": plat, "account_id": acct, "status": "disabled"}

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
        try:
            from src.ops.delivery_block import seat_banner as _deliv_banner
            delivery_block = _deliv_banner()
        except Exception:
            delivery_block = {"active": False}
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
        # 官网连通性（实施86 域A-2②，#17/#51）：托管机各链（AI/克隆语音/报障）都
        # 依赖官网，此前断链只会表现为一堆互不相干的静默回落。探针 120s 进程缓存 +
        # 连续两振才报，同一 60s 轮询捎带；非托管态恒 None（前端隐藏）。
        try:
            # 本函数作用域没有 asyncio（上个函数的局部导入不可达）——漏导入会被
            # 本 except 吞成 site_link 恒 None＝功能静默死（undefined-names 门禁抓的）
            import asyncio

            from src.utils.site_link_probe import site_link_snapshot
            site_link = await asyncio.to_thread(site_link_snapshot, config_manager)
        except Exception:
            site_link = None
        # P1 2026-08-23 急停可见化：全局急停摘要随同一 60s 轮询捎带（零新增轮询）。
        # 顶栏冻结条据此渲染——收件箱横幅只覆盖「打开着的会话」，全局急停时坐席
        # 不该等点进会话才发现。只读既有单例（status_snapshot fail-open），
        # 无敏感字段（scope/来源/恢复时刻；reason 本就会显示给坐席横幅）。
        kill_switch = {"active_scopes": 0, "global_active": False}
        try:
            from src.ops.kill_switch import (
                GLOBAL_SCOPE,
                freeze_source,
                status_snapshot,
            )
            _ks_items = status_snapshot()
            _ks_global = next(
                (i for i in _ks_items if i.get("scope") == GLOBAL_SCOPE), None)
            kill_switch = {
                "active_scopes": len(_ks_items),
                "global_active": bool(_ks_global),
            }
            if _ks_global:
                _src, _cause = freeze_source(
                    _ks_global.get("actor"), _ks_global.get("reason"))
                kill_switch["source"] = _src
                kill_switch["cause"] = _cause
                kill_switch["expires_at"] = (
                    float(_ks_global.get("expires_at") or 0) or None)
        except Exception:
            kill_switch = {"active_scopes": 0, "global_active": False}
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
                    "tenant_notice": tenant_notice,
                    "delivery_block": delivery_block,
                    "kill_switch": kill_switch, "site_link": site_link}
        try:
            snap = ai_client.degradation_snapshot()
        except Exception:
            return {"ok": True, "degraded": False, "mode": "primary",
                    "primary": primary_mode,
                    "channels": channels, "instance_restart": restart_banner,
                    "tenant_notice": tenant_notice,
                    "delivery_block": delivery_block,
                    "kill_switch": kill_switch, "site_link": site_link}
        return {"ok": True, **snap, "primary": primary_mode,
                "channels": channels, "instance_restart": restart_banner,
                "tenant_notice": tenant_notice,
                "delivery_block": delivery_block,
                "kill_switch": kill_switch, "site_link": site_link}

    # 「AI 本周替你完成 N 条回复」坐席可读摘要（2026-08-14）。/api/report/weekly 是
    # 主管专属重报表，普通坐席 403 → 收件箱空态 ROI 行对最该被激励的人反而不显示。
    # 本端点只出 drafts.sent 一个数字（无明细/无客户内容/无成本字段），与
    # ai-runtime-status 同一坐席级鉴权；build_weekly_value 是 7 天窗持久库聚合 →
    # 进程级 1h TTL 缓存 + to_thread（重算每小时最多一次，绝不随前端轮询放大）。
    _weekly_brief_cache: Dict[str, Any] = {"ts": 0.0, "sent": None}

    @app.get("/api/workspace/ai-weekly-brief")
    async def api_workspace_ai_weekly_brief(request: Request):
        api_auth(request)
        import time as _t
        now = _t.time()
        if now - float(_weekly_brief_cache.get("ts") or 0) > 3600:
            _weekly_brief_cache["ts"] = now   # 失败也进冷却：不对故障聚合连环重试
            sent = None
            try:
                inbox = getattr(request.app.state, "inbox_store", None)
                if inbox is not None:
                    import asyncio as _aio
                    from src.ops.value_report import build_weekly_value
                    val = await _aio.to_thread(build_weekly_value, inbox)
                    sent = int((((val or {}).get("this_week") or {})
                                .get("drafts") or {}).get("sent") or 0)
            except Exception:
                sent = None
            _weekly_brief_cache["sent"] = sent
        sent = _weekly_brief_cache.get("sent")
        return {"ok": True, "available": sent is not None,
                "sent": int(sent or 0)}

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

    @app.get("/api/setup/model-routes")
    async def api_setup_model_routes_get(request: Request, probe: int = 0):
        """多模型路由总览（ai.models + ai.task_routes）：模型档（密钥打码）+ 任务映射 +
        已认任务名 + 运行态（哪些档已装载）。``probe=1`` 时逐档探活端点（🟢/🔴，只读不耗
        token）。密钥绝不回显全量。
        """
        api_auth(request)
        _require_supervisor_or_shell(request)
        config = getattr(config_manager, "config", None) or {}
        ai_cfg = config.get("ai") or {}
        out_models = []
        models_cfg = ai_cfg.get("models") or {}
        if isinstance(models_cfg, dict):
            for name, spec in models_cfg.items():
                if not isinstance(spec, dict):
                    continue
                k = str(spec.get("api_key") or "")
                base = str(spec.get("base_url") or "")
                row = {
                    "name": str(name),
                    "base_url": base,
                    "model": str(spec.get("model") or ""),
                    "api_key_masked": (k[:4] + "…" + k[-4:]) if len(k) > 12 else ("…" if k.strip() else ""),
                }
                if probe:
                    real_key = str(spec.get("api_key") or ai_cfg.get("api_key") or "")
                    row["health"] = _probe_model_endpoint(base, real_key)
                out_models.append(row)
        routes_cfg = ai_cfg.get("task_routes") or {}
        task_routes = ({str(t): str(p) for t, p in routes_cfg.items()}
                       if isinstance(routes_cfg, dict) else {})
        loaded = []
        try:
            ai_client = getattr(request.app.state, "ai_client", None)
            if ai_client is not None:
                loaded = list(getattr(ai_client, "_route_clients", {}).keys())
        except Exception:
            loaded = []
        return {
            "ok": True,
            "models": out_models,
            "task_routes": task_routes,
            "known_tasks": ["assistant_planner", "assistant_qa", "computer_use", "chat"],
            "loaded": loaded,
        }

    @app.post("/api/setup/model-routes")
    async def api_setup_model_routes_save(request: Request):
        """保存多模型路由到 overlay（ai.models + ai.task_routes）并热重建 AI 运行时。

        - body: ``{models: [{name, base_url, model, api_key?}], task_routes: {task: profile}}``；
        - base_url/model 必填；掩码回传的 api_key 沿用同名旧真值；空 api_key 允许（本地端点
          无鉴权，运行时自动填占位）；task_routes 指向不存在的档直接拒绝（防「路由到空气」）。
        """
        api_auth(request)
        _require_supervisor_or_shell(request)
        if config_manager is None:
            return {"ok": False, "detail": tr(request, "err.svc.config_manager_not_ready")}
        try:
            body: Dict[str, Any] = await request.json()
        except Exception:
            body = {}
        raw_models = (body or {}).get("models")
        if not isinstance(raw_models, list):
            return {"ok": False, "detail": tr(request, "err.setup.routes_models_required")}
        if len(raw_models) > 20:
            return {"ok": False, "detail": tr(request, "err.setup.routes_too_many", max=20)}
        old_by_name: Dict[str, str] = {}
        _m = (((getattr(config_manager, "config", None) or {}).get("ai") or {})
              .get("models")) or {}
        if isinstance(_m, dict):
            for nm, sp in _m.items():
                if isinstance(sp, dict):
                    old_by_name[str(nm)] = str(sp.get("api_key") or "")
        cleaned: Dict[str, Any] = {}
        seen_names: set = set()
        for i, item in enumerate(raw_models):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()[:40]
            if not name:
                return {"ok": False, "detail": tr(request, "err.setup.routes_name_empty")}
            if name in seen_names:
                return {"ok": False, "detail": tr(request, "err.setup.routes_dup_name", name=name)}
            seen_names.add(name)
            base = str(item.get("base_url") or "").strip()[:300]
            if not base or "://" not in base:
                return {"ok": False, "detail": tr(request, "err.setup.routes_base_invalid", name=name)}
            model = str(item.get("model") or "").strip()[:120]
            if not model:
                return {"ok": False, "detail": tr(request, "err.setup.routes_model_empty", name=name)}
            spec: Dict[str, Any] = {"base_url": base, "model": model}
            key = str(item.get("api_key") or "").strip()
            if ("…" in key) or key.endswith("***"):
                key = old_by_name.get(name, "")   # 掩码回传 → 沿用旧真值
            if key:
                spec["api_key"] = key[:512]
            cleaned[name] = spec
        raw_routes = (body or {}).get("task_routes") or {}
        routes: Dict[str, str] = {}
        if isinstance(raw_routes, dict):
            for task, prof in raw_routes.items():
                t = str(task).strip()[:40]
                p = str(prof or "").strip()
                if not t or not p:
                    continue
                if p not in cleaned:
                    return {"ok": False,
                            "detail": tr(request, "err.setup.routes_unknown_profile", task=t, name=p)}
                routes[t] = p
        ok1, msg1 = config_manager.set_overlay_flag("ai.models", cleaned)
        if not ok1:
            return {"ok": False, "detail": tr(request, "err.setup.ai_save_failed", reason=msg1)}
        ok2, msg2 = config_manager.set_overlay_flag("ai.task_routes", routes)
        if not ok2:
            return {"ok": False, "detail": tr(request, "err.setup.ai_save_failed", reason=msg2)}
        ai_ready = await reload_ai_runtime(request.app, config_manager)
        return {"ok": True, "detail": tr(request, "setup.routes.saved"),
                "models": len(cleaned), "routes": len(routes), "ai_ready": bool(ai_ready)}

    @app.post("/api/setup/model-routes/list-models")
    async def api_setup_model_routes_list_models(request: Request):
        """拉某端点的模型清单（``GET {base}/v1/models``，OpenAI 兼容）——给开发者页「多模型路由」
        的模型名输入框做候选，运营不必背 gpt-/gemini-/grok- 的现役 id（2026-09-12 厂商预设配套）。

        body ``{base_url, api_key?, name?}``：api_key 为空或掩码 → 同名已存档的真值 → ``ai.api_key``。
        只读、不耗 token；返回 ``{ok, ids:[...], n, status}``；密钥不回显。
        """
        api_auth(request)
        _require_supervisor_or_shell(request)
        try:
            body: Dict[str, Any] = await request.json()
        except Exception:
            body = {}
        base = str((body or {}).get("base_url") or "").strip().rstrip("/")[:300]
        if not base or "://" not in base:
            return {"ok": False, "detail": tr(request, "err.setup.routes_base_invalid", name="-")}
        if not base.endswith("/v1"):
            base = base + "/v1"
        key = str((body or {}).get("api_key") or "").strip()
        cfg = getattr(config_manager, "config", None) or {}
        ai_cfg = cfg.get("ai") or {}
        if not key or ("…" in key) or key.endswith("***"):
            nm = str((body or {}).get("name") or "").strip()
            stored = ((ai_cfg.get("models") or {}).get(nm) or {}) if nm else {}
            key = str(stored.get("api_key") or "") or str(ai_cfg.get("api_key") or "")
        ids: list = []
        status = 0
        try:
            import httpx
            async with httpx.AsyncClient(timeout=8.0) as cli:
                resp = await cli.get(base + "/models",
                                     headers={"Authorization": "Bearer " + (key or "probe")})
            status = int(resp.status_code)
            if status < 400:
                data = resp.json()
                rows = data.get("data") if isinstance(data, dict) else data
                for it in (rows or [])[:500]:
                    mid = it.get("id") if isinstance(it, dict) else it
                    if mid:
                        ids.append(str(mid))
        except Exception as ex:
            return {"ok": False, "detail": str(ex)[:120], "status": status}
        ids = sorted(set(ids))[:200]
        return {"ok": status < 400, "ids": ids, "n": len(ids), "status": status}

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
