#!/usr/bin/env python3
"""安装版后端冒烟：真起打包产物，打一遍 URL 矩阵，通不过就别发版。

用法（在 desktop/ 下）：
    npm run smoke:backend            # = python build/smoke_backend.py
    python build/smoke_backend.py [--port 0] [--timeout 240] [--keep]

为什么需要它
============
CI 与 pytest 全跑在**源码环境**，而安装版跑的是 PyInstaller 冻结产物：目录布局、
可选文件是否存在、相对路径解析全都不同。0.2.0/0.2.1 就这样带着三个必现缺陷出厂
（173 实测）——

  1. ``shared/copilot`` 未随包 → ``/copilot/*`` 全 404（右栏业务助手直接显示
     ``{"detail":"Not Found"}``）；
  2. ``config/templates.yaml`` 未随包 → 触发 ConfigManager 里一行「只要走到就崩」
     的日志格式化 → 点管理后台必 500；
  3. 离线壳缺少 ``data-ws-offline`` 标记 → 桌面壳把 PWA 离线页误判成加载成功。

三条全部能被下面这张矩阵一次抓住。静态侧另有
``tests/test_desktop_package_datas.py``（打包清单棘轮，CI 每次都跑）。

退出码：0 全过；1 有断言失败；2 环境问题（产物缺失 / 起不来）。
"""
from __future__ import annotations

import argparse
import http.cookiejar
import os
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

HERE = Path(__file__).resolve().parent          # desktop/build
DIST = HERE / "backend-dist"
TOKEN = "smoke-token"

# (路径, 期望状态码, 响应体必须包含的片段 or None)
ANON_MATRIX = [
    ("/login", 200, None),
    # 身份探针：桌面壳复用外部后端前用它核对「这是不是自家后端」。它必须免鉴权、
    # 且在**打包产物**里也在——漏了的话壳退回「有响应就复用」，端口冲突重新变成盲区。
    ("/api/desktop/ping", 200, '"app":"chengjie"'),
    ("/sw.js", 200, None),
    # 离线壳：桌面壳靠 data-ws-offline 识破「SW 回落」，丢了这个标记
    # 自动重连会重新失效（用户又得手点「重新连接」）。
    ("/static/pwa/offline.html", 200, "data-ws-offline"),
    ("/copilot/app.html", 200, "<cp-draft"),
    ("/copilot/tokens.css", 200, "--cp-radius"),
    ("/copilot/client/copilot-client.js", 200, None),
]
AUTH_MATRIX = [
    # 管理后台首页。必须先切「完整模式」——默认 simple 会 303 到 /cases，
    # 根本走不到 _build_dashboard_data，那正是 0.2.1 的 500 现场（用户切过完整模式才踩中）。
    ("/", 200, "<html"),
    ("/cases", 200, None),
    # /templates 直接调 get_dynamic_templates_config（与 dashboard 同一崩溃路径），
    # 不依赖 ui_mode，是这条 bug 最短的探针。
    ("/templates", 200, "<html"),
    ("/workspace", 200, None),
    ("/api/workspace/me", 200, None),
]


# 结构化断言：光看状态码抓不到「接口通了但少了关键字段」。
# 典型事故形态：诊断字段被重构掉 → 接入弹窗静默退回「一律说尚未启用」，全绿无人察觉。
def _check_modes_carry_blockers(d: dict) -> str:
    modes = d.get("modes")
    if not isinstance(modes, list) or not modes:
        return "modes 为空"
    for m in modes:
        for field in ("mode", "available", "ready", "blockers"):
            if field not in m:
                return f"mode={m.get('mode')} 缺字段 {field}"
        if not isinstance(m["blockers"], list):
            return f"mode={m['mode']} 的 blockers 不是列表"
    return ""


def _check_readiness_matrix(d: dict) -> str:
    mtx = d.get("platforms")
    if not isinstance(mtx, dict):
        return "缺 platforms 矩阵"
    missing = {"telegram", "line", "whatsapp", "messenger"} - set(mtx)
    if missing:
        return f"矩阵缺平台 {sorted(missing)}"
    for plat, pr in mtx.items():
        if "ready" not in pr or not isinstance(pr.get("modes"), dict):
            return f"{plat} 结构不完整"
    return ""


