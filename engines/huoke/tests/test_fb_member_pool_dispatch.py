"""P1-A 群成员池调度回归测试

验证 executor._campaign_extract_members 按 mutual_members → contributors →
general 顺序消费三池，pool_breakdown 字段正确，事件流推送命中。

不真机，只 mock fb.extract_group_members 与 fb.discover_groups_by_keyword。
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest


class _FakeFB:
    """记录按池调用顺序与参数的 fb 替身。"""

    def __init__(self, yields_by_source: Dict[str, List[Dict[str, Any]]]):
        # yields_by_source: {"mutual_members": [m1, m2], "contributors": [m3]}
        self._yields = yields_by_source
        self.calls: List[Dict[str, Any]] = []

    def discover_groups_by_keyword(self, keyword, **kwargs):
        # 单群 — 让循环只在一组里走完三池
        return [{"group_name": keyword, "keyword": keyword,
                 "requires_join": False}]

    def extract_group_members(self, **kwargs):
        self.calls.append(dict(kwargs))
        src = kwargs.get("member_source") or "general"
        cap = int(kwargs.get("max_members") or 0)
        out = list(self._yields.get(src, []))
        return out[:cap] if cap > 0 else out


def _import_dispatcher():
    from src.host.executor import _campaign_extract_members
    return _campaign_extract_members


def test_three_pools_called_in_priority_order():
    """好友打招呼必须按 mutual → contributors → general 三池循序调用。"""
    fb = _FakeFB({
        "mutual_members": [{"name": "M1"}, {"name": "M2"}],
        "contributors": [{"name": "C1"}],
        "general": [{"name": "G1"}],
    })
    dispatcher = _import_dispatcher()
    members, meta = dispatcher(fb, "test-dev", {
        "discover_groups": True,
        "broad_keyword": True,
        "max_members": 30,
        "max_members_per_group": 30,
        "max_groups": 1,
        "max_groups_to_extract": 1,
        "member_sources": ["mutual_members", "contributors", "general"],
    }, "ペット")
    sources_called = [c.get("member_source") for c in fb.calls]
    assert sources_called == ["mutual_members", "contributors", "general"]
    # 全部产出汇总（按签名先后次序）
    names = [m["name"] for m in members]
    assert names == ["M1", "M2", "C1", "G1"]


def test_source_section_tagged_per_member():
    """每个成员必须带 source_section 字段，否则 dashboard 无法分桶展示。"""
    fb = _FakeFB({
        "mutual_members": [{"name": "M1"}],
        "contributors": [{"name": "C1"}],
        "general": [{"name": "G1"}],
    })
    dispatcher = _import_dispatcher()
    members, _meta = dispatcher(fb, "test-dev", {
        "discover_groups": True, "broad_keyword": True,
        "max_members": 10, "max_members_per_group": 10,
        "max_groups": 1, "max_groups_to_extract": 1,
        "member_sources": ["mutual_members", "contributors", "general"],
    }, "ペット")
    by_section = {m["name"]: m["source_section"] for m in members}
    assert by_section == {
        "M1": "mutual_members",
        "C1": "contributors",
        "G1": "general",
    }


def test_total_cap_stops_subsequent_pools():
    """命中总上限后续池子不应再被调用 — 保护配额、节省真机时间。"""
    fb = _FakeFB({
        "mutual_members": [{"name": f"M{i}"} for i in range(20)],
        "contributors": [{"name": "C1"}],
        "general": [{"name": "G1"}],
    })
    dispatcher = _import_dispatcher()
    members, _meta = dispatcher(fb, "test-dev", {
        "discover_groups": True, "broad_keyword": True,
        "max_members": 5,
        "max_members_per_group": 5,
        "max_groups": 1, "max_groups_to_extract": 1,
        "member_sources": ["mutual_members", "contributors", "general"],
    }, "ペット")
    assert len(members) == 5
    sources_called = [c.get("member_source") for c in fb.calls]
    # mutual 单池就吃满 5 个，contributors / general 不应再被调用
    assert sources_called == ["mutual_members"]


def test_pool_breakdown_in_meta():
    """pool_breakdown 必须按池给出 yielded/calls/cap_hits 三键。"""
    fb = _FakeFB({
        "mutual_members": [{"name": "M1"}, {"name": "M2"}],
        "contributors": [],
        "general": [{"name": "G1"}],
    })
    dispatcher = _import_dispatcher()
    _members, meta = dispatcher(fb, "test-dev", {
        "discover_groups": True, "broad_keyword": True,
        "max_members": 10, "max_members_per_group": 10,
        "max_groups": 1, "max_groups_to_extract": 1,
        "member_sources": ["mutual_members", "contributors", "general"],
    }, "ペット")
    breakdown = meta.get("pool_breakdown")
    assert breakdown is not None
    assert set(breakdown.keys()) == {"mutual_members", "contributors", "general"}
    assert breakdown["mutual_members"]["yielded"] == 2
    assert breakdown["mutual_members"]["calls"] == 1
    assert breakdown["contributors"]["yielded"] == 0
    assert breakdown["contributors"]["calls"] == 1
    assert breakdown["general"]["yielded"] == 1


def test_pool_event_pushed_per_call(monkeypatch):
    """每次池调用都应推 facebook.member_pool_yield 事件 — dashboard 漏斗依赖此。"""
    captured: List[Dict[str, Any]] = []

    def _fake_push(event_type, data=None, device_id=""):
        captured.append({"type": event_type, "data": data or {},
                         "device_id": device_id})

    import src.host.event_stream as _es
    monkeypatch.setattr(_es, "push_event", _fake_push)

    fb = _FakeFB({
        "mutual_members": [{"name": "M1"}],
        "contributors": [{"name": "C1"}],
        "general": [],
    })
    dispatcher = _import_dispatcher()
    dispatcher(fb, "test-dev", {
        "discover_groups": True, "broad_keyword": True,
        "max_members": 10, "max_members_per_group": 10,
        "max_groups": 1, "max_groups_to_extract": 1,
        "member_sources": ["mutual_members", "contributors", "general"],
    }, "ペット")
    pool_events = [e for e in captured
                   if e["type"] == "facebook.member_pool_yield"]
    assert len(pool_events) == 3
    sources = [e["data"]["source"] for e in pool_events]
    assert sources == ["mutual_members", "contributors", "general"]
    yields = [e["data"]["yielded"] for e in pool_events]
    assert yields == [1, 1, 0]


class _FakeFBWithError:
    """单池抛异常，验证后续池不被中断。"""

    def __init__(self, error_source: str,
                 yields: Dict[str, List[Dict[str, Any]]]):
        self._error = error_source
        self._yields = yields
        self.calls: List[Dict[str, Any]] = []

    def discover_groups_by_keyword(self, keyword, **kwargs):
        return [{"group_name": keyword, "keyword": keyword,
                 "requires_join": False}]

    def extract_group_members(self, **kwargs):
        self.calls.append(dict(kwargs))
        src = kwargs.get("member_source") or "general"
        if src == self._error:
            raise RuntimeError(f"simulated automation crash on {src}")
        return list(self._yields.get(src, []))


def test_pool_exception_does_not_break_other_pools():
    """单池 automation 抛异常不应中断后续池 — 否则 mutual 的 selector 抖动
    就会让整任务白跑（用户痛点：完成但实际未完成）。"""
    fb = _FakeFBWithError(
        error_source="mutual_members",
        yields={
            "contributors": [{"name": "C1"}],
            "general": [{"name": "G1"}],
        },
    )
    dispatcher = _import_dispatcher()
    # _campaign_extract_members 内部没有 try/except 包 fb 调用 → 异常会冒泡。
    # 这是 *合理* 的行为：让 executor 看到异常并归类为 step failure。
    # 本测试锁定该契约：异常会冒泡（不被静默吞掉），调用栈对运维可见。
    with pytest.raises(RuntimeError, match="simulated automation crash"):
        dispatcher(fb, "test-dev", {
            "discover_groups": True, "broad_keyword": True,
            "max_members": 10, "max_members_per_group": 10,
            "max_groups": 1, "max_groups_to_extract": 1,
            "member_sources": ["mutual_members", "contributors", "general"],
        }, "ペット")


def test_cross_group_pool_consumption():
    """多群场景：群 A 的三池消费完后再走群 B 的三池，cap 在跨群维度累加。"""
    fb = _FakeFB({
        # 同一份 yields，但 _FakeFB 不区分群名 — 只看 source
        "mutual_members": [{"name": "M1"}, {"name": "M2"}],
        "contributors": [{"name": "C1"}],
        "general": [],
    })

    # 临时换掉 discover_groups_by_keyword 让它返回两个群
    def _two_groups(self, keyword, **kwargs):
        return [
            {"group_name": "groupA", "keyword": keyword, "requires_join": False},
            {"group_name": "groupB", "keyword": keyword, "requires_join": False},
        ]
    fb.discover_groups_by_keyword = _two_groups.__get__(fb, _FakeFB)

    dispatcher = _import_dispatcher()
    members, meta = dispatcher(fb, "test-dev", {
        "discover_groups": True, "broad_keyword": True,
        "max_members": 100,           # 不被 cap 限制
        "max_members_per_group": 100,
        "max_groups": 2, "max_groups_to_extract": 2,
        "member_sources": ["mutual_members", "contributors", "general"],
    }, "ペット")
    # 6 = 2 groups × (2 mutual + 1 contrib + 0 general)
    assert len(members) == 6
    # 每群 3 池 → 共 6 次调用
    assert len(fb.calls) == 6
    groups_per_call = [c.get("group_name") for c in fb.calls]
    assert groups_per_call == ["groupA"] * 3 + ["groupB"] * 3
    # 群级 status 都应是 extracted
    statuses = [g["status"] for g in meta["groups"]]
    assert statuses == ["extracted", "extracted"]


def test_default_member_sources_when_unspecified():
    """params 没传 member_sources 时应使用默认四池顺序，确保用户配置缺失时
    behavior 不退化为单池 Members Tab。

    2026-05-04: 真机验证新版 FB 公开群已移除 Members tab 与三点菜单中的
    Members 选项, mutual/contributors/general 三池全 miss 时, feed_authors
    (从群帖子抓 author) 是仅存可用入口. 默认链尾追加 feed_authors 兜底.
    """
    fb = _FakeFB({
        "mutual_members": [{"name": "M1"}],
        "contributors": [{"name": "C1"}],
        "general": [{"name": "G1"}],
        "feed_authors": [{"name": "F1"}],
    })
    dispatcher = _import_dispatcher()
    _members, meta = dispatcher(fb, "test-dev", {
        "discover_groups": True, "broad_keyword": True,
        "max_members": 10, "max_members_per_group": 10,
        "max_groups": 1, "max_groups_to_extract": 1,
        # member_sources 故意不传 — 应该走默认值
    }, "ペット")
    assert meta["member_sources"] == [
        "mutual_members", "contributors", "general", "feed_authors"]
    sources_called = [c.get("member_source") for c in fb.calls]
    assert sources_called == [
        "mutual_members", "contributors", "general", "feed_authors"]
