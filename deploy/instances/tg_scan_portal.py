#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""Telegram 总机扫码门户 v2（实施30+：多实例统一入口 · 一次根治「扫了没反应」）。

相对旧 tg_backend_scan.py 的根治（保留）：
  1) 码永远新鲜 —— 后台驱动线程持续 start/poll，expired 自动换码；绝不扫到过期静态图。
  2) 状态实时可见 —— 页面每秒轮询 /state，等待/已扫/需两步验证/成功/换码 全程显示。
  3) 两步验证自助 —— 账号开云密码时页面直接输入 → 转交后端 /login/{id}/password。

v2 新增「门户再进一步」：
  - 多实例统一入口：首页列出所有可扫实例（智聊/通译…），点哪个扫哪个；
  - 懒启动：只有进入某实例扫码页才为它拉起登录会话（不空耗未选实例）；
  - 就绪探测：启动时探测各后端 protocol 通道，只列出就绪实例；
  - 向后兼容：仍支持 --base/--config 单实例模式（scan_tg.ps1 老用法不变）。

架构：独立本地 HTTP 服务（默认 127.0.0.1:18790），与浏览器同源（/state、/password 的
fetch 无 file:// CORS 限制）。只调实例既有登录 API（Bearer=web_admin.auth_token），
session/registry/编排器全由引擎管——扫成功即进统一收件箱。

用法：
  python deploy\instances\tg_scan_portal.py                      # 多实例（内置 zhiliao+tongyi，自动探测就绪）
  python deploy\instances\tg_scan_portal.py --instances zhiliao  # 只挂智聊
  python deploy\instances\tg_scan_portal.py --base http://127.0.0.1:18899 --config <cfg>  # 单实例（旧）
