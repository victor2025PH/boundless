# -*- coding: utf-8 -*-
"""团队角色分层（supervisor）+ 坐席字符额度 —— store / 路由 / 契约门禁。

钉住的不变量：
- store：supervisor 角色可建；web_users 新增 quota 两列（默认 0/80，负额度→0、
  alert_pct 夹 [50,100]）；assignable_roles / can_manage_target 全矩阵（master 行保护）。
- 路由：users 页 admin 可开；创建/更新/删除/设额度全走同一层级判定
  （admin 不得造/管平级 admin、任何人不得动 master 行）。
- GET /api/users/char-usage（master/admin/supervisor）与
  GET /api/workspace/my-usage（任意登录角色）的响应字段是**并行 agent 的消费契约**，
  字段名漂移 = 契约破坏，本文件先红。
- 两处 _SUPERVISOR_ROLES 副本必须同步含 supervisor（防漂移）。
"""

import pytest
from starlette.testclient import TestClient

from src.utils.web_user_store import (
    PAGE_PERMISSIONS,
    ROLE_ADMIN,
    ROLE_DEFAULT_UI_MODE,
    ROLE_LABELS,
    ROLE_MASTER,
    ROLE_SUPERVISOR,
    WRITE_PERMISSIONS,
    WebUserStore,
    assignable_roles,
    can_manage_target,
)


@pytest.fixture(autouse=True)
def _isolated_agent_char_store():
    """坐席字符账本模块级单例按用例隔离（防串味/防泄漏到后续文件）。"""
    from src.utils.agent_char_usage import reset_agent_char_usage_store
    reset_agent_char_usage_store()
    yield
    reset_agent_char_usage_store()


# ── fixtures（照 conftest.viewer_client 模式：master 建号 → 登出 → 目标号登录）──

def _provision_and_login(c: TestClient, username: str, password: str, role: str):
    c.headers.update({"Authorization": "Bearer test-token-123"})
    c.post("/login", data={"auth_token": "test-token-123"}, follow_redirects=True)
    r = c.post(
        "/users/create",
        data={"username": username, "password": password, "role": role},
        headers={"Accept": "application/json"},
    )
    assert r.json().get("ok") is True, r.text[:200]
    c.get("/logout", follow_redirects=True)
    c.post("/login", data={"username": username, "password": password},
           follow_redirects=False)


@pytest.fixture()
def admin_client(app):
    with TestClient(app, raise_server_exceptions=True) as c:
        _provision_and_login(c, "teamadmin", "admin123456", "admin")
        yield c


@pytest.fixture()
def agent_client(app):
    with TestClient(app, raise_server_exceptions=True) as c:
        _provision_and_login(c, "teamagent", "agent123456", "agent")
        yield c


# ─────────────────────────────────────────────────────────
# store 层
# ─────────────────────────────────────────────────────────

