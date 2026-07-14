# -*- coding: utf-8 -*-
"""轻量告警出口：状态化(去抖 + 恢复通知) + 持久化 + 可选 webhook(钉钉/企业微信/通用)。

设计目标：让"服务裸奔/鉴权回归"这类事件真正能"叫醒人"，而不是只躺在日志里。
零外部依赖（仅标准库），任何模块都能 `import alerts` 直接用。

产物：
  logs/alerts.jsonl       追加式历史（每次状态变化/重发一行，便于审计与回溯）
  logs/alerts_state.json  当前活动告警快照（key -> 状态，便于 UI/巡检读取）

Webhook 配置（任一即可，多个用逗号或换行分隔；无配置则仅落文件，不报错）：
  环境变量 AVATARHUB_ALERT_WEBHOOK
  文件     secrets/alert_webhooks.txt   （每行一个 URL，# 开头为注释）
  URL 含 dingtalk / weixin|qyapi → markdown 卡片（可点链接；AVATARHUB_ALERT_MARKDOWN=0
  退回纯文本）；其他 URL → 通用 {"text": ...} 纯文本。

去抖：同一 key 持续 firing 时，最多每 REFIRE_SEC 外发一次（默认 6h），但每次仍记历史。
恢复：firing -> resolved 时外发一条"已恢复"，避免"只报警不报平安"。

对外接口：
  raise_alert(key, title, detail="", level="error", source="", links=None) -> bool 是否外发
  clear_alert(key, note="")                          -> bool  本次是否外发恢复(带原链接)
  notify_event(title, detail="", links=None, md_body="") -> int 点状事件通知(不入 state)
  active_alerts()                                    -> dict  当前 firing 的告警
"""
import os
import json
import time
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOGS = BASE / "logs"
LOGS.mkdir(exist_ok=True)
HIST = LOGS / "alerts.jsonl"
STATE = LOGS / "alerts_state.json"
WEBHOOK_FILE = BASE / "secrets" / "alert_webhooks.txt"

REFIRE_SEC = int(os.environ.get("AVATARHUB_ALERT_REFIRE_SEC", str(6 * 3600)))
HTTP_TIMEOUT = float(os.environ.get("AVATARHUB_ALERT_HTTP_TIMEOUT", "6"))
# 历史文件滚动归档上限（字节）与保留份数：防止持续掉线把 alerts.jsonl 撑爆（实测曾达 4.3MB）。
HIST_MAX_BYTES = int(os.environ.get("AVATARHUB_ALERT_HIST_MAX_BYTES", str(2 * 1024 * 1024)))
HIST_BACKUPS = int(os.environ.get("AVATARHUB_ALERT_HIST_BACKUPS", "3"))


def _now():
    return time.time()


def _ts(t=None):
    return datetime.fromtimestamp(t or _now()).strftime("%Y-%m-%d %H:%M:%S")


def _load_state():
    try:
        if STATE.exists():
            return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(d):
    try:
        STATE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _rotate_hist():
    """超过 HIST_MAX_BYTES 时滚动归档：alerts.jsonl → .1 → .2 …，保留 HIST_BACKUPS 份。"""
    try:
        if HIST_MAX_BYTES <= 0 or not HIST.exists() or HIST.stat().st_size < HIST_MAX_BYTES:
            return
        oldest = HIST.with_suffix(HIST.suffix + f".{HIST_BACKUPS}")
        if oldest.exists():
            oldest.unlink()
        for i in range(HIST_BACKUPS - 1, 0, -1):
            src = HIST.with_suffix(HIST.suffix + f".{i}")
            if src.exists():
                src.rename(HIST.with_suffix(HIST.suffix + f".{i + 1}"))
        HIST.rename(HIST.with_suffix(HIST.suffix + ".1"))
    except Exception:
        pass


