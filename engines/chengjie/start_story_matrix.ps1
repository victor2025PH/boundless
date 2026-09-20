# 智聊 · story_matrix 本地实例（176 智拓机）。
#   前台：  .\start_story_matrix.ps1
#   后台：  .\start_story_matrix.ps1 -Background      （最小化窗口常驻，日志 logs\story_matrix_local.log）
#   停止：  .\start_story_matrix.ps1 -Stop
param([switch]$Background, [switch]$Stop)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
Remove-Item Env:AITR_DESKTOP_MODE -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path logs | Out-Null

$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'zhiliao' -and $_.CommandLine -match 'main\.py' }
if ($Stop) {
    foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force; "stopped pid=$($p.ProcessId)" }
    return
}
if ($procs) { "already running: pid=$($procs.ProcessId -join ',')"; return }

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if ($Background) {
    $p = Start-Process -FilePath $py -ArgumentList 'main.py' -WorkingDirectory $PSScriptRoot -WindowStyle Minimized -PassThru `
        -RedirectStandardOutput logs\story_matrix_local.log -RedirectStandardError logs\story_matrix_local.err.log
    "started pid=$($p.Id)  http://127.0.0.1:18796"
} else {
    & $py main.py
}
