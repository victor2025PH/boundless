# publish_chatx.ps1 - one-command atomic publish of a ChatX desktop build to bd2026.cc.
#
# Codifies the release flow that was done by hand for 1.0.6 (and is easy to get wrong):
#   1. locate the build in desktop/dist (exe + blockmap + latest.yml)
#   2. VALIDATE before touching anything: exe/blockmap exist; version is proper semver (x.y.z, no
#      more "1.001"); latest.yml version/size/sha512 all match the actual exe
#   3. stage into website/public/downloads + regenerate manifest.json
#   4. ATOMIC upload order: push exe+blockmap FIRST, verify sha512 on the VPS equals local, and only
#      THEN push the latest.yml/manifest.json pointers (never leave a pointer aimed at a missing exe)
#   4.5 R2 mirror sync (download-speed P0, 2026-08-08): the site's /dl/<path> router prefers the
#      Cloudflare R2 mirror (dl.bd2026.cc) and falls back to the VPS per-file; keep the mirror in
#      lockstep so the big exe serves from the edge instead of the 15Mbps VPS pipe. Missing
#      rclone/credentials only means slower downloads, so warn instead of failing the publish.
#   5. pm2 restart (Next.js rebuilds its public/ static index) + public verification
#   6. hygiene: keep the newest -Keep versions, delete older exe/blockmap locally, on the VPS AND on R2
#
# -DryRun does every read-only check and PRINTS what it would upload/restart/delete, touching nothing
# remote. Run it first, always.
#
#   pwsh website/scripts/publish_chatx.ps1 -DryRun            # verify the build is publishable
#   pwsh website/scripts/publish_chatx.ps1                    # publish (asks to confirm)

param(
    [string]$Version    = "",                                   # default: read from dist/latest.yml
    [string]$DistDir    = "D:\boundless\engines\chengjie\desktop\dist",
    [int]   $Keep       = 3,
    [switch]$DryRun,
    [switch]$Yes,
    # Release announcement (P0 2026-08-14): every publish also prepends a "release-v<ver>"
    # entry to downloads/announcements.json, which every >=1.0.27 client polls and shows as
    # an in-app banner. -Notes inline text, or -NotesFile path to a text file (UTF-8);
    # neither given -> generic copy. -NoAnnouncement skips the feed update entirely.
    [string]$Notes      = "",
    [string]$NotesFile  = "",
    [switch]$NoAnnouncement,
    [string]$Key        = "$HOME\.ssh\hualing_deploy",
    [string]$Vps        = "ubuntu@165.154.233.121",
    [string]$RemoteDir  = "/home/ubuntu/yuntech/public/downloads",
    [string]$SiteUrl    = "https://bd2026.cc"
)
$ErrorActionPreference = "Stop"
$DownloadsDir = Join-Path (Split-Path -Parent $PSScriptRoot) "public\downloads"
$GenManifest  = Join-Path $PSScriptRoot "gen-chatx-manifest.ps1"

