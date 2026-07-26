"""会话级人设覆写（2026-07-26 方案 A）门禁。

覆盖三层：
1. 纯函数 —— ``conv_binding_key``（与 inbox.normalizer.conv_id 同构钉）、
   ``conv_override_enabled``（开关语义 + 异常安全）、
   ``resolve_effective_persona``（覆写 > 账号 > 空；kill-switch；悬空 profile 降级）。
2. PersonaManager —— ``get_persona_with_tier(conversation_key=)`` 优先级 +
   **2026-07-24 回归钉**（无覆写时账号人设仍压过 legacy chat 绑定，防双号串话）。
3. 路由 —— ``GET /api/persona/effective`` 全景 / ``POST /api/persona/bind|unbind``
   (scope=conversation) / ``POST /api/persona/account-persona``（registry 写侧）。

registry 均走 conftest autouse 的临时库（绝不碰生产 account_registry.db）。
"""

import asyncio
import sys
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.ai.persona_voice import (
    conv_binding_key,
    conv_override_enabled,
    resolve_effective_persona,
    resolve_effective_persona_id,
)
from src.utils.persona_manager import PersonaManager

_PROFILES = {
    "personas": {
        "profiles": [
            {"id": "chen_mo", "name": "陈默", "role": "陪伴"},
            {"id": "lin_xiaoyu", "name": "林小雨", "role": "陪伴"},
        ]
    }
}


class _FakeRegistry:
    """resolve_account_persona_id 只用 .get(platform, account_id)。"""

    def __init__(self, meta=None):
        self._meta = meta or {}

    def get(self, platform, account_id):
        return {"meta": dict(self._meta)}


@pytest.fixture()
def pm():
    PersonaManager.reset()
    m = PersonaManager.get_instance()
    m.load_profiles_from_config(_PROFILES)
    yield m
    PersonaManager.reset()


# ── 1. 纯函数 ────────────────────────────────────────────────────────────────

def test_conv_binding_key_matches_normalizer_conv_id():
    """3 段键必须与 inbox 层 conv_id 逐字节同构（persona_voice 不 import inbox 防环，
    一致性靠本钉守住——漂移=前端 conversation_id 与后端绑定键对不上，覆写全失效）。"""
    from src.inbox.normalizer import conv_id
    cases = [
        ("telegram", "8244899900", "8921664288"),
        ("line_rpa", "default", "U1234abcd"),
        ("whatsapp", "wa_23l9mu5o", "639270135480@s.whatsapp.net"),
    ]
    for plat, acct, ck in cases:
        assert conv_binding_key(plat, acct, ck) == conv_id(plat, acct, ck)


def test_conv_override_enabled_semantics():
    assert conv_override_enabled({}) is False
    assert conv_override_enabled(None) is False
    assert conv_override_enabled(
        {"inbox": {"persona_conv_override": {"enabled": True}}}) is True
    assert conv_override_enabled(
        {"inbox": {"persona_conv_override": {"enabled": False}}}) is False
    # 异常安全：inbox 段是垃圾类型也不抛
    assert conv_override_enabled({"inbox": "garbage"}) is False


_FLAG_ON = {"inbox": {"persona_conv_override": {"enabled": True}}}
_FLAG_OFF = {"inbox": {"persona_conv_override": {"enabled": False}}}


def test_resolve_conv_override_wins_when_enabled(pm):
    key = conv_binding_key("telegram", "acct1", "chat9")
    pm.bind_chat_persona_by_profile_id(key, "chen_mo")
    reg = _FakeRegistry({"persona_id": "lin_xiaoyu"})
    pid, tier = resolve_effective_persona(
        _FLAG_ON, "telegram", "acct1", "chat9", registry=reg)
    assert (pid, tier) == ("chen_mo", "conv_override")


