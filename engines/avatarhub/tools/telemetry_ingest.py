#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""telemetry_ingest.py — 匿名遥测接收端（下载站 VPS 部署，纯标准库零依赖）。

刻意不用 FastAPI/uvicorn：接收端跑在生产下载站上，引 venv+pip 是额外故障面；
本服务负载极小（客户端侧已做 24h 去重+日限频），stdlib ThreadingHTTPServer 足够。

路由：
  POST /t/ingest   接收单条事件 JSON（≤64KB；X-AH-T 令牌校验，未配令牌=放行）
  GET  /t/health   存活探针 {"ok":true,"today":N}
  GET  /t/stats    聚簇速览（须带 ?token=<令牌>）：Top 错误簇/版本分布/当日量

存储（env AH_INGEST_DATA，默认 ~/avatarhub-telemetry）：
  events-YYYYMMDD.jsonl   原始事件按天滚动（保留 60 天）
  agg.sqlite              聚簇表：sig → 计数/首末见/版本集（AI 归因管道直接读它）

运行：AH_INGEST_TOKEN=xxx python3 telemetry_ingest.py --port 8787 [--bind 0.0.0.0]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA_DIR = Path(os.environ.get("AH_INGEST_DATA", str(Path.home() / "avatarhub-telemetry")))
TOKEN = os.environ.get("AH_INGEST_TOKEN", "").strip()
WEBHOOK = os.environ.get("AH_INGEST_WEBHOOK", "").strip()   # 告警外推(可选 TG/企业微信/通用 webhook)
BODY_MAX = 64 * 1024
RATE_PER_HOUR = 120          # 单 IP 每小时上限（客户端本就限频，这是防滥用兜底）
KEEP_DAYS = 60
NEWCLUSTER_ALERT_MIN = 3     # 新错误簇当日累计达此次数即告警（过滤偶发单点）
# 错误率超阈自动停放（P5 重构为控制通道，安全）：不再重签 manifest（那需要代码密钥 A 在
#   VPS，安全大忌）；改为把出事版本写进【密钥 B 签名的 rollout_control.json】的 halted_versions。
#   客户端拉 manifest(密钥A验)+control(密钥B验)，两者叠加——VPS 只持密钥 B，即使被攻陷也只能
#   停更新(fail-safe DoS)、无法推代码。配 AH_INGEST_CONTROL 指向可写的 rollout_control.json 才启用。
AUTO_HALT_CONTROL = os.environ.get("AH_INGEST_CONTROL", "").strip()
AUTO_HALT_RATE = float(os.environ.get("AH_INGEST_HALT_RATE", "0.30"))   # 崩溃机器占比阈值
AUTO_HALT_MIN_ACTIVE = int(os.environ.get("AH_INGEST_HALT_MIN_ACTIVE", "8"))  # 最小活跃样本
# 控制通道 TTL（P6）：halt/覆盖默认过期时长——防某次紧急 halt 忘了 resume 永久冻结全网更新。
#   过期后客户端 verify_control 视其无效（等同无控制，恢复正常放量）。0=永不过期。
CONTROL_TTL_H = int(os.environ.get("AH_INGEST_CONTROL_TTL_H", "168"))   # 默认 7 天


