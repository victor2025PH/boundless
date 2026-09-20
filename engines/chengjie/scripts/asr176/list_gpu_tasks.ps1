$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
Get-ScheduledTask | Where-Object {
    $_.TaskName -match 'AITR|ASR|Ollama|STT|Nemo|Emotion|Watchdog|ollama'
} | ForEach-Object {
    $i = $_ | Get-ScheduledTaskInfo
    Write-Output ($_.TaskName + " state=" + $_.State + " limit=" + $_.Settings.ExecutionTimeLimit + " last=" + $i.LastRunTime + " result=" + $i.LastTaskResult)
}