def test_resolve_kill_switch_ignores_binding_when_disabled(pm):
    """开关关＝已写入的覆写立即失效（事故 kill-switch 语义），回账号人设。"""
    key = conv_binding_key("telegram", "acct1", "chat9")
    pm.bind_chat_persona_by_profile_id(key, "chen_mo")
    reg = _FakeRegistry({"persona_id": "lin_xiaoyu"})
    pid, tier = resolve_effective_persona(
        _FLAG_OFF, "telegram", "acct1", "chat9", registry=reg)
    assert (pid, tier) == ("lin_xiaoyu", "account_profile")


def test_resolve_stale_conv_ref_degrades_to_account(pm):
    """覆写指向已删除 profile → 视同未覆写（绝不给出站链悬空 id）。"""
    key = conv_binding_key("telegram", "acct1", "chat9")
    pm.bind_chat_persona_by_profile_id(key, "chen_mo")
    pm._profile_personas.pop("chen_mo")   # 模拟 profile 事后被删
    reg = _FakeRegistry({"persona_id": "lin_xiaoyu"})
    pid, tier = resolve_effective_persona(
        _FLAG_ON, "telegram", "acct1", "chat9", registry=reg)
    assert (pid, tier) == ("lin_xiaoyu", "account_profile")


def test_resolve_no_binding_falls_to_account_plural(pm):
    """无覆写 → 账号层；registry 复数 persona_ids[0] 也认（7-24 修复口径）。"""
    reg = _FakeRegistry({"persona_ids": ["lin_xiaoyu"]})
    pid, tier = resolve_effective_persona(
        _FLAG_ON, "telegram", "acct1", "chat9", registry=reg)
    assert (pid, tier) == ("lin_xiaoyu", "account_profile")


def test_resolve_nothing_returns_empty(pm):
    pid, tier = resolve_effective_persona(
        _FLAG_ON, "telegram", "acct1", "chat9", registry=_FakeRegistry())
    assert (pid, tier) == ("", "")


def test_resolve_empty_chat_key_skips_conv_layer(pm):
    """chat_key 空（如纯账号语境）→ 不查会话层，直接账号级。"""
    reg = _FakeRegistry({"persona_id": "lin_xiaoyu"})
    assert resolve_effective_persona_id(
        _FLAG_ON, "telegram", "acct1", "", registry=reg) == "lin_xiaoyu"


def test_conv_binding_persist_roundtrip(pm, tmp_path):
    """3 段覆写键经 persist_chat_bindings → bindings_runtime.yaml →
    load_chat_bindings_runtime 完整往返（键含冒号，YAML 序列化不得丢/变形）。"""
    key = conv_binding_key("telegram", "8244899900", "7340576921")
    pm.bind_chat_persona_by_profile_id(key, "chen_mo")
    pm.bind_chat_persona_by_profile_id("7340576921", "lin_xiaoyu")  # legacy 共存

    class _CM:
        config_path = str(tmp_path / "config.yaml")
        config = {"persona_persistence": {"enabled": True}}

    assert pm.persist_chat_bindings(_CM()) is True

    PersonaManager.reset()
    pm2 = PersonaManager.get_instance()
    pm2.load_profiles_from_config(_PROFILES)
    n = pm2.load_chat_bindings_runtime(
        Path(_CM.config_path), _CM.config)
    assert n >= 2
    assert pm2.get_chat_binding_ref(key) == "chen_mo"
    assert pm2.get_chat_binding_ref("7340576921") == "lin_xiaoyu"


# ── 2. PersonaManager 分层（含 7-24 回归钉）─────────────────────────────────

def test_tier_conv_key_beats_account(pm):
    key = conv_binding_key("telegram", "a1", "c1")
    pm.bind_chat_persona_by_profile_id(key, "chen_mo")
    p, tier = pm.get_persona_with_tier(
        "c1", "lin_xiaoyu", conversation_key=key)
    assert p["id"] == "chen_mo" and tier == "conv_override"


def test_tier_regression_20260724_account_beats_legacy_chat(pm):
    """7-24 回归钉：无会话覆写时，账号人设仍压过 legacy peer-global chat 绑定
    （双协议号同客户不串话的根基——本次改造不得回退该语义）。"""
    pm.bind_chat_persona_by_profile_id("c1", "chen_mo")   # legacy 裸 chat 键
    p, tier = pm.get_persona_with_tier("c1", "lin_xiaoyu")
    assert p["id"] == "lin_xiaoyu" and tier == "account_profile"


