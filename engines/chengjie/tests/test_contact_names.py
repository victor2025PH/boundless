# -*- coding: utf-8 -*-
"""#155 人设归属层级：双向称呼按**联系人**存（2026-09-03）。

问题：「你称呼对方（爱称）/ 对方称呼你」此前长在人设档案里，可它们描述的不是
人设自己，是**这段客户关系**——同一人设服务一百个客户，不可能对每个人都叫
babe；改人设档会连带改掉另外九十九个。运营要么不敢填，要么填了全员共用。

本文件钉住的不变量：
  · 有会话 id → **只认联系人级**（Q-20 #178，2026-09-11：#155 的「逐字段回落
    人设级」让一百个客户共用一个 babe）；``call_peer`` 空 → 会话显示名或不称呼，
    ``peer_calls_you`` 空 → 空（对方叫你就是叫人设名）；人设级只在无会话 id 生效；
  · prompt 硬钉子与出站呼格守卫**同源**——两处不一致，守卫会拿人设的 babe 去
    「纠正」本该是联系人 honey 的正确文本；
  · 迁移幂等且不覆盖新层已有决定；迁移后生效爱称逐字节一致；
  · 方向永不反转（AI 用 babe 称客户，绝不把客户叫它的 baba 反过来用）。
"""
from __future__ import annotations

import pytest

from src.inbox import contact_names as cn
from src.inbox.store import InboxConversation, InboxStore

_CID = "telegram:acc:c1"
_PERSONA = {
    "id": "p1", "name": "小美", "role": "咖啡店店主",
    "names": {"call_peer": "babe", "peer_calls_you": "baba"},
}


@pytest.fixture
def store(tmp_path):
    cn._reset_for_tests()
    s = InboxStore(tmp_path / "inbox.db")
    s.upsert_conversation(InboxConversation(
        conversation_id=_CID, platform="telegram", account_id="acc",
        chat_key="c1", display_name="客户1", last_text="hi", last_ts=100.0))
    yield s
    s.close()
    cn._reset_for_tests()


# ══ 1. 读写与回落 ═══════════════════════════════════════════════════════════

def test_contact_level_overrides_persona_level(store):
    # Q-20：未设置 → **不**回落人设级 babe/baba；call_peer 用会话显示名、peer_calls_you 空
    assert cn.resolve_address_names(store, _CID, _PERSONA) == {
        "call_peer": "客户1", "peer_calls_you": ""}
    ctx = cn.resolve_address_context(store, _CID, _PERSONA)
    assert ctx["call_peer_source"] == "display" and ctx["peer_calls_you_source"] == ""
    assert ctx["self_name"] == "小美" and ctx["peer_display_name"] == "客户1"
    cn.set_contact_names(store, _CID, call_peer="honey")
    got = cn.resolve_address_names(store, _CID, _PERSONA)
    # 只覆写了 call_peer，另一格仍为空（不是人设级 baba）
    assert got == {"call_peer": "honey", "peer_calls_you": ""}
    assert cn.resolve_address_context(store, _CID, _PERSONA)["call_peer_source"] == "contact"


def test_display_name_fallback_is_filtered(store):
    """显示名是裸号码 / 等于 chat_key / 无字母 → 不称呼（call_peer 空）。"""
    assert cn.usable_display_name("David", "c1") == "David"
    assert cn.usable_display_name("c1", "c1") == ""
    assert cn.usable_display_name("+15551234567", "x") == ""
    assert cn.usable_display_name("8613800000000", "x") == ""
    assert cn.usable_display_name("🔥🔥", "x") == ""
    assert cn.usable_display_name("x" * 41, "y") == ""
    bare = "telegram:acc:c9"
    store.upsert_conversation(InboxConversation(
        conversation_id=bare, platform="telegram", account_id="acc",
        chat_key="c9", display_name="c9", last_text="hi", last_ts=90.0))
    assert cn.resolve_address_names(store, bare, _PERSONA) == {
        "call_peer": "", "peer_calls_you": ""}


