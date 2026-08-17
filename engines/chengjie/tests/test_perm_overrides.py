# -*- coding: utf-8 -*-
"""L3 按人权限覆写（PERM_REGISTRY / resolve / set_user_perms / 路由）+ char-usage 二批扩展。

钉住的不变量：
- 注册表 v1 只收录四个**有真实执法点**的键（translate/voice/send 路由，另一条线接线），
  dict 定义序即 API/编辑器展示序；
- ``resolve_user_perm`` 单点判定：master 恒 True（防自锁）/ 未注册键 fail-open /
  deny > allow > 角色默认 / 行缺失、坏 JSON、空 username、无 store 一律回落角色默认，绝不抛；
- ``set_user_perms`` 整单校验：未注册键 / allow∩deny 冲突 → 拒绝零写入；双空=清覆写回纯继承；
- 路由 GET /api/users/{id}/perms、POST /users/perms/{id} 与角色变更/设额度走同一层级守卫
  （admin 不得动平级 admin / master 行不可触），写入成功落 ``set_perms`` 审计行；
- GET /api/users/char-usage 第二批扩展（enforce / license_month / daily /
  agents[].has_overrides）是并行线 workspace_usage.html 的消费契约，字段名漂移=契约破坏。
"""

import json

import pytest
from starlette.testclient import TestClient

from src.utils.web_user_store import (
    PERM_REGISTRY,
    ROLE_MASTER,
    WebUserStore,
    default_perm_allowed,
    parse_perms,
    resolve_user_perm,
)

_REGISTRY_ORDER = [
    "chat.send_text", "chat.send_media", "chat.send_voice", "ai.translate",
]


@pytest.fixture(autouse=True)
def _isolated_usage_stores():
    """坐席字符账本 + 授权额度库两个模块级单例按用例隔离（防串味/防泄漏）。"""
    from src.licensing.quota_store import reset_license_quota_store
    from src.utils.agent_char_usage import reset_agent_char_usage_store
    reset_agent_char_usage_store()
    reset_license_quota_store()
    yield
    reset_agent_char_usage_store()
    reset_license_quota_store()


# ── fixtures（照 test_team_roles_quota 模式：master 建号 → 登出 → 目标号登录）──

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
        _provision_and_login(c, "permadmin", "admin123456", "admin")
        yield c


# ─────────────────────────────────────────────────────────
# 纯函数矩阵
# ─────────────────────────────────────────────────────────

class TestRegistryAndDefaults:
    def test_registry_v1_shape_and_order(self):
        # v1 四键钉死（加键必须同时有执法点——见 PERM_REGISTRY 注释纪律）
        assert list(PERM_REGISTRY) == _REGISTRY_ORDER
        for key, meta in PERM_REGISTRY.items():
            assert meta["domain"] in ("chat", "ai"), key
            assert isinstance(meta["roles"], set) and meta["roles"], key

    def test_default_perm_allowed_matrix(self):
        for perm in _REGISTRY_ORDER:
            for role in ("master", "admin", "supervisor", "agent"):
                assert default_perm_allowed(role, perm) is True, (role, perm)
            # viewer 不在任何注册键的默认允许集
            assert default_perm_allowed("viewer", perm) is False, perm
            assert default_perm_allowed("", perm) is False, perm
        # 未注册键 → 恒 True（fail-open）
        assert default_perm_allowed("viewer", "no.such_perm") is True
        assert default_perm_allowed("", "") is True


class TestParsePerms:
    def test_empty_and_none(self):
        assert parse_perms("") == {"allow": set(), "deny": set()}
        assert parse_perms(None) == {"allow": set(), "deny": set()}

    def test_bad_json_and_wrong_shapes(self):
        assert parse_perms("{bad json") == {"allow": set(), "deny": set()}
        assert parse_perms("[1,2]") == {"allow": set(), "deny": set()}
        assert parse_perms('"str"') == {"allow": set(), "deny": set()}
        # allow/deny 非 list → 该桶按空处理
        assert parse_perms('{"allow": {"x": 1}, "deny": "y"}') == {
            "allow": set(), "deny": set()}

    def test_valid_payload_and_non_string_members_skipped(self):
        raw = json.dumps({"allow": ["ai.translate", 3, ""],
                          "deny": ["chat.send_voice"]})
        out = parse_perms(raw)
        assert out["allow"] == {"ai.translate"}
        assert out["deny"] == {"chat.send_voice"}


