# -*- coding: utf-8 -*-
"""套壳版（Electron 壳）工作台真机门禁——裸 CDP 直驱 workspace webview，纯只读。

**为什么需要它**（2026-08-29 工单#20 沉淀，老板拍板「内测基本全是套壳版，
先在壳里测过才许回群」）：网页侧 playwright 门禁（verify_inbox_density 等）
测的是纯 chromium + 干净视口，而内测用户全在 Electron 壳里——壳 chrome 挤占、
webview 分区缓存、账号视角数据形态都只有真壳才有。本工具起**本仓同版壳**
（dev electron，与发行版同源），CDP 直驱其 workspace webview 做断言+截图。

默认场景＝工单#20「左列滚到底『已归档』入口可见可点」：切到有真实归档数据的
账号视角（footer 自然渲染，零数据写入），滚动、量可见性、点击进归档视图。

机制备忘（换场景改脚本时先读）：
- Electron 的 <webview> 在 CDP 是独立 target（type=webview），playwright
  connect_over_cdp **不认** → 必须直连其 webSocketDebuggerUrl 裸说 CDP。
- dev config.json 的 token 是占位 "admin" → 运行时临时换实例真 token
  （不打印），spawn.enabled 强制 false（防拉起自带后端，8/15 事故形态），
  platforms 清空（不载第三方页），测完按原字节还原 config.json。
- webview 分区会记住上轮 location.hash（#filter=archived），进场必须视图归零。
- 页面闭包变量（chatFilter 等）不在 window 上，判定用 location.hash / DOM。

用法::

    python tools/verify_shell_inbox.py            # 默认 katie 账号视角
    AITR_SHELL_ACCT=<account_id> 环境变量可换账号

缺 electron/websocket-client/token/实例不可达 → 非零退出（本工具**不进**
gate_sweep 常跑清单：起真壳约 15s 且需桌面会话，按需人工/事故驱动跑）。
"""
import base64
import json
import pathlib
import subprocess
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os

DESKTOP = pathlib.Path(__file__).resolve().parent.parent / "desktop"
CFG = DESKTOP / "config.json"
SHOTS = pathlib.Path(r"D:\chengjie-instances\.ops\shell_verify")
CDP = "http://127.0.0.1:9333"
# 默认 katie 账号：库里有真实归档（只读利用，不动数据）；可经环境变量换
ACCT = os.environ.get("AITR_SHELL_ACCT", "8244899900")


def read_token() -> str:
    import yaml
    root = pathlib.Path(r"D:\chengjie-instances\zhiliao\data\config")
    for name in ("config.local.yaml", "config.yaml"):
        f = root / name
        if not f.is_file():
            continue
        try:
            cfg = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        tok = str((cfg.get("web_admin") or {}).get("auth_token") or "")
        if tok:
            return tok
    raise RuntimeError("读不到 web_admin.auth_token")


class Cdp:
    def __init__(self, ws_url: str):
        import websocket
        self.ws = websocket.create_connection(ws_url, timeout=30,
                                              suppress_origin=True)
        self.mid = 0

    def call(self, method: str, params: dict | None = None, timeout=30):
        self.mid += 1
        req_id = self.mid
        self.ws.send(json.dumps({"id": req_id, "method": method,
                                 "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result") or {}
        raise TimeoutError(method)

    def js(self, expr: str, await_promise=False):
        r = self.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True,
            "awaitPromise": await_promise})
        ex = r.get("exceptionDetails")
        if ex:
            raise RuntimeError(f"js exception: {str(ex)[:300]}")
        return (r.get("result") or {}).get("value")

    def shot(self, path: pathlib.Path):
        r = self.call("Page.captureScreenshot", {"format": "png"})
        path.write_bytes(base64.b64decode(r["data"]))

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def find_webview(deadline_sec=45):
    end = time.time() + deadline_sec
    while time.time() < end:
        try:
            targets = json.load(urllib.request.urlopen(
                CDP + "/json/list", timeout=5))
            for t in targets:
                if t.get("type") == "webview" and \
                        "127.0.0.1:18799" in (t.get("url") or ""):
                    return t
        except Exception:
            pass
        time.sleep(1.5)
    return None


token = read_token()
orig = CFG.read_bytes()
cfg = json.loads(orig.decode("utf-8-sig"))
cfg.setdefault("backend", {}).setdefault("spawn", {})["enabled"] = False
cfg["backend"]["token"] = token
cfg["platforms"] = []
cfg["onboarding"] = {"completed": True, "completed_at": int(time.time()),
                     "edition": "managed"}
CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=1),
               encoding="utf-8")
print("[cfg] spawn off / platforms [] / real token（不打印）")

