# -*- coding: utf-8 -*-
"""peer 显示名清洗契约门禁（2026-08-14，「好友名单显示 Active now」事故）。

三层守护：
1. **跨语言契约**——services/messenger-web/server.js::DIRTY_NAME_RE 与
   src/integrations/protocol_bridge.py::_DIRTY_NAME_PATTERN 必须**逐字一致**
   （worker 取名侧与服务端入口侧共用同一词表；任何一侧单独改 → 这里先红）。
2. **金标正例**——实录脏名（状态/导航/未读/列表头）必须全被归零。
3. **金标反例**——含状态词的**真名**（"Active Fitness Club"）绝不误伤；
   词表是窄黑名单，误伤真名比放过脏名更糟。
"""

from __future__ import annotations

import re
from pathlib import Path

from src.integrations.protocol_bridge import (
    _DIRTY_NAME_PATTERN,
    sanitize_peer_name,
)

_ENGINE_ROOT = Path(__file__).resolve().parent.parent
_SERVER_JS = _ENGINE_ROOT / "services" / "messenger-web" / "server.js"


# ── 1. 跨语言契约：JS 与 Python 同一 pattern ─────────────────────────────────

def _extract_js_pattern() -> str:
    src = _SERVER_JS.read_text(encoding="utf-8")
    m = re.search(r"const DIRTY_NAME_RE = /(.+)/i;", src)
    assert m, "server.js 里找不到 DIRTY_NAME_RE 定义（被改名/删除？契约随之失守）"
    return m.group(1)


def test_js_py_pattern_verbatim_identical():
    js = _extract_js_pattern()
    assert js == _DIRTY_NAME_PATTERN, (
        "DIRTY_NAME_RE（JS）与 _DIRTY_NAME_PATTERN（PY）不一致——两侧必须逐字同源。\n"
        f"JS : {js}\nPY : {_DIRTY_NAME_PATTERN}"
    )


# ── 2. 金标正例：脏名必须归零 ───────────────────────────────────────────────

_DIRTY_SAMPLES = [
    # 事故原图实录
    "Active now",
    # 英文在线状态全形态
    "Active", "active today", "Active yesterday", "ACTIVE NOW",
    "Active 3m ago", "Active 35 minutes ago", "Active 2h ago",
    "Active 1 hr ago", "Active 5 days ago", "Active 2 weeks ago",
    "Active 10 min ago", "Active 3",  # 「Active 3」= 时间单位被截断的残行
    "online", "Online",
    "typing...", "Typing", "typing a message",
    # 中文在线状态
    "在线", "在线状态", "刚刚活跃", "昨天活跃", "活跃",
    "3 分钟前活跃", "12小时前活跃", "2天前活跃", "1 周前活跃",
    "正在输入…", "对方正在输入中",
    # 导航/请求区标签（旧版完全零过滤的重灾区）
    "消息请求", "Message request", "Message requests",
    # 未读标记 / 列表头
    "未读消息", "未读消息 2 条", "Unread messages", "unread messages: 3",
    "Chats · 3", "Chat · new", "聊天 · 5",
    # 分隔符残行 / 行动标签
    "·", "•", "回复？", "是否跟进？",
]


def test_dirty_names_sanitized_to_empty():
    bad = [s for s in _DIRTY_SAMPLES if sanitize_peer_name(s) != ""]
    assert not bad, f"以下脏名未被归零（词表漏网）：{bad!r}"


# ── 3. 金标反例：真名绝不误伤 ───────────────────────────────────────────────

_REAL_NAMES = [
    # 含状态词但显然是真名（锚定 ^…$ + 词形收紧的存在理由）
    "Active Fitness Club", "Online Shop PH", "Active8 Media",
    "李活跃", "王在线科技",
    # 普通真名（中/英/混排/带 emoji/带数字）
    "张伟", "Maria Santos", "Nguyen Van A", "小美 Mia", "JM 🌸",
    "Kim 123", "回复哥",  # 「回复？」是标签，「回复哥」是名字
    "9639346239762887",  # 裸 chat_key 数字 id：上游回落语义，不归零
]


def test_real_names_preserved():
    hurt = [s for s in _REAL_NAMES if sanitize_peer_name(s) != s]
    assert not hurt, f"以下真名被误伤（黑名单过宽）：{hurt!r}"


# ── 4. 边界语义 ─────────────────────────────────────────────────────────────

def test_edge_semantics():
    assert sanitize_peer_name(None) == ""
    assert sanitize_peer_name("") == ""
    assert sanitize_peer_name("   ") == ""
    # 清洗自带 strip：合法名保内容、去两端空白
    assert sanitize_peer_name("  Maria  ") == "Maria"
    # 非字符串输入不炸（防御边车传数字）
    assert sanitize_peer_name(12345) == "12345"
