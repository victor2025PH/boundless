# -*- coding: utf-8 -*-
"""融合实例 P3 精简版门禁：会员中心页 + API + 顶栏档位徽章 + nav 锁标渲染。

conftest 全量 admin app；档位一律走 plan_override（不依赖宿主机真实 license）。
nav 锁标的纯逻辑在 test_feature_gate.py::test_nav_context_marks_locked_items，
这里补 HTML 渲染面（锁定项渲染为 /membership 跳转 + 徽章出现在顶栏）。
"""


def _set_gate(config_manager, enabled=True, override=None):
    lic = config_manager.config.setdefault("licensing", {})
    fg = {"enabled": enabled}
    if override:
        fg["plan_override"] = override
    lic["feature_gate"] = fg


def test_membership_api_snapshot_shape(auth_client, config_manager):
    _set_gate(config_manager, override="basic")
    r = auth_client.get("/api/admin/membership")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["gate"]["enabled"] is True
    assert d["gate"]["plan"] == "basic"
    assert "companion" in d["gate"]["locked"]
    assert "translation_suite" not in d["gate"]["locked"]
    # 渲染友好矩阵：行×档位格，与 FEATURE_MIN_PLAN 单源一致
    plans = d["matrix"]["plans"]
    assert plans == ["community", "basic", "pro", "flagship"]
    rows = {row["name"]: row for row in d["matrix"]["rows"]}
    assert rows["translation_suite"]["cells"] == [False, True, True, True]
    assert rows["companion"]["cells"] == [False, False, False, True]
    assert rows["companion"]["allowed"] is False
    # 席位/额度块存在（值随环境，形状必须在）
    assert "seats" in d and "limit" in d["seats"]
    assert "quota" in d and "included_chars" in d["quota"]


def test_membership_api_gate_off(auth_client, config_manager):
    _set_gate(config_manager, enabled=False)
    d = auth_client.get("/api/admin/membership").json()
    assert d["gate"]["enabled"] is False
    assert d["gate"]["locked"] == []


def test_membership_page_renders(auth_client, config_manager):
    """注意：_i18n_bootstrap.html 会把整包 i18n 以 JSON（\\u 转义）嵌进每页供
    window.T 用——「裸键不得出现在 html」的断言天然不成立；改断言服务端已把
    键解析成字面 CJK（JSON 里的值是 \\u 转义，字面 CJK 只能来自模板渲染）。"""
    _set_gate(config_manager, override="pro")
    r = auth_client.get("/membership")
    assert r.status_code == 200
    html = r.text
    # 矩阵标题/档位名被服务端解析成字面文本（非 \u 转义 → 来自模板而非 i18n JSON 包）
    assert ("功能矩阵" in html) or ("Feature matrix" in html)
    assert ("专业版" in html) or (">Pro<" in html)
    # 矩阵格子真渲染（✓）且 pro 档存在锁定行标（flagship 专属功能）
    assert "✓" in html
    assert ("未包含" in html) or ("Not included" in html)


def test_topbar_badge_and_nav_lock_render(auth_client, config_manager):
    """gate 开 → 顶栏出档位徽章；被锁 nav 项渲染为 /membership 锁标跳转。

    base.html 的 CSS 永远含 .plan-badge/.nav-locked 选择器文本 → 断言必须
    锚定渲染出的元素标记（class="..."），不能裸搜类名。"""
    _set_gate(config_manager, override="basic")
    html = auth_client.get("/").text
    assert 'class="plan-badge plan-badge-basic"' in html
    # basic 档：知识库(kb=pro) 应以锁标形式指向会员中心（E4 起带 ?from=<族> 来源参数）
    assert 'href="/membership?from=' in html
    assert 'class="nav-locked"' in html


def test_topbar_badge_absent_when_gate_off(auth_client, config_manager):
    _set_gate(config_manager, enabled=False)
    html = auth_client.get("/").text
    assert 'class="plan-badge' not in html
    assert 'class="nav-locked"' not in html


# ── P4b：购买/续费 CTA + 坐席工作台徽章 + charpack 加量包 ────────────────────

