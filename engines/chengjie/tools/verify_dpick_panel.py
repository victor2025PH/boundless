# -*- coding: utf-8 -*-
"""坐席工作台「AI 思考面板」三态真浏览器门禁（Playwright；2026-08-01）。

**为什么需要它**：点「AI回复」立即弹 loading（骨架条/相位/计秒/可取消/可收起）、
返回后原地切 ready（勾选逐条发送）、失败原地 error（重试）——这套三态状态机是
纯前端逻辑、模板热更新直上生产。静态门禁只能证「函数挂了 window」，证不了
「loading 真的立即出现、取消真的把按钮放回来、error 真的能原地重试」。生产实测
P90≈9s / 尖峰 18s，等待期的状态外化就是这个面板的存在理由——这里钉住它。

与 ``verify_inbox_draft_review.py`` 同族（同实例 + token 登录 + Playwright +
SKIP exit 0）。**三层 route mock、绝不写生产**：
  - ``/api/unified-inbox/thread*`` → 注入确定性消息（引用条内容可断言，不依赖
    生产会话形态）；
  - ``/api/desktop/smart-reply`` → 扣住请求（held route）模拟慢生成，主流程按需
    放行/报错——不烧真 LLM、时序完全可控；
  - ``/api/telemetry/ui-event`` → 打进 mock 池（测试产生的 dpick.* 漏斗事件不进
    生产计数）。
只点已读会话（``.conv-item:not(.has-unread)``），不改任何已读态。

覆盖的不变量：
  0. 源码接线：三态标记 / 取消链 / IME 守卫 / 备稿新鲜度闸门 / 漏斗埋点 / 胶囊键盘。
  1. 点击后 loading **立即**出现：骨架条 3 根、相位文案非空、引用条含客户原话、
     AI回复按钮 busy 置灰、进度线可见。
  2. 计秒器 ≥1s 后开始走字。
  3. 取消：modal 关、状态回 idle、按钮立即恢复可用（等待期解锁）。
  4. 成功：held 放行 → ready 态、按人设短句拆行、发送按钮带条数。
  5. 勾选联动：取消勾选 → 条数减；全不选 → 发送置灰。
  6. 行内编辑 → 「已改」标记出现。
  7. 收起后台等：胶囊出现（生成中态）→ 放行 → 胶囊亮「已就绪」→ 点胶囊回弹 ready。
  8. 失败：500 → error 态 + 重试按钮可见；手动输入关闭面板。
  9. （有第二个会话时）loading 中切会话 → 看门狗自动取消回 idle。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

DEFAULT_BASE = "http://localhost:18799"
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"
VIEWPORT = {"width": 1440, "height": 900}
ENGINE_ROOT = Path(__file__).resolve().parents[1]
INBOX_TEMPLATE = ENGINE_ROOT / "src" / "web" / "templates" / "unified_inbox.html"
INBOX_CSS = ENGINE_ROOT / "src" / "web" / "static" / "workspace" / "unified-inbox.css"

_GATE_INBOUND = "GATE_INBOUND_你最近在忙什么呀"
_GATE_REPLY = "GATE第一句哈。GATE第二句啦。GATE第三句哟。"

_STATE_JS = """() => {
  const g = id => document.getElementById(id);
  const m = g('dpick-modal');
  const btn = g('ai-reply-btn');
  const pill = g('dpick-pill');
  const send = g('dpick-send-btn');
  const loading = document.querySelector('.dpick-loading');
  return {
    shown: !!(m && m.classList.contains('show')),
    st: m ? (m.getAttribute('data-st') || '') : '',
    ariaBusy: m ? (m.getAttribute('aria-busy') || '') : '',
    skel: document.querySelectorAll('.dpick-skel i').length,
    loadingVisible: !!(loading && getComputedStyle(loading).display !== 'none'),
    phase: (g('dpick-phase') || {}).textContent || '',
    elapsed: (g('dpick-elapsed') || {}).textContent || '',
    quote: (g('dpick-quote') || {}).textContent || '',
    hint: (g('dpick-hintline') || {}).textContent || '',
    rows: document.querySelectorAll('#dpick-list .dpick-row').length,
    editedRows: document.querySelectorAll('#dpick-list .dpick-row.edited').length,
    sendTxt: send ? (send.textContent || '') : '',
    sendDisabled: !!(send && send.disabled),
    studioBtn: (function(){
      const b = document.querySelector('.dpick-act-ready[onclick="draftPickToStudio()"]');
      return !!(b && getComputedStyle(b).display !== 'none');
    })(),
    btnDisabled: !!(btn && btn.disabled),
    btnBusy: !!(btn && btn.classList.contains('busy')),
    pillShown: !!(pill && pill.classList.contains('show')),
    pillReady: !!(pill && pill.classList.contains('ready')),
    pillErr: !!(pill && pill.classList.contains('err')),
    title: (g('dpick-title') || {}).textContent || '',
    sub: (g('dpick-sub') || {}).textContent || '',
    convCount: document.querySelectorAll('#conv-items .conv-item').length,
  };
}"""


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
        self.skipped: List[str] = []

    def check(self, name: str, cond: Any, detail: str = "") -> bool:
        ok = bool(cond)
        self.results.append((name, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        return ok

    def skip(self, name: str, why: str) -> None:
        self.skipped.append(name)
        print(f"  [SKIP] {name}  {why}")

    def summary(self, title: str = "AI 思考面板三态验证") -> int:
        fails = [n for n, ok in self.results if not ok]
        total = len(self.results)
        tail = f"  FAILED: {fails}" if fails else " =="
        extra = f"  (skipped {len(self.skipped)})" if self.skipped else ""
        print(f"\n== {title}: {total - len(fails)}/{total} PASS{extra}{tail}")
        return 1 if fails else 0


def check_source_wiring(ck: Checker) -> None:
    print("== 0. 源码接线（静态）==")
    if not INBOX_TEMPLATE.exists():
        ck.check("unified_inbox.html 存在", False, str(INBOX_TEMPLATE))
        return
    src = INBOX_TEMPLATE.read_text(encoding="utf-8")
    ck.check("三态状态机（_dpickSetState + data-st）",
             "_dpickSetState" in src and "data-st" in src)
    ck.check("取消链（AbortController + draftPickCancel）",
             "AbortController" in src and "draftPickCancel" in src)
    ck.check("请求代际防串（_dpickReqSeq）", "_dpickReqSeq" in src)
    ck.check("切会话看门狗覆盖 ready+error 胶囊",
             "(_dpickState==='ready'||_dpickState==='error') && _dpickMinimized" in src)
    ck.check("IME 组合期 Esc 守卫（isComposing）", "e.isComposing" in src)
    ck.check("备稿新鲜度闸门（approve_blocked + 晚于最后入站）",
             "approve_blocked) d=null" in src and "(+d.created_ts||0)<lastIn" in src)
    ck.check("漏斗埋点（dpick.open/ready/cancel/send）",
             all(k in src for k in
                 ("dpick.open", "dpick.ready", "dpick.cancel", "dpick.send")))
    ck.check("动态 ETA（_dpickEtaText + hint_eta_dyn）",
             "_dpickEtaText" in src and "inbox.dpick.hint_eta_dyn" in src)
    ck.check("胶囊键盘可达（draftPickPillKey 定义+内联+暴露）",
             src.count("draftPickPillKey") >= 3)
    ck.check("工坊联动（draftPickToStudio 定义+内联+暴露）",
             src.count("draftPickToStudio") >= 3 and "dpick.studio" in src)
    if INBOX_CSS.exists():
        css = INBOX_CSS.read_text(encoding="utf-8")
        ck.check("CSS 三态显隐（[data-st=loading]）", "[data-st=loading]" in css)
        ck.check("CSS 动效可及性（prefers-reduced-motion 覆盖 dpick）",
                 "prefers-reduced-motion" in css and ".dpick-skel i" in css)
    else:
        ck.check("unified-inbox.css 存在", False, str(INBOX_CSS))


def _login_workspace(p: Any, base: str, token: str, *, headed: bool) -> Tuple[Any, Any, Any]:
    browser = p.chromium.launch(headless=not headed)
    ctx = browser.new_context(viewport=VIEWPORT)
    ctx.request.post(base + "/login", form={"auth_token": token})
    page = ctx.new_page()
    page.goto(base + "/workspace", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => typeof window.genAiReply === 'function'", timeout=20000)
    page.wait_for_timeout(3000)
    return browser, ctx, page


def _thread_mock_body() -> str:
    import time as _t
    now = _t.time()
    return json.dumps({"ok": True, "messages": [
        {"message_id": "g1", "direction": "out", "text": "嗨嗨",
         "ts": now - 600},
        {"message_id": "g2", "direction": "in", "text": _GATE_INBOUND,
         "ts": now - 300},
    ]}, ensure_ascii=False)


def _click_read_conv(page: Any, skip_key: str = "") -> str:
    """点一个已读会话（可跳过指定 convKey 的行），返回被点行的 data key（空=没点成）。"""
    return page.evaluate(
        """(skip) => {
          const rows = Array.from(document.querySelectorAll(
            '#conv-items .conv-item:not(.has-unread)'));
          for (const row of rows) {
            const key = row.getAttribute('data-ckey') || row.textContent.slice(0, 40);
            if (skip && key === skip) continue;
            row.click();
            return key;
          }
          return '';
        }""", skip_key)


def run(base: str, token: str, *, headed: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    check_source_wiring(ck)

    with sync_playwright() as p:
        try:
            browser, ctx, page = _login_workspace(p, base, token, headed=headed)
        except Exception as e:  # noqa: BLE001
            print(f"[SKIP] 工作台脚本未就绪（{str(e)[:80]}）")
            return 0

        # ── route mocks（先挂再交互；全部只读隔离）─────────────────────
        held: List[Any] = []
        page.on("request",
                lambda r: print(f"  [dbg] REQ {r.method} …{r.url[-40:]}")
                if "smart-reply" in r.url else None)
        page.on("requestfailed",
                lambda r: print(f"  [dbg] REQFAIL …{r.url[-40:]} {r.failure}")
                if "smart-reply" in r.url else None)
        page.route("**/api/desktop/smart-reply",
                   lambda route: held.append(route))
        page.route("**/api/unified-inbox/thread*",
                   lambda route: route.fulfill(
                       status=200, content_type="application/json",
                       body=_thread_mock_body()))
        page.route("**/api/telemetry/ui-event",
                   lambda route: route.fulfill(
                       status=200, content_type="application/json",
                       body='{"ok": true}'))
        # 备稿箱确定性关闭：composer 草稿条拿不到 pending 草稿 → 面板永不出备稿块
        page.route("**/api/drafts?status=pending*",
                   lambda route: route.fulfill(
                       status=200, content_type="application/json",
                       body='{"ok": true, "drafts": []}'))

        def snap() -> dict:
            return page.evaluate(_STATE_JS)

        def wait_state(want_st: str, timeout_ms: int = 5000) -> dict:
            s = snap()
            waited = 0
            while s.get("st") != want_st and waited < timeout_ms:
                page.wait_for_timeout(120)
                waited += 120
                s = snap()
            return s

        def flush_held(status: int = 200, ok: bool = True) -> bool:
            """放行最早被扣住的 smart-reply 请求（500 时给失败体）。"""
            while held:
                route = held.pop(0)
                try:
                    if status == 200:
                        route.fulfill(
                            status=200, content_type="application/json",
                            body=json.dumps({
                                "ok": ok, "reply": _GATE_REPLY,
                                "persona": "gate_persona"}, ensure_ascii=False))
                    else:
                        route.fulfill(
                            status=status, content_type="application/json",
                            body='{"ok": false, "detail": "gate mocked failure"}')
                    return True
                except Exception:
                    continue      # 已被客户端 abort 的路由：丢弃换下一个
            return False

        def abort_held() -> None:
            while held:
                try:
                    held.pop(0).abort()
                except Exception:
                    pass

        print("== 1. 打开已读会话（thread 已 mock）==")
        first_key = _click_read_conv(page)
        if not first_key:
            ck.skip("三态交互", "无已读会话可开——不碰未读")
            browser.close()
            return ck.summary()
        page.wait_for_timeout(1200)     # loadThread 消费 mock + 渲染

        # ── T1 loading 立即出现 ─────────────────────────────────────────
        print("== 2. 点击即弹 loading ==")
        page.evaluate("() => document.getElementById('ai-reply-btn').click()")
        s = wait_state("loading", 2000)
        ck.check("loading 态立即出现", s["shown"] and s["st"] == "loading",
                 f"st={s['st']!r}")
        ck.check("骨架条 3 根 + loading 区可见",
                 s["skel"] == 3 and s["loadingVisible"], f"skel={s['skel']}")
        ck.check("相位文案非空", bool(s["phase"].strip()), f"phase={s['phase']!r}")
        ck.check("引用条含客户原话", _GATE_INBOUND[:12] in s["quote"],
                 f"quote={s['quote'][:40]!r}")
        ck.check("AI回复按钮 busy 置灰", s["btnDisabled"] and s["btnBusy"])
        ck.check("aria-busy=true", s["ariaBusy"] == "true")
        ck.check("ETA 提示行非空", bool(s["hint"].strip()), f"hint={s['hint']!r}")

        print("== 3. 计秒器 ==")
        page.wait_for_timeout(1400)
        s = snap()
        ck.check("≥1s 后计秒器走字", bool(s["elapsed"].strip()),
                 f"elapsed={s['elapsed']!r}")

        # ── T3 取消 ────────────────────────────────────────────────────
        print("== 4. 取消生成 ==")
        page.evaluate("() => window.draftPickCancel()")
        page.wait_for_timeout(400)
        s = snap()
        ck.check("取消后回 idle + modal 关", (not s["shown"]) and s["st"] == "idle",
                 f"st={s['st']!r}")
        ck.check("取消后按钮立即恢复", (not s["btnDisabled"]) and (not s["btnBusy"]))
        abort_held()

        # ── T4 成功 → ready ────────────────────────────────────────────
        print("== 5. 成功路径（held 放行 → ready）==")
        page.evaluate("() => document.getElementById('ai-reply-btn').click()")
        wait_state("loading", 2000)
        page.wait_for_timeout(300)      # 请求进 held 池
        ck.check("smart-reply 被扣住（held）", len(held) >= 1, f"held={len(held)}")
        flush_held(200)
        s = wait_state("ready", 5000)
        ck.check("ready 态出现", s["shown"] and s["st"] == "ready", f"st={s['st']!r}")
        ck.check("拆成 3 条短句", s["rows"] == 3, f"rows={s['rows']}")
        ck.check("发送按钮带条数 3", "3" in s["sendTxt"], f"send={s['sendTxt']!r}")
        ck.check("人设署名进副标题", "gate_persona" in s["sub"], f"sub={s['sub'][:40]!r}")
        ck.check("按钮已恢复", not s["btnDisabled"])
        ck.check("就绪态有「去工坊深编」按钮", s["studioBtn"])

        print("== 6. 勾选联动 + 行内编辑 ==")
        page.evaluate(
            """() => {
              const ck0 = document.querySelector(
                '#dpick-list .dpick-row input[type=checkbox]');
              if (ck0) { ck0.checked = false; ck0.dispatchEvent(new Event('change')); }
            }""")
        page.wait_for_timeout(200)
        s = snap()
        ck.check("取消勾选后条数变 2", "2" in s["sendTxt"], f"send={s['sendTxt']!r}")
        page.evaluate(
            """() => {
              const el = document.querySelector('#dpick-list .dpick-txt');
              el.textContent = el.textContent + 'X';
              el.dispatchEvent(new Event('input'));
            }""")
        page.wait_for_timeout(200)
        s = snap()
        ck.check("行内编辑出「已改」标记", s["editedRows"] >= 1,
                 f"edited={s['editedRows']}")
        page.evaluate("() => window.closeDraftPick()")
        page.wait_for_timeout(300)

        # ── T6 收起后台等（胶囊）──────────────────────────────────────
        print("== 7. 收起后台等 → 就绪胶囊 → 回弹 ==")
        page.evaluate("() => document.getElementById('ai-reply-btn').click()")
        wait_state("loading", 2000)
        page.wait_for_timeout(300)
        page.evaluate("() => window.draftPickMinimize()")
        page.wait_for_timeout(300)
        s = snap()
        ck.check("收起后 modal 关 + 生成中胶囊亮",
                 (not s["shown"]) and s["pillShown"] and not s["pillReady"],
                 f"pill={s['pillShown']}/{s['pillReady']}")
        flush_held(200)
        page.wait_for_timeout(600)
        s = snap()
        ck.check("就绪后胶囊亮「已就绪」（不抢屏）",
                 s["pillShown"] and s["pillReady"] and not s["shown"],
                 f"pill={s['pillShown']}/{s['pillReady']} shown={s['shown']}")
        page.evaluate("() => window.draftPickPillClick()")
        page.wait_for_timeout(300)
        s = snap()
        ck.check("点胶囊回弹 ready", s["shown"] and s["st"] == "ready",
                 f"st={s['st']!r}")
        page.evaluate("() => window.closeDraftPick()")
        page.wait_for_timeout(300)

        # ── T7 失败 → error → 手动输入 ─────────────────────────────────
        print("== 8. 失败路径（500 → error → 手动输入）==")
        page.evaluate("() => document.getElementById('ai-reply-btn').click()")
        wait_state("loading", 2000)
        page.wait_for_timeout(300)
        flush_held(500)
        s = wait_state("error", 5000)
        ck.check("error 态出现", s["shown"] and s["st"] == "error", f"st={s['st']!r}")
        ck.check("重试按钮可见", page.evaluate(
            """() => {
              const b = document.querySelector('.dpick-act-error.rt-primary');
              return !!(b && getComputedStyle(b).display !== 'none');
            }"""))
        page.evaluate("() => window.draftPickManual()")
        page.wait_for_timeout(300)
        s = snap()
        ck.check("手动输入关闭面板", (not s["shown"]) and s["st"] == "idle")

        # ── T9 切会话看门狗（有第二个已读会话才跑）────────────────────
        print("== 9. loading 中切会话（看门狗）==")
        if snap()["convCount"] >= 2:
            page.evaluate("() => document.getElementById('ai-reply-btn').click()")
            wait_state("loading", 2000)
            second = _click_read_conv(page, skip_key=first_key)
            if second:
                page.wait_for_timeout(900)     # 250ms 心跳 ×3 余量
                s = snap()
                ck.check("切会话自动取消回 idle",
                         (not s["shown"]) and s["st"] == "idle" and not s["btnDisabled"],
                         f"st={s['st']!r}")
            else:
                ck.skip("切会话看门狗", "没点到第二个已读会话")
                page.evaluate("() => window.draftPickCancel()")
        else:
            ck.skip("切会话看门狗", "只有一个会话")
        abort_held()

        page.unroute("**/api/desktop/smart-reply")
        page.unroute("**/api/unified-inbox/thread*")
        page.unroute("**/api/telemetry/ui-event")
        page.unroute("**/api/drafts?status=pending*")
        browser.close()

    return ck.summary()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="AI 思考面板三态门禁（route mock，无副作用）")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--token", default="")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--source-only", action="store_true",
                    help="只跑静态源码接线（不启浏览器）")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    if args.source_only:
        ck = Checker()
        check_source_wiring(ck)
        return ck.summary("AI 思考面板源码接线")

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("[WARN] 未装 playwright；仅跑静态源码接线")
        ck = Checker()
        check_source_wiring(ck)
        code = ck.summary("AI 思考面板源码接线（无 playwright）")
        return 0 if code == 0 else code

    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen(args.base + "/login", timeout=4)
    except urllib.error.HTTPError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"[SKIP] 实例不可达（{args.base}）：{str(e)[:80]}")
        ck = Checker()
        check_source_wiring(ck)
        code = ck.summary("AI 思考面板源码接线（实例不可达）")
        return 0 if code == 0 else code

    token = args.token or read_token(args.data_root)
    if not token:
        print(f"[ABORT] 未能从 {args.data_root} 读到 web_admin.auth_token")
        return 2

    print(f"== 目标 {args.base}（视口 {VIEWPORT['width']}x{VIEWPORT['height']}）==")
    return run(args.base, token, headed=args.headed)


if __name__ == "__main__":
    sys.exit(main())
