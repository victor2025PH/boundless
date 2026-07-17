# -*- coding: utf-8 -*-
"""fulfill_orders.py — 官网订单本地履约（私钥不出本机的自动开通）。

链路：官网下单（留机器指纹）→ USDT 到账自动核销（usdt-watch 标记 paid）→
      本脚本在厂商机轮询「已到账」订单 → 用本地私钥按指纹签发授权 →
      把整份签名授权（base64）回填到订单并标记「已开通」→
      客户在 /order 状态页自取授权码，粘贴进客户端「🔑 授权」即激活（离线验签）。

为什么不把签发搬上服务器：Ed25519 私钥留在厂商机是整个授权体系的安全底座；
服务器只做「订单载体 + 状态机」，被攻破也伪造不了授权。

用法：
  python fulfill_orders.py            # 单次运行（配 Windows 计划任务每 5 分钟）
  python fulfill_orders.py --watch 300   # 常驻轮询，每 300 秒一轮
  python fulfill_orders.py --dry-run     # 只看不做

配置来源（按序）：环境变量 > secrets/deploy/prod.env.local.bak（ADMIN_KEY）>
  secrets/deploy/deploy.config.json（站点地址）。
无指纹的已到账订单不自动开通，转 Telegram 提醒人工跟进（找客户要指纹）。
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
DEPLOY_DIR = BASE / "secrets" / "deploy"
STATE_FILE = BASE / "secrets" / "fulfilled_orders.json"

sys.path.insert(0, str(BASE))
import license as lic            # noqa: E402
import license_admin as la       # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 官网档位 → 引擎授权档（enterprise 引擎侧同 pro 全开能力，另加座席特性）
EDITION_MAP = {"trial": "trial", "standard": "standard", "pro": "pro", "enterprise": "pro"}
# 订阅周期 → 授权天数（留 1 天缓冲，跨时区/链上确认延迟不吃亏在客户头上）
PERIOD_DAYS = {"monthly": 32, "annual": 366}


def load_conf() -> dict:
    import os
    site, key = os.environ.get("AVH_SITE", ""), os.environ.get("ADMIN_KEY", "")
    if not site:
        try:
            cfg = json.loads((DEPLOY_DIR / "deploy.config.json").read_text(encoding="utf-8"))
            site = cfg.get("site", {}).get("url", "")
        except Exception:
            pass
    if not key:
        try:
            for line in (DEPLOY_DIR / "prod.env.local.bak").read_text(encoding="utf-8").splitlines():
                m = re.match(r"^ADMIN_KEY=(.+)$", line.strip())
                if m:
                    key = m.group(1).strip()
                    break
        except Exception:
            pass
    if not site or not key:
        print("[错误] 缺站点地址或 ADMIN_KEY（查 secrets/deploy/ 下配置）。")
        sys.exit(2)
    return {"site": site.rstrip("/"), "key": key}


def http_json(url: str, payload: dict | None = None, key: str = "") -> dict:
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
        return {"done": {}, "reminded": {}}


def save_state(st: dict):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def sign_license_b64(fingerprint: str, edition: str, days: int, licensee: str) -> str:
    """本地私钥签发一份绑指纹授权，返回整份 JSON 的 base64（客户端粘贴框直接吃）。"""
    sk = la._load_sk()
    import secrets as _secrets
    now = time.time()
    payload = {
        "v": 1,
        "lic_id": _secrets.token_hex(8),
        "machine": fingerprint,
        "edition": edition,
        "licensee": licensee or "",
        "issued": int(now),
        "expires": int(now + days * 86400) if days > 0 else 0,
    }
    doc = {"payload": payload, "sig": sk.sign(lic.canonical_payload(payload)).hex(), "alg": "Ed25519"}
    return base64.b64encode(json.dumps(doc, ensure_ascii=False).encode("utf-8")).decode("ascii")


def run_once(conf: dict, dry: bool) -> int:
    st = load_state()
    try:
        resp = http_json(f"{conf['site']}/api/admin/orders?status=paid", key=conf["key"])
    except Exception as e:
        print(f"[错误] 拉订单失败：{e}")
        return 0
    orders = resp.get("orders", []) if resp.get("ok") else []
    handled = 0
    for o in orders:
        oid = o.get("id", "")
        if not oid or oid in st["done"] or o.get("code"):
            continue
        fp = (o.get("fingerprint") or "").strip()
        edition = EDITION_MAP.get(o.get("edition", ""), "")
        days = PERIOD_DAYS.get(o.get("period", ""), 366)
        if not fp or not edition:
            # 无指纹/未知档：人工跟进（提醒一次，不刷屏）
            if oid not in st["reminded"]:
                st["reminded"][oid] = int(time.time())
                save_state(st)
                print(f"[跟进] {oid} 已到账但{'无指纹' if not fp else '档位未知'}，需联系客户：{o.get('contact')}")
            continue
        print(f"[签发] {oid} · {o.get('plan')} ({edition}) · {days} 天 · 指纹 {fp[:16]}…")
        if dry:
            continue
        try:
            code_b64 = sign_license_b64(fp, edition, days, licensee=o.get("contact", "")[:80])
            r = http_json(f"{conf['site']}/api/admin/order-status",
                          {"id": oid, "status": "activated", "code": code_b64}, key=conf["key"])
            if r.get("ok"):
                st["done"][oid] = int(time.time())
                save_state(st)
                handled += 1
                print(f"[开通] {oid} ✓ 客户可在 {conf['site']}/order?check={oid} 自取授权码")
                try:   # 签发即导出：履约成功追加台账 outbox（ledger_outbox 静默钩子，绝不影响履约）
                    import ledger_outbox as _lo
                    _pl = json.loads(base64.b64decode(code_b64)).get("payload") or {}
                    _lo.record_issue(_lo.normalize_from_fulfillment(oid, _pl, st["done"][oid]))
                except Exception:
                    pass
            else:
                print(f"[错误] {oid} 回填失败：{r}")
        except Exception as e:
            print(f"[错误] {oid} 履约异常：{e}")
    return handled


def main():
    ap = argparse.ArgumentParser(description="官网订单本地履约（签发授权并回填开通）")
    ap.add_argument("--watch", type=int, default=0, help="常驻轮询间隔秒数（0=单次）")
    ap.add_argument("--dry-run", action="store_true", help="只列出待履约订单，不签发")
    args = ap.parse_args()
    conf = load_conf()
    if args.watch > 0:
        print(f"[履约] 常驻模式，每 {args.watch}s 轮询 {conf['site']}（Ctrl-C 退出）")
        while True:
            try:
                run_once(conf, args.dry_run)
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"[错误] 本轮异常：{e}")
            time.sleep(args.watch)
    else:
        n = run_once(conf, args.dry_run)
        print(f"[履约] 完成，本次开通 {n} 单。")


if __name__ == "__main__":
    main()
