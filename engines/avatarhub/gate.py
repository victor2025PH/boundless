# -*- coding: utf-8 -*-
"""测试门禁（事实 CI）：开发循环 / 提交前 / 多 agent 协作的统一入口。

把项目已有的各类校验串成分层门禁，一条命令跑完给出 PASS/FAIL + 退出码，
方便人工、pre-commit、或「测试 agent」直接调用。

分层（由快到慢、由离线到在线）：
  Tier A  语法编译   对所有「纳入 git 的项目自有 *.py」做 py_compile（不 import，
                     不需 GPU/Hub/重依赖，秒级；抓 syntax error 最快的网）
  Tier B  离线单测   run_all_tests.py（聚合 Phase 5–11 单元套件，离线子集）
  Tier U  UI 门禁    test_ui_optimization.py（UI 字符串契约，离线恒跑）
                     + ui_visual_regress.py（无头多分辨率像素回归，需 Hub+Edge，仅 --online）
  Tier C  在线全检   deliver_check.py（需 Hub 在线；仅 --online/--full 时跑）
                     + run_all_tests.py 带 HUB_URL 的在线用例

用法：
  python gate.py                 # 默认：Tier A + B（纯离线，无需 Hub/GPU）—— 开发循环常用
  python gate.py --online        # 追加 Tier C（需 Hub 在线，自动探活）
  python gate.py --online --full # 在线 + 重负载回归（acceptance --full）
  python gate.py --compile-only  # 只跑语法编译（最快）
  python gate.py --env-gate      # 追加 Tier E：site-packages 完整性编译（环境迁移/解包后必跑，
                                 #   见 tools/env_compile_gate.py；.140/.117 两起损坏事故的源头闸）
  python gate.py --hub http://127.0.0.1:9000

P15-5 抗并发三闸（多 agent 并行开发的瞬态假红是头号误报来源）：
  单实例锁    _gate.lock 防两个门禁互踩（Playwright/端口/临时角色互相污染）；
              撞锁默认等待 --wait-lock 秒（持有者跑完即接棒），--no-lock 可跳过。
  漂移检测    每层开跑前快照自有文件 mtime；层失败且检测到运行期间被外部修改
              → 判定环境不洁，自动重跑该层一次（有证据才重试，无证据的失败是真失败）。
  漂移通报    结尾汇报整场运行期间的外部修改清单（绿灯也报，供判断结果可信度）。

退出码：0=全绿  1=有失败（任一 Tier 未通过）  3=撞锁超时未获得执行权
"""
import json
import os
import sys
import time
import argparse
import subprocess
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def _ok(s):
    try:
        "✓✗·".encode(sys.stdout.encoding or "utf-8")
        return {"ok": "✓", "ng": "✗", "skip": "·"}[s]
    except Exception:
        return {"ok": "[OK]", "ng": "[X]", "skip": "[-]"}[s]


