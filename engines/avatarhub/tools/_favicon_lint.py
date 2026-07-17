# -*- coding: utf-8 -*-
"""窗口身份门禁（2026-07-16 四视角 P1-1）：favicon 双格式 + theme-color + 最大化公约。

背景（当日实锤事故）：桌面端全部站内页面用 Edge/Chrome --app 应用窗口打开，窗口
任务栏/标题栏图标取自页面 favicon 的**位图**——全站只挂 SVG favicon 且 /favicon.ico 404
→ 所有窗口退化成 Edge 图标；未挂 theme-color 的深色页顶着浅色标题栏。
公约（设计规范_图标与令牌.md · 三）：
  1) 应用窗口页面必须双格式 favicon：icon.svg（标签页）+ app-icon-256.png（窗口/任务栏）；
  2) 页面必须挂 <meta name="theme-color">（标题栏跟品牌深色）；
  3) 带 UI 的服务（Hub 9000 / faceswap 8000 / 同传 7900）必须提供 GET /favicon.ico 兜底；
  4) launcher_qt._open_app_window 默认最大化（--start-maximized）。
exit 0=通过 / 1=违约（进 run_all_tests 套件）。新增应用窗口页面 → 补齐三件套后加进 PAGES。
"""
import io
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent

fails = []


def ok(msg):
    print(f"  [OK] {msg}")


def ng(msg):
    fails.append(msg)
    print(f"  [NG] {msg}")


# 应用窗口 / 客户可达页面（必须三件套）。豁免（不在此列即豁免，豁免原因备案）：
#   report.html=战报刻意零外链单文件 · ops_share.html/wall.html/highlights.html=分享只读页
#   sales_onepager.html=打印一页纸 · _setmode.html=门禁跳转桩 · phone.html.bak*=备份
PAGES = ["ui.html", "home.html", "phone.html", "dashboard.html", "ops.html",
         "delivery.html", "verify.html", "ask.html", "converse.html",
         "order.html", "download.html", "setup.html", "landing.html"]

RE_PNG_ICON = re.compile(r'rel="icon"[^>]*app-icon-256\.png|app-icon-256\.png[^>]*rel="icon"')
RE_THEME = re.compile(r'<meta\s+name=["\']?theme-color')


def main() -> int:
    for name in PAGES:
        p = ROOT / "static" / name
        if not p.exists():
            ng(f"static/{name} 不存在（页面改名请同步本清单）")
            continue
        html = io.open(p, encoding="utf-8").read()
        head = html.split("</head>", 1)[0]
        miss = []
        if 'rel="icon"' not in head:
            miss.append("favicon 链接")
        if not RE_PNG_ICON.search(head):
            miss.append("PNG 位图 favicon（app-icon-256.png，窗口/任务栏图标必需）")
        if not RE_THEME.search(head):
            miss.append("theme-color（标题栏品牌深色）")
        if miss:
            ng(f"static/{name} 缺 {' + '.join(miss)}")
        else:
            ok(f"static/{name} favicon 双格式 + theme-color 齐全")

    # Hub：/favicon.ico 兜底路由 + 教程模板三件套
    hub = io.open(ROOT / "avatar_hub.py", encoding="utf-8").read()
    if '@app.get("/favicon.ico"' in hub:
        ok("avatar_hub /favicon.ico 兜底路由")
    else:
        ng("avatar_hub 缺 /favicon.ico 路由（应用窗口图标兜底）")
    tmpl_m = re.search(r"_HELP_TMPL\s*=\s*\"\"\"(.*?)\"\"\"", hub, re.S)
    tmpl = tmpl_m.group(1) if tmpl_m else ""
    if "app-icon-256.png" in tmpl and "theme-color" in tmpl:
        ok("avatar_hub /help 模板 favicon + theme-color")
    else:
        ng("avatar_hub /help 模板缺 favicon 位图或 theme-color")

    # faceswap(8000)：路由 + 页面 head
    fsw = io.open(ROOT / "faceswap_api.py", encoding="utf-8").read()
    if '@app.get("/favicon.ico"' in fsw:
        ok("faceswap /favicon.ico 路由")
    else:
        ng("faceswap 缺 /favicon.ico 路由")
    if 'rel="icon"' in fsw and "theme-color" in fsw:
        ok("faceswap /ui 面板 favicon + theme-color")
    else:
        ng("faceswap /ui 面板缺 favicon 或 theme-color")

    # 同传(7900)：路由 + 5 个内嵌页 favicon（主页/复盘/字幕层/术语表/会话导出）
    li = io.open(ROOT / "live_interpreter.py", encoding="utf-8").read()
    if '@app.get("/favicon.ico"' in li:
        ok("live_interpreter /favicon.ico 路由")
    else:
        ng("live_interpreter 缺 /favicon.ico 路由")
    n_icon = len(re.findall(r"<link rel=icon", li))
    if n_icon >= 5:
        ok(f"live_interpreter 内嵌页 favicon 链接 ×{n_icon}")
    else:
        ng(f"live_interpreter 内嵌页 favicon 仅 {n_icon} 处（应 ≥5：主页/复盘/字幕层/术语表/会话导出）")

    # 桌面启动台：应用窗口默认最大化公约
    lq = io.open(ROOT / "launcher_qt.py", encoding="utf-8").read()
    if "--start-maximized" in lq and "AVATARHUB_APP_MAXIMIZED" in lq:
        ok("launcher_qt 应用窗口默认最大化（--start-maximized + 可关开关）")
    else:
        ng("launcher_qt 应用窗口未按公约默认最大化")

    print(("通过" if not fails else f"失败 {len(fails)} 项"))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
