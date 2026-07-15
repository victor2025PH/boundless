# -*- coding: utf-8 -*-
"""
内存看门狗 mem_watchdog.py  (v2)
================================
独立监控进程：盯住整机内存 + 显存 + 各 AI 服务进程占用，在吃紧时分级处置，
防止再次出现「内存被吃爆 → Windows 疯狂压缩内存 → 全局卡顿（连打字都卡）」。

v2 相比 v1 的改进（本次阶段优化）：
  1. 监控显存(VRAM)并记录，趋势一目了然。
  2. 软/硬双阈值：软阈值只告警(提前发现趋势)，硬阈值才重启(最后手段)。
  3. 泄漏趋势检测：某服务提交内存持续单调上涨 → 标记「疑似泄漏」提前预警。
  4. 优雅自愈钩子：若服务暴露 /gc 端点(下一阶段加)，先调 /gc 释放，无效再重启。
  5. 更清晰的日志与一行式状态摘要，落 logs\mem_watchdog.log。

用法：
  python mem_watchdog.py                 # 前台常驻（Ctrl+C 退出）
  python mem_watchdog.py --once          # 只检查一次并打印
  python mem_watchdog.py --dry-run       # 只告警/不真正重启
环境变量（可选）：
  MEMWD_INTERVAL=20         采样间隔秒
  MEMWD_HARD_PCT=88         系统内存使用率(%)≥此值 → 硬处置(重启占用最高者)
  MEMWD_SOFT_PCT=80         ≥此值 → 软告警
  MEMWD_MIN_AVAIL_GB=4      可用内存(GB)≤此值 → 硬处置
  MEMWD_COOLDOWN=300        同一服务两次重启的最小间隔(秒)
  MEMWD_TRY_GC=0            1=重启前先尝试调用服务 /gc 端点(需服务支持)

依赖：psutil（facefusion 环境通常已自带）。
推荐：C:\\Users\\user\\Miniconda3\\envs\\facefusion\\python.exe mem_watchdog.py
"""
import os
import re
import sys
import time
import shutil
import argparse
import subprocess
from datetime import datetime
from pathlib import Path
from collections import deque, defaultdict

# 隐藏子进程控制台：nvidia-smi/powershell/ssh 等周期性调用若不加此标志，会在屏幕上
# 反复闪现黑色控制台窗口（每个巡检间隔一次），非常扰人。仅 Windows 有该标志。
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

try:
    import psutil
except ImportError:
    print("[mem_watchdog] 需要 psutil：pip install psutil")
    print("  例: C:\\Users\\user\\Miniconda3\\envs\\facefusion\\python.exe -m pip install psutil")
    sys.exit(1)

try:
    import urllib.request
except Exception:
    urllib = None

import app_config
BASE_DIR = app_config.BASE
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
LOG_FILE = LOGS_DIR / "mem_watchdog.log"
# 自管日志轮转：单文件曾无界长到 12MB+。超额即滚动，保留近 KEEP 份(默认 3×20MB)。
LOG_MAX_BYTES = int(os.environ.get("MEMWD_LOG_MAX_MB", "20")) * 1024 * 1024
LOG_KEEP = max(1, int(os.environ.get("MEMWD_LOG_KEEP", "3")))

# 跨机服务面鉴权巡检：每 N 秒跑一次 harden_remote.ps1 -Mode verify（他机无令牌应 401）。
# 退出码=1 即有服务"裸奔"→ 告警；若启用自愈则自动 deploy 后复验。
# 异常时切高频巡检(MEMWD_AUDIT_FAST_SEC)，恢复后回到 MEMWD_AUDIT_SEC。设 0 关闭。
AUDIT_EVERY_SEC = int(os.environ.get("MEMWD_AUDIT_SEC", "1800"))
AUDIT_FAST_SEC = int(os.environ.get("MEMWD_AUDIT_FAST_SEC", "120"))
AUDIT_HEAL_ENABLED = os.environ.get("MEMWD_AUTH_HEAL", "1") != "0"
AUDIT_HEAL_COOLDOWN = int(os.environ.get("MEMWD_AUTH_HEAL_COOLDOWN", "600"))
AUDIT_HEAL_TIMEOUT = int(os.environ.get("MEMWD_AUTH_HEAL_TIMEOUT", "300"))
# 连续自愈失败达此次数 → 升级 critical 告警(@人)，提醒"自愈也救不回，需人工"。
AUDIT_HEAL_FAIL_ESCALATE = int(os.environ.get("MEMWD_HEAL_FAIL_ESCALATE", "2"))
# 自愈熔断：连续失败达 MAX_RETRY 次 → 打开熔断器，停手 BREAKER_COOLDOWN 秒（默认6h）不再反复 deploy。
# （历史教训：远端机长期离线时，deploy 永远 exit=1，看门狗曾空转累计 1100+ 次。）
# 熔断冷却到点后半开——只放行一次试探，仍失败则重新熔断，把无谓重试压到极低频。
AUDIT_HEAL_MAX_RETRY = int(os.environ.get("MEMWD_HEAL_MAX_RETRY", "6"))
AUDIT_HEAL_BREAKER_COOLDOWN = int(os.environ.get("MEMWD_HEAL_BREAKER_COOLDOWN", str(6 * 3600)))
_AUTH_AUDIT_PS = BASE_DIR / "harden_remote.ps1"
_STATUS_FILE = LOGS_DIR / "watchdog_status.json"
# 令牌保养提醒：service_token 超龄(默认>90天)时随巡检每日主动叫一声(notify_event 事件，
# 非状态化告警——超龄是"该保养"不是"失守"，不进 /ops 活动告警、不污染安全总判定)。
_TOKEN_FILE = BASE_DIR / "secrets" / "service_token.txt"
_TOKEN_STALE_DAYS = int(os.environ.get("MEMWD_TOKEN_STALE_DAYS", "90"))
_TOKEN_REMIND_STAMP = LOGS_DIR / "token_stale_notified.txt"
# 火警演习互斥：drill 期间写此标记，看门狗本轮鉴权巡检让路（避免复原 deploy 竞态）。
# 带 TTL：即使 drill 崩溃残留标记，超时后看门狗自动恢复巡检，不会被永久抑制。
_DRILL_FLAG = LOGS_DIR / "drill_active.flag"
_DRILL_TTL = int(os.environ.get("MEMWD_DRILL_TTL", "600"))
_last_audit = 0.0
_last_heal_attempt = 0.0
_heal_consec_fail = 0
_heal_breaker_until = 0.0        # >now 时熔断器打开：停止自动 deploy，等待半开试探/人工

# 远程服务健康自愈：只巡检 env_config 中明确 offload 的 SVC_* URL。
# 连续失败才 SSH 重启远端计划任务，避免一次 LAN 抖动误杀 GPU 服务。
REMOTE_HEALTH_SEC = int(os.environ.get("MEMWD_REMOTE_HEALTH_SEC", "30"))
REMOTE_FAIL_THRESHOLD = int(os.environ.get("MEMWD_REMOTE_FAIL_THRESHOLD", "3"))
REMOTE_HEAL_COOLDOWN = int(os.environ.get("MEMWD_REMOTE_HEAL_COOLDOWN", "180"))
REMOTE_SSH_USER = os.environ.get("MEMWD_REMOTE_USER", "Administrator")
_last_remote_health = 0.0
_remote_fail_count = defaultdict(int)          # service -> consecutive failures
_remote_last_heal = defaultdict(float)         # service -> last SSH restart ts
_remote_was_down = set()                       # services currently in alert state
# 服务名→远端计划任务名。以 cluster_map.json(单一数据源)为准，启动时加载；
# 静态表仅作 cluster_map 缺失时的兜底。2026-07-05 修复：旧硬编码("STT"/"EmotionTTS")与
# 远端实际任务名("STT_Boot"/"EmotionTTS_Boot")不符 → SSH 自愈静默空转(SilentlyContinue 吞错)。
_REMOTE_TASKS = {
    "stt": "STT_Boot",
    "faceswap": "FaceSwap_Boot",
    "emotion_tts": "EmotionTTS_Boot",
    "nemo_stt": "NemoSTT_Boot",
}
# 服务名→远端入口脚本名。自愈时按命令行强杀"进程活着但端口已死"的僵尸（端口 kill 抓不到它——
# 2026-07-08 .117 qwen3_tts 网络抖动后 accept 循环死掉、进程仍活却不监听 7858 的实测事故）。
# 同以 cluster_map.json(svcs[].server) 为准，静态表兜底。
_REMOTE_SCRIPTS = {
    "stt": "stt_server.py",
    "faceswap": "faceswap_api.py",
    "emotion_tts": "emotion_tts_server.py",
    "nemo_stt": "nemotron_stt_server.py",
    "qwen3_tts": "qwen3_tts_server.py",
}

