# -*- coding: utf-8 -*-
"""坐席工作台「联系人页签」门禁（只读，无副作用）。

守什么（2026-08-01 P0「进得去回不来」事故 + 同日闭环接线的回归网）：

  1. 入口：列表头「会话|联系人」分段控件存在且可点（旧版是右上角小图标模式开关，
     列表头折叠时会整个消失——坐席进得去回不来的根因）。
  2. 面板骨架：账号下拉有选项、计数行有内容、盘点胶囊/名单新鲜度渲染、
     「刷新名单」按钮可用性随平台能力（WA/TG 可同步，LINE/Messenger 禁用）。
  3. 档位：「未开口」pill 激活后行带未开口徽章（或如实空态，绝不静默）。
  4. 出口四路，缺一即陷阱回潮：
     a. Esc；b. 面板标题行 X；c. 会话侧操作（点筛选 tab）自动退出；
     d. 列表头折叠态经 cmdk 入口进入 → 强制展开，「会话」段可见。
  5. 深链：?pane=contacts&only=never_spoke（ops「客户资产」卡 → 工作台的最后一公里）。
  6. 破冰闭环：未开口联系人点进空会话 → 「AI 破冰开场」按钮在场
     （只验按钮存在，不点生成——门禁不烧 LLM）。

只读边界：点联系人行只在客户端合成占位会话（optimistic），不落库不发送；
全程不碰「刷新名单」（POST）与破冰生成（LLM）。

缺 playwright / 实例不可达 => SKIP exit 0（不污染回归信号）。
等待一律 polling=100：无头老化页面的 rAF 轮询会被 Chromium 节流，
默认 raf polling 曾产出「恒 18.8s 假慢」的幽灵读数（2026-08-01 实测）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改回 localhost：::1 回退每连接 ~2s（见 verify_inbox_density.py 同行注释）
DEFAULT_DATA_ROOT = r"D:\chengjie-instances\zhiliao\data"

VIEWPORT = {"width": 1440, "height": 900}
POLL_MS = 100


def read_token(data_root: str) -> str:
    """从实例数据根读 web_admin.auth_token（overlay 优先；不打印）。

    与同族 verify_*_ui.py 各自持有一份（工具保持自包含、可单独拷走运行）。
    """
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

    def summary(self) -> int:
        fails = [n for n, ok in self.results if not ok]
        total = len(self.results)
        tail = f"  FAILED: {fails}" if fails else " =="
        extra = f"  (skipped {len(self.skipped)})" if self.skipped else ""
        print(f"\n== 联系人页签验证: {total - len(fails)}/{total} PASS{extra}{tail}")
        return 1 if fails else 0


_STATE_JS = """() => ({
  panel: getComputedStyle(document.getElementById('contacts-panel')).display,
  convs: getComputedStyle(document.getElementById('conv-items')).display,
  segConv: document.getElementById('lp-opt-conv').classList.contains('active'),
  segCts: document.getElementById('lp-opt-contacts').classList.contains('active'),
  hdrCompact: document.getElementById('list-header').classList.contains('compact'),
})"""

# 面板加载落定 = 下拉有选项 且（有行 或 空态有文字）——spinner 的 empty-list 是空文本
_PANE_READY_JS = (
    "() => document.getElementById('contacts-acct-select').options.length > 0 && "
    "(document.querySelector('#contacts-list .contact-row') || "
    " ((document.querySelector('#contacts-list .empty-list')||{}).textContent||'').trim())"
)
_LIST_SETTLED_JS = (
    "() => document.querySelector('#contacts-list .contact-row') || "
    "((document.querySelector('#contacts-list .empty-list')||{}).textContent||'').trim()"
)


def run(base: str, token: str, *, headed: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(viewport=VIEWPORT)
        ctx.request.post(base + "/login", form={"auth_token": token})
        # 预置「业务助手三步引导已看过」：全新 profile 首次打开会话会弹 coach-marks，
        # 其 cp-tour-veil 全屏拦截 pointer events，把后续每一次 click 卡到超时。
        # 门禁不测引导（它有自己的锚点回落逻辑），直接标记看过。
        ctx.add_init_script("try{localStorage.setItem('ws_cp_tour_done_v1','1');}catch(_){}")
        page = ctx.new_page()
        page.goto(base + "/workspace", wait_until="domcontentloaded")
        try:
            page.wait_for_function("() => typeof window.setListPane === 'function'",
                                   timeout=20000, polling=POLL_MS)
        except Exception:
            print("[SKIP] 工作台脚本未就绪（实例在重启？）")
            browser.close()
            return 0
        page.wait_for_timeout(2500)

        # ── 1. 分段控件入口 + 面板骨架 ──────────────────────────────
        print("== 1. 入口与面板骨架 ==")
        seg_ok = page.evaluate(
            "() => !!(document.getElementById('lp-opt-conv') && document.getElementById('lp-opt-contacts'))")
        ck.check("会话|联系人分段控件在场", seg_ok)
        page.click("#lp-opt-contacts")
        try:
            page.wait_for_function(_PANE_READY_JS, timeout=25000, polling=POLL_MS)
        except Exception:
            pass
        st = page.evaluate(_STATE_JS)
        ck.check("进入：面板可见/会话隐藏/段激活",
                 st["panel"] == "flex" and st["convs"] == "none" and st["segCts"], str(st))
        n_opts = page.evaluate("() => document.getElementById('contacts-acct-select').options.length")
        ck.check("账号下拉有选项", n_opts > 0, f"n={n_opts}")
        cnt = page.evaluate("() => (document.getElementById('contacts-count').textContent||'').trim()")
        ck.check("计数行有内容", bool(cnt), cnt[:40])
        chrome = page.evaluate("""() => {
          const plat=(document.getElementById('contacts-acct-select').value||'').split('|')[0];
          return {plat,
            sum: getComputedStyle(document.getElementById('cp-sum-row')).display,
            ts: (document.getElementById('cp-sync-ts').textContent||'').trim(),
            syncDisabled: document.getElementById('cp-sync-btn').disabled};
        }""")
        can_sync = chrome["plat"] in ("whatsapp", "telegram")
        ck.check("盘点胶囊行渲染", chrome["sum"] == "flex", f"plat={chrome['plat']}")
        ck.check("名单新鲜度文案在场", bool(chrome["ts"]), chrome["ts"][:36])
        ck.check("刷新按钮可用性随平台能力", chrome["syncDisabled"] == (not can_sync),
                 f"plat={chrome['plat']} disabled={chrome['syncDisabled']}")

        # ── 2. 「未开口」档位 ────────────────────────────────────────
        print("== 2. 未开口档位 ==")
        page.click(".cp-fpill[data-only='never_spoke']")
        try:
            page.wait_for_function(_LIST_SETTLED_JS, timeout=25000, polling=POLL_MS)
        except Exception:
            pass
        row0 = page.evaluate("""() => {
          const r=document.querySelector('#contacts-list .contact-row');
          if(!r) return {none:true};
          return {badge: !!r.querySelector('.contact-badge.never'), never: r.dataset.never};
        }""")
        pill_on = page.evaluate(
            "() => document.querySelector(\".cp-fpill[data-only='never_spoke']\").classList.contains('active')")
        ck.check("未开口档激活且行带徽章（或如实空态）",
                 pill_on and (row0.get("none") or (row0.get("badge") and row0.get("never") == "1")),
                 str(row0))

        # ── 3. 破冰闭环：未开口行 → 空会话 → 「AI 破冰开场」按钮在场 ──
        print("== 3. 破冰开场入口（只验在场，不点生成）==")
        has_never_row = not row0.get("none")
        if has_never_row:
            page.evaluate("() => document.querySelector('#contacts-list .contact-row').click()")
            try:
                page.wait_for_function(
                    "() => { const a=document.getElementById('msg-area');"
                    " return a && (a.querySelector('.empty-list') || a.querySelector('.msg-row')); }",
                    timeout=25000, polling=POLL_MS)
            except Exception:
                pass
            ib = page.evaluate("""() => {
              const a=document.getElementById('msg-area');
              if(!a) return {area:false};
              const empty=a.querySelector('.empty-list');
              const btns=[...a.querySelectorAll('.empty-list .tiny-btn')].map(b=>(b.textContent||'').trim());
              return {area:true, hasEmpty:!!empty, hasMsgs:!!a.querySelector('.msg-row'), btns};
            }""")
            if ib.get("hasEmpty"):
                ck.check("空会话给出「AI 破冰开场」按钮",
                         any(b for b in (ib.get("btns") or []) if b), str(ib.get("btns"))[:80])
            else:
                # 「未开口」按 last_ts<=0 判定，本地可能已有出站草稿类消息——如实跳过
                ck.skip("空会话破冰按钮", f"该会话非空态（hasMsgs={ib.get('hasMsgs')}），换号数据态再验")
            # 回到联系人页继续后续出口检查（点行已自动退到会话列表）
            page.evaluate("() => setListPane('contacts')")
            try:
                page.wait_for_function(_PANE_READY_JS, timeout=25000, polling=POLL_MS)
            except Exception:
                pass
        else:
            ck.skip("空会话破冰按钮", "当前账号无未开口联系人")
        page.click(".cp-fpill[data-only='']")
        page.wait_for_timeout(400)

        # ── 4. 出口四路 ─────────────────────────────────────────────
        print("== 4. 出口 A：Esc ==")
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        st = page.evaluate(_STATE_JS)
        ck.check("Esc 退回会话列表", st["panel"] == "none" and st["segConv"], str(st))

        print("== 5. 出口 B：标题行 X ==")
        page.click("#lp-opt-contacts")
        page.wait_for_timeout(800)
        page.click("#cp-close-btn")
        page.wait_for_timeout(400)
        st = page.evaluate(_STATE_JS)
        ck.check("X 退回会话列表", st["panel"] == "none" and st["segConv"], str(st))

        print("== 6. 出口 C：会话侧操作自动退出 ==")
        page.click("#lp-opt-contacts")
        page.wait_for_timeout(800)
        page.evaluate("() => setFilter('unread', document.querySelector('.ftab[data-f=unread]'))")
        page.wait_for_timeout(400)
        st = page.evaluate(_STATE_JS)
        ck.check("点筛选 tab 自动退出", st["panel"] == "none" and st["segConv"], str(st))
        page.evaluate("() => setFilter('all', document.querySelector('.ftab[data-f=all]'))")
        page.wait_for_timeout(300)

        print("== 7. 出口 D：折叠态入场（cmdk 路径）不再无出口 ==")
        page.evaluate("""() => { const el=document.getElementById('conv-items');
          el.scrollTop = 300; el.dispatchEvent(new Event('scroll')); }""")
        page.wait_for_timeout(500)
        page.evaluate("() => toggleContactsPanel()")
        page.wait_for_timeout(800)
        st = page.evaluate(_STATE_JS)
        seg_vis = page.evaluate("() => document.getElementById('lp-opt-conv').offsetParent !== null")
        ck.check("折叠态进入后列表头强制展开且出口可见",
                 st["panel"] == "flex" and (not st["hdrCompact"]) and seg_vis, str(st))

        # ── 8. 深链（ops 客户资产卡 → 工作台联系人页）────────────────
        print("== 8. 深链 ?pane=contacts&only=never_spoke ==")
        page.goto(base + "/workspace?pane=contacts&only=never_spoke",
                  wait_until="domcontentloaded")
        try:
            page.wait_for_function("() => typeof window.setListPane === 'function'",
                                   timeout=20000, polling=POLL_MS)
            page.wait_for_function(
                "() => getComputedStyle(document.getElementById('contacts-panel')).display === 'flex'",
                timeout=25000, polling=POLL_MS)
        except Exception:
            pass
        dl = page.evaluate("""() => ({
          panel: getComputedStyle(document.getElementById('contacts-panel')).display,
          pill: document.querySelector(".cp-fpill[data-only='never_spoke']").classList.contains('active'),
        })""")
        ck.check("深链直达联系人页且未开口档激活",
                 dl["panel"] == "flex" and dl["pill"], str(dl))

        browser.close()
    return ck.summary()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="联系人页签门禁（只读，无副作用）")
    ap.add_argument("--base", default=DEFAULT_BASE, help=f"实例地址（默认 {DEFAULT_BASE}）")
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT,
                    help="实例数据根（读 web_admin.auth_token）")
    ap.add_argument("--token", default="", help="直接给 token（优先于 --data-root）")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    args = ap.parse_args(argv)

    # Windows 控制台默认 GBK：文案含 ✕/emoji 时直接 UnicodeEncodeError（同族工具同坑）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("[SKIP] 未装 playwright（pip install playwright && playwright install chromium）")
        return 0

    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen(args.base + "/login", timeout=4)
    except urllib.error.HTTPError:
        pass          # 任何 HTTP 响应都代表实例活着
    except Exception as e:  # noqa: BLE001
        print(f"[SKIP] 实例不可达（{args.base}）：{str(e)[:80]}")
        return 0

    token = args.token or read_token(args.data_root)
    if not token:
        print(f"[ABORT] 未能从 {args.data_root} 读到 web_admin.auth_token")
        return 2
    print(f"== 0. 目标 {args.base}（视口 {VIEWPORT['width']}x{VIEWPORT['height']}）==")
    return run(args.base, token, headed=args.headed)


if __name__ == "__main__":
    sys.exit(main())
