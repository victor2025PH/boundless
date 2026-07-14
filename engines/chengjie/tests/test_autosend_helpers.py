"""autosend_helpers.autosend_voice 抽取回归测试。

重点验证：函数可导入、可调用、正确使用 assistant 捕获（config），
voice 未启用时早退 False（不触发合成/发送）。
"""
import asyncio
from types import SimpleNamespace

import pytest

from src.inbox import autosend_helpers
from src.inbox.autosend_helpers import (
    autosend_image,
    autosend_voice,
    build_autosend_callbacks,
    build_autosend_mark_read_cb,
    build_autosend_translate_cb,
    build_autosend_typing_cb,
)


def _assistant(cfg: dict):
    return SimpleNamespace(config=SimpleNamespace(config=cfg))


def test_is_coroutine_function():
    assert asyncio.iscoroutinefunction(autosend_voice)


def test_disabled_empty_config_returns_false():
    # 空配置 → voice_autosend 未启用 → 早退 False（验证 assistant.config 捕获正确）
    r = asyncio.run(autosend_voice(_assistant({}), "telegram", "acct", "chat", "hi"))
    assert r is False


def test_disabled_explicit_returns_false():
    cfg = {"voice_autosend": {"enabled": False}}
    r = asyncio.run(autosend_voice(_assistant(cfg), "telegram", "acct", "chat", "hi"))
    assert r is False


def test_owns_media_false_returns_false(monkeypatch):
    # voice 启用，但账号不归编排器管理（owns_media=False）→ 早退 False。
    # 验证走到 orchestrator 分支的捕获链不 NameError。
    cfg = {"voice_autosend": {"enabled": True}}

    class _Orch:
        def owns_media(self, platform, account_id):
            return False

    import src.integrations.account_orchestrator as _ao
    monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())

    r = asyncio.run(autosend_voice(_assistant(cfg), "telegram", "acct", "chat", "hi"))
    assert r is False


class TestAutosendImage:
    def test_is_coroutine_function(self):
        assert asyncio.iscoroutinefunction(autosend_image)

    def test_disabled_empty_config_returns_false(self):
        r = asyncio.run(autosend_image(_assistant({}), "telegram", "acct", "chat", "hi"))
        assert r is False

    def test_disabled_explicit_returns_false(self):
        cfg = {"image_autosend": {"enabled": False}}
        r = asyncio.run(autosend_image(_assistant(cfg), "telegram", "acct", "chat", "hi"))
        assert r is False

    def test_owns_media_false_returns_false(self, monkeypatch):
        cfg = {"image_autosend": {"enabled": True}}

        class _Orch:
            def owns_media(self, platform, account_id):
                return False

        import src.integrations.account_orchestrator as _ao
        monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())
        r = asyncio.run(autosend_image(_assistant(cfg), "telegram", "acct", "chat", "hi"))
        assert r is False


def _assistant_full(cfg=None):
    return SimpleNamespace(
        config=SimpleNamespace(config=cfg or {}),
        logger=SimpleNamespace(debug=lambda *a, **k: None,
                               info=lambda *a, **k: None,
                               warning=lambda *a, **k: None),
        inbox_store=None, _web_loop=None,
    )


def _web_app():
    return SimpleNamespace(state=SimpleNamespace())


