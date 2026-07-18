param(
  [Parameter(Mandatory)][string]$ZhName,
  [string]$Role = 'dev',
  [string]$Products = '',
  [string]$SshAlias = '',
  [switch]$AllowClone
)
$ErrorActionPreference = 'Continue'
$rootDrive = if (Test-Path 'D:\') { 'D:\' } else { 'C:\' }
$work = Join-Path $rootDrive 'boundless'
$devRoot = Join-Path $rootDrive ([string]([char]0x5F00) + [char]0x53D1) # 开发
New-Item -ItemType Directory -Force -Path $devRoot | Out-Null
Write-Output ("ROOT=" + $rootDrive)

$cloned = 'no'
$git = $null
foreach ($c in @(
  'git',
  'C:\Program Files\Git\cmd\git.exe',
  'C:\Program Files\Git\bin\git.exe',
  'C:\Users\user\AppData\Local\Programs\Git\cmd\git.exe'
)) {
  if ($c -eq 'git') {
    $cmd = Get-Command git -ErrorAction SilentlyContinue
    if ($cmd) { $git = $cmd.Source; break }
  } elseif (Test-Path $c) { $git = $c; break }
}

if ($AllowClone -and -not (Test-Path (Join-Path $work '.git'))) {
  if ($git) {
    if (Test-Path $work) { Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue }
    & $git clone --depth 1 https://github.com/victor2025PH/boundless.git $work
    if (Test-Path (Join-Path $work '.git')) { $cloned = 'yes' } else { $cloned = 'fail' }
  } else { $cloned = 'no_git' }
} elseif (Test-Path (Join-Path $work '.git')) {
  if ($git) { Push-Location $work; & $git pull --rebase origin main 2>$null | Out-Null; Pop-Location }
  $cloned = 'exists'
}
if (-not (Test-Path $work)) { New-Item -ItemType Directory -Force -Path $work | Out-Null }

function Ensure-Junction([string]$link, [string]$target) {
  if (-not (Test-Path $target)) { New-Item -ItemType Directory -Force -Path $target | Out-Null }
  if (Test-Path $link) {
    $item = Get-Item $link -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { return 'exists' }
    Remove-Item $link -Force -Recurse -ErrorAction SilentlyContinue
  }
  cmd /c mklink /J "$link" "$target" | Out-Null
  return 'created'
}

$rel = 'engines\avatarhub'
if ($ZhName -match '通译|智聊') { $rel = 'engines\chengjie' }
elseif ($ZhName -match '智拓') { $rel = 'engines\huoke' }

$j = Ensure-Junction (Join-Path $devRoot $ZhName) (Join-Path $work $rel)
if ($ZhName -match '通译') {
  Ensure-Junction (Join-Path $devRoot '智聊') (Join-Path $work 'engines\chengjie') | Out-Null
}

$lines = @(
  ("machine=" + $ZhName),
  ("role=" + $Role),
  ("products=" + $Products),
  ("workdir=" + $work),
  ("ssh=" + $SshAlias),
  "rule=pull on start; push before leave"
)
$utf8 = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllLines((Join-Path $devRoot 'README.txt'), $lines, $utf8)
Write-Output ("WORKDIR_OK clone=$cloned junction=$j path=" + (Test-Path (Join-Path $devRoot $ZhName)) + " work=" + $work)
