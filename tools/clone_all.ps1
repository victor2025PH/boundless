# Disaster-recovery / new-machine bootstrap: clone the umbrella repo with all subprojects.
# Requires an ssh key of the victor2025PH account configured as Host github-push (ssh.github.com:443).
# The materialized submodule trees are READ-ONLY copies -- real working dirs live per PROJECT_MAP.md.
param([string]$Root = 'D:\restore')
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path $Root | Out-Null
git clone --recurse-submodules git@github-push:victor2025PH/boundless.git (Join-Path $Root 'boundless')
Write-Output ("done: " + (Join-Path $Root 'boundless') + " (treat submodule checkouts as read-only)")
