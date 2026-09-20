"""统一收件箱——平台扫码登录路由域（巨石拆分 slice 9）。

把"平台扫码登录（P3/M1：多方式并存 · 无限扫码 / 多账号接入 + 自助重连）"这一
自包含子域，从 ``register_unified_inbox_routes`` 巨型闭包中抽出，封装为
``register_platform_login_routes(app, *, api_auth, config_manager)``，由主 register
顺序调用。端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫保证）。

域内闭包级 helper（_platform_login_cfg / _platform_login_enabled / _login_qr_data_url /
_ensure_login_providers / _persist_login_account）随域同搬，保持闭包捕获 config_manager
的原行为。子注册函数只收自身真正需要的依赖（api_auth + config_manager）。
"""

from __future__ import annotations

import inspect
import logging
import time
from typing import Any, Dict

from fastapi import Request

from src.inbox.channel_adapters import status_via_adapters
from src.integrations.account_registry import get_account_registry
from src.integrations.fingerprint import get_fingerprint_store
from src.integrations.platform_login import (
    SUPPORTED_PLATFORMS,
    first_available_mode,
    get_login_manager,
    get_login_provider,
    list_modes,
    login_kind,
    mode_available,
    online_account_keys,
)
from src.integrations.login_funnel_stats import record_login_stage
from src.integrations.proxy_pool import get_proxy_pool
from src.web.routes.unified_inbox_aggregate import _INBOX_ADAPTERS
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def _funnel(sess, stage: str, *, reason_code: str = "") -> None:
    """记一次登录漏斗分段（每会话每段只记一次）。

    去重靠 ``sess.funnel_marks``——状态轮询 2.5s 一轮，不去重会把一次登录记成上百次，
    漏斗比例失真。观测全程 best-effort，绝不能影响登录本身。
    """
    try:
        if stage in sess.funnel_marks:
            return
        sess.funnel_marks.add(stage)
        elapsed = 0.0
        if stage == "authorized":
            elapsed = max(0.0, (time.time() - sess.created_at) * 1000.0)
        record_login_stage(sess.platform, sess.mode, stage,
                           reason_code=reason_code, elapsed_ms=elapsed)
    except Exception:  # noqa: BLE001
        pass


