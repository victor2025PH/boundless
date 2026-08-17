# push_announcement.ps1 - publish/manage ChatX in-app announcements WITHOUT shipping a build.
#
# publish_chatx.ps1 owns the "release-vX" entry that rides along with every release; this
# script owns everything else on downloads/announcements.json:
#   - urgent/notice announcements (maintenance windows, incident notices, feature news)
#   - the top-level min_supported_version forced-upgrade line (clients below it show a
#     non-dismissable "stop-support" banner and auto-download the newest build)
#   - removing stale entries
#
# Feed contract (consumed by desktop update-notify.js normalizeFeed):
#   { items: [ { id, type: urgent|release|notice, title, body, link, published_at,
#                target: { minVersion, maxVersion } } ],
#     min_supported_version: "x.y.z" }          # optional; absent/"" = no forced line
#
# Source of truth = the live public feed (fetched fresh), same as publish_chatx.ps1, so
# the two scripts and multiple operator machines never clobber each other.
#
# Propagation: clients poll on boot + every 6h -> an urgent notice reaches the whole fleet
# within ~6h (immediately for anyone restarting the app). Plan maintenance windows with that
# lead time in mind.
#
#   pwsh push_announcement.ps1 -List
#   pwsh push_announcement.ps1 -Type urgent -Title "今晚维护" -Body "22:00-22:30 服务升级，期间可能短暂掉线。" -DryRun
#   pwsh push_announcement.ps1 -Type notice -Title "新功能上线" -Body "..." -Link "https://bd2026.cc/blog/x"
#   pwsh push_announcement.ps1 -Remove ann-20260814-maintenance
#   pwsh push_announcement.ps1 -MinSupportedVersion 1.0.20      # force-upgrade everyone below 1.0.20
#   pwsh push_announcement.ps1 -ClearMinSupported               # lift the forced-upgrade line

param(
    # -- announcement entry (all optional; skip to only touch min_supported_version) --
    [ValidateSet("", "urgent", "notice", "release")]
    [string]$Type      = "",
    [string]$Title     = "",
    [string]$Body      = "",
    [string]$BodyFile  = "",
    [string]$Link      = "",
    [string]$Id        = "",       # default: ann-<yyyyMMdd>-<slug(Title)>; same id = replace (idempotent)
    [string]$TargetMin = "",       # only show to clients >= this version
    [string]$TargetMax = "",       # only show to clients <= this version
    # -- feed management --
    [string]$Remove    = "",       # remove entry by id
    [switch]$List,                 # print current public feed and exit
    [string]$MinSupportedVersion = "",  # set the forced-upgrade line (semver x.y.z)
    [switch]$ClearMinSupported,         # remove the forced-upgrade line
    # -- plumbing (same defaults as publish_chatx.ps1) --
    [switch]$DryRun,
    [switch]$Yes,
    [string]$Key       = "$HOME\.ssh\hualing_deploy",
    [string]$Vps       = "ubuntu@165.154.233.121",
    [string]$RemoteDir = "/home/ubuntu/yuntech/public/downloads",
    [string]$SiteUrl   = "https://bd2026.cc"
)
$ErrorActionPreference = "Stop"
$DownloadsDir = Join-Path (Split-Path -Parent $PSScriptRoot) "public\downloads"
$AnnLocal = Join-Path $DownloadsDir "announcements.json"

function Fail([string]$m) { Write-Host "[FAIL] $m" -ForegroundColor Red; exit 1 }
function Info([string]$m) { Write-Host "[..] $m" }
function Ok([string]$m)   { Write-Host "[OK] $m" -ForegroundColor Green }

# -- 1. fetch the live public feed (source of truth) ------------------------------
$items = @()
$minSupported = ""
try {
    $feed = (& curl.exe -s -m 15 "$SiteUrl/downloads/announcements.json" | Out-String).Trim()
    if ($feed -and $feed.StartsWith("{")) {
        $parsed = $feed | ConvertFrom-Json
        if ($parsed.items) { $items = @($parsed.items) }
        if ($parsed.min_supported_version) { $minSupported = [string]$parsed.min_supported_version }
    }
} catch { Info "no existing public announcements.json - starting fresh" }

if ($List) {
    Write-Host "min_supported_version: $(if ($minSupported) { $minSupported } else { '(none)' })"
    Write-Host "items ($($items.Count)):"
    foreach ($it in $items) {
        $tgt = ""
        if ($it.target -and ($it.target.minVersion -or $it.target.maxVersion)) {
            $tgt = " target=[$($it.target.minVersion)..$($it.target.maxVersion)]"
        }
        Write-Host ("  [{0,-7}] {1}  {2}  ({3}){4}" -f $it.type, $it.id, $it.title, $it.published_at, $tgt)
    }
    exit 0
}

# -- 2. validate + build the mutation ---------------------------------------------
$changes = @()

if ($Remove) {
    $before = $items.Count
    $items = @($items | Where-Object { $_.id -ne $Remove })
    if ($items.Count -eq $before) { Fail "no entry with id '$Remove' (use -List to inspect)" }
    $changes += "remove entry '$Remove'"
}