class TestBuildAutosendCallbacks:
    def test_deliver_disabled_send_cb_none(self):
        send_cb, _tr = build_autosend_callbacks(_assistant_full(), _web_app(), False)
        assert send_cb is None

    def test_deliver_enabled_send_cb_callable(self):
        send_cb, _tr = build_autosend_callbacks(_assistant_full(), _web_app(), True)
        assert asyncio.iscoroutinefunction(send_cb)

    def test_translate_cb_none_when_disabled(self):
        _s, tr = build_autosend_callbacks(_assistant_full(), _web_app(), False)
        assert tr is None  # 出站翻译默认未启用

    def test_send_cb_falls_through_to_text(self, monkeypatch):
        # image/voice 都返回 False → deliver 落到文本投递(_send_via)。
        # 验证 deliver 主体的捕获链(autosend_image/voice/_send_via/_send_shim/
        # _send_adapters/assistant/_make_coro)运行时不 NameError。
        async def _false(*a, **k):
            return False
        monkeypatch.setattr(autosend_helpers, "autosend_image", _false)
        monkeypatch.setattr(autosend_helpers, "autosend_voice", _false)

        async def _fake_send_via(shim, platform, account_id, chat_key, text, adapters):
            return {"ok": True, "delivered_as": "text", "echo": text}
        import src.inbox.channel_adapters as _ca
        monkeypatch.setattr(_ca, "send_via_adapters", _fake_send_via)

        class _Orch:
            def owns(self, platform, account_id):
                return False
        import src.integrations.account_orchestrator as _ao
        monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())

        send_cb, _tr = build_autosend_callbacks(_assistant_full(), _web_app(), True)
        res = asyncio.run(send_cb("telegram", "acct", "chat", "hi"))
        assert res.get("delivered_as") == "text" and res.get("echo") == "hi"

    def test_voice_branch_uses_original_text(self, monkeypatch):
        # 出站翻译生效场景：语音分支应拿到翻译前原文（text 位）+ 译文（sent_text），
        # 文本回落仍发译文。
        seen = {}

        async def _fake_voice(assistant, platform, account_id, chat_key, text,
                              sent_text=None):
            seen["voice_text"] = text
            seen["sent_text"] = sent_text
            return False  # 不发语音 → 回落文本

        async def _false(*a, **k):
            return False
        monkeypatch.setattr(autosend_helpers, "autosend_voice", _fake_voice)
        monkeypatch.setattr(autosend_helpers, "autosend_image", _false)

        async def _fake_send_via(shim, platform, account_id, chat_key, text, adapters):
            return {"ok": True, "delivered_as": "text", "echo": text}
        import src.inbox.channel_adapters as _ca
        monkeypatch.setattr(_ca, "send_via_adapters", _fake_send_via)

        class _Orch:
            def owns(self, platform, account_id):
                return False
        import src.integrations.account_orchestrator as _ao
        monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())

        send_cb, _tr = build_autosend_callbacks(_assistant_full(), _web_app(), True)
        res = asyncio.run(send_cb(
            "telegram", "acct", "chat", "Hello~", original_text="你好呀~"))
        assert seen == {"voice_text": "你好呀~", "sent_text": "Hello~"}
        assert res.get("echo") == "Hello~"  # 文本回落发译文


