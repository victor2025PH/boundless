# -*- coding: utf-8 -*-
"""Messenger 系统通知预扫 —— 判定纯函数层（P1 2026-08-15）。

**方案演进说明**（相对上一阶段规划的「通知监听 helper APK」）：
上阶段建议用 `NotificationListenerService` helper APK 读 MessagingStyle。深入
后发现设备端有一条**更轻、零新增 APK**的等价路径 —— `adb shell dumpsys
notification --noredact` 直接吐出系统里所有活动通知的 extras（含
`android.title` = 会话名 / `android.text` = 最新预览），本进程已有 adb 通道，
无需安装/授权任何 App，也**不打开会话 → 零已读回执**。

**定位（重要，别当成完整读取器）**：通知是**只增信号**——「通知里有这个
peer = 一定有新活动，必须打开读」，但「通知里没有 ≠ 没新消息」（通知会被系统/
用户清除、MIUI 会折叠或 redact）。所以它的角色是**在打开 App 之前预扫**，把
「未读判定漏判 / 不在首屏」的会话捞回来强制打开，与预览指纹增量正交叠加，共同
把「消息不读」的召回顶上去。任何解析失败一律返回空 → 退化到纯 UI 读取，绝不
阻塞主链。

dumpsys 输出格式随 Android 版本/ROM 漂移严重，故解析用宽松多候选正则 + 逐块
防御，且所有判定抽成纯函数便于离线夹具回归。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

# Messenger 家族包名（orca=主 App / mlite=Lite / katana=FB 主 App 内消息）
MESSENGER_PACKAGES = (
    "com.facebook.orca",
    "com.facebook.mlite",
    "com.facebook.katana",
)

# FLAG_GROUP_SUMMARY = 0x00000200 —— 该 flag 的通知是「N 条汇总」不是单条会话
_FLAG_GROUP_SUMMARY = 0x200

# 汇总/系统类 title（不是真实 peer），多语
_SUMMARY_TITLE_RES = (
    re.compile(r"^\d+\s*(条|則|条新|new)\b", re.I),
    re.compile(r"messenger$", re.I),
    re.compile(r"^\d+\s*(new\s+messages?|messages?)$", re.I),
    re.compile(r"^(chats?|messages?)$", re.I),
    re.compile(r"新着メッセージ"),
)

_RECORD_SPLIT_RE = re.compile(r"NotificationRecord\(")
_PKG_RE = re.compile(r"pkg=([A-Za-z0-9_.]+)")
_KEY_RE = re.compile(r"key=(\S+)")
_FLAGS_RE = re.compile(r"\bflags=0x([0-9a-fA-F]+)")
# extras 里的键值：android.title=山田花子 (String)  /  android.text=你好呀
_EXTRA_RES = {
    "title": re.compile(r"android\.title=(.*?)(?:\s+\(String\)|\s*\n|$)"),
    "text": re.compile(r"android\.text=(.*?)(?:\s+\(String\)|\s*\n|$)"),
    "bigText": re.compile(r"android\.bigText=(.*?)(?:\s+\(String\)|\s*\n|$)"),
    "subText": re.compile(r"android\.subText=(.*?)(?:\s+\(String\)|\s*\n|$)"),
    "conversationTitle": re.compile(
        r"android\.conversationTitle=(.*?)(?:\s+\(String\)|\s*\n|$)"),
}


@dataclass
class NotifRecord:
    package: str = ""
    key: str = ""
    title: str = ""
    text: str = ""
    flags: int = 0
    is_group_summary: bool = False


@dataclass
class PeerActivity:
    """一个有活动通知的 peer：预扫据此强制打开读取。"""
    peer: str
    text: str = ""
    key: str = ""


def _clean_extra(v: str) -> str:
    s = (v or "").strip()
    # dumpsys 有时给 null / 空对象引用
    if s in ("null", "", "Null"):
        return ""
    return s


def _is_summary_title(title: str) -> bool:
    t = (title or "").strip()
    if not t:
        return True
    for pat in _SUMMARY_TITLE_RES:
        if pat.search(t):
            return True
    return False


def parse_dumpsys_notifications(
        raw: str,
        packages: Sequence[str] = MESSENGER_PACKAGES) -> List[NotifRecord]:
    """从 `dumpsys notification --noredact` 文本解析出目标包的通知记录。

    宽松防御：格式不符/字段缺失的块跳过；绝不抛。
    """
    if not raw or not raw.strip():
        return []
    pkgset = set(packages or MESSENGER_PACKAGES)
    out: List[NotifRecord] = []
    blocks = _RECORD_SPLIT_RE.split(raw)
    for blk in blocks[1:]:  # blocks[0] 是首个 record 之前的头部
        mpkg = _PKG_RE.search(blk)
        if not mpkg or mpkg.group(1) not in pkgset:
            continue
        rec = NotifRecord(package=mpkg.group(1))
        mkey = _KEY_RE.search(blk)
        if mkey:
            rec.key = mkey.group(1).rstrip(")")
        mflags = _FLAGS_RE.search(blk)
        if mflags:
            try:
                rec.flags = int(mflags.group(1), 16)
            except ValueError:
                rec.flags = 0
        rec.is_group_summary = bool(rec.flags & _FLAG_GROUP_SUMMARY) or (
            "GROUP_SUMMARY" in blk)
        mt = _EXTRA_RES["title"].search(blk)
        if mt:
            rec.title = _clean_extra(mt.group(1))
        for tk in ("text", "bigText", "subText"):
            mx = _EXTRA_RES[tk].search(blk)
            if mx and _clean_extra(mx.group(1)):
                rec.text = _clean_extra(mx.group(1))
                break
        out.append(rec)
    return out


def extract_active_peers(
        records: Sequence[NotifRecord],
        valid_name_fn: Optional[Callable[[str], bool]] = None,
        ) -> List[PeerActivity]:
    """从通知记录归一化出「有活动的 peer 列表」。

    剔除：group summary、汇总/系统 title、无效名字。同 peer 去重（保留最后
    出现的 = 通常最新）。返回顺序按首次出现，便于稳定测试。
    """
    is_valid = valid_name_fn or (lambda s: bool((s or "").strip()) and len(s.strip()) >= 2)
    by_peer = {}
    order: List[str] = []
    for rec in records:
        if rec.is_group_summary:
            continue
        name = (rec.title or "").strip()
        if _is_summary_title(name):
            continue
        if not is_valid(name):
            continue
        if name not in by_peer:
            order.append(name)
        by_peer[name] = PeerActivity(peer=name, text=rec.text or "",
                                     key=rec.key or "")
    return [by_peer[n] for n in order]


def active_peer_names(records: Sequence[NotifRecord],
                      valid_name_fn: Optional[Callable[[str], bool]] = None,
                      ) -> set:
    """便捷：只要有活动的 peer 名字集合（供 UI 列表交叉强制打开）。"""
    return {pa.peer for pa in extract_active_peers(records, valid_name_fn)}


def offscreen_search_targets(notif_peer_names: Sequence[str],
                             listed_names: Sequence[str],
                             max_opens: int) -> List[str]:
    """P2: 选出「通知里有活动、但不在当前列表可见项」的屏外 peer, 供搜索打开。

    纯函数, 便于离线测限流/去重/顺序:
      * 只取 ``notif`` 有、``listed`` 没有的 peer（列表里有的已由 should_open 处理）；
      * 去重且保序（稳定, 测试可断言）；
      * 限流到 ``max_opens``（搜索是主动 UI 操作, 有风控成本, 必须封顶）。
    """
    if max_opens <= 0:
        return []
    listed = set(listed_names or ())
    out: List[str] = []
    seen = set()
    for p in notif_peer_names or ():
        if not p or p in listed or p in seen:
            continue
        seen.add(p)
        out.append(p)
        if len(out) >= max_opens:
            break
    return out