def register_platform_login_routes(app, *, api_auth, config_manager=None) -> None:
    """挂载平台扫码登录相关端点（/api/platforms/{platform}/...）。"""

    # ── 平台扫码登录（P3/M1：多方式并存 · 无限扫码 / 多账号接入 + 自助重连） ──
    def _platform_login_cfg() -> Dict[str, Any]:
        try:
            if config_manager is None:
                return {}
            return (config_manager.config or {}).get("platform_login", {}) or {}
        except Exception:
            return {}

    def _platform_login_enabled() -> bool:
        return bool(_platform_login_cfg().get("enabled", True))

    def _login_qr_data_url(qr_url: str) -> str:
        """把 tg://login?token=… 等登录 URL 服务端渲染为 base64 PNG data URL。

        令牌不出本机（避免泄露给第三方 QR 服务）。qrcode/PIL 缺失或失败时返回空串，
        前端回落为显示链接 / 设备端指引。
        """
        text = str(qr_url or "")
        if not text:
            return ""
        try:
            import base64
            import io
            import qrcode
            img = qrcode.make(text)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return "data:image/png;base64," + \
                base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            logger.debug("登录二维码服务端渲染失败", exc_info=True)
            return ""

    def _ensure_login_providers() -> None:
        """按需注册真实 per-(platform,mode) provider（幂等、全程降级）。"""
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        try:
            from src.integrations.telegram_protocol_login import maybe_register as _tg_reg
            _tg_reg(cfg)
        except Exception:
            logger.debug("注册 telegram protocol provider 失败", exc_info=True)
        try:
            from src.integrations.telegram_phone_login import maybe_register as _tg_phone_reg
            _tg_phone_reg(cfg)
        except Exception:
            logger.debug("注册 telegram phone provider 失败", exc_info=True)
        try:
            from src.integrations.whatsapp_baileys_login import maybe_register as _wa_reg
            _wa_reg(cfg)
        except Exception:
            logger.debug("注册 whatsapp baileys provider 失败", exc_info=True)
        try:
            from src.integrations.messenger_web_login import maybe_register as _mg_reg
            _mg_reg(cfg)
        except Exception:
            logger.debug("注册 messenger web provider 失败", exc_info=True)
        try:
            from src.integrations.zalo_personal_login import maybe_register as _zl_reg
            _zl_reg(cfg)
        except Exception:
            logger.debug("注册 zalo personal provider 失败", exc_info=True)
        try:
            from src.integrations.instagram_web_login import maybe_register as _ig_reg
            _ig_reg(cfg)
        except Exception:
            logger.debug("注册 instagram web provider 失败", exc_info=True)
        try:
            # TK-3 ②-A：TikTok 个人号网页边车（assistOnly，默认关；platform_login.tiktok.web_enabled 才注册）
            from src.integrations.tiktok_web_login import maybe_register as _tt_reg
            _tt_reg(cfg)
        except Exception:
            logger.debug("注册 tiktok web provider 失败", exc_info=True)
        try:
            from src.integrations.line_protocol_login import maybe_register as _ln_reg
            _ln_reg(cfg)
        except Exception:
            logger.debug("注册 line protocol provider 失败", exc_info=True)
        try:
            from src.integrations.qq_protocol_login import maybe_register as _qq_reg
            _qq_reg(cfg)
        except Exception:
            logger.debug("注册 qq protocol provider 失败", exc_info=True)

    def _persist_login_account(platform: str, account_id: str, sess: Any) -> None:
        """登录成功后把账号 + mode + 代理 + 指纹 + 备注落库，并把代理标记为已分配。"""
        try:
            get_account_registry().upsert(
                platform, account_id, mode=getattr(sess, "mode", "device"),
                status="online",
                label=(getattr(sess, "label", "") or None),
                proxy_id=(getattr(sess, "proxy_id", "") or None),
                fingerprint_id=(getattr(sess, "fingerprint_id", "") or None),
            )
            if getattr(sess, "proxy_id", ""):
                get_proxy_pool().assign(sess.proxy_id, f"{platform}:{account_id}")
        except Exception:
            logger.debug("账号注册表上线 upsert 失败", exc_info=True)
        # P1-⑧ 激活里程碑：首个账号接入成功（所有平台/形态的登录成功都汇到本函数，
        # 单点上报；beacon 未装=no-op，只送 platform/mode 两个低敏字段）
        try:
            from src.utils.telemetry_beacon import note_milestone
            note_milestone("account_online",
                           f"platform={platform} mode={getattr(sess, 'mode', '')}")
        except Exception:
            logger.debug("account_online 里程碑上报失败（忽略）", exc_info=True)
        # P0-1（B57+B59）：登录成功=最强「没被封」证据 → 自动解除该账号 auth 族
        # auto_ban 冻结 + 清 meta.banned，并开启登录变更降档窗口（升级风暴闭环：
        # 会话被杀 → 重登 → 冻结自动解除，不再需要运维手工翻 kill-switch）。
        try:
            from src.ops.ban_signal import clear_auth_ban_on_login
            res = clear_auth_ban_on_login(platform, account_id)
            if res.get("cleared") or res.get("meta_cleared"):
                logger.info("登录成功自动解除 auth 族冻结 %s：%s", res.get("scope"), res)
        except Exception:
            logger.debug("登录后自动解冻检查失败（忽略）", exc_info=True)

    async def _diagnose_modes(platform: str, modes: list, *, force: bool = False) -> None:
        """给每个 mode 补 ``ready`` / ``blockers``，并把笼统的 reason_code 换成真原因。

        为什么要在这里做：`list_modes` 的 reason_code 来自一张静态表，只会说
        「尚未启用」。LINE 缺 okline、Telegram 缺 api_id、Baileys 没起，三种完全
        不同的处置被压成同一句话，运维照着它去翻开关只会白跑。此处按真实依赖状态
        重算，弹窗的「为什么 / 怎么办」说明卡才有意义。

        另一半价值是**预检**：WhatsApp 开关开着、provider 也注册了（available=true），
        但 sidecar 没起——旧行为要用户点下去等二维码，等来一句 service_down。
        现在开弹窗那一刻就能看见警示。
        """
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        svc: Dict[str, Any] = {}
        try:
            from src.integrations.protocol_diagnostics import probe_services
            svc = await probe_services(cfg, force=force)
        except Exception:  # noqa: BLE001
            logger.debug("sidecar 可达性探测失败（按未知处理）", exc_info=True)
        try:
            from src.integrations.platform_readiness import diagnose_mode
        except Exception:  # noqa: BLE001
            logger.debug("平台诊断模块不可用，跳过 blockers 富集", exc_info=True)
            return
        for m in modes:
            try:
                d = diagnose_mode(
                    platform, str(m.get("mode") or ""), cfg,
                    provider_registered=bool(m.get("available")),
                    service_ok=svc.get(platform),
                )
            except Exception:  # noqa: BLE001
                logger.debug("诊断 %s/%s 失败", platform, m.get("mode"), exc_info=True)
                continue
            m["ready"] = d["ready"]
            m["blockers"] = d["blockers"]
            # 只在诊断出更具体原因时覆盖，避免把静态表已有的准确值（如 Messenger 的
            # needs_server_setup）冲成空。
            if d["reason_code"]:
                m["reason_code"] = d["reason_code"]
            # 可用但未就绪（典型：sidecar 挂了）不该继续挂「推荐」角标去骗点击。
            if not d["ready"]:
                m["preferred"] = False

    @app.get("/api/platforms/{platform}/modes")
    async def api_platform_login_modes(platform: str, request: Request, recheck: int = 0):
        api_auth(request)
        platform = str(platform or "").lower()
        if platform not in SUPPORTED_PLATFORMS:
            return {"ok": False, "detail": tr(request, "err.login.platform_unsupported", platform=platform)}
        _ensure_login_providers()
        platform_cfg = _platform_login_cfg().get(platform, {}) or {}
        modes = list_modes(platform, platform_cfg)
        await _diagnose_modes(platform, modes, force=bool(recheck))
        return {"ok": True, "platform": platform, "modes": modes}

    @app.get("/help/qq-personal-agreement")
    async def qq_personal_agreement_page(request: Request):
        """《QQ 个人号接入协议与风险须知》全文页（登录后可看）。

        docs/ 目录不随桌面包分发——文件在就渲染 Markdown 原文（<pre> 保留结构），不在则回落
        i18n 五要点，绝不 404（协议页 404 比没有协议更糟：用户会认为我们藏着掖着）。
        """
        api_auth(request)
        from pathlib import Path as _P
        from fastapi.responses import HTMLResponse
        import html as _html
        doc = _P(__file__).resolve().parents[3] / "docs" / "QQ个人号接入协议与风险须知.md"
        if doc.is_file():
            body = "<pre style='white-space:pre-wrap;font:14px/1.7 system-ui,sans-serif;max-width:860px;margin:24px auto;padding:0 16px;'>" \
                + _html.escape(doc.read_text(encoding="utf-8")) + "</pre>"
        else:
            pts = "".join(f"<li>{_html.escape(tr(request, 'inbox.connect.qq_risk_p' + str(i)))}</li>"
                          for i in range(1, 6))
            body = (f"<div style='font:14px/1.7 system-ui,sans-serif;max-width:860px;margin:24px auto;padding:0 16px;'>"
                    f"<h2>{_html.escape(tr(request, 'inbox.connect.qq_risk_title'))}</h2><ol>{pts}</ol></div>")
        return HTMLResponse(f"<!doctype html><meta charset='utf-8'><title>QQ</title>{body}")

    @app.post("/api/platforms/qq/download-qq")
    async def api_qq_download(request: Request):
        """让自研 QQ 边车按需下载并静默安装锁定版 QQ 客户端（腾讯官方安装包，落用户可写区）。

        连接弹窗 ``qq_not_installed`` 态的「下载并安装 QQ」按钮调本端点；进度经
        ``GET /api/platforms/qq/qq-status`` 轮询（边车 ``x_qq_status.download``）。幂等。
        """
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.qq_milky import MilkyClient, service_base_url, service_token, start_qq_download
        client = MilkyClient(service_base_url(cfg), service_token(cfg), timeout=8.0)
        out = await start_qq_download(client)
        return {"ok": bool(out), **out}

    @app.get("/api/platforms/qq/qq-status")
    async def api_qq_status(request: Request):
        """本机 QQ 客户端安装态 / 版本 / 是否受支持 / 下载进度（边车 ``x_qq_status``）。"""
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.qq_milky import MilkyClient, fetch_qq_status, service_base_url, service_token
        client = MilkyClient(service_base_url(cfg), service_token(cfg), timeout=8.0)
        out = await fetch_qq_status(client)
        return {"ok": bool(out), **out}

    @app.post("/api/platforms/qq/risk-consent")
    async def api_qq_risk_consent(request: Request):
        """记录 QQ 个人号一次性风险须知的确认（写 platform_login.qq.risk_acknowledged_at）。

        个人号是非官方接入、有封号风险（见 docs/QQ个人号接入协议与风险须知.md）：登录 provider
        在 risk_acknowledged 为假时返回 reason_code=needs_risk_ack，前端弹协议页；用户勾选同意
        调本端点后再重新发起扫码。仅记录时间戳，不改其它开关。
        """
        api_auth(request)
        if config_manager is None:
            return {"ok": False, "detail": tr(request, "err.svc.config_manager_not_ready")}
        import time as _t
        try:
            ok = config_manager.save_overlay_patch(
                {"platform_login": {"qq": {"risk_acknowledged_at": int(_t.time())}}})
        except Exception:  # noqa: BLE001
            logger.debug("[qq] 风险确认写盘失败", exc_info=True)
            ok = False
        return {"ok": bool(ok)}

    @app.get("/api/platforms/telegram/login/preflight")
    async def api_tg_login_preflight(request: Request, force: int = 0):
        """Telegram 直连可达性预检（P1-⑥，2026-08-10 事故链）。

        进入扫码步时前端调用：直连不通（大陆典型形态）→ 黄条提前给「配代理」
        路标，而不是让用户撞完失败再猜。只探 TCP 通性（60s 缓存），**只提示
        不阻断**——用户给本次登录配了代理时直连不通是预期态。
        """
        api_auth(request)
        try:
            from src.integrations.tg_preflight import probe_telegram_reachable
            res = await probe_telegram_reachable(force=bool(force))
        except Exception:  # noqa: BLE001 —— 预检自身故障=没有信息，绝不影响登录
            logger.debug("telegram 预检探测失败（静默）", exc_info=True)
            return {"ok": False}
        return {"ok": True, **res}

    @app.post("/api/platforms/{platform}/login/start")
    async def api_platform_login_start(platform: str, request: Request):
        api_auth(request)
        if not _platform_login_enabled():
            return {"ok": False, "detail": tr(request, "err.login.disabled")}
        platform = str(platform or "").lower()
        if platform not in SUPPORTED_PLATFORMS:
            return {"ok": False, "detail": tr(request, "err.login.platform_unsupported", platform=platform)}
        if platform == "web":
            return {"ok": False, "detail": tr(request, "err.login.web_native")}
        body: Dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        account_id = str((body or {}).get("account_id") or "")
        # M4：账号配置（防关联）
        cfg_label = str((body or {}).get("label") or "")
        cfg_group = str((body or {}).get("group") or "")
        cfg_proxy_id = str((body or {}).get("proxy_id") or "")
        cfg_use_fp = bool((body or {}).get("use_fingerprint") or False)
        cfg_phone = str((body or {}).get("phone") or "")
        _ensure_login_providers()
        # 解析登录方式：缺省取该平台默认 mode
        platform_cfg = _platform_login_cfg().get(platform, {}) or {}
        modes = list_modes(platform, platform_cfg)
        mode = str((body or {}).get("mode") or "").lower()
        if not mode:
            mode = first_available_mode(modes)
        # credentials 形态（官方 API 渠道）没有登录会话可开：凭证在接入向导里配置，
        # 配好即自动上线。这里兜住旧前端/直连 API 的调用，给出结构化指路而非假二维码。
        if login_kind(platform, mode) == "credentials":
            return {"ok": False, "reason_code": "credentials_mode",
                    "setup_url": "/workspace/setup",
                    "detail": tr(request, "err.login.credentials_mode")}
        if not mode_available(platform, mode):
            return {"ok": False, "detail": tr(request, "err.login.mode_unavailable", platform=platform, mode=mode)}

        status_map = status_via_adapters(request, _INBOX_ADAPTERS)
        baseline = online_account_keys(status_map, platform)
        # 重连场景：目标账号当前离线，从基线移除，使其上线时被判定为「新上线」
        if account_id:
            baseline.discard(account_id)

        # M4：解析代理 + 生成/绑定指纹，组装 provider 上下文
        fingerprint_id = ""
        login_ctx: Dict[str, Any] = {}
        if cfg_proxy_id:
            try:
                px = get_proxy_pool().get(cfg_proxy_id, mask=False)
                if px:
                    login_ctx["proxy"] = px
            except Exception:
                logger.debug("读取代理失败", exc_info=True)
        if cfg_use_fp:
            try:
                fp = get_fingerprint_store().create(seed=cfg_label or None,
                                                    label=cfg_label)
                fingerprint_id = fp["fingerprint_id"]
                login_ctx["fingerprint"] = fp["profile"]
            except Exception:
                logger.debug("生成指纹失败", exc_info=True)
        if cfg_phone:
            login_ctx["phone"] = cfg_phone

        qr_url = qr_image = instruction = instruction_key = prov_reason = ""
        poll_fn = cancel_fn = submit_fn = submit_code_fn = resend_code_fn = None
        relay_step_fn = relay_submit_fn = None
        interactive = False
        provider_state = None
        init_status = init_detail = ""
        cred_source = ""
        retry_after_sec = -1
        provider = get_login_provider(platform, mode)
        if provider is not None:
            try:
                try:
                    info = provider(request, platform, mode, account_id, ctx=login_ctx)
                except TypeError:
                    info = provider(request, platform, mode, account_id)
                if inspect.isawaitable(info):
                    info = await info
                info = info or {}
                qr_url = str(info.get("qr_url") or "")
                qr_image = str(info.get("qr_image") or "")
                instruction = str(info.get("instruction") or "")
                instruction_key = str(info.get("instruction_key") or "")
                account_id = str(info.get("account_id") or account_id)
                poll_fn = info.get("poll")
                cancel_fn = info.get("cancel")
                submit_fn = info.get("submit_password")
                submit_code_fn = info.get("submit_code")
                resend_code_fn = info.get("resend_code")
                relay_step_fn = info.get("relay_step")
                relay_submit_fn = info.get("relay_submit")
                interactive = bool(info.get("interactive"))
                provider_state = info.get("state")
                prov_reason = str(info.get("reason_code") or "")
                init_status = str(info.get("status") or "")
                init_detail = str(info.get("detail") or "")
                cred_source = str(info.get("cred_source") or "")
                try:
                    retry_after_sec = int(info.get("retry_after_sec", -1))
                except Exception:
                    retry_after_sec = -1
            except Exception:
                logger.debug("登录 provider[%s:%s] 失败（回落设备端指引）",
                             platform, mode, exc_info=True)

        sess = get_login_manager().create(
            platform, account_id, baseline, mode=mode,
            qr_url=qr_url, qr_image=qr_image, instruction=instruction,
            instruction_key=instruction_key,
            label=cfg_label, group=cfg_group,
            proxy_id=cfg_proxy_id, fingerprint_id=fingerprint_id,
            provider_state=provider_state, poll_fn=poll_fn, cancel_fn=cancel_fn,
            submit_fn=submit_fn, submit_code_fn=submit_code_fn,
            resend_code_fn=resend_code_fn, relay_step_fn=relay_step_fn,
            relay_submit_fn=relay_submit_fn,
            initial_status=init_status, reason_code=prov_reason,
            detail=init_detail,
            cred_source=cred_source, retry_after_sec=retry_after_sec,
        )
        _funnel(sess, "started")
        if sess.qr_image or sess.qr_url:
            _funnel(sess, "qr_shown")
        # provider 开局就报致命故障（组件缺失/客户端起不来）且没给 poll：立刻置终态，
        # 否则会话只能挂到 TTL 耗尽——用户对着转圈等满三分钟才等来一句超时。
        if sess.status == "failed" and sess.reason_code:
            _funnel(sess, "failed", reason_code=sess.reason_code)
        elif prov_reason and poll_fn is None:
            sess.reason_code = prov_reason
            sess.status = "failed"
            _funnel(sess, "failed", reason_code=prov_reason)
        # 落库：重连/已知账号即记录（mode + 代理 + 指纹持久化，供编排器重启后正确拉起）
        if account_id:
            try:
                get_account_registry().upsert(
                    platform, account_id, mode=mode, status="pending",
                    label=cfg_label or None, proxy_id=cfg_proxy_id or None,
                    fingerprint_id=fingerprint_id or None)
                if cfg_proxy_id:
                    get_proxy_pool().assign(cfg_proxy_id, f"{platform}:{account_id}")
            except Exception:
                logger.debug("账号注册表 upsert 失败", exc_info=True)
        out = {
            "ok": True,
            "login_id": sess.login_id,
            "mode": sess.mode,
            # 登录形态（qr/hosted/device）：前端整套向导词汇按它切换
            "login_kind": login_kind(platform, sess.mode),
            "status": sess.status,
            "qr_url": sess.qr_url,
            "qr_image": sess.qr_image or _login_qr_data_url(sess.qr_url),
            "instruction": sess.instruction,
            "instruction_key": sess.instruction_key,
            "reason_code": sess.reason_code,
            # start 即失败时把 detail 一并回（否则前端只有归因码没有技术详情可折叠）
            "detail": sess.detail,
            # 表单中继能力位：true ⇒ 前端走应用内原生分步表单（headless 边车，不弹窗）；
            # false ⇒ 既有 headed 窗口 + 截图预览路径。默认 false（interactive_login 默认关）。
            "interactive": interactive,
            # 会话剩余秒数：前端画二维码有效期条（纯展示；过期判定仍在服务端）
            "expires_in": sess.remaining_sec(),
        }
        if sess.cred_source:
            # cred_invalid 处置元信息：前端据此倒计时自愈（hosted）/亮修正表单（self）
            out["cred_source"] = sess.cred_source
            out["retry_after_sec"] = int(sess.retry_after_sec)
        return out

    @app.get("/api/platforms/{platform}/login/{login_id}/status")
    async def api_platform_login_status(platform: str, login_id: str, request: Request):
        api_auth(request)
        platform = str(platform or "").lower()
        sess = get_login_manager().get(login_id)
        if sess is None:
            return {"ok": True, "status": "expired", "pin": "", "reason_code": "",
                    "detail": tr(request, "err.login.session_expired")}
        if sess.status in ("authorized", "failed"):
            # 终态早退绕过 provider poll，原因码只能从会话读（poll 时已落 sess）
            _out = {"ok": True, "status": sess.status, "pin": "",
                    "reason_code": sess.reason_code, "detail": sess.detail}
            if sess.cred_source:
                _out["cred_source"] = sess.cred_source
                _out["retry_after_sec"] = int(sess.retry_after_sec)
            elif sess.retry_after_sec >= 0:
                # rate_limited（FloodWait）等与凭据来源无关的等待秒数：前端画倒计时
                _out["retry_after_sec"] = int(sess.retry_after_sec)
            return _out
        if sess.is_expired():
            sess.status = "expired"
            # 会话耗尽 TTL 也是一次失败结局，此前**一次都没记进漏斗**——于是「发起 20 次、
            # 成功 0 次、失败 0 次」这种自相矛盾的读数才是常态（托管登录尤甚：人工在服务器
            # 窗口里慢慢登，最可能的非成功结局就是超时）。归因优先用最后观察到的页面分段
            # （卡在 2FA / 检查点），认不出才记 session_timeout。
            reason = sess.hint_code or "session_timeout"
            _funnel(sess, "failed", reason_code=reason)
            return {"ok": True, "status": "expired", "pin": "", "reason_code": ""}
        # provider 事件驱动（protocol/web）：直接问 provider 拿登录结果
        if sess.poll_fn is not None:
            try:
                res = sess.poll_fn(sess)
                if inspect.isawaitable(res):
                    res = await res
                res = res or {}
                st = str(res.get("status") or sess.status)
                sess.status = st
                rc = str(res.get("reason_code") or "")
                if rc:
                    # 只在非空时落：failed 是终态，原因码不应被后续轮询的空值抹掉
                    sess.reason_code = rc
                # cred_invalid 处置元信息与 reason_code 同生命周期（非空才落，终态不抹）
                if res.get("cred_source"):
                    sess.cred_source = str(res.get("cred_source") or "")
                    try:
                        sess.retry_after_sec = int(res.get("retry_after_sec", -1))
                    except Exception:
                        sess.retry_after_sec = -1
                elif "retry_after_sec" in res:
                    # 无凭据来源的等待秒数（rate_limited/FloodWait）：同样落会话供终态早退回读
                    try:
                        sess.retry_after_sec = int(res.get("retry_after_sec", -1))
                    except Exception:
                        sess.retry_after_sec = -1
                # hint_code 与之相反——它是**可来回变的实时态**（用户从 2FA 页退回登录页
                # 就该跟着清掉），故空值也照落，绝不粘住旧提示误导坐席。
                sess.hint_code = str(res.get("hint_code") or "")
                if res.get("qr_image") or res.get("qr_url"):
                    _funnel(sess, "qr_shown")
                if st == "pin_needed":
                    _funnel(sess, "pin_issued")
                elif st == "scanned":
                    # Telegram 扫码确认：「scanned 有量而 authorized 近零」＝扫码后
                    # 的 DC 迁移链坏了（2026-08-14 事故形态），漏斗必须能看见这一段
                    _funnel(sess, "scanned")
                elif st == "authorized":
                    _funnel(sess, "authorized")
                elif st == "failed":
                    _funnel(sess, "failed", reason_code=sess.reason_code)
                if st == "authorized" and res.get("account_id"):
                    _persist_login_account(platform, str(res["account_id"]), sess)
                poll_qr = str(res.get("qr_url") or sess.qr_url)
                if poll_qr and not sess.qr_url:
                    sess.qr_url = poll_qr
                _pout = {"ok": True, "status": st,
                         "detail": str(res.get("detail") or ""),
                         "pin": str(res.get("pin") or ""),
                         # LINE PIN 展示剩余秒（provider 按签发时刻推算）：前端倒计时的权威来源，
                         # 缺省 0＝旧 provider，前端回落本地兜底窗口
                         "pin_expires_in": int(res.get("pin_expires_in") or 0),
                         "reason_code": sess.reason_code,
                         "hint_code": sess.hint_code,
                         "qr_url": poll_qr,
                         "qr_image": str(res.get("qr_image") or "")
                         or _login_qr_data_url(poll_qr),
                         "expires_in": sess.remaining_sec()}
                # #181 WA 配对 DNS 连败次数：随 hint_code=dns_retry 一起给前端出「失败 N 次，
                # 正在重试」；非 WA / 无连败时不带键（旧前端零感知）。
                if res.get("pairing_dns_fails"):
                    try:
                        _pout["pairing_dns_fails"] = int(res.get("pairing_dns_fails") or 0)
                    except (TypeError, ValueError):
                        pass
                if sess.cred_source:
                    _pout["cred_source"] = sess.cred_source
                    _pout["retry_after_sec"] = int(sess.retry_after_sec)
                elif sess.retry_after_sec >= 0:
                    _pout["retry_after_sec"] = int(sess.retry_after_sec)
                return _pout
            except Exception:
                # poll 是登录链路的心跳，静默失败会让整条链路查无实据
                logger.warning("provider poll 失败", exc_info=True)
                return {"ok": True, "status": sess.status, "pin": "",
                        "reason_code": sess.reason_code}
        # 实时对比基线：检测到该平台有新账号上线 → 判定登录成功
        try:
            status_map = status_via_adapters(request, _INBOX_ADAPTERS)
            online = online_account_keys(status_map, platform)
            new_accounts = online - sess.baseline
            if new_accounts:
                sess.status = "authorized"
                _funnel(sess, "authorized")
                for aid in new_accounts:
                    _persist_login_account(platform, aid, sess)
                return {"ok": True, "status": "authorized", "pin": "", "reason_code": ""}
        except Exception:
            logger.debug("登录状态轮询失败", exc_info=True)
        return {"ok": True, "status": sess.status, "pin": "", "reason_code": "",
                "instruction": sess.instruction,
                "instruction_key": sess.instruction_key,
                "expires_in": sess.remaining_sec()}

    @app.post("/api/platforms/{platform}/login/{login_id}/password")
    async def api_platform_login_password(platform: str, login_id: str, request: Request):
        """两步验证：提交云密码完成登录（仅 status==password_needed 的会话有效）。

        body：``{"password": "..."}``。密码错误返回 status=password_needed + detail（可重试）；
        成功返回 authorized 并落库（provider 内已 upsert 全 meta，此处再补 status/代理绑定）。
        """
        api_auth(request)
        platform = str(platform or "").lower()
        sess = get_login_manager().get(login_id)
        if sess is None:
            return {"ok": False, "status": "expired",
                    "detail": tr(request, "err.login.session_expired")}
        if sess.submit_fn is None:
            return {"ok": False, "status": sess.status,
                    "detail": tr(request, "err.login.password_unsupported")}
        body: Dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        password = str((body or {}).get("password") or "")
        if not password:
            return {"ok": False, "status": sess.status,
                    "detail": tr(request, "err.login.password_empty")}
        try:
            res = sess.submit_fn(sess, password)
            if inspect.isawaitable(res):
                res = await res
            res = res or {}
            st = str(res.get("status") or sess.status)
            sess.status = st
            if st == "authorized" and res.get("account_id"):
                _persist_login_account(platform, str(res["account_id"]), sess)
            return {"ok": st == "authorized", "status": st,
                    "detail": str(res.get("detail") or "")}
        except Exception:
            logger.debug("provider submit_password 失败", exc_info=True)
            return {"ok": False, "status": sess.status,
                    "detail": tr(request, "err.login.password_submit_failed")}

    @app.post("/api/platforms/{platform}/login/{login_id}/code")
    async def api_platform_login_code(platform: str, login_id: str, request: Request):
        """手机号登录：提交短信/App 验证码（仅 status==code_needed 的会话有效）。

        body：``{"code": "..."}``。码错停留 code_needed 可重试；成功→authorized，
        或→password_needed（两步验证）。
        """
        api_auth(request)
        platform = str(platform or "").lower()
        sess = get_login_manager().get(login_id)
        if sess is None:
            return {"ok": False, "status": "expired",
                    "detail": tr(request, "err.login.session_expired")}
        if sess.submit_code_fn is None:
            return {"ok": False, "status": sess.status,
                    "detail": tr(request, "err.login.code_unsupported")}
        body: Dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        code = str((body or {}).get("code") or "")
        if not code:
            return {"ok": False, "status": sess.status,
                    "detail": tr(request, "err.login.code_empty")}
        try:
            res = sess.submit_code_fn(sess, code)
            if inspect.isawaitable(res):
                res = await res
            res = res or {}
            st = str(res.get("status") or sess.status)
            sess.status = st
            rc = str(res.get("reason_code") or "")
            if rc:
                sess.reason_code = rc
            if st == "authorized" and res.get("account_id"):
                _persist_login_account(platform, str(res["account_id"]), sess)
                _funnel(sess, "authorized")
            elif st == "failed":
                _funnel(sess, "failed", reason_code=sess.reason_code)
            return {"ok": st == "authorized", "status": st,
                    "detail": str(res.get("detail") or ""),
                    "reason_code": sess.reason_code}
        except Exception:
            logger.debug("provider submit_code 失败", exc_info=True)
            return {"ok": False, "status": sess.status,
                    "detail": tr(request, "err.login.code_submit_failed")}

    @app.post("/api/platforms/{platform}/login/{login_id}/resend-code")
    async def api_platform_login_resend_code(
        platform: str, login_id: str, request: Request,
    ):
        """手机号登录：重发验证码（仅 code_needed；刷新 phone_code_hash）。"""
        api_auth(request)
        sess = get_login_manager().get(login_id)
        if sess is None:
            return {"ok": False, "status": "expired",
                    "detail": tr(request, "err.login.session_expired")}
        if sess.resend_code_fn is None:
            return {"ok": False, "status": sess.status,
                    "detail": tr(request, "err.login.code_unsupported")}
        try:
            res = sess.resend_code_fn(sess)
            if inspect.isawaitable(res):
                res = await res
            res = res or {}
            st = str(res.get("status") or sess.status)
            sess.status = st
            return {"ok": True, "status": st,
                    "detail": str(res.get("detail") or ""),
                    "reason_code": str(res.get("reason_code") or "")}
        except Exception:
            logger.debug("provider resend_code 失败", exc_info=True)
            return {"ok": False, "status": sess.status,
                    "detail": tr(request, "err.login.code_resend_failed")}

    @app.get("/api/platforms/{platform}/login/{login_id}/relay-step")
    async def api_platform_login_relay_step(platform: str, login_id: str, request: Request):
        """表单中继（Form-Relay）只读探针：登录页此刻该渲染哪一步应用内原生表单
        （credentials / twofactor / e2ee_pin / checkpoint / done / wait）。

        仅交互登录（provider 提供 relay_step_fn，即 interactive_login 开启）时可用；否则回
        reason_code=not_supported，前端走既有 headed / 截图预览路径。纯读、零副作用；探针
        异常一律软回落 wait，绝不阻断登录链。文案走结构化 reason_code + 前端本地化，路由不
        内联任何 CJK。"""
        api_auth(request)
        sess = get_login_manager().get(login_id)
        if sess is None:
            return {"ok": True, "status": "expired", "step": "wait", "fields": [],
                    "error": False, "escalate": False, "code": "",
                    "detail": tr(request, "err.login.session_expired")}
        if sess.relay_step_fn is None:
            return {"ok": False, "status": sess.status, "step": "wait", "fields": [],
                    "error": False, "escalate": False, "code": "",
                    "reason_code": "not_supported", "detail": ""}
        try:
            res = sess.relay_step_fn(sess)
            if inspect.isawaitable(res):
                res = await res
            res = res or {}
        except Exception:
            logger.debug("provider relay_step 失败", exc_info=True)
            return {"ok": True, "status": sess.status, "step": "wait", "fields": [],
                    "error": False, "escalate": False, "code": ""}
        return {
            "ok": True,
            "status": str(res.get("status") or sess.status),
            "booting": bool(res.get("booting")),
            "step": str(res.get("step") or "wait"),
            "fields": list(res.get("fields") or []),
            "error": bool(res.get("error")),
            "escalate": bool(res.get("escalate")),
            "code": str(res.get("code") or ""),
            "qr_image": str(res.get("qr_image") or ""),
        }

    @app.post("/api/platforms/{platform}/login/{login_id}/relay-submit")
    async def api_platform_login_relay_submit(platform: str, login_id: str, request: Request):
        """表单中继写入端：把应用内原生表单字段值（账密 / 2FA 码 / E2EE PIN）填回登录页。

        body：``{"step": "credentials|twofactor|e2ee_pin", "values": {...}}``。fire-and-forget
        ——边车填完即回，真正结果（→ 2FA / 密码错 / 授权）由 relay-step / status 轮询观测；
        本路由**不**改 sess.status（避免用填页当刻的过渡态覆盖轮询的权威判定）。仅交互登录
        （provider 提供 relay_submit_fn）时可用，否则 reason_code=not_supported。文案走结构化
        reason_code + 前端本地化，路由不内联 CJK。"""
        api_auth(request)
        sess = get_login_manager().get(login_id)
        if sess is None:
            return {"ok": False, "status": "expired",
                    "detail": tr(request, "err.login.session_expired")}
        if sess.relay_submit_fn is None:
            return {"ok": False, "status": sess.status,
                    "reason_code": "not_supported", "detail": ""}
        body: Dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        step = str((body or {}).get("step") or "")
        values = (body or {}).get("values") or {}
        if not isinstance(values, dict):
            values = {}
        try:
            res = sess.relay_submit_fn(sess, step, values)
            if inspect.isawaitable(res):
                res = await res
            res = res or {}
        except Exception:
            logger.debug("provider relay_submit 失败", exc_info=True)
            return {"ok": False, "status": sess.status, "reason_code": "submit_failed"}
        return {
            "ok": bool(res.get("ok")),
            "status": str(res.get("status") or sess.status),
            "step": str(res.get("step") or step),
            "reason_code": str(res.get("reason_code") or ""),
            "missing": list(res.get("missing") or []),
            "submitted": bool(res.get("submitted")),
            "accepted": bool(res.get("accepted")),
            # B64 三期：检查点「继续」回执——边车 relayClickContinue 是否真点到了页面按钮
            # （点到=前端进「已替点，跟进中」态；没点到=指路先去手机确认）。旧边车无此键
            # 时恒 False，前端另以 submitted 兜底（checkpoint 分支两者同义）。
            "clicked": bool(res.get("clicked")),
            "detail": str(res.get("detail") or ""),
        }

    @app.post("/api/platforms/{platform}/login/{login_id}/cancel")
    async def api_platform_login_cancel(platform: str, login_id: str, request: Request):
        api_auth(request)
        sess = get_login_manager().get(login_id)
        if sess is not None and sess.cancel_fn is not None:
            try:
                res = sess.cancel_fn(sess)
                if inspect.isawaitable(res):
                    await res
            except Exception:
                logger.debug("provider cancel 失败", exc_info=True)
        get_login_manager().cancel(login_id)
        return {"ok": True}
