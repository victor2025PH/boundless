"""云端故障主机告警 wiring 测试（2026-07-12）。

覆盖：
- AIClient 生产主路径（openai_compat 运行时）双失败 + key 特征 → notify_key_failure
  （修「余额耗尽发生在运行中却静默到下次重启」的盲区）；
- 占位/未配置 key（桌面首启预期态）不弹；
- 熔断开路（含无 key 特征的网络黑洞）→ notify_cloud_outage，文案区分兜底就绪与否；
- 告警标签可读（model @ host，而非裸 provider 名）；
- HealthWatchdog：余额低水位 → notify_balance_low；余额接口 401 → notify_key_failure；
  本地兜底长期顶班 → 升级提醒（首个周期只建基线、after_min 前不提醒、idle 重置）。

不触网：主链客户端用假对象，探针/notify 全 monkeypatch。
"""

import time
import types

from src.ai.ai_client import AIClient
from src.inbox.health_watchdog import HealthWatchdog
from src.utils import host_alert


class _Cfg:
    config_path = None
    config = {"web_admin": {"site_name": "T"}, "ai": {}}

    def get_ai_config(self):
        return {}


class _FailingChatClient:
    """恒抛指定异常的 AsyncOpenAI 替身。"""

    def __init__(self, exc: Exception, base_url: str = "https://api.deepseek.com/v1"):
        self.base_url = base_url
        outer_exc = exc

        class _Completions:
            async def create(self, **kw):
                raise outer_exc

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def _client(exc: Exception) -> AIClient:
    c = AIClient(_Cfg())
    c._use_openai_compat = True
    c._oa_client = _FailingChatClient(exc)
    c.model = "deepseek-chat"
    c.timeout = 5
    c._cb_enabled = False
    return c


def _capture_notifications(monkeypatch):
    seen = {"keyfail": [], "outage": []}
    monkeypatch.setattr(
        host_alert, "notify_key_failure",
        lambda provider, detail="", **kw: seen["keyfail"].append((provider, detail)) or True)
    monkeypatch.setattr(
        host_alert, "notify_cloud_outage",
        lambda provider, detail="", *, fallback_ready=False, **kw:
        seen["outage"].append((provider, detail, fallback_ready)) or True)
    return seen


# ── 运行时主路径 key 失效弹窗 ─────────────────────────────────────


async def test_runtime_key_failure_alerts(monkeypatch):
    seen = _capture_notifications(monkeypatch)
    c = _client(Exception("Error code: 402 - Insufficient Balance"))
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out  # canned 兜底仍出话
    assert len(seen["keyfail"]) == 1
    label, detail = seen["keyfail"][0]
    assert label == "deepseek-chat @ api.deepseek.com"  # 可读标签而非 openai_compatible
    assert "Insufficient Balance" in detail


async def test_runtime_connection_error_does_not_key_alert(monkeypatch):
    seen = _capture_notifications(monkeypatch)
    c = _client(Exception("Connection error."))
    await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert seen["keyfail"] == []  # 网络故障≠key 异常（由熔断开路的 outage 告警负责）


async def test_placeholder_key_never_alerts(monkeypatch):
    seen = _capture_notifications(monkeypatch)
    c = _client(Exception("Error code: 401 - Authentication Fails"))
    c._key_is_placeholder = True  # 桌面首启：key 留空/YOUR_* 占位
    await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert seen["keyfail"] == []


def test_placeholder_flag_from_initialize_semantics():
    """initialize() 的占位判定口径：空/YOUR_* = 占位。"""
    c = AIClient(_Cfg())
    for raw, expect in (("", True), ("  ", True), ("YOUR_AI_API_KEY", True),
                        ("your_ai_api_key", True), ("sk-real", False), ("ollama", False)):
        _k = str(raw or "").strip()
        c._key_is_placeholder = (not _k) or _k.upper().startswith("YOUR_")
        assert c._key_is_placeholder is expect, raw


# ── 熔断开路 → 云端不可达告警 ────────────────────────────────────


