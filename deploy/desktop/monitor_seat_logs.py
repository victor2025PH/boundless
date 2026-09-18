# -*- coding: utf-8 -*-
"""坐席机 ChatX 工作日志监控（名单来自 deploy/machines.json 的 ``chatx_seat`` 机器）。

做什么
------
0. 坐席名单与中文名从台账 ``deploy/machines.json`` 推导（``chatx_seat: true`` 的机器，
   显示名 = ``IP尾段 zh(主别名)``，如 ``198 视觉机(shijue)``）。改编制只改台账。
1. SSH 拉取每台坐席机的 ChatX 后端工作日志
   ``%APPDATA%\\telegram-ai-desktop\\logs\\backend.log``（远端 base64 回传，
   零 GBK 乱码），只取 ERROR/WARNING/Traceback 行 + app 版本 + 文件 mtime。
2. 逐行分类：**良性噪声**（已知、有兜底、不用管）vs **真问题**（会影响收发/崩溃/
   会话失效）。良性清单与真问题规则见 ``BENIGN`` / ``REAL``。
3. 去重：状态文件按「问题指纹」记已报过的项（默认 24h 窗），只对**新出现**的真问题告警。
   无时间戳的 Traceback 续行继承上一行 ts（否则绕过增量 floor，每天幽灵重报）；
   ping 不通或日志 mtime 过旧时改报「坐席离线」，不再逐行当崩溃。
4. 投递：把「本轮新真问题 + 健康摘要」经 ``@tgzkw_bot`` 发到**运维群**（tg-ywqz）；
   bot 不可用时回落本机智聊实例 ``POST /api/unified-inbox/send``，account_id=8244899900
   （@Sousaun 本人）chat_key='me'（发到该账号自己的「收藏消息」）。
5. 落档：每轮结果追加 ``SEAT_LOG_MONITOR_LOG.md`` 台账。

为什么这样投递
--------------
2026-09-10 前主通道是 bot 私聊 @Sousaun（8244899900）。@Sousaun 是智聊**生产在线账号**
（人设 su_wan / Katie），bot 私聊它＝生产收件箱里出现一个 bot 对端 → peer_bot_guard
弹 bot_peer_alert 进运维群——监控每发一次报告就自造一条告警。改投运维群后生产链零感知，
且运维群本就是这类信息该去的地方。

用法
----
    python monitor_seat_logs.py                # 扫描 + 有新真问题才发 + 落档
    python monitor_seat_logs.py --baseline     # 强制发一份健康基线（首次上线用）
    python monitor_seat_logs.py --dry-run      # 只扫描打印，不发不改状态
    python monitor_seat_logs.py --include-benign  # 报告里也列良性噪声（排查用）
    python monitor_seat_logs.py --since-min 180   # 只看最近 N 分钟的日志行（默认全量按状态增量）
    python monitor_seat_logs.py --baseline --note "…"  # 基线 + 一行说明（编制变更时告知群里为什么名单变了）

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

# ── 坐席机清单：来自台账 deploy/machines.json（chatx_seat: true）────────────────
# 2026-08-13 起曾在此硬编码四台（198 智拓 / 104 幻颜 / 173 云升 / 140 听写）。08-29
# 全员改编（140 听写→记忆机，桌面版停用）后这份名单没跟着走 → 群里整点连报
# 「140 听写 ping 不通」假离线 68 条。2026-09-18 改为读台账：名单、中文名、SSH 别名
# 单一来源，改编制只改 machines.json 的 chatx_seat / zh。
MACHINES_JSON = Path(__file__).parent.parent / "machines.json"
LOG_REL = r"AppData\Roaming\telegram-ai-desktop\logs\backend.log"


def load_seats(path: Path = MACHINES_JSON) -> List[Dict[str, str]]:
    """从台账取坐席机：``[{id, name, alias, ip}]``，台账顺序。

    - ``id``：台账 id，作状态文件键（增量水位 / 指纹）——与 08-13 起的旧别名键
      （kouxing / lianbei / yunsheng）一致，改读台账不丢水位、不重报旧问题。
    - ``alias``：SSH 连接用台账主别名 ``ssh[0]``（无则退回 id）。
    - ``name``：``IP尾段 zh(alias)``，群里看到的名字，跟台账 zh 走。
    读不到 / 解析失败 / 没有 chatx_seat 机器 → 抛 RuntimeError，由 scan() 上报
    （静默变成「0 台受监控」比报错更糟）。
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"读台账失败 {path}: {str(exc)[:120]}") from exc
    seats: List[Dict[str, str]] = []
    for m in (data.get("machines") or []) if isinstance(data, dict) else []:
        if not isinstance(m, dict) or not m.get("chatx_seat"):
            continue
        mid = str(m.get("id") or "").strip()
        if not mid:
            continue
        ssh_aliases = [str(a) for a in (m.get("ssh") or []) if str(a).strip()]
        alias = ssh_aliases[0] if ssh_aliases else mid
        ip = str(m.get("ip") or "")
        tail = ip.rsplit(".", 1)[-1] if ip else "?"
        zh = str(m.get("zh") or mid)
        seats.append({"id": mid, "name": f"{tail} {zh}({alias})", "alias": alias, "ip": ip})
    if not seats:
        raise RuntimeError(f"台账 {path} 里没有 chatx_seat=true 的机器")
    return seats

