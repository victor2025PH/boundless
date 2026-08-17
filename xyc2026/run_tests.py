# 功能与 API 集成测试（临时 SQLite 文件，无需真实 .env）
# 运行：python run_tests.py
import os
import asyncio
import tempfile

# 必须在导入 config/database 之前设置；强制覆盖 .env
_test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_test_db.close()
_db_path = os.path.abspath(_test_db.name).replace("\\", "/")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db_path}"
os.environ["BOT_TOKEN"] = "test_bot_token_for_tests"
os.environ["API_BASE_URL"] = "http://127.0.0.1:8001"

from fastapi.testclient import TestClient


async def _ensure_tables():
    """按当前 models 建表。"""
    from database import engine, Base
    import models  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def test_imports():
    """确保各模块可导入且无语法错误。"""
    import database
    import models
    import services
    import api.main
    import bot.handlers.start
    import bot.handlers.check
    import bot.handlers.mycredit
    import bot.handlers.alerts
    import bot.format_result
    import bot.i18n
    assert database.engine is not None
    assert hasattr(services, "check_by_tg_id")
    assert hasattr(services, "alert_subscribe")
    assert hasattr(services, "blacklist_add")


def test_api_health():
    """GET /health 返回 200 且 status ok。"""
    from api.main import app
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json().get("status") == "ok"


def test_api_check_and_mycredit():
    """GET /v1/check 与 /v1/mycredit 正常返回且含必要字段。"""
    from api.main import app
    client = TestClient(app)
    r = client.get("/v1/check", params={"user_id": 999001})
    assert r.status_code == 200
    j = r.json()
    assert "risk" in j and "score" in j and "tg_id" in j
    assert j["tg_id"] == 999001
    assert "timeline" in j
    assert isinstance(j.get("timeline"), list)
    # 阶段 C：四维度与 high_risk_group_count
    assert "risk_dimensions" in j
    dims = j.get("risk_dimensions") or {}
    assert "identity" in dims and "funds" in dims and "community" in dims and "association" in dims
    assert dims.get("funds") == "unverified"
    assert "high_risk_group_count" in j

    r2 = client.get("/v1/mycredit", params={"tg_id": 999001})
    assert r2.status_code == 200
    j2 = r2.json()
    assert "recent_7d_query_count" in j2 and "last_queried_at" in j2
    assert "timeline" in j2


def test_api_report_and_my_feedback():
    """POST /v1/report 成功；GET /v1/my-feedback 返回列表。"""
    from api.main import app
    client = TestClient(app)
    r = client.post(
        "/v1/report",
        json={
            "reporter_tg_id": 888001,
            "target_tg_id": 999001,
            "reason": "test reason",
        },
    )
    assert r.status_code == 200
    assert r.json().get("status") == "received"

    r2 = client.get("/v1/my-feedback", params={"tg_id": 888001})
    assert r2.status_code == 200
    assert "items" in r2.json()
    assert len(r2.json()["items"]) >= 1


def test_api_alert_subscribe_and_list():
    """POST /v1/alert/subscribe 与 GET /v1/alert/subscriptions 正常。"""
    from api.main import app
    client = TestClient(app)
    r = client.post(
        "/v1/alert/subscribe",
        params={"tg_id": 777001, "target_tg_id": 999001, "lang": "zh"},
    )
    assert r.status_code == 201
    assert r.json().get("target_tg_id") == 999001

    r2 = client.get("/v1/alert/subscriptions", params={"tg_id": 777001})
    assert r2.status_code == 200
    assert len(r2.json()["items"]) >= 1

    r3 = client.delete("/v1/alert/subscribe", params={"tg_id": 777001, "target_tg_id": 999001})
    assert r3.status_code == 204

    # 订阅类型 online_offline 与列表含 alert_type
    r4 = client.post(
        "/v1/alert/subscribe",
        params={"tg_id": 777002, "target_tg_id": 999001, "type": "online_offline", "lang": "zh"},
    )
    assert r4.status_code == 201
    r5 = client.get("/v1/alert/subscriptions", params={"tg_id": 777002})
    assert r5.status_code == 200
    items = r5.json().get("items") or []
    assert any(it.get("alert_type") == "online_offline" for it in items)
    client.delete("/v1/alert/subscribe", params={"tg_id": 777002, "target_tg_id": 999001, "type": "online_offline"})


