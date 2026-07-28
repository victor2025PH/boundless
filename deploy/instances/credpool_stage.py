# -*- coding: utf-8 -*-
r"""中央凭据池「全链路开通」一键工具（智聊实例）。

为什么要有它：全链路验证（凭据走中央池 + 每账号设备指纹）需要三件事同时就位——
① 有一个在跑的智控池 ② 池里有真实可用的托管凭据 ③ 实例 overlay 开对开关。
手工做要开三个窗口、复制粘贴一个只显示一次的 token，容易在**重启窗口内**手忙脚乱。
本工具把它压成两条命令，让生产停机窗口只用来重启，不用来查文档。

子命令：
  serve   起一个**隔离的**智控凭据池（临时数据目录，绝不碰智控生产库），
          并把**本机实例真实的 api_id/api_hash** 作为托管凭据播种进去，
          再签发一枚 chatx 服务 token 打印出来。前台常驻，Ctrl+C 结束。
  enable  把 overlay 开关写进实例 config.local.yaml（走 ConfigManager.set_overlay_flag
          原子写入，保结构、自动备份）。
  disable 关掉开关（保留已写的 base_url/token，方便再开）。
  status  只读体检：开关状态 + 池可达性 + 已扫账号里带池键/指纹的比例。

安全：
  - 全程不打印 api_hash / auth_token / 服务 token 明文以外的任何密钥；
    服务 token 明文只在 serve 签发那一刻打印一次（这是它的设计）。
  - serve 用独立临时目录 + IS_PACKAGED/TG_DATA_DIR 顶掉智控的开发模式硬编码路径，
    起服前自证隔离，不满足就中止（连错库比不连更危险）。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(r"D:\boundless")
TGKZ_BACKEND = REPO / "tgkz2026" / "backend"
if not TGKZ_BACKEND.is_dir():  # 仓库根在 D:\workspace\boundless 时的等价路径
    TGKZ_BACKEND = Path(r"D:\workspace\boundless\tgkz2026\backend")
CHENGJIE = REPO / "engines" / "chengjie"
INSTANCE_CFG = Path(r"D:\chengjie-instances\zhiliao\data\config\config.yaml")
OVERLAY = INSTANCE_CFG.parent / "config.local.yaml"

FLAG_ENABLED = "platform_login.telegram.credpool.enabled"
FLAG_BASE = "platform_login.telegram.credpool.base_url"
FLAG_TOKEN = "platform_login.telegram.credpool.service_token"
FLAG_FP = "platform_login.telegram.device_fingerprint.enabled"
FLAG_LICENSE = "platform_login.telegram.credpool.license_key"


# ── 读实例真实凭据（只取值，不打印）──────────────────────────────────────────

def _instance_config() -> dict:
    import yaml

    def load(p: Path) -> dict:
        if not p.exists():
            return {}
        try:
            return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}

    def merge(a: dict, b: dict) -> dict:
        out = dict(a)
        for k, v in (b or {}).items():
            out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
        return out

    return merge(load(INSTANCE_CFG), load(OVERLAY))


def _real_credential() -> tuple:
    tg = (_instance_config().get("telegram") or {})
    api_id, api_hash = tg.get("api_id"), tg.get("api_hash")
    if not (api_id and api_hash):
        raise SystemExit("实例配置里没有 telegram.api_id/api_hash，无法播种托管凭据")
    return str(api_id), str(api_hash)


# ── 数据目录：常驻（转正）用固定目录，演练用临时目录 ────────────────────────

#: 常驻池的数据目录。放 .ops 下与 last_status/restart_cooldown/retired 同级——
#: 那里本来就是本机运维状态的归属地，重启机器不丢。
PERSIST_DIR = Path(r"D:\chengjie-instances\.ops\credpool")


def _bind_data_dir(data_dir: str) -> str:
    """把后端的数据路径**在 import 之前**钉死到指定目录，并自证隔离。

    光设 DATABASE_PATH 不够：智控 config.py 检测到 node_modules 就进「开发模式」，
    把数据目录硬编码成 backend/data（它自己的生产库）。必须 IS_PACKAGED=1 顶掉。
    """
    os.makedirs(os.path.join(data_dir, "sessions"), exist_ok=True)
    os.environ.update({
        "IS_PACKAGED": "1", "TG_DEV_MODE": "false", "TG_DATA_DIR": data_dir,
        "TG_SESSIONS_DIR": os.path.join(data_dir, "sessions"),
        "DATABASE_PATH": os.path.join(data_dir, "matrixx.db"),
    })
    sys.path.insert(0, str(TGKZ_BACKEND))
    from config import DATABASE_PATH as _db  # noqa: E402

    if Path(data_dir).resolve() not in Path(str(_db)).resolve().parents:
        raise SystemExit(f"数据目录隔离失败：config.DATABASE_PATH={_db} 不在 {data_dir} 内，中止")
    return str(_db)


def _resolve_data_dir(args) -> str:
    if getattr(args, "ephemeral", False):
        return tempfile.mkdtemp(prefix="credpool_stage_")
    return str(getattr(args, "data_dir", "") or PERSIST_DIR)


# ── serve：池服务（常驻 / 演练两用）─────────────────────────────────────────

def cmd_serve(args) -> int:
    data_dir = _resolve_data_dir(args)
    persist = not getattr(args, "ephemeral", False)
    db = _bind_data_dir(data_dir)
    print(f"[credpool] 数据目录：{data_dir}（{'常驻' if persist else '演练/临时'}）")
    print(f"[credpool] 隔离自证 OK：{db}")

    from aiohttp import web  # noqa: E402

    from admin.api_pool import get_api_pool_manager  # noqa: E402
    from api.admin_module_routes import register_admin_module_routes  # noqa: E402

    pool = get_api_pool_manager()

    if not persist:
        # 演练模式才自动播种本机凭据 + 签发一次性 token；
        # 常驻模式的凭据/令牌一律经 add-cred / token 子命令显式管理，
        # 免得每次重启服务都悄悄多出一组凭据或一枚令牌。
        from admin.service_token import issue_token  # noqa: E402

        api_id, api_hash = _real_credential()
        pool.add_api(api_id, api_hash, name="chatx-live-credential",
                     max_accounts=args.max_accounts, note="本机实例真实凭据（演练）")
        tok = issue_token("chatx", ["credpool:*"], note="全链路演练")
        print(f"[credpool] 已播种托管凭据 api_id={api_id}（api_hash 不打印）", flush=True)
        print(f"[credpool] 服务 token（**只显示这一次**）：\n\n    {tok['token']}\n", flush=True)
    else:
        stats = pool.get_pool_stats()
        print(f"[credpool] 现有凭据 {stats.get('total', 0)} 组"
              f"（可用 {stats.get('available', 0)}），已分配 {stats.get('total_allocations', 0)} 个账号位",
              flush=True)
        if not stats.get("total"):
            print("[credpool] ⚠️ 池是空的——先跑 add-cred 导入托管凭据，否则产品会一直回落自带凭据",
                  flush=True)

    app = web.Application()
    register_admin_module_routes(app)

    async def _health(_req):
        return web.json_response({"status": "ok", "server": "credpool",
                                  "persist": persist})

    app.router.add_get("/api/health", _health)

    import asyncio  # noqa: E402

    async def _report_tick():
        """日报班：容量预警必须有人送出去，否则算得再准也没人知道。

        日报本来注册在智控调度器里（`admin/scheduler.py`），但本机**只起了池这一
        小块**（这个 serve 就是个最小 aiohttp app），调度器根本没在跑 → 日报一次都
        不会发。2026-07-27 自检时才发现：池已经在报「剩 3 个位、约 1.5 天耗尽」，
        而这句话没有任何出口。补货靠人看日报，日报没出口＝补货链断在最后一米。

        挂在池服务上是最省的做法：它是唯一常驻在池边上的进程，池活着日报就活着。
        `send_pool_daily_report()` 自带「到点没到点 + 今天发过没有」双重幂等
        （落 system_config），所以这里可以放心地半小时敲一次——顺带把「机器当时
        没开机、错过整点」的情况补上；发送失败不写已发日期，下一轮自动重试。
        """
        while True:
            try:
                from admin.pool_daily_report import send_pool_daily_report  # noqa: E402

                res = await send_pool_daily_report()
                if res.get("sent"):
                    print(f"[credpool] 日报已推送：{res}", flush=True)
            except Exception as exc:  # noqa: BLE001 —— 日报绝不能把池带下线
                print(f"[credpool] 日报班异常（忽略，下轮重试）：{exc}", flush=True)
            await asyncio.sleep(1800)

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _run():
        asyncio.set_event_loop(loop)
        runner = web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", args.port).start())
        loop.create_task(_report_tick())
        ready.set()
        loop.run_forever()

    threading.Thread(target=_run, daemon=True).start()
    if not ready.wait(30):
        print("池服务 30 秒内没起来")
        return 2
    # 明说日报班挂上了：否则「今天怎么没收到日报」只能靠猜是没挂还是通道没配
    print(f"[credpool] 日报班已挂（每 30 分钟检查一次，目标 "
          f"{os.environ.get('CREDPOOL_REPORT_HOUR', '9')} 点后推送）", flush=True)

    print(f"\n[credpool] 中央凭据池已起：http://127.0.0.1:{args.port}", flush=True)
    print("[credpool] Ctrl+C 结束" + ("" if persist else "（临时库随之作废）"), flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\n[credpool] 已停止")
    return 0


# ── 常驻池的资源管理（凭据 / 代理 / 卡密 / 令牌）─────────────────────────────

def cmd_add_cred(args) -> int:
    """导入托管凭据。--from-instance 直接取本机实例那对真实凭据。"""
    _bind_data_dir(_resolve_data_dir(args))
    from admin.api_pool import get_api_pool_manager  # noqa: E402

    if args.from_instance:
        api_id, api_hash = _real_credential()
        name = args.name or "chatx-live-credential"
    else:
        if not (args.api_id and args.api_hash):
            print("需要 --api-id/--api-hash，或用 --from-instance 取本机实例凭据")
            return 2
        api_id, api_hash, name = args.api_id, args.api_hash, (args.name or f"API-{args.api_id}")

    pool = get_api_pool_manager()
    # add_api 返回 (ok, msg, obj) 三元组——早先这里错写成 `cred = add_api(...)` 再判
    # `cred is None`，元组永远不是 None，于是「已存在/失败」也照报成功。按真实签名解包。
    ok, msg, _ = pool.add_api(api_id, api_hash, name=name,
                              max_accounts=args.max_accounts, note=args.note or "")
    if not ok:
        print(f"添加失败（api_id={api_id}）：{msg}")
        return 1
    if args.min_level:
        ok2, msg2 = pool.update_api(api_id, min_member_level=args.min_level)
        print(f"  会员门槛 min_member_level={args.min_level}：{'OK' if ok2 else msg2}")
    print(f"已导入凭据 api_id={api_id} name={name} 容量={args.max_accounts}（api_hash 不打印）")
    return 0


# ── 批量导入：从 CSV 补充托管凭据（集团补货的常规路径）─────────────────────────

import re as _re  # noqa: E402

_API_ID_RE = _re.compile(r"^\d{5,10}$")
_HASH_RE = _re.compile(r"^[0-9a-f]{32}$")


def _norm_header(h: str) -> str:
    return _re.sub(r"[^a-z0-9]", "", str(h or "").lower())


def read_cred_csv(path: str) -> list:
    """从 CSV 读凭据行，按**表头名**认列（顺序无关，多出来的列忽略）。

    认列规则（归一化去空格/符号后包含即命中）：
      api_id  ← 含 apiid
      hash    ← 含 hash 或 apihash（但不含 id 的 hash 列）
      phone   ← 含 number 或 phone（来源手机号，落 source_phone 便于追溯）
      name    ← 含 name（别名）
    Google 表格「文件→下载→CSV」导出的表头（api_id / hash id / number /
    Telegram name）正好命中，集团补货只要重新导出再喂给本命令即可。
    """
    import csv  # noqa: E402

    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return []
    # 表头可能不在第 1 行（表格顶部常有标题/空行）：找到含 apiid 的那一行当表头
    header_idx = 0
    for i, r in enumerate(rows[:5]):
        if any("apiid" in _norm_header(c) for c in r):
            header_idx = i
            break
    header = [_norm_header(c) for c in rows[header_idx]]

    def col(*keys):
        for idx, h in enumerate(header):
            if any(k in h for k in keys):
                return idx
        return -1

    i_api = col("apiid")
    i_hash = next((idx for idx, h in enumerate(header)
                   if "hash" in h), -1)
    i_phone = col("number", "phone")
    i_name = col("name")
    out = []
    for r in rows[header_idx + 1:]:
        if i_api < 0 or i_api >= len(r):
            continue
        aid = (r[i_api] or "").strip()
        ah = (r[i_hash].strip() if 0 <= i_hash < len(r) else "")
        if not aid and not ah:
            continue
        out.append({
            "api_id": aid,
            "api_hash": ah,
            "phone": (r[i_phone].strip() if 0 <= i_phone < len(r) else ""),
            "name": (r[i_name].strip() if 0 <= i_name < len(r) else ""),
        })
    return out


def classify_cred_rows(rows: list) -> dict:
    """把原始行分成 可导入 / 非法 / 文件内重复 / 可疑（hash 撞车）四类。

    非法一律不导入（错 hash 的凭据登录必失败，进池只会污染健康统计）；
    hash 在不同 api_id 间重复 = 表里多半有一处填错，导入但标出来让人回查。
    """
    valid, invalid, dup = [], [], []
    seen_api = {}
    hash_owners = {}
    for r in rows:
        aid = r["api_id"].strip()
        ah = r["api_hash"].strip().lower()
        if not _API_ID_RE.match(aid):
            invalid.append((r, f"api_id 非法「{aid}」"))
            continue
        if not _HASH_RE.match(ah):
            invalid.append((r, f"hash 非 32 位 hex（{len(ah)} 位）"))
            continue
        if aid in seen_api:
            dup.append((r, f"api_id 在文件里重复（首见于 {seen_api[aid]}）"))
            continue
        seen_api[aid] = r.get("name") or r.get("phone") or aid
        r["_hash_lc"] = ah
        valid.append(r)
        hash_owners.setdefault(ah, []).append(aid)
    suspicious = {h: ids for h, ids in hash_owners.items() if len(ids) > 1}
    return {"valid": valid, "invalid": invalid, "dup": dup, "suspicious": suspicious}


def cmd_import_csv(args) -> int:
    """从 CSV 批量补充托管凭据——集团补货的常规路径。

    幂等：已在池里的 api_id 自动跳过（可反复重跑同一份导出）。
    先 `--dry-run` 看清「导入几条 / 拒几条 / 为什么拒」，再实导，避免把错 hash
    灌进池（错凭据登录必失败 → 被标 banned → 稀释可用容量）。
    """
    path = args.csv
    if not os.path.isfile(path):
        print(f"找不到文件：{path}")
        return 2
    rows = read_cred_csv(path)
    if not rows:
        print("没解析到任何行（表头里要有 api_id / hash 列）")
        return 2
    c = classify_cred_rows(rows)
    valid, invalid, dup, suspicious = c["valid"], c["invalid"], c["dup"], c["suspicious"]

    print(f"[credpool] 解析 {len(rows)} 行：可导入 {len(valid)}、"
          f"非法 {len(invalid)}、文件内重复 {len(dup)}")
    if invalid:
        print("\n-- 拒收（请在表里修正后重导）--")
        for r, why in invalid:
            print(f"  行「{r.get('name') or r.get('phone') or '?'}」 api_id={r['api_id']}：{why}")
    if dup:
        print("\n-- 文件内重复 api_id（只导第一条）--")
        for r, why in dup:
            print(f"  api_id={r['api_id']}：{why}")
    if suspicious:
        print("\n-- ⚠ 同一个 hash 挂在多个 api_id 上（多半填错一处，已导入待回查）--")
        for h, ids in suspicious.items():
            print(f"  hash …{h[-8:]} ← api_id {', '.join(ids)}")

    if args.dry_run:
        print(f"\n[credpool] --dry-run：不写库。以上 {len(valid)} 条可导入。")
        return 0
    if not valid:
        print("\n[credpool] 没有可导入的凭据。")
        return 1

    _bind_data_dir(_resolve_data_dir(args))
    from admin.api_pool import get_api_pool_manager  # noqa: E402

    pool = get_api_pool_manager()
    added = skipped = failed = 0
    for r in valid:
        note = args.note or ("sheet:" + "/".join(x for x in (r.get("name"), r.get("phone")) if x))
        ok, msg, _ = pool.add_api(
            r["api_id"], r["_hash_lc"], name=(r.get("name") or f"API-{r['api_id']}"),
            source_phone=(r.get("phone") or None), max_accounts=args.max_accounts,
            note=note[:200], min_member_level=(args.min_level or "free"))
        if ok:
            added += 1
        elif "已存在" in (msg or ""):
            skipped += 1
        else:
            failed += 1
            print(f"  失败 api_id={r['api_id']}：{msg}")
    print(f"\n[credpool] 导入完成：新增 {added}、已存在跳过 {skipped}、失败 {failed}"
          f"（每组 max_accounts={args.max_accounts}，api_hash 不打印）")
    stats = pool.get_pool_stats()
    cap = 0
    conn = pool._get_connection()
    try:
        row = conn.execute("SELECT COALESCE(SUM(max_accounts),0) c FROM telegram_api_pool "
                           "WHERE status='available'").fetchone()
        cap = int(row["c"] or 0)
    finally:
        conn.close()
    print(f"[credpool] 池现有凭据 {stats.get('total', 0)} 组（可用 {stats.get('available', 0)}），"
          f"总账号位 {cap}")
    return 0 if failed == 0 else 1


def cmd_add_proxy(args) -> int:
    """导入独立出口 IP（三隔离第三件套；付费档分配凭据时一并下发）。"""
    _bind_data_dir(_resolve_data_dir(args))
    from admin.proxy_pool import get_proxy_pool  # noqa: E402

    p = get_proxy_pool().add_proxy(
        args.scheme, args.host, args.port,
        username=args.username or None, password=args.password or None,
        country=args.country or None, provider=args.provider or None,
        note=args.note or "集团统一采买")
    print(f"已导入出口 {args.scheme}://{args.host}:{args.port}"
          f"（id={getattr(p, 'id', '?')}，账密不打印）")
    return 0


def cmd_add_license(args) -> int:
    """写入一张卡密（验证付费档链路用；正式发卡仍走智控 license_server）。"""
    _bind_data_dir(_resolve_data_dir(args))
    from admin.api_pool import get_api_pool_manager  # noqa: E402

    conn = get_api_pool_manager()._get_connection()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS licenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, license_key TEXT UNIQUE NOT NULL,
            level TEXT, status TEXT, machine_id TEXT, expires_at TEXT)""")
        conn.execute(
            "INSERT OR REPLACE INTO licenses (license_key, level, status, machine_id, expires_at)"
            " VALUES (?,?,?,?,?)",
            (args.key.upper(), args.level, "unused", "", args.expires_at or None))
        conn.commit()
    finally:
        conn.close()
    print(f"已写入卡密 {args.key.upper()} 档位={args.level}（未绑机；首次使用即绑）")
    return 0