def _load_remote_tasks_from_cluster_map():
    try:
        import json as _json
        cm = _json.loads((BASE_DIR / "cluster_map.json").read_text(encoding="utf-8"))
        for hcfg in (cm.get("hosts") or {}).values():
            for svc in hcfg.get("svcs") or []:
                if svc.get("name") and svc.get("task"):
                    _REMOTE_TASKS[svc["name"]] = svc["task"]
                if svc.get("name") and svc.get("server"):
                    _REMOTE_SCRIPTS[svc["name"]] = svc["server"]
    except Exception as e:  # 模块导入期,log() 尚未定义 → stderr 兜底,绝不让守护进程起不来
        print(f"[memwd] cluster_map.json 读取失败,远端任务名用内置兜底表: {e}", file=sys.stderr)

_load_remote_tasks_from_cluster_map()


def _drill_active():
    try:
        if _DRILL_FLAG.exists() and (time.time() - _DRILL_FLAG.stat().st_mtime) < _DRILL_TTL:
            return True
    except Exception:
        pass
    return False

# GPU 总开关「已释放」标记：avatar_hub 在释放显存时写此文件、占用时删除。看门狗据此让路——
# 释放期间不复活任何 GPU 服务（把整张卡让给另一台机器），仅保留 avatar_hub 守护(编排中枢、非 GPU)。
# 刻意无 TTL：这是用户的显式长期状态(不像 drill 那种短临时态)；engage 由 hub 删标记，
# 万一 hub 异常残留，用户手动删 logs\gpu_released.flag 即恢复看门狗复活逻辑。
_GPU_RELEASE_FLAG = LOGS_DIR / "gpu_released.flag"


def _gpu_released() -> bool:
    try:
        return _GPU_RELEASE_FLAG.exists()
    except Exception:
        return False


_auth_fast_mode = False

# ---- 全机单实例保护 ----
# 历史事故：守护脚本 _watchdog_guard.ps1 与启动器在开机/重启窗口竞态，先后各拉
# 起一个 mem_watchdog，导致两个实例并存——鉴权巡检/自愈、告警去抖、状态文件写
# 全部翻倍且互相打架。根治办法不是去协调"谁来拉起"(协调本身仍有竞态)，而是让
# 进程自己保证"只活一个"：绑定一个回环端口当锁。内核级、跨会话、进程退出即自动
# 释放，无残留锁文件、无竞态窗口；端口被占用即说明已有实例在跑，新实例自退。
import socket as _socket
import ctypes as _ctypes
_SINGLETON_SOCK = None
_SINGLETON_MUTEX = None
_SINGLETON_PORT = int(os.environ.get("MEMWD_SINGLETON_PORT", "49677"))
# 单实例锁名：Windows 命名互斥体（内核级、进程退出即自动释放，无端口占用/无残留锁文件）。
# 用 Global\ 前缀求跨会话唯一；无权限创建 Global 时 CreateMutexW 会失败 → 自动落到端口兜底。
_SINGLETON_MUTEX_NAME = os.environ.get(
    "MEMWD_SINGLETON_MUTEX", r"Global\AvatarHub_mem_watchdog_singleton_v1")


def _sibling_watchdog_running() -> bool:
    """是否存在“另一个”常驻 mem_watchdog.py 进程（排除自己与 --once/--audit 一次性取样）。
    用于回环端口 bind 失败时甄别：是真有第二实例，还是端口被无关进程抢占。"""
    me = os.getpid()
    try:
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if p.info["pid"] == me:
                    continue
                if "python" not in (p.info["name"] or "").lower():
                    continue
                cmd = " ".join(p.info["cmdline"] or [])
                if "mem_watchdog.py" in cmd and "--once" not in cmd and "--audit" not in cmd:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return False


def _acquire_singleton():
    """确保“全机只活一个常驻实例”。
    首选 Windows 命名互斥体：内核级、跨会话、进程退出即自动释放，无端口占用问题。
    历史事故(本次修)：旧实现用固定回环端口(49677)当锁，但它落在动态端口段(49152–65535)，
    机器重启后可能被无关进程(实测 services.exe)先抢走 → bind 失败被误判成“已有实例”→
    看门狗永远起不来、静默失守（avatar_hub 掉线也没人拉回，一键开播随之报 Failed to fetch）。
    成功→返回 True 并长期持有句柄(防 GC/内核释放)；确有第二实例→False。"""
    global _SINGLETON_MUTEX, _SINGLETON_SOCK
    # 1) Windows 命名互斥体（首选，彻底规避端口抢占误判）
    if sys.platform.startswith("win"):
        try:
            ERROR_ALREADY_EXISTS = 183
            k32 = _ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateMutexW.restype = _ctypes.c_void_p
            k32.CreateMutexW.argtypes = [_ctypes.c_void_p, _ctypes.c_bool, _ctypes.c_wchar_p]
            handle = k32.CreateMutexW(None, False, _SINGLETON_MUTEX_NAME)
            last = _ctypes.get_last_error()
            if handle and last == ERROR_ALREADY_EXISTS:
                return False
            if handle:
                _SINGLETON_MUTEX = handle       # 常驻持有；进程退出由内核自动释放
                return True
        except Exception:
            pass                                 # 互斥体不可用(如无 Global 权限) → 落到端口兜底
    # 2) 回环端口兜底（跨平台）：bind 失败时先甄别是否真有同类实例，避免被无关占用误锁死
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", _SINGLETON_PORT))
        s.listen(1)
        _SINGLETON_SOCK = s
        return True
    except OSError:
        try:
            s.close()
        except Exception:
            pass
        if _sibling_watchdog_running():
            return False                         # 确有第二实例 → 让位退出
        log(f"[单实例] 锁端口 {_SINGLETON_PORT} 被无关进程占用，但未发现同类实例 → 忽略端口锁继续运行")
        return True

PY = {
    "latentsync":  app_config.conda_python("latentsync"),
    "musethepeak": app_config.conda_python("musethepeak"),
    "cosyvoice":   app_config.conda_python("cosyvoice"),
    "facefusion":  app_config.conda_python("facefusion"),
    "rvc":         app_config.conda_python("rvc"),
    "fishspeech":  app_config.conda_python("fishspeech"),
    "nemoasr":     app_config.conda_python("nemoasr"),
    "sbv2":        app_config.conda_python("sbv2"),
}