def _control_mutate(mutate, ttl_h: int | None = None) -> bool:
    """读 rollout_control.json → mutate(dict) → 盖 ts/expires_at → 密钥 B 重签写回。私钥 B 允许在 VPS。"""
    if not AUTO_HALT_CONTROL:
        return False
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import release_sign
        cp = Path(AUTO_HALT_CONTROL)
        try:
            ctrl = json.loads(cp.read_text(encoding="utf-8"))
        except Exception:
            ctrl = {"halted_versions": [], "percent_overrides": {}}
        mutate(ctrl)
        now = int(time.time())
        ctrl["ts"] = now
        ttl = CONTROL_TTL_H if ttl_h is None else ttl_h
        # 仍有生效项（halt/覆盖）才设过期；全清空则去掉过期戳（永久"无控制"态）
        if ttl and (ctrl.get("halted_versions") or ctrl.get("percent_overrides")):
            ctrl["expires_at"] = now + ttl * 3600
        else:
            ctrl.pop("expires_at", None)
        release_sign.sign_control_dict(ctrl)
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps(ctrl, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def _audit(actor: str, action: str, version: str, extra: str = ""):
    """放量操作审计留痕（谁/何时/做了什么）——落 alerts(kind=audit) + 追加式哈希链（防篡改）。
    哈希链：每条 = sha256(上一条哈希 + 本条内容)，任何历史条目被改/删都会让后续链断裂，
    `_verify_audit_chain()` 或 /t/audit?verify=1 即可检出。链文件独立于 sqlite（双写留痕）。"""
    ts = int(time.time())
    line = f"actor={actor} action={action} version={version} {extra}".strip()
    try:
        _db.execute("INSERT INTO alerts(ts,level,kind,title,detail) VALUES(?,?,?,?,?)",
                    (ts, "info", "audit", f"[{actor}] {action} v{version}", line))
        _db.commit()
    except Exception:
        pass
    try:
        chain = DATA_DIR / "audit_chain.jsonl"
        prev = ""
        if chain.exists():
            last = None
            for ln in chain.read_text(encoding="utf-8").splitlines():
                if ln.strip():
                    last = ln
            if last:
                prev = json.loads(last).get("h", "")
        import hashlib as _hl
        rec = {"ts": ts, "actor": actor, "action": action, "version": version, "extra": extra}
        body = json.dumps(rec, ensure_ascii=False, sort_keys=True)
        rec["h"] = _hl.sha256((prev + body).encode("utf-8")).hexdigest()
        rec["prev"] = prev[:12]
        with open(chain, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _verify_audit_chain() -> dict:
    """校验审计哈希链完整性：返回 {ok, count, broken_at}。任一条被改/删 → ok=False。"""
    import hashlib as _hl
    chain = DATA_DIR / "audit_chain.jsonl"
    if not chain.exists():
        return {"ok": True, "count": 0, "broken_at": None}
    prev = ""
    n = 0
    for i, ln in enumerate(chain.read_text(encoding="utf-8").splitlines()):
        if not ln.strip():
            continue
        try:
            rec = json.loads(ln)
        except Exception:
            return {"ok": False, "count": n, "broken_at": i}
        h = rec.pop("h", "")
        rec.pop("prev", None)
        body = json.dumps({k: rec[k] for k in ("ts", "actor", "action", "version", "extra")},
                          ensure_ascii=False, sort_keys=True)
        if _hl.sha256((prev + body).encode("utf-8")).hexdigest() != h:
            return {"ok": False, "count": n, "broken_at": i}
        prev = h
        n += 1
    return {"ok": True, "count": n, "broken_at": None}

_lock = threading.Lock()
_rate: dict[str, list[float]] = {}
_db: sqlite3.Connection | None = None


def _init_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DATA_DIR / "agg.sqlite"), check_same_thread=False)
    db.execute("""CREATE TABLE IF NOT EXISTS clusters(
        sig TEXT PRIMARY KEY, service TEXT, exc TEXT, kind TEXT,
        first_ts INTEGER, last_ts INTEGER, count INTEGER,
        versions TEXT, sample_msg TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS daily(
        day TEXT PRIMARY KEY, events INTEGER)""")
    # 每日活跃/版本分布：心跳按 (day, anon_id) 去重记 DAU；版本/edition 计数
    db.execute("""CREATE TABLE IF NOT EXISTS active(
        day TEXT, anon_id TEXT, version TEXT, edition TEXT, gpu TEXT,
        PRIMARY KEY(day, anon_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS errdaily(
        day TEXT, kind TEXT, n INTEGER, PRIMARY KEY(day, kind))""")
    db.execute("""CREATE TABLE IF NOT EXISTS alerts(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, level TEXT,
        kind TEXT, title TEXT, detail TEXT)""")
    # 功能用量：按 (day, anon_id) 存最近一次上报的计数快照（客户端本地累计，日聚合上报）→
    #   看板做"多少台机器用过某功能"的漏斗 + 各功能总量。
    db.execute("""CREATE TABLE IF NOT EXISTS usage(
        day TEXT, anon_id TEXT, counters TEXT, PRIMARY KEY(day, anon_id))""")
    db.commit()
    return db


