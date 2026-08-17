"""语音补欠账 voice IOU 门禁（``src/client/voice_iou.py``，2026-08-02）。

产品语义：诚实回落说了「回头补给你」→ 该会话下一回合语音链恢复时强制走语音
（「下一回合语音升级」，刻意不做后台主动补发）。覆盖：记账/覆盖/TTL 过期/
清除/坏文件容错/开关语义/快照/落盘重载/键归一化 + 接线静态钉
（telegram_client 记账、sender 记账+兑现+清账）。

测试铁律：JSON 落盘全部经 ``store_path`` 参数指向 tmp_path，绝不写仓库 config/。
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.client import voice_iou as vi  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_registry():
    """每例前后清空进程注册表，防跨例/跨路径串味。"""
    vi._reset_state_for_tests()
    yield
    vi._reset_state_for_tests()


@pytest.fixture()
def store(tmp_path):
    return tmp_path / "voice_iou.json"


# ── 配置解析 ────────────────────────────────────────────────────────


def test_parse_cfg_defaults_disabled():
    """新子系统铁律：默认 enabled=False、ttl_hours=24。"""
    for cfg in (None, {}, {"telegram": {}}, {"telegram": {"voice_reply": {}}}):
        out = vi.parse_iou_cfg(cfg)
        assert out == {"enabled": False, "ttl_hours": 24.0}


def test_parse_cfg_override_and_bad_shapes():
    cfg = {"telegram": {"voice_reply": {"voice_iou": {
        "enabled": True, "ttl_hours": 6}}}}
    out = vi.parse_iou_cfg(cfg)
    assert out["enabled"] is True and out["ttl_hours"] == 6.0
    # 坏形：块不是 dict / ttl 不是数字 → 回默认，永不抛
    bad = {"telegram": {"voice_reply": {"voice_iou": "yes"}}}
    assert vi.parse_iou_cfg(bad) == {"enabled": False, "ttl_hours": 24.0}
    bad2 = {"telegram": {"voice_reply": {"voice_iou": {
        "enabled": True, "ttl_hours": "abc"}}}}
    out2 = vi.parse_iou_cfg(bad2)
    assert out2["enabled"] is True and out2["ttl_hours"] == 24.0


# ── 记账 / 兑现 / 过期 ──────────────────────────────────────────────


def test_record_and_pending(store):
    assert vi.pending_iou("777", now=1000.0, store_path=store) is False
    vi.record_iou("777", "lin", now=1000.0, store_path=store)
    assert vi.pending_iou("777", now=1000.0 + 3600, store_path=store) is True
    # 其它会话不受影响
    assert vi.pending_iou("888", now=1000.0, store_path=store) is False


def test_record_overwrites_latest(store):
    """每 chat 只保留最新一条：旧账时间戳被覆盖 → TTL 从新账起算。"""
    vi.record_iou("c1", now=1000.0, store_path=store)
    vi.record_iou("c1", now=90000.0, store_path=store)
    snap = vi.iou_snapshot(now=90000.0, store_path=store)
    assert snap["pending"] == 1
    # 按旧账早已超 24h，但新账未超 → 仍 pending
    assert vi.pending_iou(
        "c1", now=90000.0 + 3600, store_path=store) is True


def test_ttl_expiry_cleans_entry(store):
    vi.record_iou("c2", now=1000.0, store_path=store)
    assert vi.pending_iou(
        "c2", ttl_hours=24.0, now=1000.0 + 24 * 3600 + 1,
        store_path=store) is False
    # 过期顺手清理：快照归零 + 落盘文件也不再含该键
    assert vi.iou_snapshot(store_path=store)["pending"] == 0
    on_disk = json.loads(store.read_text(encoding="utf-8"))
    assert "c2" not in on_disk


def test_custom_ttl_respected(store):
    vi.record_iou("c3", now=0.0, store_path=store)
    assert vi.pending_iou(
        "c3", ttl_hours=1.0, now=1800.0, store_path=store) is True
    assert vi.pending_iou(
        "c3", ttl_hours=1.0, now=3700.0, store_path=store) is False


def test_clear_iou(store):
    vi.record_iou("c4", now=100.0, store_path=store)
    vi.clear_iou("c4", store_path=store)
    assert vi.pending_iou("c4", now=101.0, store_path=store) is False
    on_disk = json.loads(store.read_text(encoding="utf-8"))
    assert on_disk == {}
    # 清不存在的键不抛
    vi.clear_iou("ghost", store_path=store)


def test_key_normalization_int_vs_str(store):
    """接线两端分别用 str(chat.id) 与 int——归一化后必须指向同一条账。"""
    vi.record_iou(12345, now=50.0, store_path=store)  # type: ignore[arg-type]
    assert vi.pending_iou("12345", now=60.0, store_path=store) is True
    vi.clear_iou(12345, store_path=store)  # type: ignore[arg-type]
    assert vi.pending_iou("12345", now=60.0, store_path=store) is False


# ── 防御式：坏文件 / 空键 / 永不抛 ─────────────────────────────────


def test_bad_file_tolerated_as_empty(store):
    store.write_text("{not-json!!!", encoding="utf-8")
    assert vi.pending_iou("x", store_path=store) is False
    # 坏文件不阻塞新记账，且记账后文件恢复为合法 JSON
    vi.record_iou("x", now=10.0, store_path=store)
    assert vi.pending_iou("x", now=11.0, store_path=store) is True
    assert json.loads(store.read_text(encoding="utf-8"))["x"]["ts"] == 10.0


def test_missing_file_is_empty_table(store):
    assert vi.pending_iou("nobody", store_path=store) is False
    assert vi.iou_snapshot(store_path=store) == {
        "pending": 0, "oldest_age_sec": 0.0}
    assert not store.exists()   # 只读操作不落文件


def test_empty_or_none_key_never_throws(store):
    vi.record_iou("", store_path=store)
    vi.record_iou(None, store_path=store)  # type: ignore[arg-type]
    assert vi.pending_iou("", store_path=store) is False
    assert vi.pending_iou(None, store_path=store) is False  # type: ignore
    vi.clear_iou("", store_path=store)
    assert vi.iou_snapshot(store_path=store)["pending"] == 0


def test_persistence_across_process_reload(store):
    """落盘的意义：进程重启（模拟＝清内存注册表）后欠账仍在。"""
    vi.record_iou("c9", "persona_a", now=500.0, store_path=store)
    vi._reset_state_for_tests()
    assert vi.pending_iou("c9", now=600.0, store_path=store) is True
    on_disk = json.loads(store.read_text(encoding="utf-8"))
    assert on_disk["c9"]["persona_id"] == "persona_a"


def test_legacy_flat_file_shape_tolerated(store):
    """容忍扁平 {chat: ts} 旧形（防手工编辑/降级写入把表读崩）。"""
    store.write_text(json.dumps({"c10": 700.0}), encoding="utf-8")
    assert vi.pending_iou("c10", now=800.0, store_path=store) is True


def test_snapshot_pending_and_oldest(store):
    vi.record_iou("a", now=1000.0, store_path=store)
    vi.record_iou("b", now=2000.0, store_path=store)
    snap = vi.iou_snapshot(now=2600.0, store_path=store)
    assert snap["pending"] == 2
    assert snap["oldest_age_sec"] == pytest.approx(1600.0, abs=0.2)


def test_thread_safety_smoke(store):
    """并发记账/查询不抛不崩（模块级锁）。"""
    errs = []

    def _work(i: int) -> None:
        try:
            for j in range(20):
                vi.record_iou(f"t{i}", now=float(j), store_path=store)
                vi.pending_iou(f"t{i}", now=float(j), store_path=store)
        except Exception as e:  # pragma: no cover
            errs.append(e)

    threads = [threading.Thread(target=_work, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errs
    assert vi.iou_snapshot(store_path=store)["pending"] == 4


# ── 接线静态钉（源码级：记账/兑现/清账三链齐备 + 开关门控）──────────


def _src(name: str) -> str:
    root = Path(__file__).parent.parent / "src" / "client"
    return (root / name).read_text(encoding="utf-8")


def test_wiring_iou_writers_stay_removed():
    """无兜底纪律钉（2026-08-17）：IOU 的**记账写入方**随「诚实回落话术」整体拆除
    ——语音失败=拦截+待发+弹窗，不再有「回头补给你」台阶话，自然没有欠账可记。
    生产链（telegram_client / sender）不得再出现 record_iou 写入点（谁翻回来先红）。
    纯函数模块与兑现读取侧保留（无写入方=天然惰性，配置已 enabled:false）。"""
    tc = _src("telegram_client.py")
    assert "record_iou" not in tc
    assert "[voice_iou] 欠账登记" not in tc
    sender = _src("sender.py")
    assert "record_iou" not in sender
    assert "[voice_iou] 欠账登记" not in sender


def test_wiring_sender_redeem_side_still_gated():
    """兑现读取侧（pending→强制语音、真发成功清账）保留且仍受开关门控——
    无写入方时天然不触发；将来若产品决定恢复 IOU，先过本文件的纪律钉。"""
    sender = _src("sender.py")
    assert "pending_iou" in sender
    assert "clear_iou" in sender
    idx = sender.index("_iou_pending_fn(")
    guard = sender[idx - 800: idx + 400]
    assert '_iou_cfg["enabled"]' in guard
    assert "_peer_req_voice = True" in guard
    ok_idx = sender.index("def _note_voice_ok")
    ok_seg = sender[ok_idx: ok_idx + 900]
    assert "clear_iou" in ok_seg
