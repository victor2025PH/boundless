# -*- coding: utf-8 -*-
"""列出 avatar_hub / uvicorn 相关进程（pid + 启动时刻 + 命令行摘要）。"""
import subprocess

ps = ("Get-CimInstance Win32_Process | "
      "Where-Object { $_.CommandLine -match 'avatar_hub|uvicorn' } | "
      "ForEach-Object { '{0}|{1}|{2}' -f $_.ProcessId, $_.CreationDate, "
      "($_.CommandLine -replace '\\s+', ' ') }")
out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                     capture_output=True, text=True, timeout=30).stdout
for ln in out.splitlines():
    if ln.strip():
        print(ln[:200])
