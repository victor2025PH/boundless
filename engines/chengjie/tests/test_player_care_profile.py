"""player_care B2：画像旁表 + 阶段机 + 日报。

锁定契约：
  - ContactStore.player_profiles / player_daily_stats 旁表：upsert / rebind 合并 / 按账号×阶段计数
  - 阶段只前进：new_friend → chatting（第 2 条入站）→ mentioned_game（对方自己聊到游戏）
    → registered（网关查到）→ depositing（事实里有充值）→ active（≥2 个自然日充值）
  - dormant：沉默超期由 sweep 置入，再来消息回到原阶段
  - hook 每轮都落库（不查网关也落），占位键拿到手机号后合并；数字闸命中计数
  - 日报按账号 × 阶段给快照 + 当日计数；config=None 的 hook 不落盘
"""
from __future__ import annotations

import pytest

from domains.player_care import profile as prof
from domains.player_care.gateway import LookupResult
from domains.player_care.hooks import PlayerCareDomainHook, _SAFE_LINE
from domains.player_care.profile import (
    STAGES,
    PlayerProfileService,
    advance,
    detect_deposit,
    get_profile_service,
    set_profile_service,
)
from src.contacts.store import ContactStore
from src.hooks.base import HookContext

DAY = 86400.0
T0 = 1_800_000_000.0  # 固定基准时间


@pytest.fixture
def store(tmp_path):
    s = ContactStore(tmp_path / "contacts.db")
    yield s
    s.close()


@pytest.fixture
def svc(store):
    s = PlayerProfileService(store, dormant_after_days=7, active_min_deposit_days=2)
    set_profile_service(s)
    yield s
    set_profile_service(None)


def _inbound(svc, key, text, *, now, facts=None, looked_up=False, round_kind="", phone="", acct="wa-01"):
    return svc.record_inbound(
        key=key, text=text, platform="whatsapp", account_id=acct, external_id=key,
        phone=phone, facts=facts, looked_up=looked_up, round_kind=round_kind, now=now,
    )


# ── 纯函数 ─────────────────────────────────────────────────────────────────

def test_stage_order_and_advance_is_monotonic():
    assert STAGES == ["new_friend", "chatting", "mentioned_game", "registered", "depositing", "active", "dormant"]
    assert advance("new_friend", "chatting") == "chatting"
    assert advance("registered", "chatting") == "registered"          # 不后退
    assert advance("chatting", "bogus") == "chatting"
    assert advance("bogus", "chatting") == "new_friend"
    assert advance("dormant", "chatting") == "chatting"               # 唤醒交给调用方给目标


@pytest.mark.parametrize("text,exp", [
    ("deposit: 500 PHP on 2026-09-19", True),
    ("last deposit 1,000; withdraw 200", True),
    ("充值 300 元", True),
    ("deposit: 0", False),
    ("balance: 1,250 PHP; platform: JILI, game: Super Ace", False),
    ("no deposits yet", False),
    ("", False),
])
def test_detect_deposit_conservative(text, exp):
    assert detect_deposit(text) is exp


# ── store 旁表 ─────────────────────────────────────────────────────────────

def test_store_upsert_and_games_roundtrip(store):
    row = store.upsert_player_profile("639171234567", phone_e164="639171234567", account_id="wa-01",
                                      games=[{"platform": "JILI", "game": "Super Ace"}], last_found=True)
    assert row["stage"] == "new_friend" and row["first_seen"] > 0
    assert row["games"] == [{"platform": "JILI", "game": "Super Ace"}] and row["last_found"] is True
    row2 = store.upsert_player_profile("639171234567", uid="U77", bogus_column="x")
    assert row2["uid"] == "U77" and row2["games"] == row["games"]
    assert store.get_player_profile_by_phone("639171234567")["profile_key"] == "639171234567"
    assert store.get_player_profile("") is None


