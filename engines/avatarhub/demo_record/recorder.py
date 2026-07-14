# -*- coding: utf-8 -*-
"""Bandicam 命令行录屏控制。

前置(已由安装脚本写好注册表):
  - HKCU\Software\BANDISOFT\BANDICAM\OPTION 里 bTargetFullScreen=1 (主屏全屏),
    sOutputFolder=demo_record\raw, nRunAsAdmin=0 (否则非管理员 shell 拉起会被 UAC 卡死)。
注意: 未注册版有顶部水印(postprod.py 的 delogo 负责去除)和单段 10 分钟上限。
"""
import os
import subprocess
import time
import winreg

BDCAM = r"C:\Program Files\Bandicam\bdcam.exe"
RAW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raw")


def _reg(name):
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\BANDISOFT\BANDICAM\OPTION")
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None
    finally:
        key.Close()


class Bandicam:
    def __init__(self):
        os.makedirs(RAW_DIR, exist_ok=True)
        self._before = set()

    def ensure_running(self):
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq bdcam.exe"],
                             capture_output=True, text=True).stdout
        if "bdcam.exe" not in out:
            subprocess.Popen([BDCAM, "/nosplash"])
            time.sleep(6)

    def start(self):
        self.ensure_running()
        self._before = set(os.listdir(RAW_DIR))
        subprocess.run([BDCAM, "/record"], check=False)
        time.sleep(1.5)  # 编码器起帧

    def stop(self):
        subprocess.run([BDCAM, "/stop"], check=False)
        path = self._wait_output()
        return path

    def _wait_output(self, timeout=30):
        """等新 mp4 出现且大小稳定(收尾封装完)。"""
        deadline = time.time() + timeout
        candidate = None
        while time.time() < deadline:
            new = [f for f in os.listdir(RAW_DIR)
                   if f.lower().endswith(".mp4") and f not in self._before]
            if new:
                candidate = os.path.join(
                    RAW_DIR, max(new, key=lambda f: os.path.getmtime(os.path.join(RAW_DIR, f))))
                s1 = os.path.getsize(candidate)
                time.sleep(1.5)
                if os.path.getsize(candidate) == s1 and s1 > 0:
                    return candidate
            else:
                time.sleep(1)
        return candidate or _reg("sLatestRecordingFile")
