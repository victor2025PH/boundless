# ASCII-ONLY: deploy_desktop_agent.ps1 -- roll out the read-only desktop_agent to one machine.
# Codifies the P0 rollout recipe (2026-08-10) so future machines are ONE command, not manual
# archaeology: pip install (mss+Pillow) into the given python, scp agent, generate an ASCII
# launcher with that python baked in, register an ONLOGON+INTERACTIVE task (ssh sessions have
# no desktop -> mss BitBlt fails; capture MUST run in the console session), and start it.
# Deliberately uses a STANDALONE system python (NOT the machine's ML venv) to avoid disturbing
# production TTS/STT/lipsync environments.
#
# Usage:
#   powershell -File deploy_desktop_agent.ps1 -Alias kouxing -Py "C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe" -RunUser Administrator
#   (Hub IP defaults to 192.168.0.176:7913; -Machine defaults to auto-detect by hostname.)
param(
  [Parameter(Mandatory = $true)][string]$Alias,
  [Parameter(Mandatory = $true)][string]$Py,
  [string]$RunUser = "Administrator",
  [string]$Hub = "http://192.168.0.176:7913",
  [string]$Machine = "",
  [int]$Fps = 8
)
$ErrorActionPreference = "Stop"
$src = Join-Path $PSScriptRoot "desktop_agent.py"
$dstDir = "C:\Users\Public\boundless-hud"
$dst = "$dstDir\desktop_agent.py"
$bat = "$dstDir\desktop_agent_launch.bat"

Write-Host "[1/5] ensure remote dir + deps (mss + Pillow) into $Py ..."
ssh $Alias "if not exist $dstDir mkdir $dstDir"
# NOTE: uv-created venvs often have NO pip ("No module named pip"), and some compute nodes have
# limited PyPI reach -- pip here can fail. Fallback: mss is pure-python; scp the hub's
# site-packages\mss dir straight into the target venv site-packages (Pillow is usually already
# present in ML/vision venvs). 2026-08-10 yunsheng index-tts uv-venv used exactly this fallback.
# No -q here: surface the failure so you know whether to use the copy fallback.
ssh $Alias "`"$Py`" -m pip install mss pillow --timeout 30"

Write-Host "[2/5] scp desktop_agent.py ..."
scp $src "${Alias}:C:/Users/Public/boundless-hud/desktop_agent.py" | Out-Null

Write-Host "[3/5] generate ASCII launcher (python baked in) ..."
$mArg = if ($Machine) { " --machine $Machine" } else { "" }
$batBody = "@echo off`r`n" +
  "rem ASCII-ONLY: desktop_agent launcher (interactive console session; mss needs a real desktop)`r`n" +
  "start `"`" /b `"$Py`" `"$dst`" --hub $Hub --fps $Fps$mArg`r`n"
$tmp = Join-Path $env:TEMP "desktop_agent_launch.bat"
[IO.File]::WriteAllText($tmp, $batBody, [Text.Encoding]::ASCII)
scp $tmp "${Alias}:C:/Users/Public/boundless-hud/desktop_agent_launch.bat" | Out-Null

Write-Host "[4/5] register + run ONLOGON interactive task ..."
ssh $Alias "schtasks /create /f /tn BoundlessDesktopAgent /sc onlogon /ru $RunUser /it /tr $bat"
ssh $Alias "schtasks /end /tn BoundlessDesktopAgent 2>nul & schtasks /run /tn BoundlessDesktopAgent"

Write-Host "[5/5] done. Verify from hub:  set want + pull  /api/desktop/$Alias.jpg"
Write-Host "  (agent idles at 1s poll until the hub wants this machine; capture runs in console session)"
