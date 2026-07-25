"""「账号隔离健康」观测门禁：键形态分类 / 人设绑定 / 总评 / API 端到端。

纯核心 = src/utils/isolation_health.py（复用迁移工具 classify_key 口径）；
路由 = /api/admin/isolation-health（ops_overview_routes.py，60s TTL 缓存）。
app 构造沿用同路由文件既有测试的假 Ctx 模式（见 test_send_route_trend_store.py）。
"""
from __future__ import annotations

import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.utils.isolation_health import (
    build_isolation_health,
    classify_episodic_keys,
    summarize_persona_binding,
)


# ── 键形态分类（口径 = account_scope_migration.classify_key） ────────────────

def test_classify_key_forms_and_row_sums():
    stats = [
        ("telegram:8244899900:5433982810", 12),        # scoped
        ("whatsapp:639270135480:639273815533", 3),     # scoped
        ("telegram:5433982810", 7),                    # legacy_platform
        ("5433982810", 5),                             # bare
        ("line_rpa:c1", 2),                            # 组合键（含下划线）→ other
        ("bogus:xx:yy", 1),                            # 未知平台三段 → other
    ]
    out = classify_episodic_keys(stats)
    assert out["scoped"] == 2 and out["scoped_rows"] == 15
    assert out["legacy_platform"] == 1 and out["legacy_rows"] == 7
    assert out["bare"] == 1 and out["bare_rows"] == 5
    assert out["other"] == 2


def test_classify_empty_input_all_zero():
    assert classify_episodic_keys([]) == {
        "scoped": 0, "legacy_platform": 0, "bare": 0, "other": 0,
        "scoped_rows": 0, "legacy_rows": 0, "bare_rows": 0,
    }


def test_classify_narrowed_platforms_moves_foreign_to_other():
    """known_platforms 收窄时，界外平台的 scoped/legacy 键归 other（口径由入参说了算）。"""
    out = classify_episodic_keys(
        [("telegram:a1:p1", 1), ("line:p2", 2)], known_platforms={"line"})
    assert out["scoped"] == 0
    assert out["legacy_platform"] == 1 and out["legacy_rows"] == 2
    assert out["other"] == 1


def test_classify_bad_count_tolerated():
    out = classify_episodic_keys([("telegram:a:p", "oops"), ("p2", None)])
    assert out["scoped"] == 1 and out["scoped_rows"] == 0
    assert out["bare"] == 1 and out["bare_rows"] == 0


# ── 人设绑定摘要 ─────────────────────────────────────────────────────────────

def _acct(platform="telegram", account_id="a1", status="online", meta=None, label=""):
    return {"platform": platform, "account_id": account_id, "status": status,
            "label": label, "meta": meta if meta is not None else {}}


def test_persona_binding_bound_unbound_and_offline_excluded():
    rows = [
        _acct(account_id="a1", meta={"persona_id": "lin_jiaxin"}),
        _acct(account_id="a2", meta={"persona_ids": ["su_qingyan", "x"]}),
        _acct(account_id="a3", meta={}, label="新号"),
        _acct(account_id="a4", status="offline", meta={}),   # offline 不计
        _acct(account_id="a5", status="pending", meta={}),   # 非 online 不计
    ]
    s = summarize_persona_binding(rows)
    assert s["online"] == 3 and s["bound"] == 2
    assert s["unbound"] == [
        {"platform": "telegram", "account_id": "a3", "label": "新号"}]


def test_persona_binding_empty_meta_variants_are_unbound():
    rows = [
        _acct(account_id="a1", meta={"persona_id": ""}),
        _acct(account_id="a2", meta={"persona_ids": []}),
        _acct(account_id="a3", meta={"persona_ids": [""]}),
        _acct(account_id="a4", meta=None),
    ]
    s = summarize_persona_binding(rows)
    assert s["bound"] == 0 and s["online"] == 4
    assert len(s["unbound"]) == 4


def test_persona_binding_unbound_list_capped_at_8():
    rows = [_acct(account_id=f"a{i}") for i in range(12)]
    s = summarize_persona_binding(rows)
    assert s["online"] == 12 and s["bound"] == 0
    assert len(s["unbound"]) == 8   # 计数不封顶，点名列表封顶


def test_persona_binding_empty_input():
    assert summarize_persona_binding([]) == {"online": 0, "bound": 0, "unbound": []}


# ── 总评 build ───────────────────────────────────────────────────────────────

def test_build_warn_on_unbound_online_account():
    out = build_isolation_health([], [_acct(meta={})], {"birth_profiles": 0})
    assert out["level"] == "warn"
    assert out["personas"]["online"] == 1 and out["personas"]["bound"] == 0


def test_build_legacy_ratio_boundary_20_percent():
    # 4 scoped + 1 legacy = 20%：不越线（>20% 才 warn）
    stats = [(f"telegram:a:p{i}", 1) for i in range(4)] + [("telegram:p5", 1)]
    out = build_isolation_health(stats, [], {})
    assert out["level"] == "ok" and out["legacy_ratio"] == 0.2
    # 再加一条 bare → 2/6 = 33% → warn（组合键 other 不入分母）
    out2 = build_isolation_health(stats + [("p6", 1), ("line_rpa:c1", 9)], [], {})
    assert out2["level"] == "warn" and out2["legacy_ratio"] == round(2 / 6, 3)