def _list_own_py():
    """项目自有的 *.py 文件清单。优先用 `git ls-files`（自动排除 .gitignore 的
    第三方模型仓库/虚拟环境/产物），git 不可用时回退到顶层 + 选定目录的手动遍历。"""
    try:
        r = subprocess.run(["git", "ls-files", "*.py"], cwd=str(HERE),
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            files = [l.strip() for l in r.stdout.splitlines() if l.strip()]
            if files:
                return files
    except Exception:
        pass
    # 回退：仅顶层 + tools/ + installer/（避免误扫第三方大目录）
    files = [p.name for p in HERE.glob("*.py")]
    for sub in ("tools", "installer"):
        d = HERE / sub
        if d.is_dir():
            files += [str(p.relative_to(HERE)).replace("\\", "/") for p in d.rglob("*.py")]
    return files


# ── P15-5 抗并发三闸：单实例锁 / 漂移检测自愈重试 / 漂移通报 ──────────────
LOCK_FILE = HERE / "_gate.lock"


def _pid_alive(pid: int) -> bool:
    """进程存活检查。Windows 严禁 os.kill(pid, 0)——CPython 在 NT 上对非信号值直接
    TerminateProcess，探活会把别人杀了；走 OpenProcess+GetExitCodeProcess。"""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == 259               # STILL_ACTIVE
            return True
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def gate_lock_acquire(wait_s: int = 0) -> bool:
    """单实例锁：O_CREAT|O_EXCL 原子建锁文件（记 pid/时刻）。撞锁时：持有者已死或
    锁龄 >2h → 陈锁接管；否则按 wait_s 每 3s 重试。True=拿到执行权。"""
    deadline = time.time() + max(0, wait_s)
    while True:
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps({"pid": os.getpid(), "ts": time.time(),
                                    "at": time.strftime("%Y-%m-%d %H:%M:%S")}))
            return True
        except FileExistsError:
            try:   # utf-8-sig：容忍第三方工具写锁带 BOM——解析失败会被误判陈锁而接管
                info = json.loads(LOCK_FILE.read_text(encoding="utf-8-sig"))
            except Exception:
                info = {}
            pid, ts = int(info.get("pid", 0) or 0), float(info.get("ts", 0) or 0)
            if not _pid_alive(pid) or (time.time() - ts) > 2 * 3600:
                print("  [锁] 清除陈锁（持有者 pid=%s 已不在/超龄）" % (pid or "?"))
                try:
                    LOCK_FILE.unlink()
                except Exception:
                    pass
                continue
            if time.time() >= deadline:
                print("  [锁] 另一门禁正在运行（pid=%s 自 %s）——等待 %ds 未让出，退出。\n"
                      "        并发门禁互踩正是瞬态假红的头号来源；稍后重跑，或 --wait-lock 600 排队。"
                      % (pid, info.get("at", "?"), wait_s))
                return False
            time.sleep(3)
        except Exception as e:
            print("  [锁] 锁机制异常(降级为无锁运行): %s" % e)
            return True


def gate_lock_release():
    try:
        if LOCK_FILE.exists():
            info = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
            if int(info.get("pid", 0) or 0) == os.getpid():
                LOCK_FILE.unlink()
    except Exception:
        pass


def _watch_files():
    """漂移监视清单：门禁实际取证的自有源文件（py + 前端静态 + 文档）。"""
    files = list(_list_own_py())
    for pat in ("static/*.html", "static/*.css", "static/*.js", "*.md"):
        files += [str(p.relative_to(HERE)).replace("\\", "/") for p in HERE.glob(pat)]
    return sorted(set(files))


def _mtime_snapshot() -> dict:
    """自有文件 mtime 快照（~300 个 stat，毫秒级）。"""
    snap = {}
    for f in _watch_files():
        try:
            snap[f] = (HERE / f).stat().st_mtime_ns
        except Exception:
            snap[f] = None
    return snap


def _snapshot_drift(old: dict, new: dict) -> list:
    """两快照间被外部改动/增删的文件清单。"""
    keys = set(old) | set(new)
    return sorted(k for k in keys if old.get(k) != new.get(k))


_GATE_DRIFT: list = []   # 整场累计的外部修改（结尾通报）


def _run_tier(fn):
    """跑一层，失败且检测到「运行期间自有文件被外部修改」→ 判定环境不洁，自动重跑一次。
    只在有漂移证据时重试：无证据的失败是真失败，盲目重试只会掩盖 bug（P14 复盘：
    三轮门禁两轮红都是并发会话竞争——一轮改了测试文件、一轮抢了资源）。"""
    snap = _mtime_snapshot()
    ok = fn()
    drift = _snapshot_drift(snap, _mtime_snapshot())
    if drift:
        _GATE_DRIFT.extend(d for d in drift if d not in _GATE_DRIFT)
    if ok is False and drift:
        print("  ⚠ 本层运行期间检测到外部修改 %d 个文件（%s%s）——疑似并发会话竞争，自动重跑一次"
              % (len(drift), ", ".join(drift[:3]), " …" if len(drift) > 3 else ""))
        ok = fn()
    return ok


