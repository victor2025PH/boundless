# -*- coding: utf-8 -*-
r"""platform/licensing/audit_orders_schema.py — 支付相关表 schema 复核工具（纯 stdlib，只读）。

背景：第五阶段（2026-07-20）在智控王本机的 `tgmatrix.db` 上发现并修复了 orders/coupons
表的列名不匹配问题（详见 LICENSE_CONTRACT.md §6①）。修复只改了 `license_server.py`
的代码，**没有改任何数据库文件**——本工具就是用来在**任意一台**部署了智控王的机器上，
重跑同一套核实逻辑：给出一个 tgmatrix.db 路径，工具只读打开它，对照本工具内置的
「修复后代码期望的列名清单」逐一核对，报告这台机器上的真实 schema 是否与代码假设一致。

为什么需要这个工具（而不是每次都让 AI 重新读一遍代码+现场写查询）：
  - 无界文档提到过多机部署（五机集群），每台机器的 tgmatrix.db 是本机独立文件；
  - 2026-07-20 的核实与修复只覆盖了本机这一份数据库，**不代表其它机器的历史数据
    schema 一定相同**——如果某台机器在更早的代码版本下跑过、累积了真实订单，
    它的真实列名可能与"当前代码修复后期望的列名"不一致，直接照搬本机的修复结论
    去覆盖那台机器的数据是危险的；
  - 本工具让任何人（不需要会写 SQL）在新机器上快速自查，而不必等 AI 逐台核实。

绝对安全保证：
  - 全程只用 `sqlite3.connect("file:...?mode=ro", uri=True)` 只读 URI 模式打开，
    物理上不可能写入或修改被检查的数据库文件；
  - 不读取任何一行业务数据内容（不 SELECT 任何用户/订单明细），只读取
    `sqlite_master`（建表语句）与 `PRAGMA table_info`（列结构）+ `COUNT(*)`（行数），
    不会把任何客户隐私数据打印出来。

用法：
    python audit_orders_schema.py <tgmatrix.db的路径>
    python audit_orders_schema.py "\\某机器\共享路径\backend\data\tgmatrix.db"
    python audit_orders_schema.py --selftest    # 用临时造的库自测工具本身
"""
from __future__ import annotations

import sqlite3
import sys
from typing import Dict, List, Optional, Tuple

# 2026-07-20 修复后，license_server.py 实际读写的列名（单一真相；如果后续代码
# 又变了，这里要跟着更新，否则本工具的比对基准会过期）。
EXPECTED_COLUMNS: Dict[str, List[str]] = {
    "orders": [
        "order_id", "user_id", "product_type", "product_level", "product_duration",
        "product_name", "original_price", "discount_amount", "final_price",
        "currency", "payment_method", "payment_gateway", "transaction_id",
        "status", "license_key", "coupon_code", "referrer_code",
        "created_at", "paid_at", "expired_at", "refunded_at",
        "ip_address", "user_agent", "gateway_response",
    ],
    "coupons": [
        "coupon_code", "name", "discount_type", "discount_value",
        "min_order_amount", "max_discount_amount", "applicable_levels",
        "applicable_durations", "total_count", "used_count", "per_user_limit",
        "start_at", "expire_at", "created_at", "is_active",
    ],
    "licenses": [
        "license_key", "type_code", "level", "duration_type", "duration_days",
        "price", "status", "used_by", "used_at", "machine_id", "activated_at",
        "expires_at", "batch_id", "notes", "created_at", "created_by",
    ],
}

# 已知的、历史上出现过的"错误列名猜测"，命中即高危信号（说明这台机器可能还在跑
# 修复前的旧代码，或者数据是旧代码写入的历史遗留）。
KNOWN_BAD_COLUMNS: Dict[str, List[str]] = {
    "orders": ["product_id", "duration_type", "duration_days", "coupon_id", "tx_hash", "paid_amount"],
    "coupons": ["code", "status", "expires_at", "max_uses", "min_amount", "created_by"],
}


def _real_columns(cur: sqlite3.Cursor, table: str) -> Optional[List[str]]:
    cur.execute(f"PRAGMA table_info({table})")
    rows = cur.fetchall()
    if not rows:
        return None
    return [r[1] for r in rows]  # PRAGMA table_info 第 2 列(index 1)是列名