def test_circuit_trip_emits_outage_alert(monkeypatch):
    seen = _capture_notifications(monkeypatch)
    c = AIClient(_Cfg())
    c.model = "deepseek-chat"
    c._oa_client = _FailingChatClient(Exception("x"))
    c._cb_enabled = True
    c._cb_window_size = 4
    from collections import deque
    c._cb_window = deque([False, False, False, False], maxlen=4)
    c._maybe_trip_circuit()
    assert c._cb_open_until > time.time()
    assert len(seen["outage"]) == 1
    label, detail, fallback_ready = seen["outage"][0]
    assert label == "deepseek-chat @ api.deepseek.com"
    assert "100%" in detail
    assert fallback_ready is False  # 未配兜底


def test_circuit_trip_reports_fallback_ready(monkeypatch):
    seen = _capture_notifications(monkeypatch)
    c = AIClient(_Cfg())
    c.model = "deepseek-chat"
    c._fb_client = object()
    c._fb_model = "qwen-local"
    c._cb_enabled = True
    c._cb_window_size = 2
    from collections import deque
    c._cb_window = deque([False, False], maxlen=2)
    c._maybe_trip_circuit()
    assert seen["outage"] and seen["outage"][0][2] is True


def test_half_open_retrip_alerts_debounced_by_host_alert(monkeypatch):
    """半开再失败也走 outage 告警（真实去抖由 host_alert 冷却窗负责，同 key 不刷屏）。"""
    seen = _capture_notifications(monkeypatch)
    c = AIClient(_Cfg())
    c.model = "m"
    c._cb_enabled = True
    c._cb_half_open = True
    c._maybe_trip_circuit()
    assert len(seen["outage"]) == 1


def test_alert_label_falls_back_to_provider():
    c = AIClient(_Cfg())
    c.model = ""
    c._oa_client = None
    assert c._alert_label() == "gemini"


# ── HealthWatchdog：余额水位 + 兜底顶班 ──────────────────────────


class _CM:
    def __init__(self, config):
        self.config = config


def _watchdog(config, *, ai_stats=None):
    state = types.SimpleNamespace()
    if ai_stats is not None:
        state.ai_client = types.SimpleNamespace(get_stats=lambda: ai_stats)
    app = types.SimpleNamespace(state=state)
    return HealthWatchdog(app=app, config_manager=_CM(config), interval_sec=60)


def _balance_cfg(enabled=True):
    return {
        "ai": {"provider": "openai_compatible",
               "base_url": "https://api.deepseek.com/v1", "api_key": "sk-x"},
        "ops": {"cloud_credentials": {"enabled": enabled, "balance_warn_cny": 20,
                                      "probe_interval_sec": 3600}},
    }


def test_watchdog_balance_low_alerts(monkeypatch):
    from src.utils import cloud_credentials as cc
    monkeypatch.setattr(
        cc, "collect_cloud_balances",
        lambda cfg, **kw: [{"status": "low", "provider": "DeepSeek", "balance": 12.0,
                            "threshold": 20.0, "currency": "CNY", "remind_sec": 21600}])
    seen = []
    monkeypatch.setattr(host_alert, "notify_balance_low",
                        lambda p, b, t, cur="CNY", **kw: seen.append((p, b, t)) or True)
    wd = _watchdog(_balance_cfg())
    wd._check_cloud_balance(now=1000.0)
    assert seen == [("DeepSeek", 12.0, 20.0)]
    assert wd.total_cloud_balance_alerts == 1
    # 稀疏节流：间隔内第二次 tick 不再探测
    wd._check_cloud_balance(now=1300.0)
    assert len(seen) == 1


def test_watchdog_balance_auth_failed_routes_to_keyfail(monkeypatch):
    from src.utils import cloud_credentials as cc
    monkeypatch.setattr(
        cc, "collect_cloud_balances",
        lambda cfg, **kw: [{"status": "auth_failed", "provider": "DeepSeek",
                            "error": "HTTP 401"}])
    seen = []
    monkeypatch.setattr(host_alert, "notify_key_failure",
                        lambda p, d="", **kw: seen.append((p, d)) or True)
    wd = _watchdog(_balance_cfg())
    wd._check_cloud_balance(now=1000.0)
    assert seen and seen[0][0] == "DeepSeek" and "401" in seen[0][1]


