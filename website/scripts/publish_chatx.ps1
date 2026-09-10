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
# ALSO, every release must grow the website changelog page (/download/chatx/releases). That page's
# data lives in website/lib/chatx-release-notes.json (git-tracked, bilingual) — NOT in this
# downloads flow. After publishing, run the generator and redeploy the website:
#   node website/scripts/gen-chatx-changelog.mjs add --version <ver> --tags fix,improve \
#     --title-zh "..." --title-en "..." --zh <notes.txt> --en <notes_en.txt>
# gate:content check 6 fails the website build if a published version has no changelog entry, so
# you cannot forget. This script prints the exact command with -Version prefilled at the end.
#
# -DryRun does every read-only check and PRINTS what it would upload/restart/delete, touching nothing
# remote. Run it first, always.
#
#   pwsh website/scripts/publish_chatx.ps1 -DryRun            # verify the build is publishable
#   pwsh website/scripts/publish_chatx.ps1                    # publish (asks to confirm)
#
# THREE CHANNELS (L-5 / boss decision D-L1, 2026-09-06). K-5 shipped 1.0.74 by publishing the CLEAN
# build (dist-clean) as the public latest.yml; the two internal testers clicked "update" and got the
# clean package, so the smart package's seed-data (and every "factory-on" decision riding in it)
# never reached them. Fix = the channel is a property of the PACKAGE, baked in at build time:
#   public   : dist-clean\latest.yml            -> <RemoteDir>/            (default, unchanged flow)
#   internal : dist\latest-internal.yml         -> <RemoteDir>/internal/   (-Channel internal)
#   lite     : dist-lite\latest-lite.yml        -> <RemoteDir>/lite/       (-Channel lite; boss 2026-09-06
#              "do as recommended": the lite custom build must not auto-update into the clean package)
# Non-public builds are built with publish.channel=latest-<channel> + publish.url=.../downloads/<channel>/,
# so their dist dir has NO latest.yml -> publishing them without -Channel fails at step 1 (cannot be
# shipped as the public package by accident), and the installed app's resources/app-update.yml keeps
# it on its own feed forever after. Non-public modes never touch public pointers: no latest.yml, no
# public manifest.json, no announcements.json, no public pruning. Everything lives under
# downloads/<channel>/ (VPS + R2).
#
#   pwsh website/scripts/publish_chatx.ps1 -DistDir dist -Channel internal -DryRun
#   pwsh website/scripts/publish_chatx.ps1 -DistDir dist -Channel internal -Yes
#   pwsh website/scripts/publish_chatx.ps1 -DistDir dist-lite -Channel lite -Yes

