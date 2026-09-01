# -*- coding: utf-8 -*-
"""对话翻译弹层 P0+P1 真浏览器门禁（Playwright；2026-08-16 坐席实录三报障沉淀）。

**为什么需要它**：2026-08-16 坐席实录三连报障——「一键开启点了没响应」（旧实现只把
收到→硬设 zh，已是 zh 时零 DOM 变化零反馈）、「默认只译中文」（硬编码 zh 散落 9 处，
外籍坐席被当成中文坐席）、「管理弹窗没作用」（只读清单 + 保存强依赖选中会话）。
P1 增量：信息架构重排（方向卡片 + 发送方式分段 + 高级折叠）、智能建议条（会话语言
≠坐席语言且未开翻译 → 一键建议）、坐席级「我的语言」服务端偏好（特性探测 fail-soft）。
修复全部是模板/i18n 热更新**直接上生产**，真值只能在真浏览器验。

覆盖三层：
- 0) 源码接线（零依赖）：开关/提示/分段/折叠/管理编辑行的模板+CSS+i18n+后端路由全链
  在位，硬编码 ``'zh'`` 兜底与旧预览行不回潮（ratchet 断言）。
- 1) 坐席语言解析 ``_agentLang``：en-US context → en；vi-VN locale context → vi。
- 2) 行为（route mock chats language='vi'，零生产写入）：智能建议出现→一键接受；
  quick 开关两态往返 + 状态胶囊 flash；同语提示随目标语切换；发送方式分段随
  checkbox 状态双向同步；高级折叠展开后引擎选择保持隐藏（2026-08-16 决策）；「我的语言」行显隐与特性探测
  一致（旧后端隐藏，重启装载后自动出现——断言按 ``window._xlAgentLangState()`` 动态判）；
  管理弹窗两节编辑行 + 生效链胶囊 + 范围显隐（只渲染不点保存，零写接口）。

与 ``tools/verify_composer_langwarn.py`` 同族（同实例 + token 登录 + Playwright +
缺环境 SKIP exit 0）。

用法::

    python tools/verify_xlate_ui.py                # 门禁
    python tools/verify_xlate_ui.py --headed       # 肉眼
    python tools/verify_xlate_ui.py --source-only  # 只跑静态接线
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改 localhost：::1 回退每连接 ~2s
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"
VIEWPORT = {"width": 1440, "height": 900}
ENGINE_ROOT = Path(__file__).resolve().parents[1]
INBOX_TEMPLATE = ENGINE_ROOT / "src" / "web" / "templates" / "unified_inbox.html"
INBOX_CSS = ENGINE_ROOT / "src" / "web" / "static" / "workspace" / "unified-inbox.css"
I18N_PACK = ENGINE_ROOT / "src" / "web" / "i18n_packs" / "inbox_workspace.py"
TRANSLATE_ROUTES = ENGINE_ROOT / "src" / "web" / "routes" / "unified_inbox_translate_routes.py"

# 新增 i18n 键（zh+en 双语都必须在 pack 里，出现次数 >=2）
_NEW_KEYS = [
    # P0
    "inbox.xl.quick_on_done", "inbox.xl.quick_off_t",
    "inbox.xl.toast_on", "inbox.xl.toast_off", "inbox.xl.same_hint",
    "inbox.dlm.add_lbl", "inbox.dlm.eff", "inbox.dlm.eff_none",
    "inbox.dlm.eff_none_reply", "inbox.dlm.no_targets", "inbox.dlm.none_clear",
    # P1
    "inbox.xl.suggest", "inbox.xl.suggest_btn", "inbox.xl.sendmode",
    "inbox.xl.mode_direct", "inbox.xl.mode_preview", "inbox.xl.adv",
    "inbox.xl.mylang", "inbox.xl.mylang_follow", "inbox.xl.mylang_saved",
    "inbox.xl.pick_search", "inbox.xl.pick_back",
    "inbox.xl.pick_title_lb", "inbox.xl.pick_cust",
]


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

    def summary(self, title: str = "对话翻译弹层 P0+P1 验证") -> int:
        fails = [n for n, ok in self.results if not ok]
        total = len(self.results)
        tail = f"  FAILED: {fails}" if fails else " =="
        extra = f"  (skipped {len(self.skipped)})" if self.skipped else ""
        print(f"\n== {title}: {total - len(fails)}/{total} PASS{extra}{tail}")
        return 1 if fails else 0


def read_token(data_root: str) -> str:
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


def check_source_wiring(ck: Checker) -> None:
    """不依赖实例：防开关/提示/分段/折叠/编辑行被顺手删掉（热更新直上生产）。"""
    print("== 0. 源码接线（静态）==")
    if not INBOX_TEMPLATE.exists():
        ck.check("unified_inbox.html 存在", False, str(INBOX_TEMPLATE))
        return
    src = INBOX_TEMPLATE.read_text(encoding="utf-8")
    # P0
    ck.check("坐席语言解析已定义并挂 window",
             "function _agentLang" in src and "window._agentLang" in src)
    ck.check("两态开关按钮在模板", 'id="xl-quick-btn"' in src and '_xlateQuickOn()' in src)
    ck.check("两态/提示/分段同步接进状态汇聚点",
             "_syncQuickBtn()" in src and "_syncXlConvHint()" in src and "_syncSendModeSeg()" in src)
    ck.check("提示容器（同语+建议双模式）",
             'id="xl-conv-hint"' in src and "function _syncXlConvHint" in src
             and "_xlateSuggestAccept" in src)
    ck.check("开关反馈（flash + toast）",
             "function _flashXlStatus" in src
             and "inbox.xl.toast_on" in src and "inbox.xl.toast_off" in src)
    ck.check("无 `_xlateIn||'zh'` 残留", "_xlateIn||'zh'" not in src)
    ck.check("无 `si.value='zh'` 快捷硬编码残留", "si.value='zh'" not in src)
    # P1 结构
    ck.check("方向卡片", 'class="xl-dir-card"' in src)
    ck.check("发送方式分段（隐藏 checkbox 保 id 契约）",
             'id="xl-seg-direct"' in src and 'id="xl-seg-preview"' in src
             and 'id="xlate-out-preview" style="display:none;"' in src)
    ck.check("旧预览行已移除", "xl-preview-row" not in src)
    ck.check("高级折叠", 'id="xl-adv"' in src and "details" in src)
    ck.check("我的语言行（特性探测 fail-soft）",
             'id="xl-agent-lang-row"' in src and 'id="xl-agent-lang"' in src
             and "function _loadAgentLangPref" in src and "_xlAgentLangState" in src)
    ck.check("新内联 handler 已登记 window 导出清单",
             "_setOutPreviewMode, _onAgentLangChange, _xlateSuggestAccept," in src)
    ck.check("HY-MT 目录 + 选语第二屏接线",
             "const _XL_CATALOG=" in src and "function _canonXlLang" in src
             and 'id="xl-pop-picker"' in src and 'id="xl-chip-in"' in src
             and "function _xlPickOpen" in src)
    ck.check("灯箱接同一选语页（无原生 select）",
             "function _lbPickChrome" in src and "_xlPickOpen('lb')" in src
             and "_LBXL_MORE_LANGS" not in src and "function _xlCustLang" in src)
    ck.check("埋点 xlt_* 在位",
             all(k in src for k in ("xlt_quick_on", "xlt_quick_off", "xlt_suggest_shown",
                                    "xlt_suggest_accept", "xlt_preview_on", "xlt_mgr_open",
                                    "xlt_agent_lang_set")))
    # 管理弹窗（P0-5）
    ck.check("管理编辑行构建器（_dlmEditor）", "function _dlmEditor" in src)
    ck.check("统一保存入口（_saveDefaultLangFor）", "function _saveDefaultLangFor" in src)
    ck.check("生效链胶囊（_dlmEffLine）", "function _dlmEffLine" in src)
    ck.check("旧 _saveReplyLangDefault 已收编", "_saveReplyLangDefault(" not in src)
    if INBOX_CSS.exists():
        css = INBOX_CSS.read_text(encoding="utf-8")
        for cls in (".xl-quick.on", ".rt-xl-status.flash", ".xl-conv-hint", ".xl-hint-btn",
                    ".xl-dir-card", ".xl-seg-btn", "details.xl-adv",
                    ".dlm-editor", ".dlm-eff", ".xl-lang-chip", "#xl-pop-picker",
                    "#lb-xl-picker", ".xl-pick-row.cust"):
            ck.check(f"CSS 规则存在（{cls}）", cls in css)
        # 戳只增不减：字面量匹配会被后续批次正常 bump 误红（2026-08-17 实锤：
        # Account Dock 批 bump 到 20260817a 后本检查假红）——改「>= 20260816b」序比较。
        _css_v = re.search(r"unified-inbox\.css\?v=(\d{8}[a-z]?)", src)
        ck.check("CSS 缓存戳已 bump（?v=20260816b+）",
                 bool(_css_v) and _css_v.group(1) >= "20260816b",
                 f"v={_css_v.group(1) if _css_v else '?'}")
    if I18N_PACK.exists():
        pack = I18N_PACK.read_text(encoding="utf-8")
        for key in _NEW_KEYS:
            ck.check(f"i18n 键 zh+en 齐备：{key}", pack.count(f'"{key}"') >= 2)
    if TRANSLATE_ROUTES.exists():
        py = TRANSLATE_ROUTES.read_text(encoding="utf-8")
        ck.check("后端 agent-lang 路由在源码（待重启窗装载）",
                 py.count("/api/unified-inbox/agent-lang") >= 2
                 and "_AGENT_LANG_ALLOWED" in py)


def _swallow_beacons(ctx: Any) -> None:
    """门禁交互不得污染 xlt_* 裁决计数（cp-next-actions no-beacon 守卫同款）：
    context 级拦掉 ui-event 上报，真实点击照跑、埋点不落库。"""
    ctx.route("**/api/telemetry/ui-event",
              lambda r: r.fulfill(status=204, body=""))


def _login_workspace(p: Any, base: str, token: str, *, headed: bool,
                     locale: str = "en-US",
                     viewport: Optional[dict] = None) -> Tuple[Any, Any, Any]:
    browser = p.chromium.launch(headless=not headed)
    ctx = browser.new_context(viewport=viewport or VIEWPORT, locale=locale)
    _swallow_beacons(ctx)
    # （cp-tour 已于 2026-08-31 随新手引导退役，无需再预置跳过键。）
    ctx.request.post(base + "/login", form={"auth_token": token})
    page = ctx.new_page()
    page.goto(base + "/workspace", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => typeof window.setPlatFilter === 'function'", timeout=20000)
    page.wait_for_timeout(3500)
    return browser, ctx, page


_STATE_JS = """() => {
  const g = id => document.getElementById(id);
  const btn = g('xl-quick-btn'), chip = g('rt-xl-status'), hint = g('xl-conv-hint');
  const segD = g('xl-seg-direct'), segP = g('xl-seg-preview'), pv = g('xlate-out-preview');
  return {
    in_v: (g('xlate-in')||{}).value || '',
    out_v: (g('xlate-out')||{}).value || '',
    btn_on: !!(btn && btn.classList.contains('on')),
    btn_txt: btn ? (btn.textContent||'').trim() : '',
    chip_on: !!(chip && chip.classList.contains('on')),
    chip_flash: !!(chip && chip.classList.contains('flash')),
    chip_txt: chip ? (chip.textContent||'').trim() : '',
    hint_visible: !!(hint && getComputedStyle(hint).display !== 'none'),
    hint_btn: !!(hint && hint.querySelector('.xl-hint-btn')),
    hint_txt: hint ? (hint.textContent||'').trim() : '',
    seg_direct_on: !!(segD && segD.classList.contains('on')),
    seg_preview_on: !!(segP && segP.classList.contains('on')),
    pv_checked: !!(pv && pv.checked),
  };
}"""


def run(base: str, token: str, *, headed: bool = False) -> int:
    import json as _json
    from playwright.sync_api import sync_playwright

    ck = Checker()
    check_source_wiring(ck)

    with sync_playwright() as p:
        try:
            browser, ctx, page = _login_workspace(p, base, token, headed=headed)
        except Exception as e:  # noqa: BLE001
            print(f"[SKIP] 工作台脚本未就绪（{str(e)[:80]}）")
            return ck.summary()

        # ── 1. 坐席语言解析真值 ─────────────────────────────────────────
        print("== 1. _agentLang 坐席语言解析 ==")
        if not ck.check("window._agentLang 可达",
                        page.evaluate("() => typeof window._agentLang === 'function'")):
            browser.close()
            return ck.summary()
        lang_en = page.evaluate("() => window._agentLang()")
        ck.check("en-US context → en（浏览器语言信号）", lang_en == "en", f"got={lang_en!r}")
        canon = page.evaluate("() => window._canonXlLang('zh_hant')")
        ck.check("_canonXlLang('zh_hant') === 'zh-tw'", canon == "zh-tw", f"got={canon!r}")
        n_tgt = page.evaluate("() => (window._XL_TARGETS||[]).length")
        ck.check("_XL_TARGETS.length === 34", n_tgt == 34, f"got={n_tgt!r}")
        sel_ok = page.evaluate(
            """() => {
              const vals = id => [...(document.getElementById(id)||{options:[]}).options]
                .map(o => o.value);
              const inn = vals('xlate-in'), out = vals('xlate-out');
              return inn.includes('zh-tw') && inn.includes('fr')
                && out.includes('zh-tw') && out.includes('fr') && out.includes('auto');
            }""")
        ck.check("入站/出站 select 含 zh-tw + fr（出站另含 auto）", sel_ok)
        try:
            ctx_vi = browser.new_context(viewport=VIEWPORT, locale="vi-VN")
            _swallow_beacons(ctx_vi)
            ctx_vi.request.post(base + "/login", form={"auth_token": token})
            pg_vi = ctx_vi.new_page()
            pg_vi.goto(base + "/workspace", wait_until="domcontentloaded")
            pg_vi.wait_for_function(
                "() => typeof window._agentLang === 'function'", timeout=20000)
            lang_vi = pg_vi.evaluate("() => window._agentLang()")
            ck.check("vi-VN context → vi（外籍坐席开箱母语）", lang_vi == "vi",
                     f"got={lang_vi!r}")
            ctx_vi.close()
        except Exception as e:  # noqa: BLE001
            ck.skip("vi-VN 坐席语言", str(e)[:80])

        # ── 2. 行为：route mock 会话语言=vi，真实点击（零生产写入）────────
        print("== 2. 建议条 / 一键开关 / 同语提示 / 分段 / 折叠 / 管理弹窗 ==")

        def _patch_chats(route: Any) -> None:
            resp = route.fetch()
            try:
                data = resp.json()
            except Exception:
                route.fulfill(response=resp)
                return
            for c in (data.get("chats") or []):
                c["language"] = "vi"      # 客户=越南语（≠ en-US 坐席 → 建议条场景）
            route.fulfill(status=resp.status, content_type="application/json",
                          body=_json.dumps(data, ensure_ascii=False))

        page.route("**/api/unified-inbox/chats*", _patch_chats)
        page.reload(wait_until="domcontentloaded")
        try:
            page.wait_for_function(
                "() => typeof window.setPlatFilter === 'function'", timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(3000)
        opened = page.evaluate(
            """() => {
              const row = document.querySelector('#conv-items .conv-item:not(.has-unread)');
              if (!row) return false;
              row.click();
              return true;
            }""")
        if not opened:
            ck.skip("行为段", "实例无已读会话可点")
            browser.close()
            return ck.summary()
        try:
            page.wait_for_selector("#reply-ta", state="visible", timeout=8000)
        except Exception:
            ck.skip("行为段", "composer 未就绪")
            browser.close()
            return ck.summary()
        page.wait_for_timeout(1200)   # 等 _loadXlateForConv/_loadServerDefaultLang 落定

        # 2a. 复位为全关 → 智能建议出现（含一键按钮）
        page.evaluate(
            "() => { const si=document.getElementById('xlate-in'),"
            " so=document.getElementById('xlate-out');"
            " if(si) si.value=''; if(so) so.value=''; _onXlateChange(); }")
        page.wait_for_timeout(300)
        page.click("#xlate-toggle-btn")   # 打开弹层（后续真实点击需要可见）
        page.wait_for_timeout(200)
        st0 = page.evaluate(_STATE_JS)
        ck.check("复位后：开关 off 态", (not st0["btn_on"]) and st0["in_v"] == ""
                 and st0["out_v"] == "", f"st={st0}")
        ck.check("客户语言≠坐席语言且全关 → 智能建议条出现（带按钮）",
                 st0["hint_visible"] and st0["hint_btn"], f"hint={st0['hint_txt'][:70]!r}")

        # 2b. 点建议按钮 → 一键双向开启
        if st0["hint_btn"]:
            page.click("#xl-conv-hint .xl-hint-btn")
            page.wait_for_timeout(400)
        st1 = page.evaluate(_STATE_JS)
        agent = page.evaluate("() => window._agentLang()")
        ck.check("建议接受：对方消息→坐席语言", st1["in_v"] == agent,
                 f"in={st1['in_v']!r} agent={agent!r}")
        ck.check("建议接受：我的消息→自动", st1["out_v"] == "auto", f"out={st1['out_v']!r}")
        ck.check("建议接受：按钮/胶囊进入开启态 + flash",
                 st1["btn_on"] and st1["chip_on"] and st1["chip_flash"],
                 f"chip={st1['chip_txt']!r}")
        ck.check("开启后建议条退场（隐藏且内容清空）",
                 (not st1["hint_visible"]) and not st1["hint_btn"],
                 f"visible={st1['hint_visible']} hint={st1['hint_txt'][:50]!r}")

        # 2c. quick 两态往返：关 → 全清；再开 → 双向亮
        page.click("#xl-quick-btn")
        page.wait_for_timeout(300)
        st2 = page.evaluate(_STATE_JS)
        ck.check("quick 再点=关：双向清空 + 按钮回 off",
                 st2["in_v"] == "" and st2["out_v"] == "" and not st2["btn_on"], f"st={st2}")
        page.click("#xl-quick-btn")
        page.wait_for_timeout(300)
        st3 = page.evaluate(_STATE_JS)
        ck.check("quick 再点=开：双向亮",
                 st3["in_v"] == agent and st3["out_v"] == "auto" and st3["btn_on"])

        # 2d. 同语提示：目标语改成客户语言（vi）→ 提示（无按钮）；改 zh → 隐藏
        page.evaluate(
            "() => { const si=document.getElementById('xlate-in');"
            " if(si) si.value='vi'; _onXlateChange(); }")
        page.wait_for_timeout(300)
        st4 = page.evaluate(_STATE_JS)
        ck.check("目标语==客户语言 → 同语提示（无按钮）",
                 st4["hint_visible"] and not st4["hint_btn"], f"hint={st4['hint_txt'][:60]!r}")
        page.evaluate(
            "() => { const si=document.getElementById('xlate-in');"
            " if(si) si.value='zh'; _onXlateChange(); }")
        page.wait_for_timeout(300)
        st5 = page.evaluate(_STATE_JS)
        ck.check("目标语!=客户语言（已开启态）→ 提示隐藏", not st5["hint_visible"])

        # 2e. 发送方式分段：默认一击直发；点「先预览再发」→ checkbox 同步；点回
        ck.check("分段初始：一击直发 on", st5["seg_direct_on"] and not st5["pv_checked"])
        page.click("#xl-seg-preview")
        page.wait_for_timeout(200)
        st6 = page.evaluate(_STATE_JS)
        ck.check("切「先预览再发」：分段+checkbox 同步",
                 st6["seg_preview_on"] and st6["pv_checked"] and not st6["seg_direct_on"])
        page.click("#xl-seg-direct")
        page.wait_for_timeout(200)
        st7 = page.evaluate(_STATE_JS)
        ck.check("切回「一击直发」：分段+checkbox 同步",
                 st7["seg_direct_on"] and not st7["pv_checked"])

        # 2f. 高级折叠：默认收起 → 展开可达引擎选择；「我的语言」显隐与特性探测一致
        adv = page.evaluate(
            """() => {
              const d = document.getElementById('xl-adv');
              if (!d) return {absent: true};
              const wasOpen = d.open;
              d.open = true;
              const eng = document.getElementById('xlate-engine-sel');
              const row = document.getElementById('xl-agent-lang-row');
              const st = (typeof window._xlAgentLangState==='function')?window._xlAgentLangState():{feat:null};
              return {
                absent: false, was_open: wasOpen,
                engine_visible: !!(eng && eng.getBoundingClientRect().height > 0),
                mylang_shown: !!(row && getComputedStyle(row).display !== 'none'),
                feat: st.feat,
              };
            }""")
        ck.check("高级折叠默认收起", (not adv.get("absent")) and adv.get("was_open") is False,
                 f"adv={adv}")
        # 2026-08-16 老板拍板「坐席不暴露引擎选择」（ui-build 20260816-2110：
        # _XL_ENGINE_PICKER_UI=false 收面，id 契约保留）——断言随产品决策翻转：
        # 展开高级折叠后引擎行必须保持不可见（复显=决策被悄悄回退，先红再谈）。
        ck.check("引擎选择对坐席隐藏（2026-08-16 决策）", not adv.get("engine_visible"))
        ck.check("「我的语言」显隐与特性探测一致（旧后端隐藏/新后端显示）",
                 adv.get("mylang_shown") == (adv.get("feat") is True),
                 f"shown={adv.get('mylang_shown')} feat={adv.get('feat')}")

        # 2g. 管理弹窗：两节编辑行 + 生效链胶囊 + 范围显隐（只渲染不保存）
        page.evaluate("() => _openDefaultLangMgr()")
        try:
            page.wait_for_selector(".dlm-mask", state="visible", timeout=8000)
        except Exception:
            ck.check("管理弹窗打开", False, "dlm-mask 未出现")
            browser.close()
            return ck.summary()
        modal = page.evaluate(
            """() => {
              const m = document.querySelector('.dlm-mask');
              const eds = m.querySelectorAll('.dlm-editor');
              const ed = eds[0] || null;
              const sels = ed ? ed.querySelectorAll('select') : [];
              return {
                editors: eds.length,
                effs: m.querySelectorAll('.dlm-eff').length,
                ed_sel_n: sels.length,
                has_save: !!(ed && ed.querySelector('button')),
              };
            }""")
        ck.check("两节各有「新增/修改」编辑行", modal["editors"] == 2, f"modal={modal}")
        ck.check("编辑行控件齐备（范围/平台/账号/语言 + 保存）",
                 modal["ed_sel_n"] >= 4 and modal["has_save"])
        ck.check("选中会话时显示「当前会话生效」链胶囊 ×2", modal["effs"] == 2,
                 f"effs={modal['effs']}")
        scope_beh = page.evaluate(
            """() => {
              const ed = document.querySelector('.dlm-mask .dlm-editor');
              const sels = ed.querySelectorAll('select');
              const r = {};
              sels[0].value = 'global'; sels[0].onchange();
              r.global_plat_hidden = sels[1].style.display === 'none';
              r.global_acct_hidden = sels[2].style.display === 'none';
              sels[0].value = 'account'; sels[0].onchange();
              r.acct_plat_shown = sels[1].style.display !== 'none';
              r.acct_acct_shown = sels[2].style.display !== 'none';
              return r;
            }""")
        ck.check("范围=全局 → 平台/账号选择隐藏",
                 scope_beh["global_plat_hidden"] and scope_beh["global_acct_hidden"])
        ck.check("范围=账号 → 平台/账号选择显示",
                 scope_beh["acct_plat_shown"] and scope_beh["acct_acct_shown"])
        page.click(".dlm-mask .xlcompare-cancel")
        page.wait_for_timeout(200)
        ck.check("弹窗可关闭", page.evaluate(
            "() => !document.querySelector('.dlm-mask')"))

        page.unroute("**/api/unified-inbox/chats*")

        # ── 3. 405px 桌面壳窄屏：弹层不溢出（P2 视觉收尾专项）─────────────
        print("== 3. 窄屏 405x844（桌面壳档位）==")
        try:
            ctx_n = browser.new_context(viewport={"width": 405, "height": 844},
                                        locale="en-US")
            _swallow_beacons(ctx_n)
            ctx_n.request.post(base + "/login", form={"auth_token": token})
            pg = ctx_n.new_page()
            pg.goto(base + "/workspace", wait_until="domcontentloaded")
            pg.wait_for_function(
                "() => typeof window.setPlatFilter === 'function'", timeout=20000)
            pg.wait_for_timeout(2500)
            # 窄屏移动布局：聊天面板（含翻译弹层的 composer）要先开会话才可见——
            # 不开直接量会得到全 0 的 rect（祖先 display:none），断言空转（首跑实锤）。
            opened_n = pg.evaluate(
                """() => {
                  const row = document.querySelector('#conv-items .conv-item');
                  if (!row) return false;
                  row.click();
                  return true;
                }""")
            geo = {"no_conv": True}
            if opened_n:
                try:
                    pg.wait_for_selector("#reply-ta", state="visible", timeout=8000)
                except Exception:
                    pass
                pg.wait_for_timeout(800)
                geo = pg.evaluate(
                    """() => {
                      const btn = document.getElementById('xlate-toggle-btn');
                      if (!btn) return {absent: true};
                      if (typeof toggleXlatePop === 'function') toggleXlatePop();
                      const pop = document.getElementById('xlate-pop');
                      if (!pop || !pop.classList.contains('show')) return {no_pop: true};
                      const r = pop.getBoundingClientRect();
                      return {
                        absent: false, no_pop: false,
                        left: r.left, right: r.right, top: r.top, bottom: r.bottom,
                        w: r.width, h: r.height,
                        vw: window.innerWidth, vh: window.innerHeight,
                        overflow_x: pop.scrollWidth > pop.clientWidth + 1,
                      };
                    }""")
            if geo.get("no_conv") or geo.get("absent") or geo.get("no_pop") or not geo.get("w"):
                ck.skip("窄屏弹层几何", f"不可量（无会话/弹层未显）geo={geo}")
            else:
                gshow = {k: (round(v) if isinstance(v, float) else v) for k, v in geo.items()}
                ck.check("窄屏：弹层真实渲染且水平不出界",
                         geo["w"] >= 280 and geo["left"] >= -1 and geo["right"] <= geo["vw"] + 1,
                         f"geo={gshow}")
                ck.check("窄屏：弹层顶部不被裁切（垂直可视）", geo["top"] >= 0, f"top={gshow['top']}")
                ck.check("窄屏：弹层内容无横向溢出", not geo["overflow_x"])
            ctx_n.close()
        except Exception as e:  # noqa: BLE001
            ck.skip("窄屏 405px", str(e)[:80])

        browser.close()

    return ck.summary()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="对话翻译弹层 P0+P1 门禁（只读 + route mock，无副作用）")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--token", default="", help="直接给 token（优先于 --data-root）")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--source-only", action="store_true", help="只跑静态接线（不启浏览器）")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    if args.source_only:
        ck = Checker()
        check_source_wiring(ck)
        return ck.summary("对话翻译弹层源码接线")

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("[WARN] 未装 playwright；仅跑静态源码接线")
        ck = Checker()
        check_source_wiring(ck)
        return ck.summary("对话翻译弹层源码接线（无 playwright）")

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
        code = ck.summary("对话翻译弹层源码接线（实例不可达）")
        return 0 if code == 0 else code

    token = args.token or read_token(args.data_root)
    if not token:
        print(f"[ABORT] 未能从 {args.data_root} 读到 web_admin.auth_token")
        return 2

    print(f"== 目标 {args.base}（视口 {VIEWPORT['width']}x{VIEWPORT['height']}）==")
    return run(args.base, token, headed=args.headed)


if __name__ == "__main__":
    sys.exit(main())