def test_shop_cta_renders_when_configured(auth_client, config_manager):
    """licensing.shop_url 配置 → 会员页出「购买/续费」CTA；未配 → 零渲染。"""
    _set_gate(config_manager, override="basic")
    config_manager.config["licensing"]["shop_url"] = "https://shop.example/pricing"
    d = auth_client.get("/api/admin/membership").json()
    # 非 /order 基址原样透传（TG/外链商城不被深链污染）
    assert d["shop"]["url"] == "https://shop.example/pricing"
    html = auth_client.get("/membership").text
    assert 'href="https://shop.example/pricing"' in html
    assert ("购买 / 续费" in html) or ("Buy / Renew" in html)

    config_manager.config["licensing"].pop("shop_url")
    html2 = auth_client.get("/membership").text
    assert "shop.example" not in html2


def test_shop_cta_deeplinks_order_by_plan(auth_client, config_manager):
    """P7：shop_url 指向 /order 时按当前档拼 ?plan=（basic → autochat-entry）。"""
    _set_gate(config_manager, override="basic")
    config_manager.config["licensing"]["shop_url"] = "https://bd2026.cc/order"
    d = auth_client.get("/api/admin/membership").json()
    assert "plan=autochat-entry" in d["shop"]["url"]
    assert d["shop"]["offer"] == "autochat-entry"
    html = auth_client.get("/membership").text
    assert "plan=autochat-entry" in html


def test_workspace_topbar_plan_badge(auth_client, config_manager):
    """gate 开 → 坐席工作台顶栏渲染档位 pill（master 可点进会员中心）；关 → 无。"""
    _set_gate(config_manager, override="pro")
    html = auth_client.get("/workspace").text
    assert 'class="ws-plan-pill ws-plan-pro"' in html
    assert 'href="/membership"' in html
    _set_gate(config_manager, enabled=False)
    html2 = auth_client.get("/workspace").text
    assert 'class="ws-plan-pill' not in html2


def test_topup_route_idempotent(auth_client, tmp_path, monkeypatch):
    """POST /api/admin/license/topup：入账→quota 生效；同 ref 重复→duplicate_ref。"""
    from types import SimpleNamespace

    import src.licensing.quota_store as qs

    qs.reset_license_quota_store()
    qs.configure_license_quota_store(db_path=tmp_path / "topup.db")
    monkeypatch.setattr(qs, "_current_status", lambda: SimpleNamespace(
        licensed=True, included_chars=1000, lic_id="LIC-T", enforce=True,
        state="active"))
    try:
        r = auth_client.post("/api/admin/license/topup",
                             json={"chars": 500, "ref": "ORD-77", "note": "测试"})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True and d["included"] == 1500
        assert d["topup_chars"] == 500

        dup = auth_client.post("/api/admin/license/topup",
                               json={"chars": 500, "ref": "ORD-77"}).json()
        assert dup["ok"] is False and dup["error"] == "duplicate_ref"
        assert dup.get("detail")  # i18n 文案已解析

        q = qs.check_license_quota()
        assert q["included"] == 1500 and q["topup_chars"] == 500
    finally:
        qs.reset_license_quota_store()


# ── P4c：加量凭证自助兑换 ────────────────────────────────────────────────────

def test_topup_voucher_route_success_and_error_mapping(auth_client, monkeypatch):
    """POST /api/admin/license/topup-voucher：成功透传；错误码映射两族 i18n detail。

    验签/绑定/幂等的真密码学全链在 test_topup_voucher.py；这里守路由胶水
    （voucher 族 vs topup 族的 detail 键选择 + 结果透传）。"""
    import src.licensing.topup_voucher as tv

    monkeypatch.setattr(tv, "redeem_topup_voucher", lambda v: {
        "ok": True, "chars": 50000, "ref": "ORD-1",
        "lic_id": "L", "topup_chars": 50000, "included": 150000})
    d = auth_client.post("/api/admin/license/topup-voucher",
                         json={"voucher": "x.y"}).json()
    assert d["ok"] is True and d["chars"] == 50000 and "detail" not in d

    for err, needle_zh in [
        ("bad_signature", "验签失败"),          # voucher 族
        ("customer_mismatch", "其他客户"),       # voucher 族
        ("duplicate_ref", "已入账"),             # 入账层 → topup 族
    ]:
        monkeypatch.setattr(tv, "redeem_topup_voucher",
                            lambda v, e=err: {"ok": False, "error": e})
        d = auth_client.post("/api/admin/license/topup-voucher",
                             json={"voucher": "x.y"}).json()
        assert d["ok"] is False and d["error"] == err
        assert d.get("detail") and "err.lic." not in d["detail"], d
        assert (needle_zh in d["detail"]) or d["detail"].isascii()


