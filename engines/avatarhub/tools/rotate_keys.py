#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rotate_keys.py — 发布/控制密钥轮换助手（add-then-swap，2026-07-13 P6）。

密钥泄露或例行轮换时用。**在构建机运行**（持私钥）。轮换分三步，本工具做第 1 步的机械活
（生成新钥 + 打印要粘进 release_sign.py 的新公钥 PEM），人工完成 2/3 步（发版切签、清旧钥）。

轮换协议（零断更）：
  1) `python tools/rotate_keys.py new A`（或 B）→ 生成新私钥到 secrets/*.next.pem，打印新公钥。
     把新公钥【追加】进 release_sign.py 的 PINNED_PUBKEYS（A）/ PINNED_CONTROL_PUBKEYS（B）列表
     → 发一版 app 组件（客户端更新到"信任新旧两把"）。此时仍用【旧】私钥签。
  2) 确认存量客户端都升到了含新公钥的版本（看板版本分布）后：
     `python tools/rotate_keys.py activate A` → 把 secrets/*.next.pem 提升为正式私钥
     （旧私钥备份为 *.old.pem）。之后发布/控制用新私钥签，新老客户端都验得过。
  3) 再发一版把【旧】公钥从列表移除（PINNED_* 只留新钥）→ 旧钥彻底退役。

安全：私钥永不打印/入库（gitignore）；密钥 A 私钥只在构建机，密钥 B 私钥在 VPS（轮换 B 时
新私钥也要部署到 VPS 的 /opt/avatarhub/secrets/）。
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
SECRETS = HERE / "secrets"
ROLE = {"A": ("release_sign_ed25519_sk.pem", "PINNED_PUBKEYS", "代码/manifest"),
        "B": ("rollout_control_ed25519_sk.pem", "PINNED_CONTROL_PUBKEYS", "放量控制")}


def _pub_pem_and_fp(sk):
    from cryptography.hazmat.primitives import serialization as s
    import hashlib
    pk = sk.public_key()
    pem = pk.public_bytes(s.Encoding.PEM, s.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
    raw = pk.public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)
    return pem, hashlib.sha256(raw).hexdigest()[:16]


def cmd_new(role: str):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization as s
    fn, listname, label = ROLE[role]
    nxt = SECRETS / (fn.replace(".pem", ".next.pem"))
    if nxt.exists():
        print(f"[rotate] 已存在待激活新钥 {nxt.name}（先 activate 或删除再 new）"); return 2
    sk = Ed25519PrivateKey.generate()
    SECRETS.mkdir(exist_ok=True)
    nxt.write_bytes(sk.private_bytes(s.Encoding.PEM, s.PrivateFormat.PKCS8, s.NoEncryption()))
    pem, fp = _pub_pem_and_fp(sk)
    print(f"[rotate] 已生成密钥 {role}（{label}）新私钥 → {nxt}（fp {fp}）")
    print(f"[rotate] 第1步：把下面公钥【追加】进 release_sign.py 的 {listname}（放列表**首位**表示"
          f"下版起用它签；验签对全列表任一通过），发一版 app 组件让客户端信任新旧两把：\n")
    print('    """' + pem.strip() + '\n    """,')
    print(f"\n[rotate] 客户端全部更新后执行：python tools/rotate_keys.py activate {role}")
    return 0


def cmd_activate(role: str):
    fn, listname, label = ROLE[role]
    cur = SECRETS / fn
    nxt = SECRETS / (fn.replace(".pem", ".next.pem"))
    if not nxt.exists():
        print(f"[rotate] 无待激活新钥 {nxt.name}（先 new {role}）"); return 2
    if cur.exists():
        old = SECRETS / (fn.replace(".pem", ".old.pem"))
        cur.replace(old)
        print(f"[rotate] 旧私钥备份 → {old.name}（异地留存后可删）")
    nxt.replace(cur)
    print(f"[rotate] 密钥 {role}（{label}）已切换：新私钥就位 {cur.name}。之后发布/控制用新私钥签。")
    if role == "B":
        print("[rotate] 记得把新的 B 私钥同步部署到 VPS /opt/avatarhub/secrets/rollout_control_ed25519_sk.pem 并重启 ingest。")
    print(f"[rotate] 第3步（稳定后）：从 release_sign.py 的 {listname} 移除旧公钥，旧钥退役。")
    return 0


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in ("new", "activate") or sys.argv[2] not in ROLE:
        print(__doc__); return 2
    try:
        import cryptography  # noqa
    except Exception:
        print("[rotate] 需 cryptography"); return 2
    return cmd_new(sys.argv[2]) if sys.argv[1] == "new" else cmd_activate(sys.argv[2])


if __name__ == "__main__":
    raise SystemExit(main())