def test_api_monitor_event():
    """POST /v1/monitor/event 需 Admin Key；上报后订阅了 online_offline 的用户应入队。"""
    from api.main import app
    from config import settings
    client = TestClient(app)
    # 先订阅上下线
    client.post("/v1/alert/subscribe", params={"tg_id": 777003, "target_tg_id": 999001, "type": "online_offline"})
    headers = {}
    if getattr(settings, "api_internal_key", None):
        headers["Authorization"] = f"Bearer {settings.api_internal_key}"
    r = client.post(
        "/v1/monitor/event",
        json={"target_tg_id": 999001, "event_type": "online", "reporter_tg_id": 777000},
        headers=headers,
    )
    assert r.status_code == 200
    j = r.json()
    assert j.get("status") in ("ok", "dedupe")
    assert j.get("enqueued") is not None
    client.delete("/v1/alert/subscribe", params={"tg_id": 777003, "target_tg_id": 999001, "type": "online_offline"})


def test_api_share_create_and_invite():
    """POST /v1/share/create、/v1/invite/create 返回 link。"""
    from api.main import app
    client = TestClient(app)
    r = client.post("/v1/share/create", params={"tg_id": 999001})
    assert r.status_code == 200
    assert "link" in r.json() and "share_" in r.json().get("link", "")

    r2 = client.post("/v1/invite/create", params={"querier_tg_id": 888001})
    assert r2.status_code == 200
    assert "link" in r2.json() and "inv_" in r2.json().get("link", "")


def test_api_my_recent_queries():
    """GET /v1/my-recent-queries 返回 items。"""
    from api.main import app
    client = TestClient(app)
    r = client.get("/v1/my-recent-queries", params={"tg_id": 999002})
    assert r.status_code == 200
    assert "items" in r.json()


def test_api_report_register_and_verify():
    """阶段 A：POST /v2/report/register 与 GET /v2/report/verify 验真流程。"""
    from api.main import app
    client = TestClient(app)
    r = client.post(
        "/v2/report/register",
        json={"target_tg_id": 999001, "content_hash": "a" * 64},
    )
    assert r.status_code == 200
    j = r.json()
    assert "report_hash" in j
    report_hash = j["report_hash"]
    assert report_hash.startswith("TC-") and len(report_hash) == 9  # TC- + 6 hex
    r2 = client.get("/v2/report/verify", params={"hash": report_hash})
    assert r2.status_code == 200
    v = r2.json()
    assert v.get("valid") is True
    assert "created_at" in v
    r3 = client.get("/v2/report/verify", params={"hash": "TC-INVALID"})
    assert r3.status_code == 200
    assert r3.json().get("valid") is False


def test_api_privacy_and_check_masking():
    """阶段 B：GET/PATCH 隐私设置；check 结果按被查人隐私遮蔽。"""
    from api.main import app
    client = TestClient(app)
    tg_id = 999009
    r = client.get("/v1/user/privacy", params={"tg_id": tg_id})
    assert r.status_code == 200
    j = r.json()
    assert j.get("show_register_year") is True
    assert j.get("show_premium") is True
    assert j.get("show_tx_30d") is True
    r2 = client.patch("/v1/user/privacy", json={"tg_id": tg_id, "show_register_year": False})
    assert r2.status_code == 200
    assert r2.json().get("show_register_year") is False
    r3 = client.get("/v1/check", params={"user_id": tg_id})
    assert r3.status_code == 200
    check_j = r3.json()
    assert check_j.get("tg_id") == tg_id
    assert check_j.get("first_seen") is None


