# -*- coding: utf-8 -*-
"""WP-4 rider ①：A 线（telegram 原生 `_send_reply`）系统级披露接线门禁（2026-08-17）。

钉住的不变量：
1. `compliance.disclosure.notice=true` → 同会话**首条**文本回复前置披露语，
   第二条起原样（持久防重）；
2. 防重键＝`conv_id("telegram", account_id, chat_id)`——与 B 线 autosend /
   协议线**同键空间**（任一条线披露过，其余线不再重复）；
3. 默认关（runtime provider 未注册）＝逐字节原样发送，零行为变化；
4. 披露注入任何异常＝原样发送（fail-open，绝不阻断回复）。

真 `TelegramSenderMixin._send_reply` 驱动（发送/镜像/节流全桩），不是复刻逻辑。
"""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from src.client.sender import TelegramSenderMixin


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """披露状态钉 tmp + 清 disclosure/runtime 两个进程级单例。"""
    monkeypatch.setenv("AITR_CONFIG_PATH", str(tmp_path / "config" / "config.yaml"))
    from src.compliance import disclosure, runtime

    disclosure._reset_cache_for_tests()
    runtime._reset_for_tests()
    yield tmp_path / "config"
    disclosure._reset_cache_for_tests()
    runtime._reset_for_tests()


class _FakeTgClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(id=f"m{len(self.sent)}")


class _Sender(TelegramSenderMixin):
    """真 mixin + 全桩外围（节流/镜像/记账/清洗都置空，只留披露与发送）。"""

    def __init__(self):
        self.client = _FakeTgClient()
        self.config = {}
        self.account_id = "acct7"
        self.logger = logging.getLogger("test_aline_disclosure")

    def _presend_blocked(self, is_autoreply=True, peer=None):
        return False

    async def _mark_peer_read(self, chat_id):
        return None

    async def _presend_pace(self):
        return None

    def _sanitize_parenthetical_stage_directions(self, text):
        return text

    def _persona_display_name(self):
        return ""

    def _reply_to_message_id_for_send(self, message):
        return None

    def _postsend_record_count(self):
        return None

    def _postsend_mirror_and_record(self, chat_id, text, msg_id=""):
        return None

    def _log_safe_text(self, text):
        return text

    def _handle_send_exc(self, exc):
        return None


def _msg(chat_id=9001):
    return SimpleNamespace(chat=SimpleNamespace(id=chat_id), from_user=None)


def _enable_notice():
    from src.compliance.runtime import set_config_provider
    set_config_provider(lambda: {
        "compliance": {"disclosure": {"notice": True}}})


def test_first_text_reply_carries_disclosure_once(iso):
    _enable_notice()
    s = _Sender()
    asyncio.run(s._send_reply(_msg(), "你好呀，今天过得怎么样"))
    asyncio.run(s._send_reply(_msg(), "我这边刚忙完呢"))
    assert len(s.client.sent) == 2
    first, second = s.client.sent[0]["text"], s.client.sent[1]["text"]
    assert first != "你好呀，今天过得怎么样", "首条必须带披露前缀"
    assert "你好呀，今天过得怎么样" in first, "披露是前置，原回复必须完整保留"
    assert second == "我这边刚忙完呢", "同会话第二条不得重复披露"


def test_mark_key_matches_bline_conv_id(iso):
    """防重键必须与 B 线/协议线同键空间（跨线只披露一次的前提）。"""
    _enable_notice()
    s = _Sender()
    asyncio.run(s._send_reply(_msg(chat_id=9002), "hello there, how are you"))
    from src.compliance.disclosure import STATE_FILENAME
    state = json.loads((iso / STATE_FILENAME).read_text(encoding="utf-8"))
    keys = set((state.get("marks") or state or {}).keys()) if isinstance(
        state, dict) else set()
    flat = json.dumps(state, ensure_ascii=False)
    assert "telegram:acct7:9002" in flat, f"键空间漂移: keys={keys} raw={flat[:200]}"


def test_provider_unset_is_verbatim_passthrough(iso):
    s = _Sender()
    asyncio.run(s._send_reply(_msg(), "原样文本，一个字不动"))
    assert s.client.sent[0]["text"] == "原样文本，一个字不动"
    from src.compliance.disclosure import marks_count
    assert marks_count() == 0, "默认关不得烧标记"


def test_disclosure_exception_fails_open(iso, monkeypatch):
    _enable_notice()
    import src.compliance.disclosure as disc
    monkeypatch.setattr(disc, "apply_disclosure",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    s = _Sender()
    asyncio.run(s._send_reply(_msg(), "异常也要把话发出去"))
    assert s.client.sent[0]["text"] == "异常也要把话发出去"
