"""Vision Ollama→智谱 回退 + 多端点双活：纯配置/失效切换单测（无网络）。"""

import pytest

import src.vision_client as vc_mod
from src.vision_client import (
    VisionClient,
    _vision_base_urls,
    has_any_vision_backend,
    _wants_openai_primary,
)


def test_wants_openai_primary():
    assert _wants_openai_primary(
        {"provider": "openai_compatible", "base_url": "http://127.0.0.1:11434/v1"}
    )
    assert not _wants_openai_primary({"provider": "openai_compatible"})
    assert not _wants_openai_primary({"provider": "zhipu", "api_key": "x"})


def test_wants_openai_primary_via_base_urls_list():
    assert _wants_openai_primary(
        {"provider": "openai_compatible", "base_urls": ["http://a:11434"]}
    )
    assert not _wants_openai_primary({"provider": "openai_compatible", "base_urls": []})


def test_vision_base_urls_parsing_dedup_and_v1():
    cfg = {
        "base_urls": ["http://a:11434", "http://b:11434/v1/"],
        "base_url": "http://a:11434/v1",  # 与列表首项重复 → 去重
    }
    assert _vision_base_urls(cfg) == ["http://a:11434/v1", "http://b:11434/v1"]
    # 逗号串形式
    assert _vision_base_urls({"base_urls": "http://a:1, http://b:2/v1"}) == [
        "http://a:1/v1",
        "http://b:2/v1",
    ]
    assert _vision_base_urls({}) == []


def test_has_backend_ollama_url():
    assert has_any_vision_backend(
        {"provider": "openai_compatible", "base_url": "http://127.0.0.1:11434/v1"},
        {},
    )


def test_has_backend_zhipu_key():
    assert has_any_vision_backend({"api_key": "not-ollama-real"}, {})


def test_has_backend_zhipu_api_key_field():
    assert has_any_vision_backend({"api_key": "ollama", "zhipu_api_key": "zk"}, {})


def test_has_backend_neither():
    assert not has_any_vision_backend({"provider": "openai_compatible"}, {})
    assert not has_any_vision_backend({"api_key": "ollama"}, {})


# ---------------------------------------------------------------------------
# 多端点双活：失效切换 / 冷却降权 / 空答不换端点
# ---------------------------------------------------------------------------


class _FakeMsg:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMsg(content)


class _FakeResp:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeOpenAI:
    """按 base_url 决定行为：behaviors[url] = Exception 实例(抛) / str(返回) / None(空答)。"""

    behaviors: dict = {}
    calls: list = []
    models: list = []  # (url, model) 逐调用记录——每端点模型契约（实施71）的断言面
    timeouts: list = []  # (url, timeout) 逐**构造**记录——每端点超时契约的断言面

    def __init__(self, api_key=None, base_url=None, timeout=None, **kw):
        self._url = base_url
        _FakeOpenAI.timeouts.append((base_url, timeout))
        outer = self

        class _Completions:
            def create(self, **kw):
                _FakeOpenAI.calls.append(outer._url)
                _FakeOpenAI.models.append((outer._url, kw.get("model")))
                b = _FakeOpenAI.behaviors.get(outer._url)
                if isinstance(b, Exception):
                    raise b
                return _FakeResp(b)

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


@pytest.fixture
def _fake_openai(monkeypatch):
    monkeypatch.setattr(vc_mod, "OpenAI", _FakeOpenAI)
    monkeypatch.setattr(vc_mod, "OPENAI_SYNC_AVAILABLE", True)
    monkeypatch.setattr(
        vc_mod, "_image_to_data_url", lambda *a, **k: "data:image/jpeg;base64,x"
    )
    _FakeOpenAI.behaviors = {}
    _FakeOpenAI.calls = []
    _FakeOpenAI.models = []
    _FakeOpenAI.timeouts = []
    vc_mod._URL_BAD_UNTIL.clear()
    yield
    vc_mod._URL_BAD_UNTIL.clear()