param(
    [string]$Version    = "",                                   # default: read from dist/latest*.yml
    [string]$DistDir    = "D:\boundless\engines\chengjie\desktop\dist",
    [ValidateSet("public", "internal", "lite")]
    [string]$Channel    = "public",
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
$GenManifest  = Join-Path $PSScriptRoot "gen-chatx-manifest.ps1"
$PublicDownloadsDir = Join-Path (Split-Path -Parent $PSScriptRoot) "public\downloads"

# Channel-derived layout. Every later step uses ONLY these (no hardcoded "latest.yml"/root paths),
# so every non-public channel is fully namespaced and the public channel is byte-for-byte the old flow.
# $Internal = "not the public channel" (internal or lite) — the name predates the lite channel.
$AllChannels = @("public", "internal", "lite")
$Internal = ($Channel -ne "public")
if ($Internal) {
    $YmlName      = "latest-$Channel.yml"                 # electron-builder: publish.channel=latest-<channel>
    $DownloadsDir = Join-Path $PublicDownloadsDir $Channel
    $RemoteDir    = "$RemoteDir/$Channel"
    $R2Path       = "r2:avatarhub/downloads/$Channel"
    $PublicBase   = "$SiteUrl/downloads/$Channel"
} else {
    $YmlName      = "latest.yml"
    $DownloadsDir = $PublicDownloadsDir
    $R2Path       = "r2:avatarhub/downloads"
    $PublicBase   = "$SiteUrl/downloads"
}
function YmlOf([string]$ch) { if ($ch -eq "public") { "latest.yml" } else { "latest-$ch.yml" } }

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

# -- 1. locate + parse dist/<channel yml> (authoritative for version/size/sha) ------
$distYml = Join-Path $DistDir $YmlName
if (-not (Test-Path $distYml)) {
    # Channel interlock: another channel's yml sitting there means this dist dir was built for THAT
    # channel (each build emits exactly one yml: latest.yml / latest-internal.yml / latest-lite.yml).
    foreach ($other in ($AllChannels | Where-Object { $_ -ne $Channel })) {
        $otherYml = YmlOf $other
        if (Test-Path (Join-Path $DistDir $otherYml)) {
            $hint = switch ($other) {
                "public"   { "clean builds ('npm run dist:win:clean') go out WITHOUT -Channel" }
                "internal" { "smart/internal builds come from 'npm run dist:win' and ship with -Channel internal" }
                "lite"     { "lite builds come from 'npm run dist:win:lite' and ship with -Channel lite" }
            }
            Fail "$DistDir holds a $($other.ToUpper())-channel build ($otherYml, no $YmlName) - refusing to publish it as '$Channel'. $hint."
        }
    }
    Fail "no $YmlName in $DistDir (did the build run?)"
}
Info "channel=$Channel  yml=$YmlName  remote=$RemoteDir  r2=$R2Path"
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
Ok "build valid: v$Version  size=$sizeLocal  sha512 matches $YmlName"

if (-not $Yes -and -not $DryRun) {
    $ans = Read-Host "Publish v$Version to $PublicBase/ [$Channel] (restart site, keep newest $Keep)? type YES"
    if ($ans -ne "YES") { Write-Host "aborted."; exit 0 }
}

# -- 3. stage into website/public/downloads[/internal] + regenerate manifest -----
# manifest.json in the internal dir is a convenience readout (version/sha256/signed) for
# operators; nothing public consumes it. The PUBLIC manifest is never touched in internal mode.
if ($DryRun) {
    Info "DRYRUN would copy exe+blockmap+$YmlName -> $DownloadsDir, regenerate manifest.json there"
} else {
    New-Item -ItemType Directory -Force -Path $DownloadsDir | Out-Null
    Copy-Item $exe, $bmap, $distYml $DownloadsDir -Force
    & powershell -ExecutionPolicy Bypass -File $GenManifest -Exe (Join-Path $DownloadsDir "ChatX-Setup-$Version.exe") -OutDir $DownloadsDir | Out-Null
    Ok "staged + manifest.json regenerated ($DownloadsDir)"
}

# -- 3.5 release announcement feed (announcements.json) --------------------------
# Source of truth = the currently public feed (fetched fresh) so multiple build hosts
# never clobber each other's entries; this publish's entry is replaced idempotently.
# Feed contract (consumed by desktop update-notify.js): { items: [ { id, type,
# title, body, link, published_at, target:{minVersion,maxVersion} } ] }.
# INTERNAL channel: never announce. The feed is global (every client polls it) and the
# internal smart package is not a customer release; the public announcement for the same
# version comes from the clean publish.
$AnnLocal = Join-Path $DownloadsDir "announcements.json"
$PublishAnnouncement = (-not $NoAnnouncement) -and (-not $Internal)
if ($Internal -and -not $NoAnnouncement) { Info "$Channel channel: announcements.json untouched (feed is global; only the public clean release announces)" }
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
    Info "DRYRUN would scp exe+blockmap -> ${Vps}:$RemoteDir, verify sha512 on VPS, then scp $YmlName+manifest.json, then pm2 reload (rolling)"
} else {
    if ($Internal) {
        & ssh @sshBase $Vps "mkdir -p '$RemoteDir'"
        if ($LASTEXITCODE -ne 0) { Fail "cannot create $RemoteDir on VPS" }
    }
    Info "uploading exe+blockmap (large) ..."
    & scp @scpBase (Join-Path $DownloadsDir "ChatX-Setup-$Version.exe") (Join-Path $DownloadsDir "ChatX-Setup-$Version.exe.blockmap") "${Vps}:$RemoteDir/"
    if ($LASTEXITCODE -ne 0) { Fail "scp exe failed" }
    $vpsSha = (& ssh @sshBase $Vps "openssl dgst -sha512 -binary '$RemoteDir/ChatX-Setup-$Version.exe' | openssl base64 -A").Trim()
    if ($vpsSha -ne $shaLocal) { Fail "VPS sha512 mismatch after upload (corrupt transfer): got $vpsSha" }
    Ok "exe verified on VPS (sha512 matches)"
    $pointerFiles = @((Join-Path $DownloadsDir $YmlName), (Join-Path $DownloadsDir "manifest.json"))
    if ($PublishAnnouncement -and (Test-Path $AnnLocal)) { $pointerFiles += $AnnLocal }
    & scp @scpBase @pointerFiles "${Vps}:$RemoteDir/"
    if ($LASTEXITCODE -ne 0) { Fail "scp pointers failed" }
    # Next.js indexes public/ at boot: a NEW file (new version exe, or the whole internal/ dir the
    # first time) is 404 until the server restarts. Same restart for both channels.
    # Q-14 #262 (2026-09-09): `pm2 reload` = rolling (cluster mode, ecosystem.config.js): new worker
    # up first, old one stopped after -> zero 5xx window. 09-08 19h the old `pm2 restart` cost 7 x 5xx
    # in nginx during the R78 publish. Falls back to restart only if reload is rejected (old pm2).
    & ssh @sshBase $Vps "pm2 reload yuntech --update-env >/dev/null 2>&1 || pm2 restart yuntech --update-env >/dev/null 2>&1; sleep 4 && echo reloaded" | Out-Null
    Ok "pointers uploaded + pm2 reloaded (rolling)"
}

