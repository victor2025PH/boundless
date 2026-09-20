"""S5: Tests for CrossPlatformIdentity module."""
import tempfile
from pathlib import Path
import pytest
from src.utils.cross_platform_identity import (
    CrossPlatformIdentity,
    link_and_merge_memory,
)


@pytest.fixture
def cpi(tmp_path):
    return CrossPlatformIdentity(tmp_path / "bot.db")


class TestResolve:
    def test_new_uid_gets_default_canonical(self, cpi):
        c = cpi.resolve("telegram", "12345")
        assert c == "telegram:12345"

    def test_same_uid_returns_same_canonical(self, cpi):
        c1 = cpi.resolve("line_rpa", "Uabc")
        c2 = cpi.resolve("line_rpa", "Uabc")
        assert c1 == c2

    def test_different_platforms_different_defaults(self, cpi):
        ct = cpi.resolve("telegram", "999")
        cl = cpi.resolve("line_rpa", "999")
        assert ct != cl

    def test_empty_platform_returns_uid(self, cpi):
        assert cpi.resolve("", "abc") == "abc"

    def test_empty_uid_returns_empty(self, cpi):
        assert cpi.resolve("telegram", "") == ""


class TestLink:
    def test_link_shares_canonical(self, cpi):
        c = cpi.link("telegram", "111", "line_rpa", "Uaaa")
        assert c == "telegram:111"
        assert cpi.resolve("line_rpa", "Uaaa") == "telegram:111"

    def test_linked_uid_is_persistent(self, cpi):
        cpi.link("telegram", "222", "messenger_rpa", "fb_222")
        assert cpi.resolve("messenger_rpa", "fb_222") == "telegram:222"

    def test_link_returns_canonical_of_a(self, cpi):
        # Pre-create a with custom canonical by linking it first
        cpi.link("telegram", "A", "line_rpa", "B")
        # Now link B to whatsapp C — canon should remain telegram:A
        c = cpi.link("telegram", "A", "whatsapp_rpa", "C")
        assert c == "telegram:A"
        assert cpi.resolve("whatsapp_rpa", "C") == "telegram:A"


class TestUnlink:
    def test_unlink_restores_own_canonical(self, cpi):
        cpi.link("telegram", "333", "line_rpa", "Ubbb")
        assert cpi.resolve("line_rpa", "Ubbb") == "telegram:333"
        new_c = cpi.unlink("line_rpa", "Ubbb")
        assert new_c == "line_rpa:Ubbb"
        assert cpi.resolve("line_rpa", "Ubbb") == "line_rpa:Ubbb"

    def test_unlink_leaves_other_party_intact(self, cpi):
        cpi.link("telegram", "444", "line_rpa", "Uccc")
        cpi.unlink("line_rpa", "Uccc")
        assert cpi.resolve("telegram", "444") == "telegram:444"


class TestListAndGetByCanonical:
    def test_list_returns_all_rows(self, cpi):
        cpi.resolve("telegram", "X1")
        cpi.resolve("line_rpa", "X2")
        rows = cpi.list_all()
        platforms = [r[0] for r in rows]
        assert "telegram" in platforms
        assert "line_rpa" in platforms

    def test_get_by_canonical_finds_linked(self, cpi):
        cpi.link("telegram", "T1", "line_rpa", "L1")
        pairs = cpi.get_by_canonical("telegram:T1")
        platforms = [p[0] for p in pairs]
        assert "telegram" in platforms
        assert "line_rpa" in platforms


class _RecStore:
    """记录 merge_key 调用的最小情景记忆库替身。"""

    def __init__(self, rows_per_merge: int = 3):
        self.calls = []
        self._rows = rows_per_merge

    def merge_key(self, old_key, new_key):
        self.calls.append((old_key, new_key))
        return self._rows


class TestLinkAndMergeMemory:
    """P10：手动 link（AI Studio 路径）与影子确认同口径的记忆合流 + 簇传递。"""

    def test_basic_merge_moves_b_history(self, cpi):
        store = _RecStore()
        out = link_and_merge_memory(
            cpi, store, "telegram", "111", "whatsapp", "639")
        assert out["canonical_id"] == "telegram:111"
        assert store.calls == [("whatsapp:639", "telegram:111")]
        assert out["memory_rows_merged"] == 3
        assert out["merged_from"] == ["whatsapp:639"]
        assert cpi.resolve("whatsapp", "639") == "telegram:111"

    def test_no_store_still_links(self, cpi):
        out = link_and_merge_memory(
            cpi, None, "telegram", "111", "whatsapp", "639")
        assert out["canonical_id"] == "telegram:111"
        assert out["memory_rows_merged"] == 0
        assert cpi.resolve("whatsapp", "639") == "telegram:111"

    def test_already_same_canonical_is_noop_merge(self, cpi):
        cpi.link("telegram", "111", "whatsapp", "639")
        store = _RecStore()
        out = link_and_merge_memory(
            cpi, store, "telegram", "111", "whatsapp", "639")
        assert out["canonical_id"] == "telegram:111"
        assert store.calls == []            # pre_b == canon → 零合并
        assert out["cluster_relinked"] == []

    def test_cluster_members_follow(self, cpi):
        # 既有簇：line L1 ← whatsapp 639（共享 whatsapp:639）
        cpi.link("whatsapp", "639", "line", "L1")
        store = _RecStore()
        out = link_and_merge_memory(
            cpi, store, "telegram", "111", "whatsapp", "639")
        canon = out["canonical_id"]
        assert canon == "telegram:111"
        # 簇友 line 一并改挂（传递性）；旧簇史合流一次
        assert cpi.resolve("line", "L1") == canon
        assert {(r["platform"], r["uid"]) for r in out["cluster_relinked"]} \
            == {("line", "L1")}
        assert store.calls == [("whatsapp:639", canon)]

    def test_merge_failure_never_blocks_link(self, cpi):
        class _Boom:
            def merge_key(self, *_a):
                raise RuntimeError("locked")

        out = link_and_merge_memory(
            cpi, _Boom(), "telegram", "111", "whatsapp", "639")
        assert out["canonical_id"] == "telegram:111"
        assert out["memory_rows_merged"] == 0
        assert cpi.resolve("whatsapp", "639") == "telegram:111"
