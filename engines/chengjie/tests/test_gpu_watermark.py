"""LAN GPU 显存水位（Ollama /api/ps 聚合）：解析/分级/汇总/TTL 缓存。

背景：140(12G) 兼任嵌入双活备点+视觉备点，两者同时压上会挤爆（Ollama 静默
换入换出→延迟毛刺），此前只能 SSH 肉眼 `ollama ps`。纯函数 + 探针分离，
路由 /api/admin/gpu-watermark，卡片在 ops-overview（未启用整卡隐藏）。
"""
from __future__ import annotations

import src.utils.gpu_watermark as gw
from src.utils.gpu_watermark import (
    config_state,
    parse_hosts,
    probe_hosts,
    summarize_fleet,
    summarize_host,
)

GB = 1_000_000_000


def _cfg(enabled=True, hosts=None):
    return {"ops": {"gpu_watermark": {
        "enabled": enabled,
        "hosts": hosts if hosts is not None else [
            {"name": "176-5090", "base_url": "http://192.168.0.176:11434", "vram_gb": 32},
            {"name": "140-4070", "base_url": "http://192.168.0.140:11434/", "vram_gb": 12},
        ],
    }}}


# ---------- parse_hosts ----------

def test_parse_hosts_happy_and_gating():
    hosts = parse_hosts(_cfg())
    assert [h["name"] for h in hosts] == ["176-5090", "140-4070"]
    assert hosts[1]["base_url"] == "http://192.168.0.140:11434"   # 尾斜杠归一
    assert parse_hosts(_cfg(enabled=False)) == []
    assert parse_hosts({}) == []
    # 非法条目被剔除
    assert parse_hosts(_cfg(hosts=[{"name": "x"}, "junk", {"base_url": "notaurl"}])) == []


# ---------- config_state：区分「没开」与「开了但没配」----------
# 2026-07-29 实测事故：overlay 只有 enabled:true、hosts 缺失 → 卡片静默隐藏数周，
# 运营与文档都以为已生效。这两种状态必须可区分，否则永远查不出来。

def test_config_state_three_way():
    assert config_state(_cfg()) == "ready"
    assert config_state(_cfg(enabled=False)) == "off"
    assert config_state({}) == "off"                       # 整段缺失=没开
    assert config_state({"ops": {"gpu_watermark": {}}}) == "off"


def test_config_state_misconfigured_when_hosts_missing():
    """开关开了但 hosts 缺失/全非法 → misconfigured（正是那次事故的形态）。"""
    assert config_state({"ops": {"gpu_watermark": {"enabled": True}}}) == "misconfigured"
    assert config_state(_cfg(hosts=[])) == "misconfigured"
    assert config_state(_cfg(hosts=[{"name": "x"}, "junk"])) == "misconfigured"
    # 至少一台合法即 ready（部分非法不算 misconfigured）
    assert config_state(_cfg(hosts=[
        "junk", {"name": "ok", "base_url": "http://1.2.3.4:11434", "vram_gb": 8},
    ])) == "ready"


# ---------- summarize_host ----------

def test_summarize_host_levels():
    def _ps(used_gb):
        return {"models": [{"name": "m", "size_vram": int(used_gb * GB)}]}
    assert summarize_host("h", 12, _ps(6))["level"] == "ok"        # 50%
    assert summarize_host("h", 12, _ps(9.6))["level"] == "warn"    # 80%
    assert summarize_host("h", 12, _ps(11.5))["level"] == "high"   # 96%


def test_summarize_host_fields_and_sorting():
    ps = {"models": [
        {"name": "small", "size_vram": 1 * GB, "expires_at": "2026-07-12T05:00:00Z"},
        {"name": "big", "size_vram": 18 * GB},
    ]}
    out = summarize_host("176-5090", 32, ps)
    assert out["reachable"] is True
    assert out["used_gb"] == 19.0 and out["total_gb"] == 32
    assert out["used_pct"] == 59.4 and out["level"] == "ok"
    assert [m["name"] for m in out["models"]] == ["big", "small"]  # 大头在前
    assert out["models"][1]["until"].startswith("2026-")


def test_summarize_host_unreachable_and_empty():
    bad = summarize_host("h", 12, None, error="connect timeout")
    assert bad["reachable"] is False and bad["level"] == "unknown"
    assert bad["used_gb"] is None and "connect" in bad["error"]
    idle = summarize_host("h", 12, {"models": []})
    assert idle["level"] == "ok" and idle["used_gb"] == 0.0 and idle["models"] == []


def test_summarize_host_zero_total_no_div_crash():
    out = summarize_host("h", 0, {"models": [{"name": "m", "size_vram": GB}]})
    assert out["used_pct"] == 0.0 and out["level"] == "ok"


# ---------- vLLM 主机（kind: vllm，2026-09-17）----------
# 173 出话口 08-28 迁到 vLLM :8001 后，watermark 条目仍指 :11434 → Ollama 守护进程活着
# 但不管这张卡 → 「主链正在服务的 5090」被画成 reachable / 0 GB / 无模型的空卡。

