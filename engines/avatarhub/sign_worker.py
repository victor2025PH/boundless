# -*- coding: utf-8 -*-
"""sign_worker.py — 官网后台的「本机签发机」（私钥永不上服务器的关键一环）。

链路（与 fulfill_orders.py 同一安全模型）：
  官网 /console 客服签发/发码/吊销 → 写「签发队列」→ 本脚本在厂商机轮询队列 →
  用本地 Ed25519 私钥就地签发 → 回填官网 → 客户端在线激活/下载 key 即得授权。
  私钥只在这台机器（secrets/license_vendor_sk.pem），服务器被攻破也伪造不了授权。

用法：
  python sign_worker.py                 # 单次（配 Windows 计划任务每 1-5 分钟）
  python sign_worker.py --watch 20      # 常驻，每 20 秒一轮（签发体感接近即时）
  python sign_worker.py --dry-run       # 只看有多少待签，不签

配置来源（复用 fulfill_orders 的约定）：环境变量 AVH_SITE / ADMIN_KEY >
  secrets/deploy/deploy.config.json（site）+ secrets/deploy/prod.env.local.bak（ADMIN_KEY）。
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import license as lic            # noqa: E402
import license_admin as la       # noqa: E402
import fulfill_orders as fo      # noqa: E402  复用 load_conf / http_json

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _sign_doc(sk, payload: dict) -> str:
    """签一份授权 → 返回整份 {payload,sig,alg} JSON 的 base64（官网/客户端直接吃）。"""
    sig = sk.sign(lic.canonical_payload(payload)).hex()
    doc = {"payload": payload, "sig": sig, "alg": "Ed25519"}
    return base64.b64encode(json.dumps(doc, ensure_ascii=False).encode("utf-8")).decode("ascii")


def _sign_crl(sk, payload: dict) -> dict:
    """签吊销名单 → 返回 {payload,sig,alg}（官网 /api/revocations 原样分发）。"""
    sig = sk.sign(lic.canonical_payload(payload)).hex()
    return {"payload": payload, "sig": sig, "alg": "Ed25519"}


def run_once(conf: dict, sk, dry: bool) -> int:
    site, key = conf["site"], conf["key"]
    try:
        pull = fo.http_json(f"{site}/api/console/sign/pull", key=key)
    except Exception as e:
        print(f"[错误] 拉签发队列失败：{e}")
        return 0
    if not pull.get("ok"):
        print(f"[错误] 拉队列返回异常：{pull}")
        return 0
    requests = pull.get("requests", []) or []
    crl = pull.get("crl", {}) or {}
    handled = 0

    for r in requests:
        rid = r.get("id")
        payload = r.get("signPayload")
        if not rid or not isinstance(payload, dict):
            continue
        lic_id = r.get("licId", "")
        print(f"[签发] {rid} · lic={lic_id} · {payload.get('edition')} · 机器 {str(payload.get('machine'))[:16]}…")
        if dry:
            continue
        try:
            doc_b64 = _sign_doc(sk, payload)
            resp = fo.http_json(f"{site}/api/console/sign/complete", {"id": rid, "doc": doc_b64}, key=key)
            if resp.get("ok"):
                handled += 1
                print(f"  ✓ 已回填 {lic_id}")
            else:
                print(f"  ✗ 回填失败：{resp}")
        except Exception as e:
            print(f"  ✗ 签发异常：{e}")
            try:
                fo.http_json(f"{site}/api/console/sign/complete", {"id": rid, "error": str(e)[:200]}, key=key)
            except Exception:
                pass

    if crl.get("pending") and isinstance(crl.get("payload"), dict):
        n = len(crl["payload"].get("revoked", []))
        print(f"[CRL] 重签吊销名单（{n} 条）")
        if not dry:
            try:
                crl_doc = _sign_crl(sk, crl["payload"])
                resp = fo.http_json(f"{site}/api/console/sign/complete", {"crlDoc": crl_doc}, key=key)
                print(f"  {'✓ 已下发' if resp.get('ok') else '✗ 失败：' + str(resp)}")
                if resp.get("ok"):
                    handled += 1
            except Exception as e:
                print(f"  ✗ CRL 签名异常：{e}")

    return handled


def main():
    ap = argparse.ArgumentParser(description="官网后台本机签发机（私钥不出本机）")
    ap.add_argument("--watch", type=int, default=0, help="常驻轮询间隔秒数（0=单次）")
    ap.add_argument("--dry-run", action="store_true", help="只看待签数量，不签")
    args = ap.parse_args()

    conf = fo.load_conf()
    sk = la._load_sk()   # 复用 license_admin 的私钥加载（缺失会明确报错退出）
    print(f"[签发机] 站点 {conf['site']} · 私钥已加载 · 公钥指纹自洽")

    if args.watch > 0:
        print(f"[签发机] 常驻模式，每 {args.watch}s 一轮（Ctrl-C 退出）")
        while True:
            try:
                n = run_once(conf, sk, args.dry_run)
                if n:
                    print(f"[签发机] 本轮处理 {n} 项")
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"[错误] 本轮异常：{e}")
            time.sleep(args.watch)
    else:
        n = run_once(conf, sk, args.dry_run)
        print(f"[签发机] 完成，本次处理 {n} 项。")


if __name__ == "__main__":
    main()
