# -*- coding: utf-8 -*-
"""Tests for the Chat Control module — intent parsing and execution."""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _reset_chat_preflight_yaml_cache():
    yield
    import src.chat.controller as mod

    mod._chat_preflight_yaml_cache = None


from src.chat.ai_client import ChatAI
from src.chat.intent_executor import IntentExecutor
from src.chat.controller import ChatController
from src.chat.triage import ChatRoute, triage_message
from src.chat.converse_reply import generate_converse_reply
from src.chat.unified_parse import normalize_unified_payload


# ══════════════════════════════════════════════════════
# ChatAI — local fallback parser tests
# ══════════════════════════════════════════════════════

@pytest.fixture
def ai():
    cfg = {
        "ai": {"api_key": ""},
        "device_aliases": {
            "01": "AIUKQ8WSKZBUQK4X",
            "02": "8D7DWWUKQGJRNN79",
            "03": "8XINBU4HNF9TSOXW",
        },
        "defaults": {
            "target_country": "italy",
            "warmup_duration": 30,
        },
    }
    return ChatAI(config=cfg)


class TestUnifiedPayload:
    def test_normalize_query(self):
        r = normalize_unified_payload({
            "schema_version": 1,
            "routing": "query",
            "query_subtype": "device_list",
            "intent": "help",
            "devices": [],
            "params": {},
            "targeting": {},
            "goals": {},
            "multi_task": False,
            "intents": [],
        })
        assert r["routing"] == "query"
        assert r["query_subtype"] == "device_list"

    def test_normalize_rejects_bad_routing(self):
        assert normalize_unified_payload({"schema_version": 1, "routing": "nope"}) is None


class TestTriage:
    """消息分流：查询 / 执行 / 闲聊。"""

    def test_query_main_control_devices(self):
        r = triage_message("现在主控有几个电脑在线")
        assert r.route == ChatRoute.QUERY
        assert r.query_subtype in ("device_list", "general")

    def test_execute_warmup(self):
        r = triage_message("01号手机养号30分钟")
        assert r.route == ChatRoute.EXECUTE

    def test_greeting_converse(self):
        r = triage_message("你好")
        assert r.route == ChatRoute.CONVERSE

    def test_zai_ma_converse(self):
        r = triage_message("在吗")
        assert r.route == ChatRoute.CONVERSE

    def test_howto_converse(self):
        r = triage_message("怎么用AI指令控制台")
        assert r.route == ChatRoute.CONVERSE

    def test_zai_ma_reply_not_help(self):
        text = generate_converse_reply("在吗")
        assert "在的" in text
        assert "完整能力列表" not in text


class TestChatControllerQueryPath:
    """QUERY 分流走只读 API，不调用 parse_intent。"""

    def test_query_device_count(self):
        ex = MagicMock()
        ex.execute.return_value = [
            {"action": "device_list", "data": [
                {"device_id": "A", "status": "online"},
                {"device_id": "B", "status": "offline"},
            ]},
        ]
        ai = MagicMock()
        ctrl = ChatController(ai=ai, executor=ex)
        r = ctrl.handle("主控现在有几台手机在线")
        assert r.get("chat_mode") == "query"
        assert r.get("intent") == "device_list"
        assert "2" in r.get("reply", "") or "共" in r.get("reply", "")
        ai.parse_intent.assert_not_called()


