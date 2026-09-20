"""群消息「未触发也入库」（2026-09-18 P6 社群舞台实锤）。

此前 ``handle_group_message`` 在触发裁决为假时整条 return，未触发的群消息从未落库，
工作台「群组」视图只能等历史自动同步 / 手动 from_latest 滞后出现。现在未触发分支
调 ``_mirror_untriggered_group_message`` 只镜像进收件箱（不回复、不起草）。本文件钉住：

1. 镜像字段口径：群名当会话名（不是发言人名）、发言人走结构化字段、显式 chat_type=group、
   msg_id / ts 随行（与历史同步同一去重主键）；
2. 纯媒体不下载本体：占位文 + media_type；
3. 两把闸：standalone（mirror_inbox 关）与 ``group_reply.mirror_untriggered=false`` 均 no-op；
4. 接线静态断言：群 handler 未触发分支真的在调它，且位于触发裁决之后、_process_message 之前。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.client.telegram_client import TelegramClient
from tests._source_block import source_block

_TC_PATH = Path(__file__).resolve().parents[1] / "src" / "client" / "telegram_client.py"


class _Cfg:
    def __init__(self, group_reply: dict | None = None) -> None:
        self._d = {"telegram": {"group_reply": dict(group_reply or {})}}

    def get(self, key, default=None):
        return self._d.get(key, default)


class _Logger:
    def info(self, *a, **k):  # noqa: D401
        pass

    debug = info
    warning = info
    error = info


def _client(*, mirror: bool = True, group_reply: dict | None = None):
    c = TelegramClient.__new__(TelegramClient)
    c._mirror_inbox = mirror
    c.config = _Cfg(group_reply)
    c._logger = _Logger()  # LoggerMixin.logger 属性只读，走其惰性缓存槽
    c.account_id = "6834964252"
    calls: list[dict] = []
    c._emit_inbox = lambda **kw: calls.append(kw)  # type: ignore[method-assign]
    return c, calls


def _msg(text: str | None = "Pak @htqp456, harga GaN 20W?", **extra):
    base = dict(
        id=4321,
        chat=SimpleNamespace(id=-1004309455763, title="2026社群聊天", type="supergroup"),
        from_user=SimpleNamespace(id=8244899900, first_name="Katie", last_name="",
                                  username="Sousaun"),
        text=text, caption=None, photo=None, voice=None, audio=None, video=None,
        video_note=None, animation=None, sticker=None, document=None,
        date=datetime(2026, 9, 18, 10, 0, 0, tzinfo=timezone.utc),
    )
    base.update(extra)
    return SimpleNamespace(**base)


def test_text_message_mirrored_with_group_semantics():
    c, calls = _client()
    c._mirror_untriggered_group_message(_msg())
    assert len(calls) == 1
    kw = calls[0]
    assert kw["direction"] == "in"
    assert kw["chat_id"] == -1004309455763
    assert kw["chat_type"] == "group"
    # 会话名＝群名，不是发言人（否则群会话会被改名成 Katie）
    assert kw["name"] == "2026社群聊天"
    assert kw["sender_id"] == "8244899900"
    assert kw["sender_name"] == "Katie"
    assert kw["msg_id"] == "4321"
    assert kw["text"] == "Pak @htqp456, harga GaN 20W?"
    assert kw["media_type"] == ""
    assert abs(kw["ts"] - datetime(2026, 9, 18, 10, 0, 0, tzinfo=timezone.utc).timestamp()) < 1e-6


def test_sender_falls_back_to_username_when_no_name():
    c, calls = _client()
    m = _msg(from_user=SimpleNamespace(id=1, first_name="", last_name="", username="buyer_x"))
    c._mirror_untriggered_group_message(m)
    assert calls[0]["sender_name"] == "@buyer_x"


def test_media_only_uses_placeholder_without_download():
    c, calls = _client()
    c._mirror_untriggered_group_message(_msg(text=None, photo=object()))
    assert len(calls) == 1
    assert calls[0]["media_type"] == "image"
    assert calls[0]["text"]  # 占位文非空（与 history_message_obj 同口径）
    assert "media_ref" not in calls[0] or not calls[0].get("media_ref")


def test_service_message_without_content_not_mirrored():
    c, calls = _client()
    c._mirror_untriggered_group_message(_msg(text=None))
    assert calls == []


def test_noop_when_mirror_inbox_off():
    c, calls = _client(mirror=False)
    c._mirror_untriggered_group_message(_msg())
    assert calls == []


def test_noop_when_switch_off_and_default_on():
    c, calls = _client(group_reply={"mirror_untriggered": False})
    c._mirror_untriggered_group_message(_msg())
    assert calls == []
    c2, calls2 = _client(group_reply={})
    c2._mirror_untriggered_group_message(_msg())
    assert len(calls2) == 1


def test_emit_inbox_threads_chat_type_into_source():
    block = source_block(_TC_PATH, "    def _emit_inbox(")
    assert "chat_type: str = \"\"" in block
    assert "_src[\"chat_type\"] = str(chat_type)" in block


def test_triggered_path_mirrors_group_under_group_title():
    # 触发路径（_process_message_async）的镜像同样不能拿发言人名当群会话名
    block = source_block(_TC_PATH, "    async def _process_message_async(")
    assert "_mirror_name = _peer_name" in block
    assert "if _mirror_is_group:" in block
    assert "name=_mirror_name," in block
    assert "name=_peer_name," not in block, "群镜像仍在用发言人名当会话名"


def test_group_handler_untriggered_branch_calls_mirror():
    block = source_block(_TC_PATH, "async def handle_group_message(")
    assert "_mirror_untriggered_group_message(" in block, "群 handler 未触发分支未接镜像"
    skip_pos = block.index("跳过未触发消息")
    mirror_pos = block.index("self._mirror_untriggered_group_message(")
    process_pos = block.index("await self._process_message(")
    assert skip_pos < mirror_pos < process_pos, "镜像应在触发裁决为假之后、进入回复链之前"
