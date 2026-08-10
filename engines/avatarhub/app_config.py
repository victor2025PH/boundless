# -*- coding: utf-8 -*-
"""
app_config.py — 统一配置 / 路径解析层（纯标准库，零新依赖）

设计目标（P0 工程地基）：
  1. 消除所有硬编码绝对路径（C:\\模仿音色、C:\\Users\\<user>\\Miniconda3\\...）。
  2. 项目根目录由本文件自身位置自动推导 —— 换盘符 / 换用户名 / 换机器无需改代码。
  3. conda 环境的 python.exe 多策略自动探测 + 环境变量覆盖（修掉 user/Administrator 不一致问题）。
  4. 端口与子服务清单单一真相，供 Hub / _doctor / 启动器共用。
  5. 可选 config.json 覆盖层（仅用于真正的跨机差异：非标准 conda 根、模型在别的盘等）。

所有微服务（不同 conda 环境）均可 `import app_config`，因为它只依赖标准库。

覆盖优先级：环境变量 > config.json > 自动探测 / 默认值。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

# ── 项目根目录：自动推导（本文件所在目录即项目根） ──────────────────
#   既不依赖中文路径，也不需要任何配置；复制到任意路径即生效。
#   环境变量 AVATARHUB_BASE 可显式覆盖（极少需要）。
def _detect_base() -> Path:
    env = os.environ.get("AVATARHUB_BASE")
    if env:
        return Path(env).resolve()
    # 打包冻结态（PyInstaller）：__file__ 指向临时解包目录，须改用 exe 所在目录，
    # 这样安装后 exe 旁边的服务脚本 / 配置才能被正确定位。
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

BASE: Path = _detect_base()
BASE_DIR: Path = BASE  # 兼容别名（历史代码常用 BASE_DIR）

# ── 可选配置覆盖层 config.json（标准库 json，存在才读，不存在零影响） ──
def _load_config() -> dict:
    cfg_path = BASE / "config.json"
    if cfg_path.exists():
        try:
            return json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

CONFIG: dict = _load_config()


def config_path() -> Path:
    """config.json 路径（可选覆盖层，存在才生效）。"""
    return BASE / "config.json"


def update_config(patch: dict) -> dict:
    """把 patch 合并写入 config.json 并刷新内存 CONFIG（不存在则新建）。
    顶层 dict 值做一层浅合并（如 stt_sla 内部字段增量更新，不覆盖整块）；其余键直接替换。
    保留文件内已有的 _comment_* 等键。返回写入后的完整配置。"""
    global CONFIG
    path = config_path()
    cur: dict = {}
    if path.exists():
        try:
            cur = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            cur = {}
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(cur.get(k), dict):
            cur[k].update(v)
        else:
            cur[k] = v
    path.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    CONFIG = cur
    return cur


def stt_sla() -> dict:
    """STT 实时闭环压测的 SLA 达标线。
    优先级：环境变量 AVATARHUB_SLA_* > config.json["stt_sla"] > 内置默认。
    返回 {first_p95(ms,int), final_p95(ms,int), ok_rate(0~1,float), target_c(int,0=阶梯最大档)}。"""
    cfg = CONFIG.get("stt_sla", {}) or {}

    def _pick(key, env, default, cast):
        v = os.environ.get(env)
        if v not in (None, ""):
            try:
                return cast(v)
            except Exception:
                pass
        if cfg.get(key) is not None:
            try:
                return cast(cfg[key])
            except Exception:
                pass
        return default

    return {
        "first_p95": _pick("first_p95", "AVATARHUB_SLA_FIRST_P95", 700, int),
        "final_p95": _pick("final_p95", "AVATARHUB_SLA_FINAL_P95", 1500, int),
        "ok_rate": _pick("ok_rate", "AVATARHUB_SLA_OK_RATE", 0.95, float),
        "target_c": _pick("target_c", "AVATARHUB_SLA_CONCURRENCY", 0, int),
    }


def p(*parts) -> Path:
    """构造项目根下的路径：app_config.p('logs', 'avatar_hub.log')"""
    return BASE.joinpath(*parts)


# ── 常用目录（可被 config.json 覆盖，默认在项目根下） ───────────────
def _dir(key: str, *default_parts) -> Path:
    override = CONFIG.get("dirs", {}).get(key)
    if override:
        return Path(override)
    return BASE.joinpath(*default_parts)

LOGS_DIR: Path = _dir("logs", "logs")
STATIC_DIR: Path = _dir("static", "static")
DATA_DIR: Path = _dir("data", "data")
MODELS_DIR: Path = _dir("models", "models")


# ── conda 环境 python.exe 解析 ──────────────────────────────────────
#   多策略探测 conda 根目录；按环境名拼出 python.exe。
#   优先级：AVATARHUB_PY_<ENV> 环境变量 > config.json["conda_python"][env]
#           > <conda_root>\envs\<env>\python.exe（自动探测的 conda_root）。
def _detect_conda_root() -> Optional[Path]:
    # 1) 显式覆盖
    explicit = os.environ.get("AVATARHUB_CONDA_ROOT") or CONFIG.get("conda_root")
    if explicit:
        return Path(explicit)

    # 2) 从当前解释器反推：...\Miniconda3\envs\<name>\python.exe -> ...\Miniconda3
    exe = Path(sys.executable).resolve()
    parts = list(exe.parts)
    if "envs" in parts:
        i = parts.index("envs")
        return Path(*parts[:i])

    # 3) 当前解释器可能就是 base 环境（root\python.exe）
    if (exe.parent / "condabin").exists() or (exe.parent / "envs").exists():
        return exe.parent

    # 4) 环境变量 CONDA_PREFIX / CONDA_EXE
    cp = os.environ.get("CONDA_PREFIX")
    if cp:
        cpp = Path(cp)
        if cpp.name and cpp.parent.name == "envs":
            return cpp.parent.parent
        return cpp
    ce = os.environ.get("CONDA_EXE")  # ...\Scripts\conda.exe -> root
    if ce:
        return Path(ce).parent.parent

    # 5) 常见安装位置
    candidates = []
    up = os.environ.get("USERPROFILE")
    if up:
        candidates += [Path(up) / "Miniconda3", Path(up) / "Anaconda3"]
    la = os.environ.get("LOCALAPPDATA")
    if la:
        candidates += [Path(la) / "Continuum" / "miniconda3"]
    candidates += [Path(r"C:\ProgramData\Miniconda3"), Path(r"C:\ProgramData\Anaconda3")]
    for c in candidates:
        if (c / "envs").exists() or (c / "python.exe").exists():
            return c
    return None


CONDA_ROOT: Optional[Path] = _detect_conda_root()


def conda_python(env: str) -> str:
    """返回指定 conda 环境的 python.exe 绝对路径（字符串）。

    解析顺序：环境变量 AVATARHUB_PY_<ENV> > config.json["conda_python"][env]
              > <CONDA_ROOT>\\envs\\<env>\\python.exe。
    探测不到时回退当前解释器（best-effort，调用方应自行校验存在性）。
    """
    ev = os.environ.get("AVATARHUB_PY_" + env.upper())
    if ev:
        return ev
    cfg = CONFIG.get("conda_python", {}).get(env)
    if cfg:
        return cfg
    if CONDA_ROOT is not None:
        return str(CONDA_ROOT / "envs" / env / "python.exe")
    return sys.executable


def env_installed(env: str) -> bool:
    """该运行环境是否真实就位（首启向导装的 runtime\\envs、conda 环境或显式配置均可）。

    conda_python() 的「回退当前解释器」不算就位——除非当前解释器本就属于该环境
    （生产机直接用某环境的 python 跑服务的情形）。供健康面板 / 进程守护 / 预热按
    「已安装」裁剪：未购档位的服务显示「未安装」而非「掉线」，也不再被反复拉起刷错
    （2026-07-12 Lite 装机实锤：[Sup] fish_tts/stt 每 20s 一条熔断告警刷屏）。"""
    try:
        from pathlib import Path as _P
        p = _P(conda_python(env))
        if p.name.lower() != "python.exe" or not p.exists():
            return False
        if p.resolve() == _P(sys.executable).resolve():
            return p.parent.name.lower() == env.lower()
        return True
    except Exception:
        return False


# ── 端口与子服务地址 ────────────────────────────────────────────────
#   svc_url 兼容 avatar_hub 原有 _svc_url：环境变量 SVC_<KEY> 可指向远程机。
DEFAULT_PORTS = {
    "faceswap":    8000,
    "tts":         7851,
    "hair":        8001,
    "makeup":      8004,
    "rvc":         6242,
    "lipsync":     8090,
    "latentsync":  8091,
    "enhance":     8092,
    "emotion_tts": 7852,
    "fish_tts":    7855,
    "voxcpm":      7856,
    "qwen3_tts":   7858,
    "sbv2_tts":    7861,
    "singing":     7853,
    "ace_studio":  7859,
    "stt":         7854,
    "nemo_stt":    7857,
    "vcam":        7870,
    "hub":         9000,
    "interpreter": 7900,
    "monitor":     7878,   # 手机无线终端中继(monitor_relay，https=port+1)
    "faceswap2":   8003,   # 换脸容灾副本(同脚本第二实例)
}


# ── 端口覆盖层（两套安装并存，2026-07-17） ──────────────────────────
#   目的：同一台机器上「安装版 + 源码开发版」不打架——给其中一套整体挪端口段。
#   覆盖优先级：环境变量 AVATARHUB_PORT_OFFSET > config.json["port_offset"]；
#   精确覆盖：config.json["ports"] = {"hub": 9100, "interpreter": 8000, ...}（单个服务指定，
#   优先于 offset）。默认零配置=零偏移，所有端口与历史完全一致（零回归）。
#   示例（开发副本的 config.json）：{"port_offset": 2000}
#   → hub 11000 / 同传 9900... 整段平移；port_guard 仍兜底防真撞。
#   ⚠ 偏移值有坑：+100 让同传 7900→8000 撞出厂换脸口，+1000 让换脸 8000→9000 撞出厂 hub。
#   2000 已验证与出厂端口集无交集；换别的值时 _port_collision_check 会当场警告(回归测试盯防)。
def _port_layer() -> tuple[int, dict]:
    off = 0
    try:
        off = int(os.environ.get("AVATARHUB_PORT_OFFSET") or CONFIG.get("port_offset") or 0)
    except Exception:
        off = 0
    exact: dict = {}
    for k, v in (CONFIG.get("ports") or {}).items():
        try:
            exact[str(k)] = int(v)
        except Exception:
            continue
    return off, exact


PORT_OFFSET, _PORTS_EXACT = _port_layer()


def _eff_port(key: str, default: int) -> int:
    """服务 key 的生效端口：精确覆盖 > 整体偏移 > 默认。"""
    if key in _PORTS_EXACT:
        return _PORTS_EXACT[key]
    return int(default) + PORT_OFFSET


if PORT_OFFSET or _PORTS_EXACT:
    DEFAULT_PORTS = {k: _eff_port(k, v) for k, v in DEFAULT_PORTS.items()}


# ── 启动器静态默认(SVC_*)兜底 ────────────────────────────────────────
#   集群路由的单一真相写在 env_config.bat / deploy.env.bat（被各 .bat 启动链 call 注入）。
#   但 doctor/acceptance/selfcheck 常从「裸终端」直跑——没 call 过 bat 就看不到 SVC_*，
#   会拿本机默认端口去探远程服务 → 全链路自检报「核心服务未就绪」假红旗（2026-07-06 实锤）。
#   兜底：环境变量缺失时，静态解析这两个 bat 里的 set "SVC_X=..."（含 if not defined 形式，
#   语义本就是"默认值"）。仅收字面量(不含 %展开%)、跳过 rem/:: 注释行；deploy.env.bat 后读=覆盖。
def _bat_svc_defaults() -> dict:
    import re as _re
    out: dict = {}
    for name in ("env_config.bat", "deploy.env.bat"):
        p = BASE / name
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for ln in text.splitlines():
            s = ln.strip()
            low = s.lower()
            if low.startswith(("rem", "::")):
                continue
            m = _re.search(r'set\s+"(SVC_[A-Z0-9_]+)=([^"%]+)"', s, _re.I)
            if m:
                out[m.group(1).upper()] = m.group(2).strip()
    return out


_BAT_SVC_DEFAULTS: Optional[dict] = None


def svc_url(key: str, default: Optional[str] = None) -> str:
    """子服务地址：SVC_<KEY> 环境变量覆盖（三机分工用）＞启动器 bat 静态默认＞本机默认端口。"""
    global _BAT_SVC_DEFAULTS
    if default is None:
        port = DEFAULT_PORTS.get(key, 0)
        default = f"http://127.0.0.1:{port}"
    name = "SVC_" + key.upper()
    v = os.environ.get(name)
    if v:
        return v.rstrip("/")
    if _BAT_SVC_DEFAULTS is None:
        try:
            _BAT_SVC_DEFAULTS = _bat_svc_defaults()
        except Exception:
            _BAT_SVC_DEFAULTS = {}
    return _BAT_SVC_DEFAULTS.get(name, default).rstrip("/")


# ── 子服务清单（单一真相）：启动器 / _doctor / 健康监控共用 ──────────
#   env:    所属 conda 环境名（用 conda_python(env) 解析解释器）
#   script: 入口脚本（相对项目根）
#   port:   监听端口
#   health: 健康检查路径
#   core:   是否核心链路（实时对话最小集）
#   gpu:    是否占用 GPU（用于显存编排提示）
#   delay:  启动就绪宽限秒数（重模型加载慢，启动器据此设探测超时）
#   label:  人类可读名称（启动器/控制台展示）
SERVICES = {
    "fish_tts":    {"env": "fishspeech",  "script": "fish_speech_server.py", "port": 7855, "health": "/health", "core": True,  "gpu": True,  "delay": 20, "label": "克隆音 TTS (Fish-Speech)"},
    "voxcpm":      {"env": "voxcpm",      "script": "voxcpm_server.py",      "port": 7856, "health": "/health", "core": False, "gpu": True,  "delay": 20, "label": "克隆音 TTS (VoxCPM2·可商用)"},
    "qwen3_tts":   {"env": "qwen3tts",    "script": "qwen3_tts_server.py",   "port": 7858, "health": "/health", "core": False, "gpu": True,  "delay": 20, "label": "克隆音 TTS (Qwen3-TTS·低延迟流式)"},
    "sbv2_tts":    {"env": "sbv2",        "script": "sbv2_tts_server.py",    "port": 7861, "health": "/health", "core": False, "gpu": True,  "delay": 25, "label": "日语情感 TTS (SBV2 JP-Extra·林小玲)"},
    "stt":         {"env": "cosytts",     "script": "stt_server.py",         "port": 7854, "health": "/health", "core": True,  "gpu": True,  "delay": 15, "label": "语音转文字 (Whisper)"},
    "nemo_stt":    {"env": "nemoasr",     "script": "nemotron_stt_server.py","port": 7857, "health": "/health", "core": False, "gpu": True,  "delay": 20, "label": "语音转文字 (Nemotron3.5·流式)"},
    "lipsync":     {"env": "musethepeak", "script": "lipsync_server.py",     "port": 8090, "health": "/health", "core": True,  "gpu": True,  "delay": 30, "label": "口型同步 (MuseTalk+活体)"},
    "vcam":        {"env": "facefusion",  "script": "vcam_server.py",        "port": 7870, "health": "/health", "core": True,  "gpu": True,  "delay": 10, "label": "广播中枢 (OBS+WebRTC)"},
    "hub":         {"env": "facefusion",  "script": "avatar_hub.py",         "port": 9000, "health": "/health", "core": True,  "gpu": False, "delay": 8,  "label": "AvatarHub 中枢"},
    # 产品定位＝直播换脸优先：faceswap 是核心链路（随「开始直播换脸」一起启动，并计入就绪判定）。
    "faceswap":    {"env": "facefusion",  "script": "faceswap_api.py",       "port": 8000, "health": "/health", "core": True,  "gpu": True,  "delay": 12, "label": "直播换脸 (FaceFusion)"},
    # S6 容灾瘦身副本：同脚本第二实例(8003)，FACESWAP_PORT 区分端口；不加载 GFPGAN/CodeFormer(省 ~2GB，
    # 5090 与 TTS/LLM 共卡)、LRU=2(inswapper+1 DFM)。生产 faceswap(.104) 掉线时 Hub /faceswap 自动改道至此。
    "faceswap2":   {"env": "facefusion",  "script": "faceswap_api.py",       "port": 8003, "health": "/health", "core": False, "gpu": True,  "delay": 12, "label": "换脸副本(容灾)",
                    "env_extra": {"FACESWAP_PORT": "8003", "FACESWAP_LOAD_ENHANCE": "0",
                                  "FACESWAP_MODEL_LRU": "2", "FACESWAP_ENH_CONCURRENCY": "1"}},
    "tts":         {"env": "rvc",         "script": "tts_api.py",            "port": 7851, "health": "/health", "core": False, "gpu": True,  "delay": 15, "label": "Coqui TTS (XTTS)"},
    "hair":        {"env": "facefusion",  "script": "hair_api.py",           "port": 8001, "health": "/health", "core": False, "gpu": True,  "delay": 12, "label": "发型·离线"},
    "tryon":       {"env": "fitdit",      "script": "tryon_api.py",          "port": 8002, "health": "/health", "core": False, "gpu": True,  "delay": 45, "label": "虚拟试衣·离线(FitDiT)"},
    "videotryon":  {"env": "fitdit",      "script": "videotryon_api.py",     "port": 8006, "health": "/health", "core": False, "gpu": True,  "delay": 10, "label": "动态试衣·离线(CatV2TON 视频)"},
    "makeup":      {"env": "facefusion",  "script": "makeup_api.py",         "port": 8004, "health": "/health", "core": False, "gpu": False, "delay": 8,  "label": "妆容定妆·离线"},
    "dfm_lab":     {"env": "facefusion",  "script": "dfm_lab_server.py",     "port": 8005, "health": "/health", "core": False, "gpu": False, "delay": 8,  "label": "角色库·整脸换脸·离线"},
    "enhance":     {"env": "facefusion",  "script": "enhance_server.py",     "port": 8092, "health": "/health", "core": False, "gpu": True,  "delay": 10, "label": "人脸增强 (GFPGAN)"},
    "latentsync":  {"env": "latentsync",  "script": "latentsync_server.py",  "port": 8091, "health": "/health", "core": False, "gpu": True,  "delay": 10, "label": "高清口型 (LatentSync)"},
    "emotion_tts": {"env": "cosytts",     "script": "emotion_tts_server.py", "port": 7852, "health": "/health", "core": False, "gpu": True,  "delay": 15, "label": "情感 TTS (CosyVoice)"},
    "singing":     {"env": "ymsvc",       "script": "song_studio_server.py", "port": 7853, "health": "/health", "core": False, "gpu": True,  "delay": 20, "label": "唱歌工作室 (AI 翻唱·YingMusic-SVC)"},
    "ace_studio":  {"env": "ymsvc",       "script": "ace_studio_server.py",  "port": 7859, "health": "/health", "core": False, "gpu": True,  "delay": 15, "label": "原创歌工作室 (ACE-Step 文本成曲)"},
    "interpreter": {"env": "facefusion",  "script": "live_interpreter.py",   "port": 7900, "health": "/health", "core": False, "gpu": False, "delay": 6,  "label": "实时同传 (通译 LingoX)"},
}

# 端口覆盖层落到 SERVICES（单一真相：launcher/doctor/supervisor 全部经此消费端口）。
# faceswap2 的 env_extra["FACESWAP_PORT"] 是子进程实际监听依据，须与生效端口保持一致。
_BASE_SERVICE_PORTS = {k: int(s["port"]) for k, s in SERVICES.items()}   # 出厂端口快照(冲突自检基准)
if PORT_OFFSET or _PORTS_EXACT:
    for _k, _s in SERVICES.items():
        _s["port"] = _eff_port(_k, _s["port"])
        _ee = _s.get("env_extra")
        if _ee and "FACESWAP_PORT" in _ee:
            _ee["FACESWAP_PORT"] = str(_s["port"])


def _port_collision_check() -> list:
    """覆盖层激活时的端口冲突自检（返回冲突描述，同时打印警告，不阻断——port_guard 兜底）。
    查两类：① 生效端口集内互撞（两个服务同端口）；② 偏移后落回出厂端口集
    （偏移选得不巧，如 +100 让同传 7900→8000 正撞另一套安装的换脸口 → 静默串门）。"""
    bad: list = []
    seen: dict = {}
    for k, s in SERVICES.items():
        p = int(s["port"])
        if p in seen:
            bad.append(f"{k}={p} 与 {seen[p]} 同端口(集内互撞)")
        seen[p] = k
    if PORT_OFFSET:
        base_vals = {v: k for k, v in _BASE_SERVICE_PORTS.items()}
        for k, s in SERVICES.items():
            p = int(s["port"])
            if p in base_vals and _BASE_SERVICE_PORTS.get(k) != p:
                bad.append(f"{k} 偏移后={p} 落回出厂端口集(另一套安装的 {base_vals[p]} 可能正用)")
    for _m in bad:
        # 编码安全：app_config 被所有服务 import，Windows 子进程 stdout 常是 GBK——
        # 警告绝不能反把导入炸了(测试实锤 ⚠ 字符在 GBK 下 UnicodeEncodeError)。
        _line = f"[app_config] !! 端口覆盖层冲突: {_m}——请换 port_offset(推荐 2000)或用 ports 精确错开"
        try:
            print(_line, flush=True)
        except UnicodeEncodeError:
            print(_line.encode("ascii", "replace").decode("ascii"), flush=True)
    return bad


PORT_COLLISIONS: list = _port_collision_check() if (PORT_OFFSET or _PORTS_EXACT) else []


def port(key: str) -> int:
    """服务 key 的生效监听端口（含 config.json ports/port_offset 覆盖层）。
    未登记的 key 返回 0（调用方自行兜底）。"""
    if key in SERVICES:
        return int(SERVICES[key]["port"])
    return int(DEFAULT_PORTS.get(key, 0))


# 服务 key → 子进程识别的端口环境变量名（launch 注入的单一真相）。
# 子服务脚本各自认这些 env(历史约定)；覆盖层激活时由 supervisor/service_manager
# 在拉起子进程时注入生效端口，子脚本无需感知覆盖层即可整段平移。
PORT_ENV = {
    "hub": "AVATARHUB_PORT", "interpreter": "INTERP_PORT", "monitor": "MONITOR_PORT",
    "vcam": "VCAM_PORT", "faceswap": "FACESWAP_PORT", "faceswap2": "FACESWAP_PORT",
    "fish_tts": "FISH_PORT", "stt": "STT_PORT", "lipsync": "LIPSYNC_PORT",
    "nemo_stt": "NEMO_STT_PORT", "qwen3_tts": "QWEN3_TTS_PORT", "sbv2_tts": "SBV2_TTS_PORT",
    "voxcpm": "VOXCPM_PORT", "emotion_tts": "EMOTION_TTS_PORT", "singing": "SONG_STUDIO_PORT",
    "ace_studio": "ACE_STUDIO_PORT", "dfm_lab": "DFM_LAB_PORT",
    "tts": "TTS_PORT", "hair": "HAIR_PORT", "makeup": "MAKEUP_PORT",
    "enhance": "ENHANCE_PORT", "latentsync": "LATENTSYNC_PORT",
    "tryon": "TRYON_PORT", "videotryon": "VIDEOTRYON_PORT",
}


def port_env_extra(key: str) -> dict:
    """拉起子服务时应注入的端口环境变量({}=无需注入)。
    仅覆盖层激活时才注入——零配置时不碰子进程环境(零回归)。"""
    if not (PORT_OFFSET or _PORTS_EXACT):
        return {}
    ev = PORT_ENV.get(key)
    return {ev: str(port(key))} if ev else {}


# 外部仓服务目录（自带 repo + 独立 conda env，脚本不在项目根下）：
#   Ditto 实时全脸口型在 C:\ditto，刻意**不**放进上面的 SERVICES —— SERVICES 被
#   doctor/launcher/provision/build_packs 等按「脚本相对项目根 + env 为项目可 provision 环境」消费，
#   外部仓不满足该约定。其进程编排（GPU 总开关 / 自愈）由 service_supervisor.EXTERNAL_SERVICES
#   单独承接，避免把外部仓特例铺到全链路（doctor/打包等无需改动）。
DITTO_DIR = os.environ.get("DITTO_DIR", r"C:\ditto")
#   EchoMimic 全脸音频驱动数字人(离线高清 512)同为外部仓(C:\echomimic + 独立 env)，同形纳管。
ECHOMIMIC_DIR = os.environ.get("ECHOMIMIC_DIR", r"C:\echomimic")


# ── 服务间共享密钥 / 访问控制（GPU 服务面加固，纯标准库） ──────────────
#   威胁模型：fish/tts/faceswap/lipsync/vcam 等推理服务监听 0.0.0.0 且历史上
#   CORS:* 无鉴权，任意同网段机器/页面都能直接调用（白嫖算力 / 越权控制）。
#   方案（默认关闭，全部 opt-in，向后兼容）：服务侧中间件放行以下任一来源——
#     1) 回环(127.0.0.1/::1)：本机工具/同机 hub 永远可用；
#     2) 携带正确 X-AH-Svc 令牌（AVATARHUB_SERVICE_TOKEN，多机各端设同值）；
#     3) 源 IP 命中允许清单（AVATARHUB_SERVICE_ALLOW_IPS，多机部署最省心、hub 零改动）。
#   未配置 token 也未配置 allowlist 时 → 不启用（保持现状）。
_SERVICE_TOKEN_FILE = BASE / "secrets" / "service_token.txt"


def service_token() -> str:
    """服务间共享令牌：环境变量 AVATARHUB_SERVICE_TOKEN 优先，否则读 secrets\\service_token.txt。
    都没有则返回空串（表示未启用令牌校验）。不自动生成，避免多机各自生成不一致。"""
    ev = os.environ.get("AVATARHUB_SERVICE_TOKEN", "").strip()
    if ev:
        return ev
    try:
        if _SERVICE_TOKEN_FILE.exists():
            return _SERVICE_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def service_allow_ips() -> set:
    """允许免令牌直连服务的源 IP 清单（逗号分隔）。多机部署时在各服务机填入 hub 的 IP。"""
    raw = os.environ.get("AVATARHUB_SERVICE_ALLOW_IPS", "").strip()
    return {ip.strip() for ip in raw.split(",") if ip.strip()}


def service_auth_enabled() -> bool:
    """是否启用服务侧访问控制（配置了 token 或 allowlist 任一即启用）。"""
    return bool(service_token() or service_allow_ips())


def service_headers(extra: Optional[dict] = None) -> dict:
    """hub/客户端调用服务时应携带的头（含共享令牌）。未配置令牌则只返回 extra。"""
    h = dict(extra or {})
    tok = service_token()
    if tok:
        h["X-AH-Svc"] = tok
    return h


def service_cors_origins() -> list:
    """服务侧建议放行的浏览器来源（收敛掉 '*'）：hub 来源 + 本机常用端口。
    可用 AVATARHUB_CORS_ORIGINS 覆盖（与 hub 同源策略一致）。"""
    env = os.environ.get("AVATARHUB_CORS_ORIGINS", "").strip()
    if env:
        return [o.strip() for o in env.split(",") if o.strip()]
    hub = svc_url("hub")          # 含 SVC_HUB 远程覆盖
    _hp = DEFAULT_PORTS.get("hub", 9000)
    base = {hub, f"http://127.0.0.1:{_hp}", f"http://localhost:{_hp}"}
    return sorted(o for o in base if o)


def service_url(key: str) -> str:
    """某服务的本机/远程基址（含 SVC_<KEY> 远程覆盖）。"""
    return svc_url(key)


def health_url(key: str) -> str:
    """某服务的完整健康检查 URL。"""
    svc = SERVICES.get(key, {})
    return svc_url(key) + svc.get("health", "/health")


if __name__ == "__main__":
    # 自检：打印解析结果，便于异机排查
    print(f"BASE         = {BASE}")
    print(f"CONDA_ROOT   = {CONDA_ROOT}")
    print(f"LOGS_DIR     = {LOGS_DIR}")
    print(f"STATIC_DIR   = {STATIC_DIR}")
    print(f"config.json  = {'已加载' if CONFIG else '无（使用默认）'}")
    print("conda python 解析：")
    for _env in sorted({s['env'] for s in SERVICES.values()}):
        _py = conda_python(_env)
        print(f"  {_env:14s} -> {_py}  {'[存在]' if Path(_py).exists() else '[缺失]'}")
