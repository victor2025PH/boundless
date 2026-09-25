# -*- coding: utf-8 -*-
"""个人微信 PC 副驾 · 接入引导第 ① 步「环境检测」（Windows）。

只用 Win32（EnumWindows / 进程映像路径 / 文件版本信息）找窗；UIA 只做**单窗** ``ControlFromHandle`` 类名精判
（毫秒级、结果缓存），绝不做根枚举——引导页每几秒轮询一次，不能拖慢微信。
返回 ``{os_windows, running, main_window, login_window, version, version_ok, exe_path, pid, session, reason}``；
``version_ok`` = 主版本 ≥ 4（4.x 的 ``mmui::*`` 锚点是驱动的前提）。纯函数 :func:`parse_version` / :func:`version_ok` 可单测。
``session`` 见 :func:`session_isolation`——引擎与微信不在同一会话时窗口枚举恒为空，
``reason=engine_not_interactive`` 把这种「看不见」和「微信真没开」区分开。
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, Optional, Tuple

MIN_MAJOR = 4
_VER_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def parse_version(text: Any) -> Tuple[int, ...]:
    m = _VER_RE.search(str(text or ""))
    if not m:
        return ()
    return tuple(int(g) for g in m.groups() if g is not None)


def version_ok(text: Any, min_major: int = MIN_MAJOR) -> bool:
    v = parse_version(text)
    return bool(v) and v[0] >= int(min_major)


#: 「发语音」按钮（PC 端直接录语音消息）自 4.1.9 起有；更早的 4.x 只有「语音输入文字」
VOICE_MIN_VERSION = (4, 1, 9)


def voice_version_ok(text: Any, min_version: Tuple[int, ...] = VOICE_MIN_VERSION) -> bool:
    v = parse_version(text)
    if not v:
        return False
    n = len(min_version)
    return tuple(list(v) + [0] * n)[:n] >= tuple(min_version)


_LOGIN_TITLES = ("weixin", "登录", "login")
#: 登录窗外框上限（4.1.13 真机 280×380；主窗最小也 ≥ 600 宽）——标题同为「WeChat」时靠尺寸分
LOGIN_MAX_W, LOGIN_MAX_H = 520, 640


def classify_window(class_name: Any, title: Any, width: int = 0, height: int = 0) -> str:
    """微信顶层窗口 → ``'main'`` / ``'login'`` / ``''``（纯函数）。

    4.x 真机（4.1.13 实测）顶层是 Qt 窗：类名 ``Qt51514QWindowIcon``；``mmui::MainWindow`` / ``mmui::LoginWindow``
    只是 UIA 树里的类名，**不是**顶层类名——置前/环境检测得按标题分类，否则托盘态永远找不到窗口。
    标题：主窗「微信」/「WeChat」，登录窗多为「Weixin」/「登录」，**但英文界面下登录窗标题也叫「WeChat」**
    （2026-09-19 二次联调实锤：刚拉起的登录窗被判成主窗 → 引导页显示"已登录"）。同名时靠外框尺寸分：
    登录窗是 280×380 的小窗，主窗 ≥ 600 宽。尺寸缺省 0 = 不知道（最小化态），按标题给主窗。
    精判交给 :func:`window_kind`（有 uiautomation 时用 UIA 类名一锤定音）。
    """
    cls = str(class_name or "")
    t = str(title or "").strip()
    if cls == "mmui::MainWindow":
        return "main"
    if cls == "mmui::LoginWindow":
        return "login"
    if cls.startswith("Qt"):
        if t.lower() in _LOGIN_TITLES:
            return "login"
        if t == "微信" or t.lower() == "wechat":
            if width and height and width <= LOGIN_MAX_W and height <= LOGIN_MAX_H:
                return "login"
            return "main"
    return ""


_UIA_KIND_CACHE: Dict[Tuple[int, int], str] = {}


def _uia_kind(hwnd: int, pid: int = 0) -> str:  # pragma: no cover - 平台相关
    """单窗 ``ControlFromHandle`` 取 UIA 类名（毫秒级；60 秒的坑是根枚举，不在这里）。没装 uiautomation → ``''``。
    结果按 (pid, hwnd) 缓存：同一窗口的类名不会变。"""
    key = (int(pid), int(hwnd))
    if key in _UIA_KIND_CACHE:
        return _UIA_KIND_CACHE[key]
    kind = ""
    try:
        import uiautomation as uia  # type: ignore
        c = uia.ControlFromHandle(int(hwnd))
        cls = str(getattr(c, "ClassName", "") or "")
        kind = "main" if cls == "mmui::MainWindow" else "login" if cls == "mmui::LoginWindow" else ""
    except Exception:
        kind = ""
    if kind:
        _UIA_KIND_CACHE[key] = kind
        if len(_UIA_KIND_CACHE) > 64:
            _UIA_KIND_CACHE.pop(next(iter(_UIA_KIND_CACHE)))
    return kind


def window_kind(w: Any) -> str:  # pragma: no cover - 平台相关
    """``TopWindow`` → ``main`` / ``login`` / ``''``：先按类名+标题+尺寸粗筛（非微信窗直接出局），
    是微信窗再用 UIA 类名精判；UIA 不可用时以粗筛结果为准。"""
    rough = classify_window(w.class_name, w.title, int(getattr(w, "width", 0) or 0), int(getattr(w, "height", 0) or 0))
    if not rough:
        return ""
    return _uia_kind(w.hwnd, w.pid) or rough


def _exe_path_of(pid: int) -> str:  # pragma: no cover - 平台相关
    if os.name != "nt" or not pid:
        return ""
    import ctypes
    from ctypes import wintypes as wt
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    k32.QueryFullProcessImageNameW.restype = wt.BOOL
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        k32.CloseHandle(h)


def _file_version(path: str) -> str:  # pragma: no cover - 平台相关
    if os.name != "nt" or not path or not os.path.exists(path):
        return ""
    import ctypes
    from ctypes import wintypes as wt
    ver = ctypes.windll.version
    ver.GetFileVersionInfoSizeW.argtypes = [wt.LPCWSTR, ctypes.POINTER(wt.DWORD)]
    ver.GetFileVersionInfoSizeW.restype = wt.DWORD
    ver.GetFileVersionInfoW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p]
    ver.GetFileVersionInfoW.restype = wt.BOOL
    ver.VerQueryValueW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wt.UINT)]
    ver.VerQueryValueW.restype = wt.BOOL
    size = ver.GetFileVersionInfoSizeW(path, None)
    if not size:
        return ""
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(path, 0, size, buf):
        return ""
    ptr = ctypes.c_void_p()
    ln = wt.UINT()
    if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(ln)) or not ptr.value:
        return ""

    class VS_FIXEDFILEINFO(ctypes.Structure):
        _fields_ = [("dwSignature", wt.DWORD), ("dwStrucVersion", wt.DWORD),
                    ("dwFileVersionMS", wt.DWORD), ("dwFileVersionLS", wt.DWORD),
                    ("dwProductVersionMS", wt.DWORD), ("dwProductVersionLS", wt.DWORD)]

    info = ctypes.cast(ptr, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
    ms, ls = info.dwProductVersionMS, info.dwProductVersionLS
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def current_session_id() -> int:
    """本进程所在的终端服务会话（拿不到 = -1）。"""
    if os.name != "nt":
        return -1
    try:
        import ctypes
        sid = ctypes.c_ulong(0)
        k32 = ctypes.windll.kernel32
        if not k32.ProcessIdToSessionId(k32.GetCurrentProcessId(), ctypes.byref(sid)):
            return -1
        return int(sid.value)
    except Exception:
        return -1


def console_session_id() -> int:
    """当前控制台（交互桌面）会话；无人登录时 Windows 给 0xFFFFFFFF → -1。"""
    if os.name != "nt":
        return -1
    try:
        import ctypes
        sid = int(ctypes.windll.kernel32.WTSGetActiveConsoleSessionId())
        return -1 if sid in (0xFFFFFFFF, -1) else sid
    except Exception:
        return -1


def session_isolation() -> Dict[str, Any]:
    """引擎与交互桌面是否不在同一会话——窗口站按会话隔离，跨会话看不见微信。

    Windows 的 ``EnumWindows``/UIA 只能看到**调用者自己会话**的窗口站。引擎由
    S4U 计划任务（watchdog/开机自愈）拉起时跑在 session 0，用户的微信在 session 1：
    枚举恒返回空，环境检测于是报「微信未运行」——2026-09-20 实测同一段枚举代码在
    session 1 里找到 14 个微信窗口、在 session 0 里找到 0 个。这不是微信的问题，
    报错必须说清楚，否则运维会一直去查微信而不是查会话。

    ``isolated=True`` 仅在「有交互会话 ∧ 引擎不在其中」时成立；无人登录（console=-1）
    不算隔离——那时本就没有可驱动的桌面，是另一种故障。绝不抛。
    """
    eng, con = current_session_id(), console_session_id()
    return {"engine_session": eng, "console_session": con,
            "isolated": bool(eng >= 0 and con >= 0 and eng != con)}


def check_environment() -> Dict[str, Any]:
    """真机环境快照；非 Windows 或任何异常都给出结构完整的「否」答案，绝不抛。"""
    out: Dict[str, Any] = {"os_windows": os.name == "nt", "running": False, "main_window": False,
                           "login_window": False, "version": "", "version_ok": False, "exe_path": "", "pid": 0,
                           "session": {}, "reason": ""}
    if os.name != "nt":
        return out
    out["session"] = session_isolation()
    try:
        from src.integrations.wechat_pc.win32_windows import find_wechat_windows
        wins = find_wechat_windows(visible_only=False)
    except Exception:
        wins = []
    if not wins:
        # 跨会话时「找不到窗口」说明不了微信在不在——别把它说成「微信未运行」
        if out["session"].get("isolated"):
            out["reason"] = "engine_not_interactive"
        return out
    out["running"] = True
    pid = 0
    for w in wins:
        kind = window_kind(w)
        if kind and w.visible:
            pid = pid or int(w.pid)
            if kind == "main":
                out["main_window"] = True
            else:
                out["login_window"] = True
    if not pid:
        pid = int(wins[0].pid)
    out["pid"] = pid
    try:
        path = _exe_path_of(pid)
        out["exe_path"] = path
        out["version"] = _file_version(path)
        out["version_ok"] = version_ok(out["version"])
    except Exception:
        pass
    # 主窗与登录窗都不可见但进程在：多半是最小化到托盘或正在启动
    return out


# ── 一键接入用（2026-09-19 P1）：找安装路径 / 拉起微信 / 把窗口置前 / 驱动依赖是否齐 ───────────

#: 注册表候选（按优先级：4.x Weixin 优先于 3.x WeChat；真机实测 HKCU\Software\Tencent\Weixin\InstallPath）
_REG_CANDIDATES = (
    (r"Software\Tencent\Weixin", "InstallPath", "Weixin.exe", "HKCU"),
    (r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Weixin", "DisplayIcon", "", "HKLM"),
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Weixin", "DisplayIcon", "", "HKLM"),
    (r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Weixin", "DisplayIcon", "", "HKCU"),
    (r"Software\Tencent\WeChat", "InstallPath", "WeChat.exe", "HKCU"),
    (r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\WeChat", "DisplayIcon", "", "HKLM"),
)


def _default_exe_candidates() -> Tuple[str, ...]:
    roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
             os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
             os.environ.get("LOCALAPPDATA", "")]
    out = []
    for r in roots:
        if not r:
            continue
        out.append(os.path.join(r, "Tencent", "Weixin", "Weixin.exe"))
        out.append(os.path.join(r, "Tencent", "WeChat", "WeChat.exe"))
    return tuple(out)


def resolve_exe_from_registry_value(value: Any, exe_name: str) -> str:
    """注册表值 → 可执行文件路径（纯函数）：``DisplayIcon`` 可能带引号/``,0`` 后缀；``InstallPath`` 是目录。"""
    s = str(value or "").strip().strip('"').strip()
    if not s:
        return ""
    if s.lower().endswith(",0"):
        s = s[:-2].strip().strip('"')
    if exe_name and not s.lower().endswith(".exe"):
        # 注册表路径是 Windows 路径。os.path.join 在 Linux 不认反斜杠，
        # `...\Weixin\` + Weixin.exe 会变成 `Weixin\/Weixin.exe`（双分隔符）。
        s = s.rstrip("\\/") + "\\" + exe_name
    return s


def find_wechat_exe() -> str:  # pragma: no cover - 平台相关
    """已安装的电脑微信可执行文件（4.x 优先）；未运行时也能找到。找不到返回空串。"""
    if os.name != "nt":
        return ""
    try:
        import winreg
        hives = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}
        for sub, name, exe_name, hive in _REG_CANDIDATES:
            try:
                with winreg.OpenKey(hives[hive], sub) as k:
                    val, _ = winreg.QueryValueEx(k, name)
                p = resolve_exe_from_registry_value(val, exe_name)
                if p and os.path.exists(p):
                    return p
            except OSError:
                continue
    except Exception:
        pass
    for p in _default_exe_candidates():
        if os.path.exists(p):
            return p
    return ""


def driver_ready() -> Dict[str, Any]:
    """副驾驱动在**后端这台机器**能否跑：Windows + ``uiautomation`` 包 + 同一桌面会话。

    会话也是硬前置：驱动进程继承引擎的会话，跨会话的 UIA 连窗口都枚举不到，
    起来也只能是个瞎子（``copilot/start`` 据此 409，而不是拉一个永远 blind 的驱动）。
    """
    import importlib.util
    import sys
    has_uia = importlib.util.find_spec("uiautomation") is not None
    sess = session_isolation()
    out = {"os_windows": os.name == "nt", "uiautomation": has_uia,
           "python": sys.executable, "session": sess,
           "ok": bool(os.name == "nt" and has_uia and not sess.get("isolated"))}
    if sess.get("isolated"):
        out["reason"] = "engine_not_interactive"
    return out


def voice_environment(*, selftest: bool = False, version: str = "") -> Dict[str, Any]:
    """语音通路环境（引导页「发语音」步骤 / 排障用）：微信版本够不够、音频库在不在、虚拟声卡装没装、
    两端采样率齐不齐、默认麦克风是谁、（可选）回环自测。``ready`` = 版本够 ∧ 声卡就绪。绝不抛。

    驱动运行态的权威在心跳 ``voice_ready``（还叠加了「发语音」锚点实际在不在）；这里是装机前/排障时的静态检查。
    """
    out: Dict[str, Any] = {"os_windows": os.name == "nt", "version": str(version or ""),
                           "voice_version_ok": False, "audio_libs": False, "cable": {}, "ready": False,
                           "min_version": ".".join(str(x) for x in VOICE_MIN_VERSION)}
    if os.name != "nt":
        return out
    try:
        if not out["version"]:
            out["version"] = str(check_environment().get("version") or "")
        out["voice_version_ok"] = voice_version_ok(out["version"])
    except Exception:
        pass
    try:
        from src.integrations.wechat_pc import audio_cable
        out["audio_libs"] = audio_cable.available()
        if out["audio_libs"]:
            out["cable"] = audio_cable.cable_status(selftest=selftest)
    except Exception:
        out["cable"] = {"error": "probe_failed"}
    out["ready"] = bool(out["voice_version_ok"] and (out["cable"] or {}).get("ready"))
    return out


def launch_wechat(exe_path: str = "") -> Dict[str, Any]:  # pragma: no cover - 平台相关
    """拉起电脑微信（不登录、不碰协议，只是 ``os.startfile``）。已运行则置前。"""
    env = check_environment()
    if env["running"]:
        return {"ok": True, "action": "focus", **bring_wechat_to_front()}
    exe = exe_path or find_wechat_exe()
    if not exe:
        return {"ok": False, "action": "launch", "reason": "not_installed"}
    try:
        os.startfile(exe)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "action": "launch", "reason": f"start_failed: {exc}", "exe_path": exe}
    return {"ok": True, "action": "launch", "exe_path": exe}


def bring_wechat_to_front() -> Dict[str, Any]:  # pragma: no cover - 平台相关
    """把微信登录窗/主窗还原并置前（托盘态 = 主窗被 SW_HIDE，也能 ShowWindow 拉回）。

    返回 ``{ok, window, visible, foreground}``：``window`` 为 ``main`` / ``login``；``ok`` 以 ``IsWindowVisible`` 事后核实为准，
    不以 API 返回值自信；``foreground`` 是 ``GetForegroundWindow`` 事后核实（置前失败但窗口已可见仍算 ok——
    驱动只要窗口可见就能读屏，抢焦点只是锦上添花）。优先级：可见登录窗（等扫码）> 可见主窗 > 隐藏主窗（托盘）> 隐藏登录窗。
    置前两段式：先 Alt 键放行，仍不在前台再 AttachThreadInput 兜底。

    **托盘态主窗（隐藏）优先走托盘图标单击**（2026-09-19 真机实锤，4.1.13.65）：``ShowWindow`` 能把窗口画回来，
    但 mmui 的无障碍树不重建（只剩空的 ``MMUIRenderSubWindowHW``），锚点自检永远缺 session_list → 副驾锁只读；
    只有微信自己的托盘单击路径会重建。找不到托盘图标 / 点了没出来再回落 ShowWindow。返回多带 ``via``：
    ``tray`` / ``show_window``。
    """
    if os.name != "nt":
        return {"ok": False, "window": "", "visible": False, "foreground": False}
    try:
        import ctypes
        from src.integrations.wechat_pc.win32_windows import find_wechat_windows
        wins = find_wechat_windows(visible_only=False)
        ranked = []
        for w in wins:
            kind = window_kind(w)
            if not kind:
                continue
            rank = {("login", True): 0, ("main", True): 1, ("main", False): 2, ("login", False): 3}[(kind, bool(w.visible))]
            ranked.append((rank, kind, w))
        if not ranked:
            return {"ok": False, "window": "", "visible": False, "foreground": False}
        ranked.sort(key=lambda x: x[0])
        _, kind, target = ranked[0]
        user32 = ctypes.windll.user32
        via = "show_window"
        if kind == "main" and not target.visible:
            try:
                from src.integrations.wechat_pc.win32_tray import click_tray_icon
                if click_tray_icon(pids=(int(target.pid),)):
                    for _ in range(12):
                        time.sleep(0.25)
                        if user32.IsWindowVisible(target.hwnd):
                            via = "tray"
                            break
            except Exception:
                via = "show_window"
        SW_RESTORE = 9
        if via != "tray":
            user32.ShowWindow(target.hwnd, SW_RESTORE)
        # 前台窗口不是本进程时 SetForegroundWindow 会被系统拒绝；先模拟按一下 Alt 是公认的放行法
        try:
            VK_MENU, KEYEVENTF_KEYUP = 0x12, 0x0002
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        except Exception:
            pass
        user32.SetForegroundWindow(target.hwnd)
        user32.BringWindowToTop(target.hwnd)
        foreground = int(user32.GetForegroundWindow() or 0) == int(target.hwnd)
        if not foreground:
            # 兜底：把本线程的输入队列挂到当前前台窗口所属线程上，系统就把我们视作"前台进程"放行
            # （AttachThreadInput 是 SetForegroundWindow 限制的第二条公认绕行路；Alt 法在锁屏/UAC 后偶失效）
            try:
                kernel32 = ctypes.windll.kernel32
                fg = user32.GetForegroundWindow()
                fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
                cur_tid = kernel32.GetCurrentThreadId()
                if fg_tid and fg_tid != cur_tid and user32.AttachThreadInput(cur_tid, fg_tid, True):
                    try:
                        user32.SetForegroundWindow(target.hwnd)
                        user32.BringWindowToTop(target.hwnd)
                        user32.SetFocus(target.hwnd)
                    finally:
                        user32.AttachThreadInput(cur_tid, fg_tid, False)
                foreground = int(user32.GetForegroundWindow() or 0) == int(target.hwnd)
            except Exception:
                pass
        visible = bool(user32.IsWindowVisible(target.hwnd))
        return {"ok": visible, "window": kind, "visible": visible, "foreground": foreground, "via": via}
    except Exception:
        return {"ok": False, "window": "", "visible": False, "foreground": False}


def accessibility_tree_empty(hwnd: int) -> Optional[bool]:  # pragma: no cover - 平台相关
    """主窗 UIA 树是否是「ShowWindow 拉回后的空壳」：只有渲染子窗、没有 Qt ``QWidget`` 子树。

    True＝空壳（锚点必缺）；False＝有内容；None＝判不了（UIA 不可用/异常）。判据来自真机：
    正常态主窗子节点 ``[MMUIRenderSubWindowHW, QWidget]``，空壳态只有 ``[MMUIRenderSubWindowHW]``。
    """
    if os.name != "nt" or not hwnd:
        return None
    try:
        import uiautomation as auto  # type: ignore
        win = auto.ControlFromHandle(int(hwnd))
        kids = win.GetChildren()
        if not kids:
            return True
        return not any((k.ClassName or "") != "MMUIRenderSubWindowHW" for k in kids)
    except Exception:
        return None


def rebuild_accessibility_via_tray(hwnd: int) -> Dict[str, Any]:  # pragma: no cover - 平台相关
    """空壳主窗自愈：``SW_HIDE`` 收回托盘 → 点托盘图标让微信自己把窗口显示出来（重建无障碍树）。

    只在 :func:`accessibility_tree_empty` 为 True 时调用；托盘图标找不到就什么都不做（窗口保持可见，
    不能把用户面前的窗口收走却拉不回来）。返回 ``{ok, tree_rebuilt, via}``。
    """
    if os.name != "nt" or not hwnd:
        return {"ok": False, "tree_rebuilt": False, "via": ""}
    try:
        import ctypes
        from src.integrations.wechat_pc.win32_tray import click_tray_icon, list_tray_icons, match_icon
        user32 = ctypes.windll.user32
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
        if match_icon(list_tray_icons(), pids=(int(pid.value),)) is None:
            return {"ok": False, "tree_rebuilt": False, "via": "no_tray_icon"}
        user32.ShowWindow(int(hwnd), 0)   # SW_HIDE：与点 X 收进托盘同款
        time.sleep(0.8)
        clicked = click_tray_icon(pids=(int(pid.value),))
        shown = False
        for _ in range(16):
            time.sleep(0.25)
            if user32.IsWindowVisible(int(hwnd)):
                shown = True
                break
        if not shown:
            user32.ShowWindow(int(hwnd), 9)   # 兜底把窗口画回来（树可能仍空，但不能让用户丢窗口）
        time.sleep(0.8)
        empty = accessibility_tree_empty(int(hwnd))
        return {"ok": bool(clicked and shown), "tree_rebuilt": empty is False, "via": "tray" if clicked else "show_window"}
    except Exception:
        return {"ok": False, "tree_rebuilt": False, "via": "error"}


__all__ = ["MIN_MAJOR", "VOICE_MIN_VERSION", "parse_version", "version_ok", "voice_version_ok", "classify_window",
           "window_kind", "check_environment", "find_wechat_exe", "driver_ready", "voice_environment", "launch_wechat",
           "bring_wechat_to_front", "resolve_exe_from_registry_value", "accessibility_tree_empty",
           "rebuild_accessibility_via_tray", "current_session_id", "console_session_id",
           "session_isolation"]
