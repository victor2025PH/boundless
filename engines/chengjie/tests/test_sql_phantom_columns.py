# -*- coding: utf-8 -*-
"""全库「幽灵 SQL」门禁（2026-08-01 幽灵列事故的泛化防线，V2 全库自动发现）。

事故：``conversation_meta`` 从未有过 ``claimed_by`` 列，但 Phase 34/35 的 SQL 直接
引用它 → `churn-risks` / `agent-qa-stats` 自出生起在**所有部署**上 500，两个月无人
发现（页面长期没人点开 + 没有任何测试真正执行那两条 SQL）。共性：**SQL 写错列名
不会在导入期/测试期报错，只在真实调用时炸**。

机制（V2；V2.1 扩 ``scripts/``+``tools/``——CLI/运维工具比路由更少被测试执行，
是幽灵的另一大藏身处，且它们打的是同一批实例库、schema 池零新增成本）：
1. **自动发现**：AST 扫 ``src|scripts|tools/**/*.py`` 全部完整字符串常量，
   DML（SELECT/INSERT/UPDATE/DELETE/WITH）进受检集——新文件零登记自动纳管；
2. **schema 自采**：同一轮扫描顺便收割 DDL 字面量（CREATE TABLE/INDEX/TRIGGER/
   VIRTUAL + ALTER ADD COLUMN）灌进一个 :memory: 联合库——store 的建表语句本身
   就是 schema 的唯一事实源，无需逐个实例化 40+ 个 store 类；
   同名异体表（两个模块建了不同结构的同名表）**整表投毒剔除**，宁可漏检不误报；
3. **真 store 地基**：Inbox/Contacts/Care/Entitlement 四大核心仍真实例化（全套
   DDL+迁移跑完的活连接），防「DDL 在运行时动态拼」类漏采；
4. **EXPLAIN 预编译判定**：sqlite prepare 期即校验列/表存在性，零执行零数据。

零假阳性设计（宁漏勿误，每条都有 V2 首跑实锤校准）：
- f-string（JoinedStr）天然不进受检集；参与 ``+`` 拼接的常量整体排除——
  实锤 `contacts/store.py:954` 的「SELECT 头 + base + 尾」拼接头会被 sqlite
  报成 no such column（外层别名悬空）而非语法错，且其子查询自带 FROM，
  任何「含 FROM 才算完整」启发式都挡不住，只有 AST 结构能精确识别；
- **子句完备性**：SELECT/DELETE 须含 FROM、UPDATE 须含 SET、INSERT 须含 INTO
  ——V2 首跑 20 处假阳性全是 i18n 英文文案（"Select a conversation" 被当 SQL
  解析成 no such column: a）。语义上零漏报代价：无 FROM 的 SELECT 根本引用
  不到表列，不可能是幽灵列案发现场；
- **动态迁移表豁免**：``f"ALTER TABLE kb_entries ADD COLUMN {col} …"`` 这类
  循环加列（kb_store / RPA state_store 家族惯用）静态收割不到列集 → 该表列
  集不可知 → 凡 SQL 文本涉及这些表的判定一律跳过（表级豁免，不是全局放水）。
  V2 首跑 5 处「疑似真凶」全属此类（对照生产库核实列真实存在）；
- docstring 排除（示例 SQL 不是运行时语句）；
- 判红需要「至少一处 no such column **且** 所有 schema 都编译不过」——
  表在全池都不存在（no such table）只说明池没覆盖到，不算证据；
- 命名参数（:name）语句跳过（本仓惯用 ``?``）。

已知漏报（诚实边界）：f-string / ``+`` 拼接构造的 SQL、动态迁移表上的语句
不在覆盖内——本门禁只守「完整字面量 × 静态可知 schema」这条窄而硬的不变量。
"""
from __future__ import annotations

import ast
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_REPO = Path(__file__).resolve().parent.parent
_SCAN_ROOTS = [_REPO / "src", _REPO / "scripts", _REPO / "tools"]

# 已知真缺陷但归属他线/待产品决策的登记簿：{(相对路径, 行号): "说明"}
# 修好后 test_pending_phantoms_still_broken 会点名要求移除（防过期）。
_PENDING_PHANTOMS: Dict[Tuple[str, int], str] = {}

_DML_RE = re.compile(r"^\s*(WITH|SELECT|INSERT|UPDATE|DELETE)\b", re.I)
_DDL_RE = re.compile(r"^\s*(CREATE\s+(TABLE|INDEX|UNIQUE\s+INDEX|TRIGGER|"
                     r"VIRTUAL\s+TABLE)|ALTER\s+TABLE)\b", re.I)
