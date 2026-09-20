# -*- coding: utf-8 -*-
"""审计日志展示层纯函数（首页「最近操作」卡 / ``/audit`` 时间轴共用，2026-08-05）。

背景：首页卡片曾直接渲染原始 action 枚举、把通用 ``target`` 字段硬标成
「通道」（该卡最初为汇率通道域设计，如今全库 100+ 动作共用同一本账），
且批量操作逐条记账刷屏（一次清理 9 条记忆＝9 行同样条目霸屏）。

本模块把「展示用的结构化组装」收成纯函数：**只出结构不出文案**——
人话标签/前缀词由模板层经 i18n 键（``aud_act_*`` / ``aud_ui_*``）渲染，
保证中英双语与模板热更新语义不变。

设计取舍（勿顺手"修"掉）：
- 聚合按「相邻同 (操作人, 动作) 且间隔 ≤window_sec」合并——批量删除的
  逐条审计在**展示层**折叠成一行 ×N；账本本身不动（历史数据、CSV 导出
  口径、逐条追溯能力都保持原样）。
- 排序统一**最新在前**。``AuditStore.query`` 返回旧→新是历史契约（CSV
  导出按时间正序依赖它），故在展示层翻转而非改 store 全局语义。
"""

from __future__ import annotations

import math
import re
import time
from typing import Dict, Iterable, List, Optional, Tuple

from src.utils.audit_store import DANGER_TOKENS, is_danger_action

# 图标家族 token（沿用旧模板 绿=新增/蓝=更新/红=删除 的配色语义，保持视觉连续）。
_ICON_DEL_TOKENS = ("delete", "remove", "revoke", "cancel", "disable", "rollback", "unlink", "unbind")
_ICON_ADD_TOKENS = ("add", "create", "import", "seed", "enroll", "link", "claim")
_ICON_UPD_TOKENS = ("update", "set", "change", "batch", "save", "write", "toggle",
                    "assign", "bind", "restore", "sync", "confirm", "migrate")


# ── /audit 业务视角族（family）单源定义 ────────────────────────────
# 谓词（python 侧）与 SQL LIKE 模式（下推 AuditStore.query，全账本覆盖）由
# 同一份定义派生；两者语义一致由门禁钉住（test_audit_display.py::
# test_family_patterns_equal_predicates）。改这里 = 谓词与 SQL 同步改。
# danger token 与 AuditStore / EventBus 告警同源（utils.audit_store.DANGER_TOKENS）。
FAMILY_DEFS: Dict[str, Dict[str, tuple]] = {
    "danger": {"contains": DANGER_TOKENS},
    "memory": {"prefixes": ("episodic_", "identity_")},
    "kb": {"prefixes": ("kb_", "learner_")},
    # 人设资产：档案 + 相册/脸图 + 克隆声/台词备货（pmedia_/voice_ 不含
    # "persona" 字样，旧定义漏进「未分类」——2026-08-18 并入本族）。
    "persona": {"contains": ("persona",),
                "prefixes": ("profile", "pmedia_", "voice_", "prerender_")},
    "sticker": {"prefixes": ("stk_",)},
    "contacts": {"prefixes": (
        "merge_review_", "mobile_handoff_", "reunion_", "admin_link_",
        "relations_", "account_limit_", "draft_eval_", "draft_unmark_",
        "tg_members_",
    )},
    "rpa": {"contains": ("rpa",), "prefixes": ("wa_", "line_", "messenger_")},
}

# 芯片渲染序＝FAMILY_DEFS 的唯一展示顺序；模板 / 路由禁止再手写一份
# （漏登记 = 新族有谓词没入口，比没有更糟）。门禁钉 keys 集合相等。
FAMILY_ORDER: Tuple[str, ...] = (
    "danger", "memory", "kb", "persona", "sticker", "contacts", "rpa",
)
if set(FAMILY_ORDER) != set(FAMILY_DEFS):
    raise RuntimeError(
        f"FAMILY_ORDER {set(FAMILY_ORDER)!r} != FAMILY_DEFS {set(FAMILY_DEFS)!r}"
    )


def family_of(action: str) -> str:
    """action → 业务族名（首页族芯片用）；无归属返回 ""。

    刻意跳过 danger——那是**严重度**标记（行已有红色强调），不是业务域；
    ``stk_pack_delete`` 的芯片该指 ``family=sticker`` 而非 ``family=danger``。
    """
    for fam in FAMILY_ORDER:
        if fam == "danger":
            continue
        pred = family_predicate(fam)
        if pred and pred(action):
            return fam
    return ""