"""
from __future__ import annotations

import argparse
import base64
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# 内置实例登记（.117 chengjie 双实例；新增实例在此加一行即可）
DEFAULT_INSTANCES = {
    "zhiliao": {"label": "智聊 ChatX", "port": 18799,
                "config": r"D:\chengjie-instances\zhiliao\data\config\config.local.yaml",
                "out": r"D:\chengjie-instances\zhiliao\data\logs"},
    "tongyi": {"label": "通译 LingoX", "port": 18899,
               "config": r"D:\chengjie-instances\tongyi\data\config\config.local.yaml",
               "out": r"D:\chengjie-instances\tongyi\data\logs"},
}


def _read_token_from_config(cfg_path: Path) -> str:
    if not cfg_path.exists():
        return ""
    try:
        import yaml
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        for sect in ("web_admin", "web"):
            s = data.get(sect)
            if isinstance(s, dict) and s.get("auth_token"):
                return str(s["auth_token"])
    except Exception:
        pass
    try:
        for line in cfg_path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("auth_token:"):
                return s.split(":", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


class InstanceScanner:
    """单实例的扫码驱动（懒启动）：持续保持一个新鲜可扫的登录会话 + 2FA。"""

    def __init__(self, inst_id: str, label: str, base: str, token: str, out_dir: Path) -> None:
        self.id = inst_id
        self.label = label
        self.base = base.rstrip("/")
        self.token = token
        self.out_dir = out_dir
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._lock = threading.Lock()
        self._started = False
        self._stop = False
        self.login_id = ""
        self.status = "idle"     # idle|pending|password_needed|authorized|failed|error
        self.detail = ""
        self.account_id = ""
        self.qr_png = b""
        self.restarts = 0
        self.updated_at = 0.0

    def _api(self, path: str, method: str = "GET", body: dict | None = None) -> dict:
        req = urllib.request.Request(self.base + path, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        data = None
        if body is not None:
            req.add_header("Content-Type", "application/json")
            data = json.dumps(body).encode()
        with urllib.request.urlopen(req, data=data, timeout=40) as r:
            return json.loads(r.read().decode())

    def _set(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)
            self.updated_at = time.time()
        try:
            (self.out_dir / "tg_scan_state.json").write_text(
                json.dumps({"instance": self.id, "status": self.status,
                            "detail": self.detail, "account_id": self.account_id,
                            "restarts": self.restarts, "updated_at": self.updated_at},
                           ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _decode_qr(self, data_url: str) -> None:
        if data_url and "," in data_url:
            try:
                with self._lock:
                    self.qr_png = base64.b64decode(data_url.split(",", 1)[1])
            except Exception:
                pass

    def ensure_started(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        self.status = "pending"
        threading.Thread(target=self._run_driver, daemon=True).start()
        print(f"[portal] 实例 {self.id} 扫码驱动启动", flush=True)

    def submit_password(self, password: str) -> dict:
        if not self.login_id:
            return {"ok": False, "detail": "登录会话未就绪"}
        try:
            r = self._api(f"/api/platforms/telegram/login/{self.login_id}/password",
                          "POST", {"password": password})
            self._set(status=str(r.get("status") or self.status),
                      detail=str(r.get("detail") or ""),
                      account_id=str(r.get("account_id") or self.account_id))
            return r
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "detail": f"提交异常：{ex}"}

    def _run_driver(self) -> None:
        while not self._stop and self.status != "authorized":
            try:
                r = self._api("/api/platforms/telegram/login/start", "POST", {"mode": "protocol"})
            except Exception as ex:  # noqa: BLE001
                self._set(status="error", detail=f"发起登录失败（后端在跑？protocol 已开？）：{ex}")
                time.sleep(5)
                continue
            if not r.get("ok"):
                self._set(status="error",
                          detail="发起登录失败：" + json.dumps(r, ensure_ascii=False)[:160])
                time.sleep(5)
                continue
            self._decode_qr(str(r.get("qr_image") or ""))
            self._set(login_id=str(r.get("login_id") or ""),
                      status=str(r.get("status") or "pending"), detail="")
            while not self._stop:
                time.sleep(3)
                try:
                    s = self._api(f"/api/platforms/telegram/login/{self.login_id}/status")
                except Exception as ex:  # noqa: BLE001
                    self._set(detail=f"状态查询异常：{ex}")
                    continue
                st = str(s.get("status") or "")
                if s.get("qr_image"):
                    self._decode_qr(str(s.get("qr_image")))
                if st == "authorized":
                    self._set(status="authorized", detail="",
                              account_id=str(s.get("account_id") or ""))
                    print(f"[portal] {self.id} AUTHORIZED account_id={self.account_id}", flush=True)
                    return
                if st == "password_needed":
                    self._set(status="password_needed",
                              detail=str(s.get("detail") or "该账号开启两步验证，请输入云密码"))
                    if self.status == "authorized":
                        return
                    continue
                if st == "expired":
                    self.restarts += 1
                    self._set(status="pending", detail="二维码已过期，正在换新码…")
                    break
                if st == "failed":
                    self.restarts += 1
                    self._set(status="pending", detail="上一次登录失败，正在重试…")
                    time.sleep(2)
                    break
                self._set(status="pending")

    def snapshot(self) -> dict:
        with self._lock:
            return {"instance": self.id, "label": self.label, "status": self.status,
                    "detail": self.detail, "account_id": self.account_id,
                    "restarts": self.restarts, "has_qr": bool(self.qr_png)}


_LANDING = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>无界 · Telegram 扫码接入</title>
<style>*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;flex-direction:column;
align-items:center;justify-content:center;background:#0a0c1b;color:#e2e8f0;
font-family:'Microsoft YaHei',system-ui,sans-serif;padding:24px}
h2{margin:6px 0}.sub{color:#94a3b8;margin:0 0 22px;text-align:center}
.cards{display:flex;gap:16px;flex-wrap:wrap;justify-content:center}
.card{background:#121631;border:1px solid #1e2a4a;border-radius:16px;padding:22px 26px;
min-width:220px;cursor:pointer;transition:.15s;text-align:center;text-decoration:none;color:inherit}
.card:hover{border-color:#22d3ee;transform:translateY(-2px)}
.card h3{margin:0 0 6px;font-size:18px}.pill{font-size:12px;color:#93c5fd}
.st{margin-top:10px;font-size:13px}.on{color:#34d399}.off{color:#94a3b8}
</style></head><body>
<h2>无界 · Telegram 扫码接入</h2>
<p class="sub">选择要把 Telegram 号接入到哪个实例，然后用手机扫码。</p>
<div class="cards" id="cards"><span class="off">正在探测可用实例…</span></div>
<script>
async function load(){
  try{
    const d=await (await fetch('/instances')).json();
    const box=document.getElementById('cards');
    if(!d.instances||!d.instances.length){box.innerHTML='<span class="off">无可用实例（后端未就绪或 protocol 未开）</span>';return;}
    box.innerHTML=d.instances.map(i=>{
      const acct=i.account_id?('已接入 '+i.account_id):(i.status==='authorized'?'已接入':'未接入');
      return `<a class="card" href="/scan?inst=${encodeURIComponent(i.instance)}">
        <h3>${i.label}</h3><div class="pill">${i.instance} · :${i.port}</div>
        <div class="st ${i.status==='authorized'?'on':'off'}">${acct}</div></a>`;
    }).join('');
  }catch(e){document.getElementById('cards').innerHTML='<span class="off">门户连接中断</span>';}
}
load();setInterval(load,4000);
</script></body></html>"""


