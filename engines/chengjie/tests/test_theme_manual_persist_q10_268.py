# -*- coding: utf-8 -*-
"""Q-10 B（#268 N9JQ3M / #267 4KEJ63，2026-09-10）：主题手动选档退出定时 + 持久化不再依赖 URL 钉。

事故：① 外观快捷面板的夜间四档 chip 直写 state.night.mode 绕过 setNightMode → 被钉窗口
（桌面壳 ?theme=dark）高亮会动、画面不变，退出定时也无提示；② URL 钉的释放标记只在
sessionStorage（窗口级）→ 刷新 / 切页 / 顶栏胶囊 / 重启壳后钉复活，用户的亮色又被盖回深色。

钉住（行为层用 node + 最小 DOM 桩真跑 workspace_base 头部脚本 + appearance.js）：
- 首次带钉：钉生效（深色壳里不白）；显式选亮色 → 当场生效 + localStorage(cp_theme_user_set)=1；
- 新窗口再带 ?theme=dark：钉被忽略（__cpThemePin=null），画面按用户持久选择；
- 定时中点亮/暗色 → cp_theme_sched_handoff=1，用户菜单定时行显「定时已手动接管 · 恢复定时」，
  快捷面板定时 chip 带「（已手动接管）」；再选定时 → 标记清除；
- console 一行 `theme change source=manual|schedule|system|restore|sync value=… mode=…`；
- 静态：快捷面板 chip 走 setNightMode；i18n 键 zh/en 齐；?v= 戳已升。

与 tests/test_theme_pin_window_local.py 的关系：那里禁的是「把**钉**写进共享键」与「释放标记
进 localStorage」——本线写进 localStorage 的是**用户选择过**这一事实（cp_theme_user_set），
不是钉值也不是 PIN_OFF；钉仍只在「用户从未选过」时当出厂默认。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WEB = REPO / "src" / "web"
BASE = WEB / "templates" / "workspace_base.html"
ENGINE = WEB / "static" / "workspace" / "appearance.js"
NODE = shutil.which("node")


def _base_head_script() -> str:
    src = BASE.read_text(encoding="utf-8")
    m = re.search(r"<script>\n(/\* 主题应用提升到 base[\s\S]*?)</script>", src)
    assert m, "workspace_base 主题头部脚本找不到（改了开头注释？请同步本测试）"
    body = m.group(1)
    assert "{{" not in body and "{%" not in body, "头部脚本里不该有 Jinja 定界符"
    return body


_HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const [,, baseHeadPath, enginePath] = process.argv;
const baseHead = fs.readFileSync(baseHeadPath, 'utf8');
const engine = fs.readFileSync(enginePath, 'utf8');

function makeStorage(init) {
  const m = Object.assign({}, init || {});
  return {
    getItem: k => (Object.prototype.hasOwnProperty.call(m, k) ? m[k] : null),
    setItem: (k, v) => { m[k] = String(v); },
    removeItem: k => { delete m[k]; },
    _dump: () => Object.assign({}, m),
  };
}
function el(id) {
  return {
    id, style: {}, textContent: '', _attrs: {}, _cls: new Set(),
    classList: { toggle(c, on) { on ? this._s.add(c) : this._s.delete(c); }, contains(c) { return this._s.has(c); }, add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, _s: new Set() },
    getAttribute(k) { return this._attrs[k] == null ? null : this._attrs[k]; },
    setAttribute(k, v) { this._attrs[k] = String(v); },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    addEventListener() {},
  };
}
function newWindow(search, localStorage, extraEls) {
  const logs = [];
  const els = {};
  (extraEls || []).forEach(id => { els[id] = el(id); });
  const root = el('html');
  root.style = { setProperty() {}, removeProperty() {} };
  const doc = {
    documentElement: root, readyState: 'complete', hidden: false,
    getElementById: id => els[id] || null,
    querySelectorAll: () => [], querySelector: () => null,
    addEventListener() {}, startViewTransition: undefined,
  };
  const sb = {
    document: doc,
    location: { search, pathname: '/workspace', href: 'http://x/workspace' + search, origin: 'http://x' },
    localStorage, sessionStorage: makeStorage(),
    navigator: { sendBeacon() { return true; } },
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    fetch: () => Promise.resolve({ ok: false }),
    console: { info: (...a) => logs.push(a), log() {}, warn() {}, error() {} },
    setTimeout: () => 0, setInterval: () => 0, clearTimeout() {}, clearInterval() {},
    addEventListener() {}, removeEventListener() {},
    URLSearchParams, URL, Blob: function () {},
  };
  sb.window = sb;
  sb.globalThis = sb;
  vm.createContext(sb);
  vm.runInContext(baseHead, sb, { filename: 'workspace_base_head.js' });
  vm.runInContext(engine, sb, { filename: 'appearance.js' });
  sb.__logs = logs;
  sb.__els = els;
  return sb;
}
function fmt(a) { let s = String(a[0]); for (let i = 1; i < a.length; i++) s = s.replace(/%s/, String(a[i])); return s; }
const out = {};
const ls = makeStorage();

// S1: fresh profile, pinned window (desktop shell ?theme=dark) -> pin wins, then user picks light
let w = newWindow('?theme=dark', ls);
out.s1_boot_theme = w.document.documentElement.getAttribute('data-cp-theme');
out.s1_boot_pin = w.__cpThemePin;
out.s1_boot_log = w.__logs.map(fmt);
w.WSAppearance.setNightMode('light');
out.s1_after_theme = w.document.documentElement.getAttribute('data-cp-theme');
out.s1_after_pin = w.__cpThemePin;
out.s1_after_log = w.__logs.map(fmt);
out.s1_ls = ls._dump();

// S2: new window in the same profile, still carrying ?theme=dark -> user's pick persists
w = newWindow('?theme=dark', ls);
out.s2_pin = w.__cpThemePin;
out.s2_theme = w.document.documentElement.getAttribute('data-cp-theme');
out.s2_log = w.__logs.map(fmt);

// S3: schedule -> manual dark (handoff) -> user menu row -> back to schedule clears it
const menuIds = ['ws-theme-seg', 'ws-theme-eff', 'ws-theme-sched', 'ws-theme-sched-txt', 'ws-theme-sched-edit', 'ws-theme-sched-resume'];
w = newWindow('', ls, menuIds);
w.WSAppearance.setNightMode('schedule');
out.s3_handoff_after_schedule = ls.getItem('cp_theme_sched_handoff');
w.WSAppearance.setNightMode('dark');
out.s3_handoff_after_dark = ls.getItem('cp_theme_sched_handoff');
out.s3_mode = w.WSAppearance.get().night.mode;
w.cpSyncThemeSeg();
out.s3_row_display = w.__els['ws-theme-sched'].style.display;
out.s3_row_txt = w.__els['ws-theme-sched-txt'].textContent;
out.s3_resume_display = w.__els['ws-theme-sched-resume'].style.display;
out.s3_edit_display = w.__els['ws-theme-sched-edit'].style.display;
w.cpSetTheme('schedule');
out.s3_handoff_after_resume = ls.getItem('cp_theme_sched_handoff');
out.s3_mode_after_resume = w.WSAppearance.get().night.mode;
w.cpSyncThemeSeg();
out.s3_resume_display_after = w.__els['ws-theme-sched-resume'].style.display;
out.s3_log = w.__logs.map(fmt);
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    if not NODE:
        pytest.skip("node 不可用，跳过行为层")
    d = tmp_path_factory.mktemp("theme_q10")
    (d / "base_head.js").write_text(_base_head_script(), encoding="utf-8")
    (d / "harness.js").write_text(_HARNESS, encoding="utf-8")
    p = subprocess.run([NODE, str(d / "harness.js"), str(d / "base_head.js"), str(ENGINE)],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_pin_wins_until_first_explicit_pick_then_persists(run):
    assert run["s1_boot_pin"] == "dark" and run["s1_boot_theme"] == "dark"      # 深色壳里不白
    assert any("source=restore" in ln for ln in run["s1_boot_log"])
    assert run["s1_after_pin"] is None and run["s1_after_theme"] == "light"     # 点了就变
    assert run["s1_ls"].get("cp_theme_user_set") == "1"
    assert run["s1_ls"].get("cp_theme") == "light"
    assert any(ln == "theme change source=manual value=light mode=light" for ln in run["s1_after_log"]), run["s1_after_log"]


def test_new_window_with_theme_param_keeps_user_choice(run):
    # 4KEJ63：刷新 / 切页 / 顶栏胶囊都会带 ?theme=dark 回来——不再当钉，按用户持久选择渲染
    assert run["s2_pin"] is None
    assert run["s2_theme"] == "light"
    assert any("source=restore value=light mode=light" in ln for ln in run["s2_log"]), run["s2_log"]


def test_manual_pick_exits_schedule_with_handoff_mark_and_resume(run):
    assert run["s3_handoff_after_schedule"] is None
    assert run["s3_handoff_after_dark"] == "1" and run["s3_mode"] == "dark"
    assert run["s3_row_display"] == "flex"
    assert run["s3_row_txt"] == "base.theme.sched_handoff"         # 无 i18n 包时 T() 回落键名
    assert run["s3_resume_display"] == "" and run["s3_edit_display"] == "none"
    # 「恢复定时」→ cpSetTheme('schedule') → 标记清除、行回到定时窗显示
    assert run["s3_handoff_after_resume"] is None and run["s3_mode_after_resume"] == "schedule"
    assert run["s3_resume_display_after"] == "none"
    assert any("source=manual value=dark mode=dark" in ln for ln in run["s3_log"]), run["s3_log"]
    assert any("mode=schedule" in ln for ln in run["s3_log"])


# ── 静态契约 ──────────────────────────────────────────────────────────────────

def test_quick_panel_night_chips_go_through_single_write_path():
    eng = ENGINE.read_text(encoding="utf-8")
    m = re.search(r"else if \(el\.hasAttribute\('data-ap-night'\)\) \{([\s\S]*?)\} else if \(el\.hasAttribute\('data-ap-fs'\)\)", eng)
    assert m, "快捷面板夜间 chip 处理分支找不到"
    body = m.group(1)
    assert "setNightMode(el.getAttribute('data-ap-night'))" in body
    assert "state.night.mode =" not in body                       # 不许再直写绕过单一写路径
    assert "function logThemeChange" in eng and "'theme change source=%s value=%s mode=%s'" in eng
    for src in ("'manual'", "'schedule'", "'system'", "'restore'", "'sync'"):
        assert f"applyAll(false, {src})" in eng or f"applyAll(true, {src})" in eng, src
    assert "localStorage.getItem(USER_SET_KEY) === '1') _urlPin = null" in eng


def test_base_head_honours_user_set_and_menu_has_resume_link():
    base = BASE.read_text(encoding="utf-8")
    assert "localStorage.getItem(USER_SET)==='1') PIN=null" in base
    assert "localStorage.setItem(USER_SET,'1')" in base            # 释放入口同时落持久标记
    assert 'id="ws-theme-sched-resume"' in base and "cpSetTheme('schedule')" in base
    assert "cp_theme_sched_handoff" in base
    # 缓存戳已升（appearance.js 两处引用同戳）
    ps = (WEB / "templates" / "personal_settings.html").read_text(encoding="utf-8")
    stamps = set(re.findall(r"appearance\.js\?v=(\w+)", base + ps))
    assert len(stamps) == 1 and stamps != {"20260816a"}, stamps
    assert "'ap.night.handoff'" in ps                               # 设置页 _apk 暴露给引擎
    from src.web.i18n_packs.appearance_page import EN as AEN, ZH as AZH
    from src.web.i18n_packs.workspace_shell import EN as SEN, ZH as SZH
    assert "ap.night.handoff" in AZH and "ap.night.handoff" in AEN
    for k in ("base.theme.sched_handoff", "base.theme.sched_resume"):
        assert k in SZH and k in SEN, k