def test_parse_hosts_vllm_kind_and_resident_gb():
    hosts = parse_hosts(_cfg(hosts=[
        {"name": "173-5090", "base_url": "http://192.168.0.173:8001/", "vram_gb": 32,
         "kind": "vllm", "resident_gb": 28},
        {"name": "176", "base_url": "http://192.168.0.176:11434", "vram_gb": 32},
        {"name": "x", "base_url": "http://1.2.3.4:8001", "vram_gb": 8, "kind": "weird"},
    ]))
    assert hosts[0]["kind"] == "vllm" and hosts[0]["resident_gb"] == 28.0
    assert hosts[0]["base_url"] == "http://192.168.0.173:8001"
    assert hosts[1]["kind"] == "ollama" and "resident_gb" not in hosts[1]
    assert hosts[2]["kind"] == "ollama"          # 未知 kind 按 ollama（不静默丢主机）
    # vllm 未配 resident_gb → None（不猜显存）
    h = parse_hosts(_cfg(hosts=[{"name": "a", "base_url": "http://h:8001", "kind": "vllm"}]))[0]
    assert h["resident_gb"] is None


_METRICS = ("# HELP vllm:kv_cache_usage_perc GPU KV-cache usage. 1 means 100 percent usage.\n"
            "vllm:kv_cache_usage_perc{engine=\"0\",model_name=\"chatx\"} 0.034\n"
            "vllm:num_requests_running{engine=\"0\",model_name=\"chatx\"} 0.0\n")


def test_parse_vllm_kv_cache_pct():
    assert gw.parse_vllm_kv_cache_pct(_METRICS) == 3.4
    assert gw.parse_vllm_kv_cache_pct("") is None and gw.parse_vllm_kv_cache_pct(None) is None
    assert gw.parse_vllm_kv_cache_pct("vllm:num_requests_running 1.0\n") is None
    # 多引擎取最挤的那个；>1 视作已是百分比
    assert gw.parse_vllm_kv_cache_pct(
        "vllm:kv_cache_usage_perc{engine=\"0\"} 0.2\nvllm:kv_cache_usage_perc{engine=\"1\"} 0.8\n") == 80.0
    assert gw.parse_vllm_kv_cache_pct("vllm:kv_cache_usage_perc 42\n") == 42.0


def test_summarize_vllm_host_shapes():
    models = {"object": "list", "data": [{"id": "chatx", "object": "model"}]}
    row = gw.summarize_vllm_host("173-5090", 32, models, metrics_text=_METRICS)
    assert row["reachable"] is True and row["kind"] == "vllm" and row["level"] == "ok"
    assert row["used_gb"] is None and row["used_pct"] is None      # 不猜显存
    assert row["kv_cache_pct"] == 3.4
    assert row["models"] == [{"name": "chatx", "size_gb": None, "until": "常驻（vLLM）"}]
    assert "vLLM 常驻" in row["note"] and "chatx" in row["note"] and "KV cache 3.4%" in row["note"]
    # 配了 resident_gb → 显示占用并按卡容量算百分比；模型均分
    row2 = gw.summarize_vllm_host("173", 32, models, resident_gb=28.8)
    assert row2["used_gb"] == 28.8 and row2["used_pct"] == 90.0
    assert row2["models"][0]["size_gb"] == 28.8
    assert row2["level"] == "high"      # 无 KV 指标时按预留占比分级（90% → high）
    # 有 KV 指标时以 KV 为准（预留 90% 但 cache 只用 3% → ok）
    assert gw.summarize_vllm_host("173", 32, models, metrics_text=_METRICS,
                                  resident_gb=28.8)["level"] == "ok"
    # 进程在、目录空 → warn（出话会 404）
    empty = gw.summarize_vllm_host("173", 32, {"data": []})
    assert empty["level"] == "warn" and empty["models"] == [] and "目录为空" in empty["note"]
    # KV cache 挤爆 → high
    hot = gw.summarize_vllm_host("173", 32, models,
                                 metrics_text="vllm:kv_cache_usage_perc 0.95\n")
    assert hot["level"] == "high"
    # 不可达
    bad = gw.summarize_vllm_host("173", 32, None, error="refused")
    assert bad["reachable"] is False and bad["level"] == "unknown" and bad["kind"] == "vllm"


# ---------- summarize_fleet ----------

def test_fleet_takes_worst_level():
    assert summarize_fleet([{"level": "ok"}, {"level": "high"}])["level"] == "high"
    assert summarize_fleet([{"level": "ok"}, {"level": "warn"}])["level"] == "warn"
    # 探不到与 warn 同级（该报修不该装绿），高危仍压过它
    assert summarize_fleet([{"level": "ok"}, {"level": "unknown"}])["level"] == "unknown"
    assert summarize_fleet([{"level": "unknown"}, {"level": "high"}])["level"] == "high"
    assert summarize_fleet([])["level"] == "ok"


# ---------- probe_hosts（假 httpx 注入 + TTL 缓存） ----------

