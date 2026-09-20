# -*- coding: utf-8 -*-
r"""中央凭据池「全链路自检」——一条命令跑完所有层，给一个总判定。

为什么要有它：这条链现在横跨四个层次，每层各有自己的检查脚本，散着跑很容易
漏掉一层，而漏掉的那层恰好就是出问题的那层（本项目已经吃过两次教训：
替身测试全绿但真 HTTP 不通；第一轮通过但第二轮撞唯一键）。

覆盖的四层：
  1. 契约层·断总线   —— platform/chain_selftest.py（7 条契约优雅降级）
  2. 契约层·真 HTTP  —— credpool_live_smoke.py（隔离起服，全链路真打）
  3. 服务端逻辑      —— 智控三个 selftest（服务 token / 会员档 / 日报）
  4. 在跑的真实环境  —— 池矩阵验收 + 生产观测面 + 池台账 + 实例开关

用法：
    python deploy\instances\credpool_chain_check.py --token <服务token>
    python deploy\instances\credpool_chain_check.py --token <t> --skip-slow   # 跳过 pytest 层
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(r"D:\boundless")
WS = Path(r"D:\workspace\boundless")
ROOT = REPO if (REPO / "platform").is_dir() else WS
CHENGJIE = ROOT / "engines" / "chengjie"
INSTANCE_CFG = Path(r"D:\chengjie-instances\zhiliao\data\config\config.yaml")
OVERLAY = INSTANCE_CFG.parent / "config.local.yaml"

_results: list = []


def step(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def _run(cmd, cwd=None, timeout=900) -> tuple:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "超时"
    except Exception as exc:  # noqa: BLE001
        return 125, str(exc)


def _instance_cfg() -> dict:
    import yaml

    def load(p: Path) -> dict:
        if not p.exists():
            return {}
        try:
            return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            return {}

    def merge(a: dict, b: dict) -> dict:
        out = dict(a)
        for k, v in (b or {}).items():
            out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
        return out

    return merge(load(INSTANCE_CFG), load(OVERLAY))


def _deep(d: dict, *keys):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def check_contract_degrade() -> None:
    code, out = _run([sys.executable, "chain_selftest.py"], cwd=str(ROOT / "platform"))
    ok = code == 0 and "全部优雅降级" in out
    n = out.count("[PASS]")
    step("契约层·断总线优雅降级", ok, f"{n} 条契约通过")


def check_contract_live() -> None:
    code, out = _run([sys.executable, "credpool_live_smoke.py"],
                     cwd=str(ROOT / "platform" / "credpool"))
    ok = code == 0 and "全链路真实 HTTP 打通" in out
    step("契约层·真 HTTP 全链路", ok,
         f"{out.count('PASS')} 项断言" if ok else out.strip().splitlines()[-1][:120])


def check_backend_selftests() -> None:
    backend = ROOT / "tgkz2026" / "backend"
    if not backend.is_dir():
        backend = WS / "tgkz2026" / "backend"
    total = passed = 0
    bad = []
    for name in ("selftest_service_token", "selftest_member_tier", "selftest_pool_daily_report"):
        code, out = _run([sys.executable, f"tests/{name}.py"], cwd=str(backend))
        line = next((l for l in out.splitlines() if "passed" in l), "")
        try:
            got, tot = line.strip().split(" ")[0].split("/")
            passed += int(got)
            total += int(tot)
        except Exception:  # noqa: BLE001
            pass
        if code != 0:
            bad.append(name)
    step("服务端逻辑·智控三项自检", not bad, f"{passed}/{total}"
         + (f"，失败：{','.join(bad)}" if bad else ""))


def check_pool_matrix(token: str, port: int) -> None:
    code, out = _run([sys.executable, str(REPO / "deploy" / "instances" / "credpool_stage.py"),
                      "verify", "--token", token, "--port", str(port)], timeout=300)
    ok = code == 0 and "矩阵全部符合预期" in out
    step("真实环境·会员档×独立出口矩阵", ok,
         f"{out.count('PASS')} 项" if ok else "见 verify 输出")


def check_pool_health(port: int) -> None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=6) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        step("真实环境·池服务在线", d.get("status") == "ok",
             f"persist={d.get('persist')}")
    except Exception as exc:  # noqa: BLE001
        step("真实环境·池服务在线", False, str(exc)[:100])


def check_instance_flags() -> None:
    cfg = _instance_cfg()
    want = {
        "protocol_enabled": _deep(cfg, "platform_login", "telegram", "protocol_enabled"),
        "credpool.enabled": _deep(cfg, "platform_login", "telegram", "credpool", "enabled"),
        "device_fingerprint": _deep(cfg, "platform_login", "telegram",
                                    "device_fingerprint", "enabled"),
    }
    missing = [k for k, v in want.items() if not v]
    step("真实环境·实例开关就位", not missing,
         "已开：" + "/".join(k for k, v in want.items() if v)
         + (f"；未开：{','.join(missing)}" if missing else ""))
    tok = (_deep(cfg, "platform_login", "telegram", "credpool", "service_token") or "")
    lic = (_deep(cfg, "platform_login", "telegram", "credpool", "license_key") or "")
    step("真实环境·池凭证与卡密已配", bool(tok) and bool(lic),
         f"token={'有' if tok else '无'} 卡密={'有' if lic else '无'}")


def check_production_metrics() -> None:
    """生产观测面：直接对**在跑实例的真实注册表**算一遍在册覆盖率。

    刻意不打 /api/workspace/metrics —— 那个口要主管会话（Bearer 会 403），
    而真正要验的是「重启后还能不能说真话」的数据通路，不是 HTTP 鉴权。
    路由把 shields 挂进 metrics 由 tests/test_isolation_shields.py 静态守。
    """
    import sqlite3

    db = Path(r"D:\chengjie-instances\zhiliao\data\config\account_registry.db")
    if not db.is_file():
        step("真实环境·在册覆盖率（重启也说真话）", False, f"找不到注册表 {db}")
        return
    sys.path.insert(0, str(CHENGJIE))
    try:
        from src.integrations.isolation_shields import build_shields  # noqa: E402
    except Exception as exc:  # noqa: BLE001
        step("真实环境·在册覆盖率（重启也说真话）", False, f"导入失败 {exc}")
        return
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT account_id, platform, mode, status, meta_json FROM platform_accounts")]
    finally:
        conn.close()
    sh = build_shields(rows)
    total = sh.get("total") or 0
    # 存量号（池上线前登录的）刻意不迁凭据——换 api_id 会与既有 session 错配。
    # 不点出来，读数的人会以为 2/8 是配置漏了，跑去「修」它反而制造封号风险。
    legacy = int(sh.get("legacy") or 0)
    step("真实环境·在册覆盖率（重启也说真话）", total > 0,
         f"协议号 {total} 个（池内 {sh.get('pooled')} / 存量 {legacy}）"
         f"，三件套齐 {sh.get('full_isolated')}；"
         f"凭据 {(sh.get('credential') or {}).get('covered')}/{total}、"
         f"出口 {(sh.get('egress') or {}).get('covered')}/{total}、"
         f"指纹 {(sh.get('fingerprint') or {}).get('covered')}/{total}"
         + (f"；最弱={sh.get('weakest')}" if sh.get("weakest") else "")
         + (f"；存量号需重登才升级" if legacy else ""))


def check_connect_time_isolation() -> None:
    """连接那一刻的隔离落地——最容易静默失效的一层。

    2026-07-27 就发生过：功能都写了，但本机走的 worker 那条路没接上，
    三隔离全是纸面的（登录报一套指纹、重连报另一套）。所以这一层必须常验。
    """
    code, out = _run([sys.executable, str(REPO / "deploy" / "instances" / "isolation_verify.py")],
                     timeout=180)
    ok = code == 0 and "全部符合预期" in out
    pooled = out.count("（池内号")
    step("真实环境·连接那一刻的凭据/指纹/出口", ok,
         f"协议号 {out.count('──')} 个（池内 {pooled}）"
         if ok else "见 isolation_verify 输出")


def check_pool_ledger(port: int) -> None:
    code, out = _run([sys.executable, str(REPO / "deploy" / "instances" / "credpool_stage.py"),
                      "ledger"], timeout=180)
    ok = code == 0 and "分配台账" in out
    active = out.count(" active")
    step("真实环境·池台账可读", ok, f"活跃分配 {active} 笔")


def check_chengjie_gates() -> None:
    code, out = _run([sys.executable, "-m", "pytest",
                      "tests/test_credpool_bridge.py", "tests/test_credpool_wiring.py",
                      "tests/test_credpool_stats.py", "tests/test_device_fingerprint.py",
                      "tests/test_isolation_shields.py",
                      "-q", "--tb=line", "--timeout=180"],
                     cwd=str(CHENGJIE), timeout=900)
    line = next((l for l in out.splitlines() if " passed" in l), "")
    step("引擎侧·credpool 门禁", code == 0, line.strip()[:80])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default="", help="池服务 token（缺省从实例 overlay 读）")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--skip-slow", action="store_true", help="跳过 pytest 层")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    token = args.token or (_deep(_instance_cfg(), "platform_login", "telegram",
                                 "credpool", "service_token") or "")

    print("== 中央凭据池 · 全链路自检 ==\n")
    print("-- 契约层 --")
    check_contract_degrade()
    check_contract_live()
    print("\n-- 服务端逻辑 --")
    check_backend_selftests()
    print("\n-- 真实环境 --")
    check_pool_health(args.port)
    check_instance_flags()
    if token:
        check_pool_matrix(token, args.port)
    else:
        step("真实环境·会员档×独立出口矩阵", False, "没有服务 token，跳不了这一步")
    check_production_metrics()
    check_connect_time_isolation()
    check_pool_ledger(args.port)
    if not args.skip_slow:
        print("\n-- 引擎侧门禁 --")
        check_chengjie_gates()

    bad = [n for n, ok, _ in _results if not ok]
    print()
    if bad:
        print(f"== 判定：{len(bad)}/{len(_results)} 项未通过 ==")
        for n in bad:
            print(f"  - {n}")
        return 1
    print(f"== 判定：全链路 {len(_results)}/{len(_results)} 项通过 ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
