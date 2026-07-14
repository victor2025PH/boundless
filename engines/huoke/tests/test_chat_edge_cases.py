# -*- coding: utf-8 -*-
"""Edge case and stress tests for chat intent parsing accuracy."""

import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.chat.ai_client import ChatAI

CFG = {
    "ai": {"api_key": ""},
    "device_aliases": {
        "01": "AIUKQ8WSKZBUQK4X",
        "02": "8D7DWWUKQGJRNN79",
        "03": "8XINBU4HNF9TSOXW",
        "04": "BMNJCMH69HVGAUMN",
    },
    "defaults": {"target_country": "italy", "warmup_duration": 30},
}


@pytest.fixture
def ai():
    return ChatAI(config=CFG)


class TestDeviceRecognition:
    """Test various ways users might refer to devices."""

    def test_01号(self, ai):
        assert "AIUKQ8WSKZBUQK4X" in ai._extract_devices("01号手机养号")

    def test_1号(self, ai):
        assert "AIUKQ8WSKZBUQK4X" in ai._extract_devices("1号手机")

    def test_phone_dash(self, ai):
        assert "AIUKQ8WSKZBUQK4X" in ai._extract_devices("phone-01养号")

    def test_multiple(self, ai):
        devs = ai._extract_devices("01号和02号一起养号")
        assert "AIUKQ8WSKZBUQK4X" in devs
        assert "8D7DWWUKQGJRNN79" in devs

    def test_all(self, ai):
        assert ai._extract_devices("所有手机") == ["all"]

    def test_全部(self, ai):
        assert ai._extract_devices("全部开始") == ["all"]

    def test_no_device(self, ai):
        assert ai._extract_devices("养号") == []

    def test_手机加号(self, ai):
        assert "8XINBU4HNF9TSOXW" in ai._extract_devices("手机03号")


class TestDurationExtraction:
    """Test duration parsing from various expressions."""

    def test_分钟(self, ai):
        assert ai._extract_duration("养号30分钟") == 30

    def test_min(self, ai):
        assert ai._extract_duration("warmup 45min") == 45

    def test_小时(self, ai):
        assert ai._extract_duration("养号2小时") == 120

    def test_no_duration(self, ai):
        assert ai._extract_duration("开始养号") is None


class TestCountryExtraction:
    """Test country name parsing."""

    def test_意大利(self, ai):
        assert ai._extract_country("目标意大利") == "italy"

    def test_germany(self, ai):
        assert ai._extract_country("目标德国") == "germany"

    def test_france(self, ai):
        assert ai._extract_country("法国用户") == "france"

    def test_spain(self, ai):
        assert ai._extract_country("西班牙市场") == "spain"

    def test_default(self, ai):
        assert ai._extract_country("开始养号") == "italy"


class TestAmbiguousInputs:
    """Test inputs that could match multiple intents."""

    def test_消息_vs_发消息(self, ai):
        r1 = ai.parse_intent("查看消息")
        r2 = ai.parse_intent("发消息给他")
        assert r1["intent"] == "check_inbox"
        assert r2["intent"] == "send_dm"

    def test_vpn_stop_vs_status(self, ai):
        r1 = ai.parse_intent("VPN状态")
        r2 = ai.parse_intent("停掉VPN")
        assert r1["intent"] == "vpn_status"
        assert r2["intent"] == "vpn_stop"

    def test_follow_test_vs_follow(self, ai):
        r1 = ai.parse_intent("测试关注能力")
        r2 = ai.parse_intent("开始关注")
        assert r1["intent"] == "test_follow"
        assert r2["intent"] == "follow"

    def test_stats_vs_leads(self, ai):
        r1 = ai.parse_intent("今天的数据")
        r2 = ai.parse_intent("线索数据")
        assert r1["intent"] == "stats"
        assert r2["intent"] == "leads"


class TestColloquialInputs:
    """Test informal/colloquial Chinese expressions."""

    def test_informal_warmup(self, ai):
        r = ai.parse_intent("01号刷视频")
        assert r["intent"] == "warmup"

    def test_informal_device_check(self, ai):
        r = ai.parse_intent("手机哪些在线")
        assert r["intent"] == "device_list"

    def test_informal_stop(self, ai):
        r = ai.parse_intent("紧急停止")
        assert r["intent"] == "stop_all"

    def test_short_warmup(self, ai):
        r = ai.parse_intent("养号")
        assert r["intent"] == "warmup"


class TestReplyGeneration:
    """Test reply text generation."""

    def test_help_reply(self, ai):
        r = ai.generate_reply(
            {"intent": "help", "devices": [], "params": {}}, [], "帮助")
        assert "养号" in r
        assert "VPN" in r

    def test_task_reply(self, ai):
        r = ai.generate_reply(
            {"intent": "warmup", "devices": ["D1"], "params": {}},
            [{"action": "warmup", "task_id": "abc123def456"}],
            "养号")
        assert "abc123" in r

    def test_error_reply(self, ai):
        r = ai.generate_reply(
            {"intent": "warmup", "devices": ["D1"], "params": {}},
            [{"action": "warmup", "error": "设备离线"}],
            "养号")
        assert "失败" in r

    def test_data_reply(self, ai):
        r = ai.generate_reply(
            {"intent": "health", "devices": [], "params": {}},
            [{"action": "health", "data": {"status": "ok", "devices": 5}}],
            "健康")
        assert "ok" in r or "status" in r
