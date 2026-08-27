# set_seat_scale.ps1 -- set a seat's display scale remotely, LIVE (no logoff/reboot),
# and optionally (re)start the ChatX shell so it renders at the new DPI.
#
# Why this exists (2026-08-17 .173 incident): a 4K panel at Windows-recommended 300%
# leaves a 1280x720 logical desktop; the workspace UI collapses into its narrow layout
# and the composer toolbar sits below the fold -- the seat "loses" buttons. The fix is
# a per-monitor scale override, but Settings-slider access needs hands on the machine.
#
# Mechanism: DisplayConfigSetDeviceInfo with the undocumented DPI packet (type -4,
# relative-to-recommended step offset) -- the same call SetDPI-style tools use. It is
# the ONLY remote path verified to work on Win11 26200: SPI_SETLOGICALDPIOVERRIDE
# (0x009F) returns TRUE but silently no-ops there (measured 2026-08-17 on .173).
# Applied via a one-shot /IT scheduled task because display state lives in the
# interactive session (SSH sessions see a phantom 1024x768 WinDisc display).
#
# Every attempt is verified by probing the logical width from a FRESH child process
# (long-lived processes cache pre-change metrics); failure reverts to the original
# offset. App start picks the LARGEST non-Uninstall exe -- naive "first *.exe" picked
# "Uninstall <CJK>.exe" once and launched the uninstaller (killed in time; lesson baked
# in here). CJK exe names never cross the ssh command line: everything is globbed
# inside the generated remote script.
#
# Usage (from 117):
#   powershell -File deploy\desktop\set_seat_scale.ps1 -TargetSsh yunsheng -Scale 200 -StartApp
#   powershell -File deploy\desktop\set_seat_scale.ps1 -TargetSsh yunsheng -Scale 200 -RestartApp
# Exit codes: 0 = scale verified (and app handled); 2 = scale not reached (reverted);
#             3 = remote task create/run failed; 4 = timeout waiting for remote log.
# ASCII only (PS 5.1 GBK lesson). Scale change itself is per-user, no admin required.
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$TargetSsh,
  [ValidateRange(100, 500)][int]$Scale = 200,
  [switch]$StartApp,     # start shell if not running (never kills)
  [switch]$RestartApp,   # kill UI process only (backend sidecar survives) then start
  [int]$TimeoutSec = 150
)
$ErrorActionPreference = 'Continue'

# --- remote one-shot script (placeholders baked in; runs inside /IT task) ----------
$remoteTemplate = @'
$ErrorActionPreference = 'Continue'
$outDir = Join-Path $env:USERPROFILE 'Downloads\chatx'
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$log = Join-Path $outDir 'seat_scale.log'
'' | Out-File -FilePath $log -Encoding ascii
function Log($m){ ('[' + (Get-Date -Format 'HH:mm:ss') + '] ' + $m) | Out-File -FilePath $log -Append -Encoding ascii }

Add-Type @'CS'
using System;
using System.Runtime.InteropServices;
public class DpiCfg {
  [DllImport("user32.dll")] public static extern int GetDisplayConfigBufferSizes(uint flags, out uint numPaths, out uint numModes);
  [DllImport("user32.dll")] public static extern int QueryDisplayConfig(uint flags, ref uint numPaths, IntPtr paths, ref uint numModes, IntPtr modes, IntPtr topo);
  [DllImport("user32.dll")] public static extern int DisplayConfigGetDeviceInfo(IntPtr pkt);
  [DllImport("user32.dll")] public static extern int DisplayConfigSetDeviceInfo(IntPtr pkt);
  static int[] SourceIds() {
    uint np, nm;
    GetDisplayConfigBufferSizes(2u, out np, out nm);
    IntPtr paths = Marshal.AllocHGlobal((int)(np * 72));
    IntPtr modes = Marshal.AllocHGlobal((int)(nm * 64));
    try {
      int rc = QueryDisplayConfig(2u, ref np, paths, ref nm, modes, IntPtr.Zero);
      if (rc != 0) throw new Exception("QueryDisplayConfig rc=" + rc);
      // first active path; source LUID at offset 0, source id at offset 8
      return new int[] { Marshal.ReadInt32(paths, 0), Marshal.ReadInt32(paths, 4), Marshal.ReadInt32(paths, 8) };
    } finally { Marshal.FreeHGlobal(paths); Marshal.FreeHGlobal(modes); }
  }
  static IntPtr Header(int type, int size, int[] ids) {
    IntPtr pkt = Marshal.AllocHGlobal(size);
    for (int i = 0; i < size; i += 4) Marshal.WriteInt32(pkt, i, 0);
    Marshal.WriteInt32(pkt, 0, type);
    Marshal.WriteInt32(pkt, 4, size);
    Marshal.WriteInt32(pkt, 8, ids[0]);
    Marshal.WriteInt32(pkt, 12, ids[1]);
    Marshal.WriteInt32(pkt, 16, ids[2]);
    return pkt;
  }
  public static int[] GetScaleRel() { // [minRel, curRel, maxRel] vs recommended
    int[] ids = SourceIds();
    IntPtr pkt = Header(-3, 32, ids);
    try {
      int rc = DisplayConfigGetDeviceInfo(pkt);
      if (rc != 0) throw new Exception("DisplayConfigGetDeviceInfo rc=" + rc);
      return new int[] { Marshal.ReadInt32(pkt, 20), Marshal.ReadInt32(pkt, 24), Marshal.ReadInt32(pkt, 28) };
    } finally { Marshal.FreeHGlobal(pkt); }
  }
  public static int SetScaleRel(int rel) {
    int[] ids = SourceIds();
    IntPtr pkt = Header(-4, 24, ids);
    try {
      Marshal.WriteInt32(pkt, 20, rel);
      return DisplayConfigSetDeviceInfo(pkt);
    } finally { Marshal.FreeHGlobal(pkt); }
  }
}
'CS'@

