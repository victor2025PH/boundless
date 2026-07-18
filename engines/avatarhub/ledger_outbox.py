# -*- coding: utf-8 -*-
"""ledger_outbox.py — 签发即导出：授权签发/激活/吊销的本地台账 outbox（P1 收尾）。

定位：license_admin.py（离线 issue/revoke）、license_server.py（activate/trial_upgrade）、
fulfill_orders.py 与 sign_worker.py（官网履约/签发队列）在各自「成功点」调用本模块，
把每次发放实时追加一条归一化记录到本地 outbox（JSON Lines，一行一条），供集团账本
（website/scripts/ledger-import-licenses.mjs）周期导入。outbox=增量实时；
tools/license_ledger/export_avatarhub.py=全量对账。两者 source_key 规则完全一致，
同一授权不论走哪条路进账本，幂等键 (source_system, source_key) 相同，重复导入不重复。

source_key 对齐契约（改 export_avatarhub.py 的 key 规则时必须同步这里）：
  orders 激活    lic_id；缺则 "act:"  + sha256("<code>|<fp>|<issued>")[:16]
  试用签发       lic_id；缺则 "trial:" + <指纹>
  离线/队列签发  payload.lic_id；缺则 "local:" + sha256(canonical(payload))[:16]
                 （与 export 读客户 license.key 的兜底同式 → 回收 key 补录也同 key）
  官网订单履约   "order:" + <订单号>（与 export 的 fulfilled_order 同 key，账本合并
                 为一行；outbox 版多带 payload，是授权细节进账本的唯一实时来源）
  吊销（本模块独有事件行）带 lic_id 时直接用 lic_id（与原签发记录同 key，导入即翻
                 revoked）；缺则 "revoke:" + sha256(canonical(匹配字段))[:16]
记录格式 = platform/licensing/ledger/ledger_import.schema.json 的 definitions.record
（14 字段全出现，无值为 null）。raw 绝不含 sig / token / 私钥（写入前递归剥除兜底）。

安全纪律：仅标准库；import 零副作用；record_issue 全程 try/except 静默（首次失败仅
一行 stderr 警告），任何异常都不影响签发主流程。路径优先级：环境变量
AVATARHUB_LEDGER_OUTBOX（最高，可全局改道；设为 off/0/none 整体停写）>
outbox_path 参数（license_server 钩子跟随 _STATE 台账目录传入——单测把台账重定向到
临时目录时 outbox 自动跟着隔离，不污染生产 outbox）> 默认 secrets/ledger_outbox.jsonl。

自测（不做任何真实签发，只在临时目录读写）：
  python engines/avatarhub/ledger_outbox.py --selftest
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SOURCE_SYSTEM = "avatarhub"
# 与 tools/license_ledger/export_avatarhub.py::PRODUCT_IDS 一致（预留透传校验）
PRODUCT_IDS = {"huansheng", "huanyan", "huanying", "tongchuan",
               "tongyi", "zhiliao", "zhituo"}
ENV_VAR = "AVATARHUB_LEDGER_OUTBOX"
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTBOX = BASE_DIR / "secrets" / "ledger_outbox.jsonl"   # secrets/ 已 .gitignore
_SENSITIVE_KEYS = {"sig", "token"}   # 写入前递归剥除（键名精确匹配，大小写不敏感）
_WARNED = False                      # 失败告警只发一次，避免常驻服务刷屏


# ── 与 export_avatarhub.py 同口径的小工具（保持逐字段一致，勿独自演化）────────


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _canonical(payload: dict) -> str:
    """与 license.py/export_avatarhub.py 的规范化 JSON 同参数（sort_keys+紧凑+非 ASCII 保留）。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _to_iso(ts) -> "str | None":
    """unix 秒（int/float/数字串）→ ISO8601(UTC)；0/空/解析失败 → None（原值在 raw）。"""
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


