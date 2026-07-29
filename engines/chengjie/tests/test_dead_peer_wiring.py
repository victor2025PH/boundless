# -*- coding: utf-8 -*-
"""死 peer 登记表 → sender A 线接线门禁（gated 默认关）。

不变量：
- flag 关（默认）：永久错误照旧走 _handle_send_exc，**不**落黑名单（生产零变化）；
- flag 开：INPUT_USER_DEACTIVATED 失败 → 落黑名单 → 下次同 peer send 开头直接拦截
  （client.send_message 不再被调用），修「死号每 15min 重发累积风控」；
- flag 开：peer_invalid（可自愈）走预热重发，**不**被误拉黑。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.ops.dead_peer_registry import reset_singleton

DEAD = 5433982810      # 私聊 peer（模拟已注销用户）
DEACTIVATED = "Telegram says: [400 INPUT_USER_DEACTIVATED] - The target user has been deleted"


def _mk_tc(*, config, send_side_effects, dialogs_chats=()):
    from src.client.telegram_client import TelegramClient
    tc = TelegramClient.__new__(TelegramClient)
    tc.config = config
    tc.account_id = "default"
    # logger 是只读 property（真实类提供），测试不覆写；接线里的 logger.info 直接走真 logger
    tc._presend_blocked = MagicMock(return_value=False)
    tc._presend_pace = AsyncMock()
    tc._postsend_record_count = MagicMock()
    tc._handle_send_exc = MagicMock()

    calls = {"send": 0}

    async def _send_message(chat_id, text):
        idx = min(calls["send"], len(send_side_effects) - 1)
        calls["send"] += 1
        eff = send_side_effects[idx]
        if isinstance(eff, BaseException):
            raise eff
        return eff

    async def _get_dialogs(limit=0):
        for cid in dialogs_chats:
            yield SimpleNamespace(chat=SimpleNamespace(id=cid))

    tc.client = SimpleNamespace(send_message=_send_message, get_dialogs=_get_dialogs)
    tc._send_calls = calls
    return tc


def _cfg(tmp_path, *, enabled: bool, ttl_by_reason=None):
    dpc = {"enabled": enabled}
    if ttl_by_reason is not None:
        dpc["ttl_by_reason"] = ttl_by_reason
    return SimpleNamespace(
        config={"ops": {"dead_peer_registry": dpc}},
        config_path=str(tmp_path / "config.yaml"),
    )


@pytest.mark.asyncio
async def test_flag_off_no_blocklist(tmp_path):
    reset_singleton()
    tc = _mk_tc(config=_cfg(tmp_path, enabled=False),
                send_side_effects=[Exception(DEACTIVATED)])
    ok, _ = await tc._send_text_guarded(DEAD, "在吗")
    assert ok is False
    assert tc._handle_send_exc.call_count == 1        # flag 关：照旧走风控分级
    # 无黑名单文件写出（gated 关时 record 不触发）
    assert not (tmp_path / "dead_peers.json").exists()


@pytest.mark.asyncio
async def test_flag_on_records_and_then_blocks(tmp_path):
    reset_singleton()
    # 第一次：注销错误 → 记录拉黑
    tc = _mk_tc(config=_cfg(tmp_path, enabled=True),
                send_side_effects=[Exception(DEACTIVATED)])
    ok, _ = await tc._send_text_guarded(DEAD, "在吗")
    assert ok is False
    assert tc._send_calls["send"] == 1
    assert (tmp_path / "dead_peers.json").exists()     # 已落盘

    # 第二次：同 peer 再发 → 开头闸拦截，client.send_message 完全不被调用
    reset_singleton()                                   # 模拟新进程从盘重载
    tc2 = _mk_tc(config=_cfg(tmp_path, enabled=True),
                 send_side_effects=[Exception("should-not-be-called")])
    ok2, _ = await tc2._send_text_guarded(DEAD, "还在吗")
    assert ok2 is False
    assert tc2._send_calls["send"] == 0                 # 闸生效：零 API 调用
    assert tc2._handle_send_exc.call_count == 0         # 拦下不喂风控


@pytest.mark.asyncio
async def test_guard_rereads_flag_after_hot_reload(tmp_path):
    """flag 从关→开（config 热重载）后守卫必须**立即**生效，不得缓存关闭态。

    2026-07-29 灰度实测事故：proactive 侧曾把 None 也惰性缓存 → 重启到配置热重载
    之间的窗口内被调用一次，就把「未启用」钉死，灰度开关看似生效实则永不生效。
    这里钉住 sender 侧的等价不变量（每次调用现读 config）。
    """
    reset_singleton()
    cfg = _cfg(tmp_path, enabled=False)
    tc = _mk_tc(config=cfg, send_side_effects=[])
    on, reg = tc._dead_peer_guard()
    assert on is False and reg is None

    cfg.config["ops"]["dead_peer_registry"]["enabled"] = True   # 模拟热重载开启
    on2, reg2 = tc._dead_peer_guard()
    assert on2 is True and reg2 is not None


@pytest.mark.asyncio
async def test_guard_passes_ttl_by_reason(tmp_path):
    """sender 守卫把 ttl_by_reason 透传给登记表（reason 级 TTL 在生产可配生效）。"""
    reset_singleton()
    tc = _mk_tc(config=_cfg(tmp_path, enabled=True,
                            ttl_by_reason={"blocked": 604800}),
                send_side_effects=[])
    on, reg = tc._dead_peer_guard()
    assert on is True and reg is not None
    assert reg.dump()["ttl_by_reason"].get("blocked") == 604800.0
    # deactivated 恒永久（即便同时配了它的 TTL，登记表内部也无视）
    assert reg._ttl_for_reason("deactivated") == 0.0
    assert reg._ttl_for_reason("blocked") == 604800.0


@pytest.mark.asyncio
async def test_flag_on_peer_invalid_not_blacklisted(tmp_path):
    """可自愈的 peer_invalid（本地缓存问题）**绝不进黑名单**——否则一次缓存 miss
    就把一个正常 peer 永久拉黑。私聊 peer_invalid 预热救不了（预热仅群），失败即返回，
    但关键不变量是：下次仍会真发（未被误拉黑）。群场景的自愈成功由
    test_telegram_peer_selfheal 覆盖。"""
    reset_singleton()
    tc = _mk_tc(config=_cfg(tmp_path, enabled=True),
                send_side_effects=[Exception("[400 PEER_ID_INVALID]")])
    ok, _ = await tc._send_text_guarded(DEAD, "hi")
    assert ok is False                                  # 私聊 peer_invalid 无法自愈
    # 未落黑名单：文件不含该 peer（record 对 peer_unresolved 忽略）
    import json
    if (tmp_path / "dead_peers.json").exists():
        data = json.loads((tmp_path / "dead_peers.json").read_text("utf-8"))
        assert f"telegram:{DEAD}" not in data
    # 下次仍真发（闸不拦）
    reset_singleton()
    tc2 = _mk_tc(config=_cfg(tmp_path, enabled=True),
                 send_side_effects=[SimpleNamespace(id=2)])
    ok2, _ = await tc2._send_text_guarded(DEAD, "hi again")
    assert ok2 is True and tc2._send_calls["send"] == 1
