# -*- coding: utf-8 -*-
"""R87 P2-2 词条：会话头旁注「本会话语音不可用（克隆声不支持 {lang}）」。

不改状态（文字照常回），只替代每条 ``clone_lang_unsupported`` WARNING。
占位符 ``{lang}`` = ja / th / …。
"""

ZH = {
    "inbox.cs.note.voice_clone_lang": "本会话语音不可用（克隆声不支持 {lang}）",
    "inbox.cs.note.voice_clone_lang_t": "这个会话的回复语种克隆声念不了，系统已按设计改发文字，不是语音链路坏了。同一会话不再反复告警。",
}

EN = {
    "inbox.cs.note.voice_clone_lang": "Voice off for this chat (clone does not support {lang})",
    "inbox.cs.note.voice_clone_lang_t": "The clone voice cannot speak this chat's language, so replies go as text by design. The same chat is not warned on every message.",
}

ZH_HANT = {
    "inbox.cs.note.voice_clone_lang": "本會話語音不可用（克隆聲不支援 {lang}）",
    "inbox.cs.note.voice_clone_lang_t": "這個會話的回覆語種克隆聲唸不了，系統已按設計改發文字，不是語音鏈路壞了。同一會話不再反覆告警。",
}
