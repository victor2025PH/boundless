# -*- coding: utf-8 -*-
"""隔空指挥网关（P2 · 2026-08-08）——手势/语音/键盘共用的运维动词执行器。

出处《未来感机房展演_手势语音指挥与手机万能外设_五视角优化美化方案_20260808.md》§七 P2
与《集群实况板_五视角优化美化方案_20260807.md》§14（四重护栏原案）。

第一性原则：**客户端零特权**。本模块跑在中枢 hud_server 进程内，所有动词都转发到
Hub 既有守护通道（HTTP API），自己不持有 ssh 凭据、不直接杀进程：
  svc_restart  → POST :9000/api/engine/start?name=   （supervisor.launch_any，本机 Popen）
  route_switch → PATCH :9000/api/config {key:"faceswap", value:url}（热切零重启，P20-5）
  wol          → UDP 魔术包（stdlib socket；machines.json mac 台账 2026-08-08 实采增列）
被误触/被攻破的最坏后果 = 提交一个会被服务端拒绝的请求。

四重护栏（§14.2 全案照落）：
  1. 服务端判决：每动词前置断言 + fire 时**实时**再查 /api/ops/hub_busy（拿不到=拒，fail-closed）；
  2. 两段式武装点火：arm（8s 无点火自动撤防）→ fire（在场信号确认）→ 可逆动作 10s 撤销窗；
  3. 授权：P2 首批=在场信号（手势/语音/键盘=人在指挥台前；遥控不许点火）；
     刷脸授权是 AUTH_MODE="face" 插槽（决策点 5/7 拍板后接 insightface，本批保守降级）；
  4. 应急总闸：logs/hud_ops_disable.flag 存在 → 武装一律拒绝（doctor 可见）。
外加：审计先行（logs/ops_audit.jsonl 写失败即拒执行——不留「做了没记下」）；
      熔断（10min 点火 >6 次 → 熔断 10min，防手势风暴/误识别雪崩）。
"""
from __future__ import annotations

import json
import os
import re
import secrets as _secrets
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HUB = "http://127.0.0.1:9000"
AVATARHUB = Path(r"C:\模仿音色")
AUDIT = AVATARHUB / "logs" / "ops_audit.jsonl"
DISABLE_FLAG = AVATARHUB / "logs" / "hud_ops_disable.flag"
HUB_CONFIG = AVATARHUB / "hub_config.json"
MACHINES = Path(r"D:\boundless\deploy\machines.json")

# ---- 隔空受控（P2 输入注入；2026-08-10 主人拍板 §九决策点）----
# 全项目唯一往远端注入鼠标键盘的路径。执行器是每机 control_agent.py（分家于只读
# desktop_agent），判据全在此。白名单硬编码在服务端——客户端改不动；生产直播链
# （中枢/韵声/听写）永不入白名单=永久只读。密钥台账 secrets/ctrl_agents.json。
CTRL_TARGETS = {"lianbei", "kouxing", "shengbei"}     # 脸备/口型/声备（开发/热备机）
CTRL_DISABLE_FLAG = AVATARHUB / "logs" / "hud_ctrl_disable.flag"   # 受控专用总闸（独立于 ops）
CTRL_AGENTS = AVATARHUB / "secrets" / "ctrl_agents.json"          # {machine: secret}
CTRL_TTL_S = 45              # 受控会话窗；每个动作续期，闲置自动过期
CTRL_TYPE_MAX = 240
CTRL_QUEUE_MAX = 64
CTRL_KINDS = {"move", "click", "dblclick", "rclick", "scroll", "drag", "type", "key"}
CTRL_FUSE_WINDOW_S = 60
CTRL_FUSE_MAX = 80           # 窗内动作上限（防手势风暴/误识别雪崩），超了熔断
CTRL_FUSE_COOL_S = 120

ARM_TTL_S = 8            # 武装后无点火自动撤防（§14.2 原案 5s；手势定位+dwell 1.2s 留余量）
UNDO_TTL_S = 10          # 可逆动作点火后的撤销窗
FUSE_WINDOW_S = 600      # 熔断统计窗
FUSE_MAX_FIRES = 6       # 窗内点火上限，超了熔断
FUSE_COOL_S = 600        # 熔断时长
AUTH_MODE = "presence"   # 基线授权=在场信号（手势/语音/键盘）；刷脸是**叠加层**：
                         # face_auth 登记表非空即自动生效（P2b 已接，登记即开，零配置）

# 换脸路由的两个合法落点（route_switch 白名单——不接受任意 URL）
FACESWAP_TARGETS = {
    "zhongshu": "http://127.0.0.1:8000",
    "lianbei": "http://192.168.0.104:8000",
}