def _decide_status(*, revoked: bool, expires_ts, is_trial: bool,
                   now: "float | None" = None) -> str:
    """归一化状态口径（platform ledger README §4）：revoked > expired > trial > active。"""
    now = time.time() if now is None else now
    if revoked:
        return "revoked"
    try:
        exp = float(expires_ts or 0)
    except (TypeError, ValueError):
        exp = 0.0
    if exp > 0 and now > exp:
        return "expired"
    if is_trial:
        return "trial"
    return "active"


def _map_product_sku(rec: dict) -> tuple:
    """预留透传（export 同名函数的引擎侧版）：引擎读不到 sku_registry，sku 原样透传不校验。"""
    product = rec.get("product_id") or rec.get("product")
    product = product if product in PRODUCT_IDS else None
    sku = rec.get("sku_id") or rec.get("sku") or None
    return product, sku


def _make_record(*, source_key: str, product_id=None, sku_id=None, plan=None,
                 edition=None, seats=None, customer_name=None, customer_contact=None,
                 machine_fingerprint=None, issued_at=None, expires_at=None,
                 status="unknown", raw=None) -> dict:
    """schema definitions.record 的 14 字段（与 export_avatarhub.make_record 逐字段一致）。"""
    return {
        "source_system": SOURCE_SYSTEM,
        "source_key": source_key,
        "product_id": product_id,
        "sku_id": sku_id,
        "plan": plan,
        "edition": edition,
        "seats": seats,
        "customer_name": customer_name or None,
        "customer_contact": customer_contact or None,
        "machine_fingerprint": machine_fingerprint or None,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "status": status,
        "raw": raw or {},
    }


def _strip_sensitive(obj):
    """递归剥除敏感键（sig/token）。构造器本就不放入，这里是落盘前的最后兜底。"""
    if isinstance(obj, dict):
        return {k: _strip_sensitive(v) for k, v in obj.items()
                if str(k).lower() not in _SENSITIVE_KEYS}
    if isinstance(obj, list):
        return [_strip_sensitive(v) for v in obj]
    return obj


# ── 便捷构造器（每个成功点一款；字段映射与 export_avatarhub.py 对齐）──────────


def normalize_from_activation(code: str, code_record: dict, activation: dict,
                              *, now=None) -> dict:
    """license_server.activate 成功点：一次兑换码激活（对应 export 的 orders_activation）。"""
    rec = code_record if isinstance(code_record, dict) else {}
    act = activation if isinstance(activation, dict) else {}
    edition = rec.get("edition") or None
    fp = act.get("fingerprint") or None
    lic_id = act.get("lic_id") or ""
    issued, expires = act.get("issued"), act.get("expires")
    product, sku = _map_product_sku(rec)
    is_trial = (edition == "trial") or str(lic_id).startswith("trial-")
    return _make_record(
        source_key=lic_id or ("act:" + _sha16(f"{code}|{fp}|{issued}")),
        product_id=product, sku_id=sku,
        edition=edition, seats=1, customer_name=rec.get("licensee") or None,
        machine_fingerprint=fp,
        issued_at=_to_iso(issued), expires_at=_to_iso(expires),
        status=_decide_status(revoked=bool(rec.get("disabled")), expires_ts=expires,
                              is_trial=is_trial, now=now),
        raw={"kind": "orders_activation", "code": code,
             "activation": act, "code_record": rec},
    )


def normalize_from_trial(fingerprint: str, record: dict, *, now=None) -> dict:
    """license_server.trial_upgrade 成功点：一键试用签发（对应 export 的 trial_upgrade）。
    注意：edition 记 null 不虚构（引擎试签固定 pro 档，但台账行本身无该字段，同 export §7.2）。"""
    rec = record if isinstance(record, dict) else {}
    fp = str(fingerprint or "")
    lic_id = rec.get("lic_id") or ""
    issued, expires = rec.get("issued"), rec.get("expires")
    product, sku = _map_product_sku(rec)
    return _make_record(
        source_key=lic_id or f"trial:{fp}",
        product_id=product, sku_id=sku,
        machine_fingerprint=fp or None,
        issued_at=_to_iso(issued), expires_at=_to_iso(expires),
        status=_decide_status(revoked=False, expires_ts=expires, is_trial=True, now=now),
        raw={"kind": "trial_upgrade", "fingerprint": fp, "record": rec},
    )


