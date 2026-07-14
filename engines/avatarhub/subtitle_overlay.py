# -*- coding: utf-8 -*-
"""
对方语音翻译 · 桌面置顶悬浮字幕窗
─────────────────────────────────────────────────────────────────────────
用途：视频通话(Telegram/微信/Zoom 等)时，把同传服务(live_interpreter:7900)
      「方向B = 对方说话→译中」的字幕浮在所有窗口最上层，盯着通话画面就能看翻译。
      —— 字幕原本只烧进 OBS 虚拟摄像头 或 显示在 7900 网页里，无法浮在通话窗上。

数据源：消费现成端点，零侵入不改同传核心：
  SSE  /events ：对方 {"who":"other","turn":N,"uid":U,"en":英文,"zh":中文译文}
                润色 {"finalize":true,"turn":N,"who":"other","zh":整轮润色稿}
                我方 {"who":"me",...,"en":对方听到的英文}
  GET  /status ：{"running":bool,"cap_b_err":对方声采集失败原因}（驱动失败横幅）

依赖：仅标准库 tkinter + requests（facefusion 环境已具备）。无需 PySide6。

交互：
  拖动            移动窗口（位置自动记忆）
  Ctrl+滚轮 / +/- 调字号（自动记忆）
  双击            切换是否显示「我方回显」
  F8              切换鼠标点击穿透（穿透时不挡 Telegram 操作；再按 F8 关闭）
  Esc / 右键      关闭

启动：python subtitle_overlay.py
      python subtitle_overlay.py --url http://127.0.0.1:7900
      python subtitle_overlay.py --show-me
"""
import os
import sys
import json
import time
import argparse
import threading
import queue

try:
    import requests
except Exception:
    print("缺少 requests（请在 facefusion 环境运行：python subtitle_overlay.py）")
    sys.exit(1)

try:
    import tkinter as tk
    from tkinter import font as tkfont
except Exception as e:
    print(f"无法加载 tkinter（该 Python 缺少 Tk 支持）: {e}")
    sys.exit(1)

IS_WIN = sys.platform.startswith("win")
SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "subtitle_overlay_settings.json")


