# -*- coding: utf-8 -*-
"""死 peer 共享表 → B 线 autosend 接线门禁（2026-07-29，三链收敛最后一环）。

收敛前：A 线 sender / proactive / autosend **各持一份**永久错误词表与黑名单，
一条链确证的死号另两条还在打（实录：已注销号 2h 内被重试 17 次）。本文件钉住：
- 词表并集：autosend 原独有的四项并入共享 classify 后覆盖面只增不减；
- 判据语义：autosend 用「命中任意 reason」（peer 失效也该进带 TTL 的会话冷却），
  而写永久黑名单只认 permanent；
- 读侧：共享表命中 → 即便本地冷却关闭（cooldown=0）也拦；
- 写侧：永久错误登记进共享表；可自愈类不登记；
- 只 peek 不创建：单例未建立时不得自建无路径实例（否则共享失效）。
"""
from __future__ import annotations

import time

import pytest

from src.inbox.autosend_worker import AutosendWorker, _is_permanent_send_error
from src.ops.dead_peer_registry import (
    DeadPeerRegistry,
    classify_send_error,
    get_dead_peer_registry,
    peek_dead_peer_registry,
    reset_singleton,
)

# autosend 收敛前独有的四项——并入共享词表后必须仍被判为「短期内发不出」
_AUTOSEND_LEGACY_MARKERS = (
    "CHAT_WRITE_FORBIDDEN", "USER_IS_BLOCKED", "INPUT_USER_DEACTIVATED",
    "USER_DEACTIVATED", "PEER_ID_INVALID", "CHAT_ADMIN_REQUIRED",
    "CHANNEL_PRIVATE", "USER_BANNED_IN_CHANNEL",
    "You don't have rights to send messages in this chat",
)


def _worker(cfg=None):
    """最小 worker。注意：构造入参是 **inbox.l2_autosend 子段**（不是全局 config）。"""
    return AutosendWorker(draft_service=object(), config=cfg or {})


# ── 词表并集：覆盖面只增不减 ────────────────────────────────────

@pytest.mark.parametrize("marker", _AUTOSEND_LEGACY_MARKERS)
def test_legacy_markers_still_permanent(marker):
    assert _is_permanent_send_error(f"Telegram says: [400 {marker}]") is True


def test_shared_extras_now_covered_too():
    """收敛顺带获得共享词表原有项（收敛前 autosend 认不出这些）。"""
    for extra in ("YOU_BLOCKED_USER", "CHAT_SEND_PLAIN_FORBIDDEN", "CHANNEL_INVALID"):
        assert _is_permanent_send_error(f"[400 {extra}]") is True


def test_transient_errors_still_not_permanent():
    for ok_err in ("Request timed out", "FLOOD_WAIT_42", "Connection reset", ""):
        assert _is_permanent_send_error(ok_err) is False


def test_permanent_vs_registry_reason_semantics():
    """peer 失效：autosend 判「该进冷却」为真，但它不是永久黑名单对象。"""
    from src.ops.dead_peer_registry import is_permanent_reason
    err = "[400 PEER_ID_INVALID]"
    assert _is_permanent_send_error(err) is True              # 进会话冷却
    assert is_permanent_reason(classify_send_error(err)) is False  # 不进永久黑名单


# ── 平台解析 ────────────────────────────────────────────────

def test_platform_of_conv():
    w = _worker()
    assert w._platform_of_conv("inbox:whatsapp:639270135480:639055224566") == "whatsapp"
    assert w._platform_of_conv("inbox:telegram:acc:123") == "telegram"
    assert w._platform_of_conv("junk") == "telegram"          # 兜底与 A 线口径一致
    assert w._platform_of_conv("") == "telegram"


# ── 只 peek 不创建 ──────────────────────────────────────────

def test_peek_does_not_create_singleton():
    """单例未建立（＝flag 未开，A 线/proactive 没建表）→ 放行，且**不得自建**。

    自建会得到无路径纯内存实例，看起来"能用"实则与其他链不共享（静默失效）。
    """
    reset_singleton()
    assert peek_dead_peer_registry() is None
    w = _worker()
    assert w._dead_peer_blocked("inbox:telegram:acc:1") is False
    assert peek_dead_peer_registry() is None                        # 关键：未自建
    reset_singleton()


