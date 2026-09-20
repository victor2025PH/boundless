"""跨平台档案（contacts.origin_profile）门禁：store 表 / 块渲染纯函数 / pill /
provider 热闸 / companion_context 注册 / skill_manager 注入 / ai_client 消费 /
P2 合并联动记忆合流桥。

全部离线（tmp_path 建 ContactStore，绝不碰仓库 config/）；skill_manager 侧走
轻量绑定（与 test_bazi_wiring 同模式，免全量 init）。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from src.contacts.origin_context import (
    build_origin_block,
    derive_origin_trail,
    invalidate_origin_cache,
    known_since_phrase,
    make_origin_provider,
    normalize_channel,
    origin_pill,
    resolve_contact_for_conversation,
)
from src.utils.companion_context import (
    reset_relationship_providers,
    resolve_origin_block,
    set_relationship_providers,
)

_SMcls = __import__(
    "src.skills.skill_manager", fromlist=["SkillManager"]
).SkillManager


@pytest.fixture(autouse=True)
def _clean_providers_and_cache():
    reset_relationship_providers()
    invalidate_origin_cache()
    yield
    reset_relationship_providers()
    invalidate_origin_cache()


def _mk_store(tmp_path):
    from src.contacts.store import ContactStore
    return ContactStore(db_path=tmp_path / "contacts_test.db")


def _ci(channel, *, direction="first_seen", linked_at=0, via="", name=""):
    return {
        "channel": channel, "direction": direction, "linked_at": linked_at,
        "linked_via": via, "display_name": name,
    }


# ── store：contact_profiles 表 ────────────────────────────────────────────────

def test_store_profile_roundtrip(tmp_path):
    st = _mk_store(tmp_path)
    assert st.get_contact_profile("nope") is None
    contact, ci, created = st.ensure_channel_identity(
        channel="telegram", account_id="default", external_id="10086",
        display_name="Bob")
    assert created
    ok = st.upsert_contact_profile(
        contact.contact_id,
        origin_channel="wechat", origin_label="广告线索", known_since="months",
        preferred_name="宝哥", prior_names={"wechat": "阿宝"},
        topics=["钓鱼装备", "换车"], background_note="在微信聊了半年",
        updated_by="tester")
    assert ok
    p = st.get_contact_profile(contact.contact_id)
    assert p["origin_channel"] == "wechat"
    assert p["prior_names"] == {"wechat": "阿宝"}
    assert p["topics"] == ["钓鱼装备", "换车"]
    assert p["ai_visible"] is True
    # 部分更新语义：None=保留旧值
    st.upsert_contact_profile(contact.contact_id, preferred_name="宝爷")
    p2 = st.get_contact_profile(contact.contact_id)
    assert p2["preferred_name"] == "宝爷"
    assert p2["origin_channel"] == "wechat"
    assert p2["topics"] == ["钓鱼装备", "换车"]
    # ai_visible 显式关
    st.upsert_contact_profile(contact.contact_id, ai_visible=False)
    assert st.get_contact_profile(contact.contact_id)["ai_visible"] is False


def test_store_profile_bad_json_degrades(tmp_path):
    st = _mk_store(tmp_path)
    contact, _ci, _ = st.ensure_channel_identity(
        channel="line", account_id="default", external_id="U1")
    st.upsert_contact_profile(contact.contact_id, origin_label="x")
    with st._lock:  # noqa: SLF001 — 故意写坏 JSON 验证读侧软降级
        st._conn.execute(
            "UPDATE contact_profiles SET topics_json='{bad', prior_names_json='[' "
            "WHERE contact_id=?", (contact.contact_id,))
        st._conn.commit()
    p = st.get_contact_profile(contact.contact_id)
    assert p["topics"] == [] and p["prior_names"] == {}


# ── 纯函数：normalize / trail / block / pill ─────────────────────────────────

def test_normalize_channel():
    assert normalize_channel("line_rpa") == "line"
    assert normalize_channel("Telegram") == "telegram"
    assert normalize_channel("") == ""


def test_known_since_phrase():
    assert known_since_phrase("months") == "认识几个月了"
    assert known_since_phrase("自由文本") == "自由文本"
    assert known_since_phrase("") == ""


def test_trail_dedup_and_first_seen_priority():
    trail = derive_origin_trail([
        _ci("line", direction="linked_from", linked_at=200, via="token"),
        _ci("messenger", direction="first_seen", linked_at=100, name="Bob"),
        _ci("line", direction="linked_from", linked_at=300),  # 同渠道去重
    ])
    assert [t["channel"] for t in trail] == ["messenger", "line"]
    assert trail[0]["is_origin"] and not trail[1]["is_origin"]
    assert trail[1]["via_label"] == "引流码"


def test_block_empty_when_nothing_to_say():
    assert build_origin_block(None, []) == ""
    # 单平台裸 CI（每个会话都有）不值得占 prompt
    assert build_origin_block(None, [_ci("telegram")]) == ""


def test_block_from_identities_only_two_platforms():
    """零输入自动叙事：仅凭 ≥2 平台身份即可渲染（关系进展自动结合的 P0 形态）。"""
    blk = build_origin_block(None, [
        _ci("messenger", direction="first_seen", linked_at=1750000000, name="Bob"),
        _ci("line", direction="linked_from", linked_at=1755000000, via="token", name="阿宝"),
    ], current_platform="line")
    assert "Messenger" in blk and "引流码" in blk and "LINE" in blk
    assert "跨平台背景" in blk
    assert "绝不编造" in blk  # 守则行永不裁
    # 当前平台（line）的昵称不重复注入；来源平台昵称要在
    assert "Bob" in blk and "阿宝" not in blk


def test_block_renders_profile_fields():
    prof = {
        "origin_channel": "wechat", "origin_label": "广告线索",
        "known_since": "halfyear", "preferred_name": "宝哥",
        "prior_names": {"wechat": "阿宝"},
        "topics": ["钓鱼装备", "儿子上学", "换车"],
        "background_note": "在微信聊了半年，关系不错",
        "ai_visible": True,
    }
    blk = build_origin_block(prof, [], current_platform="telegram")
    assert "微信" in blk and "广告线索" in blk and "认识半年以上" in blk
    assert "阿宝" in blk and "宝哥" in blk
    assert "钓鱼装备" in blk and "在微信聊了半年" in blk


def test_block_respects_ai_visible_off():
    prof = {"origin_channel": "wechat", "topics": ["a"], "ai_visible": False}
    assert build_origin_block(prof, []) == ""


def test_block_budget_cap_keeps_guard_line():
    prof = {
        "origin_channel": "wechat",
        "topics": [f"话题{i}" for i in range(8)],
        "background_note": "长" * 600,
        "ai_visible": True,
    }
    blk = build_origin_block(prof, [], max_chars=300)
    assert blk and len(blk) <= 300 + 40  # 行结构造成的少量余量
    assert "绝不编造" in blk


def test_pill_consistent_with_block():
    # 块为空 → pill 也空（防「pill 说有、展开却空」）
    assert origin_pill(None, []) == ""
    assert origin_pill({"ai_visible": False, "topics": ["x"]}, []) == ""
    trail = derive_origin_trail([
        _ci("messenger", direction="first_seen", linked_at=1),
        _ci("line", direction="linked_from", linked_at=2),
    ])
    assert origin_pill(None, trail) == "Messenger→LINE"
    assert origin_pill({"origin_channel": "wechat", "ai_visible": True}, []) == "微信"


# ── provider：热闸 / 解析 / 缓存失效 ─────────────────────────────────────────

def _provider_env(tmp_path, enabled=True):
    st = _mk_store(tmp_path)
    contact, _ci_row, _ = st.ensure_channel_identity(
        channel="telegram", account_id="default", external_id="10086",
        display_name="Bob")
    st.upsert_contact_profile(
        contact.contact_id, origin_channel="wechat",
        topics=["钓鱼"], background_note="老客户")
    cfg = SimpleNamespace(config={
        "contacts": {"origin_profile": {"enabled": enabled, "max_chars": 500}}})
    return st, contact, make_origin_provider(st, cfg), cfg


def test_provider_flag_off_returns_empty(tmp_path):
    _st, _c, provider, cfg = _provider_env(tmp_path, enabled=False)
    assert provider(channel="telegram", account_id="default", external_id="10086") == ""
    # 热开：改 config dict 即生效（provider 按次实读）
    cfg.config["contacts"]["origin_profile"]["enabled"] = True
    invalidate_origin_cache()
    assert "微信" in provider(
        channel="telegram", account_id="default", external_id="10086")


def test_provider_resolves_and_caches(tmp_path):
    st, contact, provider, _cfg = _provider_env(tmp_path)
    blk = provider(channel="telegram", account_id="default", external_id="10086")
    assert "微信" in blk and "钓鱼" in blk
    # 未失效前吃缓存：改档案后旧块仍在 → invalidate 后新值
    st.upsert_contact_profile(contact.contact_id, topics=["新话题"])
    assert "钓鱼" in provider(
        channel="telegram", account_id="default", external_id="10086")
    invalidate_origin_cache()
    assert "新话题" in provider(
        channel="telegram", account_id="default", external_id="10086")


def test_provider_unknown_conversation_empty(tmp_path):
    _st, _c, provider, _cfg = _provider_env(tmp_path)
    assert provider(channel="telegram", account_id="default", external_id="nobody") == ""


def test_resolve_contact_tries_normalized_channel(tmp_path):
    st = _mk_store(tmp_path)
    contact, _ci_row, _ = st.ensure_channel_identity(
        channel="line", account_id="default", external_id="U99")
    assert resolve_contact_for_conversation(
        st, platform="line_rpa", account_id="default", chat_key="U99",
    ) == contact.contact_id


# ── companion_context：provider 注册/回落 ───────────────────────────────────

def test_resolve_origin_block_without_provider_none():
    assert resolve_origin_block("default", "10086", channel="telegram") is None


def test_resolve_origin_block_with_provider(tmp_path):
    _st, _c, provider, _cfg = _provider_env(tmp_path)
    set_relationship_providers(origin_block_lookup=provider)
    blk = resolve_origin_block("default", "10086", channel="telegram")
    assert blk and "微信" in blk
    assert resolve_origin_block("default", "", channel="telegram") is None
    reset_relationship_providers()
    assert resolve_origin_block("default", "10086", channel="telegram") is None


# ── skill_manager 注入（轻量绑定） ───────────────────────────────────────────

class _SM:
    _inject_origin_context = _SMcls._inject_origin_context

    def __init__(self):
        self.logger = logging.getLogger("test_origin")


def test_inject_clears_stale_and_noop_without_provider():
    sm = _SM()
    ctx = {"_origin_block": "残留"}
    sm._inject_origin_context(ctx, platform="telegram",
                              account_id="default", chat_key="10086")
    assert "_origin_block" not in ctx


def test_inject_sets_block_with_provider(tmp_path):
    _st, _c, provider, _cfg = _provider_env(tmp_path)
    set_relationship_providers(origin_block_lookup=provider)
    sm = _SM()
    ctx = {}
    sm._inject_origin_context(ctx, platform="telegram",
                              account_id="default", chat_key="10086")
    assert "微信" in (ctx.get("_origin_block") or "")
    # 空 chat_key：只清残留不注入
    ctx2 = {"_origin_block": "残留"}
    sm._inject_origin_context(ctx2, platform="telegram",
                              account_id="default", chat_key="")
    assert "_origin_block" not in ctx2


# ── P2：合并联动记忆合流桥 ───────────────────────────────────────────────────

class _FakeInbox:
    def __init__(self, by_contact=None, by_external=None):
        self._bc = dict(by_contact or {})
        self._be = dict(by_external or {})

    def list_conversation_ids_for_contact(self, cid):
        return list(self._bc.get(cid, []))

    def find_conversation_ids_by_external(self, ch, acct, ext):
        return list(self._be.get((ch, acct, ext), []))


def test_conv_id_to_memory_pair_account_bucketing():
    from src.contacts.memory_merge_bridge import conv_id_to_memory_pair
    assert conv_id_to_memory_pair("telegram:default:10086") == ("telegram", "10086")
    assert conv_id_to_memory_pair("telegram::10086") == ("telegram", "10086")
    assert conv_id_to_memory_pair("line:acct9:U1") == ("line", "acct9:U1")
    # chat_key 可含冒号（只切前两段）
    assert conv_id_to_memory_pair("messenger:default:rpa:Bob") == ("messenger", "rpa:Bob")
    assert conv_id_to_memory_pair("bad") is None
    assert conv_id_to_memory_pair("") is None


def test_collect_pairs_dedup_and_dual_channel(tmp_path):
    from src.contacts.memory_merge_bridge import collect_contact_memory_pairs
    st = _mk_store(tmp_path)
    contact, _ci1, _ = st.ensure_channel_identity(
        channel="messenger", account_id="default", external_id="Bob")
    _c2, ci2, _ = st.ensure_channel_identity(
        channel="line", account_id="acct9", external_id="U1")
    st.relink_channel_identity(
        ci_id=ci2.channel_identity_id, new_contact_id=contact.contact_id,
        linked_via="manual", attribution_confidence=1.0)
    inbox = _FakeInbox(
        by_contact={contact.contact_id: ["messenger:default:Bob"]},
        by_external={
            ("messenger", "default", "Bob"): ["messenger:default:Bob"],  # 与回写重复→去重
            ("line", "acct9", "U1"): ["line:acct9:U1"],
        })
    pairs = collect_contact_memory_pairs(st, inbox, contact.contact_id)
    assert pairs == [("messenger", "Bob"), ("line", "acct9:U1")]
    # ci_ids 限定（拆分场景）：只看被拆身份
    only = collect_contact_memory_pairs(
        st, inbox, contact.contact_id, ci_ids=[ci2.channel_identity_id])
    assert only == [("line", "acct9:U1")]


def _bridge_env(tmp_path):
    from src.utils.cross_platform_identity import CrossPlatformIdentity
    from src.utils.episodic_memory_store import EpisodicMemoryStore
    st = _mk_store(tmp_path)
    cpi = CrossPlatformIdentity(tmp_path / "cpi.db")
    es = EpisodicMemoryStore(str(tmp_path / "epi.db"))
    contact, _ci1, _ = st.ensure_channel_identity(
        channel="messenger", account_id="default", external_id="Bob")
    _c2, ci2, _ = st.ensure_channel_identity(
        channel="line", account_id="default", external_id="U1")
    st.relink_channel_identity(
        ci_id=ci2.channel_identity_id, new_contact_id=contact.contact_id,
        linked_via="manual", attribution_confidence=1.0)
    inbox = _FakeInbox(by_external={
        ("messenger", "default", "Bob"): ["messenger:default:Bob"],
        ("line", "default", "U1"): ["line:default:U1"],
    })
    return st, cpi, es, contact, ci2, inbox


def test_merge_contact_memory_links_and_moves_rows(tmp_path):
    from src.contacts.memory_merge_bridge import merge_contact_memory
    from src.contacts.origin_context import origin_stats_snapshot
    st, cpi, es, contact, _ci2, inbox = _bridge_env(tmp_path)
    # 预置两侧独立 canonical 下的记忆
    es.add_fact(cpi.resolve("messenger", "Bob"), "喜欢钓鱼", category="general")
    es.add_fact(cpi.resolve("line", "U1"), "儿子上一年级", category="general")
    base_links = origin_stats_snapshot()["merge_links"]
    out = merge_contact_memory(
        contacts_store=st, inbox_store=inbox, cpi=cpi,
        episodic_store=es, contact_id=contact.contact_id)
    assert out["pairs"] == 2 and out["linked"] == 1
    assert out["rows_merged"] >= 1
    # 两侧 canonical 已合一，line 侧历史事实在共享键下可读
    canon = cpi.resolve("messenger", "Bob")
    assert cpi.resolve("line", "U1") == canon
    bullets = es.get_bullets_for_prompt(canon)
    assert "儿子上一年级" in bullets and "喜欢钓鱼" in bullets
    assert origin_stats_snapshot()["merge_links"] == base_links + 1
    # 幂等：重跑 already_linked 不再计数
    out2 = merge_contact_memory(
        contacts_store=st, inbox_store=inbox, cpi=cpi,
        episodic_store=es, contact_id=contact.contact_id)
    assert out2["linked"] == 0


def test_merge_contact_memory_single_identity_skips(tmp_path):
    from src.contacts.memory_merge_bridge import merge_contact_memory
    from src.utils.cross_platform_identity import CrossPlatformIdentity
    st = _mk_store(tmp_path)
    cpi = CrossPlatformIdentity(tmp_path / "cpi2.db")
    contact, _ci, _ = st.ensure_channel_identity(
        channel="telegram", account_id="default", external_id="10086")
    inbox = _FakeInbox(by_external={
        ("telegram", "default", "10086"): ["telegram:default:10086"]})
    out = merge_contact_memory(
        contacts_store=st, inbox_store=inbox, cpi=cpi,
        episodic_store=None, contact_id=contact.contact_id)
    assert out["linked"] == 0 and out["skipped"] == "single_identity"
    out2 = merge_contact_memory(
        contacts_store=st, inbox_store=inbox, cpi=None,
        episodic_store=None, contact_id=contact.contact_id)
    assert out2["skipped"] == "no_cpi"


def test_unlink_identity_memory_detaches_future_writes(tmp_path):
    from src.contacts.memory_merge_bridge import (
        merge_contact_memory, unlink_identity_memory,
    )
    st, cpi, es, contact, ci2, inbox = _bridge_env(tmp_path)
    merge_contact_memory(
        contacts_store=st, inbox_store=inbox, cpi=cpi,
        episodic_store=es, contact_id=contact.contact_id)
    assert cpi.resolve("line", "U1") == cpi.resolve("messenger", "Bob")
    out = unlink_identity_memory(
        contacts_store=st, inbox_store=inbox, cpi=cpi,
        contact_id=contact.contact_id, ci_id=ci2.channel_identity_id)
    assert out["unlinked"] == 1
    assert cpi.resolve("line", "U1") == "line:U1"  # 回独立 canonical


# ── P3：gateway 自动合并 post-merge 钩子（轻量绑定） ─────────────────────────

def test_gateway_post_merge_hook_mechanics():
    """钩子契约：set 后 fire 带 contact_id/source；未 set / 空 id / 回调抛异常
    均静默（绝不影响合并主流程）。token/heuristic 两个真实触发点由本机制承载。"""
    from src.contacts.gateway import ContactGateway
    calls = []

    class _GW:
        set_post_merge_hook = ContactGateway.set_post_merge_hook
        _fire_post_merge = ContactGateway._fire_post_merge

    gw = _GW()
    gw._fire_post_merge("c1", "token")          # 未注册 → 静默
    gw.set_post_merge_hook(
        lambda contact_id="", source="": calls.append((contact_id, source)))
    gw._fire_post_merge("c1", "token")
    gw._fire_post_merge("", "token")            # 空 id → 不触发
    assert calls == [("c1", "token")]

    def _boom(contact_id="", source=""):
        raise RuntimeError("boom")

    gw.set_post_merge_hook(_boom)
    gw._fire_post_merge("c2", "heuristic")      # 回调异常 → 吞掉不抛
    gw.set_post_merge_hook(None)
    gw._fire_post_merge("c3", "token")          # 显式卸载 → 静默


# ── ai_client 消费 ───────────────────────────────────────────────────────────

def test_ai_client_consumes_origin_block():
    from src.ai.ai_client import AIClient

    class _Cfg:
        config_path = None
        config = {"web_admin": {"site_name": "T"}, "ai": {}}

        def get_ai_config(self):
            return {}

    client = AIClient(_Cfg())
    out = client._build_context_prompt({
        "channel": "telegram",
        "_origin_block": "【跨平台背景】\n· 来源：最早来自 Messenger",
    })
    assert "跨平台背景" in out
    out2 = client._build_context_prompt({"channel": "telegram"})
    assert "跨平台背景" not in out2
