"""云端凭证健康探针（DeepSeek 余额水位）单测：探测目标决策 / 分级 / TTL 缓存。

不触网：probe 网络路径由假响应/monkeypatch 覆盖。
"""

import pytest

from src.utils import cloud_credentials as cc


# ── 配置读取 ─────────────────────────────────────────────────────


class TestCredentialsConfig:
    def test_defaults_disabled(self):
        out = cc.credentials_config({})
        assert out["enabled"] is False
        assert out["balance_warn_cny"] == 20.0

    def test_overrides(self):
        out = cc.credentials_config({"ops": {"cloud_credentials": {
            "enabled": True, "balance_warn_cny": 50,
            "probe_interval_sec": 60, "remind_sec": 60}}})
        assert out["enabled"] is True
        assert out["balance_warn_cny"] == 50.0
        # 下限保护：探测间隔 ≥300s、重提 ≥600s（防误配成刷屏）
        assert out["probe_interval_sec"] == 300.0
        assert out["remind_sec"] == 600.0


# ── 探测目标决策（纯函数）─────────────────────────────────────────


class TestBalanceTarget:
    def _cfg(self, provider="openai_compatible",
             base="https://api.deepseek.com/v1", key="sk-real"):
        return {"ai": {"provider": provider, "base_url": base, "api_key": key}}

    def test_deepseek_target(self):
        t = cc.deepseek_balance_target(self._cfg())
        assert t["url"] == "https://api.deepseek.com/user/balance"
        assert t["api_key"] == "sk-real"

    def test_base_without_v1_suffix(self):
        t = cc.deepseek_balance_target(self._cfg(base="https://api.deepseek.com"))
        assert t["url"] == "https://api.deepseek.com/user/balance"

    def test_non_deepseek_base_skipped(self):
        assert cc.deepseek_balance_target(self._cfg(base="http://192.168.0.176:11434/v1")) == {}

    def test_gemini_provider_skipped(self):
        assert cc.deepseek_balance_target(self._cfg(provider="gemini")) == {}

    def test_placeholder_or_empty_key_skipped(self):
        assert cc.deepseek_balance_target(self._cfg(key="")) == {}
        assert cc.deepseek_balance_target(self._cfg(key="YOUR_AI_API_KEY")) == {}

    def test_empty_config_skipped(self):
        assert cc.deepseek_balance_target({}) == {}


class TestBalanceTargets:
    """主 Key + 备用池全凭证探测目标（备用 Key 悄悄欠费必须被巡检覆盖）。"""

    def _cfg(self, pool_keys, primary_key="sk-main"):
        return {"ai": {
            "provider": "openai_compatible",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": primary_key,
            "key_pool": {"enabled": True, "keys": pool_keys},
        }}

    def test_primary_plus_pool(self):
        out = cc.balance_targets(self._cfg([
            {"name": "ds2", "api_key": "sk-b"},
        ]))
        assert [t["name"] for t in out] == ["DeepSeek", "备用:ds2"]
        assert out[1]["url"] == "https://api.deepseek.com/user/balance"
        assert out[1]["api_key"] == "sk-b"

    def test_pool_inherits_primary_base_url(self):
        out = cc.balance_targets(self._cfg([{"name": "x", "api_key": "sk-b"}]))
        assert all("api.deepseek.com" in t["url"] for t in out)

    def test_pool_non_deepseek_entry_skipped(self):
        out = cc.balance_targets(self._cfg([
            {"name": "zhipu", "api_key": "sk-z",
             "base_url": "https://open.bigmodel.cn/api/paas/v4"},
        ]))
        assert [t["name"] for t in out] == ["DeepSeek"]  # 智谱无 /user/balance 契约

    def test_dedup_same_key(self):
        out = cc.balance_targets(self._cfg([
            {"name": "dupe-of-primary", "api_key": "sk-main"},
            {"name": "b", "api_key": "sk-b"},
            {"name": "b-again", "api_key": "sk-b"},
        ]))
        assert [t["name"] for t in out] == ["DeepSeek", "备用:b"]

    def test_pool_disabled_only_primary(self):
        cfg = self._cfg([{"name": "b", "api_key": "sk-b"}])
        cfg["ai"]["key_pool"]["enabled"] = False
        assert [t["name"] for t in cc.balance_targets(cfg)] == ["DeepSeek"]

    def test_pool_placeholder_skipped(self):
        out = cc.balance_targets(self._cfg([
            {"name": "ph", "api_key": "YOUR_BACKUP"},
            {"name": "empty", "api_key": ""},
        ]))
        assert [t["name"] for t in out] == ["DeepSeek"]

    def test_no_primary_pool_still_probed(self):
        # 主链临时切走 DeepSeek（如本地 Ollama）时，池内 DeepSeek 备用键仍应被巡检
        cfg = {"ai": {
            "provider": "openai_compatible",
            "base_url": "http://192.168.0.176:11434/v1",
            "api_key": "ollama",
            "key_pool": {"keys": [
                {"name": "ds", "api_key": "sk-b",
                 "base_url": "https://api.deepseek.com"},
            ]},
        }}
        assert [t["name"] for t in cc.balance_targets(cfg)] == ["备用:ds"]


