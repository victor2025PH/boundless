# -*- coding: utf-8 -*-
"""原生窗口壳（P3-2 灰度通道 · P4-A 移至根目录支持冻结态打包）：WebView2 承载站内页面。

与 Edge --app 的差别：窗口属于本进程 → 任务栏身份（AppUserModelID）与图标（WM_SETICON /
冻结后 exe 图标）100% 可控，不再受浏览器 favicon 行为摆布（P2-5 POC 结论：可行）。

接入方式（默认关闭，逐台灰度）：
    set AVATARHUB_APP_SHELL=webview          启动台改走本壳开窗（launcher_qt._open_app_window）
    set AVATARHUB_APP_SHELL_PY=<python.exe>  开发态指定装有 pywebview 的解释器（默认 .venv_launcher\\pythonw.exe）
冻结态（AvatarHub.exe）：`AvatarHub.exe --webview-shell <url>` 子进程入口（launcher_qt.main 顶部派发）；
    需 pywebview 进打包清单（AvatarHub.spec hiddenimports，体积实测 ≈5.4MB 裸增量）——
    未打包时 available()=False，启动台自动回退 Edge --app。
依赖用 importlib 字符串导入：PyInstaller 静态分析不会自动吞入 pywebview，
    打包与否的决策显式留在 spec（防"装了构建环境就悄悄进包"）。

直接运行（调试/验收）：
    python webview_shell.py http://127.0.0.1:9000/ui [--title 标题] [--autoclose 秒]
"""
import argparse
import ctypes
import importlib
import importlib.util
import sys
import threading
import time
from pathlib import Path

if getattr(sys, "stdout", None) is not None and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def _base_dir() -> Path:
    """资源根。冻结态=exe 旁（装机布局 {app}\\assets\\app.ico 与 data\\ 同级）；
    dist\\ 下直跑开发验证时 exe 旁无 assets → 回退上一级（项目根）。脚本态=本文件目录。"""
    if getattr(sys, "frozen", False):
        d = Path(sys.executable).resolve().parent
        if (d / "assets" / "app.ico").exists():
            return d
        if (d.parent / "assets" / "app.ico").exists():
            return d.parent
        return d
    return Path(__file__).resolve().parent


ROOT = _base_dir()
ICON = ROOT / "assets" / "app.ico"
STORAGE = ROOT / "data" / "webview_profile"   # 持久化 localStorage/Cookie（hub_tab/演示模式等偏好不丢）


def available() -> bool:
    """pywebview 是否就位（父进程派发前探测，缺依赖直接回退 Edge，不空转子进程）。"""
    try:
        return importlib.util.find_spec("webview") is not None
    except Exception:
        return False


def _find_own_hwnd() -> int:
    """按进程号枚举顶层窗口（标题会被页面 title 动态改写，不能按标题找）。"""
    user32 = ctypes.windll.user32
    pid = ctypes.windll.kernel32.GetCurrentProcessId()
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _cb(hwnd, _):
        wpid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(_cb, None)
    return found[0] if found else 0


def _inject_icon(ensure_max: bool = False):
    """窗体就绪后挂 assets/app.ico（任务栏/标题栏/Alt-Tab）。冻结进 exe 后本步自动多余但无害。
    ensure_max：对最大化再上一道保险（个别 WebView2 版本 maximized 参数首帧不落地）。"""
    user32 = ctypes.windll.user32
    LR_LOADFROMFILE, IMAGE_ICON, WM_SETICON, SW_MAXIMIZE = 0x0010, 1, 0x0080, 3
    for _ in range(20):                      # 最多等 ~5s（页面重、窗体晚建）
        time.sleep(0.25)
        hwnd = _find_own_hwnd()
        if hwnd:
            if ICON.exists():
                for wparam, px in ((0, 16), (1, 32)):    # ICON_SMALL / ICON_BIG
                    hicon = user32.LoadImageW(None, str(ICON), IMAGE_ICON, px, px, LR_LOADFROMFILE)
                    if hicon:
                        user32.SendMessageW(hwnd, WM_SETICON, wparam, hicon)
            if ensure_max and not user32.IsZoomed(hwnd):
                user32.ShowWindow(hwnd, SW_MAXIMIZE)
            return


def run(url: str, title: str = "无界 BOUNDLESS", autoclose: float = 0, windowed: bool = False) -> int:
    # GUI 子系统 stdout/stderr=None：pywebview 内部异常会被 logging 静默丢弃（冻结态排障两眼一抹黑，
    # 2026-07-16 实锤：进程存活 0 窗口无任何线索）。根 logger 落盘 logs\webview_shell.log。
    import logging
    try:
        _ld = ROOT / "logs"
        _ld.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(filename=str(_ld / "webview_shell.log"), level=logging.WARNING,
                            format="%(asctime)s [%(name)s] %(levelname)s %(message)s", encoding="utf-8")
    except Exception:
        pass

    # 任务栏身份：与浏览器/裸 python 分组彻底解耦
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Boundless.AvatarHub")

    webview = importlib.import_module("webview")   # 字符串导入：打包决策显式留在 spec
    win = webview.create_window(title, url, maximized=not windowed,
                                width=1280, height=850)

    def _sync_title():
        """页面加载后把窗口标题同步为 document.title（对齐 Edge --app 的行为）。"""
        try:
            t = win.evaluate_js("document.title")
            if t:
                win.set_title(str(t))
        except Exception:
            pass

    win.events.loaded += _sync_title
    threading.Thread(target=_inject_icon, kwargs={"ensure_max": not windowed}, daemon=True).start()
    if autoclose > 0:
        def _later():
            time.sleep(autoclose)
            try:
                win.destroy()
            except Exception:
                pass
        threading.Thread(target=_later, daemon=True).start()

    # 排障备忘（2026-07-16 发版实测）：onefile exe 是【bootloader 父 + 运行时子】双进程，
    # 窗口/事件循环都在子进程——按父 pid 找窗口必空，faulthandler 里 winforms app.Run()
    # 的驻留栈是消息循环常态而非卡死。误判过一轮，特此留档。
    STORAGE.mkdir(parents=True, exist_ok=True)
    try:
        webview.start(gui="edgechromium", private_mode=False, storage_path=str(STORAGE))
    except BaseException:
        logging.exception("webview.start 崩溃")
        raise
    logging.info("shell exited ok")
    if getattr(sys, "stdout", None):    # GUI 子系统 stdout=None，裸 print 会炸
        print("shell exited ok")
    return 0


def run_cli(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--title", default="无界 BOUNDLESS")
    ap.add_argument("--autoclose", type=float, default=0, help="秒数>0 时自动关闭（验收用）")
    ap.add_argument("--windowed", action="store_true", help="不最大化（AVATARHUB_APP_MAXIMIZED=0 通道）")
    a = ap.parse_args(argv)
    return run(a.url, a.title, a.autoclose, a.windowed)


if __name__ == "__main__":
    sys.exit(run_cli())