# 覆盖全部重型服务（含 start_all_services.bat 启动的 9 个 + 按需的 latentsync）。
# 字段: name, script(匹配进程), py(重启用), title(窗口名), port(健康/可选 /gc),
#       cap_gb(单服务提交内存硬上限), gc(是否支持 /gc 端点), restart(是否允许自动重启),
#       env(重启时需注入的环境变量, 可选)
SERVICES = [
    # —— 已打补丁: 支持 /gc 优雅回收, 可自动重启 ——
    {"name": "latentsync",  "script": "latentsync_server.py",  "py": PY["latentsync"],  "title": "LatentSync",  "port": 8091, "cap_gb": 18.0, "gc": True,  "restart": True},
    {"name": "lipsync",     "script": "lipsync_server.py",     "py": PY["musethepeak"], "title": "LipSync",     "port": 8090, "cap_gb": 20.0, "gc": True,  "restart": True, "launcher": "_launch_lipsync_local.bat"},
    {"name": "emotion_tts", "script": "emotion_tts_server.py", "py": PY["cosyvoice"],   "title": "EmotionTTS",  "port": 7852, "cap_gb": 14.0, "gc": True,  "restart": True, "revive_if_seen": True, "launcher": "_launch_emotion_local.bat"},
    {"name": "enhance",     "script": "enhance_server.py",     "py": PY["facefusion"],  "title": "Enhance",     "port": 8092, "cap_gb": 8.0,  "gc": True,  "restart": True},
    # —— 其余重型服务: 无 /gc 端点, 仅在硬阈值时重启 ——
    {"name": "faceswap",    "script": "faceswap_api.py",       "py": PY["facefusion"],  "title": "FaceSwap-API", "port": 8000, "cap_gb": 8.0,  "gc": True,  "restart": True},
    # S6 换脸容灾副本（同脚本第二实例, FACESWAP_PORT=8003）: 只监控+优雅 /gc, **绝不在此重启**——
    #   重启走 Hub supervisor(会注入瘦身 env)；这里 restart 会拉成"全量 faceswap 抢 8000"(2026-07-09 实锤事故:
    #   副本被按 faceswap(8G cap)记账→反复处决→重启成 22.7G 常驻流氓)。cap 放宽: CUDA 提交虚高(瘦身实测 ~13G)。
    {"name": "faceswap2",   "script": "faceswap_api.py",       "py": PY["facefusion"],  "title": "FaceSwap-副本", "port": 8003, "cap_gb": 26.0, "gc": True,  "restart": False},
    {"name": "tts_api",     "script": "tts_api.py",            "py": PY["rvc"],         "title": "TTS-API",     "port": 7851, "cap_gb": 8.0,  "gc": True,  "restart": True, "env": {"COQUI_TOS_AGREED": "1"}, "revive_if_seen": True},
    {"name": "hair",        "script": "hair_api.py",           "py": PY["facefusion"],  "title": "Hair-API",    "port": 8001, "cap_gb": 8.0,  "gc": True,  "restart": True},
    {"name": "singing",     "script": "singing_server.py",     "py": PY["musethepeak"], "title": "Singing",     "port": 7853, "cap_gb": 10.0, "gc": True,  "restart": True},
    # —— 仅监控(不自动重启): vcam 持有直播流、avatar_hub 是编排中枢, 重启会误伤且本就很轻 ——
    {"name": "vcam",        "script": "vcam_server.py",        "py": PY["facefusion"],  "title": "VCam",        "port": 7870, "cap_gb": 6.0,  "gc": False, "restart": False},
    # restart=False: 不因"接近/超内存上限"重启（避免误伤在线会话）；revive=True: 但若进程
    # 真正"死了"(掉线)必须拉回，否则编排中枢永久掉线=全产品瘫痪(实测过的"定时炸弹")。
    {"name": "avatar_hub",  "script": "avatar_hub.py",         "py": PY["facefusion"],  "title": "AvatarHub",   "port": 9000, "cap_gb": 4.0,  "gc": False, "restart": False, "revive": True, "launcher": "_launch_hub_detached.bat"},
    # fish 是对话主链 TTS（克隆音），同属核心：内存不杀(模型重载昂贵~3min)，但进程死了必拉回。
    # load_probe: 进程在但"模型迟迟未加载"(如 torch.compile 卡死/缓存损坏)也算病态——
    #   超 load_grace 秒仍 model_loaded=false 则回收；连续两次卡死则先清编译缓存再拉(破损坏缓存死循环)。
    {"name": "fish_tts",    "script": "fish_speech_server.py", "py": PY["fishspeech"],   "title": "FishTTS",     "port": 7855, "cap_gb": 12.0, "gc": False, "restart": False, "revive": True, "launcher": "_launch_fish_local.bat",
     "load_probe": "/health", "load_key": "model_loaded", "load_grace": 600, "compile_cache_dirs": [r"C:\ic", r"C:\tc"]},
    # 同传(通话主链)：此前未纳管,实测父 shell 关闭即身亡、通话中掉线只能手工发现(P1 复盘补上)。
    # 内存不杀(会话在内存里,误杀=掉线);进程死了必拉回。声纹底座已落盘,拉回后重开会话即恢复。
    {"name": "interpreter", "script": "live_interpreter.py",   "py": PY["facefusion"],   "title": "Interpreter", "port": 7900, "cap_gb": 6.0,  "gc": False, "restart": False, "revive": True, "launcher": "_launch_interp.bat"},
    # 流式逐词 STT(P2 上线)：掉线→同传自动回退分段模式(功能仍在,体验降级),故 revive_if_seen
    # (开机脚本起过它才守;没起=用户选择不占这 4G 显存,不越权冷启)。fp16+expandable_segments。
    {"name": "nemo_stt",    "script": "nemotron_stt_server.py", "py": PY["nemoasr"],     "title": "NemoSTT",     "port": 7857, "cap_gb": 10.0, "gc": False, "restart": False, "revive_if_seen": True, "launcher": "_launch_nemo_local.bat"},
    # P5d 日语情感 TTS(林小玲 SBV2 JP-Extra)：ja 语向同传主配音引擎(0.15s/句)。掉线→同传自动
    # 回退 Fish(音色不带情感,体验降级),故进程死了拉回;模型小(<1GB)重载快,内存不杀。
    {"name": "sbv2_tts",    "script": "sbv2_tts_server.py",     "py": PY["sbv2"],        "title": "SBV2TTS",     "port": 7861, "cap_gb": 5.0,  "gc": False, "restart": False, "revive": True, "launcher": "_launch_sbv2_local.bat"},
    # P11 S1-mini: 显存重(与 LoRA 训练互斥),训练期不自动拉起;LoRA 合并后手动或看门狗 revive。
    {"name": "s1_upstream", "script": "",                       "py": "",                "title": "S1Upstream", "port": 7862, "cap_gb": 10.0, "gc": False, "restart": False, "revive": False, "launcher": r"C:\fishs1\_launch_s1_server.bat"},
    {"name": "s1_tts",      "script": "s1_tts_server.py",       "py": PY["sbv2"],        "title": "S1TTS",      "port": 7863, "cap_gb": 0.5,  "gc": False, "restart": False, "revive": False, "launcher": "_launch_s1_local.bat"},
]

INTERVAL = int(os.environ.get("MEMWD_INTERVAL", "20"))
HARD_PCT = float(os.environ.get("MEMWD_HARD_PCT", "88"))
SOFT_PCT = float(os.environ.get("MEMWD_SOFT_PCT", "80"))
MIN_AVAIL_GB = float(os.environ.get("MEMWD_MIN_AVAIL_GB", "4"))
COOLDOWN = int(os.environ.get("MEMWD_COOLDOWN", "300"))
# 进程"已死"拉起的冷却（独立于内存重启冷却，更短：核心服务掉线要尽快拉回）。
REVIVE_COOLDOWN = int(os.environ.get("MEMWD_REVIVE_COOLDOWN", "60"))
TRY_GC = os.environ.get("MEMWD_TRY_GC", "0") == "1"

_last_restart = {}                       # name -> ts
_ever_seen = {}                          # name -> 本会话是否曾在线（用于"在线过才拉回"，不冷启未起的服务）
_load_bad_since = {}                     # name -> 首次探到 model 未加载的 ts（卡死计时起点）
_load_stall_count = {}                   # name -> 连续"卡死回收"次数（用于升级清编译缓存）
_last_gc = {}                            # name -> 上次调用 /gc 的 ts（限频）
_hist = defaultdict(lambda: deque(maxlen=10))   # name -> 最近若干次提交内存(GB)
_soft_warned = {}                        # name -> 上次软告警 ts（限频）

# ── 崩溃熔断（本次新增）──────────────────────────────────────────
# 某服务被“拉起→仍旧秒崩”连续 N 次 → 判为“拉了也白拉”(缺依赖/配置损坏，实测 tts_api 缺
# torchcodec 即如此)：打开熔断器停手一段时间并告警，杜绝每 60s 空转重拉把日志刷爆。
# 服务一旦被探到真正存活 → 计数清零、熔断解除，修好后自动恢复守护。
REVIVE_MAX_RETRY = int(os.environ.get("MEMWD_REVIVE_MAX_RETRY", "5"))
REVIVE_BREAKER_COOLDOWN = int(os.environ.get("MEMWD_REVIVE_BREAKER_COOLDOWN", "1800"))
_revive_fail = defaultdict(int)          # name -> 连续“拉起仍死”次数
_revive_breaker_until = {}               # name -> 熔断到期 ts

GC_COOLDOWN = int(os.environ.get("MEMWD_GC_COOLDOWN", "90"))  # 同一服务两次主动 /gc 的最小间隔


