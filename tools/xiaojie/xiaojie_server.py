# -*- coding: utf-8 -*-
"""小界 · 集群值班 AI —— 桌面机器人后端（只跑中枢机，:7912）。

刻意纯标准库（http.server + urllib）：零第三方依赖，任何 Python 都能跑。
六台机的机器人窗口（Edge --app）都指向本服务，密钥只留中枢。

端点：
  GET  /robot?machine=<id>   机器人页面（robot.html，同目录热改生效）
  GET  /board/<id>.png       该机视角的集群实况板 PNG（P0 六机广播位，2026-08-07）：
                             中枢哨兵每分钟 render_cluster_board --all 产出，节点哨兵按分钟拉取；
                             新鲜度判决在服务端——陈旧(>3min)/未知 id 一律 404，
                             节点拿不到 200 就回落静态身份壁纸，判据不下放
  GET  /api/context          集群快照（板子同源数据文件，零额外探测：
                             history.jsonl 末行 + alerts_state + hub_busy 缓存）
  POST /api/chat             {machine, messages[]} → LLM 回复（deepseek → qwen14b-173 容灾；
                             系统提示词注入实时快照；只读人设，不执行任何操作）
  POST /api/tts              {text} → WAV 音频（fish_tts :7855 代理，默认音色）

判据同源纪律：小界回答用的数据与桌面实况板完全同一来源（board_state/history/alerts_state/
hub_busy）——机器人嘴里的集群状态永远和壁纸上画的一致。
"""
from __future__ import annotations

import base64
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = Path(r"D:\boundless")
AVATARHUB = Path(r"C:\模仿音色")
MACHINES = ROOT / "deploy" / "machines.json"
HISTORY = Path(r"C:\Users\Public\boundless-hud\history.jsonl")
ALERTS_STATE = AVATARHUB / "logs" / "alerts_state.json"
LLM_BACKENDS = AVATARHUB / "llm_backends.json"
HUB = "http://127.0.0.1:9000"
FISH = "http://127.0.0.1:7855"
PORT = 7912
BOARDS = Path(r"C:\Users\Public\boundless-hud\boards")
BOARD_STALE_S = 180  # 板子 mtime 超此秒数视为陈旧：宁可 404 让节点回落静态壁纸，不发过期情报

SYSTEM_PROMPT = """你是「小界」，无界科技六机 GPU 集群的桌面值班 AI，驻守在每台机器的桌面上。
你所在的这台机器是：{machine_line}

你的性格：专业、简洁、有温度，像一位靠谱的值班工程师同事。中文回答，口语化，默认 3 句话以内说清楚；
用户明确要详细分析时才展开。可以用少量 emoji。

铁律：
1. 回答集群状态问题时，只依据下方「实时快照」，并在句中自然引用数据（如「韵声显存 30.6/32G」）。
   快照里没有的信息就直说不知道，绝不编造。
2. 你是只读顾问：不执行任何操作。用户要重启/停服务时，给出建议命令文本并提醒安全纪律
   （重启 Hub 前先看忙态判定；直播中禁止重启）。
3. 判断「能不能重启 Hub」只认快照里的 verdict 字段：safe=可以，wait=建议等，danger=禁止（直播中）。
4. 集群机器：中枢(zhongshu,Hub/换脸/同传主力)、韵声(yunsheng,质量轨TTS+LLM容灾)、
   声备(shengbei,qwen3+TTS热备+坐席)、脸备(lianbei,换脸热备)、听写(tingxie,STT专机)、
   口型(kouxing,口型热备+获客)。别名可 ssh 直呼。
5. 常用参考：静态租约操作单在 docs/静态租约_路由器操作单_20260806.md；
   台账对账 python tools/machines_lint.py --remote；六机 SSH 台账 docs/MACHINE_SSH.md。

实时快照（{snap_ts}）：
{snapshot}
"""


# ---------------- 数据 ----------------

def _read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return default


def _machines() -> list[dict]:
    d = _read_json(MACHINES, {}) or {}
    return d.get("machines", [])


_busy_cache = {"ts": 0.0, "data": {}}


def _hub_busy() -> dict:
    now = time.time()
    if now - _busy_cache["ts"] < 30:
        return _busy_cache["data"]
    try:
        with urllib.request.urlopen(f"{HUB}/api/ops/hub_busy", timeout=3) as r:
            _busy_cache["data"] = json.loads(r.read().decode("utf-8", "replace"))
            _busy_cache["ts"] = now
    except Exception:
        pass
    return _busy_cache["data"]


def _last_history() -> dict:
    try:
        lines = HISTORY.read_text(encoding="utf-8", errors="replace").splitlines()
        for ln in reversed(lines[-5:]):
            try:
                return json.loads(ln)
            except Exception:
                continue
    except Exception:
        pass
    return {}