def test_per_contact_isolation(store):
    """同一人设、两个客户，各叫各的——这正是 #155 要的能力。"""
    other = "telegram:acc:c2"
    store.upsert_conversation(InboxConversation(
        conversation_id=other, platform="telegram", account_id="acc",
        chat_key="c2", display_name="客户2", last_text="hi", last_ts=90.0))
    cn.set_contact_names(store, _CID, call_peer="honey")
    cn.set_contact_names(store, other, call_peer="sweetie")
    assert cn.resolve_address_names(store, _CID, _PERSONA)["call_peer"] == "honey"
    assert cn.resolve_address_names(store, other, _PERSONA)["call_peer"] == "sweetie"
    # 人设档一个字没动（这就是「改一个客户不影响另外九十九个」）
    assert _PERSONA["names"]["call_peer"] == "babe"


def test_explicit_clear_drops_pet_name(store):
    """传空串＝显式清除（运营改主意「这个客户不用爱称」必须做得到）。"""
    cn.set_contact_names(store, _CID, call_peer="honey")
    cn.set_contact_names(store, _CID, call_peer="")
    assert cn.get_contact_names(store, _CID)["call_peer"] == ""
    # Q-20：清掉之后就是没有爱称（显示名兜底），绝不回落人设级 babe
    got = cn.resolve_address_names(store, _CID, _PERSONA)["call_peer"]
    assert got == "客户1" and got != "babe"


def test_partial_update_keeps_other_field(store):
    cn.set_contact_names(store, _CID, call_peer="honey", peer_calls_you="darling")
    cn.set_contact_names(store, _CID, call_peer="sugar")   # 只动一格
    assert cn.get_contact_names(store, _CID) == {
        "call_peer": "sugar", "peer_calls_you": "darling"}


def test_long_value_truncated_and_soft_failures(store):
    cn.set_contact_names(store, _CID, call_peer="x" * 200)
    assert len(cn.get_contact_names(store, _CID)["call_peer"]) == cn.MAX_LEN
    # store 不可用/空 id/无人设 → 全空，绝不抛
    assert cn.get_contact_names(None, _CID) == {"call_peer": "", "peer_calls_you": ""}
    assert cn.set_contact_names(store, "", call_peer="x") is False
    assert cn.resolve_address_names(None, _CID, None) == {
        "call_peer": "", "peer_calls_you": ""}


def test_deleting_conversation_clears_contact_names(store):
    """删会话＝连称呼一起清（store 按「含 conversation_id 列的表」结构发现式
    清理，本表自动在内——否则同 chat_key 的新会话会继承旧客户的爱称）。"""
    cn.set_contact_names(store, _CID, call_peer="honey")
    store.delete_conversation_data(_CID, deleted_by="agent")
    assert cn.get_contact_names(store, _CID)["call_peer"] == ""


# ══ 2. prompt 注入（联系人级生效） ═══════════════════════════════════════════

def test_prompt_pin_uses_contact_level(store, monkeypatch):
    from src.utils.persona_manager import PersonaManager

    monkeypatch.setattr(
        "src.integrations.protocol_bridge.get_inbox_store", lambda: store)
    cn.set_contact_names(store, _CID, call_peer="honey")
    pm = PersonaManager.get_instance()

    blk = pm._format_persona_instructions(dict(_PERSONA), conversation_id=_CID)
    assert "honey" in blk and "称呼·硬约束" in blk
    assert "babe" not in blk          # 人设级值不得再出现（否则两个爱称打架）
    assert "baba" not in blk          # Q-20：peer_calls_you 未设 → 不回落人设级
    # compact 是生产主用格式，同样按联系人级
    cblk = pm._format_persona_compact(dict(_PERSONA), conversation_id=_CID)
    assert "honey" in cblk and "babe" not in cblk and "baba" not in cblk
    # 不传会话 id（预览/工具型调用）→ 纯人设级＝旧行为
    assert "babe" in pm._format_persona_instructions(dict(_PERSONA))


