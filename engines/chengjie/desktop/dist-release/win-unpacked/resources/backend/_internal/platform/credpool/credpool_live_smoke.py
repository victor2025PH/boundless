# -*- coding: utf-8 -*-
r"""platform/credpool/credpool_live_smoke.py — 中央凭据池『真实接线』端到端烟雾测试。

与 `chain_selftest.py` / 各单测的区别（互补，不是重复）：
  - `chain_selftest.py` 证明「断总线，客户端优雅降级」；
  - chengjie/tgkz 的单测用**替身**证明各自逻辑正确；
  - 本脚本证明「**接上真服务端，整条链真的通**」——起一个只挂智控 admin 路由的
    真 aiohttp 服务（临时库、随机端口、不碰 Telegram/生产数据），用 platform 下的
    **真实瘦客户端**发起**真实 HTTP**，把服务 token 鉴权、会员定档、卡密绑机、
    独立出口下发、粘定复用、产品归属、释放回收全部走一遍并核对落库。

前三期全是单测与替身，这条链从未真打过一次 HTTP——本脚本就是补这个洞。

安全边界：
  - `DATABASE_PATH` 在导入任何后端模块**之前**指向临时目录，绝不碰生产库；
  - 服务只监听 127.0.0.1 随机端口，用完即关；
  - 不做真实 Telegram 扫码（那需要真手机号）——扫码之后的链路由 chengjie 单测覆盖。

用法：
    python platform/credpool/credpool_live_smoke.py
    python platform/credpool/credpool_live_smoke.py --tgkz-dir <path/to/tgkz2026>
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent

_failures = []


def check(desc: str, ok: bool, evidence=None) -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
    if not ok:
        _failures.append(desc)
        if evidence is not None:
            print(f"        ↳ 实际返回: {evidence}")


def _load_client():
    """按文件路径加载瘦客户端（不 sys.path.insert 仓库根，避免遮蔽标准库 platform）。"""
    spec = importlib.util.spec_from_file_location(
        "boundless_smoke_credpool", str(_HERE / "credpool_client.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_aiohttp_in_thread(app, port: int):
    import asyncio

    from aiohttp import web

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _run():
        asyncio.set_event_loop(loop)
        runner = web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", port).start())
        ready.set()
        loop.run_forever()

    threading.Thread(target=_run, daemon=True).start()
    if not ready.wait(30):
        raise RuntimeError("服务未能在 30s 内就绪")
    return loop


def _seed(tmp_dir: str):
    """用后端自己的模块播种：服务 token / 凭据 / 卡密 / 代理。"""
    from admin.api_pool import get_api_pool_manager
    from admin.service_token import issue_token

    tok = issue_token("chatx", ["credpool:*"], note="live smoke")
    pool = get_api_pool_manager()
    # 一组通用凭据 + 一组仅 gold 以上可用的专属凭据
    pool.add_api("1001", "hash_free_pool", name="free-pool", max_accounts=5)
    pool.add_api("2002", "hash_gold_only", name="gold-only", max_accounts=5)
    pool.update_api("2002", min_member_level="gold")

    # 卡密（gold，未绑机）
    conn = pool._get_connection()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS licenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, license_key TEXT UNIQUE NOT NULL,
            level TEXT, status TEXT, machine_id TEXT, expires_at TEXT)""")
        conn.execute("INSERT INTO licenses (license_key, level, status) VALUES (?,?,?)",
                     ("KEY-GOLD-LIVE", "gold", "unused"))
        conn.commit()
    finally:
        conn.close()

    proxy_ok = False
    try:
        from admin.proxy_pool import get_proxy_pool

        get_proxy_pool().add_proxy("socks5", "10.9.9.9", 1080,
                                   username="pu", password="pp", note="live smoke")
        proxy_ok = True
    except Exception as exc:  # noqa: BLE001
        print(f"  (代理池播种跳过：{exc})")
    return tok["token"], proxy_ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tgkz-dir", default=str(_REPO_ROOT / "tgkz2026"))
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    backend = str(Path(args.tgkz_dir) / "backend")
    if not Path(backend).is_dir():
        print(f"找不到智控 backend 目录：{backend}")
        return 2

    # ⚠️ 必须在 import 任何后端模块之前设置，否则会连上生产库。
    # 光设 DATABASE_PATH 不够：config.py 检测到 node_modules 就进「开发模式」，
    # 把数据目录**硬编码**成 backend/data（生产库），而 schema_adapter 又优先用
    # config.DATABASE_PATH。故必须同时 IS_PACKAGED=1（跳过 dev 检测）+ TG_DATA_DIR。
    tmp_dir = tempfile.mkdtemp(prefix="credpool_smoke_")
    os.environ["IS_PACKAGED"] = "1"
    os.environ["TG_DEV_MODE"] = "false"
    os.environ["TG_DATA_DIR"] = tmp_dir
    os.environ["TG_SESSIONS_DIR"] = os.path.join(tmp_dir, "sessions")
    os.environ["DATABASE_PATH"] = os.path.join(tmp_dir, "matrixx.db")
    os.makedirs(os.environ["TG_SESSIONS_DIR"], exist_ok=True)
    sys.path.insert(0, backend)

    print("== 中央凭据池真实接线烟雾测试（credpool_live_smoke.py）==")
    print(f"智控 backend: {backend}")
    print(f"临时数据目录（不碰生产）: {tmp_dir}")

    # 起服前先自证隔离成功——连错库的烟雾测试比没有更危险
    from config import DATABASE_PATH as _CFG_DB

    if Path(tmp_dir) not in Path(str(_CFG_DB)).parents:
        print(f"隔离失败：config.DATABASE_PATH={_CFG_DB} 不在临时目录内，中止")
        return 2
    print(f"隔离自证 OK：config.DATABASE_PATH={_CFG_DB}")

    from aiohttp import web

    from api.admin_module_routes import register_admin_module_routes

    app = web.Application()
    register_admin_module_routes(app)

    # 真实服务端的 /api/health 在 http_server.py 上（不在 admin 路由里）。
    # 这里补一个同路径探针，否则本演练里 client.available() 会因 404 而"看起来通"
    # ——鉴权与探活都必须打到真东西，不能靠碰巧的状态码蒙混。
    async def _health(_req):
        return web.json_response({"status": "ok", "server": "credpool-live-smoke"})

    app.router.add_get("/api/health", _health)

    token, proxy_ok = _seed(tmp_dir)
    port = args.port or _free_port()
    loop = _run_aiohttp_in_thread(app, port)
    base = f"http://127.0.0.1:{port}"
    print(f"智控 admin 路由已起于 {base}（真 aiohttp，仅 admin 模块路由）\n")

    mod = _load_client()
    try:
        # ── 传输层 ─────────────────────────────────────────────────────
        anon = mod.CredPoolClient(base_url=base, service_token="")
        r = anon.health()
        check("health 可达且返回真 JSON（不是碰巧的 404）",
              r.get("available") is True and r.get("status") == "ok", r)

        r = anon.allocate(phone="chatx:anon", machine_id="m-anon")
        check("无服务 token 被拒（鉴权真的在生效）",
              r.get("available") is True and r.get("success") is not True)

        cp = mod.CredPoolClient(base_url=base, service_token=token)
        check("已配置服务 token", cp.configured() is True)

        # ── 免费档：拿得到通用凭据，但拿不到 gold 专属 ───────────────────
        r = cp.allocate(phone="chatx:free1", account_id="acc-free", machine_id="m-free")
        d = r.get("data") or {}
        check("免费档分配成功", r.get("success") is True)
        check("免费档拿到通用凭据（非 gold 专属）", d.get("api_id") == "1001")
        check("免费档回传 member_level=free", d.get("member_level") == "free")
        check("免费档不下发独立出口", d.get("proxy") in (None, {}))

        # ── 粘定：同一 key 反复调回同一组 ────────────────────────────────
        again = (cp.allocate(phone="chatx:free1", machine_id="m-free").get("data") or {})
        check("粘定：同一 key 拿回同一组凭据", again.get("api_id") == d.get("api_id"))

        # ── 付费档：卡密定档 + 首次绑机 + 专属凭据 + 独立出口 ─────────────
        r = cp.allocate(phone="chatx:gold1", account_id="acc-gold",
                        license_key="KEY-GOLD-LIVE", machine_id="m-gold")
        g = r.get("data") or {}
        check("付费档分配成功", r.get("success") is True)
        check("卡密定档为 gold（服务端查库，非客户端自报）",
              g.get("member_level") == "gold", r)
        if proxy_ok:
            px = g.get("proxy") or {}
            check("付费档随凭据下发独立出口", px.get("host") == "10.9.9.9", g)
            check("出口形状可直接喂给客户端", px.get("port") == 1080 and px.get("scheme"), px)
        else:
            print("  SKIP  付费档独立出口（代理池播种未成功）")

        # 自报档位无效
        r = cp.allocate(phone="chatx:fake1", machine_id="m-fake")
        fake = r.get("data") or {}
        check("产品自报档位无效（未送卡密仍是 free）",
              fake.get("member_level") == "free")

        # 不送 machine_id → 不可绕过绑机
        r = cp.allocate(phone="chatx:nomid", license_key="KEY-GOLD-LIVE")
        check("不送 machine_id → 降 free（绑机不可绕过）",
              (r.get("data") or {}).get("member_level") == "free")

        # 换一台机器 → 降 free
        r = cp.allocate(phone="chatx:gold2", license_key="KEY-GOLD-LIVE",
                        machine_id="m-other")
        check("卡密换机 → 降 free", (r.get("data") or {}).get("member_level") == "free")

        # ── 查询 / 回报 / 释放 ───────────────────────────────────────────
        r = cp.account("chatx:gold1")
        check("account 查得到绑定关系",
              r.get("success") is True and (r.get("data") or {}).get("api_id"))

        r = cp.report(api_id=str(g.get("api_id")), success=True, phone="chatx:gold1")
        check("report(成功) 被服务端接受", r.get("available") is True and r.get("success"), r)

        # 失败回报走的是另一条分支（历史上 sqlite3.Row.get 在这条路上必崩）
        r = cp.report(api_id=str(g.get("api_id")), success=False,
                      error="API_ID_PUBLISHED_FLOOD", phone="chatx:gold1")
        check("report(失败) 也被接受（失败分支同样不能崩）",
              r.get("available") is True and r.get("success"), r)

        r = cp.release("chatx:free1")
        check("release 成功", r.get("success") is True, r)
        again = cp.release("chatx:free1")
        err = again.get("error")
        check("重复 release 返回结构化 JSON 错误（不是裸 500 HTML）",
              again.get("available") is True and again.get("success") is False
              and isinstance(err, dict) and err.get("code"), again)
        after = cp.account("chatx:free1")
        check("释放后绑定已解除", (after.get("data") or None) is None)

        # ── 换机解绑（客户换电脑的正常诉求，不能只靠人工改库）────────────
        import urllib.request as _u

        req = _u.Request(f"{base}/api/admin/licenses/KEY-GOLD-LIVE/unbind",
                         data=b"{}", method="POST",
                         headers={"Content-Type": "application/json"})
        try:
            with _u.urlopen(req, timeout=10) as resp:
                unbind_status = resp.status
        except Exception as exc:  # 无管理员 JWT → 应被拒（而不是放行）
            unbind_status = getattr(exc, "code", 0)
        check("解绑接口需要管理员权限（服务 token 不够）", unbind_status != 200, unbind_status)

        # ── 落库核对（不是只看响应）──────────────────────────────────────
        from admin.api_pool import get_api_pool_manager

        conn = get_api_pool_manager()._get_connection()
        try:
            prods = dict(conn.execute(
                "SELECT COALESCE(NULLIF(product,''),'?'), COUNT(*) "
                "FROM telegram_api_allocations WHERE status='active' GROUP BY 1"
            ).fetchall())
            check("分配记录带产品归属 chatx", prods.get("chatx", 0) >= 1)
            bound = conn.execute(
                "SELECT machine_id FROM licenses WHERE license_key='KEY-GOLD-LIVE'"
            ).fetchone()[0]
            check("卡密首次使用已绑机并落库", bound == "m-gold", bound)
        finally:
            conn.close()

        # ── 日报：用真实池数据生成 ───────────────────────────────────────
        from admin.pool_daily_report import (by_product, format_report, proxy_capacity,
                                             _collect)

        stats, forecast = _collect()
        text = format_report(stats, forecast, by_product(), proxies=proxy_capacity())
        check("日报能用真实池数据生成", "凭据池日报" in text and "凭据组数" in text)
        check("日报含按产品分帐", "按产品" in text and "chatx" in text)
        check("日报含代理水位（分得清『没买』还是『分完了』）", "独立出口" in text, text)
        print("\n---- 真实日报预览 ----\n" + text + "\n----------------------\n")
    finally:
        loop.call_soon_threadsafe(loop.stop)

    print()
    if _failures:
        print(f"== 结果：{len(_failures)} 项未通过 ==")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("== 结果：全链路真实 HTTP 打通（鉴权/定档/绑机/出口/粘定/归属/释放/日报）==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