# ── 分级（纯函数）────────────────────────────────────────────────


class TestClassifyBalance:
    def test_ok_above_threshold(self):
        out = cc.classify_balance(
            {"reachable": True, "http_status": 200, "balances": {"CNY": 38.56}}, 20)
        assert out["status"] == "ok"
        assert out["balance"] == 38.56

    def test_low_below_threshold(self):
        out = cc.classify_balance(
            {"reachable": True, "http_status": 200, "balances": {"CNY": 19.99}}, 20)
        assert out["status"] == "low"

    def test_currency_fallback_to_nonzero(self):
        out = cc.classify_balance(
            {"reachable": True, "http_status": 200,
             "balances": {"USD": 5.0}}, 20)
        assert out["status"] == "low" and out["currency"] == "USD"

    def test_auth_failed(self):
        out = cc.classify_balance(
            {"reachable": True, "http_status": 401, "error": "HTTP 401", "balances": {}}, 20)
        assert out["status"] == "auth_failed"

    def test_unreachable(self):
        out = cc.classify_balance({"reachable": False, "error": "timeout", "balances": {}}, 20)
        assert out["status"] == "unreachable"

    def test_no_data_unknown(self):
        assert cc.classify_balance(None, 20)["status"] == "unknown"
        out = cc.classify_balance(
            {"reachable": True, "http_status": 200, "balances": {}}, 20)
        assert out["status"] == "unknown"

    def test_zero_balance_is_low_not_unknown(self):
        # 全零余额=真的没钱（DeepSeek 停付费后 total_balance=0），须报 low 不是 unknown
        out = cc.classify_balance(
            {"reachable": True, "http_status": 200, "balances": {"CNY": 0.0}}, 20)
        assert out["status"] == "low" and out["balance"] == 0.0


# ── collect（TTL 缓存 + 门控）────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_cache():
    cc._BALANCE_CACHE.update({"ts": 0.0, "sig": "", "result": None})
    yield
    cc._BALANCE_CACHE.update({"ts": 0.0, "sig": "", "result": None})


class TestCollect:
    def _cfg(self, enabled=True):
        return {
            "ai": {"provider": "openai_compatible",
                   "base_url": "https://api.deepseek.com/v1", "api_key": "sk-x"},
            "ops": {"cloud_credentials": {"enabled": enabled, "balance_warn_cny": 20}},
        }

    def test_disabled_returns_none_without_probe(self, monkeypatch):
        called = []
        monkeypatch.setattr(cc, "probe_deepseek_balance",
                            lambda *a, **k: called.append(1) or {})
        assert cc.collect_deepseek_balance(self._cfg(enabled=False)) is None
        assert not called

    def test_non_deepseek_returns_none(self, monkeypatch):
        cfg = self._cfg()
        cfg["ai"]["base_url"] = "http://192.168.0.176:11434/v1"
        assert cc.collect_deepseek_balance(cfg) is None

    def test_collect_classifies_and_caches(self, monkeypatch):
        calls = []

        def _fake_probe(url, key, **kw):
            calls.append(url)
            return {"reachable": True, "http_status": 200, "balances": {"CNY": 15.0}}
        monkeypatch.setattr(cc, "probe_deepseek_balance", _fake_probe)

        out1 = cc.collect_deepseek_balance(self._cfg())
        assert out1["status"] == "low" and out1["provider"] == "DeepSeek"
        out2 = cc.collect_deepseek_balance(self._cfg())
        assert out2 is out1, "TTL 窗口内应吃缓存"
        assert len(calls) == 1

    def test_force_bypasses_cache(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            cc, "probe_deepseek_balance",
            lambda *a, **k: calls.append(1) or
            {"reachable": True, "http_status": 200, "balances": {"CNY": 30.0}})
        cc.collect_deepseek_balance(self._cfg())
        cc.collect_deepseek_balance(self._cfg(), force=True)
        assert len(calls) == 2

    def test_collect_multi_key_pool(self, monkeypatch):
        """主 + 备用池逐 key 探测，provider 名区分；缓存签名覆盖全部凭证。"""
        probed = []

        def _fake_probe(url, key, **kw):
            probed.append(key)
            bal = 100.0 if key == "sk-x" else 5.0
            return {"reachable": True, "http_status": 200, "balances": {"CNY": bal}}
        monkeypatch.setattr(cc, "probe_deepseek_balance", _fake_probe)

        cfg = self._cfg()
        cfg["ai"]["key_pool"] = {"keys": [{"name": "ds2", "api_key": "sk-backup"}]}
        out = cc.collect_cloud_balances(cfg)
        assert [(s["provider"], s["status"]) for s in out] == [
            ("DeepSeek", "ok"), ("备用:ds2", "low")]
        assert probed == ["sk-x", "sk-backup"]
        # 兼容入口仍只回主 Key
        primary = cc.collect_deepseek_balance(cfg)
        assert primary["provider"] == "DeepSeek" and primary["status"] == "ok"
        assert len(probed) == 2  # 吃缓存，无新探测
