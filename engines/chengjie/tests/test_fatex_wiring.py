"""FateX 产品接线门禁：作用域生辰解析三层读序 + capture 双写独立库（P0-2 根治）。

用最小 stub 绑真方法跑真代码（与 test_conversation_scope 的公式同源门禁同款手法），
不依赖常驻服务。
"""
from __future__ import annotations

import logging

import pytest

from src.companion.bazi_engine import BirthInfo
from src.companion.bazi_profile import birth_info_fact_text
from src.fatex.store import (
    configure_fatex_store,
    get_fatex_store,
    reset_fatex_store_for_tests,
)
from src.skills.skill_manager import SkillManager


@pytest.fixture(autouse=True)
def _fresh_fatex_store():
    reset_fatex_store_for_tests()
    configure_fatex_store(":memory:")
    yield
    reset_fatex_store_for_tests()


class _FakeEpisodic:
    """按预置 {prefix: fact_text} 响应 list_rows 的假记忆库。"""

    def __init__(self, rows_by_prefix=None):
        self.rows_by_prefix = dict(rows_by_prefix or {})
        self.added = []

    def list_rows(self, prefix="", limit=80, source=None):
        fact = self.rows_by_prefix.get(prefix)
        return [{"content": fact}] if fact else []

    def add_fact(self, key, fact, kind, source=""):
        self.added.append((key, fact, source))
        return 1


def _stub(episodic=None):
    import types

    class _S:
        _memory_cfg = {"scope": "user"}
        _cpi = None
        _episodic_store = episodic
        logger = logging.getLogger("test_fatex_wiring")
    s = _S()
    # 绑真方法：键公式与记忆扫描跑生产代码，其余依赖保持最小
    s._episodic_storage_key = types.MethodType(
        SkillManager._episodic_storage_key, s)
    s.resolve_birth_info = types.MethodType(SkillManager.resolve_birth_info, s)
    return s


def _bi(**kw):
    base = dict(year=1995, month=3, day=5, hour=8, minute=0,
                is_lunar=False, gender="female")
    base.update(kw)
    return BirthInfo(**base)


def test_scoped_resolver_prefers_fatex_store():
    """① FateX 独立库命中 → 直接返回（不再碰记忆扫描）。"""
    get_fatex_store().upsert_birth("telegram", "8244899900", "5433982810", _bi())
    s = _stub(episodic=None)  # 记忆库故意缺席：命中独立库就不该需要它
    got = SkillManager.resolve_birth_info_scoped(
        s, "telegram", "8244899900", "5433982810")
    assert got is not None and got.year == 1995 and got.gender == "female"


def test_scoped_resolver_falls_back_to_scoped_episodic():
    """② 独立库无行 → 账号作用域记忆键前缀扫描（与写入侧同源）。"""
    fact = birth_info_fact_text(_bi(year=1992))
    s = _stub(_FakeEpisodic({"8244899900:5433982810": fact}))
    got = SkillManager.resolve_birth_info_scoped(
        s, "telegram", "8244899900", "5433982810")
    assert got is not None and got.year == 1992


def test_scoped_resolver_falls_back_to_legacy_bare_key():
    """③ 新键无 → 存量裸键兜底（键格式升级前的老数据不失忆）。"""
    fact = birth_info_fact_text(_bi(year=1990))
    s = _stub(_FakeEpisodic({"5433982810": fact}))
    got = SkillManager.resolve_birth_info_scoped(
        s, "telegram", "8244899900", "5433982810")
    assert got is not None and got.year == 1990


def test_scoped_resolver_isolated_between_accounts():
    """双号同 peer：A 号独立库有生辰，B 号任何层都不该读到 A 的行。"""
    get_fatex_store().upsert_birth("telegram", "acctA", "peer1", _bi(year=1995))
    s = _stub(_FakeEpisodic({}))
    assert SkillManager.resolve_birth_info_scoped(
        s, "telegram", "acctA", "peer1").year == 1995
    assert SkillManager.resolve_birth_info_scoped(
        s, "telegram", "acctB", "peer1") is None


@pytest.mark.asyncio
async def test_capture_birth_dual_writes_fatex():
    """capture 主链：episodic 落事实的同时，权威生辰进 FateX 独立库（账号作用域行）。"""
    epi = _FakeEpisodic({})
    s = _stub(epi)
    s._bazi_cfg = lambda: {"enabled": True}
    s.resolve_birth_info = lambda key: None

    async def _noop_embed(rid, fact):
        return None
    s._episodic_patch_embedding = _noop_embed

    await SkillManager._capture_birth_info_fact(
        s, "5433982810", "我是1995年3月5日早上8点出生的，女生",
        "好的记住啦", "", platform="telegram", account_id="8244899900")

    assert epi.added, "episodic 对话记忆仍须落一行"
    assert epi.added[0][0] == "8244899900:5433982810"
    row = get_fatex_store().get_birth("telegram", "8244899900", "5433982810")
    assert row is not None and row.year == 1995 and row.gender == "female"
    # 别的账号读不到
    assert get_fatex_store().get_birth("telegram", "other", "5433982810") is None
