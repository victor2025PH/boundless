"""账号作用域键迁移（数据治理·Phase 2）：存量裸键/平台键 → 账号分桶键。

背景
====
双号隔离改造（2026-07）后，episodic 记忆与 ContextStore 的**写入键**带上了
account 分桶（``platform:acct:peer`` / ``acct:peer``）；但升级前的存量数据仍躺在
旧键（``platform:peer`` / 裸 ``peer``）下——对新读路径不可见 = 老客户「失忆」、
关系阶段清零（P1-1 数据孤儿）。

本工具按「**归属可判才迁、宁可不迁不能迁错**」迁移存量键：

- 归属判定：收件箱 ``conversations`` 表（platform, account_id, chat_key）是
  「哪个 peer 和哪个号聊过」的权威账本——peer 只出现在**一个非 default 账号**
  下 → 迁入该号分桶；出现在 default（default 的现行键**就是**旧格式，动了反而
  破坏）或多个账号下 → 跳过并登记原因。
- episodic 迁移经 ``EpisodicMemoryStore.merge_key``（content_hash 去重、幂等）；
  ContextStore 迁移为 rename（目标已存在 → 跳过保新，旧行留作死数据并登记）。
- 默认 **dry-run**；``--apply`` 才落地，落地前自动备份 db 文件。

CLI::

    python -m src.utils.account_scope_migration ^
        --episodic-db <bot.db> --context-db <context.db> --inbox-db <inbox.db> ^
        --registry-db <account_registry.db>
    （加 --apply 落地；--json-out report.json 落报告）

生产实测补充的两条安全闸（2026-07-25 智聊 dry-run 教训）：

- **在线账号闸门**（``--registry-db``）：目标桶必须是注册表 ``status=online`` 的
  账号——removed/offline 号没有活的分桶读者，迁进去=换个姿势继续孤儿；更危险的是
  「A 线主号曾以裸键运行、注册表行已 removed」场景（如智聊 8127518232），迁走裸键
  会把**还在跑的 A 线**搞失忆。给了 registry 才启用该闸，不给保持旧行为。
- **平台前缀键候选**：``user_context`` 里隔离改造前的 ``platform:acct:peer`` /
  ``platform:peer`` 老键同样是孤儿（现行读键=``acct:peer``），一并纳入；同一目标
  多来源（裸键+平台键并存）按 ``updated_at`` 最新行择优 rename，其余登记跳过。
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# 现行接入平台（键首段命中 = platform 前缀键）；RPA/组合键不进候选。
KNOWN_PLATFORMS = frozenset({
    "telegram", "whatsapp", "line", "messenger", "instagram", "zalo", "web",
    "qqbot", "qq",
    "wechat_kf",   # 微信客服（企业微信官方通道，实施97 线 A）
})

_SIMPLE_PEER = re.compile(r"^[A-Za-z0-9@.\-]+$")


# ── 归属账本 ────────────────────────────────────────────────────────────────

def build_attribution(inbox_db: str) -> Dict[str, Set[Tuple[str, str]]]:
    """从收件箱 conversations 表建 ``peer → {(platform, account_id), ...}``。"""
    out: Dict[str, Set[Tuple[str, str]]] = {}
    conn = sqlite3.connect(inbox_db)
    try:
        rows = conn.execute(
            "SELECT DISTINCT platform, account_id, chat_key FROM conversations"
        ).fetchall()
    finally:
        conn.close()
    for plat, acct, ck in rows:
        peer = str(ck or "").strip()
        if not peer:
            continue
        out.setdefault(peer, set()).add(
            (str(plat or "").strip().lower(), str(acct or "default").strip()))
    return out


def _sole_nondefault_account(
    pairs: Set[Tuple[str, str]], platform: str = "",
) -> Optional[Tuple[str, str]]:
    """归属唯一判定：候选 (platform, acct) 集合里恰有一个非 default 账号且
    default 从未与该 peer 聊过 → 返回它；否则 None（歧义/默认号在场=不迁）。"""
    cand = {(p, a) for p, a in pairs if not platform or p == platform}
    if not cand:
        return None
    if any(a in ("", "default") for _, a in cand):
        return None
    if len({a for _, a in cand}) != 1 or len({p for p, _ in cand}) != 1:
        return None
    return next(iter(cand))


def load_online_accounts(registry_db: str) -> Set[Tuple[str, str]]:
    """注册表在线账号集合 ``{(platform, account_id)}``。

    迁移目标桶必须有「活的分桶读者」——removed/offline 号迁进去仍是孤儿，
    且可能踩「A 线主号裸键还在被读」的雷（见模块 docstring）。
    """
    conn = sqlite3.connect(registry_db)
    try:
        rows = conn.execute(
            "SELECT platform, account_id FROM platform_accounts "
            "WHERE status='online'").fetchall()
    finally:
        conn.close()
    return {(str(p or "").strip().lower(), str(a or "").strip())
            for p, a in rows}


# ── 键分类 ─────────────────────────────────────────────────────────────────

def classify_key(key: str) -> Dict[str, str]:
    """episodic 键形态：scoped / legacy_platform / bare / other（组合键等不动）。"""
    k = str(key or "")
    if not k or "_" in k:
        return {"form": "other", "platform": "", "peer": ""}
    parts = k.split(":")
    if len(parts) == 1:
        if _SIMPLE_PEER.match(k):
            return {"form": "bare", "platform": "", "peer": k}
        return {"form": "other", "platform": "", "peer": ""}
    if len(parts) == 2 and parts[0].lower() in KNOWN_PLATFORMS:
        if _SIMPLE_PEER.match(parts[1]):
            return {"form": "legacy_platform",
                    "platform": parts[0].lower(), "peer": parts[1]}
        return {"form": "other", "platform": parts[0].lower(), "peer": ""}
    if len(parts) == 3 and parts[0].lower() in KNOWN_PLATFORMS:
        return {"form": "scoped", "platform": parts[0].lower(), "peer": parts[2]}
    return {"form": "other", "platform": "", "peer": ""}


# ── episodic 迁移 ──────────────────────────────────────────────────────────

def plan_episodic_account_scope(
    store: Any, attribution: Dict[str, Set[Tuple[str, str]]],
    online_accounts: Optional[Set[Tuple[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """dry-run 规划（纯只读）：每个存量键 → 动作/目标/原因。"""
    stats = store.list_key_stats()  # [(key, count), ...]
    all_keys = {k for k, _ in stats}
    plan: List[Dict[str, Any]] = []
    for key, cnt in stats:
        c = classify_key(key)
        if c["form"] not in ("bare", "legacy_platform"):
            continue
        pairs = attribution.get(c["peer"]) or set()
        hit = _sole_nondefault_account(pairs, platform=c["platform"])
        if hit is None:
            reason = ("default_or_ambiguous" if pairs else "no_attribution")
            plan.append({"old_key": key, "new_key": "", "fact_count": int(cnt),
                         "action": "skip", "reason": reason})
            continue
        plat, acct = hit
        target = f"{plat}:{acct}:{c['peer']}"
        if online_accounts is not None and (plat, acct) not in online_accounts:
            plan.append({"old_key": key, "new_key": target,
                         "fact_count": int(cnt), "action": "skip",
                         "reason": "target_account_not_online"})
            continue
        plan.append({
            "old_key": key, "new_key": target, "fact_count": int(cnt),
            "action": "merge" if target in all_keys else "rename",
            "reason": "sole_nondefault_account",
        })
    return plan


def apply_episodic_account_scope(
    store: Any, attribution: Dict[str, Set[Tuple[str, str]]],
    online_accounts: Optional[Set[Tuple[str, str]]] = None,
) -> Dict[str, Any]:
    plan = plan_episodic_account_scope(store, attribution, online_accounts)
    moved = keys = 0
    details: List[Dict[str, Any]] = []
    for item in plan:
        if item["action"] == "skip":
            details.append(item)
            continue
        n = store.merge_key(item["old_key"], item["new_key"])
        moved += n
        keys += 1
        details.append({**item, "moved_rows": n})
    return {"candidates": len(plan), "migrated_keys": keys,
            "moved_rows": moved, "details": details}


# ── ContextStore 迁移 ──────────────────────────────────────────────────────

def plan_context_account_scope(
    context_db: str, attribution: Dict[str, Set[Tuple[str, str]]],
    online_accounts: Optional[Set[Tuple[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """ContextStore（user_context 表）存量键 → ``acct:peer``（dry-run）。

    候选三形态（现行读键=``make_context_key`` 的 ``acct:peer``）：

    - 裸 ``peer``：靠收件箱归属账本判唯一非 default 账号；
    - ``platform:peer``（隔离前旧格式）：同上，platform 段用于收窄归属；
    - ``platform:acct:peer``（隔离前协议链旧格式）：键自带账号段，直接映射。

    同一目标多来源（裸键+平台键并存）→ ``updated_at`` 最新者 rename，
    其余 ``skip_older_duplicate``；目标已存在（现行键已有新数据）→ 全部
    ``skip_target_exists`` 保新。
    """
    conn = sqlite3.connect(context_db)
    try:
        rows = conn.execute(
            "SELECT user_id, updated_at FROM user_context").fetchall()
    finally:
        conn.close()
    existing = {str(r[0]) for r in rows}
    updated = {str(r[0]): float(r[1] or 0) for r in rows}

    groups: Dict[str, List[Tuple[float, str]]] = {}
    gated: List[Dict[str, Any]] = []
    for key in sorted(existing):
        if "_" in key:
            continue  # 组合键（冷却等）不动
        c = classify_key(key)
        if c["form"] == "bare":
            hit = _sole_nondefault_account(attribution.get(key) or set())
            if hit is None:
                continue  # default/歧义：现行合法或不可判，静默跳过
            plat, acct = hit
            peer = key
        elif c["form"] == "legacy_platform":
            hit = _sole_nondefault_account(
                attribution.get(c["peer"]) or set(), platform=c["platform"])
            if hit is None:
                continue
            plat, acct = hit
            peer = c["peer"]
        elif c["form"] == "scoped":
            # platform:acct:peer —— 键自带账号段，无需归属账本
            plat = c["platform"]
            acct = str(key.split(":")[1] or "").strip()
            peer = c["peer"]
            if not acct or acct == "default" or not _SIMPLE_PEER.match(peer or ""):
                continue
        else:
            continue
        target = f"{acct}:{peer}"
        if target == key:
            continue
        if online_accounts is not None and (plat, acct) not in online_accounts:
            gated.append({"old_key": key, "new_key": target,
                          "action": "skip_account_not_online"})
            continue
        groups.setdefault(target, []).append((updated.get(key, 0.0), key))

    plan: List[Dict[str, Any]] = []
    for target in sorted(groups):
        cands = sorted(groups[target], reverse=True)  # 最新在前
        winner_taken = target in existing  # 目标已有现行数据 → 全跳
        for _ts, key in cands:
            if winner_taken:
                plan.append({"old_key": key, "new_key": target,
                             "action": ("skip_target_exists"
                                        if target in existing
                                        else "skip_older_duplicate")})
                continue
            plan.append({"old_key": key, "new_key": target,
                         "action": "rename"})
            winner_taken = True
    plan.extend(gated)
    return plan


def apply_context_account_scope(
    context_db: str, attribution: Dict[str, Set[Tuple[str, str]]],
    online_accounts: Optional[Set[Tuple[str, str]]] = None,
) -> Dict[str, Any]:
    plan = plan_context_account_scope(context_db, attribution, online_accounts)
    conn = sqlite3.connect(context_db)
    renamed = 0
    try:
        for item in plan:
            if item["action"] != "rename":
                continue
            conn.execute(
                "UPDATE OR IGNORE user_context SET user_id=? WHERE user_id=?",
                (item["new_key"], item["old_key"]))
            renamed += 1
        conn.commit()
    finally:
        conn.close()
    return {"candidates": len(plan), "renamed": renamed, "details": plan}


# ── P-2 E（#259 #252 · D-P4，2026-09-08）：会话级停联标记 → 账号级停联名单 ─────────────
#   O-1 A 的冻结只记在 conversation_meta.conv_tags（「客户要求停联」）上；存量库升级到 1.0.79
#   要把这些行补进 account_blocklist.db（只增不删；已在名单的不重复）。默认 dry-run。

STOP_CONTACT_TAG = "客户要求停联"


def plan_blocklist_seed(inbox_db: str) -> List[Dict[str, Any]]:
    """扫 inbox.db：带「客户要求停联」标签的会话 → 名单候选行（纯只读）。"""
    conn = sqlite3.connect(inbox_db)
    try:
        rows = conn.execute(
            "SELECT c.platform, c.account_id, c.chat_key, c.last_ts, m.conv_tags "
            "FROM conversation_meta m JOIN conversations c "
            "ON c.conversation_id = m.conversation_id "
            "WHERE m.conv_tags LIKE ?", (f"%{STOP_CONTACT_TAG}%",)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out: List[Dict[str, Any]] = []
    for plat, acct, ck, last_ts, tags_json in rows:
        try:
            tags = json.loads(tags_json or "[]")
        except Exception:
            tags = []
        if STOP_CONTACT_TAG not in tags:
            continue   # LIKE 预筛的子串假阳性
        peer = str(ck or "").strip()
        if not peer:
            continue
        out.append({"type": "blocklist", "platform": str(plat or "").lower(),
                    "account_id": str(acct or "default"), "peer": peer,
                    "reason": "stop_contact", "hit_text": "", "ts": float(last_ts or 0),
                    "unfrozen_ts": 0.0, "source": "seed:conv_tag", "hits": 1})
    return out


def apply_blocklist_seed(inbox_db: str, blocklist_db: str) -> Dict[str, Any]:
    """落地：候选行 upsert 进 account_blocklist.db（只增不删；已有行只刷 ts / hits）。"""
    from src.inbox.account_blocklist import AccountBlocklist
    plan = plan_blocklist_seed(inbox_db)
    bl = AccountBlocklist(Path(blocklist_db))
    try:
        stats = bl.import_rows(plan)
    finally:
        bl.close()
    return {"candidates": len(plan), **stats, "details": plan}


# ── CLI ────────────────────────────────────────────────────────────────────

def _backup(path: str) -> str:
    dst = f"{path}.bak_{time.strftime('%Y%m%d%H%M%S')}"
    shutil.copy2(path, dst)
    return dst


def _main(argv: Optional[List[str]] = None) -> int:
    import argparse

    from src.utils.episodic_memory_store import EpisodicMemoryStore

    ap = argparse.ArgumentParser(
        description="账号作用域键迁移（存量裸键/平台键 → 账号分桶；默认 dry-run）")
    ap.add_argument("--episodic-db", required=True, help="episodic_memory 所在 db")
    ap.add_argument("--inbox-db", required=True, help="inbox 会话库（归属账本）")
    ap.add_argument("--context-db", default="", help="user_context 库（可选）")
    ap.add_argument("--registry-db", default="",
                    help="账号注册表库（给了才启用在线账号闸门，强烈建议）")
    ap.add_argument("--blocklist-db", default="",
                    help="P-2 E：账号级停联名单库（给了即把「客户要求停联」会话补进名单；"
                         "缺省 = inbox.db 同目录 account_blocklist.db）")
    ap.add_argument("--skip-blocklist", action="store_true", help="不做停联名单补种")
    ap.add_argument("--apply", action="store_true", help="落地（自动备份 db）")
    ap.add_argument("--json-out", default="", help="报告落 JSON 路径")
    args = ap.parse_args(argv)

    attribution = build_attribution(args.inbox_db)
    online: Optional[Set[Tuple[str, str]]] = None
    if args.registry_db:
        online = load_online_accounts(args.registry_db)
    report: Dict[str, Any] = {"apply": bool(args.apply),
                              "attributed_peers": len(attribution),
                              "online_accounts": (sorted(
                                  f"{p}:{a}" for p, a in online)
                                  if online is not None else None)}

    if args.apply:
        report["episodic_backup"] = _backup(args.episodic_db)
        if args.context_db:
            report["context_backup"] = _backup(args.context_db)

    store = EpisodicMemoryStore(args.episodic_db)
    if args.apply:
        report["episodic"] = apply_episodic_account_scope(
            store, attribution, online)
        if args.context_db:
            report["context"] = apply_context_account_scope(
                args.context_db, attribution, online)
    else:
        epi_plan = plan_episodic_account_scope(store, attribution, online)
        report["episodic"] = {
            "candidates": len(epi_plan),
            "migratable": sum(1 for p in epi_plan if p["action"] != "skip"),
            "details": epi_plan,
        }
        if args.context_db:
            ctx_plan = plan_context_account_scope(
                args.context_db, attribution, online)
            report["context"] = {
                "candidates": len(ctx_plan),
                "migratable": sum(
                    1 for p in ctx_plan if p["action"] == "rename"),
                "details": ctx_plan,
            }

    if not args.skip_blocklist:
        _bl_db = args.blocklist_db or str(
            Path(args.inbox_db).parent / "account_blocklist.db")
        if args.apply:
            report["blocklist"] = apply_blocklist_seed(args.inbox_db, _bl_db)
        else:
            _bl_plan = plan_blocklist_seed(args.inbox_db)
            report["blocklist"] = {"candidates": len(_bl_plan), "db": _bl_db,
                                   "details": _bl_plan}
        print(f"[{'apply' if args.apply else 'dry-run'}] 停联名单补种 候选="
              f"{report['blocklist'].get('candidates')} → {_bl_db}")

    mode = "apply" if args.apply else "dry-run"
    epi = report.get("episodic") or {}
    print(f"[{mode}] peers(归属账本)={report['attributed_peers']} "
          f"episodic 候选={epi.get('candidates')} "
          f"可迁/已迁={epi.get('migratable', epi.get('migrated_keys'))}")
    for d in (epi.get("details") or [])[:50]:
        print(f"  {d['old_key']} → {d.get('new_key') or '-'}  "
              f"({d['action']}{',' + d.get('reason', '') if d.get('reason') else ''})")
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[report] → {args.json_out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