def cmd_token(args) -> int:
    """签发 / 列出 / 吊销产品服务 token。"""
    _bind_data_dir(_resolve_data_dir(args))
    from admin.service_token import issue_token, list_tokens, revoke_token  # noqa: E402

    if args.action == "issue":
        r = issue_token(args.product, ["credpool:*"], note=args.note or "")
        if not r.get("success"):
            print(r.get("message"))
            return 1
        print(f"产品={r['product']} id={r['id']}")
        print(f"token（**只显示这一次**）：\n\n    {r['token']}\n")
    elif args.action == "list":
        for t in list_tokens():
            print(f"  {t['id']}  {t['product']:<10} enabled={t['enabled']} "
                  f"scopes={','.join(t['scopes'])} last_used={t['last_used_at'] or '-'}")
    elif args.action == "revoke":
        print(revoke_token(args.token_id).get("message"))
    return 0


# ── enable / disable：overlay 开关 ───────────────────────────────────────────

def _config_manager():
    sys.path.insert(0, str(CHENGJIE))
    from src.utils.config_manager import ConfigManager

    return ConfigManager(config_path=str(INSTANCE_CFG))


def _set(cm, path: str, value) -> None:
    ok, msg = cm.set_overlay_flag(path, value)
    shown = "***" if "token" in path else value
    print(f"  {'OK ' if ok else 'ERR'} {path} = {shown}  ({msg})")


