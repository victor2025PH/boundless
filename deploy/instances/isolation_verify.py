# -*- coding: utf-8 -*-
r"""用**实例真配置 + 注册表真账号 + 在跑的真池**验证「连接那一刻」的隔离落地。

刻意不重启生产：本仓纪律是「别用重启生产当测试」，且这条路要验的是解析结果，
重启只是让它生效，验证本身不需要。

跑完会明确回答三件事：
  1. 这个号连接时用的 api_id 是**池给的**还是配置里的？
  2. 设备指纹有没有真的进到 pyrogram 参数里？
  3. 付费档的独立出口有没有接上？
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

CHENGJIE = Path(r"D:\boundless\engines\chengjie")
sys.path.insert(0, str(CHENGJIE))
INST = Path(r"D:\chengjie-instances\zhiliao\data\config")


def load_cfg() -> dict:
    import yaml

    def rd(p: Path) -> dict:
        try:
            return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            return {}

    def merge(a: dict, b: dict) -> dict:
        out = dict(a)
        for k, v in (b or {}).items():
            out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
        return out

    return merge(rd(INST / "config.yaml"), rd(INST / "config.local.yaml"))


def accounts() -> list:
    db = INST / "account_registry.db"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT account_id, platform, mode, status, proxy_id, label, meta_json "
            "FROM platform_accounts WHERE platform='telegram' AND mode='protocol'")]
    finally:
        conn.close()
    for r in rows:
        try:
            r["meta"] = json.loads(r.pop("meta_json") or "{}") or {}
        except Exception:  # noqa: BLE001
            r["meta"] = {}
    return rows


async def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    cfg = load_cfg()
    from src.integrations import credpool_bridge as cb
    from src.integrations.telegram_companion_worker import TelegramCompanionWorker

    tg = (cfg.get("telegram") or {})
    cfg_api_id = str(tg.get("api_id") or "")
    print(f"配置里的 api_id = {cfg_api_id}（若连接用的是这个，说明池没接上）")
    print(f"中央池启用 = {cb.credpool_enabled(cfg)}\n")

    fails = []
    for acc in accounts():
        aid = acc["account_id"]
        key = cb.pool_key_of(acc)
        tag = "池内号" if key else "存量号"
        print(f"── {aid}（{tag}，status={acc['status']}）")
        w = TelegramCompanionWorker(acc, cfg)
        try:
            ov = await w._isolation_overlay()
        except RuntimeError as exc:
            print(f"   拒绝上线：{exc}")
            fails.append(f"{aid} 拒绝上线")
            continue
        api_id = ov.get("api_id")
        if key:
            if api_id is None:
                fails.append(f"{aid} 没拿到凭据")
                print("   ✗ 没解析出凭据")
            else:
                same_as_cfg = str(api_id) == cfg_api_id
                print(f"   凭据 api_id={api_id}"
                      + ("（与配置同一组——本例池里只有这一组，正常）" if same_as_cfg
                         else "（来自池，与配置不同组）"))
        else:
            print("   凭据：不覆盖（存量号保持 TelegramClient 原读法）"
                  if api_id is None else f"   凭据 api_id={api_id}")
            if api_id is not None:
                fails.append(f"{aid} 存量号被覆盖了凭据")
        fp = {k: ov.get(k) for k in ("device_model", "system_version", "app_version")
              if ov.get(k)}
        stored = (acc["meta"].get("device_fp") or {})
        if key and stored:
            if not fp:
                fails.append(f"{aid} 落库有指纹但连接不带")
                print("   ✗ 指纹没进连接参数（登录报一套、重连报另一套＝每次换设备）")
            elif fp.get("device_model") != stored.get("device_model"):
                fails.append(f"{aid} 指纹与落库不一致")
                print(f"   ✗ 指纹漂移：连接 {fp.get('device_model')} ≠ 落库 {stored.get('device_model')}")
            else:
                print(f"   指纹 {fp['device_model']} / {fp['system_version']}"
                      f" / {fp['app_version']}（与扫码那刻落库的一致）")
        elif fp:
            print(f"   指纹 {fp.get('device_model')}")
        else:
            print("   指纹：无（存量号保持 pyrogram 默认，正确）")
        print(f"   独立出口：{ov['proxy']['hostname'] if ov.get('proxy') else '无'}"
              + ("（显式绑定 proxy_id，走 _resolve_proxy）" if acc.get("proxy_id") else ""))
        if key:
            cred = acc["meta"].get(cb.META_CRED_KEY) or {}
            print("   池挂时：" + (f"用缓存凭据 api_id={cred.get('api_id')} "
                                   f"tier={cred.get('tier')} 继续在线"
                                   if cred else "拒绝上线（无缓存，防错配）"))

    print()
    if fails:
        print(f"== 判定：{len(fails)} 项不符合预期 ==")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("== 判定：连接那一刻的凭据/指纹/出口全部符合预期 ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
