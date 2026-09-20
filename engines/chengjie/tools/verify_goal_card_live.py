# -*- coding: utf-8 -*-
"""目标引擎可追溯 / 卡片真相 —— 活体实例冒烟（M-7 #236，2026-09-08）。

对一台**在跑的**实例做端到端核对，全程只用一个合成会话（`telegram:m7smoke:<ts>`），
跑完即取消目标，不碰真实客户会话、不发任何消息：

    python tools/verify_goal_card_live.py --base http://127.0.0.1:18799 --token <web_admin.auth_token>
    python tools/verify_goal_card_live.py --base ... --token ... --log D:\\chengjie-instances\\zhiliao\\data\\logs\\app.log
    python tools/verify_goal_card_live.py --base ... --token ... --no-browser   # 只跑 API 段

核对面（失败以非零码退出，结果 JSON 落 --out）：
  API     建目标 → for-conversation 的 sprint_live 新字段（beats_used/trace/auto_state/stalled）
          → GET /api/goals/{id}/beats → 详情 created 事件带 conversation_id → 取消
  日志    --log 给了实例 app.log 时，核 [goal-state] created / updated 两行真出现在生产日志
          （#236 真因：goals 包 logger 曾不在 src.* 命名空间，INFO 全丢）
  浏览器  /copilot/app.html?cid=… 直挂 cp-goal：标题 / 自治标签 ⚠ / 状态行按 auto_state 说真相 /
          旧空头承诺不在 /「AI 做了什么」→ /beats 空态；/workspace 宿主：__wsFocusConv、
          cp-goal-jump-message 桥、cp-goal.js 缓存戳、通知中心 goal_settled_alert 映射、i18n 热更
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path


def _api_phase(base: str, token: str, conv: str, log_path: str) -> dict:
    import requests

    out: dict = {}
    s = requests.Session()
    r = s.post(f"{base}/login", data={"auth_token": token}, allow_redirects=False, timeout=10)
    out["login"] = r.status_code

    def api(method, path, **kw):
        # 与 workspace_base.html 的 fetch 包装同口径：csrf_token cookie → X-CSRF-Token
        hdr = dict(kw.pop("headers", {}) or {})
        tok = s.cookies.get("csrf_token", "")
        if tok:
            hdr["X-CSRF-Token"] = tok
        rr = s.request(method, base + path, timeout=20, headers=hdr, **kw)
        try:
            body = rr.json()
        except Exception:
            body = rr.text[:300]
        return rr.status_code, body

    def log_hits(pattern: str) -> list:
        if not log_path or not Path(log_path).exists():
            return []
        tail = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()[-600:]
        return [l for l in tail if re.search(pattern, l)][-4:]

    # csrf_token cookie 由首个 GET 响应下发（admin._csrf_admit）——先读一次再写
    code, body = api("GET", f"/api/goals/for-conversation?conversation_id={conv}")
    out["fc_before"] = {"status": code, "goal": (body or {}).get("goal") if isinstance(body, dict) else None}
    code, body = api("POST", "/api/goals", json={
        "template": "custom", "conversation_id": conv, "autonomy": "auto",
        "deadline_days": 3, "params": {"note": "M-7 live smoke"}, "title": "M7 smoke"})
    gid = (body.get("goal") or {}).get("goal_id") if isinstance(body, dict) else None
    out["create"] = {"status": code, "goal_id": gid}
    if not gid:
        out["create"]["body"] = body
        return out
    try:
        time.sleep(0.4)
        out["log_goal_state_created"] = log_hits(r"\[goal-state\] created goal=" + gid)
        code, body = api("GET", f"/api/goals/for-conversation?conversation_id={conv}")
        g = (body or {}).get("goal") or {}
        live = g.get("sprint_live") or {}
        out["for_conversation"] = {
            "status": code, "goal_status": g.get("status"),
            "has_trace": isinstance(live.get("trace"), dict),
            "beats_used": live.get("beats_used"),
            "auto_state": live.get("auto_state"), "stalled": live.get("stalled"),
            "blockers": live.get("blockers"),
        }
        code, body = api("GET", f"/api/goals/{gid}/beats")
        out["beats"] = {"status": code,
                        "ok": isinstance(body, dict) and "beats" in body and "summary" in body}
        code, body = api("GET", f"/api/goals/{gid}")
        evs = (body or {}).get("events") or []
        created = [e for e in evs if e.get("kind") == "created"]
        out["created_event_has_conv"] = bool(created and created[0].get("conversation_id") == conv)
    finally:
        code, body = api("POST", f"/api/goals/{gid}/status", json={"action": "cancel"})
        out["cancel"] = code
        time.sleep(0.4)
        out["log_goal_state_cancelled"] = log_hits(r"\[goal-state\] updated goal=" + gid)
    return out


def _browser_phase(base: str, token: str, conv: str) -> dict:
    from playwright.sync_api import sync_playwright

    out: dict = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, locale="zh-CN")
        ctx.request.post(base + "/login", form={"auth_token": token})
        csrf = next((c["value"] for c in ctx.cookies() if c["name"] == "csrf_token"), "")
        hdr = {"Content-Type": "application/json"}
        if csrf:
            hdr["X-CSRF-Token"] = csrf
        r = ctx.request.post(base + "/api/goals", headers=hdr, data=json.dumps({
            "template": "custom", "conversation_id": conv, "autonomy": "auto",
            "deadline_days": 3, "params": {"note": "M-7 browser smoke"},
            "title": "M7 browser smoke"}))
        gid = ((r.json() or {}).get("goal") or {}).get("goal_id")
        out["create"] = (r.status, gid)
        if not gid:
            browser.close()
            return out
        errors: list = []
        try:
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"{base}/copilot/app.html?cid={conv}&theme=light",
                      wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3500)
            grab = """() => { const el = document.querySelector('cp-goal');
                return el ? ((el.shadowRoot ? el.shadowRoot.innerHTML : '') + el.innerHTML) : ''; }"""
            html = page.evaluate(grab)
            text = re.sub(r"<[^>]+>", " ", html)
            out["card_has_title"] = "M7 browser smoke" in text
            out["card_auto_tag_warn"] = ("\u26a0" in text)
            out["card_status_line"] = [s.strip() for s in re.findall(
                r"gl-sprint-line\"><span>(.*?)</span>", html)][:2]
            out["card_no_stale_promise"] = "等对方回复中" not in text and "Waiting for their reply" not in text
            clicked = page.evaluate("""() => { const el = document.querySelector('cp-goal');
                const r = el.shadowRoot || el; const b = r.querySelector('[data-act="prog_toggle"]');
                if (!b) return false; b.click(); return true; }""")
            page.wait_for_timeout(1500)
            html2 = page.evaluate(grab)
            out["beats_panel"] = clicked and ("gl-prog open" in html2)
            out["beats_empty_copy"] = ("还没有任何动作" in html2) or ("No activity yet" in html2)
            ws = ctx.new_page()
            ws.goto(f"{base}/workspace", wait_until="domcontentloaded", timeout=60000)
            ws.wait_for_timeout(3000)
            src = ws.content()
            out["ws_focus_conv_fn"] = ws.evaluate("() => typeof window.__wsFocusConv === 'function'")
            out["ws_jump_handler_live"] = "m.type==='cp-goal-jump-message'" in src
            m = re.search(r"cp-goal\.js\?v=(\w+)", src)
            out["ws_cp_goal_stamp"] = m.group(1) if m else None
            out["ws_notif_goal_settled"] = bool(re.search(r"goal_settled_alert\s*:\s*\{", src))
            out["i18n_live"] = ws.evaluate("""() => ({
                settled: (window.T && window.T('base.notif.type_goal_settled')) || null,
                auto_active: (window.T && window.T('inbox.goal.auto.active')) || null })""")
            out["page_errors"] = errors
        finally:
            r2 = ctx.request.post(base + f"/api/goals/{gid}/status", headers=hdr,
                                  data=json.dumps({"action": "cancel"}))
            out["cleanup_cancel"] = r2.status
            browser.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18799")
    ap.add_argument("--token", required=True, help="web_admin.auth_token（实例 config.local.yaml）")
    ap.add_argument("--log", default="", help="实例 app.log 路径（给了才核 [goal-state] 行）")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--out", default="verify_goal_card_live.json")
    args = ap.parse_args()
    stamp = int(time.time())
    result = {"base": args.base,
              "api": _api_phase(args.base, args.token, f"telegram:m7smoke:{stamp}a", args.log)}
    if not args.no_browser:
        result["browser"] = _browser_phase(args.base, args.token, f"telegram:m7smoke:{stamp}b")
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=1))

    a = result["api"]
    ok = (a.get("create", {}).get("status") == 200
          and a.get("for_conversation", {}).get("status") == 200
          and a.get("for_conversation", {}).get("has_trace") is True
          and a.get("for_conversation", {}).get("auto_state")
          and a.get("beats", {}).get("ok") is True
          and a.get("created_event_has_conv") is True
          and a.get("cancel") == 200)
    if args.log:
        ok = ok and bool(a.get("log_goal_state_created")) and bool(a.get("log_goal_state_cancelled"))
    b = result.get("browser")
    if b is not None:
        ok = ok and all(b.get(k) for k in (
            "card_has_title", "card_auto_tag_warn", "card_no_stale_promise", "beats_panel",
            "beats_empty_copy", "ws_focus_conv_fn", "ws_jump_handler_live",
            "ws_notif_goal_settled")) and not b.get("page_errors")
    print("RESULT:", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