def test_prompt_pin_blank_contact_uses_display_name_not_persona_pet(store, monkeypatch):
    """Q-20 A（#178 VDUJX6/PQE9ZF 根因）：联系人级空 + 人设级 babe → 提示词里
    不得出现 babe；改为「对方的名字（显示名）」+ 明令不用爱称。"""
    from src.utils.persona_manager import PersonaManager

    monkeypatch.setattr(
        "src.integrations.protocol_bridge.get_inbox_store", lambda: store)
    pm = PersonaManager.get_instance()
    for fmt in (pm._format_persona_instructions, pm._format_persona_compact):
        blk = fmt(dict(_PERSONA), conversation_id=_CID)
        assert "babe" not in blk and "baba" not in blk
        assert "对方的名字（显示名）：「客户1」" in blk
        assert "没有设置爱称" in blk


def test_prompt_pin_direction_never_reversed(store, monkeypatch):
    """AI 用 call_peer 称客户，peer_calls_you 是客户叫 AI 的——绝不反向。"""
    from src.utils.persona_manager import PersonaManager

    monkeypatch.setattr(
        "src.integrations.protocol_bridge.get_inbox_store", lambda: store)
    cn.set_contact_names(store, _CID, call_peer="honey", peer_calls_you="papi")
    blk = PersonaManager.get_instance()._format_persona_instructions(
        dict(_PERSONA), conversation_id=_CID)
    assert "你对对方的称呼（爱称/昵称）：「honey」" in blk
    assert "对方对你的称呼：「papi」" in blk
    assert "绝不互换" in blk


def test_prompt_pin_absent_when_nothing_configured(store, monkeypatch):
    from src.utils.persona_manager import PersonaManager

    monkeypatch.setattr(
        "src.integrations.protocol_bridge.get_inbox_store", lambda: store)
    # 会话显示名不合格（裸 chat_key）且联系人级未配 → 整段钉子不出现
    noname = "telegram:acc:c7"
    store.upsert_conversation(InboxConversation(
        conversation_id=noname, platform="telegram", account_id="acc",
        chat_key="c7", display_name="c7", last_text="hi", last_ts=90.0))
    bare = {"id": "p2", "name": "A", "role": "r", "names": {"nickname": "小A"}}
    blk = PersonaManager.get_instance()._format_persona_instructions(
        bare, conversation_id=noname)
    assert "称呼·硬约束" not in blk


# ══ 3. 出站呼格守卫同源（不同源 = 守卫会「纠正」正确文本） ═══════════════════

def test_sendpoint_overlay_matches_prompt(store, monkeypatch):
    monkeypatch.setattr(
        "src.integrations.protocol_bridge.get_inbox_store", lambda: store)
    cn.set_contact_names(store, _CID, call_peer="honey")
    base = {"call_peer": "babe", "peer_calls_you": "baba", "self_names": ["小美"]}
    out = cn.overlay_sendpoint_names(base, "telegram", "acc", "c1")
    assert out["call_peer"] == "honey"          # 与 prompt 注入同源
    assert out["peer_calls_you"] == ""          # Q-20：未配 → 空，人设级 baba 不进守卫
    assert out["self_names"] == ["小美"]        # 其余键原样保留
    assert out["conversation_id"] == _CID and out["peer_display_name"] == "客户1"
    # 缺任一段（无会话上下文）→ 原样返回
    assert cn.overlay_sendpoint_names(base, "telegram", "acc", "")["call_peer"] \
        == "babe"


def test_guard_uses_contact_name_when_correcting(store, monkeypatch):
    """联系人叫 honey 时，守卫必须把客户的 papi 换成 honey，而不是人设的 babe。"""
    from src.ai.sendpoint_guard import sendpoint_vocative_pass

    monkeypatch.setattr(
        "src.integrations.protocol_bridge.get_inbox_store", lambda: store)
    cn.set_contact_names(store, _CID, call_peer="honey", peer_calls_you="papi")
    names = cn.overlay_sendpoint_names(
        {"call_peer": "babe", "peer_calls_you": "baba", "self_names": []},
        "telegram", "acc", "c1")
    out, meta = sendpoint_vocative_pass("Good morning papi!", names)
    assert "honey" in out and "papi" not in out
    assert meta["swap_hits"]


