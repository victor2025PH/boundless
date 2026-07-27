"""客户端试用领取桥接门禁（P2）：状态机 + 路由端到端，全程离线（HTTP 注入假官网）。

覆盖的是「用户点了『注册领 7 天』之后到底发生什么」这条链上每一个会出事的点：
重复领取不该建两单、拿不到机器指纹要当场说清楚、授权回来要真的落盘生效、
客服赠量重复入账要被幂等挡住、以及任何一步网络挂了都不能把首启向导带崩。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

import pytest

from src.licensing import trial_claim_client as tc


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    p = tmp_path / "trial_claim.json"
    monkeypatch.setattr(tc, "state_path", lambda config=None: p)
    return p


class FakeSite:
    """假官网：记录收到的请求，按脚本回应。"""

    def __init__(self, **script: Any):
        self.calls: list = []
        self.claim_resp = script.get("claim", {"ok": True, "claim_id": "tc_1", "status": "pending",
                                               "bind_code": "BC-1111-2222"})
        self.status_resp = script.get("status", {"ok": True, "status": "pending"})
        self.bind_resp = script.get("bind", {"ok": True, "bind_code": "BC-1111-2222",
                                             "telegram_url": "https://t.me/x?text=BC"})

    def __call__(self, url: str, method: str = "GET", body: Optional[dict] = None) -> Dict[str, Any]:
        self.calls.append((method, url, body))
        if "/api/trial/claim-status" in url:
            return self.status_resp
        if "/api/trial/bind-code" in url:
            return self.bind_resp
        return self.claim_resp


@pytest.fixture(autouse=True)
def _real_fingerprint(monkeypatch):
    """默认给一个稳定的假指纹，个别用例再覆盖成空。"""
    import src.licensing.machine_bridge as mb
    monkeypatch.setattr(mb, "machine_fingerprint", lambda: "A1B2-C3D4-E5F6-0789")


# ── 领取 ───────────────────────────────────────────────────────────────────

def test_claim_persists_state_and_sends_fingerprint(state_file):
    site = FakeSite()
    res = tc.claim("@bob", fetch=site)
    assert res["ok"] and res["claim_id"] == "tc_1"
    _m, _u, body = site.calls[0]
    assert body["fingerprint"] == "A1B2-C3D4-E5F6-0789"
    assert body["contact"] == "@bob"
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["claim_id"] == "tc_1" and saved["bind_code"] == "BC-1111-2222"


def test_second_claim_does_not_create_another_order(state_file):
    site = FakeSite()
    tc.claim("@bob", fetch=site)
    res = tc.claim("@bob", fetch=site)
    assert res["ok"] and res.get("already") is True
    assert len(site.calls) == 1, "本地已有单号就不该再打官网"


def test_claim_without_fingerprint_fails_loudly(state_file, monkeypatch):
    """拿不到指纹 = 签出来的授权本机也验不过，必须当场报错而不是静默建单。"""
    import src.licensing.machine_bridge as mb
    monkeypatch.setattr(mb, "machine_fingerprint", lambda: "")
    site = FakeSite()
    res = tc.claim("@bob", fetch=site)
    assert res == {"ok": False, "error": "no_fingerprint"}
    assert not site.calls and not state_file.exists()


def test_claim_network_failure_is_soft(state_file):
    res = tc.claim("@bob", fetch=lambda *a, **k: {"ok": False, "error": "network"})
    assert res["ok"] is False and res["error"] == "network"
    assert not state_file.exists(), "失败不该留下半条状态，用户重试要能重新建单"


@pytest.mark.parametrize("raw,want", [
    ("bob", "@bob"),
    ("@bob", "@bob"),
    ("https://t.me/bob", "@bob"),
    ("t.me/@bob", "@bob"),
    ("+8613800138000", "+8613800138000"),
    ("13800138000", "13800138000"),
    ("", ""),
])
def test_contact_normalization(raw, want):
    assert tc.normalize_contact(raw) == want


# ── 轮询 ───────────────────────────────────────────────────────────────────

def test_poll_before_claim_is_noop(state_file):
    assert tc.poll(fetch=FakeSite()) == {"ok": True, "claimed": False}


def test_poll_surfaces_license_once_then_stops(state_file):
    site = FakeSite(status={"ok": True, "status": "issued", "license": "LIC.TOKEN"})
    tc.claim("@bob", fetch=site)

    first = tc.poll(fetch=site)
    assert first["license"] == "LIC.TOKEN"

    tc.mark_activated()  # 路由激活成功后会调它
    second = tc.poll(fetch=site)
    assert "license" not in second, "已激活就别再把授权在网络上搬来搬去"
    assert second["activated"] is True


def test_poll_surfaces_voucher_once(state_file):
    site = FakeSite(status={"ok": True, "status": "issued", "topup_voucher": "V.TOKEN"})
    tc.claim("@bob", fetch=site)
    assert tc.poll(fetch=site)["topup_voucher"] == "V.TOKEN"
    tc.mark_topup(100_000)
    after = tc.poll(fetch=site)
    assert "topup_voucher" not in after
    assert after["gift_redeemed"] is True and after["gift_chars"] == 100_000


def test_poll_records_then_clears_error(state_file):
    site = FakeSite()
    tc.claim("@bob", fetch=site)
    bad = tc.poll(fetch=lambda *a, **k: {"ok": False, "error": "network"})
    assert bad["ok"] is False and bad["last_error"] == "network"
    good = tc.poll(fetch=site)
    assert good["ok"] is True and good["last_error"] == ""


def test_summary_never_leaks_credentials(state_file):
    site = FakeSite(status={"ok": True, "status": "issued", "license": "LIC.TOKEN"})
    tc.claim("@bob", fetch=site)
    tc.poll(fetch=site)
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert "license" not in saved and "topup_voucher" not in saved, \
        "本地状态文件只记进度，凭证归 license.key / 额度库"
    assert "license" not in tc.summarize(saved)


# ── 绑定码 ─────────────────────────────────────────────────────────────────

def test_bind_code_requires_claim(state_file):
    assert tc.bind_code(fetch=FakeSite())["error"] == "not_claimed"


def test_bind_code_returns_deeplinks(state_file):
    site = FakeSite()
    tc.claim("@bob", fetch=site)
    r = tc.bind_code(fetch=site)
    assert r["ok"] and r["bind_code"] == "BC-1111-2222"
    assert r["telegram_url"].startswith("https://t.me/")


# ── 站点配置 ───────────────────────────────────────────────────────────────

def test_site_url_override():
    assert tc.site_url({}) == tc.DEFAULT_SITE
    assert tc.site_url({"licensing": {"trial": {"site_url": "http://localhost:3571/"}}}) \
        == "http://localhost:3571"


# ── 路由层：授权真落地 + 赠量真入账 ────────────────────────────────────────

def test_route_activates_license_and_redeems_gift(state_file, tmp_path, monkeypatch):
    """端到端：假官网发回真签发的授权与凭证 → 路由把两者都落地。"""
    monkeypatch.undo()  # 先撤掉假指纹，再按模块属性取值（真实本机指纹才过绑机校验）
    import src.licensing.machine_bridge as mb
    from src.licensing.chatx_fulfillment import build_trial_payload
    from src.licensing.license_manager import generate_keypair, issue_license
    from src.licensing.topup_voucher import issue_topup_voucher
    from src.web.routes import license_routes as lr

    fp = mb.machine_fingerprint()
    if not fp:
        pytest.skip("本机指纹不可用")
    monkeypatch.setattr(tc, "state_path", lambda config=None: state_file)

    kp = generate_keypair()
    lic_path = tmp_path / "license.key"
    import src.licensing as lic_pkg
    from src.licensing.license_manager import LicenseManager
    mgr = LicenseManager(license_path=str(lic_path), public_key_hex=kp["public_hex"])
    monkeypatch.setattr(lic_pkg, "get_license_manager", lambda *a, **k: mgr)
    monkeypatch.setattr("src.licensing.license_manager.get_license_manager", lambda *a, **k: mgr)

    monkeypatch.setattr(mb, "machine_fingerprint", lambda: fp)
    tc.claim("@bob", fetch=FakeSite())  # 先有单子，激活才有进度可标

    token = issue_license(
        build_trial_payload(customer="@bob", machine=fp, claim_id="tc_1"), kp["private_hex"])
    res = lr._consume_trial_payload({"ok": True}, token, "")
    assert res.get("activated") is True, res.get("activate_error")
    assert lic_path.read_text(encoding="utf-8").strip() == token, "授权必须真写进 license.key"
    assert json.loads(state_file.read_text(encoding="utf-8")).get("activated_at")

    # 赠量：同一张凭证连兑两次，第二次必须被 ref 幂等挡住而不是再加一笔。
    voucher = issue_topup_voucher(kp["private_hex"], chars=100_000,
                                  ref="trial-topup-tc_1", lic_id=f"trial-{fp.replace('-', '')[:8]}")
    r1 = lr._consume_trial_payload({"ok": True}, "", voucher)
    r2 = lr._consume_trial_payload({"ok": True}, "", voucher)
    assert r1.get("gift_redeemed") or r1.get("gift_error"), r1
    if r1.get("gift_redeemed"):
        assert r2.get("gift_redeemed") is True and not r2.get("gift_error"), \
            "重复兑换应按幂等命中处理，不能报错吓用户"


def test_routes_see_real_config_so_site_url_is_configurable():
    """回归钉：路由层必须真能读到配置，否则 site_url 静默回落成默认官网。

    2026-07-27 实锤事故：`_cfg_or_none` 原本 `from src.utils.config_manager import
    config_manager`——**该符号不存在**（那个模块只导出类），ImportError 被 except 吞掉，
    于是配置永远是空 dict、`licensing.trial.site_url` 永远无效。表现极隐蔽：功能看着
    全对，只是本地测试台把试用单子建到了**生产台账**里。
    """
    from src.web.routes import license_routes as lr

    class FakeCM:
        config = {"licensing": {"trial": {"site_url": "http://localhost:9999"}}}

    app_stub = type("A", (), {"get": lambda *a, **k: (lambda f: f),
                              "post": lambda *a, **k: (lambda f: f)})()
    lr.register_license_routes(app_stub, api_auth=lambda r: None, config_manager=FakeCM())
    assert lr._cfg_or_none() == FakeCM.config
    assert tc.site_url(lr._cfg_or_none()) == "http://localhost:9999", \
        "路由拿不到配置 → 会连默认官网，本地测试会污染生产台账"


def test_register_license_routes_is_wired_with_config_manager():
    """admin.py 必须把 config_manager 注入进来——漏注入等于上面那条又坏一次。"""
    from pathlib import Path as _P
    src = (_P(__file__).resolve().parent.parent / "src" / "web" / "admin.py").read_text(
        encoding="utf-8")
    idx = src.find("register_license_routes(app")
    assert idx > 0, "找不到 register_license_routes 调用点"
    assert "config_manager=config_manager" in src[idx:idx + 200], \
        "register_license_routes 调用点漏了 config_manager 注入"


def test_route_reports_bad_license_without_crashing(state_file):
    from src.web.routes import license_routes as lr
    res = lr._consume_trial_payload({"ok": True}, "not-a-token", "")
    assert res["ok"] is True and res.get("activated") is not True
    assert res["activate_error"] == "invalid"


def test_reclaim_on_used_machine_says_exhausted_not_invalid(state_file, tmp_path, monkeypatch):
    """同机重领会按机器码去重、拿回**上次那张已过期的**授权。

    这条路径不修的话用户只会看到「invalid」，像是系统坏了；实际语义是
    「这台机器的 7 天已经用完」——差别决定了向导该弹「重试」还是「去购买」。
    """
    monkeypatch.undo()
    import src.licensing as lic_pkg
    import src.licensing.machine_bridge as mb
    from src.licensing.chatx_fulfillment import build_trial_payload
    from src.licensing.license_manager import LicenseManager, generate_keypair, issue_license
    from src.web.routes import license_routes as lr

    fp = mb.machine_fingerprint()
    if not fp:
        pytest.skip("本机指纹不可用")
    monkeypatch.setattr(tc, "state_path", lambda config=None: state_file)
    monkeypatch.setattr(mb, "machine_fingerprint", lambda: fp)

    kp = generate_keypair()
    mgr = LicenseManager(license_path=str(tmp_path / "license.key"),
                         public_key_hex=kp["public_hex"])
    monkeypatch.setattr(lic_pkg, "get_license_manager", lambda *a, **k: mgr)

    tc.claim("@bob", fetch=FakeSite())
    stale = build_trial_payload(customer="@bob", machine=fp)
    stale["exp"] = int(time.time()) - 86400  # 上个月领的那张
    token = issue_license(stale, kp["private_hex"])

    res = lr._consume_trial_payload({"ok": True}, token, "")
    assert res.get("trial_exhausted") is True
    assert res.get("activate_error") == "expired"
    assert res.get("activated") is not True
    assert not (tmp_path / "license.key").exists(), "过期授权不得落盘顶掉现状"
    assert tc.summarize(tc.load_state())["exhausted"] is True