_lock = threading.Lock()
_armed: dict | None = None        # 单例武装位：{id, verb, args, desc, impact, by, src, at, expires}
_undo: dict | None = None         # 撤销位：{verb, restore, until, desc}
_fires: list[float] = []          # 点火时刻（熔断统计）
_fuse_until = 0.0
_seq = 0
_ctrl: dict | None = None         # 单例受控会话：{machine, token, by, until, actions}
_ctrl_q: dict[str, list] = {}     # machine -> 待 agent 领取的动作队列
_ctrl_fires: list[float] = []
_ctrl_fuse_until = 0.0
_ctrl_alive: dict[str, float] = {}   # machine -> 上次持正确密钥轮询时刻（=受控 agent 真在线）
CTRL_ALIVE_S = 8

OPS_LAST = {"phase": "idle", "seq": 0, "at": 0.0, "id": "", "verb": "", "desc": "",
            "impact": "", "by": "", "src": "", "ok": True, "detail": "",
            "expires": 0.0, "undo_until": 0.0}

_deps = {"get_ctx": None, "notify": None, "log_stat": None}


def configure(**kw) -> None:
    _deps.update({k: v for k, v in kw.items() if k in _deps})


# ---------------- 基础设施 ----------------

def _audit(ev: str, **kw) -> bool:
    """审计先行：写不进审计文件就拒绝执行（§14.4）。"""
    rec = {"ts": round(time.time(), 3), "ev": ev}
    rec.update(kw)
    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def _publish(phase: str, **kw) -> None:
    global _seq
    with _lock:
        _seq += 1
        OPS_LAST.update({"phase": phase, "seq": _seq, "at": round(time.time(), 3),
                         "ok": True, "detail": ""})
        for k, v in kw.items():
            OPS_LAST[k] = v
    if _deps["notify"]:
        _deps["notify"]()


def _stat(k: str, v: str = "", src: str = "") -> None:
    if _deps["log_stat"]:
        _deps["log_stat"](k, m="zhongshu", v=v, src=src)


def _hub_busy_now() -> tuple[str, str]:
    """fire 前的实时忙态（不用缓存）：拿不到 = ("unknown", …) → fail-closed 拒点火。"""
    try:
        with urllib.request.urlopen(HUB + "/api/ops/hub_busy", timeout=3) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        s = d.get("summary") or {}
        return str(s.get("restart") or "unknown"), str(s.get("reason") or "")
    except Exception as e:  # noqa: BLE001
        return "unknown", f"忙态接口不可达：{type(e).__name__}"


