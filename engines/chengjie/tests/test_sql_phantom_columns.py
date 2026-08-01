# -*- coding: utf-8 -*-
"""全库「幽灵 SQL」门禁（2026-08-01 幽灵列事故的泛化防线）。

事故：``conversation_meta`` 从未有过 ``claimed_by`` 列，但 Phase 34/35 的 SQL 直接
引用它 → `churn-risks` / `agent-qa-stats` 自出生起在**所有部署**上 500，两个月无人
发现（页面长期没人点开 + 没有任何测试真正执行那两条 SQL）。同类缺陷的共性：
**SQL 写错列名/表名不会在导入期或测试期报错，只在真实调用时炸**。

本门禁把这一类整体收口：AST 抽取受检文件里的 SQL 字符串字面量，对**真实 schema**
（各 store 全套 DDL+迁移后的连接）做 ``EXPLAIN`` 预编译——sqlite 在预编译期即校验
列/表存在性，无需真实数据、无副作用。

零假阳性设计（宁漏勿误）：
- 只扫**完整**字符串常量：f-string 天然跳过（JoinedStr）；参与 ``+`` 拼接的
  常量整体排除——首跑实锤 `contacts/store.py:954` 的「SELECT 头 + base + 尾」
  拼接头会被 sqlite 报成 no such column（外层别名悬空）而非语法错，
  且其子查询自带 FROM，任何「含 FROM 才算完整」的启发式都挡不住，
  只有 AST 结构（BinOp 成员）能精确识别；docstring 同样排除（示例非运行时语句）；
- 只把「至少一个 schema 报 no such column，且**没有任何** schema 能预编译通过」
  判红——查别家 store 表的语句在本池找不到表（no such table）不算证据；
- 命名参数（:name）语句直接跳过（本仓惯用 ``?``）。

已知漏报（诚实边界）：f-string / ``+`` 拼接构造的 SQL 不在覆盖内——本门禁
守「完整字面量」这条窄而硬的不变量；动态构造类靠 code review 与运行时测试。
"""
from __future__ import annotations

import ast
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_REPO = Path(__file__).resolve().parent.parent

# 受检文件（SQL 密集 + 跨文件消费 store 内部 schema 的高危区；加文件零成本）
_SCAN_FILES = [
    "src/inbox/store.py",
    "src/contacts/store.py",
    "src/contacts/care_schedule.py",
    "src/contacts/winback_stats.py",
    "src/contacts/journey_seed.py",
    "src/contacts/contact_backfill.py",
    "src/contacts/inbox_enrichment.py",
    "src/skills/intimacy_engine.py",
    "src/skills/reactivation_scheduler.py",
    "src/utils/entitlement_store.py",
    "src/web/routes/contacts_routes.py",
]

# 已知真缺陷但归属他线/待产品决策的登记簿：{(相对路径, 行号): "说明"}
# 修好后 test_pending_phantoms_still_broken 会点名要求移除（防过期）。
_PENDING_PHANTOMS: Dict[Tuple[str, int], str] = {}

_DML_RE = re.compile(r"^\s*(WITH|SELECT|INSERT|UPDATE|DELETE)\b", re.I)
_NAMED_PARAM_RE = re.compile(r"(?<!:):[a-zA-Z_]\w*")


def _docstring_linenos(tree: ast.AST) -> set:
    """收集 module/class/def 的 docstring 节点行号（示例 SQL 不是运行时语句）。"""
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


def _extract_sql_literals(path: Path) -> List[Tuple[int, str]]:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    doc_lines = _docstring_linenos(tree)
    # ``+`` 拼接成员整体排除：拼接头/尾是不完整语句，外层别名悬空会被 sqlite
    # 报成 no such column（与真幽灵列同 message），只有 AST 结构能精确区分。
    # 注意：相邻字面量合并（"a" "b"）在解析期完成、不是 BinOp，完整长 SQL 不受影响。
    frag_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant):
                    frag_ids.add(id(sub))
    out: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in frag_ids or node.lineno in doc_lines:
            continue
        s = node.value
        if not _DML_RE.match(s):
            continue
        if _NAMED_PARAM_RE.search(s.replace("::", "")):
            continue  # 命名参数语句（本仓罕见）不猜绑定
        out.append((node.lineno, s))
    return out