_CREATE_TABLE_NAME_RE = re.compile(
    r"CREATE\s+(?:VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"[\"'`\[]?(\w+)", re.I)
_NAMED_PARAM_RE = re.compile(r"(?<!:):[a-zA-Z_]\w*")
# 动态迁移标记：f-string/拼接片段里的「ALTER TABLE x ADD COLUMN」前缀 →
# 该表列集静态不可知，整表豁免判定
_DYN_ALTER_RE = re.compile(r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN", re.I)

# 子句完备性：动词 → 必须同现的关键字（无它则引用不到表列，非幽灵案发现场）
_CLAUSE_REQ = [
    (re.compile(r"^\s*SELECT\b", re.I), re.compile(r"\bFROM\b", re.I)),
    (re.compile(r"^\s*DELETE\b", re.I), re.compile(r"\bFROM\b", re.I)),
    (re.compile(r"^\s*UPDATE\b", re.I), re.compile(r"\bSET\b", re.I)),
    (re.compile(r"^\s*INSERT\b", re.I), re.compile(r"\bINTO\b", re.I)),
    (re.compile(r"^\s*WITH\b", re.I), re.compile(r"\bFROM\b", re.I)),
]


def _clause_complete(sql: str) -> bool:
    for verb, need in _CLAUSE_REQ:
        if verb.match(sql):
            return bool(need.search(sql))
    return True


def _docstring_linenos(tree: ast.AST) -> Set[int]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                out.add(body[0].value.lineno)
    return out


def _analyze_file(path: Path) -> Tuple[List[Tuple[int, str]], List[str], Set[str]]:
    """单文件单次 AST：返回 (DML 字面量[(行,SQL)], DDL 语句列表, 动态迁移表名集)。

    ``+`` 拼接成员整体排除：拼接头/尾是不完整语句，外层别名悬空会被 sqlite
    报成 no such column（与真幽灵列同 message），只有 AST 结构能精确区分。
    相邻字面量合并（"a" "b"）发生在解析期、不是 BinOp，完整长 SQL 不受影响。

    动态迁移表：``ALTER TABLE x ADD COLUMN`` 出现在 f-string 片段或 ``+`` 拼接
    片段里 ⇒ 该表在运行时被动态加列，静态收割不到完整列集 ⇒ 记入豁免集。
    （完整字面量 ALTER 正常收割，不豁免。）
    """
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except Exception:
        return [], [], set()
    doc_lines = _docstring_linenos(tree)
    frag_ids: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant):
                    frag_ids.add(id(sub))
    dyn_tables: Set[str] = set()
    # 表名也是变量的全动态迁移 f-string：``f"ALTER TABLE {table} ADD COLUMN {col} {decl}"``
    # ——常量片段只剩 "ALTER TABLE "，_DYN_ALTER_RE 抓不到表名。这类文件通常配一张
    # 迁移元组表 ``(("t", "col", "DECL"), …)`` 驱动循环（tiktok_huoke_bridge._MIGRATIONS
    # 实锤：kind / last_seen_ts 被判幽灵 72h）。检测到该形状 → 把文件里所有三元字符串
    # 元组合成完整 ALTER 收进 DDL，让联合库学到真列集（比整表豁免更保强度）。
    table_driven_alter = False
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for piece in node.values:
                if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                    m = _DYN_ALTER_RE.search(piece.value)
                    if m:
                        dyn_tables.add(m.group(1).lower())
            vals = node.values
            for i, piece in enumerate(vals[:-1]):
                if (isinstance(piece, ast.Constant) and isinstance(piece.value, str)
                        and re.search(r"ALTER\s+TABLE\s*$", piece.value, re.I)
                        and isinstance(vals[i + 1], ast.FormattedValue)):
                    table_driven_alter = True
    dml: List[Tuple[int, str]] = []
    ddl: List[str] = []
    if table_driven_alter:
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Tuple, ast.List)) or len(node.elts) != 3:
                continue
            if all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                   for e in node.elts):
                t, c, d = (e.value.strip() for e in node.elts)  # type: ignore[union-attr]
                if re.fullmatch(r"\w+", t) and re.fullmatch(r"\w+", c) and d:
                    ddl.append(f"ALTER TABLE {t} ADD COLUMN {c} {d}")
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        s = node.value
        if id(node) in frag_ids:
            m = _DYN_ALTER_RE.search(s)
            if m:
                dyn_tables.add(m.group(1).lower())
            continue
        if node.lineno in doc_lines:
            continue
        if _DDL_RE.match(s):
            ddl.append(s)
            continue
        if not _DML_RE.match(s):
            continue
        if not _clause_complete(s):
            continue
        if _NAMED_PARAM_RE.search(s.replace("::", "")):
            continue
        dml.append((node.lineno, s))
    return dml, ddl, dyn_tables


def _iter_src_files() -> Iterable[Path]:
    for root in _SCAN_ROOTS:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            yield p