def normalize_from_issue(payload: dict, kind: str = "admin_issue",
                         extra_raw: "dict | None" = None, *, now=None) -> dict:
    """通用「签发 payload → 记录」：license_admin issue（kind=admin_issue）、
    sign_worker 队列签发（kind=console_sign，extra_raw 带 request_id）。
    payload 即 {v, lic_id, machine, edition, licensee, issued, expires[, features]}，不含 sig。
    lic_id 兜底 "local:"+sha16(canonical) 与 export 读客户 license.key 的规则相同 → 同 key。"""
    pl = payload if isinstance(payload, dict) else {}
    lic_id = pl.get("lic_id") or ""
    edition = pl.get("edition") or None
    issued, expires = pl.get("issued"), pl.get("expires")
    product, sku = _map_product_sku(pl)
    is_trial = (edition == "trial") or str(lic_id).startswith("trial-")
    raw = {"kind": kind, "payload": pl}
    if isinstance(extra_raw, dict):
        raw.update(extra_raw)
    return _make_record(
        source_key=lic_id or ("local:" + _sha16(_canonical(pl))),
        product_id=product, sku_id=sku,
        edition=edition, customer_name=pl.get("licensee") or None,
        machine_fingerprint=pl.get("machine") or None,
        issued_at=_to_iso(issued), expires_at=_to_iso(expires),
        status=_decide_status(revoked=False, expires_ts=expires, is_trial=is_trial, now=now),
        raw=raw,
    )


def normalize_from_fulfillment(order_id: str, payload: dict, fulfilled_ts=None,
                               *, now=None) -> dict:
    """fulfill_orders 履约成功点。source_key=order:<订单号> 与 export 的 fulfilled_order
    行（仅完成标记）完全同 key → 账本合并为一行不重复；outbox 版多带 payload
    （档位/指纹/到期），是该订单授权细节进账本的唯一实时来源。"""
    pl = payload if isinstance(payload, dict) else {}
    edition = pl.get("edition") or None
    issued, expires = pl.get("issued"), pl.get("expires")
    product, sku = _map_product_sku(pl)
    is_trial = (edition == "trial") or str(pl.get("lic_id", "")).startswith("trial-")
    return _make_record(
        source_key=f"order:{order_id}",
        product_id=product, sku_id=sku,
        edition=edition, customer_name=pl.get("licensee") or None,
        machine_fingerprint=pl.get("machine") or None,
        issued_at=_to_iso(issued if issued is not None else fulfilled_ts),
        expires_at=_to_iso(expires),
        status=_decide_status(revoked=False, expires_ts=expires, is_trial=is_trial, now=now),
        raw={"kind": "fulfilled_order", "order_id": str(order_id),
             "fulfilled_ts": fulfilled_ts, "payload": pl},
    )


def normalize_from_revoke(entry: dict, *, now=None) -> dict:
    """license_admin revoke 成功点：吊销事件行。带 lic_id 时与原签发记录同 key，
    导入 upsert 即把该授权状态翻成 revoked；只按 machine/licensee 吊销时另立
    "revoke:" 事件行（export 没有对应行，不冲突）。"""
    e = entry if isinstance(entry, dict) else {}
    lic_id = e.get("lic_id") or ""
    target = {k: e[k] for k in ("lic_id", "machine", "licensee", "issued")
              if e.get(k) not in (None, "")}
    return _make_record(
        source_key=lic_id or ("revoke:" + _sha16(_canonical(target))),
        customer_name=e.get("licensee") or None,
        machine_fingerprint=e.get("machine") or None,
        issued_at=_to_iso(e.get("issued")),
        status="revoked",
        raw={"kind": "admin_revoke", "entry": e},
    )


