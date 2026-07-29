# -*- coding: utf-8 -*-
"""死 peer 统一登记表门禁（src/ops/dead_peer_registry.py）。

防回归点＝2026-07-27 事故：已注销用户被主动/自动发送链每 15min 重发、2h ×17 次
（INPUT_USER_DEACTIVATED）累积风控。关键不变量：
- 永久错误（注销/拉黑/写禁止）落黑名单；可自愈错误（peer_invalid/channel_invalid）不落；
- 平台命名空间隔离；落盘/重载往返；TTL 过期惰性放行；容量上限淘汰；
- feature flag 默认关。
"""
from __future__ import annotations

import time

import pytest

from src.ops.dead_peer_registry import (
    DeadPeerRegistry,
    classify_send_error,
    dead_peer_enabled,
    is_permanent_reason,
)


class _FakeExc(Exception):
    pass


class PeerIdInvalid(Exception):
    """模拟 pyrogram 异常类名形态（无下划线）。"""


# ── 纯函数分类 ────────────────────────────────────────────────

@pytest.mark.parametrize("text,expect", [
    ("Telegram says: [400 INPUT_USER_DEACTIVATED] - ...", "deactivated"),
    ("USER_DEACTIVATED", "deactivated"),
    ("[400 USER_IS_BLOCKED]", "blocked"),
    ("YOU_BLOCKED_USER", "blocked"),
    ("[403 CHAT_WRITE_FORBIDDEN]", "write_forbidden"),
    ("Peer id invalid: -100123", "peer_unresolved"),
    ("[400 PEER_ID_INVALID]", "peer_unresolved"),
    ("[400 CHANNEL_INVALID] - ...", "peer_unresolved"),
    ("Request timed out", None),
    ("FLOOD_WAIT_420", None),
])
def test_classify_send_error(text, expect):
    assert classify_send_error(_FakeExc(text)) == expect


def test_classify_pyrogram_class_name():
    assert classify_send_error(PeerIdInvalid("x")) == "peer_unresolved"


def test_permanent_vs_transient():
    assert is_permanent_reason("deactivated") is True
    assert is_permanent_reason("blocked") is True
    assert is_permanent_reason("write_forbidden") is True
    assert is_permanent_reason("peer_unresolved") is False   # 可自愈，不拉黑
    assert is_permanent_reason(None) is False


# ── 登记表：只落永久、平台隔离、往返 ───────────────────────────

def test_record_only_permanent(tmp_path):
    r = DeadPeerRegistry(path=str(tmp_path / "dp.json"))
    assert r.record("telegram", 111, "deactivated") is True
    assert r.record("telegram", 222, "peer_unresolved") is False   # 可自愈忽略
    assert r.is_blocked("telegram", 111) is True
    assert r.is_blocked("telegram", 222) is False


def test_cross_chain_key_unification(tmp_path):
    """跨链 key 归一化（2026-07-29 读真实 legacy 数据发现的实质缺陷）：
    A 线 sender 手里是裸 chat_id、proactive 手里是 conversation_id
    （platform:account:peer）——两者必须落**同一** key，否则一条链拉黑另一条链查不到，
    「共享黑名单」名存实亡。"""
    from src.ops.dead_peer_registry import peer_of

    assert peer_of("telegram:8244899900:8296311798") == "8296311798"
    assert peer_of("8296311798") == "8296311798"          # 裸 id 原样
    assert peer_of(8296311798) == "8296311798"            # 数字型
    assert peer_of("") == ""

    r = DeadPeerRegistry(path=str(tmp_path / "dp.json"))
    # proactive 口径写入（conversation_id）
    r.record("telegram", "telegram:8244899900:8296311798", "deactivated")
    # A 线 sender 口径读取（裸 chat_id）→ 必须命中
    assert r.is_blocked("telegram", 8296311798) is True
    # 反向：sender 写、proactive 读
    r.record("telegram", 5433982810, "blocked")
    assert r.is_blocked("telegram", "telegram:8244899900:5433982810") is True


def test_legacy_conversation_id_form_normalized(tmp_path):
    """proactive 历史 legacy（conversation_id 列表）迁入后，sender 口径能查到。"""
    legacy = tmp_path / "companion_bad_peers.json"
    legacy.write_text(
        '["telegram:8244899900:1745338727", "telegram:8244899900:8296311798"]',
        encoding="utf-8")
    r = DeadPeerRegistry(path=str(tmp_path / "dead_peers.json"),
                         legacy_paths=[str(legacy)])
    assert r.is_blocked("telegram", 1745338727) is True    # 裸 id 命中迁入条目
    assert r.is_blocked("telegram", "telegram:8244899900:8296311798") is True


def test_platform_namespace_isolation(tmp_path):
    r = DeadPeerRegistry(path=str(tmp_path / "dp.json"))
    r.record("telegram", 42, "deactivated")
    assert r.is_blocked("telegram", 42) is True
    assert r.is_blocked("whatsapp", 42) is False    # 同 id 不同平台不串味


