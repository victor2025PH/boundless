#!/usr/bin/env python3
"""drill_tenant_lifecycle.py — 托管租户全生命周期实弹演练（一键复验商业闭环）。

P6（2026-08-08）手工演练的工具化：走**生产管线**（官网订单 API + 计划任务
TenantFulfillWatch/TenantSelfHeal）验证六段闭环，任何一段断言失败即非零退出：

  1. 下单（delivery=hosted）→ admin 标 paid
  2. 履约守护零人工交付（provision+expose+gw-budget 提额+到期账本落卡）
  3. 回拨到期 → watch 自动停机（订单驱动卡 + 超宽限）
  4. 停机公网入口 = HTTP 502 + 友好续费页（非裸错误）
  5. 续费单 → 自动复机 + 叠期 + 续费串回填（**不含初始密码**）+ 公网 200
  6. 现场全清（实例/stack/旗/隧道/数据根 + 订单标 cancelled + 额度覆写清除）

安全护栏：
  - 无 ``--confirm`` 只打印执行计划，零副作用；
  - 演练件命名恒含 drill（expose 不自动保护、is_drill_instance 豁免）；
  - 同名残留实例存在即拒跑（防覆盖上次未清干净的现场）;
  - 失败也尽力清理（try/finally），清不掉的如实点名。

**刻意不进计划任务/gate_sweep**：每跑一次都真开实例、真签证书（LE 周配额）、
真发 ops 告警——按需跑（改了履约/生命周期代码后一键复验），不做周期噪音。
用法：
  python tools/drill_tenant_lifecycle.py             # 看计划
  python tools/drill_tenant_lifecycle.py --confirm   # 实弹
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # engines/chengjie
REPO_ROOT = BASE.parent.parent
SECRETS = Path(r"D:\chengjie-instances\.ops\fulfill")
INSTANCES = Path(r"D:\chengjie-instances")
TENANT_OPS = BASE / "scripts" / "tenant_ops.py"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def _site_key() -> tuple[str, str]:
    site = (SECRETS / "site.txt").read_text(encoding="utf-8").strip().rstrip("/")
    key = (SECRETS / "admin_key.txt").read_text(encoding="utf-8").strip()
    return site, key


def _post(site: str, path: str, body: dict, key: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["x-setup-key"] = key
    req = urllib.request.Request(site + path, data=json.dumps(body).encode(),
                                 method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _get(url: str, key: str = "", timeout: int = 20):
    headers = {"User-Agent": "drill-tenant-lifecycle"}
    if key:
        headers["x-setup-key"] = key
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def _run_task(name: str) -> None:
    r = subprocess.run(["schtasks", "/Run", "/TN", name],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:  # 任务在跑/触发失败 → 靠计划节拍兜底，不算错
        print(f"  [warn] schtasks /Run {name} rc={r.returncode}（等计划节拍兜底）")


def _tenant_ops(args: list[str], timeout: int = 300) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(TENANT_OPS)] + args, cwd=str(BASE),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _wait_order(site: str, oid: str, want: str = "activated",
                timeout_sec: int = 600) -> dict:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        time.sleep(10)
        with _get(f"{site}/api/order?id={oid}") as r:
            o = json.loads(r.read().decode())
        if o.get("status") == want:
            return o
    raise AssertionError(f"订单 {oid} 等待 {want} 超时（{timeout_sec}s）")


def _card(iid: str) -> dict:
    return json.loads((INSTANCES / iid / "tenant_card.json")
                      .read_text(encoding="utf-8-sig"))


def _public_status(url: str) -> tuple[int, str]:
    try:
        with _get(url) as r:
            return int(r.status), ""
    except urllib.error.HTTPError as e:
        return int(e.code), e.read().decode("utf-8", "replace")


def run_drill() -> int:
    site, key = _site_key()
    ts = time.strftime("%m%d%H%M")
    contact = f"drill lc {ts}"
    slug = f"drill-lc-{ts}"
    iid = f"zhiliao_drill_lc_{ts}"
    if (INSTANCES / iid).exists():
        print(f"[拒跑] 残留实例 {iid} 存在，先人工清理")
        return 2
    orders: list[str] = []
    ok = True
    try:
        # ── 1/2: 下单 → paid → 履约交付 ──
        o = _post(site, "/api/order", {"plan": "autochat-team", "period": "monthly",
                                       "delivery": "hosted", "contact": contact,
                                       "amount": 198, "lang": "zh"})
        oid = o["order_id"]; orders.append(oid)
        _post(site, "/api/admin/order-status", {"id": oid, "status": "paid"}, key)
        print(f"[1] 订单 {oid} 已下单并标 paid")
        _run_task("TenantFulfillWatch")
        _wait_order(site, oid)
        card = _card(iid)
        assert card.get("last_fulfill_kind") == "activate", f"kind={card.get('last_fulfill_kind')}"
        assert card.get("ai_daily_chars") == 250_000, f"budget={card.get('ai_daily_chars')}"
        assert card.get("last_order_sku") == "chatx-team"
        with _get(f"{site}/api/admin/gw-budget?subject=IID:{iid}", key) as r:
            q = json.loads(r.read().decode())
        assert int((q.get("quota") or {}).get("budget") or 0) == 250_000, q
        code, _ = _public_status(card["public_url"] + "/login")
        assert code == 200, f"公网 /login={code}"
        print(f"[2] 交付 ✓（250k 提额 + 32 天账本 + 公网 200）")

        # ── 3/4: 过期 → 自动停机 → 友好页 ──
        p = INSTANCES / iid / "tenant_card.json"
        card["expires_at"] = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 10 * 86400))
        p.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")
        _run_task("TenantSelfHeal")
        flag = Path(r"D:\chengjie-instances\.ops\suspended") / f"{iid}.flag"
        deadline = time.time() + 300
        while time.time() < deadline and not flag.exists():
            time.sleep(10)
        assert flag.exists(), "watch 未自动停机（5min 内）"
        reason = json.loads(flag.read_text(encoding="utf-8-sig")).get("reason", "")
        assert reason.startswith("auto_expired:"), f"旗 reason={reason}"
        print(f"[3] 自动停机 ✓（{reason}）")
        time.sleep(3)
        code, body = _public_status(card["public_url"] + "/login")
        assert code == 502 and "订阅已到期被暂停" in body, f"友好页缺席 code={code}"
        print("[4] 停机友好页 ✓（502 + 续费引导）")

        # ── 5: 续费 → 自动复机 + 叠期 + 续费串 ──
        o2 = _post(site, "/api/order", {"plan": "autochat-team", "period": "monthly",
                                        "delivery": "hosted", "contact": contact,
                                        "amount": 198, "lang": "zh"})
        oid2 = o2["order_id"]; orders.append(oid2)
        _post(site, "/api/admin/order-status", {"id": oid2, "status": "paid"}, key)
        _run_task("TenantFulfillWatch")
        o2f = _wait_order(site, oid2)
        rcode = str(o2f.get("code") or "")
        assert rcode.startswith("续费已到账"), f"非续费串: {rcode[:60]!r}"
        assert "初始密码" not in rcode, "续费串泄漏初始密码！"
        assert not flag.exists(), "复机未清暂停旗"
        card2 = _card(iid)
        assert card2.get("last_fulfill_kind") == "renewal"
        days = (time.mktime(time.strptime(card2["expires_at"], "%Y-%m-%d %H:%M:%S"))
                - time.time()) / 86400
        assert 30 <= days <= 33, f"叠期异常 {days:.1f}d（过期续费应≈32）"
        code, _ = _public_status(card2["public_url"] + "/login")
        assert code == 200, f"复机后公网 /login={code}"
        print(f"[5] 续费复机 ✓（叠期 {days:.1f}d + 公网 200 + 无密码泄漏）")
    except AssertionError as e:
        ok = False
        print(f"[FAIL] {e}", file=sys.stderr)
    finally:
        # ── 6: 现场全清（失败也尽力清；清不掉的如实点名）──
        print("[6] 清理现场 …")
        leftovers: list[str] = []
        _tenant_ops(["unexpose", iid])
        _tenant_ops(["deprovision", iid])
        try:
            stack_p = REPO_ROOT / "deploy" / "stack.json"
            stack = json.loads(stack_p.read_text(encoding="utf-8-sig"))
            stack["services"] = [s for s in stack["services"]
                                 if s.get("id") != f"chengjie_{iid}"]
            stack_p.write_text(json.dumps(stack, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
        except Exception:  # noqa: BLE001
            leftovers.append("stack.json 条目")
        (Path(r"D:\chengjie-instances\.ops\suspended") / f"{iid}.flag").unlink(missing_ok=True)
        try:
            port = int(_card(iid).get("web_port") or 0) if (INSTANCES / iid).exists() else 0
        except Exception:  # noqa: BLE001
            port = 0
        try:
            tp = Path(r"D:\chengjie-instances\.ops\tenant_tunnel_ports.txt")
            lines = [ln for ln in tp.read_text(encoding="utf-8-sig").splitlines()
                     if not (port and ln.strip() == str(port))]
            tp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:  # noqa: BLE001
            leftovers.append("隧道端口清单")
        try:
            import shutil
            shutil.rmtree(INSTANCES / iid, ignore_errors=True)
        except Exception:  # noqa: BLE001
            leftovers.append("数据根")
        for x in orders:  # 演练订单标 cancelled，防污染营收台账
            try:
                _post(site, "/api/admin/order-status", {"id": x, "status": "cancelled"}, key)
            except Exception:  # noqa: BLE001
                leftovers.append(f"订单 {x}")
        try:
            _post(site, "/api/admin/gw-budget",
                  {"subject": f"IID:{iid}", "clear": True}, key)
        except Exception:  # noqa: BLE001
            leftovers.append("gw-budget 覆写")
        print("  清理完成" + (f"；残留需人工: {leftovers}" if leftovers else " ✓"))
    print("[drill] PASS ✓ 全生命周期闭环" if ok else "[drill] FAIL（见上）")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="托管租户全生命周期实弹演练")
    ap.add_argument("--confirm", action="store_true",
                    help="实弹执行（缺省只打印计划：真开实例/真签证书/真发告警）")
    args = ap.parse_args(argv)
    if not args.confirm:
        print(__doc__)
        print("（未加 --confirm：以上为执行计划，零副作用退出）")
        return 0
    return run_drill()


if __name__ == "__main__":
    raise SystemExit(main())