class TestStoreLayer:
    def test_supervisor_constant_and_labels(self):
        assert ROLE_SUPERVISOR == "supervisor"
        assert "supervisor" in ROLE_LABELS
        assert ROLE_SUPERVISOR in ROLE_DEFAULT_UI_MODE

    def test_create_supervisor_and_quota_column_defaults(self, tmp_path):
        store = WebUserStore(tmp_path / "users.db")
        u = store.create_user("sup01", "pass123456", "supervisor")
        assert u and u["role"] == "supervisor"
        # 新列默认：0=不限 / 预警阈值 80%
        assert u["monthly_char_quota"] == 0
        assert u["quota_alert_pct"] == 80
        # list_users / get_user_by_id 均带两列
        rows = store.list_users()
        assert all(
            "monthly_char_quota" in r and "quota_alert_pct" in r for r in rows)
        byid = store.get_user_by_id(u["id"])
        assert byid["monthly_char_quota"] == 0
        assert byid["quota_alert_pct"] == 80

    def test_update_user_quota_writes_and_clamps(self, tmp_path):
        store = WebUserStore(tmp_path / "users.db")
        u = store.create_user("ag01", "pass123456", "agent")
        assert store.update_user(
            u["id"], monthly_char_quota=5000, quota_alert_pct=90) is True
        row = store.get_user("ag01")
        assert row["monthly_char_quota"] == 5000
        assert row["quota_alert_pct"] == 90
        # 负额度 → 0；alert_pct 下夹 50
        store.update_user(u["id"], monthly_char_quota=-3, quota_alert_pct=10)
        row = store.get_user("ag01")
        assert row["monthly_char_quota"] == 0
        assert row["quota_alert_pct"] == 50
        # alert_pct 上夹 100；脏输入不抛
        store.update_user(u["id"], quota_alert_pct=500)
        assert store.get_user("ag01")["quota_alert_pct"] == 100
        store.update_user(u["id"], monthly_char_quota="garbage")
        assert store.get_user("ag01")["monthly_char_quota"] == 0

    def test_assignable_roles_matrix(self):
        assert assignable_roles("master") == ["admin", "supervisor", "agent", "viewer"]
        assert assignable_roles("admin") == ["supervisor", "agent", "viewer"]
        for actor in ("supervisor", "agent", "viewer", "", "garbage"):
            assert assignable_roles(actor) == []

    def test_can_manage_target_matrix(self):
        # master 行任何人不可管（含 master 自己）
        for actor in ("master", "admin", "supervisor", "agent", "viewer", ""):
            assert can_manage_target(actor, "master") is False
        # master 可管其余任何角色
        for tgt in ("admin", "supervisor", "agent", "viewer"):
            assert can_manage_target("master", tgt) is True
        # admin 只可管 supervisor/agent/viewer（不得动平级 admin）
        assert can_manage_target("admin", "admin") is False
        for tgt in ("supervisor", "agent", "viewer"):
            assert can_manage_target("admin", tgt) is True
        # 其余角色无用户管理能力
        for actor in ("supervisor", "agent", "viewer"):
            for tgt in ("admin", "supervisor", "agent", "viewer"):
                assert can_manage_target(actor, tgt) is False

    def test_permission_tables_updated(self):
        assert PAGE_PERMISSIONS["users"] == {ROLE_MASTER, ROLE_ADMIN}
        assert WRITE_PERMISSIONS["manage_users"] == {ROLE_MASTER, ROLE_ADMIN}
        for key in ("workspace", "dash", "cases", "care", "analytics",
                    "audit", "crisis_audit", "episodic", "help"):
            assert ROLE_SUPERVISOR in PAGE_PERMISSIONS[key], key
        # 敏感页不放 supervisor
        for key in ("settings", "import", "export", "tpl", "strategies"):
            assert ROLE_SUPERVISOR not in PAGE_PERMISSIONS[key], key


# ─────────────────────────────────────────────────────────
# 路由层：分层守卫
# ─────────────────────────────────────────────────────────