# R2 mirror tooling (aligned across build hosts, 2026-08-10):
#   conf:   env RCLONE_R2_CONF  ->  176 avatarhub secrets  ->  monorepo deploy\secrets (117)
#   rclone: PATH  ->  C:\tools\rclone\rclone.exe (117 drop location)
$R2Conf = $env:RCLONE_R2_CONF
if (-not $R2Conf) {
    $R2Conf = @(
        'C:\模仿音色\secrets\deploy\rclone_r2.conf',
        (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'deploy\secrets\rclone_r2.conf')
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}
$RcloneExe = (Get-Command rclone -ErrorAction SilentlyContinue).Source
if (-not $RcloneExe -and (Test-Path 'C:\tools\rclone\rclone.exe')) { $RcloneExe = 'C:\tools\rclone\rclone.exe' }
$R2Ready = [bool]($RcloneExe -and $R2Conf)

function Sha512B64([string]$path) {
    $sha = [System.Security.Cryptography.SHA512]::Create()
    $fs = [System.IO.File]::OpenRead($path)
    try { return [Convert]::ToBase64String($sha.ComputeHash($fs)) } finally { $fs.Close() }
}
function Fail([string]$m) { Write-Host "[FAIL] $m" -ForegroundColor Red; exit 1 }
function Info([string]$m) { Write-Host "[..] $m" }
function Ok([string]$m)   { Write-Host "[OK] $m" -ForegroundColor Green }

# -- 1. locate + parse dist/latest.yml (authoritative for version/size/sha) ------
$distYml = Join-Path $DistDir "latest.yml"
if (-not (Test-Path $distYml)) { Fail "no latest.yml in $DistDir (did the build run?)" }
$yml = Get-Content $distYml -Raw
$verY  = if ($yml -match "(?m)^version:\s*(.+?)\s*$") { $Matches[1].Trim() } else { "" }
$exeY  = if ($yml -match "(?m)^\s*-?\s*url:\s*(.+?)\s*$") { $Matches[1].Trim() } else { "" }
$sizeY = if ($yml -match "(?m)^\s*size:\s*(\d+)\s*$") { [long]$Matches[1] } else { 0 }
$shaY  = if ($yml -match "(?m)^\s*sha512:\s*(.+?)\s*$") { $Matches[1].Trim() } else { "" }
if (-not $Version) { $Version = $verY }
if (-not $Version) { Fail "cannot determine version (empty -Version and no version in latest.yml)" }

# -- 2. validate BEFORE touching anything ---------------------------------------
if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    Fail "version '$Version' is not semver x.y.z (no leading-zero forms like 1.001 - electron-updater compares semver)"
}
if ($verY -and $verY -ne $Version) { Fail "version mismatch: -Version=$Version vs latest.yml=$verY" }
$exe = Join-Path $DistDir "ChatX-Setup-$Version.exe"
$bmap = "$exe.blockmap"
if (-not (Test-Path $exe))  { Fail "missing exe: $exe" }
if (-not (Test-Path $bmap)) { Fail "missing blockmap: $bmap" }
Info "hashing $exe ..."
$shaLocal = Sha512B64 $exe
$sizeLocal = (Get-Item $exe).Length
if ($shaY -and $shaY -ne $shaLocal) { Fail "latest.yml sha512 != actual exe sha512 (stale/mismatched build)" }
if ($sizeY -and $sizeY -ne $sizeLocal) { Fail "latest.yml size ($sizeY) != actual exe size ($sizeLocal)" }
Ok "build valid: v$Version  size=$sizeLocal  sha512 matches latest.yml"

if (-not $Yes -and -not $DryRun) {
    $ans = Read-Host "Publish v$Version to $SiteUrl (restart site, keep newest $Keep)? type YES"
    if ($ans -ne "YES") { Write-Host "aborted."; exit 0 }
}

# -- 3. stage into website/public/downloads + regenerate manifest ---------------
if ($DryRun) {
    Info "DRYRUN would copy exe+blockmap+latest.yml -> $DownloadsDir, regenerate manifest.json"
} else {
    New-Item -ItemType Directory -Force -Path $DownloadsDir | Out-Null
    Copy-Item $exe, $bmap, $distYml $DownloadsDir -Force
    & powershell -ExecutionPolicy Bypass -File $GenManifest -Exe (Join-Path $DownloadsDir "ChatX-Setup-$Version.exe") | Out-Null
    Ok "staged + manifest.json regenerated"
}

