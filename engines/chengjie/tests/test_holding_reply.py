"""L3 缓冲话术（holding_reply）单测。

锁定：
  - 纯决策：pick_holding_text 语言回落、should_send_holding 冷却
  - maybe_send_holding_reply：默认关早退 / owns_media 闸门 / 危机跳过 / 冷却 /
    已读+发送顺序 / 失败软降级；全程不抛
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.inbox import holding_reply
from src.inbox.holding_reply import (
    maybe_send_holding_reply,
    pick_holding_text,
    reset_cooldown,
    resolve_holding_cfg,
    should_send_holding,
)


@pytest.fixture(autouse=True)
def _clear_cooldown():
    reset_cooldown()
    yield
    reset_cooldown()


def _assistant(cfg: dict):
    return SimpleNamespace(
        config=SimpleNamespace(config=cfg),
        logger=SimpleNamespace(debug=lambda *a, **k: None,
                               info=lambda *a, **k: None,
                               warning=lambda *a, **k: None),
        _web_loop=None,
    )


# ── 纯决策 ──────────────────────────────────────────────

def test_resolve_cfg_missing_is_empty():
    assert resolve_holding_cfg({}) == {}
    assert resolve_holding_cfg(
        {"inbox": {"l2_autosend": {"holding": {"enabled": True}}}}
    ) == {"enabled": True}


def test_pick_text_language_fallback():
    assert pick_holding_text("zh") in holding_reply._DEFAULT_TEMPLATES["zh"]
    assert pick_holding_text("en") in holding_reply._DEFAULT_TEMPLATES["en"]
    # 未知语言 → 回落 en
    assert pick_holding_text("xx") in holding_reply._DEFAULT_TEMPLATES["en"]
    # zh-CN 归一到 zh
    assert pick_holding_text("zh-CN") in holding_reply._DEFAULT_TEMPLATES["zh"]


def test_pick_text_custom_templates_override():
    cfg = {"templates": {"zh": ["自定义缓冲一号"]}}
    assert pick_holding_text("zh", cfg) == "自定义缓冲一号"


def test_should_send_cooldown():
    assert should_send_holding("c1", now=1000.0, cooldown_sec=100.0) is True
    # 冷却窗口内 → False
    assert should_send_holding("c1", now=1050.0, cooldown_sec=100.0) is False
    # 冷却到期 → True
    assert should_send_holding("c1", now=1101.0, cooldown_sec=100.0) is True


def test_should_send_empty_conv_false():
    assert should_send_holding("", now=1.0) is False


# ── maybe_send_holding_reply（async） ────────────────────

def _run(coro):
    return asyncio.run(coro)


def test_disabled_returns_false():
    r = _run(maybe_send_holding_reply(
        _assistant({}), "telegram", "a", "c", "telegram:a:c"))
    assert r is False


def test_owns_media_false_returns_false(monkeypatch):
    cfg = {"inbox": {"l2_autosend": {"holding": {"enabled": True}}}}

    class _Orch:
        def owns_media(self, p, a):
            return False

    import src.integrations.account_orchestrator as _ao
    monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())
    r = _run(maybe_send_holding_reply(
        _assistant(cfg), "telegram", "a", "c", "telegram:a:c"))
    assert r is False


def test_crisis_skipped(monkeypatch):
    cfg = {"inbox": {"l2_autosend": {"holding": {"enabled": True}}}}
    reads = []

    class _Orch:
        def owns_media(self, p, a):
            return True

        async def mark_read(self, p, a, c):
            reads.append(c)
            return True

        async def send(self, p, a, c, t):
            raise AssertionError("危机场景不应发缓冲话术")

    import src.integrations.account_orchestrator as _ao
    monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())
    # 危机词命中 → 跳过缓冲（但不发话术）
    r = _run(maybe_send_holding_reply(
        _assistant(cfg), "telegram", "a", "c", "telegram:a:c",
        peer_text="我不想活了", lang="zh"))
    assert r is False
    snap = holding_reply.metrics_snapshot()
    assert snap["skipped_crisis"] >= 1


def test_marks_read_then_sends(monkeypatch):
    cfg = {"inbox": {"l2_autosend": {"holding": {"enabled": True}}}}
    events = []

    class _Orch:
        def owns_media(self, p, a):
            return True

        async def mark_read(self, p, a, c):
            events.append(("read", c))
            return True

        async def send(self, p, a, c, t):
            events.append(("send", t))
            return {"delivered": True}

    import src.integrations.account_orchestrator as _ao
    monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())
    r = _run(maybe_send_holding_reply(
        _assistant(cfg), "telegram", "a", "c", "telegram:a:c",
        peer_text="我生气了啊", lang="zh"))
    assert r is True
    # 顺序：先已读，后发送
    assert events[0] == ("read", "c")
    assert events[1][0] == "send"
    assert events[1][1] in holding_reply._DEFAULT_TEMPLATES["zh"]


def test_send_text_false_only_marks_read(monkeypatch):
    cfg = {"inbox": {"l2_autosend": {"holding": {
        "enabled": True, "send_text": False}}}}
    events = []

    class _Orch:
        def owns_media(self, p, a):
            return True

        async def mark_read(self, p, a, c):
            events.append(("read", c))
            return True

        async def send(self, p, a, c, t):
            raise AssertionError("send_text=false 不应发话术")

    import src.integrations.account_orchestrator as _ao
    monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())
    r = _run(maybe_send_holding_reply(
        _assistant(cfg), "telegram", "a", "c", "telegram:a:c",
        peer_text="嗯", lang="zh"))
    assert r is False
    assert events == [("read", "c")]


def test_cooldown_blocks_second_send(monkeypatch):
    cfg = {"inbox": {"l2_autosend": {"holding": {"enabled": True}}}}
    sends = []

    class _Orch:
        def owns_media(self, p, a):
            return True

        async def mark_read(self, p, a, c):
            return True

        async def send(self, p, a, c, t):
            sends.append(t)
            return {"delivered": True}

    import src.integrations.account_orchestrator as _ao
    monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())
    a = _assistant(cfg)
    r1 = _run(maybe_send_holding_reply(
        a, "telegram", "a", "c", "telegram:a:c", peer_text="在吗", lang="zh"))
    r2 = _run(maybe_send_holding_reply(
        a, "telegram", "a", "c", "telegram:a:c", peer_text="在吗", lang="zh"))
    assert r1 is True and r2 is False
    assert len(sends) == 1        # 冷却内只发一次


def test_typing_delay_shows_typing_before_send(monkeypatch):
    # 极小延迟（0.02s）→ 真 sleep 可忽略，仍验证顺序：已读 → 打字 → 发送
    cfg = {"inbox": {"l2_autosend": {"holding": {
        "enabled": True, "typing_delay_sec": 0.02}}}}
    events = []

    class _Orch:
        def owns_media(self, p, a):
            return True

        async def mark_read(self, p, a, c):
            events.append("read")
            return True

        async def send_chat_action(self, p, a, c, action):
            events.append(("typing", action))
            return True

        async def send(self, p, a, c, t):
            events.append("send")
            return {"delivered": True}

    import src.integrations.account_orchestrator as _ao
    monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())
    r = _run(maybe_send_holding_reply(
        _assistant(cfg), "telegram", "a", "c", "telegram:a:c",
        peer_text="我生气了啊", lang="zh"))
    assert r is True
    assert events[0] == "read"                     # 先已读
    assert ("typing", "typing") in events          # 打字节奏出现
    assert events.index("send") > events.index("read")  # 发送在已读后
    assert events[-1] == "send"                    # 最后才发缓冲话术


def test_never_raises_on_send_exception(monkeypatch):
    cfg = {"inbox": {"l2_autosend": {"holding": {"enabled": True}}}}

    class _Orch:
        def owns_media(self, p, a):
            return True

        async def mark_read(self, p, a, c):
            return True

        async def send(self, p, a, c, t):
            raise RuntimeError("send boom")

    import src.integrations.account_orchestrator as _ao
    monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())
    r = _run(maybe_send_holding_reply(
        _assistant(cfg), "telegram", "a", "c", "telegram:a:c",
        peer_text="嗯", lang="zh"))
    assert r is False           # 异常吞掉，软降级