class TestRouteTiering:
    def test_admin_opens_users_page(self, admin_client):
        r = admin_client.get("/users")
        assert r.status_code == 200
        # 分层下拉已注入（admin 可发 supervisor）+ 额度弹窗在页上
        assert "supervisor" in r.text
        assert "quota-modal" in r.text

    def test_admin_creates_agent_ok_but_admin_denied(self, admin_client):
        r = admin_client.post(
            "/users/create",
            data={"username": "madeagent", "password": "agent123456",
                  "role": "agent"},
            headers={"Accept": "application/json"},
        )
        assert r.status_code == 200 and r.json().get("ok") is True
        assert r.json().get("role") == "agent"
        # admin 不得造平级 admin
        r2 = admin_client.post(
            "/users/create",
            data={"username": "evildup", "password": "admin123456",
                  "role": "admin"},
            headers={"Accept": "application/json"},
        )
        assert r2.status_code == 403
        assert r2.json().get("detail")

    def test_admin_cannot_update_or_delete_peer_admin(self, admin_client, config_dir):
        store = WebUserStore(config_dir / "web_users.db")
        peer = store.create_user("peeradmin", "pass123456", "admin")
        assert peer
        r = admin_client.post(
            f"/users/update/{peer['id']}", data={"enabled": "0"},
            headers={"Accept": "application/json"})
        assert r.status_code == 403
        r = admin_client.post(
            f"/users/delete/{peer['id']}",
            headers={"Accept": "application/json"})
        assert r.status_code == 403
        # 库里没被动过
        assert store.get_user("peeradmin")["enabled"] == 1

    def test_admin_cannot_touch_master_row(self, admin_client, config_dir):
        store = WebUserStore(config_dir / "web_users.db")
        boss = store.create_user("bigboss", "boss123456", "master")
        assert boss
        r = admin_client.post(
            f"/users/update/{boss['id']}", data={"role": "viewer"},
            headers={"Accept": "application/json"})
        assert r.status_code == 403
        r = admin_client.post(
            f"/users/delete/{boss['id']}",
            headers={"Accept": "application/json"})
        assert r.status_code == 403
        assert store.get_user("bigboss")["role"] == "master"

    def test_master_cannot_change_master_row_role(self, auth_client, config_dir):
        store = WebUserStore(config_dir / "web_users.db")
        boss = store.get_user("admin")   # auth_client 的 master 行
        assert boss and boss["role"] == "master"
        r = auth_client.post(
            f"/users/update/{boss['id']}", data={"role": "viewer"},
            headers={"Accept": "application/json"})
        assert r.status_code == 403
        assert store.get_user("admin")["role"] == "master"

    def test_update_nonexistent_user_404(self, auth_client):
        r = auth_client.post(
            "/users/update/99999", data={"enabled": "0"},
            headers={"Accept": "application/json"})
        assert r.status_code == 404

    def test_master_still_manages_lower_roles(self, auth_client):
        """回归：master 建/改/删低层角色不受新守卫影响。"""
        r = auth_client.post(
            "/users/create",
            data={"username": "supx", "password": "sup1234567",
                  "role": "supervisor"},
            headers={"Accept": "application/json"})
        assert r.json().get("ok") is True, r.text[:200]
        uid = r.json()["id"]
        r = auth_client.post(
            f"/users/update/{uid}", data={"role": "viewer"},
            headers={"Accept": "application/json"})
        assert r.status_code == 200 and r.json().get("role") == "viewer"
        r = auth_client.post(
            f"/users/delete/{uid}", headers={"Accept": "application/json"})
        assert r.status_code == 200 and r.json().get("ok") is True


# ─────────────────────────────────────────────────────────
# 路由层：额度设置
# ─────────────────────────────────────────────────────────

class TestQuotaRoute:
    def test_admin_sets_quota_on_agent(self, admin_client, config_dir):
        store = WebUserStore(config_dir / "web_users.db")
        ag = store.create_user("quotaagent", "pass123456", "agent")
        r = admin_client.post(
            f"/users/quota/{ag['id']}",
            data={"monthly_quota": "50000"},
            headers={"Accept": "application/json"})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True and d["quota"] == 50000
        assert d["alert_pct"] == 80
        assert store.get_user("quotaagent")["monthly_char_quota"] == 50000

    def test_admin_sets_quota_on_admin_403(self, admin_client, config_dir):
        store = WebUserStore(config_dir / "web_users.db")
        peer = store.create_user("peeradmin2", "pass123456", "admin")
        r = admin_client.post(
            f"/users/quota/{peer['id']}",
            data={"monthly_quota": "1000"},
            headers={"Accept": "application/json"})
        assert r.status_code == 403
        assert store.get_user("peeradmin2")["monthly_char_quota"] == 0

    def test_quota_negative_clamped_and_alert_pct(self, auth_client, config_dir):
        store = WebUserStore(config_dir / "web_users.db")
        ag = store.create_user("quotaagent2", "pass123456", "agent")
        r = auth_client.post(
            f"/users/quota/{ag['id']}",
            data={"monthly_quota": "-500", "alert_pct": "95"},
            headers={"Accept": "application/json"})
        assert r.status_code == 200
        d = r.json()
        assert d["quota"] == 0 and d["alert_pct"] == 95


# ─────────────────────────────────────────────────────────
# 用量 API 契约（并行 agent 消费面——字段名不可漂移）
# ─────────────────────────────────────────────────────────

