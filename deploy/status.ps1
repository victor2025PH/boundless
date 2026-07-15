<# status.ps1 — 全栈健康(GO/DEGRADED/DOWN)。薄封装 deploy.ps1 -Action status。只读。
   退出码：0=GO 1=DEGRADED 2=DOWN（供 cron/监控消费）
   例： powershell -File deploy\status.ps1
        powershell -File deploy\status.ps1 -Profile all -Json
#>
param([string]$Profile = 'core,web', [string]$Only = '', [switch]$Json)
& (Join-Path $PSScriptRoot 'deploy.ps1') -Action status -Profile $Profile -Only $Only -Json:$Json
exit $LASTEXITCODE
