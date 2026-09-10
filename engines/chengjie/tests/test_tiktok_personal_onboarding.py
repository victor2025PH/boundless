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


def test_howto_and_registry_do_not_claim_official_personal_dm():
    spec = preg.get("tiktok")
    assert spec.implemented is False
    assert "notice_unofficial" in spec.note and "personal_rpa" in spec.note
    e = next(x for x in build_howto_entries() if x["id"] == "howto:supported-platforms")
    blob = e["content"] + e.get("content_en", "") + e.get("title", "")
    assert "个人号没有官方私信接口" in blob or "no official DM API for personal" in blob.lower()
    assert "暂不支持" not in e["content"] or "个人号没有官方" in e["content"]
    assert "official" not in e["content"].lower() or "没有官方" in e["content"]
