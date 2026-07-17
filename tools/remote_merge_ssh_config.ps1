param([Parameter(Mandatory)][string]$SrcConfig)
$ErrorActionPreference = 'Stop'
$ssh = Join-Path $env:USERPROFILE '.ssh'
New-Item -ItemType Directory -Force -Path $ssh | Out-Null
$dst = Join-Path $ssh 'config'
$body = Get-Content $SrcConfig -Raw -Encoding UTF8
$marker = '# === boundless cluster BEGIN ==='
$end = '# === boundless cluster END ==='
$block = "$marker`r`n$body`r`n$end`r`n"
if (Test-Path $dst) {
  $cur = Get-Content $dst -Raw -Encoding UTF8
  if ($cur -match [regex]::Escape($marker)) {
    $cur = [regex]::Replace($cur, '(?s)' + [regex]::Escape($marker) + '.*?' + [regex]::Escape($end), $block.TrimEnd())
    Set-Content -Path $dst -Value $cur -Encoding utf8
  } else {
    Add-Content -Path $dst -Value "`r`n$block" -Encoding utf8
  }
} else {
  Set-Content -Path $dst -Value $block -Encoding utf8
}
Write-Output "CONFIG_OK $dst"
