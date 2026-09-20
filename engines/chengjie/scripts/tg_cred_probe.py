#!/usr/bin/env python3
"""Telegram 凭据池真伪探针（2026-08-10 API_ID_INVALID 事故沉淀）。

背景
====
官网凭据池（website ``POOL_TG_CREDS`` / ``POOL_TG_CREDS_FILE``）上架只做**格式**
校验（parseCreds 验「数字 api_id + 32 位 hex api_hash」），从不验 Telegram 认不认。
废组因此可以安然上架，并按机器指纹**粘定**派发——分到该组的每一台新装机都稳定
``API_ID_INVALID``，重试/刷新二维码永远无解；而客户端旧版只记 DEBUG，事故靠用户
拍照上报才被发现。本脚本补上「真伪」这一层，三个用法：

1) **上架门禁**（换池/加组前必跑，exit 1 即挡住）::

       python -m scripts.tg_cred_probe --file new_creds.json --proxy socks5://127.0.0.1:7890

2) **VPS 日巡检**（直连可达 Telegram；坏组自动经官网中继告警到管理员 TG）::

       # crontab：每天 07:00
       0 7 * * * cd /opt/chatx && python3 tg_cred_probe.py --file /etc/chatx/tg_creds.json \
           --alert-url https://bd2026.cc/api/ops/alert --alert-key "$EVENT_INGEST_KEY"

3) **事故核查**（只探某一组）::

       python -m scripts.tg_cred_probe --file creds.json --only grp-1

凭据文件格式与 website 池完全一致（单一事实源，别发明第二种格式）::

    [{"api_id":"123456","api_hash":"32位hex","max":50,"name":"grp-1"}, ...]

判定（verdict）：
  ok            ExportLoginToken 返回登录令牌 → 凭据真实有效
  bad           Telegram 明确拒绝（API_ID_INVALID / API_ID_PUBLISHED_FLOOD）→ 必须下架
  rate_limited  FLOOD_WAIT 限流 → 本轮不下判（等下轮，别把限流当废组）
  unreachable   连不上 Telegram（网络/代理问题）→ 探针环境问题，凭据无罪
  error         其它异常 → 人工看 detail

exit code：``0``=全部 ok；``1``=存在 bad（门禁语义）；``2``=一组都没探成功
（环境不可用——巡检环境自身坏了也必须响，否则看门狗静默死亡）。

依赖：``pip install pyrogram tgcrypto``（仅真探测需要；解析/分类纯函数零依赖）。
🔒 api_hash 全程不打印（只显示尾 4 位掩码）；探测用 in-memory session，不落文件。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

PROBE_TIMEOUT_SEC = 30.0
SLEEP_BETWEEN_SEC = 2.0

# ── 纯函数（可单测，零外部依赖）──────────────────────────────────────────────


def parse_pool_creds(raw: str) -> List[Dict[str, Any]]:
    """解析池 JSON（语义镜像 website/lib/tg-cred-pool.ts::parseCreds，宁缺勿错）。"""
    try:
        arr = json.loads(raw or "")
    except Exception:
        return []
    if not isinstance(arr, list):
        return []
    out: List[Dict[str, Any]] = []
    for e in arr:
        if not isinstance(e, dict):
            continue
        api_id = str(e.get("api_id") or "").strip()
        api_hash = str(e.get("api_hash") or "").strip()
        name = str(e.get("name") or f"api-{api_id}")[:40]
        if not (api_id.isdigit() and len(api_id) >= 4):
            continue
        if not (len(api_hash) == 32 and all(c in "0123456789abcdef" for c in api_hash.lower())):
            continue
        out.append({"api_id": api_id, "api_hash": api_hash, "name": name})
    return out


def mask_hash(api_hash: str) -> str:
    h = str(api_hash or "")
    return ("…" + h[-4:]) if len(h) >= 4 else "…"


def classify_probe_error(ex: Exception) -> str:
    """异常 → verdict（与 telegram_protocol_login.classify_login_exception 同哲学，
    但输出的是池运营语义：bad / rate_limited / unreachable / error）。"""
    name = type(ex).__name__
    text = str(ex or "").lower()
    if ("api_id_invalid" in text or "api_id_published_flood" in text
            or name in ("ApiIdInvalid", "ApiIdPublishedFlood")):
        return "bad"
    if "flood_wait" in text or name == "FloodWait":
        return "rate_limited"
    if "proxy" in name.lower() or isinstance(ex, (OSError, asyncio.TimeoutError)):
        return "unreachable"
    if any(k in text for k in ("timed out", "timeout", "connection", "unreachable",
                               "network", "refused", "reset", "getaddrinfo",
                               "socks", "proxy")):
        return "unreachable"
    return "error"


def parse_proxy_arg(spec: str) -> Optional[Dict[str, Any]]:
    """``socks5://user:pass@host:port`` → pyrogram proxy dict（空/非法 → None）。"""
    s = str(spec or "").strip()
    if not s:
        return None
    if "://" not in s:
        s = "socks5://" + s
    try:
        u = urllib.parse.urlsplit(s)
        if not u.hostname or not u.port:
            return None
        out: Dict[str, Any] = {
            "scheme": (u.scheme or "socks5").lower(),
            "hostname": u.hostname,
            "port": int(u.port),
        }
        if u.username:
            out["username"] = urllib.parse.unquote(u.username)
        if u.password:
            out["password"] = urllib.parse.unquote(u.password)
        return out
    except Exception:
        return None


