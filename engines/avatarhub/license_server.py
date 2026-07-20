# -*- coding: utf-8 -*-
"""
license_server.py — 厂商端在线激活服务（输兑换码即激活，自助换 license）。

定位：把「客户发指纹 → 厂商手动 issue → 回传 license.key」自动化为
      「客户在产品里输兑换码 → 本服务用私钥签发并回传 → 产品本地复验落盘」。
      纯标准库 HTTP（零新依赖）+ cryptography（厂商端本就有）。**仅厂商部署，绝不随产品分发。**

安全：
  - 私钥只在本服务进程内（secrets/license_vendor_sk.pem），从不下发。
  - 客户端拿到已签授权后仍用内置公钥**复验签名 + 校验机器指纹**（见 license.activate_from_text）。
  - 面向公网时务必置于 TLS 反代之后，并对 /api/activate 限速 / 加前置鉴权。

兑换码 / 订单（secrets/orders.json）：每个码含 edition / days / seats（可激活机器数）/
  licensee / features，记录已激活指纹；同机重复激活幂等（不额外占座）。

CLI：
  python license_server.py addcode --edition pro --days 365 --seats 1 --licensee ACME [--feature k=v ...]
  python license_server.py listcodes
  python license_server.py serve --host 0.0.0.0 --port 8770
HTTP：
  POST /api/activate          {"code": "...", "fingerprint": "..."} → {"ok":true,"license":{...}}
  POST /api/trial             {"fingerprint": "..."} → 一键试用升级：签发限时旗舰试授权（每指纹一次）
  POST /api/telemetry         {匿名安装回执} → {"ok":true}（白名单清洗后追加 secrets/telemetry.jsonl）
  GET  /api/telemetry/summary → 聚合看板数据（会话/组件成功率、最常失败组件、通道/源分布）
  GET  /dashboard             → 极简看板页（读 summary 渲染）
  GET  /api/health            → {"ok":true,...}
"""
from __future__ import annotations

import argparse
import hashlib
import hmac as _hmac
import json
import os
import secrets as _secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import license as lic

BASE = lic.BASE_DIR
SECRETS = BASE / "secrets"
SK_FILE = SECRETS / "license_vendor_sk.pem"
ORDERS_FILE = SECRETS / "orders.json"
TRIALS_FILE = SECRETS / "trials.json"   # P10 一键试用台账（一机一次的服务端记忆）

TELEMETRY_FILE = SECRETS / "telemetry.jsonl"
HEALTH_NOTIFY_FILE = SECRETS / "health_notify.json"   # P15-2 健康告警「谁在哪天提醒过」标记
MAX_BODY = 64 * 1024          # 请求体上限：遥测回执很小，超此即拒（防滥用/撑爆磁盘）
# 试用升级默认天数（serve --trial-days 可覆写；0=关闭试用端点）。
# P12 修复：cmd_serve 一直引用此常量但从未定义——serve 起服自 P10 起就 NameError，冒烟揪出。
TRIAL_DAYS = max(0, int(os.environ.get("AVATARHUB_TRIAL_UP_DAYS", "7")))

_LOCK = threading.Lock()
_TELE_LOCK = threading.Lock()
_RATE_LOCK = threading.Lock()
_RATE_HITS: dict = {}       # "win:key" -> 次数（固定窗口计数；跨窗口自然失效 + 超量时清旧窗口防无界）
_STATE: dict = {"sk": None, "orders_path": ORDERS_FILE, "telemetry_path": TELEMETRY_FILE,
                "trials_path": TRIALS_FILE, "trial_days": 7,
                "public_base": "", "qi_edition": "pro", "qi_days": 365,
                "notify_activate": False,   # 仅 serve 置位：单测/CLI 直调不外发
                "health_th": 80, "health_notify_path": HEALTH_NOTIFY_FILE,
                "rate_window": 60, "rate_max_ip": 30}   # 反滥用限速（每 IP 每窗口最多请求数；0=关）