def tier_compile():
    """Tier A：对项目自有 *.py 逐个 py_compile（语法门禁）。"""
    import py_compile
    files = _list_own_py()
    print("\n▶ [Tier A] 语法编译  (%d 个 .py)" % len(files), flush=True)
    errors = []
    for f in files:
        path = HERE / f
        if not path.is_file():
            continue
        try:
            py_compile.compile(str(path), doraise=True, quiet=1)
        except py_compile.PyCompileError as e:
            errors.append((f, str(e.msg if hasattr(e, "msg") else e)))
        except Exception as e:
            errors.append((f, str(e)))
    if errors:
        print("  %s 语法编译失败 %d 个：" % (_ok("ng"), len(errors)))
        for f, msg in errors[:30]:
            print("    - %s" % f)
            for line in str(msg).splitlines()[:3]:
                print("        %s" % line)
        return False
    print("  %s 全部 %d 个文件语法 OK" % (_ok("ok"), len(files)))
    return True


def tier_offline_tests():
    """Tier B：run_all_tests.py 离线套件。"""
    script = HERE / "run_all_tests.py"
    print("\n▶ [Tier B] 离线单元套件  (run_all_tests.py)", flush=True)
    if not script.is_file():
        print("  %s run_all_tests.py 缺失，跳过" % _ok("skip"))
        return True
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.pop("HUB_URL", None)  # 强制离线
    r = subprocess.run([PY, "run_all_tests.py"], cwd=str(HERE), env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    for line in r.stdout.splitlines():
        if any(k in line for k in ("[OK]", "[NG]", "合计", "ALL GREEN", "失败套件")):
            print("    " + line.strip())
    ok = r.returncode == 0
    # B2（P10）：授权契约 _license_test.py（验签/指纹/CRL/预设闸门/试用升级台账，55 项）。
    # 输出格式与 run_all_tests 聚合不同（"总计: N/M 通过"），单跑按退出码判定；
    # cryptography 未装（极简环境）→ SKIP 不阻断。
    lt = HERE / "_license_test.py"
    if lt.is_file():
        r2 = subprocess.run([PY, "_license_test.py"], cwd=str(HERE), env=env,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        line2 = next((l for l in reversed(r2.stdout.splitlines()) if "总计" in l), "")
        if r2.returncode != 0 and "cryptography" in (r2.stdout + (r2.stderr or "")):
            print("    [授权契约] %s cryptography 未装，跳过" % _ok("skip"))
        else:
            print("    [授权契约] %s" % (line2.strip() or ("退出码 %d" % r2.returncode)))
            ok = ok and (r2.returncode == 0)
    # B3（P14）：一键发码/客户视图 HTTP 冒烟（tools/_p14_smoke.py，内嵌线程服务器，离线自足）。
    # 单测证纯函数语义，这里证「握手→验签→出码→台账落盘」整条 HTTP 链真通。
    sm = HERE / "tools" / "_p14_smoke.py"
    if sm.is_file():
        r3 = subprocess.run([PY, str(sm)], cwd=str(HERE), env=env,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        line3 = next((l for l in reversed(r3.stdout.splitlines()) if "P14 smoke" in l), "")
        print("    [发码冒烟] %s %s" % (_ok("ok" if r3.returncode == 0 else "ng"), line3.strip()))
        ok = ok and (r3.returncode == 0)
    print("  %s 离线套件 %s" % (_ok("ok" if ok else "ng"), "全通过" if ok else "存在失败"))
    return ok


def tier_ui(args):
    """Tier U：UI 门禁。
    U1 静态契约（test_ui_optimization.py，离线字符串断言，恒跑）；
    U2 可视化回归（ui_visual_regress.py，需 Hub 在线 + Edge，仅 --online；退出码 2=跳过）。"""
    print("\n▶ [Tier U] UI 门禁  (静态契约 + 可视化回归)", flush=True)
    ok = True
    # U0：运维一页纸对账（P13）——文档提的子命令/旗标/端点逐条核对 CLI 与源码，
    # 离线秒级；文档与代码脱节（serve NameError 类事故的前兆）在此拦下。
    dg = HERE / "tools" / "_ops_doc_gate.py"
    if dg.is_file():
        env0 = dict(os.environ)
        env0["PYTHONIOENCODING"] = "utf-8"
        env0["PYTHONUTF8"] = "1"
        r0 = subprocess.run([PY, str(dg)], cwd=str(HERE), env=env0,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        line0 = next((l for l in reversed(r0.stdout.splitlines()) if "对账" in l), "")
        print("    [一页纸对账] %s %s" % (_ok("ok" if r0.returncode == 0 else "ng"), line0.strip()))
        ok = ok and (r0.returncode == 0)
    # U1：静态字符串契约（离线，不依赖 Hub/Edge）
    script = HERE / "test_ui_optimization.py"
    if script.is_file():
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env.pop("HUB_URL", None)  # 离线：HTTP 探活项自动跳过
        r = subprocess.run([PY, "test_ui_optimization.py", "1"], cwd=str(HERE), env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        line = next((l for l in reversed(r.stdout.splitlines())
                     if "通过" in l and "/" in l), "")
        print("    [静态契约] %s" % (line.strip() or ("退出码 %d" % r.returncode)))
        ok = ok and (r.returncode == 0)
    else:
        print("    [静态契约] %s test_ui_optimization.py 缺失，跳过" % _ok("skip"))
    # U2：可视化回归（需 Hub 在线 + Edge）
    vr = HERE / "ui_visual_regress.py"
    if not vr.is_file():
        print("    [可视化回归] %s ui_visual_regress.py 缺失，跳过" % _ok("skip"))
    elif args.online and _hub_up(args.hub):
        env2 = dict(os.environ)
        env2["PYTHONIOENCODING"] = "utf-8"
        env2["PYTHONUTF8"] = "1"
        r2 = subprocess.run([PY, "ui_visual_regress.py", "--base", args.hub], cwd=str(HERE),
                            env=env2, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r2.returncode == 2:
            tail = next((l for l in reversed(r2.stdout.splitlines()) if l.strip()), "")
            print("    [可视化回归] %s 跳过（%s）" % (_ok("skip"), tail.strip()[:50]))
        else:
            line2 = next((l for l in reversed(r2.stdout.splitlines())
                          if "项超阈" in l or "全部通过" in l), "")
            print("    [可视化回归] %s" % (line2.strip() or ("退出码 %d" % r2.returncode)))
            ok = ok and (r2.returncode == 0)  # 0=通过；1=超阈回归（阻断）
            # 已实跑过一轮 → Tier C 里 deliver_check→acceptance 的 uivr 项去重跳过（省 ~52s）
            os.environ["ACCEPT_SKIP_UIVR"] = "1"
        # U3：开播页相位截图存档（P7-4 交付证据链：guide/ready/starting/ended/locked/large）。
        # 非门禁——相位图含实时服务数据（设备/角色），逐像素对比必然误报（见工具头注释），
        # 故仅存档供人工复核/交付验收取证；失败只提示不阻断。定向覆盖 ui_snapshots/phases/，不膨胀。
        ph = HERE / "tools" / "stream_state_shots.py"
        if ph.is_file():
            r3 = subprocess.run([PY, str(ph), "--base", args.hub,
                                 "--out", str(HERE / "ui_snapshots" / "phases")],
                                cwd=str(HERE), env=env2, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
            n_ok = sum(1 for l in r3.stdout.splitlines() if "captured=True" in l)
            print("    [相位存档] %s %d 张 → ui_snapshots/phases/（证据存档，不比对）"
                  % (_ok("ok" if r3.returncode == 0 else "skip"), n_ok))
        # U4：战报分享卡实拍存档（P10-2）。canvas 产物不进 DOM，uivr/相位都拍不到——
        # 用真实渲染管线导出 PNG 存档（含时间戳/实时数据，只留证不比对）；缺 playwright 退出码 2=跳过。
        rc = HERE / "tools" / "_recap_card_shot.py"
        if rc.is_file():
            r4 = subprocess.run([PY, str(rc), "--base", args.hub,
                                 "--out", str(HERE / "ui_snapshots" / "phases" / "recap_card.png")],
                                cwd=str(HERE), env=env2, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
            print("    [战报实拍] %s → ui_snapshots/phases/recap_card.png（证据存档，不比对）"
                  % (_ok("ok" if r4.returncode == 0 else "skip")))
        # U5：授权态六谱矩阵存档（P12-4）。授权卡/横幅是运行态注入的 innerHTML，uivr 关页态拍不到——
        # 拦截 /api/license/status 注入 trial/valid/trialing(+临期)/grace/expired 六态逐一实拍
        # （卡片+横幅），客户会遇到的授权 UI 全谱系留证；不比对不阻断，缺 playwright 跳过。
        lc = HERE / "tools" / "_lic_card_shot.py"
        if lc.is_file():
            r5 = subprocess.run([PY, str(lc), "--base", args.hub, "--matrix",
                                 "--out", str(HERE / "ui_snapshots" / "phases" / "lic_states")],
                                cwd=str(HERE), env=env2, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
            n5 = sum(1 for l in r5.stdout.splitlines() if l.startswith("OK: lic_") or l.startswith("OK: tri")
                     or (l.startswith("OK:") and "chip=" in l))
            print("    [授权态矩阵] %s %d 态 → ui_snapshots/phases/lic_states/（证据存档，不比对）"
                  % (_ok("ok" if r5.returncode == 0 else "skip"), n5))
        # U6：看板扫码聚焦联动（P13-5）——点行为驱动的前端态，静态契约摸不到；真浏览器点一遍。
        # 无扫码数据时探针自跳（退出码 2），不阻断；断言失败（退出码 1）阻断。
        dp = HERE / "tools" / "_p13_dash_probe.py"
        if dp.is_file():
            r6 = subprocess.run([PY, str(dp), "--base", args.hub], cwd=str(HERE), env=env2,
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
            t6 = next((l for l in r6.stdout.splitlines()
                       if l.startswith(("OK:", "FAIL:", "SKIP:"))), "")
            if r6.returncode == 2:
                print("    [扫码聚焦联动] %s %s" % (_ok("skip"), t6.strip()[:60]))
            else:
                print("    [扫码聚焦联动] %s %s" % (_ok("ok" if r6.returncode == 0 else "ng"),
                                                    t6.strip()[:60]))
                ok = ok and (r6.returncode == 0)
        # U7：厂商看板同屏探针（P13）——自起临时发牌服务，验证 遥测+漏斗+按周时序 三卡同屏
        # （P12 双 fetch 后到覆盖先到的竞态就是在这暴露的）。无 secrets/sk 的机器自跳。
        vd = HERE / "tools" / "_vendor_dash_shot.py"
        if vd.is_file():
            r7 = subprocess.run([PY, str(vd), "--self-serve"], cwd=str(HERE), env=env2,
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
            t7 = next((l for l in r7.stdout.splitlines()
                       if l.startswith(("OK:", "FAIL:", "SKIP:"))), "")
            if r7.returncode == 2:
                print("    [厂商看板] %s %s" % (_ok("skip"), t7.strip()[:60]))
            else:
                print("    [厂商看板] %s %s" % (_ok("ok" if r7.returncode == 0 else "ng"),
                                                t7.strip()[:60]))
                ok = ok and (r7.returncode == 0)
        # U8：弱网演练（P15-4）——观众墙 断链→贴片→轮询兜底→重连→toast→uivr抑制 全生命周期，
        # 真浏览器离线仿真（不碰 Hub 进程，杀页面侧连接）。P14-5 的 UI 从"字符串在"升级为"行为真"。
        rp = HERE / "tools" / "_p15_reconnect_probe.py"
        if rp.is_file():
            r8 = subprocess.run([PY, str(rp), "--base", args.hub], cwd=str(HERE), env=env2,
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
            t8 = next((l for l in r8.stdout.splitlines()
                       if l.startswith(("OK:", "FAIL:", "SKIP:"))), "")
            if r8.returncode == 2:
                print("    [弱网演练] %s %s" % (_ok("skip"), t8.strip()[:60]))
            else:
                print("    [弱网演练] %s %s" % (_ok("ok" if r8.returncode == 0 else "ng"),
                                                t8.strip()[:60]))
                ok = ok and (r8.returncode == 0)
    else:
        print("    [可视化回归] %s 跳过（需 --online 且 Hub 在线）" % _ok("skip"))
    print("  %s UI 门禁 %s" % (_ok("ok" if ok else "ng"), "通过" if ok else "存在失败"))
    return ok


def tier_env_gate():
    """Tier E：site-packages 完整性编译（tools/env_compile_gate.py）。
    环境迁移(conda-pack/scp/解包)后 site-packages 可能有损坏 .py——服务能起、/health 也 200，
    但真正调用时才炸。此闸把它拦在起服务之前。"""
    script = HERE / "tools" / "env_compile_gate.py"
    print("\n▶ [Tier E] 环境完整性  (site-packages compileall)", flush=True)
    if not script.is_file():
        print("  %s tools/env_compile_gate.py 缺失，跳过" % _ok("skip"))
        return True
    r = subprocess.run([PY, str(script)], cwd=str(HERE),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = [l for l in r.stdout.splitlines() if l.strip()][-6:]
    for line in tail:
        print("    " + line.strip())
    ok = r.returncode == 0
    print("  %s 环境完整性 %s" % (_ok("ok" if ok else "ng"), "通过" if ok else "有损坏文件"))
    return ok


def tier_phase12_gate():
    """Tier F：Phase 12 产品化门禁（test_phase12.py，离线）。"""
    script = HERE / "test_phase12.py"
    print("\n▶ [Tier F] Phase 12 产品化门禁  (test_phase12.py)", flush=True)
    if not script.is_file():
        print("  %s test_phase12.py 缺失，跳过" % _ok("skip"))
        return True
    r = subprocess.run([PY, str(script)], cwd=str(HERE),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    for line in r.stdout.splitlines():
        if line.strip():
            print("    " + line.strip())
    ok = r.returncode == 0
    print("  %s Phase 12 门禁 %s" % (_ok("ok" if ok else "ng"), "通过" if ok else "存在失败"))
    return ok


def _hub_up(hub, timeout=4.0):
    try:
        with urllib.request.urlopen(hub.rstrip("/") + "/health", timeout=timeout):
            return True
    except Exception:
        return False


def tier_online(args):
    """Tier C：在线全检（Hub 必须在线）。run_all_tests 在线用例 + deliver_check。"""
    print("\n▶ [Tier C] 在线全检  (Hub=%s)" % args.hub, flush=True)
    if not _hub_up(args.hub):
        print("  %s Hub 未响应 %s/health —— 跳过在线检（先 start_all_services.bat）"
              % (_ok("skip"), args.hub))
        return None  # None=跳过，不计入失败
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["HUB_URL"] = args.hub
    ok = True
    # C1: 在线单测
    r = subprocess.run([PY, "run_all_tests.py"], cwd=str(HERE), env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    line = next((l for l in r.stdout.splitlines() if "合计" in l), "")
    print("    [在线单测] %s" % (line.strip() or ("退出码 %d" % r.returncode)))
    ok = ok and (r.returncode == 0)
    # C2: deliver_check（在线交付门禁）
    cmd = [PY, "deliver_check.py", "--hub", args.hub]
    if args.full:
        cmd.append("--full")
    r2 = subprocess.run(cmd, cwd=str(HERE), env=env,
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    line2 = next((l for l in reversed(r2.stdout.splitlines()) if "交付结论" in l), "")
    print("    [交付门禁] %s" % (line2.strip() or ("退出码 %d" % r2.returncode)))
    # deliver_check: 0=可交付 1=警告 2=失败；门禁里把 2 视为失败，1(警告)放行
    ok = ok and (r2.returncode < 2)
    print("  %s 在线全检 %s" % (_ok("ok" if ok else "ng"), "通过" if ok else "存在失败"))
    return ok


def main():
    ap = argparse.ArgumentParser(description="AvatarHub 测试门禁（事实 CI）")
    ap.add_argument("--online", action="store_true", help="追加 Tier C 在线全检（需 Hub 在线）")
    ap.add_argument("--full", action="store_true", help="在线回归含重负载（acceptance --full）")
    ap.add_argument("--compile-only", action="store_true", help="只跑 Tier A 语法编译")
    ap.add_argument("--env-gate", action="store_true",
                    help="追加 Tier E：site-packages 完整性编译（环境迁移/解包后必跑）")
    ap.add_argument("--phase12-gate", action="store_true",
                    help="追加 Tier F：Phase 12 产品化门禁（未实现入口/端点契约，离线）")
    ap.add_argument("--hub", default=os.environ.get("ACCEPT_HUB", "http://127.0.0.1:9000"))
    ap.add_argument("--wait-lock", type=int, default=120,
                    help="撞上另一门禁实例时最多等待秒数（默认 %(default)s；0=立即失败）")
    ap.add_argument("--no-lock", action="store_true",
                    help="跳过单实例锁（明知故犯地并发跑，后果自负）")
    args = ap.parse_args()

    # P15-5 单实例锁：并发门禁互踩（Playwright/端口/临时角色）是瞬态假红的头号来源
    if not args.no_lock:
        if not gate_lock_acquire(wait_s=args.wait_lock):
            return 3
    t0 = time.time()
    try:
        print("=" * 64)
        print("  AvatarHub 测试门禁  gate.py   %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
        print("=" * 64)

        results = []
        results.append(("Tier A 语法编译", _run_tier(tier_compile)))
        if args.env_gate:
            results.append(("Tier E 环境完整性", _run_tier(tier_env_gate)))
        if args.phase12_gate:
            results.append(("Tier F Phase12 产品化", _run_tier(tier_phase12_gate)))
        if not args.compile_only:
            results.append(("Tier B 离线单测", _run_tier(tier_offline_tests)))
            results.append(("Tier U UI 门禁", _run_tier(lambda: tier_ui(args))))
            if args.online:
                c = _run_tier(lambda: tier_online(args))
                if c is not None:
                    results.append(("Tier C 在线全检", c))

        failed = [name for name, ok in results if ok is False]
        print("\n" + "=" * 64)
        for name, ok in results:
            print("  %s %s" % (_ok("ok" if ok else "ng"), name))
        if _GATE_DRIFT:
            print("  ⚠ 运行期间外部修改 %d 个文件（%s%s）——结果按当时文件为准，建议稳定后复跑"
                  % (len(_GATE_DRIFT), ", ".join(_GATE_DRIFT[:5]),
                     " …" if len(_GATE_DRIFT) > 5 else ""))
        dur = round(time.time() - t0, 1)
        if failed:
            print("\n  门禁结论：✗ 未通过（%s）  ·  用时 %ss" % ("; ".join(failed), dur))
            print("=" * 64)
            return 1
        print("\n  门禁结论：✅ 全绿 · 可提交  ·  用时 %ss" % dur)
        print("=" * 64)
        return 0
    finally:
        if not args.no_lock:
            gate_lock_release()


if __name__ == "__main__":
    sys.exit(main())
