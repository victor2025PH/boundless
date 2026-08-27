"""供应商 API 联调探针——开户日一小时开闸的工具（一键代理 P4）。

拿到供应商账号 + API key、把 ``proxies.managed.http`` 各段填进配置后，跑本工具
逐项验证接线：**能力探测 → 余量/目录 → 余额 → 对账单 → 试算**。全程只读——
``order/make`` 这类**真扣钱的下单接口本工具绝不调用**（联调姿势不对就下单 =
对上游账户误消费），真下单验证走接入弹窗的一键卡（有幂等键与台账兜底）。

用法::

    python tools/proxy_vendor_probe.py --config D:\\chengjie-instances\\zhiliao\\data\\config\\config.local.yaml
    python tools/proxy_vendor_probe.py --config <yaml> --country JP --calc   # 附带试算核价
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.integrations.proxy_provider import (  # noqa: E402
    HttpProxyProvider,
    capability,
    parse_managed_cfg,
    quote_tokens,
    reset_http_probe_cache,
)


async def run(mcfg: dict, *, country: str, kind: str, do_calc: bool) -> int:
    http_cfg = mcfg.get("http") or {}
    prov = HttpProxyProvider(http_cfg)
    ok = True

    cap = capability(mcfg)
    print(f"[1/5] 能力探测: available={cap['available']} "
          f"(provider={cap['provider'] or '-'}, reason={cap['reason'] or '-'})")
    if mcfg.get("provider") != "http":
        print("      注意: provider 不是 http——本工具只联调 http 段，"
              "开闸前记得把 provider 切 http。")
    price = quote_tokens(mcfg, kind=kind, country=country)
    print(f"      我方报价 {country}/{kind}: {price} token/期"
          f"{'（0=未配价，不可售）' if price <= 0 else ''}")

    reset_http_probe_cache()  # 联调要新鲜数据，不吃缓存
    inv = await prov.inventory_async(kind=kind)
    if inv:
        mark = inv.get(country)
        show = "目录有(不给数)" if mark == -1 else (f"{mark} 条" if mark else "无货")
        print(f"[2/5] 余量/目录: {len(inv)} 个地区可见; {country}={show}")
    else:
        print("[2/5] 余量/目录: 未配置或拉取失败"
              f"{'（inventory 段没填 url）' if not (http_cfg.get('inventory') or {}).get('url') else '（配置了但没拉到——查 url/items/country 映射）'}")
        ok = ok and not (http_cfg.get("inventory") or {}).get("url")

    bal = await prov.vendor_balance()
    if bal is not None:
        min_alert = ((http_cfg.get("balance") or {}).get("min_alert") or 0)
        print(f"[3/5] 上游余额: {bal}（提醒线 {min_alert}）"
              f"{' ⚠ 低于提醒线' if min_alert and bal < float(min_alert) else ''}")
    else:
        print("[3/5] 上游余额: 未配置或拉取失败")
        ok = ok and not (http_cfg.get("balance") or {}).get("url")

    orders = await prov.list_vendor_orders()
    if orders is not None:
        print(f"[4/5] 对账单: 拉到 {len(orders)} 单"
              + (f"，样例 {orders[0]}" if orders else "（新账户空单正常）"))
    else:
        print("[4/5] 对账单: orders 段未配置（对账 CLI --vendor-config 会不可用）")

    if do_calc:
        raw = await prov.calc_order(country=country, kind=kind, period_days=30)
        if raw is None:
            print("[5/5] 试算: calc 段未配置（跳过）")
        else:
            print("[5/5] 试算原始响应（人工核价：上游成本 × 1.5~2 ≤ 我方 token 价）:")
            print(json.dumps(raw, ensure_ascii=False, indent=2)[:1200])
    else:
        print("[5/5] 试算: 未开 --calc（跳过）")

    print("\n提示: 全绿后真单验证走接入弹窗一键卡（带幂等键+台账+验活兜底），"
          "本工具刻意不调下单接口。")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True,
                    help="含 proxies.managed 的 yaml（如实例 config.local.yaml）")
    ap.add_argument("--country", default="JP")
    ap.add_argument("--kind", default="isp")
    ap.add_argument("--calc", action="store_true", help="附带调 calc 段试算核价")
    args = ap.parse_args()

    import yaml

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    mcfg = parse_managed_cfg(cfg)
    return asyncio.run(run(mcfg, country=args.country.upper(),
                           kind=args.kind, do_calc=args.calc))


if __name__ == "__main__":
    raise SystemExit(main())