def audit(db_path: str) -> Tuple[bool, List[str]]:
    """核对一个 tgmatrix.db 文件。返回 (是否全部健康, 报告文本行列表)。"""
    lines: List[str] = []
    all_ok = True
    uri = f"file:{db_path}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.OperationalError as e:
        return False, [f"无法只读打开数据库: {db_path}", f"  错误: {e}",
                       "  （文件不存在，或路径含特殊字符需要转义，或被其它进程独占锁定）"]

    try:
        cur = con.cursor()
        lines.append(f"数据库: {db_path}")
        lines.append("")

        for table, expected in EXPECTED_COLUMNS.items():
            real = _real_columns(cur, table)
            lines.append(f"-- 表 {table} --")
            if real is None:
                lines.append(f"  ⚠ 表不存在（可能是全新空库，还没初始化过；或表名拼写在别处不同）")
                all_ok = False
                lines.append("")
                continue

            real_set = set(real)
            missing = [c for c in expected if c not in real_set]
            bad_present = [c for c in KNOWN_BAD_COLUMNS.get(table, []) if c in real_set]

            if not missing and not bad_present:
                lines.append(f"  ✓ 健康：代码期望的 {len(expected)} 列全部存在，未发现已知错误列名残留")
            else:
                all_ok = False
                if missing:
                    lines.append(f"  ✗ 缺少代码期望的列: {missing}")
                    lines.append(f"    （2026-07-20 修复后的代码会在读/写这些列时报错，"
                                 f"这台机器可能还在跑旧代码，或数据库是更早版本建的）")
                if bad_present:
                    lines.append(f"  ⚠ 发现已知的历史错误列名: {bad_present}")
                    lines.append(f"    （说明这台机器的表结构来自修复前的版本；"
                                 f"如果这些列有真实历史数据，不要直接照搬本机的修复方案覆盖，"
                                 f"应先评估这些列里的数据要不要迁移）")

            try:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                count = cur.fetchone()[0]
                lines.append(f"  历史行数: {count}"
                             + ("（0 行=可安全修复，无迁移负担）" if count == 0 else
                                "（⚠ 有真实历史数据，任何 schema 改动前必须先想清楚迁移方案）"))
            except sqlite3.OperationalError:
                pass
            lines.append("")

        # 顺手看一下 sqlite_master 里这几张表的真实建表语句，供人工比对存档
        lines.append("-- 真实建表语句（存档用，来自 sqlite_master）--")
        for table in EXPECTED_COLUMNS:
            cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))
            row = cur.fetchone()
            if row:
                lines.append(f"  [{table}]")
                for l in row[0].splitlines():
                    lines.append(f"    {l}")
    finally:
        con.close()

    return all_ok, lines


def main(argv: List[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    if argv[:1] == ["--selftest"]:
        return _selftest()

    if len(argv) != 1:
        print(__doc__)
        return 2

    ok, lines = audit(argv[0])
    print("== platform/licensing 支付表 schema 核实工具 ==")
    print("（只读打开，绝不写入；不读取任何业务数据内容，只看表结构）")
    print()
    for l in lines:
        print(l)
    print()
    if ok:
        print("== 结论：这台机器的 schema 与 2026-07-20 修复后的代码期望一致 ✓ ==")
        return 0
    else:
        print("== 结论：这台机器的 schema 与代码期望不一致，需要人工评估（见上方 ✗/⚠ 标记）==")
        return 1


def _selftest() -> int:
    import os
    import tempfile

    failures: List[str] = []

    def check(desc: str, ok: bool) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            failures.append(desc)

    print("== audit_orders_schema.py 自测 ==")

    print("[1/3] 健康库（列名与代码期望完全一致）-> 判定健康")
    with tempfile.TemporaryDirectory(prefix="audit_selftest_healthy_") as d:
        db = os.path.join(d, "healthy.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE orders (" + ", ".join(f"{c} TEXT" for c in EXPECTED_COLUMNS["orders"]) + ")")
        con.execute("CREATE TABLE coupons (" + ", ".join(f"{c} TEXT" for c in EXPECTED_COLUMNS["coupons"]) + ")")
        con.execute("CREATE TABLE licenses (" + ", ".join(f"{c} TEXT" for c in EXPECTED_COLUMNS["licenses"]) + ")")
        con.commit()
        con.close()
        ok, lines = audit(db)
        check("健康库判定为 ok=True", ok is True)
        check("报告含健康标记", any("✓ 健康" in l for l in lines))

    print("[2/3] 修复前的旧 schema（缺列+含已知错误列名）-> 判定不健康且精确指出问题")
    with tempfile.TemporaryDirectory(prefix="audit_selftest_broken_") as d:
        db = os.path.join(d, "broken.db")
        con = sqlite3.connect(db)
        # 模拟修复前的真实历史 schema（原样照抄第五阶段发现的旧列名）
        con.execute("""CREATE TABLE orders (
            id INTEGER PRIMARY KEY, order_id TEXT, user_id TEXT,
            product_type TEXT, product_level TEXT, product_duration TEXT,
            product_name TEXT, original_price REAL, discount_amount REAL,
            final_price REAL, currency TEXT, payment_method TEXT,
            payment_gateway TEXT, transaction_id TEXT, status TEXT,
            license_key TEXT, coupon_code TEXT, referrer_code TEXT,
            created_at TEXT, paid_at TEXT, expired_at TEXT, refunded_at TEXT,
            ip_address TEXT, user_agent TEXT, gateway_response TEXT,
            tx_hash TEXT, paid_amount REAL
        )""")  # 这个例子实际列是齐的+多两个旧列，测试"bad_present"分支
        con.execute("CREATE TABLE coupons (id INTEGER PRIMARY KEY, code TEXT, status TEXT)")  # 严重缺列
        con.commit()
        con.close()
        ok, lines = audit(db)
        check("不健康库判定为 ok=False", ok is False)
        check("报告标出 orders 里的已知错误列名 tx_hash/paid_amount",
              any("tx_hash" in l and "paid_amount" in l for l in lines))
        check("报告标出 coupons 缺失代码期望列", any("缺少代码期望的列" in l for l in lines))
        check("报告标出 licenses 表不存在", any("表不存在" in l for l in lines))

    print("[3/3] 数据库文件不存在 -> 优雅报错，不崩溃")
    ok, lines = audit(os.path.join(tempfile.gettempdir(), "audit_selftest_does_not_exist.db"))
    check("不存在的文件判定为 ok=False（不抛异常）", ok is False)
    check("报告含无法打开的说明", any("无法只读打开" in l for l in lines))

    if failures:
        print(f"\n== 结果：{len(failures)} 项失败 ==")
        return 1
    print("\n== 结果：全部通过 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
