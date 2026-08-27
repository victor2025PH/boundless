#!/usr/bin/env python3
"""fulfill_chatx_watch.py — chatx/lingox 订单履约守护（厂商机离线私钥自动开通）。

融合实例 P4 起双产品线通吃：chatx（智聊）与 lingox（通译）订单同一守护签发，
映射表见 src/licensing/chatx_fulfillment.py（lingox 档=翻译线 basic+显式 features；
lingox-charpack 等加购型 SKU 走 [人工] 点名，不自动签发）。

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

from src.licensing.chatx_fulfillment import (  # noqa: E402
    is_recharge_sku,
    manual_followup_orders,
    plan_recharge_fulfillment,
    select_fulfillable,
    select_topup_fulfillable,
)
from src.licensing.license_manager import issue_license  # noqa: E402
from src.licensing.topup_voucher import issue_topup_voucher  # noqa: E402

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


def _fetch_recharge_context(conf: dict, paid_orders: list) -> tuple:
    """充值单判定所需的数据面：历史已开通单（首充/每账号一次） + 试用台账（新人 72h 窗）。

    只有 paid 队列里真有充值单才发起额外请求（多数轮询周期零开销）。
    任一拉取失败 → 对应传 None，纯逻辑层按保守放行语义退化（见 chatx_fulfillment
    docstring）——绝不因辅助数据抖动卡住整条履约链。
    claims 用 ``peek=1``：这不是试用履约端，绝不能刷新 fulfiller 心跳
    （否则试用签发链死了监控还以为活着）。
    """
    if not any(is_recharge_sku(str((o or {}).get("sku_id") or "")) for o in paid_orders):
        return None, None
    history = None
    claims = None
    try:
        r = http_json(f"{conf['site']}/api/admin/orders?status=activated", key=conf["key"])
        if r.get("ok"):
            history = list(paid_orders) + list(r.get("orders") or [])
    except Exception as e:  # noqa: BLE001
        print(f"[警告] 拉历史订单失败（首充判定按保守语义退化）：{e}", file=sys.stderr)
    try:
        r = http_json(f"{conf['site']}/api/admin/trial-claims?peek=1&limit=5000",
                      key=conf["key"])
        if r.get("ok"):
            claims = list(r.get("claims") or [])
    except Exception as e:  # noqa: BLE001
        print(f"[警告] 拉试用台账失败（新人窗按无 claim 语义退化）：{e}", file=sys.stderr)
    if history is None:
        history = list(paid_orders)  # 至少让同轮并付的并列裁决成立
    return history, claims


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
    # 本引擎订单但映射不出 payload（charpack 加购/老单缺 sku）→ 点名转人工，绝不静默漏单
    for o in manual_followup_orders(orders, done_ids):
        print(f"[人工] {o.get('id')} · {o.get('sku_id') or '(缺 sku)'} · "
              f"{o.get('contact', '')} —— 不可自动履约（历史单/缺 contact），请人工跟进")
    handled = 0
    # 充值档（recharge-* / 新人包，2026-08-21 实施50）：首充加赠/新人 72h 由纯逻辑层
    # 按官网台账现算；凭证载荷=chars（1 Token=100 字符并账口径），会员中心粘贴兑换。
    history, claims = _fetch_recharge_context(conf, orders)
    plan = plan_recharge_fulfillment(orders, done_ids, history=history, claims=claims)
    for order, reason in plan["manual"]:
        print(f"[人工] {order.get('id')} · {order.get('sku_id')} · "
              f"{order.get('contact', '')} —— 充值单不符自动履约条件（{reason}），请人工裁决")
    for order, vargs, meta in plan["issue"]:
        oid = str(order.get("id") or "")
        if str(order.get("sku_id")) == "recharge-newbie-6":
            tag = "新人包x2"
        elif meta.get("first_charge"):
            tag = f"首充+{meta['bonus_pct']}%" if meta.get("bonus_pct") else "首充"
        elif meta.get("vip_pct"):
            tag = f"复充VIP+{meta['vip_pct']}%(累计{meta.get('cum_usd')}U)"
        else:
            tag = "复充"
        print(f"[充值] {oid} · {order.get('sku_id')} · {tag} · "
              f"tokens={meta['tokens']} chars={vargs['chars']} · {vargs['customer']}")
        if dry:
            continue
        try:
            token = issue_topup_voucher(conf["priv_hex"], **vargs)
            r = http_json(
                f"{conf['site']}/api/admin/order-status",
                {"id": oid, "status": "activated", "code": token}, key=conf["key"])
            if r.get("ok"):
                st.setdefault("done", {})[oid] = int(time.time())
                save_state(st)
                handled += 1
                print(f"[开通] {oid} ✓ 充值凭证已回填（{tag}），客户在会员中心粘贴兑换")
            else:
                print(f"[错误] {oid} 充值凭证回填失败：{r}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"[错误] {oid} 充值履约异常：{e}", file=sys.stderr)
    # 加购包（charpack）→ topup 凭证：与授权码同一交付通道（回填 code，官网私信客户），
    # 客户在 会员中心 → 兑换加量包 粘贴入账（ref=订单号幂等）。
    for order, vargs in select_topup_fulfillable(orders, done_ids):
        oid = str(order.get("id") or "")
        # 2026-08-19 双载荷：字符包带 chars、Token 包带 tokens——日志按实际载荷打
        # （此行在 try 外，硬取缺失键会 KeyError 崩掉整轮循环，实装前实测抓到）。
        amount = (f"chars={vargs['chars']}" if "chars" in vargs
                  else f"tokens={vargs.get('tokens')}")
        print(f"[凭证] {oid} · {order.get('sku_id')} · {amount} · "
              f"{vargs['customer']}")
        if dry:
            continue
        try:
            token = issue_topup_voucher(conf["priv_hex"], **vargs)
            r = http_json(
                f"{conf['site']}/api/admin/order-status",
                {"id": oid, "status": "activated", "code": token}, key=conf["key"])
            if r.get("ok"):
                st.setdefault("done", {})[oid] = int(time.time())
                save_state(st)
                handled += 1
                print(f"[开通] {oid} ✓ 加量凭证已回填，客户在会员中心粘贴兑换")
            else:
                print(f"[错误] {oid} 凭证回填失败：{r}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"[错误] {oid} 凭证履约异常：{e}", file=sys.stderr)
    # 退款点名（实施50 P2）：官网把单标成 refunded 后，首充/新人/VIP 判定自动把它
    # 当不存在（判定只拉 paid/activated）；但**已签发的凭证可能已被兑换**——离线
    # 凭证架构没有远程回收面（chars 在客户本机额度库），只能人工跟进（托管实例可
    # 后台扣回、自装机走客服协商）。每单只点名一次（state.refund_flagged）。
    try:
        r_ref = http_json(f"{conf['site']}/api/admin/orders?status=refunded", key=conf["key"])
        flagged = st.setdefault("refund_flagged", {})
        changed = False
        for o in (r_ref.get("orders") or []) if r_ref.get("ok") else []:
            oid = str((o or {}).get("id") or "")
            if not oid or oid in flagged:
                continue
            if oid in done_ids or (o or {}).get("code"):
                print(f"[退款] {oid} · {o.get('sku_id')} · {o.get('contact', '')} —— "
                      f"该单已履约，凭证可能已兑换：托管实例请后台扣回，自装机请客服跟进",
                      file=sys.stderr)
            flagged[oid] = int(time.time())
            changed = True
        if changed:
            save_state(st)
    except Exception:
        pass  # 退款巡检是旁路：官网旧版本没有 refunded 状态时静默

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
