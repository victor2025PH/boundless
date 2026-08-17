# -*- coding: utf-8 -*-
"""进程内采集器门禁（注入式，零网络零真单例）。

钉住：① /health 通但 /accounts 拉取失败 → 按 sidecar 不可达（保守方向——
绝不虚构「账号全丢」喂给 not_restored 告警）；② TTL 缓存对生产调用生效
（metrics 轮询 + watchdog tick 共享一次探针）；③ 注入即绕过缓存。
"""
from src.integrations import messenger_readiness_collect as mrc


def _fake_http(mapping):
    calls = {"n": 0}

    def get(url):
        calls["n"] += 1
        for suffix, v in mapping.items():
            if url.endswith(suffix):
                if isinstance(v, Exception):
                    raise v
                return v
        raise AssertionError(f"unexpected url {url}")

    get.calls = calls
    return get


_CFG = {"platform_login": {"messenger": {"web_enabled": True,
                                         "web_url": "http://127.0.0.1:9"}},
        "inbox": {"l2_autosend": {"enabled": True, "deliver": True}}}

_REG = [{"account_id": "A1", "status": "online", "label": ""}]


def test_ready_verdict_with_injected_sources():
    http = _fake_http({
        "/health": {"ok": True},
        "/accounts": {"accounts": [{"account_id": "A1", "logged_in": True,
                                    "read_attempts": 4, "read_fails": 0}]},
    })
    v = mrc.collect_messenger_readiness(
        _CFG, now=1000.0, http_get=http, registry_rows=_REG,
        session_registry={"sessions": {}, "inbox_health": {}})
    assert v["overall"] == "ready"
    assert v["accounts"][0]["registry_status"] == "online"


def test_accounts_fetch_error_is_conservative_not_missing_accounts():
    http = _fake_http({"/health": {"ok": True},
                       "/accounts": RuntimeError("boom")})
    v = mrc.collect_messenger_readiness(
        _CFG, now=1000.0, http_get=http, registry_rows=_REG,
        session_registry=None)
    assert v["sources"]["sidecar_reachable"] is False, \
        "/accounts 拉取失败必须按 sidecar 不可达（防虚构 account_not_in_sidecar）"
    assert v["overall"] == "sidecar_down"


def test_health_down_unreachable():
    http = _fake_http({"/health": RuntimeError("conn refused")})
    v = mrc.collect_messenger_readiness(
        _CFG, now=1000.0, http_get=http, registry_rows=_REG,
        session_registry=None)
    assert v["overall"] == "sidecar_down"


def test_ttl_cache_shares_one_probe(monkeypatch):
    mrc._reset_cache_for_tests()
    http = _fake_http({"/health": {"ok": True}, "/accounts": {"accounts": []}})
    monkeypatch.setattr(mrc, "_http_get_json", http)
    monkeypatch.setattr(mrc, "_registry_messenger_rows", lambda: list(_REG))
    monkeypatch.setattr(mrc, "_session_registry_snapshot", lambda: None)
    v1 = mrc.collect_messenger_readiness(_CFG, now=1000.0)
    v2 = mrc.collect_messenger_readiness(_CFG, now=1030.0)   # 30s < TTL
    assert v1 is v2
    assert http.calls["n"] == 2, "TTL 窗口内只允许一轮探针（health+accounts）"
    v3 = mrc.collect_messenger_readiness(_CFG, now=1070.0)   # 70s > TTL
    assert v3 is not v1
    mrc._reset_cache_for_tests()


def test_injection_bypasses_cache(monkeypatch):
    mrc._reset_cache_for_tests()
    monkeypatch.setattr(mrc, "_http_get_json",
                        _fake_http({"/health": {"ok": True},
                                    "/accounts": {"accounts": []}}))
    monkeypatch.setattr(mrc, "_registry_messenger_rows", lambda: [])
    monkeypatch.setattr(mrc, "_session_registry_snapshot", lambda: None)
    mrc.collect_messenger_readiness(_CFG, now=1000.0)          # 种缓存
    http = _fake_http({"/health": {"ok": True},
                       "/accounts": {"accounts": []}})
    v = mrc.collect_messenger_readiness(_CFG, now=1001.0, http_get=http,
                                        registry_rows=_REG,
                                        session_registry=None)
    assert http.calls["n"] == 2, "显式注入必须绕过缓存真采集"
    assert v["accounts"], "注入的注册表行必须参与判定"
    mrc._reset_cache_for_tests()