def _scan_page(inst_id: str, label: str) -> str:
    return ("""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__LABEL__ · 扫码接入</title>
<style>*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;flex-direction:column;
align-items:center;justify-content:center;background:#0a0c1b;color:#e2e8f0;
font-family:'Microsoft YaHei',system-ui,sans-serif}
a.back{position:fixed;top:16px;left:18px;color:#64748b;text-decoration:none;font-size:13px}
a.back:hover{color:#22d3ee}
h2{margin:12px 0 4px}.sub{color:#94a3b8;margin:0 0 16px;text-align:center;max-width:520px;padding:0 16px}
.card{background:#fff;padding:18px;border-radius:16px;position:relative;width:340px;height:340px;
display:flex;align-items:center;justify-content:center}
#qr{width:304px;height:304px;display:block}
.badge{margin-top:16px;font-size:15px;padding:8px 18px;border-radius:999px;background:#1e293b}
.ok{color:#22d3ee}.warn{color:#fbbf24}.err{color:#f87171}.suc{color:#34d399}
.pw{margin-top:18px;display:none;flex-direction:column;gap:10px;width:340px}
.pw input{padding:11px 13px;border-radius:10px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;font-size:15px}
.pw button{padding:11px;border:0;border-radius:10px;background:#22d3ee;color:#04222b;font-weight:700;font-size:15px;cursor:pointer}
.done{display:none;flex-direction:column;align-items:center;gap:8px}.done .big{font-size:64px}
.mask{position:absolute;inset:18px;border-radius:12px;background:rgba(255,255,255,.86);
display:none;align-items:center;justify-content:center;color:#0f172a;font-weight:700;text-align:center;padding:12px}
</style></head><body>
<a class="back" href="/">← 选择其他实例</a>
<h2>__LABEL__ · Telegram 扫码接入</h2>
<p class="sub">手机 Telegram → 设置 → 设备 → 连接桌面设备 → 扫下方二维码。二维码自动保持最新。</p>
<div class="card"><img id="qr" alt="二维码加载中…">
  <div class="mask" id="mask"></div>
  <div class="done" id="done"><div class="big">✅</div><div>登录成功，已接入</div>
    <div id="acct" style="color:#0f172a;font-weight:400"></div></div></div>
<div class="badge ok" id="badge">正在准备二维码…</div>
<div class="pw" id="pwbox">
  <div class="warn" style="text-align:center">该账号开启了两步验证，请输入云密码完成登录</div>
  <input id="pw" type="password" placeholder="Telegram 两步验证密码" autocomplete="off">
  <button id="pwbtn">提交密码</button></div>
<script>
const INST="__INST__";
let qrTimer=setInterval(()=>{document.getElementById('qr').src='/qr.png?inst='+INST+'&t='+Date.now()},2500);
document.getElementById('qr').src='/qr.png?inst='+INST+'&t='+Date.now();
const badge=document.getElementById('badge'),mask=document.getElementById('mask'),
  pwbox=document.getElementById('pwbox'),done=document.getElementById('done'),qr=document.getElementById('qr');
function setBadge(t,cls){badge.textContent=t;badge.className='badge '+(cls||'ok')}
async function tick(){
  try{
    const s=await (await fetch('/state?inst='+INST+'&t='+Date.now())).json();
    if(s.status==='authorized'){setBadge('登录成功','suc');clearInterval(qrTimer);qr.style.display='none';
      mask.style.display='none';pwbox.style.display='none';done.style.display='flex';
      document.getElementById('acct').textContent=s.account_id?('账号 '+s.account_id):'';return;}
    if(s.status==='password_needed'){setBadge('等待输入两步验证密码','warn');
      mask.style.display='flex';mask.textContent='已扫码 ✓ 待输入云密码';pwbox.style.display='flex';}
    else if(s.status==='error'){setBadge(s.detail||'后端未就绪','err');mask.style.display='none';pwbox.style.display='none';}
    else{setBadge(s.detail||'等待扫码…','ok');mask.style.display='none';pwbox.style.display='none';}
  }catch(e){setBadge('门户连接中断','err')}
  setTimeout(tick,1200);
}
document.getElementById('pwbtn').onclick=async function(){
  const v=document.getElementById('pw').value;if(!v)return;this.disabled=true;setBadge('正在校验云密码…','warn');
  try{const r=await (await fetch('/password?inst='+INST,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({password:v})})).json();
    if(r.ok){setBadge('密码正确，登录中…','suc')}else{setBadge(r.detail||'密码错误，请重试','err')}
  }catch(e){setBadge('提交失败，请重试','err')}
  this.disabled=false;document.getElementById('pw').value='';
};
tick();
</script></body></html>""".replace("__LABEL__", label).replace("__INST__", inst_id))


