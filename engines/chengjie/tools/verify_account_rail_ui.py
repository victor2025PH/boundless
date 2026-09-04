# -*- coding: utf-8 -*-
"""账号切换 rail/dock + scoped 账号视角 真浏览器验证（Playwright；2026-07-29 沉淀）。

**为什么需要它**：2026-07-29 实录事故——坐席点抽屉里某账号的「查看会话」没有任何
可感知效果、rail 横滚把大多数账号藏在视口外（「系统里只有 katie」）。修复全部是
**纯前端行为**（scoped 取数 / 总览下拉 / 溢出箭头 / 点击反馈），静态门禁只能证
「函数挂了 window」，证不了「点账号真的切到该账号视角」，而模板热更新直上生产。
故用真浏览器把这些用户级不变量钉住。

**2026-08-16 起双模自适应**：账号顶部状态栏 Account Dock（#acct-dock，跨平台常驻、
点击即切）上线后为默认形态，旧 rail（#account-chips）在开关关闭部署仍在——工具按
页面实际形态自动选择选择器（dock 优先），全部不变量语义两模式同一套。

与 ``tools/verify_multiwin_ui.py`` 同族（同一实例 + token 登录 + Playwright），
只读：不点任何会话行、不发消息、不改配置，只操作筛选层 UI。

用法::

    python tools/verify_account_rail_ui.py                # 打默认实例
    python tools/verify_account_rail_ui.py --headed       # 肉眼看一遍
    python tools/verify_account_rail_ui.py --shots out/   # 存证截图

覆盖的不变量：
  1. 多账号平台选中后 rail 出现，「账号总览」按钮计数与 chips 一致。
  2. 总览下拉列出全部账号 +「全部账号」聚合行，且完全落在视口内（不被裁剪）。
  3. 下拉选择账号 → 触发 scoped 取数请求（?account_id=）→ 对应 chip 变 active。
  4. 重复点击已选中 chip → 出确认脉冲（ac-ack）；全部账号重复点击 → 出 toast。
  5. Alt+2 键盘切账号生效。
  6. 账号工作记忆：切走平台再回来，账号视角自动恢复。
  7. 会话行账号徽标点击 → 切账号视角且不打开会话（stopPropagation）。
  8. 视角深链：带 #plat=&acct= 打开工作台 → 自动恢复该账号视角；地址栏 hash 随切换同步。
  9. 深链扩 filter：#plat=&acct=&filter=unread 冷启动恢复筛选；切回全部后 hash 去掉 filter。
 10. 账号级排序记忆：账号视角改排序只随该账号（回「全部账号」还原平台级，再进来恢复）。
 11. ?conv= 窗口外会话救援：目标沉在全局 top-N 外时按 conv_id 反解 scoped 直查并打开。

token 从实例数据根读取，**绝不打印**。缺 playwright / 实例不可达 → SKIP exit 0
（挂 gate_sweep -Full 的前提：环境缺失不污染回归信号）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改回 localhost：::1 回退每连接 ~2s（见 verify_inbox_density.py 同行注释）
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"
CANDIDATE_PLATFORMS = ["telegram", "whatsapp", "line", "messenger", "web"]

# rail/dock 状态读取（DOM 口径——业务 JS 在 IIFE 内，evaluate 摸不到内部变量）。
# dock 在场优先：chips 额外带 data-aid/data-plat（dock 专有，rail 回空串）。
_RAIL_JS = """() => {
  const dock = document.getElementById('acct-dock');
  if (dock && dock.style.display !== 'none' && document.getElementById('adk-scroll')) {
    const chips = Array.from(dock.querySelectorAll('#adk-scroll .adk-item:not(.ac-toggle)'))
      .map(c => ({
        name: (c.querySelector('.adk-name') || {}).textContent || '',
        active: c.classList.contains('active'),
        all: !(c.dataset && c.dataset.plat),
        ack: c.classList.contains('ac-ack'),
        title: c.getAttribute('title') || '',
        aid: (c.dataset && c.dataset.aid) || '',
        plat: (c.dataset && c.dataset.plat) || '',
      }));
    const pick = document.getElementById('ac-pick-btn');
    return {visible: true, mode: 'dock', chips: chips,
            pickCount: pick ? parseInt((pick.textContent || '').trim(), 10) : null};
  }
  const bar = document.getElementById('account-chips');
  if (!bar || bar.style.display === 'none') return {visible: false};
  const chips = Array.from(bar.querySelectorAll('.ac-scroll .acct-chip:not(.ac-toggle)'))
    .map(c => ({
      name: (c.querySelector('.ac-name') || {}).textContent || '',
      active: c.classList.contains('active'),
      all: c.classList.contains('ac-all'),
      ack: c.classList.contains('ac-ack'),
      title: c.getAttribute('title') || '',
      aid: '',
      plat: '',
    }));
  const pick = document.getElementById('ac-pick-btn');
  return {
    visible: true,
    mode: 'rail',
    chips: chips,
    pickCount: pick ? parseInt((pick.textContent || '').trim(), 10) : null,
  };
}"""

# 当前活跃账号 (aid, plat) 提取：dock 用 dataset（title 已人性化/脱敏，不再可解析）；
# rail 沿用 title 首段约定。plat 仅 dock 可知，rail 回空由调用方回落。
_ACTIVE_AID_JS = """() => {
  const d = document.querySelector('#adk-scroll .adk-item.active[data-plat]');
  if (d) return {aid: (d.dataset && d.dataset.aid) || '', plat: (d.dataset && d.dataset.plat) || ''};
  const c = document.querySelector('.ac-scroll .acct-chip.active:not(.ac-all)');
  if (!c) return {aid: '', plat: ''};
  const t = (c.getAttribute('title') || '').trim();
  return {aid: (t.split('\\u00b7')[0] || '').trim(), plat: ''};
}"""

_POP_JS = """() => {
  const pop = document.getElementById('ac-pick-pop');
  if (!pop) return {open: false};
  const r = pop.getBoundingClientRect();
  return {
    open: true,
    rows: Array.from(pop.querySelectorAll('.ac-pick-row')).map(x => ({
      name: (x.querySelector('.apr-name') || {}).textContent || '',
      sel: x.classList.contains('sel'),
    })),
    onScreen: (r.left >= 0 && r.top >= 0
               && r.right <= window.innerWidth && r.bottom <= window.innerHeight),
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

    def check(self, name: str, cond: Any, detail: str = "") -> bool:
        ok = bool(cond)
        self.results.append((name, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        return ok

    def summary(self) -> int:
        fails = [n for n, ok in self.results if not ok]
        total = len(self.results)
        print(f"\n== 账号 rail 验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


def _plat_chip_count(st: dict, plat: str) -> int:
    """该平台的可见账号 chips 数（dock=按 data-plat 过滤；rail=全部非聚合 chips）。"""
    chips = st.get("chips") or []
    if st.get("mode") == "dock":
        return len([c for c in chips if c.get("plat") == plat])
    return len([c for c in chips if not c.get("all")])


def _pick_platform(page: Any) -> Tuple[str, dict]:
    """找一个多账号平台（≥2 账号；rail 出现 / dock 中该平台组 ≥2 项）；数据不足返回 ('', {})。"""
    for _ in range(10):   # 等首轮 loadChats 就位（platformStatus 异步到达）
        for plat in CANDIDATE_PLATFORMS:
            page.evaluate(f"window.setPlatFilter('{plat}')")
            page.wait_for_timeout(250)
            st = page.evaluate(_RAIL_JS)
            if st.get("visible") and _plat_chip_count(st, plat) >= 2:
                return plat, st
        page.wait_for_timeout(1000)
    return "", {}


def run(base: str, token: str, *, shots: Optional[Path] = None,
        headed: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(viewport={"width": 1280, "height": 860})
        ctx.request.post(base + "/login", form={"auth_token": token})

        page = ctx.new_page()
        page.goto(base + "/workspace", wait_until="domcontentloaded")
        try:
            page.wait_for_function("() => typeof window.setPlatFilter === 'function'",
                                   timeout=15000)
        except Exception:
            print("  [ABORT] 工作台 JS 未就绪（登录失败或模板异常）")
            browser.close()
            return 1
        page.wait_for_timeout(2000)

        print("== 1. 选中多账号平台 → 切换条出现，总览计数与 chips 一致 ==")
        plat, st = _pick_platform(page)
        if not plat:
            print("[SKIP] 无 ≥2 账号的平台（数据不足，切换条不变量无从验证）")
            browser.close()
            return 0
        mode = st.get("mode") or "rail"
        # chips 含「全部账号」聚合项；总览计数=账号数（含折叠的仅历史号；dock=跨平台总数）
        n_acct_chips = len([c for c in st["chips"] if not c["all"]])
        ck.check(f"切换条可见（{plat} / mode={mode}）", st.get("visible"))
        ck.check("「全部账号」chip 存在且默认选中",
                 any(c["all"] and c["active"] for c in st["chips"]))
        ck.check("总览按钮计数 ≥ 可见账号 chips 数",
                 (st.get("pickCount") or 0) >= n_acct_chips,
                 f"pick={st.get('pickCount')} chips={n_acct_chips}")

        print("== 2. 总览下拉：全量清单 + 视口内完整渲染 ==")
        page.click("#ac-pick-btn")
        page.wait_for_timeout(300)
        pop = page.evaluate(_POP_JS)
        ck.check("下拉已打开", pop.get("open"))
        ck.check("行数 = 总览计数 + 1（全部账号聚合行）",
                 len(pop.get("rows") or []) == (st.get("pickCount") or 0) + 1,
                 f"rows={len(pop.get('rows') or [])}")
        ck.check("下拉完整落在视口内（不被裁剪）", pop.get("onScreen"))
        if shots:
            page.screenshot(path=str(shots / "rail_picker_open.png"))

        print("== 3. 下拉选账号 → 触发 scoped 取数 → chip 变 active ==")
        target_name = (pop["rows"][1]["name"] or "").strip()
        try:
            with page.expect_request(lambda r: "account_id=" in r.url, timeout=8000):
                # 按匹配序取第 2 行（dock 分组模式下拉里穿插 .ac-pick-sec 分组头，
                # nth-child 会点到分组头；>> nth= 只数命中行，两模式通吃）
                page.click("#ac-pick-pop .ac-pick-row >> nth=1")
            scoped_fired = True
        except Exception:
            scoped_fired = False
        ck.check("选择账号触发 scoped 请求（?account_id=）", scoped_fired)
        page.wait_for_timeout(1200)
        st2 = page.evaluate(_RAIL_JS)
        active = next((c for c in (st2.get("chips") or []) if c["active"]), {})
        ck.check("对应 chip 变 active（且非全部账号）",
                 active and not active.get("all")
                 and target_name.startswith((active.get("name") or "").strip()[:4]),
                 f"target={target_name!r} active={active.get('name')!r}")
        ck.check("下拉已随选择关闭", not page.evaluate(_POP_JS).get("open"))
        if shots:
            page.screenshot(path=str(shots / "rail_account_view.png"))

        print("== 4. 点击反馈：重复点击已选中 chip 出脉冲；全部账号重复点出 toast ==")
        mode2 = st2.get("mode") or "rail"
        item_sel = ("#adk-scroll .adk-item:not(.ac-toggle)" if mode2 == "dock"
                    else ".ac-scroll .acct-chip:not(.ac-toggle)")
        all_sel = ("#adk-scroll .adk-item[data-aid='all']" if mode2 == "dock"
                   else ".ac-scroll .acct-chip.ac-all")
        idx_active = next(i for i, c in enumerate(st2["chips"]) if c["active"])
        page.click(f"{item_sel} >> nth={idx_active}")
        page.wait_for_timeout(200)
        st3 = page.evaluate(_RAIL_JS)
        ck.check("重复点击已选中账号 → 确认脉冲（ac-ack）",
                 any(c.get("ack") for c in (st3.get("chips") or [])))
        page.click(all_sel)     # 切回全部（dock=全平台聚合视图）
        page.wait_for_timeout(400)
        page.click(all_sel)     # 重复点击 → toast
        page.wait_for_timeout(400)
        toast_ok = page.evaluate(
            "() => Array.from(document.querySelectorAll('body > div, .tk-toast'))"
            ".some(d => /全部账号|all accounts/i.test(d.textContent || '')"
            " && d.offsetParent !== null)")
        ck.check("全部账号重复点击 → 确认 toast", toast_ok)

        print("== 5. Alt+2 键盘切账号 ==")
        page.keyboard.press("Alt+2")
        page.wait_for_timeout(500)
        st4 = page.evaluate(_RAIL_JS)
        kb_active = next((c for c in (st4.get("chips") or []) if c["active"]), {})
        ck.check("Alt+2 → 第 2 个 chip（首个具体账号）被选中",
                 kb_active and not kb_active.get("all"),
                 f"active={kb_active.get('name')!r}")

        print("== 6. 账号工作记忆：切走平台再回来自动恢复 ==")
        # dock 跨平台：Alt+2 命中的账号可能属别的平台——工作记忆按**该账号的平台**验证；
        # 后续场景（badge/深链/排序/纯净）全部跟随此平台上下文（plat 就地重绑）。
        mem_plat = kb_active.get("plat") or plat
        plat = mem_plat
        remembered = (kb_active.get("aid") or kb_active.get("title")
                      or kb_active.get("name"))
        page.evaluate("window.setPlatFilter('all')")
        page.wait_for_timeout(400)
        page.evaluate(f"window.setPlatFilter('{mem_plat}')")
        page.wait_for_timeout(800)
        st5 = page.evaluate(_RAIL_JS)
        mem_active = next((c for c in (st5.get("chips") or []) if c["active"]), {})
        ck.check("回到平台后账号视角自动恢复",
                 mem_active and not mem_active.get("all")
                 and (mem_active.get("aid") or mem_active.get("title")
                      or mem_active.get("name")) == remembered,
                 f"restored={mem_active.get('name')!r}")

        print("== 7. 会话行账号徽标点击 → 账号视角（且不打开会话） ==")
        page.evaluate("window.setAccountFilter('all')")
        page.wait_for_timeout(600)
        badge_info = page.evaluate("""() => {
          const b = document.querySelector('#conv-items .conv-item .conv-acct-badge');
          if (!b) return {ok: false};
          return {ok: true, text: (b.textContent || '').trim(),
                  cursor: getComputedStyle(b).cursor};
        }""")
        if not badge_info.get("ok"):
            ck.check("列表有可点账号徽标（多账号平台）", False, "无 .conv-acct-badge")
        else:
            ck.check("徽标可交互样式（cursor=pointer）",
                     badge_info.get("cursor") == "pointer",
                     f"cursor={badge_info.get('cursor')!r}")
            try:
                with page.expect_request(lambda r: "account_id=" in r.url, timeout=8000):
                    page.click("#conv-items .conv-item .conv-acct-badge")
                badge_scoped = True
            except Exception:
                badge_scoped = False
            page.wait_for_timeout(900)
            st6 = page.evaluate(_RAIL_JS)
            badge_active = next((c for c in (st6.get("chips") or []) if c["active"]), {})
            # 未打开会话：主区仍是空态（未选中对话）
            still_empty = page.evaluate(
                "() => /选择一个对话|Select a conversation/i.test(document.body.innerText||'')")
            ck.check("点徽标触发 scoped 请求", badge_scoped)
            ck.check("点徽标后 rail 进入具体账号视角",
                     badge_active and not badge_active.get("all"),
                     f"active={badge_active.get('name')!r} badge={badge_info.get('text')!r}")
            ck.check("点徽标不打开会话（stopPropagation）", still_empty)
            if shots:
                page.screenshot(path=str(shots / "rail_badge_click.png"))

        print("== 8. 视角深链 #plat=&acct= 冷启动恢复 + 切换同步 hash ==")
        # 活跃账号 (aid, plat)：dock 用 dataset；rail 用 title 首段约定。
        # plat 再次重绑到活跃账号的平台（badge 点击可能已切到别的号）。
        _pair = page.evaluate(_ACTIVE_AID_JS) or {}
        aid = _pair.get("aid") or ""
        if _pair.get("plat"):
            plat = _pair["plat"]
        if not aid:
            ck.check("深链验收有可用 account_id", False)
        else:
            page.goto(f"{base}/workspace#plat={plat}&acct={aid}",
                      wait_until="domcontentloaded")
            try:
                page.wait_for_function(
                    "() => typeof window.setPlatFilter === 'function'", timeout=15000)
            except Exception:
                ck.check("深链冷启动工作台就绪", False)
            else:
                page.wait_for_timeout(2500)
                st7 = page.evaluate(_RAIL_JS)
                dl_active = next((c for c in (st7.get("chips") or []) if c["active"]), {})
                hash_now = page.evaluate("() => location.hash || ''")
                ck.check("深链冷启动恢复具体账号视角",
                         dl_active and not dl_active.get("all"),
                         f"active={dl_active.get('name')!r} aid={aid!r}")
                ck.check("冷启动后地址栏保留 #plat=&acct=",
                         f"plat={plat}" in hash_now and f"acct={aid}" in hash_now,
                         f"hash={hash_now!r}")
                # 切回全部账号 → hash 应去掉 acct（可保留 plat）
                page.evaluate("window.setAccountFilter('all')")
                page.wait_for_timeout(400)
                hash_all = page.evaluate("() => location.hash || ''")
                ck.check("切回全部账号后 hash 去掉 acct",
                         "acct=" not in hash_all,
                         f"hash={hash_all!r}")
                if shots:
                    page.screenshot(path=str(shots / "rail_deeplink.png"))

                print("== 9. 深链扩 filter：#plat=&acct=&filter=unread ==")
                page.goto(
                    f"{base}/workspace#plat={plat}&acct={aid}&filter=unread",
                    wait_until="domcontentloaded")
                try:
                    page.wait_for_function(
                        "() => typeof window.setFilter === 'function'", timeout=15000)
                except Exception:
                    ck.check("filter 深链冷启动工作台就绪", False)
                else:
                    page.wait_for_timeout(2500)
                    ftab = page.evaluate(
                        "() => (document.querySelector('.ftab.active')||{}).dataset?.f || ''")
                    hash_f = page.evaluate("() => location.hash || ''")
                    ck.check("冷启动恢复未读筛选 tab",
                             ftab == "unread", f"ftab={ftab!r}")
                    ck.check("地址栏含 filter=unread",
                             "filter=unread" in hash_f, f"hash={hash_f!r}")
                    ck.check("filter 深链仍保留账号视角",
                             f"acct={aid}" in hash_f, f"hash={hash_f!r}")
                    page.evaluate(
                        "window.setFilter('all', document.querySelector('.ftab[data-f=all]'))")
                    page.wait_for_timeout(400)
                    hash_no_f = page.evaluate("() => location.hash || ''")
                    ck.check("切回全部筛选后 hash 去掉 filter",
                             "filter=" not in hash_no_f, f"hash={hash_no_f!r}")
                    # 遗留经营看板 query：?filter=sla 应迁进 hash
                    page.goto(f"{base}/workspace?filter=sla",
                              wait_until="domcontentloaded")
                    page.wait_for_timeout(2500)
                    mig = page.evaluate("""() => ({
                      ftab: (document.querySelector('.ftab.active')||{}).dataset?.f || '',
                      hash: location.hash || '',
                      search: location.search || ''
                    })""")
                    ck.check("?filter=sla 迁进 hash 并生效",
                             mig.get("ftab") == "sla"
                             and "filter=sla" in (mig.get("hash") or "")
                             and "filter=" not in (mig.get("search") or ""),
                             f"{mig!r}")
                    if shots:
                        page.screenshot(path=str(shots / "rail_filter_deeplink.png"))

                print("== 10. 账号级排序记忆（与平台级解耦） ==")
                page.goto(base + "/workspace", wait_until="domcontentloaded")
                page.wait_for_function(
                    "() => typeof window.setSortMode === 'function'", timeout=15000)
                page.wait_for_timeout(2000)
                page.evaluate(f"window.setPlatFilter('{plat}')")
                page.wait_for_timeout(300)
                page.evaluate(f"window.setAccountFilter('{aid}')")
                page.wait_for_timeout(300)
                page.evaluate("window.setSortMode('unread')")
                page.wait_for_timeout(200)
                sort_in_acct = page.evaluate(
                    "() => (document.getElementById('sort-select')||{}).value || ''")
                page.evaluate("window.setAccountFilter('all')")
                page.wait_for_timeout(300)
                sort_back_all = page.evaluate(
                    "() => (document.getElementById('sort-select')||{}).value || ''")
                page.evaluate(f"window.setAccountFilter('{aid}')")
                page.wait_for_timeout(300)
                sort_reenter = page.evaluate(
                    "() => (document.getElementById('sort-select')||{}).value || ''")
                ck.check("账号视角设排序生效", sort_in_acct == "unread",
                         f"got={sort_in_acct!r}")
                ck.check("回「全部账号」还原平台级排序", sort_back_all == "recent",
                         f"got={sort_back_all!r}")
                ck.check("再进该账号恢复账号级排序", sort_reenter == "unread",
                         f"got={sort_reenter!r}")

                print("== 11. ?conv= 窗口外会话救援（scoped 反解直查） ==")
                # 候选=平台任一账号的「沉在全局 top-100 外」会话（逐账号找，直到命中）
                cand = page.evaluate(f"""async () => {{
                  const g = await (await fetch('/api/unified-inbox/chats?limit=100')).json();
                  const inWin = new Set((g.chats||[]).map(c => String(c.conversation_id)));
                  const accts = [...new Set(Object.values(g.platform_status||{{}})
                    .filter(v => v && (v.platform||'web') === '{plat}')
                    .map(v => String(v.account_id||'default')))];
                  for (const a of accts) {{
                    const s = await (await fetch('/api/unified-inbox/chats?limit=200'
                      + '&platform={plat}&account_id=' + encodeURIComponent(a))).json();
                    const hit = (s.chats||[]).find(c =>
                      c.conversation_id && !inWin.has(String(c.conversation_id))
                      && (String(c.conversation_id).split(':').length >= 3));
                    if (hit) return {{cid: String(hit.conversation_id),
                                      name: String(hit.name || '')}};
                  }}
                  return null;
                }}""")
                if not cand:
                    print("  [SKIP] 该账号无窗口外会话（全部落在全局窗口内），救援路径无从验证")
                else:
                    from urllib.parse import quote
                    page.goto(f"{base}/workspace?conv={quote(cand['cid'])}",
                              wait_until="domcontentloaded")
                    page.wait_for_function(
                        "() => typeof window.setPlatFilter === 'function'", timeout=15000)
                    # 事件驱动等待（勿改回固定 3500ms）：救援 fetch 可能撞冷启动请求
                    # 风暴被浏览器排队（2026-08-05 实测首发挂 6s+ 后靠重试成功），
                    # 门禁等「头部出名字」这一事实、上限 15s，不赌固定窗口。
                    try:
                        page.wait_for_function(
                            "() => ((document.getElementById('hdr-name')||{})"
                            ".textContent||'').trim().length > 0", timeout=15000)
                    except Exception:
                        pass   # 超时也继续取值，让断言如实红
                    hdr = page.evaluate(
                        "() => ((document.getElementById('hdr-name')||{})"
                        ".textContent||'').trim()")
                    ck.check("窗口外会话经救援打开（头部出会话名）",
                             bool(hdr) and (not cand.get("name")
                                            or hdr[:6] in cand["name"]
                                            or cand["name"][:6] in hdr),
                             f"hdr={hdr!r} want~={cand.get('name')!r}")
                    if shots:
                        page.screenshot(path=str(shots / "conv_rescue.png"))

                print("== 12. 账号视角纯净 + 头像身份键（2026-08-14 切账号串号防线） ==")
                page.goto(base + "/workspace", wait_until="domcontentloaded")
                page.wait_for_function(
                    "() => typeof window.setAccountFilter === 'function'", timeout=15000)
                page.wait_for_timeout(2000)
                page.evaluate(f"window.setPlatFilter('{plat}')")
                page.wait_for_timeout(300)
                page.evaluate(f"window.setAccountFilter('{aid}')")
                # 刻意只等一小拍就断言：渲染口 scope guard 必须在慢请求回来前就保证纯净
                page.wait_for_timeout(500)
                purity_js = f"""() => {{
                  const rows = Array.from(document.querySelectorAll(
                    '#conv-items .conv-item[data-cid]'));
                  let total = 0, leak = 0, avaTotal = 0, avaNoKey = 0, avaWrongAcct = 0;
                  for (const r of rows) {{
                    const cid = String(r.dataset.cid || '');
                    const seg = cid.split(':');
                    if (seg.length >= 3) {{
                      total++;
                      if (seg[0] !== '{plat}' || seg[1] !== '{aid}') leak++;
                    }}
                    const img = r.querySelector('.conv-avatar img.ava-img');
                    if (img) {{
                      avaTotal++;
                      const k = img.getAttribute('data-avkey') || '';
                      if (!k) avaNoKey++;
                      // 代理键携带账号归属：p:/api/platforms/<plat>/<acct>/avatar?...
                      const m = k.match(/^p:\\/api\\/platforms\\/([^/]+)\\/([^/]+)\\/avatar/);
                      if (m && decodeURIComponent(m[2]) !== '{aid}') avaWrongAcct++;
                    }}
                  }}
                  return {{total, leak, avaTotal, avaNoKey, avaWrongAcct}};
                }}"""
                pur = page.evaluate(purity_js)
                ck.check("账号视角列表零串号（切换 500ms 内即纯净）",
                         pur.get("total", 0) >= 0 and pur.get("leak", 0) == 0,
                         f"{pur!r}")
                # 快切压力：all → 账号 连续两跳后再验一次（旧实现的竞态窗口）
                page.evaluate("window.setAccountFilter('all')")
                page.wait_for_timeout(150)
                page.evaluate(f"window.setAccountFilter('{aid}')")
                page.wait_for_timeout(500)
                pur2 = page.evaluate(purity_js)
                ck.check("快切（all→账号 连跳）后仍零串号",
                         pur2.get("leak", 0) == 0, f"{pur2!r}")
                ck.check("头像 img 全部带身份键 data-avkey（增量刷新依据）",
                         pur2.get("avaNoKey", 0) == 0, f"{pur2!r}")
                ck.check("代理头像归属账号与视角一致（零串脸）",
                         pur2.get("avaWrongAcct", 0) == 0, f"{pur2!r}")
                if shots:
                    page.screenshot(path=str(shots / "acct_scope_purity.png"))

        print("== 13. 角标数字可点 → 明细浮层，条数 == 角标数（#170 幽灵未读，同 WHERE 恒等） ==")
        # 回到全部账号视角：账号 chip 上的灰色未读角标（.ac-unread 非 attn 态）才是
        # 「未读会话数」语义；attn 红标数字＝可行动数，与明细行数不是同一口径，跳过。
        page.evaluate("window.setAccountFilter('all')")
        page.wait_for_timeout(800)
        badge = page.evaluate("""() => {
          const els = Array.from(document.querySelectorAll(
            '.ac-unread[data-ac-unread]:not(.attn)'));
          const b = els.find(e => e.offsetParent !== null && e.getAttribute('data-aid'));
          if (!b) return null;
          const txt = (b.textContent || '').trim();
          return {n: txt === '99+' ? -1 : parseInt(txt, 10),
                  plat: b.getAttribute('data-plat') || '',
                  aid: b.getAttribute('data-aid') || ''};
        }""")
        if not badge or badge.get("n") in (None, -1) or badge["n"] <= 0:
            print("  [SKIP] 当前无可见的账号未读角标（无未读=无从验证；不构成失败）")
        else:
            page.click(f".ac-unread[data-ac-unread][data-plat='{badge['plat']}']"
                       f"[data-aid='{badge['aid']}'] >> nth=0")
            try:
                page.wait_for_function(
                    "() => { const p = document.getElementById('ac-unread-pop');"
                    " if (!p || p.style.display === 'none') return false;"
                    " return !!p.querySelector('.acup-row') || !!p.querySelector('[data-acup-mark]'); }",
                    timeout=8000)
            except Exception:
                pass
            pop_st = page.evaluate("""() => {
              const p = document.getElementById('ac-unread-pop');
              if (!p) return {open: false};
              return {open: p.style.display !== 'none',
                      rows: p.querySelectorAll('.acup-row').length,
                      mark: !!p.querySelector('[data-acup-mark]')};
            }""")
            ck.check("点击角标数字 → 明细浮层打开（不是切账号）", pop_st.get("open"))
            # 浮层行 = 服务端 account-unread 明细；徽标 = 同 WHERE 的 SUM 聚合口径，两者恒等
            srv = page.evaluate(
                f"""async () => {{
                  const r = await fetch('/api/unified-inbox/account-unread?platform='
                    + encodeURIComponent('{badge['plat']}') + '&account_id='
                    + encodeURIComponent('{badge['aid']}'));
                  const d = await r.json(); return {{count: d.count, total: d.unread_total}};
                }}""")
            ck.check("浮层条数 == 服务端明细条数",
                     pop_st.get("rows") == srv.get("count"),
                     f"rows={pop_st.get('rows')} srv={srv!r}")
            ck.check("角标数 == 服务端同 WHERE 聚合（unread_total 或 count）",
                     badge["n"] in (srv.get("total"), srv.get("count")),
                     f"badge={badge['n']} srv={srv!r}")
            ck.check("浮层带「全部标已读」按钮（mark-account-read 入口）", pop_st.get("mark"))
            page.keyboard.press("Escape")
            page.evaluate("document.body.click()")
            if shots:
                page.screenshot(path=str(shots / "acct_unread_pop.png"))

        browser.close()
    return ck.summary()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="账号切换 rail 真浏览器验证（只读）")
    ap.add_argument("--base", default=DEFAULT_BASE,
                    help=f"实例地址（默认 {DEFAULT_BASE}；127.0.0.1 部分环境不可达）")
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT,
                    help="实例数据根（读 web_admin.auth_token）")
    ap.add_argument("--token", default="", help="直接给 token（优先于 --data-root）")
    ap.add_argument("--shots", default="", help="截图输出目录（留空不截图）")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    args = ap.parse_args(argv)

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
    shots = None
    if args.shots:
        shots = Path(args.shots)
        shots.mkdir(parents=True, exist_ok=True)
    print(f"== 0. 目标 {args.base} ==")
    return run(args.base, token, shots=shots, headed=args.headed)


if __name__ == "__main__":
    sys.exit(main())
