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
    # ensure_* 直接写 os.environ（刻意，供热重载回放）——测试进程里必须收尾清理，
    # 否则泄漏给同进程后续测试（config_manager._apply_env_overrides 会突然重放注入）
    for key in (hg.VOICE_ENV_BASE, hg.VOICE_ENV_FIRST, hg.VOICE_ENV_HUBFISH_OFF,
                hg.VOICE_ENV_AUTO, hg.ASR_ENV_BASE, hg.ASR_ENV_FIRST,
                hg.ASR_ENV_AUTO, hg.VISION_ENV_BASE, hg.VISION_ENV_MODEL,
                hg.VISION_ENV_AUTO, hg.EMBED_ENV_BASE, hg.EMBED_ENV_MODEL):
        os.environ.pop(key, None)


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


def test_managed_overrides_stale_bad_key_on_success(tmp_path, monkeypatch):
    """升级坑：严格托管态下旧版残留坏 Key（如 ollama）→ 领到新令牌后覆盖。"""
    monkeypatch.setenv("AITR_MANAGED_EDITION", "1")
    monkeypatch.delenv("AITR_HOSTED_AI_KEY", raising=False)
    _fp(monkeypatch)
    cm = _CM(tmp_path, {"api_key": "ollama", "base_url": "http://127.0.0.1:11434"})

    def fake(u, m, b, bearer=""):
        return {"ok": True, "token": "cx.fresh", "exp": int(time.time()) + 30 * 86400}

    assert hg.ensure_hosted_ai(cm, fetch=fake) is True
    assert cm.config["ai"]["api_key"] == "cx.fresh"
    assert cm.config["ai"].get("_hosted_trial") is True


def test_managed_stale_key_kept_on_fetch_fail(tmp_path, monkeypatch):
    """覆盖只在成功领令牌后发生：领取失败（无有效缓存）→ 旧值不动，绝不破坏。"""
    monkeypatch.setenv("AITR_MANAGED_EDITION", "1")
    monkeypatch.delenv("AITR_HOSTED_AI_KEY", raising=False)
    _fp(monkeypatch)
    cm = _CM(tmp_path, {"api_key": "ollama"})
    assert hg.ensure_hosted_ai(
        cm, fetch=lambda *a, **k: {"ok": False, "error": "no_claim"}) is False
    assert cm.config["ai"]["api_key"] == "ollama"  # 未被清空/破坏


def test_desktop_nonmanaged_keeps_user_key(tmp_path, monkeypatch):
    """仅桌面模式（非严格托管）→ 用户自有 Key 仍受保护，不覆盖。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    monkeypatch.delenv("AITR_MANAGED_EDITION", raising=False)
    cm = _CM(tmp_path, {"api_key": "sk-user-real-key"})

    def boom(*a, **k):
        raise AssertionError("非严格托管不应覆盖用户 Key")

    assert hg.ensure_hosted_ai(cm, fetch=boom) is False
    assert cm.config["ai"]["api_key"] == "sk-user-real-key"


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


def _hosted_cm(tmp_path):
    cm = _CM(tmp_path, {"api_key": "cx.tok", "_hosted_trial": True})
    cache = Path(cm.config_path).parent / hg.STATE_FILENAME
    cache.write_text(json.dumps({
        "token": "cx.tok", "exp": int(time.time()) + 86400}), encoding="utf-8")
    return cm


def test_quota_probe_unlimited_flag(tmp_path, monkeypatch):
    """B82：官网显式 unlimited=true → 归一 unlimited，绝不判用尽（B40 放开态）。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _hosted_cm(tmp_path)
    out = hg.quota_probe(cm, fetch=lambda u, m, b: {
        "ok": True, "used": 999999, "budget": 1000, "remaining": 0,
        "unlimited": True})
    assert out["unlimited"] is True
    assert out["exhausted"] is False   # 不限量下 remaining=0 不算用尽


def test_quota_probe_zero_budget_is_unlimited(tmp_path, monkeypatch):
    """B82：budget<=0＝历史「0=不限量」约定（与 daily_reply_budget 同口径）。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _hosted_cm(tmp_path)
    out = hg.quota_probe(cm, fetch=lambda u, m, b: {
        "ok": True, "used": 500, "budget": 0, "remaining": 0})
    assert out["unlimited"] is True
    assert out["exhausted"] is False


def test_quota_probe_normal_budget_not_unlimited(tmp_path, monkeypatch):
    """反面：正常有限额度不误标 unlimited，用尽判定不变。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _hosted_cm(tmp_path)
    out = hg.quota_probe(cm, fetch=lambda u, m, b: {
        "ok": True, "used": 100, "budget": 50000, "remaining": 49900})
    assert out.get("unlimited") is False
    assert out["exhausted"] is False
    hg._quota_cache["ts"] = 0.0   # 清缓存再测用尽档
    hg._quota_cache["data"] = None
    out2 = hg.quota_probe(cm, fetch=lambda u, m, b: {
        "ok": True, "used": 50000, "budget": 50000, "remaining": 0})
    assert out2.get("unlimited") is False
    assert out2["exhausted"] is True


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


# ── 托管 Telegram 凭据注入 ──────────────────────────────────────────────

class _CMT:
    def __init__(self, tmp_path, telegram: dict):
        self.config_path = str(tmp_path / "config" / "config.yaml")
        Path(self.config_path).parent.mkdir(parents=True, exist_ok=True)
        self.config = {
            "telegram": dict(telegram),
            "licensing": {"hosted_ai": {"enabled": True}},
            "ai": {"api_key": "cx.tok"},
        }