def test_membership_page_renders_redeem_form(auth_client, monkeypatch):
    """master + 带额度授权 → 兑换表单渲染；不限量授权 → 表单隐藏（充了也无意义）。"""
    import src.licensing.quota_store as qs

    monkeypatch.setattr(qs, "check_license_quota", lambda **kw: {
        "included": 1000, "included_base": 1000, "topup_chars": 0,
        "used": 0, "remaining": 1000, "exceeded": False})
    html = auth_client.get("/membership").text
    assert 'id="mb-voucher-input"' in html and 'id="mb-voucher-btn"' in html

    monkeypatch.setattr(qs, "check_license_quota", lambda **kw: {
        "included": 0, "included_base": 0, "topup_chars": 0,
        "used": 0, "remaining": None, "exceeded": False})
    html2 = auth_client.get("/membership").text
    assert 'id="mb-voucher-input"' not in html2


def test_topup_route_guards(auth_client, tmp_path, monkeypatch):
    """未激活授权 → not_licensed（含 i18n detail）；不限量授权 → unlimited。"""
    from types import SimpleNamespace

    import src.licensing.quota_store as qs

    qs.reset_license_quota_store()
    qs.configure_license_quota_store(db_path=tmp_path / "topup2.db")
    monkeypatch.setattr(qs, "_current_status", lambda: SimpleNamespace(
        licensed=False, included_chars=0, lic_id="", enforce=False,
        state="unlicensed"))
    try:
        d = auth_client.post("/api/admin/license/topup",
                             json={"chars": 100, "ref": "R1"}).json()
        assert d["ok"] is False and d["error"] == "not_licensed"
        assert d.get("detail")

        monkeypatch.setattr(qs, "_current_status", lambda: SimpleNamespace(
            licensed=True, included_chars=0, lic_id="L", enforce=False,
            state="active"))
        d2 = auth_client.post("/api/admin/license/topup",
                              json={"chars": 100, "ref": "R1"}).json()
        assert d2["ok"] is False and d2["error"] == "unlimited"
    finally:
        qs.reset_license_quota_store()


# ── P5：按月用量趋势 ─────────────────────────────────────────────────────────

def test_membership_page_renders_usage_trend(auth_client, tmp_path, monkeypatch):
    """带额度授权 + 历史用量 → 趋势卡渲染（柱 + 月份标签）；空历史 → 卡隐藏。"""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    import src.licensing.quota_store as qs

    qs.reset_license_quota_store()
    store = qs.LicenseQuotaStore(tmp_path / "trend.db")
    qs.configure_license_quota_store(store=store)
    monkeypatch.setattr(qs, "_current_status", lambda: SimpleNamespace(
        licensed=True, included_chars=100000, lic_id="LIC-TR", enforce=False,
        state="active"))
    try:
        ts_may = datetime(2026, 5, 10, tzinfo=timezone.utc).timestamp()
        ts_jun = datetime(2026, 6, 20, tzinfo=timezone.utc).timestamp()
        store.record("LIC-TR", "translate", 1200, now=ts_may)
        store.record("LIC-TR", "tts", 800, now=ts_may + 3600)
        store.record("LIC-TR", "translate", 5000, now=ts_jun)

        html = auth_client.get("/membership").text
        assert 'id="mb-trend-bars"' in html
        assert ("用量趋势" in html) or ("Usage trend" in html)
        # 月份标签（month[5:]）+ 千位缩写标注（2.0k / 5.0k）
        assert ">05<" in html and ">06<" in html
        assert "2.0k" in html and "5.0k" in html

        # 无任何用量记录的授权 → history 空 → 卡隐藏
        monkeypatch.setattr(qs, "_current_status", lambda: SimpleNamespace(
            licensed=True, included_chars=100000, lic_id="LIC-EMPTY",
            enforce=False, state="active"))
        html2 = auth_client.get("/membership").text
        assert 'id="mb-trend-bars"' not in html2
    finally:
        qs.reset_license_quota_store()
