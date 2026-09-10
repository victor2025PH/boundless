"""多模型路由（ai.models + ai.task_routes）解析与 resolve_route 契约（无网络）。

覆盖：档解析 + /v1 归一化、任务→档映射、默认关（不配=零路由）、
指向不存在档=静默忽略、resolve_route(None)=None。
"""
from src.ai.ai_client import AIClient


class _Cfg:
    def __init__(self, d):
        self.config = d
        self.config_path = None

    def get_ai_config(self):
        return self.config.get("ai", {})


def _client(ai):
    return AIClient(_Cfg({"ai": ai}))


def test_routes_parse_and_resolve():
    ai = {
        "provider": "openai_compatible",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "api_key": "sk-primary",
        "models": {
            "planner_local": {"base_url": "http://192.168.0.173:8001", "model": "chatx"},
            "qa_cheap": {"base_url": "https://api.deepseek.com", "model": "deepseek-v4-flash"},
        },
        "task_routes": {"assistant_planner": "planner_local", "assistant_qa": "qa_cheap"},
    }
    c = _client(ai)
    c._build_route_clients(ai, ai["api_key"])
    assert set(c._route_clients.keys()) == {"planner_local", "qa_cheap"}

    prof = c.resolve_route("assistant_planner")
    assert prof is not None and prof["model"] == "chatx"
    # base_url 归一化补 /v1
    assert str(prof["client"].base_url).rstrip("/").endswith("192.168.0.173:8001/v1")

    # 官方主机上的退役别名归一到现役 deepseek-flash，且按主机关思维链
    assert c.resolve_route("assistant_qa")["model"] == "deepseek-flash"
    assert c.resolve_route("assistant_qa")["extra_body"] == {"thinking": {"type": "disabled"}}
    assert prof["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


def test_default_off_no_models():
    c = _client({"provider": "openai_compatible", "model": "m", "api_key": "k"})
    c._build_route_clients({}, "k")
    assert c._route_clients == {}
    assert c._task_routes == {}
    assert c.resolve_route("assistant_planner") is None
    assert c.resolve_route(None) is None


def test_route_to_missing_profile_ignored():
    ai = {
        "model": "m", "api_key": "k",
        "models": {"a": {"base_url": "http://h:1", "model": "x"}},
        "task_routes": {"assistant_planner": "does_not_exist"},
    }
    c = _client(ai)
    c._build_route_clients(ai, "k")
    assert "assistant_planner" not in c._task_routes
    assert c.resolve_route("assistant_planner") is None


def test_local_endpoint_key_placeholder_becomes_ollama():
    ai = {
        "model": "m", "api_key": "YOUR_API_KEY",
        "models": {"local": {"base_url": "http://127.0.0.1:11434", "model": "qwen"}},
        "task_routes": {"chat": "local"},
    }
    c = _client(ai)
    c._build_route_clients(ai, ai["api_key"])
    prof = c.resolve_route("chat")
    assert prof is not None and prof["model"] == "qwen"
    # 占位主 key 不外泄到本地端点，改用 'ollama' 占位
    assert c._route_clients["local"]["client"].api_key == "ollama"