# ── 落盘 ─────────────────────────────────────────────────────────────


def record_issue(record: dict, outbox_path=None) -> bool:
    """追加一条归一化记录到 outbox（一行一条 JSON，LF，append 单次写）。
    绝不抛异常：成功 True，任何失败 False（首次失败附一行 stderr 警告）。
    路径优先级：环境变量 AVATARHUB_LEDGER_OUTBOX > outbox_path 参数 > 默认
    secrets/ledger_outbox.jsonl；环境变量为 off/0/none 时整体停写。"""
    global _WARNED
    try:
        if not isinstance(record, dict):
            return False
        env = os.environ.get(ENV_VAR, "").strip()
        if env.lower() in ("off", "0", "none"):
            return False
        path = Path(env) if env else (Path(outbox_path) if outbox_path else DEFAULT_OUTBOX)
        line = json.dumps(_strip_sensitive(record), ensure_ascii=False,
                          separators=(",", ":"), default=str)
        path.parent.mkdir(parents=True, exist_ok=True)
        # append 模式 + 整行一次 write：多线程/多进程并发追加互不截断（行都很小）
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
        return True
    except Exception as e:
        if not _WARNED:
            _WARNED = True
            try:
                sys.stderr.write(f"[ledger_outbox] 警告: outbox 写入失败（不影响签发主流程）：{e}\n")
            except Exception:
                pass
        return False


# ── 自测（独立运行；不碰真实台账，不做真实签发）──────────────────────────


