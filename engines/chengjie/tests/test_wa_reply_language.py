"""回复语言策略的离线测试。不连 ssh，不点真机，不调用模型接口。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.integrations.whatsapp_rpa.phone_ledger import PhoneLedger
from src.integrations.whatsapp_rpa.phone_loop import WaPhoneLoop
from src.integrations.whatsapp_rpa.phone_send import PhoneScreen
from src.integrations.whatsapp_rpa.reply_language import (
    CONTRACT,
    canonical_tag,
    detect_peer_language,
    load_registry,
    resolve_reply_policy,
)

PHONE = "+14155552671"
INDIA = "+919876543210"


def _entry(tag, **overrides):
    row = {
        "tag": tag,
        "display_name": tag,
        "direction": "ltr",
        "aliases": [],
        "script_pattern": "",
        "min_script_chars": 2,
        "offline_acceptance": False,
        "capabilities": {
            "model_generation": "unverified",
            "phone_screen_read": "unverified",
            "unicode_input": "unverified",
        },
    }
    row.update(overrides)
    return row


def _registry_with_extra():
    rows = [dict(item) for item in load_registry()]
    rows.append(_entry(
        "fil",
        display_name="Filipino",
        aliases=["菲律宾语", "filipino"],
    ))
    rows.append(_entry(
        "xx",
        display_name="Blocked",
        aliases=["blocked"],
        capabilities={
            "model_generation": "unsupported",
            "phone_screen_read": "unverified",
            "unicode_input": "unverified",
        },
    ))
    return rows


def test_aliases_variants_and_unknown_tags_do_not_collapse_to_zh():
    from src.ai.lang_policy import normalize_lang_code

    assert normalize_lang_code("zh-tw") == "zh"
    assert canonical_tag("zh-tw") == ""
    assert canonical_tag("zh-Hant") == "zh-Hant"
    assert canonical_tag("ZH-HANT") == "zh-Hant"
    assert canonical_tag("繁体中文") == "zh-Hant"
    assert canonical_tag("印地语") == "hi"
    assert canonical_tag("Hindi") == "hi"
    assert canonical_tag("हिन्दी") == "hi"
    assert canonical_tag("klingon") == ""
    assert canonical_tag("") == ""
    fixed = resolve_reply_policy("fixed", requested="印地语")
    assert fixed["ok"] is True
    assert fixed["resolved"] == "hi"
    assert fixed["source"] == "fixed"
    assert resolve_reply_policy("fixed", requested="klingon")["ok"] is False


def test_new_language_comes_from_registry_config_not_a_hindi_branch():
    rows = _registry_with_extra()
    decision = resolve_reply_policy("fixed", requested="菲律宾语", registry=rows)
    assert decision["resolved"] == "fil"
    assert decision["source"] == "fixed"
    blocked = resolve_reply_policy("fixed", requested="xx", registry=rows)
    assert blocked["ok"] is False
    assert blocked["reason"].startswith("capability_unsupported")
    assert "if tag == \"hi\"" not in Path(
        "src/integrations/whatsapp_rpa/reply_language.py"
    ).read_text(encoding="utf-8")


def test_fixed_language_ignores_chinese_text_and_indian_number():
    decision = resolve_reply_policy("fixed", requested="hi", peer_text="你好，在吗")
    assert decision["resolved"] == "hi"
    assert decision["source"] == "fixed"
    assert "91" not in json.dumps(decision)


def test_account_defaults_stay_on_their_own_side():
    left = resolve_reply_policy("account_default", requested="hi", peer_text="مرحبا")
    right = resolve_reply_policy("account_default", requested="ar", peer_text="नमस्ते")
    assert left["resolved"] == "hi" and left["source"] == "account_default"
    assert right["resolved"] == "ar" and right["direction"] == "rtl"
    assert left["resolved"] != right["resolved"]


def test_follow_peer_uses_strong_text_or_the_explicit_fallback():
    heard = resolve_reply_policy("follow_peer", requested="ar", peer_text="नमस्ते दोस्त")
    assert heard["resolved"] == "hi"
    assert heard["source"] == "peer_message"
    assert heard["requested"] == "ar"
    missed = resolve_reply_policy("follow_peer", requested="hi", peer_text="+919876543210")
    assert missed["resolved"] == "hi"
    assert missed["source"] == "fallback"
    refused = resolve_reply_policy("follow_peer", requested="", peer_text="???")
    assert refused["ok"] is False
    assert refused["reason"] == "peer_language_unresolved"
    assert refused["resolved"] == ""
    assert detect_peer_language("你好") == "zh"
    assert detect_peer_language("ok") == ""


def test_arabic_metadata_is_rtl_and_not_a_device_claim():
    entry = next(row for row in load_registry() if row["tag"] == "ar")
    assert entry["direction"] == "rtl"
    assert entry["offline_acceptance"] is False
    assert set(entry["capabilities"].values()) == {"unverified"}
    hi = next(row for row in load_registry() if row["tag"] == "hi")
    assert hi["offline_acceptance"] is True
    assert set(hi["capabilities"].values()) == {"unverified"}


def test_unconfigured_policy_does_not_force_a_language():
    decision = resolve_reply_policy("", requested="", peer_text="你好")
    assert decision["source"] == "legacy"
    assert decision["apply"] is False
    assert decision["resolved"] == ""


class _Quiet:
    def __init__(self):
        self.taps = 0
        self.urls = []

    async def open_url(self, url):
        self.urls.append(url)
        self.taps += 1

    async def screencap(self):
        return b""

    async def read_screen(self, png):
        return PhoneScreen()

    async def tap_send(self):
        self.taps += 1


def _loop(tmp_path, device, decide, policy, account="seat-a"):
    return WaPhoneLoop(
        account_id=account,
        inbox=SimpleNamespace(),
        device=device,
        decide_fn=decide,
        cfg={
            "enabled": True,
            "hourly_cap": 20,
            "daily_cap": 80,
            "reply_mode": "auto",
            "reply_language_policy": policy,
        },
        ledger=PhoneLedger(tmp_path / f"{account}.db"),
        persona_id=account,
        now_fn=lambda: 1_700_000_000.0,
    )


@pytest.mark.asyncio
async def test_two_personas_pass_their_own_language_and_do_not_send_on_failure(tmp_path):
    seen = []

    async def decide(**kwargs):
        seen.append(dict(kwargs))
        if kwargs["persona_id"] == "seat-b":
            raise RuntimeError("model down")
        return {"ok": False, "reply": ""}

    left = _loop(tmp_path, _Quiet(), decide, {"strategy": "fixed", "requested": "hi"}, "seat-a")
    right = _loop(tmp_path, _Quiet(), decide, {"strategy": "account_default", "requested": "ar"}, "seat-b")
    first = await left.handle_inbound(phone=INDIA, text="你好")
    second = await right.handle_inbound(phone=PHONE, text="hello there friend")
    assert first["reply"] == "silent"
    assert first["reply_language"]["resolved"] == "hi"
    assert left.device.taps == 0
    assert second["reason"] == "decide_failed"
    assert right.device.taps == 0
    assert seen[0]["reply_lang"] == "hi"
    assert seen[0]["persona_id"] == "seat-a"
    assert seen[1]["reply_lang"] == "ar"
    assert seen[1]["persona_id"] == "seat-b"
    assert "91" not in seen[0]["reply_lang"]


@pytest.mark.asyncio
async def test_unsupported_capability_and_unresolved_peer_do_not_send(tmp_path):
    device = _Quiet()

    async def decide(**kwargs):
        raise AssertionError(kwargs)

    blocked = _loop(
        tmp_path, device, decide,
        {"strategy": "fixed", "requested": "xx", "registry": _registry_with_extra()},
        "blocked",
    )
    result = await blocked.handle_inbound(phone=PHONE, text="你好")
    assert result["send"]["tapped"] is False
    assert result["reason"].startswith("capability_unsupported")
    missing = _loop(
        tmp_path, device, decide,
        {"strategy": "follow_peer", "requested": ""},
        "missing",
    )
    refused = await missing.handle_inbound(phone=INDIA, text="+919876543210")
    assert refused["reason"] == "peer_language_unresolved"
    assert device.taps == 0


@pytest.mark.asyncio
async def test_explicit_language_reaches_generate_reply_with_intent(tmp_path):
    captured = {}

    class AI:
        async def generate_reply_with_intent(self, user_message, intent, user_context, strategy_overrides=None):
            captured["context"] = dict(user_context)
            captured["message"] = user_message
            return ""

    class Skill:
        def __init__(self):
            self.ai_client = AI()

        def _recognize_intent(self, text):
            return "chat"

        def get_strategy_for_intent(self, intent, user_id):
            return {}, ""

    skill = Skill()
    app = SimpleNamespace(state=SimpleNamespace(
        skill_manager=skill,
        ai_client=skill.ai_client,
        inbox_store=None,
        config_manager=None,
    ))
    from src.integrations.whatsapp_rpa.phone_loop import decide_whatsapp

    result = await decide_whatsapp(
        app, account_id="seat-a", phone=INDIA, last_inbound="你好，在吗",
        persona_id="persona-a", reply_lang="印地语",
        self_name="甲", peer_names=["乙"],
    )
    assert result["ok"] is False
    assert captured["context"]["reply_lang"] == "hi"
    assert captured["context"]["account_persona_id"] == "persona-a"
    assert captured["message"] == "你好，在吗"
    empty = await decide_whatsapp(
        app, account_id="seat-a", phone=INDIA, last_inbound="",
        persona_id="persona-a", reply_lang="hi",
    )
    assert empty["ok"] is False
    assert empty.get("reply_lang", "") in {"", "hi"}


@pytest.mark.asyncio
async def test_http_decide_refuses_a_forced_language_without_posting():
    from src.integrations.whatsapp_rpa.phone_acceptance import make_http_decide

    def post(*_args, **_kwargs):
        raise AssertionError("http decide must not be called")

    decide = make_http_decide("http://127.0.0.1:9/decide", token="secret", post_json=post)
    result = await decide(
        account_id="seat-a", phone=INDIA, last_inbound="你好",
        persona_id="persona-a", reply_lang="hi",
    )
    assert result["ok"] is False
    assert result["reason"] == "http_reply_language_unsupported"


def test_cli_contract_examples_and_legacy_default():
    from scripts.wa_phone_loop_acceptance import (
        compile_reply_language_policies,
        parser,
    )

    def args(extra):
        return parser().parse_args([
            "pair-chat",
            "--data-dir", "data",
            "--evidence-dir", "evidence",
            "--config", "config.yaml",
            "--a-serial", "SERIAL_A", "--a-phone", INDIA, "--a-account", "seat-a",
            "--a-persona", "persona-a",
            "--b-serial", "SERIAL_B", "--b-phone", PHONE, "--b-account", "seat-b",
            "--b-persona", "persona-b",
            *extra,
        ])

    legacy = compile_reply_language_policies(args([]))
    assert legacy["a"]["strategy"] == ""
    assert legacy["b"]["strategy"] == ""
    fixed = compile_reply_language_policies(args(["--reply-language", "印地语"]))
    assert fixed["a"] == {"strategy": "fixed", "requested": "hi"}
    assert fixed["b"]["requested"] == "hi"
    accounts = compile_reply_language_policies(args([
        "--reply-language-strategy", "account_default",
        "--a-reply-language", "hi",
        "--b-reply-language", "阿拉伯语",
    ]))
    assert accounts["a"]["requested"] == "hi"
    assert accounts["b"]["requested"] == "ar"
    follow = compile_reply_language_policies(args([
        "--reply-language-strategy", "follow_peer",
        "--reply-language-fallback", "hi",
    ]))
    assert follow["a"]["strategy"] == "follow_peer"
    assert follow["a"]["requested"] == "hi"
    with pytest.raises(SystemExit):
        compile_reply_language_policies(args(["--reply-language", "klingon"]))
    with pytest.raises(SystemExit):
        compile_reply_language_policies(args([
            "--reply-language-strategy", "follow_peer",
        ]))
    http_args = args(["--reply-language", "hi", "--decide-url", "http://127.0.0.1:9/decide"])
    with pytest.raises(SystemExit):
        compile_reply_language_policies(http_args)
    help_text = parser().parse_args(["pair-chat", "--help"]).__dict__ if False else ""
    del help_text
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with pytest.raises(SystemExit) as caught:
        with redirect_stdout(buf):
            parser().parse_args(["pair-chat", "--help"])
    assert caught.value.code == 0
    text = buf.getvalue()
    assert "--reply-language" in text
    assert "wa-reply-language/1" in text
    assert "--a-reply-language" in text
    assert "--reply-language-fallback" in text


def test_capabilities_payload_matches_the_contract_file():
    from scripts.wa_phone_loop_acceptance import capabilities_report

    payload = capabilities_report()
    schema = json.loads(Path(
        "src/integrations/whatsapp_rpa/reply_language_contract.json"
    ).read_text(encoding="utf-8"))
    checked_in = json.loads(Path(
        "src/integrations/whatsapp_rpa/wa_reply_language_capabilities.json"
    ).read_text(encoding="utf-8"))
    assert payload == checked_in
    assert payload["schema_version"] == CONTRACT == schema["$id"]
    assert payload["flags"] == {
        "language": "--reply-language",
        "strategy": "--reply-language-strategy",
        "fallback": "--reply-language-fallback",
        "account_a": "--a-reply-language",
        "account_b": "--b-reply-language",
    }
    assert payload["send"] is False
    assert payload["http_decide_language_policy"] is False
    assert payload["country_does_not_select_language"] is True
    assert payload["strategies"] == ["fixed", "account_default", "follow_peer"]
    assert payload["languages"] == [row["tag"] for row in load_registry()]
    assert payload["offline_acceptance"] == ["hi"]
    assert payload["device_verified"] == []
    assert payload["compatibility"]["reply_language_without_strategy"] == "fixed"
    tags = {row["tag"]: row for row in payload["language_details"]}
    assert list(tags) == payload["languages"]
    assert tags["hi"]["offline_acceptance"] is True
    assert tags["ar"]["direction"] == "rtl"
    assert tags["zh-Hant"]["offline_acceptance"] is False
    for row in payload["language_details"]:
        assert set(row["capabilities"]) == {
            "model_generation", "phone_screen_read", "unicode_input",
        }
        assert "supported" not in row["capabilities"].values()


class _DropAfterTap:
    def __init__(self):
        self.taps = 0
        self.screen = PhoneScreen(header=PHONE, composer="नमस्ते")

    async def screencap(self):
        if self.taps:
            raise ConnectionError("adb disconnected")
        return b"png"

    async def read_screen(self, png):
        return self.screen

    async def tap_send(self):
        self.taps += 1

    async def open_url(self, url):
        raise AssertionError(url)

    async def dismiss_keyboard(self):
        return None

    async def press_back(self):
        return None


@pytest.mark.asyncio
async def test_unknown_confirmation_is_not_retried(tmp_path):
    from src.integrations.whatsapp_rpa.phone_send import deliver_via_wa_me

    device = _DropAfterTap()
    device.open_timeout = 1
    device.open_interval = 0
    first = await deliver_via_wa_me(device, PHONE, "नमस्ते")
    assert first.status == "unknown"
    assert first.reason == "confirm_unread"
    assert device.taps == 1
    ledger = PhoneLedger(tmp_path / "unknown.db")
    loop = WaPhoneLoop(
        account_id="seat-a",
        inbox=SimpleNamespace(),
        device=device,
        decide_fn=_unused,
        cfg={"enabled": True, "hourly_cap": 20, "daily_cap": 80},
        ledger=ledger,
        now_fn=lambda: 1_700_000_000.0,
    )
    loop.ledger.reserve(
        client_msg_id="once", phone=PHONE, account_id="seat-a",
        source="reply", content_key="same", body="नमस्ते",
    )
    loop.ledger.finish("once", "unknown", error="confirm_unread")
    again = await loop.send_prepared(
        phone=PHONE, text="नमस्ते", source="reply", client_msg_id="twice",
    )
    assert again["tapped"] is False
    assert again["retry"] is False
    assert device.taps == 1


async def _unused(**kwargs):
    return {"ok": False, "reply": ""}
