# -*- coding: utf-8 -*-
r"""中央凭据池看门狗——每 5 分钟一次，抓「真死」也抓「半死」。

为什么不能只探 /api/health（本项目 2026-07-14 的实测教训）：
EmotionTTS 那次事故里 /health 全程 200、进程活着，但真正的合成接口全部超时，
静默降级 2 小时 20 分钟，health-only 的探测全程绿灯零告警。凭据池同一个形状：
今天这轮就抓到两个「health 200 但 allocate 已坏」的真 bug——
  · report 里的自死锁（写事务里另开连接，30 秒超时）
  · 归还后重分配撞唯一键（account_phone UNIQUE）
所以本看门狗**真打业务口**：用固定探针键走完 allocate → release，
拿不到凭据就算不健康，与端口活着无关（键为什么必须固定见 `probe_key`）。

判定与动作：
  1. /api/health 不通            → 计一次 strike（kind=down）
  2. health 通但 allocate 失败   → 计一次 strike（kind=hang，这是半死）
  3. 两次连续 strike 才重启      → 单次抖动不动服务（防抖振循环）
  4. 重启 30 分钟冷却            → 与实例重启同一条纪律
  5. 恢复后自动清零并记一条恢复日志

用法：
    python deploy\instances\credpool_watchdog.py            # 探 + 需要时自愈
    python deploy\instances\credpool_watchdog.py --dry-run  # 只探不动服务
    python deploy\instances\credpool_watchdog.py --install  # 注册 5 分钟计划任务
    python deploy\instances\credpool_watchdog.py --uninstall

半死演练（验证业务探针真的能抓到 health 绿但 allocate 死的情形）：
    python deploy\instances\credpool_halfdead_sim.py 8099   # 另开一个窗口
    python deploy\instances\credpool_watchdog.py --base-url http://127.0.0.1:8099 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
OPS = Path(r"D:\chengjie-instances\.ops")
STATE = OPS / "credpool_watchdog.state.json"
LOG = OPS / "credpool_watchdog.log"
OVERLAY = Path(r"D:\chengjie-instances\zhiliao\data\config\config.local.yaml")
SERVICE_PS1 = HERE / "credpool_service.ps1"
TASK_NAME = "CredPoolWatchdog"

STRIKES_TO_RESTART = 2
RESTART_COOLDOWN_SEC = 30 * 60
LOG_MAX_BYTES = 2 * 1024 * 1024


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        OPS.mkdir(parents=True, exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > LOG_MAX_BYTES:
            LOG.replace(LOG.with_suffix(".log.1"))
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(st: dict) -> None:
    try:
        OPS.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def read_pool_cfg() -> tuple:
    """从实例 overlay 取池地址与服务 token（不解析整份 YAML，只抓这两个键）。"""
    base_url, token = "http://127.0.0.1:8000", ""
    try:
        import yaml

        data = yaml.safe_load(OVERLAY.read_text(encoding="utf-8")) or {}
        cp = (((data.get("platform_login") or {}).get("telegram") or {})
              .get("credpool") or {})
        base_url = str(cp.get("base_url") or base_url).rstrip("/")
        token = str(cp.get("service_token") or "")
    except Exception:  # noqa: BLE001
        pass
    return base_url, (os.environ.get("CREDPOOL_SERVICE_TOKEN") or token)


def _post(url: str, payload: dict, token: str, timeout: float) -> tuple:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "X-Service-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return exc.code, {}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}


def probe_health(base_url: str) -> tuple:
    try:
        with urllib.request.urlopen(f"{base_url}/api/health", timeout=8) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return (d.get("status") == "ok"), d
    except Exception as exc:  # noqa: BLE001
        return False, {"error": str(exc)[:120]}


def probe_key() -> str:
    """探针用**固定**粘定键（每机一个），刻意不用随机键。

    随机键有三个毛病，固定键把它们一次解决：
      1. 台账每天多 288 行且永不复用，一年十万行噪声，真实分配全被埋掉；
      2. 探针在 allocate 与 release 之间崩了，那一格容量就**永久**漏掉
         （随机键下一轮换新键，没人再来回收它）；固定键下一轮 allocate
         会把上一轮的 released 行清掉、或直接拿回还挂着的那笔（池对同号
         已有活跃分配是幂等返回「已有分配」），自愈；
      3. 固定键每 5 分钟走一次「released → 同号再分配」，正是 2026-07-27
         修掉的 account_phone 唯一键冲突那条路 —— 等于把那个 bug 的回归
         测试常驻在生产里跑。

    带机器后缀是为了多机共用一个中央池时互不打扰（各自探自己那格）。
    """
    suffix = ""
    try:
        mid = OVERLAY.parent / ".credpool_machine_id"
        if mid.is_file():
            suffix = mid.read_text(encoding="utf-8").strip()[-8:]
    except OSError:
        pass
    if not suffix:
        suffix = uuid.uuid5(uuid.NAMESPACE_DNS, os.environ.get(
            "COMPUTERNAME", "host")).hex[:8]
    return f"wd:probe:{suffix}"


def probe_allocate(base_url: str, token: str) -> tuple:
    """真打业务口：走一遍 allocate → release。这才是「池还能用吗」。"""
    if not token:
        return False, "没有服务 token，无法做业务探针（只能探 health）"
    key = probe_key()
    t0 = time.time()
    code, res = _post(f"{base_url}/api/admin/api-pool/allocate",
                      {"phone": key, "account_id": None}, token, 20.0)
    cost = int((time.time() - t0) * 1000)
    ok = code == 200 and bool((res or {}).get("success"))
    api_id = ((res or {}).get("data") or {}).get("api_id")
    if ok:
        # 立刻归还，绝不让探针吃掉池容量（一天 288 次，不还就爆）
        _post(f"{base_url}/api/admin/api-pool/release", {"phone": key}, token, 20.0)
        return True, f"allocate ok api_id={api_id} {cost}ms"
    # 归还也补一刀：allocate 报失败但服务端其实已落库的边界情形，别把容量挂在这
    _post(f"{base_url}/api/admin/api-pool/release", {"phone": key}, token, 5.0)
    detail = (res or {}).get("message") or (res or {}).get("error") or f"HTTP {code}"
    return False, f"allocate 失败：{detail}（{cost}ms）"


def restart_service() -> bool:
    """重启池服务。

    输出重定向到文件而不用 capture_output/PIPE。历史原因：service.ps1 早期用
    Start-Process 拉起池，孙进程继承输出句柄 → 管道要等常驻池退出才关闭 →
    subprocess 永远等不到 EOF（连 timeout 都救不了：超时后 kill 子进程再
    communicate()，仍卡在同一个管道上）。2026-07-27 首次演练实测：池重启成功，
    看门狗自己挂死 5 分钟。

    根因已在 service.ps1 侧修掉（改经计划任务拉起，池完全脱离调用方进程树），
    这里保留文件重定向：一是不依赖对面脚本的实现细节，二是重启时对面说了什么
    正好落进看门狗日志，出事有现场。
    """
    log("触发重启：credpool_service.ps1 -Restart")
    out = OPS / "credpool_watchdog.restart.out"
    try:
        with out.open("w", encoding="utf-8", errors="replace") as fh:
            rc = subprocess.call(
                ["powershell", "-ExecutionPolicy", "Bypass", "-File",
                 str(SERVICE_PS1), "-Restart"],
                stdout=fh, stderr=subprocess.STDOUT, timeout=180)
    except subprocess.TimeoutExpired:
        log("重启脚本超时（180s）")
        return False
    except Exception as exc:  # noqa: BLE001
        log(f"重启失败：{exc}")
        return False
    try:
        for line in out.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                log("  " + line.strip())
    except OSError:
        pass
    return rc == 0


def install_task() -> int:
    ps = (f"$a=New-ScheduledTaskAction -Execute '{sys.executable}' "
          f"-Argument '-u \"{Path(__file__).resolve()}\"';"
          "$t=New-ScheduledTaskTrigger -Once -At (Get-Date) "
          "-RepetitionInterval (New-TimeSpan -Minutes 5);"
          "$s=New-ScheduledTaskSettingsSet -StartWhenAvailable "
          "-MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10);"
          # S4U：无人登录也跑（默认 Interactive 主体只在该用户登录时触发）
          "$p=New-ScheduledTaskPrincipal "
          "-UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) "
          "-LogonType S4U -RunLevel Limited;"
          f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t "
          "-Settings $s -Principal $p -Description '中央凭据池看门狗（health + 真 allocate 探针）' "
          "-Force | Out-Null;"
          f"Write-Host '已注册 {TASK_NAME}（每 5 分钟）'")
    return subprocess.call(["powershell", "-ExecutionPolicy", "Bypass", "-Command", ps])


def uninstall_task() -> int:
    return subprocess.call([
        "powershell", "-ExecutionPolicy", "Bypass", "-Command",
        f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false "
        f"-ErrorAction SilentlyContinue; Write-Host '已移除 {TASK_NAME}'"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只探不动服务")
    ap.add_argument("--force-restart", action="store_true", help="实弹演练：无条件走一次重启")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--status", action="store_true", help="打印看门狗状态")
    ap.add_argument("--base-url", default="", help="演练用：覆盖池地址（半死场景可指向假服务）")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    if args.install:
        return install_task()
    if args.uninstall:
        return uninstall_task()
    if args.status:
        st = load_state()
        print(json.dumps(st, ensure_ascii=False, indent=2) if st else "（还没有状态记录）")
        return 0

    base_url, token = read_pool_cfg()
    if args.base_url:
        base_url = args.base_url.rstrip("/")
    st = load_state()
    now = time.time()

    if args.force_restart:
        log("实弹演练：强制重启")
        ok = restart_service()
        time.sleep(3)
        h_ok, h = probe_health(base_url)
        a_ok, a = probe_allocate(base_url, token)
        log(f"演练后：health={h_ok} {h}；业务探针={a_ok} {a}")
        return 0 if (ok and h_ok and a_ok) else 1

    h_ok, h_detail = probe_health(base_url)
    if h_ok:
        a_ok, a_detail = probe_allocate(base_url, token)
        healthy, kind, detail = a_ok, ("ok" if a_ok else "hang"), a_detail
    else:
        healthy, kind, detail = False, "down", str(h_detail)

    st["last_probe_ts"] = now
    st["last_kind"] = kind
    st["last_detail"] = detail[:300]

    if healthy:
        if int(st.get("strikes") or 0) or st.get("degraded_since"):
            log(f"已恢复（{detail}）")
            st["last_recovery_ts"] = now
        st["strikes"] = 0
        st.pop("degraded_since", None)
        st["last_ok_ts"] = now
        save_state(st)
        print(f"[credpool-wd] 健康：{detail}")
        return 0

    st["strikes"] = int(st.get("strikes") or 0) + 1
    st.setdefault("degraded_since", now)
    down_min = int((now - float(st["degraded_since"])) / 60)
    log(f"不健康（{kind}）第 {st['strikes']} 振，已持续 {down_min} 分钟：{detail}")

    if st["strikes"] < STRIKES_TO_RESTART:
        log("单振不动服务（等下一轮确认，防抖振循环）")
        save_state(st)
        return 1
    if args.dry_run:
        log("--dry-run：本该重启，跳过")
        save_state(st)
        return 1

    since_restart = now - float(st.get("last_restart_ts") or 0)
    if since_restart < RESTART_COOLDOWN_SEC:
        log(f"重启冷却中（还剩 {int((RESTART_COOLDOWN_SEC - since_restart) / 60)} 分钟），"
            "本轮跳过——冷却比反复重启更能保住现场")
        save_state(st)
        return 1

    st["last_restart_ts"] = now
    st["restarts"] = int(st.get("restarts") or 0) + 1
    save_state(st)
    ok = restart_service()
    time.sleep(3)
    h2, _ = probe_health(base_url)
    a2, a2d = probe_allocate(base_url, token)
    if h2 and a2:
        log(f"重启后恢复：{a2d}")
        st.update({"strikes": 0, "last_ok_ts": time.time()})
        st.pop("degraded_since", None)
        save_state(st)
        return 0
    log(f"重启后仍不健康：health={h2} 业务={a2d} —— 需要人介入，看 {LOG}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