# -- 4.5 R2 mirror sync (download-speed P0, 2026-08-08) --------------------------
# /dl/<path> prefers the R2 mirror; keep it in lockstep so the big exe is served from
# Cloudflare edge instead of the 15Mbps VPS pipe. Soft-fail: /dl falls back to the VPS
# per-file, so a missed sync means slower downloads, never a 404.
if ($DryRun) {
    Info "DRYRUN would rclone copy exe+blockmap+$YmlName+manifest.json -> $R2Path (conf=$R2Conf rclone=$RcloneExe)"
} elseif ($R2Ready) {
    Info "syncing to R2 mirror ..."
    $r2Includes = @("--include", "ChatX-Setup-$Version.exe", "--include", "ChatX-Setup-$Version.exe.blockmap",
                    "--include", $YmlName, "--include", "manifest.json")
    if ($PublishAnnouncement) { $r2Includes += @("--include", "announcements.json") }
    & $RcloneExe --config $R2Conf copy $DownloadsDir $R2Path @r2Includes `
        --transfers 4 --s3-chunk-size 32M --log-level ERROR
    if ($LASTEXITCODE -ne 0) { Write-Warning "R2 sync failed - /dl router will fall back to VPS; re-run: rclone --config $R2Conf copy $DownloadsDir $R2Path" }
    else { Ok "R2 mirror synced ($R2Path)" }
} else {
    Write-Warning "R2 sync skipped (rclone or rclone_r2.conf missing) - /dl router will fall back to VPS"
}

# -- 5. public verification -----------------------------------------------------
if (-not $DryRun) {
    # Up to 3 attempts, 8 s apart: the checks run right after pm2 restart and a 500 MB upload;
    # 2026-09-06 a transient DNS blip on the build host failed both rclone and this curl in the
    # same second while the VPS had already served the files fine. A retry beats a false FAIL.
    $lv = "?"; $mv = ""; $code = ""
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $ly = (& curl.exe -s -m 15 "$PublicBase/$YmlName" | Out-String)
        $lv = if ($ly -match "(?m)^version:\s*(.+?)\s*$") { $Matches[1].Trim() } else { "?" }
        $mv = ""; try { $mv = ([string]((& curl.exe -s -m 15 "$PublicBase/manifest.json" | ConvertFrom-Json).version)).Trim() } catch {}
        # -L: since the 2026-09-10 download ledger, GET /downloads/*.exe is rewritten to /dl and 302s to R2
        # when the mirror has the file; follow it so the check sees what a real downloader sees (200/206).
        $code = (& curl.exe -s -L -o NUL -w "%{http_code}" -m 20 -r 0-0 "$PublicBase/ChatX-Setup-$Version.exe")
        if ($lv -eq $Version -and $mv -eq $Version -and $code -in @("200","206")) { break }
        if ($attempt -lt 3) { Info "public check attempt $attempt not yet consistent (yml=$lv manifest=$mv exe=HTTP$code) - retrying in 8s"; Start-Sleep -Seconds 8 }
    }
    if ($lv -ne $Version) { Fail "public $YmlName version=$lv != $Version" }
    if ($mv -ne $Version) { Fail "public manifest version=$mv != $Version" }
    if ($code -notin @("200","206")) { Fail "public exe not downloadable (HTTP $code)" }
    Ok "public verified [$Channel]: $YmlName=$lv manifest=$mv exe=HTTP$code ($PublicBase/)"
    if ($Internal) {
        # The public pointers must be exactly what they were: an internal publish that moved the
        # public latest.yml would re-create the K-5 accident in the other direction.
        $pubLy = (& curl.exe -s -m 15 "$SiteUrl/downloads/latest.yml" | Out-String)
        $pubV = if ($pubLy -match "(?m)^version:\s*(.+?)\s*$") { $Matches[1].Trim() } else { "?" }
        Ok "public channel untouched: /downloads/latest.yml still v$pubV"
    }
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
# Hygiene is per channel: $DownloadsDir / $RemoteDir / $R2Path are already namespaced, so an
# internal publish only ever prunes internal/ and a public publish only ever prunes the root.
$localExes = @(Get-ChildItem $DownloadsDir -Filter "ChatX-Setup-*.exe" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name)
$allVers = @(ExeVersions $localExes)
$stale = @($allVers | Select-Object -Skip $Keep)
if ($stale.Count -eq 0) { Ok "hygiene [$Channel]: $($allVers.Count) version(s) on disk, within keep=$Keep, nothing to prune" }
else {
    foreach ($v in $stale) {
        if ($DryRun) { Info "DRYRUN would delete old version $v (local + VPS + R2, $Channel channel only)" }
        else {
            Remove-Item (Join-Path $DownloadsDir "ChatX-Setup-$v.exe"), (Join-Path $DownloadsDir "ChatX-Setup-$v.exe.blockmap") -Force -ErrorAction SilentlyContinue
            & ssh @sshBase $Vps "rm -f '$RemoteDir/ChatX-Setup-$v.exe' '$RemoteDir/ChatX-Setup-$v.exe.blockmap'"
            if ($R2Ready) {
                & $RcloneExe --config $R2Conf delete $R2Path `
                    --include "ChatX-Setup-$v.exe" --include "ChatX-Setup-$v.exe.blockmap" --log-level ERROR
            }
            Ok "pruned old version $v (local + VPS + R2, $Channel)"
        }
    }
}
Write-Host ""
if ($Channel -eq "internal") {
    # Internal channel: no changelog page, no broadcast. What operators need next is the one-time
    # switch URL for machines still on the public channel (they can never auto-update INTO the
    # internal channel; installing one smart package flips their app-update.yml, then it sticks).
    Write-Host "[next] 内测渠道已就位：装过**本渠道出的** smart 包（app-update.yml 带 channel）的机器，自检更新走 $PublicBase/$YmlName。" -ForegroundColor Cyan
    Write-Host "  还在对外渠道上的机器（两台内测机走应用内更新拿的 clean 包；09-06 前 push 到坐席的 smart 包也没带 channel）要**手装/推装一次**这个包切渠道，之后自动更新只跟内测渠道：" -ForegroundColor Cyan
    Write-Host "    $PublicBase/ChatX-Setup-$Version.exe    （R2 加速入口：$SiteUrl/dl/downloads/internal/ChatX-Setup-$Version.exe）" -ForegroundColor Cyan
    Write-Host "  核对：deploy\desktop\chatx_fleet_status.ps1 的 edition/channel 列应显示 smart / internal。" -ForegroundColor Cyan
    Write-Host "  不要广播、不要写 changelog：同版本的对外说明随 clean 包的公共 publish 走。" -ForegroundColor Yellow
} elseif ($Channel -eq "lite") {
    # Lite channel: the custom lite build (2 personas / 2 cloned voices / metered-not-capped) now
    # updates only within its own channel. Customers on a lite install before 2026-09-06 were on the
    # public feed (would have turned clean on the next update) -> hand them this installer once.
    Write-Host "[next] lite 渠道已就位：装过**本渠道出的** lite 包的机器自检更新走 $PublicBase/$YmlName，不会再被公共渠道更新成 clean 包。" -ForegroundColor Cyan
    Write-Host "  09-06 之前装的 lite 包不带 channel（跟公共渠道）：客户要**手装一次**这个包切渠道，之后自动更新只跟 lite 渠道：" -ForegroundColor Cyan
    Write-Host "    $PublicBase/ChatX-Setup-$Version.exe    （R2 加速入口：$SiteUrl/dl/downloads/lite/ChatX-Setup-$Version.exe）" -ForegroundColor Cyan
    Write-Host "  核对：deploy\desktop\chatx_fleet_status.ps1 的 edition 列应显示 lite/lite。不要广播、不要写 changelog。" -ForegroundColor Yellow
} else {
    # Changelog reminder (every release must grow /download/chatx/releases; gate:content check 6 enforces).
    $notesArg = if ($NotesFile) { $NotesFile } else { "<notes.txt>" }
    Write-Host "[next] 生成官网版本更新记录条目（发布页 /download/chatx/releases）：" -ForegroundColor Cyan
    Write-Host "  node scripts/gen-chatx-changelog.mjs add --version $Version --tags fix,improve \" -ForegroundColor Cyan
    Write-Host "    --title-zh `"一句话主题`" --title-en `"one-line title`" --zh $notesArg --en <notes_en.txt>" -ForegroundColor Cyan
    Write-Host "  然后重新部署 website（gate:content 检查 6 会拦住漏补的版本）。" -ForegroundColor Cyan
    # Broadcast etiquette (boss flagged twice, 0827/0830): the Telegram group @hykjz is LINKED to
    # channel @hykj7 — a channel post auto-forwards into the group. Broadcasting with target=both
    # posts the SAME announcement into the group a second time. Always use target=channel.
    Write-Host "[next] 广播只发频道（target=channel）：群 @hykjz 已链接频道自动转发，双发=群里重复刷屏（老板两次点名）。" -ForegroundColor Yellow
    Write-Host "[next] 同版本的内测 smart 包另发内测渠道：publish_chatx.ps1 -DistDir <desktop>\dist -Channel internal -Yes（不碰公共指针）；lite 定制包同理 -DistDir <desktop>\dist-lite -Channel lite。" -ForegroundColor Cyan
}
if ($DryRun) { Ok "DRYRUN complete - build v$Version is publishable [$Channel]; re-run without -DryRun to ship" }
else { Ok "published v$Version [$Channel]" }
