<# up.ps1 — 拉起全栈（幂等；端口在听则跳过）。薄封装 deploy.ps1 -Action up。
   例： powershell -File deploy\up.ps1                 # 起 core+web
        powershell -File deploy\up.ps1 -Profile all    # 含 gpu(需 -Force/-Only 逐个确认)
        powershell -File deploy\up.ps1 -Only huoke -WhatIf
#>
param([string]$Profile = 'core,web', [string]$Only = '', [switch]$WhatIf, [switch]$Force)
& (Join-Path $PSScriptRoot 'deploy.ps1') -Action up -Profile $Profile -Only $Only -WhatIf:$WhatIf -Force:$Force