def _mk_client(urls):
    c = VisionClient(
        {"provider": "openai_compatible", "base_urls": list(urls), "model": "vlm"}
    )
    assert c.initialize()
    return c


def test_failover_to_second_endpoint(_fake_openai):
    _FakeOpenAI.behaviors = {
        "http://a:1/v1": RuntimeError("conn refused"),
        "http://b:2/v1": "描述文本",
    }
    c = _mk_client(["http://a:1", "http://b:2"])
    assert c._describe_openai_sync("fake.jpg") == "描述文本"
    assert _FakeOpenAI.calls == ["http://a:1/v1", "http://b:2/v1"]


def test_cooldown_reorders_next_instance(_fake_openai):
    """a 失败进冷却 → 新实例(模拟下一次图片调用)应先试 b。"""
    _FakeOpenAI.behaviors = {
        "http://a:1/v1": RuntimeError("down"),
        "http://b:2/v1": "ok1",
    }
    _mk_client(["http://a:1", "http://b:2"])._describe_openai_sync("f.jpg")
    _FakeOpenAI.calls = []
    _FakeOpenAI.behaviors["http://a:1/v1"] = "a-alive"  # 即便 a 恢复,冷却期内仍应殿后
    out = _mk_client(["http://a:1", "http://b:2"])._describe_openai_sync("f.jpg")
    assert out == "ok1"
    assert _FakeOpenAI.calls == ["http://b:2/v1"]


def test_all_cooling_still_hard_tries(_fake_openai):
    """全端点冷却时不弃疗：仍按序硬试。"""
    _FakeOpenAI.behaviors = {"http://a:1/v1": RuntimeError("down"), "http://b:2/v1": RuntimeError("down")}
    c = _mk_client(["http://a:1", "http://b:2"])
    assert c._describe_openai_sync("f.jpg") is None  # 双双失败,均进冷却
    _FakeOpenAI.behaviors["http://a:1/v1"] = "recovered"
    _FakeOpenAI.calls = []
    assert _mk_client(["http://a:1", "http://b:2"])._describe_openai_sync("f.jpg") == "recovered"
    assert _FakeOpenAI.calls[0] == "http://a:1/v1"


# ---------------------------------------------------------------------------
# 每端点模型（实施71 2026-08-27）：混合供应商双活（云主+LAN 备）唯一阻塞=两家模型命名不同
# ---------------------------------------------------------------------------


def test_endpoint_model_fragment_match_and_fallback():
    from src.vision_client import _endpoint_model

    cfg = {
        "model": "Qwen/Qwen3-VL-8B-Instruct",
        "endpoint_models": {
            "siliconflow": "Qwen/Qwen3-VL-8B-Instruct",
            "192.168.0.176": "qwen3-vl:8b-instruct",
        },
    }
    assert _endpoint_model(cfg, "https://api.siliconflow.cn/v1") == "Qwen/Qwen3-VL-8B-Instruct"
    assert _endpoint_model(cfg, "http://192.168.0.176:11434/v1") == "qwen3-vl:8b-instruct"
    # 未命中片段 → 回落全局 model；无任何配置 → default
    assert _endpoint_model(cfg, "http://other:8000/v1") == "Qwen/Qwen3-VL-8B-Instruct"
    assert _endpoint_model({}, "http://x/v1") == "llava"
    # 脏值防御：非 dict / 空片段 / 空模型名一律忽略
    assert _endpoint_model({"endpoint_models": "junk", "model": "m1"}, "u") == "m1"
    assert _endpoint_model({"endpoint_models": {"": "x", "u": ""}, "model": "m1"}, "u") == "m1"