def summarize(results: List[Dict[str, Any]]) -> Tuple[int, str]:
    """结果 → (exit_code, 单行摘要)。exit：1=有 bad；2=零成功；0=全 ok/可容忍。"""
    if not results:
        return 2, "probed=0（没有可探测的凭据组）"
    n_ok = sum(1 for r in results if r.get("verdict") == "ok")
    bad = [r for r in results if r.get("verdict") == "bad"]
    lines = [f"probed={len(results)} ok={n_ok} bad={len(bad)}"]
    for r in bad:
        lines.append(f"BAD {r.get('name')} api_id={r.get('api_id')} → {r.get('detail')}")
    if bad:
        return 1, "; ".join(lines)
    if results and n_ok == 0:
        return 2, "; ".join(lines) + "; 没有任何组探测成功（探针环境不可用？）"
    return 0, "; ".join(lines)


# ── 真探测（lazy import pyrogram；单组一连接，in-memory 不落盘）───────────────


async def probe_cred(api_id: str, api_hash: str, *,
                     proxy: Optional[Dict[str, Any]] = None,
                     timeout: float = PROBE_TIMEOUT_SEC) -> Dict[str, Any]:
    from pyrogram import Client  # noqa: WPS433 (lazy：解析/分类不需要它)
    from pyrogram.raw.functions.auth import ExportLoginToken

    t0 = time.time()
    client = Client(f"cred_probe_{api_id}", api_id=int(api_id), api_hash=api_hash,
                    in_memory=True, proxy=proxy or None)

    async def _run() -> str:
        await client.connect()
        r = await client.invoke(ExportLoginToken(
            api_id=int(api_id), api_hash=api_hash, except_ids=[]))
        return type(r).__name__

    try:
        rtype = await asyncio.wait_for(_run(), timeout=timeout)
        return {"verdict": "ok", "detail": f"login_token={rtype}",
                "ms": int((time.time() - t0) * 1000)}
    except Exception as ex:  # noqa: BLE001 —— 分类交给纯函数，原文只留摘要
        return {"verdict": classify_probe_error(ex),
                "detail": f"{type(ex).__name__}: {str(ex)[:160]}",
                "ms": int((time.time() - t0) * 1000)}
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


def post_alert(url: str, key: str, text: str) -> bool:
    """坏组告警经官网 /api/ops/alert 中继到管理员 TG（Bearer=EVENT_INGEST_KEY）。"""
    try:
        body = json.dumps({"text": text[:1000], "source": "tg-cred-probe"}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("content-type", "application/json")
        req.add_header("authorization", f"Bearer {key}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception:  # noqa: BLE001
        return False


async def _amain(args: argparse.Namespace) -> int:
    import os

    raw = ""
    if args.file:
        with open(args.file, "r", encoding="utf-8") as fh:
            raw = fh.read()
    else:
        path = (os.environ.get("POOL_TG_CREDS_FILE") or "").strip()
        if path:
            with open(path, "r", encoding="utf-8") as fh:
                raw = fh.read()
        else:
            raw = (os.environ.get("POOL_TG_CREDS") or "").strip()
    creds = parse_pool_creds(raw)
    if args.only:
        creds = [c for c in creds if c["name"] == args.only or c["api_id"] == args.only]
    if not creds:
        print("没有可探测的凭据组（检查 --file / POOL_TG_CREDS[_FILE]；注意格式校验会剔除非法条目）")
        return 2

    proxy = parse_proxy_arg(args.proxy)
    results: List[Dict[str, Any]] = []
    for i, c in enumerate(creds):
        if i:
            await asyncio.sleep(max(0.0, float(args.sleep)))
        res = await probe_cred(c["api_id"], c["api_hash"],
                               proxy=proxy, timeout=float(args.timeout))
        row = {"name": c["name"], "api_id": c["api_id"],
               "api_hash": mask_hash(c["api_hash"]), **res}
        results.append(row)
        if not args.json:
            print(f"[{row['verdict']:>12}] {row['name']:<12} api_id={row['api_id']} "
                  f"{row['ms']}ms  {row['detail']}")

    code, summary = summarize(results)
    if args.json:
        print(json.dumps({"exit": code, "summary": summary, "results": results},
                         ensure_ascii=False, indent=2))
    else:
        print(summary)

    alert_key = args.alert_key or os.environ.get("EVENT_INGEST_KEY", "")
    if args.alert_url and alert_key and code in (1, 2):
        head = ("Telegram 凭据池发现废组，请立即换池"
                if code == 1 else "凭据池巡检自身失败（零成功），请检查探针环境")
        sent = post_alert(args.alert_url, alert_key, f"{head}\n{summary}")
        print(f"alert {'sent' if sent else 'FAILED'} → {args.alert_url}")
    return code


def main() -> int:
    ap = argparse.ArgumentParser(description="Telegram 凭据池真伪探针（门禁/巡检/事故核查）")
    ap.add_argument("--file", default="", help="凭据 JSON 文件（缺省读 POOL_TG_CREDS_FILE / POOL_TG_CREDS）")
    ap.add_argument("--only", default="", help="只探某一组（按 name 或 api_id）")
    ap.add_argument("--proxy", default="", help="探针出口代理，如 socks5://127.0.0.1:7890（大陆机器必配）")
    ap.add_argument("--timeout", default=PROBE_TIMEOUT_SEC, type=float, help="单组探测超时秒")
    ap.add_argument("--sleep", default=SLEEP_BETWEEN_SEC, type=float, help="组间间隔秒（温和探测）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--alert-url", default="", help="坏组告警中继端点（https://…/api/ops/alert）")
    ap.add_argument("--alert-key", default="", help="中继鉴权 key（缺省读 env EVENT_INGEST_KEY）")
    args = ap.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
