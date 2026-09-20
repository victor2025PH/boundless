$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$t = Get-ScheduledTask -TaskName 'STT_Boot'
Write-Output ("STT_Boot limit=" + $t.Settings.ExecutionTimeLimit + " state=" + $t.State)
$o = Get-ScheduledTask -TaskName 'OllamaServe' -ErrorAction SilentlyContinue
if ($o) {
    $oi = $o | Get-ScheduledTaskInfo
    Write-Output ("OllamaServe state=" + $o.State + " limit=" + $o.Settings.ExecutionTimeLimit + " lastResult=" + $oi.LastTaskResult)
}
Write-Output '=== nvidia-smi ==='
nvidia-smi
