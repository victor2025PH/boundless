# Finish workdirs + wallpapers after keys/config already installed
$ErrorActionPreference = 'Continue'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Machines = (Get-Content (Join-Path $Root 'deploy\machines.json') -Raw -Encoding UTF8 | ConvertFrom-Json).machines
$LocalIp = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like '192.168.*' } | Select-Object -First 1).IPAddress

function Push-Tool([string]$Alias, [string]$LocalScript) {
  scp -o BatchMode=yes -o ConnectTimeout=12 $LocalScript "${Alias}:C:/Users/Public/_boundless_tool.ps1" 2>&1 | Out-Null
}

foreach ($m in $Machines) {
  $alias = [string]$m.ssh[0]
  if ($m.ip -eq $LocalIp) { continue }
  Write-Host ("===== {0} {1} =====" -f $m.zh, $alias) -ForegroundColor Green

  # ensure Pictures dir
  ssh -o BatchMode=yes -o ConnectTimeout=8 $alias "powershell -NoProfile -Command `"New-Item -ItemType Directory -Force -Path C:\Users\Public\Pictures | Out-Null; Write-Output OK`"" | Out-Null

  # workdir
  Push-Tool $alias (Join-Path $PSScriptRoot 'remote_ensure_workdir.ps1')
  $prod = ([string]::Join('/', @($m.products)))
  # pass zh via env to avoid quoting issues: write a tiny args file
  $argsFile = Join-Path $env:TEMP 'bw_args.txt'
  @(
    $m.zh
    $m.role
    $prod
    $alias
  ) | Set-Content $argsFile -Encoding UTF8
  scp -o BatchMode=yes $argsFile "${alias}:C:/Users/Public/bw_args.txt" 2>&1 | Out-Null
  $runner = @'
$a = Get-Content C:\Users\Public\bw_args.txt -Encoding UTF8
& C:\Users\Public\_boundless_tool.ps1 -ZhName $a[0] -Role $a[1] -Products $a[2] -SshAlias $a[3] -AllowClone
'@
  $runnerPath = Join-Path $env:TEMP 'bw_run.ps1'
  $utf8BOM = New-Object System.Text.UTF8Encoding $true
  [IO.File]::WriteAllText($runnerPath, $runner, $utf8BOM)
  scp -o BatchMode=yes $runnerPath "${alias}:C:/Users/Public/bw_run.ps1" 2>&1 | Out-Null
  ssh -o BatchMode=yes -o ConnectTimeout=180 $alias "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\Public\bw_run.ps1" 2>&1 | Select-Object -Last 6

  # wallpaper
  $rel = ([string]$m.wallpaper) -replace '/', '\'
  $localWp = Join-Path $Root $rel
  $name = "boundless-wallpaper-$($m.id).png"
  scp -o BatchMode=yes $localWp "${alias}:C:/Users/Public/Pictures/$name" 2>&1 | Out-Null
  Push-Tool $alias (Join-Path $PSScriptRoot 'remote_set_wallpaper.ps1')
  ssh -o BatchMode=yes -o ConnectTimeout=15 $alias "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\Public\_boundless_tool.ps1 -WallpaperPath C:\Users\Public\Pictures\$name" 2>&1 | Select-Object -Last 3
}

Write-Host DONE
