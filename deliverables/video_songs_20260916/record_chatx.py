#!/usr/bin/env python3
"""按 curriculum.json 的 footage_actions 在本机智聊工作台真录屏（每个 clip 一个 webm）。

  python record_chatx.py E1            # 录该集全部 clip → out/E1/footage_<clip>.webm
  python record_chatx.py E1 --only c_thread --allow-send

步骤 DSL：
  ["wait", ms]
  ["caption", 文案] / ["caption", 文案, hold_ms]   底部得点说明
  ["point_text", 文本] / ["point_text", 文本, 说明]  指针+高亮框停顿（不点）
  ["point_css", css] / ["point_css", css, 说明]
  ["point_css_text", css, 文本] / [..., 说明]
  ["click_text", 文本] / ["click_text", 文本, 说明]  先指针得点再点
  ["click_css_text", css, 文本] / [..., 说明]
  ["click_css", css] / ["click_css", css, 说明]
  ["open_chat", 会话名或 conversation_id] / [..., 说明]  精确打开（优先私聊非 bot）
  ["hover_text", 文本] / ["press", 键] / ["scroll_messages", dy] / ["goto", 路径]
  ["type", 选择器, 文本] / ["send_demo", 文本] / ["send_demo", 文本, 说明]
send_demo 仅当当前会话名命中 DEMO_PEERS 且 --allow-send 才真发。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from chatx_session import BASE, DEMO_PEERS, can_send, latest_video, session, url

ROOT = Path(__file__).resolve().parent
POINTER_JS = (ROOT / "pointer_overlay.js").read_text(encoding="utf-8")


def dur(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def inject_pointer(page) -> None:
    page.evaluate(POINTER_JS)


def _in_viewport(el) -> bool:
    try:
        box = el.bounding_box()
        if not box:
            return False
        return (
            box["x"] >= -4
            and box["y"] >= -4
            and box["x"] + box["width"] <= 1924
            and box["y"] + box["height"] <= 1084
        )
    except Exception:
        return False


def _first_visible(page, text: str):
    """优先视口内可点控件，再退回任意可见文本节点。"""
    prefer = page.locator(
        "button, a, [role=button], .ftab, .adk-item, .conv-item, .conv-tag-chip, "
        ".hdr-btn, .tiny-btn, .btn, select, .mode-select, .ws-cp-tab"
    ).filter(has_text=text)
    hits = []
    for i in range(min(prefer.count(), 16)):
        el = prefer.nth(i)
        try:
            if el.is_visible():
                hits.append(el)
        except Exception:
            continue
    for el in hits:
        if _in_viewport(el):
            return el
    if hits:
        return hits[0]
    loc = page.get_by_text(text, exact=False)
    n = loc.count()
    for i in range(min(n, 16)):
        el = loc.nth(i)
        try:
            if el.is_visible() and _in_viewport(el):
                return el
        except Exception:
            continue
    for i in range(min(n, 16)):
        el = loc.nth(i)
        try:
            if el.is_visible():
                return el
        except Exception:
            continue
    return None


def _first_css(page, css: str):
    """多选择器逗号分隔时，优先返回视口内可见命中。"""
    candidates = []
    for part in [p.strip() for p in css.split(",") if p.strip()]:
        loc = page.locator(part)
        for i in range(min(loc.count(), 8)):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    candidates.append(el)
            except Exception:
                continue
    for el in candidates:
        if _in_viewport(el):
            return el
    return candidates[0] if candidates else None


def _first_css_text(page, css: str, text: str):
    loc = page.locator(css).filter(has_text=text)
    hits = []
    for i in range(min(loc.count(), 12)):
        el = loc.nth(i)
        try:
            if el.is_visible():
                hits.append(el)
        except Exception:
            continue
    for el in hits:
        if _in_viewport(el):
            return el
    return hits[0] if hits else None


def _point_el(page, el, caption: str | None = None, hold_ms: int = 900) -> None:
    if el is None:
        return
    try:
        el.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass
    hold = max(int(hold_ms), 1100)
    page.evaluate(
        """async ({caption, hold}) => {
          const el = window.__ctpTarget;
          if (window.__chatxPointer && el) await window.__chatxPointer.point(el, caption||'', hold);
        }""",
        {"caption": caption or "", "hold": hold},
    )


def _bind_target(page, el) -> None:
    """把 Playwright ElementHandle 挂到 window.__ctpTarget 供 pointer JS 读。"""
    try:
        handle = el.element_handle()
        if handle is None:
            el.evaluate("node => { window.__ctpTarget = node; }")
        else:
            page.evaluate("(node) => { window.__ctpTarget = node; }", handle)
    except Exception:
        try:
            el.evaluate("node => { window.__ctpTarget = node; }")
        except Exception:
            page.evaluate("() => { window.__ctpTarget = null; }")


def point_and_maybe_click(page, el, caption: str | None, *, click: bool, hold_ms: int = 1500) -> None:
    if el is None:
        print(f"    ~ 得点目标未找到「{caption}」")
        return
    _bind_target(page, el)
    _point_el(page, el, caption, hold_ms=hold_ms)
    if click:
        try:
            if not _in_viewport(el):
                try:
                    el.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
            el.click(timeout=4000, force=not _in_viewport(el))
        except Exception as e:  # noqa: BLE001
            try:
                el.click(timeout=3000, force=True)
            except Exception:
                print(f"    ~ 点击软失败: {str(e)[:100]}")
        page.wait_for_timeout(350)
        page.evaluate("() => { if(window.__chatxPointer) window.__chatxPointer.clearHighlight(); }")


def open_chat(page, key: str, caption: str | None = None) -> bool:
    """按 conversation_id（推荐）或显示名打开会话。DOM：.conv-item[data-key=cid]。"""
    key = (key or "").strip()
    # 已知演示会话快捷映射
    aliases = {
        "BOUNDLESS": "telegram:8244899900:8506426282",
        "演示": "telegram:8244899900:8506426282",
        "ZH_DEMO": "telegram:6834964252:me",
        "智聊支持": "telegram:6834964252:me",
        "风险会话": "risk",  # 特殊：按风险标签找
    }
    if key in ("风险会话", "risk"):
        chip = page.locator(".conv-item:has(.conv-tag-chip)").first
        if chip.count():
            point_and_maybe_click(page, chip, caption or "打开带【风险】标签的真实会话", click=True, hold_ms=1300)
            page.wait_for_timeout(2200)
            print("    ✓ 打开风险会话")
            return True
        print("    ~ 无风险标签会话")
        return False
    cid = aliases.get(key, key)
    if ":" not in cid:
        # 按显示名在列表里找第一行（排除明显群）
        loc = page.locator(".conv-item").filter(has_text=key)
        hit = None
        for i in range(min(loc.count(), 12)):
            el = loc.nth(i)
            try:
                dk = el.get_attribute("data-key") or ""
                if dk.split(":")[-1].startswith("-"):
                    continue
                if el.is_visible():
                    hit = el
                    cid = dk or cid
                    break
            except Exception:
                continue
        if hit is None:
            print(f"    ~ open_chat 未找到「{key}」")
            return False
        point_and_maybe_click(page, hit, caption or f"打开会话【{key}】", click=True, hold_ms=1100)
        page.wait_for_timeout(2200)
        print(f"    ✓ 打开 {cid}")
        return True

    el = page.locator(f'.conv-item[data-key="{cid}"]').first
    if el.count() == 0:
        # 滚列表 / 搜索
        page.evaluate(
            """(cid) => {
              if (typeof window.__wsFocusConv === 'function') {
                try { window.__wsFocusConv(cid); return 'focus'; } catch (e) {}
              }
              if (typeof window.__desktopOpenConversation === 'function') {
                try { window.__desktopOpenConversation(cid); return 'desktop'; } catch (e) {}
              }
              return 'none';
            }""",
            cid,
        )
        page.wait_for_timeout(1500)
        el = page.locator(f'.conv-item[data-key="{cid}"]').first
    if el.count() == 0:
        print(f"    ~ open_chat 列表无 {cid}")
        return False
    # 先滚到可见再得点+点击
    try:
        el.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    point_and_maybe_click(
        page, el, caption or "打开【BOUNDLESS】演示会话（真聊）", click=True, hold_ms=1200,
    )
    page.wait_for_timeout(2200)
    print(f"    ✓ 打开 {cid}")
    return True


NAV_OPS = {"goto", "open_chat", "open_nth_chat", "wait_chat_ready", "ensure_assist", "cp_tab", "wait"}


def run_steps(page, steps: list, *, allow_send: bool) -> dict:
    """执行步骤；返回 marks：content_offset＝导航铺垫结束、真正教学动作开始的时刻（相对录像起点）。
    拼装器用它做段落起点，避免每段都从「空工作台→点会话」重播（2026-09-17 R1/R3 复盘根因）。"""
    inject_pointer(page)
    marks = {"content_offset": None, "steps": []}
    t0 = page.t_created
    for st in steps:
        op, *args = st
        if marks["content_offset"] is None and op not in NAV_OPS:
            marks["content_offset"] = round(time.monotonic() - t0, 2)
        marks["steps"].append({"t": round(time.monotonic() - t0, 2), "op": op})
        try:
            if op == "wait":
                page.wait_for_timeout(int(args[0]))
            elif op == "caption":
                text = args[0]
                hold = int(args[1]) if len(args) > 1 else 2200
                page.evaluate(
                    """({t,h}) => { if(window.__chatxPointer) window.__chatxPointer.caption(t,h); }""",
                    {"t": text, "h": hold},
                )
                page.wait_for_timeout(min(hold, 1800))
            elif op == "point_text":
                el = _first_visible(page, args[0])
                cap = args[1] if len(args) > 1 else f"看这里【{args[0]}】"
                point_and_maybe_click(page, el, cap, click=False, hold_ms=1600)
            elif op == "point_in_card":
                # 卡内文案（含 open shadow）：["point_in_card", "voice", "生成语音", 说明?]
                key, text = args[0], args[1]
                cap = args[2] if len(args) > 2 else f"看【{text}】"
                card = page.locator(f'[data-cp-card="{key}"]').first
                el = None
                if card.count():
                    try:
                        card.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    # Playwright 默认穿 open shadow
                    loc = card.get_by_text(text, exact=False)
                    for i in range(min(loc.count(), 8)):
                        cand = loc.nth(i)
                        try:
                            if cand.is_visible():
                                el = cand
                                break
                        except Exception:
                            continue
                    if el is None:
                        # 退回：卡头 / 卡体本身
                        el = card
                if el is None:
                    print(f"    ~ point_in_card 未找到 {key}/{text}")
                else:
                    point_and_maybe_click(page, el, cap, click=False, hold_ms=1600)
            elif op == "point_css":
                el = _first_css(page, args[0])
                cap = args[1] if len(args) > 1 else None
                if el is None:
                    print(f"    ~ point_css 未找到 {args[0]}")
                    continue
                point_and_maybe_click(page, el, cap, click=False, hold_ms=1600)
            elif op == "point_css_text":
                el = _first_css_text(page, args[0], args[1])
                cap = args[2] if len(args) > 2 else f"看这里【{args[1]}】"
                point_and_maybe_click(page, el, cap, click=False, hold_ms=1600)
            elif op == "click_text":
                el = _first_visible(page, args[0])
                cap = args[1] if len(args) > 1 else f"点击【{args[0]}】"
                point_and_maybe_click(page, el, cap, click=True, hold_ms=1000)
            elif op == "click_css_text":
                el = _first_css_text(page, args[0], args[1])
                cap = args[2] if len(args) > 2 else f"点击【{args[1]}】"
                point_and_maybe_click(page, el, cap, click=True, hold_ms=1000)
            elif op == "click_css":
                el = _first_css(page, args[0])
                cap = args[1] if len(args) > 1 else None
                if el is None:
                    print(f"    ~ click_css 未找到 {args[0]}")
                    continue
                point_and_maybe_click(page, el, cap, click=True, hold_ms=1000)
            elif op == "open_chat":
                cap = args[1] if len(args) > 1 else None
                open_chat(page, args[0], cap)
            elif op == "wait_chat_ready":
                # 等会话真正打开：出现回复框或气泡，且不是「正在连接…」闪屏
                timeout_ms = int(args[0]) if args else 18000
                try:
                    page.wait_for_function(
                        """() => {
                          const t = (document.body && document.body.innerText) || '';
                          if (t.includes('正在连接')) return false;
                          // 要有「带文字」的真气泡——骨架屏占位气泡没有文字（2026-09-17 R4 t=11 抽帧实锤）
                          const bubbles = document.querySelectorAll('.msg-bubble, .bubble, .msg, .message-body');
                          let real = 0;
                          for (const b of bubbles) { if (((b.innerText || '').trim()).length > 4) real++; }
                          if (real >= 2) return true;
                          if (document.querySelector('.skeleton, .msg-skeleton, [class*=skeleton]')) return false;
                          return real >= 1;
                        }""",
                        timeout=timeout_ms,
                    )
                    page.wait_for_timeout(900)
                    print("    ✓ wait_chat_ready")
                except Exception as e:  # noqa: BLE001
                    print(f"    ~ wait_chat_ready 超时: {str(e)[:100]}")
                    page.wait_for_timeout(1500)
            elif op == "open_nth_chat":
                # 打开列表第 n 个可见会话（0-based），用于多平台浏览，不强制 BOUNDLESS
                n = int(args[0])
                cap = args[1] if len(args) > 1 else f"打开第 {n+1} 个真实会话"
                loc = page.locator(".conv-item")
                hit = None
                seen = 0
                for i in range(min(loc.count(), 40)):
                    el = loc.nth(i)
                    try:
                        if not el.is_visible():
                            continue
                        if seen == n:
                            hit = el
                            break
                        seen += 1
                    except Exception:
                        continue
                point_and_maybe_click(page, hit, cap, click=True, hold_ms=1300)
                page.wait_for_timeout(2200)
            elif op == "ensure_assist":
                # 宽屏业务助手有 rail/expanded/hidden 三态；录屏必须落到 expanded
                # （rail 下卡体 dormant，点 data-cp-card 会 scroll 超时）。
                opened = page.evaluate(
                    """() => {
                      const sb = document.getElementById('info-sidebar');
                      const w = sb ? sb.getBoundingClientRect().width : 0;
                      const mode = (typeof window._cpPanelModeGet === 'function')
                        ? window._cpPanelModeGet() : null;
                      if (mode === 'expanded' && w > 200) return 'already:' + Math.round(w);
                      // 优先官方 API（挂在 window 上的出口）
                      if (typeof window.toggleInfoSidebar === 'function') {
                        // rail/hidden → expanded；若已是 expanded 会误切回 rail，故先判 mode
                        if (mode === 'rail' || mode === 'hidden' || w < 160) {
                          if (mode === 'hidden' && typeof window._cpPanelRestore === 'function') {
                            window._cpPanelRestore();
                          } else {
                            window.toggleInfoSidebar();
                          }
                          // 若仍非 expanded，再点一次（toggle 在 expanded↔rail 间切换）
                          const m2 = (typeof window._cpPanelModeGet === 'function')
                            ? window._cpPanelModeGet() : null;
                          if (m2 === 'rail') window.toggleInfoSidebar();
                          return 'toggled:' + ((typeof window._cpPanelModeGet === 'function')
                            ? window._cpPanelModeGet() : '?');
                        }
                      }
                      const expandBtn = document.querySelector(
                        '.ws-cp-rail-btn[onclick*="toggleInfoSidebar"], #ws-cp-edge-handle'
                      );
                      if (expandBtn) { expandBtn.click(); return 'rail-btn'; }
                      const btn = document.querySelector('#info-toggle-btn');
                      if (btn) { btn.click(); return 'info-toggle'; }
                      return 'missing';
                    }"""
                )
                print(f"    ensure_assist → {opened}")
                page.wait_for_timeout(900)
                inject_pointer(page)
                el = _first_css(page, "#info-sidebar .ws-cp-tab, #info-toggle-btn, .ws-cp-tab")
                if el is not None:
                    point_and_maybe_click(
                        page, el,
                        args[0] if args else "打开【业务助手】",
                        click=False, hold_ms=1200,
                    )
            elif op == "expand_card":
                # 折叠卡头是 button.cp-card-hd[data-cp-toggle=…]；点 section 本身常点空。
                # 展开后断言：!collapsed + 高度 + 子控件 marker（可穿 shadow）。
                key = args[0]
                cap = args[1] if len(args) > 1 else f"展开【{key}】"
                markers = {
                    "voice": ("生成语音", "试听", "跟随翻译发声"),
                    "goal": ("设定目标", "今日", "采纳", "暂停全部摸底目标"),
                    "chain": ("启动工作链", "管理工作链", "SOP"),
                    "persona": ("绑定", "人设", "切换"),
                    "xlate": ("翻译", "译", "源语言"),
                    "draft": ("生成", "草稿", "采纳"),
                    "chain": ("工作链", "启动", "管理", "SOP"),
                }.get(key, ())
                hd = _first_css(page, f'button.cp-card-hd[data-cp-toggle="{key}"], [data-cp-toggle="{key}"]')
                card = _first_css(page, f'[data-cp-card="{key}"]')
                if hd is None and card is None:
                    print(f"    ✗ expand_card 未找到 {key}")
                    raise SystemExit(f"expand_card missing: {key}")
                # 先滚到侧栏内可见，再得点卡头
                page.evaluate(
                    """(key) => {
                      const card = document.querySelector('[data-cp-card="'+key+'"]');
                      if (card) card.scrollIntoView({block:'center', inline:'nearest'});
                    }""",
                    key,
                )
                page.wait_for_timeout(300)
                target = hd if hd is not None else card
                # 卡已展开（如 goal 默认开）就只得点不点击——点击会把它折回去再弹开，画面闪
                already_open = page.evaluate(
                    """(key) => {
                      const card = document.querySelector('[data-cp-card="'+key+'"]');
                      return !!(card && !card.classList.contains('collapsed') && card.getBoundingClientRect().height > 80);
                    }""",
                    key,
                )
                point_and_maybe_click(page, target, cap, click=not already_open, hold_ms=1400)
                page.wait_for_timeout(700)
                # 仍折叠则强制再点一次卡头（第一次可能点到撕出按钮）
                still = page.evaluate(
                    """(key) => {
                      const card = document.querySelector('[data-cp-card="'+key+'"]');
                      if (!card) return 'missing';
                      if (!card.classList.contains('collapsed')) return 'open';
                      const hd = document.querySelector('[data-cp-toggle="'+key+'"]');
                      if (hd) hd.click();
                      return card.classList.contains('collapsed') ? 'still' : 'opened';
                    }""",
                    key,
                )
                print(f"    expand_card {key} → {still}")
                page.wait_for_timeout(500)
                st = page.evaluate(
                    """({key, markers}) => {
                      const card = document.querySelector('[data-cp-card="'+key+'"]');
                      if (!card) return {ok:false, reason:'missing'};
                      const r = card.getBoundingClientRect();
                      if (card.classList.contains('collapsed')) return {ok:false, reason:'collapsed'};
                      if (r.height < 80) return {ok:false, reason:'too_short:'+Math.round(r.height)};
                      if (!markers || !markers.length) return {ok:true, reason:'no_marker_req', h:Math.round(r.height)};
                      const host = card.querySelector('cp-voice, cp-goal, cp-persona, cp-xlate, cp-draft, [id^="ws-cp-"]');
                      const roots = [card];
                      if (host) { roots.push(host); if (host.shadowRoot) roots.push(host.shadowRoot); }
                      let blob = '';
                      for (const root of roots) {
                        blob += ' ' + (root.innerText || root.textContent || '');
                      }
                      blob = blob.replace(/\\s+/g, ' ');
                      const hit = markers.find(m => blob.includes(m));
                      return hit
                        ? {ok:true, reason:'marker:'+hit, h:Math.round(r.height)}
                        : {ok:false, reason:'no_marker', sample: blob.trim().slice(0,120), h:Math.round(r.height)};
                    }""",
                    {"key": key, "markers": list(markers)},
                )
                print(f"    expand_card assert → {st}")
                if not st.get("ok"):
                    raise SystemExit(f"expand_card assert fail {key}: {st}")
            elif op == "cp_tab":
                # 页签在 rail/窄宽下常只剩图标，visible 文本为空；用 data-tab 最稳。
                name = args[0]
                cap = args[1] if len(args) > 1 else f"切到【{name}】"
                tab_map = {
                    "回复台": "reply", "reply": "reply",
                    "客户关系": "customer", "customer": "customer", "关系": "customer",
                    "工具箱": "tools", "tools": "tools", "工具": "tools",
                }
                tab_key = tab_map.get(name, name)
                el = _first_css(page, f'button.ws-cp-tab[data-tab="{tab_key}"]')
                if el is None:
                    el = _first_css_text(page, "button.ws-cp-tab, .ws-cp-tab", name)
                if el is None:
                    # 最后兜底：JS 点 data-tab
                    clicked = page.evaluate(
                        """(tab) => {
                          const b = document.querySelector('button.ws-cp-tab[data-tab="'+tab+'"]');
                          if (!b) return false;
                          b.click();
                          return true;
                        }""",
                        tab_key,
                    )
                    print(f"    cp_tab JS → {tab_key} {'ok' if clicked else 'FAIL'}")
                    if not clicked:
                        raise SystemExit(f"cp_tab missing: {name}")
                    page.wait_for_timeout(1000)
                else:
                    point_and_maybe_click(page, el, cap, click=True, hold_ms=1200)
                    page.wait_for_timeout(1000)
            elif op == "hover_text":
                el = _first_visible(page, args[0])
                if el is not None:
                    point_and_maybe_click(page, el, args[1] if len(args) > 1 else None, click=False, hold_ms=800)
                    el.hover(timeout=4000)
            elif op == "press":
                page.keyboard.press(args[0])
            elif op == "scroll_messages":
                page.mouse.move(960, 520)
                page.mouse.wheel(0, int(args[0]))
            elif op == "goto":
                page.goto(url(args[0]), wait_until="domcontentloaded", timeout=60000)   # 带主题钉，整页重载不掉色
                page.wait_for_timeout(2500)
                inject_pointer(page)
            elif op == "type":
                page.locator(args[0]).first.click(timeout=4000)
                page.keyboard.type(args[1], delay=55)
            elif op == "send_demo":
                peer = page.evaluate(
                    """() => {
                      const h = document.querySelector('#chat-header,.chat-header') || {};
                      const active = document.querySelector('.conv-item.active,[aria-current=true].conv-item');
                      const dk = active ? (active.getAttribute('data-key') || '') : '';
                      return ((h.innerText || '') + '\\n' + dk).trim();
                    }"""
                )
                box = page.locator("#reply-ta")
                point_and_maybe_click(
                    page, box.first if box.count() else None,
                    args[1] if len(args) > 1 else "在回复台输入，真发给演示会话",
                    click=True, hold_ms=900,
                )
                page.keyboard.type(args[0], delay=55)
                page.wait_for_timeout(400)
                # 额外认 conversation_id，避免头栏换行导致白名单漏判
                allow = can_send(peer, allow_send) or (
                    allow_send and "8506426282" in peer
                ) or (
                    allow_send and "6834964252:me" in peer
                )
                if allow:
                    page.keyboard.press("Enter")
                    print(f"    ✓ 真发（白名单会话「{peer[:50].replace(chr(10),' ')}」）")
                    page.wait_for_timeout(2500)
                else:
                    print(f"    ~ 只打字不发（会话「{peer[:50].replace(chr(10),' ')}」不在白名单或未 --allow-send）")
                    page.wait_for_timeout(1200)
            else:
                print(f"    ~ 未知步骤 {op}")
        except Exception as e:  # noqa: BLE001
            print(f"    ~ 步骤 {st} 软失败: {str(e)[:140]}")
    if marks["content_offset"] is None:
        marks["content_offset"] = round(time.monotonic() - t0, 2)
    return marks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("episode")
    ap.add_argument("--only")
    ap.add_argument("--allow-send", action="store_true")
    a = ap.parse_args()
    cur = json.loads((ROOT / "curriculum.json").read_text(encoding="utf-8"))
    ep = next(e for e in cur["episodes"] if e["id"] == a.episode)
    out = ROOT / "out" / ep["id"]
    raw = out / "raw"
    ok = 0
    for clip in ep["footage_actions"]:
        if a.only and clip["clip"] != a.only:
            continue
        print(f"== {ep['id']} {clip['clip']}: {clip['desc']}")
        with session(raw) as (page, ctx):
            page.wait_for_timeout(1500)
            try:
                el = _first_visible(page, "跳过引导")
                if el is not None:
                    el.click(timeout=2000)
                    page.wait_for_timeout(600)
            except Exception:
                pass
            inject_pointer(page)
            ready_offset = time.monotonic() - page.t_created
            marks = run_steps(page, clip["steps"], allow_send=a.allow_send)
            page.wait_for_timeout(800)
            t_end = time.monotonic() - page.t_created
        tgt = latest_video(raw, out / f"footage_{clip['clip']}.webm")
        d = dur(tgt)
        if d < 3:
            raise SystemExit(f"[FAIL] {tgt.name} 仅 {d:.1f}s")
        # 实测（2026-09-17 R1 a_pref 抽帧对照）：monotonic 偏移与画面时间轴一致；webm 比 t_end 长出的部分是
        # 关闭 context 时的尾部填充，不在开头——所以不要用 d - t_end 去修正起点。只留 0.5 s 余量。
        tail_pad = max(0.0, d - t_end)
        content_offset = max(float(marks["content_offset"]), ready_offset) + 0.5
        print(f"    video tail pad {tail_pad:.1f}s")
        if d - content_offset < 6:
            raise SystemExit(f"[FAIL] {tgt.name} 教学画面仅 {d - content_offset:.1f}s（content_offset={content_offset:.1f}）")
        tgt.with_suffix(".json").write_text(
            json.dumps(
                {"clip": clip["clip"], "desc": clip["desc"], "duration": d,
                 "ready_offset": round(ready_offset, 2),
                 "content_offset": round(content_offset, 2),
                 "usable_end": round(min(d, t_end), 2),
                 "marks": marks["steps"]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"  [OK] {tgt.name} {d:.1f}s（就绪 {ready_offset:.1f}s · 教学画面 {content_offset:.1f}s 起，共 {d - content_offset:.1f}s）")
        ok += 1
    print(f"DONE {ok} clips")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    sys.exit(main())
