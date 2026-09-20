from src.integrations.wujie_player import inject_player_block, should_lookup


def test_should_lookup_requires_digits_and_flag():
    cfg = {"enabled": True, "url": "http://127.0.0.1", "key": "k"}
    assert should_lookup("UID 128891843", cfg) is True
    assert should_lookup("你好", cfg) is False
    assert should_lookup("UID 128891843", {"enabled": False, "url": "http://x", "key": "k"}) is False


def test_inject_writes_block(monkeypatch):
    ctx = {}
    cfg = {"player_gateway": {"enabled": True, "url": "http://example", "key": "k"}}

    def fake_fetch(_cfg, **_kwargs):
        return {"chatx_text": "【玩家后台资料】UID 1 余额 10"}

    monkeypatch.setattr("src.integrations.wujie_player.fetch_lookup", fake_fetch)
    inject_player_block(ctx, "我的UID 128891843", cfg)
    assert "UID 1" in ctx["_player_data_block"]


def test_inject_noop_without_digits():
    ctx = {"_player_data_block": "stale"}
    inject_player_block(ctx, "hello", {"player_gateway": {"enabled": True, "url": "http://x", "key": "k"}})
    assert "_player_data_block" not in ctx
