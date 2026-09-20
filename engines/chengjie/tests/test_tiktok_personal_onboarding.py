"""TK-3 E2：接入页个人号页签数据 + 小智口径（不宣称官方个人号私信）。"""
from __future__ import annotations

from types import SimpleNamespace

from src.assistant.howto_pack import build_howto_entries
from src.integrations import platform_registry as preg
from src.web.routes import onboarding_guide_routes as og


class _Req:
    def __init__(self, cfg):
        self.app = SimpleNamespace(state=SimpleNamespace(config_manager=SimpleNamespace(config=cfg)))
        self.headers = {}
        self.url = SimpleNamespace(scheme="http", netloc="test")


def test_tiktok_panel_exposes_personal_tabs_default_off():
    panel = og.tiktok_connect_panel(_Req({}))
    assert panel["tabs"] == ["official", "personal_rpa", "web"]
    assert panel["personal_rpa"]["bridge_enabled"] is False and panel["personal_rpa"]["notice"] == "notice_unofficial"
    assert panel["personal_rpa"]["accounts"] == []
    assert panel["web"]["ready"] is False and panel["web"]["phase"] == "assistOnly"
    assert panel["web"]["notice"] == "notice_unofficial"


def test_tiktok_panel_lists_huoke_devices_when_bridge_on(tmp_path):
    from src.integrations import tiktok_huoke_bridge as hb
    st = hb.TikTokHuokeStateStore(str(tmp_path / "hb.db"))
    hb.bind_device({"device_id": "phone-1", "account_id": "acc-ph", "username": "shop_ph",
                    "timezone": "Asia/Manila"}, config={"tiktok": {"huoke_bridge": {"enabled": True}}},
                   state=st, now=1_800_000_000.0, registry=SimpleNamespace(upsert=lambda *a, **k: None))
    cfg = {"tiktok": {"huoke_bridge": {"enabled": True, "state_db_path": str(tmp_path / "hb.db")}}}
    # get_state_store is process singleton — point it at our db
    hb._reset_for_tests()
    try:
        panel = og.tiktok_connect_panel(_Req(cfg))
        assert panel["personal_rpa"]["bridge_enabled"] is True
        accs = panel["personal_rpa"]["accounts"]
        assert accs and accs[0]["account_id"] == "acc-ph" and accs[0]["device_id"] == "phone-1"
        assert accs[0]["health"] in ("authorized", "reconnecting", "expired", "unknown")
    finally:
        hb._reset_for_tests()


def test_tiktok_panel_p3_form_data_and_device_stats(tmp_path):
    """TK-3 P3：页签表单需要的数据——active_tab / huoke endpoint / 时区候选 / 策略 / 每设备今日·待发。"""
    from src.integrations import tiktok_huoke_bridge as hb
    panel = og.tiktok_connect_panel(_Req({}))
    assert panel["active_tab"] == "official" and panel["personal_rpa"]["huoke"]["endpoint"] == ""
    assert "Asia/Manila" in panel["personal_rpa"]["timezones"] and "probe" in panel["personal_rpa"]["huoke"]["probe_cmd"]
    db = str(tmp_path / "hb3.db")
    cfg = {"tiktok": {"huoke_bridge": {"enabled": True, "state_db_path": db, "dm_daily_cap": 20}}}
    st = hb.TikTokHuokeStateStore(db)
    hb.bind_device({"device_id": "phone-3", "account_id": "acc-3", "timezone": "Asia/Manila", "dm_daily_cap": 5},
                   config=cfg, state=st, now=1_800_000_000.0, registry=SimpleNamespace(upsert=lambda *a, **k: None))
    st.record_inbound("acc-3", "tiktok:user:b1", username="b1", ts=1_800_000_000.0, kind=hb.KIND_DM)
    st.enqueue("acc-3", "tiktok:user:b1", "hi", ctx={"username": "b1"}, kind=hb.KIND_DM)
    hb._reset_for_tests()
    try:
        req = _Req(cfg)
        req.headers = {"host": "192.168.0.118:8780"}
        p = og.tiktok_connect_panel(req)["personal_rpa"]
        assert p["policy"]["dm_daily_cap"] == 20 and p["huoke"]["endpoint"] == "http://192.168.0.118:8780"
        a = p["accounts"][0]
        assert a["dm_daily_cap"] == 5 and a["sent_today_dm"] == 1 and a["queued"] == 1 and a["device_id"] == "phone-3"
    finally:
        hb._reset_for_tests()


