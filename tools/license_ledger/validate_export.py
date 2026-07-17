# -*- coding: utf-8 -*-
"""validate_export.py — 导出 JSON 的最小 schema 自检（纯标准库，不装 jsonschema）。

与 platform/licensing/ledger/ledger_import.schema.json 同口径的手写断言，
外加 schema 表达不了的两条：records[*].source_system 与顶层一致、source_key 系统内唯一。

用法：python tools/license_ledger/validate_export.py <导出json> [<导出json> ...]
全部通过退出码 0；任一违规打印明细并退出码 1。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:  # GBK 控制台防中文/符号炸 print（与 engines 侧脚本同处理）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SOURCE_SYSTEMS = {"avatarhub", "chengjie"}
PRODUCT_IDS = {"huansheng", "huanyan", "huanying", "tongchuan",
               "tongyi", "zhiliao", "zhituo"}
STATUSES = {"active", "expired", "revoked", "trial", "unknown"}
RECORD_FIELDS = [
    "source_system", "source_key", "product_id", "sku_id", "plan", "edition",
    "seats", "customer_name", "customer_contact", "machine_fingerprint",
    "issued_at", "expires_at", "status", "raw",
]
# ISO8601（date-time）宽松匹配：2026-07-18T04:00:00(+00:00|Z|±hh:mm)?(.ffffff)?
_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$")


def _is_iso(v) -> bool:
    return isinstance(v, str) and bool(_ISO_RE.match(v))


def _opt_str(v) -> bool:
    return v is None or isinstance(v, str)


def validate_record(r, idx: int, top_system: str, seen_keys: set) -> list:
    errs = []
    e = lambda m: errs.append(f"records[{idx}]: {m}")
    if not isinstance(r, dict):
        return [f"records[{idx}]: 不是对象"]
    missing = [k for k in RECORD_FIELDS if k not in r]
    if missing:
        e(f"缺少必填键 {missing}")
    extra = [k for k in r if k not in RECORD_FIELDS]
    if extra:
        e(f"含未定义键 {extra}（additionalProperties=false）")
    if r.get("source_system") not in SOURCE_SYSTEMS:
        e(f"source_system 非法: {r.get('source_system')!r}")
    elif r["source_system"] != top_system:
        e(f"source_system={r['source_system']!r} 与顶层 {top_system!r} 不一致")
    sk = r.get("source_key")
    if not isinstance(sk, str) or not sk:
        e(f"source_key 须为非空字符串: {sk!r}")
    elif sk in seen_keys:
        e(f"source_key 重复: {sk!r}")
    else:
        seen_keys.add(sk)
    if not (r.get("product_id") is None or r.get("product_id") in PRODUCT_IDS):
        e(f"product_id 非法: {r.get('product_id')!r}")
    for k in ("sku_id", "plan", "edition", "customer_name",
              "customer_contact", "machine_fingerprint"):
        if not _opt_str(r.get(k)):
            e(f"{k} 须为字符串或 null: {r.get(k)!r}")
    seats = r.get("seats")
    if not (seats is None or (isinstance(seats, int)
                              and not isinstance(seats, bool) and seats >= 0)):
        e(f"seats 须为 >=0 整数或 null: {seats!r}")
    for k in ("issued_at", "expires_at"):
        if not (r.get(k) is None or _is_iso(r.get(k))):
            e(f"{k} 须为 ISO8601 或 null: {r.get(k)!r}")
    if r.get("status") not in STATUSES:
        e(f"status 非法: {r.get('status')!r}")
    if not isinstance(r.get("raw"), dict):
        e(f"raw 须为对象: {type(r.get('raw')).__name__}")
    return errs


def validate_file(path: Path) -> list:
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as ex:
        return [f"JSON 解析失败: {ex}"]
    errs = []
    if not isinstance(doc, dict):
        return ["顶层不是对象"]
    for k in ("version", "source_system", "exported_at", "records"):
        if k not in doc:
            errs.append(f"顶层缺少必填键 {k}")
    extra = [k for k in doc if k not in
             ("version", "source_system", "exported_at", "records")]
    if extra:
        errs.append(f"顶层含未定义键 {extra}（additionalProperties=false）")
    if doc.get("version") != 1:
        errs.append(f"version 须为 1: {doc.get('version')!r}")
    top_system = doc.get("source_system")
    if top_system not in SOURCE_SYSTEMS:
        errs.append(f"source_system 非法: {top_system!r}")
    if not _is_iso(doc.get("exported_at")):
        errs.append(f"exported_at 须为 ISO8601: {doc.get('exported_at')!r}")
    recs = doc.get("records")
    if not isinstance(recs, list):
        errs.append("records 须为数组")
        return errs
    seen: set = set()
    for i, r in enumerate(recs):
        errs.extend(validate_record(r, i, top_system, seen))
    return errs


def main(argv: list) -> int:
    if not argv:
        print("用法: python validate_export.py <导出json> [...]", file=sys.stderr)
        return 2
    bad = 0
    for arg in argv:
        p = Path(arg)
        errs = validate_file(p)
        if errs:
            bad += 1
            print(f"[validate] FAIL {p}")
            for m in errs:
                print(f"    - {m}")
        else:
            try:
                n = len(json.loads(p.read_text(encoding="utf-8-sig"))["records"])
            except Exception:
                n = "?"
            print(f"[validate] OK {p}（{n} 条记录，符合 ledger_import.schema.json v1）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
