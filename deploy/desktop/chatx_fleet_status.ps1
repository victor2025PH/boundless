# chatx_fleet_status.ps1 -- one-glance ChatX version ledger across nodes.
#
# Why: "which build is .198 actually running?" cost real time on 2026-07-31
# (assumed 0.2.11, was 0.2.12, now 1.0.1). The RUNNING backend is the truth,
# not the installed exe: /api/desktop/ping is the auth-free identity probe the
# desktop shell itself uses to avoid adopting a stranger's backend, so we read
# the same endpoint per node. Backend down -> fall back to the installed exe
# version (marked "exe only", i.e. installed but not currently serving).
#
# -Telemetry (P1 2026-08-13): seat-local ui-event trend used to be a data
# island -- the copilot-panel fallback regression sat on seats for days with
# zero HQ visibility until the boss saw it with his own eyes. This flag pulls
# each seat's daily-aggregate DB (%APPDATA%\<app>\data\config\ui_event_trend.db,
# written when ops.ui_event_trend is on -- internal seed config enables it)
# via ssh copy + scp and summarizes locally. No auth token needed, works even
# when the app/backend is closed (yesterday's numbers are still on disk).
# Also prints resources/build-info.json provenance when present (1.0.24+).
#
# edition column (L-5 / D-L1, 2026-09-06): "<flavor>/<channel>" from
# _seat_edition_probe.ps1 -- flavor = what is installed (internal=smart with
# seed-data | clean | lite, from resources/build-info.json), channel = which
# update feed the installed app follows (resources/app-update.yml: internal =
# downloads/internal/latest-internal.yml, public = downloads/latest.yml).
# K-5 lesson: the seats were pushed the smart 1.0.74 while the public feed
# carried the clean 1.0.74 -- same version string, different package, and
# the next "update" click would silently turn a smart seat into a clean one.
# "internal/public" is exactly that trap and gets flagged in the note.
#
# ASCII-only (PS 5.1 GBK lesson). Read-only w.r.t. seat state: copies the DB
# to the seat's temp dir, never touches the live file or any process.
#
# Usage: powershell -File deploy\desktop\chatx_fleet_status.ps1 [-Targets yunsheng,kouxing,lianbei]
#        powershell -File deploy\desktop\chatx_fleet_status.ps1 -Telemetry [-Days 7] [-Prefix cpshell_]
[CmdletBinding()]
param(
  [string[]]$Targets = @('yunsheng', 'kouxing', 'lianbei'),
  [switch]$Telemetry,
  [int]$Days = 7,
  [string]$Prefix = 'cpshell_'
)

$ErrorActionPreference = 'Continue'
# `powershell -File` passes "a,b,c" as ONE string (comma binding only works inside
# a PS session) -- normalize so both calling styles behave identically.
$Targets = @($Targets | ForEach-Object { "$_".Split(',') } |
  ForEach-Object { "$_".Trim() } | Where-Object { $_ })
Write-Output ("{0,-16} {1,-10} {2,-24} {3,7} {4,-15} {5,-17} {6}" -f 'node', 'backend', 'app/version', 'diskGB', 'display', 'edition', 'note')
Write-Output ('-' * 104)