def operator_display_names(user_store) -> Dict[str, str]:
    """web 用户表 username → display_name（纯展示层映射）。

    账本 ``user_id`` 永不改写——模板显示 display_name、title 保留原 id 对账；
    非 web 用户的操作人（system/watchdog…）不在表里，模板回落原样。
    用户表缺席/查询异常 → 空表（等价旧行为）。
    """
    try:
        out: Dict[str, str] = {}
        for u in (user_store.list_users() if user_store else []) or []:
            name = str(u.get("username") or "")
            if name:
                out[name] = str(u.get("display_name") or "") or name
        return out
    except Exception:
        return {}


def family_predicate(family: str):
    """family → 谓词函数；未知 family 返回 None（调用方按不过滤处理）。"""
    d = FAMILY_DEFS.get(family or "")
    if not d:
        return None
    contains = d.get("contains", ())
    prefixes = d.get("prefixes", ())

    def _pred(action: str) -> bool:
        a = (action or "").lower()
        if any(t in a for t in contains):
            return True
        return bool(prefixes) and a.startswith(prefixes)

    return _pred


def _like_escape(fragment: str) -> str:
    r"""转义 LIKE 元字符（ESCAPE '\'）——动作名里的下划线是字面量不是通配。"""
    return (fragment.replace("\\", "\\\\")
            .replace("%", r"\%").replace("_", r"\_"))


def family_like_patterns(family: str) -> List[str]:
    """family → SQLite LIKE 模式列表（配 ESCAPE '\\'），与 family_predicate 同义。

    未知 family 返回 []（查询侧不加过滤子句，与旧「忽略未知 family」语义一致）。
    """
    d = FAMILY_DEFS.get(family or "")
    if not d:
        return []
    pats = [f"%{_like_escape(t)}%" for t in d.get("contains", ())]
    pats += [f"{_like_escape(p)}%" for p in d.get("prefixes", ())]
    return pats


def action_icon_kind(action: str) -> str:
    """add | update | delete | other——先判删除族防 unlink/unbind 落进 link/bind。"""
    a = (action or "").lower()
    if any(t in a for t in _ICON_DEL_TOKENS):
        return "delete"
    if any(t in a for t in _ICON_ADD_TOKENS):
        return "add"
    if any(t in a for t in _ICON_UPD_TOKENS):
        return "update"
    return "other"


def classify_target(action: str, target: str) -> Tuple[str, str]:
    """``target`` 字段按动作家族语义化：(kind, text)。

    kind ∈ ``memory``（情景记忆行 ID）| ``keyword``（批量删除的关键词）|
    ``identity``（platform:uid 对）| ``generic``（原样中性徽章）| ``""``（无对象）。
    审计表的 target 是自由文本，语义由写入方决定——这里只对**已知家族**
    做前缀标注，认不出的一律 generic 原样展示，绝不猜。
    """
    a = action or ""
    t = (target or "").strip()
    if not t:
        return "", ""
    if a in ("episodic_delete", "episodic_confirm_inferred"):
        return "memory", t
    if a == "episodic_bulk_delete":
        return "keyword", t
    if a.startswith("identity_"):
        return "identity", t
    return "generic", t


# ── 对象串结构化（kv / 增减量，只出结构不出文案）────────────────────
# 写入方把 target/new_val 写成自由文本（pack=starter-faces、from=a to=b、
# packs+2 items+51）。展示层认得出的拆成 {key,value}，认不出的保持原样——
# 绝不猜。键的人话标签由模板经 aud_tgt_* 渲染，未登记键回落原 key。
_KV_PAIR = re.compile(
    r"(?:^|(?<=[\s;,]))([A-Za-z_][A-Za-z0-9_]{0,31})=([^\s;,=]+)"
)
_DELTA_PAIR = re.compile(
    r"(?:^|(?<=[\s;,]))([A-Za-z_][A-Za-z0-9_]{0,31})([+-]\d+)"
)


