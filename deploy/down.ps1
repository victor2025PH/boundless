<# down.ps1 — 停服务。薄封装 deploy.ps1 -Action down。默认只提示，加 -Force 才真正停(或 -WhatIf 预览)。
   例： powershell -File deploy\down.ps1 -Only website -Force
#>
param([string]$Profile = 'core,web', [string]$Only = '', [switch]$WhatIf, [switch]$Force)
& (Join-Path $PSScriptRoot 'deploy.ps1') -Action down -Profile $Profile -Only $Only -WhatIf:$WhatIf -Force:$Force
