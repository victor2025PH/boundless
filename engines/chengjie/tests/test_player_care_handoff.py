"""player_care B5：Messenger → WA/TG handoff（引流码消费 + 身份合并，手机号主键）。

    $env:PYTHONPATH=""; .\\.venv\\Scripts\\python.exe -m pytest tests\\test_player_care_handoff.py -q -p no:cacheprovider
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from domains.player_care import goal_templates as gt
from domains.player_care.gateway import LookupResult
from domains.player_care.handoff import (
    HANDOFF_CONTEXT_BLOCK, VIA_LOCAL, VIA_SOURCE_DB, PlayerHandoffService,
    get_handoff_service, resolve_handoff_cfg, set_handoff_service,
)
from domains.player_care.hooks import PlayerCareDomainHook
from domains.player_care.profile import STAGE_CHATTING, STAGE_REGISTERED, PlayerProfileService, set_profile_service
from src.contacts.handoff import HandoffTokenService
from src.contacts.models import CHANNEL_MESSENGER, CHANNEL_WHATSAPP, STAGE_HANDOFF_SENT, STAGE_LINE_ENGAGED
from src.contacts.store import ContactStore
from src.hooks.base import HookContext

PHONE = "639171234567"
WA_JID = f"{PHONE}@s.whatsapp.net"


@pytest.fixture(autouse=True)
def _clean():
    gt.unregister_goal_templates()
    set_profile_service(None)
    set_handoff_service(None)
    yield
    gt.unregister_goal_templates()
    set_profile_service(None)
    set_handoff_service(None)


@pytest.fixture
def local(tmp_path):
    s = ContactStore(tmp_path / "player" / "contacts.db")
    yield s
    s.close()


@pytest.fixture
def story(tmp_path):
    s = ContactStore(tmp_path / "story" / "contacts.db")
    yield s
    s.close()


def _issue_messenger_token(store: ContactStore, *, phone_attr: str = "", name: str = "Ana"):
    """story 侧：Messenger 身份 + 已发引流话术（HANDOFF_SENT）+ 可用 token。"""
    contact, ci, _ = store.ensure_channel_identity(
        channel=CHANNEL_MESSENGER, account_id="page-1", external_id="fb-ana", display_name=name,
    )
    if phone_attr:
        store.set_contact_attribute(contact.contact_id, "phone", phone_attr)
    tok = HandoffTokenService(store).issue(ci.channel_identity_id)
    j = store.get_journey_by_contact(contact.contact_id)
    store.update_journey(j.journey_id, funnel_stage=STAGE_HANDOFF_SENT)
    return contact, ci, tok


def _profile(store):
    return PlayerProfileService(store)


# ── 配置 ────────────────────────────────────────────────────────────────────

def test_cfg_defaults_and_overrides():
    c = resolve_handoff_cfg({})
    assert c == {"enabled": True, "source_db_path": "", "link_local_identity": True}
    c2 = resolve_handoff_cfg(SimpleNamespace(config={"player_care": {"handoff": {
        "enabled": False, "source_db_path": "../config/contacts.db", "link_local_identity": False}}}))
    assert c2 == {"enabled": False, "source_db_path": "../config/contacts.db", "link_local_identity": False}


def test_store_migration_adds_handoff_columns(local):
    row = local.upsert_player_profile(PHONE, handoff_source="messenger", handoff_at=5)
    assert row["handoff_source"] == "messenger" and row["handoff_at"] == 5 and row["handoff_via"] == ""


# ── 同库：真身份合并 ──────────────────────────────────────────────────────────

def test_local_token_merge_relinks_identity_and_writes_profile(local):
    contact, msg_ci, tok = _issue_messenger_token(local)
    svc = PlayerHandoffService(local)
    prof = _profile(local)
    res = svc.try_merge(text=f"hi po {tok.token}", platform=CHANNEL_WHATSAPP, account_id="wa-01",
                        external_id=WA_JID, phone=PHONE, profile=prof, now=1000.0)
    assert res and res["via"] == VIA_LOCAL and res["token"] == tok.token
    assert res["merged_contact_id"] == contact.contact_id and res["source_name"] == "Ana"
    # WA 身份并到了 Messenger 的 Contact
    wa_ci = local.get_ci_by_external(CHANNEL_WHATSAPP, "wa-01", WA_JID)
    assert wa_ci is not None and wa_ci.contact_id == contact.contact_id
    # token 已消费、漏斗推到 LINE_ENGAGED、有 handoff_consumed 事件
    t = local.get_token(tok.token)
    assert t.is_consumed and t.consumed_by_ci_id == wa_ci.channel_identity_id
    j = local.get_journey_by_contact(contact.contact_id)
    assert j.funnel_stage == STAGE_LINE_ENGAGED
    assert local.has_event_of_type(j.journey_id, "handoff_consumed")
    # 画像：手机号主键 + handoff 字段 + contact_id + 至少 chatting
    row = local.get_player_profile(PHONE)
    assert row["handoff_source"] == CHANNEL_MESSENGER and row["handoff_via"] == VIA_LOCAL
    assert row["handoff_contact_id"] == contact.contact_id and row["handoff_ci_id"] == msg_ci.channel_identity_id
    assert row["contact_id"] == contact.contact_id and row["stage"] == STAGE_CHATTING and row["handoff_at"] == 1000


def test_token_consumed_only_once(local):
    _, _, tok = _issue_messenger_token(local)
    svc = PlayerHandoffService(local)
    assert svc.try_merge(text=tok.token, platform="whatsapp", account_id="wa-01", external_id=WA_JID, phone=PHONE)
    assert svc.try_merge(text=tok.token, platform="whatsapp", account_id="wa-01", external_id="639998887777@s.whatsapp.net",
                         phone="639998887777") is None


def test_no_token_or_unknown_token_is_noop(local):
    svc = PlayerHandoffService(local)
    assert svc.try_merge(text="kumusta", platform="whatsapp", account_id="wa-01", external_id=WA_JID) is None
    assert svc.try_merge(text="code abc234 pls", platform="whatsapp", account_id="wa-01", external_id=WA_JID) is None
    assert local.get_ci_by_external("whatsapp", "wa-01", WA_JID) is None  # 没建垃圾身份


def test_stage_only_moves_forward(local):
    _, _, tok = _issue_messenger_token(local)
    prof = _profile(local)
    local.upsert_player_profile(PHONE, stage=STAGE_REGISTERED, phone_e164=PHONE)
    PlayerHandoffService(local).try_merge(text=tok.token, platform="whatsapp", account_id="wa-01",
                                          external_id=WA_JID, phone=PHONE, profile=prof)
    assert local.get_player_profile(PHONE)["stage"] == STAGE_REGISTERED


# ── 手机号主键：TG 没号 → 借 Messenger 留资的 phone 合并占位键 ─────────────────

def test_telegram_placeholder_rebinds_to_phone_from_source_lead(local):
    contact, _, tok = _issue_messenger_token(local, phone_attr="+63 917 123 4567")
    prof = _profile(local)
    # TG 先来一条没号的消息 → 占位键
    prof.record_inbound(key="telegram:555", text="hello", platform="telegram", account_id="tg-01", external_id="555")
    assert local.get_player_profile("telegram:555")
    res = PlayerHandoffService(local).try_merge(
        text=f"{tok.token}", platform="telegram", account_id="tg-01", external_id="555",
        phone="", profile=prof, profile_key="telegram:555")
    assert res["phone"] == PHONE and res["profile_key"] == PHONE
    assert local.get_player_profile("telegram:555") is None
    row = local.get_player_profile(PHONE)
    assert row["platform"] == "telegram" and row["inbound_count"] == 1 and row["handoff_contact_id"] == contact.contact_id


def test_telegram_without_any_phone_keeps_placeholder(local):
    _, _, tok = _issue_messenger_token(local)
    prof = _profile(local)
    res = PlayerHandoffService(local).try_merge(
        text=tok.token, platform="telegram", account_id="tg-01", external_id="555", profile=prof)
    assert res["phone"] == "" and res["profile_key"] == "telegram:555"
    assert local.get_player_profile("telegram:555")["handoff_via"] == VIA_LOCAL


# ── 跨库：story 实例 contacts.db 只消费 token + 回写漏斗，不 relink ────────────

def test_source_db_merge_records_link_without_relink(local, story):
    contact, msg_ci, tok = _issue_messenger_token(story, phone_attr=PHONE)
    prof = _profile(local)
    svc = PlayerHandoffService(local, source=story)
    res = svc.try_merge(text=f"eto po {tok.token}", platform="telegram", account_id="tg-01",
                        external_id="777", profile=prof, profile_key="")
    assert res["via"] == VIA_SOURCE_DB and res["merged_contact_id"] == ""
    assert res["phone"] == PHONE and res["profile_key"] == PHONE
    # story 库：token 消费方是复合串、漏斗到 LINE_ENGAGED；本库没建任何 channel identity
    t = story.get_token(tok.token)
    assert t.is_consumed and t.consumed_by_ci_id == "telegram:tg-01:777"
    assert story.get_journey_by_contact(contact.contact_id).funnel_stage == STAGE_LINE_ENGAGED
    assert local.count_channel_identities_by_channel() == {}
    row = local.get_player_profile(PHONE)
    assert row["handoff_via"] == VIA_SOURCE_DB and row["handoff_contact_id"] == contact.contact_id
    assert row["contact_id"] == ""


def test_local_store_checked_before_source(local, story):
    _, _, tok_local = _issue_messenger_token(local)
    _, _, tok_story = _issue_messenger_token(story)
    svc = PlayerHandoffService(local, source=story)
    assert svc.locate(f"{tok_story.token} {tok_local.token}")[1] == VIA_LOCAL
    assert svc.locate(tok_story.token)[1] == VIA_SOURCE_DB


def test_get_handoff_service_resolves_source_path_relative_to_config(tmp_path, local, story):
    cfg = SimpleNamespace(
        config={"player_care": {"handoff": {"source_db_path": "../story/contacts.db"}}},
        config_path=str(tmp_path / "player" / "config.yaml"),
    )
    prof = _profile(local)
    svc = get_handoff_service(cfg, prof)
    assert svc is not None and svc.local is local and svc.source is not None
    assert svc.source._db_path == story._db_path  # noqa: SLF001
    assert get_handoff_service(cfg, prof) is svc  # 同配置复用
    cfg2 = SimpleNamespace(config={"player_care": {"handoff": {"source_db_path": "../nope/contacts.db"}}},
                           config_path=cfg.config_path)
    assert get_handoff_service(cfg2, prof).source is None  # 不存在 → 只查本库
    assert get_handoff_service({"player_care": {"handoff": {"enabled": False}}}, prof) is None


# ── hook 接线 ────────────────────────────────────────────────────────────────

class _GW:
    configured = True

    def lookup(self, q, *, phone="", uid=""):
        return LookupResult(ok=True, found=False)


def _ctx(text, chat_id, platform="whatsapp", account="wa-01"):
    return HookContext(text=text, user_id=chat_id, chat_id=chat_id, reply_lang="tl",
                       user_context={}, extra={"platform": platform, "account_id": account})


@pytest.mark.asyncio
async def test_hook_handoff_merges_once_and_injects_context(local):
    contact, _, tok = _issue_messenger_token(local)
    prof = _profile(local)
    set_profile_service(prof)
    hook = PlayerCareDomainHook(config={}, gateway=_GW(), handoff=PlayerHandoffService(local))
    ctx = _ctx(f"Hi! {tok.token}", WA_JID)
    res = await hook.on_message_pre_process(ctx)
    assert res and HANDOFF_CONTEXT_BLOCK in res["_domain_context_block"]
    ho = ctx.user_context["_player_handoff"]
    assert ho["token"] == tok.token and ctx.user_context["contact_id"] == contact.contact_id
    assert ctx.user_context["_player_profile_key"] == PHONE
    row = local.get_player_profile(PHONE)
    assert row["handoff_token"] == tok.token and row["inbound_count"] == 1 and row["contact_id"] == contact.contact_id
    # 第二轮：同一 user_context 不再合并、不再注入
    res2 = await hook.on_message_pre_process(ctx.__class__(text="musta", user_id=WA_JID, chat_id=WA_JID,
                                                           reply_lang="tl", user_context=ctx.user_context,
                                                           extra=ctx.extra))
    assert res2 is None
    assert local.get_player_profile(PHONE)["inbound_count"] == 2


@pytest.mark.asyncio
async def test_hook_without_token_no_context_and_no_identity(local):
    prof = _profile(local)
    set_profile_service(prof)
    hook = PlayerCareDomainHook(config={}, gateway=_GW(), handoff=PlayerHandoffService(local))
    res = await hook.on_message_pre_process(_ctx("kumusta po", WA_JID))
    assert res is None
    assert local.count_channel_identities_by_channel() == {}


@pytest.mark.asyncio
async def test_hook_handoff_failure_is_swallowed(local):
    class _Boom(PlayerHandoffService):
        def try_merge(self, **kw):
            raise RuntimeError("db gone")

    set_profile_service(_profile(local))
    hook = PlayerCareDomainHook(config={}, gateway=_GW(), handoff=_Boom(local))
    assert await hook.on_message_pre_process(_ctx("abc234", WA_JID)) is None
