#!/usr/bin/env python3
"""fulfill_chatx_watch.py — chatx 订单履约守护（厂商机离线私钥自动开通）。

Sprint4：把 Sprint3 的「按单签发原语」升级为守护——轮询官网 paid 的 chatx 订单，用本地
私钥签发 license token，回填到订单（status=activated + code=token），website 侧自动私信客户；
客户在 chengjie 设置页粘贴 token 激活（离线验签）。与 avatarhub/fulfill_orders.py 运维一致：

  python scripts/fulfill_chatx_watch.py --priv config/.vendor_license_private.pem            # 单次
  python scripts/fulfill_chatx_watch.py --priv ... --watch 300                               # 每 300s 常驻
  python scripts/fulfill_chatx_watch.py --priv ... --dry-run                                 # 只看不签

配置（按序）：命令行 > 环境变量。
  站点：--site 或 env CHATX_SITE / BD_SITE / AVH_SITE
  鉴权：env ADMIN_KEY（经 header x-setup-key，同 avatarhub）
  私钥：--priv（Ed25519 hex，离线保管，绝不入库）

安全：私钥只在本机；website 永不签发。纯履约决策逻辑在 src/licensing/chatx_fulfillment.py
（is_chatx_order / select_fulfillable / fulfillment_payload_for_order），本文件只做 HTTP + 签名 + state。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from src.licensing.chatx_fulfillment import select_fulfillable  # noqa: E402
from src.licensing.license_manager import issue_license  # noqa: E402

STATE_FILE = BASE / "config" / "fulfilled_chatx.json"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def load_conf(cli_site: str, cli_priv: str) -> dict:
    site = cli_site or os.environ.get("CHATX_SITE") or os.environ.get("BD_SITE") \
        or os.environ.get("AVH_SITE") or ""
    key = os.environ.get("ADMIN_KEY", "")
    if not site or not key:
        print("[错误] 缺站点地址（--site / CHATX_SITE）或 ADMIN_KEY 环境变量。", file=sys.stderr)
        sys.exit(2)
    priv = Path(cli_priv)
    if not priv.is_file():
        print(f"[错误] 私钥文件不存在：{priv}", file=sys.stderr)
        sys.exit(2)
    return {"site": site.rstrip("/"), "key": key, "priv_hex": priv.read_text(encoding="utf-8").strip()}


def http_json(url: str, payload: dict | None = None, key: str = "") -> dict:
    """与 avatarhub/fulfill_orders.py 同构：GET(无 body)/POST(有 body)，header x-setup-key。"""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Content-Type": "application/json", "x-setup-key": key},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"done": {}}


def save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def run_once(conf: dict, dry: bool) -> int:
    st = load_state()
    done_ids = set(st.get("done", {}).keys())
    try:
        resp = http_json(f"{conf['site']}/api/admin/orders?status=paid", key=conf["key"])
    except Exception as e:  # noqa: BLE001
        print(f"[错误] 拉订单失败：{e}", file=sys.stderr)
        return 0
    orders = resp.get("orders", []) if resp.get("ok") else []
    todo = select_fulfillable(orders, done_ids)
    handled = 0
    for order, payload in todo:
        oid = str(order.get("id") or "")
        print(f"[签发] {oid} · {payload.get('sku_id')} · plan={payload.get('plan')} "
              f"seats={payload.get('seats')} · {order.get('contact', '')}")
        if dry:
            continue
        try:
            token = issue_license(payload, conf["priv_hex"])
            r = http_json(
                f"{conf['site']}/api/admin/order-status",
                {"id": oid, "status": "activated", "code": token}, key=conf["key"])
            if r.get("ok"):
                st.setdefault("done", {})[oid] = int(time.time())
                save_state(st)
                handled += 1
                print(f"[开通] {oid} ✓ website 已回填 code，客户可在设置页粘贴激活")
            else:
                print(f"[错误] {oid} 回填失败：{r}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"[错误] {oid} 履约异常：{e}", file=sys.stderr)
    return handled


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="chatx 订单履约守护（签发并回填开通）")
    ap.add_argument("--priv", required=True, help="厂商 Ed25519 私钥文件（hex，离线保管）")
    ap.add_argument("--site", default="", help="官网地址（默认取 CHATX_SITE/BD_SITE/AVH_SITE）")
    ap.add_argument("--watch", type=int, default=0, help="常驻轮询间隔秒数（0=单次）")
    ap.add_argument("--dry-run", action="store_true", help="只列出待履约订单，不签发")
    args = ap.parse_args(argv)
    conf = load_conf(args.site, args.priv)
    if args.watch > 0:
        print(f"[履约] 常驻模式，每 {args.watch}s 轮询 {conf['site']}（Ctrl-C 退出）")
        while True:
            try:
                run_once(conf, args.dry_run)
            except KeyboardInterrupt:
                break
            except Exception as e:  # noqa: BLE001
                print(f"[错误] 本轮异常：{e}", file=sys.stderr)
            time.sleep(args.watch)
        return 0
    n = run_once(conf, args.dry_run)
    print(f"[履约] 完成，本次开通 {n} 单。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
