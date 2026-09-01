# -*- coding: utf-8 -*-
"""迁移包导出（账号资产保全 · 实施47 §5「工单 2」，2026-08-28）。

**为什么存在**：`export-history` 给的是 JSONL 对话流水（"读得到"），但封号后运营
真正要做的是**把人加回新号**——那需要「谁 + 用什么句柄能加回 + 在 CRM 里是什么
阶段」的清单，而这三样数据分别住在 `protocol_contacts`、`conversations` 身份列、
contacts 子系统里。迁移包把它们并成一份可离线交付的 zip。

`/api/accounts/{p}/{a}/export-migration` 端点自 2026-08-19 起就被资产中心页面用
`features.export_migration` 探测（未装载即不渲染 CTA），但端点本体一直没交付——
封禁卡上那句「迁移包导出待上线」挂了 9 天。本模块就是那张空头支票的兑付。

## 包结构（schema_version = 1）

```
manifest.json        体量/封禁信息/各成员 sha256/生成器版本
contacts.jsonl       「人」清单（三源合并，每行一个人）
contacts.csv         同上的运营友好投影（UTF-8 BOM，Excel 直开）
conversations.jsonl  逐字复用 export-history 的 schema=1 白名单
media/…              可选（默认不打包）
media_index.jsonl    message_id → 包内路径/大小/sha256/missing
README.txt           人话说明书 zh/en
```

## 五条设计红线（改之前先读）

1. **manifest 最后写、但按名读**。manifest 里的 `counts` 必须是**真正写进包里的
   行数**而不是事先查的预估——那正是「导出完整率」这个指标的意义所在（预估对不上
   实际就是数据有问题，不能让 manifest 自己把差异抹平）。zip 成员无序，读方一律
   `zf.read("manifest.json")`，写在最后不影响任何消费方。`store_counts` 同时留在
   manifest 里，两个数字并列＝差异可见。
2. **零 store.py 改动**。`conversations` 的身份列（username/phone/avatar_url/
   first_seen）没有现成的 store 方法，本模块用**只读 URI 连接**自取（与
   `asset_center_routes._reachability_map` 同一手法、同一理由：store.py 是多线共享
   热文件，为一条导出链给它加方法不值得）。只发 SELECT，绝不写。
3. **媒体默认不打包**。生产实测协议媒体 1400+ 文件 / 70MB+，默认打进去会让「先备份
   再删」这个动作从秒级变成分钟级、还可能撑爆磁盘。要媒体显式传 `include_media`，
   且有**双重预算闸**（总字节 + 文件数），超了如实记进 manifest 而不是静默截断。
4. **媒体路径必须过容纳检查**。`media_ref` 来自入站数据，理论上可被投毒成
   `/static/protocol_media/../../config/config.yaml`。解析用
   `protocol_bridge.static_media_ref_to_path`（双根语义），随后**必须**再对
   `protocol_media_roots()` 做 `os.path.commonpath` 容纳校验——容纳检查也用双根，
   否则旧根命中会被自己的守卫当穿越拒掉（比找不到更难查，见该函数 docstring）。
5b. **两个被实测否决的「刻意不做」**（2026-08-28 生产实测，别再重做这两轮分析）：

   - **导出不改异步任务**。本机最大账号 `telegram:8244899900`（74 会话 / 4097 条
     消息 / 419 个媒体文件）实测：不带媒体 **1.2s / 0.30MB**，带媒体
     **4.0s / 24.85MB**（两轮各测两次，方差 <5%）。4 秒是普通 HTTP 请求的量级，
     为它建任务表 + 轮询 + 限时链接是纯复杂度。**重新评估的触发条件**：单账号
     媒体超 ~500MB 或导出耗时稳定 >30s（媒体总量可从 assets/summary 的
     `media_bytes` 读，无需另加观测）。
   - **不打包时不为媒体算 sha256**。`media_index` 的 `sha256` 只在真打包时写。
     补齐媒体时想校验完整性确实用得上，但代价是默认路径要多读一遍全部媒体
     （上面那个号 = 25MB），把最常用的 1.2s 拖成 2~3s，换一个极少用的字段。
     `size` + `missing` 已给出弱完整性信号；真要强校验就带 `?media=1` 导一次。

5. **可加回性口径单一**：`reachability_of()` 是唯一判定，`contacts.jsonl` 的
   `reachability` 字段、CSV 那一列、manifest 的 `reachability` 汇总全部由它派生。
   资产中心卡片上的堆叠条是**另一套 SQL 聚合**（按会话档案分桶），两者口径必须能
   对上：那边分桶键是 both/username_only/phone_only/none，这里是 both/username/
   phone/none —— 命名差异刻意保留（实施47 §5 契约已定），但语义一一对应。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import sqlite3
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: 包格式版本。字段**只增不改**（下游导入器按字段名读）。
SCHEMA_VERSION = 1

#: 会话/消息字段白名单——**必须与 `export-history` 路由里的两个同名元组逐字一致**。
#: 刻意不 import 那个 5000+ 行的路由模块（测试装配不该为两个元组背上它的全部依赖），
#: 漂移由 `tests/test_migration_export.py::test_field_whitelists_match_export_history`
#: 静态比对源码钉住——它一红就说明有人只改了一侧。
CONV_FIELDS: Tuple[str, ...] = (
    "conversation_id", "chat_key", "display_name", "username", "phone",
    "last_text", "last_ts", "unread", "archived", "automation_mode",
    "funnel_stage",
)
MSG_FIELDS: Tuple[str, ...] = (
    "message_id", "platform_msg_id", "direction", "text", "original_text",
    "translated_text", "source_lang", "target_lang", "media_type",
    "media_ref", "ts", "deleted_at",
)

#: CSV 列序（运营视角：先「这是谁」再「怎么加回」再「值不值得先加」）。
CONTACT_CSV_HEADER: Tuple[str, ...] = (
    "display_name", "chat_key", "username", "phone", "reachability",
    "handle_source", "in_book", "has_conversation", "never_spoke", "unread",
    "last_ts_iso", "first_seen_iso", "funnel_stage", "intimacy_score",
    "tags", "contact_id",
)

#: 媒体打包预算（超出如实记账，绝不静默截断）。
DEFAULT_MEDIA_BUDGET_BYTES = 512 * 1024 * 1024
DEFAULT_MEDIA_MAX_FILES = 20000

#: 联系人清单上限，与 `store.list_protocol_contacts_enriched` 的硬上限同值。
CONTACTS_HARD_LIMIT = 5000

#: 「chat_key 本身就是手机号」的平台白名单。**必须逐平台确认，绝不能按形态猜**：
#: 2026-08-28 生产实测 —— WhatsApp 的 chat_key 是裸 E.164（`639273815533`，协议层
#: 就是用号码寻址的），而 Telegram 的 chat_key 是 10 位数字**用户 id**
#: （`8506426282`），形态上与手机号无从区分。按形态派生会把 155 个 TG 会话全部
#: 误标成「有手机号可加回」——那是比少报更坏的错（运营照单去加，全部加不上）。
#: LINE 的 chat_key 是 44 字 MID、Messenger 是 16 位 FB 数字 id，两者都**不能**
#: 用来重新加好友，故不在白名单里，覆盖率如实为 0。
CHAT_KEY_IS_PHONE_PLATFORMS = frozenset({"whatsapp"})

#: E.164 合理长度区间（含国家码，不含 `+`）。
PHONE_MIN_DIGITS, PHONE_MAX_DIGITS = 7, 15

# 旧私有名保留为别名：本模块内部与既有测试都在用，改名不值得动一片。
_CHAT_KEY_IS_PHONE_PLATFORMS = CHAT_KEY_IS_PHONE_PLATFORMS
_PHONE_MIN_DIGITS, _PHONE_MAX_DIGITS = PHONE_MIN_DIGITS, PHONE_MAX_DIGITS

_MEMBER_MANIFEST = "manifest.json"
_MEMBER_CONTACTS_JSONL = "contacts.jsonl"
_MEMBER_CONTACTS_CSV = "contacts.csv"
_MEMBER_CONVERSATIONS = "conversations.jsonl"
_MEMBER_MEDIA_INDEX = "media_index.jsonl"
_MEMBER_README = "README.txt"


# ── 纯函数（无 I/O，全部可单测）────────────────────────────────────────────────

def reachability_of(username: Any, phone: Any) -> str:
    """可加回句柄档位：``both`` / ``username`` / ``phone`` / ``none``。

    「可加回」= 在**新号**上能不能把这个人重新找到。username 与手机号是两种平台
    级寻址句柄，两者皆无就只能靠人工回忆或跨渠道锚点——那正是 `none` 桶的运营
    含义（迁移向导里会被折叠进「不可触达」并给出解释）。
    """
    u = str(username or "").strip()
    p = str(phone or "").strip()
    if u and p:
        return "both"
    if u:
        return "username"
    if p:
        return "phone"
    return "none"


def derive_handles(platform: Any, chat_key: Any, username: Any,
                   phone: Any) -> Tuple[str, str, str]:
    """补齐 ``(username, phone, 来源)``——只在**平台语义确定**时从 chat_key 派生。

    返回的第三个值是来源标记：``column``（身份列本来就有）/ ``chat_key``（由
    chat_key 派生）/ ``none``（两者皆无）。派生出来的号码与身份列里的号码在
    「能不能加回」这件事上完全等价，所以计入覆盖率是**修正低报**而不是注水——
    但来源必须留痕，否则日后没人说得清那个百分比是怎么算出来的。

    生产实测（2026-08-28，zhiliao 实例）：WhatsApp 47 条私聊里只有 11 条填了
    `phone` 身份列，而**全部** 47 条的 chat_key 都是裸手机号；同一账号
    `protocol_contacts` 里另有 234 条同样形态。不派生 → 该账号 165 个联系人只有
    6% 被算作「可加回」，而真实可加回率接近 100%。WhatsApp 是本机联系人最多的
    平台，这一条直接决定了「可加回覆盖率」这个对外指标可不可信。

    白名单之外的平台一律不派生（见 `_CHAT_KEY_IS_PHONE_PLATFORMS` 的说明——
    Telegram 的数字 id 与手机号形态无从区分，按形态猜会全盘误标）。
    """
    u = str(username or "").strip()
    p = str(phone or "").strip()
    if u or p:
        return u, p, "column"
    plat = str(platform or "").strip().lower()
    if plat in _CHAT_KEY_IS_PHONE_PLATFORMS:
        ck = str(chat_key or "").strip().lstrip("+")
        if (ck.isdigit()
                and _PHONE_MIN_DIGITS <= len(ck) <= _PHONE_MAX_DIGITS):
            return "", ck, "chat_key"
    return "", "", "none"


def derived_phone_sql(*, platform: Optional[str] = None,
                      platform_col: str = "platform",
                      username_col: str = "username",
                      phone_col: str = "phone",
                      chat_key_col: str = "chat_key") -> str:
    """返回一段 SQL 表达式：该行的**有效手机号**（含 chat_key 派生），无则空串。

    ``platform`` 给定（单账号查询，调用方已知平台）→ **在 Python 侧**决定要不要
    发派生分支，SQL 里不再判平台，也就不必把平台名拼进 SQL；``platform=None``
    （跨账号 GROUP BY）→ 用 ``platform_col`` 在 SQL 里判。两条路同一份白名单。

    **为什么这里出 SQL**：可加回口径有两个消费面——迁移包逐行走
    `derive_handles()`（Python），资产中心卡片走一条 GROUP BY 聚合（SQL，几万行
    会话不可能拉进内存逐行算）。两边各写一套规则就是本仓明令禁止的
    「两个消费面各算一套」（卡片说 6%、包说 100%，坐席不知道信谁）。所以**规则
    的两种投影都从这里出**，白名单与位数区间是同一组常量；行为等价另有门禁
    `test_reachability_single_source.py` 用同一批数据跑两条路径对账。

    与 `derive_handles` 严格同语义：
    - 身份列**任一**非空 → 完全不派生（那一档是 ``column``）；
    - 仅当两列皆空、且平台在白名单、且 chat_key 去掉前导 ``+`` 后是纯数字且位数
      落在 [PHONE_MIN_DIGITS, PHONE_MAX_DIGITS] → 取该数字串。

    SQLite 判「全是数字」用 ``NOT GLOB '*[^0-9]*'``——``GLOB '[0-9]*'`` 只校验
    首字符（`8506426282abc` 会通过），这是个静默错判的坑。
    """
    ck = f"ltrim({chat_key_col}, '+')"
    head = (
        "CASE"
        f" WHEN TRIM({phone_col}) != '' THEN TRIM({phone_col})"
        f" WHEN TRIM({username_col}) != '' THEN ''"
    )
    if platform is not None:
        if str(platform).strip().lower() not in CHAT_KEY_IS_PHONE_PLATFORMS:
            # 该平台不派生：表达式退化成「只认身份列」——与 derive_handles 对
            # 非白名单平台的行为逐字一致。
            return head + " ELSE '' END"
        plat_cond = ""
    else:
        plats = ", ".join(f"'{p}'" for p in sorted(CHAT_KEY_IS_PHONE_PLATFORMS))
        plat_cond = f" LOWER({platform_col}) IN ({plats}) AND"
    return (
        head
        + " WHEN" + plat_cond
        + f" {ck} != ''"
        + f" AND {ck} NOT GLOB '*[^0-9]*'"
        + f" AND LENGTH({ck}) BETWEEN {PHONE_MIN_DIGITS}"
        + f"                      AND {PHONE_MAX_DIGITS}"
        + f" THEN {ck}"
        + " ELSE '' END"
    )


def reachability_over_union(
    db_path: Optional[Any], platform: str, account_id: str,
) -> Dict[str, int]:
    """该账号「人的并集」人群的可加回分桶（卡片与迁移包共用的**同一人群+同一规则**）。

    **为什么必须是并集人群**（2026-08-28 实测发现的真 bug）：资产中心卡片一侧
    显示「165 联系人」（`protocol_contacts_summary(include_chats=True)`＝通讯录 ∪
    私聊 peer 的并集），紧挨着显示「可加回覆盖 100%」，而那个百分比原先是按
    **私聊会话行**（9 条）算的 —— 两个数字并排摆着、看起来后者是前者的比例，
    实际分母差 156 人。全机群口径：会话分母 100/217=46.1% vs 并集分母
    345/500=69.0%，差的 283 人几乎全是 WhatsApp 通讯录联系人（229）。

    原先刻意排除通讯录-only 联系人的理由是「它们没有身份列，计入会误报无句柄」
    （实施47 §1）。这个理由**已被 WhatsApp 派生规则部分推翻**：WA 通讯录联系人的
    chat_key 就是手机号，它们是**实实在在可加回的** 229 人，排除掉才是误报。
    Telegram 的通讯录-only 联系人确实没有句柄 → 如实计入 ``none``：一个偏低但
    为真的数字，永远优于一个偏高的假数字。

    实现：复用 `store._contacts_base_from(include_chats=True)` 构造并集基表——
    那个 UNION 在本仓刻意只写一处（"不给第二份拷贝留漂移空间"），所以这里 import
    它而不是抄一份。单条聚合 SQL、走索引区间扫，与同循环里的
    `protocol_contacts_summary` 同一量级。桶名沿用卡片既有契约
    （both/username_only/phone_only/none），另给 ``derived``。
    """
    empty = {"both": 0, "username_only": 0, "phone_only": 0, "none": 0,
             "derived": 0}
    if not db_path:
        return dict(empty)
    plat = str(platform or "").lower()
    acct = str(account_id or "")
    conn = _ro_connect(Path(str(db_path)))
    if conn is None:
        return dict(empty)
    try:
        from src.inbox.store import _contacts_base_from
        from_sql, from_params, where, base_wparams = _contacts_base_from(
            plat, acct, True)
        # ⚠ 身份列**必须** COALESCE：基表是 `LEFT JOIN conversations v`，通讯录-only
        # 联系人根本没有 v 行 → `TRIM(v.username)` 是 NULL，而 `NULL = ''` 求值为
        # NULL（不是真）⇒ 四个桶的 CASE 全落 ELSE 0，**那些人会整行从分母里消失**。
        # 首版就是这么写的，`test_population_is_the_union_not_just_conversations`
        # 当场抓到（并集应 12 人、实得 9）。别把 COALESCE 摘掉。
        u_col = "COALESCE(v.username, '')"
        p_col = "COALESCE(v.phone, '')"
        eff = derived_phone_sql(platform=plat, username_col=u_col,
                                phone_col=p_col, chat_key_col="c.chat_key")
        sql = (
            "SELECT"
            f" SUM(CASE WHEN TRIM({u_col})!='' AND ({eff})!='' THEN 1 ELSE 0 END),"
            f" SUM(CASE WHEN TRIM({u_col})!='' AND ({eff})='' THEN 1 ELSE 0 END),"
            f" SUM(CASE WHEN TRIM({u_col})='' AND ({eff})!='' THEN 1 ELSE 0 END),"
            f" SUM(CASE WHEN TRIM({u_col})='' AND ({eff})='' THEN 1 ELSE 0 END),"
            f" SUM(CASE WHEN TRIM({p_col})='' AND ({eff})!='' THEN 1 ELSE 0 END)"
            + from_sql
            + ((" WHERE " + " AND ".join(where)) if where else "")
        )
        row = conn.execute(
            sql, list(from_params) + list(base_wparams)).fetchone()
        if not row:
            return dict(empty)
        return {
            "both": int(row[0] or 0),
            "username_only": int(row[1] or 0),
            "phone_only": int(row[2] or 0),
            "none": int(row[3] or 0),
            "derived": int(row[4] or 0),
        }
    except Exception:
        logger.debug("[migration_export] 并集可加回聚合失败 %s:%s", plat, acct,
                     exc_info=True)
        return dict(empty)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def reachability_rule() -> Dict[str, Any]:
    """覆盖率算法的自描述（进 manifest）：让百分比日后可复现、可质疑。"""
    return {
        "handles": ["username", "phone"],
        "derive_phone_from_chat_key": sorted(CHAT_KEY_IS_PHONE_PLATFORMS),
        "phone_digits": [PHONE_MIN_DIGITS, PHONE_MAX_DIGITS],
    }


def summarize_reachability(rows: Any) -> Dict[str, int]:
    """联系人行 → 四桶计数 + ``total`` / ``covered``（covered = 非 none）。"""
    out = {"both": 0, "username": 0, "phone": 0, "none": 0}
    total = 0
    for r in rows or []:
        total += 1
        k = str((r or {}).get("reachability") or "none")
        if k not in out:
            k = "none"
        out[k] += 1
    out["total"] = total
    out["covered"] = total - out["none"]
    return out


def safe_slug(text: Any, *, fallback: str = "account") -> str:
    """文件名安全片段（只留 ``\\w.-``）。空/全非法 → ``fallback``。"""
    s = "".join(ch if (ch.isalnum() or ch in "._-") else "_"
                for ch in str(text or ""))
    s = s.strip("._-")
    return s or fallback


def zip_filename(platform: Any, account_id: Any,
                 now: Optional[float] = None) -> str:
    """``chatx-migration_{平台}_{账号}_{yyyymmdd-HHMM}.zip``（对齐既有
    ``chatx-history_*`` 命名，运营在下载目录里能一眼分出两种包）。"""
    ts = time.localtime(time.time() if now is None else float(now))
    safe = safe_slug(f"{str(platform or '').lower()}_{account_id}")
    return f"chatx-migration_{safe}_{time.strftime('%Y%m%d-%H%M', ts)}.zip"


def _iso(ts: Any) -> str:
    """unix 秒 → 本地 ISO 串；0/空/非法 → 空串（CSV 里空格比 1970 诚实）。"""
    try:
        v = float(ts or 0)
    except (TypeError, ValueError):
        return ""
    if v <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v))
    except (OSError, OverflowError, ValueError):
        return ""


def merge_contact_row(base: Optional[Dict[str, Any]],
                      ident: Optional[Dict[str, Any]] = None,
                      crm: Optional[Dict[str, Any]] = None,
                      *, platform: str = "") -> Dict[str, Any]:
    """三源合并成一行「人」：并集基表 + conversations 身份列 + CRM（可缺）。

    - `base` = `store.list_protocol_contacts_enriched(include_chats=True)` 的行
      （chat_key/name/notify_name/in_book/has_conversation/never_spoke/last_ts/
      unread）；
    - `ident` = 同 chat_key 的会话身份列（username/phone/avatar_url/first_seen/
      language）——`reachability` 只能由它派生；
    - `crm` = contacts 子系统 best-effort（contact_id/funnel_stage/
      intimacy_score/tags/notes/follow_up_at）；子系统未启用即整块留空，**不影响
      迁移包可用性**（句柄才是加回的必要条件，CRM 只是排序依据）。

    `display_name` 取「通讯录备注名 > 平台昵称」——基表 SQL 已按该优先级归并过
    `name`，这里只补 `notify_name` 兜底，不再造第二套优先级。
    """
    b = dict(base or {})
    i = dict(ident or {})
    c = dict(crm or {})
    username, phone, handle_source = derive_handles(
        platform, b.get("chat_key"), i.get("username"), i.get("phone"))
    name = str(b.get("name") or "").strip() or str(
        b.get("notify_name") or "").strip()
    tags = c.get("tags")
    if isinstance(tags, (list, tuple)):
        tags = [str(t) for t in tags if str(t or "").strip()]
    elif tags:
        tags = [str(tags)]
    else:
        tags = []
    return {
        "chat_key": str(b.get("chat_key") or ""),
        "display_name": name,
        "notify_name": str(b.get("notify_name") or ""),
        "username": username,
        "phone": phone,
        "avatar_url": str(i.get("avatar_url") or ""),
        "language": str(i.get("language") or ""),
        "first_seen": float(i.get("first_seen") or 0),
        "in_book": bool(b.get("in_book")),
        "has_conversation": bool(b.get("has_conversation")),
        "never_spoke": bool(b.get("never_spoke")),
        "last_ts": float(b.get("last_ts") or 0),
        "unread": int(b.get("unread") or 0),
        "reachability": reachability_of(username, phone),
        # 句柄来源留痕（column / chat_key / none）：覆盖率日后可复核可质疑
        "handle_source": handle_source,
        # CRM best-effort（子系统缺席 → 全空，字段仍在，下游导入器不必分支）
        "contact_id": str(c.get("contact_id") or ""),
        "funnel_stage": str(c.get("funnel_stage") or ""),
        "intimacy_score": c.get("intimacy_score"),
        "tags": tags,
        "notes": str(c.get("notes") or ""),
        "follow_up_at": float(c.get("follow_up_at") or 0),
    }


def contact_csv_row(row: Optional[Dict[str, Any]]) -> List[str]:
    """一行「人」→ CSV 单元格（列序＝``CONTACT_CSV_HEADER``）。

    布尔渲染成 ``是/否`` 而不是 True/False：这张表的读者是运营，不是程序。
    时间渲染成本地 ISO 串（Excel 直接可排序），unix 秒留在 jsonl 里给机器。
    """
    r = dict(row or {})

    def _yn(v: Any) -> str:
        return "是" if v else "否"

    score = r.get("intimacy_score")
    return [
        str(r.get("display_name") or ""),
        str(r.get("chat_key") or ""),
        str(r.get("username") or ""),
        str(r.get("phone") or ""),
        str(r.get("reachability") or "none"),
        str(r.get("handle_source") or "none"),
        _yn(r.get("in_book")),
        _yn(r.get("has_conversation")),
        _yn(r.get("never_spoke")),
        str(int(r.get("unread") or 0)),
        _iso(r.get("last_ts")),
        _iso(r.get("first_seen")),
        str(r.get("funnel_stage") or ""),
        ("" if score is None else str(score)),
        ",".join(r.get("tags") or []),
        str(r.get("contact_id") or ""),
    ]


def media_member_path(media_ref: Any) -> str:
    """``media_ref`` → 包内相对路径（``media/...``）；非 protocol 媒体返回空串。

    只接受 ``/static/protocol_media/`` 命名空间下的 ref；随后逐段净化——空段、
    ``.``、``..`` 一律丢弃，反斜杠按分隔符处理。**这是 zip 侧的穿越防线**（磁盘
    侧另有 `_resolve_media_file` 的容纳检查）：zip 成员名带 ``..`` 会让解压方把
    文件写到包外，是 zip-slip 的标准形态。
    """
    ref = str(media_ref or "").strip()
    prefix = "/static/protocol_media/"
    if not ref.startswith(prefix):
        return ""
    rel = ref[len(prefix):].split("?", 1)[0].split("#", 1)[0]
    parts = [p for p in rel.replace("\\", "/").split("/")
             if p and p not in (".", "..")]
    if not parts:
        return ""
    return "media/" + "/".join(safe_slug(p, fallback="_") for p in parts)


def build_manifest(
    *,
    platform: Any,
    account_id: Any,
    label: Any = "",
    ban: Optional[Dict[str, Any]] = None,
    counts: Optional[Dict[str, Any]] = None,
    store_counts: Optional[Dict[str, Any]] = None,
    reachability: Optional[Dict[str, int]] = None,
    integrity: Optional[Dict[str, str]] = None,
    media_included: bool = False,
    media_notes: Optional[Dict[str, Any]] = None,
    generated_at: Optional[float] = None,
    app_version: str = "",
) -> Dict[str, Any]:
    """组装 manifest（纯函数——所有数字由调用方在写完成员后传进来）。

    `counts` = 真正写进包里的行数；`store_counts` = 事先由
    `store.count_account_data` 查的预估。两个并列摆着，对不上就是**导出完整率
    告警的原料**——刻意不在这里做「取较小值」之类的抹平。
    """
    return {
        "app": "chatx",
        "kind": "migration_kit",
        "schema_version": SCHEMA_VERSION,
        "generated_at": float(time.time() if generated_at is None
                              else generated_at),
        "platform": str(platform or "").lower(),
        "account_id": str(account_id or ""),
        "label": str(label or ""),
        "ban": dict(ban or {}),
        "counts": dict(counts or {}),
        "store_counts": dict(store_counts or {}),
        "reachability": dict(reachability or {}),
        # 覆盖率怎么算出来的（含 chat_key 派生白名单）：自描述，日后可复现
        "reachability_rule": reachability_rule(),
        "media": {"included": bool(media_included), **dict(media_notes or {})},
        "integrity": dict(integrity or {}),
        "generator": {"app": "chatx", "app_version": str(app_version or "")},
    }


def readme_text(platform: Any, account_id: Any, *,
                media_included: bool = False) -> str:
    """包内说明书（zh + en）。写给**拿到 zip 的人**，不是写给开发。"""
    plat = str(platform or "").lower()
    acct = str(account_id or "")
    media_zh = ("media/ 目录内含本账号的图片/语音等附件原件。"
                if media_included else
                "本包未包含媒体原件（导出时未勾选）。media_index.jsonl 仍列出了"
                "每条媒体消息对应的文件名，便于日后按需补齐。")
    media_en = ("The media/ folder contains the original attachments."
                if media_included else
                "Media files are NOT included in this kit (not selected at "
                "export time). media_index.jsonl still lists the expected "
                "file for every media message.")
    return "\n".join([
        "ChatX 账号迁移包 / Account Migration Kit",
        f"平台 Platform: {plat}",
        f"账号 Account: {acct}",
        f"包格式版本 Schema: {SCHEMA_VERSION}",
        "",
        "== 这是什么 ==",
        "这是该账号在本系统里沉淀的客户资产的完整离线副本。账号即使被封，",
        "这些数据仍然属于你，可以带去任何地方。",
        "",
        "contacts.csv       —— 客户清单，Excel 直接打开。最重要的一列是",
        "                      reachability（可加回句柄）：",
        "                        both     = 有用户名也有手机号，最容易加回",
        "                        username = 只有用户名",
        "                        phone    = 只有手机号",
        "                        none     = 两者皆无，需靠人工回忆或其他渠道",
        "                      handle_source 说明这个句柄是哪来的：",
        "                        column   = 系统采集到的真实身份字段",
        "                        chat_key = 由会话标识推出（WhatsApp 的会话标识",
        "                                   本身就是手机号）——同样可直接加回",
        "contacts.jsonl     —— 同一份清单的机器可读版（每行一个 JSON）。",
        "conversations.jsonl—— 全部会话与消息（每行一个 JSON）。",
        "manifest.json      —— 体量、封禁信息、各文件 sha256 校验和。",
        "media_index.jsonl  —— 媒体消息与文件的对应关系。",
        f"{media_zh}",
        "",
        "== 怎么用 ==",
        "1. 先看 contacts.csv，按 reachability 排序，从 both 开始加回；",
        "2. 新号加回客户时注意平台的每日上限，别一次加太多；",
        "3. manifest.json 里的 counts 与 store_counts 应当一致——如果不一致，",
        "   说明导出期间数据有变动，建议重新导出一次。",
        "",
        "== English ==",
        "This is a complete offline copy of the customer assets this account",
        "accumulated. The data is yours even if the account gets banned.",
        "Start from contacts.csv, sort by 'reachability', and re-add the",
        "'both' rows first. Respect the platform's daily add limits.",
        f"{media_en}",
        "Verify integrity with the sha256 values in manifest.json.",
        "",
    ])


# ── 只读取数（不改 store.py）────────────────────────────────────────────────────

def _ro_connect(db_path: Path) -> Optional[sqlite3.Connection]:
    """尽力以只读模式开库；URI 不可用时回落普通连接（本模块只发 SELECT）。

    与 `asset_center_routes._ro_connect` 同实现、刻意各自持有：那是 web 路由层，
    这是 inbox 领域层，让领域模块 import 一个路由模块的私有函数是错误的依赖方向。
    """
    try:
        uri = "file:///" + str(db_path).replace("\\", "/").lstrip("/") + "?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=5)
    except Exception:
        try:
            return sqlite3.connect(str(db_path), timeout=5)
        except Exception:
            logger.debug("[migration_export] inbox.db 连接失败", exc_info=True)
            return None


def conversation_identity_map(
    db_path: Optional[Any], platform: str, account_id: str,
) -> Dict[str, Dict[str, Any]]:
    """``chat_key`` → 该会话的身份列（username/phone/avatar_url/first_seen/language）。

    这是「可加回句柄」的**唯一数据来源**——`protocol_contacts` 表只有
    `(chat_key, name, notify_name, updated_at)`，没有任何句柄列（实施47 §1 已核实
    的地雷）。极老库缺这些迁移列时整体返回空 dict：那样每个人都会落进 `none` 桶，
    覆盖率显示为 0% 而不是崩掉——**宁可诚实地报 0，不可假装有句柄**。
    """
    if not db_path:
        return {}
    conn = _ro_connect(Path(str(db_path)))
    if conn is None:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    try:
        rows = conn.execute(
            "SELECT chat_key, username, phone, avatar_url, first_seen, language"
            " FROM conversations WHERE platform=? AND account_id=?"
            "   AND chat_key != ''",
            (str(platform or "").lower(), str(account_id or "")),
        ).fetchall()
        for r in rows:
            out[str(r[0])] = {
                "username": str(r[1] or ""),
                "phone": str(r[2] or ""),
                "avatar_url": str(r[3] or ""),
                "first_seen": float(r[4] or 0),
                "language": str(r[5] or ""),
            }
    except Exception:
        logger.debug("[migration_export] 身份列查询失败（按无句柄导出）",
                     exc_info=True)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


def collect_contacts(
    store: Any, platform: str, account_id: str, *,
    db_path: Optional[Any] = None,
    crm_lookup: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
    limit: int = CONTACTS_HARD_LIMIT,
) -> List[Dict[str, Any]]:
    """「人的并集」清单 + 身份列 + CRM → 迁移包用的联系人行列表。

    并集口径（`include_chats=True`）是硬要求：Telegram 的 `get_contacts()` 只算
    「我主动存过的联系人」，生产实测三个真号通讯录全为 0 而真实会话 62 条——纯
    通讯录口径会让 TG 账号的迁移包联系人恒空（实施47 §1 实证）。
    """
    plat = str(platform or "").lower()
    acct = str(account_id or "")
    try:
        base_rows = store.list_protocol_contacts_enriched(
            plat, acct, limit=max(1, min(CONTACTS_HARD_LIMIT, int(limit or 0))),
            include_chats=True) or []
    except Exception:
        logger.warning("[migration_export] 联系人并集取数失败 %s:%s",
                       plat, acct, exc_info=True)
        base_rows = []
    if db_path is None:
        db_path = getattr(store, "_db_path", None)
    ident = conversation_identity_map(db_path, plat, acct)
    out: List[Dict[str, Any]] = []
    for b in base_rows:
        ck = str((b or {}).get("chat_key") or "")
        crm: Optional[Dict[str, Any]] = None
        if crm_lookup is not None and ck:
            try:
                crm = crm_lookup(ck)
            except Exception:
                logger.debug("[migration_export] CRM 查询失败 ck=%s", ck,
                             exc_info=True)
                crm = None
        out.append(merge_contact_row(b, ident.get(ck), crm, platform=plat))
    return out


def _resolve_media_file(media_ref: str) -> Optional[str]:
    """``media_ref`` → 本地可读绝对路径；非 protocol / 不存在 / 越界 → None。

    两道闸：`static_media_ref_to_path` 负责双根解析（主根优先、旧根兜底），随后
    **必须**对 `protocol_media_roots()` 做容纳校验。容纳检查也用双根——只用主根
    会把「旧根命中」误判成穿越（比找不到更难查，protocol_bridge 里有同款警告）。
    """
    try:
        from src.integrations.protocol_bridge import (
            protocol_media_roots, static_media_ref_to_path,
        )
    except Exception:
        logger.debug("[migration_export] protocol_bridge 不可用（跳过媒体）",
                     exc_info=True)
        return None
    try:
        path = static_media_ref_to_path(media_ref)
    except Exception:
        return None
    if not path:
        return None
    try:
        real = os.path.realpath(path)
        if not os.path.isfile(real):
            return None
        for root in protocol_media_roots():
            try:
                root_real = os.path.realpath(str(root))
                if os.path.commonpath([real, root_real]) == root_real:
                    return real
            except (OSError, ValueError):
                continue
    except OSError:
        return None
    logger.warning("[migration_export] 媒体路径越界，已拒绝：%r", media_ref)
    return None


# ── 打包 ──────────────────────────────────────────────────────────────────────

@dataclass
class MigrationKit:
    """一次导出的产物与账目。"""

    path: str
    filename: str
    counts: Dict[str, int] = field(default_factory=dict)
    store_counts: Dict[str, int] = field(default_factory=dict)
    reachability: Dict[str, int] = field(default_factory=dict)
    manifest: Dict[str, Any] = field(default_factory=dict)

    @property
    def size_bytes(self) -> int:
        try:
            return int(os.path.getsize(self.path))
        except OSError:
            return 0

    @property
    def reconciled(self) -> bool:
        """写进包的行数是否与事先预估一致（导出完整率）。"""
        for k in ("conversations", "messages"):
            if int(self.counts.get(k) or 0) != int(
                    self.store_counts.get(k) or 0):
                return False
        return True


class _HashingWriter:
    """往 zip 成员里写字符串块，同时算 sha256（避免二次读盘）。"""

    def __init__(self, zf: zipfile.ZipFile, name: str) -> None:
        self._fh = zf.open(name, "w")
        self._h = hashlib.sha256()

    def write(self, chunk: str) -> None:
        data = chunk.encode("utf-8")
        self._h.update(data)
        self._fh.write(data)

    def close(self) -> str:
        try:
            self._fh.close()
        except Exception:
            logger.debug("[migration_export] 成员关闭失败", exc_info=True)
        return self._h.hexdigest()


def _jsonl(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str) + "\n"


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(blk)
    return h.hexdigest()


def build_migration_kit(
    store: Any, platform: str, account_id: str, *,
    label: str = "",
    ban: Optional[Dict[str, Any]] = None,
    include_media: bool = False,
    crm_lookup: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
    out_dir: Optional[Any] = None,
    app_version: str = "",
    media_budget_bytes: int = DEFAULT_MEDIA_BUDGET_BYTES,
    media_max_files: int = DEFAULT_MEDIA_MAX_FILES,
    now: Optional[float] = None,
) -> MigrationKit:
    """把一个账号的资产打成迁移包 zip，返回落盘路径与账目。

    落盘到临时文件再由路由 `FileResponse` 流出（**刻意不做生成器流式 zip**）：
    zip 的中央目录在文件尾部，而 manifest 又必须携带各成员的 sha256——真流式就
    得么少一份完整性校验、么在内存里攒完整个包。临时文件方案两者都不牺牲，代价
    是一次磁盘写入（默认不含媒体时包体是 KB~MB 级）。清理由调用方负责
    （路由用 `BackgroundTask`）。
    """
    plat = str(platform or "").lower()
    acct = str(account_id or "")
    ts = time.time() if now is None else float(now)
    fname = zip_filename(plat, acct, ts)

    try:
        store_counts = dict(store.count_account_data(plat, acct) or {})
    except Exception:
        logger.debug("[migration_export] 预估体量失败（不阻断导出）",
                     exc_info=True)
        store_counts = {}

    contacts = collect_contacts(store, plat, acct, crm_lookup=crm_lookup)
    reach = summarize_reachability(contacts)

    target_dir = Path(str(out_dir)) if out_dir else Path(tempfile.gettempdir())
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        target_dir = Path(tempfile.gettempdir())
    fd, tmp_path = tempfile.mkstemp(prefix="chatx-migration_", suffix=".zip",
                                    dir=str(target_dir))
    os.close(fd)

    integrity: Dict[str, str] = {}
    counts = {"conversations": 0, "messages": 0, "contacts": len(contacts),
              "media_files": 0, "media_bytes": 0, "media_missing": 0}
    media_notes: Dict[str, Any] = {"budget_bytes": int(media_budget_bytes),
                                   "skipped_budget": 0}

    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED,
                             allowZip64=True) as zf:
            # 1) contacts.jsonl
            w = _HashingWriter(zf, _MEMBER_CONTACTS_JSONL)
            for row in contacts:
                w.write(_jsonl(row))
            integrity[_MEMBER_CONTACTS_JSONL] = w.close()

            # 2) contacts.csv（UTF-8 BOM，Excel 双击直开不乱码）
            buf = io.StringIO(newline="")
            cw = csv.writer(buf, lineterminator="\r\n")
            cw.writerow(list(CONTACT_CSV_HEADER))
            for row in contacts:
                cw.writerow(contact_csv_row(row))
            csv_bytes = "\ufeff" + buf.getvalue()
            w = _HashingWriter(zf, _MEMBER_CONTACTS_CSV)
            w.write(csv_bytes)
            integrity[_MEMBER_CONTACTS_CSV] = w.close()

            # 3) conversations.jsonl + 媒体索引（同一趟遍历，别扫两遍库）
            media_rows: List[Dict[str, Any]] = []
            w = _HashingWriter(zf, _MEMBER_CONVERSATIONS)
            for conv, msgs in store.iter_account_history(plat, acct):
                crow: Dict[str, Any] = {"type": "conversation"}
                for k in CONV_FIELDS:
                    if k in conv:
                        crow[k] = conv.get(k)
                w.write(_jsonl(crow))
                counts["conversations"] += 1
                for m in msgs:
                    mrow: Dict[str, Any] = {
                        "type": "message",
                        "conversation_id": conv.get("conversation_id"),
                    }
                    for k in MSG_FIELDS:
                        if k in m:
                            mrow[k] = m.get(k)
                    w.write(_jsonl(mrow))
                    counts["messages"] += 1
                    ref = str(m.get("media_ref") or "").strip()
                    if ref:
                        member = media_member_path(ref)
                        media_rows.append({
                            "message_id": m.get("message_id"),
                            "conversation_id": conv.get("conversation_id"),
                            "media_ref": ref,
                            "media_type": str(m.get("media_type") or ""),
                            "path": member,
                        })
            integrity[_MEMBER_CONVERSATIONS] = w.close()

            # 4) media/（可选）+ media_index.jsonl（**恒有**：不打包也要留对应关系，
            #    否则日后想补齐媒体就得重新解析全部消息）
            packed: Dict[str, bool] = {}
            for entry in media_rows:
                member = str(entry.get("path") or "")
                local = (_resolve_media_file(str(entry.get("media_ref") or ""))
                         if member else None)
                if local is None:
                    entry["missing"] = True
                    entry["size"] = 0
                    counts["media_missing"] += 1
                    continue
                entry["missing"] = False
                try:
                    entry["size"] = int(os.path.getsize(local))
                except OSError:
                    entry["size"] = 0
                if not include_media:
                    continue
                if member in packed:          # 同一文件被多条消息引用
                    continue
                over_files = counts["media_files"] >= int(media_max_files)
                over_bytes = (counts["media_bytes"] + int(entry["size"])
                              > int(media_budget_bytes))
                if over_files or over_bytes:
                    entry["skipped"] = "budget"
                    media_notes["skipped_budget"] = int(
                        media_notes["skipped_budget"]) + 1
                    continue
                try:
                    zf.write(local, member)
                except (OSError, ValueError):
                    logger.debug("[migration_export] 媒体写入失败 %s", member,
                                 exc_info=True)
                    entry["missing"] = True
                    counts["media_missing"] += 1
                    continue
                packed[member] = True
                try:
                    entry["sha256"] = _sha256_file(local)
                except OSError:
                    entry["sha256"] = ""
                counts["media_files"] += 1
                counts["media_bytes"] += int(entry["size"])

            w = _HashingWriter(zf, _MEMBER_MEDIA_INDEX)
            for entry in media_rows:
                w.write(_jsonl(entry))
            integrity[_MEMBER_MEDIA_INDEX] = w.close()

            # 5) README.txt
            w = _HashingWriter(zf, _MEMBER_README)
            w.write(readme_text(plat, acct, media_included=include_media))
            integrity[_MEMBER_README] = w.close()

            # 6) manifest.json —— **最后写**（counts 必须是真正写进去的行数）
            manifest = build_manifest(
                platform=plat, account_id=acct, label=label, ban=ban,
                counts=counts, store_counts=store_counts, reachability=reach,
                integrity=integrity, media_included=include_media,
                media_notes=media_notes, generated_at=ts,
                app_version=app_version)
            w = _HashingWriter(zf, _MEMBER_MANIFEST)
            w.write(json.dumps(manifest, ensure_ascii=False, indent=2,
                               default=str))
            w.close()   # manifest 自身的 sha256 不进 integrity（自指无意义）
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return MigrationKit(path=tmp_path, filename=fname, counts=counts,
                        store_counts=store_counts, reachability=reach,
                        manifest=manifest)
