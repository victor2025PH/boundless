"""host_alert 主机告警工具单测（不真正弹窗：HOST_ALERT_SILENT 静默）。"""
import os

import pytest

from src.utils import host_alert


@pytest.fixture(autouse=True)
def _silent(monkeypatch):
    monkeypatch.setenv("HOST_ALERT_SILENT", "1")
    # 每个用例清空去抖状态，避免相互污染
    host_alert._last_alert.clear()
    yield
    host_alert._last_alert.clear()


class TestLooksLikeKeyFailure:
    def test_status_code_401_403(self):
        class E(Exception):
            status_code = 401
        assert host_alert.looks_like_key_failure(E()) is True

        class E2(Exception):
            status_code = 403
        assert host_alert.looks_like_key_failure(E2()) is True

    def test_response_status_code(self):
        class Resp:
            status_code = 401

        class E(Exception):
            response = Resp()
        assert host_alert.looks_like_key_failure(E()) is True

    @pytest.mark.parametrize("msg", [
        "Error: Unauthorized",
        "invalid api key provided",
        "Incorrect API key",
        "insufficient quota",
        "余额不足，请充值",
        "API key 已失效",
        "HTTP 403 Forbidden",
    ])
    def test_message_markers(self, msg):
        assert host_alert.looks_like_key_failure(Exception(msg)) is True

    @pytest.mark.parametrize("msg", [
        "timeout",
        "connection reset",
        "500 internal server error",
        "model not found",
    ])
    def test_non_key_failures(self, msg):
        assert host_alert.looks_like_key_failure(Exception(msg)) is False


class TestNotifyHost:
    def test_first_alert_fires(self):
        assert host_alert.notify_host("t", "m", key="k1") is True

    def test_debounce_within_cooldown(self):
        assert host_alert.notify_host("t", "m", key="k2", cooldown_sec=1800) is True
        # 冷却窗内第二次应被抑制
        assert host_alert.notify_host("t", "m", key="k2", cooldown_sec=1800) is False

    def test_different_keys_independent(self):
        assert host_alert.notify_host("t", "m", key="a") is True
        assert host_alert.notify_host("t", "m", key="b") is True

    def test_cooldown_zero_allows_repeat(self):
        assert host_alert.notify_host("t", "m", key="k3", cooldown_sec=0) is True
        assert host_alert.notify_host("t", "m", key="k3", cooldown_sec=0) is True

    def test_never_raises_on_bad_input(self):
        # 不抛异常即通过
        host_alert.notify_host(None, None, key=None)  # type: ignore

    def test_notify_key_failure_debounced_by_provider(self):
        assert host_alert.notify_key_failure("dashscope", "401") is True
        assert host_alert.notify_key_failure("dashscope", "401") is False
        assert host_alert.notify_key_failure("zhipu", "403") is True


class TestPopupSuppression:
    """弹窗只给算力提供方：桌面打包端（用户机）/显式静默环境不弹，但告警仍记录。"""

    def test_desktop_mode_suppresses_popup(self, monkeypatch):
        monkeypatch.delenv("HOST_ALERT_SILENT", raising=False)
        monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
        assert host_alert.popups_suppressed() is True
        # 静默只关弹窗，notify 仍按去抖语义返回 True（日志/EventBus 照常）
        assert host_alert.notify_host("t", "m", key="desk1") is True

    def test_host_alert_silent_suppresses_popup(self, monkeypatch):
        monkeypatch.setenv("HOST_ALERT_SILENT", "1")
        monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
        assert host_alert.popups_suppressed() is True

    def test_operator_machine_not_suppressed(self, monkeypatch):
        monkeypatch.delenv("HOST_ALERT_SILENT", raising=False)
        monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
        assert host_alert.popups_suppressed() is False


class TestEventBusMirror:
    def test_notify_mirrors_to_event_bus(self, monkeypatch):
        published = []
        from src.integrations.shared import event_bus as eb

        class _Bus:
            def publish(self, t, d): published.append((t, d))
        monkeypatch.setattr(eb, "get_event_bus", lambda: _Bus())

        assert host_alert.notify_host("云端 Key 异常", "detail", key="mirror1") is True
        assert published and published[0][0] == "host_alert"
        data = published[0][1]
        assert data["title"] == "云端 Key 异常"
        assert data["rate_key"] == "mirror1"

    def test_debounced_notify_does_not_mirror(self, monkeypatch):
        published = []
        from src.integrations.shared import event_bus as eb

        class _Bus:
            def publish(self, t, d): published.append((t, d))
        monkeypatch.setattr(eb, "get_event_bus", lambda: _Bus())

        host_alert.notify_host("t", "m", key="mirror2")
        host_alert.notify_host("t", "m", key="mirror2")  # 冷却窗内
        assert len(published) == 1

    def test_bus_failure_never_raises(self, monkeypatch):
        from src.integrations.shared import event_bus as eb

        def _boom():
            raise RuntimeError("bus down")
        monkeypatch.setattr(eb, "get_event_bus", _boom)
        assert host_alert.notify_host("t", "m", key="mirror3") is True


class TestConvenienceAlerts:
    def test_notify_cloud_outage_mentions_fallback_state(self, monkeypatch):
        seen = {}

        def _capture(title, message, *, key="", cooldown_sec=1800.0):
            seen[key] = (title, message)
            return True
        monkeypatch.setattr(host_alert, "notify_host", _capture)

        host_alert.notify_cloud_outage("deepseek-chat @ api.deepseek.com", "失败率 100%",
                                       fallback_ready=True)
        title, msg = seen["outage:deepseek-chat @ api.deepseek.com"]
        assert title == "云端 AI 不可达"
        assert "本地兜底模型已顶班" in msg

        host_alert.notify_cloud_outage("gemini", "x", fallback_ready=False)
        _, msg2 = seen["outage:gemini"]
        assert "未配置本地兜底" in msg2

    def test_notify_balance_low_message(self, monkeypatch):
        seen = {}

        def _capture(title, message, *, key="", cooldown_sec=1800.0):
            seen["v"] = (title, message, key, cooldown_sec)
            return True
        monkeypatch.setattr(host_alert, "notify_host", _capture)

        host_alert.notify_balance_low("DeepSeek", 12.34, 20, "CNY")
        title, msg, key, cd = seen["v"]
        assert title == "云端余额不足"
        assert "12.34" in msg and "20" in msg
        assert key == "balance:DeepSeek"
        assert cd == 21600.0

    def test_402_counts_as_key_failure(self):
        class E(Exception):
            status_code = 402
        assert host_alert.looks_like_key_failure(E()) is True
