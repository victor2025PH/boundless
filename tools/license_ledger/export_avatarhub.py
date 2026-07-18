# -*- coding: utf-8 -*-
"""export_avatarhub.py — avatarhub 系授权发放记录 → 集团统一台账归一化 JSON（P1，只读）。

数据源（全部只读，缺失即跳过并 stderr 警告）：
  <engine>/secrets/orders.json            兑换码台账（主台账：codes[*].activations[*]）
  <engine>/secrets/trials.json            一键试用台账（fps[*]）
  <engine>/secrets/fulfilled_orders.json  官网订单履约完成标记（done[*]）
  <engine>/license.key                    本机当前生效授权（单条）
  <engine>/revocations.json               吊销名单 CRL（仅作状态标注，不验签）

输出格式见 platform/licensing/ledger/ledger_import.schema.json（draft-07）。
纪律：绝对只读——本脚本对 engines/ 目录只 open(..., "r")；输出文件禁止落在被读引擎目录内。
仅 Python 标准库。用法见 tools/license_ledger/README.md。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:  # GBK 控制台防中文炸 print（与 engines 侧脚本同处理）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SOURCE_SYSTEM = "avatarhub"
PRODUCT_IDS = {"huansheng", "huanyan", "huanying", "tongchuan",
               "tongyi", "zhiliao", "zhituo"}
# 与 engines/avatarhub/license.py::_REVOKE_MATCH_KEYS 同语义（条目内 AND、名单内 OR）
REVOKE_MATCH_KEYS = ("lic_id", "machine", "licensee", "issued")

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENGINE_DIR = _REPO_ROOT / "engines" / "avatarhub"


def warn(msg: str) -> None:
    print(f"[export_avatarhub] 警告: {msg}", file=sys.stderr)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_iso(ts) -> "str | None":
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


def sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def load_json(path: Path) -> "dict | None":
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except Exception as e:
        warn(f"{path} 解析失败，已跳过：{e}")
        return None


def load_sku_ids() -> "set | None":
    """sku_registry.json 可读时返回合法 sku_id 集合，否则 None（透传不校验）。"""
    reg = load_json(_REPO_ROOT / "platform" / "licensing" / "sku_registry.json")
    if not isinstance(reg, dict):
        return None
    return {r.get("sku_id") for r in reg.get("flat_skus", []) if r.get("sku_id")}


def map_product_sku(rec: dict, sku_ids: "set | None") -> tuple:
    """预留透传：原始记录带 product_id/product、sku_id/sku 时尽力映射（存量记录均无 → null）。"""
    product = rec.get("product_id") or rec.get("product")
    product = product if product in PRODUCT_IDS else None
    sku = rec.get("sku_id") or rec.get("sku") or None
    if sku is not None and sku_ids is not None and sku not in sku_ids:
        sku = None
    return product, sku


def load_crl_entries(engine_dir: Path) -> list:
    doc = load_json(engine_dir / "revocations.json")
    if not isinstance(doc, dict):
        return []
    rev = (doc.get("payload") or {}).get("revoked") or []
    return [e for e in rev if isinstance(e, dict)]


def crl_hit(crl: list, *, lic_id="", machine="", licensee="", issued=None) -> bool:
    """是否命中吊销名单（不验签，仅字段匹配；与 license.py 匹配语义一致）。"""
    probe = {"lic_id": lic_id, "machine": machine, "licensee": licensee,
             "issued": "" if issued in (None, "") else issued}
    for entry in crl:
        keys = [k for k in REVOKE_MATCH_KEYS if entry.get(k) not in (None, "")]
        if keys and all(str(probe.get(k, "")) == str(entry.get(k)) for k in keys):
            return True
    return False


def decide_status(*, revoked: bool, expires_ts, is_trial: bool,
                  known_valid: bool = True, now: "float | None" = None) -> str:
    """归一化状态口径（README §4）：revoked > expired > trial > active / unknown。"""
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
    return "active" if known_valid else "unknown"


def make_record(*, source_key: str, product_id=None, sku_id=None, plan=None,
                edition=None, seats=None, customer_name=None, customer_contact=None,
                machine_fingerprint=None, issued_at=None, expires_at=None,
                status="unknown", raw=None) -> dict:
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


# ── 各数据源 → 归一化记录 ─────────────────────────────────────────────


def records_from_orders(orders: dict, crl: list, sku_ids) -> list:
    out = []
    codes = orders.get("codes") or {}
    if not isinstance(codes, dict):
        return out
    for code in sorted(codes):
        rec = codes[code]
        if not isinstance(rec, dict):
            continue
        edition = rec.get("edition") or None
        licensee = rec.get("licensee") or None
        disabled = bool(rec.get("disabled"))
        product, sku = map_product_sku(rec, sku_ids)
        acts = [a for a in (rec.get("activations") or []) if isinstance(a, dict)]
        for act in acts:
            fp = act.get("fingerprint") or None
            lic_id = act.get("lic_id") or ""
            issued = act.get("issued")
            expires = act.get("expires")
            revoked = disabled or crl_hit(
                crl, lic_id=lic_id, machine=fp or "", licensee=licensee or "",
                issued=issued)
            is_trial = (edition == "trial") or str(lic_id).startswith("trial-")
            out.append(make_record(
                source_key=lic_id or ("act:" + sha16(f"{code}|{fp}|{issued}")),
                product_id=product, sku_id=sku,
                edition=edition, seats=1, customer_name=licensee,
                machine_fingerprint=fp,
                issued_at=to_iso(issued), expires_at=to_iso(expires),
                status=decide_status(revoked=revoked, expires_ts=expires,
                                     is_trial=is_trial),
                raw={"kind": "orders_activation", "code": code,
                     "activation": act, "code_record": rec},
            ))
        if not acts:
            # 未激活兑换码：有效期未起算 → expires_at=null 表示未知（README §7.3）
            try:
                seats = int(rec.get("seats", 1))
            except (TypeError, ValueError):
                seats = None
            out.append(make_record(
                source_key=f"code:{code}",
                product_id=product, sku_id=sku,
                edition=edition, seats=seats, customer_name=licensee,
                issued_at=to_iso(rec.get("created")),
                status="revoked" if disabled else "unknown",
                raw={"kind": "orders_code_unactivated", "code": code,
                     "code_record": rec},
            ))
    return out


def records_from_trials(trials: dict, crl: list, sku_ids) -> list:
    out = []
    fps = trials.get("fps") or {}
    if not isinstance(fps, dict):
        return out
    for fp in sorted(fps):
        rec = fps[fp]
        if not isinstance(rec, dict):
            continue
        lic_id = rec.get("lic_id") or ""
        issued = rec.get("issued")
        expires = rec.get("expires")
        product, sku = map_product_sku(rec, sku_ids)
        revoked = crl_hit(crl, lic_id=lic_id, machine=fp, issued=issued)
        # 引擎签发试签固定 pro 档，但台账行本身无 edition 字段 → 不虚构，记 null（README §7.2）
        out.append(make_record(
            source_key=lic_id or f"trial:{fp}",
            product_id=product, sku_id=sku,
            machine_fingerprint=fp,
            issued_at=to_iso(issued), expires_at=to_iso(expires),
            status=decide_status(revoked=revoked, expires_ts=expires, is_trial=True),
            raw={"kind": "trial_upgrade", "fingerprint": fp, "record": rec},
        ))
    return out


def record_from_license_key(doc: dict, crl: list, sku_ids) -> "dict | None":
    payload = doc.get("payload")
    if not isinstance(payload, dict):
        return None
    lic_id = payload.get("lic_id") or ""
    machine = payload.get("machine") or None
    licensee = payload.get("licensee") or None
    edition = payload.get("edition") or None
    issued = payload.get("issued")
    expires = payload.get("expires")
    product, sku = map_product_sku(payload, sku_ids)
    revoked = crl_hit(crl, lic_id=lic_id, machine=machine or "",
                      licensee=licensee or "", issued=issued)
    is_trial = (edition == "trial") or str(lic_id).startswith("trial-")
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False)
    # raw 不带 sig：防导出文件被拼回可直接激活的完整 license.key（README §8）
    return make_record(
        source_key=lic_id or ("local:" + sha16(canon)),
        product_id=product, sku_id=sku,
        edition=edition, customer_name=licensee, machine_fingerprint=machine,
        issued_at=to_iso(issued), expires_at=to_iso(expires),
        status=decide_status(revoked=revoked, expires_ts=expires, is_trial=is_trial),
        raw={"kind": "local_license_key", "payload": payload,
             "alg": doc.get("alg")},
    )


def records_from_fulfilled(state: dict) -> list:
    out = []
    done = state.get("done") or {}
    if not isinstance(done, dict):
        return out
    for oid in sorted(done):
        ts = done[oid]
        # 仅完成标记：授权细节在官网订单库，按订单号可关联（README §7.4）
        out.append(make_record(
            source_key=f"order:{oid}",
            issued_at=to_iso(ts),
            status="unknown",
            raw={"kind": "fulfilled_order", "order_id": oid, "fulfilled_ts": ts},
        ))
    return out


# ── 汇总 ──────────────────────────────────────────────────────────────


def collect(engine_dir: Path) -> list:
    if not engine_dir.exists():
        warn(f"数据源目录不存在：{engine_dir}（输出空 records）")
        return []
    sku_ids = load_sku_ids()
    # --input 指到 secrets/ 目录时，revocations.json 在其上一级（引擎根）
    crl = load_crl_entries(engine_dir) or load_crl_entries(engine_dir.parent)
    records: list = []

    sources = [
        ("orders", engine_dir / "secrets" / "orders.json",
         lambda d: records_from_orders(d, crl, sku_ids)),
        ("trials", engine_dir / "secrets" / "trials.json",
         lambda d: records_from_trials(d, crl, sku_ids)),
        ("license.key", engine_dir / "license.key",
         lambda d: [r for r in [record_from_license_key(d, crl, sku_ids)] if r]),
        ("fulfilled_orders", engine_dir / "secrets" / "fulfilled_orders.json",
         records_from_fulfilled),
    ]
    # --input 直接指到 secrets/ 目录时也能找到台账
    if not (engine_dir / "secrets").exists():
        sources = [
            ("orders", engine_dir / "orders.json",
             lambda d: records_from_orders(d, crl, sku_ids)),
            ("trials", engine_dir / "trials.json",
             lambda d: records_from_trials(d, crl, sku_ids)),
            ("license.key", engine_dir / "license.key",
             lambda d: [r for r in [record_from_license_key(d, crl, sku_ids)] if r]),
            ("fulfilled_orders", engine_dir / "fulfilled_orders.json",
             records_from_fulfilled),
        ]

    found_any = False
    for name, path, fn in sources:
        data = load_json(path)
        if data is None:
            continue
        found_any = True
        records.extend(fn(data))
    if not found_any:
        warn(f"{engine_dir} 下未找到任何授权台账文件"
             "（orders.json / trials.json / license.key / fulfilled_orders.json）")

    # 同 source_key 首见优先（本机 license.key 的 lic_id 可能已被 orders 台账覆盖）
    seen, unique = set(), []
    for r in records:
        if r["source_key"] in seen:
            warn(f"重复 source_key 已跳过：{r['source_key']}（kind={r['raw'].get('kind')}）")
            continue
        seen.add(r["source_key"])
        unique.append(r)
    return unique


def collect_from_file(path: Path) -> list:
    """--input 指向单个 json 文件时按结构自动识别（不依赖文件名）。"""
    sku_ids = load_sku_ids()
    data = load_json(path)
    if data is None:
        warn(f"数据源文件不存在或不可解析：{path}（输出空 records）")
        return []
    if not isinstance(data, dict):
        warn(f"{path} 不是 JSON 对象（输出空 records）")
        return []
    # 台账文件在 secrets/ 时，revocations.json 在引擎根（上两级都试）
    crl = load_crl_entries(path.parent) or load_crl_entries(path.parent.parent)
    if "codes" in data:
        return records_from_orders(data, crl, sku_ids)
    if "fps" in data:
        return records_from_trials(data, crl, sku_ids)
    if "payload" in data:
        r = record_from_license_key(data, crl, sku_ids)
        return [r] if r else []
    if "done" in data:
        return records_from_fulfilled(data)
    warn(f"{path} 结构无法识别（期望 codes/fps/payload/done 之一），输出空 records")
    return []


def demo_records() -> list:
    """3 条演示记录（管道联调用，覆盖 激活/试用/未激活码 三形态）。"""
    now = int(time.time())
    return [
        make_record(
            source_key="demo-lic-0001",
            edition="pro", seats=1, customer_name="演示客户甲",
            machine_fingerprint="DEMO-AAAA-BBBB-0001",
            issued_at=to_iso(now - 30 * 86400), expires_at=to_iso(now + 335 * 86400),
            status="active",
            raw={"kind": "orders_activation", "demo": True, "code": "AVH-DEMO-CODE-0001",
                 "activation": {"fingerprint": "DEMO-AAAA-BBBB-0001",
                                "issued": now - 30 * 86400,
                                "expires": now + 335 * 86400,
                                "lic_id": "demo-lic-0001"}},
        ),
        make_record(
            source_key="trial-demo0002",
            machine_fingerprint="DEMO-CCCC-DDDD-0002",
            issued_at=to_iso(now - 2 * 86400), expires_at=to_iso(now + 5 * 86400),
            status="trial",
            raw={"kind": "trial_upgrade", "demo": True,
                 "fingerprint": "DEMO-CCCC-DDDD-0002",
                 "record": {"issued": now - 2 * 86400, "expires": now + 5 * 86400,
                            "lic_id": "trial-demo0002"}},
        ),
        make_record(
            source_key="code:AVH-DEMO-CODE-0003",
            edition="standard", seats=2, customer_name="演示客户乙",
            issued_at=to_iso(now - 86400),
            status="unknown",
            raw={"kind": "orders_code_unactivated", "demo": True,
                 "code": "AVH-DEMO-CODE-0003",
                 "code_record": {"edition": "standard", "days": 365, "seats": 2,
                                 "licensee": "演示客户乙", "created": now - 86400,
                                 "activations": []}},
        ),
    ]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="avatarhub 授权发放记录 → 集团统一台账归一化 JSON（只读）")
    ap.add_argument("--input", default="",
                    help=f"数据源覆盖：引擎目录 / secrets 目录 / 单个台账 json"
                         f"（默认 {DEFAULT_ENGINE_DIR}）")
    ap.add_argument("--out", default="avatarhub_licenses.json",
                    help="输出 JSON 路径（默认 ./avatarhub_licenses.json）")
    ap.add_argument("--demo", action="store_true",
                    help="不读真实数据，生成 3 条演示记录（管道联调）")
    args = ap.parse_args()

    src = Path(args.input).resolve() if args.input else DEFAULT_ENGINE_DIR
    out_path = Path(args.out).resolve()

    if args.demo:
        records = demo_records()
    else:
        # 只读纪律护栏：输出禁止落在被读引擎目录内
        guard_dir = src if src.is_dir() else src.parent
        if str(out_path).startswith(str(guard_dir) + os.sep):
            print(f"[export_avatarhub] 错误: --out 不得位于被读目录内（{guard_dir}）",
                  file=sys.stderr)
            return 2
        records = collect_from_file(src) if src.is_file() else collect(src)

    doc = {"version": 1, "source_system": SOURCE_SYSTEM,
           "exported_at": now_iso(), "records": records}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(f"[export_avatarhub] 已导出 {len(records)} 条 → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
