"""pytest 共享 fixtures — Web 管理面板集成测试"""

import asyncio
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# host_alert（云端 Key 异常弹窗）在测试进程里全局静默：任何用例只要碰到
# AIClient 的 key 失效路径，都不许在开发机弹真·系统弹窗（-n auto 多 worker
# 各自独立去抖，一旦触发就是弹窗风暴）。test_host_alert 自有 fixture 再细控。
os.environ.setdefault("HOST_ALERT_SILENT", "1")

# 测试进程密封：import 期即剥离 AITR_*（桌面/实例部署注入用）环境变量。
# ConfigManager._apply_env_overrides 会用 AITR_WEB_TOKEN 覆盖 web_admin.auth_token、
# AITR_DATA_DIR/AITR_CONFIG_PATH 改写配置定位——开发机 shell 起过 dev 实例后残留这些
# 变量，fixture 里的 test-token 会被静默替换 → 整片鉴权用例 401 的"幽灵失败"
# （2026-07-20 实测踩中，曾被误判为 12 个存量回归）。测试必须只信 fixture 配置；
# 需要这些变量的用例（test_config_manager / test_host_alert）自行 monkeypatch.setenv。
for _k in [k for k in os.environ if k.startswith("AITR_")]:
    os.environ.pop(_k, None)

# 剥离之后**再指一个进程级临时数据根**（2026-07-29）：只剥不设会让所有按
# ``AITR_DATA_DIR`` 定位数据的模块回落 ``Path.cwd()/config``＝**引擎根仓库目录**，
# 于是测试真的写进仓库里那份生产配置。实测（用回归时间窗比对 config/ 文件 mtime）
# 被写脏的有 persona_usage.db / vision_metrics.db / ops_events.db / autoreply_audit.db /
# fatex.db / events/spool/*.jsonl —— 计数、审计台账、事件 spool 全被测试数据污染。
# 指到 tmp 后，凡遵循该部署约定的模块（persona_usage / telemetry / desktop_selectors /
# licensing.data_paths / instance_restart_status / config_manager 的无参定位）一次性全隔离。
# 需要真值的用例（test_config_manager / test_licensing_data_paths / test_instance_restart_status）
# 自行 monkeypatch.setenv，晚于本处生效、不受影响。
_TEST_DATA_ROOT = Path(tempfile.mkdtemp(prefix="aitr-test-dataroot-"))
(_TEST_DATA_ROOT / "config").mkdir(parents=True, exist_ok=True)
os.environ["AITR_DATA_DIR"] = str(_TEST_DATA_ROOT)
atexit.register(lambda: shutil.rmtree(_TEST_DATA_ROOT, ignore_errors=True))

# reunion 草稿 prompt 配置（``config/reunion_prompts.yaml``）＝真实生产配置，
# 且 ``POST /api/reunion-prompts/set-default`` 会**写**它。它不走 AITR_DATA_DIR，
# 但自带 ``REUNION_PROMPTS_PATH`` 覆盖钩子 → 指到 tmp，并把仓库真值**拷一份**过去，
# 使读到的内容与生产一致（只有写落在 tmp）。
_REPO_REUNION = Path(__file__).resolve().parent.parent / "config" / "reunion_prompts.yaml"
_TEST_REUNION = _TEST_DATA_ROOT / "config" / "reunion_prompts.yaml"
try:
    if _REPO_REUNION.exists():
        shutil.copy2(_REPO_REUNION, _TEST_REUNION)
except Exception:
    pass
os.environ["REUNION_PROMPTS_PATH"] = str(_TEST_REUNION)