def test_tier_legacy_chat_when_no_account(pm):
    pm.bind_chat_persona_by_profile_id("c1", "chen_mo")
    p, tier = pm.get_persona_with_tier("c1", "")
    assert p["id"] == "chen_mo" and tier == "chat_binding"


def test_tier_stale_conv_key_falls_through(pm):
    key = conv_binding_key("telegram", "a1", "c1")
    pm.bind_chat_persona_by_profile_id(key, "chen_mo")
    pm._profile_personas.pop("chen_mo")
    p, tier = pm.get_persona_with_tier("c1", "lin_xiaoyu", conversation_key=key)
    assert p["id"] == "lin_xiaoyu" and tier == "account_profile"


# ── 3. 路由 ──────────────────────────────────────────────────────────────────

@pytest.fixture
def app_on(tmp_path):
    """开关开的最小 app（profiles 已注册；registry=conftest 临时库）。"""
    cfg = {
        "domain": "general",
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {"enabled": []},
        "web_admin": {"auth_token": "test-token", "secret_key": "test-secret"},
        "inbox": {"persona_conv_override": {"enabled": True}},
        "personas": _PROFILES["personas"],
    }
    (tmp_path / "config.yaml").write_text(
        yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text(
        yaml.dump({"greeting": ["hi"]}), encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text(
        yaml.dump({"channels": {}}), encoding="utf-8")

    from src.utils.config_manager import ConfigManager
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(cm.load())
    finally:
        loop.close()

    PersonaManager.reset()
    PersonaManager.get_instance().load_profiles_from_config(_PROFILES)

    from src.web.admin import create_app
    app = create_app(cm)
    yield app
    PersonaManager.reset()


_HDRS = {"Authorization": "Bearer test-token",
         "Content-Type": "application/json"}
_CID = "telegram:8244899900:8921664288"


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_effective_requires_conv_ref(app_on):
    async with _client(app_on) as c:
        r = await c.get("/api/persona/effective", headers=_HDRS)
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_bind_conv_then_effective_panorama(app_on):
    """bind(scope=conversation) → effective 全景：conv 层命中 + tier 说真话 +
    legacy 压制标注 + unbind 后回账号层。"""
    from src.integrations.account_registry import get_account_registry
    get_account_registry().upsert(
        "telegram", "8244899900",
        meta={"persona_id": "lin_xiaoyu"}, merge_meta=True)
    pm = PersonaManager.get_instance()
    pm.bind_chat_persona_by_profile_id("8921664288", "chen_mo")   # legacy 层

    async with _client(app_on) as c:
        # 初始：账号级生效，legacy 绑定被压制
        r = await c.get(f"/api/persona/effective?conversation_id={_CID}",
                        headers=_HDRS)
        assert r.status_code == 200
        d = r.json()
        assert d["enabled"] is True
        assert d["effective"]["id"] == "lin_xiaoyu"
        assert d["effective"]["tier"] == "account_profile"
        assert d["legacy"]["id"] == "chen_mo"
        assert d["legacy_suppressed"] is True

        # 换绑本会话 → conv_override 生效
        r = await c.post("/api/persona/bind", headers=_HDRS, json={
            "scope": "conversation", "conversation_id": _CID,
            "profile_id": "chen_mo"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["to"] == "chen_mo"

        r = await c.get(f"/api/persona/effective?conversation_id={_CID}",
                        headers=_HDRS)
        d = r.json()
        assert d["effective"]["id"] == "chen_mo"
        assert d["effective"]["tier"] == "conv_override"
        assert d["conv"]["id"] == "chen_mo"
        assert d["account"]["id"] == "lin_xiaoyu"

        # 解除覆写 → 回账号层
        r = await c.post("/api/persona/unbind", headers=_HDRS, json={
            "scope": "conversation", "conversation_id": _CID})
        assert r.status_code == 200
        assert r.json()["from"] == "chen_mo"

        r = await c.get(f"/api/persona/effective?conversation_id={_CID}",
                        headers=_HDRS)
        d = r.json()
        assert d["effective"]["id"] == "lin_xiaoyu"
        assert d["effective"]["tier"] == "account_profile"
        assert d["conv"] is None


@pytest.mark.asyncio
async def test_bind_conv_rejects_unknown_profile(app_on):
    async with _client(app_on) as c:
        r = await c.post("/api/persona/bind", headers=_HDRS, json={
            "scope": "conversation", "conversation_id": _CID,
            "profile_id": "ghost_nobody"})
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_bind_conv_rejected_when_flag_off(app_on):
    """开关关 → 写侧 400（不允许写入将来才生效的暗绑定）。"""
    cm = app_on.state.config_manager
    cm.config["inbox"]["persona_conv_override"]["enabled"] = False
    try:
        async with _client(app_on) as c:
            r = await c.post("/api/persona/bind", headers=_HDRS, json={
                "scope": "conversation", "conversation_id": _CID,
                "profile_id": "chen_mo"})
            assert r.status_code == 400
    finally:
        cm.config["inbox"]["persona_conv_override"]["enabled"] = True


@pytest.mark.asyncio
async def test_effective_has_outbound_from_store(app_on):
    """has_outbound 弹窗口径素材：store 近 50 条含 out 即 True。"""
    class _FakeStore:
        def list_recent_messages(self, cid, limit=50):
            assert cid == _CID
            return [{"direction": "in"}, {"direction": "out"}]

    app_on.state.inbox_store = _FakeStore()
    async with _client(app_on) as c:
        r = await c.get(f"/api/persona/effective?conversation_id={_CID}",
                        headers=_HDRS)
        assert r.json()["has_outbound"] is True


@pytest.mark.asyncio
async def test_account_persona_set_and_clear(app_on):
    """账号级整号换绑：写 registry（merge_meta 不抹旧键 + persona_id_auto=False
    防 config 目录同步回刷）；空 profile_id=清除。"""
    from src.integrations.account_registry import get_account_registry
    reg = get_account_registry()
    reg.upsert("telegram", "8244899900",
               meta={"persona_id": "lin_xiaoyu", "session_string": "KEEP"},
               merge_meta=True)

    async with _client(app_on) as c:
        r = await c.post("/api/persona/account-persona", headers=_HDRS, json={
            "platform": "telegram", "account_id": "8244899900",
            "profile_id": "chen_mo"})
        assert r.status_code == 200
        body = r.json()
        assert body["from"] == "lin_xiaoyu" and body["to"] == "chen_mo"

        meta = (reg.get("telegram", "8244899900") or {}).get("meta") or {}
        assert meta.get("persona_id") == "chen_mo"
        assert meta.get("persona_ids") == ["chen_mo"]
        assert meta.get("persona_id_auto") is False
        assert meta.get("session_string") == "KEEP", "merge_meta 必须保住既有键"

        # 未知 profile → 404
        r = await c.post("/api/persona/account-persona", headers=_HDRS, json={
            "platform": "telegram", "account_id": "8244899900",
            "profile_id": "ghost_nobody"})
        assert r.status_code == 404

        # 清除（空 profile_id）
        r = await c.post("/api/persona/account-persona", headers=_HDRS, json={
            "platform": "telegram", "account_id": "8244899900",
            "profile_id": ""})
        assert r.status_code == 200
        meta = (reg.get("telegram", "8244899900") or {}).get("meta") or {}
        assert meta.get("persona_id") == ""
        assert meta.get("persona_ids") == []


@pytest.mark.asyncio
async def test_account_persona_requires_ref(app_on):
    async with _client(app_on) as c:
        r = await c.post("/api/persona/account-persona", headers=_HDRS,
                         json={"platform": "", "account_id": ""})
        assert r.status_code == 400
