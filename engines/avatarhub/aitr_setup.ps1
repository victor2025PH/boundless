$key = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJftPGt5w9hlHIeujzkCYyraJdxvfw19CtHs8hQ3xBXb deploy_key_2025'
$pri = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
Write-Host ("elevated=" + $pri.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction SilentlyContinue | Out-Null
Set-Service sshd -StartupType Automatic -ErrorAction SilentlyContinue
Start-Service sshd -ErrorAction SilentlyContinue
New-NetFirewallRule -Name sshd-in -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -ErrorAction SilentlyContinue | Out-Null
New-Item -ItemType Directory -Path 'C:\ProgramData\ssh' -Force | Out-Null
$aak = 'C:\ProgramData\ssh\administrators_authorized_keys'
if (!(Test-Path $aak) -or !(Select-String -Path $aak -SimpleMatch $key -Quiet)) { Add-Content -Path $aak -Value $key -Encoding ascii }
icacls $aak /inheritance:r /grant '*S-1-5-18:F' /grant '*S-1-5-32-544:F' | Out-Null
New-Item -ItemType Directory -Path "$env:USERPROFILE\.ssh" -Force | Out-Null
$uak = "$env:USERPROFILE\.ssh\authorized_keys"
if (!(Test-Path $uak) -or !(Select-String -Path $uak -SimpleMatch $key -Quiet)) { Add-Content -Path $uak -Value $key -Encoding ascii }
Restart-Service sshd -ErrorAction SilentlyContinue
Write-Host '===== AITR REPORT BEGIN ====='
Write-Host ("host=" + $env:COMPUTERNAME + "  user=" + $env:USERNAME)
Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {$_.IPAddress -notlike '169.254*' -and $_.IPAddress -ne '127.0.0.1'} | ForEach-Object { Write-Host ("ip=" + $_.IPAddress + " (" + $_.InterfaceAlias + ")") }
Get-NetRoute -DestinationPrefix 0.0.0.0/0 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host ("gateway=" + $_.NextHop) }
Write-Host ("reach_117=" + (Test-Connection 192.168.0.117 -Count 1 -Quiet -ErrorAction SilentlyContinue))
Write-Host ("sshd=" + (Get-Service sshd -ErrorAction SilentlyContinue).Status)
Select-String -Path C:\ProgramData\ssh\sshd_config -Pattern 'PubkeyAuthentication|PasswordAuthentication|AuthorizedKeysFile' -ErrorAction SilentlyContinue | ForEach-Object { Write-Host ("cfg: " + $_.Line) }
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) { nvidia-smi --query-gpu=name,memory.total,memory.used,driver_version --format=csv,noheader } else { Write-Host 'nvidia-smi: MISSING' }
if (Get-Command ollama -ErrorAction SilentlyContinue) { ollama --version; ollama list } else { Write-Host 'ollama: MISSING' }
if (Get-Command python -ErrorAction SilentlyContinue) { python --version } else { Write-Host 'python: MISSING' }
if (Get-Command py -ErrorAction SilentlyContinue) { py -0p } else { Write-Host 'py-launcher: MISSING' }
Get-PSDrive C | ForEach-Object { Write-Host ("disk_C_freeGB=" + [math]::Round($_.Free/1GB,1)) }
Write-Host '===== AITR REPORT END ====='
