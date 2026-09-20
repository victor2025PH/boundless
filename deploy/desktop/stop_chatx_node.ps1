# stop_chatx_node.ps1 -- stop the ChatX app + its sidecars on THIS machine.
# Match by executable path, never by name (the app name is CJK; PS 5.1 GBK lesson).
# Mirrors the stop section of install_chatx_node.ps1 so hot-patch flows reuse
# the exact same semantics. Exit 0 always (stopping nothing is fine).
[CmdletBinding()]
param([int]$WaitSec = 3)
$ErrorActionPreference = 'Continue'
$procs = Get-Process -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -and $_.Path -like '*telegram-ai-desktop*' }
if ($procs) {
  Write-Output ("[stop] stopping: " + ($procs.Id -join ','))
  $procs | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds $WaitSec
  # second sweep: sidecars spawned late can outlive the first pass
  Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path -like '*telegram-ai-desktop*' } |
    Stop-Process -Force -ErrorAction SilentlyContinue
} else {
  Write-Output "[stop] nothing running"
}
exit 0
