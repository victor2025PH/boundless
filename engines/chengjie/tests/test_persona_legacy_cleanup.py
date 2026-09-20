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


# ── 4. 一键安全收官（批量，行为保真策略）───────────────────────────────────────


class _NoAcctReg:
    def get(self, platform, account_id):
        return {"meta": {}}


@pytest.mark.asyncio
async def test_auto_cleanup_mixed_policy(app_on):
    """策略矩阵：压制→清除 / stale→清除 / 活债→只给活落点落覆写 /
    查无落点与内联不可解析→跳过。硬不变量：被压制落点的生效人设执行前后不变。"""
    from src.integrations.account_registry import get_account_registry
    get_account_registry().upsert(
        "telegram", "acct1", meta={"persona_id": "lin_xiaoyu"}, merge_meta=True)

    pm = PersonaManager.get_instance()
    pm.bind_chat_persona_by_profile_id("111", "chen_mo")   # 全压制 → remove
    pm.bind_chat_persona_by_profile_id("222", "chen_mo")   # 半活 → upgrade(活落点)
    pm.bind_chat_persona_by_profile_id("333", "zhao_min")  # stale → remove
    pm._profile_personas.pop("zhao_min")
    pm.bind_chat_persona_by_profile_id("444", "chen_mo")   # 查无落点 → skip
    pm.bind_chat_persona("555", {"name": "自定义客服"})      # 内联不可解析 → skip

    app_on.state.inbox_store = _FakeStore({
        "111": [_row("telegram", "acct1", "111")],
        "222": [_row("telegram", "acct1", "222"),
                _row("telegram", "acct9", "222")],
        "333": [_row("telegram", "acct9", "333")],   # 有活落点也救不了 stale
        "555": [_row("telegram", "acct9", "555")],
    })

    cfg = {"inbox": {"persona_conv_override": {"enabled": True}}}
    before = resolve_effective_persona(cfg, "telegram", "acct1", "222")

    async with _client(app_on) as c:
        r = await c.post("/api/persona/legacy-bindings/cleanup-all",
                         headers=_HDRS, json={"dry_run": False})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True and d["dry_run"] is False
        assert d["summary"] == {"upgrade": 1, "remove": 2, "skip": 2,
                                "conv_bindings": 1}
        by_key = {it["key"]: it for it in d["results"]}
        assert (by_key["111"]["decision"], by_key["111"]["reason"]) == ("remove", "suppressed")
        assert (by_key["222"]["decision"], by_key["222"]["reason"]) == ("upgrade", "live")
        assert (by_key["333"]["decision"], by_key["333"]["reason"]) == ("remove", "stale")
        assert (by_key["444"]["decision"], by_key["444"]["reason"]) == ("skip", "no_conversations")
        assert (by_key["555"]["decision"], by_key["555"]["reason"]) == ("skip", "inline_unresolvable")

    # legacy 键：已处置的没了，跳过的还在
    for gone in ("111", "222", "333"):
        assert pm.has_chat_binding(gone) is False, gone
    assert pm.has_chat_binding("444") is True
    assert pm.has_chat_binding("555") is True

    # 覆写只落在活落点（acct9），被压制落点（acct1）绝不落
    assert pm.get_chat_binding_ref(
        conv_binding_key("telegram", "acct9", "222")) == "chen_mo"
    assert pm.get_chat_binding_ref(
        conv_binding_key("telegram", "acct1", "222")) == ""
    assert pm.get_chat_binding_ref(
        conv_binding_key("telegram", "acct9", "333")) == ""

    # 行为保真终点断言：被压制会话执行前后生效人设一致（账号档继续赢）
    after = resolve_effective_persona(cfg, "telegram", "acct1", "222")
    assert before == after == ("lin_xiaoyu", "account_profile")
    # 活落点：legacy 赢 → 覆写赢，人设同一个
    pid, tier = resolve_effective_persona(
        cfg, "telegram", "acct9", "222", registry=_NoAcctReg())
    assert (pid, tier) == ("chen_mo", "conv_override")


@pytest.mark.asyncio
async def test_auto_cleanup_dry_run_no_mutation(app_on):
    """dry_run 出同一份计划但零副作用：绑定/覆写/盘点全不变。"""
    pm = PersonaManager.get_instance()
    pm.bind_chat_persona_by_profile_id("777", "chen_mo")
    app_on.state.inbox_store = _FakeStore({
        "777": [_row("telegram", "acct9", "777")],
    })
    async with _client(app_on) as c:
        r = await c.post("/api/persona/legacy-bindings/cleanup-all",
                         headers=_HDRS, json={"dry_run": True})
        d = r.json()
        assert d["dry_run"] is True
        assert d["summary"]["upgrade"] == 1 and d["summary"]["conv_bindings"] == 1
        assert d["results"][0]["upgraded"] == [
            conv_binding_key("telegram", "acct9", "777")]

        # 零副作用
        assert pm.get_chat_binding_ref("777") == "chen_mo"
        assert pm.get_chat_binding_ref(
            conv_binding_key("telegram", "acct9", "777")) == ""
        inv = await c.get("/api/persona/legacy-bindings", headers=_HDRS)
        assert inv.json()["total"] == 1

        # 幂等：第二次 dry_run 计划一致
        r2 = await c.post("/api/persona/legacy-bindings/cleanup-all",
                          headers=_HDRS, json={"dry_run": True})
        assert r2.json()["summary"] == d["summary"]


