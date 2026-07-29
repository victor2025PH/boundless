"""托管 AI 网关客户端门禁：设备令牌注入 / env 存活 / base_url 自算 / 资格闸 / 缓存回落。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from src.ai import hosted_gateway as hg


class _CM:
    def __init__(self, tmp_path: Path, ai: dict):
        self.config_path = str(tmp_path / "config" / "config.yaml")
        Path(self.config_path).parent.mkdir(parents=True, exist_ok=True)
        self.config = {"ai": dict(ai), "licensing": {"hosted_ai": {"enabled": True}}}


@pytest.fixture(autouse=True)
def _reset_module_state():
    hg._quota_cache["ts"] = 0.0
    hg._quota_cache["data"] = None
    yield


def _fp(monkeypatch, value="AAAA-BBBB-CCCC-DDDD"):
    monkeypatch.setattr(
        "src.licensing.machine_bridge.machine_fingerprint", lambda: value)


def test_ensure_skips_when_user_has_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CM(tmp_path, {"api_key": "sk-user-own", "base_url": "https://api.deepseek.com"})
    assert hg.ensure_hosted_ai(cm, fetch=lambda *a, **k: {"ok": True, "token": "cx.x"}) is False
    assert cm.config["ai"]["api_key"] == "sk-user-own"


def test_ensure_injects_token_env_and_ignores_server_base_url(tmp_path, monkeypatch):
    """注入成功：config + env 双写；base_url 用本地拼（不信服务端回传的 127.0.0.1）。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CM(tmp_path, {"api_key": "", "base_url": "https://api.deepseek.com", "model": "old"})
    _fp(monkeypatch)

    def fake_fetch(url, method, body):
        assert "/api/ai/device-token" in url and method == "POST"
        return {
            "ok": True,
            "token": "cx.test.token",
            "base_url": "http://127.0.0.1:3000/api/ai/v1",  # 反代污染值，必须被忽略
            "model": "deepseek-chat",
            "exp": int(time.time()) + 30 * 86400,
        }

    assert hg.ensure_hosted_ai(cm, fetch=fake_fetch) is True
    ai = cm.config["ai"]
    assert ai["api_key"] == "cx.test.token"
    assert ai["base_url"] == "https://bd2026.cc/api/ai/v1"
    assert ai["model"] == "deepseek-chat"
    assert ai.get("_hosted_trial") is True
    # env 双写 → 任何 config 热重载经 _apply_env_overrides 重放注入
    assert os.environ.get("AITR_HOSTED_AI_KEY") == "cx.test.token"
    assert os.environ.get("AITR_HOSTED_AI_BASE_URL") == "https://bd2026.cc/api/ai/v1"
    cache = Path(cm.config_path).parent / hg.STATE_FILENAME
    assert json.loads(cache.read_text(encoding="utf-8"))["token"] == "cx.test.token"


def test_ensure_no_claim_leaves_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CM(tmp_path, {"api_key": ""})
    _fp(monkeypatch)
    assert hg.ensure_hosted_ai(
        cm, fetch=lambda *a, **k: {"ok": False, "error": "no_claim"}) is False
    assert cm.config["ai"].get("api_key", "") == ""


def test_ensure_fresh_cache_short_circuits(tmp_path, monkeypatch):
    """缓存新鲜（距过期 > 7 天）→ 零 HTTP 直接套用。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CM(tmp_path, {"api_key": ""})
    cache = Path(cm.config_path).parent / hg.STATE_FILENAME
    cache.write_text(json.dumps({
        "token": "cx.cached", "base_url": "https://bd2026.cc/api/ai/v1",
        "model": "deepseek-chat", "exp": int(time.time()) + 20 * 86400,
    }), encoding="utf-8")

    def boom(*a, **k):
        raise AssertionError("cache fresh 时不应发 HTTP")

    assert hg.ensure_hosted_ai(cm, fetch=boom) is True
    assert cm.config["ai"]["api_key"] == "cx.cached"


def test_ensure_network_fail_falls_back_to_valid_cache(tmp_path, monkeypatch):
    """临期（<7 天）触发换新，网络失败但旧令牌仍有效 → 沿用旧令牌不掉线。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CM(tmp_path, {"api_key": ""})
    _fp(monkeypatch)
    cache = Path(cm.config_path).parent / hg.STATE_FILENAME
    cache.write_text(json.dumps({
        "token": "cx.stale-but-valid", "base_url": "https://bd2026.cc/api/ai/v1",
        "exp": int(time.time()) + 2 * 86400,  # 有效但不新鲜
    }), encoding="utf-8")
    assert hg.ensure_hosted_ai(
        cm, fetch=lambda *a, **k: {"ok": False, "error": "network"}) is True
    assert cm.config["ai"]["api_key"] == "cx.stale-but-valid"


