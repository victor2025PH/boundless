#!/usr/bin/env python3
"""安装版后端「升级路径」冒烟：陈旧数据目录 + 新程序，功能必须自愈。

用法（在 desktop/ 下）：
    python build/smoke_upgrade.py [--dist DIR] [--timeout 240] [--keep]

为什么需要它（与 smoke_backend.py 的分工）
==========================================
``smoke_backend.py`` 用 ``tempfile.mkdtemp`` **空目录**起后端——测的是**全新安装**
（种子播种 → 开关全开）。而 104 实机事故恰恰是另一条路：**旧数据目录 + 新程序**。
``ConfigManager._ensure_seeded`` 只在 config 文件**不存在**时播种，于是升级安装永远
拿不到种子里后加的开关；已装用户点「接入 LINE / WhatsApp / Messenger」看到的全是
灰卡「未启用」，且除 Telegram 外界面上没有任何入口能打开（重装也不修——数据目录在
%APPDATA%，覆盖安装不动它）。

源码侧已有三道门禁（读取函数三态 / 诊断层判定 / 跨机制覆盖），但它们都跑在**源码
环境**；冻结产物的目录布局与可选文件存在性完全不同，只有真起打包产物才算闭环
（0.2.0/0.2.1 三个必现缺陷就是这么漏出厂的，见 smoke_backend.py 头注）。

本脚本一次验**两套桌面默认机制**在冻结产物 + 陈旧配置下同时生效：

1. **读时解析**（platform_login.resolve_login_switch + _DESKTOP_LOGIN_DEFAULT_ON）
   → ``GET /api/platforms/{p}/modes`` 的 reason_code 不得是 not_enabled /
   needs_server_setup（即开关已自愈；service_down / dep_missing 属正交环境因素，放行）；
2. **启动补齐**（feature_registry A 类 + ConfigManager._ensure_baseline）
   → ``GET /api/setup/features`` 里 A 类基线必须 state=on。**这条只有陈旧目录才测得到**
   ——空目录下是种子给的，补齐机制根本没被考验。

陈旧配置**从当前种子派生**（删掉待测键）：既保证能启动，又精确隔离「旧配置缺什么」，
且种子演进时自动跟随，不必手工维护一份会腐烂的旧 YAML 副本。

退出码：0 全过；1 有断言失败（这个包不会自愈升级安装）；2 环境问题（产物缺失/起不来）。
"""
from __future__ import annotations

import argparse
import copy
import http.cookiejar
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent            # desktop/build
ENGINE_ROOT = HERE.parent.parent                  # engines/chengjie
DIST = HERE / "backend-dist"
SEED = ENGINE_ROOT / "config" / "config.desktop.min.yaml"
TOKEN = "smoke-upgrade-token"

#: 从种子里**删掉**这些点分路径来伪造「陈旧安装」——即这些键进入种子之前装的机器。
#: 每一项都必须有代码默认机制兜住（否则升级安装拿不到 = 104 事故形态）。
_STRIP_PATHS = (
    "platform_login.telegram.protocol_enabled",
    "platform_login.line",           # 整段：旧配置里没有这些平台
    "platform_login.whatsapp",
    "platform_login.messenger",
    "platform_login.orchestrator_enabled",
    "companion",                     # goals / deep_persona 均为后加基线
    "inbox.reply_style",             # bubbles 后加
    "inbox.read_from_store",         # 代码默认已对齐 True（原 False 造成读路径分叉）
)

#: 期望自愈的 (平台, 方式)。reason_code 落在下面这组＝开关仍关，升级没自愈。
_EXPECT_HEAL = (
    ("telegram", "protocol"),
    ("line", "protocol"),
    ("whatsapp", "protocol"),
    ("messenger", "web"),
)
_STILL_OFF = {"not_enabled", "needs_server_setup"}


def _strip(cfg: dict, dotted: str) -> None:
    """就地删除点分路径（缺失即跳过）。"""
    parts = dotted.split(".")
    node = cfg
    for p in parts[:-1]:
        node = node.get(p) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return
    node.pop(parts[-1], None)


def _stale_config() -> dict:
    import yaml
    cfg = yaml.safe_load(SEED.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict) or not cfg:
        raise RuntimeError(f"种子不是合法 YAML mapping：{SEED}")
    stale = copy.deepcopy(cfg)
    for path in _STRIP_PATHS:
        _strip(stale, path)
    return stale