def cmd_enable(args) -> int:
    if not args.token:
        print("需要 --token（token issue 签发的那一枚）")
        return 2
    backup = OVERLAY.with_suffix(f".yaml.bak-credpool-{os.getpid()}")
    if OVERLAY.exists():
        backup.write_bytes(OVERLAY.read_bytes())
        print(f"[stage] overlay 已备份：{backup.name}")
    cm = _config_manager()
    _set(cm, FLAG_BASE, f"http://127.0.0.1:{args.port}")
    _set(cm, FLAG_TOKEN, args.token)
    _set(cm, FLAG_ENABLED, True)
    if args.license_key:
        _set(cm, FLAG_LICENSE, args.license_key)
    if not args.no_fingerprint:
        _set(cm, FLAG_FP, True)
    print("\n[stage] 开关已写入。注意：**运行中的实例要重启一次才会加载新 provider 代码**；")
    print("        此后再改这些开关即可热生效（provider 已改读实时配置）。")
    print("        重启：powershell -File deploy\\instances\\restart_instance.ps1 -Instance zhiliao")
    return 0


def cmd_disable(args) -> int:
    cm = _config_manager()
    _set(cm, FLAG_ENABLED, False)
    _set(cm, FLAG_FP, False)
    print("\n[stage] 已关闭（base_url/token 保留，便于再开）。"
          "实例热重载后新扫的号即回落自带凭据；存量账号不受影响。")
    return 0


