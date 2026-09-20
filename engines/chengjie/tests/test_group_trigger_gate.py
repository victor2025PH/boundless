# -*- coding: utf-8 -*-
"""P3-2 群聊兜底闸门禁：_group_bare_gate 语义 + 三条兜底路径接线。

背景（2026-07-25 测试群实锤）：follow_up / l2_fallback / ai_context 是私聊
语义的兜底——「45 分钟会话窗 + 10 条内我说过话」在群里=见谁都抢答，用户
点名「每句都回复」。收紧为：群聊中仅当我们最近回复过该发言者且在群短窗
（group_reply.follow_window_minutes，默认 3 分钟）内才放行兜底；显式信号
（reply/@/关键词/L1）不经此闸。私聊行为一字不变。

不 import telegram_client（避免 pyrogram 依赖）：闸门方法从 trigger mixin
用 MethodType 绑到轻量 stub；三条兜底的接线用源码级断言锁定。
"""
from __future__ import annotations

import enum
import re
import time
import types
from pathlib import Path
from types import SimpleNamespace

from src.client.trigger import TelegramTriggerMixin

TRIGGER_SRC = (
    Path(__file__).resolve().parents[1] / "src" / "client" / "trigger.py"
).read_text(encoding="utf-8")


class ChatType(enum.Enum):  # 形态与 pyrogram.enums.ChatType 同构
    PRIVATE = "private"
    GROUP = "group"
    SUPERGROUP = "supergroup"


def _stub(follow_window_minutes=None) -> SimpleNamespace:
    grp: dict = {}
    if follow_window_minutes is not None:
        grp["follow_window_minutes"] = follow_window_minutes
    s = SimpleNamespace(
        config={"telegram": {"group_reply": grp}},
        _session_reply_ts={},
    )
    s._group_bare_gate = types.MethodType(
        TelegramTriggerMixin._group_bare_gate, s)
    return s


def _msg(chat_type, chat_id=-100888, user_id=777):
    return SimpleNamespace(
        chat=SimpleNamespace(type=chat_type, id=chat_id),
        from_user=SimpleNamespace(id=user_id),
    )


def test_private_chat_always_passes():
    """私聊恒放行——兜底行为一字不变。"""
    s = _stub()
    assert s._group_bare_gate(_msg(ChatType.PRIVATE)) is True
    assert s._group_bare_gate(_msg("private")) is True


def test_group_never_interacted_blocked():
    """群里从未互动过的发言者 → 兜底不放行（须 reply/@/关键词唤起）。"""
    s = _stub()
    assert s._group_bare_gate(_msg(ChatType.SUPERGROUP)) is False


def test_group_recent_replied_same_user_passes():
    """我们刚回复过的同一发言者，在群短窗内继续说话 → 放行（真追问）。"""
    s = _stub()
    s._session_reply_ts["-100888:777"] = time.time() - 30
    assert s._group_bare_gate(_msg(ChatType.SUPERGROUP)) is True


def test_group_window_expired_blocked():
    """静默超群短窗（默认 3 分钟）→ 不再抢答。"""
    s = _stub()
    s._session_reply_ts["-100888:777"] = time.time() - 3 * 60 - 5
    assert s._group_bare_gate(_msg(ChatType.SUPERGROUP)) is False


def test_group_other_user_blocked():
    """群里其他发言者（我们没回复过他）→ 不放行，别人的闲聊不抢答。"""
    s = _stub()
    s._session_reply_ts["-100888:777"] = time.time() - 10
    assert s._group_bare_gate(
        _msg(ChatType.SUPERGROUP, user_id=999)) is False


def test_group_zero_window_kills_bare_paths():
    """follow_window_minutes=0 → 群里彻底关兜底（只留显式信号）。"""
    s = _stub(follow_window_minutes=0)
    s._session_reply_ts["-100888:777"] = time.time()
    assert s._group_bare_gate(_msg(ChatType.GROUP)) is False


