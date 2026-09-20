$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'asr_server' } |
    ForEach-Object {
        Write-Output ("pid=" + $_.ProcessId + " parent=" + $_.ParentProcessId + " created=" + $_.CreationDate)
        Write-Output ("  cmd=" + $_.CommandLine)
    }
