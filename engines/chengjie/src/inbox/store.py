"""InboxStore — 统一收件箱 SQLite 持久层。

设计参考 src/contacts/store.py：
- 单进程单 connection + threading.Lock
- WAL + busy_timeout + row_factory=Row
- 多表 DDL 一次 executescript
- 幂等 migration（PRAGMA table_info + ALTER TABLE ADD COLUMN）

四张表：
- conversations        跨平台会话事实源（ingest 写）
- messages             统一消息（去重靠确定性 message_id 主键）
- message_analysis     意图/情绪/风险（Phase C 写）
- conversation_settings 运营态配置（automation_mode）——与 ingest 解耦，
                        ingest 永不触碰，修掉「automation_mode 进程内 dict 重启即丢」

关键不变量：
1. ingest 只写 conversations 的事实列，绝不动 conversation_settings。
2. messages 主键确定性生成（有 platform_msg_id 用之，否则 hash(text|ts)），
   INSERT OR IGNORE 天然幂等，重复轮询不重复入库。
3. conversations.last_ts 单调不回退：旧的 fetch 不覆盖更新的 last_text/last_ts。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import InboxConversation, InboxMessage, MessageAnalysis

logger = logging.getLogger(__name__)

AUTOMATION_MODES = {"manual", "review", "multi_choice", "auto_ai"}
_DEFAULT_AUTOMATION_MODE = "review"

# P1-⑧ 激活里程碑「首条真实出站」的进程级一次性闸（ingest 是热路径，标志位短路
# 保证稳态零成本；重复上报对按指纹去重的漏斗只是噪声）
_FIRST_REPLY_NOTED = False


def _note_first_reply_milestone(msg: "InboxMessage") -> None:
    """本进程首条**新鲜**出站消息 → 激活漏斗 first_reply 里程碑。

    新鲜度闸（10 分钟）：目录同步会把手机历史会话整批灌进来，旧出站不代表
    「这台新装机真的用起来了」；只有 ts≈now 的出站才算数。beacon 未装（server
    部署/测试）时 note_milestone 自身 no-op。任何异常吞掉且不再重试。
    """
    global _FIRST_REPLY_NOTED
    if _FIRST_REPLY_NOTED:
        return
    try:
        if abs(time.time() - float(msg.ts or 0)) > 600:
            return
        _FIRST_REPLY_NOTED = True
        from src.utils.telemetry_beacon import note_milestone
        platform = str(msg.conversation_id or "").split(":", 1)[0]
        note_milestone("first_reply", f"platform={platform}")
    except Exception:  # noqa: BLE001
        _FIRST_REPLY_NOTED = True

# 计费席位口径：draft_audit_log.agent_id 里有「机器/系统」actor（L2 自动发送
# worker、SLA 看门狗、工作链、无登录用户回退），它们不是人工坐席，不应计入授权
# 席位（否则会把自动化跑量误判成 over_seats 触发计费红灯告警）。统计活跃坐席时排除。
_NON_BILLABLE_AGENT_IDS = ("autosend_worker", "system")

# 好友名单「沉默」判定阈值（天）：聊过但超过这么久没动静 = 需要激活的存量线索。
# 做成常量而非 SQL 魔数，便于运营侧调档（30 天 ≈ 一个自然运营周期）。
PROTOCOL_CONTACT_SILENT_DAYS = 30.0

# 「永久搁置」哨兵时间戳 = 2100-01-01 00:00:00 UTC。
# 刻意用远期有限值而非 +inf / 新列：复用现有 snooze_until REAL 列与全部读路径
# （snoozed_ids/now 过滤、SLA/升级/红点排除、排序索引）零改动即工作；且 FastAPI 的
# JSONResponse(allow_nan=False) 序列化 inf 会 500，有限哨兵天然可序列化。
# 语义：永久搁置≠静音——ingest 侧客户再来消息仍会 clear_snooze 立即重浮（这正是
# 与「归档」的分界：归档连客户回复都不回来）。前端镜像常量见 unified_inbox.html。
SNOOZE_FOREVER_TS = 4102444800.0


def is_permanent_snooze(until_ts: Any) -> bool:
    """该 snooze_until 是否表示「永久搁置」（>= 哨兵值；容忍浮点毫厘）。"""
    try:
        return float(until_ts or 0) >= SNOOZE_FOREVER_TS - 1.0
    except (TypeError, ValueError):
        return False


def _contact_query_clause(query: str) -> Tuple[str, List[Any]]:
    """好友名单模糊匹配子句（name / notify_name / chat_key），空 query → 无子句。

    转义 LIKE 通配符：客户名里出现 ``%`` / ``_`` 时不该被当成通配符（``\\`` 先转，
    否则会把后面补的转义符再次转义）。
    """
    q = str(query or "").strip().lower()
    if not q:
        return "", []
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{esc}%"
    clause = ("(LOWER(c.name) LIKE ? ESCAPE '\\' "
              "OR LOWER(c.notify_name) LIKE ? ESCAPE '\\' "
              "OR LOWER(c.chat_key) LIKE ? ESCAPE '\\')")
    return clause, [like, like, like]


# Telegram 的「非人」固定 id：全网通用，与账号无关。
#   777000      官方服务号（登录验证码 / 账号安全通知）
#   42777       Telegram Notifications（另一个官方通知号）
#   1087968824  @GroupAnonymousBot（群匿名发言的代理身份，不是某个人）
# 全仓**唯一**一份名单：管道安全（telegram_client）、主动触达抑制（proactive_topic）、
# 联系人展示与目录同步（本模块）三条链路都读它。此前是三份各自维护且互相有缺口的
# 硬编码——两处漏 Saved Messages、一处漏 42777，谁也不知道自己漏了什么。
TELEGRAM_SERVICE_CHAT_KEYS = frozenset({"777000", "42777", "1087968824"})

# 兼容旧名（曾只有服务号一条规则时的常量名）。
TELEGRAM_SERVICE_NOTIFICATION_ID = "777000"


def is_system_peer(platform: str, account_id: str, chat_key: str) -> bool:
    """该 peer 是否为平台**系统条目**（不是人，不该进客户名单）。

    读侧（联系人面板取数）与写侧（目录同步落库）共用这一个判定，避免「界面滤掉了
    但库里照样堆噪音」/「两侧口径漂移」——所以它放在 store 模块顶层、不带任何
    self 依赖。

    当前只认两条 Telegram 规则（其余平台一律 False）：

    - ``chat_key == account_id``：Saved Messages（收藏夹，「自己发给自己」的云
      笔记）。本仓 Telegram 的 ``account_id`` 就是该账号自己的 TG user id，故
      两者相等即可判定，不必额外 RPC 查 self。
    - ``chat_key`` 落在 ``TELEGRAM_SERVICE_CHAT_KEYS``：官方通知号与匿名代理身份。

    **刻意不过滤 bot**：bot 只有真给你发过消息才会出现在会话列表里，「要不要联系
    某个 bot」是产品决策而不是脏数据——有人就是拿 bot 当工作入口。宁可留着让人
    自己忽略，也不替用户做删名单的决定（漏过一条噪音可恢复，误删一个真实往来对象
    在面板上就是「人凭空消失」，无从察觉）。
    """
    platform = str(platform or "").lower()
    chat_key = str(chat_key or "")
    if platform != "telegram" or not chat_key:
        return False
    if chat_key in TELEGRAM_SERVICE_CHAT_KEYS:
        return True
    # account_id 为空时不判 Saved Messages：空账号名下 chat_key 也不会是空串
    # （上面已挡），这里只是防「'' == ''」式的意外全命中。
    account_id = str(account_id or "")
    return bool(account_id) and chat_key == account_id


def _system_peer_where(platform: str, account_id: str) -> Tuple[List[str], List[Any]]:
    """``is_system_peer`` 的 SQL 对应物（子句 + 按出现顺序排好的参数）。

    与纯函数同源、同规则：非 telegram 平台返回空子句（**任何**其他平台的 chat_key
    哪怕恰好等于 account_id 或 '777000' 都不受影响）。返回值是「子句列表 + 参数
    列表」的配对，调用方只要保证 ``append`` 顺序一致，绑定就不会错位。
    """
    if platform != "telegram":
        return [], []
    # sorted() 而非直接迭代 frozenset：集合迭代序随进程哈希种子变化，排序后生成的
    # SQL 文本才是确定的（语义无差别，但可断言、diff 可读）。
    keys = sorted(TELEGRAM_SERVICE_CHAT_KEYS)
    clauses = ["c.chat_key NOT IN ({})".format(",".join("?" for _ in keys))]
    params: List[Any] = list(keys)
    if account_id:
        clauses.append("c.chat_key <> ?")
        params.append(account_id)
    return clauses, params


def _contacts_base_from(
    platform: str, account_id: str, include_chats: bool,
) -> Tuple[str, List[Any], List[str], List[Any]]:
    """「联系人」取数的基表 + 会话联表 FROM 块（列表与汇总两个方法共用）。

    返回 ``(from_sql, from_params, base_where, base_where_params)``。**刻意把 SQL
    文本和「按文本里 ``?`` 出现先后排好的参数」一起返回**：并集版与通讯录版的
    ``?`` 顺序不同（并集版的基表参数出现在 JOIN 前缀 **之前**），两个调用点各自
    手拼极易错位——而 SQLite 对错位绑定不报错，只会静默返回错数据。同理，UNION
    只在这里写一次，不给第二份拷贝留漂移空间。

    两种口径都在 ``base_where`` 里排除**系统 peer**（见 ``is_system_peer``）：
    Saved Messages 与 Telegram 官方服务号是 private 类型，不挡就会实打实出现在
    联系人面板里，并污染「好友总数 / 未开口数」这些运营盘点数字。挡在这里而不是
    各方法自己加，是为了让列表与汇总永远同集合。

    ``include_chats=False``（默认）：基表＝``protocol_contacts``，与本函数引入前
    逐字符一致——现有调用方（旧前端 / ops 卡）的向后兼容是硬约束。

    ``include_chats=True``：基表＝「**人的并集**」＝通讯录 ∪ 会话里的私聊 peer。
    **为什么非并集不可**：Telegram 的 ``get_contacts()`` 语义是「我主动保存过的
    联系人」，群里认识的、主动找上来的客户全都不算——2026-07 线上实测三个生产 TG
    账号目录同步全部成功、零失败，通讯录仍是 0，而同一账号有 62 条真实往来会话。
    只读通讯录 → 「联系人」面板对 Telegram 恒空，坐席想按名字找「昨天聊过的那个
    客户」永远找不到人，而这正是这个面板存在的唯一理由。WhatsApp 同样漏掉「主动
    找上来但没存进通讯录」的客户。

    复杂度：两侧各走自己的索引区间扫（``protocol_contacts`` 的
    ``(platform, account_id, chat_key)`` 唯一索引 / ``conversations`` 的
    ``idx_conv_platform(platform, account_id)``），无全表扫；``GROUP BY chat_key``
    走一次临时 b-tree 排序（O(n log n)，n = 该账号的人数，千级无感），外层再对每
    个人做一次 ``conversations`` 主键点查。
    """
    prefix = f"{platform}:{account_id}:"
    join = " LEFT JOIN conversations v ON v.conversation_id = ? || c.chat_key "
    # 系统 peer 子句放在 base_where 末尾：两个调用方都在 base_where 之后才追加
    # only / query 的子句与参数，故只要这里子句与参数同序，整体绑定顺序就成立。
    sys_where, sys_params = _system_peer_where(platform, account_id)
    if not include_chats:
        return (" FROM protocol_contacts c " + join, [prefix],
                ["c.platform=?", "c.account_id=?"] + sys_where,
                [platform, account_id] + sys_params)
    # 存量群组的第二道闸：``chat_type`` 是**后加的迁移列**，默认 'private'（见
    # ``_MIGRATIONS``），存量群会话只有再来一条消息触发 ingest 回填才会变成
    # 'group'——一个再没人说话的老群会永远顶着 'private' 混进「联系人」。故对
    # chat_type 判不出来的情况，补上 ``normalizer.infer_chat_type`` 用的同一套
    # chat_key 形态启发式（TG 群/频道 id 为负；LINE 官方 webhook 键形如
    # ``line:group:`` / ``line:room:``），两处口径必须同源。
    legacy_group = ""
    if platform == "telegram":
        legacy_group = " AND chat_key NOT GLOB '-[0-9]*'"
    elif platform == "line":
        legacy_group = (" AND LOWER(chat_key) NOT LIKE '%:group:%'"
                        " AND LOWER(chat_key) NOT LIKE '%:room:%'")
    union = (
        " FROM (SELECT chat_key, "
        # 同一个人可能两边都在（存了通讯录又聊过）→ 按 chat_key 聚合去重，
        # in_book 取 MAX：只要通讯录里有就算「已在通讯录」。
        "              MAX(in_book) AS in_book, "
        # 名字优先取通讯录名：那是坐席自己存的备注名，贴近「我认识的这个人叫什么」；
        # 会话 display_name 是平台推送的昵称，客户随时会改。通讯录名为空才回落会话
        # 名——纯会话来源的人（TG 的绝大多数）只有会话名，不回落就会显示成空白行。
        "              COALESCE(NULLIF(MAX(CASE WHEN in_book=1 THEN name END), ''), "
        "                       MAX(CASE WHEN in_book=0 THEN name END), '') AS name, "
        # 会话侧恒为空串 → MAX 天然取到非空的那个（空串排在任何非空串之前）。
        "              MAX(notify_name) AS notify_name, "
        "              MAX(updated_at) AS updated_at "
        "         FROM (SELECT chat_key, name, notify_name, updated_at, 1 AS in_book "
        "                 FROM protocol_contacts WHERE platform=? AND account_id=? "
        "               UNION ALL "
        "               SELECT chat_key, display_name AS name, '' AS notify_name, "
        "                      updated_at, 0 AS in_book "
        "                 FROM conversations "
        "                WHERE platform=? AND account_id=? AND chat_key != '' "
        # 群/频道**必须**排除：把群灌进「联系人」会毁掉这个面板（坐席要找的是人，
        # 不是群）。这里用**白名单**而非 `chat_type != 'group'`——上游 ingest 可能
        # 原样落 'channel'/'supergroup' 等未归一值（见 normalizer.infer_chat_type
        # 的类型集），黑名单会漏网；空串是 chat_type 特性之前的历史行，按 DDL 默认
        # 语义算私聊。chat_key='' 的行（如未带 peer 的兜底会话）也一并排除：它们
        # 拼出来的 conversation_id 是 'plat:acct:' 这种无意义前缀。
        "                  AND chat_type IN ('private', '')" + legacy_group + ") "
        "        GROUP BY chat_key) c "
    )
    # 绑定顺序＝文本里 ? 的出现顺序：通讯录侧 → 会话侧 → JOIN 前缀（FROM 块内）
    # → 系统 peer（外层 WHERE，排在 FROM 之后）。
    return (union + join, [platform, account_id, platform, account_id, prefix],
            sys_where, sys_params)


_DDL = """
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id   TEXT PRIMARY KEY,
    platform          TEXT NOT NULL,
    account_id        TEXT NOT NULL DEFAULT 'default',
    chat_key          TEXT NOT NULL DEFAULT '',
    contact_id        TEXT NOT NULL DEFAULT '',
    display_name      TEXT NOT NULL DEFAULT '',
    language          TEXT NOT NULL DEFAULT 'unknown',
    chat_type         TEXT NOT NULL DEFAULT 'private',
    last_text         TEXT NOT NULL DEFAULT '',
    last_ts           REAL NOT NULL DEFAULT 0,
    unread            INTEGER NOT NULL DEFAULT 0,
    risk_level        TEXT NOT NULL DEFAULT 'unknown',
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_updated  ON conversations(last_ts DESC);
CREATE INDEX IF NOT EXISTS idx_conv_platform ON conversations(platform, account_id);
CREATE INDEX IF NOT EXISTS idx_conv_contact  ON conversations(contact_id);

CREATE TABLE IF NOT EXISTS messages (
    message_id        TEXT PRIMARY KEY,
    conversation_id   TEXT NOT NULL,
    platform_msg_id   TEXT NOT NULL DEFAULT '',
    direction         TEXT NOT NULL DEFAULT 'in',
    text              TEXT NOT NULL DEFAULT '',
    original_text     TEXT NOT NULL DEFAULT '',
    translated_text   TEXT NOT NULL DEFAULT '',
    source_lang       TEXT NOT NULL DEFAULT 'unknown',
    target_lang       TEXT NOT NULL DEFAULT '',
    media_type        TEXT NOT NULL DEFAULT '',
    media_ref         TEXT NOT NULL DEFAULT '',
    ts                REAL NOT NULL DEFAULT 0,
    ingested_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_conv_ts ON messages(conversation_id, ts DESC);

-- P1：出向译文旁路表。坐席「一击直发」让后端把中文原文译成客户语言后投递，
-- 但出向消息回到 messages 的路径异构（web record_message / protocol worker push /
-- 多数 RPA 根本不回读），无法在 messages 里稳定保存「中文原文 ↔ 实发译文」配对。
-- 故此处旁路记录，按 (conversation_id, 实发译文 hash) 键；thread 读取时富集回原文，
-- 实现跨刷新/重启/设备的出向双行展示，且完全不触碰 messages 去重。
CREATE TABLE IF NOT EXISTS outbound_translations (
    conversation_id   TEXT NOT NULL,
    sent_hash         TEXT NOT NULL,
    original_text     TEXT NOT NULL DEFAULT '',
    source_lang       TEXT NOT NULL DEFAULT '',
    target_lang       TEXT NOT NULL DEFAULT '',
    provider          TEXT NOT NULL DEFAULT '',
    error             TEXT NOT NULL DEFAULT '',
    created_at        REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (conversation_id, sent_hash)
);
CREATE INDEX IF NOT EXISTS idx_outxl_conv ON outbound_translations(conversation_id);

CREATE TABLE IF NOT EXISTS message_analysis (
    analysis_id        TEXT PRIMARY KEY,
    message_id         TEXT NOT NULL,
    conversation_id    TEXT NOT NULL,
    intent             TEXT NOT NULL DEFAULT '',
    emotion            TEXT NOT NULL DEFAULT '',
    risk_level         TEXT NOT NULL DEFAULT 'low',
    risk_reasons_json  TEXT NOT NULL DEFAULT '[]',
    relationship_stage TEXT NOT NULL DEFAULT '',
    summary            TEXT NOT NULL DEFAULT '',
    order_no           TEXT NOT NULL DEFAULT '',
    confidence         REAL NOT NULL DEFAULT 0,
    analyzer           TEXT NOT NULL DEFAULT 'rule',
    ts                 REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ana_msg  ON message_analysis(message_id);
CREATE INDEX IF NOT EXISTS idx_ana_conv ON message_analysis(conversation_id, ts DESC);

CREATE TABLE IF NOT EXISTS conversation_settings (
    conversation_id   TEXT PRIMARY KEY,
    automation_mode   TEXT NOT NULL DEFAULT 'review',
    updated_at        REAL NOT NULL
);

-- P2 2026-08-09 档位变更时间线：conversation_settings 只存最新态，「谁在什么时候
-- 把档位改成什么、之前是什么」此前不可追溯（.198 接管钉死 27h 的排障只能靠猜）。
-- 只记**真实跃迁**（mode 或 source 变了才落行；接管重刷时间戳不落）→ 行数受真实
-- 状态变化约束，天然有界。消费：why-no-reply 的 mode_history 段 + AI 状态面板时间线。
CREATE TABLE IF NOT EXISTS automation_mode_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id   TEXT NOT NULL,
    mode              TEXT NOT NULL,
    source            TEXT NOT NULL DEFAULT '',
    prev_mode         TEXT NOT NULL DEFAULT '',
    ts                REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_amlog_conv ON automation_mode_log(conversation_id, ts DESC);

-- Phase B：统一草稿层。
-- 注意：平台来源的草稿事实源仍在各 RPA 表（line_rpa_pending / wa_rpa_pending /
-- messenger_rpa_approvals），读路径走 read-through 直读聚合，不在此镜像。
-- 本表只存：(a) inbox 自发草稿（source_kind='inbox'，无平台表）；
--           (b) 风险/autopilot 元数据 overlay（按 source_kind+source_id 键，Phase C 写）。
CREATE TABLE IF NOT EXISTS reply_drafts (
    draft_id           TEXT PRIMARY KEY,
    conversation_id    TEXT NOT NULL DEFAULT '',
    platform           TEXT NOT NULL DEFAULT '',
    account_id         TEXT NOT NULL DEFAULT 'default',
    chat_key           TEXT NOT NULL DEFAULT '',
    source_kind        TEXT NOT NULL,              -- inbox | line_pending | wa_pending | messenger_approval | reunion
    source_id          TEXT NOT NULL DEFAULT '',
    peer_text          TEXT NOT NULL DEFAULT '',
    draft_text         TEXT NOT NULL DEFAULT '',
    final_text         TEXT NOT NULL DEFAULT '',
    draft_lang         TEXT NOT NULL DEFAULT '',
    translated_preview TEXT NOT NULL DEFAULT '',
    risk_level         TEXT NOT NULL DEFAULT 'low',
    risk_reasons_json  TEXT NOT NULL DEFAULT '[]',
    autopilot_level    TEXT NOT NULL DEFAULT 'L1',
    status             TEXT NOT NULL DEFAULT 'pending',
    decided_by         TEXT NOT NULL DEFAULT '',
    decided_at         REAL NOT NULL DEFAULT 0,
    sent_at            REAL NOT NULL DEFAULT 0,
    error              TEXT NOT NULL DEFAULT '',
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON reply_drafts(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_drafts_conv   ON reply_drafts(conversation_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_drafts_source ON reply_drafts(source_kind, source_id);

-- Phase 5：坐席在线状态 + 会话租约锁（多坐席防重复回复）
CREATE TABLE IF NOT EXISTS agent_presence (
    agent_id          TEXT PRIMARY KEY,
    display_name      TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'offline',
    last_seen_at      REAL NOT NULL DEFAULT 0,
    updated_at        REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS conversation_claims (
    conversation_id   TEXT PRIMARY KEY,
    agent_id          TEXT NOT NULL,
    agent_name        TEXT NOT NULL DEFAULT '',
    claimed_at        REAL NOT NULL DEFAULT 0,
    expires_at        REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_claims_agent   ON conversation_claims(agent_id);
CREATE INDEX IF NOT EXISTS idx_claims_expires ON conversation_claims(expires_at);

CREATE TABLE IF NOT EXISTS agent_sends (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id   TEXT NOT NULL,
    agent_id          TEXT NOT NULL DEFAULT '',
    agent_name        TEXT NOT NULL DEFAULT '',
    ts                REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_agent_sends_conv ON agent_sends(conversation_id, ts);
CREATE INDEX IF NOT EXISTS idx_agent_sends_ts   ON agent_sends(ts);

CREATE TABLE IF NOT EXISTS agent_prefs (
    agent_id          TEXT PRIMARY KEY,
    warn_sec          INTEGER NOT NULL DEFAULT 0,   -- 0=沿用全局
    crit_sec          INTEGER NOT NULL DEFAULT 0,   -- 0=沿用全局
    muted             INTEGER NOT NULL DEFAULT 0,   -- 1=完全静音告警
    dnd_start         INTEGER NOT NULL DEFAULT -1,  -- 免打扰起(本地分钟 0-1439)，-1=关
    dnd_end           INTEGER NOT NULL DEFAULT -1,  -- 免打扰止(本地分钟 0-1439)，-1=关
    updated_at        REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS escalations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id   TEXT NOT NULL,
    reason            TEXT NOT NULL DEFAULT '',
    agent_id          TEXT NOT NULL DEFAULT '',   -- 升级时的认领人(问责)
    agent_name        TEXT NOT NULL DEFAULT '',
    wait_sec          INTEGER NOT NULL DEFAULT 0,
    ts                REAL NOT NULL DEFAULT 0,
    assigned_to       TEXT NOT NULL DEFAULT ''    -- 负责处理此次升级的主管 agent_id
);
CREATE INDEX IF NOT EXISTS idx_escalations_conv     ON escalations(conversation_id, ts);
CREATE INDEX IF NOT EXISTS idx_escalations_ts       ON escalations(ts);
CREATE INDEX IF NOT EXISTS idx_escalations_assigned ON escalations(assigned_to, ts);
"""

# P3-198 过程式一次性迁移的保留 marker id（与 _MIGRATIONS 列表索引空间隔离：
# 列表用 0..n 小整数，过程式迁移从 900001 起编号，永不冲突）
_WA_KEY_MERGE_MIG_ID = 900001
# 结构性无效 WA 占位会话（chat_key='0'，历史 chats 同步坏 jid 产物）清理
_WA_ZERO_KEY_MIG_ID = 900002

# 对存量 escalations 表补列（新安装已由 DDL 建好，旧库通过 migration 追加）
_MIGRATIONS = [
    "ALTER TABLE escalations ADD COLUMN assigned_to TEXT NOT NULL DEFAULT ''",
    # B2: 草稿强制审计日志（安全不变量：L4 拦截 / force-override / autosend 全部留档）
    """CREATE TABLE IF NOT EXISTS draft_audit_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        draft_id        TEXT NOT NULL DEFAULT '',
        autopilot_level TEXT NOT NULL DEFAULT '',
        action          TEXT NOT NULL DEFAULT '',
        agent_id        TEXT NOT NULL DEFAULT '',
        reason          TEXT NOT NULL DEFAULT '',
        risk_level      TEXT NOT NULL DEFAULT '',
        conversation_id TEXT NOT NULL DEFAULT '',
        ts              REAL NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_draft_audit_draft ON draft_audit_log(draft_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_draft_audit_ts    ON draft_audit_log(ts)",
    "CREATE INDEX IF NOT EXISTS idx_draft_audit_agent ON draft_audit_log(agent_id, ts)",
    # I1: 对话智能分析元数据（最近意图/情绪趋势/风险）
    """CREATE TABLE IF NOT EXISTS conversation_meta (
        conversation_id  TEXT PRIMARY KEY,
        platform         TEXT NOT NULL DEFAULT '',
        last_intent      TEXT NOT NULL DEFAULT '',
        last_emotion     TEXT NOT NULL DEFAULT '',
        last_risk        TEXT NOT NULL DEFAULT 'low',
        intent_history   TEXT NOT NULL DEFAULT '[]',
        emotion_history  TEXT NOT NULL DEFAULT '[]',
        msg_count        INTEGER NOT NULL DEFAULT 0,
        updated_at       REAL NOT NULL DEFAULT 0
    )""",
    # M1: conversation_meta 新增 csat_score 列
    "ALTER TABLE conversation_meta ADD COLUMN csat_score REAL NOT NULL DEFAULT -1",
    # N1: conversation_meta 新增 contact_id 列（用于跨平台会话归档）
    "ALTER TABLE conversation_meta ADD COLUMN contact_id TEXT NOT NULL DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_conv_meta_contact ON conversation_meta(contact_id)",
    # P3: 多租户 workspace_id 列（默认 'default'，向下兼容）
    "ALTER TABLE conversation_meta ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'default'",
    "ALTER TABLE draft_audit_log ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'default'",
    """CREATE TABLE IF NOT EXISTS workspaces (
        workspace_id   TEXT PRIMARY KEY,
        display_name   TEXT NOT NULL DEFAULT '',
        config_json    TEXT NOT NULL DEFAULT '{}',
        created_at     REAL NOT NULL DEFAULT 0,
        updated_at     REAL NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_conv_meta_workspace ON conversation_meta(workspace_id)",
    "CREATE INDEX IF NOT EXISTS idx_draft_audit_workspace ON draft_audit_log(workspace_id)",
    "CREATE INDEX IF NOT EXISTS idx_conv_meta_updated ON conversation_meta(updated_at DESC)",
    # Q1: conversation_meta 新增 summary 列（对话摘要自动归档）
    "ALTER TABLE conversation_meta ADD COLUMN summary TEXT NOT NULL DEFAULT ''",
    # Q2: reply_drafts 新增质量评分列
    "ALTER TABLE reply_drafts ADD COLUMN quality_score REAL NOT NULL DEFAULT -1",
    "ALTER TABLE reply_drafts ADD COLUMN quality_breakdown TEXT NOT NULL DEFAULT '{}'",
    # Q3: KB 推荐命中率记录表
    """CREATE TABLE IF NOT EXISTS kb_recommendation_log (
        id              TEXT PRIMARY KEY,
        entry_id        TEXT NOT NULL DEFAULT '',
        entry_title     TEXT NOT NULL DEFAULT '',
        conversation_id TEXT NOT NULL DEFAULT '',
        agent_id        TEXT NOT NULL DEFAULT '',
        recommended_ts  REAL NOT NULL DEFAULT 0,
        clicked         INTEGER NOT NULL DEFAULT 0,
        used_in_draft   INTEGER NOT NULL DEFAULT 0,
        draft_id        TEXT NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_kb_rec_entry ON kb_recommendation_log(entry_id)",
    "CREATE INDEX IF NOT EXISTS idx_kb_rec_ts    ON kb_recommendation_log(recommended_ts DESC)",
    # R3: CSAT 问卷表
    """CREATE TABLE IF NOT EXISTS csat_surveys (
        id               TEXT PRIMARY KEY,
        conversation_id  TEXT NOT NULL DEFAULT '',
        draft_id         TEXT NOT NULL DEFAULT '',
        agent_id         TEXT NOT NULL DEFAULT '',
        scheduled_at     REAL NOT NULL DEFAULT 0,
        send_at          REAL NOT NULL DEFAULT 0,
        sent             INTEGER NOT NULL DEFAULT 0,
        response_score   INTEGER NOT NULL DEFAULT -1,
        response_ts      REAL NOT NULL DEFAULT 0,
        created_at       REAL NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_survey_conv ON csat_surveys(conversation_id)",
    "CREATE INDEX IF NOT EXISTS idx_survey_due  ON csat_surveys(send_at, sent)",
    # S1: A/B 测试表
    """CREATE TABLE IF NOT EXISTS ab_tests (
        id              TEXT PRIMARY KEY,
        name            TEXT NOT NULL DEFAULT '',
        intent_filter   TEXT NOT NULL DEFAULT '',
        template_a_id   TEXT NOT NULL DEFAULT '',
        template_b_id   TEXT NOT NULL DEFAULT '',
        description     TEXT NOT NULL DEFAULT '',
        min_sample      INTEGER NOT NULL DEFAULT 30,
        status          TEXT NOT NULL DEFAULT 'active',
        created_by      TEXT NOT NULL DEFAULT '',
        created_at      REAL NOT NULL DEFAULT 0,
        updated_at      REAL NOT NULL DEFAULT 0,
        n_a             INTEGER NOT NULL DEFAULT 0,
        n_b             INTEGER NOT NULL DEFAULT 0,
        sat_a           INTEGER NOT NULL DEFAULT 0,
        sat_b           INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS ab_assignments (
        test_id         TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        variant         TEXT NOT NULL DEFAULT 'A',
        assigned_ts     REAL NOT NULL DEFAULT 0,
        csat_score      REAL NOT NULL DEFAULT -1,
        outcome_ts      REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (test_id, conversation_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ab_assign_conv ON ab_assignments(conversation_id)",
    # S3: 全链路追踪 trace_id
    "ALTER TABLE conversation_meta ADD COLUMN trace_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE reply_drafts ADD COLUMN trace_id TEXT NOT NULL DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_conv_trace ON conversation_meta(trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_draft_trace ON reply_drafts(trace_id)",
    # I3: 回复模板库
    """CREATE TABLE IF NOT EXISTS reply_templates (
        id           TEXT PRIMARY KEY,
        title        TEXT NOT NULL DEFAULT '',
        content      TEXT NOT NULL DEFAULT '',
        language     TEXT NOT NULL DEFAULT 'zh',
        platform     TEXT NOT NULL DEFAULT '',
        scene        TEXT NOT NULL DEFAULT '',
        created_by   TEXT NOT NULL DEFAULT 'system',
        created_at   REAL NOT NULL DEFAULT 0,
        updated_at   REAL NOT NULL DEFAULT 0,
        used_count   INTEGER NOT NULL DEFAULT 0,
        is_active    INTEGER NOT NULL DEFAULT 1
    )""",
    "CREATE INDEX IF NOT EXISTS idx_templates_scene    ON reply_templates(scene, language, is_active)",
    "CREATE INDEX IF NOT EXISTS idx_templates_platform ON reply_templates(platform, language, is_active)",
    # T1: 会话级标签 + 归档（Phase 14）
    "ALTER TABLE conversation_meta ADD COLUMN conv_tags TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE conversation_meta ADD COLUMN archived  INTEGER NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS idx_conv_meta_archived ON conversation_meta(archived)",
    # U1: FTS5 全文索引（Phase 22）
    # FTS5 虚拟表：独立存储，触发器同步，搜索降级至 LIKE 若不可用
    """CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
       USING fts5(message_id UNINDEXED, conversation_id UNINDEXED,
                  text, ts UNINDEXED, direction UNINDEXED,
                  tokenize='unicode61 remove_diacritics 1')""",
    """CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
         INSERT INTO messages_fts(message_id, conversation_id, text, ts, direction)
         VALUES (new.message_id, new.conversation_id, new.text, new.ts, new.direction);
       END""",
    """CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
         DELETE FROM messages_fts WHERE message_id = old.message_id;
       END""",
    """CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE OF text ON messages BEGIN
         DELETE FROM messages_fts WHERE message_id = old.message_id;
         INSERT INTO messages_fts(message_id, conversation_id, text, ts, direction)
         VALUES (new.message_id, new.conversation_id, new.text, new.ts, new.direction);
       END""",
    # Y1: QA 质检评分 + 流失风险（Phase 34/35）
    "ALTER TABLE conversation_meta ADD COLUMN qa_score TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE conversation_meta ADD COLUMN churn_risk TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE conversation_meta ADD COLUMN auto_archived_at REAL NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS idx_conv_meta_last_ts ON conversation_meta(updated_at DESC)",
    # O: 末条情绪强度（analyze_emotion primary_intensity；-1=未知），供主动护栏强度分级
    "ALTER TABLE conversation_meta ADD COLUMN last_emotion_intensity REAL NOT NULL DEFAULT -1",
    # P0-companion（陪伴接管）：会话搁置 snooze。真人接管后「稍后再看」——到点自动重浮，
    # 客户再次来消息立即重浮（ingest 侧 clear_snooze）。与 automation_mode（谁来答）正交：
    # snooze 只控「何时从待接管/超时告警队列重新浮出」。0=未搁置；>0=重浮的 epoch 秒。
    "ALTER TABLE conversation_meta ADD COLUMN snooze_until REAL NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS idx_conv_meta_snooze ON conversation_meta(snooze_until)",
    # V1: 坐席协作注解（Phase 25）
    """CREATE TABLE IF NOT EXISTS conv_notes (
         note_id    TEXT PRIMARY KEY,
         conversation_id TEXT NOT NULL,
         agent_id   TEXT NOT NULL DEFAULT '',
         agent_name TEXT NOT NULL DEFAULT '',
         body       TEXT NOT NULL DEFAULT '',
         mentions   TEXT NOT NULL DEFAULT '[]',
         ts         REAL NOT NULL DEFAULT 0,
         edited_ts  REAL NOT NULL DEFAULT 0
       )""",
    "CREATE INDEX IF NOT EXISTS idx_conv_notes_conv ON conv_notes(conversation_id, ts DESC)",
    # AA1: 自定义动作 + 工作链（Phase 37）
    """CREATE TABLE IF NOT EXISTS workflow_actions (
         action_id   TEXT PRIMARY KEY,
         name        TEXT NOT NULL DEFAULT '',
         action_type TEXT NOT NULL DEFAULT 'template',
         config_json TEXT NOT NULL DEFAULT '{}',
         icon        TEXT NOT NULL DEFAULT '💡',
         enabled     INTEGER NOT NULL DEFAULT 1,
         sort_order  INTEGER NOT NULL DEFAULT 0,
         created_at  REAL NOT NULL DEFAULT 0,
         updated_at  REAL NOT NULL DEFAULT 0
       )""",
    """CREATE TABLE IF NOT EXISTS workflow_chains (
         chain_id    TEXT PRIMARY KEY,
         name        TEXT NOT NULL DEFAULT '',
         steps_json  TEXT NOT NULL DEFAULT '[]',
         trigger_conditions TEXT NOT NULL DEFAULT '{}',
         enabled     INTEGER NOT NULL DEFAULT 1,
         created_at  REAL NOT NULL DEFAULT 0,
         updated_at  REAL NOT NULL DEFAULT 0
       )""",
    """CREATE TABLE IF NOT EXISTS workflow_executions (
         exec_id       TEXT PRIMARY KEY,
         chain_id      TEXT NOT NULL DEFAULT '',
         conversation_id TEXT NOT NULL DEFAULT '',
         current_step  INTEGER NOT NULL DEFAULT 0,
         status        TEXT NOT NULL DEFAULT 'pending',
         context_json  TEXT NOT NULL DEFAULT '{}',
         started_at    REAL NOT NULL DEFAULT 0,
         updated_at    REAL NOT NULL DEFAULT 0
       )""",
    "CREATE INDEX IF NOT EXISTS idx_wf_exec_conv ON workflow_executions(conversation_id, updated_at DESC)",
    "ALTER TABLE workflow_executions ADD COLUMN next_step_at REAL NOT NULL DEFAULT 0",
    "ALTER TABLE workflow_executions ADD COLUMN last_result_json TEXT NOT NULL DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_wf_exec_due ON workflow_executions(status, next_step_at)",
    # DD1: 关系阶段缓存（P43 进阶检测）
    "ALTER TABLE conversation_meta ADD COLUMN rel_stage_cached TEXT NOT NULL DEFAULT ''",
    # P46: 关系阶段人工确认
    "ALTER TABLE conversation_meta ADD COLUMN rel_stage_pending TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE conversation_meta ADD COLUMN rel_stage_pending_ts REAL NOT NULL DEFAULT 0",
    "ALTER TABLE conversation_meta ADD COLUMN rel_reunion_ack_ts REAL NOT NULL DEFAULT 0",
    # P50: 客户级关系阶段（跨会话同步）
    """CREATE TABLE IF NOT EXISTS contact_rel_stage (
         contact_id       TEXT PRIMARY KEY,
         confirmed_stage  TEXT NOT NULL DEFAULT '',
         updated_by       TEXT NOT NULL DEFAULT '',
         updated_at       REAL NOT NULL DEFAULT 0,
         reunion_ack_ts   REAL NOT NULL DEFAULT 0
       )""",
    "CREATE INDEX IF NOT EXISTS idx_contact_rel_stage_updated ON contact_rel_stage(updated_at DESC)",
    # BB1: 分流路由规则（Phase 38）
    """CREATE TABLE IF NOT EXISTS routing_rules (
         rule_id    TEXT PRIMARY KEY,
         name       TEXT NOT NULL DEFAULT '',
         conditions TEXT NOT NULL DEFAULT '{}',
         assign_to  TEXT NOT NULL DEFAULT '',
         priority   INTEGER NOT NULL DEFAULT 0,
         enabled    INTEGER NOT NULL DEFAULT 1,
         created_at REAL NOT NULL DEFAULT 0,
         updated_at REAL NOT NULL DEFAULT 0
       )""",
    "CREATE INDEX IF NOT EXISTS idx_routing_rules_priority ON routing_rules(priority DESC, enabled)",
    # CC1: 互动积分（Phase 41；Phase 40 剧本话题已下线，存量 script_topics 表留而不用）
    """CREATE TABLE IF NOT EXISTS contact_engagement (
         contact_id       TEXT PRIMARY KEY,
         points           INTEGER NOT NULL DEFAULT 0,
         level            TEXT NOT NULL DEFAULT 'new',
         breakdown_json   TEXT NOT NULL DEFAULT '{}',
         achievements_json TEXT NOT NULL DEFAULT '[]',
         history_json     TEXT NOT NULL DEFAULT '[]',
         updated_at       REAL NOT NULL DEFAULT 0
       )""",
    # P61-3：分组批量触达（再激活）日志——cooldown 判定 + 回执统计
    """CREATE TABLE IF NOT EXISTS outreach_log (
         id              INTEGER PRIMARY KEY AUTOINCREMENT,
         conversation_id TEXT NOT NULL,
         batch_id        TEXT NOT NULL DEFAULT '',
         platform        TEXT NOT NULL DEFAULT '',
         account_id      TEXT NOT NULL DEFAULT '',
         status          TEXT NOT NULL DEFAULT 'sent',
         note            TEXT NOT NULL DEFAULT '',
         ts              REAL NOT NULL DEFAULT 0
       )""",
    "CREATE INDEX IF NOT EXISTS idx_outreach_conv ON outreach_log(conversation_id, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_outreach_batch ON outreach_log(batch_id)",
    # P3：坐席技能语言（CSV 规范 ISO 码，如 "en,ja"）。供 auto_assign 的 match_language
    # 把外语会话优先派给会该语言的坐席（坐席在工作台「我的偏好」声明）。
    "ALTER TABLE agent_prefs ADD COLUMN languages TEXT NOT NULL DEFAULT ''",
    # P8：通知中心「已读水位线」上云（跨设备保留「全部已读」状态，毫秒时戳）。
    "ALTER TABLE agent_prefs ADD COLUMN notif_read_at INTEGER NOT NULL DEFAULT 0",
    # 外观个性化（2026-08-04）：坐席「主题/壁纸/气泡色/夜间/字号/圆角/动画」偏好 JSON。
    # 服务端漫游的单一事实源；写入前经 src/web/appearance_prefs.sanitize_appearance 白名单净化，
    # 空串=从未设置/恢复默认（前端回落内置默认主题，视觉与未部署本功能时一致）。
    "ALTER TABLE agent_prefs ADD COLUMN appearance TEXT NOT NULL DEFAULT ''",
    # P3：出向翻译漏斗「按日」持久化聚合（看板按 7/30 日窗读取，跨重启/含趋势线）。
    # 与内存版 OutboundTranslationStats 同口径；day 用本地日期，与 dashboard 其它面板分桶一致。
    # by_lang_json 为该日各目标语译出次数的 JSON（读改写，已在锁内）。
    """CREATE TABLE IF NOT EXISTS outbound_xlate_daily (
         day             TEXT PRIMARY KEY,
         sends           INTEGER NOT NULL DEFAULT 0,
         requested       INTEGER NOT NULL DEFAULT 0,
         translated      INTEGER NOT NULL DEFAULT 0,
         skipped         INTEGER NOT NULL DEFAULT 0,
         failed          INTEGER NOT NULL DEFAULT 0,
         auto_requested  INTEGER NOT NULL DEFAULT 0,
         auto_unresolved INTEGER NOT NULL DEFAULT 0,
         degraded        INTEGER NOT NULL DEFAULT 0,
         by_lang_json    TEXT NOT NULL DEFAULT '{}'
       )""",
    # P3：入站翻译漏斗「按日」持久化（客户→坐席自动翻译）。语义与出向不同：入站为打开会话
    # 时懒翻译，只记「新译出」（成功后即缓存，再开走 store 不重复计）与 failed；by_lang_json
    # 为该日各「客户来源语言」译出次数（与出向的目标语分布合成跨语言总览）。
    """CREATE TABLE IF NOT EXISTS inbound_xlate_daily (
         day          TEXT PRIMARY KEY,
         translated   INTEGER NOT NULL DEFAULT 0,
         failed       INTEGER NOT NULL DEFAULT 0,
         by_lang_json TEXT NOT NULL DEFAULT '{}'
       )""",
    # P3：自动派单（AutoClaimWorker）「按日」持久化。进程内 status_snapshot 是累计且重启清零，
    # 看板需按 7/30 日窗回溯，故落表。claimed=当日自动认领总数，lang_matched=其中按语言命中数，
    # by_lang_json=命中派单的会话语言分布（系统按哪些语言在精准路由）。
    """CREATE TABLE IF NOT EXISTS auto_claim_daily (
         day          TEXT PRIMARY KEY,
         claimed      INTEGER NOT NULL DEFAULT 0,
         lang_matched INTEGER NOT NULL DEFAULT 0,
         by_lang_json TEXT NOT NULL DEFAULT '{}'
       )""",
    # E2：运维事件（health_alert 闭环）。watchdog 红/黄告警按 signature 去重落表，
    # 恢复时自动 resolve；主管可 ack/指派。让系统告警可追踪到处理人而非只推一条通知。
    """CREATE TABLE IF NOT EXISTS ops_incidents (
         id            INTEGER PRIMARY KEY AUTOINCREMENT,
         kind          TEXT NOT NULL DEFAULT 'health',
         signature     TEXT NOT NULL DEFAULT '',
         light         TEXT NOT NULL DEFAULT '',
         summary_json  TEXT NOT NULL DEFAULT '{}',
         problems_json TEXT NOT NULL DEFAULT '[]',
         status        TEXT NOT NULL DEFAULT 'open',
         assigned_to   TEXT NOT NULL DEFAULT '',
         opened_ts     REAL NOT NULL DEFAULT 0,
         updated_ts    REAL NOT NULL DEFAULT 0,
         acked_ts      REAL NOT NULL DEFAULT 0,
         resolved_ts   REAL NOT NULL DEFAULT 0
       )""",
    "CREATE INDEX IF NOT EXISTS idx_ops_incidents_status ON ops_incidents(status, opened_ts)",
    "CREATE INDEX IF NOT EXISTS idx_ops_incidents_sig    ON ops_incidents(kind, signature, status)",
    # 存量库（本分支早期已建表者）补 kind 列
    "ALTER TABLE ops_incidents ADD COLUMN kind TEXT NOT NULL DEFAULT 'health'",
    # F+：会话级首选翻译引擎（坐席多线路对照择优后持久化，跨刷新/重启生效）
    "ALTER TABLE conversations ADD COLUMN pref_engine TEXT NOT NULL DEFAULT ''",
    # P3（译文默认语言运营化）：通用 KV 配置表。供「默认译文显示语言」按
    # 全局/平台/账号维度持久化（key 形如 inbox.default_lang[.platform.<p>][.account.<p>.<a>]），
    # 换机/换坐席不丢，区别于 conversations.pref_engine（会话级）。值空＝未配置。
    """CREATE TABLE IF NOT EXISTS app_settings (
         skey       TEXT PRIMARY KEY,
         sval       TEXT NOT NULL DEFAULT '',
         updated_at REAL NOT NULL DEFAULT 0
       )""",
    # P4-A：app_settings 审计列——记录最后修改人（运营级配置可追责）。
    "ALTER TABLE app_settings ADD COLUMN updated_by TEXT NOT NULL DEFAULT ''",
    # 群组分流：会话类型列（private/group/channel）。群组不进升级/SLA 告警，改走「群组动态」。
    # 存量行默认 'private'（保守按私聊照常告警），后续 ingest 会按实况回填。
    "ALTER TABLE conversations ADD COLUMN chat_type TEXT NOT NULL DEFAULT 'private'",
    "CREATE INDEX IF NOT EXISTS idx_conv_chat_type ON conversations(chat_type)",
    # 群组分流·存量回填：chat_type 特性上线前入库的 Telegram 群组/频道（chat_key 为负数 id，
    # 如 -100.../-5...）被默认成了 'private'，会错误地刷进 SLA「严重超时/待接管」。
    # 这里按 chat_key 实况一次性回填为 'group'（幂等：回填后不再命中 chat_type='private'）。
    """UPDATE conversations SET chat_type='group'
         WHERE platform='telegram' AND chat_type='private'
           AND (chat_key LIKE '-%' OR conversation_id LIKE 'telegram:%:-%')""",
    # E3: 轻量 UI 遥测事件（如 AI 安全看板「风控拦截」下钻点击），观测「看板有没有人真去用」。
    # 极简 append-only 计数表；不含 PII 正文，只留 event/source/conversation_id/ts。
    """CREATE TABLE IF NOT EXISTS ui_event_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        event           TEXT NOT NULL DEFAULT '',
        source          TEXT NOT NULL DEFAULT '',
        conversation_id TEXT NOT NULL DEFAULT '',
        ts              REAL NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ui_event ON ui_event_log(event, source, ts)",
    # P0（协议接入补全·好友名单）：平台原始通讯录镜像。刻意与 CRM 的 contacts
    # （已跟进客户档案）分开——此表只是「账号地址簿」的只读镜像，用途：
    #   (1) 好友名单展示（贴近官方客户端）；(2) 入站/占位会话把裸号码补成通讯录名。
    # 按 (platform, account_id, chat_key) 唯一；不参与 CRM 跟进/亲密度，避免污染主流程。
    """CREATE TABLE IF NOT EXISTS protocol_contacts (
        platform     TEXT NOT NULL,
        account_id   TEXT NOT NULL,
        chat_key     TEXT NOT NULL,
        name         TEXT NOT NULL DEFAULT '',
        notify_name  TEXT NOT NULL DEFAULT '',
        updated_at   REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (platform, account_id, chat_key)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_proto_contacts_acct ON protocol_contacts(platform, account_id)",
    # P4-2 引用回复：messages 增被引用消息的 id/文本摘要/发言人（纯加法，缺省空=无引用）
    "ALTER TABLE messages ADD COLUMN reply_to_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE messages ADD COLUMN reply_to_text TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE messages ADD COLUMN reply_to_sender TEXT NOT NULL DEFAULT ''",
    # P4-3 表情回应：messages 上挂 {sender: emoji} JSON（按 sender 键，天然处理改/撤）
    "ALTER TABLE messages ADD COLUMN reactions_json TEXT NOT NULL DEFAULT '{}'",
    # P4-4 已读回执：出站消息投递状态（''/sent/delivered/read；仅出站有意义，单调升级）
    "ALTER TABLE messages ADD COLUMN status TEXT NOT NULL DEFAULT ''",
    # P4-6A 编辑/撤回：撤回=对端删除给所有人（气泡置灰）；编辑=正文被改（标「已编辑」）
    "ALTER TABLE messages ADD COLUMN revoked INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE messages ADD COLUMN edited INTEGER NOT NULL DEFAULT 0",
    # 身份画像（真实昵称/头像/资料面板）：会话上补 peer 身份列，替代「一排数字 id」。
    # 纯加法、缺省空；ingest 时**空值不覆盖已存非空值**（见 ingest_batch 的 ON CONFLICT），
    # 使一次采集到的真实昵称/头像稳定留存、不被后续空名冲掉。
    "ALTER TABLE conversations ADD COLUMN username   TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE conversations ADD COLUMN phone      TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE conversations ADD COLUMN avatar_url TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE conversations ADD COLUMN first_seen REAL NOT NULL DEFAULT 0",
    # P4-11B 群「@我」未读旗标：入站群消息 @ 本账号→置 1，打开会话→清 0（供列表 @ 徽标/置顶/提醒）
    "ALTER TABLE conversations ADD COLUMN mentioned_unread INTEGER NOT NULL DEFAULT 0",
    # P4-11D 消息级提及明细：[{jid,number,name}] JSON，随消息持久（离线/列表/引用皆可读，
    # LID 群按 jid 精确寻址），供气泡把 @号码 渲染成 @名字。纯加法、缺省 '[]'=无提及。
    "ALTER TABLE messages ADD COLUMN mentions_json TEXT NOT NULL DEFAULT '[]'",
    # P4-11E 群发言人：结构化存发言人 jid + 名字（替代把「发言人：」拼进正文），供气泡上方
    # 显示发言人名 + 稳定色（对齐官方群聊读感）。纯加法、缺省空=非群/未知。
    "ALTER TABLE messages ADD COLUMN sender_id   TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE messages ADD COLUMN sender_name TEXT NOT NULL DEFAULT ''",
    # 入站翻译按日漏斗扩列（2026-07 同步预算+后台补译架构配套）：noop=产出==原文打标数
    # （emoji/不可译，只译一次的证据）、deferred=转后台补译条数（首开消化量/趋势）。
    "ALTER TABLE inbound_xlate_daily ADD COLUMN noop INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE inbound_xlate_daily ADD COLUMN deferred INTEGER NOT NULL DEFAULT 0",
    # 已读水位（P0 未读可信化）：坐席在工作台「读到哪条时间戳」。区别于 unread——
    # unread 是平台/手机端同步来的未读数（协议号 upsert_protocol_chats 每轮覆盖，
    # 坐席在台里读了它也照样回弹）。有了本列，「有效未读」由后端派生：
    # 仅当 last_ts > last_read_ts 才算真未读，坐席打开会话即写高水位，永不回弹。
    # 单调递增（见 mark_conversation_read 的 MAX 语义）。缺省 0=从未读过。
    "ALTER TABLE conversations ADD COLUMN last_read_ts REAL NOT NULL DEFAULT 0",
    # 用户时区推断缓存（src/companion/user_clock_resolver 落库口径）：主动触达要按**客户**
    # 本地时间择时/判安静时段，每 tick 重扫消息推断太贵 → 结果按 TTL 缓存在这里。
    # tz_hint=IANA 名或 "UTC+07:00" 伪名（空=推不出）；tz_source=stated_city/phone_cc/
    # behavior/behavior_corroborated/lang_default（空=负结果，trust 由 source 反推）；
    # tz_confidence=-1 表示未知；tz_resolved_at=0 表示从未推断（TTL 过期即重算）。
    # 纯缓存语义、非事实源——推断信号（自述记忆/平台号码/入站小时）本身都在别处持久。
    "ALTER TABLE conversation_meta ADD COLUMN tz_hint TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE conversation_meta ADD COLUMN tz_confidence REAL NOT NULL DEFAULT -1",
    "ALTER TABLE conversation_meta ADD COLUMN tz_source TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE conversation_meta ADD COLUMN tz_country TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE conversation_meta ADD COLUMN tz_offset REAL NOT NULL DEFAULT 0",
    "ALTER TABLE conversation_meta ADD COLUMN tz_resolved_at REAL NOT NULL DEFAULT 0",
    # T-mood（P1-198 续，2026-08-02）：坐席「客户情绪」人工标注的 arbitration 列。
    # conv_tags 仍是展示/筛选投影；AI 消费（拟稿指令/主动闸门/goals 让路/语音安抚）
    # 走 effective_mood 仲裁（TTL + 标签在场校验）。ts=0（历史标注无时间戳）恒不激活
    # ——存量部署零行为突变。
    "ALTER TABLE conversation_meta ADD COLUMN mood_manual TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE conversation_meta ADD COLUMN mood_manual_ts REAL NOT NULL DEFAULT 0",
    "ALTER TABLE conversation_meta ADD COLUMN mood_manual_by TEXT NOT NULL DEFAULT ''",
    # 对方机器人守卫 P1（2026-08-03）：检测结果持久化——收件箱 🤖 徽章/一键覆写/
    # 统计剔除的地基。peer_is_bot：0=未知、1=已判定 bot（Tier0 平台真值或运营标记）、
    # -1=运营覆写「确认是真人」（启发式疑似对其不再拦截/降档；Tier0 平台真值与
    # 复读/秒回/预算等**行为刹车**不受覆写影响——那些是行为安全项不是身份判定）。
    # bot_score=最近一次启发式疑似评分 0..1；bot_evidence=原因码+摘要（人可读，
    # 证据 chips 直显）。纯加法、缺省 0/空=存量零行为变化。
    "ALTER TABLE conversations ADD COLUMN peer_is_bot INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE conversations ADD COLUMN bot_score REAL NOT NULL DEFAULT 0",
    "ALTER TABLE conversations ADD COLUMN bot_evidence TEXT NOT NULL DEFAULT ''",
    # 归档生命周期（P0-198，2026-08-04）：archived_at＝**归档动作发生的时刻**。
    # 此前只有 auto_archived_at（仅自动归档路径写），手动归档不留任何时间戳，于是
    # 「客户是不是在归档之后又说过话」无从判定——唯一可用的近似是 conversation_meta
    # .updated_at，而那一列会被入站链路（update_conv_meta 写情绪/意图）刷成最新入站
    # 时刻，导致「新消息时间 > 归档时刻」这个判据**恒不成立**。实锤：198 上一条 33
    # 条消息的活跃会话被手动归档，updated_at 与最后一条入站消息精确到秒相同，归档
    # 时刻已被覆盖、不可追溯。故必须独立成列，别再退回 updated_at 代理。
    "ALTER TABLE conversation_meta ADD COLUMN archived_at REAL NOT NULL DEFAULT 0",
    # 存量已归档行回填「本次升级时刻」：语义＝从升级那刻起客户再开口就能复活，而
    # 升级之前的历史消息（首次全量同步/thread 重放灌进来的）不会误触发复活。
    # 自证幂等（列表按 SQL 内容哈希记账、只跑一次，仍保持可安全重跑不变量）：
    # WHERE archived_at=0 过滤掉已回填行，且此后 set_conv_archived 总会写入非 0 值。
    "UPDATE conversation_meta SET archived_at = CAST(strftime('%s','now') AS REAL)"
    " WHERE archived = 1 AND archived_at = 0",
    # 对方机器人守卫 P2（2026-08-04，198「全自动静默哑火」事故修复）：每日预算台账。
    # 旧口径按 messages 数「当日全部出站」——坐席手发/双气泡拆条全都挤占 AI 预算
    # （198 实锤：手动档人工聊掉 27 条 + 切全自动后 13 条 = 40 触顶，全自动只跑了
    # 7 轮就静默停发）。新口径＝本表数「自动链回复轮次」：A 线获准直回 +1、B 线将
    # 自动投递的拟稿 +1；人审通过/坐席手发/主动触达（自有预算）永不占。
    # day 变更即自动清零（跨日恢复）；relief_day=当日 → 坐席显式救济（「今日继续
    # 自动回复」按钮），当日不再受预算限制，明天自动回到正常预算。
    """CREATE TABLE IF NOT EXISTS peer_reply_ledger (
        conversation_id  TEXT PRIMARY KEY,
        day              TEXT NOT NULL DEFAULT '',
        auto_replies     INTEGER NOT NULL DEFAULT 0,
        relief_day       TEXT NOT NULL DEFAULT '',
        updated_at       REAL NOT NULL DEFAULT 0
    )""",
    # 档位来源治理（P1 2026-08-07，effective_automation §98 续）：谁把会话写成
    # 这个档的？source＝写入者身份：human（UI 下拉/坐席）| bulk（主管一键降档）|
    # bootstrap（首条入站持久化全局档）| guard:<原因码>（peer_bot_guard 降档：
    # tg_is_bot/inbound_repeat/instant_echo/suspected_bot…）| sweep:<信号>（存量
    # bot 一次性降档）。空串＝本列上线前的存量行（不可考，如实展示为未知）。
    # 回答两个排障问题：「界面为什么是人审」「误降档找谁翻案」。与 conversations.
    # bot_evidence 分工：那是**身份证据**，本列是**动作出处**——复读/秒回这类
    # 行为刹车刻意不写身份列，此前降档动作因此完全无痕（.104/.198 排障实锤：
    # 7/29 两侧会话同一分钟被写成 review，事后无法区分是人手切的还是系统降的）。
    "ALTER TABLE conversation_settings ADD COLUMN source TEXT NOT NULL DEFAULT ''",
    # 工作链环节执行日志（P1 2026-08-09）：此前每步只留 executions.last_result_json
    # （新步覆盖旧步）——「每环节转化率/哪一步在损耗」无数据可算。本表按**执行尝试**
    # 落账（成功/失败/重试各记一行，如实反映重试成本）；只增不改，报表窗口聚合用。
    """CREATE TABLE IF NOT EXISTS workflow_step_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        exec_id     TEXT NOT NULL DEFAULT '',
        chain_id    TEXT NOT NULL DEFAULT '',
        conversation_id TEXT NOT NULL DEFAULT '',
        step_idx    INTEGER NOT NULL DEFAULT 0,
        action_type TEXT NOT NULL DEFAULT '',
        ok          INTEGER NOT NULL DEFAULT 1,
        detail      TEXT NOT NULL DEFAULT '',
        ts          REAL NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_wf_steplog_chain ON workflow_step_log(chain_id, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_wf_steplog_exec ON workflow_step_log(exec_id, step_idx)",
    # Messenger 陌生人「消息请求」可视化（2026-08-11）：worker ingest 早就携带
    # is_request/request_category（general=可自动回 / spam=只入箱），但此前只用于
    # 预置会话档位后即丢——前端看不出「这是待验证的陌生人」，坐席也不知道
    # 「回复即自动通过验证」（worker 发送前会点官方「接受」按钮）。落列后
    # store_row_to_chat 透传 → 会话列表徽章 + 打开会话引导条；出站消息一落库
    # 即自动清零（回复=接受，见 ingest_message）；显式接受/拒绝经
    # /api/platforms/messenger/{acct}/request-action 代理 worker。纯加法缺省 0/空。
    "ALTER TABLE conversations ADD COLUMN is_request INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE conversations ADD COLUMN request_category TEXT NOT NULL DEFAULT ''",
]


def _message_pk(conversation_id: str, platform_msg_id: str, text: str, ts: Any) -> str:
    """确定性消息主键：有平台 id 用平台 id，否则用 hash(text|ts) 兜底。

    这样无 platform_msg_id 的 RPA 消息也能稳定去重（避免 (conv, '') 唯一约束
    把同会话所有无 id 消息折叠成一条）。
    """
    pid = str(platform_msg_id or "").strip()
    if pid:
        return f"{conversation_id}:{pid}"
    digest = hashlib.sha256(f"{text}|{ts}".encode("utf-8")).hexdigest()[:16]
    return f"{conversation_id}:h:{digest}"


class InboxStore:
    """线程安全的 SQLite 封装。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        # C3：L2 草稿写入通知钩子（AutosendWorker 注册，线程安全回调）
        self._l2_callbacks: List[Any] = []
        # E2：入站新消息通知钩子（AutoDraft 注册，参数 conv_dict + text）
        self._new_inbound_cbs: List[Any] = []
        # Q 延伸：ingest 热路径 contact_id 反查（contacts_store → contact_id）
        self._contact_resolver: Any = None
        # 去重护栏命中计数（可观测）：量化跨路径重复被收敛多少，判断是否需更精确的发送侧幂等键
        self._dedup_counts: Dict[str, int] = {"skipped_hash": 0, "deleted_hash": 0}
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_DDL)
            self._run_migrations()
            self._conn.commit()
            # U1: FTS5 冷启动重建（首次建表后一次性填充存量消息，后续由触发器维护）
            self._fts5_available = self._rebuild_fts5_if_empty()

    def _record_migration(self, mig_id: int) -> None:
        """记录一条已应用 migration（幂等）。调用方已持锁。"""
        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(mig_id, applied_at) VALUES (?, ?)",
                (int(mig_id), self._now()),
            )
        except Exception:
            logger.debug("[InboxStore] 记录 migration #%d 失败", mig_id, exc_info=True)

    @staticmethod
    def _migration_sha(sql: str) -> str:
        """migration 记账键＝SQL 归一化（压空白）后的 sha1——与列表位置无关。"""
        return hashlib.sha1(" ".join(str(sql).split()).encode("utf-8")).hexdigest()

    def _record_migration_sha(self, sha: str) -> None:
        """记录一条已应用 migration（内容哈希键，幂等）。调用方已持锁。"""
        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO schema_migrations_sha(sql_sha, applied_at)"
                " VALUES (?, ?)",
                (str(sha), self._now()),
            )
        except Exception:
            logger.debug(
                "[InboxStore] 记录 migration sha=%s 失败", sha[:8], exc_info=True)

    def _run_migrations(self) -> None:
        """执行 ``_MIGRATIONS``——可观测替代原「for sql: try/except: pass」静默吞错。

        记账键＝**SQL 内容哈希**（``schema_migrations_sha``，2026-08-02 起）：
        旧版按列表位置（``schema_migrations.mig_id``＝list index）记账，任何一次
        「往列表中段插入/删除条目」都会让后续条目索引错位、撞上已记录的旧 id 被
        静默跳过——当日实锤：T-mood 三条 ALTER 只有一条在生产库生效（转向列缺失、
        功能静默失效）。换内容键后与位置彻底解耦；首个启动会把历史条目全量重试
        一遍，安全性由列表构造保证：ALTER 撞重复列＝良性跳过、CREATE TABLE/INDEX
        均带 IF NOT EXISTS、两条 UPDATE 回填各自自证幂等（chat_type / archived_at 条目注释）。
        **新增 migration 必须保持这个「可安全重跑」不变量。**
        旧 ``schema_migrations`` 表保留：过程式一次性迁移（900001+ 保留 id）仍用它，
        历史行留作诊断，不迁移不清理。

        - 「duplicate column name」是新库(列已由 _DDL 建)/旧库(已迁)的良性重复 → debug + 记为已应用；
        - 其余 ``OperationalError``/异常 → ``logger.error`` + ``self.migration_errors`` 计数
          （不 crash，保可用性，但不再静默——升级后缺列/半迁移可被发现）。
        调用方已持有 ``self._lock``。
        """
        self.migration_errors = 0
        conn = self._conn
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "mig_id INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
            )
            applied = {
                int(r[0])
                for r in conn.execute("SELECT mig_id FROM schema_migrations").fetchall()
            }
        except Exception:
            logger.error("[InboxStore] schema_migrations 版本表初始化失败", exc_info=True)
            applied = set()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations_sha ("
                "sql_sha TEXT PRIMARY KEY, applied_at REAL NOT NULL)"
            )
            applied_sha = {
                str(r[0]) for r in conn.execute(
                    "SELECT sql_sha FROM schema_migrations_sha").fetchall()
            }
        except Exception:
            logger.error(
                "[InboxStore] schema_migrations_sha 版本表初始化失败", exc_info=True)
            applied_sha = set()
        for _idx, _sql in enumerate(_MIGRATIONS):
            _sha = self._migration_sha(_sql)
            if _sha in applied_sha:
                continue
            try:
                conn.execute(_sql)
                self._record_migration_sha(_sha)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" in str(exc).lower():
                    # 新库(列已由 _DDL 建)或旧库(已迁过)——良性，记为已应用不再重试。
                    self._record_migration_sha(_sha)
                    logger.debug("[InboxStore] migration #%d 列已存在（良性跳过）", _idx)
                else:
                    self.migration_errors += 1
                    logger.error(
                        "[InboxStore] migration #%d 执行失败（已跳过，未记录）: %s",
                        _idx, exc, exc_info=True)
            except Exception as exc:
                self.migration_errors += 1
                logger.error(
                    "[InboxStore] migration #%d 非预期失败（已跳过）: %s",
                    _idx, exc, exc_info=True)
        # P3-198 一次性数据合并（过程式，无法表达为单条 SQL 故不进 _MIGRATIONS 列表；
        # 用**保留高位 id** 做 marker，与列表索引空间永不冲突）：WhatsApp 设备后缀
        # 会话（chat_key='num:0'）并入规范会话（'num'）。
        if _WA_KEY_MERGE_MIG_ID not in applied:
            try:
                _n = self._merge_wa_device_suffix_convs(conn)
                self._record_migration(_WA_KEY_MERGE_MIG_ID)
                if _n:
                    logger.info(
                        "[InboxStore] WhatsApp 设备后缀会话合并完成：%d 个会话并入规范身份", _n)
            except Exception:
                self.migration_errors += 1
                logger.error("[InboxStore] WA 设备后缀会话合并失败（已跳过）", exc_info=True)
        # 900002：chat_key='0' 的 WA 会话＝历史 chats 同步坏 jid 的空占位（'0' 不可能是
        # 合法 MSISDN/群 id）。只删**零消息**的——万一有消息，宁可留着可见也不删数据。
        if _WA_ZERO_KEY_MIG_ID not in applied:
            try:
                cur = conn.execute(
                    "DELETE FROM conversations WHERE platform='whatsapp' "
                    "AND chat_key='0' AND conversation_id NOT IN "
                    "(SELECT DISTINCT conversation_id FROM messages)")
                self._record_migration(_WA_ZERO_KEY_MIG_ID)
                if cur.rowcount:
                    conn.commit()
                    logger.info(
                        "[InboxStore] 清理结构性无效 WA 占位会话（chat_key='0'）：%d 行",
                        cur.rowcount)
            except Exception:
                self.migration_errors += 1
                logger.error("[InboxStore] WA 无效占位清理失败（已跳过）", exc_info=True)

    def _merge_wa_device_suffix_convs(self, conn) -> int:
        """P3-198：把 'whatsapp:acct:num:dev' 会话整树迁并进 'whatsapp:acct:num'。

        事故背景（2026-07-31 实锤）：历史同步产生的设备后缀 chat_key（'639531765880:0'）
        与规范身份（'639531765880'）把同一客户裂成两个会话——线程分叉、主动触达按
        错误 key 发送（边车旧 toJid 把后缀并进号码 → 发到不存在的号码）。入口归一
        （sidecar + 内桥 handler）修的是**增量**；本迁移收**存量**。

        策略（调用方已持锁；逐会话处理，绝不抛）：
          - 含 conversation_id 列的表统一迁移：``UPDATE OR IGNORE`` 改写
            conversation_id（+同表 message_id/draft_id 前缀替换、chat_key 覆写），
            与目标会话撞唯一键的残留行 DELETE（目标行=真身，幻影行=同一平台消息的
            重复镜像，丢弃即去重）；
          - messages_fts 是独立 FTS5 表且触发器只挂 text 更新 → 显式同步改写；
          - conversations：目标已存在 → 按 last_ts 新者刷新预览后删幻影行；
            不存在 → 原行改名为规范身份。
        返回合并的会话数。
        """
        import re as _re
        try:
            rows = conn.execute(
                "SELECT conversation_id, account_id, chat_key FROM conversations "
                "WHERE platform = 'whatsapp' AND chat_key GLOB '*:*'"
            ).fetchall()
        except Exception:
            return 0
        merged = 0
        for r in rows:
            old_cid = str(r["conversation_id"])
            acct = str(r["account_id"] or "")
            key = str(r["chat_key"] or "")
            m = _re.fullmatch(r"(\d+):\d+", key)
            if not m:
                continue  # 非设备后缀形态（如 LINE 风格键误入）不动
            bare = m.group(1)
            new_cid = f"whatsapp:{acct}:{bare}"
            # 1) messages：改写 conversation_id + message_id 前缀；撞键残留=重复镜像，删
            #    （DELETE 触发 messages_fts_ad 按旧 message_id 清掉对应 FTS 行）
            conn.execute(
                "UPDATE OR IGNORE messages SET conversation_id = ?, "
                "message_id = replace(message_id, ?, ?) WHERE conversation_id = ?",
                (new_cid, old_cid, new_cid, old_cid))
            conn.execute(
                "DELETE FROM messages WHERE conversation_id = ?", (old_cid,))
            # 2) FTS 显式同步（触发器只挂 UPDATE OF text，迁移改 id 不触发）
            try:
                conn.execute(
                    "UPDATE messages_fts SET conversation_id = ?, "
                    "message_id = replace(message_id, ?, ?) WHERE conversation_id = ?",
                    (new_cid, old_cid, new_cid, old_cid))
            except Exception:
                logger.debug("[InboxStore] WA 合并 FTS 同步跳过", exc_info=True)
            # 3) 其余含 conversation_id 列的表（结构发现式，防新表漏迁）
            try:
                tables = [
                    str(t[0]) for t in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                    if not str(t[0]).startswith(("messages_fts", "sqlite_", "schema_migrations"))
                    and str(t[0]) not in ("messages", "conversations")
                ]
            except Exception:
                tables = []
            for t in tables:
                try:
                    cols = {str(c[1]) for c in conn.execute(f"PRAGMA table_info({t})")}
                except Exception:
                    continue
                if "conversation_id" not in cols:
                    continue
                sets = ["conversation_id = ?"]
                params: list = [new_cid]
                if "message_id" in cols:
                    sets.append("message_id = replace(message_id, ?, ?)")
                    params += [old_cid, new_cid]
                if "draft_id" in cols:
                    sets.append("draft_id = replace(draft_id, ?, ?)")
                    params += [old_cid, new_cid]
                if "chat_key" in cols:
                    sets.append("chat_key = ?")
                    params.append(bare)
                params.append(old_cid)
                try:
                    conn.execute(
                        f"UPDATE OR IGNORE {t} SET {', '.join(sets)} "
                        "WHERE conversation_id = ?", params)
                    conn.execute(
                        f"DELETE FROM {t} WHERE conversation_id = ?", (old_cid,))
                except Exception:
                    logger.debug("[InboxStore] WA 合并表 %s 跳过", t, exc_info=True)
            # 4) conversations 本体：目标存在则保真身、按新者刷新预览；否则原行转正
            try:
                tgt = conn.execute(
                    "SELECT conversation_id, last_ts FROM conversations "
                    "WHERE conversation_id = ?", (new_cid,)).fetchone()
                if tgt is None:
                    conn.execute(
                        "UPDATE conversations SET conversation_id = ?, chat_key = ? "
                        "WHERE conversation_id = ?", (new_cid, bare, old_cid))
                else:
                    src_row = conn.execute(
                        "SELECT last_text, last_ts FROM conversations "
                        "WHERE conversation_id = ?", (old_cid,)).fetchone()
                    if src_row and float(src_row["last_ts"] or 0) > float(tgt["last_ts"] or 0):
                        conn.execute(
                            "UPDATE conversations SET last_text = ?, last_ts = ? "
                            "WHERE conversation_id = ?",
                            (src_row["last_text"], src_row["last_ts"], new_cid))
                    conn.execute(
                        "DELETE FROM conversations WHERE conversation_id = ?", (old_cid,))
                merged += 1
            except Exception:
                logger.debug("[InboxStore] WA 合并 conversations 行失败 %s", old_cid,
                             exc_info=True)
        if merged:
            try:
                conn.commit()
            except Exception:
                pass
        return merged

    def _rebuild_fts5_if_empty(self) -> bool:
        """U1：检查 FTS5 表是否可用且已填充；若空则从存量 messages 批量导入。

        返回 True 表示 FTS5 可用（可用于 search_messages 优先路径）。
        """
        try:
            # 确认 messages_fts 表存在
            self._conn.execute("SELECT count(*) FROM messages_fts LIMIT 1").fetchone()
        except Exception:
            return False  # FTS5 不可用（SQLite 编译时未包含）
        try:
            # 若 FTS5 表为空且 messages 表有数据，执行全量同步（best-effort）
            fts_cnt = self._conn.execute(
                "SELECT count(*) FROM messages_fts"
            ).fetchone()[0]
            if fts_cnt == 0:
                self._conn.execute(
                    """INSERT INTO messages_fts(message_id, conversation_id, text, ts, direction)
                       SELECT message_id, conversation_id, text, ts, direction
                       FROM messages WHERE text != ''"""
                )
                self._conn.commit()
            return True
        except Exception:
            return False  # 插入失败，降级为 LIKE

    def register_l2_callback(self, cb: Any) -> None:
        """注册 L2 草稿写入通知回调（C3 事件驱动 AutosendWorker）。

        cb 为无参可调用对象，在 upsert_draft 写入 L2 草稿后从同步上下文调用。
        实现方可用 loop.call_soon_threadsafe 安全地唤醒异步任务。
        """
        self._l2_callbacks.append(cb)

    def register_new_inbound_cb(self, cb: Any) -> None:
        """注册入站新消息通知回调（E2 自动草稿生成）。

        cb 签名：cb(conv: dict, text: str)，在 ingest 检测到新入站消息后调用。
        在 ingest 锁外调用，best-effort，异常自动静默。
        """
        self._new_inbound_cbs.append(cb)

    def register_contact_resolver(self, cb: Any) -> None:
        """Q 延伸：注册 contact_id 反查回调。

        cb 签名：``cb(platform, account_id, chat_key) -> contact_id str``；
        ingest 旁路写入 ``conversations`` / ``conversation_meta`` 时 best-effort 调用。
        """
        self._contact_resolver = cb

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def dedup_stats(self) -> Dict[str, int]:
        """去重护栏命中累计（进程生命周期内）：

        - ``skipped_hash``：插 hash 兜底行时发现精确-ts pmid 孪生 → 跳过（多为入站 re-ingest 重复）；
        - ``deleted_hash``：权威 pmid 行落库时删掉早先 hash 孪生（多为出站乐观镜像 vs 回显）。

        供 /api/workspace/metrics 观测，判断各路径重复量、是否需把幂等键上提到更多发送侧。
        """
        return dict(self._dedup_counts)

    @staticmethod
    def _now() -> float:
        return time.time()

    # ── 写入（ingest 调用，幂等）──────────────────────────────

    def upsert_conversation(self, conv: InboxConversation) -> None:
        if not conv.conversation_id or not conv.platform:
            return
        now = self._now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO conversations
                    (conversation_id, platform, account_id, chat_key, contact_id,
                     display_name, language, chat_type, last_text, last_ts, unread,
                     username, phone, avatar_url, first_seen,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    -- 昵称优先级覆盖：真实昵称（非空且不等于裸 chat_key）随时更新；
                    -- 但**绝不**用空名/裸号码把已存的真实昵称冲掉——只有当现值本身还是
                    -- 空/裸号码时才用来兜底。修「采到真名后被后续空名覆盖回数字 id」。
                    display_name = CASE
                        WHEN excluded.display_name != ''
                             AND excluded.display_name != excluded.chat_key
                            THEN excluded.display_name
                        WHEN conversations.display_name = ''
                             OR conversations.display_name = conversations.chat_key
                            THEN excluded.display_name
                        ELSE conversations.display_name END,
                    language = CASE WHEN excluded.language != 'unknown'
                                    THEN excluded.language ELSE conversations.language END,
                    chat_type = CASE WHEN excluded.chat_type != 'private'
                                     THEN excluded.chat_type ELSE conversations.chat_type END,
                    last_text = CASE WHEN excluded.last_ts >= conversations.last_ts
                                     THEN excluded.last_text ELSE conversations.last_text END,
                    last_ts = MAX(excluded.last_ts, conversations.last_ts),
                    unread = excluded.unread,
                    contact_id = CASE WHEN excluded.contact_id != ''
                                      THEN excluded.contact_id ELSE conversations.contact_id END,
                    -- 身份画像：仅在带来非空值时更新（空值不覆盖已采集到的真实身份）
                    username   = CASE WHEN excluded.username   != ''
                                      THEN excluded.username   ELSE conversations.username END,
                    phone      = CASE WHEN excluded.phone      != ''
                                      THEN excluded.phone      ELSE conversations.phone END,
                    avatar_url = CASE WHEN excluded.avatar_url != ''
                                      THEN excluded.avatar_url ELSE conversations.avatar_url END,
                    -- 首次接触：单调保最早（现值>0 则保留，否则用新值兜底）
                    first_seen = CASE WHEN conversations.first_seen > 0
                                      THEN conversations.first_seen ELSE excluded.first_seen END,
                    updated_at = excluded.updated_at
                """,
                (
                    conv.conversation_id, conv.platform, conv.account_id, conv.chat_key,
                    conv.contact_id, conv.display_name, conv.language,
                    (conv.chat_type or "private"), conv.last_text,
                    float(conv.last_ts or 0), int(conv.unread or 0),
                    conv.username, conv.phone, conv.avatar_url,
                    (float(conv.first_seen or 0) or now),
                    now, now,
                ),
            )
            self._conn.commit()

    def ingest_message(self, msg: InboxMessage) -> bool:
        """INSERT OR IGNORE，返回是否新插入。"""
        if not msg.conversation_id:
            return False
        mid = _message_pk(msg.conversation_id, msg.platform_msg_id, msg.text, msg.ts)
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO messages
                    (message_id, conversation_id, platform_msg_id, direction, text,
                     original_text, translated_text, source_lang, target_lang,
                     media_type, media_ref, ts, ingested_at,
                     reply_to_id, reply_to_text, reply_to_sender, mentions_json,
                     sender_id, sender_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mid, msg.conversation_id, str(msg.platform_msg_id or ""), msg.direction,
                    msg.text, msg.original_text or msg.text, msg.translated_text,
                    msg.source_lang, msg.target_lang, msg.media_type, msg.media_ref,
                    float(msg.ts or 0), self._now(),
                    str(getattr(msg, "reply_to_id", "") or ""),
                    str(getattr(msg, "reply_to_text", "") or ""),
                    str(getattr(msg, "reply_to_sender", "") or ""),
                    str(getattr(msg, "mentions_json", "") or "[]"),
                    str(getattr(msg, "sender_id", "") or ""),
                    str(getattr(msg, "sender_name", "") or ""),
                ),
            )
            inserted = cur.rowcount > 0
            # 回复即接受：任何**新插入的出站**落库即撤「陌生人消息请求」标记——
            # worker 发送时会自动点官方「接受」按钮（messenger-web clickAcceptRequest），
            # 这里同步撤前端徽章/引导条。WHERE is_request=1 自守卫，非请求会话零成本。
            if inserted and msg.direction == "out":
                self._conn.execute(
                    "UPDATE conversations SET is_request=0, request_category=''"
                    " WHERE conversation_id=? AND is_request=1",
                    (msg.conversation_id,))
            self._conn.commit()
        # 激活里程碑（锁外，热路径稳态＝一次布尔判断）：只认真实新插入的新鲜出站
        if inserted and msg.direction == "out" and not _FIRST_REPLY_NOTED:
            _note_first_reply_milestone(msg)
        return inserted

    def ingest_batch(self, conv: InboxConversation, msgs: List[InboxMessage]) -> int:
        """一个事务内 upsert 会话 + 批量 ingest 消息；返回新插入消息条数。"""
        if not conv.conversation_id or not conv.platform:
            return 0
        now = self._now()
        inserted = 0
        # 本批**真正新插入**的入站消息里最晚的 ts（0=本批没有新入站消息）。
        # 只有它能驱动「归档会话自动复活」，见 _unarchive_on_inbound 的判据说明。
        _new_inbound_ts = 0.0
        with self._lock:
            # P4 埋点：upsert 前按主键探测是否「新会话首次入库」（PK 查询微秒级）
            _tele_new_conv = self._conn.execute(
                "SELECT 1 FROM conversations WHERE conversation_id=? LIMIT 1",
                (conv.conversation_id,)).fetchone() is None
            self._conn.execute(
                """
                INSERT INTO conversations
                    (conversation_id, platform, account_id, chat_key, contact_id,
                     display_name, language, chat_type, last_text, last_ts, unread,
                     username, phone, avatar_url, first_seen,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    -- 昵称优先级覆盖：真实昵称（非空且不等于裸 chat_key）随时更新；
                    -- 但**绝不**用空名/裸号码把已存的真实昵称冲掉——只有当现值本身还是
                    -- 空/裸号码时才用来兜底。修「采到真名后被后续空名覆盖回数字 id」。
                    display_name = CASE
                        WHEN excluded.display_name != ''
                             AND excluded.display_name != excluded.chat_key
                            THEN excluded.display_name
                        WHEN conversations.display_name = ''
                             OR conversations.display_name = conversations.chat_key
                            THEN excluded.display_name
                        ELSE conversations.display_name END,
                    language = CASE WHEN excluded.language != 'unknown'
                                    THEN excluded.language ELSE conversations.language END,
                    chat_type = CASE WHEN excluded.chat_type != 'private'
                                     THEN excluded.chat_type ELSE conversations.chat_type END,
                    last_text = CASE WHEN excluded.last_ts >= conversations.last_ts
                                     THEN excluded.last_text ELSE conversations.last_text END,
                    last_ts = MAX(excluded.last_ts, conversations.last_ts),
                    unread = excluded.unread,
                    contact_id = CASE WHEN excluded.contact_id != ''
                                      THEN excluded.contact_id ELSE conversations.contact_id END,
                    -- 身份画像：仅在带来非空值时更新（空值不覆盖已采集到的真实身份）
                    username   = CASE WHEN excluded.username   != ''
                                      THEN excluded.username   ELSE conversations.username END,
                    phone      = CASE WHEN excluded.phone      != ''
                                      THEN excluded.phone      ELSE conversations.phone END,
                    avatar_url = CASE WHEN excluded.avatar_url != ''
                                      THEN excluded.avatar_url ELSE conversations.avatar_url END,
                    -- 首次接触：单调保最早（现值>0 则保留，否则用新值兜底）
                    first_seen = CASE WHEN conversations.first_seen > 0
                                      THEN conversations.first_seen ELSE excluded.first_seen END,
                    updated_at = excluded.updated_at
                """,
                (
                    conv.conversation_id, conv.platform, conv.account_id, conv.chat_key,
                    conv.contact_id, conv.display_name, conv.language,
                    (conv.chat_type or "private"), conv.last_text,
                    float(conv.last_ts or 0), int(conv.unread or 0),
                    conv.username, conv.phone, conv.avatar_url,
                    (float(conv.first_seen or 0) or now),
                    now, now,
                ),
            )
            for msg in msgs or []:
                if not msg.conversation_id:
                    continue
                # 跨 ingest 路径去重护栏：同一条消息可能经「带 platform_msg_id 的权威路径」
                # 与「无 id 的兜底路径」分别落库，二者主键不同（``:<pmid>`` vs ``:h:<hash>``）本会
                # 凭空多出重复行并重复触发 new_inbound 回调（→ 重复 auto-draft）。按 direction 区分
                # 两种到达顺序，把两种键收敛为一条，且**绝不丢消息**：
                # - 入站：权威 pmid 先到（实时 handler）、hash 后到（聚合 re-ingest / thread 重放）→
                #   插 hash 前若已有同 (conv,text,**精确 ts**,dir) 的 pmid 孪生即跳过；
                # - 出站：乐观 hash 先到（_emit_inbox 用 send-time ts）、权威 pmid 后到（自身已发消息
                #   被消息处理器回显，ts=message.date 与 send-time 有秒级漂移）→ pmid 落库时删掉早先
                #   的 hash 孪生（按 dir+text 在时间窗内匹配，容忍 ts 漂移）。出站不做「跳过 hash」以免
                #   回显缺失时丢发言；故 skip 仅用精确 ts（出站漂移天然不命中），零丢失。
                _pmid = str(msg.platform_msg_id or "").strip()
                _tsf = float(msg.ts or 0)
                _win = 120.0 if msg.direction == "out" else 0.0
                if not _pmid:
                    _twin = self._conn.execute(
                        "SELECT 1 FROM messages WHERE conversation_id=? AND text=? AND ts=? "
                        "AND direction=? AND platform_msg_id != '' LIMIT 1",
                        (msg.conversation_id, msg.text, _tsf, msg.direction),
                    ).fetchone()
                    if _twin is not None:
                        self._dedup_counts["skipped_hash"] += 1
                        continue
                else:
                    _cur = self._conn.execute(
                        "DELETE FROM messages WHERE conversation_id=? AND text=? "
                        "AND direction=? AND platform_msg_id = '' AND message_id LIKE '%:h:%' "
                        "AND ABS(ts - ?) <= ?",
                        (msg.conversation_id, msg.text, msg.direction, _tsf, _win),
                    )
                    if (_cur.rowcount or 0) > 0:
                        self._dedup_counts["deleted_hash"] += int(_cur.rowcount)
                mid = _message_pk(msg.conversation_id, msg.platform_msg_id, msg.text, msg.ts)
                cur = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO messages
                        (message_id, conversation_id, platform_msg_id, direction, text,
                         original_text, translated_text, source_lang, target_lang,
                         media_type, media_ref, ts, ingested_at,
                         reply_to_id, reply_to_text, reply_to_sender, mentions_json,
                         sender_id, sender_name)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mid, msg.conversation_id, str(msg.platform_msg_id or ""), msg.direction,
                        msg.text, msg.original_text or msg.text, msg.translated_text,
                        msg.source_lang, msg.target_lang, msg.media_type, msg.media_ref,
                        float(msg.ts or 0), now,
                        str(msg.reply_to_id or ""), str(msg.reply_to_text or ""),
                        str(msg.reply_to_sender or ""), str(msg.mentions_json or "[]"),
                        str(msg.sender_id or ""), str(msg.sender_name or ""),
                    ),
                )
                if cur.rowcount > 0:
                    inserted += 1
                    if msg.direction == "in":
                        _new_inbound_ts = max(_new_inbound_ts, float(msg.ts or 0))
            self._conn.commit()
        # 归档会话收到「归档之后」的新入站消息 → 自动解档，防客户被静默埋没。
        # 放在 with 块之外：_unarchive_on_inbound 自己取锁，且它与 ingest 无需同一事务
        # （最坏情况＝解档延到下一条消息，不会丢消息）。
        if _new_inbound_ts > 0:
            try:
                self._unarchive_on_inbound(conv.conversation_id, _new_inbound_ts)
            except Exception:   # 复活是增益路径，绝不能反过来阻断消息入库
                logger.warning("[InboxStore] 入站自动解档失败（消息已入库）", exc_info=True)
        if _tele_new_conv:
            try:   # P4 埋点：新会话首次入库（fail-silent，绝不影响 ingest）
                from src.utils.telemetry import track
                track("session.started", {
                    "session_id": conv.conversation_id, "platform": conv.platform,
                    "chat_type": conv.chat_type or "private"})
            except Exception:
                pass
        return inserted

    # ── P0：协议通讯录（好友名单）+ 会话列表占位 ──────────────────────────

    def upsert_protocol_contacts(
        self, platform: str, account_id: str, rows: List[Dict[str, Any]],
    ) -> int:
        """批量写入平台通讯录（好友名单）。返回处理条数。

        附带补名：把已存在会话里仍是「裸号码」(display_name 空或 == chat_key) 的
        display_name 用通讯录名补齐——**不覆盖**已有真名（贴近官方「显示联系人备注名」）。
        """
        platform = str(platform or "").lower()
        account_id = str(account_id or "")
        if not platform or not account_id or not rows:
            return 0
        now = self._now()
        n = 0
        with self._lock:
            for r in rows:
                if not isinstance(r, dict):
                    continue
                ck = str(r.get("jid") or r.get("chat_key") or "").strip()
                if not ck:
                    continue
                name = str(r.get("name") or "").strip()
                notify = str(r.get("notify") or r.get("notify_name") or "").strip()
                self._conn.execute(
                    """INSERT INTO protocol_contacts
                         (platform, account_id, chat_key, name, notify_name, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(platform, account_id, chat_key) DO UPDATE SET
                         name = CASE WHEN excluded.name != '' THEN excluded.name
                                     ELSE protocol_contacts.name END,
                         notify_name = CASE WHEN excluded.notify_name != ''
                                            THEN excluded.notify_name
                                            ELSE protocol_contacts.notify_name END,
                         updated_at = excluded.updated_at""",
                    (platform, account_id, ck, name, notify, now),
                )
                n += 1
                disp = name or notify
                if disp:
                    cid = f"{platform}:{account_id}:{ck}"
                    self._conn.execute(
                        """UPDATE conversations SET display_name=?, updated_at=?
                             WHERE conversation_id=?
                               AND (display_name='' OR display_name=chat_key)""",
                        (disp, now, cid),
                    )
            self._conn.commit()
        return n

    def list_protocol_contacts(
        self, platform: str, account_id: str, *, limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """读取某账号的通讯录（好友名单）。有名字的排前，便于展示。"""
        platform = str(platform or "").lower()
        account_id = str(account_id or "")
        limit = max(1, min(5000, int(limit or 1000)))
        with self._lock:
            rows = self._conn.execute(
                """SELECT chat_key, name, notify_name, updated_at
                     FROM protocol_contacts
                    WHERE platform=? AND account_id=?
                    ORDER BY (name != '') DESC, name, chat_key LIMIT ?""",
                (platform, account_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_protocol_contacts_enriched(
        self, platform: str, account_id: str, *,
        limit: int = 1000, only: str = "", query: str = "",
        silent_days: float = PROTOCOL_CONTACT_SILENT_DAYS, now: Optional[float] = None,
        include_chats: bool = False,
    ) -> List[Dict[str, Any]]:
        """好友名单 + 会话状态联表（市场侧「加了好友但从没开口」的资产盘点数据源）。

        以 ``protocol_contacts`` 为主表 LEFT JOIN ``conversations``（关联键与
        ``upsert_protocol_contacts`` 同口径的 ``platform:account_id:chat_key``），
        在 ``{chat_key, name, notify_name, updated_at}`` 之上补：

        - ``has_conversation``：库里是否存在这条会话（占位会话也算）；
        - ``last_ts``：会话最后活动时间（无会话/仅占位无消息 → 0）；
        - ``unread``：未读数，走 ``effective_unread`` 同口径（已读水位覆盖末条即 0），
          与工作台会话列表保持一致，避免「通讯录说有未读、列表说没有」的漂移；
        - ``never_spoke``：派生「未开口」徽章——无会话 **或** 会话从无消息；
        - ``in_book``：是否在通讯录里（``include_chats=False`` 时恒 ``True``——
          那时基表就是通讯录本身，口径自洽）。

        **刻意不返回 msg_count**：``conversations`` 表没有现成的消息计数列，而为几千
        联系人逐行 ``COUNT(messages)`` 会退化成全表扫（messages 只有
        ``(conversation_id, ts)`` 索引，N 次子查询 × 上万行）。运营真正要区分的是
        「聊过 / 没聊过」而非精确条数，故用 ``last_ts > 0`` 作判据——占位会话
        （``upsert_protocol_chats`` 建的、从无消息）last_ts 为 0，同样被判为未开口。

        ``only`` 档位：``""``=全部、``"never_spoke"``=只看未开口、``"silent"``=聊过但
        超过 ``silent_days``（默认见 ``PROTOCOL_CONTACT_SILENT_DAYS``）没动静、
        ``"chat_only"``=有往来但没存进通讯录（运营语义＝「该补进通讯录的客户」，
        仅在 ``include_chats=True`` 下有意义，否则必然是空集）。
        ``query`` 为空即全量（保持旧「全量拉下来前端本地筛」的调用方向后兼容）。

        ``include_chats``（默认 **False** = 今天的行为逐字节不变）为 True 时基表
        换成「人的并集」，见 ``_contacts_base_from`` 的说明与复杂度分析。

        筛选与截断都在 SQL 里做：几千联系人先 LIMIT 再筛会漏掉靠后的命中项。
        """
        platform = str(platform or "").lower()
        account_id = str(account_id or "")
        # 上限与 list_protocol_contacts 保持一致（5000），防单次请求把整库拽进内存
        limit = max(1, min(5000, int(limit or 1000)))
        only = str(only or "").strip().lower()
        now = time.time() if now is None else float(now)
        cutoff = now - max(0.0, float(silent_days)) * 86400.0

        # 基表取候选（走索引，见 _contacts_base_from），再按会话主键
        # (conversations.conversation_id 是 PK) 逐行等值 JOIN——3000 联系人 = 一次
        # 索引区间扫 + 3000 次 PK 点查。
        # ⚠ params 必须严格按 SQL 文本里 ? 的出现顺序追加：
        # 基表/JOIN（from_params）→ 基表 WHERE → only → query → LIMIT。
        from_sql, params, where, base_wparams = _contacts_base_from(
            platform, account_id, include_chats)
        params = list(params)
        where = list(where)
        params.extend(base_wparams)
        if only == "never_spoke":
            where.append("COALESCE(v.last_ts, 0) <= 0")
        elif only == "silent":
            where.append("COALESCE(v.last_ts, 0) > 0")
            where.append("COALESCE(v.last_ts, 0) <= ?")
            params.append(cutoff)
        elif only == "chat_only":
            # 不并集时基表就是通讯录，in_book 列根本不存在 → 用恒假常量返回空集
            # （语义上「没存进通讯录的人」在纯通讯录口径下本就不存在），而不是报错：
            # 前端可能在切换口径的一瞬间把旧档位带上来，不该 500。
            where.append("c.in_book = 0" if include_chats else "1 = 0")
        qclause, qparams = _contact_query_clause(query)
        if qclause:
            where.append(qclause)
            params.extend(qparams)
        params.append(limit)
        sql = (
            "SELECT c.chat_key, c.name, c.notify_name, c.updated_at, "
            # 纯通讯录口径下每一行按定义都在通讯录里 → 常量 1，让取行代码只有一套
            + ("c.in_book AS in_book, " if include_chats else "1 AS in_book, ") +
            "       v.conversation_id AS cid, "
            "       COALESCE(v.last_ts, 0) AS last_ts, "
            "       COALESCE(v.unread, 0) AS unread, "
            "       COALESCE(v.last_read_ts, 0) AS last_read_ts "
            + from_sql
            + ((" WHERE " + " AND ".join(where)) if where else "") +
            # 有名字的排前（裸号码沉底）→ 最近有互动的排前 → 名字/号码稳定兜底
            " ORDER BY (c.name != '') DESC, last_ts DESC, c.name, c.chat_key LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            last_ts = float(r["last_ts"] or 0)
            out.append({
                "chat_key": r["chat_key"],
                "name": r["name"],
                "notify_name": r["notify_name"],
                "updated_at": float(r["updated_at"] or 0),
                "has_conversation": r["cid"] is not None,
                "last_ts": last_ts,
                "unread": self.effective_unread({
                    "last_ts": last_ts,
                    "last_read_ts": float(r["last_read_ts"] or 0),
                    "unread": int(r["unread"] or 0),
                }),
                "never_spoke": (r["cid"] is None) or last_ts <= 0,
                "in_book": bool(r["in_book"]),
            })
        return out

    def protocol_contacts_summary(
        self, platform: str, account_id: str, *,
        query: str = "", silent_days: float = PROTOCOL_CONTACT_SILENT_DAYS,
        now: Optional[float] = None, include_chats: bool = False,
    ) -> Dict[str, int]:
        """好友名单资产盘点汇总（未开口 / 沉默 / 已有会话 / 在册 / 仅会话 的条数）。

        口径：**不受 ``only`` 档位影响**（档位是视图筛选，汇总要给出全景基数，否则
        「只看未开口」时 never_spoke 会恒等于 total 而失去参考意义），但**跟随
        ``query``**（搜索时数字与列表同一集合，读感一致）。

        ``include_chats`` 与 ``list_protocol_contacts_enriched`` 同义、共用同一个
        基表构造（``_contacts_base_from``），保证两者数字永远同源。并集口径下多回
        两个数：``in_book``（在通讯录里的人数）/ ``chat_only``（只有会话的人数），
        两者相加恒等于 ``total``；其余字段口径不变，只是基数变成并集。

        单条聚合 SQL，不受 limit 截断——3000 联系人也只是一次索引扫 + PK 点查。
        """
        platform = str(platform or "").lower()
        account_id = str(account_id or "")
        now = time.time() if now is None else float(now)
        cutoff = now - max(0.0, float(silent_days)) * 86400.0
        qclause, qparams = _contact_query_clause(query)
        # ⚠ 绑定顺序必须与下面 SQL 文本里 ? 出现的先后一致：
        # cutoff(SELECT) → 基表/JOIN(FROM，见 _contacts_base_from) → 基表 WHERE
        # → query(WHERE)。并集版把基表参数插在 JOIN 前缀之前，与通讯录版不同。
        from_sql, from_params, where, base_wparams = _contacts_base_from(
            platform, account_id, include_chats)
        where = list(where)
        params: List[Any] = [cutoff] + list(from_params) + list(base_wparams)
        if qclause:
            where.append(qclause)
            params.extend(qparams)
        sql = (
            "SELECT COUNT(*) AS total, "
            "       SUM(CASE WHEN COALESCE(v.last_ts,0) <= 0 THEN 1 ELSE 0 END) "
            "           AS never_spoke, "
            "       SUM(CASE WHEN v.conversation_id IS NOT NULL THEN 1 ELSE 0 END) "
            "           AS with_conversation, "
            "       SUM(CASE WHEN COALESCE(v.last_ts,0) > 0 "
            "                 AND COALESCE(v.last_ts,0) <= ? THEN 1 ELSE 0 END) "
            "           AS silent, "
            # 纯通讯录口径下人人在册 → in_book == total、chat_only == 0
            + ("SUM(c.in_book) AS in_book " if include_chats
               else "COUNT(*) AS in_book ")
            + from_sql
            + ((" WHERE " + " AND ".join(where)) if where else "")
        )
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        total = int((row["total"] if row else 0) or 0)
        in_book = int((row["in_book"] if row else 0) or 0)
        return {
            "total": total,
            "never_spoke": int((row["never_spoke"] if row else 0) or 0),
            "silent": int((row["silent"] if row else 0) or 0),
            "with_conversation": int((row["with_conversation"] if row else 0) or 0),
            "in_book": in_book,
            # 派生而非再来一个 SUM：两者互补，减法保证 in_book + chat_only == total
            # 恒成立（前端拿这两个数拼「该补进通讯录」的引导，对不上会露馅）。
            "chat_only": max(0, total - in_book),
        }

    def get_protocol_contact_name(
        self, platform: str, account_id: str, chat_key: str,
    ) -> str:
        """按号码反查通讯录名（name 优先、其次 notify_name）；无则空串。"""
        platform = str(platform or "").lower()
        account_id = str(account_id or "")
        chat_key = str(chat_key or "")
        if not (platform and account_id and chat_key):
            return ""
        with self._lock:
            row = self._conn.execute(
                """SELECT name, notify_name FROM protocol_contacts
                    WHERE platform=? AND account_id=? AND chat_key=?""",
                (platform, account_id, chat_key),
            ).fetchone()
        if not row:
            return ""
        return str(row["name"] or row["notify_name"] or "")

    def upsert_protocol_chats(
        self, platform: str, account_id: str, rows: List[Dict[str, Any]],
    ) -> int:
        """把平台会话列表建成会话占位（无消息也可见，贴近官方全量会话列表）。

        已存在会话仅在更「新」时更新 last_ts/预览（见 upsert_conversation 的 MAX 语义）；
        display_name 优先用平台会话名 → 通讯录名 → 裸号码。返回处理条数。
        """
        platform = str(platform or "").lower()
        account_id = str(account_id or "")
        if not platform or not account_id or not rows:
            return 0
        # P1（Messenger 目录同步）：rows 可以**不带 unread 键**（DOM 侧抓不到可靠
        # 计数，宁缺勿假）。而 upsert_conversation 的 unread 是无条件覆盖语义——
        # 缺键按 0 传会让每轮目录推送把 ingest 累计的真实未读清零（Telegram/WA 的
        # rows 带云端真值，此前从未踩到这条覆盖语义）。缺键时回填库内现值：
        # 一次批量 SELECT，不逐行查询。
        need = [str(r.get("jid") or r.get("chat_key") or "").strip()
                for r in rows
                if isinstance(r, dict) and "unread" not in r]
        keep_unread: Dict[str, int] = {}
        need = [k for k in need if k]
        if need:
            cids = [f"{platform}:{account_id}:{k}" for k in need[:500]]
            marks = ",".join("?" * len(cids))
            with self._lock:
                for row in self._conn.execute(
                    f"SELECT conversation_id, unread FROM conversations "
                    f"WHERE conversation_id IN ({marks})", cids,
                ).fetchall():
                    keep_unread[str(row[0])] = int(row[1] or 0)
        n = 0
        for r in rows:
            if not isinstance(r, dict):
                continue
            ck = str(r.get("jid") or r.get("chat_key") or "").strip()
            if not ck:
                continue
            name = (str(r.get("name") or "").strip()
                    or self.get_protocol_contact_name(platform, account_id, ck)
                    or ck)
            # P2：群会话占位 chat_type=group（分流「群组动态」，不刷 SLA）
            ctype = "group" if bool(r.get("is_group")) else "private"
            cid = f"{platform}:{account_id}:{ck}"
            unread = (int(r.get("unread") or 0) if "unread" in r
                      else keep_unread.get(cid, 0))
            conv = InboxConversation(
                conversation_id=cid,
                platform=platform, account_id=account_id, chat_key=ck,
                display_name=name, last_ts=float(r.get("ts") or 0),
                unread=unread, chat_type=ctype,
                # P1（Messenger 目录同步）：占位会话带头像——messenger-web 侧栏
                # 每轮都抓得到头像直链；upsert 的 CASE 保证空值绝不冲掉已有头像。
                avatar_url=str(r.get("avatar_url") or ""),
            )
            self.upsert_conversation(conv)
            n += 1
        return n

    # ── 读取（unified_inbox 路由调用）──────────────────────────

    def search_messages(
        self, query: str, *, limit: int = 20, platform: str = ""
    ) -> List[Dict[str, Any]]:
        """Phase 22（U1）：跨会话消息全文检索。

        优先路径：FTS5 MATCH（精准分词 + rank 排序）。
        降级路径：SQLite LIKE（兜底，FTS5 不可用时自动切换）。

        每个命中返回：message_id, conversation_id, text, ts, direction,
                      platform, display_name, fts_mode（'fts5'|'like'）。
        """
        query = str(query or "").strip()
        if not query:
            return []
        limit = max(1, min(100, int(limit or 20)))

        # FTS5 优先路径：支持 phrase、prefix、NOT 等高级语法
        if getattr(self, "_fts5_available", False):
            try:
                return self._search_messages_fts5(query, limit=limit, platform=platform)
            except Exception:
                pass  # FTS5 查询失败（特殊字符/语法错），降级 LIKE

        return self._search_messages_like(query, limit=limit, platform=platform)

    def _search_messages_fts5(
        self, query: str, *, limit: int, platform: str
    ) -> List[Dict[str, Any]]:
        """FTS5 全文检索路径（Phase 22）。"""
        # 净化 query：去掉 FTS5 特殊字符，防止语法报错
        safe_q = query.replace('"', '').replace("'", "")
        params: List[Any] = [safe_q]
        extra = ""
        if platform:
            extra = " AND c.platform = ?"
            params.append(str(platform))
        params.append(limit)
        sql = f"""
            SELECT f.message_id, f.conversation_id, f.text, f.ts, f.direction,
                   c.platform, c.display_name, 'fts5' AS fts_mode
            FROM messages_fts f
            LEFT JOIN conversations c ON c.conversation_id = f.conversation_id
            WHERE messages_fts MATCH ?{extra}
            ORDER BY rank
            LIMIT ?
        """
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def _search_messages_like(
        self, query: str, *, limit: int, platform: str
    ) -> List[Dict[str, Any]]:
        """LIKE 降级路径（Phase 21 / 22 兜底）。"""
        like = f"%{query}%"
        params: List[Any] = [like]
        extra = ""
        if platform:
            extra = " AND c.platform = ?"
            params.append(str(platform))
        params.append(limit)
        sql = f"""
            SELECT m.message_id, m.conversation_id, m.text, m.ts, m.direction,
                   c.platform, c.display_name, 'like' AS fts_mode
            FROM messages m
            LEFT JOIN conversations c ON c.conversation_id = m.conversation_id
            WHERE m.text LIKE ?{extra}
            ORDER BY m.ts DESC
            LIMIT ?
        """
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def list_conversations(
        self, *, limit: int = 50, platform: str = "", account_id: str = "",
        before_ts: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """会话列表（last_ts 降序）。``before_ts``：游标分页，只取更旧的会话。

        ``platform`` / ``account_id``：scoped 过滤，非空才生效、可独立组合——
        账号视角按需查库的基础（前端全局列表只保留最近若干条，点开单账号时
        必须能绕过全局截断直接查该账号的全部会话）。索引 idx_conv_platform
        (platform, account_id) 覆盖此 WHERE。
        """
        limit = max(1, min(500, int(limit or 50)))
        sql = "SELECT * FROM conversations"
        wheres: List[str] = []
        params: List[Any] = []
        if platform:
            wheres.append("platform = ?")
            params.append(platform)
        if account_id:
            wheres.append("account_id = ?")
            params.append(account_id)
        if before_ts is not None and float(before_ts) > 0:
            wheres.append("last_ts < ?")
            params.append(float(before_ts))
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY last_ts DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def mark_conversation_request(
        self, conversation_id: str, category: str = "",
    ) -> None:
        """标记会话为「陌生人消息请求」（Messenger worker ingest 携带；幂等）。

        category：general=可自动回 / spam 等其它=只入箱不自动回（口径见
        unified_inbox_account_routes 的 ingest 预置档位注释）。
        """
        if not conversation_id:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE conversations SET is_request=1, request_category=?"
                " WHERE conversation_id=?",
                (str(category or "")[:32], conversation_id))
            self._conn.commit()

    def clear_conversation_request(self, conversation_id: str) -> None:
        """撤「陌生人消息请求」标记（显式接受/拒绝后调用；出站落库另有自动清）。"""
        if not conversation_id:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE conversations SET is_request=0, request_category=''"
                " WHERE conversation_id=? AND is_request=1",
                (conversation_id,))
            self._conn.commit()

    def count_conversations_older_than(
        self, ts: float, *, platform: str = "", account_id: str = "",
    ) -> int:
        """last_ts 早于 ts 的会话数（列表分页 has_more 判定）。

        ``platform`` / ``account_id`` 与 :meth:`list_conversations` 同口径过滤，
        scoped 翻页的 has_more 才能按同范围计数（否则全局计数会误报还有更多）。
        """
        sql = "SELECT COUNT(*) FROM conversations WHERE last_ts < ?"
        params: List[Any] = [float(ts or 0)]
        if platform:
            sql += " AND platform = ?"
            params.append(platform)
        if account_id:
            sql += " AND account_id = ?"
            params.append(account_id)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row[0] if row else 0)

    def count_conversations_active_since(
        self, since_ts: float, *, platform: str = "",
    ) -> Dict[Tuple[str, str], int]:
        """按 (platform, account_id) 统计 ``last_ts >= since_ts`` 的会话数。

        供账号卡「今日 N 会话」一次批量取数（避免 N 次 COUNT）。空结果 → {}。
        """
        since = float(since_ts or 0)
        sql = ("SELECT platform, account_id, COUNT(*) AS n FROM conversations"
               " WHERE last_ts >= ?")
        params: List[Any] = [since]
        if platform:
            sql += " AND platform = ?"
            params.append(platform)
        sql += " GROUP BY platform, account_id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out: Dict[Tuple[str, str], int] = {}
        for r in rows:
            out[(str(r["platform"] or ""), str(r["account_id"] or ""))] = int(r["n"] or 0)
        return out

    def conversations_active_since(
        self, since_ts: float, *, platform: str = "",
    ) -> List[Dict[str, str]]:
        """列出 ``last_ts >= since_ts`` 的会话（最小列：conversation_id/platform/account_id）。

        attn 聚合的取数面：只有近窗有动静的会话才可能「现在需要人工」，
        全库扫会把历史沉睡会话也计成红点（见 read_routes._attn_aggregate_map）。
        """
        since = float(since_ts or 0)
        sql = ("SELECT conversation_id, platform, account_id FROM conversations"
               " WHERE last_ts >= ?")
        params: List[Any] = [since]
        if platform:
            sql += " AND platform = ?"
            params.append(platform)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [{"conversation_id": str(r["conversation_id"]),
                 "platform": str(r["platform"] or ""),
                 "account_id": str(r["account_id"] or "")} for r in rows]

    def sum_effective_unread_by_account(
        self, *, platform: str = "",
    ) -> Dict[Tuple[str, str], int]:
        """按 (platform, account_id) 汇总**有效未读**（与 ``effective_unread`` 同口径）。

        供平台导航/账号 rail 徽标脱离客户端 top-N 窗口：未读会话若沉在全局
        快照之外，客户端求和会漏红点。SQL ``CASE`` 与 Python
        ``effective_unread`` 对齐——``unread>0 AND last_ts > last_read_ts``。
        只返回合计 >0 的桶（空/零未读账号省略，调用方按 0 处理）。
        """
        sql = (
            "SELECT platform, account_id, COALESCE(SUM(CASE "
            "WHEN unread > 0 AND last_ts > COALESCE(last_read_ts, 0) "
            "THEN unread ELSE 0 END), 0) AS n "
            "FROM conversations"
        )
        params: List[Any] = []
        if platform:
            sql += " WHERE platform = ?"
            params.append(platform)
        sql += " GROUP BY platform, account_id HAVING n > 0"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out: Dict[Tuple[str, str], int] = {}
        for r in rows:
            out[(str(r["platform"] or ""), str(r["account_id"] or ""))] = int(r["n"] or 0)
        return out

    def get_conversation(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

    def set_conversation_mentioned(self, conversation_id: str, flag: bool) -> bool:
        """P4-11B：设/清会话的「@我」未读旗标（入站群消息 @本账号→True，打开会话→False）。

        幂等；仅在值实际变化时提交，避免无谓写。返回是否更新到行。
        """
        cid = str(conversation_id or "").strip()
        if not cid:
            return False
        val = 1 if flag else 0
        with self._lock:
            cur = self._conn.execute(
                "UPDATE conversations SET mentioned_unread=?, updated_at=? "
                "WHERE conversation_id=? AND mentioned_unread!=?",
                (val, self._now(), cid, val),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def mark_conversation_read(
        self, conversation_id: str, read_ts: Optional[float] = None,
    ) -> float:
        """P0 未读可信化：把会话已读水位推进到 ``read_ts``（缺省=该会话当前 last_ts）。

        坐席在工作台打开会话即调用，写「读到这个时间戳」的高水位。「有效未读」由读路径
        据此派生（``last_ts > last_read_ts`` 才算真未读），从而不被协议号每轮同步的
        ``unread`` 覆盖回弹（手机端未读数与坐席已读态本是两套语义）。

        水位**单调递增**（取 MAX，防乱序/旧值回退）；同时把 ``mentioned_unread`` 一并清零
        （打开即视为看到 @我，与前端 _clearMention 同口径，避免两处状态漂移）。
        返回写入后的有效水位。会话不存在 → 返回 0.0（不建空行）。
        """
        cid = str(conversation_id or "").strip()
        if not cid:
            return 0.0
        with self._lock:
            row = self._conn.execute(
                "SELECT last_ts, last_read_ts FROM conversations "
                "WHERE conversation_id=?",
                (cid,),
            ).fetchone()
            if row is None:
                return 0.0
            cur_last = float(row["last_ts"] or 0)
            cur_read = float(row["last_read_ts"] or 0)
            target = float(read_ts) if read_ts is not None else cur_last
            new_read = max(cur_read, target, cur_last if read_ts is None else 0.0)
            if new_read <= cur_read and cur_read > 0:
                # 水位未前进：仅确保 @我 旗标清零，不无谓 bump updated_at
                self._conn.execute(
                    "UPDATE conversations SET mentioned_unread=0 "
                    "WHERE conversation_id=? AND mentioned_unread!=0", (cid,))
                self._conn.commit()
                return cur_read
            self._conn.execute(
                "UPDATE conversations SET last_read_ts=?, mentioned_unread=0, "
                "updated_at=? WHERE conversation_id=?",
                (new_read, self._now(), cid),
            )
            self._conn.commit()
        return new_read

    def effective_unread(self, row: Dict[str, Any]) -> int:
        """由会话行派生「有效未读」：已读水位覆盖到末条 → 0，否则用同步来的 unread。

        纯函数（读 row 的 last_ts/last_read_ts/unread），供读路径统一口径。
        """
        try:
            last_ts = float(row.get("last_ts") or 0)
            last_read = float(row.get("last_read_ts") or 0)
            raw = int(row.get("unread") or 0)
        except (TypeError, ValueError):
            return int(row.get("unread") or 0)
        if raw <= 0:
            return 0
        return raw if last_ts > last_read else 0

    def update_conversation_identity(
        self,
        conversation_id: str,
        *,
        display_name: Optional[str] = None,
        username: Optional[str] = None,
        phone: Optional[str] = None,
        avatar_url: Optional[str] = None,
    ) -> bool:
        """回填/更新会话 peer 身份（真实昵称 / username / 电话 / 头像 URL）。

        仅更新显式传入（非 None）的字段；``display_name`` 沿用与 ingest 同口径的
        优先级护栏——真实昵称（非空且不等于裸 chat_key）才落，且不用空/裸号码把已存
        真名冲掉。供两处复用：头像懒加载落地后回写 ``avatar_url``、peer 身份惰性解析
        （把「一排数字 id」的历史会话按需补成真实昵称/username）。返回是否更新到行。
        """
        cid = str(conversation_id or "").strip()
        if not cid:
            return False
        sets: List[str] = []
        params: List[Any] = []
        if display_name is not None and str(display_name).strip():
            dn = str(display_name).strip()
            # 只有真实昵称才覆盖；否则仅在现值为空/裸号码时兜底
            sets.append(
                "display_name = CASE WHEN ? != chat_key THEN ? "
                "WHEN display_name = '' OR display_name = chat_key THEN ? "
                "ELSE display_name END"
            )
            params.extend([dn, dn, dn])
        if username is not None and str(username).strip():
            sets.append("username = ?")
            params.append(str(username).strip())
        if phone is not None and str(phone).strip():
            sets.append("phone = ?")
            params.append(str(phone).strip())
        if avatar_url is not None and str(avatar_url).strip():
            sets.append("avatar_url = ?")
            params.append(str(avatar_url).strip())
        if not sets:
            return False
        sets.append("updated_at = ?")
        params.append(self._now())
        params.append(cid)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE conversations SET {', '.join(sets)} WHERE conversation_id = ?",
                params,
            )
            self._conn.commit()
        return cur.rowcount > 0

    def list_conversation_ids_for_contact(self, contact_id: str) -> List[str]:
        """Q 延伸：按 contact_id 反查已归档的 inbox 会话 id（ingest 回写后命中）。"""
        cid = str(contact_id or "").strip()
        if not cid:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT conversation_id FROM conversations "
                "WHERE contact_id=? ORDER BY last_ts DESC",
                (cid,),
            ).fetchall()
        return [str(r["conversation_id"]) for r in rows if r["conversation_id"]]

    def find_conversation_ids_by_external(
        self, platform: str, account_id: str, external_id: str,
    ) -> List[str]:
        """Q 延伸前向：按 external_id 后缀匹配真实 conv_id（不依赖 writeback）。

        CI 的 external_id 多为裸 peer 名（"Bob"），inbox chat_key 可能带前缀
        （"messenger_rpa:Bob"）。匹配 ``chat_key == external_id`` 或
        ``chat_key endswith ':external_id'``——读真实数据、不猜前缀。
        platform+account_id 走 idx_conv_platform 收窄；按 last_ts 优先。
        """
        plat = str(platform or "").strip()
        acc = str(account_id or "default").strip() or "default"
        ext = str(external_id or "").strip()
        if not plat or not ext:
            return []
        # 转义 LIKE 通配符（peer 名可能含 _ / %），用 ESCAPE 子句
        esc = ext.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            rows = self._conn.execute(
                "SELECT conversation_id FROM conversations "
                "WHERE platform=? AND account_id=? "
                "AND (chat_key=? OR chat_key LIKE ? ESCAPE '\\') "
                "ORDER BY last_ts DESC",
                (plat, acc, ext, f"%:{esc}"),
            ).fetchall()
        return [str(r["conversation_id"]) for r in rows if r["conversation_id"]]

    def find_conversations_by_chat_key(
        self, chat_key: str, *, limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """按裸 chat_key 跨平台/账号反查会话（legacy 人设绑定清理工具用）。

        legacy peer-global 绑定键就是裸 chat_key（如 tg 数字 id / LINE U 号），
        同一 peer 可能出现在多个 (platform, account) 下——升级为会话级覆写前
        必须先盘出全部落点。兼容带前缀的旧绑定键（``line_rpa:U123``）：
        同时匹配 ``chat_key == key`` 与 ``chat_key == key 去掉首段前缀``。
        只取清理面板所需轻量字段，按最近活跃排序。
        """
        ck = str(chat_key or "").strip()
        if not ck:
            return []
        lim = max(1, min(int(limit or 50), 200))
        keys = [ck]
        if ":" in ck:
            _tail = ck.split(":", 1)[1].strip()
            if _tail and _tail != ck:
                keys.append(_tail)
        ph = ",".join("?" for _ in keys)
        with self._lock:
            rows = self._conn.execute(
                "SELECT conversation_id, platform, account_id, chat_key, "
                "display_name, chat_type, last_ts FROM conversations "
                f"WHERE chat_key IN ({ph}) ORDER BY last_ts DESC LIMIT ?",
                (*keys, lim),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_conversations_missing_contact_id(
        self, *, limit: int = 200, platform: str = "",
    ) -> List[Dict[str, Any]]:
        """Q 延伸回填：列出尚未归档 contact_id 的会话（按最近活跃优先）。

        只取回填解析所需字段（platform/account_id/chat_key），轻量。
        """
        lim = max(1, min(int(limit or 200), 2000))
        sql = ("SELECT conversation_id, platform, account_id, chat_key "
               "FROM conversations WHERE (contact_id IS NULL OR contact_id='')")
        params: List[Any] = []
        if platform:
            sql += " AND platform=?"
            params.append(platform)
        sql += " ORDER BY last_ts DESC LIMIT ?"
        params.append(lim)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def set_conversation_contact_id(
        self, conversation_id: str, contact_id: str,
    ) -> bool:
        """Q 延伸回填：把 contact_id 写入 conversations + conversation_meta。

        返回是否更新了 conversations 行（会话不存在 → False）。conv_meta 行不存在时跳过
        （不凭空建——meta 由 ingest 智能分析负责生成）。
        """
        cid = str(conversation_id or "").strip()
        contact = str(contact_id or "").strip()
        if not cid or not contact:
            return False
        now = self._now()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE conversations SET contact_id=?, updated_at=? "
                "WHERE conversation_id=?",
                (contact, now, cid),
            )
            self._conn.execute(
                "UPDATE conversation_meta SET contact_id=?, updated_at=? "
                "WHERE conversation_id=? AND (contact_id IS NULL OR contact_id='')",
                (contact, now, cid),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def set_peer_bot_verdict(
        self,
        conversation_id: str,
        *,
        is_bot: Optional[int] = None,
        score: Optional[float] = None,
        evidence: Optional[str] = None,
    ) -> bool:
        """对方机器人守卫 P1：持久化检测/覆写结果（None=该列不动，部分更新）。

        ``is_bot``：0 未知 / 1 已判定 bot / -1 运营覆写「确认真人」；
        ``score``：启发式疑似评分 0..1（夹紧）；``evidence``：原因摘要（截 200 字）。
        返回是否更新到行（会话不存在 → False）。best-effort 语义由调用方包 try。
        """
        cid = str(conversation_id or "").strip()
        if not cid:
            return False
        sets: List[str] = []
        params: List[Any] = []
        if is_bot is not None:
            try:
                v = int(is_bot)
            except (TypeError, ValueError):
                v = 0
            sets.append("peer_is_bot = ?")
            params.append(v if v in (-1, 0, 1) else 0)
        if score is not None:
            try:
                s = float(score)
            except (TypeError, ValueError):
                s = 0.0
            sets.append("bot_score = ?")
            params.append(min(1.0, max(0.0, s)))
        if evidence is not None:
            sets.append("bot_evidence = ?")
            params.append(str(evidence)[:200])
        if not sets:
            return False
        sets.append("updated_at = ?")
        params.append(self._now())
        params.append(cid)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE conversations SET {', '.join(sets)} WHERE conversation_id = ?",
                params,
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ── 对方机器人守卫 P2：每日预算台账（自动链回复轮次） ────────────────
    # 语义见 _MIGRATIONS 里 peer_reply_ledger 的建表注释。三个方法都是
    # best-effort 调用面（guard 侧包 try），本层只保证原子与跨日清零。

    def bump_auto_reply(self, conversation_id: str, day: str) -> int:
        """自动链回复轮次 +1（day 变更自动清零），返回当日新计数。"""
        cid = str(conversation_id or "").strip()
        d = str(day or "").strip()
        if not cid or not d:
            return 0
        with self._lock:
            self._conn.execute(
                """INSERT INTO peer_reply_ledger
                       (conversation_id, day, auto_replies, updated_at)
                   VALUES (?, ?, 1, ?)
                   ON CONFLICT(conversation_id) DO UPDATE SET
                     auto_replies = CASE
                         WHEN peer_reply_ledger.day = excluded.day
                         THEN peer_reply_ledger.auto_replies + 1 ELSE 1 END,
                     day = excluded.day,
                     updated_at = excluded.updated_at""",
                (cid, d, self._now()),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT auto_replies FROM peer_reply_ledger WHERE conversation_id = ?",
                (cid,),
            ).fetchone()
            return int(row["auto_replies"]) if row else 0

    def get_auto_reply_ledger(self, conversation_id: str) -> Dict[str, Any]:
        """读台账行：{day, auto_replies, relief_day}；无行返回空值行。"""
        cid = str(conversation_id or "").strip()
        empty = {"day": "", "auto_replies": 0, "relief_day": ""}
        if not cid:
            return empty
        with self._lock:
            row = self._conn.execute(
                "SELECT day, auto_replies, relief_day FROM peer_reply_ledger "
                "WHERE conversation_id = ?",
                (cid,),
            ).fetchone()
        if not row:
            return empty
        return {
            "day": str(row["day"] or ""),
            "auto_replies": int(row["auto_replies"] or 0),
            "relief_day": str(row["relief_day"] or ""),
        }

    def set_budget_relief(self, conversation_id: str, day: str) -> bool:
        """坐席救济：标记该会话当日不再受预算限制（不清计数，保留观测口径）。"""
        cid = str(conversation_id or "").strip()
        d = str(day or "").strip()
        if not cid or not d:
            return False
        with self._lock:
            self._conn.execute(
                """INSERT INTO peer_reply_ledger
                       (conversation_id, day, auto_replies, relief_day, updated_at)
                   VALUES (?, ?, 0, ?, ?)
                   ON CONFLICT(conversation_id) DO UPDATE SET
                     relief_day = excluded.relief_day,
                     updated_at = excluded.updated_at""",
                (cid, d, d, self._now()),
            )
            self._conn.commit()
        return True

    def list_messages(self, conversation_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, min(500, int(limit or 50)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY ts ASC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_oldest_message(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """P1：取会话最旧一条**带平台消息 id**的消息（作为拉更早历史的锚点）。

        协议号按需回填(fetchMessageHistory)需要 oldest 消息的 (platform_msg_id/wamid,
        ts, direction)；无带 id 的消息（纯 RPA/占位会话）→ None，调用方据此放弃拉取。
        """
        if not conversation_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT platform_msg_id, ts, direction FROM messages "
                "WHERE conversation_id = ? AND platform_msg_id != '' "
                "ORDER BY ts ASC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_message_direction(
        self, conversation_id: str, platform_msg_id: str,
    ) -> str:
        """按 platform_msg_id 取消息方向（in/out）；未落库 → 空串。"""
        if not conversation_id or not platform_msg_id:
            return ""
        with self._lock:
            row = self._conn.execute(
                "SELECT direction FROM messages "
                "WHERE conversation_id = ? AND platform_msg_id = ? LIMIT 1",
                (conversation_id, str(platform_msg_id)),
            ).fetchone()
        return str(row["direction"] or "").strip().lower() if row else ""

    def set_reaction(
        self, conversation_id: str, platform_msg_id: str, sender: str, emoji: str,
    ) -> bool:
        """P4-3：给某条消息（按 platform_msg_id 定位）记一个表情回应。

        按 ``sender`` 键存于 messages.reactions_json（{sender: emoji}）——同一人再反应即替换、
        空 emoji（WhatsApp 撤销反应）即删除，天然幂等。目标消息未落库（更早历史未同步）→
        返回 False，调用方忽略即可（best-effort，绝不建空消息）。
        """
        if not conversation_id or not platform_msg_id:
            return False
        sender = str(sender or "me")
        with self._lock:
            row = self._conn.execute(
                "SELECT message_id, reactions_json FROM messages "
                "WHERE conversation_id = ? AND platform_msg_id = ? LIMIT 1",
                (conversation_id, str(platform_msg_id)),
            ).fetchone()
            if row is None:
                return False
            try:
                d = json.loads(row["reactions_json"] or "{}")
                if not isinstance(d, dict):
                    d = {}
            except Exception:
                d = {}
            if emoji:
                d[sender] = str(emoji)
            else:
                d.pop(sender, None)
            self._conn.execute(
                "UPDATE messages SET reactions_json = ? WHERE message_id = ?",
                (json.dumps(d, ensure_ascii=False), row["message_id"]),
            )
            self._conn.commit()
        return True

    _STATUS_RANK = {"": 0, "sent": 1, "delivered": 2, "read": 3}

    def set_message_status(
        self, conversation_id: str, platform_msg_id: str, status: str,
    ) -> bool:
        """P4-4：更新出站消息投递状态（按 platform_msg_id 定位）。

        **单调升级**：只在新状态等级更高时写（read>delivered>sent），防回执乱序把
        「已读」降回「送达」。目标消息未落库 → False（best-effort，不建空消息）。
        """
        status = str(status or "").strip().lower()
        rank = self._STATUS_RANK.get(status, 0)
        if not conversation_id or not platform_msg_id or rank <= 0:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT message_id, status FROM messages "
                "WHERE conversation_id = ? AND platform_msg_id = ? LIMIT 1",
                (conversation_id, str(platform_msg_id)),
            ).fetchone()
            if row is None:
                return False
            cur_rank = self._STATUS_RANK.get(str(row["status"] or ""), 0)
            if rank <= cur_rank:
                return True  # 已是同级或更高，幂等成功
            self._conn.execute(
                "UPDATE messages SET status = ? WHERE message_id = ?",
                (status, row["message_id"]),
            )
            self._conn.commit()
        return True

    def mark_message_revoked(
        self, conversation_id: str, platform_msg_id: str,
    ) -> bool:
        """P4-6A：把某条消息标为已撤回（对端「删除给所有人」）。

        按 conversation_id + platform_msg_id 定位；目标未落库 → False（不建空消息）。
        幂等：已撤回再撤回仍返回 True。
        """
        if not conversation_id or not platform_msg_id:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT message_id FROM messages "
                "WHERE conversation_id = ? AND platform_msg_id = ? LIMIT 1",
                (conversation_id, str(platform_msg_id)),
            ).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE messages SET revoked = 1 WHERE message_id = ?",
                (row["message_id"],),
            )
            self._conn.commit()
        return True

    def apply_message_edit(
        self, conversation_id: str, platform_msg_id: str, new_text: str,
    ) -> bool:
        """P4-6A：应用一次消息编辑（正文改写 + 标记 edited=1）。

        按 conversation_id + platform_msg_id 定位；目标未落库 / 新正文为空 → False。
        译文缓存清空（旧译对应旧原文，避免错配；下次可重译）。
        """
        new_text = str(new_text or "")
        if not conversation_id or not platform_msg_id or not new_text:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT message_id FROM messages "
                "WHERE conversation_id = ? AND platform_msg_id = ? LIMIT 1",
                (conversation_id, str(platform_msg_id)),
            ).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE messages SET text = ?, translated_text = '', edited = 1 "
                "WHERE message_id = ?",
                (new_text, row["message_id"]),
            )
            self._conn.commit()
        return True

    def mark_outbound_read_upto(
        self, conversation_id: str, max_platform_msg_id: int,
    ) -> int:
        """P4-4（Telegram）：``UpdateReadHistoryOutbox`` 上报对端已读到 ``max_id`` →
        把该会话所有**出站**消息中 platform_msg_id（数字）≤ max_id 且尚未 read 的
        一次性升级为 read。返回实际更新条数（best-effort，非数字 id 自动跳过）。
        """
        try:
            max_id = int(max_platform_msg_id)
        except (TypeError, ValueError):
            return 0
        if not conversation_id or max_id <= 0:
            return 0
        with self._lock:
            cur = self._conn.execute(
                "UPDATE messages SET status = 'read' "
                "WHERE conversation_id = ? AND direction = 'out' "
                "AND status != 'read' "
                "AND platform_msg_id GLOB '[0-9]*' "
                "AND CAST(platform_msg_id AS INTEGER) <= ?",
                (conversation_id, max_id),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def last_outbound_ts_map(
        self, conversation_ids: List[str],
    ) -> Dict[str, float]:
        """批量取每会话**最后一条出站**消息时刻（目标报表「完成后是否已跟进」
        的推导数据源：last_out_ts > goal.done_at ＝ 已跟进）。

        一次 IN 分组查询，不逐会话打库；空入参/异常返回空 map（调用方按
        「未知」降级，绝不抛）。direction 兼容 'out'/'outbound' 两种历史写法。
        """
        ids = list(dict.fromkeys(
            str(x).strip() for x in (conversation_ids or []) if str(x).strip()))
        if not ids:
            return {}
        try:
            ph = ",".join("?" * len(ids))
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT conversation_id AS cid, MAX(ts) AS ts"
                    f" FROM messages WHERE conversation_id IN ({ph})"
                    f" AND direction IN ('out','outbound')"
                    f" GROUP BY conversation_id",
                    ids,
                ).fetchall()
            return {str(r["cid"]): float(r["ts"] or 0.0) for r in rows}
        except Exception:
            logger.debug("last_outbound_ts_map failed", exc_info=True)
            return {}

    def count_inbound_between(
        self, conversation_id: str, since_ts: float, until_ts: float = 0.0,
    ) -> int:
        """窗口内**入站**消息数（目标失守三分法「对方有没有真回话」的判据；
        P5 2026-08-09）。``until_ts<=0``＝不设上限。单会话索引查询，绝不抛。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return 0
        try:
            sql = ("SELECT COUNT(*) FROM messages WHERE conversation_id = ?"
                   " AND direction IN ('in','inbound') AND ts > ?")
            params: List[Any] = [cid, float(since_ts or 0.0)]
            if until_ts and until_ts > 0:
                sql += " AND ts <= ?"
                params.append(float(until_ts))
            with self._lock:
                r = self._conn.execute(sql, tuple(params)).fetchone()
            return int(r[0]) if r else 0
        except Exception:
            logger.debug("count_inbound_between failed", exc_info=True)
            return 0

    def message_volume_map(
        self, since_ts: float, *, min_total: int = 1, limit: int = 50,
    ) -> Dict[str, Dict[str, int]]:
        """窗口内每会话的双向消息量（AI 对聊提醒的判据数据源；2026-08-09）。

        返回 ``{conversation_id: {"in": n, "out": n}}``，只含总量 ≥ ``min_total``
        的会话、按总量降序截断 ``limit``。一次 GROUP BY 查询不逐会话打库；
        异常返回空 map（提醒是旁路观测，绝不因它抛错）。
        """
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT conversation_id AS cid,"
                    " SUM(CASE WHEN direction IN ('in','inbound')"
                    "     THEN 1 ELSE 0 END) AS n_in,"
                    " SUM(CASE WHEN direction IN ('out','outbound')"
                    "     THEN 1 ELSE 0 END) AS n_out"
                    " FROM messages WHERE ts > ?"
                    " GROUP BY conversation_id"
                    " HAVING (n_in + n_out) >= ?"
                    " ORDER BY (n_in + n_out) DESC LIMIT ?",
                    (float(since_ts or 0.0), int(max(1, min_total)),
                     int(max(1, limit))),
                ).fetchall()
            return {
                str(r["cid"]): {"in": int(r["n_in"] or 0),
                                "out": int(r["n_out"] or 0)}
                for r in rows
            }
        except Exception:
            logger.debug("message_volume_map failed", exc_info=True)
            return {}

    def list_recent_messages(
        self,
        conversation_id: str,
        *,
        limit: int = 50,
        before_ts: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """取会话**最近** limit 条（可用 before_ts 游标向更早翻页），返回 ts 升序。

        与 list_messages（取最旧 limit 条）相反，用于时间线展示与分页加载。
        """
        limit = max(1, min(500, int(limit or 50)))
        with self._lock:
            if before_ts is not None:
                rows = self._conn.execute(
                    "SELECT * FROM messages WHERE conversation_id = ? AND ts < ? "
                    "ORDER BY ts DESC LIMIT ?",
                    (conversation_id, float(before_ts), limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM messages WHERE conversation_id = ? ORDER BY ts DESC LIMIT ?",
                    (conversation_id, limit),
                ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def last_message_dirs(
        self, conversation_ids: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """每个会话最后一条消息的方向与时间（SLA：当前未回复时长用）。

        conversation_ids=None → 全部会话；否则仅限给定集合（会话列表批量）。
        返回 {conversation_id: {"direction": "in"/"out", "ts": float}}。
        """
        where = ""
        params: List[Any] = []
        if conversation_ids is not None:
            ids = list({c for c in conversation_ids if c})
            if not ids:
                return {}
            ph = ",".join("?" * len(ids))
            where = f"WHERE conversation_id IN ({ph})"
            params = ids
        sql = (
            "SELECT m.conversation_id AS cid, m.direction AS direction, m.ts AS ts "
            "FROM messages m JOIN (SELECT conversation_id, MAX(ts) AS mts FROM messages "
            f"{where} GROUP BY conversation_id) x "
            "ON m.conversation_id=x.conversation_id AND m.ts=x.mts"
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return {str(r["cid"]): {"direction": str(r["direction"] or "in"),
                                "ts": float(r["ts"] or 0)} for r in rows}

    def last_inbound_ts_map(
        self, conversation_ids: Optional[List[str]] = None,
    ) -> Dict[str, float]:
        """每个会话最后一条**入站**消息时间戳（主动触达「未回退避」判据）。

        与 ``last_message_dirs`` 互补：那个给末条方向，这个回答「对方最后一次
        开口是什么时候」——上次主动开场之后对方没有任何入站 = 未回，冷却退避。
        从未入站的会话不在返回里（调用方按 0 处理）。
        """
        where = "WHERE direction='in'"
        params: List[Any] = []
        if conversation_ids is not None:
            ids = list({c for c in conversation_ids if c})
            if not ids:
                return {}
            ph = ",".join("?" * len(ids))
            where += f" AND conversation_id IN ({ph})"
            params = ids
        sql = (
            "SELECT conversation_id AS cid, MAX(ts) AS mts FROM messages "
            f"{where} GROUP BY conversation_id"
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return {str(r["cid"]): float(r["mts"] or 0) for r in rows}

    def first_response_rows(
        self, since_ts: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """每会话首响原始数据（首条入站 ts → 首条其后出站 ts）。

        仅返回 t_in >= since_ts 的会话（窗口内首次进线）。t_out 为 None ⇒ 尚未回复。
        首响时长/达标率/趋势的聚合交由调用方（路由）在内存完成，保持本方法纯查询。
        """
        sql = (
            "WITH firstin AS ("
            "  SELECT conversation_id, MIN(ts) AS t_in FROM messages "
            "  WHERE direction='in' GROUP BY conversation_id"
            ") "
            "SELECT f.conversation_id AS cid, f.t_in AS t_in, "
            "  (SELECT MIN(m.ts) FROM messages m "
            "   WHERE m.conversation_id=f.conversation_id AND m.direction='out' "
            "   AND m.ts>=f.t_in) AS t_out "
            "FROM firstin f WHERE f.t_in >= ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, (float(since_ts),)).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            t_out = r["t_out"]
            out.append({
                "cid": str(r["cid"]),
                "t_in": float(r["t_in"] or 0),
                "t_out": float(t_out) if t_out is not None else None,
            })
        return out

    def record_agent_send(
        self, conversation_id: str, agent_id: str, *,
        agent_name: str = "", ts: Optional[float] = None,
    ) -> None:
        """记录一次坐席人工发送（用于历史首响坐席归属）。

        与消息 ingest 解耦：发送瞬间打点，不依赖 RPA 出站消息何时被旁路 ingest。
        """
        cid = str(conversation_id or "").strip()
        aid = str(agent_id or "").strip()
        if not cid:
            return
        t = float(ts) if ts is not None else self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_sends (conversation_id, agent_id, agent_name, ts) "
                "VALUES (?,?,?,?)",
                (cid, aid, str(agent_name or ""), t),
            )
            self._conn.commit()

    def agent_first_responses(
        self, since_ts: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """每会话首响坐席归属：首条入站 → 其后**首次坐席发送**（agent_sends）。

        仅统计 t_in>=since_ts 的会话。resp_ts/agent_id 为 None ⇒ 该会话尚无坐席首响
        （可能 AI 自动回复或未回复）。聚合（按坐席的均值/达标率）交由调用方完成。
        """
        sql = (
            "WITH firstin AS ("
            "  SELECT conversation_id, MIN(ts) AS t_in FROM messages "
            "  WHERE direction='in' GROUP BY conversation_id"
            ") "
            "SELECT f.conversation_id AS cid, f.t_in AS t_in, "
            "  (SELECT s.ts FROM agent_sends s WHERE s.conversation_id=f.conversation_id "
            "   AND s.ts>=f.t_in ORDER BY s.ts ASC LIMIT 1) AS resp_ts, "
            "  (SELECT s.agent_id FROM agent_sends s WHERE s.conversation_id=f.conversation_id "
            "   AND s.ts>=f.t_in ORDER BY s.ts ASC LIMIT 1) AS agent_id, "
            "  (SELECT s.agent_name FROM agent_sends s WHERE s.conversation_id=f.conversation_id "
            "   AND s.ts>=f.t_in ORDER BY s.ts ASC LIMIT 1) AS agent_name "
            "FROM firstin f WHERE f.t_in >= ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, (float(since_ts),)).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            resp = r["resp_ts"]
            out.append({
                "cid": str(r["cid"]),
                "t_in": float(r["t_in"] or 0),
                "resp_ts": float(resp) if resp is not None else None,
                "agent_id": str(r["agent_id"]) if r["agent_id"] is not None else None,
                "agent_name": str(r["agent_name"] or "") if r["agent_name"] is not None else "",
            })
        return out

    def count_agent_sends_by_day(
        self, agent_id: str, since_ts: float = 0.0,
    ) -> Dict[str, int]:
        """某坐席按本地日期的人工发送条数（个人日报：发送量）。"""
        aid = str(agent_id or "").strip()
        if not aid:
            return {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') AS d, "
                "COUNT(*) AS n FROM agent_sends WHERE agent_id=? AND ts>=? "
                "GROUP BY d", (aid, float(since_ts)),
            ).fetchall()
        return {str(r["d"]): int(r["n"]) for r in rows if r["d"]}

    def update_message_translation(
        self,
        message_id: str,
        *,
        translated_text: str,
        target_lang: str = "zh",
        source_lang: str = "",
    ) -> bool:
        """回写入站消息译文（Phase 5-3 自动翻译缓存）。"""
        mid = str(message_id or "").strip()
        if not mid or not str(translated_text or "").strip():
            return False
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE messages SET
                    translated_text = ?,
                    target_lang = ?,
                    source_lang = CASE WHEN ? != '' THEN ? ELSE source_lang END
                WHERE message_id = ?
                """,
                (
                    str(translated_text),
                    str(target_lang or "zh"),
                    str(source_lang or ""),
                    str(source_lang or ""),
                    mid,
                ),
            )
            self._conn.commit()
            return int(cur.rowcount or 0) > 0

    def update_message_text(
        self,
        conversation_id: str,
        *,
        text: str,
        message_id: str = "",
        media_ref: str = "",
        only_if_empty: bool = True,
    ) -> bool:
        """回写消息正文（如语音转录补全），让坐席台/时间线看到真实内容而非占位。

        定位优先 ``message_id``（精确），否则按 ``(conversation_id, media_ref)``
        （唯一定位那条媒体消息）。``only_if_empty=True`` 时仅在原 text 为空/媒体
        占位（``[语音]``/``[媒体]`` 等）时覆盖，绝不踩掉已有真实内容（幂等、防竞态）。
        同步刷新 ``original_text``（原为空时）。命中 FTS 触发器保持全文检索一致。
        返回是否真的更新了行。
        """
        new_text = str(text or "").strip()
        if not new_text:
            return False
        mid = str(message_id or "").strip()
        ref = str(media_ref or "").strip()
        cid = str(conversation_id or "").strip()
        if not mid and not (cid and ref):
            return False
        where = "message_id = ?" if mid else "conversation_id = ? AND media_ref = ?"
        params: List[Any] = [mid] if mid else [cid, ref]
        if only_if_empty:
            # 占位/空白才覆盖：'', 纯空白, 或 [xxx] 单一媒体占位（无其他正文）
            where += (
                " AND (COALESCE(TRIM(text), '') = '' "
                "OR (text LIKE '[%]' AND text NOT LIKE '% %'))"
            )
        with self._lock:
            cur = self._conn.execute(
                f"""
                UPDATE messages SET
                    text = ?,
                    original_text = CASE WHEN COALESCE(TRIM(original_text), '') = ''
                        OR original_text LIKE '[%]' THEN ? ELSE original_text END
                WHERE {where}
                """,
                tuple([new_text, new_text] + params),
            )
            self._conn.commit()
            return int(cur.rowcount or 0) > 0

    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        """按主键取单条消息行（媒体按需拉取等「按行回填」路径的定位读）。"""
        mid = str(message_id or "").strip()
        if not mid:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE message_id = ?", (mid,),
            ).fetchone()
        return dict(row) if row else None

    def update_message_media(
        self,
        conversation_id: str,
        *,
        media_type: str,
        media_ref: str,
        message_id: str = "",
        platform_msg_id: str = "",
        only_if_empty: bool = True,
    ) -> bool:
        """回填消息媒体字段（「按需拉取原件」下载归档完成后），占位卡原地变真图。

        与 ``update_message_text`` 同族的幂等回写：定位优先 ``message_id``（主键精确），
        否则 ``(conversation_id, platform_msg_id)``（服务端补拉后按平台消息 id 定位）。
        ``only_if_empty=True`` 时仅在原 ``media_ref`` 为空时写入——绝不踩掉已有归档
        （与出站镜像/重复点击天然防竞态）。``media_type`` 仅在传非空时覆盖（存量
        「[图片]」纯文字行借此一并升级成结构化媒体行）。返回是否真的更新了行。
        """
        ref = str(media_ref or "").strip()
        if not ref:
            return False
        mid = str(message_id or "").strip()
        pmid = str(platform_msg_id or "").strip()
        cid = str(conversation_id or "").strip()
        if not mid and not (cid and pmid):
            return False
        where = "message_id = ?" if mid else \
            "conversation_id = ? AND platform_msg_id = ?"
        params: List[Any] = [mid] if mid else [cid, pmid]
        if only_if_empty:
            where += " AND COALESCE(TRIM(media_ref), '') = ''"
        mt = str(media_type or "").strip()
        with self._lock:
            cur = self._conn.execute(
                f"""
                UPDATE messages SET
                    media_ref = ?,
                    media_type = CASE WHEN ? != '' THEN ? ELSE media_type END
                WHERE {where}
                """,
                tuple([ref, mt, mt] + params),
            )
            self._conn.commit()
            return int(cur.rowcount or 0) > 0

    @staticmethod
    def _sent_hash(sent_text: str) -> str:
        return hashlib.sha256(str(sent_text or "").encode("utf-8")).hexdigest()[:16]

    def record_outbound_translation(
        self,
        conversation_id: str,
        sent_text: str,
        original_text: str,
        *,
        source_lang: str = "",
        target_lang: str = "",
        provider: str = "",
        error: str = "",
    ) -> bool:
        """P1：记录一条出向译文 → 原文/质量映射（一击直发后供 thread 富集双行）。

        按 (conversation_id, hash(实发译文)) 去重 upsert；译文与原文相同（未真正翻译）
        时不记录，避免无意义副行。best-effort，调用方包 try。
        """
        cid = str(conversation_id or "").strip()
        sent = str(sent_text or "").strip()
        orig = str(original_text or "").strip()
        if not cid or not sent or not orig or sent == orig:
            return False
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO outbound_translations
                    (conversation_id, sent_hash, original_text, source_lang,
                     target_lang, provider, error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id, sent_hash) DO UPDATE SET
                    original_text = excluded.original_text,
                    source_lang   = excluded.source_lang,
                    target_lang   = excluded.target_lang,
                    provider      = excluded.provider,
                    error         = excluded.error,
                    created_at    = excluded.created_at
                """,
                (cid, self._sent_hash(sent), orig, str(source_lang or ""),
                 str(target_lang or ""), str(provider or ""), str(error or ""),
                 self._now()),
            )
            self._conn.commit()
        return True

    def get_outbound_translations(self, conversation_id: str) -> Dict[str, Dict[str, Any]]:
        """返回该会话的出向译文映射 {sent_hash: {original_text, target_lang, provider, error}}。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT sent_hash, original_text, source_lang, target_lang, provider, error"
                " FROM outbound_translations WHERE conversation_id = ?",
                (cid,),
            ).fetchall()
        return {
            str(r["sent_hash"]): {
                "original_text": r["original_text"],
                "source_lang": r["source_lang"],
                "target_lang": r["target_lang"],
                "provider": r["provider"],
                "error": r["error"],
            }
            for r in rows
        }

    def record_outbound_xlate(
        self,
        *,
        requested: bool,
        is_auto: bool = False,
        auto_resolved: Optional[bool] = None,
        translated: bool = False,
        target_lang: str = "",
        degraded: bool = False,
        failed: bool = False,
    ) -> None:
        """P3：把一次出向发送的翻译漏斗结果累计进「按日」表（看板窗口读取，跨重启）。

        口径与内存版 ``OutboundTranslationStats.record_send`` 完全一致：
        failed 与 translated 互斥优先 failed；skipped = 请求了但未译且未失败。
        day 用本地日期，与 dashboard 其它面板的 day 分桶对齐。best-effort，调用方包 try。
        """
        day = time.strftime("%Y-%m-%d", time.localtime(self._now()))
        inc_translated = 1 if (translated and not failed) else 0
        inc_skipped = 1 if (requested and not failed and not inc_translated) else 0
        inc_degraded = 1 if (inc_translated and degraded) else 0
        inc_auto_req = 1 if is_auto else 0
        inc_auto_unres = 1 if (is_auto and auto_resolved is False) else 0
        inc_requested = 1 if requested else 0
        inc_failed = 1 if failed else 0
        lang = (str(target_lang or "").strip() or "unknown") if inc_translated else ""
        with self._lock:
            row = self._conn.execute(
                "SELECT by_lang_json FROM outbound_xlate_daily WHERE day = ?", (day,)
            ).fetchone()
            if row is None:
                by_lang: Dict[str, int] = {}
                if lang:
                    by_lang[lang] = 1
                self._conn.execute(
                    """INSERT INTO outbound_xlate_daily
                         (day, sends, requested, translated, skipped, failed,
                          auto_requested, auto_unresolved, degraded, by_lang_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (day, 1, inc_requested, inc_translated, inc_skipped, inc_failed,
                     inc_auto_req, inc_auto_unres, inc_degraded,
                     json.dumps(by_lang, ensure_ascii=False)),
                )
            else:
                try:
                    by_lang = json.loads(row["by_lang_json"] or "{}")
                except Exception:
                    by_lang = {}
                if lang:
                    by_lang[lang] = int(by_lang.get(lang, 0)) + 1
                self._conn.execute(
                    """UPDATE outbound_xlate_daily SET
                         sends = sends + 1,
                         requested = requested + ?,
                         translated = translated + ?,
                         skipped = skipped + ?,
                         failed = failed + ?,
                         auto_requested = auto_requested + ?,
                         auto_unresolved = auto_unresolved + ?,
                         degraded = degraded + ?,
                         by_lang_json = ?
                       WHERE day = ?""",
                    (inc_requested, inc_translated, inc_skipped, inc_failed,
                     inc_auto_req, inc_auto_unres, inc_degraded,
                     json.dumps(by_lang, ensure_ascii=False), day),
                )
            self._conn.commit()

    def get_outbound_xlate_stats(self, since_ts: float) -> Dict[str, Any]:
        """P3：读取 since_ts 起的出向翻译漏斗按日聚合（看板窗口数据 + 趋势）。

        返回与内存版 ``dump()`` 同形的 totals/coverage/by_target_lang，并附 ``trend``
        （每日 sends/translated/coverage 百分比，供 sparkPct 折线）。
        """
        since_day = time.strftime("%Y-%m-%d", time.localtime(since_ts))
        with self._lock:
            rows = self._conn.execute(
                "SELECT day, sends, requested, translated, skipped, failed,"
                " auto_requested, auto_unresolved, degraded, by_lang_json"
                " FROM outbound_xlate_daily WHERE day >= ? ORDER BY day",
                (since_day,),
            ).fetchall()
        tot = {"sends_total": 0, "requested": 0, "translated": 0, "skipped": 0,
               "failed": 0, "auto_requested": 0, "auto_unresolved": 0, "degraded": 0}
        by_lang: Dict[str, int] = {}
        trend = []
        for r in rows:
            s = int(r["sends"] or 0)
            t = int(r["translated"] or 0)
            tot["sends_total"] += s
            tot["requested"] += int(r["requested"] or 0)
            tot["translated"] += t
            tot["skipped"] += int(r["skipped"] or 0)
            tot["failed"] += int(r["failed"] or 0)
            tot["auto_requested"] += int(r["auto_requested"] or 0)
            tot["auto_unresolved"] += int(r["auto_unresolved"] or 0)
            tot["degraded"] += int(r["degraded"] or 0)
            try:
                bl = json.loads(r["by_lang_json"] or "{}")
            except Exception:
                bl = {}
            for k, v in bl.items():
                by_lang[k] = by_lang.get(k, 0) + int(v)
            trend.append({"day": str(r["day"])[5:], "sends": s, "translated": t,
                          "cov_pct": round(t / s * 100, 1) if s else 0.0})
        sends = tot["sends_total"]
        areq = tot["auto_requested"]
        out = dict(tot)
        out["coverage_rate"] = round(tot["translated"] / sends, 4) if sends else 0
        out["auto_unresolved_rate"] = round(tot["auto_unresolved"] / areq, 4) if areq else 0
        out["by_target_lang"] = dict(sorted(by_lang.items()))
        out["trend"] = trend
        return out

    def record_inbound_xlate(
        self,
        *,
        translated: int = 0,
        failed: int = 0,
        by_lang: Optional[Dict[str, int]] = None,
        noop: int = 0,
        deferred: int = 0,
    ) -> None:
        """P3：累计一次会话打开的入站翻译结果进「按日」表（客户→坐席）。

        translated 为本次**新译出**条数（命中 store 缓存的不计，避免重开重复计数）；
        by_lang 为这些新译出消息的客户来源语言分布。noop=产出==原文打标数、
        deferred=转后台补译条数（2026-07 扩列）。全部计数为 0 时不写。
        best-effort，调用方包 try。
        """
        translated = max(0, int(translated or 0))
        failed = max(0, int(failed or 0))
        noop = max(0, int(noop or 0))
        deferred = max(0, int(deferred or 0))
        if translated <= 0 and failed <= 0 and noop <= 0 and deferred <= 0:
            return
        by_lang = {str(k): int(v) for k, v in (by_lang or {}).items() if int(v) > 0}
        day = time.strftime("%Y-%m-%d", time.localtime(self._now()))
        with self._lock:
            row = self._conn.execute(
                "SELECT by_lang_json FROM inbound_xlate_daily WHERE day = ?", (day,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """INSERT INTO inbound_xlate_daily
                         (day, translated, failed, by_lang_json, noop, deferred)
                       VALUES (?,?,?,?,?,?)""",
                    (day, translated, failed,
                     json.dumps(by_lang, ensure_ascii=False), noop, deferred),
                )
            else:
                try:
                    merged = json.loads(row["by_lang_json"] or "{}")
                except Exception:
                    merged = {}
                for k, v in by_lang.items():
                    merged[k] = int(merged.get(k, 0)) + int(v)
                self._conn.execute(
                    """UPDATE inbound_xlate_daily SET
                         translated = translated + ?,
                         failed = failed + ?,
                         by_lang_json = ?,
                         noop = noop + ?,
                         deferred = deferred + ?
                       WHERE day = ?""",
                    (translated, failed, json.dumps(merged, ensure_ascii=False),
                     noop, deferred, day),
                )
            self._conn.commit()

    def get_inbound_xlate_stats(self, since_ts: float) -> Dict[str, Any]:
        """P3：读取 since_ts 起的入站翻译按日聚合（客户来源语言分布 + 趋势）。"""
        since_day = time.strftime("%Y-%m-%d", time.localtime(since_ts))
        with self._lock:
            rows = self._conn.execute(
                "SELECT day, translated, failed, by_lang_json, noop, deferred"
                " FROM inbound_xlate_daily WHERE day >= ? ORDER BY day",
                (since_day,),
            ).fetchall()
        translated = failed = noop = deferred = 0
        by_lang: Dict[str, int] = {}
        trend = []
        for r in rows:
            t = int(r["translated"] or 0)
            translated += t
            failed += int(r["failed"] or 0)
            noop += int(r["noop"] or 0)
            deferred += int(r["deferred"] or 0)
            try:
                bl = json.loads(r["by_lang_json"] or "{}")
            except Exception:
                bl = {}
            for k, v in bl.items():
                by_lang[k] = by_lang.get(k, 0) + int(v)
            trend.append({
                "day": str(r["day"])[5:],
                "translated": t,
                "failed": int(r["failed"] or 0),
                "deferred": int(r["deferred"] or 0),
            })
        return {
            "translated": translated,
            "failed": failed,
            "noop": noop,
            "deferred": deferred,
            "by_source_lang": dict(sorted(by_lang.items())),
            "trend": trend,
        }

    def record_auto_claim(self, *, matched: bool, lang: str = "") -> None:
        """P3：累计一次自动派单进「按日」表。

        matched 表示该次派单是否按坐席语言命中；lang 为命中时的会话语言（用于分布）。
        best-effort，调用方包 try。
        """
        lang = str(lang or "").strip()
        day = time.strftime("%Y-%m-%d", time.localtime(self._now()))
        inc_matched = 1 if matched else 0
        bl_inc = {lang: 1} if (matched and lang) else {}
        with self._lock:
            row = self._conn.execute(
                "SELECT by_lang_json FROM auto_claim_daily WHERE day = ?", (day,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """INSERT INTO auto_claim_daily
                         (day, claimed, lang_matched, by_lang_json)
                       VALUES (?,?,?,?)""",
                    (day, 1, inc_matched, json.dumps(bl_inc, ensure_ascii=False)),
                )
            else:
                try:
                    merged = json.loads(row["by_lang_json"] or "{}")
                except Exception:
                    merged = {}
                for k, v in bl_inc.items():
                    merged[k] = int(merged.get(k, 0)) + int(v)
                self._conn.execute(
                    """UPDATE auto_claim_daily SET
                         claimed = claimed + 1,
                         lang_matched = lang_matched + ?,
                         by_lang_json = ?
                       WHERE day = ?""",
                    (inc_matched, json.dumps(merged, ensure_ascii=False), day),
                )
            self._conn.commit()

    def get_auto_claim_stats(self, since_ts: float) -> Dict[str, Any]:
        """P3：读取 since_ts 起的自动派单按日聚合（命中语言分布 + 趋势）。"""
        since_day = time.strftime("%Y-%m-%d", time.localtime(since_ts))
        with self._lock:
            rows = self._conn.execute(
                "SELECT day, claimed, lang_matched, by_lang_json"
                " FROM auto_claim_daily WHERE day >= ? ORDER BY day",
                (since_day,),
            ).fetchall()
        claimed = lang_matched = 0
        by_lang: Dict[str, int] = {}
        trend = []
        for r in rows:
            c = int(r["claimed"] or 0)
            claimed += c
            lang_matched += int(r["lang_matched"] or 0)
            try:
                bl = json.loads(r["by_lang_json"] or "{}")
            except Exception:
                bl = {}
            for k, v in bl.items():
                by_lang[k] = by_lang.get(k, 0) + int(v)
            trend.append({"day": str(r["day"])[5:], "claimed": c})
        return {
            "claimed": claimed,
            "lang_matched": lang_matched,
            "by_lang": dict(sorted(by_lang.items())),
            "trend": trend,
        }

    def has_inbound_since(self, conversation_id: str, since_ts: float) -> bool:
        """该会话在 ``since_ts`` 之后是否有过客户**入站**消息（送达安全视图的回复率用）。

        纯只读，走 ``idx_msg_conv_ts``；异常按 False（保守：不误报「有回复」）。
        """
        cid = str(conversation_id or "")
        if not cid:
            return False
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT 1 FROM messages WHERE conversation_id=? AND direction='in' "
                    "AND ts>=? LIMIT 1",
                    (cid, float(since_ts)),
                ).fetchone()
            return row is not None
        except Exception:
            return False

    def count_messages(self, conversation_id: str = "") -> int:
        with self._lock:
            if conversation_id:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()
        return int(row[0]) if row else 0

    # ── automation_mode 持久化（替换进程内 dict）────────────────

    def get_automation_mode_if_set(self, conversation_id: str) -> Optional[str]:
        """仅当坐席/UI 显式写过档位时返回；无记录 → None（调用方回落全局默认）。"""
        if not conversation_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT automation_mode FROM conversation_settings WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if not row:
            return None
        mode = str(row["automation_mode"] or _DEFAULT_AUTOMATION_MODE)
        return mode if mode in AUTOMATION_MODES else _DEFAULT_AUTOMATION_MODE

    def get_automation_mode(self, conversation_id: str) -> str:
        if not conversation_id:
            return _DEFAULT_AUTOMATION_MODE
        explicit = self.get_automation_mode_if_set(conversation_id)
        return explicit if explicit is not None else _DEFAULT_AUTOMATION_MODE

    def set_automation_mode(
        self, conversation_id: str, mode: str, *, source: str = "",
    ) -> None:
        """写档位。``source``＝写入者身份（human/bulk/bootstrap/guard:*/sweep:*），
        空串向后兼容（旧调用方不传即视为未知——比误标来源诚实）。"""
        if not conversation_id:
            return
        mode = mode if mode in AUTOMATION_MODES else _DEFAULT_AUTOMATION_MODE
        src = str(source or "")[:80]
        now = self._now()
        with self._lock:
            # 时间线只记真实跃迁：mode/source 有一个变了才落行（接管重刷时间戳、
            # bootstrap 幂等重写都不产生历史噪音）。SELECT 在同一把锁内，与
            # upsert 原子成对。
            prev = self._conn.execute(
                "SELECT automation_mode, source FROM conversation_settings "
                "WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            prev_mode = str(prev["automation_mode"]) if prev else ""
            prev_src = str(prev["source"] or "") if prev else ""
            self._conn.execute(
                """
                INSERT INTO conversation_settings
                    (conversation_id, automation_mode, updated_at, source)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    automation_mode = excluded.automation_mode,
                    updated_at = excluded.updated_at,
                    source = excluded.source
                """,
                (conversation_id, mode, now, src),
            )
            if prev_mode != mode or prev_src != src:
                try:
                    self._conn.execute(
                        "INSERT INTO automation_mode_log "
                        "(conversation_id, mode, source, prev_mode, ts) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (conversation_id, mode, src, prev_mode, now),
                    )
                except Exception:
                    logger.debug("automation_mode_log 落行失败（忽略）",
                                 exc_info=True)
            self._conn.commit()

    def list_automation_mode_log(
        self, conversation_id: str, *, limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """会话档位变更时间线（新→旧）。无历史 → 空表。"""
        if not conversation_id:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT mode, source, prev_mode, ts FROM automation_mode_log "
                "WHERE conversation_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
                (conversation_id, max(1, min(100, int(limit or 20)))),
            ).fetchall()
        return [
            {
                "mode": str(r["mode"] or ""),
                "source": str(r["source"] or ""),
                "prev_mode": str(r["prev_mode"] or ""),
                "ts": float(r["ts"] or 0.0),
            }
            for r in rows
        ]

    def takeover_episodes(
        self, *, source_prefix: str = "case:", days: float = 30.0,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """人工接管闭环观测（P7，只读）：``source`` 匹配前缀的 manual 切档行，
        配对同会话**其后**首条非 manual 档位行（rearm 回流/人工改回）。

        返回 ``[{conversation_id, start, end}]``（end=0.0＝还没回流）。逐条子查
        走 idx_amlog_conv 索引，limit 封顶（默认 200——接管是人肉动作，量级小）。
        """
        cutoff = self._now() - max(1.0, float(days or 30.0)) * 86400.0
        out: List[Dict[str, Any]] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT conversation_id, ts FROM automation_mode_log "
                "WHERE mode = 'manual' AND source LIKE ? AND ts >= ? "
                "ORDER BY ts DESC LIMIT ?",
                (str(source_prefix or "") + "%", cutoff,
                 max(1, min(1000, int(limit or 200)))),
            ).fetchall()
            for r in rows:
                cid = str(r["conversation_id"] or "")
                t1 = float(r["ts"] or 0.0)
                nxt = self._conn.execute(
                    "SELECT ts FROM automation_mode_log "
                    "WHERE conversation_id = ? AND ts > ? AND mode != 'manual' "
                    "ORDER BY ts ASC LIMIT 1",
                    (cid, t1),
                ).fetchone()
                out.append({
                    "conversation_id": cid,
                    "start": t1,
                    "end": float(nxt["ts"]) if nxt else 0.0,
                })
        return out

    def get_automation_mode_meta(
        self, conversation_id: str,
    ) -> Optional[Dict[str, Any]]:
        """显式档位行全貌 ``{mode, source, updated_at}``；无显式行 → None。

        供「谁在什么时候把档位写成这样」的可解释性消费（GET /automation 的
        mode_source 段、why_no_reply CLI）；热路仍走 get_automation_mode_if_set。
        """
        if not conversation_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT automation_mode, source, updated_at FROM "
                "conversation_settings WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if not row:
            return None
        mode = str(row["automation_mode"] or _DEFAULT_AUTOMATION_MODE)
        return {
            "mode": mode if mode in AUTOMATION_MODES else _DEFAULT_AUTOMATION_MODE,
            "source": str(row["source"] or ""),
            "updated_at": float(row["updated_at"] or 0.0),
        }

    def list_takeover_manual(
        self, *, before_ts: float, limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """接管态会话扫描（takeover_rearm 自动接回专用）。

        只取「mode=manual 且 source 以 takeover 开头 且最后写入早于
        ``before_ts``」的行——坐席下拉显式选的 manual（source=human）、
        守卫降档（guard:*/sweep）都不在此列，自动接回绝不碰它们。
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT conversation_id, automation_mode, source, updated_at
                  FROM conversation_settings
                 WHERE automation_mode = 'manual'
                   AND source LIKE 'takeover%'
                   AND updated_at <= ?
                 ORDER BY updated_at ASC
                 LIMIT ?
                """,
                (float(before_ts), int(limit)),
            ).fetchall()
        return [
            {
                "conversation_id": str(r["conversation_id"]),
                "mode": str(r["automation_mode"] or ""),
                "source": str(r["source"] or ""),
                "updated_at": float(r["updated_at"] or 0.0),
            }
            for r in rows
        ]

    def automation_coverage_rows(self) -> List[Dict[str, Any]]:
        """自动化覆盖率聚合的原料行（ops 卡「多少会话真在全自动跑」）。

        conversations LEFT JOIN conversation_settings：每会话一行，mode 为
        NULL＝坐席/系统从未显式写过档位（运行时回落全局默认）。一次全表查询
        （会话量级 <1e4），聚合在纯函数 automation_coverage 里做——store 只出
        事实不出口径。
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT c.platform, c.account_id, c.chat_type,
                       s.automation_mode AS mode,
                       COALESCE(s.source, '') AS source
                  FROM conversations c
                  LEFT JOIN conversation_settings s
                    ON s.conversation_id = c.conversation_id
                """,
            ).fetchall()
        return [
            {
                "platform": str(r["platform"] or ""),
                "account_id": str(r["account_id"] or "default"),
                "chat_type": str(r["chat_type"] or "private"),
                "mode": (None if r["mode"] is None else str(r["mode"])),
                "source": str(r["source"] or ""),
            }
            for r in rows
        ]

    def cancel_pending_l2_drafts(
        self, conversation_id: str, *, decided_by: str = "mode_downgraded",
    ) -> int:
        """会话降出全自动时立即取消待投递 L2（含 enriching），不必等 autosend tick。

        返回取消条数。不含 L1/人审草稿——那些本就该留给坐席。
        """
        cid = str(conversation_id or "").strip()
        if not cid:
            return 0
        now = self._now()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE reply_drafts
                   SET status='cancelled', decided_by=?, decided_at=?, updated_at=?
                 WHERE conversation_id=?
                   AND autopilot_level='L2'
                   AND status IN ('pending', 'enriching')
                """,
                (str(decided_by or "mode_downgraded"), now, now, cid),
            )
            self._conn.commit()
        return int(cur.rowcount or 0)

    def bulk_set_automation_mode(
        self, from_mode: str, to_mode: str, *, source: str = "bulk",
    ) -> List[str]:
        """把所有 ``from_mode`` 会话改成 ``to_mode``。返回被改动的 conversation_id 列表。"""
        if from_mode not in AUTOMATION_MODES or to_mode not in AUTOMATION_MODES:
            return []
        if from_mode == to_mode:
            return []
        now = self._now()
        with self._lock:
            rows = self._conn.execute(
                "SELECT conversation_id FROM conversation_settings WHERE automation_mode=?",
                (from_mode,),
            ).fetchall()
            cids = [str(r["conversation_id"]) for r in rows]
            if cids:
                self._conn.execute(
                    """
                    UPDATE conversation_settings
                       SET automation_mode=?, updated_at=?, source=?
                     WHERE automation_mode=?
                    """,
                    (to_mode, now, str(source or "bulk")[:80], from_mode),
                )
                # 时间线同步落行（bulk 直写不经 set_automation_mode，单独补记）
                try:
                    self._conn.executemany(
                        "INSERT INTO automation_mode_log "
                        "(conversation_id, mode, source, prev_mode, ts) "
                        "VALUES (?, ?, ?, ?, ?)",
                        [(cid, to_mode, str(source or "bulk")[:80], from_mode,
                          now) for cid in cids],
                    )
                except Exception:
                    logger.debug("bulk 档位时间线落行失败（忽略）", exc_info=True)
                self._conn.commit()
        return cids

    def all_automation_modes(self) -> Dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT conversation_id, automation_mode FROM conversation_settings"
            ).fetchall()
        return {str(r["conversation_id"]): str(r["automation_mode"]) for r in rows}

    # ── 分析落库（Phase C 用，A 先建口）────────────────────────

    def save_analysis(self, analysis: MessageAnalysis) -> str:
        analysis_id = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO message_analysis
                    (analysis_id, message_id, conversation_id, intent, emotion, risk_level,
                     risk_reasons_json, relationship_stage, summary, order_no, confidence,
                     analyzer, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    analysis_id, analysis.message_id, analysis.conversation_id,
                    analysis.intent, analysis.emotion, analysis.risk_level,
                    json.dumps(list(analysis.risk_reasons), ensure_ascii=False),
                    analysis.relationship_stage, analysis.summary, analysis.order_no,
                    float(analysis.confidence or 0), analysis.analyzer, self._now(),
                ),
            )
            self._conn.commit()
        return analysis_id

    def latest_analysis(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM message_analysis WHERE conversation_id = ? ORDER BY ts DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        if not row:
            return None
        out = dict(row)
        try:
            out["risk_reasons"] = json.loads(out.pop("risk_reasons_json", "[]") or "[]")
        except Exception:
            out["risk_reasons"] = []
        return out

    # ── reply_drafts（Phase B：inbox 自发草稿 + 风险 overlay）─────

    def upsert_draft(self, draft: Dict[str, Any]) -> str:
        """写入/更新一条草稿。

        - inbox 自发：传 source_kind='inbox' + 自带 draft_id（或自动生成）。
        - overlay：传 source_kind+source_id（平台来源），靠 uq_drafts_source 幂等，
          用于给平台草稿挂风险/autopilot 元数据。
        """
        source_kind = str(draft.get("source_kind") or "inbox")
        source_id = str(draft.get("source_id") or "")
        now = self._now()
        draft_id = str(draft.get("draft_id") or "")
        if not draft_id:
            draft_id = (
                f"{source_kind}:{source_id}" if source_id else f"inbox:{uuid.uuid4().hex}"
            )
        risk_reasons = draft.get("risk_reasons") or []
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO reply_drafts
                    (draft_id, conversation_id, platform, account_id, chat_key,
                     source_kind, source_id, peer_text, draft_text, final_text,
                     draft_lang, translated_preview, risk_level, risk_reasons_json,
                     autopilot_level, status, decided_by, decided_at, sent_at, error,
                     created_at, updated_at, trace_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_kind, source_id) DO UPDATE SET
                    peer_text = CASE WHEN excluded.peer_text != ''
                        THEN excluded.peer_text ELSE reply_drafts.peer_text END,
                    draft_text = CASE WHEN excluded.draft_text != ''
                        THEN excluded.draft_text ELSE reply_drafts.draft_text END,
                    draft_lang = CASE WHEN excluded.draft_lang != ''
                        THEN excluded.draft_lang ELSE reply_drafts.draft_lang END,
                    risk_level = excluded.risk_level,
                    risk_reasons_json = excluded.risk_reasons_json,
                    autopilot_level = excluded.autopilot_level,
                    translated_preview = CASE WHEN excluded.translated_preview != ''
                        THEN excluded.translated_preview ELSE reply_drafts.translated_preview END,
                    status = excluded.status,
                    final_text = CASE WHEN excluded.final_text != ''
                        THEN excluded.final_text ELSE reply_drafts.final_text END,
                    decided_by = excluded.decided_by,
                    decided_at = excluded.decided_at,
                    sent_at = excluded.sent_at,
                    error = excluded.error,
                    updated_at = excluded.updated_at,
                    trace_id = CASE WHEN reply_drafts.trace_id = '' OR reply_drafts.trace_id IS NULL
                               THEN excluded.trace_id ELSE reply_drafts.trace_id END
                """,
                (
                    draft_id, str(draft.get("conversation_id") or ""),
                    str(draft.get("platform") or ""), str(draft.get("account_id") or "default"),
                    str(draft.get("chat_key") or ""), source_kind, source_id,
                    str(draft.get("peer_text") or ""), str(draft.get("draft_text") or ""),
                    str(draft.get("final_text") or ""), str(draft.get("draft_lang") or ""),
                    str(draft.get("translated_preview") or ""),
                    str(draft.get("risk_level") or "low"),
                    json.dumps(list(risk_reasons), ensure_ascii=False),
                    str(draft.get("autopilot_level") or "L1"),
                    str(draft.get("status") or "pending"),
                    str(draft.get("decided_by") or ""), float(draft.get("decided_at") or 0),
                    float(draft.get("sent_at") or 0), str(draft.get("error") or ""),
                    now, now, str(draft.get("trace_id") or ""),
                ),
            )
            self._conn.commit()
        # C3：L2 落库后锁外通知（避免回调持锁引起死锁）
        if str(draft.get("autopilot_level") or "L1") == "L2":
            for _cb in self._l2_callbacks:
                try:
                    _cb()
                except Exception:
                    pass
        return draft_id

    def update_draft_status(
        self,
        draft_id: str,
        *,
        status: str,
        final_text: str = "",
        decided_by: str = "",
        expected_statuses: Sequence[str] = ("pending", "enriching"),
    ) -> bool:
        """H1/H2：通过 draft_id 直接更新草稿状态（适用于 inbox 源草稿）。

        原子状态闸门（多开/双坐席防重，2026-07-29）：仅当当前状态在
        ``expected_statuses`` 内才更新——已 approved/rejected/cancelled 的终态草稿
        不允许被二次处置（此前无闸门：两个窗口先后点「通过」都返回 True →
        双份审计/事件/CSAT 排期，且与 AutosendWorker 的 resolve 竞态时可双投递）。
        所有既有调用方均为「从活跃态出发」的转换（pending 处置 / enriching 陈旧作废），
        默认白名单覆盖全部合法路径，行为仅在竞态双写时收紧。

        返回 True 表示找到并更新了记录；False = 不存在或已处置（调用方经
        ``get_draft`` 区分两者）。
        """
        now = self._now()
        allowed = tuple(expected_statuses or ())
        placeholders = ",".join("?" for _ in allowed)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE reply_drafts SET status=?, final_text=CASE WHEN ?!='' THEN ? ELSE final_text END, "
                "decided_by=?, decided_at=?, updated_at=? WHERE draft_id=? "
                f"AND status IN ({placeholders})",
                (status, final_text, final_text, decided_by, now, now, draft_id, *allowed),
            )
            self._conn.commit()
        return int(cur.rowcount or 0) > 0

    def finalize_draft_enrichment(
        self,
        draft_id: str,
        *,
        draft_text: str,
        autopilot_level: str = "",
        risk_level: str = "",
        risk_reasons: Optional[List[str]] = None,
        draft_lang: str = "",
        status: str = "pending",
    ) -> bool:
        """Phase 2：人设补全收尾——把停泊态（status='enriching'）草稿翻成可处置态。

        把异步人设产线生成的正文写回 ``draft_text``（``upsert_draft`` 的 ON CONFLICT
        刻意不更新 draft_text，故需本专用方法），并按「生成结果二次风控」可能升级的
        ``autopilot_level``/``risk_level`` 一并落库，最后置 ``status``（默认 pending）。

        仅在草稿仍处于 ``enriching`` 时更新（幂等 + 防与人工处置竞态）。置为 L2 + pending
        后**锁外**补发 L2 回调，唤醒 AutosendWorker 用「人设正文」投递（而非占位模板）。
        返回是否真的更新了记录。

        ★ 防「复读」根因修复：草稿是**每会话单行复用**（draft_id=inbox:平台:账号:会话），
        每来一条新消息就重生本行的 ``draft_text``。但历史上一轮送出时 ``final_text`` 被写死
        且此处从不清它，而 autosend 投递取 ``final_text or draft_text``（final 优先）→ 于是
        每一轮都把上一轮的旧 ``final_text`` 反复送出（表现为「语音一直是同一句」），且绕过防复读
        （比对只看重生的 draft_text）。重生即代表「本轮新内容」，故这里**必须清空 final_text**，
        让 autosend 回落到刚生成的 draft_text（人工 edit_send 走 resolve/update_draft_status，
        不经本方法，语义不受影响）。
        """
        did = str(draft_id or "").strip()
        if not did:
            return False
        now = self._now()
        sets = ["draft_text=?", "final_text=?", "status=?", "updated_at=?"]
        params: List[Any] = [str(draft_text or ""), "", str(status or "pending"), now]
        if autopilot_level:
            sets.append("autopilot_level=?")
            params.append(str(autopilot_level))
        if risk_level:
            sets.append("risk_level=?")
            params.append(str(risk_level))
        if risk_reasons is not None:
            sets.append("risk_reasons_json=?")
            params.append(json.dumps(list(risk_reasons), ensure_ascii=False))
        if draft_lang:
            sets.append("draft_lang=?")
            params.append(str(draft_lang))
        params.append(did)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE reply_drafts SET {', '.join(sets)} "
                "WHERE draft_id=? AND status='enriching'",
                tuple(params),
            )
            self._conn.commit()
        updated = int(cur.rowcount or 0) > 0
        # 翻成 L2 + pending 后补发 L2 回调（锁外），让 autosend 用人设正文投递
        if updated and str(autopilot_level or "") == "L2" and str(status) == "pending":
            for _cb in self._l2_callbacks:
                try:
                    _cb()
                except Exception:
                    pass
        return updated

    def get_draft(self, draft_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reply_drafts WHERE draft_id = ?", (draft_id,)
            ).fetchone()
        return self._row_to_draft(row) if row else None

    def get_overlay(self, source_kind: str, source_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reply_drafts WHERE source_kind = ? AND source_id = ?",
                (source_kind, str(source_id)),
            ).fetchone()
        return self._row_to_draft(row) if row else None

    def list_drafts(
        self,
        *,
        source_kind: str = "",
        status: str = "",
        conversation_id: str = "",
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(500, int(limit or 50)))
        sql = "SELECT * FROM reply_drafts"
        clauses: List[str] = []
        params: List[Any] = []
        if source_kind:
            clauses.append("source_kind = ?")
            params.append(source_kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if conversation_id:
            clauses.append("conversation_id = ?")
            params.append(conversation_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_draft(r) for r in rows]

    @staticmethod
    def _row_to_draft(row) -> Dict[str, Any]:
        out = dict(row)
        try:
            out["risk_reasons"] = json.loads(out.pop("risk_reasons_json", "[]") or "[]")
        except Exception:
            out["risk_reasons"] = []
        return out

    def cleanup_old_drafts(
        self,
        *,
        max_age_days: int = 7,
        statuses: Optional[List[str]] = None,
    ) -> int:
        """H3：删除超龄的已处理草稿，防止 reply_drafts 表无限膨胀。

        仅删除 statuses 中指定状态的草稿（默认 approved/rejected/cancelled），
        绝不删除 pending 草稿（安全不变量）。
        返回实际删除的行数。
        """
        if statuses is None:
            statuses = ["approved", "rejected", "cancelled"]
        # 安全过滤：强制移除 pending，不允许误删待处理草稿
        safe_statuses = [s for s in statuses if s != "pending"]
        if not safe_statuses:
            return 0
        cutoff_ts = self._now() - max(1, int(max_age_days)) * 86400
        placeholders = ",".join("?" for _ in safe_statuses)
        with self._lock:
            cur = self._conn.execute(
                f"DELETE FROM reply_drafts WHERE status IN ({placeholders})"
                f" AND updated_at < ?",
                (*safe_statuses, cutoff_ts),
            )
            self._conn.commit()
        count = int(cur.rowcount or 0)
        if count > 0:
            logger.info("cleanup_old_drafts: 删除 %d 条超龄草稿（age>%dd statuses=%s）",
                        count, max_age_days, safe_statuses)
        return count

    def expire_stale_pending_drafts(
        self,
        *,
        max_age_hours: float = 168.0,
        levels: Optional[List[str]] = None,
        groups_only: bool = False,
        agent_id: str = "system",
        dry_run: bool = False,
    ) -> List[Dict[str, Any]]:
        """治理化开关：把长期无人处置的 pending 草稿判定为「过期作废」。

        与 ``cleanup_old_drafts`` 的关键区别：那个是删除**已处置**的历史行防表膨胀、
        **绝不碰 pending**；本方法专治 pending 积压——一条搁置很久的草稿其上下文早已
        陈旧，此刻再发出去比不发更糟，故转入 ``cancelled`` 终态并写审计（action=
        ``auto_expired``），**不删行**（保留可追溯），从而让 SLA 看板与铃铛不再对它反复告警。

        安全不变量：
          - 仅作用于 ``status='pending'`` 的草稿；
          - 默认关（调用方以 feature flag / age>0 控制），本方法本身不做开关判断；
          - ``dry_run=True`` 只返回将被作废的草稿列表，不写库（供预演/CLI 核对）。

        参数：
          max_age_hours: 超过此时长仍 pending 即作废（默认 168h=7 天）。
          levels: 仅作用于这些 autopilot 等级（如 ['L3','L4']）；None=全部等级。
          groups_only: True=仅作用于群/频道会话（is_group_conversation 判定），防误伤
                       1:1 私聊的合法待审草稿（UI 一键清理默认应开）。
          agent_id: 审计里的处置人（默认 'system'）。
        返回：被作废（或 dry_run 命中）的草稿摘要列表 [{draft_id, autopilot_level,
              conversation_id, age_hours}]。
        """
        cutoff_ts = self._now() - max(0.0, float(max_age_hours)) * 3600.0
        clauses = ["status = 'pending'", "created_at > 0", "created_at < ?"]
        params: List[Any] = [cutoff_ts]
        norm_levels = [str(x) for x in (levels or []) if str(x)]
        if norm_levels:
            clauses.append(
                "autopilot_level IN (%s)" % ",".join("?" for _ in norm_levels)
            )
            params.extend(norm_levels)
        sql = (
            "SELECT draft_id, autopilot_level, risk_level, conversation_id, "
            "platform, chat_key, created_at "
            "FROM reply_drafts WHERE " + " AND ".join(clauses) + " ORDER BY created_at ASC"
        )
        now = self._now()
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        if groups_only:
            from .ingest import is_group_conversation
            rows = [
                r for r in rows
                if is_group_conversation({
                    "platform": r["platform"],
                    "conversation_id": r["conversation_id"],
                    "chat_key": r["chat_key"],
                })
            ]
        victims = [
            {
                "draft_id": str(r["draft_id"]),
                "autopilot_level": str(r["autopilot_level"] or ""),
                "risk_level": str(r["risk_level"] or ""),
                "conversation_id": str(r["conversation_id"] or ""),
                "age_hours": round((now - float(r["created_at"] or now)) / 3600.0, 1),
            }
            for r in rows
        ]
        if dry_run or not victims:
            return victims

        for v in victims:
            try:
                with self._lock:
                    self._conn.execute(
                        "UPDATE reply_drafts SET status='cancelled', decided_by=?, "
                        "decided_at=?, updated_at=? WHERE draft_id=? AND status='pending'",
                        (str(agent_id or "system"), now, now, v["draft_id"]),
                    )
                    self._conn.commit()
                self.record_draft_audit(
                    v["draft_id"],
                    autopilot_level=v["autopilot_level"],
                    action="auto_expired",
                    agent_id=str(agent_id or "system"),
                    risk_level=v["risk_level"],
                    conversation_id=v["conversation_id"],
                    reason=f"pending 搁置 {v['age_hours']:.1f}h > {max_age_hours:.0f}h，自动作废",
                    ts=now,
                )
            except Exception:
                logger.debug("expire_stale_pending_drafts: 单条作废失败（已忽略）", exc_info=True)
        logger.info(
            "expire_stale_pending_drafts: 作废 %d 条积压 pending 草稿（age>%.0fh levels=%s）",
            len(victims), max_age_hours, norm_levels or "all",
        )
        return victims

    def cleanup_outbound_translations(self, *, max_age_days: int = 30) -> int:
        """P3：删除超龄出向译文旁路记录，防 outbound_translations 表无限膨胀。

        出向译文映射仅为「双行展示」服务，超过保留期的历史会话基本不再回看，
        按 created_at 删除即可。返回删除行数。best-effort。
        """
        cutoff_ts = self._now() - max(1, int(max_age_days)) * 86400
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM outbound_translations WHERE created_at < ?",
                (cutoff_ts,),
            )
            self._conn.commit()
        count = int(cur.rowcount or 0)
        if count > 0:
            logger.info("cleanup_outbound_translations: 删除 %d 条超龄出向译文（age>%dd）",
                        count, max_age_days)
        return count

    # ── B2 草稿审计日志 ──────────────────────────────────────────

    def record_draft_audit(
        self,
        draft_id: str,
        *,
        autopilot_level: str = "",
        action: str = "",
        agent_id: str = "",
        reason: str = "",
        risk_level: str = "",
        conversation_id: str = "",
        ts: Optional[float] = None,
    ) -> int:
        """记录一条草稿处置审计事件，返回插入的 id。

        action 枚举：autosend（L2 自动）/ blocked（L4 拦截）/
                     force_override（主管强制放行）/ approved / rejected /
                     edit_send / cancelled。
        """
        now = float(ts if ts is not None else self._now())
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO draft_audit_log "
                "(draft_id, autopilot_level, action, agent_id, reason, "
                " risk_level, conversation_id, ts) VALUES (?,?,?,?,?,?,?,?)",
                (
                    str(draft_id or ""), str(autopilot_level or ""),
                    str(action or ""), str(agent_id or ""),
                    str(reason or ""), str(risk_level or ""),
                    str(conversation_id or ""), now,
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def list_draft_audit(
        self,
        *,
        draft_id: str = "",
        agent_id: str = "",
        conversation_id: str = "",
        since_ts: float = 0.0,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """查审计日志（可按 draft_id / agent_id / conversation_id / 时间过滤）。"""
        clauses: List[str] = ["ts>=?"]
        params: List[Any] = [float(since_ts)]
        if draft_id:
            clauses.append("draft_id=?")
            params.append(str(draft_id))
        if agent_id:
            clauses.append("agent_id=?")
            params.append(str(agent_id))
        if conversation_id:
            clauses.append("conversation_id=?")
            params.append(str(conversation_id))
        sql = (
            "SELECT * FROM draft_audit_log WHERE "
            + " AND ".join(clauses)
            + " ORDER BY ts DESC LIMIT ?"
        )
        params.append(int(max(1, min(1000, limit))))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_conversation_automation_stats(
        self,
        conversation_id: str,
        *,
        since_ts: float = 0.0,
    ) -> Dict[str, Any]:
        """按会话聚合 draft_audit_log（今日自动发 / 拦截等），供全自动安全条展示。"""
        cid = str(conversation_id or "")
        if not cid:
            return {"autosend": 0, "blocked": 0, "approved": 0, "failed": 0, "total": 0}
        with self._lock:
            row = self._conn.execute(
                "SELECT "
                "SUM(CASE WHEN action='autosend' THEN 1 ELSE 0 END) AS autosend, "
                "SUM(CASE WHEN action='blocked' THEN 1 ELSE 0 END) AS blocked, "
                "SUM(CASE WHEN action IN ('approved','edit_send') THEN 1 ELSE 0 END) AS approved, "
                "SUM(CASE WHEN action='autosend_failed' THEN 1 ELSE 0 END) AS failed, "
                "COUNT(*) AS total "
                "FROM draft_audit_log WHERE conversation_id=? AND ts>=?",
                (cid, float(since_ts)),
            ).fetchone()
        if not row:
            return {"autosend": 0, "blocked": 0, "approved": 0, "failed": 0, "total": 0}
        return {
            "autosend": int(row["autosend"] or 0),
            "blocked": int(row["blocked"] or 0),
            "approved": int(row["approved"] or 0),
            "failed": int(row["failed"] or 0),
            "total": int(row["total"] or 0),
        }

    def conversations_blocked_counts(
        self,
        conversation_ids: List[str],
        *,
        since_ts: float = 0.0,
    ) -> Dict[str, int]:
        """批量查一组会话今日「风控拦截(blocked)」次数（单次 IN 查询，供收件箱列表高亮）。

        命中风控转人工的会话 = draft_audit_log.action='blocked'。返回 {cid: count}，
        只含 count>0 的会话；空入参或全 0 返回空 dict。
        """
        cids = [str(c) for c in (conversation_ids or []) if c]
        if not cids:
            return {}
        placeholders = ",".join("?" for _ in cids)
        sql = (
            "SELECT conversation_id, COUNT(*) AS n FROM draft_audit_log "
            f"WHERE action='blocked' AND ts>=? AND conversation_id IN ({placeholders}) "
            "GROUP BY conversation_id"
        )
        params: List[Any] = [float(since_ts), *cids]
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return {str(r["conversation_id"]): int(r["n"] or 0) for r in rows if int(r["n"] or 0) > 0}

    def group_speech_ledger(
        self,
        *,
        since_ts: float = 0.0,
        platform: str = "",
    ) -> Dict[str, List[str]]:
        """``{群 chat_key: [在该群发过言的 account_id, ...]}``——**所有**出向发言。

        群脉的暴露度量要回答「平台能看见哪两个号总在同一批群里说话」。它自己的场次库
        只记编排出来的戏，可日常自动回复、坐席手发、主动触达同样是这些号在这些群里
        开口，平台一视同仁地统计。只读场次库 ⇒ 没演过戏就报「安全」，而号可能早已经
        在几十个群里互相同框——**假安全**，风险卡最不能犯的那种错。这里按出向消息取
        真账，把两条来源合并到同一个口径上。

        只算群会话（``chat_type`` 非 private/空；私聊里两个号同框对平台毫无意义），
        ``since_ts>0`` 限时间窗（共现要随时间淡出，否则跑几个月每一对都饱和）。
        返回形状与 ``GroupShowStore.performance_ledger`` 一致，可直接喂给共现矩阵。
        """
        clauses = ["m.direction='out'", "c.chat_key != ''", "c.account_id != ''",
                   "c.chat_type NOT IN ('private', '')"]
        params: List[Any] = []
        if since_ts and since_ts > 0:
            clauses.append("m.ts>=?")
            params.append(float(since_ts))
        if platform:
            clauses.append("c.platform=?")
            params.append(str(platform))
        sql = (
            "SELECT DISTINCT c.chat_key AS g, c.account_id AS a "
            "FROM messages m JOIN conversations c "
            "  ON c.conversation_id = m.conversation_id "
            "WHERE " + " AND ".join(clauses)
        )
        out: Dict[str, List[str]] = {}
        try:
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
        except Exception:  # noqa: BLE001 —— 读不出台账只该让卡片显示空，不该 500
            logger.debug("[inbox] 群发言台账查询失败", exc_info=True)
            return {}
        for r in rows:
            g, a = str(r["g"] or ""), str(r["a"] or "")
            if g and a:
                out.setdefault(g, []).append(a)
        return out

    def group_last_spoke_at(
        self,
        *,
        since_ts: float = 0.0,
        platform: str = "",
        exclude_group: str = "",
    ) -> Dict[str, float]:
        """``{account_id: 最近一次在**群里**出向发言的时刻}``——时间轴的另一半真账。

        与 :meth:`group_speech_ledger` 同源同过滤，只是聚合成每个号的最新时刻。
        跨群间隔闸门要挡的是「同一个号前后脚在两个群冒头」，而自动回复、坐席手发同样
        会让号在群里冒头——只看编排库会给出「这个号三天没说话」的假读数，闸门当场放行，
        实际上它十秒前刚在隔壁群回过消息。两边取 max 才是这个号真实的最近出场时刻。

        ``exclude_group``（chat_key）把**目标群自己**排除掉。闸门问的是「有没有在
        *别的* 群刚冒过头」，同一个群里连着说两句是正常对话、不是跨群编排痕迹；不排除
        的话，一个刚在本群自动回复过的号会被自己挡住，而这条拦截既没有风险意义、又会
        让运营觉得闸门在乱拦（进而把它关掉）。
        """
        clauses = ["m.direction='out'", "c.chat_key != ''", "c.account_id != ''",
                   "c.chat_type NOT IN ('private', '')"]
        params: List[Any] = []
        if since_ts and since_ts > 0:
            clauses.append("m.ts>=?")
            params.append(float(since_ts))
        if platform:
            clauses.append("c.platform=?")
            params.append(str(platform))
        if exclude_group:
            clauses.append("c.chat_key<>?")
            params.append(str(exclude_group))
        sql = (
            "SELECT c.account_id AS a, MAX(m.ts) AS last_ts "
            "FROM messages m JOIN conversations c "
            "  ON c.conversation_id = m.conversation_id "
            "WHERE " + " AND ".join(clauses) + " GROUP BY c.account_id"
        )
        out: Dict[str, float] = {}
        try:
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
        except Exception:  # noqa: BLE001 —— 读不出台账只该让闸门退回保守值，不该 500
            logger.debug("[inbox] 群最近发言查询失败", exc_info=True)
            return {}
        for r in rows:
            a = str(r["a"] or "")
            try:
                ts = float(r["last_ts"] or 0.0)
            except Exception:  # noqa: BLE001
                continue
            if a and ts > 0:
                out[a] = ts
        return out

    def group_inbound_since(
        self,
        chat_key: str,
        *,
        since_ts: float = 0.0,
        platform: str = "",
        exclude_senders: Optional[Sequence[str]] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """某群自 ``since_ts`` 起的**进向**消息（真人插话），供群脉真发让路/接管。

        返回 ``[{ts, sender_id, sender_name, text}, ...]`` 按时间升序。排除空文、
        排除 ``exclude_senders``（通常是本场演员号——他们的出站镜像偶发会标成 in）。
        读挂 → 空列表（让路失效总好过整场戏崩）。
        """
        key = str(chat_key or "").strip()
        if not key:
            return []
        clauses = [
            "c.chat_key=?",
            "c.chat_type NOT IN ('private', '')",
            "m.direction='in'",
            "m.text != ''",
        ]
        params: List[Any] = [key]
        if since_ts and since_ts > 0:
            # 严格大于：已喂给导演的最后一条不要在下一拍重复 observe
            clauses.append("m.ts>?")
            params.append(float(since_ts))
        if platform:
            clauses.append("c.platform=?")
            params.append(str(platform))
        ban = {str(s) for s in (exclude_senders or ()) if str(s or "")}
        sql = (
            "SELECT m.ts AS ts, m.text AS text, "
            "  COALESCE(NULLIF(m.sender_id, ''), '') AS sender_id, "
            "  COALESCE(NULLIF(m.sender_name, ''), '') AS sender_name "
            "FROM messages m JOIN conversations c "
            "  ON c.conversation_id = m.conversation_id "
            "WHERE " + " AND ".join(clauses) + " "
            "ORDER BY m.ts ASC LIMIT ?"
        )
        params.append(max(1, min(int(limit or 50), 200)))
        try:
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
        except Exception:  # noqa: BLE001
            logger.debug("[inbox] 群进向消息查询失败", exc_info=True)
            return []
        out: List[Dict[str, Any]] = []
        for r in rows:
            sid = str(r["sender_id"] or "")
            if sid and sid in ban:
                continue
            try:
                ts = float(r["ts"] or 0.0)
            except Exception:  # noqa: BLE001
                continue
            text = str(r["text"] or "").strip()
            if not text or ts <= 0:
                continue
            name = str(r["sender_name"] or "") or sid or "human"
            out.append({
                "ts": ts, "sender_id": sid or name,
                "sender_name": name, "text": text,
            })
        return out

    def first_private_inbound_ts(
        self,
        chat_keys: Sequence[str],
        *,
        platform: str = "",
        limit: int = 200,
    ) -> Dict[str, float]:
        """``{chat_key: 该 peer 与我们**首次**私聊进向消息的时刻}``。

        群脉效果归因的转化侧：某人在群里发过言、之后**第一次**私聊我们 ⇒ 这场戏
        带来的转化。取 ``MIN(ts)`` 而不是 ``last_ts`` 是这条读数成立的全部前提——
        用最后活跃时刻会把「本来就在聊的老客户」全部算成新转化，把转化数直接刷爆，
        而那个数字看起来完全正常（老客户当然最近有活跃）。

        只认私聊（``chat_type`` 为 private/空）：群里说过话本身是反响、不是转化，
        混进来会让两级证据糊成一个数。查不到的 key 不出现在结果里（调用方按
        「没私聊过」处理），读挂 → 空表（由调用方标 degraded，别装作零转化）。
        """
        keys = [str(k).strip() for k in (chat_keys or ()) if str(k or "").strip()]
        if not keys:
            return {}
        keys = list(dict.fromkeys(keys))[:max(1, min(int(limit or 200), 500))]
        clauses = [
            "m.direction='in'",
            "c.chat_type IN ('private', '')",
            "c.chat_key IN (" + ",".join("?" for _ in keys) + ")",
        ]
        params: List[Any] = list(keys)
        if platform:
            clauses.append("c.platform=?")
            params.append(str(platform))
        sql = (
            "SELECT c.chat_key AS k, MIN(m.ts) AS t "
            "FROM messages m JOIN conversations c "
            "  ON c.conversation_id = m.conversation_id "
            "WHERE " + " AND ".join(clauses) + " GROUP BY c.chat_key"
        )
        try:
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
        except Exception:  # noqa: BLE001 —— 读不出来只该让转化列显示「未知」，不该 500
            logger.debug("[inbox] 首次私聊时刻查询失败", exc_info=True)
            return {}
        out: Dict[str, float] = {}
        for r in rows:
            k = str(r["k"] or "")
            try:
                t = float(r["t"] or 0.0)
            except (TypeError, ValueError):
                continue
            if k and t > 0:
                out[k] = t
        return out

    # ── Phase A / C1：坐席绩效聚合 ───────────────────────────

    def get_agent_perf(
        self,
        *,
        since_ts: float = 0.0,
        agent_id: str = "",
    ) -> List[Dict[str, Any]]:
        """按 agent_id 聚合 draft_audit_log，返回每坐席的草稿处置绩效。

        返回字段：agent_id, total, approved, rejected, blocked,
                   force_override, autosend, avg_action（秒），
                   last_action_ts
        """
        clauses = ["dal.ts>=?"]
        params: List[Any] = [float(since_ts)]
        if agent_id:
            clauses.append("dal.agent_id=?")
            params.append(str(agent_id))
        sql = f"""
            SELECT
                dal.agent_id,
                COUNT(*)                                       AS total,
                SUM(CASE WHEN dal.action='approved'       THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN dal.action='rejected'       THEN 1 ELSE 0 END) AS rejected,
                SUM(CASE WHEN dal.action='blocked'        THEN 1 ELSE 0 END) AS blocked,
                SUM(CASE WHEN dal.action='force_override' THEN 1 ELSE 0 END) AS force_override,
                SUM(CASE WHEN dal.action='autosend'       THEN 1 ELSE 0 END) AS autosend,
                SUM(CASE WHEN dal.action='edit_send'      THEN 1 ELSE 0 END) AS edit_send,
                MAX(dal.ts)                                    AS last_action_ts
            FROM draft_audit_log dal
            WHERE {' AND '.join(clauses)}
              AND dal.agent_id != ''
            GROUP BY dal.agent_id
            ORDER BY total DESC
        """
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        perf = [dict(r) for r in rows]

        # M1: 为每个坐席聚合 avg_csat（其处置的对话的 CSAT 均值）
        try:
            for p in perf:
                aid = str(p.get("agent_id") or "")
                if not aid:
                    continue
                # 找该坐席处置的所有对话 ID
                conv_ids = self._conn.execute(
                    "SELECT DISTINCT conversation_id FROM draft_audit_log "
                    "WHERE agent_id=? AND ts>=? AND conversation_id!=''",
                    (aid, float(since_ts)),
                ).fetchall()
                cids = [r[0] for r in conv_ids]
                if not cids:
                    p["avg_csat"] = None
                    continue
                # 查有 csat_score > 0 的对话
                placeholders = ",".join("?" * len(cids))
                csat_rows = self._conn.execute(
                    f"SELECT csat_score FROM conversation_meta "
                    f"WHERE conversation_id IN ({placeholders}) AND csat_score >= 0",
                    cids,
                ).fetchall()
                scores = [r[0] for r in csat_rows if r[0] is not None and r[0] >= 0]
                p["avg_csat"] = round(sum(scores) / len(scores), 1) if scores else None
        except Exception:
            pass

        return perf

    def get_agent_perf_timeline(
        self,
        *,
        since_ts: float = 0.0,
        agent_id: str = "",
        bucket_sec: int = 86400,
    ) -> List[Dict[str, Any]]:
        """按时间桶聚合 draft_audit_log，返回趋势数据（默认按天）。

        返回字段：bucket_ts（UTC 秒，每桶起始）, agent_id, total, approved, rejected
        """
        clauses = ["ts>=?"]
        params: List[Any] = [float(since_ts)]
        if agent_id:
            clauses.append("agent_id=?")
            params.append(str(agent_id))
        bkt = max(3600, int(bucket_sec))
        sql = f"""
            SELECT
                (CAST(ts / {bkt} AS INTEGER) * {bkt}) AS bucket_ts,
                agent_id,
                COUNT(*) AS total,
                SUM(CASE WHEN action='approved' THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN action='rejected' THEN 1 ELSE 0 END) AS rejected
            FROM draft_audit_log
            WHERE {' AND '.join(clauses)}
              AND agent_id != ''
            GROUP BY bucket_ts, agent_id
            ORDER BY bucket_ts
        """
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_reliability_timeline(
        self, since_ts: float = 0.0, *, bucket_sec: int = 3600,
    ) -> List[Dict[str, Any]]:
        """D2 运维可靠性：按时间桶聚合 draft_audit_log 全量处置（不过滤 agent）。

        持久来源，重启不丢。返回每桶 total/autosend/blocked/rejected，
        供「处置量 + 拦截/弃用率」趋势曲线。block_rate=blocked/total 是系统拦截高风险的占比。
        """
        bkt = max(300, int(bucket_sec))
        sql = f"""
            SELECT
                (CAST(ts / {bkt} AS INTEGER) * {bkt}) AS bucket_ts,
                COUNT(*) AS total,
                SUM(CASE WHEN action='autosend' THEN 1 ELSE 0 END) AS autosend,
                SUM(CASE WHEN action='blocked'  THEN 1 ELSE 0 END) AS blocked,
                SUM(CASE WHEN action='rejected' THEN 1 ELSE 0 END) AS rejected
            FROM draft_audit_log
            WHERE ts >= ?
            GROUP BY bucket_ts
            ORDER BY bucket_ts
        """
        with self._lock:
            rows = self._conn.execute(sql, [float(since_ts)]).fetchall()
        return [dict(r) for r in rows]

    def get_csat_trend(
        self,
        *,
        since_ts: float = 0.0,
        bucket_sec: int = 86400,
    ) -> List[Dict[str, Any]]:
        """O1：按时间桶聚合 conversation_meta CSAT 均值趋势。

        返回字段：bucket_ts（UTC 秒，每桶起始）, avg_csat, count
        仅统计 csat_score >= 0（已评分）的会话。
        """
        bkt = max(3600, int(bucket_sec))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT
                    (CAST(updated_at / {bkt} AS INTEGER) * {bkt}) AS bucket_ts,
                    AVG(csat_score) AS avg_csat,
                    COUNT(*) AS count
                FROM conversation_meta
                WHERE csat_score >= 0 AND updated_at >= ?
                GROUP BY bucket_ts
                ORDER BY bucket_ts
                """,
                (float(since_ts),),
            ).fetchall()
        return [
            {
                "bucket_ts": r[0],
                "avg_csat": round(float(r[1]), 2) if r[1] is not None else None,
                "count": int(r[2]),
            }
            for r in rows
        ]

    def get_draft_level_trend(
        self,
        *,
        since_ts: float = 0.0,
        bucket_sec: int = 86400,
    ) -> List[Dict[str, Any]]:
        """O1：按时间桶聚合 draft_audit_log，统计 L3/L4 占比趋势。

        返回字段：bucket_ts, total, l3, l4, high_risk_rate（L3+L4 / total）
        """
        bkt = max(3600, int(bucket_sec))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT
                    (CAST(ts / {bkt} AS INTEGER) * {bkt}) AS bucket_ts,
                    COUNT(*) AS total,
                    SUM(CASE WHEN autopilot_level='L3' THEN 1 ELSE 0 END) AS l3,
                    SUM(CASE WHEN autopilot_level='L4' THEN 1 ELSE 0 END) AS l4
                FROM draft_audit_log
                WHERE ts >= ? AND action IN ('approved','rejected','autosend','force_override','blocked')
                GROUP BY bucket_ts
                ORDER BY bucket_ts
                """,
                (float(since_ts),),
            ).fetchall()
        result = []
        for r in rows:
            total = int(r[1] or 0)
            l3 = int(r[2] or 0)
            l4 = int(r[3] or 0)
            result.append({
                "bucket_ts": r[0],
                "total": total,
                "l3": l3,
                "l4": l4,
                "high_risk_rate": round((l3 + l4) / total, 3) if total > 0 else 0.0,
            })
        return result

    def get_automation_roi_stats(
        self, since_ts: float = 0.0, until_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        """P0-3 ROI：按动作聚合 draft_audit_log，拆「AI 自动发 / 人工发 / 拦截」。

        与 ``get_agent_perf`` 不同：**不过滤 agent_id**，故 AutosendWorker 的无主
        autosend 也计入——这正是「AI 替代多少人工」的真实口径。

        ``until_ts`` 给定时只统计 ``[since_ts, until_ts)`` 半开区间（用于环比上一窗口）。

        返回::

            {
              "ai_sent": int,        # autosend（AI 自动发送）
              "human_sent": int,     # approved + edit_send + force_override（坐席处置后发）
              "suppressed": int,     # rejected + blocked（生成但未发）
              "total_sent": int,     # ai_sent + human_sent
              "ai_share": float,     # ai_sent / total_sent（0–1）
              "trend": [{"day": "MM-DD", "ai": int, "human": int}, ...],
            }
        """
        _AI = ("autosend",)
        _HUMAN = ("approved", "edit_send", "force_override")
        _SUPPRESS = ("rejected", "blocked")
        clause = "ts >= ?"
        params: List[Any] = [float(since_ts)]
        if until_ts is not None:
            clause += " AND ts < ?"
            params.append(float(until_ts))
        with self._lock:
            rows = self._conn.execute(
                "SELECT action, COUNT(*) FROM draft_audit_log "
                f"WHERE {clause} GROUP BY action",
                params,
            ).fetchall()
            day_rows = self._conn.execute(
                f"SELECT action, ts FROM draft_audit_log WHERE {clause}",
                params,
            ).fetchall()
        counts: Dict[str, int] = {str(r[0] or ""): int(r[1] or 0) for r in rows}
        ai_sent = sum(counts.get(a, 0) for a in _AI)
        human_sent = sum(counts.get(a, 0) for a in _HUMAN)
        suppressed = sum(counts.get(a, 0) for a in _SUPPRESS)
        total_sent = ai_sent + human_sent
        per_day: Dict[str, Dict[str, int]] = {}
        for action, ts in day_rows:
            act = str(action or "")
            if act in _AI:
                key = "ai"
            elif act in _HUMAN:
                key = "human"
            else:
                continue
            day = time.strftime("%Y-%m-%d", time.localtime(float(ts)))
            bucket = per_day.setdefault(day, {"ai": 0, "human": 0})
            bucket[key] += 1
        trend = [{"day": d[5:], "ai": v["ai"], "human": v["human"]}
                 for d, v in sorted(per_day.items())]
        return {
            "ai_sent": ai_sent,
            "human_sent": human_sent,
            "suppressed": suppressed,
            "total_sent": total_sent,
            "ai_share": round(ai_sent / total_sent, 3) if total_sent else 0.0,
            "trend": trend,
        }

    def ai_safety_summary(
        self,
        since_ts: float = 0.0,
        until_ts: Optional[float] = None,
        *,
        include_trend: bool = False,
    ) -> Dict[str, Any]:
        """AI 安全/质量总览：聚合 ``draft_audit_log`` 处置动作 + 风险分级，回答管理者两问——
        「AI 自动发的靠不靠谱」（采纳/改写/弃用率 + 拦截）+「风险边缘量」（高危事件）。

        ``include_trend=True`` 追加按日 ``trend``（供看板画 sparkline）；环比上一窗口时
        第二次调用用默认 False 免重复日聚合。

        **纯读、零新存储**——复用 L2/审核链早已在写的 draft_audit_log（action 枚举见
        :meth:`record_draft_audit`：autosend / blocked / force_override / approved /
        rejected / edit_send / cancelled）。质量口径：

        - ``adopt_rate``  = approved / reviewed（坐席原样采纳 AI 初稿 = 信任度）
        - ``edit_rate``   = edit_send / reviewed（改写后才发 = AI 初稿差多少）
        - ``reject_rate`` = rejected / reviewed（弃用）
        - ``reviewed``    = approved + edit_send + rejected（人工审过的草稿总数）
        - ``autosend`` / ``blocked`` = AI 自动发 / 风控拦截转人工（安全网命中量）
        - ``high_risk``   = 审计行 risk_level 命中高危分级的事件量（风险边缘规模）

        ``until_ts`` 给定时只统计 ``[since_ts, until_ts)`` 半开区间（用于环比）。
        """
        clause = "ts >= ?"
        params: List[Any] = [float(since_ts)]
        if until_ts is not None:
            clause += " AND ts < ?"
            params.append(float(until_ts))
        with self._lock:
            act_rows = self._conn.execute(
                f"SELECT action, COUNT(*) FROM draft_audit_log WHERE {clause} GROUP BY action",
                params,
            ).fetchall()
            risk_rows = self._conn.execute(
                f"SELECT risk_level, COUNT(*) FROM draft_audit_log WHERE {clause} GROUP BY risk_level",
                params,
            ).fetchall()
        counts: Dict[str, int] = {str(r[0] or ""): int(r[1] or 0) for r in act_rows}
        risk: Dict[str, int] = {str(r[0] or ""): int(r[1] or 0) for r in risk_rows}
        approved = counts.get("approved", 0)
        edited = counts.get("edit_send", 0)
        rejected = counts.get("rejected", 0)
        autosend = counts.get("autosend", 0)
        blocked = counts.get("blocked", 0)
        override = counts.get("force_override", 0)
        reviewed = approved + edited + rejected

        def _hot(level: str) -> bool:
            lv = level.lower()
            return lv not in ("", "none", "low", "normal", "safe", "ok")

        high_risk = sum(n for k, n in risk.items() if _hot(k))

        def _rate(n: int, d: int) -> float:
            return round(n / d, 3) if d else 0.0

        out = {
            "window_since": float(since_ts),
            "autosend": autosend,
            "blocked": blocked,
            "override": override,
            "reviewed": reviewed,
            "approved": approved,
            "edited": edited,
            "rejected": rejected,
            "adopt_rate": _rate(approved, reviewed),
            "edit_rate": _rate(edited, reviewed),
            "reject_rate": _rate(rejected, reviewed),
            "high_risk": high_risk,
            "risk_dist": {k: v for k, v in risk.items() if k},
        }
        if include_trend:
            with self._lock:
                day_rows = self._conn.execute(
                    f"SELECT action, ts FROM draft_audit_log WHERE {clause}", params,
                ).fetchall()
            per_day: Dict[str, Dict[str, int]] = {}
            for action, ts in day_rows:
                act = str(action or "")
                day = time.strftime("%Y-%m-%d", time.localtime(float(ts)))
                b = per_day.setdefault(
                    day, {"approved": 0, "edited": 0, "rejected": 0, "autosend": 0, "blocked": 0},
                )
                if act == "approved":
                    b["approved"] += 1
                elif act == "edit_send":
                    b["edited"] += 1
                elif act == "rejected":
                    b["rejected"] += 1
                elif act == "autosend":
                    b["autosend"] += 1
                elif act == "blocked":
                    b["blocked"] += 1
            trend = []
            for d in sorted(per_day):
                b = per_day[d]
                rev = b["approved"] + b["edited"] + b["rejected"]
                trend.append({
                    "day": d[5:],
                    "autosend": b["autosend"],
                    "blocked": b["blocked"],
                    "reviewed": rev,
                    "adopt_rate": _rate(b["approved"], rev),
                })
            out["trend"] = trend
        return out

    def ai_quality_daily_series(self, days: int = 30) -> List[Dict[str, Any]]:
        """按日聚合 ``draft_audit_log`` 处置动作 + 高危量（F2b 阈值校准回放数据源）。

        返回近 ``days`` 天、每天一条**原始计数**（升序）：
        ``{day, reviewed, approved, edited, rejected, autosend, blocked, high_risk}``。
        口径与 :meth:`ai_safety_summary` 完全一致（reviewed=approved+edit_send+rejected；
        high_risk=risk_level 命中非低危分级），供 ``calibrate_ai_quality`` 滚动窗口重算率、
        复刻 watchdog 评估——用真实历史反推「按某阈值会告警几次/分布如何」，上线前定阈值。
        """
        days = max(1, min(120, int(days or 30)))
        since = time.time() - days * 86400
        with self._lock:
            rows = self._conn.execute(
                "SELECT action, risk_level, ts FROM draft_audit_log WHERE ts >= ?",
                (float(since),),
            ).fetchall()

        def _hot(level: str) -> bool:
            lv = str(level or "").lower()
            return lv not in ("", "none", "low", "normal", "safe", "ok")

        per_day: Dict[str, Dict[str, int]] = {}
        for action, risk_level, ts in rows:
            day = time.strftime("%Y-%m-%d", time.localtime(float(ts)))
            b = per_day.setdefault(day, {
                "approved": 0, "edited": 0, "rejected": 0,
                "autosend": 0, "blocked": 0, "high_risk": 0,
            })
            act = str(action or "")
            if act == "approved":
                b["approved"] += 1
            elif act == "edit_send":
                b["edited"] += 1
            elif act == "rejected":
                b["rejected"] += 1
            elif act == "autosend":
                b["autosend"] += 1
            elif act == "blocked":
                b["blocked"] += 1
            if _hot(risk_level):
                b["high_risk"] += 1
        out: List[Dict[str, Any]] = []
        for d in sorted(per_day):
            b = per_day[d]
            out.append({
                "day": d,
                "reviewed": b["approved"] + b["edited"] + b["rejected"],
                "approved": b["approved"], "edited": b["edited"], "rejected": b["rejected"],
                "autosend": b["autosend"], "blocked": b["blocked"], "high_risk": b["high_risk"],
            })
        return out

    def top_blocked_conversations(
        self, since_ts: float = 0.0, *, limit: int = 8,
    ) -> List[Dict[str, Any]]:
        """近窗口内被风控拦截（action='blocked'）最多的会话 Top-N，供 AI 安全看板下钻。

        回填会话名/平台 + 最近拦截时间与原因（取最近一条 blocked 的 reason，供坐席不进会话
        即知因何被拦）。纯读，复用 draft_audit_log + conversations；按拦截次数降序、同数按最近。
        """
        lim = int(max(1, min(50, limit)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT conversation_id, COUNT(*) AS n, MAX(ts) AS last_ts "
                "FROM draft_audit_log WHERE action='blocked' AND ts>=? AND conversation_id<>'' "
                "GROUP BY conversation_id ORDER BY n DESC, last_ts DESC LIMIT ?",
                (float(since_ts), lim),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            cid = str(r["conversation_id"])
            conv = self.get_conversation(cid) or {}
            with self._lock:
                rr = self._conn.execute(
                    "SELECT reason FROM draft_audit_log WHERE conversation_id=? AND action='blocked' "
                    "ORDER BY ts DESC LIMIT 1",
                    (cid,),
                ).fetchone()
            out.append({
                "conversation_id": cid,
                "count": int(r["n"] or 0),
                "last_ts": float(r["last_ts"] or 0),
                "name": conv.get("display_name") or conv.get("chat_key") or cid,
                "platform": conv.get("platform") or "",
                "reason": (str(rr["reason"]) if rr and rr["reason"] else ""),
            })
        return out

    def record_ui_event(
        self, event: str, *, source: str = "", conversation_id: str = "",
        ts: Optional[float] = None,
    ) -> None:
        """记一条轻量 UI 遥测事件（E3）。append-only，best-effort（空 event 忽略）。

        用于观测看板下钻等「功能有没有人真用」，不落 PII 正文。
        """
        ev = str(event or "").strip()
        if not ev:
            return
        now = float(ts if ts is not None else self._now())
        with self._lock:
            self._conn.execute(
                "INSERT INTO ui_event_log (event, source, conversation_id, ts) VALUES (?,?,?,?)",
                (ev, str(source or ""), str(conversation_id or ""), now),
            )
            self._conn.commit()

    def deep_link_stats(
        self, source: str = "ai_safety", since_ts: float = 0.0,
    ) -> Dict[str, int]:
        """AI 安全看板下钻观测（E3）：窗口内下钻点击数 / 命中去重会话数 /
        「点后确有人工处置」的去重会话数（下钻→处理转化，自证价值）。

        processed 判定：下钻会话在**首次下钻时刻之后**出现过人工处置
        （approved/edit_send/rejected/force_override）——诚实反映「点进去后动手了吗」。
        纯读，复用 ui_event_log + draft_audit_log。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS opens, COUNT(DISTINCT conversation_id) AS convs "
                "FROM ui_event_log WHERE event='deep_link_opened' AND source=? AND ts>=?",
                (str(source or ""), float(since_ts)),
            ).fetchone()
            processed_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM ("
                "  SELECT conversation_id, MIN(ts) AS first_ts FROM ui_event_log "
                "  WHERE event='deep_link_opened' AND source=? AND ts>=? AND conversation_id<>'' "
                "  GROUP BY conversation_id"
                ") q WHERE EXISTS ("
                "  SELECT 1 FROM draft_audit_log d WHERE d.conversation_id=q.conversation_id "
                "    AND d.action IN ('approved','edit_send','rejected','force_override') "
                "    AND d.ts>=q.first_ts"
                ")",
                (str(source or ""), float(since_ts)),
            ).fetchone()
        return {
            "opens": int((row["opens"] if row else 0) or 0),
            "convs": int((row["convs"] if row else 0) or 0),
            "processed": int((processed_row["n"] if processed_row else 0) or 0),
        }

    def get_engagement_stats(
        self,
        since_ts: float = 0.0,
        until_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        """情感陪聊·关系参与度：按 **会话(=关系)** 聚合 ``messages``，衡量「聊得多深、多黏」。

        与客服「解决率」相反——陪聊的目标是 **更长、更黏、用户愿意回来**，故这里看
        参与轮次、互惠比、跨天活跃（黏性），而非「快速结案」。窗口 ``[since_ts, until_ts)``。

        口径：
        - ``active_relationships``：窗口内 **有 ≥1 条用户入站(in)** 的会话数（真正在聊的关系）。
        - ``messages_in/out``：用户/角色消息条数；``total_turns`` 两者之和。
        - ``avg_turns``：人均(每关系)往来轮次——关系深度。
        - ``reciprocity``：out/in 比值，衡量 AI 角色的应答充分度（≈1 健康，过低=冷落）。
        - ``sticky_relationships``：窗口内在 **≥2 个不同自然日** 有入站的会话（会回来的关系）。
        - ``sticky_rate`` = sticky / active：黏性（陪聊的核心健康指标）。
        - ``trend``：逐日 {day, active, in, out}。
        """
        import datetime as _dt
        clause = "ts >= ? AND conversation_id != ''"
        params: List[Any] = [float(since_ts)]
        if until_ts is not None:
            clause += " AND ts < ?"
            params.append(float(until_ts))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT conversation_id, direction, ts FROM messages WHERE {clause}",
                params,
            ).fetchall()
        convs: Dict[str, Dict[str, Any]] = {}
        per_day: Dict[str, Dict[str, Any]] = {}
        messages_in = messages_out = 0
        for cid, direction, ts in rows:
            cid = str(cid or "")
            if not cid:
                continue
            is_in = str(direction or "in").lower() == "in"
            tsf = float(ts or 0.0)
            day = _dt.date.fromtimestamp(tsf).isoformat()
            c = convs.setdefault(cid, {"in": 0, "out": 0, "days": set()})
            if is_in:
                c["in"] += 1
                messages_in += 1
                c["days"].add(day)
            else:
                c["out"] += 1
                messages_out += 1
            b = per_day.setdefault(day, {"active": set(), "in": 0, "out": 0})
            if is_in:
                b["in"] += 1
                b["active"].add(cid)
            else:
                b["out"] += 1
        active = [c for c in convs.values() if c["in"] > 0]
        active_n = len(active)
        sticky_n = sum(1 for c in active if len(c["days"]) >= 2)
        total_turns = messages_in + messages_out
        trend = [
            {"day": d[5:], "active": len(v["active"]), "in": v["in"], "out": v["out"]}
            for d, v in sorted(per_day.items())
        ]
        return {
            "active_relationships": active_n,
            "messages_in": messages_in,
            "messages_out": messages_out,
            "total_turns": total_turns,
            "avg_turns": round(total_turns / active_n, 1) if active_n else 0.0,
            "reciprocity": round(messages_out / messages_in, 2) if messages_in else 0.0,
            "sticky_relationships": sticky_n,
            "sticky_rate": round(sticky_n / active_n, 4) if active_n else 0.0,
            "trend": trend,
        }

    def get_retention_cohorts(
        self,
        since_ts: float = 0.0,
        until_ts: Optional[float] = None,
        *,
        horizons: tuple = (1, 7, 30),
    ) -> Dict[str, Any]:
        """情感陪聊·留存：以「首次入站落在窗口内」的会话为同期群，算 D1/D7/D30 回访率。

        留存 = 用户在首次接触后的第 N 天内 **又回来发消息**——这才是陪聊的"解决率"。
        以入站(in)消息为准（用户主动说话才算"在关系里"）。日偏移按自然日计算。
        """
        import datetime as _dt
        with self._lock:
            rows = self._conn.execute(
                "SELECT conversation_id, ts FROM messages "
                "WHERE direction='in' AND conversation_id != '' ORDER BY ts ASC",
            ).fetchall()
        # 每会话的入站自然日序列 + 全局首次入站 ts
        first_ts: Dict[str, float] = {}
        days_by_conv: Dict[str, set] = {}
        for cid, ts in rows:
            cid = str(cid or "")
            if not cid:
                continue
            tsf = float(ts or 0.0)
            if cid not in first_ts:
                first_ts[cid] = tsf
            days_by_conv.setdefault(cid, set()).add(_dt.date.fromtimestamp(tsf).toordinal())
        hi = sorted(int(h) for h in horizons if int(h) > 0)
        cohort = [
            cid for cid, t0 in first_ts.items()
            if t0 >= float(since_ts) and (until_ts is None or t0 < float(until_ts))
        ]
        retained = {h: 0 for h in hi}
        for cid in cohort:
            first_ord = _dt.date.fromtimestamp(first_ts[cid]).toordinal()
            offsets = {d - first_ord for d in days_by_conv[cid]}
            for h in hi:
                if any(1 <= off <= h for off in offsets):
                    retained[h] += 1
        size = len(cohort)
        return {
            "cohort_size": size,
            "horizons": hi,
            "retained": {f"d{h}": retained[h] for h in hi},
            "retention_rate": {
                f"d{h}": round(retained[h] / size, 4) if size else 0.0 for h in hi
            },
        }

    def get_quality_stats(
        self, since_ts: float = 0.0, until_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        """P3-1 AI 回复质量：按动作 + 风险等级聚合 draft_audit_log（**不过滤 agent**）。

        ``until_ts`` 给定时只统计 ``[since_ts, until_ts)`` 半开区间（用于环比上一窗口）。

        与 ``get_agent_perf`` 不同：含 AutosendWorker 的无主 autosend，故口径是
        「全部 AI 草稿处置」，用于「AI 答得好不好」质量闭环。

        派生率：
        - ``auto_pass_rate``：autosend / 总处置（AI 直接放行占比，越高越省人）
        - ``edit_rate``：edit_send / 人工发送（坐席需改写才发的占比，越高说明 AI 初稿越差）
        - ``reject_rate``：rejected / 总处置（被坐席弃用占比）
        - ``block_rate``：blocked / 总处置（L4 高风险拦截占比）
        - ``high_risk_rate``：(L3+L4) / 有等级的处置

        返回 counts / levels / 各率 + 按日 trend（total/autosend/edit_send/rejected/high_risk）。
        """
        _DISPOSITIONS = ("autosend", "approved", "edit_send",
                         "rejected", "blocked", "force_override")
        _HUMAN_SENT = ("approved", "edit_send", "force_override")
        _IN = ("autosend", "approved", "edit_send",
               "rejected", "blocked", "force_override")
        _ph = ",".join("?" * len(_IN))
        win = "ts >= ?"
        base: list = [float(since_ts)]
        if until_ts is not None:
            win += " AND ts < ?"
            base.append(float(until_ts))
        with self._lock:
            act_rows = self._conn.execute(
                f"SELECT action, COUNT(*) FROM draft_audit_log "
                f"WHERE {win} AND action IN ({_ph}) GROUP BY action",
                base + list(_IN),
            ).fetchall()
            lvl_rows = self._conn.execute(
                f"SELECT autopilot_level, COUNT(*) FROM draft_audit_log "
                f"WHERE {win} AND autopilot_level != '' GROUP BY autopilot_level",
                base,
            ).fetchall()
            day_rows = self._conn.execute(
                f"SELECT action, autopilot_level, ts FROM draft_audit_log "
                f"WHERE {win} AND action IN ({_ph})",
                base + list(_IN),
            ).fetchall()
        counts: Dict[str, int] = {a: 0 for a in _DISPOSITIONS}
        for action, cnt in act_rows:
            counts[str(action)] = int(cnt or 0)
        levels: Dict[str, int] = {str(lv): int(c or 0) for lv, c in lvl_rows}
        total = sum(counts.values())
        human_sent = sum(counts[a] for a in _HUMAN_SENT)
        leveled = sum(levels.values())
        high_risk = levels.get("L3", 0) + levels.get("L4", 0)

        def _rate(num: int, den: int) -> float:
            return round(num / den, 3) if den else 0.0

        per_day: Dict[str, Dict[str, int]] = {}
        for action, level, ts in day_rows:
            day = time.strftime("%Y-%m-%d", time.localtime(float(ts)))
            b = per_day.setdefault(
                day, {"total": 0, "autosend": 0, "edit_send": 0,
                      "rejected": 0, "high_risk": 0})
            b["total"] += 1
            act = str(action or "")
            if act in b:
                b[act] += 1
            if str(level or "") in ("L3", "L4"):
                b["high_risk"] += 1
        trend = [
            {"day": d[5:], **v} for d, v in sorted(per_day.items())
        ]
        return {
            "counts": counts,
            "levels": levels,
            "total": total,
            "sent": counts["autosend"] + human_sent,
            "human_sent": human_sent,
            "auto_pass_rate": _rate(counts["autosend"], total),
            "edit_rate": _rate(counts["edit_send"], human_sent),
            "reject_rate": _rate(counts["rejected"], total),
            "block_rate": _rate(counts["blocked"], total),
            "high_risk_rate": _rate(high_risk, leveled),
            "trend": trend,
        }

    def get_usage_stats(
        self, since_ts: float = 0.0, until_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        """C0-2 用量计量：从既有 messages + draft_audit_log 聚合可计费用量。

        单一数据源、零新表零迁移，口径与 ROI/质量看板一致。``until_ts`` 给定时只统计
        ``[since_ts, until_ts)`` 半开区间（环比）。

        返回::

            {
              "messages_in": int, "messages_out": int, "messages_total": int,
              "ai_calls": int,        # draft_audit_log 处置行数 ≈ AI 生成草稿数
              "ai_sent": int,         # autosend（AI 自动发）
              "active_agents": int,   # 窗口内有处置记录的去重坐席数（计费口径代理）
              "active_agent_ids": [...],
              "trend": [{"day":"MM-DD","messages":int,"ai_calls":int}, ...],
            }
        """
        mwin = "ts >= ?"
        mparams: List[Any] = [float(since_ts)]
        if until_ts is not None:
            mwin += " AND ts < ?"
            mparams.append(float(until_ts))
        with self._lock:
            msg_rows = self._conn.execute(
                f"SELECT direction, COUNT(*) FROM messages WHERE {mwin} "
                "GROUP BY direction",
                mparams,
            ).fetchall()
            msg_day_rows = self._conn.execute(
                f"SELECT ts FROM messages WHERE {mwin}", mparams,
            ).fetchall()
            audit_act_rows = self._conn.execute(
                f"SELECT action, COUNT(*) FROM draft_audit_log WHERE {mwin} "
                "GROUP BY action",
                mparams,
            ).fetchall()
            _excl = ",".join("?" * len(_NON_BILLABLE_AGENT_IDS))
            agent_rows = self._conn.execute(
                f"SELECT DISTINCT agent_id FROM draft_audit_log WHERE {mwin} "
                f"AND agent_id != '' AND agent_id NOT IN ({_excl})",
                mparams + list(_NON_BILLABLE_AGENT_IDS),
            ).fetchall()
            audit_day_rows = self._conn.execute(
                f"SELECT ts FROM draft_audit_log WHERE {mwin}", mparams,
            ).fetchall()
        msg_by_dir: Dict[str, int] = {str(r[0] or "in"): int(r[1] or 0)
                                      for r in msg_rows}
        messages_in = msg_by_dir.get("in", 0)
        messages_out = msg_by_dir.get("out", 0)
        act_counts: Dict[str, int] = {str(r[0] or ""): int(r[1] or 0)
                                      for r in audit_act_rows}
        ai_calls = sum(act_counts.values())
        ai_sent = act_counts.get("autosend", 0)
        agent_ids = sorted(str(r[0]) for r in agent_rows if r[0])

        per_day: Dict[str, Dict[str, int]] = {}
        for (ts,) in msg_day_rows:
            day = time.strftime("%Y-%m-%d", time.localtime(float(ts)))
            per_day.setdefault(day, {"messages": 0, "ai_calls": 0})["messages"] += 1
        for (ts,) in audit_day_rows:
            day = time.strftime("%Y-%m-%d", time.localtime(float(ts)))
            per_day.setdefault(day, {"messages": 0, "ai_calls": 0})["ai_calls"] += 1
        trend = [{"day": d[5:], "messages": v["messages"], "ai_calls": v["ai_calls"]}
                 for d, v in sorted(per_day.items())]
        return {
            "messages_in": messages_in,
            "messages_out": messages_out,
            "messages_total": messages_in + messages_out,
            "ai_calls": ai_calls,
            "ai_sent": ai_sent,
            "active_agents": len(agent_ids),
            "active_agent_ids": agent_ids,
            "trend": trend,
        }

    def ping(self) -> bool:
        """轻量连通性自检：能否对 DB 执行一次 SELECT（健康检查用）。"""
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
            return True
        except Exception:
            logger.debug("InboxStore.ping 失败", exc_info=True)
            return False

    def count_demo(self, prefix: str = "demo:") -> Dict[str, int]:
        """统计 demo 命名空间（conversation_id 以 prefix 开头）的行数。"""
        like = f"{prefix}%"
        with self._lock:
            conv = self._conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE conversation_id LIKE ?",
                (like,)).fetchone()[0]
            msg = self._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id LIKE ?",
                (like,)).fetchone()[0]
            audit = self._conn.execute(
                "SELECT COUNT(*) FROM draft_audit_log WHERE conversation_id LIKE ?",
                (like,)).fetchone()[0]
        return {"conversations": int(conv), "messages": int(msg),
                "draft_audits": int(audit)}

    def purge_demo(self, prefix: str = "demo:") -> Dict[str, int]:
        """删除 demo 命名空间的全部行（会话/消息/草稿审计），返回删除计数。

        仅按 conversation_id 前缀删除，绝不触碰真实数据。messages FTS 触发器随删同步。
        """
        like = f"{prefix}%"
        before = self.count_demo(prefix)
        with self._lock:
            self._conn.execute(
                "DELETE FROM messages WHERE conversation_id LIKE ?", (like,))
            self._conn.execute(
                "DELETE FROM draft_audit_log WHERE conversation_id LIKE ?", (like,))
            self._conn.execute(
                "DELETE FROM conversations WHERE conversation_id LIKE ?", (like,))
            self._conn.commit()
        return before

    def get_kb_improvement_candidates(
        self, since_ts: float = 0.0, *, limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """P3-2 质量→KB 闭环：把「AI 答错被改写/拒绝」的会话挑成 KB 改进候选。

        取 ``draft_audit_log`` 中 action ∈ {edit_send, rejected} 的近期记录，关联：
        - ``question``：处置时刻前最后一条**入站**消息（客户问句 → 候选 trigger）；
        - ``suggested_reply``：edit_send 时取处置时刻后第一条**出站**消息（坐席改写后真正
          发出的好答案 → 候选 example_reply）；rejected 无好答案，留空待人工补。

        无客户问句（纯媒体/取不到）的候选跳过。返回最多 limit 条，最新在前。
        """
        limit = max(1, min(100, int(limit or 20)))
        with self._lock:
            audit_rows = self._conn.execute(
                "SELECT conversation_id, action, ts, reason FROM draft_audit_log "
                "WHERE ts >= ? AND action IN ('edit_send','rejected') "
                "AND conversation_id != '' ORDER BY ts DESC LIMIT ?",
                (float(since_ts), limit * 3),
            ).fetchall()
            out: List[Dict[str, Any]] = []
            seen_q: set = set()
            for ar in audit_rows:
                cid = str(ar["conversation_id"] or "")
                ats = float(ar["ts"] or 0)
                action = str(ar["action"] or "")
                q = self._conn.execute(
                    "SELECT text FROM messages WHERE conversation_id=? "
                    "AND direction='in' AND ts<=? AND text!='' "
                    "ORDER BY ts DESC LIMIT 1",
                    (cid, ats + 1),
                ).fetchone()
                question = str(q["text"]).strip() if q and q["text"] else ""
                if not question:
                    continue
                dedup_key = (cid, question[:80])
                if dedup_key in seen_q:
                    continue
                seen_q.add(dedup_key)
                suggested = ""
                if action == "edit_send":
                    rep = self._conn.execute(
                        "SELECT text FROM messages WHERE conversation_id=? "
                        "AND direction='out' AND ts>=? AND text!='' "
                        "ORDER BY ts ASC LIMIT 1",
                        (cid, ats - 1),
                    ).fetchone()
                    suggested = str(rep["text"]).strip() if rep and rep["text"] else ""
                out.append({
                    "conversation_id": cid,
                    "action": action,
                    "ts": ats,
                    "reason": str(ar["reason"] or ""),
                    "question": question,
                    "suggested_reply": suggested,
                })
                if len(out) >= limit:
                    break
        return out

    # ── Phase 5：坐席 presence + 会话租约 ─────────────────────

    def upsert_agent_presence(
        self,
        agent_id: str,
        *,
        display_name: str = "",
        status: str = "online",
    ) -> Dict[str, Any]:
        aid = str(agent_id or "").strip()
        if not aid:
            raise ValueError("agent_id required")
        st = str(status or "online").strip().lower()
        if st not in {"online", "busy", "offline"}:
            raise ValueError(f"invalid status: {st}")
        now = self._now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO agent_presence(agent_id, display_name, status, last_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    display_name = CASE WHEN excluded.display_name != ''
                        THEN excluded.display_name ELSE agent_presence.display_name END,
                    status = excluded.status,
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at
                """,
                (aid, str(display_name or ""), st, now, now),
            )
            self._conn.commit()
        return self.get_agent_presence(aid) or {}

    def get_agent_presence(self, agent_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agent_presence WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_agent_presence(self, *, active_within_sec: float = 120) -> List[Dict[str, Any]]:
        cutoff = self._now() - max(0.0, float(active_within_sec or 0))
        with self._lock:
            # P3：LEFT JOIN agent_prefs 带出坐席技能语言（供 auto_assign match_language）。
            rows = self._conn.execute(
                "SELECT p.*, COALESCE(pr.languages, '') AS languages"
                " FROM agent_presence p"
                " LEFT JOIN agent_prefs pr ON pr.agent_id = p.agent_id"
                " WHERE p.last_seen_at >= ? ORDER BY p.last_seen_at DESC",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def purge_expired_claims(self) -> int:
        now = self._now()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM conversation_claims WHERE expires_at > 0 AND expires_at < ?",
                (now,),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def get_conversation_claim(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        self.purge_expired_claims()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conversation_claims WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if not row:
            return None
        out = dict(row)
        if float(out.get("expires_at") or 0) < self._now():
            return None
        return out

    def get_agent_prefs(self, agent_id: str) -> Dict[str, Any]:
        """坐席告警偏好（不存在则返回全 0/默认=沿用全局、无免打扰）。"""
        aid = str(agent_id or "").strip()
        row = None
        if aid:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM agent_prefs WHERE agent_id=?", (aid,)
                ).fetchone()
        if row is None:
            return {"agent_id": aid, "warn_sec": 0, "crit_sec": 0,
                    "muted": 0, "dnd_start": -1, "dnd_end": -1, "updated_at": 0,
                    "languages": "", "notif_read_at": 0, "appearance": ""}
        return dict(row)

    def set_notif_read_at(self, agent_id: str, read_at_ms: int) -> int:
        """P8：写通知中心「已读水位线」(毫秒时戳)；只单调前进，返回生效值。"""
        aid = str(agent_id or "").strip()
        if not aid:
            return 0
        ts = max(0, int(read_at_ms or 0))
        now = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_prefs (agent_id, notif_read_at, updated_at) "
                "VALUES (?,?,?) ON CONFLICT(agent_id) DO UPDATE SET "
                "notif_read_at=MAX(agent_prefs.notif_read_at, excluded.notif_read_at), "
                "updated_at=excluded.updated_at",
                (aid, ts, now))
            self._conn.commit()
            row = self._conn.execute(
                "SELECT notif_read_at FROM agent_prefs WHERE agent_id=?", (aid,)
            ).fetchone()
        return int(row[0]) if row else ts

    def set_agent_prefs(
        self, agent_id: str, *, warn_sec: int = 0, crit_sec: int = 0,
        muted: int = 0, dnd_start: int = -1, dnd_end: int = -1,
    ) -> Dict[str, Any]:
        """写坐席告警偏好（整条覆盖）。"""
        aid = str(agent_id or "").strip()
        if not aid:
            raise ValueError("agent_id required")
        now = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_prefs (agent_id, warn_sec, crit_sec, muted, "
                "dnd_start, dnd_end, updated_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(agent_id) DO UPDATE SET warn_sec=excluded.warn_sec, "
                "crit_sec=excluded.crit_sec, muted=excluded.muted, "
                "dnd_start=excluded.dnd_start, dnd_end=excluded.dnd_end, "
                "updated_at=excluded.updated_at",
                (aid, int(warn_sec or 0), int(crit_sec or 0), 1 if muted else 0,
                 int(dnd_start), int(dnd_end), now))
            self._conn.commit()
        return self.get_agent_prefs(aid)

    def set_agent_languages(self, agent_id: str, languages: str) -> Dict[str, Any]:
        """P3：写坐席技能语言（CSV 规范码），只动 languages 列，不影响告警偏好。

        languages 已由调用方规范化（normalize_lang + 去重）。新坐席无 prefs 行时插入，
        告警字段取默认（沿用全局 / 无免打扰）。
        """
        aid = str(agent_id or "").strip()
        if not aid:
            raise ValueError("agent_id required")
        langs = str(languages or "")
        now = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_prefs (agent_id, languages, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(agent_id) DO UPDATE SET languages=excluded.languages, "
                "updated_at=excluded.updated_at",
                (aid, langs, now))
            self._conn.commit()
        return self.get_agent_prefs(aid)

    def set_agent_appearance(self, agent_id: str, appearance_json: str) -> Dict[str, Any]:
        """写坐席外观个性化 JSON（调用方已 sanitize；空串=恢复默认）。

        只动 appearance 列，不影响告警偏好/语言（与 set_agent_languages 同模式）。
        """
        aid = str(agent_id or "").strip()
        if not aid:
            raise ValueError("agent_id required")
        payload = str(appearance_json or "")
        now = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_prefs (agent_id, appearance, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(agent_id) DO UPDATE SET appearance=excluded.appearance, "
                "updated_at=excluded.updated_at",
                (aid, payload, now))
            self._conn.commit()
        return self.get_agent_prefs(aid)

    def record_escalation(
        self, conversation_id: str, *, reason: str = "", agent_id: str = "",
        agent_name: str = "", wait_sec: int = 0, dedup_sec: float = 3600,
        ts: Optional[float] = None,
    ) -> bool:
        """记录一次会话升级（问责审计）。dedup_sec 内同会话已记过则跳过。

        返回 True=本次新记录（边沿），False=去重跳过。
        """
        cid = str(conversation_id or "").strip()
        if not cid:
            return False
        now = float(ts if ts is not None else self._now())
        with self._lock:
            if dedup_sec > 0:
                row = self._conn.execute(
                    "SELECT 1 FROM escalations WHERE conversation_id=? AND ts>=? "
                    "LIMIT 1", (cid, now - float(dedup_sec))).fetchone()
                if row is not None:
                    return False
            self._conn.execute(
                "INSERT INTO escalations (conversation_id, reason, agent_id, "
                "agent_name, wait_sec, ts) VALUES (?,?,?,?,?,?)",
                (cid, str(reason or ""), str(agent_id or ""),
                 str(agent_name or ""), int(wait_sec or 0), now))
            self._conn.commit()
        try:   # P4 埋点：转人工升级（仅新记录边沿；fail-silent）
            from src.utils.telemetry import track
            track("session.handed_off",
                  {"session_id": cid, "reason": str(reason or "")})
        except Exception:
            pass
        return True

    def escalation_takeovers(
        self, since_ts: float = 0.0, limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """升级历史 + 接管时延：每条升级关联其后首个 agent_send（人工接管）。

        taken_ts/taken_by 为 None ⇒ 升级后尚无人工接管。聚合交调用方。
        """
        sql = (
            "SELECT e.id AS id, e.conversation_id AS cid, e.reason AS reason, "
            "  e.agent_id AS agent_id, e.agent_name AS agent_name, "
            "  e.wait_sec AS wait_sec, e.ts AS ts, "
            "  (SELECT MIN(s.ts) FROM agent_sends s "
            "   WHERE s.conversation_id=e.conversation_id AND s.ts>=e.ts) AS taken_ts, "
            "  (SELECT s.agent_id FROM agent_sends s "
            "   WHERE s.conversation_id=e.conversation_id AND s.ts>=e.ts "
            "   ORDER BY s.ts ASC LIMIT 1) AS taken_by "
            "FROM escalations e WHERE e.ts>=? ORDER BY e.ts DESC LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(
                sql, (float(since_ts), int(max(1, min(1000, limit))))).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            taken = r["taken_ts"]
            out.append({
                "id": int(r["id"]),
                "conversation_id": str(r["cid"]),
                "reason": str(r["reason"] or ""),
                "agent_id": str(r["agent_id"] or ""),
                "agent_name": str(r["agent_name"] or ""),
                "wait_sec": int(r["wait_sec"] or 0),
                "ts": float(r["ts"] or 0),
                "taken_ts": float(taken) if taken is not None else None,
                "taken_by": str(r["taken_by"]) if r["taken_by"] is not None else "",
            })
        return out

    def count_escalations_since(self, since_ts: float = 0.0) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM escalations WHERE ts>=?", (float(since_ts),)
            ).fetchone()[0]

    def list_escalations(
        self, since_ts: float = 0.0, limit: int = 200,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM escalations WHERE ts>=? ORDER BY ts DESC LIMIT ?",
                (float(since_ts), int(max(1, min(1000, limit)))),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Phase 6-24：定向升级 / 指定主管指派 ──────────────────────────

    def set_escalation_assigned(self, esc_id: int, assigned_to: str) -> bool:
        """把一条升级记录指派给指定主管（写 assigned_to，幂等）。返回是否有行更新。"""
        aid = str(assigned_to or "").strip()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE escalations SET assigned_to=? WHERE id=?",
                (aid, int(esc_id)),
            )
            self._conn.commit()
        return cur.rowcount > 0

    # ── E2：运维事件（health_alert 闭环）────────────────────────────────────

    def open_or_update_incident(
        self,
        *,
        signature: str,
        light: str,
        kind: str = "health",
        summary: Optional[Dict[str, Any]] = None,
        problems: Optional[List[Dict[str, Any]]] = None,
        ts: Optional[float] = None,
    ) -> int:
        """按 (kind, signature) 去重地开/更新一条未关闭的运维事件。

        同类型同 signature 已有未 resolved 事件 → 更新 light/summary/problems/updated_ts；
        否则新建 open 事件。返回事件 id。kind ∈ health|billing。
        """
        now = float(ts if ts is not None else time.time())
        sig = str(signature or "")
        knd = str(kind or "health")
        summary_json = json.dumps(summary or {}, ensure_ascii=False)
        problems_json = json.dumps(problems or [], ensure_ascii=False)
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM ops_incidents "
                "WHERE kind=? AND signature=? AND status!='resolved' "
                "ORDER BY id DESC LIMIT 1",
                (knd, sig),
            ).fetchone()
            if row:
                iid = int(row["id"])
                self._conn.execute(
                    "UPDATE ops_incidents SET light=?, summary_json=?, "
                    "problems_json=?, updated_ts=? WHERE id=?",
                    (str(light or ""), summary_json, problems_json, now, iid),
                )
                self._conn.commit()
                return iid
            cur = self._conn.execute(
                "INSERT INTO ops_incidents "
                "(kind, signature, light, summary_json, problems_json, status, "
                " opened_ts, updated_ts) "
                "VALUES (?,?,?,?,?,'open',?,?)",
                (knd, sig, str(light or ""), summary_json, problems_json, now, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def resolve_open_incidents(
        self, *, kind: str = "", ts: Optional[float] = None,
    ) -> int:
        """把未 resolved 的事件标记为 resolved；kind 非空时仅限该类型。返回受影响条数。"""
        now = float(ts if ts is not None else time.time())
        sql = "UPDATE ops_incidents SET status='resolved', resolved_ts=?, updated_ts=? WHERE status!='resolved'"
        params: List[Any] = [now, now]
        if kind:
            sql += " AND kind=?"
            params.append(str(kind))
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
        return cur.rowcount

    def ack_incident(
        self, incident_id: int, *, assigned_to: str = "", ts: Optional[float] = None,
    ) -> bool:
        """主管确认/认领一条事件（status→acked，记 acked_ts 与 assigned_to）。"""
        now = float(ts if ts is not None else time.time())
        with self._lock:
            cur = self._conn.execute(
                "UPDATE ops_incidents SET status='acked', acked_ts=?, "
                "assigned_to=?, updated_ts=? WHERE id=? AND status!='resolved'",
                (now, str(assigned_to or ""), now, int(incident_id)),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def list_incidents(
        self, *, status: str = "", kind: str = "", limit: int = 50,
        before_id: int = 0,
    ) -> List[Dict[str, Any]]:
        """列出运维事件（默认全部，可按 status / kind 过滤），新→旧。

        before_id>0 时只返回 id 小于它的（游标分页，回看历史）。
        """
        sql = "SELECT * FROM ops_incidents"
        clauses: List[str] = []
        params: List[Any] = []
        if status:
            clauses.append("status=?")
            params.append(str(status))
        if kind:
            clauses.append("kind=?")
            params.append(str(kind))
        if before_id and int(before_id) > 0:
            clauses.append("id<?")
            params.append(int(before_id))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        out: List[Dict[str, Any]] = []
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        for r in rows:
            d = dict(r)
            try:
                d["summary"] = json.loads(d.pop("summary_json", "{}") or "{}")
            except Exception:
                d["summary"] = {}
            try:
                d["problems"] = json.loads(d.pop("problems_json", "[]") or "[]")
            except Exception:
                d["problems"] = []
            out.append(d)
        return out

    def count_open_incidents(self) -> int:
        """未关闭（open + acked）的事件数。"""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM ops_incidents WHERE status!='resolved'"
            ).fetchone()[0]

    def get_incident_stats(self, since_ts: float = 0.0,
                           until_ts: Optional[float] = None) -> Dict[str, Any]:
        """统计 [since_ts, until_ts) 开启的运维事件：总数/各状态/各类型/平均解决时长（秒）。

        until_ts=None 表示「到现在」（无上界），用于环比上一窗口时显式给 until。
        """
        with self._lock:
            if until_ts is None:
                rows = self._conn.execute(
                    "SELECT kind, status, opened_ts, resolved_ts FROM ops_incidents "
                    "WHERE opened_ts>=?",
                    (float(since_ts),),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT kind, status, opened_ts, resolved_ts FROM ops_incidents "
                    "WHERE opened_ts>=? AND opened_ts<?",
                    (float(since_ts), float(until_ts)),
                ).fetchall()
        total = len(rows)
        by_status: Dict[str, int] = {}
        by_kind: Dict[str, int] = {}
        durations: List[float] = []
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
            by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
            if r["status"] == "resolved" and r["resolved_ts"] and r["opened_ts"]:
                d = float(r["resolved_ts"]) - float(r["opened_ts"])
                if d >= 0:
                    durations.append(d)
        return {
            "total": total,
            "open": by_status.get("open", 0) + by_status.get("acked", 0),
            "resolved": by_status.get("resolved", 0),
            "by_status": by_status,
            "by_kind": by_kind,
            "mttr_sec": round(sum(durations) / len(durations), 1) if durations else None,
        }

    def purge_resolved_incidents(self, older_than_ts: float) -> int:
        """删除 resolved_ts 早于阈值的已关闭事件（保留期清理）。返回删除条数。

        只删 status='resolved' 且 resolved_ts>0 的；未关闭事件永不删。
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM ops_incidents "
                "WHERE status='resolved' AND resolved_ts>0 AND resolved_ts<?",
                (float(older_than_ts),),
            )
            self._conn.commit()
        return cur.rowcount

    def count_assigned_escalations(
        self, agent_id: str, since_ts: float = 0.0,
    ) -> int:
        """某主管在 since_ts 之后被指派的升级条数（用于负载均衡选最轻的主管）。"""
        aid = str(agent_id or "").strip()
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM escalations "
                "WHERE assigned_to=? AND ts>=?",
                (aid, float(since_ts)),
            ).fetchone()[0]

    def list_my_escalations(
        self,
        agent_id: str,
        since_ts: float = 0.0,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """返回指派给 agent_id 的升级列表（主管个人视图，含接管时延）。"""
        aid = str(agent_id or "").strip()
        sql = (
            "SELECT e.id AS id, e.conversation_id AS cid, e.reason AS reason, "
            "  e.agent_id AS agent_id, e.agent_name AS agent_name, "
            "  e.wait_sec AS wait_sec, e.ts AS ts, e.assigned_to AS assigned_to, "
            "  (SELECT MIN(s.ts) FROM agent_sends s "
            "   WHERE s.conversation_id=e.conversation_id AND s.ts>=e.ts) AS taken_ts, "
            "  (SELECT s.agent_id FROM agent_sends s "
            "   WHERE s.conversation_id=e.conversation_id AND s.ts>=e.ts "
            "   ORDER BY s.ts ASC LIMIT 1) AS taken_by "
            "FROM escalations e WHERE e.assigned_to=? AND e.ts>=? "
            "ORDER BY e.ts DESC LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(
                sql, (aid, float(since_ts), int(max(1, min(500, limit))))
            ).fetchall()
        out = []
        for r in rows:
            taken = r["taken_ts"]
            out.append({
                "id": int(r["id"]),
                "conversation_id": str(r["cid"]),
                "reason": str(r["reason"] or ""),
                "agent_id": str(r["agent_id"] or ""),
                "agent_name": str(r["agent_name"] or ""),
                "wait_sec": int(r["wait_sec"] or 0),
                "ts": float(r["ts"] or 0),
                "assigned_to": str(r["assigned_to"] or ""),
                "taken_ts": float(taken) if taken is not None else None,
                "taken_by": str(r["taken_by"]) if r["taken_by"] is not None else "",
                "takeover_sec": int(float(taken) - float(r["ts"])) if taken else None,
            })
        return out

    def list_conversation_claims(self) -> List[Dict[str, Any]]:
        self.purge_expired_claims()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM conversation_claims ORDER BY claimed_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def list_claims_by_agent(self, agent_id: str) -> List[Dict[str, Any]]:
        """K2：列出指定坐席当前持有的所有 conversation claims（已过期的不计）。"""
        self.purge_expired_claims()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM conversation_claims WHERE agent_id = ? ORDER BY claimed_at DESC",
                (str(agent_id or ""),),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_conversation_claim(
        self,
        conversation_id: str,
        agent_id: str,
        *,
        agent_name: str = "",
        ttl_sec: float = 900,
        force: bool = False,
    ) -> Dict[str, Any]:
        cid = str(conversation_id or "").strip()
        aid = str(agent_id or "").strip()
        if not cid or not aid:
            raise ValueError("conversation_id and agent_id required")
        now = self._now()
        exp = now + max(60.0, float(ttl_sec or 900))
        # 原子抢占（消除 TOCTOU）：检查与写入在同一 self._lock 临界区内完成。
        # 注意：threading.Lock 不可重入——锁内绝不能调 purge_expired_claims /
        # get_conversation_claim（二者各自 with self._lock，会自死锁），故在此内联
        # 过期清理与回读。语义与旧实现等价：无主/过期/同人/force 才写入成功。
        with self._lock:
            # 1) 内联清理过期租约（替代锁外 purge_expired_claims）
            self._conn.execute(
                "DELETE FROM conversation_claims WHERE expires_at > 0 AND expires_at < ?",
                (now,),
            )
            if force:
                self._conn.execute(
                    """
                    INSERT INTO conversation_claims
                        (conversation_id, agent_id, agent_name, claimed_at, expires_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(conversation_id) DO UPDATE SET
                        agent_id = excluded.agent_id,
                        agent_name = excluded.agent_name,
                        claimed_at = excluded.claimed_at,
                        expires_at = excluded.expires_at
                    """,
                    (cid, aid, str(agent_name or ""), now, exp),
                )
                won = True
            else:
                # 2) 无主才插入（原子）；rowcount>0 表示本次抢占成功
                cur = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO conversation_claims
                        (conversation_id, agent_id, agent_name, claimed_at, expires_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (cid, aid, str(agent_name or ""), now, exp),
                )
                won = bool(cur.rowcount and cur.rowcount > 0)
                if not won:
                    # 3) 已有主：仅当为同一坐席时续租（不同坐席 → 抢占失败）
                    cur2 = self._conn.execute(
                        """
                        UPDATE conversation_claims
                            SET agent_name = ?, claimed_at = ?, expires_at = ?
                        WHERE conversation_id = ? AND agent_id = ?
                        """,
                        (str(agent_name or ""), now, exp, cid, aid),
                    )
                    won = bool(cur2.rowcount and cur2.rowcount > 0)
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM conversation_claims WHERE conversation_id = ?",
                (cid,),
            ).fetchone()
        claim = dict(row) if row else {}
        if won:
            return {"ok": True, "claim": claim}
        return {"ok": False, "reason": "already_claimed", "claim": claim}

    def renew_conversation_claim(
        self,
        conversation_id: str,
        agent_id: str,
        *,
        ttl_sec: float = 900,
    ) -> Dict[str, Any]:
        cid = str(conversation_id or "").strip()
        aid = str(agent_id or "").strip()
        existing = self.get_conversation_claim(cid)
        if not existing:
            return {"ok": False, "reason": "not_claimed"}
        if existing.get("agent_id") != aid:
            return {"ok": False, "reason": "not_owner", "claim": existing}
        now = self._now()
        exp = now + max(60.0, float(ttl_sec or 900))
        with self._lock:
            self._conn.execute(
                "UPDATE conversation_claims SET expires_at = ? WHERE conversation_id = ?",
                (exp, cid),
            )
            self._conn.commit()
        return {"ok": True, "claim": self.get_conversation_claim(cid)}

    def release_conversation_claim(
        self,
        conversation_id: str,
        agent_id: str,
        *,
        force: bool = False,
    ) -> Dict[str, Any]:
        cid = str(conversation_id or "").strip()
        aid = str(agent_id or "").strip()
        existing = self.get_conversation_claim(cid)
        if not existing:
            return {"ok": True, "released": False}
        if existing.get("agent_id") != aid and not force:
            return {"ok": False, "reason": "not_owner", "claim": existing}
        with self._lock:
            self._conn.execute(
                "DELETE FROM conversation_claims WHERE conversation_id = ?", (cid,)
            )
            self._conn.commit()
        return {"ok": True, "released": True, "conversation_id": cid}

    # ── I1 对话智能分析元数据 ─────────────────────────────────────

    _EMOTION_ORDER = ["愤怒", "不满", "催促", "焦虑", "平稳", "满意", "感谢"]

    def update_conv_meta(
        self,
        conversation_id: str,
        *,
        platform: str = "",
        intent: str = "",
        emotion: str = "",
        risk: str = "low",
        contact_id: str = "",
        workspace_id: str = "default",
        max_history: int = 10,
        emotion_intensity: float = -1.0,
    ) -> None:
        """I1：每次新入站消息后，更新对话智能元数据。

        维护滚动窗口情绪/意图历史（max_history 条），用于趋势计算。
        N1: contact_id 用于跨平台会话归档，传入后持久化。
        """
        cid = str(conversation_id or "").strip()
        if not cid:
            return
        now = self._now()
        existing = self.get_conv_meta(cid)
        trace_id_to_use = ""
        if existing:
            ih = list(existing.get("intent_history") or [])
            eh = list(existing.get("emotion_history") or [])
            mc = int(existing.get("msg_count") or 0) + 1
            # 保留已有 contact_id（如未传新值）
            if not contact_id:
                contact_id = str(existing.get("contact_id") or "")
            # S3: 继承已有 trace_id；没有则新生成
            trace_id_to_use = str(existing.get("trace_id") or "")
        else:
            ih, eh, mc = [], [], 1
        if not trace_id_to_use:
            from src.inbox.tracer import new_trace_id
            trace_id_to_use = new_trace_id()
        if intent:
            ih.append(str(intent))
        if emotion:
            eh.append(str(emotion))
        # 保持滚动窗口
        ih = ih[-max_history:]
        eh = eh[-max_history:]
        _wsid = str(workspace_id or "default")
        with self._lock:
            self._conn.execute(
                """INSERT INTO conversation_meta
                   (conversation_id, platform, last_intent, last_emotion, last_risk,
                    intent_history, emotion_history, msg_count, updated_at, contact_id, workspace_id, trace_id,
                    last_emotion_intensity)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(conversation_id) DO UPDATE SET
                     platform       = excluded.platform,
                     last_intent    = CASE WHEN excluded.last_intent != ''
                                      THEN excluded.last_intent ELSE conversation_meta.last_intent END,
                     last_emotion   = CASE WHEN excluded.last_emotion != ''
                                      THEN excluded.last_emotion ELSE conversation_meta.last_emotion END,
                     last_risk      = excluded.last_risk,
                     intent_history = excluded.intent_history,
                     emotion_history= excluded.emotion_history,
                     msg_count      = excluded.msg_count,
                     updated_at     = excluded.updated_at,
                     contact_id     = CASE WHEN excluded.contact_id != ''
                                      THEN excluded.contact_id ELSE conversation_meta.contact_id END,
                     workspace_id   = CASE WHEN excluded.workspace_id != 'default'
                                      THEN excluded.workspace_id ELSE conversation_meta.workspace_id END,
                     trace_id       = CASE WHEN conversation_meta.trace_id = '' OR conversation_meta.trace_id IS NULL
                                      THEN excluded.trace_id ELSE conversation_meta.trace_id END,
                     last_emotion_intensity = CASE WHEN excluded.last_emotion_intensity >= 0
                                      THEN excluded.last_emotion_intensity
                                      ELSE conversation_meta.last_emotion_intensity END
                """,
                (cid, str(platform or ""), str(intent or ""), str(emotion or ""),
                 str(risk or "low"),
                 json.dumps(ih, ensure_ascii=False),
                 json.dumps(eh, ensure_ascii=False),
                 mc, now, str(contact_id or ""), _wsid, trace_id_to_use,
                 float(emotion_intensity if emotion_intensity is not None else -1.0)),
            )
            self._conn.commit()

    # ── R3: CSAT 问卷 ────────────────────────────────────────────────────
    def schedule_csat_survey(
        self,
        *,
        survey_id: str,
        conversation_id: str,
        draft_id: str,
        agent_id: str,
        delay_seconds: float = 300.0,
    ) -> None:
        """R3：将待发 CSAT 问卷登记到 csat_surveys 表（由 SurveyWorker 轮询发送）。"""
        now = self._now()
        send_at = now + float(delay_seconds)
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO csat_surveys
                   (id, conversation_id, draft_id, agent_id, scheduled_at, send_at,
                    sent, response_score, response_ts, created_at)
                   VALUES (?,?,?,?,?,?,0,-1,0,?)""",
                (survey_id, conversation_id, draft_id, agent_id, now, send_at, now),
            )
            self._conn.commit()

    def list_due_surveys(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        """R3：返回到期未发的 CSAT 问卷（send_at <= now, sent=0）。"""
        now = self._now()
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, conversation_id, draft_id, agent_id, send_at
                   FROM csat_surveys WHERE send_at <= ? AND sent = 0
                   ORDER BY send_at LIMIT ?""",
                (now, limit),
            ).fetchall()
        return [
            dict(zip(["id", "conversation_id", "draft_id", "agent_id", "send_at"], r))
            for r in rows
        ]

    def mark_survey_sent(self, survey_id: str) -> None:
        """R3：标记问卷已发送。"""
        with self._lock:
            self._conn.execute(
                "UPDATE csat_surveys SET sent=1 WHERE id=?", (survey_id,)
            )
            self._conn.commit()

    def record_survey_response(
        self,
        conversation_id: str,
        score: int,
    ) -> bool:
        """R3：记录客户 CSAT 问卷回复（score 1-5），同时更新 conversation_meta.csat_score。

        返回 True 表示匹配到待回复问卷。
        """
        score = max(1, min(5, int(score)))
        now = self._now()
        with self._lock:
            row = self._conn.execute(
                """SELECT id FROM csat_surveys
                   WHERE conversation_id=? AND sent=1 AND response_score=-1
                   ORDER BY send_at DESC LIMIT 1""",
                (conversation_id,),
            ).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE csat_surveys SET response_score=?, response_ts=? WHERE id=?",
                (score, now, row[0]),
            )
            # 同步更新 conv_meta.csat_score
            self._conn.execute(
                "UPDATE conversation_meta SET csat_score=?, updated_at=? WHERE conversation_id=?",
                (float(score), now, conversation_id),
            )
            self._conn.commit()
        return True

    def set_conv_survey_awaiting(self, conversation_id: str, flag: bool) -> None:
        """R3：在 conv_meta 中标记/清除 survey_awaiting 状态（用于识别客户回复是否为问卷响应）。"""
        # survey_awaiting 存储在 summary 字段前缀 __survey__ 标记（简洁，不加新列）
        now = self._now()
        with self._lock:
            if flag:
                self._conn.execute(
                    """UPDATE conversation_meta
                       SET summary=CASE WHEN summary NOT LIKE '__survey__%'
                           THEN '__survey__' || summary ELSE summary END,
                       updated_at=?
                       WHERE conversation_id=?""",
                    (now, conversation_id),
                )
            else:
                self._conn.execute(
                    """UPDATE conversation_meta
                       SET summary=REPLACE(summary,'__survey__',''),
                       updated_at=?
                       WHERE conversation_id=?""",
                    (now, conversation_id),
                )
            self._conn.commit()

    def is_survey_awaiting(self, conversation_id: str) -> bool:
        """R3：检查会话是否正在等待 CSAT 问卷回复。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT summary FROM conversation_meta WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            return False
        return str(row[0] or "").startswith("__survey__")

    # ── Q1: 对话摘要 ─────────────────────────────────────────────────────
    def update_conv_summary(self, conversation_id: str, summary: str) -> None:
        """Q1：写入/更新对话摘要到 conversation_meta.summary。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return
        now = self._now()
        with self._lock:
            self._conn.execute(
                "UPDATE conversation_meta SET summary=?, updated_at=? WHERE conversation_id=?",
                (str(summary or ""), now, cid),
            )
            self._conn.commit()

    # ── Q2: 草稿质量评分 ─────────────────────────────────────────────────
    def update_draft_quality(
        self,
        draft_id: str,
        quality_score: float,
        quality_breakdown: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Q2：写入草稿质量分及维度明细到 reply_drafts。"""
        did = str(draft_id or "").strip()
        if not did:
            return
        breakdown_json = json.dumps(quality_breakdown or {}, ensure_ascii=False)
        now = self._now()
        with self._lock:
            self._conn.execute(
                "UPDATE reply_drafts SET quality_score=?, quality_breakdown=?, updated_at=? WHERE draft_id=?",
                (float(quality_score), breakdown_json, now, did),
            )
            self._conn.commit()

    def get_draft_quality(self, draft_id: str) -> Optional[Dict[str, Any]]:
        """Q2：读取草稿质量评分（含明细）。"""
        did = str(draft_id or "").strip()
        if not did:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT quality_score, quality_breakdown FROM reply_drafts WHERE draft_id=?", (did,)
            ).fetchone()
        if row is None:
            return None
        score = float(row[0]) if row[0] is not None else -1.0
        try:
            breakdown = json.loads(row[1] or "{}")
        except Exception:
            breakdown = {}
        return {"quality_score": score, "breakdown": breakdown}

    def list_draft_quality_stats(
        self,
        *,
        since_ts: float = 0.0,
        limit: int = 500,
    ) -> Dict[str, Any]:
        """Q2：汇总质量分分布（用于 dashboard 统计）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT quality_score FROM reply_drafts WHERE quality_score >= 0 AND created_at >= ? LIMIT ?",
                (float(since_ts), limit),
            ).fetchall()
        scores = [float(r[0]) for r in rows]
        if not scores:
            return {"count": 0, "avg": None, "excellent": 0, "good": 0, "fair": 0, "poor": 0}
        avg = sum(scores) / len(scores)
        return {
            "count": len(scores),
            "avg": round(avg, 1),
            "excellent": sum(1 for s in scores if s >= 80),
            "good": sum(1 for s in scores if 60 <= s < 80),
            "fair": sum(1 for s in scores if 40 <= s < 60),
            "poor": sum(1 for s in scores if s < 40),
        }

    # ── Q3: KB 命中率监控 ─────────────────────────────────────────────────
    def record_kb_recommendation(
        self,
        *,
        rec_id: str,
        entry_id: str,
        entry_title: str = "",
        conversation_id: str = "",
        agent_id: str = "",
    ) -> None:
        """Q3：记录 KB 条目被推荐给坐席一次。"""
        import uuid as _uuid
        rid = str(rec_id or _uuid.uuid4().hex[:12])
        now = self._now()
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO kb_recommendation_log
                   (id, entry_id, entry_title, conversation_id, agent_id, recommended_ts)
                   VALUES (?,?,?,?,?,?)""",
                (rid, str(entry_id), str(entry_title), str(conversation_id), str(agent_id), now),
            )
            self._conn.commit()

    def click_kb_recommendation(
        self,
        *,
        rec_id: str,
        used_in_draft: bool = False,
        draft_id: str = "",
    ) -> None:
        """Q3：标记坐席点击了某次 KB 推荐（可选：是否用于草稿）。"""
        now = self._now()
        with self._lock:
            self._conn.execute(
                """UPDATE kb_recommendation_log
                   SET clicked=1, used_in_draft=?, draft_id=?, recommended_ts=recommended_ts
                   WHERE id=?""",
                (1 if used_in_draft else 0, str(draft_id), str(rec_id)),
            )
            self._conn.commit()

    def get_kb_hit_stats(
        self,
        *,
        since_ts: float = 0.0,
        top_n: int = 20,
    ) -> List[Dict[str, Any]]:
        """Q3：返回 KB 条目推荐/点击/使用统计（按命中率排序）。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT entry_id, entry_title,
                          COUNT(*) as recommended,
                          SUM(clicked) as clicked,
                          SUM(used_in_draft) as used
                   FROM kb_recommendation_log
                   WHERE recommended_ts >= ?
                   GROUP BY entry_id, entry_title
                   ORDER BY clicked DESC
                   LIMIT ?""",
                (float(since_ts), top_n),
            ).fetchall()
        result = []
        for row in rows:
            entry_id, entry_title, recommended, clicked, used = row
            recommended = int(recommended or 0)
            clicked = int(clicked or 0)
            used = int(used or 0)
            hit_rate = round(clicked / recommended * 100, 1) if recommended > 0 else 0.0
            use_rate = round(used / recommended * 100, 1) if recommended > 0 else 0.0
            result.append({
                "entry_id": entry_id,
                "entry_title": entry_title,
                "recommended": recommended,
                "clicked": clicked,
                "used": used,
                "hit_rate": hit_rate,
                "use_rate": use_rate,
            })
        return result

    # ── R2: 坐席工作负荷均衡 ─────────────────────────────────────────────
    def get_agent_workload(
        self,
        agent_id: str,
        *,
        active_within_sec: float = 3600,
    ) -> Dict[str, Any]:
        """R2：返回指定坐席当前工作负荷（活跃会话数 / 审计操作数 / 最近处置率）。

        "活跃会话"定义：conversation_claims 中该坐席持有 + 过去 N 秒内有 draft_audit_log。
        """
        aid = str(agent_id or "").strip()
        if not aid:
            return {"agent_id": aid, "active_convs": 0, "recent_actions": 0, "status": "unknown"}
        now = self._now()
        since = now - active_within_sec
        with self._lock:
            # 当前会话租约数
            claimed = self._conn.execute(
                "SELECT COUNT(*) FROM conversation_claims WHERE agent_id=? AND expires_at>?",
                (aid, now),
            ).fetchone()[0]
            # 过去 N 秒内的审计操作数
            recent_actions = self._conn.execute(
                "SELECT COUNT(*) FROM draft_audit_log WHERE agent_id=? AND ts>=?",
                (aid, since),
            ).fetchone()[0]
            # 在线状态
            pres_row = self._conn.execute(
                "SELECT status FROM agent_presence WHERE agent_id=?", (aid,)
            ).fetchone()
            status = pres_row[0] if pres_row else "offline"

        return {
            "agent_id": aid,
            "active_convs": int(claimed or 0),
            "recent_actions": int(recent_actions or 0),
            "status": str(status),
        }

    def list_agent_workloads(
        self,
        *,
        active_within_sec: float = 120,
        max_load_cap: int = 0,
    ) -> List[Dict[str, Any]]:
        """R2：列出所有在线坐席的工作负荷（用于负荷均衡决策）。

        active_within_sec：判断在线的心跳窗口（秒），默认 120s。
        max_load_cap：若 > 0，标记超负荷坐席。
        """
        now = self._now()
        cutoff = now - active_within_sec
        with self._lock:
            agents = self._conn.execute(
                "SELECT agent_id FROM agent_presence WHERE last_seen_at >= ?",
                (cutoff,),
            ).fetchall()
        result = []
        for (aid,) in agents:
            wl = self.get_agent_workload(aid, active_within_sec=active_within_sec)
            if max_load_cap > 0:
                wl["overloaded"] = wl["active_convs"] >= max_load_cap
            result.append(wl)
        # 按负荷升序（用于负荷均衡时选最空的坐席）
        result.sort(key=lambda x: x["active_convs"])
        return result

    def get_lightest_agent(
        self,
        *,
        active_within_sec: float = 120,
        max_load_cap: int = 0,
        exclude_agent: str = "",
    ) -> Optional[str]:
        """R2：返回负荷最轻的在线坐席 ID（用于自动再分配）。

        max_load_cap > 0 时，过滤掉已超负荷的坐席。
        """
        workloads = self.list_agent_workloads(active_within_sec=active_within_sec)
        for wl in workloads:
            if wl["agent_id"] == exclude_agent:
                continue
            if max_load_cap > 0 and wl["active_convs"] >= max_load_cap:
                continue
            if wl.get("status", "offline") in ("offline",):
                continue
            return wl["agent_id"]
        return None

    def upsert_workspace(
        self,
        workspace_id: str,
        display_name: str = "",
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        """P3：创建或更新工作区配置。"""
        wid = str(workspace_id or "").strip()
        if not wid:
            return
        now = self._now()
        config_json = json.dumps(config or {}, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                """INSERT INTO workspaces (workspace_id, display_name, config_json, created_at, updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(workspace_id) DO UPDATE SET
                     display_name = excluded.display_name,
                     config_json  = excluded.config_json,
                     updated_at   = excluded.updated_at
                """,
                (wid, str(display_name or ""), config_json, now, now),
            )
            self._conn.commit()

    def list_workspaces(self) -> List[Dict[str, Any]]:
        """P3：列出所有工作区。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT workspace_id, display_name, config_json, created_at, updated_at "
                "FROM workspaces ORDER BY created_at"
            ).fetchall()
        result = []
        for row in rows:
            d = dict(zip(["workspace_id", "display_name", "config_json", "created_at", "updated_at"], row))
            try:
                d["config"] = json.loads(d.pop("config_json") or "{}")
            except Exception:
                d["config"] = {}
            result.append(d)
        return result

    def get_workspace_stats(self, workspace_id: str) -> Dict[str, Any]:
        """P3：返回指定工作区的基本统计（会话数/审计数/CSAT 均值）。"""
        wid = str(workspace_id or "default")
        with self._lock:
            conv_count = self._conn.execute(
                "SELECT COUNT(*) FROM conversation_meta WHERE workspace_id=?", (wid,)
            ).fetchone()[0]
            audit_count = self._conn.execute(
                "SELECT COUNT(*) FROM draft_audit_log WHERE workspace_id=?", (wid,)
            ).fetchone()[0]
            csat_row = self._conn.execute(
                "SELECT AVG(csat_score) FROM conversation_meta WHERE workspace_id=? AND csat_score>=0",
                (wid,),
            ).fetchone()
            avg_csat = round(float(csat_row[0]), 1) if csat_row and csat_row[0] is not None else None
        return {
            "workspace_id": wid,
            "conversation_count": int(conv_count or 0),
            "audit_count": int(audit_count or 0),
            "avg_csat": avg_csat,
        }

    def get_contact_sessions(
        self,
        contact_id: str,
        *,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """N1：返回同一 contact_id 的所有跨平台会话记录（含 CSAT/情绪趋势）。

        用于客户画像 (K3) 展示该客户历史会话全貌。
        """
        cid = str(contact_id or "").strip()
        if not cid:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM conversation_meta WHERE contact_id=? "
                "ORDER BY updated_at DESC LIMIT ?",
                (cid, max(1, int(limit))),
            ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            for key in ("intent_history", "emotion_history"):
                try:
                    d[key] = json.loads(d.get(key) or "[]")
                except Exception:
                    d[key] = []
            d["emotion_trend"] = self._compute_emotion_trend(d.get("emotion_history") or [])
            result.append(d)
        return result

    def get_contact_csat_avg(self, contact_id: str) -> Optional[float]:
        """N1：返回同一 contact_id 所有对话的 CSAT 均值（-1 表示无数据）。"""
        cid = str(contact_id or "").strip()
        if not cid:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT AVG(csat_score) FROM conversation_meta "
                "WHERE contact_id=? AND csat_score >= 0",
                (cid,),
            ).fetchone()
        if row and row[0] is not None:
            return round(float(row[0]), 1)
        return None

    def update_conv_csat(self, conversation_id: str, csat_score: float) -> None:
        """M1：写入对话 CSAT 评分（会话结束时调用），同时触发 S1 A/B 结果回填。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return
        csat_score = round(max(0.0, min(5.0, float(csat_score))), 1)
        with self._lock:
            self._conn.execute(
                "UPDATE conversation_meta SET csat_score=? WHERE conversation_id=?",
                (csat_score, cid),
            )
            self._conn.commit()
        # S1: 自动回填 A/B 测试结果（best-effort，不影响主流程）
        try:
            from src.inbox.ab_testing import ABTestingStore
            ab = ABTestingStore(self)
            ab.record_outcome(conversation_id=cid, csat_score=csat_score)
        except Exception:
            import logging as _log
            _log.getLogger(__name__).debug("S1 A/B outcome 回填失败（已忽略）", exc_info=True)

    # ── P61-3：分组批量触达日志 ───────────────────────────
    def record_outreach(
        self, conversation_id: str, *, batch_id: str = "", platform: str = "",
        account_id: str = "", status: str = "sent", note: str = "",
        ts: Optional[float] = None,
    ) -> int:
        """记录一次触达（execution 阶段调用）。返回新行 id。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return 0
        t = float(ts if ts is not None else self._now())
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO outreach_log (conversation_id, batch_id, platform, "
                "account_id, status, note, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cid, str(batch_id or ""), str(platform or ""), str(account_id or ""),
                 str(status or "sent"), str(note or ""), t),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def last_outreach_ts(self, conversation_id: str) -> float:
        """该会话最近一次触达时间戳；从未触达返回 0（用于 cooldown 判定）。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return 0.0
        with self._lock:
            row = self._conn.execute(
                "SELECT ts FROM outreach_log WHERE conversation_id=? "
                "ORDER BY ts DESC LIMIT 1", (cid,),
            ).fetchone()
        return float(row["ts"]) if row else 0.0

    def count_outreach_since(self, conversation_id: str, since_ts: float) -> int:
        """该会话自 ``since_ts`` 起的触达条数（P3 每联系人主动预算的读侧；
        走 idx_outreach_conv 索引，轻量）。异常返回 0（预算 fail-open）。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return 0
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM outreach_log "
                    "WHERE conversation_id=? AND ts >= ?",
                    (cid, float(since_ts)),
                ).fetchone()
            return int(row["n"] or 0) if row else 0
        except Exception:
            return 0

    def last_outreach_ts_bulk(self, conversation_ids: List[str]) -> Dict[str, float]:
        """批量取多会话最近触达 ts（避免 N+1 查询）。"""
        ids = [str(c or "").strip() for c in (conversation_ids or []) if str(c or "").strip()]
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT conversation_id, MAX(ts) AS mx FROM outreach_log "
                f"WHERE conversation_id IN ({placeholders}) GROUP BY conversation_id",
                ids,
            ).fetchall()
        return {r["conversation_id"]: float(r["mx"] or 0) for r in rows}

    # 对方机器人守卫 P1（2026-08-03）：触达效果统计剔除 bot 会话的共享条件。
    # 「SpamBot 们的回复率」会污染 pacing/媒体反哺/mode 门控的决策数字——统计
    # 把 bot 算进分母本身就是失真。判定与 peer_bot_guard.conversation_row_is_bot
    # **矩阵全等**（门禁 test_sql_and_python_bot_predicates_agree 钉死，改口径
    # 两边一起改）：持久判定（peer_is_bot=1，跨平台）> 运营覆写（-1 视为真人，
    # 不剔）> Tier0 行级信号（chat_type='bot' / username 以 bot 结尾——**仅限
    # Telegram**，那是 TG 官方保留语义，其他平台无此真值）。
    # 要求外层查询里 outreach_log 别名为 o。
    _NOT_BOT_PEER_SQL = (
        " AND NOT EXISTS (SELECT 1 FROM conversations c"
        " WHERE c.conversation_id = o.conversation_id AND ("
        "   COALESCE(c.peer_is_bot, 0) = 1"
        "   OR (COALESCE(c.peer_is_bot, 0) != -1"
        "       AND lower(COALESCE(c.platform,'')) = 'telegram'"
        "       AND ("
        "     lower(COALESCE(c.chat_type,'')) = 'bot'"
        "     OR (lower(COALESCE(c.username,'')) LIKE '%bot'"
        "         AND length(c.username) > 3)"
        "   ))"
        " ))"
    )

    def outreach_mode_histogram(
        self, batch_prefix: str, *, days: float = 14.0,
        now: Optional[float] = None,
        exclude_bot_peers: bool = True,
    ) -> Dict[str, int]:
        """近 N 天某前缀批次已发触达按 note（=开场 mode）直方图（P2 观测）。

        proactive_topic 真发时 ``record_outreach(batch_id="proactive_topic:<kind>",
        note=<mode>)``——mode 分布从进程计数（重启即清零）升级为落库口径，
        「gentle_checkin 是否还占 100%」重启后照样能回看。纯查询，无副作用。
        ``exclude_bot_peers``（默认 True）＝分布剔除 bot 会话（P1 精度修正）。
        """
        prefix = str(batch_prefix or "").strip()
        if not prefix:
            return {}
        t = float(now if now is not None else self._now())
        since = t - max(0.0, float(days or 0.0)) * 86400.0
        sql = (
            "SELECT o.note AS note, COUNT(*) AS n FROM outreach_log o "
            "WHERE o.batch_id LIKE ? AND o.status='sent' AND o.ts >= ?"
        )
        if exclude_bot_peers:
            sql += self._NOT_BOT_PEER_SQL
        sql += " GROUP BY o.note"
        with self._lock:
            rows = self._conn.execute(sql, (prefix + "%", since)).fetchall()
        return {
            str(r["note"] or "unknown"): int(r["n"])
            for r in rows
        }

    def outreach_note_response_stats(
        self, batch_prefix: str, *,
        response_window_days: float = 3.0,
        lookback_days: float = 14.0,
        now: Optional[float] = None,
        exclude_bot_peers: bool = True,
    ) -> Dict[str, Dict[str, Any]]:
        """近 N 天某前缀批次按 note（=开场 mode）分组的回复率（P4 观测）。

        与 ``outreach_response_stats`` 同一判定口径（触达后 window 内该会话有
        入站=已回复），只是按 note 分桶——「哪种开场客户真的会回」从拍脑袋
        变成读数。纯查询，无副作用。返回 ``{note: {sent, responded,
        response_rate}}``。``exclude_bot_peers``（默认 True）＝剔除 bot 会话
        （SpamBot 秒回会把某 mode 的「回复率」推成 100%，喂脏门控）。
        """
        prefix = str(batch_prefix or "").strip()
        if not prefix:
            return {}
        t = float(now if now is not None else self._now())
        window_s = (float(response_window_days) * 86400.0
                    if response_window_days and response_window_days > 0 else 0.0)
        sql = (
            "SELECT o.note AS note, o.ts AS sent_ts, "
            "(SELECT MIN(m.ts) FROM messages m "
            " WHERE m.conversation_id = o.conversation_id "
            "   AND m.direction = 'in' AND m.ts > o.ts) AS reply_ts "
            "FROM outreach_log o WHERE o.batch_id LIKE ? AND o.status='sent'"
        )
        params: List[Any] = [prefix + "%"]
        if lookback_days and float(lookback_days) > 0:
            sql += " AND o.ts >= ?"
            params.append(t - float(lookback_days) * 86400.0)
        if exclude_bot_peers:
            sql += self._NOT_BOT_PEER_SQL
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            note = str(r["note"] or "unknown")
            b = out.setdefault(note, {"sent": 0, "responded": 0})
            b["sent"] += 1
            reply_ts = r["reply_ts"]
            if reply_ts is None:
                continue
            delta = float(reply_ts) - float(r["sent_ts"])
            if delta <= 0 or (window_s > 0 and delta > window_s):
                continue
            b["responded"] += 1
        for b in out.values():
            b["response_rate"] = (
                round(b["responded"] / b["sent"], 4) if b["sent"] else 0.0)
        return out

    def outreach_batch_stats(self, batch_id: str) -> Dict[str, Any]:
        """某批次的回执统计：按 status 计数 + 总数。"""
        bid = str(batch_id or "").strip()
        if not bid:
            return {"batch_id": "", "total": 0, "by_status": {}}
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM outreach_log WHERE batch_id=? "
                "GROUP BY status", (bid,),
            ).fetchall()
        by_status = {r["status"]: int(r["n"]) for r in rows}
        return {"batch_id": bid, "total": sum(by_status.values()), "by_status": by_status}

    def outreach_response_stats(
        self, batch_id: str, *,
        response_window_days: float = 7.0,
        lookback_days: float = 0.0,
        now: Optional[float] = None,
        exclude_bot_peers: bool = True,
    ) -> Dict[str, Any]:
        """P61-5：触达效果回流——某批次"已发送"消息的回复率。

        判定：对每条 status='sent' 的触达，看其会话在触达 ts 之后（且在
        response_window_days 窗口内，<=0 表示不限窗）是否收到**入站**消息。
        ``lookback_days>0`` 只统计近 N 天发出的触达（P3 媒体反哺要新鲜度——
        三个月前的回复习惯不该决定今天的形态选择；0=全历史，旧行为）。
        返回 sent / responded / response_rate / avg_response_minutes。
        纯查询、无副作用，可随时回看（回复是异步累积的）。
        ``exclude_bot_peers``（默认 True）＝剔除 bot 会话（P1 精度修正：
        媒体反哺/回复率反哺按此读数，bot 秒回会喂脏两套自适应）。
        """
        bid = str(batch_id or "").strip()
        if not bid:
            return {"batch_id": "", "sent": 0, "responded": 0,
                    "response_rate": 0.0, "avg_response_minutes": 0.0}
        window_s = float(response_window_days) * 86400.0 if response_window_days and response_window_days > 0 else 0.0
        # 每条 sent 触达 → 该会话触达后首个入站消息 ts（相关子查询，走 idx_msg_conv_ts）
        sql = (
            "SELECT o.ts AS sent_ts, "
            "(SELECT MIN(m.ts) FROM messages m "
            " WHERE m.conversation_id = o.conversation_id "
            "   AND m.direction = 'in' AND m.ts > o.ts) AS reply_ts "
            "FROM outreach_log o WHERE o.batch_id = ? AND o.status = 'sent'"
        )
        params: List[Any] = [bid]
        if lookback_days and float(lookback_days) > 0:
            t = float(now if now is not None else self._now())
            sql += " AND o.ts >= ?"
            params.append(t - float(lookback_days) * 86400.0)
        if exclude_bot_peers:
            sql += self._NOT_BOT_PEER_SQL
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        sent = len(rows)
        responded = 0
        latencies: List[float] = []
        for r in rows:
            reply_ts = r["reply_ts"]
            if reply_ts is None:
                continue
            delta = float(reply_ts) - float(r["sent_ts"])
            if delta <= 0:
                continue
            if window_s > 0 and delta > window_s:
                continue
            responded += 1
            latencies.append(delta)
        rate = round(responded / sent, 4) if sent else 0.0
        avg_min = round(sum(latencies) / len(latencies) / 60.0, 1) if latencies else 0.0
        return {
            "batch_id": bid, "sent": sent, "responded": responded,
            "response_rate": rate, "avg_response_minutes": avg_min,
            "response_window_days": response_window_days,
        }

    def get_conv_meta(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """I1：获取对话智能元数据，含情绪趋势计算结果。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conversation_meta WHERE conversation_id = ?", (cid,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_conv_meta(row)

    def _row_to_conv_meta(self, row: Any) -> Dict[str, Any]:
        d = dict(row)
        for key in ("intent_history", "emotion_history"):
            try:
                d[key] = json.loads(d.get(key) or "[]")
            except Exception:
                d[key] = []
        d["emotion_trend"] = self._compute_emotion_trend(d.get("emotion_history") or [])
        return d

    def get_conversations_for_ids(
        self, conversation_ids: List[str],
    ) -> Dict[str, Dict[str, Any]]:
        """批量取会话事实（health-board 等跨域富集用，避免逐行 get_conversation）。"""
        ids = list(dict.fromkeys(
            str(x).strip() for x in (conversation_ids or []) if str(x).strip()))
        if not ids:
            return {}
        ph = ",".join("?" * len(ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM conversations WHERE conversation_id IN ({ph})", ids,
            ).fetchall()
        return {r["conversation_id"]: dict(r) for r in rows}

    def get_conv_meta_for_ids(
        self, conversation_ids: List[str],
    ) -> Dict[str, Dict[str, Any]]:
        """批量取对话智能元数据（含 emotion_trend），与 get_conv_meta 字段一致。"""
        ids = list(dict.fromkeys(
            str(x).strip() for x in (conversation_ids or []) if str(x).strip()))
        if not ids:
            return {}
        ph = ",".join("?" * len(ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM conversation_meta WHERE conversation_id IN ({ph})", ids,
            ).fetchall()
        return {r["conversation_id"]: self._row_to_conv_meta(r) for r in rows}

    def _compute_emotion_trend(self, history: List[str]) -> str:
        """将最近情绪序列映射为 rising/falling/stable 趋势。

        用于 UI 显示趋势箭头（📈升级/📉降级/📊平稳）。
        """
        if len(history) < 2:
            return "stable"
        # 映射情绪到数值（愤怒=最高负面=0，感谢=最高正面=6）
        order = self._EMOTION_ORDER
        def score(e: str) -> int:
            try:
                idx = order.index(e)
                # 情绪紧张度：愤怒=高，感谢=低
                return len(order) - 1 - idx
            except ValueError:
                return 2  # 默认中间值
        recent = history[-5:]
        scores = [score(e) for e in recent]
        if len(scores) >= 2:
            delta = scores[-1] - scores[0]
            if delta >= 2:
                return "rising"   # 情绪在恶化
            if delta <= -2:
                return "falling"  # 情绪在好转
        return "stable"

    # ── I3 回复模板库 ─────────────────────────────────────────────

    def seed_templates(self, templates: List[Dict[str, Any]]) -> int:
        """I3：预置模板（幂等：id 冲突则跳过）。返回实际插入数量。"""
        now = self._now()
        inserted = 0
        for t in templates:
            tid = str(t.get("id") or uuid.uuid4().hex)
            with self._lock:
                cur = self._conn.execute(
                    """INSERT OR IGNORE INTO reply_templates
                       (id, title, content, language, platform, scene,
                        created_by, created_at, updated_at, used_count, is_active)
                       VALUES (?,?,?,?,?,?,?,?,?,0,1)""",
                    (tid, str(t.get("title") or ""), str(t.get("content") or ""),
                     str(t.get("language") or "zh"), str(t.get("platform") or ""),
                     str(t.get("scene") or ""), str(t.get("created_by") or "system"),
                     now, now),
                )
                self._conn.commit()
            inserted += int(cur.rowcount or 0)
        return inserted

    def list_templates(
        self,
        *,
        language: str = "",
        platform: str = "",
        scene: str = "",
        search: str = "",
        limit: int = 100,
        active_only: bool = True,
    ) -> List[Dict[str, Any]]:
        """I3：列出模板，支持多维度过滤。"""
        clauses, params = [], []
        if active_only:
            clauses.append("is_active = 1")
        if language:
            clauses.append("language = ?")
            params.append(language)
        if platform:
            clauses.append("(platform = ? OR platform = '')")
            params.append(platform)
        if scene:
            clauses.append("scene = ?")
            params.append(scene)
        if search:
            clauses.append("(title LIKE ? OR content LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(min(200, max(1, int(limit))))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM reply_templates {where} ORDER BY used_count DESC, created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def create_template(
        self,
        *,
        title: str,
        content: str,
        language: str = "zh",
        platform: str = "",
        scene: str = "",
        created_by: str = "admin",
    ) -> str:
        """I3：创建新模板，返回 id。"""
        tid = uuid.uuid4().hex
        now = self._now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO reply_templates
                   (id, title, content, language, platform, scene,
                    created_by, created_at, updated_at, used_count, is_active)
                   VALUES (?,?,?,?,?,?,?,?,?,0,1)""",
                (tid, str(title), str(content), str(language or "zh"),
                 str(platform or ""), str(scene or ""), str(created_by or "admin"),
                 now, now),
            )
            self._conn.commit()
        return tid

    def update_template(
        self,
        tid: str,
        *,
        title: Optional[str] = None,
        content: Optional[str] = None,
        language: Optional[str] = None,
        platform: Optional[str] = None,
        scene: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> bool:
        """I3：更新模板字段（仅传入非 None 的字段），返回是否找到并更新。"""
        updates, params = [], []
        if title is not None:
            updates.append("title = ?")
            params.append(str(title))
        if content is not None:
            updates.append("content = ?")
            params.append(str(content))
        if language is not None:
            updates.append("language = ?")
            params.append(str(language))
        if platform is not None:
            updates.append("platform = ?")
            params.append(str(platform))
        if scene is not None:
            updates.append("scene = ?")
            params.append(str(scene))
        if is_active is not None:
            updates.append("is_active = ?")
            params.append(1 if is_active else 0)
        if not updates:
            return False
        updates.append("updated_at = ?")
        params.append(self._now())
        params.append(str(tid))
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE reply_templates SET {', '.join(updates)} WHERE id = ?", params
            )
            self._conn.commit()
        return int(cur.rowcount or 0) > 0

    def delete_template(self, tid: str) -> bool:
        """I3：软删除（is_active=0），保留历史记录。"""
        return self.update_template(tid, is_active=False)

    def increment_template_usage(self, tid: str) -> None:
        """I3：模板使用计数 +1（best-effort）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE reply_templates SET used_count = used_count + 1, updated_at = ?"
                " WHERE id = ?",
                (self._now(), str(tid)),
            )
            self._conn.commit()

    # ── T1: 会话级标签 + 归档 ──────────────────────────────────────────
    def save_conv_summary(self, conversation_id: str, summary: str) -> bool:
        """Phase 19：写入会话 AI 摘要（归档时自动生成）。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return False
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO conversation_meta (conversation_id, summary, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    summary    = excluded.summary,
                    updated_at = excluded.updated_at
                """,
                (cid, str(summary or ""), self._now()),
            )
            self._conn.commit()
        return True

    def get_conv_tags(self, conversation_id: str) -> List[str]:
        """T1：获取会话标签列表。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return []
        with self._lock:
            row = self._conn.execute(
                "SELECT conv_tags FROM conversation_meta WHERE conversation_id = ?", (cid,)
            ).fetchone()
        if row is None:
            return []
        try:
            return json.loads(row["conv_tags"] or "[]")
        except Exception:
            return []

    def set_conv_tags(self, conversation_id: str, tags: List[str]) -> bool:
        """T1：覆写会话标签列表；如 conversation_meta 行不存在则插入。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return False
        tags = [str(t).strip() for t in tags if str(t).strip()]
        tags_json = json.dumps(tags, ensure_ascii=False)
        now = self._now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO conversation_meta (conversation_id, conv_tags, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    conv_tags  = excluded.conv_tags,
                    updated_at = excluded.updated_at
                """,
                (cid, tags_json, now),
            )
            self._conn.commit()
        return True

    def set_manual_mood(
        self, conversation_id: str, mood: str, *, by: str = "",
        ts: Optional[float] = None,
    ) -> bool:
        """T-mood：落人工「客户情绪」标注 arbitration 列（mood=''=清除）。

        唯一调用方是 ``effective_mood.apply_mood_tag``（标签与列同步双写）；
        AI 消费判据（TTL/在场校验）在 effective_mood，本方法只管持久化。
        """
        cid = str(conversation_id or "").strip()
        if not cid:
            return False
        now = self._now()
        stamp = float(ts if ts is not None else now)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO conversation_meta
                    (conversation_id, mood_manual, mood_manual_ts, mood_manual_by, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    mood_manual    = excluded.mood_manual,
                    mood_manual_ts = excluded.mood_manual_ts,
                    mood_manual_by = excluded.mood_manual_by,
                    updated_at     = excluded.updated_at
                """,
                (cid, str(mood or "").strip(), stamp, str(by or "").strip(), now),
            )
            self._conn.commit()
        return True

    def set_conversation_pref_engine(self, conversation_id: str, engine: str) -> bool:
        """F+：持久化会话首选翻译引擎（多线路对照择优后记住）。空串=清除偏好。

        直接更新 ``conversations.pref_engine``；会话行不存在（未收过消息）→ 返回 False。
        """
        cid = str(conversation_id or "").strip()
        if not cid:
            return False
        eng = str(engine or "").strip().lower()
        now = self._now()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE conversations SET pref_engine = ?, updated_at = ? "
                "WHERE conversation_id = ?",
                (eng, now, cid),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get_app_setting(self, key: str, default: str = "") -> str:
        """P3：读通用 KV 配置（app_settings）。键不存在/空 → 返回 default。"""
        k = str(key or "").strip()
        if not k:
            return default
        with self._lock:
            row = self._conn.execute(
                "SELECT sval FROM app_settings WHERE skey = ?", (k,)
            ).fetchone()
        if row is None:
            return default
        val = row[0]
        return str(val) if val is not None else default

    def set_app_setting(self, key: str, value: str, updated_by: str = "") -> bool:
        """P3：写通用 KV 配置；``value`` 为空串 → 删除该键（视为「清除/未配置」）。

        P4-A：``updated_by`` 记录最后修改人（运营级配置可追责，best-effort）。
        """
        k = str(key or "").strip()
        if not k:
            return False
        v = str(value or "").strip()
        by = str(updated_by or "").strip()
        now = self._now()
        with self._lock:
            if not v:
                self._conn.execute("DELETE FROM app_settings WHERE skey = ?", (k,))
            else:
                self._conn.execute(
                    """INSERT INTO app_settings (skey, sval, updated_at, updated_by)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(skey) DO UPDATE SET
                            sval = excluded.sval, updated_at = excluded.updated_at,
                            updated_by = excluded.updated_by""",
                    (k, v, now, by),
                )
            self._conn.commit()
        return True

    def list_app_settings(self, prefix: str = "") -> List[Dict[str, Any]]:
        """P4-A：列出（前缀匹配的）通用 KV 配置，按 key 升序。

        返回 ``[{key, value, updated_by, updated_at}]``（仅含已存在的非空配置）。
        ``prefix=""`` 列全部。
        """
        pfx = str(prefix or "")
        with self._lock:
            if pfx:
                rows = self._conn.execute(
                    "SELECT skey, sval, updated_by, updated_at FROM app_settings "
                    "WHERE skey LIKE ? ESCAPE '\\' ORDER BY skey",
                    (pfx.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT skey, sval, updated_by, updated_at FROM app_settings ORDER BY skey"
                ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append({
                "key": r[0], "value": r[1],
                "updated_by": r[2] if len(r) > 2 else "",
                "updated_at": r[3] if len(r) > 3 else 0,
            })
        return out

    def set_conv_archived(self, conversation_id: str, archived: bool, *,
                          source: str = "", actor: str = "") -> bool:
        """T1：标记/取消归档；如 conversation_meta 行不存在则插入。

        ``archived_at`` 与 ``archived`` 同一次写入（取消归档写 0）——它是
        ``_unarchive_on_inbound`` 判断「客户是否在归档之后又开口」的唯一判据，
        **别再用 updated_at 代理**（见该列 migration 注释里的实锤）。

        ``source``/``actor`` 只进审计日志：归档会让会话从工作台所有视图消失，属高
        影响动作，而此前它在服务端完全不留痕——198 事故排查时日志里 archive 零命中，
        既无法证明也无法否证「谁在什么时候用什么方式归档的」。
        """
        cid = str(conversation_id or "").strip()
        if not cid:
            return False
        val = 1 if archived else 0
        now = self._now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO conversation_meta
                    (conversation_id, archived, archived_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    archived    = excluded.archived,
                    archived_at = excluded.archived_at,
                    updated_at  = excluded.updated_at
                """,
                (cid, val, (now if archived else 0.0), now),
            )
            self._conn.commit()
        logger.info(
            "[InboxStore] 会话%s cid=%s source=%s actor=%s",
            "归档" if archived else "取消归档", cid, source or "-", actor or "-",
        )
        if archived:
            try:   # P4 埋点：会话归档视作关闭（fail-silent）
                from src.utils.telemetry import track
                track("session.closed", {"session_id": cid})
            except Exception:
                pass
        return True

    def _unarchive_on_inbound(self, conversation_id: str, inbound_ts: float) -> bool:
        """归档会话收到「归档之后」的新入站消息 → 自动取消归档。返回是否真的复活了。

        为什么必须有这条（P0-198，2026-08-04 实锤）：归档只是「收起来」的意思，而实现
        上它是**永久**的——归档后无论客户再说多少句话，会话都不会回到任何默认视图
        （工作台列表默认 ``archived=0``），坐席看不到、也不会有未读提示。198 上那条 33
        条消息的活跃会话就是这样在聊天中途「凭空消失」的：手机上客户还在发，工作台里
        人已经找不到它了。归档在语义上应当是**可被客户自己撤销**的。

        判据只有一条：``inbound_ts > archived_at``（消息的**平台时间**，不是入库时间）。
        这样才能区分两类外形一致的入站：
        - 客户在归档后又开口 → ts 晚于归档时刻 → 复活（想要）；
        - 首次全量同步 / thread 重放把**归档之前**的历史消息灌进来 → ts 早于归档时刻
          → 不动（不然刚归档的会话会被自己的历史消息立刻顶回来，归档功能等于失效）。

        单条原子 UPDATE 完成「判定+写入」，不做先读后写——ingest 是并发热路径，
        read-modify-write 会在两条消息同时到达时丢一次判定。
        ``archived_at > 0`` 是保守闸：理论上 migration 回填后不存在
        ``archived=1 且 archived_at=0`` 的行，真出现了也宁可不动（避免一次误判把大批
        历史归档会话集体唤回），只留一条 warning 便于追查。
        """
        cid = str(conversation_id or "").strip()
        ts = float(inbound_ts or 0)
        if not cid or ts <= 0:
            return False
        now = self._now()
        stale = None
        with self._lock:
            cur = self._conn.execute(
                "UPDATE conversation_meta SET archived = 0, archived_at = 0, updated_at = ? "
                "WHERE conversation_id = ? AND archived = 1 AND archived_at > 0 "
                "AND archived_at < ?",
                (now, cid, ts),
            )
            revived = (cur.rowcount or 0) > 0
            if revived:
                self._conn.commit()
            else:
                # 顺带体检：归档着却没有归档时刻＝上面那条保守闸拦下的异常行
                stale = self._conn.execute(
                    "SELECT 1 FROM conversation_meta WHERE conversation_id = ? "
                    "AND archived = 1 AND archived_at <= 0 LIMIT 1", (cid,),
                ).fetchone()
        if revived:
            logger.info(
                "[InboxStore] 会话自动取消归档 cid=%s 触发=入站消息 ts=%.0f", cid, ts,
            )
            return True
        if stale is not None:
            logger.warning(
                "[InboxStore] 会话 archived=1 但 archived_at=0，未自动复活 cid=%s"
                "（migration 回填是否跑过？）", cid,
            )
        return False

    def list_buried_archived(self, *, min_unread: int = 1, limit: int = 50) -> list[dict]:
        """「被埋掉的会话」体检：归档着、却有未读入站——客户在等，而工作台看不见它。

        为什么需要一条与 ``archived_at`` 无关的信号（P0-198）：``archived_at`` 是本次
        才加的列，存量已归档行只能回填「升级时刻」（真实归档时刻不可追溯——
        ``updated_at`` 早被入站链路刷掉，见该列 migration 注释）。于是**存量**被埋的
        会话里，客户是在升级之前开口的 → ts < 回填值 → ``_unarchive_on_inbound`` 判据
        天然够不着它们，光靠自动复活永远查不出这批历史损失。

        ``unread > 0`` 恰好绕开时间戳完全成立：未读只可能由入站消息产生，且「没人读过」
        本身就是「没人看得见」的直接证据。误报面极小——真要归档一段已收尾的对话，坐席
        通常读完了才归档（unread=0）。

        纯查询、零副作用：**刻意不自动解档**。存量行缺可信归档时刻，批量唤回等于拿
        一个猜测去覆盖运营的明示决定；这里只负责让损失可数、可核对，处置交人。
        返回按未读数降序，供审计 CLI / 后续 watchdog 与 ops 卡共用（下一阶段只需接线）。
        """
        try:
            n = max(1, int(min_unread))
        except Exception:
            n = 1
        try:
            cap = max(1, min(500, int(limit)))
        except Exception:
            cap = 50
        with self._lock:
            rows = self._conn.execute(
                """SELECT c.conversation_id, c.platform, c.account_id, c.chat_key,
                          c.display_name, c.unread, c.last_ts, c.last_text,
                          cm.archived_at, cm.auto_archived_at
                   FROM conversations c
                   JOIN conversation_meta cm ON cm.conversation_id = c.conversation_id
                   WHERE cm.archived = 1 AND c.unread >= ?
                   ORDER BY c.unread DESC, c.last_ts DESC
                   LIMIT ?""",
                (n, cap),
            ).fetchall()
        return [dict(r) for r in rows]

    def tag_stats(self, *, since: float = 0.0) -> List[Dict[str, Any]]:
        """T2：聚合每个标签的会话数、未读数、平均等待秒数（用于标签概览 strip）。

        算法：扫 conversation_meta 中 conv_tags 非空的行，join conversations 表取
        unread / last_ts；join messages 最近1条方向判断是否等待回复。
        since=0 表示全量（不按时间过滤）。
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT m.conversation_id, m.conv_tags, m.archived,
                       c.unread, c.last_ts, c.platform
                FROM conversation_meta m
                LEFT JOIN conversations c ON c.conversation_id = m.conversation_id
                WHERE m.conv_tags != '[]' AND m.conv_tags != ''
                """
            ).fetchall()
        # 聚合
        tag_map: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            try:
                tags = json.loads(r["conv_tags"] or "[]")
            except Exception:
                tags = []
            for tag in tags:
                if not tag:
                    continue
                entry = tag_map.setdefault(tag, {
                    "tag": tag, "count": 0, "unread": 0,
                    "archived": 0, "platforms": set(),
                })
                entry["count"] += 1
                entry["unread"] += int(r["unread"] or 0)
                if r.get("archived"):
                    entry["archived"] += 1
                if r.get("platform"):
                    entry["platforms"].add(str(r["platform"]))
        result = []
        for entry in sorted(tag_map.values(), key=lambda x: -x["count"]):
            result.append({
                "tag": entry["tag"],
                "count": entry["count"],
                "unread": entry["unread"],
                "archived": entry["archived"],
                "platforms": sorted(entry["platforms"]),
            })
        return result

    def list_conv_tags_map(self, conversation_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """T1：批量获取会话标签+归档状态+搁置到点（用于列表渲染）。

        返回 {conv_id: {"tags": [...], "archived": bool, "snooze_until": float}}。
        ``snooze_until`` 为重浮的 epoch 秒（0=未搁置）；前端据此在「超时/待接管」视图
        隐藏已搁置会话并渲染 header 搁置态（读时不做「是否已到点」判定，交前端按 now 比较）。
        """
        if not conversation_ids:
            return {}
        placeholders = ",".join("?" * len(conversation_ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT conversation_id, conv_tags, archived, snooze_until FROM conversation_meta"
                f" WHERE conversation_id IN ({placeholders})",
                conversation_ids,
            ).fetchall()
        result: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            try:
                tags = json.loads(r["conv_tags"] or "[]")
            except Exception:
                tags = []
            result[r["conversation_id"]] = {
                "tags": tags,
                "archived": bool(r["archived"]),
                "snooze_until": float(r["snooze_until"] or 0),
            }
        return result

    # ── V1: 坐席协作注解（Phase 25） ─────────────────────────────────────
    def add_conv_note(
        self,
        conversation_id: str,
        body: str,
        *,
        agent_id: str = "",
        agent_name: str = "",
        mentions: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """V1：在会话中添加内部注解（对客户不可见）。

        返回刚插入的 note dict；body 为空时抛 ValueError。
        """
        body = str(body or "").strip()
        if not body:
            raise ValueError("note body 不能为空")
        cid = str(conversation_id or "").strip()
        if not cid:
            raise ValueError("conversation_id 不能为空")
        import uuid as _uuid
        note_id = str(_uuid.uuid4())
        ts = self._now()
        mentions_list: List[str] = [str(m) for m in (mentions or []) if str(m).strip()]
        with self._lock:
            self._conn.execute(
                """INSERT INTO conv_notes
                   (note_id, conversation_id, agent_id, agent_name, body, mentions, ts, edited_ts)
                   VALUES (?,?,?,?,?,?,?,0)""",
                (note_id, cid, str(agent_id or ""), str(agent_name or ""),
                 body, json.dumps(mentions_list, ensure_ascii=False), ts),
            )
            self._conn.commit()
        return {
            "note_id": note_id, "conversation_id": cid,
            "agent_id": agent_id, "agent_name": agent_name,
            "body": body, "mentions": mentions_list, "ts": ts, "edited_ts": 0,
        }

    def list_conv_notes(
        self, conversation_id: str, *, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """V1：获取会话的全部内部注解（按时间升序）。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return []
        limit = max(1, min(200, int(limit or 50)))
        with self._lock:
            rows = self._conn.execute(
                """SELECT note_id, conversation_id, agent_id, agent_name,
                          body, mentions, ts, edited_ts
                   FROM conv_notes WHERE conversation_id = ?
                   ORDER BY ts ASC LIMIT ?""",
                (cid, limit),
            ).fetchall()
        result = []
        for r in rows:
            try:
                mentions_list = json.loads(r["mentions"] or "[]")
            except Exception:
                mentions_list = []
            result.append({
                "note_id": r["note_id"], "conversation_id": r["conversation_id"],
                "agent_id": r["agent_id"], "agent_name": r["agent_name"],
                "body": r["body"], "mentions": mentions_list,
                "ts": r["ts"], "edited_ts": r["edited_ts"],
            })
        return result

    def edit_conv_note(
        self, note_id: str, body: str, *, agent_id: str = ""
    ) -> bool:
        """V1：编辑注解内容（仅注解作者或管理员可编辑，此处由 API 层鉴权）。"""
        body = str(body or "").strip()
        if not body:
            return False
        with self._lock:
            cur = self._conn.execute(
                "UPDATE conv_notes SET body=?, edited_ts=? WHERE note_id=?",
                (body, self._now(), str(note_id)),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def delete_conv_note(self, note_id: str, *, agent_id: str = "") -> bool:
        """V1：删除注解（由 API 层控制权限）。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM conv_notes WHERE note_id=?", (str(note_id),)
            )
            self._conn.commit()
        return cur.rowcount > 0

    # ── X1: 客户 360° 时间轴（Phase 31） ────────────────────────────────

    def get_contact_timeline(
        self, contact_id: str, *, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """X1：聚合指定客户所有互动事件，按时间倒序返回时间轴。

        事件类型（event_type）：
          message   — 入站/出站消息
          note      — 坐席内部注解（含@提及）
          archived  — 会话归档
          summary   — AI 摘要生成
          conv_open — 会话首次建立

        优化：主体采用 UNION ALL 单次扫描 + Python 侧合并，减少 DB 往返次数。
        """
        cid = str(contact_id or "").strip()
        if not cid:
            return []
        limit = max(1, min(500, int(limit or 100)))

        events: List[Dict[str, Any]] = []

        with self._lock:
            # 第一步：查属于该 contact 的所有会话（最多 50 条）
            conv_rows = self._conn.execute(
                """SELECT conversation_id, platform, display_name, created_at
                   FROM conversations WHERE contact_id = ?
                   ORDER BY last_ts DESC LIMIT 50""",
                (cid,),
            ).fetchall()
            if not conv_rows:
                return []

            conv_ids = [r["conversation_id"] for r in conv_rows]
            conv_info = {r["conversation_id"]: dict(r) for r in conv_rows}

            # 会话建立事件
            for cv in conv_rows:
                events.append({
                    "event_type": "conv_open",
                    "ts": float(cv["created_at"] or 0),
                    "conversation_id": cv["conversation_id"],
                    "platform": cv["platform"],
                    "display_name": cv["display_name"],
                    "preview": f"开始 {cv['platform']} 会话",
                    "meta": {},
                    "_sort_key": float(cv["created_at"] or 0),
                })

            ph = ",".join("?" * len(conv_ids))
            fetch_limit = min(limit * 4, 400)  # 多拉一些以保证排序后截断准确

            # 第二步：UNION ALL 一次性拉取消息 + 注解（同型字段）
            union_rows = self._conn.execute(
                f"""
                SELECT 'message' AS etype, m.conversation_id, m.ts,
                       substr(m.text, 1, 120) AS preview,
                       m.direction AS dir, m.media_type AS extra1, m.message_id AS extra2, '' AS extra3
                FROM messages m
                WHERE m.conversation_id IN ({ph}) AND m.text != ''

                UNION ALL

                SELECT 'note' AS etype, n.conversation_id, n.ts,
                       substr(n.body, 1, 120) AS preview,
                       n.agent_name AS dir, n.note_id AS extra1, n.mentions AS extra2, '' AS extra3
                FROM conv_notes n
                WHERE n.conversation_id IN ({ph})

                ORDER BY ts DESC LIMIT ?
                """,
                conv_ids + conv_ids + [fetch_limit],
            ).fetchall()

            for r in union_rows:
                cv = conv_info.get(r["conversation_id"], {})
                ts = float(r["ts"] or 0)
                if r["etype"] == "message":
                    events.append({
                        "event_type": "message",
                        "ts": ts,
                        "conversation_id": r["conversation_id"],
                        "platform": cv.get("platform", ""),
                        "display_name": cv.get("display_name", ""),
                        "preview": str(r["preview"] or ""),
                        "meta": {
                            "direction": r["dir"],
                            "media_type": r["extra1"] or None,
                            "message_id": r["extra2"],
                        },
                        "_sort_key": ts,
                    })
                else:  # note
                    try:
                        mentions = json.loads(r["extra2"] or "[]")
                    except Exception:
                        mentions = []
                    events.append({
                        "event_type": "note",
                        "ts": ts,
                        "conversation_id": r["conversation_id"],
                        "platform": cv.get("platform", ""),
                        "display_name": cv.get("display_name", ""),
                        "preview": str(r["preview"] or ""),
                        "meta": {
                            "note_id": r["extra1"],
                            "agent_name": r["dir"],
                            "mentions": mentions,
                        },
                        "_sort_key": ts,
                    })

            # 第三步：会话 meta（归档 + 摘要，单独查询，数量少）
            meta_rows = self._conn.execute(
                f"""SELECT conversation_id, archived, summary, updated_at
                    FROM conversation_meta WHERE conversation_id IN ({ph})""",
                conv_ids,
            ).fetchall()
            for r in meta_rows:
                cv = conv_info.get(r["conversation_id"], {})
                ts = float(r["updated_at"] or 0)
                if r["archived"]:
                    events.append({
                        "event_type": "archived",
                        "ts": ts,
                        "conversation_id": r["conversation_id"],
                        "platform": cv.get("platform", ""),
                        "display_name": cv.get("display_name", ""),
                        "preview": "会话已归档",
                        "meta": {"archived": True},
                        "_sort_key": ts,
                    })
                if r["summary"]:
                    events.append({
                        "event_type": "summary",
                        "ts": ts,
                        "conversation_id": r["conversation_id"],
                        "platform": cv.get("platform", ""),
                        "display_name": cv.get("display_name", ""),
                        "preview": str(r["summary"] or "")[:120],
                        "meta": {"summary": r["summary"]},
                        "_sort_key": ts,
                    })

        # 全局按 ts 降序，移除内部排序辅助键，截断
        events.sort(key=lambda e: e.get("_sort_key", e["ts"]), reverse=True)
        for e in events:
            e.pop("_sort_key", None)
        return events[:limit]

    # ── W1: 客户活跃时段热力图（Phase 27） ───────────────────────────────
    def activity_heatmap(
        self, *, days: int = 30, platform: str = "", direction: str = "inbound"
    ) -> Dict[str, Any]:
        """W1：统计最近 N 天的消息量按星期×小时分布（本地时区）。

        返回:
          {
            hours: [0..23],          # x 轴
            weekdays: [0..6],        # y 轴 (0=周一, 6=周日)
            matrix: [[int,…],…],    # shape: 7×24, 每格消息数
            peak_hour: int,          # 全局峰值小时
            peak_weekday: int,       # 全局峰值星期
            total: int,              # 总消息数
          }
        """
        import time as _time
        since_ts = _time.time() - max(1, min(365, int(days or 30))) * 86400
        dir_cond = ""
        params: List[Any] = [since_ts]
        if direction in ("inbound", "outbound"):
            dir_cond = " AND direction = ?"
            params.append(direction)
        plat_cond = ""
        if platform:
            plat_cond = """
                AND conversation_id IN (
                    SELECT conversation_id FROM conversations WHERE platform = ?
                )"""
            params.append(str(platform))

        sql = f"""
            SELECT ts FROM messages
            WHERE ts >= ?{dir_cond}{plat_cond}
              AND text != ''
        """
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        # 初始化 7×24 矩阵
        matrix = [[0] * 24 for _ in range(7)]
        for r in rows:
            ts_val = r[0]
            try:
                import datetime as _dt
                dt = _dt.datetime.fromtimestamp(float(ts_val))
                wd = dt.weekday()  # 0=Monday
                hr = dt.hour
                matrix[wd][hr] += 1
            except Exception:
                pass

        total = sum(matrix[wd][hr] for wd in range(7) for hr in range(24))
        peak_hour, peak_wd = 0, 0
        peak_val = -1
        for wd in range(7):
            for hr in range(24):
                if matrix[wd][hr] > peak_val:
                    peak_val = matrix[wd][hr]
                    peak_hour = hr
                    peak_wd = wd

        return {
            "hours": list(range(24)),
            "weekdays": list(range(7)),
            "matrix": matrix,
            "peak_hour": peak_hour,
            "peak_weekday": peak_wd,
            "total": total,
            "days": days,
            "direction": direction,
        }

    # ── Y1: QA 质检评分（Phase 34） ───────────────────────────────────────

    def compute_and_store_qa_score(self, conversation_id: str) -> Dict[str, Any]:
        """Y1：计算指定会话的质检评分并持久化到 conversation_meta。

        Steps:
          1. 拉取会话全量消息
          2. 调用 QAScorer 进行规则评分
          3. 将结果 JSON 写入 conversation_meta.qa_score
          4. 返回评分结果
        """
        from src.inbox.qa_scorer import QAScorer
        cid = str(conversation_id or "").strip()
        if not cid:
            return {}
        # 拉取消息（最多最近 200 条，足够评分）
        with self._lock:
            rows = self._conn.execute(
                """SELECT direction, text, ts FROM messages
                   WHERE conversation_id = ?
                   ORDER BY ts ASC LIMIT 200""",
                (cid,),
            ).fetchall()
        messages = [dict(r) for r in rows]
        result = QAScorer().score(messages)
        # 写回 conversation_meta
        self.patch_conv_meta(cid, {"qa_score": json.dumps(result, ensure_ascii=False)})
        return result

    def get_qa_score(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """Y1：读取已存储的质检评分（不重新计算）。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT qa_score FROM conversation_meta WHERE conversation_id = ?",
                (cid,),
            ).fetchone()
        if not row:
            return None
        raw = str(row["qa_score"] or "").strip()
        if not raw or raw == "{}":
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def batch_agent_qa_stats(
        self, *, days: int = 30
    ) -> List[Dict[str, Any]]:
        """Y1：聚合最近 N 天各坐席的 QA 评分均值（用于 agent_perf 看板）。

        返回 [{agent_id, agent_name, avg_score, count, grade_dist}]
        """
        since = time.time() - days * 86400
        # ⚠️ 幽灵列事故两步修复（2026-08-01）：conversation_meta **从未有过** claimed_by
        # 列，旧 SQL 的 `cm.claimed_by` 让本方法自 Phase 34 起全部署 OperationalError
        # → 路由 500（第一步曾以 '' 占位止血）。认领的唯一事实源是
        # conversation_claims 租约表 → 现按未过期租约 JOIN 归属。诚实边界：租约
        # 有 TTL，本口径是「当前在管坐席」的归属；要做完整历史归属需改读
        # draft_audit_log/reply_drafts 的 agent 字段（另一个口径，勿混）。
        now_ts = time.time()
        with self._lock:
            rows = self._conn.execute(
                """SELECT cc.agent_id AS claimed_by, cm.qa_score, cm.updated_at
                   FROM conversation_meta cm
                   JOIN conversation_claims cc
                     ON cc.conversation_id = cm.conversation_id
                    AND (cc.expires_at <= 0 OR cc.expires_at >= ?)
                   WHERE cm.updated_at >= ? AND cm.qa_score != '' AND cm.qa_score != '{}'
                   ORDER BY cm.updated_at DESC LIMIT 2000""",
                (now_ts, since),
            ).fetchall()
        # 按 claimed_by 分组聚合
        agg: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            agent = str(row["claimed_by"] or "").strip()
            if not agent:
                continue
            try:
                qa = json.loads(row["qa_score"] or "{}")
                score = int(qa.get("score") or 0)
                grade = str(qa.get("grade") or "N/A")
            except Exception:
                continue
            if agent not in agg:
                agg[agent] = {"agent_id": agent, "scores": [], "grades": {}}
            agg[agent]["scores"].append(score)
            agg[agent]["grades"][grade] = agg[agent]["grades"].get(grade, 0) + 1

        result = []
        for agent_id, data in sorted(agg.items()):
            scores = data["scores"]
            avg = round(sum(scores) / len(scores)) if scores else 0
            result.append({
                "agent_id": agent_id,
                "agent_name": agent_id,  # 可在 API 层替换真实姓名
                "avg_score": avg,
                "count": len(scores),
                "grade": QAScorer_grade(avg),
                "grade_dist": data["grades"],
            })
        result.sort(key=lambda x: x["avg_score"], reverse=True)
        return result

    # ── Z1: 流失预警（Phase 35）──────────────────────────────────────────────

    def list_churn_risk_conversations(
        self,
        *,
        silence_days: int = 7,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Z1：列出高流失风险会话（最近 N 天无活动且末条为入站）。

        辅助 ChurnPredictor 的数据获取层（独立于联系人表）。
        """
        cutoff = time.time() - silence_days * 86400
        # ⚠️ 幽灵列事故两步修复（2026-08-01，同 batch_agent_qa_stats）：`cm.claimed_by`
        # 列不存在，旧 SQL 让流失预警轻量榜自 Phase 35 起全部署 500（第一步曾以 ''
        # 占位止血）。现按 conversation_claims 未过期租约 LEFT JOIN 出真实认领人。
        now_ts = time.time()
        with self._lock:
            rows = self._conn.execute(
                """SELECT c.conversation_id, c.platform, c.display_name,
                          c.contact_id, c.last_ts,
                          COALESCE(cc.agent_id, '') AS claimed_by,
                          cm.churn_risk, cm.qa_score, cm.archived
                   FROM conversations c
                   LEFT JOIN conversation_meta cm
                     ON cm.conversation_id = c.conversation_id
                   LEFT JOIN conversation_claims cc
                     ON cc.conversation_id = c.conversation_id
                    AND (cc.expires_at <= 0 OR cc.expires_at >= ?)
                   WHERE c.last_ts <= ? AND (cm.archived IS NULL OR cm.archived = 0)
                   ORDER BY c.last_ts ASC LIMIT ?""",
                (now_ts, cutoff, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def store_churn_risk(self, conversation_id: str, risk_level: str, reasons: List[str]) -> None:
        """Z1：持久化流失风险评估结果。"""
        data = json.dumps({"level": risk_level, "reasons": reasons, "ts": time.time()}, ensure_ascii=False)
        self.patch_conv_meta(conversation_id, {"churn_risk": data})

    # ── 辅助（跨方法） ───────────────────────────────────────────────────────

    def _auto_archive_candidates(self, idle_hours: int = 24) -> List[Dict[str, Any]]:
        """P36：查找超过 idle_hours 小时未活动且未归档的会话。"""
        cutoff = time.time() - idle_hours * 3600
        with self._lock:
            rows = self._conn.execute(
                """SELECT c.conversation_id, c.display_name, c.platform, c.last_ts, c.contact_id
                   FROM conversations c
                   LEFT JOIN conversation_meta cm ON cm.conversation_id = c.conversation_id
                   WHERE c.last_ts <= ? AND (cm.archived IS NULL OR cm.archived = 0)
                     AND (cm.auto_archived_at IS NULL OR cm.auto_archived_at = 0)
                   ORDER BY c.last_ts ASC LIMIT 100""",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_auto_archived(self, conversation_id: str, ts: float = 0.0) -> None:
        """P36：记录「本次归档是自动归档」的时刻（人工归档不写这一列）。

        ``auto_archived_at`` 同时是 ``_auto_archive_candidates`` 的**幂等闸**——非 0 即
        永不再次入选，故坐席把自动归档的会话解档后，它不会被下一个 tick 立刻archive 回去。
        与 ``archived_at``（任何归档都写、供入站复活判据）职责不同，两列都要留。
        ⚠ 只 UPDATE 不 INSERT：调用序必须在 ``set_conv_archived`` 之后（那步已保证行存在）。
        """
        cid = str(conversation_id or "").strip()
        if not cid:
            return
        now = self._now()
        with self._lock:
            self._conn.execute(
                "UPDATE conversation_meta SET auto_archived_at=?, updated_at=? "
                "WHERE conversation_id=?",
                (float(ts or now), now, cid),
            )
            self._conn.commit()


    # ── AA1: 自定义动作 / 工作链 CRUD（Phase 37） ─────────────────────────

    def list_workflow_actions(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workflow_actions ORDER BY sort_order ASC, created_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def upsert_workflow_action(self, data: Dict[str, Any]) -> str:
        import uuid as _uuid
        action_id = str(data.get("action_id") or _uuid.uuid4())
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO workflow_actions
                   (action_id, name, action_type, config_json, icon, enabled, sort_order, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(action_id) DO UPDATE SET
                     name=excluded.name, action_type=excluded.action_type,
                     config_json=excluded.config_json, icon=excluded.icon,
                     enabled=excluded.enabled, sort_order=excluded.sort_order,
                     updated_at=excluded.updated_at""",
                (
                    action_id,
                    str(data.get("name") or ""),
                    str(data.get("action_type") or "template"),
                    json.dumps(data.get("config") or {}, ensure_ascii=False),
                    str(data.get("icon") or "💡"),
                    1 if data.get("enabled", True) else 0,
                    int(data.get("sort_order") or 0),
                    float(data.get("created_at") or now),
                    now,
                ),
            )
            self._conn.commit()
        return action_id

    def delete_workflow_action(self, action_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM workflow_actions WHERE action_id = ?", (action_id,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def list_workflow_chains(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workflow_chains ORDER BY created_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def upsert_workflow_chain(self, data: Dict[str, Any]) -> str:
        import uuid as _uuid
        chain_id = str(data.get("chain_id") or _uuid.uuid4())
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO workflow_chains
                   (chain_id, name, steps_json, trigger_conditions, enabled, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(chain_id) DO UPDATE SET
                     name=excluded.name, steps_json=excluded.steps_json,
                     trigger_conditions=excluded.trigger_conditions,
                     enabled=excluded.enabled, updated_at=excluded.updated_at""",
                (
                    chain_id,
                    str(data.get("name") or ""),
                    json.dumps(data.get("steps") or [], ensure_ascii=False),
                    json.dumps(data.get("trigger_conditions") or {}, ensure_ascii=False),
                    1 if data.get("enabled", True) else 0,
                    float(data.get("created_at") or now),
                    now,
                ),
            )
            self._conn.commit()
        return chain_id

    def delete_workflow_chain(self, chain_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM workflow_chains WHERE chain_id = ?", (chain_id,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def start_chain_execution(
        self,
        chain_id: str,
        conversation_id: str,
        context: Dict[str, Any],
        *,
        schedule_first_step: bool = True,
    ) -> str:
        import uuid as _uuid
        exec_id = str(_uuid.uuid4())
        now = time.time()
        next_at = 0.0
        if schedule_first_step:
            chain = self.get_workflow_chain(chain_id)
            if chain:
                try:
                    steps = json.loads(chain.get("steps_json") or "[]")
                    if steps:
                        delay_h = float(steps[0].get("delay_hours") or 0)
                        next_at = now if delay_h <= 0 else now + delay_h * 3600
                except Exception:
                    next_at = now
            else:
                next_at = now
        with self._lock:
            self._conn.execute(
                """INSERT INTO workflow_executions
                   (exec_id, chain_id, conversation_id, current_step, status,
                    context_json, started_at, updated_at, next_step_at, last_result_json)
                   VALUES (?,?,?,0,'running',?,?,?,?, '')""",
                (exec_id, chain_id, conversation_id,
                 json.dumps(context, ensure_ascii=False), now, now, next_at),
            )
            self._conn.commit()
        return exec_id

    def get_workflow_chain(self, chain_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflow_chains WHERE chain_id = ?", (chain_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_workflow_execution(self, exec_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                """SELECT we.*, wc.name as chain_name, wc.steps_json
                   FROM workflow_executions we
                   LEFT JOIN workflow_chains wc ON wc.chain_id = we.chain_id
                   WHERE we.exec_id = ?""",
                (exec_id,),
            ).fetchone()
        return dict(row) if row else None

    def has_running_chain(self, conversation_id: str, chain_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                """SELECT 1 FROM workflow_executions
                   WHERE conversation_id = ? AND chain_id = ? AND status = 'running' LIMIT 1""",
                (conversation_id, chain_id),
            ).fetchone()
        return row is not None

    def list_due_workflow_executions(
        self, now: float, *, limit: int = 30, stuck_sec: float = 120,
    ) -> List[Dict[str, Any]]:
        """P44/P47：拉取到期应执行的工作链（含卡住恢复）。"""
        stuck_before = now - stuck_sec
        with self._lock:
            rows = self._conn.execute(
                """SELECT we.*, wc.name as chain_name, wc.steps_json
                   FROM workflow_executions we
                   LEFT JOIN workflow_chains wc ON wc.chain_id = we.chain_id
                   WHERE we.status = 'running'
                     AND (
                       (we.next_step_at > 0 AND we.next_step_at <= ?)
                       OR (we.next_step_at = 0 AND we.updated_at <= ?)
                     )
                   ORDER BY we.next_step_at ASC, we.updated_at ASC LIMIT ?""",
                (now, stuck_before, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_workflow_execution(
        self,
        exec_id: str,
        *,
        current_step: int = 0,
        next_step_at: float = 0,
        last_result: Optional[Dict[str, Any]] = None,
        status: str = "",
        context_json: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = time.time()
        sets = ["current_step = ?", "updated_at = ?", "next_step_at = ?"]
        params: List[Any] = [current_step, now, next_step_at]
        if last_result is not None:
            sets.append("last_result_json = ?")
            params.append(json.dumps(last_result, ensure_ascii=False))
        if context_json is not None:
            sets.append("context_json = ?")
            params.append(json.dumps(context_json, ensure_ascii=False))
        if status:
            sets.append("status = ?")
            params.append(status)
        params.append(exec_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE workflow_executions SET {', '.join(sets)} WHERE exec_id = ?",
                params,
            )
            self._conn.commit()

    def complete_workflow_execution(self, exec_id: str, *, status: str = "completed") -> None:
        self.update_workflow_execution(exec_id, status=status, next_step_at=0)

    def cancel_workflow_execution(self, exec_id: str) -> bool:
        """P47：取消运行中的工作链执行。"""
        ex = self.get_workflow_execution(exec_id)
        if not ex or ex.get("status") != "running":
            return False
        self.complete_workflow_execution(exec_id, status="cancelled")
        return True

    def log_workflow_step(
        self,
        *,
        exec_id: str,
        chain_id: str,
        conversation_id: str,
        step_idx: int,
        action_type: str,
        ok: bool,
        detail: str = "",
        now: Optional[float] = None,
    ) -> None:
        """工作链环节执行落账（P1 2026-08-09；每次尝试一行，绝不抛）。"""
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO workflow_step_log (exec_id, chain_id,"
                    " conversation_id, step_idx, action_type, ok, detail, ts)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (str(exec_id or ""), str(chain_id or ""),
                     str(conversation_id or ""), int(step_idx or 0),
                     str(action_type or "")[:40], 1 if ok else 0,
                     str(detail or "")[:200],
                     float(now if now is not None else time.time())),
                )
                self._conn.commit()
        except Exception:
            logger.debug("log_workflow_step failed", exc_info=True)

    def workflow_step_stats(
        self, since_ts: float, *, chain_id: str = "",
    ) -> Dict[str, Dict[str, Dict[str, int]]]:
        """窗口内每链×每环节的执行尝试聚合：{chain_id: {step_idx: {attempts,ok,failed}}}。

        「哪一环节在损耗」的读数面（monitor.chain_funnel 消费拼 by_step）。绝不抛。"""
        out: Dict[str, Dict[str, Dict[str, int]]] = {}
        sql = ("SELECT chain_id, step_idx, ok, COUNT(*) AS n"
               " FROM workflow_step_log WHERE ts >= ?")
        params: List[Any] = [float(since_ts or 0.0)]
        if chain_id:
            sql += " AND chain_id = ?"
            params.append(str(chain_id))
        sql += " GROUP BY chain_id, step_idx, ok"
        try:
            with self._lock:
                rows = self._conn.execute(sql, tuple(params)).fetchall()
            for r in rows:
                c = out.setdefault(str(r["chain_id"]), {})
                s = c.setdefault(str(int(r["step_idx"])),
                                 {"attempts": 0, "ok": 0, "failed": 0})
                n = int(r["n"])
                s["attempts"] += n
                s["ok" if int(r["ok"]) else "failed"] += n
        except Exception:
            logger.debug("workflow_step_stats failed", exc_info=True)
        return out

    def list_chain_executions(
        self,
        *,
        status: str = "",
        conversation_id: str = "",
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """P47：全局/按会话列出工作链执行记录（含会话展示名）。"""
        clauses = ["1=1"]
        params: List[Any] = []
        if status:
            clauses.append("we.status = ?")
            params.append(status)
        if conversation_id:
            clauses.append("we.conversation_id = ?")
            params.append(conversation_id)
        params.append(max(1, min(int(limit), 200)))
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT we.*, wc.name as chain_name, wc.steps_json,
                           c.display_name, c.platform
                   FROM workflow_executions we
                   LEFT JOIN workflow_chains wc ON wc.chain_id = we.chain_id
                   LEFT JOIN conversations c ON c.conversation_id = we.conversation_id
                   WHERE {' AND '.join(clauses)}
                   ORDER BY
                     CASE we.status WHEN 'running' THEN 0 WHEN 'failed' THEN 1 ELSE 2 END,
                     we.updated_at DESC
                   LIMIT ?""",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    _META_PATCH_COLUMNS = frozenset({
        "rel_stage_cached", "rel_stage_pending", "rel_stage_pending_ts",
        "rel_reunion_ack_ts", "qa_score", "churn_risk", "snooze_until",
        # 用户时区推断缓存（user_clock_resolver 按 TTL 低频写；读侧走 get_conv_meta 的 SELECT *）
        "tz_hint", "tz_confidence", "tz_source", "tz_country", "tz_offset",
        "tz_resolved_at",
    })

    def _ensure_conv_meta_row(self, conversation_id: str) -> None:
        cid = str(conversation_id or "").strip()
        if not cid:
            return
        now = self._now()
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO conversation_meta (conversation_id, updated_at)
                   VALUES (?, ?)""",
                (cid, now),
            )
            self._conn.commit()

    def patch_conv_meta(self, conversation_id: str, fields: Dict[str, Any]) -> None:
        """按列名局部更新 conversation_meta（仅允许白名单字段）。"""
        cid = str(conversation_id or "").strip()
        if not cid or not fields:
            return
        safe = {k: v for k, v in fields.items() if k in self._META_PATCH_COLUMNS}
        if not safe:
            return
        self._ensure_conv_meta_row(cid)
        now = self._now()
        sets = [f"{k}=?" for k in safe]
        vals = list(safe.values()) + [now, cid]
        with self._lock:
            self._conn.execute(
                f"UPDATE conversation_meta SET {', '.join(sets)}, updated_at=? WHERE conversation_id=?",
                vals,
            )
            self._conn.commit()

    # ── P0-companion：会话搁置（snooze）——「稍后再看」不删会话地移出待接管队列 ──────
    def set_snooze(
        self, conversation_id: str, until_ts: float, *, by: str = "",
    ) -> bool:
        """把会话搁置到 ``until_ts``（epoch 秒）。``until_ts<=now`` 视为取消搁置。

        ``until_ts >= SNOOZE_FOREVER_TS``（含 +inf，一律钉到哨兵）＝「永久搁置」：
        不再按时间重浮，但客户再来消息仍经 ingest ``clear_snooze`` 立即重浮。
        NaN 视为非法输入按 0（取消）处理——NaN 与 now 比较恒 False，不拦会被存进库。
        返回是否处于搁置态。与 automation_mode（谁来答）正交——只影响「待接管/超时告警」
        队列的可见性（读侧 ``_snoozed_set`` 排除）；到点由 ``snoozed_ids`` 的 now 过滤
        自动重浮，客户再来消息由 ingest 侧 ``clear_snooze`` 立即重浮。``by`` 暂仅审计留痕用。
        """
        cid = str(conversation_id or "").strip()
        if not cid:
            return False
        try:
            until = float(until_ts or 0)
        except (TypeError, ValueError):
            until = 0.0
        if math.isnan(until):
            until = 0.0
        elif until > SNOOZE_FOREVER_TS:
            until = SNOOZE_FOREVER_TS
        if until <= self._now():
            self.clear_snooze(cid)
            return False
        self.patch_conv_meta(cid, {"snooze_until": until})
        return True

    def clear_snooze(self, conversation_id: str) -> None:
        """取消搁置（客户回复 / 坐席手动 / 到点）。

        入站消息路径会对每条新入站调用本方法，故先只读判定：无 meta 行或本就未搁置
        → 直接返回、**不写、不凭空建 meta 行**（避免每条消息空转 + 污染 meta）。
        """
        cid = str(conversation_id or "").strip()
        if not cid:
            return
        with self._lock:
            row = self._conn.execute(
                "SELECT snooze_until FROM conversation_meta WHERE conversation_id=?",
                (cid,),
            ).fetchone()
            if row is None or float(row["snooze_until"] or 0) <= 0:
                return
            self._conn.execute(
                "UPDATE conversation_meta SET snooze_until=0, updated_at=? "
                "WHERE conversation_id=?",
                (self._now(), cid),
            )
            self._conn.commit()

    def snoozed_ids(self, now: Optional[float] = None) -> set:
        """仍在搁置窗口内（``snooze_until > now``）的会话 id 集合。

        到点后自然不再包含 = 自动重浮（无需扫表/定时任务，读时按 now 过滤即可）。
        """
        ts = float(now if now is not None else self._now())
        with self._lock:
            rows = self._conn.execute(
                "SELECT conversation_id FROM conversation_meta WHERE snooze_until > ?",
                (ts,),
            ).fetchall()
        return {str(r["conversation_id"]) for r in rows}

    def list_snoozed(
        self, *, now: Optional[float] = None, limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """搁置中会话清单（含剩余秒 + 展示字段），供「搁置中」视图/API。按到点先后排序。"""
        ts = float(now if now is not None else self._now())
        lim = max(1, min(500, int(limit or 200)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.conversation_id AS conversation_id, m.snooze_until AS snooze_until, "
                "c.platform AS platform, c.account_id AS account_id, "
                "c.chat_key AS chat_key, c.display_name AS display_name "
                "FROM conversation_meta m "
                "LEFT JOIN conversations c ON c.conversation_id = m.conversation_id "
                "WHERE m.snooze_until > ? ORDER BY m.snooze_until ASC LIMIT ?",
                (ts, lim),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            cid = str(r["conversation_id"])
            until = float(r["snooze_until"] or 0)
            out.append({
                "conversation_id": cid,
                "platform": str(r["platform"] or ""),
                "account_id": str(r["account_id"] or "default"),
                "chat_key": str(r["chat_key"] or ""),
                "name": str(r["display_name"] or r["chat_key"] or cid),
                "snooze_until": until,
                "remaining_sec": int(max(0, until - ts)),
                # 永久搁置：remaining_sec 数值巨大无展示意义，消费方按本旗标显示「永久」
                "permanent": is_permanent_snooze(until),
            })
        return out

    def snooze_counts(self, now: Optional[float] = None) -> Dict[str, int]:
        """搁置中总数与其中永久数——「沉默坟场」防线的量化读数（监督面消费）。

        单条聚合 SQL，供 escalation 快照 / 看板行等低频读；与 ``snoozed_ids`` 同一
        now 过滤语义（到点即不计入）。
        """
        ts = float(now if now is not None else self._now())
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, "
                "SUM(CASE WHEN snooze_until >= ? THEN 1 ELSE 0 END) AS p "
                "FROM conversation_meta WHERE snooze_until > ?",
                (SNOOZE_FOREVER_TS - 1.0, ts),
            ).fetchone()
        return {"total": int(row["n"] or 0), "permanent": int(row["p"] or 0)}

    def get_rel_stage_cached(self, conversation_id: str) -> str:
        meta = self.get_rel_stage_meta(conversation_id)
        return meta["confirmed"]

    def get_rel_stage_meta(self, conversation_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                """SELECT rel_stage_cached, rel_stage_pending, rel_stage_pending_ts,
                          rel_reunion_ack_ts
                   FROM conversation_meta WHERE conversation_id = ?""",
                (conversation_id,),
            ).fetchone()
        if not row:
            return {
                "confirmed": "", "pending": "", "pending_ts": 0.0, "reunion_ack_ts": 0.0,
            }
        return {
            "confirmed": str(row["rel_stage_cached"] or ""),
            "pending": str(row["rel_stage_pending"] or ""),
            "pending_ts": float(row["rel_stage_pending_ts"] or 0),
            "reunion_ack_ts": float(row["rel_reunion_ack_ts"] or 0),
        }

    def set_rel_stage_cached(self, conversation_id: str, stage: str) -> None:
        self.patch_conv_meta(conversation_id, {"rel_stage_cached": str(stage or "")})

    def set_rel_stage_pending(self, conversation_id: str, stage: str, *, ts: Optional[float] = None) -> None:
        now = float(ts if ts is not None else self._now())
        self.patch_conv_meta(conversation_id, {
            "rel_stage_pending": str(stage or ""),
            "rel_stage_pending_ts": now,
        })

    def clear_rel_stage_pending(self, conversation_id: str) -> None:
        self.patch_conv_meta(conversation_id, {
            "rel_stage_pending": "",
            "rel_stage_pending_ts": 0,
        })

    def confirm_rel_stage(self, conversation_id: str, stage: str) -> None:
        self.patch_conv_meta(conversation_id, {
            "rel_stage_cached": str(stage or ""),
            "rel_stage_pending": "",
            "rel_stage_pending_ts": 0,
        })

    # ── P50: 客户级关系阶段 ───────────────────────────────────────────────

    def get_contact_rel_stage(self, contact_id: str) -> Optional[Dict[str, Any]]:
        cid = str(contact_id or "").strip()
        if not cid:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM contact_rel_stage WHERE contact_id = ?", (cid,),
            ).fetchone()
        if not row:
            return None
        return dict(row)

    def set_contact_rel_stage(
        self,
        contact_id: str,
        stage: str,
        *,
        updated_by: str = "",
        reunion_ack_ts: Optional[float] = None,
    ) -> None:
        cid = str(contact_id or "").strip()
        if not cid:
            return
        now = self._now()
        ack = float(reunion_ack_ts) if reunion_ack_ts is not None else None
        with self._lock:
            if ack is not None:
                self._conn.execute(
                    """INSERT INTO contact_rel_stage
                       (contact_id, confirmed_stage, updated_by, updated_at, reunion_ack_ts)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(contact_id) DO UPDATE SET
                         confirmed_stage = excluded.confirmed_stage,
                         updated_by = excluded.updated_by,
                         updated_at = excluded.updated_at,
                         reunion_ack_ts = excluded.reunion_ack_ts""",
                    (cid, str(stage or ""), str(updated_by or ""), now, ack),
                )
            else:
                self._conn.execute(
                    """INSERT INTO contact_rel_stage
                       (contact_id, confirmed_stage, updated_by, updated_at, reunion_ack_ts)
                       VALUES (?,?,?,?,0)
                       ON CONFLICT(contact_id) DO UPDATE SET
                         confirmed_stage = excluded.confirmed_stage,
                         updated_by = excluded.updated_by,
                         updated_at = excluded.updated_at""",
                    (cid, str(stage or ""), str(updated_by or ""), now),
                )
            self._conn.commit()

    def list_conv_rel_stages_for_contact(self, contact_id: str) -> Dict[str, str]:
        """返回该客户各会话的已确认阶段。"""
        cid = str(contact_id or "").strip()
        if not cid:
            return {}
        with self._lock:
            rows = self._conn.execute(
                """SELECT c.conversation_id, COALESCE(cm.rel_stage_cached, '') AS stage
                   FROM conversations c
                   LEFT JOIN conversation_meta cm ON cm.conversation_id = c.conversation_id
                   WHERE c.contact_id = ?""",
                (cid,),
            ).fetchall()
        return {str(r["conversation_id"]): str(r["stage"] or "") for r in rows}

    def sync_convs_to_stage(self, contact_id: str, stage: str) -> int:
        """P50：将该客户所有会话的确认阶段对齐为 stage，返回更新条数。"""
        cid = str(contact_id or "").strip()
        st = str(stage or "")
        if not cid or not st:
            return 0
        conv_ids = list(self.list_conv_rel_stages_for_contact(cid).keys())
        n = 0
        for conv_id in conv_ids:
            self.confirm_rel_stage(conv_id, st)
            n += 1
        return n

    def list_contact_stage_audits(
        self, contact_id: str, *, limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """P51：查询客户级关系阶段审计事件（跨会话聚合）。"""
        cid = str(contact_id or "").strip()
        if not cid:
            return []
        lim = max(1, min(200, int(limit or 50)))
        actions = (
            "stage_confirm", "stage_downgrade", "stage_reunion", "stage_sync",
        )
        ph_act = ",".join("?" * len(actions))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT dal.*, c.platform, c.display_name
                FROM draft_audit_log dal
                LEFT JOIN conversations c ON c.conversation_id = dal.conversation_id
                WHERE (
                    dal.conversation_id IN (
                        SELECT conversation_id FROM conversations WHERE contact_id = ?
                    )
                    OR dal.draft_id = ?
                )
                AND dal.action IN ({ph_act})
                ORDER BY dal.ts DESC
                LIMIT ?
                """,
                (cid, f"contact:{cid}", *actions, lim),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── P54: Copilot 采纳率统计 ─────────────────────────────────────────

    def record_copilot_impression(
        self,
        conversation_id: str,
        agent_id: str,
        *,
        trigger: str = "",
        stage: str = "",
        polished: bool = False,
        suggestion_count: int = 0,
        top_source: str = "",
    ) -> None:
        from src.inbox.copilot_stats import encode_impression
        self.record_draft_audit(
            "", action="copilot_impression", agent_id=agent_id,
            reason=encode_impression(
                trigger=trigger, stage=stage, polished=polished,
                suggestion_count=suggestion_count, top_source=top_source,
            ),
            conversation_id=conversation_id,
        )

    def record_copilot_adopt(
        self,
        conversation_id: str,
        agent_id: str,
        *,
        match: str = "exact",
        source: str = "",
        polished: bool = False,
        trigger: str = "",
        stage: str = "",
        suggested_preview: str = "",
        sent_preview: str = "",
    ) -> None:
        from src.inbox.copilot_stats import encode_adopt
        self.record_draft_audit(
            "", action="copilot_adopt", agent_id=agent_id,
            reason=encode_adopt(
                match=match, source=source, polished=polished,
                trigger=trigger, stage=stage,
                suggested_preview=suggested_preview, sent_preview=sent_preview,
            ),
            conversation_id=conversation_id,
        )

    def list_copilot_audit_rows(
        self, *, since_ts: float = 0.0, agent_id: str = "", limit: int = 2000,
    ) -> List[Dict[str, Any]]:
        actions = ("copilot_impression", "copilot_adopt", "copilot_polish")
        ph = ",".join("?" * len(actions))
        clauses = [f"action IN ({ph})", "ts>=?"]
        params: List[Any] = [*actions, float(since_ts)]
        if agent_id:
            clauses.append("agent_id=?")
            params.append(str(agent_id))
        sql = (
            f"SELECT * FROM draft_audit_log WHERE {' AND '.join(clauses)} "
            f"ORDER BY ts DESC LIMIT ?"
        )
        params.append(max(1, min(5000, int(limit))))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_copilot_stats(
        self, *, since_ts: float = 0.0, agent_id: str = "",
    ) -> Dict[str, Any]:
        from src.inbox.copilot_stats import aggregate_copilot_stats
        rows = self.list_copilot_audit_rows(
            since_ts=since_ts, agent_id=agent_id, limit=3000,
        )
        return aggregate_copilot_stats(rows)

    def confirm_rel_stage_with_contact(
        self,
        conversation_id: str,
        contact_id: str,
        stage: str,
        *,
        updated_by: str = "",
        sync_all_convs: bool = True,
    ) -> None:
        """P50：确认会话阶段并同步客户级（可选同步全部会话）。"""
        self.confirm_rel_stage(conversation_id, stage)
        cid = str(contact_id or "").strip()
        if not cid:
            return
        self.set_contact_rel_stage(cid, stage, updated_by=updated_by)
        if sync_all_convs:
            self.sync_convs_to_stage(cid, stage)

    def ack_rel_reunion(self, conversation_id: str, *, ts: Optional[float] = None) -> None:
        now = float(ts if ts is not None else self._now())
        self.patch_conv_meta(conversation_id, {"rel_reunion_ack_ts": now})

    def get_agent_stage_confirm_counts(
        self, *, since_ts: float = 0.0,
    ) -> Dict[str, Dict[str, int]]:
        """P48：统计各坐席确认过的关系阶段次数（来自 stage_confirm 审计）。"""
        from src.utils.companion_relationship import STAGE_ORDER
        stage_set = set(STAGE_ORDER)
        out: Dict[str, Dict[str, int]] = {}
        with self._lock:
            rows = self._conn.execute(
                """SELECT agent_id, reason FROM draft_audit_log
                   WHERE ts >= ? AND action = 'stage_confirm' AND agent_id != ''""",
                (float(since_ts),),
            ).fetchall()
        for row in rows:
            aid = str(row["agent_id"] or "").strip()
            reason = str(row["reason"] or "")
            if not aid or "→" not in reason:
                continue
            target = reason.split("→")[-1].strip()
            stage_id = target if target in stage_set else ""
            if not stage_id:
                from src.utils.companion_relationship import STAGE_LABEL_ZH
                for sid in stage_set:
                    if target == STAGE_LABEL_ZH.get(sid, sid):
                        stage_id = sid
                        break
            if stage_id not in stage_set:
                continue
            out.setdefault(aid, {})
            out[aid][stage_id] = out[aid].get(stage_id, 0) + 1
        return out

    def get_agent_mention_counts(self, *, since_ts: float = 0.0) -> Dict[str, int]:
        """P48：统计各坐席被 @ 次数（协作活跃度代理）。"""
        counts: Dict[str, int] = {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT mentions FROM conv_notes WHERE ts >= ?",
                (float(since_ts),),
            ).fetchall()
        for row in rows:
            try:
                mentions = json.loads(row["mentions"] or "[]")
            except Exception:
                mentions = []
            for m in mentions:
                aid = str(m or "").strip()
                if aid:
                    counts[aid] = counts.get(aid, 0) + 1
        return counts

    def get_recent_mention_note(
        self,
        conversation_id: str,
        agent_id: str,
        *,
        within_hours: float = 48,
    ) -> Optional[Dict[str, Any]]:
        """P49：获取最近 @ 指定坐席的协作注解。"""
        cid = str(conversation_id or "").strip()
        aid = str(agent_id or "").strip()
        if not cid or not aid:
            return None
        cutoff = time.time() - max(1.0, float(within_hours)) * 3600
        with self._lock:
            rows = self._conn.execute(
                """SELECT note_id, agent_id, agent_name, body, mentions, ts
                   FROM conv_notes
                   WHERE conversation_id = ? AND ts >= ?
                   ORDER BY ts DESC LIMIT 30""",
                (cid, cutoff),
            ).fetchall()
        for row in rows:
            try:
                mentions = json.loads(row["mentions"] or "[]")
            except Exception:
                mentions = []
            if aid in [str(m) for m in mentions]:
                return {
                    "note_id": row["note_id"],
                    "body": row["body"],
                    "agent_id": row["agent_id"],
                    "agent_name": row["agent_name"],
                    "ts": float(row["ts"] or 0),
                }
        return None

    def has_overdue_chain_execution(
        self, conversation_id: str, *, overdue_sec: float = 3600,
    ) -> bool:
        """P48：会话是否有超时未推进的运行中工作链。"""
        cutoff = time.time() - max(60.0, float(overdue_sec))
        with self._lock:
            row = self._conn.execute(
                """SELECT 1 FROM workflow_executions
                   WHERE conversation_id = ? AND status = 'running'
                     AND next_step_at > 0 AND next_step_at < ?
                   LIMIT 1""",
                (conversation_id, cutoff),
            ).fetchone()
        return row is not None

    def get_conv_chain_executions(self, conversation_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT we.*, wc.name as chain_name
                   FROM workflow_executions we
                   LEFT JOIN workflow_chains wc ON wc.chain_id = we.chain_id
                   WHERE we.conversation_id = ?
                   ORDER BY we.started_at DESC LIMIT 20""",
                (conversation_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── BB1: 分流路由规则 CRUD（Phase 38） ────────────────────────────────

    def list_routing_rules(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM routing_rules ORDER BY priority DESC, created_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def upsert_routing_rule(self, data: Dict[str, Any]) -> str:
        import uuid as _uuid
        rule_id = str(data.get("rule_id") or _uuid.uuid4())
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO routing_rules
                   (rule_id, name, conditions, assign_to, priority, enabled, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(rule_id) DO UPDATE SET
                     name=excluded.name, conditions=excluded.conditions,
                     assign_to=excluded.assign_to, priority=excluded.priority,
                     enabled=excluded.enabled, updated_at=excluded.updated_at""",
                (
                    rule_id,
                    str(data.get("name") or ""),
                    json.dumps(data.get("conditions") or {}, ensure_ascii=False),
                    str(data.get("assign_to") or ""),
                    int(data.get("priority") or 0),
                    1 if data.get("enabled", True) else 0,
                    float(data.get("created_at") or now),
                    now,
                ),
            )
            self._conn.commit()
        return rule_id

    def delete_routing_rule(self, rule_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM routing_rules WHERE rule_id = ?", (rule_id,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def get_messages_for_contact(self, contact_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        """P41：拉取客户所有会话的消息（跨会话聚合）。"""
        cid = str(contact_id or "").strip()
        if not cid:
            return []
        with self._lock:
            rows = self._conn.execute(
                """SELECT m.conversation_id, m.direction, m.text, m.ts
                   FROM messages m
                   JOIN conversations c ON c.conversation_id = m.conversation_id
                   WHERE c.contact_id = ?
                   ORDER BY m.ts ASC LIMIT ?""",
                (cid, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def compute_and_store_engagement(self, contact_id: str) -> Dict[str, Any]:
        """P41：计算并持久化客户互动积分。"""
        from src.inbox.engagement_scorer import EngagementScorer
        cid = str(contact_id or "").strip()
        if not cid:
            return {}

        existing_ach: List[str] = []
        prev_points = 0
        with self._lock:
            row = self._conn.execute(
                "SELECT points, achievements_json, history_json FROM contact_engagement WHERE contact_id = ?",
                (cid,),
            ).fetchone()
        if row:
            prev_points = int(row["points"] or 0)
            try:
                existing_ach = json.loads(row["achievements_json"] or "[]")
            except Exception:
                existing_ach = []

        messages = self.get_messages_for_contact(cid)
        # 沉默天数
        silence_days = 0.0
        if messages:
            inbound = [m for m in messages if m.get("direction") in ("in", "inbound")]
            if inbound:
                last_ts = max(float(m.get("ts") or 0) for m in inbound)
                silence_days = max(0.0, (time.time() - last_ts) / 86400)

        result = EngagementScorer().compute(
            messages,
            existing_achievements=existing_ach,
            last_silence_days=silence_days,
        )

        # 历史快照（保留最近 30 条）
        history: List[Dict[str, Any]] = []
        if row:
            try:
                history = json.loads(row["history_json"] or "[]")
            except Exception:
                history = []
        history.append({"ts": time.time(), "points": result["points"]})
        history = history[-30:]

        with self._lock:
            self._conn.execute(
                """INSERT INTO contact_engagement
                   (contact_id, points, level, breakdown_json, achievements_json, history_json, updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(contact_id) DO UPDATE SET
                     points=excluded.points, level=excluded.level,
                     breakdown_json=excluded.breakdown_json,
                     achievements_json=excluded.achievements_json,
                     history_json=excluded.history_json,
                     updated_at=excluded.updated_at""",
                (
                    cid,
                    int(result["points"]),
                    str(result["level"]),
                    json.dumps(result["breakdown"], ensure_ascii=False),
                    json.dumps(result["achievements"], ensure_ascii=False),
                    json.dumps(history, ensure_ascii=False),
                    time.time(),
                ),
            )
            self._conn.commit()
        result["previous_points"] = prev_points
        result["history"] = history
        return result

    def get_contact_engagement(self, contact_id: str) -> Optional[Dict[str, Any]]:
        cid = str(contact_id or "").strip()
        if not cid:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM contact_engagement WHERE contact_id = ?", (cid,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["breakdown"] = json.loads(d.pop("breakdown_json", "{}") or "{}")
        except Exception:
            d["breakdown"] = {}
        try:
            d["achievements"] = json.loads(d.pop("achievements_json", "[]") or "[]")
        except Exception:
            d["achievements"] = []
        try:
            d["history"] = json.loads(d.pop("history_json", "[]") or "[]")
        except Exception:
            d["history"] = []
        return d


def QAScorer_grade(score: int) -> str:
    """模块级辅助：直接从分数得等级（避免重复实例化）。"""
    if score >= 90: return "A"
    if score >= 75: return "B"
    if score >= 60: return "C"
    if score >= 45: return "D"
    return "F"
