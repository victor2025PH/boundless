"""引用消息上下文链（2026-08-20「引用+『分析』全链不可见」实锤）门禁。

纯函数（extract/format）行为 + 四处接线的静态钉：
telegram_client 恒写键 → skill_manager 显式搬运+分类并入 → ai_client 提示位消费
→ trigger 判定并入。接线断任何一环，「引用报障消息+两个字指令」就退回闲聊静默。
"""
import sys
from pathlib import Path
from types import SimpleNamespace as NS

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.client.quoted_context import (  # noqa: E402
    QUOTE_MAX_CHARS,
    extract_quoted,
    format_quoted_note,
)


def _msg(reply=None):
    return NS(reply_to_message=reply)


def test_extract_none_and_text_and_cap():
    assert extract_quoted(_msg(None)) == {"text": "", "sender": "", "is_me": False}
    rq = NS(text="语音发送失败，右侧报错", caption=None, from_user=NS(
        id=99, first_name="花", last_name="无缺", username="skuio"),
        photo=None, voice=None, audio=None, video=None, video_note=None,
        sticker=None, document=None)
    q = extract_quoted(_msg(rq), my_user_id=1)
    assert q["text"].startswith("语音发送失败") and q["sender"] == "花 无缺"
    assert q["is_me"] is False
    rq.text = "长" * 999
    assert len(extract_quoted(_msg(rq))["text"]) == QUOTE_MAX_CHARS


def test_extract_media_placeholder_and_is_me():
    rq = NS(text=None, caption=None, from_user=NS(
        id=7, first_name="智聊支持", last_name="", username="zc"),
        photo=object(), voice=None, audio=None, video=None, video_note=None,
        sticker=None, document=None)
    q = extract_quoted(_msg(rq), my_user_id=7)
    assert q["text"] == "[图片]" and q["is_me"] is True


def test_format_note_semantics():
    assert format_quoted_note({"text": ""}) == ""
    n = format_quoted_note({"text": "报错了", "sender": "花无缺", "is_me": False})
    assert "「花无缺」" in n and "报错了" in n and "引用上下文" in n
    n2 = format_quoted_note({"text": "旧回复", "sender": "x", "is_me": True})
    assert "你之前发的" in n2 and "不是对方的新消息" in n2


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_wiring_client_always_writes_key():
    src = _read("src/client/telegram_client.py")
    assert "from src.client.quoted_context import extract_quoted" in src
    assert "_sm_context['_quoted_note']" in src
    # 恒写键（异常分支也写空串），防旧引用跨轮粘住
    assert "_sm_context['_quoted_note'] = \"\"" in src


def test_wiring_skill_manager_transport_and_classify():
    src = _read("src/skills/skill_manager.py")
    assert 'user_context.pop("_quoted_note", None)' in src
    assert '[引用]' in src  # 分类判定并入引用文本


def test_wiring_ai_client_consumes():
    src = _read("src/ai/ai_client.py")
    assert '_quoted = (context.get("_quoted_note") or "").strip()' in src


def test_wiring_trigger_appends_quote():
    src = _read("src/client/trigger.py")
    body = src.split("async def _should_reply_to_group_message", 1)[1]
    assert "reply_to_message" in body and "[引用]" in body
