#!/usr/bin/env python3
"""fulfill_chatx.py — 按订单签发 chatx license（厂商机离线私钥用）。

Sprint3 自动履约的「签发」环节（补齐 chengjie 缺失的履约层，对标 avatarhub/fulfill_orders.py）：

  website 标 paid → 本脚本(厂商机, Ed25519 私钥不出本机)按 sku 映射权威 payload
  → 签发 license token → 交付客户(回填 order.code / TG 发码)
  → 客户 chengjie 设置页粘贴 → POST /api/admin/license/activate 激活（离线验签）。

安全：私钥仅经 --priv 传入，**绝不入库/上服务器**。SKU→payload 映射见
src/licensing/chatx_fulfillment.py（plan/seats/channels/有效期，业务可调）。

用法：
  # 生成厂商密钥对（一次性；见 scripts/license_tool.py genkeys）
  python scripts/fulfill_chatx.py --sku chatx-team --sub "客户公司名" \
      --priv config/.vendor_license_private.pem --order-id ORD123 --out license.key
  # 不写文件则打印 token 到 stdout（便于回填 order.code）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.licensing.chatx_fulfillment import CHATX_SKU_SPECS, build_issue_payload
from src.licensing.license_manager import issue_license


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="按订单签发 chatx license（厂商机离线私钥）")
    ap.add_argument("--sku", required=True, choices=sorted(CHATX_SKU_SPECS),
                    help="chatx SKU（chatx-entry/team/flagship）")
    ap.add_argument("--sub", required=True, help="客户标识（公司名/邮箱），写入 payload.sub")
    ap.add_argument("--priv", required=True, help="厂商 Ed25519 私钥文件（hex，离线保管）")
    ap.add_argument("--order-id", dest="order_id", default="", help="订单号 → lic_id")
    ap.add_argument("--days", type=int, default=None, help="有效天数（默认 32；<=0 永久）")
    ap.add_argument("--out", default="", help="输出 license.key 路径（默认打印 stdout）")
    args = ap.parse_args(argv)

    priv_path = Path(args.priv)
    if not priv_path.is_file():
        print(f"ERROR: 私钥文件不存在: {priv_path}", file=sys.stderr)
        return 2
    priv_hex = priv_path.read_text(encoding="utf-8").strip()

    payload = build_issue_payload(
        args.sku, customer=args.sub, order_id=args.order_id, days=args.days)
    token = issue_license(payload, priv_hex)

    if args.out:
        Path(args.out).write_text(token + "\n", encoding="utf-8")
        print(f"OK: license 已写入 {args.out}")
    else:
        print(token)
    # 台账摘要（stderr，不污染 stdout token）
    print(
        f"issued sku={args.sku} plan={payload['plan']} seats={payload['seats']} "
        f"channels={','.join(payload['channels'])} sub={args.sub} "
        f"lic_id={payload.get('lic_id', '-')}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
