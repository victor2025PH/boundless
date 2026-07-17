param([Parameter(Mandatory)][string]$WallpaperPath)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path $WallpaperPath)) { Write-Output "WP_MISSING $WallpaperPath"; exit 2 }
New-Item -ItemType Directory -Force -Path (Split-Path $WallpaperPath) | Out-Null
Set-ItemProperty -Path 'HKCU:\Control Panel\Desktop' -Name Wallpaper -Value $WallpaperPath
Set-ItemProperty -Path 'HKCU:\Control Panel\Desktop' -Name WallpaperStyle -Value 10 -ErrorAction SilentlyContinue
Set-ItemProperty -Path 'HKCU:\Control Panel\Desktop' -Name TileWallpaper -Value 0 -ErrorAction SilentlyContinue
$code = @'
using System.Runtime.InteropServices;
public class Wall {
  [DllImport("user32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
  public static extern bool SystemParametersInfo(int uAction, int uParam, string lpvParam, int fuWinIni);
}
'@
Add-Type -TypeDefinition $code -ErrorAction SilentlyContinue
[Wall]::SystemParametersInfo(20, 0, $WallpaperPath, 3) | Out-Null
rundll32.exe user32.dll,UpdatePerUserSystemParameters
Write-Output "WP_OK $WallpaperPath"
