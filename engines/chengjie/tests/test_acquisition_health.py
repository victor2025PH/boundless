"""个人号获客健康聚合（P8）门禁：把 P0/P6/P7 治理成果收口成看板读数。

守：
1. 只聚 ``personal_rpa`` 账号（device/official 等本机接管的号不混入获客健康）；
2. 风控计数（risk_source 注入，模拟 P0/P6 记的 flood/error）真实喂进评分与灯色；
3. platform 保留（个人号跨 wa/line/tg，运营要知道哪台）、最差灯排前；
4. 零 personal_rpa 账号 → applicable=False（整卡隐藏）；registry 缺失/异常不抛。
"""
from __future__ import annotations

from src.integrations.leadbus_account import PERSONAL_RPA_MODE
from src.ops.acquisition_health import collect_acquisition_health


class _FakeRegistry:
    def __init__(self, rows):
        self._rows = rows

    def list(self):
        return list(self._rows)

    def get(self, platform, account_id):
        for r in self._rows:
            if r.get("platform") == platform and r.get("account_id") == account_id:
                return r
        return None


def _row(account_id, *, platform="whatsapp", mode=PERSONAL_RPA_MODE, **extra):
    row = {"platform": platform, "account_id": account_id, "mode": mode,
           "label": account_id, "proxy_id": "px1", "status": "online",
           "created_at": 0.0, "meta": {}}
    row.update(extra)
    return row


def _risk(mapping):
    """risk_source 替身：{(platform, account_id): {"flood": n, "error": m}}。"""
    def _rc(platform, account_id, *, now=None):
        return mapping.get((platform, account_id), {})
    return _rc


def test_only_personal_rpa_accounts_counted():
    reg = _FakeRegistry([
        _row("wa_dev_1"),
        _row("wa_device_native", mode="device"),      # 本机 RPA runner 接管，不算获客
        _row("ig_biz", mode="official"),
    ])
    out = collect_acquisition_health(reg, risk_source=_risk({}))
    assert out["total"] == 1
    assert out["accounts"][0]["account_id"] == "wa_dev_1"
    assert out["applicable"] is True


def test_risk_counts_drive_light():
    reg = _FakeRegistry([
        _row("healthy"),
        _row("flooded"),
    ])
    # flooded 号近 24h 3 次限频（P0/P6 记的）→ 扣分 → 非绿；最差排前
    out = collect_acquisition_health(reg, risk_source=_risk({
        ("whatsapp", "flooded"): {"flood": 3, "error": 1},
    }))
    by_id = {a["account_id"]: a for a in out["accounts"]}
    assert by_id["flooded"]["flood_waits_24h"] == 3
    assert by_id["flooded"]["errors_24h"] == 1
    assert by_id["flooded"]["score"] < by_id["healthy"]["score"]
    assert out["accounts"][0]["account_id"] == "flooded", "最差灯/分排前"


def test_platform_preserved_cross_device():
    reg = _FakeRegistry([
        _row("a1", platform="whatsapp"),
        _row("a2", platform="line"),
    ])
    out = collect_acquisition_health(reg, risk_source=_risk({}))
    plats = {a["platform"] for a in out["accounts"]}
    assert plats == {"whatsapp", "line"}, "跨设备平台必须保留（运营要知道哪台）"


def test_banned_account_red():
    reg = _FakeRegistry([_row("banned", meta={"banned": True})])
    out = collect_acquisition_health(reg, risk_source=_risk({}))
    assert out["light"] == "red"
    assert out["counts"]["red"] == 1
    assert out["accounts"][0]["light"] == "red"


def test_no_personal_rpa_not_applicable():
    reg = _FakeRegistry([_row("d", mode="device")])
    out = collect_acquisition_health(reg, risk_source=_risk({}))
    assert out["applicable"] is False
    assert out["total"] == 0 and out["light"] == "none"


def test_empty_and_broken_registry_safe():
    assert collect_acquisition_health(None)["applicable"] is False

    class _Boom:
        def list(self):
            raise RuntimeError("db locked")
    assert collect_acquisition_health(_Boom())["applicable"] is False


def test_route_registered():
    """/api/admin/acquisition-health 在 ops_overview 路由域注册。"""
    from fastapi import FastAPI
    from src.web.routes.ops_overview_routes import register_ops_overview_routes
    app = FastAPI()

    class _Ctx:
        api_auth = staticmethod(lambda request: None)
        api_write = staticmethod(lambda request: None)
        page_auth = staticmethod(lambda request: None)
        templates = None
        config_manager = None
        audit_store = None
        telegram_client = None

    register_ops_overview_routes(app, _Ctx())
    live = {(getattr(r, "path", ""), m)
            for r in app.routes
            for m in (getattr(r, "methods", None) or set())
            if m not in {"HEAD", "OPTIONS"}}
    assert ("/api/admin/acquisition-health", "GET") in live