class TestPromiseGuardDeliver:
    """出站媒体承诺守卫在 deliver 编排的接线：兑现优先 → 撤回兜底。"""

    def _patch_common(self, monkeypatch):
        async def _fake_send_via(shim, platform, account_id, chat_key, text, adapters):
            return {"ok": True, "delivered_as": "text", "echo": text}
        import src.inbox.channel_adapters as _ca
        monkeypatch.setattr(_ca, "send_via_adapters", _fake_send_via)

        class _Orch:
            def owns(self, platform, account_id):
                return False
        import src.integrations.account_orchestrator as _ao
        monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())

    def test_promise_fulfilled_as_image(self, monkeypatch):
        """文本承诺发图：常规图链(peer 无关键词)不发 → 守卫以 assume_intent=selfie
        兑现 → delivered_as=image（文本丢弃，配文由图链另生成）。"""
        calls = []

        async def _fake_image(assistant, platform, account_id, chat_key, text,
                              assume_intent="", directive_override=None):
            calls.append(assume_intent)
            return bool(assume_intent)  # 常规判定 False；兑现路径 True

        async def _false(*a, **k):
            return False
        monkeypatch.setattr(autosend_helpers, "autosend_image", _fake_image)
        monkeypatch.setattr(autosend_helpers, "autosend_voice", _false)
        self._patch_common(monkeypatch)

        send_cb, _ = build_autosend_callbacks(_assistant_full(), _web_app(), True)
        res = asyncio.run(send_cb("telegram", "acct", "chat", "好呀，等我拍一张给你～"))
        assert res.get("delivered_as") == "image"
        assert calls == ["", "selfie"]  # 先常规、后兑现

    def test_promise_retracted_when_fulfill_fails(self, monkeypatch):
        """兑现失败（selfie 未启用/闸门拦）→ 撤回：文本剥掉承诺句再发出。"""
        async def _false(*a, **k):
            return False
        monkeypatch.setattr(autosend_helpers, "autosend_image", _false)
        monkeypatch.setattr(autosend_helpers, "autosend_voice", _false)
        self._patch_common(monkeypatch)

        send_cb, _ = build_autosend_callbacks(_assistant_full(), _web_app(), True)
        res = asyncio.run(send_cb(
            "telegram", "acct", "chat", "今天好开心！等我拍一张给你～"))
        assert res.get("delivered_as") == "text"
        assert "拍一张" not in res.get("echo", "")
        assert "开心" in res.get("echo", "")  # 其余内容保留

    def test_promise_whole_text_deflects_not_empty(self, monkeypatch):
        """整条都是承诺 → 剥空 → 语言对齐兜底话术（绝不发空文本）。"""
        async def _false(*a, **k):
            return False
        monkeypatch.setattr(autosend_helpers, "autosend_image", _false)
        monkeypatch.setattr(autosend_helpers, "autosend_voice", _false)
        self._patch_common(monkeypatch)

        send_cb, _ = build_autosend_callbacks(_assistant_full(), _web_app(), True)
        res = asyncio.run(send_cb("telegram", "acct", "chat", "等我拍一张给你哈～"))
        echo = res.get("echo", "")
        assert echo.strip() and "拍一张" not in echo

    def test_promise_guard_disabled_passes_through(self, monkeypatch):
        """守卫可配置关闭（companion.media_promise_guard.enabled=false）→ 原样发出。"""
        async def _false(*a, **k):
            return False
        monkeypatch.setattr(autosend_helpers, "autosend_image", _false)
        monkeypatch.setattr(autosend_helpers, "autosend_voice", _false)
        self._patch_common(monkeypatch)

        cfg = {"companion": {"media_promise_guard": {"enabled": False}}}
        send_cb, _ = build_autosend_callbacks(
            _assistant_full(cfg), _web_app(), True)
        res = asyncio.run(send_cb("telegram", "acct", "chat", "等我拍一张给你哈～"))
        assert res.get("echo") == "等我拍一张给你哈～"

    def test_promise_fulfill_flag_off_goes_straight_to_retract(self, monkeypatch):
        """fulfill=false：不尝试兑现（不多打一次图链），直接撤回。"""
        calls = []

        async def _fake_image(assistant, platform, account_id, chat_key, text,
                              assume_intent="", directive_override=None):
            calls.append(assume_intent)
            return False
        monkeypatch.setattr(autosend_helpers, "autosend_image", _fake_image)

        async def _false(*a, **k):
            return False
        monkeypatch.setattr(autosend_helpers, "autosend_voice", _false)
        self._patch_common(monkeypatch)

        cfg = {"companion": {"media_promise_guard": {"fulfill": False}}}
        send_cb, _ = build_autosend_callbacks(
            _assistant_full(cfg), _web_app(), True)
        res = asyncio.run(send_cb("telegram", "acct", "chat", "等我拍一张给你～"))
        assert calls == [""]  # 只有常规尝试，无兑现尝试
        assert "拍一张" not in res.get("echo", "")

    def test_voice_promise_stripped_when_voice_not_sent(self, monkeypatch):
        """语音承诺：语音分支没发成 → 文本出站前剥掉「发你语音」承诺。"""
        async def _false(*a, **k):
            return False
        monkeypatch.setattr(autosend_helpers, "autosend_image", _false)
        monkeypatch.setattr(autosend_helpers, "autosend_voice", _false)
        self._patch_common(monkeypatch)

        send_cb, _ = build_autosend_callbacks(_assistant_full(), _web_app(), True)
        res = asyncio.run(send_cb(
            "telegram", "acct", "chat", "想你啦！我录条语音给你哈～"))
        echo = res.get("echo", "")
        assert "语音" not in echo and "想你" in echo

    def test_translated_promise_uses_original_text_detection(self, monkeypatch):
        """出站翻译场景：译文（英文承诺）也能被检测/撤回——语音分支不再拿到
        含承诺的原文（original_text 被置空防克隆声念谎话）。"""
        seen = {}

        async def _fake_voice(assistant, platform, account_id, chat_key, text,
                              sent_text=None):
            seen["voice_text"] = text
            return False

        async def _false(*a, **k):
            return False
        monkeypatch.setattr(autosend_helpers, "autosend_image", _false)
        monkeypatch.setattr(autosend_helpers, "autosend_voice", _fake_voice)
        self._patch_common(monkeypatch)

        send_cb, _ = build_autosend_callbacks(_assistant_full(), _web_app(), True)
        res = asyncio.run(send_cb(
            "telegram", "acct", "chat",
            "So happy today! I'll send you a photo~",
            original_text="今天好开心！等我拍一张给你～"))
        # 撤回后语音分支念的文本不含承诺（原文被置空 → 用撤回后的译文）
        assert "photo" not in seen.get("voice_text", "")
        assert "photo" not in res.get("echo", "")