foreach ($t in $Targets) {
  $note = ''
  $backend = 'DOWN'
  $ver = ''
  # 1. running backend identity (auth-free by design)
  $ping = ssh -o ConnectTimeout=6 $t "curl -s --max-time 5 http://127.0.0.1:18799/api/desktop/ping" 2>$null
  if ($LASTEXITCODE -eq 0 -and $ping -and ("$ping" -match '"version"\s*:\s*"([^"]+)"')) {
    $backend = 'UP'
    $ver = $Matches[1]
    if ("$ping" -match '"app"\s*:\s*"([^"]+)"') { $ver = $Matches[1] + ' ' + $ver }
  } else {
    # 2. fall back to installed exe version (installed but not serving)
    $exe = ssh -o ConnectTimeout=6 $t ("powershell -NoProfile -Command " +
      "(Get-ChildItem \`"`$env:LOCALAPPDATA\Programs\telegram-ai-desktop\`" -Filter *.exe " +
      "-ErrorAction SilentlyContinue ^| Where-Object Name -notlike 'Uninstall*' ^| " +
      "Select-Object -First 1).VersionInfo.ProductVersion") 2>$null
    if ($exe) { $ver = ("" + $exe).Trim(); $note = 'exe only (backend not serving)' }
    else { $note = 'not installed / unreachable' }
  }
  # 3. C: free space (2026-08-17 .198 lesson: 2.1 GB free bit both the release
  #    push AND the running seat; stale Chromium scoped_dir* temps ate ~69 GB).
  #    Surface it on the ledger everyone already reads; <10 GB flags the note.
  $diskRaw = ssh -o ConnectTimeout=6 $t 'powershell -NoProfile -Command "[math]::Round((Get-PSDrive C).Free/1GB,1)"' 2>$null
  $disk = ''
  $dv = 0.0
  if ([double]::TryParse(("$diskRaw".Trim()), [ref]$dv)) {
    $disk = ("" + $dv)
    if ($dv -lt 10) { $note = ("LOW DISK (stale scoped_dir temps? see push_chatx auto-remedy) " + $note).Trim() }
  }
  # 4. display sanity (2026-08-17 .173 lesson: 4K panel at Windows-recommended 300%
  #    -> 1280x720 LOGICAL desktop; the workspace collapses into its narrow layout and
  #    the composer toolbar sits below the fold, i.e. the seat "loses" buttons with
  #    zero code involved). Column shows logical WxH@scale; logical height < 800
  #    flags the note -> remedy is deploy\desktop\set_seat_scale.ps1 (live, no logoff).
  #    Mechanism: scp a static probe file + run it (same pattern as the telemetry
  #    section) -- inlining this over ssh dies in PS5.1/cmd quote mangling, and naive
  #    AppliedDPI reads miss live per-monitor overrides (probe header has the details).
  $probeSrc = Join-Path $PSScriptRoot '_seat_disp_probe.ps1'
  $disp = ''
  if (Test-Path $probeSrc) {
    scp -o ConnectTimeout=6 $probeSrc ($t + ':C:/Windows/Temp/_seat_disp_probe.ps1') 2>$null | Out-Null
    $dispRaw = ssh -o ConnectTimeout=6 $t 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\Windows\Temp\_seat_disp_probe.ps1' 2>$null
    if ("$dispRaw".Trim() -match '^EXACT (\d+)x(\d+)@(\d+)$') {
      # Shell-written breadcrumb (>= 1.0.39): live Chromium truth, no candidates,
      # no ambiguity marker. Older seats keep the registry-derived branch below.
      $lw = [int]$Matches[1]; $lh = [int]$Matches[2]; $pct = [int]$Matches[3]
      $disp = ('' + $lw + 'x' + $lh + '@' + $pct + '%')   # plain = exact (shell-reported)
      if ($lh -lt 800) { $note = ('SMALL DESKTOP (toolbar may not fit; run set_seat_scale.ps1) ' + $note).Trim() }
    }
    elseif ("$dispRaw".Trim() -match '^(\d+)x(\d+)@(\d+)#(-?\d+)$') {
      $pw = [int]$Matches[1]; $ph = [int]$Matches[2]
      $dpiPct = [int][math]::Round([int]$Matches[3] * 100.0 / 96)
      $ovr = [int]$Matches[4]
      # Effective scale from logon-snapshot dpi + live override steps on the standard
      # ladder. Ambiguity (documented in the probe header): after the NEXT sign-in the
      # snapshot may absorb the override, so compute both candidates and only flag
      # SMALL when even the LARGEST logical height is under budget (no false alarms;
      # exact live value arrives with shell-reported telemetry in a future release).
      $ladder = @(100, 125, 150, 175, 200, 225, 250, 300, 350, 400, 450, 500)
      $candB = $dpiPct                                  # snapshot already includes ovr
      $candA = $dpiPct                                  # snapshot is the recommended rung
      $iRec = $ladder.IndexOf($dpiPct)
      if ($ovr -ne 0 -and $iRec -ge 0) {
        $iCur = $iRec + $ovr
        if ($iCur -ge 0 -and $iCur -lt $ladder.Count) { $candA = $ladder[$iCur] }
      }
      $pct = $candA
      $lw = [int][math]::Round($pw * 100.0 / $pct)
      $lh = [int][math]::Round($ph * 100.0 / $pct)
      $disp = ('' + $lw + 'x' + $lh + '@' + $pct + '%')
      if ($ovr -ne 0) { $disp = $disp + '*' }           # * = live override, best-effort
      $lhWorst = [math]::Max([int][math]::Round($ph * 100.0 / $candA), [int][math]::Round($ph * 100.0 / $candB))
      if ($lhWorst -lt 800) { $note = ('SMALL DESKTOP (toolbar may not fit; run set_seat_scale.ps1) ' + $note).Trim() }
    }
  }
  # 5. edition = installed flavor / followed update channel (probe file, same scp+run
  #    pattern as the display probe; see _seat_edition_probe.ps1 header).
  $edition = ''
  $edSrc = Join-Path $PSScriptRoot '_seat_edition_probe.ps1'
  if (Test-Path $edSrc) {
    scp -o ConnectTimeout=6 $edSrc ($t + ':C:/Windows/Temp/_seat_edition_probe.ps1') 2>$null | Out-Null
    $edRaw = ssh -o ConnectTimeout=6 $t 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\Windows\Temp\_seat_edition_probe.ps1' 2>$null
    if ("$edRaw".Trim() -match '^EDITION flavor=(\S+) seed=(\d) channel=(\S+)') {
      $fl = $Matches[1]; $sd = [int]$Matches[2]; $ch = $Matches[3]
      $flShow = if ($fl -eq 'internal') { 'smart' } else { $fl }
      $edition = ($flShow + '/' + $ch)
      if ($fl -eq 'internal' -and $sd -eq 0) { $edition += '!' ; $note = ('SMART BUILD-INFO BUT NO seed-data (half-shipped package?) ' + $note).Trim() }
      if ($fl -eq 'internal' -and $ch -eq 'public') {
        # The K-5 trap: smart install still on the public feed -> next update turns it clean.
        $note = ('SMART ON PUBLIC FEED (next update = clean pkg; reinstall smart from downloads/internal/ to switch) ' + $note).Trim()
      }
      if ($fl -eq 'clean' -and $ch -eq 'internal') { $note = ('CLEAN ON INTERNAL FEED (will pick up smart pkg next update) ' + $note).Trim() }
    }
  }
  Write-Output ("{0,-16} {1,-10} {2,-24} {3,7} {4,-15} {5,-17} {6}" -f $t, $backend, $ver, $disk, $disp, $edition, $note)
}

if (-not $Telemetry) { return }

# ==== telemetry section: per-seat ui-event trend + build provenance =========
$py = Join-Path $PSScriptRoot 'fleet_uievt_summary.py'
$tmpRoot = Join-Path $env:TEMP 'chatx_fleet'
New-Item -ItemType Directory -Force -Path $tmpRoot | Out-Null

Write-Output ''
Write-Output ("=== telemetry: ui-event trend (prefix={0}, last {1} days, UTC days) ===" -f $Prefix, $Days)
Write-Output ("{0,-16} {1,-36} {2,7} {3,7}" -f 'node', 'action', ('d' + $Days), 'today')
Write-Output ('-' * 70)

foreach ($t in $Targets) {
  # a. build provenance straight off the installed package (present since 1.0.24)
  $biCmd = ("powershell -NoProfile -Command " +
    "Get-Content \`"`$env:LOCALAPPDATA\Programs\telegram-ai-desktop\resources\build-info.json\`" " +
    "-Raw -ErrorAction SilentlyContinue")
  $bi = ssh -o ConnectTimeout=6 $t $biCmd 2>$null
  if ($LASTEXITCODE -eq 0 -and $bi) {
    $j = $null
    try { $j = ("$bi" | ConvertFrom-Json) } catch { $j = $null }
    if ($j) {
      $commit = ("" + $j.git.commit)
      if ($commit.Length -gt 9) { $commit = $commit.Substring(0, 9) }
      Write-Output ("{0,-16} build {1} @ {2} (dirty:{3}) {4}" -f `
        $t, $j.version, $commit, $j.git.dirty_count, $j.builtAt)
    }
  }

  # b. copy the seat's trend DB to an ASCII temp path (app dir name is CJK --
  #    glob it remotely instead of passing CJK over ssh), then scp it home.
  #    .db alone is NOT enough: the store runs WAL mode -- a freshly-booted seat
  #    holds schema+rows entirely in the -wal sidecar (bit us on the first live
  #    pull: copied .db had no table at all). Copy .db* and scp all three; the
  #    local summary opens writable so sqlite recovers the WAL into our snapshot.
  $copyCmd = ("powershell -NoProfile -Command " +
    "`$f = Get-ChildItem \`"`$env:APPDATA\*\data\config\ui_event_trend.db\`" " +
    "-ErrorAction SilentlyContinue ^| Sort-Object LastWriteTime -Descending ^| Select-Object -First 1; " +
    "if (`$f) { Copy-Item (`$f.FullName + '*') C:\Windows\Temp\ -Force; Write-Output OK } " +
    "else { Write-Output MISS }")
  $r = ssh -o ConnectTimeout=6 $t $copyCmd 2>$null
  if ("$r" -notmatch 'OK') {
    Write-Output ("{0,-16} (no trend db: app never ran / ops.ui_event_trend off / unreachable)" -f $t)
    continue
  }
  $ldir = Join-Path $tmpRoot $t
  New-Item -ItemType Directory -Force -Path $ldir | Out-Null
  Remove-Item (Join-Path $ldir 'ui_event_trend.db*') -Force -ErrorAction SilentlyContinue
  foreach ($suffix in @('', '-wal', '-shm')) {
    # -wal/-shm may legitimately be absent (checkpointed) -> tolerate scp misses
    scp -o ConnectTimeout=6 ("{0}:C:/Windows/Temp/ui_event_trend.db{1}" -f $t, $suffix) $ldir 2>$null | Out-Null
  }
  $local = Join-Path $ldir 'ui_event_trend.db'
  if (-not (Test-Path $local)) {
    Write-Output ("{0,-16} (scp failed)" -f $t)
    continue
  }
  # PS 5.1 drops empty-string args to native exes -> omit the prefix arg entirely
  if ($Prefix) { $lines = & python $py $local $Days $Prefix 2>$null }
  else { $lines = & python $py $local $Days 2>$null }
  if ($LASTEXITCODE -eq 3) {
    Write-Output ("{0,-16} (trend store uninitialized: ops.ui_event_trend off, or backend never wrote)" -f $t)
    continue
  }
  if (-not $lines) {
    Write-Output ("{0,-16} (no '{1}' events in window)" -f $t, $Prefix)
    continue
  }
  foreach ($ln in $lines) {
    $p = "$ln".Split('|')
    if ($p.Count -ge 3) {
      Write-Output ("{0,-16} {1,-36} {2,7} {3,7}" -f $t, $p[0], $p[1], $p[2])
    }
  }
}

Write-Output ''
Write-Output 'reading guide: cpshell_iframe_fallback should trend to ~0 on 1.0.24+ (boot gate);'
Write-Output '  sustained fallback = backend never becoming healthy on that seat (check backend.log).'
Write-Output '  cpshell_nba_exec_fail > 0 = seats hitting real action errors (reason now toasted on seat).'