def parse_object_tokens(text: str) -> List[Dict[str, str]]:
    """自由文本 → ``[{key, value}]``；覆盖率不够或非结构化则 ``[]``。

    覆盖率守卫：解析出的字面量必须几乎铺满原文（只允许对间分隔符），
    防止从 URL / 长句里抠出偶然的 ``pack=x`` 冒充结构化对象。
    """
    s = (text or "").strip()
    if not s:
        return []
    if "=" in s:
        pairs = _KV_PAIR.findall(s)
        if pairs and _token_coverage_ok(s, pairs, joiner="="):
            return [{"key": k, "value": v} for k, v in pairs]
    if "+" in s or (s.count("-") >= 1 and any(ch.isdigit() for ch in s)):
        pairs = _DELTA_PAIR.findall(s)
        if pairs and _token_coverage_ok(s, pairs, joiner=""):
            return [{"key": k, "value": v} for k, v in pairs]
    return []


def _token_coverage_ok(s: str, pairs: List[Tuple[str, str]], *,
                       joiner: str) -> bool:
    consumed = sum(len(k) + len(joiner) + len(v) for k, v in pairs)
    leftover = len(s) - consumed
    seps = max(0, len(pairs) - 1)
    return leftover <= max(4, seps * 2) and leftover < len(s) and consumed > 0


def decorate_object(action: str, target: str, extra: str = "") -> Dict:
    """动作 + 对象串 + 可选详情 → 展示结构。

    已知家族（memory/keyword/identity）保持旧语义，不拆 kv——那些字段
    不是 key=value（记忆行 ID / 关键词 / platform:uid）。generic 且能
    拆出 token 的升为 ``kv``；target 为空时回落解析 extra（播种类动作
    把增量写在 new_val：``packs+2 items+51``）。
    """
    kind, text = classify_target(action, target)
    tokens: List[Dict[str, str]] = []
    if kind in ("", "generic"):
        tokens = parse_object_tokens(target)
        if tokens:
            kind = "kv"
        elif not (target or "").strip():
            tokens = parse_object_tokens(extra)
            if tokens:
                kind = "kv"
    detail = parse_object_tokens(extra) if extra else []
    if tokens and tokens == detail:
        detail = []
    return {
        "target_kind": kind,
        "target_text": text,
        "target_tokens": tokens,
        "detail_tokens": detail,
    }


def operator_hue(name: str) -> int:
    """操作人名 → 稳定色相（0..359），avatar 圆点用；同名恒同色。"""
    h = 0
    for i, ch in enumerate(name or ""):
        h = (h + ord(ch) * (i * 31 + 7)) % 360
    return h


def _parse_ts(ts: str) -> Optional[float]:
    try:
        return time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None


def _sort_key(row: Dict) -> Tuple[float, str, int]:
    epoch = row.get("epoch")
    try:
        rid = int(row.get("id") or 0)
    except Exception:
        rid = 0
    return (epoch if epoch is not None else 0.0, str(row.get("ts") or ""), rid)


_BRIEF_MAX = 96


