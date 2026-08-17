# -*- coding: utf-8 -*-
"""坐席机 ChatX 工作日志监控（198 智拓 / 104 幻颜，可扩展）。

做什么
------
1. SSH 拉取每台坐席机的 ChatX 后端工作日志
   ``%APPDATA%\\telegram-ai-desktop\\logs\\backend.log``（远端 base64 回传，
   零 GBK 乱码），只取 ERROR/WARNING/Traceback 行 + app 版本 + 文件 mtime。
2. 逐行分类：**良性噪声**（已知、有兜底、不用管）vs **真问题**（会影响收发/崩溃/
   会话失效）。良性清单与真问题规则见 ``BENIGN`` / ``REAL``。
3. 去重：状态文件按「问题指纹」记已报过的项（默认 24h 窗），只对**新出现**的真问题告警。
4. 投递：把「本轮新真问题 + 健康摘要」发到 **@Sousaun 的 Telegram**——经本机智聊实例
   ``POST /api/unified-inbox/send``，account_id=8244899900（@Sousaun 本人），
   chat_key='me'（发到该账号自己的「收藏消息」，无需对方 /start、不打扰任何陌生人）。
5. 落档：每轮结果追加 ``SEAT_LOG_MONITOR_LOG.md`` 台账。

为什么这样投递
--------------
@Sousaun = 我们自管的 Telegram 账号 8244899900（人设 su_wan / Katie），在本机智聊实例
在线。通知 bot ``@tgzkw_bot`` 无法主动私聊未 /start 过它的用户，故不走 bot；改用实例把
消息投进 @Sousaun 自己的收藏消息——运营登录 @Sousaun 即可见，零副作用。

用法
----
    python monitor_seat_logs.py                # 扫描 + 有新真问题才发 + 落档
    python monitor_seat_logs.py --baseline     # 强制发一份健康基线（首次上线用）
    python monitor_seat_logs.py --dry-run      # 只扫描打印，不发不改状态
    python monitor_seat_logs.py --include-benign  # 报告里也列良性噪声（排查用）
    python monitor_seat_logs.py --since-min 180   # 只看最近 N 分钟的日志行（默认全量按状态增量）

退出码：0 正常（含无问题）；1 投递失败 / 实例不可达。
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── 坐席机清单（中文名: SSH 别名）──────────────────────────────────────────────
# 2026-08-13 起补 173/140：fresh_guard 死锁事故证实四台同病，只盯两台=另两台盲飞。
SEATS: Dict[str, str] = {
    "198 智拓(kouxing)": "kouxing",
    "104 幻颜(lianbei)": "lianbei",
    "173 云升(yunsheng)": "yunsheng",
    "140 听写(tingxie)": "tingxie",
}
LOG_REL = r"AppData\Roaming\telegram-ai-desktop\logs\backend.log"

# ── @Sousaun 投递参数 ──────────────────────────────────────────────────────────
# 主通道：@tgzkw_bot 私聊 @Sousaun（chat_id=8244899900）。bot token 从实例的
# notify_webhooks.json 读（不硬编密钥）；@Sousaun 已对该 bot /start 过，可直达。
# 备用通道：本机智聊实例把消息投进 @Sousaun 自己的「收藏消息」（chat_key='me'）。
INSTANCE_BASE = "http://127.0.0.1:18799"
INSTANCE_DATA_ROOT = r"D:\chengjie-instances\zhiliao\data"
SOUSAUN_TG_ID = "8244899900"     # @Sousaun 的 Telegram user id（bot DM 目标）
SOUSAUN_PLATFORM = "telegram"
SOUSAUN_ACCOUNT = "8244899900"   # 备用：@Sousaun 本人账号（su_wan / Katie）
SOUSAUN_CHATKEY = "me"           # 备用：发到该账号自己的收藏消息
NOTIFY_WEBHOOKS = Path(INSTANCE_DATA_ROOT) / "config" / "notify_webhooks.json"

STATE_PATH = Path(__file__).with_name("monitor_seat_logs.state.json")
LEDGER_PATH = Path(__file__).with_name("SEAT_LOG_MONITOR_LOG.md")

# ── 良性噪声：命中即忽略（附「为什么无害」）────────────────────────────────────
BENIGN: List[Tuple[str, str]] = [
    (r"请配置有效的Telegram api_id|Telegram配置缺少必需键",
     "桌面版全局 TG 凭据故意留空（账号走 credpool 逐账号提供）——伪 ERROR，源码已降级"),
    (r"register_spk 失败.*(401|Unauthorized)|register_spk 需鉴权令牌",
     "AvatarHub 语音预热无服务令牌（坐席机本就没有），合成回落正常——源码已降级"),
    (r"AI 调用失败\(attempt=1\): Connection error",
     "DeepSeek 云偶发抖动，自动重试 / 本地兜底，非故障"),
    (r"记忆抽取接地护栏丢弃",
     "记忆接地护栏正常工作（防把 AI 臆测存成用户事实），是特性不是问题"),
    (r"Failed to tokenize prompt",
     "140 视觉端点偶发 tokenize 失败，自动切下一端点，有兜底"),
    (r"冷启动隔离本轮抑制候选|cold_start_warming",
     "主动触达冷启动隔离（新会话先观察），正常抑制"),
    (r"服务 7852 等待就绪超时|预热跳过.*7852 未就绪|7852 未就绪",
     "AvatarHub 7852 冷启动懒加载未就绪，首句回落，非故障"),
    (r"quota_exhausted",
     "额度档位（首启体验档）耗尽提示，非故障"),
    (r"知识库自愈完成|每日自动学习完成|上下文或情绪增强功能未启|四层触发功能未启|人工转接（重复问句）模块已加",
     "启动期常规 INFO/状态行"),
]

# ── 真问题：命中即上报（正则, 级别 critical|warn, 人话说明）──────────────────────
REAL: List[Tuple[str, str, str]] = [
    # 2026-08-13 实锤：198 主号 8/11 被「终止所有会话」踢下线（SESSION_REVOKED），
    # 旧规则只认 AUTH_KEY_UNREGISTERED → 该形态两天只按 warn「启动失败」报，
    # 坐席对着「同步聊天记录失败」连点无人知道该去重新登录。会话已死类 401 全归 critical。
    # （刻意不含 USER_DEACTIVATED：会与对方账号注销的 INPUT_USER_DEACTIVATED 撞子串误报。）
    (r"AUTH_KEY_UNREGISTERED|key is not registered|SESSION_REVOKED"
     r"|SESSION_EXPIRED|AUTH_KEY_INVALID|AUTH_KEY_DUPLICATED",
     "critical", "Telegram 账号会话失效/被吊销，需重新登录（否则该号收发全断）"),
    (r"Traceback|Exception in ASGI|not iterable|500 Internal Server",
     "critical", "后端异常 / 页面 500 崩溃"),
    (r"启动Telegram客户端失败",
     "warn", "Telegram 客户端启动失败（会话/网络）"),
    (r"发送消息失败|发送.*失败.*Client has not been started",
     "warn", "消息发送失败（客户端未就绪）"),
    (r"platform_session|session-status|意外断线|假在线|重新登录",
     "warn", "平台会话健康告警（WA/Messenger/LINE 掉线）"),
    (r"qr_login 失败|登录.*失败",
     "warn", "渠道登录失败（扫码/协议登录）"),
    # 2026-08-13 fresh_guard 死锁教训：投递链被守卫/闸门静默吃掉时**零 ERROR**（拦截
    # 是 INFO 级），全自动哑火 6 天无人知。「入站漏球」是结果面兜底——不管哪个环节
    # 吞了回复，客户消息超 2h 无回复且无待审稿必触发此 WARNING，从此 15min 内上报。
    (r"入站漏球",
     "warn", "有客户消息长时间无回复且无待审稿（自动回复链可能被静默拦断）"),
]

_ERR_LINE = re.compile(r"\[(ERROR|WARNING|CRITICAL)\]|Traceback|Exception in ASGI|not iterable")
_TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")


def _remote_ps(alias: str, script: str, timeout: int = 40) -> str:
    """在远端跑 PowerShell（EncodedCommand，防引号/GBK 坑），返回 stdout。"""
    b64 = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        out = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=8", alias,
             f"powershell -NoProfile -EncodedCommand {b64}"],
            capture_output=True, timeout=timeout)
        return out.stdout.decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return f"__SSH_ERR__ {exc}"


def pull_seat(alias: str) -> Dict[str, Any]:
    """拉一台坐席机：app 版本 + 日志 mtime + error/warn 行（base64 回传保编码）。"""
    ps = r'''
$ErrorActionPreference = "SilentlyContinue"
$log = Join-Path $env:APPDATA "telegram-ai-desktop\logs\backend.log"
$ping = ""
try { $ping = (Invoke-WebRequest -Uri "http://127.0.0.1:18799/api/desktop/ping" -UseBasicParsing -TimeoutSec 4).Content } catch { $ping = "" }
Write-Output ("PING=" + $ping)
if (Test-Path $log) {
  $mt = (Get-Item $log).LastWriteTime.ToString("s")
  Write-Output ("MTIME=" + $mt)
  $lines = Get-Content $log -Encoding UTF8
  $sel = $lines | Select-String -Pattern "\[(ERROR|WARNING|CRITICAL)\]|Traceback|Exception in ASGI|not iterable" -CaseSensitive:$false
  $blob = ($sel | ForEach-Object { $_.Line }) -join "`n"
  $b = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($blob))
  Write-Output ("B64=" + $b)
} else {
  Write-Output "MTIME="
  Write-Output "B64="
}
'''
    raw = _remote_ps(alias, ps)
    res: Dict[str, Any] = {"alias": alias, "ok": True, "version": "", "mtime": "", "lines": []}
    if raw.startswith("__SSH_ERR__"):
        res["ok"] = False
        res["error"] = raw
        return res
    for ln in raw.splitlines():
        if ln.startswith("PING="):
            m = re.search(r'"app"\s*:\s*"([^"]+)".*?"version"\s*:\s*"([^"]+)"', ln)
            if m:
                res["version"] = f"{m.group(1)} {m.group(2)}"
        elif ln.startswith("MTIME="):
            res["mtime"] = ln[6:].strip()
        elif ln.startswith("B64="):
            b = ln[4:].strip()
            if b:
                try:
                    txt = base64.b64decode(b).decode("utf-8", "replace")
                    res["lines"] = [x for x in txt.split("\n") if x.strip()]
                except Exception:
                    res["lines"] = []
    return res


def line_ts(line: str) -> Optional[str]:
    m = _TS.match(line)
    return m.group(1) if m else None


def classify(line: str) -> Optional[Tuple[str, str, str]]:
    """返回 (kind, level, why)；benign→('benign','info',why)；real→('real',level,why)；
    未命中任何真问题规则、且不是良性、但是 ERROR → 归 ('real','warn','未分类 ERROR')。"""
    for pat, why in BENIGN:
        if re.search(pat, line):
            return ("benign", "info", why)
    for pat, level, why in REAL:
        if re.search(pat, line):
            return ("real", level, why)
    if "[ERROR]" in line or "CRITICAL" in line or "Traceback" in line:
        return ("real", "warn", "未分类 ERROR（需人工看）")
    return None  # 未分类 WARNING：不吵，忽略


def signature(alias: str, why: str, line: str) -> str:
    """问题指纹：同机同类只报一次（去掉时间戳与易变数字，抓稳定形态）。"""
    body = _TS.sub("", line)
    body = re.sub(r"\d+", "#", body)[:120]
    return f"{alias}|{why}|{body}"


def scan(dry_run: bool, since_min: int, include_benign: bool) -> Dict[str, Any]:
    state = _load_state()
    since_ts = None
    if since_min > 0:
        since_ts = (datetime.now() - timedelta(minutes=since_min)).strftime("%Y-%m-%d %H:%M:%S")

    report: Dict[str, Any] = {"seats": [], "new_real": [], "all_real": [], "benign_counts": {}}
    now_alerted = dict(state.get("alerted", {}))
    cutoff = time.time() - 24 * 3600  # 指纹 24h 过期
    now_alerted = {k: v for k, v in now_alerted.items() if v > cutoff}

    for name, alias in SEATS.items():
        seat = pull_seat(alias)
        seat["name"] = name
        last_seen = state.get("last_seen", {}).get(alias, "")
        floor = max([t for t in (last_seen, since_ts) if t], default="")

        seat_real: List[Dict[str, str]] = []
        seat_benign = 0
        max_ts = last_seen
        for line in seat.get("lines", []):
            ts = line_ts(line)
            if ts and floor and ts <= floor:
                continue
            if ts and (not max_ts or ts > max_ts):
                max_ts = ts
            c = classify(line)
            if not c:
                continue
            kind, level, why = c
            if kind == "benign":
                seat_benign += 1
                report["benign_counts"][why] = report["benign_counts"].get(why, 0) + 1
                continue
            sig = signature(alias, why, line)
            item = {"seat": name, "level": level, "why": why,
                    "ts": ts or "", "line": line.strip()[:300], "sig": sig}
            report["all_real"].append(item)
            seat_real.append(item)
            if sig not in now_alerted:
                report["new_real"].append(item)
                now_alerted[sig] = time.time()
        seat["real_count"] = len(seat_real)
        seat["benign_count"] = seat_benign
        seat["max_ts"] = max_ts
        report["seats"].append(seat)
        if not dry_run:
            state.setdefault("last_seen", {})[alias] = max_ts

    if not dry_run:
        state["alerted"] = now_alerted
        state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _save_state(state)
    report["include_benign"] = include_benign
    return report


def render_report(report: Dict[str, Any], baseline: bool) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"[坐席日志监控] {now}"]
    for seat in report["seats"]:
        if not seat.get("ok"):
            lines.append(f"❌ {seat['name']}：SSH 不可达（{str(seat.get('error',''))[:60]}）")
            continue
        ver = seat.get("version") or "?"
        rc = seat.get("real_count", 0)
        bc = seat.get("benign_count", 0)
        flag = "🟢" if rc == 0 else ("🔴" if any(
            i["level"] == "critical" and i["seat"] == seat["name"] for i in report["all_real"]) else "🟡")
        lines.append(f"{flag} {seat['name']}  {ver}  新真问题 {rc} · 良性噪声 {bc}")

    new = report["new_real"]
    if new:
        lines.append("")
        lines.append(f"⚠️ 本轮新增真问题 {len(new)} 条：")
        for it in new[:15]:
            emoji = "🔴" if it["level"] == "critical" else "🟡"
            lines.append(f"{emoji} [{it['seat']}] {it['why']}")
            lines.append(f"    {it['ts']}  {it['line'][:160]}")
    elif baseline:
        lines.append("")
        lines.append("✅ 当前无新真问题（监控已上线，两台坐席健康）。")

    if report.get("include_benign") and report.get("benign_counts"):
        lines.append("")
        lines.append("（良性噪声统计，无需处理）")
        for why, n in sorted(report["benign_counts"].items(), key=lambda x: -x[1]):
            lines.append(f"  ·{n}× {why}")
    return "\n".join(lines)


# ── 投递到 @Sousaun（本机智聊实例）──────────────────────────────────────────────
class Instance:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.token = token
        import http.cookiejar
        self._cj = http.cookiejar.CookieJar()
        self._op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._cj))

    def _raw(self, req, timeout=30):
        try:
            with self._op.open(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            return 0, json.dumps({"error": str(e)[:200]})

    def login(self) -> bool:
        data = urllib.parse.urlencode({"auth_token": self.token}).encode()
        req = urllib.request.Request(
            self.base + "/login", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        code, _ = self._raw(req)
        return code in (200, 303) and any(c.name == "session" for c in self._cj)

    def send(self, text: str) -> Tuple[int, Any]:
        body = {"platform": SOUSAUN_PLATFORM, "account_id": SOUSAUN_ACCOUNT,
                "chat_key": SOUSAUN_CHATKEY, "text": text, "skip_translate": True,
                "client_msg_id": f"seatmon-{uuid.uuid4().hex[:12]}"}
        req = urllib.request.Request(
            self.base + "/api/unified-inbox/send",
            data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"})
        code, raw = self._raw(req, 60)
        try:
            return code, json.loads(raw)
        except Exception:
            return code, {"raw": raw[:300]}


def _read_token(data_root: str) -> str:
    try:
        import yaml
    except Exception:
        return ""
    for name in ("config.local.yaml", "config.yaml"):
        fp = Path(data_root) / "config" / name
        if not fp.exists():
            continue
        try:
            cfg = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        tok = str((cfg.get("web_admin") or {}).get("auth_token") or "")
        if tok:
            return tok
    return ""


def _bot_token() -> str:
    """从 notify_webhooks.json 取第一个 telegram 渠道的 bot token（不硬编密钥）。"""
    try:
        arr = json.loads(NOTIFY_WEBHOOKS.read_text(encoding="utf-8"))
        for ch in arr if isinstance(arr, list) else []:
            if str(ch.get("format")) == "telegram" and ch.get("token"):
                return str(ch["token"])
    except Exception:
        pass
    return ""


def deliver_bot(text: str) -> Tuple[bool, str]:
    """主通道：@tgzkw_bot 私聊 @Sousaun（chat_id=SOUSAUN_TG_ID）。"""
    tok = _bot_token()
    if not tok:
        return False, "notify_webhooks.json 无 bot token"
    api = f"https://api.telegram.org/bot{tok}/sendMessage"
    chunks = _chunk(text, 3800)
    for i, ch in enumerate(chunks):
        payload = {"chat_id": SOUSAUN_TG_ID,
                   "text": ch if len(chunks) == 1 else f"({i+1}/{len(chunks)})\n{ch}"}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            api, data=data, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                resp = json.loads(r.read().decode("utf-8", "replace"))
            if not resp.get("ok"):
                return False, f"bot resp={str(resp)[:160]}"
        except urllib.error.HTTPError as e:
            return False, f"bot HTTP {e.code}: {e.read().decode('utf-8','replace')[:160]}"
        except Exception as e:  # noqa: BLE001
            return False, f"bot err={str(e)[:160]}"
        time.sleep(0.5)
    return True, "bot ok"


def deliver_instance(text: str) -> Tuple[bool, str]:
    """备用通道：本机实例把消息投进 @Sousaun 收藏消息。"""
    tok = _read_token(INSTANCE_DATA_ROOT)
    if not tok:
        return False, "未读到实例 web_admin.auth_token"
    inst = Instance(INSTANCE_BASE, tok)
    if not inst.login():
        return False, "实例登录失败（不可达 / token 变更）"
    chunks = _chunk(text, 3500)
    for i, ch in enumerate(chunks):
        code, resp = inst.send(ch if len(chunks) == 1 else f"({i+1}/{len(chunks)})\n{ch}")
        ok = code == 200 and isinstance(resp, dict) and resp.get("ok") is not False
        if not ok:
            return False, f"send code={code} resp={str(resp)[:160]}"
        time.sleep(0.6)
    return True, "instance ok"


def deliver(text: str) -> Tuple[bool, str]:
    """主 bot 私聊，失败回落实例收藏消息。"""
    ok, msg = deliver_bot(text)
    if ok:
        return True, msg
    ok2, msg2 = deliver_instance(text)
    if ok2:
        return True, f"{msg2}（bot 失败回落：{msg}）"
    return False, f"bot 失败：{msg}；实例失败：{msg2}"


def _chunk(s: str, n: int) -> List[str]:
    out, cur = [], []
    size = 0
    for line in s.split("\n"):
        if size + len(line) + 1 > n and cur:
            out.append("\n".join(cur)); cur = []; size = 0
        cur.append(line); size += len(line) + 1
    if cur:
        out.append("\n".join(cur))
    return out or [s]


def _append_ledger(report: Dict[str, Any], sent: Optional[str]) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    seats_line = "; ".join(
        f"{s['name']} {s.get('version') or '?'} 真{s.get('real_count',0)}/良{s.get('benign_count',0)}"
        + ("" if s.get("ok") else " [SSH不可达]")
        for s in report["seats"])
    if not LEDGER_PATH.exists():
        LEDGER_PATH.write_text(
            "# 坐席机工作日志监控台账\n\n"
            "> 监控对象：198 智拓 / 104 幻颜 的 ChatX 后端 `backend.log`\n"
            "> 工具：`monitor_seat_logs.py`（本机每 15 分钟巡检，真问题投递 @Sousaun）\n",
            encoding="utf-8")
    parts = [f"\n### {now}", f"- 巡检：{seats_line}"]
    if report["new_real"]:
        parts.append(f"- 新真问题 {len(report['new_real'])}：")
        for it in report["new_real"][:20]:
            parts.append(f"  - [{it['seat']}][{it['level']}] {it['why']} — `{it['line'][:180]}`")
    else:
        parts.append("- 新真问题：0（健康）")
    if sent is not None:
        parts.append(f"- 投递 @Sousaun：{sent}")
    with LEDGER_PATH.open("a", encoding="utf-8") as f:
        f.write("\n".join(parts) + "\n")


def _load_state() -> Dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只扫描打印，不发不改状态")
    ap.add_argument("--baseline", action="store_true", help="强制发一份健康基线（首次上线用）")
    ap.add_argument("--include-benign", action="store_true", help="报告含良性噪声统计")
    ap.add_argument("--since-min", type=int, default=0, help="只看最近 N 分钟（默认按状态增量）")
    args = ap.parse_args()

    report = scan(args.dry_run, args.since_min, args.include_benign)
    text = render_report(report, args.baseline)
    print(text)

    if args.dry_run:
        print("\n[dry-run] 未投递、未落档、未改状态。")
        return 0

    sent_status: Optional[str] = None
    should_send = bool(report["new_real"]) or args.baseline
    rc = 0
    if should_send:
        ok, msg = deliver(text)
        sent_status = "成功" if ok else f"失败：{msg}"
        print(f"\n[deliver] @Sousaun：{sent_status}")
        if not ok:
            rc = 1
    else:
        print("\n[deliver] 无新真问题，未打扰 @Sousaun（--baseline 可强制发健康摘要）。")
    _append_ledger(report, sent_status)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
