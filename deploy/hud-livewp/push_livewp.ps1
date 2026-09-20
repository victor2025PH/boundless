# boundless live wallpaper builder / deployer (ASCII-ONLY source).
# Assembles a self-contained Lively web-wallpaper folder for one roster machine:
#   index.html (this dir) + config.js (generated from deploy/machines.json)
#   + assets/ (cosmos bg, boundless mark, 8 product icons - pulled from website/public,
#   single source, nothing duplicated into git).
# Usage:
#   powershell -File push_livewp.ps1 -Machine shengbei                 # stage only
#   powershell -File push_livewp.ps1 -Machine shengbei -Apply         # + copy to the box
#   powershell -File push_livewp.ps1 -Machine shengbei -Apply -SetWallpaper
#   powershell -File push_livewp.ps1 -Machine shengbei -Apply -SetWallpaper -PushSentinel   # full rollout
# Target dir on every box: C:\Users\Public\boundless-hud\livewp
# (sentinel.ps1 v7 writes data.js there each minute; page reloads it every 30s).
# -Apply also drops livewp_ensure.ps1 (+ run_hidden.vbs if missing) next to sentinel.ps1;
# -SetWallpaper registers the BoundlessLively logon task on it and runs it once with -Force;
# sentinel v7 (-PushSentinel) calls the same script when the player drifts off livewp.
# Remote boxes: copies over ssh (tar pipe - scp -r glob is unreliable on win32 openssh),
# and drives Lively via schtasks so it runs in the INTERACTIVE session (session-0 ssh
# cannot show a wallpaper window; same pattern as the 8/7 pilot scripts).
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Machine,
  [switch]$Apply,
  [switch]$SetWallpaper,
  [switch]$PushSentinel,
  [switch]$Lite,
  [string]$StageRoot = ""
)
$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$rosterPath = Join-Path $RepoRoot 'deploy\machines.json'
$roster = Get-Content $rosterPath -Raw -Encoding UTF8 | ConvertFrom-Json
$m = $roster.machines | Where-Object { $_.id -eq $Machine }
if (-not $m) { throw ('machine not in roster: ' + $Machine) }
if (-not $StageRoot) { $StageRoot = Join-Path $RepoRoot ('tmp\livewp_stage\' + $Machine) }

# ---------- 1) assemble staging ----------
New-Item -ItemType Directory -Force -Path (Join-Path $StageRoot 'assets\products') | Out-Null
Copy-Item (Join-Path $PSScriptRoot 'index.html') (Join-Path $StageRoot 'index.html') -Force
$pub = Join-Path $RepoRoot 'website\public'
Copy-Item (Join-Path $pub 'intro\cosmos-wide.jpg') (Join-Path $StageRoot 'assets\cosmos.jpg') -Force
Copy-Item (Join-Path $pub 'brand\logos\boundless-mark-512.webp') (Join-Path $StageRoot 'assets\mark.webp') -Force
Copy-Item (Join-Path $pub 'brand\logos\boundless-mark-512.png') (Join-Path $StageRoot 'assets\mark.png') -Force
$keys = @('chatx', 'reachx', 'matrixx', 'voxx', 'voicex', 'facex', 'livex', 'fatex')
foreach ($k in $keys) {
  Copy-Item (Join-Path $pub ('brand\products\' + $k + '.webp')) (Join-Path $StageRoot ('assets\products\' + $k + '.webp')) -Force
  Copy-Item (Join-Path $pub ('brand\products\' + $k + '.png')) (Join-Path $StageRoot ('assets\products\' + $k + '.png')) -Force
}

# ---------- 2) config.js from the roster (zh names come from JSON, source stays ascii) ----------
$sep = ' ' + [char]0x00B7 + ' '
$svc = (@($m.services) -join $sep)
$mach = @()
foreach ($mm in $roster.machines) {
  $mach += [pscustomobject]@{ id = $mm.id; zh = $mm.zh; accent = $mm.accent }
}
$cfgObj = [pscustomobject]@{
  id = $m.id; zh = $m.zh; alias = ([string]$m.ssh[0]).ToUpper(); ip = $m.ip
  gpu = $m.gpu; accent = $m.accent; role = $m.role_short; role_detail = $svc
  lite = [bool]$Lite; machines = $mach
}
$cfgJson = $cfgObj | ConvertTo-Json -Depth 6 -Compress
[IO.File]::WriteAllText((Join-Path $StageRoot 'config.js'), ('window.HUD_CFG=' + $cfgJson + ';'), [Text.UTF8Encoding]::new($false))
Write-Output ('STAGED ' + $StageRoot)
if (-not $Apply) { exit 0 }

# ---------- 3) apply to the target box ----------
$dest = 'C:\Users\Public\boundless-hud\livewp'
$myIps = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty IPAddress)
$isLocal = ($myIps -contains [string]$m.ip)
# data.js is machine-owned (sentinel writes it); preview shots stay local.
if ($isLocal) {
  robocopy $StageRoot $dest /E /XF data.js shot_*.png /NFL /NDL /NJH /NJS | Out-Null
  if ($LASTEXITCODE -ge 8) { throw ('robocopy failed rc=' + $LASTEXITCODE) }
} else {
  $target = [string]$m.ssh[0]
  # shell-agnostic (cmd on most boxes, powershell on 176): explicit powershell, no inner quotes
  ssh -o ConnectTimeout=8 $target 'powershell -NoProfile -Command New-Item -ItemType Directory -Force -Path C:\Users\Public\boundless-hud\livewp' | Out-Null
  cmd /c ('tar -cf - --exclude data.js --exclude "shot_*.png" -C "' + $StageRoot + '" . | ssh -o ConnectTimeout=8 ' + $target + ' "tar -xf - -C C:/Users/Public/boundless-hud/livewp"')
  if ($LASTEXITCODE -ne 0) { throw ('tar-pipe copy failed rc=' + $LASTEXITCODE) }
}
Write-Output ('APPLIED ' + $Machine + ' -> ' + $dest)

# ---------- 3a) self-heal script + hidden launcher (both ASCII; scp is fine for single files) ----------
$hudRoot = 'C:\Users\Public\boundless-hud'
$ensureSrc = Join-Path $PSScriptRoot 'livewp_ensure.ps1'
$vbsSrc = Join-Path $RepoRoot 'deploy\instances\run_hidden.vbs'
foreach ($f in @($ensureSrc, $vbsSrc)) { if (-not (Test-Path $f)) { throw ('missing ' + $f) } }
if ($isLocal) {
  Copy-Item $ensureSrc (Join-Path $hudRoot 'livewp_ensure.ps1') -Force
  if (-not (Test-Path (Join-Path $hudRoot 'run_hidden.vbs'))) { Copy-Item $vbsSrc (Join-Path $hudRoot 'run_hidden.vbs') }
} else {
  $target = [string]$m.ssh[0]
  scp -o ConnectTimeout=8 $ensureSrc ($target + ':C:/Users/Public/boundless-hud/livewp_ensure.ps1')
  if ($LASTEXITCODE -ne 0) { throw 'scp livewp_ensure.ps1 failed' }
  # ssh default shell differs per box (cmd on most, powershell on 176): an explicit
  # `powershell -Command` without inner quotes works on both.
  $probe = ssh -o ConnectTimeout=8 $target 'powershell -NoProfile -Command Test-Path C:\Users\Public\boundless-hud\run_hidden.vbs'
  if (([string]$probe).Trim() -ne 'True') {
    scp -o ConnectTimeout=8 $vbsSrc ($target + ':C:/Users/Public/boundless-hud/run_hidden.vbs')
    if ($LASTEXITCODE -ne 0) { throw 'scp run_hidden.vbs failed' }
    Write-Output 'RUN_HIDDEN_VBS_PUSHED'
  }
}
Write-Output 'ENSURE_SCRIPT_PUSHED'

# ---------- 3b) optional: push the v7 sentinel (data.js bridge + livewp drift watchdog) ----------
if ($PushSentinel) {
  $senSrc = Join-Path $PSScriptRoot 'sentinel_v7.ps1'
  if (-not (Test-Path $senSrc)) { throw 'sentinel_v7.ps1 missing next to this script' }
  $senDst = 'C:\Users\Public\boundless-hud\sentinel.ps1'
  if ($isLocal) {
    Copy-Item $senDst ($senDst + '.bak-livewp') -Force -ErrorAction SilentlyContinue
    Copy-Item $senSrc $senDst -Force
  } else {
    $target = [string]$m.ssh[0]
    ssh -o ConnectTimeout=8 $target ('powershell -NoProfile -Command Copy-Item -Force ' + $senDst + ' ' + $senDst + '.bak-livewp')
    scp -o ConnectTimeout=8 $senSrc ($target + ':C:/Users/Public/boundless-hud/sentinel.ps1')
  }
  Write-Output 'SENTINEL_V7_PUSHED'
}
if (-not $SetWallpaper) { exit 0 }

# ---------- 4) drive Lively in the interactive session (schtasks pattern) ----------
# Proven recipe (117 pilot 2026-08-29): the CLI REJECTS a raw .html import
# ("Unsupported command import file"). A Type-1 (local web) library project with an
# ABSOLUTE FileName must exist first; then closewp + setwp <project folder> works.
# All of that now lives in livewp_ensure.ps1 (pushed in 3a); it runs on the target in
# the interactive session, so LOCALAPPDATA resolves to the console user there.
#   BoundlessLively       (onlogon, permanent) -> livewp_ensure.ps1         re-applies only if drifted
#   BoundlessLivelyApply  (one-shot, deleted)  -> livewp_ensure.ps1 -Force  immediate re-apply now
# Tasks go through run_hidden.vbs (SysWOW64 wscript + Sysnative powershell): a console app
# named directly in /tr flashes a window at logon even with -WindowStyle Hidden.
# schtasks /tr quoting does NOT survive ssh->cmd (inner quotes get eaten, schtasks sees
# -NoProfile as its own arg - 104 canary 2026-08-29), so the schtasks calls live in a
# tasks-setup script that is pushed and executed on the box via powershell -File.
$tasksBody = @'
$hud = 'C:\Users\Public\boundless-hud'
$tr = 'C:\Windows\SysWOW64\wscript.exe //B //Nologo ' + $hud + '\run_hidden.vbs C:\Windows\Sysnative\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File ' + $hud + '\livewp_ensure.ps1'
schtasks /create /f /tn BoundlessLively /sc onlogon /it /tr $tr | Out-Null
schtasks /create /f /tn BoundlessLivelyApply /sc onlogon /it /tr ($tr + ' -Force') | Out-Null
$logBefore = 0
if (Test-Path ($hud + '\livewp_ensure.log')) { $logBefore = (Get-Item ($hud + '\livewp_ensure.log')).Length }
schtasks /run /tn BoundlessLivelyApply | Out-Null
# -Force path: <=30s player wait + 4s closewp + <=25s setwp wait. Poll the task state
# (locale-independent via ScheduledTasks module) so we do not delete a still-running task.
$t0 = Get-Date
do {
  Start-Sleep -Seconds 3
  $st = 'Unknown'
  try { $st = [string](Get-ScheduledTask -TaskName BoundlessLivelyApply -ErrorAction Stop).State } catch {}
} while ($st -eq 'Running' -and ((Get-Date) - $t0).TotalSeconds -lt 90)
schtasks /delete /f /tn BoundlessLivelyApply | Out-Null
foreach ($legacy in @('lively_launch.ps1', 'livewp_apply.ps1')) {
  Remove-Item ($hud + '\' + $legacy) -Force -ErrorAction SilentlyContinue
}
Write-Output ('APPLY_TASK_STATE=' + $st)
if (Test-Path ($hud + '\livewp_ensure.log')) {
  Get-Content ($hud + '\livewp_ensure.log') -Tail 6 | ForEach-Object { 'ENSURE_LOG ' + $_ }
}
Write-Output 'TASKS_DONE'
'@
$tasksPath = 'C:\Users\Public\boundless-hud\livewp_tasks.ps1'
if ($isLocal) {
  [IO.File]::WriteAllText($tasksPath, $tasksBody, [Text.UTF8Encoding]::new($false))
  powershell -NoProfile -ExecutionPolicy Bypass -File $tasksPath
} else {
  # scp the file (shell-agnostic). The former base64-over-ssh write broke on a box whose ssh
  # shell is powershell (176, 2026-09-17): inner quotes were eaten and the STALE tasks script
  # from the previous rollout ran instead. Stage locally, copy, then execute.
  $target = [string]$m.ssh[0]
  $tasksStage = Join-Path (Split-Path -Parent $StageRoot) ($Machine + '_livewp_tasks.ps1')   # outside the tar'd folder
  [IO.File]::WriteAllText($tasksStage, $tasksBody, [Text.UTF8Encoding]::new($false))
  scp -o ConnectTimeout=8 $tasksStage ($target + ':C:/Users/Public/boundless-hud/livewp_tasks.ps1')
  if ($LASTEXITCODE -ne 0) { throw 'scp livewp_tasks.ps1 failed' }
  ssh -o ConnectTimeout=8 $target ('powershell -NoProfile -ExecutionPolicy Bypass -File ' + $tasksPath)
}
Write-Output 'SETWP_DISPATCHED (verify: Lively.Player.WebView2 --wallpaper-url should be livewp\index.html)'
