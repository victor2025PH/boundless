# -*- coding: utf-8 -*-
"""Messenger 收件箱读取 —— 判定逻辑纯函数层（P0 2026-08-15）。

把散落在 ``facebook.py`` 里的启发式（未读判定 / 消息方向 / 最新入站提取 /
标题匹配 / 去重指纹）抽成**无副作用纯函数**，理由：

1. **可离线夹具回归**：输入 dump 出来的结构化数据 → 输出判定，不碰真机，
   与本仓「纯函数 + 常驻门禁」哲学一致。Messenger 每次改版只要录一份
   XML 夹具即可回归，不必线上踩雷。
2. **单一事实源**：主 inbox / Message Requests 两条链路共用同一套判定，
   避免「各处口径不一致」。
3. **可观测**：每个判定都返回 reason，上层可把「为什么判未读 / 为什么跳过」
   计数进 stats，让「漏读」第一次可度量。

⚠ 三类历史事故对应关系（见 messenger-reading-optimization 分析）：
  * 「消息不读」：``detect_unread`` 修掉「``selected`` 恒 False」的假信号，
    并把 content-desc 未读关键词（多语 + 「N 条新消息」数字模式）纳入判定。
  * 「人名错乱」：``names_match`` 支撑「开会话后回读标题校验」，点错行即现形。
  * 消息归属错：``looks_outgoing`` 用**前缀**判自己发的消息，替代原来把
    ``"you"`` 当子串过滤（会误杀 "thank you" 整条 → 漏读对方消息）。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence


# ─────────────────────────────────────────────────────────────────────
# 未读判定
# ─────────────────────────────────────────────────────────────────────

# 未读圆点/红点（Messenger 未读经典标记）。刻意只认「前导」圆点，
# 不认名字中间的 "•"（那是「名字 • 时间」分隔符，认了会全部误判未读）。
_UNREAD_DOTS = ("•", "●", "⬤", "🔵", "🔴", "🔵")

# content-desc 里的未读关键词（多语）。Litho 渲染的会话行常把整行无障碍
# 描述合成成 "山田花子, 3 条新消息, 5 分钟前" / "Alice, 2 unread messages"。
_UNREAD_TEXT_PATTERNS = (
    re.compile(r"unread", re.I),
    re.compile(r"\d+\s*new\s+messages?", re.I),
    re.compile(r"未读|未讀"),
    re.compile(r"\d+\s*条\s*(?:新|未读)"),
    re.compile(r"\d+\s*則"),
    re.compile(r"新着"),          # 日: 新着メッセージ
    re.compile(r"未読"),          # 日: 未読
    re.compile(r"읽지\s*않"),      # 韩: 읽지 않은
    re.compile(r"\d+\s*개.*메시지"),  # 韩: N개 메시지
)


@dataclass
class UnreadVerdict:
    is_unread: bool
    reason: str = ""


def detect_unread(name_text: str,
                  row_desc: str = "",
                  sibling_texts: Sequence[str] = (),
                  selected: bool = False) -> UnreadVerdict:
    """综合判定一个会话行是否未读。

    信号（任一命中即未读，附 reason 便于观测）：
      1. ``selected=True``（Android 选中态，修复前恒 False）
      2. 名字**前导**未读圆点（"• 山田" 而非 "山田 • 5分钟"）
      3. 行 content-desc / 兄弟节点文本含未读关键词（多语 + 数字模式）
      4. 兄弟节点是一个独立的未读圆点 view

    刻意保守：找不到任何未读信号返回 ``False``，由上层决定是否走
    兜底（如预览指纹变化，属 P1）。宁可上层多一个兜底，也不在这里
    「name 含 • 就判未读」——那正是原实现把分隔符误当未读的坑。
    """
    if selected:
        return UnreadVerdict(True, "selected")

    name = (name_text or "").strip()
    if name[:1] in _UNREAD_DOTS:
        return UnreadVerdict(True, "lead_dot")

    blob = " ".join(t for t in [row_desc or "", *sibling_texts] if t)
    for pat in _UNREAD_TEXT_PATTERNS:
        if pat.search(blob):
            return UnreadVerdict(True, "kw")

    for t in sibling_texts:
        ts = (t or "").strip()
        if ts and ts in _UNREAD_DOTS:
            return UnreadVerdict(True, "sib_dot")

    return UnreadVerdict(False, "")


# ─────────────────────────────────────────────────────────────────────
# 消息方向（自己发的 vs 对方发的）
# ─────────────────────────────────────────────────────────────────────

# 「自己发的」前缀（无障碍描述 / 预览常见）。用**前缀**匹配（strip 后
# startswith），绝不做子串——原实现把 "you" 当子串过滤，"thank you"
# 整条被丢，是实打实的漏读源。
_OUTGOING_PREFIXES = (
    "you: ", "you sent", "you replied", "you:", "you 发",
    "你: ", "你：", "你发送", "你發送", "你回复", "你回覆",
    "已发送", "已發送", "我: ", "我：",
    "あなた: ", "あなた：", "自分: ",     # 日
    "나: ", "나：",                        # 韩
)


def looks_outgoing(text: str, desc: str = "") -> bool:
    """判断一条气泡/预览是否是**自己**发出的。

    仅用前缀，不用子串，避免误杀正文含代词的对方消息。
    """
    for raw in (text or "", desc or ""):
        s = raw.strip().lower()
        if not s:
            continue
        for pref in _OUTGOING_PREFIXES:
            if s.startswith(pref.lower()):
                return True
    return False


# ─────────────────────────────────────────────────────────────────────
# 最新入站消息提取
# ─────────────────────────────────────────────────────────────────────

# 会话页 UI 噪声：完全等于某词（按钮）或包含某特异英文短语才剔除。
# 刻意不放泛化中文词（如「发送」——对方消息可能含「我给你发送了…」）。
_UI_NOISE_EXACT = {
    "send", "发送", "發送", "send message", "aa", "gif", "回复", "回覆",
    "reply", "like", "赞", "讚", "转发", "轉發", "forward", "translate",
    "查看翻译", "みる", "送信",
}
_UI_NOISE_SUBSTR = (
    "type a message", "message…", "aa message", "active now",
    "type a", "text message", "you're friends on facebook",
    "say hi", "wave", "start of your chat",
)


def _is_cjk_char(ch: str) -> bool:
    o = ord(ch)
    return (
        0x4E00 <= o <= 0x9FFF     # CJK 统一
        or 0x3040 <= o <= 0x30FF  # 日文假名
        or 0xAC00 <= o <= 0xD7A3  # 韩文音节
        or 0x3400 <= o <= 0x4DBF  # CJK 扩展 A
    )


def _is_meaningful_message(text: str) -> bool:
    """过短判定：CJK 单字（好/嗯/在/はい）保留，单个 ASCII/符号丢弃。"""
    s = (text or "").strip()
    if len(s) >= 2:
        return True
    if len(s) == 1 and _is_cjk_char(s):
        return True
    return False


@dataclass
class BubbleRow:
    """一条候选气泡：top=垂直位置(越大越靠下=越新)，x_center=水平中心。"""
    top: int
    x_center: float
    text: str
    desc: str = ""


def is_ui_noise(text: str) -> bool:
    s = (text or "").strip().lower()
    if not s:
        return True
    if s in _UI_NOISE_EXACT:
        return True
    for sub in _UI_NOISE_SUBSTR:
        if sub in s:
            return True
    return False


def pick_latest_incoming(rows: Sequence[BubbleRow], screen_w: int) -> str:
    """从会话页气泡里挑「对方最新一条」文本。

    改进点（相对原 ``_extract_latest_incoming_message``）：
      * 方向：先看 ``looks_outgoing``（前缀）显式排除自己发的；
        content-desc 明确标了对方发送则直接采信（不再被几何误伤）。
      * 移除 "you" 子串过滤：不再误杀 "thank you"。
      * 过短放宽：CJK 单字消息（好/在/はい）不再被丢。
      * 几何仅作**兜底**：无任何方向信号时，才用「x 中心 > 60% 屏宽 =
        自己（靠右）」这个弱假设。
    """
    if screen_w <= 0:
        screen_w = 1080
    best: Optional[BubbleRow] = None
    for r in rows:
        text = (r.text or "").strip()
        if not _is_meaningful_message(text):
            continue
        if is_ui_noise(text):
            continue
        if looks_outgoing(text, r.desc):
            continue
        desc_low = (r.desc or "").strip().lower()
        # content-desc 明确「对方发来」→ 直接采信，跳过几何
        incoming_by_desc = bool(
            desc_low and ("sent" in desc_low or "发来" in desc_low
                          or "发送了" in desc_low or "message from" in desc_low)
        )
        if not incoming_by_desc and r.x_center > screen_w * 0.6:
            # 无方向信号 + 明显靠右 → 视为自己发的，兜底跳过
            continue
        if best is None or r.top > best.top:
            best = r
    return (best.text.strip()[:500] if best else "")


# ─────────────────────────────────────────────────────────────────────
# 会话页标题（进入会话后回读，用于校验点对了行没有）
# ─────────────────────────────────────────────────────────────────────

@dataclass
class TitleCandidate:
    top: int
    x_left: int
    text: str


def pick_thread_title(candidates: Sequence[TitleCandidate],
                      screen_h: int,
                      valid_name_fn=None) -> str:
    """从会话页顶部 toolbar 区域挑对方名字。

    启发式：只看顶部 ``top < screen_h * 0.18`` 区域内的候选，取最靠上
    （top 最小）的一个合法名字。找不到返回 ""（上层退回列表名，不误纠正）。

    ``valid_name_fn``：注入 ``_is_valid_peer_name``（避免本模块反依赖
    facebook.py）。为 None 时用内置弱校验。
    """
    if screen_h <= 0:
        screen_h = 2400
    band = screen_h * 0.18
    is_valid = valid_name_fn or _basic_name_ok
    picked: Optional[TitleCandidate] = None
    for c in candidates:
        if c.top > band:
            continue
        name = (c.text or "").strip()
        if not is_valid(name):
            continue
        if picked is None or c.top < picked.top:
            picked = c
    return picked.text.strip() if picked else ""


def _basic_name_ok(s: str) -> bool:
    s = (s or "").strip()
    if len(s) < 2 or len(s) > 40:
        return False
    if s.isdigit():
        return False
    return True


# ─────────────────────────────────────────────────────────────────────
# 名字归一化 + 匹配（标题校验用）
# ─────────────────────────────────────────────────────────────────────

_NAME_STRIP_RE = re.compile(r"[\s\u200b\u200e\u200f•·・,，、.。!！?？…]+")


def normalize_name(s: str) -> str:
    """归一化名字用于比较：去空白/零宽/分隔标点，小写。"""
    s = (s or "").strip()
    s = _NAME_STRIP_RE.sub("", s)
    return s.lower()


def names_match(a: str, b: str) -> bool:
    """列表名 vs 会话内标题是否指向同一人。

    宽松匹配（一方是另一方前缀 / 包含）以吸收「列表截断名 vs 全名」
    「带/不带称谓后缀」这类差异；两边都空视为不匹配（无从校验）。
    """
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # 列表名常被截断（"山田花..." vs "山田花子"）→ 前缀/包含即算同一人
    if len(na) >= 3 and (na in nb or nb in na):
        return True
    if len(nb) >= 3 and (nb in na or na in nb):
        return True
    return False


# ─────────────────────────────────────────────────────────────────────
# 去重指纹（同一条消息被重复打开/重复入库）
# ─────────────────────────────────────────────────────────────────────

def message_fingerprint(peer_name: str, text: str) -> str:
    """(peer, 文本) → 短哈希。用于「最近是否已入库同一条」去重判断。"""
    base = f"{normalize_name(peer_name)}\x1f{(text or '').strip()}"
    return hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()[:16]


def name_from_desc(desc: str) -> str:
    """content-desc 第一段（逗号/顿号前）当名字源，供 text 为空的行兜底。"""
    d = (desc or "").strip()
    if not d:
        return ""
    for sep in (",", "，", "、", "·", "•", "  "):
        if sep in d:
            return d.split(sep, 1)[0].strip()
    return d


# ─────────────────────────────────────────────────────────────────────
# P1 (2026-08-15): 预览内容核提取（增量检测「消息不读」的兜底信号）
# ─────────────────────────────────────────────────────────────────────
#
# 思路 = messenger-web 的 `seen` 预览指纹地图移植到设备端：不依赖易变的
# 未读 UI 表示，「这行的预览内容变了」就是有新活动的语义信号。
# 难点是 content-desc 里混着时间段（"5分钟前"→"6分钟前" 每轮都变），
# 不剔除会把变化检测变成噪声发生器 —— 所以指纹只对「内容核」计算。

_TIME_SEGMENT_RES = (
    re.compile(r"^\d{1,2}:\d{2}(\s*(am|pm|上午|下午))?$", re.I),
    re.compile(r"^\d+\s*(分钟前|分鐘前|小时前|小時前|天前|周前|週前|秒前)$"),
    re.compile(r"^\d+\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w)(\s+ago)?$", re.I),
    re.compile(r"^(刚刚|剛剛|昨天|昨日|今天|现在|現在|now|just now|yesterday|today)$", re.I),
    re.compile(r"^(mon|tue|wed|thu|fri|sat|sun)(day|sday|nesday|rsday|urday)?$", re.I),
    re.compile(r"^(周|週|星期|礼拜|禮拜)[一二三四五六日天]$"),
    re.compile(r"^\d{1,2}[月/\-]\d{1,2}[日号號]?$"),
    re.compile(r"^\d+\s*(分|時間|時|日)前$"),      # 日: 5分前 / 3時間前
    re.compile(r"^(いま|昨日|今日)$"),
)


def looks_time_segment(seg: str) -> bool:
    """一个片段是否是「时间/时刻」（预览指纹必须剔除的抖动源）。"""
    s = (seg or "").strip().rstrip("。.")
    if not s or len(s) > 24:
        return False
    for pat in _TIME_SEGMENT_RES:
        if pat.match(s):
            return True
    return False


_PREVIEW_SPLIT_RE = re.compile(r"[,，、·•]+")


def preview_core(name: str, desc: str) -> str:
    """从会话行 content-desc 提取「内容核」（剔名字段 + 时间段 + 未读计数段）。

    content-desc 典型形态 "山田花子, 你好呀, 5分钟前" / "Alice, 2 unread
    messages, photo, 3m"。返回稳定的内容部分；两次读取内容核一致 ⇒ 没有
    新消息（即使时间段在变）。全部段都被剔掉/解析不出 → 返回 ""（上层
    视为无信号，绝不误报变化）。
    """
    d = (desc or "").strip()
    if not d:
        return ""
    segs = [s.strip() for s in _PREVIEW_SPLIT_RE.split(d) if s.strip()]
    if not segs:
        return ""
    core_parts: List[str] = []
    for i, seg in enumerate(segs):
        # 剔名字段（通常第一段；后续段落若逐字等于名字也剔）
        if names_match(seg, name):
            continue
        # 剔时间段
        if looks_time_segment(seg):
            continue
        # 剔「未读计数」段（那是状态不是内容——已由未读判定消费；
        # 留在指纹里会让「已读→未读」翻转触发两次变化）
        if any(p.search(seg) for p in _UNREAD_TEXT_PATTERNS):
            continue
        core_parts.append(seg)
    return " ".join(core_parts).strip()


def preview_fingerprint(name: str, desc: str) -> str:
    """会话行预览指纹：内容核非空才有指纹（"" = 无信号）。"""
    core = preview_core(name, desc)
    if not core:
        return ""
    return message_fingerprint(name, core)