def test_singleton_presence_encodes_flag(tmp_path):
    """「单例存在」即「flag 已开」——B 线据此接入，无需自己读全局 config
    （它只拿到 inbox.l2_autosend 子段，读不到 ops.dead_peer_registry）。"""
    reset_singleton()
    w = _worker()
    assert w._dead_peer_shared() is None                # 未建 → 不查
    reg = get_dead_peer_registry(path=str(tmp_path / "dp.json"))
    reg.record("telegram", 777, "deactivated")
    assert w._dead_peer_shared() is reg                 # 已建 → 同一张表
    assert w._dead_peer_blocked("inbox:telegram:acc:777") is True
    reset_singleton()


# ── 读侧：共享表命中即拦（独立于本地冷却配置）────────────────

def test_shared_block_applies_even_with_cooldown_disabled(tmp_path):
    reset_singleton()
    reg = get_dead_peer_registry(path=str(tmp_path / "dp.json"))
    reg.record("whatsapp", "inbox:whatsapp:acc:555", "deactivated")
    w = _worker({"send_block_cooldown_sec": 0})                 # 子段口径
    assert w._send_block_cooldown_sec == 0                     # 本地冷却关
    assert w._conv_send_blocked("inbox:whatsapp:acc:555") is True   # 共享表仍拦
    assert w._conv_send_blocked("inbox:whatsapp:acc:999") is False  # 无关会话放行
    reset_singleton()


def test_a_line_blacklist_blocks_b_line(tmp_path):
    """A 线用裸 chat_id 拉黑 → B 线用 conversation_id 查询必须命中（key 归一化）。"""
    reset_singleton()
    reg = get_dead_peer_registry(path=str(tmp_path / "dp.json"))
    reg.record("telegram", 5433982810, "blocked")              # A 线口径
    w = _worker()
    assert w._dead_peer_blocked("inbox:telegram:acc:5433982810") is True
    reset_singleton()


# ── 写侧：永久登记、可自愈不登记 ────────────────────────────

def test_record_permanent_into_shared_table(tmp_path):
    reset_singleton()
    reg = get_dead_peer_registry(path=str(tmp_path / "dp.json"))
    w = _worker()
    w._dead_peer_record("inbox:whatsapp:acc:888",
                        "Telegram says: [400 INPUT_USER_DEACTIVATED]")
    assert reg.is_blocked("whatsapp", 888) is True
    assert reg.reason_of("whatsapp", 888) == "deactivated"
    reset_singleton()


def test_record_skips_self_healable(tmp_path):
    reset_singleton()
    reg = get_dead_peer_registry(path=str(tmp_path / "dp.json"))
    w = _worker()
    w._dead_peer_record("inbox:telegram:acc:111", "[400 PEER_ID_INVALID]")
    assert reg.is_blocked("telegram", 111) is False       # 可自愈类不进永久黑名单
    reset_singleton()


def test_record_noop_on_unknown_conv(tmp_path):
    reset_singleton()
    reg = get_dead_peer_registry(path=str(tmp_path / "dp.json"))
    w = _worker()
    w._dead_peer_record("?", "[400 INPUT_USER_DEACTIVATED]")
    w._dead_peer_record("", "[400 INPUT_USER_DEACTIVATED]")
    assert reg.dump()["total"] == 0
    reset_singleton()


# ── 本地会话冷却机制未被削弱 ────────────────────────────────

def test_local_cooldown_still_works_without_shared_table():
    reset_singleton()
    w = _worker({"send_block_cooldown_sec": 60})                # 子段口径
    conv = "inbox:line:acc:1"
    assert w._conv_send_blocked(conv) is False
    w._blocked_conv_until[conv] = time.time() + 60
    assert w._conv_send_blocked(conv) is True
    w._blocked_conv_until[conv] = time.time() - 1          # 过期 → 自动清理
    assert w._conv_send_blocked(conv) is False
    assert conv not in w._blocked_conv_until