# 意图字典（``config/intent_tags.yaml``）＝生产在用的跨平台意图词表，且后台有整套
# 编辑栈会**写**它（``write_intent_tags_yaml`` + 时间戳备份轮转 + restore）。它不走
# AITR_DATA_DIR，但自带 ``INTENT_TAGS_PATH`` 覆盖钩子 → 与 reunion 同款：指到 tmp 并把
# 仓库真值拷一份过去（读到的内容与生产一致，只有写落 tmp）。
# 现状（2026-07-29 探针实测）：既有 intent_tags 用例都自设该变量，唯一触及写端点的
# ``test_admin_route_inventory`` 只静态列 URL 不真调 → 这颗地雷**当前不可达**。
# 本兜底是防「以后谁加一个真打 /api/rpa/intent-tags/write 的路由测试」——那一刻
# 生产词表就会被测试数据覆盖 + 备份轮转把真值挤走（global_rules 已实锤过同样剧本）。
# 自设该变量的用例晚于本处生效，不受影响。
_REPO_INTENT_TAGS = Path(__file__).resolve().parent.parent / "config" / "intent_tags.yaml"
_TEST_INTENT_TAGS = _TEST_DATA_ROOT / "config" / "intent_tags.yaml"
try:
    if _REPO_INTENT_TAGS.exists():
        shutil.copy2(_REPO_INTENT_TAGS, _TEST_INTENT_TAGS)
except Exception:
    pass
os.environ["INTENT_TAGS_PATH"] = str(_TEST_INTENT_TAGS)

from starlette.testclient import TestClient

from src.utils.config_manager import ConfigManager  # noqa: E402  (env 剥离必须先跑)
from src.utils.audit_store import AuditStore
from src.web.admin import create_app

# 字符额度库全局隔离：`license_quota.db` 的默认路径由 __file__ 推出，落在
# **仓库的 config/ 目录**。只要某个用例造出一份有效授权（试用链的用例就会），
# record/topup 就会写进那个真库——2026-07-27 实测：一次 pytest 把 10 万字符加量
# 写到了本机真 lic_id 上，之后本地激活的授权凭空多出 10 万额度。
# 额度是要拿来对外收钱的数，绝不能被测试污染，故按用例隔离到 tmp。
@pytest.fixture(autouse=True)
def _isolate_license_quota_db(tmp_path_factory):
    try:
        from src.licensing.quota_store import (
            configure_license_quota_store,
            reset_license_quota_store,
        )
    except Exception:  # pragma: no cover - 模块缺失时无需隔离
        yield
        return
    reset_license_quota_store()
    d = tmp_path_factory.mktemp("licquota")
    configure_license_quota_store(db_path=str(d / "license_quota.db"))
    try:
        yield
    finally:
        reset_license_quota_store()


# ─────────────────────────────────────────────────────────
# 配置目录 fixture
# ─────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def _config_data():
    """最小可用的配置文件内容"""
    cfg = {
        "telegram": {"api_id": "111", "api_hash": "abc", "phone_number": "+1"},
        "ai": {"api_key": "test"},
        "skills": {"enabled": []},
        "domain": "payment",
        "domain_plugins": {"payment": {"enabled": True}},
        "web_admin": {
            "secret_key": "test-secret-very-long-key-for-testing",
            "auth_token": "test-token-123",
            "session_max_age": 3600,
        },
    }
    tpl = {
        "greeting": ["hello", "hi", "hey there"],
        "farewell": "goodbye",
        "follow_up": "let me know if you need anything else",
    }
    rates = {
        "channels": {
            "ep": {
                "display_name": "EP通道", "fee_rate": "0.5%", "status": "正常",
                "limits": {"default": "100-20000"}, "names": ["EP"],
            },
            "usdt": {
                "display_name": "USDT通道", "fee_rate": "1.0%", "status": "正常",
                "limits": {"default": "50-10000"}, "names": ["USDT"],
            },
        }
    }
    strategies = {
        "strategies": {
            "standard": {
                "temperature": 0.7, "max_tokens": 800,
                "context_rounds": 3, "enabled": True,
            }
        },
        "intent_strategy_map": {"default": "standard"},
    }
    return {"cfg": cfg, "tpl": tpl, "rates": rates, "strategies": strategies}


