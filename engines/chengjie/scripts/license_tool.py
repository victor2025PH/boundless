#!/usr/bin/env python3
"""厂商侧授权工具：生成密钥对 + 签发授权码（离线）。

用法
====
1) 生成厂商密钥对（一次性，私钥离线保管、切勿入库）::

     python scripts/license_tool.py genkeys --out config/.vendor_license_private.pem

   会打印 public_hex —— 将其替换到
   ``src/licensing/license_manager.py::DEFAULT_VENDOR_PUBLIC_KEY_HEX``。

2) 签发授权码::

     python scripts/license_tool.py issue \\
         --priv config/.vendor_license_private.pem \\
         --sub "示例客户公司" --plan pro --days 30 \\
         --seats 10 --channels telegram,line,web \\
         --features l4,white_label \\
         --out config/license.key

   把生成的 license.key 交付给客户放到其 ``config/license.key`` 即可。

3) 签发字符加量凭证（charpack；客户在 会员中心 → 兑换加量包 粘贴）::

     python scripts/license_tool.py topup \\
         --priv config/.vendor_license_private.pem \\
         --chars 1500000 --ref ORD-2026-001 \\
         --lic-id lingox-pro-123          # 或 --sub "客户标识"（至少给一个）

   ``--ref`` 是兑换幂等键（订单号），同 ref 在同一实例只会入账一次。
   大客户一次买 N 包：加 ``--count 10`` → ref 自动派生 ``ORD-2026-001-01..-10``，
   每张独立幂等；``--out`` 时写成每行 ``ref<TAB>token`` 的清单文件。
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.licensing import generate_keypair, issue_license  # noqa: E402


def _cmd_genkeys(args: argparse.Namespace) -> int:
    kp = generate_keypair()
    Path(args.out).write_text(kp["private_hex"], encoding="utf-8")
    print(f"私钥已写入：{args.out}（请离线保管，切勿入库）")
    print(f"public_hex = {kp['public_hex']}")
    print("→ 将上面 public_hex 替换到 src/licensing/license_manager.py 的 "
          "DEFAULT_VENDOR_PUBLIC_KEY_HEX")
    return 0


def _cmd_issue(args: argparse.Namespace) -> int:
    priv_hex = Path(args.priv).read_text(encoding="utf-8").strip()
    payload = {
        "sub": args.sub,
        "plan": args.plan,
        "iat": int(time.time()),
        "seats": int(args.seats),
        "channels": [c.strip() for c in (args.channels or "").split(",") if c.strip()],
        "features": {f.strip(): True for f in (args.features or "").split(",") if f.strip()},
    }
    if args.days and int(args.days) > 0:
        payload["exp"] = int(time.time()) + int(args.days) * 86400
    if args.lic_id:
        payload["lic_id"] = args.lic_id
    # P0-4 免费试用（字符额度）：翻译/TTS 合计含量；0/省略 = 不限
    if args.chars and int(args.chars) > 0:
        payload["included_chars"] = int(args.chars)
    if args.trial:
        payload["trial"] = True
    token = issue_license(payload, priv_hex)
    if args.out:
        Path(args.out).write_text(token, encoding="utf-8")
        print(f"授权码已写入：{args.out}")
    else:
        print(token)
    print("payload:", json.dumps(payload, ensure_ascii=False))
    try:  # P1 签发即台账：追加归一化记录到本地 outbox（fail-silent，不改变本工具输出）
        from ledger_outbox import normalize_issue, record_issue
        record_issue(normalize_issue(payload, token))
    except Exception:
        pass
    return 0


def _cmd_topup(args: argparse.Namespace) -> int:
    from src.licensing.topup_voucher import batch_refs, issue_topup_voucher
    priv_hex = Path(args.priv).read_text(encoding="utf-8").strip()
    count = max(1, int(args.count or 1))
    if count > 500:
        print("[错误] --count 上限 500（防误操作批量灌爆）", file=sys.stderr)
        return 2
    pairs = [
        (r, issue_topup_voucher(
            priv_hex, chars=int(args.chars), ref=r,
            lic_id=args.lic_id, customer=args.sub, note=args.note))
        for r in batch_refs(args.ref, count)
    ]
    if args.out:
        if count == 1:
            Path(args.out).write_text(pairs[0][1], encoding="utf-8")
            print(f"加量凭证已写入：{args.out}")
        else:
            Path(args.out).write_text(
                "".join(f"{r}\t{t}\n" for r, t in pairs), encoding="utf-8")
            print(f"加量凭证已写入：{args.out}（{count} 张，每行 ref<TAB>token）")
    else:
        for r, t in pairs:
            print(t if count == 1 else f"{r}\t{t}")
    bind = f"lic={args.lic_id}" if args.lic_id else f"sub={args.sub}"
    if count == 1:
        print(f"绑定：{bind} · chars={args.chars} · ref={args.ref}"
              "（客户：会员中心 → 兑换加量包 粘贴）")
    else:
        print(f"绑定：{bind} · chars={args.chars} × {count} 张 · "
              f"ref={pairs[0][0]}..{pairs[-1][0]}"
              "（每张独立幂等，客户逐张粘贴兑换）")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="厂商授权工具（离线签发）")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("genkeys", help="生成 Ed25519 密钥对")
    g.add_argument("--out", default="config/.vendor_license_private.pem",
                   help="私钥输出路径")
    g.set_defaults(func=_cmd_genkeys)

    i = sub.add_parser("issue", help="签发授权码")
    i.add_argument("--priv", required=True, help="厂商私钥文件路径")
    i.add_argument("--sub", required=True, help="客户标识（公司名）")
    i.add_argument("--plan", default="pro",
                   choices=["community", "basic", "pro", "flagship"])
    i.add_argument("--days", default="0", help="有效天数（0=永久）")
    i.add_argument("--seats", default="0", help="最大坐席席位（0=不限）")
    i.add_argument("--channels", default="", help="允许渠道，逗号分隔")
    i.add_argument("--features", default="", help="功能位，逗号分隔（如 l4,white_label）")
    i.add_argument("--lic-id", dest="lic_id", default="", help="授权编号")
    i.add_argument("--chars", default="0",
                   help="含翻译/TTS 字符额度（0=不限；试用授权配合 --trial 用）")
    i.add_argument("--trial", action="store_true", help="标记为试用授权")
    i.add_argument("--out", default="", help="授权码输出路径（默认打印）")
    i.set_defaults(func=_cmd_issue)

    t = sub.add_parser("topup", help="签发字符加量凭证（charpack）")
    t.add_argument("--priv", required=True, help="厂商私钥文件路径")
    t.add_argument("--chars", required=True, help="加量字符数（如 1500000）")
    t.add_argument("--ref", required=True, help="订单号（兑换幂等键）")
    t.add_argument("--count", default="1",
                   help="批量签发张数（>1 时 ref 自动派生 -01..-NN，每张独立幂等）")
    t.add_argument("--lic-id", dest="lic_id", default="", help="精确绑定授权编号")
    t.add_argument("--sub", default="", help="客户级绑定（与授权 sub/customer 一致）")
    t.add_argument("--note", default="", help="备注（随入账记录展示）")
    t.add_argument("--out", default="", help="凭证输出路径（默认打印）")
    t.set_defaults(func=_cmd_topup)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