def _emit_alert(level: str, kind: str, title: str, detail: str):
    """落库 + 可选 webhook 外推（去重：同 kind+title 24h 内只发一次）。永不抛错。"""
    now = int(time.time())
    try:
        row = _db.execute("SELECT ts FROM alerts WHERE kind=? AND title=? ORDER BY ts DESC LIMIT 1",
                          (kind, title)).fetchone()
        if row and now - row[0] < 86400:
            return
        _db.execute("INSERT INTO alerts(ts,level,kind,title,detail) VALUES(?,?,?,?,?)",
                    (now, level, kind, title, detail))
        _db.commit()
    except Exception:
        return
    if not WEBHOOK:
        return
    try:
        import urllib.request
        body = json.dumps({"level": level, "kind": kind, "title": title, "detail": detail,
                           "ts": now}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(WEBHOOK, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=8).close()
    except Exception:
        pass


def _auto_halt(version: str, detail: str):
    """把出事版本加入控制通道 halted_versions（密钥 B 签名），坏版本自动刹车。
    需配 AH_INGEST_CONTROL；未配则返回 False（调用方转为只告警）。"""
    def mut(ctrl):
        hv = ctrl.setdefault("halted_versions", [])
        if version not in hv:
            hv.append(version)
    if _control_mutate(mut):
        _emit_alert("critical", "auto_halt", f"自动停放 app v{version}",
                    f"错误率超阈已通过控制通道 halt 该版本放量；{detail}")
        _audit("auto", "halt", version, detail)
        return True
    return False


def _anomaly_scan(ev: dict):
    """错误率异动检测（在 crash/error 入库后调用，锁内）：
      · 新错误簇首现且当日累计达阈值 → 告警；
      · 某 app 版本当日"崩溃机器占比"超阈且样本足 → 告警 + （配了 manifest 则）自动停放。"""
    try:
        kind = str(ev.get("kind", ""))
        if kind not in ("crash", "error"):
            return
        sig = str(ev.get("sig", ""))[:400]
        ver = str((ev.get("env") or {}).get("app", "") or (ev.get("env") or {}).get("release", ""))[:32]
        row = _db.execute("SELECT count, first_ts FROM clusters WHERE sig=?", (sig,)).fetchone()
        if row and row[0] == NEWCLUSTER_ALERT_MIN and (int(time.time()) - row[1] < 86400):
            _emit_alert("error", "new_cluster", f"新错误簇激增: {ev.get('exc','')}",
                        f"service={ev.get('service','')} sig={sig[:120]} 版本={ver} 当日已 {row[0]} 次")
        # 版本错误率：当日该版本崩溃机器占比（去重 anon_id）/ 该版本活跃机器
        if ver and kind == "crash":
            day = _today()
            active = _db.execute("SELECT COUNT(*) FROM active WHERE day=? AND version=?", (day, ver)).fetchone()
            active_n = active[0] if active else 0
            if active_n >= AUTO_HALT_MIN_ACTIVE:
                # 当日该版本崩溃事件数（近似崩溃机器；客户端 24h 去重已使其接近"崩溃机器数"）
                crashed = _db.execute(
                    "SELECT COALESCE(SUM(count),0) FROM clusters WHERE kind='crash' AND versions LIKE ? "
                    "AND last_ts>=?", (f'%{ver}%', int(time.time()) - 86400)).fetchone()[0]
                rate = crashed / max(1, active_n)
                if rate >= AUTO_HALT_RATE:
                    det = f"版本 {ver} 当日崩溃占比 {rate:.0%}（{crashed}/{active_n} 机），超阈 {AUTO_HALT_RATE:.0%}"
                    if not _auto_halt(ver, det):
                        _emit_alert("critical", "high_error_rate", f"版本 {ver} 错误率过高", det)
    except Exception:
        pass


def _today() -> str:
    return time.strftime("%Y%m%d")


def _rotate_cleanup():
    try:
        files = sorted(DATA_DIR.glob("events-*.jsonl"))
        for old in files[:-KEEP_DAYS]:
            old.unlink(missing_ok=True)
    except Exception:
        pass


def _store(ev: dict, ip: str):
    now = int(time.time())
    ev["_ip_day"] = ip.rsplit(".", 1)[0] + ".x"   # 只留 /24 段做粗粒度地区去重，不存全 IP
    ev["_recv"] = now
    with _lock:
        day = _today()
        with open(DATA_DIR / f"events-{day}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        sig = str(ev.get("sig", ""))[:400]
        ver = str((ev.get("env") or {}).get("app", "") or (ev.get("env") or {}).get("release", ""))[:32]
        row = _db.execute("SELECT versions, count FROM clusters WHERE sig=?", (sig,)).fetchone()
        if row:
            vers = set(json.loads(row[0] or "[]"))
            if ver:
                vers.add(ver)
            _db.execute("UPDATE clusters SET last_ts=?, count=?, versions=? WHERE sig=?",
                        (now, row[1] + 1, json.dumps(sorted(vers)[:20]), sig))
        else:
            _db.execute("INSERT INTO clusters VALUES(?,?,?,?,?,?,?,?,?)",
                        (sig, str(ev.get("service", ""))[:40], str(ev.get("exc", ""))[:80],
                         str(ev.get("kind", ""))[:16], now, now, 1,
                         json.dumps([ver] if ver else []), str(ev.get("msg", ""))[:300]))
        _db.execute("INSERT INTO daily(day,events) VALUES(?,1) "
                    "ON CONFLICT(day) DO UPDATE SET events=events+1", (day,))
        kind = str(ev.get("kind", ""))[:16]
        # DAU/版本分布：心跳事件按 (day, anon_id) 去重
        if kind == "heartbeat":
            aid = str(ev.get("anon_id", ""))[:40]
            env = ev.get("env") or {}
            _db.execute("INSERT OR REPLACE INTO active(day,anon_id,version,edition,gpu) VALUES(?,?,?,?,?)",
                        (day, aid, str(env.get("app", "") or env.get("release", ""))[:32],
                         str(ev.get("edition", ""))[:24], str(ev.get("gpu", ""))[:48]))
        # 功能用量快照（每台机器每天一条，取最新）→ 漏斗
        if kind == "usage":
            aid = str(ev.get("anon_id", ""))[:40]
            ctr = ev.get("counters") or {}
            if aid and isinstance(ctr, dict):
                _db.execute("INSERT OR REPLACE INTO usage(day,anon_id,counters) VALUES(?,?,?)",
                            (day, aid, json.dumps(ctr, ensure_ascii=False)[:2000]))
        # 错误率趋势：crash/error 按天计（发布质量看板用）
        if kind in ("crash", "error"):
            _db.execute("INSERT INTO errdaily(day,kind,n) VALUES(?,?,1) "
                        "ON CONFLICT(day,kind) DO UPDATE SET n=n+1", (day, kind))
        _db.commit()
        _anomaly_scan(ev)


def _rate_ok(ip: str) -> bool:
    now = time.time()
    with _lock:
        q = _rate.setdefault(ip, [])
        q[:] = [t for t in q if now - t < 3600]
        if len(q) >= RATE_PER_HOUR:
            return False
        q.append(now)
        if len(_rate) > 5000:          # 表防膨胀
            _rate.clear()
        return True


def _spark_svg(points, w=280, h=44, color="#7cc4ff"):
    """极简趋势折线 SVG（纯服务端渲染，零前端依赖）。points 按时间升序的数值列表。"""
    pts = [float(p or 0) for p in points]
    if not pts:
        return "<span style='color:#888'>—</span>"
    lo, hi = min(pts), max(pts)
    rng = (hi - lo) or 1.0
    n = len(pts)
    step = w / max(1, n - 1)
    coords = " ".join(f"{i*step:.1f},{h-4-(v-lo)/rng*(h-8):.1f}" for i, v in enumerate(pts))
    last = pts[-1]
    return (f"<svg width='{w}' height='{h}' style='vertical-align:middle'>"
            f"<polyline fill='none' stroke='{color}' stroke-width='2' points='{coords}'/>"
            f"</svg> <b>{int(last) if last==int(last) else round(last,2)}</b> <span style='color:#8a93a6'>(峰{int(hi)})</span>")


def _render_dashboard() -> str:
    """运营看板 v2（服务端直出静态 HTML，读 SQLite）：趋势小图 + 版本×错误率交叉 + 留存 + 分布。"""
    now = int(time.time())
    with _lock:
        dau = _db.execute("SELECT day, COUNT(*) FROM active GROUP BY day ORDER BY day ASC").fetchall()
        evd = _db.execute("SELECT day, events FROM daily ORDER BY day ASC").fetchall()
        errdc = _db.execute("SELECT day, SUM(n) FROM errdaily WHERE kind IN('crash','error') GROUP BY day ORDER BY day ASC").fetchall()
        ver = _db.execute("SELECT version, COUNT(*) FROM active WHERE day=? GROUP BY version ORDER BY 2 DESC", (_today(),)).fetchall()
        ed = _db.execute("SELECT edition, COUNT(*) FROM active WHERE day=? GROUP BY edition ORDER BY 2 DESC", (_today(),)).fetchall()
        top = _db.execute("SELECT service,exc,kind,count,versions,sample_msg FROM clusters ORDER BY count DESC LIMIT 20").fetchall()
        alerts = _db.execute("SELECT ts,level,kind,title FROM alerts WHERE kind!='audit' ORDER BY ts DESC LIMIT 15").fetchall()
        audits = _db.execute("SELECT ts,title,detail FROM alerts WHERE kind='audit' ORDER BY ts DESC LIMIT 15").fetchall()
        # 版本×错误率交叉：今日各版本活跃机器 + 近24h该版本崩溃数 → 崩溃占比（放量决策核心视图）
        crossrows = []
        for vrow in _db.execute("SELECT version, COUNT(*) FROM active WHERE day=? GROUP BY version", (_today(),)).fetchall():
            v, act = vrow[0] or "(未知)", vrow[1]
            crashed = _db.execute("SELECT COALESCE(SUM(count),0) FROM clusters WHERE kind='crash' "
                                  "AND versions LIKE ? AND last_ts>=?", (f'%{v}%', now - 86400)).fetchone()[0]
            rate = crashed / max(1, act)
            crossrows.append((v, act, crashed, f"{rate:.0%}"))
        crossrows.sort(key=lambda r: r[1], reverse=True)
        # 留存：今日活跃机器里，7天前也活跃过的占比
        d7 = time.strftime("%Y%m%d", time.localtime(now - 7 * 86400))
        today_ids = {r[0] for r in _db.execute("SELECT anon_id FROM active WHERE day=?", (_today(),)).fetchall()}
        d7_ids = {r[0] for r in _db.execute("SELECT anon_id FROM active WHERE day=?", (d7,)).fetchall()}
        ret7 = (len(today_ids & d7_ids) / len(d7_ids)) if d7_ids else 0.0
        # 功能漏斗：近 7 天各机器最新用量快照 → 用过某功能(计数>0)的机器数 + 该功能总量
        urows = _db.execute("SELECT counters FROM usage WHERE day>=?",
                            (time.strftime("%Y%m%d", time.localtime(now - 7 * 86400)),)).fetchall()
        funnel = {}   # feature -> [机器数, 总量]
        for (cj,) in urows:
            try:
                c = json.loads(cj)
            except Exception:
                continue
            for k, v in c.items():
                if isinstance(v, (int, float)) and v > 0:
                    f = funnel.setdefault(k, [0, 0])
                    f[0] += 1
                    f[1] += v
        funnel_rows = sorted(([k, m, int(t)] for k, (m, t) in funnel.items()),
                             key=lambda r: r[1], reverse=True)

    def esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def rows(data, cols):
        return "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in data) \
            or f"<tr><td colspan={cols} style='color:#888'>暂无数据</td></tr>"

    ctl_on = bool(AUTO_HALT_CONTROL)

    def crossrow_html(data):
        out = ""
        for v, act, cr, pct in data:
            warn = float(pct.rstrip("%")) >= 30
            style = " style='color:#ff7a7a;font-weight:600'" if warn else ""
            btns = ""
            if ctl_on:
                btns = (f"<button onclick=\"ctl('halt','{esc(v)}')\">停放</button> "
                        f"<button onclick=\"ctl('resume','{esc(v)}')\">恢复</button> "
                        f"<button onclick=\"ctl('set_percent','{esc(v)}')\">设%</button>")
            else:
                btns = "<span style='color:#8a93a6'>控制未启用</span>"
            out += (f"<tr><td>{esc(v)}</td><td>{act}</td><td>{cr}</td>"
                    f"<td{style}>{esc(pct)}{' ⚠可考虑 halt' if warn else ''}</td><td>{btns}</td></tr>")
        return out or "<tr><td colspan=5 style='color:#888'>暂无数据</td></tr>"
    css = ("body{font-family:system-ui,Segoe UI,sans-serif;margin:24px;background:#0f1420;color:#e6e9ef}"
           "h2{color:#7cc4ff;border-bottom:1px solid #26304a;padding-bottom:6px;margin-top:26px;font-size:16px}"
           "table{border-collapse:collapse;width:100%;margin:8px 0 18px;font-size:13px}"
           "td,th{border:1px solid #26304a;padding:6px 10px;text-align:left}"
           "th{background:#182034;color:#9fb3d1}h1{color:#fff}.sub{color:#8a93a6;font-size:13px}"
           ".cards{display:flex;gap:16px;flex-wrap:wrap;margin:12px 0}"
           ".card{background:#151b2b;border:1px solid #26304a;border-radius:10px;padding:12px 16px;min-width:300px}"
           ".card .t{color:#9fb3d1;font-size:12px;margin-bottom:4px}")
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>AvatarHub 运营看板</title>"
            f"<meta http-equiv='refresh' content='60'><style>{css}</style></head><body>"
            f"<h1>AvatarHub 运营看板 v2</h1><div class='sub'>匿名遥测 · 每60s自动刷新 · {esc(_today())} · 7日留存 <b>{ret7:.0%}</b></div>"
            f"<div class='cards'>"
            f"<div class='card'><div class='t'>DAU 趋势</div>{_spark_svg([r[1] for r in dau])}</div>"
            f"<div class='card'><div class='t'>每日事件量</div>{_spark_svg([r[1] for r in evd], color='#8ad19f')}</div>"
            f"<div class='card'><div class='t'>崩溃/错误 趋势</div>{_spark_svg([r[1] for r in errdc], color='#ff9f7a')}</div>"
            f"</div>"
            f"<h2>告警 (近15条)</h2><table><tr><th>时间</th><th>级别</th><th>类型</th><th>标题</th></tr>"
            f"{rows([(time.strftime('%m-%d %H:%M', time.localtime(a[0])), a[1], a[2], a[3]) for a in alerts], 4)}</table>"
            f"<h2>版本 × 错误率（放量决策）</h2><table><tr><th>app 版本</th><th>今日活跃机</th><th>近24h崩溃</th><th>崩溃占比</th><th>操作</th></tr>{crossrow_html(crossrows)}</table>"
            "<script>function ctl(a,v){var p=100;if(a==='set_percent'){p=prompt('设该版本放量百分比 0-100',50);if(p===null)return;}"
            "if(!confirm('确认 '+a+' 版本 '+v+(a==='set_percent'?(' → '+p+'%'):'')+' ？'))return;"
            "var t=new URLSearchParams(location.search).get('token');"
            "fetch('/t/control',{method:'POST',headers:{'Content-Type':'application/json','X-AH-T':t},"
            "body:JSON.stringify({action:a,version:v,percent:parseInt(p)})}).then(r=>r.json())"
            ".then(d=>{alert(d.ok?('已执行 '+a):('失败: '+(d.err||d.hint||'')));location.reload();});}</script>"
            f"<h2>放量操作审计 (近15条：谁/何时/改了什么)</h2><table><tr><th>时间</th><th>操作</th></tr>"
            f"{rows([(time.strftime('%m-%d %H:%M', time.localtime(a[0])), a[1]) for a in audits], 2)}</table>"
            f"<h2>今日档位分布</h2><table><tr><th>edition</th><th>机器数</th></tr>{rows(ed,2)}</table>"
            f"<h2>功能漏斗 (近7天：用过的机器数 / 总量)</h2><table><tr><th>功能</th><th>用过的机器数</th><th>累计次数</th></tr>{rows(funnel_rows,3)}</table>"
            f"<h2>错误簇 Top20 (按累计次数)</h2><table><tr><th>服务</th><th>异常</th><th>类型</th><th>次数</th><th>版本</th><th>样例(已脱敏)</th></tr>{rows(top,6)}</table>"
            f"</body></html>")


class H(BaseHTTPRequestHandler):
    server_version = "AHIngest/1"

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):   # 静默访问日志（事件已落 jsonl）
        pass

    def _send_html(self, code: int, html: str):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/t/health"):
            with _lock:
                row = _db.execute("SELECT events FROM daily WHERE day=?", (_today(),)).fetchone()
            return self._send(200, {"ok": True, "today": row[0] if row else 0})
        if self.path.startswith("/t/dash"):
            from urllib.parse import urlsplit, parse_qs
            q = parse_qs(urlsplit(self.path).query)
            if TOKEN and q.get("token", [""])[0] != TOKEN:
                return self._send_html(401, "<h3>401 需要 ?token=</h3>")
            return self._send_html(200, _render_dashboard())
        if self.path.startswith("/t/audit"):
            from urllib.parse import urlsplit, parse_qs
            q = parse_qs(urlsplit(self.path).query)
            if TOKEN and q.get("token", [""])[0] != TOKEN:
                return self._send(401, {"ok": False})
            with _lock:
                v = _verify_audit_chain()
            return self._send(200, {"ok": True, "chain_intact": v["ok"],
                                    "entries": v["count"], "broken_at": v["broken_at"]})
        if self.path.startswith("/t/alerts"):
            from urllib.parse import urlsplit, parse_qs
            q = parse_qs(urlsplit(self.path).query)
            if TOKEN and q.get("token", [""])[0] != TOKEN:
                return self._send(401, {"ok": False})
            with _lock:
                rows = _db.execute("SELECT ts,level,kind,title,detail FROM alerts "
                                   "ORDER BY ts DESC LIMIT 50").fetchall()
            return self._send(200, {"ok": True, "alerts": [
                {"ts": r[0], "level": r[1], "kind": r[2], "title": r[3], "detail": r[4]} for r in rows]})
        if self.path.startswith("/t/stats"):
            from urllib.parse import urlsplit, parse_qs
            q = parse_qs(urlsplit(self.path).query)
            if TOKEN and q.get("token", [""])[0] != TOKEN:
                return self._send(401, {"ok": False})
            with _lock:
                top = _db.execute(
                    "SELECT sig,service,exc,kind,count,first_ts,last_ts,versions,sample_msg "
                    "FROM clusters ORDER BY last_ts DESC LIMIT 50").fetchall()
                days = _db.execute("SELECT day,events FROM daily ORDER BY day DESC LIMIT 14").fetchall()
            return self._send(200, {"ok": True, "days": days, "clusters": [
                {"sig": r[0], "service": r[1], "exc": r[2], "kind": r[3], "count": r[4],
                 "first": r[5], "last": r[6], "versions": json.loads(r[7] or "[]"),
                 "msg": r[8]} for r in top]})
        return self._send(404, {"ok": False})

    def do_POST(self):
        if self.path.rstrip("/") == "/t/control":
            # 看板一键放量操作（密钥 B 签控制通道；令牌鉴权 + 只动 halted/percent，不碰代码）
            if TOKEN and self.headers.get("X-AH-T", "") != TOKEN:
                return self._send(401, {"ok": False, "err": "token"})
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n <= 0 or n > BODY_MAX:
                return self._send(413, {"ok": False})
            try:
                req = json.loads(self.rfile.read(n).decode("utf-8"))
                action = req.get("action"); ver = str(req.get("version", ""))
            except Exception:
                return self._send(400, {"ok": False, "err": "json"})
            if not AUTO_HALT_CONTROL:
                return self._send(200, {"ok": False, "err": "control_disabled",
                                        "hint": "服务端未配 AH_INGEST_CONTROL"})

            def mut(c):
                hv = c.setdefault("halted_versions", []); ov = c.setdefault("percent_overrides", {})
                if action == "halt" and ver and ver not in hv:
                    hv.append(ver)
                elif action == "resume" and ver in hv:
                    hv.remove(ver)
                elif action == "set_percent" and ver:
                    ov[ver] = max(0, min(100, int(req.get("percent", 100))))
            ok = _control_mutate(mut)
            if ok:
                actor = str(req.get("actor", "") or "dashboard")[:40]
                _emit_alert("info", "manual_control", f"看板操作 {action} v{ver}",
                            f"[{actor}] {action}（percent={req.get('percent','-')}）")
                _audit(actor, action, ver, f"percent={req.get('percent','-')}")
            return self._send(200 if ok else 500, {"ok": ok, "action": action, "version": ver})
        if self.path.rstrip("/") != "/t/ingest":
            return self._send(404, {"ok": False})
        ip = self.client_address[0]
        if TOKEN and self.headers.get("X-AH-T", "") != TOKEN:
            return self._send(401, {"ok": False, "err": "token"})
        if not _rate_ok(ip):
            return self._send(429, {"ok": False, "err": "rate"})
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0 or n > BODY_MAX:
            return self._send(413, {"ok": False, "err": "size"})
        try:
            ev = json.loads(self.rfile.read(n).decode("utf-8"))
            assert isinstance(ev, dict) and ev.get("kind") and ev.get("service")
        except Exception:
            return self._send(400, {"ok": False, "err": "json"})
        try:
            _store(ev, ip)
        except Exception as e:
            return self._send(500, {"ok": False, "err": type(e).__name__})
        return self._send(200, {"ok": True})


def main():
    global _db
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--bind", default="0.0.0.0")
    args = ap.parse_args()
    _db = _init_db()
    _rotate_cleanup()
    print(f"[ingest] listening {args.bind}:{args.port}  data={DATA_DIR}  "
          f"token={'set' if TOKEN else 'OPEN(未配令牌)'}")
    ThreadingHTTPServer((args.bind, args.port), H).serve_forever()


if __name__ == "__main__":
    main()