function LogicalWidth {
  $w = & powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Width"
  return [int]$w
}

$targetScale = __SCALE__
$physW = 0
try {
  $vc = Get-CimInstance Win32_VideoController | Where-Object CurrentHorizontalResolution | Select-Object -First 1
  if ($vc) { $physW = [int]$vc.CurrentHorizontalResolution }
} catch {}
$curW = LogicalWidth
Log ('physical width=' + $physW + ' logical width=' + $curW + ' target scale=' + $targetScale)

$scaleOk = $false
if ($physW -le 0 -or $curW -le 0) {
  Log 'RESULT=FAIL cannot read resolution'
} else {
  $targetW = [int][math]::Round($physW * 100.0 / $targetScale)
  if ($curW -eq $targetW) {
    Log 'already at target scale'
    $scaleOk = $true
  } else {
    $ladder = @(100, 125, 150, 175, 200, 225, 250, 300, 350, 400, 450, 500)
    $curAbs = [int][math]::Round($physW * 100.0 / $curW)
    $rel = [DpiCfg]::GetScaleRel()
    Log ('scaleRel min=' + $rel[0] + ' cur=' + $rel[1] + ' max=' + $rel[2] + ' curAbs=' + $curAbs + '%')
    $iCur = $ladder.IndexOf($curAbs); $iTgt = $ladder.IndexOf($targetScale)
    $origRel = $rel[1]
    if ($iCur -lt 0 -or $iTgt -lt 0) {
      Log ('RESULT=FAIL scale not on ladder: cur=' + $curAbs + ' target=' + $targetScale)
    } else {
      $guess = $origRel + ($iTgt - $iCur)
      $cands = @($guess, ($guess - 1), ($guess + 1), ($guess - 2), ($guess + 2)) |
        Where-Object { $_ -ge $rel[0] -and $_ -le $rel[2] } | Select-Object -Unique
      foreach ($c in $cands) {
        $rc = [DpiCfg]::SetScaleRel([int]$c)
        Start-Sleep -Seconds 2
        $w = LogicalWidth
        Log ('SetScaleRel ' + $c + ' rc=' + $rc + ' -> logical width ' + $w)
        if ($w -eq $targetW) { $scaleOk = $true; break }
      }
      if (-not $scaleOk) {
        [void][DpiCfg]::SetScaleRel([int]$origRel)
        Log ('RESULT=FAIL target not reached; reverted to rel ' + $origRel)
      }
    }
  }
  if ($scaleOk) { Log ('RESULT=OK scale, logical width now ' + (LogicalWidth)) }
}

# --- app handling: kill only window-owning UI process (backend sidecar survives) ----
$doStart = __STARTAPP__
$doRestart = __RESTARTAPP__
if ($doRestart) {
  Get-Process | Where-Object {
    $_.Path -and $_.Path -like '*telegram-ai-desktop*' -and $_.MainWindowHandle -ne 0
  } | ForEach-Object {
    Log ('killing UI pid=' + $_.Id)
    Stop-Process -Id $_.Id -Force
  }
  Start-Sleep -Seconds 4
  $doStart = $true
}
if ($doStart) {
  $running = Get-Process | Where-Object {
    $_.Path -and $_.Path -like '*telegram-ai-desktop*' -and $_.MainWindowHandle -ne 0 }
  if ($running) {
    Log 'shell already running; not starting a second one'
  } else {
    $dir = Join-Path $env:LOCALAPPDATA 'Programs\telegram-ai-desktop'
    $exe = Get-ChildItem $dir -Filter *.exe -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -notlike 'Uninstall*' } |
      Sort-Object Length -Descending | Select-Object -First 1
    if ($exe) { Start-Process -FilePath $exe.FullName; Log ('started ' + $exe.Name + ' (' + $exe.Length + ' bytes)') }
    else { Log 'RESULT=FAIL shell exe not found' }
  }
  Start-Sleep -Seconds 25
}