def test_build_ok_when_all_green_and_fatex_passthrough():
    stats = [(f"telegram:acct:p{i}", 2) for i in range(5)]
    accounts = [_acct(meta={"persona_id": "p"}),
                _acct(account_id="a9", status="offline", meta={})]
    fx = {"birth_profiles": 3, "by_platform": {"telegram": 3}, "complete": 1}
    out = build_isolation_health(stats, accounts, fx)
    assert out["level"] == "ok"
    assert out["keys"]["scoped"] == 5 and out["keys"]["scoped_rows"] == 10
    assert out["personas"] == {"online": 1, "bound": 1, "unbound": []}
    assert out["fatex"] == fx


def test_build_empty_inputs_is_ok():
    out = build_isolation_health([], [], None)
    assert out["level"] == "ok"
    assert out["legacy_ratio"] == 0.0
    assert out["fatex"] == {}


# ── API 端到端（/api/admin/isolation-health） ────────────────────────────────

def _make_ops_app(telegram_client=None, config_manager=None):
    from src.web.routes.ops_overview_routes import register_ops_overview_routes

    class _Ctx:
        def api_auth(self, request):
            return True

        def api_write(self, perm):
            def _dep():
                return True
            return _dep

        def page_auth(self, request):
            return True

        templates = None
        config_manager = None
        audit_store = None
        user_store = None
        token = None

    ctx = _Ctx()
    ctx.telegram_client = telegram_client
    if config_manager is not None:
        ctx.config_manager = config_manager
    app = FastAPI()
    register_ops_overview_routes(app, ctx)
    return app


class _FakeEpisodicStore:
    def __init__(self, stats):
        self._stats = stats

    def list_key_stats(self):
        return list(self._stats)


def _fake_assistant(stats):
    sm = types.SimpleNamespace(_episodic_store=_FakeEpisodicStore(stats))
    return types.SimpleNamespace(skill_manager=sm)


@pytest.fixture(autouse=True)
def _reset_isolation_cache():
    """路由 60s TTL 缓存按测试清零（模块级 dict，测试间互不串味）。"""
    from src.web.routes import ops_overview_routes as mod

    mod._ISOLATION_CACHE.update({"ts": 0.0, "data": None})
    yield
    mod._ISOLATION_CACHE.update({"ts": 0.0, "data": None})


@pytest.fixture()
def _fatex_memory():
    """FateX 单例重定向到 :memory:（防测试碰生产 config/fatex.db）。"""
    from src.fatex.store import configure_fatex_store, reset_fatex_store_for_tests

    reset_fatex_store_for_tests()
    configure_fatex_store(":memory:")
    yield
    reset_fatex_store_for_tests()


def test_api_isolation_health_end_to_end(_fatex_memory):
    from src.fatex.store import get_fatex_store
    from src.integrations.account_registry import get_account_registry

    reg = get_account_registry()   # conftest autouse 已重定向到临时库
    reg.upsert("telegram", "111", status="online", label="主号",
               meta={"persona_id": "lin_jiaxin"})
    reg.upsert("telegram", "222", status="online", label="新号", meta={})
    reg.upsert("whatsapp", "333", status="offline", meta={})

    info = types.SimpleNamespace(year=1995, month=3, day=5, hour=8, minute=0,
                                 is_lunar=False, gender="female")
    assert get_fatex_store().upsert_birth("telegram", "111", "u1", info)

    app = _make_ops_app(_fake_assistant([
        ("telegram:111:5433982810", 6),   # scoped
        ("telegram:5433982810", 2),       # legacy_platform
        ("999888", 1),                    # bare
    ]))
    c = TestClient(app, raise_server_exceptions=True)
    d = c.get("/api/admin/isolation-health").json()
    assert d["ok"] is True
    assert d["level"] == "warn"   # 222 在线未绑人设
    assert d["personas"] == {
        "online": 2, "bound": 1,
        "unbound": [{"platform": "telegram", "account_id": "222", "label": "新号"}]}
    assert d["keys"]["scoped"] == 1 and d["keys"]["scoped_rows"] == 6
    assert d["keys"]["legacy_platform"] == 1 and d["keys"]["bare"] == 1
    assert d["fatex"]["birth_profiles"] == 1 and d["fatex"]["complete"] == 1


def test_api_isolation_health_ttl_cache_and_force(_fatex_memory):
    from src.integrations.account_registry import get_account_registry

    app = _make_ops_app(None)   # 无 assistant → key_stats 按空算
    c = TestClient(app, raise_server_exceptions=True)
    d1 = c.get("/api/admin/isolation-health").json()
    assert d1["ok"] is True and d1["personas"]["online"] == 0
    # 60s 窗口内命中缓存：新上号不可见；force=1 绕过缓存立即可见
    get_account_registry().upsert("telegram", "999", status="online", meta={})
    d2 = c.get("/api/admin/isolation-health").json()
    assert d2["personas"]["online"] == 0
    d3 = c.get("/api/admin/isolation-health?force=1").json()
    assert d3["personas"]["online"] == 1 and d3["level"] == "warn"


