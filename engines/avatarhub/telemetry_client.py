# -*- coding: utf-8 -*-
"""telemetry_client.py — 崩溃/错误匿名上报客户端（P0 MVP，2026-07-13）。

与 telemetry.py（安装回执）同族共享配置，红线一致并加严：
  * 永不采集：人脸/声音/视频内容、b64 数据、完整用户路径、用户名、IP、令牌、license。
  * 崩溃上报默认开（config "crash": false 或 AVATARHUB_TELEMETRY=0 可关）——它只含
    栈签名与脱敏摘要；使用统计仍走 telemetry.enabled()（默认关 / opt-in），本文件不涉。
  * 本地先落盘（runtime/telemetry/queue.jsonl，可审计），后台 best-effort 补发；
    无网/无端点=只留本地。上报失败绝不影响宿主服务。
  * 去重限频：同一栈签名 24h 只发一次；单日全局 ≤ 20 条；队列上限 500 条自动裁剪。

接入（各服务一行，缺文件/异常=完全无感）：
    try:
        import telemetry_client; telemetry_client.install("faceswap")
    except Exception:
        pass
手动上报可捕获但值得关注的错误：
    telemetry_client.report_error("faceswap", exc=e, context="model_load")
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

try:
    import telemetry as _tele          # 复用 enabled()/anon_id()/配置文件
except Exception:
    _tele = None

try:
    import app_config
    BASE = app_config.BASE
except Exception:                      # 极端兜底：独立运行时以本文件目录为根
    BASE = Path(__file__).resolve().parent

TELE_DIR = BASE / "runtime" / "telemetry"
QUEUE_FILE = TELE_DIR / "queue.jsonl"
STATE_FILE = TELE_DIR / "state.json"
SCHEMA = 2
QUEUE_MAX = 500          # 本地队列行数上限（超出裁掉最旧）
DAILY_MAX = 20           # 单日外发上限（防异常风暴刷服务器/刷流量）
DEDUP_HOURS = 24         # 同签名重发间隔
MSG_MAX = 300            # 异常消息摘要长度
TB_FRAMES = 8            # 栈签名最多帧数


# ── 开关与端点 ─────────────────────────────────────────────────────
def crash_enabled() -> bool:
    """崩溃上报开关：AVATARHUB_TELEMETRY=0 一票否决；config "crash" 显式 false 关；默认开。"""
    env = os.environ.get("AVATARHUB_TELEMETRY", "").strip().lower()
    if env in ("0", "off", "false", "no"):
        return False
    if _tele is None:
        return False
    try:
        return bool(_tele._load_conf().get("crash", True))
    except Exception:
        return True


def set_crash_enabled(on: bool):
    if _tele is None:
        return
    c = _tele._load_conf()
    c["crash"] = bool(on)
    _tele._save_conf(c)


def _endpoint() -> tuple[str, str]:
    """(url, token)：环境变量 > telemetry 持久配置(tele_url) > 本地 manifest.json。
    优先持久配置的原因：app 组件热更新不含 manifest，本地 manifest 停在安装期旧版、
    端点会漂移；启动器每次检查更新时把生效端点 remember_endpoint() 落盘，此处即取新鲜值。"""
    url = os.environ.get("AVATARHUB_TELEMETRY_URL", "").strip()
    tok = os.environ.get("AVATARHUB_TELEMETRY_TOKEN", "").strip()
    if not url and _tele is not None:
        try:
            c = _tele._load_conf()
            url = (c.get("tele_url") or "").strip()
            tok = tok or (c.get("tele_token") or "").strip()
        except Exception:
            pass
    if not url:
        try:
            m = json.loads((BASE / "manifest.json").read_text(encoding="utf-8"))
            url = (m.get("telemetry_url") or "").strip()
            tok = tok or (m.get("telemetry_token") or "").strip()
        except Exception:
            pass
    return url, tok


def remember_endpoint(url: str, token: str = ""):
    """把生效的上报端点落进 telemetry 持久配置（启动器检查更新时调用），
    使崩溃上报不依赖本地 manifest 的新鲜度。"""
    if _tele is None or not url:
        return
    try:
        c = _tele._load_conf()
        c["tele_url"] = url.strip()
        if token:
            c["tele_token"] = token.strip()
        _tele._save_conf(c)
    except Exception:
        pass


# ── 脱敏管道（宁可多删，不可漏删）─────────────────────────────────────
_SCRUB_PATTERNS = [
    (re.compile(r"(?i)[a-z]:[\\/]users[\\/][^\\/\s\"']+"), r"~"),           # C:\Users\<name>
    (re.compile(r"(?i)/(?:home|users)/[^/\s\"']+"), r"~"),                   # /home/<name>
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "x.x.x.x"),                 # IPv4
    (re.compile(r"[A-Za-z0-9+/=]{64,}"), "<b64>"),                           # base64/长密文
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "<hex>"),                          # 长十六进制
    (re.compile(r"(?i)(token|key|secret|password|授权码)[=:\s\"']{1,4}[^\s\"'，。]{6,}"), r"\1=<redacted>"),
]


def scrub(text: str) -> str:
    s = str(text or "")
    for pat, rep in _SCRUB_PATTERNS:
        s = pat.sub(rep, s)
    tok = os.environ.get("AVATARHUB_SERVICE_TOKEN", "")
    if tok and tok in s:
        s = s.replace(tok, "<redacted>")
    return s


def _sig_from_tb(exc_type, tb) -> str:
    """栈签名：项目内帧的 文件名:函数:行号 链 + 异常类名（路径只留文件名，天然无 PII）。"""
    frames = []
    try:
        for fs in traceback.extract_tb(tb)[-TB_FRAMES:]:
            frames.append(f"{Path(fs.filename).name}:{fs.name}:{fs.lineno}")
    except Exception:
        pass
    return "|".join(frames) + "#" + getattr(exc_type, "__name__", "Exception")


def _env_fingerprint() -> dict:
    fp = {"py": sys.version.split()[0], "os": sys.getwindowsversion().build if hasattr(sys, "getwindowsversion") else ""}
    try:
        mk = json.loads((BASE / "app_build.json").read_text(encoding="utf-8"))
        fp["app"] = str(mk.get("version", ""))
    except Exception:
        fp["app"] = ""
    try:
        m = json.loads((BASE / "manifest.json").read_text(encoding="utf-8"))
        fp["release"] = str(m.get("version", ""))
    except Exception:
        pass
    return fp


# ── 队列/状态（本地先落盘）────────────────────────────────────────────
_lock = threading.Lock()


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(st: dict):
    try:
        TELE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _should_send(sig: str) -> bool:
    """签名 24h 去重 + 单日总量闸。判定通过即预记账（防并发重复）。"""
    now = time.time()
    day = time.strftime("%Y%m%d")
    with _lock:
        st = _load_state()
        sent = st.setdefault("sent", {})
        daily = st.setdefault("daily", {})
        if daily.get("day") != day:
            st["daily"] = daily = {"day": day, "n": 0}
        if daily["n"] >= DAILY_MAX:
            return False
        last = sent.get(sig, 0)
        if now - last < DEDUP_HOURS * 3600:
            return False
        sent[sig] = now
        daily["n"] += 1
        if len(sent) > 400:            # 状态文件防膨胀
            for k, _ in sorted(sent.items(), key=lambda kv: kv[1])[:100]:
                sent.pop(k, None)
        _save_state(st)
        return True


def _enqueue(ev: dict):
    try:
        TELE_DIR.mkdir(parents=True, exist_ok=True)
        with _lock:
            with open(QUEUE_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            try:                        # 队列裁剪（保最新）
                lines = QUEUE_FILE.read_text(encoding="utf-8").splitlines()
                if len(lines) > QUEUE_MAX:
                    QUEUE_FILE.write_text("\n".join(lines[-QUEUE_MAX:]) + "\n", encoding="utf-8")
            except Exception:
                pass
    except Exception:
        pass


def flush_async():
    """后台补发队列（每事件独立 POST，成功即从队列摘除；无端点/失败=原样留队）。"""
    url, tok = _endpoint()
    if not (url.startswith(("http://", "https://")) and crash_enabled()):
        return

    def work():
        try:
            with _lock:
                lines = QUEUE_FILE.read_text(encoding="utf-8").splitlines() if QUEUE_FILE.exists() else []
            if not lines:
                return
            keep = []
            for ln in lines:
                ok = False
                try:
                    req = urllib.request.Request(
                        url, data=ln.encode("utf-8"), method="POST",
                        headers={"Content-Type": "application/json", "X-AH-T": tok})
                    with urllib.request.urlopen(req, timeout=8) as r:
                        ok = 200 <= r.status < 300
                except Exception:
                    ok = False
                if not ok:
                    keep.append(ln)
            with _lock:
                if keep:
                    QUEUE_FILE.write_text("\n".join(keep) + "\n", encoding="utf-8")
                else:
                    QUEUE_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    threading.Thread(target=work, daemon=True, name="tele-flush").start()


# ── 事件构造与上报 ───────────────────────────────────────────────────
def _build_event(service: str, kind: str, exc_type=None, exc=None, tb=None,
                 context: str = "") -> dict:
    sig = _sig_from_tb(exc_type, tb) if exc_type else f"{service}:{context}#manual"
    ev = {
        "schema": SCHEMA, "kind": kind,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "service": service, "sig": sig,
        "exc": getattr(exc_type, "__name__", "") if exc_type else "",
        "msg": scrub(str(exc))[:MSG_MAX] if exc is not None else "",
        "context": scrub(context)[:120],
        "env": _env_fingerprint(),
    }
    if tb is not None:
        try:
            ev["tb"] = scrub("".join(traceback.format_exception(exc_type, exc, tb))[-4000:])
        except Exception:
            pass
    try:
        if _tele is not None and crash_enabled():
            ev["anon_id"] = _tele.anon_id()
    except Exception:
        pass
    return ev


def usage_enabled() -> bool:
    """使用统计/心跳开关：opt-in（默认关），与崩溃报告分离。AVATARHUB_TELEMETRY=0 亦一票否决。"""
    if os.environ.get("AVATARHUB_TELEMETRY", "").strip().lower() in ("0", "off", "false", "no"):
        return False
    return bool(_tele is not None and _tele.enabled())


def heartbeat(service: str = "hub", extra: dict | None = None, min_interval_h: int = 20):
    """每日心跳（版本/edition/GPU/在线）——DAU/留存/版本分布的数据源。opt-in；本地限频一天一条。
    绝不含内容数据。失败无感。"""
    try:
        if not usage_enabled():
            return
        now = time.time()
        with _lock:
            st = _load_state()
            if now - float(st.get("hb_ts", 0)) < min_interval_h * 3600:
                return
            st["hb_ts"] = now
            _save_state(st)
        ev = {"schema": SCHEMA, "kind": "heartbeat",
              "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "service": service,
              "sig": f"hb#{service}", "env": _env_fingerprint(),
              "edition": scrub(str((extra or {}).get("edition", "")))[:24],
              "gpu": scrub(str((extra or {}).get("gpu", "")))[:48],
              "vram_gb": (extra or {}).get("vram_gb", 0)}
        if _tele is not None:
            try:
                ev["anon_id"] = _tele.anon_id()
            except Exception:
                pass
        _enqueue(ev)
        flush_async()
    except Exception:
        pass


def report_usage(service: str, counters: dict, min_interval_h: int = 20):
    """每日聚合用量（功能计数/延迟分位；已在本地聚合，此处只上报数字，非逐次事件）。
    opt-in；本地限频一天一条。只收数值/短标签，绝不含内容。失败无感。"""
    try:
        if not usage_enabled() or not counters:
            return
        now = time.time()
        with _lock:
            st = _load_state()
            if now - float(st.get("usage_ts", 0)) < min_interval_h * 3600:
                return
            st["usage_ts"] = now
            _save_state(st)
        safe = {}
        for k, v in list(counters.items())[:40]:
            if isinstance(v, (int, float)):
                safe[str(k)[:32]] = round(v, 3) if isinstance(v, float) else v
        ev = {"schema": SCHEMA, "kind": "usage",
              "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "service": service,
              "sig": f"usage#{service}", "env": _env_fingerprint(), "counters": safe}
        if _tele is not None:
            try:
                ev["anon_id"] = _tele.anon_id()
            except Exception:
                pass
        _enqueue(ev)
        flush_async()
    except Exception:
        pass


def report_error(service: str, exc: BaseException | None = None, context: str = "",
                 kind: str = "error"):
    """手动上报（永不抛错）。本地必落盘；开关开且未触发去重/限频才排队外发。"""
    try:
        if not crash_enabled():
            return
        et = type(exc) if exc is not None else None
        tb = exc.__traceback__ if exc is not None else None
        ev = _build_event(service, kind, et, exc, tb, context)
        if _should_send(ev["sig"]):
            _enqueue(ev)
            flush_async()
    except Exception:
        pass


def build_feedback(note: str = "", contact: str = "", log_tail_lines: int = 120) -> dict:
    """打包一键反馈：环境指纹 + 近期日志尾（脱敏）+ 用户留言/联系方式。返回事件 dict（供预览后发送）。"""
    logs = {}
    try:
        ld = BASE / "logs"
        if ld.is_dir():
            for name in ("service_manager.log", "hub.log", "faceswap.log", "launcher.log"):
                p = ld / name
                if p.is_file():
                    tail = p.read_text(encoding="utf-8", errors="replace").splitlines()[-log_tail_lines:]
                    logs[name] = scrub("\n".join(tail))[-6000:]
    except Exception:
        pass
    ev = {"schema": SCHEMA, "kind": "feedback",
          "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "service": "user",
          "sig": f"feedback#{int(time.time())}", "env": _env_fingerprint(),
          "note": scrub(note)[:1000], "contact": scrub(contact)[:120], "logs": logs}
    if _tele is not None:
        try:
            ev["anon_id"] = _tele.anon_id()
        except Exception:
            pass
    return ev


def send_feedback(ev: dict) -> bool:
    """发送反馈（用户在预览后确认）。反馈是用户主动求助，不受 opt-in 限制，但仍需端点已配。"""
    url, tok = _endpoint()
    if not url.startswith(("http://", "https://")):
        return False
    try:
        req = urllib.request.Request(url, data=json.dumps(ev, ensure_ascii=False).encode("utf-8"),
                                     method="POST",
                                     headers={"Content-Type": "application/json", "X-AH-T": tok})
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def install(service: str) -> bool:
    """挂未捕获异常钩子（主线程 + 子线程），并补发历史队列。返回是否启用。"""
    if not crash_enabled():
        return False
    prev_hook = sys.excepthook

    def _hook(exc_type, exc, tb):
        try:
            if not issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
                ev = _build_event(service, "crash", exc_type, exc, tb)
                if _should_send(ev["sig"]):
                    _enqueue(ev)
                    flush_async()
        except Exception:
            pass
        try:
            prev_hook(exc_type, exc, tb)
        except Exception:
            pass

    sys.excepthook = _hook
    try:
        prev_thook = threading.excepthook

        def _thook(args):
            try:
                if args.exc_type is not None and not issubclass(args.exc_type, SystemExit):
                    ev = _build_event(service, "crash", args.exc_type, args.exc_value,
                                      args.exc_traceback, context=f"thread:{getattr(args.thread, 'name', '')}")
                    if _should_send(ev["sig"]):
                        _enqueue(ev)
                        flush_async()
            except Exception:
                pass
            try:
                prev_thook(args)
            except Exception:
                pass

        threading.excepthook = _thook
    except Exception:
        pass
    flush_async()      # 开机补发历史积压
    print(f"[telemetry] 崩溃上报=开（匿名·栈签名级·可在设置关闭）service={service}")
    return True