def _check_trial_claim_state(d: dict) -> str:
    """试用领取口在冻结态可用、且**指纹能算出来**。

    0.2.1/0.2.2 安装版实测漏打 platform/licensing → 指纹取空 → 授权绑机校验按
    fail-open 放行，一份试用可在任意机器复用。而漏包是静默的（聊天照跑），
    只有这里真调一次才暴露。未领取时该口回 {ok, claimed:false}，够判断链路在。
    """
    if d.get("ok") is not True:
        return f"ok={d.get('ok')}（领取口不该报错，失败也要软返回）"
    if "claimed" not in d:
        return "缺 claimed 字段"
    return ""


# (路径, 说明, 校验器) —— 校验器返回空串=通过，否则返回失败原因
JSON_CHECKS = [
    ("/api/platforms/telegram/modes", "登录方式带诊断字段", _check_modes_carry_blockers),
    ("/api/platforms/line/modes", "登录方式带诊断字段", _check_modes_carry_blockers),
    ("/api/accounts/protocol/readiness", "四平台就绪矩阵", _check_readiness_matrix),
    ("/api/admin/license/trial-claim", "试用领取口在冻结态可用", _check_trial_claim_state),
]


def check_packaged_fingerprint(dist: Path) -> str:
    """按文件路径加载的瘦模块必须真在包里、且真能算出指纹。

    PyInstaller 的静态分析追不到「按路径加载」的模块（没有 import 语句），
    platform/licensing 漏打过一次：指纹取空 → 授权绑机按 fail-open 放行 →
    一份试用可在任意机器复用。而漏包是**静默**的（聊天照跑），只有真算一次才暴露。
    刻意不经 HTTP：坐席端点刻意屏蔽指纹这类字段，绕开端点直验本体更稳。
    """
    import importlib.util

    mid = dist / "_internal" / "platform" / "licensing" / "machine_id.py"
    if not mid.exists():
        mid = dist / "platform" / "licensing" / "machine_id.py"
    if not mid.exists():
        return "platform/licensing/machine_id.py 不在包里（绑机会静默失效）"
    try:
        spec = importlib.util.spec_from_file_location("_smoke_mid", mid)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        fp = str(mod.machine_fingerprint() or "")
    except Exception as e:  # noqa: BLE001
        return f"加载失败 {type(e).__name__}: {e}"
    if len(fp.replace("-", "")) != 16:
        return f"指纹形态异常: {fp!r}"
    return ""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _backend_exe(dist: Path) -> Path | None:
    name = "backend.exe" if os.name == "nt" else "backend"
    p = dist / name
    return p if p.exists() else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_a, **_k):
        return None


def _make_opener(jar):
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar), _NoRedirect()
    )