def build_recent_groups(entries: Iterable[Dict], *, window_sec: int = 90,
                        max_groups: int = 8, shown_targets: int = 3) -> List[Dict]:
    """原始审计行（任意顺序）→ 最新在前的展示组列表。

    合并规则：排序后**相邻**且同 (操作人, 动作)、组尾与下一行间隔 ≤window_sec
    的行并成一组（批量操作逐条记账的展示层折叠）；时间戳解析失败的行不参与
    合并（宁可多行不错并）。

    每组字段：action / operator / count / hue / icon / danger / snapshot /
    target_kind / targets_shown / targets_more / target_tokens / detail_tokens /
    brief / ts / hm / date。
    """
    rows: List[Dict] = []
    for e in entries or []:
        ts = str(e.get("ts") or "")
        rows.append({
            "ts": ts,
            "epoch": _parse_ts(ts),
            "action": str(e.get("action") or ""),
            "operator": str(e.get("user_id") or ""),
            "target": str(e.get("target") or ""),
            "old_val": str(e.get("old_val") or ""),
            "new_val": str(e.get("new_val") or ""),
            "snapshot_id": str(e.get("snapshot_id") or ""),
            "id": e.get("id") or 0,
        })
    rows.sort(key=_sort_key, reverse=True)

    groups: List[Dict] = []
    for r in rows:
        g = groups[-1] if groups else None
        can_merge = (
            g is not None
            and g["action"] == r["action"]
            and g["operator"] == r["operator"]
            and g["_tail_epoch"] is not None
            and r["epoch"] is not None
            and (g["_tail_epoch"] - r["epoch"]) <= window_sec
        )
        if can_merge:
            g["count"] += 1
            g["_tail_epoch"] = r["epoch"]
            if r["target"] and r["target"] not in g["_targets"]:
                g["_targets"].append(r["target"])
            g["snapshot"] = g["snapshot"] or bool(r["snapshot_id"])
            continue
        groups.append({
            "action": r["action"],
            "operator": r["operator"],
            "count": 1,
            "ts": r["ts"],                      # 组内最新一条的完整时间戳
            "hm": r["ts"][11:16] if len(r["ts"]) >= 16 else r["ts"],
            "date": r["ts"][:10],
            "snapshot": bool(r["snapshot_id"]),
            "snapshot_id": r["snapshot_id"],
            "_targets": [r["target"]] if r["target"] else [],
            "_tail_epoch": r["epoch"],
            "_old_val": r["old_val"],
            "_new_val": r["new_val"],           # 组头=最新；合并时不覆盖
        })

    out: List[Dict] = []
    for g in groups[:max_groups]:
        dec = decorate_object(
            g["action"],
            g["_targets"][0] if g["_targets"] else "",
            g.get("_new_val") or "",
        )
        danger = is_danger_action(g["action"])
        brief = ""
        if danger and g["_old_val"]:
            brief = g["_old_val"][:_BRIEF_MAX]
            if len(g["_old_val"]) > _BRIEF_MAX:
                brief += "…"
        out.append({
            "action": g["action"],
            "operator": g["operator"],
            "count": g["count"],
            "hue": operator_hue(g["operator"]),
            "icon": action_icon_kind(g["action"]),
            "danger": danger,
            "snapshot": g["snapshot"],
            "snapshot_id": g["snapshot_id"],
            "target_kind": dec["target_kind"],
            "targets_shown": g["_targets"][:shown_targets],
            "targets_more": max(0, len(g["_targets"]) - shown_targets),
            "target_tokens": dec["target_tokens"],
            "detail_tokens": dec["detail_tokens"],
            "family": family_of(g["action"]),
            "brief": brief,
            "ts": g["ts"],
            "hm": g["hm"],
            "date": g["date"],
        })
    return out


def group_days(groups: List[Dict], *, today: str, yesterday: str) -> List[Dict]:
    """组列表（已最新在前）→ 按日分段：[{kind, date, items}]。

    kind ∈ today | yesterday | date（模板据此选「今天/昨天/月-日」标题）。
    """
    out: List[Dict] = []
    for g in groups:
        d = g.get("date") or ""
        if out and out[-1]["date"] == d:
            out[-1]["items"].append(g)
            continue
        kind = "today" if d == today else ("yesterday" if d == yesterday else "date")
        out.append({"date": d, "kind": kind, "items": [g]})
    return out


def summarize_actions(actions: Iterable[str]) -> Dict:
    """今日摘要：{total, danger, families}。

    ``families``＝业务族直方图 [(族名, 次数)]，按 次数降序 → FAMILY_ORDER 序
    平局；无归属动作（save_settings…）不进桶，故各族之和 ≤ total。
    消费方（dashboard h2）只取前几个，截断策略留给展示层。
    """
    total = 0
    danger = 0
    fam_counts: Dict[str, int] = {}
    for a in actions or []:
        total += 1
        if is_danger_action(a):
            danger += 1
        fam = family_of(a)
        if fam:
            fam_counts[fam] = fam_counts.get(fam, 0) + 1
    rank = {f: i for i, f in enumerate(FAMILY_ORDER)}
    families = sorted(fam_counts.items(),
                      key=lambda kv: (-kv[1], rank.get(kv[0], 99)))
    return {"total": total, "danger": danger, "families": families}


def paginate_newest_first(entries: List[Dict], page: int, per_page: int) -> Tuple[List[Dict], int, int, int]:
    """最新在前的分页：返回 (页内记录, 总数, 总页数, 规整后页码)。

    ``AuditStore.query`` 的旧→新契约不动（CSV 导出依赖），翻转发生在这里；
    第 1 页恒为最新一段——「查最近发生了什么」是审计页的第一场景。
    """
    rows = list(entries or [])
    rows.sort(
        key=lambda e: (
            _parse_ts(str(e.get("ts") or "")) or 0.0,
            str(e.get("ts") or ""),
            int(e.get("id") or 0),
        ),
        reverse=True,
    )
    total = len(rows)
    per_page = max(1, int(per_page or 1))
    total_pages = max(1, math.ceil(total / per_page))
    page = max(1, min(int(page or 1), total_pages))
    start = (page - 1) * per_page
    return rows[start:start + per_page], total, total_pages, page