if ($Type) {
    $bodyText = $Body
    if (-not $bodyText -and $BodyFile) {
        if (-not (Test-Path $BodyFile)) { Fail "-BodyFile not found: $BodyFile" }
        $bodyText = (Get-Content $BodyFile -Raw -Encoding UTF8).Trim()
    }
    if (-not $Title -and -not $bodyText) { Fail "-Type given but no -Title/-Body (nothing to announce)" }
    if (-not $Id) {
        $slug = ($Title -replace '[^\w\u4e00-\u9fff-]+', '-').Trim('-')
        if ($slug.Length -gt 24) { $slug = $slug.Substring(0, 24).Trim('-') }
        if (-not $slug) { $slug = $Type }
        $Id = "ann-$((Get-Date).ToString('yyyyMMdd'))-$slug"
    }
    foreach ($v in @($TargetMin, $TargetMax)) {
        if ($v -and $v -notmatch '^\d+(\.\d+)*$') { Fail "target version '$v' is not x.y.z" }
    }
    $entry = [ordered]@{
        id           = $Id
        type         = $Type
        title        = $Title
        body         = $bodyText
        link         = $Link
        published_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        target       = [ordered]@{ minVersion = $TargetMin; maxVersion = $TargetMax }
    }
    $items = @(@($entry) + @($items | Where-Object { $_.id -ne $Id }) | Select-Object -First 20)
    $changes += "upsert [$Type] '$Id' ($Title)"
}

if ($ClearMinSupported) {
    if ($MinSupportedVersion) { Fail "-MinSupportedVersion and -ClearMinSupported are mutually exclusive" }
    if ($minSupported) { $changes += "clear min_supported_version (was $minSupported)" }
    $minSupported = ""
} elseif ($MinSupportedVersion) {
    if ($MinSupportedVersion -notmatch '^\d+\.\d+\.\d+$') { Fail "min_supported_version '$MinSupportedVersion' is not semver x.y.z" }
    # the line must point at (or below) a version that actually exists to install,
    # otherwise sub-line clients get an un-dismissable banner with no way out
    $ly = ""
    try { $ly = (& curl.exe -s -m 15 "$SiteUrl/downloads/latest.yml" | Out-String) } catch {}
    $latest = if ($ly -match "(?m)^version:\s*(.+?)\s*$") { $Matches[1].Trim() } else { "" }
    if (-not $latest) { Fail "cannot read public latest.yml - refusing to set a forced-upgrade line blind" }
    if ([version]$MinSupportedVersion -gt [version]$latest) {
        Fail "min_supported_version $MinSupportedVersion > public latest $latest (clients could never satisfy it)"
    }
    $changes += "set min_supported_version=$MinSupportedVersion (public latest: $latest)"
    $minSupported = $MinSupportedVersion
}

if ($changes.Count -eq 0) { Fail "nothing to do (give -Type/-Remove/-MinSupportedVersion/-ClearMinSupported, or -List)" }
Write-Host "planned changes:" -ForegroundColor Cyan
$changes | ForEach-Object { Write-Host "  - $_" }

if ($DryRun) { Ok "DRYRUN complete - feed untouched"; exit 0 }
if (-not $Yes) {
    $ans = Read-Host "Apply to $SiteUrl/downloads/announcements.json ? type YES"
    if ($ans -ne "YES") { Write-Host "aborted."; exit 0 }
}

# -- 3. stage locally + upload (same conventions as publish_chatx.ps1) -------------
New-Item -ItemType Directory -Force -Path $DownloadsDir | Out-Null
$feedObj = [ordered]@{ items = $items }
if ($minSupported) { $feedObj["min_supported_version"] = $minSupported }
$json = $feedObj | ConvertTo-Json -Depth 6
[IO.File]::WriteAllText($AnnLocal, $json, (New-Object Text.UTF8Encoding($false)))
Ok "staged $AnnLocal ($($items.Count) entries$(if ($minSupported) { ", min_supported_version=$minSupported" }))"

$scpBase = @("-i", $Key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new")
& scp @scpBase $AnnLocal "${Vps}:$RemoteDir/"
if ($LASTEXITCODE -ne 0) { Fail "scp announcements.json failed" }
& ssh @scpBase $Vps "pm2 restart yuntech --update-env >/dev/null 2>&1 && sleep 4 && echo restarted" | Out-Null
Ok "uploaded + pm2 restarted"

# R2 mirror (soft-fail, same lookup as publish_chatx.ps1)
$R2Conf = $env:RCLONE_R2_CONF
if (-not $R2Conf) {
    $R2Conf = @(
        'C:\模仿音色\secrets\deploy\rclone_r2.conf',
        (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'deploy\secrets\rclone_r2.conf')
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}
$RcloneExe = (Get-Command rclone -ErrorAction SilentlyContinue).Source
if (-not $RcloneExe -and (Test-Path 'C:\tools\rclone\rclone.exe')) { $RcloneExe = 'C:\tools\rclone\rclone.exe' }
if ($RcloneExe -and $R2Conf) {
    & $RcloneExe --config $R2Conf copy $DownloadsDir "r2:avatarhub/downloads" --include "announcements.json" --log-level ERROR
    if ($LASTEXITCODE -eq 0) { Ok "R2 mirror synced" } else { Write-Warning "R2 sync failed (clients still reach the VPS copy)" }
} else {
    Write-Warning "R2 sync skipped (rclone or conf missing)"
}

# -- 4. public verification --------------------------------------------------------
$pub = ""
try { $pub = (& curl.exe -s -m 15 "$SiteUrl/downloads/announcements.json" | Out-String).Trim() } catch {}
$okPub = $true
if ($Id -and $pub -notmatch [regex]::Escape($Id)) { $okPub = $false; Write-Warning "public feed missing entry '$Id'" }
if ($minSupported -and $pub -notmatch [regex]::Escape($minSupported)) { $okPub = $false; Write-Warning "public feed missing min_supported_version" }
if ($okPub) { Ok "public verified: $SiteUrl/downloads/announcements.json" }
Write-Host ""
Ok "done - clients pick this up on next poll (boot or <=6h)"
