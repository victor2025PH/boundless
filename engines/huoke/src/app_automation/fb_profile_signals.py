# -*- coding: utf-8 -*-
"""
Facebook 资料页 — 与 UI hierarchy 相关的轻量启发式（无 u2 依赖）。

供 ``facebook.FacebookAutomation``、W0 脚本、测试共用，避免多处拷贝漂移。
不包含「举报对话框」等脚本侧语义；调用方在需要时自行先过滤。
"""

from __future__ import annotations


def is_likely_fb_profile_page_xml(x: str) -> bool:
    """从 dump 得到的 hierarchy 文本判断当前是否**像**个人资料页（非精确）。"""
    if not x:
        return False
    low = x.lower()
    if "profile_actionbar" in low or "profile_header" in low:
        return True
    if "com.facebook.katana:id/profile_" in low:
        return True
    if any(s in low for s in ("add friend", "message", "follow")):
        return True
    # 2026-04-24 v3: zh-CN FB katana profile markers
    zh_markers = (
        "加好友",
        "发消息",
        "添加好友",
        "取消好友申请",
    )
    if any(s in x for s in zh_markers):
        return True
    return any(
        s in x
        for s in (
            "\u53cb\u9054\u3092\u8ffd\u52a0",  # 友達を追加
            "\u30e1\u30c3\u30bb\u30fc\u30b8",  # メッセージ
            "\u30d5\u30a9\u30ed\u30fc",  # フォロー
        )
    )