def _rate_check(key: str, now: float, window_s: int, limit: int) -> bool:
    """固定窗口限速：同 key 在 window_s 内至多 limit 次。limit<=0=不限（私网/单测）。
    线程安全、纯进程内、零依赖。返回 True=放行 / False=超限。跨窗口自然重置；
    字典超量时清掉非当前窗口的键，防长跑无界增长。"""
    if limit <= 0:
        return True
    window_s = max(1, int(window_s))
    win = int(now // window_s)
    k = "%d:%s" % (win, key)
    with _RATE_LOCK:
        n = _RATE_HITS.get(k, 0) + 1
        _RATE_HITS[k] = n
        if len(_RATE_HITS) > 8192:
            pref = "%d:" % win
            for kk in [x for x in _RATE_HITS if not x.startswith(pref)]:
                _RATE_HITS.pop(kk, None)
        return n <= limit


# ── P14-2 一键发码（quickissue）：临期通知里的「转化动作」───────────────
# 销售收到「试用临期」webhook 后，点内附签名链接 → 确认页一键出码 → 发给客户输入即转正。
# 签名 = HMAC(sk 派生密钥, fp|exp)：无新增密钥文件，链接自带过期；同指纹幂等（未用完不重发）。
def _qi_key() -> bytes:
    try:
        return hashlib.sha256(SK_FILE.read_bytes() + b"|quickissue").digest()
    except Exception:
        return b""


def qi_sign(fp: str, exp: int) -> str:
    return _hmac.new(_qi_key(), f"{fp}|{exp}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def qi_verify(fp: str, exp: int, sig: str, now: "int|None" = None) -> tuple:
    """→ (ok, err)。校验签名与链接时效（exp 为链接过期时间戳）。"""
    if not fp or not sig:
        return False, "缺参数"
    if int(now or time.time()) > int(exp):
        return False, "链接已过期（重新等下一封临期通知，或用 addcode 手动出码）"
    if not _hmac.compare_digest(qi_sign(fp, int(exp)), sig):
        return False, "签名不合法"
    return True, ""


def qi_link(fp: str, base: str, days_valid: int = 7) -> str:
    exp = int(time.time()) + days_valid * 86400
    return (f"{base.rstrip('/')}/quickissue?fp={urllib.parse.quote(fp, safe='')}"
            f"&exp={exp}&sig={qi_sign(fp, exp)}")


def qi_issue(orders: dict, fp: str, edition: str, days: int) -> tuple:
    """幂等出码：该指纹已有「quickissue 出的、还没被激活」的码→原样返回；否则新发。
    返回 (code, reused)。调用方持锁并负责落盘。"""
    for code, rec in orders.get("codes", {}).items():
        if (rec.get("via") == "quickissue" and rec.get("fp_hint") == fp
                and not rec.get("activations") and not rec.get("disabled")):
            return code, True
    code = _gen_code()
    orders.setdefault("codes", {})[code] = {
        "edition": edition, "days": days, "seats": 1,
        "licensee": f"quickissue:{fp[:12]}", "features": {},
        "created": int(time.time()), "activations": [],
        "via": "quickissue", "fp_hint": fp,
    }
    return code, False


def qi_funnel(orders: dict, trials: dict) -> dict:
    """P15-1 一键发码四级漏斗（纯函数）：临期通知 → 链接点开 → 出码 → 激活。
    台账即真相：notified=trials 打过临期标的机器数；opened=确认页首开记录（orders.qi_opened，
    GET /quickissue 验签通过时记一次）；issued/activated=via=quickissue 的码及其激活。
    百分比可超 100（补发码/直接 API 出码不经确认页），如实呈现不粉饰。"""
    fps = (trials or {}).get("fps", {}) or {}
    notified = sum(1 for r in fps.values() if r.get("notified_48h"))
    opened = len((orders or {}).get("qi_opened", {}) or {})
    qi_codes = [r for r in ((orders or {}).get("codes", {}) or {}).values()
                if r.get("via") == "quickissue"]
    issued = len(qi_codes)
    activated = sum(1 for r in qi_codes if r.get("activations"))
    pct = lambda a, b: round(a / b * 100, 1) if b else 0.0
    return {"notified": notified, "opened": opened, "issued": issued, "activated": activated,
            "open_pct": pct(opened, notified), "issue_pct": pct(issued, opened),
            "activate_pct": pct(activated, issued)}


def _notify_activation(info: dict):
    """P15-1 发码→激活闭环回推：新占座激活即 webhook 通知——销售不用轮询台账就知道
    「客户已转正」。只报两类高价值事件：quickissue 码被激活（一键发码闭环达成）/
    试用客户用码转正（漏斗终点）。通知是增益不是依赖：alerts 缺失/无 webhook 静默降级 stderr。
    仅 serve 模式外发（_STATE.notify_activate 由 cmd_serve 置位；单测/CLI 直调不惊扰真实通道）。"""
    fp, code = info.get("fp", ""), info.get("code", "")
    via_qi = info.get("via") == "quickissue"
    trial_conv = False
    try:
        rec = (_load_trials(_STATE["trials_path"]).get("fps") or {}).get(fp)
        trial_conv = bool(rec and int(info.get("issued", 0)) >= int(rec.get("issued", 0)))
    except Exception:
        pass
    if not (via_qi or trial_conv):
        return
    title = "一键发码已激活·闭环达成" if via_qi else "试用客户转正"
    msg = (f"码 {code}（{info.get('edition', '-')} 档 · {info.get('days', '-')} 天）已激活"
           f" · 指纹 {fp}" + (" · 该机由试用转正" if trial_conv else ""))
    sys.stderr.write(f"[activation] 转化回推: {title} · {msg}\n")
    try:
        import alerts
        alerts.notify_event(title, detail=msg, level="info", source="license_server/激活")
    except Exception:
        pass


# ── 遥测回执接收（不可信输入 → 严格白名单清洗，绝不原样落盘）────────────────
def _to_int(v, d=0):
    try:
        return int(v)
    except Exception:
        return d


def _to_float(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


def _s(v, n=64) -> str:
    return str(v)[:n] if v is not None else ""


def _sanitize_receipt(o: dict) -> dict | None:
    """只保留已知 schema 字段并强制类型/截断/限长——白名单之外（含任何意外 PII/超大字段）一律丢弃。
    服务端接收的是不可信客户端输入，这一步是隐私与稳健性的最后闸门。"""
    if not isinstance(o, dict):
        return None
    srcs = o.get("sources", [])
    items = o.get("items", [])
    out = {
        "schema": _to_int(o.get("schema", 1), 1),
        "ts": _s(o.get("ts", ""), 32),
        "kind": _s(o.get("kind", ""), 16),
        "manifest_version": _s(o.get("manifest_version", ""), 32),
        "channel": _s(o.get("channel", ""), 24),
        "edition": _s(o.get("edition", ""), 24),
        "platform": _s(o.get("platform", ""), 24),
        "sources": [_s(s, 80) for s in srcs[:10]] if isinstance(srcs, list) else [],
        "total_bytes": _to_int(o.get("total_bytes", 0)),
        "total_secs": _to_float(o.get("total_secs", 0)),
        "ok": _to_int(o.get("ok", 0)),
        "fail": _to_int(o.get("fail", 0)),
        "received_at": int(time.time()),
    }
    if o.get("anon_id"):
        out["anon_id"] = _s(o.get("anon_id"), 64)
    if o.get("gpu"):
        out["gpu"] = _s(o.get("gpu"), 48)
    if o.get("vram_gb") is not None:
        out["vram_gb"] = _to_int(o.get("vram_gb"))
    clean = []
    if isinstance(items, list):
        for it in items[:200]:                 # 组件数封顶，防超长 items 撑爆
            if isinstance(it, dict):
                clean.append({"cid": _s(it.get("cid", ""), 64), "ok": bool(it.get("ok", True)),
                              "bytes": _to_int(it.get("bytes", 0)), "secs": _to_float(it.get("secs", 0)),
                              "err": _s(it.get("err", ""), 40)})
    out["items"] = clean
    return out


def record_telemetry(obj: dict) -> tuple[int, dict]:
    rec = _sanitize_receipt(obj)
    if rec is None:
        return 400, {"ok": False, "error": "回执格式非法。"}
    path = _STATE["telemetry_path"]
    with _TELE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return 200, {"ok": True}


def aggregate_telemetry(path: Path) -> dict:
    """读 JSONL 回执汇总成看板数据（会话/组件成功率、最常失败组件、通道/档位/源分布）。"""
    import collections
    recs = []
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line:
                try:
                    recs.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    n = len(recs)
    sess_ok = sum(1 for r in recs if r.get("fail", 0) == 0)
    comp_total = comp_fail = 0
    fail_by_cid, err_by_class = collections.Counter(), collections.Counter()
    by_channel, by_edition, by_version, src_hit = (collections.Counter() for _ in range(4))
    for r in recs:
        by_channel[r.get("channel") or "stable"] += 1
        by_edition[r.get("edition") or "-"] += 1
        by_version[r.get("manifest_version") or "-"] += 1
        for s in (r.get("sources") or [])[:1]:
            src_hit[s] += 1
        for it in r.get("items", []):
            comp_total += 1
            if not it.get("ok", True):
                comp_fail += 1
                fail_by_cid[it.get("cid", "?")] += 1
                if it.get("err"):
                    err_by_class[it["err"]] += 1
    return {
        "receipts": n, "sess_ok": sess_ok,
        "sess_ok_pct": round(sess_ok / n * 100, 1) if n else 100.0,
        "comp_total": comp_total, "comp_ok": comp_total - comp_fail,
        "comp_ok_pct": round((comp_total - comp_fail) / comp_total * 100, 1) if comp_total else 100.0,
        "fail_by_cid": dict(fail_by_cid.most_common(10)),
        "err_by_class": dict(err_by_class.most_common(10)),
        "by_channel": dict(by_channel), "by_edition": dict(by_edition),
        "by_version": dict(by_version), "src_hit": dict(src_hit.most_common(10)),
    }


# P14 一键发码确认页：webhook 链接 → 这里 → 出码复制。占位符字符串替换（无模板依赖）。
_QI_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>一键发码 · AvatarHub</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font:15px/1.7 -apple-system,Segoe UI,Microsoft YaHei,sans-serif;background:#0f1115;color:#d6dae0;
display:flex;justify-content:center;padding-top:8vh;margin:0}
.box{background:#171a21;border:1px solid #252a33;border-radius:12px;padding:26px 30px;max-width:480px}
h2{margin:0 0 8px;font-size:18px}.muted{color:#7d8590;font-size:13px}
.fp{font-family:Consolas,monospace;background:#0d0f13;border:1px solid #252a33;border-radius:6px;
padding:6px 10px;margin:10px 0;word-break:break-all;font-size:12px}
button{background:#25b566;color:#04140b;border:0;border-radius:8px;padding:10px 22px;font-size:15px;
font-weight:700;cursor:pointer;margin-top:8px}button:disabled{opacity:.5}
.code{display:none;font:700 22px Consolas,monospace;color:#ffd479;background:#0d0f13;border:1px dashed #4a4028;
border-radius:8px;padding:12px 16px;margin-top:14px;text-align:center;letter-spacing:1px}
.tip{display:none;color:#25b566;font-size:13px;margin-top:8px}.err{color:#e0574a;margin-top:10px}</style>
</head><body><div class="box">
<h2>给临期试用客户出正式码</h2>
<div class="muted">机器指纹（来自临期通知）：</div><div class="fp">__FP__</div>
<div class="muted">将签发：<b>__ED__</b> 档 · <b>__DAYS__</b> 天 · 1 座（同指纹幂等：码没被用不会重发）</div>
<button id="go">一键出码</button>
<div class="code" id="code"></div><div class="tip" id="tip">已复制 · 发给客户，在产品「输入兑换码」处粘贴即转正</div>
<div class="err" id="err"></div>
<script>
document.getElementById('go').onclick=async function(){
  this.disabled=true;
  try{
    const r=await fetch('/api/quickissue',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({fp:'__FP__',exp:__EXP__,sig:'__SIG__'})});
    const d=await r.json();
    if(!d.ok) throw new Error(d.error||'出码失败');
    const c=document.getElementById('code'); c.textContent=d.code; c.style.display='block';
    try{ await navigator.clipboard.writeText(d.code); document.getElementById('tip').style.display='block'; }catch(_){ }
    this.textContent=d.reused?'已出码（复用未激活码）':'已出码';
  }catch(e){ document.getElementById('err').textContent=e.message; this.disabled=false; }
};
</script></div></body></html>"""

def customers_view(path: Path, limit: int = 50) -> dict:
    """P14-3 客户健康度行级视图：回执按 anon_id 聚合成「客户面」。
    统计口径全来自匿名回执（无 PII）：最近活跃/版本/档位/通道/GPU 取该客户最新一条回执，
    会话成功率=fail==0 的回执占比，组件成功率=items 逐条累计；失败组件 Top 供下钻。
    anon_id 缺失的老客户端回执归入「-」桶（仍可看到量，只是无法分行）。"""
    import collections
    recs = []
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line:
                try:
                    recs.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    by: dict = {}
    for r in recs:
        k = r.get("anon_id") or "-"
        c = by.setdefault(k, {"receipts": 0, "sess_fail": 0, "comp_total": 0, "comp_fail": 0,
                              "last_ts": 0, "edition": "", "version": "", "channel": "",
                              "platform": "", "gpu": "", "fails": collections.Counter()})
        c["receipts"] += 1
        if r.get("fail", 0):
            c["sess_fail"] += 1
        ts = int(r.get("received_at", 0) or 0)
        if ts >= c["last_ts"]:
            c["last_ts"] = ts
            c["edition"] = r.get("edition") or c["edition"]
            c["version"] = r.get("manifest_version") or c["version"]
            c["channel"] = r.get("channel") or c["channel"]
            c["platform"] = r.get("platform") or c["platform"]
            c["gpu"] = r.get("gpu") or c["gpu"]
        for it in r.get("items", []) or []:
            c["comp_total"] += 1
            if not it.get("ok", True):
                c["comp_fail"] += 1
                c["fails"][it.get("cid", "?")] += 1
    rows = []
    for k, c in sorted(by.items(), key=lambda kv: -kv[1]["last_ts"])[:max(1, limit)]:
        n, cf, ct = c["receipts"], c["comp_fail"], c["comp_total"]
        rows.append({
            "anon_id": k, "receipts": n, "last_ts": c["last_ts"],
            "edition": c["edition"] or "-", "version": c["version"] or "-",
            "channel": c["channel"] or "-", "platform": c["platform"] or "-", "gpu": c["gpu"] or "-",
            "sess_ok_pct": round((n - c["sess_fail"]) / n * 100, 1) if n else 100.0,
            "comp_ok_pct": round((ct - cf) / ct * 100, 1) if ct else 100.0,
            "top_fails": dict(c["fails"].most_common(5)),
        })
    return {"ok": True, "total": len(by), "customers": rows}


_DASHBOARD_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>AvatarHub 发布质量看板</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font:14px/1.6 -apple-system,Segoe UI,Microsoft YaHei,sans-serif;background:#0f1115;color:#d6dae0;margin:0;padding:24px}
h1{font-size:20px;margin:0 0 4px}.sub{color:#7d8590;margin-bottom:18px}
.cards{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:20px}
.card{background:#171a21;border:1px solid #252a33;border-radius:10px;padding:14px 18px;min-width:150px}
.card .v{font-size:26px;font-weight:700;color:#25b566}.card .k{color:#8b929c;font-size:12px}
.grid{display:flex;gap:14px;flex-wrap:wrap}.box{background:#171a21;border:1px solid #252a33;border-radius:10px;padding:14px 18px;flex:1;min-width:260px}
.box h3{margin:0 0 8px;font-size:14px;color:#cfd4da}.row{display:flex;justify-content:space-between;border-bottom:1px solid #20242c;padding:3px 0}
.row .n{color:#e0574a}.muted{color:#7d8590}</style></head><body>
<h1>AvatarHub 发布质量看板</h1><div class="sub">来自客户端匿名健康回执（无任何个人信息）· <span id="ts"></span></div>
<div class="cards" id="cards"></div><div class="grid" id="grid"></div>
<script>
function rows(o){return Object.entries(o||{}).map(([k,v])=>`<div class="row"><span>${k}</span><span class="n">${v}</span></div>`).join('')||'<div class="muted">（暂无）</div>';}
function box(t,o){return `<div class="box"><h3>${t}</h3>${rows(o)}</div>`;}
/* 回执汇总 + 试用漏斗（P12 台账即真相 / P13 按签发周时序）一次性渲染：
   两接口曾各自 then 里一个 innerHTML= 一个 +=，谁后到谁说了算——漏斗先到即被回执覆盖
   （竞态假 miss）。Promise.allSettled 等齐再画，顺序不再影响结果。 */
Promise.allSettled([
 fetch('/api/telemetry/summary').then(r=>r.json()),
 fetch('/api/funnel').then(r=>r.json()),
]).then(([tRes,fRes])=>{
 document.getElementById('ts').textContent=new Date().toLocaleString();
 const d=tRes.status==='fulfilled'?tRes.value:null;
 const f=(fRes.status==='fulfilled'&&fRes.value&&fRes.value.ok)?fRes.value:null;
 let cards='', grid='';
 if(d){
  cards+=`<div class="card"><div class="v">${d.receipts}</div><div class="k">回执数</div></div>`+
   `<div class="card"><div class="v">${d.sess_ok_pct}%</div><div class="k">会话成功率 (${d.sess_ok}/${d.receipts})</div></div>`+
   `<div class="card"><div class="v">${d.comp_ok_pct}%</div><div class="k">组件成功率 (${d.comp_ok}/${d.comp_total})</div></div>`;
  grid+=box('最常失败组件',d.fail_by_cid)+box('失败错误类名',d.err_by_class)+
   box('通道分布',d.by_channel)+box('档位分布',d.by_edition)+
   box('版本分布',d.by_version)+box('主用下载源',d.src_hit);
 } else { cards+='<div class="muted">回执加载失败</div>'; }
 if(f){
  cards+=`<div class="card"><div class="v">${f.trial_issued}</div><div class="k">发出试签（在试 ${f.trial_active}）</div></div>`+
   `<div class="card"><div class="v">${f.converted}</div><div class="k">试用转正（${f.conversion_pct}%）</div></div>`;
  /* P15-1 一键发码四级漏斗：通知→点开→出码→激活，销售动作的转化衰减一眼可见 */
  const q=f.quickissue;
  if(q&&(q.notified||q.issued)){
   const steps=[['临期通知',q.notified,''],['链接点开',q.opened,q.open_pct+'%'],
                ['一键出码',q.issued,q.issue_pct+'%'],['激活转正',q.activated,q.activate_pct+'%']];
   const mx=Math.max(1,...steps.map(s=>s[1]));
   grid+=`<div class="box"><h3>一键发码漏斗（通知 → 点开 → 出码 → 激活）</h3>`+steps.map(([k,v,p])=>
    `<div class="row"><span>${k}${p?` <span class="muted">${p}</span>`:''}</span>
     <span style="display:flex;align-items:center;gap:8px">
      <span style="display:inline-block;width:${Math.round(v/mx*90)}px;height:8px;background:#25b566;border-radius:4px"></span>
      <span class="n">${v}</span></span></div>`).join('')+`</div>`;
  }
  const wk=f.weekly||[];
  if(wk.some(b=>b.issued)){
   const mx=Math.max(1,...wk.map(b=>b.issued));
   const bars=wk.map(b=>`<div title="${b.week} 周 · 签发 ${b.issued} · 转正 ${b.converted}（${b.pct}%）"
       style="flex:1;display:flex;flex-direction:column-reverse;height:46px;gap:1px">
     <div style="height:${Math.round(b.converted/mx*44)}px;min-height:${b.converted?2:0}px;background:#25b566;border-radius:2px"></div>
     <div style="height:${Math.round((b.issued-b.converted)/mx*44)}px;min-height:${(b.issued-b.converted)?2:0}px;background:#3b82f6;border-radius:2px"></div>
   </div>`).join('');
   grid+=`<div class="box"><h3>试用转化时序（按签发周 · <span style="color:#3b82f6">签发</span>/<span style="color:#25b566">转正</span>）</h3>
      <div style="display:flex;gap:3px;align-items:flex-end">${bars}</div>
      <div class="muted" style="font-size:12px;margin-top:4px">${wk[0].week} ~ ${wk[wk.length-1].week}（周一锚）</div></div>`;
  }
 }
 document.getElementById('cards').innerHTML=cards;
 document.getElementById('grid').innerHTML=grid;
}).catch(e=>{document.getElementById('cards').innerHTML='<div class="muted">加载失败：'+e+'</div>';});
/* P14-3 客户健康度行级视图：回执按 anon_id 聚合成客户列表，点行下钻该客户失败组件 Top。
   从「统计面」到「客户面」——售后接电话先看这行，30 秒知道对面机器啥状态。 */
fetch('/api/customers').then(r=>r.json()).then(c=>{
 if(!c||!c.ok||!c.customers||!c.customers.length) return;
 const esc=s=>String(s).replace(/[&<>"]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]));
 const fmtT=ts=>ts?new Date(ts*1000).toLocaleDateString()+' '+new Date(ts*1000).toTimeString().slice(0,5):'-';
 const rows=c.customers.map((u,i)=>{
  const warn=u.sess_ok_pct<80||u.comp_ok_pct<80;
  const fails=Object.entries(u.top_fails||{}).map(([k,v])=>`${esc(k)}×${v}`).join('、')||'（无失败组件）';
  return `<div class="row" style="cursor:pointer" onclick="var d=document.getElementById('cd${i}');d.style.display=d.style.display==='none'?'block':'none'">
    <span title="${esc(u.anon_id)}">${warn?'⚠ ':''}${esc(String(u.anon_id).slice(0,10))}… · ${esc(u.edition)} · v${esc(u.version)}</span>
    <span class="n" style="color:${warn?'#e0574a':'#25b566'}">会话${u.sess_ok_pct}% · 组件${u.comp_ok_pct}%</span></div>
   <div id="cd${i}" style="display:none;padding:4px 8px;background:#0d0f13;border-radius:6px;margin:2px 0 6px;font-size:12px" class="muted">
    最近活跃 ${fmtT(u.last_ts)} · ${esc(u.platform)} · ${esc(u.gpu)} · ${esc(u.channel)} 通道 · 回执 ${u.receipts} 份<br>失败组件：${fails}</div>`;
 }).join('');
 document.getElementById('grid').innerHTML+=
  `<div class="box"><h3>客户健康度（${c.customers.length}/${c.total} 台 · 按最近活跃排 · 点行下钻）</h3>${rows}</div>`;
}).catch(()=>{});
</script></body></html>"""


# ── 订单存储 ─────────────────────────────────────────────────────────
def _load_orders(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        d.setdefault("codes", {})
        return d
    except Exception:
        return {"codes": {}}


def _save_orders(path: Path, orders: dict):
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(orders, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _load_sk(path: Path = SK_FILE):
    if not lic._HAVE_CRYPTO:
        print("[错误] 未安装 cryptography，无法签发。`pip install cryptography`")
        sys.exit(2)
    if not path.exists():
        print(f"[错误] 未找到厂商私钥 {path}，请先 `python license_admin.py keygen`。")
        sys.exit(2)
    from cryptography.hazmat.primitives import serialization as ser
    return ser.load_pem_private_key(path.read_bytes(), password=None)


# ── 核心：签发一份绑定指纹的授权 ──────────────────────────────────────
def _sign_license(sk, *, machine: str, edition: str, licensee: str,
                  issued: int, expires: int, features: dict | None,
                  lic_id: str = "") -> dict:
    payload = {"v": 1, "machine": machine, "edition": edition,
               "licensee": licensee or "", "issued": int(issued), "expires": int(expires)}
    if lic_id:
        payload["lic_id"] = lic_id          # 唯一序列号：便于日后精确吊销单份授权
    if features:
        payload["features"] = features
    sig = sk.sign(lic.canonical_payload(payload)).hex()
    return {"payload": payload, "sig": sig, "alg": "Ed25519"}


def _norm_code(c: str) -> str:
    """兑换码归一化（容错匹配用）：去空白/连字符 + 大写。生成码字符集无歧义字符，归一后不损失区分度。"""
    return "".join(ch for ch in (c or "").upper() if ch.isalnum())


def activate(code: str, fingerprint: str) -> tuple[int, dict]:
    """处理一次激活；返回 (http_status, resp)。线程安全。
    P11 输码容错：精确命中优先（自定义码大小写/符号原样有效）；未命中再按归一化
    （去空白去连字符+大写）兜底——客户小写手敲/漏敲连字符/带空格粘贴都能激活。"""
    code = (code or "").strip()
    fp = (fingerprint or "").strip()
    if not code or not fp:
        return 400, {"ok": False, "error": "缺少 code 或 fingerprint。"}
    with _LOCK:
        orders = _load_orders(_STATE["orders_path"])
        rec = orders["codes"].get(code)
        if not rec:
            want = _norm_code(code)
            hit = next((k for k in orders["codes"] if _norm_code(k) == want), None) if want else None
            if hit is not None:
                code, rec = hit, orders["codes"][hit]
        if not rec:
            return 404, {"ok": False, "error": "兑换码无效或不存在。"}
        if rec.get("disabled"):
            return 403, {"ok": False, "error": "兑换码已停用。"}
        acts = rec.setdefault("activations", [])
        existing = next((a for a in acts if a.get("fingerprint") == fp), None)
        if existing is None and len(acts) >= int(rec.get("seats", 1)):
            return 409, {"ok": False,
                         "error": f"兑换码激活名额已用尽（{len(acts)}/{rec.get('seats',1)} 座）。"}
        now = int(time.time())
        is_new_seat = existing is None
        if existing is None:
            days = int(rec.get("days", 365))
            expires = 0 if days <= 0 else now + days * 86400
            existing = {"fingerprint": fp, "issued": now, "expires": int(expires),
                        "lic_id": _secrets.token_hex(8)}   # 稳定序列号（随激活记录持久化），便于精确吊销
            acts.append(existing)
            _save_orders(_STATE["orders_path"], orders)   # 仅新占座时落盘
        doc = _sign_license(
            _STATE["sk"], machine=fp, edition=rec.get("edition", "standard"),
            licensee=rec.get("licensee", ""), issued=existing["issued"],
            expires=existing["expires"], features=rec.get("features") or None,
            lic_id=existing.get("lic_id", ""))
    # P15-1 转化回推（锁外+后台线程：webhook 慢不拖累激活响应；仅 serve 模式外发）
    if is_new_seat and _STATE.get("notify_activate"):
        threading.Thread(target=_notify_activation, daemon=True, name="act-notify",
                         args=({"fp": fp, "code": code, "via": rec.get("via", ""),
                                "edition": rec.get("edition", ""), "days": rec.get("days", ""),
                                "issued": existing["issued"]},)).start()
    try:   # 签发即导出：激活成功追加台账 outbox（ledger_outbox 静默钩子，绝不影响激活）
        import ledger_outbox as _lo
        _lo.record_issue(_lo.normalize_from_activation(code, rec, existing),
                         outbox_path=Path(_STATE["orders_path"]).parent / "ledger_outbox.jsonl")
    except Exception:
        pass
    return 200, {"ok": True, "license": doc}


# ── 在线刷新（管理后台续费/升档后，客户端按指纹拉取最新已签授权）───────────────────
# 定位：把「客服在后台改了某机的到期/档位 → 客户还得重输码」自动化为「客户端按指纹拉一下即生效」。
# 只读重签：不占新座、不改台账，把该指纹在订单库里的【当前应得授权】原样重签回传；
# 客户端 refresh_online 再走单调闸门（只升不降）落盘。管理动作(改台账)见 cmd_renew。
def _entitlement_better(a: dict, b: dict) -> bool:
    """授权候选 a 是否优于 b：到期更晚(0=永久=+inf)为主，档位更高为次。"""
    ea = float("inf") if int(a.get("expires", 0) or 0) == 0 else float(a.get("expires", 0))
    eb = float("inf") if int(b.get("expires", 0) or 0) == 0 else float(b.get("expires", 0))
    if ea != eb:
        return ea > eb
    ra = lic._EDITION_RANK.get(str(a.get("edition", "")), -1)
    rb = lic._EDITION_RANK.get(str(b.get("edition", "")), -1)
    return ra > rb


def find_entitlement_for_fp(orders: dict, fp: str) -> "dict | None":
    """遍历订单库找该指纹当前「最优」授权（同机多次激活=续费取最晚到期/升档取最高档）。
    停用码(disabled)跳过。返回 {edition, expires, issued, features, licensee, lic_id, code} 或 None。"""
    best = None
    for code, rec in (orders.get("codes") or {}).items():
        if rec.get("disabled"):
            continue
        for a in rec.get("activations", []) or []:
            if a.get("fingerprint") != fp:
                continue
            cand = {"edition": rec.get("edition", "standard"),
                    "expires": int(a.get("expires", 0) or 0),
                    "issued": int(a.get("issued", 0) or 0),
                    "features": rec.get("features") or None,
                    "licensee": rec.get("licensee", ""),
                    "lic_id": a.get("lic_id", ""), "code": code}
            if best is None or _entitlement_better(cand, best):
                best = cand
    return best


def refresh(fingerprint: str, lic_id: str = "") -> tuple[int, dict]:
    """按指纹重签当前应得授权（只读，不占座/不改台账）。线程安全。
    找不到该指纹的任何激活记录 → 404（未激活/已被移除，客户端据此保持现状不动）。"""
    fp = (fingerprint or "").strip()
    if not fp:
        return 400, {"ok": False, "error": "缺少 fingerprint。"}
    with _LOCK:
        orders = _load_orders(_STATE["orders_path"])
        ent = find_entitlement_for_fp(orders, fp)
    if not ent:
        return 404, {"ok": False, "error": "该机器无有效授权记录（未激活或已被移除）。"}
    doc = _sign_license(
        _STATE["sk"], machine=fp, edition=ent["edition"], licensee=ent["licensee"],
        issued=ent["issued"], expires=ent["expires"], features=ent["features"],
        lic_id=ent["lic_id"])
    return 200, {"ok": True, "license": doc, "code": ent["code"]}


# ── P10 一键试用升级：免兑换码签发限时旗舰试授权（销售演示/自助尝鲜）────────────
def _load_trials(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        d.setdefault("fps", {})
        return d
    except Exception:
        return {"fps": {}}


def _save_trials(path: Path, trials: dict):
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(trials, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def trial_upgrade(fingerprint: str) -> tuple[int, dict]:
    """P10 一键试用升级：免兑换码按指纹签发限时 pro 试签（销售演示秒切档位）。
    护栏：一机终身一次——有效期内重复请求幂等重发同一份（按钮多点不炸、剩余天数照旧，
    杜绝「删 license.key 再点一次=无限续试」）；到期后再请求即拒（试用不可续，走正式兑换码）。
    serve --trial-days 0 可整体关闭。返回 (http_status, resp)。线程安全。"""
    fp = (fingerprint or "").strip()
    if not fp or len(fp) > 128:
        return 400, {"ok": False, "error": "缺少或非法 fingerprint。"}
    days = int(_STATE.get("trial_days", 7))
    if days <= 0:
        return 403, {"ok": False, "error": "本服务未开放试用升级（--trial-days 0）。"}
    with _LOCK:
        trials = _load_trials(_STATE["trials_path"])
        rec = trials["fps"].get(fp)
        now = int(time.time())
        if rec and now >= int(rec.get("expires", 0)):
            return 403, {"ok": False,
                         "error": "本机已用过试用升级（已到期）。如需正式授权请联系厂商获取兑换码。"}
        if rec is None:
            rec = {"issued": now, "expires": now + days * 86400,
                   "lic_id": "trial-" + _secrets.token_hex(8)}
            trials["fps"][fp] = rec
            _save_trials(_STATE["trials_path"], trials)
        doc = _sign_license(
            _STATE["sk"], machine=fp, edition="pro", licensee="试用升级（自动签发）",
            issued=rec["issued"], expires=rec["expires"], features=None,
            lic_id=rec.get("lic_id", ""))
    try:   # 签发即导出：试用签发追加台账 outbox（ledger_outbox 静默钩子，绝不影响签发）
        import ledger_outbox as _lo
        _lo.record_issue(_lo.normalize_from_trial(fp, rec),
                         outbox_path=Path(_STATE["trials_path"]).parent / "ledger_outbox.jsonl")
    except Exception:
        pass
    left = max(0, int((rec["expires"] - now) / 86400))
    return 200, {"ok": True, "license": doc, "trial": True, "days_left": left}


# ── HTTP ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "AvatarHubActivation/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[activation] %s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, html: str):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        _parsed = urllib.parse.urlparse(self.path)
        path = _parsed.path.rstrip("/")
        if path == "/quickissue":
            # P14 一键发码确认页（销售从临期 webhook 点进来）：验签 → 一键出码 → 复制发客户
            q = urllib.parse.parse_qs(_parsed.query)
            fp = (q.get("fp") or [""])[0]
            exp = int((q.get("exp") or ["0"])[0] or 0)
            sig = (q.get("sig") or [""])[0]
            okv, err = qi_verify(fp, exp, sig)
            if not okv:
                self._send_html(400, "<meta charset=utf-8><h3>链接无效：%s</h3>" % err)
                return
            # P15-1 漏斗第二级「链接点开」：每指纹只记首开时刻（幂等，体积有界=临期通知数）
            try:
                with _LOCK:
                    orders = _load_orders(_STATE["orders_path"])
                    if fp not in orders.setdefault("qi_opened", {}):
                        orders["qi_opened"][fp] = int(time.time())
                        _save_orders(_STATE["orders_path"], orders)
            except Exception:
                pass
            self._send_html(200, _QI_HTML.replace("__FP__", fp)
                            .replace("__EXP__", str(exp)).replace("__SIG__", sig)
                            .replace("__ED__", str(_STATE.get("qi_edition", "pro")))
                            .replace("__DAYS__", str(_STATE.get("qi_days", 365))))
            return
        if path == "/api/health":
            self._send(200, {"ok": True, "service": "activation", "ts": int(time.time())})
        elif path == "/api/telemetry/summary":
            self._send(200, aggregate_telemetry(_STATE["telemetry_path"]))
        elif path == "/api/funnel":
            # P12 试用转化漏斗（台账即真相，无客户端埋点）：看板/运营脚本直接取
            # P13 附 weekly 转化时序（按签发周）· P15-1 附 quickissue 四级漏斗（通知→点开→出码→激活）
            with _LOCK:
                orders = _load_orders(_STATE["orders_path"])
                trials = _load_trials(_STATE["trials_path"])
            self._send(200, {"ok": True, **funnel_stats(orders, trials),
                             "weekly": funnel_weekly(orders, trials),
                             "quickissue": qi_funnel(orders, trials)})
        elif path == "/api/customers":
            # P14-3 客户健康度（按 anon_id 聚合的行级视图），看板/运营脚本直接取
            self._send(200, customers_view(_STATE["telemetry_path"]))
        elif path == "/api/revocations":
            # 分发厂商已签名的吊销名单（产品可选在线拉取；离线交付则直接放产品目录）。
            try:
                doc = json.loads(lic.REVOCATION_FILE.read_text(encoding="utf-8"))
            except Exception:
                doc = {"payload": {"v": 1, "revoked": []}, "sig": ""}
            self._send(200, doc)
        elif path in ("", "/dashboard"):
            self._send_html(200, _DASHBOARD_HTML)
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def _read_json(self):
        """读请求体为 JSON；超 MAX_BODY 直接拒（返回 None 并已回 413）。"""
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > MAX_BODY:
            self._send(413, {"ok": False, "error": "请求体过大。"})
            return None, False
        try:
            return (json.loads(self.rfile.read(n).decode("utf-8")) if n else {}), True
        except Exception:
            self._send(400, {"ok": False, "error": "请求体不是合法 JSON。"})
            return None, False

    def do_POST(self):
        path = self.path.rstrip("/")
        # 反滥用限速：签发/激活/试用/发码等未鉴权面（防刷、防签发振荡放大）。回环与内网可用 --rate-max-ip 0 放开。
        if path in ("/api/activate", "/api/refresh", "/api/trial", "/api/trial_upgrade", "/api/quickissue"):
            ip = self.client_address[0] if self.client_address else "?"
            if not _rate_check("ip:" + ip, time.time(),
                               int(_STATE.get("rate_window", 60)), int(_STATE.get("rate_max_ip", 30))):
                self._send(429, {"ok": False, "error": "请求过于频繁，请稍后再试。"})
                return
        if path == "/api/activate":
            data, ok = self._read_json()
            if not ok:
                return
            status, resp = activate(data.get("code", ""), data.get("fingerprint", ""))
            self._send(status, resp)
        elif path in ("/api/trial", "/api/trial_upgrade"):   # 别名并存：老客户端/文档两种叫法都接
            data, ok = self._read_json()
            if not ok:
                return
            status, resp = trial_upgrade(data.get("fingerprint", ""))
            self._send(status, resp)
        elif path == "/api/refresh":
            # 按指纹拉取当前应得授权（管理后台续费/升档后，客户端 refresh_online 调此拉新）
            data, ok = self._read_json()
            if not ok:
                return
            status, resp = refresh(data.get("fingerprint", ""), data.get("lic_id", ""))
            self._send(status, resp)
        elif path == "/api/telemetry":
            data, ok = self._read_json()
            if not ok:
                return
            status, resp = record_telemetry(data)
            self._send(status, resp)
        elif path == "/api/quickissue":
            # P14 一键发码：验签后出正式兑换码（同指纹幂等，未激活不重发）
            data, ok = self._read_json()
            if not ok:
                return
            fp = str(data.get("fp", "")).strip()
            exp = int(data.get("exp", 0) or 0)
            sig = str(data.get("sig", "")).strip()
            okv, err = qi_verify(fp, exp, sig)
            if not okv:
                self._send(403, {"ok": False, "error": err})
                return
            with _LOCK:
                orders = _load_orders(_STATE["orders_path"])
                code, reused = qi_issue(orders, fp, _STATE.get("qi_edition", "pro"),
                                        int(_STATE.get("qi_days", 365)))
                if not reused:
                    _save_orders(_STATE["orders_path"], orders)
            sys.stderr.write(f"[activation] quickissue {'复用' if reused else '新发'}: {code} → 指纹 {fp}\n")
            self._send(200, {"ok": True, "code": code, "reused": reused,
                             "edition": _STATE.get("qi_edition", "pro"),
                             "days": int(_STATE.get("qi_days", 365))})
        else:
            self._send(404, {"ok": False, "error": "not found"})


# ── CLI ──────────────────────────────────────────────────────────────
def _gen_code() -> str:
    block = lambda: "".join(_secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(4))
    return "AVH-%s-%s-%s" % (block(), block(), block())


def cmd_addcode(args):
    feats = {}
    for kv in (args.feature or []):
        if "=" not in kv:
            print(f"[警告] 忽略非法 --feature {kv}")
            continue
        k, v = kv.split("=", 1)
        vl = v.strip().lower()
        feats[k.strip()] = (vl == "true") if vl in ("true", "false") else (
            int(vl) if vl.lstrip("-").isdigit() else v.strip())
    orders = _load_orders(ORDERS_FILE)
    code = args.code or _gen_code()
    if code in orders["codes"]:
        print(f"[错误] 兑换码已存在：{code}")
        sys.exit(2)
    orders["codes"][code] = {
        "edition": args.edition, "days": args.days, "seats": args.seats,
        "licensee": args.licensee or "", "features": feats or {},
        "created": int(time.time()), "activations": [],
    }
    _save_orders(ORDERS_FILE, orders)
    exp = "永久" if args.days <= 0 else f"{args.days} 天"
    print(f"[完成] 兑换码：{code}")
    print(f"  档位 {args.edition} · 有效 {exp} · {args.seats} 座 · 被授权方 {args.licensee or '-'}"
          + (f" · 覆盖能力 {feats}" if feats else ""))


def cmd_listcodes(args):
    orders = _load_orders(ORDERS_FILE)
    if not orders["codes"]:
        print("(无兑换码)")
        return
    for code, rec in orders["codes"].items():
        used = len(rec.get("activations", []))
        print(f"{code}  {rec.get('edition'):8s} days={rec.get('days')} seats={used}/{rec.get('seats')}"
              f"  {rec.get('licensee') or '-'}" + ("  [停用]" if rec.get('disabled') else ""))


def cmd_renew(args):
    """管理动作：给某台已激活机器续费/升档（改订单库里该指纹的激活到期/所在码的档位）。
    定位：--lic-id 精确到某份 / --fingerprint 取该机最优激活。改完客户端 refresh_online 即「只升不降」拉取生效。
      --days N   续到「今起 N 天」；配 --extend 则在「现有到期」基础上顺延 N 天（<=0 = 永久）。
      --edition  同时改该激活所在兑换码的档位（升/降档；影响该码全部座位，通常 1 座）。"""
    if not (args.lic_id or args.fingerprint):
        print("[错误] 需指定 --lic-id 或 --fingerprint 定位要续费的激活记录。")
        sys.exit(2)
    with _LOCK:
        orders = _load_orders(ORDERS_FILE)
        found = None
        for code, rec in (orders.get("codes") or {}).items():
            cands = [a for a in rec.get("activations", []) or []
                     if (args.lic_id and a.get("lic_id") == args.lic_id)
                     or (args.fingerprint and a.get("fingerprint") == args.fingerprint)]
            for a in cands:
                if found is None or _entitlement_better(
                        {"expires": a.get("expires", 0), "edition": rec.get("edition", "")},
                        {"expires": found[2].get("expires", 0), "edition": found[1].get("edition", "")}):
                    found = (code, rec, a)
        if found is None:
            print("[错误] 未找到匹配的激活记录（--lic-id / --fingerprint）。")
            sys.exit(2)
        code, rec, a = found
        old_exp, old_ed = int(a.get("expires", 0) or 0), rec.get("edition", "")
        if args.days is not None:
            if args.days <= 0:
                a["expires"] = 0
            else:
                base = max(old_exp, int(time.time())) if args.extend and old_exp else int(time.time())
                a["expires"] = base + args.days * 86400
        if args.edition:
            rec["edition"] = args.edition
        _save_orders(ORDERS_FILE, orders)
    fmt = lambda e: "永久" if not e else time.strftime("%Y-%m-%d", time.localtime(e))
    print(f"[完成] 已续费 · 码 {code} · lic_id {a.get('lic_id', '-')} · 指纹 {a.get('fingerprint', '-')[:16]}…")
    print(f"  档位 {old_ed} → {rec.get('edition')}   到期 {fmt(old_exp)} → {fmt(int(a.get('expires', 0) or 0))}")
    print("  客户机下次「刷新授权」（或 Hub 周期刷新 ≤ 数小时）即自动生效（只升不降）。")


def cmd_whois(args):
    """按机器指纹查该机「当前应得授权 + 全部激活/试用记录」——售后接电话/后台核对的第一入口。
    与客户端 summary().refresh（本机视角）互为两端：这里是后台视角「这台机该有什么」。
    CLI-only（不做 HTTP 端点）：按指纹反查谁持有什么是敏感信息，不在参考服务上开放未鉴权查询。"""
    fp = (args.fingerprint or "").strip()
    if not fp:
        print("[错误] 需 --fingerprint。")
        sys.exit(2)
    fmt = lambda e: "永久" if not int(e or 0) else time.strftime("%Y-%m-%d %H:%M", time.localtime(int(e)))
    orders = _load_orders(ORDERS_FILE)
    ent = find_entitlement_for_fp(orders, fp)
    print(f"机器指纹 {fp}")
    if ent:
        now = int(time.time())
        state = "有效" if (not ent["expires"] or now < ent["expires"]) else "已过期"
        print(f"  ▶ 当前应得：{ent['edition']} · 到期 {fmt(ent['expires'])}（{state}）"
              f" · 码 {ent['code']} · lic_id {ent['lic_id']}")
    else:
        print("  ▶ 当前应得：无（该指纹无任何兑换码激活记录）")
    hits = [(code, rec, a) for code, rec in (orders.get("codes") or {}).items()
            for a in (rec.get("activations") or []) if a.get("fingerprint") == fp]
    if hits:
        print(f"  兑换码激活记录（{len(hits)}）：")
        for code, rec, a in sorted(hits, key=lambda x: int(x[2].get("expires", 0) or 0), reverse=True):
            flag = "  [码停用]" if rec.get("disabled") else ""
            print(f"    · 码 {code} [{rec.get('edition')}] issued {fmt(a.get('issued'))}"
                  f" expires {fmt(a.get('expires'))} lic_id {a.get('lic_id', '-')}{flag}")
    tr = _load_trials(TRIALS_FILE).get("fps", {}).get(fp)
    if tr:
        now = int(time.time())
        st = "试用中" if now < int(tr.get("expires", 0)) else "已到期"
        print(f"  试用升级：issued {fmt(tr.get('issued'))} expires {fmt(tr.get('expires'))}"
              f"（{st}） lic_id {tr.get('lic_id', '-')}")
    print("  提示：续费/升档用 `renew --fingerprint … --days … [--edition …]`；"
          "改完客户端「刷新授权」或 Hub 周期刷新即「只升不降」生效。")


def cmd_listtrials(args):
    """P12 试签台账查询：每机一行（指纹/签发/到期/状态），运维接手不用手翻 JSON。"""
    trials = _load_trials(TRIALS_FILE)
    fps = trials.get("fps", {})
    if not fps:
        print("(无试用记录)")
        return
    now = int(time.time())
    fmt = lambda ts: time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    for fp, rec in sorted(fps.items(), key=lambda kv: kv[1].get("issued", 0), reverse=True):
        exp = int(rec.get("expires", 0))
        state = "试用中" if now < exp else "已到期"
        left = max(0, int((exp - now) / 86400))
        print(f"{fp}  签发 {fmt(rec.get('issued', 0))}  到期 {fmt(exp)}"
              f"  {state}" + (f"(剩 {left} 天)" if now < exp else "") + f"  {rec.get('lic_id', '')}")


def funnel_stats(orders: dict, trials: dict) -> dict:
    """P12 试用转化漏斗（纯函数，可单测）：厂商台账自身就是地面真相——
    trials.fps（谁领过试签）与 orders.codes[*].activations[*].fingerprint（谁用码转正）
    按指纹连接即得转化，零客户端埋点/零新增遥测（客户端上报既不可信也没必要）。
    「转正」口径：该指纹存在任一激活时间 >= 试签签发时间的兑换码激活记录。"""
    fps = (trials or {}).get("fps", {}) or {}
    acts: dict = {}                       # fp -> [兑换码激活时间]（activations[*].issued）
    for rec in ((orders or {}).get("codes", {}) or {}).values():
        for a in rec.get("activations", []) or []:
            f = a.get("fingerprint", "")
            if f:
                acts.setdefault(f, []).append(int(a.get("issued", 0) or 0))
    now = int(time.time())
    issued = len(fps)
    active = sum(1 for r in fps.values() if now < int(r.get("expires", 0)))
    expired = issued - active
    converted = sum(1 for f, r in fps.items()
                    if any(ts >= int(r.get("issued", 0)) for ts in acts.get(f, [])))
    return {"trial_issued": issued, "trial_active": active, "trial_expired": expired,
            "converted": converted,
            "conversion_pct": round(converted / issued * 100, 1) if issued else 0.0}


def funnel_weekly(orders: dict, trials: dict, weeks: int = 8, now: "int|None" = None) -> list:
    """P13 转化时序（纯函数）：按试签「签发周」（周一锚）分桶——本周签的试用转化率 vs 上周，
    一眼看出转化在变好还是变坏（存量切面 funnel_stats 看不出趋势）。
    每桶：{week, issued, converted, pct}；补齐空周保证连续；旧→新排序。
    转正口径与 funnel_stats 同源：该指纹存在激活时间 >= 试签签发的兑换码激活。"""
    import datetime as _dt
    now = int(now or time.time())
    fps = (trials or {}).get("fps", {}) or {}
    acts: dict = {}
    for rec in ((orders or {}).get("codes", {}) or {}).values():
        for a in rec.get("activations", []) or []:
            f = a.get("fingerprint", "")
            if f:
                acts.setdefault(f, []).append(int(a.get("issued", 0) or 0))
    monday = lambda ts: (_dt.date.fromtimestamp(ts) -
                         _dt.timedelta(days=_dt.date.fromtimestamp(ts).weekday()))
    buckets: dict = {}
    for f, r in fps.items():
        iss = int(r.get("issued", 0) or 0)
        if not iss:
            continue
        wk = monday(iss)
        b = buckets.setdefault(wk, {"issued": 0, "converted": 0})
        b["issued"] += 1
        if any(ts >= iss for ts in acts.get(f, [])):
            b["converted"] += 1
    this_wk = monday(now)
    out = []
    for i in range(weeks - 1, -1, -1):
        wk = this_wk - _dt.timedelta(weeks=i)
        b = buckets.get(wk, {"issued": 0, "converted": 0})
        out.append({"week": wk.strftime("%Y-%m-%d"), "issued": b["issued"], "converted": b["converted"],
                    "pct": round(b["converted"] / b["issued"] * 100, 1) if b["issued"] else 0.0})
    return out


def cmd_stats(args):
    """P12 商业漏斗一屏：试签发放/在试/到期/转正 + 兑换码消耗，商业决策有数可依。
    P13 追加按签发周的转化时序（趋势判断）。"""
    orders = _load_orders(ORDERS_FILE)
    trials = _load_trials(TRIALS_FILE)
    f = funnel_stats(orders, trials)
    print("── 试用转化漏斗（按机器指纹连接 trials ↔ activations）──")
    print(f"  发出试签 {f['trial_issued']} 台 · 试用中 {f['trial_active']} · 已到期 {f['trial_expired']}"
          f" · 转正 {f['converted']} 台（转化率 {f['conversion_pct']}%）")
    wk = funnel_weekly(orders, trials, weeks=int(getattr(args, "weeks", 8) or 8))
    if any(b["issued"] for b in wk):
        print("── 转化时序（按签发周，周一锚）──")
        for b in wk:
            bar = "█" * min(30, b["issued"])
            print(f"  {b['week']} 周  签发 {b['issued']:3d}  转正 {b['converted']:3d}"
                  f"（{b['pct']}%）  {bar}")
    qf = qi_funnel(orders, trials)
    if qf["notified"] or qf["issued"]:
        print("── 一键发码漏斗（临期通知 → 链接点开 → 出码 → 激活）──")
        print(f"  通知 {qf['notified']} → 点开 {qf['opened']}（{qf['open_pct']}%）"
              f" → 出码 {qf['issued']}（{qf['issue_pct']}%）"
              f" → 激活 {qf['activated']}（{qf['activate_pct']}%）")
    codes = orders.get("codes", {})
    total_seats = sum(int(r.get("seats", 1)) for r in codes.values())
    used_seats = sum(len(r.get("activations", [])) for r in codes.values())
    by_ed: dict = {}
    for r in codes.values():
        by_ed[r.get("edition", "?")] = by_ed.get(r.get("edition", "?"), 0) + 1
    print("── 兑换码 ──")
    print(f"  共 {len(codes)} 个（{'/'.join(f'{k}×{v}' for k, v in sorted(by_ed.items())) or '-'}）"
          f" · 座位 {used_seats}/{total_seats} 已用")


# ── P13 临期主动通知：销售跟进从「客户开页面才见横幅」变「厂商先知道」──────────
def scan_expiring_trials(trials: dict, now: "int|None" = None, window_h: int = 48) -> list:
    """纯函数：找出「还剩 0<t<=window_h 小时到期且未通知过」的试签，打 notified_48h 标记。
    返回命中列表 [{fp, expires, left_h}]；调用方负责持久化 trials。幂等：标记后不再命中。"""
    now = int(now or time.time())
    hit = []
    for fp, rec in ((trials or {}).get("fps", {}) or {}).items():
        exp = int(rec.get("expires", 0) or 0)
        if not exp or rec.get("notified_48h"):
            continue
        left = exp - now
        if 0 < left <= window_h * 3600:
            rec["notified_48h"] = now
            hit.append({"fp": fp, "expires": exp, "left_h": round(left / 3600, 1)})
    return hit


def _notify_expiring(hits: list):
    """临期试签 → alerts webhook（厂商机自己的 secrets/alert_webhooks.txt / 环境变量配置）。
    alerts 缺失/无 webhook 时静默降级为仅 stderr——通知是增益不是依赖。
    P14：文案附签名「一键发码」链接，销售点开即出正式码（知道了→办完了）。"""
    for h in hits:
        msg = (f"试用将于 {time.strftime('%m-%d %H:%M', time.localtime(h['expires']))} 到期"
               f"（剩 {h['left_h']}h）· 指纹 {h['fp']}")
        base = _STATE.get("public_base") or ""
        if base:
            try:
                msg += f" · 一键发码: {qi_link(h['fp'], base)}"
            except Exception:
                pass
        sys.stderr.write(f"[activation] 临期跟进: {msg}\n")
        try:
            import alerts
            alerts.notify_event("试用临期·建议跟进转化", detail=msg, level="info",
                                source="license_server/试用")
        except Exception:
            pass


def _expiry_watch_loop(interval_s: int = 3600):
    """serve 伴生线程：每小时扫一轮临期试签（daemon，随主进程退出）。"""
    while True:
        try:
            with _LOCK:
                trials = _load_trials(_STATE["trials_path"])
                hits = scan_expiring_trials(trials)
                if hits:
                    _save_trials(_STATE["trials_path"], trials)   # 标记落盘（先记后发，重启不重发）
            if hits:
                _notify_expiring(hits)
        except Exception as e:
            sys.stderr.write(f"[activation] 临期扫描异常(忽略): {e}\n")
        time.sleep(interval_s)


# ── P15-2 客户健康度告警化：看板要人来看，告警自己找人 ─────────────────────
def _load_json_dict(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_json_dict(path: Path, d: dict):
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def scan_unhealthy_customers(rows: list, notified: dict, now: "int|None" = None,
                             th: float = 80.0, min_receipts: int = 3,
                             recent_days: int = 7) -> list:
    """纯函数：从客户健康行（customers_view 输出）里挑「最近活跃 + 回执量足 + 成功率破线 +
    今日未提醒」的客户，打当日标记（notified[anon_id]='YYYY-MM-DD'）。返回命中行；
    调用方负责持久化 notified。口径与看板 ⚠ 同源（会话或组件成功率 < th）；
    min_receipts 挡单条坏回执的误报；recent_days 挡「早已停用的死机」永远报警。"""
    now = int(now or time.time())
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    hits = []
    for u in rows or []:
        k = u.get("anon_id") or "-"
        if k == "-":
            continue                                   # 无法分行的老回执桶：没有回访对象
        if int(u.get("receipts", 0)) < min_receipts:
            continue
        if now - int(u.get("last_ts", 0) or 0) > recent_days * 86400:
            continue
        if (float(u.get("sess_ok_pct", 100)) >= th
                and float(u.get("comp_ok_pct", 100)) >= th):
            continue
        if notified.get(k) == today:
            continue                                   # 每客户每日至多一次
        notified[k] = today
        hits.append(u)
    return hits


def _notify_unhealthy(hits: list):
    """健康破线客户 → alerts webhook（缺 alerts/无 webhook 静默降级 stderr，通知是增益不是依赖）。"""
    for u in hits:
        fails = "、".join(f"{k}×{v}" for k, v in (u.get("top_fails") or {}).items()) or "-"
        msg = (f"{str(u.get('anon_id'))[:12]}… 会话 {u.get('sess_ok_pct')}% · "
               f"组件 {u.get('comp_ok_pct')}%（回执 {u.get('receipts')} 份）· "
               f"{u.get('edition', '-')} v{u.get('version', '-')} · 失败组件: {fails}")
        sys.stderr.write(f"[activation] 健康破线: {msg}\n")
        try:
            import alerts
            alerts.notify_event("客户健康度破线·建议回访", detail=msg, level="warn",
                                source="license_server/健康")
        except Exception:
            pass


def _health_watch_loop(interval_s: int = 3600):
    """serve 伴生线程：每小时聚合一次遥测 → 破线客户推 webhook（每客户每日一次，标记先落盘再外发）。"""
    while True:
        try:
            th = float(_STATE.get("health_th", 80))
            if th > 0:
                rows = customers_view(_STATE["telemetry_path"], limit=500)["customers"]
                with _LOCK:
                    notified = _load_json_dict(_STATE["health_notify_path"])
                    hits = scan_unhealthy_customers(rows, notified, th=th)
                    if hits:
                        _save_json_dict(_STATE["health_notify_path"], notified)
            else:
                hits = []
            if hits:
                _notify_unhealthy(hits)
        except Exception as e:
            sys.stderr.write(f"[activation] 健康扫描异常(忽略): {e}\n")
        time.sleep(interval_s)


def cmd_customers(args):
    """P14-3 CLI 版客户健康度：售后不开浏览器也能一眼看客户机状态。"""
    d = customers_view(TELEMETRY_FILE, limit=int(args.limit))
    if not d["customers"]:
        print("(无遥测回执)")
        return
    for u in d["customers"]:
        t = time.strftime("%m-%d %H:%M", time.localtime(u["last_ts"])) if u["last_ts"] else "-"
        flag = "⚠" if (u["sess_ok_pct"] < 80 or u["comp_ok_pct"] < 80) else "·"
        fails = "、".join(f"{k}×{v}" for k, v in u["top_fails"].items()) or "-"
        print(f"{flag} {u['anon_id'][:12]:<14} {u['edition']:<8} v{u['version']:<10} "
              f"活跃 {t}  会话 {u['sess_ok_pct']}% 组件 {u['comp_ok_pct']}%  回执 {u['receipts']}  失败: {fails}")
    print(f"(共 {d['total']} 台，显示 {len(d['customers'])})")


def cmd_expiring(args):
    """CLI 只读版：列出 window 小时内到期的试签（不打标记，销售随手查）。"""
    trials = _load_trials(TRIALS_FILE)
    now = int(time.time())
    win = int(args.window) * 3600
    rows = [(fp, int(r.get("expires", 0)), bool(r.get("notified_48h")))
            for fp, r in trials.get("fps", {}).items()
            if 0 < int(r.get("expires", 0)) - now <= win]
    if not rows:
        print(f"({args.window}h 内无临期试签)")
        return
    for fp, exp, seen in sorted(rows, key=lambda x: x[1]):
        left = round((exp - now) / 3600, 1)
        print(f"{fp}  到期 {time.strftime('%Y-%m-%d %H:%M', time.localtime(exp))}"
              f"  剩 {left}h" + ("  [已通知]" if seen else ""))


def cmd_serve(args):
    _STATE["sk"] = _load_sk(Path(args.sk) if args.sk else SK_FILE)
    _STATE["orders_path"] = Path(args.orders) if args.orders else ORDERS_FILE
    _STATE["telemetry_path"] = Path(args.telemetry) if args.telemetry else TELEMETRY_FILE
    _STATE["trials_path"] = Path(args.trials) if args.trials else TRIALS_FILE
    _STATE["trial_days"] = int(args.trial_days)
    # P14 一键发码：临期 webhook 链接的可点地址与出码规格
    _STATE["public_base"] = (args.public_base or f"http://127.0.0.1:{args.port}").rstrip("/")
    _STATE["qi_edition"] = args.qi_edition
    _STATE["qi_days"] = int(args.qi_days)
    # P15-1 激活转化回推（quickissue 闭环/试用转正 → webhook）；--no-notify-activate 可关
    _STATE["notify_activate"] = not getattr(args, "no_notify_activate", False)
    # P15-2 客户健康度告警（0=关）；标记台账独立文件，不与订单/试用混写
    _STATE["health_th"] = float(args.health_th)
    _STATE["health_notify_path"] = HEALTH_NOTIFY_FILE
    _STATE["rate_window"] = int(getattr(args, "rate_window", 60))
    _STATE["rate_max_ip"] = int(getattr(args, "rate_max_ip", 30))
    if _STATE["trial_days"] > 0:
        threading.Thread(target=_expiry_watch_loop, daemon=True,
                         name="trial-expiry-watch").start()   # P13 临期跟进（试用关闭则不扫）
    if _STATE["health_th"] > 0:
        threading.Thread(target=_health_watch_loop, daemon=True,
                         name="customer-health-watch").start()   # P15-2 健康破线推送
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[激活服务] 监听 http://{args.host}:{args.port}  订单库 {_STATE['orders_path']}")
    _td = _STATE["trial_days"]
    print(f"  POST /api/activate  ·  POST /api/refresh(按指纹拉最新)  ·  POST /api/trial("
          + (f"试用升级 {_td} 天/机" if _td > 0 else "试用升级已关闭")
          + ")  ·  POST /api/telemetry  ·  GET /dashboard")
    _rl = int(_STATE["rate_max_ip"])
    _rw = int(_STATE["rate_window"])
    _rl_desc = "关闭" if _rl <= 0 else ("每 IP %d 次/%ds" % (_rl, _rw))
    print(f"  限速：{_rl_desc}（activate/refresh/trial/quickissue）")
    print(f"  试用台账 {_STATE['trials_path']} · 遥测落盘 {_STATE['telemetry_path']}"
          f"（make_release.py --telemetry-report 可直接聚合）· Ctrl-C 退出")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[激活服务] 已停止。")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # GBK 控制台防 ↔/· 等字符炸 print
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="AvatarHub 在线激活服务（厂商端）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ac = sub.add_parser("addcode", help="新增兑换码")
    ac.add_argument("--edition", default="standard", choices=["trial", "standard", "pro"])
    ac.add_argument("--days", type=int, default=365, help="有效天数，<=0 永久")
    ac.add_argument("--seats", type=int, default=1, help="可激活机器数")
    ac.add_argument("--licensee", default="")
    ac.add_argument("--feature", action="append", help="覆盖能力 k=v，可多次")
    ac.add_argument("--code", default="", help="指定码（默认随机生成）")

    sub.add_parser("listcodes", help="列出所有兑换码")

    rn = sub.add_parser("renew", help="续费/升档某台已激活机器（改台账，客户端刷新即生效）")
    rn.add_argument("--lic-id", dest="lic_id", default="", help="按授权序列号精确定位")
    rn.add_argument("--fingerprint", default="", help="按机器指纹定位（取该机最优激活）")
    rn.add_argument("--days", type=int, default=None, help="续到今起 N 天（<=0=永久）；配 --extend 则顺延")
    rn.add_argument("--extend", action="store_true", help="在现有到期基础上顺延（默认从今日重算）")
    rn.add_argument("--edition", default="", choices=["", "trial", "standard", "pro"],
                    help="同时改该激活所在码的档位（升/降档）")

    wi = sub.add_parser("whois", help="按机器指纹查当前应得授权 + 全部激活/试用记录（售后/核对）")
    wi.add_argument("--fingerprint", required=True, help="机器指纹")

    sub.add_parser("listtrials", help="列出试用升级台账（每机一行）")
    st = sub.add_parser("stats", help="商业漏斗：试签发放/在试/到期/转正 + 兑换码消耗 + 按周时序")
    st.add_argument("--weeks", type=int, default=8, help="转化时序回看周数（默认 8）")
    ex = sub.add_parser("expiring", help="列出即将到期的试签（销售跟进用，只读不打标记）")
    ex.add_argument("--window", type=int, default=48, help="临期窗口小时数（默认 48）")
    cu = sub.add_parser("customers", help="客户健康度行级视图（回执按 anon_id 聚合）")
    cu.add_argument("--limit", type=int, default=50, help="最多显示台数（默认 50）")

    sv = sub.add_parser("serve", help="启动激活 HTTP 服务")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8770)
    sv.add_argument("--sk", default="", help="私钥路径（默认 secrets/license_vendor_sk.pem）")
    sv.add_argument("--orders", default="", help="订单库路径（默认 secrets/orders.json）")
    sv.add_argument("--telemetry", default="", help="遥测回执 JSONL 路径（默认 secrets/telemetry.jsonl）")
    sv.add_argument("--trials", default="", help="试用台账路径（默认 secrets/trials.json）")
    sv.add_argument("--trial-days", type=int, default=TRIAL_DAYS,
                    help="试用升级天数/机；0=关闭试用端点（默认 %(default)s，AVATARHUB_TRIAL_UP_DAYS 可调）")
    sv.add_argument("--public-base", default="",
                    help="临期通知里「一键发码」链接的对外地址（默认 http://127.0.0.1:<port>）")
    sv.add_argument("--qi-edition", default="pro", help="一键发码档位（默认 %(default)s）")
    sv.add_argument("--qi-days", type=int, default=365, help="一键发码有效天数（默认 %(default)s）")
    sv.add_argument("--no-notify-activate", action="store_true",
                    help="关闭激活转化回推（默认开：quickissue 闭环/试用转正时 webhook 通知销售）")
    sv.add_argument("--health-th", type=float, default=80,
                    help="客户健康告警阈值%%：会话/组件成功率低于此值即推送（每客户每日一次；"
                         "0=关闭。默认 %(default)s）")
    sv.add_argument("--rate-window", type=int, default=60, help="限速窗口秒（默认 %(default)s）")
    sv.add_argument("--rate-max-ip", type=int, default=30,
                    help="每 IP 每窗口最多请求数：activate/refresh/trial/quickissue 共用；0=关闭（内网/回环）。默认 %(default)s")

    args = ap.parse_args()
    {"addcode": cmd_addcode, "listcodes": cmd_listcodes, "renew": cmd_renew,
     "whois": cmd_whois, "listtrials": cmd_listtrials, "stats": cmd_stats,
     "expiring": cmd_expiring, "customers": cmd_customers, "serve": cmd_serve}[args.cmd](args)


if __name__ == "__main__":
    main()