# --- final probe + screenshot from a FRESH process --------------------------------
$shot = Join-Path $outDir 'seat_scale_after.png'
$pf = Join-Path $outDir '_seat_scale_probe.ps1'
@"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
`$scr=[System.Windows.Forms.Screen]::PrimaryScreen
Write-Output ('final bounds=' + `$scr.Bounds.ToString() + ' work=' + `$scr.WorkingArea.ToString())
`$b=`$scr.Bounds
`$bmp=New-Object System.Drawing.Bitmap `$b.Width,`$b.Height
`$g=[System.Drawing.Graphics]::FromImage(`$bmp)
`$g.CopyFromScreen(`$b.Location,[System.Drawing.Point]::Empty,`$b.Size)
`$bmp.Save('$shot',[System.Drawing.Imaging.ImageFormat]::Png)
`$g.Dispose();`$bmp.Dispose()
Write-Output 'screenshot ok'
"@ | Out-File -FilePath $pf -Encoding ascii
$res = & powershell -NoProfile -ExecutionPolicy Bypass -File $pf
foreach ($line in $res) { Log ('probe: ' + $line) }
Log 'DONE'
'@

# Add-Type here-string nesting: the inner C# block uses @'CS' ... 'CS'@ markers that
# PowerShell does not treat specially -- rewrite them into real here-string fences.
$remoteTemplate = $remoteTemplate.Replace("@'CS'", "@'").Replace("'CS'@", "'@")
$remote = $remoteTemplate.Replace('__SCALE__', "$Scale")
$remote = $remote.Replace('__STARTAPP__', ($(if ($StartApp) { '$true' } else { '$false' })))
$remote = $remote.Replace('__RESTARTAPP__', ($(if ($RestartApp) { '$true' } else { '$false' })))

function Say($m) { Write-Output ("[set_seat_scale] " + $m) }

$gen = Join-Path $env:TEMP ('seat_scale_' + $TargetSsh + '.ps1')
$remote | Out-File -FilePath $gen -Encoding ascii

# remote paths: resolve the seat's %USERPROFILE% first (user differs per node)
$rprofile = (ssh -o ConnectTimeout=6 $TargetSsh "echo %USERPROFILE%" 2>$null | Select-Object -First 1)
$rprofile = ("" + $rprofile).Trim()
if (-not $rprofile -or $rprofile -notlike '?:\*') { Say ("cannot resolve remote USERPROFILE (got '" + $rprofile + "')"); exit 3 }
$rdirWin = $rprofile + '\Downloads\chatx'
$rdirScp = $rdirWin.Replace('\', '/')
ssh -o ConnectTimeout=6 $TargetSsh ('cmd /c if not exist "' + $rdirWin + '" mkdir "' + $rdirWin + '"') 2>$null | Out-Null

scp -o ConnectTimeout=6 $gen ($TargetSsh + ':' + $rdirScp + '/_seat_scale.ps1') | Out-Null
if ($LASTEXITCODE -ne 0) { Say 'scp failed'; exit 3 }

# no-arg bat wrapper (schtasks /TR quoting across ssh+cmd is a proven trap)
$bat = $rdirWin + '\_seat_scale.bat'
ssh $TargetSsh ('cmd /c echo powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File ' + $rdirWin + '\_seat_scale.ps1 > "' + $bat + '"') 2>$null | Out-Null
ssh $TargetSsh ('cmd /c schtasks /Delete /TN ChatXSeatScale /F >nul 2>&1') 2>$null | Out-Null
ssh $TargetSsh ('cmd /c schtasks /Create /TN ChatXSeatScale /SC ONCE /ST 23:59 /F /IT /RL LIMITED /TR "' + $bat + '"') 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Say 'schtasks create failed'; exit 3 }
ssh $TargetSsh ('cmd /c schtasks /Run /TN ChatXSeatScale') 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Say 'schtasks run failed'; exit 3 }
Say ('remote task started on ' + $TargetSsh + ' (scale ' + $Scale + '%); polling log...')

$logPath = $rdirWin + '\seat_scale.log'
$deadline = (Get-Date).AddSeconds($TimeoutSec)
$logText = ''
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Seconds 5
  $logText = (ssh -o ConnectTimeout=6 $TargetSsh ('cmd /c type "' + $logPath + '" 2>nul') 2>$null) -join "`n"
  if ($logText -match 'DONE') { break }
}
ssh $TargetSsh ('cmd /c schtasks /Delete /TN ChatXSeatScale /F >nul 2>&1') 2>$null | Out-Null
if (-not ($logText -match 'DONE')) { Say 'timeout waiting for remote log'; if ($logText) { Write-Output $logText }; exit 4 }
Write-Output $logText
if ($logText -match 'RESULT=OK scale') { Say 'scale verified'; exit 0 }
Say 'scale NOT changed (reverted); see log above'
exit 2