@pytest.fixture()
def config_dir(tmp_path, _config_data):
    d = _config_data
    (tmp_path / "config.yaml").write_text(
        yaml.dump(d["cfg"], allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "templates.yaml").write_text(
        yaml.dump(d["tpl"], allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "exchange_rates.yaml").write_text(
        yaml.dump(d["rates"], allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "reply_strategies.yaml").write_text(
        yaml.dump(d["strategies"], allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "snapshots").mkdir(exist_ok=True)
    # Create minimal domain pack for payment to enable domain web routes in tests
    domain_dir = tmp_path.parent / "domains" / "payment" / "web"
    domain_tpl_dir = domain_dir / "templates"
    domain_dir.mkdir(parents=True, exist_ok=True)
    domain_tpl_dir.mkdir(exist_ok=True)
    _real_ch_tpl = Path(__file__).resolve().parent.parent / "domains" / "payment" / "web" / "templates" / "channels.html"
    if _real_ch_tpl.exists():
        import shutil
        shutil.copy2(str(_real_ch_tpl), str(domain_tpl_dir / "channels.html"))
    manifest = {
        "name": "payment",
        "display_name": "支付通道客服",
        "version": "1.0",
        "web": {
            "routes": True,
            "pages": [
                {"key": "ch", "path": "/channels", "label": "通道管理",
                 "label_simple": "通道状态", "icon": "globe",
                 "section": "ops", "show_in_simple": True,
                 "roles": ["master", "admin", "viewer"],
                 "cmd_keys": "channels 通道 状态 管理"},
            ],
            "dashboard_widgets": [
                {"key": "channel_health", "section": "pro-only"},
            ],
        },
    }
    (domain_dir.parent / "manifest.yaml").write_text(
        yaml.dump(manifest, allow_unicode=True), encoding="utf-8"
    )
    return tmp_path


@pytest.fixture()
def config_manager(config_dir):
    cm = ConfigManager(str(config_dir / "config.yaml"))
    asyncio.run(cm.load())
    return cm


@pytest.fixture()
def audit_store(config_dir):
    return AuditStore(db_path=config_dir / "audit.db")


# ─────────────────────────────────────────────────────────
# App & Client fixtures
# ─────────────────────────────────────────────────────────

@pytest.fixture()
def app(config_manager, audit_store):
    return create_app(
        config_manager,
        audit_store=audit_store,
        boot_ts=0,
        telegram_client=None,
        event_tracker=None,
        log_buffer=None,
    )


@pytest.fixture()
def client(app):
    """未认证的测试客户端"""
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    # P22-B: clean rate-limit state after the test
    reset_fn = getattr(app.state, "intent_tags_rate_limit_reset", None)
    if callable(reset_fn):
        try: reset_fn()
        except Exception: pass


# ─────────────────────────────────────────────────────────
# 账号注册表隔离（防测试污染生产库）
# ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolated_web_env(monkeypatch):
    """隔离部署用 AITR_* 环境变量，防本地 shell 泄漏进测试进程。

    背景（2026-07 实测踩中）：开发机常为起隔离 dev 实例而在 shell 里持久化
    ``AITR_WEB_TOKEN=dev-ui-check`` 等变量；``config_manager`` 会用它覆盖
    ``web_admin.auth_token`` → ``create_app`` 的 ``_ensure_master`` 以该 token
    建 admin → conftest ``auth_client`` 用 test-token-123 登录必败 →
    **所有需登录的 HTML 渲染断言集体假失败**（曾误判为 40 个存量回归）。
    需要这些变量的用例（test_config_manager 等）自行 setenv，晚于本清理生效。
    """
    for k in (
        "AITR_WEB_TOKEN", "AITR_WEB_HOST", "AITR_WEB_PORT", "AITR_DESKTOP_MODE",
        "AITR_HOSTED_AI_KEY", "AITR_HOSTED_AI_BASE_URL", "AITR_HOSTED_AI_MODEL",
    ):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def _isolated_account_registry(tmp_path):
    """把 ``get_account_registry()`` 进程单例重定向到每测试独立的临时库。

    背景：该单例默认落 ``config/account_registry.db``（相对 CWD＝仓库根），曾有测试
    直接 upsert 假账号（acct_fleet_test 等）写进**生产注册表**——编排器把它当真账号
    拉起，因无凭据无限重启 → HealthWatchdog 红灯告警刷屏。此 autouse 护栏使任何
    测试内的注册表读写都进 tmp_path，测试自行 monkeypatch 单例的仍按其自身设定生效。
    """
    import src.integrations.account_registry as ar

    old = ar._registry
    ar._registry = ar.AccountRegistry(tmp_path / "_test_account_registry.db")
    try:
        yield
    finally:
        ar._registry = old


@pytest.fixture()
def auth_client(client, config_dir):
    """
    已认证为 master：
    - 无用户时先创建 admin（与 change-password 等测试对齐）
    - 优先用户名/密码登录；若仍停留在 /login 则回退 legacy auth_token
    - 为 JSON API 设置 Bearer（与 web_admin.auth_token 一致），绕过 CSRF 对 application/json 的拦截
    """
    from src.utils.web_user_store import ROLE_MASTER, WebUserStore

    store = WebUserStore(config_dir / "web_users.db")
    if store.user_count() == 0:
        store.create_user("admin", "test-token-123", ROLE_MASTER)

    client.get("/login")
    r = client.post(
        "/login",
        data={"username": "admin", "password": "test-token-123"},
        follow_redirects=True,
    )
    if "/login" in str(getattr(r, "url", "")):
        client.post(
            "/login",
            data={"auth_token": "test-token-123"},
            follow_redirects=True,
        )
    # JSON API 的 CSRF：Bearer 与 web_admin.auth_token 一致
    client.headers.update({"Authorization": "Bearer test-token-123"})
    return client


# ─────────────────────────────────────────────────────────
# Contacts integration fixtures (Phase 1+ e2e 复用)
# ─────────────────────────────────────────────────────────

@pytest.fixture()
def contacts_store(tmp_path):
    """真 SQLite ContactStore（每测试独立 db）。"""
    from src.contacts.store import ContactStore
    db_path = tmp_path / "contacts_e2e.db"
    store = ContactStore(str(db_path))
    yield store
    try:
        store.close()
    except Exception:
        pass


@pytest.fixture()
def contacts_gateway(contacts_store):
    """真 ContactGateway（含 HandoffTokenService + MergeService）。"""
    from src.contacts.gateway import ContactGateway
    from src.contacts.handoff import HandoffTokenService
    from src.contacts.merge import MergeService
    return ContactGateway(
        contacts_store,
        HandoffTokenService(contacts_store, ttl_seconds=3600),
        MergeService(contacts_store),
    )


@pytest.fixture()
def contacts_hooks(contacts_gateway):
    """真 GatewayContactHooks。"""
    from src.contacts.rpa_hooks import GatewayContactHooks
    return GatewayContactHooks(contacts_gateway)


@pytest.fixture()
def mock_ai_client_ja():
    """mock AIClient.chat 默认返日文 portrait JSON（可在测试中 override）。"""
    from unittest.mock import AsyncMock, MagicMock
    ai = MagicMock()
    ai.chat = AsyncMock(return_value=(
        '{"language":"ja","tone":"casual_friendly",'
        '"interests":["旅行","料理"],"recent_topics":["週末の予定"],'
        '"key_facts":["日本在住"],"intimacy_signal":"warming"}'
    ))
    return ai


# ─────────────────────────────────────────────────────────
# P20-D: 自动重置 intent_tags 编辑滑窗（防测试间污染）
# ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_intent_tags_edit_window():
    """每个 test 前后清 rpa_shared._INTENT_TAGS_EDIT_WINDOW + counter，
    避免持久化 sidecar 跨测试漏数。仅在 rpa_shared 已加载时生效。

    P21-D: 关闭持久化防抖（测试中每次写都要触发 sidecar 更新）。
    """
    try:
        from src.integrations import rpa_shared as _shr
        _shr.reset_intent_tags_edit_window()
        # P21-D: tests run faster than 1s throttle would tolerate
        _shr._STATS_SAVE_MIN_INTERVAL_SEC = 0.0
        _shr._stats_last_save_ts = 0.0
    except Exception:
        pass
    yield
    try:
        from src.integrations import rpa_shared as _shr
        _shr.reset_intent_tags_edit_window()
    except Exception:
        pass


def _reset_process_singletons_now():
    """重置一批「进程内累积型」全局单例，消除测试间串扰。

    仅纳入「在进程内累积状态、且经零参 getter 复用」的单例：
    - MetricsStore（_instance）：计数器/时间序列累积，曾致 #74 的 flaky。
    - EventBus（_bus）：_subscribers / _history 累积，14 个测试文件用；漏挂的
      订阅者会把后续 publish 串到旧 sink，history 也会跨测试漏数。

    不纳入 db-backed 单例（DocumentJobStore / EntitlementStore / CareSchedule /
    KillSwitch / CompanionFunnel / DeviceRegistry 等）——它们测试里走 :memory:
    或显式 path，天然隔离；盲目重置反而可能打断「fixture 初始化后复用」的用例。
    """
    try:
        from src.monitoring import metrics_store as _ms
        _ms.MetricsStore._instance = None
    except Exception:
        pass
    try:
        from src.integrations.shared import event_bus as _eb
        _eb._bus = None
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_process_singletons():
    """每个 test 前后重置累积型进程内单例，从根上隔离串测。

    集中在 conftest 做 autouse 重置后，所有读单例的测试对任何泄漏免疫，无需
    再逐文件加隔离 fixture（与上方 _reset_intent_tags_edit_window 同模式）。
    各测试若需取「干净引用」，仍可在用例内显式重置并调对应 getter。
    具体纳入范围与理由见 _reset_process_singletons_now。
    """
    _reset_process_singletons_now()
    yield
    _reset_process_singletons_now()


@pytest.fixture(autouse=True)
def _isolated_audit_stores(tmp_path):
    """把三个「审计/事件/指标」单例库重定向到 tmp（否则写进仓库 config/）。

    这三个不走 ``AITR_DATA_DIR``，各自用相对 cwd 或 ``__file__`` 推导默认路径 →
    pytest 的 cwd＝引擎根，于是测试数据直接进**生产台账**：
      - ``config/autoreply_audit.db``  自动回复决策流（后台/桌面壳「实时流」面板读它）
      - ``config/ops_events.db``       反封号运维事件（「这号这周被风控几次」的唯一史料）
      - ``config/vision_metrics.db``   messenger VLM 调用指标

    污染这些库不改变系统行为，但会**把假数据混进给人看的审计与健康史**——运维据此
    判断「号是不是要炸」，掺了测试数据的台账比没有台账更糟。与
    ``_isolated_account_registry`` 同款：重定向单例，用完还原。
    """
    import src.integrations.messenger_rpa.vision_metrics as vm
    import src.integrations.protocol_autoreply_audit as ara
    import src.ops.ops_events as oe

    old_audit, old_ops = ara._audit, oe._store
    old_vm_path, old_vm_init = vm._db_path, vm._initialized
    ara._audit = ara.AutoReplyAudit(tmp_path / "_t_autoreply_audit.db")
    oe._store = None                     # 下次 getter 用下面的默认路径重建
    vm._db_path = tmp_path / "_t_vision_metrics.db"
    vm._initialized = False
    try:
        # ops_events 的 getter 首次调用才定路径 → 预热到 tmp，防测试传默认值
        try:
            oe.get_ops_event_store(str(tmp_path / "_t_ops_events.db"))
        except Exception:
            pass
        yield
    finally:
        ara._audit, oe._store = old_audit, old_ops
        vm._db_path, vm._initialized = old_vm_path, old_vm_init


@pytest.fixture(autouse=True)
def _isolated_global_rules(tmp_path):
    """把 ``PersonaManager`` 的 global_rules 落盘路径重定向到每测试独立临时文件。

    背景（2026-07-29 实锤事故）：``save_global_rules`` 的路径由
    ``Path(__file__).resolve().parents[2] / "config" / "global_rules.yaml"`` 推导——
    **完全无视 tmp_path**，直接写**仓库里那份生产在用的**文件（引擎按 mtime 热加载）。
    一个路由级测试因此把生产的 13 条回复硬约束（含「不要自称AI」这类安全项）清成
    ``[]``，并经备份轮转把测试数据推进 ``.bak.1`` 槽位（运维点「恢复槽位1」会二次
    清空）。事后从 git HEAD 还原。

    与 ``_isolated_account_registry`` 同族、同理由：产品代码里按 ``__file__`` 推导
    config 路径的**写**操作都是同一颗地雷，而单靠「测试自己记得隔离」不可靠。

    ⚠️ 曾试过「快照 config/ 目录、变了就点名」的通用探测器，在本机不可用：
    ``-n auto`` 并行下 worker A 的快照窗口会把 worker B 的写入算到 A 头上，
    且共享工作树上**其他 agent 线**正在编辑 config 文件、``*.db`` 被连接即改 mtime
    → 实测 243 个假阳性。故回到「按写入口精确重定向」这条本仓已验证的路子。

    2026-07-29 后续（P7-1 overlay 化）：产品侧写入已改落「可写数据区」
    （``AITR_DATA_DIR/config``，本 conftest 顶部已把它指向进程级 tmp），所以本 fixture
    已非唯一防线；但它把落点收到**每测试独立** tmp（而非全进程共享那个），
    仍在防「用例之间经同一份 global_rules 串味」，且显式覆写同时钉住读与写落点。
    """
    from src.utils.persona_manager import PersonaManager

    pm = PersonaManager.get_instance()
    old_path = pm._global_rules_path        # noqa: SLF001 — 正是要拦的那个字段
    old_cache = pm._global_rules
    old_sig = pm._global_rules_sig
    pm._global_rules_path = tmp_path / "global_rules.yaml"
    pm._global_rules = None
    pm._global_rules_sig = ("", 0.0, -1)
    try:
        yield
    finally:
        # 只在**单例还是我改过的那一个**时还原。若用例自己 reset() 了单例
        # （``test_global_rules_overlay.py`` 那类专测自动解析链的测试就这么做），
        # 此处绝不能再 get_instance() 把它复活、还盖上一个陈旧的 tmp 覆写——
        # 那会让下一个依赖自动解析的用例读到上一个用例的 tmp 路径。
        if PersonaManager._instance is pm:      # noqa: SLF001
            pm._global_rules_path = old_path
            pm._global_rules = old_cache
            pm._global_rules_sig = old_sig


@pytest.fixture()
def viewer_client(app, config_dir):
    """已认证为 viewer 角色的客户端"""
    with TestClient(app, raise_server_exceptions=True) as c:
        c.headers.update({"Authorization": "Bearer test-token-123"})
        # 先用 master 登录创建 viewer 用户
        c.post("/login", data={"auth_token": "test-token-123"}, follow_redirects=True)
        c.post(
            "/users/create",
            data={"username": "testviewer", "password": "viewer123", "role": "viewer"},
            follow_redirects=True,
        )
        c.get("/logout", follow_redirects=True)
        # 用 viewer 登录
        c.post(
            "/login",
            data={"username": "testviewer", "password": "viewer123"},
            follow_redirects=True,
        )
        yield c
