"""无界玩家网关注入（智安 P0-3，2026-10-08 起：只认渠道核实的本人号、bind=False、金额脱敏）。"""
from src.integrations.wujie_player import inject_player_block, should_lookup

_ON = {"enabled": True, "url": "http://127.0.0.1", "key": "k", "visible_facts": True}
_WA = {"platform": "whatsapp", "chat_id": "639171234567@s.whatsapp.net"}


def test_should_lookup_requires_verified_identity_and_flags():
    assert should_lookup(_WA, _ON) is True
    # 正文里的数字 / UID 不再触发
    assert should_lookup({"platform": "telegram", "chat_id": "128891843"}, _ON) is False
    assert should_lookup({}, _ON) is False
    assert should_lookup(_WA, dict(_ON, enabled=False)) is False
    assert should_lookup(_WA, dict(_ON, visible_facts=False)) is False
    assert should_lookup(_WA, {k: v for k, v in _ON.items() if k != "visible_facts"}) is False


def test_inject_writes_redacted_block(monkeypatch):
    ctx = dict(_WA)
    seen = {}

    def fake_fetch(_cfg, **kwargs):
        seen.update(kwargs)
        return {"chatx_text": "【玩家后台资料】UID 1 余额 10 元，VIP 3"}

    monkeypatch.setattr("src.integrations.wujie_player.fetch_lookup", fake_fetch)
    inject_player_block(ctx, "我的UID 128891843", {"player_gateway": _ON})
    block = ctx["_player_data_block"]
    assert "VIP 3" in block and "10 元" not in block and "***" in block
    assert seen == {"phone": "639171234567"}


def test_inject_noop_without_verified_identity():
    ctx = {"_player_data_block": "stale", "platform": "messenger", "chat_id": "psid1"}
    inject_player_block(ctx, "uid 12345678 balance?", {"player_gateway": _ON})
    assert "_player_data_block" not in ctx