@pytest.fixture(scope="module")
def schema_pool(tmp_path_factory):
    """真 schema 连接池：各 store 全套 DDL+迁移后的活连接（EXPLAIN 用）。"""
    tmp = tmp_path_factory.mktemp("phantom_sql")
    stores = []
    pool: Dict[str, sqlite3.Connection] = {}

    from src.inbox.store import InboxStore
    st = InboxStore(tmp / "inbox.db")
    stores.append(st)
    pool["inbox"] = st._conn  # noqa: SLF001

    from src.contacts.store import ContactStore
    cs = ContactStore(db_path=tmp / "contacts.db")
    stores.append(cs)
    pool["contacts"] = cs._conn  # noqa: SLF001

    try:
        from src.contacts.care_schedule import CareScheduleStore
        care = CareScheduleStore(":memory:")
        conn = getattr(care, "_conn", None)
        if conn is not None:
            stores.append(care)
            pool["care"] = conn
    except Exception:
        pass
    try:
        from src.utils.entitlement_store import EntitlementStore
        ent = EntitlementStore(":memory:")
        conn = getattr(ent, "_conn", None)
        if conn is not None:
            stores.append(ent)
            pool["entitlement"] = conn
    except Exception:
        pass

    yield pool
    for s in stores:
        try:
            s.close()
        except Exception:
            pass


def _explain_verdict(pool: Dict[str, sqlite3.Connection], sql: str):
    """返回 (ok_somewhere, no_such_column_msgs)。语法错/缺表不算幽灵证据。"""
    n_params = sql.count("?")
    msgs: List[str] = []
    for conn in pool.values():
        try:
            conn.execute("EXPLAIN " + sql, tuple([None] * n_params))
            return True, []
        except sqlite3.OperationalError as e:
            msgs.append(str(e))
        except Exception as e:  # noqa: BLE001 — 绑定数不符等一律不当证据
            msgs.append(f"(skip) {e}")
    ns_col = [m for m in msgs if "no such column" in m]
    return False, ns_col


def test_no_phantom_sql_columns(schema_pool):
    """受检文件的每条 SQL 字面量必须能在至少一个真 schema 上预编译通过。"""
    failures: List[str] = []
    scanned = 0
    for rel in _SCAN_FILES:
        path = _REPO / rel
        if not path.is_file():
            continue
        for lineno, sql in _extract_sql_literals(path):
            scanned += 1
            ok, ns_col = _explain_verdict(schema_pool, sql)
            if ok or not ns_col:
                continue
            if (rel, lineno) in _PENDING_PHANTOMS:
                continue
            head = " ".join(sql.split())[:90]
            failures.append(f"{rel}:{lineno}  {ns_col[0]}  «{head}»")
    assert scanned >= 100, f"扫描样本异常偏少（{scanned}），抽取器可能失效"
    assert not failures, (
        "发现幽灵 SQL（引用不存在的列——该语句在真实调用时必 OperationalError，"
        "参见 2026-08-01 claimed_by 事故）：\n  " + "\n  ".join(failures)
    )


def test_pending_phantoms_still_broken(schema_pool):
    """防登记簿过期：已修复的条目必须从 _PENDING_PHANTOMS 移除，恢复门禁强度。"""
    stale = []
    for (rel, lineno), note in _PENDING_PHANTOMS.items():
        path = _REPO / rel
        if not path.is_file():
            stale.append(f"{rel}:{lineno} 文件不存在（{note}）")
            continue
        hit = None
        for ln, sql in _extract_sql_literals(path):
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
    lits = _extract_sql_literals(planted)
    assert len(lits) == 1
    ok, ns_col = _explain_verdict(schema_pool, lits[0][1])
    assert not ok and ns_col, "植入的幽灵列样本未被判红——探测器失效"


def test_docstring_examples_not_scanned(tmp_path):
    """docstring 里的示例 SQL 不进扫描（文档不是运行时语句，防假阳性）。"""
    planted = tmp_path / "doc.py"
    planted.write_text(
        'def f():\n    """SELECT ghost_col FROM nowhere"""\n    return 1\n',
        encoding="utf-8",
    )
    assert _extract_sql_literals(planted) == []
