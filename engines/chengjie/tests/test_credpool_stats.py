"""中央凭据池观测门禁。

中央池失败是**静默降级**（回落自带凭据，用户无感），所以这组计数是判断
「池到底有没有在生效」的唯一线上依据。三条不变量：
1. `pool_share` 必须如实反映池承载比例；
2. 回落原因必须落在有限枚举里（防脏字符串把 distinct 撑爆）；
3. 观测面绝不承载密钥（api_hash / 粘定键 / 卡密一律不进）。
"""
from __future__ import annotations

import pytest

from src.integrations import credpool_bridge as cb
from src.integrations.credpool_stats import CredPoolStats


@pytest.fixture()
def st():
    return CredPoolStats()


def test_empty_is_inactive(st):
    d = st.dump()
    assert d["active"] is False and d["resolves"] == 0 and d["pool_share"] == 0.0


def test_pool_share_reflects_reality(st):
    for _ in range(3):
        st.record_resolve("credpool", "gold")
    st.record_resolve("config")
    d = st.dump()
    assert d["resolves"] == 4
    assert d["by_source"] == {"credpool": 3, "credpool_cache": 0,
                              "config": 1, "none": 0}
    assert d["pool_share"] == 0.75
    assert d["by_tier"] == {"gold": 3}


def test_cache_hits_count_as_pool_but_are_visible(st):
    """池不可达时用缓存的池凭据：仍算池承载（是同一组凭据），但要单独可见。"""
    st.record_resolve("credpool", "gold", with_proxy=True)
    st.record_resolve("credpool_cache", "gold", with_proxy=True)
    d = st.dump()
    assert d["pool_share"] == 1.0, "缓存命中也是池凭据，不该把池承载率算低"
    assert d["cache_share"] == 0.5, "降级期占比要单独可见，否则池挂了看板照样全绿"
    assert d["by_tier"] == {"gold": 2}, "降级期不该让付费档凭空消失"
    assert d["proxy_share"] == 1.0, "出口覆盖率同理"


def test_tier_defaults_to_free(st):
    st.record_resolve("credpool", None)
    st.record_resolve("credpool", "")
    assert st.dump()["by_tier"] == {"free": 2}


def test_config_source_records_no_tier(st):
    """回落自带凭据不属于任何会员档，不该污染档位分布。"""
    st.record_resolve("config", "king")
    assert st.dump()["by_tier"] == {}


def test_unknown_source_counts_as_none(st):
    st.record_resolve("wat")
    assert st.dump()["by_source"]["none"] == 1


def test_fallback_reasons_are_bounded(st):
    st.record_fallback("unreachable")
    st.record_fallback("随便一个脏字符串")
    d = st.dump()
    assert set(d["fallbacks"]) <= {"unreachable", "rejected", "no_token",
                                   "no_client", "bad_data", "error"}
    assert d["fallbacks"]["error"] == 1


def test_last_error_is_truncated(st):
    st.record_fallback("unreachable", "x" * 500)
    assert len(st.dump()["last_error"]) <= 200


def test_reports_and_releases(st):
    st.record_report(True)
    st.record_report(False)
    st.record_release()
    d = st.dump()
    assert d["reports"] == {"ok": 1, "fail": 1} and d["releases"] == 1


def test_prom_exposition_shape(st):
    st.record_resolve("credpool", "gold")
    st.record_fallback("rejected")
    text = st.dump_prom()
    assert 'credpool_resolves_total{source="credpool"} 1' in text
    assert 'credpool_tier_total{tier="gold"} 1' in text
    assert 'credpool_fallback_total{reason="rejected"} 1' in text
    assert "credpool_pool_share" in text
    assert text.endswith("\n")


def test_no_secrets_in_dump(st):
    st.record_resolve("credpool", "gold")
    st.record_fallback("rejected", "pool empty")
    blob = repr(st.dump()) + st.dump_prom()
    for forbidden in ("api_hash", "license_key", "credpool_key", "svc_"):
        assert forbidden not in blob


# ── 接线：每条决策路径都要留下痕迹 ────────────────────────────────────────