def test_api_isolation_health_soft_fails_when_fatex_unavailable(monkeypatch):
    """三路数据源软失败：FateX 读挂 → 仍 200，fatex 按空算（绝不 5xx）。"""
    import src.fatex.store as fx

    def _boom():
        raise RuntimeError("fatex down")

    monkeypatch.setattr(fx, "get_fatex_store", _boom)
    app = _make_ops_app(None)
    c = TestClient(app, raise_server_exceptions=True)
    d = c.get("/api/admin/isolation-health").json()
    assert d["ok"] is True and d["fatex"] == {}


# ── 趋势落库（ops.isolation_trend，默认关） ──────────────────────────────────

def _trend_cm(config_path=None):
    """带 ops.isolation_trend.enabled=True 的假 config_manager。"""
    return types.SimpleNamespace(
        config={"ops": {"isolation_trend": {"enabled": True}}},
        config_path=config_path,
    )


@pytest.fixture()
def _trend_memory():
    """趋势 store 单例重定向到 :memory:（防测试碰实例 config 目录）。"""
    from src.utils.isolation_trend_store import (
        configure_isolation_trend_store,
        reset_isolation_trend_store,
    )

    reset_isolation_trend_store()
    configure_isolation_trend_store(":memory:")
    yield
    reset_isolation_trend_store()


def test_api_isolation_trend_off_by_default(_fatex_memory):
    """开关默认关：响应不带 trend/regression 键（feature flag 纪律锁定行为）。"""
    app = _make_ops_app(None)   # config_manager=None → 开关读不到 → 关
    c = TestClient(app, raise_server_exceptions=True)
    d = c.get("/api/admin/isolation-health").json()
    assert d["ok"] is True
    assert "trend" not in d and "regression" not in d


def test_api_isolation_trend_enabled_writes_and_returns(_fatex_memory, _trend_memory):
    """开关开：当日水位落库，响应带 trend（≤7 天升序）+ regression；同日重拍幂等一行。"""
    import time as _time

    app = _make_ops_app(None, config_manager=_trend_cm())
    c = TestClient(app, raise_server_exceptions=True)
    d = c.get("/api/admin/isolation-health").json()
    assert d["ok"] is True
    assert isinstance(d["trend"], list) and len(d["trend"]) == 1
    row = d["trend"][-1]
    assert row["day"] == _time.strftime("%Y-%m-%d", _time.localtime())
    for col in ("scoped_keys", "legacy_keys", "bare_keys",
                "online_accounts", "unbound_accounts", "fatex_rows"):
        assert col in row
    assert d["regression"] == {"regressed": False, "detail": ""}   # 单日无对比
    # 同日再拍（force 绕 TTL 缓存）→ 覆盖更新仍一行
    d2 = c.get("/api/admin/isolation-health?force=1").json()
    assert len(d2["trend"]) == 1


def test_api_isolation_trend_enabled_but_unconfigured_skips(_fatex_memory):
    """开关开但 store 未 configure 且无 config_path 可懒建 → 静默跳过，仍 200 无 trend 键。"""
    from src.utils.isolation_trend_store import reset_isolation_trend_store

    reset_isolation_trend_store()
    app = _make_ops_app(None, config_manager=_trend_cm(config_path=None))
    c = TestClient(app, raise_server_exceptions=True)
    d = c.get("/api/admin/isolation-health").json()
    assert d["ok"] is True
    assert "trend" not in d and "regression" not in d


def test_api_isolation_trend_lazy_configures_next_to_config(tmp_path, _fatex_memory):
    """开关开 + 有 config_path → 懒建 config 同目录 isolation_trend.db（fatex.db 同模式）。"""
    from src.utils.isolation_trend_store import (
        get_isolation_trend_store,
        reset_isolation_trend_store,
    )

    reset_isolation_trend_store()
    try:
        cm = _trend_cm(config_path=tmp_path / "config.yaml")
        app = _make_ops_app(None, config_manager=cm)
        c = TestClient(app, raise_server_exceptions=True)
        d = c.get("/api/admin/isolation-health").json()
        assert "trend" in d and len(d["trend"]) == 1
        assert (tmp_path / "isolation_trend.db").exists()
        assert get_isolation_trend_store() is not None
    finally:
        reset_isolation_trend_store()


def test_api_isolation_trend_store_error_still_200(_fatex_memory, monkeypatch):
    """store 抛异常 → best-effort 吞掉：仍 200，快照体完整，只是不带 trend 键。"""
    import src.utils.isolation_trend_store as its

    class _Boom:
        def upsert_today(self, snapshot):
            raise RuntimeError("trend db down")

    monkeypatch.setattr(its, "get_isolation_trend_store", lambda: _Boom())
    app = _make_ops_app(None, config_manager=_trend_cm())
    c = TestClient(app, raise_server_exceptions=True)
    d = c.get("/api/admin/isolation-health").json()
    assert d["ok"] is True and "level" in d
    assert "trend" not in d and "regression" not in d
