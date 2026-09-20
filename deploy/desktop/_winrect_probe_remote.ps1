# _winrect_probe_remote.ps1 -- runs INSIDE the seat's interactive session via a
# one-shot /IT scheduled task (SSH sessions cannot see session-1 windows).
# Outputs the ChatX shell main-window rect + IsZoomed to Downloads\chatx\winrect.log.
# ASCII only. Drill evidence tool for the 2026-08-18 win-fit verification; reusable.
$ErrorActionPreference = 'Continue'
$out = Join-Path $env:USERPROFILE 'Downloads\chatx\winrect.log'
Set-Content -Path $out -Value ('start ' + (Get-Date -Format 'HH:mm:ss')) -Encoding ascii

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public struct RECT { public int L; public int T; public int R; public int B; }
public class W {
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool IsZoomed(IntPtr h);
}
'@

$p = Get-Process | Where-Object {
  $_.Path -and $_.Path -like '*telegram-ai-desktop*' -and $_.MainWindowHandle -ne 0
} | Select-Object -First 1
if (-not $p) {
  Add-Content -Path $out -Value 'RESULT=FAIL no shell window' -Encoding ascii
  Add-Content -Path $out -Value 'DONE' -Encoding ascii
  exit 0
}
$r = New-Object RECT
[void][W]::GetWindowRect($p.MainWindowHandle, [ref]$r)
$z = [W]::IsZoomed($p.MainWindowHandle)
Add-Content -Path $out -Value ('pid=' + $p.Id + ' rect=' + $r.L + ',' + $r.T + ',' + $r.R + ',' + $r.B + ' w=' + ($r.R - $r.L) + ' h=' + ($r.B - $r.T) + ' zoomed=' + $z) -Encoding ascii
Add-Content -Path $out -Value 'RESULT=OK' -Encoding ascii
Add-Content -Path $out -Value 'DONE' -Encoding ascii
