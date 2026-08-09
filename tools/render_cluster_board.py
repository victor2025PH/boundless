# -*- coding: utf-8 -*-
"""中枢活体壁纸 v3：静态身份壁纸右上叠加「集群实况」霓虹玻璃面板（哨兵每分钟调用）。

只跑在中枢机（zhongshu）。数据四来源，互为印证：
  1. 六机 watch_ports 直接 TCP 探活（局域网，不经 Hub）
  2. 各机哨兵自报状态 status.json（一条 ssh 收一台：state/IP漂移/GPU故障/VRAM 水位）
  3. Hub /health + /api/ops/hub_busy（忙态三档与重启纪律同源；推流=danger 档）
  4. alerts_state.json / alerts.jsonl（事件流小区，与告警系统同源）

v3（2026-08-06，官网风格对齐）：
  - 视觉语言取官网 tailwind tokens：ink 深空底(#05060F→#11132A) + neon 霓虹
    (cyan #22D3EE / violet #8B5CF6 / pink #EC4899) + 渐变描边 + 扫描线 + 辉光 LED
  - VRAM 条渐变填充 + 端点光斑；高水位/饱和徽章重制（v2.1 曾渲染成空色块：
    琥珀字画在琥珀底上 + 顶对齐溢出——改深色字 + anchor=lm 垂直居中）
  - 事件流小区：最近告警/恢复 3 条（alerts_state/alerts.jsonl 同源）
  - history.jsonl：每轮探活落一行（sparkline 数据源 + 未来可用性周报原料）
  - 行内 VRAM 迷你趋势线（近 30 分钟）
  - "<1ms" 显示修正

v2.1 保留：VRAM 高水位两档（≥95%×10min 亮牌不推送；≥98%×15min 推送，<93% 回滞清报）。
渲染失败退非零码，哨兵自动回落静态壁纸——实况板永远只是增强，不是单点。

v4（2026-08-07，P0 六机广播 · 《集群实况板_五视角优化美化方案_20260807.md》）：
  - --all：一次采集、六份渲染——每台机得到「本机行高亮 + 页脚本机身份」的视角板，
    底图取 brand-assets 该机原生分辨率身份壁纸；快照全 fleet 同帧（判据同源→六屏同帧）。
  - 雨相位改为全局单推进（advance_rain 一轮一次，draw_rain 纯绘制可重复调用）：
    相位是纯网格量，与面板像素尺寸无关——六块板下的是同一场雨。
  - VRAM 水位计数/告警/history 只在采集时跑一次（六渲染不重复计数，分钟语义不破坏）。
  - 输出 C:/Users/Public/boundless-hud/boards/<id>.png，由小界 :7912 /board/<id>.png 供
    节点哨兵拉取（服务端陈旧判决 404）；--hub-out 兼容中枢哨兵既有 board.png 目标。
  - 退出码只看中枢自己的板（中枢哨兵以此决定回落）；他机失败打印告警，节点侧由
    「拉不到 200 → 回落静态壁纸」独立兜底。

v5（2026-08-10，P0 集群算力模式 · 任务书「HUD 增强」）：
  - 模式绶带：标题下一条通栏——当前算力模式（chatx 智聊=青 / face 换脸直播=品红）+
    上次切换时间；切换中显示琥珀进度。判据=hub 执行器 logs/cluster_mode.json + modes.json。
  - 每机行「本模式角色 chip」（modes.json chips[mid]）；模式未初始化=零噪音不画。
  - 事件流分色重制：红=在燃故障 / 琥珀=待复核(恢复<6h、或在燃已 ack) / 灰=已过 6h；
    窗口 2h→24h，条数 3→4；模式切换事件（开始/每步/完成/回滚）并入同一条流。
  - 文字区 85% 不透明底板压住矩阵雨（雨保留在标题带/边缘，正文小字不再与亮雨头打架）。
  - VRAM 条叠加预算刻度线（cluster_map vram_budget：底座 services + mode_services[当前模式]）。
  - 模式内「预期停机」端口豁免探活（如 chatx 停 140:7857）——板/哨兵/执行器三方判据同源，
    不再把计划内的停机渲染成 svcdown 琥珀行。
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(r"D:\boundless")
FONTS = ROOT / "brand-assets" / "fonts"
MACHINES = ROOT / "deploy" / "machines.json"
HUB = "http://127.0.0.1:9000"
AVATARHUB_BASE = Path(r"C:\模仿音色")
BOARD_STATE = Path(r"C:\Users\Public\boundless-hud\board_state.json")
LOCAL_STATUS = Path(r"C:\Users\Public\boundless-hud\status.json")
HISTORY = Path(r"C:\Users\Public\boundless-hud\history.jsonl")
ALERTS_STATE = AVATARHUB_BASE / "logs" / "alerts_state.json"
# v5（P0 集群算力模式，2026-08-10）：模式绶带/角色 chip/预期停机豁免/预算刻度 的判据来源。
# 三处全部「缺席优雅缺省」——没启用模式管理的部署渲染结果与 v4 一致。
MODES_JSON = ROOT / "deploy" / "boundless-hud" / "modes.json"
MODE_STATE = AVATARHUB_BASE / "logs" / "cluster_mode.json"        # hub 执行器落账
MODE_EVENTS = AVATARHUB_BASE / "logs" / "cluster_mode_events.jsonl"
CLUSTER_MAP = AVATARHUB_BASE / "cluster_map.json"                 # 显存预算 SSOT（刻度线）

OK, DEG, OFF, WRN = "ok", "degraded", "offline", "selfwarn"
COLORS = {OK: (46, 194, 126), DEG: (222, 108, 16), OFF: (206, 44, 49), WRN: (156, 66, 214)}
HUB_IP = ""

# 官网设计令牌（website/tailwind.config.ts）
INK_950 = (5, 6, 15)
INK_900 = (10, 12, 27)
INK_800 = (17, 19, 42)
NEON_CYAN = (34, 211, 238)
NEON_BLUE = (59, 130, 246)
NEON_VIOLET = (139, 92, 246)
NEON_PINK = (236, 72, 153)

# VRAM 水位两档（单位=连续分钟；板子每分钟跑一轮）
VRAM_BADGE_FRAC, VRAM_BADGE_MIN = 0.95, 10
VRAM_SAT_FRAC, VRAM_SAT_MIN = 0.98, 15
VRAM_CLEAR_FRAC = 0.93

# 数字雨相位网格（v4：相位与像素解耦——列/行是纯网格量，任何分辨率画同一场雨）
RAIN_MAX_COLS = 40   # 覆盖任意 s 下的实际列数（面板 500s 宽 / 15s 列距 ≈ 33 列）
RAIN_ROWS = 39       # 整条雨落出面板的行阈值（≈ 508s 高 / 13s 行距）
RAIN_SPAN = 41       # 新生雨头的落点散布范围（行）

# v4 批量输出：六机视角板目录 + 各机身份壁纸母本
BOARDS_DIR_DEFAULT = Path(r"C:\Users\Public\boundless-hud\boards")
BRAND_WP = ROOT / "brand-assets" / "05_backgrounds" / "machines"

try:
    sys.path.insert(0, str(AVATARHUB_BASE))
    import alerts as _alerts
except Exception:
    _alerts = None


def C(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def lerp_c(c1, c2, t: float):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


_fc: dict = {}


def font(name: str, size: int):
    k = (name, size)
    if k not in _fc:
        _fc[k] = ImageFont.truetype(str(FONTS / name), size=size)
    return _fc[k]


F_BOLD = "NotoSansCJKsc-Bold.otf"
F_MED = "NotoSansCJKsc-Medium.otf"
F_BLACK = "NotoSansCJKsc-Black.otf"


# ---------------- 数据采集（与 v2 相同） ----------------

def tcp_ms(ip: str, port: int, timeout: float = 0.8) -> float | None:
    t0 = time.perf_counter()
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return (time.perf_counter() - t0) * 1000
    except OSError:
        return None


def ping_ok(ip: str) -> bool:
    try:
        r = subprocess.run(["ping", "-n", "1", "-w", "800", ip], capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


def fetch_self_status(m: dict) -> dict | None:
    try:
        if m["ip"] == HUB_IP:
            return json.loads(LOCAL_STATUS.read_text(encoding="utf-8", errors="replace"))
        alias = str(m["ssh"][0])
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", alias,
             r"cmd /c type C:\Users\Public\boundless-hud\status.json"],
            capture_output=True, text=True, timeout=9, encoding="utf-8", errors="replace",
        )
        if r.returncode == 0 and r.stdout.strip().startswith("{"):
            return json.loads(r.stdout.strip())
    except Exception:
        pass
    return None


#: 当前算力模式上下文（collect() 每轮刷新一次；probe/render 只读）
_MODE_CTX: dict = {}


def load_mode_ctx() -> dict:
    """v5：算力模式上下文。state=hub 执行器 logs/cluster_mode.json；profile=modes.json；
    预算刻度=cluster_map vram_budget（services + mode_services[当前模式]）。
    watch_ports 覆盖让「模式内预期停机」的端口不再被判 svcdown——板/哨兵/执行器三方判据同源。"""
    out = {"mode": "", "zh": "", "accent": NEON_VIOLET, "since_str": "", "switching": False,
           "progress": "", "chips": {}, "watch_ports": {}, "budget": {}}
    try:
        st = json.loads(MODE_STATE.read_text(encoding="utf-8"))
        out["mode"] = str(st.get("mode") or "")
        out["since_str"] = str(st.get("since_str") or "")
        out["switching"] = bool(st.get("switching"))
        p = st.get("progress") or {}
        if p:
            out["progress"] = f"{p.get('done', '?')}/{p.get('total', '?')}"
    except Exception:
        pass
    try:
        mj = json.loads(MODES_JSON.read_text(encoding="utf-8-sig"))
        mcfg = (mj.get("modes") or {}).get(out["mode"]) or {}
        if mcfg:
            out["zh"] = str(mcfg.get("zh") or out["mode"])
            if mcfg.get("accent"):
                out["accent"] = C(str(mcfg["accent"]))
            out["chips"] = dict(mcfg.get("chips") or {})
            for mid, mach in (mcfg.get("machines") or {}).items():
                wp = mach.get("watch_ports")
                if wp is not None:
                    out["watch_ports"][mid] = [int(x) for x in wp]
    except Exception:
        pass
    try:
        cm = json.loads(CLUSTER_MAP.read_text(encoding="utf-8-sig"))
        for ip, hc in (cm.get("vram_budget") or {}).items():
            if not isinstance(hc, dict) or "vram_gb" not in hc:
                continue
            modes = hc.get("mode_services") or {}
            if modes and out["mode"] not in modes:
                continue     # 分模式记账的机器、当前模式未知 → 不画误导性刻度
            used = sum(float(v) for v in (hc.get("services") or {}).values())
            used += sum(float(v) for v in (modes.get(out["mode"]) or {}).values())
            out["budget"][ip] = (used, float(hc["vram_gb"]))
    except Exception:
        pass
    return out


def probe_machine(m: dict) -> dict:
    ip = "127.0.0.1" if m["ip"] == HUB_IP else m["ip"]
    watch = m.get("watch_ports", [])
    override = (_MODE_CTX.get("watch_ports") or {}).get(m["id"])
    if override is not None:      # v5：模式内预期停机的端口不探=不误报（判据同源 modes.json）
        watch = override
    results = {p: tcp_ms(ip, p) for p in watch}
    dead = [p for p, v in results.items() if v is None]
    alive = {p: v for p, v in results.items() if v is not None}
    selfst = fetch_self_status(m)
    if not alive:
        status = OFF if not ping_ok(ip) else (DEG if dead else OK)
    elif dead:
        status = DEG
    else:
        status = OK
    self_state = str((selfst or {}).get("state") or "")
    if status == OK and self_state in ("gpufault", "ipdrift"):
        status = WRN
    lat = min(alive.values()) if alive else None
    vu = int((selfst or {}).get("vram_used_mb") or -1)
    vt = int((selfst or {}).get("vram_total_mb") or -1)
    gu = int((selfst or {}).get("gpu_util") if (selfst or {}).get("gpu_util") is not None else -1)
    return {"id": m["id"], "zh": m["zh"], "ip": m["ip"], "accent": m.get("accent", "#8888AA"),
            "brand": m.get("primary_brand", ""), "is_hub": m["ip"] == HUB_IP,
            "status": status, "dead": dead, "lat": lat, "self_state": self_state,
            "vram_used": vu, "vram_total": vt, "gpu_util": gu}


def http_json(url: str, timeout: float = 3.5) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def load_board_state() -> dict:
    try:
        return json.loads(BOARD_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_board_state(st: dict) -> None:
    try:
        tmp = BOARD_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        tmp.replace(BOARD_STATE)
    except Exception:
        pass


def track_vram(rows: list[dict], bstate: dict) -> None:
    hi = bstate.get("vram_high") or {}
    for r in rows:
        mid = r["id"]
        rec = hi.get(mid) or {"c95": 0, "c98": 0, "sat": False}
        vt, vu = r["vram_total"], r["vram_used"]
        frac = (vu / vt) if (vt > 0 and vu >= 0) else 0.0
        rec["c95"] = rec["c95"] + 1 if frac >= VRAM_BADGE_FRAC else 0
        rec["c98"] = rec["c98"] + 1 if frac >= VRAM_SAT_FRAC else 0
        r["vram_flag"] = ""
        if rec["c98"] >= VRAM_SAT_MIN:
            r["vram_flag"] = "sat"
        elif rec["c95"] >= VRAM_BADGE_MIN:
            r["vram_flag"] = "high"
        if _alerts is not None:
            try:
                key = f"cluster_{mid}_vram_sat"
                if rec["c98"] >= VRAM_SAT_MIN and not rec["sat"]:
                    _alerts.raise_alert(key, f"显存饱和：{r['zh']}（{mid}）",
                                        detail=f"整卡 {vu / 1024:.1f}/{vt / 1024:.1f}G ≥98% 已持续 {rec['c98']} 分钟，"
                                               "OOM 边缘（参照 WDDM 溢出/3060 共驻 OOM 先例），查谁在超编。",
                                        level="warn", source="cluster_board")
                    rec["sat"] = True
                elif rec["sat"] and frac < VRAM_CLEAR_FRAC:
                    _alerts.clear_alert(key, note=f"显存回落 {frac * 100:.0f}%")
                    rec["sat"] = False
            except Exception:
                pass
        hi[mid] = rec
    bstate["vram_high"] = hi


def push_alerts(rows: list[dict], prev_states: dict) -> dict:
    now_states = {}
    for r in rows:
        mid, zh = r["id"], r["zh"]
        st = r["status"]
        now_states[mid] = st
        if _alerts is None:
            continue
        try:
            key_off = f"cluster_{mid}_offline"
            key_deg = f"cluster_{mid}_degraded"
            if st == OFF:
                _alerts.raise_alert(key_off, f"集群节点失联：{zh}（{mid}）",
                                    detail="TCP 探活与 ping 均无响应；可能断网/关机/IP 漂移。",
                                    level="error", source="cluster_board")
            elif prev_states.get(mid) == OFF:
                _alerts.clear_alert(key_off, note="节点恢复在线")
            if st == DEG:
                _alerts.raise_alert(key_deg, f"节点服务掉线：{zh}（{mid}）",
                                    detail="端口未监听: " + ",".join(str(p) for p in r["dead"]),
                                    level="warn", source="cluster_board")
            elif prev_states.get(mid) == DEG:
                _alerts.clear_alert(key_deg, note="服务端口全部恢复")
            if r["self_state"] in ("gpufault", "ipdrift"):
                _alerts.raise_alert(f"cluster_{mid}_{r['self_state']}",
                                    f"节点自检告警：{zh} {r['self_state']}",
                                    detail="哨兵自报（详见该机桌面壁纸左下角标注）",
                                    level="error" if r["self_state"] == "gpufault" else "warn",
                                    source="cluster_board")
            elif prev_states.get(mid) == WRN and r["status"] != WRN:
                for k in ("gpufault", "ipdrift"):
                    _alerts.clear_alert(f"cluster_{mid}_{k}", note="自检恢复")
        except Exception:
            pass
    return now_states


def fmt_dur(sec: float) -> str:
    sec = int(sec)
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


# ---------------- v3：历史与事件流 ----------------

def append_history(rows: list[dict], verdict: str) -> None:
    """每轮探活落一行：sparkline 数据源 + 可用性周报原料。超 2MB 保尾部 1440 行（一天）。"""
    try:
        rec = {"ts": round(time.time()),
               "v": verdict,
               "m": {r["id"]: {"s": r["status"][:1], "l": round(r["lat"], 1) if r["lat"] is not None else None,
                                "vu": r["vram_used"], "vt": r["vram_total"],
                                "u": r.get("gpu_util", -1)} for r in rows}}
        with HISTORY.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if HISTORY.stat().st_size > 2 * 1024 * 1024:
            lines = HISTORY.read_text(encoding="utf-8", errors="replace").splitlines()[-1440:]
            HISTORY.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def load_history(n: int = 30) -> list[dict]:
    try:
        lines = HISTORY.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        return out
    except Exception:
        return []


#: v5 事件分色三档（任务书口径）：红=当前故障(在燃)；琥珀=待复核(已恢复<6h/已 ack)；灰=已过 6h
EV_RED = (206, 44, 49)
EV_AMBER = (232, 179, 65)
EV_GRAY = (128, 134, 150)


def _mode_events(now: float, window_s: float) -> list[tuple[float, str, tuple, bool]]:
    """模式切换事件流（cluster_mode_events.jsonl 尾部）：切换开始/回滚=琥珀，失败=红，
    完成=绿，逐步进度只在「切换中」取最后一条（避免刷屏挤掉告警）。缺席=空。"""
    out: list[tuple[float, str, tuple, bool]] = []
    try:
        lines = MODE_EVENTS.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        steps: list[tuple[float, str, tuple, bool]] = []
        for ln in lines:
            try:
                e = json.loads(ln)
            except Exception:
                continue
            ts = float(e.get("ts") or 0)
            if now - ts > window_s:
                continue
            ev, ok, note = str(e.get("ev")), bool(e.get("ok")), str(e.get("note") or "")
            if ev == "step":
                steps.append((ts, "模式·" + note, (EV_RED if not ok else EV_GRAY), False))
            elif ev == "start":
                out.append((ts, note, EV_AMBER, False))
            elif ev == "rollback":
                out.append((ts, note, EV_RED, False))
            elif ev == "done":
                out.append((ts, note, ((46, 194, 126) if ok else EV_RED), False))
        if _MODE_CTX.get("switching") and steps:
            out.append(steps[-1])       # 切换中：外显最后一步实时进度
    except Exception:
        pass
    return out


def recent_events(max_n: int = 4, window_s: float = 24 * 3600) -> list[tuple[str, str, tuple]]:
    """事件流（v5 分色重制）：红=在燃故障 / 琥珀=待复核(恢复<6h、或在燃但已 ack) /
    灰=已过 6h 的旧事（窗口放宽到 24h，旧事压灰不清场——告警疲劳与失忆之间的折中）。
    模式切换事件（开始/每步/完成/回滚）并入同一条流，验收「有始有终」在板上直接可见。"""
    now = time.time()
    firing: list[tuple[float, str, tuple, bool]] = []
    rest: list[tuple[float, str, tuple, bool]] = []
    try:
        st = json.loads(ALERTS_STATE.read_text(encoding="utf-8", errors="replace"))
        for k, v in st.items():
            if not isinstance(v, dict):
                continue
            title = str(v.get("title") or k)
            if v.get("status") == "firing":
                ts = float(v.get("since") or now)
                acked = bool(v.get("ack"))
                # 在燃=红（当前故障）；已 ack 的在燃=琥珀（看过了、待复核收尾）
                firing.append((ts, title, (EV_AMBER if acked else EV_RED), acked))
            elif v.get("status") == "resolved":
                ts = float(v.get("resolved_at") or 0)
                if now - ts <= window_s:
                    col = EV_AMBER if (now - ts <= 6 * 3600) else EV_GRAY
                    rest.append((ts, title, col, False))
    except Exception:
        pass
    rest.extend(_mode_events(now, window_s))
    firing.sort(key=lambda x: -x[0])
    rest.sort(key=lambda x: -x[0])
    picked = (firing + rest)[:max_n]
    out = []
    for ts, title, col, acked in picked:
        hhmm = datetime.fromtimestamp(ts).strftime("%H:%M")
        out.append((hhmm, title, col, acked))
    return out


# ---------------- v3：霓虹绘制原语 ----------------

def v_gradient(size: tuple[int, int], c_top, c_bot, a_top: int, a_bot: int) -> Image.Image:
    w, h = size
    col = Image.new("RGBA", (1, h))
    px = col.load()
    for y in range(h):
        t = y / max(1, h - 1)
        r, g, b = lerp_c(c_top, c_bot, t)
        a = int(a_top + (a_bot - a_top) * t)
        px[0, y] = (r, g, b, a)
    return col.resize((w, h))


def h_gradient(size: tuple[int, int], c1, c2) -> Image.Image:
    w, h = size
    row = Image.new("RGB", (w, 1))
    px = row.load()
    for x in range(w):
        px[x, 0] = lerp_c(c1, c2, x / max(1, w - 1))
    return row.resize((w, h))


def neon_panel(base: Image.Image, box: tuple[int, int, int, int], radius: int, s: float) -> None:
    """官网语言的玻璃面板：ink 渐变底 + 暗扫描线 + cyan→violet 渐变描边 + 外辉光。
    注意全部走 alpha_composite——paste 会把填充层自带 alpha 写穿底图（v3 首渲白条纹事故）。"""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    from PIL import ImageChops
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)

    fill = v_gradient((w, h), INK_950, INK_800, 232, 208)
    r_, g_, b_, a_ = fill.split()
    fill.putalpha(ImageChops.multiply(a_, mask))
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    layer.paste(fill, (x0, y0))
    base.alpha_composite(layer)

    scan = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    sd = ImageDraw.Draw(scan)
    step = max(3, int(4 * s))
    for yy in range(0, h, step):
        sd.line((0, yy, w, yy), fill=(0, 0, 0, 22), width=1)
    sr, sg, sb, sa = scan.split()
    scan.putalpha(ImageChops.multiply(sa, mask))
    slayer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    slayer.paste(scan, (x0, y0))
    base.alpha_composite(slayer)

    ow = max(2, int(2 * s))
    omask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(omask).rounded_rectangle((ow // 2, ow // 2, w - 1 - ow // 2, h - 1 - ow // 2),
                                            radius=radius, outline=255, width=ow)
    grad = h_gradient((w, h), NEON_CYAN, NEON_VIOLET).convert("RGBA")
    glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    gm = Image.new("L", base.size, 0)
    gm.paste(omask, (x0, y0))
    gm = gm.filter(ImageFilter.GaussianBlur(int(6 * s)))
    tint = Image.new("RGBA", base.size, (*NEON_VIOLET, 88))
    glow = Image.composite(tint, glow, gm)
    base.alpha_composite(glow)
    stroke = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    stroke.paste(grad, (0, 0), omask)
    base.alpha_composite(stroke, (x0, y0))


def gradient_text(base: Image.Image, xy: tuple[int, int], text: str, fnt, c1, c2) -> int:
    """渐变填色文字（shimmer 的静态版）。返回文字宽度。"""
    d0 = ImageDraw.Draw(base)
    tw = int(d0.textlength(text, font=fnt))
    bbox = d0.textbbox((0, 0), text, font=fnt)
    th = bbox[3] + 4
    mask = Image.new("L", (tw + 4, th), 0)
    ImageDraw.Draw(mask).text((0, 0), text, font=fnt, fill=255)
    grad = h_gradient((tw + 4, th), c1, c2).convert("RGBA")
    base.paste(grad, xy, mask)
    return tw


def glow_leds(base: Image.Image, dots: list[tuple[int, int, int, tuple]], s: float) -> None:
    """一批辉光 LED：halo 单次高斯模糊 + 实心核 + 高光点。"""
    if not dots:
        return
    halo = Image.new("RGBA", base.size, (0, 0, 0, 0))
    hd = ImageDraw.Draw(halo)
    for x, y, r, c in dots:
        hd.ellipse((x - r * 2.4, y - r * 2.4, x + r * 2.4, y + r * 2.4), fill=(*c, 110))
    halo = halo.filter(ImageFilter.GaussianBlur(int(5 * s)))
    base.alpha_composite(halo)
    d = ImageDraw.Draw(base)
    for x, y, r, c in dots:
        d.ellipse((x - r, y - r, x + r, y + r), fill=(*c, 255))
        hr = max(1, int(r * 0.35))
        d.ellipse((x - hr - r * 0.25, y - hr - r * 0.25, x + hr - r * 0.25, y + hr - r * 0.25),
                  fill=(255, 255, 255, 150))


def advance_rain(bstate: dict) -> None:
    """雨相位全局单推进（v4）：一轮采集只推一次，六块板共享同一场雨。
    经典 Matrix 机理不变：字符场固定在网格上（按 (列,行) 哈希取字，永远稳定），
    动的是每列的「亮窗」头部；列相位存 board_state → 相邻两分钟的帧是连续动画。"""
    import random
    cols = bstate.get("rain_cols") or {}
    rng = random.Random(time.time_ns())
    new_cols: dict = {}
    for ci in range(RAIN_MAX_COLS):
        key = str(ci)
        rec = cols.get(key)
        if rec is None:
            if rng.random() < 0.22:      # 留空列，疏密有致
                new_cols[key] = {"off": True}
                continue
            rec = {"head": rng.randint(-RAIN_SPAN, RAIN_SPAN), "tail": rng.randint(6, 15),
                   "speed": rng.randint(3, 8)}
        elif rec.get("off"):
            new_cols[key] = rec
            continue
        else:
            rec = dict(rec)
            rec["head"] = rec["head"] + rec.get("speed", 5)   # 每分钟一帧：头部下移
            if rec["head"] - rec["tail"] > RAIN_ROWS:          # 整条雨落出面板 → 顶部重生
                rec = {"head": rng.randint(-RAIN_SPAN // 2, 0), "tail": rng.randint(6, 15),
                       "speed": rng.randint(3, 8)}
        new_cols[key] = rec
    bstate["rain_cols"] = new_cols


def draw_rain(base: Image.Image, box: tuple[int, int, int, int], radius: int, s: float,
              cols: dict) -> None:
    """数字雨绘制（纯读相位、不推进）——advance_rain 之后可对任意多块板重复调用。
    配色走品牌霓虹（青主体、雨头亮白），既是 Matrix 又不出官网色系。"""
    import hashlib
    from PIL import ImageChops
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    glyphs = "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾊﾋﾌﾍﾎﾏﾐ0123456789$#*+<>"
    fnt = font(F_MED, int(11 * s))
    col_w = int(15 * s)
    gh = int(13 * s)

    def cell_glyph(ci: int, ri: int) -> str:
        hv = int(hashlib.md5(f"{ci}:{ri}".encode()).hexdigest()[:8], 16)
        return glyphs[hv % len(glyphs)]

    for ci, cx in enumerate(range(int(8 * s), w - int(6 * s), col_w)):
        rec = cols.get(str(ci))
        if not rec or rec.get("off"):
            continue
        head_y = rec["head"] * gh
        for i in range(rec["tail"]):
            gy = head_y - i * gh
            if gy < -gh or gy > h:
                continue
            ch = cell_glyph(ci, gy // gh)
            if i == 0:
                col, a = (225, 255, 240), 120
            else:
                t = i / rec["tail"]
                col = lerp_c(NEON_CYAN, (16, 120, 80), t)
                a = int(70 * (1 - t)) + 8
            d.text((cx, gy), ch, font=fnt, fill=(*col, a))
    r_, g_, b_, a_ = layer.split()
    layer.putalpha(ImageChops.multiply(a_, mask))
    full = Image.new("RGBA", base.size, (0, 0, 0, 0))
    full.paste(layer, (x0, y0))
    base.alpha_composite(full)


_icon_cache: dict[str, Image.Image | None] = {}


def brand_icon(r: dict, size: int) -> Image.Image | None:
    """机名前的项目 LOGO：中枢=无界主标，其余=所属产品图标（machines.json primary_brand）。"""
    key = f"{'hub' if r.get('is_hub') else r.get('brand', '')}-{size}"
    if key in _icon_cache:
        return _icon_cache[key]
    try:
        if r.get("is_hub"):
            p = ROOT / "brand-assets" / "01_logos" / "mark" / "boundless-mark-128.png"
        else:
            b = r.get("brand") or "voicex"
            p = ROOT / "brand-assets" / "02_product-icons" / b / f"{b}-128.png"
        im = Image.open(p).convert("RGBA")
        im.thumbnail((size, size), Image.Resampling.LANCZOS)
        _icon_cache[key] = im
    except Exception:
        _icon_cache[key] = None
    return _icon_cache[key]


def util_color(util: int):
    """GPU 算力热度色：冷青(空闲) → 紫(半载) → 霓虹粉(满载)，官网 neon 渐变同源。"""
    t = max(0.0, min(1.0, util / 100.0))
    if t < 0.5:
        return lerp_c(NEON_CYAN, NEON_VIOLET, t * 2)
    return lerp_c(NEON_VIOLET, NEON_PINK, (t - 0.5) * 2)


def sparkline(base: Image.Image, box: tuple[int, int, int, int], values: list[float], color) -> None:
    """迷你趋势线（0..1 归一值），带末端光点。"""
    if len(values) < 4:
        return
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    d = ImageDraw.Draw(base)
    pts = []
    for i, v in enumerate(values):
        v = max(0.0, min(1.0, v))
        px = x0 + int(i * w / max(1, len(values) - 1))
        py = y1 - int(v * h)
        pts.append((px, py))
    d.line(pts, fill=(*color, 165), width=1)
    lx, ly = pts[-1]
    d.ellipse((lx - 2, ly - 2, lx + 2, ly + 2), fill=(*color, 230))


# ---------------- 采集与主渲染 ----------------

def collect() -> dict:
    """一轮数据采集（v4 全局只跑一次）：探活/自报/忙态/VRAM 水位/告警/history/雨相位。
    六块板共享同一份快照——判据同源延伸到「六屏同帧」；水位分钟计数不因多板而加速。"""
    data = json.loads(MACHINES.read_text(encoding="utf-8"))
    fleet = data["machines"]
    global HUB_IP, _MODE_CTX
    HUB_IP = str(data.get("hub", {}).get("ip", ""))
    _MODE_CTX = load_mode_ctx()      # v5：先取模式上下文——probe 的端口豁免要用
    with ThreadPoolExecutor(max_workers=6) as ex:
        rows = list(ex.map(probe_machine, fleet))

    busy = http_json(f"{HUB}/api/ops/hub_busy") or {}
    summary = busy.get("summary") or {}
    verdict = str(summary.get("restart") or "?")
    reason = str(summary.get("reason") or "")
    streaming = verdict == "danger"

    bstate = load_board_state()
    if streaming and not bstate.get("streaming_since"):
        bstate["streaming_since"] = time.time()
    if not streaming:
        bstate["streaming_since"] = None
    track_vram(rows, bstate)
    bstate["machines"] = push_alerts(rows, bstate.get("machines") or {})
    advance_rain(bstate)
    save_board_state(bstate)
    append_history(rows, verdict)
    return {"fleet": fleet, "rows": rows, "verdict": verdict, "reason": reason,
            "streaming": streaming, "bstate": bstate, "mode": _MODE_CTX,
            "hist": load_history(30), "events": recent_events(4)}


def render_board(ctx: dict, base_path: Path, out_path: Path, self_id: str = "") -> None:
    """在一张身份壁纸上渲一块实况板。self_id 非空 = 该机视角：本机行高亮 + 页脚本机身份。"""
    rows = ctx["rows"]
    verdict, reason, streaming = ctx["verdict"], ctx["reason"], ctx["streaming"]
    bstate, hist, events = ctx["bstate"], ctx["hist"], ctx["events"]

    mode = ctx.get("mode") or {}

    base = Image.open(base_path).convert("RGBA")
    W, H = base.size
    s = H / 1080.0

    pw, ph = int(500 * s), int(520 * s)
    x0, y0 = W - pw - int(36 * s), int(64 * s)
    neon_panel(base, (x0, y0, x0 + pw, y0 + ph), int(16 * s), s)
    draw_rain(base, (x0, y0, x0 + pw, y0 + ph), int(16 * s), s, bstate.get("rain_cols") or {})
    # v5：文字区 85% 不透明底板压住矩阵雨——雨只在标题带/边缘全亮，正文区降到 ~15% 隐现，
    # 13px 级小字（事件流/明细）不再与亮雨头打架；走 alpha_composite（v3 白条纹事故同教训）。
    plate = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(plate).rounded_rectangle(
        (x0 + int(10 * s), y0 + int(50 * s), x0 + pw - int(10 * s), y0 + ph - int(10 * s)),
        radius=int(12 * s), fill=(*INK_900, 217))
    base.alpha_composite(plate)
    d = ImageDraw.Draw(base)
    leds: list[tuple[int, int, int, tuple]] = []

    # ---- 标题区 ----
    f_t = font(F_BLACK, int(23 * s))
    tw = gradient_text(base, (x0 + int(22 * s), y0 + int(13 * s)), "集群实况", f_t, NEON_CYAN, NEON_VIOLET)
    d = ImageDraw.Draw(base)
    f_live = font(F_BOLD, int(13 * s))
    lx = x0 + int(22 * s) + tw + int(10 * s)
    d.rounded_rectangle((lx, y0 + int(17 * s), lx + int(44 * s), y0 + int(35 * s)), radius=int(4 * s),
                        outline=(*NEON_PINK, 210), width=max(1, int(s)))
    d.text((lx + int(22 * s), y0 + int(26 * s)), "LIVE", font=f_live, fill=(*NEON_PINK, 255), anchor="mm")
    d.text((lx + int(56 * s), y0 + int(26 * s)), datetime.now().strftime("%H:%M"),
           font=font(F_MED, int(19 * s)), fill=(150, 156, 176, 220), anchor="lm")

    if streaming:
        dur = fmt_dur(time.time() - float(bstate.get("streaming_since") or time.time()))
        tag, dot_c = f"推流中 {dur}", (222, 60, 70)
    else:
        tag, dot_c = "在线", (46, 194, 126)
    f_tag = font(F_BOLD, int(16 * s))
    tgw = d.textlength(tag, font=f_tag)
    leds.append((int(x0 + pw - tgw - int(46 * s)), int(y0 + int(27 * s)), int(5 * s), dot_c))
    d.text((x0 + pw - tgw - int(32 * s), y0 + int(27 * s)), tag, font=f_tag, fill=(*dot_c, 255), anchor="lm")
    grad_line = h_gradient((pw - int(36 * s), max(1, int(s))), NEON_CYAN, NEON_VIOLET).convert("RGBA")
    grad_line.putalpha(90)
    base.alpha_composite(grad_line, (x0 + int(18 * s), y0 + int(48 * s)))
    d = ImageDraw.Draw(base)

    # ---- v5 模式绶带：当前算力模式 + 上次切换时间（chatx 青 / face 品红 / 未初始化 中性紫） ----
    mac = tuple(mode.get("accent") or NEON_VIOLET)
    rb_y, rb_h = y0 + int(54 * s), int(24 * s)
    d.rounded_rectangle((x0 + int(18 * s), rb_y, x0 + pw - int(18 * s), rb_y + rb_h),
                        radius=int(6 * s), fill=(*mac, 26), outline=(*mac, 110),
                        width=max(1, int(s)))
    f_rb = font(F_BOLD, int(14 * s))
    if mode.get("switching"):
        rb_txt = f"算力模式 · 切换中 {mode.get('progress') or ''}…"
        rb_c = (232, 179, 65)
    elif mode.get("mode"):
        rb_txt = f"算力模式 · {mode.get('zh') or mode['mode']}（{mode['mode']}）"
        rb_c = mac
    else:
        rb_txt = "算力模式 · 未初始化（等首次切换落账）"
        rb_c = (150, 156, 176)
    leds.append((int(x0 + 32 * s), int(rb_y + rb_h / 2), int(4 * s), rb_c))
    d.text((x0 + int(44 * s), rb_y + rb_h / 2), rb_txt, font=f_rb, fill=(*rb_c, 250), anchor="lm")
    since = str(mode.get("since_str") or "")
    if since and not mode.get("switching"):
        d.text((x0 + pw - int(26 * s), rb_y + rb_h / 2), f"切换于 {since[5:16]}",
               font=font(F_MED, int(12.5 * s)), fill=(150, 156, 176, 210), anchor="rm")

    # ---- 六机行 ----
    f_zh = font(F_BOLD, int(20 * s))
    f_dt = font(F_MED, int(16 * s))
    f_vr = font(F_MED, int(13 * s))
    f_chip = font(F_MED, int(11 * s))
    ry = y0 + int(88 * s)
    row_h = int(43 * s)
    hist_by_id: dict[str, list[float]] = {}
    for hrec in hist:
        for mid, mv in (hrec.get("m") or {}).items():
            vu, vt = mv.get("vu") or -1, mv.get("vt") or -1
            if vt and vt > 0 and vu >= 0:
                hist_by_id.setdefault(mid, []).append(vu / vt)
    for r in rows:
        is_self = bool(self_id) and r["id"] == self_id
        if is_self:
            # 本机行高亮：机器识别色淡底 + 描边（P0 视角化的核心可见差异）
            ac = C(r["accent"])
            band = Image.new("RGBA", base.size, (0, 0, 0, 0))
            ImageDraw.Draw(band).rounded_rectangle(
                (x0 + int(14 * s), ry - int(2 * s), x0 + pw - int(14 * s), ry + int(36 * s)),
                radius=int(9 * s), fill=(*ac, 24), outline=(*ac, 105), width=max(1, int(s)))
            base.alpha_composite(band)
            d = ImageDraw.Draw(base)
        # LED：健康态时=GPU 算力热度色（冷青→紫→热粉）；异常态=告警色压过一切
        util = r.get("gpu_util", -1)
        if r["status"] == OK and util >= 0:
            c = util_color(util)
        else:
            c = COLORS[r["status"]]
        leds.append((int(x0 + int(29 * s)), int(ry + int(13 * s)), int(6 * s), c))
        icon = brand_icon(r, int(19 * s))
        if icon:
            base.paste(icon, (int(x0 + 44 * s), int(ry + 13 * s - icon.height / 2)), icon)
            d = ImageDraw.Draw(base)
        d.text((x0 + int(70 * s), ry + int(13 * s)), r["zh"], font=f_zh, fill=(238, 240, 248, 252), anchor="lm")
        if is_self:
            nw = d.textlength(r["zh"], font=f_zh)
            d.text((x0 + int(70 * s) + nw + int(7 * s), ry + int(13 * s)), "★",
                   font=font(F_BOLD, int(14 * s)), fill=(*C(r["accent"]), 240), anchor="lm")
        if r["status"] == OK:
            lat = r["lat"]
            lat_txt = "<1ms" if (lat is not None and lat < 1) else (f"{lat:.0f}ms" if lat is not None else "")
            bits = ["在线", lat_txt]
            if util >= 0:
                bits.append(f"GPU {util}%")
            detail = " · ".join(b for b in bits if b)
        elif r["status"] == WRN:
            detail = {"gpufault": "GPU 异常(自检)", "ipdrift": "IP 漂移(自检)"}.get(r["self_state"], "自检告警")
        elif r["status"] == DEG:
            detail = "端口掉线 " + ",".join(str(p) for p in r["dead"][:3])
        else:
            detail = "失联 · 断网/关机/漂移"
        d.text((x0 + int(166 * s), ry + int(13 * s)), detail, font=f_dt,
               fill=(*c, 255) if r["status"] != OK else (166, 172, 190, 230), anchor="lm")

        vals = hist_by_id.get(r["id"], [])
        if len(vals) >= 4:
            # 趋势线放机名下方空档（detail 行加了 GPU% 后原右侧位会撞车）
            sparkline(base, (int(x0 + 70 * s), int(ry + 22 * s), int(x0 + 128 * s), int(ry + 34 * s)),
                      vals[-30:], NEON_CYAN)
            d = ImageDraw.Draw(base)

        # v5 本模式角色 chip（modes.json chips[mid]，模式未初始化=无 chip 零噪音）
        chip = str((mode.get("chips") or {}).get(r["id"]) or "")
        if chip:
            cw = d.textlength(chip, font=f_chip)
            cx0, cy0 = x0 + int(166 * s), ry + int(20 * s)
            ch_h = int(15 * s)
            d.rounded_rectangle((cx0, cy0, cx0 + cw + int(12 * s), cy0 + ch_h),
                                radius=ch_h // 2, fill=(*mac, 22), outline=(*mac, 95),
                                width=max(1, int(s)))
            d.text((cx0 + int(6 * s), cy0 + ch_h / 2), chip, font=f_chip,
                   fill=(206, 212, 228, 235), anchor="lm")

        if r["vram_total"] > 0:
            bw, bh = int(118 * s), int(7 * s)
            bx, by = x0 + pw - bw - int(22 * s), ry + int(23 * s)
            frac = max(0.0, min(1.0, r["vram_used"] / r["vram_total"]))
            track = Image.new("RGBA", (bw, bh), (255, 255, 255, 22))
            bar_mask = Image.new("L", (bw, bh), 0)
            ImageDraw.Draw(bar_mask).rounded_rectangle((0, 0, bw - 1, bh - 1), radius=bh // 2, fill=255)
            base.paste(track, (bx, by), bar_mask)
            fw = int(bw * frac)
            if fw > 2:
                g1 = (46, 194, 126) if frac < 0.75 else lerp_c((46, 194, 126), (232, 179, 65), min(1.0, (frac - 0.75) / 0.2))
                g2 = (232, 179, 65) if frac < 0.92 else (222, 60, 70)
                grad_bar = h_gradient((fw, bh), g1, g2).convert("RGBA")
                fill_mask = Image.new("L", (bw, bh), 0)
                ImageDraw.Draw(fill_mask).rounded_rectangle((0, 0, bw - 1, bh - 1), radius=bh // 2, fill=255)
                fill_mask = fill_mask.crop((0, 0, fw, bh))
                base.paste(grad_bar, (bx, by), fill_mask)
                d = ImageDraw.Draw(base)
                leds.append((bx + fw, by + bh // 2, max(2, int(2.4 * s)), g2))
            # v5 预算刻度线（cluster_map vram_budget：底座+当前模式编制）——实际水位 vs 纸面
            # 编制一眼对齐；超过刻度=有账外租户（分模式记账的机器在模式未知时不画，防误导）
            bdg = (mode.get("budget") or {}).get(str(r.get("ip") or ""))
            if bdg and bdg[1] > 0:
                tfrac = max(0.0, min(1.0, float(bdg[0]) / float(bdg[1])))
                tx = bx + int(bw * tfrac)
                d.line((tx, by - int(3 * s), tx, by + bh + int(3 * s)),
                       fill=(240, 244, 250, 200), width=max(1, int(s)))
            vtxt = f"{r['vram_used'] / 1024:.1f}/{r['vram_total'] / 1024:.0f}G"
            vw = d.textlength(vtxt, font=f_vr)
            d.text((bx - vw - int(8 * s), ry + int(26 * s)), vtxt, font=f_vr, fill=(150, 156, 176, 215), anchor="lm")
            flag = r.get("vram_flag")
            if flag:
                lab = "饱和" if flag == "sat" else "高水位"
                fc = (222, 60, 70) if flag == "sat" else (232, 179, 65)
                f_bd = font(F_BOLD, int(12 * s))
                lw = d.textlength(lab, font=f_bd)
                pill_w, pill_h = int(lw + 12 * s), int(17 * s)
                px0, py0 = int(bx - vw - int(20 * s) - pill_w), int(ry + int(26 * s) - pill_h / 2)
                d.rounded_rectangle((px0, py0, px0 + pill_w, py0 + pill_h), radius=pill_h // 2,
                                    fill=(*fc, 235))
                d.text((px0 + pill_w / 2, py0 + pill_h / 2 - 1), lab, font=f_bd,
                       fill=(16, 10, 6, 255), anchor="mm")
        ry += row_h

    # ---- 忙态判定 ----
    grad_line2 = h_gradient((pw - int(36 * s), max(1, int(s))), NEON_VIOLET, NEON_CYAN).convert("RGBA")
    grad_line2.putalpha(70)
    base.alpha_composite(grad_line2, (x0 + int(18 * s), int(ry + 2 * s)))
    d = ImageDraw.Draw(base)
    vy = ry + int(12 * s)
    v_map = {"safe": ("此刻可安全重启 Hub", (46, 194, 126)),
             "wait": ("Hub 有长任务在跑 · 重启请等", (232, 179, 65)),
             "danger": ("直播/推流中 · 禁止重启", (222, 60, 70))}
    v_txt, v_c = v_map.get(verdict, (f"忙态未知({verdict})", (170, 176, 192)))
    leds.append((int(x0 + 31 * s), int(vy + 12 * s), int(6 * s), v_c))
    d.text((x0 + int(52 * s), vy + int(12 * s)), v_txt, font=f_zh, fill=(*v_c, 255), anchor="lm")
    if reason:
        f_r = font(F_MED, int(14 * s))
        rs = reason.strip().replace("\n", " ")
        clipped = False
        while rs and d.textlength(rs + "…", font=f_r) > pw - int(76 * s):
            rs = rs[:-1]
            clipped = True
        d.text((x0 + int(52 * s), vy + int(33 * s)), rs + ("…" if clipped else ""),
               font=f_r, fill=(160, 166, 184, 210), anchor="lm")

    # ---- 事件流 ----
    ey = vy + int(50 * s)
    grad_line3 = h_gradient((pw - int(36 * s), max(1, int(s))), NEON_CYAN, NEON_PINK).convert("RGBA")
    grad_line3.putalpha(55)
    base.alpha_composite(grad_line3, (x0 + int(18 * s), ey))
    d = ImageDraw.Draw(base)
    ey += int(9 * s)
    f_ev = font(F_MED, int(13.5 * s))
    if events:
        for hhmm, title, col, acked in events:
            title_s = ("✓ " if acked else "") + title.strip().replace("\n", " ")
            avail = pw - int(112 * s)
            clipped = False
            while title_s and d.textlength(title_s + "…", font=f_ev) > avail:
                title_s = title_s[:-1]
                clipped = True
            # 已确认：灯降为中性灰、文字压暗——「看过了」的告警不再喊
            leds.append((int(x0 + 28 * s), int(ey + 9 * s), int(3.2 * s),
                         (108, 116, 130) if acked else col))
            d.text((x0 + int(42 * s), ey + int(9 * s)), hhmm, font=f_ev,
                   fill=(130, 136, 156, 160 if acked else 200), anchor="lm")
            d.text((x0 + int(86 * s), ey + int(9 * s)), title_s + ("…" if clipped else ""),
                   font=f_ev, fill=(150, 155, 170, 165) if acked else (190, 195, 212, 235), anchor="lm")
            ey += int(19 * s)
    else:
        d.text((x0 + int(28 * s), ey + int(9 * s)), "近 2 小时无告警事件",
               font=f_ev, fill=(120, 126, 146, 170), anchor="lm")

    # ---- 页脚 ----
    ident = next((m for m in ctx["fleet"] if m["id"] == self_id), None) if self_id else None
    if ident:
        foot_txt = f"★ 本机 {ident['zh']}｜{ident.get('role_short', '')} · 哨兵 60s · 判据同源"
    else:
        foot_txt = "哨兵 60s · 圆点=GPU算力(冷青→热粉) · 判据同源"
    f_foot = font(F_MED, int(12 * s))
    clipped = False
    while foot_txt and d.textlength(foot_txt + ("…" if clipped else ""), font=f_foot) > pw - int(44 * s):
        foot_txt = foot_txt[:-1]
        clipped = True
    d.text((x0 + int(22 * s), y0 + ph - int(15 * s)), foot_txt + ("…" if clipped else ""),
           font=f_foot, fill=(122, 128, 150, 165), anchor="lm")

    glow_leds(base, leds, s)

    out = Path(out_path)
    tmp = out.with_suffix(".tmp.png")
    base.convert("RGB").save(tmp, "PNG", optimize=False)
    tmp.replace(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", help="单板模式：底图身份壁纸路径")
    ap.add_argument("--out", help="单板模式：输出 PNG 路径")
    ap.add_argument("--machine", default="", help="单板模式：本机视角 id（空=无视角，兼容 v3 调用）")
    ap.add_argument("--all", action="store_true", help="v4 批量：按台账逐机渲染视角板")
    ap.add_argument("--boards-dir", default=str(BOARDS_DIR_DEFAULT), help="--all 输出目录")
    ap.add_argument("--hub-out", default="", help="--all 时把中枢板另存一份（兼容中枢哨兵 board.png 目标）")
    args = ap.parse_args()
    if not args.all and not (args.base and args.out):
        ap.error("需要 --all，或 --base/--out 单板模式")

    ctx = collect()

    if not args.all:
        render_board(ctx, Path(args.base), Path(args.out), args.machine)
        print(f"board ok -> {args.out}")
        return 0

    boards = Path(args.boards_dir)
    boards.mkdir(parents=True, exist_ok=True)
    failed: list[str] = []
    hub_png: Path | None = None
    for m in ctx["fleet"]:
        mid = m["id"]
        base_p = BRAND_WP / f"{mid}-wallpaper.png"
        out_p = boards / f"{mid}.png"
        try:
            if not base_p.exists():
                raise FileNotFoundError(base_p)
            render_board(ctx, base_p, out_p, mid)
            print(f"board ok -> {out_p}")
            if m["ip"] == HUB_IP:
                hub_png = out_p
        except Exception as e:  # noqa: BLE001
            failed.append(f"{mid}:{type(e).__name__}")
            print(f"board FAIL {mid}: {e}")
    if args.hub_out and hub_png is not None:
        try:
            dst = Path(args.hub_out)
            tmp = dst.with_suffix(".tmp.png")
            tmp.write_bytes(hub_png.read_bytes())
            tmp.replace(dst)
            print(f"hub board -> {dst}")
        except Exception as e:  # noqa: BLE001
            hub_png = None
            print(f"hub-out FAIL: {e}")
    if failed:
        print("partial failures: " + ", ".join(failed))
    # 退出码只看中枢自己的板（中枢哨兵以此决定是否回落静态壁纸）；
    # 节点板失败/陈旧由「小界 404 → 节点回落静态壁纸」独立兜底。
    return 0 if hub_png is not None else 1


if __name__ == "__main__":
    sys.exit(main())
