"""云端 Key 备用池（ai.key_pool，2026-07-12）门禁。

降级链不变量：主 Key(2 次) → 备用池(逐 key 一次，失败 120s 冷却) → 本地兜底 → canned。
覆盖：配置解析（继承/去重/占位跳过）、运行时接管（messages 保留 + 提醒 + 统计）、
池 key 自身失效告警、熔断开路先池后本地、冷却跳过。本文件不触网（假客户端注入）。
"""

import asyncio
import time

from src.ai.ai_client import AIClient
from src.utils import host_alert


class _Cfg:
    config_path = None
    config = {"web_admin": {"site_name": "T"}, "ai": {}}

    def get_ai_config(self):
        return {}


class _Msg:
    def __init__(self, content):
        self.content = content
        self.model_extra = {}


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = _Usage()


class _FakeChatClient:
    """AsyncOpenAI 替身：fail 传异常则恒抛；reply=None 回空答。"""

    def __init__(self, *, fail: Exception | None = None, reply: str | None = "ok",
                 base_url: str = "https://api.deepseek.com/v1"):
        self.fail = fail
        self.reply = reply
        self.base_url = base_url
        self.calls = 0
        outer = self

        class _Completions:
            async def create(self, **kw):
                outer.calls += 1
                outer.last_kw = kw
                if outer.fail is not None:
                    raise outer.fail
                return _Resp(outer.reply)

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def _client(primary: _FakeChatClient) -> AIClient:
    c = AIClient(_Cfg())
    c._use_openai_compat = True
    c._oa_client = primary
    c.model = "deepseek-chat"
    c.timeout = 5
    c._cb_enabled = False
    return c


def _pool_entry(name: str, fake: _FakeChatClient, model: str = "deepseek-chat") -> dict:
    return {"name": name, "client": fake, "model": model,
            "label": f"{model} @ pool ({name})", "bad_until": 0.0}


def _silence_notifications(monkeypatch):
    seen = {"keyfail": [], "takeover": [], "outage": []}
    monkeypatch.setattr(host_alert, "notify_key_failure",
                        lambda p, d="", **kw: seen["keyfail"].append((p, d)) or True)
    monkeypatch.setattr(host_alert, "notify_cloud_outage",
                        lambda p, d="", **kw: seen["outage"].append(p) or True)

    def _fake_notify(title, message, *, key="", cooldown_sec=1800.0):
        seen["takeover"].append((title, key))
        return True
    monkeypatch.setattr(host_alert, "notify_host", _fake_notify)
    return seen


# ── 配置解析 ─────────────────────────────────────────────────────


def _parse_pool(monkeypatch, ai_cfg: dict) -> AIClient:
    built = []

    class _FakeAsyncOpenAI:
        def __init__(self, **kw):
            built.append(kw)
            self.base_url = kw.get("base_url")

        class chat:  # noqa: N801 - 仅占位，初始化路径不真调
            class completions:
                @staticmethod
                async def create(**kw):
                    return _Resp("hi")

    import src.ai.ai_client as mod
    monkeypatch.setattr(mod, "AsyncOpenAI", _FakeAsyncOpenAI)
    monkeypatch.setattr(mod, "OPENAI_SDK_AVAILABLE", True)
    c = AIClient(_Cfg())
    c.timeout = 5
    c.model = "deepseek-chat"

    async def _ok():
        return True
    monkeypatch.setattr(c, "_test_openai_connection", _ok)
    ok = asyncio.run(c._initialize_openai_compatible(ai_cfg, ai_cfg.get("api_key")))
    assert ok is True
    return c


def test_pool_parse_inherits_base_and_model(monkeypatch):
    c = _parse_pool(monkeypatch, {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-main",
        "key_pool": {"enabled": True, "keys": [
            {"name": "ds2", "api_key": "sk-backup"},
        ]},
    })
    assert len(c._pool_entries) == 1
    e = c._pool_entries[0]
    assert e["name"] == "ds2"
    assert e["model"] == "deepseek-chat"
    assert "api.deepseek.com" in e["label"] and "(ds2)" in e["label"]


