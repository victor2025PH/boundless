# -*- coding: utf-8 -*-
"""关键子服务进程级守护：进程崩溃/消失时自动拉起，带退避、熔断与告警。

与 service_manager.py 的区别：本模块在 Hub 进程内运行，只盯对话主链路的关键服务
（fish_tts / stt / lipsync），由 health_monitor 的探测结果驱动，单卡环境下安全去重启动。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import app_config
BASE_DIR = app_config.BASE
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

# conda envs 根目录：由当前解释器(...\envs\facefusion\python.exe)反推
_ENVS_ROOT = Path(sys.executable).resolve().parents[1]


def _py(env_name: str) -> str:
    return str(_ENVS_ROOT / env_name / "python.exe")


# 受守护的关键服务：script 相对 BASE_DIR，env 为 conda 环境名，port 用于存活探测
# 实时视频对话主链路：克隆音(fish_tts) + 识别(stt) + 口型(lipsync) + 广播中枢(vcam)
#
# 注意按运行模式裁剪：在「本地真实摄像头实时换脸」模式下——
#   · lipsync 不参与（口型来自真实摄像头，不需 TTS 驱动静态头像）；
#   · vcam_server 与 realtime_stream 争用同一个 OBS 虚拟摄像头输出口，必崩。
# 因此两者改为环境变量可关（默认仍开，保持其它模式行为不变）：
#   HUB_SUP_LIPSYNC=0  关闭 lipsync 守护
#   HUB_SUP_VCAM=0     关闭 vcam 守护（本地换脸模式建议设 0）
SUPERVISED: dict[str, dict] = {
    "fish_tts": {"script": "fish_speech_server.py", "env": "fishspeech",  "port": app_config.port("fish_tts") or 7855},
    "stt":      {"script": "stt_server.py",         "env": "cosytts",     "port": app_config.port("stt") or 7854},
}
if os.environ.get("HUB_SUP_LIPSYNC", "1") == "1":
    SUPERVISED["lipsync"] = {"script": "lipsync_server.py", "env": "musethepeak", "port": app_config.port("lipsync") or 8090}
# 换脸引擎守护按拓扑二选一：
#   生产拓扑（SVC_FACESWAP 指向远端 .104）：本机 8003 常驻瘦身容灾副本
#     (无 GFPGAN/CodeFormer、LRU=2，~2.2GB)。主引擎失联时 Hub /faceswap 熔断改道至此——
#     副本自身崩了也由本守护拉回，保证"备胎常在"。HUB_SUP_FACESWAP2=0 可关（省显存）。
#   单机拓扑（客户安装，SVC_FACESWAP 未设）：主引擎(8000)就在本机，直接守护它——
#     否则「真人换脸」模式核心服务没人拉起，控制台恒红「换脸服务未启动」
#     （2026-07-12 在 198 Lite 实锤）。此时不再另起 8003 副本：小显存卡上
#     主+副本双份换脸模型纯属浪费，单机也无 failover 意义。
if os.environ.get("SVC_FACESWAP"):
    if os.environ.get("HUB_SUP_FACESWAP2", "1") == "1":
        _fs2_port = app_config.port("faceswap2") or 8003
        SUPERVISED["faceswap2"] = {"script": "faceswap_api.py", "env": "facefusion", "port": _fs2_port,
                                   "no_stale_kill": True,   # 与主实例同脚本：按脚本名清僵尸会误杀，禁用
                                   "env_extra": {"FACESWAP_PORT": str(_fs2_port), "FACESWAP_LOAD_ENHANCE": "0",
                                                 "FACESWAP_MODEL_LRU": "2", "FACESWAP_ENH_CONCURRENCY": "1",
                                                 # 副本恒 inswapper 轻量核：不因继承 SWAP_PRESET=hd 而载 HyperSwap(省显存/秒接管)
                                                 "FACESWAP_HD_CORE": "0"}}
elif os.environ.get("HUB_SUP_FACESWAP", "1") == "1":
    SUPERVISED["faceswap"] = {"script": "faceswap_api.py", "env": "facefusion",
                              "port": app_config.port("faceswap") or 8000,
                              "no_stale_kill": True}   # 防误杀其它同脚本实例（如手动起的副本）
if os.environ.get("HUB_SUP_VCAM", "1") == "1":
    SUPERVISED["vcam"] = {"script": "vcam_server.py", "env": "facefusion",
                          "port": app_config.port("vcam") or 7870}
# 手机监听中继(PC音频+字幕→手机)：轻量、无 GPU，崩了自动拉起。HUB_SUP_MONITOR=0 可关。
if os.environ.get("HUB_SUP_MONITOR", "1") == "1":
    SUPERVISED["monitor"] = {"script": "monitor_relay.py", "env": "facefusion",
                             "port": app_config.port("monitor") or 7878}

# ── 外部仓服务（自带 repo + 独立 conda env，不在 app_config.SERVICES，理由见 app_config.DITTO_DIR）──
#   由本守护统一编排：launch_any/stop_any 复用、_launch 认 dir、GPU 总开关纳管。
#   gpu/core 供 Hub 总开关排序/过滤(core=False → engage?core 不拉起，engage?all 才拉)。
EXTERNAL_SERVICES: dict[str, dict] = {
    "ditto": {"script": "ditto_server.py", "env": "ditto", "port": 8096,
              "dir": str(app_config.DITTO_DIR), "gpu": True, "core": False,
              "label": "实时全脸数字人 (Ditto·512 高清)"},
    # EchoMimic：离线高清全脸(realtime=False)。同形纳管(launch_any/stop_any/GPU 总开关释放)，
    #   但**不自愈/不随 engage(all) 自启**(冷启 60-120s + 多 G 显存)——按需经 /api/engine/start 预热。
    "echomimic": {"script": "echomimic_server.py", "env": "echomimic", "port": 8095,
                  "dir": str(app_config.ECHOMIMIC_DIR), "gpu": True, "core": False,
                  "label": "全脸数字人 (EchoMimic·离线高清512)"},
}
# Ditto 自愈（默认关）：HUB_SUP_DITTO=1 → 纳入守护循环，让「实时高清默认」常在自愈；
#   默认关=按需(start_ditto.bat / GPU 启用 all)启动，不常占 ~5G 显存(尊重多机共卡)。
if os.environ.get("HUB_SUP_DITTO", "0") == "1":
    SUPERVISED["ditto"] = dict(EXTERNAL_SERVICES["ditto"])
# EchoMimic 自愈（默认关，且**不建议**开）：它是离线出片引擎，常驻自愈无意义且占显存；
#   仅为与 ditto 对称保留开关。正常用法是按需 /api/engine/start 预热、用完 /api/engine/stop 释放。
if os.environ.get("HUB_SUP_ECHOMIMIC", "0") == "1":
    SUPERVISED["echomimic"] = dict(EXTERNAL_SERVICES["echomimic"])

# 退避与熔断参数（可被环境变量覆盖）
_RESTART_GRACE_SEC = int(os.environ.get("SUP_RESTART_GRACE", "90"))   # 启动后给多久就绪宽限
_BACKOFF_BASE_SEC = int(os.environ.get("SUP_BACKOFF_BASE", "20"))     # 退避基数
_BACKOFF_MAX_SEC = int(os.environ.get("SUP_BACKOFF_MAX", "300"))      # 退避上限
_MAX_ATTEMPTS = int(os.environ.get("SUP_MAX_ATTEMPTS", "4"))          # 熔断阈值（窗口内）
_ATTEMPT_WINDOW_SEC = int(os.environ.get("SUP_ATTEMPT_WINDOW", "900"))  # 熔断统计窗口

# 运行态：service -> {attempts:[ts...], last_attempt, launching_until, pid, last_error, tripped}
_state: dict[str, dict] = {}


def _st(name: str) -> dict:
    if name not in _state:
        _state[name] = {"attempts": [], "last_attempt": 0.0, "launching_until": 0.0,
                        "pid": 0, "last_error": "", "tripped": False, "restarts": 0}
    return _state[name]


def _port_alive(port: int, timeout: float = 1.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0


import urllib.parse as _urlparse

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1", ""}
_local_ip_cache: set | None = None


def _local_ips() -> set:
    global _local_ip_cache
    if _local_ip_cache is not None:
        return _local_ip_cache
    ips = set(_LOCAL_HOSTS)
    try:
        host = socket.gethostname()
        ips.add(host.lower())
        for info in socket.getaddrinfo(host, None):
            ips.add(str(info[4][0]).lower())
    except Exception:
        pass
    _local_ip_cache = ips
    return ips


def _is_offloaded(name: str) -> bool:
    """该服务是否已通过 SVC_<NAME> 迁到远端机器（如 fish_tts/stt → 4090）。
    迁移后本机不再守护它，避免本地重复拉起抢 5090 显存、破坏分机。"""
    url = os.environ.get("SVC_" + name.upper(), "").strip()
    if not url:
        return False
    try:
        host = (_urlparse.urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host not in _local_ips()


def _recent_attempts(st: dict) -> int:
    cutoff = time.time() - _ATTEMPT_WINDOW_SEC
    st["attempts"] = [t for t in st["attempts"] if t >= cutoff]
    return len(st["attempts"])


def _backoff_ok(st: dict) -> bool:
    """退避：连续尝试间隔随次数指数增长，未到间隔则跳过本次。"""
    n = len(st["attempts"])
    if n == 0:
        return True
    wait = min(_BACKOFF_BASE_SEC * (2 ** (n - 1)), _BACKOFF_MAX_SEC)
    return time.time() - st["last_attempt"] >= wait


def _kill_stale(cfg: dict) -> int:
    """端口已确认 down 时，清掉所有跑同一脚本的残留进程（它们没在服务，只白占显存）。
    根因修复：避免「端口丢了但进程没死→反复重启」累积僵尸实例（Fish-Speech 等重模型尤甚，
    历史上曾累积 5 个实例占满显存）。仅在 _launch（端口确认 down）时调用，故同脚本进程必为僵尸。"""
    script_name = Path(cfg["script"]).name
    killed = 0
    try:
        import psutil
    except Exception:
        return 0
    me = os.getpid()
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if "python" not in ((p.info.get("name") or "").lower()):
                continue
            if p.pid == me:
                continue
            if script_name in " ".join(p.info.get("cmdline") or []):
                p.kill()
                killed += 1
        except Exception:
            continue
    if killed:
        time.sleep(1.2)   # 给显存回收留点时间，避免新实例 OOM
    return killed


def _launch(name: str, cfg: dict, st: dict) -> bool:
    py = _py(cfg["env"])
    base = Path(cfg["dir"]) if cfg.get("dir") else BASE_DIR   # 外部仓服务用其自带 repo 目录(cwd+脚本)
    script = str(base / cfg["script"])
    if not Path(py).exists():
        st["last_error"] = f"python 不存在: {py}"
        return False
    if not Path(script).exists():
        st["last_error"] = f"脚本不存在: {script}"
        return False
    # S6: 多实例脚本（faceswap2 与 faceswap 同脚本不同端口）跳过按脚本名清僵尸——
    #   否则拉副本会误杀本机主实例（单机模式 8000 也跑 faceswap_api.py）。端口去重由调用方保证。
    if not cfg.get("no_stale_kill"):
        _stale = _kill_stale(cfg)   # 启动前清残留同脚本僵尸进程，防显存累积
        if _stale:
            st["last_cleaned"] = _stale
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    for _k, _v in (cfg.get("env_extra") or {}).items():   # S6: 按服务注入实例参数(端口/LRU/瘦身开关)
        env[str(_k)] = str(_v)
    # 端口覆盖层(两套安装并存)：把生效端口注入子进程认的 <SVC>_PORT env——子脚本零改动整段平移。
    # setdefault：env_extra/外部显式指定仍最优先；零配置时 port_env_extra 恒空(零回归)。
    for _k, _v in app_config.port_env_extra(name).items():
        env.setdefault(_k, _v)
    # 抑制 PyTorch/OpenMP 空闲自旋：重模型常驻服务即使没有请求，OpenMP/MKL 线程默认
    # 会忙等(spin-wait)空转，单机多服务叠加可烧掉数个 CPU 核 → 桌面/打字全局卡顿。
    # PASSIVE + KMP_BLOCKTIME=0 让线程做完即休眠，空闲 CPU 从 ~250% 降到接近 0，不影响推理吞吐。
    env.setdefault("OMP_WAIT_POLICY", "PASSIVE")
    env.setdefault("KMP_BLOCKTIME", "0")
    try:
        log_fp = open(LOGS_DIR / f"sup_{name}.log", "a", encoding="utf-8")
        # 脱离 Hub 进程组，避免 Hub 退出时被连带杀死；隐藏窗口
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS \
                | subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen([py, script], stdout=log_fp, stderr=subprocess.STDOUT,
                                cwd=str(base), env=env, creationflags=flags)
        st["pid"] = proc.pid
        st["last_error"] = ""
        return True
    except Exception as e:
        st["last_error"] = f"{type(e).__name__}: {e}"
        return False


def ensure_up(name: str) -> dict:
    """关键服务掉线时尝试拉起；返回本次动作结果。幂等、带退避与熔断。"""
    cfg = SUPERVISED.get(name)
    if not cfg:
        return {"service": name, "action": "skip", "reason": "未纳入守护"}
    if _is_offloaded(name):
        return {"service": name, "action": "skip", "reason": "已迁远端(SVC_*)，本机不守护"}
    # 档位感知：运行环境未安装（Lite 不含克隆音/STT/口型）→ 静默跳过。
    # 不计尝试、不退避、不熔断——否则每个监控周期都产出 tripped 告警刷屏
    # （2026-07-12 在 198 Lite 装机实锤：[Sup] fish_tts/stt 每 20s 两条 WARNING 无休止）。
    # 环境装好后（向导补装）无需重启即自动恢复守护。
    if cfg.get("env") and not app_config.env_installed(cfg["env"]):
        return {"service": name, "action": "skip", "reason": "运行环境未安装（档位不含）"}
    st = _st(name)
    now = time.time()

    # 端口已就绪 → 无需动作（顺带清理熔断/启动宽限）
    if _port_alive(cfg["port"]):
        st["launching_until"] = 0.0
        if st["tripped"]:
            st["tripped"] = False
            st["attempts"] = []
        return {"service": name, "action": "noop", "reason": "已在线"}

    # 上次刚拉起，还在就绪宽限期 → 等待，不重复启动
    if now < st["launching_until"]:
        return {"service": name, "action": "waiting",
                "reason": f"启动宽限中（剩 {int(st['launching_until']-now)}s）"}

    # 熔断：窗口内尝试过多，停止自动拉起，等待人工
    if _recent_attempts(st) >= _MAX_ATTEMPTS:
        st["tripped"] = True
        return {"service": name, "action": "tripped",
                "reason": f"{_ATTEMPT_WINDOW_SEC}s 内已尝试 {_MAX_ATTEMPTS} 次，熔断待人工",
                "last_error": st["last_error"]}

    # 退避：未到下次允许时间
    if not _backoff_ok(st):
        return {"service": name, "action": "backoff", "reason": "退避等待中"}

    # 执行拉起
    st["attempts"].append(now)
    st["last_attempt"] = now
    ok = _launch(name, cfg, st)
    if ok:
        st["launching_until"] = now + _RESTART_GRACE_SEC
        st["restarts"] += 1
        return {"service": name, "action": "launched", "pid": st["pid"],
                "attempt": len(st["attempts"])}
    return {"service": name, "action": "launch_failed", "error": st["last_error"]}


def force_reset(name: str = "") -> dict:
    """人工清除熔断/退避状态（修复后调用）。"""
    targets = [name] if name else list(SUPERVISED.keys())
    for t in targets:
        st = _st(t)
        st["attempts"] = []
        st["tripped"] = False
        st["launching_until"] = 0.0
    return {"ok": True, "reset": targets}


def launch_any(name: str) -> dict:
    """按 app_config.SERVICES 启动任意本机服务（detached，复用 _launch 的脱离/清残留/env）。
    供 Hub 的「GPU 总开关·启用」用。hub 自身与已迁远端(SVC_*)的服务不在本机启动。"""
    s = app_config.SERVICES.get(name) or EXTERNAL_SERVICES.get(name)   # 含外部仓服务(ditto)
    if not s:
        return {"service": name, "ok": False, "reason": "未知服务"}
    if name == "hub":
        return {"service": name, "ok": False, "reason": "hub 不在编排范围"}
    if _is_offloaded(name):
        return {"service": name, "ok": False, "reason": "已迁远端(SVC_*)，本机不启动"}
    if _port_alive(s["port"]):
        return {"service": name, "ok": True, "reason": "已在线"}
    st = _st(name)
    cfg = {"script": s["script"], "env": s["env"], "port": s["port"], "dir": s.get("dir"),
           "env_extra": s.get("env_extra"),
           # 同脚本多实例（faceswap2）：禁止按脚本名清僵尸，防误杀另一实例
           "no_stale_kill": bool(s.get("env_extra", {}).get("FACESWAP_PORT"))}
    ok = _launch(name, cfg, st)
    return {"service": name, "ok": ok, "pid": st.get("pid", 0),
            "error": "" if ok else st.get("last_error", "")}


def stop_any(name: str) -> dict:
    """停止任意本机服务（按脚本名杀全部实例，复用 _kill_stale）。供「GPU 总开关·释放」用。
    hub 自身受保护；已迁远端的不动（那是另一台机器在跑）。"""
    s = app_config.SERVICES.get(name) or EXTERNAL_SERVICES.get(name)   # 含外部仓服务(ditto)
    if not s:
        return {"service": name, "ok": False, "reason": "未知服务"}
    if name == "hub":
        return {"service": name, "ok": False, "reason": "hub 受保护，不停"}
    if _is_offloaded(name):
        return {"service": name, "ok": False, "reason": "远端服务，本机不停"}
    if s.get("env_extra", {}).get("FACESWAP_PORT"):
        # S6 同脚本多实例：只杀「占本服务端口」的那个进程，绝不按脚本名扫射（会误杀另一实例）
        killed = _kill_port_owner(int(s["port"]))
    else:
        killed = _kill_stale({"script": s["script"]})   # 含 psutil 缺失保护；killed=0 表示无实例/无 psutil
    st = _st(name)
    st["pid"] = 0
    return {"service": name, "ok": True, "killed": killed, "still_alive": _port_alive(s["port"])}


def _kill_port_owner(port: int) -> int:
    """杀掉监听指定端口的进程（S6：faceswap2 与主实例同脚本，只能按端口定位）。"""
    killed = 0
    try:
        import psutil
        for c in psutil.net_connections(kind="tcp"):
            if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN and c.pid:
                try:
                    psutil.Process(c.pid).kill()
                    killed += 1
                except Exception:
                    pass
    except Exception:
        return 0
    if killed:
        time.sleep(1.0)
    return killed


def snapshot() -> dict:
    out = {}
    for name, cfg in SUPERVISED.items():
        st = _st(name)
        out[name] = {
            "port": cfg["port"],
            "alive": _port_alive(cfg["port"]),
            "offloaded": _is_offloaded(name),
            "restarts": st["restarts"],
            "recent_attempts": _recent_attempts(st),
            "tripped": st["tripped"],
            "last_error": st["last_error"],
            "pid": st["pid"],
        }
    return {"ok": True, "supervised": out,
            "envs_root": str(_ENVS_ROOT)}