# ── status：只读体检 ─────────────────────────────────────────────────────────

def cmd_status(args) -> int:
    cfg = _instance_config()

    def g(*keys):
        cur = cfg
        for k in keys:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(k)
        return cur

    print("== 实例开关 ==")
    print(f"  protocol_enabled     : {g('platform_login','telegram','protocol_enabled')}")
    print(f"  orchestrator_enabled : {g('platform_login','orchestrator_enabled')}")
    print(f"  credpool.enabled     : {g('platform_login','telegram','credpool','enabled')}")
    print(f"  credpool.base_url    : {g('platform_login','telegram','credpool','base_url')}")
    tok = g('platform_login', 'telegram', 'credpool', 'service_token')
    print(f"  credpool.token       : {'已配置' if tok else '未配置'}")
    lic = g('platform_login', 'telegram', 'credpool', 'license_key')
    print(f"  credpool.license_key : {'已配置（付费档）' if lic else '未配置（按 free 档分配）'}")
    print(f"  device_fingerprint   : {g('platform_login','telegram','device_fingerprint','enabled')}")

    base = g('platform_login', 'telegram', 'credpool', 'base_url') or f"http://127.0.0.1:{args.port}"
    print(f"\n== 池可达性（{base}）==")
    try:
        import urllib.request

        with urllib.request.urlopen(f"{base}/api/health", timeout=5) as r:
            print(f"  OK  HTTP {r.status} {r.read(120).decode('utf-8', 'replace')}")
    except Exception as exc:
        print(f"  DOWN  {type(exc).__name__}: {exc}")

    print("\n== 账号侧痕迹 ==")
    sys.stdout.flush()  # 否则子进程输出会插到前面，读起来错乱
    os.system(f'python "{Path(__file__).with_name("scan_verify.py")}"')
    return 0