def test_watchdog_balance_disabled_noop(monkeypatch):
    from src.utils import cloud_credentials as cc
    called = []
    monkeypatch.setattr(cc, "collect_cloud_balances",
                        lambda cfg, **kw: called.append(1) or [])
    wd = _watchdog(_balance_cfg(enabled=False))
    wd._check_cloud_balance(now=1000.0)
    assert not called  # enabled=false 在 collect 之前就短路


def test_watchdog_balance_ok_no_alert(monkeypatch):
    from src.utils import cloud_credentials as cc
    monkeypatch.setattr(
        cc, "collect_cloud_balances",
        lambda cfg, **kw: [{"status": "ok", "provider": "DeepSeek", "balance": 100.0}])
    seen = []
    monkeypatch.setattr(host_alert, "notify_balance_low",
                        lambda *a, **kw: seen.append(1) or True)
    wd = _watchdog(_balance_cfg())
    wd._check_cloud_balance(now=1000.0)
    assert not seen and wd.total_cloud_balance_alerts == 0


def test_watchdog_balance_pool_key_low_alerts_independently(monkeypatch):
    """主 Key 健康、备用 Key 欠费 → 只为备用 Key 告警（provider 名区分，独立去抖）。"""
    from src.utils import cloud_credentials as cc
    monkeypatch.setattr(
        cc, "collect_cloud_balances",
        lambda cfg, **kw: [
            {"status": "ok", "provider": "DeepSeek", "balance": 100.0},
            {"status": "low", "provider": "备用:ds2", "balance": 3.0,
             "threshold": 20.0, "currency": "CNY", "remind_sec": 21600},
        ])
    seen = []
    monkeypatch.setattr(host_alert, "notify_balance_low",
                        lambda p, b, t, cur="CNY", **kw: seen.append(p) or True)
    wd = _watchdog(_balance_cfg())
    wd._check_cloud_balance(now=1000.0)
    assert seen == ["备用:ds2"]
    assert wd.total_cloud_balance_alerts == 1


def test_fallback_duty_reminder_escalates(monkeypatch):
    stats = {"local_fallback_calls": 0}
    wd = _watchdog({}, ai_stats=stats)
    seen = []
    monkeypatch.setattr(host_alert, "notify_host",
                        lambda t, m, *, key="", cooldown_sec=0: seen.append((t, key)) or True)
    t0 = 10000.0
    wd._check_local_fallback_duty(now=t0)          # 首个周期建基线
    stats["local_fallback_calls"] = 3
    wd._check_local_fallback_duty(now=t0 + 300)    # 顶班开始（30min 未满，不提醒）
    assert seen == []
    stats["local_fallback_calls"] = 9
    wd._check_local_fallback_duty(now=t0 + 300 + 1900)  # 顶班已超 after_min(30min)
    assert len(seen) == 1
    assert seen[0][1] == "fallback_duty"
    assert wd.total_fallback_duty_reminders == 1


def test_fallback_duty_resets_after_idle(monkeypatch):
    stats = {"local_fallback_calls": 5}
    wd = _watchdog({}, ai_stats=stats)
    seen = []
    monkeypatch.setattr(host_alert, "notify_host",
                        lambda *a, **kw: seen.append(1) or True)
    t0 = 20000.0
    wd._check_local_fallback_duty(now=t0)                 # 基线
    stats["local_fallback_calls"] = 6
    wd._check_local_fallback_duty(now=t0 + 300)           # 顶班计时开始
    # 连续 2 个无增量周期 → 重置顶班计时
    wd._check_local_fallback_duty(now=t0 + 600)
    wd._check_local_fallback_duty(now=t0 + 900)
    assert wd._fb_duty_since_ts == 0.0
    # 云端再次故障：重新起算 after_min，不会立刻提醒
    stats["local_fallback_calls"] = 7
    wd._check_local_fallback_duty(now=t0 + 1200)
    assert seen == []


def test_fallback_duty_disabled(monkeypatch):
    stats = {"local_fallback_calls": 100}
    wd = _watchdog({"health_watchdog": {"fallback_duty_remind": {"enabled": False}}},
                   ai_stats=stats)
    called = []
    monkeypatch.setattr(host_alert, "notify_host",
                        lambda *a, **kw: called.append(1) or True)
    wd._check_local_fallback_duty(now=1.0)
    wd._check_local_fallback_duty(now=99999.0)
    assert not called and wd._fb_duty_last_calls is None