def _get(opener, url: str, timeout: float = 20.0):
    """返回 (status, body_text)。非 2xx 也返回，不抛。"""
    try:
        with opener.open(urllib.request.Request(url, method="GET"), timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"<{type(e).__name__}: {e}>"


def _post_form(opener, url: str, data: dict, timeout: float = 20.0):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"<{type(e).__name__}: {e}>"


def _force_full_ui_mode(opener, jar, base: str) -> None:
    """把会话切到「完整模式」。

    默认 ui_mode=simple 时 ``/`` 直接 303 到 ``/cases`` —— 冒烟看起来「通了」，
    实际从没进过 dashboard。先走真路由（顺带验证 /set_ui_mode 可用），
    再兜底把 cookie 塞进 jar，确保断言打在真正的页面上。
    """
    _get(opener, base + "/set_ui_mode?mode=full&next=/cases")
    if any(c.name == "ui_mode" and c.value == "full" for c in jar):
        return
    jar.set_cookie(http.cookiejar.Cookie(
        version=0, name="ui_mode", value="full", port=None, port_specified=False,
        domain="127.0.0.1", domain_specified=False, domain_initial_dot=False,
        path="/", path_specified=True, secure=False, expires=None, discard=True,
        comment=None, comment_url=None, rest={}, rfc2109=False,
    ))


def _wait_ready(base: str, timeout_sec: float, proc) -> bool:
    jar = http.cookiejar.CookieJar()
    opener = _make_opener(jar)
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        if proc.poll() is not None:
            print(f"✗ 后端进程提前退出（code={proc.returncode}）")
            return False
        code, _ = _get(opener, base + "/login", timeout=4)
        if code and code != 0:
            print(f"  · 就绪耗时 {time.time() - t0:.1f}s")
            return True
        time.sleep(1.0)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0, help="0=自动挑空闲端口")
    ap.add_argument("--timeout", type=float, default=240.0, help="等待就绪上限（秒）")
    ap.add_argument("--keep", action="store_true", help="保留临时数据目录便于排查")
    ap.add_argument("--dist", default=str(DIST),
                    help="后端产物目录（默认 build/backend-dist；装机后可指向 "
                         "<安装目录>/resources/backend 做安装后验收）")
    args = ap.parse_args()

    dist = Path(args.dist).resolve()
    exe = _backend_exe(dist)
    if not exe:
        print(f"✗ 未找到打包产物：{dist}（先跑 npm run build:backend）", file=sys.stderr)
        return 2

    port = args.port or _free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = Path(tempfile.mkdtemp(prefix="chatx-smoke-"))
    (data_dir / "config").mkdir(parents=True, exist_ok=True)

    # 与 backend-launcher.js 的发布态注入保持同款，冒烟才算「打的是真实姿势」
    env = dict(os.environ)
    env.update({
        "AITR_DESKTOP_MODE": "1",
        "AITR_WEB_HOST": "127.0.0.1",
        "AITR_WEB_PORT": str(port),
        "AITR_WEB_TOKEN": TOKEN,
        "AITR_DATA_DIR": str(data_dir),
        "AITR_CONFIG_PATH": str(data_dir / "config" / "config.yaml"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "HOST_ALERT_SILENT": "1",
    })

    log_path = data_dir / "smoke-backend.out.log"
    print(f"→ 起后端：{exe}\n  port={port} dataDir={data_dir}")
    with open(log_path, "wb") as logf:
        proc = subprocess.Popen(
            [str(exe)], cwd=str(data_dir), env=env,
            stdout=logf, stderr=subprocess.STDOUT,
        )
        try:
            if not _wait_ready(base, args.timeout, proc):
                print(f"✗ {args.timeout:.0f}s 内未就绪；日志：{log_path}", file=sys.stderr)
                return 2

            jar = http.cookiejar.CookieJar()
            opener = _make_opener(jar)
            failures: list[str] = []

            def check(tag, path, want, needle):
                code, body = _get(opener, base + path)
                ok = code == want and (needle is None or needle in body)
                print(f"  {'✓' if ok else '✗'} [{tag}] {path:<34} {code}"
                      + ("" if needle is None else f"  needle={'hit' if needle in body else 'MISS'}"))
                if not ok:
                    head = body[:200].replace("\n", " ")
                    failures.append(f"[{tag}] {path} → {code}（期望 {want}）{head}")

            print("· 匿名矩阵")
            for path, want, needle in ANON_MATRIX:
                check("anon", path, want, needle)

            print("· 登录")
            code, _ = _post_form(opener, base + "/login", {"auth_token": TOKEN})
            print(f"  {'✓' if code in (200, 302, 303) else '✗'} POST /login → {code}")
            if code not in (200, 302, 303):
                failures.append(f"POST /login → {code}")

            _force_full_ui_mode(opener, jar, base)

            print("· 鉴权矩阵（完整模式）")
            for path, want, needle in AUTH_MATRIX:
                check("auth", path, want, needle)

            print("· 结构化断言")
            import json as _json
            for path, desc, validator in JSON_CHECKS:
                code, body = _get(opener, base + path)
                if code != 200:
                    print(f"  ✗ [json] {path:<40} {code}")
                    failures.append(f"[json] {path} → {code}（期望 200）")
                    continue
                try:
                    why = validator(_json.loads(body))
                except Exception as e:  # noqa: BLE001
                    why = f"响应不是合法 JSON（{type(e).__name__}）"
                print(f"  {'✓' if not why else '✗'} [json] {path:<40} {desc}"
                      + (f" —— {why}" if why else ""))
                if why:
                    failures.append(f"[json] {path}（{desc}）: {why}")

            print("· 按路径加载的瘦模块")
            why = check_packaged_fingerprint(dist)
            print(f"  {'✓' if not why else '✗'} [pkg] platform/licensing 机器指纹"
                  + (f" —— {why}" if why else ""))
            if why:
                failures.append(f"[pkg] platform/licensing: {why}")

            print()
            if failures:
                print(f"✗ 冒烟失败 {len(failures)} 项：", file=sys.stderr)
                for f in failures:
                    print("   - " + f, file=sys.stderr)
                print(f"  后端日志：{log_path}", file=sys.stderr)
                return 1
            print("✓ 安装版后端冒烟全过")
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
            if not args.keep:
                import shutil
                shutil.rmtree(data_dir, ignore_errors=True)
            else:
                print(f"  （--keep）临时数据目录保留：{data_dir}")


if __name__ == "__main__":
    raise SystemExit(main())
