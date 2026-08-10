# -*- coding: utf-8 -*-
"""共享 copilot 写通道 CSRF 凭证真浏览器验证（Playwright；2026-07-31 事故沉淀）。

**为什么需要它**：「人设切换失败」事故的本质＝写请求的 CSRF 通行证靠宿主页面
补丁隐式发放，共享组件在 iframe App 宿主一张证都不带、只剩 Referer 一根线。
修复后传输层自带 X-CSRF-Token（copilot-client）+ 宿主兜底补丁（app.html），
但这是**纯前端行为**：静态门禁能证「代码里写了注入」，证不了「真浏览器发出的
请求头里真有」；而共享组件热更新直上生产。故用真浏览器把三条不变量钉住：

  1. 原生工作台模式：换绑 POST 携带 X-CSRF-Token（页面补丁 / 客户端注入皆可）；
  2. iframe App 模式 + **Referer/Origin 全剥**（事故环境）：经共享客户端换绑成功
     ——写通道不再依赖 Referer；
  3. 无凭证裸写请求 → 403 且带机器可读 code=csrf（前端分型/自愈的契约字段）。

只写探针会话键（qa_probe_write_csrf），绑后即解、真实客户会话零触碰。
缺 playwright / 实例不可达 → SKIP exit 0（不污染回归信号）。

用法::

    python tools/verify_copilot_write_csrf.py
    python tools/verify_copilot_write_csrf.py --base http://127.0.0.1:18799
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改回 localhost：::1 回退每连接 ~2s（见 verify_inbox_density.py 同行注释）
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"
PROBE_CID = "telegram:8244899900:qa_probe_write_csrf"


def read_token(data_root: str) -> str:
    """从实例数据根读 web_admin.auth_token（overlay 优先；不打印）。"""
    import yaml
    root = Path(data_root)
    for name in ("config.local.yaml", "config.yaml"):
        fp = root / "config" / name
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


class Checker:
    def __init__(self) -> None:
        self.results: List[Tuple[str, bool]] = []

    def check(self, name: str, cond: Any, detail: str = "") -> bool:
        ok = bool(cond)
        self.results.append((name, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        return ok

    def summary(self) -> int:
        fails = [n for n, ok in self.results if not ok]
        total = len(self.results)
        print(f"\n== copilot 写通道 CSRF 验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


def _first_profile_id(ctx, base: str) -> str:
    r = ctx.request.get(base + "/api/personas/profiles")
    if not r.ok:
        return ""
    try:
        summary = (r.json() or {}).get("summary") or []
    except Exception:
        return ""
    return str(summary[0].get("id")) if summary else ""


def run(base: str, token: str) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        ctx.request.post(base + "/login", form={"auth_token": token})
        pid = _first_profile_id(ctx, base)
        if not pid:
            print("[SKIP] 读不到人设清单（登录失败或零人设），不验")
            browser.close()
            return 0

        # ── 1. 原生工作台：换绑 POST 必须带 X-CSRF-Token ──
        print("== 1. 原生模式：写请求头携带 CSRF 凭证 ==")
        page = ctx.new_page()
        captured: List[dict] = []

        def on_request(req):
            if "/api/persona/" in req.url and req.method == "POST":
                captured.append({"url": req.url,
                                 "csrf": req.headers.get("x-csrf-token", "")})

        page.on("request", on_request)
        page.goto(base + "/workspace", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        r1 = page.evaluate(
            """async (args) => {
                const [cid, pid] = args;
                const client = window.CopilotShared.createCopilotClient();
                const b = await client.bindConvPersona({ conversationId: cid, profileId: pid });
                const u = await client.unbindConvPersona({ conversationId: cid });
                return { bind: !!(b && b.ok), unbind: !!(u && u.ok) };
            }""",
            [PROBE_CID, pid],
        )
        ck.check("原生模式换绑+解绑成功", r1.get("bind") and r1.get("unbind"))
        ck.check("换绑请求头携带 X-CSRF-Token",
                 captured and all(c["csrf"] for c in captured),
                 f"captured={len(captured)}")
        page.close()

        # ── 2. iframe App 模式 + Referer/Origin 全剥（事故环境） ──
        print("== 2. iframe App 模式：剥除 Referer/Origin 后写通道仍通 ==")
        page2 = ctx.new_page()

        def strip_headers(route):
            h = {k: v for k, v in route.request.headers.items()
                 if k.lower() not in ("referer", "origin")}
            route.continue_(headers=h)

        page2.route("**/api/persona/*", strip_headers)
        page2.goto(base + "/workspace?app=1", wait_until="domcontentloaded")
        page2.wait_for_timeout(3500)
        frame = None
        for f in page2.frames:
            if "copilot/app.html" in f.url:
                frame = f
                break
        if frame is None:
            try:
                page2.evaluate("_wsEnableApp && _wsEnableApp()")
                page2.wait_for_timeout(2500)
                for f in page2.frames:
                    if "copilot/app.html" in f.url:
                        frame = f
                        break
            except Exception:
                frame = None
        if ck.check("iframe App 已加载", frame is not None):
            r2 = frame.evaluate(
                """async (args) => {
                    const [cid, pid] = args;
                    const client = window.CopilotShared.createCopilotClient();
                    const b = await client.bindConvPersona({ conversationId: cid, profileId: pid });
                    const u = await client.unbindConvPersona({ conversationId: cid });
                    return { bind: !!(b && b.ok), status: (b && b.status), unbind: !!(u && u.ok) };
                }""",
                [PROBE_CID, pid],
            )
            ck.check("事故环境（无 Referer/Origin）换绑成功",
                     r2.get("bind") and r2.get("unbind"),
                     f"status={r2.get('status')}")
        page2.close()

        # ── 3. 裸写请求 → 403 + code=csrf（前端分型契约） ──
        print("== 3. 无凭证裸写 → 结构化 403 ==")
        r3 = ctx.request.post(base + "/api/persona/bind",
                              data={"scope": "conversation",
                                    "conversation_id": PROBE_CID,
                                    "profile_id": pid})
        body = {}
        try:
            body = r3.json() or {}
        except Exception:
            body = {}
        ck.check("裸写 403", r3.status == 403, f"status={r3.status}")
        ck.check("403 带 code=csrf", body.get("code") == "csrf", f"body={body}")

        browser.close()
    return ck.summary()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT,
                    help="实例数据根（读 web_admin.auth_token）")
    ap.add_argument("--token", default="", help="直接给 token（优先于 --data-root）")
    args = ap.parse_args()

    try:
        import playwright  # noqa: F401
    except Exception:
        print("[SKIP] playwright 未安装，跳过（exit 0）")
        return 0
    import urllib.request
    try:
        urllib.request.urlopen(args.base + "/login", timeout=4)
    except Exception:
        print(f"[SKIP] 实例不可达 {args.base}，跳过（exit 0）")
        return 0
    token = args.token or read_token(args.data_root)
    if not token:
        print(f"[SKIP] 未能从 {args.data_root} 读到 web_admin.auth_token（exit 0）")
        return 0
    return run(args.base, token)


if __name__ == "__main__":
    sys.exit(main())
