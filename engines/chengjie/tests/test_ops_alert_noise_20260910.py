"""运维群告警降噪门禁（2026-09-10 值守分析沉淀）。

31 小时里运维群 65 条告警只有 1 条是业务信息，其余四类根因各钉一条不变量：
  ① 176 qwen3-vl 永久钉丢失 → 探针周期撞冷载 → host_alert 抖动 12 条
     → true_probe 识图探针成功后顺手核钉（ensure_ollama_pinned）
  ② 104 IndexTTS-2 /health 返回 status/model_loaded，探针只认 ok/models_loaded
     → 全程健康却每 4h 一条 avatar_voice_alert
  ③ 173:8001 是 vLLM，LAN GPU 探针只打 Ollama /api/version → 404 → 永远「不可达」
  ④ 坐席日志监控用自家 bot 私聊生产账号 → peer_bot_guard 把自己人报成 bot 对端
"""
from __future__ import annotations

from src.ops import true_probe as tp


# ── ① Ollama 永久钉核查 ───────────────────────────────────────────────

_PS_LOST = {"models": [{
    "name": "qwen3-vl:8b-instruct", "model": "qwen3-vl:8b-instruct",
    # 真实返回：Go RFC3339 带 7 位小数 + 时区偏移
    "expires_at": "2026-09-10T15:21:59.1274709+08:00",
    "size_vram": 5970035998,
}]}
_PS_PINNED = {"models": [{
    "name": "qwen3-vl:8b-instruct",
    "expires_at": "2318-12-21T15:04:55.008935607+08:00",
}]}


def test_pin_needed_when_expires_within_a_day():
    now = 1789000000.0  # 任意；实测 2026-09-10 的 expires_at 相对它远在过去 ⇒ 判丢钉
    assert tp.ollama_pin_needed(_PS_LOST, "qwen3-vl:8b-instruct", now_ts=now) is True


def test_pin_not_needed_when_permanent():
    assert tp.ollama_pin_needed(_PS_PINNED, "qwen3-vl:8b-instruct") is False


def test_pin_not_needed_when_model_not_resident_or_bad_payload():
    # 未驻留：冷载交给探针请求自己触发，这里不替代加载
    assert tp.ollama_pin_needed(_PS_PINNED, "hy-mt2:7b") is False
    assert tp.ollama_pin_needed({}, "qwen3-vl:8b-instruct") is False
    assert tp.ollama_pin_needed({"models": "garbage"}, "qwen3-vl:8b-instruct") is False
    # expires_at 解析不出来 ⇒ 宁可不钉
    assert tp.ollama_pin_needed(
        {"models": [{"name": "qwen3-vl:8b-instruct", "expires_at": "???"}]},
        "qwen3-vl:8b-instruct") is False


def test_ollama_root_only_for_openai_chat_shape():
    assert tp._ollama_root("http://192.168.0.176:11434/v1/chat/completions") \
        == "http://192.168.0.176:11434"
    assert tp._ollama_root("https://api.siliconflow.cn/v1/chat/completions") \
        == "https://api.siliconflow.cn"
    assert tp._ollama_root("http://h:9000/api/tts_only") == ""