def cmd_alert_channel(args) -> int:
    """配置日报/告警的投递通道（Telegram bot → 你的号）。

    为什么单独一条命令：容量预警算得再准，送不到人手上就等于没有。通道存在池库
    `system_config.alert_config` 里，本机这份池库是新建的隔离库，天生没有通道
    （自检时 `channels: {}` 就是这么露出来的）。

    密钥不落代码不落日志：从 `--bot-token` 或环境变量取，写进池库后**不回显**。
    chat_id 缺省用本机既有告警配置里那个号（huoke 的 notifications.yaml 已在用），
    这样池日报和你现有的其他日报落在同一个会话里。
    """
    _bind_data_dir(_resolve_data_dir(args))
    token = (args.bot_token or os.environ.get(args.from_env or "BOT_TOKEN", "")).strip()
    if not token:
        print("没拿到 bot token：用 --bot-token <token>，或先设好环境变量再加 --from-env BOT_TOKEN")
        return 2
    if ":" not in token or len(token) < 20:
        print("这个 token 形状不像 Telegram bot token（应形如 123456789:AA...），先核对一下")
        return 2

    import asyncio  # noqa: E402

    from admin.alert_service import get_alert_service  # noqa: E402

    svc = get_alert_service()
    cfg = dict(svc.get_config() or {})
    cfg.update({"enabled": True, "telegram_bot_token": token,
                "telegram_chat_id": str(args.chat_id)})
    svc.configure(cfg)  # 落库（system_config.alert_config）
    st = svc.get_config()
    print(f"[credpool] 通道已写入池库：telegram → chat_id={args.chat_id}")
    print(f"[credpool] 自证：telegram_configured={bool(st.get('telegram_bot_token')) and bool(st.get('telegram_chat_id'))}"
          f"（token 不回显）")

    if args.test:
        from admin.pool_daily_report import send_pool_daily_report  # noqa: E402

        res = asyncio.run(send_pool_daily_report(force=True))
        ok = bool(res.get("sent"))
        print(f"[credpool] 试发{'成功——去手机上看一眼' if ok else '失败'}："
              f"{res.get('result') or res}")
        return 0 if ok else 1
    print("[credpool] 加 --test 可立刻试发一份验证通道")
    return 0


def cmd_report(args) -> int:
    """真发一份日报——验「容量预警能不能送到人手上」这最后一米。

    `ledger` 只是把报文打印出来（看内容对不对），送不送得出去是另一回事：
    通道要配 bot token + chat_id，配歪了只会静默失败。所以要有这条命令。
    """
    _bind_data_dir(_resolve_data_dir(args))
    import asyncio  # noqa: E402

    from admin.pool_daily_report import send_pool_daily_report  # noqa: E402

    res = asyncio.run(send_pool_daily_report(force=bool(args.force)))
    if res.get("sent"):
        print(f"[credpool] 日报已送出：{res}")
        return 0
    print(f"[credpool] 未送出：{res}")
    print("  · reason=already_sent_today / not_due → 正常（加 --force 可强发）")
    print("  · 其余 → 通道没配好：智控告警渠道要有 telegram bot token + chat_id")
    return 1


