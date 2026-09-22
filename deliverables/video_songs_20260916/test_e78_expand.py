#!/usr/bin/env python3
"""活体闸：E7/E8 展开路径（ensure_assist → cp_tab → expand_card 断言）。

不录像，只验证 DSL 关键路径在本机智聊上能真正展开并看到子控件。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from chatx_session import session  # noqa: E402
from record_chatx import inject_pointer  # noqa: E402
import record_chatx as rc  # noqa: E402


def run_steps(page, steps) -> None:
    """复用 record_chatx 的 step 解释器，但不启录像。"""
    # 直接调用内部逻辑：构造最小 run_clip 路径太重，这里内联关键 ops
    for step in steps:
        op, *args = step
        if op == "wait":
            page.wait_for_timeout(int(args[0]))
        elif op == "open_nth_chat":
            n = int(args[0])
            loc = page.locator(".conv-item")
            seen = 0
            hit = None
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
            if hit is None:
                raise SystemExit("open_nth_chat miss")
            hit.click(timeout=5000)
            page.wait_for_timeout(1800)
        elif op == "ensure_assist":
            # 走与录像同一段 evaluate：通过伪造一次 DSL 调用
            rc_run_one(page, ["ensure_assist"])
        elif op == "cp_tab":
            rc_run_one(page, ["cp_tab", *args])
        elif op == "expand_card":
            rc_run_one(page, ["expand_card", *args])
        else:
            raise SystemExit(f"unsupported op in smoke: {op}")


def rc_run_one(page, step) -> None:
    """借用 record_chatx.run_steps 里单步；用临时 footage runner 太重，复制 dispatch。"""
    # 最稳：调用私有循环的一段——直接 exec 一个迷你 clip
    from record_chatx import point_and_maybe_click, _first_css, _first_css_text  # noqa: F401

    op, *args = step
    # 把 ensure/cp_tab/expand 三段逻辑通过一次「伪 clip」：读源码太脆
    # 改为：临时 monkey 调用 page + 复制关键分支（与 record 保持同步的最小集）
    if op == "ensure_assist":
        opened = page.evaluate(
            """() => {
              const sb = document.getElementById('info-sidebar');
              const w = sb ? sb.getBoundingClientRect().width : 0;
              const mode = (typeof window._cpPanelModeGet === 'function')
                ? window._cpPanelModeGet() : null;
              if (mode === 'expanded' && w > 200) return 'already:' + Math.round(w);
              if (typeof window.toggleInfoSidebar === 'function') {
                if (mode === 'rail' || mode === 'hidden' || w < 160) {
                  if (mode === 'hidden' && typeof window._cpPanelRestore === 'function') {
                    window._cpPanelRestore();
                  } else {
                    window.toggleInfoSidebar();
                  }
                  const m2 = (typeof window._cpPanelModeGet === 'function')
                    ? window._cpPanelModeGet() : null;
                  if (m2 === 'rail') window.toggleInfoSidebar();
                  return 'toggled:' + ((typeof window._cpPanelModeGet === 'function')
                    ? window._cpPanelModeGet() : '?');
                }
              }
              return 'noop:' + mode + ':' + Math.round(w);
            }"""
        )
        print("  ensure_assist", opened)
        page.wait_for_timeout(600)
        return
    if op == "cp_tab":
        name = args[0]
        tab_map = {
            "回复台": "reply", "reply": "reply",
            "客户关系": "customer", "customer": "customer",
            "工具箱": "tools", "tools": "tools",
        }
        tab_key = tab_map.get(name, name)
        ok = page.evaluate(
            """(tab) => {
              const b = document.querySelector('button.ws-cp-tab[data-tab="'+tab+'"]');
              if (!b) return false;
              b.click();
              return true;
            }""",
            tab_key,
        )
        print("  cp_tab", tab_key, ok)
        if not ok:
            raise SystemExit(f"cp_tab fail {name}")
        page.wait_for_timeout(800)
        return
    if op == "expand_card":
        key = args[0]
        markers = {
            "voice": ("生成语音", "试听", "跟随翻译发声"),
            "goal": ("设定目标", "今日", "采纳", "暂停全部摸底目标"),
        }.get(key, ())
        page.evaluate(
            """(key) => {
              const card = document.querySelector('[data-cp-card="'+key+'"]');
              if (card) card.scrollIntoView({block:'center'});
              const hd = document.querySelector('[data-cp-toggle="'+key+'"]');
              if (hd) hd.click();
            }""",
            key,
        )
        page.wait_for_timeout(800)
        page.evaluate(
            """(key) => {
              const card = document.querySelector('[data-cp-card="'+key+'"]');
              if (card && card.classList.contains('collapsed')) {
                const hd = document.querySelector('[data-cp-toggle="'+key+'"]');
                if (hd) hd.click();
              }
            }""",
            key,
        )
        page.wait_for_timeout(600)
        st = page.evaluate(
            """({key, markers}) => {
              const card = document.querySelector('[data-cp-card="'+key+'"]');
              if (!card) return {ok:false, reason:'missing'};
              const r = card.getBoundingClientRect();
              if (card.classList.contains('collapsed')) return {ok:false, reason:'collapsed'};
              if (r.height < 80) return {ok:false, reason:'too_short:'+Math.round(r.height)};
              const host = card.querySelector('cp-voice, cp-goal, [id^="ws-cp-"]');
              const roots = [card];
              if (host) { roots.push(host); if (host.shadowRoot) roots.push(host.shadowRoot); }
              let blob = '';
              for (const root of roots) blob += ' ' + (root.innerText || root.textContent || '');
              blob = blob.replace(/\\s+/g, ' ');
              const hit = markers.find(m => blob.includes(m));
              return hit
                ? {ok:true, reason:'marker:'+hit, h:Math.round(r.height)}
                : {ok:false, reason:'no_marker', sample: blob.trim().slice(0,120), h:Math.round(r.height)};
            }""",
            {"key": key, "markers": list(markers)},
        )
        print("  expand_card", key, st)
        if not st.get("ok"):
            raise SystemExit(f"assert fail {key}: {st}")
        return
    raise SystemExit(f"bad op {op}")


def main() -> int:
    cases = [
        ("E7-voice", [
            ["open_nth_chat", 0],
            ["ensure_assist"],
            ["cp_tab", "工具箱"],
            ["expand_card", "voice"],
        ]),
        ("E8-goal", [
            ["open_nth_chat", 0],
            ["ensure_assist"],
            ["cp_tab", "客户关系"],
            ["expand_card", "goal"],
        ]),
    ]
    fails = []
    with session() as (page, _):
        inject_pointer(page)
        for name, steps in cases:
            print(f"-- {name}")
            try:
                for st in steps:
                    if st[0] == "open_nth_chat":
                        n = int(st[1])
                        loc = page.locator(".conv-item")
                        seen = 0
                        hit = None
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
                        if hit is None:
                            raise SystemExit("no chat")
                        hit.click(timeout=5000)
                        page.wait_for_timeout(1800)
                    else:
                        rc_run_one(page, st)
                print(f"  PASS {name}")
            except SystemExit as e:
                print(f"  FAIL {name}: {e}")
                fails.append(f"{name}:{e}")
            except Exception as e:  # noqa: BLE001
                print(f"  FAIL {name}: {e}")
                fails.append(f"{name}:{e}")
    print("== test_e78_expand ==")
    print("RESULT", "FAIL" if fails else "PASS", fails or "")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
