# -*- coding: utf-8 -*-
"""融合实例 P2：把一个实例数据根合并进另一个实例数据根（幂等 + dry-run 默认）。

场景（2026-07 智聊×通译融合）：双实例（AITR_DATA_DIR 隔离的两套 SQLite）合并为
单实例。源实例（通译）数据并入目标实例（智聊）数据根，账号打 business_line
标签（默认 translation → autodraft 档位封顶 review + proactive 一票否决，见 P1）。

用法::

    # 干跑（默认，只读两边，打印/落 JSON 计划）
    python -m scripts.merge_instance_data \
        --source D:/chengjie-instances/tongyi/data \
        --target D:/chengjie-instances/zhiliao/data

    # 实写（先自动备份目标各库为 *.premerge-<ts>.bak）
    python -m scripts.merge_instance_data --source ... --target ... --apply

设计要点：
- **幂等**：全部按自然键（conversation_id / message_id / cache_key / id /
  username / platform+account_id）「不存在才插入」；重跑 inserted=0。
- **列交集**：INSERT 只写源目标两侧都有的列（两侧 schema 版本略有出入也不炸；
  目标独有列吃默认值，如 platform_accounts.business_line）。
- **唯一索引守卫**（2026-07-24 首切事故补）：自然键之外，目标表全部唯一索引
  插入前自省预查（干跑/实写同判）——两实例各自播种同一模板包时
  kb_entries.template_key 撞 UNIQUE 的场景降级为 skipped_guard 而非炸穿；
  残余不可见约束再有行级 IntegrityError 兜底（skipped_conflict + 点名，
  语句级回滚不伤事务）。
- **凭证换密**：platform_accounts.meta_json 敏感字段（session_string 等）用
  源实例 config/registry.key 解密 → 目标实例 key 重加密（enc:v1 约定同
  src/integrations/registry_crypto）。两侧 key 相同则原样透传。密文解不开 →
  该账号跳过并如实报告（绝不迁 garbage 凭证）。
- **FTS 同步**：messages 插入后同步 INSERT messages_fts（目标 store 的检索
  不留盲区）；FTS 写失败只记 warning（可事后 rebuild），不阻断迁移。
- **审慎面**：web_users 同名用户跳过（目标侧管理员优先）；web_sessions /
  统计表 / 空表一概不迁。任何校验不平 → 退出码 1。
- 报告绝不包含凭证明文/密文，只有计数与字段名。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# 与 registry_crypto 同约定（单一事实源：仅常量复用，不碰其单例密钥缓存）
_ENC_PREFIX = "enc:v1:"
_SENSITIVE_KEYS = ("session_string", "two_fa_password", "session_secret")

# 迁移表清单：(库相对路径, 表名, 自然键列)。顺序即执行序（conversations 先于
# messages 仅为可读性；SQLite 无外键强制，两者独立幂等）。
MERGE_SPEC: List[Tuple[str, str, Tuple[str, ...]]] = [
    ("config/inbox.db", "conversations", ("conversation_id",)),
    ("config/inbox.db", "messages", ("message_id",)),
    ("config/inbox.db", "conversation_settings", ("conversation_id",)),
    ("config/translation_memory.db", "translation_memory", ("cache_key",)),
    # kb_entries.id 是内容哈希（两库天然不同），模板身份在 template_key
    # （UNIQUE WHERE != ''）——同包重播由唯一索引守卫兜住（skipped_guard）
    ("config/knowledge_base.db", "kb_entries", ("id",)),
    ("config/knowledge_base.db", "kb_error_codes", ("id",)),
    ("config/knowledge_base.db", "kb_rules", ("id",)),
    ("config/web_users.db", "web_users", ("username",)),
]

# platform_accounts 单独处理（换密 + business_line 标签）
_ACCOUNTS_DB = "config/account_registry.db"
_BUSINESS_LINE_MIGRATION = (
    "ALTER TABLE platform_accounts ADD COLUMN "
    "business_line TEXT NOT NULL DEFAULT ''"
)


def _connect(path: Path, readonly: bool) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _table_cols(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info([{table}])")]
    except sqlite3.Error:
        return []


def _unique_guards(
    dst: sqlite3.Connection, table: str, common: List[str],
) -> List[Tuple[str, List[str], bool]]:
    """目标表唯一索引自省 → [(索引名, 列, 是否部分索引)]，主键除外。

    自然键漏判时（2026-07-24 首切事故：kb_entries 按 id(内容哈希) 判重，
    模板身份实际在 UNIQUE(template_key)）插入会 IntegrityError 炸穿实写。
    guard = 插入前按目标库全部唯一索引再查一遍存在性，命中记 skipped_guard；
    干跑与实写同判 → 干跑预测数与实写行为一致，收敛校验才可信。

    partial 索引（如 WHERE template_key != ''）近似语义：任一 guard 列为
    NULL/空串 → 该行不在索引担保范围，不查（多空串行合法共存）。
    非 partial 索引 NULL 不参与唯一性（SQLite 语义），同样跳过。
    """
    guards: List[Tuple[str, List[str], bool]] = []
    try:
        for ix in dst.execute(f"PRAGMA index_list([{table}])").fetchall():
            keys = ix.keys()
            if not ix["unique"] or ("origin" in keys and ix["origin"] == "pk"):
                continue
            cols = [r["name"] for r in
                    dst.execute(f"PRAGMA index_info([{ix['name']}])")]
            if not cols or any(c not in common for c in cols):
                continue
            partial = bool(ix["partial"]) if "partial" in keys else False
            guards.append((str(ix["name"]), cols, partial))
    except sqlite3.Error:
        pass
    return guards


def _load_fernet(key_path: Path):
    """key 文件 → Fernet 实例；文件缺失/库缺失返回 None（调用方决定是否致命）。"""
    try:
        from cryptography.fernet import Fernet
    except Exception:
        return None
    try:
        raw = key_path.read_bytes().strip()
        return Fernet(raw) if raw else None
    except Exception:
        return None


def recrypt_meta_json(
    meta_json: str, src_fernet, dst_fernet
) -> Tuple[str, int, List[str]]:
    """meta_json 敏感字段 源钥→目标钥 换密。

    返回 (新 meta_json, 换密字段数, 解密失败字段名列表)。
    - 明文字段（无 enc: 前缀）→ 用目标钥加密（若有目标钥）；
    - 密文字段：源钥解开 → 目标钥重加密；解不开记入 failed（调用方跳过该账号）。
    """
    try:
        meta = json.loads(meta_json or "{}")
    except (json.JSONDecodeError, TypeError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    changed = 0
    failed: List[str] = []
    for k in _SENSITIVE_KEYS:
        v = meta.get(k)
        if not isinstance(v, str) or not v:
            continue
        if v.startswith(_ENC_PREFIX):
            if src_fernet is None:
                failed.append(k)
                continue
            try:
                plain = src_fernet.decrypt(
                    v[len(_ENC_PREFIX):].encode("ascii")).decode("utf-8")
            except Exception:
                failed.append(k)
                continue
        else:
            plain = v  # 旧明文行
        if dst_fernet is not None:
            meta[k] = _ENC_PREFIX + dst_fernet.encrypt(
                plain.encode("utf-8")).decode("ascii")
        else:
            meta[k] = plain
        changed += 1
    return json.dumps(meta, ensure_ascii=False), changed, failed


def merge_table(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    table: str,
    key_cols: Tuple[str, ...],
    *,
    apply: bool,
    transform: Optional[Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = None,
    after_insert: Optional[Callable[[sqlite3.Connection, Dict[str, Any]], None]] = None,
    exclude_cols: Tuple[str, ...] = (),
) -> Dict[str, Any]:
    """通用「不存在才插入」合并。transform 返回 None = 跳过该行（记 skipped_transform）。

    exclude_cols：自增代理键（platform_accounts.id / web_users.id）必须剔除，
    让目标库重新分配，否则源 id 与目标既有行撞 UNIQUE。
    """
    rep: Dict[str, Any] = {
        "table": table, "source_rows": 0, "target_before": 0,
        "inserted": 0, "skipped_existing": 0, "skipped_transform": 0,
        "skipped_guard": 0, "skipped_conflict": 0,
        "target_after": None, "ok": True, "notes": [],
    }
    src_cols = _table_cols(src, table)
    dst_cols = _table_cols(dst, table)
    if not src_cols:
        rep["notes"].append("source table missing")
        return rep
    if not dst_cols:
        rep["ok"] = False
        rep["notes"].append("target table missing")
        return rep
    if any(k not in src_cols or k not in dst_cols for k in key_cols):
        rep["ok"] = False
        rep["notes"].append(f"key cols {key_cols} not on both sides")
        return rep
    # 列交集，剔除 exclude_cols（自增代理键由目标库重新分配）
    common = [c for c in src_cols
              if c in dst_cols and c not in exclude_cols]
    rows = src.execute(f"SELECT * FROM [{table}]").fetchall()
    rep["source_rows"] = len(rows)
    rep["target_before"] = dst.execute(
        f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]
    where = " AND ".join(f"[{k}]=?" for k in key_cols)
    ins_sql = (
        f"INSERT INTO [{table}] ({', '.join('['+c+']' for c in common)}) "
        f"VALUES ({', '.join('?' for _ in common)})"
    )
    guards = _unique_guards(dst, table, common)
    for r in rows:
        d = {c: r[c] for c in common}
        exists = dst.execute(
            f"SELECT 1 FROM [{table}] WHERE {where}",
            tuple(d[k] for k in key_cols),
        ).fetchone()
        if exists:
            rep["skipped_existing"] += 1
            continue
        # 唯一索引守卫：自然键没抓住、但目标唯一索引会拒收的行 → 视为已存在
        # （同一模板/同一身份在两实例各自播种的场景），干跑/实写同判。
        g_hit = None
        for g_name, g_cols, g_partial in guards:
            vals = [d.get(c) for c in g_cols]
            if any(v is None for v in vals):
                continue
            if g_partial and any(isinstance(v, str) and v == "" for v in vals):
                continue
            if dst.execute(
                f"SELECT 1 FROM [{table}] WHERE "
                + " AND ".join(f"[{c}]=?" for c in g_cols),
                vals,
            ).fetchone():
                g_hit = g_name
                break
        if g_hit:
            rep["skipped_guard"] += 1
            continue
        if transform is not None:
            d2 = transform(dict(d))
            if d2 is None:
                rep["skipped_transform"] += 1
                continue
            d = {c: d2.get(c, d[c]) for c in common}
            # transform 可能补充目标独有列（如 business_line）
            extra = {k: v for k, v in d2.items()
                     if k in dst_cols and k not in common}
        else:
            extra = {}
        if apply:
            try:
                if extra:
                    cols_all = list(d.keys()) + list(extra.keys())
                    vals = list(d.values()) + list(extra.values())
                    dst.execute(
                        f"INSERT INTO [{table}] "
                        f"({', '.join('['+c+']' for c in cols_all)}) "
                        f"VALUES ({', '.join('?' for _ in cols_all)})",
                        vals,
                    )
                else:
                    dst.execute(ins_sql, [d[c] for c in common])
            except sqlite3.IntegrityError as ex:
                # 兜底安全网：guard 覆盖不到的约束（如带 WHERE 的复杂部分索引）
                # 绝不炸穿整个切换——跳过该行、如实点名；SQLite 语句级回滚，
                # 事务内其余行不受影响。收敛校验会因该行持续 pending 而变红，
                # 逼人工审视，不会静默丢失。
                rep["skipped_conflict"] += 1
                rep["notes"].append(
                    f"integrity skip key={tuple(d.get(k) for k in key_cols)}: {ex}")
                continue
            if after_insert is not None:
                try:
                    after_insert(dst, d)
                except Exception as ex:  # FTS 等附属写失败不阻断主迁移
                    rep["notes"].append(f"after_insert warn: {ex}")
        rep["inserted"] += 1
    if apply:
        dst.commit()
    rep["target_after"] = dst.execute(
        f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]
    if apply and rep["target_after"] != rep["target_before"] + rep["inserted"]:
        rep["ok"] = False
        rep["notes"].append("row count mismatch after insert")
    return rep


def _messages_fts_writer(dst: sqlite3.Connection, row: Dict[str, Any]) -> None:
    """messages 插入后同步 FTS（列集与 store 的 messages_fts 定义一致）。"""
    if not _table_cols(dst, "messages_fts"):
        return
    dst.execute(
        "INSERT INTO messages_fts (message_id, conversation_id, text, ts, direction) "
        "VALUES (?,?,?,?,?)",
        (row.get("message_id"), row.get("conversation_id"),
         row.get("text") or "", row.get("ts") or 0, row.get("direction") or ""),
    )


def merge_accounts(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    *,
    apply: bool,
    src_fernet,
    dst_fernet,
    same_key: bool,
    business_line: str,
) -> Dict[str, Any]:
    """platform_accounts 合并：换密 + business_line 标签 + 冲突跳过。"""
    rep = {"table": "platform_accounts", "recrypted_fields": 0,
           "credential_failures": []}
    # 目标库先补 business_line 列（与 account_registry._MIGRATIONS 同 SQL，幂等）
    if apply and "business_line" not in _table_cols(dst, "platform_accounts"):
        try:
            dst.execute(_BUSINESS_LINE_MIGRATION)
            dst.commit()
        except sqlite3.Error:
            pass

    def _transform(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if same_key:
            new_meta, n, failed = row.get("meta_json") or "{}", 0, []
        else:
            new_meta, n, failed = recrypt_meta_json(
                row.get("meta_json") or "{}", src_fernet, dst_fernet)
        if failed:
            rep["credential_failures"].append({
                "platform": row.get("platform"),
                "account_id": row.get("account_id"),
                "fields": failed,
            })
            return None  # 凭证解不开 → 不迁该账号（宁缺勿 garbage）
        rep["recrypted_fields"] += n
        out = dict(row)
        out["meta_json"] = new_meta
        out["business_line"] = business_line
        return out

    base = merge_table(
        src, dst, "platform_accounts", ("platform", "account_id"),
        apply=apply, transform=_transform, exclude_cols=("id",),
    )
    base.update({k: v for k, v in rep.items() if k != "table"})
    if rep["credential_failures"]:
        base["ok"] = False
        base["notes"].append("credential decrypt failures (skipped accounts)")
    return base


def run_merge(
    source_root: Path,
    target_root: Path,
    *,
    apply: bool = False,
    business_line: str = "translation",
    source_key: Optional[Path] = None,
    target_key: Optional[Path] = None,
    backup: bool = True,
) -> Dict[str, Any]:
    source_root = Path(source_root)
    target_root = Path(target_root)
    report: Dict[str, Any] = {
        "source": str(source_root), "target": str(target_root),
        "apply": apply, "business_line": business_line,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tables": [], "backups": [], "ok": True,
    }
    src_key_path = Path(source_key or source_root / "config" / "registry.key")
    dst_key_path = Path(target_key or target_root / "config" / "registry.key")
    src_fernet = _load_fernet(src_key_path)
    dst_fernet = _load_fernet(dst_key_path)
    same_key = False
    try:
        same_key = (src_key_path.read_bytes().strip()
                    == dst_key_path.read_bytes().strip())
    except Exception:
        pass
    report["registry_key"] = {
        "source_loaded": src_fernet is not None,
        "target_loaded": dst_fernet is not None,
        "same_key": same_key,
    }

    # 备份（仅 apply；干跑零写入零备份）
    dbs = sorted({rel for rel, _t, _k in MERGE_SPEC} | {_ACCOUNTS_DB})
    if apply and backup:
        ts = time.strftime("%Y%m%d_%H%M%S")
        for rel in dbs:
            p = target_root / rel
            if p.exists():
                bak = p.with_name(p.name + f".premerge-{ts}.bak")
                shutil.copy2(str(p), str(bak))
                report["backups"].append(str(bak))

    conns: Dict[Tuple[str, bool], sqlite3.Connection] = {}

    def _get(root: Path, rel: str, readonly: bool) -> Optional[sqlite3.Connection]:
        key = (str(root / rel), readonly)
        if key in conns:
            return conns[key]
        p = root / rel
        if not p.exists():
            return None
        c = _connect(p, readonly)
        conns[key] = c
        return c

    try:
        for rel, table, key_cols in MERGE_SPEC:
            src = _get(source_root, rel, True)
            if src is None:
                report["tables"].append(
                    {"table": table, "notes": ["source db missing"],
                     "source_rows": 0, "inserted": 0, "ok": True})
                continue
            # 干跑目标侧只读打开（连 PRAGMA 都不写），apply 才开写连接
            dst = _get(target_root, rel, not apply)
            if dst is None:
                report["tables"].append(
                    {"table": table, "notes": ["target db missing"],
                     "ok": False})
                report["ok"] = False
                continue
            after = _messages_fts_writer if table == "messages" else None
            # web_users 的 id 是自增代理键，剔除让目标重排
            excl = ("id",) if table == "web_users" else ()
            rep = merge_table(src, dst, table, key_cols,
                              apply=apply, after_insert=after,
                              exclude_cols=excl)
            report["tables"].append(rep)
            report["ok"] = report["ok"] and rep["ok"]

        # 账号（换密 + 标签）
        src_a = _get(source_root, _ACCOUNTS_DB, True)
        dst_a = _get(target_root, _ACCOUNTS_DB, not apply)
        if src_a is not None and dst_a is not None:
            has_enc = any(
                _ENC_PREFIX in (r["meta_json"] or "")
                for r in src_a.execute(
                    "SELECT meta_json FROM platform_accounts").fetchall()
            )
            if has_enc and not same_key and src_fernet is None:
                report["tables"].append({
                    "table": "platform_accounts", "ok": False,
                    "notes": ["source registry.key unusable but source has "
                              "encrypted credentials"],
                })
                report["ok"] = False
            else:
                rep = merge_accounts(
                    src_a, dst_a, apply=apply,
                    src_fernet=src_fernet, dst_fernet=dst_fernet,
                    same_key=same_key, business_line=business_line)
                report["tables"].append(rep)
                report["ok"] = report["ok"] and rep["ok"]
        elif src_a is not None and dst_a is None:
            report["tables"].append({"table": "platform_accounts",
                                     "ok": False,
                                     "notes": ["target registry missing"]})
            report["ok"] = False
    finally:
        for c in conns.values():
            try:
                c.close()
            except Exception:
                pass
    return report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", required=True, help="源实例数据根（被并入方，如通译）")
    ap.add_argument("--target", required=True, help="目标实例数据根（存续方，如智聊）")
    ap.add_argument("--apply", action="store_true",
                    help="实写（默认 dry-run 只读）")
    ap.add_argument("--business-line", default="translation",
                    help="导入账号打的业务线标签（默认 translation）")
    ap.add_argument("--source-key", default=None,
                    help="源 registry.key 路径（默认 <source>/config/registry.key）")
    ap.add_argument("--target-key", default=None,
                    help="目标 registry.key 路径（默认 <target>/config/registry.key）")
    ap.add_argument("--no-backup", action="store_true",
                    help="apply 时跳过目标库自动备份（不建议）")
    ap.add_argument("--report", default=None, help="报告 JSON 落盘路径")
    args = ap.parse_args(argv)

    report = run_merge(
        Path(args.source), Path(args.target),
        apply=bool(args.apply),
        business_line=str(args.business_line),
        source_key=Path(args.source_key) if args.source_key else None,
        target_key=Path(args.target_key) if args.target_key else None,
        backup=not args.no_backup,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
    print(text)
    if not report["ok"]:
        print("[merge] FAILED —— 见上方 notes", file=sys.stderr)
        return 1
    mode = "APPLIED" if report["apply"] else "DRY-RUN"
    total = sum(int(t.get("inserted") or 0) for t in report["tables"])
    print(f"[merge] {mode} ok —— 待插入/已插入 {total} 行", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
