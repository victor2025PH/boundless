# -*- coding: utf-8 -*-
"""P11 前端交互日巡：fe_smoke(零 pageerror) + fe_interact(自救卡/试音/试听/开关真点击)。

设计对齐 selfcheck_pipeline / stability 哨兵：
  - Hub 不在线 → skip（不算 FAIL，避免 Hub 停机时日巡误报风暴）
  - playwright 未装 → skip（与 acceptance 同口径，不阻断交付）
  - 任一子项 FAIL + --alert → alerts.raise_alert(fe_patrol:...)
  - 全绿 → clear_alert（恢复通知）

产物：
  logs/fe_patrol_last.log      末次完整输出（计划任务覆盖写）
  logs/fe_patrol_history.jsonl 追加历史（/ops 读趋势）

末行：== 前端日巡 smoke N/M · interact N/M · 总判 PASS|FAIL|SKIP ==
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
HUB = os.environ.get("ACCEPT_HUB", "http://127.0.0.1:9000")
HIST = ROOT / "logs" / "fe_patrol_history.jsonl"
LAST = ROOT / "logs" / "fe_patrol_last.log"
HIST_MAX = 120

_UNPROVISIONED = (
    "No module named 'playwright'",
    "playwright install",
    "Executable doesn't exist",
    "looks like Playwright was just installed",
)


def _verdict(out: str):
    sums = re.findall(r"==[^\n]*==", out)
    scope = sums[-1] if sums else out[-600:]
    m = re.search(r"(\d+)\s*/\s*(\d+)", scope)
    if m:
        return int(m.group(1)) == int(m.group(2)), scope.strip()
    if "FAIL" in scope or "PARTIAL" in scope:
        return False, scope.strip()
    if "PASS" in scope or "全部通过" in scope or "全部干净" in scope:
        return True, scope.strip()
    return False, scope.strip() or "<无摘要>"


def _hub_up(timeout=4.0) -> bool:
    try:
        req = urllib.request.Request(HUB + "/realtime/status", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _run_script(rel: str, timeout: int) -> dict:
    path = ROOT / rel
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["ACCEPT_HUB"] = HUB
    t0 = time.time()
    try:
        p = subprocess.run(
            [sys.executable, "-X", "utf8", str(path)],
            cwd=str(ROOT), env=env, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = p.stdout.decode("utf-8", errors="replace")
        ok, scope = _verdict(out)
        unprov = any(m in out for m in _UNPROVISIONED)
        return {"ok": ok, "rc": p.returncode, "scope": scope, "out": out,
                "sec": round(time.time() - t0, 1), "unprovisioned": unprov, "script": rel}
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", errors="replace")
        return {"ok": False, "rc": -1, "scope": "超时 %ds" % timeout, "out": out,
                "sec": timeout, "unprovisioned": False, "script": rel}


def _sync_alerts(res: dict) -> dict:
    try:
        import alerts
    except Exception as e:
        return {"error": str(e)}
    fired, cleared = [], []
    if res.get("skip"):
        return {"skipped": True, "reason": res.get("skip_reason", "")}
    for key, item in (("smoke", res.get("smoke")), ("interact", res.get("interact"))):
        ak = f"fe_patrol:{key}"
        if item and item.get("unprovisioned"):
            continue
        if item and not item.get("ok"):
            title = "前端日巡失败：" + ("零 pageerror" if key == "smoke" else "交互冒烟")
            detail = (item.get("scope") or "")[:400]
            alerts.raise_alert(ak, title, detail=detail, level="error", source="fe_patrol")
            fired.append(ak)
        elif item:
            alerts.clear_alert(ak, note=item.get("scope", "通过"))
            cleared.append(ak)
    return {"fired": fired, "cleared": cleared}


def _append_hist(rec: dict):
    HIST.parent.mkdir(exist_ok=True)
    try:
        with open(HIST, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if HIST.exists():
            lines = HIST.read_text(encoding="utf-8").splitlines()
            if len(lines) > HIST_MAX:
                HIST.write_text("\n".join(lines[-HIST_MAX:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def run(alert: bool = False) -> dict:
    ts = time.time()
    if not _hub_up():
        res = {"ok": True, "skip": True, "skip_reason": "hub_offline",
               "hub": HUB, "ts": ts,
               "smoke": None, "interact": None,
               "overall": "SKIP", "detail": "Hub 未在线，跳过日巡（避免停机误报）"}
        if alert:
            res["alert_sync"] = {"skipped": True}
        _append_hist({k: res[k] for k in ("ok", "skip", "skip_reason", "overall", "detail", "ts")})
        return res

    smoke = _run_script("_fe_smoke.py", 120)
    # 半途死亡检测①：smoke 后 Hub 已不在 → 环境问题不是前端回归（Hub 可用性归稳定性哨兵管，
    # 这里 FAIL 会错误归因 + 与 hub 死亡告警重复）。02:29 实弹：部署竞态窗口里 smoke 过、interact 连接拒绝。
    if not _hub_up():
        res = {"ok": True, "skip": True, "skip_reason": "hub_died_midrun",
               "hub": HUB, "ts": ts, "smoke": smoke, "interact": None,
               "overall": "SKIP", "detail": "Hub 在日巡进行中掉线（smoke 后）——前端状态未知，交稳定性哨兵归因"}
        if alert:
            res["alert_sync"] = {"skipped": True}
        _append_hist({**{k: res[k] for k in ("ok", "skip", "skip_reason", "overall", "detail", "ts")},
                      "smoke_ok": smoke.get("ok"), "interact_ok": None})
        return res

    interact = _run_script("tools/_fe_interact.py", 150)
    # 半途死亡检测②：interact 失败且 Hub 已不在 → 同上降级 SKIP（失败原因就是 Hub 没了）
    if not interact.get("ok") and not interact.get("unprovisioned") and not _hub_up():
        res = {"ok": True, "skip": True, "skip_reason": "hub_died_midrun",
               "hub": HUB, "ts": ts, "smoke": smoke, "interact": interact,
               "overall": "SKIP", "detail": "Hub 在日巡进行中掉线（interact 段）——前端状态未知，交稳定性哨兵归因"}
        if alert:
            res["alert_sync"] = {"skipped": True}
        _append_hist({**{k: res[k] for k in ("ok", "skip", "skip_reason", "overall", "detail", "ts")},
                      "smoke_ok": smoke.get("ok"), "interact_ok": None})
        return res

    # 两者都 unprovisioned → 整体 skip
    if smoke.get("unprovisioned") and interact.get("unprovisioned"):
        res = {"ok": True, "skip": True, "skip_reason": "playwright_missing",
               "hub": HUB, "ts": ts, "smoke": smoke, "interact": interact,
               "overall": "SKIP", "detail": "playwright 未装，跳过（pip install playwright && playwright install chrome）"}
        if alert:
            res["alert_sync"] = {"skipped": True}
        _append_hist({**{k: res[k] for k in ("ok", "skip", "skip_reason", "overall", "detail", "ts")},
                      "smoke_ok": None, "interact_ok": None})
        return res

    # smoke unprovisioned 但 interact 能跑 → 只评 interact；反之亦然
    checks = []
    if not smoke.get("unprovisioned"):
        checks.append(smoke.get("ok"))
    if not interact.get("unprovisioned"):
        checks.append(interact.get("ok"))
    ok = all(checks) if checks else False

    res = {"ok": ok, "skip": False, "hub": HUB, "ts": ts,
           "smoke": smoke, "interact": interact,
           "overall": "PASS" if ok else "FAIL",
           "detail": "%s · %s" % (smoke.get("scope", "?"), interact.get("scope", "?"))}
    if alert:
        res["alert_sync"] = _sync_alerts(res)
    _append_hist({"ts": ts, "ok": ok, "overall": res["overall"],
                  "smoke_ok": smoke.get("ok"), "interact_ok": interact.get("ok"),
                  "smoke_scope": smoke.get("scope"), "interact_scope": interact.get("scope"),
                  "skip": False})
    return res


def _print(res: dict):
    if res.get("skip"):
        print("  [SKIP] %s" % res.get("detail", res.get("skip_reason", "")))
    else:
        for label, key in (("smoke", "smoke"), ("interact", "interact")):
            it = res.get(key) or {}
            if it.get("unprovisioned"):
                print("  [SKIP] %s  playwright 未装" % label)
                continue
            mark = "OK" if it.get("ok") else "NG"
            print("  [%s] %s  (%.1fs)  %s" % (mark, label, it.get("sec", 0), it.get("scope", "")))
    sm = res.get("smoke") or {}
    ia = res.get("interact") or {}
    sm_s = "skip" if sm.get("unprovisioned") else ("%d/%d" % (
        int(bool(sm.get("ok"))), 1) if sm else "0/0")
    ia_s = "skip" if ia.get("unprovisioned") else ("%d/%d" % (
        int(bool(ia.get("ok"))), 1) if ia else "0/0")
    overall = res.get("overall", "FAIL")
    print("\n== 前端日巡 smoke %s · interact %s · 总判 %s ==" % (sm_s, ia_s, overall))


def main():
    ap = argparse.ArgumentParser(description="前端交互日巡(fe_smoke + fe_interact)")
    ap.add_argument("--alert", action="store_true", help="失败→alerts.py 告警；恢复→clear")
    ap.add_argument("--json", action="store_true", help="stdout 只打 JSON")
    args = ap.parse_args()
    res = run(alert=args.alert)
    if args.json:
        # 截断 out 防 JSON 过大
        slim = dict(res)
        for k in ("smoke", "interact"):
            if isinstance(slim.get(k), dict) and "out" in slim[k]:
                slim[k] = {kk: slim[k][kk] for kk in slim[k] if kk != "out"}
        print(json.dumps(slim, ensure_ascii=False))
    else:
        _print(res)
    if res.get("skip"):
        return 0
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
