#!/usr/bin/env python3
"""
Telegram MTProto AI Chat Assistant 主程序入口

基于 Telegram User API (MTProto) + 大模型 API + Skill 工作流的自动化客服/对话系统。
"""

import asyncio
import sys
import signal
import logging
import threading
import time
import os
from pathlib import Path

# Windows console 默认 cp936；强制 UTF-8 防止日文/emoji 被 stdout 重定向时损坏。
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# 添加项目根目录到Python路径
sys.path.insert(0, str(Path(__file__).parent))

# 打包态副驾驱动分流（2026-09-19，个人微信 PC 副驾 1.0.90 进包）：PyInstaller 产物 backend.exe
# 没有 `python -m`，supervisor 在 frozen 态用 `backend.exe --wechat-pc-driver <args>` 拉驱动子进程
#（见 src/integrations/wechat_pc/supervisor.py build_driver_command）。首参命中即整段转交驱动入口，
# 放在重 import（AIClient/SkillManager）之前——驱动进程不该为后端主链的 import 付费，也绝不
# 走到下面的后端启动（否则就是第二个后端抢 18799）。源码态 `-m src.integrations.wechat_pc` 不受影响。
if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--wechat-pc-driver":
    from src.integrations.wechat_pc.__main__ import main as _wechat_pc_driver_main
    sys.exit(_wechat_pc_driver_main(sys.argv[2:]))

# P3-2（2026-08-12 可靠性复盘）：TelegramClient 顶层 import 已移除——它是死代码
# （构造点在 bootstrap/services.py::setup_telegram_clients，人家自己函数内 import），
# 却把 pyrogram 的 ~5s import（raw.types 生成为主，-X importtime 实测 6.98s 链）
# 提前到进程启动第 0 秒、挡在一切之前。移除后该成本落回 telegram_clients 阶段，
# boot phases 归因变诚实，也为将来 web-first 排序解锁 runway。
from src.ai.ai_client import AIClient
from src.skills.skill_manager import SkillManager
from src.utils.config_manager import ConfigManager
from src.utils.logger import setup_logger
from src.utils.net_helpers import is_bind_address_in_use_error
from src.utils.domain_policy import effective_domain_name


# 启动期环境/配置探测辅助已抽取到 src/bootstrap/env_probe.py（2026-07-12 Stage 1.5，行为不变）
# 保留 main.* 命名以兼容 tests/test_desktop_boot_gate.py 的 main._telegram_configured 等访问。
from src.bootstrap.env_probe import (
    _is_desktop_mode,
    _resolve_mobile_auto_openclaw_db,
    _telegram_configured,
)