proc = None
sess = None
rc = 1
try:
    proc = subprocess.Popen(["npx.cmd", "electron", ".",
                             "--remote-debugging-port=9333"],
                            cwd=str(DESKTOP), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen(CDP + "/json/version", timeout=1)
            break
        except Exception:
            time.sleep(1)
    else:
        raise RuntimeError("CDP 口 60s 未起")
    print("[shell] CDP up")

    t = find_webview()
    if not t:
        raise RuntimeError("45s 没等到 backend webview target")
    sess = Cdp(t["webSocketDebuggerUrl"])
    sess.call("Runtime.enable")
    sess.call("Page.enable")

    for i in range(30):
        url = sess.js("location.href")
        if "/workspace" in url and "/login" not in url:
            break
        if "/login" in url:
            sess.js(
                "fetch('/login',{method:'POST',headers:{'Content-Type':"
                "'application/x-www-form-urlencoded'},body:'auth_token='"
                f"+encodeURIComponent({json.dumps(token)})"
                "+'&next=%2Fworkspace',credentials:'same-origin'})"
                ".then(()=>location.replace('/workspace'))")
            time.sleep(3)
        else:
            time.sleep(1)
    if "/workspace" not in sess.js("location.href"):
        raise RuntimeError("webview 没到 /workspace")
    print("[shell] webview @ /workspace")

    for i in range(30):
        if sess.js("typeof window.setPlatFilter === 'function'"):
            break
        time.sleep(1)
    time.sleep(3)

    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok)))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {detail}")

    SHOTS.mkdir(parents=True, exist_ok=True)

    # ── 1. 视图归零 + 切到有归档数据的账号视角（纯视图操作）────────────
    sess.js("(() => { if (typeof setFilter==='function') setFilter('all');"
            " try { history.replaceState(null,'','/workspace'); } catch(e){}"
            " })()")
    time.sleep(1.2)
    ok_acct = sess.js(
        f"typeof setAccountFilter==='function' ? "
        f"(setAccountFilter({json.dumps(ACCT)}), true) : false")
    if not ok_acct:
        raise RuntimeError("setAccountFilter 不可用")
    time.sleep(2.5)
    print(f"[cond] 账号视角 -> {ACCT}")

    # 群组区展开（挤占高发条件；无群组区如实记录）
    grp = sess.js("""(() => {
        const sec = document.getElementById('group-section');
        if (!sec || !sec.offsetParent) return 'absent';
        const body = sec.querySelector('.group-sec-body');
        if (body && body.offsetHeight > 8) return 'expanded';
        const head = sec.querySelector('.group-sec-head') || sec.firstElementChild;
        if (head) { head.click(); return 'clicked'; }
        return 'no-head';
    })()""")
    time.sleep(1)
    print("[cond] group-section:", grp)

    # ── 2. 等 footer 渲染（该账号 18 条既有归档 → 应自然出现）──────────
    has_foot = False
    for _ in range(15):
        has_foot = bool(sess.js(
            "!!document.querySelector('#conv-items .conv-arch-foot')"))
        if has_foot:
            break
        time.sleep(1)
    check("「已归档」入口自然渲染（账号视角）", has_foot)

    if has_foot:
        # ── 3. 滚到底 → 完整可见（工单#20 主诉）───────────────────────
        meas = sess.js("""(() => {
            const items = document.getElementById('conv-items');
            items.scrollTop = items.scrollHeight;
            const foot = items.querySelector('.conv-arch-foot');
            const ir = items.getBoundingClientRect();
            const fr = foot.getBoundingClientRect();
            return {
                footText: (foot.textContent || '').trim().slice(0, 40),
                itemsRect: { top: ir.top, bottom: ir.bottom },
                footRect: { top: fr.top, bottom: fr.bottom },
                scrollGap: items.scrollHeight - items.scrollTop
                           - items.clientHeight,
                convCount: items.querySelectorAll('.conv-item').length,
                vw: innerWidth, vh: innerHeight,
            };
        })()""")
        time.sleep(0.5)
        sess.shot(SHOTS / "shell_t20_scrolled.png")
        print(f"[data] 会话行 {meas['convCount']}; viewport "
              f"{meas['vw']}x{meas['vh']}; footer={meas['footText']!r}; "
              f"scrollGap={meas['scrollGap']:.1f}")
        check("滚动到底（无残余滚动距离）", abs(meas["scrollGap"]) < 2,
              f"gap={meas['scrollGap']:.1f}px")
        fr, ir = meas["footRect"], meas["itemsRect"]
        fully = fr["top"] >= ir["top"] - 1 and fr["bottom"] <= ir["bottom"] + 1
        check("「已归档」入口完整可见（工单#20 主诉）", fully,
              f"foot[{fr['top']:.0f}..{fr['bottom']:.0f}] "
              f"容器[{ir['top']:.0f}..{ir['bottom']:.0f}]")

        # ── 4. 点击 → 进归档视图（只切视图）────────────────────────────
        sess.js("document.querySelector('#conv-items .conv-arch-foot').click()")
        time.sleep(2)
        after = sess.js("""(() => {
            const items = document.getElementById('conv-items');
            return { count: items ?
                     items.querySelectorAll('.conv-item').length : -1,
                     hash: String(location.hash || '') };
        })()""")
        sess.shot(SHOTS / "shell_t20_archived.png")
        # chatFilter 是模板闭包变量不上 window；页面切归档视图会写
        # location.hash=#filter=archived（上轮 webview 分区就是靠它记住的）。
        check("点击后进入归档视图且有行",
              "archived" in after["hash"] and after["count"] >= 1,
              f"hash={after['hash']} rows={after['count']}")

    # ── 5. 视图复原（纯视图，无数据写）─────────────────────────────────
    sess.js("(() => { if (typeof setFilter==='function') setFilter('all');"
            " if (typeof setAccountFilter==='function')"
            " setAccountFilter('all'); })()")
    time.sleep(1)

    fails = [r for r in results if not r[1]]
    print(f"== 套壳实测: {len(results) - len(fails)}/{len(results)} PASS ==")
    rc = 1 if fails else 0
finally:
    if sess:
        sess.close()
    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    CFG.write_bytes(orig)
    print("[cfg] 已按原字节还原 config.json")
raise SystemExit(rc)
