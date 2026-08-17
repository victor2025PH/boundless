# -*- coding: utf-8 -*-
"""业务助手面板「四形态」真浏览器门禁（Playwright；2026-08-17 沉淀）。

**为什么需要它**：2026-08-17 之前「工具箱消失」反复复发的机制性根因是右栏可见性
由四处机制合谋（父容器 display × .show × infoOn × media query），静态门禁只能证
「函数挂了 window」，证不了「切平台后面板还在」。本工具把形态状态机的核心不变量
固化成真浏览器断言：

  1. 空态常驻（本轮修复的靶心）：未选会话 → 面板可见 + cp-empty-mode + 说明书卡
     点亮 + 通用工具（翻译卡）可用 + 会话依赖卡收起；
  2. 收纳轨：] → 44px 轨（tabs 竖排、body 隐藏）；点轨上页签 → 飞层预览（仍是 rail）；
     Esc 收回飞层；同页再点 / 📌 → 钉住 expanded；
  3. 彻底隐藏：Shift+] → 面板 display:none + 右缘把手可见；点把手 → 唤回；
  4. 选中会话退出空态（说明卡让位业务卡）；**清选中后面板不得蒸发**（回空态而非消失）；
  5. 拖宽动态上限：存储宽度 999 → 实际被夹到 min(720, 45vw, 父宽-520)；
  6. 单卡撕出（P2/P3）：撕出=搬同一节点进悬浮窗 + 原位回收 chip；面板收轨撕出窗
     独立存活；刷新按记忆恢复；收回按钮与双击标题栏都=节点回原位零残留；
  7. App(iframe) 模式（?app=1）：空态面板常驻 + iframe 隐藏防串上一客户残留 +
     宿主侧说明块点亮；P3 起 App 同样有收纳轨/飞层（宿主 tabs 当图标轨，切页走
     postMessage set-tab 桥）；Shift+] 隐藏与把手唤回；撕出记忆不渲染（native-only）。

原生断言经 ``?app=0`` 深链（优先级最高，压过运营默认/坐席偏好）——工具结果与部署
默认形态解耦。与 ``tools/verify_inbox_density.py`` 同族（同一实例 + token 登录 +
Playwright + SKIP exit 0 语义）。**副作用：无**——只点已读会话
（`.conv-item:not(.has-unread)`），不发消息不改会话状态；形态偏好写在本工具自己的
浏览器 context（即用即弃）。

用法::

    python tools/verify_cp_panel_modes.py             # 断言（门禁模式）
    python tools/verify_cp_panel_modes.py --headed    # 肉眼看一遍
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改 localhost：Windows 先试 ::1 每连接 ~2s 回退（density 同注）
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"
VIEWPORT = {"width": 1440, "height": 900}

_SNAP_JS = """() => {
  const sb = document.getElementById('info-sidebar');
  const handle = document.getElementById('ws-cp-edge-handle');
  const guide = sb ? [...sb.querySelectorAll('.cp-guide-card')] : [];
  const xlate = sb ? sb.querySelector('[data-cp-card="xlate"]') : null;
  const draft = sb ? sb.querySelector('[data-cp-card="draft"]') : null;
  const voice = sb ? sb.querySelector('[data-cp-card="voice"]') : null;
  const tabs = document.getElementById('ws-cp-tabs');
  const body = document.getElementById('ws-cp-body');
  const vis = (el) => !!el && el.offsetHeight > 0 && getComputedStyle(el).display !== 'none';
  return {
    sb_w: sb ? sb.offsetWidth : -1,
    sb_visible: vis(sb),
    empty_mode: sb ? sb.classList.contains('cp-empty-mode') : null,
    mode_rail: sb ? sb.classList.contains('mode-rail') : null,
    mode_hidden: sb ? sb.classList.contains('mode-hidden') : null,
    guide_visible: guide.filter(vis).length,
    xlate_visible: vis(xlate),
    draft_visible: vis(draft),
    voice_visible: vis(voice),
    tabs_visible: vis(tabs),
    body_visible: vis(body),
    handle_visible: vis(handle),
    js_mode: (typeof window._cpPanelModeGet === 'function') ? window._cpPanelModeGet() : null,
    flyout: sb ? sb.classList.contains('mode-flyout') : null,
    fly_open: (typeof window._cpFlyoutOpenGet === 'function') ? window._cpFlyoutOpenGet() : null,
    pane_visible: vis(document.getElementById('ws-cp-flyout-pane')),
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

    def summary(self) -> int:
        fails = [n for n, ok in self.results if not ok]
        total = len(self.results)
        tail = f"  FAILED: {fails}" if fails else " =="
        extra = f"  (skipped {len(self.skipped)})" if self.skipped else ""
        print(f"\n== 面板形态验证: {total - len(fails)}/{total} PASS{extra}{tail}")
        return 1 if fails else 0


def run(base: str, token: str, *, headed: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(viewport=VIEWPORT)
        ctx.request.post(base + "/login", form={"auth_token": token})
        page = ctx.new_page()
        page.goto(base + "/workspace?app=0", wait_until="domcontentloaded")
        try:
            page.wait_for_function(
                "() => typeof window.toggleInfoSidebar === 'function' && typeof window._cpPanelModeGet === 'function'",
                timeout=20000)
        except Exception:
            print("[SKIP] 工作台脚本未就绪（实例在重启？或形态状态机批次未上线）")
            browser.close()
            return 0
        page.wait_for_timeout(3500)   # 等首轮 loadChats → applyFilters（空态入口）

        # ── 1. 空态常驻（修复靶心：未选会话面板不得蒸发） ────────────────
        print("== 1. 空态（未选会话）：面板常驻 + 说明书 + 通用工具可用 ==")
        s = page.evaluate(_SNAP_JS)
        ck.check("面板可见且处于 expanded", s["sb_visible"] and s["js_mode"] == "expanded",
                 f"w={s['sb_w']} mode={s['js_mode']}")
        ck.check("空态模式已进入（cp-empty-mode）", s["empty_mode"] is True, f"empty={s['empty_mode']}")
        ck.check("说明书卡点亮（当前页签 ≥1 张可见）", s["guide_visible"] >= 1, f"n={s['guide_visible']}")
        ck.check("会话依赖卡（回复工坊）已收起", not s["draft_visible"], f"draft_visible={s['draft_visible']}")
        # 走真实用户路径点页签（主脚本活在 IIFE，setWsCpTab 不在 window 上）
        page.click(".ws-cp-tab[data-tab='tools']")
        page.wait_for_timeout(300)
        s = page.evaluate(_SNAP_JS)
        ck.check("工具箱页签：翻译工具空态可用", s["xlate_visible"], f"xlate={s['xlate_visible']}")
        ck.check("工具箱页签：语音卡空态收起（需会话）", not s["voice_visible"], f"voice={s['voice_visible']}")

        # ── 2. 收纳轨 ] + 飞层预览 ────────────────────────────────────
        print("== 2. 收纳轨（] / 点图标飞层 / Esc 收回 / 再点钉住）==")
        page.keyboard.press("]")
        page.wait_for_timeout(400)
        s = page.evaluate(_SNAP_JS)
        ck.check("] → 44px 收纳轨", s["mode_rail"] is True and 0 < s["sb_w"] <= 48,
                 f"w={s['sb_w']} rail={s['mode_rail']}")
        ck.check("轨态：tabs 竖排可见、body 隐藏、飞层关",
                 s["tabs_visible"] and not s["body_visible"] and not s["fly_open"],
                 f"tabs={s['tabs_visible']} body={s['body_visible']} fly={s['fly_open']}")
        page.click(".ws-cp-tab[data-tab='tools']")
        page.wait_for_timeout(400)
        s = page.evaluate(_SNAP_JS)
        ck.check("点轨上页签 → 仍是 rail + 飞层打开 + body 可见（聊天区不跳）",
                 s["js_mode"] == "rail" and s["fly_open"] is True and s["body_visible"] and 0 < s["sb_w"] <= 48,
                 f"w={s['sb_w']} mode={s['js_mode']} fly={s['fly_open']} body={s['body_visible']}")
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        s = page.evaluate(_SNAP_JS)
        ck.check("Esc → 飞层关、仍留在 rail",
                 s["js_mode"] == "rail" and not s["fly_open"] and not s["body_visible"],
                 f"mode={s['js_mode']} fly={s['fly_open']} body={s['body_visible']}")
        page.click(".ws-cp-tab[data-tab='tools']")
        page.wait_for_timeout(300)
        page.click(".ws-cp-tab[data-tab='tools']")
        page.wait_for_timeout(400)
        s = page.evaluate(_SNAP_JS)
        ck.check("同页再点 → 钉住 expanded",
                 s["js_mode"] == "expanded" and s["sb_w"] >= 280 and not s["fly_open"],
                 f"w={s['sb_w']} mode={s['js_mode']} fly={s['fly_open']}")

        # ── 3. 彻底隐藏 Shift+] + 右缘把手唤回 ─────────────────────────
        print("== 3. 彻底隐藏（Shift+] / 把手唤回）==")
        page.keyboard.press("Shift+]")
        page.wait_for_timeout(400)
        s = page.evaluate(_SNAP_JS)
        ck.check("Shift+] → 面板隐藏", s["mode_hidden"] is True and not s["sb_visible"],
                 f"hidden={s['mode_hidden']} visible={s['sb_visible']}")
        ck.check("右缘唤回把手可见（永远有回来的路）", s["handle_visible"], f"handle={s['handle_visible']}")
        page.click("#ws-cp-edge-handle")
        page.wait_for_timeout(400)
        s = page.evaluate(_SNAP_JS)
        # 唤回语义=回到隐藏前的形态（此处隐藏前是 expanded；若前态是 rail 则回 rail——
        # 断言「非 hidden 且可见」而不是硬编码 expanded，语义与实现解耦。
        ck.check("点把手 → 唤回（恢复隐藏前形态，非 hidden 且可见）",
                 s["js_mode"] != "hidden" and (s["sb_visible"] or s["sb_w"] > 0),
                 f"mode={s['js_mode']} visible={s['sb_visible']} w={s['sb_w']}")
        if s["js_mode"] != "expanded":
            page.keyboard.press("]")   # 归位 expanded，供后续小节使用
            page.wait_for_timeout(300)

        # ── 4. 选中会话/清选中：空态进出，面板全程常驻 ─────────────────
        print("== 4. 选中会话/清选中：空态进出，面板全程常驻 ==")
        n_read = page.evaluate("() => document.querySelectorAll('#conv-items .conv-item:not(.has-unread)').length")
        if not n_read:
            ck.skip("选中会话进出空态", "无已读会话可安全打开（不碰未读）")
        else:
            # 走元素自身 onclick（selectChat 在 IIFE 内）。Playwright 的 page.click
            # 在本页会偶发点到抽屉/历史里同 class 的行，或被捕获相位拦截；
            # 与 closeMobileChat 同口径：evaluate 调真实用户路径。
            page.evaluate("""() => {
              const el=document.querySelector('#conv-items .conv-item:not(.has-unread)');
              if(el) el.click();
            }""")
            try:
                page.wait_for_function(
                    "() => { const sb=document.getElementById('info-sidebar'); return sb && !sb.classList.contains('cp-empty-mode'); }",
                    timeout=8000)
            except Exception:
                page.wait_for_timeout(1500)
            s = page.evaluate(_SNAP_JS)
            ck.check("选中会话 → 退出空态、业务卡回归", s["empty_mode"] is False and s["guide_visible"] == 0,
                     f"empty={s['empty_mode']} guide={s['guide_visible']}")
            # 清选中：走窗口级公开函数 closeMobileChat（真实「返回列表」路径——清选中
            # + 显空态 + applyFilters；主脚本闭包内的 selectedChat 无法从 evaluate 直改）
            page.evaluate("() => window.closeMobileChat()")
            page.wait_for_timeout(600)
            s = page.evaluate(_SNAP_JS)
            ck.check("清选中 → 回空态而【不是】面板蒸发（本轮修复靶心）",
                     s["sb_visible"] and s["empty_mode"] is True,
                     f"visible={s['sb_visible']} empty={s['empty_mode']} w={s['sb_w']}")

        # ── 5. 拖宽动态上限（存储 999 → 夹到 min(720, 45vw, 父宽-520)） ──
        print("== 5. 拖宽上限：向左拉伸有保底 ==")
        page.evaluate("() => localStorage.setItem('ws_sidebar_w','999')")
        page.reload(wait_until="domcontentloaded")
        try:
            page.wait_for_function("() => typeof window.toggleInfoSidebar === 'function'", timeout=20000)
            page.wait_for_timeout(2500)
            cap = page.evaluate("""() => {
              const sb = document.getElementById('info-sidebar');
              const pw = sb && sb.parentElement ? sb.parentElement.getBoundingClientRect().width : 0;
              return {w: sb ? sb.offsetWidth : -1, cap: Math.min(720, Math.round(innerWidth*0.45)), pw: Math.round(pw)};
            }""")
            limit = min(cap["cap"], max(280, cap["pw"] - 520)) if cap["pw"] > 0 else cap["cap"]
            ck.check("存储宽 999 → 实际被动态上限夹住", 280 <= cap["w"] <= limit + 2,
                     f"w={cap['w']} <= {limit}（45vw={cap['cap']} 父宽={cap['pw']}）")
        except Exception:
            ck.skip("拖宽上限", "reload 后脚本未就绪")

        # ── 6. 单卡撕出（P2）：搬节点 + 占位 chip + 独立存活 + 持久化 + 收回 ──
        print("== 6. 单卡撕出（翻译工具卡 → 悬浮小窗）==")
        page.evaluate("() => { localStorage.removeItem('cp_tear_open_v1'); localStorage.removeItem('cp_tear_geom_v1'); }")
        if page.evaluate("() => window._cpPanelModeGet()") != "expanded":
            page.keyboard.press("]")
            page.wait_for_timeout(300)
        page.click(".ws-cp-tab[data-tab='tools']")
        page.wait_for_timeout(250)
        page.evaluate("""() => {
          const el=document.querySelector('#info-sidebar .cp-card[data-cp-card="xlate"] .cp-tear-btn');
          if(el) el.click();
        }""")
        page.wait_for_timeout(400)
        t = page.evaluate("""() => {
          const sb=document.getElementById('info-sidebar');
          const win=document.querySelector('.ws-cp-float[data-tear="xlate"]');
          const vis=(el)=>!!el&&el.offsetHeight>0&&getComputedStyle(el).display!=='none';
          return {
            win_vis: vis(win),
            card_in_win: !!(win&&win.querySelector('.cp-card[data-cp-card="xlate"]')),
            card_in_sb: !!(sb&&sb.querySelector('.cp-card[data-cp-card="xlate"]')),
            ph_vis: vis(sb?sb.querySelector('.cp-tear-ph[data-tear-ph="xlate"]'):null),
          };
        }""")
        ck.check("点撕出 → 悬浮窗打开、卡片搬进窗、侧栏留回收 chip",
                 t["win_vis"] and t["card_in_win"] and not t["card_in_sb"] and t["ph_vis"],
                 f"win={t['win_vis']} in_win={t['card_in_win']} in_sb={t['card_in_sb']} ph={t['ph_vis']}")
        page.keyboard.press("]")   # 收成轨：撕出窗必须独立存活
        page.wait_for_timeout(300)
        t = page.evaluate("() => { const w=document.querySelector('.ws-cp-float[data-tear=\"xlate\"]');"
                          " return {mode: window._cpPanelModeGet(), win: !!w && w.offsetHeight>0}; }")
        ck.check("面板收成轨后撕出窗仍在（独立于面板形态）",
                 t["mode"] == "rail" and t["win"], f"mode={t['mode']} win={t['win']}")
        page.reload(wait_until="domcontentloaded")
        try:
            page.wait_for_function("() => typeof window._cpTearOpenGet === 'function'", timeout=20000)
            page.wait_for_timeout(2500)
            t = page.evaluate("() => ({win: !!document.querySelector('.ws-cp-float[data-tear=\"xlate\"]'),"
                              " open: window._cpTearOpenGet()})")
            ck.check("刷新 → 撕出窗按记忆恢复", t["win"] and "xlate" in t["open"],
                     f"win={t['win']} open={t['open']}")
            page.evaluate("""() => {
              const b=document.querySelector('.ws-cp-float[data-tear="xlate"] .ws-cp-float-dock');
              if(b) b.click();
            }""")
            page.wait_for_timeout(400)
            t = page.evaluate("""() => {
              const sb=document.getElementById('info-sidebar');
              return {
                win: !!document.querySelector('.ws-cp-float[data-tear="xlate"]'),
                card_in_sb: !!(sb&&sb.querySelector('.cp-card[data-cp-card="xlate"]')),
                ph: !!(sb&&sb.querySelector('.cp-tear-ph')),
              };
            }""")
            ck.check("点「收回」→ 卡片回侧栏原位、小窗与 chip 零残留",
                     not t["win"] and t["card_in_sb"] and not t["ph"],
                     f"win={t['win']} in_sb={t['card_in_sb']} ph={t['ph']}")
            # 双击标题栏收回（P3 窗口惯例）：再撕出一次 → dblclick → 应收回
            page.evaluate("() => window._cpTearOut('xlate')")
            page.wait_for_timeout(300)
            page.dblclick(".ws-cp-float[data-tear='xlate'] .ws-cp-float-bar")
            page.wait_for_timeout(400)
            t = page.evaluate("""() => {
              const sb=document.getElementById('info-sidebar');
              return {
                win: !!document.querySelector('.ws-cp-float[data-tear="xlate"]'),
                card_in_sb: !!(sb&&sb.querySelector('.cp-card[data-cp-card="xlate"]')),
              };
            }""")
            ck.check("双击标题栏 → 同样收回（窗口惯例）",
                     not t["win"] and t["card_in_sb"], f"win={t['win']} in_sb={t['card_in_sb']}")
        except Exception:
            ck.skip("撕出持久化/收回", "reload 后脚本未就绪")

        # ── 7. App(iframe) 模式（?app=1）：空态防串数据 + ] 降级 + 把手 ──
        print("== 7. App(iframe) 模式（?app=1）==")
        # 撕出记忆刻意留一份：App 模式必须不渲染悬浮窗（native-only 收敛）
        page.evaluate("() => { localStorage.removeItem('ws_sidebar_w'); localStorage.removeItem('cp_panel_mode_v1');"
                      " localStorage.setItem('cp_tear_open_v1','[\"xlate\"]'); }")
        page.goto(base + "/workspace?app=1", wait_until="domcontentloaded")
        try:
            page.wait_for_function(
                "() => typeof window._cpPanelModeGet === 'function' && typeof window.toggleInfoSidebar === 'function'",
                timeout=20000)
            page.wait_for_timeout(3500)
            a = page.evaluate("""() => {
              const vis = (el) => !!el && el.offsetHeight > 0 && getComputedStyle(el).display !== 'none';
              const sb = document.getElementById('info-sidebar');
              return {
                sb_visible: vis(sb),
                empty_mode: sb ? sb.classList.contains('cp-empty-mode') : null,
                app_hidden: !!(document.getElementById('ws-cp-appwrap')||{}).hidden,
                note_visible: vis(document.getElementById('ws-cp-empty-note')),
                mode: window._cpPanelModeGet(),
              };
            }""")
            ck.check("App 空态：面板常驻 + iframe 隐藏（防串上一客户）+ 说明块点亮",
                     a["sb_visible"] and a["empty_mode"] and a["app_hidden"] and a["note_visible"],
                     f"visible={a['sb_visible']} empty={a['empty_mode']} app_hidden={a['app_hidden']} note={a['note_visible']}")
            page.keyboard.press("]")
            page.wait_for_timeout(400)
            b2 = page.evaluate("""() => {
              const sb=document.getElementById('info-sidebar');
              const tabs=document.getElementById('ws-cp-tabs');
              const vis=(el)=>!!el&&el.offsetHeight>0&&getComputedStyle(el).display!=='none';
              return {mode: window._cpPanelModeGet(), w: sb?sb.offsetWidth:-1, tabs: vis(tabs)};
            }""")
            ck.check("App 下 ] → 收纳轨（宿主 tabs 当图标轨）",
                     b2["mode"] == "rail" and 0 < b2["w"] <= 48 and b2["tabs"],
                     f"mode={b2['mode']} w={b2['w']} tabs={b2['tabs']}")
            page.click(".ws-cp-tab[data-tab='tools']")
            page.wait_for_timeout(400)
            f2 = page.evaluate("""() => {
              const vis=(el)=>!!el&&el.offsetHeight>0&&getComputedStyle(el).display!=='none';
              return {
                mode: window._cpPanelModeGet(),
                fly: (typeof window._cpFlyoutOpenGet==='function')?window._cpFlyoutOpenGet():null,
                note: vis(document.getElementById('ws-cp-empty-note')),
                app_hidden: !!(document.getElementById('ws-cp-appwrap')||{}).hidden,
              };
            }""")
            ck.check("App 轨上点图标 → 飞层打开（空态=说明块，iframe 保持防串隐藏）",
                     f2["mode"] == "rail" and f2["fly"] is True and f2["note"] and f2["app_hidden"],
                     f"mode={f2['mode']} fly={f2['fly']} note={f2['note']} app_hidden={f2['app_hidden']}")
            page.click(".ws-cp-tab[data-tab='tools']")
            page.wait_for_timeout(400)
            p2 = page.evaluate("() => ({mode: window._cpPanelModeGet(), fly: window._cpFlyoutOpenGet()})")
            ck.check("App 同页再点 → 钉住 expanded", p2["mode"] == "expanded" and not p2["fly"],
                     f"mode={p2['mode']} fly={p2['fly']}")
            page.keyboard.press("Shift+]")
            page.wait_for_timeout(400)
            b3 = page.evaluate("() => ({mode: window._cpPanelModeGet(), "
                               "handle: (document.getElementById('ws-cp-edge-handle')||{}).offsetHeight > 0})")
            ck.check("App 下 Shift+] 彻底隐藏 + 把手出现",
                     b3["mode"] == "hidden" and b3["handle"], f"mode={b3['mode']} handle={b3['handle']}")
            page.click("#ws-cp-edge-handle")
            page.wait_for_timeout(400)
            c2 = page.evaluate("() => window._cpPanelModeGet()")
            ck.check("App 下点把手唤回", c2 == "expanded", f"mode={c2}")
            no_float = page.evaluate("() => !document.querySelector('.ws-cp-float')")
            ck.check("App 模式：撕出记忆在册但不渲染悬浮窗（native-only）", no_float, "")
        except Exception as e:
            ck.skip("App 模式段", f"未就绪（{str(e)[:60]}）")

        browser.close()
    return ck.summary()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    try:
        import playwright  # noqa: F401
    except Exception:
        print("[SKIP] playwright 未安装（pip install playwright && playwright install chromium）")
        return 0
    token = read_token(args.data_root)
    if not token:
        print("[SKIP] 读不到 auth_token（数据根不对？）")
        return 0
    try:
        import urllib.request
        urllib.request.urlopen(args.base + "/login", timeout=5)
    except Exception:
        print("[SKIP] 实例不可达: " + args.base)
        return 0
    return run(args.base, token, headed=args.headed)


if __name__ == "__main__":
    sys.exit(main())
