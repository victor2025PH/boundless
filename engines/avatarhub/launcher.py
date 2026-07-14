# -*- coding: utf-8 -*-
"""
launcher.py — 桌面启动器（tkinter，零新依赖）

一键管理实时数字人对话系统：启动/停止/重启服务、实时就绪状态、打开控制台、一键体检。
设计：GUI 仅为薄壳，进程管理全部复用 service_manager.py 的成熟逻辑（健康检查 / 端口清理 /
孤儿进程回收 / 失败熔断），避免重复造轮子；网络与启停均在后台线程执行，UI 始终不卡。

运行：launcher.bat（推荐，自动选对解释器），或 <任意带 tkinter 的 python> launcher.py
"""
import sys, threading, webbrowser, subprocess, queue
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox

import app_config
import service_manager as sm

POLL_MS = 2000  # 状态自动刷新间隔

# 状态点颜色
COLOR_OK = "#23c552"      # 健康
COLOR_PARTIAL = "#e2b007" # 端口开但未健康（加载中）
COLOR_DOWN = "#888888"    # 未运行
COLOR_BG = "#1e1e1e"
COLOR_FG = "#e8e8e8"
COLOR_PANEL = "#2a2a2a"


class LauncherApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.busy = False
        self._polling = False
        # 线程安全：后台线程只把「要在主线程执行的回调」塞进队列，
        # 由主线程 pump 取出执行 —— 所有 Tk 调用都留在主线程，杜绝跨线程崩溃。
        self.ui_q: "queue.Queue" = queue.Queue()
        self._build_ui()
        self.root.after(80, self._pump)
        self.root.after(200, self._tick)

    def _post(self, fn):
        """供后台线程调用：把 UI 更新排队到主线程。"""
        self.ui_q.put(fn)

    def _pump(self):
        try:
            while True:
                fn = self.ui_q.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.root.after(80, self._pump)

    # ── UI 构建 ────────────────────────────────────────────────
    def _build_ui(self):
        self.root.title("数字人实时对话 — 控制台")
        self.root.configure(bg=COLOR_BG)
        self.root.geometry("760x560")
        self.root.minsize(680, 480)

        # 顶部标题
        top = tk.Frame(self.root, bg=COLOR_BG)
        top.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(top, text="数字人实时对话系统", bg=COLOR_BG, fg=COLOR_FG,
                 font=("Microsoft YaHei UI", 16, "bold")).pack(side="left")
        self.summary = tk.Label(top, text="检测中…", bg=COLOR_BG, fg="#9aa0a6",
                                font=("Microsoft YaHei UI", 11))
        self.summary.pack(side="right")

        # 服务表
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Treeview", background=COLOR_PANEL, fieldbackground=COLOR_PANEL,
                        foreground=COLOR_FG, rowheight=30, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Treeview", background=[("selected", "#3a5a8c")])

        mid = tk.Frame(self.root, bg=COLOR_BG)
        mid.pack(fill="both", expand=True, padx=16, pady=6)

        cols = ("status", "name", "label", "port", "kind")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="browse")
        for c, t, w, anchor in (
            ("status", "状态", 70, "center"),
            ("name", "服务", 110, "w"),
            ("label", "说明", 250, "w"),
            ("port", "端口", 70, "center"),
            ("kind", "类型", 70, "center"),
        ):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=anchor)
        self.tree.tag_configure("ok", foreground=COLOR_OK)
        self.tree.tag_configure("partial", foreground=COLOR_PARTIAL)
        self.tree.tag_configure("down", foreground=COLOR_DOWN)
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)

        for name, s in app_config.SERVICES.items():
            kind = "必需" if s.get("core") else "可选"
            self.tree.insert("", "end", iid=name,
                             values=("●", name, s.get("label", name), s["port"], kind),
                             tags=("down",))

        # 按钮区
        btns = tk.Frame(self.root, bg=COLOR_BG)
        btns.pack(fill="x", padx=16, pady=(4, 8))
        self.btn_core = self._mkbtn(btns, "启动核心链路", self.on_start_core, "#2e7d32")
        self.btn_all = self._mkbtn(btns, "启动全部", self.on_start_all, "#33691e")
        self.btn_restart = self._mkbtn(btns, "重启选中", self.on_restart_sel, "#1565c0")
        self.btn_stop = self._mkbtn(btns, "停止全部", self.on_stop_all, "#b71c1c")
        self._mkbtn(btns, "打开控制台", self.on_open_ui, "#37474f")
        self._mkbtn(btns, "环境体检", self.on_provision, "#37474f")
        self._mkbtn(btns, "一键体检", self.on_doctor, "#4527a0")

        # 日志区
        logf = tk.Frame(self.root, bg=COLOR_BG)
        logf.pack(fill="both", padx=16, pady=(0, 12))
        self.log = tk.Text(logf, height=7, bg="#141414", fg="#c8c8c8",
                           font=("Consolas", 9), relief="flat", wrap="word")
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled")
        self._log("就绪。点击「启动核心链路」开始实时对话最小集（克隆音/识别/口型/广播/中枢）。")

    def _mkbtn(self, parent, text, cmd, color):
        b = tk.Button(parent, text=text, command=cmd, bg=color, fg="white",
                      activebackground=color, activeforeground="white",
                      relief="flat", font=("Microsoft YaHei UI", 10, "bold"),
                      padx=12, pady=7, cursor="hand2", borderwidth=0)
        b.pack(side="left", padx=(0, 8))
        return b

    def _log(self, msg: str):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_busy(self, busy: bool, note: str = ""):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.btn_core, self.btn_all, self.btn_restart, self.btn_stop):
            b.configure(state=state)
        if note:
            self._log(note)

    # ── 状态轮询 ───────────────────────────────────────────────
    def _tick(self):
        # 主线程定时触发：仅当上一轮探测已结束才发起新一轮，避免堆积。
        if not self._polling:
            self._polling = True
            threading.Thread(target=self._poll_worker, daemon=True).start()
        self.root.after(POLL_MS, self._tick)

    def _poll_worker(self):
        try:
            status = sm.get_status()
            self._post(lambda: self._apply_status(status))
        except Exception as e:
            self._post(lambda: self.summary.configure(text=f"探测异常: {e}"))
        finally:
            self._polling = False

    def _apply_status(self, status: dict):
        core_total = core_ready = 0
        for name, info in status.items():
            svc = app_config.SERVICES.get(name, {})
            if svc.get("core"):
                core_total += 1
            if info["healthy"]:
                tag, dot = "ok", "● 就绪"
                if svc.get("core"):
                    core_ready += 1
            elif info["running"]:
                tag, dot = "partial", "● 加载中"
            else:
                tag, dot = "down", "● 停止"
            vals = list(self.tree.item(name, "values"))
            vals[0] = dot
            self.tree.item(name, values=vals, tags=(tag,))
        self.summary.configure(text=f"核心链路就绪 {core_ready}/{core_total}")

    # ── 操作（均后台线程） ─────────────────────────────────────
    def _run_bg(self, fn, start_note):
        if self.busy:
            return
        self._set_busy(True, start_note)

        def worker():
            try:
                fn()
            except Exception as e:
                self._post(lambda: self._log(f"出错: {e}"))
            finally:
                self._post(lambda: self._set_busy(False, "操作完成。"))

        threading.Thread(target=worker, daemon=True).start()

    def on_start_core(self):
        self._run_bg(lambda: sm.start_all(required_only=True),
                     "正在启动核心链路（首次加载模型可能需 1–2 分钟）…")

    def on_start_all(self):
        self._run_bg(lambda: sm.start_all(required_only=False),
                     "正在启动全部服务（含扩展，显存占用较高）…")

    def on_stop_all(self):
        if not messagebox.askyesno("确认", "停止全部服务？正在进行的直播会中断。"):
            return
        self._run_bg(sm.stop_all, "正在停止全部服务…")

    def on_restart_sel(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在列表中选择一个服务。")
            return
        name = sel[0]
        svc = next((s for s in sm.SERVICES if s["name"] == name), None)
        if not svc:
            return

        def do():
            sm.stop_service(name)
            sm.start_service(svc)

        self._run_bg(do, f"正在重启 {name} …")

    def on_open_ui(self):
        url = app_config.svc_url("hub") + "/ui"
        webbrowser.open(url)
        self._log(f"已在浏览器打开 {url}")

    def _run_in_console(self, script_name, note):
        py = app_config.conda_python("facefusion")
        script = str(app_config.BASE / script_name)
        try:
            subprocess.Popen(["cmd", "/k", py, script],
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                             cwd=str(app_config.BASE))
            self._log(note)
        except Exception as e:
            self._log(f"启动失败: {e}")

    def on_doctor(self):
        self._run_in_console("doctor.py", "已在新窗口运行一键体检 doctor.py。")

    def on_provision(self):
        self._run_in_console("provision.py", "已在新窗口运行环境体检 provision.py。")


def main():
    root = tk.Tk()
    LauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
