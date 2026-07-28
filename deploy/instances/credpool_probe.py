# -*- coding: utf-8 -*-
r"""中央凭据池「凭据可用性」探针——真打 Telegram 验每组 api_id/api_hash 能不能用。

为什么这样验：Telegram 官方**没有**「查某个 api_id 被多少账号关联过」的接口
（那是服务端信息，MTProto 不暴露）。但「这组 api_id/api_hash 是不是有效」可以真验——
走二维码登录的第一步 `auth.ExportLoginToken`（和线上扫码登录同一个调用）：
  · 能换回登录 token          → 凭据**有效**（api_id/hash 匹配、未被封）
  · API_ID_INVALID            → **无效**（api_id 错，或 hash 与 api_id 不匹配）
  · API_ID_PUBLISHED_FLOOD    → **公共/泄露** api_id（被限流，绝不能用）
  · FLOOD_WAIT                → 本 IP 探测太频繁，退避
这一步**不发短信、不建会话、不碰任何账号**，in_memory 会话跑完即弃，零残留。

关于「之前的关联」——诚实的技术边界：
  · Telegram 不提供「这个 api_id 被多少账号用过」的全局计数（服务端不外露）。
  · 唯一能查「关联」的官方接口是 `account.getAuthorizations`，它返回**某个已登录
    账号**当前的活跃会话（每条含创建它的 api_id / 应用名 / IP / 国家 / 登录时间）。
    也就是说，关联是**按账号**查的，不是按 api_id 查的，且必须先有那个账号的会话。
    表里 86 个号我们手上没有它们的会话，所以无法对它们查关联——除非先登录进来。
  · 我们**自己**的关联（池把某 api_id 分给过我们几个号）在池台账里，新导入的≈0。

用法：
    python deploy\instances\credpool_probe.py --only 25784476,30099940   # 先验几个
    python deploy\instances\credpool_probe.py --all --sleep 4            # 全验（限速）
    python deploy\instances\credpool_probe.py --all --apply              # 顺手停用无效的
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sqlite3
import sys
import time
from pathlib import Path

POOL_DB = Path(r"D:\chengjie-instances\.ops\credpool\matrixx.db")


def _load(only=None, include_disabled=False):
    conn = sqlite3.connect(f"file:{POOL_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT api_id, api_hash, name, status, current_accounts, "
            "success_count, fail_count FROM telegram_api_pool ORDER BY created_at")]
    finally:
        conn.close()
    if not include_disabled:
        rows = [r for r in rows if str(r.get("status") or "") != "disabled"]
    if only:
        want = {s.strip() for s in only.split(",") if s.strip()}
        rows = [r for r in rows if str(r["api_id"]) in want]
    return rows


def _classify(ex: Exception) -> tuple:
    s = f"{type(ex).__name__}: {ex}"
    up = s.upper()
    if "API_ID_PUBLISHED_FLOOD" in up:
        return "flood_public", "公共/泄露 api_id（被限流，不可用）"
    if "API_ID_INVALID" in up:
        return "invalid", "api_id 无效或与 hash 不匹配"
    if "FLOOD_WAIT" in up:
        return "flood_wait", s
    if isinstance(ex, asyncio.TimeoutError) or "TIMEOUT" in up:
        return "timeout", "连接/调用超时（网络，非凭据问题）"
    return "error", s


async def _probe_one(api_id: str, api_hash: str, timeout: float) -> tuple:
    from pyrogram import Client
    from pyrogram.raw.functions.auth import ExportLoginToken

    client = Client(f"probe_{api_id}", api_id=int(api_id), api_hash=api_hash,
                    in_memory=True)
    t0 = time.time()
    try:
        await asyncio.wait_for(client.connect(), timeout=timeout)
        r = await asyncio.wait_for(
            client.invoke(ExportLoginToken(
                api_id=int(api_id), api_hash=api_hash, except_ids=[])),
            timeout=timeout)
        cost = int((time.time() - t0) * 1000)
        # 任何非异常响应（LoginToken / LoginTokenMigrateTo / …）都代表 api_id 被接受
        return "valid", f"{type(r).__name__} {cost}ms"
    except Exception as ex:  # noqa: BLE001
        return _classify(ex)
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


def _apply_disable(api_ids: list) -> int:
    if not api_ids:
        return 0
    conn = sqlite3.connect(str(POOL_DB))
    try:
        conn.executemany(
            "UPDATE telegram_api_pool SET status='disabled' WHERE api_id=?",
            [(a,) for a in api_ids])
        conn.commit()
        return conn.total_changes
    finally:
        conn.close()


async def _main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="验池里所有可用凭据")
    g.add_argument("--only", default="", help="只验这些 api_id（逗号分隔）")
    ap.add_argument("--sleep", type=float, default=4.0, help="每次之间等待秒（限速防 IP flood）")
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--apply", action="store_true", help="把验出的无效/公共凭据在池里停用")
    ap.add_argument("--limit", type=int, default=0, help="最多验几个（0=不限）")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    if not POOL_DB.is_file():
        print(f"找不到池库：{POOL_DB}")
        return 2
    creds = _load(only=(args.only or None))
    if args.limit and len(creds) > args.limit:
        creds = creds[:args.limit]
    if not creds:
        print("没有要验的凭据")
        return 1

    print(f"[probe] 待验 {len(creds)} 组，限速 {args.sleep}s/个"
          f"（本 IP 直连 Telegram，无代理）\n")
    buckets: dict = {}
    bad: list = []
    for i, c in enumerate(creds, 1):
        aid, name = str(c["api_id"]), (c.get("name") or "")
        kind, detail = await _probe_one(aid, c["api_hash"], args.timeout)
        buckets.setdefault(kind, []).append(aid)
        tag = {"valid": "✅ 有效", "invalid": "❌ 无效",
               "flood_public": "⛔ 公共/泄露", "flood_wait": "⏳ 限流",
               "timeout": "… 超时", "error": "… 错误"}.get(kind, kind)
        print(f"  [{i}/{len(creds)}] api_id={aid:<10} {name[:14]:<14} {tag}  {detail}")
        if kind in ("invalid", "flood_public"):
            bad.append(aid)
        if kind == "flood_wait":
            print("  ⏳ 本 IP 触发 FLOOD_WAIT —— 停止本轮，过一阵再验剩下的"
                  "（继续打只会更糟）")
            break
        if i < len(creds):
            await asyncio.sleep(args.sleep + random.uniform(0, 1.5))

    print("\n== 汇总 ==")
    for k in ("valid", "invalid", "flood_public", "flood_wait", "timeout", "error"):
        if buckets.get(k):
            print(f"  {k:<12} {len(buckets[k])}  {', '.join(buckets[k][:12])}"
                  + (" …" if len(buckets[k]) > 12 else ""))

    if bad and args.apply:
        n = _apply_disable(bad)
        print(f"\n[probe] 已在池里停用 {n} 组无效/公共凭据（分配器不再选它们）")
    elif bad:
        print(f"\n[probe] {len(bad)} 组无效/公共凭据。加 --apply 可一键在池里停用它们。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