class _Corpus:
    """全库扫描结果 + 联合 schema（session 级构建一次）。"""

    def __init__(self) -> None:
        self.dml_by_file: Dict[str, List[Tuple[int, str]]] = {}
        self.dyn_tables: Set[str] = set()
        all_ddl: List[str] = []
        for p in _iter_src_files():
            dml, ddl, dyn = _analyze_file(p)
            if dml:
                self.dml_by_file[str(p.relative_to(_REPO)).replace("\\", "/")] = dml
            all_ddl.extend(ddl)
            self.dyn_tables |= dyn
        self._dyn_res = [
            re.compile(r"\b" + re.escape(t) + r"\b", re.I) for t in self.dyn_tables
        ]

        # 同名异体表投毒：两个模块建了不同结构的同名表 → 整表剔除（列集合并
        # 会把「A 店的列」误判给「B 店的表」，宁可该表漏检不误报）。
        body_by_name: Dict[str, str] = {}
        self.poisoned: Set[str] = set()
        for stmt in all_ddl:
            m = _CREATE_TABLE_NAME_RE.search(stmt)
            if not m:
                continue
            name = m.group(1).lower()
            norm = " ".join(stmt.lower().split())
            if name in body_by_name and body_by_name[name] != norm:
                self.poisoned.add(name)
            else:
                body_by_name[name] = norm

        self.union = sqlite3.connect(":memory:")
        # 先建表/虚表，再补 ALTER/索引/触发器（依赖表存在）
        def _wanted(stmt: str, phase: int) -> bool:
            is_create_table = bool(_CREATE_TABLE_NAME_RE.search(stmt))
            return is_create_table if phase == 0 else not is_create_table

        for phase in (0, 1):
            for stmt in all_ddl:
                if not _wanted(stmt, phase):
                    continue
                m = _CREATE_TABLE_NAME_RE.search(stmt)
                if m and m.group(1).lower() in self.poisoned:
                    continue
                try:
                    self.union.executescript(stmt)
                except Exception:
                    # 重复列/依赖缺失/方言差异——联合库是 best-effort 补集，
                    # 判定安全性由「无处编译通过才红」保证，这里绝不抛。
                    pass

    def mentions_dyn_table(self, sql: str) -> bool:
        return any(r.search(sql) for r in self._dyn_res)

    def close(self) -> None:
        try:
            self.union.close()
        except Exception:
            pass


@pytest.fixture(scope="module")
def corpus():
    c = _Corpus()
    yield c
    c.close()


@pytest.fixture(scope="module")
def schema_pool(tmp_path_factory, corpus):
    """判定池＝联合自采 schema + 四大核心 store 真实例（DDL+迁移全跑）。"""
    tmp = tmp_path_factory.mktemp("phantom_sql")
    stores = []
    pool: List[sqlite3.Connection] = [corpus.union]

    from src.inbox.store import InboxStore
    st = InboxStore(tmp / "inbox.db")
    stores.append(st)
    pool.append(st._conn)  # noqa: SLF001

    from src.contacts.store import ContactStore
    cs = ContactStore(db_path=tmp / "contacts.db")
    stores.append(cs)
    pool.append(cs._conn)  # noqa: SLF001

    for factory in (
        lambda: __import__("src.contacts.care_schedule", fromlist=["x"])
        .CareScheduleStore(":memory:"),
        lambda: __import__("src.utils.entitlement_store", fromlist=["x"])
        .EntitlementStore(":memory:"),
    ):
        try:
            obj = factory()
            conn = getattr(obj, "_conn", None)
            if conn is not None:
                stores.append(obj)
                pool.append(conn)
        except Exception:
            pass

    yield pool
    for s in stores:
        try:
            s.close()
        except Exception:
            pass


def _explain_verdict(pool: List[sqlite3.Connection], sql: str):
    """返回 (ok_somewhere, no_such_column_msgs)。语法错/缺表不算幽灵证据。"""
    n_params = sql.count("?")
    msgs: List[str] = []
    for conn in pool:
        try:
            conn.execute("EXPLAIN " + sql, tuple([None] * n_params))
            return True, []
        except sqlite3.OperationalError as e:
            msgs.append(str(e))
        except Exception as e:  # noqa: BLE001 — 绑定数不符等一律不当证据
            msgs.append(f"(skip) {e}")
    return False, [m for m in msgs if "no such column" in m]