# ── 事件聚合：把逐子句事件按「轮(turn)」拼成整句，与网页 addRow 逻辑同源 ──────────
class TurnStore:
    """线程安全地维护每一轮的子句拼接 + 润色替换，并记录最新一轮的 turn id。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.turns = {}            # tid -> {"order":[uid], "seg":{uid:{"en","zh"}}, "fin_en","fin_zh"}
        self.latest = {"other": None, "me": None}

    def apply(self, ev: dict):
        who = ev.get("who")
        tid = ev.get("turn")
        with self._lock:
            if ev.get("finalize") and tid is not None:
                t = self.turns.get(tid)
                if t is not None:
                    if ev.get("en") is not None:
                        t["fin_en"] = ev["en"]
                    if ev.get("zh") is not None:
                        t["fin_zh"] = ev["zh"]
                if who in self.latest:
                    self.latest[who] = tid
                return

            uid = ev.get("uid")
            if tid is None or uid is None or who not in ("other", "me"):
                return
            t = self.turns.get(tid)
            if t is None:
                t = {"order": [], "seg": {}, "fin_en": None, "fin_zh": None, "who": who}
                self.turns[tid] = t
                if len(self.turns) > 40:
                    for old in sorted(self.turns)[:-40]:
                        self.turns.pop(old, None)
            if uid not in t["seg"]:
                t["order"].append(uid)
                t["seg"][uid] = {"en": "", "zh": ""}
            if ev.get("en"):
                t["seg"][uid]["en"] = ev["en"]
            if ev.get("zh"):
                t["seg"][uid]["zh"] = ev["zh"]
            self.latest[who] = tid

    def _render(self, tid):
        t = self.turns.get(tid)
        if not t:
            return "", ""
        if t["fin_zh"] is not None or t["fin_en"] is not None:
            return (t["fin_zh"] or ""), (t["fin_en"] or "")
        zh = " ".join(t["seg"][u]["zh"] for u in t["order"] if t["seg"][u]["zh"]).strip()
        en = " ".join(t["seg"][u]["en"] for u in t["order"] if t["seg"][u]["en"]).strip()
        return zh, en

    def latest_text(self, who):
        with self._lock:
            tid = self.latest.get(who)
            if tid is None:
                return "", ""
            return self._render(tid)


# ── SSE 客户端：后台线程长连 /events，断线自动重连 ─────────────────────────────
class EventClient(threading.Thread):
    def __init__(self, base_url: str, store: TurnStore, status_q: queue.Queue):
        super().__init__(daemon=True)
        self.url = base_url.rstrip("/") + "/events"
        self.store = store
        self.status_q = status_q
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                with requests.get(self.url, stream=True, timeout=(5, 65)) as r:
                    r.raise_for_status()
                    self.status_q.put(("conn", True))
                    backoff = 1.0
                    for raw in r.iter_lines(decode_unicode=True):
                        if self._stop.is_set():
                            break
                        if not raw or not raw.startswith("data:"):
                            continue
                        payload = raw[5:].strip()
                        if not payload:
                            continue
                        try:
                            ev = json.loads(payload)
                        except Exception:
                            continue
                        self.store.apply(ev)
                        self.status_q.put(("event", None))
            except Exception:
                self.status_q.put(("conn", False))
            if self._stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 1.6, 8.0)


# ── 会话状态轮询：拿 running / 对方声采集失败(cap_b_err) 驱动横幅 ─────────────────
class StatusClient(threading.Thread):
    def __init__(self, base_url: str, status_q: queue.Queue):
        super().__init__(daemon=True)
        self.url = base_url.rstrip("/") + "/status"
        self.status_q = status_q
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                j = requests.get(self.url, timeout=4).json()
                self.status_q.put(("status", {"running": bool(j.get("running")),
                                              "cap_b_err": j.get("cap_b_err")}))
            except Exception:
                self.status_q.put(("status", None))
            for _ in range(15):                      # ~1.5s，可快速响应停止
                if self._stop.is_set():
                    return
                time.sleep(0.1)


# ── Windows 点击穿透 / 不抢焦点（全部 try 包裹，失败则静默降级） ──────────────────
class WinFx:
    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_NOACTIVATE = 0x08000000
    WS_EX_TOOLWINDOW = 0x00000080

    def __init__(self, root):
        self.ok = False
        self.hwnd = None
        if not IS_WIN:
            return
        try:
            import ctypes
            self.ctypes = ctypes
            self.u32 = ctypes.windll.user32
            hwnd = root.winfo_id()
            parent = self.u32.GetParent(hwnd)
            self.hwnd = parent if parent else hwnd
            self._get = getattr(self.u32, "GetWindowLongPtrW", None) or self.u32.GetWindowLongW
            self._set = getattr(self.u32, "SetWindowLongPtrW", None) or self.u32.SetWindowLongW
            self.ok = True
        except Exception:
            self.ok = False

    def _ex(self):
        return int(self._get(self.hwnd, self.GWL_EXSTYLE))

    def no_activate(self):
        """让窗口出现/刷新时不抢走 Telegram 的输入焦点（纯收益，始终开启）。"""
        if not self.ok:
            return
        try:
            ex = self._ex() | self.WS_EX_NOACTIVATE | self.WS_EX_TOOLWINDOW
            self._set(self.hwnd, self.GWL_EXSTYLE, ex)
        except Exception:
            pass

    def set_click_through(self, on: bool):
        if not self.ok:
            return False
        try:
            ex = self._ex() | self.WS_EX_LAYERED
            if on:
                ex |= self.WS_EX_TRANSPARENT
            else:
                ex &= ~self.WS_EX_TRANSPARENT
            self._set(self.hwnd, self.GWL_EXSTYLE, ex)
            return True
        except Exception:
            return False


class HotkeyThread(threading.Thread):
    """全局热键 F8：穿透模式下窗口收不到键盘事件，用系统级热键才能切回。"""
    WM_HOTKEY = 0x0312
    VK_F8 = 0x77

    def __init__(self, on_toggle):
        super().__init__(daemon=True)
        self.on_toggle = on_toggle
        self._tid = None
        self.ok = IS_WIN

    def run(self):
        if not IS_WIN:
            return
        try:
            import ctypes
            from ctypes import wintypes
            u32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            self._tid = kernel32.GetCurrentThreadId()
            if not u32.RegisterHotKey(None, 1, 0, self.VK_F8):
                return
            msg = wintypes.MSG()
            while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == self.WM_HOTKEY:
                    try:
                        self.on_toggle()
                    except Exception:
                        pass
            u32.UnregisterHotKey(None, 1)
        except Exception:
            pass

    def stop(self):
        if IS_WIN and self._tid:
            try:
                import ctypes
                ctypes.windll.user32.PostThreadMessageW(self._tid, 0x0012, 0, 0)  # WM_QUIT
            except Exception:
                pass


def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(d):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ── 悬浮窗 UI ───────────────────────────────────────────────────────────────
class OverlayApp:
    BG = "#0b0e14"
    OTHER_ZH = "#ffffff"
    OTHER_EN = "#8b96b0"
    ME_FG = "#9fb4ff"
    DIM = "#5b6b8c"
    DANGER = "#fca5a5"
    BASE_ALPHA = 0.92
    FADE_ALPHA = 0.40
    FADE_AFTER = 9.0           # 对方静默多少秒后字幕淡出
    WIDTH = 760

    def __init__(self, base_url: str, show_me: bool):
        self.base_url = base_url
        self.store = TurnStore()
        self.status_q = queue.Queue()
        self.connected = False
        self.running = None
        self.cap_b_err = None
        self._last_render = None
        self._last_other_change = 0.0
        self._faded = False

        st = load_settings()
        self.show_me = bool(st.get("show_me", show_me))
        self.font_scale = float(st.get("font_scale", 1.0))
        self.font_scale = min(2.4, max(0.7, self.font_scale))
        self.click_through = False                  # 启动恒为关，避免一开就锁住
        self._want_ct = bool(st.get("click_through", False))
        self._saved_geo = st.get("geometry")

        self.root = tk.Tk()
        self.root.title("对方翻译字幕")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", self.BASE_ALPHA)
        except Exception:
            pass
        self.root.configure(bg=self.BG)

        fam = self._pick_font()
        self._fam = fam
        self.f_title = tkfont.Font(family=fam, size=9)
        self.f_zh = tkfont.Font(family=fam, size=self._sz(20), weight="bold")
        self.f_en = tkfont.Font(family=fam, size=self._sz(12))
        self.f_me = tkfont.Font(family=fam, size=self._sz(11))

        pad = {"padx": 16}
        bar = tk.Frame(self.root, bg=self.BG)
        bar.pack(fill="x", pady=(8, 0), **pad)
        self.lbl_title = tk.Label(bar, text="● 对方翻译", font=self.f_title,
                                  fg=self.DIM, bg=self.BG)
        self.lbl_title.pack(side="left")
        self.lbl_hint = tk.Label(bar, text="拖动移动 · Ctrl滚轮缩放 · 双击回显 · F8穿透 · Esc关闭",
                                 font=self.f_title, fg=self.DIM, bg=self.BG)
        self.lbl_hint.pack(side="left", padx=10)
        btn_close = tk.Label(bar, text="✕", font=self.f_title, fg=self.DIM, bg=self.BG,
                             cursor="hand2")
        btn_close.pack(side="right")
        btn_close.bind("<Button-1>", lambda e: self.quit())
        self.bar = bar

        # 失败/状态横幅（仅在异常时显示）
        self.lbl_banner = tk.Label(self.root, text="", font=self.f_title,
                                   fg=self.DANGER, bg=self.BG, justify="left",
                                   wraplength=self.WIDTH - 40, anchor="w")

        self.lbl_zh = tk.Label(self.root, text="等待对方说话…", font=self.f_zh,
                               fg=self.OTHER_ZH, bg=self.BG, justify="left",
                               wraplength=self.WIDTH - 40, anchor="w")
        self.lbl_zh.pack(fill="x", pady=(2, 0), **pad)
        self.lbl_en = tk.Label(self.root, text="", font=self.f_en,
                               fg=self.OTHER_EN, bg=self.BG, justify="left",
                               wraplength=self.WIDTH - 40, anchor="w")
        self.lbl_en.pack(fill="x", **pad)
        self.lbl_me = tk.Label(self.root, text="", font=self.f_me,
                               fg=self.ME_FG, bg=self.BG, justify="left",
                               wraplength=self.WIDTH - 40, anchor="w")
        if self.show_me:
            self.lbl_me.pack(fill="x", pady=(2, 8), **pad)

        for w in (self.root, bar, self.lbl_zh, self.lbl_en, self.lbl_hint):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", lambda e: self._persist())
            w.bind("<Button-3>", lambda e: self.quit())
            w.bind("<Double-Button-1>", lambda e: self._toggle_me())
        self.root.bind("<Escape>", lambda e: self.quit())
        self.root.bind("<F8>", lambda e: self.toggle_click_through())
        self.root.bind("<Control-MouseWheel>", self._on_wheel)
        self.root.bind("<plus>", lambda e: self._bump_font(1))
        self.root.bind("<minus>", lambda e: self._bump_font(-1))
        self.root.bind("<Control-plus>", lambda e: self._bump_font(1))
        self.root.bind("<Control-minus>", lambda e: self._bump_font(-1))

        self._place()

        # 不抢焦点（始终）
        self.winfx = WinFx(self.root)
        self.root.after(200, self.winfx.no_activate)

        # 全局热键（穿透切换）
        self.hotkey = HotkeyThread(self._hotkey_toggle)
        self.hotkey.start()
        self._ct_request = queue.Queue()            # 热键线程 → 主线程

        self.client = EventClient(base_url, self.store, self.status_q)
        self.client.start()
        self.statusc = StatusClient(base_url, self.status_q)
        self.statusc.start()

        if self._want_ct:
            self.root.after(800, self.toggle_click_through)

        self.root.after(120, self._tick)

    # ---- 字号 ----
    def _sz(self, base):
        return max(7, int(round(base * self.font_scale)))

    def _apply_fonts(self):
        self.f_zh.configure(size=self._sz(20))
        self.f_en.configure(size=self._sz(12))
        self.f_me.configure(size=self._sz(11))
        self.root.after_idle(self._refit_height)

    def _bump_font(self, step):
        self.font_scale = min(2.4, max(0.7, round(self.font_scale + 0.1 * step, 2)))
        self._apply_fonts()
        self._persist()

    def _on_wheel(self, e):
        self._bump_font(1 if e.delta > 0 else -1)

    # ---- 字体族 ----
    def _pick_font(self):
        fams = set(tkfont.families())
        for cand in ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
                     "Noto Sans CJK SC", "SimHei", "Segoe UI"):
            if cand in fams:
                return cand
        return "TkDefaultFont"

    # ---- 位置 ----
    def _place(self):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = self.WIDTH
        h = self.root.winfo_reqheight()
        if self._saved_geo and isinstance(self._saved_geo, dict):
            x = int(self._saved_geo.get("x", (sw - w) // 2))
            y = int(self._saved_geo.get("y", sh - h - 90))
            x = min(max(0, x), max(0, sw - 80))
            y = min(max(0, y), max(0, sh - 60))
        else:
            x = (sw - w) // 2
            y = sh - h - 90
        self.root.geometry(f"{w}x{h}+{x}+{max(20, y)}")

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _refit_height(self):
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_reqheight()
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _toggle_me(self):
        self.show_me = not self.show_me
        if self.show_me:
            self.lbl_me.pack(fill="x", pady=(2, 8), padx=16)
        else:
            self.lbl_me.pack_forget()
        self._persist()
        self.root.after_idle(self._refit_height)

    # ---- 点击穿透 ----
    def _hotkey_toggle(self):
        self._ct_request.put(True)                  # 跨线程，主循环里真正执行

    def toggle_click_through(self):
        on = not self.click_through
        if self.winfx.set_click_through(on):
            self.click_through = on
            self.lbl_title.config(
                text="◌ 穿透中(F8 退出)" if on else "● 对方翻译",
                fg=(self.DIM if on else "#34d399"))
            self._persist()

    # ---- 设置持久化 ----
    def _persist(self):
        save_settings({
            "geometry": {"x": self.root.winfo_x(), "y": self.root.winfo_y()},
            "font_scale": self.font_scale,
            "show_me": self.show_me,
            "click_through": self.click_through,
        })

    # ---- 主循环 ----
    def _tick(self):
        # 跨线程的穿透切换请求
        try:
            while True:
                self._ct_request.get_nowait()
                self.toggle_click_through()
        except queue.Empty:
            pass

        new_other = False
        try:
            while True:
                kind, val = self.status_q.get_nowait()
                if kind == "conn":
                    self.connected = bool(val)
                elif kind == "event":
                    pass
                elif kind == "status":
                    if val is None:
                        self.running = None
                    else:
                        self.running = val.get("running")
                        self.cap_b_err = val.get("cap_b_err")
        except queue.Empty:
            pass

        # 标题连接态（穿透中不覆盖穿透提示）
        if not self.click_through:
            if self.connected:
                self.lbl_title.config(text="● 对方翻译", fg="#34d399")
            else:
                self.lbl_title.config(text="○ 同传未连接(等待 7900)…", fg=self.DIM)

        # 失败/状态横幅
        banner = ""
        if self.connected and self.cap_b_err:
            banner = "⚠ 对方声采集失败：" + str(self.cap_b_err)[:80] + "  → 请在录音设备启用「立体声混音」或换来源"
        elif self.connected and self.running is False:
            banner = "○ 同传未开始 —— 在 7900 页面点「开始」，或用一键脚本(?go=1)自动开跑"
        self._set_banner(banner)

        # 渲染最新字幕
        zh, en = self.store.latest_text("other")
        me_zh, me_en = self.store.latest_text("me")
        cur = (zh, en, me_en if self.show_me else "")
        if cur != self._last_render:
            if self._last_render is not None and (zh, en) != self._last_render[:2]:
                self._last_other_change = time.time()
                self._unfade()
            elif self._last_render is None and (zh or en):
                self._last_other_change = time.time()
            self._last_render = cur
            self.lbl_zh.config(text=zh or ("等待对方说话…" if self.connected else "等待同传服务…"))
            self.lbl_en.config(text=en)
            if self.show_me:
                self.lbl_me.config(text=("你→对方: " + me_en) if me_en else "")
            self.root.after_idle(self._refit_height)

        # 静默淡出
        if (zh or en) and self._last_other_change and not self._faded:
            if time.time() - self._last_other_change > self.FADE_AFTER:
                self._fade()

        self.root.after(140, self._tick)

    def _set_banner(self, text):
        cur = self.lbl_banner.cget("text")
        if text == cur:
            return
        self.lbl_banner.config(text=text)
        if text:
            if not self.lbl_banner.winfo_ismapped():
                self.lbl_banner.pack(after=self.bar, fill="x", padx=16, pady=(4, 0))
        else:
            if self.lbl_banner.winfo_ismapped():
                self.lbl_banner.pack_forget()
        self.root.after_idle(self._refit_height)

    def _fade(self):
        self._faded = True
        try:
            self.root.attributes("-alpha", self.FADE_ALPHA)
        except Exception:
            pass

    def _unfade(self):
        self._faded = False
        try:
            self.root.attributes("-alpha", self.BASE_ALPHA)
        except Exception:
            pass

    def quit(self):
        try:
            self.client.stop()
            self.statusc.stop()
            self.hotkey.stop()
        except Exception:
            pass
        self._persist()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    ap = argparse.ArgumentParser(description="对方语音翻译 · 桌面置顶悬浮字幕窗")
    ap.add_argument("--url", default=os.environ.get("INTERP_URL", "http://127.0.0.1:7900"),
                    help="同传服务地址（默认 http://127.0.0.1:7900）")
    ap.add_argument("--show-me", action="store_true",
                    help="启动即显示我方回显（对方听到的克隆英文）")
    args = ap.parse_args()
    OverlayApp(args.url, args.show_me).run()


if __name__ == "__main__":
    main()