def test_persist_reload_roundtrip(tmp_path):
    p = str(tmp_path / "dp.json")
    r1 = DeadPeerRegistry(path=p)
    r1.record("telegram", 999, "blocked")
    r2 = DeadPeerRegistry(path=p)                    # 新实例从盘重载
    assert r2.is_blocked("telegram", 999) is True
    assert r2.reason_of("telegram", 999) == "blocked"


def test_legacy_list_format_compat(tmp_path):
    """兼容 proactive 旧格式（裸 peer 列表）。"""
    p = tmp_path / "dp.json"
    p.write_text('["555", "666"]', encoding="utf-8")
    r = DeadPeerRegistry(path=str(p))
    assert r.is_blocked("telegram", 555) is True
    assert r.is_blocked("telegram", 666) is True


# ── legacy 迁移：各链独立黑名单收敛进共享表不丢历史 ─────────────

def test_legacy_migration_imports_and_persists(tmp_path):
    """主表为空 → 从旧格式（proactive 的裸 peer 列表）一次性迁入并落新主文件。"""
    legacy = tmp_path / "companion_bad_peers.json"
    legacy.write_text('["111", "222"]', encoding="utf-8")
    main = tmp_path / "dead_peers.json"

    r = DeadPeerRegistry(path=str(main), legacy_paths=[str(legacy)])
    assert r.is_blocked("telegram", 111) is True
    assert r.is_blocked("telegram", 222) is True
    # 迁移结果已落新主文件（下次启动不再依赖 legacy）
    assert main.is_file()
    import json
    data = json.loads(main.read_text("utf-8"))
    assert data["telegram:111"]["reason"] == "blocked"   # 保守记可解除类
    # legacy 文件保持原样（可回退，不删运营的历史文件）
    assert legacy.read_text(encoding="utf-8") == '["111", "222"]'


def test_legacy_not_imported_when_main_has_data(tmp_path):
    """主表已有数据 → 不再回头读 legacy（避免已解除的旧条目被反复复活）。"""
    legacy = tmp_path / "companion_bad_peers.json"
    legacy.write_text('["999"]', encoding="utf-8")
    main = tmp_path / "dead_peers.json"
    main.write_text('{"telegram:1": {"reason": "deactivated", "ts": 1, "count": 1}}',
                    encoding="utf-8")

    r = DeadPeerRegistry(path=str(main), legacy_paths=[str(legacy)])
    assert r.is_blocked("telegram", 1) is True
    assert r.is_blocked("telegram", 999) is False        # legacy 未被导入


def test_legacy_absent_is_noop(tmp_path):
    r = DeadPeerRegistry(path=str(tmp_path / "dead_peers.json"),
                         legacy_paths=[str(tmp_path / "nope.json")])
    assert r.dump()["total"] == 0


def test_singleton_late_legacy_ingest(tmp_path):
    """时序缺陷防回归：单例被**先到的调用方**（不带 legacy）建好后，后到者带来的
    历史黑名单仍须能迁入——否则谁先发消息决定历史丢不丢。"""
    from src.ops.dead_peer_registry import get_dead_peer_registry, reset_singleton

    reset_singleton()
    legacy = tmp_path / "companion_bad_peers.json"
    legacy.write_text('["777"]', encoding="utf-8")
    main = str(tmp_path / "dead_peers.json")

    # ① 先到者（如 A 线 sender）建单例，不知道 legacy 文件
    first = get_dead_peer_registry(path=main)
    assert first.is_blocked("telegram", 777) is False

    # ② 后到者（如 proactive）带 legacy → 补迁移到同一张共享表
    second = get_dead_peer_registry(path=main, legacy_paths=[str(legacy)])
    assert second is first                              # 仍是同一单例
    assert second.is_blocked("telegram", 777) is True    # 历史已迁入
    reset_singleton()


def test_late_legacy_ingest_skipped_when_table_nonempty(tmp_path):
    """补迁移仍受「表非空不导入」保护（不复活运营已清理的旧条目）。"""
    from src.ops.dead_peer_registry import get_dead_peer_registry, reset_singleton

    reset_singleton()
    legacy = tmp_path / "companion_bad_peers.json"
    legacy.write_text('["888"]', encoding="utf-8")
    reg = get_dead_peer_registry(path=str(tmp_path / "dead_peers.json"))
    reg.record("telegram", 1, "deactivated")            # 表已非空
    get_dead_peer_registry(path=str(tmp_path / "dead_peers.json"),
                           legacy_paths=[str(legacy)])
    assert reg.is_blocked("telegram", 888) is False      # 未被复活
    reset_singleton()


def test_ttl_expiry_lazy_release(tmp_path):
    r = DeadPeerRegistry(path=str(tmp_path / "dp.json"), ttl_sec=0.3)
    r.record("telegram", 1, "write_forbidden")
    assert r.is_blocked("telegram", 1) is True
    time.sleep(0.35)
    assert r.is_blocked("telegram", 1) is False      # TTL 过期惰性放行


