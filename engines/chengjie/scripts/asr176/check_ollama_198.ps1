$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
Write-Output '=== listen 11434 ==='
netstat -ano | Select-String ':11434'
$t = Get-ScheduledTask -TaskName 'OllamaServe198' -ErrorAction SilentlyContinue
if ($t) {
    Write-Output ("OllamaServe198 limit=" + $t.Settings.ExecutionTimeLimit + " state=" + $t.State)
    Write-Output ("actions=" + (($t.Actions | ForEach-Object { $_.Execute + ' ' + $_.Arguments }) -join ' | '))
}