# -- 3.5 release announcement feed (announcements.json) --------------------------
# Source of truth = the currently public feed (fetched fresh) so multiple build hosts
# never clobber each other's entries; this publish's entry is replaced idempotently.
# Feed contract (consumed by desktop update-notify.js): { items: [ { id, type,
# title, body, link, published_at, target:{minVersion,maxVersion} } ] }.
$AnnLocal = Join-Path $DownloadsDir "announcements.json"
$PublishAnnouncement = -not $NoAnnouncement
if ($PublishAnnouncement) {
    $notesText = $Notes
    if (-not $notesText -and $NotesFile) {
        if (-not (Test-Path $NotesFile)) { Fail "-NotesFile not found: $NotesFile" }
        $notesText = (Get-Content $NotesFile -Raw -Encoding UTF8).Trim()
    }
    if (-not $notesText) { $notesText = "包含稳定性与体验改进，建议尽快更新。" }
    $existingItems = @()
    $minSupported = ""   # top-level forced-upgrade line: MUST survive the merge (set via push_announcement.ps1)
    try {
        $feed = (& curl.exe -s -m 15 "$SiteUrl/downloads/announcements.json" | Out-String).Trim()
        if ($feed -and $feed.StartsWith("{")) {
            $parsed = $feed | ConvertFrom-Json
            if ($parsed.items) { $existingItems = @($parsed.items | Where-Object { $_.id -ne "release-v$Version" }) }
            if ($parsed.min_supported_version) { $minSupported = [string]$parsed.min_supported_version }
        }
    } catch { Info "no existing public announcements.json (first publish or fetch failed) - starting fresh" }
    $entry = [ordered]@{
        id           = "release-v$Version"
        type         = "release"
        title        = "ChatX v$Version 已发布"
        body         = $notesText
        link         = "$SiteUrl/download/chatx"
        published_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        # only nudge clients still below this version; the just-updated ones shouldn't see it
        target       = [ordered]@{ minVersion = ""; maxVersion = "" }
    }
    $items = @(@($entry) + $existingItems | Select-Object -First 20)   # feed stays small forever
    if ($DryRun) {
        Info "DRYRUN would stage announcements.json (release-v$Version + $($existingItems.Count) kept entries; min_supported_version='$minSupported')"
    } else {
        $feedObj = [ordered]@{ items = $items }
        if ($minSupported) { $feedObj["min_supported_version"] = $minSupported }
        $json = $feedObj | ConvertTo-Json -Depth 6
        [IO.File]::WriteAllText($AnnLocal, $json, (New-Object Text.UTF8Encoding($false)))
        Ok "announcements.json staged (release-v$Version, $($items.Count) total entries$(if ($minSupported) { ", min_supported_version=$minSupported" }))"
    }
}

