# -*- coding: utf-8 -*-
"""pyrogram 2.0.106 冻结版的运行时兼容补丁（单一出口，幂等）。

为什么存在
----------
Telegram 2024 起新建超级群的内部 id 已越过 int32（本机测试群
``-1003142518418`` → internal ``3142518418 > 2**31-1``），而本仓冻结的
pyrogram 2.0.106 里 ``utils.MIN_CHANNEL_ID = -1002147483647`` 还是 int32
时代的边界。后果：**本地 peers 缓存 miss 的号**对这类群做任何
``resolve_peer``（send / get_chat / read_chat_history …）都在客户端就被
``ValueError("Peer id invalid: …")`` 拦死，根本不给服务器机会——
2026-07-27 凌晨双号群演灰度（P3-5）的 send_failed 收场即此：缓存命中的
老号毫发无损、新拉进群的号第一拍就炸，症状极具迷惑性。

为什么 monkeypatch 而不升级 pyrogram：升级动的是生产 A 线地基（全账号
session 兼容、raw layer 变更），不是灰度阶段该背的回归面；上游活跃分支的
修法本质也是把边界改宽。此处直接对齐 tdlib 的 supergroup 语义上限
（internal ≤ 997852516352 → bot-api 形态最小 -1997852516352），一步放到
官方极限，后续 id 增长无需再跟。

放宽**纯本地**校验零风险：id 真非法时服务器会以 PEER_ID_INVALID 拒绝，
真权威从来在服务端。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: tdlib 语义的 supergroup/channel（bot-api 形态）id 下限：
#: ``-10**12 - 997852516352``。
WIDE_MIN_CHANNEL_ID = -1997852516352


def ensure_wide_channel_ids() -> bool:
    """把 pyrogram 的 channel id 下边界放宽到 tdlib 上限。幂等，可重复调用。

    返回是否处于「已放宽」状态（无 pyrogram 的环境返回 False，调用方无需关心）。
    ``utils.get_peer_type`` 读的是模块级全局 → 改属性即全路径生效。
    """
    try:
        from pyrogram import utils
    except Exception:  # noqa: BLE001 —— 纯单测等无 pyrogram 环境，静默跳过
        return False
    try:
        if int(getattr(utils, "MIN_CHANNEL_ID", 0)) > WIDE_MIN_CHANNEL_ID:
            utils.MIN_CHANNEL_ID = WIDE_MIN_CHANNEL_ID
            logger.info(
                "[pyrogram-compat] MIN_CHANNEL_ID 放宽至 %d（超 int32 群 id 兼容）",
                WIDE_MIN_CHANNEL_ID)
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[pyrogram-compat] 放宽 MIN_CHANNEL_ID 失败", exc_info=True)
        return False


__all__ = ["ensure_wide_channel_ids", "WIDE_MIN_CHANNEL_ID"]
