"""风控隔离三盾「在册覆盖率」门禁。

为什么要有这个持久口径：看板上的三盾读的是**本进程登录事件**比率，
一重启计数归零 → 盾牌显示 0%，而账号其实都隔离着。运营看到 0% 会以为
隔离没生效，去做多余的排查甚至误关功能。覆盖率按注册表里当下在册的号算，
重启不受影响。

同时守住分母口径：只算协议号、不算已移除的号——否则官方 API 号和历史删号
会把分母撑大，覆盖率永远上不去，看板变成常年报警。
"""
from __future__ import annotations

import json

from src.integrations.isolation_shields import build_shields, collect_shields


def _acct(mode="protocol", status="online", meta=None, proxy_id=""):
    return {"mode": mode, "status": status, "proxy_id": proxy_id,
            "meta": meta if meta is not None else {}}


def test_empty_is_all_zero():
    out = build_shields([])
    assert out["total"] == 0 and out["full_isolated"] == 0
    for k in ("credential", "egress", "fingerprint"):
        assert out[k] == {"covered": 0, "total": 0, "pct": 0.0}


def test_counts_each_shield_independently():
    out = build_shields([
        _acct(meta={"credpool_key": "k1"}),
        _acct(meta={"device_fp": {"device_model": "x"}}),
        _acct(proxy_id="px1"),
        _acct(),
    ])
    assert out["total"] == 4
    assert out["credential"]["covered"] == 1
    assert out["fingerprint"]["covered"] == 1
    assert out["egress"]["covered"] == 1
    assert out["full_isolated"] == 0, "各占一件套不算完整隔离"


def test_full_isolation_needs_all_three():
    out = build_shields([
        _acct(meta={"credpool_key": "k", "device_fp": {"m": 1}}, proxy_id="p"),
        _acct(meta={"credpool_key": "k"}, proxy_id="p"),
    ])
    assert out["full_isolated"] == 1
    assert out["credential"]["pct"] == 1.0
    assert out["fingerprint"]["pct"] == 0.5


def test_only_protocol_accounts_count():
    """官方 API / RPA 号不走凭据池，算进分母会让覆盖率永远上不去。"""
    out = build_shields([
        _acct(mode="protocol", meta={"credpool_key": "k"}),
        _acct(mode="official"),
        _acct(mode="rpa"),
    ])
    assert out["total"] == 1 and out["credential"]["pct"] == 1.0


def test_removed_accounts_excluded():
    out = build_shields([
        _acct(meta={"credpool_key": "k"}),
        _acct(status="removed"),
    ])
    assert out["total"] == 1


def test_reads_meta_json_string_form():
    """注册表里 meta 是 JSON 字符串列（meta_json），不能只认已解析的 dict。"""
    out = build_shields([
        {"mode": "protocol", "status": "online", "proxy_id": "",
         "meta_json": json.dumps({"credpool_key": "k", "device_fp": {"m": 1}})},
    ])
    assert out["credential"]["covered"] == 1 and out["fingerprint"]["covered"] == 1


def test_broken_meta_does_not_crash():
    out = build_shields([
        {"mode": "protocol", "status": "online", "meta_json": "{不是json"},
        {"mode": "protocol", "status": "online", "meta": "也不是dict"},
    ])
    assert out["total"] == 2 and out["credential"]["covered"] == 0


def test_weakest_points_at_the_thing_to_fix():
    out = build_shields([
        _acct(meta={"credpool_key": "k", "device_fp": {"m": 1}}),
        _acct(meta={"credpool_key": "k", "device_fp": {"m": 1}}),
    ])
    assert out["weakest"] == "egress", "两个号都缺出口 → 应指向出口"


def test_legacy_accounts_counted_separately():
    """存量号（池上线前登录的）要单列，否则覆盖率永远到不了 100%。

    这不是统计口味问题：按 `credpool_bridge.resolve_for_account` 的规矩，没有粘定键
    的号刻意继续用配置自带凭据（换 api_id 会与既有 session 错配 → 封号级信号），
    所以它们的覆盖率**运维补货补不动**，只有重新登录才升级。混在一个分子里，
    卡片就长年停在低位，运营会连真实缺口一起无视。
    """
    out = build_shields([
        _acct(meta={"credpool_key": "k1", "device_fp": {"m": 1}}),
        _acct(),  # 存量号：无粘定键
        _acct(),
        _acct(),
    ])
    assert out["total"] == 4
    assert out["pooled"] == 1, "池内号＝有粘定键的号"
    assert out["legacy"] == 3, "剩下的都是待重登升级的存量号"
    assert out["pooled"] + out["legacy"] == out["total"], "两者必须正好切满分母"


def test_legacy_zero_when_all_pooled():
    out = build_shields([_acct(meta={"credpool_key": "k"}) for _ in range(3)])
    assert out["legacy"] == 0 and out["pooled"] == 3


def test_collect_never_raises():
    """观测永远不该把主流程带崩——读不到注册表就返回空壳。"""
    out = collect_shields()
    assert set(out) >= {"credential", "egress", "fingerprint", "total", "weakest",
                        "pooled", "legacy"}


def test_metrics_route_exposes_shields():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "routes" / "drafts_routes.py").read_text(encoding="utf-8")
    assert 'collect_shields' in src and '_cp["shields"]' in src


def test_card_renders_coverage_line():
    """盾牌重启归零时，这一行是唯一还能说真话的东西，不能被删。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "templates" / "ops_overview.html").read_text(encoding="utf-8")
    assert "ov2_cp_coverage" in src and "coverHtml" in src