def resolve_skin() -> str:
    """小界皮肤：xiaojie_config.json {"skin":"auto|normal|loong"}；auto=春节窗口自动换祥龙金鳞。
    服务端统一下发 → 六机同步换装零重发。"""
    cfg = _read_json(HERE / "xiaojie_config.json", {}) or {}
    mode = str(cfg.get("skin") or "auto").lower()
    if mode in ("normal", "loong"):
        return mode
    cny = {  # 春节 → 元宵后数日（近似窗口，够用到 2030）
        2026: ("02-17", "03-05"), 2027: ("02-06", "02-22"), 2028: ("01-26", "02-11"),
        2029: ("02-13", "03-01"), 2030: ("02-03", "02-19"),
    }
    now = time.localtime()
    win = cny.get(now.tm_year)
    if win:
        today = time.strftime("%m-%d", now)
        if win[0] <= today <= win[1]:
            return "loong"
    return "normal"


def build_context() -> dict:
    fleet = _machines()
    hist = _last_history()
    hm = hist.get("m") or {}
    busy = _hub_busy()
    summary = busy.get("summary") or {}
    verdict = str(summary.get("restart") or hist.get("v") or "?")
    reason = str(summary.get("reason") or "")
    st_map = {"o": "ok", "d": "degraded", "s": "selfwarn", "f": "offline"}
    machines = []
    worst = "ok"
    for m in fleet:
        mid = m["id"]
        rec = hm.get(mid) or {}
        s_raw = str(rec.get("s") or "?")
        status = st_map.get(s_raw, "unknown")
        if status in ("offline",) or (status == "degraded" and worst == "ok"):
            worst = "down" if status == "offline" else "warn"
        machines.append({
            "id": mid, "zh": m["zh"], "role": m.get("role_short", ""),
            "status": status, "lat_ms": rec.get("l"),
            "vram_used_mb": rec.get("vu"), "vram_total_mb": rec.get("vt"),
            # v4.1（P1 活体 HUD 增列，机器人同享）：GPU 算力 / 机器识别色 / 现役职能
            "gpu_util": rec.get("u"), "accent": m.get("accent", "#8888AA"),
            "role_now": m.get("role_now", ""),
        })
    events = []
    fresh_bad = False   # 24h 内新起的严重告警才点亮小界红瞳——陈年 firing 常驻红眼=告警疲劳
    st = _read_json(ALERTS_STATE, {}) or {}
    now = time.time()
    for k, v in st.items():
        if not isinstance(v, dict):
            continue
        if v.get("status") == "firing":
            # v4.2（P2）：带 key/ack——HUD 竖拇指确认要 key 做靶，acked 在各屏统一降噪显示
            events.append({"t": v.get("since_str", ""), "title": str(v.get("title") or k),
                           "level": v.get("level", "warn"), "state": "firing",
                           "key": k, "ack": bool(v.get("ack"))})
            if (v.get("level") in ("error", "critical") and not v.get("ack")
                    and now - float(v.get("since") or 0) < 24 * 3600):
                fresh_bad = True
        elif v.get("status") == "resolved" and now - float(v.get("resolved_at") or 0) < 2 * 3600:
            events.append({"t": v.get("resolved_str", ""), "title": str(v.get("title") or k),
                           "level": "ok", "state": "resolved", "key": k, "ack": False})
    events.sort(key=lambda e: e.get("t", ""), reverse=True)
    return {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "verdict": verdict, "reason": reason,
        "streaming": verdict == "danger",
        "overall": worst,
        "alerting": worst == "down" or fresh_bad,
        "skin": resolve_skin(),
        "machines": machines,
        "events": events[:6],
        "hist_ts": hist.get("ts"),
        # 快照年龄由服务端算（各机时钟可能漂移，客户端只做「收到后又过了多久」的加法）
        "age_s": (round(now - float(hist.get("ts"))) if hist.get("ts") else None),
    }


def snapshot_text(ctx: dict) -> str:
    lines = []
    v_h = {"safe": "safe(此刻可安全重启 Hub)", "wait": "wait(有长任务在跑,重启请等)",
           "danger": "danger(直播/推流中,禁止重启)"}.get(ctx["verdict"], ctx["verdict"])
    lines.append(f"- Hub 忙态 verdict={v_h}" + (f"；理由：{ctx['reason']}" if ctx.get("reason") else ""))
    for m in ctx["machines"]:
        vu, vt = m.get("vram_used_mb"), m.get("vram_total_mb")
        vram = f"显存 {vu / 1024:.1f}/{vt / 1024:.0f}G({vu / vt * 100:.0f}%)" if vu and vt and vt > 0 else "显存未知"
        lat = f"延迟 {m['lat_ms']}ms" if m.get("lat_ms") is not None else "延迟未知"
        lines.append(f"- {m['zh']}({m['id']},{m.get('role','')}): {m['status']} · {lat} · {vram}")
    if ctx["events"]:
        lines.append("最近事件：")
        for e in ctx["events"]:
            mark = "🔥在燃" if e["state"] == "firing" else "✅已恢复"
            lines.append(f"  - [{mark}] {e.get('t','')} {e['title']}")
    else:
        lines.append("最近 2 小时无告警事件。")
    return "\n".join(lines)


