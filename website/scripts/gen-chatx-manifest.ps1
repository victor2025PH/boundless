# 生成 ChatX 下载清单 public/downloads/manifest.json（发版辅助，消灭手工编辑漂移）。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\gen-chatx-manifest.ps1 -Exe ..\path\to\ChatX-Setup-0.1.0.exe
#   （版本号从文件名 ChatX-Setup-<ver>.exe 解析；-Version 可显式覆盖）
#
# 产出字段与 ChatxDownloadSection/DownloadHub 的运行时消费契约一致：
#   version / filename / size_mb / sha256 / signed / released_at
# signed 用 Get-AuthenticodeSignature 实测（拿到代码签名证书后重打包，此处自动变 true，前端零改动）。
param(
  [Parameter(Mandatory = $true)][string]$Exe,
  [string]$Version = '',
  [string]$OutDir = ''
)
$ErrorActionPreference = 'Stop'

if (-not (Test-Path $Exe)) { throw "找不到安装包: $Exe" }
$exeItem = Get-Item $Exe

if (-not $Version) {
  if ($exeItem.Name -match 'ChatX-Setup-([\d\.]+)\.exe') { $Version = $Matches[1] }
  else { throw "无法从文件名解析版本号（期望 ChatX-Setup-<ver>.exe），请用 -Version 显式指定" }
}
if (-not $OutDir) { $OutDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'public\downloads' }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

Write-Host "[1/3] SHA-256 ..."
$sha = (Get-FileHash -Algorithm SHA256 -Path $exeItem.FullName).Hash.ToLower()
$sizeMb = [math]::Round($exeItem.Length / 1MB)

Write-Host "[2/3] 签名状态 ..."
$sig = Get-AuthenticodeSignature -FilePath $exeItem.FullName
$signed = $sig.Status -eq 'Valid'
if (-not $signed -and $sig.Status -ne 'NotSigned') {
  Write-Warning "签名存在但校验未通过（$($sig.Status)）——按未签名记录"
}

# 字段与既有 manifest.json 契约一致（size_bytes/size_mb/built_at），消费方零改动
$manifest = [ordered]@{
  version    = $Version
  filename   = $exeItem.Name
  size_bytes = $exeItem.Length
  size_mb    = "$sizeMb"
  sha256     = $sha
  built_at   = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ssK')
  signed     = $signed
}
$json = ($manifest | ConvertTo-Json -Depth 3) + "`n"
$outFile = Join-Path $OutDir 'manifest.json'
[IO.File]::WriteAllText($outFile, $json, [Text.UTF8Encoding]::new($false))

Write-Host "[3/3] 已写 $outFile"
Write-Host ($json)
Write-Host "下一步：把安装包复制到 $OutDir 后运行 scripts\deploy.ps1（差量上传步骤会自动送上服务器），"
Write-Host "或部署跳过源码时用 -SkipAssets:`$false 单独同步发布物。"