def test_failover_uses_per_endpoint_model(_fake_openai):
    """云主挂 → 切 LAN 备时必须换成 LAN 的模型名（全局单 model 会 404 在备端点上）。"""
    _FakeOpenAI.behaviors = {
        "https://cloud.example/v1": RuntimeError("cloud down"),
        "http://192.168.0.176:11434/v1": "备胎描述",
    }
    c = VisionClient({
        "provider": "openai_compatible",
        "base_urls": ["https://cloud.example", "http://192.168.0.176:11434"],
        "model": "Cloud/VL-Model",
        "endpoint_models": {
            "cloud.example": "Cloud/VL-Model",
            "192.168.0.176": "qwen3-vl:8b-instruct",
        },
    })
    assert c.initialize()
    assert c._describe_openai_sync("fake.jpg") == "备胎描述"
    assert _FakeOpenAI.models == [
        ("https://cloud.example/v1", "Cloud/VL-Model"),
        ("http://192.168.0.176:11434/v1", "qwen3-vl:8b-instruct"),
    ]


# ---------------------------------------------------------------------------
# 每端点超时（2026-08-27）：「5 秒没响应就切下一个」，但云端备胎正常就要 6~9 秒
# ---------------------------------------------------------------------------


def test_endpoint_timeout_fragment_match_and_fallback():
    from src.vision_client import _endpoint_timeout

    cfg = {"timeout": 150, "endpoint_timeouts": {"192.168.0.176": 5}}
    assert _endpoint_timeout(cfg, "http://192.168.0.176:11434/v1") == 5.0
    # 未命中片段 → 回落全局 timeout（云端要留足）
    assert _endpoint_timeout(cfg, "https://api.siliconflow.cn/v1") == 150.0
    # 无任何配置 → default
    assert _endpoint_timeout({}, "http://x/v1", 120.0) == 120.0
    # 脏值防御：非 dict / 空片段 / 非数字 / 非正数一律忽略，回落全局
    assert _endpoint_timeout({"endpoint_timeouts": "junk", "timeout": 30}, "u") == 30.0
    assert _endpoint_timeout(
        {"endpoint_timeouts": {"": 5, "u": "abc"}, "timeout": 30}, "u") == 30.0
    assert _endpoint_timeout({"endpoint_timeouts": {"u": 0}, "timeout": 30}, "u") == 30.0


def test_per_endpoint_timeout_is_applied_at_client_construction(_fake_openai):
    """主路 5s 快切、备胎保留长超时——**必须逐端点**。

    这条是「快速失败」与「有地方可退」的分界：全局砍到 5s 主路如愿快切，但云端备胎
    正常就要 6.6s（降级时 8.8s+），会 100% 超时，等于把兜底整条废掉。
    """
    VisionClient({
        "provider": "openai_compatible",
        "base_urls": ["http://192.168.0.176:11434", "https://api.siliconflow.cn"],
        "model": "m",
        "timeout": 150,
        "endpoint_timeouts": {"192.168.0.176": 5},
    }).initialize()

    got = {u: t for u, t in _FakeOpenAI.timeouts}
    lan = got["http://192.168.0.176:11434/v1"]
    cloud = got["https://api.siliconflow.cn/v1"]
    # httpx.Timeout 有 .read；退化路径是裸 float——两种都要能断言
    assert float(getattr(lan, "read", lan)) == 5.0
    assert float(getattr(cloud, "read", cloud)) == 150.0
    # 连接超时不得超过读超时（5s 端点上连接不能还等 5s 以上）
    assert float(getattr(lan, "connect", 5.0)) <= 5.0


def test_empty_answer_does_not_failover(_fake_openai):
    """端点通但空答 → **默认**（vision.empty_retry 关）保持旧语义返回 None,不烧第二块 GPU。

    开 empty_retry 后仅入站识图链会换 1 个端点重试,见 tests/test_vision_empty_failover.py。
    """
    _FakeOpenAI.behaviors = {"http://a:1/v1": None, "http://b:2/v1": "should-not-run"}
    c = _mk_client(["http://a:1", "http://b:2"])
    assert c._describe_openai_sync("f.jpg") is None
    assert _FakeOpenAI.calls == ["http://a:1/v1"]
    assert not vc_mod._URL_BAD_UNTIL  # 空答不算端点故障