def test_no_phantom_sql_columns(corpus, schema_pool):
    """src 全库每条完整 SQL 字面量必须能在至少一个真 schema 上预编译通过。"""
    failures: List[str] = []
    scanned = 0
    for rel, literals in sorted(corpus.dml_by_file.items()):
        for lineno, sql in literals:
            scanned += 1
            ok, ns_col = _explain_verdict(schema_pool, sql)
            if ok or not ns_col:
                continue
            if corpus.mentions_dyn_table(sql):
                continue  # 动态迁移表：列集静态不可知，表级豁免（见文件头）
            if (rel, lineno) in _PENDING_PHANTOMS:
                continue
            head = " ".join(sql.split())[:90]
            failures.append(f"{rel}:{lineno}  {ns_col[0]}  «{head}»")
    assert scanned >= 300, f"扫描样本异常偏少（{scanned}），抽取器可能失效"
    assert not failures, (
        "发现幽灵 SQL（引用不存在的列——真实调用时必 OperationalError，"
        "参见 2026-08-01 claimed_by 事故）：\n  " + "\n  ".join(failures)
    )


def test_pending_phantoms_still_broken(corpus, schema_pool):
    """防登记簿过期：已修复的条目必须从 _PENDING_PHANTOMS 移除，恢复门禁强度。"""
    stale = []
    for (rel, lineno), note in _PENDING_PHANTOMS.items():
        hit = None
        for ln, sql in corpus.dml_by_file.get(rel, []):
            if ln == lineno:
                hit = sql
                break
        if hit is None:
            stale.append(f"{rel}:{lineno} 该行已无 SQL（{note}）")
            continue
        ok, ns_col = _explain_verdict(schema_pool, hit)
        if ok or not ns_col:
            stale.append(f"{rel}:{lineno} 已可预编译（{note}）")
    assert not stale, "以下登记项已不再是幽灵 SQL，请从 _PENDING_PHANTOMS 移除：\n  " \
        + "\n  ".join(stale)


def test_detector_catches_planted_phantom(schema_pool, tmp_path):
    """探测器有效性自证：埋一个幽灵列样本，抽取+判定必须抓到（门禁不是摆设）。"""
    planted = tmp_path / "planted.py"
    planted.write_text(
        'SQL = """SELECT cm.claimed_by_never_exists FROM conversation_meta cm"""\n',
        encoding="utf-8",
    )
    lits, _, _ = _analyze_file(planted)
    assert len(lits) == 1
    ok, ns_col = _explain_verdict(schema_pool, lits[0][1])
    assert not ok and ns_col, "植入的幽灵列样本未被判红——探测器失效"


def test_false_positive_classes_excluded(tmp_path):
    """四类实锤假阳性来源全部不进受检集/判定：
    拼接片段 / docstring / 无 FROM 的 i18n 文案 / 动态迁移表标记提取。"""
    planted = tmp_path / "frag.py"
    planted.write_text(
        'def f(base, col):\n'
        '    """SELECT ghost FROM nowhere"""\n'
        '    q = "SELECT c.x, c.y " + base + " LIMIT 1"\n'
        '    ui = "Select a conversation"\n'
        '    mig = f"ALTER TABLE kb_entries ADD COLUMN {col} TEXT"\n'
        '    return q, ui, mig\n',
        encoding="utf-8",
    )
    lits, _ddl, dyn = _analyze_file(planted)
    assert lits == []
    assert dyn == {"kb_entries"}


def test_table_driven_migration_tuples_harvested(tmp_path):
    """``f\"ALTER TABLE {t} ADD COLUMN {c} {d}\"`` + 三元迁移表 → DDL 收割到列。

    2026-09 tiktok_huoke_bridge._MIGRATIONS 形状：表名变量导致 _DYN_ALTER_RE
    抓不到表名、整表又不该豁免——只能把元组合成完整 ALTER 灌进联合库。
    """
    planted = tmp_path / "mig.py"
    planted.write_text(
        '_MIGRATIONS = (\n'
        '    ("lead_accounts", "last_seen_ts", "REAL NOT NULL DEFAULT 0"),\n'
        '    ("outbound", "kind", "TEXT NOT NULL DEFAULT \'comment\'"),\n'
        ')\n'
        'def migrate(conn, table, col, decl):\n'
        '    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")\n',
        encoding="utf-8",
    )
    _lits, ddl, dyn = _analyze_file(planted)
    assert dyn == set()  # 表名是变量，不能走整表豁免
    joined = "\n".join(ddl).lower()
    assert "lead_accounts" in joined and "last_seen_ts" in joined
    assert "outbound" in joined and "add column kind" in joined


def test_union_schema_harvest_and_poisoning(tmp_path, corpus):
    """DDL 自采联合库确实建出了非四大 store 的表（覆盖面自证）+ 投毒表被剔除。"""
    # goals store 只在联合库里（未实例化）——它的表必须可查
    row = corpus.union.execute(
        "SELECT name FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
    assert row is not None
    n_tables = corpus.union.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    assert n_tables >= 40, f"联合库表数异常偏少（{n_tables}），DDL 收割可能失效"
    for name in corpus.poisoned:
        r = corpus.union.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND lower(name)=?",
            (name,)).fetchone()
        assert r is None, f"投毒表 {name} 不应存在于联合库"