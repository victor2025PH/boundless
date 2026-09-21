"""player_care 域包（WA / TG 普通朋友 + 玩家网关只读事实）单测。

锁定契约：
  - 手机号归一：9… / 09… / 639… / +63 9… / WA JID → 639xxxxxxxxx；非菲律宾移动号 → 空
  - 会员号只在带 uid / member id 前缀时抽取，手机号样式不算
  - 网关客户端：默认关不发请求；401 / 超时 / bad json 收敛为 ok=False；404 = 通但没资料；
    成功取 chatx_text；同联系人 TTL 缓存
  - hook 明用 / 暗用 / 无资料 / 未知身份四分支的提示块与 user_context 画像
  - 数字闸：问账户那一轮回复里出现事实外 ≥3 位数字 → 安全句；日常 1–2 位数字放行
  - 域包可被 DomainLoader 加载，hook 类名正确
  - SkillManager.generate_inbox_draft 真的派发 pre / post hook（2026-09-20 接线）
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from domains.player_care.gateway import (
    HEADER_KEY,
    LookupResult,
    PlayerGateway,
    extract_games,
    extract_phone,
    extract_uid,
    normalize_ph_phone,
    numbers_not_in_facts,
    phone_variants,
    resolve_gateway_cfg,
)
from domains.player_care.hooks import ACCOUNT_QUERY_RE, PlayerCareDomainHook, _SAFE_LINE
from src.hooks.base import HookContext


# ── 归一 / 抽取 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,exp", [
    ("9171234567", "639171234567"),
    ("09171234567", "639171234567"),
    ("639171234567", "639171234567"),
    ("+63 917 123 4567", "639171234567"),
    ("0917-123-4567", "639171234567"),
    ("639171234567@s.whatsapp.net", "639171234567"),
    ("8123456", ""),          # 座机样式
    ("1234567890", ""),       # 不是 9 开头
    ("", ""),
    (None, ""),
])
def test_normalize_ph_phone(raw, exp):
    assert normalize_ph_phone(raw) == exp


def test_phone_variants():
    assert phone_variants("09171234567") == ("9171234567", "09171234567", "639171234567")
    assert phone_variants("abc") == ("", "", "")


def test_extract_phone_from_text():
    assert extract_phone("number ko 0917 123 4567 po") == "639171234567"
    assert extract_phone("+639171234567 yan") == "639171234567"
    assert extract_phone("wala akong number") == ""
    # 金额不是号码
    assert extract_phone("nag-deposit ako 9000") == ""


def test_extract_uid_requires_prefix():
    assert extract_uid("uid: A12345") == "A12345"
    assert extract_uid("member id 778899") == "778899"
    assert extract_uid("my account no. 55667788") == "55667788"
    assert extract_uid("balance ko 12345") == ""            # 无前缀
    assert extract_uid("id 09171234567") == ""              # 手机号样式不算
    assert extract_uid("") == ""


def test_numbers_not_in_facts():
    facts = "balance: 1,250.50 PHP; last deposit 500 on 2026-09-18"
    assert numbers_not_in_facts("Tinanong ko, 1,250.50 ang balance mo, 500 last deposit", facts) == []
    assert numbers_not_in_facts("balance mo 1250.50", facts) == []       # 千分位差异放行
    assert numbers_not_in_facts("mga 1,300 na yata", facts) == ["1300"]
    assert numbers_not_in_facts("2 araw lang, 3 games", "") == []         # 1–2 位日常数字放行
    assert numbers_not_in_facts("₱2,000 ang bonus mo", "") == ["2000"]


def test_extract_games():
    txt = "platform: JILI, game: Super Ace | platform: PG, game: Mahjong Ways; balance 120"
    assert extract_games(txt) == [
        {"platform": "JILI", "game": "Super Ace"},
        {"platform": "PG", "game": "Mahjong Ways"},
    ]
    assert extract_games("游戏：Fortune Gems") == [{"platform": "", "game": "Fortune Gems"}]
    assert extract_games("balance 120") == []


def test_account_query_regex():
    assert ACCOUNT_QUERY_RE.search("pwede mo ba i-check balance ko")
    assert ACCOUNT_QUERY_RE.search("na-deposit ko kanina hindi pumasok")
    assert ACCOUNT_QUERY_RE.search("我的余额多少")
    assert not ACCOUNT_QUERY_RE.search("boring dito sa office haha")
    assert not ACCOUNT_QUERY_RE.search("kumain ka na ba")


# ── 配置 ───────────────────────────────────────────────────────────────────

def test_resolve_gateway_cfg_defaults_off_and_env_key(monkeypatch):
    assert resolve_gateway_cfg({})["enabled"] is False
    monkeypatch.setenv("GATEWAY_KEY", "env-secret")
    cfg = resolve_gateway_cfg({"player_gateway": {"enabled": True, "url": "http://gw/"}})
    assert cfg == {
        "enabled": True, "url": "http://gw", "key": "env-secret", "key_env": "GATEWAY_KEY",
        "timeout_sec": 4.0, "cache_ttl_sec": 60.0, "lookup_path": "/lookup",
    }
    cfg2 = resolve_gateway_cfg({"player_gateway": {"enabled": True, "url": "http://gw", "key": "cfg-key", "timeout_sec": -1}})
    assert cfg2["key"] == "cfg-key" and cfg2["timeout_sec"] == 4.0

    class _CM:  # ConfigManager 形态
        config = {"player_gateway": {"enabled": True, "url": "http://gw", "key": "k"}}
    assert resolve_gateway_cfg(_CM())["key"] == "k"


# ── 网关客户端 ─────────────────────────────────────────────────────────────

def _gw(transport, **over):
    cfg = {"enabled": True, "url": "http://gw", "key": "k", "timeout_sec": 1, "cache_ttl_sec": 60, "lookup_path": "/lookup"}
    cfg.update(over)
    return PlayerGateway(cfg, transport=transport)


def test_gateway_not_configured_never_calls_transport():
    calls = []
    gw = _gw(lambda *a: calls.append(a) or (200, b"{}"), enabled=False)
    r = gw.lookup("balance", phone="09171234567")
    assert r.configured is False and r.ok is False and r.usable is False
    assert calls == []


def test_gateway_success_payload_and_headers():
    seen = {}

    def _t(url, headers, body, timeout):
        seen.update(url=url, headers=headers, body=json.loads(body), timeout=timeout)
        return 200, json.dumps({"chatx_text": "balance: 1,250 PHP; platform: JILI, game: Super Ace"}).encode()

    gw = _gw(_t)
    r = gw.lookup("balance ko?", phone="0917 123 4567", uid="A1")
    assert r.ok and r.found and r.usable
    assert "1,250" in r.chatx_text
    assert seen["url"] == "http://gw/lookup"
    assert seen["headers"]["X-Gateway-Key"] == "k"
    assert seen["body"] == {"q": "balance ko?", "phone": "639171234567", "uid": "A1"}
    assert seen["timeout"] == 1
    assert gw.calls == 1


def test_gateway_cache_by_identity():
    n = {"c": 0}

    def _t(url, headers, body, timeout):
        n["c"] += 1
        return 200, b'{"chatx_text": "x"}'

    gw = _gw(_t)
    a = gw.lookup("q1", phone="09171234567")
    b = gw.lookup("q2 different text", phone="9171234567")   # 同人不同写法 → 命中缓存
    assert n["c"] == 1 and a.cached is False and b.cached is True
    gw.lookup("q", phone="09181234567")                        # 另一个人 → 新请求
    assert n["c"] == 2


@pytest.mark.parametrize("status,body,exp_ok,exp_found,exp_err", [
    (401, b"unauthorized", False, False, "http_401"),
    (403, b"", False, False, "http_403"),
    (404, b"", True, False, ""),
    (500, b"boom", False, False, "http_500"),
    (200, b"not json", False, False, "bad_json"),
    (200, b'{"chatx_text": ""}', True, False, ""),
    (200, b'{"ok": false, "error": "no player", "chatx_text": "x"}', True, False, "no player"),
])
def test_gateway_error_shapes(status, body, exp_ok, exp_found, exp_err):
    gw = _gw(lambda *a: (status, body))
    r = gw.lookup("q", phone="09171234567")
    assert (r.ok, r.found, r.error) == (exp_ok, exp_found, exp_err)
    assert r.usable is False


def test_gateway_timeout_is_swallowed():
    import socket

    def _t(*a):
        raise socket.timeout("timed out")

    r = _gw(_t).lookup("q", phone="09171234567")
    assert r.ok is False and r.error == "timeout" and r.usable is False


# ── hook ───────────────────────────────────────────────────────────────────

class _FakeGW:
    def __init__(self, result, configured=True):
        self.result = result
        self.configured = configured
        self.calls = []

    def lookup(self, q, *, phone="", uid=""):
        self.calls.append({"q": q, "phone": phone, "uid": uid})
        return self.result


def _ctx(text, chat_id="", history=None, reply_lang="tl", uc=None):
    uc = uc if uc is not None else {}
    if history is not None:
        uc["_conversation_history"] = history
    return HookContext(text=text, user_id=chat_id, chat_id=chat_id, reply_lang=reply_lang, user_context=uc)


FOUND = LookupResult(ok=True, found=True, chatx_text="balance: 1,250 PHP; platform: JILI, game: Super Ace")
MISSING = LookupResult(ok=True, found=False)


@pytest.mark.asyncio
async def test_hook_visible_when_asking_account_with_wa_phone():
    gw = _FakeGW(FOUND)
    hook = PlayerCareDomainHook(gateway=gw)
    ctx = _ctx("pwede mo ba i-check balance ko?", chat_id="639171234567@s.whatsapp.net")
    inj = await hook.on_message_pre_process(ctx)
    assert gw.calls == [{"q": "pwede mo ba i-check balance ko?", "phone": "639171234567", "uid": ""}]
    blk = inj["_domain_context_block"]
    assert "只读事实" in blk and "1,250 PHP" in blk and "Super Ace" in blk
    facts = ctx.user_context["_player_facts"]
    assert facts["found"] is True and facts["phone"] == "639171234567"
    assert facts["games"] == [{"platform": "JILI", "game": "Super Ace"}]
    assert ctx.user_context["_player_facts_visible"] is True

    # 数字闸：照抄放行，编数字换安全句
    ok = await hook.on_reply_post_process("Tinanong ko, 1,250 PHP ang balance mo sa Super Ace.", ctx)
    assert ok.startswith("Tinanong ko")
    bad = await hook.on_reply_post_process("Mga 1,300 PHP yata, tapos may 500 bonus.", ctx)
    assert bad == _SAFE_LINE["tl"]
    assert ctx.user_context["_player_numeric_gate_hits"] == 1


@pytest.mark.asyncio
async def test_hook_with_real_gateway_sample_uses_structured_games_and_strips_risk():
    """真网关样例（脱敏）回放进 hook：明用块 = 朋友版事实 + 结构化游戏名，无风控/客服包装；
    暗用轮拿到 top_games；画像 facts 带 deposit / agent；数字闸放行事实与游戏名里的数字。"""
    raw = json.loads(_real_found_sample()["body"])
    res = LookupResult(ok=True, found=True, chatx_text=raw["chatx_text"], raw=raw, status=200)
    hook = PlayerCareDomainHook(gateway=_FakeGW(res))
    ctx = _ctx("余额还有多少？", chat_id="639171234567@s.whatsapp.net", reply_lang="zh")
    blk = (await hook.on_message_pre_process(ctx))["_domain_context_block"]
    assert "近期玩过：Fortune Gems 2（JILI）" in blk and "余额 100.93" in blk
    assert "同IP" not in blk and "风险" not in blk and "玩家后台资料" not in blk and "不要猜" not in blk
    facts = ctx.user_context["_player_facts"]
    assert facts["deposit"] is True and facts["agent"] == "***"
    assert facts["games"][0] == {"platform": "JILI", "game": "Fortune Gems 2"}
    ok = await hook.on_reply_post_process("余额 100.93，最近你玩的 Fortune Gems 2 和 Gates of Olympus 呀。", ctx)
    assert ok.startswith("余额")
    assert await hook.on_reply_post_process("余额大概 2,000 吧。", ctx) == _SAFE_LINE["zh"]

    # 暗用：没问账户 → 只给隐藏画像里的游戏名
    ctx2 = _ctx("好无聊啊", chat_id="639171234567@s.whatsapp.net", reply_lang="zh")
    hidden = (await PlayerCareDomainHook(gateway=_FakeGW(res)).on_message_pre_process(ctx2))["_domain_context_block"]
    assert "隐藏画像" in hidden and "Fortune Gems 2（JILI）" in hidden and "余额" not in hidden

    # 风控 / 人身 / 后台字段既不进提示词，也不进 user_context（画像只吃 _player_facts 这一份）
    for leak in ("same_ip", "same_device", "register_ip", "history_ips", "scripts", "credit_score", "ban_payout", "核身"):
        assert leak not in blk and leak not in hidden
        assert leak not in json.dumps(facts, ensure_ascii=False)
    assert set(facts) <= {"phone", "uid", "found", "text", "games", "ts", "error", "cached", "deposit", "agent"}


@pytest.mark.asyncio
async def test_hook_visible_block_tells_model_to_restate_in_reply_language():
    raw = json.loads(_real_found_sample()["body"])
    res = LookupResult(ok=True, found=True, chatx_text=raw["chatx_text"], raw=raw, status=200)
    for lang, marker in (("tl", "Taglish"), ("en", "英文")):
        ctx = _ctx("magkano pa balance ko?", chat_id="639171234567@s.whatsapp.net", reply_lang=lang)
        blk = (await PlayerCareDomainHook(gateway=_FakeGW(res)).on_message_pre_process(ctx))["_domain_context_block"]
        assert "事实是中文摘要" in blk and marker in blk and "原样照抄" in blk and "别把整段资料念出来" in blk
    ctx = _ctx("余额多少", chat_id="639171234567@s.whatsapp.net", reply_lang="zh")
    blk = (await PlayerCareDomainHook(gateway=_FakeGW(res)).on_message_pre_process(ctx))["_domain_context_block"]
    assert "事实是中文摘要" not in blk and "别把整段资料念出来" in blk


MULTI_BODY = {"ok": True, "multi": True, "query": {}, "phone_hits": {}, "players": [],
              "candidates": [{"uid": "128891843", "name": "Juan"}, {"uid": "128899999", "name": "Ana"}],
              "message": "这个手机号对应多个账号，请用下面的 UID 再查一次拿全档。"}


def test_gateway_multi_response_is_not_found_but_keeps_candidate_uids():
    def _tp(url, headers, body, timeout):
        return 200, json.dumps(MULTI_BODY).encode()

    gw = _gw(_tp)
    res = gw.lookup("balance", phone="09171234567")
    assert res.ok and not res.found and not res.usable and res.error == ""
    assert res.multi is True and res.candidates == ["128891843", "128899999"]
    assert extract_games(res) == []


@pytest.mark.asyncio
async def test_hook_multi_accounts_asks_which_uid_then_requeries_with_uid_only():
    """同号多账号：第一轮 ambiguous（不报数字、不列候选）；对方回一串命中候选的 UID → 只用 UID 复查 → 明用。"""
    multi = LookupResult(ok=True, found=False, raw=MULTI_BODY, status=200, multi=True,
                         candidates=["128891843", "128899999"])

    class _GW(_FakeGW):
        def lookup(self, q, *, phone="", uid=""):
            self.calls.append({"q": q, "phone": phone, "uid": uid})
            return FOUND if uid == "128891843" else multi

    gw = _GW(multi)
    hook = PlayerCareDomainHook(gateway=gw)
    uc = {}
    ctx = _ctx("check balance ko", chat_id="639171234567@s.whatsapp.net", uc=uc)
    blk = (await hook.on_message_pre_process(ctx))["_domain_context_block"]
    assert "不止一个账号" in blk and "128891843" not in blk
    assert uc["_player_facts_round"] == "ambiguous" and uc["_player_facts"]["error"] == "multi"
    assert uc["_player_facts"]["candidates"] == ["128891843", "128899999"]
    assert await hook.on_reply_post_process("Alin sa 128891843 o 128899999?", ctx) == _SAFE_LINE["tl"]
    assert (await hook.on_reply_post_process("Alin account mo ba, may dalawa e?", ctx)).startswith("Alin")

    # 对方回 UID（裸数字也认，但要完全命中候选；随手一串别的数字不认）
    ctx2 = _ctx("ito 128891843", chat_id="639171234567@s.whatsapp.net", uc=uc)
    blk2 = (await hook.on_message_pre_process(ctx2))["_domain_context_block"]
    assert gw.calls[-1] == {"q": "ito 128891843", "phone": "", "uid": "128891843"}
    assert "只读事实" in blk2 and uc["_player_facts_round"] == "visible" and uc["_player_facts"]["uid"] == "128891843"
    ctx3 = _ctx("ito 55555", chat_id="639171234567@s.whatsapp.net", uc={"_player_facts": dict(uc["_player_facts"], uid="", found=False, multi=True)})
    assert PlayerCareDomainHook.resolve_identity(ctx3)["uid"] == ""


@pytest.mark.asyncio
async def test_hook_missing_facts_says_not_found_and_gates_numbers():
    hook = PlayerCareDomainHook(gateway=_FakeGW(MISSING))
    ctx = _ctx("check mo balance ko, number ko 09171234567", reply_lang="en")
    inj = await hook.on_message_pre_process(ctx)
    assert "无结果" in inj["_domain_context_block"]
    assert ctx.user_context["_player_facts"]["found"] is False
    assert ctx.user_context["_player_facts_visible"] is False
    assert await hook.on_reply_post_process("Your balance is 2,000.", ctx) == _SAFE_LINE["en"]
    assert (await hook.on_reply_post_process("Couldn't check yet, I'll get back to you.", ctx)).startswith("Couldn't")


@pytest.mark.asyncio
async def test_hook_asks_account_without_identity_does_not_call_gateway_for_tg_id():
    gw = _FakeGW(FOUND)
    hook = PlayerCareDomainHook(gateway=gw)
    # Telegram 数字 id 归一失败 → 没身份；但问了账户 → q-only 查询 + 让人设问号码
    ctx = _ctx("balance ko?", chat_id="123456789")
    inj = await hook.on_message_pre_process(ctx)
    assert gw.calls and gw.calls[0]["phone"] == "" and gw.calls[0]["uid"] == ""
    assert "还不知道对方的手机号" in inj["_domain_context_block"]
    assert ctx.user_context["_player_facts_round"] == "need_identity"
    # 即使网关按 q 命中了，也不明用（没核对身份不给数字）
    assert "1,250" not in inj["_domain_context_block"]


@pytest.mark.asyncio
async def test_hook_hidden_profile_when_not_asking_account():
    gw = _FakeGW(FOUND)
    hook = PlayerCareDomainHook(gateway=gw)
    ctx = _ctx("boring dito sa office haha", chat_id="639171234567")
    inj = await hook.on_message_pre_process(ctx)
    blk = inj["_domain_context_block"]
    assert "隐藏画像" in blk and "Super Ace" in blk
    assert "1,250" not in blk                        # 暗用绝不带数字
    assert ctx.user_context["_player_facts_round"] == "hidden"
    # 暗用轮不启数字闸（日常数字放行）
    assert (await hook.on_reply_post_process("Haha same, 300 pesos lang ulam ko kanina.", ctx)).startswith("Haha")


@pytest.mark.asyncio
async def test_hook_no_identity_no_account_question_skips_gateway():
    gw = _FakeGW(FOUND)
    hook = PlayerCareDomainHook(gateway=gw)
    ctx = _ctx("kumain ka na ba", chat_id="123456789")
    assert await hook.on_message_pre_process(ctx) is None
    assert gw.calls == []


@pytest.mark.asyncio
async def test_hook_phone_from_history_and_uid_from_text():
    gw = _FakeGW(FOUND)
    hook = PlayerCareDomainHook(gateway=gw)
    hist = [{"role": "user", "content": "eto number ko 0917 123 4567"}, {"role": "assistant", "content": "sige"}]
    ctx = _ctx("uid: A778 — nag-deposit ako hindi pumasok", history=hist)
    await hook.on_message_pre_process(ctx)
    assert gw.calls[0]["phone"] == "639171234567" and gw.calls[0]["uid"] == "A778"


@pytest.mark.asyncio
async def test_hook_unconfigured_gateway_stays_honest():
    hook = PlayerCareDomainHook(gateway=_FakeGW(FOUND, configured=False))
    ctx = _ctx("balance ko?", chat_id="639171234567")
    inj = await hook.on_message_pre_process(ctx)
    assert "无结果" in inj["_domain_context_block"]
    assert await hook.on_reply_post_process("Balance mo 5,000.", ctx) == _SAFE_LINE["tl"]
    assert await hook.on_message_pre_process(_ctx("musta", chat_id="639171234567")) is None


def test_hook_escalation_line_is_empty_and_gateway_rebuilds_on_config_change():
    class _CM:
        config = {"player_gateway": {"enabled": True, "url": "http://gw", "key": "k"}}
    cm = _CM()
    hook = PlayerCareDomainHook(config=cm)
    assert hook.get_escalation_line() == ""
    g1 = hook.gateway()
    assert g1.configured
    assert hook.gateway() is g1
    cm.config["player_gateway"]["url"] = "http://gw2"
    g2 = hook.gateway()
    assert g2 is not g1 and g2.cfg["url"] == "http://gw2"


# ── 域包可加载 ──────────────────────────────────────────────────────────────

def test_domain_pack_loads_with_hook():
    from src.utils.domain_loader import DomainLoader, DomainPack
    root = Path(__file__).parent.parent
    domains_dir = root / "domains"
    manifest = yaml.safe_load((domains_dir / "player_care" / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == "player_care" and manifest["hooks"] is True and manifest["skills"] == []
    pack = DomainPack("player_care", domains_dir / "player_care", manifest)
    DomainLoader(domains_dir)._load_hooks(pack, None)
    assert pack.hook_class is not None and pack.hook_class.__name__ == "PlayerCareDomainHook"
    for rel in ("persona.yaml", "personas.example.yaml", "config/defaults.yaml",
                "prompts/system_prompt.txt", "kb/categories.yaml", "kb/seeds.yaml",
                "i18n/zh.yaml", "i18n/en.yaml"):
        assert (domains_dir / "player_care" / rel).exists(), rel
    persona = yaml.safe_load((domains_dir / "player_care" / "persona.yaml").read_text(encoding="utf-8"))
    assert "customer service" in persona["speaking"]["forbidden_phrases"]
    assert "sure win" in persona["speaking"]["forbidden_phrases"]


# ── SkillManager 接线：generate_inbox_draft 真的派发 pre / post hook ────────

async def _make_cm(tmp_path: Path):
    from src.utils.config_manager import ConfigManager
    cfg = {
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {"enabled": []},
        "intent": {"keywords": {}, "patterns": {}},
        "reply": {},
        "context_store": {"ttl_days": 30},
        "memory": {"enabled": False},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text("greeting: hi\n", encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text("channels: {}\n", encoding="utf-8")
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    await cm.load()
    return cm


@pytest.mark.asyncio
async def test_generate_inbox_draft_dispatches_domain_hooks(tmp_path):
    from src.hooks.registry import HookRegistry
    from src.skills.skill_manager import SkillManager

    cm = await _make_cm(tmp_path)
    seen = {}

    async def _gen(**kw):
        seen["block"] = str((kw["user_context"] or {}).get("_domain_context_block") or "")
        return "Mga 1,300 PHP yata."   # 编数字 → 应被 post hook 换掉

    ai = MagicMock()
    ai.generate_reply_with_intent = AsyncMock(side_effect=_gen)
    sm = SkillManager(cm, ai)

    reg = HookRegistry.get_instance()
    prev_hook, prev_dom = reg._hook, getattr(reg, "_domain_name", "")
    reg.register(PlayerCareDomainHook(gateway=_FakeGW(FOUND)), "player_care")
    try:
        out = await sm.generate_inbox_draft(
            text="pwede mo ba i-check balance ko?",
            chat_key="639171234567",
            platform="whatsapp",
            history=[{"role": "user", "content": "pwede mo ba i-check balance ko?"}],
        )
    finally:
        reg.register(prev_hook, prev_dom)
    assert out is not None
    assert "1,250 PHP" in seen["block"], "pre hook 的事实块必须进 LLM 的 user_context"
    assert out["reply"] == _SAFE_LINE["tl"], "post hook 数字闸必须拦下编造数字"
    # 每轮结束后事实块不残留在持久 user_context
    uc = sm._get_user_context("639171234567", account_id="")
    assert "_domain_context_block" not in uc
    assert uc["_player_facts"]["found"] is True


@pytest.mark.asyncio
async def test_process_message_dispatches_domain_hooks(tmp_path):
    """A 线（协议号 7×24 自动回复 = WA / TG companion_runtime 走的路）同样接了 pre / post hook。"""
    from src.hooks.registry import HookRegistry
    from src.skills.skill_manager import SkillManager, SmallTalkSkill

    cm = await _make_cm(tmp_path)
    cm.config["skills"]["cooldown"] = {"global": 0, "per_user": 0, "per_content": 0, "per_chat_user": 0}
    seen = {}

    async def _gen(**kw):
        seen["block"] = str((kw["user_context"] or {}).get("_domain_context_block") or "")
        return "Mga 1,300 PHP yata."

    ai = MagicMock()
    ai.generate_reply_with_intent = AsyncMock(side_effect=_gen)
    ai.embed = AsyncMock(return_value=[[0.01] * 16])
    sm = SkillManager(cm, ai)
    # A 线要有技能承接 direct_chat 才会走到 LLM（player_care 预设 skills.enabled: [small_talk]）
    sm.skills["small_talk"] = SmallTalkSkill(cm, sm.ai_client)

    reg = HookRegistry.get_instance()
    prev_hook, prev_dom = reg._hook, getattr(reg, "_domain_name", "")
    reg.register(PlayerCareDomainHook(gateway=_FakeGW(FOUND)), "player_care")
    try:
        out = await sm.process_message(
            "pwede mo ba i-check balance ko?", "639171234567",
            {"_trigger_path": "mention", "chat_id": 639171234567},
        )
    finally:
        reg.register(prev_hook, prev_dom)
    assert "1,250 PHP" in seen.get("block", ""), "A 线 pre hook 的事实块必须进 LLM 的 user_context"
    # A 线自己决定 reply_lang（Taglish 短句可能判成 en）→ 安全句跟随该语言即可
    assert out in _SAFE_LINE.values(), "A 线 post hook 数字闸必须拦下编造数字"
    assert "_domain_context_block" not in sm._get_user_context("639171234567")


# ── B1.5 前置：样例文件驱动的 /lookup 解析回放 + gateway_probe 自检 ──────────

_SAMPLES_PATH = Path(__file__).resolve().parent / "fixtures" / "player_care_lookup_samples.json"


def _load_samples():
    return json.loads(_SAMPLES_PATH.read_text(encoding="utf-8"))["samples"]


@pytest.mark.parametrize("sample", _load_samples(), ids=lambda s: s["name"])
def test_lookup_samples_file(sample):
    """样例文件里的每条 status/body 回放进 PlayerGateway → found / error / games / deposit 必须等于 expect。
    真网关样例到手后：python -m domains.player_care.gateway_probe --out tests/fixtures/player_care_lookup_samples.json
    覆盖本文件、改 expect，再调 extract_games / detect_deposit 直到本测试绿。"""
    from domains.player_care.gateway_probe import replay_sample

    assert replay_sample(sample) == sample["expect"]


def test_probe_redaction_keeps_json_and_kills_values():
    from domains.player_care.gateway_probe import redact_body, redact_raw, redact_text

    secrets = ["639171234567", "09171234567", "9171234567"]
    t = redact_text("phone 09171234567 balance 1,250.50 turnover 12345 2 araw", secrets)
    assert "1234567" not in t and "12345" not in t and "250" not in t
    assert "2 araw" in t                                # 1–2 位日常数字不动
    body = redact_body('{"ok": true, "key": "abc", "balance": 1250, "n": 3, "chatx_text": "uid 778899"}', secrets)
    d = json.loads(body)                                # 仍是合法 JSON
    assert d["key"] == "***" and d["balance"] == 100 and d["n"] == 3 and "778899" not in d["chatx_text"]
    # 人身字段值遮掉（保留非空）；手机号当键名也遮；IP 不走版本号保形
    assert redact_raw({"X-Gateway-Key": "s3cr3t", "agent": "A100", "agent_x": ""}) == {
        "X-Gateway-Key": "***", "agent": "***", "agent_x": ""}
    r = redact_raw({"phone_hits": {"639171234567": "128891843"}, "register_ip": "180.191.72.186"}, secrets)
    assert list(r["phone_hits"]) == ["*" * 12] and "128891843" not in json.dumps(r) and r["register_ip"] == "***"
    assert redact_text("reg 180.191.72.186 v1.2.3") == "reg 0.0.0.0 v1.2.3"
    assert redact_text("UID 128891843 / Juan99 / VIP0 / 正常；余额 1500") == "UID 100000000 / *** / VIP0 / 正常；余额 1000"
    # 日期 / 时间 / 版本号只是形态信息，保留；紧挨着的金额仍毁
    t = redact_text("last deposit: 2,500 on 2026-09-21 18:05:30 (app 3.14.159) uid 445566", secrets)
    assert "2026-09-21 18:05:30" in t and "3.14.159" in t and "2,500" not in t and "445566" not in t


def _real_found_sample():
    return next(s for s in _load_samples() if s["name"] == "found")


def test_real_gateway_found_sample_shape():
    """2026-09-21 真网关样例（已脱敏）：游戏在 players[].top_games，不在 chatx_text；
    agent 在 players[].agent；充值看 has_deposited / deposit_count；朋友版事实去掉包头包尾和风险尾巴。"""
    from domains.player_care.gateway import extract_agent, friend_facts_text, has_deposit
    from domains.player_care.profile import detect_deposit

    s = _real_found_sample()
    raw = json.loads(s["body"])
    assert s["status"] == 200 and raw["ok"] is True and raw["players"] and raw["chatx_text"]
    assert s["agent_fields"] and all(p.endswith(".agent") for p in s["agent_fields"])
    games = extract_games(raw)
    assert {"platform": "JILI", "game": "Fortune Gems 2"} in games
    assert all(g["platform"] and g["game"] for g in games)
    assert extract_games(raw["chatx_text"]) == []          # 文本里确实没游戏，必须走结构化
    assert extract_agent(raw) == "***"                      # 样例里遮掉了，但字段存在
    assert has_deposit(raw) is True                          # 第二个玩家 has_deposited
    assert has_deposit({"players": [raw["players"][0]]}) is False
    # 文本兜底：“充 100×3”算充过，“充 0×0”不算
    lines = [ln for ln in raw["chatx_text"].splitlines() if ln.startswith("UID")]
    assert detect_deposit(lines[0]) is False and detect_deposit(lines[1]) is True
    ft = friend_facts_text(raw["chatx_text"])
    assert "【" not in ft and "不要猜" not in ft and "风险" not in ft and "同IP" not in ft
    assert ft.count("\n") == 1 and "余额" in ft and "充 100×3" in ft


def test_real_gateway_not_found_and_bad_key_samples():
    by = {s["name"]: s for s in _load_samples()}
    assert by["not_found"]["status"] == 404 and by["not_found"]["expect"] == {
        "ok": True, "found": False, "error": "", "games": [], "deposit": False, "agent": False}
    assert by["bad_key"]["status"] == 401 and by["bad_key"]["expect"]["error"] == "http_401"
    assert json.loads(by["bad_key"]["body"])["error"] == "unauthorized"


def test_probe_find_agent_fields_recursive():
    from domains.player_care.gateway_probe import find_agent_fields

    raw = {"ok": True, "player": {"uid": 1, "agent_id": "A7"}, "meta": [{"upline": "U1"}, {"x": 1}], "代理号": "C9"}
    assert find_agent_fields(raw) == ["player.agent_id", "meta[0].upline", "代理号"]
    assert find_agent_fields({"ok": True}, "balance: 100\nAgent: A7\n代理: B8") == ["chatx_text:agent", "chatx_text:代理"]
    assert find_agent_fields({"ok": True, "chatx_text": "..."}) == []


def test_probe_runs_three_probes_without_leaking_key(tmp_path, capsys):
    from domains.player_care import gateway_probe as gp

    seen = []

    def _tp(url, headers, body, timeout):
        seen.append((json.loads(body.decode()), headers[HEADER_KEY]))
        if headers[HEADER_KEY] != "real-key":
            return 401, b'{"error":"unauthorized"}'
        if seen[-1][0].get("phone") == gp.NOT_FOUND_PHONE:
            return 404, b'{"error":"not found"}'
        return 200, json.dumps({"ok": True, "player": {"agent_id": "AG7"}, "chatx_text": FOUND.chatx_text}).encode()

    cfg = {"url": "http://gw", "key": "real-key", "key_env": "GATEWAY_KEY", "timeout_sec": 1, "lookup_path": "/lookup"}
    rep = gp.run_probes(cfg, phone="09171234567", transport=_tp)
    names = [s["name"] for s in rep["samples"]]
    assert names == ["found", "not_found", "bad_key"]
    assert [s["expect"]["found"] for s in rep["samples"]] == [True, False, False]
    assert rep["samples"][1]["status"] == 404 and rep["samples"][1]["expect"]["ok"] is True
    assert rep["samples"][2]["expect"]["error"] == "http_401"
    assert rep["samples"][0]["agent_fields"] == ["player.agent_id"] and "player" in rep["samples"][0]["raw_keys"]
    assert rep["samples"][1]["agent_fields"] == []
    assert rep["samples"][0]["expect"]["games"] == extract_games(FOUND.chatx_text)
    assert seen[0][0]["phone"] == "639171234567" and seen[2][1] == gp.BAD_KEY
    dumped = json.dumps(rep, ensure_ascii=False)
    assert "real-key" not in dumped and "639171234567" not in dumped and "1,250" not in dumped
    assert rep["key_present"] is True
    # 每条样例都自洽：写出去的 body 回放 == expect（测试 test_lookup_samples_file 的前提）
    for s in rep["samples"]:
        assert gp.replay_sample(s) == s["expect"]
    txt = gp.render_report(rep)
    assert "[found]" in txt and "[bad_key]" in txt and "real-key" not in txt


def test_probe_cli_refuses_without_key(monkeypatch, capsys):
    from domains.player_care import gateway_probe as gp

    monkeypatch.delenv("GATEWAY_KEY", raising=False)
    assert gp.main(["--url", "http://gw", "--phone", "09171234567"]) == 2
    assert "GATEWAY_KEY" in capsys.readouterr().err
