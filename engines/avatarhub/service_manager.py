# -*- coding: utf-8 -*-
"""
统一服务编排器 — 一键启动/停止/重启所有子服务
==============================================
功能：
  1. 按依赖顺序启动所有服务（FaceSwap → TTS → AvatarHub）
  2. 健康检查 + 自动重启崩溃的服务
  3. 提供 CLI 和 HTTP API 两种控制方式
  4. 日志持久化到 logs/ 目录

用法：
  python service_manager.py start      # 启动所有
  python service_manager.py stop       # 停止所有
  python service_manager.py restart    # 重启所有
  python service_manager.py status     # 查看状态
  python service_manager.py daemon     # 守护模式（含HTTP API :9999）
"""
import sys, os, io, json, time, signal, threading, subprocess, argparse
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from pathlib import Path
from datetime import datetime
from urllib.request import urlopen

# ── 配置 ────────────────────────────────────────────────────────
import app_config
BASE_DIR = app_config.BASE
LOGS_DIR = BASE_DIR / "logs"
try:
    LOGS_DIR.mkdir(exist_ok=True)
except OSError:
    # 安装目录只读（如被装进 Program Files 且无管理员权限）时绝不能死在 import：
    # 日志退到用户可写目录，让主界面能起来并给出明确指引（launcher 有可写性体检）。
    LOGS_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "AvatarHub" / "logs"
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

# 服务定义：由 app_config.SERVICES（单一真相）派生，避免与启动脚本/中枢清单漂移。
# 历史遗留：本文件原先硬编码的是旧架构(faceswap/tts/hair/tryon/avatarhub)，已不含
# 当前实时对话核心(fish_tts/stt/lipsync/vcam)；改为从清单派生后自动跟随架构演进。
#   required ← core（核心链路）；python ← conda_python(env)；health ← 完整 URL（含远程覆盖）。
def _build_services():
    out = []
    for name, s in app_config.SERVICES.items():
        out.append({
            "name": name,
            "label": s.get("label", name),
            "script": str(BASE_DIR / s["script"]),
            "env": s["env"],
            "python": app_config.conda_python(s["env"]),
            "port": s["port"],
            "health": app_config.health_url(name),
            "delay": s.get("delay", 10),
            "required": s.get("core", False),
        })
    return out

SERVICES = _build_services()

# ── 全局状态 ────────────────────────────────────────────────────
_processes  = {}  # name -> subprocess.Popen
_status     = {}  # name -> {running, pid, last_check, healthy}
_fail_count = {}  # name -> 连续启动失败次数
_running    = True
MAX_FAIL    = 2   # 连续失败超过此次数停止自动重启
PID_FILE    = BASE_DIR / ".service_pids.json"