def test_format_result_has_required_sections():
    """format_check_result 包含用途、下一步、免责等。"""
    from bot.format_result import format_check_result
    data = {
        "risk": "medium",
        "score": 50,
        "tips": [],
        "blacklist": False,
        "tg_id": 123,
        "username": "test",
        "verdict": "verdict_medium",
        "tags": [],
    }
    text = format_check_result(data, "zh")
    assert "用途" in text or "对质" in text
    assert "下一步" in text
    assert "仅供参考" in text or "谨慎" in text
    # 阶段 A：多源标识（中文：官方/众包/链上）
    assert "官方" in text or "众包" in text or "链上" in text
    # 阶段 C：四维度（有 risk_dimensions 时展示）
    data["risk_dimensions"] = {"identity": "medium", "funds": "unverified", "community": "high", "association": "low"}
    text2 = format_check_result(data, "zh")
    assert "风险维度" in text2 or "Risk dimensions" in text2
    assert "身份稳定性" in text2 or "Identity stability" in text2
    assert "未验证" in text2 or "Unverified" in text2


def test_i18n_keys():
    """关键 i18n key 存在且非空。"""
    from bot.i18n import t, SUPPORTED_LOCALES
    keys = [
        "btn_alert_subscribe", "msg_alert_subscribed", "alert_push_new_report",
        "help_section_dont_trust_me_title", "menu_hint", "alert_my_subscriptions_empty_invite_hint", "result_alert_tip",
        "alert_push_online", "alert_push_offline", "btn_subscribe_online_offline", "msg_alert_subscribed_online",
        "alert_type_risk", "alert_type_online", "msg_status_reported",
        "report_footer_verify", "verify_valid", "verify_invalid", "verify_usage", "verify_enter_prompt", "btn_verify_report",
        "source_official", "source_crowd", "source_network", "source_on_chain",
        "btn_privacy_settings", "privacy_title", "privacy_intro", "privacy_option_register_year", "privacy_value_on", "privacy_value_off",
        "section_risk_dimensions", "dim_identity", "dim_funds", "dim_level_unverified", "tag_same_wallet_risk",
    ]
    for k in keys:
        for lang in SUPPORTED_LOCALES:
            v = t(k, lang=lang)
            assert v and isinstance(v, str), f"missing or empty i18n {k} for {lang}"


def test_admin_stats_feedback_pending():
    """admin/stats 返回 feedback_pending 字段。"""
    from api.main import app
    client = TestClient(app)
    r = client.get("/v1/admin/stats")
    assert r.status_code == 200
    j = r.json()
    assert "feedback_pending" in j
    assert isinstance(j["feedback_pending"], int)
    assert "reports_pending" in j


def test_admin_reports_list_and_filter():
    """admin/reports 列表、status 筛选、reporter_tg_id 筛选。"""
    from api.main import app
    client = TestClient(app)
    r = client.get("/v1/admin/reports")
    assert r.status_code == 200
    items = r.json().get("items", [])
    assert isinstance(items, list)
    assert len(items) >= 1
    first = items[0]
    assert "id" in first and "reporter_tg_id" in first and "status" in first

    r2 = client.get("/v1/admin/reports", params={"status": "pending"})
    assert r2.status_code == 200
    for it in r2.json().get("items", []):
        assert it["status"] == "pending"

    reporter_id = items[0]["reporter_tg_id"]
    r3 = client.get("/v1/admin/reports", params={"reporter_tg_id": reporter_id})
    assert r3.status_code == 200
    for it in r3.json().get("items", []):
        assert it["reporter_tg_id"] == reporter_id


def test_admin_report_patch_and_batch():
    """admin/reports PATCH 单条 + POST batch 批量。"""
    from api.main import app
    client = TestClient(app)
    client.post("/v1/report", json={"reporter_tg_id": 880001, "target_tg_id": 990001, "reason": "batch test 1"})
    client.post("/v1/report", json={"reporter_tg_id": 880001, "target_tg_id": 990002, "reason": "batch test 2"})
    client.post("/v1/report", json={"reporter_tg_id": 880001, "target_tg_id": 990003, "reason": "batch test 3"})

    r = client.get("/v1/admin/reports", params={"status": "pending", "limit": 10})
    pending = r.json().get("items", [])
    assert len(pending) >= 2

    first_id = pending[0]["id"]
    r2 = client.patch(f"/v1/admin/reports/{first_id}", json={"status": "verified"})
    assert r2.status_code == 200
    assert r2.json().get("ok") is True

    batch_ids = [it["id"] for it in pending[1:3]]
    r3 = client.post("/v1/admin/reports/batch", json={"ids": batch_ids, "status": "rejected"})
    assert r3.status_code == 200
    j3 = r3.json()
    assert j3.get("ok") is True
    assert j3.get("updated") >= 1