def test_hosted_telegram_injects_when_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    _fp(monkeypatch)
    cm = _CMT(tmp_path, {"api_id": "", "api_hash": ""})

    def fake(u, m, b, bearer=""):
        assert u.endswith("/api/pool/telegram-cred") and m == "POST"
        return {"ok": True, "api_id": "1001", "api_hash": "a" * 32, "name": "grp-A"}

    assert hg.ensure_hosted_telegram(cm, fetch=fake) is True
    assert cm.config["telegram"]["api_id"] == "1001"
    assert cm.config["telegram"]["api_hash"] == "a" * 32
    assert cm.config["telegram"].get("_hosted_cred") is True


def test_hosted_telegram_never_overrides_user_creds(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    _fp(monkeypatch)
    cm = _CMT(tmp_path, {"api_id": "999", "api_hash": "f" * 32})

    def boom(*a, **k):
        raise AssertionError("用户已自填凭据时不应请求池")

    assert hg.ensure_hosted_telegram(cm, fetch=boom) is True
    assert cm.config["telegram"]["api_id"] == "999"


def test_hosted_telegram_pool_disabled_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    _fp(monkeypatch)
    cm = _CMT(tmp_path, {"api_id": "", "api_hash": ""})
    assert hg.ensure_hosted_telegram(
        cm, fetch=lambda *a, **k: {"ok": False, "error": "pool_disabled"}) is False
    assert cm.config["telegram"].get("api_id", "") == ""


def test_hosted_telegram_noop_when_not_hosted(tmp_path, monkeypatch):
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    cm = _CMT(tmp_path, {"api_id": "", "api_hash": ""})
    cm.config["licensing"]["hosted_ai"]["enabled"] = False
    assert hg.ensure_hosted_telegram(cm, fetch=lambda *a, **k: {"ok": True}) is False


def test_refresh_once_retries_telegram_cred(tmp_path, monkeypatch):
    """刷新守护必须补领 Telegram 凭据（2026-08-10 连带发现的断链）。

    此前 refresh_once 只刷 AI/识图/语音——首启没网错过启动注入的机器，
    凭据缺口要等到下次重启才补，用户全程停在自助填写表单。
    """
    called = []
    for fn in ("ensure_hosted_ai", "ensure_hosted_vision",
               "ensure_hosted_voice", "ensure_hosted_asr"):
        monkeypatch.setattr(hg, fn, lambda cm, _n=fn: called.append(_n))
    monkeypatch.setattr(hg, "ensure_hosted_telegram",
                        lambda cm: called.append("ensure_hosted_telegram"))
    hg.refresh_once(object())
    assert "ensure_hosted_telegram" in called, "refresh_once 漏掉 Telegram 凭据补领"


def test_report_invalid_and_refetch_swaps_creds(tmp_path, monkeypatch):
    """P1-⑤ 无感换发：举报废组 → 领新组 → 内存凭据就地更新。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    _fp(monkeypatch)
    hg._reset_swap_cooldown_for_tests()
    cfg = {"telegram": {"api_id": "1001", "api_hash": "a" * 32, "_hosted_cred": True},
           "licensing": {"hosted_ai": {"enabled": True}}, "ai": {"api_key": "cx.tok"}}
    seen = {}

    def fake(u, m, b, bearer=""):
        assert u.endswith("/api/pool/telegram-cred") and m == "POST"
        seen.update(b or {})
        return {"ok": True, "api_id": "2002", "api_hash": "b" * 32,
                "name": "grp-B", "swapped": True}

    got = hg.report_invalid_and_refetch(cfg, "1001", fetch=fake)
    assert got == ("2002", "b" * 32)
    assert seen.get("invalid_api_id") == "1001", "举报必须带废 api_id"
    assert cfg["telegram"]["api_id"] == "2002"
    assert cfg["telegram"]["api_hash"] == "b" * 32
    assert cfg["telegram"]["_hosted_cred"] is True


def test_report_invalid_never_touches_user_creds(tmp_path, monkeypatch):
    """用户自填凭据（无 _hosted_cred 标记）→ 一个字节都不动、不发请求。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    _fp(monkeypatch)
    hg._reset_swap_cooldown_for_tests()
    cfg = {"telegram": {"api_id": "999", "api_hash": "f" * 32}}

    def boom(*a, **k):
        raise AssertionError("用户自填凭据不得触发换发请求")

    assert hg.report_invalid_and_refetch(cfg, "999", fetch=boom) is None
    assert cfg["telegram"]["api_id"] == "999"


def test_report_invalid_cooldown_blocks_hammering(tmp_path, monkeypatch):
    """120s 冷却：坏组 × 连点刷新不能变成打官网的机关枪。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    _fp(monkeypatch)
    hg._reset_swap_cooldown_for_tests()
    cfg = {"telegram": {"api_id": "1001", "api_hash": "a" * 32, "_hosted_cred": True}}
    calls = []

    def fake(u, m, b, bearer=""):
        calls.append(1)
        return {"ok": True, "api_id": "2002", "api_hash": "b" * 32}

    assert hg.report_invalid_and_refetch(cfg, "1001", fetch=fake) is not None
    # 立刻再来一次（新凭据又废的极端场景）→ 冷却挡下，fetch 不再发生
    assert hg.report_invalid_and_refetch(cfg, "2002", fetch=fake) is None
    assert len(calls) == 1


def test_report_invalid_same_group_back_is_failure(tmp_path, monkeypatch):
    """服务端把同一组发回来（池只剩这组/异常）→ 视为失败，不得假成功空转。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    _fp(monkeypatch)
    hg._reset_swap_cooldown_for_tests()
    cfg = {"telegram": {"api_id": "1001", "api_hash": "a" * 32, "_hosted_cred": True}}
    assert hg.report_invalid_and_refetch(
        cfg, "1001",
        fetch=lambda *a, **k: {"ok": True, "api_id": "1001", "api_hash": "a" * 32},
    ) is None
    assert cfg["telegram"]["api_id"] == "1001"  # 原值不动


def test_hot_reload_carries_hosted_cred_marker():
    """热重载保护集必须含 ``_hosted_cred`` / ``_hosted_proxy``：api_id/api_hash、
    托管标记、出口是同批注入的整体，漏任一个都会在热重载后造成错配。"""
    from src.utils.config_manager import ConfigManager

    keys = ConfigManager._HOT_RELOAD_PROTECTED_KEYS
    assert "_hosted_cred" in keys
    assert "_hosted_proxy" in keys
    assert {"api_id", "api_hash"} <= keys


def test_hosted_telegram_sends_tg_direct_and_applies_proxy(tmp_path, monkeypatch):
    """P2-⑨ 智能派发：派发请求带 tg_direct；响应带出口 → 注入 _hosted_proxy。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    _fp(monkeypatch)
    monkeypatch.setattr(hg, "_probe_tg_direct", lambda: False)  # 直连不通
    cm = _CMT(tmp_path, {"api_id": "", "api_hash": ""})
    seen = {}

    def fake(u, m, b, bearer=""):
        seen.update(b or {})
        return {"ok": True, "api_id": "3003", "api_hash": "c" * 32, "name": "grp-proxy",
                "proxy": {"scheme": "socks5", "host": "1.2.3.4", "port": 1080,
                          "username": "x", "password": "y"}}

    assert hg.ensure_hosted_telegram(cm, fetch=fake) is True
    assert seen.get("tg_direct") is False, "直连探测结论必须随派发请求上报"
    hp = cm.config["telegram"].get("_hosted_proxy")
    assert hp and hp["host"] == "1.2.3.4" and hp["port"] == 1080


def test_hosted_proxy_cleared_when_new_group_has_none(tmp_path, monkeypatch):
    """换组到无出口的组 → 旧出口必须清（否则新组带旧出口连=错配出口 IP）。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    _fp(monkeypatch)
    hg._reset_swap_cooldown_for_tests()
    monkeypatch.setattr(hg, "_probe_tg_direct", lambda: None)
    cfg = {"telegram": {"api_id": "1001", "api_hash": "a" * 32, "_hosted_cred": True,
                        "_hosted_proxy": {"scheme": "socks5", "host": "9.9.9.9", "port": 1080}}}
    got = hg.report_invalid_and_refetch(
        cfg, "1001",
        fetch=lambda *a, **k: {"ok": True, "api_id": "2002", "api_hash": "b" * 32})
    assert got == ("2002", "b" * 32)
    assert "_hosted_proxy" not in cfg["telegram"], "换到无出口组必须清掉旧出口"


def test_probe_tg_direct_soft_fails(monkeypatch):
    """预检模块不可用/抛错 → 返回 None（没有信息，绝不影响派发）。"""
    import sys
    monkeypatch.setitem(sys.modules, "src.integrations.tg_preflight", None)
    assert hg._probe_tg_direct() is None


# ── 托管识图注入（问题 #2 公网解）──────────────────────────────────────

class _CMV:
    def __init__(self, tmp_path, ai_key="cx.tok", vision=None):
        self.config_path = str(tmp_path / "config" / "config.yaml")
        Path(self.config_path).parent.mkdir(parents=True, exist_ok=True)
        self.config = {
            "ai": {"api_key": ai_key},
            "vision": dict(vision or {}),
            "licensing": {"hosted_ai": {"enabled": True, "site_url": "https://bd2026.cc"}},
        }


def test_hosted_vision_injects_gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMV(tmp_path)
    assert hg.ensure_hosted_vision(cm) is True
    v = cm.config["vision"]
    assert v["enabled"] is True and v["provider"] == "openai_compatible"
    assert v["base_url"] == "https://bd2026.cc/api/ai/v1"
    # 规范 VLM：176/140 双活都装的那个（网关侧还会统一改写 model）
    assert v["model"] == "qwen3-vl:8b-instruct"
    assert v["api_key"] == "cx.tok" and v.get("_hosted_vision") is True


def test_hosted_vision_skips_without_token(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMV(tmp_path, ai_key="")  # 还没设备令牌
    assert hg.ensure_hosted_vision(cm) is False
    assert not cm.config["vision"].get("base_url")


def test_hosted_vision_never_overrides_user_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMV(tmp_path, vision={"base_url": "http://192.168.1.9:11434", "provider": "openai_compatible"})
    assert hg.ensure_hosted_vision(cm) is False
    assert cm.config["vision"]["base_url"] == "http://192.168.1.9:11434"


def test_hosted_vision_noop_when_not_hosted(tmp_path, monkeypatch):
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    cm = _CMV(tmp_path)
    cm.config["licensing"]["hosted_ai"]["enabled"] = False
    assert hg.ensure_hosted_vision(cm) is False


# ── 混合形态（_lan_seed，2026-08-03）：LAN 直连优先 / 出网网关接管 ────────


def _seed_vision() -> dict:
    return {
        "enabled": True, "_lan_seed": True, "provider": "openai_compatible",
        "base_url": "http://192.168.0.176:11434/v1",
        "base_urls": ["http://192.168.0.176:11434/v1", "http://192.168.0.140:11434/v1"],
        "api_key": "ollama", "model": "qwen3-vl:8b-instruct",
    }


def test_hosted_vision_lan_seed_keeps_lan_when_alive(tmp_path, monkeypatch):
    """办公室内网：种子 LAN 后端可达 → 保持直连，不注入网关。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMV(tmp_path, vision=_seed_vision())
    assert hg.ensure_hosted_vision(cm, probe=lambda u: True) is False
    v = cm.config["vision"]
    assert v["base_url"] == "http://192.168.0.176:11434/v1"
    assert v["api_key"] == "ollama"
    assert not v.get("_hosted_vision")


def test_hosted_vision_lan_seed_takes_over_when_dead(tmp_path, monkeypatch):
    """外网机器：LAN 全不可达 → 网关接管（base_urls 必须一并改写，否则种子列表仍打死端点）。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    monkeypatch.delenv(hg.VISION_ENV_BASE, raising=False)
    cm = _CMV(tmp_path, vision=_seed_vision())
    assert hg.ensure_hosted_vision(cm, probe=lambda u: False) is True
    v = cm.config["vision"]
    assert v["base_url"] == "https://bd2026.cc/api/ai/v1"
    assert v["base_urls"] == ["https://bd2026.cc/api/ai/v1"]
    assert v["api_key"] == "cx.tok" and v.get("_hosted_vision") is True
    assert v["_lan_backend"]["base_url"] == "http://192.168.0.176:11434/v1"
    assert os.environ.get(hg.VISION_ENV_BASE) == "https://bd2026.cc/api/ai/v1"


def test_hosted_vision_lan_seed_restores_on_return(tmp_path, monkeypatch):
    """漫游回内网：LAN 恢复可达 → 还原直连、撤 env（防热重载把网关重放回来）。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMV(tmp_path, vision=_seed_vision())
    assert hg.ensure_hosted_vision(cm, probe=lambda u: False) is True
    assert hg.ensure_hosted_vision(cm, probe=lambda u: True) is False
    v = cm.config["vision"]
    assert v["base_url"] == "http://192.168.0.176:11434/v1"
    assert v["base_urls"] == [
        "http://192.168.0.176:11434/v1", "http://192.168.0.140:11434/v1"]
    assert v["api_key"] == "ollama"
    assert not v.get("_hosted_vision") and not v.get("_lan_backend")
    assert not os.environ.get(hg.VISION_ENV_BASE)


# ── 托管识图自动接入（_hosted_auto，2026-08-22 外网机「发图即拦」）──────


def test_hosted_vision_auto_enables_pure_cloud(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMV(tmp_path, vision={"enabled": False, "_hosted_auto": True})
    assert hg.ensure_hosted_vision(cm, probe=lambda u: False) is True
    v = cm.config["vision"]
    assert v["enabled"] is True                    # 纯内存自动开启
    assert v["base_url"] == "https://bd2026.cc/api/ai/v1"
    assert v["api_key"] == "cx.tok" and v.get("_hosted_vision") is True
    assert os.environ.get(hg.VISION_ENV_AUTO) == "1"   # 热重载回放标记


def test_hosted_vision_auto_respects_opt_out(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMV(tmp_path, vision={
        "enabled": False, "_hosted_auto": True, "hosted_opt_out": True})
    assert hg.ensure_hosted_vision(cm, probe=lambda u: False) is False
    assert cm.config["vision"]["enabled"] is False
    assert not os.environ.get(hg.VISION_ENV_AUTO)


def test_hosted_vision_auto_requires_device_token(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMV(tmp_path, ai_key="sk-user-own",
              vision={"enabled": False, "_hosted_auto": True})
    assert hg.ensure_hosted_vision(cm, probe=lambda u: False) is False
    assert cm.config["vision"]["enabled"] is False


def test_hosted_vision_plain_disabled_still_untouched(tmp_path, monkeypatch):
    """无 _hosted_auto 的纯关闭段（存量自建/显式关）→ enabled 一字不变。

    现有 ensure 仍会注入网关后端（一键开齐入站识别靠它），但不得把显式 false
    扳回 true——那是运营「全部关闭」的意图。
    """
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMV(tmp_path, vision={"enabled": False})
    assert hg.ensure_hosted_vision(cm, probe=lambda u: False) is True
    assert cm.config["vision"]["enabled"] is False
    assert not os.environ.get(hg.VISION_ENV_AUTO)


def test_cloud_light_profile_carries_vision_hosted_auto():
    """客户档契约：识图静态保守关 + 托管预授权标记并存（与语音/转写同构）。"""
    import yaml
    prof = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config" / "profiles"
         / "cloud_light.yaml").read_text(encoding="utf-8"))
    v = prof["vision"]
    assert v["enabled"] is False
    assert v["_hosted_auto"] is True


def test_config_manager_replays_vision_auto_flag():
    """回放接线钉：热重载必须透传 AITR_HOSTED_VISION_AUTO（缺了=写 overlay 关识图）。"""
    src = (Path(__file__).resolve().parents[1] / "src" / "utils"
           / "config_manager.py").read_text(encoding="utf-8")
    assert "AITR_HOSTED_VISION_AUTO" in src


# ── 托管克隆语音（混合形态）──────────────────────────────────────────────


class _CMM:
    def __init__(self, tmp_path, ai_key="cx.tok", avatar_voice=None,
                 voice_recognition=None, ai_extra=None):
        self.config_path = str(tmp_path / "config" / "config.yaml")
        Path(self.config_path).parent.mkdir(parents=True, exist_ok=True)
        self.config = {
            "ai": {"api_key": ai_key, **(ai_extra or {})},
            "licensing": {"hosted_ai": {"enabled": True, "site_url": "https://bd2026.cc"}},
        }
        if avatar_voice is not None:
            self.config["avatar_voice"] = avatar_voice
        if voice_recognition is not None:
            self.config["voice_recognition"] = voice_recognition


_HUB = "https://bd2026.cc/api/ai/hub"


def _seed_voice() -> dict:
    return {
        "enabled": True, "_lan_seed": True,
        "base_urls": ["http://192.168.0.117:7852", "http://192.168.0.140:7852"],
        "hub_fish": {"enabled": True, "base_url": "http://192.168.0.176:9000"},
    }


def test_hosted_voice_lan_alive_appends_gateway_last(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMM(tmp_path, avatar_voice=_seed_voice())
    assert hg.ensure_hosted_voice(cm, probe=lambda u: True) is True
    av = cm.config["avatar_voice"]
    assert av["base_urls"] == [
        "http://192.168.0.117:7852", "http://192.168.0.140:7852", _HUB]
    assert av["base_url"] == "http://192.168.0.117:7852"
    assert av["hub_fish"]["enabled"] is True  # LAN 可达 → 高保真链不动
    assert os.environ.get(hg.VOICE_ENV_FIRST) == "0"


def test_hosted_voice_lan_dead_gateway_first_and_hubfish_off(tmp_path, monkeypatch):
    """外网机器：网关排首位（省首条语音的死端点等待）+ hub_fish 就地禁用。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMM(tmp_path, avatar_voice=_seed_voice())
    assert hg.ensure_hosted_voice(cm, probe=lambda u: False) is True
    av = cm.config["avatar_voice"]
    assert av["base_urls"][0] == _HUB and av["base_url"] == _HUB
    assert av["base_urls"][1:] == [
        "http://192.168.0.117:7852", "http://192.168.0.140:7852"]
    assert av["hub_fish"]["enabled"] is False
    assert av["hub_fish"]["_hosted_off"] is True
    assert os.environ.get(hg.VOICE_ENV_FIRST) == "1"
    assert os.environ.get(hg.VOICE_ENV_HUBFISH_OFF) == "1"


def test_hosted_voice_back_on_lan_restores(tmp_path, monkeypatch):
    """漫游回内网：LAN 回到首位，hub_fish 自动恢复。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMM(tmp_path, avatar_voice=_seed_voice())
    assert hg.ensure_hosted_voice(cm, probe=lambda u: False) is True
    assert hg.ensure_hosted_voice(cm, probe=lambda u: True) is True
    av = cm.config["avatar_voice"]
    assert av["base_urls"] == [
        "http://192.168.0.117:7852", "http://192.168.0.140:7852", _HUB]
    assert av["hub_fish"]["enabled"] is True
    assert "_hosted_off" not in av["hub_fish"]
    assert os.environ.get(hg.VOICE_ENV_FIRST) == "0"
    assert not os.environ.get(hg.VOICE_ENV_HUBFISH_OFF)


def test_hosted_voice_never_touches_unseeded_config(tmp_path, monkeypatch):
    """用户/运维自配 TTS 端点（无 _lan_seed 标记）→ 绝不动。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    own = {"enabled": True, "base_urls": ["http://10.0.0.9:7852"]}
    cm = _CMM(tmp_path, avatar_voice=own)
    assert hg.ensure_hosted_voice(cm, probe=lambda u: True) is False
    assert cm.config["avatar_voice"]["base_urls"] == ["http://10.0.0.9:7852"]


def test_hosted_voice_requires_device_token(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMM(tmp_path, ai_key="", avatar_voice=_seed_voice())
    assert hg.ensure_hosted_voice(cm, probe=lambda u: True) is False


def test_apply_hosted_voice_replay_after_reload(tmp_path):
    """模拟热重载：盘上种子刚读回 → env 回放（apply_*）复现同一注入结论。"""
    cfg = {"avatar_voice": _seed_voice()}
    assert hg.apply_hosted_voice(
        cfg, _HUB, gateway_first=True, hub_fish_off=True) is True
    av = cfg["avatar_voice"]
    assert av["base_urls"][0] == _HUB
    assert av["hub_fish"]["enabled"] is False


# ── 托管自动接入（2026-08-19 报障群实测「客户部署与语音引擎零通路」修复）────
# 纯云客户档（cloud_light）静态保守关（enabled:false，五项部署档门禁语义不变），
# 带 _hosted_auto 预授权：持设备令牌的部署运行时纯内存自动开启并接官方网关。


def test_hosted_voice_auto_enables_pure_cloud(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMM(tmp_path, avatar_voice={"enabled": False, "_hosted_auto": True})
    assert hg.ensure_hosted_voice(cm, probe=lambda u: False) is True
    av = cm.config["avatar_voice"]
    assert av["enabled"] is True            # 纯内存自动开启
    assert av["base_urls"] == [_HUB]        # 无 LAN 种子 → 网关唯一端点
    assert os.environ.get(hg.VOICE_ENV_AUTO) == "1"   # 热重载回放标记


def test_hosted_voice_auto_respects_opt_out(tmp_path, monkeypatch):
    """用户显式退出（hosted_opt_out）→ 绝不自动开启（意图键与种子默认分离）。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMM(tmp_path, avatar_voice={
        "enabled": False, "_hosted_auto": True, "hosted_opt_out": True})
    assert hg.ensure_hosted_voice(cm, probe=lambda u: False) is False
    assert cm.config["avatar_voice"]["enabled"] is False
    assert not os.environ.get(hg.VOICE_ENV_AUTO)


def test_hosted_voice_auto_requires_device_token(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMM(tmp_path, ai_key="sk-user-own",
              avatar_voice={"enabled": False, "_hosted_auto": True})
    assert hg.ensure_hosted_voice(cm, probe=lambda u: False) is False
    assert cm.config["avatar_voice"]["enabled"] is False


def test_hosted_voice_plain_disabled_still_untouched(tmp_path, monkeypatch):
    """无 _hosted_auto 的纯关闭段（存量自建/显式关）→ 行为一字不变。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMM(tmp_path, avatar_voice={"enabled": False})
    assert hg.ensure_hosted_voice(cm, probe=lambda u: False) is False
    assert cm.config["avatar_voice"]["enabled"] is False


def test_apply_hosted_voice_auto_replay_survives_reload(tmp_path):
    """热重载把 enabled 抹回 false → 回放带 auto_enable 恢复；不带则不碰。"""
    cfg = {"avatar_voice": {"enabled": False, "_hosted_auto": True}}
    assert hg.apply_hosted_voice(
        cfg, _HUB, gateway_first=True, hub_fish_off=False,
        auto_enable=True) is True
    assert cfg["avatar_voice"]["enabled"] is True
    assert cfg["avatar_voice"]["base_urls"] == [_HUB]
    cfg2 = {"avatar_voice": {"enabled": False, "_hosted_auto": True}}
    assert hg.apply_hosted_voice(
        cfg2, _HUB, gateway_first=True, hub_fish_off=False) is False
    assert cfg2["avatar_voice"]["enabled"] is False


def test_cloud_light_profile_carries_hosted_auto():
    """客户档契约：静态保守关 + 托管预授权标记并存（部署档五门禁的前提）。"""
    import yaml
    prof = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config" / "profiles"
         / "cloud_light.yaml").read_text(encoding="utf-8"))
    av = prof["avatar_voice"]
    assert av["enabled"] is False
    assert av["_hosted_auto"] is True


def test_config_manager_replays_auto_flag():
    """回放接线钉：热重载必须透传 AITR_HOSTED_VOICE_AUTO（缺了=写 overlay 关语音）。"""
    src = (Path(__file__).resolve().parents[1] / "src" / "utils"
           / "config_manager.py").read_text(encoding="utf-8")
    assert "AITR_HOSTED_VOICE_AUTO" in src


# ── 托管语音识别（混合形态）──────────────────────────────────────────────


def _seed_asr() -> dict:
    return {
        "enabled": True, "_lan_seed": True, "provider": "openai_compatible",
        "base_url": "http://192.168.0.176:8765/v1", "api_key": "local",
        "model": "large-v3-turbo", "timeout": 20, "max_retries": 0,
        "fallback": [
            {"provider": "avatar_whisper", "base_url": "http://192.168.0.140:7854"}],
    }


_GW = "https://bd2026.cc/api/ai/v1"


def test_hosted_asr_lan_alive_appends_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMM(tmp_path, voice_recognition=_seed_asr())
    assert hg.ensure_hosted_asr(cm, probe=lambda u: True) is True
    vr = cm.config["voice_recognition"]
    assert vr["base_url"] == "http://192.168.0.176:8765/v1"  # 主转写保持 LAN
    assert vr["api_key"] == "local"
    assert vr["fallback"][0]["provider"] == "avatar_whisper"
    tail = vr["fallback"][-1]
    assert tail["base_url"] == _GW
    assert tail["api_key"] == hg.HOSTED_KEY_PLACEHOLDER
    assert tail["_hosted_asr_entry"] is True
    assert os.environ.get(hg.ASR_ENV_FIRST) == "0"


def test_hosted_asr_lan_dead_swaps_primary(tmp_path, monkeypatch):
    """外网机器：主转写直接换网关（原 LAN 主位入 _lan_primary 暂存），不吃 20s 死端点等待。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMM(tmp_path, voice_recognition=_seed_asr())
    assert hg.ensure_hosted_asr(cm, probe=lambda u: False) is True
    vr = cm.config["voice_recognition"]
    assert vr["base_url"] == _GW
    assert vr["api_key"] == hg.HOSTED_KEY_PLACEHOLDER
    assert vr["_lan_primary"]["base_url"] == "http://192.168.0.176:8765/v1"
    assert all(not e.get("_hosted_asr_entry") for e in vr["fallback"])
    assert os.environ.get(hg.ASR_ENV_FIRST) == "1"


def test_hosted_asr_back_on_lan_restores_primary(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMM(tmp_path, voice_recognition=_seed_asr())
    assert hg.ensure_hosted_asr(cm, probe=lambda u: False) is True
    assert hg.ensure_hosted_asr(cm, probe=lambda u: True) is True
    vr = cm.config["voice_recognition"]
    assert vr["base_url"] == "http://192.168.0.176:8765/v1"
    assert vr["api_key"] == "local"
    assert vr["fallback"][-1]["_hosted_asr_entry"] is True


def test_hosted_asr_untouched_without_seed(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    own = {"enabled": True, "provider": "openai_compatible",
           "base_url": "http://10.0.0.9:8765/v1"}
    cm = _CMM(tmp_path, voice_recognition=own)
    assert hg.ensure_hosted_asr(cm, probe=lambda u: True) is False
    assert "fallback" not in cm.config["voice_recognition"]


# ── 托管转写自动接入（_hosted_auto，2026-08-20 内测群「语音无法识别」）──────


def test_hosted_asr_auto_enables_pure_cloud(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMM(tmp_path, voice_recognition={"enabled": False, "_hosted_auto": True})
    assert hg.ensure_hosted_asr(cm, probe=lambda u: False) is True
    vr = cm.config["voice_recognition"]
    assert vr["enabled"] is True                    # 纯内存自动开启
    assert vr["provider"] == "openai_compatible"    # 无 LAN 种子 → 网关即主转写
    assert vr["base_url"] == _GW
    assert vr["api_key"] == hg.HOSTED_KEY_PLACEHOLDER
    assert os.environ.get(hg.ASR_ENV_AUTO) == "1"   # 热重载回放标记


def test_hosted_asr_auto_respects_opt_out(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMM(tmp_path, voice_recognition={
        "enabled": False, "_hosted_auto": True, "hosted_opt_out": True})
    assert hg.ensure_hosted_asr(cm, probe=lambda u: False) is False
    assert cm.config["voice_recognition"]["enabled"] is False
    assert not os.environ.get(hg.ASR_ENV_AUTO)


def test_hosted_asr_auto_requires_device_token(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMM(tmp_path, ai_key="sk-user-own",
              voice_recognition={"enabled": False, "_hosted_auto": True})
    assert hg.ensure_hosted_asr(cm, probe=lambda u: False) is False
    assert cm.config["voice_recognition"]["enabled"] is False


def test_hosted_asr_plain_disabled_still_untouched(tmp_path, monkeypatch):
    """无 _hosted_auto 的纯关闭段（存量自建/显式关）→ 行为一字不变。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMM(tmp_path, voice_recognition={"enabled": False})
    assert hg.ensure_hosted_asr(cm, probe=lambda u: False) is False
    assert cm.config["voice_recognition"]["enabled"] is False


def test_apply_hosted_asr_auto_replay_survives_reload(tmp_path):
    """热重载把 enabled 抹回 false → 回放带 auto_enable 恢复；不带则不碰。"""
    cfg = {"voice_recognition": {"enabled": False, "_hosted_auto": True}}
    assert hg.apply_hosted_asr(cfg, _GW, gateway_first=True,
                               auto_enable=True) is True
    assert cfg["voice_recognition"]["enabled"] is True
    assert cfg["voice_recognition"]["base_url"] == _GW
    cfg2 = {"voice_recognition": {"enabled": False, "_hosted_auto": True}}
    assert hg.apply_hosted_asr(cfg2, _GW, gateway_first=True) is False
    assert cfg2["voice_recognition"]["enabled"] is False


def test_cloud_light_profile_carries_asr_hosted_auto():
    """客户档契约：转写静态保守关 + 托管预授权标记并存（与语音同构）。"""
    import yaml
    prof = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config" / "profiles"
         / "cloud_light.yaml").read_text(encoding="utf-8"))
    vr = prof["voice_recognition"]
    assert vr["enabled"] is False
    assert vr["_hosted_auto"] is True


