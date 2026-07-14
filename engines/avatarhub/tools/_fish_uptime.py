# -*- coding: utf-8 -*-
"""fish_tts(7855) 进程启动时间与 /health 元数据：核对是否在 cos 漂移时间点(07-06 凌晨)前后重启。"""
import json
import subprocess
import urllib.request

try:
    with urllib.request.urlopen("http://127.0.0.1:7855/health", timeout=6) as r:
        print("health:", r.read().decode()[:300])
except Exception as e:
    print("health err:", e)

ps = subprocess.run(
    ["powershell", "-NoProfile", "-Command",
     "Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -like '*fish_speech_server*'} | "
     "Select-Object ProcessId,CreationDate,CommandLine | Format-List"],
    capture_output=True, text=True, timeout=30)
print(ps.stdout[:1500])
