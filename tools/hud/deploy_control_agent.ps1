# ASCII-ONLY: deploy_control_agent.ps1 -- roll out the INPUT-INJECTION control_agent to ONE machine.
# P2 (2026-08-10, owner-approved: only lianbei/kouxing/shengbei; production live-chain stays read-only).
# control_agent is the ONLY code that injects mouse/keyboard on a target; its mere presence means the
# machine was explicitly authorized to be controllable. It has ZERO third-party deps (stdlib + ctypes),
# so no pip step -- lighter than desktop_agent. Steps: mkdir; generate a per-machine secret; record it in
# the hub ledger (C:\AvatarHub\secrets\ctrl_agents.json) AND ship it to the target; scp the agent; write
# an ASCII launcher (python + secret baked); register ONLOGON+INTERACTIVE task (SendInput needs a real
# desktop -> ssh session has none); start it.
#
# Usage:
#   powershell -File deploy_control_agent.ps1 -Alias kouxing -Py "C:\...\python.exe" -Machine kouxing -RunUser Administrator
#   (Hub defaults 192.168.0.176:7913; ledger path defaults to C:\AvatarHub\secrets\ctrl_agents.json)
param(
  [Parameter(Mandatory = $true)][string]$Alias,
  [Parameter(Mandatory = $true)][string]$Py,
  [Parameter(Mandatory = $true)][string]$Machine,
  [Parameter(Mandatory = $true)][string]$Secret,   # 32-hex; caller records it in the hub ledger
  [string]$RunUser = "Administrator",
  [string]$Hub = "http://192.168.0.176:7913"
)
$ErrorActionPreference = "Stop"
$allow = @("lianbei", "kouxing", "shengbei")
if ($allow -notcontains $Machine) {
  throw "REFUSE: '$Machine' is not in the control whitelist ($($allow -join '/')). Production live-chain stays read-only."
}
if ($Secret -notmatch '^[0-9a-fA-F]{16,}$') { throw "Secret must be >=16 hex chars (caller-generated, ledger-recorded)." }
$src = Join-Path $PSScriptRoot "control_agent.py"
$dstDir = "C:\Users\Public\boundless-hud"
$dst = "$dstDir\control_agent.py"
$secFile = "$dstDir\control_agent.secret"
$bat = "$dstDir\control_agent_launch.bat"
$secret = $Secret

Write-Host "[3/6] ensure remote dir + ship agent + secret ..."
ssh $Alias "if not exist $dstDir mkdir $dstDir"
scp $src "${Alias}:C:/Users/Public/boundless-hud/control_agent.py" | Out-Null
$tmpSec = Join-Path $env:TEMP "control_agent.secret"
[IO.File]::WriteAllText($tmpSec, $secret, [Text.Encoding]::ASCII)
scp $tmpSec "${Alias}:C:/Users/Public/boundless-hud/control_agent.secret" | Out-Null
Remove-Item $tmpSec -Force

Write-Host "[4/6] generate ASCII launcher (python + machine baked; secret read from file) ..."
$batBody = "@echo off`r`n" +
  "rem ASCII-ONLY: control_agent launcher (interactive console session; SendInput needs a real desktop)`r`n" +
  "start `"`" /b `"$Py`" `"$dst`" --hub $Hub --machine $Machine`r`n"
$tmp = Join-Path $env:TEMP "control_agent_launch.bat"
[IO.File]::WriteAllText($tmp, $batBody, [Text.Encoding]::ASCII)
scp $tmp "${Alias}:C:/Users/Public/boundless-hud/control_agent_launch.bat" | Out-Null
Remove-Item $tmp -Force

Write-Host "[5/6] register + run ONLOGON interactive task ..."
ssh $Alias "schtasks /create /f /tn BoundlessControlAgent /sc onlogon /ru $RunUser /it /tr $bat"
ssh $Alias "schtasks /end /tn BoundlessControlAgent 2>nul & schtasks /run /tn BoundlessControlAgent"

Write-Host "[6/6] done. $Machine is now controllable (armed + face-auth required per action)."
Write-Host "  Verify: control_agent polls /api/ctrl/pull (granted=false until a face-armed session)."
Write-Host "  Selftest on target console:  `"$Py`" `"$dst`" --selftest   (harmless: nudges cursor, reads back)"
