"""入站「引用消息」上下文提取（2026-08-20 内测实锤）。

事故：用户在报障群**引用一条消息**后只发「分析」两个字——入站管线从不读
``message.reply_to_message``，AI 看到的就是孤零零的「分析」，引用的问题内容
全链不可见（分类器判成闲聊、LLM 无从分析、收件箱镜像 reply_to_* 列恒空）。

设计：
- ``extract_quoted``：pyrogram Message → ``{text, sender, is_me}``（纯 getattr，
  无网络调用；无引用返回全空 dict——调用方**恒写键**防上一轮的引用粘住）。
- ``format_quoted_note``：拼给 LLM 的中文提示块；引用的是 AI 自己的旧话时明示
  「你之前发的」——防模型把自己的话当成对方新说的（Phase8 记忆接地同族风险，
  这也是引用内容刻意走 context 提示位、**绝不并进用户正文**的原因：正文会进
  记忆抽取，AI 旧话会被接地成「用户说过」）。
- 分类器（bug_intake）另行把引用文本并入判定串——「分析」+引用的报障内容
  应判 bug 而非闲聊；那条路径不落记忆，无接地风险。
"""
from __future__ import annotations

from typing import Any, Dict

#: 引用摘要长度上限（进 prompt 的是摘要不是全文；超长引用多为转发长文）
QUOTE_MAX_CHARS = 400


def extract_quoted(message: Any, my_user_id: Any = None) -> Dict[str, Any]:
    """从 pyrogram Message 提取被引用消息摘要。

    返回 ``{"text": str, "sender": str, "is_me": bool}``；无引用 → text 为空串。
    媒体引用给占位符（「[图片]」等），engagement 语义由调用方决定。
    """
    out = {"text": "", "sender": "", "is_me": False}
    rq = getattr(message, "reply_to_message", None)
    if rq is None:
        return out
    text = (getattr(rq, "text", None) or getattr(rq, "caption", None) or "")
    text = str(text).strip()
    if not text:
        if getattr(rq, "photo", None):
            text = "[图片]"
        elif getattr(rq, "voice", None) or getattr(rq, "audio", None):
            text = "[语音]"
        elif getattr(rq, "video", None) or getattr(rq, "video_note", None):
            text = "[视频]"
        elif getattr(rq, "sticker", None):
            text = "[贴纸]"
        elif getattr(rq, "document", None):
            text = "[文件]"
        else:
            text = "[消息]"
    out["text"] = text[:QUOTE_MAX_CHARS]
    fu = getattr(rq, "from_user", None)
    if fu is not None:
        first = str(getattr(fu, "first_name", "") or "")
        last = str(getattr(fu, "last_name", "") or "")
        name = (first + " " + last).strip() or str(getattr(fu, "username", "") or "")
        out["sender"] = name
        try:
            if my_user_id is not None and getattr(fu, "id", None) == my_user_id:
                out["is_me"] = True
        except Exception:
            pass
    return out


def format_quoted_note(q: Dict[str, Any]) -> str:
    """引用摘要 → LLM 提示块（无引用返回空串，调用方恒写键）。"""
    text = str((q or {}).get("text") or "").strip()
    if not text:
        return ""
    if q.get("is_me"):
        who = "你之前发的一条消息"
    elif q.get("sender"):
        who = f"「{q['sender']}」的一条消息"
    else:
        who = "一条消息"
    return (
        f"【引用上下文】对方引用了{who}后发言，被引用的内容是：「{text}」。"
        "对方本条发言针对的就是这段被引用内容——回应时以它为对象；"
        "若引用的是你自己之前的话，那是你说过的，不是对方的新消息。")