def test_bridge_records_every_fallback_reason():
    import inspect

    src = inspect.getsource(cb.allocate)
    for reason in ("no_client", "no_token", "unreachable", "rejected", "bad_data", "error"):
        assert f'"{reason}"' in src, f"回落原因 {reason} 未记账 → 线上排查会瞎"


def test_metrics_route_exposes_credpool():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "routes" / "drafts_routes.py").read_text(encoding="utf-8")
    assert 'metrics["credpool"]' in src
    assert "get_credpool_stats().dump_prom()" in src


# ── 池服务自身的健康（外部看门狗状态）──────────────────────────────────────
# 客户端指标再漂亮，池本体挂着就什么都不成立。看门狗把状态写文件，这里读给
# ops 卡。路径由配置给出且默认空——引擎要发到客户桌面，那里没有运维目录。

def _write_wd(tmp_path, **kw):
    import json
    import time

    p = tmp_path / "wd.json"
    base = {"last_probe_ts": time.time(), "last_kind": "ok", "last_detail": "allocate ok",
            "strikes": 0, "restarts": 0}
    base.update(kw)
    p.write_text(json.dumps(base), encoding="utf-8")
    return p


def test_watchdog_state_absent_without_path():
    from src.integrations.credpool_stats import watchdog_state

    assert watchdog_state("") is None, "没配路径 → 这一段不该存在（客户桌面就是这种情形）"
    assert watchdog_state("   ") is None
    assert watchdog_state(r"Z:\no\such\file.json") is None, "读不到也不能崩"


def test_watchdog_state_reports_healthy(tmp_path):
    from src.integrations.credpool_stats import watchdog_state

    d = watchdog_state(str(_write_wd(tmp_path)))
    assert d["kind"] == "ok" and d["strikes"] == 0
    assert d["degraded_min"] == 0
    assert 0 <= d["probe_age_min"] <= 1


def test_watchdog_state_reports_half_dead(tmp_path):
    """hang＝health 200 但取不到凭据。这一档是 2h20m 静默降级换来的。"""
    import time

    from src.integrations.credpool_stats import watchdog_state

    d = watchdog_state(str(_write_wd(
        tmp_path, last_kind="hang", strikes=2, restarts=1,
        degraded_since=time.time() - 1800)))
    assert d["kind"] == "hang" and d["strikes"] == 2 and d["restarts"] == 1
    assert 29 <= d["degraded_min"] <= 31, "降级时长要算出来，否则看不出严重程度"


def test_watchdog_state_exposes_its_own_staleness(tmp_path):
    """看门狗自己死了是更深的盲区——探针年龄必须可读。"""
    import time

    from src.integrations.credpool_stats import watchdog_state

    d = watchdog_state(str(_write_wd(tmp_path, last_probe_ts=time.time() - 7200)))
    assert d["probe_age_min"] >= 119


def test_watchdog_state_survives_garbage(tmp_path):
    from src.integrations.credpool_stats import watchdog_state

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert watchdog_state(str(bad)) is None
    lst = tmp_path / "lst.json"
    lst.write_text("[1,2,3]", encoding="utf-8")
    assert watchdog_state(str(lst)) is None, "不是 dict 就不该往下解析"


def test_watchdog_state_carries_no_secrets(tmp_path):
    """状态文件将来若被塞进 token，也不能经这条路漏到看板上。"""
    from src.integrations.credpool_stats import watchdog_state

    d = watchdog_state(str(_write_wd(
        tmp_path, service_token="svc_should_never_surface",
        license_key="CHATX-GOLD-0001")))
    blob = repr(d)
    assert "svc_" not in blob and "CHATX-GOLD" not in blob


def test_metrics_route_reads_watchdog_from_config(tmp_path):
    """路径必须来自配置，不得在引擎里硬编码我们的运维目录。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "routes" / "drafts_routes.py").read_text(encoding="utf-8")
    assert "watchdog_state_path" in src, "看门狗状态路径必须读配置"
    assert "chengjie-instances" not in src, "绝不能把供应商运维路径写进引擎代码"