class TestResolveUserPerm:
    def _mk(self, tmp_path):
        store = WebUserStore(tmp_path / "users.db")
        return store

    def _plant_raw(self, store, username, raw):
        """直写 perms_json（模拟手改库/历史脏数据——set_user_perms 会拒绝的形态）。"""
        store._conn.execute(
            "UPDATE web_users SET perms_json=? WHERE username=?", (raw, username))
        store._conn.commit()

    def test_master_immune_even_with_deny_row(self, tmp_path):
        store = self._mk(tmp_path)
        u = store.create_user("boss", "pass123456", ROLE_MASTER)
        self._plant_raw(store, "boss", '{"deny":["chat.send_text"]}')
        assert resolve_user_perm(store, "boss", "master", "chat.send_text") is True

    def test_deny_beats_allow_and_role_default(self, tmp_path):
        store = self._mk(tmp_path)
        store.create_user("ag", "pass123456", "agent")
        # 同键同现 allow+deny（只能来自手改库）→ deny 优先
        self._plant_raw(
            store, "ag",
            '{"allow":["chat.send_voice"],"deny":["chat.send_voice"]}')
        assert resolve_user_perm(store, "ag", "agent", "chat.send_voice") is False
        # 纯 deny：压过角色默认允许；其余键不受影响
        u2 = store.create_user("ag2", "pass123456", "agent")
        assert store.set_user_perms(u2["id"], [], ["ai.translate"]) is True
        assert resolve_user_perm(store, "ag2", "agent", "ai.translate") is False
        assert resolve_user_perm(store, "ag2", "agent", "chat.send_text") is True

    def test_allow_grants_beyond_role_default(self, tmp_path):
        store = self._mk(tmp_path)
        u = store.create_user("vw", "pass123456", "viewer")
        assert resolve_user_perm(store, "vw", "viewer", "chat.send_text") is False
        assert store.set_user_perms(u["id"], ["chat.send_text"], []) is True
        assert resolve_user_perm(store, "vw", "viewer", "chat.send_text") is True
        # 未覆写的键仍按角色默认禁止
        assert resolve_user_perm(store, "vw", "viewer", "ai.translate") is False

    def test_unregistered_perm_fail_open(self, tmp_path):
        store = self._mk(tmp_path)
        store.create_user("vw2", "pass123456", "viewer")
        self._plant_raw(store, "vw2", '{"deny":["future.key"]}')
        # 未注册键不进判定，恒放行（fail-open）——即便 deny 里写着它
        assert resolve_user_perm(store, "vw2", "viewer", "future.key") is True

    def test_fallbacks_never_raise(self, tmp_path):
        store = self._mk(tmp_path)
        store.create_user("ag3", "pass123456", "agent")
        # 坏 JSON → 角色默认
        self._plant_raw(store, "ag3", "{oops")
        assert resolve_user_perm(store, "ag3", "agent", "chat.send_text") is True
        # 行缺失 / username 空 / store 缺 → 角色默认
        assert resolve_user_perm(store, "ghost", "agent", "chat.send_text") is True
        assert resolve_user_perm(store, "ghost", "viewer", "chat.send_text") is False
        assert resolve_user_perm(store, "", "agent", "chat.send_text") is True
        assert resolve_user_perm(None, "whoever", "viewer", "ai.translate") is False

        # store.get_user 抛异常 → 角色默认（绝不外抛）
        class _Boom:
            def get_user(self, u):
                raise RuntimeError("db down")

        assert resolve_user_perm(_Boom(), "x", "agent", "chat.send_text") is True
        assert resolve_user_perm(_Boom(), "x", "viewer", "chat.send_text") is False


class TestSetUserPerms:
    def test_reject_unknown_key_and_conflict(self, tmp_path):
        store = WebUserStore(tmp_path / "users.db")
        u = store.create_user("ag", "pass123456", "agent")
        assert store.set_user_perms(u["id"], ["no.such"], []) is False
        assert store.set_user_perms(u["id"], [], ["no.such"]) is False
        assert store.set_user_perms(
            u["id"], ["chat.send_text"], ["chat.send_text"]) is False
        # 整单拒绝=零写入
        assert store.get_user("ag")["perms_json"] == ""

    def test_write_readback_and_clear(self, tmp_path):
        store = WebUserStore(tmp_path / "users.db")
        u = store.create_user("ag", "pass123456", "agent")
        assert store.set_user_perms(
            u["id"], ["chat.send_media"], ["chat.send_voice", "ai.translate"]) is True
        out = parse_perms(store.get_user("ag")["perms_json"])
        assert out["allow"] == {"chat.send_media"}
        assert out["deny"] == {"chat.send_voice", "ai.translate"}
        # get_user_by_id / list_users 均带出该列
        assert store.get_user_by_id(u["id"])["perms_json"]
        assert all("perms_json" in r for r in store.list_users())
        # 双空 → 存 ''（回归纯继承）
        assert store.set_user_perms(u["id"], [], []) is True
        assert store.get_user("ag")["perms_json"] == ""

    def test_nonexistent_user_false(self, tmp_path):
        store = WebUserStore(tmp_path / "users.db")
        assert store.set_user_perms(99999, ["chat.send_text"], []) is False