class TestLocalParser:

    def test_warmup_with_device_and_duration(self, ai):
        r = ai.parse_intent("01号手机养号30分钟")
        assert r["intent"] == "warmup"
        assert "AIUKQ8WSKZBUQK4X" in r["devices"]
        assert r["params"]["duration_minutes"] == 30

    def test_warmup_all_devices(self, ai):
        r = ai.parse_intent("所有手机开始养号")
        assert r["intent"] == "warmup"
        assert r["devices"] == ["all"]

    def test_warmup_default_duration(self, ai):
        r = ai.parse_intent("01号养号")
        assert r["intent"] == "warmup"
        assert r["params"]["duration_minutes"] == 30

    def test_test_follow(self, ai):
        r = ai.parse_intent("测试01能不能关注")
        assert r["intent"] == "test_follow"
        assert "AIUKQ8WSKZBUQK4X" in r["devices"]

    def test_follow_philippines_country(self, ai):
        r = ai.parse_intent("01号菲律宾关注")
        assert r["intent"] == "follow"
        assert r["params"].get("target_country") == "philippines"

    def test_follow(self, ai):
        r = ai.parse_intent("01号开始关注")
        assert r["intent"] == "follow"
        assert "AIUKQ8WSKZBUQK4X" in r["devices"]

    def test_check_inbox(self, ai):
        r = ai.parse_intent("查看01收件箱")
        assert r["intent"] == "check_inbox"

    def test_vpn_status(self, ai):
        r = ai.parse_intent("VPN状态")
        assert r["intent"] == "vpn_status"

    def test_vpn_setup(self, ai):
        r = ai.parse_intent("配置VPN")
        assert r["intent"] == "vpn_setup"

    def test_vpn_stop(self, ai):
        r = ai.parse_intent("停掉01的VPN")
        assert r["intent"] == "vpn_stop"

    def test_device_list(self, ai):
        r = ai.parse_intent("哪些手机在线")
        assert r["intent"] == "device_list"

    def test_stats(self, ai):
        r = ai.parse_intent("今天数据怎么样")
        assert r["intent"] == "stats"

    def test_health(self, ai):
        r = ai.parse_intent("有没有手机掉线")
        assert r["intent"] == "health"

    def test_risk(self, ai):
        r = ai.parse_intent("01号风险等级多少")
        assert r["intent"] == "risk"
        assert "AIUKQ8WSKZBUQK4X" in r["devices"]

    def test_schedule(self, ai):
        r = ai.parse_intent("定时任务有哪些")
        assert r["intent"] == "schedule_list"

    def test_geo_check(self, ai):
        r = ai.parse_intent("01号IP在哪")
        assert r["intent"] == "geo_check"

    def test_leads(self, ai):
        r = ai.parse_intent("CRM数据")
        assert r["intent"] == "leads"

    def test_stop_all(self, ai):
        r = ai.parse_intent("全部停止")
        assert r["intent"] == "stop_all"

    def test_help(self, ai):
        r = ai.parse_intent("帮助")
        assert r["intent"] == "help"

    def test_device_number_without_zero(self, ai):
        r = ai.parse_intent("1号手机养号")
        assert r["intent"] == "warmup"
        assert "AIUKQ8WSKZBUQK4X" in r["devices"]

    def test_country_extraction(self, ai):
        r = ai.parse_intent("目标德国，01号养号")
        assert r["params"]["target_country"] == "germany"

    def test_multiple_devices(self, ai):
        r = ai.parse_intent("01号和02号养号")
        assert r["intent"] == "warmup"
        assert len(r["devices"]) == 2

    def test_send_dm(self, ai):
        r = ai.parse_intent("给回关的人发消息")
        assert r["intent"] == "send_dm"

    def test_help_text(self, ai):
        text = ai._help_text()
        assert "养号" in text
        assert "VPN" in text

    def test_multi_intent_chat_routes_to_inbox(self, ai):
        """泛化「聊天」应走收件箱+自动回复，避免无 recipient 的 send_dm。"""
        r = ai._multi_intent_parse("01号关注，再聊天")
        assert r.get("intent") == "multi_task"
        acts = [x["action"] for x in r.get("intents", [])]
        assert "tiktok_check_inbox" in acts
        assert "tiktok_chat" not in acts

    def test_explicit_at_clause_prefers_send_dm_action(self, ai):
        """含 @ / 私信给 → 仍用 tiktok_chat（映射 send_dm），与泛化「聊天」区分。"""
        r = ai._multi_intent_parse("私信给@someone")
        assert r.get("intent") == "tiktok_chat"


# ══════════════════════════════════════════════════════
# IntentExecutor tests (mocked HTTP)
# ══════════════════════════════════════════════════════

class TestIntentExecutor:

    def test_warmup_creates_task(self):
        executor = IntentExecutor(api_url="http://localhost:9999")
        with patch.object(executor, "_post",
                          return_value={"task_id": "abc123"}):
            results = executor.execute("warmup", ["DEV1"],
                                       {"duration_minutes": 30})
            assert len(results) == 1
            assert results[0]["task_id"] == "abc123"

    def test_warmup_all_devices(self):
        executor = IntentExecutor(api_url="http://localhost:9999")
        mock_devices = [
            {"device_id": "DEV1", "status": "connected", "busy": False},
            {"device_id": "DEV2", "status": "connected", "busy": False},
        ]
        batch_resp = {"batch_id": "abc123", "task_ids": ["t1", "t2"], "count": 2}
        with patch.object(executor, "_get", return_value=mock_devices), \
             patch.object(executor, "_post",
                          return_value=batch_resp) as mock_post:
            results = executor.execute("warmup", ["all"],
                                       {"duration_minutes": 30})
            assert len(results) == 1
            assert results[0].get("batch_id") == "abc123"
            assert results[0].get("count") == 2
            mock_post.assert_called_once()

    def test_device_list(self):
        executor = IntentExecutor(api_url="http://localhost:9999")
        mock_devices = [
            {"device_id": "A", "display_name": "Phone-1", "status": "online"},
            {"device_id": "B", "display_name": "Phone-2", "status": "offline"},
        ]
        with patch.object(executor, "_get", return_value=mock_devices):
            results = executor.execute("device_list", [], {})
            assert results[0]["data"] == mock_devices

    def test_health(self):
        executor = IntentExecutor(api_url="http://localhost:9999")
        with patch.object(executor, "_get",
                          return_value={"status": "ok", "devices_online": 5}):
            results = executor.execute("health", [], {})
            assert results[0]["data"]["status"] == "ok"

    def test_unknown_intent(self):
        executor = IntentExecutor()
        results = executor.execute("unknown_xyz", [], {})
        assert results[0].get("error")

    def test_task_name_alias_tiktok_follow(self):
        """_multi_intent_parse 使用 tiktok_follow 等 task 名，须映射到 _do_follow。"""
        executor = IntentExecutor(api_url="http://localhost:9999")
        with patch.object(executor, "_post",
                          return_value={"task_id": "tid"}):
            results = executor.execute("tiktok_follow", ["DEV1"],
                                       {"max_follows": 10, "country": "italy"})
            assert not results[0].get("error")

    def test_send_dm_requires_recipient(self):
        ex = IntentExecutor(api_url="http://localhost:9999")
        r = ex.execute("send_dm", ["DEV1"], {"target_country": "italy"})
        assert r[0].get("error")

    def test_help(self):
        executor = IntentExecutor()
        results = executor.execute("help", [], {})
        assert results[0]["action"] == "help"


