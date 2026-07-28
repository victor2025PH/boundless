"""三隔离在「真实连接那一刻」是否真的生效 —— 本轮最重要的一组门禁。

2026-07-27 实测发现的空转：本机开 `platform_login.telegram.companion_runtime`，
协议号实际由 `TelegramCompanionWorker` → A 线 `TelegramClient` 拉起，而那条路
**既不查中央池也不带设备字段**。三隔离于是全是纸面的：

  · 凭据：扫码用池凭据、重连用配置里那组。当时没出事只因为配置里那组恰好就是
    池里唯一那组（巧合）；池里一旦有第二组，就是「session 与 api_id 错配」——
    Telegram 明确的风控信号。
  · 指纹：扫码时报派生机型（MacBook Pro），重连又回 pyrogram 默认值 ——
    **每次重连都在换设备**，比根本不做指纹更可疑。
  · 出口：池按付费档下发的独立 IP 根本没接上。

「功能写了但那条路没走到」这种失效不会有任何报错，只能靠门禁钉住。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.integrations import credpool_bridge as cb
from src.integrations import telegram_companion_worker as tcw
from tests._source_block import source_block

# 按路径读而不导入：telegram_client 会 import pyrogram，而 pyrogram 的 sync 包装
# 在**导入时**就要求有事件循环，纯静态断言没必要为此付代价。
_TG_CLIENT = Path(__file__).resolve().parents[1] / "src" / "client" / "telegram_client.py"


def _worker(meta=None, proxy_id=""):
    return tcw.TelegramCompanionWorker(
        {"account_id": "8438080491", "platform": "telegram", "mode": "protocol",
         "proxy_id": proxy_id, "meta": dict(meta or {"session_string": "s"})},
        {"platform_login": {"telegram": {
            "credpool": {"enabled": True, "base_url": "http://127.0.0.1:1",
                         "service_token": "t"},
            "device_fingerprint": {"enabled": True}}}})


def _patch_alloc(monkeypatch, alloc):
    async def _fake(config, account=None, pool_key=None):
        return alloc
    monkeypatch.setattr(cb, "aresolve_for_account", _fake)


# ── 凭据必须来自解析结果，不能让 TelegramClient 回落全局 telegram.api_id ────

def test_overlay_carries_pool_credentials(monkeypatch):
    _patch_alloc(monkeypatch, cb.Allocation(777, "pool_hash", "credpool", "gold"))
    ov = asyncio.run(_worker()._isolation_overlay())
    assert ov["api_id"] == 777 and ov["api_hash"] == "pool_hash", \
        "连接必须用解析出来的凭据；漏了这一步 TelegramClient 会回落全局 api_id"


def test_worker_refuses_to_start_without_credentials(monkeypatch):
    """池内号解析不到 → 抛错保持 stopped，绝不用配置凭据顶上（credpool 契约 §4b）。"""
    _patch_alloc(monkeypatch, None)
    w = _worker(meta={"session_string": "s", cb.META_KEY: "chatx:x"})
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(w._isolation_overlay())
    assert "错配" in str(exc.value), "错误信息要说清为什么不上线，否则运维会以为是坏了"


def test_legacy_account_credentials_untouched(monkeypatch):
    """存量号回落自带凭据 → **不覆盖**：两条读法不等价，不动我不拥有的东西。"""
    _patch_alloc(monkeypatch, cb.Allocation(25784476, "cfg_hash", "config"))
    ov = asyncio.run(_worker()._isolation_overlay())
    assert "api_id" not in ov and "api_hash" not in ov, \
        "source=config 时不该覆盖，值相同也不行（get_telegram_config 可能带 env 覆盖）"


def test_cached_pool_credentials_are_applied(monkeypatch):
    """降级期用的缓存凭据同样要落到连接上（否则降级期反而错配）。"""
    _patch_alloc(monkeypatch, cb.Allocation(777, "cached_hash", "credpool_cache", "gold"))
    ov = asyncio.run(_worker(meta={"session_string": "s", cb.META_KEY: "chatx:x"})
                     ._isolation_overlay())
    assert ov["api_id"] == 777 and ov["api_hash"] == "cached_hash"


def test_start_merges_overlay_into_account_cfg():
    """静态钉住 start() 真的把 overlay 合进了 account_cfg（否则解析了也用不上）。"""
    src = source_block(tcw, "    async def start(")
    assert "_isolation_overlay()" in src, "start 必须解析隔离 overlay"
    assert "_cfg.update(" in src, "overlay 必须合进 account_cfg"
    assert "account_cfg=_cfg" in src, "传给 TelegramClient 的必须是合并后的那份"


# ── 设备指纹必须落到 pyrogram Client 上 ───────────────────────────────────

def test_overlay_carries_device_fingerprint(monkeypatch):
    _patch_alloc(monkeypatch, cb.Allocation(1, "h", "credpool"))
    from src.integrations.device_fingerprint import META_FP_KEY

    ov = asyncio.run(_worker(meta={
        "session_string": "s",
        META_FP_KEY: {"device_model": "MacBook Pro", "system_version": "14.5",
                      "app_version": "Telegram macOS 10.9"}})._isolation_overlay())
    assert ov["device_model"] == "MacBook Pro", "必须用该号落库的指纹本体"
    assert ov["system_version"] == "14.5" and "Telegram" in ov["app_version"]


def test_telegram_client_passes_device_fields_to_pyrogram():
    """A 线客户端必须把设备字段喂给 Client——只放进 account_cfg 不用等于没做。"""
    src = source_block(_TG_CLIENT, "    async def initialize(")
    assert "_device_fp" in src, "Client 构造必须带设备指纹"
    for field in ("device_model", "system_version", "app_version"):
        assert field in src, f"缺少 {field}"


def test_legacy_account_gets_no_device_fields(monkeypatch):
    """存量号 meta 无指纹 → 不注入，保持 pyrogram 默认（换设备才是可疑的）。"""
    _patch_alloc(monkeypatch, cb.Allocation(1, "h", "config"))
    ov = asyncio.run(_worker()._isolation_overlay())
    for field in ("device_model", "system_version", "app_version"):
        assert field not in ov


def test_pool_off_does_not_touch_credentials(monkeypatch):
    """没开池的部署（所有客户桌面）行为必须一字不变：不覆盖凭据、不因解析失败拒起。"""
    called = {"n": 0}

    async def _fake(config, account=None, pool_key=None):
        called["n"] += 1
        return None

    monkeypatch.setattr(cb, "aresolve_for_account", _fake)
    w = tcw.TelegramCompanionWorker(
        {"account_id": "a", "platform": "telegram", "mode": "protocol",
         "meta": {"session_string": "s"}},
        {"platform_login": {"telegram": {"credpool": {"enabled": False}}}})
    ov = asyncio.run(w._isolation_overlay())
    assert "api_id" not in ov and "api_hash" not in ov, \
        "没开池不该覆盖凭据——TelegramClient 自己那条读法可能带 env 覆盖，不等价"
    assert called["n"] == 0, "没开池连解析都不该发起（省一次无谓 I/O）"


def test_fingerprint_works_without_pool(monkeypatch):
    """指纹独立于中央池（本地种子派生）——只开指纹也要生效。"""
    from src.integrations.device_fingerprint import META_FP_KEY

    w = tcw.TelegramCompanionWorker(
        {"account_id": "a", "platform": "telegram", "mode": "protocol",
         "meta": {"session_string": "s",
                  # 三个字段必须齐全：字段不全的脏指纹被刻意当作「没有」
                  META_FP_KEY: {"device_model": "iPad Pro",
                                "system_version": "17.4",
                                "app_version": "Telegram iOS 10.9"}}},
        {"platform_login": {"telegram": {
            "credpool": {"enabled": False},
            "device_fingerprint": {"enabled": True}}}})
    ov = asyncio.run(w._isolation_overlay())
    assert ov["device_model"] == "iPad Pro"


# ── 池下发的独立出口要接上，但不能盖掉运营显式绑定的代理 ──────────────────

def test_pool_proxy_is_applied(monkeypatch):
    _patch_alloc(monkeypatch, cb.Allocation(
        1, "h", "credpool", "gold",
        {"scheme": "socks5", "host": "1.2.3.4", "port": 1080}))
    ov = asyncio.run(_worker()._isolation_overlay())
    assert ov["proxy"]["hostname"] == "1.2.3.4", "付费档的独立出口必须真的接上"


def test_explicit_proxy_binding_wins(monkeypatch):
    """账号上显式绑定的代理代表运营意图，不该被池自动分配悄悄覆盖。"""
    _patch_alloc(monkeypatch, cb.Allocation(
        1, "h", "credpool", "gold",
        {"scheme": "socks5", "host": "1.2.3.4", "port": 1080}))
    ov = asyncio.run(_worker(proxy_id="px-1")._isolation_overlay())
    assert "proxy" not in ov, "有显式 proxy_id 时不注入池出口，交给 _resolve_proxy"


def test_telegram_client_falls_back_to_account_proxy():
    src = source_block(_TG_CLIENT, "    async def initialize(")
    assert "_account_proxy" in src, "池下发的出口必须在 Client 构造时被用上"
    assert src.index("self._resolve_proxy()") < src.index("_account_proxy"), \
        "顺序必须是「显式绑定优先、池出口兜底」"


# ── 两条 worker 路径都要接隔离，别再出现「只接了不走的那条」────────────────

def test_both_worker_paths_wire_isolation():
    """companion_runtime 开/关走的是两个 worker，两边都得接（这次就是漏了开的那条）。"""
    from src.integrations import account_orchestrator as ao

    b_line = source_block(ao, "    async def start(")
    assert "aresolve_for_account" in b_line and "client_kwargs_for_account" in b_line, \
        "B 线 TelegramProtocolWorker 必须接凭据池与指纹"
    a_line = source_block(tcw, "    async def _isolation_overlay(")
    assert "aresolve_for_account" in a_line and "client_kwargs_for_account" in a_line, \
        "A 线 companion worker 同样必须接（本机实际走的就是这条）"