def test_group_bad_config_falls_back_to_default():
    """窗口配置坏值 → 回落默认 3 分钟，不崩不放飞。"""
    s = _stub(follow_window_minutes="oops")
    s._session_reply_ts["-100888:777"] = time.time() - 30
    assert s._group_bare_gate(_msg(ChatType.SUPERGROUP)) is True
    s2 = _stub(follow_window_minutes="oops")
    s2._session_reply_ts["-100888:777"] = time.time() - 200
    assert s2._group_bare_gate(_msg(ChatType.SUPERGROUP)) is False


def test_pyrogram_enum_chat_type_recognized():
    """枚举形态 chat.type 必须被识别为群（str() 陷阱回归钉）。"""
    s = _stub()
    # 枚举 SUPERGROUP：未互动 → False 证明它走了「群」分支而非私聊放行
    assert s._group_bare_gate(_msg(ChatType.SUPERGROUP)) is False
    # 同参数字符串形态结果一致
    assert s._group_bare_gate(_msg("supergroup")) is False


def _fn_body(name: str) -> str:
    m = re.search(
        rf"(async )?def {name}\(.*?\n(?=    (async )?def |\Z)",
        TRIGGER_SRC, re.S)
    assert m, f"trigger.py 里找不到 {name}"
    return m.group(0)


def test_all_three_bare_paths_wired_to_gate():
    """源码级接线门禁：三条兜底路径必须都过 _group_bare_gate。"""
    for fn in ("_should_reply_by_follow_up_context",
               "_should_reply_by_l2_fallback",
               "_should_reply_by_ai_context"):
        body = _fn_body(fn)
        assert "_group_bare_gate" in body, f"{fn} 未接群兜底闸"


def test_explicit_paths_not_gated():
    """显式信号路径（回复链/@）不得被闸——被点名必须应答。"""
    for fn in ("_should_reply_by_reply_chain",):
        body = _fn_body(fn)
        assert "_group_bare_gate" not in body, f"{fn} 不应过群兜底闸"


def test_legacy_default_mode_is_not_always():
    """legacy 缺省 mode 必须是 mention_or_keyword（P3-2 事故回归钉）。

    实例基线 config 漂移丢 mode 键时，旧默认 always 会让群里每句都回，
    且在 _should_reply_to_group_message 里先于三条兜底闸短路放行——
    2026-07-25 灰度实测踩坑（用户点名「每句都回复」的主犯）。
    """
    body = _fn_body("_should_reply_with_legacy_method")
    assert "get('mode', 'always')" not in body, "legacy 缺省回退成 always 了"
    assert "'mention_or_keyword'" in body


def test_legacy_semantics_via_stub():
    """legacy 三档语义行为级验证（缺省/显式 always/关键词）。"""
    def _legacy(cfg_group_reply):
        s = SimpleNamespace(config={"telegram": {"group_reply": cfg_group_reply}})
        s._contains_mention = types.MethodType(
            TelegramTriggerMixin._contains_mention, s)
        s._contains_keyword = types.MethodType(
            TelegramTriggerMixin._contains_keyword, s)
        s._should_reply_with_legacy_method = types.MethodType(
            TelegramTriggerMixin._should_reply_with_legacy_method, s)
        return s

    msg = SimpleNamespace(text="好的👌🏻", caption=None)
    # 缺 mode（实例配置漂移场景）→ mention_or_keyword 语义 → 普通闲聊不回
    assert _legacy({})._should_reply_with_legacy_method(msg) is False
    # 显式 always（老部署故意配的）→ 尊重配置照回
    assert _legacy({"mode": "always"})._should_reply_with_legacy_method(
        msg) is True
    # 关键词命中仍回
    kw = _legacy({"mode": "mention_or_keyword", "keywords": ["客服"]})
    assert kw._should_reply_with_legacy_method(
        SimpleNamespace(text="找客服帮忙", caption=None)) is True
