# publish_chatx_hotpatch.ps1 -- upload a hotpatch zip+pointer to bd2026.cc/downloads.
#
# Same atomic order as publish_chatx.ps1: big file first, verify sha on the VPS,
# THEN flip the hotpatch.json pointer. Never leaves a pointer aimed at a missing zip.
# Does NOT touch latest.yml / ChatX-Setup-*.exe (full-release channel stays intact).
#
#   pwsh publish_chatx_hotpatch.ps1 -DryRun
#   pwsh publish_chatx_hotpatch.ps1 -Yes
#   pwsh publish_chatx_hotpatch.ps1 -Yes -Announce
#
# ASCII-only where it matters. Exit 0 ok / 1 fail
param(
  [string]$OutDir   = "D:\boundless\engines\chengjie\desktop\dist\hotpatch",
  [switch]$DryRun,
  [switch]$Yes,
  [switch]$Announce,
  [int]   $Keep     = 5,
  [string]$Key      = "$HOME\.ssh\hualing_deploy",
  [string]$Vps      = "ubuntu@165.154.233.121",
  [string]$RemoteDir = "/home/ubuntu/yuntech/public/downloads",
  [string]$SiteUrl  = "https://bd2026.cc"
)
$ErrorActionPreference = "Stop"
$DownloadsDir = Join-Path (Split-Path -Parent $PSScriptRoot) "public\downloads"

function Fail([string]$m) { Write-Host "[FAIL] $m" -ForegroundColor Red; exit 1 }
function Info([string]$m) { Write-Host "[..] $m" }
function Ok([string]$m)   { Write-Host "[OK] $m" -ForegroundColor Green }

$ptr = Join-Path $OutDir "hotpatch.json"
if (-not (Test-Path $ptr)) { Fail "no hotpatch.json in $OutDir (run make_chatx_hotpatch.ps1 first)" }
$mani = Get-Content -LiteralPath $ptr -Raw -Encoding UTF8 | ConvertFrom-Json
if ($mani.kind -ne "chatx_hotpatch") { Fail "pointer is not a chatx_hotpatch manifest" }
$zipName = [string]$mani.zip
$zipPath = Join-Path $OutDir $zipName
if (-not (Test-Path $zipPath)) { Fail "zip missing: $zipPath" }
$shaLocal = (Get-FileHash $zipPath -Algorithm SHA256).Hash.ToLower()
if ($mani.sha256 -and $mani.sha256.ToLower() -ne $shaLocal) { Fail "hotpatch.json sha256 != actual zip" }
$id = [string]$mani.id
Ok ("hotpatch valid: " + $id + "  " + [math]::Round((Get-Item $zipPath).Length / 1MB, 1) + " MB")

if (-not $Yes -and -not $DryRun) {
  $ans = Read-Host "Publish hotpatch $id to $SiteUrl/downloads ? type YES"
  if ($ans -ne "YES") { Write-Host "aborted."; exit 0 }
}

if ($DryRun) {
  Info "DRYRUN would copy $zipName + hotpatch.json -> $DownloadsDir, scp zip then pointer, pm2 restart"
} else {
  New-Item -ItemType Directory -Force -Path $DownloadsDir | Out-Null
  Copy-Item $zipPath, $ptr $DownloadsDir -Force
  Ok "staged into website/public/downloads"
}

$scpBase = @("-i", $Key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new")
if ($DryRun) {
  Info "DRYRUN would scp zip -> ${Vps}:$RemoteDir, verify sha256, then scp hotpatch.json"
} else {
  Info "uploading zip ..."
  & scp @scpBase (Join-Path $DownloadsDir $zipName) "${Vps}:$RemoteDir/"
  if ($LASTEXITCODE -ne 0) { Fail "scp zip failed" }
  $vpsSha = (& ssh @scpBase $Vps "openssl dgst -sha256 '$RemoteDir/$zipName'").Trim().ToLower()
  if ($vpsSha -match '([0-9a-f]{64})') { $vpsSha = $Matches[1] }
  if ($vpsSha -ne $shaLocal) { Fail "VPS sha256 mismatch after upload: got $vpsSha" }
  Ok "zip verified on VPS"
  & scp @scpBase (Join-Path $DownloadsDir "hotpatch.json") "${Vps}:$RemoteDir/"
  if ($LASTEXITCODE -ne 0) { Fail "scp pointer failed" }
  & ssh @scpBase $Vps "pm2 restart yuntech --update-env >/dev/null 2>&1 && sleep 4 && echo restarted" | Out-Null
  Ok "pointer uploaded + pm2 restarted"
}

$R2Conf = $env:RCLONE_R2_CONF
if (-not $R2Conf) {
  $R2Conf = @(
    'C:\模仿音色\secrets\deploy\rclone_r2.conf',
    (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'deploy\secrets\rclone_r2.conf')
  ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}
$RcloneExe = (Get-Command rclone -ErrorAction SilentlyContinue).Source
if (-not $RcloneExe -and (Test-Path 'C:\tools\rclone\rclone.exe')) { $RcloneExe = 'C:\tools\rclone\rclone.exe' }

if ($DryRun) {
  Info "DRYRUN would rclone copy zip+hotpatch.json -> r2:avatarhub/downloads"
} elseif ($RcloneExe -and $R2Conf) {
  Info "syncing to R2 mirror ..."
  & $RcloneExe --config $R2Conf copy $DownloadsDir "r2:avatarhub/downloads" `
    --include $zipName --include "hotpatch.json" --transfers 4 --s3-chunk-size 32M --log-level ERROR
  if ($LASTEXITCODE -ne 0) { Write-Warning "R2 sync failed - /dl will fall back to VPS" }
  else { Ok "R2 mirror synced" }
} else {
  Write-Warning "R2 sync skipped (rclone or conf missing)"
}

if (-not $DryRun) {
  $pub = ""
  try { $pub = (& curl.exe -s -m 15 "$SiteUrl/downloads/hotpatch.json" | Out-String) } catch {}
  if ($pub -notmatch [regex]::Escape($id)) { Fail "public hotpatch.json missing id $id" }
  $code = (& curl.exe -s -o NUL -w "%{http_code}" -m 20 -r 0-0 "$SiteUrl/downloads/$zipName")
  if ($code -notin @("200", "206")) { Fail "public zip not downloadable (HTTP $code)" }
  Ok "public verified: hotpatch.json=$id zip=HTTP$code"
}

$localZips = @(Get-ChildItem $DownloadsDir -Filter "ChatX-Hotpatch-*.zip" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)
$stale = @($localZips | Select-Object -Skip $Keep)
foreach ($old in $stale) {
  if ($DryRun) { Info ("DRYRUN would prune " + $old.Name) }
  else {
    Remove-Item $old.FullName -Force -ErrorAction SilentlyContinue
    & ssh @scpBase $Vps "rm -f '$RemoteDir/$($old.Name)'"
    Ok ("pruned old " + $old.Name)
  }
}

if ($Announce -and -not $DryRun) {
  $ann = Join-Path $PSScriptRoot "push_announcement.ps1"
  if (Test-Path $ann) {
    $title = "ChatX " + $mani.baseAppVersion + "+p" + $mani.patch + " 热更新"
    $body = [string]$mani.notes
    if ($mani.fixes -and @($mani.fixes).Count) { $body = ($mani.fixes -join ", ") + "  " + $body }
    & powershell -ExecutionPolicy Bypass -File $ann -Type notice -Title $title -Body $body -Yes
  }
}

Write-Host ""
if ($DryRun) { Ok "DRYRUN complete - hotpatch $id is publishable" }
else { Ok "published hotpatch $id" }