# ── 投递参数 ──────────────────────────────────────────────────────────────────
# 主通道：@tgzkw_bot 投**运维群**（notify_webhooks.json 里名为 OPS_GROUP_CHANNEL 的
# 渠道的 target）。bot token 同样从该文件读（不硬编密钥）。
# 2026-09-10 改：此前主通道是 bot 私聊 @Sousaun（8244899900）——那是智聊**生产在线
# 账号**，bot 消息进它的收件箱会被 peer_bot_guard 判「对端是 bot」→ 运维群弹
# bot_peer_alert，等于监控自己制造告警。改投运维群后生产账号零感知。
# 备用通道：本机智聊实例把消息投进 @Sousaun 自己的「收藏消息」（chat_key='me'，
# 自己发给自己不是对端 bot，不触发 peer_bot_guard）。
INSTANCE_BASE = "http://127.0.0.1:18799"
INSTANCE_DATA_ROOT = r"D:\chengjie-instances\zhiliao\data"
OPS_GROUP_CHANNEL = "tg-ywqz"    # 运维群在 notify_webhooks.json 里的渠道名
SOUSAUN_TG_ID = "8244899900"     # @Sousaun 的 Telegram user id（仅作历史参考，不再 DM）
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
    # 2026-08-31 #101 教训：LINE 语音/图下载失败此前是 debug 级＝任何监控都看不见，
    # 客户机上「无音频存档」两天无人能归因。line_media 已升 WARNING，这里接住：
    # download_error/empty_body/write_error 是链路坏（token/网络/磁盘），too_large
    # 是护栏正常工作不报。出站时长探不到（对方端将显示 0:00）同报。
    (r"\[line_media\] 入站媒体未取到.*reason=(download_error|empty_body|write_error)",
     "warn", "LINE 入站媒体拉取失败（客户发的图/语音坐席看不到）——查 token/网络；会话里可点「拉取原件」补救"),
    (r"\[line_media\] 出站媒体时长未探到",
     "warn", "LINE 出站语音/视频缺时长（对方端显示 0:00）——查随包 ffmpeg/ffprobe 是否完好"),
]

_ERR_LINE = re.compile(r"\[(ERROR|WARNING|CRITICAL)\]|Traceback|Exception in ASGI|not iterable")
_TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")

# 坐席 ping 不通 / 日志长期不写 → 报「离线」而不是把旧 Traceback 当新崩溃
# （2026-09-16：198/104/173 自 09-13 起 ping 不通，每天 10:07 重报幽灵 Traceback）。
OFFLINE_AFTER_HOURS = 6.0


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


def assign_line_timestamps(lines: List[str]) -> List[Tuple[Optional[str], str]]:
    """给每行一个有效时间戳：自身有则用，没有则继承上一条有戳行的 ts。

    Traceback 续行 / ``Traceback (most recent call last):`` 本身通常无 ``[ts]``，
    旧逻辑 ``ts is None`` 直接绕过 floor → 同一段堆栈每 15 分钟当新问题。
    """
    last: Optional[str] = None
    out: List[Tuple[Optional[str], str]] = []
    for line in lines:
        ts = line_ts(line)
        if ts:
            last = ts
        out.append((ts or last, line))
    return out