def _expected_baseline() -> tuple:
    """A 类基线键清单（与构建机同一棵树，零漂移）；读不到时回落最低保障。"""
    try:
        sys.path.insert(0, str(ENGINE_ROOT))
        from src.utils.feature_registry import product_baseline_map
        keys = tuple(product_baseline_map())
        if keys:
            return keys
    except Exception:
        pass
    return ("companion.goals.enabled",)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _rmtree_retry(path: Path, attempts: int = 6, delay: float = 1.0) -> None:
    """删临时目录并重试；实在删不掉就报路径，别无声占盘。

    Windows 上 ``taskkill`` 之后内核可能仍短暂持有刚退出进程的文件句柄，单次
    ``rmtree(ignore_errors=True)`` 会**静默失败**，把一份产物拷贝（数百 MB）永久留在
    %TEMP%。首次实跑即踩到（另一个成因是日志句柄仍开着——调用方须先 close）。
    """
    for _ in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(delay)
    print(f"  ⚠ 临时目录未能删除（可能仍被占用），请手工清理：{path}", file=sys.stderr)


def _backend_exe(dist: Path):
    name = "backend.exe" if os.name == "nt" else "backend"
    p = dist / name
    return p if p.exists() else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_a, **_k):
        return None


def _get(opener, url: str, timeout: float = 25.0):
    try:
        with opener.open(urllib.request.Request(url, method="GET"), timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, f"<{type(e).__name__}: {e}>"


def _post_form(opener, url: str, data: dict, timeout: float = 25.0):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, f"<{type(e).__name__}: {e}>"


def _wait_ready(base: str, timeout_sec: float, proc) -> bool:
    opener = urllib.request.build_opener(_NoRedirect())
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        if proc.poll() is not None:
            print(f"✗ 后端进程提前退出（code={proc.returncode}）", file=sys.stderr)
            return False
        code, _ = _get(opener, base + "/login", timeout=4)
        if code:
            print(f"  · 就绪耗时 {time.time() - t0:.1f}s")
            return True
        time.sleep(1.0)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default=str(DIST),
                    help="后端产物目录（默认 build/backend-dist；也可指向 "
                         "<安装目录>/resources/backend 做装机后验收）")
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--keep", action="store_true", help="保留临时数据目录便于排查")
    ap.add_argument("--no-copy", action="store_true",
                    help="直接用 --dist 原目录（默认先拷到临时目录，避免与并发构建"
                         "抢同一份产物）")
    args = ap.parse_args()

    src_dist = Path(args.dist).resolve()
    if _backend_exe(src_dist) is None:
        print(f"✗ 未找到打包产物：{src_dist}（先跑 npm run build:backend）", file=sys.stderr)
        return 2

    work = Path(tempfile.mkdtemp(prefix="chatx-upgrade-"))
    data_dir = work / "data"
    (data_dir / "config").mkdir(parents=True, exist_ok=True)

    # 产物默认拷一份再用：构建输出目录是共享的，并发构建会把正在跑的 exe 换掉。
    if args.no_copy:
        dist = src_dist
    else:
        dist = work / "dist"
        print(f"→ 拷贝产物到临时目录（避免与并发构建互踩）：{dist}")
        shutil.copytree(src_dist, dist)
    exe = _backend_exe(dist)
    if exe is None:
        print("✗ 拷贝后未找到可执行文件", file=sys.stderr)
        return 2

    # ── 关键一步：先写陈旧 config.yaml，使 _ensure_seeded 判定「已存在」不再播种 ──
    import yaml
    stale = _stale_config()
    cfg_path = data_dir / "config" / "config.yaml"
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("# 升级冒烟伪造的「陈旧安装」配置：由当前种子删去待测键派生。\n")
        yaml.dump(stale, f, default_flow_style=False, allow_unicode=True,
                  sort_keys=False)
    print(f"→ 已写入陈旧配置（剥离 {len(_STRIP_PATHS)} 处）：{cfg_path}")

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env.update({
        "AITR_DESKTOP_MODE": "1",
        "AITR_WEB_HOST": "127.0.0.1",
        "AITR_WEB_PORT": str(port),
        "AITR_WEB_TOKEN": TOKEN,
        "AITR_DATA_DIR": str(data_dir),
        "AITR_CONFIG_PATH": str(cfg_path),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "HOST_ALERT_SILENT": "1",
    })

    log_path = work / "smoke-upgrade.out.log"
    print(f"→ 起后端：{exe}\n  port={port} dataDir={data_dir}")
    failures: list = []
    with open(log_path, "wb") as logf:
        proc = subprocess.Popen([str(exe)], cwd=str(data_dir), env=env,
                                stdout=logf, stderr=subprocess.STDOUT)
        try:
            if not _wait_ready(base, args.timeout, proc):
                print(f"✗ {args.timeout:.0f}s 内未就绪；日志：{log_path}", file=sys.stderr)
                return 2

            jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(jar), _NoRedirect())
            code, _ = _post_form(opener, base + "/login", {"auth_token": TOKEN})
            if code not in (200, 302, 303):
                print(f"✗ POST /login → {code}", file=sys.stderr)
                return 2
            print("  ✓ 登录")

            # ① 读时解析：接入开关必须自愈
            print("· 接入开关自愈（读时解析机制）")
            for platform, mode in _EXPECT_HEAL:
                code, body = _get(opener, f"{base}/api/platforms/{platform}/modes")
                if code != 200:
                    failures.append(f"[modes] {platform} → HTTP {code}")
                    print(f"  ✗ {platform:<10} HTTP {code}")
                    continue
                try:
                    modes = (json.loads(body) or {}).get("modes") or []
                except Exception:
                    failures.append(f"[modes] {platform} 响应非合法 JSON")
                    print(f"  ✗ {platform:<10} 响应非 JSON")
                    continue
                row = next((m for m in modes if m.get("mode") == mode), None)
                if row is None:
                    failures.append(f"[modes] {platform} 的 modes 清单里没有 {mode}")
                    print(f"  ✗ {platform:<10} 缺 mode={mode}")
                    continue
                rc = str(row.get("reason_code") or "")
                ok = rc not in _STILL_OFF
                print(f"  {'✓' if ok else '✗'} {platform:<10} mode={mode:<8} "
                      f"available={row.get('available')} reason_code={rc or '-'}")
                if not ok:
                    failures.append(
                        f"[modes] {platform}/{mode} 仍报 {rc}——升级安装不会自愈"
                        "（该平台读取函数没接 resolve_login_switch？）")

            # ② 启动补齐：A 类基线必须已开（空目录测不到这条）
            print("· 产品基线补齐（启动补齐机制）")
            code, body = _get(opener, base + "/api/setup/features")
            if code != 200:
                failures.append(f"[features] HTTP {code}")
                print(f"  ✗ /api/setup/features → HTTP {code}")
            else:
                try:
                    feats = {f.get("key"): f for f in
                             (json.loads(body) or {}).get("features") or []
                             if isinstance(f, dict)}
                except Exception:
                    feats = {}
                    failures.append("[features] 响应非合法 JSON")
                for key in _expected_baseline():
                    it = feats.get(key)
                    if it is None:
                        # show=False 的 A 类不进总览，不算失败（无从此处观测）
                        print(f"  · {key:<38} 不在总览（show=False，跳过）")
                        continue
                    state = it.get("state")
                    ok = state == "on"
                    print(f"  {'✓' if ok else '✗'} {key:<38} state={state}")
                    if not ok:
                        failures.append(
                            f"[features] {key} state={state}——陈旧目录下基线补齐"
                            "（_ensure_baseline）没生效")

            print()
            if failures:
                print(f"✗ 升级冒烟失败 {len(failures)} 项：", file=sys.stderr)
                for f in failures:
                    print("   - " + f, file=sys.stderr)
                print(f"  后端日志：{log_path}", file=sys.stderr)
                return 1
            print("✓ 升级路径冒烟全过：陈旧数据目录 + 新程序，两套机制均自愈")
            return 0
        finally:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/pid", str(proc.pid), "/T", "/F"],
                                   capture_output=True)
                else:
                    proc.terminate()
                proc.wait(timeout=20)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            # 日志文件就在 work 里，句柄不放开 rmtree 会整棵树删不掉（静默残留数百 MB
            # 的产物拷贝）。close() 幂等，with 退出时再关一次无害。
            try:
                logf.close()
            except Exception:
                pass
            if args.keep or failures:
                print(f"  （保留现场）{work}")
            else:
                _rmtree_retry(work)


if __name__ == "__main__":
    raise SystemExit(main())