def _http(method: str, path: str, body: dict | None = None, timeout: float = 15) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(HUB + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


def _machine(mid: str) -> dict | None:
    try:
        d = json.loads(MACHINES.read_text(encoding="utf-8"))
        return next((m for m in d.get("machines", []) if m.get("id") == mid), None)
    except Exception:
        return None


def _ctx_machine(mid: str) -> dict:
    ctx = _deps["get_ctx"]() if _deps["get_ctx"] else {}
    return next((m for m in (ctx.get("machines") or []) if m.get("id") == mid), {})


def _send_magic(mac: str) -> None:
    """WOL 魔术包 ×3（广播 :9）。对在线机器发包=无副作用。"""
    raw = bytes.fromhex(re.sub(r"[^0-9A-Fa-f]", "", mac))
    if len(raw) != 6:
        raise ValueError(f"MAC 格式错：{mac}")
    pkt = b"\xff" * 6 + raw * 16
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        for _ in range(3):
            s.sendto(pkt, ("255.255.255.255", 9))
            time.sleep(0.05)
    finally:
        s.close()


def _faceswap_current() -> str:
    try:
        return str(json.loads(HUB_CONFIG.read_text(encoding="utf-8")).get("faceswap") or "")
    except Exception:
        return ""


def _probe_health(url: str, timeout: float = 4) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _dead_engines() -> list[str]:
    """Hub 离线引擎清单（svc_restart 的合法目标集：只救死的，不动活的）。"""
    try:
        d = _http("GET", "/api/engine/list", timeout=5)
        items = d if isinstance(d, list) else (d.get("engines") or d.get("list") or d.get("items") or [])
        out = []
        for it in items:
            if isinstance(it, str):
                out.append(it)
            elif isinstance(it, dict):
                name = it.get("name") or it.get("service") or ""
                alive = it.get("up") or it.get("alive") or it.get("running")
                if name and not alive:
                    out.append(str(name))
        return out
    except Exception:
        return []


# ---------------- 动词表（T 级白名单；voice/手势句式与本表契约同源对账） ----------------
# verb: {tier, reversible}
VERBS = {
    "wol":          {"tier": "T1", "reversible": False},
    "svc_restart":  {"tier": "T2", "reversible": False},
    "route_switch": {"tier": "T2", "reversible": True},
    # P1 2026-08-13：算力模式切换（chatx⇄face⇄code）。两段式+刷脸之外还有第三重：执行不带 force，
    # 执行器自己的闸门（hub_busy/同传/出片队列/冷却）在受理时再判一遍——双重把关不越权。
    # reversible=False：切换是分钟级重布局，10s 撤销窗语义不成立（要回去就再切一次）。
    "mode_switch":  {"tier": "T2", "reversible": False},
    # P0 联动 2026-08-14：姿态一键归位=按当前模式 force 重放（Hub /api/cluster/repair，
    # 复用执行器全部护栏/事件流/回滚）。force 只越闸门不越不变量，故仍 T2 两段式+刷脸。
    "posture_repair": {"tier": "T2", "reversible": False},
}

_MODES_JSON = Path(r"D:\boundless\deploy\boundless-hud\modes.json")


def _cluster_mode_now() -> tuple[str, dict]:
    """→ (当前模式, modes 定义表)。SSOT 同源：state=logs/cluster_mode.json、定义=modes.json。"""
    cur = ""
    try:
        cur = str(json.loads((AVATARHUB / "logs" / "cluster_mode.json")
                             .read_text(encoding="utf-8")).get("mode") or "")
    except Exception:
        pass
    modes: dict = {}
    try:
        modes = json.loads(_MODES_JSON.read_text(encoding="utf-8-sig")).get("modes") or {}
    except Exception:
        pass
    return cur, modes


def _precheck(verb: str, args: dict) -> tuple[bool, str, str, dict]:
    """武装前置断言。返回 (ok, desc 人话, impact 影响面, 执行上下文)。"""
    if verb == "wol":
        mid = str(args.get("machine") or "")
        m = _machine(mid)
        if not m:
            return False, "", f"不认识的机器「{mid}」", {}
        if not m.get("mac"):
            return False, "", f"{m.get('zh')} 台账无 MAC（machines.json 补列后再来）", {}
        st = _ctx_machine(mid)
        if st.get("status") == "ok":
            return False, "", f"{m.get('zh')} 现在醒着（在线），无需唤醒", {}
        return True, f"网络唤醒 {m.get('zh')}", f"向 {m.get('zh')}（{m.get('ip')}）发 WOL 魔术包 · 零副作用", \
            {"mac": m["mac"], "zh": m.get("zh")}
    if verb == "svc_restart":
        name = str(args.get("service") or "")
        dead = _dead_engines()
        if not name:
            return False, "", "没说要救哪个服务", {}
        if dead and name not in dead:
            return False, "", f"「{name}」不在离线清单（活着的服务不许动——只救死的）", {}
        return True, f"拉起离线服务 {name}", f"POST /api/engine/start · 本机进程拉起 · 占用对应显存", \
            {"name": name}
    if verb == "route_switch":
        target = str(args.get("target") or "")
        url = FACESWAP_TARGETS.get(target)
        if not url:
            return False, "", f"换脸路由只认 {'/'.join(FACESWAP_TARGETS)}，不认识「{target}」", {}
        cur = _faceswap_current()
        if cur == url:
            return False, "", f"换脸主引擎已经在{('中枢' if target == 'zhongshu' else '脸备')}，无需切换", {}
        if not _probe_health(url):
            return False, "", f"目标 {url} 探活失败——不切给一台病机", {}
        zh = "中枢" if target == "zhongshu" else "脸备"
        return True, f"换脸主引擎切至{zh}", \
            f"PATCH faceswap {cur} → {url} · 影响：直播换脸路径 · 热切零重启 · 可撤销", \
            {"url": url, "restore": cur, "zh": zh}
    if verb == "mode_switch":
        target = str(args.get("target") or "")
        cur, modes = _cluster_mode_now()
        if target not in modes:
            return False, "", f"算力模式只认 {'/'.join(sorted(modes)) or 'chatx/face'}，不认识「{target}」", {}
        if target == cur:
            return False, "", f"已经是{modes[cur].get('zh', cur)}（{cur}），无需切换", {}
        zh = modes[target].get("zh", target)
        gate_note = ""
        try:   # 执行器闸门预览（dry_run 与真跑同源）——武装时就把「此刻不宜」摆到台面
            d = _http("POST", "/api/cluster/mode",
                      body={"target": target, "dry_run": True}, timeout=20)
            rs = (d.get("gate") or {}).get("reasons") or []
            gate_note = "闸门：" + ("；".join(rs) if rs else "此刻可安全切换")
        except Exception:
            gate_note = "闸门：预览不可达（点火时执行器仍会实判）"
        return True, f"算力模式切换 {cur or '?'}→{target}（{zh}）", \
            f"{modes[target].get('desc', '')[:60]} · 分钟级显存重布局 · {gate_note}", \
            {"target": target, "zh": zh}
    if verb == "posture_repair":
        cur, modes = _cluster_mode_now()
        if not cur or cur == "unknown":
            return False, "", "当前算力模式不明（先明确切一次模式再谈归位）", {}
        zh = modes.get(cur, {}).get("zh", cur)
        drift = "巡检结论缺席/过期——归位=重申当前模式，无害"
        try:
            po = json.loads((AVATARHUB / "logs" / "cluster_posture.json")
                            .read_text(encoding="utf-8"))
            fresh = time.time() - float(po.get("ts") or 0) < 1800 and not po.get("skip")
            errs = po.get("errors") or []
            if fresh and not errs:
                return False, "", f"姿态巡检结论=健康（{zh}），无需归位", {}
            if errs:
                drift = f"漂移 {len(errs)} 处：" + str(errs[0])[:50]
        except Exception:
            pass
        return True, f"姿态一键归位（重申{zh}）", \
            f"POST /api/cluster/repair · force 重放当前模式全部步骤（分钟级） · {drift}", {"zh": zh}
    return False, "", f"动词「{verb}」不在白名单", {}


def _execute(verb: str, ectx: dict) -> tuple[bool, str]:
    if verb == "wol":
        _send_magic(ectx["mac"])
        return True, f"魔术包已发出 ×3（{ectx['zh']}）；上线以实况板变绿为准"
    if verb == "svc_restart":
        d = _http("POST", f"/api/engine/start?name={ectx['name']}", body=None, timeout=30)
        ok = bool(d.get("ok", d))
        return ok, json.dumps(d, ensure_ascii=False)[:160]
    if verb == "route_switch":
        d = _http("PATCH", "/api/config", body={"key": "faceswap", "value": ectx["url"]}, timeout=10)
        ok = bool(d.get("ok", True)) and _faceswap_current() == ectx["url"]
        return ok, f"hub_config.faceswap={_faceswap_current()}"
    if verb == "mode_switch":
        # 不带 force：执行器闸门（hub_busy/同传/出片队列/冷却）受理时实判——被拒=转人话如实报
        try:
            d = _http("POST", "/api/cluster/mode",
                      body={"target": ectx["target"],
                            "reason": "隔空指挥（ops_gateway 两段式+刷脸点火）"}, timeout=30)
        except urllib.error.HTTPError as e:
            try:
                det = json.loads(e.read().decode("utf-8", "replace")).get("detail") or {}
            except Exception:
                det = {}
            rs = (det.get("gate") or {}).get("reasons") if isinstance(det, dict) else None
            return False, "执行器拒绝：" + ("；".join(rs) if rs else f"HTTP {e.code}")
        if d.get("noop"):
            return True, str(d.get("detail") or "已在目标模式")
        ok = bool(d.get("ok"))
        return ok, (f"已受理（{d.get('steps_total', '?')} 步）——进度看实况板绶带/HUD 绶带，"
                    f"事件流 cluster_mode_events.jsonl" if ok
                    else json.dumps(d, ensure_ascii=False)[:160])
    if verb == "posture_repair":
        try:
            d = _http("POST", "/api/cluster/repair",
                      body={"reason": "隔空指挥（ops_gateway 姿态归位·两段式+刷脸点火）"},
                      timeout=30)
        except urllib.error.HTTPError as e:
            try:
                det = json.loads(e.read().decode("utf-8", "replace")).get("detail")
            except Exception:
                det = ""
            return False, f"执行器拒绝：{det or ('HTTP ' + str(e.code))}"
        ok = bool(d.get("ok"))
        return ok, (f"已受理归位（{d.get('steps_total', '?')} 步）——进度看实况板绶带/HUD 绶带" if ok
                    else json.dumps(d, ensure_ascii=False)[:160])
    return False, "unreachable"


# ---------------- 武装 / 点火 / 撤防 / 撤销 ----------------

def arm(verb: str, args: dict, by: str = "", src: str = "") -> dict:
    global _armed
    if DISABLE_FLAG.exists():
        _stat("ops_deny", "flag", src)
        return {"ok": False, "error": "隔空指挥总闸已关（hud_ops_disable.flag 在位）"}
    if time.time() < _fuse_until:
        _stat("ops_deny", "fuse", src)
        return {"ok": False, "error": f"熔断中，{round(_fuse_until - time.time())}s 后恢复"}
    if verb not in VERBS:
        _stat("ops_deny", "verb", src)
        return {"ok": False, "error": f"动词「{verb}」不在白名单"}
    verdict = "safe"
    if _deps["get_ctx"]:
        verdict = (_deps["get_ctx"]() or {}).get("verdict") or "safe"
    if verdict == "danger":
        _stat("ops_deny", "danger", src)
        return {"ok": False, "error": "直播/推流中：隔空指挥整层冻结（推流硬闸）"}
    ok, desc, impact, ectx = _precheck(verb, args or {})
    if not ok:
        _audit("deny", verb=verb, args=args, by=by, src=src, detail=impact)
        _stat("ops_deny", "precheck", src)
        _publish("denied", verb=verb, desc=verb, impact=impact, by=by, src=src,
                 ok=False, detail=impact, id="", expires=0.0, undo_until=0.0)
        return {"ok": False, "error": impact}
    aid = f"{int(time.time() * 1000) % 100000000:08d}"
    rec = {"id": aid, "verb": verb, "args": args, "desc": desc, "impact": impact,
           "by": by[:40], "src": src[:16], "at": time.time(),
           "expires": time.time() + ARM_TTL_S, "ectx": ectx}
    if not _audit("arm", id=aid, verb=verb, args=args, by=by, src=src, verdict=verdict):
        return {"ok": False, "error": "审计写入失败——按纪律拒绝武装"}
    with _lock:
        _armed = rec
    _stat("ops_arm", verb, src)
    _publish("armed", id=aid, verb=verb, desc=desc, impact=impact, by=by, src=src,
             expires=rec["expires"], undo_until=0.0)
    threading.Timer(ARM_TTL_S + 0.5, _expire_check, args=(aid,)).start()
    return {"ok": True, "id": aid, "desc": desc, "impact": impact,
            "expires_in": ARM_TTL_S, "tier": VERBS[verb]["tier"]}


def _expire_check(aid: str) -> None:
    global _armed
    with _lock:
        cur = _armed
        if not cur or cur["id"] != aid or time.time() < cur["expires"]:
            return
        _armed = None
    _audit("expire", id=aid, verb=cur["verb"])
    _publish("disarmed", id=aid, verb=cur["verb"], desc=cur["desc"],
             impact="超时未点火，自动撤防", expires=0.0, undo_until=0.0)


def fire(aid: str, src: str = "", frame_b64: str = "") -> dict:
    global _armed, _undo, _fuse_until
    with _lock:
        cur = _armed
    if not cur or (aid and cur["id"] != aid):
        return {"ok": False, "error": "没有武装中的动作（先武装再点火）"}
    if time.time() > cur["expires"]:
        return {"ok": False, "error": "武装已超时撤防，请重新武装"}
    # 授权层 1：在场信号（手势/语音/键盘=人在指挥台前；遥控永远不许点火）
    if src not in ("gesture", "voice", "key"):
        _stat("ops_deny", "auth_src", src)
        return {"ok": False, "error": f"点火只认在场信号（手势/语音/键盘），「{src}」不行"}
    # 授权层 2（P2b 刷脸·叠加不是替换）：登记表非空即生效——「系统认识主人」。
    # 判据同源 face_auth（buffalo_l + 0.40 = gallery_audit 同款）；帧内存验证即弃。
    fired_by = cur["by"]
    import face_auth  # noqa: PLC0415  惰性：未登记时零开销
    if face_auth.enabled():
        okf, who, cos, why = face_auth.verify(frame_b64)
        if not okf:
            _audit("deny", id=cur["id"], verb=cur["verb"], src=src,
                   detail=f"face: {why[:80]} cos={cos:.2f}")
            _stat("ops_deny", "auth_face", src)
            _publish("denied", id=cur["id"], verb=cur["verb"], desc=cur["desc"],
                     impact=f"刷脸未通过：{why[:60]}", ok=False, detail=why[:120],
                     expires=0.0, undo_until=0.0)
            return {"ok": False, "error": f"刷脸未通过：{why[:80]}"}
        fired_by = f"{who}(刷脸 {cos:.2f})"      # 审计记名：谁点的火从此有名有据
    now = time.time()
    fires = [t for t in _fires if now - t < FUSE_WINDOW_S]
    if len(fires) >= FUSE_MAX_FIRES:
        _fuse_until = now + FUSE_COOL_S
        _audit("fuse", detail=f"{len(fires)} fires in {FUSE_WINDOW_S}s")
        _stat("ops_deny", "fuse_trip", src)
        return {"ok": False, "error": f"点火过于频繁，熔断 {FUSE_COOL_S // 60} 分钟"}
    verdict, reason = _hub_busy_now()      # 实时二查（fail-closed）
    if VERBS[cur["verb"]]["tier"] == "T2" and verdict != "safe":
        _audit("deny", id=cur["id"], verb=cur["verb"], src=src,
               detail=f"busy verdict={verdict} {reason[:80]}")
        _stat("ops_deny", "busy", src)
        _publish("denied", id=cur["id"], verb=cur["verb"], desc=cur["desc"],
                 impact=f"被忙态否决（{verdict}）：{reason[:60]}", ok=False,
                 detail=reason[:120], expires=0.0, undo_until=0.0)
        with _lock:
            _armed = None
        return {"ok": False, "error": f"被忙态否决（{verdict}）：{reason[:80]}"}
    if not _audit("fire", id=cur["id"], verb=cur["verb"], args=cur["args"],
                  by=fired_by, src=src, verdict=verdict):
        return {"ok": False, "error": "审计写入失败——按纪律拒绝点火"}
    with _lock:
        _armed = None
        _fires.append(now)
        _fires[:] = [t for t in _fires if now - t < FUSE_WINDOW_S]
    try:
        ok, detail = _execute(cur["verb"], cur["ectx"])
    except Exception as e:  # noqa: BLE001
        ok, detail = False, f"{type(e).__name__}: {str(e)[:120]}"
    _audit("result", id=cur["id"], verb=cur["verb"], ok=ok, detail=detail[:200])
    _stat("ops_fire", cur["verb"], src)
    undo_until = 0.0
    if ok and VERBS[cur["verb"]]["reversible"]:
        undo_until = time.time() + UNDO_TTL_S
        with _lock:
            _undo = {"verb": cur["verb"], "restore": cur["ectx"].get("restore"),
                     "until": undo_until, "desc": cur["desc"]}
        threading.Timer(UNDO_TTL_S + 0.5, _undo_expire).start()
    _publish("fired", id=cur["id"], verb=cur["verb"], desc=cur["desc"],
             impact=cur["impact"], by=fired_by, src=src, ok=ok,
             detail=detail[:120], expires=0.0, undo_until=undo_until)
    return {"ok": ok, "detail": detail[:160], "undo_in": UNDO_TTL_S if undo_until else 0}


def _undo_expire() -> None:
    global _undo
    with _lock:
        if _undo and time.time() >= _undo["until"]:
            _undo = None


def disarm(aid: str = "", src: str = "") -> dict:
    global _armed
    with _lock:
        cur = _armed
        if not cur or (aid and cur["id"] != aid):
            return {"ok": False, "error": "没有武装中的动作"}
        _armed = None
    _audit("disarm", id=cur["id"], verb=cur["verb"], src=src)
    _publish("disarmed", id=cur["id"], verb=cur["verb"], desc=cur["desc"],
             impact="已撤防", expires=0.0, undo_until=0.0)
    return {"ok": True}


def undo(src: str = "") -> dict:
    global _undo
    with _lock:
        u = _undo
        _undo = None
    if not u or time.time() > u["until"]:
        return {"ok": False, "error": "没有可撤销的动作（撤销窗 10s 已过）"}
    if u["verb"] == "route_switch" and u.get("restore"):
        try:
            _http("PATCH", "/api/config", body={"key": "faceswap", "value": u["restore"]}, timeout=10)
            ok = _faceswap_current() == u["restore"]
        except Exception as e:  # noqa: BLE001
            ok = False
        _audit("undo", verb=u["verb"], ok=ok, detail=u["restore"])
        _stat("ops_undo", u["verb"], src)
        _publish("undone", verb=u["verb"], desc=u["desc"],
                 impact=f"已撤销，换脸路由切回 {u['restore']}", ok=ok,
                 expires=0.0, undo_until=0.0, id="", by="", src=src)
        return {"ok": ok}
    return {"ok": False, "error": "该动作不可撤销"}


# ---------------- 状态与事件流拼接 ----------------

def state() -> dict:
    with _lock:
        armed = dict(_armed) if _armed else None
        if armed:
            armed.pop("ectx", None)
    try:
        import face_auth  # noqa: PLC0415
        auth = "presence+face" if face_auth.enabled() else "presence"
        operators = face_auth.status()["operators"]
    except Exception:
        auth, operators = AUTH_MODE, []
    return {"armed": armed, "fuse_until": round(_fuse_until) if _fuse_until > time.time() else 0,
            "flag": DISABLE_FLAG.exists(), "auth_mode": auth, "operators": operators,
            "last_seq": OPS_LAST["seq"],
            "audit_age_s": round(time.time() - AUDIT.stat().st_mtime) if AUDIT.is_file() else None}


def recent_events(n: int = 2, window_s: int = 900) -> list[dict]:
    """最近点火/撤销记录 → HUD 事件流兼容结构（操作即叙事，§14.4）。
    只拼 fired/undo（arm/deny 噪音大不进屏）；板 PNG 保持分钟级不拼（边界记档）。"""
    if not AUDIT.is_file():
        return []
    out = []
    try:
        lines = AUDIT.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        for ln in reversed(lines):
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if r.get("ev") not in ("result", "undo"):
                continue
            if time.time() - float(r.get("ts") or 0) > window_s:
                break
            t = time.strftime("%H:%M", time.localtime(float(r["ts"])))
            mark = "✓" if r.get("ok") else "✗"
            name = {"route_switch": "换脸路由切换", "svc_restart": "拉起服务",
                    "wol": "网络唤醒"}.get(r.get("verb"), r.get("verb"))
            if r.get("ev") == "undo":
                name = f"{name} 撤销"
            out.append({"t": t, "title": f"⚡ 隔空指挥：{name} {mark}",
                        "level": "ok" if r.get("ok") else "warn",
                        "state": "resolved", "key": "", "ack": False})
            if len(out) >= n:
                break
    except OSError:
        return []
    return out


# ---------------- 隔空受控（P2 输入注入·会话式；四重护栏全复用） ----------------

def _ctrl_secret(mid: str) -> str:
    try:
        return str(json.loads(CTRL_AGENTS.read_text(encoding="utf-8")).get(mid) or "")
    except Exception:
        return ""


def _ctrl_end(reason: str = "") -> None:
    global _ctrl
    with _lock:
        cur = _ctrl
        _ctrl = None
        if cur:
            _ctrl_q[cur["machine"]] = []       # 会话一断，积压动作即清（绝不迟发）
    if cur:
        _audit("ctrl_end", machine=cur["machine"], reason=reason, actions=cur.get("actions", 0))
        if _deps["notify"]:
            _deps["notify"]()


def _verdict_now() -> str:
    if _deps["get_ctx"]:
        return (_deps["get_ctx"]() or {}).get("verdict") or "safe"
    return "safe"


def ctrl_state() -> dict:
    """doctor「隔空受控」+ HUD/SSE 读它（六屏同源：谁在被操控）。不外泄 token。"""
    with _lock:
        s = None
        if _ctrl:
            s = {"machine": _ctrl["machine"], "by": _ctrl["by"],
                 "until": round(_ctrl["until"]), "actions": _ctrl["actions"]}
        queued = {m: len(q) for m, q in _ctrl_q.items() if q}
        now = time.time()
        alive = sorted(m for m, t in _ctrl_alive.items() if now - t < CTRL_ALIVE_S)
    deployed = sorted(m for m in CTRL_TARGETS if _ctrl_secret(m))
    return {"session": s, "targets": sorted(CTRL_TARGETS), "deployed": deployed,
            "alive": alive, "flag": CTRL_DISABLE_FLAG.exists(),
            "fuse_until": round(_ctrl_fuse_until) if _ctrl_fuse_until > time.time() else 0,
            "queued": queued}


def ctrl_arm(machine: str, by: str = "", src: str = "", frame_b64: str = "") -> dict:
    """武装受控会话：白名单+已部署+在场信号+推流硬闸+**强制刷脸**（比 ops 更严：登记表空即拒）。"""
    global _ctrl
    mid = str(machine or "").strip().lower()
    if CTRL_DISABLE_FLAG.exists() or DISABLE_FLAG.exists():
        _stat("ctrl_deny", "flag", src)
        return {"ok": False, "error": "隔空受控总闸已关（hud_ctrl_disable.flag 在位）"}
    if time.time() < _ctrl_fuse_until:
        _stat("ctrl_deny", "fuse", src)
        return {"ok": False, "error": f"受控熔断中，{round(_ctrl_fuse_until - time.time())}s 后恢复"}
    if mid not in CTRL_TARGETS:
        _stat("ctrl_deny", "target", src)
        return {"ok": False, "error": f"「{mid}」不在受控白名单（仅 {'/'.join(sorted(CTRL_TARGETS))}；生产机永久只读）"}
    if not _ctrl_secret(mid):
        _stat("ctrl_deny", "noagent", src)
        return {"ok": False, "error": f"{mid} 未部署受控 agent（先 deploy_control_agent.ps1）"}
    if src not in ("gesture", "voice", "key", "mouse"):
        _stat("ctrl_deny", "auth_src", src)
        return {"ok": False, "error": "受控只认在场信号（人在指挥台前），遥控不许"}
    verdict = _verdict_now()
    if verdict != "safe":
        _stat("ctrl_deny", "busy", src)
        return {"ok": False, "error": f"直播/推流中：隔空受控整层冻结（推流硬闸 · {verdict}）"}
    import face_auth  # noqa: PLC0415
    if not face_auth.enabled():
        _stat("ctrl_deny", "noface", src)
        return {"ok": False, "error": "隔空受控必须刷脸授权，但未登记操作员（先 face_auth.py enroll 主人正脸）"}
    okf, who, cos, why = face_auth.verify(frame_b64)
    if not okf:
        _audit("ctrl_deny", machine=mid, src=src, detail=f"face:{why[:80]} cos={cos:.2f}")
        _stat("ctrl_deny", "face", src)
        return {"ok": False, "error": f"刷脸未通过：{why[:80]}"}
    token = _secrets.token_hex(16)
    fired_by = f"{who}(刷脸 {cos:.2f})"
    if not _audit("ctrl_arm", machine=mid, by=fired_by, src=src, verdict=verdict):
        return {"ok": False, "error": "审计写入失败——按纪律拒绝武装"}
    with _lock:
        _ctrl = {"machine": mid, "token": token, "by": fired_by,
                 "until": time.time() + CTRL_TTL_S, "actions": 0}
        _ctrl_q[mid] = []
    _stat("ctrl_arm", mid, src)
    if _deps["notify"]:
        _deps["notify"]()
    return {"ok": True, "token": token, "machine": mid, "by": who,
            "until": round(time.time() + CTRL_TTL_S), "ttl": CTRL_TTL_S}


def ctrl_action(token: str, kind: str, payload: dict, src: str = "") -> dict:
    """会话内一次离散动作。每次都实时二查：总闸/超时/推流硬闸/熔断（fail-closed）。"""
    global _ctrl_fuse_until
    with _lock:
        cur = _ctrl
    if not cur or token != cur["token"]:
        return {"ok": False, "error": "无有效受控会话（先刷脸武装）"}
    if time.time() > cur["until"]:
        _ctrl_end("timeout")
        return {"ok": False, "error": "受控会话已超时，请重新武装"}
    if CTRL_DISABLE_FLAG.exists() or DISABLE_FLAG.exists():
        _ctrl_end("flag")
        return {"ok": False, "error": "隔空受控总闸已关，会话切断"}
    if kind not in CTRL_KINDS:
        return {"ok": False, "error": f"动作「{kind}」不识别"}
    verdict = _verdict_now()
    if verdict != "safe":                       # 会话中途开播=立即硬切
        _ctrl_end(f"busy:{verdict}")
        return {"ok": False, "error": f"推流硬闸：会话中途检测到 {verdict}，受控已切断"}
    now = time.time()
    fires = [t for t in _ctrl_fires if now - t < CTRL_FUSE_WINDOW_S]
    if len(fires) >= CTRL_FUSE_MAX:
        _ctrl_fuse_until = now + CTRL_FUSE_COOL_S
        _audit("ctrl_fuse", detail=f"{len(fires)} actions in {CTRL_FUSE_WINDOW_S}s")
        _ctrl_end("fuse")
        _stat("ctrl_deny", "fuse_trip", src)
        return {"ok": False, "error": f"动作过于频繁，受控熔断 {CTRL_FUSE_COOL_S // 60} 分钟"}
    ev: dict = {"kind": kind}
    for k in ("nx", "ny", "nx2", "ny2"):
        v = payload.get(k)
        if v is not None:
            try:
                ev[k] = max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                return {"ok": False, "error": f"坐标 {k} 非法"}
    if kind == "scroll":
        ev["dy"] = max(-10, min(10, int(payload.get("dy") or 0)))
    if kind == "type":
        ev["text"] = str(payload.get("text") or "")[:CTRL_TYPE_MAX]
    if kind == "key":
        ev["key"] = str(payload.get("key") or "")[:16]
    with _lock:
        q = _ctrl_q.setdefault(cur["machine"], [])
        q.append(ev)
        _ctrl_q[cur["machine"]] = q[-CTRL_QUEUE_MAX:]
        _ctrl["until"] = now + CTRL_TTL_S       # 活跃续期
        _ctrl["actions"] += 1
        _ctrl_fires.append(now)
        _ctrl_fires[:] = [t for t in _ctrl_fires if now - t < CTRL_FUSE_WINDOW_S]
    if kind != "move":                          # move 太碎不进审计；点按/键入逐条留痕
        _audit("ctrl_act", machine=cur["machine"], kind=kind, by=cur["by"], src=src,
               pos=[ev.get("nx"), ev.get("ny")],
               detail=(ev.get("key") or (ev.get("text") or "")[:24]))
    _stat("ctrl_act", kind, src)
    if _deps["notify"]:
        _deps["notify"]()
    return {"ok": True}


def ctrl_pull(machine: str, secret: str) -> dict:
    """control_agent 轮询：密钥核对 + 会话核对，只有当前被授予才投递动作（否则清空积压）。"""
    mid = str(machine or "").strip().lower()
    if mid not in CTRL_TARGETS:
        return {"granted": False, "events": []}
    want = _ctrl_secret(mid)
    if not want or secret != want:              # 密钥不符：不返任何动作（防越权投递）
        return {"granted": False, "events": []}
    with _lock:
        _ctrl_alive[mid] = time.time()          # 持正确密钥轮询=该机受控 agent 真在线
        cur = _ctrl
        granted = bool(cur and cur["machine"] == mid and time.time() <= cur["until"]
                       and not CTRL_DISABLE_FLAG.exists())
        if granted:
            evs = _ctrl_q.get(mid, [])
            _ctrl_q[mid] = []
        else:
            _ctrl_q[mid] = []                   # 未授权：清空积压，绝不投递
            evs = []
    return {"granted": granted, "events": evs}


def ctrl_disarm(token: str = "", src: str = "") -> dict:
    with _lock:
        cur = _ctrl
    if not cur or (token and token != cur["token"]):
        return {"ok": False, "error": "无受控会话"}
    _ctrl_end(f"disarm:{src}")
    return {"ok": True}


def ctrl_active_machine() -> str:
    """当前被操控的机器（无会话=空）。语音听写路由用它判断"往哪台打字"。"""
    with _lock:
        return _ctrl["machine"] if _ctrl else ""


def _ctrl_inject_active(kind: str, payload: dict, src: str) -> dict:
    """进程内可信调用（语音听写路径）：往**当前唯一**会话注入一次动作，无需 token。
    仍走 ctrl_action=推流硬闸/总闸/熔断/超时/审计一个不少（安全语义与手势点击完全同源）。"""
    with _lock:
        cur = _ctrl
    if not cur:
        return {"ok": False, "error": "没有受控会话（先刷脸武装某台机再打字）"}
    return ctrl_action(cur["token"], kind, payload, src=src)


def ctrl_dictate(text: str, src: str = "voice") -> dict:
    return _ctrl_inject_active("type", {"text": str(text or "")}, src)


def ctrl_key_active(key: str, src: str = "voice") -> dict:
    return _ctrl_inject_active("key", {"key": str(key or "")}, src)