# ─────────────────────────────────────────────────────────
# 路由层：读写 + 层级守卫 + 审计
# ─────────────────────────────────────────────────────────

class TestPermRoutes:
    def _create(self, client, username, role):
        r = client.post(
            "/users/create",
            data={"username": username, "password": "pass123456", "role": role},
            headers={"Accept": "application/json"},
        )
        assert r.json().get("ok") is True, r.text[:200]
        return r.json()["id"]

    def test_master_reads_registry_order_and_defaults(self, auth_client):
        uid = self._create(auth_client, "permagent", "agent")
        r = auth_client.get(f"/api/users/{uid}/perms",
                            headers={"Accept": "application/json"})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True and d["role"] == "agent"
        assert [p["key"] for p in d["perms"]] == _REGISTRY_ORDER
        assert all(p["state"] == "inherit" for p in d["perms"])
        assert all(p["default"] is True for p in d["perms"])
        assert [p["domain"] for p in d["perms"]] == ["chat", "chat", "chat", "ai"]

    def test_viewer_defaults_denied(self, auth_client):
        uid = self._create(auth_client, "permviewer", "viewer")
        d = auth_client.get(f"/api/users/{uid}/perms",
                            headers={"Accept": "application/json"}).json()
        assert all(p["default"] is False for p in d["perms"])

    def test_three_state_roundtrip_and_audit(self, auth_client, audit_store):
        uid = self._create(auth_client, "permrt", "agent")
        # inherit → deny
        r = auth_client.post(
            f"/users/perms/{uid}",
            json={"allow": [], "deny": ["chat.send_voice"]},
            headers={"Accept": "application/json"})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        by_key = {p["key"]: p for p in d["perms"]}
        assert by_key["chat.send_voice"]["state"] == "deny"
        assert by_key["chat.send_text"]["state"] == "inherit"
        # 读回 deny
        d = auth_client.get(f"/api/users/{uid}/perms",
                            headers={"Accept": "application/json"}).json()
        assert {p["key"]: p["state"] for p in d["perms"]}["chat.send_voice"] == "deny"
        # allow 档也能写
        r = auth_client.post(
            f"/users/perms/{uid}",
            json={"allow": ["ai.translate"], "deny": []},
            headers={"Accept": "application/json"})
        assert {p["key"]: p["state"] for p in r.json()["perms"]}[
            "ai.translate"] == "allow"
        # 清空 → 回 inherit
        r = auth_client.post(
            f"/users/perms/{uid}", json={"allow": [], "deny": []},
            headers={"Accept": "application/json"})
        assert all(p["state"] == "inherit" for p in r.json()["perms"])
        # 审计行落库（每次成功写一行）
        rows = audit_store.query(limit=10, action="set_perms")
        assert len(rows) >= 3
        assert any("permrt" in str(x.get("target") or "") for x in rows)

    def test_admin_writes_agent_but_not_peer_admin(self, admin_client, config_dir):
        store = WebUserStore(config_dir / "web_users.db")
        ag = store.create_user("permag2", "pass123456", "agent")
        r = admin_client.post(
            f"/users/perms/{ag['id']}",
            json={"allow": [], "deny": ["ai.translate"]},
            headers={"Accept": "application/json"})
        assert r.status_code == 200 and r.json()["ok"] is True
        peer = store.create_user("peeradmin3", "pass123456", "admin")
        r = admin_client.get(f"/api/users/{peer['id']}/perms",
                             headers={"Accept": "application/json"})
        assert r.status_code == 403
        r = admin_client.post(
            f"/users/perms/{peer['id']}",
            json={"allow": [], "deny": ["ai.translate"]},
            headers={"Accept": "application/json"})
        assert r.status_code == 403
        assert store.get_user("peeradmin3")["perms_json"] == ""

    def test_master_row_untouchable(self, auth_client, config_dir):
        store = WebUserStore(config_dir / "web_users.db")
        boss = store.get_user("admin")  # auth_client 的 master 行
        assert boss and boss["role"] == "master"
        r = auth_client.get(f"/api/users/{boss['id']}/perms",
                            headers={"Accept": "application/json"})
        assert r.status_code == 403
        r = auth_client.post(
            f"/users/perms/{boss['id']}",
            json={"allow": [], "deny": ["chat.send_text"]},
            headers={"Accept": "application/json"})
        assert r.status_code == 403

    def test_bad_payloads_400_and_404(self, auth_client):
        uid = self._create(auth_client, "permbad", "agent")
        # 未注册键
        r = auth_client.post(
            f"/users/perms/{uid}", json={"allow": ["no.such"], "deny": []},
            headers={"Accept": "application/json"})
        assert r.status_code == 400 and r.json().get("detail")
        # allow ∩ deny 冲突
        r = auth_client.post(
            f"/users/perms/{uid}",
            json={"allow": ["chat.send_text"], "deny": ["chat.send_text"]},
            headers={"Accept": "application/json"})
        assert r.status_code == 400
        # 非 dict body
        r = auth_client.post(
            f"/users/perms/{uid}", json=["chat.send_text"],
            headers={"Accept": "application/json"})
        assert r.status_code == 400
        # allow 非 list
        r = auth_client.post(
            f"/users/perms/{uid}", json={"allow": "chat.send_text", "deny": []},
            headers={"Accept": "application/json"})
        assert r.status_code == 400
        # 目标不存在
        r = auth_client.get("/api/users/99999/perms",
                            headers={"Accept": "application/json"})
        assert r.status_code == 404
        r = auth_client.post(
            "/users/perms/99999", json={"allow": [], "deny": []},
            headers={"Accept": "application/json"})
        assert r.status_code == 404