def cmd_ledger(args) -> int:
    """只读核对池台账（全链路验收用）：分配归属 / 凭据健康 / 小时统计 / 真实日报。"""
    data_dir = _resolve_data_dir(args)
    _bind_data_dir(data_dir)
    print(f"[credpool] 数据目录：{data_dir}\n")

    from admin.api_pool import get_api_pool_manager  # noqa: E402
    from admin.pool_daily_report import (by_product, format_report,  # noqa: E402
                                         pool_capacity, proxy_capacity, _collect)

    pool = get_api_pool_manager()
    conn = pool._get_connection()
    try:
        print("== 分配台账（谁拿了哪组凭据、属于哪个产品）==")
        # 看门狗探针（wd: 前缀）每 5 分钟一笔，行数会盖过真实分配 → 折叠成一行。
        # 折叠而不是删行：台账是审计面，少打印可以，少记录不行。
        probes = []
        for r in conn.execute(
            "SELECT account_phone, api_id, product, status FROM telegram_api_allocations "
            "ORDER BY allocated_at"
        ):
            d = dict(r)
            if str(d["account_phone"]).startswith("wd:"):
                probes.append(d)
                continue
            print(f"  {d['account_phone']:<26} api_id={d['api_id']:<10} "
                  f"product={d['product'] or '?':<8} {d['status']}")
        if probes:
            active = [p for p in probes if p["status"] == "active"]
            print(f"  （看门狗探针 {len(probes)} 笔已折叠：{len(active)} 活跃 / "
                  f"{len(probes) - len(active)} 已归还；活跃 >1 说明探针在漏容量）")
        print("\n== 凭据健康（回报是否真的喂进去了）==")
        for r in conn.execute(
            "SELECT api_id, name, current_accounts, max_accounts, status, "
            "success_count, fail_count FROM telegram_api_pool"
        ):
            d = dict(r)
            print(f"  api_id={d['api_id']} {d['name']} 占用 {d['current_accounts']}/"
                  f"{d['max_accounts']} status={d['status']} "
                  f"成功 {d['success_count']} 失败 {d['fail_count']}")
        print("\n== 小时统计 ==")
        rows = [dict(r) for r in conn.execute(
            "SELECT api_id, hour_key, allocations, successes, failures "
            "FROM telegram_api_hourly_stats")]
        # 这张表在「写事务未提交时另开连接」的自锁 bug 修好之前，永远是空的——
        # 它有数据本身就是那个修复生效的证据，别删这条注释。
        print("  " + (str(rows) if rows else "（无记录）"))
    finally:
        conn.close()

    stats, forecast = _collect()
    print("\n== 真实数据日报 ==")
    print(format_report(stats, forecast, by_product(),
                        capacity=pool_capacity(), proxies=proxy_capacity()))
    return 0


