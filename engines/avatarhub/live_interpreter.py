# -*- coding: utf-8 -*-
"""
实时双向同传 + 克隆变声(逐句同传，pc_call 场景)
─────────────────────────────────────────────────────────────────────────
方向A  我说中文 → ASR(中) → 翻译(英) → 克隆音色TTS(英) → 播到 VB-Cable 虚拟麦
        → 通话App(把"麦克风"设成 CABLE Output)里对方听到克隆声音的英文。
方向B  对方说英文 → 环回/立体声混音抓"对方声" → ASR(英) → 翻译(中)
        → 屏幕中英双语字幕(不配音)。

依赖(facefusion 环境已具备)：sounddevice、numpy、requests、deep_translator(仅兜底)。
ASR 复用现有 STT 服务(:7854)；翻译默认用 STT 服务内置本地 NMT(MarianMT/GPU，~70ms，离线)，
失败时回退 Google；克隆英文配音直连 Fish(:7855) /v1/tts/clone。

启动:  python live_interpreter.py    →   打开 http://127.0.0.1:7900/
"""
import os, sys, io, time, json, wave, base64, threading, logging, queue, struct, hashlib, asyncio, difflib
from collections import deque, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import numpy as np
import requests
import sounddevice as sd
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, PlainTextResponse, Response, JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [INTERP] %(message)s")
logger = logging.getLogger("interp")

# ── P4-4 环回采集「数据不连续」治理(观测半)：soundcard 遇 WASAPI discontinuity 逐条 warnings
#   刷屏(实测一场 26s 十余条)且只进 stderr 无法量化。此桥折叠为计数指标(30s 合并一条日志)，
#   进 /metrics 音频健康 + 会话落盘；其余 warning 照常放行。根治半在 Capture(加大环回缓冲)。
_discont = {"n": 0, "base": 0, "last_log": 0.0}


class _WarnFold(logging.Filter):
    def filter(self, rec):
        try:
            if "data discontinuity" in rec.getMessage():
                _discont["n"] += 1
                now = time.time()
                if now - _discont["last_log"] >= 30.0:
                    _discont["last_log"] = now
                    logger.warning(f"[环回] 采集数据不连续累计 {_discont['n']} 次(已折叠上报,见音频健康 discont)")
                return False
        except Exception:
            pass
        return True


logging.captureWarnings(True)
logging.getLogger("py.warnings").addFilter(_WarnFold())

# ── P1-P 热路径 HTTP 连接池 ─────────────────────────────────────────────────
# LLM 翻译/克隆 TTS/STT 每句都是独立 requests.post → 每次都重建 TCP 连接。本机 1~3ms,
# 跨机部署(SVC_* 指向 .140/.176 等节点)握手 5~15ms——对逐句逐块的热路径是纯浪费。
# Session keep-alive 复用连接;urllib3 连接池线程安全,与线程池并发调用兼容。
# 仅热路径函数改走此池,其余低频调用(健康探测/管理接口)保持原样以降低改造面。
_HTTP_POOL = requests.Session()
_HTTP_POOL.mount("http://", requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=32))
_HTTP_POOL.mount("https://", requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8))

try:
    import alerts                       # 统一告警(webhook/钉钉/本地toast/state)；缺失则降级为仅日志 + /ops 卡内提示
except Exception:
    alerts = None

# 服务地址:优先 app_config 单一真相(支持 SVC_* 远程部署)，失败回退本机默认。


def _first_url(u: str) -> str:
    """多副本(逗号分隔)服务地址取第一个作直连地址。本进程不做池化分发；此前逗号串被
    原样拼进请求 URL → requests 解析失败(实测 2026-07-09: lipsync 双副本配置下
    人脸预计算/口型生成 100% 报错，直播模式永远回退)。"""
    return (u or "").split(",")[0].strip().rstrip("/")


try:
    import app_config as _ac
    PORT      = int(os.environ.get("INTERP_PORT") or _ac.SERVICES["interpreter"]["port"])
    STT_URL   = os.environ.get("STT_URL")  or _ac.svc_url("stt")
    HUB_URL   = _first_url(os.environ.get("HUB_URL")  or _ac.svc_url("hub"))
    FISH_URL  = _first_url(os.environ.get("FISH_URL") or _ac.svc_url("fish_tts"))
    LIPSYNC_URL = _first_url(os.environ.get("LIPSYNC_URL") or _ac.svc_url("lipsync"))  # 数字人口型
    VCAM_URL    = _first_url(os.environ.get("VCAM_URL")    or _ac.svc_url("vcam"))     # OBS 虚拟摄像头广播
except Exception:
    PORT      = int(os.environ.get("INTERP_PORT", "7900"))
    STT_URL   = os.environ.get("STT_URL", "http://127.0.0.1:7854")
    HUB_URL   = _first_url(os.environ.get("HUB_URL", "http://127.0.0.1:9000"))
    FISH_URL  = _first_url(os.environ.get("FISH_URL", "http://127.0.0.1:7855"))   # 克隆音色 TTS
    LIPSYNC_URL = _first_url(os.environ.get("LIPSYNC_URL", "http://127.0.0.1:8090"))
    VCAM_URL    = _first_url(os.environ.get("VCAM_URL", "http://127.0.0.1:7870"))
try:
    _MON_DEFAULT = f"http://127.0.0.1:{_ac.port('monitor') or 7878}"   # 端口覆盖层(两套并存)对齐
except Exception:
    _MON_DEFAULT = "http://127.0.0.1:7878"
MONITOR_URL = os.environ.get("MONITOR_URL", _MON_DEFAULT)   # 手机无线终端中继
_MON_PORT = (MONITOR_URL.rsplit(":", 1)[-1].split("/")[0] or "7878")   # 页面 JS 注入用(__MON_PORT__)
try:
    _MON_PORT_S = str(int(_MON_PORT) + 1)      # 中继 https 口恒为 http+1(monitor_relay 约定)
except Exception:
    _MON_PORT_S = "7879"
try:
    _HUB_PORT = HUB_URL.rsplit(":", 1)[-1].split("/")[0] or "9000"   # 页面 JS 注入(__HUB_PORT__)
except Exception:
    _HUB_PORT = "9000"
SR        = 16000                      # 送 ASR 的统一采样率
# 克隆声输出增益(线性)。默认 0.5≈-6dB，给虚拟麦留安全余量防爆音(实测原始峰值可达-0.3dBFS)。
TTS_OUT_GAIN = float(os.environ.get("INTERP_TTS_GAIN", "0.5"))
STREAM_TTS = os.environ.get("INTERP_STREAM_TTS", "1") == "1"      # 流式分块配音(边合成边播，降首音延迟)
# HTTP 流式合成:整句一次调用首选引擎 /v1/tts/clone/stream，边到边入队(替代多次独立调用 → 总延迟 -40%、块间无缝)。
STREAM_HTTP = os.environ.get("INTERP_STREAM_HTTP", "1") == "1"

# ── 同传配音 TTS 引擎(可切换) ────────────────────────────────────────────────
# fish(默认·零回归) | qwen3(与 Fish 同协议:/v1/tts/clone + /v1/tts/clone/stream，低延迟流式) |
# cosyvoice(emotion_tts·情感克隆：/v1/tts/clone 带 return_base64 返回同形 JSON；但无“克隆+流式”单端点
#   → 只走分块非流式克隆，仍可边分块边出声)。
# 任何引擎请求失败 → 自动回退 Fish(下一句即恢复)，故切换是低风险的。
INTERP_TTS_ENGINE = os.environ.get("INTERP_TTS_ENGINE", "fish").strip().lower()
# P5d 日语专用 TTS：林小玲 SBV2 JP-Extra 五情绪模型（训练完成后自动接管 ja 语向）
INTERP_JA_TTS_ENGINE = os.environ.get("INTERP_JA_TTS_ENGINE", "sbv2").strip().lower()
# P11 日语混合路由：sbv2(快+音色准) | s1(真笑/哭腔/标记情感) | hybrid(按句择优,默认)。
# LoRA 微调完成前默认 sbv2，避免零样本 S1 音色漂移；合并权重后改 hybrid。
INTERP_JA_TTS_MODE = os.environ.get("INTERP_JA_TTS_MODE", "sbv2").strip().lower()
S1_LORA_PATH = os.environ.get("S1_LORA_PATH", "").strip()   # merge_lora 产物目录(存在则 hybrid 更积极走 S1)


# ── S8-3 STT 单点容灾：主 STT 失联 → 按序试备用端点，主恢复自动切回 ────────────────
#   循证痛点：同传硬实时转写 _stt() 直连单个 STT_URL(.140)，raise_for_status 抛错即整句丢；
#   .140 全宕时 mem_watchdog SSH 拉起有窗口期(数十秒~分钟)，期间转写为 0——换脸早有副本容灾,STT 没有。
#   方案：STT 端点做成有序列表(主 SVC_STT + 备 SVC_STT_FALLBACK)，逐句调用按序 failover；
#     备用可为另一节点(集群)或本机 127.0.0.1:7854(单机起 stt_server)。连接/超时/5xx=节点故障切下一个；
#     4xx=业务错不切(交调用方处理)。金丝雀：容灾期每隔 _STT_PROBE_GAP 秒先重试主端点→通了自动切回。
#   零回归：默认只配主端点(今天 env_config)时，行为与改造前逐字一致(试它→失败即抛)。
_STT_FAIL_THRESH = int(os.environ.get("INTERP_STT_FAIL_THRESH", "2"))
_STT_PROBE_GAP = float(os.environ.get("INTERP_STT_PROBE_GAP", "15"))


def _stt_endpoints() -> list:
    eps = []
    for raw in (os.environ.get("SVC_STT") or STT_URL, os.environ.get("SVC_STT_FALLBACK", "")):
        for u in str(raw).replace(",", "\n").split("\n"):
            u = u.strip().rstrip("/")
            if u and u not in eps:
                eps.append(u)
    return eps or [STT_URL]


_STT_EPS = _stt_endpoints()
_STT_FO_LOCK = threading.Lock()
_STT_FO = {"active": _STT_EPS[0], "on_fallback": False, "fails": 0,
           "engaged_ts": 0.0, "served_by_fallback": 0, "probe_ts": 0.0}


def _stt_alert_engage(base: str):
    logger.warning(f"[STT容灾] 主 STT({_STT_EPS[0]}) 失联 → 切备用端点 {base}")
    if alerts:
        try:
            alerts.raise_alert("stt_failover", "STT 主节点失联·已切备用",
                               detail=f"同传转写主节点({_STT_EPS[0]})连续失败，已切到备用端点 {base}；"
                                      "mem_watchdog 会自动拉起主节点，恢复后本服务自动切回。",
                               level="critical", source="STT容灾")
        except Exception:
            pass


def _stt_alert_recover(served: int):
    logger.info(f"[STT容灾] 主 STT 恢复 → 已切回（备用顶了 {served} 次转写）")
    if alerts:
        try:
            alerts.clear_alert("stt_failover", note=f"主 STT 恢复，已自动切回（备用顶了 {served} 次转写）")
        except Exception:
            pass


def _stt_post(path: str, payload: dict, timeout):
    """向 STT 发 POST，主端点故障按序试备用；全端点失败→抛最后异常(保持改造前 raise 语义)。
    仅连接/超时/5xx 触发 failover；4xx 视为节点存活的业务错，原样返回交调用方 raise_for_status。"""
    eps = _STT_EPS
    now = time.time()
    with _STT_FO_LOCK:
        if _STT_FO["on_fallback"] and now - _STT_FO["probe_ts"] >= _STT_PROBE_GAP:
            start = 0                       # 金丝雀：容灾期定期重试主端点，通了即自动切回
            _STT_FO["probe_ts"] = now
        else:
            start = eps.index(_STT_FO["active"]) if _STT_FO["active"] in eps else 0
    last_exc = None
    for off in range(len(eps)):
        base = eps[(start + off) % len(eps)]
        try:
            r = _HTTP_POOL.post(f"{base}{path}", json=payload, timeout=timeout)
        except Exception as e:              # 连接/超时=节点故障 → 试下一个
            last_exc = e
            with _STT_FO_LOCK:
                _STT_FO["fails"] += 1
            continue
        if r.status_code >= 500:            # 5xx=节点故障 → 试下一个
            last_exc = requests.HTTPError(f"HTTP{r.status_code} from {base}")
            with _STT_FO_LOCK:
                _STT_FO["fails"] += 1
            continue
        _stt_mark_ok(base)                  # 2xx/4xx：节点存活(4xx 交调用方按业务错处理)
        return r
    raise last_exc if last_exc else RuntimeError("STT 全端点不可达")


def _stt_mark_ok(base: str):
    engaged = recovered = None
    with _STT_FO_LOCK:
        _STT_FO["fails"] = 0
        if base != _STT_EPS[0]:                       # 命中备用端点
            _STT_FO["served_by_fallback"] += 1
            if not _STT_FO["on_fallback"]:
                _STT_FO.update(on_fallback=True, active=base, engaged_ts=time.time())
                engaged = base
            else:
                _STT_FO["active"] = base
        else:                                         # 命中主端点
            if _STT_FO["on_fallback"]:
                recovered = _STT_FO["served_by_fallback"]
                _STT_FO["on_fallback"] = False
            _STT_FO["active"] = base
    if engaged:
        _stt_alert_engage(engaged)
    if recovered is not None:
        _stt_alert_recover(recovered)


def _stt_fo_view() -> dict:
    with _STT_FO_LOCK:
        return {"endpoints": len(_STT_EPS), "primary": _STT_EPS[0],
                "active": _STT_FO["active"], "on_fallback": _STT_FO["on_fallback"],
                "served_by_fallback": _STT_FO["served_by_fallback"],
                "engaged_s": (round(time.time() - _STT_FO["engaged_ts"], 1)
                              if _STT_FO["on_fallback"] and _STT_FO["engaged_ts"] else 0)}


def _engine_base(svc_key: str, legacy_env: str, legacy_default: str) -> str:
    """引擎 base URL 解析(集群优先)。2026-07-11 根因实锤：env_config 同时设了集群单一真相
    SVC_EMOTION_TTS=远端 与遗留 COSYVOICE_TTS_URL=127.0.0.1，旧序把本地摆前面 → 情感TTS明明在
    远端跑着(200)却被错指本地空端口、整场降级到 Fish、丢了 cosyvoice 情感/instruct。改为：
      集群 SVC_<KEY>(进程环境显式非空=远端分工的显式部署决定) ＞ 遗留 *_TTS_URL(本机) ＞ svc_url 兜底。
    单机模式把 SVC_* 清空为空串 → 落到遗留本机地址，行为不变；远端若挂，候选链仍有 Fish 兜底,不回归。"""
    svc_env = (os.environ.get("SVC_" + svc_key.upper()) or "").strip()
    if svc_env:
        return _first_url(svc_env)
    legacy = (os.environ.get(legacy_env) or "").strip()
    if legacy:
        return _first_url(legacy)
    try:
        return _ac.svc_url(svc_key)
    except Exception:
        return legacy_default


_cosy_pick = {"ts": 0.0, "url": ""}


def _cosy_url() -> str:
    """cosyvoice(情感TTS)地址 —— 健康感知的本地优先。2026-07-11 深挖：P3a 有意把情感TTS本地化到
    5090(COSYVOICE_TTS_URL=127.0.0.1:7852,更快 1.7~2.5s)，.117 是保温兜底(慢些 ~4s)。旧序把本地
    死端口摆前面 → 情感句撞死本地即回退 Fish、情感从未生效。改为：本地活着就用本地(快)，本地挂了
    退远端(慢但有情感)，都挂再退默认。15s 缓存,不在情感句热路径反复探测(仅情感句/体检时解析)。"""
    now = time.time()
    if _cosy_pick["url"] and now - _cosy_pick["ts"] < 15.0:
        return _cosy_pick["url"]
    local = _first_url((os.environ.get("COSYVOICE_TTS_URL") or "").strip()) if (os.environ.get("COSYVOICE_TTS_URL") or "").strip() else ""
    remote = _first_url((os.environ.get("SVC_EMOTION_TTS") or "").strip()) if (os.environ.get("SVC_EMOTION_TTS") or "").strip() else ""
    if not (local or remote):
        try:
            remote = _ac.svc_url("emotion_tts")
        except Exception:
            remote = "http://127.0.0.1:7852"
    if local and _probe_engine_alive(local):
        pick = local
    elif remote and _probe_engine_alive(remote):
        pick = remote
    else:
        pick = local or remote or "http://127.0.0.1:7852"
    _cosy_pick.update({"ts": now, "url": pick})
    return pick


def _tts_url_for(engine: str) -> str:
    """引擎 → 克隆 TTS 服务 base URL。未知引擎回退 Fish。集群 SVC_ 路由优先(见 _engine_base)；
    cosyvoice 走健康感知本地优先(见 _cosy_url)。"""
    if engine == "qwen3":
        return _engine_base("qwen3_tts", "QWEN3_TTS_URL", "http://127.0.0.1:7858")
    if engine == "cosyvoice":
        return _cosy_url()
    if engine == "sbv2":
        return _engine_base("sbv2_tts", "SBV2_TTS_URL", "http://127.0.0.1:7861")
    if engine == "s1":
        return os.environ.get("S1_TTS_URL", "http://127.0.0.1:7863")
    return FISH_URL


# SBV2 是「按人设训练」的模型(林小玲东京女声)，不是通用克隆引擎——只有白名单人设
# 的日语才接管；其他人设(如硬汉先生男声)仍走 cosyvoice/fish 克隆链路,音色跟人。
# (2026-07-10 实测教训：用户用硬汉先生测中→日,若不设门禁会被换成女声。)
_SBV2_PROFILES = set(x.strip() for x in (os.environ.get(
    "INTERP_SBV2_PROFILES", "林小玲") or "").replace("，", ",").split(",") if x.strip())


def _profile_voice_optional(nm: str) -> bool:
    """该人设在当前语向下配音可不依赖克隆参考音(SBV2 按人设训练的模型接管)。"""
    return ((_DST_LANG or "").split("-")[0].lower() == "ja"
            and INTERP_JA_TTS_ENGINE == "sbv2"
            and (nm or "") in _SBV2_PROFILES)


def _ja_use_sbv2() -> bool:
    """日语语向 + SBV2 引擎已配置 + 活动人设在白名单 → 用林小玲 JP-Extra 五情绪模型。"""
    return _profile_voice_optional(getattr(ST, "profile", "") or "")


# P11 S1-mini 健康探测(30s 缓存,避免每句打 /health)
_s1_health = {"ok": False, "ts": 0.0}
# S1 原生非言语标记比 SBV2 素材库更真：哭腔(sobbing)/害怕(scared)/真笑(laughing)
_EMO_S1_NONVERBAL = {"sad", "fearful"}


def _s1_ready() -> bool:
    now = time.time()
    if now - _s1_health["ts"] < 30:
        return _s1_health["ok"]
    ok = False
    try:
        r = requests.get(f"{_tts_url_for('s1')}/health", timeout=2)
        j = r.json() if r.ok else {}
        ok = bool(j.get("model_loaded"))
    except Exception:
        pass
    _s1_health["ok"] = ok
    _s1_health["ts"] = now
    return ok


def _s1_lora_ready() -> bool:
    """LoRA 合并权重已落盘 → hybrid 可把强情感句也交给 S1(音色已贴近林小玲)。"""
    if not S1_LORA_PATH:
        return False
    p = os.path.join(S1_LORA_PATH, "model.pth")
    return os.path.isfile(p)


def _ja_route_s1(emotion: str = "", laugh: bool = False, style_weight: float = 0.0) -> bool:
    """日语混合路由：笑意/哭腔/强情感(LoRA 后) → S1；其余 → SBV2。"""
    if not _ja_use_sbv2():
        return False
    if INTERP_JA_TTS_MODE == "sbv2":
        return False
    if not _s1_ready():
        return False
    if INTERP_JA_TTS_MODE == "s1":
        return True
    # hybrid：S1 擅长非言语/高唤醒标记；平叙仍 SBV2(延迟低、JVNV 音色稳)
    if laugh:
        return True
    emo = (emotion or "").strip().lower()
    if emo in _EMO_S1_NONVERBAL and style_weight >= float(os.environ.get("INTERP_EMO_W_NORMAL", "1.8")):
        return True
    if emo in _EMO_STRONG and (_s1_lora_ready() or style_weight >= float(os.environ.get("INTERP_EMO_W_STRONG", "2.4"))):
        return True
    return False


def _resolve_tts_engine(emotion: str = "", laugh: bool = False, style_weight: float = 0.0) -> str:
    if _ja_route_s1(emotion, laugh, style_weight):
        return "s1"
    if _ja_use_sbv2():
        return "sbv2"
    return INTERP_TTS_ENGINE


def _tts_urls(emotion: str = "", laugh: bool = False, style_weight: float = 0.0) -> list:
    """按优先级返回配音 base URL 列表：首选引擎 → 兜底(去重)。"""
    eng = _resolve_tts_engine(emotion, laugh, style_weight)
    urls = [_tts_url_for(eng)]
    if eng == "s1":                        # S1 失败 → SBV2(林小玲 JVNV) → Fish
        sbv2u = _tts_url_for("sbv2")
        if sbv2u not in urls:
            urls.append(sbv2u)
    if FISH_URL not in urls:
        urls.append(FISH_URL)
    return urls


# ── 无音色守卫 ──────────────────────────────────────────────────────────────
# 角色缺参考音时克隆配音必然失败(cosyvoice 400"reference_audio_b64 不能为空"/fish 400/500)。
# 2026-07-10 实测事故：切到无音色的「阳光型男」再切语向重启,整场配音静默失败、UI 零提示,
# 用户以为是"切语种没生效"。故源头跳过(不再每句撞 2~3 个引擎刷 traceback) + 30s 节流告警。
_novoice = {"ts": 0.0, "n": 0}


def _voice_ready() -> bool:
    """当前配置能出配音：有克隆参考音，或该人设在此语向走 SBV2 训练模型(无需参考音)。"""
    return bool(ST.voice_b64) or _resolve_tts_engine() == "sbv2"


# ── 配音引擎可达性探测（10s 缓存）──────────────────────────────────────────
# 2026-07-10 无声事故的另一半根因：不只是"角色无音色"，还有 TTS 引擎本身报错(cosy 422/fish 500)。
# 当时 voice_ok 只查了参考音有无 → 引擎挂了照样"全绿却整场无声"。此处补齐：探首选+兜底引擎的
# /health，任一可达即算"引擎在线"(出声链路走兜底链)；全挂=红灯明说，别再让用户对着静默猜。
_tts_health_cache = {"ts": 0.0, "ok": None, "detail": ""}
# 配音引擎 → 主控台(Hub)可启停的服务名(app_config.SERVICES)。供"一键拉起"把挂掉的引擎重新拉起来。
# s1(7863) 无独立服务登记 → 不列(不可自动拉起)；voxcpm 非同传候选,略。
_ENGINE_SVC = {"fish": "fish_tts", "cosyvoice": "emotion_tts", "qwen3": "qwen3_tts", "sbv2": "sbv2_tts"}


def _url_to_engine(url: str) -> str:
    """把候选 URL 反解成引擎名(fish/cosyvoice/…)。认不出就退回端口号,至少不误导。"""
    u = (url or "").rstrip("/")
    for name in ("fish", "cosyvoice", "qwen3", "sbv2", "s1"):
        try:
            if _tts_url_for(name).rstrip("/") == u:
                return name
        except Exception:
            pass
    if (FISH_URL or "").rstrip("/") == u:
        return "fish"
    return u.rsplit(":", 1)[-1] or u


def _probe_engine_alive(url: str) -> bool:
    """探一个引擎进程是否在监听：拿到任何 HTTP 应答(含 404/405)都算在线(有的 TTS 无 /health
    路由却能合成)；只有连不上/超时(进程没起)才算 dead。合成端 500/422 属入参问题,不在此判。"""
    try:
        requests.get(f"{url.rstrip('/')}/health", timeout=2)
        return True
    except requests.exceptions.RequestException as e:
        if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
            return False
        return True
    except Exception:
        return False


def _tts_engines_detail() -> list:
    """当前语向候选链(首选→兜底)逐个引擎的健康明细：{engine, url, port, primary, alive, svc, launchable}。
    供「配音引擎体检」红绿灯与「一键拉起」按钮渲染。"""
    out, seen = [], set()
    for i, url in enumerate(_tts_urls()):
        u = (url or "").rstrip("/")
        if not u or u in seen:
            continue
        seen.add(u)
        eng = _url_to_engine(u)
        out.append({"engine": eng, "url": u, "port": u.rsplit(":", 1)[-1],
                    "primary": (i == 0), "alive": _probe_engine_alive(u),
                    "svc": _ENGINE_SVC.get(eng), "launchable": eng in _ENGINE_SVC})
    return out


def _tts_engine_health(ttl: float = 10.0) -> dict:
    """探测配音引擎候选链(首选→兜底)的 /health。返回 {ok, detail, alive:[...], primary_ok}。
    只要有一个引擎在线就算可出声(合成时本就按候选链逐个兜底)。10s 缓存防每次开播刷探测。
    tag 用引擎名(fish/cosyvoice)而非端口，话术对用户可读；并单列"主引擎是否在线"。"""
    now = time.time()
    if _tts_health_cache["ok"] is not None and now - _tts_health_cache["ts"] < ttl:
        return {"ok": _tts_health_cache["ok"], "detail": _tts_health_cache["detail"],
                "primary_ok": _tts_health_cache.get("primary_ok"),
                "primary": _tts_health_cache.get("primary"), "cached": True}
    alive, dead, primary_ok, primary = [], [], None, None
    for i, url in enumerate(_tts_urls()):
        if not url:
            continue
        eng = _url_to_engine(url)
        ok1 = _probe_engine_alive(url)
        if i == 0:
            primary, primary_ok = eng, ok1
        (alive if ok1 else dead).append(eng)
    alive = list(dict.fromkeys(alive)); dead = list(dict.fromkeys(dead))
    ok = len(alive) > 0
    if ok:
        detail = f"引擎在线: {'/'.join(alive)}"
        if primary is not None and not primary_ok:      # 主引擎挂了但有兜底：明确告知在走兜底
            detail += f"；主引擎「{primary}」未响应，正走兜底(点『拉起配音引擎』可恢复)"
        elif dead:
            detail += f"；未响应: {'/'.join(dead)}"
    else:
        detail = f"全部配音引擎连不上({'/'.join(dead) or '无候选'})：对方将听不到配音，请检查 Fish/CosyVoice 服务是否在跑"
    _tts_health_cache.update({"ts": now, "ok": ok, "detail": detail,
                              "primary_ok": primary_ok, "primary": primary})
    return {"ok": ok, "detail": detail, "alive": alive, "dead": dead,
            "primary_ok": primary_ok, "primary": primary}


def _dub_ready() -> dict:
    """配音能否真正出声 = 有音色样本 且 至少一个 TTS 引擎在线。两半都查,分别给因。"""
    v = _voice_ready()
    h = _tts_engine_health()
    ok = bool(v and h["ok"])
    if ok:
        reason = "克隆参考音就绪 · " + h["detail"]
    elif not v and not h["ok"]:
        reason = "无音色样本，且配音引擎不可达 —— 对方只能看字幕"
    elif not v:
        reason = _voice_missing_hint()
    else:
        reason = h["detail"]
    return {"ok": ok, "voice_ok": v, "engine_ok": h["ok"], "reason": reason}


_dub_kick = {"ts": 0.0}


def _kick_dub_engine_async(engine: str = ""):
    """配音引擎不可达时,后台经主控台(Hub /api/engine/start)自动拉起。非阻塞、限频(60s),
    不改变就绪判定——只把进程拉起来,省得用户手动点。开播自检发现引擎挂时自动触发。"""
    now = time.time()
    if now - _dub_kick["ts"] < 60:
        return
    _dub_kick["ts"] = now
    eng = (engine or _resolve_tts_engine() or INTERP_TTS_ENGINE or "fish").strip().lower()
    svc = _ENGINE_SVC.get(eng)
    if not svc:
        return

    def _go():
        try:
            requests.post(f"{HUB_URL}/api/engine/start", params={"name": svc}, timeout=120)
            _tts_health_cache["ok"] = None      # 作废健康缓存,下次探测取真值
            ST.push_event({"who": "sys", "warn":
                           f"🔧 配音引擎「{eng}」未在线，已在后台自动拉起…十几秒后点『试音』确认"})
        except Exception:
            logger.warning("自动拉起配音引擎失败(忽略,可手动点『拉起配音引擎』)")
    threading.Thread(target=_go, name="dub-engine-kick", daemon=True).start()


def _voice_missing_hint(nm: str = "") -> str:
    nm = nm or getattr(ST, "profile", "") or "当前角色"
    return (f"🔇 角色「{nm}」无音色样本：配音将无声(字幕/翻译不受影响)。"
            f"请换有音色的角色，或先在角色库为「{nm}」录制/导入音色")


def _voice_probe_hint(nm: str = "") -> str:
    """音色为空时的精准话术：问一次 Hub 分清「角色真没录音色」vs「有音色但拉取/Hub 故障」，
    两者的处置完全不同(录音色 vs 重试/查 Hub)。3s 超时,探测失败按 Hub 故障口径。"""
    name = (nm or "").strip()
    try:
        if not name:
            name = requests.get(f"{HUB_URL}/profiles", timeout=3).json().get("active", "") or "当前角色"
        j = requests.get(f"{HUB_URL}/profiles/{quote(name, safe='')}", timeout=3).json()
        if j.get("has_voice"):
            return (f"⚠ 角色「{name}」在角色库有音色，但本次拉取失败：配音将无声(字幕/翻译不受影响)。"
                    "请停止→重新开始重试；反复出现请查主控台日志")
    except Exception:
        return (f"⚠ 取角色「{name or '当前'}」音色失败(主控台 9000 不可达?)：配音将无声(字幕/翻译不受影响)。"
                "请确认主控台在线后停止→重新开始")
    return _voice_missing_hint(name)


_NOSIG_SEC = float(os.environ.get("INTERP_NOSIGNAL_SEC", "35"))   # 开播后多少秒我方麦零识别→主动提示
_selfhear = {"fixable": False}                # 最近一次开播是否检出"对方声=我的麦同族"且可一键修复
_last_call_req = {"profile": "", "mode": "local", "stream": None}   # 上次通话开播参数(供一键修复/重启复用)
_selfheal = {"ts": 0.0, "log": []}             # 无信号自愈：上次触发时刻(冷却,防死循环) + 留痕(前/后/切到的实例)


def _nosignal_watchdog(session_ts: float):
    """开播 _NOSIG_SEC 秒后，若我方麦(方向A)仍零识别产出且未静音 → 弹一条"没听到你说话"的提示。
    只对本会话有效(session_ts 比对，防停/重启后误触)、只弹一次。这是把"探测瞬间静音"的不确定，
    升级成"真的一直没声音"的确定判断——治 2026-07-10 那种"麦静音全程无字幕、用户以为切语种没生效"。"""
    try:
        time.sleep(_NOSIG_SEC)
    except Exception:
        return
    if not ST.running or ST.session_start != session_ts:
        return                                   # 会话已停/已被新会话替换
    if getattr(ST, "muted", False):
        return                                   # 主动急停期间零识别是正常的
    a_hits = int(ST.stats.get("a", 0)) + int(ST.stream_stats.get("part_a", 0)) + int(ST.stream_stats.get("fin_a", 0))
    if a_hits > 0:
        return                                   # 已经听到你说话，正常
    b_hits = int(ST.stats.get("b", 0)) + int(ST.stream_stats.get("part_b", 0))
    # 自愈判据用采集线程"自己读到的"噪声底(_noise_floor_dbfs)，不去二次开被占用的麦(不可靠)：
    # nf 从未发布或 ≤-85dBFS = 采到数字静音 → 多半是麦"实例坏死/索引漂移"(实测 MME 实例强杀后
    # 读硬零,同名 WASAPI 却正常)。仅当①数字静音 ②物理麦 ③存在可切换的备用实例 ④过冷却期,
    # 才触发一次"已知良好重启"(call_mode_start 内含实例故障转移,把会话钉到有真信号的实例)。
    now = time.time()
    nf_a = _noise_floor_dbfs("a")
    phone_mic = bool(ST.mic_net_url)
    silent = (nf_a is None) or (nf_a <= -85.0)
    try:
        alts = [] if phone_mic else _find_device_all(str(_ap_get().get("mic") or CALL_MIC_NAME), False)
    except Exception:
        alts = []
    if silent and (not phone_mic) and len(alts) > 1 and (now - _selfheal["ts"] > 180):
        _selfheal["ts"] = now
        ST.push_event({"who": "sys", "warn": f"🔧 开播 {int(_NOSIG_SEC)}s 零识别、麦读到数字静音——"
                       "正自动切换到可用的麦克风实例并重启采集链…"})
        logger.warning(f"[无信号自愈] a 零识别 + nf_a={nf_a} 备用实例{len(alts)}个 → 触发已知良好重启(实例故障转移)")
        try:
            kw = dict(profile=_last_call_req.get("profile") or "", mode=_last_call_req.get("mode") or "local")
            if _last_call_req.get("stream") is not None:
                kw["stream"] = _last_call_req["stream"]
            rep = call_mode_start(CallModeReq(**kw))
            ms = next((s for s in rep.get("steps", []) if s["name"] == "麦克风自检"), None)
            mic_after = _dev_name_safe(ST.mic_index) if ST.mic_index is not None else ""
            _selfheal["log"].append({"ts": time.time(), "before_nf_a": nf_a, "alts": len(alts),
                                     "restarted": bool(rep.get("session_running")),
                                     "mic_after": mic_after, "mic_detail": (ms.get("detail", "") if ms else "")})
            _selfheal["log"] = _selfheal["log"][-10:]
            ST.push_event({"who": "sys", "warn": ("✅ 已重启采集，请再说一句试试。" + (ms.get("detail", "") if ms else ""))
                           if rep.get("session_running") else "⚠ 自动重启后仍未就绪，请检查麦克风连接/是否被独占"})
        except Exception as e:
            logger.exception("无信号自愈重启失败")
            _selfheal["log"].append({"ts": time.time(), "before_nf_a": nf_a, "alts": len(alts),
                                     "restarted": False, "error": str(e)[:120]})
            _selfheal["log"] = _selfheal["log"][-10:]
            ST.push_event({"who": "sys", "warn": f"⚠ 自动重启失败({str(e)[:80]})，请手动点『📞 通话向导』重来"})
        return
    # 非数字静音 / 手机麦 / 无备用实例 / 冷却中：多半是没对着麦说话/被静音/被占用 → 给可执行提示
    heal_note = ("（已尝试过自动切换实例仍数字静音，基本可排除软件问题，重点查硬件静音键/隐私开关/独占）"
                 if silent and (now - _selfheal["ts"] <= 180) else "")
    hint = (f"🎤 开播 {int(_NOSIG_SEC)}s 没听到你说话：若已在通话，请确认麦克风没静音、"
            "对着它说话、且没被别的程序独占。" + heal_note
            + ("(已能收到对方声音，问题多半只在你这侧的麦)" if b_hits > 0
               else "(对方声也没收到，一并检查「对方声来源」是否选对)"))
    ST.push_event({"who": "sys", "warn": hint})
    logger.warning(f"[真信号确认] 开播 {_NOSIG_SEC}s 我方麦零识别 (a_hits=0, b_hits={b_hits}, nf_a={nf_a}) → 已提示检查麦")


def _note_novoice_skip():
    """配音因无音色被跳过：计数 + 节流推事件(防每句刷屏)，日志同步留痕。"""
    _novoice["n"] += 1
    now = time.time()
    if now - _novoice["ts"] >= 30:
        _novoice["ts"] = now
        ST.push_event({"who": "sys",
                       "warn": _voice_missing_hint() + f"（本场已跳过 {_novoice['n']} 句配音）"})
        logger.warning(f"配音跳过(无音色参考): 本场累计 {_novoice['n']} 句 "
                       f"profile={getattr(ST, 'profile', '')!r}")


def _novoice_reset():
    """新会话/切角色后清零：新语境下首次跳过立即告警(不被上一角色的节流窗压住)。"""
    _novoice["ts"] = 0.0
    _novoice["n"] = 0


# 是否支持 Fish 式“克隆+流式”单端点(/v1/tts/clone/stream，4字节小端长度前缀 + PCM16)。
# fish/qwen3 原生支持；cosyvoice 自 P4(2026-07-09) 起服务端补齐了同协议克隆流式端点
# (zero_shot+spk缓存,本地 5090)——统一引擎后音色/语速/情感三者一致，qwen3/fish 退居兜底。
_CLONE_STREAM_ENGINES = {"fish", "qwen3", "cosyvoice", "sbv2", "s1"}
# 配音全局语速(cosyvoice 端点消费;fish/qwen3 忽略未知字段)。用户实测要求语速加快 → 默认 1.1。
INTERP_TTS_SPEED = float(os.environ.get("INTERP_TTS_SPEED", "1.1"))


def _engine_supports_clone_stream(engine: str) -> bool:
    return engine in _CLONE_STREAM_ENGINES


# ── P0→P1 情感配音路由(2026-07-09)：规则秒判 + 聊天记录情绪基调(LLM) + 响度佐证 三层融合 ──
#   动机：同传管线此前只把纯文本交给 TTS，情绪信息全丢(播音腔)。P0 只按单句关键词改道，
#   覆盖面太窄(实测用户整场只有零星句子被路由)。P1 引入「上下文情绪状态机」：
#   ① 规则层(emotion_detector,0ms)：单句强情感词/标点 → 立即改道(最快、最准的显式信号)；
#   ② 基调层(本地 LLM,实测 315ms,异步)：每次定稿后把最近 6 条双方对话喂给常驻 LLM 判定
#      「我」当前的情绪基调,写入 45s 有效期的 mood 状态——下一句即便平叙,只要基调仍强也带情感
#      (这正是"根据聊天记录做情感标签"：情绪从上下文继承,不再逐句孤立判定)；
#   ③ 佐证层(响度,0ms)：本句原声很响(≥-15dBFS)时允许沿用基调,轻声句不乱染。
#   输出：CosyVoice3 /v1/tts/instruct(克隆+情感指令+可调语速)。实测情感真实可闻(happy Hello
#   F0 270Hz vs sad 175Hz)、0/5 复读、日文/中文正文均有效；但整句非流式 2~4s，故：
#   · 只有「高唤醒度」标签(默认 excited/angry/sad/surprised,可配)才值得改道；
#   · 播放队列积压≥2 时本句放弃改道(防连续情感句把延迟滚雪球)。
#   否决的替代案：双引擎并行赛跑取先到——qwen3 与 cosyvoice 同挤 .117 一张 12G 卡，并行=互相拖慢。
#   任何失败(超时/引擎宕/无参考音)→ 原路 qwen3→fish 兜底，绝不丢句。运行时 POST /config/emotts 可调。
EMO_TTS_ON      = os.environ.get("INTERP_EMO_TTS", "1") == "1"
EMO_TTS_TIMEOUT = float(os.environ.get("INTERP_EMO_TTS_TIMEOUT", "10"))   # 整句合成上限(超时回退普通引擎)
EMO_LLM_ON      = os.environ.get("INTERP_EMO_LLM", "1") == "1"            # 聊天记录情绪基调(LLM 异步)
EMO_MOOD_TTL    = float(os.environ.get("INTERP_EMO_MOOD_TTL", "45"))      # 基调有效期(秒,无新证据自然衰减)
EMO_LLM_GAP     = float(os.environ.get("INTERP_EMO_LLM_GAP", "3"))        # LLM 判定最小间隔(防高频轰击)
EMO_SPEED       = float(os.environ.get("INTERP_EMO_SPEED", "1.0"))        # 情感句语速(CosyVoice speed 参数)
EMO_LOUD_DBFS   = float(os.environ.get("INTERP_EMO_LOUD_DBFS", "-15"))    # 原声很响的佐证阈值
# 改道情感集：高唤醒度才值得 +2~4s；INTERP_EMO_SET 逗号分隔可自定义(如加 happy)
_EMO_STRONG = set(x.strip() for x in (os.environ.get("INTERP_EMO_SET",
                  "excited,angry,sad,surprised") or "").replace("，", ",").lower().split(",") if x.strip())
_EMO_LABELS = {"excited", "angry", "sad", "surprised", "happy", "fearful",
               "gentle", "calm", "serious", "disgusted", "neutral"}
_EMO_INSTRUCT = {   # 与 emotion_tts_server.EMOTION_INSTRUCT 同表(那端 custom_instruct 直透 instruct2)
    "excited": "用兴奋激动的语气说", "angry": "用愤怒生气的语气说",
    "sad": "用悲伤难过的语气说", "surprised": "用惊讶的语气说",
    "happy": "用开心愉快的语气说", "fearful": "用恐惧害怕的语气说",
    "gentle": "用温柔轻柔的语气说", "calm": "用平静沉着的语气说",
    "serious": "用严肃认真的语气说", "disgusted": "用厌恶的语气说",
}
# P5b 人设本地化(2026-07-10 修订)：地域口语风格只放在 LLM 翻译层(文本层面,零泄漏)；
# TTS 指令保持"纯情绪+语速"——训练数据质量闸实测：长指令会被 CosyVoice3 泄漏进语音
# (把"用自然地道的东京标准日语口语表达"念了出来,30/104 样本翻车里多条此因)。
_EMO_FAST = {"excited", "angry", "surprised"}


def _emo_instruct_for(emotion: str, dst: str) -> str:
    base = _EMO_INSTRUCT.get(emotion, "")
    if not base:
        return ""
    if emotion in _EMO_FAST:
        base += "，语速稍快"
    return base


# P5a 感情词注入：情感句在译文句首加语法安全的感叹词(句中/句尾语气词交给 LLM 翻译层,
# 避免规则层拼错语法)；笑意句给 CosyVoice3 加 [laughter] 细粒度标记(真笑声,标记仅进
# cosyvoice 载荷,兜底引擎拿纯文本防把标记念出来)。
EMO_WORDS_ON = os.environ.get("INTERP_EMO_WORDS", "1") == "1"
_EMO_PREFIX = {
    "ja": {"excited": "やった、", "surprised": "えっ、", "angry": "もう、",
           "sad": "はぁ…", "happy": "わあ、", "fearful": "うわ、"},
    "en": {"excited": "Wow, ", "surprised": "Wait, ", "angry": "Come on, ",
           "sad": "Well... ", "happy": "Hey, ", "fearful": "Oh no, "},
    "zh": {"excited": "哇，", "surprised": "诶？", "angry": "哼，",
           "sad": "唉…", "happy": "嘿，", "fearful": "呀，"},
}
# 强档感叹词(P7)：拉长/带促音+感叹号——SBV2 的韵律跟随文本(JP-Extra BERT 语义条件)，
# 文字越"用力"念得越用力。仅强档情感句用，普通档保持克制版防出戏。
_EMO_PREFIX_STRONG = {
    "ja": {"excited": "やったー！", "surprised": "ええっ！？", "angry": "もう！",
           "sad": "はぁ……", "happy": "わあっ、", "fearful": "うわっ、"},
    "en": {"excited": "Wow!! ", "surprised": "What?! ", "angry": "Seriously?! ",
           "sad": "Oh no... ", "happy": "Wow, ", "fearful": "Oh no! "},
}
# 译文已自带感叹词开头(LLM 口语化翻译常见)时不再叠加,防"えっ、えっ"复读腔
_EMO_ALREADY = ("えっ", "ええっ", "わあ", "わぁ", "うわ", "やった", "まじ", "ほんと", "もう", "はぁ",
                "ちょっと", "なんて", "何て", "そっか", "すご", "きゃ", "ああ", "あら",
                "wow", "oh", "hey", "wait", "come on", "well", "what", "man", "seriously",
                "哇", "诶", "唉", "哼", "嘿", "呀", "哦", "啊")


def _emo_flavor(text: str, emotion: str, dst: str, strong: bool = False) -> str:
    """情感句 → 句首注入感叹词(已有感叹词开头则原样)。strong=强档换加强版感叹词。
    返回实际要念的文本(字幕仍用原译文)。"""
    if not (EMO_WORDS_ON and emotion and text):
        return text
    low = text.strip().lower()
    if any(low.startswith(w) for w in _EMO_ALREADY):
        return text
    lang = (dst or "").split("-")[0].lower()
    pre = ""
    if strong:
        pre = _EMO_PREFIX_STRONG.get(lang, {}).get(emotion, "")
    if not pre:
        pre = _EMO_PREFIX.get(lang, {}).get(emotion, "")
    return (pre + text) if pre else text
_emo_stats = {"routed": 0, "fallback": 0, "skipped_busy": 0, "ms_last": 0, "last": "",
              "llm_calls": 0, "llm_ms": 0, "mood": "", "mood_src": ""}   # 观测:改道/回退/基调
_emo_ctx = {"mood": "", "mood_ts": 0.0, "mood_src": "", "llm_ts": 0.0,
            "llm_busy": False, "loud_dbfs": None}
_emo_lock = threading.Lock()
try:
    from emotion_detector import detect_emotion as _emo_detect_text   # 复用主控台同款规则器
except Exception:
    _emo_detect_text = None
    logging.getLogger("interp").warning("emotion_detector 不可用，情感配音路由停用")


def _emo_note_audio(audio16k):
    """登记本句原声响度(dBFS)，供佐证层参考。失败静默。"""
    try:
        if audio16k is None or not getattr(audio16k, "size", 0):
            return
        x = np.asarray(audio16k, np.float32).reshape(-1)
        rms = float(np.sqrt(np.mean(x * x)) + 1e-12)
        with _emo_lock:
            _emo_ctx["loud_dbfs"] = 20.0 * float(np.log10(rms))
    except Exception:
        pass


def _emo_mood() -> str:
    """当前有效的情绪基调(过期返 "")。"""
    with _emo_lock:
        if _emo_ctx["mood"] and time.time() - _emo_ctx["mood_ts"] <= EMO_MOOD_TTL:
            return _emo_ctx["mood"]
        return ""


def _emo_set_mood(label: str, src: str):
    with _emo_lock:
        _emo_ctx["mood"] = label if label != "neutral" else ""
        _emo_ctx["mood_ts"] = time.time()
        _emo_ctx["mood_src"] = src
    _emo_stats["mood"] = _emo_ctx["mood"]
    _emo_stats["mood_src"] = src


def _emo_llm_kick():
    """异步：把最近 6 条双方对话喂本地 LLM(常驻,实测 315ms)判「我」的情绪基调 → 更新 mood。
    节流 EMO_LLM_GAP 秒、单飞(inflight 不重入)；失败静默(规则层继续兜底)。"""
    if not (EMO_LLM_ON and EMO_TTS_ON):
        return
    now = time.time()
    with _emo_lock:
        if _emo_ctx["llm_busy"] or now - _emo_ctx["llm_ts"] < EMO_LLM_GAP:
            return
        _emo_ctx["llm_busy"] = True
        _emo_ctx["llm_ts"] = now

    def _do():
        try:
            with ST.lock:
                items = list(ST.transcript)[-6:]
            if not items:
                return
            lines = []
            for e in items:
                who = "我" if e.get("who") == "me" else "对方"
                src = (e.get("src") or e.get("trans") or "").strip()
                if src:
                    lines.append(f"{who}: {src[:60]}")
            if not lines:
                return
            prompt = ("以下是一段通话里最近的对话(从旧到新):\n" + "\n".join(lines) +
                      "\n\n判断「我」此刻说话的情绪基调。只输出一个英文单词，从这里选："
                      "excited/angry/sad/surprised/happy/gentle/serious/calm/neutral")
            t0 = time.time()
            r = requests.post(f"{_LLM_URL}/api/generate",
                              json={"model": _LLM_MODEL, "prompt": prompt, "stream": False,
                                    "keep_alive": _LLM_KEEP,
                                    "options": {"num_predict": 6, "temperature": 0.0}},
                              timeout=8)
            r.raise_for_status()
            word = str(r.json().get("response") or "").strip().lower()
            word = _re.sub(r"[^a-z]", " ", word).split()[0] if _re.sub(r"[^a-z]", " ", word).split() else ""
            _emo_stats["llm_calls"] += 1
            _emo_stats["llm_ms"] = int((time.time() - t0) * 1000)
            if word in _EMO_LABELS:
                _emo_set_mood(word, "llm")
                logger.info(f"[情绪基调] LLM({_emo_stats['llm_ms']}ms) → {word or 'neutral'}")
        except Exception:
            pass
        finally:
            with _emo_lock:
                _emo_ctx["llm_busy"] = False
    threading.Thread(target=_do, daemon=True).start()


# ── P9.1 韵律跟随(2026-07-10)：情感一半在"怎么说"里——逐句提取原声的音高摆幅/响度/语速
# 相对说话人自身基线的偏离，映射到 SBV2 合成参数(抑扬/强度/语速)。文字层测不到的
# "用愤怒语气说平常话"由此跟上；平叙句也随你的现场表演起伏,不再查词典式定情绪。
PROSODY_FOLLOW = os.environ.get("INTERP_PROSODY_FOLLOW", "1") == "1"
_PROS = {"f0r_base": None, "rms_base": None, "rate_base": None,   # EWMA 说话人基线
         "cur": None, "lock": threading.Lock()}                    # cur=本句特征快照


def _pros_f0_stats(x: np.ndarray, sr: int):
    """轻量 F0 估计(分帧自相关,仅浊音帧)。返回 (median_hz, iqr_hz)；失败 (0,0)。~5ms/句。"""
    try:
        fl = int(sr * 0.04)
        hop = int(sr * 0.02)
        lo, hi = int(sr / 400.0), int(sr / 70.0)      # 70~400Hz
        f0s = []
        for i in range(0, len(x) - fl, hop):
            fr = x[i:i + fl]
            if float(np.sqrt(np.mean(fr * fr))) < 0.02:
                continue                               # 静音/清音帧跳过
            fr = fr - fr.mean()
            ac = np.correlate(fr, fr, "full")[fl - 1:]
            if ac[0] <= 0:
                continue
            seg = ac[lo:hi]
            if not seg.size:
                continue
            k = int(np.argmax(seg)) + lo
            if ac[k] / ac[0] > 0.45:                   # 周期性足够才算浊音
                f0s.append(sr / k)
        if len(f0s) < 4:
            return 0.0, 0.0
        a = np.asarray(f0s, np.float32)
        return float(np.median(a)), float(np.percentile(a, 80) - np.percentile(a, 20))
    except Exception:
        return 0.0, 0.0


def _pros_note(audio16k, zh: str = ""):
    """登记本句原声韵律特征并更新说话人基线(EWMA)。在 _emo_note_audio 同点调用。"""
    if not PROSODY_FOLLOW:
        return
    try:
        x = np.asarray(audio16k, np.float32).reshape(-1)
        if x.size < 1600:
            return
        dur = x.size / 16000.0
        rms = float(np.sqrt(np.mean(x * x)) + 1e-12)
        _, f0r = _pros_f0_stats(x, 16000)
        rate = (len(zh) / dur) if (zh and dur > 0.3) else 0.0   # 字/秒(zh 文本可得时)
        with _PROS["lock"]:
            for key, v in (("f0r_base", f0r), ("rms_base", rms), ("rate_base", rate)):
                if v and v > 0:
                    b = _PROS[key]
                    _PROS[key] = v if b is None else (0.9 * b + 0.1 * v)
            _PROS["cur"] = {"f0r": f0r, "rms": rms, "rate": rate, "ts": time.time()}
    except Exception:
        pass


def _pros_params() -> dict:
    """本句韵律偏离 → SBV2 参数增量 {intonation, speed_mult, w_add}。无数据/过期 → {}。"""
    if not PROSODY_FOLLOW:
        return {}
    with _PROS["lock"]:
        cur = _PROS["cur"]
        f0b, rb, rtb = _PROS["f0r_base"], _PROS["rms_base"], _PROS["rate_base"]
    if not cur or time.time() - cur["ts"] > 8.0:
        return {}
    out = {}
    if cur["f0r"] and f0b:
        ratio = cur["f0r"] / max(1e-6, f0b)            # 音高摆幅比基线宽 → 抑扬放大
        if ratio > 1.15:
            out["intonation"] = min(1.5, 1.0 + 0.35 * (ratio - 1.0))
    if cur["rate"] and rtb:
        out["speed_mult"] = max(0.88, min(1.18, (cur["rate"] / rtb) ** 0.6))  # 压幂防过冲
    if cur["rms"] and rb:
        db = 20.0 * np.log10(cur["rms"] / max(1e-9, rb))
        if db > 3.0:
            out["w_add"] = min(0.5, 0.1 * db)          # 明显比平时响 → 情绪强度加成
    return out


# SBV2 路径可用的全部表现性标签：SBV2 情感=同一次流式合成换 style 向量,零额外延迟——
# 不必像 CosyVoice 改道那样只放行高唤醒度；happy/gentle 这类中唤醒情绪也值得上色。
# (2026-07-10 实测空档：用户最常测的"开心"句在旧门禁下全走 Neutral,被听成"没感情"。)
_EMO_SBV2_OK = {"excited", "angry", "sad", "surprised", "happy", "fearful", "gentle"}


def _emo_for_sentence(zh: str, en: str) -> str:
    """三层融合返回该句改道标签；平叙/轻度情感/超长句/功能关闭 → ""(走原流式链路)。
    ① 单句规则命中强情感 → 立即用(顺带刷新基调)；② 否则若聊天基调仍有效且属强情感集 →
    继承基调(响度佐证：本句明显偏轻声(<阈值-10dB)则不染)；③ 都没有 → ""。
    超长句(>24词)不改道：情感在长句中被稀释,不值得付整句非流式的延迟。
    SBV2(日语)例外：情感零成本 → 任何表现性标签都上色,长句也不设限。"""
    if not (EMO_TTS_ON and _emo_detect_text):
        return ""
    _emo_llm_kick()                       # 每次定稿都尝试刷新基调(带节流,异步不阻塞)
    sbv2 = _ja_use_sbv2()
    if not sbv2 and len((en or "").split()) > 24:
        _llm_emo_take(_DST_LANG)          # 超长句不改道:清掉本句标签,防串染下一句
        return ""
    # P0-R2b：翻译 LLM 同调产出的语义级情绪标签优先(比关键词规则准,且零额外调用)。
    llm_emo = _llm_emo_take(_DST_LANG)
    if llm_emo and (llm_emo in _EMO_STRONG or (sbv2 and llm_emo in _EMO_SBV2_OK)):
        _emo_set_mood(llm_emo, "llm-tag")
        return llm_emo
    rule = ""
    try:
        rule = _emo_detect_text((zh or "").strip() or (en or "").strip())
    except Exception:
        rule = ""
    if rule in _EMO_STRONG:
        _emo_set_mood(rule, "rule")       # 显式强情感也刷新基调(后续平叙句可继承)
        return rule
    if sbv2 and rule in _EMO_SBV2_OK:
        return rule                       # SBV2 零成本:中唤醒情绪(happy/gentle…)也上 style
    mood = _emo_mood()
    if mood in _EMO_STRONG:
        loud = _emo_ctx.get("loud_dbfs")
        if loud is not None and loud < EMO_LOUD_DBFS - 10:
            return ""                     # 本句明显轻声 → 别把基调硬染上去
        return mood
    return ""


# P6.2 情感强度三档 → SBV2 style_weight(style 向量相对均值的外推倍率,1.0=训练分布原样)。
# 证据分级：强=原声很响 或 感叹/问号≥2 或 规则层直接命中(显式情感词)；
#           弱=仅从聊天基调继承(本句文本无显式信号)；其余=中。
# 2026-07-10 用户反馈"情感还不够" → 三档整体上调(SBV2 style 外推更狠,服务端 3.0 封顶)。
EMO_W_SUBTLE = float(os.environ.get("INTERP_EMO_W_SUBTLE", "1.2"))
EMO_W_NORMAL = float(os.environ.get("INTERP_EMO_W_NORMAL", "1.8"))
EMO_W_STRONG = float(os.environ.get("INTERP_EMO_W_STRONG", "2.4"))


def _emo_intensity(zh: str, en: str, label: str) -> float:
    """返回该情感句的 SBV2 style_weight。label 为空返回 0(不传,服务端用默认)。"""
    if not label:
        return 0.0
    txt = f"{zh or ''}{en or ''}"
    bangs = sum(txt.count(c) for c in "！!？?")
    loud = _emo_ctx.get("loud_dbfs")
    if (loud is not None and loud >= EMO_LOUD_DBFS) or bangs >= 2:
        return EMO_W_STRONG
    rule = ""
    try:
        rule = _emo_detect_text((zh or "").strip() or (en or "").strip()) if _emo_detect_text else ""
    except Exception:
        rule = ""
    if rule != label and bangs == 0:
        return EMO_W_SUBTLE               # 仅基调继承且本句无显式信号 → 轻染
    return EMO_W_NORMAL


def _emo_prewarm_kick():
    """会话启动时后台踢一脚 CosyVoice3 预加载：该服务空闲会自动卸载模型，
    冷态下首个情感句会付模型加载(可 >10s)→超时回退,用户以为功能没生效。best-effort。"""
    if not (EMO_TTS_ON and _emo_detect_text):
        return
    def _do():
        try:
            requests.post(f"{_tts_url_for('cosyvoice')}/v1/tts/preload", timeout=6)
        except Exception:
            pass
    threading.Thread(target=_do, daemon=True).start()


def _synth_emotional_stream(en: str, emotion: str, q, dev_sr: int, dev_ch: int,
                            laugh: bool = False) -> bool:
    """P2a：情感句流式合成(CosyVoice3 /v1/tts/clone/stream,协议同 fish/qwen3)。
    边到边入队,首包即出声；服务端按(参考音+指令)缓存 spk → 复调免参考特征抽取。
    首块前失败返回 False(调用方回退非流式/普通引擎)；已出声后中途断流则吞掉(防整句重播)。
    laugh=True 给正文尾加 [laughter] 细粒度标记(仅 cosyvoice 认识,兜底引擎拿纯文本)。"""
    if not ST.voice_b64:
        return False
    payload = {"text": (en + " [laughter]") if laugh else en,
               "instruct": _emo_instruct_for(emotion, _DST_LANG),
               "reference_audio_b64": ST.voice_b64,
               "speed": max(0.6, min(1.6, EMO_SPEED))}
    t0 = time.time()
    try:
        r = _HTTP_POOL.post(f"{_tts_url_for('cosyvoice')}/v1/tts/clone/stream",
                          json=payload, stream=True, timeout=(3, EMO_TTS_TIMEOUT))
        r.raise_for_status()
    except Exception as e:
        logger.warning(f"[情感配音] 流式请求失败({str(e)[:60]})，回退非流式")
        return False
    sr = int(r.headers.get("X-Sample-Rate", "24000"))
    buf = b""; idx = 0; prev = None
    try:
        for raw in r.iter_content(chunk_size=4096):
            if ST.play_q is None:
                break
            buf += raw
            while len(buf) >= 4:
                ln = struct.unpack("<I", buf[:4])[0]
                if ln == 0:
                    buf = b""; break
                if len(buf) < 4 + ln:
                    break
                pcm = buf[4:4 + ln]; buf = buf[4 + ln:]
                if len(pcm) < sr // 20:            # 跳过碎帧(<0.05s)
                    continue
                d = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
                if sr != dev_sr:
                    d = _resample(d, sr, dev_sr)
                if idx == 0:
                    _emo_stats["routed"] += 1
                    _emo_stats["ms_last"] = int((time.time() - t0) * 1000)
                    _emo_stats["last"] = emotion
                    logger.info(f"[情感配音·流式] {emotion} 首包 {_emo_stats['ms_last']}ms: {en[:50]!r}")
                    _put_block(q, d, dev_ch, fin_ms=12, fout_ms=0, dev_sr=dev_sr)
                else:
                    if prev is not None:
                        _put_block(q, prev, dev_ch, fin_ms=0, fout_ms=0, dev_sr=dev_sr)
                    prev = d
                idx += 1
        if prev is not None:
            _put_block(q, prev, dev_ch, fin_ms=0, fout_ms=10, dev_sr=dev_sr)
    except Exception:
        if idx == 0:
            logger.warning("[情感配音] 流式中途失败且未出声，回退非流式")
            return False
        logger.warning("[情感配音] 流式合成中途断开，已部分出声，不再重播")
    finally:
        r.close()
    return idx > 0


def _synth_emotional(en: str, emotion: str, laugh: bool = False) -> bytes:
    """CosyVoice3 克隆+情感整句合成 → WAV bytes；任何异常/超时返回 b""(调用方回退普通链路)。"""
    if not ST.voice_b64:
        return b""
    try:
        url = _tts_url_for("cosyvoice")
        t0 = time.time()
        r = requests.post(f"{url}/v1/tts/instruct",
                          json={"text": (en + " [laughter]") if laugh else en,
                                "instruct": _emo_instruct_for(emotion, _DST_LANG),
                                "reference_audio_b64": ST.voice_b64,
                                "speed": max(0.6, min(1.6, EMO_SPEED)),
                                "return_base64": True},
                          timeout=(3, EMO_TTS_TIMEOUT))
        r.raise_for_status()
        b64 = r.json().get("audio_base64") or ""
        if not b64:
            return b""
        _emo_stats["routed"] += 1
        _emo_stats["ms_last"] = int((time.time() - t0) * 1000)
        _emo_stats["last"] = emotion
        logger.info(f"[情感配音] {emotion} ({_emo_stats['ms_last']}ms): {en[:60]!r}")
        return base64.b64decode(b64)
    except Exception as e:
        _emo_stats["fallback"] += 1
        logger.warning(f"[情感配音] cosyvoice 失败({str(e)[:80]})，本句回退普通引擎")
        return b""
SUB_DEBOUNCE_SEC = float(os.environ.get("INTERP_SUB_DEBOUNCE", "0.45"))  # 对方字幕防抖(秒)
FIRST_SEG_FRAMES = int(os.environ.get("INTERP_FIRST_SEG", "8"))          # lipsync 首段帧数(压测后定 8)
# 长句分句流水线:句子超过该词数→切 2 块,先合成首块即开播,次块在首块播放期间并行合成,
#   隐藏整句 TTS 尾延迟(首帧从"整句synth+TTFV"降到"首块synth+TTFV")。0=关闭。
PIPELINE_MIN_WORDS = int(os.environ.get("INTERP_PIPELINE_MIN_WORDS", "14"))

# 逐句 VAD 参数(能量法，免 webrtcvad 依赖)
VAD_SILENCE_SEC = float(os.environ.get("INTERP_VAD_SILENCE", "0.5"))   # 静音多久判定一句结束
VAD_MIN_SEC     = float(os.environ.get("INTERP_VAD_MIN", "0.4"))       # 一句最短(过滤咳嗽/杂音)
VAD_MAX_SEC     = float(os.environ.get("INTERP_VAD_MAX", "10.0"))      # 一句最长(强制断句)
VAD_PREROLL_SEC = 0.25                                                 # 句首回看(防吞字)
# 子句级提交(增量同传)：连续长句里，一旦出现"子句停顿"且已说够时长，就把这段子句
# 当作独立单元先提交(各段独立、永不回译→无重复风险)，对方不必等整段说完。
VAD_CLAUSE_ENABLE = os.environ.get("INTERP_CLAUSE", "1") == "1"
VAD_CLAUSE_SIL  = float(os.environ.get("INTERP_CLAUSE_SIL", "0.35"))   # 子句停顿阈值(<整句阈值)
VAD_CLAUSE_MIN  = float(os.environ.get("INTERP_CLAUSE_MIN", "1.4"))    # 子句最短有效语音(防切碎)
# 轮次合并 + 软终判：同说话人 TURN_GAP 内的子句归为一轮；一轮静默 FINALIZE_GAP 后整轮重译润色替换字幕
TURN_GAP        = float(os.environ.get("INTERP_TURN_GAP", "2.0"))
FINALIZE_GAP    = float(os.environ.get("INTERP_FINALIZE_GAP", "1.2"))
FINALIZE_ENABLE = os.environ.get("INTERP_FINALIZE", "1") == "1"
# 转写留存/导出：每轮定稿把「原文+译文+相对时间戳」记入会话转写，支持导出 TXT/SRT(配录像)/JSON，
# 会话结束落 logs/interp_transcript_*.json 供事后复盘/导出。关闭=不记录(零开销、导出为空)。
TRANSCRIPT_ON   = os.environ.get("INTERP_TRANSCRIPT", "1") == "1"
TRANSCRIPT_MAX  = max(50, int(os.environ.get("INTERP_TRANSCRIPT_MAX", "5000")))   # 内存留存上限(超出丢最旧)

# ── 流式 STT(Nemotron)逐词字幕 ───────────────────────────────────────────
# 通话/直播均可启用：连续推 PCM 到 nemotron /ws/transcribe(auto_eou)，partial 逐词刷字幕、
# final(经门控+幻听过滤)触发 NMT。通话→克隆配音；直播→数字人口型(口型/配音仍按整句一次，最敏感链路不变)。
# 灰度开关：默认开(P0 优化：逐词流式边说边出字幕更跟手；仅影响字幕/文本呈现，口型/配音仍按整句不变)。
# 可 env INTERP_STREAM=0 关闭；/start 传 stream 显式覆盖；Nemotron 不可达时自动回退整句分段(安全网)→默认开为低风险。
STREAM_STT_DEFAULT = os.environ.get("INTERP_STREAM", "1") == "1"
# P5-2 双 ASR 评审实测：同一段韩语克隆音,Whisper 相似度 0.89 而 Nemotron 仅 0.34——Nemotron 弱语种
# 走流式会把对方的话转错。弱语种语向默认回退分段模式(Whisper 转写,仍可 /start 显式 stream=true 强制流式)。
# P6-1 清单数据化：策略文件 data/weak_langs.json 由 tools/lang_qa.py 双 ASR 实测生成(24 语全测),
# 升级流式引擎后重跑工具即自动摘除达标语种;env 为手工附加(并集),平时留空。


def _load_lang_policy():
    weak = {c.strip().lower() for c in
            os.environ.get("INTERP_STREAM_WEAK_LANGS", "").split(",") if c.strip()}
    tts_low = set()
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "weak_langs.json")
        d = json.load(open(p, encoding="utf-8"))
        weak |= {str(c).strip().lower() for c in (d.get("weak") or []) if str(c).strip()}
        tts_low = {str(c).strip().lower() for c in (d.get("tts_low") or []) if str(c).strip()}
    except Exception:
        weak |= {"ko"}      # 无实测文件时保底沿用 P5-2 的人工实测结论
    return weak, tts_low


STREAM_WEAK_LANGS, TTS_LOW_LANGS = _load_lang_policy()
NEMO_WS_URL    = os.environ.get("INTERP_NEMO_WS", "ws://127.0.0.1:7857")
STREAM_SIL_MS  = int(os.environ.get("INTERP_STREAM_SIL_MS", "500"))   # 服务端 auto_eou 静音定稿阈值(ms);越小越快出译/配音,过小会切碎句子
# 一句最短有效语音(ms)：低于此的极短"幻听blip"不定稿→减少空/噪声定稿造成的字幕行闪烁。
STREAM_MINVOICE_MS = int(os.environ.get("INTERP_STREAM_MINVOICE_MS", "300"))
# 最长句强制定稿(秒)：连续说话不停顿时,流式 auto_eou 永不定稿→缓冲膨胀、对方迟迟听不到克隆配音。
# 补回分段模式的 VAD_MAX_SEC 保护：连续语音达此时长即客户端主动发 eou 强制断句。
STREAM_MAX_SEC = float(os.environ.get("INTERP_STREAM_MAX_SEC", "12"))
# wait-k 字幕稳定：partial 的末 k 个 token(最易被后续改写的"正在识别"尾巴)先不显示，
# 只刷已稳定的前缀→显著降低逐词字幕"回跳/闪烁"；末尾 k 个 token 在 final 一次性补齐(整句永远完整)。
# CJK 按“字”、含空格语言按“词”计。默认 1(仅压最不稳的末 1 token，肉眼几乎无延迟)；0=关闭(旧逐词行为)。
STREAM_WAITK = int(os.environ.get("INTERP_WAITK", "1"))

# ══════════ P0-R 实时性/真人感四件套(2026-07-16) ══════════
# R1 语义块流式配音：partial 的稳定前缀到达子句边界(标点/超长)即提前翻译+配音，
#   不再等整句 final——长句首音可提前 1.5s+。仅通话/配音模式(play_q)生效：
#   直播口型模式有"口型生成期暂停 ASR 上行"的 GPU 让位机制，分块会与其互锁,保持整句。
#   安全闸与 final 同源：文本回声闸 + 缓冲音频门控 + 声纹锁,全过才提交。0=关(旧行为)。
CHUNK_DUB_ON      = os.environ.get("INTERP_CHUNK_DUB", "1") == "1"
CHUNK_MIN_LEN     = int(os.environ.get("INTERP_CHUNK_MIN_LEN", "10"))    # 块最短(CJK字/西文词)——太短不值一次 MT+TTS
CHUNK_FORCE_LEN   = int(os.environ.get("INTERP_CHUNK_FORCE_LEN", "24"))  # 无标点连续说话时,稳定前缀达此长度强制提交
# R2 翻译滚动上下文：最近 N 句源文注入 LLM 翻译 prompt——跨句指代(它=刚才说的商品)、
#   术语一致、以及 R1 分块翻译的语境连贯都靠它。仅 LLM 译路消费(NMT/Google 兜底不变)。
#   注意:开启后翻译缓存键含上下文哈希(同句不同语境不共享缓存,正确性优先)。0=关(旧缓存行为)。
MT_CTX_ON         = os.environ.get("INTERP_MT_CONTEXT", "1") == "1"
MT_CTX_N          = max(1, int(os.environ.get("INTERP_MT_CONTEXT_N", "2")))
MT_CTX_WINDOW_S   = float(os.environ.get("INTERP_MT_CONTEXT_WINDOW_S", "90"))
# R2b 单次调用附带情绪：翻译 LLM 在译文尾追加 [emo:xxx] 轻量标签(强情绪句才加)，
#   解析成功=情感配音路由零额外 LLM 调用；解析失败/未加=自动回退现有关键词规则,零风险。
LLM_EMO_TAG       = os.environ.get("INTERP_LLM_EMO_TAG", "1") == "1"
# R4 音画字一致(方向A=有配音的方向)：软终判/GER 的"润色译文"不再替换屏幕与 OBS 字幕
#   (观众耳听的是已播配音版本,字幕跟着改=可感知穿帮)；润色稿仍进转写留存(导出质量不降)。
#   源文(中文)字幕纠错不受限——克隆音念的是译文,改源文不产生音画不一致。0=旧行为(润色替换字幕)。
SUBS_MATCH_AUDIO  = os.environ.get("INTERP_SUBS_MATCH_AUDIO", "1") == "1"

# ══════════ P1-R 实时性/真人感第二批(2026-07-16) ══════════
# S 流式 LLM 翻译(边译边配)：ollama stream=True,译文 token 到达子句边界即送 TTS——
#   非分块句(短中句/分块被预检退回的句)的"整段译文生成等待"从关键路径消失。
#   适用面收窄换安全：仅通话配音链路 + LLM 可用 + 翻译缓存未命中 + 无术语命中 +
#   非强情绪句(保留情感引擎改道)。首段过目标语健全闸后才出声,闸拒=整句退回旧非流式路径(零重播)。
LLM_STREAM_ON  = os.environ.get("INTERP_LLM_STREAM", "1") == "1"
LLM_STREAM_MIN = max(2, int(os.environ.get("INTERP_LLM_STREAM_MIN", "6")))   # 首段最短(CJK字/西文词)
# F 填充音垫场：定稿后超过 FILLER_AFTER_MS 仍无音频可播(翻译/TTS 慢)→ 用克隆音色的
#   气口("嗯…"/"Okay, so...")垫住空白,对方不觉得"断线了"。素材开播时后台自动用当前
#   音色生成一次(data/fillers/<角色>/<语>/),之后离线复用。仅通话模式(直播口型垫场会音画错位)。
FILLER_ON        = os.environ.get("INTERP_FILLER", "1") == "1"
FILLER_AFTER_MS  = max(300, int(os.environ.get("INTERP_FILLER_AFTER_MS", "900")))
FILLER_MIN_GAP_S = float(os.environ.get("INTERP_FILLER_MIN_GAP_S", "6"))     # 两次垫场最小间隔(防口头禅刷屏感)
# L Language Pack：data/langpacks/<语>.json 热加载(mtime 变更即生效),把「地域口语风格/
#   常用语气词/禁忌词」注入 LLM 翻译 system prompt——文化适配不训模型、按国家热插拔。
#   无文件=零行为变化；日语关东口语化保持内置(langpack 是其泛化,可覆盖追加)。
LANGPACK_ON = os.environ.get("INTERP_LANGPACK", "1") == "1"

# ══════════ P2-R 韵律规划 v0(Dialogue Planner 雏形, 2026-07-16) ══════════
# D1 句间呼吸间隙：播放积压时(上句音频还没播完就来了下句)在新句音频前垫一小段静音——
#   句句无缝相撞的"机关枪感"变成真人换气节奏。队列为空=对方正在等声音,不加任何延迟。
SENT_GAP_MS = max(0, int(os.environ.get("INTERP_SENT_GAP_MS", "160")))
# D2 口语韵律标点：LLM 翻译按"朗读节奏"打标点(换气逗号/犹豫省略号/长句拆两短句)。
#   TTS 韵律天然跟随标点 → 零解析零延迟的停顿规划;不靠中途插标记(7b 解析不可靠)。
PROSODY_PUNCT = os.environ.get("INTERP_PROSODY_PUNCT", "1") == "1"

# ══════════ P3-R 直播翻译预取 + 接话规划(2026-07-16) ══════════
# L 直播翻译预取：直播口型链路复用 P0 语义块机制但"只译不配"——说话期间子句就位即翻译
#   (走同一 pool_a 串行,不与口型生成抢档期),定稿时译文已基本就绪(NMT 等待≈0)。
#   口型仍整句一次触发(最敏感链路不动);无提前音频=无音画风险,预取错了只是白付几次块翻译。
LIVE_PREFETCH_ON = os.environ.get("INTERP_LIVE_PREFETCH", "1") == "1"
# T 接话礼让(Dialogue Planner v1a)：对方"刚开口"(<1.5s)时我方新句正要出声 → 句首礼让
#   最多 TURN_HOLD_MS,对方一停顿立即开播——消掉"双方同时开口"的抢话感。只挡句首不断句中;
#   对方长篇讲话中不礼让(同传本来就该压话说)。0=关。仅通话模式。
TURN_HOLD_MS = max(0, int(os.environ.get("INTERP_TURN_HOLD_MS", "600")))
# B 倾听附和(Dialogue Planner v1b)：对方说了长句、之后 3s 我方没接话 → 克隆音轻附和一声
#   ("Mm-hm."/"はい。")——对方知道你在听没掉线。素材与垫场气口同库(bc 前缀),开播自动生成;
#   30s 防抖防"应声虫"。仅通话模式。
BACKCHANNEL_ON   = os.environ.get("INTERP_BACKCHANNEL", "1") == "1"
BC_MIN_GAP_S     = float(os.environ.get("INTERP_BC_MIN_GAP_S", "30"))
BC_MIN_SRC_CHARS = int(os.environ.get("INTERP_BC_MIN_SRC_CHARS", "12"))   # 对方句短于此不附和(寒暄不用嗯嗯)

# ══════════ P0-S2S 云端语音到语音同传后端(可插拔，默认关闭) ══════════
#   竞争力动机：端到端 S2S(字节 Seed LiveInterpret 2.0 一类)延迟 ~2.2s，对本地级联
#   (STT→NMT→TTS ≈3-5s)是代差。做成【可选后端】：INTERP_S2S_BACKEND=seed 且配好密钥
#   才启用；方向A(我→对方)整链路(识别+翻译+克隆配音)交云端，方向B字幕链路永远本地；
#   云端断线/拒绝 → 自动回退本地级联(环形缓冲回放当前句，不丢话)。默认 none=纯离线,零行为变化。
INTERP_S2S_BACKEND = (os.environ.get("INTERP_S2S_BACKEND", "none").strip().lower() or "none")
_S2S_CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "s2s_config.json")
_s2s_cfg_lock = threading.Lock()


def _s2s_runtime_cfg() -> dict:
    """运行时可改配置(POST /config/s2s 落盘,重启保持)。文件缺失/损坏 → env 默认。"""
    out = {"backend": INTERP_S2S_BACKEND,
           "mode": (os.environ.get("SEED_S2S_MODE", "s2s").strip().lower() or "s2s"),
           "speaker": (os.environ.get("SEED_S2S_SPEAKER", "") or "").strip()}
    try:
        with _s2s_cfg_lock:
            with open(_S2S_CFG_PATH, encoding="utf-8") as f:
                d = json.load(f)
        for k in ("backend", "mode", "speaker"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                out[k] = v.strip().lower() if k != "speaker" else v.strip()
    except Exception:
        pass
    return out


def _s2s_cfg_save(patch: dict):
    cfg = _s2s_runtime_cfg()
    cfg.update({k: v for k, v in patch.items() if v is not None})
    with _s2s_cfg_lock:
        os.makedirs(os.path.dirname(_S2S_CFG_PATH), exist_ok=True)
        with open(_S2S_CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    return cfg


# ── 音频工具 ──────────────────────────────────────────────────────────
def _to_wav_bytes(mono_f32: np.ndarray, sr: int = SR) -> bytes:
    """float32[-1,1] 单声道 → 16-bit PCM WAV 字节(soundfile/whisper 可读)。"""
    pcm = np.clip(mono_f32, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _wav_bytes_to_f32(raw: bytes):
    """WAV 字节 → (float32 单声道, sr)。"""
    with wave.open(io.BytesIO(raw), "rb") as w:
        ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        data = w.readframes(n)
    if sw == 2:
        a = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    elif sw == 4:
        a = np.frombuffer(data, dtype="<i4").astype(np.float32) / 2147483648.0
    elif sw == 1:
        a = (np.frombuffer(data, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        a = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a, sr


def _resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    if sr_in == sr_out or x.size == 0:
        return x.astype(np.float32)
    n = int(round(len(x) * sr_out / sr_in))
    if n <= 0:
        return np.zeros(0, np.float32)
    return np.interp(np.linspace(0, len(x), n, endpoint=False),
                     np.arange(len(x)), x).astype(np.float32)


# ── 逐句切分器(能量 VAD + 句首回看 + 自适应噪声底) ──────────────────────
class Segmenter:
    """喂入定长音频块(任意采样率，单声道 float32)，吐出完整"一句"(已重采样到 16k)。"""
    def __init__(self, sr: int, on_segment, direction: str = None):
        self.sr = sr
        self.on_segment = on_segment
        self.direction = direction     # "a"/"b"：用于把噪声底发布给前置门控做自适应校准
        self.preroll = deque(maxlen=max(1, int(VAD_PREROLL_SEC * sr / 1024) + 2))
        self.seg = []
        self.in_speech = False
        self.silence_run = 0.0
        self.speech_len = 0.0
        self.noise = 1e-3          # 自适应噪声底
        self._lock = threading.Lock()

    def feed(self, block: np.ndarray):
        if block.ndim > 1:
            block = block.mean(axis=1)
        if _capture_should_mute(self.direction):   # 半双工:自家外放期间对采集装聋(防灌麦/环回自激)
            block = np.zeros_like(block)
        dur = len(block) / self.sr
        rms = float(np.sqrt(np.mean(block * block)) + 1e-9)
        thr = max(self.noise * 3.0, 0.012)
        with self._lock:
            if not self.in_speech:
                self.preroll.append(block.copy())
                if rms > thr:                       # 起音
                    if self.direction == "a":
                        _trigger_bargein(rms)
                    self.in_speech = True
                    self.seg = list(self.preroll)
                    self.preroll.clear()
                    self.speech_len = sum(len(b) for b in self.seg) / self.sr
                    self.silence_run = 0.0
                else:                               # 静默期更新噪声底
                    self.noise = 0.97 * self.noise + 0.03 * rms
                    _publish_noise_floor(self.direction, self.noise)
                    if self.direction == "a":       # P7: 非语音块喂环境音垫(门控在前,绝不含人声)
                        _amb_note(block, self.sr)
            else:
                self.seg.append(block.copy())
                self.speech_len += dur
                if rms <= thr:
                    self.silence_run += dur
                else:
                    self.silence_run = 0.0
                voiced = self.speech_len - self.silence_run
                if self.silence_run >= VAD_SILENCE_SEC or self.speech_len >= VAD_MAX_SEC:
                    self._emit()                                  # 整句结束 / 强制断句
                elif (VAD_CLAUSE_ENABLE and self.silence_run >= VAD_CLAUSE_SIL
                      and voiced >= VAD_CLAUSE_MIN):
                    self._emit()                                  # 子句停顿处先提交(增量同传)

    def _emit(self):
        seg = np.concatenate(self.seg) if self.seg else np.zeros(0, np.float32)
        voiced = self.speech_len - self.silence_run
        self.seg = []; self.in_speech = False
        self.silence_run = 0.0; self.speech_len = 0.0
        if voiced >= VAD_MIN_SEC and seg.size:
            mono16k = _resample(seg, self.sr, SR)
            try:
                self.on_segment(mono16k)
            except Exception:
                logger.exception("segment 处理异常")

    def flush(self):
        with self._lock:
            if self.in_speech and self.seg:
                self.silence_run = VAD_SILENCE_SEC
                self._emit()


# ── 采集线程(支持普通输入设备 / WASAPI 环回输出设备) ─────────────────────
# P4-4 根治半：soundcard 默认按设备周期(~10ms)开 WASAPI 采集缓冲——Python 侧被 GIL/合成挤占
# 超过 10ms 就丢数据(discontinuity)。缓冲加大到 500ms 只是抖动吸收池，record() 仍 50ms 一取，
# 不增加链路延迟；实测 warning 从一场十余条归零。
LOOPBACK_BUF_MS = max(50, int(os.environ.get("INTERP_LOOPBACK_BUF_MS", "500")))

# ── Phase B-4 抢话打断(barge-in)：克隆音播放中用户开口 → 硬停 CABLE 输出 ──
BARGEIN_ENABLE   = os.environ.get("INTERP_BARGEIN", "1") == "1"
BARGEIN_RMS_DBFS = float(os.environ.get("INTERP_BARGEIN_RMS", "-38"))   # 低于此视为环境噪声
BARGEIN_DEBOUNCE = float(os.environ.get("INTERP_BARGEIN_DEBOUNCE", "0.8"))
_bargein_last_ts = 0.0


def _rms_dbfs(rms: float) -> float:
    import math
    return 20.0 * math.log10(max(float(rms), 1e-9))


def _playback_active() -> bool:
    """是否有克隆音在播或排队(抢话打断只在此态生效)。"""
    q = ST.play_q
    if q is not None and q.qsize() > 0:
        return True
    return bool(getattr(ST, "_avatar_inflight", 0) > 0 and ST.live_mode)


def _trigger_bargein(rms: float):
    """用户开口抢话：清空待播队列并切断当前子块(playback_worker 0.1s 粒度)。"""
    global _bargein_last_ts
    if not BARGEIN_ENABLE or not ST.running or ST.muted:
        return
    if not _playback_active():
        return
    if _rms_dbfs(rms) < BARGEIN_RMS_DBFS:
        return
    now = time.time()
    if now - _bargein_last_ts < BARGEIN_DEBOUNCE:
        return
    _bargein_last_ts = now
    ST.bargein_count = int(getattr(ST, "bargein_count", 0) or 0) + 1
    n = 0
    q = ST.play_q
    if q is not None:
        try:
            while True:
                q.get_nowait()
                n += 1
        except queue.Empty:
            pass
    ST.muted = True
    time.sleep(0.05)   # 让 playback_worker 丢弃当前子块
    ST.muted = False
    ST.push_event({"who": "sys", "warn": f"🛑 抢话打断：已停止克隆音({n} 块待播)"})
    logger.info(f"barge-in #{ST.bargein_count}: dropped={n} rms={_rms_dbfs(rms):.1f}dBFS")


class Capture(threading.Thread):
    def __init__(self, device_index, loopback: bool, on_segment, tag: str, direction: str = None,
                 stream: bool = False, language: str = "", on_partial=None, on_final=None,
                 net_url: str = "", sink_factory=None):
        super().__init__(daemon=True)
        self.device_index = device_index
        self.loopback = loopback
        self.tag = tag
        self.direction = direction
        self.on_segment = on_segment
        # net_url 非空 → 不读本机声卡，改从手机中继 /mic/pcm 直连拉手机麦(零中转，免 VB-Cable)。
        self.net_url = net_url or ""
        # 流式模式(Nemotron 逐词)：用 StreamSink 取代 Segmenter，接口一致(feed/flush)。
        self.stream = stream
        self.language = language
        self.on_partial = on_partial
        self.on_final = on_final
        # P0-S2S: 汇工厂覆盖(sr→sink)。云 S2S 同传用自定义汇整流上云,接口同 feed/flush。
        self.sink_factory = sink_factory
        self._stop = threading.Event()
        self.error = None

    def _make_sink(self, sr: int):
        """构造采集汇：sink_factory 覆盖(云S2S) > 流式 StreamSink > Segmenter(能量VAD 整句)。"""
        if self.sink_factory is not None:
            return self.sink_factory(sr)
        if self.stream:
            # 方向A(广播者):口型生成期暂停上行/partial,让 lipsync 独占 GPU(音频本地暂存不丢)。
            # 方向B(对方):始终流式,口型生成期对方字幕仍要实时,不暂停。
            pause = (lambda: ST._avatar_inflight > 0) if self.direction == "a" else None
            return StreamSink(sr, self.direction, self.language,
                              self.on_partial, self.on_final, tag=self.tag, pause_pred=pause)
        return Segmenter(sr, self.on_segment, direction=self.direction)

    def _run_soundcard_loopback(self) -> bool:
        """用 soundcard 库做 WASAPI 环回(主线 sounddevice 不支持 loopback)。
        按设备名匹配扬声器，数字抓取其输出(对方声)。成功 True，不可用 False。"""
        try:
            import soundcard as sc
        except Exception:
            return False
        try:
            try:
                dev_name = sd.query_devices(self.device_index).get("name", "")
            except Exception:
                dev_name = ""
            core = dev_name.split(" (")[0].strip()
            spk = None
            for s in sc.all_speakers():
                sc_core = s.name.split(" (")[0].strip()
                if core and (core in s.name or sc_core in dev_name):
                    spk = s; break
            if spk is None:
                spk = sc.default_speaker()
            mic = sc.get_microphone(spk.name, include_loopback=True)
            sr = 48000
            seg = self._make_sink(sr)
            logger.info(f"采集启动[{self.tag}]: soundcard环回 '{spk.name[:34]}' sr={sr} stream={self.stream} "
                        f"buf={LOOPBACK_BUF_MS}ms")
            with mic.recorder(samplerate=sr, channels=2,
                              blocksize=int(sr * LOOPBACK_BUF_MS / 1000)) as rec:
                while not self._stop.is_set():
                    data = rec.record(numframes=2400)   # ~50ms
                    if data is not None and len(data):
                        seg.feed(np.asarray(data, dtype=np.float32))
            seg.flush()
            return True
        except Exception as e:
            logger.warning(f"[{self.tag}] soundcard 环回失败({e})，回退立体声混音")
            return False

    def _run_net_mic(self):
        """从手机中继 /mic/pcm 拉手机麦(int16 单声道 PCM)，喂入与声卡路径一致的采集汇。
        采样率以响应头 X-Sample-Rate 为准；断线自动重连，停止时优雅退出。"""
        bps = 2  # int16 mono
        while not self._stop.is_set():
            try:
                with requests.get(self.net_url, stream=True, timeout=(5, 20)) as r:
                    if r.status_code != 200:
                        self.error = f"手机麦直连失败 HTTP {r.status_code}(确认手机端已打开并在监听/对讲)"
                        logger.warning(f"[{self.tag}] {self.error}")
                        time.sleep(1.0); continue
                    try:
                        sr = int(r.headers.get("X-Sample-Rate") or 48000)
                    except Exception:
                        sr = 48000
                    seg = self._make_sink(sr)
                    self.error = None
                    logger.info(f"采集启动[{self.tag}]: 手机麦直连 {self.net_url} sr={sr} stream={self.stream}")
                    buf = b""
                    try:
                        for chunk in r.iter_content(chunk_size=4096):
                            if self._stop.is_set():
                                break
                            if not chunk:
                                continue
                            buf += chunk
                            n = (len(buf) // bps) * bps          # 对齐 int16 边界
                            if n <= 0:
                                continue
                            frame = np.frombuffer(buf[:n], dtype="<i2").astype(np.float32) / 32768.0
                            buf = buf[n:]
                            seg.feed(frame)
                    finally:
                        seg.flush()
            except Exception as e:
                if self._stop.is_set():
                    break
                self.error = f"手机麦直连中断，重连中: {e}"
                logger.warning(f"[{self.tag}] {self.error}")
                time.sleep(1.0)

    def run(self):
        # 手机麦直连(无线)：不读本机声卡，从中继 /mic/pcm 拉流。仅方向A使用。
        if self.net_url:
            self._run_net_mic()
            return
        # 环回(对方声)：优先 soundcard WASAPI 环回(最可靠)，失败回退立体声混音。
        if self.loopback:
            if self._run_soundcard_loopback():
                return
        try:
            info = sd.query_devices(self.device_index)
            sr = int(info.get("default_samplerate") or 48000)
            ch = 1
            extra = None
            if self.loopback:
                # soundcard 环回不可用 → 旧路径:尝试 sd WASAPI loopback，再回退立体声混音。
                ch = min(2, int(info.get("max_output_channels") or 2)) or 1
                try:
                    extra = sd.WasapiSettings(loopback=True)
                except Exception as e:
                    mix = _find_loopback()
                    if mix is not None:
                        logger.warning(f"[{self.tag}] WASAPI 环回不可用({e})；自动改用立体声混音 dev={mix}")
                        self.device_index = mix
                        self.loopback = False
                        info = sd.query_devices(mix)
                        sr = int(info.get("default_samplerate") or 48000)
                        ch = min(2, int(info.get("max_input_channels") or 1)) or 1
                        extra = None
                    else:
                        self.error = ("WASAPI 环回不可用，且未找到「立体声混音」。请在 Windows"
                                      "「声音→录制设备」里启用「立体声混音/Stereo Mix」，再在「对方声来源」选它。")
                        logger.error(self.error); return
            else:
                ch = min(2, int(info.get("max_input_channels") or 1)) or 1
            seg = self._make_sink(sr)
            block = max(256, int(sr * 0.05))

            def cb(indata, frames, t, status):
                if status:
                    logger.debug(f"[{self.tag}] {status}")
                seg.feed(np.asarray(indata, dtype=np.float32).copy())

            logger.info(f"采集启动[{self.tag}]: dev={self.device_index} '{info.get('name','')[:30]}' "
                        f"sr={sr} ch={ch} loopback={self.loopback}")
            with sd.InputStream(device=self.device_index, channels=ch, samplerate=sr,
                                blocksize=block, dtype="float32", callback=cb,
                                extra_settings=extra):
                while not self._stop.is_set():
                    time.sleep(0.1)
            seg.flush()
        except Exception as e:
            self.error = str(e)
            logger.exception(f"采集线程[{self.tag}]异常")

    def stop(self):
        self._stop.set()


# ── 配音播放(方向A输出 → VB-Cable 虚拟麦) ─────────────────────────────
import re as _re


def _split_en_chunks(text: str, coarse: bool = False):
    """把英文译文切成"子句优先、词数封顶"的小块：首块短(尽快出声)，其后稍长(更自然)。
    coarse=True(积压追平模式)：整句不切，单次合成最省固定开销、最快追平积压。"""
    text = (text or "").strip()
    if not text:
        return []
    words = text.split()
    if coarse or len(words) <= 5:             # 追平模式 / 短句:不切，避免碎读与多次固定开销
        return [text]
    chunks, buf = [], []
    for w in words:
        buf.append(w)
        # 首块尽量小(4-6词)→ 输出短、合成快、最快出声；其后块大(10-16词)→ 少吃固定开销、更自然
        target = 4 if not chunks else 10
        cap = (target + 2) if not chunks else (target + 6)
        ends_clause = w[-1] in ",;:.!?"        # 优先在子句标点处断
        if len(buf) >= target and (ends_clause or len(buf) >= cap):
            chunks.append(" ".join(buf)); buf = []
    if buf:
        if chunks and len(buf) <= 2:           # 碎尾并入上一块
            chunks[-1] += " " + " ".join(buf)
        else:
            chunks.append(" ".join(buf))
    return chunks


def _dev_out_params(device_index: int):
    try:
        info = sd.query_devices(device_index)
        return int(info.get("default_samplerate") or SR), int(info.get("max_output_channels") or 1)
    except Exception:
        return SR, 1


def _trim_silence(data: np.ndarray, sr: int, thr: float = 0.02, pad_ms: int = 20):
    """裁掉首尾静音(Fish 每块的填充)，减小块间拼接的"双倍静音"卡顿。保留少量边界 pad 防削音头。"""
    if data is None or data.size == 0:
        return data
    mono = data if data.ndim == 1 else data.mean(axis=1)
    idx = np.where(np.abs(mono) > thr)[0]
    if idx.size == 0:
        return data
    pad = int(sr * pad_ms / 1000)
    a = max(0, idx[0] - pad)
    b = min(len(mono), idx[-1] + pad)
    return data[a:b]


def _edge_fade(data: np.ndarray, sr: int, fin_ms: int = 0, fout_ms: int = 0):
    """对 1D 音频做线性渐入/渐出，消除段边界欠载时的咔哒声。"""
    n = len(data)
    if n < 2:
        return data
    if fin_ms > 0:
        k = min(n, int(sr * fin_ms / 1000))
        if k > 1:
            data[:k] *= np.linspace(0.0, 1.0, k, dtype=np.float32)
    if fout_ms > 0:
        k = min(n, int(sr * fout_ms / 1000))
        if k > 1:
            data[-k:] *= np.linspace(1.0, 0.0, k, dtype=np.float32)
    return data


def _playback_worker(device_index: int, dev_sr: int, ch: int, q: "queue.Queue", stop_evt):
    """常驻播放线程：独占一个 OutputStream，按序消费全局队列里的 PCM 块，连续写入(块间/段间零停顿)。
    与合成解耦 → 合成(GPU)可在播放(声卡)进行时并行跑下一段。
    P1-4 急停：按 ≤0.1s 子块写入并在子块间检查 ST.muted → /panic 后 100ms 内切断正在播的话。
    P7 环境音垫：句间写垫层(房间底噪保活)、配音块下也叠垫层(背景连续,不再"说话时环境音消失")。
    急停只停配音不停垫——真实房间不会突然死寂。无垫层素材时行为与旧版逐字节一致。"""
    stream = None
    slice_n = max(1, int(dev_sr * 0.1))      # 0.1s 子块=急停响应粒度=垫层写入粒度

    def _bed(n: int):
        b = _amb_next(n, dev_sr)
        if b is None:
            return None
        return np.column_stack([b] * ch) if ch >= 2 else b

    try:
        stream = sd.OutputStream(samplerate=dev_sr, channels=ch,
                                 device=device_index, dtype="float32")
        stream.start()
        while not stop_evt.is_set():
            try:
                item = q.get(timeout=0.1)
            except queue.Empty:
                bed = _bed(slice_n)          # 句间/静默期:垫层保活(无素材=旧行为静默)
                if bed is not None:
                    stream.write(bed)
                continue
            if item is None:                 # 停止哨兵
                break
            if ST.muted or not item.size:    # 急停中:配音整块丢弃(垫层由 Empty 分支继续)
                continue
            for off in range(0, len(item), slice_n):
                if ST.muted or stop_evt.is_set():
                    break
                sl = item[off:off + slice_n]
                bed = _bed(len(sl))
                if bed is not None:          # 配音下垫底噪:贴近真麦收音(人声+环境共存)
                    sl = np.clip(sl + bed, -1.0, 1.0)
                stream.write(np.ascontiguousarray(sl, dtype=np.float32))   # 阻塞写;连续 write 无间隙
    except Exception:
        logger.exception("播放线程异常")
    finally:
        if stream is not None:
            try:
                stream.stop(); stream.close()
            except Exception:
                pass


# ── P7 环境音垫(comfort-noise bed)：真实房间底噪垫底,根治"纯净合成音+数字静音"的违和 ──
#   动机(2026-07-10 用户实测反馈)：真人通话背景音连续存在；我们的虚拟麦在句间是绝对零信号、
#   配音又是录音棚级纯净 → 对方听感"背景音突然消失,太假"。
#   做法：采集侧只收「非语音块」(Segmenter 静默分支,天然带门控=绝不漏原声中文)进 6s 滚动环，
#   播放侧把它循环垫进 CABLE 输出——句间垫、配音下也垫(连续推进,无缝)。电平即真实房间电平
#   (采到什么放什么)，上限钳制防吵麦。急停(muted)只停配音不停垫(真房间不会突然死寂)。
AMBIENT_ON       = os.environ.get("INTERP_AMBIENT_BED", "1") == "1"
AMBIENT_GAIN     = float(os.environ.get("INTERP_AMBIENT_GAIN", "1.0"))      # 相对真实底噪的倍率
AMBIENT_MAX_DBFS = float(os.environ.get("INTERP_AMBIENT_MAX_DBFS", "-38"))  # 垫层电平上限(防吵环境灌麦)
_AMB = {"ring": deque(maxlen=256), "sr": 0, "buf": None, "buf_sr": 0, "pos": 0,
        "built": 0.0, "lock": threading.Lock()}


def _amb_note(block: np.ndarray, sr: int):
    """Segmenter(方向A) 静默分支回调：收集非语音块进滚动环。全零块(半双工静音)跳过。"""
    if not AMBIENT_ON or block is None or not block.size:
        return
    rms = float(np.sqrt(np.mean(block * block)) + 1e-12)
    if rms < 1e-6:                        # 半双工/急停期间被上游置零 → 不是真环境音
        return
    with _AMB["lock"]:
        if _AMB["sr"] != sr:              # 设备/采样率变化 → 重新积累
            _AMB["ring"].clear()
            _AMB["sr"] = sr
            _AMB["buf"] = None
        _AMB["ring"].append(block.astype(np.float32, copy=True))


def _amb_dur_s() -> float:
    with _AMB["lock"]:
        if not _AMB["ring"] or not _AMB["sr"]:
            return 0.0
        return sum(len(b) for b in _AMB["ring"]) / float(_AMB["sr"])


def _amb_bed(dev_sr: int):
    """返回 dev_sr 采样率的循环垫层缓冲(≥2s 素材才建)；带节流重建(20s)跟踪环境变化。"""
    now = time.time()
    with _AMB["lock"]:
        buf = _AMB["buf"]
        if (buf is not None and _AMB["buf_sr"] == dev_sr
                and now - _AMB["built"] < 20.0):
            return buf
        if not _AMB["ring"] or not _AMB["sr"]:
            return buf if (buf is not None and _AMB["buf_sr"] == dev_sr) else None
        raw = np.concatenate(list(_AMB["ring"]))
        src_sr = _AMB["sr"]
    if raw.size < int(src_sr * 2.0):      # 素材不足 2s：先不上垫(保持旧行为)
        return None
    d = _resample(raw, src_sr, dev_sr) if src_sr != dev_sr else raw.copy()
    d = (d * AMBIENT_GAIN).astype(np.float32)
    rms = float(np.sqrt(np.mean(d * d)) + 1e-12)
    cap = 10.0 ** (AMBIENT_MAX_DBFS / 20.0)
    if rms > cap:                          # 吵环境:整体压到上限电平
        d *= cap / rms
    k = min(len(d) // 4, int(dev_sr * 0.25))   # 首尾交叉淡化,消循环接缝咔哒
    if k > 8:
        d[:k] = d[:k] * np.linspace(0.0, 1.0, k, dtype=np.float32) \
            + d[-k:] * np.linspace(1.0, 0.0, k, dtype=np.float32)
        d = d[:-k]
    with _AMB["lock"]:
        _AMB["buf"] = d
        _AMB["buf_sr"] = dev_sr
        _AMB["built"] = now
        if _AMB["pos"] >= len(d):
            _AMB["pos"] = 0
    return d


def _amb_next(n: int, dev_sr: int):
    """取垫层接下来 n 个样本(循环推进)。无素材/关闭 → None。"""
    if not AMBIENT_ON or n <= 0:
        return None
    bed = _amb_bed(dev_sr)
    if bed is None or not len(bed):
        return None
    with _AMB["lock"]:
        pos = _AMB["pos"]
        out = np.empty(n, np.float32)
        got = 0
        while got < n:
            take = min(n - got, len(bed) - pos)
            out[got:got + take] = bed[pos:pos + take]
            got += take
            pos = (pos + take) % len(bed)
        _AMB["pos"] = pos
    return out


def _amb_view() -> dict:
    with _AMB["lock"]:
        ready = _AMB["buf"] is not None and len(_AMB["buf"]) > 0
        dbfs = None
        if ready:
            r = float(np.sqrt(np.mean(_AMB["buf"] ** 2)) + 1e-12)
            dbfs = round(20.0 * np.log10(r), 1)
    return {"on": AMBIENT_ON, "ready": ready, "material_s": round(_amb_dur_s(), 1),
            "bed_dbfs": dbfs, "gain": AMBIENT_GAIN, "max_dbfs": AMBIENT_MAX_DBFS}


# ── P1-4 监听耳返：克隆音小音量镜像到"你的耳机"(默认输出)，让你确认对方听到的内容 ──
MONITOR_DEFAULT = os.environ.get("INTERP_MONITOR", "0") == "1"
MONITOR_GAIN    = float(os.environ.get("INTERP_MONITOR_GAIN", "0.25"))

# ── P2-2 对方译文朗读：对方中文译文用「对方自己的音色」克隆朗读到你的耳机(免盯屏幕) ──
# 参考音=对方开口后第一段过全部门控且时长达标的真话(锁定后整场复用→Fish 参考缓存热,
# 每句只多一次合成)。锁定前只出字幕不朗读(避免"先路人音后本人音"的音色跳变)。
READBACK_DEFAULT   = os.environ.get("INTERP_READBACK", "0") == "1"
READBACK_GAIN      = float(os.environ.get("INTERP_READBACK_GAIN", "0.9"))
READBACK_MIN_REF_S = float(os.environ.get("INTERP_READBACK_MIN_REF_S", "2.5"))   # 参考音最短时长(s)
READBACK_MAX_REF_S = 12.0                                                        # 过长截断(编码耗时可控)


def _default_out_params():
    """默认输出设备(你的耳机/音箱)的 (sr, ch)。查询失败给 48k/2。"""
    try:
        info = sd.query_devices(kind="output")
        return int(info.get("default_samplerate") or 48000), min(2, int(info.get("max_output_channels") or 2))
    except Exception:
        return 48000, 2


def _resolve_monitor_out():
    """耳返/朗读输出设备：按「默认输出的设备名」在全部输出设备里挑 hostapi 最稳的实例。
    device=None 会让 PortAudio 自选实例,实测在 Voicemeeter/多驱动并存的机器上可能落到
    WDM-KS 实例 → 开流即 -9999(WdmSyncIoctl)。按名选 MME(最兼容)>DirectSound>WASAPI 根治。
    P8-1: 设备方案指定了监听出口(耳机模式/自定义名)时优先按其解析,失败回退默认输出。
    返回 (index_or_None, sr, ch)；找不到同名实例回退 (None, 默认参数)。"""
    try:
        ov = globals().get("_MONITOR_OUT_OVERRIDE")
        if ov:
            idx = _find_headset_out() if ov == "headset" else _find_device(str(ov), True)
            if idx is not None:
                d = sd.query_devices(idx)
                return idx, int(d.get("default_samplerate") or 48000), \
                    min(2, int(d.get("max_output_channels") or 2))
        base = sd.query_devices(kind="output")
        name = (base.get("name") or "").strip()
        pref = {"MME": 0, "Windows DirectSound": 1, "Windows WASAPI": 2}
        cands = []
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] <= 0:
                continue
            ha = sd.query_hostapis(d["hostapi"])["name"]
            if ha not in pref:
                continue
            # MME 截断名到 31 字符：取短边做前缀比较
            n = (d["name"] or "").strip()
            if n[:28] and (name.startswith(n[:28]) or n.startswith(name[:28])):
                cands.append((pref[ha], i, d))
        if not cands:
            return None, *(_default_out_params())
        cands.sort()
        _, idx, d = cands[0]
        return idx, int(d.get("default_samplerate") or 48000), min(2, int(d.get("max_output_channels") or 2))
    except Exception:
        return None, *(_default_out_params())


def _monitor_worker(q: "queue.Queue", stop_evt):
    """耳返/朗读共用播放线程(独立于主播放)：写默认输出设备。共用一条输出流的动机：
    耳返(我的克隆音)与对方译文朗读同放一个耳机,串行排队天然不重叠(重叠=两路人声打架听不清)。
    队列项 = (kind, ndarray)，kind: 'mon'=耳返(受 monitor_on 门控) / 'rb'=对方译文朗读(受 readback_on)。
    尽力而为：设备异常自动退出不影响主链路。"""
    stream = None
    try:
        dev, sr, ch = _resolve_monitor_out()
        try:
            stream = sd.OutputStream(samplerate=sr, channels=ch, device=dev, dtype="float32")
            stream.start()
        except Exception:
            # 按名选的实例开流失败(设备热插拔/独占)→ 退回 PortAudio 默认实例再试一次
            logger.warning(f"耳返输出按名开流失败(dev={dev})，回退默认实例重试")
            sr, ch = _default_out_params()
            stream = sd.OutputStream(samplerate=sr, channels=ch, device=None, dtype="float32")
            stream.start()
        logger.info(f"耳返/朗读输出就绪: dev={dev if dev is not None else '默认'} sr={sr} ch={ch}")
        _monitor_feed._p = (sr, ch)        # 发布实际流参数,喂入方按此重采样(防默认参数不一致致变调)
        slice_n = max(1, int(sr * 0.1))
        while not stop_evt.is_set():
            try:
                item = q.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            kind, data = item if isinstance(item, tuple) else ("mon", item)
            gate_on = ST.readback_on if kind == "rb" else ST.monitor_on
            if ST.muted or not gate_on or not data.size:
                continue
            for off in range(0, len(data), slice_n):
                if ST.muted or stop_evt.is_set():
                    break
                # 半双工回声闸：标记"此刻外放中"(+0.1s块长)，方向B据此丢弃与外放重叠的段
                ST.aux_out_until = time.time() + 0.35
                stream.write(data[off:off + slice_n])
    except Exception:
        logger.warning("耳返/朗读线程退出(默认输出设备不可用?)，主链路不受影响")
    finally:
        if stream is not None:
            try:
                stream.stop(); stream.close()
            except Exception:
                pass


def _monitor_feed(mono: np.ndarray, src_sr: int):
    """把克隆音块(单声道,cable采样率)缩音量后镜像进耳返队列。满即丢，绝不阻塞主链路。"""
    q = ST.monitor_q
    if q is None or not ST.monitor_on or mono is None or not mono.size:
        return
    try:
        p = getattr(_monitor_feed, "_p", None)
        if not p:
            p = _default_out_params()
            _monitor_feed._p = p
        sr, ch = p
        d = mono if src_sr == sr else _resample(mono, src_sr, sr)
        d = (d * MONITOR_GAIN).astype(np.float32)
        if ch >= 2:
            d = np.column_stack([d] * ch)
        q.put_nowait(("mon", np.ascontiguousarray(d, dtype=np.float32)))
    except queue.Full:
        pass
    except Exception:
        pass


# ── P2-2 对方译文朗读：捕获对方参考音 → 用 TA 的音色克隆读中文译文 → 耳返通道 ──
_RB_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="readback")


ECHO_GUARD_S = float(os.environ.get("INTERP_ECHO_GUARD_S", "1.2"))   # 回声闸富余(见下)
# 音箱半双工：耳返/朗读外放期间采集上游喂静音(1=开)。音箱场景防外放灌麦(乱码+VAD永不定稿)；
# 全程戴耳机可关闭以支持"边播边说"。运行时可经 POST /halfduplex 切换。
HALF_DUPLEX_SPK = os.environ.get("INTERP_HALF_DUPLEX", "1") == "1"


def _aux_room_live(tail_s: float = 0.4) -> bool:
    """耳返/朗读此刻正通过默认输出(音箱/耳机)外放中(含 tail 混响余量)。
    音箱场景下外放会灌进麦克风和环回：识别成乱码 + VAD 永不静音不定稿(实测 2026-07-04 复现)。
    采集上游在此期间直接喂静音(半双工)，从源头隔离，比事后按文本/时间补拦干净得多。"""
    until = ST.aux_out_until
    return bool(until) and time.time() < until + tail_s


def _capture_should_mute(direction: str) -> bool:
    """采集上游半双工判定：
    · 方向B(默认输出环回)：耳返/朗读写的就是默认输出,回录是电气必然 → 外放期间恒静音；
    · 方向A(物理麦)：音箱外放会声学灌麦 → HALF_DUPLEX_SPK(默认开)时静音;全程戴耳机可关。"""
    if not _aux_room_live():
        return False
    return direction == "b" or (direction == "a" and HALF_DUPLEX_SPK)


def _aux_echo_overlap(audio16k) -> bool:
    """方向B半双工回声闸：该段与我们自己的耳返/朗读外放时段重叠→真源是回录的自家音频。
    环回/立体声混音收"默认输出的一切"，不闸会把朗读再翻一遍(朗读→翻→朗读…自激)。
    段起点按 now-时长 估计,再前移 ECHO_GUARD_S 富余：流式定稿到达时刻比音频实际结束晚
    "EOU 静音阈(0.5s)+解码/网络",不加富余则朗读尾段的回录会漏网(实测漏 3/6)。代价是
    朗读刚结束 1.2s 内对方真话也被丢——半双工对讲机语义,可接受。"""
    until = ST.aux_out_until
    if not until:
        return False
    dur = (audio16k.shape[0] / float(SR)) if (audio16k is not None and getattr(audio16k, "size", 0)) else 0.0
    return (time.time() - dur - ECHO_GUARD_S) < until


# ── 自播文本回声闸：手机既当麦又当播放(或 Voicemeeter 把 CABLE 回路到默认输出)时，
# 我们自己播的配音会被麦克风再录回来 → 再翻一遍 → 再播一遍(英文说两次的自激循环)。
# 时间重叠闸(_aux_echo_overlap)只覆盖本机耳返/朗读；无线回路的延迟不可控，改用文本比对：
# 凡识别文本与「最近 25s 内我们自己播出的配音/朗读」高度相似 → 判为回录丢弃。
_SELF_OUT_WINDOW_S = float(os.environ.get("INTERP_SELF_ECHO_WINDOW_S", "25"))
_SELF_OUT_SIM = float(os.environ.get("INTERP_SELF_ECHO_SIM", "0.75"))
_self_out_texts: "deque[tuple[str, float]]" = deque(maxlen=12)


def _norm_echo_text(t: str) -> str:
    return "".join(ch.lower() for ch in (t or "") if ch.isalnum())


def _note_self_output(text: str):
    """凡是要用喇叭/虚拟麦播出去的合成文本(配音译文/对方译文朗读)都登记在案。"""
    n = _norm_echo_text(text)
    if len(n) >= 3:
        _self_out_texts.append((n, time.time()))


def _self_echo_risk_a() -> bool:
    """方向A(我的麦)是否存在「自播回录」的物理路径：手机麦(麦和喇叭同一台手机) 或
    实测扬声器↔麦耦合(半双工)。全双工+耳机的 PC 场景，麦物理上听不到配音——
    文本回声闸在这种场景只会误杀"连说两句相似话"的真人(实测 '对是麦克风。' 被
    上一句配音文本误配拦截)，故无风险时方向A直接跳过该闸。方向B(环回)不受此限：
    环回定义上就录着自家播出的一切+微信远端回声，文本闸永远需要。"""
    cap_a = getattr(ST, "cap_a", None)
    if (cap_a is not None and getattr(cap_a, "net_url", "")) or HALF_DUPLEX_SPK:
        return True
    return bool((getattr(ST, "coupling", None) or {}).get("coupled"))


def _is_self_echo(text: str) -> bool:
    """识别文本与近窗自播文本相似(≥0.75 或互相包含) → 是自家播出的回录。"""
    n = _norm_echo_text(text)
    if len(n) < 3 or not _self_out_texts:
        return False
    now = time.time()
    for ref, ts in _self_out_texts:
        if now - ts > _SELF_OUT_WINDOW_S:
            continue
        if len(n) >= 6 and len(ref) >= 6 and (n in ref or ref in n):
            return True
        # 短句重复形态的回录(实测:播「开始测试」→ 远端绕回「开始测试开始测试」，
        # 相似度只有 0.67 过不了 0.75 门槛)：短边≥4 字、占长边一半以上且被包含 → 判回声。
        lo, hi = (n, ref) if len(n) <= len(ref) else (ref, n)
        if len(lo) >= 4 and 2 * len(lo) >= len(hi) and lo in hi:
            return True
        if difflib.SequenceMatcher(None, n, ref).ratio() >= _SELF_OUT_SIM:
            return True
    return False


# 回环体检提示：文本回声闸拦到"自播回录"说明配音正在通话链路里绕圈(对方端很可能听到
# 同一句英文两遍以上，实测 2026-07-08 12:28/12:33 同句被环回连收 2~4 次)。拦截只保住了
# 字幕/翻译不自激，外部回环(Windows 侦听此设备/Voicemeeter 把麦混进 CABLE/对方外放)只有
# 用户能拆 → 侦测到就推醒目提示，给出排查清单。
_echo_loop_track = {"ts": deque(maxlen=8), "warned": 0.0}


def _note_echo_loop():
    now = time.time()
    _echo_loop_track["ts"].append(now)
    recent = [t for t in _echo_loop_track["ts"] if now - t <= 30]
    if len(recent) >= 2 and now - _echo_loop_track["warned"] > 120:
        _echo_loop_track["warned"] = now
        ST.push_event({"who": "sys", "warn":
            "🔁 检测到你的配音在通话链路中回环(对方可能听到同一句英文两遍)。排查："
            "① 关闭 Windows 对 CABLE Output 的「侦听此设备」；② 别把「我的麦」混进 CABLE"
            "(Voicemeeter 路由)；③ 请对方戴耳机(对方外放会把配音录回去)"})
        logger.warning("[回声闸] 疑似外部回环:近30s拦截自播回录≥2次,已提示用户排查")


def _rb_ref_consider(audio16k: np.ndarray, text: str):
    """把「对方第一段达标真话」锁定为整场克隆参考音(之后 Fish 参考缓存全程命中)。
    只在通过全部门控后调用(幻听/底噪永远进不了参考音)。锁定后不再更换——
    中途换参考=音色跳变+缓存击穿,得不偿失。会话 stop 时清空。"""
    if ST.rb_ref_b64 or audio16k is None or not getattr(audio16k, "size", 0):
        return
    sec = audio16k.shape[0] / float(SR)
    if sec < READBACK_MIN_REF_S or not (text or "").strip():
        return
    if sec > READBACK_MAX_REF_S:
        audio16k = audio16k[: int(SR * READBACK_MAX_REF_S)]
    try:
        ST.rb_ref_b64 = base64.b64encode(_to_wav_bytes(audio16k, SR)).decode()
        ST.rb_ref_text = text.strip()
        ST.rb_ref_sec = round(min(sec, READBACK_MAX_REF_S), 1)
        logger.info(f"[朗读] 已锁定对方参考音 {ST.rb_ref_sec}s: {ST.rb_ref_text[:50]!r}")
        ST.push_event({"who": "sys", "warn": f"🔈 已捕获对方音色({ST.rb_ref_sec}s)，译文朗读就绪"})
        # 后台预热参考编码：首句朗读即走热路径(与我方参考音同一机制,按引擎分流免 404)
        threading.Thread(target=_prewarm_ref, args=(ST.rb_ref_b64, ST.rb_ref_text),
                         daemon=True).start()
    except Exception:
        logger.exception("[朗读] 参考音锁定失败")


def _readback_say(zh_text: str):
    """对方中文译文 → 对方音色克隆 → 耳返通道播放。busy≥2 丢新句(听旧不如听新,但半句丢弃更糟,
    故丢的是最新入队而非在播——实践里 2 句缓冲已够覆盖 TTS 耗时)。全程尽力而为,不影响字幕主链路。"""
    if not (ST.readback_on and ST.rb_ref_b64 and (zh_text or "").strip()):
        return
    if ST.monitor_q is None or ST.rb_busy >= 2:
        return
    _note_self_output(zh_text)   # 朗读文本同样登记(防手机麦把朗读录回再翻)
    ST.rb_busy += 1

    def _work():
        try:
            payload = {"text": zh_text.strip(), "language": _SRC_LANG, "return_base64": True,
                       "temperature": 0.7, "top_p": 0.7, "repetition_penalty": 1.2, "seed": 42,
                       "reference_audio_b64": ST.rb_ref_b64, "reference_text": ST.rb_ref_text}
            r = requests.post(f"{_tts_urls()[0]}/v1/tts/clone", json=payload, timeout=60)
            r.raise_for_status()
            b64 = r.json().get("audio_base64", "")
            if not b64 or ST.monitor_q is None or not ST.readback_on:
                return
            wav, wsr = _wav_bytes_to_f32(base64.b64decode(b64))
            p = getattr(_monitor_feed, "_p", None) or _default_out_params()
            _monitor_feed._p = p
            sr, ch = p
            d = (_resample(wav, wsr, sr) * READBACK_GAIN).astype(np.float32)
            if ch >= 2:
                d = np.column_stack([d] * ch)
            try:
                ST.monitor_q.put_nowait(("rb", np.ascontiguousarray(d, dtype=np.float32)))
            except queue.Full:
                pass
        except Exception as e:
            logger.warning(f"[朗读] 合成失败(跳过本句): {e}")
        finally:
            ST.rb_busy = max(0, ST.rb_busy - 1)

    try:
        _RB_POOL.submit(_work)
    except Exception:
        ST.rb_busy = max(0, ST.rb_busy - 1)


def _put_block(q, d: np.ndarray, dev_ch: int, fin_ms: int, fout_ms: int, dev_sr: int) -> bool:
    """单块 PCM(float32 单声道, dev_sr) → 边界淡化 + 声道适配 → 入队(满则计丢弃)。返回是否成功。"""
    if d is None or d.size == 0:
        return False
    if ST.muted:                           # 急停中:新块直接丢弃(含正在合成的后续块)
        return False
    # P0-R3 首音埋点：本句第一块进主播放队列的时刻(≈对方开始听到的时刻,误差=队列前积压)。
    # 句首由 _stream_final_a/_process_a/块配音任务清零；pool_a 串行 → 无竞态。
    if q is ST.play_q and getattr(ST, "_tts_first_ts", 0.0) == 0.0:
        ST._tts_first_ts = time.time()
    d = _edge_fade(d.copy(), dev_sr, fin_ms=fin_ms, fout_ms=fout_ms)
    if TTS_OUT_GAIN != 1.0:
        d = (d * TTS_OUT_GAIN).astype(np.float32)
    _monitor_feed(d, dev_sr)               # P1-4 耳返镜像(尽力而为,不阻塞)
    if dev_ch >= 2:
        d = np.column_stack([d] * dev_ch)
    if ST.play_q is None:                  # 已停止则不再入队
        return False
    try:
        q.put(np.ascontiguousarray(d, dtype=np.float32), timeout=5)
        return True
    except queue.Full:
        ST.dropped += 1
        logger.warning("播放队列拥塞,丢弃一块配音")
        return False


def _enqueue_synth_stream(en: str, q, dev_sr: int, dev_ch: int, base_url: str = None,
                          emotion: str = "", style_weight: float = 0.0, laugh: bool = False):
    """整句一次调用首选引擎流式端点，边到边入队：首段~880ms 即出声，总延迟 -40%、块间无缝。
    协议:4字节小端长度前缀 + PCM16 mono；0 长度帧结束；采样率在 X-Sample-Rate。
    base_url 缺省用首选引擎(_tts_urls()[0])；Fish/Qwen3/CosyVoice/SBV2 同协议，本函数通用。
    参考音按「本次实际调用的 URL」决定：SBV2 用训练好的林小玲模型(带 emotion/style_weight，
    无需参考音)；其它引擎(含 SBV2 失败后的 Fish 兜底)必须带 voice_b64，否则音色错人。"""
    bu = base_url or _tts_urls(emotion, laugh, style_weight)[0]
    is_sbv2 = (bu == _tts_url_for("sbv2"))
    is_s1 = (bu == _tts_url_for("s1"))
    txt = en
    if is_s1 and laugh and "(laughing)" not in txt[:16]:
        txt = f"(laughing){txt}"
    payload = {"text": txt, "language": _DST_LANG, "temperature": 0.7, "top_p": 0.7,
               "repetition_penalty": 1.2, "seed": 42,
               "speed": max(0.6, min(1.6, INTERP_TTS_SPEED))}   # cosyvoice/sbv2 消费;fish/qwen3 忽略
    if is_sbv2:
        if emotion:                        # SBV2 服务端 emotion→六情绪 style 映射
            payload["emotion"] = emotion
        if style_weight:                   # P6.2 强度三档(style 向量外推倍率)
            payload["style_weight"] = style_weight
        pp = _pros_params()                # P9.1 韵律跟随:平叙句也随原声表演起伏
        if pp.get("intonation"):
            payload["intonation_scale"] = round(pp["intonation"], 3)
        if pp.get("speed_mult"):
            payload["speed"] = round(max(0.6, min(1.6, payload["speed"] * pp["speed_mult"])), 3)
        if pp.get("w_add") and emotion:
            payload["style_weight"] = round(min(3.0, (style_weight or 1.3) + pp["w_add"]), 2)
    elif is_s1:
        if emotion:
            payload["emotion"] = emotion
        if ST.voice_b64:
            payload["reference_audio_b64"] = ST.voice_b64
            payload["reference_text"] = ST.ref_text
    elif ST.voice_b64:
        payload["reference_audio_b64"] = ST.voice_b64
        payload["reference_text"] = ST.ref_text
    r = _HTTP_POOL.post(f"{bu}/v1/tts/clone/stream", json=payload, stream=True, timeout=120)
    r.raise_for_status()
    sr = int(r.headers.get("X-Sample-Rate", "44100"))
    # 首段立即入队(淡入、无尾淡)→ 尽快出声；其后段用单段前瞻，只给真末段淡出。
    # 流内为连续样本，段间本就无缝、无需中间淡化。
    buf = b""; idx = 0; prev = None
    try:
        for raw in r.iter_content(chunk_size=4096):
            if ST.play_q is None:
                break
            buf += raw
            while len(buf) >= 4:
                ln = struct.unpack("<I", buf[:4])[0]
                if ln == 0:                # 结束帧
                    buf = b""; break
                if len(buf) < 4 + ln:
                    break
                pcm = buf[4:4+ln]; buf = buf[4+ln:]
                if len(pcm) < 2205:        # 跳过 priming/碎帧(<0.05s@44.1k)
                    continue
                d = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
                if sr != dev_sr:
                    d = _resample(d, sr, dev_sr)
                if idx == 0:               # 首段:立即入队，最快出声
                    _put_block(q, d, dev_ch, fin_ms=12, fout_ms=0, dev_sr=dev_sr)
                else:                      # 其后:前瞻一段，确保只末段淡出
                    if prev is not None:
                        _put_block(q, prev, dev_ch, fin_ms=0, fout_ms=0, dev_sr=dev_sr)
                    prev = d
                idx += 1
        if prev is not None:               # 真末段淡出
            _put_block(q, prev, dev_ch, fin_ms=0, fout_ms=10, dev_sr=dev_sr)
    except Exception:
        # 中途断流(常见于换脸抢 GPU 致流式超时):若本句已出声则吞掉异常，
        # 不让上层回退到分块非流式——否则整句会被重新合成播一遍(“说两遍”)。
        # 若尚未出声(idx==0)则上抛，由上层正常回退兜底。
        if idx == 0:
            raise
        logger.warning("流式合成中途断开，已部分出声，跳过回退以免整句重复")
    finally:
        r.close()
    return idx


def _enqueue_wav(raw: bytes):
    """已合成的 WAV 字节 → 适配设备格式 → 推入全局播放队列(VB-Cable)。
    直播兜底复用已合成音频时用,不二次 TTS。"""
    q = ST.play_q
    if q is None or not raw:
        return
    dev_sr, dev_ch = ST.play_sr, ST.play_ch
    d, sr = _wav_bytes_to_f32(raw)
    d = _trim_silence(d, sr)
    if d is None or d.size == 0:
        return
    if sr != dev_sr:
        d = _resample(d, sr, dev_sr)
    _put_block(q, d, dev_ch, fin_ms=12, fout_ms=12, dev_sr=dev_sr)


# P9.3 真笑声素材库：data\laughs\<角色>\*.wav(CosyVoice [laughter] 克隆生成,离线加工)。
# 笑意句在 TTS 正文前插一段真笑声——纯 TTS 念"ハハハ"永远不像真笑。
_LAUGH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "laughs")
_laugh_cache: dict = {}


def _laugh_clip(profile: str) -> bytes:
    """随机取该角色的一段笑声 wav 字节(内存缓存)。无素材返回 b""。"""
    try:
        d = os.path.join(_LAUGH_DIR, profile or "")
        if not os.path.isdir(d):
            return b""
        files = _laugh_cache.get(d)
        if files is None:
            files = [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(".wav")]
            _laugh_cache[d] = files
        if not files:
            return b""
        p = files[int(time.time() * 997) % len(files)]   # 轮换避免每次同一声笑
        b = _laugh_cache.get(p)
        if b is None:
            with open(p, "rb") as f:
                b = f.read()
            _laugh_cache[p] = b
        return b
    except Exception:
        return b""


def _sent_gap():
    """P2-D1 句间呼吸间隙：仅当播放队列有积压时,在新句音频前垫 SENT_GAP_MS 静音。
    队列空=对方在等声,零延迟原则不垫；静音块不算首音(埋点时戳垫后还原)。pool_a 串行无竞态。"""
    q = ST.play_q
    if not SENT_GAP_MS or q is None or q.qsize() == 0 or ST.muted:
        return
    try:
        prev = getattr(ST, "_tts_first_ts", 0.0)
        d = np.zeros(int(ST.play_sr * SENT_GAP_MS / 1000.0), dtype=np.float32)
        _put_block(q, d, ST.play_ch, dev_sr=ST.play_sr)
        if prev == 0.0:
            ST._tts_first_ts = 0.0             # 呼吸静音不是"出声":首音时戳留给真语音块
    except Exception:
        pass


def _enqueue_synth(en: str, emotion: str = "", laugh: bool = False, style_weight: float = 0.0):
    """整句流式合成 → 适配设备格式 → 按序推入全局播放队列(满则阻塞=背压，控制超前量)。
    不阻塞等播完：推完即返回 → 上层可立刻处理下一段(其合成与本段播放重叠)。
    emotion 非空=强情感句：ja+SBV2 直接在主链路带五情绪 style(原生情感,零改道延迟)；
    其它引擎先试 CosyVoice3 克隆+情感指令(整句)，失败原路流式兜底。"""
    q = ST.play_q                          # 取一次引用，避免 /stop 置 None 的竞态
    if q is None:
        return
    if not _voice_ready():                 # 无参考音必败：源头跳过+节流告警,不再每句撞引擎
        _note_novoice_skip()
        return
    if laugh and not _ja_route_s1(emotion, laugh, style_weight) and _ja_use_sbv2() and q.qsize() < 2:
        clip = _laugh_clip(getattr(ST, "profile", ""))
        if clip:
            try:
                _enqueue_wav(clip)
            except Exception:
                pass
    if emotion and not _ja_use_sbv2():     # P0/P2a 情感改道(SBV2 原生情感,无需改道)
        if q.qsize() >= 2:                 # 播放积压中：本句放弃改道,防连续情感句延迟滚雪球
            _emo_stats["skipped_busy"] += 1
        else:
            if _synth_emotional_stream(en, emotion, q, ST.play_sr, ST.play_ch, laugh=laugh):
                return                     # 流式已出声(P2a 首选,免整句等待)
            wav = _synth_emotional(en, emotion, laugh=laugh)
            if wav:
                _enqueue_wav(wav)
                return
    dev_sr, dev_ch = ST.play_sr, ST.play_ch
    # 背压自适应：积压逼近上限(说话快于合成/播放)→ 告警(流式天然单次调用，无需再切粗)
    if STREAM_TTS and q.qsize() >= max(2, q.maxsize - 2):
        ST.note_backlog()
    eng = _resolve_tts_engine(emotion, laugh, style_weight)
    if STREAM_HTTP and _engine_supports_clone_stream(eng):
        for bu in _tts_urls(emotion, laugh, style_weight):
            try:
                _enqueue_synth_stream(en, q, dev_sr, dev_ch, base_url=bu,
                                      emotion=emotion, style_weight=style_weight, laugh=laugh)
                if emotion and eng in ("sbv2", "s1"):
                    _emo_stats["routed"] += 1
                return
            except Exception:
                logger.exception(f"HTTP 流式合成失败@{bu}，尝试下一候选/回退分块非流式")
    # 回退:分块非流式(首块小→快出声，其后大→省开销)
    chunks = _split_en_chunks(en) if STREAM_TTS else [en]
    last = len(chunks) - 1
    for i, c in enumerate(chunks):
        try:
            b64 = _synth_en(c)
            if not b64:
                continue
            d, sr = _wav_bytes_to_f32(base64.b64decode(b64))
            d = _trim_silence(d, sr)
            if d is None or d.size == 0:
                continue
            if sr != dev_sr:
                d = _resample(d, sr, dev_sr)
            _put_block(q, d, dev_ch, fin_ms=(12 if i == 0 else 0),
                       fout_ms=(12 if i == last else 0), dev_sr=dev_sr)
        except Exception:
            logger.exception("分块配音合成/入队失败")


# ── P1-F 填充音垫场：克隆音色的"气口"盖住翻译/TTS 空窗 ─────────────────────
# 真人同传在组织语言时会发"嗯…/Okay, so..."的气口,对方由此知道"线路没断、马上有话"。
# 定稿后超 FILLER_AFTER_MS 仍无本句音频且播放队列已空 → 播一段预生成的克隆音气口。
# 素材:data/fillers/<角色>/<语>/fNN.wav。开播时若缺,后台用当前音色自动生成一次(离线复用);
# 生成失败=静默无垫场(功能可缺,不可碍事)。仅通话模式(直播垫场音与口型必然错位)。
_FILLER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "fillers")
_FILLER_PHRASES = {
    "en": ["Um...", "Okay, so...", "Well...", "Right..."],
    "ja": ["えっと…", "そうですね…", "うーん…"],
    "zh": ["嗯……", "那个……", "好……"],
    "ko": ["음…", "그러니까…", "네…"],
    "ru": ["Ну…", "Так…", "Хм…"],
    "es": ["Eh...", "Bueno...", "A ver..."],
}
# P3-B 附和语(对方长句后的"我在听"信号)：短、轻、不含实义,防跟正片抢语义。
_BC_PHRASES = {
    "en": ["Mm-hm.", "Right.", "Okay."],
    "ja": ["うんうん。", "はい。", "なるほど。"],
    "zh": ["嗯嗯。", "好的。", "对。"],
    "ko": ["네.", "그렇군요."],
    "ru": ["Ага.", "Да-да."],
    "es": ["Ajá.", "Claro."],
}
_filler_state = {"last": 0.0, "bc_last": 0.0, "cache": {}, "gen_key": "", "rr": 0}
_filler_lock = threading.Lock()


def _filler_phrases(lang: str, cat: str = "f") -> list:
    tbl = _BC_PHRASES if cat == "bc" else _FILLER_PHRASES
    dflt = ["Mm-hm."] if cat == "bc" else ["Mm...", "Okay..."]
    return tbl.get((lang or "").split("-")[0].lower(), dflt)


def _filler_clips(cat: str = "f") -> list:
    """当前(角色,目标语,类别)的素材 wav 字节列表(内存缓存)。cat: f=垫场气口 bc=附和。
    文件名 bc* 归附和,其余(含用户手放的任意名)归气口。无素材返回 []。"""
    key = f"{getattr(ST, 'profile', '')}|{(_DST_LANG or '').lower()}|{cat}"
    with _filler_lock:
        if key in _filler_state["cache"]:
            return _filler_state["cache"][key]
    clips = []
    try:
        d = os.path.join(_FILLER_DIR, getattr(ST, "profile", "") or "_default",
                         (_DST_LANG or "en").split("-")[0].lower())
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if not f.lower().endswith(".wav"):
                    continue
                if (cat == "bc") != f.lower().startswith("bc"):
                    continue
                with open(os.path.join(d, f), "rb") as fh:
                    clips.append(fh.read())
    except Exception:
        clips = []
    with _filler_lock:
        _filler_state["cache"][key] = clips
    return clips


def _filler_prepare_kick():
    """开播后台补齐气口素材：该(角色,语)目录为空且当前音色可合成 → 逐条 TTS 生成落盘。
    与 LLM/参考音预热错峰(延迟 6s),best-effort,失败静默(垫场自动缺席)。"""
    if not (FILLER_ON and not ST.live_mode and _voice_ready()):
        return
    prof = getattr(ST, "profile", "") or "_default"
    lang = (_DST_LANG or "en").split("-")[0].lower()
    gen_key = f"{prof}|{lang}"
    with _filler_lock:
        if _filler_state["gen_key"] == gen_key:      # 本进程已试过(成败皆不重试,防每次开播都打 TTS)
            return
        _filler_state["gen_key"] = gen_key

    def _job():
        try:
            time.sleep(6.0)
            d = os.path.join(_FILLER_DIR, prof, lang)
            wavs = ([f.lower() for f in os.listdir(d) if f.lower().endswith(".wav")]
                    if os.path.isdir(d) else [])
            batches = []                             # 分类补缺:老目录只有气口时也能补上附和素材
            if not any(not f.startswith("bc") for f in wavs):
                batches.append(("f", _filler_phrases(lang)))
            if BACKCHANNEL_ON and not any(f.startswith("bc") for f in wavs):
                batches.append(("bc", _filler_phrases(lang, "bc")))
            if not batches or not (ST.running and _voice_ready()):
                return
            os.makedirs(d, exist_ok=True)
            n = 0
            for pre, phrases in batches:
                for i, p in enumerate(phrases):
                    try:
                        b64 = _synth_en(p)
                        raw = base64.b64decode(b64) if b64 else b""
                        if len(raw) > 44:
                            with open(os.path.join(d, f"{pre}{i:02d}.wav"), "wb") as fh:
                                fh.write(raw)
                            n += 1
                    except Exception:
                        continue
            if n:
                with _filler_lock:                   # 使缓存失效,下次取即加载新素材
                    _filler_state["cache"] = {}
                logger.info(f"[垫场] 气口/附和素材已生成 {n} 条({prof}/{lang})")
        except Exception:
            pass
    threading.Thread(target=_job, name="filler-prep", daemon=True).start()


def _filler_arm():
    """定稿进翻译前武装一次垫场检查：FILLER_AFTER_MS 后若本句仍未出声且无积压在播 → 播气口。
    竞态窗口仅"查后入队"数毫秒,最坏也只是气口紧贴正片,听感自然。"""
    if not (FILLER_ON and ST.running and not ST.live_mode and ST.play_q is not None):
        return

    def _job():
        try:
            time.sleep(FILLER_AFTER_MS / 1000.0)
            q = ST.play_q
            if q is None or not ST.running or getattr(ST, "muted", False):
                return
            if getattr(ST, "_tts_first_ts", 0.0):    # 本句已出声
                return
            if q.qsize() > 0:                        # 上句音频还在排队,对方耳中不缺声
                return
            now = time.time()
            if now - getattr(ST, "_b_voice_ts", 0.0) < 0.6:
                return                               # P3-T 对方正在说话:此刻垫"嗯…"像打断,不垫
            with _filler_lock:
                if now - _filler_state["last"] < FILLER_MIN_GAP_S:
                    return
                _filler_state["last"] = now
                _filler_state["rr"] += 1
                rr = _filler_state["rr"]
            clips = _filler_clips()
            if not clips:
                return
            _enqueue_wav(clips[rr % len(clips)])
            ST.llms_stats["filler"] += 1
            logger.info(f"[垫场] 本句 {FILLER_AFTER_MS}ms 未出声,已播气口垫场")
        except Exception:
            pass
    threading.Thread(target=_job, name="filler", daemon=True).start()


# ── P3-T/B 接话规划(Dialogue Planner v1)：说话时戳/句首礼让/倾听附和 ─────────
def _note_voice(direction: str):
    """登记双方语音活跃时戳(流式 partial/分段有效语音都算)。b 侧另记"开口时刻"
    (静默 >1s 后再活跃=新一轮开口),供礼让判断"刚开口"还是"长篇讲话中"。"""
    now = time.time()
    if direction == "b":
        if now - getattr(ST, "_b_voice_ts", 0.0) > 1.0:
            ST._b_voice_start = now
        ST._b_voice_ts = now
    else:
        ST._a_voice_ts = now


def _turn_hold():
    """P3-T 句首礼让：对方刚开口(<1.5s)时我方新句最多等 TURN_HOLD_MS,对方一停顿
    (0.35s 无新活跃)立即开播。对方长篇讲话中不等(同传本来就压话说);仅通话、仅句首。"""
    if not TURN_HOLD_MS or ST.live_mode:
        return
    deadline = time.time() + TURN_HOLD_MS / 1000.0
    held = False
    while time.time() < deadline:
        now = time.time()
        if now - getattr(ST, "_b_voice_ts", 0.0) > 0.35:
            break                                    # 对方停了(或本来就没说话)
        if now - getattr(ST, "_b_voice_start", 0.0) > 1.5:
            break                                    # 对方长篇讲话中:不无限礼让
        held = True
        time.sleep(0.06)
    if held:
        ST.llms_stats["hold"] = ST.llms_stats.get("hold", 0) + 1


def _backchannel_kick(other_text: str):
    """P3-B 倾听附和：对方长句定稿 3s 后我方仍无动静(没说话/没音频在播) → 克隆音轻附和。
    30s 防抖;素材缺失/直播模式=空操作。"""
    if not (BACKCHANNEL_ON and FILLER_ON and ST.running
            and not ST.live_mode and ST.play_q is not None):
        return
    if len(_flat_text(other_text or "")) < BC_MIN_SRC_CHARS:
        return

    def _job():
        try:
            time.sleep(3.0)
            q = ST.play_q
            if q is None or not ST.running or getattr(ST, "muted", False):
                return
            now = time.time()
            if now - getattr(ST, "_a_voice_ts", 0.0) < 3.0:
                return                               # 我已在接话(译文马上到,不用嗯嗯)
            if q.qsize() > 0:
                return                               # 有音频在排队=我方声音已在路上
            with _filler_lock:
                if now - _filler_state["bc_last"] < BC_MIN_GAP_S:
                    return
                _filler_state["bc_last"] = now
            clips = _filler_clips("bc")
            if not clips:
                return
            _enqueue_wav(clips[int(now) % len(clips)])
            ST.llms_stats["bc"] = ST.llms_stats.get("bc", 0) + 1
            logger.info("[附和] 对方长句后我方静默,已轻附和(我在听)")
        except Exception:
            pass
    threading.Thread(target=_job, name="backchannel", daemon=True).start()


# ── 引擎状态 ──────────────────────────────────────────────────────────
class State:
    def __init__(self):
        self.running = False
        self.cap_a = None          # 我的麦
        self.cap_b = None          # 对方(环回)
        self.stream_on = False     # 本会话是否启用流式逐词(Nemotron)
        self.disp_a = None         # 流式分派器(方向A)
        self.disp_b = None         # 流式分派器(方向B)
        # 流式观测:逐词数/定稿数/GPU让位次数(方向A口型独占)/partial 时戳(算实时速率)
        self.stream_stats = {"part_a": 0, "part_b": 0, "fin_a": 0, "fin_b": 0,
                             "yields": 0, "part_ts": deque(maxlen=80)}
        # P0-R3 首音埋点:本句首块音频进播放队列的时刻(0=未出声)。pool_a 串行保证无竞态。
        self._tts_first_ts = 0.0
        # P0-R1 语义块流式配音观测:committed=提前配音块数 tail=定稿尾段补配数
        # blocked=预检未过退回整句数 mismatch=定稿与已配前缀对不上数(只丢尾段,不重播)
        self.chunk_stats = {"committed": 0, "tail": 0, "blocked": 0, "mismatch": 0, "prefetch": 0}
        # P1 观测:used=走流式翻译句数 segs=流式出声段数 bail=段健全闸拒绝数 filler=垫场次数
        # P3 追加:hold=句首礼让次数 bc=倾听附和次数(键按需增,报告用 get 兜底)
        self.llms_stats = {"used": 0, "segs": 0, "bail": 0, "filler": 0}
        # P3-T/B 双方语音活跃时戳(礼让/附和/垫场判据):a=我 b=对方 b_start=对方本轮开口时刻
        self._a_voice_ts = 0.0
        self._b_voice_ts = 0.0
        self._b_voice_start = 0.0
        self.asr_unloaded = False  # 流式下 Whisper 是否已卸载(显存优化生效标志)
        # B-5 当前 ASR 引擎路由真相 {engine,label,why}：/start 时定格,经 /metrics 常驻显示在
        # 本页顶栏+Hub 观测条——弱语种自动回退不再只是开播瞬间闪过的一条事件,整场可见用的是哪个引擎、为什么。
        self.asr_route = {}
        self.profile = ""
        self.mode = "local"         # local=transcribe+本地NMT(快·准·离线) / whisper=Whisper直译英文(偶有词误)
        self.mic_index = None
        self.loop_index = None
        self.loop_is_output = False
        self.cable_index = None
        self.mic_net_url = ""      # 手机麦无线中继URL(空=用物理麦)；供切语向自动重启原样重建
        self.voice_b64 = ""        # 角色音色参考(克隆英文用)
        self.ref_text = ""
        self.pool_a = None
        self.pool_b = None
        self.play_q = None         # 全局有序配音播放队列(PCM 块，已适配设备格式)
        self.play_thread = None
        self.play_stop = None
        self.play_sr = SR
        self.play_ch = 1
        self.events = deque(maxlen=500)
        self.event_id = 0
        self.lock = threading.Lock()
        self.stats = {"a": 0, "b": 0}
        self._uid = 0
        # 轮次跟踪(同说话人近 TURN_GAP 内的子句归为一轮)+ 软终判润色累积源文
        self._turn = {"me": {"id": 0, "last": 0.0}, "other": {"id": 0, "last": 0.0}}
        self._turn_seq = 0
        self.turn_src = {}         # turn_id -> {"who","lang","texts":[],"last","done"}
        self.fin_stop = None       # 软终判线程停止事件
        self.fin_thread = None
        self.metrics = deque(maxlen=200)   # 每段各阶段耗时(观测)
        self.transcript = deque(maxlen=TRANSCRIPT_MAX)  # 会话转写留存(定稿原文+译文+相对时间戳),供 TXT/SRT/JSON 导出
        self.transcript_seq = 0    # 转写自增序号
        self.session_start = 0.0   # 本会话起点(供转写相对时间戳/SRT 计时)
        self.dropped = 0           # 背压丢弃的配音块计数
        # 各层幻听拦截计数(供"音频健康"面板：让用户看清"没出字幕"是被哪层挡的)
        self.drops = {"gate": 0, "halluc": 0, "filler": 0, "lowconf": 0, "dedup": 0, "spk": 0, "echo": 0}
        # P0-① 终稿 GER 纠错观测：checked=复核句数 fixed=改写生效 rejected=改写被拼音闸否决
        # revived=门控存疑句复核后晋升(误杀救回) vetoed=存疑句复核后确认为噪声撤回
        # garbage=P0h 垃圾字符闸拒绝(全角字母/数字/�/凭空ASCII词)——单列分桶:占比升高=仲裁模型该换/该调
        self.ger_stats = {"checked": 0, "fixed": 0, "rejected": 0, "revived": 0, "vetoed": 0,
                          "skipped": 0, "garbage": 0, "overfix": 0}
        self.mt_stats = {"llm_reject": 0}
        self.review_clips = []     # P3 复盘剪辑索引(本场,≤_REVIEW_MAX;音频在 logs/review_audio/<stamp>/)
        self.review_stamp = ""     # P3 本场剪辑目录戳(会话开始时间)
        # P-Affirm 声纹拦截缓存(近8句)：被拦句音频留证 → 控制台灰行显示拦截原因,
        # 一键「是我·放行并学习」把该句学进底座并补跑翻译/配音(误拦不再是黑洞)
        self.spk_blocked = deque(maxlen=8)
        self.muted = False         # P1-4 急停：True=丢弃一切待播/正播配音(≤0.1s 内切断)
        self.bargein_count = 0     # Phase B-4：用户抢话自动打断次数
        self.monitor_on = False    # P1-4 耳返：克隆音小音量同步回放到默认输出(你能听到对方听到的)
        self.monitor_q = None      # 耳返播放队列(尽力而为,满即丢,绝不拖累主链路)
        self.monitor_stop = None
        self.monitor_thread = None
        self.call_report = None    # P1-1 一键通话模式最近一次就绪报告
        self.readback_on = False   # P2-2 对方译文朗读：用对方音色克隆读中文译文到默认输出
        self.rb_ref_b64 = ""       # 对方参考音(WAV b64,锁定后整场复用)
        self.rb_ref_text = ""      # 参考音对应的原文转写(克隆需要)
        self.rb_ref_sec = 0.0
        self.rb_busy = 0           # 在途朗读合成数(>=2 时丢新句,永不堆积)
        # 耳返/朗读正在外放的截止时刻(半双工回声闸)：立体声混音收的是"默认输出的一切"，
        # 我们自己放的耳返/朗读也会被它录回→方向B若不闸会把自家朗读再翻一遍(自激循环)。
        self.aux_out_until = 0.0
        self._last_backlog_warn = 0.0
        self.live_mode = False     # 直播模式:英文驱动数字人口型→OBS虚拟摄像头(否则配音→VB-Cable)
        self.face_id = ""          # 数字人人脸缓存ID(角色激活时预计算)
        self.face_ready = False    # 人脸预热是否就绪(就绪前直播输出回退/等待)
        self.idle_video = ""       # 角色真人待机视频路径(有则 Hub 激活时已推 vcam)
        self.warm_ms = 0           # 直播口型热启动(UNet 编译预热)耗时,0=未预热
        self.last_session = None   # 最近一次会话观测摘要(供 Hub 控制台读取)
        self._last_lag_warn = 0.0  # 口型段间隔 SLA 告警节流
        self._avatar_calls = 0     # 已完成口型驱动句数(首句含编译,SLA 跳过)
        self.demo_running = False  # 演示模式运行中
        self.demo_stop = None      # 演示模式停止事件
        self.pending_switch = None # 待生效的角色切换(就绪后下一句边界原子切换,不断流)
        self.switching = False     # 角色切换准备中
        self.live_degraded = False # 直播自愈降级中(lipsync/vcam 异常→临时配音/字幕)
        self._avatar_fail = 0      # 连续口型驱动失败计数(≥2 即降级)
        self._last_degrade_warn = 0.0
        self.heal_stop = None      # 自愈 watchdog 停止事件
        self.heal_thread = None
        self.preset_cache = {}     # 角色预案池:profile -> 已就绪的切换包(预计算/参考预热完成),切换近即时
        self.preset_disk = {}      # 上次落盘的预案池元数据(指纹/face_id),用于跨会话复用与失效判断
        self.preset_loading = False
        self.preset_queue = []     # P5: 预载排队中的角色名(供 UI 显示"谁在排队/谁已就绪")
        self.switch_count = 0      # 本场角色切换生效次数
        self.degrade_count = 0     # 本场降级触发次数
        self.degrade_ms = 0        # 本场累计降级时长(已结束的降级区间)
        self._degrade_since = 0.0  # 当前降级区间起点(0=未降级)
        self._post_switch_probe = False  # 切换后首句做一次 A/V 对齐采样
        self._post_recover_probe = False # 降级恢复后首句做一次 A/V 复检
        self.gpu = {}              # 直播启动时 GPU 快照(争用自检)
        self._avatar_inflight = 0  # 正在生成口型的直播句数(>0=直播占用 GPU,预载应让位)
        self._last_avatar_ts = 0.0 # 最近一次口型驱动时刻(预载据此让位活跃直播)
        self._gpu_streak = 0       # 自愈watchdog连续探到争用的次数(持续争用→升级置顶告警)
        self.gpu_alert = False     # 持续争用置顶告警态(供UI显示"建议独占显卡")
        # P0-S2S 云同传态：s2s_on=本会话方向A是否走云端；s2s_state=观测(连接/句数/回退/错误)
        self.s2s_on = False
        self.s2s_sink = None       # 云同传汇(供 /config/s2s 中途刷新术语表)
        self.s2s_state = {}
        self.stream_sink_a = None  # P0-R1 方向A流式采集汇(块提交前 peek 当前句音频做门控/声纹预检)

    def set_degraded(self, flag: bool) -> bool:
        """切换降级态并累计统计;返回是否发生状态变化(供调用方决定是否告警)。"""
        with self.lock:
            if bool(flag) == self.live_degraded:
                return False
            self.live_degraded = bool(flag)
            if flag:
                self.degrade_count += 1
                self._degrade_since = time.time()
            elif self._degrade_since:
                self.degrade_ms += int((time.time() - self._degrade_since) * 1000)
                self._degrade_since = 0.0
            return True

    def degraded_ms_total(self) -> int:
        """累计降级时长(含当前进行中的区间)。"""
        extra = int((time.time() - self._degrade_since) * 1000) if self._degrade_since else 0
        return self.degrade_ms + extra

    def next_uid(self) -> int:
        with self.lock:
            self._uid += 1
            return self._uid

    def turn_id(self, who: str) -> int:
        """同说话人距上一子句 < TURN_GAP → 同一轮，否则新一轮。"""
        with self.lock:
            t = self._turn[who]; now = time.time()
            if now - t["last"] > TURN_GAP:
                self._turn_seq += 1; t["id"] = self._turn_seq
            t["last"] = now
            return t["id"]

    def record_turn_src(self, tid: int, who: str, lang: str, text: str):
        if not text:
            return
        with self.lock:
            e = self.turn_src.get(tid)
            if e is None:
                e = {"who": who, "lang": lang, "texts": [], "last": 0.0, "done": False}
                self.turn_src[tid] = e
            e["texts"].append(text); e["last"] = time.time()

    def record_transcript(self, who: str, src: str, trans: str, tid=None):
        """记一条会话转写(定稿)：who=me/other，src=原文，trans=译文。供 /transcript* 导出。
        关闭(TRANSCRIPT_ON=0)或原文/译文皆空则跳过。相对时间戳 t 从会话起点算(SRT 计时用)。"""
        if not TRANSCRIPT_ON:
            return
        src = (src or "").strip(); trans = (trans or "").strip()
        if not src and not trans:
            return
        with self.lock:
            self.transcript_seq += 1
            now = time.time()
            base = self.session_start or now
            self.transcript.append({"seq": self.transcript_seq, "who": who, "turn": tid,
                                    "ts": now, "t": max(0.0, now - base),
                                    "src": src, "trans": trans})

    def drop_transcript_turn(self, who: str, tid):
        """跨引擎去重撤回：该轮已定稿上屏又被判定为重复段 → 从留存稿整轮删除(导出=屏幕稿)。"""
        with self.lock:
            kept = [e for e in self.transcript
                    if not (e.get("turn") == tid and e.get("who") == who)]
            if len(kept) != len(self.transcript):
                self.transcript.clear(); self.transcript.extend(kept)

    def finalize_transcript(self, who: str, tid, src_full: str, trans_full: str):
        """软终判润色回填：把该轮(who+tid)的逐段转写并为一条润色终稿，导出即与屏幕终稿一致。
        保留该轮首段的时间戳/序号与其在序列中的位置(时序不乱、SRT 起点=该轮起点)；删该轮其余段。
        无匹配段(如单子句轮未记多段/该轮未记转写)则不动——永不劣于逐段稿。"""
        if not TRANSCRIPT_ON:
            return
        src_full = (src_full or "").strip(); trans_full = (trans_full or "").strip()
        if not src_full and not trans_full:
            return
        with self.lock:
            items = list(self.transcript)
            idxs = [i for i, e in enumerate(items) if e.get("turn") == tid and e.get("who") == who]
            if not idxs:
                return
            first = idxs[0]; base = items[first]; drop = set(idxs[1:])
            merged = {"seq": base.get("seq"), "who": who, "turn": tid,
                      "ts": base.get("ts"), "t": base.get("t"),
                      "src": src_full or base.get("src", ""),
                      "trans": trans_full or base.get("trans", ""), "polished": True}
            keep = []
            for i, e in enumerate(items):
                if i == first:
                    keep.append(merged)
                elif i not in drop:
                    keep.append(e)
            self.transcript.clear(); self.transcript.extend(keep)

    def add_metric(self, m: dict):
        with self.lock:
            self.metrics.append(m)

    def note_backlog(self):
        """积压告警(节流 5s 一次)：提示语速过快、配音/字幕可能滞后。"""
        now = time.time()
        if now - self._last_backlog_warn > 5.0:
            self._last_backlog_warn = now
            self.push_event({"who": "sys", "warn": "⚠ 语速较快，配音正在追赶（已自动切换粗粒度合成）"})

    def note_degrade(self, msg: str):
        """降级/恢复告警(节流 8s)。"""
        now = time.time()
        if now - self._last_degrade_warn > 8.0:
            self._last_degrade_warn = now
            self.push_event({"who": "sys", "warn": msg})

    def note_lipsync_lag(self, seg_gap_ms: float, seg_dur_ms: float):
        """口型段间隔 SLA 告警(节流 8s)：段生成慢于实时(>1.25x)→ 口型会滞后于说话。
        严重落后(>2.5x)多为他进程争抢 GPU(实测争用下可达 5x)→ 提示排查显卡占用。"""
        now = time.time()
        if seg_gap_ms > seg_dur_ms * 1.25 and now - self._last_lag_warn > 8.0:
            self._last_lag_warn = now
            ratio = seg_gap_ms / max(1.0, seg_dur_ms)
            if ratio >= 2.5:
                self.push_event({"who": "sys",
                                 "warn": f"⚠ 口型严重滞后({ratio:.1f}x)，疑似 GPU 被其他程序占用；"
                                         f"请关闭占卡进程或独占显卡(隔离实测仅 0.4x)"})
            else:
                self.push_event({"who": "sys",
                                 "warn": f"⚠ 口型生成慢于实时({ratio:.1f}x)，画面可能滞后；建议缩短句子或降画质"})

    def push_event(self, ev: dict):
        with self.lock:
            self.event_id += 1
            ev["id"] = self.event_id
            ev["ts"] = time.time()
            self.events.append(ev)

ST = State()


# ── 外部服务调用 ──────────────────────────────────────────────────────
def _stt(mono16k: np.ndarray, language: str, task: str = "transcribe",
         return_meta: bool = False, initial_prompt: str = ""):
    """return_meta=True 时返回 (text, meta)；meta 含远端 Whisper 自报的
    no_speech_prob/avg_logprob/compression_ratio(旧服务端无此字段则为 None)。
    initial_prompt: 热词引导(术语/人名)——旧服务端会忽略该字段(向后兼容)。"""
    wav = _to_wav_bytes(mono16k, SR)
    payload = {"audio_base64": base64.b64encode(wav).decode(),
               "language": language, "task": task}
    if initial_prompt:
        payload["initial_prompt"] = initial_prompt
    r = _stt_post("/transcribe_b64", payload, timeout=30)   # S8-3: 主 STT 失联自动切备用端点
    r.raise_for_status()
    j = r.json()
    text = (j.get("text") or "").strip()
    # P3 实测：噪声/轻声下 Whisper 中文偶发漂去繁体(CER 基准里 8/12 句)。统一简体出稿，
    # 防繁体经 GER 快路径(拼音同音=相似度满分)直接上屏。
    if text and (language or "").lower().startswith("zh"):
        try:
            from zhconv import convert as _zcc
            text = _zcc(text, "zh-cn")
        except Exception:
            pass
    if return_meta:
        return text, {"no_speech_prob": j.get("no_speech_prob"),
                      "avg_logprob": j.get("avg_logprob"),
                      "compression_ratio": j.get("compression_ratio")}
    return text


# ── P0-② 热词引导：术语表 → Whisper initial_prompt(识别前偏置，比事后替换更根治) ──
#   实测痛点：人名/品牌/行话是同音字错误重灾区("通译"→"通义"、"换脸"→"换连")。
#   initial_prompt 是 Whisper 官方上下文偏置通道：把术语放进引导句，解码时相应 token 概率上调。
#   术语来源=data/glossary.json(已有热加载)：取该语向 src 侧 + 反向 dst 侧(双向都可能被说出)。
_ASR_HOTWORDS_ON = os.environ.get("INTERP_ASR_HOTWORDS", "1") == "1"
_hotword_cache = {"ver": -1, "by_lang": {}}


def _asr_hotwords(lang: str) -> str:
    """给定识别语言，产出 initial_prompt 热词引导句。空表/关闭 → ""(零行为变化)。"""
    if not _ASR_HOTWORDS_ON:
        return ""
    comp = _glossary_load()
    ver = _glossary_cache["ver"]
    if ver != _hotword_cache["ver"]:
        _hotword_cache["by_lang"] = {}
        _hotword_cache["ver"] = ver
    lang = (lang or "").lower()
    hit = _hotword_cache["by_lang"].get(lang)
    if hit is not None:
        return hit
    terms, seen = [], set()
    for key, items in comp.items():
        parts = key.split("->")
        for (s, d, _is_latin) in items:
            # 该语言可能被说出的写法：语向源语=lang 取 src；语向目标语=lang 取 dst；'*' 两侧都取
            cands = []
            if key == "*":
                cands = [s, d]
            elif len(parts) == 2:
                if parts[0] == lang:
                    cands = [s]
                elif parts[1] == lang:
                    cands = [d]
            for c in cands:
                c = (c or "").strip()
                if c and c.lower() not in seen:
                    seen.add(c.lower()); terms.append(c)
    if not terms:
        # P3 实测：噪声音频下无 prompt 时 Whisper 常漂去繁体/粤语脚本。空表也给中文一句
        # 基础引导锚定"普通话+简体"(其它语言维持空,避免误引导)。
        base = "以下是普通话内容。" if lang == "zh" else ""
        _hotword_cache["by_lang"][lang] = base
        return base
    terms = terms[:24]                                   # prompt 预算(whisper 上限 224 token)
    if lang == "zh":
        out = "以下是普通话内容，可能出现这些词语：" + "、".join(terms) + "。"
    else:
        out = "Vocabulary that may appear: " + ", ".join(terms) + "."
    _hotword_cache["by_lang"][lang] = out
    return out


# ══════════ P0-① 终稿 GER 纠错闭环(生成式纠错) + P0-④ 门控标记制复核 ══════════
#   动机：流式引擎(Nemotron 0.6B)终稿常见同音/近音字错，降质段还会整句幻听外语；
#   而门控"宁可错杀"的拦截哲学与通话产品"说了必上屏"矛盾(当日实测 4 次误杀)。
#   设计(RLLM-CF 三段式思想,单次调用+硬闸落地)：
#     正稿复核：终稿已上屏/已配音后【异步】——①强模型 Whisper(large-v3-turbo)对同段
#       音频重转写(异构第二假设) ②两假设一致=通过(零 LLM 开销)；不一致才请本机 LLM
#       仲裁纠错(带术语表约束) ③拼音相似度闸(纠错只许换同音/近音字,防 LLM 改写句意)
#       → 通过才推字幕替换事件。实时链路零延迟：配音照旧即刻播出,纠错只改屏幕稿+留存稿。
#     存疑复核(标记制)：可恢复类门控拒绝(底噪/幻听/填充词/语种漂移)不再无声丢弃——
#       灰字上屏 + 同段音频送强模型复核：确认真话→晋升正稿(翻译+补配音,误杀救回)；
#       确认噪声→撤回灰字。回声/声纹/连刷仍硬拦截(保护对方不被自激循环轰炸)。
_GER_ON        = os.environ.get("INTERP_GER", "1") == "1"
_GER_MODEL     = os.environ.get("INTERP_GER_MODEL", "").strip()      # 空=复用 INTERP_LLM_MODEL
_GER_TIMEOUT   = float(os.environ.get("INTERP_GER_TIMEOUT", "12"))
_GER_MIN_CHARS = int(os.environ.get("INTERP_GER_MIN_CHARS", "4"))    # 短于此的正稿无纠错空间
_GER_PEND_MAX  = int(os.environ.get("INTERP_GER_PEND_MAX", "6"))     # 复核积压上限(语速快时丢最旧复核,不丢正稿)
_GER_PY_SIM    = float(os.environ.get("INTERP_GER_PY_SIM", "0.62"))  # 拼音闸:纠错稿与原稿最低发音相似度
_GER_SUSPECT_ON = os.environ.get("INTERP_GER_SUSPECT", "1") == "1"   # 门控标记制(存疑复核)开关
# P1 再优化：两假设发音几乎一致(≥此阈值)时=纯同音字之争 → 直接采信强引擎(turbo)稿，
# 免去 LLM 仲裁(实测省 1~3.5s)。低于阈值说明两引擎听到的内容真有分歧,仍走 LLM 裁决。
_GER_TRUST_SIM = float(os.environ.get("INTERP_GER_TRUST_SIM", "0.85"))
_ger_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ger")
_ger_lock = threading.Lock()
_ger_pending = {"n": 0}

try:
    from pypinyin import lazy_pinyin                     # 拼音闸(同音字校验)；缺库→退化字符相似度
    _HAS_PINYIN = True
except Exception:
    _HAS_PINYIN = False


def _pron_key(text: str) -> str:
    """发音归一键：中文转无声调拼音串，其余取字母数字小写。空文本 → ""。"""
    t = "".join(ch for ch in (text or "") if ch.isalnum())
    if not t:
        return ""
    if _HAS_PINYIN and any("\u4e00" <= c <= "\u9fff" for c in t):
        try:
            return "".join(lazy_pinyin(t)).lower()
        except Exception:
            pass
    return t.lower()


def _pron_sim(a: str, b: str) -> float:
    """两句话的发音相似度 0~1(拼音序列 SequenceMatcher)。任一为空 → 0。"""
    ka, kb = _pron_key(a), _pron_key(b)
    if not ka or not kb:
        return 0.0
    return difflib.SequenceMatcher(None, ka, kb).ratio()


def _ger_llm_fix(text: str, hyp2: str, lang: str, context: str = "") -> str:
    """LLM 仲裁纠错：给 1~2 个 ASR 假设 → 输出修正句(只换同音/近音字,不改写)。失败/不可用 → ""。
    context=同说话人上一句(帮助同音字按语境消歧,只作参考不修改)。"""
    global _llm_fail_until
    if not _llm_ready():
        return ""
    lname = _LANG_NAMES.get(lang, lang)
    hint = _asr_hotwords(lang)
    mh = _ger_mistake_hints(lang)          # P2 高频误写负面提示(自学习台账反哺仲裁)
    sys_prompt = (
        f"You are a proofreader for real-time {lname} speech-recognition transcripts. "
        f"The transcript may contain homophone or near-homophone recognition errors. Rules: "
        f"1) Fix ONLY clear mis-recognitions (characters/words replaced by same- or similar-sounding ones); "
        f"2) NEVER rephrase, never add/remove content, never translate, keep punctuation style; "
        f"3) If hypothesis 2 exists it comes from a stronger engine — prefer its wording where they differ, "
        f"as long as the pronunciation matches; "
        f"4) If the transcript is already correct, output hypothesis 1 unchanged; "
        f"5) Output ONLY the corrected sentence, no quotes, no explanations."
        + (f" Preferred domain spellings: {hint}" if hint else "")
        + (f" Recurring mis-recognitions seen before (wrong->correct): {mh}." if mh else "")
    )
    user = ((f"previous sentence (context only, do NOT output it): {context}\n" if context else "")
            + f"hypothesis 1: {text}" + (f"\nhypothesis 2: {hyp2}" if hyp2 else ""))
    try:
        r = _HTTP_POOL.post(f"{_LLM_URL}/api/chat", json={
            "model": _GER_MODEL or _LLM_MODEL,
            "messages": [{"role": "system", "content": sys_prompt},
                         {"role": "user", "content": user}],
            "stream": False, "think": False, "keep_alive": _LLM_KEEP,
            "options": {"temperature": 0.0, "top_p": 0.8,
                        "num_predict": 220, "num_ctx": 1024},
        }, timeout=_GER_TIMEOUT)
        r.raise_for_status()
        out = ((r.json().get("message") or {}).get("content") or "")
        out = _THINK_RE.sub("", out)
        out = " ".join(ln.strip() for ln in out.splitlines() if ln.strip())
        out = _re.sub(r"^\s*hypothesis\s*\d\s*[:：]\s*", "", out, flags=_re.IGNORECASE)
        return out.strip().strip('"').strip("“”").strip()
    except Exception as e:
        _llm_fail_until = time.time() + min(_LLM_COOLDOWN, 10.0)   # 与翻译共用熔断(同一后端)
        logger.warning(f"[GER] LLM 纠错失败(冷却)：{e}")
        return ""


# 2026-07-10 实录：7b 仲裁模型会把句尾标点幻改成全角字母/数字/替换符(『…呢?』→『…呢Ｂ』、
# 『…声音。』→『…声音７』、『，』→『�』)。这些不是"发音字符"，拼音闸对它们无感——整句
# 相似度仍≥阈值,垃圾就这么上了字幕。此闸专拦"凭空引入"的非发音垃圾。
_GER_GARBAGE_RE = _re.compile(r"[\uFFFD\uFF10-\uFF19\uFF21-\uFF3A\uFF41-\uFF5A]")   # �/全角数字/全角字母
_ASCII_WORD_RE = _re.compile(r"[A-Za-z]{2,}")


def _ger_garbage_introduced(orig: str, cand: str, lang: str = "") -> bool:
    """纠错稿引入原稿没有的垃圾字符或凭空冒出的 ASCII 词 → True(应拒)。
    同音字修正不可能产出全角字母/数字/�；新增 ASCII 词仅当在 ASR 热词表(术语白名单,
    如 LingoX)里才放行——热词纠错(凌克斯→LingoX)是设计行为,不误伤。"""
    o, c = orig or "", cand or ""
    for ch in set(_GER_GARBAGE_RE.findall(c)):
        if ch not in o:
            return True
    new_words = {w.lower() for w in _ASCII_WORD_RE.findall(c)} \
        - {w.lower() for w in _ASCII_WORD_RE.findall(o)}
    if new_words:
        try:
            hw = (_asr_hotwords(lang) or "").lower() if lang else ""
        except Exception:
            hw = ""
        return any(w not in hw for w in new_words)
    return False


def _ger_overcorrected(orig: str, cand: str) -> bool:
    """同音纠错不该改太多字：对齐后改动超长度 30%(至少允许 1 处) → 过度纠错。
    拼音闸对『嗯对他』→『横距它』这类"发音略像、语义全错"的整句改写无感——此闸专拦。"""
    o, c = _flat_text(orig), _flat_text(cand)
    if not o or not c:
        return False
    n = max(len(o), len(c))
    matches = sum(triple[-1] for triple in difflib.SequenceMatcher(None, o, c).get_matching_blocks())
    return (n - matches) > max(1, int(n * 0.30 + 0.5))


def _ger_gate_check(orig: str, cand: str, lang: str = "") -> str:
    """纠错稿硬闸。返回 ''=通过；否则 'pinyin'|'garbage'|'overfix'|'ratio'|'empty'。"""
    if not cand:
        return "empty"
    ratio = len(cand) / max(1, len(orig))
    if not (0.5 <= ratio <= 2.0):
        return "ratio"
    if _pron_sim(cand, orig) < _GER_PY_SIM:
        return "pinyin"
    if _ger_garbage_introduced(orig, cand, lang):
        ST.ger_stats["garbage"] = ST.ger_stats.get("garbage", 0) + 1
        logger.info(f"[GER] 垃圾字符闸拒绝: {orig!r} -> {cand!r}")
        return "garbage"
    if _ger_overcorrected(orig, cand):
        ST.ger_stats["overfix"] = ST.ger_stats.get("overfix", 0) + 1
        logger.info(f"[GER] 过度纠错闸拒绝: {orig!r} -> {cand!r}")
        return "overfix"
    return ""


def _ger_gate_ok(orig: str, cand: str, lang: str = "") -> bool:
    """纠错稿硬闸：长度比 0.5~2.0 且发音相似度 ≥ _GER_PY_SIM(只许换同音近音字)；
    再拒垃圾字符/凭空 ASCII 词/过度整句改写(见 _ger_gate_check)。"""
    return not _ger_gate_check(orig, cand, lang)


# P4 截断恢复：CER 基准实测轻声(-24dB)下流式引擎 CER 28%(以"整半句丢失"为主:
# "先试用七天满意再付款"→"意在付款"),而 turbo 仅 4%。若 turbo 稿明显更长、且流式稿
# 的发音几乎被它完整包含(=流式稿是截断残句),直接采信 turbo 全句。此路径绕过
# _ger_gate_ok 的长度闸(截断恢复本来就要变长),用包含度+置信度+幻听闸另行把关。
_GER_TRUNC = os.environ.get("INTERP_GER_TRUNC", "1") == "1"


def _ger_trunc_hit(direction: str, text: str, hyp2: str, meta2) -> bool:
    """判定"流式稿是 turbo 稿的截断残句"。True → 调用方直接采 hyp2 全句。"""
    ln_t, ln_h = len(_norm_text(text)), len(_norm_text(hyp2))
    if not (ln_t >= 2 and ln_h >= ln_t + 3 and ln_h <= ln_t * 4 + 12):
        return False                                     # 不够"明显更长"或长得离谱(防幻听扩写)
    kt, kh = _pron_key(text), _pron_key(hyp2)
    if not kt or not kh:
        return False
    m = difflib.SequenceMatcher(None, kt, kh)
    cover = sum(b.size for b in m.get_matching_blocks()) / max(1, len(kt))
    if cover < 0.80:                                     # 流式稿发音 8 成以上要能在 turbo 稿里找到
        return False
    if _is_hallucination(hyp2) or _lang_sanity_drop(direction, hyp2):
        return False
    nsp = (meta2 or {}).get("no_speech_prob")
    lp = (meta2 or {}).get("avg_logprob")
    return (nsp is None or nsp < 0.4) and (lp is None or lp > -1.0)   # turbo 自信才敢补全句


# ── P1-4 纠错自学习：GER 高频修正对自动沉淀进术语表 → 反哺 ASR 热词，越用越准 ──
#   原理：同一个词被反复纠错(如「通义→通译」×2)说明是系统性 ASR 弱点。把正确写法
#   自动写入 glossary.json 的 '*' 语向(恒等映射)——下一句起 initial_prompt 热词就带上它，
#   识别阶段直接偏置到正确写法(源头防错)，同时 MT 术语锁定保护它不被翻译打散。
#   安全闸：只学「已过拼音闸并实际上屏」的纠错；候选词 2~8 字(CJK)或 3~20 字符(拉丁)；
#   出现 ≥ _GER_LEARN_ADOPT 次才采纳；已在词表的不重复；留存上限 200 条防膨胀。
_GER_LEARN_ON    = os.environ.get("INTERP_GER_LEARN", "1") == "1"
_GER_LEARN_ADOPT = int(os.environ.get("INTERP_GER_LEARN_ADOPT", "2"))   # 0=只记录不自动采纳
_GER_LEARN_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ger_learned.json")
_ger_learn_lock  = threading.Lock()


def _cjk_word_span(text: str, j1: int, j2: int):
    """用 jieba 把 [j1,j2) 扩成覆盖它的完整词边界(热词得是词不是单字)。失败→原区间。"""
    try:
        import jieba
        pos = 0
        s0, e0 = j1, j2
        for w in jieba.cut(text):
            s, e = pos, pos + len(w)
            pos = e
            if e <= j1:
                continue
            if s >= j2:
                break
            s0 = min(s0, s); e0 = max(e0, e)
        return s0, e0
    except Exception:
        return j1, j2


def _latin_word_span(text: str, a: int, b: int):
    """把 [a,b) 扩到拉丁词边界(字母数字/'-)。"""
    while a > 0 and (text[a - 1].isalnum() or text[a - 1] in "'-"):
        a -= 1
    while b < len(text) and (text[b].isalnum() or text[b] in "'-"):
        b += 1
    return a, b


def _ger_learn_pairs(orig: str, fixed: str):
    """从(原稿,纠错稿)中抽取词级修正对[(错写法,对写法)]。CJK：字符级 diff 的 replace 片段
    按 jieba 词边界扩成完整词(「通→传」→「通译→传译」，replace 两侧上下文相同故同量扩边
    即得错写法)。拉丁：replace/insert/delete 都扩到词边界(fourty→forty 是删字符不是换字符)。
    过滤：长度 2~6 字(CJK)/3~20(拉丁)、发音相似度 ≥0.5(同音之争才是可学的系统性错误)。"""
    out, seen = [], set()
    try:
        smx = difflib.SequenceMatcher(None, orig, fixed)
        for tag, i1, i2, j1, j2 in smx.get_opcodes():
            if tag == "equal":
                continue
            seg = orig[i1:i2] + fixed[j1:j2]
            if any("\u4e00" <= ch <= "\u9fff" for ch in seg):
                if tag != "replace":
                    continue
                s, e = _cjk_word_span(fixed, j1, j2)
                # P4 词典外新词(人名/品牌,人工标注常见)jieba 切成单字 → span 退化到 1 字被过滤。
                # 借 replace 两侧的等值上下文扩到 ≥2 字(优先向左,中文词多为左偏正结构)。
                if e - s < 2 and s > 0 and "\u4e00" <= fixed[s - 1] <= "\u9fff":
                    s -= 1
                if e - s < 2 and e < len(fixed) and "\u4e00" <= fixed[e] <= "\u9fff":
                    e += 1
                lpad, rpad = j1 - s, e - j2          # 两侧扩的字符在 orig 里就是同样的上下文
                a = orig[max(0, i1 - lpad):i2 + rpad]
                b = fixed[s:e]
                if not (2 <= len(b) <= 6 and all("\u4e00" <= ch <= "\u9fff" for ch in b)):
                    continue
            else:
                ia, ib = _latin_word_span(orig, i1, i2)
                ja, jb = _latin_word_span(fixed, j1, j2)
                a, b = orig[ia:ib].strip(), fixed[ja:jb].strip()
                if not (3 <= len(b) <= 20 and b.replace(" ", "").replace("-", "").isalnum()):
                    continue
            if not a or a == b or (a, b) in seen or _pron_sim(a, b) < 0.5:
                continue
            seen.add((a, b))
            out.append((a, b))
    except Exception:
        pass
    return out


def _ger_learn_load() -> dict:
    try:
        with open(_GER_LEARN_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _glossary_has_term(term: str) -> bool:
    """词表(任意语向的任一侧)是否已含该写法。"""
    t = (term or "").strip().lower()
    for items in _glossary_load().values():
        for (s, d, _lat) in items:
            if t in (s.lower(), d.lower()):
                return True
    return False


def _glossary_adopt_term(term: str) -> bool:
    """把正确写法以恒等映射写进 glossary.json 的 '*' 语向(热加载自动生效)。"""
    try:
        with _glossary_lock:
            try:
                with open(_GLOSSARY_PATH, encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception:
                raw = {}
            lst = raw.get("*")
            if not isinstance(lst, list):
                lst = []
            if any(isinstance(it, dict) and (it.get("src") or "").strip().lower() == term.lower()
                   for it in lst):
                return False
            lst.append({"src": term, "dst": term})
            raw["*"] = lst
            with open(_GLOSSARY_PATH, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
        _glossary_load(force=True)          # 立即重编译(ver 跳,热词缓存/翻译缓存自动失效)
        return True
    except Exception:
        logger.exception("[纠错自学习] 写入术语表失败")
        return False


def _ger_learn_note(orig: str, fixed: str, lang: str):
    """记录一次已生效的纠错；高频修正自动采纳进词表(反哺热词)。best-effort,失败不影响主链。"""
    if not (_GER_LEARN_ON and orig and fixed):
        return
    pairs = _ger_learn_pairs(orig, fixed)
    if not pairs:
        return
    try:
        with _ger_learn_lock:
            store = _ger_learn_load()
            now = time.time()
            for wrong, right in pairs:
                key = f"{lang}|{wrong}\u2192{right}"
                e = store.get(key) or {"lang": lang, "wrong": wrong, "right": right,
                                       "n": 0, "adopted": False}
                e["n"] = int(e.get("n", 0)) + 1
                e["last"] = now
                store[key] = e
                if (_GER_LEARN_ADOPT > 0 and not e.get("adopted")
                        and e["n"] >= _GER_LEARN_ADOPT and not _glossary_has_term(right)):
                    if _glossary_adopt_term(right):
                        e["adopted"] = True
                        logger.info(f"[纠错自学习] 「{wrong}→{right}」出现 {e['n']} 次,"
                                    f"已采纳进术语表热词(下一句生效)")
                        ST.push_event({"who": "sys",
                                       "warn": f"📚 纠错自学习：「{right}」已加入术语热词"
                                               f"(此前 {e['n']} 次被识别成「{wrong}」)"})
            if len(store) > 200:            # 防膨胀:按最近使用留 200 条
                keep = sorted(store.items(), key=lambda kv: kv[1].get("last", 0), reverse=True)[:200]
                store = dict(keep)
            os.makedirs(os.path.dirname(_GER_LEARN_PATH), exist_ok=True)
            with open(_GER_LEARN_PATH, "w", encoding="utf-8") as f:
                json.dump(store, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("[纠错自学习] 记录失败(不影响主链)")


_ger_hint_cache = {"ts": 0.0, "by_lang": {}}


def _ger_mistake_hints(lang: str) -> str:
    """P2 台账反哺仲裁：高频修正对(n≥2)拼成负面提示串「错→对; …」，最多 8 条。
    60s 缓存(台账文件小但纠错高频,不必每次读盘)。无台账/无该语言 → ""(零行为变化)。"""
    now = time.time()
    if now - _ger_hint_cache["ts"] > 60:
        by = {}
        try:
            with _ger_learn_lock:
                store = _ger_learn_load()
            for e in store.values():
                if int(e.get("n", 0)) >= 2 and e.get("wrong") and e.get("right"):
                    by.setdefault((e.get("lang") or "").lower(), []).append(e)
        except Exception:
            by = {}
        _ger_hint_cache["by_lang"] = {
            lg: "; ".join(f"{x['wrong']}->{x['right']}"
                          for x in sorted(v, key=lambda x: -int(x.get("n", 0)))[:8])
            for lg, v in by.items()}
        _ger_hint_cache["ts"] = now
    return _ger_hint_cache["by_lang"].get((lang or "").lower(), "")


def _ger_recent_context(who: str, tid) -> str:
    """同说话人最近一条已定稿原文(排除当前轮)，供 GER 仲裁按语境消歧。无 → ""。"""
    try:
        with ST.lock:
            items = list(ST.transcript)[-8:]
        for e in reversed(items):
            if e.get("who") == who and e.get("turn") != tid and e.get("src"):
                return str(e["src"])[:60]
    except Exception:
        pass
    return ""


def _ger_text_polish(text: str, lang: str) -> str:
    """无音频版纠错(软终判整轮润色前调用)：LLM 修同音字 + 拼音闸。失败/被拒 → 原文。"""
    if not _GER_ON or len(_norm_text(text)) < _GER_MIN_CHARS:
        return text
    cand = _ger_llm_fix(text, "", lang)
    if cand and _norm_text(cand) != _norm_text(text) \
            and _flat_text(cand) != _flat_text(text) \
            and _ger_gate_ok(text, cand, lang):
        ST.ger_stats["fixed"] += 1
        _ger_learn_note(text, cand, lang)                # P1-4 高频修正自动沉淀热词
        logger.info(f"[GER] 整轮润色纠错: {text!r} -> {cand!r}")
        return cand
    return text


def _ger_submit(direction: str, uid: int, tid: int, text: str, audio,
                suspect: bool = False, reason: str = "") -> bool:
    """把一条终稿(或门控存疑段)排进复核队列。返回是否已受理；积压超限 → False(调用方自行兜底)。"""
    if not _GER_ON:
        return False
    with _ger_lock:
        if _ger_pending["n"] >= _GER_PEND_MAX:
            ST.ger_stats["skipped"] += 1
            return False
        _ger_pending["n"] += 1
    try:
        _ger_pool.submit(_ger_review, direction, uid, tid, text, audio,
                         suspect, reason, time.time())
        return True
    except Exception:
        with _ger_lock:
            _ger_pending["n"] -= 1
        return False


def _ger_review(direction, uid, tid, text, audio, suspect, reason, t0):
    """复核工作线程体：重转写(第二假设) → 存疑裁决 或 正稿纠错。"""
    try:
        who = "me" if direction == "a" else "other"
        src_lang = _SRC_LANG if direction == "a" else _DST_LANG
        dst_lang = _DST_LANG if direction == "a" else _SRC_LANG
        ST.ger_stats["checked"] += 1
        rec = _review_clip_save(who, tid, text, audio)      # P3 复盘剪辑(音频已在手,顺手留档)
        hyp2, meta2 = "", None
        # 异构第二假设：仅流式模式有意义(分段模式正稿本就出自 Whisper,重转=同引擎无增益)
        if ST.stream_on and audio is not None and getattr(audio, "size", 0) >= int(SR * 0.3):
            try:
                hyp2, meta2 = _stt(audio, src_lang, return_meta=True,
                                   initial_prompt=_asr_hotwords(src_lang))
            except Exception as e:
                logger.warning(f"[GER] 复核重转失败(跳过二次假设): {e}")
        if suspect:
            try:
                outcome, final_txt = _ger_suspect_resolve(direction, who, uid, tid, text,
                                                          audio, hyp2, meta2, reason, t0)
                if rec is not None:
                    rec["state"] = outcome
                    if final_txt:
                        rec["src"] = final_txt
            except Exception:
                logger.exception("[GER] 存疑裁决异常,撤回灰字兜底")
                ST.push_event({"uid": uid, "turn": tid, "who": who, "retract": True})
                if rec is not None:
                    rec["state"] = "vetoed"
            return
        # ── 正稿纠错：只改字幕/留存稿(配音已播出,不动) ──
        if len(_norm_text(text)) < _GER_MIN_CHARS:
            return
        if hyp2 and _norm_text(hyp2) == _norm_text(text):
            return                                       # 双引擎一致 → 无需纠错(零 LLM 开销)
        cand = ""
        trunc = False
        # P1 快路径：发音几乎一致=纯同音字之争 → 直接采信强引擎稿(免 LLM,省 1~3.5s)
        if hyp2 and _pron_sim(hyp2, text) >= _GER_TRUST_SIM:
            cand = hyp2
        # P4 截断恢复：流式稿=turbo 稿的截断残句(轻声典型故障) → 采 turbo 全句(免 LLM)
        if not cand and hyp2 and _GER_TRUNC and _ger_trunc_hit(direction, text, hyp2, meta2):
            cand, trunc = hyp2, True
        if not cand:
            cand = _ger_llm_fix(text, hyp2, src_lang,
                                context=_ger_recent_context(who, tid))   # P2 语境消歧
        if not cand and hyp2 and _pron_sim(hyp2, text) >= 0.75:
            cand = hyp2      # LLM 不可用的降级仲裁：发音高度一致=同音字之争 → 采信强引擎
        cand = _collapse_repeats(cand)       # Whisper 宽窗稿常见复读幻觉,上屏前折叠
        if not cand or _norm_text(cand) == _norm_text(text):
            return
        if _flat_text(cand) == _flat_text(text):
            # 只动了标点=没修字。仲裁规则本就要求"保持标点风格"，此类输出全是模型手滑
            # (实录『…名字?』→『…名字：』)，上屏零收益还引入怪标点——按无修正处理。
            return
        fail = _ger_gate_check(text, cand, src_lang) if not trunc else ""   # 截断恢复走包含度闸
        if fail:
            if fail == "pinyin":
                ST.ger_stats["rejected"] += 1
                logger.info(f"[GER] 纠错稿被拼音闸拒绝[{direction}]: sim={_pron_sim(cand, text):.2f} "
                            f"{text!r} -> {cand!r}")
            return
        # P6 跨引擎去重(修后再验)：纠错稿与近窗找回稿(晋升句)重合 → 本段是流式引擎对同一段
        # 音频的二次吐字,修完只会把屏幕上已有的句子再贴一遍。撤回本段而非替换。
        # (2026-07-08 11:08 实测:晋升句已上屏,14s 后流式残稿被截断恢复修成同一句=连显两遍)
        if _xdedup_hit(direction, cand):
            ST.drops["dedup"] += 1
            logger.info(f"[跨引擎去重] 纠错稿与近窗找回稿重合,撤回本段[{direction}]: "
                        f"{text!r} -> {cand!r}")
            ST.push_event({"uid": uid, "turn": tid, "who": who, "retract": True})
            if rec is not None:
                rec["state"] = "vetoed"
            ST.drop_transcript_turn(who, tid)
            return
        ST.ger_stats["fixed"] += 1
        if rec is not None:
            rec["src"] = cand; rec["state"] = "fixed"    # P3 复盘剪辑同步为屏幕终稿
        if trunc:
            _xdedup_note(direction, cand)   # P6 截断恢复=Whisper宽窗稿,流式随后可能重吐尾段
        else:
            _ger_learn_note(text, cand, src_lang)        # P1-4 高频修正沉淀热词(截断不是错字,不学)
        trans = _translate_nmt(cand, src_lang, dst_lang)
        logger.info(f"[GER] {'截断恢复' if trunc else '字幕纠错'}[{direction}] "
                    f"({time.time()-t0:.1f}s): {text!r} -> {cand!r}")
        if direction == "a":
            ST.push_event({"uid": uid, "turn": tid, "who": who, "src": cand, "zh": cand, "ger": True})
            if not SUBS_MATCH_AUDIO:
                # P0-R4 音画字一致：配音已按旧译文播出,译文字幕不再事后替换(源文纠错照常上屏);
                # 纠错译文仍进转写留存(导出质量不降)。
                ST.push_event({"uid": uid, "turn": tid, "who": who, "dst": trans, "en": trans, "ger": True})
        else:
            ST.push_event({"uid": uid, "turn": tid, "who": who, "src": cand, "en": cand, "ger": True})
            ST.push_event({"uid": uid, "turn": tid, "who": who, "dst": trans, "zh": trans, "ger": True})
        ST.finalize_transcript(who, tid, cand, trans)    # 留存稿同步为纠错稿(导出=屏幕稿)
    except Exception:
        logger.exception("[GER] 复核异常")
    finally:
        with _ger_lock:
            _ger_pending["n"] -= 1


# ── P6 跨引擎防重(2026-07-08 11:08 实测事故)：存疑句晋升用的是 Whisper 对"整段缓冲窗"
# 的重转写,常比流式稿覆盖更多内容；而 Nemotron 随后继续解码同一段音频,又把重叠内容当
# "新句子"定稿(再被截断恢复修成同一句) → 同一段话上屏两次。两引擎互不知情,只能在
# 上屏层去重：登记"宽窗找回稿"(晋升/截断恢复,均为 Whisper 视野),近窗内新稿与其高度
# 重合(互相包含或相似度≥阈值)即判跨引擎重复。只登记找回稿 → 用户故意重复说话(两条都
# 是流式稿)不受影响；纯连刷已有 HALLUC_DEDUP 兜底。
_XDEDUP_ON       = os.environ.get("INTERP_XDEDUP", "1") == "1"
_XDEDUP_WINDOW_S = float(os.environ.get("INTERP_XDEDUP_WINDOW_S", "20"))
_XDEDUP_SIM      = float(os.environ.get("INTERP_XDEDUP_SIM", "0.85"))
_xdedup_recent = {"a": deque(maxlen=4), "b": deque(maxlen=4)}   # (norm_text, ts)
_xdedup_lock = threading.Lock()


def _xdedup_note(direction: str, text: str):
    """登记一条宽窗找回稿(晋升/截断恢复)。"""
    n = _norm_text(text)
    if len(n) >= 5:
        with _xdedup_lock:
            _xdedup_recent[direction].append((n, time.time()))


def _xdedup_hit(direction: str, text: str) -> bool:
    """新稿与近窗找回稿高度重合 → 跨引擎重复。短稿(<5字)不判(碎片误伤大于收益)。"""
    if not _XDEDUP_ON:
        return False
    n = _norm_text(text)
    if len(n) < 5:
        return False
    now = time.time()
    with _xdedup_lock:
        items = list(_xdedup_recent[direction])
    for ref, ts in items:
        if now - ts > _XDEDUP_WINDOW_S:
            continue
        lo, hi = (n, ref) if len(n) <= len(ref) else (ref, n)
        if lo in hi:
            return True
        if difflib.SequenceMatcher(None, n, ref).ratio() >= _XDEDUP_SIM:
            return True
    return False


def _ger_suspect_resolve(direction, who, uid, tid, text, audio, hyp2, meta2, reason, t0):
    """存疑段裁决：强模型重转确认。真话 → 晋升正稿(翻译+补配音)；噪声 → 撤回灰字。
    晋升前补验声纹(方向A)——存疑路径跳过了正常链路的声纹锁,不能让旁人声借复核绕行。"""
    cand = ""
    if hyp2:
        nsp = (meta2 or {}).get("no_speech_prob")
        lp = (meta2 or {}).get("avg_logprob")
        sane = (not _is_hallucination(hyp2)) and (not _lang_sanity_drop(direction, hyp2))
        conf_ok = (nsp is None or nsp < 0.5) and (lp is None or lp > -1.2)
        if sane and conf_ok and len(_norm_text(hyp2)) >= 2:
            cand = hyp2
    if cand and direction == "a" and _voicelock.ready() and audio is not None:
        try:
            ok_spk, sim = _voicelock.check(audio)
            if not ok_spk:
                ST.drops["spk"] += 1; ST.dropped += 1
                logger.info(f"[GER] 存疑段复核=旁人声,撤回[a]: sim={sim:.3f} {cand!r}")
                ST.push_event({"uid": uid, "turn": tid, "who": who, "retract": True})
                ST.ger_stats["vetoed"] += 1
                return "vetoed", ""
        except Exception:
            pass
    if not cand:
        ST.ger_stats["vetoed"] += 1
        key = {"gate": "gate", "halluc": "halluc", "lang": "halluc",
               "filler": "filler", "lowconf": "lowconf"}.get(reason, "gate")
        ST.drops[key] += 1; ST.dropped += 1              # 此刻才计入拦截(复核确认的真拦截)
        logger.info(f"[GER] 存疑段复核=噪声,撤回[{direction}]({reason}): "
                    f"{text!r} / whisper={hyp2!r}")
        ST.push_event({"uid": uid, "turn": tid, "who": who, "retract": True})
        return "vetoed", ""
    cand = _collapse_repeats(cand)     # Whisper 宽窗稿常见复读幻觉,晋升前折叠(防配音复读)
    # P6 跨引擎防重(晋升侧补齐)：存疑段常与相邻正常段共享同一段宽窗音频，若内容与近窗
    # 已找回/正常定稿高度重合 → 晋升会把已经播过的话再配一遍音,造成"同一句英文听两遍"。
    # 之前只有"字幕纠错"分支查了 _xdedup_hit，这里(存疑晋升→会补配音)漏查，此处补上。
    if _xdedup_hit(direction, cand):
        ST.drops["dedup"] += 1
        logger.info(f"[跨引擎去重] 晋升稿与近窗找回稿重合,撤回本段[{direction}]: {cand!r}")
        ST.push_event({"uid": uid, "turn": tid, "who": who, "retract": True})
        return "vetoed", ""
    ST.ger_stats["revived"] += 1
    logger.info(f"[GER] 存疑段复核=真话,晋升[{direction}]({reason}, {time.time()-t0:.1f}s): "
                f"{cand!r} (流式稿:{text!r})")
    _xdedup_note(direction, cand)      # P6 找回稿登记:流式引擎稍后重吐重叠内容时可判重
    try:
        ST.stream_stats[f"fin_{direction}"] += 1       # 救回句同样计入有效定稿(观测口径一致)
    except Exception:
        pass
    pool = ST.pool_a if direction == "a" else ST.pool_b
    fn = _stream_final_a if direction == "a" else _stream_final_b
    try:
        # audio=None + reviewed=True：晋升稿不再回送复核(防循环)；经 pool 串行保配音顺序
        pool.submit(fn, uid, tid, cand, t0, None, True)
    except Exception:
        logger.exception("[GER] 晋升分派失败(会话可能已停止)")
    return "revived", cand


# ── P3 转写复盘：每句音频剪辑落盘 + 人工对错标注 → 真实 CER 趋势(遥测从"代理指标"升级) ──
#   复用 GER 复核已在手的同段 16k 音频(零额外采集/零主链开销)。防磁盘膨胀：每场 ≤ _REVIEW_MAX
#   段、单段 ≤12s、音频目录只留最近 3 场。标注落 data/review_marks.jsonl，/review 页人工闭环；
#   被门控撤回的段也留档(state=vetoed) → 误杀可审计。
_REVIEW_ON    = os.environ.get("INTERP_REVIEW", "1") == "1"
_REVIEW_MAX   = int(os.environ.get("INTERP_REVIEW_MAX", "40"))
_REVIEW_DIR   = os.path.join("logs", "review_audio")
_REVIEW_MARKS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "review_marks.jsonl")
_review_mark_lock = threading.Lock()


def _review_clip_save(who: str, tid, text: str, audio):
    """存一段复盘剪辑(best-effort,失败静默)。返回可变记录 dict(纠错/裁决后可更新)或 None。"""
    if not (_REVIEW_ON and ST.running and getattr(ST, "review_stamp", "")):
        return None
    try:
        if audio is None or getattr(audio, "size", 0) < int(SR * 0.4):
            return None
        with ST.lock:
            if len(ST.review_clips) >= _REVIEW_MAX:
                return None
            rec = {"file": f"seg{len(ST.review_clips) + 1:03d}_{who}.wav", "who": who,
                   "turn": tid, "src": text, "state": "ok",
                   "t": round(max(0.0, time.time() - (ST.session_start or time.time())), 1),
                   "dur_s": round(float(audio.size) / SR, 2)}
            ST.review_clips.append(rec)
            stamp = ST.review_stamp
        d = os.path.join(_REVIEW_DIR, stamp)
        os.makedirs(d, exist_ok=True)
        a = np.asarray(audio, np.float32)[: SR * 12]
        with wave.open(os.path.join(d, rec["file"]), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
            w.writeframes((np.clip(a, -1.0, 1.0) * 32767.0).astype("<i2").tobytes())
        return rec
    except Exception:
        return None


def _review_cleanup():
    """新会话开场清旧存货：音频目录留最近 3 场,剪辑索引留最近 12 份。"""
    import glob as _g
    import shutil
    try:
        for d in sorted(_g.glob(os.path.join(_REVIEW_DIR, "*")), reverse=True)[3:]:
            shutil.rmtree(d, ignore_errors=True)
        for f in sorted(_g.glob(os.path.join("logs", "review_clips_*.json")), reverse=True)[12:]:
            try:
                os.remove(f)
            except Exception:
                pass
    except Exception:
        pass


def _review_dump():
    """会话结束把剪辑索引落盘(无剪辑不落)。"""
    try:
        with ST.lock:
            clips = [dict(c) for c in ST.review_clips]
            stamp = getattr(ST, "review_stamp", "")
        if not clips or not stamp:
            return
        with open(os.path.join("logs", f"review_clips_{stamp}.json"), "w", encoding="utf-8") as f:
            json.dump({"session": stamp, "count": len(clips), "clips": clips},
                      f, ensure_ascii=False, indent=2)
        logger.info(f"复盘剪辑索引已写入({len(clips)} 段): review_clips_{stamp}.json")
    except Exception:
        logger.exception("写复盘剪辑索引失败")


def _char_cer(ref: str, hyp: str) -> float:
    """字符错误率(与 CER 基准工具同口径：字母数字+CJK 归一后 编辑距离/参考长度)。"""
    nm = lambda s: "".join(ch for ch in (s or "") if ch.isalnum() or "\u4e00" <= ch <= "\u9fff").lower()
    r, h = nm(ref), nm(hyp)
    if not r:
        return 0.0
    prev = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        cur = [i]
        for j, hc in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc)))
        prev = cur
    return prev[-1] / len(r)


def _review_marks_index() -> dict:
    """标注台账 → {(session,file): 最新一条标注}(追加式 jsonl,后写覆盖先写)。"""
    out = {}
    try:
        with open(_REVIEW_MARKS, encoding="utf-8") as f:
            for ln in f:
                try:
                    m = json.loads(ln)
                    out[(m.get("session"), m.get("file"))] = m
                except Exception:
                    continue
    except Exception:
        pass
    return out


# ── 术语锁定 / 词表(glossary)：强制品牌名·人名·专有名词·行话译成指定写法，消除 MT 音译不一致/误译 ──
#   痛点：MarianMT/Google 对「美团→Meituan」「数字人→digital human」等常音译错或前后不一致，直播/会议
#   同传尤其致命。做法(占位保护)：译前把源串里的术语换成 MT 难改动的占位符 → MT 翻译 → 译后换回目标写法，
#   保证术语原样落地、不被 MT 打散。安全兜底：若占位符没 survive MT(被吞/打散)→回退「无保护」干净译文
#   (永不劣于无词表，绝不吐残迹)。词表 data/glossary.json 按语向分组，改文件即热加载(mtime 变即重读)；
#   空表=完全等价旧行为(零回归)。匹配：拉丁词按词边界·忽略大小写；中日韩按子串；长词优先(防子串误伤)。
_GLOSSARY_ON   = os.environ.get("INTERP_GLOSSARY", "1") == "1"          # 硬开关(默认开；空表本就无效)
_GLOSSARY_PATH = (os.environ.get("INTERP_GLOSSARY_PATH", "").strip()
                  or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "glossary.json"))
_glossary_lock  = threading.Lock()
_glossary_cache = {"mtime": None, "compiled": {}, "ver": 0}   # ver: 每次重编译自增，作翻译缓存失效信号
_GLOSSARY_PH_FMT = " Z%dQ "  # 占位符：Z<序号>Q + 前后空格。实测 opus-mt(zh<->en)存活率：旧"ZQX%dQZ"58% → 此式100%。
                             # 短&中置数字锚定→MT 近乎原样透传(数字最不被改写)；Z/Q 字母框在英文译文里天然不现→零碰撞
                             # (不会误伤真实数字，纯数字占位如"88088"会)；留空格→防相邻术语被 MT 合并打散(如「数字人换脸」)。


def _glossary_compile(raw: dict) -> dict:
    """{方向:[{src,dst}]} → {方向:[(src,dst,是否拉丁),...]}，长词优先(先替长的防子串误伤)。忽略 _ 开头键。"""
    out = {}
    for key, items in (raw or {}).items():
        if not isinstance(key, str) or key.startswith("_") or not isinstance(items, list):
            continue
        lst = []
        for it in items:
            if not isinstance(it, dict):
                continue
            s = (it.get("src") or "").strip()
            d = (it.get("dst") or "").strip()
            if not s or not d:
                continue
            is_latin = bool(_re.fullmatch(r"[\x00-\x7f]+", s))   # 纯 ASCII → 按词边界匹配
            lst.append((s, d, is_latin))
        if lst:
            lst.sort(key=lambda t: len(t[0]), reverse=True)
            out[key.strip().lower()] = lst
    return out


def _glossary_load(force: bool = False) -> dict:
    """读 data/glossary.json 并按 mtime 热加载(改文件即生效，无需重启)。缺失/坏 → 空表(不报错)。"""
    try:
        mtime = os.stat(_GLOSSARY_PATH).st_mtime
    except OSError:
        mtime = None
    if not force and mtime == _glossary_cache["mtime"]:
        return _glossary_cache["compiled"]
    with _glossary_lock:
        if not force and mtime == _glossary_cache["mtime"]:
            return _glossary_cache["compiled"]
        compiled = {}
        if mtime is not None:
            try:
                with open(_GLOSSARY_PATH, encoding="utf-8") as f:
                    compiled = _glossary_compile(json.load(f))
            except Exception as e:
                logger.warning(f"术语表读取失败({_GLOSSARY_PATH})：{e}；本次用空表")
        _glossary_cache["mtime"] = mtime
        _glossary_cache["compiled"] = compiled
        _glossary_cache["ver"] += 1          # 词表内容变 → ver 跳 → 翻译缓存旧键自然失效
        n = sum(len(v) for v in compiled.values())
        logger.info(f"术语表已加载：{n} 条 / {len(compiled)} 语向 ({_GLOSSARY_PATH})")
    return _glossary_cache["compiled"]


def _glossary_entries(src: str, dest: str):
    """取 src->dest 与通配 '*'(任意语向都保持的品牌名)两组条目。关时/空表 → []。"""
    if not _GLOSSARY_ON:
        return []
    comp = _glossary_load()
    key = f"{(src or '').lower()}->{(dest or '').lower()}"
    return (comp.get(key) or []) + (comp.get("*") or [])


def _glossary_ph_re(i: int):
    """占位符还原正则：容忍 MT 插空格/改大小写/补前导零(Z0Q → 'z 0 q'、'Z00Q' 也能还原)。"""
    return _re.compile(r"Z\s*0*%d\s*Q" % i, _re.IGNORECASE)


def _glossary_pre(text: str, src: str, dest: str):
    """译前保护：术语源写法 → 占位符。返回(受保护文本, [(占位正则, 目标写法)])。"""
    entries = _glossary_entries(src, dest)
    if not entries:
        return text, []
    mapping = []
    for (s, d, is_latin) in entries:
        pat = _re.compile((r"\b%s\b" % _re.escape(s)) if is_latin else _re.escape(s),
                          _re.IGNORECASE if is_latin else 0)
        if not pat.search(text):
            continue
        i = len(mapping)
        text = pat.sub(_GLOSSARY_PH_FMT % i, text)
        mapping.append((_glossary_ph_re(i), d))
    return text, mapping


def _glossary_post(translated: str, mapping):
    """译后还原：占位符 → 目标写法(容忍空格/大小写)。返回(还原文本, 未 survive 的占位数 miss)。"""
    if not mapping:
        return translated, 0
    miss = 0
    for (ph_re, dst) in mapping:
        new, n = ph_re.subn(lambda _m: dst, translated)
        if n:
            translated = new
        else:
            miss += 1
    return translated, miss


import re as _re  # 用于清理大模型可能夹带的思考链

# ── 翻译后端(方案B「翻译脑力拉满」)：本机大模型(LLM，质量最优) → STT服务NMT(opus-mt/NLLB) → Google ──
#   默认 auto：优先本机 ollama 上的大模型做同传翻译(上下文/语气/语序/术语远胜 2020 的 opus-mt)，
#   不可达/失败自动回退原 NMT，再回退 Google，绝不让某句无译文。带轻量熔断：LLM 连续失败后冷却
#   _LLM_COOLDOWN 秒内跳过(不给每句加无谓时延)。术语锁定/缓存/存活自检对本后端同样生效(本函数被
#   _translate_nmt 的词表+缓存包裹)。INTERP_MT_BACKEND=local 即完全等价旧行为(零回归)。
_MT_BACKEND   = os.environ.get("INTERP_MT_BACKEND", "auto").strip().lower()   # auto=LLM优先并兜底 | llm | local
# P5c(2026-07-10) 口语化目标语：即使 backend=local(NMT 提速)，此集合内的目标语仍先走 LLM——
# 实测 qwen2.5:7b 热态 160~190ms、自带自然语气词(ね/よ/な)，NMT 出的书面语在通话里太生硬。
# LLM 失败/被日语健全闸拒 → 原路 NMT 兜底，延迟风险有限(熔断机制共用)。
_MT_LLM_LANGS = set(x.strip() for x in (os.environ.get("INTERP_MT_LLM_LANGS", "ja")
                    or "").replace("，", ",").lower().split(",") if x.strip())
_LLM_URL      = (os.environ.get("INTERP_LLM_URL") or "http://127.0.0.1:11434").rstrip("/")
_LLM_MODEL    = os.environ.get("INTERP_LLM_MODEL", "qwen2.5:32b").strip()      # 已本地；qwen3:32b 拉取完可切
_LLM_TIMEOUT  = float(os.environ.get("INTERP_LLM_TIMEOUT", "20"))
_LLM_TEMP     = float(os.environ.get("INTERP_LLM_TEMP", "0.2"))
# 常驻时长：直播中每句翻译都会刷新计时(不会播中冷卸)；停播 90 分钟后自动让出 ~9G 显存
# 给录播增强/其他任务(原 8h 会让 LLM 在深夜闲置时白占卡)。要回旧行为设 INTERP_LLM_KEEPALIVE=8h。
_LLM_KEEP     = os.environ.get("INTERP_LLM_KEEPALIVE", "90m")
_LLM_COOLDOWN = float(os.environ.get("INTERP_LLM_COOLDOWN", "30"))
_LLM_NUMPRED  = int(os.environ.get("INTERP_LLM_NUM_PREDICT", "512"))
# 关键：翻译每句上下文仅~200 token，绝不需要默认的 32768 ctx。把 num_ctx 压到 2048 可让 32B 的 KV 缓存
# 从 ~11GB 降到 ~0.6GB（模型体积 31GB→20GB），显著减少 CPU 分层、把每句时延从 5~7s 拉回 ~2s。
_LLM_NUMCTX   = int(os.environ.get("INTERP_LLM_NUM_CTX", "2048"))
_llm_fail_until = 0.0
_LANG_NAMES = {"zh": "Chinese", "en": "English", "ja": "Japanese", "ko": "Korean",
               "fr": "French", "de": "German", "es": "Spanish", "ru": "Russian",
               "pt": "Portuguese", "it": "Italian", "vi": "Vietnamese", "th": "Thai",
               "id": "Indonesian", "ar": "Arabic", "yue": "Cantonese", "zh-cn": "Chinese"}
_THINK_RE = _re.compile(r"<think>.*?</think>", _re.S)


def _llm_ready() -> bool:
    """当前是否应尝试 LLM 翻译(后端启用且不在熔断冷却期)。"""
    return _MT_BACKEND in ("auto", "llm") and time.time() >= _llm_fail_until


# P1-3 MT 专用模型适配：腾讯 Hy-MT2/混元-MT 是「翻译特化」模型,须用官方固定指令
# (中文语向用中文指令),不吃通用同传 system prompt;采样参数也按官方推荐(t=0.7/top_k20/top_p0.6/rp1.05)。
# 判定按模型名,与通用 chat 模型(qwen 等)零冲突——不切模型时行为逐字节不变。
_LANG_NAMES_ZH = {"zh": "中文", "en": "英语", "ja": "日语", "ko": "韩语", "fr": "法语",
                  "de": "德语", "es": "西班牙语", "ru": "俄语", "pt": "葡萄牙语", "it": "意大利语",
                  "vi": "越南语", "th": "泰语", "id": "印尼语", "ar": "阿拉伯语",
                  "yue": "粤语", "zh-cn": "中文"}


def _is_mt_model(model: str) -> bool:
    m = (model or "").lower()
    return "hy-mt" in m or "hunyuan-mt" in m


# ── P1-L Language Pack：按目标语热插拔的文化/风格包 ──────────────────────────
# data/langpacks/<语>.json，字段全部可选：
#   style      : str  地域口语风格描述(英文写,直接进 system prompt,如美式直播腔/英式克制)
#   tone_words : list 常用语气词/口头表达(提示模型自然使用,不强塞)
#   taboo      : list 禁忌词(要求译文绝不出现)
#   terms      : dict 领域说法偏好 {"性价比":"bang for the buck"}(轻量版术语,重型仍走 glossary)
# mtime 变更即热加载(与术语表同机制),无文件=零注入零行为变化。区域码回退主语言(en-US→en)。
_LANGPACK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "langpacks")
_langpack_cache = {}    # lang -> {"mtime": float, "prompt": str}
_langpack_lock = threading.Lock()


def _langpack_prompt(dest: str) -> str:
    """目标语的 Language Pack → system prompt 追加段；无包/关闭返回空串。"""
    if not LANGPACK_ON:
        return ""
    d = (dest or "").lower()
    for lang in (d, d.split("-")[0]):
        path = os.path.join(_LANGPACK_DIR, f"{lang}.json")
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        with _langpack_lock:
            ent = _langpack_cache.get(lang)
            if ent and ent["mtime"] == mtime:
                return ent["prompt"]
        try:
            with open(path, encoding="utf-8") as f:
                pk = json.load(f)
            parts = []
            style = str(pk.get("style", "")).strip()
            if style:
                parts.append(f" Regional style guide: {style}")
            tw = [str(w).strip() for w in (pk.get("tone_words") or []) if str(w).strip()]
            if tw:
                parts.append(" Where it sounds natural, you may use local expressions such as: "
                             + ", ".join(tw[:12]) + ". Never force them into every sentence.")
            tb = [str(w).strip() for w in (pk.get("taboo") or []) if str(w).strip()]
            if tb:
                parts.append(" NEVER use these words or phrases: " + ", ".join(tb[:20]) + ".")
            terms = pk.get("terms") or {}
            if isinstance(terms, dict) and terms:
                kv = "; ".join(f"{k} -> {v}" for k, v in list(terms.items())[:15])
                parts.append(f" Preferred renderings: {kv}.")
            prompt = "".join(parts)[:900]      # 封顶:再长挤占 num_ctx 且拖慢首 token
        except Exception as e:
            logger.warning(f"LanguagePack 读取失败({path})：{e}；本次忽略")
            prompt = ""
        with _langpack_lock:
            _langpack_cache[lang] = {"mtime": mtime, "prompt": prompt}
        if prompt:
            logger.info(f"LanguagePack 已加载：{lang} ({len(prompt)} 字符)")
        return prompt
    return ""


def _llm_req_body(model: str, text: str, src: str, dest: str, ctx: list = None) -> dict:
    """按模型族拼 /api/chat 请求体。ctx=[(源文,译文),...] 滚动语境(P0-R2)——
    以 few-shot user/assistant 消息对注入(比塞进 system 文本更贴 chat 模型的注意力习惯,
    且模型天然只输出"最后一句"的译文,不会把语境句复述出来)。MT 特化模型(Hy-MT)吃固定
    指令模板,不注语境(官方模板外的内容会劣化其输出)。"""
    if _is_mt_model(model):
        if src.startswith("zh") or dest.startswith("zh") or src == "yue" or dest == "yue":
            tgt = _LANG_NAMES_ZH.get(dest, _LANG_NAMES.get(dest, dest))
            prompt = f"把下面的文本翻译成{tgt}，不要额外解释。\n\n{text}"
        else:
            tgt = _LANG_NAMES.get(dest, dest)
            prompt = f"Translate the following segment into {tgt}, without additional explanation.\n\n{text}"
        return {"model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False, "keep_alive": _LLM_KEEP,
                "options": {"temperature": 0.7, "top_k": 20, "top_p": 0.6,
                            "repeat_penalty": 1.05,
                            "num_predict": _LLM_NUMPRED, "num_ctx": _LLM_NUMCTX}}
    sl = _LANG_NAMES.get(src, src)
    dl = _LANG_NAMES.get(dest, dest)
    sys_prompt = (
        f"You are a professional real-time interpreter. Translate the user's {sl} text into {dl}. "
        f"Output ONLY the {dl} translation on a single line: no quotes, no pinyin, no explanations, no notes. "
        f"Preserve the meaning, tone and named entities, and produce natural spoken-style {dl}. "
        f"Keep any placeholder tokens shaped like Z1Q or Z2Q exactly unchanged."
    )
    if ctx:
        # P0-R2 语境指令：告知前文仅供指代/术语一致,只译最后一句(few-shot 对在下方注入)
        sys_prompt += (
            " Earlier sentences from this conversation are provided as context; use them to resolve "
            "pronouns and keep terminology consistent, but translate ONLY the latest user message."
        )
    if LLM_EMO_TAG:
        # P0-R2b 情绪标签：同一次调用顺带产出语义级情绪(平叙句不加,解析失败自动回退关键词规则)
        sys_prompt += (
            " If (and only if) the sentence carries a strong emotion, append exactly one tag at the very "
            "end of the line from this set: [emo:excited] [emo:angry] [emo:sad] [emo:surprised] "
            "[emo:happy]. Neutral sentences get no tag."
        )
    if PROSODY_PUNCT:
        # P2-D2 韵律标点：TTS 停顿/节奏天然跟随标点——让 LLM 按"朗读节奏"断句,
        # 零解析零延迟实现停顿规划(不用句中控制标记,7b 解析不可靠)。
        sys_prompt += (
            " Punctuate for natural SPEECH rhythm, since the translation will be read aloud: put a comma "
            "where a speaker would take a quick breath, and prefer splitting one long sentence into two "
            "short spoken sentences. If the source clearly hesitates, one ellipsis (…) may mark the pause. "
            "Do not overuse commas or ellipses."
        )
    if (dest or "").lower().startswith("ja"):
        # P5c 关东口语化：贴情绪、适度语气词。禁止罗马字/中文残留(7b 偶发混稿,配 _llm_out_sane 双保险)。
        # P7 加强：随意语境偏关东日常口语(じゃん/だよね/かも/マジで)；情绪句要"演出来"不许平。
        sys_prompt += (
            " For Japanese: use natural colloquial Tokyo-standard Japanese (東京の標準的な話し言葉). "
            "In casual contexts prefer Kanto everyday speech patterns such as 〜じゃん, 〜だよね, "
            "〜かも, マジで. Match the speaker's emotion vividly: if the source sounds excited, angry, "
            "sad or surprised, make the Japanese emotionally expressive with interjections and emphatic "
            "final particles (よ！/ね！/の！？) — never render an emotional sentence flatly. Use particles "
            "sparingly in neutral sentences. Never output romaji, Chinese characters-only sentences, or "
            "English words unless they are proper nouns."
        )
    sys_prompt += _langpack_prompt(dest)       # P1-L 文化包(无文件=空串,零开销)
    msgs = [{"role": "system", "content": sys_prompt}]
    for (c_src, c_dst) in (ctx or []):
        msgs.append({"role": "user", "content": c_src})
        msgs.append({"role": "assistant", "content": c_dst})
    msgs.append({"role": "user", "content": text})
    return {"model": model,
            "messages": msgs,
            "stream": False,
            "think": False,                        # 关掉 Qwen3 思考链(否则污染译文+爆时延)；旧版 ollama 会忽略此字段
            "keep_alive": _LLM_KEEP,
            "options": {"temperature": _LLM_TEMP, "top_p": 0.8,
                        "num_predict": _LLM_NUMPRED, "num_ctx": _LLM_NUMCTX}}


_MT_CJK_DESTS = {"zh", "ja", "ko"}     # 译文里"凭空 ASCII 词"检查只对这些目标语有意义


def _llm_out_sane(out: str, dest: str, src_text: str = None) -> bool:
    """LLM 译文健全性：①目标语可用字符集校验时，译文须含该字符集(实测 7b 冷态首句吐过
    'great! 我们成功了呢！' 这类中英混稿)。日语按假名判(纯汉字句极罕见,宁可误退 NMT)。
    ②P0h(2026-07-10 实录 'あ、これ何 helfulnoの！？')：CJK 目标语译文里冒出源文没有的
    ASCII 词/替换符=模型手滑——字幕难看，克隆音更会把乱码念出来。术语热词(如 LingoX)与
    常用缩写(OK/AI)放行；全角数字/字母不查(日文排版合法用它们写 '３０分')。拒绝即退 NMT。"""
    d = (dest or "").lower()
    base = d.split("-")[0]
    if d.startswith("ja"):
        if not any("\u3040" <= ch <= "\u30ff" for ch in out):
            return False
    else:
        chk = _SCRIPT_CHECKS.get(base)
        if chk is not None and not any(chk(ch) for ch in out if ch.isalnum()):
            return False
    if src_text is not None and base in _MT_CJK_DESTS:
        if "\ufffd" in out:
            return False
        new_words = ({w.lower() for w in _ASCII_WORD_RE.findall(out)}
                     - {w.lower() for w in _ASCII_WORD_RE.findall(src_text)}
                     - {"ok", "ai"})
        if new_words:
            try:
                hw = (_asr_hotwords(base) or "").lower()
            except Exception:
                hw = ""
            if any(w not in hw for w in new_words):
                return False
    return True


# ── P0-R2 翻译滚动语境：最近 N 句(源文,译文)按语向留存,注入 LLM 翻译的 few-shot 消息对 ──
#   收益：跨句指代消解(「它支持快充」的「它」=上句的商品)、术语/称谓跨句一致、
#   语义块分块翻译(P0-R1)的块间连贯。只喂 LLM 译路(NMT/Google 无上下文接口,行为不变)。
#   上下文按 (src,dest) 语向分桶——方向A/B、chunk/final 各自命中自己的语境,互不污染。
_mt_ctx_lock = threading.Lock()
_mt_ctx = {}                     # (src,dest) -> deque[(src_text, dst_text, ts)]
# 指代词粗判(zh/en/ja/ko)：句含指代 → 翻译缓存键附语境签名(同句不同语境不共享缓存)。
_CTX_ANAPHORA_RE = _re.compile(
    r"它|他们|她们|它们|这个|那个|这款|那款|这些|那些|这东西|那东西|其中|该产品"
    r"|それ|これ|あれ|こちら|そちら|彼女|彼ら"
    r"|그것|이것|저것"
    r"|\b(?:it|its|they|them|their|this|that|these|those|he|she|him|her|one)\b", _re.I)


def _mt_ctx_note(src: str, dest: str, src_text: str, dst_text: str):
    """登记一对已完成的(源文,译文)供后续句子当语境。best-effort,关闭时空操作。"""
    if not MT_CTX_ON or not (src_text or "").strip() or not (dst_text or "").strip():
        return
    with _mt_ctx_lock:
        dq = _mt_ctx.get((src, dest))
        if dq is None:
            dq = _mt_ctx[(src, dest)] = deque(maxlen=MT_CTX_N)
        dq.append(((src_text or "").strip(), (dst_text or "").strip(), time.time()))


def _mt_ctx_get(src: str, dest: str) -> list:
    """取该语向的有效语境对 [(src_text,dst_text),...](过期不取)。"""
    if not MT_CTX_ON:
        return []
    now = time.time()
    with _mt_ctx_lock:
        dq = _mt_ctx.get((src, dest)) or ()
        return [(s, d) for (s, d, ts) in dq if now - ts <= MT_CTX_WINDOW_S]


def _mt_ctx_clear():
    with _mt_ctx_lock:
        _mt_ctx.clear()


# ── P0-R2b LLM 情绪标签直通：翻译 prompt 请求在译文尾追加 [emo:xxx](强情绪句才加)。
#   _translate_llm 解析后剥离并寄存于"最近情绪槽"(按目标语校验+8s TTL)；
#   _emo_for_sentence 优先消费——语义级情绪判断,替代纯关键词规则,且零额外 LLM 调用。
_llm_emo_slot = {"emo": "", "dst": "", "ts": 0.0}
_llm_emo_lock = threading.Lock()
_LLM_EMO_RE = _re.compile(r"[\[\(【（]\s*emo\s*[:：]\s*([A-Za-z]+)\s*[\]\)】）]\s*$", _re.I)


def _llm_emo_note(emo: str, dst_lang: str):
    with _llm_emo_lock:
        _llm_emo_slot.update({"emo": emo, "dst": (dst_lang or "").lower(), "ts": time.time()})


def _llm_emo_take(dst_lang: str) -> str:
    """取走最近 LLM 情绪标签(仅目标语匹配且 8s 内有效;取走即清,防跨句串染)。"""
    if not LLM_EMO_TAG:
        return ""
    with _llm_emo_lock:
        emo, dst, ts = _llm_emo_slot["emo"], _llm_emo_slot["dst"], _llm_emo_slot["ts"]
        _llm_emo_slot.update({"emo": "", "dst": "", "ts": 0.0})
    if emo and dst == (dst_lang or "").lower() and time.time() - ts <= 8.0:
        return emo if emo in _EMO_LABELS else ""
    return ""


def _translate_llm(text: str, src: str, dest: str, ctx: list = None) -> str:
    """本机 ollama 大模型翻译(同传口吻，只出译文)。不可达/失败→返回 "" 交上层兜底并触发冷却。
    P5c：backend=local(NMT 提速)时，_MT_LLM_LANGS 内的目标语(默认 ja)仍先走 LLM——
    NMT 书面语在通话里生硬，7b 热态实测 160~190ms 且自带自然语气词。
    ctx=[(src_text,dst_text),...] 滚动语境(P0-R2)：注入 few-shot 消息对,指代/术语跨句一致。"""
    global _llm_fail_until
    dest_l = (dest or "").lower()
    allow = _llm_ready() or (dest_l.split("-")[0] in _MT_LLM_LANGS and time.time() >= _llm_fail_until)
    if not allow:
        return ""
    try:
        body = _llm_req_body(_LLM_MODEL, text, src, dest, ctx=ctx)
        r = _HTTP_POOL.post(f"{_LLM_URL}/api/chat", json=body, timeout=_LLM_TIMEOUT)
        r.raise_for_status()
        out = ((r.json().get("message") or {}).get("content") or "")
        out = _THINK_RE.sub("", out)               # 防御式清除 <think>…</think>
        out = " ".join(ln.strip() for ln in out.splitlines() if ln.strip())  # 多行折成一行
        out = out.strip().strip('"').strip("“”").strip()
        # P0-R2b 先剥情绪尾标签(必须在健全闸之前——CJK 目标语里 '[emo:excited]' 的 ASCII
        # 词会被 _llm_out_sane 判成凭空英文而误拒整句译文)。
        m = _LLM_EMO_RE.search(out)
        if m:
            out = out[:m.start()].rstrip()
            _llm_emo_note(m.group(1).lower(), dest)
        if out and not _llm_out_sane(out, dest, src_text=text):
            ST.mt_stats["llm_reject"] = ST.mt_stats.get("llm_reject", 0) + 1
            logger.info(f"[LLM译] 目标语健全闸拒绝({src}->{dest})，回退 NMT: {out[:50]!r}")
            return ""
        if out:
            return out
    except Exception as e:
        _llm_fail_until = time.time() + _LLM_COOLDOWN
        logger.warning(f"LLM 翻译失败({src}->{dest})，冷却 {_LLM_COOLDOWN:.0f}s；本句回退 NMT: {e}")
    return ""


# ── P1-S 流式 LLM 翻译(边译边配) ────────────────────────────────────────────
# 非分块句的最后一段串行等待是"整段译文生成"(7b 热态 0.2~0.8s,长句更久)。ollama
# stream=True 把它拆掉：译文 token 到达子句边界(LLM_STREAM_MIN)立即送 TTS,首段音频
# 在整句译完前就开播。安全设计(宁可退回也不出错声):
#   ① 只在「未出声」时允许放弃(返回 None→调用方走旧非流式路径,零重播风险);
#   ② 每段出声前过 _llm_out_sane 目标语健全闸——首段被拒=整句放弃流式;
#      后段被拒=丢弃余下,已出声部分作为终稿(音字一致优先);
#   ③ 强情绪句/术语命中句/缓存命中句不走流式(保情感改道·术语锁定·TM 命中收益);
#   ④ 段间由 pool_a 串行线程直接顺序 _enqueue_synth → 天然保序。


def _llm_stream_disp(parts: list) -> str:
    """已出声段 → 展示译文(字幕/语境/缓存用)。"""
    return _join_dst([p for p in parts if p]) if parts else ""


def _translate_dub_stream(text: str, src: str, dest: str):
    """流式 LLM 翻译+边译边配(仅通话配音链路调用)。成功返回完整展示译文(音频已入队/
    _note_self_output 已逐段登记)；返回 None=本句不适用或未出声即失败(调用方走旧路径)。"""
    global _llm_fail_until
    # LLM 可用性与 _translate_llm 同判:auto/llm 后端,或 local 后端但目标语在口语化集合(P5c ja 等)
    allow = _llm_ready() or ((dest or "").split("-")[0].lower() in _MT_LLM_LANGS
                             and time.time() >= _llm_fail_until)
    if not (LLM_STREAM_ON and allow and ST.play_q is not None):
        return None
    text = (text or "").strip()
    if not text or src == dest:
        return None
    # 强情绪句不走流式：情感引擎按整句改道(CosyVoice/SBV2 上色),流式逐段出声会失去情感音。
    try:
        if EMO_TTS_ON and _emo_detect_text:
            if _ja_use_sbv2():
                return None                        # SBV2 语向逐句都上 style,整句保留表现力
            rule = _emo_detect_text(text)
            if rule in _EMO_STRONG or _emo_mood() in _EMO_STRONG:
                return None
    except Exception:
        pass
    global _last_xlate_mono
    _last_xlate_mono = time.monotonic()
    ctx = _mt_ctx_get(src, dest)
    # 缓存命中→不走流式(调用方 _translate_nmt 会秒回);术语命中→不走(占位符还原需整段校验)
    key = None
    if _TR_CACHE_ON:
        _glossary_load()
        ctx_sig = hash(tuple(ctx)) if (ctx and _CTX_ANAPHORA_RE.search(text)) else 0
        key = (src, dest, _GLOSSARY_ON, _glossary_cache["ver"], ctx_sig, text)
        with _tr_cache_lock:
            if _tr_cache.get(key) is not None:
                return None
    try:
        if _glossary_pre(text, src, dest)[1]:
            return None
    except Exception:
        return None
    body = _llm_req_body(_LLM_MODEL, text, src, dest, ctx=ctx)
    body["stream"] = True
    spoken, buf, done_ok, clean = [], "", False, True
    r = None
    try:
        r = _HTTP_POOL.post(f"{_LLM_URL}/api/chat", json=body, stream=True, timeout=_LLM_TIMEOUT)
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            try:
                j = json.loads(line)
            except Exception:
                continue
            delta = ((j.get("message") or {}).get("content") or "")
            if delta:
                buf += delta.replace("\n", " ")
            if "<think" in buf:                    # 思考链混入(旧版 ollama 忽略 think:False)→ 放弃流式
                if not spoken:
                    return None
                break
            if j.get("done"):
                done_ok = True
                break
            while True:
                cut = _chunk_cut(buf, min_len=LLM_STREAM_MIN, force_len=0)
                if not cut:
                    break
                seg, buf = buf[:cut], buf[cut:]
                if not _llm_stream_speak(spoken, seg, dest, text):
                    if not spoken:
                        return None                # 首段即不健全:未出声,整句安全退回
                    buf, done_ok, clean = "", True, False   # 已出声:丢余下,已播部分为终稿
                    break
            if done_ok and not buf:
                break
        # 尾段:剥情绪标签(在健全闸前,防 ASCII 标签误伤 CJK 校验)后出声
        if clean and (done_ok or buf):
            m = _LLM_EMO_RE.search(buf.rstrip())
            if m:
                tag = m.group(1).lower()
                buf = buf.rstrip()[:m.start()].rstrip()
                if tag in _EMO_STRONG:
                    _emo_set_mood(tag, "llm-tag")  # 只刷基调不入本句(本句已按平叙出声)
            if buf.strip():
                _llm_stream_speak(spoken, buf, dest, text)
        if not spoken:
            return None
        ST.llms_stats["used"] += 1
        ST.llms_stats["segs"] += len(spoken)
        out = _llm_stream_disp(spoken)
        if key is not None and done_ok and clean and out and out != text:
            with _tr_cache_lock:                   # 完整+健全才进 TM(半句稿固化会污染缓存)
                _tr_cache[key] = out
                _tr_cache.move_to_end(key)
                while len(_tr_cache) > _TR_CACHE_MAX:
                    _tr_cache.popitem(last=False)
        return out
    except Exception as e:
        if not spoken:                             # 未出声:交回旧路径(其 LLM 重试自带熔断)
            logger.info(f"[流译] 失败未出声,退回非流式({src}->{dest}): {e}")
            return None
        _llm_fail_until = time.time() + _LLM_COOLDOWN
        logger.warning(f"[流译] 中途失败,已播 {len(spoken)} 段为终稿({src}->{dest}): {e}")
        return _llm_stream_disp(spoken)
    finally:
        try:
            if r is not None:
                r.close()
        except Exception:
            pass


def _llm_stream_speak(spoken: list, seg: str, dest: str, src_text: str) -> bool:
    """流式段出声：清洗→健全闸→登记→配音入队。通过返回 True 并记入 spoken。"""
    s = _THINK_RE.sub("", seg).strip().strip('"').strip("“”").strip()
    if not spoken:
        s = s.lstrip('"“” ').strip()
    if not s or not _flat_text(s):
        return True                                # 纯标点/空段:跳过不出声,不视为失败
    if not _llm_out_sane(s, dest, src_text=src_text):
        ST.mt_stats["llm_reject"] = ST.mt_stats.get("llm_reject", 0) + 1
        ST.llms_stats["bail"] += 1
        logger.info(f"[流译] 段健全闸拒绝({dest}): {s[:40]!r}")
        return False
    try:
        if not spoken:
            _turn_hold()                       # P3-T 对方刚开口→句首礼让
            _sent_gap()                        # P2-D1 本句首段:积压时垫呼吸间隙
        _note_self_output(s)
        _enqueue_synth(s)
        spoken.append(s)
        return True
    except Exception:
        logger.exception("[流译] 段配音失败,余下退回")
        return False


def _translate_raw(text: str, src: str, dest: str, ctx: list = None) -> str:
    """翻译原语：本机大模型(最优) → STT服务NMT(opus-mt/NLLB) → Google，逐级兜底(永远有译文)。
    ctx=滚动语境(P0-R2)只喂 LLM 层；NMT/Google 无上下文接口,兜底行为不变。"""
    text = (text or "").strip()
    if not text:
        return text
    # 1) 本机大模型(方案B 主力；后端=local 或熔断期内则内部直接返回 "" 跳过)
    out = _translate_llm(text, src, dest, ctx=ctx)
    if out:
        return out
    # 2) STT 服务内置 NMT(MarianMT/opus-mt、NLLB；~70ms，离线)
    try:
        r = _stt_post("/translate", {"text": text, "src": src, "dest": dest}, timeout=15)  # S8-3 容灾
        r.raise_for_status()
        out = (r.json().get("text") or "").strip()
        if out:
            return out
    except Exception as e:
        logger.warning(f"本地NMT翻译失败({src}->{dest})，回退 Google: {e}")
    # 3) Google 兜底
    return _translate_google(text, src, dest)


def _llm_warmup_kick():
    """启动即后台预热本机大模型：发一次极短翻译，把权重提前载入显存，消除首句 ~6s 冷启动。
    LLM 后端(auto/llm)、或 backend=local 但目标语在口语化 LLM 集合(P5c,如 ja)时执行；
    best-effort、不阻塞、失败静默(有熔断+NMT/Google 兜底)。"""
    if _MT_BACKEND not in ("auto", "llm") and \
            (_DST_LANG or "").split("-")[0].lower() not in _MT_LLM_LANGS:
        return
    def _job():
        try:
            time.sleep(1.5)                       # 等 uvicorn/端口就绪再预热，不抢启动
            t0 = time.time()
            if _translate_llm("你好", _SRC_LANG, _DST_LANG):
                logger.info(f"LLM 预热完成（{_LLM_MODEL}，{time.time()-t0:.1f}s）：已常驻显存，首句不再冷启动")
        except Exception:
            pass
    threading.Thread(target=_job, name="llm-warmup", daemon=True).start()


# ── P4-1 语向预热：切语向/开会话即后台预热该语对的「全部翻译层」──────────────
#   动机：远端 NMT 兜底(Marian/NLLB)按语对懒加载——实测 ja→zh 首调 73s、热态 0.2s。
#   LLM 是主力时兜底平时碰不到，可一旦 LLM 超时/熔断，通话中段就会撞上 73s 冷加载=事故。
#   预热两层：①LLM(该语对一次短译,顺带填提示模板路径)；②远端 STT /translate 双向各一句
#   (触发懒加载,在远端节点耗时,本机零成本)。同语对进行中不重复预热；结果留观测。
_LANG_WARM_ON = os.environ.get("INTERP_LANG_WARM", "1") == "1"
_LANG_WARM_SAMPLE = {"zh": "你好", "ja": "こんにちは", "ko": "안녕하세요", "ru": "Привет",
                     "ar": "مرحبا", "th": "สวัสดี", "vi": "Xin chào"}   # 其余语种用 hello
_lang_warm_lock = threading.Lock()
_lang_warm_state = {"pair": None, "running": False, "done": [], "at": 0.0}   # done=[{层,方向,ms,ok}]


def _lang_warm_run(src: str, dst: str, remote_only: bool):
    res = []
    for (frm, to) in ((src, dst), (dst, src)):
        sample = _LANG_WARM_SAMPLE.get(frm, "hello")
        if not remote_only:
            t0 = time.time()
            ok = bool(_translate_llm(sample, frm, to))
            res.append({"layer": "llm", "dir": f"{frm}->{to}", "ms": int((time.time()-t0)*1000), "ok": ok})
        t0 = time.time()
        ok = False
        try:  # 直调远端 /translate(绕过 LLM 优先的 _translate_raw)，专门把兜底层的懒加载提前付掉
            r = requests.post(f"{STT_URL}/translate",
                              json={"text": sample, "src": frm, "dest": to}, timeout=300)
            ok = bool(r.ok and (r.json().get("text") or "").strip())
        except Exception:
            pass
        res.append({"layer": "nmt", "dir": f"{frm}->{to}", "ms": int((time.time()-t0)*1000), "ok": ok})
    return res


# ── P6-5 预载自学习：通译端统计会话日志的真实语对分布,启动时推给 .140 预跑(客户端驱动) ──
_PRELOAD_PUSH_ON = os.environ.get("INTERP_PRELOAD_PUSH", "1") == "1"


def _top_lang_pairs(limit: int = 3):
    """近 40 场会话日志按语对计频(双向计入),返回 top 有向对 ["ja:zh",...]。当前语向必入选。"""
    import glob
    cnt = {}
    try:
        for fp in sorted(glob.glob(os.path.join("logs", "interp_session_*.json")), reverse=True)[:40]:
            try:
                lg = json.load(open(fp, encoding="utf-8")).get("langs") or []
                if len(lg) == 2 and lg[0] and lg[1] and lg[0] != lg[1]:
                    k = (lg[0], lg[1])
                    cnt[k] = cnt.get(k, 0) + 1
            except Exception:
                continue
    except Exception:
        pass
    top = sorted(cnt.items(), key=lambda kv: -kv[1])[:limit]
    pairs = []
    for (s, d), _n in top:
        for p in (f"{s}:{d}", f"{d}:{s}"):
            if p not in pairs:
                pairs.append(p)
    for p in (f"{_SRC_LANG}:{_DST_LANG}", f"{_DST_LANG}:{_SRC_LANG}"):
        if p not in pairs:
            pairs.append(p)
    return pairs


def _preload_push_kick(delay: float = 20.0):
    """启动后延时把 top 语对推给远端翻译服务 /translate/preload(旧版远端无此端点=静默跳过)。"""
    if not _PRELOAD_PUSH_ON:
        return False

    def _job():
        try:
            time.sleep(max(0.0, delay))
            pairs = _top_lang_pairs()
            if not pairs:
                return
            r = requests.post(f"{STT_URL}/translate/preload", json={"pairs": pairs}, timeout=8)
            if r.ok and r.json().get("ok"):
                logger.info(f"预载自学习已推送 .140: {','.join(pairs)}")
        except Exception:
            pass
    threading.Thread(target=_job, name="preload-push", daemon=True).start()
    return True


def _lang_warm_kick(src: str, dst: str, remote_only: bool = False, reason: str = ""):
    """后台预热语对(LLM+远端NMT 双向)。同语对已在预热=跳过；best-effort 不阻塞。"""
    if not _LANG_WARM_ON or not src or not dst or src == dst:
        return False
    with _lang_warm_lock:
        if _lang_warm_state["running"] and _lang_warm_state["pair"] == (src, dst):
            return False
        _lang_warm_state.update({"pair": (src, dst), "running": True})
    def _job():
        try:
            t0 = time.time()
            res = _lang_warm_run(src, dst, remote_only)
            brief = " ".join(f"{r['layer']}[{r['dir']}]{'OK' if r['ok'] else 'X'}{r['ms']}ms" for r in res)
            logger.info(f"语向预热完成 {src}⇄{dst}({reason or 'switch'}, {time.time()-t0:.1f}s): {brief}")
            with _lang_warm_lock:
                _lang_warm_state.update({"done": res, "at": time.time()})
        except Exception:
            logger.exception("语向预热线程异常")
        finally:
            with _lang_warm_lock:
                _lang_warm_state["running"] = False
    threading.Thread(target=_job, name="lang-warm", daemon=True).start()
    return True


# ── 翻译缓存 / 翻译记忆(TM)：相同(语向+文本)直接命中，降时延 + 保一致(同句永远同译) + 减 MT 负载 ──
#   键含术语表签名(on + ver)：词表一改 ver 跳→旧键自然失效(不会用旧术语的陈缓存)。只缓存"确实翻译了"
#   的结果(非空且 != 输入)，避免把 MT 全挂时的原样回显固化成永久错译。LRU 上限。关(=0)→纯直译零回归。
_TR_CACHE_ON   = os.environ.get("INTERP_TRANSLATE_CACHE", "1") == "1"
_TR_CACHE_MAX  = max(1, int(os.environ.get("INTERP_TRANSLATE_CACHE_SIZE", "512")))
_tr_cache      = OrderedDict()                 # (src,dest,gloss_on,gloss_ver,text) -> out ；LRU
_tr_cache_lock = threading.Lock()
_tr_cache_stat = {"hit": 0, "miss": 0}         # 累计(跨会话)命中/未命中；per-session 靠 base 差分
# 跨会话统计留存：会话结束把本场命中率/预热战果追加进趋势(logs/interp_tm_stats.json)，并累计"高频未命中
# 短句"(真正付了 MT 时延、被 LRU 逐出或首现的句)。可选反哺预热(默认关，避免与转写自学重复)。关=零开销。
_TM_STATS_ON          = os.environ.get("INTERP_TM_STATS", "1") == "1"
_TM_STATS_MAX         = max(5, int(os.environ.get("INTERP_TM_STATS_MAX", "60")))            # 趋势保留最近 N 场
_TM_STATS_MISS_TOP    = max(0, int(os.environ.get("INTERP_TM_STATS_MISS_TOP", "200")))      # 累计未命中句 top-N
_TM_STATS_MISS_MAXLEN = max(1, int(os.environ.get("INTERP_TM_STATS_MISS_MAXLEN", "24")))    # 只追踪短句(可预热)
_TM_WARMUP_FROM_STATS = os.environ.get("INTERP_TM_WARMUP_FROM_STATS", "0") == "1"           # 反哺预热(opt-in)
_TM_STATS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "interp_tm_stats.json")
_TR_MISS_CAP   = 4000                          # 内存未命中键上限(超出仅已存键累加，防长会话膨胀)
_tr_miss = {}                                  # 本会话未命中短句计数 (src,dest,text)->n ；会话结束并入落盘
_tm_session_base = {"hit": 0, "miss": 0}       # 会话起点的累计命中/未命中(用于算本场差分命中率)

# ── 术语占位「真·存活率」在线自检：每次带词表的翻译累计(占位总数, survive)按语向分桶。占位符经不同 MT
#   后端/语向的 survive 率不同(实测 opus-mt zh->en=100%)——某语向掉下阈值即该向术语正被 MT 打散(已自愈退回
#   无保护译文=不吐残迹但术语未锁),/ops 据此告警。零额外时延(只累加两个整数);关(=0)→不统计。历史一次性靠
#   探针发现"旧占位符仅 58% survive",此机制把该发现沉淀成长期免疫:任何语向/后端劣化,运维即刻可见。
_GLOSSARY_SURV_ON   = os.environ.get("INTERP_GLOSSARY_SURVIVAL", "1") == "1"
_GLOSSARY_SURV_MIN  = max(1, int(os.environ.get("INTERP_GLOSSARY_SURVIVAL_MIN", "20")))     # 够样本(占位数)才判定,防小样本误报
_GLOSSARY_SURV_WARN = float(os.environ.get("INTERP_GLOSSARY_SURVIVAL_WARN", "0.85"))         # 存活率告警阈值
_GLOSSARY_SURV_REGRESS = float(os.environ.get("INTERP_GLOSSARY_SURVIVAL_REGRESS", "0.05"))   # 探针回归提示阈:较上次同后端跌≥此值即标⚠(即便仍>WARN,抓悄悄漂移)
_gloss_surv = {}                               # "src->dest" -> {ph:占位总数, surv:存活数, sent:含术语句数, fb:回退句数}
_gloss_surv_lock = threading.Lock()
_gloss_surv_alerted = set()                    # 已 firing 的语向 key：仅在跨阈值时 raise/clear，避免每句翻译都碰告警文件
_GLOSS_PROBE_EVERY = max(0, int(os.environ.get("INTERP_GLOSSARY_PROBE_EVERY", "0")))   # 定时探针间隔(分);0=关。开则后台每N分自检存活+抓回归漂移(无人值守)
_GLOSS_PROBE_IDLE  = max(0, int(os.environ.get("INTERP_GLOSSARY_PROBE_IDLE", "20")))   # 仅最近N秒无翻译活动才跑(避让直播抢MT);0=不管忙闲
_last_xlate_mono = 0.0                          # 最近一次经 _translate_nmt 的真译时刻(monotonic),供定时探针避让直播


def _gloss_surv_maybe_alert(key: str, b: dict):
    """占位存活率「跨阈值」时(仅此刻)触发/解除系统级告警(alerts.py→webhook/钉钉/本地toast)。够样本才判;附处置建议。"""
    if alerts is None:
        return
    ph = b["ph"]
    if ph < _GLOSSARY_SURV_MIN:            # 样本不足不判定(防开播头几句误报)
        return
    below = (b["surv"] / ph) < _GLOSSARY_SURV_WARN if ph else False
    was = key in _gloss_surv_alerted
    ak = "interp_gloss_surv_" + key
    rate_pct = round(b["surv"] / ph * 100) if ph else 0
    if below and not was:
        _gloss_surv_alerted.add(key)
        try:
            alerts.raise_alert(ak, f"术语占位存活率偏低 {key} {rate_pct}%",
                detail=(f"{key} 占位真·存活率 {rate_pct}% (<{round(_GLOSSARY_SURV_WARN*100)}%, 样本 {ph} 占位)。"
                        "该语向术语锁定正被 MT 打散→已自愈退回无保护译文(不吐残迹但术语未锁)。"
                        "处置建议：①核查该向 MT 后端/模型(不同后端存活率不同)；"
                        "②换更稳的占位符(现为 ' Z%dQ '，数字锚定+字母框零碰撞)；③或降低对该向术语锁定的预期。"),
                level="warn", source="interpreter(LingoX)")
        except Exception as e:
            logger.warning(f"占位存活告警发送失败: {e}")
    elif was and not below:
        _gloss_surv_alerted.discard(key)
        try:
            alerts.clear_alert(ak, note=f"{key} 占位存活率已回升至 {rate_pct}%")
        except Exception:
            pass


def _gloss_surv_record(src: str, dest: str, n_ph: int, miss: int):
    """记一次带词表翻译的占位存活：n_ph 个占位、miss 个未 survive(被 MT 打散)。按语向累计,供在线自检+告警。"""
    if not _GLOSSARY_SURV_ON or n_ph <= 0:
        return
    key = f"{src}->{dest}"
    with _gloss_surv_lock:
        b = _gloss_surv.get(key) or _gloss_surv.setdefault(key, {"ph": 0, "surv": 0, "sent": 0, "fb": 0})
        b["ph"]   += n_ph
        b["surv"] += (n_ph - miss)
        b["sent"] += 1
        if miss:
            b["fb"] += 1
        snap = dict(b)                    # 锁内快照，锁外判定/发告警(不在持锁时做文件 IO)
    _gloss_surv_maybe_alert(key, snap)


def _gloss_surv_report() -> dict:
    """汇总各语向占位存活率 + 是否低于阈值(够样本才判)。供 /config/translate_cache 暴露、/ops 自检告警。"""
    pairs, low = [], []
    with _gloss_surv_lock:
        for key, b in sorted(_gloss_surv.items()):
            ph = b["ph"]
            rate = round(b["surv"] / ph, 3) if ph else 0.0
            enough = ph >= _GLOSSARY_SURV_MIN
            below = enough and rate < _GLOSSARY_SURV_WARN
            pairs.append({"pair": key, "ph": ph, "surv": b["surv"], "sent": b["sent"],
                          "fb": b["fb"], "rate": rate, "enough": enough, "below": below})
            if below:
                low.append(key)
    return {"on": _GLOSSARY_SURV_ON, "min": _GLOSSARY_SURV_MIN, "warn": _GLOSSARY_SURV_WARN,
            "pairs": pairs, "below": low}


def _tr_cache_clear():
    with _tr_cache_lock:
        _tr_cache.clear()
        _tr_cache_stat["hit"] = _tr_cache_stat["miss"] = 0
        _tr_miss.clear()
    with _gloss_surv_lock:
        _gloss_surv.clear()          # 清缓存时一并归零占位存活自检(换引擎/语向后重新累计,免旧样本污染)
    if alerts is not None:           # 统计归零→同时解除该维度所有 firing 告警,免残留
        for k in list(_gloss_surv_alerted):
            try: alerts.clear_alert("interp_gloss_surv_" + k, note="占位存活统计已重置")
            except Exception: pass
    _gloss_surv_alerted.clear()


def _translate_nmt(text: str, src: str, dest: str) -> str:
    """翻译总入口：缓存查 → 术语锁定(词表) → MT → 术语还原 → 缓存写。词表空/关=直接 MT；缓存关=不缓存。
    所有译路都经此，故缓存与术语锁定对全链路统一生效(零回归：关且空表时逐字节等价旧行为)。"""
    text = (text or "").strip()
    if not text:
        return text
    if src == dest:
        # 同语向(如中文对中文的变声通话)：原样透传。送 MT/LLM 会被"翻译"成改写稿
        # (实测 zh→zh 大模型把「为什么会有两遍中文?」改写成「你为什么会输入两遍中文?」)，
        # 变声场景必须一字不差；顺带省一跳 LLM 延迟。
        return text
    global _last_xlate_mono
    _last_xlate_mono = time.monotonic()              # 标记翻译活跃,供定时探针避让直播(探针走 _translate_raw,不触此)
    # P0-R2 滚动语境：取该语向近句对喂 LLM。缓存键仅对「含指代词」的句子附语境签名——
    # 指代句("它支持快充")在不同语境下译文必须不同,不签名会错误复用旧语境译文；
    # 非指代句语境只影响风格,复用缓存与旧(无语境)行为等价 → TM 命中率不塌方。
    ctx = _mt_ctx_get(src, dest)
    ctx_sig = hash(tuple(ctx)) if (ctx and _CTX_ANAPHORA_RE.search(text)) else 0
    key = None
    if _TR_CACHE_ON and src != dest:                 # 同语向无需缓存(直译才有意义)
        _glossary_load()                             # 刷新 ver(热加载)，保证键含最新词表签名
        key = (src, dest, _GLOSSARY_ON, _glossary_cache["ver"], ctx_sig, text)
        with _tr_cache_lock:
            hit = _tr_cache.get(key)
            if hit is not None:
                _tr_cache.move_to_end(key)           # LRU：命中挪到最新
                _tr_cache_stat["hit"] += 1
                return hit
            _tr_cache_stat["miss"] += 1
            if _TM_STATS_ON and len(text) <= _TM_STATS_MISS_MAXLEN:   # 记短句未命中(会话末并入累计→趋势/反哺)
                mk = (src, dest, text)
                if mk in _tr_miss or len(_tr_miss) < _TR_MISS_CAP:
                    _tr_miss[mk] = _tr_miss.get(mk, 0) + 1
    # 未命中/不缓存 → 正常翻译(术语锁定 + MT + 还原 + 失败回退)
    protected, mapping = _glossary_pre(text, src, dest)
    if not mapping:
        out = _translate_raw(text, src, dest, ctx=ctx)
    else:
        raw = _translate_raw(protected, src, dest, ctx=ctx)
        restored, miss = _glossary_post(raw, mapping)
        _gloss_surv_record(src, dest, len(mapping), miss)   # 在线自检：累计该语向占位真·存活率
        if miss:  # 占位符未 survive MT(被吞/打散)→退回无保护译文，宁可术语未锁也不吐残迹，永不劣于无词表
            logger.warning(f"术语占位 {miss}/{len(mapping)} 处未 survive MT，回退无保护译文({src}->{dest})")
            out = _translate_raw(text, src, dest, ctx=ctx)
        else:
            out = restored
    if key is not None and out and out != text:       # 仅缓存"确实翻译了"的结果(防固化失败回显)
        with _tr_cache_lock:
            _tr_cache[key] = out
            _tr_cache.move_to_end(key)
            while len(_tr_cache) > _TR_CACHE_MAX:
                _tr_cache.popitem(last=False)          # 逐出最久未用
    return out


# ── 术语占位「存活率」按需实测探针：用一组固定「术语密集」句(含相邻术语压力)过当前 MT 双向，绕缓存直测占位
#   真·存活率 → 开播前/换 MT 后端后**一键得双向确定结论**(不必等真实流量慢慢积累到"够样本")。结果并入在线自检
#   累计(_gloss_surv)→/ops 徽章即刻转"确定判定"、跨阈值同样触发系统告警。探针句本身不入 TM 缓存(直调底层原语)。
_SURV_PROBE = {
    "zh": [
        "通译支持数字人换脸和声音克隆",
        "数字人换脸由通译驱动",
        "通译的数字人换脸效果自然，声音克隆逼真",
        "欢迎体验通译数字人",
        "声音克隆加数字人加换脸都用通译",
        "今天用通译演示实时换脸和声音克隆",
        "通译的换脸和声音克隆很稳定",
    ],
    "en": [
        "LingoX supports digital human face swap and voice cloning",
        "the digital human face swap is powered by LingoX",
        "LingoX makes digital human face swap natural and voice cloning realistic",
        "welcome to try the LingoX digital human",
        "voice cloning plus digital human plus face swap all use LingoX",
        "today we demo real-time face swap and voice cloning with LingoX",
        "the LingoX face swap and voice cloning are stable",
    ],
}
_GLOSS_SURV_HIST     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "gloss_survival.jsonl")
_GLOSS_SURV_HIST_MAX = 500          # 存活基线保留最近 N 次探针(供换 MT 后端前后对比;满即滚动)


def _mt_tag():
    """当前 MT 后端标识,供存活基线区分后端：优先 INTERP_MT_TAG(换 NLLB/Qwen-MT 等时手工标),否则用 STT 端点。"""
    return os.environ.get("INTERP_MT_TAG", "").strip() or STT_URL


def _translate_status() -> dict:
    """翻译链路状态(供 /metrics 与 Hub 状态条)：当前层/熔断/TM/术语。"""
    hit = int(_tr_cache_stat.get("hit") or 0)
    miss = int(_tr_cache_stat.get("miss") or 0)
    total = hit + miss
    llm_open = time.time() >= _llm_fail_until
    layer = "nmt"
    layer_detail = "MarianMT/NLLB"
    if _MT_BACKEND in ("auto", "llm"):
        if llm_open:
            layer = "llm"
            layer_detail = _LLM_MODEL
        else:
            layer = "nmt"
            layer_detail = f"NMT( LLM熔断 {_LLM_COOLDOWN:.0f}s )"
    elif _MT_BACKEND == "local":
        layer = "nmt"
        layer_detail = "本地 NMT"
    _glossary_load()
    return {
        "layer": layer,
        "layer_detail": layer_detail,
        "llm_circuit_open": llm_open,
        "llm_model": _LLM_MODEL,
        "glossary_on": bool(_GLOSSARY_ON),
        "glossary_ver": int(_glossary_cache.get("ver") or 0),
        "tm_hit_rate_pct": round(100.0 * hit / total, 1) if total else None,
        "tm_hits": hit,
        "tm_misses": miss,
        "tm_on": bool(_TR_CACHE_ON),
    }


def _gloss_surv_prev(backend):
    """读存活基线里最近一条「同后端」记录(供回归对比)。无/坏/无同后端 → None。只按 backend 匹配,换后端不误比。"""
    try:
        if not os.path.exists(_GLOSS_SURV_HIST):
            return None
        with open(_GLOSS_SURV_HIST, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for ln in reversed(lines):
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if r.get("backend") == backend:
                return r
    except Exception:
        pass
    return None


def _gloss_surv_attach_delta(out, prev):
    """给本次各语向结果挂上「较上次同后端」的 prev_rate/delta/regress(纯函数,便于单测)。
    prev=上一条同后端基线记录(或 None)。delta=本次rate-上次rate;regress=跌幅≥REGRESS 阈(即便仍>WARN)。"""
    prev_map = {d.get("pair"): d for d in ((prev or {}).get("dirs") or [])}
    for d in out:
        pd = prev_map.get(d.get("pair"))
        pr = pd.get("rate") if pd else None
        if pr is not None and d.get("rate") is not None:
            d["prev_rate"] = pr
            d["delta"] = round(d["rate"] - pr, 3)
            d["regress"] = (d["delta"] <= -_GLOSSARY_SURV_REGRESS)
        else:
            d["prev_rate"] = None
            d["delta"] = None
            d["regress"] = False
    return out


def _gloss_survival_probe(dirs=None):
    """按需过当前 MT 实测各语向占位真·存活率(绕缓存、术语密集句、含相邻压力),结果并入 _gloss_surv 在线自检
    (→/ops 徽章 + 跨阈值告警)。dirs=[(src,dst),...],缺省=当前会话两方向。
    返回 {min,warn,dirs:[{pair,sents,ph,surv,miss,rate,verdict}]}。verdict: healthy/degraded/no_terms。"""
    if not dirs:
        dirs = [(_SRC_LANG, _DST_LANG), (_DST_LANG, _SRC_LANG)]
    seen, out = set(), []
    for (src, dst) in dirs:
        if not src or not dst or src == dst or (src, dst) in seen:
            continue
        seen.add((src, dst))
        ph = surv = miss_tot = sents = 0
        for sent in _SURV_PROBE.get(src, []):
            protected, mapping = _glossary_pre(sent, src, dst)
            if not mapping:
                continue
            raw = _translate_raw(protected, src, dst)
            _restored, miss = _glossary_post(raw, mapping)
            n = len(mapping)
            ph += n; surv += (n - miss); miss_tot += miss; sents += 1
            _gloss_surv_record(src, dst, n, miss)      # 并入在线自检(→/ops 徽章 + 跨阈值系统告警)
        rate = round(surv / ph, 3) if ph else None
        verdict = "no_terms" if ph == 0 else ("healthy" if rate >= _GLOSSARY_SURV_WARN else "degraded")
        out.append({"pair": f"{src}->{dst}", "sents": sents, "ph": ph, "surv": surv,
                    "miss": miss_tot, "rate": rate, "verdict": verdict})
    backend = _mt_tag()
    prev = _gloss_surv_prev(backend)     # 先读上一条同后端基线(用于回归对比),再写本次(避免自比)
    _gloss_surv_attach_delta(out, prev)  # 各向挂 prev_rate/delta/regress
    if out:                              # 留档基线(带后端标+时间戳)：换 MT 后端前后可离线对比逐语向存活率
        try:
            os.makedirs(os.path.dirname(_GLOSS_SURV_HIST), exist_ok=True)
            # 落盘只存"实测量"(pair/ph/surv/rate/verdict…),不含 delta/prev(那是相对量,读时再算,免历史里存冗余)
            slim = [{k: v for k, v in d.items() if k not in ("prev_rate", "delta", "regress")} for d in out]
            rec = json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "backend": backend, "dirs": slim},
                             ensure_ascii=False) + "\n"
            lines = []
            if os.path.exists(_GLOSS_SURV_HIST):
                with open(_GLOSS_SURV_HIST, "r", encoding="utf-8") as f:
                    lines = f.readlines()[-(_GLOSS_SURV_HIST_MAX - 1):]
            lines.append(rec)
            with open(_GLOSS_SURV_HIST, "w", encoding="utf-8") as f:
                f.writelines(lines)
        except Exception as e:
            logger.warning(f"存活基线写入失败: {e}")
    return {"min": _GLOSSARY_SURV_MIN, "warn": _GLOSSARY_SURV_WARN, "regress_thresh": _GLOSSARY_SURV_REGRESS,
            "backend": backend, "prev_ts": (prev or {}).get("ts"), "dirs": out}


def _gloss_regress_notify(res: dict):
    """探针检出「较上次同后端回归」(仍≥WARN但明显下滑)→一次性事件通知(webhook+toast,非状态化,不常驻active)。
    绝对劣化(<WARN)另有状态化告警(_gloss_surv_maybe_alert 已在 _gloss_surv_record 内触发);此处专抓漂移。"""
    if alerts is None or not res:
        return
    backend = res.get("backend", "?")
    for d in (res.get("dirs") or []):
        if not d.get("regress"):
            continue
        pair, dl = d.get("pair"), abs(d.get("delta") or 0)
        try:
            alerts.notify_event(
                f"术语存活漂移 {pair} ↓{round(dl * 100)}pp",
                detail=(f"{pair} 占位存活 {round((d.get('rate') or 0) * 100)}% "
                        f"(较上次同后端 {round((d.get('prev_rate') or 0) * 100)}% 跌 {round(dl * 100)}pp,后端 {backend})。"
                        "仍≥WARN 故未触发劣化告警,但同后端明显下滑→疑 MT 模型/权重变动或输入分布漂移。"
                        "建议:核查 MT 后端是否更新 / 跑 acceptance --only glosssurv 看趋势。"),
                level="warn", source="interpreter(LingoX)")
        except Exception as e:
            logger.warning(f"回归事件通知失败: {e}")


def _gloss_probe_scheduler():
    """后台定时探针(opt-in)：每 _GLOSS_PROBE_EVERY 分自检占位存活,漂移/劣化自动进告警,无人值守也能抓。
    避让直播(最近 _GLOSS_PROBE_IDLE 秒内有翻译则跳过,短睡快重试);best-effort 吞异常,永不拖垮服务。"""
    every = _GLOSS_PROBE_EVERY * 60
    if every <= 0:
        return
    time.sleep(min(float(every), 120.0))              # 启动后缓一会(等 MT/预热就绪)再首探
    while True:
        slept = float(every)
        try:
            busy = (_GLOSS_PROBE_IDLE > 0 and _last_xlate_mono > 0
                    and (time.monotonic() - _last_xlate_mono) < _GLOSS_PROBE_IDLE)
            if busy:
                slept = min(float(every), 60.0)        # 直播正忙→短睡快重试,别抢 MT
            else:
                res = _gloss_survival_probe()
                _gloss_regress_notify(res)             # 回归→一次性事件通知(webhook/toast)
                logger.info("定时探针: " + " ; ".join(
                    f"{d.get('pair')} {round((d.get('rate') or 0) * 100)}%"
                    + (f"↓{round(abs(d.get('delta') or 0) * 100)}pp" if d.get("regress") else "")
                    for d in (res.get("dirs") or [])) + f" (后端 {res.get('backend')})")
        except Exception:
            logger.exception("定时探针异常")
        time.sleep(slept)


def _gloss_probe_kick():
    """按 _GLOSS_PROBE_EVERY 起后台定时探针线程(0=不起)。进程级单发,启动时调用一次。"""
    if _GLOSS_PROBE_EVERY <= 0:
        return False
    threading.Thread(target=_gloss_probe_scheduler, name="gloss-probe", daemon=True).start()
    logger.info(f"定时占位存活探针已启用: 每 {_GLOSS_PROBE_EVERY} 分 · 忙闲阈 {_GLOSS_PROBE_IDLE}s (漂移自动进告警)")
    return True


# ── 翻译记忆预热(TM warmup)：会话启动后台预译高频句/开场白/应答词 → 首次出现即命中缓存(~0ms) ──
#   预热=对每条高频句预调 _translate_nmt(缓存自然填充，含术语锁定+版本签名，与实时链路逐字节一致)。
#   仅缓存开时有意义；best-effort 后台线程(不阻塞会话、吞异常)；跨会话缓存不清→只首会话真译、后续全命中。
#   MT 全挂→回显不缓存、下次自然重试(不固化坏译)。短语表 data/warmup.json：{语言码:[高频句]}。
_TM_WARMUP_ON    = os.environ.get("INTERP_TM_WARMUP", "1") == "1"
_TM_WARMUP_MAX   = max(0, int(os.environ.get("INTERP_TM_WARMUP_MAX", "200")))   # 单语言预热条数上限(防过载)
_TM_WARMUP_DELAY = float(os.environ.get("INTERP_TM_WARMUP_DELAY", "1.5"))       # 启动后延迟(让首句不与预热抢 MT)
_TM_WARMUP_PATH  = (os.environ.get("INTERP_TM_WARMUP_PATH", "").strip()
                    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "warmup.json"))
# 自学：扫往期转写(logs/interp_transcript_*.json)高频短源句并入预热，让预热随真实场景进化(域内口头禅/产品名/套话)。
_TM_WARMUP_LEARN        = os.environ.get("INTERP_TM_WARMUP_LEARN", "1") == "1"
_TM_WARMUP_LEARN_MIN    = max(2, int(os.environ.get("INTERP_TM_WARMUP_LEARN_MIN", "3")))       # 最少复现次数才学
_TM_WARMUP_LEARN_MAXLEN = max(1, int(os.environ.get("INTERP_TM_WARMUP_LEARN_MAXLEN", "24")))   # 短句长度上限(字符)
_TM_WARMUP_LEARN_FILES  = max(1, int(os.environ.get("INTERP_TM_WARMUP_LEARN_FILES", "20")))    # 最多扫最近 N 个转写
_TM_WARMUP_LEARN_TOP    = max(1, int(os.environ.get("INTERP_TM_WARMUP_LEARN_TOP", "60")))      # 每语言自学条数上限
_tm_warmup_last  = {"done": 0, "learned": 0, "size_before": 0, "size_after": 0,
                    "ms": 0, "src": "", "dst": "", "at": 0.0}
_tm_warmup_lock  = threading.Lock()


def _tm_warmup_load():
    """读 data/warmup.json：{语言码:[高频句...]}。以 _ 开头键=注释忽略；去重保序；缺失/坏→空(不报错)。"""
    try:
        with open(_TM_WARMUP_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"预热短语表读取失败({_TM_WARMUP_PATH})：{e}；跳过预热")
        return {}
    out = {}
    if isinstance(raw, dict):
        for lang, arr in raw.items():
            if not lang or lang.startswith("_") or not isinstance(arr, list):
                continue
            seen = set(); phrases = []
            for p in arr:
                if isinstance(p, str):
                    p = p.strip()
                    if p and p not in seen:
                        seen.add(p); phrases.append(p)
            if phrases:
                out[lang.strip().lower()] = phrases
    return out


def _tm_warmup_learn():
    """自学：扫最近 N 个 logs/interp_transcript_*.json，统计各语言高频短源句(>=MIN 次且 <=MAXLEN 字)，
    按频次降序每语言取前 TOP，返回 {语言码:[句...]}。用于把真实高频内容并入预热(域内进化)。
    某条源句语言：who=me→会话 src、who=other→会话 dst；whisper 无源句(src 空)跳过；坏文件跳过；关/缺→{}。
    大小写不敏感合并计数(取最常见表面写法)，故 'OK'/'ok' 归一，输出保留原写法。"""
    if not _TM_WARMUP_LEARN:
        return {}
    import glob
    from collections import Counter
    try:
        files = sorted(glob.glob(os.path.join("logs", "interp_transcript_*.json")),
                       reverse=True)[:_TM_WARMUP_LEARN_FILES]
    except Exception:
        return {}
    counters = {}    # lang -> Counter(归一化键 -> 次数)
    surfaces = {}    # lang -> {归一化键 -> Counter(表面写法 -> 次数)}
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                j = json.load(f)
        except Exception:
            continue
        m_src = (j.get("src") or "").strip().lower()
        m_dst = (j.get("dst") or "").strip().lower()
        for e in (j.get("entries") or []):
            who = e.get("who")
            lang = m_src if who == "me" else (m_dst if who == "other" else "")
            if not lang:
                continue
            s = (e.get("src") or "").strip()
            if not s or len(s) > _TM_WARMUP_LEARN_MAXLEN:
                continue
            key = s.casefold()
            counters.setdefault(lang, Counter())[key] += 1
            surfaces.setdefault(lang, {}).setdefault(key, Counter())[s] += 1
    out = {}
    for lang, ctr in counters.items():
        picked = []
        for key, n in ctr.most_common():
            if n < _TM_WARMUP_LEARN_MIN:
                break                                    # most_common 降序，后续更少→可停
            picked.append(surfaces[lang][key].most_common(1)[0][0])   # 取最常见表面写法
            if len(picked) >= _TM_WARMUP_LEARN_TOP:
                break
        if picked:
            out[lang] = picked
    return out


def _tm_warmup_merge(static, learned):
    """并集：每语言 静态在前 + 自学在后(已频次降序)，去重保序。返回 {语言码:[句]}。"""
    out = {}
    langs = list((static or {}).keys()) + [l for l in (learned or {}) if l not in (static or {})]
    for lang in langs:
        seen = set(); lst = []
        for p in list((static or {}).get(lang, [])) + list((learned or {}).get(lang, [])):
            if p not in seen:
                seen.add(p); lst.append(p)
        if lst:
            out[lang] = lst
    return out


def _tm_warmup_run(src: str, dst: str):
    """对当前语向两方向的高频句(静态表 + 往期转写自学)预调 _translate_nmt 填充缓存。
    src==dst / 缓存关 / 无句 → 空跑。返回汇总(含自学条数)。"""
    summary = {"done": 0, "learned": 0, "size_before": len(_tr_cache), "size_after": len(_tr_cache),
               "ms": 0, "src": src, "dst": dst, "at": time.time()}
    if not (_TR_CACHE_ON and src and dst and src != dst):
        return summary
    learned = _tm_warmup_learn()
    data = _tm_warmup_merge(_tm_warmup_merge(_tm_warmup_load(), learned), _tm_warmup_stats_phrases())
    summary["learned"] = sum(len(v) for v in learned.values())
    if not data:
        return summary
    t0 = time.time()
    for (frm, to) in ((src, dst), (dst, src)):
        for ph in data.get(frm, [])[:_TM_WARMUP_MAX]:
            try:
                _translate_nmt(ph, frm, to)            # 命中即近零成本；未命中则真译并入缓存
                summary["done"] += 1
            except Exception:
                pass
    summary["size_after"] = len(_tr_cache)
    summary["ms"] = int((time.time() - t0) * 1000)
    with _tm_warmup_lock:
        _tm_warmup_last.update(summary)
    logger.info(f"TM 预热完成：预调 {summary['done']} 句(自学 {summary['learned']}) · "
                f"缓存 {summary['size_before']}→{summary['size_after']} · {summary['ms']}ms ({src}⇄{dst})")
    return summary


def _tm_warmup_kick(src: str, dst: str, delay=None):
    """后台预热(默认延迟 _TM_WARMUP_DELAY 秒起，避开首句抢 MT)。best-effort，不阻塞调用者。"""
    if not (_TM_WARMUP_ON and _TR_CACHE_ON):
        return False
    d = _TM_WARMUP_DELAY if delay is None else delay
    def _job():
        try:
            if d and d > 0:
                time.sleep(d)
            _tm_warmup_run(src, dst)
        except Exception:
            logger.exception("TM 预热线程异常")
    threading.Thread(target=_job, name="tm-warmup", daemon=True).start()
    return True


def _tm_stats_load() -> dict:
    """读 logs/interp_tm_stats.json：{sessions:[...], misses:[{src,dst,text,n}], updated_at}。缺失/坏→空骨架。"""
    try:
        with open(_TM_STATS_PATH, "r", encoding="utf-8") as f:
            j = json.load(f)
        if isinstance(j, dict):
            j.setdefault("sessions", []); j.setdefault("misses", [])
            return j
    except Exception:
        pass
    return {"sessions": [], "misses": [], "updated_at": None}


def _tm_stats_record(stamp: str):
    """会话结束：本场缓存命中(差分)/预热快照 append 进趋势(滚动保留最近 _TM_STATS_MAX)，本会话未命中短句
    并入累计计数(保留 top _TM_STATS_MISS_TOP)。原子落盘并清本会话未命中计数。关(=0)→不落不清(零开销)。"""
    if not _TM_STATS_ON:
        return None
    try:
        with _tr_cache_lock:
            hit_now, miss_now, size = _tr_cache_stat["hit"], _tr_cache_stat["miss"], len(_tr_cache)
            base_h, base_m = _tm_session_base["hit"], _tm_session_base["miss"]
            miss_items = list(_tr_miss.items())
        with _tm_warmup_lock:
            w = dict(_tm_warmup_last)
        d_hit = max(0, hit_now - base_h); d_miss = max(0, miss_now - base_m)
        tot = d_hit + d_miss
        snap = {"at": stamp, "hit": d_hit, "miss": d_miss,
                "rate": round(d_hit / tot, 4) if tot else 0.0, "size": size,
                "warmup_done": w.get("done", 0), "warmup_learned": w.get("learned", 0),
                "src": _SRC_LANG, "dst": _DST_LANG}
        data = _tm_stats_load()
        data["sessions"] = (data.get("sessions", []) + [snap])[-_TM_STATS_MAX:]
        agg = {}
        for it in data.get("misses", []):
            if isinstance(it, dict) and it.get("text"):
                agg[(it.get("src", ""), it.get("dst", ""), it["text"])] = int(it.get("n", 0))
        for (k, n) in miss_items:
            agg[k] = agg.get(k, 0) + n
        top = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[:_TM_STATS_MISS_TOP]
        data["misses"] = [{"src": k[0], "dst": k[1], "text": k[2], "n": n} for (k, n) in top]
        import datetime as _dt
        data["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        os.makedirs(os.path.dirname(_TM_STATS_PATH), exist_ok=True)
        tmp = _TM_STATS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _TM_STATS_PATH)
        with _tr_cache_lock:
            _tr_miss.clear()
        logger.info(f"TM 统计已留存：本场命中率 {snap['rate']*100:.0f}% · 累计未命中句 {len(data['misses'])}")
        return _TM_STATS_PATH
    except Exception:
        logger.exception("写 TM 统计失败")
        return None


def _tm_warmup_stats_phrases():
    """opt-in 反哺：从累计未命中句取 n>=2 的短句(真复现、值得预热)，按源语言分组 {lang:[句]}。
    默认关(_TM_WARMUP_FROM_STATS=0)→ {}。与转写自学互补(这里是「真付了 MT 时延」的直接证据)。"""
    if not _TM_WARMUP_FROM_STATS:
        return {}
    out = {}
    for it in _tm_stats_load().get("misses", []):
        if not isinstance(it, dict):
            continue
        txt = (it.get("text") or "").strip(); lang = (it.get("src") or "").strip().lower()
        if txt and lang and int(it.get("n", 0)) >= 2:
            lst = out.setdefault(lang, [])
            if txt not in lst:
                lst.append(txt)
    return out


# ── P4-5 翻译记忆落地修复：命中率 17 场 0%~7% 的根因是「预热被关(避免与 32B LLM 抢 GPU)+
#   缓存只活在进程内(每次重启清零)」——高频未命中 top 全是问候/致谢这类预热表里本来就有的句子。
#   两针根治：①缓存持久化 data/tm_cache.json(重启后秒恢复,跨进程累积)；②预热挪到「开机空闲时」
#   慢速执行(句间留隙、会话进行中自动让路),不再与通话抢 GPU。会话级预热保持 env 关闭不变。
_TM_CACHE_PERSIST = os.environ.get("INTERP_TM_CACHE_PERSIST", "1") == "1"
_TM_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tm_cache.json")
_TM_WARMUP_BOOT       = os.environ.get("INTERP_TM_WARMUP_BOOT", "1") == "1"
_TM_WARMUP_BOOT_DELAY = float(os.environ.get("INTERP_TM_WARMUP_BOOT_DELAY", "45"))   # 开机后等服务就绪
_TM_WARMUP_BOOT_GAP   = float(os.environ.get("INTERP_TM_WARMUP_BOOT_GAP", "0.3"))    # 句间空隙(不占满 GPU)
_tm_boot_state = {"running": False, "done": 0, "translated": 0, "at": 0.0}


def _tm_cache_save():
    """翻译缓存快照落盘(≤LRU 上限,含词表 mtime 签名)。会话结束/预热完成时调用,开销毫秒级。"""
    if not (_TM_CACHE_PERSIST and _TR_CACHE_ON):
        return
    try:
        with _tr_cache_lock:
            items = [[k[0], k[1], bool(k[2]), k[4], v] for k, v in _tr_cache.items()]
        os.makedirs(os.path.dirname(_TM_CACHE_PATH), exist_ok=True)
        tmp = _TM_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"gloss_mtime": _glossary_cache["mtime"], "saved_at": time.time(),
                       "items": items}, f, ensure_ascii=False)
        os.replace(tmp, _TM_CACHE_PATH)
    except Exception:
        logger.exception("翻译缓存持久化失败(不影响运行)")


def _tm_cache_load():
    """启动恢复翻译缓存。词表 mtime 变了 → 整体弃用(键含术语签名,旧译可能带旧术语)。"""
    if not (_TM_CACHE_PERSIST and _TR_CACHE_ON):
        return
    try:
        if not os.path.exists(_TM_CACHE_PATH):
            return
        _glossary_load()                          # 先建立当前词表 ver/mtime
        with open(_TM_CACHE_PATH, "r", encoding="utf-8") as f:
            j = json.load(f) or {}
        if j.get("gloss_mtime") != _glossary_cache["mtime"]:
            logger.info("术语表已变更,持久化翻译缓存整体弃用(防旧术语陈译)")
            return
        ver = _glossary_cache["ver"]
        n = 0
        with _tr_cache_lock:
            for it in (j.get("items") or [])[-_TR_CACHE_MAX:]:
                try:
                    src, dest, gon, text, out = it
                except Exception:
                    continue
                if bool(gon) != _GLOSSARY_ON or not text or not out:
                    continue
                _tr_cache[(src, dest, _GLOSSARY_ON, ver, text)] = out
                n += 1
        if n:
            logger.info(f"翻译缓存已恢复 {n} 条(data/tm_cache.json)——高频句重启后仍即时命中")
    except Exception:
        logger.exception("读取持久化翻译缓存失败,从空缓存开始")


def _tm_boot_warmup_kick(delay: float = None):
    """开机空闲预热：延迟起、逐句慢速预译(两方向)、会话进行中自动让路。已命中句近零成本,
    故每次开机重跑安全(配合持久化=只有新句真付 MT)。返回是否启动。"""
    if not (_TM_WARMUP_BOOT and _TR_CACHE_ON):
        return False
    if _tm_boot_state["running"]:
        return False
    d = _TM_WARMUP_BOOT_DELAY if delay is None else delay
    def _job():
        _tm_boot_state["running"] = True
        try:
            time.sleep(max(0.0, d))
            src, dst = _SRC_LANG, _DST_LANG
            data = _tm_warmup_merge(_tm_warmup_merge(_tm_warmup_load(), _tm_warmup_learn()),
                                    _tm_warmup_stats_phrases())
            t0 = time.time()
            done = 0
            base_miss = _tr_cache_stat["miss"]
            for (frm, to) in ((src, dst), (dst, src)):
                for ph in data.get(frm, [])[:_TM_WARMUP_MAX]:
                    while ST.running:              # 通话中让路,结束后继续
                        time.sleep(5.0)
                    try:
                        _translate_nmt(ph, frm, to)
                        done += 1
                    except Exception:
                        pass
                    time.sleep(_TM_WARMUP_BOOT_GAP)
            translated = max(0, _tr_cache_stat["miss"] - base_miss)
            _tm_boot_state.update({"done": done, "translated": translated, "at": time.time()})
            _tm_cache_save()
            logger.info(f"TM 开机空闲预热完成：预调 {done} 句(真译 {translated}/其余命中) · "
                        f"{time.time()-t0:.0f}s · 缓存 {len(_tr_cache)} 条已持久化 ({src}⇄{dst})")
        except Exception:
            logger.exception("TM 开机预热线程异常")
        finally:
            _tm_boot_state["running"] = False
    threading.Thread(target=_job, name="tm-boot-warmup", daemon=True).start()
    return True


def _translate_google(text: str, src: str, dest: str) -> str:
    try:
        from deep_translator import GoogleTranslator
        s = "zh-CN" if src == "zh" else src
        d = "zh-CN" if dest == "zh" else dest
        return GoogleTranslator(source=s, target=d).translate(text) or text
    except Exception as e:
        logger.warning(f"Google 翻译失败({src}->{dest}): {e}")
        return text


# ── 同传语向(可运行时切换)：默认「我=中文 / 对方=英文」；经 env 或 POST /config/langs ──
# 改为任意 STT/NLLB 覆盖语向。方向A(我→对方)=SRC→DST；方向B(对方→我)=DST→SRC。
# 默认 zh/en 时行为与旧版完全一致(仅把此前硬编码的 "zh"/"en" 换成这两个变量)。
_SRC_LANG = (os.environ.get("INTERP_SRC_LANG", "zh").strip().lower() or "zh")   # 我方语言
_DST_LANG = (os.environ.get("INTERP_DST_LANG", "en").strip().lower() or "en")   # 对方语言


# P3-5 语言清单：常用语种置顶的内置底单(全链路验证过的语种)，远端 STT 的清单作补充合并。
# 之前"失败只回退中英"导致选择器长期只有两项(远端老版本没有 /translate/langs 就 404)——
# 底单让多语向不再被远端版本绑架；Nemotron 单模型 40 语种、Whisper 99 语种、LLM 翻译全覆盖。
# 显示名统一用中文(操作者为中文用户)；name 仅作 UI 展示，识别/翻译链路一律走 code。
_LANGS_BASE = [
    {"code": "zh", "name": "中文"},        {"code": "en", "name": "英语"},
    {"code": "ja", "name": "日语"},        {"code": "ko", "name": "韩语"},
    {"code": "ru", "name": "俄语"},        {"code": "fr", "name": "法语"},
    {"code": "de", "name": "德语"},        {"code": "es", "name": "西班牙语"},
    {"code": "pt", "name": "葡萄牙语"},    {"code": "it", "name": "意大利语"},
    {"code": "ar", "name": "阿拉伯语"},    {"code": "vi", "name": "越南语"},
    {"code": "th", "name": "泰语"},        {"code": "id", "name": "印尼语"},
]


# 远端增补语种的中文显示名(远端老版本仍回原生名/英文名时，本地统一成中文再进选择器)。
_LANG_DISP_ZH = {
    "ms": "马来语", "hi": "印地语", "tr": "土耳其语", "nl": "荷兰语", "pl": "波兰语",
    "uk": "乌克兰语", "tl": "菲律宾语", "km": "高棉语", "my": "缅甸语", "fa": "波斯语",
    "yue": "粤语",
}


def _fetch_stt_langs():
    """UI 语向选择器清单：内置底单(常用序) + 远端 STT 增补语种(去重后缀)。远端失败=纯底单。"""
    langs = list(_LANGS_BASE)
    seen = {l["code"] for l in langs}
    try:
        r = requests.get(f"{STT_URL}/translate/langs", timeout=3)
        if r.ok:
            for l in (r.json().get("langs") or []):
                c = (l.get("code") or "").strip().lower()
                if c and c not in seen:
                    seen.add(c)
                    langs.append({"code": c, "name": _LANG_DISP_ZH.get(c) or l.get("name") or c})
    except Exception:
        pass
    return langs


# ── 幻觉过滤(Whisper 在静音/底噪上常吐固定"幻听"套话，会刷屏字幕、甚至被克隆音说给对方) ──
HALLUC_FILTER   = os.environ.get("INTERP_HALLUC_FILTER", "1") == "1"
HALLUC_DEDUP_SEC = float(os.environ.get("INTERP_HALLUC_DEDUP", "4.0"))  # 同向连刷同句的抑制窗

# ── 前置音频门控：在送进 STT *之前* 就用绝对能量挡掉静音/底噪段，从源头杜绝
# "you / thank you / 谢谢 / 嗯" 这类静音幻听（治本，省一次 STT 往返）。阈值均可经环境变量调。
# 取值是相对满幅 1.0 的 dBFS：真实人声通常 > -40dBFS，空闲底噪/电流声多在 -45dBFS 以下。
GATE_ENABLE     = os.environ.get("INTERP_GATE", "1") == "1"
GATE_RMS_DBFS   = float(os.environ.get("INTERP_GATE_RMS_DBFS", "-50"))   # 整段 RMS 低于此=静音/底噪，丢弃
GATE_PEAK_DBFS  = float(os.environ.get("INTERP_GATE_PEAK_DBFS", "-41"))  # 峰值过低=无真实语音起音，丢弃
# 动态范围门：帧间能量起伏(p90-p10)。稳态底噪/电流声各帧能量趋同(实测手机麦底噪仅 2~4dB)，
# 真人语音有音节起伏(通常≥12dB)。响度门拦不住"响而平"的高底噪麦，这道门是关键补刀。
# 间隙极大(噪 2~4 / 话 12+)，默认 6dB 既稳挡底噪又不误伤真人。dyn 未算出(段过短=0)时本门不生效。
GATE_DYN_DB_MIN = float(os.environ.get("INTERP_GATE_DYN", "6.0"))
# 单词级"幻影词"情境过滤的门限：仅当该填充词来源音频"偏弱"或"几乎无起伏"时才判为幻听。
SOFT_FILLER_RMS_DBFS = float(os.environ.get("INTERP_FILLER_RMS_DBFS", "-36"))  # 弱于此=可疑
SOFT_FILLER_DYN_DB   = float(os.environ.get("INTERP_FILLER_DYN_DB", "9"))      # 帧间起伏(p90-p10)小于此=稳态噪声

# ── 自适应噪声底校准：固定 dBFS 门只在"标准麦克风/标准音量"下最优；麦增益高/系统音量大/
# 环境吵时底噪会整体抬高，固定 -50 门就放噪声进来。Segmenter 本就实时跟踪每路噪声底(self.noise)，
# 这里把它接进门控：门 = max(绝对门, 噪声底+余量)，并设上限避免误伤轻声真人。
# 关键：自适应只会"抬高"门(更严)、绝不低于当前固定门 → 对"零静音幻听"零回归，仅在吵环境额外收紧。
CALIB_ENABLE        = os.environ.get("INTERP_CALIB", "1") == "1"
CALIB_RMS_MARGIN_DB  = float(os.environ.get("INTERP_CALIB_RMS_MARGIN", "8"))   # RMS 门 = 噪声底 + 此余量
CALIB_PEAK_MARGIN_DB = float(os.environ.get("INTERP_CALIB_PEAK_MARGIN", "10")) # 峰值门 = 噪声底 + 此余量
CALIB_RMS_CEIL_DBFS  = float(os.environ.get("INTERP_CALIB_RMS_CEIL", "-33"))   # RMS 门上限(防误杀轻声)
CALIB_PEAK_CEIL_DBFS = float(os.environ.get("INTERP_CALIB_PEAK_CEIL", "-24"))  # 峰值门上限
CALIB_FILLER_MARGIN_DB = float(os.environ.get("INTERP_CALIB_FILLER_MARGIN", "14"))  # 软填充"弱"判据余量

_noise_lock = threading.Lock()
_NOISE_DBFS = {"a": None, "b": None}   # 各方向自适应噪声底(dBFS)，由 Segmenter 实时发布


def _publish_noise_floor(direction, rms_linear):
    """Segmenter 在静默块更新噪声底时调用，把线性 RMS 换算成 dBFS 存入共享表。"""
    if direction not in _NOISE_DBFS:
        return
    dbfs = 20.0 * float(np.log10(max(float(rms_linear), 1e-12)))
    with _noise_lock:
        _NOISE_DBFS[direction] = dbfs


def _noise_floor_dbfs(direction):
    with _noise_lock:
        return _NOISE_DBFS.get(direction)


def _reset_noise_floor():
    with _noise_lock:
        for k in _NOISE_DBFS:
            _NOISE_DBFS[k] = None


def _adaptive_gates(direction):
    """返回 (rms_gate, peak_gate) dBFS。基线=固定绝对门；噪声底+余量更高时按其抬高(夹在上限内)。"""
    rms_gate, peak_gate = GATE_RMS_DBFS, GATE_PEAK_DBFS
    if CALIB_ENABLE and direction is not None:
        nf = _noise_floor_dbfs(direction)
        if nf is not None:
            rms_gate  = min(max(rms_gate,  nf + CALIB_RMS_MARGIN_DB),  CALIB_RMS_CEIL_DBFS)
            peak_gate = min(max(peak_gate, nf + CALIB_PEAK_MARGIN_DB), CALIB_PEAK_CEIL_DBFS)
    return rms_gate, peak_gate

# ── Whisper 置信度门控（最强内部信号）：远端 STT 现随每段返回 no_speech_prob / avg_logprob。
# 对短输出(≤2 词 / ≤3 汉字，正是 you/您/谢谢 这类幻影词高发区)用更严阈值；长句放宽，
# 避免误伤真实长句中偶有低置信的一段。字段缺失(旧服务端)→ 本门控自动不生效，纯向后兼容。
CONF_GATE_ENABLE = os.environ.get("INTERP_CONF_GATE", "1") == "1"
CONF_NSP_SHORT = float(os.environ.get("INTERP_CONF_NSP_SHORT", "0.50"))  # 短输出 no_speech 高于此=幻听
CONF_LP_SHORT  = float(os.environ.get("INTERP_CONF_LP_SHORT", "-0.85"))  # 短输出 avg_logprob 低于此=幻听
CONF_NSP_LONG  = float(os.environ.get("INTERP_CONF_NSP_LONG", "0.80"))   # 长句放宽
CONF_LP_LONG   = float(os.environ.get("INTERP_CONF_LP_LONG", "-1.30"))

# 这些"视频片尾/字幕组"套话在 1 对 1 通话里绝不会出现。多为 "X by Y" 变体(字幕by索兰娅 /
# Subtitles by Solanya / Subtitles by the Amara.org community)，故用"词干子串"命中，覆盖任意后缀。
_HALLUC_STEMS = (
    "subtitles by", "subtitle by", "captions by", "transcription by", "translated by",
    "subtitler", "subtitle maker", "subtitles maker", "edited by", "zither harp",
    "thanks for watching", "thank you for watching", "thanks for your watching",
    "please subscribe", "like and subscribe", "amara",
    # 中文片尾/字幕组套话（含繁体）：通话里绝不会出现
    "字幕by", "字幕 by", "字幕志愿者", "字幕志願者", "字幕组", "字幕組",
    "字幕制作", "字幕製作", "制作人", "製作人", "剪辑", "剪輯", "沛队剪辑", "沛隊剪輯",
    "感谢观看", "感谢您的观看", "感谢大家观看", "谢谢观看", "谢谢大家观看",
    "感謝觀看", "感謝您的觀看", "謝謝觀看",
    "请订阅", "请点赞订阅", "下期再见", "請訂閱", "請點贊訂閱", "下期再見",
    "请不吝点赞", "明镜与点点栏目",
)
# 单词级"幻影词"：Whisper 在静音/底噪上最爱凭空吐出的整句短词（you / thank you / 谢谢 / 嗯…）。
# 这些真人偶尔也会说，故不无条件拉黑——只在"整句即此词 且 来源音频弱/无起伏"时判为幻听（见 _is_soft_filler）。
_SOFT_FILLERS = {
    "you", "you you", "you you you", "thank you", "thank you very much", "thanks",
    "thank", "thank you so much", "thank you for watching", "bye", "bye bye",
    "uh", "um", "umm", "hmm", "hm", "mm", "mhm", "oh", "ah", "yeah", "the", "so", "and", "i",
    "嗯", "嗯嗯", "呃", "啊", "哦", "噢", "诶", "唉", "谢谢", "谢谢你", "谢谢您", "謝謝", "您", "你",
}
_last_emit = {"a": ("", 0.0), "b": ("", 0.0)}
_emit_lock = threading.Lock()
_re_repeat_char = _re.compile(r"^(.)\1{3,}$")                  # 单字符重复≥4，如 "。。。。"
_re_repeat_unit = _re.compile(r"(.{2,40}?)(?:\s*\1){2,}")      # 2-40字词组连续重复≥3次(幻听典型)
_re_only_punct  = _re.compile(r"^[\s\.,!?。，！？、…·\-_=~]+$")  # 全是标点/分隔符
_re_strip_punct = _re.compile(r"^[\s\.,!?。，！？、…·\-_=~]+|[\s\.,!?。，！？、…·\-_=~]+$")


def _norm_text(t: str) -> str:
    return _re_strip_punct.sub("", (t or "").strip().lower())


_re_flat_all = _re.compile(r"[\s\.,!?。，！？、…·\-_=~；;:：'\"“”‘’]+")
_re_sent_split = _re.compile(r"(?<=[。！？!?；;.])\s*")


def _flat_text(t: str) -> str:
    """全串规范化(去所有标点/空白+小写),供复读比对。"""
    return _re_flat_all.sub("", (t or "").lower())


def _collapse_repeats(text: str):
    """折叠「紧邻复读」——ASR 复读幻觉/回录拼接/NMT 跟读的典型形态(实测 2026-07-08 12:33
    存疑晋升稿='我给你认识。我给你认识。' → 译成 'Let you meet him. Let you meet him.'
    → 克隆配音把同一句话对对方说了两遍)。两条保守规则,只动"逐字相同"的紧邻重复:
    ① 按句末标点切句,相邻句规范化后相同 → 只留一句;
    ② 整段无法切句但恰为同一半句重复(半句规范化后 ≥4 字符) → 折半。"""
    t = (text or "").strip()
    if len(_flat_text(t)) < 4:
        return text
    parts = [p for p in _re_sent_split.split(t) if p and p.strip()]
    if len(parts) >= 2:
        out = []
        for p in parts:
            if out and _flat_text(p) and _flat_text(p) == _flat_text(out[-1]):
                continue
            out.append(p)
        if len(out) < len(parts):
            cjk = any("\u4e00" <= c <= "\u9fff" for c in t)
            t2 = ("" if cjk else " ").join(s.strip() for s in out)
            logger.info(f"[复读折叠] {text!r} -> {t2!r}")
            return t2
        return text
    n = _flat_text(t)
    if len(n) >= 8 and len(n) % 2 == 0 and n[: len(n) // 2] == n[len(n) // 2:]:
        for i in range(2, len(t) - 1):     # 在原文上找切分点,使两侧规范化后相同
            if _flat_text(t[:i]) == _flat_text(t[i:]):
                t2 = t[:i].strip()
                logger.info(f"[复读折叠] {text!r} -> {t2!r}")
                return t2
    return text


def _is_hallucination(text: str) -> bool:
    """空 / 纯标点 / 单字符重复 / 已知片尾套话 → 判为 STT 幻听，丢弃。"""
    raw = (text or "").strip()
    if not raw or _re_only_punct.match(raw):
        return True
    n = _norm_text(raw)
    if not n:
        return True
    compact = _re.sub(r"\s+", "", n)
    if _re_repeat_char.match(compact):
        return True
    # 词组连续重复≥3次(如 "沛隊剪輯沛隊剪輯…" / "Pei Team Editing Pei Team Editing…")→ 幻听
    if _re_repeat_unit.search(n) or _re_repeat_unit.search(compact):
        return True
    # 词干子串命中(去掉内部标点后比对，如 "amara.org" → "amara org")
    key = _re.sub(r"[\.\-_/]", " ", n)
    key = _re.sub(r"\s+", " ", key).strip()
    return any(stem in key for stem in _HALLUC_STEMS)


def _dup_suppressed(direction: str, text: str) -> bool:
    """同方向短窗内连续吐出同一句(幻听 loop 的典型特征)→ 抑制。返回 True 表示应丢弃。"""
    n = _norm_text(text)
    if not n:
        return True
    now = time.time()
    with _emit_lock:
        last_n, last_t = _last_emit.get(direction, ("", 0.0))
        if n == last_n and (now - last_t) < HALLUC_DEDUP_SEC:
            _last_emit[direction] = (n, now)      # 刷新时间，持续抑制连刷
            return True
        _last_emit[direction] = (n, now)
    return False


def _audio_features(x: np.ndarray):
    """从 16k 单声道 float32 段抽取门控特征：整段 RMS/峰值(dBFS) + 帧间动态范围(p90-p10, dB)。
    动态范围用于区分"真实语音(有音节起伏，gap 与浊音差异大)"与"稳态底噪/电流声(各帧能量趋同)"。"""
    if x is None or getattr(x, "size", 0) == 0:
        return None
    x = np.asarray(x, dtype=np.float32).reshape(-1)
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    rms  = float(np.sqrt(np.mean(x * x)))
    rms_dbfs  = 20.0 * float(np.log10(rms + 1e-12))
    peak_dbfs = 20.0 * float(np.log10(peak + 1e-12))
    fl = max(1, int(0.02 * SR))                  # 20ms 帧
    n = (x.size // fl) * fl
    dyn_db = 0.0
    if n >= fl * 3:
        fr = x[:n].reshape(-1, fl)
        fr_db = 20.0 * np.log10(np.sqrt(np.mean(fr * fr, axis=1)) + 1e-12)
        # 流式方向A的音频是 RNNoise 洗后的：无声帧被压成≈数字零，混在段里会把整段
        # RMS 稀释到门下(实测真话 rms=-53 被 -50 门误杀)、把 dyn 撑到无意义的 191dB。
        # 门控特征只看"活跃帧"(> -75dBFS,零帧以上/一切真实底噪以下)；纯零段或全活跃段
        # (未降噪的原始音频)走原口径,行为不变。
        act = fr_db > -75.0
        if act.any() and (~act).any():
            fa = fr[act]
            rms_dbfs = 20.0 * float(np.log10(float(np.sqrt(np.mean(fa * fa))) + 1e-12))
            fad = fr_db[act]
            dyn_db = float(np.percentile(fad, 90) - np.percentile(fad, 10)) if fad.size >= 3 else 0.0
        else:
            dyn_db = float(np.percentile(fr_db, 90) - np.percentile(fr_db, 10))
    return {"rms_dbfs": rms_dbfs, "peak_dbfs": peak_dbfs,
            "dyn_db": dyn_db, "dur": x.size / float(SR)}


def _segment_is_speech(feats, direction=None) -> bool:
    """整段是否像真实语音；否则(静音/底噪)直接不送 STT —— 从源头杜绝静音幻听。
    门限 = max(固定绝对门, 自适应噪声底+余量)：标准环境≈原固定门，吵环境自动收紧。"""
    if not GATE_ENABLE or not feats:
        return True
    rms_gate, peak_gate = _adaptive_gates(direction)
    if feats["rms_dbfs"] < rms_gate:
        return False
    if feats["peak_dbfs"] < peak_gate:
        return False
    # 动态范围门：响而平稳=底噪(电流/风扇/麦自噪)，非语音。dyn=0 表示段过短未算出，跳过本门。
    dyn = feats.get("dyn_db", 0.0)
    if GATE_DYN_DB_MIN > 0 and dyn > 0 and dyn < GATE_DYN_DB_MIN:
        return False
    return True


def _is_soft_filler(text: str, feats, direction=None) -> bool:
    """整句仅为一个"幻影词"(you/thank you/谢谢/嗯…) 且来源音频"偏弱 或 几乎无起伏"→ 判幻听。
    "弱"判据同样自适应：max(固定阈, 噪声底+余量)，真人清晰说出(响度足、起伏大)则放行。"""
    if feats is None:
        return False
    n = _norm_text(text)
    if not n or n not in _SOFT_FILLERS:
        return False
    weak_thr = SOFT_FILLER_RMS_DBFS
    if CALIB_ENABLE and direction is not None:
        nf = _noise_floor_dbfs(direction)
        if nf is not None:
            weak_thr = max(weak_thr, nf + CALIB_FILLER_MARGIN_DB)
    weak = feats.get("rms_dbfs", 0.0) < weak_thr                 # 整体偏弱(相对噪声底)
    flat = feats.get("dyn_db", 99.0) < SOFT_FILLER_DYN_DB        # 帧间几乎无起伏(稳态噪声)
    return weak or flat


def _short_output(text: str) -> bool:
    """≤2 个空格词且 ≤3 个汉字 → 视为"短输出"(幻影词高发)，置信度门用更严阈值。"""
    n = _norm_text(text)
    if not n:
        return True
    cjk = sum(1 for ch in n if "\u4e00" <= ch <= "\u9fff")
    tokens = [w for w in n.split() if w]
    return len(tokens) <= 2 and cjk <= 3


def _low_confidence(text: str, meta):
    """据远端 Whisper 自报的 no_speech_prob / avg_logprob 判低置信幻听。
    返回 (drop: bool, reason: str)。meta 缺字段(旧服务端)→ 不判，向后兼容。"""
    if not CONF_GATE_ENABLE or not meta:
        return False, ""
    nsp = meta.get("no_speech_prob")
    lp  = meta.get("avg_logprob")
    if nsp is None and lp is None:
        return False, ""
    short = _short_output(text)
    tag = "短" if short else "长"
    nsp_max = CONF_NSP_SHORT if short else CONF_NSP_LONG
    lp_min  = CONF_LP_SHORT  if short else CONF_LP_LONG
    if nsp is not None and nsp >= nsp_max:
        return True, f"no_speech_prob={nsp:.2f}≥{nsp_max:.2f}({tag}输出)"
    if lp is not None and lp <= lp_min:
        return True, f"avg_logprob={lp:.2f}≤{lp_min:.2f}({tag}输出)"
    return False, ""


# 语种脚本审查：能靠字符集识别的语言 → 对应 Unicode 判定。拉丁系(en/es/…)全球通用不查。
_SCRIPT_CHECKS = {
    "zh": lambda ch: "\u4e00" <= ch <= "\u9fff",
    "ja": lambda ch: ("\u4e00" <= ch <= "\u9fff") or ("\u3040" <= ch <= "\u30ff"),
    "ko": lambda ch: "\uac00" <= ch <= "\ud7a3",
    "ru": lambda ch: "\u0400" <= ch <= "\u04ff",
    "ar": lambda ch: "\u0600" <= ch <= "\u06ff",
    "th": lambda ch: "\u0e00" <= ch <= "\u0e7f",
}


def _lang_sanity_drop(direction: str, text: str) -> bool:
    """语种健全性门：该通道的期望源语言可用字符集判定时，整句不含该字符集的"长输出"=幻听。
    实测 zh 通道对回声/压缩伪影会整句吐外语(印地语'हेलो'、匈牙利语'Nincs öröm maja'、
    英语'It was marked sword and ventlin')——language=zh 只是提示,模型仍会漂走。
    短输出(≤5 有效字符)放行：中文里夹的 OK/hello/品牌词不误伤。"""
    expect = _SRC_LANG if direction == "a" else _DST_LANG
    chk = _SCRIPT_CHECKS.get(expect)
    if chk is None:
        return False
    alnum = [ch for ch in (text or "") if ch.isalnum()]
    if len(alnum) <= 5:
        return False
    return not any(chk(ch) for ch in alnum)


def _stt_drop_reason(direction: str, text: str, feats=None, meta=None):
    """纯判定(不计数不落日志)：这条 STT 结果该不该拦。返回 (reason_key, 日志文案)；
    ("","") = 放行。reason_key ∈ halluc/lang/filler/lowconf/dedup——标记制据此分
    「可复核存疑」(halluc/lang/filler/lowconf) 与「必须硬拦」(dedup=连刷循环保护)。"""
    if not HALLUC_FILTER:
        return "", ""
    if _is_hallucination(text):
        return "halluc", f"[幻觉过滤] 幻听[{direction}]: {text!r}"
    if _lang_sanity_drop(direction, text):
        return "lang", f"[语种门] 非目标语种输出[{direction}]: {text!r}"
    if _is_soft_filler(text, feats, direction):
        return "filler", (f"[幻觉过滤] 弱能量填充词[{direction}]: {text!r} "
                          f"(rms={feats.get('rms_dbfs'):.1f}dBFS dyn={feats.get('dyn_db'):.1f}dB)")
    drop_conf, why = _low_confidence(text, meta)
    if drop_conf:
        return "lowconf", f"[幻觉过滤] 低置信[{direction}]: {text!r} ({why})"
    if _dup_suppressed(direction, text):
        return "dedup", f"[幻觉过滤] 连刷[{direction}]: {text!r}"
    if _xdedup_hit(direction, text):
        return "dedup", (f"[跨引擎去重] 与近窗找回稿重合[{direction}]: {text!r} "
                         "(存疑晋升/截断恢复已覆盖此内容)")
    return "", ""


def _drop_stt(direction: str, text: str, feats=None, meta=None) -> bool:
    """统一入口(分段管线用,行为不变)：判定+计数+日志，True=丢弃。"""
    key, msg = _stt_drop_reason(direction, text, feats, meta)
    if not key:
        return False
    ST.drops["halluc" if key == "lang" else key] += 1
    logger.info(f"丢弃{msg}")
    return True


REF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "refs")

# 通用兜底音色：INTERP_FALLBACK_VOICE=某 wav 路径(同名 .txt=参考文本)。
# 全新装机默认兜底到随包的「启动角色包」示例音色——角色没样本时也别整场静音(对方至少听得到声)。
# 想恢复"无样本即静音"的旧行为：设 INTERP_FALLBACK_VOICE=off。显式指定路径则用你的。
def _default_fallback_voice_path() -> str:
    env = (os.environ.get("INTERP_FALLBACK_VOICE", "") or "").strip()
    if env.lower() in ("off", "none", "0", "disable", "disabled"):
        return ""                            # 显式关闭：回到"无样本即静音"
    if env:
        return env                           # 用户显式指定的兜底音色
    cand = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "starter_profiles", "voices", "starter_zh_f.wav")
    return cand if os.path.exists(cand) else ""


FALLBACK_VOICE_PATH = _default_fallback_voice_path()


def _fallback_voice():
    """读 env 指定的通用兜底参考音。返回 (b64, ref_text)；未配置/文件缺失返回 None。"""
    if not FALLBACK_VOICE_PATH:
        return None
    try:
        p = FALLBACK_VOICE_PATH if os.path.isabs(FALLBACK_VOICE_PATH) else \
            os.path.join(os.path.dirname(os.path.abspath(__file__)), FALLBACK_VOICE_PATH)
        if not os.path.exists(p):
            logger.warning(f"兜底音色文件不存在: {p}(忽略)")
            return None
        with open(p, "rb") as f:
            b = base64.b64encode(f.read()).decode()
        t = ""
        tp = os.path.splitext(p)[0] + ".txt"
        if os.path.exists(tp):
            with open(tp, "r", encoding="utf-8") as f:
                t = f.read().strip()
        return (b, t) if b else None
    except Exception:
        logger.exception("兜底音色加载失败(忽略)")
        return None


def _local_short_ref(name: str):
    """优先用离线生成的"转写对齐短参考音"(make_interp_ref.py 产物)：合成更快、音质不降。
    仅当 refs/interp_<profile>.wav + .txt 同时存在时启用，否则回退 hub 全长参考。"""
    safe = _re.sub(r"[^\w\-]", "_", name or "")
    wavp = os.path.join(REF_DIR, f"interp_{safe}.wav")
    txtp = os.path.join(REF_DIR, f"interp_{safe}.txt")
    if os.path.exists(wavp) and os.path.exists(txtp):
        try:
            with open(wavp, "rb") as f:
                b = base64.b64encode(f.read()).decode()
            with open(txtp, "r", encoding="utf-8") as f:
                t = f.read().strip()
            if b:
                logger.info(f"使用离线短参考音 refs/interp_{safe}.wav ({len(b)//1024}KB) ref_text={t[:20]!r}")
                return b, t
        except Exception:
            logger.exception("读取离线短参考音失败，回退 hub 全长参考")
    return None


def _fetch_voice_ref(profile: str):
    """取角色音色参考(voice_b64 + reference_text)，供 Fish 克隆英文。
    优先离线短参考(快)，否则取 hub 全长参考。"""
    try:
        name = profile.strip()
        if not name:
            j0 = requests.get(f"{HUB_URL}/profiles", timeout=5).json()
            name = j0.get("active", "")
        short = _local_short_ref(name)
        if short:
            return short
        r = requests.get(f"{HUB_URL}/profiles/{name}",
                         params={"include_face": "true"}, timeout=10)
        r.raise_for_status()
        j = r.json()
        return j.get("voice_b64", "") or "", (j.get("fish_tts_params") or {}).get("reference_text", "") or ""
    except Exception:
        logger.exception("取角色音色参考失败")
        return "", ""


# ══════════ P1-3/P3-3 降噪前端（RNNoise 帧级流式优先，谱减法兜底）══════════
# P3-3 选型结论：DeepFilterNet 质量最高但其 pip 依赖强制 numpy<2.0(会把本环境 numpy 2.2
# 降级、连坐 torch/cv 全家)且无流式状态；RNNoise(Xiph,为 VoIP 而生)10ms 帧级流式、纯 C
# 零显存、实测 1s 音频仅 40ms CPU——通话场景的正确选择。noisereduce 谱减保留为兜底。
# 接入两处：①段级 _denoise16k(分段模式,先门后洗)；②流式路径 StreamSink 帧级洗麦上行——
# 原先流式完全没有降噪(P1-3 只覆盖了分段路径)，这是本次补上的空档。
DENOISE_ENABLE = os.environ.get("INTERP_DENOISE", "1") == "1"
DENOISE_PROP   = float(os.environ.get("INTERP_DENOISE_PROP", "0.75"))  # 谱减强度 0~1(过高会伤语音谐波)
DENOISE_ENGINE = os.environ.get("INTERP_DENOISE_ENGINE", "rnnoise").strip().lower()  # rnnoise/nr
_nr_mod = None
_nr_tried = False
_rnn_mod = None            # pyrnnoise 低层绑定(create/destroy/process_frame)
_rnn_tried = False


def _rnnoise_binding():
    """惰性导入 pyrnnoise 低层帧接口。不可用返回 None(自动落回谱减法)。"""
    global _rnn_mod, _rnn_tried
    if _rnn_mod is None and not _rnn_tried:
        _rnn_tried = True
        if DENOISE_ENGINE == "rnnoise":
            try:
                from pyrnnoise import rnnoise as _rn
                _rnn_mod = _rn
                logger.info("RNNoise 降噪引擎就绪(帧级流式,48k/10ms)")
            except Exception as e:
                logger.warning(f"pyrnnoise 不可用({e})，降噪回退谱减法")
    return _rnn_mod


class _RNNoiseStream:
    """RNNoise 连续流降噪：喂 16k float32 任意长块 → 吐同长(±10ms 缓冲)降噪块。
    内部 16k→48k(RNNoise 固定采样率)→10ms 帧循环→回 16k；状态跨块保持(这正是
    谱减法给不了的：帧级噪声跟踪对键盘敲击等非稳态噪声有效)。"""
    def __init__(self):
        rn = _rnnoise_binding()
        if rn is None:
            raise RuntimeError("rnnoise unavailable")
        self._rn = rn
        self._st = [rn.create()]
        self._buf48 = np.zeros(0, np.float32)
        self.lock = threading.Lock()

    def process16k(self, x: np.ndarray) -> np.ndarray:
        up = _resample(np.asarray(x, np.float32), SR, 48000)
        buf = np.concatenate([self._buf48, up]) if self._buf48.size else up
        n = (len(buf) // 480) * 480
        self._buf48 = buf[n:]
        if n == 0:
            return np.zeros(0, np.float32)
        frames = (np.clip(buf[:n], -1.0, 1.0) * 32767.0).astype(np.int16).reshape(-1, 480)
        outs = []
        for fr in frames:
            den, _prob = self._rn.process_frame(self._st, fr.reshape(1, -1))
            outs.append(np.asarray(den, np.int16).reshape(-1))
        out48 = np.concatenate(outs).astype(np.float32) / 32768.0
        return _resample(out48, 48000, SR)

    def close(self):
        try:
            for s in self._st:
                self._rn.destroy(s)
        except Exception:
            pass
        self._st = []


_rnn_seg = {}              # direction -> _RNNoiseStream(段级路径复用实例,状态跨段延续)


def _denoise16k(x: np.ndarray, direction: str = None) -> np.ndarray:
    """16k 单声道段级降噪。RNNoise 优先(状态按方向持久,跨段跟踪噪声底)，谱减法兜底；
    库缺失/异常一律原样返回(零风险)。"""
    global _nr_mod, _nr_tried
    if not DENOISE_ENABLE or x is None or getattr(x, "size", 0) < 1600:
        return x
    if _rnnoise_binding() is not None:
        try:
            key = direction or "_"
            dn = _rnn_seg.get(key)
            if dn is None:
                dn = _rnn_seg[key] = _RNNoiseStream()
            with dn.lock:                       # 段处理走线程池,同方向并发时串行保护内部状态
                y = dn.process16k(x)
            if y.size >= x.size * 0.8:          # 帧缓冲最多欠 10ms;异常短=失败,回退谱减
                return np.ascontiguousarray(y, dtype=np.float32)
        except Exception:
            logger.exception("RNNoise 段级降噪失败,本段回退谱减法")
    if _nr_mod is None:
        if _nr_tried:
            return x
        _nr_tried = True
        try:
            import noisereduce as _nr
            _nr_mod = _nr
        except Exception:
            logger.warning("noisereduce 不可用，降噪前端停用(不影响主流程)")
            return x
    try:
        y = _nr_mod.reduce_noise(y=x, sr=SR, stationary=True, prop_decrease=DENOISE_PROP)
        return np.ascontiguousarray(y, dtype=np.float32)
    except Exception:
        return x


# ══════════ P1-2 声纹锁（只翻译注册说话人；旁人声/电视声/键盘噪一律不出）══════════
# 模型：本地 CAMPPlus(CosyVoice 自带 campplus.onnx，CPU 10~70ms/段)。
# 注册：默认"自动注册"——开播后最先通过全部门控+幻听过滤的 3 句真话构成你的声纹底座
#       (开播即说话的人=机主，零操作)；也可 /voicelock/enroll 显式录 6s 重注册。
# 判定：段级嵌入 vs 底座余弦相似度。实测同人 0.73+/异人 0.41-，默认门 0.52 余量充足。
# 自适应：高置信命中(≥门+0.15)时以 3% 步长更新底座，跟踪感冒/疲劳等音色漂移。
VOICELOCK_ENABLE     = os.environ.get("INTERP_VOICELOCK", "1") == "1"
VOICELOCK_THR        = float(os.environ.get("INTERP_VOICELOCK_THR", "0.52"))
VOICELOCK_AUTOENROLL = os.environ.get("INTERP_VOICELOCK_AUTOENROLL", "1") == "1"
# 连拒自愈(P6)：底座若注册时混入外放/旁人声(实测 2026-07-07 23:52 电视声成底座,机主
# 全程 sim≈0.25 被拦到天亮)，"连续 N 段全拒且零放行"在机主麦上几乎只有一种解释=底座失真。
# 达到阈值→隔离旧底座(.bad.npz 留档)重新自动注册。最坏损失 N 句后自愈，永不"整场哑巴"。
VOICELOCK_SUSPECT_N  = int(os.environ.get("INTERP_VOICELOCK_SUSPECT_N", "5"))
# 影子模式(P-Silence)：底座失真时"降级可用"而非"静默不可用"。同一会话零放行且短窗内连拒≥N
# → "底座对不上机主"(换麦/增益漂移/注册污染)的概率远大于"旁人恰好连讲 N 句"——继续硬拦
# =整场哑播(2026-07-15 10:33 事故：机主 sim 0.12~0.40 全拦,CABLE 无声 237s,连拒自愈因
# 历史 accepts>0 门槛升到 3N 永远差一步)。触发后：
#   通话模式 = 放行但打标 + 用影子期自洽真话后台重建底座(一致性门同自动注册,成座即替换退出影子)；
#   直播无人值守 = 只告警不放行(防主播离开时电视/旁人声被翻译给观众,重演底座污染事故)，
#                  INTERP_VOICELOCK_SHADOW_LIVE=1 可显式放开。
# 任一真实放行(sim≥门)说明底座没坏 → 立即退出影子恢复拦截。
VOICELOCK_SHADOW       = os.environ.get("INTERP_VOICELOCK_SHADOW", "1") == "1"
VOICELOCK_SHADOW_LIVE  = os.environ.get("INTERP_VOICELOCK_SHADOW_LIVE", "0") == "1"
# N=5/120s 按事故真实节奏标定：用户排障时几分钟就重启一次会话,证据窗跨"同麦快速重启"保留
# (换采集设备才清零),否则每次重启清零永远凑不满。
VOICELOCK_SHADOW_N     = int(os.environ.get("INTERP_VOICELOCK_SHADOW_N", "5"))
VOICELOCK_SHADOW_WIN_S = float(os.environ.get("INTERP_VOICELOCK_SHADOW_WIN", "120"))
# 底座健康度：距上次注册/更新过久、或换了采集设备 → 开播时主动提示重注册(防患于未然)。
VOICELOCK_BASE_AGE_H   = float(os.environ.get("INTERP_VOICELOCK_BASE_AGE_H", "72"))
VOICELOCK_MODEL      = os.environ.get("CAMPPLUS_ONNX",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "CosyVoice", "pretrained_models", "Fun-CosyVoice3-0.5B", "campplus.onnx"))
_VL_PERSIST = os.path.join(REF_DIR, "voicelock_owner.npz")


class _VoiceLock:
    """说话人验证：懒加载(torch/onnxruntime 后台预热)，底座持久化跨会话复用。"""
    def __init__(self):
        self.lock = threading.Lock()
        self.sess = None            # onnxruntime session
        self._kaldi = None          # torchaudio.compliance.kaldi
        self._torch = None
        self.available = None       # None=未尝试 / True/False
        self.centroid = None        # 归一化声纹底座(np.float32)
        self.n_enrolled = 0
        self.pending = []           # 自动注册累积中的嵌入
        self.last_sim = None
        self.accepts = 0
        self.rejects = 0
        self._adapt_n = 0
        self._streak_rej = 0        # 连续拒绝计数(连拒自愈用,任一放行即清零)
        # P-Silence 影子模式态(会话级)
        self.sess_accepts = 0       # 本会话真实放行数(影子触发前提：必须为 0)
        self._rej_win = deque(maxlen=16)  # 连拒证据窗[{ts,emb,n}](任一真实放行清空;嵌入供自洽判别+重建种子)
        self.shadow_on = False
        self.shadow_since = 0.0
        self.shadow_passes = 0      # 影子期放行(未验证)段数
        self._shadow_pool = []      # 影子期重建底座的候选嵌入(一致性门同自动注册)
        self._shadow_live_warned = False  # 直播模式影子被抑制时的单次提示闸
        self.base_meta = {}         # 底座元数据 {ts, mic}(健康度体检用)
        self.session_mic = ""       # 本会话麦名(注册时随底座持久化,供换设备检测)
        self._load_persisted()

    def _load_persisted(self):
        try:
            if os.path.exists(_VL_PERSIST):
                d = np.load(_VL_PERSIST)
                c = np.asarray(d["emb"], dtype=np.float32)
                self.centroid = c / (np.linalg.norm(c) + 1e-9)
                self.n_enrolled = int(d.get("n", 1))
                try:                     # 底座元数据(旧档无 mic 字段,ts 缺失回退文件时间)
                    _ts = float(d["ts"]) if "ts" in d else os.path.getmtime(_VL_PERSIST)
                except Exception:
                    _ts = 0.0
                try:
                    _mic = str(d["mic"]) if "mic" in d else ""
                except Exception:
                    _mic = ""
                self.base_meta = {"ts": _ts, "mic": _mic}
                logger.info(f"声纹锁：已加载持久化底座(样本 {self.n_enrolled} 段)")
        except Exception:
            logger.exception("声纹底座加载失败，将重新注册")

    def _persist(self):
        try:
            os.makedirs(REF_DIR, exist_ok=True)
            mic = self.session_mic or (self.base_meta.get("mic") if self.base_meta else "") or ""
            np.savez(_VL_PERSIST, emb=self.centroid, n=self.n_enrolled, ts=time.time(), mic=mic)
            self.base_meta = {"ts": time.time(), "mic": mic}
        except Exception:
            logger.exception("声纹底座持久化失败")

    def _ensure_loaded(self) -> bool:
        """加载 onnx + kaldi fbank(首次数秒，故有 warmup_async 后台预热)。"""
        if self.available is not None:
            return self.available
        with self.lock:
            if self.available is not None:
                return self.available
            try:
                import onnxruntime as ort
                import torch as _t
                import torchaudio.compliance.kaldi as _k
                self.sess = ort.InferenceSession(VOICELOCK_MODEL, providers=["CPUExecutionProvider"])
                self._inp = self.sess.get_inputs()[0].name
                self._torch, self._kaldi = _t, _k
                self.available = True
                logger.info(f"声纹锁模型就绪: {os.path.basename(VOICELOCK_MODEL)}")
            except Exception as e:
                self.available = False
                logger.warning(f"声纹锁模型不可用({e})，本功能自动停用")
        return self.available

    def warmup_async(self):
        if VOICELOCK_ENABLE and self.available is None:
            threading.Thread(target=self._ensure_loaded, daemon=True).start()

    def embed(self, wav16k: np.ndarray):
        """16k 单声道 → 归一化 192 维声纹。失败返回 None。"""
        if not self._ensure_loaded():
            return None
        try:
            x = self._torch.from_numpy(np.ascontiguousarray(wav16k)).float().unsqueeze(0)
            feat = self._kaldi.fbank(x, num_mel_bins=80, dither=0, sample_frequency=16000)
            feat = feat - feat.mean(dim=0, keepdim=True)
            with self.lock:                      # onnx session 串行使用
                e = self.sess.run(None, {self._inp: feat.unsqueeze(0).numpy()})[0][0]
            return e / (np.linalg.norm(e) + 1e-9)
        except Exception:
            logger.exception("声纹嵌入失败")
            return None

    def ready(self) -> bool:
        return VOICELOCK_ENABLE and self.centroid is not None

    def check(self, wav16k: np.ndarray):
        """(是否本人, 相似度)。未注册/模型不可用 → 一律放行。
        P2 滑动窗投票：>4s 长句取首/中/尾 3 个 3s 窗分别打分取最高——
        长句里混入短暂环境声(键盘/远处人声)时整句嵌入被稀释，单窗会误拒真人；
        取窗最高分只要有一窗是本人就放行(段已过мик门控,假阳风险远小于误拒真话的伤害)。"""
        if not self.ready():
            return True, None
        n = int(wav16k.shape[0])
        win = 16000 * 3
        if n <= win + 16000:                     # ≤4s：单窗(原行为，零额外开销)
            e = self.embed(wav16k)
            if e is None:
                return True, None
            sim, best_e = float(np.dot(self.centroid, e)), e
        else:
            sims, embs = [], []
            for s0 in (0, max(0, (n - win) // 2), max(0, n - win)):
                e = self.embed(wav16k[s0:s0 + win])
                if e is not None:
                    sims.append(float(np.dot(self.centroid, e)))
                    embs.append(e)
            if not sims:
                return True, None
            i = int(np.argmax(sims))
            sim, best_e = sims[i], embs[i]
        self.last_sim = round(sim, 3)
        if sim >= VOICELOCK_THR:
            self.accepts += 1
            self.sess_accepts += 1
            self._streak_rej = 0
            self._rej_win.clear()
            if self.shadow_on:               # 真实命中=底座没坏,影子是误会 → 立即恢复正常拦截
                self._shadow_exit("底座重新命中,拦截恢复")
            if sim >= VOICELOCK_THR + 0.15:      # 高置信命中→小步自适应，跟踪音色漂移
                c = 0.97 * self.centroid + 0.03 * best_e
                self.centroid = c / (np.linalg.norm(c) + 1e-9)
                self._adapt_n += 1
                if self._adapt_n % 20 == 0:
                    self._persist()
            return True, sim
        # ── 未过门 ──
        if self.shadow_on:                   # 影子期：放行但打标(计数冻结当证据),并喂后台重建
            self.shadow_passes += 1
            logger.info(f"[声纹锁·影子] 放行未验证段 sim={sim:.3f} (累计 {self.shadow_passes})")
            self._shadow_relearn(best_e, int(wav16k.shape[0]))
            return True, sim
        self.rejects += 1
        self._streak_rej += 1
        self._rej_win.append({"ts": time.time(), "emb": best_e, "n": int(wav16k.shape[0])})
        # 连拒自愈：机主麦上连拒 N 段且本进程从未放行过任何人 → 底座本身失真(注册被污染/
        # 换麦换人)概率远大于"N 句全是旁人"，隔离重注册。曾有放行(底座至少匹配过机主)则
        # 把门槛提到 3N——覆盖中途换麦/嗓音剧变，又不至于被旁人连说几句就顶掉好底座。
        # 本段大概率就是机主,放行并直接充当重注册第一句(≥1.2s 才算,与自动注册口径一致)。
        if self._streak_rej >= (VOICELOCK_SUSPECT_N if self.accepts == 0 else 3 * VOICELOCK_SUSPECT_N):
            self._quarantine()
            if wav16k.shape[0] >= 16000 * 1.2:
                try:
                    self.enroll_from(wav16k)
                except Exception:
                    pass
            return True, sim
        # P-Silence 影子触发：长寿进程里 accepts>0 让上面 3N 门槛几乎够不着(本次事故实证)，
        # 会话级"零放行+短窗连拒"是更贴近现场的失真证据。触发时先拿证据窗里被拦的段当场
        # 试重建(它们大概率就是机主的话)——自洽即秒愈,不自洽再进影子边放行边等。
        if self._shadow_should_trigger():
            # 证据窗里被拦的段(含本段)当场试重建——自洽即秒愈;不自洽(混源)带着清洗后的
            # 池子进影子,后续真话继续凑(池子不清零,少等 2~3 句)
            self._shadow_pool = [r["emb"] for r in self._rej_win
                                 if r["emb"] is not None and r["n"] >= int(16000 * 1.2)][-4:]
            if self._shadow_try_rebuild():
                return True, sim             # 底座已当场重建,本段放行,下一句起正常拦截
            self._shadow_enter(sim)
            self.shadow_passes += 1
            return True, sim
        return False, sim

    # ── P-Silence 影子模式：底座失真时降级可用,绝不整场哑播 ──────────────
    def _shadow_should_trigger(self) -> bool:
        """会话零放行 + 短窗连拒≥N → 底座疑似失真。直播无人值守默认不放行(防旁人声出街)。"""
        if not VOICELOCK_SHADOW or self.shadow_on or self.sess_accepts > 0:
            return False
        now = time.time()
        recent = [r["ts"] for r in self._rej_win if now - r["ts"] <= VOICELOCK_SHADOW_WIN_S]
        if len(recent) < VOICELOCK_SHADOW_N:
            return False
        if getattr(ST, "live_mode", False) and not VOICELOCK_SHADOW_LIVE:
            if not self._shadow_live_warned:     # 直播模式:不自动放行,但要把话说明白(单次)
                self._shadow_live_warned = True
                logger.warning(f"[声纹锁] 会话零放行且 {VOICELOCK_SHADOW_WIN_S:.0f}s 内连拒 "
                               f"{len(recent)} 段,底座疑似失真;直播模式不自动放行(防旁人声出街),请人工处理")
                try:
                    ST.push_event({"who": "sys", "warn":
                                   "⚠ 声纹锁连续拦截且零放行：底座疑似失真。直播模式不自动放行，"
                                   "请点「重置声纹」后正常说 3 句重新注册"})
                except Exception:
                    pass
            return False
        return True

    def _shadow_enter(self, sim):
        self.shadow_on = True
        self.shadow_since = time.time()
        # 注意:不清 _shadow_pool——触发路径已播种"清洗过的证据段",清掉会白等 2~3 句
        logger.warning(f"[声纹锁] 进入影子模式：本会话零放行且 {VOICELOCK_SHADOW_WIN_S:.0f}s 内"
                       f"连拒≥{VOICELOCK_SHADOW_N} 段(最近 sim={sim:.3f}<{VOICELOCK_THR})——"
                       "底座疑似失真。改为放行+打标,用影子期自洽真话后台重建底座")
        try:
            ST.push_event({"who": "sys", "warn":
                           "🟡 声纹锁已切影子模式：底座疑似失真(连续拦截且零放行)。已临时放行保出声；"
                           "正常说几句话将自动重建声纹，或点「重置声纹」立即重录"})
        except Exception:
            pass

    def _shadow_exit(self, why: str):
        self.shadow_on = False
        self._shadow_pool = []
        logger.info(f"[声纹锁] 恢复正常拦截：{why}")
        try:
            ST.push_event({"who": "sys", "warn": f"🔒 声纹锁已恢复正常拦截：{why}"})
        except Exception:
            pass

    def _shadow_relearn(self, emb, n_samples: int):
        """影子期后台重建底座：≥1.2s 的放行段进候选池(滚动 ≤4)，凑满即试重建。"""
        if emb is None or n_samples < int(16000 * 1.2):
            return
        self._shadow_pool.append(emb)
        self._shadow_pool = self._shadow_pool[-4:]
        self._shadow_try_rebuild()

    def _shadow_try_rebuild(self) -> bool:
        """候选池 3 段两两自洽(≥0.50,同自动注册口径)→ 归档旧座 .bad、换新座、退出影子。
        混源(电视/旁人插话)过不了一致性门:剔最不合群的一段继续等。返回是否已重建。"""
        try:
            if len(self._shadow_pool) < 3:
                return False
            E = np.stack(self._shadow_pool)
            sims = E @ E.T
            np.fill_diagonal(sims, 1.0)
            if float(sims.min()) < 0.50:         # 有段与其他段对不上→混源,剔最不合群的继续等
                avg = (sims.sum(axis=1) - 1.0) / max(1, E.shape[0] - 1)
                self._shadow_pool.pop(int(np.argmin(avg)))
                return False
            try:
                if os.path.exists(_VL_PERSIST):
                    os.replace(_VL_PERSIST, _VL_PERSIST + ".bad")
            except OSError:
                pass
            c = E.mean(axis=0)
            self.centroid = (c / (np.linalg.norm(c) + 1e-9)).astype(np.float32)
            self.n_enrolled = int(E.shape[0])
            self._persist()
            self._streak_rej = 0
            self._rej_win.clear()
            self._shadow_exit(f"已用被拦的 {E.shape[0]} 段自洽真话重建底座(旧座归档 .bad)")
            return True
        except Exception:
            logger.exception("影子模式重建底座失败(不影响放行)")
            return False

    def on_session_start(self, mic_name: str = ""):
        """会话开始钩子：会话级计数/影子态清零 + 底座健康度体检(过老/换采集设备→开播即提示)。
        连拒证据窗跨"同麦重启"保留：排障时用户几分钟就重启一次会话(2026-07-15 实录 6 分钟
        重启 3 次)，若每次清窗则影子永远凑不满 N 句——重演连拒自愈 3N 门槛够不着的老毛病。
        换了采集设备(证据环境变了)才清零。"""
        new_mic = (mic_name or "").strip()
        if new_mic and new_mic != self.session_mic:
            self._rej_win.clear()            # 换麦=旧证据作废;同麦重启保留(时间窗自然淘汰过期证据)
        self.session_mic = new_mic
        self.sess_accepts = 0
        self.shadow_passes = 0
        self._shadow_pool = []
        self._shadow_live_warned = False
        if self.shadow_on:
            self.shadow_on = False           # 新会话回正常态(证据窗还在,再失真立刻再触发)
        if self.centroid is None:
            return
        tips = []
        ts = float((self.base_meta or {}).get("ts") or 0.0)
        if not ts:
            try:
                ts = os.path.getmtime(_VL_PERSIST)
            except OSError:
                ts = 0.0
        if ts and (time.time() - ts) / 3600.0 >= VOICELOCK_BASE_AGE_H:
            tips.append(f"距上次注册/更新已 {(time.time() - ts) / 86400.0:.1f} 天")
        bm = ((self.base_meta or {}).get("mic") or "").strip()
        if bm and self.session_mic and bm != self.session_mic:
            tips.append(f"注册时用麦「{bm}」·本场是「{self.session_mic}」")
        if tips:
            logger.info(f"[声纹锁] 底座健康提示: {'；'.join(tips)}")
            try:
                ST.push_event({"who": "sys", "warn":
                               "ℹ 声纹底座健康提示：" + "；".join(tips) +
                               "。若开口被拦，请点「重置声纹」说 3 句话重新注册"})
            except Exception:
                pass

    def _quarantine(self):
        """疑似污染底座 → 移到 .bad 留档(可事后取证)，回未注册态等真话重新自动注册。"""
        try:
            if os.path.exists(_VL_PERSIST):
                os.replace(_VL_PERSIST, _VL_PERSIST + ".bad")
        except OSError:
            pass
        self.centroid = None; self.n_enrolled = 0; self.pending = []
        self._streak_rej = 0
        logger.warning(f"[声纹锁] 连拒自愈触发：连续 {VOICELOCK_SUSPECT_N} 段全拒且零放行，"
                       "底座疑似注册时被污染(外放/旁人声成底座)。已隔离旧底座,重新自动注册")
        try:
            ST.push_event({"who": "sys", "warn": "🔓 声纹底座疑似失真已自动重置：请正常说 3 句话完成重新注册"})
        except Exception:
            pass

    def enroll_from(self, wav16k: np.ndarray, need: int = 3) -> bool:
        """自动注册：累积一段真话声纹；凑满 need 段且「彼此是同一人」→ 生成底座并持久化。
        一致性门(P2 加固)：环境里放着电视/电影时,过门控的段来自多个说话人+配乐,直接平均会
        注册出"谁都像"的垃圾底座(实测 accepts 全放行)。同一真人连说 need 句的两两相似度
        天然 ≥0.6,混源段则显著更低 → 每次凑满先剔除离群段,直到 need 段全体自洽才成底座。"""
        e = self.embed(wav16k)
        if e is None:
            return False
        self.pending.append(e)
        logger.info(f"声纹自动注册进度 {len(self.pending)}/{need}")
        if len(self.pending) >= need:
            E = np.stack(self.pending)
            sims = E @ E.T                                   # 两两余弦(已归一化)
            np.fill_diagonal(sims, 1.0)
            avg = (sims.sum(axis=1) - 1.0) / max(1, len(self.pending) - 1)
            if float(sims.min()) < 0.50:                     # 有段与其他人对不上→混源
                drop = int(np.argmin(avg))
                logger.info(f"声纹自动注册：段间相似度过低(min={sims.min():.2f})，"
                            f"剔除离群段#{drop}(avg={avg[drop]:.2f})继续等真话")
                self.pending.pop(drop)
                return False
            c = E.mean(axis=0)
            self.centroid = (c / (np.linalg.norm(c) + 1e-9)).astype(np.float32)
            self.n_enrolled = len(self.pending)
            self.pending = []
            self._persist()
            return True
        return False

    def enroll_direct(self, wav16k: np.ndarray) -> bool:
        """显式注册：一次 6s 录音直接成底座(覆盖旧底座)。"""
        e = self.embed(wav16k)
        if e is None:
            return False
        self.centroid = e.astype(np.float32)
        self.n_enrolled = 1
        self.pending = []
        self._persist()
        return True

    def affirm_learn(self, wav16k: np.ndarray) -> bool:
        """P-Affirm 用户点「是我」的强学习：人工确认可信度远高于自适应(3%)，以 20% 权重
        并入底座并立即持久化；未注册则当自动注册素材。同时清连拒证据(用户已裁决)。"""
        e = self.embed(wav16k)
        if e is None:
            return False
        if self.centroid is None:
            try:
                return self.enroll_from(wav16k)
            except Exception:
                return False
        c = 0.8 * self.centroid + 0.2 * e
        self.centroid = (c / (np.linalg.norm(c) + 1e-9)).astype(np.float32)
        self._persist()
        self._streak_rej = 0
        self._rej_win.clear()
        if self.shadow_on:
            self._shadow_exit("用户确认本人并已学习,底座已校正")
        logger.info("[声纹锁] 用户放行学习：本句已并入底座(权重20%)并持久化")
        return True

    def reset(self):
        self.centroid = None; self.n_enrolled = 0; self.pending = []
        self.last_sim = None; self.accepts = 0; self.rejects = 0; self._streak_rej = 0
        self.sess_accepts = 0; self._rej_win.clear()
        self.shadow_on = False; self.shadow_passes = 0; self._shadow_pool = []
        self._shadow_live_warned = False; self.base_meta = {}
        try:
            os.remove(_VL_PERSIST)
        except OSError:
            pass

    def brief(self) -> dict:
        return {"enabled": VOICELOCK_ENABLE, "model_ok": self.available,
                "enrolled": self.centroid is not None, "n": self.n_enrolled,
                "pending": len(self.pending), "thr": VOICELOCK_THR,
                "last_sim": self.last_sim, "accepts": self.accepts, "rejects": self.rejects,
                # P-Silence 影子/健康度观测(vlPill + hub 归因共用)
                "sess_accepts": self.sess_accepts,
                "shadow": self.shadow_on, "shadow_passes": self.shadow_passes,
                "base_ts": (self.base_meta or {}).get("ts"),
                "base_mic": (self.base_meta or {}).get("mic")}


_voicelock = _VoiceLock()


def _prewarm_ref(voice_b64: str, ref_text: str):
    """会话启动/切角色时把参考音预热进「首选引擎」缓存 → 消除首句参考编码冷启动惩罚。
    按引擎分流(此前一律 POST /v1/refs/prewarm,cosyvoice 没这端点→常年 404 噪音、预热落空)：
    fish/qwen3 → /v1/refs/prewarm；cosyvoice → /v1/tts/register_spk(zero-shot spk 缓存,
    与 clone/stream 同一命中键)；sbv2 → 训练模型无参考音,跳过。失败仅记日志、不影响启动。"""
    if not voice_b64:
        return
    eng = _resolve_tts_engine()
    if eng == "sbv2":
        return
    bu = _tts_url_for(eng)
    try:
        if eng == "cosyvoice":
            r = requests.post(f"{bu}/v1/tts/register_spk",
                              json={"reference_audio_b64": voice_b64}, timeout=30)
        else:
            r = requests.post(f"{bu}/v1/refs/prewarm",
                              json={"references": [{"audio_b64": voice_b64, "text": ref_text}]},
                              timeout=30)
        if r.ok and r.json().get("ok"):
            logger.info(f"参考音预热完成@{eng}(首句将跳过参考编码)")
        else:
            logger.warning(f"参考音预热未成功@{eng}: {r.status_code} {r.text[:120]}")
    except Exception:
        logger.exception(f"参考音预热失败@{eng}(不影响启动)")


def _warmup_lipsync(face_id: str):
    """直播口型热启动:用 0.6s 静音驱动一次 generate_stream(不推 vcam),触发 UNet/datagen 的
    首次按尺寸编译(冷态实测可达数秒)。预热后首句段间隔回落到实时区间,消除开播第一句卡顿。"""
    if not face_id:
        return
    try:
        # 2.5s 静音:段数(~4-6)接近真实句,覆盖 UNet 按序列长度的全部编译路径。
        # 实测 0.6s(2段)只覆盖部分→首句仍 ~4s;2.5s→首句即 ~380ms(实时区间)。
        silent = (np.zeros(int(SR * 2.5), dtype=np.float32))
        wav = _to_wav_bytes(silent, SR)
        t = time.time()
        r = requests.post(f"{LIPSYNC_URL}/lipsync/generate_stream",
                          files={"audio": ("warm.wav", wav, "audio/wav")},
                          data={"face_id": face_id, "fps": "25",
                                "first_seg_frames": str(FIRST_SEG_FRAMES),
                                "seg_frames": "25", "push_segs": "false"}, timeout=(3.05, 180))
        if r.ok:
            ST.warm_ms = int((time.time() - t) * 1000)
            logger.info(f"口型热启动完成 ({ST.warm_ms}ms，首句将走热态)")
            ST.push_event({"who": "sys", "warn": "✅ 口型引擎已热启动，首句即时出画"})
        else:
            logger.warning(f"口型热启动失败 {r.status_code}: {r.text[:120]}")
    except Exception:
        logger.exception("口型热启动异常(不影响启动)")


def _prewarm_llm():
    """LLM(ollama) 会话预热：把模型提前拉进显存并驻留(keep_alive)。
    翻译(口语化语向)/GER 终稿复核/情感基调三处共用同一本地后端——冷加载若发生在第一句
    正稿上,单工人串行管线会被逐句放大成分钟级积压(2026-07-15 22:55 实测:冷加载撞上
    显存挤兑,e2e 5.7s→28s→59s→66s)。预热放在会话启动后台,与 NMT/参考音热身并行。"""
    if not (_MT_BACKEND in ("auto", "llm") or _MT_LLM_LANGS or _GER_ON):
        return
    try:
        t = time.time()
        r = requests.post(f"{_LLM_URL}/api/chat",
                          json={"model": _LLM_MODEL,
                                "messages": [{"role": "user", "content": "hi"}],
                                "stream": False, "keep_alive": _LLM_KEEP,
                                "options": {"num_predict": 1, "num_ctx": _LLM_NUMCTX}},
                          timeout=120)
        if r.ok:
            logger.info(f"LLM 预热完成({_LLM_MODEL} 已驻卡, {(time.time() - t) * 1000:.0f}ms)")
        else:
            logger.warning(f"LLM 预热失败 {r.status_code}(不影响开播;冷加载风险留给首句,或将触发熔断回退 NMT)")
    except Exception as e:
        logger.warning(f"LLM 预热不可达({str(e)[:80]})——本场翻译/复核将按熔断机制回退")


def _warmup_session(voice_b64: str, ref_text: str, face_bytes: bytes = b"", face_id: str = "",
                    stream: bool = False):
    """会话启动后台热身:① 预热 Fish 参考编码;② 短 ASR(+NMT)预热 STT;③ 直播模式预计算人脸+口型引擎。
    消除首句/首帧冷启动。Fish 参考预热与人脸/口型预热并行(分属不同 GPU 服务,互不争用),整体就绪更快。
    全程后台、失败仅记日志、不影响启动。
    stream=True(流式逐词):Whisper 不参与转写,跳过其热身(否则会把刚卸载的 Whisper 重新拉起,白占显存)。"""
    # Fish 参考预热与口型链路预热分属不同服务(Fish vs lipsync)→ 并行不互斥,缩短整体就绪时间
    ref_th = threading.Thread(target=_prewarm_ref, args=(voice_b64, ref_text), daemon=True)
    ref_th.start()
    threading.Thread(target=_prewarm_llm, daemon=True).start()   # LLM 驻卡预热(翻译/GER/情感共用)
    if face_bytes and face_id:               # 直播模式:先把人脸预热好(冷脸~28s,命中磁盘缓存秒回)
        if _precompute_face(face_bytes, face_id):
            _warmup_lipsync(face_id)         # 人脸就绪后预热 UNet 编译(消除首句段间隔尖峰)
    ref_th.join(timeout=60)
    try:
        t = time.time()
        if stream:
            _translate_nmt("你好", _SRC_LANG, _DST_LANG)          # 流式:只热身译路(跟随配置语向),不碰 Whisper(已卸载省显存)
            if _DST_LANG != _SRC_LANG:
                _translate_nmt("hello", _DST_LANG, _SRC_LANG)     # 方向B(对方→我)反向译路也预热,消除对方首句冷启动
            logger.info(f"NMT 热身完成 ({(time.time()-t)*1000:.0f}ms，语向 {_SRC_LANG}⇄{_DST_LANG}，流式跳过 Whisper 热身)")
        else:
            if voice_b64:
                data, sr = _wav_bytes_to_f32(base64.b64decode(voice_b64))
                clip = _resample(data[:int(sr * 0.6)], sr, SR)   # 取 0.6s 参考切片当热身输入
            else:
                clip = (np.random.randn(int(SR * 0.4)) * 0.01).astype(np.float32)
            zh = _stt(clip, _SRC_LANG, task="transcribe")        # 用配置的我方语言热身 STT
            _translate_nmt(zh or "你好", _SRC_LANG, _DST_LANG)    # 热身 我方→对方 译路
            if _DST_LANG != _SRC_LANG:
                _translate_nmt("hello", _DST_LANG, _SRC_LANG)     # 方向B 反向译路也预热
            logger.info(f"STT/NMT 热身完成 ({(time.time()-t)*1000:.0f}ms，语向 {_SRC_LANG}⇄{_DST_LANG}，首句将走热态)")
    except Exception:
        logger.exception("STT 热身失败(不影响启动)")


_SYNTH_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="synth")  # 分句次块并行合成


def _synth_en(text_en: str, base_url: str = None) -> str:
    """英文文本 → 克隆音色 TTS → 音频 b64(WAV)。base_url 缺省用首选引擎并 Fish 兜底；
    全部候选失败才抛错(保持原契约：上层据异常降级/兜底)。engine=fish 时逐字节等价旧行为。"""
    if not text_en.strip():
        return ""
    # 固定 seed:实测可显著降低 AR 采样抖动(尾延迟 -48%、CV 38%→8%)，
    # 音色仍由参考音决定，仅固定采样路径 → 逐句配音延迟更稳。
    # language 跟随语向配置(P3-5 多语向：日/韩/俄等,原先硬编码 en 会误导 TTS 文本前端)。
    payload = {"text": text_en, "language": _DST_LANG, "return_base64": True,
               "temperature": 0.7, "top_p": 0.7, "repetition_penalty": 1.2, "seed": 42}
    if ST.voice_b64:
        payload["reference_audio_b64"] = ST.voice_b64
        payload["reference_text"] = ST.ref_text
    urls = [base_url] if base_url else _tts_urls()
    last_err = None
    for i, bu in enumerate(urls):
        try:
            r = _HTTP_POOL.post(f"{bu}/v1/tts/clone", json=payload, timeout=120)
            r.raise_for_status()
            return r.json().get("audio_base64", "")
        except Exception as e:
            last_err = e
            if i < len(urls) - 1:
                logger.warning(f"clone TTS @{bu} 失败，回退下一候选: {e}")
    raise last_err


def _b64bytes(s: str) -> bytes:
    """base64(可能带 data: 前缀) → 原始字节。"""
    if not s:
        return b""
    try:
        return base64.b64decode(s.split(",", 1)[-1] if "," in s else s)
    except Exception:
        return b""


# lipsync 预计算熔断(P-Silence 附带修复)：远端不可达时旧代码 connect 要等满 180s 才失败，
# 一场会话 3 次预热=9 分钟线程占用+每次 40 行堆栈刷屏(2026-07-15 实录)。connect 3s 快速失败；
# 连续 3 次连接失败 → 熔断 300s 只跳过预计算(直播口型自身另有 heal 降级,不受影响)，恢复自动重试。
_LS_CB = {"fails": 0, "open_until": 0.0, "lock": threading.Lock()}


def _ls_cb_allow() -> bool:
    with _LS_CB["lock"]:
        return time.time() >= _LS_CB["open_until"]


def _ls_cb_report(ok: bool):
    with _LS_CB["lock"]:
        if ok:
            _LS_CB["fails"] = 0
            return
        _LS_CB["fails"] += 1
        if _LS_CB["fails"] >= 3 and time.time() >= _LS_CB["open_until"]:
            _LS_CB["open_until"] = time.time() + 300
            logger.warning(f"[lipsync] 连续 {_LS_CB['fails']} 次连接失败 → 人脸预计算熔断 300s"
                           f"(期间直接跳过,期满自动重试): {LIPSYNC_URL}")


def _precompute_face_call(face_bytes: bytes, face_id: str) -> bool:
    """仅把人脸预计算进 lipsync 缓存(不改 ST 状态)。供首启预热与运行中切角色复用。
    connect 3s 快速失败+熔断：lipsync 掉线只损失预热，不再长时间阻塞线程/刷整段堆栈。"""
    if not face_bytes or not face_id:
        return False
    if not _ls_cb_allow():
        logger.info(f"人脸预计算跳过(lipsync 熔断开启中) face_id={face_id}")
        return False
    try:
        t = time.time()
        r = requests.post(f"{LIPSYNC_URL}/lipsync/precompute_face",
                          files={"face": ("f.jpg", face_bytes, "image/jpeg")},
                          data={"face_id": face_id}, timeout=(3.05, 180))
        _ls_cb_report(True)
        if r.ok:
            logger.info(f"人脸预计算完成 face_id={face_id} ({(time.time()-t)*1000:.0f}ms)")
        else:
            logger.warning(f"人脸预计算失败 {r.status_code}: {r.text[:120]}")
        return r.ok
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        _ls_cb_report(False)
        logger.warning(f"人脸预计算不可达({e.__class__.__name__}): {str(e)[:140]}")
        return False
    except Exception:
        logger.exception("人脸预计算异常")
        return False


def _precompute_face(face_bytes: bytes, face_id: str) -> bool:
    """首启预热:预计算人脸并置 ST.face_ready(直播首句即热)。"""
    ok = _precompute_face_call(face_bytes, face_id)
    if ok:
        ST.face_id = face_id
        ST.face_ready = True
        ST.push_event({"who": "sys", "warn": "🎭 数字人形象已就绪"})
        ST.push_event({"who": "sys", "live_ready": True})
    return ok


def _push_subtitle(l1: str, l2: str = "", ttl: float = 6.0, slot: str = "bottom"):
    """推直播字幕到 vcam 叠加层(失败静默，不阻断口型驱动)。slot: bottom=我说 / top=对方说。"""
    try:
        requests.post(f"{VCAM_URL}/subtitle",
                      json={"line1": l1, "line2": l2, "ttl": ttl, "slot": slot}, timeout=2)
    except Exception:
        pass


def _clear_subtitles():
    """清除 vcam 上下字幕(会话结束)。"""
    for slot in ("bottom", "top"):
        try:
            requests.post(f"{VCAM_URL}/subtitle",
                          json={"line1": "", "ttl": 0, "slot": slot}, timeout=2)
        except Exception:
            pass


def _set_vcam_notice(text: str):
    """设置/清除 vcam 角标(降级时显示「配音模式」,让观众侧明确感知,非冻结旧帧)。"""
    try:
        requests.post(f"{VCAM_URL}/notice", json={"text": text}, timeout=2)
    except Exception:
        pass


def _vcam_return_idle():
    """让 vcam 立即丢弃当前段回待机(降级瞬间用,避免停在半张嘴死帧)。"""
    try:
        requests.post(f"{VCAM_URL}/return_idle", timeout=2)
    except Exception:
        pass


def _enter_degrade(notice: str, warn: str):
    """统一降级入场动作:置角标 + 立即回待机帧(不停在半张嘴死帧) + 节流告警。"""
    _set_vcam_notice(notice)
    _vcam_return_idle()
    ST.note_degrade(warn)


def _exit_degrade():
    """统一恢复动作:清角标 + 告警 + 置复检探针(恢复后首句复测 A/V 是否回到实时区间)。"""
    _set_vcam_notice("")
    ST._post_recover_probe = True
    ST.push_event({"who": "sys", "warn": "✅ 口型/广播服务已恢复，切回数字人"})


def _report_av_probe(av: dict, label: str):
    """A/V 对齐采样:首帧/段间隔达标(实时区间)与否上报观测。label 区分切换/恢复场景。"""
    ttfv = av.get("ttfv_ms"); gap = av.get("seg_gap_ms")
    ok = (gap is None or gap <= 1250)            # 段间隔 ≤1.25×段时长(1s) 视为实时达标
    tag = "✅ 达标" if ok else "⚠ 偏慢"
    parts = []
    if ttfv is not None:
        parts.append(f"首帧 {ttfv}ms")
    if gap is not None:
        parts.append(f"段间隔 {gap}ms")
    ST.push_event({"who": "sys", "warn": f"🎯 {label} {tag}" + ("（" + "，".join(parts) + "）" if parts else "")})


class _SubDebouncer:
    """对方/润色字幕防抖:短停顿内多次更新合并为一次推送,减少闪烁。"""
    def __init__(self):
        self._lock = threading.Lock()
        self._pending = None
        self._timer = None

    def push(self, slot: str, l1: str, l2: str, ttl: float, immediate: bool = False):
        with self._lock:
            self._pending = (slot, l1, l2, ttl)
            if self._timer:
                self._timer.cancel(); self._timer = None
            if immediate:
                self._flush_locked()
                return
            self._timer = threading.Timer(SUB_DEBOUNCE_SEC, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def flush(self):
        with self._lock:
            self._flush_locked()

    def _flush(self):
        with self._lock:
            self._flush_locked()

    def _flush_locked(self):
        p = self._pending
        self._pending = None
        if self._timer:
            self._timer.cancel(); self._timer = None
        if p:
            _push_subtitle(p[1], p[2], p[3], p[0])

    def cancel(self):
        with self._lock:
            if self._timer:
                self._timer.cancel(); self._timer = None
            self._pending = None


_sub_debouncer = _SubDebouncer()


def _sub_ttl_en(en: str) -> float:
    return max(3.0, len((en or "").split()) * 0.42 + 1.8)


def _ensure_live_profile(profile: str, face_bytes: bytes):
    """直播一键联动:Hub 激活角色(含 vcam 待机 + lipsync 预计算);失败则直推 vcam /set_idle。"""
    nm = profile
    if not nm:
        try:
            nm = requests.get(f"{HUB_URL}/profiles", timeout=5).json().get("active", "")
        except Exception:
            nm = ""
    if not nm:
        return
    try:
        r = requests.post(f"{HUB_URL}/profiles/{quote(nm, safe='')}/activate", timeout=25)
        if r.ok:
            ST.push_event({"who": "sys", "warn": f"🎭 已联动激活角色「{nm}」(虚拟摄像头待机已同步)"})
            logger.info(f"直播联动: Hub 激活 {nm}")
            return
        logger.warning(f"Hub 激活 {nm} 失败 {r.status_code}: {r.text[:80]}")
    except Exception:
        logger.exception("Hub 激活失败，尝试直推 vcam")
    if face_bytes:
        try:
            requests.post(f"{VCAM_URL}/set_idle",
                          files={"face": ("face.jpg", face_bytes, "image/jpeg")}, timeout=12)
            logger.info("直播联动: 直推 vcam set_idle")
        except Exception:
            logger.exception("直推 vcam set_idle 失败")


def _push_audio_only(wav: bytes) -> bool:
    """纯音频推 vcam(待机脸不变 + 克隆语音)。直播自愈降级用:口型异常时画面/声音不中断,
    保留 OBS 桌面声与 WebRTC 音轨(VB-Cable 配音无法进 OBS,故直播降级走此路)。"""
    try:
        r = requests.post(f"{VCAM_URL}/play_audio",
                          files={"audio": ("a.wav", wav, "audio/wav")}, timeout=15)
        return r.ok
    except Exception:
        logger.exception("纯音频降级推送失败")
        return False


def _gen_stream_wav(wav: bytes, first_seg: int = FIRST_SEG_FRAMES) -> dict:
    """把一段克隆音 wav 送 lipsync 流式生成并推 vcam,返回该块的 ttfv/seg_gap/耗时。"""
    files = {"audio": ("a.wav", wav, "audio/wav")}
    data = {"face_id": ST.face_id, "fps": "25", "first_seg_frames": str(first_seg),
            "seg_frames": "25", "vcam_url": VCAM_URL}
    t_lip0 = time.time()
    # connect 3s 快速失败(read 仍 180s)：lipsync 掉线时热路径秒级触发降级,不再拖满 3 分钟
    r = requests.post(f"{LIPSYNC_URL}/lipsync/generate_stream",
                      files=files, data=data, timeout=(3.05, 180))
    lipsync_ms = int((time.time() - t_lip0) * 1000)
    out = {"ok": r.ok, "lipsync_ms": lipsync_ms}
    if r.ok:
        try:
            j = r.json()
            out["ttfv_ms"] = int(j["ttfv_ms"]) if j.get("ttfv_ms") is not None else None
            out["seg_gap_ms"] = int(j["seg_gap_ms"]) if j.get("seg_gap_ms") is not None else None
            out["seg_count"] = j.get("seg_count", 0)
        except Exception:
            pass
    else:
        logger.warning(f"数字人口型生成失败 {r.status_code}: {r.text[:120]}")
    return out


def _split_clauses(en: str) -> list:
    """长句切 2 块:优先在中点附近的子句标点(,;:—)断,否则在最近空格断。短句不切。"""
    words = en.split()
    if PIPELINE_MIN_WORDS <= 0 or len(words) < PIPELINE_MIN_WORDS:
        return [en]
    mid = len(en) // 2
    best = -1
    for i, ch in enumerate(en):                       # 中点附近的子句标点优先(语气自然)
        if ch in ",;:—" and abs(i - mid) < abs(best - mid):
            best = i
    if best > 0:
        a, b = en[:best + 1].strip(), en[best + 1:].strip()
    else:                                             # 无标点→最近空格二分
        sp = en.rfind(" ", 0, mid) if en.find(" ", mid) < 0 else en.find(" ", mid)
        if sp <= 0:
            return [en]
        a, b = en[:sp].strip(), en[sp + 1:].strip()
    return [a, b] if a and b else [en]


def _drive_avatar(en: str, zh: str = "") -> dict:
    """直播模式:英文克隆音 → lipsync 流式生成 → 逐段推 vcam(OBS虚拟摄像头)。
    长句走分句流水线:首块合成完即开播,次块在首块播放期间并行合成 → 隐藏整句 TTS 尾延迟。
    返回各阶段耗时(供观测/会话日志)。阻塞至该句生成完;pool_a 串行 → 句间自然排队。"""
    t0 = time.time()
    chunks = _split_clauses(en)
    _push_subtitle(en, zh, _sub_ttl_en(en), slot="bottom")   # 字幕始终整句,只推一次

    # 首块:同步合成(决定首帧延迟)。次块:后台并行合成,首块播放期间隐藏其 TTS 延迟。
    t_syn0 = time.time()
    b0 = _synth_en(chunks[0])
    synth_ms = int((time.time() - t_syn0) * 1000)
    if not b0:
        return {"ok": False}
    futures = [_SYNTH_POOL.submit(_synth_en, c) for c in chunks[1:]]   # 次块并行合成

    first = _gen_stream_wav(base64.b64decode(b0))            # 首块:首帧关键路径(用小 first_seg)
    out = {"ok": first.get("ok"), "synth_ms": synth_ms, "lipsync_ms": first.get("lipsync_ms"),
           "ttfv_ms": first.get("ttfv_ms"), "seg_gap_ms": first.get("seg_gap_ms"),
           "seg_count": first.get("seg_count", 0), "chunks": len(chunks)}
    if not first.get("ok"):
        out["avatar_ms"] = int((time.time() - t0) * 1000)
        return out
    for fut in futures:                                      # 次块:等其合成(多半已就绪)→续播
        try:
            bk = fut.result(timeout=120)
        except Exception:
            logger.exception("分句次块合成失败"); break
        if not bk:
            break
        rk = _gen_stream_wav(base64.b64decode(bk))                 # 续块同样用小首段→块间首帧≈160ms,消欠载
        if rk.get("seg_count"):
            out["seg_count"] += rk["seg_count"]
        if rk.get("seg_gap_ms") is not None:                 # 取较差段间隔(更保守地反映卡顿)
            out["seg_gap_ms"] = max(out.get("seg_gap_ms") or 0, rk["seg_gap_ms"])
    out["avatar_ms"] = int((time.time() - t0) * 1000)
    return out


def _live_fallback(en: str, zh: str = ""):
    """直播兜底:口型不可用(预热中/降级)时,保持待机脸 + 克隆语音经 vcam 进 OBS(画面/声音不断),
    字幕续传保留信息;vcam 推送失败(典型:真人换脸开播把 lipsync/vcam 泊车停机,2026-07-09 无声事故)
    时,同一段克隆音改推 VB-Cable 播放队列 → 以 CABLE 为直播音源的链路(OBS 麦=CABLE Output)仍有声。"""
    _push_subtitle(en, zh, _sub_ttl_en(en), slot="bottom")
    if ST.live_mode:
        wav = b""
        pushed = False
        try:
            b64 = _synth_en(en)                          # 复用克隆英文音
            if b64:
                wav = base64.b64decode(b64)
                pushed = _push_audio_only(wav)           # 待机脸 + 真声 → OBS(桌面声/WebRTC 音轨)
        except Exception:
            logger.exception("直播兜底纯音频推送失败")
        if not pushed and wav and ST.play_q is not None:
            try:
                _enqueue_wav(wav)                        # vcam 不可用 → 已合成音频走 CABLE,不二次 TTS
                logger.warning("直播兜底: vcam 推送失败，克隆音已改推 VB-Cable 播放队列")
            except Exception:
                logger.exception("直播兜底 VB-Cable 配音失败")
    elif ST.play_q is not None:
        try:
            _enqueue_synth(en)
        except Exception:
            logger.exception("直播兜底配音失败")


def _emit_output_a(en: str, zh: str, uid=None, tid=None) -> dict:
    """方向A输出分支(分段/流式·通话/直播 共用):
    直播→英文克隆音驱动数字人口型→OBS虚拟摄像头(含失败降级/兜底自愈);通话→克隆配音推 VB-Cable。
    返回 avatar 各阶段耗时 dict(通话模式为空 {})。"""
    av = {}
    emo = "" if ST.live_mode else _emo_for_sentence(zh, en)   # 情感路由仅通话配音(直播口型时序敏感)
    emo_w = _emo_intensity(zh, en, emo) if emo else 0.0       # P6.2 强度三档(SBV2 style_weight)
    if emo and uid is not None and tid is not None:           # P6.3 情感徽章事件(UI 按 uid 合并到该句)
        ST.push_event({"uid": uid, "turn": tid, "who": "me", "emo": emo,
                       "emo_w": round(emo_w, 2) if emo_w else None})
    # P5a 感情词注入：实际念的文本(spoken)带感叹词/笑声，字幕仍是干净译文(en)
    spoken = _emo_flavor(en, emo, _DST_LANG,
                         strong=bool(emo_w and emo_w >= EMO_W_STRONG)) if emo else en
    laugh = bool(emo in ("happy", "excited") and _re.search("哈哈|哈哈哈|笑死|大笑", zh or ""))
    _note_self_output(spoken)            # 登记自播文本(供无线回路文本回声闸比对,按实际播出稿)
    if ST.live_mode:
        if not _voice_ready():           # 无参考音:口型/兜底配音必败——保字幕、跳合成、节流告警
            _note_novoice_skip()
            _push_subtitle(en, zh, _sub_ttl_en(en), slot="bottom")
            return av
        use_avatar = ST.face_ready and not ST.live_degraded
        if use_avatar:
            try:
                ST._avatar_inflight += 1                  # 标记直播占用 GPU → 预载让位
                try:
                    av = _drive_avatar(en, zh) or {}
                finally:
                    ST._avatar_inflight -= 1; ST._last_avatar_ts = time.time()
                if av.get("ok"):
                    ST._avatar_calls += 1; ST._avatar_fail = 0
                    gap = av.get("seg_gap_ms")
                    if gap and ST._avatar_calls > 1:
                        ST.note_lipsync_lag(gap, 25.0 / 25.0 * 1000.0)   # seg_frames/fps=1s
                    if ST._post_switch_probe:    # 切换后首句:采样新脸 A/V 对齐
                        ST._post_switch_probe = False
                        _report_av_probe(av, "切换后首句口型")
                    elif ST._post_recover_probe: # 恢复后首句:复检 A/V 是否回实时
                        ST._post_recover_probe = False
                        _report_av_probe(av, "恢复后首句口型")
                else:
                    raise RuntimeError("lipsync 返回非 200")
            except Exception:
                logger.exception("方向A 数字人口型驱动失败")
                ST._avatar_fail += 1
                if ST._avatar_fail >= 2 and ST.set_degraded(True):
                    _enter_degrade("● 配音模式（口型恢复中）", "⚠ 口型/广播多次失败，已临时降级为配音/字幕")
                _live_fallback(en, zh)               # 本句立即兜底,不丢句
        else:
            # 预热未就绪 或 已降级 → 兜底输出(配音优先,无配音设备则仅字幕)
            _live_fallback(en, zh)
    elif ST.play_q is not None:
        try:
            _turn_hold()                       # P3-T 对方刚开口→句首礼让(最多 TURN_HOLD_MS)
            _sent_gap()                        # P2-D1 积压时句前垫呼吸间隙(真人换气节奏)
            _enqueue_synth(spoken, emotion=emo, laugh=laugh,   # 合成与播放并行，本段推完即返回
                           style_weight=emo_w)
        except Exception:
            logger.exception("方向A 克隆配音失败")
    return av


def _apply_pending_switch():
    """在句子边界(pool_a 串行)原子应用待生效的角色切换 → 直播不断流。"""
    if ST.pending_switch is None:
        return
    with ST.lock:
        sw = ST.pending_switch
        ST.pending_switch = None
    if not sw:
        return
    ST.profile = sw["profile"]
    ST.voice_b64 = sw["voice_b64"]; ST.ref_text = sw["ref_text"]
    face_changed = False
    if ST.live_mode and sw.get("face_id"):
        face_changed = (sw["face_id"] != ST.face_id)
        ST.face_id = sw["face_id"]; ST.idle_video = sw.get("idle_video", "")
        if sw.get("face_bytes"):                     # 同步切换 vcam 待机头像
            try:
                requests.post(f"{VCAM_URL}/set_idle",
                              files={"face": ("face.jpg", sw["face_bytes"], "image/jpeg")}, timeout=12)
            except Exception:
                logger.exception("切换 vcam 待机头像失败")
    ST.switch_count += 1
    _novoice_reset()                                 # 切角色=新语境:告警节流窗/计数清零,首句即时反馈
    if face_changed:
        ST._post_switch_probe = True                 # 新脸首句做一次 A/V 对齐回归采样
    ST.push_event({"who": "sys", "warn": f"✅ 已切换到角色「{sw['profile']}」"})
    logger.info(f"角色切换生效: {sw['profile']}")
    if ST.live_mode:   # P6: 切换刚记完账(use_n 已更新)，顺手回补预案池——常用榜第一时间反映到池子
        threading.Thread(target=_auto_preload_top, args=(3,), daemon=True).start()


PRESET_MANIFEST = os.path.join("logs", "preset_pool.json")


def _sig_from_detail(d: dict) -> str:
    """从 Hub 角色详情(无需大字段)算轻量签名:音色/人脸/待机视频/引擎任一变更即不同 → 预案失效。"""
    return "|".join(str(d.get(k, "")) for k in
                    ("has_face", "has_voice", "voice_name", "idle_video", "lipsync_engine", "tts_engine"))


def _profile_sig(nm: str) -> str:
    """轻量拉取角色详情(不取 face/voice 大字段)算签名,用于运行中预案失效判断。"""
    try:
        d = requests.get(f"{HUB_URL}/profiles/{quote(nm, safe='')}", timeout=6).json()
        return _sig_from_detail(d)
    except Exception:
        return ""


def _save_preset_manifest():
    """预案池落盘(仅元数据,不含音/脸大字段):跨会话记录已就绪角色与指纹,供复用与失效判断。"""
    try:
        os.makedirs("logs", exist_ok=True)
        data = {nm: {"fp": sw.get("fp", ""), "sig": sw.get("sig", ""),
                     "face_id": sw.get("face_id", ""), "idle_video": sw.get("idle_video", ""),
                     "ts": time.time()}
                for nm, sw in ST.preset_cache.items()}
        with open(PRESET_MANIFEST, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("预案池落盘失败")


def _load_preset_manifest() -> dict:
    """读取上次落盘的预案池元数据(供 UI 展示可复用角色;指纹匹配则视为磁盘缓存仍有效)。"""
    try:
        if os.path.exists(PRESET_MANIFEST):
            with open(PRESET_MANIFEST, encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        logger.exception("预案池读取失败")
    return {}


def _build_switch(nm: str, warn: bool = True) -> dict | None:
    """准备一个角色的切换包(取音色/人脸→预计算→参考预热);供运行中切换与预案池预载复用。
    口型 UNet 是按序列长度的全局编译,会话首启已 _warmup_lipsync 后即全局生效→切脸无需重复热启动,
    仅冷脸做一次 precompute_face(命中 lipsync 磁盘缓存则秒回)。返回 sw,失败返回 None。"""
    voice_b64, ref_text = _fetch_voice_ref(nm)
    if not voice_b64 and not _profile_voice_optional(nm):
        fb = _fallback_voice()
        if fb:                              # env 开了兜底:切换后出通用声,同样明说
            voice_b64, ref_text = fb
            if warn:
                ST.push_event({"who": "sys", "warn":
                               f"🔉 角色「{nm}」无音色样本，切换后将使用通用兜底音色(非该角色专属声)"})
        elif warn:
            # 与"无人脸仅切音色"同级的对称提醒：无音色切过去=配音无声,提前说清(切换仍放行,字幕不受影响)
            ST.push_event({"who": "sys", "warn": _voice_missing_hint(nm)})
            logger.warning(f"切换目标无音色参考: profile={nm!r}(切换后配音将跳过)")
    _prewarm_ref(voice_b64, ref_text)
    sw = {"profile": nm, "voice_b64": voice_b64, "ref_text": ref_text,
          "face_id": "", "idle_video": "", "face_bytes": b"", "fp": "", "sig": ""}
    if ST.live_mode:
        pj = requests.get(f"{HUB_URL}/profiles/{quote(nm, safe='')}",
                          params={"include_face": "true"}, timeout=10).json()
        face_b64 = pj.get("face_b64", "")
        face_bytes = _b64bytes(face_b64)
        sw["sig"] = _sig_from_detail(pj)
        sw["fp"] = hashlib.sha1(((voice_b64 or "") + (face_b64 or "")).encode("utf-8")).hexdigest()[:16]
        if not face_bytes:
            if warn:
                ST.push_event({"who": "sys", "warn": f"⚠ 角色「{nm}」无人脸，仅切换音色"})
            sw["face_id"] = ST.face_id          # 保留当前形象
        else:
            face_id = f"interp_{nm}"
            disk = ST.preset_disk.get(nm)
            if disk and disk.get("fp") == sw["fp"]:   # 指纹命中上次落盘 → 内容未变,lipsync 磁盘缓存命中(预计算秒回)
                logger.info(f"预案指纹命中(内容未变,走 lipsync 磁盘缓存): {nm}")
            ok = _precompute_face_call(face_bytes, face_id)   # 命中 lipsync 磁盘缓存则极快;始终调用以保正确
            if ok:
                if ST.warm_ms <= 0:             # 仅会话尚未热启动过才补热(UNet 编译全局,无需逐脸重复)
                    _warmup_lipsync(face_id)
                sw["face_id"] = face_id
                sw["idle_video"] = (pj.get("idle_video") or "").strip()
                sw["face_bytes"] = face_bytes
            else:
                if warn:
                    ST.push_event({"who": "sys", "warn": f"⚠ 角色「{nm}」人脸预计算失败，仅切换音色"})
                sw["face_id"] = ST.face_id
    else:
        sw["sig"] = _profile_sig(nm)
    return sw


def _usage_report(nm: str, kind: str):
    """向 Hub 上报一次会话级使用(常用音色自动置顶的数据源)。忙轮不重要,失败静默。"""
    def _post(name=nm, k=kind):
        try:
            name = (name or "").strip()
            if not name:   # 未显式选角色=用 Hub 当前激活角色(激活时 Hub 已自计,不再重复)
                return
            requests.post(f"{HUB_URL}/profiles/{quote(name, safe='')}/usage_bump",
                          json={"kind": k}, timeout=4)
        except Exception:
            pass
    threading.Thread(target=_post, daemon=True).start()


_auto_pl_lock = threading.Lock()   # P6: 开播自动预载 与 切换后回补 可能同刻触发，抢闸只留一个


def _auto_preload_top(n: int = 3):
    """P4: 开播后静默预载台账 Top N（只预载真用过的角色，省 GPU/显存）。
    P6: 切换生效后也调它回补——池子始终保持"下一批最可能用的"就绪。
    延迟几秒让首播热身/切换收尾先跑，不与口型争卡。"""
    time.sleep(4)
    if not _auto_pl_lock.acquire(blocking=False):
        return
    try:
        if not ST.running or ST.preset_loading:
            return
        d = requests.get(f"{HUB_URL}/profiles", params={"fields": "name,use_n"}, timeout=5).json()
        cur = (ST.profile or "").strip()
        ranked = sorted(
            [p for p in d.get("profiles", []) if p.get("name") and p["name"] != cur and (p.get("use_n") or 0) > 0],
            key=lambda p: -(p.get("use_n") or 0))[:n]
        todo = [p["name"] for p in ranked if p["name"] not in ST.preset_cache]
        if not todo:
            return
        ST.preset_loading = True
        ST.preset_queue = list(todo)
        threading.Thread(target=_preload_worker, args=(todo,), daemon=True).start()
        logger.info(f"自动预载常用 Top{len(todo)}: {todo}")
    except Exception:
        logger.debug("自动预载常用跳过", exc_info=True)
    finally:
        _auto_pl_lock.release()


def _prepare_switch(nm: str):
    """后台准备新角色,就绪后挂 pending,下一句边界生效。命中预案池则近即时。"""
    try:
        cached = ST.preset_cache.get(nm)
        if cached and _profile_sig(nm) == cached.get("sig"):   # 命中且内容未变(指纹/签名一致)→ 直接挂起,几乎瞬时
            sw = dict(cached)
            logger.info(f"角色切换命中预案池: {nm}")
        else:
            if cached:                           # 命中但内容已变 → 失效重建
                ST.preset_cache.pop(nm, None)
                logger.info(f"预案池失效(内容已变),重建: {nm}")
            sw = _build_switch(nm)
        ST.pending_switch = sw
        ST.push_event({"who": "sys", "warn": f"🔄 角色「{nm}」已就绪，下一句无缝切换"})
        _usage_report(nm, "interp_switch")   # P3: 通话中换到谁=最强的"常用"信号
    except Exception:
        logger.exception("准备角色切换失败")
        ST.push_event({"who": "sys", "warn": "⚠ 角色切换准备失败"})
    finally:
        ST.switching = False


def _yield_to_live(max_wait: float = 30.0):
    """预载让位:lipsync 是单 GPU 队列,后台重活(冷脸预计算~7-28s)会排在直播句前面阻塞口型。
    故在发起重活前,若有直播句正在生成或刚生成(<1.5s),先等其空档,优先保障实时口型。"""
    t0 = time.time()
    while ST.running and time.time() - t0 < max_wait:
        if ST._avatar_inflight <= 0 and (time.time() - ST._last_avatar_ts) > 1.5:
            return
        time.sleep(0.3)


def _preload_worker(names: list):
    """预案池预载:后台串行(避免 GPU 争用)为常用角色预计算人脸+预热参考并缓存,
    后续切到这些角色近即时(只剩挂起+换头像)。不影响当前直播输出。"""
    try:
        for nm in names:
            nm = (nm or "").strip()
            if not nm or nm == ST.profile or nm in ST.preset_cache:
                if nm in ST.preset_queue:
                    ST.preset_queue.remove(nm)
                continue
            if not ST.running:
                break
            _yield_to_live()                          # 让位:直播句正在/刚生成时,先等(单GPU队避免阻塞口型)
            try:
                sw = _build_switch(nm, warn=False)
                if sw:
                    ST.preset_cache[nm] = sw
                    _save_preset_manifest()       # 每入池一个即落盘:跨会话记录指纹/已就绪角色
                    ST.push_event({"who": "sys", "warn": f"📦 角色「{nm}」已预载入池"})
            except Exception:
                logger.exception(f"预载角色失败: {nm}")
            finally:
                if nm in ST.preset_queue:             # 成败都出队:失败的别永远挂着"排队中"
                    ST.preset_queue.remove(nm)
    finally:
        ST.preset_loading = False
        ST.preset_queue = []


def _heal_worker(stop_evt):
    """自愈 watchdog:周期探测 lipsync/vcam 健康,异常→降级(配音/字幕),恢复→回升口型;
    并周期 GPU 争用自检:持续争用→升级为「建议独占显卡」置顶告警(联动 UI)。"""
    cycle = 0
    while not stop_evt.is_set():
        if stop_evt.wait(5.0):
            break
        if not (ST.running and ST.live_mode):
            continue
        try:
            ls_ok = False
            try:
                ls_ok = requests.get(f"{LIPSYNC_URL}/health", timeout=2).ok
            except Exception:
                ls_ok = False
            vs = _vcam_status()
            vc_bad = vs.get("cam_ready") is False
            bad = (not ls_ok) or vc_bad
            if bad and ST.set_degraded(True):
                why = "口型服务" if not ls_ok else "虚拟摄像头"
                _enter_degrade(f"● 配音模式（{why}恢复中）", f"⚠ {why}异常，已临时降级为配音/字幕")
            elif (not bad) and ST._avatar_fail == 0 and ST.set_degraded(False):
                _exit_degrade()
            cycle += 1                                     # GPU 争用自检:约每 10s 一次(nvidia-smi 较重)
            if cycle % 2 == 0:
                _heal_gpu_contention()
        except Exception:
            logger.exception("自愈 watchdog 异常")


def _heal_gpu_contention():
    """运行中周期探 GPU 争用:连续 ~3 次(约 30s)仍争用→升级置顶告警「建议独占显卡」;
    争用消失→撤销告警。仅更新告警态与一次性事件,不强制降级(口型由健康/失败逻辑兜底)。"""
    g = _gpu_snapshot()
    if not g:
        return
    ST.gpu = g
    if _gpu_contended(g):
        ST._gpu_streak += 1
        if ST._gpu_streak >= 3 and not ST.gpu_alert:       # 持续争用→升级置顶
            ST.gpu_alert = True
            ST.push_event({"who": "sys", "warn":
                           f"🔥 显卡持续被占用(计算进程 {g.get('compute_apps','?')}个/"
                           f"利用率 {g.get('util_pct','?')}%)，口型可能滞后；"
                           f"强烈建议关闭占卡程序或独占显卡"})
            _set_vcam_notice("● 显卡争用：口型可能滞后")
    else:
        ST._gpu_streak = 0
        if ST.gpu_alert:                                   # 争用解除→撤销告警
            ST.gpu_alert = False
            ST.push_event({"who": "sys", "warn": "✅ 显卡争用已缓解，口型恢复实时"})
            if not ST.live_degraded:                       # 不覆盖降级中的配音提示
                _set_vcam_notice("")


# ── 两个方向的处理 ────────────────────────────────────────────────────
def _process_a(mono16k: np.ndarray, skip_spk: bool = False):
    """我说中文 → 英文 → 克隆配音 → 播虚拟麦 → 字幕。
    local(默认): Whisper transcribe(中) 单次出中文字幕 + 本地 NMT 译英(~70ms，快·准·离线)；
    whisper:     Whisper task=translate 一步出英文(偶有词误)，中文字幕异步补。
    skip_spk=True: P-Affirm 放行复跑的一次性声纹豁免——用户刚点过「是我」，学习权重(20%)
    不保证复跑句立即过门,不豁免会出现"点了放行又被拦"的荒谬循环。"""
    _apply_pending_switch()                # 句子边界:若有待生效的角色切换则原子应用(不断流)
    t0 = time.time()
    uid = ST.next_uid(); tid = ST.turn_id("me")
    feats = _audio_features(mono16k)       # 前置门控:静音/底噪段直接不送 STT(杜绝幻听,省一次往返)
    if not _segment_is_speech(feats, "a"):
        ST.dropped += 1; ST.drops["gate"] += 1
        _rg, _pg = _adaptive_gates("a")
        logger.info(f"[前置门控] 丢弃静音/底噪段[a]: rms={feats['rms_dbfs']:.1f}dBFS "
                    f"peak={feats['peak_dbfs']:.1f}dBFS dyn={feats['dyn_db']:.1f}dB "
                    f"(门 rms≥{_rg:.0f}/peak≥{_pg:.0f}/dyn≥{GATE_DYN_DB_MIN:.0f} 噪底={_noise_floor_dbfs('a')})")
        return
    # P1-2 声纹锁：已注册 → 送 STT 前先验说话人(20~70ms)，旁人声/键盘噪直接拦下(还省一次 STT)
    if _voicelock.ready() and not skip_spk:
        ok_spk, sim = _voicelock.check(mono16k)
        if not ok_spk:
            ST.dropped += 1; ST.drops["spk"] += 1
            logger.info(f"[声纹锁] 拦截非注册说话人[a]: sim={sim:.3f} < {VOICELOCK_THR}")
            _spk_blocked_note(uid, tid, "", mono16k, sim)   # P-Affirm 留证+灰行(可一键放行)
            return
    _note_voice("a")                       # P3 我方语音活跃时戳(附和判据:我在说就别嗯嗯)
    _emo_note_audio(mono16k)               # P1 情感:登记本句原声响度(基调佐证)
    _pros_note(mono16k)                    # P9.1 韵律跟随:音高摆幅/响度(zh 未知,语速跳过)
    mono16k = _denoise16k(mono16k, "a")    # P1-3 送 STT 前洗掉稳态噪声(识别更准)
    if ST.mode == "whisper":
        en, meta = _stt(mono16k, "en", task="translate", return_meta=True); t_asr = time.time()
        if not en:
            return
        if _drop_stt("a", en, feats, meta):  # 静音幻听/弱填充词/低置信 → 不出字幕、更不能用克隆音把幻听说给对方
            return
        en = _collapse_repeats(en)           # 复读折叠:同一句话绝不配音两遍
        if _self_echo_risk_a() and _is_self_echo(en):   # 无线回路文本回声闸(仅麦可能录到自家配音的场景)
            ST.dropped += 1; ST.drops["echo"] += 1
            logger.info(f"[回声闸] 丢弃自播回录文本[a]: {en!r}")
            _note_echo_loop()
            return
        _voicelock_autoenroll(mono16k)
        ST.push_event({"uid": uid, "turn": tid, "who": "me", "en": en,
                       "ms": int((time.time() - t0) * 1000)})
        ST.record_turn_src(tid, "me", "zh", "")    # whisper 模式无中文源，软终判跳过
        ST.record_transcript("me", "", en, tid)    # whisper 直译无同步中文源，仅记译文
        def _fill_zh():
            try:
                zh = _stt(mono16k, "zh", task="transcribe", initial_prompt=_asr_hotwords("zh"))
                if zh:
                    ST.push_event({"uid": uid, "turn": tid, "who": "me", "zh": zh})
            except Exception:
                pass
        threading.Thread(target=_fill_zh, daemon=True).start()
        t_nmt = t_asr
    else:
        # zh/en 变量名沿用历史；语义为 zh=我方原文(SRC 语言)、en=译文(DST 语言)，字段位置不变
        zh, meta = _stt(mono16k, _SRC_LANG, task="transcribe", return_meta=True,
                        initial_prompt=_asr_hotwords(_SRC_LANG)); t_asr = time.time()   # P0-② 热词引导
        if not zh:
            return
        if _drop_stt("a", zh, feats, meta):  # 静音幻听/弱填充词/低置信 → 不出字幕、更不能用克隆音把幻听说给对方
            return
        if _self_echo_risk_a() and _is_self_echo(zh):   # 无线回路文本回声闸(仅麦可能录到自家配音的场景)
            ST.dropped += 1; ST.drops["echo"] += 1
            logger.info(f"[回声闸] 丢弃自播回录文本[a]: {zh!r}")
            _note_echo_loop()
            return
        _voicelock_autoenroll(mono16k)       # 过全部门控的真话 → 累积自动注册(已注册则空操作)
        zh = _collapse_repeats(zh)           # 复读折叠:同一句话绝不配音两遍
        ST.push_event({"uid": uid, "turn": tid, "who": "me", "zh": zh})   # 原文先到(确认)
        ST._tts_first_ts = 0.0               # P0-R3 本句首音时戳起点(译前置零:流式路径译中即出声)
        _filler_arm()                        # P1-F 超时未出声→垫场气口
        en = None
        if not ST.live_mode:                 # P1-S 流式 LLM 边译边配(仅通话;不适用返回 None)
            en = _translate_dub_stream(zh, _SRC_LANG, _DST_LANG)
        _streamed = en is not None
        if en is None:
            en = _collapse_repeats(_translate_nmt(zh, _SRC_LANG, _DST_LANG))
        t_nmt = time.time()
        ST.push_event({"uid": uid, "turn": tid, "who": "me", "en": en,
                       "ms": int((time.time() - t0) * 1000)})
        _mt_ctx_note(_SRC_LANG, _DST_LANG, zh, en)   # P0-R2 滚动语境(分段模式同样受益)
        ST.record_turn_src(tid, "me", _SRC_LANG, zh)
        ST.record_transcript("me", zh, en, tid)
    ST.stats["a"] += 1
    # 输出分支(共用):直播→数字人口型(OBS虚拟摄像头);通话→克隆配音推VB-Cable。
    t_syn0 = time.time()
    if ST.mode == "whisper":
        _streamed = False
        ST._tts_first_ts = 0.0             # P0-R3 本句首音时戳起点(whisper 分支在此置零)
    av = {} if _streamed else _emit_output_a(en, zh if ST.mode != "whisper" else "", uid=uid, tid=tid)
    m = {"dir": "a", "asr_ms": int((t_asr - t0) * 1000),
         "nmt_ms": int((t_nmt - t_asr) * 1000),
         "backlog": (ST.play_q.qsize() if ST.play_q else 0), "ts": time.time()}
    _ft = getattr(ST, "_tts_first_ts", 0.0)
    if _ft:                                # 首音延迟:段起点→首块入播放队列
        m["tts_first_ms"] = int((_ft - t0) * 1000)
    if _streamed:
        m["llm_stream"] = 1
    if av:
        m.update(av)
    else:
        m["synth_ms"] = int((time.time() - t_syn0) * 1000)
        m["avatar_ms"] = int((time.time() - t_syn0) * 1000) if ST.live_mode else 0
    ST.add_metric(m)


def _spk_blocked_note(uid: int, tid: int, text: str, wav16k, sim):
    """P-Affirm 声纹拦截留证：音频进环形缓存(近8句)，推灰行事件到控制台——
    用户看到「这句被声纹拦了」+ 一键「是我·放行并学习」。误拦从黑洞变成一次点击。"""
    try:
        ST.spk_blocked.append({"uid": int(uid), "tid": int(tid), "text": text or "",
                               "sim": (round(float(sim), 3) if sim is not None else None),
                               "ts": time.time(), "wav": np.copy(wav16k)})
        ST.push_event({"uid": int(uid), "turn": int(tid), "who": "me", "blocked": "spk",
                       "text": text or "", "dur": round(wav16k.size / float(SR), 1),
                       "sim": (round(float(sim), 3) if sim is not None else None),
                       "thr": VOICELOCK_THR})
    except Exception:
        logger.exception("声纹拦截留证失败(不影响拦截本身)")


def _voicelock_autoenroll(mono16k: np.ndarray):
    """方向A一句"过全部门控+幻听过滤"的真话 → 累积进自动注册(够 3 句成底座)。
    已注册/未启用/段过短(<1.2s) → 空操作。"""
    if not (VOICELOCK_ENABLE and VOICELOCK_AUTOENROLL):
        return
    if _voicelock.ready() or mono16k is None or mono16k.size < int(SR * 1.2):
        return
    # 注册防污染(P6)：段与自家耳返/朗读外放时段重叠 → 麦可能录的是外放(音箱场景),
    # 这种段进底座就是 2026-07-07 23:52 事故的根源(污染底座→机主整场被拦)。宁慢勿脏。
    if _aux_echo_overlap(mono16k):
        logger.info("[声纹锁] 跳过与自家外放重叠的注册候选段(防底座污染)")
        return
    try:
        if _voicelock.enroll_from(mono16k):
            ST.push_event({"who": "sys", "warn": "🔒 声纹已自动注册：此后只翻译你的声音(旁人声/键盘噪自动拦截)"})
            logger.info("声纹锁自动注册完成")
    except Exception:
        logger.exception("声纹自动注册失败(不影响主流程)")


def _process_b(mono16k: np.ndarray):
    """对方说英文 → 识别 → 本地 NMT 译中 → 中英双语字幕(不配音)。"""
    _apply_pending_switch()                # 我方静默期对方说话时也能让切换生效(不断流)
    t0 = time.time()
    uid = ST.next_uid(); tid = ST.turn_id("other")
    feats = _audio_features(mono16k)       # 前置门控:静音/底噪段直接不送 STT(对方通道幻听重灾区)
    if not _segment_is_speech(feats, "b"):
        ST.dropped += 1; ST.drops["gate"] += 1
        _rg, _pg = _adaptive_gates("b")
        logger.info(f"[前置门控] 丢弃静音/底噪段[b]: rms={feats['rms_dbfs']:.1f}dBFS "
                    f"peak={feats['peak_dbfs']:.1f}dBFS dyn={feats['dyn_db']:.1f}dB "
                    f"(门 rms≥{_rg:.0f}/peak≥{_pg:.0f}/dyn≥{GATE_DYN_DB_MIN:.0f} 噪底={_noise_floor_dbfs('b')})")
        return
    if _aux_echo_overlap(mono16k):         # P2-2 回声闸:与自家耳返/朗读外放重叠的段不进翻译
        ST.dropped += 1; ST.drops["echo"] += 1
        logger.info("[回声闸] 丢弃与耳返/朗读外放重叠的对方段(防自激)")
        return
    _note_voice("b")                       # P3 对方语音活跃时戳(礼让/附和/垫场判据)
    mono16k = _denoise16k(mono16k, "b")    # P1-3 对方通道同样先洗噪(通话压缩伪影多,收益更大)
    # en/zh 变量名沿用历史；语义为 en=对方原文(DST 语言)、zh=译文(SRC 语言)，字段位置不变
    en, meta = _stt(mono16k, _DST_LANG, task="transcribe", return_meta=True,
                    initial_prompt=_asr_hotwords(_DST_LANG)); t_asr = time.time()   # P0-② 热词引导
    if not en:
        return
    if _drop_stt("b", en, feats, meta):   # 静音幻听/弱填充词/低置信/连刷 → 不出字幕(否则对方一停顿就刷屏垃圾)
        return
    if _is_self_echo(en):                  # 文本回声闸:环回收到自家配音(Voicemeeter 路由回默认输出时)
        ST.dropped += 1; ST.drops["echo"] += 1
        logger.info(f"[回声闸] 丢弃自播回录文本[b]: {en!r}")
        _note_echo_loop()                  # 连续拦到=配音在外部链路绕圈,提示用户排查
        return
    _rb_ref_consider(mono16k, en)          # P2-2 过全部门控的真话才可当对方参考音
    en = _collapse_repeats(en)             # 复读折叠:同一句不重复上屏/朗读
    ST.push_event({"uid": uid, "turn": tid, "who": "other", "en": en})
    zh = _collapse_repeats(_translate_nmt(en, _DST_LANG, _SRC_LANG)); t_nmt = time.time()
    ST.push_event({"uid": uid, "turn": tid, "who": "other", "zh": zh,
                   "ms": int((time.time() - t0) * 1000)})
    _mt_ctx_note(_DST_LANG, _SRC_LANG, en, zh)   # P0-R2 滚动语境(对方向独立分桶)
    ST.record_turn_src(tid, "other", _DST_LANG, en)
    ST.record_transcript("other", en, zh, tid)
    ST.stats["b"] += 1
    _readback_say(zh)                      # P2-2 对方音色读中文译文(开关关闭时是空操作)
    _backchannel_kick(en)                  # P3-B 对方长句后我方久无动静→轻附和"我在听"
    if ST.live_mode:
        _sub_debouncer.push("top", en, zh, _sub_ttl_en(en))   # 顶部=对方(防抖)
    ST.add_metric({"dir": "b", "asr_ms": int((t_asr - t0) * 1000),
                   "nmt_ms": int((t_nmt - t_asr) * 1000), "ts": time.time()})


def _finalizer_worker(stop_evt):
    """软终判润色：一轮静默 FINALIZE_GAP 后，把整轮源文当一句重译，替换该轮的逐子句译文(读感更顺)。
    仅改字幕(音频已逐子句播出，不动)。单子句轮无需润色。"""
    while not stop_evt.is_set():
        time.sleep(0.3)
        now = time.time(); due = []
        with ST.lock:
            for tid, e in list(ST.turn_src.items()):
                if e["done"]:
                    if now - e["last"] > 30:
                        ST.turn_src.pop(tid, None)
                elif now - e["last"] > FINALIZE_GAP and len([t for t in e["texts"] if t]) >= 2:
                    e["done"] = True
                    due.append((tid, e["who"], e["lang"], " ".join(t for t in e["texts"] if t)))
        for tid, who, lang, full in due:
            try:
                if who == "me":                      # 方向A：我方原文(SRC) → 译文(DST)
                    full = _collapse_repeats(_ger_text_polish(full, _SRC_LANG))  # P0-① 修同音字+折叠复读
                    en = _translate_nmt(full, _SRC_LANG, _DST_LANG)
                    if SUBS_MATCH_AUDIO:
                        # P0-R4 音画字一致：译文配音已按逐子句版本播出,屏幕/OBS 译文字幕不再
                        # 被整轮润色稿替换(耳听≠屏读=直播可感知穿帮)。源文润色照常上屏
                        # (配音念的是译文,改源文无音字冲突)；润色译文仍进转写留存(导出质量不降)。
                        ST.push_event({"finalize": True, "turn": tid, "who": who,
                                       "src": full, "zh": full})
                    else:
                        ST.push_event({"finalize": True, "turn": tid, "who": who,
                                       "src": full, "zh": full, "en": en})
                        if ST.live_mode:
                            _sub_debouncer.push("bottom", en, full, _sub_ttl_en(en), immediate=True)
                    ST.finalize_transcript(who, tid, full, en)   # 润色终稿回填转写(导出=屏幕稿)
                else:                                # 方向B：对方原文(DST) → 译文(SRC)
                    full = _collapse_repeats(_ger_text_polish(full, _DST_LANG))
                    zh = _translate_nmt(full, _DST_LANG, _SRC_LANG)
                    ST.push_event({"finalize": True, "turn": tid, "who": who,
                                   "src": full, "en": full, "zh": zh})
                    ST.finalize_transcript(who, tid, full, zh)   # 润色终稿回填转写(导出=屏幕稿)
                    if ST.live_mode:
                        _sub_debouncer.push("top", full, zh, _sub_ttl_en(full), immediate=True)
            except Exception:
                logger.exception("软终判润色失败")


# ── 流式 STT 客户端(Nemotron 逐词) ───────────────────────────────────────
class StreamSink:
    """流式采集汇：接 dev_sr 音频块 → 重采样 16k → PCM16 推 nemotron /ws/transcribe(auto_eou)。
    partial→on_partial(text) 逐词刷字幕；final→on_final(text, audio16k) 经门控后定稿处理。
    与 Segmenter 接口一致(feed/flush)，供 Capture 在流式模式下无缝替换。断线自动重连。"""
    def __init__(self, dev_sr, direction, language, on_partial, on_final, tag="", pause_pred=None):
        self.dev_sr = int(dev_sr or 48000)
        self.direction = direction
        self.language = language or ("zh" if direction == "a" else "en")
        self.on_partial = on_partial
        self.on_final = on_final
        self.tag = tag
        self._pause_pred = pause_pred          # ()→bool：True 时暂停上行(口型独占GPU),本地暂存
        # P3-3 流式降噪：方向A(我的麦)帧级 RNNoise 洗后再上行(键盘声等非稳态噪声在
        # ASR 听到之前就被压掉;门控/声纹/朗读参考音用的也是洗后音频,口径一致)。
        # 方向B(对方环回)不洗:通话软件已做过处理,再洗可能伤到用作克隆参考的音质。
        self._dn = None
        if direction == "a" and DENOISE_ENABLE:
            try:
                self._dn = _RNNoiseStream()
            except Exception:
                self._dn = None
        self._q = queue.Queue(maxsize=256)     # PCM16 bytes 上行队列
        self._gate = []                        # 自上次 final 起的 16k 音频(门控用,也=连续未定稿时长)
        self._gate_lock = threading.Lock()
        self._flush_pending = False            # 已请求强制定稿(避免重复发 eou)
        self._stop = threading.Event()
        if direction == "a":                   # P0-R1: 语义块提前配音要在 final 前预检当前句音频
            ST.stream_sink_a = self
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()

    def peek_gate(self):
        """不清空地取当前句已累积的 16k 音频副本(P0-R1 块提交前的门控/声纹预检用)。"""
        with self._gate_lock:
            return np.concatenate(self._gate) if self._gate else np.zeros(0, np.float32)

    def feed(self, block: np.ndarray):
        if self._stop.is_set():
            return
        # P7 修补(2026-07-10)：流式模式下 Segmenter 被本类替换,环境音垫一直采不到素材
        # (实测整场 material_s=0)。在此按响度粗判静音块喂垫——-42dBFS 以下视为纯环境音。
        if self.direction == "a" and block is not None and getattr(block, "size", 0):
            try:
                b = block.mean(axis=1) if block.ndim > 1 else block
                rms = float(np.sqrt(np.mean(b * b)) + 1e-12)
                if 1e-6 < rms < 10.0 ** (-42.0 / 20.0):
                    _amb_note(np.asarray(b, np.float32), self.dev_sr)
            except Exception:
                pass
        if block.ndim > 1:
            block = block.mean(axis=1)
        if _capture_should_mute(self.direction):   # 半双工:自家外放期间喂静音(流不断,VAD 可正常定稿)
            block = np.zeros_like(block)
        b16 = _resample(np.asarray(block, np.float32), self.dev_sr, SR)
        if self._dn is not None and b16.size:
            try:
                b16 = self._dn.process16k(b16)     # 帧级流式降噪(状态跨块,≤10ms 缓冲)
            except Exception:
                self._dn = None                    # 运行中异常→本会话降级为不洗,不断流
        if b16.size == 0:
            return
        force = False
        with self._gate_lock:
            self._gate.append(b16)
            tot = sum(len(x) for x in self._gate)
            # 自上次 final 起累积时长达上限(连续说话不停顿)→ 主动强制定稿(补回 VAD_MAX 保护)。
            if tot > SR * STREAM_MAX_SEC and not self._flush_pending:
                self._flush_pending = True; force = True
            # 防爆：门控缓冲只留最近 ~ (MAX+3)s
            cap = int(SR * (STREAM_MAX_SEC + 3))
            while tot > cap and len(self._gate) > 1:
                tot -= len(self._gate.pop(0))
        pcm = (np.clip(b16, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        try:
            self._q.put_nowait(pcm)
        except queue.Full:
            pass
        if force:
            try: self._q.put_nowait(b"__FLUSH__")
            except queue.Full: pass

    def flush(self):
        self._stop.set()
        try:
            self._q.put_nowait(b"__EOU__")
        except Exception:
            pass
        if self._dn is not None:
            try:
                self._dn.close()
            except Exception:
                pass
            self._dn = None

    def _pop_gate(self):
        with self._gate_lock:
            buf = np.concatenate(self._gate) if self._gate else np.zeros(0, np.float32)
            self._gate = []
            self._flush_pending = False     # 定稿到达,缓冲清零,允许下次强制定稿
        return buf

    def _run(self):
        try:
            asyncio.run(self._main())
        except Exception:
            logger.exception(f"[{self.tag}] 流式线程异常")

    async def _main(self):
        import websockets
        uri = (f"{NEMO_WS_URL}/ws/transcribe?language={self.language}"
               f"&auto_eou=1&sil_ms={STREAM_SIL_MS}&min_voice_ms={STREAM_MINVOICE_MS}")
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(uri, max_size=None, ping_interval=20) as ws:
                    logger.info(f"采集启动[{self.tag}]: 流式WS {uri}")
                    backoff = 1.0
                    await asyncio.gather(self._sender(ws), self._receiver(ws))
            except Exception as e:
                if self._stop.is_set():
                    break
                logger.warning(f"[{self.tag}] 流式WS断线重连({e})")
                await asyncio.sleep(backoff); backoff = min(backoff * 2, 8.0)

    def _q_get(self):
        try:
            return self._q.get(timeout=0.2)
        except queue.Empty:
            return None

    def _paused(self) -> bool:
        try:
            return bool(self._pause_pred and self._pause_pred())
        except Exception:
            return False

    async def _send_marker_or_pcm(self, ws, pcm):
        if pcm in (b"__EOU__", b"__FLUSH__"):
            try: await ws.send(json.dumps({"event": "eou"}))
            except Exception: pass
        else:
            await ws.send(pcm)

    async def _sender(self, ws):
        loop = asyncio.get_event_loop()
        held = []                            # 暂停期(口型生成)本地暂存,不喂服务端→省 GPU
        while not self._stop.is_set():
            pcm = await loop.run_in_executor(None, self._q_get)
            if pcm is None:
                if held and not self._paused():        # 暂停结束 → 冲刷积压(保序)
                    for p in held: await self._send_marker_or_pcm(ws, p)
                    held = []
                continue
            if pcm == b"__EOU__":           # 停止：始终立即发 eou 并结束(优先于暂停)
                try: await ws.send(json.dumps({"event": "eou"}))
                except Exception: pass
                break
            if self._paused():              # 口型生成中：本地暂存,让 lipsync 独占 GPU
                if not held:                # 本轮让位开始(空→非空)→ 计一次让位事件
                    try: ST.stream_stats["yields"] += 1
                    except Exception: pass
                held.append(pcm); continue
            if held:                        # 刚解除暂停：先补发积压再发当前
                for p in held: await self._send_marker_or_pcm(ws, p)
                held = []
            await self._send_marker_or_pcm(ws, pcm)

    async def _receiver(self, ws):
        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except Exception:
                break
            try:
                m = json.loads(raw)
            except Exception:
                continue
            if "partial" in m:
                txt = (m.get("partial") or "").strip()
                if txt and self.on_partial:
                    try: self.on_partial(txt)
                    except Exception: logger.exception(f"[{self.tag}] on_partial")
            elif "final" in m:
                txt = (m.get("final") or "").strip()
                audio = self._pop_gate()
                if self.on_final:
                    try: self.on_final(txt, audio)
                    except Exception: logger.exception(f"[{self.tag}] on_final")


class StreamDispatcher:
    """每方向一个：维护"当前句"的 uid/turn，partial 逐词刷同一行，final 过门控后分派处理。
    P0-R1(方向A·通话配音)：partial 稳定前缀到达子句边界即提前提交"语义块"翻译+配音,
    final 只补尾段——长句首音不再等整句说完。直播口型/云S2S/无配音设备时自动关闭。"""
    def __init__(self, direction):
        self.direction = direction
        self.who = "me" if direction == "a" else "other"
        self.uid = None
        self.tid = None
        self._lock = threading.Lock()
        self._last_shown = ""                  # 本句已显示的稳定前缀(wait-k 去重/防回跳用)
        self._sent = None                      # P0-R1 当前句语义块状态(仅方向A使用)

    @staticmethod
    def _waitk_stable(text: str) -> str:
        """隐藏 partial 末 k 个 token(最易被改写的识别尾巴)，只返回稳定前缀。
        CJK 按字、含空格语言按词；不足 k 个 token 时返回空串(尾巴太短→本次先不刷)。k<=0 原样返回。"""
        t = (text or "").strip()
        if STREAM_WAITK <= 0 or not t:
            return t
        if any("\u4e00" <= c <= "\u9fff" for c in t):     # 含中日韩表意字→按字
            return t[:-STREAM_WAITK] if len(t) > STREAM_WAITK else ""
        toks = t.split()                                   # 其余(英文等)→按词
        return " ".join(toks[:-STREAM_WAITK]) if len(toks) > STREAM_WAITK else ""

    def on_partial(self, text):
        with self._lock:
            if self.uid is None:
                # 每句独立 uid+turn(流式每个 auto_eou 段即完整句)，避免同 turn 多句共用一行被 partial 覆盖。
                self.uid = ST.next_uid(); self.tid = ST.next_uid(); self._last_shown = ""
            uid, tid = self.uid, self.tid
        try:                                   # 观测:逐词计数 + 时戳(算实时 partial 速率)
            ST.stream_stats[f"part_{self.direction}"] += 1
            ST.stream_stats["part_ts"].append(time.time())
            _note_voice(self.direction)       # P3-T 语音活跃时戳(礼让/附和/垫场判据)
        except Exception:
            pass
        shown = self._waitk_stable(text)
        if not shown or shown == self._last_shown:   # 尾巴太短 或 稳定前缀未变→不刷(天然降回跳、省事件)
            return
        self._last_shown = shown
        ST.push_event({"uid": uid, "turn": tid, "who": self.who, "live": shown, "partial": True})
        if self.direction == "a":
            try:
                self._maybe_commit_chunk(uid, tid, shown)   # P0-R1 语义块提前配音(异常绝不影响字幕)
            except Exception:
                logger.exception("[语义块] 提交判定异常")

    def _maybe_commit_chunk(self, uid, tid, shown):
        """P0-R1：稳定前缀出现子句边界且长度达标 → 该块立即送 pool_a 处理。
        通话配音链路(play_q)：翻译+提前配音；直播口型链路(P3-L)：只译不配(翻译预取,
        定稿时译文已就绪,口型仍整句触发)。云S2S/开关关闭 → 空操作(整句旧行为)。"""
        if not CHUNK_DUB_ON or ST.s2s_on:
            return
        if ST.live_mode:
            if not LIVE_PREFETCH_ON:
                return                          # 直播不预取(旧行为)
        elif ST.play_q is None:
            return
        st = self._sent
        if st is None or st.get("uid") != uid:
            st = self._sent = {"uid": uid, "tid": tid, "src_done": "", "src_ok": "",
                               "dst": [], "blocked": False, "checked": False,
                               "dub": not ST.live_mode, "t0": time.time()}
        if st["blocked"]:
            return
        if st["src_done"] and not shown.startswith(st["src_done"]):
            st["blocked"] = True          # partial 回改了已提交区 → 本句停止分块(final 兜底)
            return
        rest = shown[len(st["src_done"]):]
        cut = _chunk_cut(rest)
        if not cut:
            return
        chunk = rest[:cut]
        st["src_done"] += chunk
        audio = None
        try:                              # 当前句已积累音频快照(块预检:门控/声纹,不清缓冲)
            sink = ST.stream_sink_a
            if sink is not None:
                audio = sink.peek_gate()
        except Exception:
            audio = None
        try:
            ST.pool_a.submit(_stream_chunk_a, st, uid, tid, chunk, audio)
        except Exception:
            st["blocked"] = True

    def _retract(self, uid, tid):
        ST.push_event({"uid": uid, "turn": tid, "who": self.who, "retract": True})

    def _mark_suspect(self, uid, tid, text, audio, reason, log_msg):
        """P0-④ 标记制：可恢复类拦截不再无声丢弃——灰字上屏 + 送 GER 存疑复核。
        复核确认真话→晋升正稿(误杀救回,补配音)；确认噪声→撤回灰字并计入拦截。
        复核积压/GER 关闭/无音频 → 回退旧行为(硬拦截+计数)。返回 True=已接管。"""
        if not (_GER_ON and _GER_SUSPECT_ON):
            return False
        if audio is None or getattr(audio, "size", 0) < int(SR * 0.3):
            return False
        # 先上灰字(pending 样式,与逐词 partial 同款)：用户可见"听到了、在复核"
        ST.push_event({"uid": uid, "turn": tid, "who": self.who,
                       "live": text, "partial": True, "suspect": True})
        if _ger_submit(self.direction, uid, tid, text, audio, suspect=True, reason=reason):
            logger.info(f"[标记制] 存疑复核中[{self.direction}]({reason}): {text!r} ({log_msg})")
            return True
        self._retract(uid, tid)                      # 复核积压 → 撤灰字,退回旧硬拦截
        return False

    def on_final(self, text, audio):
        with self._lock:
            uid, tid = self.uid, self.tid
            self.uid = None; self.tid = None; self._last_shown = ""
        # P0-R1: 取走本句语义块状态(final 据此只补尾段;已配过音的句子不走存疑改道防重播)
        # P3-L: 预取句(dub=False)没出过声,存疑改道/重译都安全 → dubbed_ahead 只认真出声的
        st = self._sent if (self._sent and self._sent.get("uid") == uid) else None
        self._sent = None
        dubbed_ahead = bool(st and st.get("dst") and st.get("dub", True))
        if st:
            st["blocked"] = True   # 定稿已到:pool 里未执行的块任务不再单独出声,统一并入尾段补配
        if uid is None:
            uid = ST.next_uid(); tid = ST.next_uid()
        text = (text or "").strip()
        if not text:
            if dubbed_ahead:
                ST.chunk_stats["mismatch"] += 1
            self._retract(uid, tid); return
        # ── 硬拦截(不进标记制)：回声/连刷——"放行即事故"(自激循环)的类别 ──
        if (self.direction == "b" or _self_echo_risk_a()) and _is_self_echo(text):
            # 文本回声闸：方向B(环回)永远开；方向A仅在麦可能物理录到自家配音时开
            # (手机麦/半双工/实测耦合)，全双工耳机场景不误杀连说两句相似话的真人。
            ST.dropped += 1; ST.drops["echo"] += 1
            logger.info(f"[回声闸] 丢弃自播回录文本[流式{self.direction}]: {text!r}")
            _note_echo_loop()                # 连续拦到=配音在外部链路绕圈,提示用户排查
            self._retract(uid, tid); return
        if self.direction == "b" and _aux_echo_overlap(audio):   # P2-2 自家外放回录段不进翻译
            ST.dropped += 1; ST.drops["echo"] += 1
            logger.info(f"[回声闸] 丢弃与耳返/朗读外放重叠的流式定稿: {text!r}")
            self._retract(uid, tid); return
        # ── 可恢复类门控 → 标记制(灰字+复核)；标记制不可用时回退旧硬拦截 ──
        #    存疑段跳过此处声纹锁,但晋升前在 _ger_suspect_resolve 里补验(旁人声不能借道)。
        feats = None
        if audio is not None and getattr(audio, "size", 0):
            trimmed = _trim_silence(audio, SR)
            feats = _audio_features(trimmed if trimmed is not None and trimmed.size else audio)
        if feats is not None and not _segment_is_speech(feats, self.direction):
            msg = (f"rms={feats['rms_dbfs']:.1f} peak={feats['peak_dbfs']:.1f} "
                   f"dyn={feats['dyn_db']:.1f}")
            # P0-R1: 已提前配音的句子不走存疑改道——存疑晋升会重新整句配音(同一句话说两遍)
            if not dubbed_ahead and self._mark_suspect(uid, tid, text, audio, "gate", msg):
                return
            if dubbed_ahead:
                ST.chunk_stats["mismatch"] += 1
            ST.dropped += 1; ST.drops["gate"] += 1
            logger.info(f"[流式门控] 丢弃底噪定稿[{self.direction}]: {text!r} ({msg})")
            self._retract(uid, tid); return
        dkey, dmsg = _stt_drop_reason(self.direction, text, feats, None)
        if dkey == "dedup":                          # 连刷循环保护：永远硬拦
            ST.drops["dedup"] += 1
            logger.info(f"丢弃{dmsg}")
            self._retract(uid, tid); return
        if dkey:
            if not dubbed_ahead and self._mark_suspect(uid, tid, text, audio, dkey, dmsg):
                return
            if dubbed_ahead:
                ST.chunk_stats["mismatch"] += 1
            ST.drops["halluc" if dkey == "lang" else dkey] += 1
            logger.info(f"丢弃{dmsg}")
            self._retract(uid, tid); return
        # ── 门控全过的干净语音：声纹锁硬校验(a,防旁人声)/参考音积累(b)，分派正稿处理 ──
        if self.direction == "a" and audio is not None and getattr(audio, "size", 0):
            if _voicelock.ready():
                ok_spk, sim = _voicelock.check(audio)
                if not ok_spk:
                    ST.dropped += 1; ST.drops["spk"] += 1
                    logger.info(f"[声纹锁] 拦截非注册说话人[流式a]: sim={sim:.3f} < {VOICELOCK_THR} {text!r}")
                    self._retract(uid, tid)
                    _spk_blocked_note(uid, tid, text, audio, sim)   # P-Affirm 留证+灰行(可一键放行)
                    return
            else:
                _voicelock_autoenroll(audio)
        elif self.direction == "b":
            _rb_ref_consider(audio, text)            # P2-2 对方通道:过门控的真话可当朗读参考音
        try:
            ST.stream_stats[f"fin_{self.direction}"] += 1     # 观测:有效定稿计数
        except Exception:
            pass
        try:
            if self.direction == "a":
                # chunk_st 随定稿传入:已配块拼终稿+只补尾段(音字一致,免整句重译/重播)
                ST.pool_a.submit(_stream_final_a, uid, tid, text, time.time(), audio, False, st)
            else:
                ST.pool_b.submit(_stream_final_b, uid, tid, text, time.time(), audio)
        except Exception:
            logger.exception(f"[{self.direction}] 流式分派失败")


# ── P0-R1 语义块流式配音：块切分/预检/提前配音/定稿收尾 ─────────────────────
def _chunk_cut(rest: str, min_len: int = None, force_len: int = None) -> int:
    """返回 rest 里可提交为语义块的结尾索引(含标点)；不足返回 0。
    CJK 标点即边界；西文 ,;:.!? 须后随空格/串尾(防拆小数 3.14)。长度 CJK 按字、西文按词。
    无标点连续说话时,达 force_len 在最近空格(CJK 直接全量)强制断；force_len=0 禁用强制断。
    缺省参数=P0-R1 源文分块阈值；P1-S 流式译文切段以更小 min_len 复用同一逻辑。"""
    t = rest or ""
    if not t:
        return 0
    if min_len is None:
        min_len = CHUNK_MIN_LEN
    if force_len is None:
        force_len = CHUNK_FORCE_LEN
    cjk = any("\u4e00" <= c <= "\u9fff" for c in t)

    def _tlen(s: str) -> int:
        return len(_flat_text(s)) if cjk else len(s.split())

    best = 0
    for i, ch in enumerate(t):
        if ch in "，、；：。！？…":
            ok = True
        elif ch in ",;:.!?":
            ok = (i + 1 >= len(t)) or t[i + 1] == " "
        else:
            continue
        if ok and _tlen(t[:i]) >= min_len:
            best = i + 1
    if best:
        return best
    if force_len and _tlen(t) >= force_len:
        if cjk:
            return len(t)
        sp = t.rstrip().rfind(" ")
        return sp if sp > 0 else 0
    return 0


def _stream_chunk_a(st: dict, uid: int, tid: int, chunk: str, audio):
    """P0-R1 语义块提前配音(pool_a 串行,与 final 天然保序)：
    首块预检(能量门控+回声闸+声纹锁,与 final 同源标准) → 翻译(带滚动语境) → 克隆配音。
    只出声音不动字幕(字幕仍由 partial/final 驱动;final 用已配块拼终稿保音字一致)。
    任何失败 → blocked(本句余下退回整句路径),已出声块由 src_ok 记账,final 只补真尾段。
    P3-L st["dub"]=False(直播翻译预取)：只译不配——跳过音频级预检(无提前音频=无风险),
    译文暂存 dst,final 拼终稿后仍整句驱动口型。"""
    try:
        dub = st.get("dub", True)
        if not ST.running or st.get("blocked") or (dub and ST.play_q is None):
            return
        text = (chunk or "").strip()
        if not _flat_text(text):
            return
        if not st.get("checked"):
            if _self_echo_risk_a() and _is_self_echo(text):
                st["blocked"] = True; ST.chunk_stats["blocked"] += 1
                return
            if dub and audio is not None and getattr(audio, "size", 0) >= int(SR * 0.4):
                feats = _audio_features(audio)
                if not _segment_is_speech(feats, "a"):
                    st["blocked"] = True; ST.chunk_stats["blocked"] += 1
                    return
                if _voicelock.ready():
                    ok_spk, sim = _voicelock.check(audio)
                    if not ok_spk:
                        st["blocked"] = True; ST.chunk_stats["blocked"] += 1
                        logger.info(f"[语义块] 声纹预检未过,本句退回整句(final 再裁): sim={sim:.3f}")
                        return
                elif VOICELOCK_ENABLE and VOICELOCK_AUTOENROLL:
                    st["blocked"] = True   # 声纹底座未建成期不冒险提前出声(建成后自动恢复分块)
                    return
            st["checked"] = True
        zh = _collapse_repeats(text)
        en = _collapse_repeats(_translate_nmt(zh, _SRC_LANG, _DST_LANG))
        _llm_emo_take(_DST_LANG)             # 分块句不改道情感引擎:丢弃本块情绪标签(防串染下一句)
        if not en:
            st["blocked"] = True
            return
        if not dub:                          # P3-L 预取:译文暂存,不出声不登记自播
            st["dst"].append(en)
            st["src_ok"] += chunk
            _mt_ctx_note(_SRC_LANG, _DST_LANG, zh, en)
            ST.chunk_stats["prefetch"] = ST.chunk_stats.get("prefetch", 0) + 1
            logger.info(f"[语义块] 直播预取翻译#{len(st['dst'])}: {zh[:24]!r} -> {en[:40]!r}")
            return
        if not st["dst"]:
            ST._tts_first_ts = 0.0           # 本句首块:清首音时戳(P0-R3 埋点起点)
            _turn_hold()                     # P3-T 对方刚开口→句首礼让
            _sent_gap()                      # P2-D1 本句首块:积压时垫呼吸间隙
        st["dst"].append(en)
        st["src_ok"] += chunk                # 记账用原始块(含边界标点/空格,供 final 前缀对齐)
        _mt_ctx_note(_SRC_LANG, _DST_LANG, zh, en)   # 块间语境:下一块指代/术语连贯
        _note_self_output(en)                # 自播文本登记(回声闸按实际播出稿比对)
        _enqueue_synth(en)                   # 平情绪克隆流式(分块句不改道情感引擎,句内音色一致)
        ST.chunk_stats["committed"] += 1
        logger.info(f"[语义块] 提前配音#{len(st['dst'])}: {zh[:24]!r} -> {en[:40]!r}")
    except Exception:
        st["blocked"] = True
        logger.exception("[语义块] 提前配音异常(本句退回整句)")


def _tail_after_flat_prefix(full: str, flat_prefix_len: int):
    """在 full 中找出「规范化后前 flat_prefix_len 个字符」之后的原文尾段。
    覆盖不足返回 None(说明 full 与已配前缀对不上)。"""
    cnt = 0
    for i, ch in enumerate(full):
        if cnt >= flat_prefix_len:
            return full[i:]
        if _flat_text(ch):
            cnt += 1
    return "" if cnt >= flat_prefix_len else None


def _join_dst(parts: list) -> str:
    """按目标语拼接块译文：CJK 无空格,其余以空格连。"""
    d = (_DST_LANG or "").split("-")[0].lower()
    sep = "" if d in ("zh", "ja", "yue") else " "
    return sep.join(p.strip() for p in parts if p and p.strip())


def _finish_chunked_a(zh: str, dubbed_src: str, parts: list) -> str:
    """P0-R1 定稿收尾：已配块之外的真尾段补译+补配,终稿译文=已播块拼接(音字一致)。
    定稿与已配前缀对不上(ASR 回改) → 不再出声(宁少勿重),字幕以已播配音为准。"""
    nd = _flat_text(dubbed_src)
    nz = _flat_text(zh)
    tail = _tail_after_flat_prefix(zh, len(nd)) if (nd and nz.startswith(nd)) else None
    if tail is None:
        ST.chunk_stats["mismatch"] += 1
        logger.warning(f"[语义块] 定稿与已配前缀不一致,尾段不补配(字幕以已播配音为准): "
                       f"dubbed={dubbed_src[:24]!r} final={zh[:24]!r}")
    else:
        tail = tail.strip()
        if _flat_text(tail):
            en_tail = _collapse_repeats(_translate_nmt(tail, _SRC_LANG, _DST_LANG))
            _llm_emo_take(_DST_LANG)         # 尾段同为平情绪路线:丢弃标签防串染
            if en_tail:
                parts.append(en_tail)
                _mt_ctx_note(_SRC_LANG, _DST_LANG, tail, en_tail)
                _note_self_output(en_tail)
                _enqueue_synth(en_tail)
                ST.chunk_stats["tail"] += 1
    return _join_dst(parts)


def _finish_prefetch_a(zh: str, dubbed_src: str, parts: list) -> str:
    """P3-L 直播预取收尾：预取块只译未配 → 终稿译文=预取译文拼接+真尾段补译(NMT 等待≈0)。
    与已播音频零耦合:定稿与预取前缀不一致就整句重译(无风险,只是白付了几次块翻译)。"""
    nd = _flat_text(dubbed_src)
    nz = _flat_text(zh)
    tail = _tail_after_flat_prefix(zh, len(nd)) if (nd and nz.startswith(nd)) else None
    if tail is None:
        ST.chunk_stats["mismatch"] += 1
        return _collapse_repeats(_translate_nmt(zh, _SRC_LANG, _DST_LANG))
    tail = tail.strip()
    if _flat_text(tail):
        en_tail = _collapse_repeats(_translate_nmt(tail, _SRC_LANG, _DST_LANG))
        _llm_emo_take(_DST_LANG)             # 与分块句同策:预取路线不改道情感引擎
        if en_tail:
            parts.append(en_tail)
            _mt_ctx_note(_SRC_LANG, _DST_LANG, tail, en_tail)
            ST.chunk_stats["tail"] += 1
    return _join_dst(parts)


def _stream_final_a(uid: int, tid: int, zh: str, t0: float, audio=None, ger_reviewed: bool = False,
                    chunk_st: dict = None):
    """方向A(我)流式定稿：我方原文已得 → 出原文字幕 → NMT 译对方语言 → 输出(直播=数字人口型/通话=克隆配音)。
    逐词 partial 只刷字幕；直播口型按整句一次触发(最敏感链路不变)。
    P0-R1 通话配音：chunk_st 非空=本句已有语义块提前配音,此处只补尾段并以已播块拼终稿(音字一致)。
    zh/en 变量名沿用历史；语义为 zh=我方原文(SRC 语言)、en=译文(DST 语言)，字段位置不变。
    audio=该句 16k 音频(P0-① GER 异步复核用)；ger_reviewed=True 表示本稿出自复核晋升,不再回送。"""
    try:
        # 句子边界应用角色切换：流式管线不经 _process_a，若不在此处挂钩，
        # 流式模式 /switch_profile 永远不生效(2026-07-08 12:18 实测卡在 pending)。
        _apply_pending_switch()
        _emo_note_audio(audio)               # P1 情感:登记本句原声响度(基调佐证)
        _pros_note(audio, zh)                # P9.1 韵律跟随:本句音高/响度/语速 vs 说话人基线
        zh = _collapse_repeats(zh)           # 复读折叠:同一句话绝不配音两遍
        # 事件同带语义键(src/dst，语言无关，overlay 优先用)与兼容键(zh/en，旧主控台消费)。
        ST.push_event({"uid": uid, "turn": tid, "who": "me", "src": zh, "zh": zh})   # 原文先到(定稿)
        dubbed_src = (chunk_st or {}).get("src_ok") or ""
        parts = list((chunk_st or {}).get("dst") or [])
        got_chunks = bool(dubbed_src and parts)
        prefetched = got_chunks and (chunk_st or {}).get("dub", True) is False
        chunked = got_chunks and not prefetched
        streamed = False
        if chunked:                          # 已提前配音:补尾段,终稿=已播块拼接
            en = _finish_chunked_a(zh, dubbed_src, parts)
        elif prefetched:                     # P3-L 直播预取:拼译文+补尾段,口型仍整句触发
            ST._tts_first_ts = 0.0
            en = _finish_prefetch_a(zh, dubbed_src, parts)
        else:
            ST._tts_first_ts = 0.0           # P0-R3 本句首音时戳起点(整句路径)
            _filler_arm()                    # P1-F 翻译/TTS 超时未出声→克隆音气口垫场
            en = None
            if not ST.live_mode:             # P1-S 流式 LLM 边译边配(仅通话;不适用返回 None)
                en = _translate_dub_stream(zh, _SRC_LANG, _DST_LANG)
                streamed = en is not None
            if en is None:
                en = _collapse_repeats(_translate_nmt(zh, _SRC_LANG, _DST_LANG))
        t_nmt = time.time()
        ST.push_event({"uid": uid, "turn": tid, "who": "me", "dst": en, "en": en,
                       "ms": int((time.time() - t0) * 1000)})
        _mt_ctx_note(_SRC_LANG, _DST_LANG, zh, en)   # P0-R2 滚动语境:整句对(跨句指代/术语一致)
        ST.record_turn_src(tid, "me", _SRC_LANG, zh)
        ST.record_transcript("me", zh, en, tid)
        ST.stats["a"] += 1
        t_syn0 = time.time()
        # 已分块/流式配音的句子不再整句输出(声音已经/正在播),仅整句路径走 _emit_output_a
        av = {} if (chunked or streamed) else (_emit_output_a(en, zh, uid=uid, tid=tid) if en else {})
        if not ger_reviewed:
            _ger_submit("a", uid, tid, zh, audio)   # P0-① 异步纠错复核(配音已出,不加实时延迟)
        m = {"dir": "a", "asr_ms": 0, "nmt_ms": int((t_nmt - t0) * 1000),
             "backlog": (ST.play_q.qsize() if ST.play_q else 0), "ts": time.time()}
        ft = getattr(ST, "_tts_first_ts", 0.0)
        if ft:                               # P0-R3 首音延迟:定稿→首块入播放队列(分块句可为负=提前出声)
            m["tts_first_ms"] = int((ft - t0) * 1000)
        if chunked:
            m["chunks"] = len(parts)
        if prefetched:
            m["prefetch"] = len(parts)       # P3-L 该句预取块数(nmt_ms 应显著缩短)
        if streamed:
            m["llm_stream"] = 1
        if av:
            m.update(av)
        else:
            m["synth_ms"] = int((time.time() - t_syn0) * 1000)
            m["avatar_ms"] = int((time.time() - t_syn0) * 1000) if ST.live_mode else 0
        ST.add_metric(m)
    except Exception:
        logger.exception("方向A 流式定稿异常")


def _stream_final_b(uid: int, tid: int, en: str, t0: float, audio=None, ger_reviewed: bool = False):
    """方向B(对方)流式定稿：对方原文已得 → 出原文 → NMT 译我方语言 → 我方语言字幕(不配音)。
    en/zh 变量名沿用历史；语义为 en=对方原文(DST 语言)、zh=译文(SRC 语言)，字段位置不变。"""
    try:
        _apply_pending_switch()   # 我方静默期只有对方在说话时,切换也能生效(与分段管线对齐)
        en = _collapse_repeats(en)           # 复读折叠:同一句不重复上屏/朗读
        # 事件同带语义键(src/dst)与兼容键(en/zh)。
        ST.push_event({"uid": uid, "turn": tid, "who": "other", "src": en, "en": en})
        zh = _collapse_repeats(_translate_nmt(en, _DST_LANG, _SRC_LANG)); t_nmt = time.time()
        ST.push_event({"uid": uid, "turn": tid, "who": "other", "dst": zh, "zh": zh,
                       "ms": int((time.time() - t0) * 1000)})
        _mt_ctx_note(_DST_LANG, _SRC_LANG, en, zh)   # P0-R2 滚动语境(对方向独立分桶)
        ST.record_turn_src(tid, "other", _DST_LANG, en)
        ST.record_transcript("other", en, zh, tid)
        ST.stats["b"] += 1
        _readback_say(zh)                  # P2-2 对方音色读中文译文(开关关闭时是空操作)
        _backchannel_kick(en)              # P3-B 对方长句后我方久无动静→轻附和"我在听"
        if not ger_reviewed:
            _ger_submit("b", uid, tid, en, audio)   # P0-① 异步纠错复核(字幕稿替换)
        if ST.live_mode:
            _sub_debouncer.push("top", en, zh, _sub_ttl_en(en))   # 直播:顶部=对方(vcam字幕,防抖)
        ST.add_metric({"dir": "b", "asr_ms": 0, "nmt_ms": int((t_nmt - t0) * 1000), "ts": time.time()})
    except Exception:
        logger.exception("方向B 流式定稿异常")


# ══════════ P0-S2S 云同传胶水层：协议客户端(s2s_backends) ↔ 本地管线 ══════════
class _S2SGlueSink:
    """方向A采集汇(feed/flush 接口同 Segmenter/StreamSink)：16k PCM 推云端 S2S，
    回调落地为本地事件流——原文/译文字幕、克隆配音(通话=VB-Cable 逐块流播;直播=整句驱动口型)、
    转写留存、延迟指标。云端任何致命错误 → 一次性故障转移回本地级联：
    环形缓冲(≤8s)回放进 Segmenter,当前句不丢,本场余下时间全走本地。"""

    RING_SEC = 8.0

    def __init__(self, dev_sr: int):
        import s2s_backends as _s2s
        self.dev_sr = int(dev_sr or 48000)
        rc = _s2s_runtime_cfg()
        cfg = _s2s.seed_config_from_env(os.environ)
        cfg["mode"] = rc["mode"] if rc["mode"] in ("s2s", "s2t") else "s2s"
        cfg["speaker_id"] = rc["speaker"]
        cfg["source_language"] = _SRC_LANG
        cfg["target_language"] = _DST_LANG
        gl, hw = self._corpus()
        cfg["glossary"], cfg["hot_words"] = gl, hw
        self.mode = cfg["mode"]
        self._lock = threading.Lock()
        self._fallen = False
        self._seg = None              # 回退后的本地 Segmenter(16k)
        self._ring = deque()          # 最近 16k 音频块(回退回放用)
        self._ring_n = 0
        self._noise = 1e-3            # 抢话打断用的自适应噪声底
        self._in_speech = False
        # 当前句状态(云端事件按句串行)
        self._uid = None
        self._tid = None
        self._src_txt = ""
        self._dst_txt = ""
        self._t0 = 0.0                # 句首(第一个原文事件)
        self._t_src = 0.0             # 原文定稿时刻
        self._t_dst = 0.0             # 译文定稿时刻
        self._t_tts0 = 0.0            # 首块配音时刻
        self._tts_buf = []            # 直播模式整句配音缓冲 [(f32, rate)]
        self._spk_warned = 0.0
        ST.s2s_state = {"backend": "seed", "mode": self.mode, "connected": False,
                        "sentences": 0, "fallbacks": 0, "tts_bytes": 0,
                        "last_error": ""}
        self._client = _s2s.SeedAstClient(
            cfg,
            on_source=self._on_source,
            on_translation=self._on_translation,
            on_tts_chunk=self._on_tts_chunk,
            on_tts_sentence_end=self._on_tts_end,
            on_state=self._on_state,
            on_fail=self._on_fail)
        self._client.start()
        ST.s2s_sink = self
        if _voicelock.ready():
            ST.push_event({"who": "sys", "warn":
                           "ℹ 云同传模式：声纹锁不在此链路生效(云端自带说话人跟踪);"
                           "旁人插话会被翻译,请留意环境"})
        logger.info(f"云同传汇就绪: mode={self.mode} {_SRC_LANG}→{_DST_LANG} "
                    f"speaker={cfg['speaker_id'] or '(复刻说话人)'} 术语={len(gl)} 热词={len(hw)}")

    # ── 语料(术语表→云端 glossary/热词) ─────────────────────────────
    @staticmethod
    def _corpus():
        gl, hw = {}, []
        try:
            for (s, d, _lat) in _glossary_entries(_SRC_LANG, _DST_LANG):
                if s and d and s not in gl:
                    gl[s] = d
                    hw.append(s)
        except Exception:
            pass
        return gl, hw[:50]

    def refresh_corpus(self) -> bool:
        """术语表热更新(POST /config/s2s refresh_glossary=1 时调用)。"""
        if self._fallen or not self._client.connected:
            return False
        gl, hw = self._corpus()
        self._client.update_corpus(gl, hw)
        return True

    # ── 采集接口(Capture 线程调用) ──────────────────────────────────
    def feed(self, block: np.ndarray):
        if block is None or getattr(block, "size", 0) == 0:
            return
        if block.ndim > 1:
            block = block.mean(axis=1)
        if _capture_should_mute("a"):        # 半双工:自家外放期间装聋(与本地汇口径一致)
            block = np.zeros_like(block)
        b16 = _resample(np.asarray(block, np.float32), self.dev_sr, SR)
        if b16.size == 0:
            return
        if self._fallen:
            self._seg.feed(b16)
            return
        # 抢话打断：克隆音正在外放时用户开口 → 与 Segmenter 同款硬停(云端链路同样要跟手)
        rms = float(np.sqrt(np.mean(b16 * b16)) + 1e-9)
        thr = max(self._noise * 3.0, 0.012)
        if rms > thr:
            if not self._in_speech:
                self._in_speech = True
                _trigger_bargein(rms)
        else:
            self._in_speech = False
            self._noise = 0.97 * self._noise + 0.03 * rms
        # 环形缓冲(回退回放当前句用)
        self._ring.append(b16)
        self._ring_n += b16.size
        cap = int(SR * self.RING_SEC)
        while self._ring_n > cap and len(self._ring) > 1:
            self._ring_n -= self._ring.popleft().size
        pcm = (np.clip(b16, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        self._client.feed(pcm)

    def flush(self):
        if self._fallen and self._seg is not None:
            self._seg.flush()
        try:
            self._client.stop()
        except Exception:
            pass
        ST.s2s_state["connected"] = False

    # ── 云端回调(客户端线程) ────────────────────────────────────────
    def _sentence_reset(self):
        self._uid = None; self._tid = None
        self._src_txt = ""; self._dst_txt = ""
        self._t0 = 0.0; self._t_src = 0.0; self._t_dst = 0.0; self._t_tts0 = 0.0

    def _ensure_ids(self):
        if self._uid is None:
            self._uid = ST.next_uid(); self._tid = ST.turn_id("me")
            self._t0 = time.time()
        return self._uid, self._tid

    def _on_source(self, text: str, phase: str, spk_chg: bool):
        try:
            if spk_chg and time.time() - self._spk_warned > 20:
                self._spk_warned = time.time()
                ST.push_event({"who": "sys", "warn": "ℹ 云同传检测到说话人切换"})
            text = (text or "").strip()
            if phase == "start":
                self._sentence_reset()
                self._ensure_ids()
            if not text:
                return
            uid, tid = self._ensure_ids()
            if phase in ("start", "update"):
                ST.push_event({"uid": uid, "turn": tid, "who": "me",
                               "live": text, "partial": True})
            else:                                        # final
                self._src_txt = _collapse_repeats(text)
                self._t_src = time.time()
                ST.push_event({"uid": uid, "turn": tid, "who": "me",
                               "src": self._src_txt, "zh": self._src_txt})
        except Exception:
            logger.exception("S2S 原文回调异常")

    def _on_translation(self, text: str, phase: str):
        try:
            text = (text or "").strip()
            if not text or phase != "final":             # 译文只落定稿(partial 防字幕行抖动)
                return
            uid, tid = self._ensure_ids()
            self._dst_txt = _collapse_repeats(text)
            self._t_dst = time.time()
            ms = int((self._t_dst - (self._t0 or self._t_dst)) * 1000)
            ST.push_event({"uid": uid, "turn": tid, "who": "me",
                           "dst": self._dst_txt, "en": self._dst_txt, "ms": ms})
            if self._src_txt:
                ST.record_turn_src(tid, "me", _SRC_LANG, self._src_txt)
                ST.record_transcript("me", self._src_txt, self._dst_txt, tid)
            ST.stats["a"] += 1
            ST.s2s_state["sentences"] = ST.s2s_state.get("sentences", 0) + 1
            if ST.live_mode:
                _push_subtitle(self._dst_txt, self._src_txt, _sub_ttl_en(self._dst_txt),
                               slot="bottom")
            if self.mode == "s2t":                       # 纯字幕模式:句到此完结
                self._metric_close()
        except Exception:
            logger.exception("S2S 译文回调异常")

    def _on_tts_chunk(self, raw: bytes, rate: int, bits: int):
        try:
            if ST.muted:
                return
            if bits == 32:
                x = np.frombuffer(raw, dtype="<f4").astype(np.float32)
            else:
                x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            if x.size == 0:
                return
            if not self._t_tts0:
                self._t_tts0 = time.time()
            ST.s2s_state["tts_bytes"] = ST.s2s_state.get("tts_bytes", 0) + len(raw)
            if ST.live_mode:
                self._tts_buf.append((x, rate))          # 直播:攒整句驱动口型
                return
            q = ST.play_q                                # 通话:逐块流播(最低首音延迟)
            if q is None:
                return
            d = _resample(x, rate, ST.play_sr) if rate != ST.play_sr else x
            _put_block(q, d, ST.play_ch, fin_ms=4, fout_ms=4, dev_sr=ST.play_sr)
        except Exception:
            logger.exception("S2S 配音块处理异常")

    def _on_tts_end(self):
        try:
            parts = self._tts_buf
            self._tts_buf = []
            if ST.live_mode and parts:
                x = np.concatenate([p for p, _ in parts])
                rate = parts[0][1]
                wav = _to_wav_bytes(x, rate)
                if ST.pool_a is not None:
                    ST.pool_a.submit(self._drive_live, wav)
            self._metric_close()
        except Exception:
            logger.exception("S2S 句配音收尾异常")

    def _drive_live(self, wav: bytes):
        """直播模式:云端整句克隆音 → 口型驱动;口型不可用 → 待机脸纯音频 → VB-Cable 兜底。"""
        try:
            if ST.face_ready and ST.face_id:
                r = _gen_stream_wav(wav)
                if r.get("ok"):
                    return
            if not _push_audio_only(wav):
                _enqueue_wav(wav)
        except Exception:
            logger.exception("S2S 直播口型驱动失败")
            try:
                _enqueue_wav(wav)
            except Exception:
                pass

    def _metric_close(self):
        if not self._t0:
            return
        t0, tsrc, tdst, ttts = self._t0, self._t_src, self._t_dst, self._t_tts0
        asr_ms = int(max(0.0, (tsrc or tdst or time.time()) - t0) * 1000)
        nmt_ms = int(max(0.0, (tdst - tsrc)) * 1000) if (tdst and tsrc) else 0
        syn_ms = int(max(0.0, (ttts - tdst)) * 1000) if (ttts and tdst) else 0
        ST.add_metric({"dir": "a", "s2s": 1, "asr_ms": asr_ms, "nmt_ms": nmt_ms,
                       "synth_ms": syn_ms,
                       "backlog": (ST.play_q.qsize() if ST.play_q else 0),
                       "ts": time.time()})
        self._sentence_reset()

    def _on_state(self, name: str, detail: dict):
        if name == "SessionStarted":
            ST.s2s_state["connected"] = True
            ST.push_event({"who": "sys",
                           "src": "云同传已连接(Seed·方向A识别/翻译/克隆配音云端一体)",
                           "dst": "Cloud S2S connected", "stage": "sys"})
            logger.info("云同传会话已建立")
        elif name == "SessionFinished":
            ST.s2s_state["connected"] = False

    def _on_fail(self, reason: str):
        """一次性故障转移：本场余下时间全走本地级联。环形缓冲回放当前句(不丢话)。"""
        with self._lock:
            if self._fallen:
                return
            self._fallen = True
        ST.s2s_state.update(connected=False, last_error=(reason or "")[:200],
                            fallbacks=ST.s2s_state.get("fallbacks", 0) + 1)
        logger.warning(f"云同传故障转移→本地级联: {reason}")
        ST.push_event({"who": "sys", "warn":
                       f"⚠ 云同传中断({(reason or '')[:60]})，已自动切换本地级联"
                       "(本场持续,重新开播可重试云端)"})
        try:
            seg = Segmenter(SR, lambda s: ST.pool_a.submit(_process_a, s), direction="a")
            for blk in list(self._ring):
                seg.feed(blk)
            self._ring.clear(); self._ring_n = 0
            self._seg = seg
        except Exception:
            logger.exception("故障转移构建本地 Segmenter 失败")
            self._seg = Segmenter(SR, lambda s: ST.pool_a.submit(_process_a, s), direction="a")


def _s2s_make_factory():
    """(factory, why)：factory=None 表示不可用(why 给人看)。所有前置校验集中在此——
    模块可载入、密钥已配、语向受支持、授权档位放行。"""
    rc = _s2s_runtime_cfg()
    name = rc["backend"]
    if name in ("", "none"):
        return None, "未启用(INTERP_S2S_BACKEND=none)"
    if name != "seed":
        return None, f"未知后端 {name}(当前支持: seed)"
    try:
        import s2s_backends as _s2s
    except Exception as e:
        return None, f"s2s_backends 模块加载失败: {e}"
    ok, why = _s2s.seed_config_ready(os.environ)
    if not ok:
        return None, why
    langs = _s2s.seed_config_from_env(os.environ)["langs"]
    if _SRC_LANG not in langs or _DST_LANG not in langs:
        return None, (f"语向 {_SRC_LANG}→{_DST_LANG} 不在云端支持集({','.join(sorted(langs))});"
                      "可用 SEED_S2S_LANGS 扩展")
    try:
        import license as _lic
        if not _lic.allowed("s2s_cloud"):
            return None, "当前授权档位未含云同传(s2s_cloud)"
    except Exception:
        pass                                     # license 模块缺失 → 不拦(与全局软降级一致)
    return (lambda sr: _S2SGlueSink(sr)), ""


def _nemo_reachable() -> bool:
    """探测 nemotron 流式服务是否就绪(模型已加载)，否则自动回退分段管线。"""
    try:
        base = NEMO_WS_URL.replace("ws://", "http://").replace("wss://", "https://")
        j = requests.get(f"{base}/health", timeout=3).json()
        return bool(j.get("loaded"))
    except Exception:
        return False


def _set_whisper_loaded(load: bool):
    """显存优化:流式逐词(Nemotron)模式 Whisper 不参与转写→卸载它给口型/TTS 让显存;
    回退分段模式前→主动预热,避免首句重载延迟。后台尽力而为,失败不影响主流程
    (任何 /transcribe 请求都会自动懒加载兜底)。"""
    def _do():
        try:
            ep = "/asr/load" if load else "/asr/unload"
            r = requests.post(f"{STT_URL}{ep}", timeout=(load and 60 or 10))
            j = r.json()
            ST.asr_unloaded = (not load) and not j.get("loaded", True)   # 观测:卸载是否生效
            logger.info(f"Whisper {'预热' if load else '卸载'}: {j}")
        except Exception as e:
            logger.warning(f"Whisper {'预热' if load else '卸载'}失败(忽略): {e}")
    threading.Thread(target=_do, daemon=True).start()


# ══════════ P3-1 实战调参：核心体验参数运行时可调(通话中拖滑杆即生效)+持久化 ══════════
# 动机：回声闸窗宽/朗读音量/声纹门槛这类参数的"正确值"只有真实通话里才能确定，
# 改 env 重启一次代价太高(掉线重拨)。所有参数在使用点都是"每次读全局"，改全局即时生效。
# 持久化到 data/tuning.json：重启后沿用实战调好的值(优先级高于 env 默认)；可一键回默认。
TUNE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tuning.json")
_TUNABLES = {
    "TTS_OUT_GAIN":  {"label": "克隆音量·给对方", "min": 0.1,  "max": 1.0, "step": 0.05,
                      "desc": "发给对方的克隆语音音量：对方说听不清→调大；说爆音→调小"},
    "MONITOR_GAIN":  {"label": "耳返音量",       "min": 0.0,  "max": 1.5, "step": 0.05,
                      "desc": "克隆音镜像到你耳机的音量"},
    "READBACK_GAIN": {"label": "朗读音量",       "min": 0.0,  "max": 1.5, "step": 0.05,
                      "desc": "对方译文朗读(对方音色)到你耳机的音量"},
    "ECHO_GUARD_S":  {"label": "回声闸窗宽(秒)", "min": 0.0,  "max": 3.0, "step": 0.1,
                      "desc": "自家外放结束后多久内的对方段按回声丢弃：漏回声(自激)→调大；丢对方真话→调小"},
    "VOICELOCK_THR": {"label": "声纹门槛",       "min": 0.30, "max": 0.75, "step": 0.01,
                      "desc": "相似度低于门槛的说话人被拦：误拦你本人→调低；放过旁人→调高"},
    "DENOISE_PROP":  {"label": "谱减降噪强度",   "min": 0.0,  "max": 1.0, "step": 0.05,
                      "desc": "兜底谱减法强度(RNNoise 引擎可用时此项不参与)"},
}
_TUNE_DEFAULTS = {k: float(globals()[k]) for k in _TUNABLES}   # env 计算出的出厂值(重置目标)


def _tune_clamp(name: str, val) -> float:
    m = _TUNABLES[name]
    return max(m["min"], min(m["max"], float(val)))


def _tune_values() -> dict:
    return {k: round(float(globals()[k]), 3) for k in _TUNABLES}


def _tune_save():
    try:
        os.makedirs(os.path.dirname(TUNE_PATH), exist_ok=True)
        with open(TUNE_PATH, "w", encoding="utf-8") as f:
            json.dump({"values": _tune_values(), "saved_at": time.time()}, f,
                      ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("调参持久化失败(不影响本次生效)")


def _tune_load():
    """启动时套用上次实战调好的值。文件缺失/坏 → 保持 env 默认。"""
    try:
        if not os.path.exists(TUNE_PATH):
            return
        with open(TUNE_PATH, "r", encoding="utf-8") as f:
            vals = (json.load(f) or {}).get("values") or {}
        applied = []
        for k, v in vals.items():
            if k in _TUNABLES:
                nv = _tune_clamp(k, v)
                if abs(nv - _TUNE_DEFAULTS[k]) > 1e-9:
                    globals()[k] = nv
                    applied.append(f"{k}={nv}")
        if applied:
            logger.info(f"已套用实战调参(data/tuning.json): {', '.join(applied)}")
    except Exception:
        logger.exception("读取调参文件失败,使用 env 默认")


_tune_load()
_tm_cache_load()      # P4-5 启动恢复持久化翻译缓存(高频句重启后仍即时命中)


# ── FastAPI ───────────────────────────────────────────────────────────
app = FastAPI(title="LiveInterpreter")
try:
    import service_auth                                  # 服务面加固：鉴权 + CORS 收敛（opt-in，默认关，零破坏）
    service_auth.secure(app, name="interpreter")         # 本服务绑 127.0.0.1：默认防护+回环豁免，改绑 0.0.0.0 时即生效
except Exception as _e:
    logger.warning(f"service_auth 未启用: {_e}")


# [2026-07-16 窗口图标] /favicon.ico：Edge/Chrome --app 应用窗口的任务栏/标题栏图标取自
# 页面 favicon 的位图；此前本服务无任何 favicon → 同传窗口退化成 Edge 图标。
# 与 Hub 同一母版（static/app-icon-256.png 随仓库分发），页面 head 均已挂 /favicon.ico。
_FAVICON_PNG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "app-icon-256.png")


@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
    try:
        with open(_FAVICON_PNG, "rb") as f:
            return Response(f.read(), media_type="image/png",
                            headers={"Cache-Control": "no-cache"})
    except Exception:
        return Response(status_code=404)


# ── 设备表防漂移：MME waveIn/waveOut ID 是位置序号,虚拟声卡(DroidCam/SplitCam/iVCam)动态
# 注册/注销后整表移位;本进程 PortAudio 快照冻结在启动时刻 → 按旧索引开的是"漂移后的别的设备"
# (实测联测复现:自检"麦克风正常 -96.7dBFS"实际开的是 CABLE Output 静音口)。绑定前重建快照。
_pa_refresh_state = {"t": 0.0}
_pa_lock = threading.Lock()   # terminate→initialize 的 ~150ms 窗口内任何 sd 调用都会 -10000


def _pa_refresh(force: bool = False) -> bool:
    """重建 PortAudio 设备快照(会话运行中禁止——会拔掉活动流)。3s 节流防抖。"""
    if ST.running:
        return False
    now = time.time()
    if not force and now - _pa_refresh_state["t"] < 3.0:
        return True
    try:
        with _pa_lock:
            sd._terminate()
            time.sleep(0.15)
            sd._initialize()
        _pa_refresh_state["t"] = time.time()
        logger.info("PortAudio 设备快照已重建(防索引漂移)")
        return True
    except Exception:
        logger.exception("PortAudio 快照重建失败")
        return False


def _find_device_all(name_sub: str, want_output: bool) -> list:
    """按名字列出同名设备的全部可用实例(排除 WDM-KS)，按 MME>WASAPI>DS 偏好排序。
    动机：宿主API实例会单独"坏死"——实测强杀持流进程后,PD100X 的 MME 实例整机读数字
    静音(-96.7dBFS,连新开进程都是),同名 WASAPI/DS 实例却正常。给上层留备选清单。"""
    pref = {"MME": 0, "Windows WASAPI": 1, "Windows DirectSound": 2}
    cands = []
    for i, d in enumerate(sd.query_devices()):
        if name_sub.lower() in d["name"].lower():
            ch = d["max_output_channels"] if want_output else d["max_input_channels"]
            if ch > 0:
                ha = sd.query_hostapis(d["hostapi"])["name"]
                if ha in pref:
                    cands.append((pref[ha], i))
    cands.sort()
    return [i for _, i in cands]


def _find_device(name_sub: str, want_output: bool):
    """按名字找设备。硬性排除 WDM-KS：回调式流在它上面必 -9999(WdmSyncIoctl,实测 BRIO 复现,
    且设备热插拔/默认切换后索引会漂到 KS 实例)。偏好 MME(最兼容)>WASAPI>DirectSound,
    与 _find_loopback 同一套经过实战的次序。"""
    cands = _find_device_all(name_sub, want_output)
    return cands[0] if cands else None


def _find_loopback():
    """选环回采集设备(立体声混音/Stereo Mix)。回调式采集在 WDM-KS 上会 -9999 打不开，
    故排除 WDM-KS；经验上 MME 最稳，其次 WASAPI、DirectSound。"""
    pref = {"MME": 0, "Windows WASAPI": 1, "Windows DirectSound": 2}
    cands = []
    for i, d in enumerate(sd.query_devices()):
        nm = d["name"]
        if d["max_input_channels"] > 0 and ("立体声混音" in nm or "stereo mix" in nm.lower()):
            ha = sd.query_hostapis(d["hostapi"])["name"]
            if ha in pref:
                cands.append((pref[ha], i))
    cands.sort()
    return cands[0][1] if cands else None


def _wasapi_loopback_ok():
    """本机 sounddevice 是否支持 WasapiSettings(loopback=)。结果缓存(进程级不变)。"""
    c = getattr(_wasapi_loopback_ok, "_v", None)
    if c is None:
        try:
            sd.WasapiSettings(loopback=True); c = True
        except Exception:
            c = False
        _wasapi_loopback_ok._v = c
    return c


@app.get("/devices")
def devices():
    _pa_refresh()          # 非会话期重建快照:虚拟声卡热插拔后旧索引全表漂移(3s 节流)
    inputs, outputs = [], []
    for i, d in enumerate(sd.query_devices()):
        ha = sd.query_hostapis(d["hostapi"])["name"]
        item = {"index": i, "name": d["name"], "hostapi": ha,
                "sr": int(d.get("default_samplerate") or 0)}
        if d["max_input_channels"] > 0:
            inputs.append(item)
        if d["max_output_channels"] > 0:
            outputs.append(item)
    # 智能默认:优先按名绑定的通话麦(INTERP_MIC_NAME)——按"第一个含『麦克风』"选会命中
    # 摄像头麦(BRIO 排在 PD100X 前)，实测 2026-07-09 用户对着 PD100X 说话无一句识别。
    mic = (_find_device(CALL_MIC_NAME, False)
           or _find_device("Realtek HD Audio Mic", False) or _find_device("麦克风", False))
    cable = _find_device("CABLE Input", True)
    loop = _find_loopback()
    return {"inputs": inputs, "outputs": outputs,
            "stereo_mix": loop,                 # 立体声混音(抓对方声的推荐设备)；None=本机未启用
            "loopback_ok": _wasapi_loopback_ok(),  # 本机是否支持 WASAPI 环回(否则只能用立体声混音)
            "defaults": {"mic": mic, "cable": cable, "loopback": loop,
                         "loopback_is_output": loop is None}}


# ══════════ P1-1 一键通话模式：设默认设备 → 按名绑定 → 链路自检 → 开播 → 就绪报告 ══════════
# 消灭"设备接错只能等对方说听不到"：一个调用完成全部通话准备，每步红绿灯可见。
CALL_MIC_NAME = os.environ.get("INTERP_MIC_NAME", "BRIO")   # 「我的麦」按名绑定(索引会漂,名字不会)


def _set_default_mic(target: str = "CABLE Output") -> dict:
    """subprocess 跑 set_default_mic.py(comtypes 需干净进程)。把系统默认录音切到 target。"""
    import subprocess
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "set_default_mic.py")
    try:
        r = subprocess.run([sys.executable, "-X", "utf8", script, target],
                           capture_output=True, text=True, encoding="utf-8", timeout=25,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (r.stdout or "").strip().splitlines()
        return json.loads(out[-1]) if out else {"ok": False, "error": r.stderr[-200:]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _audio_probe(kind: str, idx: int, name: str = "") -> dict:
    """声卡链路自检走独立进程(audio_path_check.py)：默认设备刚切换过时宿主进程的
    PortAudio 快照可能失效，且中文 Windows 上 sd 错误文本是 GBK、宿主里解码会炸。
    传设备名让子进程按名重解析：父子进程各自初始化 PortAudio,索引可能对不上
    (实测父进程 BRIO 的索引在子进程快照里是 Voicemeeter B3 静音口→自检误报)。"""
    import subprocess
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio_path_check.py")
    try:
        r = subprocess.run([sys.executable, "-X", "utf8", script, kind, str(idx), name or "",
                            _dev_hostapi_name(idx)],
                           capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = r.stdout.decode("utf-8", errors="replace").strip().splitlines()
        return json.loads(out[-1]) if out else {"ok": False, "detail": r.stderr.decode("utf-8", errors="replace")[-160:]}
    except Exception as e:
        return {"ok": False, "detail": f"{kind} 自检子进程异常: {str(e)[:120]}"}


def _cable_path_test(cable_out_idx: int) -> dict:
    """端到端通路自检：往 CABLE Input 放 0.4s 测试音、同时录 CABLE Output(微信的收音口)。
    峰值达标=对方一定能听到。"""
    return _audio_probe("cable", cable_out_idx, _dev_name_safe(cable_out_idx))


def _mic_open_test(mic_idx: int) -> dict:
    """麦克风可用性自检：能打开=设备正常，顺带报底噪。"""
    return _audio_probe("mic", mic_idx, _dev_name_safe(mic_idx))


# ── 端到端「试音」：合成一句真实克隆音 → 推 CABLE → 回录量峰值 → 一盏灯验成 音色+引擎+通路 ──
# 动机(2026-07-10 无声事故复盘)：此前"配音就绪"只静态查(有音色样本 + 引擎 /health 可达)，
# 但真正决定"对方能听到"的是三件事串成一条线：①音色取得到 ②引擎能合成出波形 ③波形能原样
# 到达 CABLE Output(微信/TG 的收音口)。任一环断=对方无声。试音把这条线跑一遍真数据,给一盏灯。
_DUB_PROBE_TEXT = {
    "en": "Hi, this is a quick voice test.",
    "ja": "これは音声テストです。",
    "ko": "이것은 음성 테스트입니다.",
    "ru": "Это проверка голоса.",
    "es": "Esto es una prueba de voz.",
    "fr": "Ceci est un test de voix.",
    "de": "Dies ist ein Sprachtest.",
    "zh": "这是一句配音测试。",
    "yue": "呢句係配音測試。",
}


def _dub_probe_text() -> str:
    return _DUB_PROBE_TEXT.get(_DST_LANG, _DUB_PROBE_TEXT["en"])


# 最近一次试音结果(常驻红绿灯用)：让桌面/手机随时看到"对方最近一次能否听到 + 峰值 + 多久前"，
# 不必每次现点。仅内存(重启即清,符合"当次通话"语义)。
_last_dub = {"ts": 0.0, "ok": None, "peak_dbfs": None, "synth_ms": None, "via": "", "stage": "", "reason": ""}


def _record_last_dub(res: dict):
    _last_dub.update({"ts": time.time(), "ok": res.get("ok"), "peak_dbfs": res.get("peak_dbfs"),
                      "synth_ms": res.get("synth_ms"), "via": res.get("via", ""),
                      "stage": res.get("stage", ""), "reason": res.get("reason", "")})


def _run_audio_check(args: list, timeout: int = 30) -> dict:
    """跑 audio_path_check.py(独立 PortAudio,规避默认设备切换后的快照失效/中文 GBK 崩溃)。"""
    import subprocess
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio_path_check.py")
    try:
        r = subprocess.run([sys.executable, "-X", "utf8", script, *[str(a) for a in args]],
                           capture_output=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = r.stdout.decode("utf-8", errors="replace").strip().splitlines()
        return json.loads(out[-1]) if out else {"ok": False, "detail": r.stderr.decode("utf-8", errors="replace")[-160:]}
    except Exception as e:
        return {"ok": False, "detail": f"试音子进程异常: {str(e)[:120]}"}


def _dub_test_duplex(wav_bytes: bytes, cable_idx: int) -> dict:
    """会话未起：把合成 WAV 双工推入 CABLE Input 并回录 CABLE Output(子进程,干净 PortAudio)。"""
    import tempfile
    fp = None
    try:
        f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        f.write(wav_bytes); f.close(); fp = f.name
        return _run_audio_check(["cablewav", cable_idx, fp, _dev_name_safe(cable_idx),
                                 _dev_hostapi_name(cable_idx)], timeout=30)
    finally:
        if fp:
            try:
                os.remove(fp)
            except Exception:
                pass


def _dub_test_silent() -> dict:
    """通话中静默核对：只回录 CABLE Output 当前电平(不注入任何测试音,绝不打扰对方)。
    CABLE Output 就是对方的收音口,任何注入对方都会听到——故通话中默认走这条:合成已单独验过
    (音色+引擎),这里只确认 CABLE 收音口可读并报当前电平(说话时应明显高于此底噪)。"""
    return _run_audio_check(["reccable", "1.5"], timeout=12)


def _dub_test_via_queue(wav_bytes: bytes) -> dict:
    """会话进行中：不与播放线程抢 CABLE 输出流——把合成音投进播放队列(真实出声链路)，
    同时起子进程仅回录 CABLE Output。按绝对峰值判(垫层底噪≤-38，合成-6~-12,-30 阈值可分)。"""
    import subprocess
    x, wsr = _wav_bytes_to_f32(wav_bytes)
    x = _resample(x, wsr, ST.play_sr) if wsr != ST.play_sr else x
    if ST.play_ch >= 2:
        x = np.column_stack([x] * ST.play_ch)
    dur = max(2.5, len(x) / max(1, ST.play_sr) + 2.0)
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio_path_check.py")
    try:
        p = subprocess.Popen([sys.executable, "-X", "utf8", script, "reccable", f"{dur:.1f}"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        return {"ok": False, "detail": f"回录子进程起不来: {str(e)[:100]}"}
    time.sleep(1.3)                                  # 等录音端进入采集再投音(否则可能录不到)
    was_muted = ST.muted
    ST.muted = False                                 # 试音期间临时解急停(否则整块被播放线程丢弃)
    try:
        ST.play_q.put_nowait(np.ascontiguousarray(x, dtype=np.float32))
    except Exception:
        pass
    try:
        out, _err = p.communicate(timeout=int(dur) + 8)
        res = json.loads((out.decode("utf-8", errors="replace").strip().splitlines() or ["{}"])[-1])
    except Exception as e:
        res = {"ok": False, "detail": f"回录读取失败: {str(e)[:100]}"}
        try:
            p.kill()
        except Exception:
            pass
    finally:
        ST.muted = was_muted
    peak = res.get("peak_dbfs")
    if peak is not None:                             # 运行态用绝对峰值 -30 判(垫层底噪压不过阈值)
        res["ok"] = float(peak) > -30.0
    return res


def _dub_test(profile: str = "", silent=None) -> dict:
    """端到端试音：真实合成一句 → 推 CABLE → 回录量峰值。返回单灯 {ok, stage, reason, ...}。
    stage 标出断在哪一环：voice(无音色) / engine(合成失败) / device(无CABLE) / path(推不到对方)。
    silent=None 智能默认：通话中(session 已起)默认静默(只验合成+回录电平,不注入对方)；
    未开播默认整条通路双工验证。silent=True/False 可显式覆盖。"""
    t0 = time.time()
    running = bool(ST.running)
    silent_eff = (running if silent is None else bool(silent))
    saved = (ST.voice_b64, ST.ref_text)              # 运行中用已载音色;未起时按角色拉取(用后恢复)
    prof = (profile or getattr(ST, "profile", "") or "").strip()
    if not running:
        vb, rt = _fetch_voice_ref(prof)
        ST.voice_b64, ST.ref_text = vb, rt
    try:
        if not _voice_ready():
            return {"ok": False, "stage": "voice", "reason": _voice_probe_hint(prof),
                    "ms": int((time.time() - t0) * 1000)}
        text = _dub_probe_text()
        try:
            b64 = _synth_en(text)                     # 真合成一句 → 一次验 音色+引擎 两半
        except Exception as e:
            return {"ok": False, "stage": "engine",
                    "reason": f"配音引擎合成失败：{str(e)[:140]}——请确认 Fish/CosyVoice 服务在跑",
                    "ms": int((time.time() - t0) * 1000)}
        raw = _b64bytes(b64)
        if len(raw) < 400:
            return {"ok": False, "stage": "engine", "reason": "引擎返回空音频(参考音无效或合成异常)",
                    "ms": int((time.time() - t0) * 1000)}
        synth_ms = int((time.time() - t0) * 1000)
        cable = _find_device("CABLE Input", True)
        if cable is None:
            return {"ok": False, "stage": "device", "synth_ms": synth_ms,
                    "reason": "未找到 CABLE Input 输出设备：请确认 VB-Cable 已安装",
                    "ms": int((time.time() - t0) * 1000)}
        _h = _tts_engine_health()                   # 兜底出声也算成功,但若主引擎挂了要顺带提醒(丢情感/延迟高)
        _bak = (f"（注意：主引擎「{_h['primary']}」未在线，当前走兜底，点『拉起配音引擎』可恢复）"
                if _h.get("primary") and _h.get("primary_ok") is False else "")
        if silent_eff:
            # 通话中静默：合成已验过(音色+引擎)，这里只回录 CABLE 当前电平,绝不注入对方
            lvl = _dub_test_silent()
            peak = lvl.get("peak_dbfs")
            return {"ok": True, "stage": "synth_ok", "peak_dbfs": peak, "synth_ms": synth_ms,
                    "text": text, "via": "silent", "ms": int((time.time() - t0) * 1000),
                    "reason": (f"✅ 配音可合成(音色+引擎就绪，合成 {synth_ms}ms)。通话中为不打扰对方，"
                               f"未向对方收音口注入测试音；当前 CABLE 电平 {peak}dBFS(你说话时应明显更高)。"
                               f"要完整验证整条通路，请挂断后再点试音。" + _bak)}
        res = _dub_test_via_queue(raw) if (running and ST.play_q is not None) else _dub_test_duplex(raw, cable)
        peak, ok = res.get("peak_dbfs"), bool(res.get("ok"))
        if ok:
            reason = (f"✅ 试音成功：克隆音已推到对方收音口(peak {peak}dBFS，合成 {synth_ms}ms)。"
                      + ("通话中已把这句播给对方" if running else "开播后对方即可听到") + _bak)
            stage = "done"
        else:
            reason = (f"合成正常但没推到 CABLE Output(peak {peak}dBFS)：{res.get('detail','')}"
                      "——检查 VB-Cable 是否被别的程序独占、微信/TG 收音口是否选 CABLE Output")
            stage = "path"
        return {"ok": ok, "stage": stage, "reason": reason, "peak_dbfs": peak, "synth_ms": synth_ms,
                "text": text, "via": ("queue" if running else "duplex"), "ms": int((time.time() - t0) * 1000)}
    finally:
        if not running:
            ST.voice_b64, ST.ref_text = saved


def _dev_family(name: str) -> str:
    """取设备的物理归属标识：优先括号内的设备名(如 '麦克风 (PD100X...)' → 'pd100x...')，
    用于判断"我的麦"与"对方声来源"是否同一块物理声卡(同族=自听风险)。"""
    if not name:
        return ""
    m = _re.findall(r"\(([^()]+)\)", name)
    base = (m[-1] if m else name).strip().lower()
    return base


# ══════════ P8-1 设备方案中心：三套场景预设(手机随身/电脑直连/耳机专业)+自定义 ══════════
# 统一管住五个设备角色：我的麦 / 摄像头 / 监听出口(耳返+朗读) / 对方声来源 / 克隆音出口。
# mic: 设备名子串 或 "phone"(手机麦无线直连中继 /mic/pcm)
# listen: "default"(默认输出=音箱/Voicemeeter) / "headset"(按名找耳机) / "phone"(手机监听,
#         物理上仍播默认输出→中继环回抓给手机,故解析同 default) / 设备名子串
# half_duplex: "auto"(P8-2 声学耦合自检实测决定) / "on" / "off"
AUDIO_PROFILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "data", "audio_profiles.json")
_AP_BUILTINS = {
    "pc":      {"label": "电脑直连", "icon": "🖥", "mic": CALL_MIC_NAME, "cam": "pc",
                "listen": "default", "half_duplex": "auto",
                "desc": "电脑麦+摄像头，音箱听。开播自检实测音箱↔麦是否互通，互通自动轮流说话"},
    "phone":   {"label": "手机随身", "icon": "📱", "mic": "phone", "cam": "phone",
                "listen": "phone", "half_duplex": "auto",
                "desc": "手机当麦+摄像头+监听。手机插耳机=全双工；外放=自动轮流说话防回声"},
    "headset": {"label": "耳机专业", "icon": "🎧", "mic": CALL_MIC_NAME, "cam": "pc",
                "listen": "headset", "half_duplex": "auto",
                "desc": "耳机听(声学隔离=全双工)。说话建议用电脑麦：蓝牙耳机开麦会掉到电话音质"},
}
_ap_state = {"active": "pc", "profiles": {}}
_MONITOR_OUT_OVERRIDE = None      # 监听出口按名覆盖(耳机模式)；None=默认输出


def _ap_load():
    global _ap_state, _MONITOR_OUT_OVERRIDE
    prof = {k: dict(v) for k, v in _AP_BUILTINS.items()}
    active = "pc"
    try:
        if os.path.exists(AUDIO_PROFILE_PATH):
            d = json.load(open(AUDIO_PROFILE_PATH, encoding="utf-8")) or {}
            active = d.get("active") or "pc"
            for k, patch in (d.get("profiles") or {}).items():
                base = prof.get(k) or {}
                base.update({kk: vv for kk, vv in (patch or {}).items() if vv is not None})
                prof[k] = base
    except Exception:
        logger.exception("audio_profiles.json 读取失败,用内置预设")
    if active not in prof:
        active = "pc"
    _ap_state = {"active": active, "profiles": prof}
    _ap_apply_globals()


def _ap_save():
    try:
        os.makedirs(os.path.dirname(AUDIO_PROFILE_PATH), exist_ok=True)
        with open(AUDIO_PROFILE_PATH, "w", encoding="utf-8") as f:
            json.dump({"active": _ap_state["active"], "profiles": _ap_state["profiles"],
                       "saved_at": time.time()}, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("audio_profiles.json 保存失败(本次仍生效)")


def _ap_get(name: str = None) -> dict:
    return dict(_ap_state["profiles"].get(name or _ap_state["active"]) or _AP_BUILTINS["pc"])


def _ap_apply_globals():
    """把当前方案落到运行全局：监听出口覆盖(耳机按名)/半双工强制值。auto 留给开播探针定。"""
    global _MONITOR_OUT_OVERRIDE, HALF_DUPLEX_SPK
    p = _ap_get()
    _MONITOR_OUT_OVERRIDE = None
    if p.get("listen") == "headset":
        _MONITOR_OUT_OVERRIDE = "headset"
    elif p.get("listen") not in (None, "", "default", "phone"):
        _MONITOR_OUT_OVERRIDE = str(p["listen"])
    hd = p.get("half_duplex")
    if hd == "on":
        HALF_DUPLEX_SPK = True
    elif hd == "off":
        HALF_DUPLEX_SPK = False


_HEADSET_PAT = ("耳机", "headphone", "headset", "earphone", "airpods", "buds", "arctis", "hyperx")
_VIRTUAL_PAT = ("voicemeeter", "cable", "vb-audio", "virtual", "droidcam", "splitcam", "obs")


def _find_headset_out():
    """按名找真实耳机输出(排除虚拟声卡)。MME 优先。找不到 → None。"""
    pref = {"MME": 0, "Windows DirectSound": 1, "Windows WASAPI": 2}
    cands = []
    try:
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] <= 0:
                continue
            nm = (d.get("name") or "").lower()
            ha = sd.query_hostapis(d["hostapi"])["name"]
            if ha not in pref or any(v in nm for v in _VIRTUAL_PAT):
                continue
            if any(h in nm for h in _HEADSET_PAT):
                cands.append((pref[ha], i))
        cands.sort()
        return cands[0][1] if cands else None
    except Exception:
        return None


def _ap_resolve(name: str = None) -> dict:
    """把方案解析成具体设备(不开流)：每一路 {ok, name/index, note}。供 UI 状态灯与开播绑定。
    与 _pa_refresh 互斥：快照重建的 ~150ms 窗口内查询设备会 PortAudio -10000。"""
    with _pa_lock:
        return _ap_resolve_inner(name)


def _ap_resolve_inner(name: str = None) -> dict:
    p = _ap_get(name)
    legs = {}
    # 我的麦
    if p.get("mic") == "phone":
        relay_ok = False
        try:
            relay_ok = requests.get(f"{MONITOR_URL}/health", timeout=2).status_code == 200
        except Exception:
            pass
        legs["mic"] = {"ok": relay_ok, "kind": "net", "name": "手机麦(无线直连)",
                       "note": "" if relay_ok else f"手机中继({_MON_PORT})不在线"}
    else:
        idx = _find_device(str(p.get("mic") or CALL_MIC_NAME), False) or _find_device("麦克风", False)
        legs["mic"] = {"ok": idx is not None, "kind": "device", "index": idx,
                       "name": _dev_name_safe(idx) or "", 
                       "note": "" if idx is not None else f"未找到含 {p.get('mic')!r} 的输入设备"}
    # 监听出口(耳返/朗读;手机监听物理上也走默认输出→中继环回)
    if p.get("listen") == "headset":
        hs = _find_headset_out()
        legs["listen"] = {"ok": hs is not None, "index": hs, "name": _dev_name_safe(hs) or "",
                          "note": "" if hs is not None else "未找到耳机设备(插耳机后重试);暂用默认输出"}
    else:
        dev, _sr, _ch = _resolve_monitor_out()
        legs["listen"] = {"ok": True, "index": dev, "name": _dev_name_safe(dev) or "系统默认输出",
                          "note": "手机监听经中继转发此出口" if p.get("listen") == "phone" else ""}
    # 克隆音出口(固定 CABLE)
    cable = _find_device("CABLE Input", True)
    legs["dub_out"] = {"ok": cable is not None, "index": cable, "name": _dev_name_safe(cable) or "",
                       "note": "" if cable is not None else "未找到 CABLE Input(VB-Cable 未装?)"}
    # 摄像头(信息位:换脸源在枢纽/推流侧选择)
    legs["cam"] = {"ok": True, "name": ("手机摄像头(WebRTC)" if p.get("cam") == "phone" else "电脑摄像头"),
                   "note": "开播页/手机页选择实际画面源"}
    return {"profile": name or _ap_state["active"], "legs": legs}


class APReq(BaseModel):
    active: str = None                 # 切换方案
    name: str = None                   # 配合 patch:改哪套
    patch: dict = None                 # {mic/listen/cam/half_duplex/...}


@app.get("/audio_profile")
def audio_profile_get():
    return {"ok": True, "active": _ap_state["active"], "profiles": _ap_state["profiles"],
            "resolved": _ap_resolve(), "half_duplex_now": HALF_DUPLEX_SPK,
            "coupling": getattr(ST, "coupling", None),
            "running": ST.running,
            "note": "会话运行中切换方案将于下次开播生效" if ST.running else ""}


@app.post("/audio_profile")
def audio_profile_set(req: APReq):
    if req.active:
        if req.active not in _ap_state["profiles"]:
            return JSONResponse({"ok": False, "detail": f"无此方案: {req.active}"}, 400)
        _ap_state["active"] = req.active
    if req.name and req.patch:
        base = _ap_state["profiles"].get(req.name) or {}
        allowed = {"label", "icon", "mic", "cam", "listen", "half_duplex", "desc"}
        base.update({k: v for k, v in req.patch.items() if k in allowed})
        _ap_state["profiles"][req.name] = base
    _ap_apply_globals()
    _ap_save()
    p = _ap_get()
    ST.push_event({"who": "sys", "warn": f"🎛 设备方案已切换：{p.get('icon','')} {p.get('label','')}"
                   + ("（会话运行中，下次开播生效）" if ST.running else "")})
    return audio_profile_get()


# ══════════ P8-2 声学耦合自检：啁啾探针实测「监听出口 ↔ 我的麦」是否互通 ══════════
# 物理测量取代猜测：往监听出口播两声不同频率的短音,同时测麦克风对应频段能量。
# 互通(音箱外放灌麦)→ 半双工轮流说话；隔离(耳机)→ 全双工。~4 秒出结论。
# 频率取语音带内(1k/1.6k):高频易踩房间/音箱陷波(实测 2350Hz 同机三跑漂 -8~+13dB)。
_CHIRP_F = (1000.0, 1600.0)
_COUPLE_DB = float(os.environ.get("INTERP_COUPLE_DB", "10"))         # 双频段都过 → 耦合
_COUPLE_DB_HI = float(os.environ.get("INTERP_COUPLE_DB_HI", "18"))   # 单频段强过 → 也判耦合
# 判定不对称的动机：误判"耦合"只是多开半双工(轮流说话,无害)；误判"隔离"会开全双工→自激回声(有害)。


def _band_db(x: np.ndarray, sr: int, f0: float, bw: float = 120.0) -> float:
    """频段能量(dB)。Goertzel 式窄带测量:对指定频率±bw 的 FFT bin 求和。"""
    if x is None or len(x) < int(sr * 0.05):
        return -120.0
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1.0 / sr)
    m = (freqs >= f0 - bw) & (freqs <= f0 + bw)
    e = float(np.sum(spec[m] ** 2)) / max(1, int(m.sum()))
    return 10.0 * np.log10(e + 1e-12)


def _band_db_windows(x: np.ndarray, sr: int, f0: float, win_s: float = 0.4, hop_s: float = 0.1):
    """滑窗频段能量序列(dB)。"""
    win, hop = int(sr * win_s), int(sr * hop_s)
    out = []
    for off in range(0, max(1, len(x) - win), hop):
        out.append(_band_db(x[off: off + win], sr, f0))
    return out or [-120.0]


def _coupling_probe(mic_idx: int = None, mic_net_url: str = "", out_idx: int = None) -> dict:
    """播 0.5s×2 双频音到监听出口,同时录麦 → 每频段「播放峰值 vs 底噪中位」能量差。
    底噪取滑窗中位(抗键盘/鼠标瞬态)；双频段≥10dB 或单频段≥18dB = 声学耦合。
    会话运行中禁用(设备被占,且无必要)。"""
    if ST.running:
        return {"ok": False, "detail": "会话运行中不可探测"}
    try:
        out_sr, out_ch = _dev_out_params(out_idx) if out_idx is not None else _default_out_params()
    except Exception:
        out_sr, out_ch = 48000, 2
    # 采集侧:设备 or 手机网络流
    cap = {"buf": [], "sr": 16000, "err": None}
    stop_cap = threading.Event()

    def _cap_device():
        try:
            di = sd.query_devices(mic_idx)
            csr = int(di.get("default_samplerate") or 48000)
            cch = min(2, max(1, int(di.get("max_input_channels") or 1)))   # WASAPI 实例常 2ch,强开 1ch 会打不开
            cap["sr"] = csr
            with sd.InputStream(device=mic_idx, channels=cch, samplerate=csr,
                                blocksize=int(csr * 0.05), dtype="float32") as st_:
                while not stop_cap.is_set():
                    d, _ = st_.read(int(csr * 0.05))
                    d = np.asarray(d, np.float32)
                    cap["buf"].append(d.mean(axis=1) if d.ndim > 1 and d.shape[1] > 1 else d.reshape(-1))
        except Exception as e:
            cap["err"] = str(e)[:160]

    def _cap_net():
        try:
            with requests.get(mic_net_url, stream=True, timeout=(3, 8)) as r:
                cap["sr"] = int(r.headers.get("X-Sample-Rate") or 48000)
                raw = b""
                for chunk in r.iter_content(chunk_size=4096):
                    if stop_cap.is_set():
                        break
                    raw += chunk or b""
                    n = (len(raw) // 2) * 2
                    if n:
                        cap["buf"].append(np.frombuffer(raw[:n], dtype="<i2").astype(np.float32) / 32768.0)
                        raw = raw[n:]
        except Exception as e:
            cap["err"] = str(e)[:160]

    th = threading.Thread(target=_cap_net if mic_net_url else _cap_device, daemon=True)
    th.start()
    time.sleep(1.2)                                   # 前 1.2s 静默=底噪窗
    try:
        t1 = np.linspace(0, 0.5, int(out_sr * 0.5), False)
        gap = np.zeros(int(out_sr * 0.25), np.float32)
        tone = lambda f: (0.5 * np.sin(2 * np.pi * f * t1) *
                          np.concatenate([np.linspace(0, 1, int(out_sr * 0.02)),
                                          np.ones(len(t1) - int(out_sr * 0.04)),
                                          np.linspace(1, 0, int(out_sr * 0.02))])).astype(np.float32)
        sig = np.concatenate([tone(_CHIRP_F[0]), gap, tone(_CHIRP_F[1])])
        if out_ch >= 2:
            sig = np.column_stack([sig] * out_ch)
        sd.play(sig, out_sr, device=out_idx)
        sd.wait()
    except Exception as e:
        stop_cap.set(); th.join(timeout=2)
        return {"ok": False, "detail": f"探测音播放失败: {str(e)[:120]}"}
    time.sleep(1.0 if mic_net_url else 0.4)           # 手机路径留中继+WS 延迟余量
    stop_cap.set(); th.join(timeout=3)
    if cap["err"] or not cap["buf"]:
        return {"ok": False, "detail": f"探测录音失败: {cap['err'] or '无数据'}"}
    x = np.concatenate(cap["buf"]); sr = cap["sr"]
    if len(x) < sr * 1.8:
        return {"ok": False, "detail": f"录音过短({len(x)/sr:.1f}s)"}
    # 数字静音守卫：采到全零(刚停会话的设备释放瞬态/开错静音口)时两频段 margin 恒为 0,
    # 会被误判"隔离"→错误开全双工(自激)。宁可报失败走保守半双工。
    rms_db = 20.0 * float(np.log10(float(np.sqrt(np.mean(x ** 2))) + 1e-9))
    if rms_db <= -85.0:
        return {"ok": False, "detail": f"麦克风采到数字静音({rms_db:.0f}dBFS),设备可能未就绪,稍后重测"}
    base = x[: int(sr * 1.0)]                          # 底噪窗(播放前的静默)
    rest = x[int(sr * 1.0):]                           # 含探测音时段
    margins = []
    for f0 in _CHIRP_F:
        b_db = float(np.median(_band_db_windows(base, sr, f0)))   # 中位抗瞬态噪声
        p_db = float(max(_band_db_windows(rest, sr, f0)))
        margins.append(round(p_db - b_db, 1))
    coupled = all(m >= _COUPLE_DB for m in margins) or any(m >= _COUPLE_DB_HI for m in margins)
    res = {"ok": True, "coupled": coupled, "margin_db": dict(zip(("f1", "f2"), margins)),
           "thr_db": _COUPLE_DB, "path": "net" if mic_net_url else "device",
           "detail": (f"耦合(超底噪 {margins} dB)：外放会灌进麦克风" if coupled
                      else f"声学隔离(超底噪 {margins} dB)：可全双工")}
    ST.coupling = {**res, "ts": time.time()}
    return res


@app.post("/coupling_probe")
def coupling_probe_api():
    """手动声学耦合自检(会话停止时)。按当前方案解析麦/监听出口。"""
    r = _ap_resolve()
    mic_leg, listen_leg = r["legs"]["mic"], r["legs"]["listen"]
    if mic_leg.get("kind") == "net":
        return _coupling_probe(mic_net_url=f"{MONITOR_URL}/mic/pcm",
                               out_idx=listen_leg.get("index"))
    if not mic_leg.get("ok"):
        return {"ok": False, "detail": mic_leg.get("note") or "麦克风不可用"}
    return _coupling_probe(mic_idx=mic_leg.get("index"), out_idx=listen_leg.get("index"))


# ══════════ P8-3 冲突规则引擎：把踩过的坑固化成规则,周期巡检+实时提示 ══════════
_vm_engine_cache = {"ts": 0.0, "ok": None}


def _vm_engine_ok() -> bool:
    """Voicemeeter 引擎在跑?(默认输出是它时,引擎挂=全系统无声)。30s 缓存。"""
    now = time.time()
    if now - _vm_engine_cache["ts"] < 30 and _vm_engine_cache["ok"] is not None:
        return _vm_engine_cache["ok"]
    ok = True
    try:
        import ctypes
        dll = r"C:\Program Files (x86)\VB\Voicemeeter\VoicemeeterRemote64.dll"
        if os.path.exists(dll):
            vm = ctypes.CDLL(dll)
            ok = vm.VBVMR_Login() == 0     # 0=引擎在跑 1=进程未启动
            vm.VBVMR_Logout()
    except Exception:
        ok = True                          # 探测失败不误报
    _vm_engine_cache.update(ts=now, ok=ok)
    return ok


def _conflict_scan() -> list:
    """返回 [{level: red/yellow, code, msg, fix}]。规则全部来自实战事故。"""
    issues = []
    try:
        p = _ap_get()
        # R1 默认输出是 Voicemeeter 而引擎没跑 → 全系统无声(2026-07-04 实测事故)
        try:
            defname = (sd.query_devices(kind="output").get("name") or "").lower()
        except Exception:
            defname = ""
        if "voicemeeter" in defname and not _vm_engine_ok():
            issues.append({"level": "red", "code": "vm_down",
                           "msg": "默认输出是 Voicemeeter 但混音引擎没运行——全系统无声",
                           "fix": "启动 Voicemeeter 或运行 tools/_vm_fix_routing.py --fix"})
        # R2 我的麦与对方声来源同设备 → 把自己当对方(自我回授)
        #    仅当两路都是"真实物理设备索引(>=0)"且非手机麦时才比较：
        #    手机麦走网络中继(mic_index=-1 哨兵)、立体声混音缺失时对方声也会是 -1 哨兵，
        #    两个 -1 相等会误报"同一设备"(实测 2026-07-14 电脑198:耳机场景+手机麦+未启用立体声
        #    混音,-1==-1 触发假红灯)。哨兵不是设备,不参与同设备判定。
        _net_mic = bool(getattr(ST, "mic_net_url", ""))
        if ST.running and not _net_mic \
                and ST.mic_index is not None and ST.loop_index is not None \
                and ST.mic_index >= 0 and ST.loop_index >= 0 \
                and ST.mic_index == ST.loop_index:
            issues.append({"level": "red", "code": "mic_eq_loop",
                           "msg": "「我的麦」与「对方声来源」是同一设备,会把你自己当对方翻译",
                           "fix": "对方声来源改用立体声混音/默认输出环回"})
        # R2b 对方声来源未解析到设备(会话在跑但采集b报错)→ 只听得到自己、听不到对方。
        #     真实信号取自采集线程错误,而非仅看 loop_index<0(WASAPI环回时索引本就是-1哨兵)。
        if ST.running and ST.cap_b is not None and ST.cap_b.error:
            issues.append({"level": "red", "code": "loop_unavailable",
                           "msg": "「对方声来源」未就绪,对方说话不会出字幕/翻译",
                           "fix": "启用「立体声混音/Stereo Mix」或在对方声来源选默认输出环回后重开同传"})
        # R3 监听出口解析到 CABLE → 耳返直接灌进发给对方的虚拟麦(必自激)
        try:
            mon_dev, _s, _c = _resolve_monitor_out()
            mon_name = (_dev_name_safe(mon_dev) or "").lower()
            if "cable input" in mon_name:
                issues.append({"level": "red", "code": "monitor_into_cable",
                               "msg": "监听出口指向 CABLE(发给对方的虚拟麦),耳返会二次灌给对方",
                               "fix": "监听出口改耳机或默认输出"})
        except Exception:
            pass
        # R4 蓝牙耳机麦(HFP 免提)采样率≤16k → 掉电话音质,识别也差
        if p.get("mic") not in ("phone",):
            try:
                mi = _find_device(str(p.get("mic") or CALL_MIC_NAME), False)
                if mi is not None:
                    minfo = sd.query_devices(mi)
                    mnm = (minfo.get("name") or "").lower()
                    if int(minfo.get("default_samplerate") or 48000) <= 16000 \
                            and any(h in mnm for h in _HEADSET_PAT):
                        issues.append({"level": "yellow", "code": "bt_hfp",
                                       "msg": "蓝牙耳机麦为免提(HFP)模式,音质降为电话级,识别会变差",
                                       "fix": "说话改用电脑麦,蓝牙耳机只用来听(耳机专业方案默认如此)"})
            except Exception:
                pass
        # R5 已实测耦合 + 耳返开 + 半双工被手动关 → 自激风险
        cp = getattr(ST, "coupling", None) or {}
        if cp.get("coupled") and ST.monitor_on and not HALF_DUPLEX_SPK:
            issues.append({"level": "red", "code": "coupled_fullduplex",
                           "msg": "实测音箱↔麦互通,且半双工已关——外放将灌回麦克风造成乱码/自激",
                           "fix": "戴耳机后重测,或 POST /halfduplex {on:true}"})
        # R6 手机方案 + 实测耦合 → 提示插耳机
        if p.get("mic") == "phone" and cp.get("coupled"):
            issues.append({"level": "yellow", "code": "phone_earphone",
                           "msg": "手机外放与手机麦互通(实测),已启用轮流说话;插耳机可解锁全双工",
                           "fix": "手机插有线耳机或连蓝牙耳机后重新开播"})
    except Exception:
        logger.exception("冲突巡检异常(忽略本轮)")
    return issues


_conflict_state = {"last_codes": set(), "issues": []}


def _conflict_tick():
    with _pa_lock:                      # 与 _pa_refresh 互斥(重建窗口内查设备会 -10000)
        issues = _conflict_scan()
    codes = {i["code"] for i in issues}
    new_red = [i for i in issues if i["level"] == "red" and i["code"] not in _conflict_state["last_codes"]]
    resolved = _conflict_state["last_codes"] - codes
    for i in new_red:
        ST.push_event({"who": "sys", "warn": f"🔴 {i['msg']}｜{i['fix']}"})
        try:
            import alerts as _al
            _al.raise_alert(f"audio_{i['code']}", "音频设备冲突", f"{i['msg']}。处理: {i['fix']}",
                            "error", "audio")
        except Exception:
            pass
    for c in resolved:
        try:
            import alerts as _al
            _al.clear_alert(f"audio_{c}", note="冲突已消除")
        except Exception:
            pass
    _conflict_state["last_codes"] = codes
    _conflict_state["issues"] = issues


def _conflict_worker():
    while True:
        time.sleep(12.0)
        try:
            _conflict_tick()
        except Exception:
            pass


@app.get("/conflicts")
def conflicts_get():
    return {"ok": True, "issues": _conflict_state["issues"],
            "half_duplex": HALF_DUPLEX_SPK, "coupling": getattr(ST, "coupling", None)}


_ap_load()   # 启动即载入方案(监听出口覆盖/半双工策略随之生效)
threading.Thread(target=_conflict_worker, name="conflict-scan", daemon=True).start()


class CallModeReq(BaseModel):
    profile: str = ""
    mode: str = "local"
    stream: bool = None
    loop_src: str = ""      # 对方声来源覆盖：""=自动(默认输出环回优先) / "stereomix"=强制立体声混音(修复自听)


@app.post("/call_mode/start")
def call_mode_start(req: CallModeReq):
    """一键通话模式：已运行则先停 → 系统默认麦切 CABLE Output → 按名绑定三设备 →
    通路/麦自检 → 开播。返回每步红绿灯的就绪报告(同时留在 /call_mode/status)。"""
    steps = []
    _last_call_req.update(profile=req.profile, mode=req.mode, stream=req.stream)

    def _step(name, ok, detail="", soft=False):
        # soft=True 的步骤只作提示、不参与 ready 判定：麦/耦合自检是"探测那一刻"的采样，
        # 真麦是否好用要等你开口说话才知道(实测 PD100X 探测瞬间读数字静音但会话识别正常)——
        # 不该因探测瞬时静音把整张向导判红、吓退用户。关键交付物(设备找到/会话起来/有配音音色)才 gate。
        steps.append({"name": name, "ok": bool(ok), "detail": detail, "soft": bool(soft)})
        return ok

    if ST.running:
        try:
            stop()
            _step("停止旧会话", True)
        except Exception as e:
            _step("停止旧会话", False, str(e)[:120])
    # 0) P8-1 场景方案：本次开播的设备角色全部由当前方案决定
    ap = _ap_get()
    ap_name = _ap_state["active"]
    phone_mic = ap.get("mic") == "phone"
    _step("场景方案", True, f"{ap.get('icon','')} {ap.get('label', ap_name)}"
          + (f"｜监听:{ap.get('listen')}" if ap.get("listen") not in ("", "default") else ""))
    # 1) 系统默认麦 → CABLE Output(微信从默认麦收音=收到克隆音)
    r = _set_default_mic("CABLE Output")
    _step("默认麦→CABLE Output", r.get("ok"), r.get("device") or r.get("error", ""))
    # 1.5) 重建设备快照：虚拟声卡注册/注销会让 MME 索引整表漂移,旧快照按名解析到错误设备
    #      (实测:BRIO 名字命中的索引实际是静音的虚拟口→"全绿但说话没反应")。
    _pa_refresh(force=True)
    # 2) 按名绑定设备(索引每次重启会漂,名字不会)。麦按方案:手机麦=中继网络流,免声卡。
    mic_net = f"{MONITOR_URL}/mic/pcm" if phone_mic else ""
    if phone_mic:
        mic = -1
        try:
            relay_ok = requests.get(f"{MONITOR_URL}/health", timeout=2).status_code == 200
        except Exception:
            relay_ok = False
        _step("手机麦中继", relay_ok, "手机页开着即通" if relay_ok else f"手机中继({_MON_PORT})不在线,请先启动 monitor_relay")
    else:
        mic = _find_device(str(ap.get("mic") or CALL_MIC_NAME), False) or _find_device("麦克风", False)
    cable = _find_device("CABLE Input", True)
    # 对方声来源：首选「默认输出的 WASAPI 环回」——微信把对方声放到默认输出,环回抓的就是它,
    # 与具体声卡无关(默认输出是 Voicemeeter/USB 耳机时,立体声混音只搭在 Realtek 上=聋)。
    # soundcard 库缺失/环回失败时 Capture 内部自动回退立体声混音,故这里兜底解析它。
    # loop_src="stereomix" 时跳过环回、直接强制立体声混音（一键「修复对方声来源」治自听同族）。
    force_mix = (req.loop_src or "").strip().lower() in ("stereomix", "stereo_mix", "mix")
    loop, loop_is_out = None, False
    if not force_mix:
        try:
            import soundcard as _sc
            _spk = _sc.default_speaker().name
            loop = _find_device(_spk.split(" (")[0].strip(), True)
            loop_is_out = loop is not None
        except Exception:
            pass
    if loop is None:
        loop = _find_loopback()
    if not phone_mic:
        _step("我的麦", mic is not None, _dev_name_safe(mic) or f"未找到含 {ap.get('mic') or CALL_MIC_NAME!r} 的输入设备")
    _step("克隆音出口(CABLE)", cable is not None, _dev_name_safe(cable))
    _step("对方声来源" + ("(默认输出环回)" if loop_is_out else "(立体声混音)"), loop is not None,
          (_dev_name_safe(loop) or ("未找到「立体声混音」：请在 Windows 声音→录制 里启用它后重试"
                                     if force_mix else "未启用立体声混音：请在 声音→录制 里启用"))
          + ("（已按『修复对方声来源』强制立体声混音）" if force_mix and loop is not None else ""))
    # 防自听：环回若抓的是「我的麦」同一物理设备(如麦和监听都在 PD100X)，你自己的话会被
    # 当"对方"再识别一遍→串语言/乱字幕(2026-07-10 实测 a/b 串扰根因)。同族即软提示+给一键修复。
    if (not phone_mic) and mic is not None and loop is not None:
        fam_m, fam_l = _dev_family(_dev_name_safe(mic)), _dev_family(_dev_name_safe(loop))
        if fam_m and fam_m == fam_l:
            _step("对方声防自听", False,
                  f"「对方声来源」与「我的麦」同属【{fam_m}】——你自己的话可能被当对方再识别一遍"
                  "(串语言/乱字幕)。点『修复对方声来源』一键改用立体声混音；或戴耳机减轻",
                  soft=True)
            _selfhear["fixable"] = (not force_mix)   # 供 /call_mode/status 与 UI 决定是否显示一键修复
            if _selfhear["fixable"]:
                # 询问式自动降级：检出即在实时提示流推一条"带修复动作"的高可见事件(桌面/手机都有修复按钮)，
                # 不让它埋没在就绪报告的软步骤里——2026-07-10 用户正是没注意到自听导致整场乱字幕。
                ST.push_event({"who": "sys",
                               "warn": f"🎧 检测到「对方声来源」与「我的麦」同属【{fam_m}】：你自己的话可能被当对方"
                                       "再识别一遍(串语言/乱字幕)。建议点『🔧 修复对方声来源』一键改用立体声混音；戴耳机也能减轻。",
                               "action": {"label": "🔧 修复对方声来源", "endpoint": "/call_mode/fix_loopback", "method": "POST"}})
        else:
            _selfhear["fixable"] = False
    else:
        _selfhear["fixable"] = False
    if mic is None or cable is None or loop is None:
        report = {"ready": False, "steps": steps, "ts": time.time()}
        ST.call_report = report
        return report
    # 3) 音频链路自检(开播前跑,不与采集流冲突)
    ct = _cable_path_test(cable)
    _step("CABLE 通路测试音", ct.get("ok"), ct.get("detail", ""))
    if not phone_mic:
        mt = _mic_open_test(mic)
        if not mt.get("ok"):
            # 首选实例(通常 MME)读数字静音时按名换宿主API实例重试：实测强杀持流进程后
            # PD100X 的 MME 实例整机坏死(新进程开它也是 -96.7dBFS),同名 WASAPI/DS 却正常。
            # 谁有真信号就把会话钉到谁上,避免"自检全绿以外全线失聪"。
            for alt in _find_device_all(str(ap.get("mic") or CALL_MIC_NAME), False):
                if alt == mic:
                    continue
                mt2 = _mic_open_test(alt)
                if mt2.get("ok"):
                    logger.warning(f"麦克风实例故障转移: dev={mic}({_dev_hostapi_name(mic)}) 静音 → "
                                   f"改用 dev={alt}({_dev_hostapi_name(alt)}) {_dev_name_safe(alt)!r}")
                    mic, mt = alt, mt2
                    mt["detail"] = f"首选实例静音,已自动换 {_dev_hostapi_name(alt)} 实例: " + str(mt2.get("detail", ""))
                    break
        # 软步骤：探测瞬间静音≠麦坏(实测 PD100X 探测读 -96dBFS 但开口即正常识别)。
        # 失败给"对着麦说话/确认未静音"的可执行提示，但不把整张向导判红。
        _mic_detail = mt.get("detail", "") if mt.get("ok") else \
            ("探测瞬间没采到声音（麦静音/没对着说话/被独占）——不影响开播，"
             "开口说话若字幕正常即可忽略；仍无字幕再查麦克风。原始: " + str(mt.get("detail", ""))[:80])
        _step("麦克风自检", mt.get("ok"), _mic_detail, soft=True)
    # 3.5) P8-2 声学耦合自检：实测「监听出口↔我的麦」互通性 → 决定半/全双工。
    #      auto 才探测；on/off 尊重手动。探测失败不拦开播(保守用半双工)。
    global HALF_DUPLEX_SPK
    hd_policy = ap.get("half_duplex", "auto")
    if hd_policy == "auto":
        mon_dev, _ms, _mc = _resolve_monitor_out()
        cp = _coupling_probe(mic_idx=(None if phone_mic else mic),
                             mic_net_url=mic_net, out_idx=mon_dev)
        if cp.get("ok"):
            HALF_DUPLEX_SPK = bool(cp.get("coupled"))
            _step("声学耦合自检", True,
                  ("互通→轮流说话(半双工)" if cp["coupled"] else "隔离→全双工") + f" {cp.get('margin_db')}", soft=True)
        else:
            HALF_DUPLEX_SPK = True
            _step("声学耦合自检", True, f"探测未完成({cp.get('detail','')[:60]})，保守用半双工", soft=True)
    else:
        HALF_DUPLEX_SPK = (hd_policy == "on")
        _step("声学耦合自检", True, f"方案指定{'半' if HALF_DUPLEX_SPK else '全'}双工(跳过探测)", soft=True)
    # 4) 开播(通话模式;声纹锁/降噪按 env 生效)
    try:
        kw = dict(mic_index=mic, cable_index=cable, loopback_index=loop,
                  loopback_is_output=loop_is_out, profile=req.profile,
                  mode=req.mode, live_mode=False, mic_net_url=mic_net)
        if req.stream is not None:              # 缺省交给 env 默认(显式 None 过不了 bool 校验)
            kw["stream"] = req.stream
        sr = start(StartReq(**kw))
        _step("同传会话", bool(sr.get("ok")),
              (f"采集a={sr.get('cap_a_err') or 'OK'} b={sr.get('cap_b_err') or 'OK'}"))
        # 通话模式的交付物就是"对方听到克隆音"——配音可用性必须是一盏灯,不能全绿却无声。
        # 配音就绪 = 有音色样本 且 TTS 引擎在线(2026-07-10 事故:引擎报 422/500 也会整场无声,
        # 光查音色不够)。任一不满足→红灯明说到底缺哪半，会话仍保留(字幕可用)。
        if sr.get("ok"):
            _step("配音就绪(音色+引擎)", bool(sr.get("dub_ok")),
                  sr.get("dub_reason") or ("就绪" if sr.get("dub_ok") else "配音不可用，对方只能看字幕"))
            # 配音没就绪且是"引擎不可达"这一半 → 后台自动拉起引擎(不阻塞开播;用户也可手动点)。
            if not sr.get("dub_ok") and not _tts_engine_health().get("ok"):
                _kick_dub_engine_async()
    except Exception as e:
        _step("同传会话", False, str(e)[:160])
    # ready 只看关键交付物(设备找到/会话起来/有配音音色)；soft 步骤(麦/耦合探测)只提示不判红——
    # 探测瞬时静音曾让"麦其实好用"的会话被误判 ready=False，吓退用户(2026-07-10 实测)。
    ready = all(s["ok"] for s in steps if not s.get("soft"))
    cp_now = getattr(ST, "coupling", None) or {}
    tips = ["微信重新拨打后生效(通话中途改默认麦不生效)",
            "开口先说三句话完成声纹注册,之后旁人声自动拦截"]
    if cp_now.get("coupled"):
        tips.insert(0, ("手机插耳机可解锁全双工(当前实测外放会灌麦,已自动轮流说话)" if phone_mic
                        else "戴耳机可解锁全双工(当前实测音箱会灌麦,已自动轮流说话)"))
    elif cp_now.get("ok"):
        tips.insert(0, "声学隔离良好,全双工已启用(双方可同时说话)")
    report = {"ready": ready, "steps": steps,
              "session_running": ST.running,   # 自检有红灯但会话已起(如仅缺配音)时,UI 按真相同步按钮态
              "voicelock": _voicelock.brief(), "denoise": DENOISE_ENABLE,
              "monitor_on": ST.monitor_on, "ts": time.time(),
              "audio_profile": ap_name, "half_duplex": HALF_DUPLEX_SPK,
              "self_hear_fixable": bool(_selfhear.get("fixable")),   # 检出自听同族且可一键改立体声混音
              "coupling": cp_now or None, "tips": tips}
    ST.call_report = report
    ST.push_event({"who": "sys", "warn": ("✅ 通话模式就绪" if ready else "⚠ 通话模式有未通过项,见 /call_mode/status")})
    if ready:
        ST.push_event({"who": "sys", "warn": (
            "🔊 实测外放↔麦互通：已启用轮流说话(半双工)，对方说话时你的麦自动静音；戴耳机可解锁全双工"
            if HALF_DUPLEX_SPK and cp_now.get("coupled")
            else ("🎧 声学隔离良好：全双工已启用，双方可同时说话" if cp_now.get("ok") and not cp_now.get("coupled")
                  else f"ℹ 当前{'半' if HALF_DUPLEX_SPK else '全'}双工"))})
    try:
        _conflict_tick()        # 开播即巡检一轮,红灯立即可见
    except Exception:
        pass
    return report


@app.get("/call_mode/status")
def call_mode_status():
    rep = dict(ST.call_report) if ST.call_report else {"ready": False, "steps": [], "detail": "尚未运行 /call_mode/start"}
    if _selfheal["log"]:                       # 附最近自愈留痕(前/后 nf、切到的实例)，UI 可展示"曾自动修过"
        rep["selfheal_log"] = _selfheal["log"][-5:]
    return rep


@app.get("/call_mode/selfheal_log")
def call_mode_selfheal_log():
    """无信号自愈留痕：每次触发的时间、触发时噪声底、备用实例数、切到的麦、是否重启成功。便于复盘哪块实例坏死。"""
    return {"ok": True, "log": _selfheal["log"], "cooldown_left": max(0.0, 180.0 - (time.time() - _selfheal["ts"]))}


class DubTestReq(BaseModel):
    profile: str = ""
    silent: bool | None = None    # None=智能(通话中静默不打扰对方/未开播整条通路)；True/False 显式覆盖


@app.post("/call_mode/dub_test")
def call_mode_dub_test(req: DubTestReq = None):
    """端到端试音：合成一句真实克隆音 → 推 CABLE → 回录量峰值，一盏灯验成 音色+引擎+CABLE 通路。
    未开播=双工推入 CABLE Input 回录；通话中默认静默(只验合成+回录电平)，silent=false 才注入对方。"""
    prof = (req.profile if req else "") or ""
    sil = (req.silent if req else None)
    try:
        res = _dub_test(prof, silent=sil)
    except Exception as e:
        logger.exception("试音异常")
        res = {"ok": False, "stage": "error", "reason": f"试音异常: {str(e)[:140]}"}
    _record_last_dub(res)
    ST.push_event({"who": "sys", "warn": ("🔊 " if res.get("ok") else "⚠ ") + str(res.get("reason", ""))})
    return res


@app.get("/tts/last_dub")
def tts_last_dub():
    """最近一次试音结果(常驻红绿灯)：ok / 峰值 / 多久前 / 说明。从未试过=never。"""
    d = dict(_last_dub)
    d["age_sec"] = (round(time.time() - d["ts"], 1) if d["ts"] else None)
    d["never"] = (d["ts"] == 0.0)
    return d


@app.post("/call_mode/stop")
def call_mode_stop():
    """收尾：停会话 + 系统默认麦还原为物理麦(通话结束后正常语音通话/录音不受影响)。"""
    r = stop() if ST.running else {"ok": True, "already_stopped": True}
    mic = _set_default_mic(CALL_MIC_NAME)
    return {"ok": True, "session": r, "default_mic_restored": mic}


class FixLoopReq(BaseModel):
    profile: str = ""


@app.post("/call_mode/fix_loopback")
def call_mode_fix_loopback(req: FixLoopReq = None):
    """一键「修复对方声来源」：强制用立体声混音重跑开播链，根治"对方声来源=我的麦同族"的自听
    (你自己的话被当对方再识别一遍→串语言/乱字幕)。沿用上次开播的角色/模式,只改对方声来源。"""
    prof = (req.profile if req and req.profile else _last_call_req.get("profile")) or ""
    kw = dict(profile=prof, mode=_last_call_req.get("mode") or "local", loop_src="stereomix")
    if _last_call_req.get("stream") is not None:
        kw["stream"] = _last_call_req["stream"]
    rep = call_mode_start(CallModeReq(**kw))
    src = next((s for s in rep.get("steps", []) if s["name"].startswith("对方声来源")), None)
    ok = bool(src and src.get("ok"))
    rep["fix_applied"] = ok
    rep["fix_note"] = ("已改用立体声混音抓对方声：" + (src.get("detail", "") if src else "")) if ok \
        else "未能切到立体声混音——请先在 Windows 声音→录制 里启用「立体声混音/Stereo Mix」再试"
    ST.push_event({"who": "sys", "warn": ("🔧 " if ok else "⚠ ") + rep["fix_note"]})
    return rep


# ══════════ P1-4 急停 / 耳返 / 声纹锁控制 ══════════
class PanicReq(BaseModel):
    on: bool = True


@app.post("/panic")
def panic(req: PanicReq = None):
    """一键急停：立刻切断正在播的克隆音(≤0.1s)并清空待播队列。再调 {on:false} 恢复。"""
    on = True if req is None else bool(req.on)
    ST.muted = on
    n = 0
    q = ST.play_q
    if on and q is not None:
        try:
            while True:
                q.get_nowait(); n += 1
        except queue.Empty:
            pass
    mq = ST.monitor_q
    if on and mq is not None:
        try:
            while True:
                mq.get_nowait()
        except queue.Empty:
            pass
    ST.push_event({"who": "sys", "warn": ("⛔ 已急停：克隆音已切断(再按恢复)" if on else "▶ 已恢复配音输出")})
    return {"ok": True, "muted": ST.muted, "dropped_blocks": n}


class MonitorReq(BaseModel):
    on: bool


@app.post("/monitor")
def monitor_toggle(req: MonitorReq):
    """耳返开关：把发给对方的克隆音小音量镜像到你的默认输出(耳机)。务必戴耳机再开。"""
    ST.monitor_on = bool(req.on)
    return {"ok": True, "monitor_on": ST.monitor_on, "gain": MONITOR_GAIN}


class AmbientReq(BaseModel):
    on: bool = None
    gain: float = None                 # 相对真实底噪倍率(0.2~2.0)
    max_dbfs: float = None             # 电平上限(-60~-25)


@app.get("/config/ambient")
def ambient_get():
    return {"ok": True, **_amb_view()}


@app.post("/config/ambient")
def ambient_set(req: AmbientReq):
    """P7 环境音垫运行时开关/调级。改档即时生效(垫层 20s 内重建亦可置零强刷)。"""
    global AMBIENT_ON, AMBIENT_GAIN, AMBIENT_MAX_DBFS
    if req.on is not None:
        AMBIENT_ON = bool(req.on)
    if req.gain is not None:
        AMBIENT_GAIN = max(0.2, min(2.0, float(req.gain)))
    if req.max_dbfs is not None:
        AMBIENT_MAX_DBFS = max(-60.0, min(-25.0, float(req.max_dbfs)))
    with _AMB["lock"]:
        _AMB["built"] = 0.0            # 强制下次取块时按新参数重建垫层
    logger.info(f"环境音垫 → on={AMBIENT_ON} gain={AMBIENT_GAIN} cap={AMBIENT_MAX_DBFS}dBFS")
    return {"ok": True, **_amb_view()}


@app.post("/halfduplex")
def halfduplex_toggle(req: MonitorReq):
    """音箱半双工开关：耳返/朗读外放期间麦克风上游喂静音(默认开,防音箱灌麦)。
    全程戴耳机(外放进不了麦)可关闭，恢复"边播边说"全双工。"""
    global HALF_DUPLEX_SPK
    HALF_DUPLEX_SPK = bool(req.on)
    return {"ok": True, "half_duplex": HALF_DUPLEX_SPK}


@app.post("/readback")
def readback_toggle(req: MonitorReq):
    """对方译文朗读开关：对方中文译文用 TA 自己的音色克隆读给你听(耳机)。务必戴耳机再开
    (外放会被麦收音——声纹锁会拦,但仍浪费一次门控)。参考音=对方开口后第一段达标真话,自动捕获。"""
    ST.readback_on = bool(req.on)
    return {"ok": True, "readback_on": ST.readback_on, "gain": READBACK_GAIN,
            "ref_locked": bool(ST.rb_ref_b64), "ref_sec": ST.rb_ref_sec}


# ── P3-1 实战调参 API：GET 读全量(值+范围+说明)，POST 改若干项(即时生效+落盘)，reset 回出厂 ──
class TuneReq(BaseModel):
    values: dict = {}


@app.get("/tune")
def tune_get():
    return {"ok": True, "values": _tune_values(), "defaults": _TUNE_DEFAULTS,
            "meta": _TUNABLES, "persisted": os.path.exists(TUNE_PATH)}


@app.post("/tune")
def tune_set(req: TuneReq):
    """运行时调参：{values:{名:值}}。越界自动夹取；未知名忽略；改完即持久化。
    通话中拖滑杆即听到效果(所有参数在使用点都是每次读全局)。"""
    changed = {}
    for k, v in (req.values or {}).items():
        if k in _TUNABLES:
            try:
                nv = _tune_clamp(k, v)
            except Exception:
                continue
            if abs(nv - float(globals()[k])) > 1e-9:
                globals()[k] = nv
                changed[k] = nv
    if changed:
        _tune_save()
        logger.info(f"实战调参: {changed}")
    return {"ok": True, "changed": changed, "values": _tune_values()}


@app.post("/tune/reset")
def tune_reset():
    """全部参数回 env 出厂值并删除持久化文件。"""
    for k, dv in _TUNE_DEFAULTS.items():
        globals()[k] = float(dv)
    try:
        if os.path.exists(TUNE_PATH):
            os.remove(TUNE_PATH)
    except Exception:
        pass
    logger.info("实战调参已重置为出厂值")
    return {"ok": True, "values": _tune_values()}


class EnrollReq(BaseModel):
    seconds: float = 6.0
    mic_index: int = None          # 缺省:会话麦,否则按名绑定


@app.post("/voicelock/enroll")
def voicelock_enroll(req: EnrollReq):
    """显式声纹注册：从麦克风录 N 秒(请正常朗读一段话)，直接生成/覆盖声纹底座。"""
    if not VOICELOCK_ENABLE:
        return JSONResponse({"ok": False, "detail": "声纹锁未启用(INTERP_VOICELOCK=0)"}, 400)
    mic = req.mic_index
    if mic is None:
        mic = ST.mic_index if (ST.running and ST.mic_index is not None and ST.mic_index >= 0) \
            else (_find_device(CALL_MIC_NAME, False) or _find_device("麦克风", False))
    if mic is None:
        return JSONResponse({"ok": False, "detail": "未找到可用麦克风"}, 400)
    sec = max(3.0, min(15.0, float(req.seconds or 6.0)))
    try:
        sr = int(sd.query_devices(mic).get("default_samplerate") or 48000)
        ST.push_event({"who": "sys", "warn": f"🎙 声纹注册中：请对着麦克风正常说话 {sec:.0f} 秒…"})
        r = sd.rec(int(sr * sec), samplerate=sr, channels=1, device=mic, dtype="float32")
        sd.wait()
        x = np.nan_to_num(r).reshape(-1)
        x16 = _resample(x, sr, SR)
        feats = _audio_features(x16)
        if feats and feats["rms_dbfs"] < -45:
            return JSONResponse({"ok": False, "detail": f"录到的声音太弱({feats['rms_dbfs']:.0f}dBFS)，请靠近麦克风重试"}, 400)
        ok = _voicelock.enroll_direct(x16)
        if ok:
            ST.push_event({"who": "sys", "warn": "🔒 声纹注册完成：此后只翻译你的声音"})
        return {"ok": ok, "voicelock": _voicelock.brief()}
    except Exception as e:
        return JSONResponse({"ok": False, "detail": str(e)[:160]}, 500)


class AffirmReq(BaseModel):
    uid: int


@app.post("/voicelock/affirm")
def voicelock_affirm(req: AffirmReq):
    """P-Affirm 一键「是我·放行并学习」：被声纹拦截的句子经用户确认后——
    ① 该句音频以 20% 权重学进底座(人工裁决>自适应)并持久化；
    ② 补跑原链路：有文本(流式已识别)直接翻译+配音；无文本(分段拦在 STT 前)整段重识别。
    音频只在近 8 句环形缓存里,过期返回 410。"""
    itm = next((x for x in list(ST.spk_blocked) if x.get("uid") == req.uid), None)
    if itm is None:
        return JSONResponse({"ok": False, "detail": "该句已过期(仅保留最近8句)，请重说一遍"}, 410)
    if itm.get("affirmed"):
        return {"ok": True, "already": True, "voicelock": _voicelock.brief()}
    wav = itm.get("wav")
    learned = False
    try:
        learned = _voicelock.affirm_learn(wav)
    except Exception:
        logger.exception("放行学习失败(仍尝试补跑该句)")
    replayed = False
    try:
        if ST.running and wav is not None:
            if itm.get("text"):
                ST.pool_a.submit(_stream_final_a, ST.next_uid(), itm["tid"], itm["text"],
                                 time.time(), np.copy(wav))
            else:                                  # 分段路径拦在 STT 前:整段重跑(带一次性声纹豁免)
                threading.Thread(target=_process_a, args=(np.copy(wav), True), daemon=True).start()
            replayed = True
    except Exception:
        logger.exception("放行补跑失败")
    itm["affirmed"] = True
    logger.info(f"[声纹锁] 用户放行 uid={req.uid} learned={learned} replayed={replayed} "
                f"text={itm.get('text', '')[:30]!r}")
    return {"ok": True, "learned": learned, "replayed": replayed,
            "voicelock": _voicelock.brief()}


@app.post("/voicelock/reset")
def voicelock_reset():
    """清除声纹底座(下场会话重新自动注册)。"""
    _voicelock.reset()
    return {"ok": True, "voicelock": _voicelock.brief()}


@app.get("/voicelock/status")
def voicelock_status():
    return {"ok": True, "voicelock": _voicelock.brief()}


class StartReq(BaseModel):
    mic_index: int
    cable_index: int
    loopback_index: int
    loopback_is_output: bool = False
    profile: str = ""
    mode: str = "local"
    live_mode: bool = False        # True=直播模式(英文驱动数字人口型→OBS虚拟摄像头)
    stream: bool = None            # True=流式逐词字幕(Nemotron)；None=按 env 默认；通话/直播均可
    mic_net_url: str = ""          # 非空=「我的麦」走手机无线直连(中继 /mic/pcm)，免 VB-Cable


@app.post("/start")
def start(req: StartReq):
    if ST.running:
        return {"ok": True, "already": True}
    from concurrent.futures import ThreadPoolExecutor
    ST.profile = req.profile
    ST.mode = req.mode or "local"
    ST.mic_index = req.mic_index
    ST.cable_index = req.cable_index
    ST.loop_index = req.loopback_index
    ST.loop_is_output = req.loopback_is_output
    ST.mic_net_url = req.mic_net_url or ""   # 存下手机麦中继URL，供「切语向自动重启」原样重建采集链
    # 设备角色校验：把「对方声来源」误选成 CABLE Input(发给对方的虚拟麦=我们写出去的克隆音)
    # 或与「我的麦」同一设备 → 自我回授/把自己当对方，是"没人说话却冒字幕/回声"的常见根因。
    # 非致命，给出醒目提示与纠正建议(改用「立体声混音/Stereo Mix」抓对方声)。
    try:
        _loop_nm = (sd.query_devices(req.loopback_index)["name"]
                    if req.loopback_index is not None and req.loopback_index >= 0 else "")
    except Exception:
        _loop_nm = ""
    _dev_warn = ""
    if (not req.mic_net_url) and req.loopback_index is not None and req.loopback_index == req.mic_index:
        _dev_warn = "⚠ 「对方声来源」与「我的麦」是同一设备，会把你自己的声音当对方识别。建议改用「立体声混音」。"
    elif "cable input" in _loop_nm.lower() or "cable in (" in _loop_nm.lower():
        _dev_warn = (f"⚠ 「对方声来源」选了 {_loop_nm}，那是发给对方的虚拟麦(会抓到自己的克隆音=自我回授)。"
                     f"请改用「立体声混音/Stereo Mix」抓对方声。")
    if _dev_warn:
        ST.push_event({"who": "sys", "warn": _dev_warn})
        logger.warning(_dev_warn)
    ST.voice_b64, ST.ref_text = _fetch_voice_ref(req.profile)
    logger.info(f"音色参考: voice={len(ST.voice_b64)}B ref_text={ST.ref_text[:20]!r}")
    _novoice_reset()                     # 新会话:无音色告警的节流窗/计数清零
    if not _voice_ready():
        fb = _fallback_voice()
        if fb:                           # env 显式开了兜底:出通用声,但"换了声"必须明说
            ST.voice_b64, ST.ref_text = fb
            ST.push_event({"who": "sys", "warn":
                           f"🔉 角色「{req.profile or '当前'}」无音色样本，已启用通用兜底音色"
                           "(对方听到的是通用声、非该角色专属音色)"})
            logger.warning(f"无音色→启用兜底参考音: {FALLBACK_VOICE_PATH}")
        else:                            # 开播即知会:否则整场配音静默失败,只见字幕不见声
            ST.push_event({"who": "sys", "warn": _voice_probe_hint(req.profile)})
            logger.warning(f"开播无音色参考: profile={req.profile!r} → 本场配音将全程跳过(字幕/翻译正常)")
    # 直播模式:取角色人脸 + 预热(后台);否则不取脸。
    ST.live_mode = bool(req.live_mode)
    ST.face_ready = False; ST.face_id = ""; ST.idle_video = ""
    face_bytes, face_id = b"", ""
    if ST.live_mode:
        try:
            nm = req.profile or requests.get(f"{HUB_URL}/profiles", timeout=5).json().get("active", "")
            pj = requests.get(f"{HUB_URL}/profiles/{nm}", params={"include_face": "true"}, timeout=10).json()
            face_bytes = _b64bytes(pj.get("face_b64", ""))
            face_id = f"interp_{nm}"
            ST.idle_video = (pj.get("idle_video") or "").strip()
            if not face_bytes:
                ST.push_event({"who": "sys", "warn": "⚠ 该角色无人脸,直播模式将回退为配音(VB-Cable)"})
            elif ST.idle_video:
                ST.push_event({"who": "sys", "warn": "🎬 真人待机视频已启用(激活角色时已推虚拟摄像头)"})
        except Exception:
            logger.exception("取角色人脸失败(直播模式)")
        vs = _vcam_status()                            # OBS 虚拟摄像头未就绪 → 明确提示
        if vs.get("cam_ready") is False and vs.get("cam_error"):
            ST.push_event({"who": "sys", "warn": "⚠ " + vs["cam_error"]})
        g = _gpu_snapshot(); ST.gpu = g                # 直播前 GPU 争用自检(争用下口型会严重滞后)
        if _gpu_contended(g):
            ST.push_event({"who": "sys", "warn":
                           f"⚠ 检测到显卡可能被占用(计算进程 {g.get('compute_apps')} 个/利用率 {g.get('util_pct')}%)，"
                           f"口型恐滞后；建议关闭占卡程序或独占显卡后开播"})
        threading.Thread(target=_ensure_live_profile,
                         args=(req.profile, face_bytes), daemon=True).start()
    # 流式决策需在热身前定下：流式下 Whisper 不用→既要卸载显存,也不能让热身把它重新拉起。
    # 通话/直播均可启用；req.stream 优先,None 用 env 默认；Nemotron 不可达自动回退分段。
    want_stream = STREAM_STT_DEFAULT if req.stream is None else bool(req.stream)
    # P5-2 弱语种保护：语向含 Nemotron 弱语种(实测 ko)且未显式要求流式 → 回退分段(Whisper 准)。
    # B-5: 每个分支顺带定格「为什么用这个引擎」(asr_why)，随 /metrics 常驻透出。
    weak = {_SRC_LANG, _DST_LANG} & STREAM_WEAK_LANGS
    asr_why = "本次显式指定流式" if req.stream is True else "默认流式(逐词字幕更跟手)"
    if not want_stream:
        asr_why = "流式已关闭" + ("(本次显式指定)" if req.stream is False else "(默认设置)")
    if want_stream and weak and req.stream is None:
        want_stream = False
        asr_why = (f"语向含弱语种 {'/'.join(sorted(weak))}：流式引擎该语种识别弱(实测)，"
                   "自动回退 Whisper 分段(更准)")
        ST.push_event({"who": "sys", "warn": f"ℹ 语向含 {'/'.join(sorted(weak))}：流式引擎该语种识别弱，已自动改用分段模式(更准)"})
        logger.info(f"弱语种 {sorted(weak)} → 流式改分段(Whisper)。可 /start stream=true 强制流式")
    use_stream = want_stream and _nemo_reachable()
    if want_stream and not use_stream:
        asr_why = "Nemotron 流式服务未就绪(7857)，自动回退 Whisper 分段"
        ST.push_event({"who": "sys", "warn": "ℹ 已回退分段同传(Nemotron 流式服务未就绪 7857)"})
        logger.info("流式未启用，回退分段：Nemotron 流式服务未就绪(7857)")
    if _DST_LANG in TTS_LOW_LANGS:      # P6-1 开播时也提示配音弱语种(实测发音不可懂)
        ST.push_event({"who": "sys",
                       "warn": f"⚠ 实测克隆配音的{_DST_LANG}发音可懂度低，对方请以字幕为准(翻译/字幕不受影响)"})
    ST.stream_on = use_stream
    # B-5 路由真相定格：强制流式但语向含弱语种时把风险写进 why(用户自担，但明示)。
    _warn_weak = (f"；⚠ 语向含流式弱语种 {'/'.join(sorted(weak))}(识别恐不准)"
                  if (use_stream and weak) else "")
    ST.asr_route = {"engine": "nemotron" if use_stream else "whisper",
                    "label": "流式·逐词" if use_stream else "Whisper·分段",
                    "why": asr_why + _warn_weak}
    # 显存策略：分段模式→预热 Whisper 兜底首句；流式+GER→Whisper 留驻当"终稿复核第二引擎"
    # (P0-①两遍法:.140 卡上口型/TTS 并不驻留,旧的"卸载让显存"在跨机拓扑下让给了空气)；
    # 流式且 GER 关闭→维持旧卸载行为。
    _set_whisper_loaded(load=(not use_stream) or _GER_ON)
    threading.Thread(target=_warmup_session,
                     args=(ST.voice_b64, ST.ref_text, face_bytes, face_id, use_stream),
                     daemon=True).start()
    ST.turn_src.clear(); ST.metrics.clear(); ST.dropped = 0   # 新会话清观测/轮次状态
    ST.transcript.clear(); ST.transcript_seq = 0; ST.session_start = time.time()   # 新会话清转写留存+置起点
    with _tr_cache_lock:                       # 新会话:清未命中计数 + 记缓存累计基线(算本场差分命中率)
        _tr_miss.clear()
        _tm_session_base["hit"] = _tr_cache_stat["hit"]; _tm_session_base["miss"] = _tr_cache_stat["miss"]
    ST.drops = {"gate": 0, "halluc": 0, "filler": 0, "lowconf": 0, "dedup": 0, "spk": 0, "echo": 0}
    ST.spk_blocked = deque(maxlen=8)               # P-Affirm 拦截留证按场清(旧场音频不跨场复跑)
    ST.ger_stats = {"checked": 0, "fixed": 0, "rejected": 0, "revived": 0, "vetoed": 0,
                    "skipped": 0, "garbage": 0, "overfix": 0}
    ST.mt_stats = {"llm_reject": 0}
    ST.review_clips = []; ST.review_stamp = time.strftime("%Y%m%d_%H%M%S")   # P3 复盘剪辑按场重置
    threading.Thread(target=_review_cleanup, daemon=True).start()            # 顺手清旧场音频(限 3 场)
    _discont["base"] = _discont["n"]               # P4-4 环回断点计数按会话差分
    ST.stream_stats = {"part_a": 0, "part_b": 0, "fin_a": 0, "fin_b": 0,
                       "yields": 0, "part_ts": deque(maxlen=80)}   # 清流式观测计数
    # P0-R 新会话清态：语义块观测/首音时戳/翻译滚动语境/LLM情绪槽(跨场不串染)
    ST.chunk_stats = {"committed": 0, "tail": 0, "blocked": 0, "mismatch": 0, "prefetch": 0}
    ST.llms_stats = {"used": 0, "segs": 0, "bail": 0, "filler": 0}   # P1 流译/垫场观测清零
    ST._a_voice_ts = 0.0; ST._b_voice_ts = 0.0; ST._b_voice_start = 0.0   # P3 礼让/附和时戳清零
    ST._tts_first_ts = 0.0
    ST.stream_sink_a = None
    _mt_ctx_clear()
    with _llm_emo_lock:
        _llm_emo_slot.update({"emo": "", "dst": "", "ts": 0.0})
    ST.warm_ms = 0; ST._avatar_calls = 0; ST._last_lag_warn = 0.0
    ST.s2s_on = False; ST.s2s_state = {}; ST.s2s_sink = None  # P0-S2S 新会话清云同传态
    ST.pending_switch = None; ST.switching = False            # 清角色切换/自愈态
    ST.live_degraded = False; ST._avatar_fail = 0; ST._last_degrade_warn = 0.0
    ST.preset_cache = {}; ST.preset_loading = False; ST.preset_queue = []
    ST.preset_disk = _load_preset_manifest()          # 载入上次落盘的指纹清单(跨会话复用)
    ST.switch_count = 0; ST.degrade_count = 0; ST.degrade_ms = 0
    ST._degrade_since = 0.0; ST._post_switch_probe = False; ST._post_recover_probe = False
    ST._avatar_inflight = 0; ST._last_avatar_ts = 0.0
    ST._gpu_streak = 0; ST.gpu_alert = False
    ST.pool_a = ThreadPoolExecutor(max_workers=1, thread_name_prefix="A")
    ST.pool_b = ThreadPoolExecutor(max_workers=1, thread_name_prefix="B")

    # 软终判润色线程(整轮重译替换字幕，仅在启用时)
    if FINALIZE_ENABLE:
        ST.fin_stop = threading.Event()
        ST.fin_thread = threading.Thread(target=_finalizer_worker, args=(ST.fin_stop,), daemon=True)
        ST.fin_thread.start()

    # 自愈 watchdog(直播模式):lipsync/vcam 异常自动降级,恢复自动回升
    if ST.live_mode:
        ST.heal_stop = threading.Event()
        ST.heal_thread = threading.Thread(target=_heal_worker, args=(ST.heal_stop,), daemon=True)
        ST.heal_thread.start()

    # 全局配音播放线程(独占 OutputStream，与合成解耦)。队列上限=背压，控制音频超前说话人的量。
    ST.muted = False                       # 新会话解除上一场的急停
    if req.cable_index is not None and req.cable_index >= 0:
        ST.play_sr, ST.play_ch = _dev_out_params(req.cable_index)
        ST.play_q = queue.Queue(maxsize=8)
        ST.play_stop = threading.Event()
        ST.play_thread = threading.Thread(
            target=_playback_worker,
            args=(req.cable_index, ST.play_sr, ST.play_ch, ST.play_q, ST.play_stop),
            daemon=True)
        ST.play_thread.start()
        logger.info(f"配音播放线程已启动: dev={req.cable_index} sr={ST.play_sr} ch={ST.play_ch}")
        # P1-4 耳返线程(独立默认输出;开关由 ST.monitor_on 控制,线程常驻本会话)
        ST.monitor_on = MONITOR_DEFAULT if not ST.monitor_on else ST.monitor_on
        # P2-2 朗读开关跨会话保持;参考音每场清零(对方换人了,旧音色必须作废)
        ST.readback_on = READBACK_DEFAULT if not ST.readback_on else ST.readback_on
        ST.rb_ref_b64 = ""; ST.rb_ref_text = ""; ST.rb_ref_sec = 0.0; ST.rb_busy = 0
        ST.aux_out_until = 0.0
        ST.monitor_q = queue.Queue(maxsize=16)
        ST.monitor_stop = threading.Event()
        ST.monitor_thread = threading.Thread(
            target=_monitor_worker, args=(ST.monitor_q, ST.monitor_stop), daemon=True)
        ST.monitor_thread.start()
        try:
            _monitor_feed._p = None        # 会话级缓存失效(默认输出设备可能已变)
        except Exception:
            pass
    else:
        ST.play_q = None
    _voicelock.warmup_async()              # P1-2 后台加载声纹模型(首次数秒,不阻塞开播)
    # P-Silence 会话钩子:影子/会话计数清零 + 底座健康度体检(过老/换麦→开播即提示重注册)
    _voicelock.on_session_start(_dev_name_safe(ST.mic_index))

    _reset_noise_floor()        # 新会话:清掉上会话的自适应噪声底，按当前设备/环境重新校准
    # P0-S2S: 云端 S2S 后端可用则方向A整链路上云(识别+翻译+克隆配音一体,延迟代差)；
    # 不可用/未配置 → 原本地管线,并把原因写进事件流(用户知道为什么这场没走云端)。
    _s2s_factory = None
    if _s2s_runtime_cfg()["backend"] != "none":
        _s2s_factory, _s2s_why = _s2s_make_factory()
        if _s2s_factory is None:
            ST.push_event({"who": "sys", "warn": f"ℹ 云同传未启用({_s2s_why})，本场走本地链路"})
            logger.info(f"S2S 后端不可用({_s2s_why}) → 本地链路")
    # 采集汇构造：云S2S(方向A) > 流式逐词 > 分段。方向B永远本地(离线可用性不动摇)。
    if _s2s_factory is not None:
        ST.s2s_on = True
        ST.cap_a = Capture(req.mic_index, False, None, "我→对方(云同传)", direction="a",
                           net_url=req.mic_net_url, sink_factory=_s2s_factory)
        if use_stream:
            ST.disp_b = StreamDispatcher("b")
            ST.cap_b = Capture(req.loopback_index, req.loopback_is_output, None, "对方→我",
                               direction="b", stream=True, language=_DST_LANG,
                               on_partial=ST.disp_b.on_partial, on_final=ST.disp_b.on_final)
        else:
            ST.cap_b = Capture(req.loopback_index, req.loopback_is_output,
                               lambda s: ST.pool_b.submit(_process_b, s), "对方→中", direction="b")
        ST.asr_route = {"engine": "seed_s2s", "label": "云同传·S2S一体",
                        "why": ("INTERP_S2S_BACKEND=seed：方向A识别/翻译/克隆配音由云端一体完成"
                                "(断线自动回本地)；方向B本地" +
                                ("(流式)" if use_stream else "(分段)"))}
        logger.info("启用云端 S2S 同传(Seed)·方向A上云,方向B本地")
    elif use_stream:
        ST.disp_a = StreamDispatcher("a"); ST.disp_b = StreamDispatcher("b")
        ST.cap_a = Capture(req.mic_index, False, None, "我→对方", direction="a",
                           stream=True, language=_SRC_LANG,
                           on_partial=ST.disp_a.on_partial, on_final=ST.disp_a.on_final,
                           net_url=req.mic_net_url)
        ST.cap_b = Capture(req.loopback_index, req.loopback_is_output, None, "对方→我", direction="b",
                           stream=True, language=_DST_LANG,
                           on_partial=ST.disp_b.on_partial, on_final=ST.disp_b.on_final)
        logger.info("启用流式逐词同传(Nemotron)")
    else:
        ST.cap_a = Capture(req.mic_index, False,
                           lambda s: ST.pool_a.submit(_process_a, s), "我→英", direction="a",
                           net_url=req.mic_net_url)
        ST.cap_b = Capture(req.loopback_index, req.loopback_is_output,
                           lambda s: ST.pool_b.submit(_process_b, s), "对方→中", direction="b")
    ST.cap_a.start(); ST.cap_b.start()
    time.sleep(0.4)
    err = ST.cap_a.error or ST.cap_b.error
    if err and (ST.cap_a.error and ST.cap_b.error):
        ST.running = False
        raise HTTPException(500, f"采集启动失败: {err}")
    ST.running = True
    _emo_prewarm_kick()    # P0 情感配音:后台预加载 CosyVoice3(防冷态首个情感句超时回退)
    _filler_prepare_kick()  # P1-F 垫场气口素材缺则后台用当前音色生成(错峰 6s,失败静默)
    if not ST.live_mode:   # P3: 通话开播记一次使用；直播模式经 Hub /activate 已自计,不重复
        _usage_report(req.profile, "interp_start")
    if ST.live_mode:   # P4: 直播开播后静默预载常用 Top3（通话模式无脸预案池，跳过）
        threading.Thread(target=_auto_preload_top, args=(3,), daemon=True).start()
    _tm_warmup_kick(_SRC_LANG, _DST_LANG)          # 后台预译高频句→首次即命中缓存(best-effort,延迟起避抢首句MT)
    # P4-1: 开会话即预热远端 NMT 兜底层(LLM 层 _warmup_session 已热)——LLM 中途熔断时兜底已是热态。
    _lang_warm_kick(_SRC_LANG, _DST_LANG, remote_only=True, reason="session-start")
    ST.push_event({"who": "sys", "clear": True})   # 新会话:清掉上一会话残留的旧字幕(含历史幻听)
    ST.push_event({"who": "sys", "src": "同传已启动", "dst": "Interpreter started", "stage": "sys"})
    # 开播后「真信号确认」：探测瞬间静音≠麦坏，但开播 N 秒后我方麦仍零识别产出，
    # 基本可确定麦静音/选错/被独占——主动弹一条可执行提示，不让用户对着静默自己猜。
    threading.Thread(target=_nosignal_watchdog, args=(ST.session_start,), daemon=True).start()
    _dub = _dub_ready()
    return {"ok": True, "cap_a_err": ST.cap_a.error, "cap_b_err": ST.cap_b.error,
            "voice_ok": _dub["voice_ok"], "dub_ok": _dub["ok"], "dub_reason": _dub["reason"]}


def _pfeat_report_push():
    """P2 实战验证配套：停播即推一份实时性特性引擎报告(控制台事件+日志)。
    首音中位/流译占比/语义块/垫场一屏读完,并按阈值给一句调参建议——验证不用翻 /metrics。"""
    try:
        with ST.lock:
            ms = [m for m in ST.metrics if m.get("dir") == "a"]
        if not ms:
            return
        tf = sorted(m["tts_first_ms"] for m in ms if m.get("tts_first_ms") is not None)
        med = tf[len(tf) // 2] if tf else None
        streamed = sum(1 for m in ms if m.get("llm_stream"))
        cs = getattr(ST, "chunk_stats", None) or {}
        ls = getattr(ST, "llms_stats", None) or {}
        txt = (f"📊 实时性报告：我方 {len(ms)} 句"
               + (f" · 首音中位 {med}ms" if med is not None else "")
               + f" · 流译 {streamed} 句/{ls.get('segs', 0)} 段(闸拒 {ls.get('bail', 0)})"
               + f" · 语义块 {cs.get('committed', 0)} 块(尾补 {cs.get('tail', 0)}"
                 f"/退回 {cs.get('blocked', 0)}/不一致 {cs.get('mismatch', 0)})"
               + (f" · 直播预取 {cs.get('prefetch', 0)} 块" if cs.get("prefetch") else "")
               + f" · 垫场 {ls.get('filler', 0)} 次"
               + (f" · 礼让 {ls.get('hold', 0)} 次" if ls.get("hold") else "")
               + (f" · 附和 {ls.get('bc', 0)} 次" if ls.get("bc") else ""))
        hints = []
        if med is not None and med > 1500:
            hints.append("首音仍偏慢:确认 LLM 已预热常驻,或调低 INTERP_LLM_STREAM_MIN(现"
                         f"{LLM_STREAM_MIN})/INTERP_CHUNK_MIN_LEN 提早出声")
        if ls.get("bail", 0) >= 3:
            hints.append("流译段闸拒偏多:换更稳的翻译模型,或 INTERP_LLM_STREAM=0 暂关流译")
        if cs.get("mismatch", 0) >= 3:
            hints.append("语义块定稿不一致偏多:INTERP_CHUNK_MIN_LEN 调大(块更稳)")
        if ls.get("filler", 0) >= max(3, len(ms) // 4):
            hints.append("垫场频繁=翻译/TTS 常超 900ms:先看 GPU 争用,再考虑调大 INTERP_FILLER_AFTER_MS")
        if hints:
            txt += "。建议：" + "；".join(hints)
        ST.push_event({"who": "sys", "warn": txt})
        logger.info(txt)
    except Exception:
        pass


@app.post("/stop")
def stop():
    if ST.cap_a: ST.cap_a.stop()
    if ST.cap_b: ST.cap_b.stop()
    ST.stream_on = False; ST.disp_a = None; ST.disp_b = None
    ST.s2s_on = False; ST.s2s_sink = None      # P0-S2S: s2s_state 保留供停播后复盘
    if ST.pool_a: ST.pool_a.shutdown(wait=False)
    if ST.pool_b: ST.pool_b.shutdown(wait=False)
    # 停播放线程：置停止事件 + 投哨兵唤醒 + join(让 OutputStream 在线程内干净关闭，避免骤关 PortAudio)
    th = ST.play_thread
    if ST.play_stop is not None:
        ST.play_stop.set()
    if ST.play_q is not None:
        try:
            ST.play_q.put_nowait(None)
        except Exception:
            pass
    if th is not None:
        th.join(timeout=3)
    ST.play_q = None
    ST.play_thread = None
    ST.play_stop = None
    # P1-4 停耳返线程(同样哨兵+join)
    mth = ST.monitor_thread
    if ST.monitor_stop is not None:
        ST.monitor_stop.set()
    if ST.monitor_q is not None:
        try:
            ST.monitor_q.put_nowait(None)
        except Exception:
            pass
    if mth is not None:
        mth.join(timeout=2)
    ST.monitor_q = None; ST.monitor_thread = None; ST.monitor_stop = None
    ST.muted = False
    ST.rb_ref_b64 = ""; ST.rb_ref_text = ""; ST.rb_ref_sec = 0.0; ST.rb_busy = 0   # P2-2 参考音只属于本场对方
    if ST.fin_stop is not None:
        ST.fin_stop.set()
    ST.fin_thread = None
    ST.fin_stop = None
    if ST.heal_stop is not None:
        ST.heal_stop.set()
    ST.heal_thread = None
    ST.heal_stop = None
    ST.set_degraded(False)                # 结算最后一段降级时长(若仍在降级)
    ST.pending_switch = None; ST.switching = False
    _sub_debouncer.cancel()
    if ST.live_mode:
        _clear_subtitles()
        _set_vcam_notice("")
    ST.running = False
    log_path = _write_session_log()
    ST.push_event({"who": "sys", "clear": True})   # 停止即清空 web 字幕,旧幻听不残留
    ST.push_event({"who": "sys", "src": "同传已停止", "dst": "Interpreter stopped", "stage": "sys"})
    _pfeat_report_push()   # P2 停播即出实时性特性报告(首音/流译/语义块/垫场+调参建议)
    return {"ok": True, "session_log": log_path}


@app.get("/health")
def health():
    """轻量健康检查(供启动器/守护/健康聚合探测)。base=本套安装根目录(两套并存时自证身份)。"""
    try:
        import app_config as _hc
        _base = str(_hc.BASE)
    except Exception:
        _base = ""
    return {"ok": True, "service": "interpreter", "running": ST.running, "base": _base, "port": PORT}


def _dev_name_safe(idx):
    try:
        if idx is not None and idx >= 0:
            return sd.query_devices(idx)["name"]
    except Exception:
        pass
    return ""


def _dev_hostapi_name(idx) -> str:
    """设备所属宿主API名("MME"/"Windows WASAPI"/…)。供自检子进程按同宿主API重解析,
    防止按名解析漂回别的实例(MME 实例坏死时必须钉死在备选的 WASAPI/DS 上测)。"""
    try:
        if idx is not None and idx >= 0:
            return sd.query_hostapis(sd.query_devices(idx)["hostapi"])["name"]
    except Exception:
        pass
    return ""


@app.get("/status")
def status():
    vs = _vcam_status()
    return {"running": ST.running, "profile": ST.profile, "stats": ST.stats,
            # 配音可用性(None=未运行不评估):False 即"角色无音色,本场只有字幕没有声"
            "voice_ok": (_voice_ready() if ST.running else None),
            "novoice_skips": _novoice["n"],
            "live_mode": ST.live_mode, "face_ready": ST.face_ready,
            "vcam_playing": vs.get("playing"), "hub_ok": _hub_ok(),
            "cap_a_err": ST.cap_a.error if ST.cap_a else None,
            "cap_b_err": ST.cap_b.error if ST.cap_b else None,
            # 设备态(只读,供手机中继做"跟随/配对"，无行为改变)
            "mic_index": ST.mic_index, "mic_name": _dev_name_safe(ST.mic_index),
            "loop_index": ST.loop_index, "loop_name": _dev_name_safe(ST.loop_index),
            "cable_index": ST.cable_index, "cable_name": _dev_name_safe(ST.cable_index),
            # P1: 急停/耳返/声纹锁态(供控制台按钮同步)
            "muted": ST.muted, "monitor_on": ST.monitor_on,
            "readback": {"on": ST.readback_on, "ref_locked": bool(ST.rb_ref_b64),
                         "ref_sec": ST.rb_ref_sec},
            "voicelock": _voicelock.brief(), "denoise": DENOISE_ENABLE,
            # P-Silence 归因数据：hub 静音看门狗据此把「哑播」翻译成人话根因
            # (声纹拦截/无有效识别/无音色),不再让用户瞎查麦克风/OBS
            "drops": dict(ST.drops), "drops_total": sum(ST.drops.values()),
            "fin_a": int(ST.stream_stats.get("fin_a") or 0),
            "fin_b": int(ST.stream_stats.get("fin_b") or 0),
            # P0-S2S: 云同传态(后端/连接/句数/回退)
            "s2s_on": ST.s2s_on, "s2s": (ST.s2s_state or None),
            # P8: 场景方案/双工态/耦合实测/冲突巡检(供 PC+手机页状态灯同源读取)
            "audio_profile": _ap_state["active"], "half_duplex": HALF_DUPLEX_SPK,
            "coupling": getattr(ST, "coupling", None),
            "conflicts": _conflict_state["issues"]}


@app.get("/monitor_status")
def monitor_status():
    """聚合手机无线终端(monitor_relay)状态，供同传页"手机端就绪"面板同源读取。
    纯只读代理；relay 不在线则返回 ok=False，不影响同传本身。"""
    base = MONITOR_URL.rstrip("/")

    def _get(path: str, timeout: float = 0.8):
        try:
            return requests.get(base + path, timeout=timeout).json()
        except Exception:
            return {}

    info = _get("/info")
    health = _get("/health")
    mic = _get("/mic/level")
    cam = _get("/cam/status")
    cap = health.get("capture") or {}
    mic_st = mic.get("status") or {}
    cam_st = cam.get("status") or {}
    cam_live = bool(cam_st.get("connected")) or (
        int(cam_st.get("peers") or 0) > 0 and int(cam_st.get("frames") or 0) > 0
    ) or float(cam_st.get("fps") or 0) > 0
    return {
        # reachable=relay 进程是否在线(任一探测有响应);前端据此决定是否显示就绪条,
        # 避免只用有线/通话的用户看到无关的"终端离线"。
        "reachable": bool(info or health or mic or cam),
        "ok": bool(health.get("ok")),
        "https_url": info.get("https_url") or "",
        "show_url": (info.get("url") or (base + "/")).rstrip("/") + "/show",
        "audio": {"ok": bool(cap.get("ok")), "clients": int(health.get("clients") or 0)},
        # taps=解释器直连订阅数(手机麦无线直连路径);ok=注入(VB-Cable)路径。任一即"手机麦在用"。
        "mic": {"ok": bool(mic_st.get("ok")), "level": mic_st.get("level") or 0,
                "taps": int(mic.get("taps") or 0)},
        "cam": {"ok": cam_live, "w": cam_st.get("w") or 0, "h": cam_st.get("h") or 0,
                "fps": cam_st.get("fps") or 0},
    }


def _vcam_status():
    """节流查询 vcam 播放态(供观测面板,失败静默)。"""
    now = time.time()
    c = getattr(_vcam_status, "_cache", None)
    if c and now - c.get("ts", 0) < 1.0:
        return c
    out = {"ts": now, "playing": False, "queued": 0, "ok": False,
           "cam_ready": None, "cam_error": "", "av_offset_ms": None, "av_drift_ms": None}
    try:
        j = requests.get(f"{VCAM_URL}/status", timeout=1.5).json()
        out.update({"playing": bool(j.get("playing")), "queued": int(j.get("queued") or 0),
                    "cam_ready": j.get("cam_ready"), "cam_error": j.get("cam_error") or "",
                    "av_offset_ms": j.get("av_offset_ms"), "av_drift_ms": j.get("av_drift_ms"), "ok": True})
    except Exception:
        pass
    _vcam_status._cache = out
    return out


def _hub_ok() -> bool:
    try:
        return requests.get(f"{HUB_URL}/health", timeout=1.5).ok
    except Exception:
        return False


def _gpu_snapshot() -> dict:
    """抓 GPU 占用快照(显存/利用率/计算进程数),用于直播前争用自检与观测。失败返回 {}。"""
    try:
        import subprocess
        _NW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # 隐藏控制台：周期性 metrics 轮询会反复调用，缺此标志会不停弹小黑窗
        q = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=6,
                           creationflags=_NW)
        mu, mt, ut = [x.strip() for x in q.stdout.strip().splitlines()[0].split(",")]
        a = subprocess.run(["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=6, creationflags=_NW)
        napps = len([l for l in a.stdout.strip().splitlines() if l.strip()])
        return {"mem_used_mib": int(mu), "mem_total_mib": int(mt), "util_pct": int(ut), "compute_apps": napps}
    except Exception:
        return {}


def _gpu_contended(g: dict) -> bool:
    """据快照判断显卡是否疑似被其它程序争用(实测争用下口型段间隔可达 5x)。"""
    if not g:
        return False
    return g.get("compute_apps", 0) > 4 or g.get("util_pct", 0) > 60 \
        or (g.get("mem_total_mib", 0) and g.get("mem_used_mib", 0) > g["mem_total_mib"] * 0.85)


def _audio_health():
    """音频健康快照：实时噪声底 + 生效门限 + 各层拦截计数。供面板"音频健康"指示灯。
    level: idle(未运行) / ok(标准) / noisy(环境偏吵，门已自适应抬高)。"""
    a_rms, a_peak = _adaptive_gates("a")
    b_rms, b_peak = _adaptive_gates("b")
    nf_a, nf_b = _noise_floor_dbfs("a"), _noise_floor_dbfs("b")
    raised = (a_rms > GATE_RMS_DBFS + 0.1) or (b_rms > GATE_RMS_DBFS + 0.1)
    level = "idle" if not ST.running else ("noisy" if raised else "ok")
    return {"level": level, "calib": CALIB_ENABLE,
            "noise_dbfs": {"a": (round(nf_a, 1) if nf_a is not None else None),
                           "b": (round(nf_b, 1) if nf_b is not None else None)},
            "gates": {"a": {"rms": round(a_rms), "peak": round(a_peak), "dyn": round(GATE_DYN_DB_MIN)},
                      "b": {"rms": round(b_rms), "peak": round(b_peak), "dyn": round(GATE_DYN_DB_MIN)}},
            "drops": dict(ST.drops),
            "drops_total": sum(ST.drops.values()),
            "ger": dict(ST.ger_stats),             # P0-① 纠错复核观测(复核/纠错/救回/撤回)
            "mt": dict(ST.mt_stats),               # P0i LLM 译文健全闸拒绝(回退 NMT)
            "discont": max(0, _discont["n"] - _discont["base"])}   # P4-4 本会话环回采集断点数


def _metrics_snapshot():
    """各阶段耗时/积压观测快照：近窗中位与 p90。供 /metrics 与会话日志复用。"""
    ms = list(ST.metrics)
    vs = _vcam_status()

    def stat_from(vals):
        xs = sorted(v for v in vals if v)
        if not xs:
            return None
        n = len(xs)
        return {"n": n, "median": xs[n // 2], "p90": xs[min(n - 1, int(n * 0.9))], "max": xs[-1]}

    def stat(key, dirf=None):
        return stat_from([m[key] for m in ms if key in m and (dirf is None or m.get("dir") == dirf)])

    # 近窗端到端延迟序列(每段 asr+nmt+synth)，供面板火花线观察趋势
    recent = [m.get("asr_ms", 0) + m.get("nmt_ms", 0) + m.get("synth_ms", 0) for m in ms][-40:]
    avatar_recent = [m.get("avatar_ms", 0) for m in ms if m.get("avatar_ms")][-40:]
    ttfv_recent = [m.get("ttfv_ms") for m in ms if m.get("ttfv_ms")][-40:]
    seg_gap_recent = [m.get("seg_gap_ms") for m in ms if m.get("seg_gap_ms")][-40:]
    # 流式观测:逐词总数/有效定稿/实时 partial 速率/逐词粒度(每句刷新次数)/GPU让位次数/Whisper卸载
    ss = ST.stream_stats
    _ts = list(ss.get("part_ts", []))
    _rate = 0.0
    if len(_ts) >= 2 and (_ts[-1] - _ts[0]) > 0:
        _rate = round((len(_ts) - 1) / (_ts[-1] - _ts[0]), 1)
    _part = ss["part_a"] + ss["part_b"]; _fin = ss["fin_a"] + ss["fin_b"]
    stream_m = {"on": ST.stream_on, "part": _part, "fin": _fin,
                "part_rate": _rate, "part_per_fin": (round(_part / _fin, 1) if _fin else 0.0),
                "yields": ss["yields"], "asr_unloaded": ST.asr_unloaded}
    # P0-R 实时性观测:语义块提前配音计数 + 首音延迟分布(定稿→首块入播放队列;分块句可为负=提前出声)
    chunk_m = {"on": CHUNK_DUB_ON, **(getattr(ST, "chunk_stats", None) or {})}
    # P1 观测:流式 LLM 翻译(边译边配) + 垫场气口
    llms_m = {"on": LLM_STREAM_ON, "filler_on": FILLER_ON, **(getattr(ST, "llms_stats", None) or {})}
    return {"running": ST.running, "live_mode": ST.live_mode, "stream_on": ST.stream_on,
            "stream": stream_m, "asr_route": (ST.asr_route or None),
            "s2s": {"on": ST.s2s_on, **(ST.s2s_state or {})},   # P0-S2S 云同传观测

            "stt_failover": _stt_fo_view(),   # S8-3: STT 单点容灾态（主端点失联→备用接管）
            "emo_tts": {"on": EMO_TTS_ON, **_emo_stats},   # P0 情感配音路由观测
            "face_ready": ST.face_ready, "face_id": ST.face_id or "",
            "idle_video": bool(ST.idle_video), "warm_ms": ST.warm_ms,
            "profile": ST.profile, "mode": ST.mode,
            "degraded": ST.live_degraded, "switching": ST.switching,
            "switch_count": ST.switch_count, "degrade_count": ST.degrade_count,
            "degrade_ms": ST.degraded_ms_total(),
            "preset_ready": sorted(ST.preset_cache.keys()), "preset_loading": ST.preset_loading,
            "preset_queue": list(ST.preset_queue),
            "preset_disk": sorted(ST.preset_disk.keys()),
            "gpu": ST.gpu, "gpu_contended": _gpu_contended(ST.gpu), "gpu_alert": ST.gpu_alert,
            "hub_ok": _hub_ok(), "vcam_playing": vs.get("playing"), "vcam_queued": vs.get("queued", 0),
            "cam_ready": vs.get("cam_ready"), "cam_error": vs.get("cam_error", ""),
            "av_offset_ms": vs.get("av_offset_ms"), "av_drift_ms": vs.get("av_drift_ms"),
            "counts": dict(ST.stats),
            "voice_ok": (_voice_ready() if ST.running else None),
            "novoice_skips": _novoice["n"],
            "backlog_now": (ST.play_q.qsize() if ST.play_q else 0),
            "dropped": ST.dropped,
            "chunk_dub": chunk_m,
            "llm_stream": llms_m,
            "tts_first_ms": stat("tts_first_ms", "a"),
            "asr_ms": stat("asr_ms"), "nmt_ms": stat("nmt_ms"),
            "synth_ms": stat("synth_ms", "a"), "avatar_ms": stat("avatar_ms", "a"),
            "ttfv_ms": stat("ttfv_ms", "a"), "seg_gap_ms": stat("seg_gap_ms", "a"),
            "backlog": stat("backlog", "a"),
            "e2e_ms": stat_from(recent), "recent_e2e": recent,
            "recent_avatar": avatar_recent, "recent_ttfv": ttfv_recent,
            "recent_seg_gap": seg_gap_recent,
            "open_turns": len(ST.turn_src),
            "audio_health": _audio_health(),
            "ambient": _amb_view(),
            "muted": ST.muted, "monitor_on": ST.monitor_on,
            "readback_on": ST.readback_on, "readback_ref": bool(ST.rb_ref_b64),
            "voicelock": _voicelock.brief(),
            # P8: 场景方案/双工/耦合/冲突(PC 页顶部场景卡与冲突条同源轮询)
            "audio_profile": _ap_state["active"], "half_duplex": HALF_DUPLEX_SPK,
            "coupling": getattr(ST, "coupling", None),
            "conflicts": _conflict_state["issues"],
            "translate": _translate_status(),
            "bargein_count": int(getattr(ST, "bargein_count", 0) or 0),
            "call_ready": bool((ST.call_report or {}).get("ready"))}


def _session_summary(snap: dict) -> dict:
    """从快照抽精简摘要(供 Hub 控制台直接展示，无需读整个日志文件)。"""
    def med(s): return s.get("median") if isinstance(s, dict) else None
    # 流式会话摘要(仅当本会话有逐词活动时附带;part_rate 为实时表针、停时已失真故不入摘要)
    sm = snap.get("stream") or {}
    stream_sum = None
    if sm.get("part") or sm.get("on"):
        stream_sum = {"part": sm.get("part"), "fin": sm.get("fin"),
                      "part_per_fin": sm.get("part_per_fin"), "yields": sm.get("yields"),
                      "asr_unloaded": sm.get("asr_unloaded")}
    # P3-4 质量摘要补全：分层拦截/声纹/朗读原本只在完整快照里，摘要看不到→趋势没法比。
    ah = snap.get("audio_health") or {}
    vl = snap.get("voicelock") or {}
    # P1-1 质量遥测四指标：上屏率(说了必上屏)/纠错率(强模型仍要改多少)/误杀救回/复核撤回。
    gr = ah.get("ger") or {}
    c = snap.get("counts") or {}
    fins = int(c.get("a") or 0) + int(c.get("b") or 0)
    drops_total = sum(int(v or 0) for v in (ah.get("drops") or {}).values())
    kpi = None
    if fins or drops_total or gr.get("checked"):
        kpi = {"onscreen_rate": (round(fins / (fins + drops_total), 3)          # 上屏率=定稿/(定稿+拦截)
                                 if (fins + drops_total) else None),
               "fix_rate": (round(gr.get("fixed", 0) / gr["checked"], 3)         # 纠错率=改写/复核
                            if gr.get("checked") else None),
               "revived": gr.get("revived", 0), "vetoed": gr.get("vetoed", 0)}
    return {"profile": snap.get("profile"), "mode": snap.get("mode"),
            "live_mode": snap.get("live_mode"), "counts": snap.get("counts"),
            "langs": snap.get("langs"),
            "dropped": snap.get("dropped"), "warm_ms": snap.get("warm_ms"),
            "asr_ms": med(snap.get("asr_ms")), "nmt_ms": med(snap.get("nmt_ms")),
            "e2e_ms": med(snap.get("e2e_ms")), "ttfv_ms": med(snap.get("ttfv_ms")),
            "tts_first_ms": med(snap.get("tts_first_ms")),          # P0-R3 首音中位(负=提前出声)
            "chunk_dub": snap.get("chunk_dub"),                      # P0-R1 语义块观测
            "llm_stream": snap.get("llm_stream"),                    # P1 流译/垫场观测
            "seg_gap_ms": med(snap.get("seg_gap_ms")), "avatar_ms": med(snap.get("avatar_ms")),
            "switch_count": snap.get("switch_count"), "degrade_count": snap.get("degrade_count"),
            "degrade_ms": snap.get("degrade_ms"), "stream": stream_sum,
            "dur_s": snap.get("dur_s"),
            "drops": ah.get("drops"),
            "ger": (dict(gr) if gr else None), "kpi": kpi,
            "discont": ah.get("discont"),
            "voicelock": {"enrolled": vl.get("enrolled"), "accepts": vl.get("accepts"),
                          "rejects": vl.get("rejects")} if vl else None,
            "readback_used": bool(snap.get("readback_ref")),
            "ended_at": snap.get("ended_at")}


def _dump_transcript_file(stamp: str):
    """会话转写落 logs/interp_transcript_<stamp>.json(非空时)。供事后 /transcript?session= 导出/复盘。"""
    try:
        with ST.lock:
            entries = list(ST.transcript)
        if not entries:
            return None
        path = os.path.join("logs", f"interp_transcript_{stamp}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"src": _SRC_LANG, "dst": _DST_LANG, "ended_at": stamp,
                       "count": len(entries), "entries": entries}, f, ensure_ascii=False, indent=2)
        logger.info(f"会话转写已写入 {path}({len(entries)} 条)")
        return path
    except Exception:
        logger.exception("写会话转写失败")
        return None


# ── P4-2 会话质量守门：每场结束自动对比历史基线，劣化(延迟爬升/拦截暴涨/环回断点)推送告警 ──
#   P3-4 落了数据,这一步让数据"会叫":不用人盯 /ops 趋势卡,变差当场收到 webhook/托盘通知。
#   冒烟/误开的短会话不评(免误报)；基线=最近 20 场有效会话的中位(自适应本机常态,而非拍脑袋阈值)；
#   另设绝对红线(基线整体劣化时相对比较会失灵)。notify_event=点状事件,不占活动告警位。
_QA_ALERT_ON   = os.environ.get("INTERP_QA_ALERT", "1") == "1"
_QA_MIN_DUR_S  = float(os.environ.get("INTERP_QA_MIN_DUR_S", "120"))   # 短于此的会话不评(冒烟/误开)
_QA_MIN_SEGS   = int(os.environ.get("INTERP_QA_MIN_SEGS", "5"))        # 段数不足不评(小样本噪声)
_QA_E2E_ABS_MS = float(os.environ.get("INTERP_QA_E2E_ABS_MS", "6000")) # 红线:端到端中位(ms)
_QA_DROP_ABS   = float(os.environ.get("INTERP_QA_DROP_ABS", "0.4"))    # 红线:拦截占比
_QA_E2E_RATIO  = float(os.environ.get("INTERP_QA_E2E_RATIO", "1.6"))   # 相对基线:延迟倍率
_QA_DROP_RATIO = float(os.environ.get("INTERP_QA_DROP_RATIO", "2.0"))  # 相对基线:拦截率倍率
# P2 纠错率守门：纠错率=fixed/checked=「流式引擎错到强模型要改多少」。突升=识别引擎/链路劣化
# (麦坏、降噪失效、模型退化)。复核数不足 10 句不评(小样本噪声)。
_QA_FIX_ABS    = float(os.environ.get("INTERP_QA_FIX_ABS", "0.65"))    # 红线:纠错率绝对值
_QA_FIX_RATIO  = float(os.environ.get("INTERP_QA_FIX_RATIO", "2.5"))   # 相对基线:纠错率倍率
_QA_FIX_MIN_N  = int(os.environ.get("INTERP_QA_FIX_MIN_N", "10"))      # 最少复核句数


def _qa_segs_drops(s):
    c = s.get("counts") or {}
    segs = int(c.get("a") or 0) + int(c.get("b") or 0)
    drops = sum(int(v or 0) for v in (s.get("drops") or {}).values())
    return segs, drops


def _qa_baseline(exclude_path):
    """历史基线：最近 20 场有效会话(≥60s 且有段,排除本场)的 e2e 中位/拦截率中位/纠错率中位。"""
    import glob
    e2e, dr, fx = [], [], []
    ex = os.path.abspath(exclude_path or "")
    for fp in sorted(glob.glob(os.path.join("logs", "interp_session_*.json")), reverse=True):
        if os.path.abspath(fp) == ex:
            continue
        if len(e2e) >= 20:
            break
        try:
            with open(fp, "r", encoding="utf-8") as f:
                s = _session_summary(json.load(f))
        except Exception:
            continue
        segs, drops = _qa_segs_drops(s)
        if (s.get("dur_s") or 0) < 60 or segs + drops < _QA_MIN_SEGS:
            continue
        if s.get("e2e_ms"):
            e2e.append(float(s["e2e_ms"]))
        if segs + drops > 0:
            dr.append(drops / (segs + drops))
        g = s.get("ger") or {}
        if int(g.get("checked") or 0) >= _QA_FIX_MIN_N:
            fx.append(int(g.get("fixed") or 0) / int(g["checked"]))
    med = lambda xs: sorted(xs)[len(xs) // 2] if xs else None
    return {"n": len(e2e), "e2e_med": med(e2e), "drop_med": med(dr),
            "fix_n": len(fx), "fix_med": med(fx)}


def _qa_check(summary, log_path):
    """会话结束质量守门。返回 flags 列表(空=健康)；劣化时推送点状告警(webhook/托盘)。"""
    if not _QA_ALERT_ON:
        return []
    try:
        segs, drops = _qa_segs_drops(summary)
        if (summary.get("dur_s") or 0) < _QA_MIN_DUR_S or segs + drops < _QA_MIN_SEGS:
            logger.info(f"质量守门: 样本不足(时长 {summary.get('dur_s')}s · 段+拦 {segs + drops}),本场不评")
            return []
        flags = []
        e2e = summary.get("e2e_ms")
        drop_rate = drops / (segs + drops) if segs + drops else 0.0
        base = _qa_baseline(log_path)
        if e2e and e2e > _QA_E2E_ABS_MS:
            flags.append(f"端到端中位 {e2e:.0f}ms 超红线 {_QA_E2E_ABS_MS:.0f}ms")
        elif e2e and base.get("e2e_med") and base["n"] >= 3 and e2e > base["e2e_med"] * _QA_E2E_RATIO:
            flags.append(f"端到端中位 {e2e:.0f}ms 较基线 {base['e2e_med']:.0f}ms 涨至 {e2e / base['e2e_med']:.1f}×")
        if drop_rate > _QA_DROP_ABS:
            flags.append(f"拦截率 {drop_rate * 100:.0f}% 超红线 {_QA_DROP_ABS * 100:.0f}%")
        elif base.get("drop_med") is not None and base["n"] >= 3 \
                and drop_rate > max(0.08, base["drop_med"] * _QA_DROP_RATIO):
            flags.append(f"拦截率 {drop_rate * 100:.0f}% 较基线 {base['drop_med'] * 100:.0f}% 异常升高")
        # P2 纠错率守门：识别引擎劣化的早期信号(强模型复核后要改的比例突升)
        g = summary.get("ger") or {}
        if int(g.get("checked") or 0) >= _QA_FIX_MIN_N:
            fix_rate = int(g.get("fixed") or 0) / int(g["checked"])
            if fix_rate > _QA_FIX_ABS:
                flags.append(f"纠错率 {fix_rate * 100:.0f}% 超红线 {_QA_FIX_ABS * 100:.0f}%(识别引擎可能劣化)")
            elif base.get("fix_med") is not None and base.get("fix_n", 0) >= 3 \
                    and fix_rate > max(0.15, base["fix_med"] * _QA_FIX_RATIO):
                flags.append(f"纠错率 {fix_rate * 100:.0f}% 较基线 {base['fix_med'] * 100:.0f}% 异常升高")
        disc = int(summary.get("discont") or 0)
        dur_min = max(0.5, (summary.get("dur_s") or 60) / 60.0)
        if disc / dur_min > 30:
            flags.append(f"环回采集断点 {disc} 次({disc / dur_min:.0f}/分),对方声可能丢字")
        if flags:
            detail = "；".join(flags) + f"（时长 {summary.get('dur_s')}s · 段 {segs} · 拦截 {drops}）"
            logger.warning(f"质量守门: 本场劣化 → {detail}")
            if alerts is not None:
                try:
                    alerts.notify_event("通话质量劣化", detail, level="warn", source="live_interpreter")
                except Exception:
                    pass
        else:
            logger.info(f"质量守门: 本场健康(e2e {e2e}ms · 拦截率 {drop_rate * 100:.0f}% · 基线 {base['n']} 场)")
        return flags
    except Exception:
        logger.exception("质量守门评估失败(不影响会话落盘)")
        return []


def _write_session_log():
    """会话结束落观测日志 logs/interp_session_*.json + 转写 logs/interp_transcript_*.json，
    便于真机联调后复盘延迟/丢弃、事后导出字幕/记录。转写独立落盘(即使本会话无 metrics 也保留)。"""
    try:
        import datetime
        os.makedirs("logs", exist_ok=True)
        stamp = f"{datetime.datetime.now():%Y%m%d_%H%M%S}"
        _dump_transcript_file(stamp)          # 转写独立落盘(与观测日志同一 stamp 便于配对)
        _review_dump()                        # P3 复盘剪辑索引落盘(戳=会话开始时间)
        _tm_stats_record(stamp)               # 翻译质量趋势 + 累计未命中句独立落盘(即使本会话无 metrics)
        _tm_cache_save()                      # P4-5 本场新译句并入持久化缓存(重启不丢)
        snap = _metrics_snapshot()
        snap["profile"] = ST.profile
        snap["mode"] = ST.mode
        snap["langs"] = [_SRC_LANG, _DST_LANG]     # P6-5 语对入日志(预载自学习/复盘按语向过滤)
        snap["dur_s"] = round(time.time() - ST.session_start, 1) if ST.session_start else None
        snap["ended_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        if not ST.metrics:
            # P6-3 空会话(一句都没识别)不落盘防日志堆积,但摘要仍更新——手机质量卡如实报"0 句",
            # 这本身就是有效信号(设备选错/没人说话)。
            ST.last_session = _session_summary(snap)
            ST.last_session["empty"] = True
            ST.last_session["quality_flags"] = []
            return None
        path = os.path.join("logs", f"interp_session_{stamp}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
        ST.last_session = _session_summary(snap)
        ST.last_session["log"] = path
        ST.last_session["quality_flags"] = _qa_check(ST.last_session, path)   # P4-2 劣化守门
        logger.info(f"会话观测日志已写入 {path}")
        return path
    except Exception:
        logger.exception("写会话日志失败")
        return None


@app.get("/metrics")
def metrics():
    return _metrics_snapshot()


@app.get("/gpu")
def gpu():
    """实时 GPU 占用快照 + 争用判定(供直播前自检/排查口型滞后)。"""
    g = _gpu_snapshot()
    return {"ok": bool(g), "gpu": g, "contended": _gpu_contended(g)}


@app.get("/session/last")
def session_last():
    """最近一次会话观测摘要(供 Hub 同传控制台展示)。无则 running=当前态。"""
    return {"ok": True, "running": ST.running, "summary": ST.last_session}


@app.get("/session/list")
def session_list(limit: int = 10):
    """近 N 次会话观测摘要(读 logs/interp_session_*.json),供 Hub 对比延迟趋势。"""
    import glob
    out = []
    try:
        files = sorted(glob.glob(os.path.join("logs", "interp_session_*.json")), reverse=True)[:max(1, limit)]
        for fp in files:
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    snap = json.load(f)
                s = _session_summary(snap)
                s["log"] = os.path.basename(fp)
                out.append(s)
            except Exception:
                continue
    except Exception:
        logger.exception("读取会话历史失败")
    return {"ok": True, "sessions": out}


@app.get("/session/weekly")
def session_weekly(days: int = 7):
    """P3-4 会话质量周报：近 N 天会话按天聚合(场次/时长/延迟中位/分层拦截合计)，
    横向可见"越用越好还是变差"。数据源=会话落盘日志,无额外运行时开销。"""
    import glob, datetime
    days = max(1, min(60, days))
    since = datetime.datetime.now() - datetime.timedelta(days=days)
    daily = {}          # "MM-DD" -> 聚合桶
    totals = {"n": 0, "dur_s": 0.0, "counts_a": 0, "counts_b": 0,
              "drops": {}, "e2e": [], "asr": [],
              "ger": {"checked": 0, "fixed": 0, "revived": 0, "vetoed": 0}}
    try:
        for fp in sorted(glob.glob(os.path.join("logs", "interp_session_*.json"))):
            stamp = os.path.basename(fp)[len("interp_session_"):-len(".json")]
            try:
                dt = datetime.datetime.strptime(stamp, "%Y%m%d_%H%M%S")
            except Exception:
                continue
            if dt < since:
                continue
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    snap = json.load(f)
            except Exception:
                continue
            s = _session_summary(snap)
            key = dt.strftime("%m-%d")
            b = daily.setdefault(key, {"date": key, "n": 0, "dur_s": 0.0,
                                       "counts_a": 0, "counts_b": 0,
                                       "drops": {}, "e2e": [], "spk": 0, "echo": 0,
                                       "ger": {"checked": 0, "fixed": 0, "revived": 0, "vetoed": 0}})
            b["n"] += 1; totals["n"] += 1
            b["dur_s"] += float(s.get("dur_s") or 0); totals["dur_s"] += float(s.get("dur_s") or 0)
            c = s.get("counts") or {}
            b["counts_a"] += int(c.get("a") or 0); b["counts_b"] += int(c.get("b") or 0)
            totals["counts_a"] += int(c.get("a") or 0); totals["counts_b"] += int(c.get("b") or 0)
            for k, v in (s.get("drops") or {}).items():
                b["drops"][k] = b["drops"].get(k, 0) + int(v or 0)
                totals["drops"][k] = totals["drops"].get(k, 0) + int(v or 0)
            for k in ("checked", "fixed", "revived", "vetoed"):   # P1-1 纠错/救回按天入趋势
                v = int((s.get("ger") or {}).get(k) or 0)
                b["ger"][k] += v; totals["ger"][k] += v
            if s.get("e2e_ms"):
                b["e2e"].append(s["e2e_ms"]); totals["e2e"].append(s["e2e_ms"])
            if s.get("asr_ms"):
                totals["asr"].append(s["asr_ms"])
        # P4 复盘人工标注按天并入(真值指标:句准率/真实CER)。与会话日志独立读取,
        # 当前进行中的会话(尚无日志)只要标了也计入 totals。
        rev_daily, rev_tot = {}, {"marked": 0, "ok": 0, "cers": []}
        for m in _review_marks_index().values():
            try:
                dt = datetime.datetime.strptime(m.get("session") or "", "%Y%m%d_%H%M%S")
            except Exception:
                continue
            if dt < since:
                continue
            r = rev_daily.setdefault(dt.strftime("%m-%d"), {"marked": 0, "ok": 0, "cers": []})
            for tgt in (r, rev_tot):
                tgt["marked"] += 1
                tgt["ok"] += int(bool(m.get("ok")))
                if m.get("cer") is not None:
                    tgt["cers"].append(float(m["cer"]))
        _rev_pack = lambda r: {"marked": r["marked"],
                               "acc": round(r["ok"] / r["marked"], 3) if r["marked"] else None,
                               "cer": round(sum(r["cers"]) / len(r["cers"]), 4) if r["cers"] else None}
        out_days = []
        for key in sorted(daily.keys()):
            b = daily[key]
            e2e = sorted(b.pop("e2e"))
            b["e2e_med"] = e2e[len(e2e) // 2] if e2e else None
            b["dur_s"] = round(b["dur_s"], 1)
            b["spk"] = b["drops"].get("spk", 0); b["echo"] = b["drops"].get("echo", 0)
            fins = b["counts_a"] + b["counts_b"]
            dr = sum(b["drops"].values())
            b["onscreen_rate"] = round(fins / (fins + dr), 3) if (fins + dr) else None   # 上屏率/天
            if key in rev_daily:
                b["review"] = _rev_pack(rev_daily[key])
            out_days.append(b)
        e2e = sorted(totals.pop("e2e")); asr = sorted(totals.pop("asr"))
        totals["e2e_med"] = e2e[len(e2e) // 2] if e2e else None
        totals["asr_med"] = asr[len(asr) // 2] if asr else None
        totals["dur_s"] = round(totals["dur_s"], 1)
        fins = totals["counts_a"] + totals["counts_b"]
        dr = sum(totals["drops"].values())
        totals["onscreen_rate"] = round(fins / (fins + dr), 3) if (fins + dr) else None
        gt = totals["ger"]
        totals["fix_rate"] = round(gt["fixed"] / gt["checked"], 3) if gt["checked"] else None
        if rev_tot["marked"]:
            totals["review"] = _rev_pack(rev_tot)
        return {"ok": True, "days": days, "daily": out_days, "totals": totals}
    except Exception:
        logger.exception("会话周报聚合失败")
        return {"ok": False, "days": days, "daily": [], "totals": totals}


# ── 演示模式(市场/验收一键展示，无需麦克风) ────────────────────────────────
DEMO_SCRIPT = [
    "大家好，欢迎来到今天的直播间。",
    "我是你们的数字人主播，很高兴和大家见面。",
    "我可以实时把中文翻译成英文，并用克隆的原声说出来。",
    "无论对面说哪种语言，都能瞬间跨越沟通的障碍。",
    "这就是实时同传数字人带来的全新体验。",
    "感谢大家观看，我们下期节目再见！",
]


def _run_demo(profile: str, live_mode: bool):
    """后台播放预设脚本:中文→英文→(直播)数字人开口+双语字幕 /(通话)克隆配音。供市场演示。"""
    stop = ST.demo_stop
    try:
        ST.profile = profile
        ST.voice_b64, ST.ref_text = _fetch_voice_ref(profile)
        ST.live_mode = bool(live_mode)
        if live_mode:
            ST.face_ready = False; ST.face_id = ""
            nm = profile or requests.get(f"{HUB_URL}/profiles", timeout=5).json().get("active", "")
            pj = requests.get(f"{HUB_URL}/profiles/{quote(nm, safe='')}",
                              params={"include_face": "true"}, timeout=10).json()
            face_bytes = _b64bytes(pj.get("face_b64", ""))
            ST.idle_video = (pj.get("idle_video") or "").strip()
            if not face_bytes:
                ST.push_event({"who": "sys", "warn": "⚠ 演示需要角色人脸,当前角色无人脸"})
                return
            _ensure_live_profile(profile, face_bytes)
            face_id = f"interp_{nm}"
            if _precompute_face(face_bytes, face_id):
                _warmup_lipsync(face_id)
        ST.push_event({"who": "sys", "warn": "🎬 演示开始"})
        for zh in DEMO_SCRIPT:
            if stop and stop.is_set():
                break
            en = _translate_nmt(zh, "zh", "en")
            tid = ST.turn_id("me"); uid = ST.next_uid()
            ST.push_event({"uid": uid, "turn": tid, "who": "me", "zh": zh})
            ST.push_event({"uid": uid, "turn": tid, "who": "me", "en": en})
            if live_mode and ST.face_ready:
                try:
                    _drive_avatar(en, zh)
                except Exception:
                    logger.exception("演示 数字人驱动失败")
            if stop and stop.wait(0.8):
                break
        ST.push_event({"who": "sys", "warn": "🎬 演示结束"})
    except Exception:
        logger.exception("演示运行异常")
    finally:
        ST.demo_running = False
        if live_mode:
            _clear_subtitles()


class DemoReq(BaseModel):
    profile: str = ""
    live_mode: bool = True


@app.post("/demo")
def demo(req: DemoReq):
    """一键演示:无需麦克风,播放预设脚本展示同传数字人。需先停止正在进行的会话。"""
    if ST.running:
        raise HTTPException(409, "请先停止当前同传会话，再运行演示")
    if ST.demo_running:
        return {"ok": True, "already": True}
    ST.demo_running = True
    ST.demo_stop = threading.Event()
    threading.Thread(target=_run_demo, args=(req.profile, bool(req.live_mode)), daemon=True).start()
    return {"ok": True, "lines": len(DEMO_SCRIPT)}


@app.post("/demo/stop")
def demo_stop():
    if ST.demo_stop is not None:
        ST.demo_stop.set()
    return {"ok": True}


class SwitchReq(BaseModel):
    profile: str


@app.post("/switch_profile")
def switch_profile(req: SwitchReq):
    """直播/通话中切换角色:后台准备新角色,就绪后在下一句边界无缝切换(不断流)。"""
    if not ST.running:
        raise HTTPException(409, "未在运行，无法切换；请用 /start 选定角色启动")
    nm = (req.profile or "").strip()
    if not nm:
        raise HTTPException(400, "profile 不能为空")
    if nm == ST.profile and ST.pending_switch is None:
        return {"ok": True, "same": True}
    if ST.switching:
        return {"ok": True, "busy": True}
    ST.switching = True
    threading.Thread(target=_prepare_switch, args=(nm,), daemon=True).start()
    return {"ok": True, "preparing": nm}


class PreloadReq(BaseModel):
    profiles: list[str] = []


@app.post("/preload")
def preload(req: PreloadReq):
    """角色预案池:后台为常用角色预计算人脸+预热参考并缓存,后续切换近即时。
    需在运行中(直播模式)调用;串行预载避免与直播争 GPU。"""
    if not ST.running:
        raise HTTPException(409, "未在运行，无法预载")
    if ST.preset_loading:
        return {"ok": True, "busy": True, "ready": sorted(ST.preset_cache.keys())}
    names = [n.strip() for n in (req.profiles or []) if n and n.strip()]
    todo = [n for n in names if n != ST.profile and n not in ST.preset_cache]
    if not todo:
        return {"ok": True, "ready": sorted(ST.preset_cache.keys())}
    ST.preset_loading = True
    ST.preset_queue = list(todo)
    threading.Thread(target=_preload_worker, args=(todo,), daemon=True).start()
    return {"ok": True, "loading": todo}


class LangCfgReq(BaseModel):
    src: str | None = None
    dst: str | None = None
    restart: bool = False          # True=切语向后自动停→重启采集链，让流式识别语言即时按新语向生效


@app.get("/config/langs")
def get_langs():
    """当前语向 + 可选语言清单(来自 STT，失败回退中英) + 术语表条数，供 UI 语向选择器/术语角标渲染。"""
    comp = _glossary_load()
    with _lang_warm_lock:
        warm = {"pair": list(_lang_warm_state["pair"] or []), "running": _lang_warm_state["running"],
                "done": list(_lang_warm_state["done"]), "at": _lang_warm_state["at"]}
    return {"ok": True, "src": _SRC_LANG, "dst": _DST_LANG, "langs": _fetch_stt_langs(),
            "glossary_on": _GLOSSARY_ON, "glossary_count": sum(len(v) for v in comp.values()),
            "transcript_on": TRANSCRIPT_ON, "transcript_count": len(ST.transcript),
            "warm": warm, "stream_weak": sorted(STREAM_WEAK_LANGS)}


@app.post("/config/langs")
def set_langs(req: LangCfgReq):
    """运行时切换语向：我说 SRC → 对方听 DST（方向B 反之）。默认 zh/en。
    P4-1: 语对变化即后台预热该语对全部翻译层(LLM+远端NMT 双向)，消除兜底层懒加载冷启(实测 ja→zh 73s)。"""
    global _SRC_LANG, _DST_LANG
    old = (_SRC_LANG, _DST_LANG)
    if req.src and req.src.strip():
        _SRC_LANG = req.src.strip().lower()
    if req.dst and req.dst.strip():
        _DST_LANG = req.dst.strip().lower()
    logger.info(f"语向切换 → 我={_SRC_LANG} 对方={_DST_LANG}")
    warm = False
    if (_SRC_LANG, _DST_LANG) != old:
        warm = _lang_warm_kick(_SRC_LANG, _DST_LANG, reason="lang-switch")
        # P5-4 顺带预热新语对的高频句(warmup.json 已多语化)：引擎先热(2s 让路),短语随后慢速灌缓存
        _tm_boot_warmup_kick(delay=2.0)
    # P6-1 TTS 弱语种提示(lang_qa 实测发音不可懂)：字幕/翻译不受影响,提醒别依赖配音
    tts_warn = ""
    if _DST_LANG in TTS_LOW_LANGS:
        tts_warn = f"⚠ 实测克隆配音的{_DST_LANG}发音可懂度低，对方请以字幕为准(翻译/字幕不受影响)"
        ST.push_event({"who": "sys", "warn": tts_warn})
    # B-5 运行中切语向的 ASR 路由提醒：路由决策定格在 /start(采集链不中途重建,不断流)，
    # 但引擎与新语向不匹配必须明说——流式会话切进弱语种=对方的话会被转错,不能让用户带着错引擎播完整场。
    # 反向(分段因弱语种而起,切走后已无弱语种)也提示,可重启换回更跟手的流式。
    asr_advice = ""
    if ST.running and (_SRC_LANG, _DST_LANG) != old:
        weak_now = {_SRC_LANG, _DST_LANG} & STREAM_WEAK_LANGS
        if ST.stream_on and weak_now:
            asr_advice = (f"⚠ 新语向含流式弱语种 {'/'.join(sorted(weak_now))}，本会话仍在流式引擎上(识别恐不准)。"
                          "建议停止→重新开始同传，将自动改用更准的 Whisper 分段")
        elif ST.stream_on:
            asr_advice = "ℹ 语向已切换：流式识别的语言提示仍按开播时初始化，若识别变差请停止→重新开始同传"
        elif (not weak_now and STREAM_STT_DEFAULT
              and "弱语种" in (ST.asr_route or {}).get("why", "")):
            asr_advice = "ℹ 新语向已无弱语种：重新开始同传可恢复流式逐词字幕(更跟手)"
        if asr_advice and not req.restart:
            ST.push_event({"who": "sys", "warn": asr_advice})
            logger.info(f"[B-5] 切语向路由提醒: {asr_advice}")

    # 一键切语向：自动停→重启采集链，让流式识别语言(开播时冻结的 WS language)立即按新语向重开。
    # 原样重建上一场的设备/角色/模式(从 ST 快照)，用户无需手动停/开。设备索引在会话内稳定。
    restarted = False
    restart_err = ""
    if req.restart and ST.running:
        try:
            recipe = dict(
                mic_index=(ST.mic_index if ST.mic_index is not None else -1),
                cable_index=(ST.cable_index if ST.cable_index is not None else -1),
                loopback_index=(ST.loop_index if ST.loop_index is not None else -1),
                loopback_is_output=bool(ST.loop_is_output),
                profile=ST.profile or "",
                mode=ST.mode or "local",
                live_mode=bool(ST.live_mode),
                mic_net_url=getattr(ST, "mic_net_url", "") or "",
            )   # 不传 stream：走 StartReq 默认(None)→按新语向重新决策流式/分段(弱语种自动回退更准)
            stop()
            time.sleep(0.3)
            sr = start(StartReq(**recipe))
            restarted = bool(sr.get("ok"))
            ce = sr.get("cap_a_err") or sr.get("cap_b_err")
            if ce:
                restart_err = f"采集告警 a={sr.get('cap_a_err') or 'OK'} b={sr.get('cap_b_err') or 'OK'}"
            logger.info(f"[切语向重启] 我={_SRC_LANG} 对方={_DST_LANG} ok={restarted} {restart_err}")
            ST.push_event({"who": "sys", "warn":
                           f"🔄 已切换并重启：我说「{_LANG_NAMES_ZH.get(_SRC_LANG, _SRC_LANG)}」→ "
                           f"对方「{_LANG_NAMES_ZH.get(_DST_LANG, _DST_LANG)}」"
                           + (f"（{(ST.asr_route or {}).get('label','')}）" if ST.asr_route else "")})
        except Exception as e:
            restart_err = str(e)[:180]
            logger.exception("切语向自动重启失败")
            ST.push_event({"who": "sys", "warn": f"⚠ 切语向重启失败：{restart_err}（可手动停止再开始）"})

    return {"ok": True, "src": _SRC_LANG, "dst": _DST_LANG, "warm_kicked": warm,
            "tts_low_warn": tts_warn, "asr_advice": ("" if req.restart else asr_advice),
            "restarted": restarted, "restart_err": restart_err,
            "asr_route": (ST.asr_route or {}).get("label", "")}


# ── P0-S2S 云同传配置面(查看/切换后端/热更术语表)。密钥只报"是否已配",绝不回显 ──
class S2SCfgReq(BaseModel):
    backend: str = None          # none|seed(下场开播生效)
    mode: str = None             # s2s(带克隆配音)|s2t(纯字幕)
    speaker: str = None          # 云端音色名；空=复刻当前说话人(默认,与克隆定位对齐)
    refresh_glossary: bool = False   # 会话中把最新术语表热推到云端


@app.get("/config/s2s")
def get_s2s_cfg():
    rc = _s2s_runtime_cfg()
    factory, why = (None, "")
    if rc["backend"] != "none":
        factory, why = _s2s_make_factory()
    creds = False
    try:
        import s2s_backends as _s2s
        creds = _s2s.seed_config_ready(os.environ)[0]
    except Exception:
        why = why or "s2s_backends 模块不可用"
    return {"ok": True, "backend": rc["backend"], "mode": rc["mode"],
            "speaker": rc["speaker"], "creds_configured": creds,
            "ready": factory is not None, "why": why,
            "active": ST.s2s_on, "state": (ST.s2s_state or None),
            "langs": {"src": _SRC_LANG, "dst": _DST_LANG},
            "backends": ["none", "seed"]}


@app.post("/config/s2s")
def set_s2s_cfg(req: S2SCfgReq):
    """运行时改 S2S 配置(落盘 data/s2s_config.json)。后端/模式/音色下场开播生效；
    refresh_glossary=true 且会话进行中 → 立即把术语表热推云端(UpdateConfig)。"""
    patch = {}
    if req.backend is not None:
        b = req.backend.strip().lower()
        if b not in ("none", "seed"):
            raise HTTPException(400, f"未知后端 {b}(可选 none/seed)")
        patch["backend"] = b
    if req.mode is not None:
        m = req.mode.strip().lower()
        if m not in ("s2s", "s2t"):
            raise HTTPException(400, "mode 只能是 s2s(带配音)/s2t(纯字幕)")
        patch["mode"] = m
    if req.speaker is not None:
        patch["speaker"] = req.speaker.strip()
    cfg = _s2s_cfg_save(patch) if patch else _s2s_runtime_cfg()
    refreshed = False
    if req.refresh_glossary and ST.s2s_sink is not None:
        try:
            refreshed = ST.s2s_sink.refresh_corpus()
        except Exception:
            logger.exception("术语表热推云端失败")
    if patch:
        logger.info(f"S2S 配置更新: {patch}(下场开播生效)")
        ST.push_event({"who": "sys", "warn":
                       f"ℹ 云同传配置已更新({'/'.join(f'{k}={v}' for k, v in patch.items())})，"
                       "下次开播生效"})
    return {"ok": True, "cfg": cfg, "glossary_refreshed": refreshed,
            "note": ("配置下场开播生效" if patch else "")}


# ── 会话转写导出：TXT(含时间) / SRT(配录像字幕) / JSON。内存取当前/最近会话，?session= 取历史落盘 ──
_TS_WHO_LABEL = {"me": "我", "other": "对方"}
_SRT_MIN_SEC = 1.2      # 单条字幕最短显示时长
_SRT_MAX_SEC = 7.0      # 单条字幕最长显示时长(下一条更早则截到下一条)


def _srt_ts(sec: float) -> str:
    """秒 → SRT 时间戳 HH:MM:SS,mmm。"""
    if not sec or sec < 0:
        sec = 0.0
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _clock(sec: float) -> str:
    """秒 → 人读时钟 M:SS 或 H:MM:SS(超 1 小时)。"""
    t = int(sec) if sec and sec > 0 else 0
    h, rem = divmod(t, 3600); m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _entry_lines(e: dict, content: str):
    """按 content(both/src/trans)取一条转写的可显示行(空串过滤)。"""
    src = (e.get("src") or "").strip(); trans = (e.get("trans") or "").strip()
    if content == "src":
        return [src] if src else []
    if content == "trans":
        return [trans] if trans else []
    out = []
    if src:   out.append(src)
    if trans: out.append(trans)
    return out


def _valid_stamp(s: str) -> bool:
    """会话标识须形如 20260702_010203(8 位日期 + _ + 6 位时间)，防路径穿越。"""
    return len(s) == 15 and s[8] == "_" and s[:8].isdigit() and s[9:].isdigit()


def _transcript_load(session: str = ""):
    """返回 (entries, meta)。session=时间戳→读盘历史；否则取内存(当前/最近会话)。"""
    session = (session or "").strip()
    if session:
        if not _valid_stamp(session):
            raise HTTPException(400, "无效的会话标识(应形如 20260702_010203)")
        path = os.path.join("logs", f"interp_transcript_{session}.json")
        if not os.path.exists(path):
            raise HTTPException(404, "该会话转写不存在")
        try:
            with open(path, "r", encoding="utf-8") as f:
                j = json.load(f)
        except Exception:
            raise HTTPException(500, "读取会话转写失败")
        entries = j.get("entries") or []
        return entries, {"session": session, "src": j.get("src"), "dst": j.get("dst"),
                         "count": len(entries), "live": False}
    with ST.lock:
        entries = list(ST.transcript)
    return entries, {"session": None, "src": _SRC_LANG, "dst": _DST_LANG,
                     "count": len(entries), "live": ST.running}


def _transcript_filter(entries, who: str):
    """按说话人过滤：me / other / 其它(=全部)。"""
    if who in ("me", "other"):
        return [e for e in entries if e.get("who") == who]
    return entries


def _transcript_txt(entries, content: str = "both", header: str = "") -> str:
    """转写 → 人读 TXT。每条 [时间] 说话人\\t原文，双语时译文换行缩进。"""
    lines = [header, ""] if header else []
    for e in entries:
        parts = _entry_lines(e, content)
        if not parts:
            continue
        who = _TS_WHO_LABEL.get(e.get("who"), e.get("who") or "")
        lines.append(f"[{_clock(e.get('t') or 0.0)}] {who}\t{parts[0]}")
        for extra in parts[1:]:
            lines.append(f"           → {extra}")
    return "\n".join(lines).rstrip() + "\n"


def _transcript_srt(entries, content: str = "both") -> str:
    """转写 → SRT 字幕。cue 结束=下一条起点(截到 [MIN,MAX])；双语=两行(原文/译文)。"""
    rows = []
    for e in entries:
        ls = _entry_lines(e, content)
        if ls:
            rows.append((float(e.get("t") or 0.0), ls))
    out = []
    for i, (start, ls) in enumerate(rows):
        nxt = rows[i + 1][0] if i + 1 < len(rows) else start + _SRT_MAX_SEC
        end = min(start + _SRT_MAX_SEC, nxt)
        if end <= start:
            end = start + _SRT_MIN_SEC
        out.append(str(i + 1))
        out.append(f"{_srt_ts(start)} --> {_srt_ts(end)}")
        out.extend(ls)
        out.append("")
    return ("\n".join(out).strip() + "\n") if out else ""


def _dl_headers(download, fn: str):
    return {"Content-Disposition": f'attachment; filename="{fn}"'} if download else {}


@app.get("/transcript")
def transcript_json(who: str = "all", session: str = ""):
    """会话转写(JSON)：内存当前/最近会话，?session=时间戳取历史。?who=me/other/all 过滤。"""
    entries, meta = _transcript_load(session)
    entries = _transcript_filter(entries, who)
    return {"ok": True, **meta, "who": who, "count_shown": len(entries), "entries": entries}


@app.get("/transcript.txt")
def transcript_txt(who: str = "all", content: str = "both", session: str = "", download: int = 0):
    """转写文本(含相对时间)。content=both/src/trans；download=1 触发下载。"""
    entries, meta = _transcript_load(session)
    entries = _transcript_filter(entries, who)
    head = (f"通译 LingoX 转写记录 · 我({meta.get('src')}) ⇄ 对方({meta.get('dst')}) · "
            f"{len(entries)} 条" + ("" if meta.get("live") else " · 已结束"))
    body = _transcript_txt(entries, content, head)
    fn = f"lingox_transcript_{session or 'live'}.txt"
    return PlainTextResponse(body, headers=_dl_headers(download, fn),
                             media_type="text/plain; charset=utf-8")


@app.get("/transcript.srt")
def transcript_srt(who: str = "all", content: str = "both", session: str = "", download: int = 0):
    """SRT 字幕文件(配录像/剪辑)。content=both 时双语双行；download=1 触发下载。"""
    entries, _meta = _transcript_load(session)
    entries = _transcript_filter(entries, who)
    body = _transcript_srt(entries, content)
    fn = f"lingox_subtitle_{session or 'live'}.srt"
    return PlainTextResponse(body, headers=_dl_headers(download, fn),
                             media_type="application/x-subrip; charset=utf-8")


@app.get("/transcript.json")
def transcript_download_json(who: str = "all", session: str = "", download: int = 1):
    """原始转写数据(JSON 文件下载)。"""
    entries, meta = _transcript_load(session)
    entries = _transcript_filter(entries, who)
    body = json.dumps({"ok": True, **meta, "who": who, "count_shown": len(entries),
                       "entries": entries}, ensure_ascii=False, indent=2)
    fn = f"lingox_transcript_{session or 'live'}.json"
    return Response(body, headers=_dl_headers(download, fn),
                    media_type="application/json; charset=utf-8")


@app.get("/transcript/list")
def transcript_list(limit: int = 20):
    """历史会话转写清单(读 logs/interp_transcript_*.json)，供选择导出往期。"""
    import glob
    out = []
    try:
        files = sorted(glob.glob(os.path.join("logs", "interp_transcript_*.json")),
                       reverse=True)[:max(1, limit)]
        for fp in files:
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    j = json.load(f)
                stem = os.path.basename(fp)[len("interp_transcript_"):-len(".json")]
                out.append({"session": stem, "count": j.get("count"),
                            "src": j.get("src"), "dst": j.get("dst"), "ended_at": j.get("ended_at")})
            except Exception:
                continue
    except Exception:
        pass
    return {"ok": True, "sessions": out}


@app.get("/config/glossary")
def get_glossary():
    """查看术语锁定表(按语向分组)+ 条数 + 文件路径。改 data/glossary.json 后调 /reload 或改文件即热加载。"""
    comp = _glossary_load()
    by_dir = {k: [{"src": s, "dst": d} for (s, d, _lat) in v] for k, v in comp.items()}
    return {"ok": True, "on": _GLOSSARY_ON, "path": _GLOSSARY_PATH,
            "count": sum(len(v) for v in comp.values()), "dirs": list(comp.keys()), "glossary": by_dir}


@app.post("/config/glossary/reload")
def reload_glossary():
    """强制重载术语表(改文件后即时生效，无需重启进程)。返回新条数与语向清单。"""
    comp = _glossary_load(force=True)
    n = sum(len(v) for v in comp.values())
    logger.info(f"术语表手动重载：{n} 条 / {len(comp)} 语向")
    return {"ok": True, "count": n, "dirs": list(comp.keys())}


@app.get("/ger/learned")
def ger_learned():
    """P1-4 纠错自学习台账：GER 修正对累计次数 + 是否已采纳进术语表(按最近使用排序)。"""
    with _ger_learn_lock:
        store = _ger_learn_load()
    items = [dict(v, key=k) for k, v in store.items()]     # 带 key,供一键采纳回传
    items.sort(key=lambda e: e.get("last", 0), reverse=True)
    return {"ok": True, "count": len(items),
            "adopted": sum(1 for e in items if e.get("adopted")),
            "auto_adopt_at": _GER_LEARN_ADOPT, "items": items[:100]}


class GerAdoptReq(BaseModel):
    key: str            # 台账键 "lang|错写法→对写法"(GET /ger/learned 的 items[].key)


@app.post("/ger/learned/adopt")
def ger_learned_adopt(req: GerAdoptReq):
    """P2 一键采纳：不等出现次数阈值，立即把该修正对的正确写法写入术语热词。
    已在词表 → 只标记 adopted(幂等)。供运维页「学习」徽章按钮调用。"""
    with _ger_learn_lock:
        store = _ger_learn_load()
        e = store.get(req.key)
        if e is None:
            return {"ok": False, "error": "not_found"}
        right = (e.get("right") or "").strip()
        already = _glossary_has_term(right)
        ok = bool(right) and (already or _glossary_adopt_term(right))
        if ok and not e.get("adopted"):
            e["adopted"] = True
            try:
                os.makedirs(os.path.dirname(_GER_LEARN_PATH), exist_ok=True)
                with open(_GER_LEARN_PATH, "w", encoding="utf-8") as f:
                    json.dump(store, f, ensure_ascii=False, indent=2)
            except Exception:
                logger.exception("[纠错自学习] 采纳落盘失败")
    if ok:
        logger.info(f"[纠错自学习] 手动采纳「{e.get('wrong')}→{right}」进术语热词"
                    + ("(词表已有,仅标记)" if already else ""))
    return {"ok": ok, "right": right, "already_in_glossary": already}


@app.post("/ger/learned/clear")
def ger_learned_clear():
    """清空自学习台账(词表里已采纳的条目不受影响,要删去 glossary.json 删)。"""
    with _ger_learn_lock:
        try:
            if os.path.exists(_GER_LEARN_PATH):
                os.remove(_GER_LEARN_PATH)
        except Exception:
            logger.exception("清空纠错自学习台账失败")
            return {"ok": False}
    return {"ok": True}


# ── P3 转写复盘接口：剪辑清单 / 音频回放 / 对错标注 / 真实 CER 统计 ──
def _review_load_session(session: str):
    try:
        with open(os.path.join("logs", f"review_clips_{session}.json"), encoding="utf-8") as f:
            return json.load(f).get("clips") or []
    except Exception:
        return []


@app.get("/review/clips")
def review_clips(session: str = ""):
    """复盘剪辑清单：默认当前会话(内存实时)，?session=戳 读往期索引。附可选会话列表+已有标注。"""
    import glob as _g
    hist = sorted((os.path.basename(p)[len("review_clips_"):-len(".json")]
                   for p in _g.glob(os.path.join("logs", "review_clips_*.json"))), reverse=True)[:12]
    with ST.lock:
        cur = ST.review_stamp if ST.review_clips else ""
        cur_clips = [dict(c) for c in ST.review_clips]
    sessions = ([cur] if cur and cur not in hist else []) + hist
    live = bool(cur) and (not session or session == cur)
    sel = cur if live else (session or (hist[0] if hist else ""))
    clips = cur_clips if live else _review_load_session(sel)
    marks = _review_marks_index()
    for c in clips:
        m = marks.get((sel, c.get("file")))
        if m:
            c["mark"] = {"ok": m.get("ok"), "correct": m.get("correct"), "cer": m.get("cer")}
    return {"ok": True, "session": sel, "live": live, "running": ST.running,
            "sessions": sessions, "clips": clips}


@app.get("/review/audio")
def review_audio(session: str, file: str):
    """剪辑音频回放(16k mono wav)。路径钉死在 review_audio/<戳>/<segNNN_who.wav> 防目录穿越。"""
    if not _re.fullmatch(r"\d{8}_\d{6}", session or "") \
            or not _re.fullmatch(r"seg\d{3}_(me|other)\.wav", file or ""):
        raise HTTPException(400, "bad path")
    p = os.path.join(_REVIEW_DIR, session, file)
    if not os.path.exists(p):
        raise HTTPException(404, "clip not found")
    with open(p, "rb") as f:
        return Response(f.read(), media_type="audio/wav")


class ReviewMarkReq(BaseModel):
    session: str
    file: str
    ok: bool
    correct: str = ""       # 判错时可填正确文本 → 才能算真实 CER


@app.post("/review/mark")
def review_mark(req: ReviewMarkReq):
    """人工标注一段剪辑：对/错(+可选正确文本)。重复标注后写覆盖先写。"""
    clips = ([dict(c) for c in ST.review_clips] if req.session == ST.review_stamp
             else _review_load_session(req.session))
    hit = next((c for c in clips if c.get("file") == req.file), None)
    if hit is None:
        raise HTTPException(404, "clip not found")
    cer_val = None
    if req.ok:
        cer_val = 0.0
    elif req.correct.strip():
        cer_val = round(_char_cer(req.correct.strip(), hit.get("src") or ""), 4)
    row = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "session": req.session,
           "file": req.file, "who": hit.get("who"), "text": hit.get("src"),
           "ok": bool(req.ok), "correct": req.correct.strip(), "cer": cer_val}
    with _review_mark_lock:
        os.makedirs(os.path.dirname(_REVIEW_MARKS), exist_ok=True)
        with open(_REVIEW_MARKS, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    # P4 人工标注反哺自学习：判错+给了正确文本=人工确认的"错写法→对写法"，
    # 与 GER 自动纠错走同一台账/阈值(出现 2 次自动进热词,或运维页一键采纳)。
    if not req.ok and row["correct"] and _norm_text(row["correct"]) != _norm_text(hit.get("src") or ""):
        lang = _SRC_LANG if hit.get("who") == "me" else _DST_LANG
        try:
            _ger_learn_note(hit.get("src") or "", row["correct"], lang)
        except Exception:
            pass
    return {"ok": True, "mark": row}


@app.get("/review/stats")
def review_stats():
    """人工标注聚合：整体/近 20 场的句准率 + 真实 CER(有正确文本的样本)。"""
    by = {}
    for m in _review_marks_index().values():
        b = by.setdefault(m.get("session") or "?", {"marked": 0, "ok": 0, "cers": []})
        b["marked"] += 1
        if m.get("ok"):
            b["ok"] += 1
        if m.get("cer") is not None:
            b["cers"].append(float(m["cer"]))
    rows = []
    for s in sorted(by, reverse=True)[:20]:
        b = by[s]
        rows.append({"session": s, "marked": b["marked"], "ok": b["ok"],
                     "acc": round(b["ok"] / b["marked"], 3) if b["marked"] else None,
                     "cer_avg": round(sum(b["cers"]) / len(b["cers"]), 4) if b["cers"] else None})
    tot = sum(r["marked"] for r in rows); tok = sum(r["ok"] for r in rows)
    cers = [c for b in by.values() for c in b["cers"]]
    return {"ok": True, "sessions": rows,
            "total": {"marked": tot, "acc": round(tok / tot, 3) if tot else None,
                      "cer_avg": round(sum(cers) / len(cers), 4) if cers else None}}


@app.get("/review", response_class=HTMLResponse)
def review_page():
    """P3 转写复盘页(自包含)：逐句回放+对/错标注(错可填正确文本)→真实 CER 趋势。"""
    return _REVIEW_PAGE


_REVIEW_PAGE = r"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><link rel=icon href=/favicon.ico>
<meta name=theme-color content="#080b10">
<title>转写复盘 · 真实CER标注</title>
<style>
:root{color-scheme:dark;--acc:#4f7aff;--acc2:#a855f7;--bg:#0b0e14;--card:#12172290;--bord:#232a3a;--mut:#8b94a7}
body{margin:0;background:var(--bg);color:#e8ecf4;font:14px/1.6 system-ui,"Microsoft YaHei",sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:18px}
h1{font-size:18px;margin:0 0 4px}.sub{color:var(--mut);font-size:12px;margin-bottom:14px}
select{background:#0c111c;color:#e8ecf4;border:1px solid var(--bord);border-radius:8px;padding:5px 8px}
.row{background:var(--card);border:1px solid var(--bord);border-radius:12px;padding:10px 12px;margin:8px 0;
     display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.row audio{height:30px;max-width:230px}
.txt{flex:1;min-width:200px}.meta{color:var(--mut);font-size:11px}
.st{font-size:11px;border:1px solid var(--bord);border-radius:99px;padding:1px 8px;color:var(--mut)}
.st.fixed{color:#7dd3fc;border-color:#7dd3fc55}.st.revived{color:#86efac;border-color:#86efac55}
.st.vetoed{color:#fca5a5;border-color:#fca5a555}
button{background:#0c111c;color:#e8ecf4;border:1px solid var(--bord);border-radius:8px;padding:4px 12px;cursor:pointer}
button:hover{border-color:var(--acc)}button.on{background:var(--acc);border-color:var(--acc)}
button.bad.on{background:#b91c1c;border-color:#b91c1c}
input{background:#0c111c;color:#e8ecf4;border:1px solid var(--bord);border-radius:8px;padding:4px 8px;min-width:200px}
.stats{margin-top:14px;padding:10px 12px;background:var(--card);border:1px solid var(--bord);border-radius:12px;
       color:var(--mut);font-size:12px}
.empty{color:var(--mut);padding:30px;text-align:center}
</style></head><body><div class=wrap>
<h1>转写复盘 · 真实 CER 标注</h1>
<div class=sub>逐句听音频，点「对/错」；判错时可填正确文本，系统按编辑距离算真实字错率。撤回段(红)用于审计误杀。
会话: <select id=sel onchange="load(this.value)"></select> <span id=live class=meta></span></div>
<div id=list></div>
<div class=stats id=stats>统计加载中…</div>
</div><script>
let CUR='';
async function load(session){
  const r=await (await fetch('/review/clips'+(session?('?session='+encodeURIComponent(session)):''),{cache:'no-store'})).json();
  CUR=r.session;
  const sel=document.getElementById('sel');
  sel.innerHTML=(r.sessions||[]).map(s=>'<option value="'+s+'"'+(s===r.session?' selected':'')+'>'+s+'</option>').join('');
  document.getElementById('live').textContent=r.live?'（当前会话·实时）':'';
  const box=document.getElementById('list');
  if(!(r.clips||[]).length){ box.innerHTML='<div class=empty>本场暂无剪辑。开着通话说几句话，定稿后自动留档。</div>'; renderStats(); return; }
  box.innerHTML=r.clips.map((c,i)=>{
    const st=c.state||'ok', m=c.mark;
    return '<div class=row id="r'+i+'">'
      +'<audio controls preload=none src="/review/audio?session='+CUR+'&file='+c.file+'"></audio>'
      +'<div class=txt>'+String(c.src||'').replace(/</g,'&lt;')
      +' <span class="st '+st+'">'+st+'</span>'
      +'<div class=meta>'+c.who+' · '+c.dur_s+'s · t+'+c.t+'s'+(m&&m.cer!=null?(' · 已标CER '+(m.cer*100).toFixed(0)+'%'):'')+'</div></div>'
      +'<button class="'+(m&&m.ok?'on':'')+'" onclick="mark('+i+',true)">对</button>'
      +'<button class="bad '+(m&&m.ok===false?'on':'')+'" onclick="togglebad('+i+')">错</button>'
      +'<span id="w'+i+'" style="display:'+(m&&m.ok===false?'':'none')+'">'
      +'<input id="in'+i+'" placeholder="正确文本(可选,填了才算CER)" value="'+((m&&m.correct)||'').replace(/"/g,'&quot;')+'">'
      +' <button onclick="mark('+i+',false)">提交</button></span></div>';
  }).join('');
  window._clips=r.clips; renderStats();
}
function togglebad(i){ const w=document.getElementById('w'+i); w.style.display=w.style.display==='none'?'':'none'; }
async function mark(i,ok){
  const c=window._clips[i];
  const correct=ok?'':(document.getElementById('in'+i).value||'');
  await fetch('/review/mark',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({session:CUR,file:c.file,ok:ok,correct:correct})});
  load(CUR);
}
async function renderStats(){
  try{
    const s=await (await fetch('/review/stats',{cache:'no-store'})).json();
    const t=s.total||{};
    document.getElementById('stats').textContent='累计标注 '+(t.marked||0)+' 句'
      +(t.acc!=null?(' · 句准率 '+(t.acc*100).toFixed(0)+'%'):'')
      +(t.cer_avg!=null?(' · 真实CER '+(t.cer_avg*100).toFixed(1)+'%'):'')
      +'（各场: '+(s.sessions||[]).slice(0,5).map(x=>x.session.slice(9)+' '+(x.acc!=null?(x.acc*100).toFixed(0)+'%':'-')).join(' · ')+'）';
  }catch(e){}
}
load('');setInterval(()=>{ if(document.getElementById('live').textContent) load(CUR); },15000);
</script></body></html>"""


class GlossaryReq(BaseModel):
    glossary: dict          # {"zh->en":[{"src","dst"}], "*":[...], ...}


def _glossary_save(incoming: dict) -> dict:
    """校验并整表覆盖真实语向键(保留 _ 注释/示例键)→原子落盘→热重载。返回 {count, dirs}。
    纯函数(与端点解耦，便于单测)：非法结构抛 ValueError；空 src/dst 行自动丢弃。"""
    if not isinstance(incoming, dict):
        raise ValueError("glossary 必须是对象")
    clean = {}
    for key, items in incoming.items():
        if not isinstance(key, str) or key.startswith("_"):
            continue                      # 忽略 _ 注释键(不让前端覆盖示例)
        if not isinstance(items, list):
            raise ValueError(f"语向 {key} 的值必须是数组")
        rows = []
        for it in items:
            if not isinstance(it, dict):
                raise ValueError(f"语向 {key} 的条目必须是对象")
            s = str(it.get("src") or "").strip()
            d = str(it.get("dst") or "").strip()
            if s and d:
                rows.append({"src": s, "dst": d})   # 空行静默丢弃
        clean[key.strip().lower()] = rows
    base = {}
    try:
        with open(_GLOSSARY_PATH, encoding="utf-8") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                base = loaded
    except Exception:
        base = {}
    merged = {k: v for k, v in base.items() if isinstance(k, str) and k.startswith("_")}  # 留注释/示例
    merged.update(clean)
    os.makedirs(os.path.dirname(_GLOSSARY_PATH), exist_ok=True)
    tmp = _GLOSSARY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _GLOSSARY_PATH)       # 原子替换(避免半写坏文件)
    comp = _glossary_load(force=True)
    return {"count": sum(len(v) for v in comp.values()), "dirs": list(comp.keys())}


# ── 术语表 CSV 批量导入/导出：运营在 Excel/表格里规模化维护(导出→编辑→回传)，复用 _glossary_save 落盘 ──
#   CSV 列 dir,src,dst(dir=语向如 zh->en 或 * ；src=源写法；dst=目标写法)。导出含 BOM 便于 Excel 认 UTF-8。
#   导入首行若含表头(dir/src/dst 或 方向/源/目标…)则按名映射列(顺序随意、允许缺 dir 列→默认 *)，否则按位置。
_CSV_DIR_HDR = frozenset({"dir", "direction", "方向", "语向"})
_CSV_SRC_HDR = frozenset({"src", "source", "term", "源", "源术语", "原文"})
_CSV_DST_HDR = frozenset({"dst", "dest", "target", "目标", "目标写法", "译文"})


def _glossary_to_csv(comp: dict, dir_filter: str = "") -> str:
    """编译后的术语表 → CSV 文本(列 dir,src,dst)。dir_filter 非空则只导该语向。始终含表头(空表=纯模板)。"""
    import csv as _csv, io as _io
    buf = _io.StringIO()
    w = _csv.writer(buf, lineterminator="\r\n")
    w.writerow(["dir", "src", "dst"])
    for key in sorted(comp.keys()):
        if dir_filter and key != dir_filter:
            continue
        for (s, d, _lat) in comp[key]:
            w.writerow([key, s, d])
    return buf.getvalue()


def _glossary_from_csv(text: str):
    """CSV 文本 → ({语向:[{src,dst}]}, skipped)。表头按名映射(缺 dir 列→'*')，无表头按位置(dir,src,dst)。
    空 src/dst 行丢弃并计入 skipped；同语向内按 exact src 去重(后者覆盖)；容忍 BOM。"""
    import csv as _csv, io as _io
    text = (text or "").lstrip("\ufeff")
    if not text.strip():
        return {}, 0
    rows = list(_csv.reader(_io.StringIO(text)))
    if not rows:
        return {}, 0
    idx = {"dir": 0, "src": 1, "dst": 2}
    start = 0
    head = [str(c).strip().lower() for c in rows[0]]
    if any((c in _CSV_DIR_HDR or c in _CSV_SRC_HDR or c in _CSV_DST_HDR) for c in head):
        m = {"dir": -1, "src": -1, "dst": -1}
        for i, c in enumerate(head):
            if c in _CSV_DIR_HDR and m["dir"] < 0: m["dir"] = i
            elif c in _CSV_SRC_HDR and m["src"] < 0: m["src"] = i
            elif c in _CSV_DST_HDR and m["dst"] < 0: m["dst"] = i
        if m["src"] >= 0 and m["dst"] >= 0:      # 表头至少要能定位 src/dst，否则当数据按位置解析
            idx = m; start = 1
    out = {}; skipped = 0
    for r in rows[start:]:
        if not r or all(not str(c).strip() for c in r):
            continue
        def cell(k):
            i = idx[k]
            return str(r[i]).strip() if 0 <= i < len(r) else ""
        s = cell("src"); d = cell("dst")
        if not s or not d:
            skipped += 1
            continue
        dr = cell("dir").lower() or "*"
        lst = out.setdefault(dr, [])
        for existing in lst:
            if existing["src"] == s:
                existing["dst"] = d
                break
        else:
            lst.append({"src": s, "dst": d})
    return out, skipped


def _glossary_merge_dirs(base: dict, incoming: dict) -> dict:
    """merge 模式：以现有 by_dir 为底，incoming 覆盖/追加(同语向同 exact src → 覆盖 dst)。返回合并 by_dir。"""
    out = {k: [dict(x) for x in v] for k, v in (base or {}).items()}
    for dr, items in (incoming or {}).items():
        lst = out.setdefault(dr, [])
        for it in items:
            for existing in lst:
                if existing["src"] == it["src"]:
                    existing["dst"] = it["dst"]
                    break
            else:
                lst.append({"src": it["src"], "dst": it["dst"]})
    return out


@app.post("/config/glossary")
def save_glossary(req: GlossaryReq):
    """保存术语表(整表覆盖真实语向键，保留 data/glossary.json 里 _ 开头的注释/示例)+原子落盘+热重载。"""
    try:
        res = _glossary_save(req.glossary or {})
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"写入术语表失败：{e}")
    logger.info(f"术语表已保存：{res['count']} 条 / {len(res['dirs'])} 语向")
    return {"ok": True, **res}


@app.get("/config/glossary.csv")
def export_glossary_csv(dir: str = "", download: int = 1):
    """导出术语表为 CSV(列 dir,src,dst；含 BOM 便于 Excel 认 UTF-8)。?dir= 只导某语向；download=0 浏览器内预览。"""
    comp = _glossary_load()
    body = "\ufeff" + _glossary_to_csv(comp, (dir or "").strip().lower())
    headers = {"Content-Disposition": 'attachment; filename="glossary.csv"'} if download else {}
    return PlainTextResponse(body, headers=headers, media_type="text/csv; charset=utf-8")


class GlossaryCsvReq(BaseModel):
    csv: str = ""
    mode: str = "merge"          # merge=并入现有(同语向同源覆盖) / replace=整表替换真实语向


@app.post("/config/glossary/import_csv")
def import_glossary_csv(req: GlossaryCsvReq):
    """从 CSV 批量导入术语。mode=merge 并入现有(同语向同源覆盖)/replace 整表替换。复用 _glossary_save
    (校验+保留 _ 注释+原子落盘+热重载)。无有效行→400。返回 parsed/skipped/最终条数。"""
    parsed, skipped = _glossary_from_csv(req.csv or "")
    if not parsed:
        raise HTTPException(400, f"CSV 未解析出有效术语(跳过 {skipped} 行)。需 dir,src,dst 三列且 src/dst 非空。")
    mode = (req.mode or "merge").strip().lower()
    if mode == "replace":
        final = parsed
    else:
        mode = "merge"
        comp = _glossary_load()
        base = {k: [{"src": s, "dst": d} for (s, d, _l) in v] for k, v in comp.items()}
        final = _glossary_merge_dirs(base, parsed)
    try:
        res = _glossary_save(final)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"写入术语表失败：{e}")
    parsed_n = sum(len(v) for v in parsed.values())
    logger.info(f"术语表 CSV 导入({mode})：解析 {parsed_n} / 跳过 {skipped} → 现 {res['count']} 条")
    return {"ok": True, "mode": mode, "parsed": parsed_n, "skipped": skipped, **res}


@app.get("/config/translate_cache")
def get_translate_cache():
    """翻译缓存(TM)命中统计：hit/miss/命中率/当前条数/上限 + 最近一次预热汇总。供运维观测缓存效益。"""
    with _tr_cache_lock:
        hit, miss, size = _tr_cache_stat["hit"], _tr_cache_stat["miss"], len(_tr_cache)
    tot = hit + miss
    with _tm_warmup_lock:
        warmup = dict(_tm_warmup_last)
    return {"ok": True, "on": _TR_CACHE_ON, "size": size, "max": _TR_CACHE_MAX,
            "hit": hit, "miss": miss, "hit_rate": round(hit / tot, 3) if tot else 0.0,
            "warmup_on": _TM_WARMUP_ON, "warmup": warmup,
            "persist_on": _TM_CACHE_PERSIST, "boot_warmup": dict(_tm_boot_state),
            "survival": _gloss_surv_report()}


@app.post("/config/translate_cache/clear")
def clear_translate_cache():
    """清空翻译缓存(切换引擎/怀疑陈译时手动清；改词表会自动失效无需手清)。"""
    _tr_cache_clear()
    logger.info("翻译缓存已清空")
    return {"ok": True}


@app.post("/config/tm_warmup")
def trigger_tm_warmup():
    """手动触发翻译记忆预热(当前语向，后台跑，立即返回)。轮询 GET /config/translate_cache 看 warmup 汇总。
    会话级预热被 env 关闭时(默认,避免与 LLM 抢 GPU)改走开机空闲预热器(慢速句间留隙版)。"""
    started = _tm_warmup_kick(_SRC_LANG, _DST_LANG, delay=0)
    boot_started = False
    if not started:
        boot_started = _tm_boot_warmup_kick(delay=0)
    return {"ok": True, "started": started, "boot_started": boot_started,
            "src": _SRC_LANG, "dst": _DST_LANG,
            "warmup_on": _TM_WARMUP_ON, "boot_warmup_on": _TM_WARMUP_BOOT, "cache_on": _TR_CACHE_ON}


@app.post("/config/glossary/survival_probe")
def glossary_survival_probe():
    """按需实测当前会话两语向的术语占位真·存活率(绕缓存,术语密集句含相邻压力,同步过真实 MT)。
    结果并入在线自检 → /ops「占位存活」徽章即刻确定判定 + 若某向被打散→跨阈值触发系统告警。
    用途：开播前自检 / 换 MT 后端(NLLB 等)后逐语向回归,不必等真实流量慢慢攒够样本。"""
    return {"ok": True, **_gloss_survival_probe()}


@app.get("/config/tm_warmup/preview")
def tm_warmup_preview():
    """预热将预译的短语预览：静态表 + 转写自学 + (opt-in)未命中反哺，各语言计数 + 自学样例。供核对。"""
    static = _tm_warmup_load()
    learned = _tm_warmup_learn()
    from_stats = _tm_warmup_stats_phrases()
    merged = _tm_warmup_merge(_tm_warmup_merge(static, learned), from_stats)
    cnt = lambda d: {k: len(v) for k, v in d.items()}
    return {"ok": True, "warmup_on": _TM_WARMUP_ON, "learn_on": _TM_WARMUP_LEARN,
            "learn_min": _TM_WARMUP_LEARN_MIN, "learn_maxlen": _TM_WARMUP_LEARN_MAXLEN,
            "from_stats_on": _TM_WARMUP_FROM_STATS,
            "static": cnt(static), "learned": cnt(learned), "from_stats": cnt(from_stats),
            "merged": cnt(merged), "sample_learned": {k: v[:10] for k, v in learned.items()}}


@app.get("/config/tm_stats")
def get_tm_stats(misses: int = 30):
    """跨会话翻译质量趋势：最近各场命中率/规模/预热战果 + 累计高频未命中短句(top)。
    供观测缓存/预热 ROI(命中率是否随场次上升)与调优(哪些高频句还在付 MT 时延→可加词表/开反哺)。"""
    data = _tm_stats_load()
    sessions = data.get("sessions", [])
    agg_hit = sum(int(s.get("hit", 0)) for s in sessions)
    agg_tot = sum(int(s.get("hit", 0)) + int(s.get("miss", 0)) for s in sessions)
    return {"ok": True, "on": _TM_STATS_ON, "warmup_from_stats": _TM_WARMUP_FROM_STATS,
            "session_count": len(sessions), "sessions": sessions,
            "lifetime_rate": round(agg_hit / agg_tot, 4) if agg_tot else 0.0,
            "recent_rate": (sessions[-1].get("rate") if sessions else None),
            "miss_total": len(data.get("misses", [])),
            "misses": (data.get("misses", []))[:max(0, misses)],
            "updated_at": data.get("updated_at")}


class TtsCfgReq(BaseModel):
    engine: str | None = None


@app.get("/config/tts")
def get_tts():
    """当前配音引擎 + 可选项，供 UI 选择器。fish(默认稳)/qwen3(低延迟流式，失败自动回退 Fish)。"""
    return {"ok": True, "engine": INTERP_TTS_ENGINE, "engines": ["fish", "qwen3", "cosyvoice"],
            "url": _tts_urls()[0]}


@app.post("/config/tts")
def set_tts(req: TtsCfgReq):
    """运行时切换同传配音引擎(下一句即生效)；任何请求失败仍自动回退 Fish。"""
    global INTERP_TTS_ENGINE
    if req.engine and req.engine.strip():
        INTERP_TTS_ENGINE = req.engine.strip().lower()
    logger.info(f"配音引擎切换 → {INTERP_TTS_ENGINE}({_tts_urls()[0]})")
    return {"ok": True, "engine": INTERP_TTS_ENGINE, "url": _tts_urls()[0]}


@app.get("/tts/engines_health")
def tts_engines_health():
    """配音引擎体检：当前语向候选链逐个引擎的健康 + 主引擎在线否 + 哪个可一键拉起。
    供桌面/手机「配音引擎」红绿灯与「拉起配音引擎」按钮渲染。"""
    engines = _tts_engines_detail()
    down_launchable = [e for e in engines if (not e["alive"]) and e["launchable"]]
    prim = next((e for e in engines if e["primary"]), None)
    ld = dict(_last_dub)
    ld["age_sec"] = (round(time.time() - ld["ts"], 1) if ld["ts"] else None)
    ld["never"] = (ld["ts"] == 0.0)
    # 实时通话延迟提醒：主引擎在线但最近合成明显偏慢(如远端 cosyvoice ~4s)→ 建议切 Fish(~1s)更跟手。
    # 阈值 2500ms：fish 热态约 0.7~1.1s、cosyvoice 远端热态约 4s,取中间。不擅自切,只给可执行建议。
    plat = ld.get("synth_ms") if isinstance(ld.get("synth_ms"), (int, float)) else None
    prim_alive = bool(prim and prim["alive"])
    slow = bool(prim_alive and plat and plat > 2500 and (prim or {}).get("engine") != "fish")
    advice = (f"主引擎「{prim['engine']}」最近合成 {plat}ms，对实时通话偏慢；想更跟手可切 Fish(约1秒，代价是少了情感/instruct)。"
              if slow else "")
    return {"ok": True, "engines": engines,
            "any_alive": any(e["alive"] for e in engines),
            "primary": (prim or {}).get("engine"),
            "primary_ok": prim_alive,
            "primary_latency_ms": plat, "slow_primary": slow, "advice": advice,
            "can_launch": [e["engine"] for e in down_launchable],
            "last_dub": ld, "hub": HUB_URL}


class EngRestartReq(BaseModel):
    engine: str = ""       # 缺省=当前主引擎；也可指定 fish/cosyvoice/qwen3/sbv2


@app.post("/tts/engine_restart")
def tts_engine_restart(req: EngRestartReq = None):
    """一键拉起配音引擎：把挂掉的引擎经主控台(Hub /api/engine/start)重新拉起来。
    缺省拉当前主引擎(如 cosyvoice)。拉起后清健康缓存并复探,返回是否已恢复。"""
    eng = ((req.engine if req else "") or _resolve_tts_engine() or INTERP_TTS_ENGINE).strip().lower()
    svc = _ENGINE_SVC.get(eng)
    if not svc:
        return {"ok": False, "engine": eng,
                "reason": f"引擎「{eng}」无独立服务登记，不能一键拉起(可在主控台手动启动)"}
    try:
        r = requests.post(f"{HUB_URL}/api/engine/start", params={"name": svc}, timeout=120)
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text[:160]}
    except Exception as e:
        return {"ok": False, "engine": eng, "svc": svc,
                "reason": f"调用主控台拉起失败({str(e)[:120]})：请确认主控台(9000)在线"}
    _tts_health_cache["ok"] = None                 # 作废缓存,强制复探
    time.sleep(1.5)                                 # 给端口起监听留点时间(模型仍可能在后台加载)
    h = _tts_engine_health(ttl=0.0)
    hub_ok = bool(j.get("ok"))
    hubmsg = j.get("reason") or (j if isinstance(j, dict) else str(j))
    # 如实反映：主引擎真恢复 / 进程已拉起但加载中 / 本机拉不起(常见=已迁远端或 GPU 释放)——都别谎称成功
    if h.get("primary_ok"):
        good, reason = True, f"✅ 主引擎「{eng}」已恢复在线：{h['detail']}"
    elif hub_ok:
        good, reason = True, f"⏳ 已拉起「{eng}」进程，模型加载中；十几秒后再点『试音』确认。{h['detail']}"
    else:
        good = False
        reason = (f"⚠ 主引擎「{eng}」本机没拉起（{hubmsg}）。"
                  + ("当前走兜底出声，对方仍能听到；要用回 " + eng + " 需在其所在机器启动、或把同传指向它。"
                     if h.get("ok") else "且暂无可用兜底引擎，对方将听不到配音——请检查 Fish/远端引擎。"))
    ST.push_event({"who": "sys", "warn": ("🔧 " if good else "⚠ ") + reason})
    return {"ok": good, "engine": eng, "svc": svc, "restarted": hub_ok,
            "engine_ok": h["ok"], "primary_ok": h.get("primary_ok"),
            "detail": h["detail"], "hub": j, "reason": reason}


class EmoTtsReq(BaseModel):
    on: bool = None
    llm_on: bool = None                # 聊天记录情绪基调(LLM)开关
    strong_set: str = None             # 改道情感集,逗号分隔(如 "excited,angry,sad,surprised,happy")


@app.get("/config/emotts")
def get_emotts():
    """情感配音路由状态：规则+聊天基调(LLM)+响度三层融合,强情感句改道 CosyVoice3(下一句即生效)。"""
    return {"ok": True, "on": EMO_TTS_ON, "llm_on": EMO_LLM_ON,
            "available": _emo_detect_text is not None,
            "strong_set": sorted(_EMO_STRONG), "mood": _emo_mood() or "neutral",
            "mood_ttl_s": EMO_MOOD_TTL, "engine_url": _tts_url_for("cosyvoice"),
            "stats": dict(_emo_stats)}


@app.post("/config/emotts")
def set_emotts(req: EmoTtsReq):
    global EMO_TTS_ON, EMO_LLM_ON, _EMO_STRONG
    if req.on is not None:
        EMO_TTS_ON = bool(req.on)
        logger.info(f"情感配音路由 → {'开' if EMO_TTS_ON else '关'}")
        ST.push_event({"who": "sys", "warn": f"🎭 情感配音已{'开启(强情感句用 CosyVoice3)' if EMO_TTS_ON else '关闭'}"})
    if req.llm_on is not None:
        EMO_LLM_ON = bool(req.llm_on)
    if req.strong_set:
        s = set(x.strip().lower() for x in req.strong_set.replace("，", ",").split(",") if x.strip())
        if s & _EMO_LABELS:
            _EMO_STRONG = s & _EMO_LABELS
    return get_emotts()


@app.get("/events")
def events(since: int = 0):
    def gen():
        last = since
        idle = 0
        while True:
            new = [e for e in list(ST.events) if e["id"] > last]
            if new:
                for e in new:
                    last = e["id"]
                    yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
                idle = 0
            else:
                idle += 1
                if idle % 30 == 0:
                    yield ": ping\n\n"
            time.sleep(0.1)
    return StreamingResponse(gen(), media_type="text/event-stream")


# ── 独立字幕图层(OBS Browser Source / 浏览器窗) ──────────────────────────────
# 与 _PAGE 解耦：透明背景、可作 OBS「浏览器」源直接叠加在换脸/数字人/任意画面上；
# 同一页面内含样式面板(?panel=1 编辑、?panel=0 给 OBS)，设置存 localStorage。
# 消费既有 SSE /events；前向兼容未来多语种字段(src/dst)，当前回退 legacy zh/en。
_OVERLAY_HTML = r"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<link rel=icon href=/favicon.ico>
<title>字幕图层 · LingoX Overlay</title>
<style>
:root{
  --f1:34px; --f2:20px; --c1:#ffffff; --c2:#9fe3c9; --band:rgba(8,11,16,.45);
  --radius:14px; --maxw:84vw; --w1:800; --w2:600; --safe-x:3vw; --safe-y:5vh;
  --lh2:1.3; --max2:2;
  --font:"Microsoft YaHei UI","Microsoft YaHei","PingFang SC","Noto Sans CJK SC","Segoe UI",sans-serif;
}
html,body{margin:0;height:100%;background:transparent;overflow:hidden}
body.chroma{background:#00ff00}
#wrap{position:fixed;inset:0;display:flex;justify-content:center;pointer-events:none;padding:var(--safe-y) var(--safe-x)}
#wrap.pos-bottom{align-items:flex-end}
#wrap.pos-top{align-items:flex-start}
#wrap.pos-center{align-items:center}
#sub{max-width:var(--maxw);text-align:center;transition:opacity .5s ease}
#sub.hide{opacity:0}
/* 字幕安全区参考框(仅设置期显示)：虚线框=字幕可现的边界，助对齐平台UI(抖音右侧按钮/底部条、TV 边缘) */
#guide{position:fixed;inset:var(--safe-y) var(--safe-x);border:2px dashed rgba(120,190,255,.75);
  border-radius:8px;pointer-events:none;z-index:8;display:none}
#guide.on{display:block}
#guide::after{content:"字幕安全区";position:absolute;top:5px;left:9px;font:12px var(--font);color:rgba(120,190,255,.95)}
@keyframes subIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
#sub.anim .line{animation:subIn .3s cubic-bezier(.2,.7,.3,1)}
#sub.fx-news .line{display:block;border-radius:6px;padding-left:26px;padding-right:26px}
.line{display:inline-block;background:var(--band);border-radius:var(--radius);
  padding:12px 20px;margin:5px 0;line-height:1.3;font-family:var(--font);
  word-break:break-word;white-space:pre-wrap}
.line1{font-size:var(--f1);font-weight:var(--w1);color:var(--c1)}
.line2{font-size:var(--f2);font-weight:var(--w2);color:var(--c2);line-height:var(--lh2)}
/* 副行超出最大行数则省略号截断(仅 max2>0 时挂 clamp2；-webkit-box 是 line-clamp 必需，OBS/CEF 支持) */
#sub.clamp2 .line2{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:var(--max2);overflow:hidden}
.pend{opacity:.72;font-style:italic}
#panel{position:fixed;top:12px;right:12px;width:284px;max-height:94vh;overflow:auto;
  background:rgba(14,18,26,.97);border:1px solid rgba(255,255,255,.14);border-radius:14px;
  padding:14px;color:#e5e9f5;font:13px/1.5 var(--font);pointer-events:auto;z-index:9}
#panel h3{margin:0 0 10px;font-size:14px;display:flex;justify-content:space-between;align-items:center}
#panel .row{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:7px 0}
#panel label{color:#aab4cc;font-size:12px}
#panel input[type=range]{width:132px}
#panel input[type=color]{width:42px;height:26px;border:none;background:none;cursor:pointer}
#panel select{background:#1a2030;color:#e5e9f5;border:1px solid rgba(255,255,255,.18);
  border-radius:8px;padding:5px 8px;font-size:12px}
#panel select option{background:#1a2030;color:#e5e9f5}
#panel .btns{display:flex;gap:8px;margin-top:10px}
#panel button{flex:1;background:linear-gradient(135deg,#4f7aff,#a855f7);color:#fff;border:none;
  border-radius:9px;padding:8px;font-weight:700;font-size:12px;cursor:pointer}
#panel button.sec{background:#232a3a}
#panel .plats{display:flex;gap:6px;flex:1;margin-left:8px}
#panel button.plat{padding:7px 3px;font-size:11px}
#panel .hint{color:#8b96b0;font-size:11px;margin-top:8px;line-height:1.45}
#panel .hint b{color:#cbd5e1;word-break:break-all}
.ok{color:#34d399}.hidden{display:none!important}
</style></head><body>
<div id="wrap"><div id="sub"><div id="p1" class="line line1"></div><div id="p2" class="line line2"></div></div></div>
<div id="guide"></div>
<div id="panel" class="hidden">
  <h3>字幕图层设置 <span id="conn" style="font-size:11px;color:#8b96b0">○</span></h3>
  <div class="row"><label>主题</label>
    <select id="theme"><option value="classic">经典</option><option value="neon">霓虹</option><option value="minimal">极简</option><option value="news">新闻条</option><option value="big">大字幕</option></select></div>
  <div class="row"><label>显示对象</label>
    <select id="who"><option value="auto">自动·当前说话人</option><option value="me">我(译给对方)</option><option value="other">对方(译给我)</option></select></div>
  <div class="row"><label>主行内容</label>
    <select id="big"><option value="trans">译文</option><option value="source">原文</option></select></div>
  <div class="row"><label>双语(显示副行)</label><input type="checkbox" id="bi"></div>
  <div class="row"><label>位置</label>
    <select id="pos"><option value="bottom">底部</option><option value="top">顶部</option><option value="center">居中</option></select></div>
  <div class="row"><label>对齐</label>
    <select id="align"><option value="center">居中</option><option value="left">左对齐</option><option value="right">右对齐</option></select></div>
  <div class="row"><label>平台预设</label>
    <span class="plats"><button class="sec plat" data-plat="douyin">抖音竖屏</button><button class="sec plat" data-plat="wide">横屏16:9</button><button class="sec plat" data-plat="tv">电视框</button></span></div>
  <div class="hint" style="margin-top:2px">平台预设一键套安全区(边距/宽度/位置)；配色·字号请用「主题」。</div>
  <div class="row"><label>左右安全边距</label><input type="range" id="safex" min="0" max="20" step="1"></div>
  <div class="row"><label>上下安全边距</label><input type="range" id="safey" min="0" max="20" step="1"></div>
  <div class="row"><label>最大宽度</label><input type="range" id="maxw" min="40" max="100" step="2"></div>
  <div class="row"><label>安全区参考框</label><input type="checkbox" id="guideck"></div>
  <div class="row"><label>主行字号</label><input type="range" id="f1" min="16" max="80" step="1"></div>
  <div class="row"><label>副行字号</label><input type="range" id="f2" min="12" max="56" step="1"></div>
  <div class="row"><label>主行字重</label><input type="range" id="w1" min="300" max="900" step="100"></div>
  <div class="row"><label>副行字重</label><input type="range" id="w2" min="300" max="900" step="100"></div>
  <div class="row"><label>副行行高</label><input type="range" id="lh2" min="1" max="2.2" step="0.05"></div>
  <div class="row"><label>副行最大行数</label><select id="max2"><option value="0">不限</option><option value="1">1 行</option><option value="2">2 行</option><option value="3">3 行</option><option value="4">4 行</option></select></div>
  <div class="row"><label>背景不透明度</label><input type="range" id="bg" min="0" max="100" step="5"></div>
  <div class="row"><label>描边粗细</label><input type="range" id="sw" min="0" max="6" step="1"></div>
  <div class="row"><label>主行颜色</label><input type="color" id="col1"></div>
  <div class="row"><label>副行颜色</label><input type="color" id="col2"></div>
  <div class="row"><label>停留(秒)</label><input type="range" id="hold" min="2" max="30" step="1"></div>
  <div class="row"><label>入场动画</label><input type="checkbox" id="anim"></div>
  <div class="row"><label>绿幕背景·便于抠像</label><input type="checkbox" id="chroma"></div>
  <div class="btns"><button id="copy">复制 OBS 地址</button><button id="reset" class="sec">重置</button></div>
  <div class="hint">OBS：添加「浏览器」源，点「复制 OBS 地址」粘贴即可（已含当前全部样式，跨端重现）：<b id="obsurl"></b>。快捷键：<b>H</b> 隐藏面板 · <b>1/2/3</b> 切 抖音/横屏/电视。</div>
</div>
<script>
(function(){
 var $=function(s){return document.querySelector(s)};
 var DEF={theme:'classic',who:'auto',big:'trans',bi:1,pos:'bottom',align:'center',f1:34,f2:20,w1:800,w2:600,lh2:1.3,max2:0,bg:45,sw:2,col1:'#ffffff',col2:'#9fe3c9',hold:8,anim:1,chroma:0,safex:3,safey:5,maxw:84,guide:0,panel:1};
 var NUM={f1:1,f2:1,w1:1,w2:1,lh2:1,max2:1,bg:1,sw:1,hold:1,safex:1,safey:1,maxw:1}, CHK={bi:1,anim:1,chroma:1,guide:1,panel:1}, KEY='lingox_overlay_v1';
 var THEMES={ classic:{f1:34,f2:20,w1:800,w2:600,bg:45,sw:2,col1:'#ffffff',col2:'#9fe3c9',fx:'none'},
   neon:{f1:36,f2:21,w1:800,w2:600,bg:18,sw:0,col1:'#eafff8',col2:'#5ef2c0',fx:'neon'},
   minimal:{f1:32,f2:19,w1:600,w2:400,bg:0,sw:3,col1:'#ffffff',col2:'#dbe7ff',fx:'none'},
   news:{f1:30,f2:18,w1:800,w2:700,bg:82,sw:0,col1:'#ffffff',col2:'#ffd479',fx:'news'},
   big:{f1:52,f2:28,w1:900,w2:700,bg:55,sw:3,col1:'#ffffff',col2:'#ffe08a',fx:'none'} };
 function loadCfg(){
   var c={}; try{c=JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){c={}}
   var q=new URLSearchParams(location.search), out={};
   for(var k in DEF){
     if(q.has(k)){ var v=q.get(k); out[k]=CHK[k]?((v=='1'||v=='true')?1:0):(NUM[k]?parseFloat(v):v); }
     else out[k]=(k in c)?c[k]:DEF[k];
   }
   return out;
 }
 var cfg=loadCfg();
 function saveCfg(){ try{localStorage.setItem(KEY,JSON.stringify(cfg))}catch(e){} }
 function obsUrl(){   // 自包含 OBS 地址：把当前全部样式塞进 query(query 优先于 localStorage)，OBS 独立浏览上下文也能原样重现；强制 panel/guide=0
   var k=JSON.parse(JSON.stringify(cfg)); k.panel=0; k.guide=0;
   return location.origin+'/overlay?'+Object.keys(k).map(function(n){return n+'='+encodeURIComponent(k[n])}).join('&');
 }
 function outline(px){ if(px<=0) return 'none'; var o=[]; for(var dx=-px;dx<=px;dx++)for(var dy=-px;dy<=px;dy++){ if(dx||dy)o.push(dx+'px '+dy+'px 0 #000'); } return o.join(','); }
 function apply(){
   var r=document.documentElement.style;
   r.setProperty('--f1',cfg.f1+'px'); r.setProperty('--f2',cfg.f2+'px');
   r.setProperty('--c1',cfg.col1); r.setProperty('--c2',cfg.col2);
   r.setProperty('--w1',cfg.w1); r.setProperty('--w2',cfg.w2);
   r.setProperty('--safe-x',cfg.safex+'vw'); r.setProperty('--safe-y',cfg.safey+'vh'); r.setProperty('--maxw',cfg.maxw+'vw');
   r.setProperty('--lh2',cfg.lh2); var _mc=(+cfg.max2>0); if(_mc)r.setProperty('--max2',cfg.max2); $('#sub').classList.toggle('clamp2',_mc);
   r.setProperty('--band','rgba(8,11,16,'+(cfg.bg/100).toFixed(2)+')');
   var fx=(THEMES[cfg.theme]||{}).fx||'none';
   $('#sub').classList.toggle('fx-news',fx=='news');
   if(fx=='neon'){ $('#p1').style.textShadow='0 0 6px '+cfg.col1+',0 0 15px '+cfg.col1;
     $('#p2').style.textShadow='0 0 6px '+cfg.col2+',0 0 13px '+cfg.col2; }
   else { var sh=outline(cfg.sw); $('#p1').style.textShadow=sh; $('#p2').style.textShadow=sh; }
   $('#sub').style.textAlign=cfg.align;
   $('#wrap').className='pos-'+cfg.pos;
   document.body.classList.toggle('chroma',!!cfg.chroma);
   $('#panel').classList.toggle('hidden',!cfg.panel);
   $('#theme').value=cfg.theme; $('#align').value=cfg.align; $('#anim').checked=!!cfg.anim;
   $('#who').value=cfg.who; $('#big').value=cfg.big; $('#bi').checked=!!cfg.bi;
   $('#pos').value=cfg.pos; $('#f1').value=cfg.f1; $('#f2').value=cfg.f2; $('#bg').value=cfg.bg;
   $('#sw').value=cfg.sw; $('#col1').value=cfg.col1; $('#col2').value=cfg.col2; $('#hold').value=cfg.hold;
   $('#chroma').checked=!!cfg.chroma;
   $('#w1').value=cfg.w1; $('#w2').value=cfg.w2; $('#safex').value=cfg.safex; $('#safey').value=cfg.safey;
   $('#maxw').value=cfg.maxw; $('#guideck').checked=!!cfg.guide; $('#lh2').value=cfg.lh2; $('#max2').value=cfg.max2;
   $('#guide').classList.toggle('on', !!cfg.guide && !!cfg.panel);   // 参考框仅设置期(面板可见)显示，OBS(panel=0)自动隐藏
   $('#obsurl').textContent=obsUrl();
 }
 var sides={me:blank(),other:blank()};
 function blank(){return {turn:null,order:[],seg:{},finS:null,finT:null,pending:false,liveText:null,ts:0}}
 function pick(ev){
   return {src:(ev.src!=null)?ev.src:(ev.who=='me'?ev.zh:ev.en),
           trans:(ev.dst!=null)?ev.dst:(ev.who=='me'?ev.en:ev.zh)};
 }
 function onEvent(ev){
   if(ev.who=='sys'){ if(ev.clear){sides.me=blank();sides.other=blank();render();} return; }
   if(ev.who!='me'&&ev.who!='other') return;
   var S=sides[ev.who];
   if(ev.finalize){ var pf=pick(ev); if(pf.src!=null)S.finS=pf.src; if(pf.trans!=null)S.finT=pf.trans; S.pending=false; S.ts=Date.now(); render(); return; }
  if(ev.turn==null||ev.uid==null) return;
  if(ev.retract){ if(S.turn==ev.turn){S.order=S.order.filter(function(u){return u!=ev.uid}); delete S.seg[ev.uid]; S.pending=false; S.liveText=null;} S.ts=Date.now(); render(); return; }
  if(ev.ger&&S.turn!==ev.turn) return; /* 迟到的GER纠错(上一句)不打断当前句 */
  if(S.turn!==ev.turn){ S=blank(); S.turn=ev.turn; sides[ev.who]=S; }
   if(ev.live!=null){ S.pending=true; S.liveText=ev.live; S.ts=Date.now(); render(); return; }
   S.pending=false; S.finS=null; S.finT=null;
   if(!(ev.uid in S.seg)){ S.order.push(ev.uid); S.seg[ev.uid]={src:'',trans:''}; }
   var p=pick(ev); if(p.src)S.seg[ev.uid].src=p.src; if(p.trans)S.seg[ev.uid].trans=p.trans;
   S.ts=Date.now(); render();
 }
 function textOf(S){
   if(S.pending&&S.liveText!=null) return {src:S.liveText,trans:'',pending:true};
   if(S.finS!=null||S.finT!=null) return {src:S.finS||'',trans:S.finT||'',pending:false};
   return {src:S.order.map(function(u){return S.seg[u].src}).filter(Boolean).join(' '),
           trans:S.order.map(function(u){return S.seg[u].trans}).filter(Boolean).join(' '),pending:false};
 }
 var lastShownTs=0, _wasShown=false;
 function _pop(){ var s=$('#sub'); s.classList.remove('anim'); void s.offsetWidth; s.classList.add('anim'); }
 function render(){
   var chosen = (cfg.who=='auto') ? ((sides.me.ts>=sides.other.ts)?sides.me:sides.other) : sides[cfg.who];
   var t=textOf(chosen);
   var primary = (cfg.big=='source') ? t.src : (t.trans||t.src);
   var secondary = (cfg.big=='source') ? t.trans : t.src;
   if(cfg.big=='trans' && !t.trans) secondary='';
   $('#p1').textContent=primary||''; $('#p1').classList.toggle('pend',!!t.pending);
   $('#p1').style.display=primary?'inline-block':'none';
   $('#p2').textContent=(cfg.bi?secondary:'')||'';
   $('#p2').style.display=(cfg.bi&&secondary)?'inline-block':'none';
   if(primary||secondary){ if(cfg.anim&&!_wasShown)_pop(); lastShownTs=Date.now(); $('#sub').classList.remove('hide'); _wasShown=true; }
 }
 setInterval(function(){ if(lastShownTs&&Date.now()-lastShownTs>cfg.hold*1000){ $('#sub').classList.add('hide'); _wasShown=false; } },500);
 var lastId=0, es=null;
 function connect(){
   try{ es=new EventSource('/events?since='+lastId); }catch(e){ setTimeout(connect,1500); return; }
   es.onopen=function(){ $('#conn').textContent='● 已连接'; $('#conn').className='ok'; };
   es.onmessage=function(e){ try{var ev=JSON.parse(e.data); if(ev.id)lastId=ev.id; onEvent(ev);}catch(x){} };
   es.onerror=function(){ $('#conn').textContent='○ 重连中'; $('#conn').className=''; try{es.close()}catch(x){} setTimeout(connect,1500); };
 }
 function bind(id,key,isNum,isChk){ var el=$(id); if(!el)return; el.addEventListener('input',function(){ cfg[key]=isChk?(el.checked?1:0):(isNum?parseFloat(el.value):el.value); saveCfg(); apply(); render(); }); }
 bind('#who','who'); bind('#big','big'); bind('#bi','bi',false,true); bind('#pos','pos');
 bind('#f1','f1',true); bind('#f2','f2',true); bind('#bg','bg',true); bind('#sw','sw',true);
 bind('#col1','col1'); bind('#col2','col2'); bind('#hold','hold',true); bind('#chroma','chroma',false,true);
 bind('#align','align'); bind('#anim','anim',false,true);
 bind('#w1','w1',true); bind('#w2','w2',true);
 bind('#safex','safex',true); bind('#safey','safey',true); bind('#maxw','maxw',true); bind('#guideck','guide',false,true);
 bind('#lh2','lh2',true); bind('#max2','max2',true);
 $('#theme').addEventListener('change',function(){ var t=THEMES[this.value]; cfg.theme=this.value;
   if(t){ ['f1','f2','w1','w2','bg','sw','col1','col2'].forEach(function(k){ if(t[k]!=null)cfg[k]=t[k]; }); }
   saveCfg(); apply(); render(); });
 // 平台安全区预设(只调布局：边距/宽度/位置，与主题正交)。抖音竖屏=抬高避开底部条+收窄避右侧按钮；电视=title-safe 四边 10%。
 var PLATS={ douyin:{safex:6,safey:18,maxw:74,pos:'bottom'}, wide:{safex:4,safey:8,maxw:86,pos:'bottom'}, tv:{safex:10,safey:10,maxw:80,pos:'bottom'} };
 Array.prototype.forEach.call(document.querySelectorAll('.plat'),function(b){
   b.addEventListener('click',function(){ var p=PLATS[b.getAttribute('data-plat')]; if(!p)return;
     for(var k in p)cfg[k]=p[k]; saveCfg(); apply(); render(); }); });
 $('#copy').addEventListener('click',function(){ var u=obsUrl(); if(navigator.clipboard)navigator.clipboard.writeText(u); $('#copy').textContent='已复制!'; setTimeout(function(){$('#copy').textContent='复制 OBS 地址'},1500); });
 $('#reset').addEventListener('click',function(){ localStorage.removeItem(KEY); cfg=JSON.parse(JSON.stringify(DEF)); saveCfg(); apply(); render(); });
 document.addEventListener('keydown',function(e){
   if(e.target&&/^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName))return;      // 别抢输入焦点
   if(e.key=='h'||e.key=='H'){ cfg.panel=cfg.panel?0:1; saveCfg(); apply(); return; }
   var pk={'1':'douyin','2':'wide','3':'tv'}[e.key];                            // 数字键快切平台预设(直播不开面板也能改)
   if(pk&&PLATS[pk]){ var p=PLATS[pk]; for(var k in p)cfg[k]=p[k]; saveCfg(); apply(); render(); } });
 apply(); render(); connect();
})();
</script></body></html>"""


@app.get("/overlay", response_class=HTMLResponse)
def overlay():
    """独立字幕图层：OBS「浏览器」源直接叠加(透明底)，或浏览器窗预览。含样式面板。"""
    return _OVERLAY_HTML


@app.get("/subtitle_overlay", response_class=HTMLResponse)
def subtitle_overlay():
    """Phase B-2 别名：与 /overlay 同源，供 Hub/OBS 稳定路径。"""
    return _OVERLAY_HTML


_GLOSSARY_PAGE = r"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=theme-color content="#080b10">
<link rel=icon href=/favicon.ico>
<title>术语锁定表 · 编辑</title>
<style>
  :root{color-scheme:dark;}
  body{margin:0;background:#0d1117;color:#e6edf3;font:14px/1.6 system-ui,"Microsoft YaHei",sans-serif;padding:0 24px 40px;}
  h1{font-size:20px;margin:18px 0 4px;}
  .muted{color:#8b949e;font-size:13px;}
  a{color:#4f7aff;text-decoration:none;} a:hover{text-decoration:underline;}
  code{background:#21262d;padding:1px 6px;border-radius:5px;font-size:12px;}
  .dir{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px 16px;margin:14px 0;}
  .dir h2{font-size:15px;margin:0 0 10px;display:flex;align-items:center;gap:8px;}
  .dir h2 .tag{font:12px ui-monospace,monospace;background:#21262d;padding:2px 8px;border-radius:6px;color:#a855f7;}
  .dir h2 .rm{margin-left:auto;background:transparent;border:none;color:#8b949e;cursor:pointer;font-size:12px;}
  .dir h2 .rm:hover{color:#f85149;}
  table{width:100%;border-collapse:collapse;}
  th,td{text-align:left;padding:4px 6px;vertical-align:middle;}
  th{color:#8b949e;font-weight:500;font-size:12px;}
  input[type=text]{width:100%;box-sizing:border-box;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:6px 8px;font-size:14px;}
  input[type=text]:focus{border-color:#4f7aff;outline:none;}
  button{background:#21262d;border:1px solid #30363d;color:#e6edf3;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:13px;}
  button:hover{border-color:#4f7aff;}
  .del{background:transparent;border:none;color:#f85149;padding:2px 8px;font-size:18px;line-height:1;}
  .add{margin-top:8px;color:#4f7aff;border-style:dashed;}
  .bar{position:sticky;top:0;background:#0d1117;padding:14px 0 12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;z-index:5;border-bottom:1px solid #21262d;}
  .bar .csv{margin-left:auto;display:flex;gap:8px;align-items:center;}
  .bar select{background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:6px 8px;font-size:13px;}
  .bar select option{background:#161b22;color:#e6edf3;}
  .save{background:linear-gradient(135deg,#4f7aff,#a855f7);border:none;font-weight:600;padding:8px 20px;}
  #status{font-size:13px;margin-left:6px;}
</style></head><body>
<div class=bar>
  <button class="save" id=save>保存并生效</button>
  <button id=reload>放弃改动·重载</button>
  <span id=status class=muted></span>
  <span class=csv>
    <button id=expcsv title="导出全部术语为 CSV(Excel/表格可开，列 dir,src,dst)">⬇ 导出 CSV</button>
    <select id=impmode title="导入方式：合并=并入现有(同语向同源覆盖)；替换=整表覆盖真实语向"><option value=merge>合并导入</option><option value=replace>整表替换</option></select>
    <button id=impbtn title="从 CSV 批量导入术语(在 Excel 编辑后回传)">⬆ 导入 CSV</button>
    <input type=file id=impfile accept=".csv,text/csv" style="display:none">
  </span>
</div>
<h1>术语锁定表 · 编辑</h1>
<p class=muted>强制品牌 / 人名 / 专有名词 / 行话译成指定写法，消除音译不一致。按语向分组(源→目标)；<code>*</code> 对任意语向生效(适合任何语言都保持原样的品牌名)。保存即原子落盘 + 热重载，无需重启；空行自动忽略。批量维护可 <b>导出 CSV</b>(列 <code>dir,src,dst</code>)在 Excel 编辑后 <b>导入 CSV</b>(合并/替换)。</p>
<div id=dirs></div>
<div style="margin-top:12px;">
  <input type=text id=newdir placeholder="新增语向，如 ja-&gt;en 或 *" style="width:240px;display:inline-block;">
  <button id=adddir>+ 添加语向</button>
</div>
<p class=muted style="margin-top:22px;">返回 <a href="/">同传主页</a> · 亦可直接编辑 <code>data/glossary.json</code></p>
<script>
var state={};
function el(id){return document.getElementById(id);}
function setStatus(msg,ok){var s=el('status');s.textContent=msg;s.style.color=(ok===false)?'#f85149':((ok===true)?'#3fb950':'#8b949e');}
function render(){
  var box=el('dirs'); box.innerHTML='';
  var dirs=Object.keys(state);
  if(!dirs.length){ box.innerHTML='<p class=muted>暂无语向，点下方“添加语向”开始。</p>'; return; }
  dirs.forEach(function(dir){
    var rows=state[dir]||[];
    var wrap=document.createElement('div'); wrap.className='dir';
    var label=(dir==='*')?'任意语向（品牌名保持）':dir.replace('->',' → ');
    var h=document.createElement('h2');
    var sp1=document.createElement('span'); sp1.textContent=label;
    var sp2=document.createElement('span'); sp2.className='tag'; sp2.textContent=dir;
    var rm=document.createElement('button'); rm.className='rm'; rm.textContent='移除此语向';
    rm.onclick=function(){ delete state[dir]; render(); };
    h.appendChild(sp1); h.appendChild(sp2); h.appendChild(rm); wrap.appendChild(h);
    var t=document.createElement('table');
    var hd=document.createElement('tr');
    hd.innerHTML='<th style="width:45%">源写法（待翻译文本中出现）</th><th style="width:45%">目标写法（强制译为）</th><th></th>';
    t.appendChild(hd);
    rows.forEach(function(r,i){
      var tr=document.createElement('tr');
      var td1=document.createElement('td'); var s=document.createElement('input'); s.type='text'; s.value=(r.src==null?'':r.src);
      s.oninput=function(){ state[dir][i].src=s.value; }; td1.appendChild(s);
      var td2=document.createElement('td'); var d=document.createElement('input'); d.type='text'; d.value=(r.dst==null?'':r.dst);
      d.oninput=function(){ state[dir][i].dst=d.value; }; td2.appendChild(d);
      var td3=document.createElement('td'); var x=document.createElement('button'); x.className='del'; x.textContent='\u00d7'; x.title='删除此行';
      x.onclick=function(){ state[dir].splice(i,1); render(); }; td3.appendChild(x);
      tr.appendChild(td1); tr.appendChild(td2); tr.appendChild(td3); t.appendChild(tr);
    });
    wrap.appendChild(t);
    var add=document.createElement('button'); add.className='add'; add.textContent='+ 添加术语';
    add.onclick=function(){ state[dir].push({src:'',dst:''}); render(); };
    wrap.appendChild(add);
    box.appendChild(wrap);
  });
}
function load(){
  setStatus('加载中…');
  Promise.all([
    fetch('/config/glossary').then(function(r){return r.json();}).catch(function(){return {};}),
    fetch('/config/langs').then(function(r){return r.json();}).catch(function(){return {};})
  ]).then(function(res){
    var g=res[0]||{}, lang=res[1]||{};
    state=(g.glossary && typeof g.glossary==='object')?g.glossary:{};
    var src=lang.src||'zh', dst=lang.dst||'en';
    [src+'->'+dst, dst+'->'+src, '*'].forEach(function(k){ if(!state[k]) state[k]=[]; });
    render();
    setStatus('已加载 '+(g.count||0)+' 条术语'+((g.on===false)?'（术语锁定当前为关闭：INTERP_GLOSSARY=0）':''), true);
  });
}
function save(){
  var payload={};
  Object.keys(state).forEach(function(dir){
    payload[dir]=(state[dir]||[]).filter(function(r){return (r.src||'').trim() && (r.dst||'').trim();});
  });
  setStatus('保存中…');
  fetch('/config/glossary',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({glossary:payload})})
    .then(function(r){ return r.json().then(function(j){ return {ok:r.ok,j:j}; }); })
    .then(function(res){
      if(res.ok && res.j && res.j.ok){ setStatus('已保存并生效 · 共 '+res.j.count+' 条术语', true); load(); }
      else { setStatus('保存失败：'+((res.j&&(res.j.detail||res.j.error))||'未知错误'), false); }
    }).catch(function(e){ setStatus('保存失败：'+e, false); });
}
el('save').onclick=save;
el('reload').onclick=load;
el('adddir').onclick=function(){
  var k=(el('newdir').value||'').trim().toLowerCase();
  if(!k){ return; }
  if(k!=='*' && k.indexOf('->')<0){ setStatus('语向格式应为 源->目标（如 ja->en），或 *', false); return; }
  if(!state[k]){ state[k]=[]; }
  state[k].push({src:'',dst:''});
  el('newdir').value=''; render();
};
// CSV 批量导出/导入(复用服务端 /config/glossary.csv 与 /config/glossary/import_csv)
el('expcsv').onclick=function(){ window.open('/config/glossary.csv?download=1','_blank'); };
el('impbtn').onclick=function(){ el('impfile').click(); };
el('impfile').onchange=function(){
  var fs=el('impfile').files, f=fs&&fs[0]; if(!f){ return; }
  var mode=el('impmode').value;
  if(mode==='replace' && !confirm('整表替换：将用该 CSV 覆盖所有语向的现有术语，确定？')){ el('impfile').value=''; return; }
  var rd=new FileReader();
  rd.onload=function(){
    setStatus('导入中…');
    fetch('/config/glossary/import_csv',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csv:String(rd.result||''),mode:mode})})
      .then(function(r){ return r.json().then(function(j){ return {ok:r.ok,j:j}; }); })
      .then(function(res){
        if(res.ok && res.j && res.j.ok){ setStatus('导入成功（'+res.j.mode+'）：解析 '+res.j.parsed+' 条 / 跳过 '+res.j.skipped+' → 现 '+res.j.count+' 条', true); load(); }
        else { setStatus('导入失败：'+((res.j&&(res.j.detail||res.j.error))||'未知错误'), false); }
      }).catch(function(e){ setStatus('导入失败：'+e, false); });
  };
  rd.onerror=function(){ setStatus('读取文件失败', false); };
  rd.readAsText(f,'utf-8'); el('impfile').value='';
};
load();
</script></body></html>"""


@app.get("/glossary", response_class=HTMLResponse)
def glossary_page():
    """术语锁定表可视化编辑器(自包含单页)：增删条目→保存即原子落盘+热重载，运营免手改 JSON。"""
    return _GLOSSARY_PAGE


_SESSIONS_PAGE = r"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=theme-color content="#080b10">
<link rel=icon href=/favicon.ico>
<title>会话转写 · 导出</title>
<style>
  :root{color-scheme:dark;}
  body{margin:0;background:#0d1117;color:#e6edf3;font:14px/1.6 system-ui,"Microsoft YaHei",sans-serif;padding:0 24px 40px;}
  h1{font-size:20px;margin:18px 0 4px;} h2{font-size:15px;margin:20px 0 4px;}
  .muted{color:#8b949e;font-size:13px;}
  a{color:#4f7aff;text-decoration:none;} a:hover{text-decoration:underline;}
  code{background:#21262d;padding:1px 6px;border-radius:5px;font-size:12px;}
  .bar{position:sticky;top:0;background:#0d1117;padding:14px 0 12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;z-index:5;border-bottom:1px solid #21262d;}
  .bar select{background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:6px 8px;font-size:13px;}
  .bar select option{background:#161b22;color:#e6edf3;}
  .bar label{color:#8b949e;font-size:12px;margin-left:6px;}
  button{background:#21262d;border:1px solid #30363d;color:#e6edf3;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:13px;}
  button:hover{border-color:#4f7aff;}
  #status{font-size:13px;margin-left:auto;}
  .card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px 16px;margin:12px 0;display:flex;align-items:center;gap:14px;flex-wrap:wrap;}
  .card.live{border-color:rgba(63,185,80,.45);}
  .when{font-weight:600;font-size:15px;min-width:180px;}
  .meta{color:#8b949e;font-size:12px;}
  .tag{font:12px ui-monospace,monospace;background:#21262d;padding:2px 8px;border-radius:6px;color:#a855f7;}
  .dl{margin-left:auto;display:flex;gap:8px;}
  .dl a{background:#21262d;border:1px solid #30363d;border-radius:7px;padding:6px 12px;color:#e6edf3;font-size:13px;}
  .dl a:hover{border-color:#4f7aff;text-decoration:none;}
  .dl a.srt{color:#ffd479;}
  .empty{color:#8b949e;padding:20px 0;}
</style></head><body>
<div class=bar>
  <b>导出格式</b>
  <label>内容</label>
  <select id=content><option value=both>双语(原文+译文)</option><option value=src>仅原文</option><option value=trans>仅译文</option></select>
  <label>说话人</label>
  <select id=who><option value=all>全部</option><option value=me>我</option><option value=other>对方</option></select>
  <button id=refresh>刷新</button>
  <span id=status class=muted></span>
</div>
<h1>会话转写 · 导出</h1>
<p class=muted>每次同传的原文+译文按定稿时间留存。<b>SRT</b> 可直接给录像/剪辑配字幕；<b>TXT</b> 含时间戳适合会议纪要；<b>JSON</b> 为原始数据。上方选内容/说话人后，下方链接即时生效。往期会话结束后自动出现在此(留存于 <code>logs/interp_transcript_*.json</code>)。</p>
<div id=live></div>
<h2>往期会话</h2>
<div id=list></div>
<p class=muted style="margin-top:22px;">返回 <a href="/">同传主页</a></p>
<script>
function el(id){return document.getElementById(id);}
function qs(session){
  var p='content='+encodeURIComponent(el('content').value)+'&who='+encodeURIComponent(el('who').value)+'&download=1';
  if(session){ p+='&session='+encodeURIComponent(session); }
  return p;
}
function fmtStamp(s){
  var m=/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/.exec(s||'');
  return m? (m[1]+'-'+m[2]+'-'+m[3]+' '+m[4]+':'+m[5]+':'+m[6]) : (s||'');
}
function dlLinks(session){
  var wrap=document.createElement('div'); wrap.className='dl';
  [['txt','TXT'],['srt','SRT'],['json','JSON']].forEach(function(k){
    var a=document.createElement('a'); if(k[0]==='srt'){ a.className='srt'; }
    a.textContent=k[1]; a.href='/transcript.'+k[0]+'?'+qs(session);
    a.setAttribute('target','_blank'); wrap.appendChild(a);
  });
  return wrap;
}
function card(o){
  var c=document.createElement('div'); c.className='card'+(o.live?' live':'');
  var w=document.createElement('div'); w.className='when'; w.textContent=o.live?'当前 / 最近会话':fmtStamp(o.session);
  var meta=document.createElement('div'); meta.className='meta';
  var tag=document.createElement('span'); tag.className='tag'; tag.textContent=(o.src||'?')+' \u21c4 '+(o.dst||'?');
  meta.appendChild(tag);
  meta.appendChild(document.createTextNode(' \u00b7 '+((o.count==null)?'\u2014':o.count)+' 条'));
  c.appendChild(w); c.appendChild(meta); c.appendChild(dlLinks(o.live?'':o.session));
  return c;
}
function render(list, live){
  var lv=el('live'); lv.innerHTML='';
  lv.appendChild(card({live:true, count:(live&&live.transcript_count), src:(live&&live.src), dst:(live&&live.dst)}));
  var box=el('list'); box.innerHTML='';
  if(!list || !list.length){ box.innerHTML='<div class=empty>暂无往期会话转写。结束一次同传后会自动出现在此。</div>'; return; }
  list.forEach(function(o){ box.appendChild(card(o)); });
}
function load(){
  el('status').textContent='加载中…';
  Promise.all([
    fetch('/transcript/list').then(function(r){return r.json();}).catch(function(){return {sessions:[]};}),
    fetch('/config/langs').then(function(r){return r.json();}).catch(function(){return {};})
  ]).then(function(res){
    var list=(res[0]&&res[0].sessions)||[], live=res[1]||{};
    render(list, live);
    var extra=(live.transcript_on===false)?'（转写留存当前关闭：INTERP_TRANSCRIPT=0）':'';
    el('status').textContent='共 '+list.length+' 个往期会话'+extra;
  });
}
el('refresh').onclick=load;
el('content').onchange=load;
el('who').onchange=load;
load();
</script></body></html>"""


@app.get("/sessions", response_class=HTMLResponse)
def sessions_page():
    """会话转写导出页(自包含单页)：列出当前/往期会话，选内容/说话人后一键下 TXT/SRT/JSON。
    消费既有 /transcript/list(往期) + /config/langs(当前计数/开关) + /transcript.*(下载)。"""
    return _SESSIONS_PAGE


@app.get("/", response_class=HTMLResponse)
def index():
    return (_PAGE.replace("__HUB_BASE__", HUB_URL)
                 .replace("__HUB_PORT__", _HUB_PORT)
                 .replace("__MON_PORT_S__", _MON_PORT_S)
                 .replace("__MON_PORT__", _MON_PORT))


@app.get("/hub_profiles")
def hub_profiles():
    """同传页角色下拉的同源代理：页面直连 Hub(9000) 会被 CORS 拦（7900 不在 Hub 白名单，
    此前下拉一直只剩"(默认激活角色)"兜底项就是这么来的）；从本进程转发则同源无跨域。
    只取下拉需要的瘦身字段，不搬缩略图。has_voice 供下拉标记无音色角色(选择时即拦截)。"""
    try:
        return requests.get(f"{HUB_URL}/profiles",
                            params={"fields": "name,active,voicepack_spk,vp_featured,vp_fav,use_n,has_voice"},
                            timeout=6).json()
    except Exception as e:
        raise HTTPException(502, f"Hub 不可达: {e}")


# ══════════ 同传页(内嵌 HTML) ══════════
# 全站词汇表(所有按钮/提示/文档统一用词,新增文案从这里取词——与主控台口径一致):
#   开播   = 画面出镜(换脸/数字人推到虚拟摄像头,主控台功能,不含翻译)
#   同传   = 翻译传声(听写→翻译→克隆配音+双语字幕,本页功能)
#   直播同传 = 数字人开口说外语(需先「开播」出画面)   通话同传 = 微信/Zoom 克隆变声
#   通话向导 = 自动配设备+自检后开始(全站唯一保留"一键"语义的入口)
#   耳返   = 听自己发出的外语        朗读   = 听对方话的中文(对方音色)
#   急停   = 立刻闭麦并清空待播      测回声 = 实测扬声器↔麦是否互通(旧名"测耦合")
#   全双工 = 双方可同时说话          轮流说话(半双工) = 防回声,同一时间只翻一方
#   术语锁定 = 专有名词固定译法      场景方案 = 听说设备组合一键切换
# 交互提示统一用 data-tip="标题|作用|何时用"(富提示组件渲染);动态元素用 setAttribute。
_PAGE = r"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=theme-color content="#080b10">
<link rel=icon href=/favicon.ico>
<title>通译 LingoX · 实时同传</title>
<style>
/* 无界科技 BOUNDLESS 品牌令牌(与 brand.css 同源,内联以保持本页自包含) */
:root{
  color-scheme:dark; /* 原生控件(下拉弹层/滚动条)按暗色绘制，防浏览器按亮色/强制变色渲染导致选项不可读 */
  --acc:#4f7aff;--acc2:#a855f7;--grad:linear-gradient(135deg,#4f7aff,#a855f7);
  --bg:#080b10;--surface:rgba(255,255,255,.04);--surface2:rgba(255,255,255,.06);
  --bd:rgba(255,255,255,.12);--bd2:rgba(255,255,255,.18);
  --txt:#e5e9f5;--txt2:#aab4cc;--mut:#8b96b0;--faint:#5b6b8c;
  --ok:#34d399;--warn:#fbbf24;--danger:#f87171;
  --me:#4f7aff;--ot:#34d399;
  --r-sm:12px;--r-md:16px;
  --font:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei","Segoe UI",sans-serif;
}
*{box-sizing:border-box}
/* 弹性布局:顶部条自然高度、main 吃满剩余(嵌 iframe 时顶部条换行也不会把字幕列表挤出可视区,
   否则新字幕落在被裁剪的列表底部,看起来就是"字幕不滚动") */
body{margin:0;font:15px/1.5 var(--font);color:var(--txt);
  background:
    radial-gradient(120% 60% at 50% 0%,rgba(79,122,255,.16),transparent 55%),
    radial-gradient(120% 60% at 50% 100%,rgba(168,85,247,.12),transparent 50%),
    var(--bg);height:100vh;display:flex;flex-direction:column;overflow:auto}
body>header,body>.scenebar,body>.conflictbar,body>.ctl,body>.phonebar,body>.mbar,body>.note{flex-shrink:0}
.gt{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
/* 顶栏:品牌 + 状态 + 主操作 */
header{padding:14px 20px;border-bottom:1px solid var(--bd);
  display:flex;gap:14px;align-items:center;flex-wrap:wrap;background:var(--surface)}
.brand{display:flex;align-items:center;gap:11px;margin-right:6px}
.logo{width:34px;height:34px;border-radius:10px;background:var(--grad);display:grid;place-items:center;
  font-size:18px;font-weight:800;color:#fff;box-shadow:0 6px 20px rgba(79,122,255,.4)}
.brand .nm{font-size:17px;font-weight:800;letter-spacing:.3px;line-height:1.1}
.brand .sub{font-size:11px;color:var(--mut);margin-top:1px}
.spacer{flex:1}
.status{display:flex;align-items:center;gap:7px;font-size:13px;color:var(--txt2);
  background:var(--surface);border:1px solid var(--bd);border-radius:30px;padding:6px 13px}
.go:disabled{opacity:.65;cursor:wait;transform:none}
/* 手机无线终端就绪条:收口"手机端是否准备好",一眼可见+一键直达 */
.phonebar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 6px;padding:8px 12px;
  background:var(--surface);border:1px solid var(--bd);border-radius:12px;font-size:12px}
.phonebar .tag{font-weight:600;color:var(--txt2)}
.phonebar .chip{display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border-radius:999px;
  background:var(--surface2);border:1px solid var(--bd);color:var(--txt2)}
.phonebar .chip i{width:7px;height:7px;border-radius:50%;background:#6e7681;font-style:normal}
.phonebar .chip.ok i{background:var(--ok)}
.phonebar .chip.bad i{background:#f87171}
.phonebar .msg{color:var(--txt2)}
.phonebar .msg.ready{color:var(--ok);font-weight:600}
.phonebar .grow{flex:1}
.phonebar a.act{text-decoration:none;font-size:12px;color:var(--txt2);background:var(--surface2);
  border:1px solid var(--bd);border-radius:999px;padding:4px 11px;cursor:pointer}
.phonebar a.act:hover{border-color:var(--acc);color:var(--txt)}
.phonebar a.act.cta{color:#0b0f14;background:var(--acc);border-color:var(--acc);font-weight:600}
/* P8 场景方案卡：三预设一键切换 + 设备状态灯 + 双工徽章 + 耦合实测 */
.scenebar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:8px 16px 0;padding:8px 12px;
  background:var(--surface);border:1px solid var(--bd);border-radius:12px;font-size:12px}
.scenebar .tag{font-weight:600;color:var(--txt2)}
.scenebar .sc{display:inline-flex;align-items:center;gap:5px;padding:4px 12px;border-radius:999px;cursor:pointer;
  background:var(--surface2);border:1px solid var(--bd);color:var(--txt2);font-size:12px;transition:border-color .15s}
.scenebar .sc:hover{border-color:var(--acc)}
.scenebar .sc.on{color:#0b0f14;background:var(--acc);border-color:var(--acc);font-weight:700}
.scenebar .chip{display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border-radius:999px;
  background:var(--surface2);border:1px solid var(--bd);color:var(--txt2)}
.scenebar .chip i{width:7px;height:7px;border-radius:50%;background:#6e7681;font-style:normal}
.scenebar .chip.ok i{background:var(--ok)}
.scenebar .chip.bad i{background:#f87171}
.scenebar .dup{padding:3px 10px;border-radius:999px;border:1px solid var(--bd);font-weight:600}
.scenebar .dup.full{color:var(--ok);border-color:rgba(52,211,153,.4)}
.scenebar .dup.half{color:#fbbf24;border-color:rgba(251,191,36,.4)}
.scenebar button.act{font-size:12px;color:var(--txt2);background:var(--surface2);font-family:var(--font);
  border:1px solid var(--bd);border-radius:999px;padding:4px 11px;cursor:pointer}
.scenebar button.act:hover{border-color:var(--acc);color:var(--txt)}
.scenebar button.act:disabled{opacity:.55;cursor:wait}
.scenebar .msg{color:var(--txt2)}
.conflictbar{margin:6px 16px 0;padding:8px 12px;border-radius:10px;font-size:12px;line-height:1.7;
  background:rgba(248,113,113,.08);border:1px solid rgba(248,113,113,.35);color:#fca5a5}
.conflictbar.yellow{background:rgba(251,191,36,.08);border-color:rgba(251,191,36,.35);color:#fbbf24}
#dot{width:9px;height:9px;border-radius:50%;background:#6e7681;display:inline-block}
#dot.on{background:var(--ok);box-shadow:0 0 0 0 rgba(52,211,153,.6);animation:pulse 1.8s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(52,211,153,.55)}70%{box-shadow:0 0 0 7px rgba(52,211,153,0)}100%{box-shadow:0 0 0 0 rgba(52,211,153,0)}}
/* 控件条 */
.ctl{padding:11px 20px;border-bottom:1px solid var(--bd);display:flex;gap:10px 14px;flex-wrap:wrap;align-items:flex-end}
.field{display:flex;flex-direction:column;gap:4px}
.field label{font-size:11px;color:var(--mut);padding-left:2px}
/* 语向单行组:胶囊语对+生效+常用直达,与角色音色同排(原三层堆叠悬在中央、两侧大片空腔) */
#profile{max-width:200px}
.langrow{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.langseg{display:inline-flex;align-items:stretch;background:var(--surface2);border:1px solid var(--bd);
  border-radius:10px;overflow:hidden}
.langseg:hover{border-color:var(--bd2)}
.langseg select{background:transparent;border:0;border-radius:0;min-width:86px;padding:8px 9px}
.langseg select:hover{background:rgba(255,255,255,.05)}
.langseg button{background:transparent;border:0;border-left:1px solid var(--bd);border-right:1px solid var(--bd);
  color:var(--txt2);padding:0 10px;font-size:13px;cursor:pointer}
.langseg button:hover{color:var(--txt);background:rgba(255,255,255,.05)}
#langswap.spin{animation:lgspin .3s ease}
@keyframes lgspin{from{transform:rotate(-180deg)}to{transform:rotate(0)}}
/* 「生效」键只在运行中且语向有未生效改动时现身:琥珀渐变+轻脉冲=「有待生效的更改」 */
#langapply{display:none;border-radius:10px;padding:8px 14px;font-size:13px;font-weight:700;color:#fff;
  background:linear-gradient(135deg,#f59e0b,#f97316);box-shadow:0 6px 18px rgba(245,158,11,.3)}
#langapply.show{display:inline-block;animation:apulse 1.6s ease-in-out infinite}
#langapply:disabled{animation:none;opacity:.75;cursor:wait}
@keyframes apulse{0%,100%{transform:scale(1)}50%{transform:scale(1.045)}}
#langquick{display:inline-flex;gap:5px;flex-wrap:wrap;align-items:center}
#langquick .qlabel{font-size:11px;color:var(--mut)}
.qc{padding:4px 11px;border-radius:999px;background:var(--surface2);border:1px solid var(--bd);
  color:var(--txt2);font-size:12px;cursor:pointer;transition:border-color .15s,color .15s;flex:0 0 auto}
.qc:hover{border-color:var(--acc);color:var(--txt)}
.qc.on{color:#0b0f14;background:var(--acc);border-color:var(--acc);font-weight:700}
.eguide a.egact{color:var(--acc);cursor:pointer;text-decoration:underline;text-underline-offset:3px}
/* 线性图标(SVG sprite,继承文字色)：替代功能位 emoji——emoji 彩色/粗细/字号不受控,是首屏"乱"的微观来源 */
.ic{width:15px;height:15px;fill:none;stroke:currentColor;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round;
  vertical-align:-2.5px;display:inline-block;flex:0 0 auto}
.expmenu a .ic{margin-right:3px;opacity:.85}
/* 嵌入态(Hub iframe 内)：宿主已有品牌与卡头,本页品牌块隐藏、渐变底让位纯色,不再"页中页两套皮"。
   品牌隐去后状态胶囊靠左压阵、操作按钮居右,保持一行两端平衡 */
body.embed{background:var(--bg)}
body.embed .brand,body.embed .spacer{display:none}
body.embed header{padding:10px 16px}
body.embed .status{margin-right:auto}
select{background:var(--surface2);color:var(--txt);border:1px solid var(--bd);border-radius:10px;
  padding:8px 11px;font-size:13px;font-family:var(--font);min-width:120px;cursor:pointer}
select:hover{border-color:var(--bd2)}
/* 下拉弹层选项：select 的半透明底不会带进原生弹层，必须给实色深底+实色浅字，否则选项不可读 */
select option,select optgroup{background:#151b28;color:#e5e9f5}
select option:checked{background:#2b3550;color:#fff}
select option:disabled{color:#5b6b8c}
button{font-family:var(--font);cursor:pointer;border:none}
.go{background:var(--grad);color:#fff;font-weight:700;font-size:14px;border-radius:11px;
  padding:10px 22px;box-shadow:0 8px 24px rgba(79,122,255,.34);transition:transform .12s}
.go:hover{transform:translateY(-1px)}
.go.stop{background:linear-gradient(135deg,#f87171,#da3633);box-shadow:0 8px 24px rgba(248,113,113,.3)}
/* 双列字幕:flex:1 吃满剩余高度(不再用 calc 硬扣顶部高度——顶部条在窄窗口会换行长高) */
main{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:16px;
  flex:1 1 auto;min-height:220px;overflow:hidden}
.col{background:var(--surface);border:1px solid var(--bd);border-radius:var(--r-md);
  display:flex;flex-direction:column;overflow:hidden;min-height:0;box-shadow:0 10px 28px rgba(0,0,0,.4)}
.col h2{margin:0;padding:12px 16px;font-size:13px;font-weight:700;border-bottom:1px solid var(--bd);
  display:flex;align-items:center;gap:8px}
.col h2::before{content:"";width:8px;height:8px;border-radius:50%}
.col.me h2{color:#a9c3ff}.col.me h2::before{background:var(--me)}
.col.ot h2{color:#9af0c8}.col.ot h2::before{background:var(--ot)}
.list{flex:1;overflow:auto;padding:14px 16px;display:flex;flex-direction:column;gap:10px}
.row{animation:f .22s;background:var(--surface2);border:1px solid var(--bd);
  border-left:3px solid var(--bd);border-radius:var(--r-sm);padding:11px 14px;
  transition:opacity .35s,background .35s,box-shadow .35s;opacity:.6}
.me .row{border-left-color:var(--me)}.ot .row{border-left-color:var(--ot)}
.row.latest{opacity:1;background:rgba(79,122,255,.10);box-shadow:0 0 0 1px var(--bd2) inset}
.ot .row.latest{background:rgba(52,211,153,.10)}
.row.vlblock{background:rgba(248,113,113,.06);box-shadow:0 0 0 1px rgba(248,113,113,.22) inset}
.affirmbtn{background:transparent;border:1px solid rgba(248,113,113,.5);color:#fca5a5;
  border-radius:8px;padding:2px 10px;font-size:12px;cursor:pointer;margin-left:8px}
.affirmbtn:hover{background:rgba(248,113,113,.12)}
.affirmbtn:disabled{cursor:default;opacity:.8}
@keyframes f{from{opacity:0;transform:translateY(6px)}to{opacity:.6}}
/* 主=中文(大·醒目)，次=英文(小·暗) */
.src{font-size:21px;font-weight:700;line-height:1.42;color:var(--txt);letter-spacing:.2px;word-break:break-word}
.dst{font-size:13.5px;color:var(--mut);margin-top:5px;min-height:1em;line-height:1.45;word-break:break-word}
.cl{margin-right:3px}.cl.fin{color:var(--txt)}
.pend{opacity:.5;font-style:italic}
/* P6.3 情感徽章：该句实际用的情感配音标签(SBV2 style / CosyVoice instruct) */
.emob{display:inline-block;margin-left:8px;padding:1px 8px;border-radius:999px;font-size:10.5px;
      font-weight:600;vertical-align:1px;border:1px solid;opacity:.92;cursor:help}
@keyframes gerflash{0%{background:rgba(96,165,250,.35)}100%{background:transparent}}
.cl.gerfix{animation:gerflash 1.4s ease-out;border-radius:3px}
.meta{font-size:10px;color:var(--faint);margin-top:5px;font-family:ui-monospace,Consolas,monospace}
/* 空状态引导:列表无字幕时显示上手三步(左列)/一句说明(右列),数据取自现有状态 */
.eguide{margin:auto;padding:16px 22px;color:var(--mut);font-size:13px;line-height:2.05;max-width:440px}
.eguide b{color:var(--txt2)}
.eguide .egt{font-weight:800;color:var(--txt2);letter-spacing:1px}
.eguide .egs{margin-top:6px;font-size:12px;color:var(--faint);line-height:1.6}
#eguide2.eguide{text-align:center;color:var(--faint)}
/* 富提示(data-tip="标题|作用|何时用"):150ms 即出、样式统一、边缘自动翻转——替代原生 title 的慢与小 */
#tipbox{position:fixed;z-index:99;max-width:300px;background:#0f141b;border:1px solid var(--bd2);
  border-radius:10px;padding:9px 12px;font-size:12px;line-height:1.55;color:var(--txt2);
  box-shadow:0 12px 34px rgba(0,0,0,.55);pointer-events:none;display:none}
#tipbox .tt{font-weight:700;color:var(--txt);font-size:12.5px;margin-bottom:2px}
#tipbox .tw{color:var(--faint);margin-top:3px;font-size:11.5px}
.field[data-tip]>label{text-decoration:underline dotted rgba(139,148,158,.55);text-underline-offset:3px}
#followBtn{position:fixed;right:18px;bottom:64px;z-index:70;border:1px solid var(--bd);
  background:var(--surface2);color:var(--txt2);font-size:12px;padding:8px 13px;border-radius:20px;
  cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.35)}
#followBtn.paused{background:linear-gradient(135deg,#4f7aff,#a855f7);border-color:transparent;
  color:#fff;font-weight:700;animation:fpulse 1.6s ease-in-out infinite}
@keyframes fpulse{0%,100%{transform:scale(1)}50%{transform:scale(1.06)}}
/* 观测条 + 提示 */
.mbar{padding:8px 20px;font:12px/1.4 ui-monospace,Consolas,monospace;color:var(--mut);
  border-top:1px solid var(--bd);display:flex;gap:16px;flex-wrap:wrap;align-items:center;background:var(--surface)}
.mbar b{color:var(--txt2);font-weight:600}.mbar .hi{color:var(--warn)}.mbar .bad{color:var(--danger)}
.mbar .ok{color:var(--ok)}.mbar .tag{display:inline-block;padding:1px 7px;border-radius:4px;
  font-size:11px;border:1px solid var(--bd);background:rgba(0,0,0,.2)}
.mbar .tag.on{border-color:rgba(74,222,128,.4);color:#86efac}
.mbar .tag.wait{border-color:rgba(251,191,36,.4);color:#fcd34d}
.mbar .tag.off{opacity:.5}
#spark,#spark2{background:rgba(0,0,0,.25);border:1px solid var(--bd);border-radius:6px;vertical-align:middle}
#warn{color:var(--warn);margin-left:auto}
.note{padding:9px 20px;font-size:12px;color:var(--mut);border-top:1px solid var(--bd)}
.note b{color:var(--txt2)}
/* 次级幽灵按钮(声音/工具) */
.go.ghost{background:var(--surface2);color:var(--txt2);border:1px solid var(--bd);
  box-shadow:none;font-weight:600;padding:9px 14px;font-size:13px}
.go.ghost:hover{border-color:var(--bd2);color:var(--txt);transform:none}
/* 拆分主按钮:「▶ 开始」+「▾」(一键通话向导/演示收进下拉) */
.startgrp{display:inline-flex;align-items:stretch}
.startgrp #btn{border-radius:11px 0 0 11px}
.startgrp .caret{border-radius:0 11px 11px 0;padding:10px 11px;margin-left:1px;font-size:12px}
/* 菜单内:节标题 / 开关项点亮态 / 内嵌调参手风琴 */
.expmenu .mlabel{padding:8px 12px 2px;font-size:11px;color:var(--mut);letter-spacing:1.5px;font-weight:700}
.expmenu a.dis{opacity:.55;pointer-events:none}
#tunemenu{padding:0 2px 4px}
/* 高级设置折叠面板(引擎/设备覆盖,默认收起) */
#advpanel{display:none;border-top:1px dashed var(--bd);background:rgba(255,255,255,.02)}
#advpanel.on{display:flex}
#advbtn.on{border-color:var(--acc);color:var(--txt)}
.advhint{flex-basis:100%;font-size:11px;color:var(--faint);margin-bottom:2px}
/* 导出下拉菜单(本页TXT/完整TXT/字幕SRT/JSON) */
.expwrap{position:relative;display:inline-block}
.expwrap .expn{font-size:11px;opacity:.7;font-weight:400}
.expmenu{position:absolute;right:0;top:calc(100% + 6px);min-width:214px;background:#0f141b;
  border:1px solid var(--bd);border-radius:12px;padding:6px;box-shadow:0 12px 34px rgba(0,0,0,.5);
  display:none;z-index:40}
.expmenu.on{display:block}
.expmenu a{display:block;padding:9px 12px;border-radius:8px;font-size:13px;color:var(--txt);
  cursor:pointer;white-space:nowrap}
.expmenu a small{color:var(--mut);font-weight:400}
.expmenu a:hover{background:rgba(255,255,255,.07)}
.expmenu .sep{height:1px;background:var(--bd);margin:5px 6px}
/* P3-1 实战调参面板(复用下拉容器) */
.tnrow{padding:7px 12px 3px}
.tnrow label{display:flex;justify-content:space-between;gap:12px;font-size:12.5px;color:var(--txt);margin-bottom:2px;cursor:help}
.tnrow .tnval{color:var(--mut);font-variant-numeric:tabular-nums}
.tnrow input[type=range]{width:100%;accent-color:#6ea8fe;height:18px;cursor:pointer}
/* 大字幕/全屏模式 */
#fsov{position:fixed;inset:0;z-index:60;background:rgba(6,9,14,.975);display:none;
  flex-direction:column;padding:4vh 4vw 3vh;gap:2vh}
#fsov.on{display:flex}
#fsclose{position:absolute;top:16px;right:22px;color:var(--mut);cursor:pointer;font-size:13px;
  background:var(--surface2);border:1px solid var(--bd);border-radius:22px;padding:7px 15px}
#fsclose:hover{color:var(--txt);border-color:var(--bd2)}
.fsblock{flex:1;overflow:hidden;display:flex;flex-direction:column;justify-content:flex-end;gap:1.4vh;
  border-bottom:1px solid var(--bd);padding-bottom:1.4vh}
.fsblock:last-child{border-bottom:none}
.fsblock .fl{font-size:12px;font-weight:800;letter-spacing:2px;opacity:.65}
.fsblock.me .fl{color:#a9c3ff}.fsblock.ot .fl{color:#9af0c8}
.fsline{font-size:min(4.6vw,46px);font-weight:800;line-height:1.28;color:#fff;animation:fsin .25s}
.fsline.old{opacity:.34;font-size:min(3.1vw,30px);font-weight:700}
.fsline .en{display:block;font-size:.46em;font-weight:500;color:var(--mut);margin-top:.18em}
@keyframes fsin{from{opacity:0;transform:translateY(10px)}to{opacity:1}}
@media(max-width:760px){main{grid-template-columns:1fr;flex:none;height:auto;overflow:visible}
  .list{max-height:44vh}}
</style></head><body>
<!-- 线性图标(内联 sprite)：功能位统一单色线性图标,与暗色霓虹主题同源;emoji 只留内容位。
     全站图标库单一真相=static/brand-icons.svg(Hub 侧)；本页跨端口自包含,内联持有所用子集拷贝,
     新增图标先入库再同步这里(tools/_gen_brand_icons.py 生成库文件)。 -->
<svg style="display:none" aria-hidden="true"><defs>
<symbol id=i-sound viewBox="0 0 24 24"><path d="M11 5 6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.4 5.6a9 9 0 0 1 0 12.8"/></symbol>
<symbol id=i-headphones viewBox="0 0 24 24"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M3 14h3a1 1 0 0 1 1 1v4a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z"/><path d="M21 14h-3a1 1 0 0 0-1 1v4a1 1 0 0 0 1 1h2a1 1 0 0 0 1-1z"/></symbol>
<symbol id=i-waves viewBox="0 0 24 24"><path d="M2 10v4"/><path d="M6 6v12"/><path d="M10 3v18"/><path d="M14 8v8"/><path d="M18 5v14"/><path d="M22 10v4"/></symbol>
<symbol id=i-tools viewBox="0 0 24 24"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></symbol>
<symbol id=i-fullscreen viewBox="0 0 24 24"><path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/></symbol>
<symbol id=i-folder viewBox="0 0 24 24"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></symbol>
<symbol id=i-lock viewBox="0 0 24 24"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></symbol>
<symbol id=i-sliders viewBox="0 0 24 24"><path d="M21 4h-7"/><path d="M10 4H3"/><path d="M21 12h-9"/><path d="M8 12H3"/><path d="M21 20h-5"/><path d="M12 20H3"/><path d="M14 2v4"/><path d="M8 10v4"/><path d="M16 18v4"/></symbol>
<symbol id=i-monitor viewBox="0 0 24 24"><rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8"/><path d="M12 17v4"/></symbol>
<symbol id=i-phone viewBox="0 0 24 24"><rect x="7" y="2" width="10" height="20" rx="2"/><path d="M12 18h.01"/></symbol>
<symbol id=i-qr viewBox="0 0 24 24"><rect x="3" y="3" width="6" height="6" rx="1"/><rect x="15" y="3" width="6" height="6" rx="1"/><rect x="3" y="15" width="6" height="6" rx="1"/><path d="M15 15h2v2h-2z"/><path d="M19 15h2v2h-2z"/><path d="M15 19h2v2h-2z"/><path d="M19 19h2v2h-2z"/></symbol>
<symbol id=i-call viewBox="0 0 24 24"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"/></symbol>
<symbol id=i-demo viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="m10 8 6 4-6 4z"/></symbol>
<symbol id=i-live viewBox="0 0 24 24"><path d="m22 8-6 4 6 4V8z"/><rect x="2" y="6" width="14" height="12" rx="2"/></symbol>
<symbol id=i-scene viewBox="0 0 24 24"><path d="m12 2 10 5.5L12 13 2 7.5Z"/><path d="m2 12.5 10 5.5 10-5.5"/><path d="m2 17.5 10 5.5 10-5.5" opacity=".45"/></symbol>
<symbol id=i-probe viewBox="0 0 24 24"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></symbol>
<symbol id=i-zap viewBox="0 0 24 24"><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/></symbol>
<symbol id=i-package viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="5" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/></symbol>
<symbol id=i-stopcircle viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M9 9l6 6"/><path d="M15 9l-6 6"/></symbol>
</defs></svg>
<header>
  <div class=brand>
    <div class=logo>译</div>
    <div><div class=nm><span class=gt>通译 LingoX</span></div><div class=sub>实时双向同传 · 克隆原声</div></div>
  </div>
  <div class=spacer></div>
  <div class=status><span id=dot></span><span id=st>未运行</span></div>
  <div class=expwrap>
    <button class="go ghost" id=sndbtn data-tip="声音|耳返、对方朗读两个开关和三路音量都在这里|通话中随时调,拖动即时生效"><svg class=ic><use href="#i-sound"/></svg> 声音<span id=sndbadge class=expn></span></button>
    <div class=expmenu id=sndmenu style="min-width:288px">
      <a id=monitor data-tip="耳返 · 听自己发出的外语|把发给对方的克隆音小声放进你的耳机,确认对方听到了什么|不放心翻译质量时开;务必戴耳机,外放会回声"><svg class=ic><use href="#i-headphones"/></svg> 耳返</a>
      <a id=readback data-tip="对方朗读 · 免盯字幕|对方说的外语翻成中文后,用 TA 自己的音色读进你耳机|想脱屏听译文时开;对方开口第一句自动捕获音色"><svg class=ic><use href="#i-waves"/></svg> 对方朗读</a>
      <div class=sep></div>
      <div class=mlabel>音量 · 拖动即时生效</div>
      <div id=sndvols></div>
    </div>
  </div>
  <div class=expwrap>
    <button class="go ghost" id=toolbtn data-tip="工具|大字幕、导出转写、术语表、高级调参、页面入口|低频功能都收在这里"><svg class=ic><use href="#i-tools"/></svg> 工具</button>
    <div class=expmenu id=toolmenu style="min-width:252px">
      <a id=fs data-tip="大字幕|全屏放大双语字幕,适合边通话边看|快捷键 F 开关,Esc 退出"><svg class=ic><use href="#i-fullscreen"/></svg> 大字幕 <small>(F)</small></a>
      <div class=sep></div>
      <div class=mlabel>导出转写 <span id=expn></span></div>
      <a id=expScreen data-tip="本页对话 TXT|把当前屏幕上的双语对话存成文本|快速留底当前这一屏">本页对话 · TXT <small>(当前屏)</small></a>
      <a id=expTxt data-tip="完整转写 TXT|全程逐句记录,含时间戳,页面没开着的时段也在|会后复盘/交付文字稿">完整转写 · TXT <small>(含时间戳)</small></a>
      <a id=expSrt data-tip="字幕文件 SRT|标准字幕格式,时间轴对齐|配录像/剪辑软件用">字幕文件 · SRT <small>(配录像/剪辑)</small></a>
      <a id=expJson data-tip="原始数据 JSON|逐句原文/译文/耗时等全部字段|排查问题或二次处理">原始数据 · JSON</a>
      <a id=expSessions data-tip="往期会话|选择历史场次,单独导出|找以前某一场的记录"><svg class=ic><use href="#i-folder"/></svg> 往期会话… <small>(选历史场次导出)</small></a>
      <div class=sep></div>
      <a href="/glossary" target=_blank data-tip="术语锁定|把品牌/人名/专有名词固定成指定译法,翻错自动纠正|专有名词总被翻错时来这里加一条"><svg class=ic><use href="#i-lock"/></svg> 术语表 <small id=gcount></small></a>
      <a id=tuneItem data-tip="高级调参|回声闸/声纹门槛/降噪等 6 个参数,拖动立即生效并自动记住|出现漏回声、误拦人声时微调"><svg class=ic><use href="#i-sliders"/></svg> 高级调参…</a>
      <div class=sep></div>
      <div class=mlabel>打开</div>
      <a id=hubLink target=_blank style="display:none" data-tip="主控台|打开数字人主控台(9000)|管理角色/开播/换脸"><svg class=ic><use href="#i-monitor"/></svg> 主控台</a>
      <a id=phoneLink target=_blank data-tip="手机端|手机变无线麦克风+监听耳机|人不在电脑前时用手机说和听"><svg class=ic><use href="#i-phone"/></svg> 手机端</a>
      <a id=qrLink target=_blank data-tip="扫码页|大二维码页面,手机扫码直达手机端|给手机快速配对"><svg class=ic><use href="#i-qr"/></svg> 扫码页</a>
    </div>
    <div class=expmenu id=tunemenu style="min-width:290px"></div>
  </div>
  <button class="go ghost" id=panic data-tip="急停 · 立刻闭麦|0.1 秒切断正在播的克隆音并清空待播队列;再按一次恢复|说错话、误识别时拍它" style="display:none;border-color:rgba(248,113,113,.55);color:#fca5a5"><svg class=ic><use href="#i-stopcircle"/></svg> 急停</button>
  <div class="expwrap startgrp">
    <button class=go id=btn data-tip="开始同传|按当前选好的角色/语言/设备直接开始翻译|设备已配好的日常使用;首次建议用右边 ▾ 里的通话向导">▶ 开始</button><button class="go caret" id=startCaret data-tip="更多开始方式|通话向导(自动配设备)、直播同传和演示模式在这里|首次使用或想先看效果时点开">▾</button>
    <div class=expmenu id=startmenu style="min-width:262px">
      <a id=callmode data-tip="通话向导 · 自动配设备|默认麦切 CABLE→设备按名绑定→测试音自检→开始,每步红绿灯|首次通话或换了设备后推荐"><svg class=ic><use href="#i-call"/></svg> 通话向导 <small>(自动配设备 · 首次推荐)</small></a>
      <a id=livemode data-tip="直播同传 · 数字人开口说外语|切输出方式为直播并立即开始(开播联动自动激活当前角色)|先在主控台「开播」出画面,再点这里配声音"><svg class=ic><use href="#i-live"/></svg> 直播同传 <small>(需先开播)</small></a>
      <a id=demo data-tip="演示模式|不用麦克风,播放预设脚本看完整同传效果|第一次了解产品,或给别人展示"><svg class=ic><use href="#i-demo"/></svg> 演示模式 <small>(无需麦克风)</small></a>
    </div>
  </div>
</header>
<div class=scenebar id=scenebar>
  <span class=tag data-tip="场景方案|一键切换你的听说设备组合(麦/耳机/虚拟麦自动按名绑定)|换了设备环境就换方案,不用逐个选设备"><svg class=ic><use href="#i-scene"/></svg> 场景方案</span>
  <span id=scChips style="display:inline-flex;gap:6px;flex-wrap:wrap"></span>
  <span class=chip id=scMic><i></i><span id=scMicT>麦</span></span>
  <span class=chip id=scListen><i></i><span id=scListenT>监听</span></span>
  <span class=chip id=scDub><i></i><span id=scDubT>虚拟麦</span></span>
  <span class=dup id=scDup style="display:none"></span>
  <button class=act id=scProbe data-tip="测回声 · 回声体检|播 2 声测试音,实测扬声器的声音会不会被麦收回去;互通→自动改轮流说话防回声,隔离→可同时说话|换了音响/耳机/方案后测一次(停止状态可用)"><svg class=ic><use href="#i-probe"/></svg> 测回声</button>
  <button class=act id=scDubTest data-tip="试音 · 端到端配音自检|真的用你的克隆音色合成一句话、推给对方的收音口(CABLE)并回录,一次验成 音色+引擎+虚拟麦通路 一盏灯|通话前点一下确认对方能听到;通话中点会把这句测试语播给对方"><svg class=ic><use href="#i-zap"/></svg> 试音</button>
  <button class=act id=scFixLoop style="display:none;border-color:var(--warn,#fbbf24)" data-tip="修复对方声来源 · 治自听串扰|检测到「对方声来源」和「你的麦」是同一块声卡时出现:你自己的话会被当成对方再识别一遍(串语言/乱字幕)|点它一键改用「立体声混音」抓对方声并重启采集链"><svg class=ic><use href="#i-tools"/></svg> 修复对方声来源</button>
  <button class=act id=scEngineFix style="display:none;border-color:var(--warn,#fbbf24)" data-tip="拉起配音引擎 · 治主引擎掉线|当前配音主引擎(如 CosyVoice)进程没在跑时出现:虽有兜底能出声,但会丢情感/延迟偏高|点它经主控台把主引擎重新拉起来"><svg class=ic><use href="#i-tools"/></svg> 拉起配音引擎</button>
  <span class=chip id=scEngineChip style="display:none"><i></i><span id=scEngineT>引擎</span></span>
  <span class=chip id=scLastDub style="display:none"><i></i><span id=scLastDubT>试音</span></span>
  <span class=msg id=scMsg></span>
</div>
<div class=conflictbar id=conflictbar style="display:none"></div>
<div class=ctl>
  <div class=field data-tip="角色音色|用哪个克隆音色向对方说话;运行中也能切,下一句无缝生效|开始前选好;直播中可点「预载常用」提前准备 Top5 常用角色(省切换等待)"><label>角色音色 <span id=swtag></span> <a id=preload style="display:none;cursor:pointer;color:var(--warn);font-size:11px" title="只预载你最常用的 5 个角色(按使用次数)，不抢 GPU 给全部角色">预载常用</a></label><select id=profile><option value="" disabled selected>加载中…</option></select><div id=pstat style="display:none;font-size:11px;color:var(--mut);margin-top:3px;line-height:1.5"></div></div>
  <div class=field data-tip="语向|左=我说的语言,右=对方的语言,⇄ 一键对调;点常用胶囊一步直达|下拉/胶囊选好即记住;通话中改动会亮出琥珀「生效」键,点它自动按新语言重启识别(不点只改翻译,识别仍按旧语言→会串语言)">
    <label>语向 · 我说 ⇄ 对方 <span id=langtag></span></label>
    <div class=langrow>
      <span class=langseg>
        <select id=lsrc aria-label="我说的语言"><option value="" disabled selected>加载中…</option></select>
        <button id=langswap type=button data-tip="对调语向|把「我说」和「对方」两种语言互换|想反向翻译时点一下">⇄</button>
        <select id=ldst aria-label="对方的语言"><option value="" disabled selected>加载中…</option></select>
      </span>
      <button id=langapply type=button data-tip="生效 · 按新语向重启识别|自动停→按新语言重启采集链,流式识别即时按新语向工作|通话中改了语向就点它;不点只改翻译目标,识别语言不变→会串语言">生效</button>
      <span id=langquick data-tip="常用语向一键直达|我说中文→对方听这个语言,点一下即切换并生效(通话中自动重启)|避开 24 项下拉框误点(实测点日语点成韩/俄)"><span class=qlabel>常用→</span></span>
    </div>
  </div>
  <div class=field data-tip="输出方式|通话=克隆音走虚拟麦给微信/Zoom;直播=数字人开口说外语+字幕(出镜经虚拟摄像头)|决定这一场是打电话还是开直播"><label>输出方式</label><select id=omode><option value=call>📞 通话(VB-Cable)</option><option value=live>🎭 直播(数字人)</option></select></div>
  <div class=field><button id=advbtn type=button class="go ghost" data-tip="高级设置|配音/翻译/字幕引擎与设备覆盖(我的麦/虚拟麦/对方声来源)|通常不用动:设备已按场景方案自动匹配"><svg class=ic><use href="#i-sliders"/></svg> 高级设置</button></div>
</div>
<div class=ctl id=advpanel>
  <div class=advhint>引擎与设备通常无需手动改——设备已按上方「场景方案」自动匹配，仅特殊情况在此覆盖(切换方案后以方案为准)。</div>
  <div class=field data-tip="配音引擎|Fish=默认最稳;Qwen3=低延迟流式;CosyVoice=情感克隆|请求失败会自动回退 Fish,平时无需改"><label>配音引擎</label><select id=ttsengine><option value=fish>Fish(默认)</option><option value=qwen3>Qwen3(低延迟)</option><option value=cosyvoice>CosyVoice(情感)</option></select></div>
  <div class=field data-tip="翻译模式|本地NMT=快·准·离线;Whisper直译=识别翻译一步出英文|默认本地NMT即可"><label>翻译模式</label><select id=mode><option value=local>本地NMT(快·准·离线)</option><option value=whisper>Whisper直译(英文一步出)</option></select></div>
  <div class=field data-tip="字幕引擎|逐词流式=边说边出字(Nemotron);整句分段=说完一句出一句(Whisper,更稳)|想要更快的字幕用逐词,识别不稳时换整句"><label>字幕引擎</label><select id=stream><option value=1>逐词流式(Nemotron)</option><option value=0>整句分段(Whisper)</option></select></div>
  <div class=field data-tip="我的麦|收你说话的麦克风;选「手机麦」则用手机无线收音|平时跟随场景方案,声音收不到时来这里换"><label>我的麦</label><select id=mic><option value="" disabled selected>加载中…</option></select></div>
  <div class=field id=fcable data-tip="虚拟麦(给对方)|克隆音输出到的虚拟声卡,通话 App 的麦克风要选它对应的 CABLE Output|通话模式必配;直播模式不用"><label>虚拟麦(给对方)</label><select id=cable><option value="" disabled selected>加载中…</option></select></div>
  <div class=field data-tip="对方声来源|从哪里采集对方的声音(推荐立体声混音=抓扬声器)|听不到对方翻译时,第一个来这里换设备"><label>对方声来源 <span id=loophint style="font-size:11px;font-weight:400"></span></label><select id=loop><option value="" disabled selected>加载中…</option></select></div>
</div>
<div class=phonebar id=phonebar style="display:none">
  <span class=tag data-tip="手机端|手机当无线麦克风和监听耳机,免走线|人离电脑说话/想用手机听翻译时">📱 手机端</span>
  <span class="chip" id=pbTerm data-tip="终端|手机页面是否已打开并连上|灰/红=手机扫码打开手机端"><i></i>终端</span>
  <span class="chip" id=pbAudio data-tip="监听|手机是否在收听翻译音频|红=在手机端点开「监听」"><i></i>监听</span>
  <span class="chip" id=pbMic data-tip="手机麦|手机麦克风是否已接入|红=在手机端点开「对讲」"><i></i>手机麦</span>
  <span class="chip" id=pbCam data-tip="摄像头|手机摄像头是否已接入(直播模式用)|红=在手机端点开「摄像头」"><i></i>摄像头</span>
  <span class="msg" id=pbMsg></span>
  <span class=grow></span>
  <a class="act cta" id=pbOpen target=_blank data-tip="打开手机端|在新窗口打开手机端页面(也可手机直接扫码)|手机在手边时点这里">打开手机端</a>
  <a class=act id=pbShow target=_blank data-tip="扫码页|大二维码,手机扫码直达|手机不在同一浏览器时用">扫码页</a>
</div>
<main>
  <div class="col me"><h2 id=hme>我 · 中文 → 英文(对方听到克隆英文)</h2><div class=list id=lme></div></div>
  <div class="col ot"><h2 id=hot>对方 · 英文 → 中文字幕</h2><div class=list id=lot></div></div>
</main>
<div class=mbar id=mbar>
  <b data-tip="观测指标|ASR=听写耗时 · NMT=翻译耗时 · 端到端=你说完到对方听到 · 积压=排队句数|数值持续变红时留意网络与显卡占用">观测</b><span id=livestat></span><span id=swstat></span><span id=mtxt>未运行</span>
  <span data-tip="端到端延迟走势|越低越好,毛刺=偶发卡顿|"><canvas id=spark width=120 height=22></canvas></span>
  <span id=spark2wrap data-tip="口型/合成延迟走势|直播模式下数字人出画的耗时|" style="display:none"><canvas id=spark2 width=120 height=22></canvas></span>
  <span id=warn></span></div>
<div class=note id=hint></div>
<div id=fsov>
  <div id=fsclose>✕ 退出大字幕 (Esc)</div>
  <div class="fsblock me" id=fsme></div>
  <div class="fsblock ot" id=fsot></div>
</div>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const IC=n=>'<svg class=ic><use href="#i-'+n+'"/></svg>';   // 线性图标(sprite 引用),动态改写按钮文案时用它拼
// 嵌入态：Hub iframe 内品牌块隐藏/纯色底(宿主已有品牌与卡头)。?embed=0/1 可强制覆盖(预览/调试)。
const EMBED=(()=>{ try{ const q=new URLSearchParams(location.search);
  if(q.get('embed')==='1') return true; if(q.get('embed')==='0') return false;
  return window.self!==window.top; }catch(e){ return false; } })();
if(EMBED) document.body.classList.add('embed');
function setupNav(){
  const host=location.hostname||'127.0.0.1';
  // 端口由服务端注入(__HUB_PORT__/__MON_PORT__)：两套安装并存(端口偏移)时链接不再指到别家
  $('#hubLink').href='http://'+host+':__HUB_PORT__/';
  $('#phoneLink').href='https://'+host+':__MON_PORT_S__/';
  $('#qrLink').href='http://'+host+':__MON_PORT__/show';
  const op=$('#pbOpen'); if(op) op.href='https://'+host+':__MON_PORT_S__/';
  const sh=$('#pbShow'); if(sh) sh.href='http://'+host+':__MON_PORT__/show';
  // 「主控台」仅独立窗口打开时才显示——嵌在主控台 iframe 里时它指向的就是外层页面,纯冗余
  try{ if(window.self===window.top) $('#hubLink').style.display=''; }catch(e){}
}
setupNav();
let running=false, demoRunning=false, lastId=0, es=null;
// ── 顶栏菜单管理(声音/工具/调参/开始▾ 共用一套):同时只开一个,点菜单外任意处关闭 ──
function closeMenus(){ document.querySelectorAll('.expmenu.on').forEach(m=>m.classList.remove('on')); }
document.addEventListener('click',closeMenus);
function bindMenu(btnSel,menuSel,onOpen){
  const b=$(btnSel),m=$(menuSel); if(!b||!m) return;
  b.onclick=async(e)=>{ e.stopPropagation();
    const was=m.classList.contains('on'); closeMenus();
    if(!was){ if(onOpen) await onOpen(); m.classList.add('on'); } };
  m.onclick=(e)=>e.stopPropagation();
}
// ── 富提示组件:data-tip="标题|作用|何时用" → 悬停 150ms 出三段式卡片,边缘自动翻转。
//    替代原生 title(出得慢/字太小/不可控);动态生成的元素同样生效(事件委托)。──
(function(){
  const box=document.createElement('div'); box.id='tipbox';
  addEventListener('DOMContentLoaded',()=>document.body.appendChild(box));
  let timer=null, cur=null;
  function show(el){
    const raw=el.getAttribute('data-tip')||''; if(!raw) return;
    const p=raw.split('|'), t=p[0]||'', w=p[1]||'', when=p[2]||'';
    box.innerHTML=(t?'<div class=tt>'+esc(t)+'</div>':'')+(w?'<div>'+esc(w)+'</div>':'')
      +(when?'<div class=tw>💡 '+esc(when)+'</div>':'');
    box.style.display='block'; box.style.visibility='hidden'; box.style.left='0px'; box.style.top='0px';
    const r=el.getBoundingClientRect(), bw=box.offsetWidth, bh=box.offsetHeight;
    let x=Math.min(Math.max(8,r.left), innerWidth-bw-8);
    let y=r.bottom+8; if(y+bh>innerHeight-8) y=r.top-bh-8;
    box.style.left=x+'px'; box.style.top=y+'px'; box.style.visibility='visible';
  }
  const hide=()=>{ clearTimeout(timer); box.style.display='none'; };
  document.addEventListener('mouseover',e=>{
    const el=e.target.closest?e.target.closest('[data-tip]'):null;
    if(el===cur) return;
    cur=el; hide();
    if(el) timer=setTimeout(()=>show(el),150);
  });
  document.addEventListener('mousedown',hide);
  addEventListener('scroll',hide,true);
})();
// 主按钮情境化:未运行=开始 / 运行中=停止 / 演示中=停止演示;急停仅运行期显示
function syncMainBtn(){ const b=$('#btn'); if(!b) return;
  if(demoRunning){ b.textContent='■ 停止演示'; b.className='go stop';
    b.setAttribute('data-tip','停止演示|结束演示播放,回到待机|'); }
  else if(running){ b.textContent='■ 停止'; b.className='go stop';
    b.setAttribute('data-tip','停止同传|结束本场会话并写入场次日志|说完了就停,避免误采集'); }
  else { b.textContent='▶ 开始'; b.className='go';
    b.setAttribute('data-tip','开始同传|按当前选好的角色/语言/设备直接开始翻译|设备已配好的日常使用;首次建议用右边 ▾ 里的通话向导'); }
  const c=$('#startCaret'); if(c) c.className='go caret'+((running||demoRunning)?' stop':''); }
function syncCtx(){ const p=$('#panic'); if(p) p.style.display=(running||demoRunning||muted)?'':'none'; }
function setDemoUI(on){ demoRunning=on;
  const d=$('#demo'); if(d) d.innerHTML=on?'■ 停止演示':IC('demo')+' 演示模式 <small>(无需麦克风)</small>';
  if(on){ $('#dot').className='on'; $('#st').textContent='演示中…'; }
  else if(!running){ $('#dot').className=''; $('#st').textContent='已停止'; }
  syncMainBtn(); syncCtx(); syncEmptyGuide(); syncLangUI(); }
// ── 空状态引导:字幕列表没内容时,左列显示"开始前 3 步"(实时取当前角色/方案)+上次会话摘要,
//    右列一句话说明;运行后变成"请开口说话"。有真实字幕行即自动移除。──
let lastSession=null;   // /session/last 摘要(原 Hub 卡头观测条独有信息,随观测条退役迁到这里)
async function loadLastSession(){
  try{ const j=await (await fetch('/session/last')).json();
    lastSession=(j&&j.summary)||null; syncEmptyGuide(); }catch(e){}
}
function lastSessionLine(){
  const s=lastSession; if(!s||running||demoRunning) return '';
  const c=s.counts||{}, v=x=>(x==null?'—':x);
  return '<div class=egs>上次会话：'+esc(s.profile||'—')+' · 我'+(c.a||0)+'/对方'+(c.b||0)+' 句 · 端到端 '
    +v(s.e2e_ms)+'ms'+(s.live_mode?(' · TTFV '+v(s.ttfv_ms)+'ms'):'')+'</div>';
}
function syncEmptyGuide(){
  const lme=$('#lme'), lot=$('#lot'); if(!lme||!lot) return;
  const mk=(host,id)=>{ let g=host.querySelector('#'+id);
    if(host.querySelector('.row')){ if(g) g.remove(); return null; }
    if(!g){ g=document.createElement('div'); g.id=id; g.className='eguide'; host.appendChild(g); }
    return g; };
  const g1=mk(lme,'eguide');
  if(g1){
    if(running||demoRunning){ g1.innerHTML='🎙 已开始——请开口说话,你说的每句话和译文会出现在这里…'; }
    else{
      const prof=(($('#profile')||{}).value||'').trim()||'默认激活角色';
      const sc=document.querySelector('#scChips .sc.on');
      const scene=sc?sc.textContent.trim():'—';
      g1.innerHTML='<div class=egt>🚀 开始前 3 步</div>'
        +'<div>① 角色音色:<b>'+esc(prof)+'</b>(上方可换)</div>'
        +'<div>② 场景方案:<b>'+esc(scene)+'</b>(按你的设备环境选)</div>'
        +'<div>③ 点右上角 <b>▶ 开始</b>;首次建议「开始 ▾ → 通话向导」</div>'
        +'<div class=egs>💡 想先看效果? <a class=egact id=egdemo>播放 30 秒演示</a>(无需麦克风) &nbsp;·&nbsp; 直播出镜:先在主控台「开播」出画面,再点「开始 ▾ → 直播同传」</div>'
        +lastSessionLine();
      const dl=g1.querySelector('#egdemo'); if(dl) dl.onclick=()=>{ const d=$('#demo'); if(d) d.click(); };
    }
  }
  const g2=mk(lot,'eguide2');
  if(g2) g2.textContent=(running||demoRunning)?'等待对方开口…':'开始后,对方说的外语会在这里实时变成中文字幕';
}
const rowByTurn={};        // 服务端 turn_id → {row,srcCol,dstCol}(同一轮成段)
const spanByUid={};        // uid → {src,dst}(子句级回填)
// 自动滚动跟随：开=新句到达两列各自滚到底；用户在某列上滑看历史(离底>160px)→整体暂停跟随,
// 亮"回到最新"；点按钮恢复。每次打开页面/新会话恢复跟随(不跨会话记忆"关"——
// 否则一次误触上滑后,之后每场同传字幕都不动,像坏了一样)。
let autoFollow=true;
let userScrolling=false;          // 仅真实手势(滚轮/触摸)后短窗内才允许判定"用户上滑"
function scrollList(listId){ const el=$(listId); if(!autoFollow||!el) return;
  el.scrollTop=el.scrollHeight;
  // GER纠错/字体加载等会在本帧后再改行高 → 下一帧补滚一次，确保真正贴底
  requestAnimationFrame(()=>{ if(autoFollow) el.scrollTop=el.scrollHeight; }); }
function setFollow(on){
  autoFollow=on;
  const b=$('#followBtn'); if(b){ b.classList.toggle('paused',!on);
    b.textContent=on?'📜 自动滚动·开':'⬇ 回到最新'; }
  if(on){ scrollList('#lme'); scrollList('#lot'); }
}
addEventListener('DOMContentLoaded',()=>{
  const b=document.createElement('button'); b.id='followBtn';
  b.setAttribute('data-tip','自动滚动|新字幕来了自动滚到最新;你上滑看历史会自动暂停|点击可手动恢复跟随');
  b.onclick=()=>setFollow(!autoFollow); document.body.appendChild(b);
  setFollow(true);
  ['#lme','#lot'].forEach(id=>{
    const el=$(id); if(!el) return; let t=null, g=null;
    const gesture=()=>{ userScrolling=true; clearTimeout(g); g=setTimeout(()=>userScrolling=false,900); };
    el.addEventListener('wheel',gesture,{passive:true});
    el.addEventListener('touchmove',gesture,{passive:true});
    el.addEventListener('scroll',()=>{ clearTimeout(t); t=setTimeout(()=>{
      const gap=el.scrollHeight-el.scrollTop-el.clientHeight;
      if(gap>160&&autoFollow&&userScrolling) setFollow(false);   // 程序滚动/布局变化不误触发暂停
      else if(gap<=40&&!autoFollow&&userScrolling) setFollow(true); // 手动拉回底部 → 自动恢复跟随(聊天软件惯例)
    },120); },{passive:true});
  });
});
let fsOn=false;            // 大字幕全屏开关
let lastMeRow=null, lastOtRow=null;   // 各列最新行(用于高亮)
function markLatest(row,col){
  if(col==='me'){ if(lastMeRow&&lastMeRow!==row)lastMeRow.classList.remove('latest'); row.classList.add('latest'); lastMeRow=row; }
  else { if(lastOtRow&&lastOtRow!==row)lastOtRow.classList.remove('latest'); row.classList.add('latest'); lastOtRow=row; }
}
function renderFS(){           // 大字幕:每列取最近3句,最新句最大,旧句淡化缩小
  [['me','#lme','我 · 中文'],['ot','#lot','对方 · 中文']].forEach(([c,listId,lbl])=>{
    const rows=[...document.querySelectorAll(listId+' .row')].slice(-3);
    const box=$('#fs'+c); if(!box) return;
    box.innerHTML='<div class=fl>'+lbl+'</div>'+rows.map((r,i)=>{
      const zh=(r.querySelector('.src')||{}).textContent?.trim()||'';
      const en=(r.querySelector('.dst')||{}).textContent?.trim()||'';
      const old=i<rows.length-1?' old':'';
      return zh?`<div class="fsline${old}">${esc(zh)}${en?'<span class=en>'+esc(en)+'</span>':''}</div>`:'';
    }).join('');
  });
}

async function loadDevices(){
  const d=await (await fetch('/devices')).json();
  const optf=(arr,sel,val)=>arr.map(x=>`<option value="${x.index}" ${x.index==val?'selected':''}>[${x.index}] ${x.name} · ${x.hostapi}</option>`).join('');
  // 「手机麦(无线直连)」置顶为可选项:选它则我的麦走中继 /mic/pcm,免 VB-Cable(默认仍选本机设备)。
  $('#mic').innerHTML='<option value="phone">📱 手机麦(无线直连·免 VB-Cable)</option>'+optf(d.inputs,0,d.defaults.mic);
  $('#cable').innerHTML=optf(d.outputs,0,d.defaults.cable);
  // 对方声来源:输入型设备(立体声混音=抓扬声器对方声 / CABLE Output 等)。立体声混音标注"推荐"。
  $('#loop').innerHTML=d.inputs.map(x=>{
    const isMix=(x.index===d.stereo_mix);
    const tag=isMix?' ✓推荐':'';
    return `<option value="i:${x.index}">[${x.index}] ${x.name} · ${x.hostapi}${tag}</option>`;
  }).join('');
  if(d.defaults.loopback!=null) $('#loop').value='i:'+d.defaults.loopback;
  const lh=$('#loophint');
  if(lh){
    if(d.stereo_mix!=null) lh.innerHTML='<span style="color:var(--ok,#34d399)">已检到立体声混音 ✓</span>';
    else lh.innerHTML='<span style="color:var(--warn,#fbbf24)">未检到「立体声混音」——请在 Windows「声音→录制」里启用后刷新，否则抓不到对方声</span>';
  }
  try{
    // 走本服务同源代理(/hub_profiles)拿瘦身字段：直连 Hub 会被 CORS 拦(历史上下拉一直空着)
    const p=await (await fetch('/hub_profiles')).json();
    // 通话中换声的高频入口:❤收藏最顶、精选真人次之、真人音色库再次、其他角色殿后；
    // 各组内按会话级使用次数(use_n)降序——常用的自己浮上来，新手零配置也顺手。
    const escA=s=>esc(s).replace(/"/g,'&quot;');
    // 无音色角色在"选择时"就标出来(🔇+暗红):选了它同传只有字幕没配音——这是最早的一道门
    const opt=x=>{const nv=(x.has_voice===false);
      return `<option value="${escA(x.name)}" ${x.active?'selected':''} data-novoice="${nv?1:0}"`+
        (nv?' style="color:#f87171" title="该角色无音色样本：同传只有字幕、对方听不到配音"':'')+
        `>${nv?'🔇 ':''}${esc(x.name)}${(x.use_n||0)>1?' 🔥':''}</option>`;};
    const byUse=(a,b)=>(b.use_n||0)-(a.use_n||0)||String(a.name).localeCompare(String(b.name));
    const all=p.profiles.slice().sort(byUse);
    // P0g-2 治理:无音色角色从各组抽出、沉底单列——同传的核心交付是"声",不让它们混在可用声里
    const mute=all.filter(x=>x.has_voice===false);
    const va=all.filter(x=>x.has_voice!==false);
    const fav=va.filter(x=>x.vp_fav), ft=va.filter(x=>x.vp_featured&&!x.vp_fav),
          vp=va.filter(x=>x.voicepack_spk&&!x.vp_featured&&!x.vp_fav),
          rest=va.filter(x=>!x.voicepack_spk);
    $('#profile').innerHTML=(fav.length||ft.length||vp.length||mute.length)
      ? (fav.length?`<optgroup label="❤ 我的收藏">${fav.map(opt).join('')}</optgroup>`:'')
        +(ft.length?`<optgroup label="⭐ 精选真人音色">${ft.map(opt).join('')}</optgroup>`:'')
        +(vp.length?`<optgroup label="🧬 真人音色库">${vp.map(opt).join('')}</optgroup>`:'')
        +(rest.length?`<optgroup label="🎭 其他角色">${rest.map(opt).join('')}</optgroup>`:'')
        +(mute.length?`<optgroup label="🔇 无音色（仅字幕，先去配音色）">${mute.map(opt).join('')}</optgroup>`:'')
      : all.map(opt).join('');
  }catch(e){ $('#profile').innerHTML='<option value="">(默认激活角色)</option>'; }
}

function addRow(ev){
  if(ev.who==='sys'){
    if(ev.clear){ $('#lme').innerHTML=''; $('#lot').innerHTML='';
      for(const k in rowByTurn) delete rowByTurn[k];
      for(const k in spanByUid) delete spanByUid[k];
      lastMeRow=null; lastOtRow=null; setFollow(true); syncEmptyGuide(); if(fsOn) renderFS(); return; }
    if(ev.warn){ $('#warn').textContent=ev.warn;
      clearTimeout(window._wt); window._wt=setTimeout(()=>$('#warn').textContent='',6000);
      if(ev.warn.indexOf('演示结束')>=0) setDemoUI(false);
      const sw=$('#swtag');
      if(sw){ if(ev.warn.indexOf('已就绪，下一句')>=0) sw.innerHTML='<span style="color:var(--warn)">就绪，下一句切换</span>';
        else if(ev.warn.indexOf('已切换到角色')>=0){ sw.innerHTML='<span style="color:var(--ok)">✓ 已切换</span>'; setTimeout(()=>{if(sw)sw.innerHTML='';},3000); } } }
    if(ev.live_ready && running){ $('#st').textContent='运行中 · 数字人就绪'; }
    return;
  }
  const col=ev.who==='me'?'me':'other';
  const listId=col==='me'?'#lme':'#lot';
  // 软终判:整轮润色 → 把该轮的逐子句译文替换为一句润色稿
  if(ev.finalize){
    const t=rowByTurn[ev.turn]; if(!t) return;
    if(ev.zh!=null) t.srcCol.innerHTML='<span class="cl fin">'+esc(ev.zh)+'</span>';  // 中文为主
    if(ev.en!=null) t.dstCol.innerHTML='<span class=cl>'+esc(ev.en)+'</span>';        // 英文为辅
    scrollList(listId);                       // 润色稿可能变长,保持贴底
    if(fsOn) renderFS();
    return;
  }
  if(ev.uid==null || ev.turn==null) return;
  // P-Affirm 声纹拦截灰行：显示被拦原因 + 一键「是我·放行并学习」(误拦不再是黑洞)
  if(ev.blocked==='spk'){
    const row=document.createElement('div'); row.className='row me vlblock';
    const txt=ev.text?esc(ev.text):('🎤 '+(ev.dur!=null?ev.dur:'?')+'s 语音(拦在识别前)');
    row.innerHTML='<div class=src style="opacity:.5">'+txt+'</div>'
      +'<div class=dst><span style="color:#f87171;font-size:12px">🚫 声纹拦截 '
      +(ev.sim!=null?ev.sim:'—')+' / 门限 '+(ev.thr!=null?ev.thr:'—')+'</span> '
      +'<button class=affirmbtn data-uid="'+ev.uid+'" onclick="vlAffirm(this)">是我 · 放行并学习</button></div>'
      +'<div class=meta></div>';
    $('#lme').appendChild(row);
    markLatest(row,'me'); scrollList('#lme'); if(fsOn) renderFS();
    return;
  }
  // 流式撤回：该句经门控判为底噪/幻听 → 删除占位行
  if(ev.retract){
    const t=rowByTurn[ev.turn];
    if(t){ const r=t.row; r.style.transition='opacity .25s,transform .25s';
      r.style.opacity='0'; r.style.transform='translateX(-8px)';
      setTimeout(()=>r.remove(),250); delete rowByTurn[ev.turn]; }
    delete spanByUid[ev.uid];
    if(fsOn) renderFS(); return;
  }
  let t=rowByTurn[ev.turn];
  if(!t){                                              // 新一轮 → 新行
    const row=document.createElement('div'); row.className='row '+col;
    row.innerHTML='<div class=src></div><div class=dst></div><div class=meta></div>';
    $(listId).appendChild(row);
    t={row, srcCol:row.querySelector('.src'), dstCol:row.querySelector('.dst')};
    rowByTurn[ev.turn]=t;
  }
  // 流式 partial：整句逐词刷新主行(占位、斜体淡化)，定稿前不分子句 span
  if(ev.live!=null){
    t.srcCol.innerHTML='<span class="cl pend">'+esc(ev.live)+'</span>';
    t.row.classList.add('pending');
    markLatest(t.row, col==='me'?'me':'ot');
    scrollList(listId); if(fsOn) renderFS();
    return;
  }
  // 流式定稿：清掉 partial 占位，改为正式 span
  if(t.row.classList.contains('pending')){
    t.srcCol.innerHTML=''; t.dstCol.innerHTML='';
    t.row.classList.remove('pending');
    delete spanByUid[ev.uid];
  }
  let sp=spanByUid[ev.uid];
  if(!sp){                                             // 同轮新子句 → 追加子 span(成段)
    const s=document.createElement('span'); s.className='cl';
    const d=document.createElement('span'); d.className='cl';
    t.srcCol.appendChild(s); t.dstCol.appendChild(d);
    sp={src:s, dst:d}; spanByUid[ev.uid]=sp;
  }
  if(ev.zh) sp.src.textContent=ev.zh+' ';              // 中文为主(大)
  if(ev.en) sp.dst.textContent=ev.en+' ';              // 英文为辅(小)
  if(ev.ger){ sp.src.classList.remove('gerfix'); void sp.src.offsetWidth; sp.src.classList.add('gerfix'); }  // GER纠错闪蓝提示
  if(ev.ms) t.row.querySelector('.meta').textContent=ev.ms+' ms';
  if(ev.emo){                                          // P6.3 情感徽章(挂 meta 行,GER 重写正文不冲掉)
    const EMOB={excited:['🔥','兴奋','#fb923c'],angry:['😠','生气','#f87171'],sad:['😢','难过','#93c5fd'],
                surprised:['😮','惊讶','#fbbf24'],happy:['😊','开心','#4ade80'],fearful:['😨','害怕','#c4b5fd'],
                gentle:['🌸','温柔','#f9a8d4'],calm:['🍃','平静','#86efac'],serious:['📌','严肃','#94a3b8'],
                disgusted:['😒','厌恶','#a3a3a3']};
    const b=EMOB[ev.emo]||['🎭',ev.emo,'#a78bfa'];
    const meta=t.row.querySelector('.meta');
    let eb=meta.querySelector('.emob');
    if(!eb){ eb=document.createElement('span'); eb.className='emob'; meta.appendChild(eb); }
    eb.textContent=b[0]+' '+b[1]+(ev.emo_w?(' ·'+ev.emo_w):'');
    eb.style.color=b[2]; eb.style.borderColor=b[2]+'66';
    eb.title='本句用情感配音: '+b[1]+(ev.emo_w?('\n强度 style_weight='+ev.emo_w+' (0.9轻/1.3中/1.8强)'):'');
  }
  markLatest(t.row, col==='me'?'me':'ot');
  scrollList(listId);
  if(fsOn) renderFS();
}

function connect(){
  es=new EventSource('/events?since='+lastId);
  es.onmessage=e=>{ const ev=JSON.parse(e.data); lastId=ev.id; addRow(ev); syncEmptyGuide(); };
}

async function vlAffirm(btn){                          // P-Affirm 一键放行:学声纹+补跑翻译配音
  btn.disabled=true; btn.textContent='处理中…';
  try{
    const r=await (await fetch('/voicelock/affirm',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({uid:+btn.dataset.uid})})).json();
    if(r.ok){
      btn.textContent=r.replayed?'✓ 已放行·声纹已学习':'✓ 声纹已学习';
      btn.style.color='#4ade80'; btn.style.borderColor='#4ade8066';
      const row=btn.closest('.row'); if(row) row.style.opacity='.45';
    }else{
      btn.textContent=(r.detail&&r.detail.indexOf('过期')>=0)?'已过期·请重说':'失败·重试';
      btn.disabled=false;
    }
  }catch(e){ btn.disabled=false; btn.textContent='失败·重试'; }
}

const f=v=>v==null?'—':(v.median+'/'+v.p90+'ms');     // 中位/p90
function ahPill(h){                                    // 音频健康指示灯(绿=正常/黄=偏吵已自适应)
  if(!h||h.level==='idle') return '';
  const noisy=h.level==='noisy', col=noisy?'#fbbf24':'#34d399';
  const label=noisy?'🎙 偏吵·已自动收紧':'🎙 音频正常';
  const d=h.drops||{}, nf=h.noise_dbfs||{}, g=h.gates||{}, gr=h.ger||{};
  const tip=`自适应噪声门控\n`+
    `噪声底  我=${nf.a==null?'—':nf.a+'dBFS'}  对方=${nf.b==null?'—':nf.b+'dBFS'}\n`+
    `生效门  我 rms≥${g.a?g.a.rms:'—'}/peak≥${g.a?g.a.peak:'—'}/dyn≥${g.a&&g.a.dyn!=null?g.a.dyn:'—'}  对方 rms≥${g.b?g.b.rms:'—'}/peak≥${g.b?g.b.peak:'—'}/dyn≥${g.b&&g.b.dyn!=null?g.b.dyn:'—'}\n`+
    `拦截幻听  门控${d.gate||0} · 套话${d.halluc||0} · 弱填充${d.filler||0} · 低置信${d.lowconf||0} · 连刷${d.dedup||0} · 声纹拦${d.spk||0}\n`+
    `终稿复核  复核${gr.checked||0} · 字幕纠错${gr.fixed||0} · 误杀救回${gr.revived||0} · 存疑撤回${gr.vetoed||0}`+
    ((gr.garbage||0)?`\n垃圾闸拦  ${gr.garbage} 句(仲裁模型输出全角/乱码被拒,占比高=该换模型/调提示词)`:``)+
    ((gr.overfix||0)?`\n过度纠错拦  ${gr.overfix} 句(改字过多如整句语义漂移,拼音闸漏网)`:``)+
    ((h.mt&&h.mt.llm_reject)?`\n译文健全拦  ${h.mt.llm_reject} 句(LLM 译文凭空乱码/ASCII,已回退 NMT)`:``);
  const fixes=(gr.fixed||0)+(gr.revived||0);
  return ` · <span title="${tip}" style="color:${col};font-weight:700;cursor:help">${label}</span>`+
    (h.drops_total?` <span style="opacity:.6">(拦${h.drops_total})</span>`:'')+
    (fixes?` <span style="opacity:.6;color:#93c5fd" title="GER 终稿复核已纠错/救回句数">(纠${fixes})</span>`:'');
}
function vlPill(v){                                    // 声纹锁指示(锁定=只翻译注册人;注册中=自动累积;影子=底座失真降级放行)
  if(!v||!v.enabled||v.model_ok===false) return '';
  if(v.shadow){
    const tip=`声纹底座疑似失真(连续拦截且零放行)，已临时放行保出声\n影子放行 ${v.shadow_passes||0} 句 · 门限 ${v.thr} · 最近相似度 ${v.last_sim==null?'—':v.last_sim}\n正常说几句话会自动重建声纹；或点重置声纹立即重录`;
    return ` · <span title="${tip}" style="color:#fbbf24;font-weight:700;cursor:help">🟡 声纹影子·放行中</span>`;
  }
  if(v.enrolled){
    const tip=`声纹锁已激活：只翻译注册说话人\n门限 ${v.thr} · 最近相似度 ${v.last_sim==null?'—':v.last_sim}\n放行 ${v.accepts||0} 句 · 拦截 ${v.rejects||0} 句\n换人使用请点重置(voicelock/reset)`;
    return ` · <span title="${tip}" style="color:#86efac;font-weight:700;cursor:help">🔒 声纹锁定</span>`;
  }
  const n=(v.pending||0);
  return ` · <span title="开口说 3 句真话即自动注册你的声纹(之后旁人声/键盘噪自动拦截)" style="color:#fcd34d;cursor:help">🔓 声纹注册中 ${n}/3</span>`;
}
async function pollMetrics(){
  try{
    const m=await (await fetch('/metrics')).json();
    // 会话在跑而本页不知道(中途刷新/别处开的)→对齐真相,运行态 UI(主按钮/生效键)才不失真
    if(m.running===true && !running && !demoRunning){ running=true; syncMainBtn(); syncCtx(); syncEmptyGuide(); syncLangUI(); }
    const live=m.live_mode;
    const ls=$('#livestat');
    if(live){
      ls.innerHTML=
        `<span class="tag ${m.hub_ok?'on':'off'}">Hub</span>`+
        `<span class="tag ${m.face_ready?'on':'wait'}">${m.face_ready?'人脸✓':'预热…'}</span>`+
        `<span class="tag ${m.vcam_playing?'on':''}">${m.vcam_playing?'出画中':'vcam'}</span>`+
        (m.degraded?`<span class="tag off">降级·配音/字幕</span>`:'')+
        (m.switching?`<span class="tag wait">切换准备中</span>`:'')+
        (m.gpu_alert?`<span class="tag" style="border-color:#ef4444;background:rgba(239,68,68,.18);color:#fecaca;font-weight:700" title="显卡持续被其他程序占用，口型可能滞后；强烈建议关闭占卡程序或独占显卡">🔥 建议独占显卡</span>`:(m.gpu_contended?`<span class="tag" style="border-color:rgba(248,113,113,.5);color:#fca5a5" title="显卡疑似被其他程序占用，口型可能滞后；建议独占显卡">GPU争用</span>`:''))+
        ((m.av_offset_ms!=null||m.av_drift_ms!=null)?`<span class="tag" style="border-color:rgba(74,222,128,.5);color:#86efac" title="音画对齐：首帧偏移=声音起→首帧出画；全程漂移=整句末画面落后声音(越小越贴合)">A/V ${m.av_offset_ms!=null?m.av_offset_ms+'ms':'—'}${m.av_drift_ms!=null?' · 漂移'+Math.round(m.av_drift_ms)+'ms':''}</span>`:'');
      $('#spark2wrap').style.display='inline';
      const pl=$('#preload'); pl.style.display=running?'inline':'none';
      const pr=(m.preset_ready||[]), pq=(m.preset_queue||[]);
      if(m.preset_loading) pl.textContent='预载中'+(pq.length?' 剩'+pq.length:'…');
      else if(pr.length){ pl.textContent='已入池 '+pr.length; }
      else pl.textContent='预载常用';
      // P5: 预载进度明细——谁已秒切就绪、谁还在排队,不用翻事件流猜
      const ps=$('#pstat'), cut=a=>a.slice(0,3).join('、')+(a.length>3?' 等'+a.length+'个':'');
      if(running&&(pr.length||pq.length)){
        ps.style.display='block';
        ps.innerHTML=(pr.length?('<span style="color:var(--ok)">✓ 秒切就绪:</span> '+esc(cut(pr))):'')+
                     (pq.length?((pr.length?' &nbsp;·&nbsp; ':'')+'<span style="color:var(--warn)">⏳ 预载排队:</span> '+esc(cut(pq))):'');
      } else ps.style.display='none';
      const sw=$('#swstat'); let s='';
      if(m.switch_count) s+=`<b>切换</b> ${m.switch_count} `;
      if(m.degrade_count) s+=`<span class="${m.degraded?'bad':'hi'}"><b>降级</b> ${m.degrade_count}次/${(m.degrade_ms/1000).toFixed(1)}s</span>`;
      sw.innerHTML=s;
    } else { ls.innerHTML=''; $('#swstat').innerHTML=''; $('#preload').style.display='none'; $('#pstat').style.display='none'; $('#spark2wrap').style.display='none'; }
    if(running){
      const bl=m.backlog_now||0;
      const blc=bl>=6?'bad':(bl>=3?'hi':'');
      const ss=m.stream||{};
      const ar=m.asr_route||{};
      const arWhy=(ar.why||'').replace(/"/g,'&quot;');
      const streamPill = m.stream_on ? (
        `<span class="tag on" style="cursor:help" title="Nemotron 流式逐词字幕${arWhy?'&#10;'+arWhy:''}&#10;逐词刷新 ${ss.part||0} · 有效定稿 ${ss.fin||0} · 粒度 ${ss.part_per_fin||0} 词/句">⚡逐词流式 ${ss.part_rate||0}/s</span> `+
        (ss.yields?`<span class="tag" style="border-color:rgba(74,222,128,.5);color:#86efac;cursor:help" title="口型生成期暂停方向A上行、让 lipsync 独占 GPU 的次数">让位 ${ss.yields}</span> `:``)+
        (ss.asr_unloaded?`<span class="tag" style="border-color:rgba(96,165,250,.5);color:#93c5fd;cursor:help" title="流式下 Whisper 已卸载,显存让给口型/TTS(分段时自动重载)">Whisper已卸</span> `:``)
      ) : (
        // B-5: 分段模式也常驻标出 ASR 引擎+原因(弱语种自动回退不再只是开播闪过的一条事件)
        `<span class="tag" style="border-color:rgba(148,163,184,.5);color:#cbd5e1;cursor:help" title="${arWhy||'整句分段转写(Whisper)'}">ASR·${ar.label||'Whisper·分段'}</span> `
      );
      // 无音色常驻徽章(与 GPU 争用同级醒目)：事件会滚走,这枚整场钉在观测条上
      const novoicePill=(m.voice_ok===false)?`<span class="tag" style="border-color:#ef4444;background:rgba(239,68,68,.18);color:#fecaca;font-weight:700;cursor:help" title="当前角色没有音色样本：克隆配音已全程跳过(本场已跳过 ${m.novoice_skips||0} 句)，对方只能看字幕、听不到声音。&#10;请切换有音色的角色，或在角色库为它录制/导入音色后重新开始">🔇 无音色·仅字幕</span> `:'';
      // P0-R 语义块提前配音徽章:committed=提前出声块数(负首音=定稿前已开口)
      const cd=m.chunk_dub||{};
      const chunkPill=(cd.on&&(cd.committed||cd.tail||cd.prefetch))?
        `<span class="tag on" style="cursor:help" title="语义块流式:子句边界提前翻译(通话另提前出声),不等整句定稿&#10;提前块 ${cd.committed||0} · 直播预取 ${cd.prefetch||0} · 尾段补 ${cd.tail||0} · 预检退回 ${cd.blocked||0} · 定稿不一致 ${cd.mismatch||0}">⚡块 ${(cd.committed||0)+(cd.prefetch||0)}</span> `:'';
      // P1 流式 LLM 翻译(边译边配)+垫场气口徽章
      const lm=m.llm_stream||{};
      const llmsPill=(lm.on&&(lm.used||lm.filler))?
        `<span class="tag on" style="cursor:help" title="流式LLM翻译:译文token到子句边界即送TTS,不等整句译完&#10;流译句 ${lm.used||0}(共 ${lm.segs||0} 段) · 段闸拒 ${lm.bail||0} · 垫场气口 ${lm.filler||0} 次">🌊流译 ${lm.used||0}</span> `:'';
      $('#mtxt').innerHTML=
        novoicePill+streamPill+chunkPill+llmsPill+
        `<b>ASR</b> ${f(m.asr_ms)} · <b>NMT</b> ${f(m.nmt_ms)} · `+
        (!live&&m.tts_first_ms?`<b>首音</b> <span style="cursor:help" title="定稿→首块配音入播放队列(中位);负值=靠语义块在定稿前已出声">${f(m.tts_first_ms)}</span> · `:'')+
        (live?`<b>TTFV</b> ${f(m.ttfv_ms)} · <b>段间隔</b> ${f(m.seg_gap_ms)} · `:'')+
        (live?`<b>口型</b> ${f(m.avatar_ms||m.synth_ms)} · `:'')+
        `<b>端到端</b> ${f(m.e2e_ms)} · `+
        `<b>积压</b> <span class="${blc}">${bl}</span> · <b>丢弃</b> <span class="${m.dropped?'bad':''}">${m.dropped}</span> · `+
        `<b>句数</b> 我${(m.counts&&m.counts.a)||0}/对方${(m.counts&&m.counts.b)||0}`+
        ahPill(m.audio_health)+vlPill(m.voicelock)+
        (m.muted?` · <span style="color:#f87171;font-weight:800">⛔ 急停中(克隆音已切断)</span>`:'');
      syncP1Buttons(m);
      drawSpark(m.recent_e2e||[], '#spark', '#4f7aff');
      if(live) drawSpark((m.recent_ttfv&&m.recent_ttfv.length?m.recent_ttfv:m.recent_avatar)||[], '#spark2', '#34d399');
      if(live && running){
        let st='运行中';
        if(!m.face_ready) st='运行中 · 人脸预热中';
        else if(m.vcam_playing) st='运行中 · 口型生成中';
        else st='运行中 · 数字人就绪';
        if($('#st').textContent.startsWith('运行中')) $('#st').textContent=st;
      }
    } else { $('#mtxt').textContent= demoRunning?'演示中…':'未运行'; drawSpark([], '#spark'); drawSpark([], '#spark2'); }
    if(live && m.cam_ready===false && m.cam_error){       // OBS 虚拟摄像头未就绪 → 常驻红字提示
      $('#warn').innerHTML='⚠ '+esc(m.cam_error); }
    syncSceneFromMetrics(m);   // P8: 双工徽章/冲突条/方案态(运行与否都刷新)
    syncEmptyGuide();          // 空状态引导兜底刷新(角色/方案变化 1.5s 内跟上)
  }catch(e){ if(running) $('#mtxt').textContent='观测取数失败'; }
  setTimeout(pollMetrics, 1500);
}

function drawSpark(vals, sel, color){
  const c=$(sel); if(!c) return;
  const x=c.getContext('2d'), W=c.width, H=c.height;
  x.clearRect(0,0,W,H);
  if(!vals||!vals.length) return;
  const mx=Math.max(...vals,1), n=vals.length, dx=W/Math.max(n-1,1);
  x.beginPath();
  vals.forEach((v,i)=>{ const px=i*dx, py=H-2-(v/mx)*(H-4); i?x.lineTo(px,py):x.moveTo(px,py); });
  x.strokeStyle=color||'#4f7aff'; x.lineWidth=1.5; x.stroke();
}

// ── P8 场景方案卡：三预设切换 / 设备状态灯 / 双工徽章 / 耦合实测 / 冲突提示 ──
let scActive='', scSwitching=false, lastConflictSig=null;
async function loadScene(){
  try{
    const r=await (await fetch('/audio_profile')).json();
    if(!r.ok) return;
    scActive=r.active;
    // 方案 chip 不再渲染 emoji 图标(p.icon)：选中态实心胶囊已足够辨识,首屏少一排彩色噪点
    $('#scChips').innerHTML=Object.entries(r.profiles).map(([k,p])=>
      `<span class="sc${k===scActive?' on':''}" data-k="${k}" data-tip="${esc(p.label||k)}|${esc(p.desc||'')}|点击切换;会话中切换将于下次开播生效">${esc(p.label||k)}</span>`).join('');
    document.querySelectorAll('#scChips .sc').forEach(el=>el.onclick=()=>switchScene(el.dataset.k));
    renderLegs(r.resolved&&r.resolved.legs);
    renderDup(r.half_duplex_now, r.coupling);
    if(r.note) $('#scMsg').textContent=r.note;
  }catch(e){}
}
function renderLegs(legs){
  if(!legs) return;
  const set=(id,tid,leg,label)=>{ const el=$(id); if(!el||!leg) return;
    el.className='chip '+(leg.ok?'ok':'bad');
    el.setAttribute('data-tip',label+'状态|'+((leg.name||'未解析')+(leg.note?(' · '+leg.note):''))
      +'|红灯=设备没找到:换个场景方案,或去 ⚙高级设置 手动指定');
    $(tid).textContent=label+(leg.name?('·'+leg.name.split(' (')[0].slice(0,10)):'');
  };
  set('#scMic','#scMicT',legs.mic,'麦');
  set('#scListen','#scListenT',legs.listen,'监听');
  set('#scDub','#scDubT',legs.dub_out,'虚拟麦');
}
function renderDup(hd, cp){
  const el=$('#scDup'); if(!el) return;
  el.style.display='';
  el.className='dup '+(hd?'half':'full');
  el.textContent=hd?'⇅ 轮流说话(半双工)':'⇄ 全双工';
  el.setAttribute('data-tip', hd
    ? '轮流说话(半双工)|'+((cp&&cp.detail)||'实测扬声器↔麦互通:同一时间只翻一方,防回声自激')+'|想同时说话:戴上耳机后点「测回声」重测'
    : '全双工|'+((cp&&cp.detail)||'双方可同时说话,链路声学隔离良好')+'|开播自检自动实测;点「测回声」可随时复测');
}
async function switchScene(k){
  if(scSwitching||k===scActive) return;
  scSwitching=true;
  try{
    const r=await (await fetch('/audio_profile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({active:k})})).json();
    if(r.ok){
      scActive=r.active;
      const p=(r.profiles&&r.profiles[r.active])||{};
      // 手机方案→「我的麦」下拉同步切手机(下次手动开始也一致)；其它方案切回首个实体麦
      const micSel=$('#mic');
      if(p.mic==='phone'&&micSel.querySelector('option[value=phone]')) micSel.value='phone';
      else if(p.mic!=='phone'&&micSel.value==='phone'){
        const o=[...micSel.options].find(o=>o.value!=='phone'&&(o.textContent||'').includes(p.mic||''));
        if(o) micSel.value=o.value;
      }
      loadScene();
      if(r.running) $('#scMsg').textContent='已保存，重新点「一键通话」生效';
    }
  }catch(e){}
  scSwitching=false;
}
$('#scProbe').onclick=async()=>{
  const b=$('#scProbe'); b.disabled=true; b.innerHTML=IC('probe')+' 测试中…';
  try{
    const r=await (await fetch('/coupling_probe',{method:'POST'})).json();
    // 情境指引:测出互通不只报结果,直接给"想全双工该怎么做"
    $('#scMsg').textContent=r.ok
      ? (r.detail+(r.coupled?'；已自动改轮流说话防回声——想同时说话:戴上耳机再测一次':''))
      : ('探测失败: '+(r.detail||''));
    if(r.ok) renderDup(r.coupled, r);
  }catch(e){ $('#scMsg').textContent='探测异常'; }
  b.disabled=false; b.innerHTML=IC('probe')+' 测回声';
};
$('#scDubTest').onclick=async()=>{
  const b=$('#scDubTest'); b.disabled=true; b.innerHTML=IC('zap')+' 试音中…';
  $('#scMsg').textContent='正在用你的克隆音色合成一句、推给对方收音口…';
  try{
    const r=await (await fetch('/call_mode/dub_test',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({profile:$('#profile').value||''})})).json();
    $('#scMsg').textContent=(r.ok?'✅ ':'⚠ ')+(r.reason||'试音完成');
  }catch(e){ $('#scMsg').textContent='试音异常: '+e; }
  b.disabled=false; b.innerHTML=IC('zap')+' 试音'; setTimeout(dcEnginePoll,300);
};
// 一键修复对方声来源(自听同族→强制立体声混音重跑)；仅在自检检出可修复时显示
function showFixLoop(on){ const b=$('#scFixLoop'); if(b) b.style.display=on?'':'none'; }
$('#scFixLoop').onclick=async()=>{
  const b=$('#scFixLoop'); b.disabled=true; b.innerHTML=IC('tools')+' 修复中…';
  $('#scMsg').textContent='正在改用「立体声混音」抓对方声并重启采集链…';
  try{
    const r=await (await fetch('/call_mode/fix_loopback',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({profile:$('#profile').value||''})})).json();
    $('#scMsg').textContent=(r.fix_applied?'✅ ':'⚠ ')+(r.fix_note||'已尝试修复');
    showFixLoop(!!r.self_hear_fixable);
    if(r.session_running){ running=true; callModeActive=true; $('#dot').className='on';
      $('#st').textContent='运行中 · 通话模式'; syncMainBtn&&syncMainBtn(); syncCtx&&syncCtx(); markApplied(); }
  }catch(e){ $('#scMsg').textContent='修复异常: '+e; }
  b.disabled=false; b.innerHTML=IC('tools')+' 修复对方声来源';
};
// 配音引擎体检 + 最近试音：轮询点亮引擎状态灯/常驻最近一次试音峰值,主引擎挂了露出「拉起配音引擎」
function fmtAge(s){ if(s==null) return ''; if(s<60) return Math.round(s)+'秒前'; if(s<3600) return Math.round(s/60)+'分前'; return Math.round(s/3600)+'时前'; }
async function dcEnginePoll(){
  try{
    const d=await (await fetch('/tts/engines_health',{cache:'no-store'})).json();
    const chip=$('#scEngineChip'), dot=chip.querySelector('i');
    chip.style.display=''; chip.title=d.advice||'';
    if(d.primary_ok && d.slow_primary){ dot.style.background='#fbbf24'; $('#scEngineT').textContent='引擎:'+(d.primary||'')+' 偏慢~'+Math.round((d.primary_latency_ms||0)/100)/10+'s'; }
    else if(d.primary_ok){ dot.style.background='#34d399'; $('#scEngineT').textContent='引擎:'+(d.primary||'')+' 在线'; }
    else if(d.any_alive){ dot.style.background='#fbbf24'; $('#scEngineT').textContent='主引擎'+(d.primary||'')+'掉线·走兜底'; }
    else { dot.style.background='#ef4444'; $('#scEngineT').textContent='引擎全挂'; }
    $('#scEngineFix').style.display=(d.can_launch&&d.can_launch.length)?'':'none';
    if(d.advice && !$('#scMsg').textContent) $('#scMsg').textContent='💡 '+d.advice;
    const ld=d.last_dub||{};
    if(!ld.never && ld.peak_dbfs!=null){ const c=$('#scLastDub'); c.style.display=''; c.querySelector('i').style.background=ld.ok?'#34d399':'#ef4444';
      $('#scLastDubT').textContent='上次试音 '+ld.peak_dbfs+'dBFS'+(ld.synth_ms?' · 合成'+Math.round(ld.synth_ms/100)/10+'s':'')+' · '+fmtAge(ld.age_sec); }
  }catch(e){ }
}
$('#scEngineFix').onclick=async()=>{
  const b=$('#scEngineFix'); b.disabled=true; b.innerHTML=IC('tools')+' 拉起中…';
  $('#scMsg').textContent='正在经主控台把配音主引擎重新拉起(模型加载可能十几秒)…';
  try{
    const r=await (await fetch('/tts/engine_restart',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();
    $('#scMsg').textContent=(r.ok?'✅ ':'⚠ ')+(r.reason||'已请求拉起');
  }catch(e){ $('#scMsg').textContent='拉起异常: '+e; }
  b.disabled=false; b.innerHTML=IC('tools')+' 拉起配音引擎'; setTimeout(dcEnginePoll,1500);
};
setInterval(dcEnginePoll, 20000); setTimeout(dcEnginePoll, 1500);
function renderConflicts(list){
  const bar=$('#conflictbar'); if(!bar) return;
  const sig=JSON.stringify((list||[]).map(i=>i.code));
  if(sig===lastConflictSig) return;
  lastConflictSig=sig;
  if(!list||!list.length){ bar.style.display='none'; return; }
  bar.className='conflictbar'+(list.some(i=>i.level==='red')?'':' yellow');
  bar.innerHTML=list.map(i=>`${i.level==='red'?'🔴':'🟡'} ${esc(i.msg)} <b style="opacity:.9">→ ${esc(i.fix)}</b>`).join('<br>');
  bar.style.display='';
}
function syncSceneFromMetrics(m){
  if(typeof m.half_duplex==='boolean') renderDup(m.half_duplex, m.coupling);
  renderConflicts(m.conflicts||[]);
  if(m.audio_profile&&m.audio_profile!==scActive&&!scSwitching){ scActive=m.audio_profile; loadScene(); }
}
loadScene();

const HINTS={
  call:'通话模式：通话App「麦克风」设为 <b>CABLE Output (VB-Audio Virtual Cable)</b>；扬声器设为你在「对方声来源」(高级设置内)选的设备（或选"立体声混音/环回"抓对方声）。',
  live:'直播模式：你说中文 → 数字人用克隆英文开口(底部字幕)；对方英文 → 顶部字幕。启动时自动激活所选角色并同步虚拟摄像头待机。直播软件「摄像头」选 <b>OBS Virtual Camera</b>。<span id=idlehint></span>'};
function applyHint(){
  const live=$('#omode').value==='live';
  $('#hint').innerHTML=HINTS[$('#omode').value]||HINTS.call;
  const fc=$('#fcable'); if(fc){ fc.style.opacity=live?'0.4':'1'; fc.title=live?'直播模式下输出走数字人虚拟摄像头，无需虚拟麦':''; }
  $('#cable').disabled=live;
  const sf=$('#stream'); if(sf){ sf.disabled=false; sf.parentElement.style.opacity='1';
    sf.parentElement.title=live?'直播模式:逐词只刷字幕(网页/虚拟摄像头顶部)，口型/配音仍按整句':''; }
}
async function refreshIdleHint(){
  try{
    const m=await (await fetch('/metrics')).json();
    const el=$('#idlehint'); if(!el) return;
    el.innerHTML=m.idle_video? ' <b class=ok>🎬 真人待机已启用</b>':'';
  }catch(e){}
}
$('#omode').onchange=applyHint; applyHint();

// 运行中切换角色:不停会话,后台准备就绪后下一句无缝切换
$('#profile').addEventListener('change', async ()=>{
  // 选到无音色角色立刻当面提醒(未运行也提示;运行中后台 _build_switch 还会推事件)，附直达修复入口
  const _o=$('#profile').selectedOptions[0];
  if(_o && _o.dataset.novoice==='1'){
    const w=$('#warn');
    if(w){ w.innerHTML='🔇 该角色无音色样本：同传将只有字幕、对方听不到配音。可换角色，或 '
        +'<a href="__HUB_BASE__/ui#profiles" target="_blank" style="color:#93c5fd;text-decoration:underline">去角色库配声音 ↗</a>（找到该角色点「🎤 配声音」，一分钟补齐）';
      clearTimeout(window._wt); window._wt=setTimeout(()=>{w.textContent='';},15000); }
  }
  if(!running) return;                       // 未运行:仅作为下次启动的选择
  const nm=$('#profile').value||'';
  if(!nm) return;
  $('#swtag').innerHTML='<span style="color:var(--warn)">切换准备中…</span>';
  try{
    const r=await (await fetch('/switch_profile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({profile:nm})})).json();
    if(r.busy) $('#swtag').innerHTML='<span style="color:var(--warn)">上个切换进行中…</span>';
  }catch(e){ $('#swtag').innerHTML='<span style="color:var(--danger)">切换失败</span>'; }
});

// 预载常用角色:只送台账 Top5(按 use_n)，不全量预载——省 GPU/显存，常用的才值得提前准备
$('#preload').addEventListener('click', async ()=>{
  const cur=$('#profile').value||'';
  $('#preload').textContent='📦 预载中…';
  try{
    const p=await (await fetch('/hub_profiles')).json();
    const byUse=(a,b)=>(b.use_n||0)-(a.use_n||0);
    const names=p.profiles.slice().sort(byUse)
      .filter(x=>x.name && x.name!==cur && (x.use_n||0)>0)
      .slice(0,5).map(x=>x.name);
    if(!names.length){
      $('#preload').textContent='暂无常用';
      setTimeout(()=>{ if(running) $('#preload').textContent='预载常用'; }, 2500);
      return;
    }
    const r=await (await fetch('/preload',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({profiles:names})})).json();
    if(r.busy) $('#preload').textContent='预载中…';
    else{
      const tip=names.join('、');
      $('#preload').textContent='已排队 '+names.length;
      $('#preload').title='预载: '+tip;
    }
  }catch(e){ $('#preload').textContent='预载常用'; }
});

async function doStart(){
  const lv=$('#loop').value.split(':');
  const micPhone = $('#mic').value==='phone';   // 手机麦无线直连:免本机声卡/VB-Cable
  const body={mic_index: micPhone? -1 : +$('#mic').value, cable_index:+$('#cable').value,
    loopback_index:+lv[1], loopback_is_output: lv[0]==='o', profile:$('#profile').value||'',
    mode:$('#mode').value, live_mode: $('#omode').value==='live',
    stream: $('#stream').value==='1',
    mic_net_url: micPhone? ('http://'+(location.hostname||'127.0.0.1')+':__MON_PORT__/mic/pcm') : ''};
  for(const k in spanByUid) delete spanByUid[k];
  for(const k in rowByTurn) delete rowByTurn[k];
  // 启动要数秒(拉音色参考/开两路采集流):立即给「启动中」反馈;失败当面报错——不再静默无反应
  const b=$('#btn'); b.disabled=true; b.textContent='⏳ 启动中…'; $('#st').textContent='启动中…';
  let r;
  try{
    const resp=await fetch('/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    r=await resp.json().catch(()=>({}));
    if(!resp.ok||!r.ok){
      const d=r.detail!==undefined?r.detail:(r.error!==undefined?r.error:('HTTP '+resp.status));
      const why=(typeof d==='string')?d:JSON.stringify(d);
      $('#st').textContent='启动失败';
      $('#warn').textContent='⚠ 启动失败：'+why+'（检查麦克风/设备下拉是否有效，或用「开始 ▾ → 通话向导」自动配设备）';
      clearTimeout(window._wt); window._wt=setTimeout(()=>$('#warn').textContent='',20000);
      return r;
    }
  }catch(e){
    $('#st').textContent='启动失败';
    $('#warn').textContent='⚠ 启动请求异常：'+((e&&e.message)||e)+'（同传服务可能未响应，稍后重试或重启服务）';
    clearTimeout(window._wt); window._wt=setTimeout(()=>$('#warn').textContent='',20000);
    return {};
  }finally{
    b.disabled=false; syncMainBtn();   // 成功路径随后由 r.ok 分支置运行态(同一轮事件内,无可见闪烁)
  }
  if(r.ok){
    running=true; $('#dot').className='on';
    const live=$('#omode').value==='live';
    $('#st').textContent= live?'运行中 · 人脸预热中':'运行中';
    if(r.cap_b_err){
      $('#st').textContent+='(对方声采集失败)';
      // 情境指引:直接告诉用户去哪修,而不是只报错
      $('#warn').textContent='⚠ 对方声采集失败 → 点「高级设置」把「对方声来源」换一个设备(推荐 立体声混音)后重新开始';
      clearTimeout(window._wt); window._wt=setTimeout(()=>$('#warn').textContent='',20000);
    }
    if(live) refreshIdleHint();
    markApplied();                       // 本场以当前语向开跑→快照对齐,「生效」键熄灭
    syncMainBtn(); syncCtx(); syncEmptyGuide();
  }
  return r;
}

let callModeActive=false;   // 经"一键通话"启动→停止时自动还原系统默认麦(否则微信正常通话会没声)
$('#btn').onclick=async()=>{
  if(demoRunning){ await fetch('/demo/stop',{method:'POST'}); setDemoUI(false); return; }
  if(!running){ await doStart(); }
  else{
    await fetch(callModeActive?'/call_mode/stop':'/stop',{method:'POST'});
    running=false; callModeActive=false;
    $('#dot').className=''; $('#st').textContent='已停止';
    syncMainBtn(); syncCtx(); syncEmptyGuide(); syncLangUI();
    loadLastSession();                 // 刚结束的场次摘要落进空态(原 Hub 观测条独有信息就近迁入)
  }
};
bindMenu('#startCaret','#startmenu',()=>{
  // 运行中"再开始"无意义→置灰;演示中"演示"项变为可点的「停止演示」
  $('#callmode').classList.toggle('dis',running||demoRunning);
  $('#livemode').classList.toggle('dis',running||demoRunning);
  $('#demo').classList.toggle('dis',running&&!demoRunning);
});

// ── P1 控制：一键通话 / 急停 / 耳返 / 对方朗读 ──
let muted=false, monitorOn=false, readbackOn=false, readbackRef=false;
function syncP1Buttons(m){
  if(typeof m.muted==='boolean' && m.muted!==muted){ muted=m.muted; renderPanic(); }
  if(typeof m.monitor_on==='boolean' && m.monitor_on!==monitorOn){ monitorOn=m.monitor_on; renderMonitor(); }
  const rbOn=(typeof m.readback_on==='boolean')?m.readback_on:(m.readback&&m.readback.on);
  const rbRef=(typeof m.readback_ref==='boolean')?m.readback_ref:!!(m.readback&&m.readback.ref_locked);
  if(typeof rbOn==='boolean' && (rbOn!==readbackOn||rbRef!==readbackRef)){ readbackOn=rbOn; readbackRef=rbRef; renderReadback(); }
}
function renderPanic(){ const b=$('#panic');
  b.textContent=muted?'▶ 恢复':'⛔ 急停';
  b.style.background=muted?'linear-gradient(135deg,#f87171,#da3633)':'';
  b.style.color=muted?'#fff':'#fca5a5'; syncCtx(); }
// 声音菜单:耳返/朗读为菜单项(点击不收起,方便连调);按钮上亮绿点数提示"有开启项"
function updateSndBadge(){ const n=(monitorOn?1:0)+(readbackOn?1:0);
  const e=$('#sndbadge'); if(e) e.textContent=n?(' · '+n):'';
  const sb=$('#sndbtn'); if(sb){ sb.style.color=n?'#86efac':''; sb.style.borderColor=n?'rgba(52,211,153,.5)':''; } }
function renderMonitor(){ const b=$('#monitor'); if(!b) return;
  b.innerHTML=IC('headphones')+' 耳返 <small>'+(monitorOn?'· 开':'· 关')+'</small>';
  b.style.color=monitorOn?'#86efac':''; updateSndBadge(); }
function renderReadback(){ const b=$('#readback'); if(!b) return;
  b.innerHTML=IC('waves')+' 对方朗读 <small>'+(readbackOn?(readbackRef?'· 开':'· 等对方开口'):'· 关')+'</small>';
  b.style.color=readbackOn?'#86efac':''; updateSndBadge(); }
$('#panic').onclick=async()=>{
  muted=!muted; renderPanic();
  try{ await fetch('/panic',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({on:muted})}); }catch(e){}
};
$('#monitor').onclick=async()=>{
  monitorOn=!monitorOn; renderMonitor();
  try{ await fetch('/monitor',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({on:monitorOn})}); }catch(e){}
};
$('#readback').onclick=async()=>{
  readbackOn=!readbackOn; renderReadback();
  try{ await fetch('/readback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({on:readbackOn})}); }catch(e){}
};

// ── P3-1 实战调参：滑杆行渲染/防抖提交(声音菜单音量区 与 工具→高级调参 共用) ──
let tuneTimer=null;
function pushTuneFrom(box){ clearTimeout(tuneTimer); tuneTimer=setTimeout(async()=>{
  const values={};
  box.querySelectorAll('input[type=range]').forEach(s=>{ values[s.dataset.k]=parseFloat(s.value); });
  try{ await fetch('/tune',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({values})}); }catch(_){ }
},250); }
function tnRow(k,m,v,pf){
  return '<div class=tnrow title="'+m.desc+'"><label>'+m.label+'<span class=tnval id="'+pf+'_'+k+'">'+v+'</span></label>'
       +'<input type=range data-k="'+k+'" data-pf="'+pf+'" min="'+m.min+'" max="'+m.max+'" step="'+m.step+'" value="'+v+'"></div>'; }
function wireTn(box){ box.querySelectorAll('input[type=range]').forEach(s=>{
  s.oninput=()=>{ const tv=$('#'+s.dataset.pf+'_'+s.dataset.k); if(tv)tv.textContent=s.value; pushTuneFrom(box); }; }); }
// 声音菜单:打开时刷新开关态 + 拉取三路音量滑杆(克隆音量给对方/耳返/朗读)
bindMenu('#sndbtn','#sndmenu',async()=>{
  renderMonitor(); renderReadback();
  try{
    const d=await (await fetch('/tune')).json();
    const keys=['TTS_OUT_GAIN','MONITOR_GAIN','READBACK_GAIN'].filter(k=>d.meta[k]);
    const box=$('#sndvols');
    box.innerHTML=keys.map(k=>tnRow(k,d.meta[k],d.values[k],'sv')).join('');
    wireTn(box);
  }catch(_){ }
});
// 工具菜单 + 其内「高级调参」二级面板(完整参数,含回声闸/声纹门槛/降噪)
bindMenu('#toolbtn','#toolmenu');
(function(){
  const menu=$('#tunemenu'), item=$('#tuneItem'); if(!menu||!item) return;
  item.onclick=async(e)=>{
    e.stopPropagation(); closeMenus();
    try{
      const d=await (await fetch('/tune')).json();
      menu.innerHTML=Object.keys(d.meta).map(k=>tnRow(k,d.meta[k],d.values[k],'tv')).join('')
        +'<div class=sep></div><a id=tnreset>↺ 恢复出厂默认</a>';
      wireTn(menu);
      $('#tnreset').onclick=async()=>{
        try{ const r=await (await fetch('/tune/reset',{method:'POST'})).json();
          menu.querySelectorAll('input[type=range]').forEach(s=>{ s.value=r.values[s.dataset.k];
            const tv=$('#tv_'+s.dataset.k); if(tv)tv.textContent=r.values[s.dataset.k]; });
        }catch(_){ }
      };
      menu.classList.add('on');
    }catch(_){ }
  };
  menu.onclick=(e)=>e.stopPropagation();
})();
$('#callmode').onclick=async()=>{
  closeMenus();
  const b=$('#btn'), it=$('#callmode');
  b.disabled=true; b.textContent='📞 准备中…'; it.classList.add('dis');
  $('#st').textContent='通话模式准备中…';
  try{
    const r=await (await fetch('/call_mode/start',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({profile:$('#profile').value||''})})).json();
    // soft 步骤(麦/耦合探测)失败只是提示用 ⚠️，不是 ❌：它们不决定就绪，避免"麦其实好用却满屏红叉"吓人
    const lines=(r.steps||[]).map(s=>(s.ok?'✅ ':(s.soft?'⚠️ ':'❌ '))+s.name+(s.detail?('：'+s.detail):'')).join('\n');
    showFixLoop(!!r.self_hear_fixable);   // 检出"对方声=我的麦同族"→亮出一键修复
    if(r.ready){
      running=true; callModeActive=true; markApplied();
      $('#dot').className='on'; $('#st').textContent='运行中 · 通话模式';
      $('#warn').textContent='✅ 通话模式就绪！微信重新拨打即生效';
      clearTimeout(window._wt); window._wt=setTimeout(()=>$('#warn').textContent='',9000);
    } else {
      // 有红灯但会话其实已启动(典型:缺配音音色,字幕仍可用)→按钮态跟真相走,不再显示"已停止"却在录
      if(r.session_running){ running=true; callModeActive=true; markApplied(); $('#dot').className='on';
        $('#st').textContent='运行中 · 通话模式(有未通过项)'; }
      else $('#st').textContent=running?'运行中':'已停止';
      alert('通话模式自检未全部通过：\n\n'+lines);
      syncMainBtn(); syncCtx();
    }
  }catch(e){ $('#st').textContent=running?'运行中':'已停止'; alert('一键通话失败: '+e); }
  b.disabled=false; it.classList.remove('dis'); syncMainBtn(); syncCtx();
};
$('#demo').onclick=async()=>{
  closeMenus();
  if(demoRunning){ await fetch('/demo/stop',{method:'POST'}); setDemoUI(false); return; }
  if(running){ alert('请先停止当前会话再运行演示。'); return; }
  const body={profile:$('#profile').value||'', live_mode: $('#omode').value==='live'};
  for(const k in spanByUid) delete spanByUid[k];
  for(const k in rowByTurn) delete rowByTurn[k];
  const r=await (await fetch('/demo',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(r.ok){ setDemoUI(true);
    // 演示约 30s 后自动复位按钮态(后端结束会推 sys 事件,这里兜底)
    setTimeout(()=>{ if(demoRunning) setDemoUI(false); }, 60000); }
};
// 直播同传(原 Hub 卡头 CTA 的编排,移入唯一开始入口)：切直播输出并立即开始;
// 角色激活由 /start 的直播联动完成(服务端向 Hub 激活当前角色并同步虚拟摄像头)。
$('#livemode').onclick=async()=>{
  closeMenus();
  if(running||demoRunning){ alert('请先停止当前会话再切直播同传。'); return; }
  $('#omode').value='live'; applyHint();
  const r=await doStart();
  if(!(r&&r.ok)){ const w=$('#warn'); if(w){ w.textContent='⚠ 直播同传启动失败：'+((r&&(r.detail||r.error))||'请查设备/服务后重试');
    clearTimeout(window._wt); window._wt=setTimeout(()=>w.textContent='',9000); } }
};

// 大字幕全屏:工具菜单项切换 + 快捷键 F + Esc 退出
function renderFsItem(){ const b=$('#fs'); if(b) b.innerHTML=IC('fullscreen')+(fsOn?' 退出大字幕':' 大字幕')+' <small>(F)</small>'; }
$('#fs').onclick=()=>{ closeMenus(); fsOn=!fsOn; $('#fsov').classList.toggle('on',fsOn); renderFsItem(); if(fsOn) renderFS(); };
$('#fsclose').onclick=()=>{ fsOn=false; $('#fsov').classList.remove('on'); renderFsItem(); };
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&fsOn){ fsOn=false; $('#fsov').classList.remove('on'); renderFsItem(); return; }
  if((e.key==='f'||e.key==='F')&&!e.ctrlKey&&!e.metaKey&&!e.altKey){
    const t=(e.target&&e.target.tagName)||'';
    if(t!=='INPUT'&&t!=='SELECT'&&t!=='TEXTAREA') $('#fs').click();
  }
});

// 本页对话导出(客户端·抓当前屏DOM,即时快照)
function screenDumpTxt(){
  const dump=(listId)=>[...document.querySelectorAll(listId+' .row')].map(r=>{
    const zh=(r.querySelector('.src')||{}).textContent?.trim()||'';
    const en=(r.querySelector('.dst')||{}).textContent?.trim()||'';
    return zh?(zh+(en?'  ('+en+')':'')):'';
  }).filter(Boolean).join('\n');
  let txt='通译 LingoX 对话记录  '+new Date().toLocaleString()+'\n\n=== 我 · 中文(英文为发送译文) ===\n'+dump('#lme')
    +'\n\n=== 对方 · 中文(英文为原话) ===\n'+dump('#lot')+'\n';
  const blob=new Blob([txt],{type:'text/plain;charset=utf-8'});
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob);
  a.download='lingox_'+Date.now()+'.txt'; a.click();
  setTimeout(()=>URL.revokeObjectURL(a.href),2000);
}
// 导出(工具菜单内)：本页(客户端) + 完整转写/SRT/JSON(服务端·含时间戳·覆盖全程,UI未开也留存)
(function(){
  const go=(url)=>{ closeMenus(); window.open(url,'_blank'); };
  $('#expScreen').onclick=()=>{ closeMenus(); screenDumpTxt(); };
  $('#expTxt').onclick=()=>go('/transcript.txt?download=1');
  $('#expSrt').onclick=()=>go('/transcript.srt?download=1');
  $('#expJson').onclick=()=>go('/transcript.json?download=1');
  $('#expSessions').onclick=()=>go('/sessions');
})();

// 高级设置折叠面板:引擎/设备覆盖默认收起;记住展开状态(高频覆盖用户不必每次点开)
(function(){
  const p=$('#advpanel'), b=$('#advbtn'); if(!p||!b) return;
  const set=on=>{ p.classList.toggle('on',on); b.classList.toggle('on',on);
    b.innerHTML=IC('sliders')+(on?' 收起高级':' 高级设置');
    try{ localStorage.setItem('lx_adv',on?'1':'0'); }catch(e){} };
  let saved='0'; try{ saved=localStorage.getItem('lx_adv')||'0'; }catch(e){}
  set(saved==='1');
  b.onclick=()=>set(!p.classList.contains('on'));
})();

// 手机无线终端就绪轮询:relay 在线才显示该条;汇总"终端/监听/麦/摄像头"四态并给一句行动指引
function pbChip(id,ok){ const c=$(id); if(c) c.className='chip '+(ok?'ok':'bad'); }
async function pollPhone(){
  try{
    const s=await (await fetch('/monitor_status')).json();
    const bar=$('#phonebar');
    if(!s||!s.reachable){ if(bar) bar.style.display='none'; }
    else{
      if(bar) bar.style.display='flex';
      const live=$('#omode').value==='live';
      // 手机麦在用 = 注入(VB-Cable)就绪 或 已有直连订阅(无线直连路径)
      const a=!!(s.audio&&s.audio.ok), m=!!(s.mic&&(s.mic.ok||(s.mic.taps>0))), cam=!!(s.cam&&s.cam.ok);
      pbChip('#pbTerm', s.ok); pbChip('#pbAudio', a); pbChip('#pbMic', m); pbChip('#pbCam', cam);
      // 就绪判定:听对方=监听通;说话经手机=手机麦通;直播还需手机摄像头
      const needCam=live;
      const ready = s.ok && a && m && (!needCam||cam);
      const msg=$('#pbMsg');
      if(msg){
        if(ready){ msg.className='msg ready'; msg.textContent='✓ 手机端就绪，可以开始同传'; }
        else{
          const miss=[];
          if(!s.ok) miss.push('打开手机端');
          if(!a) miss.push('开监听');
          if(!m) miss.push('开手机麦');
          if(needCam&&!cam) miss.push('开摄像头');
          msg.className='msg';
          msg.textContent= s.ok? ('待完成：'+miss.join(' · ')+'（手机端「一键准备」可自动完成）')
                                 : '未连接：手机扫码打开手机端，点「一键准备」';
        }
      }
      const op=$('#pbOpen'); if(op&&s.https_url) op.href=s.https_url;
      const sh=$('#pbShow'); if(sh&&s.show_url) sh.href=s.show_url;
    }
  }catch(e){ const bar=$('#phonebar'); if(bar) bar.style.display='none'; }
  setTimeout(pollPhone, 3000);
}
// ── 语向控件状态(P0/P1)：appliedSrc/Dst=会话已生效语向快照。
//    运行中"当前选择≠已生效"→亮出琥珀「生效」键(有未生效的改动)；未运行时选好即记住,键不出现。──
let appliedSrc='zh', appliedDst='en';
function markApplied(){ const s=$('#lsrc'), d=$('#ldst');
  if(s&&s.value) appliedSrc=s.value; if(d&&d.value) appliedDst=d.value; syncLangUI(); }
function syncLangUI(){
  const ap=$('#langapply'), s=$('#lsrc'), d=$('#ldst'); if(!ap||!s||!d) return;
  const pend=running && !demoRunning && (s.value!==appliedSrc||d.value!==appliedDst);
  ap.classList.toggle('show',pend);
  if(!pend){ ap.disabled=false; ap.textContent='生效'; }
  if(window._renderQuick) window._renderQuick();
}
// 语向就地反馈(标签右侧小字,与角色字段 swtag 同模式):操作结果不再只藏在页脚观测条
function langTag(html,ms){ const t=$('#langtag'); if(!t) return; t.innerHTML=html||'';
  clearTimeout(window._lgt); if(html&&ms) window._lgt=setTimeout(()=>{ t.innerHTML=''; },ms); }
async function loadLangs(){
  try{
    // 取数失败也要让语向下拉长出中英兜底(否则骨架"加载中…"永久驻留)
    let d={}; try{ d=await (await fetch('/config/langs')).json(); }catch(e){}
    const langs=(d.langs&&d.langs.length)?d.langs:[{code:'zh',name:'中文'},{code:'en',name:'英语'}];
    const opt=(cur)=>langs.map(l=>'<option value="'+l.code+'"'+(l.code===cur?' selected':'')+'>'+l.name+'</option>').join('');
    const ls=$('#lsrc'), ld=$('#ldst');
    if(ls) ls.innerHTML=opt(d.src||'zh');
    if(ld) ld.innerHTML=opt(d.dst||'en');
    appliedSrc=d.src||'zh'; appliedDst=d.dst||'en';
    const gc=$('#gcount');
    if(gc){ const n=d.glossary_count||0; gc.textContent=(d.glossary_on===false)?'（已关闭）':(n>0?('· '+n+' 条'):'· 未设'); }
    const exn=$('#expn');
    if(exn){ const tc=d.transcript_count||0; exn.textContent=(d.transcript_on===false)?'':(tc>0?('· '+tc):''); }
    // P3-5 两栏标题跟随语向(原先硬编码"中文→英文"，切日/韩/俄时误导)
    const updHeads=()=>{
      const sn=(ls&&ls.selectedOptions[0])?ls.selectedOptions[0].text:'中文';
      const dn=(ld&&ld.selectedOptions[0])?ld.selectedOptions[0].text:'英语';
      const h1=$('#hme'), h2=$('#hot');
      if(h1) h1.textContent='我 · '+sn+' → '+dn+'(对方听到克隆'+dn+')';
      if(h2) h2.textContent='对方 · '+dn+' → '+sn+'字幕';
    };
    updHeads();
    // 下拉/⇄改动：立即记住翻译方向;运行中转入"待生效"(亮琥珀键),不再静默留串语言隐患
    const post=async()=>{ try{ await fetch('/config/langs',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({src:$('#lsrc').value,dst:$('#ldst').value})}); }catch(e){} updHeads(); syncLangUI();
      if(running && $('#langapply').classList.contains('show')){
        langTag('<span style="color:var(--warn)">待生效</span>');
        const w=$('#warn'); if(w){ w.textContent='ℹ 已改翻译方向；点琥珀「生效」让识别按新语言重启，否则会串语言';
        clearTimeout(window._wt); window._wt=setTimeout(()=>{w.textContent='';},7000);} }
      else langTag(''); };
    if(ls) ls.onchange=post; if(ld) ld.onchange=post;
    // 生效：自动停→按新语言重启采集链，无需手动停/开(未运行=仅记住,开始后生效)
    const applyLangs=async(restart)=>{
      const s=$('#lsrc').value, d=$('#ldst').value;
      const sn=($('#lsrc').selectedOptions[0]||{}).text||s, dn=($('#ldst').selectedOptions[0]||{}).text||d;
      const w=$('#warn'), ap=$('#langapply');
      if(ap && restart){ ap.disabled=true; ap.textContent='生效中…';
        langTag('<span style="color:var(--warn)">切换重启中…</span>'); }
      try{
        const r=await (await fetch('/config/langs',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({src:s,dst:d,restart:!!restart})})).json();
        updHeads();
        if(w){
          if(r.restarted && !r.restart_err) w.textContent='✅ 已切换并重启：我说「'+sn+'」→ 对方「'+dn+'」'+(r.asr_route?('（'+r.asr_route+'）'):'');
          else if(r.restarted && r.restart_err) w.textContent='⚠ 已重启但采集有告警：'+r.restart_err;
          else if(r.restart_err) w.textContent='⚠ 重启失败：'+r.restart_err+'（可手动停止再开始）';
          else w.textContent='已设置语向：我说「'+sn+'」→ 对方「'+dn+'」'+(running?'（点「生效」重启识别）':'（未在通话，开始后生效）');
          clearTimeout(window._wt); window._wt=setTimeout(()=>{w.textContent='';},9000);
        }
        if(r.restarted && !r.restart_err) langTag('<span style="color:var(--ok)">✓ 已生效</span>',3000);
        else if(r.restart_err) langTag('<span style="color:var(--danger)">✗ 未生效,可重试</span>',6000);
        else if(restart) langTag('<span style="color:var(--ok)">✓ 已记住</span>',3000);
        if(!r.restart_err) markApplied();          // 成功(或未运行仅记住)→快照对齐,生效键熄灭
      }catch(e){ if(w) w.textContent='切换失败：'+e; langTag('<span style="color:var(--danger)">✗ 切换失败</span>',6000); }
      if(ap && restart){ ap.disabled=false; ap.textContent='生效'; }
      syncLangUI();
    };
    const ap=$('#langapply'); if(ap) ap.onclick=()=>applyLangs(true);
    // 常用语向一键直达(我说中文→X)：避开 24 项下拉框误点(实测点日语点成韩/俄)。一点即切+重启生效。
    const QUICK=[['en','英语'],['ja','日语'],['ko','韩语'],['ru','俄语'],['yue','粤语']];
    const qbox=$('#langquick');
    if(qbox){
      const avail=new Set(langs.map(l=>l.code));
      const renderQuick=()=>{
        [...qbox.querySelectorAll('button')].forEach(b=>b.remove());
        QUICK.filter(([c])=>avail.has(c)).forEach(([code,name])=>{
          const b=document.createElement('button'); b.type='button';
          b.textContent=name; b.dataset.code=code;
          const on=($('#ldst').value===code && $('#lsrc').value==='zh');
          b.className='qc'+(on?' on':'');
          b.onclick=async()=>{
            if($('#lsrc').value==='zh' && $('#ldst').value===code && !$('#langapply').classList.contains('show')){
              const w=$('#warn'); if(w){ w.textContent='已经是「中文 → '+name+'」了'; clearTimeout(window._wt); window._wt=setTimeout(()=>{w.textContent='';},4000);} return;
            }
            $('#lsrc').value='zh'; $('#ldst').value=code; await applyLangs(true);
          };
          qbox.appendChild(b);
        });
      };
      window._renderQuick=renderQuick;   // 语向任何来源的变化(下拉/⇄/生效/开始)统一经 syncLangUI 刷新高亮
      renderQuick();
    }
    const sw=$('#langswap');
    if(sw) sw.onclick=async()=>{
      const a=$('#lsrc').value, b=$('#ldst').value;
      if(a===b) return;                                  // 同语言无需互换
      sw.classList.remove('spin'); void sw.offsetWidth; sw.classList.add('spin');
      $('#lsrc').value=b; $('#ldst').value=a;
      await post();                                      // 与下拉同一模型:记住方向;运行中亮「生效」键待确认
    };
    syncLangUI();
  }catch(e){}
  try{
    const te=await (await fetch('/config/tts')).json();
    const ts=$('#ttsengine');
    if(ts){ ts.value=te.engine||'fish';
      ts.onchange=async()=>{ try{ await fetch('/config/tts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({engine:ts.value})}); }catch(e){} }; }
  }catch(e){}
}
async function boot(){
  // 服务刚拉起时 /devices 可能还没就绪；此前一次失败会让 boot 整体中断(字幕通道都不连)。
  // 重试 3 次(1.5s 间隔)兜住"启动器起服务后立刻开页"的常见时序。
  for(let i=0;i<3;i++){
    try{ await loadDevices(); break; }
    catch(e){ if(i===2) console.warn('loadDevices 三连败(设备/角色下拉可能为空):',e);
              else await new Promise(r=>setTimeout(r,1500)); }
  }
  loadLangs(); loadLastSession();
  connect(); pollMetrics(); pollPhone();
  syncEmptyGuide();
  const q=new URLSearchParams(location.search);
  if(q.get('live')==='1'){ $('#omode').value='live'; applyHint(); }
  if(q.get('go')==='1' && !running){ setTimeout(()=>doStart(), 300); }
  // 改版一次性搬家提示(仅首次):告知老控件的新位置/新行为
  try{ if(!localStorage.getItem('lx_ui3_tip')){ localStorage.setItem('lx_ui3_tip','1');
    $('#warn').textContent= localStorage.getItem('lx_ui2_tip')
      ? '✨ 语向已并入角色一排：下拉/⇄/常用胶囊选好即记住；通话中改动会亮出琥珀「生效」键,点它按新语言重启识别'
      : '✨ 界面导览：耳返/朗读 → 「声音」 · 大字幕/导出/调参/术语表 → 「工具」 · 通话向导/直播同传/演示 → 「开始 ▾」 · 语向与角色同排,通话中改语向后点琥珀「生效」键';
    localStorage.setItem('lx_ui2_tip','1');
    clearTimeout(window._wt); window._wt=setTimeout(()=>$('#warn').textContent='',20000); } }catch(e){}
}
boot();
</script></body></html>"""


if __name__ == "__main__":
    # 双开预检(06o)：uvicorn 默认 REUSE,Windows 下第二实例会静默双绑 7900 → 会话/字幕串线。
    import port_guard
    port_guard.ensure_port_free(PORT, "live_interpreter", host="127.0.0.1")
    logger.info(f"LiveInterpreter 启动: http://127.0.0.1:{PORT}/  (STT={STT_URL} HUB={HUB_URL})")
    logger.info(f"  配音引擎={INTERP_TTS_ENGINE}({_tts_urls()[0]}) · 语向 我={_SRC_LANG}/对方={_DST_LANG} · "
                f"流式STT默认={'开' if STREAM_STT_DEFAULT else '关'} · 字幕层=/overlay")
    _gloss_probe_kick()          # opt-in 定时占位存活探针(INTERP_GLOSSARY_PROBE_EVERY>0 才起)
    _llm_warmup_kick()           # 后台预热本机大模型(auto/llm 后端)：消除首句 ~6s 冷启动
    _tm_boot_warmup_kick()       # P4-5 开机空闲预热高频句(慢速让路版,与持久化缓存配合)
    _preload_push_kick()         # P6-5 按会话日志 top 语对驱动 .140 预载(客户端最懂真实分布)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