@pytest.mark.asyncio
async def test_auto_cleanup_flag_off_degrades(app_on):
    """开关关：活债跳过（升级会静默失效=行为改变，禁做），压制清除照常
    （账号层压制与开关无关，删了行为也不变）。"""
    from src.integrations.account_registry import get_account_registry
    get_account_registry().upsert(
        "telegram", "acct1", meta={"persona_id": "lin_xiaoyu"}, merge_meta=True)
    pm = PersonaManager.get_instance()
    pm.bind_chat_persona_by_profile_id("111", "chen_mo")   # 压制
    pm.bind_chat_persona_by_profile_id("222", "chen_mo")   # 活
    app_on.state.inbox_store = _FakeStore({
        "111": [_row("telegram", "acct1", "111")],
        "222": [_row("telegram", "acct9", "222")],
    })
    cm = app_on.state.config_manager
    cm.config["inbox"]["persona_conv_override"]["enabled"] = False
    try:
        async with _client(app_on) as c:
            r = await c.post("/api/persona/legacy-bindings/cleanup-all",
                             headers=_HDRS, json={"dry_run": False})
            d = r.json()
            assert d["enabled"] is False
            by_key = {it["key"]: it for it in d["results"]}
            assert by_key["111"]["decision"] == "remove"
            assert (by_key["222"]["decision"], by_key["222"]["reason"]) == ("skip", "disabled")
    finally:
        cm.config["inbox"]["persona_conv_override"]["enabled"] = True
    assert pm.has_chat_binding("111") is False
    assert pm.get_chat_binding_ref("222") == "chen_mo"
    assert pm.get_chat_binding_ref(
        conv_binding_key("telegram", "acct9", "222")) == ""


@pytest.mark.asyncio
async def test_auto_cleanup_variant_key_cascade(app_on):
    """同 peer 两条变体键（777 / line_rpa:777 反查到同一落点）：排序在前者
    升级落覆写，在后者被 planned 集判压制清除——真跑与 dry_run 决策一致。"""
    pm = PersonaManager.get_instance()
    pm.bind_chat_persona_by_profile_id("777", "chen_mo")
    pm.bind_chat_persona_by_profile_id("line_rpa:777", "lin_xiaoyu")
    rows = [_row("line", "a", "777")]
    app_on.state.inbox_store = _FakeStore({"777": rows, "line_rpa:777": rows})

    async with _client(app_on) as c:
        plan = (await c.post("/api/persona/legacy-bindings/cleanup-all",
                             headers=_HDRS, json={"dry_run": True})).json()
        run = (await c.post("/api/persona/legacy-bindings/cleanup-all",
                            headers=_HDRS, json={"dry_run": False})).json()
    for d in (plan, run):
        by_key = {it["key"]: it for it in d["results"]}
        assert by_key["777"]["decision"] == "upgrade"           # 排序在前，先落
        assert (by_key["line_rpa:777"]["decision"],
                by_key["line_rpa:777"]["reason"]) == ("remove", "suppressed")
    assert pm.get_chat_binding_ref(
        conv_binding_key("line", "a", "777")) == "chen_mo"      # 先处理者的 pid 赢
    assert pm.has_chat_binding("777") is False
    assert pm.has_chat_binding("line_rpa:777") is False


@pytest.mark.asyncio
async def test_manual_upgrade_keeps_existing_conv_override(app_on):
    """手动升级不踩既有会话覆写：覆写是比 legacy 更新的显式意图。"""
    pm = PersonaManager.get_instance()
    ck_acct1 = conv_binding_key("telegram", "acct1", "777")
    pm.bind_chat_persona_by_profile_id(ck_acct1, "lin_xiaoyu")  # 运营刚设的覆写
    pm.bind_chat_persona_by_profile_id("777", "chen_mo")        # 旧 legacy 债
    app_on.state.inbox_store = _FakeStore({
        "777": [_row("telegram", "acct1", "777"),
                _row("telegram", "acct9", "777")],
    })
    async with _client(app_on) as c:
        r = await c.post("/api/persona/legacy-bindings/cleanup", headers=_HDRS,
                         json={"key": "777", "action": "upgrade"})
        d = r.json()
        assert r.status_code == 200 and d["upgraded_count"] == 1
    assert pm.get_chat_binding_ref(ck_acct1) == "lin_xiaoyu"    # 没被踩
    assert pm.get_chat_binding_ref(
        conv_binding_key("telegram", "acct9", "777")) == "chen_mo"
    assert pm.has_chat_binding("777") is False


