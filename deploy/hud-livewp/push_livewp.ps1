# boundless live wallpaper builder / deployer (ASCII-ONLY source).
# Assembles a self-contained Lively web-wallpaper folder for one roster machine:
#   index.html (this dir) + config.js (generated from deploy/machines.json)
#   + assets/ (cosmos bg, boundless mark, 8 product icons - pulled from website/public,
#   single source, nothing duplicated into git).
# Usage:
#   powershell -File push_livewp.ps1 -Machine shengbei                 # stage only
#   powershell -File push_livewp.ps1 -Machine shengbei -Apply         # + copy to the box
#   powershell -File push_livewp.ps1 -Machine shengbei -Apply -SetWallpaper
# Target dir on every box: C:\Users\Public\boundless-hud\livewp
# (sentinel.ps1 v6 writes data.js there each minute; page reloads it every 30s).
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
  ssh -o ConnectTimeout=8 $target 'if not exist C:\Users\Public\boundless-hud\livewp mkdir C:\Users\Public\boundless-hud\livewp'
  cmd /c ('tar -cf - --exclude data.js --exclude "shot_*.png" -C "' + $StageRoot + '" . | ssh -o ConnectTimeout=8 ' + $target + ' "tar -xf - -C C:/Users/Public/boundless-hud/livewp"')
  if ($LASTEXITCODE -ne 0) { throw ('tar-pipe copy failed rc=' + $LASTEXITCODE) }
}
Write-Output ('APPLIED ' + $Machine + ' -> ' + $dest)

# ---------- 3b) optional: push the v6 sentinel (data.js bridge) ----------
if ($PushSentinel) {
  $senSrc = Join-Path $PSScriptRoot 'sentinel_v6.ps1'
  if (-not (Test-Path $senSrc)) { throw 'sentinel_v6.ps1 missing next to this script' }
  $senDst = 'C:\Users\Public\boundless-hud\sentinel.ps1'
  if ($isLocal) {
    Copy-Item $senDst ($senDst + '.bak-livewp') -Force -ErrorAction SilentlyContinue
    Copy-Item $senSrc $senDst -Force
  } else {
    $target = [string]$m.ssh[0]
    ssh -o ConnectTimeout=8 $target ('copy /y ' + $senDst + ' ' + $senDst + '.bak-livewp')
    scp -o ConnectTimeout=8 $senSrc ($target + ':C:/Users/Public/boundless-hud/sentinel.ps1')
  }
  Write-Output 'SENTINEL_V6_PUSHED'
}
if (-not $SetWallpaper) { exit 0 }

# ---------- 4) drive Lively in the interactive session (schtasks pattern) ----------
# Proven recipe (117 pilot 2026-08-29): the CLI REJECTS a raw .html import
# ("Unsupported command import file"). A Type-1 (local web) library project with an
# ABSOLUTE FileName must exist first; then closewp + setwp <project folder> works.
# The apply script runs on the target in the interactive session, so LOCALAPPDATA
# resolves to the console user there.
$findLively = @'
$exe = @(
  (Join-Path ${env:ProgramFiles(x86)} 'Lively Wallpaper\Lively.exe'),
  (Join-Path $env:ProgramFiles 'Lively Wallpaper\Lively.exe'),
  (Join-Path $env:LOCALAPPDATA 'Programs\Lively Wallpaper\Lively.exe')
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $exe) { Write-Output 'LIVELY_NOT_FOUND'; exit 1 }
$lib = Join-Path $env:LOCALAPPDATA 'Lively Wallpaper\Library\wallpapers\boundless-livewp'
New-Item -ItemType Directory -Force -Path $lib | Out-Null
$info = '{"AppVersion":"2.2.1.0","Title":"BOUNDLESS LIVE CLUSTER","Thumbnail":"","Preview":"",' +
  '"Desc":"stargate particles + machine identity + live cluster panel","Author":"BOUNDLESS",' +
  '"License":"","Contact":"","Type":1,' +
  '"FileName":"C:\\Users\\Public\\boundless-hud\\livewp\\index.html",' +
  '"Arguments":"","IsAbsolutePath":true,"Id":"boundless-livewp"}'
[IO.File]::WriteAllText((Join-Path $lib 'LivelyInfo.json'), $info, [Text.UTF8Encoding]::new($false))
Start-Process -FilePath $exe
Start-Sleep -Seconds 8
Start-Process -FilePath $exe -ArgumentList @('closewp','--monitor','-1')
Start-Sleep -Seconds 4
Start-Process -FilePath $exe -ArgumentList @('setwp','--file',('"' + $lib + '"'))
'@
$setScript = 'C:\Users\Public\boundless-hud\livewp_apply.ps1'
$launcher = 'C:\Users\Public\boundless-hud\lively_launch.ps1'
$launchBody = @'
$exe = @(
  (Join-Path ${env:ProgramFiles(x86)} 'Lively Wallpaper\Lively.exe'),
  (Join-Path $env:ProgramFiles 'Lively Wallpaper\Lively.exe'),
  (Join-Path $env:LOCALAPPDATA 'Programs\Lively Wallpaper\Lively.exe')
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($exe) { Start-Process -FilePath $exe }
'@
if ($isLocal) {
  [IO.File]::WriteAllText($setScript, $findLively, [Text.UTF8Encoding]::new($false))
  [IO.File]::WriteAllText($launcher, $launchBody, [Text.UTF8Encoding]::new($false))
  schtasks /create /f /tn BoundlessLively /sc onlogon /it /tr ('powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ' + $launcher) | Out-Null
  schtasks /create /f /tn BoundlessLivelyApply /sc onlogon /it /tr ('powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ' + $setScript) | Out-Null
  schtasks /run /tn BoundlessLivelyApply | Out-Null
  Start-Sleep -Seconds 16
  schtasks /delete /f /tn BoundlessLivelyApply | Out-Null
} else {
  # schtasks /tr quoting does NOT survive ssh->cmd (inner quotes get eaten, schtasks
  # sees -NoProfile as its own arg - 104 canary 2026-08-29). So: push the scripts,
  # plus a tasks-setup script, and run THAT on the box via powershell -File; the
  # schtasks calls then execute locally where PS passes /tr as one quoted arg.
  $target = [string]$m.ssh[0]
  $tasksBody = @'
schtasks /create /f /tn BoundlessLively /sc onlogon /it /tr "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Users\Public\boundless-hud\lively_launch.ps1"
schtasks /create /f /tn BoundlessLivelyApply /sc onlogon /it /tr "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Users\Public\boundless-hud\livewp_apply.ps1"
schtasks /run /tn BoundlessLivelyApply
Start-Sleep -Seconds 16
schtasks /delete /f /tn BoundlessLivelyApply
Write-Output 'TASKS_DONE'
'@
  $tasksPath = 'C:\Users\Public\boundless-hud\livewp_tasks.ps1'
  foreach ($pair in @(@($setScript, $findLively), @($launcher, $launchBody), @($tasksPath, $tasksBody))) {
    $b64 = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($pair[1]))
    ssh -o ConnectTimeout=8 $target ('powershell -NoProfile -Command "[IO.File]::WriteAllBytes(''' + $pair[0] + ''', [Convert]::FromBase64String(''' + $b64 + '''))"')
  }
  ssh -o ConnectTimeout=8 $target ('powershell -NoProfile -ExecutionPolicy Bypass -File ' + $tasksPath)
}
Write-Output 'SETWP_DISPATCHED (verify: msedgewebview2 cmdline should reference livewp\index.html)'
