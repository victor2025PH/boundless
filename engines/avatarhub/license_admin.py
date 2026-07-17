# -*- coding: utf-8 -*-
"""license_admin.py — 授权管理 CLI（厂商 + 客户两端共用）。

子命令：
  fingerprint                 打印本机机器指纹（客户运行，发给厂商）
  keygen [--force]            生成厂商 Ed25519 密钥对（厂商一次性；私钥务必保密、勿入库）
  issue --machine <fp|*> --edition {trial,standard,pro} [--days N] [--licensee 名]
        [--out license.key] [--feature k=v ...]
                              厂商用私钥签发授权文件
  status                      显示本机当前授权状态（验签 + 指纹 + 有效期）

存放约定：
  私钥  secrets/license_vendor_sk.pem   （仅厂商，.gitignore 已忽略 secrets/）
  公钥  license_pubkey.pem              （随产品分发，也会内置进 license.py）
  授权  license.key                     （厂商签发给客户，客户放项目根）
"""
from __future__ import annotations

import os
import sys
import json
import time
import argparse
import secrets as _secrets
from pathlib import Path

import license as lic

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = lic.BASE_DIR
SECRETS = BASE / "secrets"
SK_FILE = SECRETS / "license_vendor_sk.pem"
PK_FILE = BASE / "license_pubkey.pem"


def _need_crypto():
    if not lic._HAVE_CRYPTO:
        print("[错误] 未安装 cryptography，无法签发 / 验签。`pip install cryptography`")
        sys.exit(2)


def cmd_fingerprint(args):
    print(lic.machine_fingerprint())


def cmd_keygen(args):
    _need_crypto()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization as ser
    if SK_FILE.exists() and not args.force:
        print(f"[跳过] 私钥已存在：{SK_FILE}（--force 覆盖会使已签发授权全部失效）")
        return
    SECRETS.mkdir(exist_ok=True)
    sk = Ed25519PrivateKey.generate()
    sk_pem = sk.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption())
    pk_pem = sk.public_key().public_bytes(ser.Encoding.PEM, ser.PublicFormat.SubjectPublicKeyInfo)
    SK_FILE.write_bytes(sk_pem)
    PK_FILE.write_bytes(pk_pem)
    # 把公钥内置进 license.py（防客户私换公钥伪造）
    _embed_pubkey(pk_pem.decode("utf-8"))
    print(f"[完成] 厂商私钥 → {SK_FILE}（保密！）")
    print(f"[完成] 厂商公钥 → {PK_FILE}（随产品分发）")
    print(f"[完成] 公钥已内置进 license.py")


def _embed_pubkey(pem: str):
    """把公钥 PEM 写入 license.py 的 _VENDOR_PUBKEY_PEM 常量。"""
    lp = BASE / "license.py"
    src = lp.read_text(encoding="utf-8")
    body = pem.strip()
    marker = '_VENDOR_PUBKEY_PEM = """'
    i = src.find(marker)
    if i < 0:
        print("[警告] 未在 license.py 找到 _VENDOR_PUBKEY_PEM 占位，跳过内置（仍可用文件公钥）。")
        return
    j = src.find('"""', i + len(marker))
    new = src[:i + len(marker)] + "\n" + body + "\n" + src[j:]
    lp.write_text(new, encoding="utf-8")


def _load_sk():
    _need_crypto()
    if not SK_FILE.exists():
        print(f"[错误] 未找到厂商私钥 {SK_FILE}，请先 `python license_admin.py keygen`。")
        sys.exit(2)
    from cryptography.hazmat.primitives import serialization as ser
    return ser.load_pem_private_key(SK_FILE.read_bytes(), password=None)


def cmd_issue(args):
    sk = _load_sk()
    feats = {}
    for kv in (args.feature or []):
        if "=" not in kv:
            print(f"[警告] 忽略非法 --feature {kv}（应为 k=v）")
            continue
        k, v = kv.split("=", 1)
        vl = v.strip().lower()
        if vl in ("true", "false"):
            feats[k.strip()] = (vl == "true")
        elif vl.lstrip("-").isdigit():
            feats[k.strip()] = int(vl)
        else:
            feats[k.strip()] = v.strip()
    now = time.time()
    expires = 0 if args.days <= 0 else now + args.days * 86400
    payload = {
        "v": 1,
        "lic_id": _secrets.token_hex(8),      # 唯一序列号：便于日后精确吊销单份授权
        "machine": args.machine,
        "edition": args.edition,
        "licensee": args.licensee or "",
        "issued": int(now),
        "expires": int(expires),
    }
    if feats:
        payload["features"] = feats
    sig = sk.sign(lic.canonical_payload(payload)).hex()
    doc = {"payload": payload, "sig": sig, "alg": "Ed25519"}
    out = Path(args.out) if args.out else lic.LICENSE_FILE
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    exp_s = "永久" if not expires else time.strftime("%Y-%m-%d", time.localtime(expires))
    print(f"[完成] 已签发 → {out}")
    print(f"  序列号: {payload['lic_id']}   机器: {args.machine}   档位: {args.edition}   到期: {exp_s}   被授权方: {args.licensee or '-'}")
    if feats:
        print(f"  覆盖能力: {feats}")
    try:   # 签发即导出：追加台账 outbox（ledger_outbox 静默钩子，绝不影响签发）
        import ledger_outbox as _lo
        _lo.record_issue(_lo.normalize_from_issue(payload, extra_raw={"out": str(out)}))
    except Exception:
        pass


def cmd_status(args):
    st = lic.load_state(force=True)
    print(json.dumps(st.to_public(), ensure_ascii=False, indent=2))


# ── 吊销名单（CRL）签发/维护：厂商用私钥签名 revocations.json，产品用同一公钥验签 ──────
REVOKE_FILE = lic.REVOCATION_FILE


