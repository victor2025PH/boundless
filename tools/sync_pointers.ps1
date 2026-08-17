# Refresh subproject (submodule) pointers in the umbrella repo.
# Policy: pointers only -- never materializes submodule working trees on dev machines.
# Usage: powershell -File tools\sync_pointers.ps1
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$subs = @('mofangyinse','zhituo','guanwang')  # append 'zhiliao','zhikong' after Phase B first push

$changed = $false
foreach ($s in $subs) {
  $lines = @(git -C $repoRoot ls-remote "git@github-push:victor2025PH/$s.git" HEAD 2>$null)
  if (-not $lines -or $lines.Count -eq 0) { Write-Warning "ls-remote failed: $s"; continue }
  $sha = ($lines[0] -split "`t")[0]
  $cur = (git -C $repoRoot rev-parse ":$s" 2>$null)
  if ($cur -ne $sha) {
    git -C $repoRoot update-index --add --cacheinfo "160000,$sha,$s"
    Write-Output "pointer updated: $s $cur -> $sha"
    $changed = $true
  }
}

if (-not $changed) { Write-Output 'pointers already up to date'; exit 0 }

# Safety: refuse to commit if anything besides the pointer paths is staged
# (this working tree is shared by multiple work lines).
$staged = @(git -C $repoRoot diff --cached --name-only)
$foreign = $staged | Where-Object { $subs -notcontains $_ }
if ($foreign) { Write-Error "other staged changes present, aborting commit: $foreign"; exit 1 }

git -C $repoRoot commit -m "chore(umbrella): refresh subproject pointers"
git -C $repoRoot push origin main
