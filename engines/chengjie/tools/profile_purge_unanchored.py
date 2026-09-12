# -*- coding: utf-8 -*-
"""Q-19（#294 / #272 追加）：清洗画像里「不可锚定」的 AI 推断 / 昵称槽位（一次性 + 回访可复跑）。

只处理 ``source=ai_inferred / nickname``（含旧形 ``src=llm / llm_pending``）的单元；
**confirmed（坐席确认 / 手录 / 客户原话正则 auto）一字不动**。判据与线上写口同一把尺子：

- A 槽类型校验 ``profile_slots.slot_validate``：age 16–99 / 年龄段；occupation·location·residence
  ≤24 字短语禁整句（「我住在去嵐山散步」「織り機の前に座って、新しい帯の色合わせ」）；
  name ≤12；婚恋 / 家庭闭集 → 不过 ``invalid:<why>``；
- B 原话锚定：``ai_inferred`` 单元无 ``evidence`` → ``unanchored``（存量多是 Q-5 之前 / 无引文的
  写入，线上新写口已强制 evidence；有 evidence 的存量视为可锚定，不去翻会话历史——画像行没有
  账号维度，跨账号反查代价与误删风险都不值）。昵称来源不要求 evidence（evidence 就是昵称）。

Q-25（#295 #300）二期：``--memory`` 同门扫 **记忆链**（episodic_memory）——只判 ``source=ai_inferred`` 的
LLM 事实（``user_stated`` 客户原话正则一字不动），判据 = ``src.companion.fact_gate.check(kind=fact)``：
无 ``source_quote`` → ``no_evidence``；事实含合体主语（一起 / 我们 / together…）而引文没有 → ``subject``；
事实里的名字 / 数字不在引文里 → ``unanchored``。``--apply`` 硬删命中行（``DELETE … WHERE id``）。

用法（引擎目录下）::

    python tools/profile_purge_unanchored.py                 # = --dry-run：只列出，不动库
    python tools/profile_purge_unanchored.py --dry-run --json
    python tools/profile_purge_unanchored.py --apply         # 真删（先看一遍 dry-run 输出）
    python tools/profile_purge_unanchored.py --db D:\\path\\marketing_goals.db --apply
    python tools/profile_purge_unanchored.py --platform telegram --chat-key 7340576921
    python tools/profile_purge_unanchored.py --memory --dry-run             # Q-25：扫记忆链
    python tools/profile_purge_unanchored.py --memory --user-id telegram:7543790794 --dry-run
    python tools/profile_purge_unanchored.py --memory --memory-db D:\\path\\bot.db --apply
    python tools/profile_purge_unanchored.py --own-name --dry-run           # Q-38：人设自称 name 槽
    python tools/profile_purge_unanchored.py --own-name --account-id 12084403608 --dry-run
    python tools/profile_purge_unanchored.py --own-name --apply             # 真删（先看 dry-run；confirmed 不动）

库路径：``--db`` 显式 > ``companion.goals.db_path``（config.yaml / config.local.yaml）>
``<数据根>/config/marketing_goals.db``（``AITR_DATA_ROOT`` / 实例根发现，与 goal_sprint_drill 同口径）。
记忆库：``--memory-db`` 显式 > ``memory.db_path`` > ``<数据根>/config/bot.db``（与 skill_manager 同口径）。

回访说明（发版对账 v1.0.82 Q-19）：
1. 先 ``--dry-run`` 看清单：每行 ``platform:chat_key slot=… source=… reason=… value=…``；
   reason 分布应集中在 ``invalid:sentence / invalid:not_place / invalid:age_format / unanchored``；
2. 抽 3–5 条对照会话原文确认确是幻觉 / 整句，再 ``--apply``；
3. 复跑 ``--dry-run`` 应为 0 行；线上新写入不再产生这类单元（``[profile] drop … reason=`` 日志可查）；
4. 该脚本可反复跑，幂等。删除只影响 mentioned 单元；目标卡「已采集 N/10」只数 confirmed，不受影响。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

_AUTO_SOURCES = ("ai_inferred", "nickname", "llm", "llm_pending")


def _cell_parts(cell: Any):
    """→ (value, source, status, evidence)；三种形状都认。"""
    from src.companion.goals.profile_slots import cell_view
    val, src, st = cell_view(cell)
    ev = ""
    if isinstance(cell, dict):
        ev = str(cell.get("evidence") or "").strip()
    return val, str(src or "").strip().lower(), st, ev


def judge_cell(slot: str, cell: Any) -> str:
    """单元判定 → 原因（空串＝保留）。只对自动来源判；confirmed / 手录恒保留。"""
    val, src, st, ev = _cell_parts(cell)
    if not val or st == "confirmed" or src not in _AUTO_SOURCES:
        return ""
    from src.companion.goals.profile_slots import slot_validate
    _nv, why = slot_validate(slot, val)
    if why:
        return f"invalid:{why}"
    if src in ("ai_inferred", "llm", "llm_pending") and not ev:
        return "unanchored"
    return ""


def judge_own_name(slot: str, cell: Any, reserved: Any) -> str:
    """Q-38：``name`` 槽值 ∈ 该会话人设 reserved → ``own_name`` / ``vocative``。confirmed 恒空。"""
    if str(slot or "").strip().lower() != "name":
        return ""
    val, src, st, ev = _cell_parts(cell)
    if not val or st == "confirmed" or src not in _AUTO_SOURCES:
        return ""
    try:
        from src.companion.fact_gate import name_reserved_reason
        return name_reserved_reason(val, reserved, ev)
    except Exception:
        return ""


def scan_fields(platform: str, chat_key: str, fields: Dict[str, Any],
                *, own_name_reserved: Any = None, own_name_only: bool = False
                ) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    bag = list(own_name_reserved or [])
    for slot, cell in (fields or {}).items():
        why = "" if own_name_only else judge_cell(str(slot), cell)
        if not why and own_name_reserved is not None:
            why = judge_own_name(str(slot), cell, own_name_reserved)
        if not why:
            continue
        val, src, st, ev = _cell_parts(cell)
        out.append({"platform": platform, "chat_key": chat_key, "slot": str(slot), "value": val,
                    "source": src, "status": st, "evidence": ev, "reason": why,
                    "reserved": bag})
    return out


def iter_profiles(store: Any):
    """遍历 customer_profiles → (platform, chat_key, fields)。"""
    rows = store._conn.execute(
        "SELECT platform, chat_key, fields FROM customer_profiles").fetchall()
    for r in rows:
        try:
            fields = json.loads(r["fields"] or "{}")
        except Exception:
            continue
        if isinstance(fields, dict):
            yield str(r["platform"]), str(r["chat_key"]), fields


def scan_store(store: Any, *, platform: str = "", chat_key: str = "",
               own_name: bool = False, reserved_resolver: Any = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for pf, ck, fields in iter_profiles(store):
        if platform and pf != platform:
            continue
        if chat_key and ck != chat_key:
            continue
        reserved = None
        if own_name:
            try:
                reserved = set(reserved_resolver(pf, ck) if reserved_resolver else [])
            except Exception:
                reserved = set()
        out.extend(scan_fields(pf, ck, fields, own_name_reserved=reserved,
                               own_name_only=bool(own_name)))
    return out


def purge(store: Any, rows: List[Dict[str, Any]], *, apply: bool) -> int:
    """按扫描结果删槽（``upsert_profile_cells(cells={slot: None})``）。dry-run → 0。"""
    if not apply or not rows:
        return 0
    by_conv: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        by_conv.setdefault((r["platform"], r["chat_key"]), {})[r["slot"]] = None
    n = 0
    for (pf, ck), cells in by_conv.items():
        before = dict((store.get_customer_profile(pf, ck) or {}).get("fields") or {})
        # 二次防线：只删仍是自动来源 + 非 confirmed 的槽（扫描到删除之间坐席可能已确认）
        keep: Dict[str, Any] = {}
        for k in cells:
            meta = next((r for r in rows if r["platform"] == pf and r["chat_key"] == ck
                         and r["slot"] == k), None) or {}
            if str(meta.get("reason") or "") in ("own_name", "vocative"):
                if judge_own_name(k, before.get(k), meta.get("reserved") or []):
                    keep[k] = None
            elif judge_cell(k, before.get(k)):
                keep[k] = None
        cells = keep
        if not cells:
            continue
        store.upsert_profile_cells(pf, ck, cells)
        after = dict((store.get_customer_profile(pf, ck) or {}).get("fields") or {})
        n += sum(1 for k in cells if k not in after)
    return n


# ── Q-25（#295 #300）：记忆链同门 ────────────────────────────────────────────────

_MEMORY_AUTO_SOURCES = ("ai_inferred",)


def judge_memory_row(content: Any, source: Any, source_quote: Any) -> str:
    """episodic 一行 → 原因（空串＝保留）。只判 ``source=ai_inferred``；``user_stated`` 恒保留。
    判据 = ``fact_gate.check(kind=fact, evidence=source_quote, inbound_texts=[source_quote])``：
    引文在不在客户入站里这一步在存量上无从核（库里只有引文），退化为 no_evidence / subject /
    名字数字锚定三条。"""
    src = str(source or "").strip().lower()
    c = str(content or "").strip()
    if not c or src not in _MEMORY_AUTO_SOURCES:
        return ""
    q = str(source_quote or "").strip()
    try:
        from src.companion.fact_gate import check as _gate_check
        ok, why = _gate_check(c, slot_or_kind="fact", evidence=q, inbound_texts=[q] if q else [])
    except Exception:
        return "gate_error"
    return "" if ok else (why or "unanchored")


def _memory_conn(db: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def scan_memory(conn: sqlite3.Connection, *, user_id: str = "") -> List[Dict[str, Any]]:
    """遍历 episodic_memory 的 ``source=ai_inferred`` 行 → 命中清单。"""
    out: List[Dict[str, Any]] = []
    sql = ("SELECT id, user_id, content, COALESCE(source, 'user_stated') AS source,"
           " COALESCE(source_quote, '') AS source_quote, COALESCE(status, 'active') AS status"
           " FROM episodic_memory WHERE COALESCE(source, 'user_stated') = 'ai_inferred'")
    args: List[Any] = []
    if user_id:
        sql += " AND user_id LIKE ?"
        args.append(f"%{user_id}%")
    try:
        rows = conn.execute(sql, args).fetchall()
    except sqlite3.Error as exc:
        print(f"! 读取 episodic_memory 失败: {exc}", file=sys.stderr)
        return out
    for r in rows:
        why = judge_memory_row(r["content"], r["source"], r["source_quote"])
        if not why:
            continue
        out.append({"id": int(r["id"]), "user_id": str(r["user_id"] or ""), "fact": str(r["content"] or ""),
                    "source": str(r["source"] or ""), "quote": str(r["source_quote"] or ""),
                    "status": str(r["status"] or ""), "reason": why})
    return out


def purge_memory(conn: sqlite3.Connection, rows: List[Dict[str, Any]], *, apply: bool) -> int:
    """按扫描结果硬删（``DELETE FROM episodic_memory WHERE id=?``）。dry-run → 0。删前二次判定。"""
    if not apply or not rows:
        return 0
    n = 0
    for r in rows:
        try:
            cur = conn.execute(
                "SELECT content, COALESCE(source, 'user_stated'), COALESCE(source_quote, '')"
                " FROM episodic_memory WHERE id = ?", (int(r["id"]),)).fetchone()
            if not cur or not judge_memory_row(cur[0], cur[1], cur[2]):
                continue
            n += int(conn.execute("DELETE FROM episodic_memory WHERE id = ?", (int(r["id"]),)).rowcount or 0)
        except sqlite3.Error as exc:
            print(f"! 删除 id={r.get('id')} 失败: {exc}", file=sys.stderr)
    conn.commit()
    return n


def _resolve_memory_db(cli_db: str, data_root: str) -> Optional[Path]:
    if cli_db:
        return Path(cli_db)
    try:
        from scripts._data_root import load_merged_config, resolve_data_roots
        for root in resolve_data_roots(data_root):
            cfg = load_merged_config(Path(root)) or {}
            mdb = ((cfg.get("memory") or {}).get("db_path") or "")
            p = Path(str(mdb)) if mdb else Path(root) / "config" / "bot.db"
            if not p.is_absolute() and mdb:
                p = Path(root) / p
            if p.is_file():
                return p
    except Exception as exc:  # noqa: BLE001
        print(f"! 解析记忆库路径失败: {exc}", file=sys.stderr)
    fallback = _ENGINE_ROOT / "config" / "bot.db"
    return fallback if fallback.is_file() else None


def _fmt_memory(r: Dict[str, Any]) -> str:
    f = str(r["fact"]).replace("\n", " ")
    f = f if len(f) <= 60 else f[:59] + "…"
    q = str(r.get("quote") or "").replace("\n", " ")
    q = f" quote={q[:40]!r}" if q else ""
    return (f"memory id={r['id']} key={r['user_id']} source={r['source']} "
            f"reason={r['reason']} fact={f!r}{q}")


def run_memory(args: Any) -> int:
    """``--memory`` 分支：扫 / 删 episodic_memory 的不可锚定 ai_inferred 事实。"""
    db = _resolve_memory_db(args.memory_db, args.data_root)
    if db is None or not Path(db).is_file():
        print("! 找不到记忆库（--memory-db 指定或检查 memory.db_path）", file=sys.stderr)
        return 2
    apply = bool(args.apply) and not args.dry_run
    try:
        conn = _memory_conn(Path(db), readonly=not apply)
    except sqlite3.Error as exc:
        print(f"! 打开记忆库失败 {db}: {exc}", file=sys.stderr)
        return 2
    rows = scan_memory(conn, user_id=args.user_id)
    if args.json:
        print(json.dumps({"db": str(db), "kind": "memory", "apply": apply, "count": len(rows), "rows": rows},
                         ensure_ascii=False, indent=1))
    else:
        print(f"# memory db={db} mode={'APPLY' if apply else 'dry-run'} hits={len(rows)}")
        for r in rows:
            print(_fmt_memory(r))
        dist: Dict[str, int] = {}
        for r in rows:
            dist[r["reason"]] = dist.get(r["reason"], 0) + 1
        if dist:
            print("# reasons: " + ", ".join(f"{k}={v}" for k, v in sorted(dist.items())))
    if apply:
        n = purge_memory(conn, rows, apply=True)
        print(json.dumps({"deleted": n}, ensure_ascii=False) if args.json
              else f"# deleted {n} memory row(s); rerun --memory --dry-run to verify 0")
    elif rows and not args.json:
        print("# dry-run：未改库。确认后加 --apply")
    conn.close()
    return 0


def _own_name_resolver(args: Any):
    """--own-name：按 --persona-name 覆盖，否则 resolve_reserved_self_names（缺人设=空集）。"""
    pname = str(getattr(args, "persona_name", "") or "").strip()
    acct = str(getattr(args, "account_id", "") or "").strip()
    data_root = str(getattr(args, "data_root", "") or "").strip()

    def _res(pf: str, ck: str):
        if pname:
            from src.utils.persona_guard import reserved_self_names
            return reserved_self_names({"name": pname})
        cfg = {}
        try:
            from scripts._data_root import load_merged_config, resolve_data_roots
            for root in resolve_data_roots(data_root):
                cfg = load_merged_config(Path(root)) or {}
                if cfg:
                    break
        except Exception:
            cfg = {}
        from src.companion.goals.profile_fill import resolve_reserved_self_names
        cid = f"{pf}:{acct}:{ck}" if acct else ""
        return resolve_reserved_self_names(
            cfg_root=cfg, platform=pf, account_id=acct, chat_key=ck,
            conversation_id=cid)

    return _res


def _resolve_db(cli_db: str, data_root: str) -> Optional[Path]:
    if cli_db:
        return Path(cli_db)
    try:
        from scripts._data_root import load_merged_config, resolve_data_roots
        from src.companion.goals.service import resolve_db_path
        for root in resolve_data_roots(data_root):
            cfg = load_merged_config(Path(root)) or {}
            p = Path(resolve_db_path(cfg, Path(root) / "config" / "config.yaml"))
            if p.is_file():
                return p
    except Exception as exc:  # noqa: BLE001
        print(f"! 解析库路径失败: {exc}", file=sys.stderr)
    fallback = _ENGINE_ROOT / "config" / "marketing_goals.db"
    return fallback if fallback.is_file() else None


def _fmt(r: Dict[str, Any]) -> str:
    v = str(r["value"]).replace("\n", " ")
    v = v if len(v) <= 40 else v[:39] + "…"
    ev = f" evidence={r['evidence'][:30]!r}" if r.get("evidence") else ""
    return (f"{r['platform']}:{r['chat_key']} slot={r['slot']} source={r['source']} "
            f"reason={r['reason']} value={v!r}{ev}")


def main(argv: Optional[List[str]] = None) -> int:
    # Windows 控制台默认 GBK：画像值含泰文 / 假名会让 print 直接炸，改 utf-8 + replace
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[attr-defined]
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default="", help="goals 库路径（缺省按配置 / 数据根解析）")
    ap.add_argument("--data-root", default="", help="实例数据根（缺省 AITR_DATA_ROOT / 自动发现）")
    ap.add_argument("--platform", default="", help="只扫该平台")
    ap.add_argument("--chat-key", default="", help="只扫该 chat_key")
    ap.add_argument("--dry-run", action="store_true", help="只列出（默认行为）")
    ap.add_argument("--apply", action="store_true", help="真删（不带 = dry-run）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--memory", action="store_true",
                    help="Q-25：改扫记忆链 episodic_memory（只判 source=ai_inferred）")
    ap.add_argument("--memory-db", default="", help="记忆库路径（缺省按 memory.db_path / 数据根 config/bot.db）")
    ap.add_argument("--user-id", default="", help="--memory 时只扫 user_id 含此串的行（如 telegram:7543790794）")
    ap.add_argument("--own-name", action="store_true",
                    help="Q-38：只列出 name 槽值 ∈ 该会话人设 reserved 的自动来源（默认 dry-run）")
    ap.add_argument("--account-id", default="", help="--own-name 时用于解析人设的账号 id")
    ap.add_argument("--persona-name", default="", help="--own-name 测试/覆盖：按此人设名建 reserved（不读配置）")
    args = ap.parse_args(argv)
    if args.memory:
        return run_memory(args)
    db = _resolve_db(args.db, args.data_root)
    if db is None or not Path(db).is_file():
        print("! 找不到 goals 库（--db 指定或检查 companion.goals.db_path）", file=sys.stderr)
        return 2
    from src.companion.goals.store import GoalStore
    apply = bool(args.apply) and not args.dry_run     # 两个都给 → dry-run 赢
    try:
        store = GoalStore(db) if apply else GoalStore.open_readonly(db)
    except sqlite3.Error as exc:
        print(f"! 打开库失败 {db}: {exc}", file=sys.stderr)
        return 2
    resolver = None
    if args.own_name:
        resolver = _own_name_resolver(args)
    rows = scan_store(store, platform=args.platform, chat_key=args.chat_key,
                      own_name=bool(args.own_name), reserved_resolver=resolver)
    if args.json:
        print(json.dumps({"db": str(db), "apply": apply, "count": len(rows), "rows": rows},
                         ensure_ascii=False, indent=1))
    else:
        print(f"# db={db} mode={'APPLY' if apply else 'dry-run'} hits={len(rows)}")
        for r in rows:
            print(_fmt(r))
        dist: Dict[str, int] = {}
        for r in rows:
            dist[r["reason"]] = dist.get(r["reason"], 0) + 1
        if dist:
            print("# reasons: " + ", ".join(f"{k}={v}" for k, v in sorted(dist.items())))
    if apply:
        n = purge(store, rows, apply=True)
        if args.json:
            print(json.dumps({"deleted": n}, ensure_ascii=False))
        else:
            print(f"# deleted {n} slot(s); rerun --dry-run to verify 0")
    elif rows and not args.json:
        print("# dry-run：未改库。确认后加 --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
