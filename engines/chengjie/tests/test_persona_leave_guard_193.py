"""#193「页面未能打开: /admin/voice-eval」回归钉（J-7 E，2026-09-05）。

真因不是路由缺失 / 角色门 / chunk 404：`/admin/voice-eval` 一直注册在 admin.py（session auth，
无角色门），侧栏 `voice_eval` 项也随 nav_schema 正常渲染。链条是——

  personas.html 的「全局规则」tab 用 beforeunload 守未保存改动（_grDirty）
  → 桌面壳（Electron）对 beforeunload 拦截的默认处理是**静默取消导航**，不弹对话框
  → 坐席点侧栏「声音评测」像没反应；页面早绘制载入层（_loading_overlay.html）在点击时已亮，
    15s 后文档仍在 → 误报「页面未能打开: /admin/voice-eval [重试打开]」。

两层修法各钉一条：
  ① 页面侧：站内链接点击在 window 捕获段显式 confirm（先于载入层的 document 捕获监听），
     留下 → preventDefault + stopPropagation（载入层不亮）；离开 → 清 _grDirty 放行。
  ② 壳侧：main.js 对承载后台页的全部 webContents 挂 will-prevent-unload → 原生「离开 / 留下」，
     选离开 preventDefault 放行；popup / 主窗 / 工作台 webview 三处都接。
另钉住前提事实：voice-eval 路由仍在、nav 项仍指向它——若将来有人真的把路由拿掉而入口留着，
本测试会先红。
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PERSONAS = _ROOT / "src" / "web" / "templates" / "personas.html"
_MAIN_JS = _ROOT / "desktop" / "main.js"
_OVERLAY = _ROOT / "src" / "web" / "templates" / "_loading_overlay.html"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_voice_eval_route_and_nav_entry_consistent():
    from src.web import nav_schema
    item = nav_schema.NAV_ITEMS["voice_eval"]
    assert item["path"] == "/admin/voice-eval"
    routes_src = _read(_ROOT / "src" / "web" / "routes" / "voice_eval_routes.py")
    assert '@app.get("/admin/voice-eval")' in routes_src
    admin_src = _read(_ROOT / "src" / "web" / "admin.py")
    assert "register_voice_eval_routes(" in admin_src
    # 页面走 session auth（_page_auth），没有角色门——入口对所有登录用户都应打得开
    assert re.search(r"register_voice_eval_routes\(\s*app,\s*page_auth=_page_auth", admin_src)


def test_personas_link_click_guard_runs_before_loading_overlay():
    src = _read(_PERSONAS)
    # 捕获段 window 监听（比 _loading_overlay 的 document 捕获早一跳）
    m = re.search(
        r"window\.addEventListener\('click',\s*function\s*\(e\)\s*\{(?P<body>.*?)\n\},\s*true\);",
        src, re.S,
    )
    assert m, "personas.html 缺 window 捕获段的离开守卫"
    body = m.group("body")
    assert "if (!_grDirty) return;" in body
    assert "confirm(window.T('psn_gr_leave_confirm'))" in body
    assert "_grDirty = false; return;" in body, "确认离开必须清脏标放行（否则 beforeunload 二次拦）"
    assert "e.preventDefault(); e.stopPropagation();" in body, "留下必须阻止导航并截住载入层"
    # 与载入层同一套判定口径：命名窗口链接（__openUnique 不导航）不该被问
    assert "data-winname" in body
    assert "u.origin !== location.origin" in body
    # beforeunload 兜底仍在（浏览器关标签页 / 地址栏跳转）
    assert "window.addEventListener('beforeunload'" in src


def test_loading_overlay_respects_default_prevented():
    """载入层对 defaultPrevented 的点击必须让路——这是页面侧守卫能「不亮层」的前提。"""
    src = _read(_OVERLAY)
    assert "if(e.defaultPrevented" in src
    assert "addEventListener('click'" in src and "},true);" in src.replace(" ", "")


def test_shell_will_prevent_unload_dialog_wired_everywhere():
    src = _read(_MAIN_JS)
    assert "function wireUnloadGuard(wc, ownerWin)" in src
    assert 'wc.on("will-prevent-unload"' in src
    assert "dialog.showMessageBoxSync" in src
    # 离开 = 按钮 0 → preventDefault 忽略 beforeunload；默认/Esc = 留下
    assert re.search(r"leave = \(r === 0\);", src)
    assert re.search(r"defaultId:\s*1,\s*\n\s*cancelId:\s*1", src)
    assert "if (leave) e.preventDefault();" in src
    # 三处承载后台页的 webContents 都接上
    assert "wireUnloadGuard(child.webContents, child);" in src, "后台弹窗（admin/wsub 槽）"
    assert "wireUnloadGuard(win.webContents, win);" in src, "主窗"
    assert "wireUnloadGuard(wc, win);" in src, "工作台 webview"
    # 词条 zh/en 齐（shell-i18n.test.js 另守双语对等；这里钉键名不漂）
    for k in ("unload.title", "unload.msg", "unload.detail", "unload.leave", "unload.stay"):
        assert src.count(f'"{k}"') >= 2, f"SHELL_STR 缺 {k} 的 zh/en 之一"


def test_i18n_keys_present_both_langs():
    from src.web.i18n_packs import persona_studio
    for k in ("psn_gr_leave_confirm", "psn_gr_leave_stay"):
        assert k in persona_studio.ZH and k in persona_studio.EN