class TestUsageApis:
    _AGENT_ROW_KEYS = {
        "id", "username", "display_name", "role", "enabled", "quota",
        "alert_pct", "used_month", "used_today", "by_category", "status",
    }

    def test_char_usage_admin_ok_zero_usage_users_present(self, admin_client):
        r = admin_client.get(
            "/api/users/char-usage", headers={"Accept": "application/json"})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        # 测试环境未开 usage.agent_chars —— enabled 必须仍有结构（False）
        assert d["enabled"] is False
        assert isinstance(d["month"], str) and len(d["month"]) == 7
        lic = d["license"]
        for k in ("available", "included", "used", "remaining",
                  "topup_chars", "enforce", "source"):
            assert k in lic, k
        totals = d["totals"]
        for k in ("month_total", "today_total", "by_category"):
            assert k in totals, k
        agents = d["agents"]
        unames = {a["username"] for a in agents}
        assert "teamadmin" in unames  # 零用量用户也在列表
        row = next(a for a in agents if a["username"] == "teamadmin")
        assert self._AGENT_ROW_KEYS <= set(row)
        assert row["used_month"] == 0
        assert row["status"]["level"] == "unlimited"

    def test_char_usage_sorted_desc_and_reads_ledger(self, admin_client):
        from src.utils.agent_char_usage import (
            AgentCharUsageStore,
            configure_agent_char_usage,
        )
        s = AgentCharUsageStore(":memory:")
        s.record("teamadmin", "tts", 123)
        configure_agent_char_usage(store=s)
        r = admin_client.get(
            "/api/users/char-usage", headers={"Accept": "application/json"})
        d = r.json()
        assert d["totals"]["month_total"] == 123
        agents = d["agents"]
        assert agents[0]["username"] == "teamadmin"
        assert agents[0]["used_month"] == 123
        assert agents[0]["by_category"] == {"tts": 123}

    def test_char_usage_agent_403(self, agent_client):
        r = agent_client.get(
            "/api/users/char-usage", headers={"Accept": "application/json"})
        assert r.status_code == 403

    def test_my_usage_agent_ok(self, agent_client):
        r = agent_client.get(
            "/api/workspace/my-usage", headers={"Accept": "application/json"})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert d["username"] == "teamagent"
        assert d["quota"] == 0
        assert d["status"]["level"] == "unlimited"
        for k in ("enabled", "alert_pct", "month_total", "today_total",
                  "by_category", "month"):
            assert k in d, k


# ─────────────────────────────────────────────────────────
# 主管角色集两处副本同步（防漂移）
# ─────────────────────────────────────────────────────────

def test_supervisor_roles_constants_synced():
    from src.web.routes.drafts_routes import _SUPERVISOR_ROLES as drafts_roles
    from src.web.routes.unified_inbox_auth import _SUPERVISOR_ROLES as inbox_roles
    assert "supervisor" in inbox_roles
    assert "supervisor" in drafts_roles
    assert {"master", "admin"} <= inbox_roles
    assert {"master", "admin"} <= drafts_roles


# ─────────────────────────────────────────────────────────
# 坐席 Telegram 通知号绑定（P2 2026-08-18：目标达成定向副本的收件地址）
# ─────────────────────────────────────────────────────────

class TestNotifyBindingStore:
    def test_column_default_and_listing(self, tmp_path):
        store = WebUserStore(tmp_path / "users.db")
        u = store.create_user("nb01", "pass123456", "agent")
        assert u["notify_tg_chat_id"] == ""
        assert all("notify_tg_chat_id" in r for r in store.list_users())
        assert "notify_tg_chat_id" in store.get_user_by_id(u["id"])

    def test_update_sanitizes(self, tmp_path):
        store = WebUserStore(tmp_path / "users.db")
        u = store.create_user("nb02", "pass123456", "agent")
        # 正数 / 负数（群）/ 前后空白 → 接受并归一
        for raw, want in (("5433982810", "5433982810"),
                          ("-100123", "-100123"),
                          ("  42 ", "42")):
            store.update_user(u["id"], notify_tg_chat_id=raw)
            assert store.get_user("nb02")["notify_tg_chat_id"] == want
        # 脏值（字母/@名/超长/中置负号）→ 忽略不写（保留旧值）
        for bad in ("@boss", "12a3", "1" * 21, "12-3"):
            store.update_user(u["id"], notify_tg_chat_id=bad)
            assert store.get_user("nb02")["notify_tg_chat_id"] == "42", bad
        # 空串 = 解绑
        store.update_user(u["id"], notify_tg_chat_id="")
        assert store.get_user("nb02")["notify_tg_chat_id"] == ""