def _load_crl_doc() -> dict:
    try:
        return json.loads(REVOKE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _crl_entries() -> list:
    rev = (_load_crl_doc().get("payload") or {}).get("revoked") or []
    return rev if isinstance(rev, list) else []


def _sign_and_write_crl(revoked: list):
    sk = _load_sk()
    payload = {"v": 1, "updated": int(time.time()), "revoked": revoked}
    sig = sk.sign(lic.canonical_payload(payload)).hex()
    doc = {"payload": payload, "sig": sig, "alg": "Ed25519"}
    REVOKE_FILE.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def _same_target(a: dict, b: dict) -> bool:
    return all(str(a.get(k, "")) == str(b.get(k, "")) for k in lic._REVOKE_MATCH_KEYS)


def cmd_revoke(args):
    entry = {}
    if args.machine:
        entry["machine"] = args.machine
    if args.lic_id:
        entry["lic_id"] = args.lic_id
    if args.licensee:
        entry["licensee"] = args.licensee
    if args.issued:
        entry["issued"] = int(args.issued)
    if not entry:
        print("[错误] 至少指定一个吊销目标：--machine / --lic-id / --licensee [--issued]。")
        sys.exit(2)
    entry["reason"] = args.reason or "已被厂商吊销"
    entry["ts"] = int(time.time())
    revoked = [r for r in _crl_entries() if not _same_target(r, entry)]   # 幂等：同目标覆盖
    revoked.append(entry)
    _sign_and_write_crl(revoked)
    try:   # 签发即导出：吊销事件追加台账 outbox（ledger_outbox 静默钩子，绝不影响吊销）
        import ledger_outbox as _lo
        _lo.record_issue(_lo.normalize_from_revoke(entry))
    except Exception:
        pass
    print(f"[完成] 已吊销并签名 → {REVOKE_FILE}")
    print(f"  目标 { {k: v for k, v in entry.items() if k != 'ts'} } · 当前 {len(revoked)} 条")
    print("  交付：把该文件放到产品同目录（或经激活服务 /api/revocations 分发），产品重启/≤15s 内生效。")


def cmd_unrevoke(args):
    def _hit(r):
        return ((args.machine and r.get("machine") == args.machine)
                or (args.lic_id and r.get("lic_id") == args.lic_id)
                or (args.licensee and r.get("licensee") == args.licensee))
    before = _crl_entries()
    if not (args.machine or args.lic_id or args.licensee):
        print("[错误] 至少指定一个移除目标：--machine / --lic-id / --licensee。")
        sys.exit(2)
    revoked = [r for r in before if not _hit(r)]
    _sign_and_write_crl(revoked)
    print(f"[完成] 已移除 {len(before) - len(revoked)} 条，剩 {len(revoked)} 条 → {REVOKE_FILE}")


def cmd_list_revoked(args):
    doc = _load_crl_doc()
    if not doc:
        print(f"(无吊销名单：{REVOKE_FILE} 不存在)")
        return
    payload = doc.get("payload") or {}
    verified = lic.verify_payload(payload, doc.get("sig", ""))
    upd = time.strftime("%Y-%m-%d %H:%M", time.localtime(payload.get("updated", 0)))
    print(f"吊销名单 {REVOKE_FILE}  签名={'有效' if verified else '无效或缺失'}  更新={upd}")
    for r in payload.get("revoked") or []:
        tgt = " ".join(f"{k}={r[k]}" for k in lic._REVOKE_MATCH_KEYS if r.get(k))
        print(f"  · {tgt}   reason={r.get('reason', '')}")


def main():
    ap = argparse.ArgumentParser(description="AvatarHub 授权管理")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fingerprint", help="打印本机机器指纹")
    kg = sub.add_parser("keygen", help="生成厂商密钥对")
    kg.add_argument("--force", action="store_true")
    iss = sub.add_parser("issue", help="签发授权文件")
    iss.add_argument("--machine", required=True, help="客户机器指纹，或 * 表示站点授权")
    iss.add_argument("--edition", default="standard", choices=["trial", "standard", "pro"])
    iss.add_argument("--days", type=int, default=365, help="有效天数，<=0 为永久")
    iss.add_argument("--licensee", default="", help="被授权方名称（仅展示）")
    iss.add_argument("--out", default="", help="输出文件，默认 license.key")
    iss.add_argument("--feature", action="append", help="覆盖能力 k=v，可多次")
    sub.add_parser("status", help="显示本机授权状态")

    rv = sub.add_parser("revoke", help="吊销授权（写入并签名 revocations.json）")
    rv.add_argument("--machine", default="", help="按机器指纹吊销")
    rv.add_argument("--lic-id", dest="lic_id", default="", help="按授权序列号吊销")
    rv.add_argument("--licensee", default="", help="按被授权方吊销")
    rv.add_argument("--issued", default="", help="配合 --machine 精确到某次签发（issued 时间戳，避免误伤同机重签）")
    rv.add_argument("--reason", default="", help="吊销原因（展示给客户）")

    urv = sub.add_parser("unrevoke", help="从吊销名单移除目标并重签")
    urv.add_argument("--machine", default="")
    urv.add_argument("--lic-id", dest="lic_id", default="")
    urv.add_argument("--licensee", default="")

    sub.add_parser("list-revoked", help="列出当前吊销名单")

    args = ap.parse_args()
    {"fingerprint": cmd_fingerprint, "keygen": cmd_keygen,
     "issue": cmd_issue, "status": cmd_status, "revoke": cmd_revoke,
     "unrevoke": cmd_unrevoke, "list-revoked": cmd_list_revoked}[args.cmd](args)


if __name__ == "__main__":
    main()