class AIChatAssistant:
    """AI聊天助手主类"""

    def __init__(self):
        """初始化AI聊天助手"""
        self.config = None
        self.telegram_client = None          # primary (backward-compat)
        self.telegram_clients: list = []     # all accounts (including primary)
        self.ai_client = None
        self.skill_manager = None
        self.logger = None
        self.running = False
        self.line_rpa_service = None         # primary (backward-compat)
        self.line_rpa_services: list = []      # all LINE accounts
        self.messenger_rpa_service = None
        self.whatsapp_rpa_service = None        # primary (backward-compat)
        self.whatsapp_rpa_services: list = []   # all WhatsApp accounts
        self.device_coordinator_service = None   # 多平台设备协调器（可选）
        self.hotplug_watcher = None              # ADB 热插拔自动纳管（可选）
        # Phase A：统一收件箱持久层（纯旁路；store 故障/为空自动回落实时聚合）
        self.inbox_store = None  # type: Optional[Any]  # noqa: F821
        # Phase C：翻译记忆持久层（可选）
        self.translation_memory = None  # type: Optional[Any]  # noqa: F821
        # Phase D：电商工具服务（可选）
        self.ecommerce_tools = None  # type: Optional[Any]  # noqa: F821
        # W2-W4：跨平台 Contacts 子系统（feature flag 控制；默认关）
        self.contacts = None  # type: Optional["ContactsSubsystem"]  # noqa: F821
        # Mobile Bridge：mobile-auto0423 ↔ telegram-mtproto-ai 双向同步
        self.mobile_bridge = None  # type: Optional[Any]  # noqa: F821
        self._telegram_task = None
        self._secondary_tg_tasks: list = []  # extra account tasks
        # D: web admin 隔离到独立线程
        self._web_thread = None
        self._web_loop = None
        self._web_server = None
        # 本机 IndexTTS2 情感克隆服务的进程托管（随主程序启停；见 local_autostart 开关）
        self.local_tts = None
        # W2-D4: 主动唤醒循环引用（关程序时 stop）
        self._reactivation_loop = None
        # Phase O: 主动关怀派发器引用（关程序时 stop）
        self._care_dispatcher = None
        # P2: care LLM 抽取影子扫描器引用（关程序时 stop）
        self._care_shadow_scanner = None
        # P2: 陪伴主动话题调度循环引用（关程序时 stop）
        self._companion_proactive_loop = None
        self._companion_funnel_store = None
        # 多平台 deferred 队列（非 messenger 主动消息走此队列；关程序时 stop）
        self._deferred_outbox_dispatcher = None
        # 质量趋势持久化快照器（周期落地 companion_quality_overview；关程序时 stop）
        self._quality_trend_snapshotter = None
        # 坐席工作台实时化（D5a）：收件箱后台 ingest 轮询任务 + web_app 引用
        self._web_app = None  # type: Optional[Any]  # noqa: F821
        self._inbox_ingest_task = None
        # Phase 11：启动分阶段计时（冷启动归因）；initialize() 早期实例化，失败绝不挡启动
        self._boot_timer = None  # type: Optional[Any]  # noqa: F821

    def _boot_mark(self, name: str) -> None:
        """标记一个启动阶段边界（None 安全 + 异常安全，绝不影响启动流程）。"""
        t = getattr(self, "_boot_timer", None)
        if t is not None:
            try:
                t.mark(name)
            except Exception:
                pass

    async def initialize(self):
        """初始化所有组件"""
        try:
            # Phase 11：启动分阶段计时起点（尽早创建，覆盖 config 加载起）。
            try:
                from src.bootstrap.boot_timing import BootTimer
                self._boot_timer = BootTimer()
            except Exception:
                self._boot_timer = None

            # 1. 先设置一个临时的控制台日志记录器
            self.logger = setup_logger(log_file=None, console_output=True)
            self.logger.info("开始初始化AI聊天助手...")

            # 2. 加载配置
            self.config = ConfigManager()
            await self.config.load()
            self.logger.info("配置加载成功")
            self._boot_mark("config")

            # 2b. 本机情感克隆(IndexTTS2)进程托管：随主程序一起启停（默认关，见
            #     minicpm_clone.local_autostart）。尽早拉起，让 ~60-90s 的 eager 载入
            #     与后续初始化/登录并行；非阻塞，失败只回落 edge，绝不挡启动。
            try:
                from src.integrations.local_tts_supervisor import LocalTTSSupervisor
                self.local_tts = LocalTTSSupervisor(
                    self.config.config.get("minicpm_clone") or {})
                await self.local_tts.start()
            except Exception as ex:
                self.logger.warning("本机 TTS 托管启动异常（忽略，语音走回落）: %s", ex)

            # 2c. AvatarHub 语音预热：对每个配了参考音的人设调 7852 register_spk
            #     （显著降首句延迟）。后台 daemon 线程 fire-and-forget：服务没起会先经
            #     计划任务拉起再轮询；任何失败只影响首句延迟，绝不挡启动/主流程。
            try:
                from src.ai.avatar_voice import warmup_personas_async
                if (self.config.config.get("avatar_voice") or {}).get("enabled"):
                    warmup_personas_async(self.config.config)
                    self.logger.info("AvatarHub 语音预热已调度（后台）")
            except Exception as ex:
                self.logger.warning("AvatarHub 语音预热调度异常（忽略）: %s", ex)
            # 2c'. #333 视觉身份层人设原型预热：常驻人设的人脸原型算好落 JSON 缓存，
            #     首图不再冷算 ~600ms。同 AvatarHub 模式：后台 daemon fire-and-forget。
            #     未启用 / 边车不健康由 warmup 自己放弃（入站时照旧现算），绝不挡启动。
            try:
                from src.companion.face_identity import warmup_persona_prototypes_async
                warmup_persona_prototypes_async(
                    self.config.config, config_path=getattr(self.config, "config_path", None))
            except Exception as ex:
                self.logger.warning("视觉身份层预热调度异常（忽略）: %s", ex)
            self._boot_mark("local_tts")

            # 2d. 协议媒体旧根迁移（账号资产 P0，2026-08-19）：媒体根迁实例数据根
            #     （protocol_bridge.protocol_media_root）后，把引擎树
            #     static/protocol_media 的存量逐文件搬进数据根（同盘 rename 秒级；
            #     幂等，单文件失败下次启动重试）。daemon fire-and-forget，绝不挡启动。
            #     ⚠ 只能挂这里（真实服务启动）——绝不挂 app 装配路径：pytest 也装配
            #     app，测试态数据根是即弃 tmp，在那触发会把生产媒体搬进临时目录。
            try:
                from src.integrations.protocol_bridge import (
                    migrate_legacy_protocol_media,
                )

                def _pm_migrate() -> None:
                    st = migrate_legacy_protocol_media()
                    if st.get("moved") or st.get("failed"):
                        self.logger.info("协议媒体旧根迁移完成: %s", st)

                threading.Thread(target=_pm_migrate, name="pm_media_migrate",
                                 daemon=True).start()
            except Exception as ex:
                self.logger.warning("协议媒体迁移调度异常（忽略）: %s", ex)

            # 3. 根据配置重新配置日志记录器
            log_config = self.config.config.get("logging", {})
            from src.bootstrap.logging_setup import setup_logging
            setup_logging(self, log_config)

            # 3b. 进程退出可观测（2026-07-12 无痕死亡排障配套）：哨兵残留检测上次
            # 非正常死亡（taskkill /F / OOM 等任何死法）+ atexit/signal 记退出原因 +
            # faulthandler 落致命 traceback。失败绝不挡启动。
            _prev_exit = None
            try:
                from src.utils.exit_sentinel import install as _install_exit_obs
                _prev_exit = _install_exit_obs()
            except Exception:
                self.logger.debug("退出可观测安装失败（已忽略）", exc_info=True)

            # 3c. 客户端错误回传（桌面版默认开；telemetry.client_errors.enabled: false 可关）：
            # 公网安装版的 ERROR 摘要回官网归集，装在别人机器上的故障不再失明。
            # 上次崩溃/OOM（哨兵残留，日志里无 ERROR）也一并回传——远程可发现崩溃。
            try:
                from src.utils.telemetry_beacon import install_beacon
                _beacon = install_beacon(self.config.config)
                if _beacon is not None and _prev_exit:
                    _beacon.note_prev_exit(_prev_exit)
            except Exception:
                self.logger.debug("错误回传 beacon 安装失败（已忽略）", exc_info=True)

            self._boot_mark("logging")

            # 3. 初始化AI客户端
            # 托管版：向官网换设备令牌（用户零配 Key；真云 Key 只在服务端）。
            # 守护线程每小时续期/补领（首启无网或未领试用时条件满足自动接入）。
            try:
                from src.ai.hosted_gateway import (
                    ensure_hosted_ai, ensure_hosted_asr, ensure_hosted_embed,
                    ensure_hosted_telegram, ensure_hosted_vision,
                    ensure_hosted_voice, ensure_hosted_chatx, start_refresh_daemon)
                if await asyncio.to_thread(ensure_hosted_ai, self.config):
                    self.logger.info("托管 AI 网关已就绪（设备令牌）")
                # 托管 Telegram 凭据：用户只登录、不填 api_id/hash（池未配则静默跳过）
                if await asyncio.to_thread(ensure_hosted_telegram, self.config):
                    self.logger.info("托管 Telegram 凭据已就绪（用户无需申请 api_id）")
                # 托管识图：识图指向官网网关（我们的 GPU 模型；中继未开则不影响，客户端回落）
                if await asyncio.to_thread(ensure_hosted_vision, self.config):
                    self.logger.info("托管识图已就绪（经网关调我们的 GPU VLM）")
                # 托管克隆语音 / 语音识别（混合形态）：LAN 直连优先，出网走网关回集群 GPU。
                # 必须先于各 worker 构建转写器（转写器构建一次常驻）。
                if await asyncio.to_thread(ensure_hosted_voice, self.config):
                    self.logger.info("托管克隆语音已就绪（LAN 不可达时经网关回集群 GPU）")
                if await asyncio.to_thread(ensure_hosted_asr, self.config):
                    self.logger.info("托管语音识别已就绪（LAN 不可达时经网关转写）")
                # 托管嵌入：必须先于 AIClient 构建（嵌入客户端在 initialize()
                # 里按当时的 ai.embedding_* 一次性建好，之后改配置不重建）。
                if await asyncio.to_thread(ensure_hosted_embed, self.config):
                    self.logger.info("托管嵌入已就绪（LAN 不可达时经网关，语义记忆不降级）")
                if await asyncio.to_thread(ensure_hosted_chatx, self.config):
                    self.logger.info("托管 ChatX 27B 已就绪（LAN 不可达时经网关，不改写成云厂商）")
                start_refresh_daemon(self.config)
            except Exception as _hg:
                self.logger.debug("托管网关跳过: %s", _hg)
            # 2026-09-09：成本账本必须在 AIClient 之前挂好——启动探针（Say hi）在 AIClient
            # 初始化里就打出去了，sink 后挂那一笔就永远进不了账（0909 首次装载实锤）。
            try:
                from src.ai.cost_ledger import attach_ledger_sink
                attach_ledger_sink()
            except Exception:
                self.logger.debug("成本账本预挂载跳过", exc_info=True)
            self.ai_client = AIClient(self.config)
            # defer_probe（P3-1 2026-08-12）：启动连接探针后台化——boot phases 实测
            # 它是 init 段最大单项（4.3s），而本处从不消费 initialize() 的返回值；
            # 后台任务保留原有日志/坏 key 告警。reload_ai_runtime 仍走阻塞探针。
            await self.ai_client.initialize(defer_probe=True)
            self.logger.info("AI客户端初始化成功")
            # 语音口语化 LLM 档（voice_colloquial_llm）复用主 AIClient 的 ai.fallback 本地端点
            # 做 rewrite_local——注入已初始化实例（否则其懒加载会建未初始化的空 client → 恒回落规则档）。
            try:
                from src.ai.voice_colloquial_llm import set_ai_client as _set_vcl_client
                _set_vcl_client(self.ai_client)
            except Exception as _e:
                self.logger.debug("voice_colloquial_llm 注入 AIClient 失败（回落规则档）: %s", _e)
            self._boot_mark("ai_client")

            # 4. 初始化Skill管理器
            self.skill_manager = SkillManager(self.config, self.ai_client)
            await self.skill_manager.initialize()
            self.logger.info("Skill管理器初始化成功")
            self._boot_mark("skill_manager")

            # N 线 核心4：注入统一运行时上下文，供编排器把协议号（扫码登录）拉起为 A 线丰富 client
            try:
                from src.integrations.telegram_companion_worker import set_companion_context
                set_companion_context(
                    config_manager=self.config,
                    skill_manager=self.skill_manager,
                    ai_client=self.ai_client,
                )
            except Exception as _ctx_ex:
                self.logger.debug("companion runtime 上下文注入失败: %s", _ctx_ex)

            # 5. Telegram 协议客户端(registry + N5 + desktop/client-init)
            from src.bootstrap.services import setup_telegram_clients
            await setup_telegram_clients(self)
            self._boot_mark("telegram_clients")
            self.logger.info("✅ AI聊天助手初始化完成")

            # C0-1 授权状态（只读提示，不阻断启动）
            try:
                from src.licensing import configure_license_manager

                # C0-3：按 config 配置强制开关（licensing.enforce，默认关）
                _lic_cfg = (self.config.config or {}).get("licensing", {}) or {}
                _lic = configure_license_manager(
                    enforce=bool(_lic_cfg.get("enforce", False)))
                # 2026-08-19 Token 定价改版：Token 账本总闸（默认关；开=消费点
                # 观测记账 + 会员页钱包卡。local_trial 同款模块级开关范式，改需重启）。
                try:
                    from src.licensing.token_ledger import configure_token_ledger
                    _tl_cfg = _lic_cfg.get("token_ledger") or {}
                    _tl_enabled = bool(_tl_cfg.get("enabled", False))
                    _tl_enforce = bool(_tl_cfg.get("enforce", False))
                    _fair_use = int(((_lic_cfg.get("fair_use") or {})
                                     .get("translate_chars_per_day")) or 0)
                    configure_token_ledger(
                        enabled=_tl_enabled, enforce=_tl_enforce,
                        fair_use_translate_chars=_fair_use or None)
                    if _tl_enabled:
                        self.logger.info(
                            "🪙 Token 账本：已启用（%s）",
                            "enforce=耗尽降级免费路径" if _tl_enforce else "观测记账")
                except Exception:
                    self.logger.debug("Token 账本装配跳过", exc_info=True)
                # 2026-09-08 成本对账 P0：LLM 真实用量落盘（cost_ledger.db）。
                # 与 Token 账本正交——那边是对客配额单位，这边是对厂商账单的真金白银。
                # 常开：没有它，重启即丢账、看板恒 0（0908 前的状态）。
                try:
                    from src.ai.cost_ledger import attach_ledger_sink, get_cost_ledger
                    if attach_ledger_sink():
                        _ai_cfg = (self.config.config or {}).get("ai", {}) or {}
                        self.logger.info(
                            "💴 成本账本：已落盘 %s（价格表 %s）",
                            getattr(get_cost_ledger(), "path", "?"),
                            "已配置" if _ai_cfg.get("pricing") else
                            "**未配置** ai.pricing → 金额恒 0，对账无意义")
                except Exception:
                    self.logger.debug("成本账本装配跳过", exc_info=True)
                if _lic.state == "active":
                    _exp = ("永久" if not _lic.expires_at
                            else f"剩 {_lic.days_left} 天")
                    self.logger.info(
                        "🔑 授权：%s · %s · 客户=%s · %s",
                        _lic.plan, _lic.state, _lic.customer or "—", _exp,
                    )
                elif _lic.state == "unlicensed":
                    self.logger.info("🔑 授权：社区模式（未检测到授权文件）")
                    # 无授权时启用首启体验档（本地赠量，默认关；桌面随包种子里开）。
                    # 只在 unlicensed 分支装配：有正式授权的部署压根不该出现体验档口径。
                    try:
                        from src.licensing.local_trial import (
                            configure_local_trial, get_local_trial,
                        )
                        configure_local_trial(
                            self.config.config or {},
                            config_dir=str(self.config.config_path.parent))
                        _lt = get_local_trial()
                        if _lt is not None:
                            # 首次启动即落锚点：「装完直接能用」不该依赖用户先走完向导。
                            # begin() 幂等（已关闭/已开始原样返回），并顺带无节流地
                            # 推进 last_seen 水位——每次启动校准一次即可，热路上的
                            # touch() 是节流的（见 local_trial.TOUCH_MIN_INTERVAL_SEC）。
                            _lt.begin()
                            _snap = _lt.snapshot()
                            # P-4 #254（MTRCH2④）：window_hours=0 = 不限时 → hours_left 是 None，
                            # 此前原样打成「0.0 小时窗口（剩 None 小时）」；窗口 >0 但还没落
                            # 锚点（hours_left None）则是「未开始计时」。三态分开说人话。
                            _wh = float(_snap.get("window_hours") or 0.0)
                            _hl = _snap.get("hours_left")
                            if _wh <= 0:
                                _win_txt = "不限时"
                            elif _hl is None:
                                _win_txt = f"{_wh:g} 小时窗口（未开始计时）"
                            else:
                                _win_txt = f"{_wh:g} 小时窗口（剩 {_hl} 小时）"
                            self.logger.info(
                                "🎁 首启体验档：%s · %s 字符 / %s",
                                "已结束" if not _snap.get("active") else "生效中",
                                _snap.get("included"), _win_txt,
                            )
                    except Exception:
                        self.logger.debug("首启体验档装配跳过", exc_info=True)
                else:
                    self.logger.warning(
                        "🔑 授权状态=%s：%s",
                        _lic.state, "；".join(_lic.messages) or "—",
                    )
            except Exception:
                self.logger.debug("授权状态读取跳过", exc_info=True)

            self._startup_advisory_events = []
            try:
                from src.utils.config_advisories import (
                    collect_production_advisories,
                    log_advisory_events,
                )

                self._startup_advisory_events = collect_production_advisories(
                    self.config.config or {}
                )
                log_advisory_events(self.logger, self._startup_advisory_events)
            except Exception:
                self.logger.debug("config_advisories 跳过", exc_info=True)

            try:
                ev = getattr(self, "_startup_advisory_events", []) or []
                wn = sum(
                    1
                    for e in ev
                    if str(getattr(e, "level", "")).lower() == "warning"
                )
                from src.monitoring.metrics_store import get_metrics_store

                get_metrics_store().set_startup_advisory_counts(len(ev), wn)
            except Exception:
                self.logger.debug("startup_advisory metrics 跳过", exc_info=True)

            self._boot_mark("advisories")

            # RPA 服务: LINE / Messenger / WhatsApp
            from src.bootstrap.services import setup_rpa_services
            setup_rpa_services(self)
            self._boot_mark("rpa_services")

            # 设备管理: 协调器 / 注册表 / 热插拔(HotPlug)
            from src.bootstrap.services import setup_device_management
            setup_device_management(self)
            self._boot_mark("device_mgmt")

            # ── Contacts 跨平台子系统（feature flag 控制）──
            from src.bootstrap.services import setup_contacts_subsystem
            setup_contacts_subsystem(self)
            self._boot_mark("contacts")

            # ── Mobile Bridge（依赖 Contacts 子系统，仅 contacts 启用时构建）──
            # 精简档（contacts.mode=lite，客户装机默认）不建：它每 15s 打本机
            # 18080 的手机自动化 rig，客户机上没有那套服务，只会刷连接失败日志。
            if self.contacts is not None and self.contacts.heavy_integrations_enabled():
                try:
                    from src.contacts.mobile_bridge import MobileBridgeService
                    _bridge_cfg = (self.config.config or {}).get("mobile_bridge", {})
                    _mr_cfg = (self.config.config or {}).get("messenger_rpa", {})
                    _ma_cfg = _mr_cfg.get("mobile_auto", {}) if isinstance(_mr_cfg, dict) else {}
                    _openclaw_path = _resolve_mobile_auto_openclaw_db(
                        self.config.config or {},
                        self.config.config_path,
                    )
                    _mobile_api = (
                        _bridge_cfg.get("mobile_api_base")
                        or (_ma_cfg.get("api_base") if isinstance(_ma_cfg, dict) else "")
                        or "http://127.0.0.1:18080"
                    )
                    _poll_interval = float(_bridge_cfg.get("poll_interval_sec", 15))
                    self.mobile_bridge = MobileBridgeService(
                        contacts_store=self.contacts.store,
                        openclaw_db_path=_openclaw_path,
                        mobile_api_base=_mobile_api,
                        poll_interval_sec=_poll_interval,
                    )
                    self.logger.info(
                        "Mobile Bridge 已构建 (openclaw=%s mobile_api=%s poll=%.0fs)",
                        _openclaw_path, _mobile_api, _poll_interval,
                    )
                except Exception as ex:
                    self.logger.warning("Mobile Bridge 构建跳过: %s", ex)

            self._boot_mark("mobile_bridge")

            # Web 管理后台
            web_cfg = self.config.config.get("web_admin", {})
            from src.bootstrap.web_app import setup_web_app
            setup_web_app(self, web_cfg)
            # ★ /login 可服务的里程碑：web 线程在 setup_web_app 末尾已 bind+serve。
            #    此处累计 = 「进程起来 → seat 可登录」的真实冷启动窗口。
            self._boot_mark("web_app")

            # 监控 API 后台线程（Stage 2：抽到 bootstrap/web_app.py::start_monitoring_thread）
            from src.bootstrap.web_app import start_monitoring_thread
            start_monitoring_thread(self)
            self._boot_mark("monitoring")

            # Phase 11：落一行分阶段计时到 app.log + 入 metrics_store（下次重启即读真实归因）。
            try:
                if self._boot_timer is not None:
                    _summary = self._boot_timer.summary()
                    self.logger.info(self._boot_timer.format_line())
                    try:
                        from src.monitoring.metrics_store import get_metrics_store
                        get_metrics_store().set_boot_timing(_summary)
                    except Exception:
                        self.logger.debug("boot_timing metrics 跳过", exc_info=True)
                    if self._web_app is not None:
                        try:
                            self._web_app.state.boot_timing = _summary
                        except Exception:
                            pass
            except Exception:
                self.logger.debug("boot_timing 汇总跳过", exc_info=True)
            return True

        except Exception as e:
            self.logger.error(f"初始化失败: {e}")
            return False

    async def start(self):
        """启动 AI 聊天助手(Stage4:实现已迁至 bootstrap/lifecycle)。"""
        from src.bootstrap.lifecycle import start_assistant
        return await start_assistant(self)

    def _maybe_start_inbox_ingest_loop(self) -> None:
        """D5a：启动收件箱后台 ingest 轮询循环。

        周期性把各平台 runner 的最近会话聚合 ingest 进 inbox.db；对**新入站消息**
        发 inbox_message 事件（坐席工作台 SSE 实时刷新）。冷启动首轮 warmup 不发事件。
        条件：inbox 已挂载 + web_app 就绪。
        """
        if self.inbox_store is None or self._web_app is None:
            return
        try:
            _inbox_cfg = (self.config.config or {}).get("inbox", {}) or {}
            interval = float(_inbox_cfg.get("realtime_poll_sec", 10))
        except Exception:
            _inbox_cfg = {}
            interval = 10.0
        if interval <= 0:
            self.logger.info("收件箱实时 ingest 轮询已禁用（realtime_poll_sec<=0）")
            return
        # 二期：忙/闲自适应——有新入站的窗口内用快档（默认 2.5s，压坐席看到 RPA 消息的
        # 延迟），持续安静则退回慢档（realtime_poll_sec，默认 10s，控制空转成本）。
        # 协议号（TG/Baileys）走事件直写不吃这口轮询；此处主要惠及 RPA/兜底路径。
        try:
            busy = float(_inbox_cfg.get("realtime_busy_poll_sec", 2.5))
        except Exception:
            busy = 2.5
        busy = max(0.5, min(busy, interval))
        self._inbox_ingest_task = asyncio.create_task(
            self._inbox_ingest_loop(interval, busy), name="inbox_ingest_loop",
        )
        self.logger.info(
            "✅ 收件箱实时 ingest 轮询已启动（idle=%ss busy=%ss 自适应）", interval, busy,
        )

    async def _inbox_ingest_loop(self, interval: float, busy_interval: float | None = None) -> None:
        from types import SimpleNamespace
        from src.inbox.channel_adapters import (
            default_inbox_adapters, collect_chats_via_adapters,
        )
        from src.inbox.ingest import ingest_collected_chats

        adapters = default_inbox_adapters()
        shim = SimpleNamespace(app=self._web_app)
        warmup = True  # 首轮只 ingest 不发事件，避免冷启动事件洪泛
        busy = float(busy_interval) if busy_interval else interval
        busy_until = 0.0   # 最近有新入站 → 60s 内保持快档（对话通常成串到来）
        while self.running:
            inserted = 0
            try:
                chats = await asyncio.to_thread(
                    collect_chats_via_adapters, shim, 50, adapters,
                )
                # ingest（含发事件）放主循环线程执行：SSE 用的 asyncio.Queue 非线程安全
                inserted = ingest_collected_chats(
                    self.inbox_store, chats, publish_events=not warmup,
                )
                warmup = False
            except Exception:
                self.logger.debug("收件箱 ingest 轮询异常", exc_info=True)
            now = time.time()
            if inserted:
                busy_until = now + 60.0
            await asyncio.sleep(busy if now < busy_until else interval)

    def _build_contact_resolver(self):
        """Q 延伸：构造 (platform, account_id, chat_key) → contact_id 解析器。

        inbox/contacts 未就绪时返回 None。供 ingest 回写与存量回填共用。
        """
        if self.inbox_store is None or self.contacts is None:
            return None
        from src.contacts.identity_bridge import resolve_contact_id

        cstore = self.contacts.store

        def _resolver(platform: str, account_id: str, chat_key: str) -> str:
            return resolve_contact_id(
                cstore, platform=platform, account_id=account_id, chat_key=chat_key)

        return _resolver

    def _maybe_wire_ingest_contact_writeback(self) -> None:
        """Q 延伸：ingest 热路径回写 contact_id（默认关，companion.relations_health）。"""
        try:
            rh = ((self.config.config.get("companion") or {})
                  .get("relations_health") or {})
            if not rh.get("ingest_contact_id_writeback", False):
                self.logger.info(
                    "ingest contact_id 回写未启用"
                    "（companion.relations_health.ingest_contact_id_writeback=false）")
                return
            resolver = self._build_contact_resolver()
            if resolver is None:
                self.logger.info("ingest contact_id 回写跳过（inbox/contacts 未就绪）")
                return
            self.inbox_store.register_contact_resolver(resolver)
            self.logger.info("✅ ingest contact_id 回写已接线（Q 延伸）")
        except Exception:
            self.logger.warning("ingest contact_id 回写接线跳过", exc_info=True)

    async def _maybe_run_contact_id_backfill(self) -> None:
        """Q 延伸·存量回填：给历史会话补 contact_id（默认关，可 dry_run）。

        config: companion.relations_health.contact_id_backfill.{enabled, limit, dry_run,
        delay_seconds}。一次性启动任务，DB 扫描放线程池避免阻塞事件循环。
        """
        try:
            rh = ((self.config.config.get("companion") or {})
                  .get("relations_health") or {})
            bf = (rh.get("contact_id_backfill") or {})
            if not bf.get("enabled", False):
                return
            resolver = self._build_contact_resolver()
            if resolver is None:
                self.logger.info("contact_id 存量回填跳过（inbox/contacts 未就绪）")
                return
            delay = float(bf.get("delay_seconds", 20))
            await asyncio.sleep(max(0.0, delay))
            if not self.running:
                return
            from src.contacts.contact_backfill import backfill_contact_ids

            limit = max(1, min(int(bf.get("limit", 200)), 2000))
            dry_run = bool(bf.get("dry_run", False))
            result = await asyncio.to_thread(
                backfill_contact_ids, self.inbox_store, resolver,
                limit=limit, dry_run=dry_run,
            )
            import time as _time
            out = {**result.as_dict(), "trigger": "startup", "ts": _time.time()}
            if self._web_app is not None:
                self._web_app.state.last_contact_backfill = out
            self.logger.info("✅ contact_id 存量回填完成: %s", out)
        except Exception:
            self.logger.warning("contact_id 存量回填失败", exc_info=True)

    def _ensure_deferred_outbox(self, *args, **kwargs):
        from src.bootstrap.background_tasks import ensure_deferred_outbox
        return ensure_deferred_outbox(self, *args, **kwargs)

    async def _maybe_translate_outbound(self, platform, account_id, chat_key, text):
        """deferred 主动触达投递前的出站翻译/语言硬闸（best-effort）。

        复用 L2 autosend 同一 ``translate_outbound_text``（含「已是客户语言则跳过」检测护栏）
        与同一开关 ``inbox.l2_autosend.translate.enabled``。translation_service 懒取。

        P3-198（2026-08-04）：``translate.enabled=false`` 时按 ``lang_gate``（默认开）
        走 gate-only——常规消息原样放行（尊重运营关闭翻译的决定），CJK↔非 CJK 实质
        冲突抢救翻译；翻译不可用返回 **None**（HOLD 信号，调用方转
        DeferredSenderNotReady 推后重试——发中文给外语客户比这条消息晚到更糟）。
        其余任何缺失/异常 → 回落发原文，绝不阻塞投递。
        """
        try:
            from src.inbox.outbound_translate import (
                parse_outbound_lang_gate_cfg,
                parse_outbound_translate_cfg,
                translate_outbound_text,
            )
            cfg = parse_outbound_translate_cfg(self.config.config or {})
            if not cfg.get("enabled") and not parse_outbound_lang_gate_cfg(
                    self.config.config or {}).get("enabled"):
                return text
            ts = getattr(self._web_app.state, "translation_service", None) \
                if self._web_app is not None else None
            if ts is None or self.inbox_store is None:
                return text
            from src.inbox.draft_models import _conv_id
            item = {"conversation_id": _conv_id(str(platform), str(account_id), str(chat_key)),
                    "text": str(text)}
            _out = await translate_outbound_text(
                item, translation_service=ts, store=self.inbox_store,
                source_lang=cfg.get("source_lang") or "zh", style=cfg.get("style") or "chat",
                gate_only=not cfg.get("enabled"))
            # #64/B125 补口（0830）：deferred 触达是三条翻译出口里唯一没挂
            # 混语守卫的（autosend 自动链/人工通过链都经 build_autosend_translate_cb
            # 的 _guard_translated_lang_mix）——MT 漏译残字（「I'm 我」）从这里
            # 照样直发客户。同一守卫补齐；HOLD(None) 语义原样透传。
            from src.inbox.autosend_helpers import _guard_translated_lang_mix
            return _guard_translated_lang_mix(self, str(text), _out)
        except Exception:
            self.logger.debug("[deferred_outbox] 出站翻译跳过", exc_info=True)
            return text

    def _enqueue_deferred_outbox(self, channel, account_id, chat_name, reply,
                                 defer_until, reason, staleness_sec, extra) -> int:
        """把非 messenger 主动消息入多平台 deferred 队列。返回 row_id（0=未入队）。

        作为 care/reactivation send_callback 的非 messenger 分支：队列关或不可用 → 返回 0
        （上层据此 mark_skipped/failed，与原「return 0」语义一致，零破坏）。
        N-1 D（#243）：「关」＝``companion.multiplatform_deferred.enabled`` 实时值为 False
        （入队热闸）；队列本体与 drain loop 随后端启动常备，不再因启动期关闸而缺席。
        """
        from src.bootstrap.background_tasks import deferred_outbox_accepting
        if not deferred_outbox_accepting(self):
            return 0
        dispatcher = self._ensure_deferred_outbox()
        if dispatcher is None:
            return 0
        try:
            return dispatcher._store.enqueue(
                platform=str(channel), account_id=str(account_id or "default"),
                chat_key=str(chat_name), reply_text=str(reply),
                defer_until=float(defer_until), reason=str(reason or ""),
                staleness_sec=float(staleness_sec), extra=extra or {})
        except Exception:
            self.logger.debug("deferred_outbox enqueue 失败 %s", channel, exc_info=True)
            return 0

    async def _maybe_start_deferred_outbox(self) -> None:
        """启动多平台 deferred 队列 drain loop——**常备**（N-1 D #243）。

        旧行为：``multiplatform_deferred.enabled=false`` 时整段 return，运行时开闸后
        再没人 start（skuio 09-07 16:34:52 入队、16:47:52 预计出队零动作的根因）。
        现在 store + dispatcher + drain loop 无条件起，``enabled`` 只做入队热闸
        （见 ``_enqueue_deferred_outbox``）——关闸时队列空转零副作用。
        """
        try:
            from src.bootstrap.background_tasks import deferred_outbox_accepting
            dispatcher = self._ensure_deferred_outbox()
            if dispatcher is None:
                self.logger.warning("多平台 deferred 队列初始化失败，drain loop 未启动")
                return
            await dispatcher.start()
            self.logger.info(
                "✅ 多平台 deferred 队列 drain loop 已启动（常备；accepting=%s ← "
                "companion.multiplatform_deferred.enabled 实时读，关闸只是不收新消息）",
                deferred_outbox_accepting(self))
        except Exception:
            self.logger.warning("多平台 deferred 队列启动跳过", exc_info=True)

    def _ensure_quality_trend(self):
        """惰性建质量趋势快照器（周期落地 companion_quality_overview）。

        返回 snapshotter（未 start），或 None（功能关/不可用）。幂等。
        store 挂到 app.state.quality_trend_store 供 /api/companion/quality-trend 读。
        """
        if self._quality_trend_snapshotter is not None:
            return self._quality_trend_snapshotter
        try:
            comp = (self.config.config.get("companion") or {})
            cfg = (comp.get("quality_trend") or {})
            if not cfg.get("enabled", False):
                return None
            from src.monitoring.metrics_store import get_metrics_store
            from src.monitoring.quality_trend_store import (
                QualityTrendSnapshotter, QualityTrendStore,
            )

            _cfg_dir = Path(self.config.config_path).parent
            store = QualityTrendStore(_cfg_dir / "quality_trend.db")
            win_h = float(cfg.get("window_hours", 24))

            def _overview():
                return get_metrics_store().companion_quality_overview(
                    window_sec=max(1.0, win_h) * 3600.0)

            snap = QualityTrendSnapshotter(
                store=store,
                overview_fn=_overview,
                interval_sec=float(cfg.get("interval_sec", 300)),
                retention_days=float(cfg.get("retention_days", 30)),
            )
            self._quality_trend_snapshotter = snap
            if self._web_app is not None:
                self._web_app.state.quality_trend_store = store
            self.logger.info(
                "✅ 质量趋势持久化已就绪（interval=%ss retention=%sd）",
                cfg.get("interval_sec", 300), cfg.get("retention_days", 30))
            return snap
        except Exception:
            self.logger.warning("质量趋势持久化初始化失败", exc_info=True)
            return None

    async def _maybe_start_quality_trend(self) -> None:
        """启动质量趋势快照循环（默认关）。"""
        try:
            snap = self._ensure_quality_trend()
            if snap is None:
                self.logger.info(
                    "质量趋势持久化未启用（companion.quality_trend.enabled=false）")
                return
            await snap.start()
            self.logger.info("✅ 质量趋势快照循环已启动")
        except Exception:
            self.logger.warning("质量趋势持久化启动跳过", exc_info=True)

    def _maybe_seed_assistant_help(self) -> None:
        """小智帮助语料首启自动播种（2026-08-23，1.0.51 全功能开箱）。

        assistant.enabled 时后台幂等 upsert（seed_corpus 随包生成，与代码版本
        同步）：新装机首启即有语料，升级自动补新词条；关着=零动作。
        刻意挂 lifecycle 启动段而非 web 装配（测试自建 app 不应触发，首版踩过）。
        """
        try:
            acfg = self.config.config.get("assistant") or {}
            if not (isinstance(acfg, dict) and acfg.get("enabled")):
                return
            from src.assistant.seed_corpus import seed_help_corpus_bg
            seed_help_corpus_bg()
        except Exception:
            self.logger.debug("帮助语料播种调度失败（忽略）", exc_info=True)

    def _maybe_init_tts_cost_log(self) -> None:
        """P4-B：按 ``voice_routing.cost_log.enabled`` 装配 TTS 成本日聚合落库（默认关）。

        开启后 ``tts_pipeline._record_stats`` 旁路把每次合成写入 ``tts_cost.db``，
        ops 看板经 ``/api/admin/tts-cost-trend`` 读近 N 天花费/缓存命中曲线。
        关闭时 ``record_tts_cost`` 恒 no-op（无 voice 用量部署零 IO）。
        """
        try:
            vr = (self.config.config.get("voice_routing") or {})
            cl = (vr.get("cost_log") or {})
            if not cl.get("enabled", False):
                self.logger.info("TTS 成本落库未启用（voice_routing.cost_log.enabled=false）")
                return
            from src.ai.tts_cost_store import configure_tts_cost_store
            _cfg_dir = Path(self.config.config_path).parent
            store = configure_tts_cost_store(
                enabled=True,
                db_path=_cfg_dir / "tts_cost.db",
                retention_days=float(cl.get("retention_days", 90)),
            )
            if store is not None:
                self.logger.info(
                    "✅ TTS 成本落库已就绪（retention=%sd）", cl.get("retention_days", 90))
        except Exception:
            self.logger.warning("TTS 成本落库初始化失败（已忽略）", exc_info=True)

    def _maybe_init_translation_trend_log(self) -> None:
        """S：按 ``translation.engines.confidence_switch.trend_log`` 装配翻译置信度日聚合落库（默认关）。

        开启后 ``EngineRouter.translate`` 旁路把每次翻译的 {尝试/低置信/切换} 写入
        ``xlate_trend.db``，ops 看板经 ``/api/admin/translation-confidence-trend``
        读近 N 天低置信率/切换率 sparkline。关闭时 ``record_translation_trend`` 恒 no-op。
        """
        try:
            _tr = (self.config.config.get("translation") or {})
            _cs = ((_tr.get("engines") or {}).get("confidence_switch") or {})
            if not _cs.get("trend_log", False):
                self.logger.info(
                    "翻译置信度趋势落库未启用（translation.engines.confidence_switch.trend_log=false）")
                return
            from src.ai.translation_trend_store import configure_translation_trend_store
            _cfg_dir = Path(self.config.config_path).parent
            store = configure_translation_trend_store(
                enabled=True,
                db_path=_cfg_dir / "xlate_trend.db",
                retention_days=float(_cs.get("trend_retention_days", 90)),
            )
            if store is not None:
                self.logger.info(
                    "✅ 翻译置信度趋势落库已就绪（retention=%sd）",
                    _cs.get("trend_retention_days", 90))
        except Exception:
            self.logger.warning("翻译置信度趋势落库初始化失败（已忽略）", exc_info=True)

    def _init_persona_media_store(self) -> None:
        """装配每人设「相册/媒体」注册表（DB 落 config/persona_media.db；始终开启）。

        媒体元数据（触发词/配文/权重/关系闸门/命中）落库，文件落 static/persona_albums；
        相册后台（``/api/personas/{pid}/media*``）与回复链（image_autosend / skill_manager
        Stage 0）读同一份 store。DB 路径随 config 目录，避免与 :memory: 单测串味。
        """
        try:
            from src.companion.persona_media_store import configure_persona_media_store
            _cfg_dir = Path(self.config.config_path).parent
            store = configure_persona_media_store(_cfg_dir / "persona_media.db")
            if store is not None:
                self.logger.info("✅ 每人设相册/媒体注册表就绪（persona_media.db）")
        except Exception:
            self.logger.warning("每人设相册/媒体注册表初始化失败（已忽略）", exc_info=True)

    def _init_group_members_store(self) -> None:
        """装配 Telegram 群成员提取库（DB 落 config/group_members.db；始终建库，行为受 flag 门控）。

        成员库（去重成员 + 提取任务/进度）落库；后台 API（``/api/tg-members/*``）与提取编排读写
        同一份 store。DB 路径随 config 目录 → 双实例天然隔离，且避免与 :memory: 单测串味。
        提取行为本身受 ``companion.group_members.enabled`` 总闸（默认关）——建库 ≠ 开功能。
        """
        try:
            from src.companion.group_members_store import configure_group_members_store
            _cfg_dir = Path(self.config.config_path).parent
            store = configure_group_members_store(_cfg_dir / "group_members.db")
            if store is not None:
                self.logger.info("✅ Telegram 群成员提取库就绪（group_members.db）")
        except Exception:
            self.logger.warning("Telegram 群成员提取库初始化失败（已忽略）", exc_info=True)

    def _init_fatex_store(self) -> None:
        """装配 FateX（问衍）产品独立数据库（fatex.db，与主库物理分离）。

        命理产品的权威生辰画像按 (platform, account_id, chat_key) 结构化落本库
        （episodic 仅保留「说过生辰」的对话记忆），产品数据与主产品互不落对方库；
        路径随 config 目录 → 双实例天然隔离。故障软忽略，绝不阻断主产品启动。

        P-4 #254（MTRCH2①，2026-09-08）：只在 FateX 产品开着（``fatex.enabled`` /
        兼容 ``companion.bazi.enabled``）时才建库。此前无条件 ``configure_fatex_store``
        让每台用户版陪伴机 clean 装就多出一个 ``config/fatex.db``（命理产品库），
        报告误判为「随包打进来」——实为启动期空建；命理产品库只随 FateX 产品线走。
        """
        try:
            from src.fatex.config import fatex_enabled
            if not fatex_enabled(self.config):
                from src.fatex.store import disable_fatex_store
                disable_fatex_store()     # 连懒建也封死：生辰双写 / ops 体检那几处不再凭空建库
                self.logger.info("FateX 产品未启用（fatex.enabled=false）：不建 fatex.db（用户版不带命理产品库）")
                return
            from src.fatex import product_badge
            from src.fatex.store import configure_fatex_store
            _cfg_dir = Path(self.config.config_path).parent
            store = configure_fatex_store(_cfg_dir / "fatex.db")
            if store is not None:
                self.logger.info("✅ %s 产品库就绪（fatex.db）", product_badge())
        except Exception:
            self.logger.warning("FateX 产品库初始化失败（已忽略）", exc_info=True)

    def _maybe_init_frontend_error_trend_log(self) -> None:
        """P9：按 ``ops.frontend_error_trend`` 装配前端错误/意图落空日聚合落库（默认关）。

        开启后 beacon 路由（/api/telemetry/frontend-error）旁路把消毒后的错误类型
        （ReferenceError/timeout/scoped_fail/dead_intent/conv_not_found…）按日 upsert
        进 ``fe_trend.db``，ops 看板经 ``/api/admin/frontend-error-trend`` 读近 N 天
        ——「意图落空在收敛还是回潮」从进程内瞬时快照变成可回看的时间线。
        """
        try:
            _ops = (self.config.config.get("ops") or {})
            _fet = (_ops.get("frontend_error_trend") or {})
            if not _fet.get("enabled", False):
                self.logger.info(
                    "前端错误趋势落库未启用（ops.frontend_error_trend.enabled=false）")
                return
            from src.web.frontend_error_trend import configure_frontend_error_trend
            _cfg_dir = Path(self.config.config_path).parent
            store = configure_frontend_error_trend(
                enabled=True,
                db_path=_cfg_dir / "fe_trend.db",
                retention_days=float(_fet.get("retention_days", 90)),
            )
            if store is not None:
                self.logger.info(
                    "✅ 前端错误趋势落库已就绪（retention=%sd）",
                    _fet.get("retention_days", 90))
        except Exception:
            self.logger.warning("前端错误趋势落库初始化失败（已忽略）", exc_info=True)

    def _maybe_init_login_funnel_trend_log(self) -> None:
        """按 ``ops.login_funnel_trend`` 装配账号接入漏斗日聚合落库（默认关）。

        开启后 ``record_login_stage`` 旁路把 started/authorized/failed + 失败原因码
        按日 upsert 进 ``login_funnel_trend.db``，ops 看板经
        ``/api/admin/login-funnel-trend`` 读近 N 天——「成功率/checkpoint 占比是在收敛
        还是回潮」从进程内瞬时快照变成跨重启可回看的时间线。
        """
        try:
            _ops = (self.config.config.get("ops") or {})
            _lft = (_ops.get("login_funnel_trend") or {})
            if not _lft.get("enabled", False):
                self.logger.info(
                    "账号接入漏斗趋势落库未启用（ops.login_funnel_trend.enabled=false）")
                return
            from src.integrations.login_funnel_trend import configure_login_funnel_trend
            _cfg_dir = Path(self.config.config_path).parent
            store = configure_login_funnel_trend(
                enabled=True,
                db_path=_cfg_dir / "login_funnel_trend.db",
                retention_days=float(_lft.get("retention_days", 90)),
            )
            if store is not None:
                self.logger.info(
                    "✅ 账号接入漏斗趋势落库已就绪（retention=%sd）",
                    _lft.get("retention_days", 90))
        except Exception:
            self.logger.warning("账号接入漏斗趋势落库初始化失败（已忽略）", exc_info=True)

    def _maybe_init_ui_event_trend_log(self) -> None:
        """按 ``ops.ui_event_trend`` 装配 UI 事件日聚合落库（默认关）。

        开启后 beacon 路由（/api/telemetry/ui-event）旁路把消毒后的动作按日 upsert 进
        ``ui_event_trend.db``，看板/周读经 ``/api/admin/ui-event-trend`` 读近 N 天
        （``?prefix=dpick.`` 取 AI 回复漏斗命名空间）——取消率/采纳率从进程内瞬时
        快照（重启即清零）变成可回看的时间线。
        """
        try:
            _ops = (self.config.config.get("ops") or {})
            _uet = (_ops.get("ui_event_trend") or {})
            if not _uet.get("enabled", False):
                self.logger.info(
                    "UI 事件趋势落库未启用（ops.ui_event_trend.enabled=false）")
                return
            from src.web.ui_event_trend import configure_ui_event_trend
            _cfg_dir = Path(self.config.config_path).parent
            store = configure_ui_event_trend(
                enabled=True,
                db_path=_cfg_dir / "ui_event_trend.db",
                retention_days=float(_uet.get("retention_days", 90)),
            )
            if store is not None:
                self.logger.info(
                    "✅ UI 事件趋势落库已就绪（retention=%sd）",
                    _uet.get("retention_days", 90))
        except Exception:
            self.logger.warning("UI 事件趋势落库初始化失败（已忽略）", exc_info=True)

    def _maybe_init_contacts_asset_trend_log(self) -> None:
        """按 ``ops.contacts_asset_trend`` 装配客户资产（好友/未开口/沉默）日快照落库（默认关）。

        写侧＝ops 看板 trend 端点的**读时懒快照**（当天一次），无常驻 job；
        读经 ``/api/admin/contacts-asset-trend``。回答「破冰/主动触达上线后，
        未开口存量有没有真的压下去」——点值卡片给不出的趋势判据。
        """
        try:
            _ops = (self.config.config.get("ops") or {})
            _cat = (_ops.get("contacts_asset_trend") or {})
            if not _cat.get("enabled", False):
                self.logger.info(
                    "客户资产趋势落库未启用（ops.contacts_asset_trend.enabled=false）")
                return
            from src.web.contacts_asset_trend import configure_contacts_asset_trend
            _cfg_dir = Path(self.config.config_path).parent
            store = configure_contacts_asset_trend(
                enabled=True,
                db_path=_cfg_dir / "contacts_asset_trend.db",
                retention_days=float(_cat.get("retention_days", 180)),
            )
            if store is not None:
                self.logger.info(
                    "✅ 客户资产趋势落库已就绪（retention=%sd）",
                    _cat.get("retention_days", 180))
        except Exception:
            self.logger.warning("客户资产趋势落库初始化失败（已忽略）", exc_info=True)

    def _maybe_init_csrf_trend_log(self) -> None:
        """P2（2026-07-31）：按 ``ops.csrf_trend`` 装配 CSRF 准入/拒绝日聚合落库（默认关）。

        进程计数器重启即清零，而「同源 Origin/Referer 回落能不能收口」要看**跨两周**
        的通行侧证据（admit:origin/referer 是否持续归零）。开启后中间件旁路把
        拒绝（全量）与 origin/referer 放行按日 upsert 进 ``csrf_trend.db``，
        经 ``/api/admin/csrf-trend`` 读近 N 天。csrf_pair/bearer 放行刻意不落库
        （绝对主流，每请求一次 SQLite 写不值得；进程内 admitted_by 可见当下分布）。
        """
        try:
            _ops = (self.config.config.get("ops") or {})
            _ct = (_ops.get("csrf_trend") or {})
            if not _ct.get("enabled", False):
                self.logger.info("CSRF 趋势落库未启用（ops.csrf_trend.enabled=false）")
                return
            from src.web.csrf_trend import configure_csrf_trend
            _cfg_dir = Path(self.config.config_path).parent
            store = configure_csrf_trend(
                enabled=True,
                db_path=_cfg_dir / "csrf_trend.db",
                retention_days=float(_ct.get("retention_days", 90)),
            )
            if store is not None:
                self.logger.info(
                    "✅ CSRF 准入/拒绝趋势落库已就绪（retention=%sd）",
                    _ct.get("retention_days", 90))
        except Exception:
            self.logger.warning("CSRF 趋势落库初始化失败（已忽略）", exc_info=True)

    def _maybe_init_identity_trend_log(self) -> None:
        """F1：按 ``inbox.identity.trend_log`` 装配会话身份健康日聚合落库（默认关）。

        开启后 ``_record_ingest_identity`` / ``_record_avatar`` 旁路把入站 named/backfilled/raw
        与头像 hit/empty/total 写入 ``identity_trend.db``，ops 看板经
        ``/api/admin/identity-health-trend`` 读近 N 天 raw%/empty% sparkline。关闭时
        ``record_identity_trend`` 恒 no-op。
        """
        try:
            _inbox = (self.config.config.get("inbox") or {})
            _ident = (_inbox.get("identity") or {})
            if not _ident.get("trend_log", False):
                self.logger.info("会话身份趋势落库未启用（inbox.identity.trend_log=false）")
                return
            from src.web.identity_trend_store import configure_identity_trend_store
            _cfg_dir = Path(self.config.config_path).parent
            store = configure_identity_trend_store(
                enabled=True,
                db_path=_cfg_dir / "identity_trend.db",
                retention_days=float(_ident.get("trend_retention_days", 90)),
            )
            if store is not None:
                self.logger.info(
                    "✅ 会话身份趋势落库已就绪（retention=%sd）",
                    _ident.get("trend_retention_days", 90))
        except Exception:
            self.logger.warning("会话身份趋势落库初始化失败（已忽略）", exc_info=True)

    def _maybe_init_realtime_voice_trend_log(self) -> None:
        """E 线：按 ``realtime_voice.trend_log`` 装配实时语音按日聚合落库（默认关）。

        开启后 stats 热路旁路 upsert ``config/rtv_trend.db``，ops 经
        ``/api/admin/realtime-voice-trend`` 画 sparkline，告警校准可读近 N 天回放。
        """
        try:
            rtv = (self.config.config.get("realtime_voice") or {})
            if not rtv.get("trend_log", False):
                self.logger.info(
                    "实时语音趋势落库未启用（realtime_voice.trend_log=false）")
                return
            from src.ai.realtime_voice_trend_store import configure_realtime_voice_trend_store
            _cfg_dir = Path(self.config.config_path).parent
            store = configure_realtime_voice_trend_store(
                enabled=True,
                db_path=_cfg_dir / "rtv_trend.db",
                retention_days=float(rtv.get("trend_retention_days", 90)),
            )
            if store is not None:
                self.logger.info(
                    "✅ 实时语音趋势落库已就绪（retention=%sd）",
                    rtv.get("trend_retention_days", 90))
        except Exception:
            self.logger.warning("实时语音趋势落库初始化失败（已忽略）", exc_info=True)

    def _maybe_init_send_route_trend_log(self) -> None:
        """P8：按 ``inbox.send_route.trend_log`` 装配出站路由回落率按日聚合落库（默认关）。

        开启后 watchdog tick 旁路把 ``SendRouteStats`` 的累计增量 upsert
        ``config/send_route_trend.db``，ops 看板经 ``/api/admin/send-route-trend`` 画近 N 天
        回落率 sparkline。关闭时 ``sync_send_route_trend_from_stats`` 恒 no-op。
        """
        try:
            _sr = ((self.config.config.get("inbox") or {}).get("send_route") or {})
            if not _sr.get("trend_log", False):
                self.logger.info(
                    "出站路由趋势落库未启用（inbox.send_route.trend_log=false）")
                return
            from src.inbox.send_route_trend_store import configure_send_route_trend_store
            _cfg_dir = Path(self.config.config_path).parent
            store = configure_send_route_trend_store(
                enabled=True,
                db_path=_cfg_dir / "send_route_trend.db",
                retention_days=float(_sr.get("trend_retention_days", 90)),
            )
            if store is not None:
                self.logger.info(
                    "✅ 出站路由趋势落库已就绪（retention=%sd）",
                    _sr.get("trend_retention_days", 90))
        except Exception:
            self.logger.warning("出站路由趋势落库初始化失败（已忽略）", exc_info=True)

    def _maybe_init_media_promise_trend_log(self) -> None:
        """Phase22c：按 ``companion.media_promise_guard.trend_log`` 装配出站媒体承诺兑现率
        日聚合落库（默认关）。

        开启后 ``image_autosend.record_promise_event`` 单一 choke point 旁路把
        detected/fulfilled/retracted/offer_accept 写入 ``media_promise_trend.db``，ops 看板经
        ``/api/admin/media-promise-trend`` 读近 N 天兑现率 sparkline，并为
        ``health_watchdog.media_promise_remind`` 的阈值校准提供真实分布。关闭时
        ``record_media_promise_trend`` 恒 no-op。
        """
        try:
            _pg = ((self.config.config.get("companion") or {})
                   .get("media_promise_guard") or {})
            if not _pg.get("trend_log", False):
                self.logger.info(
                    "媒体承诺趋势落库未启用（companion.media_promise_guard.trend_log=false）")
                return
            from src.inbox.media_promise_trend_store import (
                configure_media_promise_trend_store,
            )
            _cfg_dir = Path(self.config.config_path).parent
            store = configure_media_promise_trend_store(
                enabled=True,
                db_path=_cfg_dir / "media_promise_trend.db",
                retention_days=float(_pg.get("trend_retention_days", 90)),
            )
            if store is not None:
                self.logger.info(
                    "✅ 媒体承诺趋势落库已就绪（retention=%sd）",
                    _pg.get("trend_retention_days", 90))
        except Exception:
            self.logger.warning("媒体承诺趋势落库初始化失败（已忽略）", exc_info=True)

    def _maybe_init_inject_extract_trend_log(self) -> None:
        """按 ``inbox.desktop_inject.trend_log`` 装配注入抽取率日聚合落库（默认关）。

        开启后 ``POST /api/desktop/inject-health`` 旁路把每条健康上报的抽取计数
        （decorated/unresolved/ingest_tried/ingest_keyed + 装饰率直方图）写入
        ``inject_extract_trend.db``——为「归零判 → 比率阈值」的校准攒各平台真实分布
        （拍脑袋定 30% 会在某个平台天天误报）。读端点
        ``GET /api/desktop/inject-health/extract-trend``。关闭时
        ``record_inject_extract_trend`` 恒 no-op。
        """
        try:
            _di = ((self.config.config.get("inbox") or {})
                   .get("desktop_inject") or {})
            if not _di.get("trend_log", False):
                self.logger.info(
                    "注入抽取率趋势落库未启用（inbox.desktop_inject.trend_log=false）")
                return
            from src.web.inject_extract_trend import (
                configure_inject_extract_trend,
            )
            _cfg_dir = Path(self.config.config_path).parent
            store = configure_inject_extract_trend(
                enabled=True,
                db_path=_cfg_dir / "inject_extract_trend.db",
                retention_days=float(_di.get("trend_retention_days", 90)),
            )
            if store is not None:
                self.logger.info(
                    "✅ 注入抽取率趋势落库已就绪（retention=%sd）",
                    _di.get("trend_retention_days", 90))
        except Exception:
            self.logger.warning("注入抽取率趋势落库初始化失败（已忽略）", exc_info=True)

    def _maybe_init_monetization(self, *args, **kwargs):
        from src.bootstrap.background_tasks import maybe_init_monetization
        return maybe_init_monetization(self, *args, **kwargs)

    def _build_care_paywall(self, care_store):
        """K2b：构造主动关怀配额门控回调。变现 gate 关 → 返回 None（不拦，零破坏）。

        回调懒读 app.state 的 MonetizationRuntime：免费用户近 24h 已发主动数超配额 → False。
        """
        try:
            mon = (self.config.config.get("monetization") or {})
            if not (mon.get("enabled") and (mon.get("gate") or {}).get("enabled")):
                return None
        except Exception:
            return None

        def _allowed(contact_key: str) -> bool:
            try:
                import time as _t
                from src.utils.monetization_runtime import MonetizationRuntime
                rt = MonetizationRuntime.from_app(self._web_app)
                if rt is None:
                    return True
                since = _t.time() - 86400.0
                sent = care_store.count_sent_since(contact_key, since)
                return rt.proactive_allowed(contact_key, sent)
            except Exception:
                return True  # 门控异常绝不拦关怀

        self.logger.info("✅ 主动关怀变现配额门控已接入")
        return _allowed

    async def _maybe_start_proactive_care(self, *args, **kwargs):
        from src.bootstrap.background_tasks import maybe_start_proactive_care
        return await maybe_start_proactive_care(self, *args, **kwargs)

    async def _maybe_start_nurture_engine(self, *args, **kwargs):
        from src.bootstrap.background_tasks import maybe_start_nurture_engine
        return await maybe_start_nurture_engine(self, *args, **kwargs)

    async def _maybe_start_companion_proactive(self) -> None:
        from src.companion.proactive_topic import maybe_start_companion_proactive
        return await maybe_start_companion_proactive(self)

    async def _maybe_start_reactivation_loop(self, *args, **kwargs):
        from src.bootstrap.background_tasks import maybe_start_reactivation_loop
        return await maybe_start_reactivation_loop(self, *args, **kwargs)

    async def _wait_until_telegram_ready(self) -> None:
        """轮询 telegram_client.running/client.is_connected 直到 True，用于启动
        顺序解耦：我们不能 await telegram_client.start()（它内部 idle 永不返回），
        但需要在继续启动 RPA 之前给 Telegram 一个合理的就绪窗口。"""
        while True:
            try:
                tc = self.telegram_client
                running = bool(getattr(tc, "running", False))
                client = getattr(tc, "client", None)
                connected = bool(client and getattr(client, "is_connected", False))
                if running and connected:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.3)

    async def _warmup_embeddings(self, *args, **kwargs):
        from src.bootstrap.background_tasks import warmup_embeddings
        return await warmup_embeddings(self, *args, **kwargs)

    async def _episodic_backfill_on_startup(self):
        """可选：启动后补全一批缺失的情景记忆向量（配置 memory.vector.backfill_on_startup）。"""
        try:
            mcfg = (self.config.config or {}).get("memory") or {}
            vcfg = (mcfg.get("vector") or {})
            bcfg = vcfg.get("backfill_on_startup") or {}
            if not bcfg.get("enabled", False):
                return
            if (vcfg.get("backfill_periodic") or {}).get("enabled", False):
                self.logger.info(
                    "情景记忆启动补全已跳过（已启用周期补全 memory.vector.backfill_periodic，避免重复嵌入）"
                )
                return
            delay = float(bcfg.get("delay_seconds", 12))
            limit = max(1, min(int(bcfg.get("limit", 15)), 50))
            await asyncio.sleep(max(0.0, delay))
            if not self.running:
                return
            sm = self.skill_manager
            if not sm:
                return
            out = await sm.episodic_backfill_embeddings(limit)
            self.logger.info("情景记忆启动补全: %s", out)
        except Exception:
            self.logger.exception("情景记忆启动补全失败")

    async def _episodic_backfill_periodic(self, *args, **kwargs):
        from src.bootstrap.background_tasks import episodic_backfill_periodic
        return await episodic_backfill_periodic(self, *args, **kwargs)

    async def _periodic_self_heal(self):
        """每24小时执行一次知识库自愈巡检"""
        await asyncio.sleep(300)
        while self.running:
            try:
                cfg_dir = (Path(self.config.config_path).parent if hasattr(self.config, "config_path") else Path("config")).resolve()
                kb_path = (cfg_dir / "knowledge_base.db").resolve()
                if kb_path.exists():
                    from src.utils.kb_store import KnowledgeBaseStore
                    kb = KnowledgeBaseStore(kb_path)
                    result = kb.run_self_heal(stale_days=14)
                    self.logger.info(
                        "知识库自愈完成: 触发词扩展=%d, 归档=%d, 过载标记=%d",
                        result.get("triggers_expanded", 0),
                        result.get("entries_archived", 0),
                        result.get("overloaded_flagged", 0),
                    )
                    for detail in result.get("details", [])[:5]:
                        self.logger.debug("  自愈: %s", detail)
            except Exception as e:
                self.logger.warning("知识库自愈异常: %s", e)
            await asyncio.sleep(86400)

    async def _periodic_draft_eval(self):
        """W3-3G：每小时跑一次 reunion 草稿成功率评估。

        对所有「已发 24h+ 但还没评估」的草稿，看 sent_ts 后 24h 内有没有
        对方 msg_in，写回 ``draft_log.success``。让 digest 的成功率
        指标持续刷新，无需运营手动触发 ``/api/drafts/eval-run``。

        启动延迟 5min（避免与启动期其他 init 抢 SQLite 锁）；
        每轮 sleep 3600 秒（1h，比窗口 24h 更密以减小 stats 滞后）。
        """
        await asyncio.sleep(300)
        sched = getattr(getattr(self, "contacts", None), "draft_eval_scheduler", None)
        while self.running:
            if sched is not None:
                sched.run_once()
            interval = sched.next_interval_secs if sched else 3600
            await asyncio.sleep(interval)

    async def _periodic_daily_learn(self):
        """每24小时执行一次自动学习：汇总未命中 → AI生成草稿 → 等待人工审核"""
        await asyncio.sleep(600)
        while self.running:
            try:
                cfg_dir = (Path(self.config.config_path).parent
                           if hasattr(self.config, "config_path")
                           else Path("config")).resolve()
                kb_path = (cfg_dir / "knowledge_base.db").resolve()
                if kb_path.exists() and self.ai_client:
                    from src.utils.kb_store import KnowledgeBaseStore
                    from src.utils.daily_learner import DailyLearner
                    kb = KnowledgeBaseStore(kb_path)
                    learner = DailyLearner(kb, self.ai_client, db_path=kb_path)
                    domain_name = ""
                    lcfg = {}
                    if hasattr(self.config, "config") and isinstance(self.config.config, dict):
                        domain_name = effective_domain_name(self.config.config)
                        lcfg = self.config.config.get("kb_learner") or {}
                    domain_ctx = f"当前行业: {domain_name}" if domain_name else ""
                    result = await learner.run_daily_learn(
                        domain_context=domain_ctx,
                        min_miss_count=lcfg.get("min_miss_count"),
                        source="scheduled",
                    )
                    self.logger.info(
                        "每日自动学习完成: 收集=%d, 生成=%d, 保存=%d",
                        result["collected"], result["generated"], result["saved"]
                    )
            except Exception as e:
                self.logger.warning("每日自动学习异常: %s", e)
            await asyncio.sleep(86400)

    async def stop(self):
        """停止 AI 聊天助手(Stage4:实现已迁至 bootstrap/lifecycle)。"""
        from src.bootstrap.lifecycle import stop_assistant
        return await stop_assistant(self)

    def _setup_signal_handlers(self):
        """设置信号处理"""
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """信号处理函数"""
        self.logger.info(f"收到信号 {signum}, 正在关闭...")
        asyncio.create_task(self.stop())


