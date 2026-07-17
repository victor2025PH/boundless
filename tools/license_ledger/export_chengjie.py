# -*- coding: utf-8 -*-
"""export_chengjie.py — chengjie 系授权发放记录 → 集团统一台账归一化 JSON（P1，只读）。

侦察结论：chengjie 签发端（scripts/license_tool.py）不留台账，引擎侧唯一持久化的
授权记录是当前部署生效的 token 文件 config/license.key（`<payload_b64url>.<sig_b64url>`）。
故本导出器支持三种输入形态（--input，全部只读）：
  * 单个 token 文件（默认 <repo>/engines/chengjie/config/license.key）；
  * 目录（递归收集其中 *.key —— 用于补录从客户侧回收的授权文件）；
  * .jsonl 台账（每行：token 字符串 / {"token": "..."} / 直接 payload 对象，
    行内可显式带 customer_name/customer_contact/product_id/sku_id/revoked
    等归一化同名字段，导出器透传 —— 为厂商日后手工维护签发台账预留）。

安全：raw 不含 token 原文（完整 token 即可直接激活的授权凭证），以 token_sha256
摘要替代；payload 解码后完整保留。不做 Ed25519 验签（纯标准库，台账定位是对账不是防伪）。

输出格式见 platform/licensing/ledger/ledger_import.schema.json（draft-07）。
纪律：绝对只读；输出文件禁止落在被读引擎目录内。仅 Python 标准库。
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

try:  # GBK 控制台防中文炸 print（与 engines 侧脚本同处理）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SOURCE_SYSTEM = "chengjie"
PRODUCT_IDS = {"huansheng", "huanyan", "huanying", "tongchuan",
               "tongyi", "zhiliao", "zhituo"}

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = _REPO_ROOT / "engines" / "chengjie" / "config" / "license.key"


def warn(msg: str) -> None:
    print(f"[export_chengjie] 警告: {msg}", file=sys.stderr)


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


def token_sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_sku_ids() -> "set | None":
    try:
        reg = json.loads((_REPO_ROOT / "platform" / "licensing" /
                          "sku_registry.json").read_text(encoding="utf-8-sig"))
        return {r.get("sku_id") for r in reg.get("flat_skus", []) if r.get("sku_id")}
    except Exception:
        return None


def map_product_sku(src: dict, sku_ids: "set | None") -> tuple:
    """预留透传：payload/台账行带 product_id/product、sku_id/sku 时尽力映射（存量均无 → null）。"""
    product = src.get("product_id") or src.get("product")
    product = product if product in PRODUCT_IDS else None
    sku = src.get("sku_id") or src.get("sku") or None
    if sku is not None and sku_ids is not None and sku not in sku_ids:
        sku = None
    return product, sku


def decode_token(token: str) -> "dict | None":
    """`<payload_b64url>.<sig_b64url>` → payload dict；解析失败 → None（不验签）。"""
    if "." not in token:
        return None
    body_b64 = token.split(".", 1)[0]
    try:
        raw = base64.urlsafe_b64decode(body_b64 + "=" * (-len(body_b64) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def decide_status(payload: dict, *, revoked: bool = False,
                  now: "float | None" = None) -> str:
    """归一化状态口径（ledger README §4）：revoked > expired(含宽限) > trial > active。"""
    now = time.time() if now is None else now
    if revoked:
        return "revoked"
    try:
        exp = float(payload.get("exp") or 0)
    except (TypeError, ValueError):
        exp = 0.0
    if exp > 0 and now > exp:
        return "expired"
    if bool(payload.get("trial", False)):
        return "trial"
    return "active"


def make_record(payload: "dict | None", token: "str | None", *, origin: str,
                kind: str, overrides: "dict | None" = None,
                sku_ids: "set | None" = None) -> dict:
    """一条 token/payload → 归一化记录。payload=None 表示 token 无法解析（unknown）。"""
    ov = overrides or {}
    raw: dict = {"kind": kind, "origin": origin}
    if token is not None:
        raw["token_sha256"] = token_sha(token)
    if payload is None:
        raw["error"] = "token 无法解析（非 <payload_b64url>.<sig_b64url> 格式）"
        key = "token:" + (raw.get("token_sha256", "")[:16] or "invalid")
        return {
            "source_system": SOURCE_SYSTEM, "source_key": key,
            "product_id": None, "sku_id": None, "plan": None, "edition": None,
            "seats": None, "customer_name": None, "customer_contact": None,
            "machine_fingerprint": None, "issued_at": None, "expires_at": None,
            "status": "unknown", "raw": raw,
        }
    raw["payload"] = payload
    if ov:
        raw["ledger_row_extras"] = ov
    product, sku = map_product_sku({**payload, **ov}, sku_ids)
    lic_id = str(payload.get("lic_id") or "")
    if lic_id:
        key = lic_id
    elif token is not None:
        key = "token:" + raw["token_sha256"][:16]
    else:  # jsonl 行直接给 payload 对象且无 lic_id：按规范化 payload 内容取键
        canon = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
        key = "payload:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
    # revoked / 联系人等归一化键：jsonl 行 extras 优先，payload 直写形态兜底
    revoked = (bool(ov.get("revoked")) or str(ov.get("status", "")) == "revoked"
               or bool(payload.get("revoked"))
               or str(payload.get("status", "")) == "revoked")
    return {
        "source_system": SOURCE_SYSTEM,
        "source_key": key,
        "product_id": product,
        "sku_id": sku,
        "plan": (str(payload.get("plan")) if payload.get("plan") else None),
        "edition": None,                      # chengjie 无 edition 概念
        "seats": (int(payload["seats"]) if isinstance(payload.get("seats"), (int, float))
                  and not isinstance(payload.get("seats"), bool)
                  else None),                 # 0=不限，原样保留
        "customer_name": (ov.get("customer_name") or payload.get("sub")
                          or payload.get("customer_name") or None),
        "customer_contact": (ov.get("customer_contact")
                             or payload.get("customer_contact") or None),
        "machine_fingerprint": None,          # chengjie 不绑机
        "issued_at": to_iso(payload.get("iat")),
        "expires_at": to_iso(payload.get("exp")),
        "status": decide_status(payload, revoked=revoked),
        "raw": raw,
    }


# ── 输入形态识别 ─────────────────────────────────────────────────────


def records_from_token_text(text: str, origin: str, sku_ids) -> list:
    """一个 token 文件可能含多行（每行一个 token）。空文件 → []。"""
    out = []
    for line in text.splitlines():
        tok = line.strip()
        if not tok or tok.startswith("#"):
            continue
        out.append(make_record(decode_token(tok), tok, origin=origin,
                               kind="license_key", sku_ids=sku_ids))
    return out


def records_from_jsonl(path: Path, sku_ids) -> list:
    out = []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except Exception as e:
        warn(f"{path} 读取失败：{e}")
        return out
    for i, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
        except Exception:
            warn(f"{path}:{i} 不是合法 JSON，已跳过")
            continue
        origin = f"{path}:{i}"
        if isinstance(row, str):
            out.append(make_record(decode_token(row), row, origin=origin,
                                   kind="ledger_jsonl", sku_ids=sku_ids))
        elif isinstance(row, dict):
            tok = row.get("token") or row.get("key") or row.get("license")
            if isinstance(tok, str) and tok.strip():
                tok = tok.strip()
                ov = {k: v for k, v in row.items()
                      if k not in ("token", "key", "license")}
                out.append(make_record(decode_token(tok), tok, origin=origin,
                                       kind="ledger_jsonl", overrides=ov,
                                       sku_ids=sku_ids))
            else:
                # 行本身就是 payload 对象（手工补录场景）
                out.append(make_record(row, None, origin=origin,
                                       kind="ledger_jsonl_payload", sku_ids=sku_ids))
        else:
            warn(f"{path}:{i} 结构无法识别（期望字符串或对象），已跳过")
    return out


def collect(src: Path) -> list:
    sku_ids = load_sku_ids()
    if not src.exists():
        warn(f"数据源不存在：{src}（输出空 records）。chengjie 签发端不留台账，"
             "本机无已激活授权时这是正常现象。")
        return []
    records: list = []
    if src.is_dir():
        keys = sorted(src.rglob("*.key"))
        if not keys:
            warn(f"目录 {src} 下未找到 *.key 文件（输出空 records）")
        for p in keys:
            try:
                records.extend(records_from_token_text(
                    p.read_text(encoding="utf-8-sig"), str(p), sku_ids))
            except Exception as e:
                warn(f"{p} 读取失败，已跳过：{e}")
    elif src.suffix.lower() == ".jsonl":
        records = records_from_jsonl(src, sku_ids)
    else:
        try:
            records = records_from_token_text(
                src.read_text(encoding="utf-8-sig"), str(src), sku_ids)
        except Exception as e:
            warn(f"{src} 读取失败（输出空 records）：{e}")
    if src.is_file() and not records:
        warn(f"{src} 内容为空，未产出记录")

    # 同 source_key 首见优先（同一份 key 被重复收集时去重）
    seen, unique = set(), []
    for r in records:
        if r["source_key"] in seen:
            warn(f"重复 source_key 已跳过：{r['source_key']}")
            continue
        seen.add(r["source_key"])
        unique.append(r)
    return unique


def demo_records() -> list:
    """3 条演示记录（管道联调用，覆盖 旗舰在期/基础过期/试用额度 三形态）。"""
    now = int(time.time())
    demos = [
        ({"sub": "演示客户A公司", "plan": "flagship", "iat": now - 10 * 86400,
          "exp": now + 355 * 86400, "seats": 10,
          "channels": ["telegram", "line", "web"],
          "features": {"l4": True, "white_label": True},
          "grace_days": 7, "lic_id": "CJ-DEMO-0001"}, "demo:flagship"),
        ({"sub": "演示客户B工作室", "plan": "basic", "iat": now - 400 * 86400,
          "exp": now - 30 * 86400, "seats": 3, "channels": ["web"],
          "features": {}, "grace_days": 7, "lic_id": "CJ-DEMO-0002"}, "demo:basic"),
        ({"sub": "演示试用客户C", "plan": "pro", "iat": now - 86400,
          "exp": now + 13 * 86400, "seats": 0, "channels": ["telegram", "web"],
          "features": {"l4": True}, "grace_days": 7, "lic_id": "CJ-DEMO-0003",
          "included_chars": 100000, "trial": True}, "demo:trial"),
    ]
    out = []
    for payload, origin in demos:
        fake_token = "demo." + hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()).hexdigest()
        rec = make_record(payload, fake_token, origin=origin, kind="demo")
        rec["raw"]["demo"] = True
        out.append(rec)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="chengjie 授权发放记录 → 集团统一台账归一化 JSON（只读）")
    ap.add_argument("--input", default="",
                    help=f"数据源覆盖：token 文件 / 目录(收集*.key) / .jsonl 台账"
                         f"（默认 {DEFAULT_INPUT}）")
    ap.add_argument("--out", default="chengjie_licenses.json",
                    help="输出 JSON 路径（默认 ./chengjie_licenses.json）")
    ap.add_argument("--demo", action="store_true",
                    help="不读真实数据，生成 3 条演示记录（管道联调）")
    args = ap.parse_args()

    src = Path(args.input).resolve() if args.input else DEFAULT_INPUT
    out_path = Path(args.out).resolve()

    if args.demo:
        records = demo_records()
    else:
        # 只读纪律护栏：输出禁止落在被读目录内
        guard_dir = src if src.is_dir() else src.parent
        if str(out_path).startswith(str(guard_dir) + os.sep):
            print(f"[export_chengjie] 错误: --out 不得位于被读目录内（{guard_dir}）",
                  file=sys.stderr)
            return 2
        records = collect(src)

    doc = {"version": 1, "source_system": SOURCE_SYSTEM,
           "exported_at": now_iso(), "records": records}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(f"[export_chengjie] 已导出 {len(records)} 条 → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