# ---------------- LLM ----------------

def _deepseek_key() -> str:
    """密钥链：环境变量 → 解析 secrets.bat（Hub 同源，密钥永不出中枢）。"""
    import os
    import re
    k = os.environ.get("CONV_DEEPSEEK_API_KEY", "").strip()
    if k:
        return k
    try:
        for ln in (AVATARHUB / "secrets.bat").read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"\s*set\s+CONV_DEEPSEEK_API_KEY=(.+)", ln, re.I)
            if m:
                return m.group(1).strip().strip('"')
    except Exception:
        pass
    return ""


def _backends() -> list[dict]:
    d = _read_json(LLM_BACKENDS, []) or []
    if isinstance(d, dict):
        d = d.get("backends", [])
    out = []
    ds_key = _deepseek_key()
    for b in d:
        if not isinstance(b, dict):
            continue
        base = str(b.get("base_url") or "")
        # 铁律：绝不用中枢本机 LLM——渲染主力机 95%+ 显存常态，冷载 14B=挤爆渲染链
        # （2026-08-06 02:45 实锤：回落链掉到 127.0.0.1:11434 钉了 9.5G，已卸载并立此禁令）
        if "127.0.0.1" in base or "localhost" in base:
            continue
        b2 = dict(b)
        if "api.deepseek.com" in base and not b2.get("api_key"):
            b2["api_key"] = ds_key  # 按域名注入，与 avatar_hub._conv_setup_llms 同机制
        out.append(b2)
    order = {"deepseek": 0, "deepseek-pro": 1, "qwen14b-173": 2}
    return sorted(out, key=lambda b: order.get(b.get("name", ""), 9))


def _chat_openai(base_url: str, key: str, model: str, messages: list, timeout: float = 40) -> str:
    body = json.dumps({"model": model, "messages": messages, "temperature": 0.4,
                       "max_tokens": 700}).encode("utf-8")
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    return d["choices"][0]["message"]["content"]