def test_tiktok_rpa_i18n_pack_collects_without_conflict():
    from src.web.i18n_packs import collect_all, tiktok_huoke_onboarding as pk
    assert set(pk.ZH) == set(pk.EN) == set(pk.ZH_HANT) and all(v.strip() for v in pk.ZH.values())
    zh, en, extras = collect_all()
    assert zh["obg.tt.tab.personal_rpa"] == "个人号 · 真机" and "unofficial" in en["obg.tt.rpa.intro"].lower()
    assert extras.get("zh_hant", {}).get("obg.tt.rpa.enable_btn") == "開啟真機橋"
    blob = " ".join(pk.ZH.values()) + " ".join(pk.EN.values())
    assert "官方个人号" not in blob and "official personal" not in blob.lower()


def test_tiktok_rpa_tab_enable_bind_and_render(auth_client, app, tmp_path, monkeypatch):
    """接入页：默认关 → 一键开桥（热挂路由）→ 绑定设备（坏时区 / 缺字段 / 成功）→ 页面列出设备与健康 pill。"""
    from src.integrations import tiktok_huoke_bridge as hb
    cm = app.state.config_manager
    cfg = cm.config
    cfg.setdefault("tiktok", {})["huoke_bridge"] = {"enabled": False, "state_db_path": str(tmp_path / "hb_page.db"), "dm_daily_cap": 20}
    hb._reset_for_tests()
    monkeypatch.setattr("src.web.routes.unified_inbox_aggregate._INBOX_ADAPTERS", [])
    try:
        r = auth_client.get("/workspace/onboarding/tiktok")
        assert r.status_code == 200
        html = r.text
        assert 'data-obg-tab="personal_rpa"' in html and 'data-obg-tab="web"' in html and 'id="obg-rpa-enable"' in html
        assert 'data-pane="official" ' in html or 'data-pane="official"' in html
        # 桥关着 → 绑定被拒
        r = auth_client.post("/workspace/onboarding/tiktok/device", data={"device_id": "d1", "account_id": "a1"}, follow_redirects=False)
        assert r.status_code == 303 and "error=rpa:bridge_off" in r.headers["location"]
        # 一键开桥 → 配置为 true + 路由热挂（status 路由可达）
        r = auth_client.post("/workspace/onboarding/tiktok/huoke_bridge", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].endswith("?rpa=on")
        assert hb.bridge_enabled(cm.config) is True
        r = auth_client.get(hb.STATUS_ROUTE)
        assert r.status_code == 200 and r.json()["enabled"] is True
        # 回跳页默认落个人号页签 + flash
        r = auth_client.get("/workspace/onboarding/tiktok?rpa=on")
        assert r.status_code == 200 and 'class="obg-pane on" data-pane="personal_rpa"' in r.text and "真机桥已开启" in r.text
        # 绑定：坏时区 / 缺字段 / 成功
        r = auth_client.post("/workspace/onboarding/tiktok/device",
                             data={"device_id": "phone-9", "account_id": "acc-9", "timezone": "Mars/Olympus"}, follow_redirects=False)
        assert r.status_code == 303 and "error=rpa:bad_timezone" in r.headers["location"]
        r = auth_client.post("/workspace/onboarding/tiktok/device", data={"device_id": "", "account_id": "acc-9"}, follow_redirects=False)
        assert "error=rpa:missing_device_or_account" in r.headers["location"]
        r = auth_client.post("/workspace/onboarding/tiktok/device",
                             data={"device_id": "phone-9", "account_id": "acc-9", "username": "@shop9", "timezone": "Asia/Manila",
                                   "dm_daily_cap": "8"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].endswith("?rpa=bound:acc-9"), r.headers["location"]
        r = auth_client.get("/workspace/onboarding/tiktok?rpa=bound:acc-9")
        assert r.status_code == 200
        html = r.text
        assert "设备已绑定到账号" in html and "<code>acc-9</code>" in html and "phone-9" in html and "@shop9" in html
        assert "真机在线" in html and "今日</span>" not in html and "0/8" in html
        assert 'class="obg-pane on" data-pane="personal_rpa"' in html and 'class="obg-pane on" data-pane="official"' not in html
        # ?tab=web 落网页页签占位
        r = auth_client.get("/workspace/onboarding/tiktok?tab=web")
        assert 'class="obg-pane on" data-pane="web"' in r.text and "阶段 1 未开工" in r.text
    finally:
        hb._reset_for_tests()


def test_howto_and_registry_do_not_claim_official_personal_dm():
    spec = preg.get("tiktok")
    assert spec.implemented is False
    assert "notice_unofficial" in spec.note and "personal_rpa" in spec.note
    e = next(x for x in build_howto_entries() if x["id"] == "howto:supported-platforms")
    blob = e["content"] + e.get("content_en", "") + e.get("title", "")
    assert "个人号没有官方私信接口" in blob or "no official DM API for personal" in blob.lower()
    assert "暂不支持" not in e["content"] or "个人号没有官方" in e["content"]
    assert "official" not in e["content"].lower() or "没有官方" in e["content"]