class TestVoiceLangGate:
    def test_translated_text_without_peer_voice_falls_back(self, monkeypatch):
        # 出站翻译生效（sent_text≠text）且对方未发语音 → lang_mismatch 回落文本，
        # 不进入合成/投递。
        cfg = {"inbox": {"l2_autosend": {"voice": {"enabled": True}}}}

        class _Orch:
            def owns_media(self, platform, account_id):
                return True

        import src.integrations.account_orchestrator as _ao
        monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())

        r = asyncio.run(autosend_voice(
            _assistant_full(cfg), "telegram", "acct", "chat",
            "你好呀~", sent_text="Hello~"))
        assert r is False
        from src.inbox.voice_autosend import metrics_snapshot
        assert metrics_snapshot()["last_decision"] == "text:lang_mismatch"

    def test_same_text_skips_lang_gate(self, monkeypatch):
        # 未翻译（sent_text==text）→ 语言闸门不拦，走正常 decide_voice
        # （无 peer_voice 信号 → when_peer_voice 默认 trigger 判文字 no_peer_voice）。
        cfg = {"inbox": {"l2_autosend": {"voice": {"enabled": True}}}}

        class _Orch:
            def owns_media(self, platform, account_id):
                return True

        import src.integrations.account_orchestrator as _ao
        monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())

        r = asyncio.run(autosend_voice(
            _assistant_full(cfg), "telegram", "acct", "chat",
            "你好呀~", sent_text="你好呀~"))
        assert r is False
        from src.inbox.voice_autosend import metrics_snapshot
        assert metrics_snapshot()["last_decision"] == "text:no_peer_voice"


class TestBuildMarkReadCb:
    def test_default_enabled_returns_coroutine_fn(self):
        cb = build_autosend_mark_read_cb(_assistant_full({}))
        assert cb is not None and asyncio.iscoroutinefunction(cb)

    def test_disabled_returns_none(self):
        cfg = {"inbox": {"l2_autosend": {"mark_read_before_reply": False}}}
        assert build_autosend_mark_read_cb(_assistant_full(cfg)) is None

    def test_dispatches_to_orchestrator(self, monkeypatch):
        calls = []

        class _Orch:
            async def mark_read(self, platform, account_id, chat_key):
                calls.append((platform, account_id, chat_key))
                return True

        import src.integrations.account_orchestrator as _ao
        monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())
        cb = build_autosend_mark_read_cb(_assistant_full({}))
        asyncio.run(cb("telegram", "acct", 12345))
        assert calls == [("telegram", "acct", "12345")]


class TestBuildTypingCb:
    def test_default_enabled_returns_coroutine_fn(self):
        cb = build_autosend_typing_cb(_assistant_full({}))
        assert cb is not None and asyncio.iscoroutinefunction(cb)

    def test_disabled_returns_none(self):
        cfg = {"inbox": {"l2_autosend": {"typing_indicator": False}}}
        assert build_autosend_typing_cb(_assistant_full(cfg)) is None

    def test_dispatches_to_orchestrator(self, monkeypatch):
        calls = []

        class _Orch:
            async def send_chat_action(self, platform, account_id, chat_key, action):
                calls.append((platform, account_id, chat_key, action))
                return True

        import src.integrations.account_orchestrator as _ao
        monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())
        cb = build_autosend_typing_cb(_assistant_full({}))
        asyncio.run(cb("telegram", "acct", 12345, "typing"))
        assert calls == [("telegram", "acct", "12345", "typing")]