def make_handler(portal: "Portal"):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body.encode("utf-8") if isinstance(body, str) else body)

        def _inst(self):
            q = parse_qs(urlparse(self.path).query)
            return (q.get("inst") or [""])[0]

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                if portal.single_id:
                    self.send_response(302)
                    self.send_header("Location", f"/scan?inst={portal.single_id}")
                    self.end_headers()
                    return
                self._send(200, _LANDING)
            elif path == "/instances":
                self._send(200, json.dumps({"instances": portal.list_instances()},
                                           ensure_ascii=False), "application/json; charset=utf-8")
            elif path == "/scan":
                sc = portal.get(self._inst())
                if sc is None:
                    self._send(404, "unknown instance"); return
                sc.ensure_started()
                self._send(200, _scan_page(sc.id, sc.label))
            elif path == "/qr.png":
                sc = portal.get(self._inst())
                png = sc.qr_png if sc else b""
                if png:
                    self._send(200, png, "image/png")
                else:
                    self._send(404, b"", "image/png")
            elif path == "/state":
                sc = portal.get(self._inst())
                if sc is None:
                    self._send(404, json.dumps({"status": "error", "detail": "unknown instance"}),
                               "application/json; charset=utf-8"); return
                sc.ensure_started()
                self._send(200, json.dumps(sc.snapshot(), ensure_ascii=False),
                           "application/json; charset=utf-8")
            else:
                self._send(404, "not found")

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/password":
                sc = portal.get(self._inst())
                if sc is None:
                    self._send(404, json.dumps({"ok": False, "detail": "unknown instance"}),
                               "application/json; charset=utf-8"); return
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n).decode() or "{}")
                except Exception:
                    body = {}
                res = sc.submit_password(str(body.get("password") or ""))
                self._send(200, json.dumps(
                    {"ok": bool(res.get("ok") or res.get("status") == "authorized"),
                     "detail": str(res.get("detail") or "")}, ensure_ascii=False),
                    "application/json; charset=utf-8")
            else:
                self._send(404, "not found")
    return H


