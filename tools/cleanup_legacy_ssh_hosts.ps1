# Remove stale cluster Host blocks that live OUTSIDE the managed boundless marker block.
# Problem this fixes: ssh uses the FIRST matching Host entry; leftover pre-2026-07 blocks
# (e.g. "Host hub176 gpu176 pc5090" at the top of ~/.ssh/config on the .117 box) shadow the
# freshly rendered definitions inside the marker block. We delete any out-of-marker block
# whose alias list intersects the cluster alias set; everything else (w03, lan-embed,
# github-push, raw-IP hosts...) is preserved verbatim. Original saved as config.bak_<stamp>.
# ASCII-ONLY source on purpose.
[CmdletBinding()]
param(
  # Comma-separated alias list to purge. Caller renders this from deploy/machines.json.
  [Parameter(Mandatory)][string]$Aliases
)
$ErrorActionPreference = 'Stop'
$purge = @{}
foreach ($a in ($Aliases -split ',')) { $t = $a.Trim(); if ($t) { $purge[$t.ToLowerInvariant()] = $true } }

$cfgPath = Join-Path (Join-Path $env:USERPROFILE '.ssh') 'config'
if (-not (Test-Path $cfgPath)) { Write-Output "NO_CONFIG $cfgPath"; exit 0 }

$raw = Get-Content $cfgPath -Raw -Encoding UTF8
$marker = '# === boundless cluster BEGIN ==='
$end = '# === boundless cluster END ==='

$mi = $raw.IndexOf($marker)
$ei = $raw.IndexOf($end)
if ($mi -ge 0 -and $ei -gt $mi) {
  $pre = $raw.Substring(0, $mi)
  $block = $raw.Substring($mi, $ei - $mi + $end.Length)
  $post = $raw.Substring($ei + $end.Length)
} else {
  $pre = $raw; $block = ''; $post = ''
}

function Remove-PurgedBlocks([string]$text, [hashtable]$purgeSet) {
  if (-not $text) { return @{ text = ''; removed = @() } }
  $lines = $text -split "(`r`n|`n)"
  # -split with capture keeps separators as elements; rebuild into logical lines instead
  $lines = $text -replace "`r`n", "`n" -split "`n"
  $out = New-Object System.Collections.Generic.List[string]
  $removed = New-Object System.Collections.Generic.List[string]
  $skipping = $false
  foreach ($line in $lines) {
    if ($line -match '^\s*Host\s+(.+)$') {
      $aliases = ($Matches[1].Trim()) -split '\s+'
      $hit = $false
      foreach ($al in $aliases) { if ($purgeSet.ContainsKey($al.ToLowerInvariant())) { $hit = $true; break } }
      if ($hit) {
        $skipping = $true
        $removed.Add(($aliases -join ' '))
        continue
      } else {
        $skipping = $false
      }
    }
    if (-not $skipping) { $out.Add($line) }
  }
  return @{ text = ($out -join "`r`n"); removed = $removed }
}

$preR = Remove-PurgedBlocks $pre $purge
$postR = Remove-PurgedBlocks $post $purge
$removedAll = @($preR.removed) + @($postR.removed)

if ($removedAll.Count -eq 0) {
  Write-Output "CLEAN_NOOP nothing to remove"
  exit 0
}

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
Copy-Item $cfgPath "$cfgPath.bak_$stamp" -Force

$newText = ($preR.text.TrimEnd() + "`r`n`r`n" + $block + "`r`n" + $postR.text.TrimStart()).TrimEnd() + "`r`n"
Set-Content -Path $cfgPath -Value $newText -Encoding utf8
Write-Output ("CLEAN_OK removed=[{0}] backup={1}" -f ($removedAll -join ' | '), "config.bak_$stamp")