def test_ensure_pinned_skips_cloud_and_repins_lan(monkeypatch):
    calls = []

    class _Resp:
        def __init__(self, body: bytes):
            self._b = body

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import json

    def _fake_urlopen(url, timeout=0):
        calls.append(("GET", url))
        return _Resp(json.dumps(_PS_LOST).encode())

    def _fake_http_json(url, payload, timeout, headers=None):
        calls.append(("POST", url, payload))
        return {"done": True}

    monkeypatch.setattr(tp.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(tp, "_http_json", _fake_http_json)

    # 云端端点：一个请求都不发
    assert tp.ensure_ollama_pinned(
        "https://api.siliconflow.cn/v1/chat/completions", "Qwen/Qwen3-VL-8B-Instruct") == ""
    assert calls == []

    # LAN 端点丢钉：GET /api/ps → POST /api/generate keep_alive=-1
    tag = tp.ensure_ollama_pinned(
        "http://192.168.0.176:11434/v1/chat/completions", "qwen3-vl:8b-instruct")
    assert tag == "repinned"
    assert calls[0] == ("GET", "http://192.168.0.176:11434/api/ps")
    assert calls[1][0:2] == ("POST", "http://192.168.0.176:11434/api/generate")
    assert calls[1][2] == {"model": "qwen3-vl:8b-instruct", "keep_alive": -1}


def test_ensure_pinned_never_raises(monkeypatch):
    def _boom(*a, **k):
        raise OSError("host down")
    monkeypatch.setattr(tp.urllib.request, "urlopen", _boom)
    assert tp.ensure_ollama_pinned(
        "http://192.168.0.176:11434/v1/chat/completions", "qwen3-vl:8b-instruct") == ""


def test_vision_spec_marks_lan_endpoint_for_pin_check_only():
    cfg = {
        "vision": {
            "enabled": True, "model": "qwen3-vl:8b-instruct",
            "base_urls": ["http://192.168.0.176:11434/v1", "https://api.siliconflow.cn/v1"],
            "endpoint_models": {"siliconflow": "Qwen/Qwen3-VL-8B-Instruct"},
        },
    }
    specs = {s["domain"]: s for s in tp.build_probe_specs(cfg)}
    assert specs["vision"].get("pin_keep_alive") is True
    assert "pin_keep_alive" not in specs["vision_backup1"]
    # 开关可关
    cfg["vision"]["lan_keep_alive_pin"] = False
    specs = {s["domain"]: s for s in tp.build_probe_specs(cfg)}
    assert "pin_keep_alive" not in specs["vision"]


# ── ② 语音 /health 双契约 ────────────────────────────────────────────────

import urllib.request  # noqa: E402

from src.inbox import health_watchdog as hw  # noqa: E402


def test_health_payload_accepts_both_contracts():
    # AvatarHub 7852
    assert hw.health_payload_ready({"ok": True, "models_loaded": True, "device": "cuda"})
    assert not hw.health_payload_ready({"ok": True, "models_loaded": False})
    assert not hw.health_payload_ready({"ok": False})
    # 104:7865 IndexTTS-2（fish_speech 契约）——2026-09-10 真实返回体
    assert hw.health_payload_ready({
        "status": "ok", "engine": "index_tts2", "model_loaded": True,
        "load_err": "", "fp16": True})
    assert not hw.health_payload_ready({"status": "ok", "model_loaded": False})
    assert not hw.health_payload_ready({"status": "loading"})
    # 老服务无加载字段 ⇒ ok 即就绪；垃圾输入 ⇒ False
    assert hw.health_payload_ready({"ok": True})
    assert not hw.health_payload_ready(None)
    assert not hw.health_payload_ready("ok")


def test_probe_avatar_voice_treats_indextts_node_as_loaded(monkeypatch):
    import json

    class _Resp:
        def read(self):
            return json.dumps({"status": "ok", "engine": "index_tts2",
                               "model_loaded": True}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=0: _Resp())
    cfg = {"minicpm_clone": {"enabled": True, "base_url": "http://192.168.0.104:7865"}}
    r = hw.probe_avatar_voice(cfg, force=True)
    assert r["url"] == "http://192.168.0.104:7865/health"
    assert r["reachable"] is True and r["models_loaded"] is True


# ── ③ LAN GPU 探活兼容 vLLM ──────────────────────────────────────────────

def _urlopen_factory(behaviour):
    """behaviour: path → 'ok' | int(HTTP code) | Exception"""
    import io
    import urllib.error

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _urlopen(url, timeout=0):
        path = "/" + url.split("/", 3)[3] if url.count("/") >= 3 else "/"
        b = behaviour.get(path, 404)
        if b == "ok":
            return _Resp(b"{}")
        if isinstance(b, Exception):
            raise b
        raise urllib.error.HTTPError(url, int(b), "nf", {}, None)
    return _urlopen


def test_lan_gpu_probe_falls_back_to_openai_models_for_vllm(monkeypatch):
    # vLLM 173:8001：/api/version 404，/v1/models 200
    monkeypatch.setattr(urllib.request, "urlopen",
                        _urlopen_factory({"/api/version": 404, "/v1/models": "ok"}))
    r = hw.probe_lan_gpu_host("http://192.168.0.173:8001")
    assert r["reachable"] is True and r["probe"] == "/v1/models"


def test_lan_gpu_probe_ollama_fast_path(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        _urlopen_factory({"/api/version": "ok"}))
    r = hw.probe_lan_gpu_host("http://192.168.0.176:11434")
    assert r["reachable"] is True and r["probe"] == "/api/version"


def test_lan_gpu_probe_connection_failure_stops_immediately(monkeypatch):
    calls = []
    inner = _urlopen_factory({"/api/version": OSError("refused")})

    def _urlopen(url, timeout=0):
        calls.append(url)
        return inner(url, timeout=timeout)
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    r = hw.probe_lan_gpu_host("http://192.168.0.140:7852")
    assert r["reachable"] is False and "refused" in r["error"]
    assert len(calls) == 1        # 主机真挂：不再逐路径白等 3s×N


def test_lan_gpu_probe_all_paths_404_is_unreachable(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_factory({}))
    r = hw.probe_lan_gpu_host("http://192.168.0.9:1")
    assert r["reachable"] is False and "HTTP 404" in r["error"]


# ── ④ 自家 bot 不触发 bot_peer_alert ────────────────────────────────────

from src.inbox import peer_bot_guard as pbg  # noqa: E402


def test_parse_cfg_own_bot_ids_normalized():
    cfg = pbg.parse_cfg({"inbox": {"peer_bot_guard": {
        "enabled": True, "own_bot_ids": ["@TGZKW_bot", 8506426282, "", None]}}})
    assert cfg["own_bot_ids"] == ["tgzkw_bot", "8506426282"]
    cfg = pbg.parse_cfg({"inbox": {"peer_bot_guard": {"own_bot_ids": "a_bot, 123"}}})
    assert cfg["own_bot_ids"] == ["a_bot", "123"]
    assert pbg.parse_cfg({})["own_bot_ids"] == []


def test_is_own_bot_matches_id_or_username_and_webhook_token_prefix():
    cfg = pbg.parse_cfg({"inbox": {"peer_bot_guard": {"own_bot_ids": ["@tgzkw_bot"]}}})
    assert pbg.is_own_bot(cfg, chat_key="1", username="tgzkw_bot", extra_ids=frozenset())
    assert pbg.is_own_bot(cfg, chat_key="8506426282", username="",
                          extra_ids=frozenset({"8506426282"}))
    assert not pbg.is_own_bot(cfg, chat_key="93372553", username="BotFather",
                              extra_ids=frozenset({"8506426282"}))
    assert not pbg.is_own_bot(cfg, chat_key="", username="", extra_ids=frozenset())


def test_webhook_bot_ids_derived_from_telegram_token(monkeypatch):
    import src.integrations.notify_webhooks_store as nws
    monkeypatch.setattr(nws, "load", lambda: [
        {"name": "tg-ops", "format": "telegram", "token": "8506426282:AAxx", "target": "1"},
        {"name": "hook", "format": "generic", "url": "https://x/y"},
    ])
    pbg._OWN_BOT_CACHE.update({"ts": 0.0, "ids": frozenset()})
    assert pbg._webhook_bot_ids() == frozenset({"8506426282"})
    pbg._OWN_BOT_CACHE.update({"ts": 0.0, "ids": frozenset()})


def test_a_line_own_bot_blocks_but_does_not_alert(monkeypatch):
    """自家 bot 私聊生产账号：仍拦自动链 + 降 manual（AI 不回自家 bot），但零告警。"""
    pbg._reset_for_tests()
    alerts = []
    monkeypatch.setattr(pbg, "_maybe_alert", lambda *a, **k: alerts.append(a))
    monkeypatch.setattr(pbg, "_webhook_bot_ids", lambda: frozenset({"8506426282"}))

    class _Store:
        modes = {}

        def list_recent_messages(self, cid, limit=120):
            return []

        def get_conversation(self, cid):
            return {"conversation_id": cid}

        def get_automation_mode_if_set(self, cid):
            return self.modes.get(cid)

        def set_automation_mode(self, cid, mode, source=""):
            self.modes[cid] = mode

        def set_peer_bot_verdict(self, cid, **kw):
            pass

        def list_conversations(self, limit=1000):
            return []

    store = _Store()
    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(pb, "get_inbox_store", lambda: store)

    class _Peer:
        is_bot = True
        username = "tgzkw_bot"
        first_name = "监控"

    class _Chat:
        type = "private"

    class _Msg:
        from_user = _Peer()
        chat = _Chat()
        reply_markup = None

    cfg = {"inbox": {"peer_bot_guard": {"enabled": True, "sweep_legacy": False}}}
    reason = pbg.guard_a_line_should_skip(
        config=cfg, account_id="8244899900", chat_id=8506426282, message=_Msg(),
        current_text="[坐席日志监控] 健康")
    assert reason == "tg_is_bot"                       # 仍然拦
    assert list(store.modes.values()) == ["manual"]     # 仍然降档
    assert alerts == []                                # 但不告警
    assert pbg.stats_snapshot()["suppressed"].get("own_bot") == 1

    # 对照：陌生 bot 照常告警
    _Peer.username = "SpamBot"
    pbg.guard_a_line_should_skip(
        config=cfg, account_id="8244899900", chat_id=178220800, message=_Msg(),
        current_text="Please use buttons")
    assert len(alerts) == 1
    pbg._reset_for_tests()


def test_openai_chat_probe_appends_repin_tag(monkeypatch):
    monkeypatch.setattr(tp, "_http_json", lambda *a, **k: {
        "choices": [{"message": {"content": "鲜艳的红色"}}]})
    monkeypatch.setattr(tp, "ensure_ollama_pinned", lambda url, model: "repinned")
    ok, detail = tp.run_probe({
        "domain": "vision", "kind": "openai_chat",
        "url": "http://192.168.0.176:11434/v1/chat/completions",
        "json": {"model": "qwen3-vl:8b-instruct"}, "expect_any": ["红"],
        "pin_keep_alive": True, "timeout": 3,
    })
    assert ok and detail.endswith("repinned")