def test_config_manager_replays_asr_auto_flag():
    """回放接线钉：热重载必须透传 AITR_HOSTED_ASR_AUTO（缺了=写 overlay 关转写）。"""
    src = (Path(__file__).resolve().parents[1] / "src" / "utils"
           / "config_manager.py").read_text(encoding="utf-8")
    assert "AITR_HOSTED_ASR_AUTO" in src


# ── 托管态下的调用端令牌解析 ────────────────────────────────────────────


def test_svc_headers_falls_back_to_device_token(tmp_path, monkeypatch):
    """客户机没有集群令牌文件 → 非回环端点带设备令牌（网关验它；LAN 401 同旧行为）。"""
    from src.ai.avatar_voice import AvatarVoiceClient

    monkeypatch.setenv("AITR_HOSTED_AI_KEY", "cx.dev.tok")
    client = AvatarVoiceClient({
        "stt": {"token_file": str(tmp_path / "no-such-token.txt")},
    })
    assert client._svc_headers(_HUB) == {"X-AH-Svc": "cx.dev.tok"}
    assert client._svc_headers("http://127.0.0.1:7852") is None  # 回环恒不带头
    monkeypatch.setenv("AITR_HOSTED_AI_KEY", "sk-not-device-token")
    assert client._svc_headers(_HUB) is None  # 非 cx. 形态不冒充集群令牌