# ══════════════════════════════════════════════════════
# ChatController integration tests (mocked)
# ══════════════════════════════════════════════════════

class TestChatController:

    def test_full_flow(self):
        ai = MagicMock()
        ai._multi_intent_parse.return_value = {}
        ai.parse_intent.return_value = {
            "intent": "warmup",
            "devices": ["DEV1"],
            "params": {"duration_minutes": 30},
        }
        ai.generate_reply.return_value = "已创建养号任务"

        executor = MagicMock()
        executor.execute.return_value = [
            {"action": "tiktok_warmup", "task_id": "task123", "device": "DEV1"},
        ]

        ctrl = ChatController(ai=ai, executor=executor)
        result = ctrl.handle("01号手机养号30分钟")

        assert result["reply"] == "已创建养号任务"
        assert result["intent"] == "warmup"
        assert result["task_ids"] == ["task123"]
        assert result["elapsed_ms"] >= 0

    def test_empty_message(self):
        ctrl = ChatController(ai=MagicMock(), executor=MagicMock())
        result = ctrl.handle("")
        assert "请输入" in result["reply"]

    def test_help_flow(self):
        ai_mock = MagicMock()
        ai_mock._multi_intent_parse.return_value = {}
        ai_mock.parse_intent.return_value = {
            "intent": "help", "devices": [], "params": {},
        }
        ai_mock.generate_reply.return_value = "帮助信息..."

        ctrl = ChatController(ai=ai_mock, executor=MagicMock())
        result = ctrl.handle("帮助")
        assert result["intent"] == "help"

    def test_history_tracking(self):
        ai_mock = MagicMock()
        ai_mock._multi_intent_parse.return_value = {}
        ai_mock.parse_intent.return_value = {
            "intent": "help", "devices": [], "params": {},
        }
        ai_mock.generate_reply.return_value = "ok"

        ctrl = ChatController(ai=ai_mock, executor=MagicMock())
        ctrl.handle("test1")
        ctrl.handle("test2")
        assert len(ctrl.history) == 4

    def test_clear(self):
        ai_mock = MagicMock()
        ai_mock._multi_intent_parse.return_value = {}
        ai_mock.parse_intent.return_value = {
            "intent": "help", "devices": [], "params": {},
        }
        ai_mock.generate_reply.return_value = "ok"

        ctrl = ChatController(ai=ai_mock, executor=MagicMock())
        ctrl.handle("test")
        ctrl.clear()
        assert len(ctrl.history) == 0


class TestChatPreflightMode:
    """OPENCLAW_CHAT_PREFLIGHT_MODE 与 yaml 缓存交互。"""

    def test_env_full_overrides_yaml_none(self, monkeypatch):
        import src.chat.controller as mod

        monkeypatch.setenv("OPENCLAW_CHAT_PREFLIGHT_MODE", "full")
        mod._chat_preflight_yaml_cache = {"mode": "none"}
        assert mod._chat_preflight_mode() == "full"

    def test_invalid_env_falls_back_to_yaml(self, monkeypatch):
        import src.chat.controller as mod

        monkeypatch.setenv("OPENCLAW_CHAT_PREFLIGHT_MODE", "bogus")
        mod._chat_preflight_yaml_cache = {"mode": "full"}
        assert mod._chat_preflight_mode() == "full"

    def test_yaml_used_when_env_empty(self, monkeypatch):
        import src.chat.controller as mod

        monkeypatch.delenv("OPENCLAW_CHAT_PREFLIGHT_MODE", raising=False)
        mod._chat_preflight_yaml_cache = {"mode": "network_only"}
        assert mod._chat_preflight_mode() == "network_only"
