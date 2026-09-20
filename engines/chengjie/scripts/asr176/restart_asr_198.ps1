# Restart the ASR service via its scheduled task (run ON 192.168.0.198).
# 2026-08-29 the ASR+SER service moved 176 -> 198 (task name AITR_ASR_198); restart_asr.ps1
# still targets AITR_ASR_176 for the (now disabled) 176 deployment. Deploy flow from 117:
#   scp scripts\asr176\asr_server.py asr198:C:/aitr_asr/asr_server.py
#   ssh asr198 powershell -NoProfile -ExecutionPolicy Bypass -File C:\aitr_asr\restart_asr_198.ps1
#   Invoke-RestMethod http://192.168.0.198:8765/health   # asr_loaded=true ~15s later
$task = 'AITR_ASR_198'
schtasks /End /TN $task 2>$null | Out-Null
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'asr_server' } |
    ForEach-Object { Write-Output ("killing pid " + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }
Start-Sleep 1
schtasks /Run /TN $task | Out-Null
Write-Output 'restarted via AITR_ASR_198'