# ── reason 级 TTL：注销恒永久、可解除类到期释放 ─────────────────

def test_deactivated_never_expires_even_with_ttl(tmp_path):
    """账号注销不可逆：即便配了很短的 reason TTL 也恒永久（安全第一）。"""
    r = DeadPeerRegistry(path=str(tmp_path / "dp.json"),
                         ttl_by_reason={"deactivated": 0.2})
    r.record("telegram", 1, "deactivated")
    time.sleep(0.3)
    assert r.is_blocked("telegram", 1) is True        # 无视 TTL，恒拉黑


def test_blocked_reason_ttl_releases(tmp_path):
    """被拉黑可能解除：配 reason TTL 到期后释放，允许探路重试。"""
    r = DeadPeerRegistry(path=str(tmp_path / "dp.json"),
                         ttl_by_reason={"blocked": 0.2, "write_forbidden": 0.2})
    r.record("telegram", 1, "blocked")
    r.record("telegram", 2, "write_forbidden")
    assert r.is_blocked("telegram", 1) is True
    assert r.is_blocked("telegram", 2) is True
    time.sleep(0.3)
    assert r.is_blocked("telegram", 1) is False       # 到期释放
    assert r.is_blocked("telegram", 2) is False


def test_reason_ttl_falls_back_to_global(tmp_path):
    """未配 reason TTL → 回落全局 ttl_sec（向后兼容）。"""
    r = DeadPeerRegistry(path=str(tmp_path / "dp.json"), ttl_sec=0.2,
                         ttl_by_reason={"blocked": 0})   # blocked 显式永久
    r.record("telegram", 1, "blocked")           # reason 级=0 永久
    r.record("telegram", 2, "write_forbidden")   # 未配 → 回落全局 0.2
    time.sleep(0.3)
    assert r.is_blocked("telegram", 1) is True    # blocked 永久（reason 级 0）
    assert r.is_blocked("telegram", 2) is False   # write_forbidden 回落全局到期


def test_no_ttl_config_all_permanent(tmp_path):
    """无任何 TTL 配置 = 全永久（旧行为，向后兼容）。"""
    r = DeadPeerRegistry(path=str(tmp_path / "dp.json"))
    for reason in ("deactivated", "blocked", "write_forbidden"):
        r.record("telegram", reason, reason)
    time.sleep(0.05)
    for reason in ("deactivated", "blocked", "write_forbidden"):
        assert r.is_blocked("telegram", reason) is True
    assert r.dump()["ttl_by_reason"] == {}


def test_unblock(tmp_path):
    r = DeadPeerRegistry(path=str(tmp_path / "dp.json"))
    r.record("telegram", 7, "deactivated")
    assert r.unblock("telegram", 7) is True
    assert r.is_blocked("telegram", 7) is False
    assert r.unblock("telegram", 7) is False         # 已无 → False


def test_capacity_eviction(tmp_path):
    r = DeadPeerRegistry(path=str(tmp_path / "dp.json"), max_entries=100)
    for i in range(120):
        r.record("telegram", i, "deactivated")
    d = r.dump()
    assert d["total"] <= 100                          # 超限淘汰最旧
    assert r.is_blocked("telegram", 119) is True      # 最新的保留


def test_dump_shape(tmp_path):
    r = DeadPeerRegistry(path=str(tmp_path / "dp.json"))
    r.record("telegram", 1, "deactivated")
    r.record("telegram", 2, "blocked")
    r.record("whatsapp", 3, "blocked")
    d = r.dump()
    assert d["total"] == 3
    assert d["by_reason"]["deactivated"] == 1
    assert d["by_reason"]["blocked"] == 2
    assert d["by_platform"]["telegram"] == 2
    assert d["by_platform"]["whatsapp"] == 1


def test_record_count_increments(tmp_path):
    r = DeadPeerRegistry(path=str(tmp_path / "dp.json"))
    r.record("telegram", 1, "deactivated")
    r.record("telegram", 1, "deactivated")
    # 重复登记累加 count（观测「同一死号被撞了几次」）
    import json
    data = json.loads((tmp_path / "dp.json").read_text("utf-8"))
    assert data["telegram:1"]["count"] == 2


# ── feature flag ─────────────────────────────────────────────

def test_flag_default_off():
    assert dead_peer_enabled({}) is False
    assert dead_peer_enabled({"ops": {}}) is False
    assert dead_peer_enabled({"ops": {"dead_peer_registry": {}}}) is False


def test_flag_on():
    assert dead_peer_enabled(
        {"ops": {"dead_peer_registry": {"enabled": True}}}) is True


def test_flag_configmanager_like():
    class _CM:
        config = {"ops": {"dead_peer_registry": {"enabled": True}}}
    assert dead_peer_enabled(_CM()) is True