def _save_pids():
    data = {n: p.pid for n, p in _processes.items() if p.poll() is None}
    try:
        PID_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _load_and_kill_orphans():
    """启动前读取上次PID文件，清理孤儿进程。
    健康在跑的不杀（幂等收养）：用户连点两次「一键开机」时，上次会话拉起的 hub/vcam
    还活得好好的，旧逻辑照单全杀再重启 → 网页控制台断连、掉线告警弹一轮
    （2026-07-12 在 198 实锤：16:13/16:17 两次点击 = 两次全家桶重启 = 「不时提醒出错」）。
    只有对应端口不健康的记录才按孤儿清理。"""
    if not PID_FILE.exists():
        return
    try:
        data = json.loads(PID_FILE.read_text(encoding="utf-8"))
        svc_by_name = {s["name"]: s for s in SERVICES}
        for name, pid in data.items():
            svc = svc_by_name.get(name)
            if svc and health_check(svc["health"]):
                log(f"  上次会话的 {name} (PID={pid}) 仍健康在跑，收养不重启")
                continue
            try:
                subprocess.run(f'taskkill /F /PID {pid}',
                    shell=True, capture_output=True, timeout=5)
                log(f"  清理孤儿进程 {name} PID={pid}")
            except Exception:
                pass
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _cleanup_on_exit():
    """程序退出时清理所有子进程"""
    for name, proc in list(_processes.items()):
        if proc.poll() is None:
            proc.terminate()
    PID_FILE.unlink(missing_ok=True)


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOGS_DIR / "service_manager.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── 端口检查 ────────────────────────────────────────────────────
def is_port_in_use(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0


def kill_port(port: int):
    """杀掉占用指定端口的进程"""
    try:
        result = subprocess.run(
            f'netstat -ano | findstr ":{port} "',
            shell=True, capture_output=True, text=True, timeout=5)
        for line in result.stdout.strip().split('\n'):
            if 'LISTENING' in line:
                pid = line.strip().split()[-1]
                try:
                    subprocess.run(f'taskkill /F /PID {pid}',
                        shell=True, capture_output=True, timeout=5)
                    log(f"  杀掉端口 {port} 占用进程 PID={pid}")
                except Exception:
                    pass
    except Exception:
        pass


def health_check(url: str, timeout: float = 3.0) -> bool:
    # 纯标准库（urllib）：真实探测 /health 返回 200，去掉 requests 依赖，便于打包瘦身。
    try:
        with urlopen(url, timeout=timeout) as r:
            return getattr(r, "status", r.getcode()) == 200
    except Exception:
        return False


# ── 启动单个服务 ────────────────────────────────────────────────
def start_service(svc: dict) -> bool:
    name = svc["name"]
    port = svc["port"]

    # 幂等收养：端口上已有健康服务（上次会话留下 / 手动起的）→ 直接算启动成功，
    # 绝不往下走到「清理端口」把好端端的服务弹掉再重启（连点「一键开机」的骚扰根源）。
    if health_check(svc["health"]):
        log(f"✅ {name} 已在运行（健康），跳过重启")
        _fail_count[name] = 0
        _status[name] = {"running": True, "pid": None, "healthy": True,
                         "last_check": time.time(),
                         "log_file": str(LOGS_DIR / f"{name}.log")}
        return True

    # 失败次数检查
    fails = _fail_count.get(name, 0)
    if fails >= MAX_FAIL:
        log(f"🛑 {name} 已连续失败 {fails} 次，停止重试。"
            f" 请检查显存/依赖后手动重启。")
        return False

    # 检查是否已在运行
    if name in _processes and _processes[name].poll() is None:
        if health_check(svc["health"]):
            log(f"✅ {name} 已在运行 (PID={_processes[name].pid})")
            _fail_count[name] = 0
            return True

    # 检查脚本和 Python 是否存在 —— 必须在「清理端口」之前：
    # 环境缺失时若先 kill_port，会平白杀掉正占用该端口的外部服务（如生产口型标机的 8090），
    # 然后自己又起不来，纯属破坏（1.0.3 在 198 标机实锤过一次）。
    # 注意 python 路径必须现算：SERVICES 在 import 时构建，而首启向导装完环境后才把
    # runtime\envs\<env>\python.exe 写进 config.json——用 import 时的快照会把刚装好的
    # 环境仍判成「未安装」（1.0.4 在 198 实锤：向导装完 hub 拒启，控制台打不开）。
    py = app_config.conda_python(svc["env"]) if svc.get("env") else svc["python"]
    svc["python"] = py
    if getattr(sys, "frozen", False) and Path(py).resolve() == Path(sys.executable).resolve():
        # conda_python() 找不到环境时回退当前解释器；冻结态那是启动器 exe 本身——
        # 拿它当 python 只会重生启动器副本（被单实例守卫秒退）。视为环境未装。
        log(f"❌ {name}: 运行环境未安装（请先在首启向导或「组件」中安装）")
        _fail_count[name] = _fail_count.get(name, 0) + 1
        return False
    if not Path(py).exists():
        log(f"❌ {name}: Python 不存在: {py}")
        return False
    if not Path(svc["script"]).exists():
        log(f"❌ {name}: 脚本不存在: {svc['script']}")
        return False

    # 清理占用端口
    if is_port_in_use(port):
        log(f"⚠️  端口 {port} 被占用，清理中...")
        kill_port(port)
        time.sleep(2)

    # 启动
    log_file = LOGS_DIR / f"{name}.log"
    log(f"🚀 启动 {name} (端口 {port})...")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["COQUI_TOS_AGREED"] = "1"

    try:
        f_log = open(log_file, "a", encoding="utf-8")
        proc = subprocess.Popen(
            [svc["python"], svc["script"]],
            stdout=f_log, stderr=subprocess.STDOUT,
            cwd=str(BASE_DIR), env=env,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        _processes[name] = proc
        _status[name] = {
            "running": True, "pid": proc.pid,
            "healthy": False, "last_check": time.time(),
            "log_file": str(log_file)
        }
        _save_pids()
        log(f"  PID={proc.pid}, 等待 {svc['delay']}s 启动...")
    except Exception as e:
        log(f"❌ {name} 启动失败: {e}")
        _fail_count[name] = _fail_count.get(name, 0) + 1
        return False

    # 等待健康检查
    deadline = time.time() + svc["delay"] + 30
    while time.time() < deadline:
        time.sleep(3)
        if health_check(svc["health"]):
            _status[name]["healthy"] = True
            _fail_count[name] = 0   # 成功后重置计数
            log(f"✅ {name} 启动成功 (PID={proc.pid}, 端口 {port})")
            return True
        if proc.poll() is not None:
            log(f"❌ {name} 进程意外退出 (code={proc.returncode})")
            _fail_count[name] = _fail_count.get(name, 0) + 1
            if _fail_count[name] >= MAX_FAIL:
                log(f"🛑 {name} 已达失败上限({MAX_FAIL}次)。"
                    f" 可能原因: 显存不足 / 依赖缺失 / 脚本错误")
            return False

    log(f"⚠️  {name} 健康检查超时（服务可能仍在加载）")
    return is_port_in_use(port)


def stop_service(name: str):
    if name in _processes:
        proc = _processes[name]
        if proc.poll() is None:
            log(f"🛑 停止 {name} (PID={proc.pid})...")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        del _processes[name]

    # 确保端口释放
    svc = next((s for s in SERVICES if s["name"] == name), None)
    if svc and is_port_in_use(svc["port"]):
        kill_port(svc["port"])

    _status[name] = {"running": False, "pid": None, "healthy": False,
                     "last_check": time.time()}
    log(f"  {name} 已停止")


# ── 批量操作 ────────────────────────────────────────────────────
# 直播同传最小依赖栈(顺序：中枢→推理→同传)
LIVE_STACK = ("hub", "stt", "fish_tts", "lipsync", "vcam", "interpreter")


def start_live_stack() -> dict:
    """按序启动直播同传所需服务；已在线则跳过。返回 {name: ok}。"""
    log("=" * 50)
    log("🎭 启动直播同传链路")
    log("=" * 50)
    results = {}
    for name in LIVE_STACK:
        svc = next((s for s in SERVICES if s["name"] == name), None)
        if not svc:
            results[name] = False
            continue
        if health_check(svc["health"]):
            log(f"✅ {name} 已在线")
            results[name] = True
        else:
            results[name] = start_service(svc)
    return results


def start_all(required_only: bool = False):
    log("=" * 50)
    log("🚀 启动所有服务")
    log("=" * 50)
    _load_and_kill_orphans()   # 清理上次残留孤儿进程
    import atexit
    atexit.register(_cleanup_on_exit)
    results = {}
    for svc in SERVICES:
        if required_only and not svc.get("required", True):
            log(f"⏭️  跳过可选服务: {svc['name']}")
            continue
        # 档位感知：运行环境未装（Lite 无 fishspeech/cosytts/musethepeak）→ 干净跳过。
        # 老行为是硬启 → 起不来 → 汇总表三行 ❌ + 白等启动超时（198 Lite 每次开机实锤）。
        _env = svc.get("env") or ""
        if _env and not app_config.env_installed(_env):
            log(f"⏭️  跳过未安装服务: {svc['name']}（当前版本不含）")
            results[svc["name"]] = None
            continue
        ok = start_service(svc)
        results[svc["name"]] = ok

    log("-" * 50)
    for name, ok in results.items():
        log(f"  {'✅' if ok else ('⏭️ 未安装' if ok is None else '❌')} {name}")
    log("=" * 50)
    return results


def stop_all():
    log("=" * 50)
    log("🛑 停止所有服务")
    log("=" * 50)
    for svc in reversed(SERVICES):
        stop_service(svc["name"])
    log("  所有服务已停止")


def restart_all():
    stop_all()
    time.sleep(3)
    return start_all()


def _probe_one(svc: dict) -> dict:
    """单服务探测：先看端口（瞬时），端口开放才发 HTTP 健康检查。"""
    name = svc["name"]
    proc = _processes.get(name)
    managed = proc is not None and proc.poll() is None
    port_open = is_port_in_use(svc["port"])
    healthy = health_check(svc["health"], timeout=3) if port_open else False
    return {
        "running": managed or port_open,
        "healthy": healthy,
        "managed": managed,
        "pid": proc.pid if managed else None,
        "port": svc["port"],
        "required": svc.get("required", True),
        "label": svc.get("label", name),
    }


def get_status() -> dict:
    # 并发探测所有服务，避免逐个 HTTP/端口检查串行累加（12 个服务从 ~13s 降到 ~2s）。
    from concurrent.futures import ThreadPoolExecutor
    result = {}
    with ThreadPoolExecutor(max_workers=min(12, len(SERVICES))) as ex:
        futs = {svc["name"]: ex.submit(_probe_one, svc) for svc in SERVICES}
        for name, fut in futs.items():
            result[name] = fut.result()
            _status[name] = result[name]
    return result


# ── 守护线程：自动重启 ──────────────────────────────────────────
def watchdog_loop(interval: int = 30):
    """每 interval 秒检查一次，崩溃的 required 服务自动重启"""
    log("🐕 守护线程启动，检查间隔 30s")
    while _running:
        time.sleep(interval)
        for svc in SERVICES:
            if not svc.get("required", True):
                continue
            # 与 start_all 同一档位闸门：环境未装的服务不守护（否则每 30s 空转一次重启）
            if svc.get("env") and not app_config.env_installed(svc["env"]):
                continue
            name = svc["name"]
            proc = _processes.get(name)
            if proc is None or proc.poll() is not None:
                log(f"⚠️  {name} 进程不存在，尝试重启...")
                start_service(svc)
            elif not health_check(svc["health"], timeout=3):
                log(f"⚠️  {name} 健康检查失败，尝试重启...")
                stop_service(name)
                time.sleep(2)
                start_service(svc)


# ── HTTP API (守护模式) ─────────────────────────────────────────
def run_daemon():
    """启动守护模式：HTTP API + 定时健康检查"""
    try:
        from fastapi import FastAPI
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import JSONResponse
        import uvicorn
    except ImportError:
        log("❌ 守护模式需要 fastapi + uvicorn")
        log("   直接进入命令行模式")
        start_all(required_only=True)
        watchdog_loop()
        return

    daemon = FastAPI(title="Service Manager", version="1.0")
    # GPU 服务面加固：9999 能起/杀全部服务，是最高危端口。鉴权放行回环/令牌/白名单；
    # /status 只读放行，/start /stop /restart 等变更操作强制鉴权（启用后）。
    try:
        import service_auth
        enabled = service_auth.secure(daemon, name="service_manager",
                                      open_paths=("/health", "/status"))
        log("🔐 管理API 访问控制: %s" % ("已启用(令牌/白名单)" if enabled else "未启用(仅回环可控，建议设 AVATARHUB_SERVICE_TOKEN)"))
    except Exception as _e:
        from fastapi.middleware.cors import CORSMiddleware
        daemon.add_middleware(CORSMiddleware, allow_origins=["*"],
                             allow_methods=["*"], allow_headers=["*"])
        log("⚠️ service_auth 接入失败，回退无鉴权 CORS:* : %s" % _e)

    @daemon.get("/status")
    def api_status():
        return get_status()

    @daemon.post("/start")
    def api_start():
        return start_all()

    @daemon.post("/stop")
    def api_stop():
        stop_all()
        return {"ok": True}

    @daemon.post("/restart")
    def api_restart():
        return restart_all()

    @daemon.post("/restart/{name}")
    def api_restart_one(name: str):
        svc = next((s for s in SERVICES if s["name"] == name), None)
        if not svc:
            return JSONResponse({"error": f"未知服务: {name}"}, 404)
        stop_service(name)
        time.sleep(2)
        ok = start_service(svc)
        return {"ok": ok, "name": name}

    # 先启动所有服务
    start_all(required_only=True)

    # 启动守护线程
    t = threading.Thread(target=watchdog_loop, daemon=True)
    t.start()

    log(f"🌐 管理API: http://127.0.0.1:9999")
    uvicorn.run(daemon, host="0.0.0.0", port=9999, log_level="warning")


# ── CLI ─────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="服务编排器")
    parser.add_argument("action", nargs="?", default="status",
                        choices=["start", "stop", "restart", "status", "daemon"],
                        help="操作: start|stop|restart|status|daemon")
    parser.add_argument("--required-only", action="store_true",
                        help="只启动必需服务(跳过hair/tryon)")
    args = parser.parse_args()

    if args.action == "start":
        start_all(required_only=args.required_only)
    elif args.action == "stop":
        stop_all()
    elif args.action == "restart":
        restart_all()
    elif args.action == "status":
        st = get_status()
        print("\n服务状态：")
        for name, info in st.items():
            icon = "✅" if info["healthy"] else ("🟡" if info["running"] else "❌")
            if info.get("pid"):
                who = f"PID={info['pid']}"          # 本编排器托管
            elif info["running"]:
                who = "运行中(外部)"                  # 健康但非本进程拉起
            else:
                who = "未运行"
            req = "必需" if info["required"] else "可选"
            print(f"  {icon} {name:12s} 端口={info['port']}  {who:14s}  [{req}]  {info.get('label','')}")
        print()
    elif args.action == "daemon":
        run_daemon()


if __name__ == "__main__":
    main()