class TestNotifyBindingRoute:
    def test_admin_binds_agent_ok(self, admin_client, config_dir):
        store = WebUserStore(config_dir / "web_users.db")
        ag = store.create_user("nbagent", "pass123456", "agent")
        r = admin_client.post(
            f"/users/notify-binding/{ag['id']}",
            data={"tg_chat_id": "5433982810"},
            headers={"Accept": "application/json"})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "tg_chat_id": "5433982810"}
        assert store.get_user("nbagent")["notify_tg_chat_id"] == "5433982810"
        # 空串保存 = 解绑
        r2 = admin_client.post(
            f"/users/notify-binding/{ag['id']}",
            data={"tg_chat_id": ""},
            headers={"Accept": "application/json"})
        assert r2.status_code == 200 and r2.json()["tg_chat_id"] == ""

    def test_bad_chat_id_400(self, admin_client, config_dir):
        store = WebUserStore(config_dir / "web_users.db")
        ag = store.create_user("nbagent2", "pass123456", "agent")
        r = admin_client.post(
            f"/users/notify-binding/{ag['id']}",
            data={"tg_chat_id": "@boss"},
            headers={"Accept": "application/json"})
        assert r.status_code == 400
        assert store.get_user("nbagent2")["notify_tg_chat_id"] == ""

    def test_admin_cannot_bind_peer_admin(self, admin_client, config_dir):
        store = WebUserStore(config_dir / "web_users.db")
        peer = store.create_user("nbpeeradmin", "pass123456", "admin")
        r = admin_client.post(
            f"/users/notify-binding/{peer['id']}",
            data={"tg_chat_id": "1"},
            headers={"Accept": "application/json"})
        assert r.status_code == 403

    def test_agent_role_denied(self, agent_client, config_dir):
        store = WebUserStore(config_dir / "web_users.db")
        ag = store.create_user("nbagent3", "pass123456", "agent")
        r = agent_client.post(
            f"/users/notify-binding/{ag['id']}",
            data={"tg_chat_id": "7"},
            headers={"Accept": "application/json"},
            follow_redirects=False)
        # require_role 对无权角色是重定向（HTML 语义）而非 403——跟随后会拿到
        # 200 落地页；真正的不变量是绑定绝不落库
        assert r.status_code in (302, 303, 307, 403)
        assert store.get_user("nbagent3")["notify_tg_chat_id"] == ""


# ─────────────────────────────────────────────────────────
# 坐席自助绑定 + 测试推送（P3 2026-08-18：/api/workspace/my-notify-binding*）
# api_auth choke point：agent 白名单放行 /api/workspace 前缀——坐席自己就能绑，
# 不再卡在「找管理员代绑」。测试推送借告警渠道 bot 真发（这里全程 mock HTTP）。
# ─────────────────────────────────────────────────────────