def parse_mtime(mtime: str) -> Optional[datetime]:
    """解析远端 ``Get-Item.LastWriteTime.ToString("s")``（``2026-09-13T14:52:00``）。"""
    s = str(mtime or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def seat_offline_reason(
    seat: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
    offline_after_h: float = OFFLINE_AFTER_HOURS,
) -> Optional[str]:
    """SSH 通但后端未跑：返回人话原因；在线 / SSH 失败 → None。

    判据：``ping`` 解析不出 version（``/api/desktop/ping`` 不通），或日志 mtime
    超过 ``offline_after_h`` 小时未更新。两者满足其一即可——ping 空是「进程没了」，
    mtime 陈旧是「进程假活但不写日志」。
    """
    if not seat.get("ok"):
        return None
    n = now or datetime.now()
    ver = str(seat.get("version") or "").strip()
    mt = parse_mtime(str(seat.get("mtime") or ""))
    stale = False
    age_h = 0.0
    if mt is not None:
        age_h = max(0.0, (n - mt).total_seconds() / 3600.0)
        stale = age_h >= float(offline_after_h)
    # 措辞点明「ping」是桌面后端的 /api/desktop/ping 活性探针，不是网络 ICMP——
    # 2026-09-18 群里「140 ping 不通」被读成机器掉线，实为该机已改编算力节点、桌面版未跑。
    if not ver and stale:
        return (f"坐席桌面后端离线/未运行（/api/desktop/ping 无响应，日志已 {age_h:.0f}h 未更新）")
    if not ver:
        return "坐席桌面后端离线/未运行（/api/desktop/ping 无响应）"
    if stale:
        return (f"坐席桌面后端疑似停摆（ping 有响应，但日志已 {age_h:.0f}h 未更新）")
    return None


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
    """问题指纹：同机同类只报一次（去掉时间戳与易变数字，抓稳定形态）。

    ``why`` 同样去数字：离线原因里带「日志已 604h 未更新」这种每小时 +1 的计数，
    原先只对 ``line`` 去数字 → 指纹每小时变一次 → 同一台离线坐席被当新问题整点重报
    （2026-09-16~18 对 140 连报 68 条）。
    """
    body = _TS.sub("", line)
    body = re.sub(r"\d+", "#", body)[:120]
    why_n = re.sub(r"\d+", "#", why)
    return f"{alias}|{why_n}|{body}"


def scan(dry_run: bool, since_min: int, include_benign: bool) -> Dict[str, Any]:
    state = _load_state()
    since_ts = None
    if since_min > 0:
        since_ts = (datetime.now() - timedelta(minutes=since_min)).strftime("%Y-%m-%d %H:%M:%S")

    report: Dict[str, Any] = {"seats": [], "new_real": [], "all_real": [], "benign_counts": {}}
    now_alerted = dict(state.get("alerted", {}))
    cutoff = time.time() - 24 * 3600  # 指纹 24h 过期
    now_alerted = {k: v for k, v in now_alerted.items() if v > cutoff}
    now_dt = datetime.now()

    try:
        seats = load_seats()
    except RuntimeError as exc:
        # 台账坏了不是「没问题」：当一条 critical 上报（同指纹 24h 一次），本轮 0 台巡检。
        # 不动 last_seen / 指纹——台账恢复后水位还在，不会把旧日志全当新问题重报。
        seats = []
        why = "坐席台账不可用（本轮 0 台受监控）"
        sig = signature("ledger", why, str(exc))
        item = {"seat": "台账", "level": "critical", "why": why,
                "ts": "", "line": str(exc)[:300], "sig": sig}
        report["all_real"].append(item)
        if sig not in now_alerted:
            report["new_real"].append(item)
            now_alerted[sig] = time.time()
    else:
        # 台账里已不是坐席的机器（如 140 听写→记忆机）：清掉其水位与指纹，不留幽灵
        keep = {s["id"] for s in seats} | {"ledger"}
        state["last_seen"] = {k: v for k, v in (state.get("last_seen") or {}).items() if k in keep}
        now_alerted = {k: v for k, v in now_alerted.items() if k.split("|", 1)[0] in keep}

    for s in seats:
        sid, name, alias = s["id"], s["name"], s["alias"]
        seat = pull_seat(alias)
        seat["name"] = name
        seat["id"] = sid
        last_seen = state.get("last_seen", {}).get(sid, "")
        floor = max([t for t in (last_seen, since_ts) if t], default="")

        seat_real: List[Dict[str, str]] = []
        seat_benign = 0
        max_ts = last_seen

        offline_why = seat_offline_reason(seat, now=now_dt)
        seat["offline"] = bool(offline_why)
        if offline_why:
            # 离线本身是真问题；旧日志里的 Traceback 不再当「新崩溃」刷屏
            sig = signature(sid, offline_why, f"offline|{seat.get('mtime') or ''}|{seat.get('version') or ''}")
            item = {"seat": name, "level": "warn", "why": offline_why,
                    "ts": seat.get("mtime") or "", "line": offline_why, "sig": sig}
            report["all_real"].append(item)
            seat_real.append(item)
            if sig not in now_alerted:
                report["new_real"].append(item)
                now_alerted[sig] = time.time()
        else:
            for eff_ts, line in assign_line_timestamps(seat.get("lines", [])):
                if eff_ts and floor and eff_ts <= floor:
                    continue
                if eff_ts and (not max_ts or eff_ts > max_ts):
                    max_ts = eff_ts
                c = classify(line)
                if not c:
                    continue
                kind, level, why = c
                if kind == "benign":
                    seat_benign += 1
                    report["benign_counts"][why] = report["benign_counts"].get(why, 0) + 1
                    continue
                sig = signature(sid, why, line)
                item = {"seat": name, "level": level, "why": why,
                        "ts": eff_ts or "", "line": line.strip()[:300], "sig": sig}
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
            state.setdefault("last_seen", {})[sid] = max_ts

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
        if seat.get("offline"):
            flag = "⚫"
        else:
            flag = "🟢" if rc == 0 else ("🔴" if any(
                i["level"] == "critical" and i["seat"] == seat["name"] for i in report["all_real"]) else "🟡")
        lines.append(f"{flag} {seat['name']}  {ver}  新真问题 {rc} · 良性噪声 {bc}")

    if not report["seats"]:
        lines.append("❌ 本轮 0 台坐席受监控（台账 deploy/machines.json 不可用，见下）")

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
        lines.append(f"✅ 当前无新真问题（监控已上线，{len(report['seats'])} 台坐席健康；"
                     "名单来自 deploy/machines.json chatx_seat）。")

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


def _ops_group_chat_id() -> str:
    """运维群 chat_id：取 notify_webhooks.json 中名为 OPS_GROUP_CHANNEL 的渠道 target。"""
    try:
        arr = json.loads(NOTIFY_WEBHOOKS.read_text(encoding="utf-8"))
        for ch in arr if isinstance(arr, list) else []:
            if str(ch.get("name")) == OPS_GROUP_CHANNEL and ch.get("target"):
                return str(ch["target"])
    except Exception:
        pass
    return ""


def deliver_bot(text: str, chat_id: str = "") -> Tuple[bool, str]:
    """主通道：@tgzkw_bot 投运维群（缺省取 notify_webhooks.json 的 tg-ywqz target；
    实施81 起 duty 工具族经 ``chat_id`` 参数改投 @ai_zkw）。

    禁止把 chat_id 指回任何智聊在线的生产账号（见文件头 2026-09-10 注）。"""
    tok = _bot_token()
    if not tok:
        return False, "notify_webhooks.json 无 bot token"
    chat_id = str(chat_id or "").strip() or _ops_group_chat_id()
    if not chat_id:
        return False, f"notify_webhooks.json 无 {OPS_GROUP_CHANNEL} 渠道 target"
    if chat_id == SOUSAUN_TG_ID:
        return False, "拒绝：不得用 bot 私聊生产账号 @Sousaun（会触发 bot_peer_alert）"
    api = f"https://api.telegram.org/bot{tok}/sendMessage"
    chunks = _chunk(text, 3800)
    for i, ch in enumerate(chunks):
        payload = {"chat_id": str(chat_id),
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
        names = " / ".join(s["name"] for s in report["seats"]) or "（台账不可用）"
        LEDGER_PATH.write_text(
            "# 坐席机工作日志监控台账\n\n"
            f"> 监控对象：`deploy/machines.json` 中 `chatx_seat: true` 的机器（当前 {names}）"
            "的 ChatX 后端 `backend.log`\n"
            "> 工具：`monitor_seat_logs.py`（本机每 15 分钟巡检，真问题投递运维群 tg-ywqz）\n",
            encoding="utf-8")
    parts = [f"\n### {now}", f"- 巡检：{seats_line}"]
    if report["new_real"]:
        parts.append(f"- 新真问题 {len(report['new_real'])}：")
        for it in report["new_real"][:20]:
            parts.append(f"  - [{it['seat']}][{it['level']}] {it['why']} — `{it['line'][:180]}`")
    else:
        parts.append("- 新真问题：0（健康）")
    if sent is not None:
        parts.append(f"- 投递：{sent}")
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
    ap.add_argument("--note", default="", help="附加一行说明随本轮报告发出（如编制变更说明；需配 --baseline 或有新真问题才会投递）")
    args = ap.parse_args()

    report = scan(args.dry_run, args.since_min, args.include_benign)
    text = render_report(report, args.baseline)
    if args.note.strip():
        text += "\n\n📝 " + args.note.strip()
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
        print(f"\n[deliver] 运维群 {OPS_GROUP_CHANNEL}：{sent_status}")
        if not ok:
            rc = 1
    else:
        print(f"\n[deliver] 无新真问题，未打扰运维群 {OPS_GROUP_CHANNEL}（--baseline 可强制发健康摘要）。")
    _append_ledger(report, sent_status)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
