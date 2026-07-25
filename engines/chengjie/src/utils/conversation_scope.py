"""ConversationScope —— 「账号 × 会话」作用域键的单一事实源（键 SSOT）。

历史上「这条消息属于哪个账号的哪个会话」在系统里有**三种即兴拼法**：
- A 线 / 收件箱草稿：裸 ``peer`` + account → ``acct:peer``（经 ``make_context_key``）；
- 协议线 autoreply：user_id 自拼 ``platform:acct:chat`` 再叠 account 前缀（双重前缀）；
- WhatsApp RPA：chat_key 自带 ``wa:acct:peer`` 再叠 account 前缀（双重前缀）。

键仍唯一（不串话），但「各链路各自拼键」是回归温床——proactive 记忆键失配（P0-1）
与 B 线生辰失配（P0-2）皆源于此。本模块自此成为**唯一出口**：新代码一律经
:class:`ConversationScope` 派生键；存量键格式在 :func:`legacy_key_formats` 登记并由
门禁锁定（``tests/test_conversation_scope.py``），防止静默漂移（键格式=数据格式，
改格式=改数据，必须走迁移）。

注意：本模块只做**纯函数派生**，不做 CPI canonical 化（那是 episodic 层的事，
经 ``SkillManager._episodic_storage_key`` 在 base key 之上叠加）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple

from src.utils.context_store import make_context_key


@dataclass(frozen=True)
class ConversationScope:
    """一个会话的完整作用域：平台 × 账号 × 会话（群聊另带 chat_id 维度）。

    - ``platform``：telegram / whatsapp / line / messenger / line_rpa …
    - ``account_id``：本方账号（协议号/RPA 号）；单号老路径 = "default"/""。
    - ``chat_key``：对端标识（私聊 peer id / RPA 线程键）。
    - ``chat_id``：群聊场景的群 id（私聊留空）。预留给「群聊/私聊分窗」（P1-2）。
    """

    platform: str
    account_id: str = ""
    chat_key: str = ""
    chat_id: str = ""

    # ── 构造 ────────────────────────────────────────────────────────────────

    @classmethod
    def from_conversation_id(cls, conversation_id: Any) -> "ConversationScope":
        """从收件箱 ``conv_id``（``platform:account_id:chat_key``）解析。

        与 ``src.inbox.normalizer.conv_id`` 互逆；解析不出的段留空。
        """
        parts = str(conversation_id or "").split(":", 2)
        if len(parts) == 3:
            return cls(platform=parts[0], account_id=parts[1], chat_key=parts[2])
        if len(parts) == 2:
            return cls(platform=parts[0], account_id="", chat_key=parts[1])
        return cls(platform="", account_id="", chat_key=str(conversation_id or ""))

    @classmethod
    def from_conv_row(cls, conv: Dict[str, Any]) -> "ConversationScope":
        """从收件箱会话行（dict）构造（缺 account 时从 conversation_id 补齐）。"""
        row = conv or {}
        acct = str(row.get("account_id") or "").strip()
        ck = str(row.get("chat_key") or "").strip()
        plat = str(row.get("platform") or "").strip()
        if (not acct or not plat or not ck) and row.get("conversation_id"):
            parsed = cls.from_conversation_id(row.get("conversation_id"))
            plat = plat or parsed.platform
            acct = acct or parsed.account_id
            ck = ck or parsed.chat_key
        return cls(platform=plat, account_id=acct, chat_key=ck)

    # ── 键派生（唯一出口） ────────────────────────────────────────────────────

    def context_key(self) -> str:
        """ContextStore 主键（= ``make_context_key`` 同语义：default/空账号裸键）。

        群聊（``chat_id`` 非空）自带 chat 维度：``acct:chat:peer``——同 peer 的
        群聊发言与私聊各开一窗，不互相污染（P1-2）。
        """
        base = self.chat_key
        if self.chat_id:
            base = f"{self.chat_id}:{self.chat_key}"
        return make_context_key(base, self.account_id)

    def memory_base_key(self) -> str:
        """episodic 记忆 base 键（CPI canonical 化之前的那一层）。

        与 ``SkillManager._episodic_storage_key`` 的 ``make_context_key(base, acct)``
        一步同源；memory scope 配置为 "user"（默认）时 base=chat_key。
        """
        return make_context_key(self.chat_key, self.account_id)

    def cooldown_key(self) -> str:
        """回复冷却键（``{chat}_{user}`` 语义 + 账号前缀，与 SkillManager 同口径）。"""
        return make_context_key(
            f"{self.chat_id or self.chat_key}_{self.chat_key}", self.account_id)

    def spoken_scope(self) -> str:
        """spoken_variant（口语版暂存）的作用域 = 账号（同号同文互取无实害）。"""
        acct = str(self.account_id or "").strip()
        return "" if acct == "default" else acct

    def conversation_id(self) -> str:
        """收件箱 conv_id（``platform:account_id:chat_key``，account 空归一 default）。"""
        return f"{self.platform}:{self.account_id or 'default'}:{self.chat_key}"

    def log_tag(self) -> str:
        """日志战术标签（Phase 3 观测统一格式）：``plat/acct/chat``。"""
        return f"{self.platform or '?'}/{self.account_id or 'default'}/{self.chat_key or '?'}"


def legacy_key_formats() -> Dict[str, Tuple[str, str]]:
    """存量键格式登记表（门禁锁定用）：{链路: (格式说明, 例子)}。

    这些格式下**已有生产数据**，改格式=改数据=必须走迁移脚本；门禁测试断言
    本表内容不被静默修改（改动者必须显式更新本表 + 迁移方案）。
    """
    return {
        "a_line_default": ("裸 peer（default 账号零前缀）", "5433982810"),
        "a_line_companion": ("acct:peer", "8244899900:5433982810"),
        "protocol_autoreply": (
            "acct:platform:acct:chat（user_id 自拼 + make_context_key 叠加，双重前缀）",
            "639270135480:whatsapp:639270135480:639273815533"),
        "whatsapp_rpa": (
            "acct:wa:acct:peer（chat_key 自带账号 + 叠加，双重前缀）",
            "worker1:wa:worker1:Alice"),
        "line_rpa": (
            "md5(line_rpa:{chat_key}) 数字化（P2-1 已接 ctx.account_id："
            "default=裸键不变，多号自动 acct: 前缀分桶）",
            "md5 hash int"),
    }
