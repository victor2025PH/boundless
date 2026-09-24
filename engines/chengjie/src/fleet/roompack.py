"""机房安装包：一个很小的 zip（启动器 + room.key），不含通用安装包本体，也不把密钥写进公开下载。

密钥只出现在 zip 里的 room.key。启动脚本用文件路径把密钥交给安装器，不把明文放进命令行回显。
"""

from __future__ import annotations

import io
import zipfile
from typing import Optional


def _cmd(setup_url: str, controller: str) -> str:
    # ASCII only (cmd.exe + GBK consoles). The key stays in room.key.
    url = setup_url.replace("%", "%%").replace('"', "")
    ctl = controller.replace("%", "%%").replace('"', "")
    return f"""@echo off
setlocal
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
set "SETUP=%TEMP%\\ChatXAgentSetup.exe"
set "KEYFILE=%TEMP%\\chatx-fleet-room.key"
if not exist "%~dp0room.key" (
  echo room.key missing
  exit /b 1
)
copy /Y "%~dp0room.key" "%KEYFILE%" >nul
if not exist "%SETUP%" (
  echo downloading installer
  powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '{url}' -OutFile '%SETUP%' -UseBasicParsing"
  if errorlevel 1 exit /b 1
)
"%SETUP%" /CONTROLLER={ctl} /ROOMKEYFILE="%KEYFILE%"
set "RC=%ERRORLEVEL%"
del /f /q "%KEYFILE%" >nul 2>&1
exit /b %RC%
"""


def _silent_cmd(setup_url: str, controller: str) -> str:
    url = setup_url.replace("%", "%%").replace('"', "")
    ctl = controller.replace("%", "%%").replace('"', "")
    return f"""@echo off
setlocal
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
set "SETUP=%TEMP%\\ChatXAgentSetup.exe"
set "KEYFILE=%TEMP%\\chatx-fleet-room.key"
if not exist "%~dp0room.key" exit /b 1
copy /Y "%~dp0room.key" "%KEYFILE%" >nul
if not exist "%SETUP%" (
  powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '{url}' -OutFile '%SETUP%' -UseBasicParsing"
  if errorlevel 1 exit /b 1
)
"%SETUP%" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CONTROLLER={ctl} /ROOMKEYFILE="%KEYFILE%"
set "RC=%ERRORLEVEL%"
del /f /q "%KEYFILE%" >nul 2>&1
exit /b %RC%
"""


def _readme(group: str, label: str, expires_at: Optional[float], max_uses: int) -> str:
    when = ""
    if expires_at:
        when = f"expires_at_unix={int(expires_at)}\n"
    return (
        "ChatX fleet room installer\n"
        "==========================\n"
        f"group={group or '-'}\n"
        f"label={label or '-'}\n"
        f"max_uses={int(max_uses)}\n"
        f"{when}"
        "\n"
        "Double-click Install.cmd on each PC (UAC prompt, then the finish screen).\n"
        "For a silent mass deploy, run Install-Silent.cmd instead.\n"
        "Each successful install consumes one use. This zip is a secret: it is not\n"
        "the public download. Delete it when the room is full, or revoke the key\n"
        "in the console. Do not mail the zip or paste room.key into chat.\n"
    )


def build_room_pack(*, room_key: str, controller: str, setup_url: str, group: str = "",
                    label: str = "", expires_at: Optional[float] = None, max_uses: int = 1) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("room.key", room_key.strip() + "\n")
        z.writestr("Install.cmd", _cmd(setup_url, controller).replace("\n", "\r\n"))
        z.writestr("Install-Silent.cmd", _silent_cmd(setup_url, controller).replace("\n", "\r\n"))
        z.writestr("README.txt", _readme(group, label, expires_at, max_uses).replace("\n", "\r\n"))
    return buf.getvalue()
