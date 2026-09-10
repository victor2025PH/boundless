# -*- coding: utf-8 -*-
"""个人微信 PC 副驾 · 会话身份稳定（纯函数 + 小缓存，实施97 线 B）。

UIA 只给**显示名**（备注 / 昵称 / 群名），不给 wxid。改备注、重名、对方改昵称都会让 ``chat_key``
漂移——历史、人设绑定、配额记账全乱。这是 PC 读屏路线最容易被忽视、后果最重的一条。

分辨率优先级：
1. **微信号**（资料卡「微信号: xxx」/ 4.x 资料面板字段）→ ``wx:id:<微信号>``；驱动在首次见到会话时
   点开资料卡读一次并缓存（``ChatIdentityCache``）。
2. 对方隐藏微信号 → ``wx:name:<归一显示名>#<头像哈希前 8 位>``（头像哈希由驱动对头像位图算，
   缺则省略）。
3. 群聊 → ``wx:group:<归一群名>``（群名可改，但群成员数/头像拼图哈希可辅助；先按名）。

归一：去首尾空白、折叠内部空白、去掉 4.x 列表里附带的未读角标/时间/预览（这些会被拼进 Name）。
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

_WS_RE = re.compile(r"\s+")
#: 带标签形态（「微信号: xxx」）不再限最短长度——标签已是强信号；裸串形态仍按官方 6–20 位规则
_WXID_LINE_RE = re.compile(r"(?:微信号|WeChat ID|Weixin ID)\s*[:：]\s*([A-Za-z][-A-Za-z0-9_]{2,19})")
_WXID_RAW_RE = re.compile(r"^(?:wxid_[A-Za-z0-9_-]{6,}|[A-Za-z][-A-Za-z0-9_]{5,19})$")
#: 4.x 会话列表 ListItem 的 Name 常拼成「名字 时间 预览」或带角标数字；只取第一段作显示名
_LIST_ITEM_TAIL_RE = re.compile(r"\s+(?:\d{1,2}:\d{2}|昨天|星期[一二三四五六日天]|\d{4}/\d{1,2}/\d{1,2}|\d{1,2}/\d{1,2}).*$")


def normalize_display_name(name: Any) -> str:
    s = _WS_RE.sub(" ", str(name or "")).strip()
    s = _LIST_ITEM_TAIL_RE.sub("", s).strip()
    return s


#: 群聊标题带成员数：「群名 (12)」/「群名(12)」/「群名（12）」；私聊标题没有
_GROUP_TITLE_RE = re.compile(r"^(.*?)\s*[（(](\d{1,4})[)）]\s*$")


def parse_group_title(title: Any) -> Tuple[str, int]:
    """聊天标题 → (群名, 成员数)；没有「(N)」尾巴 → (原标题, 0)。"""
    s = normalize_display_name(title)
    m = _GROUP_TITLE_RE.match(s)
    if not m or not m.group(1).strip():
        return s, 0
    return m.group(1).strip(), int(m.group(2))


def is_group_title(title: Any, session_name: Any) -> bool:
    """会话格名字没有成员数、聊天标题有（「群名」 vs 「群名 (12)」）→ 群聊。

    备注本身带括号数字的联系人（「张三(2)」）两边一致，不会误判。
    """
    t = normalize_display_name(title)
    n = normalize_display_name(session_name)
    if not t or not n or t == n:
        return False
    gname, count = parse_group_title(t)
    return count > 0 and gname == n


def parse_wxid(text: Any) -> str:
    """从资料卡文本抽微信号（``微信号: abc_123``）；纯 wxid 串也接受。抽不到空串。"""
    s = str(text or "").strip()
    if not s:
        return ""
    m = _WXID_LINE_RE.search(s)
    if m:
        return m.group(1)
    if _WXID_RAW_RE.match(s) and not s.isdigit():
        return s
    return ""


def avatar_fingerprint(avatar_bytes: Optional[bytes]) -> str:
    if not avatar_bytes:
        return ""
    return hashlib.sha1(avatar_bytes).hexdigest()[:8]


def make_chat_key(*, display_name: str, wxid: str = "", avatar_fp: str = "",
                  is_group: bool = False) -> str:
    """稳定 chat_key（见模块头优先级）。全部为空 → 空串（调用方跳过该会话）。"""
    name = normalize_display_name(display_name)
    if is_group:
        return f"wx:group:{name}" if name else ""
    w = parse_wxid(wxid) or ""
    if w:
        return f"wx:id:{w}"
    if not name:
        return ""
    return f"wx:name:{name}#{avatar_fp}" if avatar_fp else f"wx:name:{name}"


def chat_key_kind(chat_key: str) -> str:
    s = str(chat_key or "")
    for k in ("id", "group", "name"):
        if s.startswith(f"wx:{k}:"):
            return k
    return ""


@dataclass
class _Entry:
    chat_key: str
    display_name: str
    wxid: str = ""
    avatar_fp: str = ""
    is_group: bool = False
    seen_at: float = field(default_factory=time.time)


class ChatIdentityCache:
    """显示名 ↔ chat_key 双向缓存（可落盘：``path`` 给定时微信号级条目持久化，重启后仍能把
    出站命令的 ``wx:id:<微信号>`` 映射回显示名去定位会话格）。

    ``resolve`` 在已有 wxid 级身份时永远返回它——对方改昵称/我改备注后新显示名会被**并入**
    同一 chat_key（调用方传 ``wxid`` 时），不裂会话；仅按名匹配到的老条目在名字变化后会被当新会话，
    直到驱动读到微信号。
    """

    def __init__(self, max_items: int = 5000, path: str = "") -> None:
        self._by_key: Dict[str, _Entry] = {}
        self._by_name: Dict[str, str] = {}
        self._max = max_items
        self._path = str(path or "")
        self._dirty = False
        if self._path:
            self._load()

    def _load(self) -> None:
        import json
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for item in (data if isinstance(data, list) else []):
                if not isinstance(item, dict):
                    continue
                key = str(item.get("chat_key") or "")
                if not key.startswith("wx:id:"):
                    continue
                ent = _Entry(key, str(item.get("display_name") or ""), key[len("wx:id:"):],
                             str(item.get("avatar_fp") or ""), False)
                self._put(ent)
        except Exception:
            pass

    def save(self) -> None:
        """落盘微信号级条目（best-effort；无路径/未变更＝no-op）。"""
        if not self._path or not self._dirty:
            return
        import json
        import os
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            rows = [{"chat_key": e.chat_key, "display_name": e.display_name, "avatar_fp": e.avatar_fp}
                    for e in self._by_key.values() if e.wxid]
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rows, fh, ensure_ascii=False)
            os.replace(tmp, self._path)
            self._dirty = False
        except Exception:
            pass

    def resolve(self, *, display_name: str, wxid: str = "", avatar_fp: str = "",
                is_group: bool = False) -> str:
        name = normalize_display_name(display_name)
        w = parse_wxid(wxid)
        if w:
            key = f"wx:id:{w}"
            ent = self._by_key.get(key)
            if ent is None:
                ent = _Entry(key, name, w, avatar_fp, is_group)
                self._put(ent)
                self._dirty = True
            elif name and ent.display_name != name:
                self._by_name.pop(ent.display_name, None)
                ent.display_name = name
                self._by_name[name] = key
                self._dirty = True
            ent.seen_at = time.time()
            self.save()
            return key
        if name and name in self._by_name:
            key = self._by_name[name]
            ent = self._by_key.get(key)
            if ent is not None:
                ent.seen_at = time.time()
                return key
        key = make_chat_key(display_name=name, avatar_fp=avatar_fp, is_group=is_group)
        if key:
            self._put(_Entry(key, name, "", avatar_fp, is_group))
        return key

    def display_name_for(self, chat_key: str) -> str:
        ent = self._by_key.get(str(chat_key or ""))
        return ent.display_name if ent else ""

    def wxid_keys_for_name(self, display_name: str) -> List[str]:
        """该显示名对应的全部微信号级 chat_key（≥2 即「同名不同号」，此后对这个名字永远逐格核对）。"""
        name = normalize_display_name(display_name)
        return [e.chat_key for e in self._by_key.values() if e.wxid and e.display_name == name]

    def needs_wxid_lookup(self, chat_key: str) -> bool:
        """是否还没拿到微信号级身份（驱动据此决定要不要点开资料卡）。"""
        ent = self._by_key.get(str(chat_key or ""))
        return bool(ent) and not ent.is_group and not ent.wxid

    def _put(self, ent: _Entry) -> None:
        if len(self._by_key) >= self._max:
            oldest = min(self._by_key.values(), key=lambda e: e.seen_at)
            self._by_key.pop(oldest.chat_key, None)
            self._by_name.pop(oldest.display_name, None)
        self._by_key[ent.chat_key] = ent
        if ent.display_name:
            self._by_name[ent.display_name] = ent.chat_key

    def __len__(self) -> int:
        return len(self._by_key)


__all__ = [
    "normalize_display_name", "parse_wxid", "avatar_fingerprint", "make_chat_key",
    "chat_key_kind", "ChatIdentityCache",
]
