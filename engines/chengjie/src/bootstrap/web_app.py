"""main.py web 管理后台的启动编排（Stage 2 拆分目标）。

2026-07-12 Stage 2 起，把 initialize() 内联的 FastAPI web 装配/启动逐簇迁到这里，
把闭包捕获的 self.* 显式化为 assistant 参数。首簇：web 服务线程启动。
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import threading
from typing import Any

# ★ 必须在模块级导入：本文件启用了 PEP 563（from __future__ import annotations），
# 所有注解都是字符串 ForwardRef，FastAPI 按「函数 __globals__ = 本模块全局」解析。
# make_api_auth 里的 `request: Request` 若只靠函数内局部导入，'Request' 解析失败会被
# 静默当成 query 参数——所有经 make_api_auth 挂载的端点（/api/drafts*、contacts API）
# 一律 422 {"loc":["query","request"]}。2026-07-20 生产实测踩中，故固定在模块级。
from starlette.requests import Request

from src.utils.net_helpers import is_bind_address_in_use_error


def classify_web_serve_outcome(
    exc: BaseException | None,
    started: bool,
    should_exit: bool,
    web_port: int,
) -> str | None:
    """serve() 结束后的定性（纯函数，可测）：返回 None=合法退出，否则返回致命原因。

    幽灵实例事故（2026-07-22 18:53）取证：uvicorn 绑定失败在 startup() 内部
    `sys.exit(1)`——SystemExit 是 BaseException，旧代码 `except OSError/Exception`
    全接不住；非主线程里 SystemExit 只杀线程本身，于是进程带着 Telegram 客户端
    继续裸奔（对看门狗/坐席完全不可见，却抢同一个 TG 会话）。

    合法退出只有一种：lifecycle 优雅停机（should_exit=True）。其余任何
    「web 没起来/中途死了」都按致命处理。
    """
    if should_exit:
        return None
    if exc is None:
        if started:
            # 已成功启动且非异常返回：uvicorn 实现上只在 should_exit 时返回，
            # 走到这里多半是竞态（should_exit 刚被清）；保守放行不误杀。
            return None
        return f"web 服务从未完成启动（疑似端口 {web_port} 绑定失败，被 uvicorn 内部吞掉）"
    if isinstance(exc, SystemExit):
        return (
            f"uvicorn 启动失败 sys.exit(code={exc.code})"
            f"（通常=端口 {web_port} 被占用；本进程疑似重复启动的幽灵实例）"
        )
    if isinstance(exc, OSError) and is_bind_address_in_use_error(exc):
        return f"端口 {web_port} 已被占用（本进程疑似重复启动的幽灵实例）"
    return f"web 服务异常终止: {exc!r}"


def insecure_default_secret_exposed(secret: Any, host: Any, *, allow_insecure: str | None = None) -> bool:
    """S2/S4 fail-safe 判定（纯函数）：出厂默认 session 密钥 + 绑定非本地地址 → True（应改绑回环）。

    语义与 2026-09 前的内联判定**逐字相同**：只认字面 ``change-me-in-production``（键缺失按
    默认算；空串/其它值不触发——服务器实例既有行为不变）。``allow_insecure`` 缺省读 env
    ``ALLOW_INSECURE``；为 "1" 时永不触发（运维显式放行）。
    """
    from src.utils.config_manager import ConfigManager

    flag = os.getenv("ALLOW_INSECURE") if allow_insecure is None else allow_insecure
    if str(flag or "") == "1":
        return False
    exposed = str(host or "") not in ("127.0.0.1", "::1", "localhost", "")
    value = ConfigManager.DEFAULT_WEB_SECRET if secret is None else str(secret)
    return exposed and value == ConfigManager.DEFAULT_WEB_SECRET


def handle_web_fatal(assistant: Any, reason: str, web_port: int, *, _exit=os._exit) -> None:
    """web 管理后台致命失败的处置：默认整进程立刻退出（幽灵纵深防御）。

    没有 web 的实例 = 看门狗探活/坐席/重启脚本都找不到它，却仍占着 Telegram
    会话收发消息。与其留一个不可见的幽灵，不如立刻退出让 watchdog/操作员
    走正规重启路径。`web_admin.exit_on_bind_fail: false` 可退回旧「只告警」
    行为（不建议，仅留给特殊部署逃生）。
    """
    try:
        cfg = (getattr(assistant.config, "config", {}) or {}).get("web_admin", {}) or {}
    except Exception:
        cfg = {}
    if not cfg.get("exit_on_bind_fail", True):
        assistant.logger.warning(
            "Web 管理后台未启动（%s）；exit_on_bind_fail=false，按旧行为继续运行（进程将对看门狗不可见）",
            reason,
        )
        return
    try:
        assistant.logger.critical(
            "Web 管理后台致命失败（%s）——为防幽灵实例抢占 Telegram 会话，进程立即退出（exit 78）。"
            "如确需换端口请改 web_admin.port；旧行为可用 web_admin.exit_on_bind_fail: false 恢复。",
            reason,
        )
    except Exception:
        pass
    try:
        from src.utils.host_alert import notify_host

        notify_host(
            "实例启动失败已自杀（防幽灵）",
            f"web 端口 {web_port} 启动失败：{reason}",
            key=f"web_bind_fatal:{web_port}",
        )
    except Exception:
        pass
    try:
        import logging

        logging.shutdown()
    except Exception:
        pass
    _exit(78)


def start_web_server_thread(assistant: Any, server: Any, web_host: str, web_port: int) -> threading.Thread:
    """在独立线程 + 独立 event loop 里跑 uvicorn server，避免与主 loop 抢占。

    主 loop 上的同步阻塞（SQLite 写、BM25 全表扫描）不再卡 web 请求。
    2026-07-26 起：绑定失败不再「只告警继续跑」——那正是幽灵实例的温床，
    见 classify_web_serve_outcome / handle_web_fatal。
    """
    def _run_web_in_thread():
        exc: BaseException | None = None
        try:
            web_loop = asyncio.new_event_loop()
            assistant._web_loop = web_loop
            asyncio.set_event_loop(web_loop)
            try:
                web_loop.run_until_complete(server.serve())
            finally:
                try:
                    web_loop.close()
                except Exception:
                    pass
        except BaseException as e:  # 必须含 SystemExit：uvicorn bind 失败的真实路径
            exc = e
        reason = classify_web_serve_outcome(
            exc,
            bool(getattr(server, "started", False)),
            bool(getattr(server, "should_exit", False)),
            web_port,
        )
        if reason is None:
            return
        handle_web_fatal(assistant, reason, web_port)

    web_thread = threading.Thread(
        target=_run_web_in_thread,
        name="web_admin_thread",
        daemon=True,
    )
    web_thread.start()
    return web_thread


def make_api_auth(web_app: Any):
    """构造 API 鉴权依赖：优先 admin 的 api_auth（登录校验 + 坐席白名单），
    回退 require_role('line_rpa')。参数带 Request 注解，避免 FastAPI 误判为 query 参数。

    从 initialize() 抽出并去重：原 _drafts_api_auth / _contacts_api_auth 逻辑一致。
    ⚠ Request 注解依赖模块级 import（见文件头）：PEP 563 下字符串注解按模块全局解析，
    函数内局部 import 对 FastAPI 不可见，会让 request 退化成必填 query 参数（422）。
    """
    def _api_auth(request: Request):
        _fn = getattr(web_app.state, "api_auth", None)
        if _fn is not None:
            _fn(request)
        elif hasattr(web_app.state, "require_role"):
            web_app.state.require_role(request, "line_rpa")

    return _api_auth


def start_monitoring_thread(assistant: Any):
    """按配置启动监控 API 后台线程（供前端对接）。从 initialize() 原样抽出（行为不变）。

    monitoring.enabled=false 时直接跳过。绑定失败只告警、不挡启动。返回线程或 None。
    """
    mon = getattr(assistant.config, "config", {}) or {}
    mon = mon.get("monitoring", {})
    if not mon.get("enabled", True):
        return None
    try:
        port = int(mon.get("metrics_port", 9090))
        from src.monitoring.server import run_server
        _web_cfg = assistant.config.config.get("web_admin", {})
        mon_token = mon.get("auth_token") or _web_cfg.get("auth_token", "")
        t = threading.Thread(
            target=run_server,
            kwargs={"host": "127.0.0.1", "port": port,
                    "assistant_ref": assistant, "auth_token": mon_token},
            daemon=True,
        )
        t.start()
        assistant._monitor_thread = t
        assistant.logger.info(
            "监控 API 线程已启动，正在绑定 127.0.0.1:%s（若端口被占用将在线程内失败，见日志）",
            port,
        )
        return t
    except Exception as ex:
        assistant.logger.warning(f"监控 API 启动跳过: {ex}")
        return None


def setup_web_app(assistant: Any, web_cfg: dict) -> None:
    """装配并启动 FastAPI web 管理后台(Stage5,从 main.py initialize() 原样迁出)。

    web_cfg=config.web_admin;web_admin.enabled=false 时整块跳过。创建 app、
    挂载各平台 service 到 app.state、起 web 线程,均 try/except 兜底不挡启动。"""
    if web_cfg.get("enabled"):
        try:
            import uvicorn
            from src.web.admin import create_app
            from src.utils.audit_store import AuditStore
            from src.utils.webhook import WebhookNotifier
            from src.utils.log_buffer import install_log_buffer
            _log_buf = install_log_buffer()
            cfg_dir = Path(assistant.config.config_path).parent
            wh_cfg = assistant.config.config.get("webhook", {})
            webhook = WebhookNotifier(wh_cfg) if wh_cfg.get("enabled") else None
            # W4-Cap-Alert：contacts 已 bootstrap 且 webhook 就绪 → 把 cap 阈值事件接上
            if assistant.contacts is not None and webhook is not None:
                try:
                    assistant.contacts.wire_cap_alert_webhook(webhook)
                except Exception:
                    assistant.logger.debug(
                        "wire_cap_alert_webhook 失败", exc_info=True)
            audit = AuditStore(
                db_path=cfg_dir / "audit.db",
                legacy_jsonl_path=cfg_dir / "audit_log.jsonl",
                webhook_notifier=webhook,
            )
            audit.cleanup(keep_days=90, max_rows=50000)
            try:
                from src.utils.config_advisories import (
                    record_warning_advisories_to_audit,
                )

                n = record_warning_advisories_to_audit(
                    audit, getattr(assistant, "_startup_advisory_events", []) or []
                )
                if n:
                    assistant.logger.debug("已将 %s 条配置告警写入审计", n)
                try:
                    from src.monitoring.metrics_store import get_metrics_store

                    get_metrics_store().set_startup_advisory_audit_logged(n)
                except Exception:
                    assistant.logger.debug(
                        "startup_advisory audit metrics 跳过", exc_info=True
                    )
            except Exception:
                assistant.logger.debug("配置告警写入审计跳过", exc_info=True)
            _tc_for_web = assistant.telegram_client
            web_app = create_app(
                assistant.config, audit_store=audit,
                boot_ts=(_tc_for_web._boot_timestamp
                         if _tc_for_web is not None else 0),
                telegram_client=_tc_for_web,
                event_tracker=(_tc_for_web.event_tracker
                               if _tc_for_web is not None else None),
                log_buffer=_log_buf)
            assistant._web_app = web_app  # 供收件箱后台 ingest 轮询访问 state 上的各平台 service
            # 翻译/意图等服务的兜底路径（inbox 未启用时）需要 ai_client，
            # 否则 _get_translation_service 会建出无引擎的退化实例。
            if getattr(assistant, "ai_client", None) is not None:
                web_app.state.ai_client = assistant.ai_client
            if assistant.line_rpa_service is not None:
                web_app.state.line_rpa_service = assistant.line_rpa_service
            web_app.state.line_rpa_services = assistant.line_rpa_services
            if assistant.messenger_rpa_service is not None:
                web_app.state.messenger_rpa_service = assistant.messenger_rpa_service
            if assistant.whatsapp_rpa_service is not None:
                web_app.state.whatsapp_rpa_service = assistant.whatsapp_rpa_service
            web_app.state.whatsapp_rpa_services = assistant.whatsapp_rpa_services
            if assistant.device_coordinator_service is not None:
                web_app.state.device_coordinator_service = assistant.device_coordinator_service
            if assistant.hotplug_watcher is not None:
                web_app.state.hotplug_watcher = assistant.hotplug_watcher
            if assistant.local_tts is not None:
                web_app.state.local_tts_supervisor = assistant.local_tts

            # ── G1 全局 Kill-Switch：初始化单例（回填持久化的冻结态，重启不丢）──
            try:
                from src.ops.kill_switch import get_kill_switch
                _cfg_dir0 = Path(assistant.config.config_path).parent
                _ks_cfg = ((assistant.config.config or {}).get("ops") or {}).get("kill_switch") or {}
                _ks_db = Path(_ks_cfg.get("db_path") or (_cfg_dir0 / "runtime_flags.db"))
                if not _ks_db.is_absolute():
                    _ks_db = _cfg_dir0 / _ks_db
                _ks = get_kill_switch(_ks_db)
                web_app.state.kill_switch = _ks
                _active = _ks.status()
                if _active:
                    assistant.logger.warning(
                        "🛑 Kill-Switch 启动即生效（重启回填）：%s",
                        [i["scope"] for i in _active])
                else:
                    assistant.logger.info("Kill-Switch 已就绪（%s）", _ks_db)
            except Exception:
                assistant.logger.warning("Kill-Switch 初始化跳过", exc_info=True)

            # ── 统一收件箱持久层（Phase A：纯旁路，store 故障/为空自动回落） ──
            try:
                _inbox_cfg = (assistant.config.config or {}).get("inbox", {}) or {}
                if _inbox_cfg.get("enabled", True):
                    from src.inbox.store import InboxStore

                    _cfg_dir = Path(assistant.config.config_path).parent
                    _inbox_db = Path(_inbox_cfg.get("db_path") or (_cfg_dir / "inbox.db"))
                    if not _inbox_db.is_absolute():
                        _inbox_db = _cfg_dir / _inbox_db
                    assistant.inbox_store = InboxStore(_inbox_db)
                    web_app.state.inbox_store = assistant.inbox_store
                    assistant.logger.info("统一收件箱持久层已挂载（%s）", _inbox_db)

                    # #155（2026-09-03 人设归属层级）：把人设档里的双向称呼
                    # 一次性落成其已绑定会话的**联系人级默认值**——爱称是客户
                    # 关系属性，此后各会话可独立改。幂等（联系人级已有值不覆盖）、
                    # 只搬同一个值，迁移前后生效爱称逐字节一致。
                    try:
                        from src.inbox.contact_names import migrate_all_personas
                        _cn_n = migrate_all_personas(assistant.inbox_store)
                        if _cn_n:
                            assistant.logger.info(
                                "#155 联系人级称呼存量迁移完成：%d 个会话", _cn_n)
                    except Exception:
                        assistant.logger.debug(
                            "#155 联系人级称呼迁移跳过（不影响启动）",
                            exc_info=True)

                    # ── Phase B：统一草稿/审批层（read-through 聚合 4 平台源表） ──
                    from src.inbox.drafts import DraftService
                    from src.web.routes.drafts_routes import register_drafts_routes
                    from src.ai.chat_assistant_service import quick_risk as _quick_risk

                    draft_svc = DraftService(
                        inbox_store=assistant.inbox_store,
                        line_services=assistant.line_rpa_services or [],
                        wa_services=assistant.whatsapp_rpa_services or [],
                        messenger_service=assistant.messenger_rpa_service,
                        risk_fn=_quick_risk,
                    )
                    web_app.state.draft_service = draft_svc

                    from src.bootstrap.web_app import make_api_auth
                    _drafts_api_auth = make_api_auth(web_app)

                    register_drafts_routes(web_app, api_auth=_drafts_api_auth)
                    assistant.logger.info("统一草稿层已挂载（/api/drafts）")

                    # ── Phase A：L2 草稿自动发送后台 worker ──
                    try:
                        from src.inbox.autosend_worker import AutosendWorker
                        _as_cfg = (assistant.config.config or {}).get(
                            "inbox", {}
                        ).get("l2_autosend", {}) or {}
                        # 融合实例 P1：授权档位闸门（gate 默认关 = 恒放行零变化）。
                        # 档位不含 ai_autosend → worker 不启（AI 拟稿/自动发送属 pro+）。
                        try:
                            from src.licensing.feature_gate import (
                                feature_enabled as _feat_on,
                            )
                            _autosend_allowed = _feat_on(
                                "ai_autosend", assistant.config.config or {})
                        except Exception:
                            _autosend_allowed = True
                        if not _autosend_allowed:
                            assistant.logger.info(
                                "AutosendWorker 跳过：授权档位未含 ai_autosend（feature gate）")
                        elif _as_cfg.get("enabled", True):
                            # H3：合并 auto_draft 清理配置到 worker cfg
                            _ad_cleanup = (assistant.config.config or {}).get(
                                "inbox", {}
                            ).get("auto_draft", {}) or {}
                            _merged_as_cfg = {
                                "cleanup_age_days": int(_ad_cleanup.get("cleanup_age_days", 7)),
                                "cleanup_enabled": bool(_ad_cleanup.get("cleanup_enabled", True)),
                                **_as_cfg,
                            }
                            # 全自动真实投递：默认 false（仅 DB 标记+审计，不发客户）。
                            # 置 inbox.l2_autosend.deliver=true 才真正把 L2 草稿发到平台，
                            # 且仅对会话档位=全自动(auto_ai) 的低风险草稿生效（双重 opt-in）。
                            _deliver = bool(_as_cfg.get("deliver", False))
                            from src.inbox.autosend_helpers import (
                                build_autosend_callbacks,
                                build_autosend_mark_read_cb,
                                build_autosend_typing_cb,
                            )
                            _send_cb, _translate_cb = build_autosend_callbacks(assistant, web_app, _deliver)
                            # 人工通过专用真发回调：deliver=false 时自动链 _send_cb=None，
                            # 但坐席点「通过」是人的明示决定（手动发送端点本就不受 deliver
                            # 约束），必须能真发——否则「AI 拟稿 + 人审后发」这个最谨慎档位
                            # 里发送按钮空转。
                            # P1 2026-08-12：人工链一律独立构建 origin="manual"——
                            # 人工预留额度（reserve_for_manual）下，坐席通过的草稿与
                            # 手动发送同待遇（用满额度），不再与自动链共享让路口径；
                            # 构建失败回落共享自动链回调（能力不丢，只丢 manual 待遇）。
                            try:
                                _human_send_cb, _ = build_autosend_callbacks(
                                    assistant, web_app, True, origin="manual")
                            except Exception:
                                _human_send_cb = _send_cb
                                assistant.logger.debug(
                                    "人工通过真发回调构建失败（回落自动链回调）",
                                    exc_info=True)
                            # 拟人已读回执 + 打字状态：仅真投递模式需要（DB-only 不碰平台）。
                            # always=True：开关（mark_read_before_reply / typing_indicator）
                            # 由 worker 运行时自持（apply_humanize_flags 可热更）——这里只
                            # 决定「能力在不在」，不再把开关冻进「回调建不建」。
                            _mark_read_cb = (
                                build_autosend_mark_read_cb(assistant, always=True)
                                if _deliver else None
                            )
                            _typing_cb = (
                                build_autosend_typing_cb(assistant, always=True)
                                if _deliver else None
                            )
                            # 人设解析器（人设化节奏参数 + 观测分维）：按 (platform,
                            # account_id, chat_key) 解析生效人设 id（含会话级覆写，
                            # 节奏参数跟随实际说话的人设）。仅投递模式需要；
                            # 失败/未就绪回落空（顶层默认）。
                            _persona_resolver = None
                            if _deliver:
                                def _persona_resolver(platform, account_id, chat_key="", _cfg=assistant.config.config or {}):
                                    try:
                                        from src.ai.persona_voice import (
                                            resolve_effective_persona_id as _repi,
                                        )
                                        return _repi(
                                            _cfg, platform, account_id,
                                            str(chat_key or ""),
                                        ) or ""
                                    except Exception:
                                        return ""
                            # 出站近重复守卫配置（inbox.outbound_dup_guard，默认关）：
                            # worker 只收 l2_autosend 子段拿不到全局树，这里解析注入。
                            try:
                                from src.inbox.outbound_dup_guard import (
                                    attach_rewrite_fn as _dup_attach_rw,
                                    resolve_guard_cfg as _dup_cfg_fn,
                                )
                                _dup_guard_cfg = _dup_attach_rw(
                                    _dup_cfg_fn(assistant.config.config or {}),
                                    getattr(assistant, "ai_client", None))
                            except Exception:
                                _dup_guard_cfg = None
                            # 出站事实门配置（inbox.outbound_fact_gate，默认开）：同上注入范式；
                            # 重写走 ai_client.rewrite_local，口述事实按会话键经 skill_manager 精确召回。
                            try:
                                from src.inbox.outbound_fact_gate import (
                                    attach_sources as _fg_attach,
                                    resolve_cfg as _fg_cfg_fn,
                                )
                                _fact_gate_cfg = _fg_attach(
                                    _fg_cfg_fn(assistant.config.config or {}),
                                    ai_client=getattr(assistant, "ai_client", None),
                                    skill_manager=getattr(assistant, "skill_manager", None))
                            except Exception:
                                _fact_gate_cfg = None
                            try:
                                from src.inbox.opener_guard import resolve_cfg as _og_cfg_fn
                                _opener_guard_cfg = _og_cfg_fn(assistant.config.config or {})
                            except Exception:
                                _opener_guard_cfg = None
                            # 新入站过期守卫配置（inbox.l2_autosend.fresh_guard，默认关）：
                            # 完整树解析（含 auto_draft.min_text_len 镜像——判「新入站会不会
                            # 触发新拟稿」用），worker 只收 l2_autosend 子段拿不到，这里注入。
                            try:
                                from src.inbox.draft_fresh_guard import (
                                    parse_fresh_guard_cfg as _fresh_cfg_fn,
                                )
                                _fresh_guard_cfg = _fresh_cfg_fn(
                                    assistant.config.config or {})
                            except Exception:
                                _fresh_guard_cfg = None
                            # 工作时间班表 provider（inbox.work_schedule，默认关）：
                            # 每次调用活读 config 根（overlay 热重载就地 merge，
                            # 闭包持 config_manager 引用天然看到新值）——班表改动
                            # 免重启生效。worker 只收 l2_autosend 子段拿不到全局树，
                            # 与 dup/fresh 注入同因，但作息要热调所以给闭包不给快照。
                            def _ws_provider(_cm=assistant.config):
                                try:
                                    from src.inbox.work_hours_gate import (
                                        work_schedule_cfg,
                                    )
                                    return work_schedule_cfg(
                                        getattr(_cm, "config", None) or {})
                                except Exception:
                                    return {}
                            # 驾驶权互斥锁 guard（surface_fusion P0，默认关）：
                            # 闭包活读 config 根（与 _ws_provider 同因）——overlay
                            # 开关/切换驾驶权免重启即时生效；判定 fail-open 在模块内。
                            def _pilot_guard(platform, account_id,
                                             _cm=assistant.config):
                                try:
                                    from src.integrations.surface_fusion import (
                                        autosend_blocked,
                                        note_pilot_yield,
                                    )
                                    blocked = autosend_blocked(
                                        getattr(_cm, "config", None) or {},
                                        platform, account_id)
                                    if blocked:
                                        # 让位观测（P4）：与 A 线同一读数面
                                        note_pilot_yield(
                                            platform, account_id, "autosend")
                                    return blocked
                                except Exception:
                                    return False
                            _as_worker = AutosendWorker(
                                draft_service=draft_svc,
                                config=_merged_as_cfg,
                                send_callback=_send_cb,
                                human_send_callback=_human_send_cb,
                                translate_callback=_translate_cb,
                                mark_read_callback=_mark_read_cb,
                                typing_callback=_typing_cb,
                                persona_resolver=_persona_resolver,
                                dup_guard_cfg=_dup_guard_cfg,
                                fact_gate_cfg=_fact_gate_cfg,
                                opener_guard_cfg=_opener_guard_cfg,
                                fresh_guard_cfg=_fresh_guard_cfg,
                                work_schedule_provider=_ws_provider,
                                pilot_guard=_pilot_guard,
                                app=web_app,   # Q-23 #303：软回应人设口吻短生成要拿 skill_manager
                            )
                            web_app.state.autosend_worker = _as_worker
                            # C3：注册 L2 事件驱动钩子，新草稿落库时立即唤醒
                            assistant.inbox_store.register_l2_callback(
                                _as_worker.notify_new_l2
                            )
                            # 2026-07-29：人工通过 inbox 草稿 → 经同一投递链真发送
                            # （修「坐席点发送只标记不发」断链）。**刻意不受 deliver 闸门**
                            # ——deliver 管「AI 可否自己发」，人工通过是人的明示决定；
                            # 想恢复「仅标记」旧语义置 inbox.auto_draft.human_deliver=false。
                            _human_deliver_on = bool(
                                ((assistant.config.config or {}).get("inbox", {})
                                 .get("auto_draft", {}) or {}).get("human_deliver", True))
                            # 陈旧草稿护栏：太老的稿子原样发＝穿帮（实测队列里有 8.9 天的
                            # 「我刚到家娃在拼乐高」）。0 = 关闭。见 DraftService._stale_check。
                            _stale_h = float(
                                ((assistant.config.config or {}).get("inbox", {})
                                 .get("auto_draft", {}) or {}).get(
                                    "stale_approve_hours", 24) or 0)
                            if _human_deliver_on and _human_send_cb is not None:
                                try:
                                    # Q-23 #303：守卫软回应改走 stage=soft_reply 单一闸门
                                    # （policy(kind=soft_reply) + 人工优先闸 + 场景闸），不再复用人工通过直投。
                                    draft_svc.set_inbox_deliver_callback(
                                        _as_worker.deliver_human_approved,
                                        stale_approve_hours=_stale_h,
                                        soft_reply_cb=_as_worker.deliver_soft_reply)
                                except Exception:
                                    assistant.logger.debug(
                                        "人工通过投递回调注入失败", exc_info=True)
                            elif not _human_deliver_on:
                                assistant.logger.info(
                                    "人工通过投递已按配置关闭（human_deliver=false，仅 DB 标记）")
                            asyncio.ensure_future(_as_worker.run())
                            assistant.logger.info(
                                "AutosendWorker 已启动（min=%ss max=%ss deliver=%s）",
                                _as_cfg.get("min_interval_sec", 60),
                                _as_cfg.get("max_interval_sec", 600),
                                _deliver,
                            )
                    except Exception:
                        assistant.logger.debug("AutosendWorker 启动跳过", exc_info=True)

                    # ── 人审档兜底：worker 没创建时，仍要有人消费「人工通过」──────
                    # `l2_autosend.enabled=false`（或授权档位不含 ai_autosend）时上面
                    # 整块跳过 ⇒ 坐席点「通过」只把 DB 标成 approved，**没有任何消费者
                    # 真发出去**（客户什么也没收到、坐席以为发了）。而「AI 拟稿 + 人审后发、
                    # 不要任何自动发送」恰恰是最谨慎客户最可能选的部署形态。
                    # 这里建一个 deliver_only 实例：**不 run() 自动循环**，只作人工投递载体。
                    # 放同一 state 键是刻意的（观测链零改动全通，见 AutosendWorker.__init__）。
                    # 人工发送不属 ai_autosend 授权范畴——手动发送端点本就不受其约束。
                    try:
                        if getattr(web_app.state, "autosend_worker", None) is None:
                            _ib_cfg = (assistant.config.config or {}).get("inbox", {}) or {}
                            _ad_cfg = _ib_cfg.get("auto_draft", {}) or {}
                            if (_ad_cfg.get("enabled", True)
                                    and bool(_ad_cfg.get("human_deliver", True))):
                                from src.inbox.autosend_worker import (
                                    AutosendWorker as _AW,
                                )
                                from src.inbox.autosend_helpers import (
                                    build_autosend_callbacks as _bac,
                                )
                                _hs_cb, _htr_cb = _bac(
                                    assistant, web_app, True, origin="manual")
                                if _hs_cb is not None:
                                    _do_worker = _AW(
                                        draft_service=draft_svc,
                                        config={"enabled": False},
                                        send_callback=None,      # 自动链刻意无能力
                                        human_send_callback=_hs_cb,
                                        translate_callback=_htr_cb,
                                        deliver_only=True,
                                        app=web_app,
                                    )
                                    web_app.state.autosend_worker = _do_worker
                                    # Q-23 #303：deliver_only 实例同样承载软回应闸门——档位非 auto_ai
                                    # 时闸内即转审核候选，不会因「自动发送未启用」而绕闸直投。
                                    draft_svc.set_inbox_deliver_callback(
                                        _do_worker.deliver_human_approved,
                                        stale_approve_hours=float(
                                            _ad_cfg.get("stale_approve_hours", 24) or 0),
                                        soft_reply_cb=_do_worker.deliver_soft_reply)
                                    assistant.logger.info(
                                        "人工通过投递已接线（deliver_only；自动发送未启用）")
                    except Exception:
                        assistant.logger.debug(
                            "人工投递兜底接线跳过", exc_info=True)

                    # ── P1 2026-08-22「一键全自动」热接线闭包 ─────────────
                    # deliver/worker 此前构造期冻结（开关写完 overlay 要等重启）。
                    # 路由（值守三档/能力看板/向导档位）写完 overlay 后调它，
                    # 真发能力就地武装/撤除——「点了全自动」当场生效。
                    try:
                        from src.inbox.autosend_helpers import (
                            make_autosend_rewire,
                        )
                        web_app.state.autosend_rewire = make_autosend_rewire(
                            assistant, web_app)
                    except Exception:
                        assistant.logger.debug(
                            "autosend 热接线闭包注册跳过", exc_info=True)

                    # ── K1+K2：SLAWatcher 草稿 SLA 预警 + 自动再分配 ──
                    try:
                        from src.inbox.sla_watcher import SLAWatcher
                        _sw_cfg = (assistant.config.config or {}).get(
                            "inbox", {}
                        ).get("sla_watcher", {}) or {}
                        if _sw_cfg.get("enabled", True):
                            _sw = SLAWatcher(
                                draft_service=draft_svc,
                                inbox_store=assistant.inbox_store,
                                config=_sw_cfg,
                            )
                            web_app.state.sla_watcher = _sw
                            asyncio.ensure_future(_sw.run())
                            assistant.logger.info(
                                "SLAWatcher 已启动（sla=%.0fh tick=%.0fs absent=%.0fs）",
                                float(_sw_cfg.get("sla_hours", 4)),
                                float(_sw_cfg.get("tick_sec", 60)),
                                float(_sw_cfg.get("absent_sec", 300)),
                            )
                    except Exception:
                        assistant.logger.debug("SLAWatcher 启动跳过", exc_info=True)

                    # ── P3：AutoClaimWorker auto_assign 自动认领执行端 ──
                    # 默认关（workspace.auto_assign.auto_claim.enabled=false）；
                    # worker 每 tick 重读配置，开关无需重启。仅在 inbox 可用时启。
                    try:
                        from src.workspace.auto_claim_worker import AutoClaimWorker
                        _ac_cfg = (((assistant.config.config or {}).get(
                            "workspace", {}) or {}).get(
                            "auto_assign", {}) or {}).get("auto_claim", {}) or {}
                        _acw = AutoClaimWorker(
                            inbox_store=assistant.inbox_store,
                            config_manager=assistant.config,
                            config=_ac_cfg,
                        )
                        web_app.state.auto_claim_worker = _acw
                        asyncio.ensure_future(_acw.run())
                        assistant.logger.info(
                            "AutoClaimWorker 已启动（默认关，按 auto_claim.enabled 热生效）")
                    except Exception:
                        assistant.logger.debug("AutoClaimWorker 启动跳过", exc_info=True)

                    # ── 入站翻译存量消化（低频巡检，默认关）─────────
                    # workspace.auto_translate_inbound.backfill.enabled=true 开启；
                    # 闲时把老会话未译存量提前译好落库，坐席首开即毫秒级+译文备好。
                    # 复用 enrich 同一套判定/防重/负缓存（会话级锁与在线路径互斥）。
                    try:
                        from src.workspace.inbound_backfill import (
                            InboundXlateBackfillWorker,
                        )
                        _bfw = InboundXlateBackfillWorker(
                            inbox_store=assistant.inbox_store,
                            config_manager=assistant.config,
                            translation_svc_getter=lambda: getattr(
                                web_app.state, "translation_service", None),
                        )
                        web_app.state.inbound_backfill_worker = _bfw
                        asyncio.ensure_future(_bfw.run())
                        assistant.logger.info(
                            "InboundXlateBackfill 已启动（默认关，按 backfill.enabled 热生效）")
                    except Exception:
                        assistant.logger.debug("InboundXlateBackfill 启动跳过", exc_info=True)

                    # ── L2：WebhookNotifier 企业 IM 通知 ──────────────
                    try:
                        from src.inbox.webhook_notifier import WebhookNotifier
                        # 有效列表：notify_webhooks.json 覆盖层优先，否则 config.yaml
                        try:
                            from src.integrations.notify_webhooks_store import (
                                effective_webhooks,
                            )
                            _wh_list = effective_webhooks(assistant.config.config or {})
                        except Exception:
                            _wh_list = (assistant.config.config or {}).get(
                                "notify", {}
                            ).get("webhooks", []) or []
                        # 即使当前为空也创建 notifier：便于后台「告警渠道」面板
                        # 运行时 reload() 增删，免重启
                        _whn = WebhookNotifier(config=_wh_list)
                        web_app.state.webhook_notifier = _whn
                        asyncio.ensure_future(_whn.run())
                        assistant.logger.info(
                            "WebhookNotifier 已启动（%d 个 webhook）",
                            len(_wh_list),
                        )
                    except Exception:
                        assistant.logger.debug("WebhookNotifier 启动跳过", exc_info=True)

                    # ── D3：HealthWatchdog 运行时健康主动告警 ─────────
                    # 默认开；周期巡检 D1 健康，异常经 EventBus→WebhookNotifier
                    # 推送（需在「告警渠道」订阅 health_alert 事件才会真正发出）。
                    try:
                        from src.inbox.health_watchdog import HealthWatchdog
                        _hw_cfg = (assistant.config.config or {}).get(
                            "health_watchdog", {}
                        ) or {}
                        if _hw_cfg.get("enabled", True):
                            _hw = HealthWatchdog(
                                app=web_app,
                                config_manager=assistant.config,
                                interval_sec=float(_hw_cfg.get("interval_sec", 300)),
                                pending_threshold=int(_hw_cfg.get("queue_threshold", 200)),
                                alert_on_warn=bool(_hw_cfg.get("alert_on_warn", False)),
                                billing_interval_sec=float(_hw_cfg.get("billing_interval_sec", 3600)),
                                incident_retention_days=float(_hw_cfg.get("incident_retention_days", 30)),
                                weekly_report_enabled=bool(_hw_cfg.get("weekly_report_enabled", False)),
                                weekly_interval_sec=float(_hw_cfg.get("weekly_interval_sec", 604800)),
                                daily_report_enabled=bool(_hw_cfg.get("daily_report_enabled", False)),
                                daily_interval_sec=float(_hw_cfg.get("daily_interval_sec", 86400)),
                            )
                            web_app.state.health_watchdog = _hw
                            asyncio.ensure_future(_hw.run())
                            assistant.logger.info(
                                "HealthWatchdog 已启动（interval=%ss alert_on_warn=%s）",
                                _hw_cfg.get("interval_sec", 300),
                                _hw_cfg.get("alert_on_warn", False),
                            )
                    except Exception:
                        assistant.logger.debug("HealthWatchdog 启动跳过", exc_info=True)

                    # ── 实施97：官网中继设备端（relay.enabled）——把企微回调/成员登录回跳带进 NAT 后的本实例 ──
                    try:
                        from src.integrations.relay_client import RelayClient, ensure_identity, relay_config
                        _rc = relay_config(assistant.config.config or {})
                        if _rc["enabled"]:
                            _dev_id, _dev_secret = ensure_identity(assistant.config)
                            _web = (assistant.config.config or {}).get("web_admin") or {}
                            _relay = RelayClient(
                                relay_url=_rc["url"], device_id=_dev_id, secret=_dev_secret,
                                local_base=f"http://127.0.0.1:{int(_web.get('port') or 18799)}",
                                register_key=_rc["register_key"], ping_sec=_rc["ping_sec"],
                                app_version=str(getattr(web_app.state, "version", "") or ""),
                            )
                            web_app.state.relay_client = _relay
                            asyncio.ensure_future(_relay.run_forever())
                            assistant.logger.info("官网中继设备端已启动：%s → 公网前缀 %s", _rc["url"], _relay.public_base)
                    except Exception:
                        assistant.logger.debug("官网中继设备端启动跳过", exc_info=True)

                    # ── N2：ScheduledReporter 定时简报推送 ─────────────
                    try:
                        from src.inbox.scheduled_reporter import ScheduledReporter
                        _rpt_cfg = (assistant.config.config or {}).get(
                            "report", {}
                        ) or {}
                        if _rpt_cfg.get("enabled", False):
                            _rpt = ScheduledReporter(
                                inbox_store=web_app.state.inbox_store,
                                draft_service=getattr(web_app.state, "draft_service", None),
                                app_state=web_app.state,
                                config=_rpt_cfg,
                            )
                            web_app.state.scheduled_reporter = _rpt
                            asyncio.ensure_future(_rpt.run())
                            assistant.logger.info(
                                "ScheduledReporter 已启动（daily=%s weekly=%s）",
                                _rpt_cfg.get("daily_time", "09:00"),
                                _rpt_cfg.get("weekly_day") or "禁用",
                            )
                    except Exception:
                        assistant.logger.debug("ScheduledReporter 启动跳过", exc_info=True)

                    # ── P2 2026-08-09：目标结算/提醒扫描（常备循环）─────────
                    # 刻意不搭 ScheduledReporter 便车——那个调度器受 report.enabled
                    # 闸（生产常年关），P0 首版挂那里导致扫描从未运行（实锤见
                    # goals/notify.py 模块注释）。与 care 引擎同哲学：循环无条件
                    # 启动、每 tick 现读配置自闸（goals/sweep/notify 全关＝零开销），
                    # 开关经 overlay 热重载免重启。state 挂 app.state 当心跳快照。
                    try:
                        from src.companion.goals.notify import run_scan_loop
                        _gscan_state: dict = {}
                        web_app.state.goal_scan_state = _gscan_state
                        asyncio.ensure_future(run_scan_loop(
                            assistant.config,
                            inbox_store=web_app.state.inbox_store,
                            state=_gscan_state,
                        ))
                        assistant.logger.info(
                            "目标结算/提醒扫描循环已挂载（常备接线，配置热自闸）")
                    except Exception:
                        assistant.logger.debug(
                            "目标扫描循环启动跳过", exc_info=True)

                    # ── P3 2026-08-09：工作链推进常备循环 ────────────────
                    # 同一次事故的同类病：链推进原挂 ScheduledReporter
                    # （report.enabled 闸，生产常年关）＝坐席点「启动工作链」
                    # 后步骤永不推进。迁到常备循环（inbox.workflows.autorun
                    # 默认开可热关）。生产现状 4 条种子链 0 执行 → 迁移当天
                    # 零行为变化，链从此「点了真会走」。
                    try:
                        from src.inbox.workflow_autorun import run_workflow_loop
                        _wfrun_state: dict = {}
                        web_app.state.workflow_autorun_state = _wfrun_state
                        asyncio.ensure_future(run_workflow_loop(
                            web_app.state, state=_wfrun_state))
                        assistant.logger.info(
                            "工作链推进循环已挂载（常备接线，配置热自闸）")
                    except Exception:
                        assistant.logger.debug(
                            "工作链推进循环启动跳过", exc_info=True)

                    # E2/F2：按 auto_draft 配置注册入站新消息 → 自动草稿生成回调
                    from src.inbox.autodraft_helpers import setup_auto_draft
                    setup_auto_draft(assistant, draft_svc, web_app)

                    # 复班补觉重拟接线（work_schedule.off_hours.catch_up）：
                    # setup_auto_draft 把拟稿回调存进 app.state.auto_draft_cb 后，
                    # 回填给 AutosendWorker——重拟走**原拟稿产线**（enrich/人设/
                    # 档位封顶全生效）。skip_companion_yield=True：补觉重拟的消息
                    # A 线早已跳过（当时休息中），双轨互斥的「让位」在此必须旁路，
                    # 否则 TG 陪伴号的隔夜消息作废后无人重拟=静默丢回复。
                    # 任一句柄缺席（auto_draft 关/worker 没建）→ 不接线，
                    # worker 侧「未注入就绝不作废」的铁律兜底。
                    try:
                        _adc = getattr(web_app.state, "auto_draft_cb", None)
                        _asw = getattr(web_app.state, "autosend_worker", None)
                        if callable(_adc) and _asw is not None and hasattr(
                                _asw, "set_catchup_regenerate_cb"):
                            def _catchup_regen(conv, text, _cb=_adc):
                                _cb(conv, text, skip_companion_yield=True)
                                return True
                            _asw.set_catchup_regenerate_cb(_catchup_regen)
                    except Exception:
                        assistant.logger.debug(
                            "补觉重拟回调接线跳过", exc_info=True)

                    # I3：预置回复模板库（幂等，id 冲突则跳过）
                    try:
                        from src.inbox.template_seeds import SEED_TEMPLATES
                        _seeded = assistant.inbox_store.seed_templates(SEED_TEMPLATES)
                        if _seeded > 0:
                            assistant.logger.info("模板库已预置 %d 条种子模板", _seeded)
                    except Exception:
                        assistant.logger.debug("模板库预置跳过", exc_info=True)

                    # ── Phase C：意图 LLM 升级 + 翻译记忆持久化（预置带依赖的 service） ──
                    _cfg_root = assistant.config.config or {}
                    _ia_cfg = _cfg_root.get("intent_analysis", {}) or {}
                    _tr_cfg = _cfg_root.get("translation", {}) or {}
                    from src.ai.chat_assistant_service import ChatAssistantService
                    web_app.state.chat_assistant_service = ChatAssistantService(
                        ai_client=assistant.ai_client,
                        use_llm=bool(_ia_cfg.get("use_llm", False)),
                        analysis_store=assistant.inbox_store,
                        timeout_sec=float(_ia_cfg.get("timeout_sec", 8) or 8),
                    )
                    _tm_store = None
                    if (_tr_cfg.get("memory", {}) or {}).get("enabled", True):
                        from src.ai.translation_memory import TranslationMemoryStore
                        _tm_db = Path(
                            (_tr_cfg.get("memory", {}) or {}).get("db_path")
                            or (_cfg_dir / "translation_memory.db")
                        )
                        if not _tm_db.is_absolute():
                            _tm_db = _cfg_dir / _tm_db
                        _tm_store = TranslationMemoryStore(_tm_db)
                        assistant.translation_memory = _tm_store
                    # P56：术语库（全局+域包合并）+ 多引擎路由
                    from src.ai.translation_glossary import build_glossary
                    from src.ai.translation_engines import build_engines
                    _domain_files = []
                    try:
                        _dom_dir = Path(assistant.config.config_path).parent.parent / "domains"
                        if _dom_dir.exists():
                            _domain_files = list(_dom_dir.glob("*/prompts/terminology.yaml"))
                    except Exception:
                        _domain_files = []
                    # P59：术语库可编辑覆盖层（后台控制台增删改，最高优先）
                    from src.ai.glossary_store import GlossaryStore
                    _gloss_ov_path = _cfg_dir / "glossary_overrides.yaml"
                    _gloss_store = GlossaryStore(_gloss_ov_path)
                    _gloss_overrides = _gloss_store.load()
                    _glossary = build_glossary(
                        _cfg_root, domain_files=_domain_files, overrides=_gloss_overrides,
                    )
                    _engines = build_engines(_tr_cfg, assistant.ai_client)
                    # K：引擎置信度智能切换（默认关 → min_confidence=0 行为不变）
                    _conf_sw = (_tr_cfg.get("engines") or {}).get("confidence_switch") or {}
                    _min_conf = (
                        float(_conf_sw.get("min_confidence", 0.5) or 0.5)
                        if _conf_sw.get("enabled", False) else 0.0
                    )
                    # 按目标语引擎覆写（弱语对直走强引擎；只重排 order 内引擎）
                    _per_lang = (_tr_cfg.get("engines") or {}).get("per_lang_order") or {}
                    # 在线语义闸门（confidence_switch 的可选进阶；默认关。
                    # 开启需 confidence_switch.enabled + semantic.enabled + 嵌入端点已配）
                    _sem_cfg = _conf_sw.get("semantic") or {}
                    _sem_fn = None
                    _sem_min = float(_sem_cfg.get("min_similarity", 0.65) or 0.65)
                    if (_conf_sw.get("enabled", False)
                            and _sem_cfg.get("enabled", False)
                            and assistant.ai_client is not None
                            and hasattr(assistant.ai_client, "embed")):
                        _sem_fn = assistant.ai_client.embed
                    # 存重建上下文，供 /api/workspace/glossary 热更新复用
                    web_app.state.glossary_store = _gloss_store
                    web_app.state.glossary_config = _cfg_root
                    web_app.state.glossary_domain_files = _domain_files
                    from src.ai.translation_service import TranslationService
                    web_app.state.translation_service = TranslationService(
                        ai_client=assistant.ai_client,
                        memory_store=_tm_store,
                        glossary_terms=_glossary.terms,
                        glossary_version=_glossary.version,
                        glossary_protect=_glossary.protect,
                        cost_tracking=bool(_tr_cfg.get("cost_tracking", False)),
                        engines=_engines,
                        min_confidence=_min_conf,
                        per_lang_order=_per_lang,
                        semantic_embed_fn=_sem_fn,
                        semantic_min_similarity=_sem_min,
                    )
                    assistant.logger.info(
                        "Phase C/P56 服务已预置（意图LLM=%s, 翻译记忆=%s, 引擎=%s, 术语=%d, 保护词=%d）",
                        bool(_ia_cfg.get("use_llm", False)),
                        _tm_store is not None,
                        "→".join(e.name for e in _engines),
                        len(_glossary.terms), len(_glossary.protect),
                    )

                    # ── Phase B：可选统计语种检测（缺库自动跳过，仅精修含糊拉丁） ──
                    try:
                        _ld_cfg = ((_tr_cfg.get("lang_detect") or {}).get("statistical") or {})
                        if _ld_cfg.get("enabled", False):
                            from src.ai.lang_detect_statistical import build_statistical_detector
                            from src.ai.translation_service import set_statistical_detector
                            _stat_fn = build_statistical_detector()
                            if _stat_fn is not None:
                                set_statistical_detector(
                                    _stat_fn,
                                    min_chars=int(_ld_cfg.get("min_chars", 12) or 12),
                                )
                                assistant.logger.info("统计语种检测已启用（回退精修含糊拉丁）")
                            else:
                                assistant.logger.warning(
                                    "translation.lang_detect.statistical.enabled=true 但未装 lingua/langdetect，已跳过"
                                )
                    except Exception:
                        assistant.logger.debug("统计语种检测装配跳过", exc_info=True)

                    # ── Phase D：电商工具层（订单/物流查询 + 事实校验 + 审计） ──
                    _ec_cfg = _cfg_root.get("ecommerce_tools", {}) or {}
                    if _ec_cfg.get("enabled", False):
                        from src.ecommerce_tools import (
                            EcommerceToolService, build_connector,
                            build_logistics_connector,
                        )
                        from src.web.routes.ecommerce_tools_routes import (
                            register_ecommerce_tools_routes,
                        )
                        _ec_conn = build_connector(_ec_cfg)
                        _logi_conn = build_logistics_connector(_ec_cfg.get("logistics") or {})
                        assistant.ecommerce_tools = EcommerceToolService(
                            _ec_conn, audit_store=audit,
                            timeout_sec=float(_ec_cfg.get("timeout_sec", 8) or 8),
                            cache_ttl_sec=float(_ec_cfg.get("cache_ttl_sec", 0) or 0),
                            cache_max_entries=int(_ec_cfg.get("cache_max_entries", 512) or 512),
                            logistics_connector=_logi_conn,
                        )
                        web_app.state.ecommerce_tools = assistant.ecommerce_tools
                        register_ecommerce_tools_routes(
                            web_app, api_auth=_drafts_api_auth,
                        )
                        # P1-b：注入回复生成链路 → 命中订单号自动带真实事实/反幻觉守卫
                        if assistant.ai_client is not None:
                            assistant.ai_client.set_ecommerce_tools(assistant.ecommerce_tools)
                        assistant.logger.info(
                            "电商工具层已挂载（provider=%s, /api/tools/ecommerce/* + 回复事实注入）",
                            assistant.ecommerce_tools.connector_name,
                        )
            except Exception:
                assistant.logger.warning("统一收件箱持久层挂载跳过", exc_info=True)

            # ── 挂载 Contacts 路由（仅 contacts 子系统启用时） ──
            if assistant.contacts is not None:
                try:
                    from src.web.routes.contacts_routes import (
                        register_contacts_routes,
                    )

                    from src.bootstrap.web_app import make_api_auth
                    _contacts_api_auth = make_api_auth(web_app)

                    register_contacts_routes(
                        web_app,
                        api_auth=_contacts_api_auth,
                        contacts_store=assistant.contacts.store,
                        merge_service=assistant.contacts.merge_svc,
                        audit_store=audit,
                        intimacy_engine=assistant.contacts.intimacy_engine,
                        reactivation_scheduler=assistant.contacts.reactivation,
                        eval_scheduler=getattr(
                            assistant.contacts, "draft_eval_scheduler", None,
                        ),
                        gateway=assistant.contacts.gateway,
                        account_limiter=assistant.contacts.limiter,
                        mobile_bridge=assistant.mobile_bridge,
                        fire_webhook=getattr(
                            web_app.state, "fire_webhook", None,
                        ),
                        ai_client=assistant.ai_client,
                    )
                    # 让 web 能通过 state 直接访问
                    web_app.state.contacts = assistant.contacts
                    assistant.logger.info("Contacts Web 路由已注册（/api/contacts /ops/contacts）")
                except Exception:
                    assistant.logger.warning(
                        "Contacts 路由注册跳过", exc_info=True)
                # 把 state_store 也挂上，路由能直接读 approvals
                try:
                    web_app.state.messenger_rpa_state_store = (
                        assistant.messenger_rpa_service.state_store
                    )
                except Exception:
                    assistant.logger.debug(
                        "messenger_rpa state_store 注入跳过", exc_info=True
                    )
            # ★ P1-2：Suggest More 端点需要 SkillManager
            if assistant.skill_manager is not None:
                web_app.state.skill_manager = assistant.skill_manager
            web_port = int(web_cfg.get("port", 8080))
            web_host = web_cfg.get("host", "127.0.0.1")
            # S2/S4 fail-safe：默认 secret_key + 绑定非本地地址 = 危险暴露（session 可伪造）。
            # 除非显式 ALLOW_INSECURE=1，否则降级绑回 127.0.0.1 并告警（不 crash 整进程）。
            # 桌面态走不到这里：ConfigManager._ensure_web_secret_key 首启已随机生成（L-6 D）。
            if insecure_default_secret_exposed(web_cfg.get("secret_key"), web_host):
                assistant.logger.error(
                    "[SECURITY] 检测到默认 secret_key 且绑定非本地地址 %s；为防不安全暴露，"
                    "Web 后台改绑 127.0.0.1。请配置随机 web_admin.secret_key，或设 ALLOW_INSECURE=1。",
                    web_host,
                )
                web_host = "127.0.0.1"
            uvi_config = uvicorn.Config(web_app, host=web_host, port=web_port, log_level="warning")
            server = uvicorn.Server(uvi_config)
            assistant._web_server = server

            # ★ 隔离 web 到独立线程 + 独立 event loop（Stage 2：启动逻辑抽到
            # src/bootstrap/web_app.py::start_web_server_thread，行为不变）
            from src.bootstrap.web_app import start_web_server_thread
            assistant._web_thread = start_web_server_thread(assistant, server, web_host, web_port)
            assistant.logger.info(
                "Web 管理后台正在绑定 http://%s:%s（独立线程隔离，避免抢占主 event loop）",
                web_host,
                web_port,
            )
        except Exception as ex:
            assistant.logger.warning("Web 管理后台启动跳过: %s", ex)