# ══ 4. 存量迁移 ═════════════════════════════════════════════════════════════

def test_migration_seeds_contact_defaults(store):
    n = cn.migrate_persona_names_to_contacts(
        store, "p1", _PERSONA["names"], conversation_ids=[_CID])
    assert n == 1
    assert cn.get_contact_names(store, _CID) == {
        "call_peer": "babe", "peer_calls_you": "baba"}
    # 迁移后生效值与迁移前逐字节一致（搬的是同一个值）
    assert cn.resolve_address_names(store, _CID, _PERSONA)["call_peer"] == "babe"


def test_migration_is_idempotent_and_never_overwrites(store):
    cn.set_contact_names(store, _CID, call_peer="honey")
    assert cn.migrate_persona_names_to_contacts(
        store, "p1", _PERSONA["names"], conversation_ids=[_CID]) == 0
    assert cn.get_contact_names(store, _CID)["call_peer"] == "honey"
    # 无称呼可迁 / 无会话 → 0，绝不抛
    assert cn.migrate_persona_names_to_contacts(
        store, "p1", {}, conversation_ids=[_CID]) == 0
    assert cn.migrate_persona_names_to_contacts(
        store, "p1", _PERSONA["names"], conversation_ids=[]) == 0


# ══ 5. 路由读写口 ═══════════════════════════════════════════════════════════

def _client(store):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from src.web.routes.unified_inbox_intel_profile_routes import (
        register_intel_profile_routes,
    )

    app = FastAPI()

    def api_auth(request: Request):
        return True

    register_intel_profile_routes(app, api_auth=api_auth)
    app.state.inbox_store = store
    return TestClient(app)


def test_address_names_api_roundtrip(store):
    c = _client(store)
    d = c.post("/api/unified-inbox/conv-meta/address-names",
               json={"conversation_id": _CID, "call_peer": "honey"}).json()
    assert d["ok"] is True and d["call_peer"] == "honey"
    g = c.get("/api/unified-inbox/conv-meta/address-names",
              params={"conversation_id": _CID}).json()
    assert g["call_peer"] == "honey"
    assert g["effective_call_peer"] == "honey"
    # 清除 → 生效值回落人设级（此处无人设解析 → 空，语义仍是「不再有联系人级值」）
    c.post("/api/unified-inbox/conv-meta/address-names",
           json={"conversation_id": _CID, "call_peer": ""})
    assert c.get("/api/unified-inbox/conv-meta/address-names",
                 params={"conversation_id": _CID}).json()["call_peer"] == ""


def test_address_names_api_requires_conversation_id(store):
    c = _client(store)
    assert c.get("/api/unified-inbox/conv-meta/address-names",
                 params={"conversation_id": ""}).status_code == 400
    assert c.post("/api/unified-inbox/conv-meta/address-names",
                  json={}).status_code == 400


# ══ 6. 人设页不再有这两格（#155 落点门禁） ═══════════════════════════════════

def test_persona_studio_no_longer_renders_the_two_fields():
    import pathlib

    repo = pathlib.Path(__file__).resolve().parents[1]
    html = (repo / "src" / "web" / "templates" / "personas.html").read_text(
        encoding="utf-8")
    assert 'id="pe-pf-name-callpeer"' not in html
    assert 'id="pe-pf-name-peercalls"' not in html
    # #178（0905 skuio 原图 1192）：#155 留下的「已改为按客户单独设置——请去
    # 收件箱…」说明条也删了——档案页里一段没输入项、只指路去别处的说明是噪音；
    # 去处由收件箱客户信息面板自身承担。两键随删，模板不得再引用。
    assert "psn_pf_name_address_moved" not in html
    assert "psn_pf_name_address" not in html
    # 存量值仍随保存原样回传（不带键会被后端当清空，抹掉回落默认）
    assert "'call_peer','peer_calls_you'" in html