class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


class _FakeAsyncClient:
    calls = []

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        _FakeAsyncClient.calls.append(url)
        if "140" in url:
            raise OSError("host down")
        return _FakeResp({"models": [{"name": "qwen", "size_vram": 20 * GB}]})


def _reset_cache():
    gw._CACHE.update({"ts": 0.0, "key": "", "result": None})


async def test_probe_hosts_aggregates_and_caches(monkeypatch):
    _reset_cache()
    _FakeAsyncClient.calls = []
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    out = await probe_hosts(_cfg())
    assert out is not None and len(out["hosts"]) == 2
    by = {h["name"]: h for h in out["hosts"]}
    assert by["176-5090"]["reachable"] and by["176-5090"]["used_gb"] == 20.0
    assert by["140-4070"]["reachable"] is False
    assert out["level"] in ("warn", "unknown")     # 一台探不到 → 整队非绿
    n = len(_FakeAsyncClient.calls)
    # TTL 缓存：立即再探不打网络
    out2 = await probe_hosts(_cfg())
    assert out2 is out and len(_FakeAsyncClient.calls) == n
    # force 绕过缓存
    await probe_hosts(_cfg(), force=True)
    assert len(_FakeAsyncClient.calls) > n
    _reset_cache()


class _FakeVllmClient(_FakeAsyncClient):
    async def get(self, url):
        _FakeAsyncClient.calls.append(url)
        if url.endswith("/v1/models"):
            return _FakeResp({"data": [{"id": "chatx"}]})
        if url.endswith("/metrics"):
            r = _FakeResp(None)
            r.status_code = 200
            r.text = _METRICS
            return r
        if "140" in url:
            raise OSError("host down")
        return _FakeResp({"models": [{"name": "qwen", "size_vram": 20 * GB}]})


async def test_probe_hosts_mixed_ollama_and_vllm(monkeypatch):
    _reset_cache()
    _FakeAsyncClient.calls = []
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeVllmClient)
    out = await probe_hosts(_cfg(hosts=[
        {"name": "176-5090", "base_url": "http://192.168.0.176:11434", "vram_gb": 32},
        {"name": "173-5090", "base_url": "http://192.168.0.173:8001", "vram_gb": 32, "kind": "vllm"},
    ]), force=True)
    by = {h["name"]: h for h in out["hosts"]}
    assert by["176-5090"]["used_gb"] == 20.0
    assert by["173-5090"]["reachable"] and by["173-5090"]["models"][0]["name"] == "chatx"
    assert by["173-5090"]["kv_cache_pct"] == 3.4 and out["level"] == "ok"
    # vLLM 主机打的是 /v1/models + /metrics，绝不打 /api/ps
    v_calls = [c for c in _FakeAsyncClient.calls if "173" in c]
    assert v_calls == ["http://192.168.0.173:8001/v1/models", "http://192.168.0.173:8001/metrics"]
    _reset_cache()


async def test_probe_hosts_disabled_returns_none():
    _reset_cache()
    assert await probe_hosts(_cfg(enabled=False)) is None
    assert await probe_hosts({}) is None


# ---------- 路由三态：misconfigured 必须可辨识 ----------

def _route_client(cfg):
    """挂 /api/admin/gpu-watermark 的最小 app（注册器取 ctx 属性，见 register 签名）。"""
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.web.routes import ops_overview_routes as R

    R._GW_MISCONFIG_WARNED = False          # 复位一次性告警旗标
    app = FastAPI()
    ctx = SimpleNamespace(
        api_auth=lambda request: True,
        api_write=lambda perm: (lambda: True),   # 依赖工厂（见其他 ops 路由测试）
        page_auth=lambda request: True,
        templates=None,
        config_manager=SimpleNamespace(config=cfg),
        audit_store=None,
        user_store=None,
        token=None,
        telegram_client=None,
    )
    R.register_ops_overview_routes(app, ctx)
    return TestClient(app, raise_server_exceptions=True)


def test_route_reports_misconfigured(monkeypatch):
    """开了但没配 hosts → enabled:false（前端仍隐藏，行为不变）+ misconfigured:true。"""
    c = _route_client({"ops": {"gpu_watermark": {"enabled": True}}})
    d = c.get("/api/admin/gpu-watermark").json()
    assert d["enabled"] is False
    assert d["misconfigured"] is True
    assert "hosts" in d["detail"]


def test_route_off_has_no_misconfigured_flag():
    c = _route_client({"ops": {"gpu_watermark": {"enabled": False}}})
    d = c.get("/api/admin/gpu-watermark").json()
    assert d["enabled"] is False
    assert d.get("misconfigured") is None     # 没开就是没开，不误报配置错


def test_route_ready_returns_hosts(monkeypatch):
    _reset_cache()
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    c = _route_client(_cfg())
    d = c.get("/api/admin/gpu-watermark?force=1").json()
    assert d["enabled"] is True
    assert d.get("misconfigured") is None
    assert len(d["hosts"]) == 2
    _reset_cache()