def cmd_verify(args) -> int:
    """对**在跑的池**验收「会员档 × 独立出口」矩阵，跑完自动清理。

    为什么不靠扫码验这一段：扫码一次只能验一种组合，而且付费档要是真下发了
    一个不通的出口 IP，登录会直接失败——把「功能对不对」和「IP 通不通」两个
    问题搅在一起。这里在契约层把四种组合一次跑完，再让扫码只验生产链路。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "cp_client", str(REPO / "platform" / "credpool" / "credpool_client.py"))
    if not Path(spec.origin).is_file():
        spec = importlib.util.spec_from_file_location(
            "cp_client", r"D:\workspace\boundless\platform\credpool\credpool_client.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cp = mod.CredPoolClient(base_url=f"http://127.0.0.1:{args.port}",
                            service_token=args.token)

    fails = []

    def ck(desc, ok, evidence=None):
        print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            fails.append(desc)
            if evidence is not None:
                print(f"        ↳ {evidence}")

    # 用**一次性卡密**而不是生产那张：生产卡密扫码时已绑到实例机器上，
    # 验收再拿它去申请就会被绑机正确降级成 free —— 那是功能对，脚本错。
    # 每轮自建自删，既不抢生产资源，也保证可重复跑。
    _bind_data_dir(_resolve_data_dir(args))
    import secrets as _secrets

    lic_key = args.license_key or f"VERIFY-{_secrets.token_hex(4).upper()}"
    disposable = not args.license_key
    if disposable:
        from admin.api_pool import get_api_pool_manager as _g  # noqa: E402

        _c = _g()._get_connection()
        try:
            _c.execute("""CREATE TABLE IF NOT EXISTS licenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT, license_key TEXT UNIQUE NOT NULL,
                level TEXT, status TEXT, machine_id TEXT, expires_at TEXT)""")
            _c.execute("INSERT OR REPLACE INTO licenses "
                       "(license_key, level, status, machine_id) VALUES (?,?,?,?)",
                       (lic_key, "gold", "unused", ""))
            _c.commit()
        finally:
            _c.close()
        print(f"  （本轮用一次性卡密 {lic_key}，验完即删）")

    from admin.api_pool import get_api_pool_manager  # noqa: E402

    # 基线：生产账号本来就正当地占着分配位，断言必须回到**基线**而不是回到 0
    baseline_allocs = int(get_api_pool_manager().get_pool_stats().get("total_allocations", 0))
    print(f"  （生产已占用 {baseline_allocs} 个账号位，验收后应回到这个数）")

    keys = []

    def alloc(key, expect_ok=True, **kw):
        """分配后**立即释放**——验收脚本自己不能把池占满。

        （第一版就是这么翻车的：5 个容量位被前几次验收吃光，后面的分配根本没
        拿到凭据，表现出来却是「付费档没下发出口」，差点误判成功能坏了。）
        """
        keys.append(key)
        r = cp.allocate(phone=key, machine_id=kw.pop("machine_id", "verify-machine"), **kw)
        d = r.get("data") or {}
        if expect_ok and not d.get("api_id"):
            ck(f"[{key}] 分配本身要成功（拿不到凭据后面的断言都无意义）", False, r)
        cp.release(key)  # 立刻还容量
        return r, d

    print("== 会员档位 ==")
    _, free = alloc("verify:free")
    ck("不带卡密 → free 档", free.get("member_level") == "free", free.get("member_level"))
    ck("free 档也能拿到凭据（不影响正常用）", bool(free.get("api_id")), free)

    _, gold = alloc("verify:gold", license_key=lic_key)
    ck(f"带卡密 {lic_key} → gold 档",
       gold.get("member_level") == "gold", gold.get("member_level"))

    _, forged = alloc("verify:forged", license_key="NOT-A-REAL-KEY")
    ck("假卡密 → 降 free（不可伪造提权）",
       forged.get("member_level") == "free", forged.get("member_level"))

    _, nomid = alloc("verify:nomid", license_key=lic_key, machine_id="")
    ck("不送 machine_id → 降 free（绑机不可绕过）",
       nomid.get("member_level") == "free", nomid.get("member_level"))

    _, othermid = alloc("verify:othermid", license_key=lic_key,
                        machine_id="another-machine")
    ck("卡密已绑机，换机器 → 降 free",
       othermid.get("member_level") == "free", othermid.get("member_level"))

    print("\n== 独立出口 IP（临时插一个测试出口，验完即删）==")
    _bind_data_dir(_resolve_data_dir(args))
    from admin.proxy_pool import get_proxy_pool  # noqa: E402

    pool_p = get_proxy_pool()
    marker = "credpool-verify-temp"
    probe = pool_p.add_proxy("socks5", "203.0.113.7", 1080,
                             username="u", password="p", note=marker)
    try:
        _, gold2 = alloc("verify:gold2", license_key=lic_key)
        ck("付费档分配成功（前置）", gold2.get("member_level") == "gold",
           gold2.get("member_level"))
        px = gold2.get("proxy") or {}
        ck("付费档随凭据下发独立出口", px.get("host") == "203.0.113.7", gold2.get("proxy"))
        ck("出口形状可直接喂给客户端",
           px.get("port") == 1080 and px.get("scheme"), px)
        _, free2 = alloc("verify:free2")
        ck("免费档不下发出口（这正是专业版权益）",
           bool(free2.get("api_id")) and not free2.get("proxy"), free2.get("proxy"))
    finally:
        conn = get_proxy_pool()._get_connection()
        try:
            conn.execute("DELETE FROM static_proxies WHERE note = ?", (marker,))
            conn.commit()
        finally:
            conn.close()
        print(f"  （测试出口已删除 id={getattr(probe, 'id', '?')}——"
              f"不能让生产扫码路由到一个不通的 IP）")

    print("\n== 池容量已还清 ==")
    for k in keys:
        cp.release(k)  # 幂等兜底
    from admin.api_pool import get_api_pool_manager  # noqa: E402

    st = get_api_pool_manager().get_pool_stats()
    if disposable:
        _c2 = get_api_pool_manager()._get_connection()
        try:
            _c2.execute("DELETE FROM licenses WHERE license_key = ?", (lic_key,))
            _c2.commit()
        finally:
            _c2.close()
        print(f"  （一次性卡密 {lic_key} 已删除）")
    now_allocs = int(st.get("total_allocations", -1))
    ck(f"验收分配已全部释放（回到基线 {baseline_allocs}）",
       now_allocs == baseline_allocs, f"现在 {now_allocs}，基线 {baseline_allocs}")

    print()
    if fails:
        print(f"== 结果：{len(fails)} 项未通过 ==")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("== 结果：会员档位 × 独立出口 矩阵全部符合预期 ==")
    return 0


def cmd_unbind_license(args) -> int:
    """解除卡密绑机（客户换机的正常诉求；与 admin 的 unbind 端点同语义）。

    绑机是为了收口「一张卡密贴几台机器」，但换电脑、重装、或者像现在这样
    「验收时把卡密绑到了测试机器」，都需要一个正规的放行口，而不是去手改库。
    """
    _bind_data_dir(_resolve_data_dir(args))
    from admin.api_pool import get_api_pool_manager  # noqa: E402

    key = args.key.upper()
    conn = get_api_pool_manager()._get_connection()
    try:
        row = conn.execute(
            "SELECT machine_id FROM licenses WHERE license_key = ?", (key,)).fetchone()
        if not row:
            print(f"卡密不存在：{key}")
            return 1
        old = row["machine_id"] if "machine_id" in row.keys() else None
        conn.execute("UPDATE licenses SET machine_id = '' WHERE license_key = ?", (key,))
        conn.commit()
    finally:
        conn.close()
    print(f"已解除绑机：{key}（原绑定 {old or '(无)'}）；下次使用会重新绑定")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="中央凭据池运维工具（智聊）")

    def _data_args(p):
        p.add_argument("--data-dir", default="", help=f"池数据目录（默认 {PERSIST_DIR}）")
        p.add_argument("--ephemeral", action="store_true", help="用临时目录（演练态，重启即弃）")

    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="起中央凭据池（默认常驻目录）")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--max-accounts", type=int, default=5)
    _data_args(s)
    s.set_defaults(func=cmd_serve)

    ac = sub.add_parser("add-cred", help="导入托管凭据")
    ac.add_argument("--api-id", default="")
    ac.add_argument("--api-hash", default="")
    ac.add_argument("--from-instance", action="store_true", help="取本机实例那对真实凭据")
    ac.add_argument("--name", default="")
    ac.add_argument("--max-accounts", type=int, default=5)
    ac.add_argument("--min-level", default="", help="会员门槛（free/silver/gold/diamond…）")
    ac.add_argument("--note", default="")
    _data_args(ac)
    ac.set_defaults(func=cmd_add_cred)

    ic = sub.add_parser("import-csv", help="从 CSV 批量补充托管凭据（集团补货）")
    ic.add_argument("csv", help="CSV 路径（表头含 api_id / hash 列；Google 表格导出即可）")
    ic.add_argument("--dry-run", action="store_true", help="只解析校验、不写库")
    ic.add_argument("--max-accounts", type=int, default=5)
    ic.add_argument("--min-level", default="", help="会员门槛（free/gold…；缺省 free）")
    ic.add_argument("--note", default="", help="备注（缺省用 sheet:名字/手机号）")
    _data_args(ic)
    ic.set_defaults(func=cmd_import_csv)

    ap2 = sub.add_parser("add-proxy", help="导入独立出口 IP")
    ap2.add_argument("--scheme", default="socks5")
    ap2.add_argument("--host", required=True)
    ap2.add_argument("--port", dest="port", type=int, required=True)
    ap2.add_argument("--username", default="")
    ap2.add_argument("--password", default="")
    ap2.add_argument("--country", default="")
    ap2.add_argument("--provider", default="")
    ap2.add_argument("--note", default="")
    _data_args(ap2)
    ap2.set_defaults(func=cmd_add_proxy)

    al = sub.add_parser("add-license", help="写入卡密（验证付费档链路）")
    al.add_argument("--key", required=True)
    al.add_argument("--level", default="gold")
    al.add_argument("--expires-at", default="")
    _data_args(al)
    al.set_defaults(func=cmd_add_license)

    ul = sub.add_parser("unbind-license", help="解除卡密绑机（换机/验收后放行）")
    ul.add_argument("--key", required=True)
    _data_args(ul)
    ul.set_defaults(func=cmd_unbind_license)

    tk = sub.add_parser("token", help="产品服务 token 管理")
    tk.add_argument("action", choices=["issue", "list", "revoke"])
    tk.add_argument("--product", default="chatx")
    tk.add_argument("--note", default="")
    tk.add_argument("--token-id", default="")
    _data_args(tk)
    tk.set_defaults(func=cmd_token)

    e = sub.add_parser("enable", help="写 overlay 开关")
    e.add_argument("--token", required=False, default="")
    e.add_argument("--port", type=int, default=8000)
    e.add_argument("--license-key", default="", help="客户卡密（付费档隔离）")
    e.add_argument("--no-fingerprint", action="store_true")
    e.set_defaults(func=cmd_enable)

    d = sub.add_parser("disable", help="关掉开关")
    d.set_defaults(func=cmd_disable)

    st = sub.add_parser("status", help="只读体检")
    st.add_argument("--port", type=int, default=8000)
    st.set_defaults(func=cmd_status)

    lg = sub.add_parser("ledger", help="只读核对池台账（验收用）")
    _data_args(lg)
    lg.set_defaults(func=cmd_ledger)

    ac = sub.add_parser("alert-channel", help="配置日报投递通道（Telegram bot → 你的号）")
    ac.add_argument("--bot-token", default="", help="Telegram bot token（不回显、不入日志）")
    ac.add_argument("--from-env", default="BOT_TOKEN", help="改从这个环境变量取 token")
    ac.add_argument("--chat-id", default="5433982810",
                    help="投递目标（缺省＝本机既有告警配置在用的那个号）")
    ac.add_argument("--test", action="store_true", help="配完立刻试发一份")
    _data_args(ac)
    ac.set_defaults(func=cmd_alert_channel)

    rp = sub.add_parser("report", help="立刻推一份池日报（验通道用；serve 每天自动发）")
    rp.add_argument("--force", action="store_true",
                    help="忽略「到点/今天已发过」，立即补发一份")
    _data_args(rp)
    rp.set_defaults(func=cmd_report)

    vf = sub.add_parser("verify", help="验收会员档×独立出口矩阵（对在跑的池，跑完自动清理）")
    vf.add_argument("--token", required=True)
    vf.add_argument("--port", type=int, default=8000)
    vf.add_argument("--license-key", default="",
                    help="指定卡密（缺省自建一次性卡密，避免抢生产那张）")
    _data_args(vf)
    vf.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    raise SystemExit(main())