def test_store_rebind_merges_placeholder_into_phone_key(store):
    store.upsert_player_profile("telegram:12345", account_id="tg-01", inbound_count=3, lookups=1,
                                stage="mentioned_game", stage_changed_at=100, first_seen=50, last_seen=100)
    # 目标不存在 → 直接改键
    assert store.rebind_player_profile("telegram:12345", "639171234567")["inbound_count"] == 3
    assert store.get_player_profile("telegram:12345") is None
    # 目标存在 → 计数相加、阶段取较新、时间取早/晚
    store.upsert_player_profile("whatsapp:639171234567", inbound_count=2, lookups=2, gate_hits=1,
                                stage="registered", stage_changed_at=200, first_seen=80, last_seen=300)
    merged = store.rebind_player_profile("whatsapp:639171234567", "639171234567")
    assert merged["inbound_count"] == 5 and merged["lookups"] == 3 and merged["gate_hits"] == 1
    assert merged["stage"] == "registered" and merged["first_seen"] == 50 and merged["last_seen"] == 300
    assert store.get_player_profile("whatsapp:639171234567") is None


def test_store_daily_counters_and_stage_counts(store):
    store.bump_player_daily("2026-09-20", "wa-01", inbound=1, lookups=1)
    store.bump_player_daily("2026-09-20", "wa-01", inbound=2, gate_hits=1, bogus=9)
    store.bump_player_daily("2026-09-20", "wa-01")  # 全零 → 不写
    rows = store.player_daily_stats("2026-09-20")
    assert rows == [{"day": "2026-09-20", "account_id": "wa-01", "inbound": 3, "lookups": 1, "found": 0,
                     "visible": 0, "gate_hits": 1, "new_profiles": 0, "stage_ups": 0}]
    store.upsert_player_profile("a", account_id="wa-01", stage="chatting")
    store.upsert_player_profile("b", account_id="wa-01", stage="chatting")
    store.upsert_player_profile("c", account_id="tg-01", stage="registered")
    assert store.count_player_profiles_by_stage() == {"wa-01": {"chatting": 2}, "tg-01": {"registered": 1}}
    assert sorted(r["profile_key"] for r in store.list_player_profiles(account_id="wa-01")) == ["a", "b"]
    assert [r["profile_key"] for r in store.list_player_profiles(stage="registered")] == ["c"]


# ── 阶段机 ─────────────────────────────────────────────────────────────────

def test_stage_machine_progression(svc):
    k = "639171234567"
    assert _inbound(svc, k, "hi", now=T0)["stage"] == "new_friend"
    assert _inbound(svc, k, "kamusta", now=T0 + 60)["stage"] == "chatting"
    assert _inbound(svc, k, "sobrang bored ako today", now=T0 + 120)["stage"] == "mentioned_game"
    found = {"found": True, "text": "balance: 1,250 PHP; platform: JILI, game: Super Ace",
             "games": [{"platform": "JILI", "game": "Super Ace"}], "error": ""}
    row = _inbound(svc, k, "balance ko?", now=T0 + 180, facts=found, looked_up=True, round_kind="visible")
    assert row["stage"] == "registered" and row["registered_at"] == int(T0 + 180)
    assert row["games"] == [{"platform": "JILI", "game": "Super Ace"}] and row["visible_hits"] == 1
    assert row["lookups"] == 1 and row["last_found"] is True
    dep = {"found": True, "text": "deposit: 500 PHP; platform: JILI, game: Super Ace", "games": [], "error": ""}
    row = _inbound(svc, k, "check deposit ko", now=T0 + 240, facts=dep, looked_up=True, round_kind="visible")
    assert row["stage"] == "depositing" and row["deposit_days"] == 1
    # 同一天再充值不算第二天
    row = _inbound(svc, k, "deposit?", now=T0 + 300, facts=dep, looked_up=True, round_kind="visible")
    assert row["stage"] == "depositing" and row["deposit_days"] == 1
    # 第二个自然日 → active
    row = _inbound(svc, k, "deposit?", now=T0 + DAY + 300, facts=dep, looked_up=True, round_kind="visible")
    assert row["stage"] == "active" and row["deposit_days"] == 2
    # 阶段不后退：随便聊一句仍 active
    assert _inbound(svc, k, "hehe", now=T0 + DAY + 400)["stage"] == "active"