def test_fallback_duty_no_ai_client():
    wd = _watchdog({})  # state 无 ai_client
    wd._check_local_fallback_duty(now=1.0)  # 不抛即过


# ── 备用 Key 主动探活告警 ────────────────────────────────────────


def test_watchdog_pool_ping_auth_fail_alerts(monkeypatch):
    """池 key 探活 401（通了但被拒）→ key 失效告警；网络不可达/正常 → 静默。"""
    from src.utils import cloud_credentials as ccm
    monkeypatch.setattr(
        ccm, "run_chat_pings",
        lambda cfg, **kw: [
            {"name": "dead", "ok": False, "reachable": True,
             "http_status": 401, "error": "HTTP 401: invalid key"},
            {"name": "netfail", "ok": False, "reachable": False, "error": "timeout"},
            {"name": "alive", "ok": True, "reachable": True, "http_status": 200},
        ])
    seen = []
    monkeypatch.setattr(host_alert, "notify_key_failure",
                        lambda p, d="", **kw: seen.append((p, d)) or True)
    wd = _watchdog({})
    wd._check_pool_key_pings(now=1000.0)
    assert len(seen) == 1
    assert seen[0][0] == "备用:dead" and "401" in seen[0][1]
    assert wd.total_cloud_balance_alerts == 1


def test_watchdog_pool_ping_empty_noop(monkeypatch):
    from src.utils import cloud_credentials as ccm
    monkeypatch.setattr(ccm, "run_chat_pings", lambda cfg, **kw: [])
    wd = _watchdog({})
    wd._check_pool_key_pings(now=1000.0)  # 不抛即过
    assert wd.total_cloud_balance_alerts == 0


# ── 降级态快照（坐席状态条数据源）────────────────────────────────


class TestDegradationSnapshot:
    def _client(self):
        c = AIClient(_Cfg())
        c.model = "deepseek-chat"
        return c

    def test_no_traffic_not_degraded(self):
        snap = self._client().degradation_snapshot()
        assert snap == {"degraded": False, "mode": "primary"}

    def test_primary_recent_not_degraded(self):
        c = self._client()
        c._last_primary_ok_ts = time.time()
        assert c.degradation_snapshot()["degraded"] is False

    def test_pool_serving_degraded(self):
        c = self._client()
        c._last_primary_ok_ts = time.time() - 3600   # 主链一小时没出话
        c._last_pool_ok_ts = time.time() - 30        # 池 30s 前顶班
        snap = c.degradation_snapshot()
        assert snap["degraded"] is True and snap["mode"] == "pool"

    def test_local_serving_degraded(self):
        c = self._client()
        c._last_fb_ok_ts = time.time() - 10
        snap = c.degradation_snapshot()
        assert snap["degraded"] is True and snap["mode"] == "local"

    def test_pool_beats_local_in_mode(self):
        c = self._client()
        c._last_pool_ok_ts = time.time() - 5
        c._last_fb_ok_ts = time.time() - 5
        assert c.degradation_snapshot()["mode"] == "pool"

    def test_circuit_open_degraded_predicts_chain(self):
        c = self._client()
        c._cb_enabled = True
        c._cb_open_until = time.time() + 60
        snap = c.degradation_snapshot()
        assert snap["degraded"] is True
        assert snap["mode"] == "none"          # 无池无本地 → none
        c._fb_client = object(); c._fb_model = "qwen-local"
        assert c.degradation_snapshot()["mode"] == "local"
        c._pool_entries = [{"name": "b", "client": object(), "model": "m",
                            "label": "l", "bad_until": 0.0}]
        assert c.degradation_snapshot()["mode"] == "pool"

    def test_recovery_clears_degraded(self):
        c = self._client()
        c._last_pool_ok_ts = time.time() - 60
        c._last_primary_ok_ts = time.time()          # 主链已恢复且更近
        assert c.degradation_snapshot()["degraded"] is False
