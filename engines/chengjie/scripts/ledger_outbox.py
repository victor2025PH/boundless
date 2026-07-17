#!/usr/bin/env python3
"""签发即台账：chengjie 授权签发 outbox（P1 收尾，仅标准库）。

背景：``scripts/license_tool.py issue`` 产出 token 打印即走，签发端不留任何台账
（platform/licensing/ledger/README.md §5.2、§7 已知局限 5）。本模块提供「签发即导出」
钩子：签发成功后把 payload 归一化为集团台账 v1 记录
（platform/licensing/ledger/ledger_import.schema.json），append 一行到本地 outbox JSONL。

- 默认 outbox：``engines/chengjie/config/ledger_outbox.jsonl``（相对本文件定位，与 cwd
  无关）；环境变量 ``CHENGJIE_LEDGER_OUTBOX`` 可覆盖。含客户名等经营数据，已 gitignore。
- 安全：记录绝不含 token 原文 / 签名 / 私钥；仅保留 payload 非敏感字段 + token 的
  sha256 摘要（与 tools/license_ledger/export_chengjie.py 同口径）。
- source_key 对齐 export_chengjie：``payload.lic_id`` 优先；缺失时
  ``token:<sha256 前 16 位>``；连 token 也没有时 ``payload:<规范化 payload sha256 前 16 位>``。
  故同一授权经「outbox 实时记录」与「回收客户 key 补录」两条路进集团账本会 upsert 合并。
- ``record_issue`` 全程 fail-silent（只返回 bool，绝不抛异常、绝不打印）——台账钩子
  不得影响签发主流程与其 stdout。

用法::

    python scripts/ledger_outbox.py --selftest                # 临时目录自测（不碰真实 outbox）
    python scripts/ledger_outbox.py --export out.json         # outbox → 台账导入 JSON（v1）
    python scripts/ledger_outbox.py --export out.json --input D:/collected/outbox.jsonl

--export 产物过 tools/license_ledger/validate_export.py 自检后，交
website/scripts/ledger-import-licenses.mjs 导入（幂等键 (source_system, source_key)）。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才重配控制台；被 license_tool 导入时零副作用
    try:  # GBK 控制台防中文炸 print（与 engines 侧脚本同处理）
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SOURCE_SYSTEM = "chengjie"
ENV_OUTBOX = "CHENGJIE_LEDGER_OUTBOX"
# 七产品矩阵（与 export_chengjie.PRODUCT_IDS 一致）：payload 带 product_id 时才透传
PRODUCT_IDS = {"huansheng", "huanyan", "huanying", "tongchuan",
               "tongyi", "zhiliao", "zhituo"}
# 兜底黑名单：正常 payload 不含这些键，防未来有人把凭证塞进 payload 后带进台账
_SENSITIVE_KEYS = {"token", "sig", "signature", "key", "license",
                   "priv", "private_hex", "private_key"}
_RECORD_FIELDS = [
    "source_system", "source_key", "product_id", "sku_id", "plan", "edition",
    "seats", "customer_name", "customer_contact", "machine_fingerprint",
    "issued_at", "expires_at", "status", "raw",
]


def _default_outbox_path() -> Path:
    env = os.environ.get(ENV_OUTBOX, "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "config" / "ledger_outbox.jsonl"


def _to_iso(ts) -> "str | None":
    """unix 秒 → ISO8601(UTC)；0/空/解析失败 → None（与 export_chengjie.to_iso 同口径）。"""
    try:
        v = float(ts)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    try:
        return datetime.fromtimestamp(v, timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _decide_status(payload: dict, status: "str | None" = None) -> str:
    """归一化状态（ledger README §4 口径的签发侧子集）。显式 status 优先
    （日后 renew/revoke 类命令挂钩时传入）；否则 expired > trial > active。"""
    if status:
        return str(status)
    try:
        exp = float(payload.get("exp") or 0)
    except (TypeError, ValueError):
        exp = 0.0
    if exp > 0 and time.time() > exp:
        return "expired"
    if bool(payload.get("trial", False)):
        return "trial"
    return "active"


def normalize_issue(payload: dict, token: "str | None" = None, *,
                    status: "str | None" = None) -> dict:
    """签发 payload → 集团台账 v1 归一化记录（14 字段全出现，无值用 null）。

    ``token`` 仅用于两件事：sha256 摘要（进 raw.token_sha256 / source_key 兜底）、
    解出**实际签名的 payload**（issue_license 会补齐 grace_days 等缺省，以 token 内
    为准可与 export_chengjie 对同一授权的产出完全一致）。token 原文绝不写入记录。
    """
    p = {k: v for k, v in dict(payload or {}).items() if k not in _SENSITIVE_KEYS}
    if token and "." in token:
        try:  # token 可解码时以其 payload 为准（含 issue_license 补齐的缺省字段）
            body = token.split(".", 1)[0]
            dec = json.loads(base64.urlsafe_b64decode(
                body + "=" * (-len(body) % 4)).decode("utf-8"))
            if isinstance(dec, dict):
                p = {k: v for k, v in dec.items() if k not in _SENSITIVE_KEYS}
        except Exception:
            pass
    raw: dict = {"kind": "cli_issue", "origin": "scripts/license_tool.py",
                 "payload": p}
    if token:
        raw["token_sha256"] = hashlib.sha256(token.encode("utf-8")).hexdigest()
    lic_id = str(p.get("lic_id") or "")
    if lic_id:
        key = lic_id
    elif token:
        key = "token:" + raw["token_sha256"][:16]
    else:
        canon = json.dumps(p, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
        key = "payload:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
    product = p.get("product_id") or p.get("product")
    sku = p.get("sku_id") or p.get("sku") or None
    seats = p.get("seats")
    return {
        "source_system": SOURCE_SYSTEM,
        "source_key": key,
        "product_id": product if product in PRODUCT_IDS else None,
        "sku_id": (str(sku) if sku else None),
        "plan": (str(p["plan"]) if p.get("plan") else None),
        "edition": None,                    # chengjie 无 edition 概念
        "seats": (int(seats) if isinstance(seats, (int, float))
                  and not isinstance(seats, bool) else None),  # 0=不限，原样保留
        "customer_name": (p.get("sub") or p.get("customer_name") or None),
        "customer_contact": (p.get("customer_contact") or None),
        "machine_fingerprint": None,        # chengjie 不绑机
        "issued_at": _to_iso(p.get("iat")),
        "expires_at": _to_iso(p.get("exp")),
        "status": _decide_status(p, status),
        "raw": raw,
    }


def record_issue(record: dict, outbox_path=None) -> bool:
    """append 单行 JSON（LF）到 outbox。全程 fail-silent：任何异常吞掉返回 False，
    绝不抛、绝不打印——不得影响签发主流程及其输出。"""
    try:
        if not isinstance(record, dict):
            return False
        path = Path(outbox_path) if outbox_path else _default_outbox_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
        return True
    except Exception:
        return False


def export_outbox(input_path=None) -> dict:
    """outbox JSONL → 台账导入文档（schema v1）。同 source_key 后写覆盖先写
    （outbox 是 append-only 事件流，最新一条即当前口径）；坏行跳过并 stderr 警告。"""
    src = Path(input_path) if input_path else _default_outbox_path()
    by_key: dict = {}
    if not src.exists():
        print(f"[ledger_outbox] 警告: outbox 不存在：{src}（输出空 records）",
              file=sys.stderr)
    else:
        for i, ln in enumerate(src.read_text(encoding="utf-8-sig").splitlines(), 1):
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            try:
                rec = json.loads(ln)
            except Exception:
                print(f"[ledger_outbox] 警告: {src}:{i} 不是合法 JSON，已跳过",
                      file=sys.stderr)
                continue
            if not (isinstance(rec, dict) and isinstance(rec.get("source_key"), str)
                    and rec["source_key"]):
                print(f"[ledger_outbox] 警告: {src}:{i} 缺 source_key，已跳过",
                      file=sys.stderr)
                continue
            by_key[rec["source_key"]] = rec
    return {"version": 1, "source_system": SOURCE_SYSTEM,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "records": list(by_key.values())}


def _selftest() -> int:
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="cj_outbox_selftest_"))
    env_backup = os.environ.get(ENV_OUTBOX)
    ok = True

    def check(name: str, cond) -> None:
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)

    try:
        now = int(time.time())
        fake_token = "ZmFrZQ.c2ln"  # 假 token（body 非 JSON），仅参与 sha256，绝不落盘
        p1 = {"sub": "自测客户A", "plan": "pro", "iat": now, "exp": now + 30 * 86400,
              "seats": 10, "channels": ["telegram", "web"], "features": {"l4": True},
              "lic_id": "CJ-SELFTEST-0001", "included_chars": 100000}
        r1 = normalize_issue(p1, fake_token)
        check("14 字段齐全且顺序对齐 schema", list(r1) == _RECORD_FIELDS)
        check("source_key = lic_id", r1["source_key"] == "CJ-SELFTEST-0001")
        check("iat/exp → ISO8601(UTC)", (r1["issued_at"] or "").endswith("+00:00")
              and (r1["expires_at"] or "").endswith("+00:00"))
        check("status = active", r1["status"] == "active")
        check("raw 含 payload + token_sha256",
              r1["raw"]["payload"] == p1 and r1["raw"]["token_sha256"]
              == hashlib.sha256(fake_token.encode("utf-8")).hexdigest())
        check("记录不含 token 原文",
              fake_token not in json.dumps(r1, ensure_ascii=False))

        # token body 可解码时，以 token 内实际签名的 payload 为准（模拟 issue_license
        # setdefault 补齐 grace_days，全程不经私钥）
        body = base64.urlsafe_b64encode(json.dumps(
            dict(p1, grace_days=7), sort_keys=True,
            separators=(",", ":")).encode("utf-8")).rstrip(b"=").decode("ascii")
        r1b = normalize_issue(p1, body + ".c2lnbmF0dXJl")
        check("token 可解码时取 token 内 payload（含 grace_days）",
              r1b["raw"]["payload"].get("grace_days") == 7
              and r1b["source_key"] == "CJ-SELFTEST-0001")

        p2 = {"sub": "自测试用B", "plan": "basic", "iat": now, "seats": 0,
              "trial": True}
        r2 = normalize_issue(p2)
        check("无 lic_id 无 token → payload:<sha16>",
              r2["source_key"].startswith("payload:")
              and len(r2["source_key"]) == len("payload:") + 16)
        check("trial → status = trial", r2["status"] == "trial")
        check("无 exp → expires_at = null", r2["expires_at"] is None)
        check("seats=0 原样保留（0=不限）", r2["seats"] == 0)

        r3 = normalize_issue({"sub": "C", "plan": "flagship", "iat": now},
                             fake_token)
        check("无 lic_id 有 token → token:<sha16>",
              r3["source_key"].startswith("token:"))
        check("显式 status 透传（revoke/renew 挂钩用）",
              normalize_issue(p1, status="revoked")["status"] == "revoked")

        ob = tmp / "outbox" / "ledger_outbox.jsonl"
        check("record_issue 显式路径写入", record_issue(r1, ob) is True)
        check("append 第二条", record_issue(r2, ob) is True)
        lines = ob.read_text(encoding="utf-8").splitlines()
        check("outbox 两行且回读一致", len(lines) == 2
              and json.loads(lines[0]) == r1 and json.loads(lines[1]) == r2)
        check("单行 LF 落盘", ob.read_bytes().count(b"\r") == 0)

        ob_env = tmp / "env_outbox.jsonl"
        os.environ[ENV_OUTBOX] = str(ob_env)
        check("env CHENGJIE_LEDGER_OUTBOX 覆盖默认路径",
              record_issue(r3) is True and ob_env.exists())
        os.environ.pop(ENV_OUTBOX, None)

        blocker = tmp / "blocker"
        blocker.write_text("x", encoding="utf-8")
        check("不可写路径 fail-silent 返回 False",
              record_issue(r1, blocker / "x.jsonl") is False)
        check("不可序列化记录 fail-silent 返回 False",
              record_issue({"raw": {1, 2}}, ob) is False)
        check("非 dict 记录 fail-silent 返回 False",
              record_issue(["not-a-record"], ob) is False)
        check("失败不追加脏行",
              len(ob.read_text(encoding="utf-8").splitlines()) == 2)

        record_issue(dict(r1, status="revoked"), ob)  # 同 source_key 追加新状态
        doc = export_outbox(ob)
        check("export 文档骨架（v1/chengjie/exported_at/records）",
              doc["version"] == 1 and doc["source_system"] == SOURCE_SYSTEM
              and (doc["exported_at"] or "").endswith("+00:00")
              and isinstance(doc["records"], list))
        check("export 同 source_key 后写覆盖", len(doc["records"]) == 2 and next(
            r for r in doc["records"]
            if r["source_key"] == "CJ-SELFTEST-0001")["status"] == "revoked")
    except Exception as e:  # 自测框架兜底：任何未预期异常算失败
        ok = False
        print(f"  [FAIL] selftest 异常：{e!r}")
    finally:
        if env_backup is None:
            os.environ.pop(ENV_OUTBOX, None)
        else:
            os.environ[ENV_OUTBOX] = env_backup
        shutil.rmtree(tmp, ignore_errors=True)
    print("SELFTEST", "OK" if ok else "FAILED")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="chengjie 签发 outbox 台账（签发即导出，P1）")
    ap.add_argument("--selftest", action="store_true",
                    help="临时目录自测（不触碰真实 outbox）")
    ap.add_argument("--export", metavar="OUT_JSON", default="",
                    help="outbox → 台账导入 JSON（schema v1，同 source_key 取最新一条）")
    ap.add_argument("--input", default="",
                    help=f"--export 数据源 outbox 路径（默认 {_default_outbox_path()}）")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    if args.export:
        src = Path(args.input).resolve() if args.input else _default_outbox_path().resolve()
        out = Path(args.export).resolve()
        if out.parent == src.parent:  # 纪律护栏：导出禁止落在 outbox 所在目录（config/）
            print("[ledger_outbox] 错误: --export 不得写进 outbox 所在目录",
                  file=sys.stderr)
            return 2
        doc = export_outbox(src)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        print(f"[ledger_outbox] 已导出 {len(doc['records'])} 条 → {out}")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