def test_not_found_lookup_does_not_register(svc):
    k = "639171234567"
    miss = {"found": False, "text": "", "games": [], "error": "not_found"}
    row = _inbound(svc, k, "balance ko", now=T0, facts=miss, looked_up=True, round_kind="missing")
    assert row["stage"] == "new_friend" and row["lookups"] == 1 and row["last_found"] is False
    assert row["last_error"] == "not_found" and row["registered_at"] == 0
    # 上轮的 found 事实还在 user_context 里，但这轮没查 → 不算 found、不重复计数
    stale = {"found": True, "text": "balance: 1", "games": [], "error": ""}
    row = _inbound(svc, k, "ok", now=T0 + 10, facts=stale, looked_up=False)
    assert row["lookups"] == 1 and row["stage"] == "chatting"


def test_dormant_sweep_and_wake(svc):
    k = "639171234567"
    _inbound(svc, k, "hi", now=T0)
    _inbound(svc, k, "laro tayo", now=T0 + 60)
    assert svc.store.get_player_profile(k)["stage"] == "mentioned_game"
    assert svc.apply_dormancy(now=T0 + 6 * DAY) == 0
    assert svc.apply_dormancy(now=T0 + 8 * DAY) == 1
    row = svc.store.get_player_profile(k)
    assert row["stage"] == "dormant" and row["stage_before_dormant"] == "mentioned_game"
    assert svc.apply_dormancy(now=T0 + 9 * DAY) == 0  # 不重复
    row = _inbound(svc, k, "uy", now=T0 + 10 * DAY)
    assert row["stage"] == "mentioned_game" and row["stage_before_dormant"] == ""


def test_note_deposit_from_sync(svc):
    k = "639171234567"
    _inbound(svc, k, "hi", now=T0)
    assert svc.note_deposit("nope", now=T0) is None
    assert svc.note_deposit(k, now=T0 + 1)["stage"] == "depositing"
    assert svc.note_deposit(k, now=T0 + DAY + 1)["stage"] == "active"


# ── 日报 ───────────────────────────────────────────────────────────────────

def test_daily_report_by_account_and_stage(svc):
    _inbound(svc, "639170000001", "hi", now=T0, acct="wa-01")
    _inbound(svc, "639170000001", "hello", now=T0 + 1, acct="wa-01")
    _inbound(svc, "639170000002", "hi", now=T0, acct="wa-01")
    _inbound(svc, "telegram:555", "hi", now=T0, acct="tg-01")
    miss = {"found": False, "text": "", "games": [], "error": "timeout"}
    _inbound(svc, "telegram:555", "balance ko 09171234567", now=T0 + 2, acct="tg-01",
             facts=miss, looked_up=True, round_kind="missing")
    svc.record_gate_hit("telegram:555", "tg-01", now=T0 + 3)
    rep = svc.daily_report(prof.day_key(T0), now=T0 + 4)
    by = {r["account_id"]: r for r in rep["accounts"]}
    assert by["wa-01"]["contacts"] == 2 and by["wa-01"]["stages"]["chatting"] == 1
    assert by["wa-01"]["stages"]["new_friend"] == 1 and by["wa-01"]["inbound"] == 3
    assert by["wa-01"]["new_profiles"] == 2 and by["wa-01"]["stage_ups"] == 1
    assert by["tg-01"]["inbound"] == 2 and by["tg-01"]["lookups"] == 1 and by["tg-01"]["found"] == 0
    assert by["tg-01"]["gate_hits"] == 1 and by["tg-01"]["stages"]["chatting"] == 1
    assert rep["totals"]["contacts"] == 3 and rep["totals"]["inbound"] == 5
    txt = PlayerProfileService.render_daily_report(rep)
    assert "wa-01" in txt and "tg-01" in txt and "合计 | 3" in txt
    assert svc.store.get_player_profile("telegram:555")["gate_hits"] == 1


# ── hook 接入 ───────────────────────────────────────────────────────────────

class _FakeGW:
    def __init__(self, result, configured=True):
        self.result = result
        self.configured = configured

    def lookup(self, q, *, phone="", uid=""):
        return self.result