def _chat_ollama(base_url: str, model: str, messages: list, timeout: float = 60) -> str:
    body = json.dumps({"model": model, "messages": messages, "stream": False,
                       "options": {"temperature": 0.4}, "keep_alive": "10m"}).encode("utf-8")
    req = urllib.request.Request(base_url.rstrip("/") + "/api/chat", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    return (d.get("message") or {}).get("content", "")


def llm_reply(machine_id: str, messages: list) -> tuple[str, str]:
    fleet = {m["id"]: m for m in _machines()}
    m = fleet.get(machine_id) or {}
    machine_line = f"{m.get('zh', machine_id)}（{machine_id}，{m.get('role_now', '')}，IP {m.get('ip', '?')}）"
    ctx = build_context()
    sys_msg = SYSTEM_PROMPT.format(machine_line=machine_line, snap_ts=ctx["ts"],
                                   snapshot=snapshot_text(ctx))
    msgs = [{"role": "system", "content": sys_msg}] + messages[-8:]
    errs = []
    for b in _backends():
        name = b.get("name", "?")
        try:
            if "11434" in str(b.get("base_url", "")):
                txt = _chat_ollama(b["base_url"], b.get("model", ""), msgs)
            else:
                txt = _chat_openai(b.get("base_url", ""), b.get("api_key", ""), b.get("model", ""), msgs)
            if txt and txt.strip():
                return txt.strip(), name
        except Exception as e:  # noqa: BLE001
            errs.append(f"{name}:{type(e).__name__}")
            continue
    return f"（小界暂时联系不上大脑：{'; '.join(errs) or '无可用后端'}。集群快照仍可看桌面实况板。）", "none"


# ---------------- TTS ----------------
# [P0 抗停机 2026-08-14] 语音回执不许因算力模式失声：code 编程模式会停掉 fish_tts(:7855)，
# 而小界的嗓子全走 Fish——「切到编程模式」说完系统从此哑巴（确认/完成播报全没，实锤断点）。
# 三级降级：① Fish 在线合成（成功顺手落盘缓存）→ ② 磁盘缓存（同一句话说过一次=永远会说，
# 音色一致）→ ③ Windows SAPI 离线机械音（陌生句子也不失声）。缓存键=文本 sha1，LRU 留 200 条。

TTS_CACHE_DIR = Path(r"C:\Users\Public\boundless-hud\tts_cache")


def _tts_cache_path(text: str) -> Path:
    import hashlib
    return TTS_CACHE_DIR / (hashlib.sha1(text.encode("utf-8")).hexdigest()[:24] + ".wav")


def _tts_fish(text: str) -> bytes | None:
    try:
        body = json.dumps({"text": text[:280], "language": "zh", "return_base64": True}).encode("utf-8")
        req = urllib.request.Request(f"{FISH}/v1/tts", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=45) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        b64 = d.get("audio_base64") or ""
        return base64.b64decode(b64) if b64 else None
    except Exception:
        return None


def _tts_sapi(text: str) -> bytes | None:
    """SAPI 兜底（System.Speech 落临时 wav）：文本经 base64 隧道防引号剥层（O9 先例）。"""
    import subprocess
    import tempfile
    tmp = Path(tempfile.gettempdir()) / f"xj_sapi_{int(time.time() * 1000)}.wav"
    t64 = base64.b64encode(text.encode("utf-16-le")).decode("ascii")
    ps = ("Add-Type -AssemblyName System.Speech;"
          "$t=[Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('" + t64 + "'));"
          "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
          "$s.SetOutputToWaveFile('" + str(tmp) + "');$s.Speak($t);$s.Dispose()")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=20,
                       capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        wav = tmp.read_bytes() if tmp.exists() else None
        return wav if wav and len(wav) > 1000 else None
    except Exception:
        return None
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _tts_cache_prune(limit: int = 200) -> None:
    try:
        files = sorted(TTS_CACHE_DIR.glob("*.wav"), key=lambda p: p.stat().st_mtime)
        for p in files[:-limit]:
            p.unlink(missing_ok=True)
    except Exception:
        pass


def tts_wav(text: str) -> bytes | None:
    text = (text or "").strip()
    if not text:
        return None
    cp = _tts_cache_path(text[:280])
    wav = _tts_fish(text)
    if wav:
        try:
            TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cp.write_bytes(wav)
            _tts_cache_prune()
        except Exception:
            pass
        return wav
    try:
        if cp.exists():
            import os
            os.utime(cp)   # LRU touch：常用回执别被裁剪
            return cp.read_bytes()
    except Exception:
        pass
    return _tts_sapi(text[:120])


# ---------------- HTTP ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "XiaojieServer/1.0"

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # 安静点，别刷屏
        pass

    def _send_board(self, path: str):
        """六机实况板 PNG（P0 广播位）。id 白名单=台账机器；陈旧即 404——
        节点哨兵只认 200，其余一律回落静态壁纸（撒谎的仪表盘比没有仪表盘危险）。"""
        name = path[len("/board/"):]
        mid = name[:-4] if name.endswith(".png") else ""
        if not mid or mid not in {m.get("id") for m in _machines()}:
            self._send(404, b"{}")
            return
        p = BOARDS / f"{mid}.png"
        try:
            if time.time() - p.stat().st_mtime > BOARD_STALE_S:
                self._send(404, json.dumps({"error": "stale"}).encode())
                return
            body = p.read_bytes()
        except OSError:
            self._send(404, b"{}")
            return
        self._send(200, body, "image/png")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/robot"):
            try:
                html = (HERE / "robot.html").read_bytes()
                self._send(200, html, "text/html; charset=utf-8")
            except Exception:
                self._send(500, b"robot.html missing")
        elif u.path.startswith("/board/"):
            self._send_board(u.path)
        elif u.path == "/api/context":
            try:
                self._send(200, json.dumps(build_context(), ensure_ascii=False).encode("utf-8"))
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(e)}).encode())
        elif u.path == "/health":
            self._send(200, json.dumps({"ok": True, "service": "xiaojie", "port": PORT}).encode())
        else:
            self._send(404, b"{}")

    def do_POST(self):
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            # utf-8-sig：PowerShell Set-Content 的 UTF8 自带 BOM，普通 utf-8 会 json 解析失败
            payload = json.loads(self.rfile.read(n).decode("utf-8-sig", "replace")) if n else {}
        except Exception:
            payload = {}
        if u.path == "/api/chat":
            machine = str(payload.get("machine") or "zhongshu")
            messages = payload.get("messages") or []
            clean = [{"role": str(m.get("role", "user")), "content": str(m.get("content", ""))[:2000]}
                     for m in messages if isinstance(m, dict)]
            reply, backend = llm_reply(machine, clean)
            self._send(200, json.dumps({"reply": reply, "backend": backend}, ensure_ascii=False).encode("utf-8"))
        elif u.path == "/api/tts":
            wav = tts_wav(str(payload.get("text") or ""))
            if wav:
                self._send(200, wav, "audio/wav")
            else:
                self._send(503, json.dumps({"error": "tts unavailable"}).encode())
        else:
            self._send(404, b"{}")


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"xiaojie server on :{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