def test_pool_parse_skips_placeholder_dupe_and_primary(monkeypatch):
    c = _parse_pool(monkeypatch, {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-main",
        "key_pool": {"enabled": True, "keys": [
            {"name": "empty", "api_key": ""},                      # 空 → 跳过
            {"name": "ph", "api_key": "YOUR_BACKUP_KEY"},          # 占位 → 跳过
            {"name": "same-as-primary", "api_key": "sk-main"},     # 与主 Key 相同 → 跳过
            {"name": "b1", "api_key": "sk-b"},
            {"name": "b1-dupe", "api_key": "sk-b"},                # 池内重复 → 跳过
            {"name": "other-vendor", "api_key": "sk-z",
             "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
        ]},
    })
    names = [e["name"] for e in c._pool_entries]
    assert names == ["b1", "other-vendor"]
    assert c._pool_entries[1]["model"] == "glm-4-flash"


def test_pool_disabled_or_absent(monkeypatch):
    c1 = _parse_pool(monkeypatch, {
        "base_url": "https://api.deepseek.com/v1", "api_key": "sk-main",
        "key_pool": {"enabled": False, "keys": [{"name": "x", "api_key": "sk-b"}]},
    })
    assert c1._pool_entries == []
    c2 = _parse_pool(monkeypatch, {
        "base_url": "https://api.deepseek.com/v1", "api_key": "sk-main",
    })
    assert c2._pool_entries == []


def test_get_stats_exposes_pool(monkeypatch):
    c = _parse_pool(monkeypatch, {
        "base_url": "https://api.deepseek.com/v1", "api_key": "sk-main",
        "key_pool": {"keys": [{"name": "ds2", "api_key": "sk-b"}]},
    })
    st = c.get_stats()
    assert st["key_pool_size"] == 1
    assert st["key_pool_calls"] == 0 and st["key_pool_ok"] == 0


# ── 运行时降级链 ─────────────────────────────────────────────────


async def test_primary_down_pool_takes_over(monkeypatch):
    seen = _silence_notifications(monkeypatch)
    primary = _FakeChatClient(fail=Exception("Error code: 402 - Insufficient Balance"))
    backup = _FakeChatClient(reply="备用键出话")
    c = _client(primary)
    c._pool_entries = [_pool_entry("ds2", backup)]
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out == "备用键出话"
    assert primary.calls == 2 and backup.calls == 1
    assert c._pool_calls == 1 and c._pool_ok == 1
    assert c._pool_last_key == "ds2"
    # 备用键拿到与主链同款 messages（上下文不丢）
    assert {"role": "user", "content": "在吗"} in backup.last_kw["messages"]
    # 主 Key 失效告警 + 接管提醒都发了
    assert seen["keyfail"] and "Insufficient Balance" in seen["keyfail"][0][1]
    assert any(k == "pool_takeover" for _, k in seen["takeover"])


async def test_pool_iterates_to_next_key_and_cools_failed(monkeypatch):
    seen = _silence_notifications(monkeypatch)
    primary = _FakeChatClient(fail=Exception("Connection error."))
    bad = _FakeChatClient(fail=Exception("Error code: 401 - invalid api key"),
                          base_url="https://api.deepseek.com/v1")
    good = _FakeChatClient(reply="第二把钥匙")
    c = _client(primary)
    c._pool_entries = [_pool_entry("dead", bad), _pool_entry("alive", good)]
    out = await c._generate_reply_openai_compat("hi", context={"reply_lang": "zh"})
    assert out == "第二把钥匙"
    assert bad.calls == 1 and good.calls == 1
    assert c._pool_entries[0]["bad_until"] > time.time()  # 失败键进冷却
    # 池内 key 自身 401 → 也要告警（备用键悄悄过期必须暴露）
    assert any("(dead)" in p for p, _ in seen["keyfail"])


async def test_pool_cooldown_skips_dead_key(monkeypatch):
    _silence_notifications(monkeypatch)
    primary = _FakeChatClient(fail=Exception("boom"))
    cooling = _FakeChatClient(reply="不该被调")
    good = _FakeChatClient(reply="ok")
    c = _client(primary)
    e1 = _pool_entry("cooling", cooling)
    e1["bad_until"] = time.time() + 60
    c._pool_entries = [e1, _pool_entry("good", good)]
    out = await c._generate_reply_openai_compat("hi", context={"reply_lang": "zh"})
    assert out == "ok"
    assert cooling.calls == 0 and good.calls == 1


async def test_all_pool_dead_falls_to_local_then_canned(monkeypatch):
    _silence_notifications(monkeypatch)
    primary = _FakeChatClient(fail=Exception("boom"))
    dead = _FakeChatClient(fail=Exception("also boom"))
    local = _FakeChatClient(reply="本地兜底")
    c = _client(primary)
    c._pool_entries = [_pool_entry("dead", dead)]
    c._fb_client = local
    c._fb_model = "qwen-local"
    out = await c._generate_reply_openai_compat("hi", context={"reply_lang": "zh"})
    assert out == "本地兜底"
    assert dead.calls == 1 and local.calls == 1
    # 全链尽失 → canned
    dead2 = _FakeChatClient(fail=Exception("x"))
    c2 = _client(_FakeChatClient(fail=Exception("boom")))
    c2._pool_entries = [_pool_entry("dead", dead2)]
    out2 = await c2._generate_reply_openai_compat("hi", context={"reply_lang": "zh"})
    assert out2  # canned 占位仍出话


async def test_breaker_open_uses_pool_before_local(monkeypatch):
    _silence_notifications(monkeypatch)
    primary = _FakeChatClient(reply="不该被调用")
    backup = _FakeChatClient(reply="熔断期备用键出话")
    local = _FakeChatClient(reply="不该轮到本地")
    c = _client(primary)
    c._pool_entries = [_pool_entry("ds2", backup)]
    c._fb_client = local
    c._fb_model = "qwen-local"
    c._cb_enabled = True
    c._cb_open_until = time.time() + 60
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out == "熔断期备用键出话"
    assert primary.calls == 0    # 开路语义保住：主模型免打扰
    assert backup.calls == 1 and local.calls == 0


async def test_breaker_open_pool_only_no_local_still_answers(monkeypatch):
    _silence_notifications(monkeypatch)
    primary = _FakeChatClient(reply="不该被调用")
    backup = _FakeChatClient(reply="池顶班")
    c = _client(primary)
    c._pool_entries = [_pool_entry("ds2", backup)]
    c._cb_enabled = True
    c._cb_open_until = time.time() + 60
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out == "池顶班"
    assert primary.calls == 0


async def test_pool_empty_reply_cools_and_continues(monkeypatch):
    _silence_notifications(monkeypatch)
    primary = _FakeChatClient(fail=Exception("boom"))
    empty = _FakeChatClient(reply=None)
    good = _FakeChatClient(reply="有货")
    c = _client(primary)
    c._pool_entries = [_pool_entry("empty", empty), _pool_entry("good", good)]
    out = await c._generate_reply_openai_compat("hi", context={"reply_lang": "zh"})
    assert out == "有货"
    assert c._pool_entries[0]["bad_until"] > time.time()


# ── 池智能排序（探活/运行态证据优先）─────────────────────────────


def _e(name, *, bad_until=0.0, last_ok_ts=0.0):
    return {"name": name, "client": object(), "model": "m",
            "label": f"m @ x ({name})", "bad_until": bad_until,
            "last_ok_ts": last_ok_ts}


class TestOrderPoolEntries:
    def test_ping_ok_beats_unknown_beats_fail(self):
        from src.ai.ai_client import order_pool_entries
        entries = [_e("nofail"), _e("pingfail"), _e("pingok")]
        pings = {"pingok": {"ok": True, "latency_ms": 800},
                 "pingfail": {"ok": False, "latency_ms": 100}}
        out = order_pool_entries(entries, pings, now=1000.0)
        assert [e["name"] for e in out] == ["pingok", "nofail", "pingfail"]

    def test_cooling_goes_last_even_if_ping_ok(self):
        from src.ai.ai_client import order_pool_entries
        entries = [_e("cooling", bad_until=2000.0), _e("plain")]
        pings = {"cooling": {"ok": True, "latency_ms": 10}}
        out = order_pool_entries(entries, pings, now=1000.0)
        assert [e["name"] for e in out] == ["plain", "cooling"]

    def test_recent_success_wins_within_same_rank(self):
        from src.ai.ai_client import order_pool_entries
        entries = [_e("old", last_ok_ts=100.0), _e("fresh", last_ok_ts=900.0)]
        out = order_pool_entries(entries, {}, now=1000.0)
        assert [e["name"] for e in out] == ["fresh", "old"]

    def test_latency_tiebreak_then_config_order(self):
        from src.ai.ai_client import order_pool_entries
        entries = [_e("slow"), _e("fast"), _e("also-fast")]
        pings = {"slow": {"ok": True, "latency_ms": 900},
                 "fast": {"ok": True, "latency_ms": 50},
                 "also-fast": {"ok": True, "latency_ms": 50}}
        out = order_pool_entries(entries, pings, now=1000.0)
        assert [e["name"] for e in out] == ["fast", "also-fast", "slow"]

    def test_no_signals_keeps_config_order(self):
        from src.ai.ai_client import order_pool_entries
        entries = [_e("a"), _e("b"), _e("c")]
        assert [e["name"] for e in order_pool_entries(entries, {}, now=1.0)] == ["a", "b", "c"]


async def test_runtime_prefers_ping_healthy_key(monkeypatch):
    """运行时集成：探活标记 dead 的 key 排后，第一击命中健康钥匙。"""
    _silence_notifications(monkeypatch)
    from src.utils import cloud_credentials as ccm
    monkeypatch.setattr(ccm, "ping_state_snapshot",
                        lambda: {"k1": {"ok": False, "latency_ms": 10},
                                 "k2": {"ok": True, "latency_ms": 300}})
    primary = _FakeChatClient(fail=Exception("boom"))
    k1 = _FakeChatClient(reply="不该先被调")
    k2 = _FakeChatClient(reply="健康钥匙出话")
    c = _client(primary)
    c._pool_entries = [_pool_entry("k1", k1), _pool_entry("k2", k2)]
    out = await c._generate_reply_openai_compat("hi", context={"reply_lang": "zh"})
    assert out == "健康钥匙出话"
    assert k2.calls == 1 and k1.calls == 0
    # 成功后运行态新鲜度落到条目上（后续排序继续受益）
    k2_entry = next(e for e in c._pool_entries if e["name"] == "k2")
    assert k2_entry["last_ok_ts"] > 0
