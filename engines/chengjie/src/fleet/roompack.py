"""机房安装包：一个很小的 zip（启动器 + room.key），不含通用安装包本体，也不把密钥写进公开下载。

密钥只出现在 zip 里的 room.key。启动脚本用文件路径把密钥交给安装器，不把明文放进命令行回显。
安装包下载到随机临时路径，只接受 https，并用主控钉住的 setup_sha256 校验，不复用已经躺在 %TEMP% 里的 exe。
"""

from __future__ import annotations

import io
import zipfile
from typing import Optional


def _install_body(*, silent: bool, setup_url: str, controller: str, setup_sha256: str) -> str:
    url = setup_url.replace("%", "%%").replace('"', "").replace("\r", "").replace("\n", "")
    ctl = controller.replace("%", "%%").replace('"', "").replace("\r", "").replace("\n", "")
    sha = "".join(c for c in (setup_sha256 or "") if c in "0123456789abcdefABCDEF")
    flags = " /VERYSILENT /SUPPRESSMSGBOXES /NORESTART" if silent else ""
    missing = "exit /b 1" if silent else "echo room.key missing\n  exit /b 1"
    # ASCII only. %RANDOM% is expanded by cmd, so a planted %TEMP%\\ChatXAgentSetup.exe is never reused.
    return f"""@echo off
setlocal
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
set "URL={url}"
set "WANT={sha}"
set "SETUP=%TEMP%\\ChatXAgentSetup-%RANDOM%%RANDOM%.exe"
set "KEYFILE=%TEMP%\\chatx-fleet-room-%RANDOM%.key"
if /I not "%URL:~0,8%"=="https://" (
  echo installer URL must be https
  exit /b 1
)
if "%WANT%"=="" (
  echo setup sha256 is not pinned
  exit /b 1
)
if not exist "%~dp0room.key" (
  {missing}
)
copy /Y "%~dp0room.key" "%KEYFILE%" >nul
echo downloading installer
powershell -NoProfile -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%URL%' -OutFile '%SETUP%' -UseBasicParsing; $h=(Get-FileHash -LiteralPath '%SETUP%' -Algorithm SHA256).Hash.ToLower(); if ($h -ne '%WANT%'.ToLower()) {{ Remove-Item -LiteralPath '%SETUP%' -Force; exit 1 }}"
if errorlevel 1 (
  del /f /q "%SETUP%" >nul 2>&1
  del /f /q "%KEYFILE%" >nul 2>&1
  exit /b 1
)
"%SETUP%" {flags} /CONTROLLER={ctl} /ROOMKEYFILE="%KEYFILE%"
set "RC=%ERRORLEVEL%"
del /f /q "%SETUP%" >nul 2>&1
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
        "The script downloads the setup exe to a new temp path over https and checks\n"
        "the pinned SHA-256. It does not reuse a file already sitting in TEMP.\n"
        "Each successful install consumes one use. This zip is a secret: it is not\n"
        "the public download. Delete it when the room is full, or revoke the key\n"
        "in the console. Do not mail the zip or paste room.key into chat.\n"
    )


def build_room_pack(*, room_key: str, controller: str, setup_url: str, group: str = "",
                    label: str = "", expires_at: Optional[float] = None, max_uses: int = 1,
                    setup_sha256: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("room.key", room_key.strip() + "\n")
        z.writestr("Install.cmd", _install_body(
            silent=False, setup_url=setup_url, controller=controller, setup_sha256=setup_sha256).replace("\n", "\r\n"))
        z.writestr("Install-Silent.cmd", _install_body(
            silent=True, setup_url=setup_url, controller=controller, setup_sha256=setup_sha256).replace("\n", "\r\n"))
        z.writestr("README.txt", _readme(group, label, expires_at, max_uses).replace("\n", "\r\n"))
    return buf.getvalue()
