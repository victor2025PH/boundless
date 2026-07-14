# -*- coding: utf-8 -*-
"""port_guard.py — 服务启动前「端口独占」预检（2026-07-06 realtime 双绑事故的通用化防线）

背景：Windows 的 SO_REUSEADDR 语义与 POSIX 不同——**两个进程可同时 bind+listen 同一端口**
（实测第二次 bind 静默成功），而 uvicorn 的 bind_socket 默认就开 SO_REUSEADDR。后果是
「静默双实例」：请求在两实例间不确定分发、状态/控制串线，比崩溃更难查——2026-07-06 soak
首跑即因 realtime_stream 双绑 8080 而整场作废（路线图 06m）。realtime_stream 已改独占绑定；
本模块把同一防线给到 uvicorn 系服务（hub/vcam 等）：**起服务前先独占探测，占用即拒绝上线**。

用法（uvicorn.run 之前一行）：
    import port_guard
    port_guard.ensure_port_free(9000, "avatar_hub")

语义：
- 端口空闲 → 静默返回 True；
- 已被占用 → 打印占用进程（netstat 反查 best-effort）后 sys.exit(2)——fail-fast，绝不带病上线；
- PORT_GUARD_SKIP=1 → 跳过预检（确需并行调试的逃生门）。

探测原理：SO_EXCLUSIVEADDRUSE 试绑（Windows）/ 无 REUSE 裸 bind（POSIX）——即使现有监听者
开着 REUSE 也能探出（WinError 10048）。探测 socket 立即关闭，与随后真实 bind 之间的竞态窗口
仅数毫秒，防的是「长期双实例」而非 TOCTOU。
"""
import os
import socket
import subprocess
import sys


def _probe_exclusive(host: str, port: int):
    """独占试绑一次。返回 None=空闲；OSError=被占。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind((host, port))
        return None
    except OSError as e:
        return e
    finally:
        try:
            s.close()
        except Exception:
            pass


def _who_owns(port: int) -> str:
    """best-effort 反查占用进程。psutil 优先（纯 API；06t 实测 netstat 在 detached
    无窗口宿主里会返回 0 字节），缺席才退 netstat/tasklist 解析（仅 Windows）。"""
    try:
        import psutil
        for c in psutil.net_connections("tcp"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid:
                name = ""
                try:
                    name = psutil.Process(c.pid).name()
                except Exception:
                    pass
                return f"pid={c.pid}" + (f" ({name})" if name else "")
        return ""
    except Exception:
        pass
    if os.name != "nt":
        return ""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                             capture_output=True, text=True, timeout=5).stdout or ""
        for ln in out.splitlines():
            parts = ln.split()
            if len(parts) >= 5 and parts[-2] == "LISTENING" and parts[1].endswith(f":{port}"):
                pid = parts[-1]
                name = ""
                try:
                    q = subprocess.run(["tasklist", "/fi", f"PID eq {pid}", "/fo", "csv", "/nh"],
                                       capture_output=True, text=True, timeout=5).stdout or ""
                    if q.strip() and "," in q:
                        name = q.split(",")[0].strip().strip('"')
                except Exception:
                    pass
                return f"pid={pid}" + (f" ({name})" if name else "")
    except Exception:
        pass
    return ""


def ensure_port_free(port: int, service: str = "service", host: str = "0.0.0.0") -> bool:
    """端口空闲→True；被占→打印诊断并 sys.exit(2)；PORT_GUARD_SKIP=1→跳过。"""
    if os.environ.get("PORT_GUARD_SKIP") == "1":
        print(f"[PortGuard] PORT_GUARD_SKIP=1 → 跳过 {service}:{port} 预检", flush=True)
        return True
    err = _probe_exclusive(host, port)
    if err is None:
        return True
    print(f"[PortGuard] {service} 端口 {port} 已被占用: {err}", flush=True)
    owner = _who_owns(port)
    if owner:
        print(f"[PortGuard] 占用者: {owner}", flush=True)
    print("[PortGuard] 拒绝启动第二实例——Windows REUSE 双绑会静默串线(请求两边乱跳)。"
          "确认旧实例后再起,或换端口/设 PORT_GUARD_SKIP=1。", flush=True)
    sys.exit(2)


def selftest() -> dict:
    """双绑场景真演练（127.0.0.1 临时端口,不碰生产）：
    REUSE 监听者在场 → 必须探出占用；释放后 → 必须放行。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(1)
    try:
        occupied = _probe_exclusive("127.0.0.1", port) is not None
    finally:
        srv.close()
    freed = _probe_exclusive("127.0.0.1", port) is None
    checks = {"REUSE监听者在场→探出占用": occupied, "释放后→放行": freed}
    return {"ok": all(checks.values()), "checks": checks}


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    r = selftest()
    print(r)
    sys.exit(0 if r["ok"] else 1)
