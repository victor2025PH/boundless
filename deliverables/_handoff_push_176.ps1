# Runs ON 176. Copies HANDOFF_176_RELEASE_1.5.0.md from home dir into the AvatarHub repo root.
# ASCII-only on purpose: survives ssh/PowerShell quoting and any console codepage.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$src = Join-Path $env:USERPROFILE 'HANDOFF_176_RELEASE_1.5.0.md'
if (-not (Test-Path $src)) { Write-Output 'ERR: source md missing in home dir'; exit 1 }

$repo = $null
foreach ($root in @('C:\', 'D:\')) {
    if (-not (Test-Path $root)) { continue }
    $hit = Get-ChildItem $root -Directory -ErrorAction SilentlyContinue | Where-Object {
        (Test-Path (Join-Path $_.FullName 'pack_installer.py')) -and
        (Test-Path (Join-Path $_.FullName 'launcher_wizard.py'))
    } | Select-Object -First 1
    if ($hit) { $repo = $hit.FullName; break }
}
if (-not $repo) {
    # fallback: one level deeper
    foreach ($root in @('C:\', 'D:\')) {
        if (-not (Test-Path $root)) { continue }
        $hit = Get-ChildItem $root -Directory -ErrorAction SilentlyContinue |
            Get-ChildItem -Directory -ErrorAction SilentlyContinue | Where-Object {
                (Test-Path (Join-Path $_.FullName 'pack_installer.py')) -and
                (Test-Path (Join-Path $_.FullName 'launcher_wizard.py'))
            } | Select-Object -First 1
        if ($hit) { $repo = $hit.FullName; break }
    }
}
if (-not $repo) { Write-Output 'ERR: repo dir not found'; exit 2 }

$dst = Join-Path $repo 'HANDOFF_176_RELEASE_1.5.0.md'
Copy-Item $src $dst -Force
$len = (Get-Item $dst).Length

# Emit destination path as base64(UTF8) too, so it survives any console codepage.
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($dst))
Write-Output ('OK bytes=' + $len)
Write-Output ('OK dst_b64=' + $b64)
