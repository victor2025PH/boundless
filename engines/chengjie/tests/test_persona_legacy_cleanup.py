"""legacy peer-global 绑定清理工具（P3）门禁。

2026-07-24 双账号串音修复后，legacy 绑定在有账号人设时恒被压制成「暗债」。
清理工具＝盘点（GET /api/persona/legacy-bindings）+ 逐条处置
（POST /api/persona/legacy-bindings/cleanup：upgrade 语义保真升级 / remove 清除）。

覆盖三层：
1. 纯函数 —— ``is_conv_binding_key``（3 段覆写键判据：宁可漏判不可错判）。
2. 真实 InboxStore —— ``find_conversations_by_chat_key``（跨平台/账号反查 +
   带前缀旧键兼容 + 排序）。
3. 路由 —— 盘点行契约（kind/stale/suppressed）+ upgrade 全链（3 段键落地、
   legacy 删除、生效切换）+ remove + 全部拒绝路径。
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
    is_conv_binding_key,
    resolve_effective_persona,
)
from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore
from src.utils.persona_manager import PersonaManager

_PROFILES = {
    "personas": {
        "profiles": [
            {"id": "chen_mo", "name": "陈默", "role": "陪伴"},
            {"id": "lin_xiaoyu", "name": "林小雨", "role": "陪伴"},
            {"id": "zhao_min", "name": "赵敏", "role": "客服"},
        ]
    }
}


# ── 1. 纯函数：覆写键判据 ────────────────────────────────────────────────────

def test_is_conv_binding_key_semantics():
    # 3 段 + 已知平台 → 覆写键
    assert is_conv_binding_key("telegram:8244899900:8921664288") is True
    assert is_conv_binding_key("whatsapp:wa_x:639@s.whatsapp.net") is True
    assert is_conv_binding_key("line:acct:U1234") is True
    # legacy 形态全部不是：裸 id / 2 段前缀键 / 未知平台 3 段（宁可漏判）
    assert is_conv_binding_key("8921664288") is False
    assert is_conv_binding_key("line_rpa:U1234") is False
    assert is_conv_binding_key("weird_plat:a:b") is False
    # 空段/空串防御
    assert is_conv_binding_key("telegram::x") is False
    assert is_conv_binding_key("") is False
    assert is_conv_binding_key(None) is False


# ── 2. 真实 store 反查 ───────────────────────────────────────────────────────

def _conv(cid, plat, acct, ck, ts):
    return InboxConversation(
        conversation_id=cid, platform=plat, account_id=acct, chat_key=ck,
        display_name=f"客户{ck}", language="zh", last_text="hi",
        last_ts=ts, unread=0,
    )


def test_find_conversations_by_chat_key(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    try:
        # 同一 peer 落在两个账号 + 一条无关会话
        store.upsert_conversation(
            _conv("telegram:acct1:777", "telegram", "acct1", "777", 200))
        store.upsert_conversation(
            _conv("telegram:acct2:777", "telegram", "acct2", "777", 100))
        store.upsert_conversation(
            _conv("line:a:room1", "line", "a", "room1", 300))

        rows = store.find_conversations_by_chat_key("777")
        assert [r["account_id"] for r in rows] == ["acct1", "acct2"]  # last_ts DESC
        assert all(r["chat_key"] == "777" for r in rows)

        # 带前缀旧键（line_rpa:room1）→ 去前缀尾段也命中
        rows = store.find_conversations_by_chat_key("line_rpa:room1")
        assert len(rows) == 1 and rows[0]["platform"] == "line"

        assert store.find_conversations_by_chat_key("") == []
        assert store.find_conversations_by_chat_key("nobody") == []
    finally:
        store.close()


# ── 3. 路由 ──────────────────────────────────────────────────────────────────

@pytest.fixture
def app_on(tmp_path):
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


class _FakeStore:
    """find_conversations_by_chat_key 的最小假 store（按键返回预置落点）。"""

    def __init__(self, mapping=None):
        self._map = mapping or {}

    def find_conversations_by_chat_key(self, key, limit=50):
        return list(self._map.get(str(key), []))


def _row(plat, acct, ck):
    return {
        "conversation_id": f"{plat}:{acct}:{ck}", "platform": plat,
        "account_id": acct, "chat_key": ck,
        "display_name": f"客户{ck}", "chat_type": "private", "last_ts": 1,
    }


_HDRS = {"Authorization": "Bearer test-token",
         "Content-Type": "application/json"}


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_inventory_rows_contract(app_on):
    """盘点行契约：conv 3 段键不列；reference/inline/stale 各归各；
    有账号人设的落点 suppressed=True、没有的 False。"""
    from src.integrations.account_registry import get_account_registry
    get_account_registry().upsert(
        "telegram", "acct1",
        meta={"persona_id": "lin_xiaoyu"}, merge_meta=True)

    pm = PersonaManager.get_instance()
    pm.bind_chat_persona_by_profile_id("777", "chen_mo")           # legacy 引用
    pm.bind_chat_persona("888", {"name": "自定义客服"})              # legacy 内联
    pm.bind_chat_persona_by_profile_id("999", "zhao_min")          # 将变 stale
    pm._profile_personas.pop("zhao_min")
    pm.bind_chat_persona_by_profile_id(                             # 3 段覆写键：不是债
        conv_binding_key("telegram", "acct1", "555"), "chen_mo")

    app_on.state.inbox_store = _FakeStore({
        "777": [_row("telegram", "acct1", "777"),   # acct1 有账号人设 → 压制
                _row("telegram", "acct9", "777")],  # acct9 没有 → legacy 还活着
    })
    async with _client(app_on) as c:
        r = await c.get("/api/persona/legacy-bindings", headers=_HDRS)
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True and d["enabled"] is True
        by_key = {it["key"]: it for it in d["items"]}
        assert set(by_key) == {"777", "888", "999"}, "3 段覆写键不得列为债"

        it = by_key["777"]
        assert it["kind"] == "reference" and it["profile_id"] == "chen_mo"
        assert it["stale"] is False and it["profile_name"] == "陈默"
        supp = {c_["account_id"]: c_["suppressed"] for c_ in it["conversations"]}
        assert supp == {"acct1": True, "acct9": False}
        assert it["suppressed_all"] is False

        assert by_key["888"]["kind"] == "inline"
        assert by_key["888"]["profile_name"] == "自定义客服"
        assert by_key["999"]["stale"] is True


@pytest.mark.asyncio
async def test_cleanup_upgrade_full_chain(app_on):
    """upgrade 语义保真：peer 全部落点各得一条 3 段覆写 + legacy 键删除 +
    出站解析立即按覆写生效。"""
    pm = PersonaManager.get_instance()
    pm.bind_chat_persona_by_profile_id("777", "chen_mo")
    app_on.state.inbox_store = _FakeStore({
        "777": [_row("telegram", "acct1", "777"),
                _row("whatsapp", "wa_x", "777")],
    })
    async with _client(app_on) as c:
        r = await c.post("/api/persona/legacy-bindings/cleanup", headers=_HDRS,
                         json={"key": "777", "action": "upgrade"})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True and d["upgraded_count"] == 2

    assert pm.get_chat_binding_ref("777") == ""          # legacy 键已删
    k1 = conv_binding_key("telegram", "acct1", "777")
    k2 = conv_binding_key("whatsapp", "wa_x", "777")
    assert pm.get_chat_binding_ref(k1) == "chen_mo"
    assert pm.get_chat_binding_ref(k2) == "chen_mo"

    # 升级后出站解析真按覆写走（语义保真的终点断言）
    cfg = {"inbox": {"persona_conv_override": {"enabled": True}}}

    class _NoAcct:
        def get(self, platform, account_id):
            return {"meta": {}}

    pid, tier = resolve_effective_persona(
        cfg, "telegram", "acct1", "777", registry=_NoAcct())
    assert (pid, tier) == ("chen_mo", "conv_override")


@pytest.mark.asyncio
async def test_cleanup_remove(app_on):
    pm = PersonaManager.get_instance()
    pm.bind_chat_persona_by_profile_id("777", "chen_mo")
    pm.bind_chat_persona("888", {"name": "自定义客服"})
    app_on.state.inbox_store = _FakeStore()
    async with _client(app_on) as c:
        for key in ("777", "888"):
            r = await c.post("/api/persona/legacy-bindings/cleanup",
                             headers=_HDRS,
                             json={"key": key, "action": "remove"})
            assert r.status_code == 200 and r.json()["ok"] is True
        r = await c.get("/api/persona/legacy-bindings", headers=_HDRS)
        assert r.json()["total"] == 0
    assert pm.has_chat_binding("777") is False
    assert pm.has_chat_binding("888") is False


@pytest.mark.asyncio
async def test_cleanup_rejections(app_on):
    """全部拒绝路径：开关关 400 / stale 409 / 无落点 404 / 键不存在 404 /
    覆写键与坏 action 400。"""
    pm = PersonaManager.get_instance()
    pm.bind_chat_persona_by_profile_id("777", "chen_mo")
    pm.bind_chat_persona_by_profile_id("999", "zhao_min")
    pm._profile_personas.pop("zhao_min")
    app_on.state.inbox_store = _FakeStore({
        "777": [_row("telegram", "acct1", "777")],
    })
    cm = app_on.state.config_manager
    async with _client(app_on) as c:
        # 开关关 → upgrade 拒绝（写入静默失效的绑定＝旧事故形态，直接拒）
        cm.config["inbox"]["persona_conv_override"]["enabled"] = False
        r = await c.post("/api/persona/legacy-bindings/cleanup", headers=_HDRS,
                         json={"key": "777", "action": "upgrade"})
        assert r.status_code == 400
        cm.config["inbox"]["persona_conv_override"]["enabled"] = True

        # stale profile → 409（升不了级，只能 remove）
        r = await c.post("/api/persona/legacy-bindings/cleanup", headers=_HDRS,
                         json={"key": "999", "action": "upgrade"})
        assert r.status_code == 409

        # 查无落点 → 404
        app_on.state.inbox_store = _FakeStore()
        r = await c.post("/api/persona/legacy-bindings/cleanup", headers=_HDRS,
                         json={"key": "777", "action": "upgrade"})
        assert r.status_code == 404

        # 键不存在 / 3 段覆写键 / 坏 action / 空键
        r = await c.post("/api/persona/legacy-bindings/cleanup", headers=_HDRS,
                         json={"key": "nobody", "action": "remove"})
        assert r.status_code == 404
        r = await c.post("/api/persona/legacy-bindings/cleanup", headers=_HDRS,
                         json={"key": "telegram:acct1:777", "action": "remove"})
        assert r.status_code == 400
        r = await c.post("/api/persona/legacy-bindings/cleanup", headers=_HDRS,
                         json={"key": "777", "action": "explode"})
        assert r.status_code == 400
        r = await c.post("/api/persona/legacy-bindings/cleanup", headers=_HDRS,
                         json={"key": "", "action": "remove"})
        assert r.status_code == 400

    # 拒绝路径不得有副作用：两条 legacy 都还在
    assert pm.get_chat_binding_ref("777") == "chen_mo"
    assert pm.has_chat_binding("999") is True
