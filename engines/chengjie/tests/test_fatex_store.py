"""FateX 独立生辰库门禁：账号隔离 / 补全合并 / 兼容回落 / 软失败 / 配置门面。"""
from __future__ import annotations

import pytest

from src.companion.bazi_engine import BirthInfo
from src.fatex.config import fatex_cfg, fatex_enabled
from src.fatex.store import FatexStore


@pytest.fixture()
def store():
    return FatexStore(":memory:")


def _bi(**kw):
    base = dict(year=1995, month=3, day=5, hour=-1, minute=0,
                is_lunar=False, gender="")
    base.update(kw)
    return BirthInfo(**base)


def test_roundtrip_scoped_by_account(store):
    """双号同 peer 各存各的生辰，互不可见——产品级账号隔离的根。"""
    assert store.upsert_birth("telegram", "8244899900", "5433982810", _bi(year=1995))
    assert store.upsert_birth("telegram", "8755679833", "5433982810", _bi(year=1988))
    a = store.get_birth("telegram", "8244899900", "5433982810")
    b = store.get_birth("telegram", "8755679833", "5433982810")
    assert a and a.year == 1995
    assert b and b.year == 1988
    # 第三个号没写过 → 取不到（绝不跨账号串）
    assert store.get_birth("telegram", "6834964252", "5433982810") is None


def test_merge_keeps_known_hour_and_gender(store):
    """先报「生日+时辰+性别」，后仅更正日期 → 时辰/性别保留（补全合并语义）。"""
    store.upsert_birth("telegram", "a1", "u1", _bi(hour=8, gender="female"))
    store.upsert_birth("telegram", "a1", "u1", _bi(day=6))  # 更正日期，未提时辰性别
    got = store.get_birth("telegram", "a1", "u1")
    assert got is not None
    assert got.day == 6 and got.hour == 8 and got.gender == "female"


def test_new_known_values_override(store):
    """新值已知 → 覆盖（用户补时辰/更正性别）。"""
    store.upsert_birth("telegram", "a1", "u1", _bi())
    store.upsert_birth("telegram", "a1", "u1", _bi(hour=22, gender="male"))
    got = store.get_birth("telegram", "a1", "u1")
    assert got.hour == 22 and got.gender == "male"


def test_legacy_fallback_row_without_account(store):
    """account='' 的存量行：精确键未命中时回落可读（迁移过渡），有精确行则精确优先。"""
    store.upsert_birth("whatsapp", "", "639273815533", _bi(year=1990))
    got = store.get_birth("whatsapp", "639270135480", "639273815533")
    assert got and got.year == 1990
    store.upsert_birth("whatsapp", "639270135480", "639273815533", _bi(year=1992))
    got2 = store.get_birth("whatsapp", "639270135480", "639273815533")
    assert got2 and got2.year == 1992


def test_delete_and_stats(store):
    store.upsert_birth("telegram", "a1", "u1", _bi(hour=8, gender="female"))
    store.upsert_birth("line", "a2", "u2", _bi())
    st = store.stats()
    assert st["birth_profiles"] == 2 and st["complete"] == 1
    assert st["by_platform"].get("telegram") == 1
    assert store.delete_birth("telegram", "a1", "u1")
    assert store.get_birth("telegram", "a1", "u1") is None


def test_invalid_inputs_soft_fail(store):
    assert store.upsert_birth("telegram", "a1", "", _bi()) is False
    assert store.upsert_birth("telegram", "a1", "u1", None) is False
    assert store.get_birth("telegram", "a1", "") is None


def test_fatex_cfg_merges_legacy_and_modern():
    cfg = {
        "companion": {"bazi": {"enabled": True, "kline": {"enabled": True},
                               "sticky_minutes": 10}},
        "fatex": {"kline": {"enabled": False}},
    }
    merged = fatex_cfg(cfg)
    assert merged["enabled"] is True            # 旧键兜底
    assert merged["kline"]["enabled"] is False  # 新键覆盖
    assert merged["sticky_minutes"] == 10
    assert fatex_enabled(cfg) is True
    assert fatex_enabled({}) is False
    assert fatex_cfg(None) == {}