def _selftest() -> int:
    import tempfile
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        print(("  [PASS] " if cond else "  [FAIL] ") + name)

    print("[ledger_outbox] --selftest（临时目录读写，只测本模块）")
    env_backup = os.environ.pop(ENV_VAR, None)   # 隔离外部环境变量（含 off 开关）
    try:
        with tempfile.TemporaryDirectory(prefix="avh_outbox_test_") as td:
            tdp = Path(td)
            ob = tdp / "sub" / "ledger_outbox.jsonl"
            now = int(time.time())
            act = {"fingerprint": "TEST-AAAA-BBBB-0001", "issued": now,
                   "expires": now + 30 * 86400, "lic_id": "selftest-lic-0001"}
            code_rec = {"edition": "pro", "days": 365, "seats": 1, "licensee": "自测客户",
                        "created": now, "activations": [act], "token": "应被剥除"}
            r1 = normalize_from_activation("AVH-TEST-CODE-0001", code_rec, act)
            r2 = normalize_from_trial("TEST-CCCC-DDDD-0002",
                                      {"issued": now, "expires": now + 7 * 86400})
            r3 = normalize_from_issue(
                {"v": 1, "lic_id": "selftest-lic-0003", "machine": "*",
                 "edition": "standard", "licensee": "自测站点", "issued": now, "expires": 0},
                kind="admin_issue", extra_raw={"out": "license.key", "sig": "应被剥除"})
            check("record_issue 写入 3 条均返回 True",
                  all(record_issue(r, outbox_path=ob) for r in (r1, r2, r3)))
            data = ob.read_bytes()
            check("行尾为 LF（无 \\r）", b"\r" not in data)
            lines = [ln for ln in data.decode("utf-8").split("\n") if ln.strip()]
            check("恰好 3 行", len(lines) == 3)
            try:
                parsed = [json.loads(ln) for ln in lines]
            except Exception:
                parsed = []
            check("每行均为合法 JSON", len(parsed) == 3)
            req = {"source_system", "source_key", "product_id", "sku_id", "plan",
                   "edition", "seats", "customer_name", "customer_contact",
                   "machine_fingerprint", "issued_at", "expires_at", "status", "raw"}
            check("schema 14 字段全在场且无多余键",
                  bool(parsed) and all(set(p.keys()) == req for p in parsed))

            def has_sensitive(o):
                if isinstance(o, dict):
                    return (any(str(k).lower() in _SENSITIVE_KEYS for k in o)
                            or any(has_sensitive(v) for v in o.values()))
                if isinstance(o, list):
                    return any(has_sensitive(v) for v in o)
                return False

            check("读回无敏感字段（sig/token 已剥除）",
                  bool(parsed) and not any(has_sensitive(p) for p in parsed))
            check("source_key：激活取 lic_id",
                  bool(parsed) and parsed[0]["source_key"] == "selftest-lic-0001")
            check("source_key：试用缺 lic_id 时 trial:<指纹>",
                  bool(parsed) and parsed[1]["source_key"] == "trial:TEST-CCCC-DDDD-0002")
            check("状态口径 activation→active / trial→trial / 永久→active",
                  bool(parsed) and [p["status"] for p in parsed] == ["active", "trial", "active"])
            fb = normalize_from_activation("C", {}, {"fingerprint": "F", "issued": 123})
            check("act: 兜底公式与 export_avatarhub 一致",
                  fb["source_key"] == "act:" + hashlib.sha256(b"C|F|123").hexdigest()[:16])
            check("试用已过期→expired",
                  normalize_from_trial("F", {"issued": now - 10 * 86400,
                                             "expires": now - 86400})["status"] == "expired")
            rv = normalize_from_revoke({"lic_id": "selftest-lic-0001",
                                        "reason": "自测", "ts": now})
            check("吊销行 status=revoked 且与原记录同 key",
                  rv["status"] == "revoked" and rv["source_key"] == "selftest-lic-0001")
            pl_nolic = {"v": 1, "machine": "M", "edition": "pro", "licensee": "",
                        "issued": 1, "expires": 0}
            canon = json.dumps(pl_nolic, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False)
            check("payload 缺 lic_id 时 local: 兜底公式与 export 一致",
                  normalize_from_issue(pl_nolic)["source_key"]
                  == "local:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16])
            ff = normalize_from_fulfillment(
                "ord_0001", {"lic_id": "ff01", "edition": "pro", "machine": "F",
                             "issued": now, "expires": 0}, fulfilled_ts=now)
            check("履约行 source_key=order:<订单号>（与 export 同 key 合并）",
                  ff["source_key"] == "order:ord_0001" and ff["status"] == "active")

            ob2 = tdp / "env_override.jsonl"
            os.environ[ENV_VAR] = str(ob2)
            check("环境变量可覆盖默认路径", record_issue(r1) and ob2.exists())
            n_ob = len(ob.read_bytes().splitlines())
            check("环境变量优先于显式 outbox_path 参数",
                  record_issue(r1, outbox_path=ob)
                  and len(ob.read_bytes().splitlines()) == n_ob
                  and len(ob2.read_bytes().splitlines()) == 2)
            os.environ[ENV_VAR] = "off"
            check("环境变量 off 时静默停写", record_issue(r1) is False)
            del os.environ[ENV_VAR]

            blocker = tdp / "not_a_dir"
            blocker.write_text("x", encoding="utf-8")
            check("非法路径静默返回 False（不抛异常）",
                  record_issue(r1, outbox_path=blocker / "x.jsonl") is False)
            check("非 dict 入参静默返回 False", record_issue("不是字典") is False)
    finally:
        if env_backup is not None:
            os.environ[ENV_VAR] = env_backup

    failed = sum(1 for _, okv in results if not okv)
    print(f"[ledger_outbox] 自测 {len(results)} 项："
          + ("全部通过" if not failed else f"失败 {failed} 项"))
    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # GBK 控制台防中文炸 print
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    import argparse
    ap = argparse.ArgumentParser(
        description="授权台账 outbox（签发即导出）。作为模块被签发路成功点调用；"
                    "独立运行仅支持 --selftest。")
    ap.add_argument("--selftest", action="store_true", help="临时目录自测（不做真实签发）")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(_selftest())
    ap.print_help()