async def test_openai_transcriber_resolves_hosted_key_at_call_time(tmp_path, monkeypatch):
    """api_key='hosted' 占位 → 调用时取 env 设备令牌；未就绪则如实失败交 fallback。"""
    import openai

    from src.voice_transcriber import OpenAITranscriber

    seen: dict = {}

    def fake_client(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop-here")

    monkeypatch.setattr(openai, "OpenAI", fake_client)
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF0000WAVE")
    t = OpenAITranscriber({
        "provider": "openai_compatible", "api_key": "hosted",
        "base_url": _GW, "timeout": 5,
    })
    monkeypatch.setenv("AITR_HOSTED_AI_KEY", "cx.now")
    assert await t._transcribe_impl(str(wav), "auto") is None  # 假客户端抛错 → 软失败
    assert seen.get("api_key") == "cx.now"

    seen.clear()
    monkeypatch.delenv("AITR_HOSTED_AI_KEY", raising=False)
    assert await t._transcribe_impl(str(wav), "auto") is None
    assert not seen  # 令牌未就绪 → 根本不构建客户端，直接交 fallback


def test_resolve_instance_id_from_chengjie_instances(monkeypatch):
    monkeypatch.delenv("AITR_INSTANCE_ID", raising=False)
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    monkeypatch.setenv(
        "AITR_DATA_DIR", r"D:\chengjie-instances\zhiliao_pilot\data")
    assert hg.resolve_instance_id() == "zhiliao_pilot"


def test_resolve_instance_id_ignores_desktop_data_dir(monkeypatch):
    monkeypatch.delenv("AITR_INSTANCE_ID", raising=False)
    monkeypatch.setenv("AITR_DATA_DIR", r"C:\Users\x\AppData\chatx\data")
    assert hg.resolve_instance_id() == ""


def test_fetch_device_token_forwards_instance_id(monkeypatch):
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    monkeypatch.setenv("AITR_INSTANCE_ID", "zhiliao_acme")
    _fp(monkeypatch)
    seen = {}

    def fake_fetch(url, method, body):
        seen.update(body)
        return {"ok": True, "token": "cx.x", "model": "m", "exp": 9}

    got = hg.fetch_device_token(fetch=fake_fetch)
    assert got["ok"] and got["instance_id"] == "zhiliao_acme"
    assert seen["instance_id"] == "zhiliao_acme"
    assert seen["source"] == "hosted"


def test_fetch_device_token_desktop_skips_instance_id(monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    monkeypatch.setenv("AITR_INSTANCE_ID", "should-ignore")
    _fp(monkeypatch)
    seen = {}

    def fake_fetch(url, method, body):
        seen.update(body)
        return {"ok": True, "token": "cx.x", "model": "m", "exp": 9}

    got = hg.fetch_device_token(fetch=fake_fetch)
    assert "instance_id" not in seen
    assert seen["source"] == "desktop"
    assert got.get("instance_id") == ""


# ── 托管嵌入（2026-08-28 P0-1 配套；B126 根因的客户端半边）───────────────────
# 服务端补 /api/ai/v1/embeddings 只救「没配嵌入端点」的客户档；**内测档 overlay
# 把嵌入端点钉在 LAN**（140/176:11434），外网机器那两个地址不可达 → 客户端连败
# 3 次熔断 120s → 语义记忆召回全程降级关键词。这批机器必须客户端改道。

_LAN_EMBED = {
    "embedding_base_url": "http://192.168.0.140:11434",
    "embedding_base_urls": ["http://192.168.0.140:11434",
                            "http://192.168.0.176:11434"],
    "embedding_model": "bge-m3",
}


def test_hosted_embed_lan_dead_switches_to_gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMM(tmp_path, ai_extra=dict(_LAN_EMBED))
    assert hg.ensure_hosted_embed(cm, probe=lambda u: False) is True
    ai = cm.config["ai"]
    assert ai["embedding_base_urls"] == [_GW]
    assert ai["embedding_base_url"] == _GW
    assert ai["_hosted_embed"] is True
    # api_key 刻意不写：ai_client 缺省继承 ai.api_key（设备令牌），换新自动跟随
    assert "embedding_api_key" not in ai
    assert os.environ.get(hg.EMBED_ENV_BASE) == _GW


def test_hosted_embed_lan_alive_leaves_config_alone(tmp_path, monkeypatch):
    """办公室机器：LAN 嵌入端点可达 → 一个字都不改（低延迟直连）。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMM(tmp_path, ai_extra=dict(_LAN_EMBED))
    assert hg.ensure_hosted_embed(cm, probe=lambda u: True) is False
    assert cm.config["ai"]["embedding_base_urls"] == _LAN_EMBED["embedding_base_urls"]
    assert "_hosted_embed" not in cm.config["ai"]


def test_hosted_embed_back_on_lan_restores(tmp_path, monkeypatch):
    """漫游回内网：还原原端点（含原模型名），不把网关粘住。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMM(tmp_path, ai_extra=dict(_LAN_EMBED))
    assert hg.ensure_hosted_embed(cm, probe=lambda u: False) is True
    assert hg.ensure_hosted_embed(cm, probe=lambda u: True) is False
    ai = cm.config["ai"]
    assert ai["embedding_base_urls"] == _LAN_EMBED["embedding_base_urls"]
    assert ai["embedding_base_url"] == _LAN_EMBED["embedding_base_url"]
    assert "_hosted_embed" not in ai
    assert not os.environ.get(hg.EMBED_ENV_BASE)


def test_hosted_embed_never_touches_public_endpoint(tmp_path, monkeypatch):
    """客户自配云端嵌入（OpenAI / 自建域名）→ 不可达也绝不改写。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    for url in ("https://api.openai.com", "http://embed.customer.example:11434"):
        cm = _CMM(tmp_path, ai_extra={"embedding_base_url": url,
                                      "embedding_model": "text-embedding-3-small"})
        assert hg.ensure_hosted_embed(cm, probe=lambda u: False) is False
        assert cm.config["ai"]["embedding_base_url"] == url


def test_hosted_embed_mixed_public_and_lan_not_touched(tmp_path, monkeypatch):
    """混配（LAN + 云）：只要有一个公网端点就是用户的明示选择，整段不动。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMM(tmp_path, ai_extra={
        "embedding_base_urls": ["http://192.168.0.140:11434", "https://api.openai.com"],
        "embedding_model": "bge-m3"})
    assert hg.ensure_hosted_embed(cm, probe=lambda u: False) is False
    assert len(cm.config["ai"]["embedding_base_urls"]) == 2


def test_hosted_embed_no_endpoint_is_left_to_chat_fallback(tmp_path, monkeypatch):
    """未配嵌入端点＝ai_client 本就回落对话客户端（即网关）→ 这里不插手。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMM(tmp_path)
    assert hg.ensure_hosted_embed(cm, probe=lambda u: False) is False
    assert "embedding_base_url" not in cm.config["ai"]


def test_hosted_embed_requires_device_token(tmp_path, monkeypatch):
    """用户自有 Key（非 cx.）→ 网关鉴不了权，绝不改道（否则嵌入全 401）。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMM(tmp_path, ai_key="sk-user-own", ai_extra=dict(_LAN_EMBED))
    assert hg.ensure_hosted_embed(cm, probe=lambda u: False) is False
    assert cm.config["ai"]["embedding_base_url"] == _LAN_EMBED["embedding_base_url"]


def test_hosted_embed_fills_model_when_blank(tmp_path, monkeypatch):
    """模型名为空时 ai_client.embed() 直接返回空、一次请求都不发 → 必须补非空值。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CMM(tmp_path, ai_extra={"embedding_base_url": "http://192.168.0.140:11434",
                                  "embedding_model": ""})
    assert hg.ensure_hosted_embed(cm, probe=lambda u: False) is True
    assert cm.config["ai"]["embedding_model"] == hg.HOSTED_EMBED_MODEL


def test_apply_hosted_embed_replay_after_reload(tmp_path):
    """热重载回放：重新读入的 LAN 配置必须再次被接管（否则语义记忆重新降级）。"""
    cfg = {"ai": {"api_key": "cx.tok", **_LAN_EMBED}}
    assert hg.apply_hosted_embed(cfg, _GW) is True
    assert cfg["ai"]["embedding_base_urls"] == [_GW]
    # 幂等：再来一次不叠加、不把网关自己存进 _lan_embed
    assert hg.apply_hosted_embed(cfg, _GW) is True
    assert cfg["ai"]["_lan_embed"]["embedding_base_urls"] == \
        _LAN_EMBED["embedding_base_urls"]


def test_is_private_endpoint():
    assert hg.is_private_endpoint("http://192.168.0.140:11434") is True
    assert hg.is_private_endpoint("http://127.0.0.1:11434") is True
    assert hg.is_private_endpoint("http://10.1.2.3") is True
    assert hg.is_private_endpoint("http://localhost:1234") is True
    assert hg.is_private_endpoint("https://api.openai.com") is False
    assert hg.is_private_endpoint("https://bd2026.cc/api/ai/v1") is False
    assert hg.is_private_endpoint("") is False
    assert hg.is_private_endpoint("not a url") is False
