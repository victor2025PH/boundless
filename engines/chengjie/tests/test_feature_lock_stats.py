# -*- coding: utf-8 -*-
"""E6 门禁：功能锁触达观测——「哪个功能的锁被撞得最多」（定价/打包信号）。

不变量：
- 计数只发生在**license 锁**拦截面（API 403 / 页面 302 升级引导）；
- 运营 kill-switch（config 关闭）**刻意不计**——部署选择不是购买意向；
- gate 总开关关 → 全放行零计数；
- 观测失败绝不影响拦截语义（record 全部 best-effort try/except 包裹）；
- 读出面：/api/workspace/metrics.feature_lock + Prometheus 两行族。
"""

from src.web.feature_lock_stats import FeatureLockStats, get_feature_lock_stats


def _set_lic(config_manager, plan=None, gate_on=True):
    lic = config_manager.config.setdefault("licensing", {})
    fg = {"enabled": gate_on}
    if plan:
        fg["plan_override"] = plan
    lic["feature_gate"] = fg


# ── 纯语义 ───────────────────────────────────────────────────────────────────

def test_record_dump_semantics():
    s = FeatureLockStats()
    s.record("kb", "page")
    s.record("kb", "api")
    s.record("kb", "api")
    s.record("workflows", "page")
    d = s.dump()
    assert d["total"] == 4
    assert d["by_family"]["kb"] == {"api": 2, "page": 1, "total": 3}
    assert d["by_family"]["workflows"] == {"api": 0, "page": 1, "total": 1}
    # 排序：总量降序
    assert list(d["by_family"]) == ["kb", "workflows"]


def test_sanitize_and_kind_guard():
    s = FeatureLockStats()
    s.record("KB", "api")                    # 大写归一
    s.record("bad family!", "api")           # 非法族名 → unknown
    s.record("kb", "click")                  # 非法 kind → 不计
    d = s.dump()
    assert d["total"] == 2
    assert d["by_family"]["kb"]["api"] == 1
    assert d["by_family"]["unknown"]["api"] == 1


def test_prom_format_and_reset():
    s = FeatureLockStats()
    s.record("care", "page")
    text = s.dump_prom()
    assert "feature_lock_hits_total 1" in text
    assert 'feature_lock_hits_by_family_total{family="care",kind="page"} 1' in text
    assert 'feature_lock_hits_by_family_total{family="care",kind="api"} 0' in text
    s.reset()
    assert s.dump()["total"] == 0


# ── 接线（conftest 全量 admin app） ──────────────────────────────────────────

def test_page_and_api_lock_hits_counted(auth_client, config_manager):
    st = get_feature_lock_stats()
    st.reset()
    _set_lic(config_manager, plan="basic")
    auth_client.get("/knowledge", follow_redirects=False)          # 页面锁 kb
    auth_client.get("/api/kb/anything")                            # API 前缀锁 kb
    auth_client.get("/api/workspace/workflow-chains")              # 模块闸 workflows
    d = st.dump()
    assert d["by_family"]["kb"] == {"api": 1, "page": 1, "total": 2}
    assert d["by_family"]["workflows"]["api"] == 1


def test_config_off_workflows_not_counted(auth_client, config_manager):
    """运营 kill-switch 的 403 不是购买意向，绝不进定价信号。"""
    st = get_feature_lock_stats()
    st.reset()
    config_manager.config.setdefault("inbox", {})["workflows"] = {"enabled": False}
    r = auth_client.get("/api/workspace/workflow-chains")
    assert r.status_code == 403          # 拦是拦了
    assert st.dump()["total"] == 0       # 但不计数


def test_gate_off_zero_counting(auth_client, config_manager):
    st = get_feature_lock_stats()
    st.reset()
    _set_lic(config_manager, plan="basic", gate_on=False)
    assert auth_client.get("/knowledge").status_code == 200
    auth_client.get("/api/kb/anything")   # gate 关 → 中间件跳过（404 无所谓）
    assert st.dump()["total"] == 0


def test_metrics_endpoint_exposes_feature_lock(auth_client, config_manager):
    st = get_feature_lock_stats()
    st.reset()
    _set_lic(config_manager, plan="basic")
    auth_client.get("/knowledge", follow_redirects=False)
    d = auth_client.get("/api/workspace/metrics").json()
    assert "feature_lock" in d
    assert d["feature_lock"]["by_family"]["kb"]["page"] == 1
    # Prometheus 面
    text = auth_client.get("/api/workspace/metrics?format=prometheus").text
    assert "feature_lock_hits_total" in text