def _update_status(section: str, **kv):
    """合并式更新 logs/watchdog_status.json，供 hub 显性化看门狗运行态。
    计数器跨重启保留（先读后并）。任何异常都不得影响主逻辑。"""
    import json
    try:
        data = {}
        if _STATUS_FILE.exists():
            try:
                data = json.loads(_STATUS_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        sec = data.get(section) or {}
        for k, v in kv.items():
            if k.startswith("+"):                       # "+attempts" 形式做自增
                sec[k[1:]] = int(sec.get(k[1:], 0)) + int(v)
            else:
                sec[k] = v
        data[section] = sec
        data["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            data["pid"] = os.getpid()
        except Exception:
            pass
        _STATUS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _rotate_logfile():
    """滚动 mem_watchdog.log：.（KEEP）删除→ .k→.k+1 … 当前→.1。log() 每次按需
    open/close，故两次写之间滚动安全(无常驻句柄)。任一步失败都吞掉，绝不影响监控。"""
    base = LOG_FILE
    try:
        oldest = base.with_name(base.name + f".{LOG_KEEP}")
        if oldest.exists():
            oldest.unlink()
    except Exception:
        pass
    for i in range(LOG_KEEP - 1, 0, -1):
        try:
            s = base.with_name(base.name + f".{i}")
            if s.exists():
                s.replace(base.with_name(base.name + f".{i + 1}"))
        except Exception:
            pass
    try:
        base.replace(base.with_name(base.name + ".1"))
    except Exception:
        pass


def log(msg: str):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        # 控制台编码不支持(如 emoji) 时降级为 ASCII，绝不让打印异常中断检查逻辑
        try:
            print(line.encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            _rotate_logfile()
    except Exception:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def gpu_mem():
    """返回 (used_mb, total_mb, util%)；无 GPU/失败返回 (None, None, None)。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=CREATE_NO_WINDOW)
        if out.returncode == 0 and out.stdout.strip():
            u, t, util = [x.strip() for x in out.stdout.strip().splitlines()[0].split(",")]
            return float(u), float(t), float(util)
    except Exception:
        pass
    return None, None, None


def _proc_commit_mb(p):
    try:
        mi = p.memory_info()
        return max(getattr(mi, "vms", 0), getattr(mi, "rss", 0)) / (1024 * 1024)
    except Exception:
        return 0.0


def _pid_listen_ports(p) -> set:
    try:
        return {c.laddr.port for c in p.connections(kind="tcp")
                if c.status == psutil.CONN_LISTEN and c.laddr}
    except Exception:
        return set()


def find_service_procs():
    found = {s["name"]: [] for s in SERVICES}
    # S6: 同脚本多实例（faceswap 8000 / faceswap2 8003 都是 faceswap_api.py）→ 脚本名不再唯一，
    # 归属按「进程监听端口」二次甄别；还没监听（启动中）的实例本轮不记账（防错杀）。
    svcs_by_script: dict[str, list] = {}
    for s in SERVICES:
        svcs_by_script.setdefault(s["script"], []).append(s)
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if "python" not in (p.info["name"] or "").lower():
                continue
            cmd = " ".join(p.info["cmdline"] or [])
            for script, svcs in svcs_by_script.items():
                if script not in cmd:
                    continue
                if len(svcs) == 1:
                    found[svcs[0]["name"]].append((p, _proc_commit_mb(p)))
                else:
                    ports = _pid_listen_ports(p)
                    owner = next((s for s in svcs if s["port"] in ports), None)
                    if owner is not None:
                        found[owner["name"]].append((p, _proc_commit_mb(p)))
                break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


def try_gc_endpoint(svc) -> bool:
    """调用服务 /gc 端点做非侵入式回收（gc + 释放显存缓存，不卸载模型、不重启）。
    成功返回 True，并记录回收量；端点不存在/不可达返回 False。"""
    if not TRY_GC or urllib is None or not svc.get("gc"):
        return False
    import json
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{svc['port']}/gc", method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status != 200:
                return False
            body = json.loads(r.read().decode("utf-8", "ignore") or "{}")
        _last_gc[svc["name"]] = time.time()
        freed = body.get("gpu_reserved_freed_mb")
        objs = body.get("gc_objects")
        log(f"  ✅ {svc['name']} /gc 回收完成（优雅，无需重启）"
            f" gc对象={objs} 释放显存={freed}MB")
        return True
    except Exception:
        return False


def _is_local_service(svc) -> bool:
    """该服务是否由本机托管：对应 SVC_<NAME> 未设或指向 localhost/127.0.0.1 → 本机；
    指向远端(如 184)则 False —— 远端机器自管其存活，本机绝不越权拉起。
    与 avatar_hub 的 _svc_url 命名约定一致(SVC_<大写服务名>)。"""
    env = os.environ.get("SVC_" + svc["name"].upper(), "").strip()
    if not env:
        return True
    return ("127.0.0.1" in env) or ("localhost" in env)


def _env_service_url(env_name: str) -> str:
    """Read SVC_* from current environment; fall back to env_config.bat parsing.
    The watchdog is normally launched through env_config.bat, but parsing the file keeps
    one-shot/manual runs honest and lets config changes be picked up without code edits."""
    val = os.environ.get(env_name, "").strip()
    if val:
        return val
    try:
        cfg = BASE_DIR / "env_config.bat"
        if not cfg.exists():
            return ""
        # 兼容两种写法: `set "X=..."` 与 `if not defined X set "X=..."`(env_config 常用防覆盖形式)
        pat = re.compile(r'^\s*(?:if\s+not\s+defined\s+\S+\s+)?set\s+"?%s=([^"\r\n]+)"?\s*$'
                         % re.escape(env_name), re.I)
        for line in cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.lstrip().lower().startswith("rem"):
                continue
            m = pat.match(line)
            if m:
                return m.group(1).strip()
    except Exception:
        pass
    return ""


def _remote_targets():
    """Return configured offloaded services from env_config: (name, url, host, task, port, script)."""
    try:
        from urllib.parse import urlparse
    except Exception:
        return []
    out = []
    for name, env_name in [
        ("stt", "SVC_STT"),
        ("faceswap", "SVC_FACESWAP"),
        ("emotion_tts", "SVC_EMOTION_TTS"),
        ("nemo_stt", "SVC_NEMO_STT"),   # 2026-07-05 迁 .140(掉线→同传自动回退分段,自愈仍要跟上)
        ("qwen3_tts", "SVC_QWEN3_TTS"),  # 2026-07-05 .117 落地(商用备选引擎,离线出片用)
    ]:
        url = _env_service_url(env_name)
        if not url:
            continue
        try:
            parsed = urlparse(url)
            host = parsed.hostname or ""
            port = parsed.port
        except Exception:
            host, port = "", None
        if host in ("", "127.0.0.1", "localhost", "::1"):
            continue
        task = _REMOTE_TASKS.get(name)
        if task:
            out.append((name, url.rstrip("/"), host, task, port, _REMOTE_SCRIPTS.get(name)))
    return out


# 探活端点缓存：记住每个 url 上一次成功的健康端点，避免对只有 /health 的服务
#   每轮都先打一次必定 404 的 /healthz（既费请求又污染对端访问日志）。
_PROBE_EP_CACHE: dict = {}


def _remote_probe(url: str) -> bool:
    if urllib is None:
        return True
    # Prefer healthz (pure liveness) and fall back to health for older deployments.
    # 命中缓存的端点优先尝试；探测成功后记忆该端点，后续循环不再重复 404 路径。
    cached = _PROBE_EP_CACHE.get(url)
    eps = ("/healthz", "/health")
    if cached:
        eps = (cached,) + tuple(e for e in eps if e != cached)
    for ep in eps:
        try:
            with urllib.request.urlopen(url + ep, timeout=5) as r:
                if r.status == 200:
                    _PROBE_EP_CACHE[url] = ep
                    return True
                if ep == "/healthz" and r.status in (404, 405):
                    continue
                return False
        except Exception:
            if ep == "/healthz":
                continue
            return False
    return False


def _ssh_restart_task(host: str, task: str, port=None, script=None) -> bool:
    """Restart a remote Windows Scheduled Task via SSH using an encoded PowerShell payload.

    先按端口占用 + 按命令行(脚本名)强杀旧进程，再 Stop/Start 计划任务。
    只做 Stop-ScheduledTask 杀不到 boot .bat 用 `start` 甩出去的孤儿 python（任务跑的是 .bat，
    spawn python 后即退出）。更狠的是 2026-07-08 .117 事故：qwen3_tts 遇网络抖动(WinError 64)后
    accept 循环死掉——进程仍活、CUDA 还在、却不再监听端口，旧逻辑"只重启任务"永远空转、救不回。
      · 按端口杀：抓正常僵尸（还占着端口的）；
      · 按命令行杀：兜住"进程活着但端口已死"这种端口 kill 漏网的。
    杀掉后端口释放，boot .bat 的幂等闸(端口/探活)才会真正拉起一个干净实例。"""
    lines = ["$ErrorActionPreference='SilentlyContinue'"]
    if port:
        lines.append(
            f"$owners = Get-NetTCPConnection -LocalPort {int(port)} -State Listen "
            "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique")
        lines.append("foreach($p in $owners){ if($p){ Stop-Process -Id $p -Force -ErrorAction SilentlyContinue } }")
    if script:
        _safe = re.escape(script)
        lines.append(
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            f"Where-Object {{ $_.CommandLine -match '{_safe}' }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    lines.append(f"Stop-ScheduledTask -TaskName '{task}'")
    lines.append("Start-Sleep 3")
    lines.append(f"Start-ScheduledTask -TaskName '{task}'")
    lines.append(f"Write-Output 'REMOTE_RESTART_OK {task}'")
    ps = "\n".join(lines)
    try:
        import base64
        b64 = base64.b64encode(ps.encode("utf-16le")).decode("ascii")
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", f"{REMOTE_SSH_USER}@{host}",
             "powershell", "-NoProfile", "-EncodedCommand", b64],
            cwd=str(BASE_DIR), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=45,
            creationflags=CREATE_NO_WINDOW,
        )
        ok = (r.returncode == 0) and ("REMOTE_RESTART_OK" in ((r.stdout or "") + (r.stderr or "")))
        if not ok:
            log(f"[远程自愈] SSH 重启 {host}/{task} 失败 rc={r.returncode}: {(r.stderr or r.stdout or '').strip()[:300]}")
        return ok
    except Exception as e:
        log(f"[远程自愈] SSH 重启 {host}/{task} 异常: {e}")
        return False


def run_remote_health_audit(dry_run=False):
    """Probe offloaded remote services and restart their scheduled task after consecutive failures."""
    global _last_remote_health
    if REMOTE_HEALTH_SEC <= 0:
        return True
    now = time.time()
    if now - _last_remote_health < REMOTE_HEALTH_SEC:
        return True
    _last_remote_health = now
    targets = _remote_targets()
    if not targets:
        _update_status("remote_health", enabled=False, targets=0, healthy=True)
        return True
    all_ok = True
    summary = {}
    for name, url, host, task, port, script in targets:
        ok = _remote_probe(url)
        summary[name] = {"url": url, "host": host, "task": task, "ok": ok}
        key = f"remote_service_down_{name}"
        if ok:
            if _remote_fail_count.get(name):
                log(f"[远程巡检] {name} 已恢复: {url}")
            _remote_fail_count[name] = 0
            if name in _remote_was_down:
                try:
                    import alerts
                    alerts.clear_alert(key, note=f"{name} remote service recovered: {url}")
                except Exception:
                    pass
                _remote_was_down.discard(name)
            continue

        all_ok = False
        _remote_fail_count[name] += 1
        fc = _remote_fail_count[name]
        log(f"[远程巡检][WARN] {name} 不可达({fc}/{REMOTE_FAIL_THRESHOLD}): {url}")
        if fc < REMOTE_FAIL_THRESHOLD:
            continue
        detail = f"{name} unreachable at {url}; host={host}; scheduled task={task}; fail_count={fc}"
        try:
            import alerts
            alerts.raise_alert(
                key,
                f"远程服务掉线：{name}",
                detail=detail,
                level="error",
                source="mem_watchdog/remote_health",
            )
            _remote_was_down.add(name)
        except Exception:
            pass
        if dry_run:
            log(f"[远程自愈] {name} [dry-run] 跳过 SSH 重启")
            continue
        if now - _remote_last_heal.get(name, 0) < REMOTE_HEAL_COOLDOWN:
            continue
        log(f"[远程自愈] {name} 连续不可达 → SSH 强杀旧进程(端口{port}/{script})并重启 {host}/{task}")
        _remote_last_heal[name] = now
        if _ssh_restart_task(host, task, port, script):
            log(f"[远程自愈] 已触发 {host}/{task} 重启，后续巡检确认恢复")
    _update_status("remote_health", enabled=True, targets=len(targets), healthy=all_ok,
                   last_check=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), services=summary)
    return all_ok


def _probe_loaded(svc):
    """GET 服务健康端点，按 load_key 判模型是否就绪。返回 True/False；不可达返回 None(不计时)。"""
    if urllib is None:
        return None
    import json
    url = f"http://127.0.0.1:{svc['port']}{svc.get('load_probe', '/health')}"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            if r.status != 200:
                return None
            body = json.loads(r.read().decode("utf-8", "ignore") or "{}")
        return bool(body.get(svc.get("load_key", "model_loaded")))
    except Exception:
        return None


def _clear_dir(d) -> bool:
    """清空目录内容(保留目录本身)。目录不存在也算成功。"""
    try:
        p = Path(d)
        if not p.exists():
            return True
        for child in p.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink()
            except Exception:
                pass
        return True
    except Exception:
        return False


_hotfix_reverted_for = set()   # 已针对哪些 probation(版本) 回滚过，防重复回滚


def _maybe_revert_bad_hotfix():
    """hub 反复起不来 + 存在未确认的程序热修 probation → 回滚上一代 app（无人值守自愈）。
    仅客户安装目录生效（app_revert 内有 .git 护栏，开发/受管仓自动拒绝）。每 probation 只回一次。"""
    try:
        import pack_installer as _pi
    except Exception:
        return
    try:
        if not _pi.app_probation_pending():
            return
        p = _pi._load_json(_pi.APP_PROBATION_FILE) or {}
        ver = str(p.get("version", ""))
        if ver in _hotfix_reverted_for:
            return
        log(f"🛟[热修自愈] hub 反复起不来且热修 v{ver} 未通过运行验收 → 自动回滚上一代 app")
        if _pi.app_revert(log=lambda m: log("   " + m)):
            _hotfix_reverted_for.add(ver)
            _revive_fail["avatar_hub"] = 0            # 给回滚后的旧代码一次干净重拉机会
            _revive_breaker_until.pop("avatar_hub", None)
            try:
                import alerts
                alerts.raise_alert("hotfix_auto_revert", f"程序热修 v{ver} 自动回滚",
                                   detail=f"hub 应用热修 v{ver} 后反复起不来，看门狗已回滚上一代并重拉。请核查该版本。",
                                   level="error", source="mem_watchdog/热修自愈")
            except Exception:
                pass
    except Exception as _e:
        log(f"   [热修自愈] 异常(忽略): {_e}")


def _port_alive(port) -> bool:
    """端口是否有人在听（快速 connect 探测，300ms 超时）。用于"判死"前交叉验证：
    进程枚举(cmdline)在高负载下会瞬时漏报，端口在听即认定服务存活，避免误拉第二实例。"""
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(0.3)
        ok = s.connect_ex(("127.0.0.1", int(port))) == 0
        s.close()
        return ok
    except Exception:
        return False


def _wait_port_free(port, timeout=15.0):
    """等本地端口可绑(TIME_WAIT 释放)再拉起：根治"杀→秒级重启撞端口、新进程 bind 失败即崩"
    的自愈空窗（实测：force-kill 后旧端口未释放，看门狗那次 revive 起来即死，又被冷却锁住 5 分钟）。
    不带 SO_REUSEADDR 试绑 0.0.0.0:port（与 uvicorn 绑法一致）；返回 True=可绑/False=超时仍占用。"""
    if not port:
        return True
    import socket
    deadline = time.time() + timeout
    while True:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("0.0.0.0", int(port)))
            s.close()
            return True
        except OSError:
            s.close()
            if time.time() >= deadline:
                return False
            time.sleep(1)


def restart_service(svc, procs, force=False, min_interval=None):
    """force=True 用于"进程已死"的拉起：绕过 restart=False 的"仅内存监控"语义。
    min_interval：本次重启的最小间隔（秒）。核心服务 liveness 重拉传 REVIVE_COOLDOWN(60s)，
    避免"首拉撞端口失败后被 300s 内存防抖冷却锁死、空窗 5 分钟"；内存压力重启不传=用默认 COOLDOWN。"""
    name = svc["name"]
    if not force and not svc.get("restart", True):
        log(f"  ⚠️ {name} 标记为仅监控、不自动重启（需人工处理）")
        return False
    now = time.time()
    cd = COOLDOWN if min_interval is None else min_interval
    if now - _last_restart.get(name, 0) < cd:
        log(f"  跳过重启 {name}（冷却中，{int(cd-(now-_last_restart.get(name,0)))}s 后可再重启）")
        return False
    for p, _mb in procs:
        try:
            p.kill()
            log(f"  已结束 {name} 旧进程 PID={p.pid}")
        except Exception as e:
            log(f"  结束 {name} 失败: {e}")
    time.sleep(2)
    # 拉起前先等端口释放：核心服务掉线常因刚被杀，端口仍在 TIME_WAIT，立刻拉起会 bind 失败即崩。
    _port = svc.get("port")
    if _port and not _wait_port_free(_port):
        log(f"  ⚠️ {name} 端口 {_port} 超 15s 仍被占用，仍尝试拉起（避免不作为）")
    title = svc["title"]
    script_path = str(BASE_DIR / svc["script"])
    # 优先用服务自带的启动器(.bat)：它经 %~dp0 自定位、自行 call env_config→secrets，
    # 对中文路径/密钥注入最稳妥（避免手拼命令在中文全路径 + && 短路上翻车）。
    launcher = svc.get("launcher")
    if launcher and (BASE_DIR / launcher).exists():
        # 用 /D 显式设工作目录 + 相对启动器名：规避中文全路径经 `cmd /c ""中文绝对路径""`
        # 嵌套引号传参被损坏(实测：绝对中文路径嵌套调用启动器会"拉空"——python 根本没跑起来)。
        cmd = f'start "{title}" /MIN /D "{BASE_DIR}" cmd /c "{launcher}"'
    else:
        env_cfg = BASE_DIR / "env_config.bat"
        env_prefix = "".join(f'set "{k}={v}" && ' for k, v in (svc.get("env") or {}).items())
        # 关键：先 call env_config.bat 注入完整运行环境(云端密钥/SVC_ 跨机路由/服务令牌)，
        # 用 & 而非 && —— 即便环境脚本返回非零也务必拉起进程，绝不因短路而"拉了个空窗"。
        env_call = f'call "{env_cfg}" & ' if env_cfg.exists() else ""
        cmd = (f'start "{title}" /MIN cmd /k "chcp 65001 >nul & {env_call}{env_prefix}'
               f'"{svc["py"]}" "{script_path}""')
    try:
        subprocess.Popen(cmd, shell=True, cwd=str(BASE_DIR))
        _last_restart[name] = now
        log(f"  已重新启动 {name}")
        return True
    except Exception as e:
        log(f"  重启 {name} 失败: {e}")
        return False


def _leak_suspect(name) -> bool:
    """最近样本是否单调上涨且累计涨幅 > 2G（疑似泄漏）。"""
    h = _hist[name]
    if len(h) < 5:
        return False
    rising = all(h[i] <= h[i + 1] + 0.05 for i in range(len(h) - 1))
    return rising and (h[-1] - h[0]) > 2.0


def check_once(dry_run=False):
    vm = psutil.virtual_memory()
    avail_gb = vm.available / (1024 ** 3)
    total_gb = vm.total / (1024 ** 3)
    gu, gt, gutil = gpu_mem()
    found = find_service_procs()

    svc_commit = {}
    for s in SERVICES:
        gb = sum(mb for _p, mb in found[s["name"]]) / 1024
        svc_commit[s["name"]] = gb
        if found[s["name"]]:
            _ever_seen[s["name"]] = True       # 本会话曾在线 → 日后掉线才"拉回"(不冷启没起过的服务)
            # 真正存活 → 崩溃熔断计数清零、解除熔断（服务修好后自动恢复守护）
            if _revive_breaker_until.pop(s["name"], None) is not None:
                log(f"✅[熔断解除] {s['name']} 已恢复存活，恢复自动守护")
                try:
                    import alerts
                    alerts.clear_alert(f"svc_revive_breaker_{s['name']}", note=f"{s['name']} 已恢复存活")
                except Exception:
                    pass
            _revive_fail[s["name"]] = 0
        if gb > 0:
            _hist[s["name"]].append(round(gb, 2))

    parts = " ".join(f"{n}={svc_commit[n]:.1f}G" for n in svc_commit if svc_commit[n] > 0)
    gpu_str = (f" | 显存{gu/1024:.1f}/{gt/1024:.0f}G({gutil:.0f}%)"
               if gu is not None else "")
    log(f"内存 用{vm.percent:.0f}% 可用{avail_gb:.1f}/{total_gb:.0f}G{gpu_str} | {parts or '(无服务)'}")

    sys_hard = (vm.percent >= HARD_PCT) or (avail_gb <= MIN_AVAIL_GB)
    sys_soft = (vm.percent >= SOFT_PCT)

    # 软告警：泄漏趋势 / 接近硬上限 / 系统软阈值（限频，每服务≥120s 一次）
    now = time.time()
    for s in SERVICES:
        nm = s["name"]
        gb = svc_commit[nm]
        if gb <= 0:
            continue
        warn = None
        if _leak_suspect(nm):
            warn = f"疑似泄漏(近样本持续上涨 {list(_hist[nm])})"
        elif gb > s["cap_gb"] * 0.8:
            warn = f"接近上限({gb:.1f}/{s['cap_gb']:.0f}G)"
        if warn and (now - _soft_warned.get(nm, 0) > 120):
            log(f"⚠️[软] {nm}: {warn}")
            _soft_warned[nm] = now
        # 软阶段主动优雅回收：接近上限/疑似泄漏即调 /gc，争取在硬阈值前化解，避免重启
        if warn and not dry_run and (now - _last_gc.get(nm, 0) > GC_COOLDOWN):
            try_gc_endpoint(s)
    if sys_soft and not sys_hard:
        if now - _soft_warned.get("_sys", 0) > 120:
            log(f"⚠️[软] 系统内存使用 {vm.percent:.0f}% 偏高，留意趋势")
            _soft_warned["_sys"] = now

    # 硬处置 0：进程"已死"→ 拉起(liveness)。两类：
    #   ① revive(hub/fish 核心)：无条件拉回——掉线=全产品瘫痪；
    #   ② revive_if_seen(兜底引擎 cosy/xtts)：仅当「本机托管 + 本会话曾在线 + 现掉线」才拉回——
    #      绝不冷启从没起过的服务(尊重"省显存/挪 184"的选择)，也绝不在远端部署时越权本机拉起。
    _gpu_off = _gpu_released()
    for s in SERVICES:
        nm = s["name"]
        if found[nm]:
            continue
        # P0l 判死交叉验证（2026-07-11 实锤）：cmdline 枚举在高负载下会瞬时漏进程
        # (psutil AccessDenied/竞态)——"进程不存在"可能是尺子抖了。端口还在听=服务活着，
        # 跳过本轮拉起（真死的下一轮 20s 后端口必然释放，不会漏拉）。PortGuard 是最后
        # 防线(拒双绑)，这里是第一道：不该拉的根本不拉。
        _p = s.get("port")
        if _p and _port_alive(_p):
            if time.time() - _soft_warned.get(f"_ghost_{nm}", 0) > 300:
                log(f"⚠️[判死跳过] {nm} 进程枚举未见但端口 {_p} 仍在听——按存活处理(枚举抖动)")
                _soft_warned[f"_ghost_{nm}"] = time.time()
            continue
        # GPU 总开关已释放：不复活 GPU 服务(显存让给另一台机器)；avatar_hub 例外(编排中枢必须在)。
        if _gpu_off and nm != "avatar_hub":
            continue
        if s.get("revive"):
            reason = "核心服务"
        elif s.get("revive_if_seen") and _ever_seen.get(nm) and _is_local_service(s):
            reason = "兜底引擎(曾在线·本机托管)"
        else:
            continue
        # 崩溃熔断打开期间：不再空转重拉（到期自动半开重试；服务恢复存活则在上面清零解除）
        if _revive_breaker_until.get(nm, 0) > now:
            if now - _soft_warned.get(f"_revbrk_{nm}", 0) > 300:
                log(f"⛔[熔断] {nm} 反复拉起仍崩，暂停自动重拉（剩 {int(_revive_breaker_until[nm]-now)}s，需人工修复）")
                _soft_warned[f"_revbrk_{nm}"] = now
            continue
        if dry_run:
            log(f"⛔[死] {nm} 进程不存在 [dry-run] 跳过拉起")
            continue
        if now - _last_restart.get(nm, 0) < REVIVE_COOLDOWN:
            continue
        _revive_fail[nm] += 1
        log(f"⛔[死] {nm} 进程不存在({reason}) → 自动拉起(第 {_revive_fail[nm]} 次，注入完整环境)")
        # P4 无人值守自愈：hub 反复起不来且刚应用过程序热修 → 大概率坏热修，自动回滚上一代。
        #   独立于 launcher UI 路径（无人开控制台的机器也受保护）；每次 probation 只回滚一次。
        if nm == "avatar_hub" and _revive_fail[nm] >= 2:
            _maybe_revert_bad_hotfix()
        # 进程已死的核心 liveness 重拉：走 60s 短冷却 + 等端口释放，杜绝"首拉失败被 300s 锁死"空窗
        restart_service(s, [], force=True, min_interval=REVIVE_COOLDOWN)
        # 连续拉起仍崩达上限 → 熔断并告警（tts_api 缺 torchcodec 这类"修不好只会白拉"的场景）
        if _revive_fail[nm] >= REVIVE_MAX_RETRY:
            _revive_breaker_until[nm] = now + REVIVE_BREAKER_COOLDOWN
            log(f"⛔[熔断] {nm} 连续 {_revive_fail[nm]} 次拉起仍崩 → 熔断，暂停自动重拉 "
                f"{REVIVE_BREAKER_COOLDOWN}s（约 {REVIVE_BREAKER_COOLDOWN // 60} 分钟）")
            try:
                import alerts
                alerts.raise_alert(
                    f"svc_revive_breaker_{nm}",
                    f"服务反复崩溃已熔断：{nm}",
                    detail=(f"{nm} 连续 {_revive_fail[nm]} 次“拉起→仍崩”，已暂停自动重拉 {REVIVE_BREAKER_COOLDOWN}s。"
                            f"常见原因：缺依赖/配置损坏（如 tts_api 缺 torchcodec）；"
                            f"请查该服务启动日志，修复后它一旦正常存活即自动恢复守护。"),
                    level=("critical" if s.get("revive") else "error"),
                    source="mem_watchdog/崩溃熔断",
                )
            except Exception:
                pass

    # 硬处置 0.5：进程在、但"模型迟迟未加载"(torch.compile 卡死/编译缓存损坏)。
    #   超 load_grace 仍未就绪 → 杀并重拉；连续≥2 次卡死 → 先清编译缓存再拉(破损坏死循环)。
    for s in SERVICES:
        nm = s["name"]
        if not s.get("load_probe") or not found[nm]:
            _load_bad_since.pop(nm, None)
            continue
        loaded = _probe_loaded(s)
        if loaded is None:
            continue                       # 探测不可达(刚起/抖动)：不计时、不处置
        if loaded:
            if _load_bad_since.pop(nm, None) is not None:
                log(f"✅[载] {nm} 模型已就绪（卡死计时清零）")
            _load_stall_count[nm] = 0
            continue
        waited = now - _load_bad_since.setdefault(nm, now)
        grace = s.get("load_grace", 600)
        if waited < grace:
            if now - _soft_warned.get(f"_load_{nm}", 0) > 120:
                log(f"⏳[载] {nm} 模型未加载 {waited:.0f}/{grace:.0f}s（编译/加载中，宽限内不动）")
                _soft_warned[f"_load_{nm}"] = now
            continue
        if dry_run:
            log(f"💀[卡] {nm} 模型卡死 {waited:.0f}s [dry-run] 跳过回收")
            continue
        if now - _last_restart.get(nm, 0) < COOLDOWN:
            continue                       # 重启冷却中：下个周期再处置(避免清了缓存却没拉起)
        n = _load_stall_count.get(nm, 0) + 1
        _load_stall_count[nm] = n
        if n >= 2 and s.get("compile_cache_dirs"):
            cleared = [d for d in s["compile_cache_dirs"] if _clear_dir(d)]
            log(f"💀[卡] {nm} 连续{n}次卡死 → 清编译缓存 {cleared}（破损坏缓存死循环）")
        log(f"💀[卡] {nm} 模型卡死 {waited:.0f}s（第{n}次）→ 杀进程并重拉")
        _load_bad_since.pop(nm, None)
        restart_service(s, found[nm], force=True)

    # 硬处置 1：单服务超硬上限
    acted = False
    for s in SERVICES:
        if svc_commit[s["name"]] > s["cap_gb"] and found[s["name"]]:
            log(f"⛔[硬] {s['name']} 提交 {svc_commit[s['name']]:.1f}G 超上限 {s['cap_gb']:.0f}G")
            if dry_run:
                log("   [dry-run] 跳过处置")
                continue
            if try_gc_endpoint(s):
                time.sleep(3)
                continue   # 优雅回收后本轮先观察，下轮再判断
            acted |= restart_service(s, found[s["name"]])

    # 硬处置 2：系统级压力 → 在可重启服务里挑真正的内存大户处理
    if sys_hard and not acted:
        log(f"⛔[硬] 系统内存吃紧（用{vm.percent:.0f}%/可用{avail_gb:.1f}G）")
        present = [(s, svc_commit[s["name"]]) for s in SERVICES if found[s["name"]]]
        if present:
            abs_top, abs_mb = max(present, key=lambda x: x[1])
            log(f"   占用最高: {abs_top['name']}({abs_mb:.1f}G)")
            cands = [(s, mb) for s, mb in present if s.get("restart", True)]
            if dry_run:
                log("   [dry-run] 跳过处置")
            elif cands:
                top, mb = max(cands, key=lambda x: x[1])
                if not try_gc_endpoint(top):
                    restart_service(top, found[top["name"]])
            else:
                log("   无可自动重启的服务，需人工处理")


def _audit_interval():
    """当前巡检间隔：异常时高频，正常时常规。"""
    if AUDIT_EVERY_SEC <= 0:
        return 0
    return AUDIT_FAST_SEC if _auth_fast_mode else AUDIT_EVERY_SEC


def _auth_verify():
    """纯 verify，返回 (ok, alert_line)。"""
    if not _AUTH_AUDIT_PS.exists():
        return True, ""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(_AUTH_AUDIT_PS), "-Mode", "verify"],
        cwd=str(BASE_DIR), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
        creationflags=CREATE_NO_WINDOW,
    )
    if r.returncode == 0:
        return True, ""
    alert = next((ln.strip() for ln in (r.stdout or "").splitlines() if "ALERT" in ln), "")
    if not alert:
        alert = "harden_remote.ps1 verify 退出码=%d" % r.returncode
    return False, alert


def _auth_verify_retry(attempts=6, delay=10):
    """deploy/重启后等服务就绪，带退避复验。"""
    alert = ""
    for i in range(attempts):
        ok, alert = _auth_verify()
        if ok:
            return True, alert
        if i < attempts - 1:
            log("[鉴权自愈] 复验未过，%ds 后重试(%d/%d)…" % (delay, i + 1, attempts))
            time.sleep(delay)
    return False, alert


_PAIR_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})/([A-Za-z0-9_]+)")


def _parse_failed(alert):
    """从告警行解析故障 (主机, 服务)。形如 192.168.0.184/faceswap。
    返回 (hosts, services) 两个去重保序列表。服务名在三机内唯一，故全局 ServiceCsv
    过滤可天然按机命中——只动故障服务，不扰同机健康服务。"""
    hseen, hosts, sseen, svcs = set(), [], set(), []
    for ip, name in _PAIR_RE.findall(alert or ""):
        if ip not in hseen:
            hseen.add(ip); hosts.append(ip)
        if name not in sseen:
            sseen.add(name); svcs.append(name)
    return hosts, svcs


def run_auth_heal(hosts=None, services=None):
    """裸奔时尝试自愈：deploy（只针对故障机+故障服务，最小爆炸半径）。受冷却控制。
    返回 'ok'(deploy 成功) / 'fail'(deploy 失败) / 'skip'(冷却/禁用，不计失败)。"""
    global _last_heal_attempt
    if not AUDIT_HEAL_ENABLED or not _AUTH_AUDIT_PS.exists():
        return "skip"
    now = time.time()
    # 熔断器打开期间：不再反复 deploy（避免远端长期离线时空转刷屏）。到点自然半开放行一次。
    if _heal_breaker_until > now:
        log("[鉴权自愈] 熔断器打开(剩余%ds)：连续失败已达上限，暂停自动 deploy，等待人工处理"
            % int(_heal_breaker_until - now))
        return "skip"
    left = AUDIT_HEAL_COOLDOWN - (now - _last_heal_attempt)
    if left > 0:
        log("[鉴权自愈] 冷却中(%ds)，跳过 deploy" % int(left))
        return "skip"
    _last_heal_attempt = now
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", str(_AUTH_AUDIT_PS), "-Mode", "deploy"]
    scope = "全量"
    if hosts:
        cmd += ["-HostCsv", ",".join(hosts)]
        scope = "仅 " + ",".join(hosts)
        if services:
            cmd += ["-ServiceCsv", ",".join(services)]
            scope += " / 服务 " + ",".join(services)
    log("[鉴权自愈] 启动 deploy（%s）..." % scope)
    _update_status("heal", last_attempt=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   last_scope=scope, **{"+attempts": 1})
    try:
        r = subprocess.run(
            cmd, cwd=str(BASE_DIR), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=AUDIT_HEAL_TIMEOUT,
            creationflags=CREATE_NO_WINDOW,
        )
        if r.returncode == 0:
            log("[鉴权自愈] deploy 完成(exit=0, %s)" % scope)
            return "ok"
        tail = "\n".join((r.stdout or "").splitlines()[-3:])
        log("[鉴权自愈] deploy 失败(exit=%d): %s" % (r.returncode, tail or "(无输出)"))
        _update_status("heal", last_fail=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       last_fail_reason="deploy exit=%d" % r.returncode, **{"+failures": 1})
    except subprocess.TimeoutExpired:
        log("[鉴权自愈] deploy 超时(%ds)" % AUDIT_HEAL_TIMEOUT)
        _update_status("heal", last_fail=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       last_fail_reason="deploy超时", **{"+failures": 1})
    except Exception as e:
        log("[鉴权自愈] 异常: %s" % e)
        _update_status("heal", last_fail=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       last_fail_reason=str(e)[:80], **{"+failures": 1})
    return "fail"


def _escalate_heal_failure(reason, alert):
    """连续自愈失败累加；达阈值升级 critical 告警(@人)；达熔断上限则打开熔断器。"""
    global _heal_consec_fail, _heal_breaker_until
    _heal_consec_fail += 1
    _update_status("heal", consec_failures=_heal_consec_fail)
    log("[鉴权自愈] 失败累计 %d 次（阈值 %d）：%s" %
        (_heal_consec_fail, AUDIT_HEAL_FAIL_ESCALATE, reason))
    if _heal_consec_fail >= AUDIT_HEAL_MAX_RETRY:
        _heal_breaker_until = time.time() + AUDIT_HEAL_BREAKER_COOLDOWN
        _update_status("heal", breaker_open_until=datetime.fromtimestamp(_heal_breaker_until)
                       .strftime("%Y-%m-%d %H:%M:%S"))
        log("[鉴权自愈] 连续失败达上限 %d 次 → 打开熔断器，暂停自动 deploy %ds（约 %.1fh）"
            % (AUDIT_HEAL_MAX_RETRY, AUDIT_HEAL_BREAKER_COOLDOWN, AUDIT_HEAL_BREAKER_COOLDOWN / 3600))
    if _heal_consec_fail >= AUDIT_HEAL_FAIL_ESCALATE:
        try:
            import alerts
            alerts.raise_alert(
                "svc_auth_heal_failed",
                "鉴权自愈连续失败，需人工介入",
                detail="已自动 deploy 自愈 %d 次仍未恢复（%s）。当前裸奔：%s | 请手动: "
                       "powershell -File harden_remote.ps1 -Mode deploy"
                       % (_heal_consec_fail, reason, alert),
                level="critical",
                source="mem_watchdog/鉴权自愈",
            )
        except Exception:
            pass


def _reset_heal_failure():
    """自愈成功 → 清零连续失败计数、关闭熔断器并解除 critical 升级告警。"""
    global _heal_consec_fail, _heal_breaker_until
    if _heal_consec_fail or _heal_breaker_until:
        _heal_consec_fail = 0
        _heal_breaker_until = 0.0
        _update_status("heal", consec_failures=0, breaker_open_until="")
    try:
        import alerts
        alerts.clear_alert("svc_auth_heal_failed", note="鉴权自愈已成功，恢复正常")
    except Exception:
        pass


def _check_token_age():
    """令牌超龄主动提醒（保养项）：>阈值天数后随巡检节拍触发，文件戳按日去重。
    看板黄牌是"要人来看"，这里是"到期喊人"——两层互补，都不动安全总判定。"""
    try:
        if not _TOKEN_FILE.exists() or _TOKEN_STALE_DAYS <= 0:
            return
        age_days = int((time.time() - _TOKEN_FILE.stat().st_mtime) / 86400)
        if age_days <= _TOKEN_STALE_DAYS:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            if _TOKEN_REMIND_STAMP.read_text(encoding="utf-8").strip() == today:
                return
        except Exception:
            pass
        import alerts
        alerts.notify_event(
            "服务令牌超龄：%d 天未轮换（阈值 %d 天）" % (age_days, _TOKEN_STALE_DAYS),
            detail="执行 powershell -File harden_remote.ps1 -Mode rotate，"
                   "或 /ops 安全卡「一键轮换」。轮换全程自动：生成新令牌→三机下发→重启→复验旧令牌失效。",
            source="mem_watchdog/令牌保养",
        )
        _TOKEN_REMIND_STAMP.write_text(today, encoding="utf-8")
        log("[令牌保养] 已提醒：令牌 %d 天未轮换（>%d 天阈值，每日至多一次）" % (age_days, _TOKEN_STALE_DAYS))
    except Exception as e:
        log(f"[令牌保养] 检查异常(忽略): {e}")


def run_auth_audit(*, heal=True):
    """跑一次跨机鉴权巡检；裸奔时可选自愈。返回 True=全护。"""
    global _auth_fast_mode
    if AUDIT_EVERY_SEC <= 0 or not _AUTH_AUDIT_PS.exists():
        return True
    _check_token_age()   # 巡检节拍顺带查令牌年龄(纯本地 stat，零成本；演习让路不影响保养提醒)
    if _drill_active():
        log("[鉴权巡检] 检测到火警演习标记，本轮让路（不巡检/不自愈）")
        return True
    try:
        ok, alert = _auth_verify()
        if ok:
            _auth_fast_mode = False
            log("[鉴权巡检] OK：三机服务面就绪，他机无令牌均被拒")
            _update_status("audit", last_ok=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                           fast_mode=False, healthy=True)
            try:
                import alerts
                alerts.clear_alert("svc_auth_regression", note="三机服务面鉴权已恢复正常")
            except Exception:
                pass
            # 鉴权已恢复正常(含"离线机被正确跳过、无真实裸奔")→ 同时清零自愈失败
            # 计数并解除 critical 升级告警；否则一旦不再触发自愈，该告警会永久滞留。
            _reset_heal_failure()
            return True

        _auth_fast_mode = True
        log("[鉴权巡检][ALERT] 发现裸奔服务！%s" % alert)
        _update_status("audit", last_fail=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       fast_mode=True, healthy=False, last_alert=alert)
        detail = alert + " | 修复: powershell -File harden_remote.ps1 -Mode deploy"
        if heal and AUDIT_HEAL_ENABLED:
            detail += " | 看门狗将自动尝试 deploy"
        log("[鉴权巡检][ALERT] %s" % detail)
        try:
            import alerts
            alerts.raise_alert(
                "svc_auth_regression",
                "服务面鉴权回归：有服务可被无令牌访问",
                detail=detail,
                source="mem_watchdog/鉴权巡检",
            )
        except Exception:
            pass

        failed_hosts, failed_svcs = _parse_failed(alert)
        if heal:
            heal_res = run_auth_heal(failed_hosts, failed_svcs)
            now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if heal_res == "ok":
                ok2, alert2 = _auth_verify_retry()
                if ok2:
                    _auth_fast_mode = False
                    log("[鉴权自愈] 成功：deploy 后复验通过，鉴权已恢复")
                    _update_status("heal", last_success=now_s, **{"+successes": 1})
                    _update_status("audit", last_ok=now_s, fast_mode=False, healthy=True)
                    _reset_heal_failure()
                    try:
                        import alerts
                        alerts.clear_alert(
                            "svc_auth_regression",
                            note="看门狗已自动 deploy 并完成复验，鉴权已恢复",
                        )
                    except Exception:
                        pass
                    return True
                log("[鉴权自愈] deploy 完成但复验仍失败：%s" % alert2)
                _update_status("heal", last_fail=now_s, last_fail_reason="deploy后复验未过",
                               **{"+failures": 1})
                _escalate_heal_failure("deploy后复验未过", alert)
            elif heal_res == "fail":
                _escalate_heal_failure("deploy 执行失败", alert)
            # heal_res == 'skip'（冷却/禁用）：不计失败，保持常规告警

        return False
    except subprocess.TimeoutExpired:
        log("[鉴权巡检] 超时(本轮跳过)")
        _auth_fast_mode = True
    except Exception as e:
        log(f"[鉴权巡检] 异常(忽略): {e}")
        _auth_fast_mode = True
    return False


def main():
    global _last_audit
    ap = argparse.ArgumentParser(description="AI 服务内存看门狗 v2")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--audit", action="store_true", help="只跑一次跨机鉴权巡检并退出")
    args = ap.parse_args()

    if args.audit:
        ok = run_auth_audit(heal=False)
        sys.exit(0 if ok else 1)

    # 单实例保护只作用于常驻守护模式；--once/--audit 是一次性取样，不参与抢锁。
    if not args.once and not _acquire_singleton():
        print("[mem_watchdog] 已有常驻实例在运行，本实例退出——单实例保护。")
        sys.exit(0)

    log("=" * 60)
    log(f"内存看门狗 v2 启动 | 间隔{INTERVAL}s 硬阈值{HARD_PCT:.0f}% 软阈值{SOFT_PCT:.0f}% "
        f"可用下限{MIN_AVAIL_GB:.0f}G 冷却{COOLDOWN}s try_gc={int(TRY_GC)} "
        f"gc冷却{GC_COOLDOWN}s {'[dry-run]' if args.dry_run else ''}")
    if AUDIT_EVERY_SEC > 0:
        log("鉴权巡检: 常规%ds / 异常高频%ds / 自愈=%s / 自愈冷却%ds" % (
            AUDIT_EVERY_SEC, AUDIT_FAST_SEC,
            "开" if AUDIT_HEAL_ENABLED else "关", AUDIT_HEAL_COOLDOWN))
    if REMOTE_HEALTH_SEC > 0:
        log("远程服务巡检: 间隔%ds / 连续失败阈值%d / SSH自愈冷却%ds / 用户=%s" % (
            REMOTE_HEALTH_SEC, REMOTE_FAIL_THRESHOLD, REMOTE_HEAL_COOLDOWN, REMOTE_SSH_USER))
    log("硬上限: " + ", ".join(f"{s['name']}={s['cap_gb']:.0f}G" for s in SERVICES))
    log("=" * 60)

    if args.once:
        check_once(dry_run=args.dry_run)
        run_remote_health_audit(dry_run=args.dry_run)
        return
    # 首次巡检延后 ~2 分钟，避开开机/服务重启的瞬态
    _last_audit = time.time() - _audit_interval() + 120
    try:
        while True:
            try:
                check_once(dry_run=args.dry_run)
                run_remote_health_audit(dry_run=args.dry_run)
                iv = _audit_interval()
                if iv > 0 and (time.time() - _last_audit) >= iv:
                    _last_audit = time.time()
                    run_auth_audit()
            except Exception as e:
                log(f"检查异常(忽略继续): {e}")
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，退出。")


if __name__ == "__main__":
    main()