# ─────────────────────────────────────────────────────────
# char-usage 二批扩展契约（workspace_usage.html 消费面——字段名不可漂移）
# ─────────────────────────────────────────────────────────

class TestCharUsageSecondBatch:
    def test_default_env_structure(self, admin_client):
        r = admin_client.get("/api/users/char-usage",
                             headers={"Accept": "application/json"})
        assert r.status_code == 200
        d = r.json()
        # enforce：usage.agent_chars.enforce 缺省 False（bool 类型钉死）
        assert d["enforce"] is False
        # license_month：quota store 未建 → available=False 全零
        lm = d["license_month"]
        assert set(lm) == {"available", "total", "by_category"}
        assert lm["available"] is False
        assert lm["total"] == 0 and lm["by_category"] == {}
        # daily：坐席账本未建 → []
        assert d["daily"] == []
        # agents 行带 has_overrides（默认 False）
        assert d["agents"], "至少含登录用户行"
        for a in d["agents"]:
            assert a["has_overrides"] is False

    def test_enforce_reflects_config(self, admin_client, config_manager):
        config_manager.config.setdefault("usage", {})["agent_chars"] = {
            "enabled": True, "enforce": True}
        d = admin_client.get("/api/users/char-usage",
                             headers={"Accept": "application/json"}).json()
        assert d["enforce"] is True
        assert d["enabled"] is True

    def test_daily_series_and_license_month_with_stores(self, admin_client):
        from src.licensing.quota_store import (
            LicenseQuotaStore,
            configure_license_quota_store,
        )
        from src.utils.agent_char_usage import (
            AgentCharUsageStore,
            configure_agent_char_usage,
        )
        s = AgentCharUsageStore(":memory:")
        s.record("permadmin", "tts", 123)
        configure_agent_char_usage(store=s)
        lic = LicenseQuotaStore(":memory:")
        # check_license_quota 在无授权测试环境回 lic_id="default"
        lic.record("default", "translation", 456)
        configure_license_quota_store(store=lic)
        d = admin_client.get("/api/users/char-usage",
                             headers={"Accept": "application/json"}).json()
        # daily：近 7 天逐日（旧→新，缺数据日补零），今日尾项含刚记的 123
        assert len(d["daily"]) == 7
        assert d["daily"][-1]["total"] == 123
        assert d["daily"][0]["total"] == 0
        # license_month：当月口径 = usage_history(1) 尾项
        assert d["license_month"] == {
            "available": True, "total": 456, "by_category": {}}
        # has_overrides：经路由写覆写后，该 agent 行翻 True、未覆写行保持 False
        r = admin_client.post(
            "/users/create",
            data={"username": "ovagent", "password": "pass123456",
                  "role": "agent"},
            headers={"Accept": "application/json"})
        uid = r.json()["id"]
        admin_client.post(
            f"/users/perms/{uid}", json={"allow": [], "deny": ["ai.translate"]},
            headers={"Accept": "application/json"})
        d2 = admin_client.get("/api/users/char-usage",
                              headers={"Accept": "application/json"}).json()
        flags = {a["username"]: a["has_overrides"] for a in d2["agents"]}
        assert flags["ovagent"] is True
        assert flags["permadmin"] is False