def _ctx(text, *, user_id="", chat_id="", uc=None, platform="telegram", account_id="tg-01"):
    return HookContext(text=text, user_id=user_id, chat_id=chat_id or user_id, reply_lang="tl",
                       user_context=uc if uc is not None else {},
                       extra={"platform": platform, "account_id": account_id})


@pytest.mark.asyncio
async def test_hook_persists_every_round_and_rebinds_placeholder_to_phone(svc):
    hook = PlayerCareDomainHook(gateway=_FakeGW(LookupResult(ok=True, found=False)))
    uc = {}
    # 第 1 轮：TG 数字 id，没手机号、没问账户 → 不打网关但落占位画像
    assert await hook.on_message_pre_process(_ctx("hello", user_id="12345", uc=uc)) is None
    assert uc["_player_profile_key"] == "telegram:12345"
    row = svc.store.get_player_profile("telegram:12345")
    assert row["stage"] == "new_friend" and row["account_id"] == "tg-01" and row["lookups"] == 0
    # 第 2 轮：报了手机号 → 合并到手机号键，查网关无资料
    await hook.on_message_pre_process(_ctx("number ko 09171234567, balance ko?", user_id="12345", uc=uc))
    assert uc["_player_profile_key"] == "639171234567"
    assert svc.store.get_player_profile("telegram:12345") is None
    row = svc.store.get_player_profile("639171234567")
    assert row["inbound_count"] == 2 and row["stage"] == "chatting" and row["lookups"] == 1
    assert row["phone_e164"] == "639171234567" and row["external_id"] == "12345"
    # 数字闸命中 → 计数
    ctx = _ctx("x", user_id="12345", uc=uc)
    assert await hook.on_reply_post_process("Meron kang 2,000 balance.", ctx) == _SAFE_LINE["tl"]
    assert svc.store.get_player_profile("639171234567")["gate_hits"] == 1
    assert svc.store.player_daily_stats(prof.day_key(row["last_seen"]))[0]["gate_hits"] == 1


@pytest.mark.asyncio
async def test_hook_found_facts_register_player(svc):
    found = LookupResult(ok=True, found=True, chatx_text="balance: 1,250 PHP; platform: JILI, game: Super Ace")
    hook = PlayerCareDomainHook(gateway=_FakeGW(found))
    uc = {}
    ctx = _ctx("bored ako", user_id="639171234567@s.whatsapp.net", uc=uc, platform="whatsapp", account_id="wa-01")
    inj = await hook.on_message_pre_process(ctx)
    assert "隐藏画像" in inj["_domain_context_block"]
    row = svc.store.get_player_profile("639171234567")
    assert row["stage"] == "registered" and row["games"] == [{"platform": "JILI", "game": "Super Ace"}]
    assert row["mentioned_game_at"] > 0 and row["visible_hits"] == 0 and row["last_found"] is True


@pytest.mark.asyncio
async def test_hook_without_config_does_not_touch_disk(tmp_path, monkeypatch):
    set_profile_service(None)
    monkeypatch.chdir(tmp_path)
    assert get_profile_service(None) is None
    assert get_profile_service({"player_care": {"profile": {"enabled": True}}}) is None
    hook = PlayerCareDomainHook(gateway=_FakeGW(LookupResult(ok=True, found=False)))
    uc = {}
    await hook.on_message_pre_process(_ctx("hello", user_id="1", uc=uc))
    assert "_player_profile_key" not in uc
    assert not list(tmp_path.glob("**/contacts.db*"))


def test_get_profile_service_resolves_db_next_to_config(tmp_path):
    set_profile_service(None)

    class _CM:
        config_path = tmp_path / "config.yaml"
        config = {"player_care": {"profile": {"dormant_after_days": 3}}}

    s = get_profile_service(_CM())
    assert s is not None and s.dormant_after_sec == 3 * DAY
    assert (tmp_path / "contacts.db").exists()
    assert get_profile_service(_CM()) is s  # 同配置复用
    s.store.close()
    set_profile_service(None)
    assert get_profile_service({"player_care": {"profile": {"enabled": False}}}) is None