# ── 5. Messenger RPA 托管键隔离 + 债水位 ─────────────────────────────────────
# mrpa 的 per-chat 覆写以 RPA 自己的 SQLite 为 SSOT、启动回灌进 PM，键是
# mrpa_chat_cid 合成的纯数字（与 TG id 形状相同）。这些键不是债：PM 侧删除
# 会被回灌，且 runner 对有绑定的 chat 清空 account_persona_id 让绑定真赢。
# 清理链路（盘点/手动/批量）与债水位都必须把它们隔离出去。


class _FakeMrpaStore:
    def __init__(self, names):
        self._names = names

    def list_chat_persona_overrides(self):
        return [{"chat_name": n, "reply_profile_id": "p1"} for n in self._names]


class _FakeMrpaCtx:
    account_id = "acct_m"

    def __init__(self, names, prefix="messenger_rpa"):
        self._names, self._prefix = names, prefix

    def state_store(self):
        return _FakeMrpaStore(self._names)

    def merged_config(self, merged):
        return {"chat_key_prefix": self._prefix}


class _FakeMrpaService:
    def __init__(self, names):
        class _Reg:
            def __init__(self, ctxs):
                self._ctxs = ctxs

            def all_contexts(self):
                return list(self._ctxs)

        self._account_registry = _Reg([_FakeMrpaCtx(names)])
        self._merged_cfg = {}


def _mrpa_cid(name):
    from src.integrations.messenger_rpa.state_store import mrpa_chat_cid
    return str(mrpa_chat_cid(name, "messenger_rpa"))


@pytest.mark.asyncio
async def test_rpa_managed_keys_isolated(app_on):
    """RPA 托管键：盘点标记不算债 / 手动处置 400 / 批量 skip 且真跑不删。"""
    pm = PersonaManager.get_instance()
    cid = _mrpa_cid("推广群A")
    pm.bind_chat_persona_by_profile_id(cid, "chen_mo")   # 回灌形态
    pm.bind_chat_persona_by_profile_id("777", "chen_mo")  # 普通债对照
    app_on.state.messenger_rpa_service = _FakeMrpaService(["推广群A"])
    app_on.state.inbox_store = _FakeStore()

    async with _client(app_on) as c:
        inv = (await c.get("/api/persona/legacy-bindings",
                           headers=_HDRS)).json()
        by_key = {it["key"]: it for it in inv["items"]}
        assert by_key[cid]["rpa_managed"] is True
        assert by_key["777"]["rpa_managed"] is False
        assert inv["total"] == 2 and inv["debt_total"] == 1

        # 手动处置被拒（SSOT 在 RPA 的 SQLite，PM 侧删除会被回灌）
        for action in ("remove", "upgrade"):
            r = await c.post("/api/persona/legacy-bindings/cleanup",
                             headers=_HDRS,
                             json={"key": cid, "action": action})
            assert r.status_code == 400, action

        # 批量：托管键 skip，普通债照常按策略走
        run = (await c.post("/api/persona/legacy-bindings/cleanup-all",
                            headers=_HDRS, json={"dry_run": False})).json()
        by_key = {it["key"]: it for it in run["results"]}
        assert (by_key[cid]["decision"], by_key[cid]["reason"]) == (
            "skip", "rpa_managed")
        assert by_key["777"]["decision"] == "skip"   # no_conversations

    assert pm.get_chat_binding_ref(cid) == "chen_mo"   # 托管键毫发无损


@pytest.mark.asyncio
async def test_rpa_managed_absent_service_degrades(app_on):
    """服务未注入（未启用 messenger RPA 的部署）→ 托管集为空，行为同旧。"""
    from src.web.routes.persona_routes import mrpa_managed_cids
    assert mrpa_managed_cids(app_on) == set()

    pm = PersonaManager.get_instance()
    pm.bind_chat_persona_by_profile_id("777", "chen_mo")
    app_on.state.inbox_store = _FakeStore()
    async with _client(app_on) as c:
        inv = (await c.get("/api/persona/legacy-bindings",
                           headers=_HDRS)).json()
        assert inv["debt_total"] == inv["total"] == 1
        assert inv["items"][0]["rpa_managed"] is False


@pytest.mark.asyncio
async def test_legacy_debt_snapshot_semantics(app_on):
    """债水位口径：conv 3 段键不算、RPA 托管键不算、reference+inline 并集去重。"""
    from src.web.routes.persona_routes import legacy_debt_snapshot

    pm = PersonaManager.get_instance()
    assert legacy_debt_snapshot(app_on) == 0

    pm.bind_chat_persona_by_profile_id("777", "chen_mo")           # 债
    pm.bind_chat_persona("888", {"name": "自定义客服"})              # 债（内联）
    pm.bind_chat_persona_by_profile_id(                             # 覆写键：不算
        conv_binding_key("telegram", "acct1", "555"), "chen_mo")
    cid = _mrpa_cid("推广群B")
    pm.bind_chat_persona_by_profile_id(cid, "chen_mo")              # 托管：不算
    app_on.state.messenger_rpa_service = _FakeMrpaService(["推广群B"])

    assert legacy_debt_snapshot(app_on) == 2
