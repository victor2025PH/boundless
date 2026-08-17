$ErrorActionPreference = "Continue"
Write-Host "HOST=$env:COMPUTERNAME"
Write-Host "=== tasks ==="
Get-ScheduledTask | Where-Object { $_.TaskName -like "*Boundless*" } | ForEach-Object {
  $i = $_ | Get-ScheduledTaskInfo
  "{0}`t{1}`trc={2}`tlast={3:MM-dd HH:mm}" -f $_.TaskName, $_.State, $i.LastTaskResult, $i.LastRunTime
}
Write-Host "=== env ==="
"EVENT_INGEST_KEY_len=$([string][Environment]::GetEnvironmentVariable('EVENT_INGEST_KEY','Machine')).Length)".Replace('.Length)','') 
# fix:
$k=[Environment]::GetEnvironmentVariable('EVENT_INGEST_KEY','Machine'); "EVENT_INGEST_KEY_len=$($k.Length)"
"SENTINEL_HEARTBEAT=$([Environment]::GetEnvironmentVariable('SENTINEL_HEARTBEAT','Machine'))"
Write-Host "=== sentinel script ==="
$s='D:\boundless\deploy\cron\cron_sentinel.ps1'
if (Test-Path $s) { "sentinel_len=$((Get-Item $s).Length)" } else { "sentinel MISSING" }
Write-Host "=== sentinel logs ==="
@(
  "$env:LOCALAPPDATA\boundless-sentinel\cron_sentinel.log",
  "C:\Windows\System32\config\systemprofile\AppData\Local\boundless-sentinel\cron_sentinel.log"
) | ForEach-Object {
  if (Test-Path $_) {
    Write-Host "-- $_ --"
    Get-Content $_ -Tail 12 -Encoding UTF8
  }
}
Write-Host "=== cron log tails ==="
$logDir='D:\boundless\logs\cron'
if (Test-Path $logDir) {
  Get-ChildItem $logDir -File | Sort-Object LastWriteTime -Descending | Select-Object -First 6 | ForEach-Object {
    Write-Host "---- $($_.Name) mtime=$($_.LastWriteTime) ----"
    Get-Content $_.FullName -Tail 4 -Encoding UTF8
  }
} else { Write-Host "no $logDir" }
Write-Host "=== sync script lfs ==="
cd D:\boundless
git config --local --get filter.lfs.smudge
git rev-parse --short HEAD 2>$null
git rev-parse --short origin/main 2>$null