"""投递错误人话映射（2026-08-20 内测工单 #3）纯函数门禁。

事故：客户用语音克隆给客户发语音，对端 Telegram 隐私设置禁收语音 →
``VOICE_MESSAGES_FORBIDDEN`` 裸英文经 ``err=ex`` 直显——用户当成系统故障报障。
映射器把常见「平台拒收」错误translate成人话+行动建议；识别不出回空串走旧文案。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.web.routes.unified_inbox_send_routes import humanize_send_err_key  # noqa: E402


class _FakeExc(Exception):
    pass


class VoiceMessagesForbidden(Exception):
    """模拟 pyrogram 异常类名形态（无下划线）。"""


def test_voice_privacy_marker_and_classname():
    assert humanize_send_err_key(
        "Telegram says: [400 VOICE_MESSAGES_FORBIDDEN] ..."
    ) == "err.inbox.voice_peer_privacy"
    assert humanize_send_err_key(
        VoiceMessagesForbidden("400")) == "err.inbox.voice_peer_privacy"


def test_blocked_flood_forbidden_deactivated():
    assert humanize_send_err_key("USER_IS_BLOCKED") == "err.inbox.send_peer_blocked"
    assert humanize_send_err_key(
        _FakeExc("[420 FLOOD_WAIT_23]")) == "err.inbox.send_flood"
    assert humanize_send_err_key("PEER_FLOOD") == "err.inbox.send_flood"
    assert humanize_send_err_key(
        "CHAT_WRITE_FORBIDDEN") == "err.inbox.send_write_forbidden"
    assert humanize_send_err_key(
        "INPUT_USER_DEACTIVATED") == "err.inbox.send_peer_deactivated"


def test_unknown_returns_empty():
    assert humanize_send_err_key("Connection reset by peer") == ""
    assert humanize_send_err_key("") == ""
    assert humanize_send_err_key(None) == ""


def test_i18n_keys_bilingual():
    from src.web.i18n_packs.errors import EN, ZH
    for key in ("err.inbox.voice_peer_privacy", "err.inbox.send_peer_blocked",
                "err.inbox.send_flood", "err.inbox.send_write_forbidden",
                "err.inbox.send_peer_deactivated"):
        assert key in ZH and key in EN
        assert "{" not in ZH[key], "无参键不应含占位符"
