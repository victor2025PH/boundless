# -*- coding: utf-8 -*-
"""语音指挥网关（P1 · 2026-08-08）——「小界」唤醒词 + 指令识别 + 意图执行。

出处《未来感机房展演_手势语音指挥与手机万能外设_五视角优化美化方案_20260808.md》§五/§14.4。
定位：hud_server 的独立模块（红线 10 纪律：hud_server 只挂路由，判据与执行都在这里；
本模块对 hud_server 零 import——依赖经 configure() 注入，防循环）。

架构定案（实施中两次再优化，理由见方案 §十五）：
  1. KWS 从「浏览器 wasm」改为「中枢服务端 sherpa-onnx」：官方无现成 wasm 发布件可自托管，
     Emscripten 胶水集成面大（MediaPipe module worker 的坑已踩过一轮）；服务端 3.3M 模型
     RTF≈0.015，换唤醒词=改一行文本。约束不变：仅 opt-in 机器、音频**内存流转不落盘**、
     屏上聆听指示、推流冻结客户端停麦+服务端 verdict=danger 拒收双闸。
  2. ASR 弃 .140 STT 专机改**本机 SenseVoice int8**（sherpa-onnx 同栈）：4.5s 音频 51ms，
     指令场景零跨机依赖——演示可靠性优先，.140 留作 P4 流式字幕升级路径。
  3. 意图匹配走**拼音层**：SenseVoice 把「韵声」写成「音声」（专有名词别字是常态，
     语料实测抓获）——机器名/动词先 pypinyin 归一再匹配，别字免疫。

隐私口径（与手势层同源话术）：唤醒前音频只进 KWS 滑窗即弃；唤醒后聆听窗 ≤6s 音频
仅内存中转 ASR，全程不落盘、不出集群。

VERBS 表是 P2 ops_gateway 白名单的前身：P2 落地时 ops_gateway import 本表对账
（契约断言两侧同源），语音只是它的一张皮。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
VOICE_DIR = HERE / "vendor" / "voice"
KWS_DIR = VOICE_DIR / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
ASR_DIR = VOICE_DIR / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
KEYWORDS_FILE = VOICE_DIR / "keywords_xiaojie.txt"   # "x iǎo j iè @小界"（text2token 生成）

SAMPLE_RATE = 16000
KWS_THRESHOLD = float(os.environ.get("HUD_KWS_TH", "0.15"))
# ↑ 语料探针 0.15-0.35 正样本全中、负样本零误触。2026-08-13 现场：合成语料 0.25 全通、
#   真人经 BRIO 远场喊话不命中（峰值 0.09 有声到服务端）——取探针下限 0.15 给远场留余量，
#   误触无恶果（唤醒只是聆听，指令仍要过意图白名单）。HUD_KWS_TH 可现场再调。
LISTEN_MAX_S = 6.0            # 聆听窗上限
SIL_END_S = 0.7               # 语音后静音 0.7s 判尾
RMS_VOICE = 0.010             # 语音活性阈（16k float32 RMS）
MIN_VOICE_S = 0.25            # 至少这么多语音才算有话
SESSION_IDLE_S = 30           # 会话无流量回收

XIAOJIE_URL = "http://127.0.0.1:7912"   # 小界 LLM/TTS 底座（fish :7855 由它代理）

_lock = threading.Lock()
_state = {"ready": False, "reason": "未初始化", "loading": False}
_kws = None
_asr = None
_sessions: dict[str, dict] = {}

# 结果广播（并入 hud_server build_ctx 快照走 SSE；phase 变化才 bump seq）
VOICE_LAST = {"phase": "idle", "seq": 0, "at": 0.0, "sid": "",
              "heard": "", "say": "", "verb": "", "m": "", "ok": True, "tts": ""}

# TTS 结果内存缓存（不落盘）
_tts_cache: dict[str, bytes] = {}
_tts_n = 0

# hud_server 注入的依赖（configure）
_deps = {"get_ctx": None, "set_tour": None, "ack_alert": None,
         "notify": None, "log_stat": None,
         "ops_arm": None, "ops_fire": None, "ops_disarm": None, "ops_undo": None,
         "ctrl_dictate": None, "ctrl_key": None, "ctrl_active": None}

# P2b 语音听写（说话→注入远端焦点框；仅在受控会话武装时可用，动作全走 ops_gateway 全闸）。
# 开关默认关：关时 feed()/意图路径**逐字节不变**（红线 10「别碰正在跑的共享管道」）。
_dictate = {"on": False, "at": 0.0}
# 听写内导航词 → 远端按键（说「回车」而不是把「回车」二字打进去）
_DICT_NAV = [(("回车", "换行", "回车键"), "enter"), (("删除", "退格", "删掉"), "backspace"),
             (("下翻", "向下翻", "往下翻页", "翻页"), "pagedown"),
             (("上翻", "向上翻", "往上翻页"), "pageup"),
             (("到顶", "回到开头"), "home"), (("到底", "去最后"), "end")]
_DICT_OFF = ("停打字", "结束打字", "停止打字", "关闭打字", "打字结束", "退出打字")


def configure(**kw) -> None:
    _deps.update({k: v for k, v in kw.items() if k in _deps})


# ---------------- 模型加载（惰性，失败=诚实降级不炸服务） ----------------

def ensure_models() -> bool:
    global _kws, _asr
    if _state["ready"]:
        return True
    with _lock:
        if _state["ready"] or _state["loading"]:
            return _state["ready"]
        _state["loading"] = True
    try:
        import sherpa_onnx  # noqa: PLC0415  惰性：没装也不拖垮 hud_server 启动
        kws = sherpa_onnx.KeywordSpotter(
            tokens=str(KWS_DIR / "tokens.txt"),
            encoder=str(KWS_DIR / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            decoder=str(KWS_DIR / "decoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
            joiner=str(KWS_DIR / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            keywords_file=str(KEYWORDS_FILE),
            keywords_threshold=KWS_THRESHOLD, num_threads=2)
        asr = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(ASR_DIR / "model.int8.onnx"),
            tokens=str(ASR_DIR / "tokens.txt"),
            num_threads=4, use_itn=True, language="zh")
        _kws, _asr = kws, asr
        _state.update(ready=True, reason="")
    except Exception as e:  # noqa: BLE001
        _state.update(ready=False, reason=f"{type(e).__name__}: {str(e)[:120]}")
    finally:
        _state["loading"] = False
    return _state["ready"]


def status() -> dict:
    now = time.time()
    return {"ready": _state["ready"], "reason": _state["reason"],
            "sessions": len(_sessions), "last_seq": VOICE_LAST["seq"],
            "dictate": bool(_dictate["on"]),
            "dictate_machine": (_deps["ctrl_active"]() if _deps.get("ctrl_active") else ""),
            "last_age_s": round(now - VOICE_LAST["at"]) if VOICE_LAST["at"] else None,
            # 拾音观测：rms≈0.0000=静音源（muted 麦/空孔），~0.003+=房间底噪，0.05+=人声
            "mics": {k: {"age_s": round(now - v.get("seen", 0)), "st": v.get("st", ""),
                         "rms": round(v.get("rms", 0.0), 4), "pk": round(v.get("pk", 0.0), 3)}
                     for k, v in _sessions.items()}}


def peek(sid: str, seconds: float = 6.0) -> dict:
    """回放耳（2026-08-13 现场诊断）：把该会话环形缓冲的最近几秒过一遍 ASR，
    返回「机器听到的字」——把『喊了没反应』从瞎猜设备变成读转写。
    音频只在函数栈上（隐私口径与 _process_utterance 同源），不落盘不出集群。"""
    if not ensure_models():
        return {"ok": False, "reason": _state["reason"]}
    with _lock:
        s = _sessions.get(sid)
        if not s:
            return {"ok": False, "reason": f"无会话 {sid}（该屏没在推流）"}
        ring = list(s.get("ring") or [])
    if not ring:
        return {"ok": True, "text": "", "dur_s": 0.0,
                "note": "缓冲空（刚唤醒过或刚连上）"}
    tail = np.concatenate(ring)[-int(SAMPLE_RATE * seconds):]
    st = _asr.create_stream()
    st.accept_waveform(SAMPLE_RATE, tail)
    _asr.decode_stream(st)
    txt = (st.result.text or "").strip()
    return {"ok": True, "text": txt, "dur_s": round(len(tail) / SAMPLE_RATE, 1),
            "rms": round(float(np.sqrt(np.mean(tail * tail))), 4) if len(tail) else 0.0}


def peek_raw(sid: str, seconds: float = 6.0) -> bytes:
    """回放耳原始档：环形缓冲尾段的 PCM16LE 字节（诊断采样率错位一类结构性损伤；
    LAN 内诊断口，音频不落服务端盘）。"""
    with _lock:
        s = _sessions.get(sid)
        ring = list((s or {}).get("ring") or [])
    if not ring:
        return b""
    tail = np.concatenate(ring)[-int(SAMPLE_RATE * seconds):]
    return (np.clip(tail, -1, 1) * 32767).astype(np.int16).tobytes()


def set_dictate(on: bool) -> dict:
    """语音打字开关。开：需已武装某台受控会话（否则无处可打）。关：把所有会话拉回 idle
    （防连续听写半句缓冲被后续当成指令误解析）。"""
    on = bool(on)
    if on and not (_deps.get("ctrl_active") and _deps["ctrl_active"]()):
        return {"ok": False, "error": "先武装某台机的受控会话，再开语音打字"}
    _dictate.update(on=on, at=time.time())
    if not on:
        with _lock:
            for s in _sessions.values():
                s.update(st="idle", buf=[], voice_s=0.0, sil_s=0.0)
    _stat("voice_dictate", "", "on" if on else "off")
    _publish("result", "dictate", heard="", verb="dictate_on" if on else "dictate_off",
             say=("请说要输入的内容；说「回车」「翻页」控制，说「停打字」退出。" if on else "已退出语音打字。"),
             m=(_deps["ctrl_active"]() if _deps.get("ctrl_active") else ""), ok=True,
             tts=_tts("好的，请说" if on else "已退出语音打字"))
    return {"ok": True, "dictate": on}


# ---------------- 拼音归一（别字免疫层） ----------------

_PUNCT_RE = re.compile(r"[，。！？、；：\s,.!?;:\"'（）()【】]+")


def _pinyin(text: str) -> str:
    try:
        from pypinyin import lazy_pinyin  # noqa: PLC0415
        return "".join(lazy_pinyin(text))
    except Exception:
        return text


def _norm(text: str) -> tuple[str, str]:
    t = _PUNCT_RE.sub("", text or "")
    return t, _pinyin(t)


# ---------------- 机器与动词白名单（P2 ops_gateway 前身） ----------------

MACHINES = [  # (id, 中文, 拼音键；别名拼音也算)
    ("zhongshu", "中枢", ["zhongshu", "zhongkong"]),
    ("yunsheng", "韵声", ["yunsheng", "yinsheng", "yunshen"]),
    ("shengbei", "声备", ["shengbei", "shenbei"]),
    ("lianbei", "脸备", ["lianbei"]),
    ("tingxie", "听写", ["tingxie"]),
    ("kouxing", "口型", ["kouxing"]),
]

# 品牌讲解固定词（演示可控性 > LLM 自由发挥；自由问答仍有 LLM 兜底）
INTRO_SAY = ("这里是无界智控集群：六台GPU工作站分别承担换脸、声音克隆、语音识别、"
             "口型合成与实时同传。您现在看到的实况板，就是我为您守护的全部算力。")

# T3 禁区词：机器级/破坏性操作对语音永远关门（方案 §14.1）。命中即明确拒答，
# **不进 LLM 兜底**——LLM 可能口头「答应」造成已执行的错觉（零执行但话术危险）。
# 注意匹配顺序：隔空指挥句式（_match_ops）先于本表——「重启数字人服务」=T2 武装，
# 「重启脸备」（整机）=T3 拒答。
T3_DENY = ["重启", "关机", "关闭机器", "断电", "部署", "删除", "格式化", "改配置", "停机"]

# P2 隔空指挥：语音可武装的服务别名（svc_restart 只救 Hub 离线清单里的死服务，
# 白名单与 ops_gateway.VERBS 契约同源对账）
SVC_ALIASES = {"数字人": "ditto", "迪托": "ditto", "高清数字人": "ditto",
               "变声": "rvc", "echo": "echomimic"}


def _match_ops(t: str, pin: str, mid: str | None):
    """隔空指挥句式（T1/T2 只到**武装**，点火永远要第二段确认）。"""
    if not _deps.get("ops_arm"):
        return None
    if any(w in t for w in ("撤销", "切回去", "恢复原样")):
        return {"verb": "ops_undo", "tier": "T2"}
    if any(w in t for w in ("取消", "撤防", "算了", "不用了")):
        return {"verb": "ops_cancel", "tier": "T0"}
    if any(w in t for w in ("确认", "点火", "就这么办")):
        return {"verb": "ops_confirm", "tier": "T2"}
    # P1 算力模式切换（2026-08-13）：「模式」强特征词，必须先于换脸路由判定——
    # 「切到换脸模式」含「换脸+切」，后者先判会误落 route_switch
    if "模式" in t and any(w in t for w in ("切", "换到", "进入")):
        target = ""
        if any(w in t for w in ("智聊", "聊天", "chatx")):
            target = "chatx"
        elif any(w in t for w in ("换脸", "直播", "face")):
            target = "face"
        elif any(w in t for w in ("编程", "算力", "代码", "写码", "code")):
            target = "code"
        if target:
            return {"verb": "ops_arm", "ops_verb": "mode_switch",
                    "args": {"target": target}, "tier": "T2"}
    # P0 联动 2026-08-14：姿态归位（漂移了一句话拉回当前模式编制；仍 T2 两段式）
    if "归位" in t and any(w in t for w in ("姿态", "模式", "算力", "一键", "集群")):
        return {"verb": "ops_arm", "ops_verb": "posture_repair", "args": {}, "tier": "T2"}
    if "换脸" in t and ("切" in t or "换到" in t):
        target = "lianbei" if "脸备" in t else ("zhongshu" if ("中枢" in t or "回" in t) else "")
        if target:
            return {"verb": "ops_arm", "ops_verb": "route_switch",
                    "args": {"target": target}, "tier": "T2"}
    if "唤醒" in t and mid:
        return {"verb": "ops_arm", "ops_verb": "wol", "args": {"machine": mid}, "tier": "T1"}
    if any(w in t for w in ("重启", "拉起", "救活")):
        for alias, svc in SVC_ALIASES.items():
            if alias in t:
                return {"verb": "ops_arm", "ops_verb": "svc_restart",
                        "args": {"service": svc}, "tier": "T2"}
    return None

VERBS = {
    # verb: (触发词表[汉字或拼音子串], 需要机器名, T级)
    # panel/drill 必须排在 detail 之前：「打开X的操作盘」「看看X的服务」含 detail 触发词，
    # dict 序=匹配优先级（P2 语音开盘 / P4 语音下钻 2026-08-12）
    "panel":    (["操作盘", "操作面板", "caozuopan"],                  True,  "T0"),
    "drill":    (["下钻", "的服务", "xiazuan"],                        True,  "T0"),
    "detail":   (["放大", "看看", "看一下", "打开", "详情", "fangda"], True,  "T0"),
    "view":     (["星座", "表格", "列表", "事件", "告警流", "概览"],   False, "T0"),
    "collapse": (["收起", "返回", "关闭面板", "shouqi"],               False, "T0"),
    # P2 演示状态宏（2026-08-18 三轮）：特效满档+切全息（纯呈现层 T0；关=说「收起」回安静档由
    # collapse 兼管——collapse 在前不冲突：「演示状态」不含其触发词）
    "show":     (["演示状态", "特效全开", "展演特效", "yanshi"],       False, "T0"),
    "tour":     (["欢迎", "开始展演", "下一幕", "谢幕", "结束展演"],   False, "T0"),
    # P0 联动 2026-08-14：算力模式问询（T0 只读秒答：当前模式/切换进度/姿态一句话）
    "mode_query": (["什么模式", "哪个模式", "当前模式", "切换进度", "模式进度", "切到哪"],
                   False, "T0"),
    "checkup":  (["体检", "状态怎么样", "集群怎么样", "健康", "tijian",
                  "集群期间", "集群机检", "集群题见"],  # 远场 ASR 别字实录（2026-08-13「集群期间」）
                 False, "T1"),
    "query":    (["显存", "温度", "负载", "怎么样了"],                 True,  "T0"),
    "ack":      (["知道了", "别报了", "确认告警", "收到告警"],          False, "T1"),
    "dim":      (["调暗", "暗一点", "调亮", "亮一点"],                 False, "T1"),
    "intro":    (["介绍", "讲解", "是什么系统", "什么系统"],           False, "T0"),
}


def _find_machine(text: str, pin: str):
    for mid, zh, keys in MACHINES:
        if zh in text or any(k in pin for k in keys):
            return mid, zh
    return None, None


def _fmt_machine(m: dict) -> str:
    parts = []
    if m.get("gpu_util") is not None and m["gpu_util"] >= 0:
        parts.append(f"GPU {m['gpu_util']}%")
    vu, vt = m.get("vram_used_mb"), m.get("vram_total_mb")
    if vu is not None and vt:
        parts.append(f"显存 {vu / 1024:.1f} 比 {round(vt / 1024)} G")
    if m.get("temp") is not None and m.get("temp", -1) >= 0:
        parts.append(f"温度 {m['temp']} 度")
    st = "在线" if m.get("status") == "ok" else "状态异常"
    return f"{m.get('zh')}现在{st}，" + "，".join(parts) + "。"


def match_intent(text: str) -> dict:
    """规则优先：隔空指挥句式 → T3 禁区拒答 → 动词白名单 → LLM 兜底（verb=""）。"""
    t, pin = _norm(text)
    mid, mzh = _find_machine(t, pin)
    ops = _match_ops(t, pin, mid)
    if ops:
        ops.update({"m": mid or "", "mzh": mzh or "", "raw": t})
        ops.setdefault("args", {})
        ops.setdefault("ops_verb", "")
        return ops
    if any(w in t for w in T3_DENY):
        return {"verb": "deny_t3", "m": mid or "", "mzh": mzh or "", "tier": "T3", "raw": t}
    for verb, (words, need_m, tier) in VERBS.items():
        hit = any((w in t) or (w.isascii() and w in pin) for w in words)
        if not hit:
            continue
        if need_m and not mid:
            continue
        return {"verb": verb, "m": mid or "", "mzh": mzh or "", "tier": tier, "raw": t}
    return {"verb": "", "m": mid or "", "mzh": mzh or "", "tier": "T0", "raw": t}


# ---------------- 意图执行（T0 呈现 / T1 只读或既有 ack 通道） ----------------

def _exec_intent(it: dict) -> tuple[str, dict, bool]:
    """返回 (say 应答文本, act 广播动作, ok)。"""
    ctx = _deps["get_ctx"]() if _deps["get_ctx"] else {}
    verb, mid, mzh, t = it["verb"], it["m"], it["mzh"], it["raw"]
    if verb == "deny_t3":
        return "这类操作不归语音管，请在控制台操作。", {}, False
    # ---- P2 隔空指挥：语音只到武装，点火要第二段「确认」（§14.2 两段式原样） ----
    if verb == "ops_arm":
        r = _deps["ops_arm"](it["ops_verb"], it["args"], by="voice", src="voice")
        if not r.get("ok"):
            return f"武装被拒：{r.get('error', '')[:60]}", {}, False
        return (f"已武装：{r['desc']}。{r['impact'][:50]}。"
                f"说「确认」点火，说「取消」撤防。"), {"act": "ops"}, True
    if verb == "ops_confirm":
        r = _deps["ops_fire"]("", src="voice")
        if not r.get("ok"):
            return f"点火失败：{r.get('error', '')[:60]}", {}, False
        say = "点火成功。" + (f"{r['undo_in']} 秒内说「撤销」可以撤回。" if r.get("undo_in") else "")
        return say, {"act": "ops"}, True
    if verb == "ops_cancel":
        r = _deps["ops_disarm"]("", src="voice")
        return ("好的，已撤防。" if r.get("ok") else "现在没有武装中的动作。"), {"act": "ops"}, True
    if verb == "ops_undo":
        r = _deps["ops_undo"](src="voice")
        return ("已撤销，恢复原样。" if r.get("ok")
                else f"撤销失败：{r.get('error', '')[:50]}"), {"act": "ops"}, r.get("ok", False)
    if verb == "panel":
        # P2 语音开盘：面板动作仍全走既有护栏（危险动词面板只武装,点火要第二段在场信号）
        return f"已打开{mzh}的操作盘。", {"act": "panel", "m": mid}, True
    if verb == "drill":
        # P4 语音下钻：子星环绕看服务；救活仍两段式（无编目服务时客户端退操作盘）
        return f"已下钻{mzh}的服务。", {"act": "drill", "m": mid}, True
    if verb == "show":
        # P2 演示状态宏（2026-08-18 三轮）：客户端 setShow=粒子×2+辉光增强+切全息（纯呈现层）
        return "演示状态已开，特效全开。", {"act": "show"}, True
    if verb == "detail":
        return f"好的，为您放大{mzh}。", {"act": "detail", "m": mid}, True
    if verb == "view":
        v = "cons" if "星座" in t else ("events" if ("事件" in t or "告警" in t) else "rows")
        vn = {"cons": "星座视图", "events": "事件流", "rows": "表格视图"}[v]
        return f"已切换到{vn}。", {"act": "view", "v": v}, True
    if verb == "collapse":
        return "已收起。", {"act": "collapse"}, True
    if verb == "tour":
        st = _deps["set_tour"]
        if not st:
            return "展演引擎不在线。", {}, False
        if "下一幕" in t:
            tour = st("next")
        elif "谢幕" in t or "结束" in t:
            tour = st(6)
        elif "常态" in t:
            tour = st(0)
        else:
            tour = st(1)
        return f"进入第{tour['step']}幕，{tour['name']}。", {"act": "tour"}, True
    if verb == "mode_query":
        cmx = ctx.get("clustermode") or {}
        if not cmx.get("mode"):
            return "还没有明确的算力模式记录，可以说：切到编程模式。", {}, True
        zh = cmx.get("zh") or cmx.get("mode")
        if cmx.get("switching"):
            pg = cmx.get("progress") or {}
            say = (f"正在切换到{zh}，进度第 {pg.get('done', '?')} 步，共 {pg.get('total', '?')} 步。"
                   if pg.get("total") else f"正在切换到{zh}。")
            return say, {"act": "checkup"}, True
        say = f"当前是{zh}。"
        po = cmx.get("posture") or {}
        if po.get("errors"):
            say += f"姿态失守 {po['errors']} 项，说「一键归位」可以拉回。"
        elif po.get("ok"):
            say += "姿态健康。"
        return say, {"act": "checkup"}, True
    if verb == "checkup":
        ms = ctx.get("machines") or []
        bad = [m.get("zh") for m in ms if m.get("status") != "ok"]
        firing = [e for e in (ctx.get("events") or [])
                  if e.get("state") == "firing" and not e.get("ack")]
        v = ctx.get("verdict")
        vt = {"safe": "此刻可以安全重启", "wait": "有长任务在跑",
              "danger": "正在直播推流"}.get(v, "忙态未知")
        say = (f"体检完成：六机全员在线，{vt}。" if not bad
               else f"体检完成：{'、'.join(str(b) for b in bad)}异常，{vt}。")
        say += f"{len(firing)} 条告警在燃。" if firing else "没有告警在燃。"
        return say, {"act": "checkup"}, not bad
    if verb == "query":
        m = next((m for m in (ctx.get("machines") or []) if m.get("id") == mid), None)
        if not m:
            return f"没找到{mzh}的数据。", {}, False
        return _fmt_machine(m), {"act": "detail", "m": mid}, True
    if verb == "ack":
        firing = [e for e in (ctx.get("events") or [])
                  if e.get("state") == "firing" and not e.get("ack") and e.get("key")]
        if not firing:
            return "现在没有待确认的告警。", {}, False
        ok = bool(_deps["ack_alert"] and _deps["ack_alert"](firing[0]["key"], "voice"))
        return ("好的，这条告警我记下了，不再打扰。" if ok else "确认失败，请在屏上双击确认。"), \
            {"act": "ack"}, ok
    if verb == "dim":
        on = ("暗" in t)
        return ("好的，调暗一点。" if on else "好的，恢复亮度。"), {"act": "dim", "on": on}, True
    if verb == "intro":
        return INTRO_SAY, {"act": "intro"}, True
    # 自由问答兜底：小界 LLM（只读咨询，永不产生动作）
    say = _llm_fallback(t)
    return say, {"act": "chat"}, True


def _llm_fallback(text: str) -> str:
    try:
        req = urllib.request.Request(
            XIAOJIE_URL + "/api/chat",
            data=json.dumps({"machine": "zhongshu",
                             "messages": [{"role": "user", "content": text}]}).encode("utf-8"),
            method="POST")
        with urllib.request.urlopen(req, timeout=12) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        reply = str(d.get("reply") or "").strip()
        return reply[:120] if reply else "这个问题我还答不好，您看屏幕上的实况吧。"
    except Exception:
        return "我在，不过大脑连线有点慢，指令类的话您可以说：小界，集群体检。"


def _tts(text: str) -> str:
    """小界的嗓子（fish 经 xiaojie 代理）；成功返回缓存 URL，失败返回空（字幕仍在）。"""
    global _tts_n
    try:
        req = urllib.request.Request(XIAOJIE_URL + "/api/tts",
                                     data=json.dumps({"text": text[:120]}).encode("utf-8"),
                                     method="POST")
        with urllib.request.urlopen(req, timeout=25) as r:
            wav = r.read()
        if len(wav) < 2000:
            return ""
        with _lock:
            _tts_n += 1
            key = str(_tts_n)
            _tts_cache[key] = wav
            for k in list(_tts_cache)[:-8]:      # 只留最近 8 条（内存纪律）
                _tts_cache.pop(k, None)
        return f"/api/voice/tts/{key}.wav"
    except Exception:
        return ""


def tts_bytes(key: str) -> bytes | None:
    return _tts_cache.get(key)


# ---------------- 会话与流式入口 ----------------

def _publish(phase: str, sid: str, **kw) -> None:
    VOICE_LAST["phase"] = phase
    VOICE_LAST["sid"] = sid
    VOICE_LAST["at"] = round(time.time(), 3)
    VOICE_LAST["a"] = None                      # 动作字段逐条清零，防上一条残留
    for k, v in kw.items():
        VOICE_LAST[k] = v
    VOICE_LAST["seq"] += 1
    if _deps["notify"]:
        _deps["notify"]()


def _stat(k: str, sid: str, v: str = "") -> None:
    if _deps["log_stat"]:
        _deps["log_stat"](k, m=sid, v=v, src="voice")


RING_S = 8.0      # idle 态环形缓冲：原 0.8s 只为唤醒回灌；2026-08-13 加长到 8s 供 peek()
                  # 「回放耳」诊断（512KB/会话，仍只驻内存滚动即弃；回灌量由下行截尾控制）
WAKE_BACK_S = 0.5  # 唤醒瞬间回灌最近 0.5s 进聆听缓冲（「小界切换到星座」连说不缺字）


def _session(sid: str) -> dict:
    s = _sessions.get(sid)
    if not s:
        s = {"st": "idle", "stream": _kws.create_stream(), "buf": [], "ring": [],
             "voice_s": 0.0, "sil_s": 0.0, "t0": 0.0, "seen": time.time()}
        _sessions[sid] = s
    s["seen"] = time.time()
    # 回收陈旧会话
    for k in [k for k, v in _sessions.items() if time.time() - v["seen"] > SESSION_IDLE_S]:
        _sessions.pop(k, None)
    return s


def _handle_dictation(sid: str, heard: str) -> None:
    """听写路由：导航词→远端按键，「停打字」→退出，其余→注入远端文本。
    全程无 TTS（每句都念会吵）——字幕带显 heard 即所见即所打；失败才出错话。"""
    tnorm, _ = _norm(heard)
    if any(w in tnorm for w in _DICT_OFF):
        set_dictate(False)
        return
    for words, key in _DICT_NAV:
        if any(w in tnorm for w in words):
            r = _deps["ctrl_key"](key) if _deps.get("ctrl_key") else {"ok": False, "error": "未接线"}
            zh = {"enter": "回车", "backspace": "删除", "pagedown": "下翻",
                  "pageup": "上翻", "home": "到顶", "end": "到底"}.get(key, key)
            _publish("result", sid, heard=heard, verb="dictate_key",
                     say=("好，" + zh) if r.get("ok") else ("按键失败：" + str(r.get("error", ""))[:40]),
                     m=(_deps["ctrl_active"]() if _deps.get("ctrl_active") else ""),
                     ok=bool(r.get("ok")), tts="")
            _stat("voice_dictate", sid, "key:" + key)
            return
    r = _deps["ctrl_dictate"](heard) if _deps.get("ctrl_dictate") else {"ok": False, "error": "未接线"}
    _publish("result", sid, heard=heard, verb="dictate",
             say=("" if r.get("ok") else "打字失败：" + str(r.get("error", ""))[:50]),
             m=(_deps["ctrl_active"]() if _deps.get("ctrl_active") else ""),
             ok=bool(r.get("ok")), tts="")
    _stat("voice_dictate", sid, "type")


def _process_utterance(sid: str, samples: np.ndarray) -> None:
    """后台线程：ASR → 意图 → 执行 → TTS → 广播。音频只在本函数栈上，出栈即弃。"""
    try:
        st = _asr.create_stream()
        st.accept_waveform(SAMPLE_RATE, samples)
        _asr.decode_stream(st)
        heard = (st.result.text or "").strip()
        # 剥掉开头的唤醒词残留（回灌缓冲会把「小界」尾音带进 ASR，近音别字全剥）
        heard = re.sub(r"^[,，。.!！?？\s]*(小[界姐介借杰建结贱戒]|少借|校界)[,，。.!！?？\s]*",
                       "", heard)
        if not heard:
            _publish("result", sid, heard="", say="我没听清，再说一次？",
                     verb="", m="", ok=False, tts="")
            _stat("voice_deny", sid, "empty")
            return
        # P2b 听写路由：开关开 + 有活受控会话 → 说什么打什么（不进指令匹配）
        if _dictate["on"] and _deps.get("ctrl_active") and _deps["ctrl_active"]():
            _handle_dictation(sid, heard)
            return
        it = match_intent(heard)
        say, act, ok = _exec_intent(it)
        verb = it["verb"] or "chat"
        if verb == "deny_t3":
            _stat("voice_deny", sid, "t3")
        else:
            _stat("voice_intent", sid, verb)
        tts_url = _tts(say)
        _publish("result", sid, heard=heard, say=say, verb=verb,
                 m=it["m"], ok=ok, tts=tts_url, **({"a": act} if act else {}))
    except Exception as e:  # noqa: BLE001
        _publish("result", sid, heard="", say="识别链路出错了，走手势或键盘吧。",
                 verb="", m="", ok=False, tts="")
        _stat("voice_deny", sid, f"err:{type(e).__name__}")


def feed(sid: str, pcm16: bytes, verdict: str = "safe", writer: str = "") -> dict:
    """流式入口（POST /api/voice/stream 的处理体）。返回给采集端的即时状态。

    writer=页面身份「<加载时间戳ms>-<随机>」（2026-08-13 幽灵写手实锤：遗留旧标签页抓着
    哑麦与外壳新页**双写同一 sid**，零块与真音频交错=KWS 永远听碎片。协议：加载更晚的
    页面到场即接管（换 KWS 流+清缓冲），更早的被拒收 superseded——确定性胜负，无乒乓）。
    未带 writer 的老页面按时间戳 0 论=永远让位给带标的新页。"""
    if verdict == "danger":
        _sessions.pop(sid, None)                 # 推流硬闸：丢弃会话与缓冲
        return {"st": "frozen"}
    if not ensure_models():
        return {"st": "off", "reason": _state["reason"]}
    if not pcm16:
        return {"st": "empty"}
    samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
    dur = len(samples) / SAMPLE_RATE

    def _wts(w: str) -> int:
        try:
            return int(str(w).split("-", 1)[0])
        except (ValueError, TypeError):
            return 0

    with _lock:
        s = _session(sid)
        if writer != s.get("w", ""):
            if _wts(writer) >= _wts(s.get("w", "")):
                s.update(w=writer, st="idle", buf=[], ring=[],
                         voice_s=0.0, sil_s=0.0)
                s["stream"] = _kws.create_stream()   # 接管=干净开局，幽灵毒化的流不续用
                _stat("voice_takeover", sid, str(writer)[:24])
            else:
                return {"st": "superseded"}
        # 拾音观测（2026-08-12 现场工单「喊小界没响应」）：每路会话滚动 RMS/峰值进 status——
        # 一眼分辨「没在推流 / 推的是静音(muted 麦=精确 0) / 有声但没命中」，免瞎猜设备
        _r = float(np.sqrt(np.mean(samples * samples))) if len(samples) else 0.0
        s["rms"] = 0.8 * s.get("rms", 0.0) + 0.2 * _r
        s["pk"] = max(s.get("pk", 0.0) * 0.98, _r)
        # P2b 连续听写：开关开 + 已武装受控会话 → 免重复「小界」，直接累积端点→转写→打字。
        # （早返回；开关关时下方原路径逐字节不变）
        if _dictate["on"] and _deps.get("ctrl_active") and _deps["ctrl_active"]():
            if s["st"] != "listen":
                s.update(st="listen", buf=[], voice_s=0.0, sil_s=0.0, t0=time.time())
            s["buf"].append(samples)
            rms = float(np.sqrt(np.mean(samples * samples))) if len(samples) else 0.0
            if rms >= RMS_VOICE:
                s["voice_s"] += dur
                s["sil_s"] = 0.0
            else:
                s["sil_s"] += dur
            ended = (s["voice_s"] >= MIN_VOICE_S and s["sil_s"] >= SIL_END_S) \
                or (time.time() - s["t0"] >= LISTEN_MAX_S)
            if not ended:
                return {"st": "dictate"}
            utt = np.concatenate(s["buf"]) if s["buf"] else np.zeros(0, dtype=np.float32)
            s.update(st="idle", buf=[], voice_s=0.0, sil_s=0.0)
            if utt.size >= SAMPLE_RATE * 0.2:
                threading.Thread(target=_process_utterance, args=(sid, utt), daemon=True).start()
            return {"st": "dictate"}
        if s["st"] == "idle":
            s["ring"].append(samples)                      # 环形缓冲（只驻内存，随即滚动丢弃）
            while sum(len(x) for x in s["ring"]) > SAMPLE_RATE * RING_S:
                s["ring"].pop(0)
            s["stream"].accept_waveform(SAMPLE_RATE, samples)
            while _kws.is_ready(s["stream"]):
                _kws.decode_stream(s["stream"])
                if _kws.get_result(s["stream"]):
                    _kws.reset_stream(s["stream"])
                    back = np.concatenate(s["ring"]) if s["ring"] else np.zeros(0, np.float32)
                    back = back[-int(SAMPLE_RATE * WAKE_BACK_S):]
                    s.update(st="listen", buf=[back], voice_s=0.0, sil_s=0.0,
                             t0=time.time(), ring=[])
                    _stat("voice_wake", sid)
                    _publish("listen", sid, heard="", say="", verb="", m="", ok=True, tts="")
                    return {"st": "wake"}
            return {"st": "idle"}
        # listen：累积语料 + 端点检测（RMS 静音判尾）
        s["buf"].append(samples)
        rms = float(np.sqrt(np.mean(samples * samples))) if len(samples) else 0.0
        if rms >= RMS_VOICE:
            s["voice_s"] += dur
            s["sil_s"] = 0.0
        else:
            s["sil_s"] += dur
        ended = (s["voice_s"] >= MIN_VOICE_S and s["sil_s"] >= SIL_END_S) \
            or (time.time() - s["t0"] >= LISTEN_MAX_S)
        if not ended:
            return {"st": "listen"}
        utt = np.concatenate(s["buf"]) if s["buf"] else np.zeros(0, dtype=np.float32)
        s.update(st="idle", buf=[], voice_s=0.0, sil_s=0.0)
        s["stream"] = _kws.create_stream()       # 重开 KWS 流，防聆听窗音频残留
    if utt.size < SAMPLE_RATE * 0.2:
        _publish("result", sid, heard="", say="我在。有指令请直接说。",
                 verb="", m="", ok=True, tts="")
        return {"st": "think"}
    _publish("think", sid, heard="", say="", verb="", m="", ok=True, tts="")
    threading.Thread(target=_process_utterance, args=(sid, utt), daemon=True).start()
    return {"st": "think"}
