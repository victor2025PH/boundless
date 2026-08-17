# -*- coding: utf-8 -*-
"""桌面活体 HUD 服务（只跑中枢机，:7913）——集群实况板 P1（2026-08-07）。

出处：《集群实况板_五视角优化美化方案_20260807.md》§五 P1 / §15.6。
定位：hud.html（真 60fps 数字雨 + 六机实况）的宿主与数据推送端。
刻意与小界 :7912 分立——小界保持「纯标准库对话机器人」的极简立身之本，
本服务同样纯标准库，但快照构建**直接 import 小界的 build_context**：
判据同源不抄第二份（板子/小界/HUD 三张嘴永远说同一个数）。

端点：
  GET  /hud?machine=<id>   活体 HUD 页面（hud.html 同目录热改生效；kiosk/壁纸共用）
  GET  /tour               展演遥控页（手机大按钮；P0 展演骨架 · 2026-08-08）
  GET  /api/context        集群快照（= 小界 build_context + vram_flags 水位牌 + tour 幕次）
  GET  /api/history?n=30   history.jsonl 尾部 N 行（行内 VRAM 迷你趋势线数据源）
  GET  /api/stream         SSE：快照变更推送（≤3s 延迟）+ 55s 强制心跳帧 + 15s keepalive
                           （幕次切换走 Condition 即时唤醒：展演编排 <200ms 全屏到达）
  GET  /api/tour           当前幕次 {ok,tour,steps,pin_required}
  GET  /api/preflight      展演预检红绿单（快照新鲜/遥测/忙态/告警/手机站/观众数）
  GET  /icon/<id>.png      机器图标（中枢=无界主标，其余=primary_brand 产品图标）
  GET  /health             {"ok":true,...,"clients":N}
  POST /api/tour           {step:0-6|"next"|"prev", visitor?, pin?}——T0 只影响呈现层，
                           无任何运维动词；PIN 由环境变量 HUD_TOUR_PIN 决定（默认关=LAN 开放）
  POST /api/stat           HUD 埋点信标 {k,m,v,src} → hud_usage.jsonl（周报数据源；demo 不落账）
  POST /api/voice/stream   语音流式入口（?sid=机器，PCM16LE 16k 单声道；P1「小界」语音指挥，
                           判据与执行在 voice_gateway.py；verdict=danger 服务端拒收二闸）
  GET  /api/voice/state    语音链就绪态（模型加载/最近意图龄；诚实降级口径）
  GET  /api/voice/peek     回放耳（?sid=机器&s=秒，转写该会话最近缓冲=「机器听到了什么字」）
  GET  /api/voice/tts/<k>.wav  小界应答音频（内存缓存，不落盘）
  POST /api/desktop/push?machine=X   desktop_agent 推屏帧（image/jpeg，内存驻留不落盘；只读拉屏 P0）
  GET  /api/desktop/wanted?machine=X 本机是否被拉屏（agent 据此决定截不截屏，不打扰生产）
  POST /api/desktop/want   {machine,on}  HUD 拉屏时刷新「想看 X」（TTL 12s）
  POST /api/cockpit/throw  {screen,dir,machine}  C0 甩桌面跨屏（座次邻接路由→ctx.handoff 广播）
  POST /api/gest/beat      {screen,q,hands,src}  C1 指挥权心跳（纯函数仲裁→ctx.gestlead,迟滞接力）
  GET  /api/desktop/<X>.jpg  最新屏帧（陈旧 404）；GET /api/desktop/list  在推的屏

心跳：C:/Users/Public/boundless-hud/hud_server_state.json 每 30s 一行
（doctor「桌面HUD」读它判活性；写失败不致命）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent           # D:\boundless\tools\hud
ROOT = Path(r"D:\boundless")
sys.path.insert(0, str(ROOT / "tools" / "xiaojie"))
sys.path.insert(0, str(HERE))
import xiaojie_server as xj  # noqa: E402  判据同源：快照构建复用小界
import voice_gateway as vg   # noqa: E402  P1 语音指挥（模型惰性加载，导入零成本）
import ops_gateway as og     # noqa: E402  P2 隔空指挥（四重护栏；执行全走 Hub 既有 API）

PORT = 7913
HUD_HTML = HERE / "hud.html"
TOUR_HTML = HERE / "tour.html"
JOIN_HTML = HERE / "join.html"
SPEAKER_HTML = HERE / "speaker.html"
WAND_HTML = HERE / "wand.html"
RUNSHEET_HTML = HERE / "runsheet.html"

# P3 手机万能外设（2026-08-08）：轻角色注册表（扬声器/魔杖——摄像头/麦克风类角色
# 仍走中继 :7879，本表只管零采集角色）。内存驻留，45s 无心跳即离列；
# /api/stations 会把本表与中继手势站合并（星座卫星按角色着色的数据源）。
ROLES: dict[tuple[str, str], dict] = {}     # (sid, role) -> {ts, batt}
ROLES_FRESH_S = 45
WANDS: dict[str, dict] = {}                 # sid -> {x, y, ts}（魔杖光标，1.5s 新鲜窗）
WAND_FRESH_S = 1.5

# 隔空拉屏（P0 · 2026-08-09）：desktop_agent 只在「被拉屏」时推 JPEG 帧；中枢内存驻留不落盘。
# DESKTOP[mid] = {jpg, ts}；WANT[mid]=到期时刻（HUD 拉屏时刷新，agent 轮询 wanted 决定截不截）。
# 只读铁证：这里只存/转 JPEG，**没有任何往 agent 回传输入的通道**（P1/P2 才有，且经 ops_gateway）。
DESKTOP: dict[str, dict] = {}
DESKTOP_FRESH_S = 5
WANT: dict[str, float] = {}
WANT_TTL_S = 12
DESKTOP_MAX_BYTES = 4 * 1024 * 1024
# 拉屏可用性（P0b+ · 2026-08-10）：agent 每秒问一次 wanted=天然的存活信号，据此记录「哪台有活
# agent=现在可拉屏」——零改 agent。主控位/星座据此标可拉，doctor 出覆盖牌（演示前一眼知道能拉谁）。
AGENTS: dict[str, float] = {}
AGENT_FRESH_S = 6
VENDOR = HERE / "vendor"           # MediaPipe wasm/模型自托管（离线红线：零 CDN）
# 推理在 hud.html 主线程（动态 import bundle）：模块 worker 的 wasm 胶水有
# "ModuleFactory not set" 兼容坑（2026-08-07 真机实错），主线程为官方演示同路径。
AVATARHUB = Path(r"C:\模仿音色")
BRAND = ROOT / "brand-assets"
MACHINES = ROOT / "deploy" / "machines.json"

# [三模式联动 2026-08-14] modes.json 的 zh/accent 元数据（mtime 缓存：SSE 每 3s 一拍，
# 别每拍读盘解析）。HUD 配色/文案/模式按钮从此数据驱动——增删模式零改前端。
_MODES_JSON = ROOT / "deploy" / "boundless-hud" / "modes.json"
_MODES_CACHE: dict = {"mt": 0.0, "modes": {}}


def modes_meta() -> dict:
    try:
        mt = _MODES_JSON.stat().st_mtime
        if mt != _MODES_CACHE["mt"]:
            raw = json.loads(_MODES_JSON.read_text(encoding="utf-8-sig")).get("modes") or {}
            _MODES_CACHE["modes"] = {k: {"zh": str(v.get("zh") or k),
                                         "accent": str(v.get("accent") or "")}
                                     for k, v in raw.items()}
            _MODES_CACHE["mt"] = mt
    except Exception:
        pass
    return _MODES_CACHE["modes"]

VENDOR_MIME = {".mjs": "text/javascript", ".js": "text/javascript",
               ".wasm": "application/wasm", ".task": "application/octet-stream",
               ".map": "application/json"}
HISTORY = Path(r"C:\Users\Public\boundless-hud\history.jsonl")
BOARD_STATE = Path(r"C:\Users\Public\boundless-hud\board_state.json")
HEARTBEAT = Path(r"C:\Users\Public\boundless-hud\hud_server_state.json")
USAGE = Path(r"C:\Users\Public\boundless-hud\hud_usage.jsonl")

# VRAM 水位两档（判据单一真相在 render_cluster_board.py，此处只读它的计数结果；
# 阈值分钟数与其 VRAM_BADGE_MIN/VRAM_SAT_MIN 同值——hud_contract_test 逐值对账防漂移）
VRAM_BADGE_MIN = 10
VRAM_SAT_MIN = 15

_lock = threading.Lock()
_clients = 0

# 六屏逐屏在线（P3b 演示可靠性 · 2026-08-09）：每个 HUD 的 SSE 连接自报机器+输入模式，
# 中枢聚合成「哪块屏活着、开着手势/语音」——主控位一眼看全六屏，不用走过去。
# 键=连接自增 id（同机可多客户端）；断开即摘（SSE finally）；screens() 按机器聚合。
_screens: dict[int, dict] = {}
_screen_seq = 0
_MODES_RE = re.compile(r"^[a-z,]{0,40}$")


def screens() -> dict[str, dict]:
    now = time.time()
    out: dict[str, dict] = {}
    with _lock:
        conns = list(_screens.values())
    for c in conns:
        mid = c.get("machine") or "?"
        rec = out.setdefault(mid, {"live": True, "modes": set(), "n": 0, "since": now})
        rec["n"] += 1
        rec["modes"].update(c.get("modes") or [])
        rec["since"] = min(rec["since"], c.get("ts", now))
    for rec in out.values():
        rec["modes"] = sorted(rec["modes"])
        rec["age_s"] = round(now - rec.pop("since"))
    return out

# U 型指挥舱（C0 · 2026-08-13）：cockpit.json=屏↔机↔座次 SSOT（boundless deploy 目录，
# 与 modes/machines 同居）。mtime 缓存零 IO 常数;文件缺席=全部 C0 特性休眠（零回归）。
# 甩桌面跨屏=HANDOFF 单槽广播（seq 递增,ctx.handoff 随 SSE 下发,目标屏认领后 deskGrab）。
COCKPIT_JSON = Path(r"D:\boundless\deploy\boundless-hud\cockpit.json")
_cockpit_cache: dict = {"mtime": 0.0, "data": None}
HANDOFF = {"seq": 0, "target": "", "machine": "", "src_screen": "", "dir": "", "at": 0.0,
           "src": ""}


def cockpit() -> dict | None:
    try:
        mt = COCKPIT_JSON.stat().st_mtime
    except Exception:
        return None
    if mt != _cockpit_cache["mtime"]:
        try:
            d = json.loads(COCKPIT_JSON.read_text(encoding="utf-8-sig"))
            scr = sorted((d.get("screens") or []), key=lambda s: int(s.get("order") or 0))
            _cockpit_cache["data"] = {"layout": d.get("layout") or "U", "screens": scr}
            _cockpit_cache["mtime"] = mt
        except Exception:
            _cockpit_cache["data"] = None
    return _cockpit_cache["data"]


def cockpit_neighbor(screen_id: str, direction: str) -> dict | None:
    """按座次邻接（order 排序推导,不双写邻接表）。→ 目标屏 dict 或 None。
    命名目标（2026-08-13d）：dir=tv → 电视演示大屏;dir=center → 中央主屏——
    「上大屏」一步直达,不用沿邻接跳七跳（173 电视在左端、日常位在右端的现实）。"""
    ck = cockpit()
    if not ck:
        return None
    scr = ck["screens"]
    if direction in ("tv", "center"):
        tgt = next((s for s in scr if s.get(direction if direction == "tv" else "center")), None)
        return None if (not tgt or tgt.get("id") == screen_id) else tgt
    idx = next((i for i, s in enumerate(scr) if s.get("id") == screen_id), None)
    if idx is None:
        return None
    j = idx - 1 if direction == "left" else idx + 1
    return scr[j] if 0 <= j < len(scr) else None


# C1 多目接力·指挥权仲裁（2026-08-13）：多摄像头同时看到操作者时,只允许一块屏响应手势。
# 各屏保留本地识别（延迟不变、算力分摊）,只上报「手感质量」心跳（≤5Hz,几十字节）;
# 中枢用纯函数仲裁唯一指挥权:迟滞防抖（挑战者要明显更强才接力,防两摄边界抖动）,
# 候选过期/空手即释放。GESTLEAD 单槽经 ctx.gestlead 随 SSE 广播,变更即时唤醒。
GEST_FRESH_S = 1.5   # 候选新鲜窗：心跳断流超过即视为掉线（源崩溃/转身离场自动让位）
GEST_KEEP = 0.75     # 迟滞：在任领导者质量 >= 最佳者*0.75 即保席位（防抖动接力拉锯）
GESTLEAD = {"screen": "", "seq": 0, "at": 0.0, "beat_at": 0.0}
_gest_cand: dict[str, dict] = {}   # screen -> {"q": float, "ts": float}


def gest_arbitrate(cands: dict, cur: str, now: float) -> str:
    """多摄像头指挥权仲裁（纯函数,_gestlead_verify.py 红绿标定）。
    只考虑新鲜(≤GEST_FRESH_S)且 q>0 的候选;无候选=释放("");
    在任者新鲜且 q >= 最佳*GEST_KEEP 即连任;否则最佳者接任。"""
    fresh = {s: c for s, c in cands.items()
             if now - float(c.get("ts") or 0) <= GEST_FRESH_S and float(c.get("q") or 0) > 0}
    if not fresh:
        return ""
    best = max(fresh, key=lambda s: fresh[s]["q"])
    if cur in fresh and fresh[cur]["q"] >= fresh[best]["q"] * GEST_KEEP:
        return cur
    return best


# 逐层下钻数据端（P3 · 2026-08-12）：按机服务清单+死活。归属=cluster_map.json（拓扑 SSOT，
# hosts[ip].svcs[].name）+ 未被远端认领的健康项归中枢（Hub 本机跑全栈的事实近似）；
# 死活=Hub /health services（28 项布尔，探测判据在 Hub 侧单一真相）。15s 缓存；
# Hub 掉线保留上次数据（svc 空=前端自动隐藏下钻口，诚实降级）。
_svc_cache: dict = {"ts": 0.0, "data": {}}


def svc_by_machine() -> dict:
    now = time.time()
    if now - _svc_cache["ts"] < 15:
        return _svc_cache["data"]
    _svc_cache["ts"] = now          # 先盖时间戳：Hub 掉线时不至于每个 SSE 拍都重试 3s 拖慢广播
    try:
        with urllib.request.urlopen(xj.HUB + "/health", timeout=3) as r:
            health = json.loads(r.read().decode("utf-8", "replace")).get("services") or {}
        if isinstance(health, dict) and health:
            ip2mid = {m.get("ip"): m.get("id") for m in xj._machines() if m.get("ip")}
            cm = json.loads((AVATARHUB / "cluster_map.json").read_text(encoding="utf-8"))
            claimed: dict[str, str] = {}
            for ip, host in (cm.get("hosts") or {}).items():
                mid = ip2mid.get(ip)
                for s in (host.get("svcs") or []) if mid else []:
                    if s.get("name"):
                        claimed[str(s["name"])] = mid
            hub_mid = ip2mid.get((cm.get("hub") or {}).get("ip")) or "zhongshu"
            # 可救合法集=ops_gateway 同一函数（判据单一真相：/api/engine/list 的死引擎；
            # 下钻只给这些标「可救」红星，其余 down 标灰=泊车/按需态，防告警疲劳）
            try:
                fixable = set(og._dead_engines())
            except Exception:
                fixable = set()
            data: dict[str, list] = {}
            for name, up in sorted(health.items()):
                rec = {"n": name, "up": bool(up)}
                if not up and name in fixable:
                    rec["fix"] = True           # 可救=在 svc_restart 合法集（红星）；其余 down=泊车灰
                data.setdefault(claimed.get(name, hub_mid), []).append(rec)
            _svc_cache["data"] = data
    except Exception:
        pass                        # 保留上次数据（新鲜度换可用性；下一拍再试）
    return _svc_cache["data"]


# 秒级遥测（P3 遥测切片 · 2026-08-07）：六机 telemetry_agent.ps1 每 3s POST 一份
# GPU/显存/温度；内存驻留不落盘（长期趋势仍归分钟级 history.jsonl，密度分层）。
# 新鲜窗 12s：超窗自动回落到小界快照的分钟级数值——数据永远有，只是新鲜度分级。
TELEM: dict[str, dict] = {}
TELEM_FRESH_S = 12

# ---------------- 展演模式（P0 展演骨架 · 2026-08-08） ----------------
# 出处《未来感机房展演_手势语音指挥与手机万能外设_五视角优化美化方案_20260808.md》§三/§七。
# 状态在服务端（六屏同帧的技术保证）：tour 并入 build_ctx 快照走既有 SSE，
# 迟到/刷新的屏自动对齐当前幕；幕次切换经 Condition 即时唤醒所有 SSE 客户端
# （原 3s 轮询的相位差会把 0.45s/屏 的波纹接力节拍完全打散——实施中抓出的问题）。
# 编排对时用服务端时钟：ctx 带 srv_epoch，客户端算钟差后按服务器时间轴播放
# （机间时钟漂移的老问题，age_s 服务端计算的同一防线）。
TOUR_STEPS = ["常态", "欢迎", "手势", "语音", "手机", "点火", "谢幕"]   # 0..6，与遥控页/HUD 同源
TOUR = {"step": 0, "name": TOUR_STEPS[0], "seq": 0, "at": 0.0, "visitor": ""}
_tour_cond = threading.Condition()
TOUR_PIN = os.environ.get("HUD_TOUR_PIN", "")      # 默认空=LAN 开放（遥控只有 T0 呈现动词）
_VISITOR_RE = re.compile(r"[<>&\"'\\\x00-\x1f]")


def set_tour(step: int, visitor=None) -> dict:
    """切幕：更新状态并即时唤醒全部 SSE 客户端。返回新状态快照。"""
    with _tour_cond:
        TOUR["step"] = step
        TOUR["name"] = TOUR_STEPS[step]
        TOUR["seq"] += 1
        TOUR["at"] = round(time.time(), 3)
        if visitor is not None:
            TOUR["visitor"] = _VISITOR_RE.sub("", str(visitor))[:24].strip()
        _tour_cond.notify_all()
        return dict(TOUR)


# ---------------- 埋点信标（hud_usage_report 周报数据源） ----------------
# 六机 HUD 页面统一 POST 到中枢本服务 → 单一 jsonl；demo 页面不发信标（客户端拦）。
# 「四周零使用提退役」家规与 §14 审计周报都吃这份账。
_usage_lock = threading.Lock()
_STAT_K_RE = re.compile(r"^[a-z0-9_]{2,32}$")
USAGE_MAX_BYTES = 4 * 1024 * 1024
USAGE_KEEP_LINES = 20000


def log_stat(k: str, m: str = "", v: str = "", src: str = "") -> bool:
    if not _STAT_K_RE.match(k):
        return False
    rec = {"ts": round(time.time()), "k": k}
    if m:
        rec["m"] = str(m)[:24]
    if v:
        # voice_cap 是诊断信标（采集层状态+选麦决策审计），48 字符装不下探针行
        rec["v"] = str(v)[:400 if k == "voice_cap" else 48]
    if src:
        rec["src"] = str(src)[:16]
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with _usage_lock:
        try:
            USAGE.parent.mkdir(parents=True, exist_ok=True)
            with USAGE.open("a", encoding="utf-8") as f:
                f.write(line)
            if USAGE.stat().st_size > USAGE_MAX_BYTES:   # 截尾（history.jsonl 同款纪律）
                lines = USAGE.read_text(encoding="utf-8", errors="replace").splitlines()
                tmp = USAGE.with_suffix(".tmp")
                tmp.write_text("\n".join(lines[-USAGE_KEEP_LINES:]) + "\n", encoding="utf-8")
                tmp.replace(USAGE)
        except OSError:
            return False
    return True


def _read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return default


def _machines_data() -> dict:
    return _read_json(MACHINES, {}) or {}


def vram_flags() -> dict:
    """把板子 board_state 里的水位计数翻译成 {id: "high"|"sat"}——与壁纸板同一判据。"""
    st = _read_json(BOARD_STATE, {}) or {}
    out = {}
    for mid, rec in (st.get("vram_high") or {}).items():
        if not isinstance(rec, dict):
            continue
        if int(rec.get("c98") or 0) >= VRAM_SAT_MIN:
            out[mid] = "sat"
        elif int(rec.get("c95") or 0) >= VRAM_BADGE_MIN:
            out[mid] = "high"
    return out


LAST_VERDICT = "safe"   # 语音流式入口的忙态缓存（SSE 循环 ≤3s 刷新；8 次/秒的音频 POST
                        # 不能每次重建快照——这是聆听链路的性能地板）


def build_ctx() -> dict:
    global LAST_VERDICT
    ctx = xj.build_context()
    ctx["vram_flags"] = vram_flags()
    # [P0-1 界面版本自愈 2026-08-16] hud.html 的 mtime 即资产版本：页面首帧记基线，
    # 后续帧不一致=盘上文件已更新→出「界面已更新」胶囊+空闲自动刷新。长驻 kiosk 窗
    # 吃旧界面（服务端改完、开着的屏永远不知道）的事故类就此根治。stat 一次 ~µs 级。
    try:
        ctx["asset_ver"] = int(HUD_HTML.stat().st_mtime)
    except OSError:
        pass
    LAST_VERDICT = ctx.get("verdict") or "safe"
    now = time.time()
    fresh = 0
    for m in ctx.get("machines") or []:
        t = TELEM.get(m.get("id") or "")
        if t and now - t["ts"] <= TELEM_FRESH_S:
            # 秒级覆盖分钟级（同一仪器 nvidia-smi，仅新鲜度分层）
            if t["u"] >= 0:
                m["gpu_util"] = t["u"]
            if t["vu"] >= 0:
                m["vram_used_mb"] = t["vu"]
            if t["vt"] > 0:
                m["vram_total_mb"] = t["vt"]
            if t["temp"] >= 0:
                m["temp"] = t["temp"]
            m["fresh"] = True
            fresh += 1
    ctx["telem_fresh"] = fresh
    ctx["tour"] = dict(TOUR)                      # 展演幕次随快照下发（迟到的屏自动对齐）
    ctx["voice"] = dict(vg.VOICE_LAST)            # 语音相位/字幕随快照广播（六屏字幕带同源）
    try:
        ctx["voice"]["dictate"] = bool(vg.status().get("dictate"))   # P2b 语音打字开关（HUD 指示）
    except Exception:
        ctx["voice"]["dictate"] = False
    ctx["ops"] = dict(og.OPS_LAST)                # 隔空指挥相位（armed/fired/denied 六屏同源）
    try:
        import face_auth  # noqa: PLC0415
        ctx["ops"]["auth"] = "face" if face_auth.enabled() else "presence"   # 武装卡提示语用
    except Exception:
        ctx["ops"]["auth"] = "presence"
    ctx["wands"] = sorted({sid for sid, w in WANDS.items()
                           if now - w["ts"] <= WAND_FRESH_S + 3})   # 布尔级：目标机见到自己才开拉取
    ctx["pullable"] = sorted(m for m, t in AGENTS.items() if now - t <= AGENT_FRESH_S)  # 有活 agent=可拉屏
    ctx["svc"] = svc_by_machine()                 # P3 下钻：按机服务清单+死活（15s 缓存，掉线保上次）
    ck = cockpit()                                # C0 指挥舱：座次台账+甩桌面单槽（缺席=特性休眠）
    if ck:
        ctx["cockpit"] = ck
        if HANDOFF["seq"]:
            ctx["handoff"] = dict(HANDOFF)
        if GESTLEAD["seq"]:
            ctx["gestlead"] = dict(GESTLEAD)      # C1 指挥权单槽（beat_at=客户端新鲜度闸的判据）
    try:
        cs = og.ctrl_state()                     # 隔空受控相位（谁在被操控；六屏同源，不含 token）
        ctx["ctrl"] = {"session": cs.get("session"), "deployed": cs.get("deployed"),
                       "alive": cs.get("alive"), "targets": cs.get("targets"), "flag": cs.get("flag")}
    except Exception:
        ctx["ctrl"] = {"session": None, "deployed": [], "alive": [], "targets": [], "flag": False}
    try:
        # 集群算力模式（P2 全息舰桥联动配色；与 render_cluster_board v5 同源读 logs/cluster_mode.json。
        # 2026-08-14 三模式联动：带 zh/accent/切换进度/全模式表——HUD 不再硬编码双模式配色文案，
        # 保持直读文件旁路=Hub 死了实况面也活着，刻意不换成 /api/cluster/board）
        cm = json.loads((AVATARHUB / "logs" / "cluster_mode.json").read_text(encoding="utf-8"))
        _lr = cm.get("last_result") or {}
        _mm = modes_meta()
        _cur = str(cm.get("mode") or "")
        _mi = _mm.get(_cur) or {}
        _pg = cm.get("progress") if cm.get("switching") else None
        ctx["clustermode"] = {"mode": _cur, "switching": bool(cm.get("switching")),
                              "since": cm.get("since_str") or "",
                              "zh": _mi.get("zh") or _cur, "accent": _mi.get("accent") or "",
                              "progress": ({"done": int(_pg.get("done") or 0),
                                            "total": int(_pg.get("total") or 0),
                                            "note": str(((_pg.get("current") or {}).get("note")) or "")[:60]}
                                           if isinstance(_pg, dict) else None),
                              "modes": [{"id": k, "zh": v["zh"], "accent": v["accent"]}
                                        for k, v in _mm.items()],
                              # 快切改造（2026-08-15）：切换同步段收口即完成，重型引擎后台暖机
                              "warming": ({"pending": int((cm.get("warming") or {}).get("pending") or 0),
                                           "failed": len((cm.get("warming") or {}).get("failed") or [])}
                                          if cm.get("warming") else None),
                              "last_ok": _lr.get("ok"), "last_target": _lr.get("target") or ""}
    except Exception:
        ctx["clustermode"] = {"mode": "", "switching": False, "since": ""}
    try:
        # 姿态巡检（P0 2026-08-13）：slo_watch 10min 一巡落 cluster_posture.json，HUD/壁纸同读。
        # 30min 龄内才认（巡检停摆时不拿旧结论装健康）；skip（切换中/未初始化）不外显。
        po = json.loads((AVATARHUB / "logs" / "cluster_posture.json").read_text(encoding="utf-8"))
        if time.time() - float(po.get("ts") or 0) < 1800 and not po.get("skip"):
            ctx["clustermode"]["posture"] = {
                "ok": bool(po.get("ok")), "errors": len(po.get("errors") or []),
                "warns": len(po.get("warns") or []), "checked": int(po.get("checked") or 0),
                "top": str((po.get("errors") or po.get("warns") or [""])[0])[:80]}
    except Exception:
        pass
    try:
        # 操作即叙事（§14.4）：最近点火/撤销拼进事件流头部（15min 窗，最多 2 条）
        ev_ops = og.recent_events(2)
        if ev_ops:
            ctx["events"] = (ev_ops + (ctx.get("events") or []))[:8]
    except Exception:
        pass
    ctx["srv_epoch"] = round(now, 3)              # 服务端时钟：客户端算钟差用（编排对时）
    return ctx


def history_tail(n: int = 30) -> list[dict]:
    try:
        lines = HISTORY.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def preflight() -> dict:
    """展演预检红绿单（开演前 10 分钟跑一遍的那张单子）。
    level: ok=绿 / warn=红牌（会影响演出效果）/ info=黄牌（不拦但要知道）。"""
    checks: list[dict] = []

    def add(name: str, level: str, note: str):
        checks.append({"name": name, "level": level, "note": note})

    try:
        ctx = build_ctx()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "checks": [{"name": "快照构建", "level": "warn",
                                         "note": f"build_context 异常：{str(e)[:80]}"}]}
    age = ctx.get("age_s")
    add("集群快照", "ok" if (age is not None and age <= 90) else "warn",
        f"快照年龄 {age}s" if age is not None else "无快照")
    ms = ctx.get("machines") or []
    bad = [m.get("zh") or m.get("id") for m in ms if m.get("status") != "ok"]
    add("六机在线", "ok" if not bad else "warn",
        "全员在线" if not bad else "异常：" + "、".join(str(b) for b in bad))
    tf = int(ctx.get("telem_fresh") or 0)
    add("秒级遥测", "ok" if tf >= 5 else ("info" if tf >= 3 else "warn"), f"{tf}/6 机在报")
    v = ctx.get("verdict")
    add("忙态", "ok" if v == "safe" else "info",
        {"safe": "safe · 点火幕可武装", "wait": "wait · 有长任务（第 5 幕将被忙态否决）",
         "danger": "danger · 推流中（展演动画会被总督冻结）"}.get(v, f"未知 {v}"))
    firing = [e for e in (ctx.get("events") or []) if e.get("state") == "firing" and not e.get("ack")]
    add("在燃告警", "ok" if not firing else "info",
        "无" if not firing else f"{len(firing)} 条未确认（屏上会亮着，介意就先 ack）")
    try:
        with urllib.request.urlopen("http://127.0.0.1:7878/stations", timeout=2) as up:
            st = json.loads(up.read().decode("utf-8", "replace")).get("stations") or {}
        n = sum(1 for s in st.values() if s.get("connected"))
        add("手机站", "ok" if n else "info", f"{n} 台在列" if n else "0 台在列（第 4 幕要现场扫码）")
    except Exception:
        add("手机站", "info", "中继 :7878 未启动（第 4 幕不可用）")
    with _lock:
        n = _clients
    scr = screens()
    fleet = [m.get("id") for m in (_machines_data().get("machines") or [])]
    zh = {m.get("id"): m.get("zh") for m in (_machines_data().get("machines") or [])}
    live = [mid for mid in fleet if mid in scr]
    miss = [mid for mid in fleet if mid not in scr]
    if fleet:
        badge = " ".join(f"{zh.get(m, m)}✓" for m in live) + \
                ("　缺：" + "、".join(zh.get(m, m) for m in miss) if miss else "")
        add("六屏在线", "ok" if not miss else "warn", f"{len(live)}/{len(fleet)}屏 · {badge}")
    else:
        add("观众屏", "ok" if n >= 1 else "warn", f"{n} 块屏连着 SSE" if n else "没有屏在听")
    try:
        # 语音采集屏数（2026-08-13 工单固化：voice=1 逐屏 opt-in,跑单漏参=全场没耳朵在听,
        # 「说了小界没响应」当场懵——演出前这张单子必须能看出「谁在听、听的是哪支麦」）
        vst = vg.status()
        mics = vst.get("mics") or {}
        if mics:
            det = "、".join(f"{k}(rms {v.get('rms', 0):.3f})" for k, v in list(mics.items())[:4])
            add("语音采集", "ok", f"{len(mics)} 屏在听 · {det}")
        else:
            add("语音采集", "warn" if vst.get("ready") else "info",
                "0 屏在听——主控屏 URL 要带 &voice=1（说「小界」不会有任何响应）")
    except Exception:
        add("语音采集", "info", "语音态不可读")
    add("遥控 PIN", "info", "已启用" if TOUR_PIN else "未启用（LAN 内任何人可切幕；仅 T0 呈现层）")
    try:
        ost = og.state()
        who = "、".join(ost.get("operators") or [])
        add("点火授权", "ok" if ost.get("auth_mode") == "presence+face" else "info",
            f"刷脸层开 · 操作员：{who}" if who else "纯在场信号（登记操作员后自动升级刷脸）")
    except Exception:
        add("点火授权", "info", "状态不可读")
    ok = not any(c["level"] == "warn" for c in checks)
    return {"ok": ok, "checks": checks, "ts": round(time.time())}


def icon_path(mid: str) -> Path | None:
    data = _machines_data()
    fleet = {m.get("id"): m for m in data.get("machines", [])}
    if mid not in fleet:
        return None
    if mid == str((data.get("hub") or {}).get("id") or ""):
        return BRAND / "01_logos" / "mark" / "boundless-mark-128.png"
    b = fleet[mid].get("primary_brand") or "voicex"
    return BRAND / "02_product-icons" / b / f"{b}-128.png"


def heartbeat_loop() -> None:
    while True:
        try:
            hist = history_tail(1)
            age = round(time.time() - float(hist[-1]["ts"])) if hist else None
            with _lock:
                n = _clients
            now = time.time()
            telem_ages = {k: round(now - v["ts"]) for k, v in TELEM.items()}
            tmp = HEARTBEAT.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "ts": round(now), "clients": n, "hist_age_s": age, "port": PORT,
                "telem": telem_ages,
                "voice": vg.status(),            # doctor「语音指挥」读它
                "ops": og.state(),               # doctor「隔空指挥」读它（旗标/熔断/审计龄）
                "ctrl": og.ctrl_state(),         # doctor「隔空受控」读它（白名单/已部署/总闸/熔断）
                "pull": sorted(m for m, t in AGENTS.items() if now - t <= AGENT_FRESH_S),  # doctor「拉屏覆盖」
            }), encoding="utf-8")
            tmp.replace(HEARTBEAT)
        except Exception:
            pass
        time.sleep(30)


def _stable_key(ctx: dict) -> str:
    """变更检测键：剔除每秒都在变的字段，避免 SSE 空转推送。
    srv_epoch 是对时载体不是数据（每次构建都变），必须剔除；tour 保留在键里
    ——幕次一变（seq++）键即变，SSE 立即推送。"""
    return json.dumps({k: v for k, v in ctx.items() if k not in ("ts", "age_s", "srv_epoch")},
                      ensure_ascii=False, sort_keys=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "BoundlessHud/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              cache: str = "no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _stream(self):
        global _clients, _screen_seq
        q = parse_qs(urlparse(self.path).query)
        mid = (q.get("machine") or [""])[0].strip().lower()[:24]
        modes_raw = (q.get("modes") or [""])[0].strip().lower()
        modes = modes_raw.split(",") if (modes_raw and _MODES_RE.match(modes_raw)) else []
        modes = [m for m in modes if m in ("gesture", "voice", "phone")]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with _lock:
            _clients += 1
            _screen_seq += 1
            conn_id = _screen_seq
            if mid:
                _screens[conn_id] = {"machine": mid, "modes": modes, "ts": time.time()}
        last_key, last_sent, last_io = "", 0.0, 0.0
        try:
            while True:
                ctx = build_ctx()
                key = _stable_key(ctx)
                now = time.time()
                if key != last_key or now - last_sent > 55:
                    self.wfile.write(("data: " + json.dumps(ctx, ensure_ascii=False) + "\n\n")
                                     .encode("utf-8"))
                    self.wfile.flush()
                    last_key, last_sent, last_io = key, now, now
                elif now - last_io > 15:
                    self.wfile.write(b": ka\n\n")   # keepalive 注释帧
                    self.wfile.flush()
                    last_io = now
                # 3s 兜底轮询 + 幕次切换即时唤醒（set_tour notify_all）：
                # 展演编排的跨屏节拍是 0.45s/屏，靠轮询相位差会被打散
                with _tour_cond:
                    _tour_cond.wait(timeout=3)
        except OSError:
            pass
        finally:
            with _lock:
                _clients -= 1
                _screens.pop(conn_id, None)

    def _station_feed(self, sid: str):
        """手机手势站 MJPEG 同源代理（手机集群方案 阶段一）：HUD 页面直连 :7878 属跨源，
        画布会被污染、MediaPipe 读不了像素——经本服务转发即同源。上游不在→502，
        HUD 端 img.onerror 自行退避重试。"""
        if not re.match(r"^[a-z0-9_-]{2,24}$", sid):
            self._send(404, b"{}")
            return
        try:
            up = urllib.request.urlopen(f"http://127.0.0.1:7878/station/{sid}.mjpeg", timeout=6)
        except Exception:
            self._send(502, json.dumps({"error": "station relay offline"}).encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", up.headers.get("Content-Type",
                                                        "multipart/x-mixed-replace; boundary=mjpegframe"))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                chunk = up.read(16384)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except OSError:
            pass
        finally:
            try:
                up.close()
            except Exception:
                pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/hud"):
            try:
                self._send(200, HUD_HTML.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, b"hud.html missing", "text/plain")
        elif u.path == "/tour":
            try:
                self._send(200, TOUR_HTML.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, b"tour.html missing", "text/plain")
        elif u.path in ("/join", "/speaker", "/wand", "/runsheet"):
            p = {"/join": JOIN_HTML, "/speaker": SPEAKER_HTML, "/wand": WAND_HTML,
                 "/runsheet": RUNSHEET_HTML}[u.path]
            try:
                self._send(200, p.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, f"{p.name} missing".encode(), "text/plain")
        elif u.path.startswith("/api/wand/"):
            sid = u.path[len("/api/wand/"):]
            w = WANDS.get(sid)
            if w and time.time() - w["ts"] <= WAND_FRESH_S:
                self._send(200, json.dumps({"ok": True, "x": w["x"], "y": w["y"]}).encode())
            else:
                self._send(404, b'{"ok": false}')
        elif u.path == "/api/desktop/wanted":
            # desktop_agent 轮询：本机现在是否被中枢拉屏（决定截不截屏——不打扰生产）
            mid = (parse_qs(u.query).get("machine") or [""])[0].strip().lower()
            if re.match(r"^[a-z0-9_-]{2,24}$", mid):
                AGENTS[mid] = time.time()          # 轮询即存活信号=该机可拉屏（零改 agent）
            on = mid in WANT and time.time() < WANT[mid]
            self._send(200, json.dumps({"wanted": bool(on)}).encode())
        elif u.path == "/api/desktop/list":
            now = time.time()
            live = {mid: {"age_s": round(now - d["ts"])}
                    for mid, d in DESKTOP.items() if now - d["ts"] <= DESKTOP_FRESH_S}
            self._send(200, json.dumps({"ok": True, "desktops": live}, ensure_ascii=False).encode())
        elif u.path.startswith("/api/desktop/") and u.path.endswith(".jpg"):
            mid = u.path[len("/api/desktop/"):-4]
            d = DESKTOP.get(mid)
            if d and time.time() - d["ts"] <= DESKTOP_FRESH_S:
                self._send(200, d["jpg"], "image/jpeg")
            else:
                self._send(404, b"{}")
        elif u.path == "/kiosk.ps1":
            # 驾驶舱外壳分发口：新机一行自举（iwr 本文件 → -Register 即入列开机自启）
            try:
                body = (HERE / "cockpit_kiosk.ps1").read_bytes()
                self._send(200, body, "text/plain; charset=utf-8")
            except Exception:
                self._send(404, b"{}")
        elif u.path == "/api/cockpit":
            # 驾驶舱外壳取账口（2026-08-13e）：台账+机器IP映射——cockpit_kiosk.ps1 据此
            # 自认机器、拼每屏 URL、开机自启全屏点亮（人肉拼 URL 时代的三类工单就此终结）
            ck0 = cockpit() or {"screens": []}
            mach = {m.get("id"): {"ip": m.get("ip", ""), "zh": m.get("zh", "")}
                    for m in (_machines_data().get("machines") or [])}
            self._send(200, json.dumps(
                {"ok": True, "cockpit": ck0, "machines": mach, "hub_port": PORT},
                ensure_ascii=False).encode("utf-8"))
        elif u.path == "/api/tour":
            self._send(200, json.dumps({"ok": True, "tour": dict(TOUR), "steps": TOUR_STEPS,
                                        "pin_required": bool(TOUR_PIN), "screens": screens()},
                                       ensure_ascii=False).encode("utf-8"))
        elif u.path == "/api/preflight":
            try:
                self._send(200, json.dumps(preflight(), ensure_ascii=False).encode("utf-8"))
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"ok": False, "error": str(e)[:200]}).encode())
        elif u.path == "/api/voice/state":
            st = vg.status()
            st["ok"] = True
            self._send(200, json.dumps(st, ensure_ascii=False).encode("utf-8"))
        elif u.path == "/api/voice/peek":
            # 回放耳：转写某会话最近几秒缓冲（现场诊断「喊了没反应」；音频不落盘）
            q = parse_qs(u.query)
            sid = (q.get("sid") or ["zhongshu"])[0].strip().lower()
            try:
                sec = min(8.0, max(1.0, float((q.get("s") or ["6"])[0])))
            except ValueError:
                sec = 6.0
            try:
                if (q.get("wav") or ["0"])[0] == "1":
                    pcm = vg.peek_raw(sid, sec)
                    import io
                    import wave as _wave
                    bio = io.BytesIO()
                    with _wave.open(bio, "wb") as w:
                        w.setnchannels(1)
                        w.setsampwidth(2)
                        w.setframerate(16000)
                        w.writeframes(pcm)
                    self._send(200, bio.getvalue(), "audio/wav")
                else:
                    self._send(200, json.dumps(vg.peek(sid, sec), ensure_ascii=False).encode("utf-8"))
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"ok": False, "error": str(e)[:150]}).encode())
        elif u.path == "/api/ops/state":
            st = og.state()
            st["ok"] = True
            self._send(200, json.dumps(st, ensure_ascii=False).encode("utf-8"))
        elif u.path == "/api/ctrl/state":
            st = og.ctrl_state()
            st["ok"] = True
            self._send(200, json.dumps(st, ensure_ascii=False).encode("utf-8"))
        elif u.path == "/api/ctrl/pull":
            # control_agent 轮询（P2 输入注入）：密钥在 header，判据全在 ops_gateway（fail-closed）
            mid = (parse_qs(u.query).get("machine") or [""])[0].strip().lower()
            secret = self.headers.get("X-Ctrl-Secret") or ""
            res = og.ctrl_pull(mid, secret)
            self._send(200, json.dumps(res, ensure_ascii=False).encode("utf-8"))
        elif u.path.startswith("/api/voice/tts/"):
            key = u.path[len("/api/voice/tts/"):]
            key = key[:-4] if key.endswith(".wav") else key
            wav = vg.tts_bytes(key)
            if wav:
                self._send(200, wav, "audio/wav")
            else:
                self._send(404, b"{}")
        elif u.path == "/api/context":
            try:
                self._send(200, json.dumps(build_ctx(), ensure_ascii=False).encode("utf-8"))
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(e)}).encode())
        elif u.path == "/api/history":
            try:
                n = max(2, min(240, int((parse_qs(u.query).get("n") or ["30"])[0])))
            except ValueError:
                n = 30
            self._send(200, json.dumps(history_tail(n), ensure_ascii=False).encode("utf-8"))
        elif u.path == "/api/stream":
            self._stream()
        elif u.path.startswith("/vendor/"):
            # MediaPipe 自托管资产：路径穿越防护（resolve 后必须仍在 vendor 下）
            rel = u.path[len("/vendor/"):]
            p = (VENDOR / rel).resolve()
            if not str(p).startswith(str(VENDOR.resolve())) or not p.is_file():
                self._send(404, b"{}")
                return
            ctype = VENDOR_MIME.get(p.suffix.lower(), "application/octet-stream")
            self._send(200, p.read_bytes(), ctype, cache="max-age=86400")
        elif u.path == "/api/stations":
            # 手机外设注册表：中继手势站透传 + 本服务轻角色（扬声器/魔杖）合并
            # （星座卫星按角色着色：粉=手势 金=扬声器 紫=魔杖）
            merged: dict = {}
            try:
                with urllib.request.urlopen("http://127.0.0.1:7878/stations", timeout=3) as up:
                    for sid, st in (json.loads(up.read()).get("stations") or {}).items():
                        if isinstance(st, dict):
                            st.setdefault("role", "gesture")
                            merged[sid] = st
            except Exception:
                pass
            now = time.time()
            for (sid, role), rec in list(ROLES.items()):
                if now - rec["ts"] > ROLES_FRESH_S:
                    ROLES.pop((sid, role), None)
                    continue
                if sid not in merged:      # 手势站占位优先（一机一卫星，角色即租约）
                    merged[sid] = {"connected": True, "role": role, "batt": rec.get("batt")}
            self._send(200, json.dumps({"ok": True, "stations": merged},
                                       ensure_ascii=False).encode("utf-8"))
        elif u.path.startswith("/station_feed/"):
            self._station_feed(u.path[len("/station_feed/"):])
        elif u.path.startswith("/icon/"):
            name = u.path[len("/icon/"):]
            mid = name[:-4] if name.endswith(".png") else ""
            p = icon_path(mid) if mid else None
            if p and p.is_file():
                self._send(200, p.read_bytes(), "image/png", cache="max-age=3600")
            else:
                self._send(404, b"{}")
        elif u.path == "/health":
            with _lock:
                n = _clients
            now = time.time()
            tf = sum(1 for v in TELEM.values() if now - v["ts"] <= TELEM_FRESH_S)
            self._send(200, json.dumps({"ok": True, "service": "hud", "port": PORT,
                                        "clients": n, "telem_fresh": tf}).encode())
        else:
            self._send(404, b"{}")


def _ack_alert(key: str, by: str) -> bool:
    """HUD 唯一写操作（P2）：确认告警。判据与执行都在 avatarhub 的 alerts.py
    （import 调用，不抄第二份、不开新特权通道——ops_gateway 纪律的第一次实践）。"""
    sys.path.insert(0, str(AVATARHUB))
    import alerts  # noqa: PLC0415
    return bool(alerts.ack_alert(str(key), by=str(by)[:40]))


class HandlerWithPost(Handler):
    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/desktop/push":
            # desktop_agent 推帧（image/jpeg 二进制）：只驻内存，不落盘
            mid = (parse_qs(urlparse(self.path).query).get("machine") or [""])[0].strip().lower()
            if not re.match(r"^[a-z0-9_-]{2,24}$", mid):
                self._send(400, b'{"ok": false}')
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                jpg = self.rfile.read(n) if 0 < n <= DESKTOP_MAX_BYTES else b""
                if jpg[:2] == b"\xff\xd8":                 # JPEG SOI 校验
                    DESKTOP[mid] = {"jpg": jpg, "ts": time.time()}
                    self._send(200, b'{"ok": true}')
                else:
                    self._send(400, b'{"ok": false}')
            except Exception:
                self._send(500, b'{"ok": false}')
            return
        if u.path == "/api/voice/stream":
            # 语音流式入口（PCM16LE 16k 单声道二进制；音频只走内存，进函数栈即弃）
            q = parse_qs(u.query)
            sid = (q.get("sid") or [""])[0].strip().lower()
            if not re.match(r"^[a-z0-9_-]{2,24}$", sid):
                self._send(400, json.dumps({"ok": False}).encode())
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if 0 < n <= 262144 else b""
                res = vg.feed(sid, raw, verdict=LAST_VERDICT,
                              writer=(q.get("w") or [""])[0][:40])
                self._send(200, json.dumps(res, ensure_ascii=False).encode("utf-8"))
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"st": "err", "error": str(e)[:120]}).encode())
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8-sig", "replace")) if n else {}
        except Exception:
            payload = {}
        if u.path == "/api/telemetry":
            mid = str(payload.get("id") or "").strip().lower()
            if not re.match(r"^[a-z0-9_-]{2,24}$", mid):
                self._send(400, json.dumps({"ok": False}).encode())
                return
            try:
                TELEM[mid] = {
                    "vu": int(payload.get("vu") if payload.get("vu") is not None else -1),
                    "vt": int(payload.get("vt") if payload.get("vt") is not None else -1),
                    "u": int(payload.get("u") if payload.get("u") is not None else -1),
                    "temp": int(payload.get("temp") if payload.get("temp") is not None else -1),
                    "ts": time.time(),
                }
                self._send(200, b'{"ok": true}')
            except Exception:
                self._send(400, json.dumps({"ok": False}).encode())
        elif u.path == "/api/alerts/ack":
            key = str(payload.get("key") or "")
            if not key:
                self._send(400, json.dumps({"ok": False, "error": "no key"}).encode())
                return
            try:
                ok = _ack_alert(key, str(payload.get("by") or "hud"))
                self._send(200, json.dumps({"ok": ok}).encode())
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"ok": False, "error": str(e)[:200]}).encode())
        elif u.path == "/api/tour":
            # 展演切幕（T0：只影响呈现层，无任何运维动词——ops_gateway 纪律的呈现侧）
            if TOUR_PIN and str(payload.get("pin") or "") != TOUR_PIN:
                self._send(401, json.dumps({"ok": False, "error": "pin"}).encode())
                return
            raw = payload.get("step")
            if raw == "next":
                step = (TOUR["step"] + 1) % len(TOUR_STEPS)
            elif raw == "prev":
                step = (TOUR["step"] - 1) % len(TOUR_STEPS)
            else:
                try:
                    step = int(raw)
                except (TypeError, ValueError):
                    step = -1
            if not 0 <= step < len(TOUR_STEPS):
                self._send(400, json.dumps({"ok": False, "error": "bad step"}).encode())
                return
            t = set_tour(step, visitor=payload.get("visitor"))
            log_stat("tour_step", v=f"{step}:{TOUR_STEPS[step]}", src="remote")
            self._send(200, json.dumps({"ok": True, "tour": t}, ensure_ascii=False).encode("utf-8"))
        elif u.path == "/api/stat":
            ok = log_stat(str(payload.get("k") or ""), m=str(payload.get("m") or ""),
                          v=str(payload.get("v") or ""), src=str(payload.get("src") or ""))
            self._send(200 if ok else 400, json.dumps({"ok": ok}).encode())
        elif u.path == "/api/voice/dictate":
            # P2b 语音打字开关（HUD 在受控武装态下切换；判据在 voice_gateway.set_dictate）
            res = vg.set_dictate(bool(payload.get("on")))
            self._send(200 if res.get("ok") else 409, json.dumps(res, ensure_ascii=False).encode())
        elif u.path == "/api/roles/beat":
            # 轻角色心跳（扬声器/魔杖；20s 一跳，45s 无跳即离列）
            sid = str(payload.get("sid") or "").strip().lower()
            role = str(payload.get("role") or "").strip().lower()
            if re.match(r"^[a-z0-9_-]{2,24}$", sid) and role in ("speaker", "wand"):
                ROLES[(sid, role)] = {"ts": time.time(), "batt": payload.get("batt")}
                self._send(200, b'{"ok": true}')
            else:
                self._send(400, b'{"ok": false}')
        elif u.path == "/api/wand":
            sid = str(payload.get("sid") or "").strip().lower()
            if re.match(r"^[a-z0-9_-]{2,24}$", sid):
                try:
                    WANDS[sid] = {"x": max(0.0, min(1.0, float(payload.get("x")))),
                                  "y": max(0.0, min(1.0, float(payload.get("y")))),
                                  "ts": time.time()}
                    self._send(200, b'{"ok": true}')
                except (TypeError, ValueError):
                    self._send(400, b'{"ok": false}')
            else:
                self._send(400, b'{"ok": false}')
        elif u.path == "/api/desktop/want":
            # HUD 拉屏时刷新「想看 X」（TTL 12s，pane 开着每几秒续；关了自动过期→agent 停截）
            mid = str(payload.get("machine") or "").strip().lower()
            if re.match(r"^[a-z0-9_-]{2,24}$", mid):
                if payload.get("on", True):
                    WANT[mid] = time.time() + WANT_TTL_S
                else:
                    WANT.pop(mid, None)
                self._send(200, b'{"ok": true}')
            else:
                self._send(400, b'{"ok": false}')
        elif u.path == "/api/gest/beat":
            # C1 指挥权心跳：{screen, q, hands, src}。screen 必须在台账（野屏当场拒）;
            # q=0/空手=快速让位。领导者变更才 bump seq+唤醒 SSE（心跳本身不惊动广播）。
            scr = str(payload.get("screen") or "").strip()[:32]
            ck0 = cockpit()
            if not ck0 or not any(s.get("id") == scr for s in ck0["screens"]):
                self._send(400, json.dumps(
                    {"ok": False, "error": f"屏「{scr}」不在台账 cockpit.json"},
                    ensure_ascii=False).encode("utf-8"))
                return
            try:
                q = max(0.0, min(1.0, float(payload.get("q") or 0)))
            except Exception:
                q = 0.0
            now2 = time.time()
            if q > 0:
                _gest_cand[scr] = {"q": q, "ts": now2}
            else:
                _gest_cand.pop(scr, None)
            for k in [k for k, c in _gest_cand.items()
                      if now2 - float(c.get("ts") or 0) > GEST_FRESH_S * 4]:
                _gest_cand.pop(k, None)
            lead = gest_arbitrate(_gest_cand, GESTLEAD["screen"], now2)
            if lead == GESTLEAD["screen"]:
                if lead:
                    GESTLEAD["beat_at"] = round(now2, 3)
            else:
                with _tour_cond:
                    GESTLEAD.update(screen=lead, seq=GESTLEAD["seq"] + 1,
                                    at=round(now2, 3), beat_at=round(now2, 3))
                    _tour_cond.notify_all()
                log_stat("gest_lead", v=lead or "released",
                         src=str(payload.get("src") or ""))
            self._send(200, json.dumps({"ok": True, "lead": GESTLEAD["screen"]}).encode())
        elif u.path == "/api/cockpit/throw":
            # C0 甩桌面跨屏：源屏+方向 → 座次邻接解析目标屏 → HANDOFF 单槽广播（SSE 即时唤醒,
            # 目标屏在 ctx.handoff 认领自己的 id 后 deskGrab）。方向语义=操作者视角左右。
            src_scr = str(payload.get("screen") or "").strip()[:32]
            direction = str(payload.get("dir") or "").strip()
            mid = str(payload.get("machine") or "").strip().lower()
            if direction not in ("left", "right", "tv", "center") \
                    or not re.match(r"^[a-z0-9_-]{2,24}$", mid):
                self._send(400, json.dumps(
                    {"ok": False, "error": "要 screen + dir(left|right|tv|center) + machine"},
                    ensure_ascii=False).encode("utf-8"))
                return
            tgt = cockpit_neighbor(src_scr, direction)
            if not tgt:
                why = {"left": "往左没有邻屏（U 型尽头）", "right": "往右没有邻屏（U 型尽头）",
                       "tv": "台账没有电视大屏（或您已在大屏上）",
                       "center": "台账没有中央屏（或您已在中央屏上）"}[direction]
                self._send(404, json.dumps(
                    {"ok": False, "error": f"「{src_scr}」{why}"},
                    ensure_ascii=False).encode("utf-8"))
                return
            with _tour_cond:
                HANDOFF.update(seq=HANDOFF["seq"] + 1, target=tgt["id"], machine=mid,
                               src_screen=src_scr, dir=direction, at=round(time.time(), 3),
                               src=str(payload.get("src") or "")[:16])   # drill 演练=页面静默不真开
                _tour_cond.notify_all()
            log_stat("cockpit_throw", m=mid, v=f"{src_scr}>{tgt['id']}",
                     src=str(payload.get("src") or ""))
            self._send(200, json.dumps(
                {"ok": True, "target": tgt["id"], "target_zh": tgt.get("zh", ""), "machine": mid},
                ensure_ascii=False).encode("utf-8"))
        elif u.path.startswith("/api/ops/"):
            # 隔空指挥（P2）：武装/点火/撤防/撤销——判据与执行全在 ops_gateway（四重护栏）
            act = u.path[len("/api/ops/"):]
            src = str(payload.get("src") or "")[:16]
            try:
                if act == "arm":
                    res = og.arm(str(payload.get("verb") or ""), payload.get("args") or {},
                                 by=str(payload.get("by") or "hud"), src=src)
                elif act == "fire":
                    res = og.fire(str(payload.get("id") or ""), src=src,
                                  frame_b64=str(payload.get("frame") or ""))
                elif act == "disarm":
                    res = og.disarm(str(payload.get("id") or ""), src=src)
                elif act == "undo":
                    res = og.undo(src=src)
                else:
                    res = {"ok": False, "error": "unknown op"}
                self._send(200 if res.get("ok") else 409,
                           json.dumps(res, ensure_ascii=False).encode("utf-8"))
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"ok": False, "error": str(e)[:160]}).encode())
        elif u.path.startswith("/api/ctrl/"):
            # 隔空受控（P2 输入注入）：武装(刷脸)/动作/撤防——判据全在 ops_gateway（四重护栏）
            act = u.path[len("/api/ctrl/"):]
            src = str(payload.get("src") or "")[:16]
            try:
                if act == "arm":
                    res = og.ctrl_arm(str(payload.get("machine") or ""), by="hud", src=src,
                                      frame_b64=str(payload.get("frame") or ""))
                elif act == "action":
                    res = og.ctrl_action(str(payload.get("token") or ""),
                                         str(payload.get("kind") or ""), payload, src=src)
                elif act == "disarm":
                    res = og.ctrl_disarm(str(payload.get("token") or ""), src=src)
                else:
                    res = {"ok": False, "error": "unknown ctrl"}
                self._send(200 if res.get("ok") else 409,
                           json.dumps(res, ensure_ascii=False).encode("utf-8"))
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"ok": False, "error": str(e)[:160]}).encode())
        else:
            self._send(404, b"{}")


def _push_notify() -> None:
    """语音相位变化即时唤醒 SSE（复用展演的 Condition 通道——同一条 12ms 快路）。"""
    with _tour_cond:
        _tour_cond.notify_all()


def main():
    vg.configure(get_ctx=build_ctx, set_tour=set_tour, ack_alert=_ack_alert,
                 notify=_push_notify, log_stat=log_stat,
                 ops_arm=og.arm, ops_fire=og.fire, ops_disarm=og.disarm, ops_undo=og.undo,
                 ctrl_dictate=og.ctrl_dictate, ctrl_key=og.ctrl_key_active,
                 ctrl_active=og.ctrl_active_machine)   # P2b 语音听写→受控会话
    og.configure(get_ctx=build_ctx, notify=_push_notify, log_stat=log_stat)
    threading.Thread(target=vg.ensure_models, daemon=True).start()   # 预热，失败=诚实降级
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), HandlerWithPost)
    print(f"hud server on :{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