class Portal:
    def __init__(self, scanners: dict, single_id: str = "") -> None:
        self._scanners = scanners
        self.single_id = single_id

    def get(self, inst_id: str):
        return self._scanners.get(inst_id)

    def list_instances(self) -> list:
        out = []
        for sc in self._scanners.values():
            out.append({"instance": sc.id, "label": sc.label,
                        "port": urlparse(sc.base).port,
                        "status": sc.status, "account_id": sc.account_id})
        return out


def _probe_ready(base: str, token: str) -> bool:
    """探测后端 telegram protocol 通道是否就绪（可发起扫码）。"""
    try:
        req = urllib.request.Request(base.rstrip("/") + "/api/platforms/telegram/modes")
        req.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(req, timeout=12) as r:
            d = json.loads(r.read().decode())
        for m in (d.get("modes") or []):
            if m.get("mode") == "protocol" and m.get("available"):
                return True
    except Exception:
        return False
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--portal-port", type=int, default=18790)
    # 多实例模式（默认）：从内置 DEFAULT_INSTANCES 选
    ap.add_argument("--instances", default="zhiliao,tongyi",
                    help="逗号分隔的内置实例 id（默认 zhiliao,tongyi；仅列出 protocol 就绪的）")
    # 单实例模式（向后兼容 scan_tg.ps1 老用法）
    ap.add_argument("--base", default="", help="单实例后端基址（给了即单实例模式）")
    ap.add_argument("--token", default="")
    ap.add_argument("--config", default="")
    ap.add_argument("--out-dir", default="")
    args = ap.parse_args()

    scanners: dict = {}
    single_id = ""

    if args.base:
        # 单实例模式
        token = args.token or _read_token_from_config(Path(args.config)) if (args.token or args.config) else args.token
        if not token and args.config:
            token = _read_token_from_config(Path(args.config))
        if not token:
            raise SystemExit("单实例模式需 --token 或 --config 提供 auth_token")
        out = Path(args.out_dir) if args.out_dir else Path(".")
        single_id = "single"
        scanners["single"] = InstanceScanner("single", "Telegram 总机", args.base, token, out)
    else:
        want = [x.strip() for x in args.instances.split(",") if x.strip()]
        for iid in want:
            meta = DEFAULT_INSTANCES.get(iid)
            if not meta:
                print(f"[portal] 未知实例 {iid}，跳过", flush=True); continue
            base = f"http://127.0.0.1:{meta['port']}"
            token = _read_token_from_config(Path(meta["config"]))
            if not token:
                print(f"[portal] {iid} 未读到 auth_token（{meta['config']}），跳过", flush=True); continue
            if not _probe_ready(base, token):
                print(f"[portal] {iid} 的 telegram protocol 通道未就绪（后端在跑？protocol_enabled？），跳过", flush=True)
                continue
            scanners[iid] = InstanceScanner(iid, meta["label"], base, token, Path(meta["out"]))
            print(f"[portal] 挂载实例 {iid}（{meta['label']} · {base}）", flush=True)

    if not scanners:
        raise SystemExit("无可用实例（后端未就绪或 protocol 未开）——先确认实例在跑且 platform_login.telegram.protocol_enabled=true")

    portal = Portal(scanners, single_id=single_id)
    srv = ThreadingHTTPServer(("127.0.0.1", args.portal_port), make_handler(portal))
    url = f"http://127.0.0.1:{args.portal_port}/"
    print(f"[portal] 扫码门户 v2 已启动：{url}（实例：{','.join(scanners.keys())}）", flush=True)
    print("[portal] 老板：浏览器打开上面地址，选实例后手机 Telegram→设置→设备→连接桌面设备 扫码", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for sc in scanners.values():
            sc._stop = True


if __name__ == "__main__":
    main()
