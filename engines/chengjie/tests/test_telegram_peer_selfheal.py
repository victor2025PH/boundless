# -*- coding: utf-8 -*-
"""群聊 peer 冷启动自愈 + pyrogram 超 int32 群 id 兼容（2026-07-27 P3-5 事故回归网）。

事故机制（双号群演灰度首场即中）：
- Telegram 新建超级群 id 已越过 int32（实测 ``-1003142518418`` → internal
  ``3142518418 > 2**31-1``），冻结版 pyrogram 2.0.106 的
  ``utils.MIN_CHANNEL_ID`` 还是 int32 边界；
- **peers 缓存命中的老号**（演过 solo 的 skeptic）resolve 首查直接返回、毫发无损；
  **新拉进群的号**（advocate）缓存 miss → 落到 ``get_peer_type`` 边界校验 →
  ``ValueError("Peer id invalid")`` → 两拍连败 → 整场 send_failed 收场。
- 更险的一层：``PeerIdInvalid`` 在 ban_signal ``_PAUSE_NAMES`` 里——若异常形态是
  RPC 版（类名精确命中），一次缓存 miss 就把无辜号冻 60min。本次是 ``ValueError``
  类名侥幸躲过；这里把「peer-invalid 永不喂风控」钉成不变量。

三层防线：
1. ``pyrogram_compat.ensure_wide_channel_ids``：MIN_CHANNEL_ID 放宽到 tdlib 上限
   （治本，覆盖 send/get_chat/read_history 全部 resolve 路径）；
2. ``_send_text_guarded`` peer-invalid → ``get_dialogs`` 预热缓存 → 同拍重发一次
   （兜底，覆盖「真·刚加群缓存缺失」的 RPC 级 PEER_ID_INVALID）；
3. peer-invalid 类错误（原始失败/预热后仍失败）**零调用** ``_handle_send_exc``。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

GROUP = -1003142518418     # 真实事故群 id（internal 超 int32）
PEER_PRIVATE = 5433982810


# ── 最小 TelegramClient 装配（对齐 test_telegram_outgoing_guard 的构造法）────

def _mk_tc(*, send_side_effects, dialogs_chats=(), all_chats=None,
           resolve_effects=None):
    """``__new__`` 出壳、只接 ``_send_text_guarded`` 依赖的最小面。

    ``send_side_effects``：底层 ``client.send_message`` 的逐次行为
    （异常实例=抛出，其余=返回值）。``dialogs_chats``：``get_dialogs``
    产出的 chat id 序列（预热第①级能不能「看见」目标群由它决定）。
    ``all_chats``：raw ``GetAllChats`` 响应里的 chats（预热第②级；None=
    桩上无 ``invoke``，等价「raw 层不可用」）。``resolve_effects``：
    ``resolve_peer`` 逐次行为（``ensure_group_peer`` 体检用）。
    """
    from src.client.telegram_client import TelegramClient
    tc = TelegramClient.__new__(TelegramClient)
    tc._presend_blocked = MagicMock(return_value=False)
    tc._presend_pace = AsyncMock()
    tc._postsend_record_count = MagicMock()
    tc._handle_send_exc = MagicMock()

    calls = {"send": 0, "invoke": 0, "resolve": 0}

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

    kw = dict(send_message=_send_message, get_dialogs=_get_dialogs)

    if all_chats is not None:
        async def _invoke(_query):
            calls["invoke"] += 1
            return SimpleNamespace(chats=list(all_chats))
        kw["invoke"] = _invoke
        kw["fetch_peers"] = AsyncMock()

    if resolve_effects is not None:
        async def _resolve_peer(peer_id):
            idx = min(calls["resolve"], len(resolve_effects) - 1)
            calls["resolve"] += 1
            eff = resolve_effects[idx]
            if isinstance(eff, BaseException):
                raise eff
            return eff
        kw["resolve_peer"] = _resolve_peer

    tc.client = SimpleNamespace(**kw)
    tc._send_calls = calls
    return tc


def _mk_typed(tname, internal_id):
    """伪 raw chat 对象（类名前缀 Channel/Chat 是 ``_warm_via_all_chats`` 的判型依据）。"""
    obj = type(tname, (), {})()
    obj.id = internal_id
    return obj


def _sent_msg(mid=777):
    return SimpleNamespace(id=mid)


# ── 1. 自愈主路径：缓存 miss → 预热见群 → 重发成功，且绝不喂风控 ─────────────

@pytest.mark.asyncio
async def test_peer_invalid_is_healed_by_dialog_warmup_and_resend():
    """事故复刻：首发抛本地 ValueError 形态 → dialogs 预热见到群 → 重发成功。

    对上层（companion worker / group_show live）来说这只是一次普通成功投递：
    delivered=True、message.id 可取、fail_streak 不动。
    """
    tc = _mk_tc(
        send_side_effects=[ValueError(f"Peer id invalid: {GROUP}"), _sent_msg(88)],
        dialogs_chats=[111, GROUP, 222])

    ok, sent = await tc._send_text_guarded(GROUP, "附和这个痛点")

    assert ok is True and getattr(sent, "id", None) == 88
    assert tc._send_calls["send"] == 2                 # 原发一次 + 重试一次
    assert tc._handle_send_exc.call_count == 0         # 红线：不喂 ban_signal
    assert tc._postsend_record_count.call_count == 1   # 成功只记一次账


@pytest.mark.asyncio
async def test_rpc_flavored_peer_id_invalid_also_triggers_the_heal():
    """RPC 形态（类名 PeerIdInvalid / 文案 PEER_ID_INVALID）同样触发自愈。

    这形态若漏进 ``_handle_send_exc``，ban_signal 会按 _PAUSE_NAMES 冻号
    60min——「新号进群第一拍」是双号群演的高频动作，误冻=整场报废。
    """
    class PeerIdInvalid(Exception):
        pass

    tc = _mk_tc(
        send_side_effects=[PeerIdInvalid("Telegram says: [400 PEER_ID_INVALID]"),
                           _sent_msg(99)],
        dialogs_chats=[GROUP])

    ok, sent = await tc._send_text_guarded(GROUP, "顺着说自己的用法")

    assert ok is True and sent.id == 99
    assert tc._handle_send_exc.call_count == 0


@pytest.mark.asyncio
async def test_channel_invalid_from_zero_access_hash_probe_is_healed():
    """第二场灰度实录：id 边界补丁生效后，缓存 miss 的号走
    ``channels.GetChannels(access_hash=0)`` 被服务器拒 → ``CHANNEL_INVALID``。
    这仍是「本地没有有效 access_hash」的症状，dialogs 预热即愈——
    判别器漏掉它，自愈层就等于没接（第二场 send_failed 的直接原因）。
    """
    class ChannelInvalid(Exception):
        pass

    tc = _mk_tc(
        send_side_effects=[ChannelInvalid(
            'Telegram says: [400 CHANNEL_INVALID] - The channel parameter is '
            'invalid (caused by "channels.GetChannels")'), _sent_msg(101)],
        dialogs_chats=[GROUP])

    ok, sent = await tc._send_text_guarded(GROUP, "附和踩坑经历")

    assert ok is True and sent.id == 101
    assert tc._handle_send_exc.call_count == 0


# ── 2. 自愈失败面：找不到群 / 重试仍炸，都如实失败且不误报风控 ────────────────

@pytest.mark.asyncio
async def test_warmup_that_cannot_see_the_group_fails_without_ban_signal():
    """dialogs 里没有目标群（号真不在群里/被踢）→ 如实 False，不重发不喂风控。"""
    tc = _mk_tc(
        send_side_effects=[ValueError(f"Peer id invalid: {GROUP}")],
        dialogs_chats=[111, 222])

    ok, sent = await tc._send_text_guarded(GROUP, "你好")

    assert (ok, sent) == (False, None)
    assert tc._send_calls["send"] == 1                 # 预热失败就不该盲目重发
    assert tc._handle_send_exc.call_count == 0
    assert tc._postsend_record_count.call_count == 0


@pytest.mark.asyncio
async def test_retry_that_fails_with_peer_error_again_stays_out_of_ban_signal():
    """预热见到了群但重发仍 peer-invalid（缓存写入竞态等）→ False，仍不喂风控。"""
    tc = _mk_tc(
        send_side_effects=[ValueError(f"Peer id invalid: {GROUP}"),
                           ValueError(f"Peer id invalid: {GROUP}")],
        dialogs_chats=[GROUP])

    ok, _ = await tc._send_text_guarded(GROUP, "你好")

    assert ok is False
    assert tc._handle_send_exc.call_count == 0


@pytest.mark.asyncio
async def test_retry_that_hits_a_real_risk_error_still_feeds_g2():
    """重试抛出的**新**异常是真风控（FloodWait 类）→ 照常喂 G2 分级，不因自愈岔路漏报。"""
    class FloodWait(Exception):
        value = 30

    flood = FloodWait("Telegram says: [420 FLOOD_WAIT_X]")
    tc = _mk_tc(
        send_side_effects=[ValueError(f"Peer id invalid: {GROUP}"), flood],
        dialogs_chats=[GROUP])

    ok, _ = await tc._send_text_guarded(GROUP, "你好")

    assert ok is False
    assert tc._handle_send_exc.call_count == 1
    assert tc._handle_send_exc.call_args[0][0] is flood


@pytest.mark.asyncio
async def test_private_peer_invalid_does_not_warm_or_resend():
    """私聊（正 id）的 peer-invalid：dialogs 预热救不了（对方注销/从未对话），
    直接 False；调用方自理（proactive 有 _bad_peers 拉黑）。也不喂风控——
    「本地不认识对方」与封号风险无关，冻整号 60min 杀伤面错配。"""
    tc = _mk_tc(
        send_side_effects=[ValueError(f"Peer id invalid: {PEER_PRIVATE}")],
        dialogs_chats=[GROUP])

    ok, _ = await tc._send_text_guarded(PEER_PRIVATE, "在吗")

    assert ok is False
    assert tc._send_calls["send"] == 1
    assert tc._handle_send_exc.call_count == 0


# ── 2b. 预热第②级：GetAllChats 兜住「归档隐身」的群 ──────────────────────────

GROUP_INTERNAL = 3142518418     # GROUP 的 raw internal id（-100 前缀剥掉）


@pytest.mark.asyncio
async def test_archived_group_invisible_to_dialogs_is_healed_via_all_chats():
    """第三场灰度实锤盲区：号在群里但群被自动归档（陌生会话默认进 folder 1），
    ``get_dialogs`` 主列表看不见 → 第①级预热 miss；raw ``GetAllChats``
    无视文件夹返回全部所在群 → 命中、fetch_peers 写缓存、同拍重发成功。
    """
    pytest.importorskip("pyrogram")
    tc = _mk_tc(
        send_side_effects=[ValueError(f"Peer id invalid: {GROUP}"), _sent_msg(55)],
        dialogs_chats=[111, 222],                      # 主列表没有群（归档隐身）
        all_chats=[_mk_typed("Channel", GROUP_INTERNAL),
                   _mk_typed("Chat", 987654)])

    ok, sent = await tc._send_text_guarded(GROUP, "接上一拍")

    assert ok is True and sent.id == 55
    assert tc._send_calls["invoke"] == 1
    tc.client.fetch_peers.assert_awaited()             # 缓存真的写了
    assert tc._handle_send_exc.call_count == 0


@pytest.mark.asyncio
async def test_all_chats_that_lacks_the_group_is_the_ground_truth_not_in_group():
    """两级预热都不见群 ＝ 号确实不在群内：如实 False、不重发、不喂风控。"""
    pytest.importorskip("pyrogram")
    tc = _mk_tc(
        send_side_effects=[ValueError(f"Peer id invalid: {GROUP}")],
        dialogs_chats=[111],
        all_chats=[_mk_typed("Channel", 42), _mk_typed("Chat", 43)])

    ok, sent = await tc._send_text_guarded(GROUP, "你好")

    assert (ok, sent) == (False, None)
    assert tc._send_calls["send"] == 1
    assert tc._send_calls["invoke"] == 1
    assert tc._handle_send_exc.call_count == 0


# ── 2c. ensure_group_peer：开演前体检的三种答案 ───────────────────────────────

@pytest.mark.asyncio
async def test_ensure_group_peer_is_zero_rpc_when_cache_is_hot():
    """缓存热：resolve 首查即过 → True，零预热零发送。"""
    tc = _mk_tc(send_side_effects=[_sent_msg()],
                dialogs_chats=[], resolve_effects=[SimpleNamespace()])

    assert await tc.ensure_group_peer(GROUP) is True
    assert tc._send_calls["resolve"] == 1
    assert tc._send_calls["send"] == 0


@pytest.mark.asyncio
async def test_ensure_group_peer_warms_then_verifies_for_cold_cache():
    """缓存冷：resolve 首查炸 → 预热（dialogs 见群）→ resolve 复核过 → True。"""
    class ChannelInvalid(Exception):
        pass

    tc = _mk_tc(send_side_effects=[_sent_msg()],
                dialogs_chats=[GROUP],
                resolve_effects=[ChannelInvalid("[400 CHANNEL_INVALID]"),
                                 SimpleNamespace()])

    assert await tc.ensure_group_peer(GROUP) is True
    assert tc._send_calls["resolve"] == 2


@pytest.mark.asyncio
async def test_ensure_group_peer_says_no_when_the_account_is_not_in_group():
    """两级预热皆 miss → False——这就是「别让它上台」的确定信号。"""
    pytest.importorskip("pyrogram")

    class ChannelInvalid(Exception):
        pass

    tc = _mk_tc(send_side_effects=[_sent_msg()],
                dialogs_chats=[111],
                all_chats=[_mk_typed("Channel", 42)],
                resolve_effects=[ChannelInvalid("[400 CHANNEL_INVALID]")])

    assert await tc.ensure_group_peer(GROUP) is False
    assert tc._send_calls["send"] == 0                 # 体检全程零发言


@pytest.mark.asyncio
async def test_ensure_group_peer_passes_private_and_username_targets_through():
    """非负 id（私聊/username）不属群体检范围：恒 True 交发送路径。"""
    tc = _mk_tc(send_side_effects=[_sent_msg()], dialogs_chats=[])

    assert await tc.ensure_group_peer(PEER_PRIVATE) is True
    assert await tc.ensure_group_peer("some_username") is True


# ── 3. 非 peer 错误：原有 G2 语义一字不动 ────────────────────────────────────

@pytest.mark.asyncio
async def test_non_peer_errors_keep_the_original_g2_path():
    class ChatWriteForbidden(Exception):
        pass

    exc = ChatWriteForbidden("CHAT_WRITE_FORBIDDEN")
    tc = _mk_tc(send_side_effects=[exc], dialogs_chats=[GROUP])

    ok, _ = await tc._send_text_guarded(GROUP, "你好")

    assert ok is False
    assert tc._send_calls["send"] == 1                 # 不触发 peer 自愈重试
    assert tc._handle_send_exc.call_count == 1
    assert tc._handle_send_exc.call_args[0][0] is exc


# ── 4. 判别器纯函数语义 ─────────────────────────────────────────────────────

def test_peer_invalid_error_matcher_matrix():
    from src.client.telegram_client import TelegramClient

    class PeerIdInvalid(Exception):
        pass

    class ChannelInvalid(Exception):
        pass

    class ChannelPrivate(Exception):
        pass

    fn = TelegramClient._is_peer_invalid_error
    assert fn(ValueError("Peer id invalid: -1003142518418")) is True
    assert fn(PeerIdInvalid("[400 PEER_ID_INVALID]")) is True
    assert fn(ChannelInvalid("[400 CHANNEL_INVALID]")) is True
    assert fn(RuntimeError("peer id invalid")) is True         # 大小写不敏感
    # CHANNEL_PRIVATE=被踢/无权限：预热救不了，不进自愈族（走原 G2 路径）
    assert fn(ChannelPrivate("[400 CHANNEL_PRIVATE]")) is False
    assert fn(RuntimeError("CHAT_WRITE_FORBIDDEN")) is False
    assert fn(ValueError("something else")) is False
    assert fn(None) is False


# ── 5. pyrogram 兼容补丁（无 pyrogram 环境自动跳过） ──────────────────────────

def test_wide_channel_id_patch_unlocks_post_int32_groups():
    pytest.importorskip("pyrogram")
    from pyrogram import utils
    from src.client.pyrogram_compat import (
        WIDE_MIN_CHANNEL_ID, ensure_wide_channel_ids,
    )

    assert ensure_wide_channel_ids() is True
    assert ensure_wide_channel_ids() is True     # 幂等：二次调用无害
    assert int(utils.MIN_CHANNEL_ID) <= WIDE_MIN_CHANNEL_ID
    # 事故群 id 现在能被正确识别为 channel，而不是 ValueError
    assert utils.get_peer_type(GROUP) == "channel"
    # 老边界内的 id 语义不变
    assert utils.get_peer_type(-1001234567890) == "channel"
    assert utils.get_peer_type(5433982810) == "user"


def test_patch_is_applied_by_importing_telegram_client():
    """A 线接线点：telegram_client 模块加载即打补丁（生产真实生效路径）。

    刻意不 reload（巨型模块 reload 会造双份类对象污染同进程其他用例）——
    无论本用例先后于其他 import 跑，断言的都是「import 过即已放宽」这个事实。
    """
    pytest.importorskip("pyrogram")
    import src.client.telegram_client  # noqa: F401 —— import 副作用即打补丁

    from pyrogram import utils
    from src.client.pyrogram_compat import WIDE_MIN_CHANNEL_ID
    assert int(utils.MIN_CHANNEL_ID) <= WIDE_MIN_CHANNEL_ID