# -- 4. atomic upload: exe first, verify sha on VPS, THEN pointers ---------------
$scpBase = @("-i", $Key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new")
$sshBase = $scpBase
if ($DryRun) {
    Info "DRYRUN would scp exe+blockmap -> ${Vps}:$RemoteDir, verify sha512 on VPS, then scp latest.yml+manifest.json, then pm2 restart"
} else {
    Info "uploading exe+blockmap (large) ..."
    & scp @scpBase (Join-Path $DownloadsDir "ChatX-Setup-$Version.exe") (Join-Path $DownloadsDir "ChatX-Setup-$Version.exe.blockmap") "${Vps}:$RemoteDir/"
    if ($LASTEXITCODE -ne 0) { Fail "scp exe failed" }
    $vpsSha = (& ssh @sshBase $Vps "openssl dgst -sha512 -binary '$RemoteDir/ChatX-Setup-$Version.exe' | openssl base64 -A").Trim()
    if ($vpsSha -ne $shaLocal) { Fail "VPS sha512 mismatch after upload (corrupt transfer): got $vpsSha" }
    Ok "exe verified on VPS (sha512 matches)"
    $pointerFiles = @((Join-Path $DownloadsDir "latest.yml"), (Join-Path $DownloadsDir "manifest.json"))
    if ($PublishAnnouncement -and (Test-Path $AnnLocal)) { $pointerFiles += $AnnLocal }
    & scp @scpBase @pointerFiles "${Vps}:$RemoteDir/"
    if ($LASTEXITCODE -ne 0) { Fail "scp pointers failed" }
    & ssh @sshBase $Vps "pm2 restart yuntech --update-env >/dev/null 2>&1 && sleep 4 && echo restarted" | Out-Null
    Ok "pointers uploaded + pm2 restarted"
}

# -- 4.5 R2 mirror sync (download-speed P0, 2026-08-08) --------------------------
# /dl/<path> prefers the R2 mirror; keep it in lockstep so the big exe is served from
# Cloudflare edge instead of the 15Mbps VPS pipe. Soft-fail: /dl falls back to the VPS
# per-file, so a missed sync means slower downloads, never a 404.
if ($DryRun) {
    Info "DRYRUN would rclone copy exe+blockmap+latest.yml+manifest.json -> r2:avatarhub/downloads (conf=$R2Conf rclone=$RcloneExe)"
} elseif ($R2Ready) {
    Info "syncing to R2 mirror ..."
    & $RcloneExe --config $R2Conf copy $DownloadsDir "r2:avatarhub/downloads" `
        --include "ChatX-Setup-$Version.exe" --include "ChatX-Setup-$Version.exe.blockmap" `
        --include "latest.yml" --include "manifest.json" --include "announcements.json" `
        --transfers 4 --s3-chunk-size 32M --log-level ERROR
    if ($LASTEXITCODE -ne 0) { Write-Warning "R2 sync failed - /dl router will fall back to VPS; re-run: rclone --config $R2Conf copy $DownloadsDir r2:avatarhub/downloads" }
    else { Ok "R2 mirror synced (r2:avatarhub/downloads)" }
} else {
    Write-Warning "R2 sync skipped (rclone or rclone_r2.conf missing) - /dl router will fall back to VPS"
}

# -- 5. public verification -----------------------------------------------------
if (-not $DryRun) {
    $ly = (& curl.exe -s -m 15 "$SiteUrl/downloads/latest.yml" | Out-String)
    $lv = if ($ly -match "(?m)^version:\s*(.+?)\s*$") { $Matches[1].Trim() } else { "?" }
    $mv = ""; try { $mv = ([string]((& curl.exe -s -m 15 "$SiteUrl/downloads/manifest.json" | ConvertFrom-Json).version)).Trim() } catch {}
    $code = (& curl.exe -s -o NUL -w "%{http_code}" -m 20 -r 0-0 "$SiteUrl/downloads/ChatX-Setup-$Version.exe")
    if ($lv -ne $Version) { Fail "public latest.yml version=$lv != $Version" }
    if ($mv -ne $Version) { Fail "public manifest version=$mv != $Version" }
    if ($code -notin @("200","206")) { Fail "public exe not downloadable (HTTP $code)" }
    Ok "public verified: latest.yml=$lv manifest=$mv exe=HTTP$code"
    if ($PublishAnnouncement) {
        # soft check: a broken feed must not fail an otherwise-good release (clients fail-open)
        $annPub = ""
        try { $annPub = (& curl.exe -s -m 15 "$SiteUrl/downloads/announcements.json" | Out-String) } catch {}
        if ($annPub -match "release-v$([regex]::Escape($Version))") { Ok "public announcements.json carries release-v$Version" }
        else { Write-Warning "public announcements.json missing release-v$Version (clients just won't see the banner; re-upload announcements.json to fix)" }
    }
}

# -- 6. hygiene: keep newest -Keep versions, delete older (local + VPS) ----------
function ExeVersions([string[]]$names) {
    $vs = @()
    foreach ($n in $names) { if ($n -match 'ChatX-Setup-(\d+\.\d+\.\d+)\.exe$') { $vs += $Matches[1] } }
    $vs | Sort-Object -Unique { [version]$_ } -Descending
}
$localExes = @(Get-ChildItem $DownloadsDir -Filter "ChatX-Setup-*.exe" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name)
$allVers = @(ExeVersions $localExes)
$stale = @($allVers | Select-Object -Skip $Keep)
if ($stale.Count -eq 0) { Ok "hygiene: $($allVers.Count) version(s) on disk, within keep=$Keep, nothing to prune" }
else {
    foreach ($v in $stale) {
        if ($DryRun) { Info "DRYRUN would delete old version $v (local + VPS)" }
        else {
            Remove-Item (Join-Path $DownloadsDir "ChatX-Setup-$v.exe"), (Join-Path $DownloadsDir "ChatX-Setup-$v.exe.blockmap") -Force -ErrorAction SilentlyContinue
            & ssh @sshBase $Vps "rm -f '$RemoteDir/ChatX-Setup-$v.exe' '$RemoteDir/ChatX-Setup-$v.exe.blockmap'"
            if ($R2Ready) {
                & $RcloneExe --config $R2Conf delete "r2:avatarhub/downloads" `
                    --include "ChatX-Setup-$v.exe" --include "ChatX-Setup-$v.exe.blockmap" --log-level ERROR
            }
            Ok "pruned old version $v (local + VPS + R2)"
        }
    }
}
Write-Host ""
# Broadcast etiquette (boss flagged twice, 0827/0830): the Telegram group @hykjz is LINKED to
# channel @hykj7 — a channel post auto-forwards into the group. Broadcasting with target=both
# posts the SAME announcement into the group a second time. Always use target=channel.
Write-Host "[next] 广播只发频道（target=channel）：群 @hykjz 已链接频道自动转发，双发=群里重复刷屏（老板两次点名）。" -ForegroundColor Yellow
if ($DryRun) { Ok "DRYRUN complete - build v$Version is publishable; re-run without -DryRun to ship" }
else { Ok "published v$Version" }
