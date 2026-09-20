"""每账号设备指纹门禁（协议多开防聚类连坐）。

守住四条不变量：
1. **按账号稳定**：同一种子永远派生同一套指纹——会话建成后换设备型号＝自曝异常；
2. **内部自洽**：机型与系统版本必须像同一台真机，混搭本身就是伪造特征；
3. **默认关 + 只对新号生效**：存量账号无种子，行为一字不变；
4. **登录与 runner 同源**：两处必须由同一种子派生，否则同一号两副面孔。
"""
from __future__ import annotations

import inspect

from src.integrations import account_orchestrator as ao
from src.integrations import device_fingerprint as fp
from src.integrations import telegram_protocol_login as tpl
from tests._source_block import source_block


ON = {"platform_login": {"telegram": {"device_fingerprint": {"enabled": True}}}}


# ── 开关 ─────────────────────────────────────────────────────────────────

def test_disabled_by_default():
    assert fp.enabled({}) is False
    assert fp.enabled({"platform_login": {"telegram": {}}}) is False
    assert fp.enabled(ON) is True


def test_disabled_yields_no_kwargs():
    assert fp.client_kwargs({}, "seed") == {}


def test_no_seed_yields_no_kwargs():
    """存量账号 meta 里没有种子 → 不改行为。"""
    assert fp.client_kwargs(ON, "") == {}
    assert fp.client_kwargs_for_account(ON, {"meta": {}}) == {}
    assert fp.client_kwargs_for_account(ON, None) == {}


# ── 稳定性与差异化 ────────────────────────────────────────────────────────

def test_same_seed_always_same_fingerprint():
    a = fp.build_fingerprint("abc123")
    b = fp.build_fingerprint("abc123")
    assert a == b


def test_seeds_spread_across_profiles():
    """一批号不能全落在同一套指纹上，否则等于没隔离。"""
    seeds = [fp.new_device_seed() for _ in range(60)]
    combos = {tuple(sorted(fp.build_fingerprint(s).items())) for s in seeds}
    assert len(combos) >= 5, f"指纹分布过于集中：仅 {len(combos)} 种"


def test_new_seed_is_unique():
    assert fp.new_device_seed() != fp.new_device_seed()


# ── 自洽性（混搭本身就是伪造特征）────────────────────────────────────────

def test_model_and_system_are_coherent():
    for seed in (fp.new_device_seed() for _ in range(80)):
        f = fp.build_fingerprint(seed)
        model, system = f["device_model"], f["system_version"]
        if model in ("MacBook Pro", "MacBook Air", "iMac"):
            assert system.startswith("macOS"), f"苹果机型配了非 macOS：{f}"
        else:
            assert not system.startswith("macOS"), f"非苹果机型配了 macOS：{f}"


def test_fingerprint_fields_are_complete_and_nonempty():
    f = fp.build_fingerprint("x")
    assert set(f) == {"device_model", "system_version", "app_version"}
    assert all(v.strip() for v in f.values())


# ── 接线：登录与 runner 必须同源 ──────────────────────────────────────────

def test_login_accepts_device_kwargs_and_applies_them():
    sig = inspect.signature(tpl.TelegramQrLogin.__init__)
    assert "device_kwargs" in sig.parameters
    src = source_block(tpl, "class TelegramQrLogin")
    assert "client_kwargs.update(self.device_kwargs)" in src


def test_provider_generates_and_persists_seed():
    src = source_block(tpl, "def make_provider(")
    assert "new_device_seed" in src, "扫码时必须生成种子"
    assert "_fp.META_KEY" in src, "种子必须随账号落库，否则 runner 复现不出同一指纹"


def test_worker_derives_fingerprint_from_account_meta():
    src = source_block(ao, "class TelegramProtocolWorker")
    assert "client_kwargs_for_account" in src


def test_meta_key_is_stable_contract():
    assert fp.META_KEY == "device_seed"
    assert fp.META_FP_KEY == "device_fp"


# ── 落库的是指纹本体：机型表演进不得改掉存量账号的设备身份 ────────────────

def test_stored_fingerprint_wins_over_seed_derivation():
    """哪怕种子还在，也必须用落库那一套——它才是建会话时真报出去的值。"""
    stored = {"device_model": "OldBox", "system_version": "Windows 7",
              "app_version": "1.0.0"}
    account = {"meta": {fp.META_KEY: "seed-x", fp.META_FP_KEY: stored}}
    assert fp.client_kwargs_for_account(ON, account) == stored
    assert fp.client_kwargs_for_account(ON, account) != fp.build_fingerprint("seed-x")


def test_seed_derivation_is_the_fallback_for_legacy_accounts():
    account = {"meta": {fp.META_KEY: "seed-x"}}
    assert fp.client_kwargs_for_account(ON, account) == fp.build_fingerprint("seed-x")


def test_corrupt_stored_fingerprint_is_ignored():
    for bad in ({}, {"device_model": "x"}, {"device_model": "x", "system_version": "",
                                            "app_version": "1"}, "notadict", None):
        account = {"meta": {fp.META_KEY: "seed-x", fp.META_FP_KEY: bad}}
        assert fp.client_kwargs_for_account(ON, account) == fp.build_fingerprint("seed-x")


def test_stored_fingerprint_still_respects_the_switch():
    account = {"meta": {fp.META_FP_KEY: {"device_model": "a", "system_version": "b",
                                         "app_version": "c"}}}
    assert fp.client_kwargs_for_account({}, account) == {}


def test_provider_persists_fingerprint_body_not_just_seed():
    src = source_block(tpl, "def make_provider(")
    assert "_fp.META_FP_KEY" in src, "必须落指纹本体，否则机型表刷新会改掉存量账号身份"