def _append_hist(rec):
    try:
        _rotate_hist()
        with open(HIST, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _webhooks():
    urls = []
    ev = os.environ.get("AVATARHUB_ALERT_WEBHOOK", "")
    for u in ev.replace(",", "\n").split("\n"):
        u = u.strip()
        if u and not u.startswith("#"):
            urls.append(u)
    try:
        if WEBHOOK_FILE.exists():
            for line in WEBHOOK_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
    except Exception:
        pass
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _payload(url, text, mentions=None):
    low = url.lower()
    if "dingtalk" in low:
        p = {"msgtype": "text", "text": {"content": text}}
        if mentions:
            p["at"] = {"atMobiles": list(mentions)}
        return p
    if "weixin" in low or "qyapi" in low:
        p = {"msgtype": "text", "text": {"content": text}}
        if mentions:
            p["text"]["mentioned_mobile_list"] = list(mentions)
        return p
    return {"text": text}


# ── 卡片化（钉钉/企微 markdown）─────────────────────────────────────
#   纯文本时代告警只能"看"，卡片时代链接能"点"——半夜收到差评告警，拇指一按直达调音台。
#   AVATARHUB_ALERT_MARKDOWN=0 可整体退回纯文本（自建 webhook 网关不认 markdown 时的逃生门）。
MARKDOWN_ON = os.environ.get("AVATARHUB_ALERT_MARKDOWN", "1") != "0"

_LEVEL_BADGE = {"critical": "🆘 严重·需人工介入", "error": "⛔ 错误",
                "warn": "⚠️ 警告", "warning": "⚠️ 警告", "info": "ℹ️ 通知"}


def _fmt_md(title, fields=None, links=None, level="warn"):
    """统一 markdown 卡片正文：标题 + 要点列表 + 可点链接行。
    只用钉钉/企微都认的子集（### / ** / - / []()），不玩花活。"""
    lines = ["### %s" % title,
             "- **级别**: %s" % _LEVEL_BADGE.get((level or "").lower(), level)]
    for k, v in (fields or []):
        if v not in (None, ""):
            lines.append("- **%s**: %s" % (k, v))
    if links:
        lines.append(" · ".join("[%s](%s)" % (lb, u) for lb, u in links if u))
    return "\n".join(lines)


def _md_payload(url, title, md, mentions=None):
    """平台 markdown 载荷；不认识的 webhook 返回 None（调用方回退纯文本）。
    钉钉：at 需正文含 @手机号 才高亮；企微：markdown 不支持 @手机号（另发文本补 @，见 _notify_md）。"""
    low = url.lower()
    if "dingtalk" in low:
        text = md + ("\n\n" + " ".join("@" + m for m in mentions) if mentions else "")
        return {"msgtype": "markdown",
                "markdown": {"title": title, "text": text},
                "at": {"atMobiles": list(mentions or [])}}
    if "weixin" in low or "qyapi" in low:
        return {"msgtype": "markdown", "markdown": {"content": md}}
    return None


def _post_payload(url, payload):
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        urllib.request.urlopen(req, timeout=HTTP_TIMEOUT).read()
        return True
    except Exception:
        return False


def _notify_md(title, md, fallback_text, mentions=None, crit=False):
    """逐 webhook 外发：认识的平台发 markdown 卡片，不认识的退纯文本。
    企微 markdown 无法 @手机号——critical 且配了 @ 时补发一条轻文本让 @ 生效（宁重不漏）。"""
    sent = 0
    for u in _webhooks():
        p = _md_payload(u, title, md, mentions) if MARKDOWN_ON else None
        if p is None:
            p = _payload(u, fallback_text, mentions)
        if _post_payload(u, p):
            sent += 1
            low = u.lower()
            if (MARKDOWN_ON and crit and mentions
                    and ("weixin" in low or "qyapi" in low)):
                _post_payload(u, _payload(u, "☝ " + title, mentions))
    return sent


def _mentions_from_env():
    raw = os.environ.get("AVATARHUB_ALERT_MENTION_MOBILES", "").strip()
    return [m.strip() for m in raw.replace(",", "\n").split("\n") if m.strip()]


def _post(url, text, mentions=None):
    try:
        data = json.dumps(_payload(url, text, mentions), ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        urllib.request.urlopen(req, timeout=HTTP_TIMEOUT).read()
        return True
    except Exception:
        return False


def _notify(text, mentions=None):
    sent = 0
    for u in _webhooks():
        if _post(u, text, mentions):
            sent += 1
    return sent


def _ps_quote(s):
    """转成 PowerShell 单引号字符串字面量（转义内部单引号、压平换行）。"""
    return "'" + str(s).replace("\r", " ").replace("\n", "  ").replace("'", "''") + "'"


def _local_toast(title, text, icon="Warning"):
    """离线本地强提示：Windows 托盘气泡（无需联网/无需配置/无需管理员）。
    不依赖任何第三方模块；失败静默（仅作兜底通道）。"""
    if os.name != "nt" or os.environ.get("AVATARHUB_ALERT_TOAST", "1") == "0":
        return
    try:
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "Add-Type -AssemblyName System.Drawing;"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::%s;"
            "$n.Visible=$true;"
            "$n.BalloonTipTitle=%s;"
            "$n.BalloonTipText=%s;"
            "$n.ShowBalloonTip(10000);"
            "Start-Sleep -Seconds 11;$n.Dispose()"
        ) % (icon, _ps_quote(title), _ps_quote(text))
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-WindowStyle", "Hidden", "-Command", ps],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
    except Exception:
        pass


def raise_alert(key, title, detail="", level="error", source="", mentions=None,
                links=None):
    """登记/触发一个告警。返回本次是否向 webhook 外发（受去抖控制）。
    level='critical' 时文本加醒目前缀；mentions(手机号列表)用于钉钉/企微 @人，
    未传则回退环境变量 AVATARHUB_ALERT_MENTION_MOBILES。
    links=[(label,url),...]：钉钉/企微渲染成 markdown 卡片可点链接，纯文本通道附在正文尾。"""
    st = _load_state()
    cur = st.get(key)
    now = _now()
    firing = bool(cur and cur.get("status") == "firing")
    do_notify = (not firing) or (now - float(cur.get("last_notified", 0)) >= REFIRE_SEC)
    is_crit = (level or "").lower() == "critical"
    if mentions is None and is_crit:
        mentions = _mentions_from_env()
    links = [(str(lb), str(u)) for lb, u in (links or []) if u]

    # 去重：同一告警持续 firing 期间不再每轮追加历史（曾导致 alerts.jsonl 每 10~20s 一行、
    #   膨胀到 4.3MB）。仅在「首次触发」或「超过 REFIRE_SEC 重发」时记一行历史；持续期的
    #   计数仍写入 state.json（count 字段），active_alerts()/UI 读取不受影响。
    if do_notify:
        _append_hist({"ts": _ts(now), "key": key, "event": "raise", "level": level,
                      "source": source, "title": title, "detail": detail,
                      "notified": True})
        head = "🆘【严重·需人工介入】" if is_crit else "[告警/%s]" % level.upper()
        text = "%s %s\n来源: %s\n时间: %s" % (head, title, source or key, _ts(now))
        if detail:
            text += "\n详情: " + str(detail)
        if links:
            text += "\n" + "\n".join("%s: %s" % (lb, u) for lb, u in links)
        if mentions:
            text += "\n" + " ".join("@" + m for m in mentions)
        md = _fmt_md(title, fields=[("详情", detail), ("来源", source or key),
                                    ("时间", _ts(now))],
                     links=links, level=level)
        _notify_md(title, md, text, mentions, crit=is_crit)
        _local_toast(("严重告警：" if is_crit else "服务告警：") + title,
                     (detail or title) + "\n来源: " + (source or key), icon="Error")

    st[key] = {
        "status": "firing", "title": title, "detail": detail, "level": level,
        "source": source,
        "links": links,
        "since": (cur.get("since") if firing else now),
        "since_str": (cur.get("since_str") if firing else _ts(now)),
        "last_notified": (now if do_notify else (cur.get("last_notified", 0) if cur else 0)),
        "count": ((cur.get("count", 0) + 1) if cur else 1),
    }
    _save_state(st)
    return do_notify


def clear_alert(key, note=""):
    """解除一个告警。仅当其先前为 firing 才外发"已恢复"。返回本次是否外发。"""
    st = _load_state()
    cur = st.get(key)
    if not cur or cur.get("status") != "firing":
        if cur and cur.get("status") != "resolved":
            cur["status"] = "resolved"
            _save_state(st)
        return False
    now = _now()
    _append_hist({"ts": _ts(now), "key": key, "event": "resolve",
                  "title": cur.get("title"), "note": note})
    text = "[恢复] %s\n来源: %s\n时间: %s" % (
        cur.get("title"), cur.get("source") or key, _ts(now))
    if note:
        text += "\n" + str(note)
    links = [tuple(x) for x in (cur.get("links") or [])]   # 报平安也带原链接，便于复核
    md = _fmt_md("✅ 已恢复 · %s" % cur.get("title"),
                 fields=[("说明", note), ("来源", cur.get("source") or key),
                         ("时间", _ts(now))],
                 links=links, level="info")
    _notify_md("已恢复 · %s" % cur.get("title"), md, text)
    _local_toast("已恢复：" + str(cur.get("title")), note or "告警已解除", icon="Information")
    cur["status"] = "resolved"
    cur["resolved_at"] = now
    cur["resolved_str"] = _ts(now)
    _save_state(st)
    return True


def notify_event(title, detail="", level="warn", source="", links=None,
                 md_body=""):
    """一次性「事件」通知(非状态化)：直接投递 webhook + 本地弹窗,不写 state / 不进 active_alerts。
    适合"漂移/回归/完成"这类点状事件——只需"当时叫一声",无需像 raise_alert 那样持续 firing 到手动 clear，
    因而不会在 /ops 常驻为"活动告警"。仍记一行历史(event=notify)便于审计。返回外发通道数。
    links 同 raise_alert；md_body 传入则整段作为卡片正文（战报摘要这类多行报文自己排版）。"""
    now = _now()
    _append_hist({"ts": _ts(now), "key": "", "event": "notify", "level": level,
                  "source": source, "title": title, "detail": detail})
    links = [(str(lb), str(u)) for lb, u in (links or []) if u]
    head = "[事件/%s]" % (level or "info").upper()
    plain_links = ("\n" + "\n".join("%s: %s" % (lb, u) for lb, u in links)) if links else ""
    md_links = ("\n" + " · ".join("[%s](%s)" % (lb, u) for lb, u in links)) if links else ""
    if md_body:
        md = md_body + md_links
        text = "%s %s\n%s%s" % (head, title, md_body, plain_links)
    else:
        md = _fmt_md(title, fields=[("详情", detail), ("来源", source or "-"),
                                    ("时间", _ts(now))],
                     links=links, level=level)
        text = "%s %s\n来源: %s\n时间: %s" % (head, title, source or "-", _ts(now))
        if detail:
            text += "\n详情: " + str(detail)
        text += plain_links
    sent = _notify_md(title, md, text)
    _local_toast("事件：" + str(title), detail or title, icon="Warning")
    return sent


def active_alerts():
    return {k: v for k, v in _load_state().items() if v.get("status") == "firing"}


def _mask_url(u):
    """只保留协议+域名，路径/token 用 … 隐去，避免把 webhook 密钥回显到前端。"""
    u = str(u)
    try:
        parts = u.split("://", 1)
        scheme = parts[0] if len(parts) > 1 else ""
        host = parts[-1].split("/", 1)[0]
        return (scheme + "://" if scheme else "") + host + "/…"
    except Exception:
        return "webhook"


def channels():
    """当前告警通道概况（不泄露完整地址）：webhook 数/掩码、是否本地弹窗、@人。供前端判断"告警会不会真的送达"。"""
    urls = _webhooks()
    return {"webhook_count": len(urls), "webhooks": [_mask_url(u) for u in urls],
            "local_toast": (os.name == "nt" and os.environ.get("AVATARHUB_ALERT_TOAST", "1") != "0"),
            "mentions": _mentions_from_env()}


def send_test(text=None, links=None):
    """告警通路自检：逐 webhook 投递一条测试消息 + 本地弹窗，返回每通道结果。
    不写 state/历史、不影响 active_alerts（纯通路验证，随便点不留痕）。
    links 传入时钉钉/企微发 markdown 卡片——顺带验证"链接可点"这一步。"""
    text = text or ("【测试告警】开播健康告警通路自检 · " + _ts())
    links = [(str(lb), str(u)) for lb, u in (links or []) if u]
    md = _fmt_md("🔔 " + text,
                 fields=[("时间", _ts()), ("说明", "这是一条测试消息，收到即通路正常")],
                 links=links, level="info")
    fallback = text + (("\n" + "\n".join("%s: %s" % (lb, u) for lb, u in links))
                       if links else "")
    urls = _webhooks()
    results = []
    for u in urls:
        p = _md_payload(u, text, md) if MARKDOWN_ON else None
        fmt = "markdown" if p is not None else "text"
        if p is None:
            p = _payload(u, fallback)
        results.append({"url": _mask_url(u), "ok": _post_payload(u, p), "fmt": fmt})
    sent = sum(1 for r in results if r["ok"])
    toast_enabled = (os.name == "nt" and os.environ.get("AVATARHUB_ALERT_TOAST", "1") != "0")
    if toast_enabled:
        _local_toast("测试告警", text, icon="Information")
    return {"ok": True, "webhook_count": len(urls), "sent": sent, "results": results,
            "local_toast": toast_enabled, "mentions": _mentions_from_env(),
            "markdown": MARKDOWN_ON}


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "list":
        a = active_alerts()
        print(json.dumps(a, ensure_ascii=False, indent=2) if a else "（无活动告警）")
    else:
        print("用法: python alerts.py list   # 查看当前活动告警")
        print("活动告警数:", len(active_alerts()))