def test_ensure_operator_env_not_taken_over(tmp_path, monkeypatch):
    """运维直注的 env Key（≠缓存令牌）→ 本模块不接管不覆盖。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    monkeypatch.setenv("AITR_HOSTED_AI_KEY", "sk-ops-injected")
    cm = _CM(tmp_path, {"api_key": ""})

    def boom(*a, **k):
        raise AssertionError("运维直注时不应发 HTTP")

    assert hg.ensure_hosted_ai(cm, fetch=boom) is True
    assert os.environ["AITR_HOSTED_AI_KEY"] == "sk-ops-injected"


def test_ensure_disabled_by_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CM(tmp_path, {"api_key": ""})
    cm.config["licensing"]["hosted_ai"]["enabled"] = False
    assert hg.ensure_hosted_ai(cm, fetch=lambda *a, **k: {"ok": True, "token": "x"}) is False


def test_quota_probe_reads_gateway_and_caches(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CM(tmp_path, {"api_key": "cx.tok", "_hosted_trial": True})
    cache = Path(cm.config_path).parent / hg.STATE_FILENAME
    cache.write_text(json.dumps({
        "token": "cx.tok", "exp": int(time.time()) + 86400}), encoding="utf-8")
    calls = []

    def fake_fetch(url, method, body):
        calls.append(url)
        assert url.endswith("/api/ai/v1/quota")
        return {"ok": True, "used": 100, "budget": 50000, "remaining": 49900, "busy": False}

    out = hg.quota_probe(cm, fetch=fake_fetch)
    assert out["enabled"] is True and out["remaining"] == 49900
    assert out["exhausted"] is False
    out2 = hg.quota_probe(cm, fetch=fake_fetch)  # 60s 内二次调用走缓存
    assert out2["remaining"] == 49900
    assert len(calls) == 1


def test_quota_probe_disabled_when_not_hosted(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CM(tmp_path, {"api_key": "sk-user-own"})
    assert hg.quota_probe(cm)["enabled"] is False


def test_schedule_forced_refresh_swaps_token(tmp_path, monkeypatch):
    """401 自愈：后台强制换新 → 缓存/config/env 更新 + on_token 热替换回调。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    monkeypatch.setattr(hg, "_last_forced_ts", 0.0)
    cm = _CM(tmp_path, {"api_key": "cx.dead", "_hosted_trial": True})
    _fp(monkeypatch)
    swapped = []

    def fake_fetch(url, method, body):
        return {"ok": True, "token": "cx.fresh", "exp": int(time.time()) + 30 * 86400}

    assert hg.schedule_forced_refresh(
        cm, on_token=swapped.append, fetch=fake_fetch) is True
    deadline = time.time() + 5
    while not swapped and time.time() < deadline:
        time.sleep(0.05)
    assert swapped == ["cx.fresh"]
    assert cm.config["ai"]["api_key"] == "cx.fresh"
    assert os.environ.get("AITR_HOSTED_AI_KEY") == "cx.fresh"
    # 冷却窗内二次调度被拒（防坏令牌期间每条消息都打官网）
    assert hg.schedule_forced_refresh(cm, fetch=fake_fetch) is False


def test_schedule_forced_refresh_noop_when_not_hosted(tmp_path, monkeypatch):
    monkeypatch.setattr(hg, "_last_forced_ts", 0.0)
    cm = _CM(tmp_path, {"api_key": "sk-user-own"})
    assert hg.schedule_forced_refresh(cm) is False