class TestSelfNotifyBinding:
    def test_agent_self_bind_get_roundtrip(self, agent_client, config_dir):
        store = WebUserStore(config_dir / "web_users.db")
        r0 = agent_client.get(
            "/api/workspace/my-notify-binding",
            headers={"Accept": "application/json"})
        assert r0.status_code == 200
        d0 = r0.json()
        assert d0["ok"] is True and d0["username"] == "teamagent"
        assert d0["bound"] is False and d0["chat_tail"] == ""
        r1 = agent_client.post(
            "/api/workspace/my-notify-binding",
            data={"tg_chat_id": "5433982810"},
            headers={"Accept": "application/json"})
        assert r1.status_code == 200 and r1.json()["bound"] is True
        assert store.get_user("teamagent")["notify_tg_chat_id"] == "5433982810"
        d2 = agent_client.get(
            "/api/workspace/my-notify-binding",
            headers={"Accept": "application/json"}).json()
        # 响应只回尾 4 位（页面显示够用，全量号不外传）
        assert d2["bound"] is True and d2["chat_tail"] == "2810"
        # 空串=解绑
        r3 = agent_client.post(
            "/api/workspace/my-notify-binding", data={"tg_chat_id": ""},
            headers={"Accept": "application/json"})
        assert r3.status_code == 200 and r3.json()["bound"] is False

    def test_self_bind_bad_id_400(self, agent_client, config_dir):
        store = WebUserStore(config_dir / "web_users.db")
        r = agent_client.post(
            "/api/workspace/my-notify-binding",
            data={"tg_chat_id": "@boss"},
            headers={"Accept": "application/json"})
        assert r.status_code == 400
        assert store.get_user("teamagent")["notify_tg_chat_id"] == ""

    def test_test_push_flows(self, agent_client, config_dir, monkeypatch):
        from src.inbox.webhook_notifier import WebhookNotifier
        from src.integrations import notify_webhooks_store as nws
        store = WebUserStore(config_dir / "web_users.db")
        # ① 未绑定 → 400
        monkeypatch.setattr(
            nws, "effective_webhooks",
            lambda cfg: [{"name": "tg-ops", "format": "telegram",
                          "token": "T", "target": "111", "enabled": True,
                          "events": ["goal_complete"]}])
        r0 = agent_client.post(
            "/api/workspace/my-notify-binding/test",
            headers={"Accept": "application/json"})
        assert r0.status_code == 400
        # ② 绑定 + mock HTTP → 200 且真调了 sendMessage 端点、body 带 chat_id
        agent_client.post(
            "/api/workspace/my-notify-binding",
            data={"tg_chat_id": "424242"},
            headers={"Accept": "application/json"})
        calls = []
        monkeypatch.setattr(
            WebhookNotifier, "_http_post",
            staticmethod(lambda url, body, headers=None:
                         calls.append((url, bytes(body)))))
        r1 = agent_client.post(
            "/api/workspace/my-notify-binding/test",
            headers={"Accept": "application/json"})
        assert r1.status_code == 200 and r1.json()["ok"] is True
        assert len(calls) == 1
        assert "api.telegram.org/botT/sendMessage" in calls[0][0]
        assert b"424242" in calls[0][1]
        # ③ 30s 防抖 → 429（同号立刻再测）
        r2 = agent_client.post(
            "/api/workspace/my-notify-binding/test",
            headers={"Accept": "application/json"})
        assert r2.status_code == 429
        assert store.get_user("teamagent")["notify_tg_chat_id"] == "424242"

    def test_test_push_no_channel_503(self, agent_client, monkeypatch):
        from src.integrations import notify_webhooks_store as nws
        monkeypatch.setattr(nws, "effective_webhooks", lambda cfg: [])
        agent_client.post(
            "/api/workspace/my-notify-binding",
            data={"tg_chat_id": "77"},
            headers={"Accept": "application/json"})
        r = agent_client.post(
            "/api/workspace/my-notify-binding/test",
            headers={"Accept": "application/json"})
        # 告警渠道没接通：诚实 503 指路接通面板，绝不装成功
        assert r.status_code == 503

    def test_admin_tests_agent_binding_with_guard(
            self, admin_client, config_dir, monkeypatch):
        from src.inbox.webhook_notifier import WebhookNotifier
        from src.integrations import notify_webhooks_store as nws
        store = WebUserStore(config_dir / "web_users.db")
        ag = store.create_user("nbagent9", "pass123456", "agent")
        store.update_user(ag["id"], notify_tg_chat_id="909090")
        monkeypatch.setattr(
            nws, "effective_webhooks",
            lambda cfg: [{"name": "tg-ops", "format": "telegram",
                          "token": "T", "target": "111", "enabled": True,
                          "events": ["goal_complete"]}])
        calls = []
        monkeypatch.setattr(
            WebhookNotifier, "_http_post",
            staticmethod(lambda url, body, headers=None:
                         calls.append(url)))
        r = admin_client.post(
            "/api/workspace/my-notify-binding/test",
            data={"user_id": str(ag["id"])},
            headers={"Accept": "application/json"})
        assert r.status_code == 200 and len(calls) == 1
        # 层级守卫：admin 不得代测平级 admin
        peer = store.create_user("nbpeeradmin2", "pass123456", "admin")
        store.update_user(peer["id"], notify_tg_chat_id="808080")
        r2 = admin_client.post(
            "/api/workspace/my-notify-binding/test",
            data={"user_id": str(peer["id"])},
            headers={"Accept": "application/json"})
        assert r2.status_code == 403