# CLI 入口 --check / --init 已抽取到 src/bootstrap/cli.py（2026-07-11 重构 Stage 1，行为不变）
from src.bootstrap.cli import run_config_check, run_init


async def main():
    """主函数"""
    assistant = AIChatAssistant()

    # 初始化
    if not await assistant.initialize():
        print("初始化失败，请检查配置和日志")
        return 1

    try:
        # 启动
        await assistant.start()
    except Exception as e:
        logging.error(f"程序运行错误: {e}")
        return 1

    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Telegram MTProto AI 多平台客服主程序")
    parser.add_argument(
        "--check", action="store_true",
        help="只体检配置并退出（不启动服务）；有 error 级问题返回非零退出码")
    parser.add_argument(
        "--init", nargs="?", const="", metavar="PRESET",
        help="用场景预设生成 config.yaml（无名称则列出可用预设）")
    parser.add_argument(
        "--set", action="append", metavar="KEY=VAL",
        help="--init 时覆盖配置项，如 --set ai.api_key=sk-xxx（可多次）")
    parser.add_argument(
        "--force", action="store_true", help="--init 时覆盖已存在的 config.yaml")
    parser.add_argument(
        "--config", default=None, help="指定 config.yaml 路径（默认 config/config.yaml）")
    args = parser.parse_args()

    if args.init is not None:
        sys.exit(run_init(args.init, args.config, args.set, args.force))

    if args.check:
        sys.exit(run_config_check(args.config))

    # 设置默认事件循环策略（Windows需要）
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # 运行主程序
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