def test_admin_feedback_list():
    """admin/feedback 列表。"""
    from api.main import app
    client = TestClient(app)
    r = client.get("/v1/admin/feedback")
    assert r.status_code == 200
    assert "items" in r.json()


def test_admin_blacklist():
    """admin/blacklist POST。"""
    from api.main import app
    client = TestClient(app)
    r = client.post("/v1/admin/blacklist", json={"tg_id": 990099, "reason": "test bl", "source": "admin"})
    assert r.status_code == 200
    j = r.json()
    assert j.get("ok") is True
    assert j.get("added") is True
    r2 = client.post("/v1/admin/blacklist", json={"tg_id": 990099})
    assert r2.status_code == 200
    assert r2.json().get("added") is False


def test_admin_reporters():
    """admin/reporters 列表含 verified_count。"""
    from api.main import app
    client = TestClient(app)
    r = client.get("/v1/admin/reporters")
    assert r.status_code == 200
    items = r.json().get("items", [])
    assert isinstance(items, list)
    if items:
        assert "verified_count" in items[0]
        assert "report_count" in items[0]


def test_admin_transactions():
    """admin/transactions 列表。"""
    from api.main import app
    client = TestClient(app)
    r = client.get("/v1/admin/transactions")
    assert r.status_code == 200
    assert "items" in r.json()


def test_admin_risk_trend():
    """admin/stats/risk_trend 返回 items。"""
    from api.main import app
    client = TestClient(app)
    r = client.get("/v1/admin/stats/risk_trend", params={"days": 7})
    assert r.status_code == 200
    assert "items" in r.json()


def run_all():
    print("0. ensure_tables (temp DB) ...")
    asyncio.run(_ensure_tables())
    print("1. test_imports ...")
    test_imports()
    print("2. test_api_health ...")
    test_api_health()
    print("3. test_api_check_and_mycredit ...")
    test_api_check_and_mycredit()
    print("4. test_api_report_and_my_feedback ...")
    test_api_report_and_my_feedback()
    print("5. test_api_alert_subscribe_and_list ...")
    test_api_alert_subscribe_and_list()
    print("5b. test_api_monitor_event ...")
    test_api_monitor_event()
    print("6. test_api_share_create_and_invite ...")
    test_api_share_create_and_invite()
    print("7. test_api_my_recent_queries ...")
    test_api_my_recent_queries()
    print("7b. test_api_report_register_and_verify ...")
    test_api_report_register_and_verify()
    print("7c. test_api_privacy_and_check_masking ...")
    test_api_privacy_and_check_masking()
    print("8. test_format_result_has_required_sections ...")
    test_format_result_has_required_sections()
    print("9. test_i18n_keys ...")
    test_i18n_keys()
    print("--- Admin API Tests ---")
    print("10a. test_admin_stats_feedback_pending ...")
    test_admin_stats_feedback_pending()
    print("10b. test_admin_reports_list_and_filter ...")
    test_admin_reports_list_and_filter()
    print("10c. test_admin_report_patch_and_batch ...")
    test_admin_report_patch_and_batch()
    print("10d. test_admin_feedback_list ...")
    test_admin_feedback_list()
    print("10e. test_admin_blacklist ...")
    test_admin_blacklist()
    print("10f. test_admin_reporters ...")
    test_admin_reporters()
    print("10g. test_admin_transactions ...")
    test_admin_transactions()
    print("10h. test_admin_risk_trend ...")
    test_admin_risk_trend()
    print("All tests passed.")


if __name__ == "__main__":
    try:
        run_all()
    finally:
        try:
            os.unlink(_test_db.name)
        except Exception:
            pass
