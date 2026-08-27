# _seat_disp_probe.ps1 -- display facts for the fleet ledger, headless-safe.
# Two output shapes (consumers: chatx_fleet_status.ps1 + push_chatx.ps1 4.6):
#   "EXACT <logicalW>x<logicalH>@<scalePct>"      e.g. "EXACT 1920x1080@200"
#     Source: %APPDATA%\<app>\display-metrics.json breadcrumb written by the
#     ChatX shell (desktop/win-fit.js, ships with >= 1.0.39). This is the live
#     Chromium truth (screen.getPrimaryDisplay), no registry guessing. Only
#     trusted when its physical WxH still matches the current GPU mode --
#     a stale breadcrumb from an unplugged monitor must not win.
#   "<physW>x<physH>@<appliedDpi>#<overrideSteps>" e.g. "3840x2160@288#-3"
#     Legacy fallback for seats on older shells:
#     physWxphysH   = primary GPU mode (CIM, machine-global, works over SSH)
#     appliedDpi    = HKCU WindowMetrics AppliedDPI (LOGON-TIME dpi snapshot)
#     overrideSteps = HKCU PerMonitorSettings DpiValue (live per-monitor
#                     override in ladder steps relative to the recommended rung)
#     The probe reports FACTS ONLY; consumers derive scale candidates. Why no
#     exact live scale here: every session-scoped API lies over SSH (WinForms
#     sees a phantom 1024x768 WinDisc; QueryDisplayConfig returns 0 active
#     paths -- measured 2026-08-17 on .173), and AppliedDPI alone misses live
#     overrides applied since logon.
# ASCII only. Single-monitor assumption matches the seat fleet.
$ErrorActionPreference = 'SilentlyContinue'
$v = Get-CimInstance Win32_VideoController | Where-Object CurrentHorizontalResolution | Select-Object -First 1
if (-not $v) { exit 0 }
$pw = [int]$v.CurrentHorizontalResolution
$ph = [int]$v.CurrentVerticalResolution

# --- preferred: shell-written breadcrumb (exact, but validate freshness) -----
$bc = Get-ChildItem "$env:APPDATA\*\display-metrics.json" -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($bc) {
  try {
    $j = Get-Content $bc.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($j.physW -and $j.physH -and $j.logicalW -and $j.logicalH -and $j.scalePct -and
        [int]$j.physW -eq $pw -and [int]$j.physH -eq $ph) {
      Write-Output ('EXACT ' + [string][int]$j.logicalW + 'x' + [string][int]$j.logicalH + '@' + [string][int]$j.scalePct)
      exit 0
    }
  } catch { }
}

# --- fallback: registry facts (consumer derives candidates) ------------------
$dpi = (Get-ItemProperty 'HKCU:\Control Panel\Desktop\WindowMetrics' -ErrorAction SilentlyContinue).AppliedDPI
if (-not $dpi) { $dpi = 96 }
$ovr = 0
$pm = Get-ChildItem 'HKCU:\Control Panel\Desktop\PerMonitorSettings' -ErrorAction SilentlyContinue |
  Select-Object -First 1
if ($pm) {
  $dv = (Get-ItemProperty $pm.PSPath -ErrorAction SilentlyContinue).DpiValue
  if ($null -ne $dv) {
    # REG_DWORD may surface as Int32 (already signed) or UInt32 -- int64 covers both
    $o = [int64]$dv
    if ($o -gt 2147483647) { $o = $o - 4294967296 }
    $ovr = [int]$o
  }
}
Write-Output ([string]$pw + 'x' + [string]$ph + '@' + [string]$dpi + '#' + [string]$ovr)
